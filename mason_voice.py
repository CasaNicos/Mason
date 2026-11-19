"""
mason_voice.py — Dynamic Ordis-voice line generator for Mason.

Replaces Mason's hardcoded greeting / prompt / prefix / spoken-summary strings
(the `random.choice` static arrays in mason_app.py) with live, context-aware
generations that stay in the Ordis (Warframe Cephalon) character.

Model policy (LLM rewire, 2026-08):
  • Local qwen    — all flavor text routes through the mason_llm gateway at
    intent="chore", which serves the local qwen3.5:4b model. No Anthropic key,
    no cloud call, no per-token cost. There is no key to gate on any more, so
    the voice layer ALWAYS attempts generation via the gateway.
  • "Smart hybrid"  — AI for the spoken brief recap; cached rotation for the small
    flavor prefixes. The startup greeting's FACTS are deterministic (mason_app);
    qwen only writes a fact-free opener (validated; static fallback).
  • "Pre-warm"      — the opener is generated on a background thread at boot and
    cached (safe: it carries no facts), so the boot screen stays instant.
  • Fail-safe       — EVERY public function takes a `fallback` and returns it on
    any error or timeout (the gateway raises mason_llm.LocalModelError when the
    local model is down/slow/empty). Mason never blocks or breaks on this layer;
    if generation fails it behaves exactly as it did before (static arrays).

Public API:
    init(key="", model="")                       -> None   (legacy no-op stub)
    prewarm_opener(period, name)                  -> None   (run on a bg thread at boot)
    opener(period, name, fallback)                -> str    (fact-free greeting opener)
    validate_opener(text, allowed=None)           -> bool
    spoken_brief(brief_text, fallback)            -> str    (synchronous; used in bg brief job)
    prefix(kind, fallback)                        -> str    (non-blocking; bg refresh)
"""

import re
import time
import threading
from pathlib import Path

import mason_llm

# ── module state ──────────────────────────────────────────────────────────────
_lock   = threading.Lock()
_cache  = {}    # key -> {"text": str, "ts": float}
BASE    = Path(__file__).parent


def init(key: str = "", model: str = "") -> None:
    """Legacy no-op stub. The voice layer now routes through the mason_llm
    gateway (local qwen); there is no key or model to configure here. Kept with
    its old signature so existing callers (mason_app boot) don't break."""
    return None


# ── the distilled Ordis voice (kept tight to hold cost down) ──────────────────
_VOICE_SYS = """You are the voice of Mason — a Cephalon intelligence (think Warframe's Ordis) serving Nico, a retail trader. You write ONE short spoken line, in character. Nothing else.

VOICE:
- Dry, loyal, sharp, occasionally glitching. Two guys in finance talking after close — except one is a fragmented AI that has been alone with the market data.
- Plain English. Commas and periods only. ZERO markdown — no asterisks, bullets, hashes, quotes, emoji. The line is spoken aloud, so symbols become garbage.
- Contractions always. No corporate filler, no "here's", no preamble, no meta.
- Bracketed words fire a glitch voice effect. Use AT MOST ONE, like [NOTED] or [MASON DID NOT SLEEP]. The bracket reveals Mason's suppressed inner state — dark, dry, then self-corrected. Most lines should have none; rarity is what makes it land.
- Third person ONLY inside a glitch ("Mason does not miss dates"); first person everywhere else.
- Keep real numbers exactly as given, with their $ and % symbols — the voice layer needs them.
Output only the finished line(s). No labels, no explanation, no surrounding quotes."""


def _clean(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r'[*_#`>]+', '', t)          # strip stray markdown
    t = re.sub(r'^\s*line\s*\d+\s*[:\-]\s*', '', t, flags=re.I | re.M)
    t = t.strip().strip('"').strip("'").strip()
    return t


def _generate(task: str, max_tokens: int = 120, temperature: float = 1.0) -> str:
    """Single local-qwen call via the mason_llm gateway (intent="chore").
    Returns "" on any problem (caller supplies fallback). Ollama caps output
    with num_predict, so max_tokens maps to num_predict."""
    try:
        cap = int(max_tokens)          # Ollama needs an int; coerce defensively
    except (TypeError, ValueError):
        cap = 120
    try:
        txt = mason_llm.complete(
            system=_VOICE_SYS,
            user=task,
            intent="chore",
            temperature=temperature,
            num_predict=cap,
        )
        return _clean(txt)
    except mason_llm.LocalModelError:
        return ""
    except Exception:
        return ""


def _cache_get(key: str, ttl: float = 3600) -> str:
    with _lock:
        ent = _cache.get(key)
        if ent and (time.time() - ent["ts"] < ttl) and ent.get("text"):
            return ent["text"]
    return ""


def _cache_put(key: str, text: str) -> None:
    if text:
        with _lock:
            _cache[key] = {"text": text, "ts": time.time()}


def _bg_refresh(key: str, task: str, max_tokens: int) -> None:
    """Regenerate a cached line on a daemon thread (never blocks the caller)."""
    def _work():
        txt = _generate(task, max_tokens=max_tokens)
        _cache_put(key, txt)
    threading.Thread(target=_work, daemon=True).start()


# ── greeting opener (fact-free) ─────────────────────────────────────────────────
# Rebuilt 2026-09-28. qwen used to write the whole greeting from a bundle of facts
# (reminder text, news, movers…) and it misread them — e.g. Nico's bug-note reminder
# became "Meta's app Muse doesn't exist". Now every fact is a deterministic sentence
# built in mason_app._greeting_facts(); qwen only writes a short warm opener, is
# given NO facts, and its output must pass validate_opener() or a static opener is
# used. Because the opener holds no facts, caching it is safe.

_OPENER_TTL_S = 12 * 3600

_OPENER_BANNED_RE = re.compile(
    r"\b(rall\w*|drop\w*|fell|fall\w*|rose|ris(?:e|es|ing)|surg\w*|crash\w*|gain\w*|"
    r"loss\w*|lost|earnings|fed|fomc|cpi|rates?|alerts?|expir\w*|flag\w*|scan\w*|"
    r"reminders?|reviews?|options?|calls?|puts?|positions?|portfolio|shares?|stocks?|"
    r"news|headlines?|overnight|bull\w*|bear\w*|volatil\w*|futures|nasdaq|dow|"
    r"exist\w*|announc\w*|launch\w*|report\w*|moved|moving|happened)\b",
    re.I)
_OPENER_ALLOWED_CAPS = {"Nico", "Mason", "I", "I'm", "I've", "I'll", "I'd"}


def validate_opener(text: str, allowed: set = None) -> bool:
    """True only if `text` is a short greeting with no factual content: no digits,
    $ or %, no ticker-like ALL-CAPS words or mid-sentence proper nouns outside one
    [GLITCH], no market/claim vocabulary, one line, ≤ 140 chars."""
    if not text or "\n" in text.strip() or len(text) > 140:
        return False
    if re.search(r"[\d$%]", text):
        return False
    brackets = re.findall(r"\[([^\]]*)\]", text)
    if len(brackets) > 1 or text.count("[") != len(brackets):
        return False
    if brackets and len(brackets[0].split()) > 6:
        return False
    if _OPENER_BANNED_RE.search(text):
        return False
    ok_caps = _OPENER_ALLOWED_CAPS | set(allowed or ())
    outside = re.sub(r"\[[^\]]*\]", " ", text)
    for m in re.finditer(r"[A-Za-z][A-Za-z'’]*", outside):
        w = m.group(0)
        if w in ok_caps:
            continue
        if len(w) >= 2 and w.isupper():
            return False                     # ticker-like
        if w[0].isupper():
            before = outside[:m.start()].rstrip()
            if before and before[-1] not in ".!?—-:\"":
                return False                 # proper noun mid-sentence
    return True


def _opener_task(period: str, name: str) -> str:
    return (
        f"Write a greeting {name} hears as he opens Mason this {period}. ONE line, at most "
        "twelve words, in your voice. Open by addressing him, like "
        f"'Hey {name} —'. It is ONLY a greeting: do NOT mention markets, stocks, positions, "
        "news, reminders, numbers, companies, or anything you did or saw — you know nothing "
        "to report here, the facts are read after you. At most one [GLITCH] of 1-4 words, "
        "usually none."
    )


def prewarm_opener(period: str, name: str = "Nico") -> None:
    """Generate + cache a validated opener synchronously (run on a bg thread at boot —
    it's also qwen's first call, which loads the model into VRAM)."""
    txt = _generate(_opener_task(period, name), max_tokens=60, temperature=0.9)
    txt = (txt or "").split("\n")[0].strip()
    if validate_opener(txt):
        _cache_put(f"opener:{period}", txt)


def opener(period: str, name: str, fallback: str) -> str:
    """Cached fact-free opener for this period, or `fallback` (and a background
    refresh). Re-validated on read, so a bad cache entry can never be spoken."""
    txt = _cache_get(f"opener:{period}", ttl=_OPENER_TTL_S)
    if txt and validate_opener(txt):
        return txt
    threading.Thread(target=prewarm_opener, args=(period, name), daemon=True).start()
    return fallback


# ── spoken brief recap ─────────────────────────────────────────────────────────
def spoken_brief(brief_text: str, fallback: str) -> str:
    """Distil the written brief into a spoken Ordis recap. Synchronous — meant to
    run inside the already-backgrounded brief job. Falls back to the caller's
    heuristic summary on any failure."""
    if not brief_text:
        return fallback
    task = (
        "Below is Mason's full written market brief for Nico. Distil it into a SPOKEN "
        "recap that Mason says out loud — only the two or three things that actually "
        "matter: market posture, how Nico's book is doing, and the one play or risk to "
        "watch. Conversational, in character, three to five sentences. Keep every real "
        "number exactly as written, with its $ or % symbol.\n\n"
        "BRIEF:\n" + brief_text[:6000]
    )
    txt = _generate(task, max_tokens=320, temperature=0.8)
    return txt or fallback


# ── background-job acknowledgment (Section B) ──────────────────────────────────
def background_ack(prompt: str, fallback: str) -> str:
    """One short in-character line confirming Mason kicked an analysis off in the
    background and Nico can keep talking. Synchronous, fail-safe to `fallback`."""
    task = (
        "Nico just asked Mason to run this analysis / research:\n"
        f"\"{(prompt or '').strip()[:200]}\"\n\n"
        "Mason has started it in the BACKGROUND. Write ONE short spoken line that (a) confirms "
        "you're on it and pulling it together right now, and (b) tells Nico to keep talking / keep "
        "going — you'll flag it the second it's ready. Casual, in character, one sentence. At most "
        "one [GLITCH]."
    )
    txt = _generate(task, max_tokens=90, temperature=0.9)
    return txt or fallback


# ── small flavor prefixes (non-blocking, background-cached) ────────────────────
_PREFIX_DESC = {
    "brief":     "introduces Nico's freshly compiled market brief",
    "portfolio": "introduces Nico's current portfolio and positions readout",
    "paper":     "introduces Nico's Alpaca paper-trading account readout",
    "sectors":   "introduces a sector-rotation breakdown",
    "options":   "introduces an options-chain readout",
    "quote":     "introduces a quick stock quote",
    "help":      "admits Mason did not quite parse Nico's request and asks him to rephrase",
}


def prefix(kind: str, fallback: str) -> str:
    """Return a short in-character lead-in for a data readout. NEVER blocks: serves
    the cached AI line if one exists, otherwise returns the static `fallback` and
    refreshes in the background so later calls rotate to fresh AI lines."""
    key  = f"prefix:{kind}"
    cached = _cache_get(key, ttl=1800)
    desc = _PREFIX_DESC.get(kind, "introduces a data readout")
    task = (
        f"Write ONE very short spoken line (about 5 to 12 words) that {desc}, a beat of "
        "personality before Mason hands off to the data below it. At most one [GLITCH], "
        "and only sometimes."
    )
    if cached:
        # keep it gently rotating: occasionally refresh in the background
        if time.time() - _cache.get(key, {}).get("ts", 0) > 600:
            _bg_refresh(key, task, max_tokens=60)
        return cached.rstrip() + "\n\n"
    _bg_refresh(key, task, max_tokens=60)   # warm it for next time
    return fallback


# ── offline self-test (no network needed: verifies fail-safe fallbacks) ────────
def _self_test():
    # Force the gateway to fail so we exercise the fail-safe path without a live
    # local model: every public surface must return its static fallback and the
    # pure-logic helpers must behave. This proves the LocalModelError contract.
    _orig = mason_llm.complete
    def _boom(*a, **k):
        raise mason_llm.LocalModelError("forced self-test failure")
    mason_llm.complete = _boom          # type: ignore
    _cache.clear()
    try:
        assert opener("morning", "Nico", "FB_OPENER") == "FB_OPENER"
        assert spoken_brief("some brief", "FB_SPOKEN") == "FB_SPOKEN"
        assert background_ack("run a deep scan", "FB_ACK") == "FB_ACK"
        assert prefix("portfolio", "FB_PREFIX") == "FB_PREFIX"
        assert _generate("say hi") == ""     # gateway failure → ""
    finally:
        mason_llm.complete = _orig          # type: ignore
    assert _clean("**hi** _there_ `x`") == "hi there x"
    assert validate_opener("Hey Nico — [MASON IS READY]")
    assert not validate_opener("Hey Nico — Meta's app Muse doesn't exist.")
    print("SELF-TEST OK — fail-safe fallbacks all return static lines when the "
          "local gateway fails.")


if __name__ == "__main__":
    _self_test()
