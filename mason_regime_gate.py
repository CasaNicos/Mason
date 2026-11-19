"""
mason_regime_gate.py — Mason's regime GATE: the "should we even be buying, and how
big" filter that sits in front of every idea.

Philosophy (per Nico's north star): posture sets SIZE and confidence, it never
silences an idea. So this gate does NOT block Mason from pitching the best edge —
it outputs (a) how much of the book may be at risk right now (exposure ceiling),
(b) a per-trade risk multiplier, and (c) a new-entries recommendation, so every
pitch is sized and tagged to the tape instead of being one-size-fits-all.

Why it exists: for a small account, avoiding a drawdown beats picking winners.
One avoided 20% drop > three good trades.

Single source of truth, fused from what's already on disk (no API key required):
  - top-risk score + zone   (.posture_cache.json, from mason_market_posture.py)
  - sector regime / cycle    (.posture_cache.json)
  - Buffett valuation zone    (.posture_cache.json, if present)
  - market breadth 0-100      (mason_skills.run_breadth — optional, FMP)

Fixes a real bug in the old mason_ideas.market_state(): it only treated
zones literally named high/severe/red/defensive as "fighting the tape", so an
ORANGE or CRITICAL top-risk reading sailed through. This gate maps the actual
Green/Yellow/Orange/Red/Critical vocabulary.

Public API:
    gate(force_breadth=False) -> dict   (the posture; see _EMPTY for shape)
    render(g=None)            -> str
"""

import json
import time
from datetime import datetime
from pathlib import Path

BASE          = Path(__file__).parent
POSTURE_CACHE = BASE / ".posture_cache.json"

# Zone → (stance, exposure_ceiling_%, per-trade risk multiplier, new-entries policy)
# ceiling = max % of the active book that should be in open risk positions.
ZONE_TABLE = {
    "green":    ("risk-on",   100, 1.30, "yes"),
    "yellow":   ("neutral",    80, 1.00, "selective"),
    "orange":   ("cautious",   55, 0.60, "defined-risk-only"),
    "red":      ("risk-off",   30, 0.40, "adds/hedges-only"),
    "critical": ("defensive",  10, 0.25, "no"),
}
_ENTRY_RANK = ["no", "adds/hedges-only", "defined-risk-only", "selective", "yes"]

_EMPTY = {
    "stance": "unknown", "risk_zone": "Unknown", "composite": None,
    "exposure_ceiling_pct": 60.0, "risk_multiplier": 1.0,
    "new_entries": "selective", "fighting_tape": False,
    "reasons": ["posture data unavailable — defaulting to neutral"],
    "inputs": {}, "line": "Posture unavailable — neutral default", "as_of": None,
}


# ── inputs ───────────────────────────────────────────────────────────────────────

def _read_posture_cache():
    """The cache file can hold trailing/multiple JSON objects — decode the first."""
    try:
        txt = POSTURE_CACHE.read_text(encoding="utf-8").lstrip()
        d, _ = json.JSONDecoder().raw_decode(txt)
        return d
    except Exception:
        return {}


def _breadth_score(force=False):
    try:
        import mason_skills as SK
        b = SK.run_breadth(force=force) if force else SK.run_breadth()
        if b.get("ok") and b.get("data"):
            return (b["data"].get("composite") or {}).get("composite_score")
    except Exception:
        pass
    return None


def _downgrade(policy, notches=1):
    i = _ENTRY_RANK.index(policy) if policy in _ENTRY_RANK else 3
    return _ENTRY_RANK[max(0, i - notches)]


# ── the gate ─────────────────────────────────────────────────────────────────────

def gate(force_breadth=False):
    cache = _read_posture_cache()
    if not cache:
        return dict(_EMPTY)

    top = cache.get("top_risk", {}) or {}
    sec = cache.get("sector", {}) or {}
    buf = cache.get("buffett", {}) or {}

    zone_raw = (top.get("zone") or "").strip()
    zone_key = zone_raw.lower()
    score    = top.get("score")                 # 0-100, higher = MORE top risk
    regime   = (sec.get("regime") or "").strip()
    phase    = (sec.get("cycle_phase") or "").strip()
    buf_zone = (buf.get("zone") or "").strip()

    # Base off the top-risk zone (primary signal). Unknown zone → derive from score.
    if zone_key not in ZONE_TABLE and score is not None:
        zone_key = ("green" if score <= 20 else "yellow" if score <= 40 else
                    "orange" if score <= 60 else "red" if score <= 80 else "critical")
        zone_raw = zone_key.capitalize()
    base = ZONE_TABLE.get(zone_key)
    if not base:
        return dict(_EMPTY)
    stance, ceiling, risk_mult, entries = base

    reasons = [f"Top-risk {zone_raw}" + (f" ({score}/100)" if score is not None else "")]

    breadth = _breadth_score(force=force_breadth)
    fighting = zone_key in ("orange", "red", "critical")

    # ── adjustments ──
    # Breadth confirmation / divergence.
    if breadth is not None:
        if breadth >= 65 and zone_key in ("green", "yellow"):
            ceiling = min(100, ceiling + 10)
            reasons.append(f"breadth strong ({breadth}/100) — broad participation")
        elif breadth < 45:
            ceiling = max(5, ceiling - 10)
            risk_mult = round(risk_mult * 0.85, 2)
            entries = _downgrade(entries)
            fighting = True
            reasons.append(f"breadth weak ({breadth}/100) — thin participation")

    # Defensive sector leadership / late cycle.
    if regime.lower() == "defensive":
        ceiling = max(5, ceiling - 10)
        entries = _downgrade(entries)
        fighting = True
        reasons.append(f"sector regime Defensive ({phase or 'late cycle'}) — money rotating to safety")
    elif regime.lower() == "cyclical" and zone_key in ("green", "yellow"):
        reasons.append(f"cyclical leadership ({phase or 'expansion'}) — risk appetite healthy")

    # Stretched valuation only caps the top, doesn't force defense by itself.
    if buf_zone.lower() in ("danger zone", "extreme"):
        ceiling = min(ceiling, 70)
        reasons.append(f"Buffett indicator {buf_zone} — cap gross exposure")

    # Composite health 0-100 (higher = healthier). Invert top-risk, blend breadth.
    health = None
    if score is not None:
        health = 100 - float(score)
        if breadth is not None:
            health = round((health + breadth) / 2, 1)
        else:
            health = round(health, 1)

    line = (f"{stance.upper()} · {zone_raw} top-risk"
            + (f" {score}/100" if score is not None else "")
            + f" · exposure ≤{ceiling:.0f}% · {risk_mult}× size · entries: {entries}")

    return {
        "stance": stance,
        "risk_zone": zone_raw or "Unknown",
        "composite": health,
        "exposure_ceiling_pct": float(ceiling),
        "risk_multiplier": float(risk_mult),
        "new_entries": entries,
        "fighting_tape": bool(fighting),
        "reasons": reasons,
        "inputs": {"top_risk_score": score, "top_risk_zone": zone_raw,
                   "regime": regime, "cycle_phase": phase,
                   "breadth": breadth, "buffett_zone": buf_zone},
        "line": line,
        "as_of": cache.get("as_of") or datetime.now().strftime("%Y-%m-%d"),
    }


# ── render ───────────────────────────────────────────────────────────────────────

def render(g=None):
    g = g or gate()
    L = [f"REGIME GATE — {g['line']}"]
    if g.get("composite") is not None:
        L.append(f"  health {g['composite']}/100 · fighting tape: {'YES' if g['fighting_tape'] else 'no'}")
    for r in g.get("reasons", []):
        L.append(f"  • {r}")
    return "\n".join(L)


# ── self-test (offline; feeds mocked posture caches) ─────────────────────────────

def _self_test():
    import io, contextlib
    global _read_posture_cache, _breadth_score
    orig_rc, orig_bs = _read_posture_cache, _breadth_score

    cases = {
        "GREEN+broad": (
            {"top_risk": {"zone": "Green", "score": 12},
             "sector": {"regime": "Cyclical", "cycle_phase": "Mid Cycle Expansion"}},
            70, "risk-on", False),    # breadth strong, green → NOT fighting; ceiling bumped to 100
        "ORANGE (the old bug)": (
            {"top_risk": {"zone": "Orange", "score": 52},
             "sector": {"regime": "Neutral", "cycle_phase": "Transitional"}},
            None, "cautious", True),  # MUST be flagged fighting — old code missed this
        "RED+defensive": (
            {"top_risk": {"zone": "Red", "score": 72},
             "sector": {"regime": "Defensive", "cycle_phase": "Late Cycle / Risk-Off"},
             "buffett": {"zone": "Danger Zone"}},
            38, "risk-off", True),
        "CRITICAL": (
            {"top_risk": {"zone": "Critical", "score": 88},
             "sector": {"regime": "Defensive"}},
            20, "defensive", True),
    }
    ok = True
    for name, (cache, breadth, exp_stance, exp_fight) in cases.items():
        _read_posture_cache = (lambda c=cache: c)
        _breadth_score = (lambda force=False, b=breadth: b)
        g = gate()
        flag = "OK" if (g["stance"] == exp_stance and g["fighting_tape"] == exp_fight) else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"  [{flag}] {name:22} → {g['stance']:9} ceil {g['exposure_ceiling_pct']:.0f}% "
              f"{g['risk_multiplier']}× entries={g['new_entries']:18} fighting={g['fighting_tape']}")
    _read_posture_cache, _breadth_score = orig_rc, orig_bs
    print("SELF-TEST", "OK" if ok else "FAILED")
    assert ok


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "selftest"])
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    else:
        print(render())
