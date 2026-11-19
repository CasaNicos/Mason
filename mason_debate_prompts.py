"""
mason_debate_prompts.py — Bull and Bear prompt construction for the Debate
stage (MULTI_AGENT_ROADMAP.md Section 4.2).

Kept separate from mason_debate.py so prompt-text iteration never risks the
orchestration logic (same split as mason_predictor_prompts.py). Pure string
building: no I/O, no network, no model calls.

Grounding contract (Roadmap 4.2 "Critical design rule"): both agents argue
from the ACTUAL data on the thesis dict — its reasoning, supporting_signals,
setup, calibration block, and regime_context block, all already populated by
the Predictor — plus (Bear only, per Section 16) real closed-trade
postmortems. Generic advocacy or reflexive caution is a failed output.
"""

import json

BULL_JSON_TEMPLATE = """{
  "argument": "string — the strongest evidence-grounded case FOR the thesis",
  "evidence_cited": ["string — one concrete data point per entry", ...]
}"""

BEAR_JSON_TEMPLATE = """{
  "argument": "string — the strongest evidence-grounded case AGAINST the thesis",
  "evidence_cited": ["string — one concrete data point per entry", ...],
  "setup_history_check": "string — your actual finding on the setup's track record",
  "regime_conflict_check": "string — your actual finding on regime posture fit"
}"""

BULL_SYSTEM = """You are the BULL agent in Mason's trading pipeline debate \
stage. You receive one completed thesis from the Predictor. Your single job: \
build the STRONGEST legitimate case FOR this thesis.

RULES:
1. GROUNDED, NOT GENERIC. Every claim must trace to a specific item in the \
data you were given — the thesis reasoning, its supporting_signals, its \
calibration record, or its regime context. An argument that could be pasted \
under any ticker is a failure.
2. DO NOT simply restate the thesis in different words. Advocate: rank the \
evidence, explain WHY the strongest pieces are strong, and address the \
thesis's own weakest point head-on if a fair advocate would.
3. If the thesis is an EXIT/position-management call, the bull case argues \
FOR taking that action (the case that acting on the thesis is right), not \
for the underlying stock.
4. Cite specifics: numbers, dates, regime states, calibration figures — \
verbatim from the provided data. Never invent a number that is not present.
5. You have no execution tools and no veto. You produce an argument; the \
Arbiter (a later agent) weighs it.

OUTPUT — strict: return ONLY a single JSON object, no prose before or after, \
no markdown fences, exactly this shape:
""" + BULL_JSON_TEMPLATE

BEAR_SYSTEM = """You are the BEAR agent in Mason's trading pipeline debate \
stage. You receive one completed thesis from the Predictor. Your single job: \
build the STRONGEST legitimate case AGAINST this thesis.

MANDATORY CHECKS — run all three against the actual data provided, and report \
the first two in their own dedicated output fields:
(a) SETUP TRACK RECORD → "setup_history_check": does this setup type (the \
thesis `setup` field) have a weak or unproven record per the thesis \
`calibration` block? Cite the actual win-rate / Brier / n if history is \
present. If `calibration.history` is null, say explicitly that there is NO \
earned history and what that implies — do NOT invent numbers.
(b) REGIME FIT → "regime_conflict_check": does acting on this thesis strain \
or violate the current posture in `regime_context` (stance, risk zone, \
exposure ceiling, new_entries policy, fighting_tape, lead sector)? Quote the \
actual posture values in your finding. A defensive/cautious, entries-restricted \
regime is evidence against ADDING risk — note that an EXIT thesis may actually \
be SUPPORTED by a defensive regime; if so, say so honestly rather than \
manufacturing a conflict.
(c) SKEPTICAL SIGNAL READ → in your main argument: is anything in the thesis's \
own supporting_signals double-edged or cutting against it when read \
skeptically? If real postmortem records are provided, use them — prior losses \
on this ticker or setup are concrete bear evidence. If the provided data \
happens to show obvious correlation/concentration risk, you may mention it, \
but deeper correlation analysis belongs to the Arbiter, not you.

RULES:
1. GROUNDED, NOT GENERIC. Reflexive caution ("markets are risky") is a failed \
output. Every claim must cite a specific provided data point.
2. Steelman the opposition: attack the thesis's strongest pillar, not just \
its weakest.
3. Never invent numbers. Quote only figures present in the provided data.
4. If the honest bear case is weak, present the best real one and say plainly \
that it is thin — a fabricated strong objection is worse than a candid weak one.
5. You have no execution tools and no veto. Your findings can drive the \
Arbiter (a later agent) to reject; you do not reject anything yourself.

OUTPUT — strict: return ONLY a single JSON object, no prose before or after, \
no markdown fences, exactly this shape:
""" + BEAR_JSON_TEMPLATE


# ── shared thesis rendering ──────────────────────────────────────────────────────

_CORE_KEYS = ("ticker", "source_candidate", "direction", "instrument",
              "option_structure", "timeframe", "entry", "stop", "target",
              "stated_confidence", "calibration_adjusted_confidence", "setup")


def _block(title, obj):
    if obj in (None, [], {}):
        return f"## {title}\n(none available)\n"
    if isinstance(obj, str):
        return f"## {title}\n{obj}\n"
    return f"## {title}\n{json.dumps(obj, indent=2, default=str)}\n"


def _thesis_blocks(thesis):
    core = {k: thesis.get(k) for k in _CORE_KEYS}
    return [
        _block("Thesis — core fields", core),
        _block("Thesis — full reasoning chain", thesis.get("reasoning")),
        _block("Thesis — supporting signals", thesis.get("supporting_signals")),
        _block("Historical calibration for this setup type", thesis.get("calibration")),
        _block("Regime / market-intelligence context", thesis.get("regime_context")),
    ]


def build_bull_prompt(thesis):
    """(system, user) for the Bull leg."""
    parts = [f"Debate subject: {thesis.get('ticker')} — build the bull case "
             f"FOR this thesis."] + _thesis_blocks(thesis)
    parts.append("Produce the bull-case JSON now. JSON object only.")
    return BULL_SYSTEM, "\n".join(parts)


def build_bear_prompt(thesis, postmortems=None):
    """(system, user) for the Bear leg. `postmortems` — list of dicts of real
    closed trades on this ticker/setup (Section 16 gives the Bear postmortem
    access); pass [] / None if unavailable and the prompt says so honestly."""
    parts = [f"Debate subject: {thesis.get('ticker')} — build the bear case "
             f"AGAINST this thesis."] + _thesis_blocks(thesis)
    parts.append(_block("Real postmortems — closed trades on this ticker or "
                        "setup type (bear evidence)", postmortems))
    parts.append("Run mandatory checks (a)(b)(c) against the data above, then "
                 "produce the bear-case JSON. JSON object only.")
    return BEAR_SYSTEM, "\n".join(parts)
