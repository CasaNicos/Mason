"""
mason_intelligence.py — Mason's market-intelligence memory: the layer that lets
today's market read build on every read before it, so Mason gets sharper over time.

Where it fits (distinct from the two learning layers that already exist):
  - mason_learning.py      grades individual TRADES (did this VCP/breakout win).
  - mason_memory_store.py  is a flat list of text facts jammed into the prompt.
  - THIS module stores structured MARKET-ANALYSIS SNAPSHOTS (regime, sector map,
    risk gauges) PLUS Mason's written thesis and the one candidate trade, and reads
    them back across time — regime timeline, streak/divergence signals, and a
    self-grading scorecard whose confidence GROWS only as past reads prove out.

Hybrid capture: the hard numbers are pulled automatically from what Mason already
computes each session (.posture_cache.json + mason_regime_gate.gate()); Mason
attaches the narrative (thesis, conviction, candidate trade — stock OR options).

Grading (key-free, uses only data already on disk):
  1. REGIME HOLD  — did the regime + lead sector it called actually persist into
                    the next snapshots? (computed purely from the stored series)
  2. TRADE OUTCOME — if it named a trade, did that trade resolve win/loss in the
                    stock OR options journal? (best-effort link)
Confidence is EARNED: with no graded history the read-confidence factor sits at
1.0 (no opinion). It drifts up only as calls are proven right, via Bayesian
shrinkage so a one-snapshot fluke can't swing it.

Everything degrades gracefully: offline / no cache -> the narrative is still saved
(hard numbers come back None), and no function ever throws on empty history.

Public API:
    capture_snapshot(thesis=None, conviction=None, candidate=None,
                     source="mason", asof=None, force=False)      -> dict
    list_snapshots(limit=None)                                    -> list
    get_snapshot(sid)                                             -> dict | None
    regime_timeline(lookback=None)                                -> dict
    detect_signals(lookback=8)                                    -> dict
    grade_snapshots(min_gap_days=3)                               -> dict
    scorecard()                                                   -> dict
    read_confidence_factor()                                      -> float
    brief()                                                       -> str
"""

import json
import tempfile
import uuid
from datetime import datetime, date
from pathlib import Path

BASE          = Path(__file__).parent
STORE         = BASE / "mason_intelligence.json"
POSTURE_CACHE = BASE / ".posture_cache.json"

# risk zones, worst last -> index doubles as a severity rank
ZONES     = ["green", "yellow", "orange", "red", "critical"]
SHRINK_K  = 5          # virtual prior trials — higher = slower to trust a small sample
PRIOR_ACC = 0.50       # a coin-flip read before any evidence
# fields that are the AUTO hard-number half of a snapshot (refreshed on same-day recapture)
_HARD_KEYS = ("regime", "cycle_phase", "leaders", "laggards", "sector_vs_spy_1m",
              "top_risk_zone", "top_risk_score", "vix", "breadth_pct_above_50ma",
              "distribution_days", "buffett_ratio", "buffett_zone", "posture", "hard_ok")


# ── small helpers ──────────────────────────────────────────────────────────────────

def _zone_rank(z):
    try:
        return ZONES.index((z or "").strip().lower())
    except ValueError:
        return -1


def _bull_bear(d):
    d = (d or "").strip().lower()
    if d in ("long", "buy", "bull", "bullish", "call", "calls"):
        return "bull"
    if d in ("short", "sell", "bear", "bearish", "put", "puts"):
        return "bear"
    return None


def _days_between(a, b):
    """Signed days: (b - a). Unparsable -> 0 so callers degrade, never crash."""
    try:
        return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days
    except Exception:
        return 0


def _shrink(wins, n, k=SHRINK_K, prior=PRIOR_ACC):
    return (wins + prior * k) / (n + k) if (n + k) else prior


def _norm_conf(c):
    if c is None:
        return None
    try:
        c = float(c)
    except Exception:
        return None
    if c <= 1:
        c *= 100
    return int(max(0, min(100, round(c))))


def _clean_candidate(c):
    if not c or not isinstance(c, dict):
        return None
    tk = (c.get("ticker") or "").strip().upper()
    if not tk:
        return None
    return {
        "asset_type": (c.get("asset_type") or "stock").strip().lower(),
        "ticker":     tk,
        "direction":  (c.get("direction") or "").strip().lower() or None,
        "rationale":  (c.get("rationale") or "").strip() or None,
    }


# ── inputs: posture cache + regime gate ────────────────────────────────────────────

def _read_cache():
    """First JSON object in .posture_cache.json (file may hold trailing objects)."""
    try:
        txt = POSTURE_CACHE.read_text(encoding="utf-8").lstrip()
        d, _ = json.JSONDecoder().raw_decode(txt)
        return d
    except Exception:
        return {}


def _read_gate():
    try:
        import mason_regime_gate as RG
        return RG.gate()
    except Exception:
        return {}


def _extract_hard(cache, gate):
    """Pull the machine-comparable numbers out of the posture cache + fused gate."""
    cache = cache or {}
    sec   = cache.get("sector", {}) or {}
    tr    = cache.get("top_risk", {}) or {}
    buf   = cache.get("buffett", {}) or {}
    comps = tr.get("components", {}) or {}
    details = sec.get("details", {}) or {}

    def _cv(key):
        return (comps.get(key, {}) or {}).get("value")

    sector_vs_spy = {}
    for tk, d in details.items():
        v = (d or {}).get("1m_vs_spy")
        if v is not None:
            sector_vs_spy[tk] = v

    g = gate or {}
    return {
        "regime":                 sec.get("regime"),
        "cycle_phase":            sec.get("cycle_phase"),
        "leaders":                list(sec.get("leaders") or [])[:3],
        "laggards":               list(sec.get("laggards") or [])[:3],
        "sector_vs_spy_1m":       sector_vs_spy,
        "top_risk_zone":          tr.get("zone"),
        "top_risk_score":         tr.get("score"),
        "vix":                    _cv("sentiment"),
        "breadth_pct_above_50ma": _cv("breadth"),
        "distribution_days":      _cv("distribution_days"),
        "buffett_ratio":          buf.get("ratio"),
        "buffett_zone":           buf.get("zone"),
        "posture": {
            "stance":               g.get("stance"),
            "risk_zone":            g.get("risk_zone"),
            "composite":            g.get("composite"),
            "exposure_ceiling_pct": g.get("exposure_ceiling_pct"),
            "risk_multiplier":      g.get("risk_multiplier"),
            "new_entries":          g.get("new_entries"),
            "fighting_tape":        g.get("fighting_tape"),
            "line":                 g.get("line"),
        },
        "hard_ok": bool(cache),
        "_as_of":  cache.get("as_of"),
    }


# ── store ───────────────────────────────────────────────────────────────────────────

def _load():
    if STORE.exists():
        try:
            d = json.loads(STORE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("snapshots", [])
                return d
        except Exception:
            pass
    return {"snapshots": [], "updated": None}


def _save(d):
    d["updated"] = datetime.now().isoformat()
    STORE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _snaps(snaps):
    return snaps if snaps is not None else _load()["snapshots"]


def _last_date(snaps):
    ds = [s.get("date") for s in snaps if s.get("date")]
    return max(ds) if ds else None


# ── capture (hybrid: auto numbers + Mason's narrative; dedup 1/day) ──────────────────

def capture_snapshot(thesis=None, conviction=None, candidate=None,
                     source="mason", asof=None, force=False,
                     _cache=None, _gate=None):
    """Save (or same-day merge) one market-analysis snapshot.

    Hard numbers are auto-read from the posture cache + regime gate. thesis /
    conviction / candidate are Mason's read. One snapshot per calendar day: a second
    capture the same day REFRESHES the hard numbers and fills in any narrative given,
    without wiping narrative already saved (so a boot snapshot + an on-demand save
    merge cleanly). force=True appends a separate snapshot anyway.
    """
    cache = _cache if _cache is not None else _read_cache()
    gate  = _gate  if _gate  is not None else _read_gate()
    hard  = _extract_hard(cache, gate)
    day   = str(asof or hard.get("_as_of") or date.today().isoformat())[:10]

    conv = _norm_conf(conviction)
    cand = _clean_candidate(candidate)
    thx  = thesis.strip() if isinstance(thesis, str) and thesis.strip() else None

    store = _load()
    snaps = store["snapshots"]
    existing = None if force else next((s for s in snaps if s.get("date") == day), None)

    if existing:
        for k in _HARD_KEYS:
            existing[k] = hard[k]
        if thx is not None:
            existing["thesis"] = thx
        if conv is not None:
            existing["conviction"] = conv
        if cand is not None:
            existing["candidate"] = cand
        existing["source"]     = source
        existing["updated_at"] = datetime.now().isoformat()
        existing["merged"]     = True
        _save(store)
        return existing

    snap = {
        "id":         uuid.uuid4().hex[:8],
        "date":       day,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "source":     source,
        "thesis":     thx,
        "conviction": conv,
        "candidate":  cand,
        "grade":      None,
    }
    for k in _HARD_KEYS:
        snap[k] = hard[k]
    snaps.append(snap)
    snaps.sort(key=lambda s: s.get("date", ""))
    _save(store)
    return snap


def list_snapshots(limit=None):
    s = sorted(_load()["snapshots"], key=lambda x: x.get("date", ""))
    return s[-limit:] if limit else s


def get_snapshot(sid):
    return next((s for s in _load()["snapshots"] if s.get("id") == sid), None)


# ── feature 1: regime timeline ──────────────────────────────────────────────────────

def regime_timeline(lookback=None, snaps=None):
    """Collapse the snapshot series into regime runs: when the current regime began,
    how long it has held, and every prior transition. Plus the lead-sector streak."""
    snaps = _snaps(snaps)
    if not snaps:
        return {"segments": [], "current": None, "transitions": [],
                "lead_sector": None, "lead_streak": 0, "n": 0, "note": "no snapshots yet"}
    ss = sorted(snaps, key=lambda s: s.get("date", ""))
    if lookback:
        ss = ss[-lookback:]

    segments = []
    for s in ss:
        r = s.get("regime") or "Unknown"
        if segments and segments[-1]["regime"] == r:
            segments[-1]["end"] = s.get("date")
            segments[-1]["snapshots"] += 1
        else:
            segments.append({"regime": r, "start": s.get("date"), "end": s.get("date"), "snapshots": 1})

    transitions = [{"from": segments[i - 1]["regime"], "to": segments[i]["regime"], "on": segments[i]["start"]}
                   for i in range(1, len(segments))]

    lead, streak = None, 0
    for s in reversed(ss):
        l0 = (s.get("leaders") or [None])[0]
        if lead is None:
            lead = l0
        if l0 is not None and l0 == lead:
            streak += 1
        else:
            break

    return {"segments": segments, "current": segments[-1], "transitions": transitions,
            "lead_sector": lead, "lead_streak": streak, "n": len(ss)}


# ── feature 3: streak / divergence signals ──────────────────────────────────────────

def detect_signals(lookback=8, snaps=None):
    """Scan the recent snapshot window for streaks, trend breaks, and divergences."""
    snaps = _snaps(snaps)
    ss = sorted(snaps, key=lambda s: s.get("date", ""))[-max(2, lookback):] if snaps else []
    if len(ss) < 2:
        return {"signals": [], "n": len(ss), "as_of": (ss[-1]["date"] if ss else None),
                "note": "need >= 2 snapshots to compare"}
    sig = []
    last, prev = ss[-1], ss[-2]

    if last.get("regime") and prev.get("regime") and last["regime"] != prev["regime"]:
        sig.append({"type": "regime_flip", "severity": "high",
                    "text": f"Regime flipped {prev['regime']} -> {last['regime']}."})

    rl, rp = _zone_rank(last.get("top_risk_zone")), _zone_rank(prev.get("top_risk_zone"))
    if rl >= 0 and rp >= 0 and rl != rp:
        worse = rl > rp
        sig.append({"type": "risk_zone", "severity": "high" if worse else "info",
                    "text": f"Risk zone {'escalated' if worse else 'eased'} "
                            f"{prev.get('top_risk_zone')} -> {last.get('top_risk_zone')}."})

    tl = regime_timeline(snaps=ss)
    if tl["lead_sector"] and tl["lead_streak"] >= 3:
        sig.append({"type": "leader_streak", "severity": "info",
                    "text": f"{tl['lead_sector']} has led {tl['lead_streak']} snapshots running."})

    bvals = [s.get("breadth_pct_above_50ma") for s in ss if s.get("breadth_pct_above_50ma") is not None]
    if len(bvals) >= 3:
        a, b, c = bvals[-3], bvals[-2], bvals[-1]
        if a > b > c:
            sig.append({"type": "breadth", "severity": "high" if (a - c) >= 8 else "warn",
                        "text": f"Breadth deteriorating: {a:.0f}% -> {c:.0f}% above 50DMA over 3 snapshots."})

    vv = [s.get("vix") for s in ss if s.get("vix") is not None]
    if len(vv) >= 3 and vv[-3] < vv[-2] < vv[-1]:
        sig.append({"type": "vix", "severity": "warn",
                    "text": f"VIX rising 3 snapshots straight: {vv[-3]:.1f} -> {vv[-1]:.1f}."})

    dd = last.get("distribution_days")
    if isinstance(dd, (int, float)) and dd >= 5:
        sig.append({"type": "distribution", "severity": "high" if dd >= 6 else "warn",
                    "text": f"{int(dd)} distribution days in the last 25 sessions — institutional selling."})

    conv = last.get("conviction")
    br   = last.get("breadth_pct_above_50ma")
    ft   = (last.get("posture") or {}).get("fighting_tape")
    if isinstance(conv, (int, float)) and conv >= 65 and ((isinstance(br, (int, float)) and br < 45) or ft):
        sig.append({"type": "divergence", "severity": "warn",
                    "text": f"Conviction high ({int(conv)}) while the tape is weak — size down, don't fight it."})

    return {"signals": sig, "n": len(ss), "as_of": last.get("date")}


# ── feature 2: grade past reads (regime hold + trade outcome) ────────────────────────

def _match_outcome(cand, journal, snap_date, window=(-3, 12)):
    """Find a resolved journal trade (stock or option) that matches this candidate."""
    want = (cand.get("ticker") or "").upper()
    wdir = _bull_bear(cand.get("direction"))
    for j in journal:
        if (j.get("ticker") or "").upper() != want:
            continue
        if wdir and _bull_bear(j.get("direction")) not in (None, wdir):
            continue
        ed = str(j.get("entry_date") or j.get("created_at") or "")[:10]
        if ed and not (window[0] <= _days_between(snap_date, ed) <= window[1]):
            continue
        oc = j.get("outcome")
        if oc in ("win", "loss", "breakeven"):
            return oc
    return None


def _grade_pure(snaps, journal, today=None, min_gap_days=3):
    """Grade every ungraded snapshot old enough to judge. Mutates grades in place."""
    today = today or date.today().isoformat()
    ss = sorted(snaps, key=lambda s: s.get("date", ""))
    graded = pending = 0

    for i, s in enumerate(ss):
        if s.get("grade"):
            continue
        d = s.get("date", "")
        if _days_between(d, today) < min_gap_days:
            pending += 1
            continue

        later = None
        for s2 in ss[i + 1:]:
            if _days_between(d, s2.get("date", "")) >= min_gap_days:
                later = s2
                break

        regime_hold = leader_hold = None
        if later:
            if s.get("regime") and later.get("regime"):
                regime_hold = (s["regime"] == later["regime"])
            l0 = (s.get("leaders") or [None])[0]
            if l0:
                leader_hold = l0 in (later.get("leaders") or [])

        trade_outcome = None
        cand = s.get("candidate") or {}
        if cand.get("ticker"):
            trade_outcome = _match_outcome(cand, journal, d)

        read_parts = [1.0 if regime_hold else 0.0] if regime_hold is not None else []
        if leader_hold is not None:
            read_parts.append(1.0 if leader_hold else 0.0)
        read_score = (sum(read_parts) / len(read_parts)) if read_parts else None

        t_part = {"win": 1.0, "loss": 0.0, "breakeven": 0.5}.get(trade_outcome)

        if read_score is None and t_part is None:
            pending += 1
            continue
        if read_score is not None and t_part is not None:
            score = 0.5 * read_score + 0.5 * t_part
        else:
            score = read_score if read_score is not None else t_part

        s["grade"] = {
            "regime_hold": regime_hold, "leader_hold": leader_hold,
            "trade_outcome": trade_outcome, "score": round(score, 3),
            "correct": score >= 0.5, "graded_on": today,
        }
        graded += 1

    return {"graded": graded, "pending": pending, "total": len(ss)}


def _load_journals():
    """Stock + options trades as one list, each tagged with asset_type.

    Goes through mason_trades / mason_options rather than reading the journals
    directly (they live in mason.db now, not JSON). Both getters return
    active-only rows, so snapshot grading never scores itself against a trade
    that has since been deleted."""
    out = []
    try:
        import mason_trades
        for r in mason_trades.get_all_trades():
            rr = dict(r)
            rr["asset_type"] = "stock"
            out.append(rr)
    except Exception:
        pass
    try:
        import mason_options
        for r in mason_options.get_all_plays():
            rr = dict(r)
            rr["asset_type"] = "option"
            out.append(rr)
    except Exception:
        pass
    return out


def grade_snapshots(min_gap_days=3, today=None):
    store = _load()
    summ = _grade_pure(store["snapshots"], _load_journals(), today=today, min_gap_days=min_gap_days)
    _save(store)
    return summ


# ── scorecard + read-confidence ─────────────────────────────────────────────────────

def scorecard(snaps=None):
    """Turn graded reads into an accuracy + a read-confidence multiplier that GROWS
    only as calls prove out (Bayesian shrinkage toward 0.50)."""
    snaps = _snaps(snaps)
    graded = [s for s in snaps if s.get("grade")]
    n = len(graded)
    if n == 0:
        return {"n_graded": 0, "raw_accuracy": None, "calib_accuracy": 0.5,
                "brier": None, "confidence_factor": 1.0, "earned": False, "by_regime": {},
                "as_of": _last_date(snaps),
                "note": "no graded reads yet — read-confidence neutral (x1.0)"}

    wins  = sum(1 for s in graded if s["grade"].get("correct"))
    raw   = wins / n
    calib = _shrink(wins, n)
    conf  = round(max(0.6, min(1.4, 1.0 + (calib - 0.5))), 3)

    br = [((s.get("conviction") or 50) / 100.0 - (1.0 if s["grade"]["correct"] else 0.0)) ** 2
          for s in graded if s.get("conviction") is not None]
    brier = round(sum(br) / len(br), 3) if br else None

    by_regime = {}
    for r in {(s.get("regime") or "Unknown") for s in graded}:
        g = [s for s in graded if (s.get("regime") or "Unknown") == r]
        w = sum(1 for s in g if s["grade"]["correct"])
        by_regime[r] = {"n": len(g), "wins": w, "accuracy": round(w / len(g), 3)}

    return {"n_graded": n, "raw_accuracy": round(raw, 3), "calib_accuracy": round(calib, 3),
            "brier": brier, "confidence_factor": conf, "earned": n >= SHRINK_K,
            "by_regime": by_regime, "as_of": _last_date(snaps)}


def read_confidence_factor(snaps=None):
    return scorecard(snaps)["confidence_factor"]


# ── human-readable one-screen brief (what Mason quotes) ──────────────────────────────

def brief(snaps=None):
    snaps = _snaps(snaps)
    if not snaps:
        return ("MARKET INTELLIGENCE — no snapshots stored yet. Run a full analysis and "
                "save one to start building the memory.")
    ss   = sorted(snaps, key=lambda s: s.get("date", ""))
    last = ss[-1]
    tl   = regime_timeline(snaps=snaps)
    sc   = scorecard(snaps=snaps)
    sg   = detect_signals(snaps=snaps)

    L = [f"MARKET INTELLIGENCE — {len(ss)} snapshots · latest {last.get('date')}"]
    p = last.get("posture") or {}
    if p.get("line"):
        L.append(f"  Now: {p['line']}")
    cur = tl.get("current") or {}
    if cur:
        line = f"  Regime: {cur.get('regime')} for {cur.get('snapshots')} snapshot(s) since {cur.get('start')}"
        if tl.get("lead_sector"):
            line += f" · {tl['lead_sector']} leading {tl['lead_streak']}x"
        L.append(line)
    if sc["n_graded"]:
        tag = "earned" if sc.get("earned") else "still proving"
        L.append(f"  Track record: {sc['raw_accuracy'] * 100:.0f}% of {sc['n_graded']} graded reads correct "
                 f"(calibrated {sc['calib_accuracy'] * 100:.0f}%) · read-confidence x{sc['confidence_factor']} ({tag})")
    else:
        L.append("  Track record: no graded reads yet — read-confidence neutral (x1.0)")
    if sg["signals"]:
        L.append("  Signals:")
        for x in sg["signals"][:6]:
            L.append(f"    - [{x['severity']}] {x['text']}")
    if last.get("thesis"):
        t = last["thesis"]
        L.append(f"  Last thesis: {t[:400]}{'…' if len(t) > 400 else ''}")
    return "\n".join(L)


# ── self-test (offline; synthetic snapshots + journal, never touches real data) ──────

def _mk(day, regime, leaders, breadth, zone, vix=18.0, dist=3,
        conviction=None, candidate=None, grade=None, laggards=None):
    return {"id": uuid.uuid4().hex[:8], "date": day, "regime": regime,
            "leaders": leaders, "laggards": laggards or [], "breadth_pct_above_50ma": breadth,
            "top_risk_zone": zone, "vix": vix, "distribution_days": dist,
            "conviction": conviction, "candidate": candidate, "grade": grade,
            "posture": {"fighting_tape": zone in ("orange", "red", "critical"),
                        "line": f"{regime} | Risk: {zone}"}}


def _self_test():
    global STORE
    # 1) hard-number extraction off the REAL posture-cache shape
    cache = {"sector": {"regime": "Defensive", "cycle_phase": "Late Cycle / Risk-Off",
                        "leaders": ["XLE", "XLV", "VNQ"], "laggards": ["XLK", "XLB", "XLY"],
                        "details": {"XLE": {"1m_vs_spy": 8.99}, "XLK": {"1m_vs_spy": -6.46}}},
             "top_risk": {"zone": "Orange", "score": 52,
                          "components": {"distribution_days": {"value": 5},
                                         "breadth": {"value": 63.6}, "sentiment": {"value": 17.9}}},
             "buffett": {"ratio": 234.3, "zone": "Extreme"}, "as_of": "2026-07-21"}
    gate = {"stance": "cautious", "risk_zone": "Orange", "composite": 48.0,
            "exposure_ceiling_pct": 55.0, "risk_multiplier": 0.6, "new_entries": "defined-risk-only",
            "fighting_tape": True, "line": "CAUTIOUS · Orange top-risk 52/100"}
    h = _extract_hard(cache, gate)
    assert h["regime"] == "Defensive" and h["leaders"][0] == "XLE"
    assert h["vix"] == 17.9 and h["distribution_days"] == 5 and h["buffett_ratio"] == 234.3
    assert h["sector_vs_spy_1m"]["XLE"] == 8.99

    # 2) capture + same-day merge + force, against a throwaway store
    STORE = Path(tempfile.gettempdir()) / ".mason_intel_selftest.json"
    try:
        if STORE.exists():
            try:
                STORE.unlink()
            except Exception:
                pass
        s1 = capture_snapshot(thesis="Defensive rotation; energy leads.", conviction=62,
                              candidate={"asset_type": "stock", "ticker": "xlv", "direction": "long"},
                              _cache=cache, _gate=gate)
        assert s1["regime"] == "Defensive" and s1["conviction"] == 62
        assert s1["candidate"]["ticker"] == "XLV" and s1["candidate"]["asset_type"] == "stock"
        # boot recapture same day: refreshes numbers, MUST NOT wipe the thesis
        s2 = capture_snapshot(source="boot", _cache={**cache, "top_risk": {"zone": "Red", "score": 70,
                              "components": {"breadth": {"value": 40}}}}, _gate=gate)
        assert s2["id"] == s1["id"] and s2.get("merged") is True
        assert s2["thesis"] == "Defensive rotation; energy leads."   # narrative preserved
        assert s2["top_risk_zone"] == "Red" and s2["breadth_pct_above_50ma"] == 40  # numbers refreshed
        assert len(_load()["snapshots"]) == 1                         # still one snapshot for the day
        s3 = capture_snapshot(source="mason", asof="2026-07-21", force=True, _cache=cache, _gate=gate)
        assert len(_load()["snapshots"]) == 2                         # force appends
    finally:
        try:
            STORE.unlink()
        except Exception:
            pass

    # 3) regime timeline: two runs + one transition
    series = [_mk("2026-07-01", "Neutral", ["XLK"], 70, "yellow"),
              _mk("2026-07-02", "Neutral", ["XLK"], 68, "yellow"),
              _mk("2026-07-03", "Defensive", ["XLE"], 66, "orange"),
              _mk("2026-07-04", "Defensive", ["XLE"], 62, "orange"),
              _mk("2026-07-05", "Defensive", ["XLE"], 58, "orange")]
    tl = regime_timeline(snaps=series)
    assert len(tl["segments"]) == 2 and tl["current"]["regime"] == "Defensive"
    assert tl["current"]["snapshots"] == 3 and len(tl["transitions"]) == 1
    assert tl["lead_sector"] == "XLE" and tl["lead_streak"] == 3

    # 4) signals: leader streak + breadth downtrend fire; regime flip does NOT (last two same)
    sg = detect_signals(snaps=series)
    kinds = {x["type"] for x in sg["signals"]}
    assert "leader_streak" in kinds and "breadth" in kinds and "regime_flip" not in kinds

    # 5) grading + scorecard: good regime call + a winning trade -> correct; confidence > 1.0
    win_series = [
        _mk("2026-06-01", "Defensive", ["XLE"], 60, "orange", conviction=65,
            candidate={"asset_type": "stock", "ticker": "XLV", "direction": "long"}),
        _mk("2026-06-08", "Defensive", ["XLE"], 59, "orange"),   # later snap: regime held, XLE still leads
    ]
    journal = [{"ticker": "XLV", "direction": "long", "entry_date": "2026-06-02", "outcome": "win"}]
    summ = _grade_pure(win_series, journal, today="2026-06-20")
    assert summ["graded"] == 1
    g0 = win_series[0]["grade"]
    assert g0["regime_hold"] is True and g0["trade_outcome"] == "win" and g0["correct"] is True
    sc = scorecard(snaps=win_series)
    assert sc["n_graded"] == 1 and sc["confidence_factor"] > 1.0

    # a wrong call (regime broke, trade lost) drags confidence below 1.0
    bad_series = [
        _mk("2026-06-01", "Cyclical", ["XLK"], 70, "green", conviction=70,
            candidate={"asset_type": "option", "ticker": "AMD", "direction": "bullish"}),
        _mk("2026-06-08", "Defensive", ["XLE"], 45, "red"),
    ]
    bad_journal = [{"ticker": "AMD", "direction": "bullish", "entry_date": "2026-06-02", "outcome": "loss"}]
    _grade_pure(bad_series, bad_journal, today="2026-06-20")
    assert bad_series[0]["grade"]["correct"] is False
    assert scorecard(snaps=bad_series)["confidence_factor"] < 1.0

    # 6) shrinkage keeps a 1/1 fluke from swinging confidence hard
    assert abs(_shrink(1, 1) - 0.583) < 0.01

    print("SELF-TEST OK")
    print()
    print(brief(snaps=series))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="brief",
                    choices=["brief", "selftest", "timeline", "signals", "grade", "scorecard"])
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    elif a.cmd == "timeline":
        print(json.dumps(regime_timeline(), indent=2, default=str))
    elif a.cmd == "signals":
        print(json.dumps(detect_signals(), indent=2, default=str))
    elif a.cmd == "grade":
        print(json.dumps(grade_snapshots(), indent=2, default=str))
    elif a.cmd == "scorecard":
        print(json.dumps(scorecard(), indent=2, default=str))
    else:
        print(brief())
