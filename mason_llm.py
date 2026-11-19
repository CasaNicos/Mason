"""mason_llm.py — the single LLM gateway for Mason.

Every model call in Mason routes through this module. Callers name an *intent*
(what they need), never a model string. The gateway maps intent -> provider using
the two-axis policy from docs/prompts/LLM_REWIRE_QWEN_OPUS_PROMPT.md:

    Capability axis (is qwen smart enough?):   qwen3.5:4b  ->  Opus (deep only)
    Availability axis (is qwen up?):           qwen3.5:4b  ->  phi4-mini -> retry -> restart

The two axes MUST NOT cross: Opus is never an availability fallback; phi/retry is
never a capability upgrade.

Intents
    "brain"  -> interactive 38-tool loop (streaming + tools). Default qwen; Opus on escalation.
    "chore"  -> short worker calls: voice, news, summaries, debate, arbiter, book/track. qwen.
    "deep"   -> high-level deep analysis (predictor, explicit deep asks). Opus.

The only three models Mason may ever call: qwen3.5:4b (workhorse), phi4-mini
(availability fallback), claude-opus-5-5 (deep). qwen3:8b is OUT — never called.

PHASE 1 STATUS: local (qwen/phi transport) + the tool-protocol adapter + complete()
+ chat_stream() local path are fully implemented and tested. The Opus branch and the
full availability ladder (phi -> escalating retry -> restart) are documented stubs,
wired in Phase 4 (brain) and Phase 5 (failover). This module does NOT import mason_app.

2026-11-13 (audit O2/O3): the local context window is chosen per purpose (see
pick_background_ctx), workhorse calls send keep_alive, the restart step fires only on
a real outage (never on a rejected request), and the one-shot Opus helper sets effort,
a thinking-safe token budget, raises on an empty reply and logs its usage.
"""

import json
import logging
import os
import re
import threading
import time

import requests

import mason_trace as _trace   # flag-gated full-workflow tracing (no-op when off)

log = logging.getLogger("mason.llm")


def _tool_names(oll_tools):
    """Just the tool names, for compact trace fields."""
    try:
        return [t["function"]["name"] for t in (oll_tools or [])]
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
# Config — read mason.config.json once, pull the "llm" block. Nothing hardcoded;
# the defaults below only backstop a missing/partial config so import never dies.
# ─────────────────────────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mason.config.json")

_DEFAULT_LLM = {
    # effort / min_max_tokens: used by the one-shot Opus helper (OpusProvider.complete).
    "deep":  {"provider": "anthropic", "model": "claude-opus-5-5",
              "effort": "medium", "min_max_tokens": 16000},
    # num_ctx            = the CHAT window (the tool loop's 34-40k-token prompts need it).
    # background_num_ctx = the window for every non-chat call (desk, triage, scoring,
    #                      digests, fact-check...). 0/None switches the feature off
    #                      (everything runs at num_ctx again). See pick_background_ctx.
    # ctx_ladder         = steps between the two, for the rare oversized background prompt.
    # ctx_hold_seconds   = after a call that needed a bigger window, background calls
    #                      keep using it this long, so the model is not reloaded back
    #                      and forth between chat turns.
    # keep_alive         = sent on workhorse calls so Ollama keeps the model loaded.
    #                      fallback_keep_alive None = not sent (Ollama's short default),
    #                      so phi never holds the one VRAM slot.
    "local": {"provider": "ollama", "base_url": "http://localhost:11434",
              "workhorse": "qwen3.5:4b", "fallback": "phi4-mini",
              "num_ctx": 40960, "temperature": 0.3, "think": False,
              "background_num_ctx": 8192, "ctx_ladder": [16384], "ctx_hold_seconds": 180,
              "keep_alive": "30m", "fallback_keep_alive": None},
    "retry_backoff_seconds": [15, 30, 60, 120, 240, 300, 300, 300, 300, 300],
    # False = never exit the process from the availability ladder, whatever happens. Default False since
    # 2026-11-15 (runtime hardening): a chat/model outage must never take the trading process down; with the
    # setting missing or the config unreadable the process stays up. True is an explicit opt-in.
    "restart_on_local_outage": False,
}

_cfg_cache = None


def _load_config():
    """Load and cache the whole config dict once."""
    global _cfg_cache
    if _cfg_cache is None:
        try:
            with open(_CONFIG_PATH) as f:
                _cfg_cache = json.load(f)
        except Exception as e:  # pragma: no cover - config should always parse
            log.error("mason_llm: could not read %s (%s); using defaults", _CONFIG_PATH, e)
            _cfg_cache = {}
    return _cfg_cache


def _llm_cfg():
    """The 'llm' block, with defaults filled for any missing key."""
    llm = dict(_DEFAULT_LLM)
    llm.update(_load_config().get("llm", {}))
    # shallow-merge the nested dicts so a partial config still gets safe defaults
    for k in ("deep", "local"):
        merged = dict(_DEFAULT_LLM[k])
        merged.update(llm.get(k, {}))
        llm[k] = merged
    return llm


def _anthropic_key():
    # B1b: env ANTHROPIC_API_KEY > private secrets file > this config (byte-exact legacy).
    import mason_secrets as _S
    return _S.get("ANTHROPIC_API_KEY", "", cfg=_load_config())


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral tuning (spec §5.5) — first-class acceptance criteria, not optional.
# These are named so tests can assert they are actually applied on the local path.
#   - think:False        -> kills qwen's self-correction / waffling mid-answer
#   - low temperature    -> from config (~0.3), fewer reconsiderations
#   - terse house rule    -> prepended to the local system prompt
# ─────────────────────────────────────────────────────────────────────────────
LOCAL_HOUSE_STYLE = (
    "Commit to one answer; do not narrate corrections or reconsider out loud. "
    "No preamble, do not restate the question, answer in the fewest correct words. "
    "TOOL CALLS: when you need a tool, invoke it as your VERY FIRST action with ZERO "
    "text before it, using the structured tool-call mechanism only. Never announce a "
    "tool in prose ('let me check', 'pulling the quote now', 'fetching', 'let me look "
    "that up', 'one moment') and never type a tool call out as text or tags — that "
    "narration reaches the user as a broken half-sentence before the real answer. "
    "Invoke the tool silently, then, after its result comes back, give the answer in "
    "prose (e.g. 'NVDA is at $500, up 2%.')."
)

# Explicit deep-analysis ask (mirrors mason_app._OPUS_DEEP_RE — copied here on purpose
# so the gateway has NO dependency on mason_app). TODO(Nico) §11: final policy dial.
_OPUS_DEEP_RE = re.compile(
    r"\b(deep\s+analysis|dig\s+deep|go\s+(?:as\s+)?deep|deep\s+dive|use\s+opus|"
    r"think\s+hard(?:er)?|full\s+analysis|thorough\s+analysis)\b",
    re.IGNORECASE,
)

# Sentinel qwen can emit to hand a turn up to Opus mid-stream (wired fully in Phase 4).
ESCALATE_SENTINEL = "[[ESCALATE]]"


class LocalModelError(Exception):
    """Raised on any local (Ollama) failure: down, timeout, OOM, malformed/empty output.
    The availability ladder (Phase 5) catches this; callers never see a raw model error.

    `status` is the HTTP status when the server answered with an error, else None.
    `kind` tells the ladder what sort of failure it was:
      "unavailable"  server down, timeout, 5xx, 408 or 429. Worth retrying. The ONLY
                     kind that can lead to the restart step.
      "client"       any other 4xx: the REQUEST was rejected. Sending it again gets the
                     same answer, so it is not retried on the backoff schedule and can
                     never restart the service.
      "empty"        the model answered, but with nothing usable.
    A bare LocalModelError("...") is "unavailable", as before."""

    def __init__(self, msg="", *, status=None, kind=None):
        super().__init__(msg)
        self.status = status
        if kind is None:
            kind = "client" if _is_client_status(status) else "unavailable"
        self.kind = kind


class DeepModelError(Exception):
    """The deep (Claude) model came back with no text at all, e.g. its thinking used
    up the whole max_tokens budget (stop_reason "max_tokens") or it declined
    ("refusal"). Raised by OpusProvider.complete so a caller never mistakes an empty
    string for an answer."""


def _is_client_status(status) -> bool:
    try:
        st = int(status)
    except (TypeError, ValueError):
        return False
    return 400 <= st < 500 and st not in (408, 429)


def _http_status(exc):
    """HTTP status carried by a requests exception, or None (connection refused,
    timeout...). NB: a requests.Response is falsy for 4xx/5xx, so test `is not None`."""
    resp = getattr(exc, "response", None)
    try:
        return int(resp.status_code) if resp is not None else None
    except Exception:
        return None


def _local_error(model, what, exc):
    """Wrap a transport exception as LocalModelError, keeping the HTTP status and the
    server's own short error text (Ollama says WHY it rejected a request in the body)."""
    status = _http_status(exc)
    detail = ""
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            detail = " ".join((resp.text or "").split())[:300]
    except Exception:
        detail = ""
    return LocalModelError(f"{model} {what}: {exc}" + (f" [{detail}]" if detail else ""),
                           status=status)


class _ForcedContextTruncated(Exception):
    """Raised inside the forced-tool /v1 path when Ollama's OpenAI-compat endpoint
    truncated the prompt. /v1 caps at the model's BAKED num_ctx (default 4096) and
    IGNORES a per-request num_ctx (verified 2026-09-06), so on a long brain prompt
    it silently drops the tools + history. Signals chat_stream to fall back to the
    native /api/chat path, which honors options.num_ctx — a best-effort (unforced)
    call with the full context beats a forced call the model made half-blind. Goes
    away once the workhorse model is built with num_ctx baked (or OLLAMA_CONTEXT_LENGTH
    is set on the service); then /v1 honors the context and this never fires."""
    def __init__(self, got, sent):
        self.got, self.sent = got, sent
        super().__init__(f"/v1 truncated prompt: processed {got} of ~{sent} tokens")


def _approx_prompt_tokens(messages, tools):
    """Cheap chars/4 token estimate of an Ollama-shape message list + tools, used
    ONLY to detect gross /v1 truncation. Approximate on purpose."""
    n = 0
    for m in messages or []:
        n += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            n += len(str(tc))
    if tools:
        n += len(json.dumps(tools))
    return n // 4


# ─────────────────────────────────────────────────────────────────────────────
# Tool-protocol adapter (spec §4.3) — the hard part. Translate BOTH directions
# between Anthropic shapes (what mason_app builds) and Ollama/OpenAI shapes (what
# qwen speaks). The Opus path keeps native Anthropic shapes; only the qwen/phi path
# goes through here.
# ─────────────────────────────────────────────────────────────────────────────
# Chat sight (2026-11-16, docs/fable/CHATSIGHT_FOR_NICO.md): Claude routes (picks, Opus escalation) get
# query_db + describe_table; the local model does not. Measured on the live prompt, qwen called neither
# (0 of 8 records questions) while their text cost every local call ~950 prompt tokens. The local model
# sees the same data through mason_memory_search.chat_lookup instead.
LOCAL_HIDDEN_TOOLS = frozenset({"query_db", "describe_table"})


def _local_tools(tools):
    """The tool list a local (Ollama) model is offered: `tools` minus LOCAL_HIDDEN_TOOLS."""
    return [t for t in (tools or []) if t.get("name") not in LOCAL_HIDDEN_TOOLS] or None


def anthropic_tools_to_ollama(tools):
    """Anthropic [{name, description, input_schema}] -> Ollama/OpenAI function tools.
    input_schema and parameters are both JSON-Schema, so this is a structural remap."""
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                # input_schema == JSON-Schema == OpenAI "parameters"
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def flatten_system(system):
    """mason_app may pass `system` as a plain string OR as a list of Anthropic content
    blocks (the prompt-cache layout). Ollama wants a single string — flatten it."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(system)


def _block_text(content):
    """Anthropic message .content may be a string or a list of blocks. Return the
    concatenated plain text (ignoring non-text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def anthropic_messages_to_ollama(messages, system=None):
    """Translate an Anthropic message history to Ollama's message list.

    - system (string or block list)          -> {"role":"system", ...} first
    - user string                            -> {"role":"user","content": ...}
    - user tool_result blocks                -> {"role":"tool","content": <result text>}
    - assistant text                         -> {"role":"assistant","content": ...}
    - assistant tool_use blocks              -> {"role":"assistant","tool_calls":[...]}
    """
    out = []
    sys_txt = flatten_system(system)
    if sys_txt:
        out.append({"role": "system", "content": sys_txt})

    for m in messages or []:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, list):
                # may hold tool_result blocks and/or text blocks
                tool_results = [b for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        c = tr.get("content")
                        # tool_result content is a string or a list of text blocks
                        txt = c if isinstance(c, str) else _block_text(c)
                        out.append({"role": "tool", "content": txt})
                    leftover = _block_text(content)
                    if leftover.strip():
                        out.append({"role": "user", "content": leftover})
                else:
                    out.append({"role": "user", "content": _block_text(content)})
            else:
                out.append({"role": "user", "content": content or ""})

        elif role == "assistant":
            if isinstance(content, list):
                tool_uses = [b for b in content
                             if isinstance(b, dict) and b.get("type") == "tool_use"]
                text = _block_text(content)
                msg = {"role": "assistant", "content": text}
                if tool_uses:
                    msg["tool_calls"] = [{
                        "id": b.get("id", ""),
                        "function": {"name": b.get("name"), "arguments": b.get("input", {})},
                    } for b in tool_uses]
                out.append(msg)
            else:
                out.append({"role": "assistant", "content": content or ""})
        else:
            # pass through anything already in Ollama shape (e.g. role == "tool")
            out.append(m)
    return out


def normalize_ollama_tool_call(tc):
    """Ollama tool_call {id?, function:{name, arguments}} -> (name, input_dict, id).
    `arguments` may be a dict (qwen native) or a JSON string — handle both. This is
    the SAME (name, input) shape mason_app._execute_tool(name, input) consumes."""
    fn = tc.get("function", {})
    name = fn.get("name")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except Exception:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    call_id = tc.get("id") or f"call_{name}_{int(time.time() * 1000) % 100000}"
    return name, args, call_id


# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────
class _OllamaProvider:
    """Shared Ollama transport. QwenProvider and PhiProvider differ only in model tag."""

    def __init__(self, model, base_url, num_ctx, temperature, think, tier, keep_alive=None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.think = think
        self.tier = tier  # "local" (qwen) or "phi", for the UI __model_tier__ channel
        # Sent as the request's top-level "keep_alive" (how long Ollama keeps the model
        # loaded after this call). None = not sent, Ollama's own default (5 min) applies.
        self.keep_alive = keep_alive

    def _finish_body(self, body):
        ka = getattr(self, "keep_alive", None)
        if ka is not None and ka != "":
            body["keep_alive"] = ka
        _note_ctx_sent(self.model, body.get("options", {}).get("num_ctx"))

    def _options(self, opts):
        o = {"num_ctx": self.num_ctx, "temperature": self.temperature}
        # allow per-call overrides (e.g. num_predict cap, temperature)
        for k in ("num_ctx", "temperature", "num_predict", "top_p", "stop"):
            if k in opts:
                o[k] = opts[k]
        return o

    def _system_with_house_style(self, system, style=None):
        sys_txt = flatten_system(system)
        # Prepend the terse house-style rule (§5.5) so it is always applied locally.
        # style=None (every caller but one) = LOCAL_HOUSE_STYLE, exactly as before. style="" = no house
        # rule: the small-prompt chat (2026-11-17, chat.local_small_prompt) carries its own short prompt and
        # is offered no tools, so the house rule's tool-call instructions would only mislead it.
        style = LOCAL_HOUSE_STYLE if style is None else style
        if not style:
            return sys_txt
        if sys_txt:
            return style + "\n\n" + sys_txt
        return style

    def complete(self, system, user, **opts):
        """Non-streaming single-shot text."""
        messages = [{"role": "user", "content": user}]
        sys_txt = self._system_with_house_style(system)
        body = {
            "model": self.model,
            "messages": ([{"role": "system", "content": sys_txt}] + messages),
            "stream": False,
            "think": self.think,
            "options": self._options(opts),
        }
        self._finish_body(body)
        _trace.emit("llm_request", provider=self.tier, model=self.model,
                    endpoint="/api/chat", stream=False, force_tool=False,
                    num_ctx=body["options"].get("num_ctx"),
                    temperature=body["options"].get("temperature"), think=self.think,
                    tools=[], msg_count=len(body["messages"]),
                    approx_tokens=_approx_prompt_tokens(body["messages"], None),
                    messages=body["messages"])
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=body,
                              timeout=opts.get("timeout", 180))
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            _trace.emit("llm_error", provider=self.tier, model=self.model,
                        endpoint="/api/chat", stream=False, error=str(e))
            raise _local_error(self.model, "complete failed", e)
        text = (data.get("message") or {}).get("content", "")
        if not text.strip():
            _trace.emit("llm_error", provider=self.tier, model=self.model,
                        endpoint="/api/chat", stream=False, error="empty output")
            raise LocalModelError(f"{self.model} returned empty output", kind="empty")
        _trace.emit("llm_response", provider=self.tier, model=self.model,
                    endpoint="/api/chat", stream=False, stop_reason="end",
                    input_tokens=int(data.get("prompt_eval_count") or 0),
                    output_tokens=int(data.get("eval_count") or 0),
                    tool_calls=[], text=text)
        return text

    def _forced_chat_openai(self, messages, system, tools, **opts):
        """Forced tool call via Ollama's OpenAI-compat /v1/chat/completions — the
        only endpoint that HONORS tool_choice='required' (native /api/chat ignores
        it). Non-streaming: a forced pass exists to COMPEL a tool call whose result
        feeds the next round, so there is no prose to stream. Re-emits the same
        provider-agnostic event shape. Raises _ForcedContextTruncated if /v1 chopped
        the prompt (it ignores per-request num_ctx), so chat_stream falls back to
        native with the full context."""
        sys_txt = self._system_with_house_style(system)
        oll_messages = anthropic_messages_to_ollama(messages, system=sys_txt)
        oll_tools = anthropic_tools_to_ollama(_local_tools(tools))
        body = {
            "model": self.model,
            "messages": oll_messages,
            "tools": oll_tools,
            "tool_choice": "required",
            "temperature": self.temperature,
            "max_tokens": int(opts.get("num_predict") or opts.get("max_tokens") or 1024),
            "stream": False,
        }
        _trace.emit("llm_request", provider=self.tier, model=self.model,
                    endpoint="/v1/chat/completions", stream=False, force_tool=True,
                    tool_choice="required", temperature=self.temperature,
                    tools=_tool_names(oll_tools), msg_count=len(oll_messages),
                    approx_tokens=_approx_prompt_tokens(oll_messages, oll_tools),
                    messages=oll_messages)

        # Request FIRST, before any yield, so a truncation bail emits nothing and
        # chat_stream can cleanly fall through to the native path.
        try:
            r = requests.post(f"{self.base_url}/v1/chat/completions", json=body,
                              timeout=opts.get("timeout", 240))
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            _trace.emit("llm_error", provider=self.tier, model=self.model,
                        endpoint="/v1/chat/completions", error=str(e))
            raise _local_error(self.model, "forced /v1 failed", e)
        if isinstance(data, dict) and data.get("error"):
            raise LocalModelError(f"{self.model} forced /v1 error: {data['error']}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        sent = _approx_prompt_tokens(oll_messages, oll_tools)
        # /v1 caps at the model's baked num_ctx (default 4096) and ignores a
        # per-request one. If it processed far fewer tokens than we sent, it
        # truncated — bail so chat_stream uses native (full context) instead.
        if in_tok and sent - in_tok > 2000:
            _trace.emit("llm_forced_truncated", provider=self.tier, model=self.model,
                        processed_tokens=in_tok, approx_sent=sent)
            raise _ForcedContextTruncated(in_tok, sent)
        yield {"type": "tier", "tier": self.tier, "model": self.model}
        emitted = False
        _resp_tools = []
        for tc in msg.get("tool_calls") or []:
            name, args, cid = normalize_ollama_tool_call(tc)
            emitted = True
            _resp_tools.append({"name": name, "input": args})
            yield {"type": "tool_call", "name": name, "input": args, "id": cid}
        text = msg.get("content") or ""
        if text and not emitted:
            yield {"type": "text", "text": text}
        _trace.emit("llm_response", provider=self.tier, model=self.model,
                    endpoint="/v1/chat/completions", forced=True,
                    stop_reason="tool_use" if emitted else "end",
                    input_tokens=in_tok, output_tokens=out_tok,
                    tool_calls=_resp_tools, text=text)
        yield {"type": "usage", "provider": self.tier, "model": self.model,
               "usage": None, "input_tokens": in_tok, "output_tokens": out_tok}
        yield {"type": "done", "stop_reason": "tool_use" if emitted else "end"}

    def chat_stream(self, messages, system, tools=None, **opts):
        """Generator yielding provider-agnostic events (spec §4.3):
             {"type":"tier","tier":...}
             {"type":"text","text": <delta>}
             {"type":"tool_call","name":...,"input":{...},"id":...}
             {"type":"done","stop_reason":"end"|"tool_use"}
        Streams text as it arrives; tool_calls typically land in the final chunk.

        force_tool routes to the OpenAI-compat /v1 path (the ONLY endpoint that
        honors tool_choice='required'; the native /api/chat below silently ignores
        it — both verified 2026-09-06), with an automatic fall-back to native if /v1
        truncated the prompt. See _forced_chat_openai / _ForcedContextTruncated."""
        if opts.get("force_tool") and tools:
            try:
                yield from self._forced_chat_openai(messages, system, tools, **opts)
                return
            except _ForcedContextTruncated as e:
                record_activity({"type": "forced_v1_truncated", "model": self.model,
                                 "processed_tokens": e.got, "approx_sent": e.sent,
                                 "action": "fallback_native_besteffort"})
                # fall through: native honors options.num_ctx but can't force the call
        # House-style rule prepended to system; then translate history to Ollama shape.
        sys_txt = self._system_with_house_style(system, opts.get("house_style"))
        oll_messages = anthropic_messages_to_ollama(messages, system=sys_txt)
        body = {
            "model": self.model,
            "messages": oll_messages,
            "stream": True,
            "think": self.think,
            "options": self._options(opts),
        }
        self._finish_body(body)
        oll_tools = anthropic_tools_to_ollama(_local_tools(tools))
        if oll_tools:
            body["tools"] = oll_tools
            # NOTE: native /api/chat IGNORES tool_choice (confirmed 2026-09-06), so
            # this line only ever runs as the truncation fall-back from the /v1
            # forced path above — the real forcing lives in _forced_chat_openai.
            # Kept (harmless) so a future Ollama that honors it here Just Works.
            if opts.get("force_tool"):
                body["tool_choice"] = "required"

        _trace.emit("llm_request", provider=self.tier, model=self.model,
                    endpoint="/api/chat", stream=True, force_tool=bool(opts.get("force_tool")),
                    num_ctx=body["options"].get("num_ctx"), temperature=self.temperature,
                    think=self.think,
                    tools=_tool_names(oll_tools), msg_count=len(oll_messages),
                    approx_tokens=_approx_prompt_tokens(oll_messages, oll_tools),
                    messages=oll_messages)

        # Carry the concrete model tag (e.g. "qwen3.5:4b", "phi4-mini") on the tier
        # event so the UI badge can show the real model, not just the tier code.
        yield {"type": "tier", "tier": self.tier, "model": self.model}

        emitted_tool = False
        got_any = False
        in_tok = out_tok = 0
        _resp_text = ""                     # accumulated for the trace
        _resp_tools = []                    # [{"name","input"}] for the trace
        try:
            with requests.post(f"{self.base_url}/api/chat", json=body, stream=True,
                               timeout=opts.get("timeout", 300)) as r:
                if r.status_code >= 400:
                    # read the server's reason NOW: the body is gone once the stream closes
                    detail = " ".join((r.text or "").split())[:300]
                    _trace.emit("llm_error", provider=self.tier, model=self.model,
                                endpoint="/api/chat", error=f"HTTP {r.status_code} {detail}")
                    raise LocalModelError(
                        f"{self.model} stream failed: HTTP {r.status_code}"
                        + (f" [{detail}]" if detail else ""), status=r.status_code)
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    got_any = True
                    msg = chunk.get("message") or {}

                    delta = msg.get("content") or ""
                    if delta:
                        _resp_text += delta
                        yield {"type": "text", "text": delta}

                    for tc in msg.get("tool_calls") or []:
                        name, args, cid = normalize_ollama_tool_call(tc)
                        emitted_tool = True
                        _resp_tools.append({"name": name, "input": args})
                        yield {"type": "tool_call", "name": name, "input": args, "id": cid}

                    if chunk.get("done"):
                        # token counts for the ledger ($0 locally — see brain loop)
                        in_tok = int(chunk.get("prompt_eval_count") or 0)
                        out_tok = int(chunk.get("eval_count") or 0)
                        break
        except LocalModelError:
            raise
        except Exception as e:
            _trace.emit("llm_error", provider=self.tier, model=self.model,
                        endpoint="/api/chat", error=str(e), text_so_far=_resp_text)
            raise _local_error(self.model, "stream failed", e)

        if not got_any:
            _trace.emit("llm_error", provider=self.tier, model=self.model,
                        endpoint="/api/chat", error="no output")
            raise LocalModelError(f"{self.model} produced no output", kind="empty")
        _trace.emit("llm_response", provider=self.tier, model=self.model,
                    endpoint="/api/chat",
                    stop_reason="tool_use" if emitted_tool else "end",
                    input_tokens=in_tok, output_tokens=out_tok,
                    tool_calls=_resp_tools, text=_resp_text)
        # usage event: provider-agnostic cost hook. Local models cost $0, but the
        # brain still records a ledger row (Phase-7 "local = $0" gate) and can see
        # token volume for latency/VRAM reasoning.
        yield {"type": "usage", "provider": self.tier, "model": self.model,
               "usage": None, "input_tokens": in_tok, "output_tokens": out_tok}
        yield {"type": "done", "stop_reason": "tool_use" if emitted_tool else "end"}


def _qwen():
    c = _llm_cfg()["local"]
    return _OllamaProvider(c["workhorse"], c["base_url"], c["num_ctx"],
                           c["temperature"], c.get("think", False), tier="local",
                           keep_alive=c.get("keep_alive"))


def _phi():
    c = _llm_cfg()["local"]
    # No long keep-alive for the fallback: it must not hold the single VRAM slot.
    return _OllamaProvider(c["fallback"], c["base_url"], c["num_ctx"],
                           c["temperature"], c.get("think", False), tier="phi",
                           keep_alive=c.get("fallback_keep_alive"))


# ─────────────────────────────────────────────────────────────────────────────
# Context-window size per purpose (audit O2, 2026-11-13).
#
# Ollama sizes the model's memory for `num_ctx` when it LOADS the model. At the chat
# size (40,960) qwen3.5:4b does not fit the 6 GB card: part of it runs on the CPU
# (measured 14%/86% CPU/GPU, 5.4 GB of system RAM, 46 tokens/s). At 8,192 or 16,384
# it is 100% on the GPU (1.3 GB RAM, 62 tokens/s). Only the interactive tool loop
# needs the big window (its prompt is 34-40k tokens); the largest non-chat prompt in
# a week was 4,610 tokens.
#
# Callers already say which they are by the entry point they use:
#   chat_stream(...)  -> the interactive brain      -> llm.local.num_ctx (big)
#   complete(...)     -> every background/desk call -> llm.local.background_num_ctx
# so no caller had to change.
#
# A different num_ctx on the next call makes Ollama RELOAD the model (measured 9-12 s,
# 25 s+ under memory pressure), so the rule is built not to flap:
#   * one background size for everything; a caller asking for LESS (debate 8192,
#     watch digests 2048) is raised to it rather than reloading down;
#   * a prompt that would not fit steps up a short ladder (16384, then the chat size).
#     Size is estimated from prompt length, deliberately on the high side, because a
#     window that is too small silently cuts the START of the prompt;
#   * after any call that needed a bigger window (a chat turn, or an oversized
#     prompt), background calls keep using that window for ctx_hold_seconds, so a
#     chat session is not interleaved with reload-down / reload-up pairs.
# ─────────────────────────────────────────────────────────────────────────────
_CHARS_PER_TOKEN = 2.8        # measured ~2.9 on headline/JSON-style prompts; err low
_CTX_HEADROOM = 256           # tokens of slack on top of prompt + output
_DEFAULT_OUT_TOKENS = 1024    # assumed output when the caller sets no num_predict

_ctx_lock = threading.Lock()
_ctx_hold = {"size": 0, "until": 0.0}     # a bigger window in use, and until when
_ctx_last_sent = {}                       # model -> last num_ctx sent (for the log line)


def _est_tokens(*texts) -> int:
    """Prompt-size estimate in tokens, erring high. Never raises."""
    n = dense = 0
    for t in texts:
        if t:
            s = t if isinstance(t, str) else str(t)
            n += len(s)
            # digits, punctuation and non-ASCII cost about one token per character
            dense += sum(1 for ch in s if ord(ch) > 127 or not (ch.isalpha() or ch.isspace()))
    return int(max(n / _CHARS_PER_TOKEN, dense + (n - dense) / 3.6)) + 1


def _ctx_sizes():
    """(background size, chat size, ascending steps incl. both). With
    background_num_ctx unset/0 the background size IS the chat size (feature off)."""
    c = _llm_cfg()["local"]
    chat = int(c.get("num_ctx") or _DEFAULT_LLM["local"]["num_ctx"])
    try:
        base = int(c.get("background_num_ctx") or 0)
    except (TypeError, ValueError):
        base = 0
    base = min(base, chat) if base > 0 else chat
    steps = {base, chat}
    for x in (c.get("ctx_ladder") or []):
        try:
            if base < int(x) < chat:
                steps.add(int(x))
        except (TypeError, ValueError):
            continue
    return base, chat, sorted(steps)


def _apply_ctx_hold(size, base, now=None):
    """Start/extend the hold when `size` is bigger than the background size; return
    the window to actually use (the held one, if a bigger window is still in use)."""
    now = time.time() if now is None else now
    try:
        hold = float(_llm_cfg()["local"].get("ctx_hold_seconds") or 0)
    except (TypeError, ValueError):
        hold = 0.0
    with _ctx_lock:
        active = now < _ctx_hold["until"]
        if size > base and hold > 0 and (not active or size >= _ctx_hold["size"]):
            _ctx_hold["size"], _ctx_hold["until"] = size, now + hold
            return size
        if active and _ctx_hold["size"] > size:
            return _ctx_hold["size"]          # ride the window that is already loaded
    return size


def pick_background_ctx(system, user, opts=None, *, now=None) -> int:
    """The num_ctx for one background (non-chat) call. See the block comment above."""
    opts = opts or {}
    base, chat, steps = _ctx_sizes()
    floor = base
    try:
        req = int(opts.get("num_ctx") or 0)
    except (TypeError, ValueError):
        req = 0
    if req > floor:
        floor = min(req, chat)                # a caller may ask for more, never for less
    try:
        out = int(opts.get("num_predict") or 0)
    except (TypeError, ValueError):
        out = 0
    if out <= 0:
        out = _DEFAULT_OUT_TOKENS
    need = _est_tokens(LOCAL_HOUSE_STYLE, flatten_system(system), user) + out + _CTX_HEADROOM
    size = next((s for s in steps if s >= max(floor, need)), chat)
    return _apply_ctx_hold(size, base, now)


def _background_opts(system, user, opts):
    """opts with num_ctx set for a background call. Sizing must never break a call."""
    try:
        return dict(opts, num_ctx=pick_background_ctx(system, user, opts))
    except Exception as e:  # pragma: no cover - defensive
        log.warning("context sizing failed (%s); using the caller's options", e)
        return opts


def _note_chat_ctx(num_ctx=None):
    """A chat turn is using the big window: start/extend the hold. num_ctx = the window the turn actually
    sent, when the caller set one (the small-prompt chat, 2026-11-17, runs at the background window and so
    starts no hold); None = the chat window, as before."""
    try:
        base, chat, _steps = _ctx_sizes()
        _apply_ctx_hold(int(num_ctx) if num_ctx else chat, base)
    except Exception:  # pragma: no cover - defensive
        pass


def _note_ctx_sent(model, num_ctx):
    """One log line whenever the window sent for a model CHANGES (= Ollama reloads it),
    so reloads can be counted with `journalctl -u mason | grep 'context window'`."""
    try:
        if num_ctx is None:
            return
        with _ctx_lock:
            prev = _ctx_last_sent.get(model)
            _ctx_last_sent[model] = num_ctx
        if prev is not None and prev != num_ctx:
            log.info("local model %s context window %s -> %s (Ollama reloads the model)",
                     model, prev, num_ctx)
    except Exception:  # pragma: no cover - defensive
        pass


class OpusProvider:
    """Thin wrapper over anthropic.Anthropic — the ONLY place `import anthropic`
    survives. Deep-analysis path only. PHASE 1: minimal; not exercised by default
    tests (Opus costs money). Streaming/caching parity is finished in Phase 4."""

    def __init__(self, model=None, effort=None, tier="opus", provider="opus"):
        # Chat model picker (2026-11-12): a manual pick passes its own Claude model
        # id + effort and the tier/provider labels the UI + cost ledger should show.
        # With no args this is exactly the old deep-tier Opus provider.
        self._client = None
        self._model_override = model
        self._effort = effort
        self._tier = tier
        self._provider = provider

    def _c(self):
        if self._client is None:
            import anthropic  # lazy — no anthropic dependency for the local path
            self._client = anthropic.Anthropic(api_key=_anthropic_key())
        return self._client

    def _model(self):
        return self._model_override or _llm_cfg()["deep"]["model"]

    def _one_shot(self, content, system, opts, what):
        """One non-interactive call. Returns (text, stop_reason, usage dict).
        Rules for claude-opus-5-5 (same ones complete_research follows): thinking
        cannot be switched off and it spends max_tokens, so (a) effort is always set
        explicitly and (b) max_tokens is never below llm.deep.min_max_tokens (16000) —
        a caller's smaller number was sized for the answer alone and would let thinking
        eat the whole budget and return no text. No forced tool_choice, no prefill.
        Streamed + get_final_message(), so a long answer cannot hit the SDK's
        non-streaming time limit. Token usage and $ cost are logged as a `model_call`
        activity row, the same record complete_research writes."""
        model = self._model()
        deep = _llm_cfg()["deep"]
        effort = opts.get("effort") or self._effort or deep.get("effort") or "medium"
        if effort not in CHAT_EFFORTS:
            effort = "medium"
        try:
            floor = int(deep.get("min_max_tokens") or 16000)
        except (TypeError, ValueError):
            floor = 16000
        max_tokens = max(int(opts.get("max_tokens") or 0), floor)
        kwargs = dict(model=model, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": content}],
                      output_config={"effort": effort})
        sys_txt = flatten_system(system)
        if sys_txt:
            kwargs["system"] = sys_txt
        with self._c().messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()
        text = "".join(getattr(b, "text", "") or "" for b in (resp.content or [])
                       if getattr(b, "type", "") == "text")
        stop = getattr(resp, "stop_reason", None)
        u = getattr(resp, "usage", None)
        usage = {k: int(getattr(u, k, 0) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens")}
        _log_model_call(model, effort, usage, stop, what=what, empty=not text.strip())
        return text, stop, usage

    def complete(self, system, user, **opts):
        """One-shot deep text. Raises DeepModelError if the model returned no text
        (it used to return "" silently). A reply cut off by max_tokens that still has
        text is returned as is, with a warning in the log."""
        text, stop, usage = self._one_shot(user, system, opts, "one-shot")
        if not text.strip():
            raise DeepModelError(
                f"{self._model()} returned no text (stop_reason={stop}, "
                f"output_tokens={usage.get('output_tokens', 0)})")
        if stop == "max_tokens":
            log.warning("deep one-shot reply was cut off by max_tokens (%s output tokens)",
                        usage.get("output_tokens", 0))
        return text

    def complete_vision(self, system, prompt, image_b64, media_type, **opts):
        """Analyze an image. Local models have NO vision, so this is Opus-only —
        a capability escalation (qwen literally can't see the pixels), not a
        deep-analysis choice. TODO(Nico) §11: you may prefer to disable image
        upload rather than spend Opus on it.

        Same budget/effort/usage-logging rules as complete(). The return contract is
        unchanged: an empty reply is returned as "" (the upload route has its own
        "could not analyze" fallback), with a warning in the log."""
        text, stop, usage = self._one_shot([
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ], system, opts, "vision")
        if not text.strip():
            log.warning("deep vision call returned no text (stop_reason=%s, output_tokens=%s)",
                        stop, usage.get("output_tokens", 0))
        return text

    def chat_stream(self, messages, system, tools=None, **opts):
        """Native Anthropic streaming, re-emitted in the provider-agnostic event shape
        so the Phase-4 brain loop consumes ONE stream regardless of provider."""
        model = self._model()
        yield {"type": "tier", "tier": self._tier, "model": model}
        kwargs = dict(
            model=model,
            max_tokens=opts.get("max_tokens", 4096),
            system=system,             # keep native Anthropic shape (cache blocks OK)
            messages=messages,
        )
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        if tools:
            kwargs["tools"] = tools
            # 2026-11-12: forced tool_choice ({"type":"any"}) is no longer sent.
            # Opus 5.5 / Sonnet 5.5 / Fable 5.2 reject it with a 400, so the old
            # force_tool toggle would have failed the continuation pass outright.
        # Claude thinks adaptively by default; effort is set explicitly above so a model
        # bump can't silently change latency/token behavior on the deep path.
        emitted_tool = False
        with self._c().messages.stream(**kwargs) as stream:
            for event in stream.text_stream:
                if event:
                    yield {"type": "text", "text": event}
            final = stream.get_final_message()
            raw_blocks, said_text = [], False
            for block in final.content:
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    emitted_tool = True
                    raw_blocks.append({"type": "tool_use", "id": block.id,
                                       "name": block.name, "input": block.input})
                    yield {"type": "tool_call", "name": block.name,
                           "input": block.input, "id": block.id}
                elif btype == "text":
                    said_text = said_text or bool(block.text)
                    raw_blocks.append({"type": "text", "text": block.text})
                elif btype in ("thinking", "redacted_thinking"):
                    # must go back to the API byte-for-byte on the next tool round
                    raw_blocks.append(block.model_dump(exclude_none=True))
        if getattr(final, "stop_reason", "") == "refusal" and not said_text:
            yield {"type": "text", "text": f"({model} declined to answer this request.)"}
        if emitted_tool:
            # The assistant turn exactly as the API returned it (thinking blocks
            # included). Claude models think on every turn and require those blocks
            # passed back unchanged between tool rounds, so the brain loop uses this
            # instead of rebuilding the turn from text + tool calls.
            yield {"type": "assistant_raw", "content": raw_blocks}
        # real usage -> the brain logs the $ cost of this turn.
        yield {"type": "usage", "provider": self._provider, "model": model,
               "usage": getattr(final, "usage", None)}
        yield {"type": "done", "stop_reason": "tool_use" if emitted_tool else "end"}


# ─────────────────────────────────────────────────────────────────────────────
# Availability ladder (spec §2.2 / §4.5) — the AVAILABILITY axis, kept strictly
# separate from the CAPABILITY axis (escalation to Opus). When a *local* call fails
# (Ollama down, GPU OOM, timeout, empty/malformed output) this ladder runs:
#     qwen  ->  phi4-mini (fast local substitute)  ->  escalating retry  ->  restart
# Opus is NEVER reached from here — that would cross the two axes.
# ─────────────────────────────────────────────────────────────────────────────

# Guard so tests (and any non-service run) can exercise the ladder WITHOUT killing
# the process. The live systemd service leaves this at "1"; the test harness sets
# MASON_LLM_ALLOW_RESTART=0.
_ALLOW_RESTART = os.environ.get("MASON_LLM_ALLOW_RESTART", "1") != "0"


def _retry_backoff():
    """The escalating retry schedule (seconds), from config (§4.6, Nico-tunable §11)."""
    return _llm_cfg().get("retry_backoff_seconds", _DEFAULT_LLM["retry_backoff_seconds"])


def _request_restart():
    """Terminal step of the availability ladder (§6.5). qwen AND phi have been dead
    through every retry (~33 min) — the safest recovery is a clean process exit so
    systemd (Restart=on-failure, verified on the box) relaunches Mason fresh. We
    exit non-zero on a short-delayed daemon thread so the critical log line flushes
    first. Guarded by MASON_LLM_ALLOW_RESTART so tests never nuke themselves.

    2026-11-13 (audit O3): callers only get here through _is_outage(), i.e. when
    EVERY workhorse attempt failed because the model server could not be reached or
    answered 5xx/408/429. A rejected request (4xx) or an empty answer never restarts
    the trading process. `llm.restart_on_local_outage: false` switches the exit off
    altogether; since 2026-11-15 it is off unless that setting is exactly true."""
    if not _ALLOW_RESTART:
        log.critical("restart requested but MASON_LLM_ALLOW_RESTART=0 — NOT exiting (test/dev mode)")
        return
    if _llm_cfg().get("restart_on_local_outage", False) is not True:
        log.critical("local model outage, but llm.restart_on_local_outage is false — NOT exiting")
        return
    def _boom():
        time.sleep(1.0)
        os._exit(1)   # hard exit: sys.exit() only unwinds the current thread
    threading.Thread(target=_boom, daemon=True).start()


def _err_kind(e) -> str:
    return getattr(e, "kind", None) or "unavailable"


def _is_outage(local_errors) -> bool:
    """True only when every WORKHORSE attempt failed for availability reasons (server
    unreachable, timeout, 5xx, 408, 429). That is the one condition the restart step
    was written for. One rejected request (4xx) or empty answer in the run means the
    server is up, so restarting Mason cannot help."""
    return bool(local_errors) and all(_err_kind(e) == "unavailable" for e in local_errors)


def _availability_complete(system, user, opts, *, retry=True, restart=True):
    """Availability ladder for a NON-streaming local call. qwen -> phi -> (optional
    escalating retry) -> (optional restart). Chore callers (voice/news/debate/
    arbiter) pass retry=False, restart=False: each is individually fail-safe to its
    own static fallback, so a chore must not block for 33 min or restart the service
    on its own — it fails fast and the caller degrades. The interactive brain path
    (streaming, below) gets the full ladder."""
    opts = _background_opts(system, user, opts)     # right-sized context window (O2)
    plan = [("local", 0), ("phi", 0)]
    if retry:
        plan += [("local", d) for d in _retry_backoff()]
    last_err = None
    local_errs = []
    tried = 0
    for i, (tier, delay) in enumerate(plan):
        if delay:
            time.sleep(delay)
        prov = _qwen() if tier == "local" else _phi()
        tried = i + 1
        try:
            return prov.complete(system, user, **opts)
        except LocalModelError as e:
            last_err = e
            if tier == "local":
                local_errs.append(e)
            record_activity({"type": "local_down", "stage": tier, "mode": "complete",
                             "attempt": i + 1, "err": str(e), "kind": _err_kind(e)})
            # The workhorse REJECTED the request (4xx): the same request will be
            # rejected again, so once phi has had its one try, stop. No backoff loop.
            if local_errs and _err_kind(local_errs[-1]) == "client" and i >= 1:
                break
            continue
    if restart and _is_outage(local_errs):
        record_activity({"type": "local_dead", "mode": "complete",
                         "attempts": tried, "action": "restart", "err": str(last_err)})
        log.critical("qwen+phi dead after %d attempts (complete) — restarting service", tried)
        _request_restart()
    elif restart:
        record_activity({"type": "local_dead", "mode": "complete", "attempts": tried,
                         "action": "none", "kind": _err_kind(last_err), "err": str(last_err)})
    err = LocalModelError(f"local models unavailable after {tried} attempt(s): {last_err}",
                          status=getattr(last_err, "status", None), kind=_err_kind(last_err))
    raise err


def _availability_stream(messages, system, tools, opts):
    """Availability ladder for the STREAMING brain (§4.5). Generator. Tries qwen;
    if it fails BEFORE emitting any content, records the outage and falls to phi
    once (fast substitute, tier badge flips to 'phi' — the UI degraded signal),
    then retries qwen on the backoff schedule, then restarts the service.

    A failure AFTER content has already streamed is re-raised rather than retried —
    you cannot cleanly restart a half-emitted turn, and the brain loop's own
    one-retry + never-empty-message net covers that rarer case."""
    plan = [("local", 0), ("phi", 0)] + [("local", d) for d in _retry_backoff()]
    last_err = None
    local_errs = []
    tried = 0
    for i, (tier, delay) in enumerate(plan):
        if delay:
            record_activity({"type": "local_retry", "stage": tier,
                             "attempt": i + 1, "delay_s": delay})
            time.sleep(delay)
        prov = _qwen() if tier == "local" else _phi()
        emitted = False
        tried = i + 1
        _note_chat_ctx(opts.get("num_ctx"))   # the chat window is in use: background calls ride it (O2)
        # Nico 2026-11-12: phi4-mini must never call tools, even as the fallback.
        _tools = tools if tier == "local" else None
        _system = system if (tier == "local" or not tools) else _with_no_tools_note(system)
        try:
            for ev in prov.chat_stream(messages, _system, tools=_tools, **opts):
                if ev["type"] in ("text", "tool_call"):
                    emitted = True
                yield ev
            return   # clean success
        except LocalModelError as e:
            last_err = e
            if emitted:
                raise   # can't retry a half-streamed turn
            if tier == "local":
                local_errs.append(e)
            record_activity({"type": "local_down", "stage": tier, "mode": "stream",
                             "attempt": i + 1, "err": str(e), "kind": _err_kind(e)})
            # The workhorse REJECTED the request (4xx, e.g. the 2026-10-26 chat 400):
            # the same request will be rejected again. phi (no tools) gets its one
            # try; after that, stop — no 33-minute backoff, and never a restart.
            if local_errs and _err_kind(local_errs[-1]) == "client" and i >= 1:
                break
            continue
        finally:
            _note_chat_ctx(opts.get("num_ctx"))
    if _is_outage(local_errs):
        record_activity({"type": "local_dead", "mode": "stream",
                         "attempts": tried, "action": "restart", "err": str(last_err)})
        log.critical("qwen+phi dead after %d attempts (stream) — restarting service", tried)
        _request_restart()
    else:
        # Not an outage (a rejected request or an empty answer): the turn fails, the
        # process stays up. The chat route already turns this error into a message.
        record_activity({"type": "local_dead", "mode": "stream", "attempts": tried,
                         "action": "none", "kind": _err_kind(last_err), "err": str(last_err)})
        log.error("local chat turn failed after %d attempt(s), not an outage (%s) — "
                  "NOT restarting: %s", tried, _err_kind(last_err), last_err)
    raise LocalModelError(f"local models unavailable after {tried} attempt(s): {last_err}",
                          status=getattr(last_err, "status", None), kind=_err_kind(last_err))


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Chat model picker (2026-11-12). The chat bar can pin ONE turn to a specific
# model instead of the default Auto route (qwen, Opus on deep escalation). Which
# models appear, and which of them may call tools, is `llm.chat_models` in
# mason.config.json — Nico edits that block; the list below only backstops a
# missing one. Config is read once at import, so a change needs a service restart.
# ─────────────────────────────────────────────────────────────────────────────
CHAT_EFFORTS = ("low", "medium", "high", "xhigh", "max")

_DEFAULT_CHAT_MODELS = [
    {"id": "qwen3.5:4b",        "label": "Qwen 3.5 4B", "provider": "ollama",    "tools": True},
    {"id": "phi4-mini",         "label": "Phi-4 mini",  "provider": "ollama",    "tools": False},
    {"id": "claude-haiku-5-5",  "label": "Haiku 5.5",   "provider": "anthropic", "tools": False},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5",  "provider": "anthropic", "tools": False},
    {"id": "claude-opus-5-5",   "label": "Opus 5.5",    "provider": "anthropic", "tools": True},
    {"id": "claude-fable-5-2",  "label": "Fable 5.2",   "provider": "anthropic", "tools": True},
]

_NO_TOOLS_NOTE = (
    "TOOLS ARE DISABLED FOR THIS TURN. You cannot fetch quotes, news or account "
    "data, and you cannot place, change or cancel anything. Answer only from this "
    "conversation and your own knowledge. Never claim or promise to run a tool; if "
    "the answer needs live data or an action, say so plainly.")


def _with_no_tools_note(system):
    """Append the tools-off notice to a system prompt in either shape (str or the
    Anthropic block list). Appended AFTER the cached blocks so the cache survives."""
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": _NO_TOOLS_NOTE}]
    return ((system or "") + "\n\n" + _NO_TOOLS_NOTE).strip()


def chat_models():
    """Normalized picker list: [{id, label, provider, tools, effort}]. `tools` is
    the per-model permission from config. phi is hard-locked to False here, so no
    config value can ever hand phi4-mini a tool. `effort` = the model takes an
    effort level (Claude only; qwen must keep think:false)."""
    raw = (_llm_cfg().get("chat_models") or {}).get("models") or _DEFAULT_CHAT_MODELS
    out, seen = [], set()
    for m in raw:
        if not isinstance(m, dict) or not m.get("id") or m["id"] in seen:
            continue
        mid = str(m["id"])
        seen.add(mid)
        provider = m.get("provider") or ("anthropic" if mid.startswith("claude-") else "ollama")
        out.append({
            "id": mid, "label": str(m.get("label") or mid), "provider": provider,
            "tools": bool(m.get("tools")) and not mid.lower().startswith("phi"),
            "effort": provider == "anthropic",
        })
    return out


def resolve_chat_model(model_id):
    """The picker entry for a model id, or None for Auto / unknown ids."""
    if not model_id or model_id == "auto":
        return None
    return next((m for m in chat_models() if m["id"] == model_id), None)


def _picked_stream(entry, effort, tools_on, messages, system, tools, opts):
    """One streamed round on a manually picked model. No Opus escalation (the pick
    IS the routing decision). Tools go to the model only when its config entry
    allows them AND the chat-bar switch is on."""
    use_tools = tools if (tools and entry["tools"] and tools_on) else None
    if tools and use_tools is None:
        system = _with_no_tools_note(system)
    record_activity({"type": "route", "intent": "brain", "provider": entry["provider"],
                     "mode": "stream", "reason": "manual-pick", "model": entry["id"],
                     "effort": effort, "tools": bool(use_tools)})
    if entry["provider"] == "anthropic":
        opts = dict(opts)
        # Claude thinks on every turn and thinking spends max_tokens; the brain
        # loop's 2048/4096 caps were sized for qwen and would truncate the answer.
        opts["max_tokens"] = max(int(opts.get("max_tokens") or 0), 16000)
        yield from OpusProvider(model=entry["id"], effort=effort, tier="pick",
                                provider="claude").chat_stream(
                                    messages, system, tools=use_tools, **opts)
        return
    c = _llm_cfg()["local"]
    mark_interactive_begin()      # background desk yields the GPU, as on Auto
    try:
        if entry["id"] == c.get("workhorse"):
            # qwen keeps its availability ladder (phi/retry) but reports as a pick
            for ev in _availability_stream(messages, system, use_tools, opts):
                if ev["type"] == "tier" and ev.get("tier") == "local":
                    ev = dict(ev, tier="pick")
                yield ev
            return
        prov = _OllamaProvider(entry["id"], c["base_url"], c["num_ctx"],
                               c["temperature"], c.get("think", False), tier="pick")
        yield from prov.chat_stream(messages, system, tools=use_tools, **opts)
    finally:
        mark_interactive_end()


def should_escalate_to_opus(query_text, qwen_signal=None):
    """Capability-axis decision (spec §4.4). TODO(Nico) §11: final policy dial.

    Fires on exactly two triggers, and NOTHING else:
      1. qwen self-escalation — qwen_signal is truthy (it emitted ESCALATE_SENTINEL
         or an escalate tool call). This is the PRIMARY path Nico wants.
      2. Explicit ask — Nico's text matches the deep-analysis keywords.

    HARD RULE (Nico, 2026-09-05): Opus is for DEEP ANALYSIS ONLY. It is never a
    route for tool calling, mutations, confirmations, or "qwen might not do it
    right" — those stay local. Reliability of tool execution is solved locally
    (num_ctx so the tools fit qwen's context) and deterministically (the
    pre-model command handler in mason_app), NOT by spending Opus.

    Availability failures NEVER come here — they go down the phi/retry ladder.
    Keep ALL escalation logic in this one function so Nico can retune in one place.
    """
    if qwen_signal:
        return True
    if query_text and _OPUS_DEEP_RE.search(query_text):
        return True
    return False


def record_activity(event):
    """Single activity sink (spec §4.5 / §11). Everything lands in the server/service
    log AND, now, in the durable Activity Ledger (mason_activity.activity table) as a
    category='plumbing' row, so gateway internals share one timeline with the desk's
    analyst activity. Import is lazy + wrapped so a logging failure can never break a
    gateway call."""
    try:
        log.info("ACTIVITY %s", json.dumps(event, default=str))
    except Exception:
        log.info("ACTIVITY %r", event)
    try:
        import mason_activity
        mason_activity.record_plumbing(event)
    except Exception:
        pass
    # Also fold gateway routing/retry/escalation/truncation events into the turn
    # trace so they line up (by turn_id) with the prompts and tool calls.
    try:
        ev = dict(event) if isinstance(event, dict) else {"event": event}
        _trace.emit("activity", **ev)
    except Exception:
        pass


# ── interactive-busy gate (Analyst Desk Phase 2, §3.E) ───────────────────────
# One RTX 2060 with OLLAMA_NUM_PARALLEL=1 serves ONE qwen call at a time, so a
# background desk call in flight delays the user's chat. These let the background
# desk worker yield the GPU to the interactive stream: chat_stream marks begin/end
# around its LOCAL (qwen) branch, and the desk calls interactive_busy() before
# every dispatch. Purely additive bookkeeping — no existing behaviour or return
# shape changes, and mark_* are cheap timestamp writes that add no stream latency.
_interactive_lock = threading.Lock()
_interactive_active = 0            # count of in-flight interactive local streams
_interactive_last_end = 0.0       # epoch of the most recent interactive stream end


def mark_interactive_begin():
    """Record that an interactive local (qwen) stream just started."""
    global _interactive_active
    with _interactive_lock:
        _interactive_active += 1


def mark_interactive_end():
    """Record that an interactive local (qwen) stream just ended."""
    global _interactive_active, _interactive_last_end
    with _interactive_lock:
        if _interactive_active > 0:
            _interactive_active -= 1
        _interactive_last_end = time.time()


def interactive_busy(within_seconds: float = 20) -> bool:
    """True if an interactive local stream is running now, or finished within the
    trailing `within_seconds` window (a cooldown so the desk doesn't grab the GPU
    the instant the user's reply finishes, mid-follow-up-typing)."""
    with _interactive_lock:
        if _interactive_active > 0:
            return True
        return (time.time() - _interactive_last_end) < within_seconds


def complete_vision(system, prompt, image_b64, media_type, **opts) -> str:
    """Single-shot image analysis. Local (qwen) has no vision — this routes to
    Opus as a capability necessity (§4, TODO(Nico) §11). Callers: the file-upload
    analyzer for PNG/JPG. Text/CSV uploads use complete(intent='chore')."""
    record_activity({"type": "route", "intent": "vision", "provider": "opus",
                     "reason": "local has no vision"})
    return OpusProvider().complete_vision(system, prompt, image_b64, media_type, **opts)


# ── Catalyst Desk: Opus research call (director v2, 2026-09-25) ─────────────────
# A dedicated, cost-accounted path for the Opus DIRECTOR — separate from the chat
# deep tier (llm.deep.model, still Opus 5.5) so the director can run claude-opus-5-5
# without touching the interactive tool loop. Opus 5.5 rules honored here:
# thinking can't be disabled (effort is the control; default medium, set
# explicitly), no forced tool_choice, no prefill, read blocks by type. Server-side
# web_search/web_fetch are optional; a pause_turn is continued by re-sending the
# assistant content. Returns text + usage + a real $ cost so the caller can enforce
# its budget. Never hit by tests (they inject a fake).

# $/MTok: (input, cache_write_5m, cache_write_1h, cache_read, output)
_RESEARCH_PRICING = {
    "claude-opus-5-5": (4.00, 5.00, 8.00, 0.20, 20.00),
    # Desk checks (2026-11-14, Nico's ruling D1): the cheaper model for the short checks
    # between the two scheduled calls.
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.10, 10.00),
}


def research_cost_usd(model, usage, *, web_search_requests=0, search_cost_usd=0.01) -> float:
    """$ for one research call from its accumulated usage dict. Unknown model →
    priced at the Opus 5.5 rate (conservative), never 0."""
    rin, rw5, _rw1h, rread, rout = _RESEARCH_PRICING.get(model, _RESEARCH_PRICING["claude-opus-5-5"])
    u = usage or {}
    cost = (u.get("input_tokens", 0) * rin
            + u.get("cache_creation_input_tokens", 0) * rw5
            + u.get("cache_read_input_tokens", 0) * rread
            + u.get("output_tokens", 0) * rout) / 1_000_000.0
    return round(cost + web_search_requests * float(search_cost_usd), 6)


def _log_model_call(model, effort, usage, stop, *, what="one-shot", empty=False):
    """Activity v2 `model_call` row for a paid one-shot call, with its $ cost — the
    same record complete_research writes for the director. Never raises."""
    try:
        cost = research_cost_usd(model, usage)
        log.info("deep %s call: model=%s effort=%s stop=%s in=%s out=%s cost=$%.4f%s",
                 what, model, effort, stop, usage.get("input_tokens", 0),
                 usage.get("output_tokens", 0), cost, " EMPTY-TEXT" if empty else "")
        import mason_activity as _ACT
        _ACT.log_activity("model_call",
                          f"Opus call ({model}, effort {effort}, {what}) · ${cost:.3f}"
                          + (" · returned no text" if empty else ""),
                          actor="opus", tier="work", status="error" if empty else None,
                          extra={"model": model, "usage": usage, "cost_usd": cost,
                                 "stop": stop, "kind": what, "effort": effort})
    except Exception:
        pass


def complete_research(system, user, *, model="claude-opus-5-5", effort="medium",
                      max_tokens=16000, web=None, max_continuations=3,
                      client_tools=None, tool_handler=None, max_tool_calls=4,
                      deadline_s=None) -> dict:
    """One director research call. `web` = None (no tools) or
    {"max_searches": int, "max_fetches": int, "blocked_domains": [...] |
     "allowed_domains": [...], "search_cost_usd": float}.
    client_tools/tool_handler (2026-09-28, whole-DB memory search): Anthropic
    client-tool schemas Opus may call mid-analysis; tool_handler(name, input) -> str
    runs them locally. At most max_tool_calls executions; past that each call gets
    a "budget exhausted — finish now" result. The assistant turn (incl. Opus 5.5
    thinking blocks) is appended UNCHANGED before the tool_result user turn
    (append-only history), and top-level cache_control caches the growing prefix
    so every extra round re-reads it at the cache rate.
    Returns {"text", "usage", "cost_usd", "web_search_requests", "web_fetch_requests",
             "citations", "stop_reason", "model", "tool_calls"}. Raises on API errors
    (caller catches and degrades)."""
    if os.environ.get("MASON_NO_BILLED_CALLS") == "1":   # test harness hard stop
        raise RuntimeError("billed Opus call blocked (MASON_NO_BILLED_CALLS=1)")
    import anthropic  # lazy, like OpusProvider
    # deadline_s (2026-11-14): a hard limit on the whole call, for runs that share a worker
    # with the exit manager (a decision asked for between the scheduled reviews). No SDK
    # retries, a bounded wait between chunks, and the stream is abandoned once the limit
    # passes. None (the default, used by the scheduled reviews) = behaviour unchanged.
    _t_end = (time.time() + float(deadline_s)) if deadline_s else None
    if _t_end:
        client = anthropic.Anthropic(api_key=_anthropic_key(), max_retries=0,
                                     timeout=max(10.0, min(float(deadline_s), 120.0)))
    else:
        client = anthropic.Anthropic(api_key=_anthropic_key())
    tools = []
    if web:
        dom = {}
        if web.get("allowed_domains"):
            dom["allowed_domains"] = list(web["allowed_domains"])
        elif web.get("blocked_domains"):
            dom["blocked_domains"] = list(web["blocked_domains"])
        if int(web.get("max_searches", 0) or 0) > 0:
            tools.append({"type": "web_search_20260209", "name": "web_search",
                          "max_uses": int(web["max_searches"]), **dom})
        if int(web.get("max_fetches", 0) or 0) > 0:
            tools.append({"type": "web_fetch_20260209", "name": "web_fetch",
                          "max_uses": int(web["max_fetches"]), **dom})
    # stable system block first + cached (anchor/event system prompts are frozen text)
    sys_blocks = [{"type": "text", "text": flatten_system(system),
                   "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": user}]
    agg = {"input_tokens": 0, "output_tokens": 0,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    n_search = n_fetch = 0
    texts, citations, tool_errors = [], [], []
    stop = None
    ctools = list(client_tools or []) if tool_handler else []
    tool_calls = []                               # [{name, input, chars}]
    rounds = max(1, int(max_continuations) + 1) + (int(max_tool_calls) + 1 if ctools else 0)
    for _ in range(rounds):
        kwargs = dict(model=model, max_tokens=int(max_tokens), system=sys_blocks,
                      messages=messages, output_config={"effort": effort})
        if tools or ctools:
            kwargs["tools"] = tools + ctools
        if ctools:
            kwargs["cache_control"] = {"type": "ephemeral"}   # cache the growing tool-loop prefix
        if _t_end and time.time() > _t_end:
            raise TimeoutError(f"model call passed its {float(deadline_s):.0f}s limit")
        with client.messages.stream(**kwargs) as stream:
            if _t_end:
                for _ev in stream:                # drain; give up once the limit passes
                    if time.time() > _t_end:
                        raise TimeoutError(f"model call passed its {float(deadline_s):.0f}s limit")
            resp = stream.get_final_message()
        u = getattr(resp, "usage", None)
        for k in agg:
            agg[k] += int(getattr(u, k, 0) or 0)
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            n_search += int(getattr(stu, "web_search_requests", 0) or 0)
            n_fetch += int(getattr(stu, "web_fetch_requests", 0) or 0)
        for b in resp.content:
            bt = getattr(b, "type", "")
            if bt in ("web_search_tool_result", "web_fetch_tool_result"):
                c = getattr(b, "content", None)
                code = getattr(c, "error_code", None) if c is not None and not isinstance(c, list) else None
                if code:                                   # server-tool errors return 200 + an error block
                    tool_errors.append({"tool": bt.replace("_tool_result", ""), "error_code": str(code)})
            if bt == "text":
                texts.append(b.text)
                for c in (getattr(b, "citations", None) or []):
                    url = getattr(c, "url", None)
                    if url:
                        citations.append({"url": url, "title": getattr(c, "title", None)})
        stop = resp.stop_reason
        if stop == "tool_use" and ctools:
            names = {t.get("name") for t in ctools}
            results = []
            for b in resp.content:
                if getattr(b, "type", "") != "tool_use" or getattr(b, "name", "") not in names:
                    continue
                if len(tool_calls) >= int(max_tool_calls):
                    out = ("Tool budget for this run is exhausted. Finish the brief and the "
                           "final JSON now with what you have.")
                else:
                    try:
                        out = str(tool_handler(b.name, dict(b.input or {})))
                    except Exception as e:
                        out = f"tool error: {e}"
                    tool_calls.append({"name": b.name, "input": dict(b.input or {}), "chars": len(out)})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:12000]})
            if not results:
                break
            texts = []                              # the brief is the text AFTER the last tool round
            messages = messages + [{"role": "assistant", "content": resp.content},
                                   {"role": "user", "content": results}]
            continue
        if stop != "pause_turn":
            break
        # server tool loop paused — continue the SAME turn (append-only history)
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if not citations:                  # fall back to URLs the model wrote in its text
        for url in re.findall(r"https?://[^\s)\]\"'<>]+", "".join(texts)):
            if url not in {c["url"] for c in citations}:
                citations.append({"url": url.rstrip(".,;"), "title": None})
    search_cost = float((web or {}).get("search_cost_usd", 0.01) or 0.01)
    record_activity({"type": "route", "intent": "director_research", "provider": "opus",
                     "model": model, "effort": effort, "web_searches": n_search,
                     "client_tool_calls": len(tool_calls)})
    try:                                   # Activity v2: every paid model call, with its cost
        import mason_activity as _ACT
        _c = research_cost_usd(model, agg, web_search_requests=n_search,
                               search_cost_usd=float((web or {}).get("search_cost_usd", 0.01) or 0.01))
        _ACT.log_activity("model_call", f"Opus call ({model}, effort {effort}) · ${_c:.3f}"
                          + (f" · {n_search} web search" if n_search else "")
                          + (f" · {len(tool_calls)} memory tool call(s)" if tool_calls else ""),
                          actor="opus", tier="work",
                          extra={"model": model, "usage": agg, "cost_usd": _c, "web_searches": n_search,
                                 "tool_calls": [t.get("name") for t in tool_calls], "stop": stop})
    except Exception:
        pass
    return {"text": "".join(texts), "usage": agg, "model": model, "stop_reason": stop,
            "web_search_requests": n_search, "web_fetch_requests": n_fetch,
            "citations": citations[:40], "tool_errors": tool_errors, "tool_calls": tool_calls,
            "cost_usd": research_cost_usd(model, agg, web_search_requests=n_search,
                                          search_cost_usd=search_cost)}


# ── Desk check: one short decision call on the cheaper Claude model (2026-11-14) ──
# Used by mason_desk_check between the two scheduled Opus calls. Rules for
# claude-sonnet-5-5 honored here: thinking cannot be switched off with "disabled"
# (it is left adaptive and kept short with effort, default "low"), no forced
# tool_choice, no prefill, no sampling parameters, blocks are read by type and
# stop_reason is checked before the text is used. The answer shape is enforced by
# the API (output_config.format = a JSON schema), so the caller gets valid JSON or
# an error, never free text. No server-side fallback model is requested: Nico chose
# the models, and a refusal simply means "no decision". No web tools, no history.
# Prompt caching is off unless the caller says a second call with the same system
# text follows within five minutes (cache_system=True); a cache that cannot be read
# back only adds the write surcharge.

class CheckModelError(Exception):
    """A desk-check model call that failed. `kind` is one of bad_request | auth |
    rate_limit | server | timeout | network | refusal | blocked | unpriced."""

    def __init__(self, kind, message=""):
        super().__init__(f"{kind}: {message}" if message else str(kind))
        self.kind = kind


def complete_check(system, user, *, model="claude-sonnet-5-5", effort="low", max_tokens=4000,
                   schema=None, timeout_s=45.0, cache_system=False) -> dict:
    """One short, cost-accounted decision call. Returns {"text", "usage", "cost_usd",
    "model", "stop_reason", "seconds"}. Raises CheckModelError (never a raw SDK error)
    so the caller can log one plain reason and do nothing."""
    if os.environ.get("MASON_NO_BILLED_CALLS") == "1":   # test harness hard stop
        raise CheckModelError("blocked", "billed model call blocked (MASON_NO_BILLED_CALLS=1)")
    if model not in _RESEARCH_PRICING:
        # an unpriced model would be booked at the wrong rate and could be a model Nico did not choose
        raise CheckModelError("unpriced", f"no price row for model {model!r}; call refused")
    if effort not in CHAT_EFFORTS:
        effort = "low"
    import anthropic  # lazy, like OpusProvider
    client = anthropic.Anthropic(api_key=_anthropic_key())
    sys_block = {"type": "text", "text": flatten_system(system)}
    if cache_system:
        sys_block["cache_control"] = {"type": "ephemeral"}
    out_cfg = {"effort": effort}
    if schema:
        out_cfg["format"] = {"type": "json_schema", "schema": schema}
    kwargs = dict(model=model, max_tokens=int(max_tokens), system=[sys_block],
                  messages=[{"role": "user", "content": user}], output_config=out_cfg)
    t0 = time.time()
    try:
        # no SDK retry: the caller runs on the desk worker, which also runs the exit manager, so one
        # check may hold it for timeout_s at most. A failed check is tried once more on the next pass.
        resp = client.with_options(timeout=float(timeout_s), max_retries=0).messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        raise CheckModelError("bad_request", str(getattr(e, "message", e))[:300])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise CheckModelError("auth", str(getattr(e, "message", e))[:200])
    except anthropic.RateLimitError as e:
        raise CheckModelError("rate_limit", str(getattr(e, "message", e))[:200])
    except anthropic.APIStatusError as e:
        raise CheckModelError("server" if getattr(e, "status_code", 0) >= 500 else "bad_request",
                              f"HTTP {getattr(e, 'status_code', '?')}: {str(getattr(e, 'message', e))[:200]}")
    except anthropic.APITimeoutError:
        raise CheckModelError("timeout", f"no answer within {timeout_s:.0f}s")
    except anthropic.APIConnectionError as e:
        raise CheckModelError("network", str(e)[:200])
    secs = round(time.time() - t0, 2)
    u = getattr(resp, "usage", None)
    usage = {k: int(getattr(u, k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")}
    cost = research_cost_usd(model, usage)
    stop = getattr(resp, "stop_reason", None)
    text = "".join(getattr(b, "text", "") or "" for b in (resp.content or [])
                   if getattr(b, "type", "") == "text")
    try:                                   # Activity v2: every paid model call, with its cost
        import mason_activity as _ACT
        _ACT.log_activity("model_call", f"Desk check call ({model}, effort {effort}) · ${cost:.4f}",
                          actor="sonnet", tier="work",
                          extra={"model": model, "usage": usage, "cost_usd": cost, "stop": stop,
                                 "kind": "desk_check", "effort": effort, "seconds": secs})
    except Exception:
        pass
    return {"text": text, "usage": usage, "cost_usd": cost, "model": getattr(resp, "model", model),
            "stop_reason": stop, "seconds": secs}


def complete(system, user, *, intent="chore", allow_escalation=False, **opts):
    """Non-streaming single-shot text (voice, news, debate, arbiter, book/track).

    intent "deep"  -> Opus.
    intent "chore"/"brain" -> qwen, unless allow_escalation and the text asks for depth.
    PHASE 1: qwen path fully wired; Opus path minimal.
    """
    if intent == "deep" or (allow_escalation and should_escalate_to_opus(user)):
        record_activity({"type": "route", "intent": intent, "provider": "opus", "mode": "complete"})
        return OpusProvider().complete(system, user, **opts)
    # local (qwen) via the availability ladder. Chore callers are individually
    # fail-safe, so this variant fails fast (qwen -> phi -> raise): no 33-min retry
    # loop, no self-restart from a background chore. The interactive brain path
    # (chat_stream) gets the FULL ladder including retry + restart.
    return _availability_complete(system, user, opts, retry=False, restart=False)


def chat_stream(messages, system, tools=None, *, intent="brain",
                allow_escalation=True, query_text="", model=None, effort=None,
                tools_on=True, **opts):
    """Provider-agnostic streaming generator for the interactive brain loop.

    Default: qwen (tier "local"). Opus on capability escalation (explicit ask now;
    qwen mid-turn self-escalation is wired in Phase 4). Yields the §4.3 event shape.
    PHASE 1: local path fully wired + streamed; the pre-turn Opus branch works but is
    not exercised by default tests.
    """
    picked = resolve_chat_model(model)
    if picked is not None:
        # Manual pick from the chat bar — bypasses Auto routing entirely.
        yield from _picked_stream(picked, effort if effort in CHAT_EFFORTS else None,
                                  tools_on, messages, system, tools, opts)
        return
    if intent == "deep" or (allow_escalation and should_escalate_to_opus(query_text)):
        record_activity({"type": "route", "intent": intent, "provider": "opus",
                         "mode": "stream", "reason": "explicit-deep-ask"})
        yield from OpusProvider().chat_stream(messages, system, tools=tools, **opts)
        return
    # local, through the availability ladder (qwen -> phi -> retry -> restart),
    # watching for the mid-turn self-escalation sentinel (§4.4). The two axes stay
    # separate: the ladder never reaches Opus; the sentinel never reaches phi/retry.
    #
    # Mark the interactive-busy gate (Analyst Desk P2, §3.E) around the local
    # stream so the background desk worker yields the GPU while the user is
    # chatting. The finally fires when the generator is exhausted or closed. No
    # change to the yielded event shape; mark_* are cheap timestamp writes.
    mark_interactive_begin()
    try:
        if not allow_escalation:
            yield from _availability_stream(messages, system, tools, opts)
            return
        yield from _local_stream_with_escalation(messages, system, tools, opts)
    finally:
        mark_interactive_end()


def _local_stream_with_escalation(messages, system, tools, opts):
    """Stream the local brain (through the availability ladder — qwen, or phi/retry
    if qwen is down), but if it emits the ESCALATE_SENTINEL (its way of saying "this
    turn needs deeper reasoning than I can give"), discard that output and re-run the
    SAME turn on Opus, streaming Opus's reply instead (§4.4).

    The two axes compose here without crossing: _availability_stream handles WHICH
    local model answers (qwen vs phi), and this wrapper handles WHETHER to hand the
    turn up to Opus. qwen is instructed to emit ONLY the sentinel when it wants to
    escalate, so it leads the content; we buffer just the first few tokens — enough
    to rule the sentinel in or out — then either escalate or flush and stream on.
    Latency cost: a handful of tokens on turns that don't escalate.
    """
    # Seeded with the configured workhorse tag; the real tier event from the
    # provider (which also carries "model") overwrites this below.
    pending_tier = {"type": "tier", "tier": "local",
                    "model": _llm_cfg()["local"].get("workhorse")}   # updated if the ladder flips to phi
    buffer = ""
    decided = False   # once True: committed to local, straight passthrough

    for ev in _availability_stream(messages, system, tools, opts):
        if decided:
            yield ev
            continue
        t = ev["type"]
        if t == "tier":
            pending_tier = ev            # hold — the ladder may flip local->phi; a
            continue                     # sentinel would override it entirely
        if t == "text":
            buffer += ev["text"]
            if ESCALATE_SENTINEL in buffer:
                record_activity({"type": "escalate", "stage": "mid_turn",
                                 "reason": "qwen emitted self-escalation sentinel"})
                yield {"type": "tier", "tier": "opus",
                       "model": _llm_cfg()["deep"].get("model")}
                yield from OpusProvider().chat_stream(messages, system, tools=tools, **opts)
                return
            if len(buffer.strip()) < len(ESCALATE_SENTINEL):
                continue                 # not enough yet to rule the sentinel out
            decided = True
            yield pending_tier
            yield {"type": "text", "text": buffer}
            buffer = ""
            continue
        # first non-text event (tool_call / usage / done) before any text settled it
        decided = True
        yield pending_tier
        if buffer:
            yield {"type": "text", "text": buffer}
            buffer = ""
        yield ev

    # generator exhausted while still buffering (reply shorter than the sentinel)
    if not decided:
        yield pending_tier
        if buffer:
            yield {"type": "text", "text": buffer}


# Convenience for Phase 5 / tests: expose provider factories.
def _provider(tier):
    return {"local": _qwen, "phi": _phi}[tier]()
