"""
mason_debate.py — Bull/Bear Debate stage (MULTI_AGENT_ROADMAP.md Section 4.2), v1.

One thesis in (Section 5.2 shape, from Theses/), one debate_transcript.json out
(Section 5.3 shape, to Debates/ — never overwritten). Bull and Bear are two
PARALLEL mid-tier model calls forked from the same thesis and joined before the
transcript is built (Roadmap 15.1 — hard requirement).

COST WARNING: every debate run = TWO real local qwen calls (Bull + Bear) via
the mason_llm gateway, plus up to two more if a leg fails once and retries
(Roadmap 15.4).

HARD RULES (build contract):
  * Mid-tier now means a LOCAL qwen call routed through mason_llm.complete(
    intent="chore") — no Anthropic call, no streaming, no extended thinking.
  * The ONLY network/LLM touchpoint is _call_model() — injectable, so tests
    run fully offline with fakes (zero real calls during the build).
  * Retry policy (Roadmap 15.4, differs from Predictor on purpose): a failed
    leg retries EXACTLY once; a second failure is logged on the transcript,
    which is then clearly marked incomplete. Never fabricate a leg's argument;
    never emit an incomplete transcript that looks complete.
  * Read-only reuse of Theses/ + mason_trades (postmortems for the Bear).
    No execution tools, no pipeline-halt power (that's the future Arbiter's).
  * Additive-only: writes new files under Debates/ and nothing else.

Entry points:
    run_debate(thesis, model_tier="local", call_model=None,
               write=True, output_dir=None) -> dict   (one transcript dict)
    python mason_debate.py <thesis_path> | --latest  [--tier local]
                           [--dry-run] [--no-write]
"""

import argparse
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import mason_llm
import mason_debate_prompts as PR

THESES_DIR = BASE / "Theses"
DEBATES_DIR = BASE / "Debates"
SCHEMA_VERSION = "debate_transcript/1.0"
ROUNDS = 1            # fixed single round per Roadmap Section 11 (cost control)
MAX_ATTEMPTS = 2      # 1 try + exactly 1 retry per leg (Roadmap 15.4)

DEFAULT_TIER = "local"
MODEL_TIERS = {
    # Roadmap Section 6: Bull/Bear = mid-tier, now served by local qwen via
    # mason_llm.complete(intent="chore"). max_tokens is passed to the gateway
    # as Ollama's num_predict cap.
    "local": {"model": "qwen3.5:4b", "max_tokens": 1500},
}


# ── the ONE model touchpoint (injectable) ────────────────────────────────────────

def _call_model(system_prompt, user_prompt, tier):
    """REAL local qwen call via the mason_llm gateway (intent="chore").
    Never invoked by this repo's tests; tests inject a fake via
    run_debate(call_model=...). Returns the model's text.

    num_ctx is pinned to 8192 (not the config default of 32768): a debate leg
    is a ~1.6k-token prompt + a 1500-token cap, so 8k is ample headroom, and
    the smaller KV cache is what lets Bull + Bear share the 6GB card as two
    real concurrent Ollama slots instead of being serialized."""
    spec = MODEL_TIERS[tier]
    return mason_llm.complete(system_prompt, user_prompt, intent="chore",
                              num_predict=spec["max_tokens"], num_ctx=8192)


# ── thesis input helpers ─────────────────────────────────────────────────────────

_STAMP_RE = re.compile(r"(\d{8}_\d{6})")


def list_theses(theses_dir=None):
    """Theses/thesis_*.json newest-first. Filenames are thesis_<TICKER>_<stamp>,
    so a plain lexicographic sort would order by ticker — sort by the embedded
    timestamp instead."""
    d = Path(theses_dir) if theses_dir else THESES_DIR
    files = list(d.glob("thesis_*.json"))

    def stamp(p):
        m = _STAMP_RE.search(p.name)
        return m.group(1) if m else "00000000_000000"
    return sorted(files, key=lambda p: (stamp(p), p.name), reverse=True)


def latest_thesis_path(theses_dir=None):
    """Newest thesis file, or None. Convenience only — multiple theses can
    coexist per cycle, so callers wanting a specific one must pass its path."""
    files = list_theses(theses_dir)
    return files[0] if files else None


def load_thesis(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")), p


# ── postmortems for the Bear (Section 16: Bear reads postmortems) ────────────────

def postmortems_for(thesis, limit=5):
    """Real closed trades on this ticker or setup type, projected to the fields
    a Bear argument can cite. Read-only, fail-soft to []."""
    try:
        import mason_trades as MT
        trades = MT.get_all_trades()
    except Exception:
        return []
    tk = (thesis.get("ticker") or "").upper()
    setup = (thesis.get("setup") or "").lower()
    out = []
    for t in trades:
        if t.get("outcome") not in ("win", "loss", "breakeven"):
            continue
        if (t.get("ticker") or "").upper() != tk and \
           (t.get("setup_type") or "").lower() != setup:
            continue
        out.append({k: t.get(k) for k in
                    ("ticker", "setup_type", "direction", "outcome",
                     "actual_pnl", "exit_reason", "review", "entry_date")})
    return out[:limit]


# ── staleness pre-check (added 2026-07-28 — real-money leak found live) ──────────
#
# A position-management/exit thesis argues for managing or closing an EXISTING
# position. If --latest (or an explicitly-passed path) resolves to an old
# thesis like this whose position has since been closed, running a real, paid
# debate on it is pure waste — there's nothing left to manage. This is the
# leak Nico hit live: an old IONQ exit-thesis was still the newest file in
# Theses/, --latest picked it up, and it cost 2 real Sonnet calls before
# mason_arbiter.py's own (correct, but downstream) correlation_check caught
# it for $0. This closes the leak at the source instead of after the spend.
#
# Deliberately a DUPLICATED, narrower copy of mason_arbiter.py's
# _is_position_mgmt/_held_underlyings — NOT an import, since mason_arbiter.py
# already imports this module and a reverse import would be circular. Keep
# the two in sync if either's logic changes; arbiter's version is the
# authoritative, fail-CLOSED hard gate, this one fails OPEN (a transient
# Alpaca hiccup should never silently block a legitimate debate — it's a
# cost-saving nudge, not the safety gate).

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _is_position_mgmt(thesis):
    try:
        is_exit = (float(thesis.get("entry") or 0) == 0.0
                   and float(thesis.get("stop") or 0) == 0.0
                   and float(thesis.get("target") or 0) == 0.0)
    except (TypeError, ValueError):
        is_exit = False
    return is_exit or "#position_alert:" in str(thesis.get("source_candidate") or "")


def _live_holdings():
    """Live paper-book underlying tickers, or None if unreachable (fail-open:
    None means 'skip the check,' not 'nothing is held')."""
    try:
        import mason_execution as EX
        result = EX.positions()
    except Exception:
        return None
    if not result or not result.get("ok"):
        return None
    held = set()
    for p in result.get("positions") or []:
        sym = (p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        m = _OCC_RE.match(sym)
        held.add(m.group(1) if m else sym)
    return held


def thesis_staleness_warning(thesis):
    """None if fine to debate. A reason string if this looks like a
    position-management/exit thesis for a ticker no longer in the live book —
    i.e. the position it manages appears to already be closed."""
    if not _is_position_mgmt(thesis):
        return None
    held = _live_holdings()
    if held is None:
        return None
    tk = (thesis.get("ticker") or "").upper()
    if tk and tk not in held:
        return (f"{tk} is a position-management/exit thesis but {tk} is not "
                f"in the live book (held: {sorted(held) or 'nothing'}) — the "
                f"position this thesis manages appears to already be closed.")
    return None


# ── model-response parsing + leg validation ──────────────────────────────────────

def parse_model_response(text):
    """First balanced JSON object out of model text (tolerates fences/prose).
    Returns (dict, None) or (None, error). Never raises."""
    if not text or not str(text).strip():
        return None, "empty model response"
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip())
    start = s.find("{")
    if start < 0:
        return None, "no JSON object in model response"
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1]), None
                except json.JSONDecodeError as e:
                    return None, f"model JSON failed to parse: {e}"
    return None, "unbalanced JSON in model response"


LEG_KEYS = {
    "bull": ("argument", "evidence_cited"),
    "bear": ("argument", "evidence_cited",
             "setup_history_check", "regime_conflict_check"),
}


def validate_leg(role, d):
    """(ok, problems) for one leg's parsed case against its 5.3 sub-shape."""
    probs = []
    for k in LEG_KEYS[role]:
        if k not in d:
            probs.append(f"{role}: missing key {k}")
    if probs:
        return False, probs
    if not str(d["argument"]).strip():
        probs.append(f"{role}: argument is empty")
    ev = d["evidence_cited"]
    if not isinstance(ev, list) or not ev or not all(
            isinstance(x, str) and x.strip() for x in ev):
        probs.append(f"{role}: evidence_cited must be a non-empty list of "
                     f"non-empty strings (grounding rule)")
    for k in LEG_KEYS[role][2:]:
        if not str(d[k]).strip():
            probs.append(f"{role}: {k} is empty")
    return (not probs), probs


# ── one debate leg, with the retry-once rule (Roadmap 15.4) ──────────────────────

def _run_leg(role, system_prompt, user_prompt, tier, call_model):
    """Call one leg. On ANY failure (exception, malformed output, invalid
    shape) retry exactly once; a second failure returns ok=False with every
    attempt's error logged. Never fabricates content."""
    started = datetime.now().isoformat(timespec="seconds")
    t0 = time.monotonic()
    attempts = []
    for n in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = call_model(system_prompt, user_prompt, tier)
        except Exception as e:
            attempts.append(f"attempt {n}: model call failed: {e}")
            continue
        parsed, perr = parse_model_response(raw)
        if perr:
            attempts.append(f"attempt {n}: {perr}")
            continue
        ok, probs = validate_leg(role, parsed)
        if not ok:
            attempts.append(f"attempt {n}: invalid leg shape: {'; '.join(probs)}")
            continue
        case = {k: parsed[k] for k in LEG_KEYS[role]}
        return {"ok": True, "case": case, "attempts": n,
                "attempt_errors": attempts, "started_at": started,
                "elapsed_seconds": round(time.monotonic() - t0, 2)}
    return {"ok": False, "case": None, "attempts": MAX_ATTEMPTS,
            "attempt_errors": attempts, "error": attempts[-1] if attempts else "?",
            "started_at": started,
            "elapsed_seconds": round(time.monotonic() - t0, 2)}


# ── transcript validation (Section 5.3) ──────────────────────────────────────────

def validate_transcript(d):
    """(ok, problems). A transcript only validates as complete-and-conformant;
    an incomplete transcript (failed leg) fails validation BY DESIGN so nothing
    downstream can mistake it for a full debate."""
    probs = []
    for k in ("thesis_ref", "bull_case", "bear_case", "rounds"):
        if k not in d:
            probs.append(f"missing key: {k}")
    if probs:
        return False, probs
    if not d.get("complete", False):
        probs.append("transcript marked incomplete (a debate leg failed — see "
                     "failed_legs); Arbiter must not consume this")
    if not isinstance(d["thesis_ref"], str) or not d["thesis_ref"]:
        probs.append("thesis_ref must be non-empty string")
    if not isinstance(d["rounds"], int):
        probs.append("rounds must be an integer")
    for role in ("bull", "bear"):
        case = d.get(f"{role}_case")
        if case is None:
            probs.append(f"{role}_case is null (leg did not run/complete)")
            continue
        ok, p = validate_leg(role, case)
        probs.extend(p)
    return (not probs), probs


# ── output writing (additive-only, same convention as Theses/) ───────────────────

def _reserve_path(ticker, output_dir=None):
    outdir = Path(output_dir) if output_dir else DEBATES_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"debate_{ticker}_{stamp}.json"
    while path.exists():
        path = outdir / f"debate_{ticker}_{stamp}_{uuid.uuid4().hex[:6]}.json"
    return path


# ── orchestration ────────────────────────────────────────────────────────────────

def run_debate(thesis, model_tier=DEFAULT_TIER, call_model=None,
               write=True, output_dir=None, force=False):
    """Run one Bull/Bear debate on one thesis. Returns the transcript dict
    (Section 5.3 fields + documented metadata extensions).

    thesis      dict (pre-loaded) or str/Path to a Theses/*.json file
    model_tier  "local" (local qwen via mason_llm gateway — the only tier)
    call_model  injectable model function (tests pass fakes; None = real)
    write       False = build the transcript but write nothing
    force       True = debate anyway even if thesis_staleness_warning() fires
                (default False: refuse and write a $0 SKIPPED transcript —
                see the staleness pre-check above)
    """
    if model_tier not in MODEL_TIERS:
        raise ValueError(f"model_tier must be one of {sorted(MODEL_TIERS)}, "
                         f"got {model_tier!r}")
    call_model = call_model or _call_model

    thesis_file = None
    if isinstance(thesis, (str, Path)):
        thesis, p = load_thesis(thesis)
        thesis_file = p.name
    ticker = (thesis.get("ticker") or "UNKNOWN").upper()
    thesis_ref = ((thesis_file + "::") if thesis_file else "") + \
        str(thesis.get("source_candidate") or f"{ticker}@{thesis.get('generated_at')}")

    if not force:
        staleness = thesis_staleness_warning(thesis)
        if staleness:
            transcript = {
                "schema_version": SCHEMA_VERSION,
                "thesis_ref": thesis_ref, "bull_case": None, "bear_case": None,
                "rounds": 0, "ticker": ticker, "complete": False,
                "status": f"SKIPPED — {staleness} Pass force=True "
                          f"(CLI: --force) to debate anyway.",
                "failed_legs": {}, "leg_meta": {}, "postmortems_provided": 0,
                "model_tier": None, "model_id": None,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "thesis_file": thesis_file, "output_path": None,
                "skipped": True,
            }
            if write:
                path = _reserve_path(ticker, output_dir)
                transcript["output_path"] = str(path)
                path.write_text(json.dumps(transcript, indent=2, default=str),
                                encoding="utf-8")
            return transcript

    pms = postmortems_for(thesis)
    bull_sys, bull_user = PR.build_bull_prompt(thesis)
    bear_sys, bear_user = PR.build_bear_prompt(thesis, postmortems=pms)

    # Fan-out/fan-in (Roadmap 15.1): both legs forked from the same thesis,
    # run concurrently, joined here before the transcript exists.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fb = pool.submit(_run_leg, "bull", bull_sys, bull_user, model_tier, call_model)
        fr = pool.submit(_run_leg, "bear", bear_sys, bear_user, model_tier, call_model)
        bull, bear = fb.result(), fr.result()

    complete = bull["ok"] and bear["ok"]
    failed = {}
    for role, leg in (("bull", bull), ("bear", bear)):
        if not leg["ok"]:
            failed[role] = {"error": leg["error"],
                            "attempt_errors": leg["attempt_errors"],
                            "attempts": leg["attempts"]}
    transcript = {
        "schema_version": SCHEMA_VERSION,
        # ── Section 5.3 contract ──
        "thesis_ref": thesis_ref,
        "bull_case": bull["case"],
        "bear_case": bear["case"],
        "rounds": ROUNDS,
        # ── metadata extensions (documented in DEBATE_README.md) ──
        "ticker": ticker,
        "complete": complete,
        "status": ("complete" if complete else
                   f"INCOMPLETE — {', '.join(sorted(failed))} leg failed after "
                   f"{MAX_ATTEMPTS} attempts (Roadmap 15.4: retried once, now "
                   f"logged; Arbiter must not render a verdict on this)"),
        "failed_legs": failed,
        "leg_meta": {r: {k: leg[k] for k in
                         ("attempts", "attempt_errors", "started_at",
                          "elapsed_seconds")}
                     for r, leg in (("bull", bull), ("bear", bear))},
        "postmortems_provided": len(pms),
        "model_tier": model_tier,
        "model_id": MODEL_TIERS[model_tier]["model"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thesis_file": thesis_file,
        "output_path": None,
    }
    if write:
        path = _reserve_path(ticker, output_dir)
        transcript["output_path"] = str(path)
        path.write_text(json.dumps(transcript, indent=2, default=str),
                        encoding="utf-8")
    return transcript


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Mason Bull/Bear Debate (Roadmap 4.2). COSTS REAL MONEY: "
                    "2 billed calls per run (up to 4 if legs retry).")
    ap.add_argument("thesis", nargs="?", help="path to a Theses/thesis_*.json")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest file in Theses/ by timestamp")
    ap.add_argument("--tier", default=DEFAULT_TIER, choices=sorted(MODEL_TIERS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print chosen thesis + both prompts — ZERO model "
                         "calls, zero cost")
    ap.add_argument("--no-write", action="store_true",
                    help="run but don't write Debates/ file")
    ap.add_argument("--force", action="store_true",
                    help="debate anyway even if this looks like a stale "
                         "position-management thesis (position already "
                         "closed in the live book)")
    a = ap.parse_args(argv)

    if a.thesis and a.latest:
        print("pass a thesis path OR --latest, not both")
        return 1
    if a.thesis:
        tpath = Path(a.thesis)
    elif a.latest:
        tpath = latest_thesis_path()
        if tpath is None:
            print("no thesis files found in Theses/")
            return 1
    else:
        avail = "\n  ".join(p.name for p in list_theses()[:10]) or "(none)"
        print("need a thesis path or --latest. Available (newest first):\n  " + avail)
        return 1
    if not tpath.exists():
        print(f"no such thesis file: {tpath}")
        return 1

    if a.dry_run:
        thesis, p = load_thesis(tpath)
        if not a.force:
            staleness = thesis_staleness_warning(thesis)
            if staleness:
                print(f"DRY RUN — {p.name}: {staleness}")
                print("Would SKIP for $0 on a real run (pass --force to "
                      "debate anyway).")
                return 0
        pms = postmortems_for(thesis)
        bull_sys, bull_user = PR.build_bull_prompt(thesis)
        bear_sys, bear_user = PR.build_bear_prompt(thesis, postmortems=pms)
        print(f"DRY RUN — would debate {thesis.get('ticker')} from {p.name} "
              f"({len(pms)} postmortems for the Bear); 2 paid {a.tier} calls "
              f"when run for real, 0 now.")
        print("\n===== BULL system =====\n" + bull_sys)
        print("\n===== BULL user =====\n" + bull_user)
        print("\n===== BEAR system =====\n" + bear_sys)
        print("\n===== BEAR user =====\n" + bear_user)
        return 0

    t = run_debate(str(tpath), model_tier=a.tier, write=not a.no_write,
                   force=a.force)
    if t.get("skipped"):
        print(f"SKIP {t['ticker']:6} {t['status']}")
        print(f"     no charge — → {t['output_path'] or '(not written)'}")
        return 0
    ok, probs = validate_transcript(t)
    if ok:
        print(f"OK  {t['ticker']:6} debate complete — bull {len(t['bull_case']['evidence_cited'])} "
              f"citations / bear {len(t['bear_case']['evidence_cited'])} citations "
              f"→ {t['output_path'] or '(not written)'}")
        return 0
    print(f"ERR {t['ticker']:6} {t['status']}")
    for r, f in (t.get("failed_legs") or {}).items():
        for e in f["attempt_errors"]:
            print(f"    {r}: {e}")
    print(f"    transcript logged → {t['output_path'] or '(not written)'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
