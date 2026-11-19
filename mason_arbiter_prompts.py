"""
mason_arbiter_prompts.py — verdict prompt construction for the Arbiter stage
(MULTI_AGENT_ROADMAP.md Section 4.3).

One prompt (not two like the debate). Kept separate from mason_arbiter.py so
prompt-text iteration never risks the orchestration/safety logic. Pure string
building: no I/O, no network, no model calls.

Safety framing baked into the system prompt (Roadmap Section 7): by the time
the model is consulted, the hard gates (regime, correlation) have ALREADY
passed in code. The model is never asked about — and cannot override — a hard
gate; it judges the debate. Its REVISE power is deliberately mechanical:
propose new values for a small whitelist of bounded thesis fields, nothing
else. The code, not the model, constructs the revised plan.
"""

import json

# The ONLY fields a REVISE may touch (Constraint #6 — mechanical edits only).
REVISABLE_FIELDS = ("entry", "stop", "target", "timeframe")

VERDICT_JSON_TEMPLATE = """{
  "verdict": "APPROVE | REVISE | REJECT",
  "reasoning": "string — grounded in the specific debate, cite actual content",
  "revised_fields": null | {"stop": number, "target": number, "entry": number, "timeframe": "0dte | short_term | swing | position"}
}"""

SYSTEM_PROMPT = """You are the ARBITER in Mason's trading pipeline — the final \
judgment stage. You read a completed Bull/Bear debate transcript about one \
thesis and issue the verdict: APPROVE, REVISE, or REJECT. REJECT halts this \
candidate entirely; APPROVE sends it to risk sizing; REVISE sends it forward \
with bounded mechanical edits.

WHAT ALREADY HAPPENED (do not redo it):
- The deterministic hard gates (regime posture, same-ticker concentration) \
already ran IN CODE and passed — that is the only reason you are being \
consulted at all. You cannot override a hard gate and you will never be shown \
a case where one failed. If the check results below include a "flagged" (not \
failed) concern, weigh it as evidence.
- The debate already re-litigated the trade idea. Judge the ARGUMENTS — do \
not invent a new thesis, new signals, or new reasoning of your own beyond \
evaluating what is in front of you.

HOW TO JUDGE:
1. Which side actually engaged the evidence better? Cite the specific bull \
and bear points that decided it — quote or name actual evidence_cited \
entries, the bear's setup_history_check and regime_conflict_check findings, \
the postmortem records, the calibration numbers. Generic reasoning \
("the bull case seems reasonable") is a failed output.
2. Weigh calibration honestly: an unproven setup (no earned history) argues \
for conservatism, not automatic rejection — but a setup with a genuinely bad \
earned record is a strong reject signal.
3. Playbook conformance: the strategy guide is included below. A thesis that \
contradicts a documented rule should be REJECTED or REVISED, and any policy \
flag listed in the check results must be addressed in your reasoning, never \
ignored.
4. EXIT theses (entry/stop/target all 0 by convention): the question is \
whether acting on the exit is right, not whether the levels are good. REVISE \
rarely makes sense there — it is APPROVE or REJECT.
5. A REJECT on judgment ("the bear's case is simply more convincing") is a \
fully legitimate verdict. So is APPROVE when the bull's case genuinely holds \
up against a real bear effort. Do not split the difference with REVISE to \
avoid deciding.

REVISE — STRICT MECHANICAL BOUNDS:
- You may ONLY propose new values for: entry, stop, target, timeframe. \
Nothing else — not reasoning, not signals, not direction, not instrument, \
not confidence. The pipeline applies your values to a copy of the original \
thesis; anything outside the whitelist is discarded and downgrades your \
verdict to REJECT.
- Only REVISE when a small bounded edit (tighter stop, nearer target, \
shorter timeframe) genuinely converts a reject-worthy plan into an \
approvable one — and say why in reasoning.
- verdict APPROVE or REJECT → revised_fields must be null.

OUTPUT — strict: return ONLY a single JSON object, no prose before or after, \
no markdown fences, exactly this shape:
""" + VERDICT_JSON_TEMPLATE


def _block(title, obj):
    if obj in (None, [], {}):
        return f"## {title}\n(none available)\n"
    if isinstance(obj, str):
        return f"## {title}\n{obj}\n"
    return f"## {title}\n{json.dumps(obj, indent=2, default=str)}\n"


def build_verdict_prompt(transcript, thesis, checks, strategy_guide_text=None):
    """(system, user) for the one Arbiter call.

    transcript  the validated debate_transcript dict (5.3 + extensions)
    thesis      the resolved original thesis dict (5.2 + extensions)
    checks      dict from mason_arbiter.deterministic_checks() — includes the
                FRESH regime gate read, correlation result, postmortems,
                policy flags, and is_exit
    strategy_guide_text  full STRATEGY_GUIDE.md text (fail-soft None)
    """
    thesis_core = {k: thesis.get(k) for k in
                   ("ticker", "direction", "instrument", "option_structure",
                    "timeframe", "entry", "stop", "target",
                    "stated_confidence", "calibration_adjusted_confidence",
                    "setup")}
    note = ""
    if checks.get("is_exit"):
        note = (" — NOTE: this is an EXIT/position-management thesis "
                "(sentinel 0 levels).")
    elif checks.get("is_position_mgmt"):
        note = (" — NOTE: this thesis manages an EXISTING held position "
                "(sourced from a position alert); judge the management "
                "action, and treat any proposed ADD to the position with "
                "the regime posture below in mind.")
    parts = [
        f"Arbitrate: {transcript.get('ticker')} — thesis_ref "
        f"{transcript.get('thesis_ref')}" + note,
        _block("BULL case (verbatim)", transcript.get("bull_case")),
        _block("BEAR case (verbatim)", transcript.get("bear_case")),
        _block("Original thesis — core fields", thesis_core),
        _block("Original thesis — Predictor's reasoning", thesis.get("reasoning")),
        _block("Original thesis — calibration record", thesis.get("calibration")),
        _block("Deterministic check results (already enforced in code — "
               "context, not up for override)", checks.get("summary")),
        _block("CURRENT regime posture (fresh at verdict time — may differ "
               "from what the Predictor/debate saw)", checks.get("gate_snapshot")),
        _block("Real postmortems (closed trades, this ticker/setup)",
               checks.get("postmortems")),
        _block("Policy flags (must be addressed in reasoning if any)",
               checks.get("policy_flags")),
    ]
    if strategy_guide_text:
        parts.append(_block("Strategy playbook (STRATEGY_GUIDE.md, verbatim)",
                            strategy_guide_text))
    parts.append("Issue the verdict JSON now. JSON object only.")
    return SYSTEM_PROMPT, "\n".join(parts)
