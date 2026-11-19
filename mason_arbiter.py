"""
mason_arbiter.py — Arbiter / Reviser stage (MULTI_AGENT_ROADMAP.md Section 4.3), v1.

One debate transcript in (Section 5.3, from Debates/), one arbiter_verdict.json
out (Section 5.4, to Verdicts/ — never overwritten). The only agent so far with
pipeline-halt power: REJECT ends the candidate.

EXECUTION ORDER — the safety design (Roadmap Section 7):
  1. validate the transcript (mason_debate.validate_transcript) — incomplete
     debate → safe REJECT, nothing else runs (Roadmap 15.4).
  2. resolve the original thesis via transcript["thesis_file"] — missing →
     safe REJECT.
  3. HARD GATES IN CODE, before any model call: fresh regime-gate read + live
     paper-book correlation check. A gate "fail" → REJECT built entirely in
     code, model NEVER called (cost skip + the model can't talk past a gate
     it never sees).
  4. only if gates pass: ONE mid-tier model call judges the debate
     (APPROVE / REVISE / REJECT), routed through the mason_llm gateway (local
     qwen by default; see the arbiter_verdict_tier seam below for the optional
     Opus escalation of the final verdict). One retry on failure; a second
     failure → verdict REJECT, never APPROVE. Fail closed, always.

REVISE is mechanical-only: the model proposes new values for a whitelist of
bounded fields (entry/stop/target/timeframe); CODE copies the original thesis
and applies exactly those — the model cannot rewrite reasoning, signals,
direction, instrument, or confidence. Anything outside the whitelist
downgrades the verdict to REJECT with a note.

COST: 0 or 1 billed calls per run — 0 when a hard gate (or input validation)
already decided, 1 (worst case 2 with the single retry) otherwise.

Read-only reuse: mason_debate (validate_transcript, postmortems_for),
mason_regime_gate.gate(), mason_execution.positions(). ZERO execution tools
(Section 16). Additive-only: writes new files under Verdicts/ and nothing else.

Entry points:
    run_arbiter(transcript, model_tier="local", call_model=None,
                write=True, output_dir=None) -> dict   (one verdict dict)
    python mason_arbiter.py <transcript_path> | --latest  [--tier local]
                            [--dry-run] [--no-write]
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
import mason_debate as D
import mason_arbiter_prompts as PR

THESES_DIR = BASE / "Theses"
DEBATES_DIR = BASE / "Debates"
VERDICTS_DIR = BASE / "Verdicts"
STRATEGY_GUIDE = BASE / "docs" / "active" / "STRATEGY_GUIDE.md"
SCHEMA_VERSION = "arbiter_verdict/1.0"
MAX_ATTEMPTS = 2                     # 1 try + exactly 1 retry (mirrors debate legs)

VERDICTS = ("APPROVE", "REVISE", "REJECT")
ALLOWED_TIMEFRAME = {"0dte", "short_term", "swing", "position"}
ALLOWED_OPTION_STRUCTURE = {None, "long_call", "long_put"}   # Section 0 policy

DEFAULT_TIER = "local"
MODEL_TIERS = {
    # Mid-tier per Roadmap Section 6, same rule as Bull/Bear: now served by
    # local qwen via mason_llm.complete(intent="chore"). max_tokens is passed
    # to the gateway as Ollama's num_predict cap. A verdict is short: 1000
    # tokens is plenty.
    "local": {"model": "qwen3.5:4b", "max_tokens": 1000},
}

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ── the ONE model touchpoint (injectable) ────────────────────────────────────────

def _verdict_intent():
    """Nico-owned seam (spec §11 item 5). The arbiter renders the FINAL VERDICT
    — a fair candidate for Opus escalation. Reads mason.config.json
    llm.arbiter_verdict_tier (default "local"); "opus" routes the verdict call
    through the deep tier, anything else stays on local qwen. Default keeps qwen.

    # TODO(Nico) §11: escalate final verdict to Opus? Flip
    #   llm.arbiter_verdict_tier -> "opus" in mason.config.json to try it.
    #   The key is NOT written to config by this rewire — a missing key reads
    #   as "local" here, so today's behavior is unchanged.
    """
    try:
        cfg = json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    tier = (cfg.get("llm", {}) or {}).get("arbiter_verdict_tier", "local")
    return "deep" if tier == "opus" else "chore"


def _call_model(system_prompt, user_prompt, tier):
    """REAL mid-tier call via the mason_llm gateway. Local qwen (intent="chore")
    by default; escalates to Opus (intent="deep") when the Nico-owned
    llm.arbiter_verdict_tier config flag is set to "opus" (see _verdict_intent).
    Never invoked by this repo's tests; tests inject fakes via
    run_arbiter(call_model=...). Returns the model's text."""
    spec = MODEL_TIERS[tier]
    intent = _verdict_intent()
    if intent == "deep":
        return mason_llm.complete(system_prompt, user_prompt, intent="deep",
                                  max_tokens=spec["max_tokens"])
    return mason_llm.complete(system_prompt, user_prompt, intent="chore",
                              num_predict=spec["max_tokens"])


# ── data-source wrappers (module-level so tests can monkeypatch them) ────────────

def _fresh_gate():
    """FRESH regime posture at verdict time — never the thesis's stale embedded
    snapshot (real time passed since the Predictor ran)."""
    import mason_regime_gate as G
    return G.gate(force_breadth=False)


def _live_positions():
    """Live paper book. Returns mason_execution.positions() shape:
    {"ok": bool, "positions": [...]} or {"ok": False, "error": ...}."""
    import mason_execution as EX
    return EX.positions()


def _strategy_guide_text():
    try:
        return STRATEGY_GUIDE.read_text(encoding="utf-8")
    except Exception:
        return None


# ── transcript input helpers (mirror mason_debate's thesis helpers) ──────────────

_STAMP_RE = re.compile(r"(\d{8}_\d{6})")


def list_transcripts(debates_dir=None):
    """Debates/debate_*.json newest-first, by embedded timestamp (a plain
    filename sort would order by ticker prefix)."""
    d = Path(debates_dir) if debates_dir else DEBATES_DIR
    files = list(d.glob("debate_*.json"))

    def stamp(p):
        m = _STAMP_RE.search(p.name)
        return m.group(1) if m else "00000000_000000"
    return sorted(files, key=lambda p: (stamp(p), p.name), reverse=True)


def latest_transcript_path(debates_dir=None):
    files = list_transcripts(debates_dir)
    return files[0] if files else None


def load_transcript(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")), p


# ── deterministic checks (Section 7: code, never LLM) ────────────────────────────

def _is_exit_thesis(thesis):
    """Predictor convention: entry/stop/target all 0 = full-exit thesis, no
    new-risk leg (documented in the IONQ live thesis reasoning)."""
    try:
        return (float(thesis.get("entry") or 0) == 0.0
                and float(thesis.get("stop") or 0) == 0.0
                and float(thesis.get("target") or 0) == 0.0)
    except (TypeError, ValueError):
        return False


def _is_position_mgmt(thesis):
    """Position-management thesis = exit-shaped OR generated from a Scanner
    position alert (source_candidate carries '#position_alert:'). These manage
    EXISTING exposure — a 'hold with tightened stop' thesis on a held ticker
    carries real levels but is not a new entry, so gating it as one would
    hard-reject legitimate position management (found live 2026-07-28: held-
    book theses would have dead-ended $0 after paid upstream calls)."""
    return (_is_exit_thesis(thesis)
            or "#position_alert:" in str(thesis.get("source_candidate") or ""))


def _held_underlyings(positions_list):
    """Set of held underlying tickers; OCC option symbols map to their root."""
    held = set()
    for p in positions_list or []:
        sym = (p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        m = _OCC_RE.match(sym)
        held.add(m.group(1) if m else sym)
    return held


def regime_gate_check(thesis, gate_result, is_mgmt):
    """'pass' | 'fail' + reason. HARD RULES (deterministic, Section 7):
      - EXIT / position-management theses never fail the regime gate —
        managing or reducing existing exposure is not a new entry. (If a mgmt
        thesis actually proposes ADDING, the posture is in the model's prompt
        and exposure-ceiling arithmetic is Risk Sizing's hard gate later —
        add-vs-trim is not reliably distinguishable from thesis shape here.)
      - New-risk theses fail when the fresh posture's new_entries policy is:
          'no'                → fail always
          'adds/hedges-only'  → fail (v1 treats any new thesis as a new entry;
                                a true add would fail correlation anyway, and
                                hedge detection is out of v1 scope)
          'defined-risk-only' → fail for equity theses; long options pass
                                (premium-bounded loss = defined risk)
          'selective' | 'yes' → pass
    """
    if is_mgmt:
        what = ("exit thesis — reduces risk" if _is_exit_thesis(thesis)
                else "position-management thesis on existing exposure — not a "
                     "new entry")
        return "pass", f"{what}; regime gate not violated"
    policy = (gate_result or {}).get("new_entries") or "selective"
    zone = (gate_result or {}).get("risk_zone")
    stance = (gate_result or {}).get("stance")
    where = f"current posture {stance}/{zone}, new_entries='{policy}'"
    if policy == "no":
        return "fail", f"regime forbids all new entries ({where})"
    if policy == "adds/hedges-only":
        return "fail", (f"regime restricts to adds/hedges only and this is a "
                        f"new entry ({where})")
    if policy == "defined-risk-only":
        if (thesis.get("instrument") == "option"
                and thesis.get("option_structure") in ("long_call", "long_put")):
            return "pass", (f"defined-risk-only posture satisfied by long "
                            f"option (premium-bounded) ({where})")
        return "fail", (f"regime allows defined-risk-only and this is an "
                        f"equity thesis ({where})")
    return "pass", f"policy permits new entries ({where})"


def correlation_check(thesis, positions_result, is_mgmt):
    """'pass' | 'fail' | 'flagged' + reason. HARD RULES (deterministic):
      - New-risk + same ticker already held (equity or option root) → FAIL
        (unambiguous concentration).
      - New-risk + live book unreachable → FAIL (cannot verify concentration —
        fail closed, Section 7 doctrine).
      - Exit/position-mgmt + ticker not held → FAIL (stale input: nothing to
        manage — live-verified 2026-07-28 on the closed IONQ position).
      - Exit/position-mgmt + ticker held → PASS (managing a held position is
        coherent, not concentration).
      - Exit/position-mgmt + book unreachable → FLAGGED (can't confirm the
        position still exists; the model is told).
    NOT covered in v1 (documented, deliberate): related/adjacent-name
    detection and true cross-ticker correlation math — positions() carries no
    sector/industry metadata to detect relatedness from, and options
    correlation is an open roadmap question (Section 11). The 'flagged' state
    is wired end-to-end so a smarter check can slot in later.
    """
    tk = (thesis.get("ticker") or "").upper()
    what = "exit" if _is_exit_thesis(thesis) else "position-management"
    if not positions_result or not positions_result.get("ok"):
        err = (positions_result or {}).get("error", "no response")
        if is_mgmt:
            return "flagged", (f"live book unreachable ({err}) — cannot "
                               f"confirm the {tk} position still exists; "
                               f"proceeding, model informed")
        return "fail", (f"live book unreachable ({err}) — cannot verify "
                        f"concentration for a new-risk entry; failing closed")
    held = _held_underlyings(positions_result.get("positions"))
    if is_mgmt:
        if tk in held:
            return "pass", f"{what} thesis and {tk} is held — coherent"
        return "fail", (f"{what} thesis but {tk} is not in the live book "
                        f"(held: {sorted(held) or 'nothing'}) — stale input, "
                        f"nothing to exit")
    if tk in held:
        return "fail", (f"{tk} already held as an open position — new entry "
                        f"would concentrate an existing bet")
    return "pass", (f"{tk} not held; book holds {len(held)} underlying(s) "
                    f"({sorted(held) or 'none'})")


def postmortem_summary(postmortems):
    """Short string for the 5.4 postmortem_check field (not a raw dump)."""
    if not postmortems:
        return "no closed trades on this ticker or setup type — no postmortem signal"
    wins = sum(1 for p in postmortems if p.get("outcome") == "win")
    losses = sum(1 for p in postmortems if p.get("outcome") == "loss")
    pnl = sum(float(p.get("actual_pnl") or 0) for p in postmortems)
    reasons = sorted({(p.get("exit_reason") or "?") for p in postmortems})
    s = (f"{len(postmortems)} closed trade(s) on this ticker/setup: {wins} win, "
         f"{losses} loss, net P&L {pnl:+.2f}; exit reasons: {', '.join(reasons)}")
    if losses and not wins:
        s += " — ALL losses (pattern worth weight)"
    return s


def policy_flags(thesis):
    """Playbook/standing-policy conformance flags (Section 0 single-leg rule).
    Upstream stages should make these impossible — the Arbiter is the last
    line of defense that FLAGS rather than silently ignores."""
    flags = []
    if thesis.get("option_structure") not in ALLOWED_OPTION_STRUCTURE:
        flags.append(f"option_structure {thesis.get('option_structure')!r} "
                     f"violates single-leg-long-only policy (Roadmap §0)")
    if thesis.get("instrument") == "equity" and thesis.get("option_structure"):
        flags.append("equity thesis carries an option_structure — malformed")
    if thesis.get("instrument") not in ("equity", "option"):
        flags.append(f"unknown instrument {thesis.get('instrument')!r}")
    return flags


def deterministic_checks(thesis, gate_result, positions_result):
    """Pure function — all the Section 7 code-side checks in one place.
    Returns a dict with the 5.4 field values, reasons, and prompt context."""
    is_exit = _is_exit_thesis(thesis)
    is_mgmt = _is_position_mgmt(thesis)
    g_status, g_reason = regime_gate_check(thesis, gate_result, is_mgmt)
    c_status, c_reason = correlation_check(thesis, positions_result, is_mgmt)
    pms = D.postmortems_for(thesis)
    flags = policy_flags(thesis)
    hard_fail = []
    if g_status == "fail":
        hard_fail.append(f"regime_gate_check failed — {g_reason}")
    if c_status == "fail":
        hard_fail.append(f"correlation_check failed — {c_reason}")
    return {
        "is_exit": is_exit,
        "is_position_mgmt": is_mgmt,
        "regime_gate_check": g_status,
        "regime_gate_reason": g_reason,
        "correlation_check": c_status,
        "correlation_reason": c_reason,
        "postmortems": pms,
        "postmortem_check": postmortem_summary(pms),
        "policy_flags": flags,
        "hard_fail_reasons": hard_fail,
        "hard_fail": bool(hard_fail),
        "gate_snapshot": {k: (gate_result or {}).get(k) for k in
                          ("stance", "risk_zone", "composite",
                           "exposure_ceiling_pct", "risk_multiplier",
                           "new_entries", "fighting_tape", "line", "as_of")},
        "summary": {
            "regime_gate_check": f"{g_status} — {g_reason}",
            "correlation_check": f"{c_status} — {c_reason}",
            "postmortem_check": postmortem_summary(pms),
            "thesis_is_exit": is_exit,
            "thesis_is_position_mgmt": is_mgmt,
        },
    }


# ── model-response parsing + REVISE bounding ─────────────────────────────────────

def parse_model_response(text):
    """First balanced JSON object out of model text. (dict, None) or
    (None, error). Never raises."""
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


def validate_model_verdict(d):
    """(ok, problems) for the model's raw {verdict, reasoning, revised_fields}."""
    probs = []
    if d.get("verdict") not in VERDICTS:
        probs.append(f"verdict must be one of {VERDICTS}, got {d.get('verdict')!r}")
    if not str(d.get("reasoning") or "").strip():
        probs.append("reasoning is empty")
    rf = d.get("revised_fields")
    if d.get("verdict") == "REVISE":
        if not isinstance(rf, dict) or not rf:
            probs.append("REVISE requires a non-empty revised_fields object")
    elif rf not in (None, {}):
        probs.append("revised_fields must be null unless verdict is REVISE")
    return (not probs), probs


def apply_revision(thesis, revised_fields):
    """Build revised_plan IN CODE (Constraint #6): copy the original thesis,
    apply ONLY whitelisted, type-checked fields. Returns (plan, None) or
    (None, reason) — any out-of-bounds proposal rejects the whole revision."""
    illegal = [k for k in revised_fields if k not in PR.REVISABLE_FIELDS]
    if illegal:
        return None, (f"revised_fields touched non-whitelisted field(s) "
                      f"{illegal} — only {list(PR.REVISABLE_FIELDS)} may be "
                      f"revised (mechanical-edits-only rule)")
    plan = json.loads(json.dumps(thesis, default=str))     # deep copy
    changed = {}
    for k, v in revised_fields.items():
        if k == "timeframe":
            if v not in ALLOWED_TIMEFRAME:
                return None, f"revised timeframe {v!r} not in {sorted(ALLOWED_TIMEFRAME)}"
        else:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                return None, f"revised {k} must be a positive number, got {v!r}"
        if plan.get(k) != v:
            changed[k] = {"from": plan.get(k), "to": v}
            plan[k] = v
    if not changed:
        return None, "REVISE proposed no actual change to any field"
    plan["revision"] = {"revised_by": "arbiter", "fields": changed,
                        "revised_at": datetime.now().isoformat(timespec="seconds")}
    return plan, None


# ── verdict validation (Section 5.4) ─────────────────────────────────────────────

def validate_verdict(d):
    """(ok, problems) against the 5.4 contract. 'not_run' is tolerated on the
    check fields ONLY for non-arbitrated safe-REJECTs (invalid input paths)."""
    probs = []
    for k in ("thesis_ref", "verdict", "reasoning", "regime_gate_check",
              "correlation_check", "postmortem_check", "revised_plan"):
        if k not in d:
            probs.append(f"missing key: {k}")
    if probs:
        return False, probs
    if d["verdict"] not in VERDICTS:
        probs.append(f"bad verdict {d['verdict']!r}")
    not_run_ok = d["verdict"] == "REJECT" and d.get("arbitrated") is False
    if d["regime_gate_check"] not in (("pass", "fail", "not_run") if not_run_ok
                                      else ("pass", "fail")):
        probs.append(f"bad regime_gate_check {d['regime_gate_check']!r}")
    if d["correlation_check"] not in (("pass", "fail", "flagged", "not_run")
                                      if not_run_ok else ("pass", "fail", "flagged")):
        probs.append(f"bad correlation_check {d['correlation_check']!r}")
    if not str(d["reasoning"]).strip():
        probs.append("reasoning is empty")
    if d["verdict"] == "REVISE":
        if not isinstance(d["revised_plan"], dict):
            probs.append("REVISE verdict requires a thesis-shaped revised_plan")
    elif d["revised_plan"] is not None:
        probs.append("revised_plan must be null unless verdict is REVISE")
    if isinstance(d.get("revised_plan"), dict):
        rp = d["revised_plan"]
        if rp.get("timeframe") not in ALLOWED_TIMEFRAME:
            probs.append(f"revised_plan bad timeframe {rp.get('timeframe')!r}")
        for k in ("entry", "stop", "target"):
            if not isinstance(rp.get(k), (int, float)):
                probs.append(f"revised_plan {k} must be a number")
        if rp.get("option_structure") not in ALLOWED_OPTION_STRUCTURE:
            probs.append("revised_plan violates single-leg policy")
    return (not probs), probs


# ── output writing (additive-only, same convention as Theses/Debates) ────────────

def _reserve_path(ticker, output_dir=None):
    outdir = Path(output_dir) if output_dir else VERDICTS_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"verdict_{ticker}_{stamp}.json"
    while path.exists():
        path = outdir / f"verdict_{ticker}_{stamp}_{uuid.uuid4().hex[:6]}.json"
    return path


# ── orchestration ────────────────────────────────────────────────────────────────

def _base_verdict(thesis_ref, ticker, transcript_file):
    return {
        "schema_version": SCHEMA_VERSION,
        # ── Section 5.4 contract ──
        "thesis_ref": thesis_ref,
        "verdict": "REJECT",                 # everything defaults CLOSED
        "reasoning": "",
        "regime_gate_check": "not_run",
        "correlation_check": "not_run",
        "postmortem_check": "not run",
        "revised_plan": None,
        # ── metadata extensions (documented in ARBITER_README.md) ──
        "ticker": ticker,
        "transcript_file": transcript_file,
        "arbitrated": False,                 # did checks+judgment actually run
        "hard_gate_forced": False,           # model call skipped by a code gate
        "model_called": False,
        "model_attempts": 0,
        "model_attempt_errors": [],
        "model_tier": None,
        "model_id": None,
        "checks_detail": None,
        "generated_at": None,
        "output_path": None,
    }


def _finish(v, write, output_dir):
    v["generated_at"] = datetime.now().isoformat(timespec="seconds")
    if write:
        path = _reserve_path(v["ticker"] or "UNKNOWN", output_dir)
        v["output_path"] = str(path)
        path.write_text(json.dumps(v, indent=2, default=str), encoding="utf-8")
    return v


def run_arbiter(transcript, model_tier=DEFAULT_TIER, call_model=None,
                write=True, output_dir=None):
    """Arbitrate one debate transcript. Returns the verdict dict (5.4 fields +
    metadata). SAFE-FAILURE CONTRACT: every abnormal path yields a structured
    REJECT — this function does not raise on bad input or model failure.

    transcript  dict (pre-loaded) or str/Path to a Debates/*.json file
    call_model  injectable model function (tests pass fakes; None = real)
    """
    if model_tier not in MODEL_TIERS:
        raise ValueError(f"model_tier must be one of {sorted(MODEL_TIERS)}, "
                         f"got {model_tier!r}")
    call_model = call_model or _call_model

    # Resolve transcript input.
    transcript_file = None
    if isinstance(transcript, (str, Path)):
        try:
            transcript, p = load_transcript(transcript)
            transcript_file = p.name
        except Exception as e:
            v = _base_verdict("(unresolvable)", None, str(transcript))
            v["reasoning"] = (f"REJECT: transcript could not be loaded ({e}) — "
                              f"nothing to arbitrate. Fail-closed default.")
            return _finish(v, write, output_dir)
    ticker = (transcript.get("ticker") or "UNKNOWN").upper()
    thesis_ref = str(transcript.get("thesis_ref") or "(missing thesis_ref)")
    v = _base_verdict(thesis_ref, ticker, transcript_file)

    # Step 1 — transcript must be a COMPLETE debate (Roadmap 15.4).
    ok, probs = D.validate_transcript(transcript)
    if not ok:
        v["reasoning"] = (
            "REJECT: upstream debate transcript is incomplete or invalid and "
            "cannot be arbitrated — an Arbiter must never render a verdict on "
            "half a debate (Roadmap 15.4). Problems: " + "; ".join(probs))
        return _finish(v, write, output_dir)

    # Step 2 — resolve the original thesis (lookup, not re-analysis).
    tf = transcript.get("thesis_file")
    if not tf:
        v["reasoning"] = ("REJECT: transcript carries no thesis_file reference "
                          "— original thesis unresolvable. Fail-closed default.")
        return _finish(v, write, output_dir)
    tpath = THESES_DIR / tf
    try:
        thesis = json.loads(tpath.read_text(encoding="utf-8"))
    except Exception as e:
        v["reasoning"] = (f"REJECT: original thesis {tf!r} could not be loaded "
                          f"({e}) — will not guess at missing fields. "
                          f"Fail-closed default.")
        return _finish(v, write, output_dir)

    # Step 3 — deterministic hard gates, IN CODE, before any model call.
    checks = deterministic_checks(thesis, _fresh_gate(), _live_positions())
    v.update(arbitrated=True,
             regime_gate_check=checks["regime_gate_check"],
             correlation_check=checks["correlation_check"],
             postmortem_check=checks["postmortem_check"],
             checks_detail={k: checks[k] for k in
                            ("is_exit", "is_position_mgmt",
                             "regime_gate_reason", "correlation_reason",
                             "policy_flags", "gate_snapshot", "postmortems")})
    if checks["hard_fail"]:
        # Constraint #1: gate already decided — model NEVER consulted.
        v["hard_gate_forced"] = True
        v["reasoning"] = ("REJECT: " + "; ".join(checks["hard_fail_reasons"])
                          + ". Hard gate decided in code (Roadmap Section 7); "
                            "no model call was made.")
        return _finish(v, write, output_dir)

    # Step 4 — the one model call (with exactly one retry, then fail CLOSED).
    system_prompt, user_prompt = PR.build_verdict_prompt(
        transcript, thesis, checks, _strategy_guide_text())
    v.update(model_called=True, model_tier=model_tier,
             model_id=MODEL_TIERS[model_tier]["model"])
    parsed = None
    for n in range(1, MAX_ATTEMPTS + 1):
        v["model_attempts"] = n
        try:
            raw = call_model(system_prompt, user_prompt, model_tier)
        except Exception as e:
            v["model_attempt_errors"].append(f"attempt {n}: model call failed: {e}")
            continue
        cand, perr = parse_model_response(raw)
        if perr:
            v["model_attempt_errors"].append(f"attempt {n}: {perr}")
            continue
        mok, mprobs = validate_model_verdict(cand)
        if not mok:
            v["model_attempt_errors"].append(
                f"attempt {n}: invalid verdict shape: {'; '.join(mprobs)}")
            continue
        parsed = cand
        break
    if parsed is None:
        # Constraint #4: an Arbiter that can't reach a judgment fails CLOSED.
        v["reasoning"] = (
            "REJECT: arbiter model call failed after "
            f"{MAX_ATTEMPTS} attempts ({'; '.join(v['model_attempt_errors'])}) "
            "— defaulting to REJECT, never APPROVE, on judgment failure.")
        return _finish(v, write, output_dir)

    verdict, reasoning = parsed["verdict"], str(parsed["reasoning"]).strip()
    revised_plan = None
    if verdict == "REVISE":
        revised_plan, rerr = apply_revision(thesis, parsed["revised_fields"])
        if rerr:
            # Out-of-bounds revision → fail closed with the note, per spec.
            verdict = "REJECT"
            reasoning = (f"REJECT (downgraded from REVISE): proposed revision "
                         f"was out of bounds — {rerr}. Model reasoning was: "
                         f"{reasoning}")
    v.update(verdict=verdict, reasoning=reasoning, revised_plan=revised_plan)
    return _finish(v, write, output_dir)


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Mason Arbiter (Roadmap 4.3). COSTS REAL MONEY when gates "
                    "pass: 0 or 1 billed calls per run (2 if the one retry fires).")
    ap.add_argument("transcript", nargs="?", help="path to a Debates/debate_*.json")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest file in Debates/ by timestamp")
    ap.add_argument("--tier", default=DEFAULT_TIER, choices=sorted(MODEL_TIERS))
    ap.add_argument("--dry-run", action="store_true",
                    help="run validation + deterministic gates and print what "
                         "would happen — ZERO model calls, zero cost")
    ap.add_argument("--no-write", action="store_true",
                    help="run but don't write a Verdicts/ file")
    a = ap.parse_args(argv)

    if a.transcript and a.latest:
        print("pass a transcript path OR --latest, not both")
        return 1
    if a.transcript:
        tpath = Path(a.transcript)
    elif a.latest:
        tpath = latest_transcript_path()
        if tpath is None:
            print("no transcript files found in Debates/")
            return 1
    else:
        avail = "\n  ".join(p.name for p in list_transcripts()[:10]) or "(none)"
        print("need a transcript path or --latest. Available (newest first):\n  "
              + avail)
        return 1
    if not tpath.exists():
        print(f"no such transcript file: {tpath}")
        return 1

    if a.dry_run:
        transcript, p = load_transcript(tpath)
        ok, probs = D.validate_transcript(transcript)
        print(f"DRY RUN — transcript {p.name} valid={ok}"
              + (f" problems={probs}" if probs else ""))
        if not ok:
            print("would return safe REJECT (incomplete transcript) — "
                  "NO model call, $0")
            return 0
        tf = transcript.get("thesis_file")
        try:
            thesis = json.loads((THESES_DIR / tf).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"thesis {tf!r} unresolvable ({e}) — would return safe "
                  f"REJECT, NO model call, $0")
            return 0
        checks = deterministic_checks(thesis, _fresh_gate(), _live_positions())
        print(json.dumps(checks["summary"], indent=2))
        if checks["hard_fail"]:
            print("HARD GATE would force REJECT — model would NOT be called "
                  "($0):")
            for r in checks["hard_fail_reasons"]:
                print(f"  - {r}")
            return 0
        sysp, userp = PR.build_verdict_prompt(transcript, thesis, checks,
                                              _strategy_guide_text())
        print(f"gates pass — 1 paid {a.tier} call would be made. Prompt below.")
        print("\n===== ARBITER system =====\n" + sysp)
        print("\n===== ARBITER user =====\n" + userp)
        return 0

    v = run_arbiter(str(tpath), model_tier=a.tier, write=not a.no_write)
    ok, probs = validate_verdict(v)
    calls = v["model_attempts"] if v["model_called"] else 0
    tag = ("hard-gate, $0" if v["hard_gate_forced"] else
           f"{calls} paid call(s)" if v["model_called"] else "no model call, $0")
    if ok:
        print(f"OK  {v['ticker']:6} {v['verdict']:7} ({tag}) "
              f"gate={v['regime_gate_check']} corr={v['correlation_check']} "
              f"→ {v['output_path'] or '(not written)'}")
        return 0
    print(f"ERR {v['ticker'] or '?':6} verdict failed validation: {probs}")
    print(f"    → {v['output_path'] or '(not written)'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
