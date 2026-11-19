"""
mason_predictor_prompts.py — prompt construction for the Predictor Agent
(MULTI_AGENT_ROADMAP.md Section 4.1).

Kept separate from mason_predictor.py so prompt-text iteration never risks
the orchestration logic. Pure string-building: no I/O, no network, no model
calls — fully unit-testable offline.
"""

import json

# Exact keys the model must return — mirrors Roadmap Section 5.2 minus the
# fields the orchestrator fills itself (source_candidate,
# calibration_adjusted_confidence).
THESIS_JSON_TEMPLATE = """{
  "ticker": "string",
  "direction": "long | short",
  "instrument": "equity | option",
  "option_structure": "long_call | long_put | null",
  "timeframe": "0dte | short_term | swing | position",
  "entry": number,
  "stop": number,
  "target": number,
  "stated_confidence": number 0-100,
  "reasoning": "string — the full chain, not a summary",
  "supporting_signals": ["string", ...]
}"""

SYSTEM_PROMPT = """You are the Predictor Agent in Mason's multi-agent trading \
pipeline — the single genuinely generative step. You receive ONE candidate from \
the Scanner plus regime context and your own historical calibration record, and \
you produce ONE complete, falsifiable thesis.

RULES — non-negotiable:
1. FALSIFIABLE: entry, stop, and target must be concrete numbers a later agent \
can check against real price action. A thesis without a stop is invalid.
2. SINGLE-LEG POLICY (standing mandate): if instrument is "option", \
option_structure must be exactly "long_call" or "long_put" — one leg, one \
ticker, long premium only. No spreads, no multi-leg structures, no short \
premium, ever. If instrument is "equity", option_structure must be null.
3. CALIBRATION-CONDITIONED CONFIDENCE: your stated_confidence must be \
conditioned on the historical calibration record you are given, not freely \
asserted. If the record says this setup type has no earned history, state \
confidence conservatively and say so in your reasoning. If it says confidence \
on this setup has historically overshot, shade yours down and say by how much.
4. A "no-trade" read is allowed and valuable: if the candidate does not \
support a thesis, still return the JSON with your best-fit direction but a \
LOW stated_confidence (< 35) and reasoning explaining why conviction is weak. \
Never invent conviction to fill the schema.
5. Direction "short" on an equity should instead be expressed as instrument \
"option" + option_structure "long_put" when the account cannot short stock — \
note the choice in reasoning.

OUTPUT FORMAT — strict:
Return ONLY a single JSON object, no prose before or after, no markdown code \
fences, matching exactly this shape:
""" + THESIS_JSON_TEMPLATE


def _fmt_block(title, obj):
    if obj is None:
        return f"## {title}\n(unavailable this cycle)\n"
    if isinstance(obj, str):
        return f"## {title}\n{obj}\n"
    return f"## {title}\n{json.dumps(obj, indent=2, default=str)}\n"


def build_user_prompt(item, kind, setup, calibration, regime_context,
                      scan_meta=None):
    """Assemble the per-candidate user prompt.

    item            one entry from scanner_output new_candidates[] or position_alerts[]
    kind            'new_candidate' | 'position_alert'
    setup           mapped setup-type string (mason_predictor_calibration)
    calibration     dict from calibration_for_setup() — MUST be included
                    (Roadmap Section 8 step 1)
    regime_context  dict of regime-gate + intelligence reads
    scan_meta       optional {cycle_id, timestamp, stale, age_note}
    """
    header = (f"Candidate type: {kind.replace('_', ' ').upper()} — "
              f"ticker {item.get('ticker')}, mapped setup type: {setup}")
    if kind == "position_alert":
        header += (f"\nThis is an ALERT on an EXISTING position "
                   f"(trigger: {item.get('trigger')}). Your thesis should answer: "
                   f"hold, exit, or adjust — expressed through the same "
                   f"entry/stop/target schema (for an exit thesis, direction is "
                   f"the direction of the remaining edge, and reasoning must say "
                   f"'exit' explicitly).")
    parts = [
        header,
        _fmt_block("Scanner candidate (verbatim)", item),
        _fmt_block("Historical calibration for this setup type "
                   "(condition your confidence on this)",
                   {"summary": calibration.get("note"),
                    "detail": calibration.get("history"),
                    "overall_brier": calibration.get("brier")}),
        _fmt_block("Regime / market-intelligence context", regime_context),
    ]
    if scan_meta:
        parts.append(_fmt_block("Scan cycle metadata", scan_meta))
    parts.append("Produce the thesis JSON now. JSON object only.")
    return "\n".join(parts)


def build_prompts(item, kind, setup, calibration, regime_context, scan_meta=None):
    """Convenience: returns (system_prompt, user_prompt)."""
    return SYSTEM_PROMPT, build_user_prompt(item, kind, setup, calibration,
                                            regime_context, scan_meta)
