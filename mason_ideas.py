"""
mason_ideas.py — Phase 3: Mason's aggressive, skill-powered idea engine.

Mason's job is to make money. This module hunts for setups using the vendored
trading skills, sizes them against Nico's actual book, and pitches the best
edge — ALWAYS. Market posture/breadth never silences an idea; it modulates the
SIZE and confidence, and flags when a setup is fighting the tape.

Pipeline:
  1. market_state()  — breadth (skill) + posture (cache) + market-top → stance,
                        an aggression multiplier, and a fighting_tape flag.
  2. hunt()          — run VCP (and optionally CANSLIM), parse candidates,
                        drop names already in the book, rank by score, size each
                        via position-sizer, build ranked PlayIdea pitches, log.

Every pitch carries thesis + entry/stop/target + share size + risk + confidence
+ a posture tag, and is logged to mason_ideas_journal.json for the track record.

Degrades gracefully: if the skills can't run (no key/network), market_state
falls back to the cached posture and hunt() returns an empty idea list with a
clear note — never raises.
"""

import json
import time
from datetime import datetime
from pathlib import Path

import mason_skills as SK
try:
    import mason_neglected_value as NV   # "undervalued + underreported" sleeve
except Exception:
    NV = None
try:
    import mason_regime_gate as RG       # regime gate: sizes/gates every idea to the tape
except Exception:
    RG = None
try:
    import mason_learning as LE          # learning loop: confidence calibrated from realized results
except Exception:
    LE = None

BASE          = Path(__file__).parent
IDEAS_JOURNAL = BASE / "mason_ideas_journal.json"
POSTURE_CACHE = BASE / ".posture_cache.json"

# Aggression dial (Nico wants aggressive; small account → meaningful but capped risk)
BASE_RISK_PCT   = 1.5    # % of the active account risked per idea, baseline
MAX_RISK_PCT    = 2.5    # ceiling when the tape is clearly with us
MIN_RISK_PCT    = 0.6    # floor when fighting the tape (still pitch, just small)
MAX_POSITION_PCT = 30.0  # never let one idea exceed this much of the book
TARGET_R         = 2.5   # reward:risk target multiple for breakout entries


# ── book context ────────────────────────────────────────────────────────────────

def _account_size():
    # Mason's strategy engine trades the ALPACA PAPER account — size off its live
    # equity, NOT a personal book. Falls back to a safe default only if
    # Alpaca is unreachable.
    try:
        import alpaca_integration as AL
        eq = float((AL.get_account() or {}).get("equity") or 0)
        if eq > 0:
            return round(eq, 2)
    except Exception:
        pass
    return 1000.0


def _holdings():
    # The book Mason trades is the ALPACA PAPER account — dedup new ideas against
    # live paper positions (+ Mason's own open option/trade plays), not a personal book.
    s = set()
    try:
        import alpaca_integration as AL
        for p in (AL.get_positions() or []):
            t = (p.get("ticker") or "").upper()
            if t:
                s.add(t)
    except Exception:
        pass
    for mod, fn in (("mason_options", "get_open_plays"), ("mason_trades", "get_open_trades")):
        try:
            m = __import__(mod)
            for x in getattr(m, fn)():
                s.add((x.get("ticker") or "").upper())
        except Exception:
            pass
    return s


def _posture():
    try:
        txt = POSTURE_CACHE.read_text(encoding="utf-8").lstrip()
        d, _ = json.JSONDecoder().raw_decode(txt)
        tr, sec = d.get("top_risk", {}) or {}, d.get("sector", {}) or {}
        return {
            "zone":   tr.get("zone") or sec.get("regime", "unknown"),
            "score":  tr.get("score") if tr.get("score") is not None else sec.get("regime_score"),
            "regime": sec.get("regime", ""),
            "phase":  sec.get("cycle_phase", ""),
        }
    except Exception:
        return None


# ── market state → aggression ────────────────────────────────────────────────────

def _gate_state():
    """Build market_state from the unified regime gate (preferred). Returns None on
    failure so the legacy breadth+posture path below can take over."""
    if RG is None:
        return None
    try:
        g = RG.gate()
    except Exception:
        return None
    if not g or g.get("stance") in (None, "unknown"):
        return None
    return {
        "stance":            g["stance"],
        "aggression":        g["risk_multiplier"],
        "fighting_tape":     g["fighting_tape"],
        "breadth_score":     (g.get("inputs") or {}).get("breadth"),
        "breadth_zone":      None,
        "exposure_ceiling_pct": g.get("exposure_ceiling_pct"),
        "new_entries":       g.get("new_entries"),
        "exposure_guidance": g.get("new_entries"),
        "posture":           {"zone": g.get("risk_zone"), "score": (g.get("inputs") or {}).get("top_risk_score"),
                              "regime": (g.get("inputs") or {}).get("regime"),
                              "phase": (g.get("inputs") or {}).get("cycle_phase")},
        "reasons":           g.get("reasons", []),
        "note":              g.get("line", ""),
    }


def market_state():
    """Unified market posture via the regime gate (falls back to breadth+posture)."""
    gs = _gate_state()
    if gs is not None:
        return gs

    breadth_score = None
    breadth_zone = exposure = None
    b = SK.run_breadth()
    if b.get("ok") and b.get("data"):
        comp = (b["data"].get("composite") or {})
        breadth_score = comp.get("composite_score")
        breadth_zone  = comp.get("zone")
        exposure      = comp.get("exposure_guidance")

    post = _posture() or {}
    pzone = str(post.get("zone", "")).lower()
    regime = str(post.get("regime", "")).lower()

    # Fighting the tape if breadth is weak OR posture is defensive/elevated-risk.
    weak_breadth = (breadth_score is not None and breadth_score < 45)
    weak_posture = pzone in ("high", "severe", "red", "defensive") or regime == "defensive"
    fighting = bool(weak_breadth or weak_posture)

    strong = (breadth_score is not None and breadth_score >= 65) and not weak_posture

    if strong:
        stance, aggr = "risk-on", 1.3
    elif fighting:
        stance, aggr = "risk-off", 0.4
    else:
        stance, aggr = "neutral", 1.0

    bits = []
    if breadth_score is not None:
        bits.append(f"breadth {breadth_score}/100 ({breadth_zone})")
    if post.get("zone"):
        bits.append(f"posture {post['zone']}" + (f" {post['score']}/100" if post.get("score") is not None else ""))
    note = ", ".join(bits) or "market state unavailable"

    return {
        "stance":        stance,
        "aggression":    aggr,
        "fighting_tape": fighting,
        "breadth_score": breadth_score,
        "breadth_zone":  breadth_zone,
        "exposure_guidance": exposure,
        "posture":       post,
        "note":          note,
    }


def _risk_pct(state):
    pct = BASE_RISK_PCT * state["aggression"]
    return max(MIN_RISK_PCT, min(MAX_RISK_PCT, round(pct, 2)))


# ── candidate parsing ─────────────────────────────────────────────────────────────

def _vcp_candidates(limit=12):
    r = SK.run_vcp()
    if not (r.get("ok") and r.get("data")):
        return [], r.get("error")
    out = []
    for c in (r["data"].get("results") or [])[:limit]:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        entry = c.get("price") or c.get("pivot_price")
        stop  = c.get("stop_loss_price") or c.get("stop")
        out.append({
            "ticker": sym,
            "setup":  "VCP",
            "score":  c.get("composite_score", 0),
            "state":  c.get("execution_state", ""),
            "entry":  entry,
            "pivot":  c.get("pivot_price"),
            "stop":   stop,
            "guidance": c.get("guidance", ""),
            "valid":  c.get("valid_vcp", False),
            "source": "vcp-screener",
        })
    return out, None


def _canslim_candidates(limit=8):
    r = SK.run_canslim()
    if not (r.get("ok") and r.get("data")):
        return []
    rows = r["data"].get("results") or r["data"].get("candidates") or []
    out = []
    for c in rows[:limit]:
        sym = (c.get("symbol") or c.get("ticker") or "").upper()
        if not sym:
            continue
        out.append({
            "ticker": sym, "setup": "CANSLIM", "score": c.get("composite_score", 0),
            "state": "growth-leader", "entry": c.get("price"), "pivot": None,
            "stop": None, "guidance": c.get("guidance", ""),
            "valid": c.get("passes_all", False), "source": "canslim-screener",
        })
    return out


def _neglected_candidates(limit=5):
    """Undervalued + underreported names (neglected-firm effect). Slow/weekly —
    pulls from the cached screen, never blocks the breakout hunt."""
    if NV is None:
        return []
    try:
        return NV.candidates(limit=limit)
    except Exception:
        return []


# ── the hunt ─────────────────────────────────────────────────────────────────────

def _build_pitch(c, state, account, risk_pct):
    setup = c.get("setup", "")
    zone  = (state.get("posture") or {}).get("zone")
    # Press setups that have earned it, fade those that haven't (within posture caps).
    if LE is not None:
        try:
            risk_pct = max(MIN_RISK_PCT, min(MAX_RISK_PCT,
                          round(risk_pct * LE.confidence_sizing_factor(setup, zone), 2)))
        except Exception:
            pass
    entry = c.get("entry")
    stop  = c.get("stop")
    # If the screener didn't hand us a stop, derive a defined-risk one below the pivot/entry.
    if entry and not stop:
        stop = round(float(entry) * 0.92, 2)        # ~8% defined risk fallback
    sized = None
    shares = risk_dollars = position_value = None
    if entry and stop and float(entry) > float(stop):
        s = SK.size_position(account, entry, stop, risk_pct=risk_pct,
                             max_pos_pct=MAX_POSITION_PCT)
        if s.get("ok"):
            sized = s["data"]
            shares         = sized.get("final_recommended_shares")
            risk_dollars   = sized.get("final_risk_dollars")
            position_value = sized.get("final_position_value")
    target = None
    rps = None
    note = ""
    if entry and stop and float(entry) > float(stop):
        rps = float(entry) - float(stop)
        target = round(float(entry) + rps * TARGET_R, 2)
        # Small account: if full-share size rounds to zero, suggest the fractional/
        # defined-risk-options route (Nico's edge is spreads, not 200-share blocks).
        if (shares is None or shares < 1):
            budget = account * risk_pct / 100.0
            frac = round(budget / rps, 2) if rps else 0
            note = (f"<1 full share at ${account:,.0f} — ~{frac} fractional, "
                    f"or a defined-risk call spread above ${entry}")

    raw_score = int(c.get("score", 0))
    conf = raw_score
    if LE is not None:
        try:
            conf = LE.calibrated_confidence(setup, raw_score, zone)
        except Exception:
            conf = raw_score
    tag = ("posture-OFF (fighting the tape — half size)" if state["fighting_tape"]
           else f"posture-ON ({state['stance']})")
    thesis = (f"{c['setup']} {c.get('state','')} at {conf}/100. {c.get('guidance','')}").strip()

    return {
        "ticker": c["ticker"], "setup": c["setup"], "source": c["source"],
        "score": conf, "confidence": conf, "raw_score": raw_score,
        "calibrated": conf != raw_score, "state": c.get("state", ""),
        "entry": entry, "stop": stop, "target": target,
        "shares": shares, "position_value": position_value, "risk_dollars": risk_dollars,
        "risk_pct_used": risk_pct, "posture_tag": tag, "thesis": thesis, "note": note,
        "ts": datetime.now().isoformat(),
    }


def hunt(max_ideas=3, include_canslim=False, include_neglected=False):
    state = market_state()
    account = _account_size()
    held = _holdings()
    risk_pct = _risk_pct(state)

    cands, err = _vcp_candidates()
    if include_canslim:
        cands += _canslim_candidates()
    if include_neglected:
        cands += _neglected_candidates()

    # drop names already in the book, rank by score, keep best
    seen, ranked = set(), []
    for c in sorted(cands, key=lambda x: x.get("score", 0), reverse=True):
        t = c["ticker"]
        if t in held or t in seen:
            continue
        seen.add(t)
        ranked.append(c)

    ideas = [_build_pitch(c, state, account, risk_pct) for c in ranked[:max_ideas]]
    if ideas:
        _log_ideas(ideas, state)

    note = state["note"]
    if not ideas and err:
        note += f" · screener: {err}"
    return {
        "state": state, "account": account, "risk_pct": risk_pct,
        "ideas": ideas, "note": note,
        "ts": datetime.now().isoformat(),
    }


# ── journal ──────────────────────────────────────────────────────────────────────

def _log_ideas(ideas, state):
    try:
        log = []
        if IDEAS_JOURNAL.exists():
            log = json.loads(IDEAS_JOURNAL.read_text(encoding="utf-8"))
        log.append({"ts": datetime.now().isoformat(), "stance": state["stance"],
                    "fighting_tape": state["fighting_tape"],
                    "ideas": [{"ticker": i["ticker"], "setup": i["setup"],
                               "score": i["score"], "entry": i["entry"],
                               "stop": i["stop"], "target": i["target"]} for i in ideas]})
        if len(log) > 500:
            log = log[-400:]
        IDEAS_JOURNAL.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


# ── rendering (for the tool + brief) ─────────────────────────────────────────────

def last_ideas(max_age_hours=24):
    """Most recent logged ideas (for the brief — avoids running a slow screener inline)."""
    try:
        log = json.loads(IDEAS_JOURNAL.read_text(encoding="utf-8"))
        if not log:
            return []
        last = log[-1]
        from datetime import datetime as _dt
        age = (_dt.now() - _dt.fromisoformat(last["ts"])).total_seconds() / 3600.0
        if age > max_age_hours:
            return []
        return last.get("ideas", [])
    except Exception:
        return []


def render_pitches(result=None):
    r = result or hunt()
    st = r["state"]
    L = [f"Market: {st['note']} → {st['stance'].upper()}"
         + ("  [FIGHTING THE TAPE]" if st["fighting_tape"] else "")]
    if st.get("exposure_ceiling_pct") is not None:
        L.append(f"Posture: exposure ≤{st['exposure_ceiling_pct']:.0f}% of book "
                 f"· new entries: {st.get('new_entries','?')}")
    if not r["ideas"]:
        L.append("No setups cleared the screen right now. "
                 + ("Tape's hostile — Mason's keeping the powder dry." if st["fighting_tape"]
                    else "Nothing with an edge worth your risk."))
        if r.get("note"):
            L.append(f"({r['note']})")
        return "\n".join(L)
    L.append(f"Account ${r['account']:,.0f} · risking {r['risk_pct']}%/idea\n")
    for i, p in enumerate(r["ideas"], 1):
        sh = f"{p['shares']} sh" if p["shares"] else "size n/a"
        rd = f"${p['risk_dollars']:.0f} risk" if p["risk_dollars"] else ""
        L.append(f"{i}. {p['ticker']} — {p['setup']} ({p['confidence']}/100) · {p['posture_tag']}")
        L.append(f"   entry ${p['entry']} · stop ${p['stop']} · target ${p['target']} · {sh} {rd}")
        L.append(f"   {p['thesis']}")
        if p.get("note"):
            L.append(f"   ↳ {p['note']}")
    return "\n".join(L)


if __name__ == "__main__":
    print(render_pitches())
