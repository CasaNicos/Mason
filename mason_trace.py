"""mason_trace.py — flag-gated, turn-correlated structured tracing for full-workflow
debugging of the Mason brain.

WHY: the normal logs only showed sparse lines ("tools called: get_quote") — you
could not see the actual prompt sent to qwen, the model's raw response, tool-call
ARGUMENTS, tool RESULTS, which tier/model served each round, forced-/v1 passes, or
the fabrication guard firing. When qwen fabricated a delete of a hallucinated id
(f9e8d1b2, 2026-09-08) the reasoning was invisible. This makes the whole turn
reconstructable.

WHAT: every seam emits one JSON line to mason_trace.jsonl (rotating) AND a compact
one-liner to the normal logger (journalctl), all tagged with a per-turn `turn_id`
so a whole turn — prompt -> model response -> tool call + args + result -> ... ->
final reply / fabrication guard — can be replayed in order.

COST: OFF => a single bool check per emit (no formatting, no I/O). ON => full prompts
(~26k chars) are written every turn, so it is a debugging file, not the hot path.
Toggle with env MASON_TRACE=1|0 (wins) or config llm.trace (bool). The trace file
contains account/journal data + the market snapshot — it is local-only, never shipped.
"""

import contextvars
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("mason.trace")

TRACE_FILE = Path(__file__).parent / "mason_trace.jsonl"
_MAX_BYTES = 25 * 1024 * 1024   # rotate to .jsonl.1 past 25 MB
_FIELD_CAP = 80000              # per-field char cap in the FILE (prompts are big)
_SUMMARY_CAP = 280             # DEFAULT per-field cap for the journalctl mirror;
                               # override via config llm.trace_log_cap / env
                               # MASON_TRACE_LOG_CAP (0 = full). See _log_cap().

_lock = threading.Lock()
_turn_id = contextvars.ContextVar("mason_turn_id", default=None)

_forced = None          # set_enabled() override (tests / runtime)
_cfg_trace = None       # cached config llm.trace
_UNSET = "___unset___"
_cfg_log_cap = _UNSET   # cached config llm.trace_log_cap


def _config_trace():
    global _cfg_trace
    if _cfg_trace is None:
        try:
            cfg = json.loads((Path(__file__).parent / "mason.config.json").read_text())
            _cfg_trace = bool(cfg.get("llm", {}).get("trace", False))
        except Exception:
            _cfg_trace = False
    return _cfg_trace


def _config_log_cap():
    global _cfg_log_cap
    if _cfg_log_cap is _UNSET:
        try:
            cfg = json.loads((Path(__file__).parent / "mason.config.json").read_text())
            _cfg_log_cap = cfg.get("llm", {}).get("trace_log_cap")
        except Exception:
            _cfg_log_cap = None
    return _cfg_log_cap


def _log_cap() -> int:
    """Per-field char cap for the journalctl mirror line (NOT the .jsonl file,
    which is always capped only by _FIELD_CAP). Env MASON_TRACE_LOG_CAP wins live,
    else config llm.trace_log_cap, else the compact default. 0 (or negative) means
    'full' — mirror the whole record to journalctl (bounded only by _FIELD_CAP) so
    a turn's prompts/responses/tool-results can be debugged from the service log
    alone, not only from mason_trace.jsonl."""
    raw = os.environ.get("MASON_TRACE_LOG_CAP")
    explicit = not (raw is None or raw == "")
    if not explicit:
        raw = _config_log_cap()
    if raw is None or raw == "":
        return _SUMMARY_CAP
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _SUMMARY_CAP
    if n <= 0:
        # Audit O8 (2026-11-15): the config's "full" (0) mirrors whole prompts and memory into the
        # system journal; honour it only while the service logs at DEBUG (mason_logsetup: config
        # logging.level / env MASON_LOG_LEVEL). An explicit env MASON_TRACE_LOG_CAP=0 still wins.
        if not explicit and not log.isEnabledFor(logging.DEBUG):
            return _SUMMARY_CAP
        return _FIELD_CAP
    return n


def enabled() -> bool:
    """Env MASON_TRACE wins (live, no restart of the flag needed); else config."""
    if _forced is not None:
        return _forced
    env = os.environ.get("MASON_TRACE")
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    return _config_trace()


def set_enabled(val):
    """Force tracing on/off (tests, or a runtime toggle). None => defer to env/config."""
    global _forced
    _forced = None if val is None else bool(val)


# ── per-turn correlation id ──────────────────────────────────────────────────
def new_turn(kind: str = "turn") -> str:
    tid = f"{kind}-{uuid.uuid4().hex[:12]}"
    _turn_id.set(tid)
    return tid


def set_turn(tid):
    _turn_id.set(tid)


def current_turn():
    return _turn_id.get()


def _cap(v, n):
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"…[+{len(v) - n} chars]"
    return v


def _summarize(fields: dict, cap: int) -> dict:
    out = {}
    for k, v in fields.items():
        if isinstance(v, str):
            out[k] = _cap(v, cap)
        elif isinstance(v, (list, dict)):
            out[k] = _cap(json.dumps(v, default=str), cap)
        else:
            out[k] = v
    return out


def _rotate_if_needed():
    try:
        if TRACE_FILE.exists() and TRACE_FILE.stat().st_size > _MAX_BYTES:
            bak = TRACE_FILE.with_name(TRACE_FILE.name + ".1")
            if bak.exists():
                bak.unlink()
            TRACE_FILE.rename(bak)
    except Exception:
        pass


def emit(phase: str, **fields):
    """Record one trace event for the current turn. Fast no-op when disabled.

    phase is a short verb ("turn_start","llm_request","llm_response","tool_exec",
    "fabrication_guard","turn_end","activity",...). Any extra kwargs are recorded
    verbatim (strings capped for the file, summarized for journalctl)."""
    if not enabled():
        return
    rec = {"ts": datetime.now().isoformat(timespec="milliseconds"),
           "turn_id": _turn_id.get(), "phase": phase}
    for k, v in fields.items():
        rec[k] = _cap(v, _FIELD_CAP) if isinstance(v, str) else v
    try:
        line = json.dumps(rec, default=str)
    except Exception:
        line = json.dumps({"ts": rec["ts"], "turn_id": rec["turn_id"], "phase": phase,
                           "_repr": _cap(repr(fields), _FIELD_CAP)})
    with _lock:
        _rotate_if_needed()
        try:
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # pragma: no cover
            log.warning("trace write failed: %s", e)
    try:
        log.info("TRACE %s %s", phase, json.dumps(_summarize(fields, _log_cap()), default=str))
    except Exception:
        pass
