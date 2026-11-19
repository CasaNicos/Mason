"""
mason_learning.py — Mason's self-learning loop: the part that makes future orders
better than prior ones.

The cycle:
  1. PREDICT   — every executed paper trade in trades_journal.json is a falsifiable
                 prediction (entry / stop / target / setup_type / confidence).
  2. RESOLVE   — resolve_open() walks real daily price action since entry and marks
                 each open trade target_hit / stop_hit / timeout, with NO human
                 scoring. Objective outcomes via mason_trades.close_trade().
  3. CALIBRATE — track_record() turns resolved outcomes into a realized edge per
                 setup type, using Bayesian shrinkage so a 1-2 trade fluke doesn't
                 swing anything; confidence is EARNED as the sample grows.
  4. FEED BACK — calibrated_confidence() / confidence_sizing_factor() let the idea
                 engine raise conviction (and size) on setups that have actually
                 worked, and quietly fade the ones that haven't.

Growing confidence by design: with zero history a setup's calibrated win-rate sits
at the 0.50 prior (confidence == the screener's raw read — "assumed"). As real
results accumulate, the number moves toward what that setup actually delivers for
Mason ("earned"). That's the whole point.

Everything degrades gracefully: no price data → trades stay open, never mis-resolved.

Public API:
    resolve_open(price_fn=None, do_timeout=True)        -> dict
    track_record(trades=None)                           -> dict
    calibrated_confidence(setup, raw_score, posture=None)-> int 0-99
    confidence_sizing_factor(setup, posture=None)        -> float
    report()                                            -> str
"""

import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import requests
except Exception:
    requests = None

BASE = Path(__file__).parent
FMP_BASE = "https://financialmodelingprep.com"

# ── calibration knobs ────────────────────────────────────────────────────────────
PRIOR_WIN   = 0.50      # base rate before any evidence
SHRINK_K    = 6         # how many "virtual" prior trials — higher = slower to trust small samples
ADJ_FLOOR, ADJ_CEIL = 0.60, 1.40     # bounds on the confidence multiplier
R_CLAMP     = (-1.5, 5.0)            # clamp realized R per trade so outliers don't distort means

# Trading-day budget per horizon → when to time a trade out if neither stop nor target hit.
HORIZON_DAYS = {"day": 2, "swing": 25, "position": 70, "long-term": 180, "longterm": 180}
DEFAULT_TIMEOUT_DAYS = 45


# ── config / price data ──────────────────────────────────────────────────────────

def _cfg():
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmp_key():
    # B1b: env FMP_API_KEY > secrets file > config. NOTE (RULINGS #24.2): this used to be
    # config-first-then-env; it is env-first now, on purpose.
    import mason_secrets as _S
    return _S.get("FMP_API_KEY", "", cfg=_cfg())


def _price_history(symbol, start_date):
    """Daily OHLC since start_date (ascending). Returns list of dicts or None offline."""
    if requests is None:
        return None
    key = _fmp_key()
    if not key:
        return None
    try:
        r = requests.get(f"{FMP_BASE}/api/v3/historical-price-full/{symbol}",
                         params={"from": start_date, "apikey": key}, timeout=12)
        if r.status_code != 200:
            return None
        hist = (r.json() or {}).get("historical") or []
        bars = [{"date": h["date"], "high": float(h["high"]), "low": float(h["low"]),
                 "close": float(h["close"])} for h in hist if h.get("date")]
        bars.sort(key=lambda b: b["date"])     # FMP returns newest-first
        return bars
    except Exception:
        return None


# ── outcome detection (pure — unit-tested without touching the journal) ──────────

def _detect_exit(trade, bars, do_timeout=True):
    """Given a trade and ascending daily bars AFTER entry, return (exit_price, reason)
    or None if still open. Conservative: if stop and target are both touched in the
    same session, assume the stop filled first (worst case)."""
    entry_dt   = str(trade.get("entry_date") or trade.get("created_at", ""))[:10]
    direction  = (trade.get("direction") or "long").lower()
    stop       = trade.get("stop_loss")
    target     = trade.get("target_price")
    try:
        stop = float(stop) if stop is not None else None
        target = float(target) if target is not None else None
    except Exception:
        stop = target = None

    fwd = [b for b in bars if b["date"] > entry_dt]
    for b in fwd:
        hi, lo = b["high"], b["low"]
        if direction == "long":
            hit_stop   = stop is not None and lo <= stop
            hit_target = target is not None and hi >= target
        else:  # short
            hit_stop   = stop is not None and hi >= stop
            hit_target = target is not None and lo <= target
        if hit_stop:                       # stop-first on ambiguous days
            return (stop, "stop_hit")
        if hit_target:
            return (target, "target_hit")

    # Neither hit — time it out if it's overstayed its horizon.
    if do_timeout and fwd:
        horizon = (trade.get("time_horizon") or "").lower()
        budget = HORIZON_DAYS.get(horizon, DEFAULT_TIMEOUT_DAYS)
        if len(fwd) >= budget:
            return (fwd[-1]["close"], "timeout")
    return None


# ── resolve open trades automatically ────────────────────────────────────────────

def resolve_open(price_fn=None, do_timeout=True):
    """Auto-close any open paper trades whose stop/target was hit (or that timed out)."""
    import mason_trades as MT
    price_fn = price_fn or _price_history
    resolved, still_open, no_data = [], [], []

    for t in MT.get_open_trades():
        tk = (t.get("ticker") or "").upper()
        try:                       # review fix 2026-11-19: a row held in ANOTHER account (mason_execution's one shared
            import mason_execution  # rule; today the 1-share NVDA row of the old brokerage account) is not a paper
            if mason_execution.non_alpaca_marker(t) is not None:   # trade: never booked closed at its old stop here
                still_open.append(tk)
                continue
        except Exception:
            pass
        start = str(t.get("entry_date") or t.get("created_at", date.today().isoformat()))[:10]
        bars = price_fn(tk, start)
        if not bars:
            no_data.append(tk)
            continue
        ex = _detect_exit(t, bars, do_timeout=do_timeout)
        if ex is None:
            still_open.append(tk)
            continue
        exit_price, reason = ex
        review = f"Auto-resolved by mason_learning on {date.today().isoformat()} ({reason})."
        try:
            MT.close_trade(t["id"], round(float(exit_price), 4), reason, review)
            resolved.append({"ticker": tk, "reason": reason, "exit": round(float(exit_price), 4)})
        except Exception as e:
            still_open.append(f"{tk}(err:{e})")

    note = f"resolved {len(resolved)}, still open {len(still_open)}"
    if no_data:
        note += f", no price data for {len(no_data)}"
    return {"resolved": resolved, "still_open": still_open, "no_data": no_data, "note": note}


# ── calibration math ──────────────────────────────────────────────────────────────

def _calib_winrate(wins, n):
    """Shrink the empirical win-rate toward the prior — confidence is earned, not assumed."""
    return (wins + PRIOR_WIN * SHRINK_K) / (n + SHRINK_K) if (n + SHRINK_K) else PRIOR_WIN


def _realized_R(t):
    """Realized reward multiple = P&L / dollars risked. Clamped for stability."""
    pnl = t.get("actual_pnl")
    rps = t.get("risk_per_share")
    sh  = t.get("shares") or 0
    try:
        risk = float(rps) * float(sh)
        if risk > 0 and pnl is not None:
            r = float(pnl) / risk
            return max(R_CLAMP[0], min(R_CLAMP[1], r))
    except Exception:
        pass
    # Fall back to outcome sign if we can't compute R.
    o = t.get("outcome")
    return 1.0 if o == "win" else (-1.0 if o == "loss" else 0.0)


def _bucket(trades):
    n = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    rs = [_realized_R(t) for t in trades]
    expectancy = round(sum(rs) / len(rs), 2) if rs else 0.0
    cw = _calib_winrate(wins, n)
    return {
        "n": n, "wins": wins, "losses": losses,
        "win_rate": round(wins / n, 3) if n else None,
        "calib_win_rate": round(cw, 3),
        "expectancy_R": expectancy,
        "earned": n >= SHRINK_K,            # enough sample to trust the edge
    }


def track_record(trades=None):
    if trades is None:
        try:
            import mason_trades as MT
            trades = MT.get_all_trades()
        except Exception:
            trades = []
    closed = [t for t in trades if t.get("outcome") in ("win", "loss", "breakeven")]

    by_setup = {}
    setups = {(t.get("setup_type") or "unknown") for t in closed}
    for s in setups:
        by_setup[s] = _bucket([t for t in closed if (t.get("setup_type") or "unknown") == s])

    overall = _bucket(closed)

    # Calibration (Brier) where Mason logged a confidence — are we over/under-confident?
    cal = []
    for t in closed:
        c = t.get("mason_confidence")
        if isinstance(c, (int, float)):
            p = c / 100.0 if c > 1 else float(c)
            y = 1.0 if t.get("outcome") == "win" else 0.0
            cal.append((p - y) ** 2)
    brier = round(sum(cal) / len(cal), 3) if cal else None

    return {"by_setup": by_setup, "overall": overall,
            "calibration": {"brier": brier, "graded": len(cal)},
            "as_of": date.today().isoformat()}


# ── feedback into new ideas ───────────────────────────────────────────────────────

def _edge_adj(setup, posture=None, _tr=None):
    tr = _tr or track_record()
    b = tr["by_setup"].get(setup) or tr["by_setup"].get((setup or "").lower())
    if not b:
        return 1.0, None
    edge = b["calib_win_rate"] - PRIOR_WIN          # -0.5 .. +0.5
    adj = max(ADJ_FLOOR, min(ADJ_CEIL, 1.0 + edge))
    return adj, b


def calibrated_confidence(setup, raw_score, posture=None, _tr=None):
    """Blend the screener's raw read with what this setup has actually delivered.
    n==0 → returns the raw score unchanged (no opinion yet); grows from there."""
    try:
        raw = float(raw_score)
    except Exception:
        return raw_score
    adj, _ = _edge_adj(setup, posture, _tr=_tr)
    return int(max(0, min(99, round(raw * adj))))


def confidence_sizing_factor(setup, posture=None, _tr=None):
    """A gentle multiplier for risk sizing (proven setups pressed, weak ones trimmed).
    Tighter band than confidence so a hot streak can't blow up risk."""
    adj, _ = _edge_adj(setup, posture, _tr=_tr)
    return round(max(0.7, min(1.15, 0.7 + (adj - ADJ_FLOOR) * (0.45 / (ADJ_CEIL - ADJ_FLOOR)))), 3)


# ── human-readable report ─────────────────────────────────────────────────────────

def report(trades=None):
    tr = track_record(trades)
    ov = tr["overall"]
    L = [f"MASON LEARNING LEDGER — {tr['as_of']}"]
    if ov["n"] == 0:
        L.append("  No resolved trades yet — confidence is still 'assumed' (screener-raw).")
        return "\n".join(L)
    L.append(f"  Overall: {ov['n']} closed · win-rate {ov['win_rate']} "
             f"(calibrated {ov['calib_win_rate']}) · expectancy {ov['expectancy_R']}R")
    if tr["calibration"]["brier"] is not None:
        L.append(f"  Calibration (Brier, lower=better): {tr['calibration']['brier']} "
                 f"over {tr['calibration']['graded']} graded")
    ranked = sorted(tr["by_setup"].items(), key=lambda kv: kv[1]["expectancy_R"], reverse=True)
    L.append("  By setup (best edge first):")
    for s, b in ranked:
        tag = "earned" if b["earned"] else f"assumed (n={b['n']})"
        L.append(f"    {s:16} {b['n']:>2} trades · win {b['win_rate']} "
                 f"→ calib {b['calib_win_rate']} · {b['expectancy_R']}R · {tag}")
    return "\n".join(L)


# ── self-test (offline; mocked bars + mocked trade list, never touches journals) ──

def _self_test():
    # 1) exit detection
    long_t = {"entry_date": "2026-06-01", "direction": "long", "stop_loss": 90, "target_price": 110,
              "time_horizon": "swing"}
    bars_target = [{"date": "2026-06-02", "high": 105, "low": 99, "close": 104},
                   {"date": "2026-06-03", "high": 112, "low": 101, "close": 111}]   # target
    bars_stop   = [{"date": "2026-06-02", "high": 101, "low": 88, "close": 92}]      # stop
    assert _detect_exit(long_t, bars_target) == (110, "target_hit")
    assert _detect_exit(long_t, bars_stop) == (90, "stop_hit")
    # ambiguous same-day → stop first
    assert _detect_exit(long_t, [{"date": "2026-06-02", "high": 115, "low": 85, "close": 100}]) == (90, "stop_hit")
    # short
    short_t = {"entry_date": "2026-06-01", "direction": "short", "stop_loss": 110, "target_price": 90}
    assert _detect_exit(short_t, [{"date": "2026-06-02", "high": 101, "low": 89, "close": 90}]) == (90, "target_hit")

    # 2) shrinkage: small sample stays near prior, large sample approaches empirical
    assert abs(_calib_winrate(1, 1) - 0.571) < 0.01     # 1/1 win → only 0.57 (earned slowly)
    assert abs(_calib_winrate(40, 50) - 0.768) < 0.01   # 80% over 50 → ~0.77

    # 3) calibrated_confidence grows with evidence
    tr_none = {"by_setup": {}}
    assert calibrated_confidence("VCP", 80, _tr=tr_none) == 80          # no history → raw
    tr_hot  = {"by_setup": {"VCP": {"calib_win_rate": 0.75, "n": 20, "win_rate": 0.75,
                                    "expectancy_R": 1.2, "earned": True}}}
    hot = calibrated_confidence("VCP", 80, _tr=tr_hot)
    tr_cold = {"by_setup": {"VCP": {"calib_win_rate": 0.30, "n": 20, "win_rate": 0.30,
                                    "expectancy_R": -0.4, "earned": True}}}
    cold = calibrated_confidence("VCP", 80, _tr=tr_cold)
    assert hot > 80 > cold, f"expected hot>80>cold, got hot={hot} cold={cold}"

    # 4) end-to-end track_record on a mock closed-trade list
    mock = [
        {"setup_type": "VCP", "outcome": "win",  "actual_pnl": 30, "risk_per_share": 5, "shares": 2, "mason_confidence": 70},
        {"setup_type": "VCP", "outcome": "loss", "actual_pnl": -10, "risk_per_share": 5, "shares": 2, "mason_confidence": 60},
        {"setup_type": "NEGLECTED-VALUE", "outcome": "win", "actual_pnl": 40, "risk_per_share": 4, "shares": 2, "mason_confidence": 55},
    ]
    tr = track_record(mock)
    assert tr["overall"]["n"] == 3 and tr["overall"]["wins"] == 2
    print("SELF-TEST OK")
    print(report(mock))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="report", choices=["report", "resolve", "selftest"])
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    elif a.cmd == "resolve":
        print(json.dumps(resolve_open(), indent=2, default=str))
    else:
        print(report())
