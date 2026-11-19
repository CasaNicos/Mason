"""
mason_predictor_calibration.py — Predictor Agent calibration adapter
(MULTI_AGENT_ROADMAP.md Section 8, steps 1-2).

Bridges the Scanner's candidate vocabulary (flagged_by sources / alert triggers)
to mason_learning.py's setup-type vocabulary, so the Predictor can:
  1. RETRIEVE historical calibration for a candidate's setup type BEFORE the
     model states confidence (Section 8 step 1) — summarized for the prompt.
  2. ADJUST the model's stated_confidence into calibration_adjusted_confidence
     via mason_learning.calibrated_confidence() (Section 8 step 2) — both
     numbers are kept separate so drift stays trackable.

Mapping design (why these strings):
  mason_learning._edge_adj() looks a setup up in track_record()["by_setup"]
  by exact match, then by .lower(). The journal's setup_type values come from
  two writers: mason_ideas pitches (setup names like "VCP", "CANSLIM") and
  manual/synced entries (lowercase like "momentum"). So the mapping below emits
  the mason_ideas-style names where one exists (future pipeline journal entries
  will carry the same name → the loop closes on itself), and relies on the
  .lower() fallback to also match today's hand-logged history (e.g.
  "MOMENTUM" → "momentum", 5 real trades as of this build).

Read-only: never writes the journal, never modifies mason_learning.
"""

import mason_learning as LE

# flagged_by source → setup type (mason_ideas naming convention).
SETUP_BY_SOURCE = {
    "vcp-screener": "VCP",
    "canslim-screener": "CANSLIM",
    "earnings-trade-analyzer": "PEAD",
    "neglected-value": "NEGLECTED-VALUE",
    "theme-detector": "THEME",
    "technical-sweep": "MOMENTUM",
}

# Multi-source candidates take the highest-priority source's setup —
# ordered by evidence strength per STRATEGY_GUIDE.md's strategy ranking.
SOURCE_PRIORITY = ["vcp-screener", "canslim-screener", "earnings-trade-analyzer",
                   "neglected-value", "theme-detector", "technical-sweep"]

# position_alert trigger → setup type. Management-only triggers get
# POSITION-MGMT: no journal history exists for it yet, so
# calibrated_confidence() correctly passes stated confidence through
# unchanged until real outcomes accumulate under that name.
SETUP_BY_TRIGGER = {
    "earnings_surprise": "PEAD",
    "gap": "MOMENTUM",
    "volume_spike": "MOMENTUM",
    "news": "POSITION-MGMT",
    "stop_approaching": "POSITION-MGMT",
    "target_approaching": "POSITION-MGMT",
    "correlation_drift": "POSITION-MGMT",
    "option_expiry": "POSITION-MGMT",
    "adverse_move": "POSITION-MGMT",
}

FALLBACK_SETUP = "UNKNOWN"


def setup_for_item(item, kind):
    """Map one Scanner item to a mason_learning setup string.
    kind: 'new_candidate' | 'position_alert'."""
    if kind == "position_alert":
        return SETUP_BY_TRIGGER.get(item.get("trigger"), FALLBACK_SETUP)
    flagged = item.get("flagged_by") or []
    for src in SOURCE_PRIORITY:
        if src in flagged:
            return SETUP_BY_SOURCE[src]
    return SETUP_BY_SOURCE.get(flagged[0], FALLBACK_SETUP) if flagged else FALLBACK_SETUP


def calibration_for_setup(setup, _tr=None):
    """Section 8 step 1: retrieve historical calibration for a setup type.
    Returns a dict safe to embed in both the prompt and the thesis file.
    Fail-soft: an unreadable journal yields the empty-history shape."""
    try:
        tr = _tr or LE.track_record()
    except Exception as e:
        return {"setup": setup, "history": None, "brier": None,
                "note": f"calibration store unreadable ({e}) — treating as no history"}
    bucket = (tr.get("by_setup") or {}).get(setup) \
        or (tr.get("by_setup") or {}).get((setup or "").lower())
    brier = (tr.get("calibration") or {}).get("brier")
    if not bucket or not bucket.get("n"):
        note = (f"No resolved trade history for setup '{setup}' yet — "
                f"confidence is ASSUMED, not earned. State it conservatively.")
        return {"setup": setup, "history": None, "brier": brier, "note": note}
    tag = "EARNED (sample large enough to trust)" if bucket.get("earned") \
        else f"assumed (only n={bucket['n']})"
    note = (f"Setup '{setup}': {bucket['n']} resolved trades, "
            f"win-rate {bucket.get('win_rate')}, Bayesian-calibrated win-rate "
            f"{bucket.get('calib_win_rate')}, expectancy {bucket.get('expectancy_R')}R "
            f"— {tag}."
            + (f" Overall Brier score {brier} (lower=better)." if brier is not None else ""))
    return {"setup": setup, "history": dict(bucket), "brier": brier, "note": note}


def adjust_confidence(setup, stated_confidence, posture_zone=None, _tr=None):
    """Section 8 step 2: calibration_adjusted_confidence, via the SAME
    Bayesian-shrinkage path mason_ideas already uses. Returns int 0-99,
    or stated_confidence unchanged if it isn't numeric."""
    try:
        return LE.calibrated_confidence(setup, stated_confidence,
                                        posture_zone, _tr=_tr)
    except Exception:
        return stated_confidence


def sizing_factor(setup, posture_zone=None, _tr=None):
    """Pass-through of confidence_sizing_factor for the (later) Risk Sizing
    agent — logged on the thesis so P4 doesn't have to recompute context."""
    try:
        return LE.confidence_sizing_factor(setup, posture_zone, _tr=_tr)
    except Exception:
        return 1.0
