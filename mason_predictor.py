"""
mason_predictor.py — Predictor Agent (MULTI_AGENT_ROADMAP.md Section 4.1), v1.

Consumes the latest Scanner output (Section 5.1) and produces one thesis.json
(Section 5.2) per candidate processed, written to Theses/ — never overwriting.

COST WARNING: every candidate processed = ONE real, billed deep-tier API call
(Opus, via the mason_llm gateway). The per-cycle cap (default 5, by urgency) is
the only thing between a busy scan day and an uncapped bill. See
PREDICTOR_README.md.

HARD RULES (build contract):
  * The ONLY network/LLM touchpoint is _call_model() — injectable, so every
    other line is testable offline with a fake (Constraint #2 of the build).
  * Model tier is a parameter, not a constant: "opus" (the sole tier, per
    Roadmap Section 6). Deep reasoning routes through mason_llm.complete(
    intent="deep"). Same output shape as before.
  * Read-only reuse of mason_scanner / mason_learning / mason_regime_gate /
    mason_intelligence. Never executes, sizes, or journals a trade.
  * Additive-only: writes new files under Theses/ and nothing else.

Entry points:
    run_predictor(model_tier="opus", max_candidates=5, ...) -> list[dict]
    python mason_predictor.py [--tier opus] [--max N] [--dry-run]
"""

import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import mason_llm
import mason_predictor_calibration as CAL
import mason_predictor_prompts as PR

THESES_DIR = BASE / "Theses"
SCHEMA_VERSION = "thesis/1.0"

# ── model tiers (Constraint #1: configurable, no rebuild to A/B) ─────────────────
DEFAULT_TIER = "opus"                       # Roadmap Section 6: Predictor = Opus
MODEL_TIERS = {
    # Deep-tier call. Routes through mason_llm.complete(intent="deep"), which maps
    # to Opus (claude-opus-5-5, the deep target per spec §4.6). max_tokens is
    # passed through to the gateway's Opus provider.
    "opus":  {"model": "claude-opus-5-5", "max_tokens": 4096, "intent": "deep"},
    # Local-tier call (Analyst Desk Phase 3). Routes through
    # mason_llm.complete(intent="chore") -> local qwen, so the desk's news→thesis
    # →debate→arbiter pipeline runs WITHOUT Opus. Same thesis/1.0 output +
    # validation as the opus tier. Kept coarse (Nico's C8) — the desk drafts, the
    # future Opus director sets precise numbers.
    "local": {"model": "qwen3.5:4b", "max_tokens": 2048, "intent": "chore"},
}

DEFAULT_MAX_CANDIDATES = 5                  # per-cycle cap = max real paid calls
_URGENCY_RANK = {"needs_attention_now": 0, "elevated": 1, "routine": 2}

ALLOWED_DIRECTION = {"long", "short"}
ALLOWED_INSTRUMENT = {"equity", "option"}
ALLOWED_OPTION_STRUCTURE = {"long_call", "long_put"}   # Section 0 single-leg policy
ALLOWED_TIMEFRAME = {"0dte", "short_term", "swing", "position"}


# ── the ONE model touchpoint (injectable — Constraint #2) ────────────────────────

def _call_model(system_prompt, user_prompt, tier):
    """The ONE model touchpoint via the mason_llm gateway. Tier-aware: the "opus"
    tier is a REAL, BILLED deep call (intent="deep" -> Opus); the "local" tier is
    a local qwen call (intent="chore", no Opus, no cost). Never invoked by this
    repo's tests — tests inject a fake via run_predictor(call_model=...). Returns
    the model's text."""
    spec = MODEL_TIERS[tier]
    intent = spec.get("intent", "deep")
    if intent == "deep":
        return mason_llm.complete(system_prompt, user_prompt, intent="deep",
                                  max_tokens=spec["max_tokens"])
    return mason_llm.complete(system_prompt, user_prompt, intent="chore",
                              num_predict=spec["max_tokens"])


# ── candidate selection ──────────────────────────────────────────────────────────

def select_candidates(scan_data, max_candidates=DEFAULT_MAX_CANDIDATES):
    """Top-N by urgency: needs_attention_now, then elevated, then routine;
    stable order within a tier (Scanner already sorts within its lists)."""
    pool = ([dict(a, _kind="position_alert") for a in scan_data.get("position_alerts") or []]
            + [dict(c, _kind="new_candidate") for c in scan_data.get("new_candidates") or []])
    pool.sort(key=lambda x: _URGENCY_RANK.get(x.get("urgency"), 3))  # stable sort
    return pool[:max(0, int(max_candidates))]


# ── regime / intelligence context (read-only, fail-soft) ─────────────────────────

def gather_regime_context():
    ctx = {"regime_gate": None, "intelligence": None, "errors": []}
    try:
        import mason_regime_gate as G
        g = G.gate(force_breadth=False)
        ctx["regime_gate"] = {k: g.get(k) for k in
                              ("stance", "risk_zone", "composite", "exposure_ceiling_pct",
                               "risk_multiplier", "new_entries", "fighting_tape", "line", "as_of")}
    except Exception as e:
        ctx["errors"].append(f"regime gate: {e}")
    try:
        import mason_intelligence as MI
        tl, sc = MI.regime_timeline(), MI.scorecard()
        cur = tl.get("current") or {}
        ctx["intelligence"] = {
            "regime": cur.get("regime"), "regime_since": cur.get("start"),
            "regime_streak_snapshots": cur.get("snapshots"),
            "lead_sector": tl.get("lead_sector"),
            "read_confidence_factor": sc.get("confidence_factor"),
            "n_graded_reads": sc.get("n_graded"),
            "calib_accuracy": sc.get("calib_accuracy")}
    except Exception as e:
        ctx["errors"].append(f"intelligence: {e}")
    return ctx


# ── response parsing + thesis validation ─────────────────────────────────────────

def parse_model_response(text):
    """Extract the thesis JSON object from model text. Tolerates code fences and
    stray prose; returns (dict, None) or (None, error_string). Never raises."""
    if not text or not str(text).strip():
        return None, "empty model response"
    s = str(text).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)      # strip fences if present
    start = s.find("{")
    if start < 0:
        return None, "no JSON object in model response"
    depth = 0
    for i in range(start, len(s)):                       # first balanced {...}
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


def validate_thesis(d):
    """Validate a completed thesis dict against the Section 5.2 contract
    (+ Section 0 single-leg policy). Returns (ok, problems)."""
    probs = []
    for k in ("ticker", "source_candidate", "direction", "instrument",
              "option_structure", "timeframe", "entry", "stop", "target",
              "stated_confidence", "calibration_adjusted_confidence",
              "reasoning", "supporting_signals"):
        if k not in d:
            probs.append(f"missing key: {k}")
    if probs:
        return False, probs
    if d["direction"] not in ALLOWED_DIRECTION:
        probs.append(f"bad direction {d['direction']!r}")
    if d["instrument"] not in ALLOWED_INSTRUMENT:
        probs.append(f"bad instrument {d['instrument']!r}")
    if d["instrument"] == "option":
        if d["option_structure"] not in ALLOWED_OPTION_STRUCTURE:
            probs.append(f"single-leg policy: option_structure must be long_call|"
                         f"long_put, got {d['option_structure']!r}")
    elif d["option_structure"] not in (None, "null"):
        probs.append(f"equity thesis must have option_structure null, "
                     f"got {d['option_structure']!r}")
    if d["timeframe"] not in ALLOWED_TIMEFRAME:
        probs.append(f"bad timeframe {d['timeframe']!r}")
    for k in ("entry", "stop", "target"):
        if not isinstance(d[k], (int, float)):
            probs.append(f"{k} must be a number, got {type(d[k]).__name__}")
    for k in ("stated_confidence", "calibration_adjusted_confidence"):
        v = d[k]
        if not isinstance(v, (int, float)) or not (0 <= v <= 100):
            probs.append(f"{k} must be 0-100, got {v!r}")
    if not str(d["reasoning"]).strip():
        probs.append("reasoning is empty")
    if not isinstance(d["supporting_signals"], list):
        probs.append("supporting_signals must be a list")
    return (not probs), probs


# ── output writing (additive-only, mirrors Scanner's never-overwrite rule) ───────

def write_thesis(thesis, output_dir=None):
    outdir = Path(output_dir) if output_dir else THESES_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"thesis_{thesis['ticker']}_{stamp}.json"
    while path.exists():
        path = outdir / f"thesis_{thesis['ticker']}_{stamp}_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(thesis, indent=2, default=str), encoding="utf-8")
    return path


# ── orchestration ────────────────────────────────────────────────────────────────

def _process_one(item, scan_data, regime_ctx, tier, call_model, write, output_dir):
    kind = item.pop("_kind")
    ticker = (item.get("ticker") or "").upper()
    ref = (f"{scan_data.get('cycle_id')}#{kind}:{ticker}"
           + (f":{item.get('trigger')}" if kind == "position_alert" else ""))
    result = {"ticker": ticker, "kind": kind, "source_candidate": ref,
              "ok": False, "path": None, "thesis": None, "error": None}

    setup = CAL.setup_for_item(item, kind)
    calibration = CAL.calibration_for_setup(setup)
    zone = ((regime_ctx.get("regime_gate") or {}).get("risk_zone"))
    scan_meta = {"cycle_id": scan_data.get("cycle_id"),
                 "timestamp": scan_data.get("timestamp")}
    system_prompt, user_prompt = PR.build_prompts(item, kind, setup, calibration,
                                                  regime_ctx, scan_meta)
    try:
        raw = call_model(system_prompt, user_prompt, tier)
    except Exception as e:
        result["error"] = f"model call failed: {e}"
        return result

    parsed, perr = parse_model_response(raw)
    if perr:
        # Deliberately NO auto-retry: a retry is another paid call. Fail loudly.
        result["error"] = perr
        return result

    stated = parsed.get("stated_confidence")
    thesis = {
        "schema_version": SCHEMA_VERSION,
        "ticker": parsed.get("ticker") or ticker,
        "source_candidate": ref,
        "direction": parsed.get("direction"),
        "instrument": parsed.get("instrument"),
        "option_structure": parsed.get("option_structure"),
        "timeframe": parsed.get("timeframe"),
        "entry": parsed.get("entry"),
        "stop": parsed.get("stop"),
        "target": parsed.get("target"),
        "stated_confidence": stated,                                   # model's raw number
        "calibration_adjusted_confidence": CAL.adjust_confidence(       # Section 8 step 2 —
            setup, stated, zone),                                       # logged SEPARATELY
        "reasoning": parsed.get("reasoning"),
        "supporting_signals": parsed.get("supporting_signals"),
        # ── metadata extensions (documented in PREDICTOR_README.md) ──
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model_tier": tier,
        "model_id": MODEL_TIERS[tier]["model"],
        "setup": setup,
        "calibration": calibration,
        "sizing_factor_hint": CAL.sizing_factor(setup, zone),
        "regime_context": regime_ctx,
        "source_item": item,
    }
    ok, probs = validate_thesis(thesis)
    if not ok:
        result["error"] = "thesis failed validation: " + "; ".join(probs)
        result["thesis"] = thesis                # returned for inspection, NOT written
        return result
    result.update(ok=True, thesis=thesis)
    if write:
        result["path"] = str(write_thesis(thesis, output_dir))
    return result


def run_predictor(model_tier=DEFAULT_TIER, max_candidates=DEFAULT_MAX_CANDIDATES,
                  call_model=None, scan=None, write=True, output_dir=None):
    """Run one Predictor cycle. Returns a list of per-candidate result dicts.

    model_tier      "opus"   (the sole tier — deep via mason_llm gateway)
    max_candidates  per-cycle cap — ALSO the max number of real paid API calls
    call_model      injectable model function (tests pass a fake; None = real)
    scan            pre-loaded scanner_output dict (None = get_latest_scan())
    write           False = build + validate theses but write nothing
    """
    if model_tier not in MODEL_TIERS:
        raise ValueError(f"model_tier must be one of {sorted(MODEL_TIERS)}, "
                         f"got {model_tier!r}")
    call_model = call_model or _call_model

    if scan is None:
        import mason_scanner as SC
        latest = SC.get_latest_scan()
        if not latest.get("ok"):
            return [{"ok": False, "error": f"no scanner output: {latest.get('error')}",
                     "ticker": None, "kind": None, "source_candidate": None,
                     "path": None, "thesis": None}]
        scan = latest["data"]

    selected = select_candidates(scan, max_candidates)
    regime_ctx = gather_regime_context()
    return [_process_one(item, scan, regime_ctx, model_tier, call_model,
                         write, output_dir) for item in selected]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Mason Predictor Agent (Roadmap 4.1). "
                    "COSTS REAL MONEY: one billed API call per candidate.")
    ap.add_argument("--tier", default=DEFAULT_TIER, choices=sorted(MODEL_TIERS),
                    help=f"model tier (default {DEFAULT_TIER})")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_CANDIDATES,
                    help=f"per-cycle candidate cap = max paid calls "
                         f"(default {DEFAULT_MAX_CANDIDATES})")
    ap.add_argument("--dry-run", action="store_true",
                    help="select + print the capped candidate list and exit — "
                         "ZERO model calls, zero cost")
    a = ap.parse_args(argv)

    if a.dry_run:
        import mason_scanner as SC
        latest = SC.get_latest_scan()
        if not latest.get("ok"):
            print(f"no scanner output: {latest.get('error')}")
            return 1
        sel = select_candidates(latest["data"], a.max)
        print(f"scan {latest['data'].get('cycle_id')} ({latest.get('age_note')})")
        print(f"would process {len(sel)} candidate(s) = {len(sel)} paid "
              f"{a.tier} call(s):")
        for i, s in enumerate(sel, 1):
            print(f"  {i}. [{s.get('urgency')}] {s.get('ticker')} "
                  f"({s['_kind']}: {s.get('trigger') or ','.join(s.get('flagged_by') or [])})")
        return 0

    results = run_predictor(model_tier=a.tier, max_candidates=a.max)
    wrote = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    for r in wrote:
        t = r["thesis"]
        print(f"OK  {r['ticker']:6} {t['direction']:5} {t['instrument']:7} "
              f"conf {t['stated_confidence']}→{t['calibration_adjusted_confidence']} "
              f"→ {r['path']}")
    for r in failed:
        print(f"ERR {r.get('ticker') or '?':6} {r.get('error')}")
    print(f"{len(wrote)} thesis file(s) written, {len(failed)} failed, "
          f"tier={a.tier}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
