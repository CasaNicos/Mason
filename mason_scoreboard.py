"""
mason_scoreboard.py — the plain-English weekly scoreboard (audit L9, ruling D4; Step 5).
Read-only apart from its own weekly file and ONE inbox note a week. NO model calls,
NO outside requests (prices come from what Mason and mason_learn already hold).

The headline is two things a non-specialist reads in ten seconds:
  1. did Mason's own trades (agent_trades, source 'mason') beat simply holding the
     S&P 500 with the same money over the same days, after trading costs — in dollars
     and percent, since it began and for the last week / month;
  2. the worst drop from a high point along the way, for Mason and for the S&P 500.
Below it: short tables of what kinds of calls have worked so far, from `learn_outcomes`
(mason_learn.py), each with a count, the average result minus the S&P 500 and a plain
verdict. A verdict leans only when the range of likely values (80%) leaves zero out
BOTH with all ideas entered on one day counted as one group (audit L4) and with all ideas
entered in one calendar week counted as one group (5-day results of neighbouring days
overlap, so days are not independent), on at least `min_weeks` weeks, and no single stock
supplies more than `max_one_stock_share` of the group. Tables leave out ideas on stocks
under `min_entry_price` and ideas with no price at the entry minute (the 0.10% cost is not
realistic there), count one idea per stock per day, and start with a comparison line: any
large stock, no idea at all (review 2026-11-15).

How the money comparison is worked out (kept out of the headline on purpose):
  * Mason: profit or loss of each trade — the booked result when closed, the last daily
    close when still open — minus the same 0.10% cost the grader charges, on the money put in.
  * S&P 500: the SAME dollars put into the S&P 500 fund (SPY) at the same moment and
    taken out at the same moment (or held to the last close while the trade is open).
    No trading cost is charged on this side.
  * Dividends (2026-11-16) count on BOTH sides: shares held when an ex-dividend date opened
    earn that dividend (a short owes it), and the same money in the S&P 500 fund earns the
    fund's. Both come from mason_learn.Prices.dividends (Mason's price cache and the grader's
    nightly record); when either side has no record, neither side gets dividends for that
    trade and a note says so. The idea tables add the grader's div_pct / spy_div_pct the
    same way. Switch: learning.grader.dividends (off = everything as before).
  * An open option has no free daily price: it is left out of BOTH sides until it closes
    (held at what was paid; the same dollars are not counted in the S&P 500 either).
  * Percent is of the most money Mason had at work at one time in that period (worked
    out from the entry and exit times, not per day).

Public API (none of these raise):
    scoreboard(now=None) -> dict          the structured data (docs/fable/SCOREBOARD_API.md)
    render_markdown(data) -> str          exactly what Nico reads
    weekly(now=None, force=False) -> dict writes docs/fable/weekly/SCOREBOARD_<week>.md + one inbox note
    tick(now=None) -> dict                hourly hook: at most once a week, inside the nightly window
    http_get(args) -> (payload, status)   GET /api/learning/scoreboard

Switch: learning.scoreboard.enabled (on when absent) stops the weekly file and note;
the route keeps answering.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import tempfile
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import mason_db
import mason_learn as ML

log = logging.getLogger("mason.scoreboard")

BASE = Path(__file__).resolve().parent
_DEFAULTS = {
    "enabled": True,                 # the weekly file and the weekly inbox note
    "report_dir": "docs/fable/weekly",
    "main_horizon_days": 5,          # the horizon the "what has worked" tables use
    "min_calls": 30,                 # fewer graded calls than this in a group -> too few to tell
    "min_days": 10,                  # fewer separate days than this -> too few to tell
    "min_weeks": 6,                  # fewer separate calendar weeks than this -> too few to tell
    "max_one_stock_share": 0.25,     # one stock supplying more of a group than this -> too few to tell
    "min_entry_price": 5.0,          # ideas on stocks under this price are left out of the tables (0 = keep all)
    "exact_entry_only": True,        # leave out ideas with no price at the entry minute
    "fresh_floor_seconds": 20,       # ?fresh=1 cannot rebuild the page more often than this
    "min_closed_trades": 30,         # Mason's own finished trades needed before the totals mean anything
    "min_days_for_yearly_pace": 90,
    "target_low_pct": 10.0,          # Nico's target: 10-15% growth a year, and beat the S&P 500
    "target_high_pct": 15.0,
    "cache_seconds": 300,
}
_T80 = ((1, 3.078), (2, 1.886), (3, 1.638), (4, 1.533), (5, 1.476), (6, 1.440), (7, 1.415), (8, 1.397),
        (9, 1.383), (10, 1.372), (12, 1.356), (15, 1.341), (20, 1.325), (30, 1.310), (60, 1.296))
_SOURCE_NAMES = {
    "catalyst": "Scored news events", "news_alert": "News alerts sent to the inbox", "director_call": "Director buy / sell calls",
    "director_watch": "Director \"watch\" calls", "local_call": "Local model buy / sell calls",
    "local_watch": "Local model \"watch\" calls", "local_read": "Local model news reads",
    "dossier": "Research files", "scanner_hit": "Scanner hits", "quick_check": "Quick checks",
}
_SCORE_BANDS = ((0.0, 0.40, "below 0.40"), (0.40, 0.60, "0.40 to 0.60"), (0.60, 0.70, "0.60 to 0.70"),
                (0.70, 0.80, "0.70 to 0.80"), (0.80, 0.85, "0.80 to 0.85"), (0.85, 9.9, "0.85 and above"))
_CONF_BANDS = ((0.0, 0.50, "below 0.50"), (0.50, 0.55, "0.50 to 0.55"), (0.55, 0.60, "0.55 to 0.60"),
               (0.60, 9.9, "0.60 and above"))
_TOD_ORDER = ("before the open", "first hour", "midday", "last two hours", "after the close", "weekend or holiday")
_EVENT_NAMES = {"mna": "merger or acquisition", "deal_partnership": "deal or partnership",
                "fda_decision": "drug regulator (FDA) decision", "legal_regulatory": "legal or regulatory news",
                "analyst_action": "analyst rating change", "insider_buy": "insider buying",
                "insider_sell": "insider selling", "trial_readout": "drug trial result",
                "trial_update": "drug trial update", "contract_award": "contract award",
                "product_launch": "product launch", "mgmt_change": "management change",
                "guidance": "company forecast (guidance)", "other": "other news"}
_BASELINE_NAME = "Any large stock, no idea at all"
_cache = {"at": 0.0, "data": None}
_state = {"last_error": None}


def cfg() -> dict:
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in ML._config_block("scoreboard").items() if not str(k).startswith("_")})
    return out


def _log_once(msg: str) -> None:
    if _state.get("last_error") != msg:
        _state["last_error"] = msg
        log.warning("[scoreboard] %s", msg)


# ── the numbers behind a verdict ─────────────────────────────────────────────────

def clustered(pairs) -> dict:
    """Mean of the values with a standard error that counts all calls entered on one day
    as ONE group (they share that day's market). pairs = [(day, value)]. Same formula as
    regrade_director_predictions._clustered. {n, days, mean, se}; se None under 2 days."""
    pairs = [(d, float(v)) for d, v in pairs if isinstance(v, (int, float)) and v == v]
    n = len(pairs)
    if not n:
        return {"n": 0, "days": 0, "mean": None, "se": None}
    m = sum(v for _, v in pairs) / n
    by = {}
    for d, v in pairs:
        by[d] = by.get(d, 0.0) + (v - m)
    g = len(by)
    if g < 2:
        return {"n": n, "days": g, "mean": m, "se": None}
    se = math.sqrt(sum(x * x for x in by.values()) / (n * n) * g / (g - 1))
    return {"n": n, "days": g, "mean": m, "se": se if se > 0 else None}


def _t80(days: int) -> float:
    df = max(1, int(days) - 1)
    for k, v in _T80:
        if df <= k:
            return v
    return 1.282


def _week_of(day) -> str:
    """ISO week of a YYYY-MM-DD day; anything else is its own group."""
    try:
        y, w, _ = date.fromisoformat(str(day)[:10]).isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return str(day)


def judge(pairs, *, min_calls: int = 30, min_days: int = 10, min_weeks: int = 6, tickers=None,
          max_one_stock_share: float = 0.25) -> dict:
    """One table row: {n, days, weeks, avg_pct, typical_pct, low_pct, high_pct, verdict,
    one_stock_share}. pairs = [(entry day, value)]; tickers (optional, same order) = the stock
    of each value. The range shown is the WIDER of two 80% ranges of the average: ideas entered
    on one day counted as one group, and ideas entered in one calendar week counted as one
    group (results over several days overlap from one entry day to the next, so days alone
    overstate how much independent evidence there is).
    verdict: "too few to tell" (under min_calls ideas, min_days days or min_weeks weeks, or one
    stock supplies more than max_one_stock_share of the group) · "leaning good" / "leaning bad"
    (the range leaves zero out) · "no clear difference" (plenty of ideas, range includes zero)."""
    keep = [i for i, (_, v) in enumerate(pairs) if isinstance(v, (int, float)) and v == v]
    pairs = [pairs[i] for i in keep]
    c = clustered(pairs)
    w = clustered([(_week_of(d), v) for d, v in pairs])
    vals = sorted(float(v) for _, v in pairs)
    share = None
    if tickers is not None and pairs:
        cnt = {}
        for i in keep:
            cnt[tickers[i]] = cnt.get(tickers[i], 0) + 1
        share = max(cnt.values()) / float(len(pairs))
    out = {"n": c["n"], "days": c["days"], "weeks": w["days"],
           "avg_pct": None if c["mean"] is None else round(c["mean"], 2),
           "typical_pct": None, "low_pct": None, "high_pct": None, "verdict": "too few to tell",
           "one_stock_share": None if share is None else round(share, 2)}
    if vals:
        k = len(vals) // 2
        out["typical_pct"] = round(vals[k] if len(vals) % 2 else (vals[k - 1] + vals[k]) / 2.0, 2)
    if c["se"] is None:
        return out
    lo, hi = c["mean"] - _t80(c["days"]) * c["se"], c["mean"] + _t80(c["days"]) * c["se"]
    if w["se"] is not None:
        lo = min(lo, w["mean"] - _t80(w["days"]) * w["se"])
        hi = max(hi, w["mean"] + _t80(w["days"]) * w["se"])
    out["low_pct"], out["high_pct"] = round(lo, 2), round(hi, 2)
    if (c["n"] < int(min_calls) or c["days"] < int(min_days) or w["days"] < int(min_weeks)
            or (share is not None and share > float(max_one_stock_share))):
        return out
    out["verdict"] = "leaning good" if lo > 0 else "leaning bad" if hi < 0 else "no clear difference"
    return out


def worst_drop(values) -> float:
    """Largest fall from an earlier high in a running total that starts at 0 (a positive number)."""
    peak, worst = 0.0, 0.0
    for v in values:
        peak = max(peak, float(v))
        worst = max(worst, peak - float(v))
    return worst


# ── Mason's own trades against the S&P 500 ───────────────────────────────────────

def _peak_at_work(trades, day: str) -> float:
    """Most dollars Mason had in trades AT THE SAME TIME on `day` (from entry and exit times:
    a trade closed at 11:42 and another opened at 13:01 were never at work together)."""
    cur, ev = 0.0, []
    for t in trades:
        if t["in_day"] > day or (t["closed"] and t["out_day"] < day):
            continue
        if t["in_day"] < day:
            cur += t["cost"]
        else:
            ev.append((t["in_ts"], 1, t["cost"]))
        if t["closed"] and t["out_day"] == day:
            ev.append((t["out_ts"] or 0.0, 0, -t["cost"]))
    peak = cur
    for _, _, x in sorted(ev):                         # at the same instant an exit comes before an entry
        cur += x
        peak = max(peak, cur)
    return peak


def _px_at_time(prices, sym: str, when: datetime):
    """(price, exact) of `sym` at an Eastern moment: the minute bar when one is held,
    else that day's open (before noon) or close. (None, False) when there is no bar."""
    PP = ML._PP()
    et = PP._to_et(when)
    d = et.date()
    hol = PP._nyse_holidays()
    while not PP._is_session_day(d, hol):
        d += timedelta(days=1)
    if d == et.date():
        ts = int(et.timestamp())
        try:
            px, _ = PP._px_at(prices.minutes(sym, d), ts)
        except Exception:
            px = None
        if px:
            return float(px), True
    bar = prices.daily(sym, d, d).get(d.isoformat())
    if not bar:
        return None, False
    early = d != et.date() or (et.hour * 60 + et.minute) < 12 * 60
    return float(bar["o"] if early else bar["c"]), False


def _close_on(bars: dict, day: str):
    ks = [k for k in bars if k <= day]
    return (float(bars[max(ks)]["c"]), max(ks)) if ks else (None, None)


def _trade_dividends(t: dict, prices, bars: dict, bench_bars: dict, bench: str, as_of: date):
    """(Mason's dividend dollars, the S&P 500 side's) for one trade as [(ex-day, $)], or the name of the side
    that has no record (then neither side gets any). Entry day < ex-date <= exit day (or the last close while
    open). A long earns, a short owes; an option earns nothing itself; an open option is out of both sides."""
    r = t["r"]
    stock = str(r["vehicle"] or "stock").lower() == "stock"
    if not (stock or t["closed"]):
        return [], []
    a = date.fromisoformat(t["in_day"])
    z = date.fromisoformat(t["out_day"]) if t["closed"] else as_of

    def known(s):                                   # the record for the newest close arrives with the night's
        for k in range(5 if z >= as_of else 1):     # pass: up to then, count to the last day it covers (4 days
            e = z - timedelta(days=k)               # at most, for a window ending on the newest close only)
            got = prices.dividends(s, a, e) if e > a else []
            if got is not None:
                return got, e
        return None, None
    sym = str(r["ticker"] or "").upper()
    sd, se = known(sym) if stock else ([], z)
    bd, be = known(bench) if t["spy_in"] else ([], z)
    if sd is None or bd is None:
        return sym if sd is None else "the S&P 500 fund"
    end = min(se, be).isoformat()                   # both sides over the same days
    sd, bd = [x for x in sd if x[0] <= end], [x for x in bd if x[0] <= end]

    def per_share(b, ex_day, amount, share):        # on the price basis of the bars the page uses
        pc, _ = _close_on(b, (date.fromisoformat(ex_day) - timedelta(days=1)).isoformat())
        return share * pc if pc else amount
    sign = -1.0 if str(r["direction"] or "").lower() == "short" else 1.0
    sold = []                                       # part-sales booked while the trade stays open (mason_agent_loop
    try:                                            # partial_exits): shares sold BEFORE an ex-date are not paid it
        for x in (json.loads(r.get("features_json") or "{}").get("partial_exits") or []):
            at = ML._aware(x.get("at"))
            if at is not None and float(x.get("qty") or 0) > 0:
                sold.append((ML._PP()._to_et(at).date().isoformat(), float(x["qty"])))
    except Exception:
        sold = []

    def held(ex_day):                               # sold on the ex-date itself: still paid
        return max(0.0, float(r["qty"] or 0) - sum(q for d, q in sold if d < ex_day))
    mine = [(d, sign * held(d) * per_share(bars.get(sym) or {}, d, a_, s_)) for d, a_, s_ in sd]
    theirs = [(d, t["cost"] / t["spy_in"] * per_share(bench_bars, d, a_, s_)) for d, a_, s_ in bd]
    return mine, theirs


def own_trades(now: float, prices, c: dict, cost_pct: float, bench: str = "SPY", dividends: bool = False) -> dict:
    """Mason's trades against the same money in the S&P 500. See the module docstring."""
    PP = ML._PP()
    hol = PP._nyse_holidays()
    now_et = datetime.fromtimestamp(now, PP._et_tz())
    as_of = ML._last_closed_session(now_et, hol)
    conn = mason_db.get_connection()
    try:
        sql = ("SELECT id, ticker, vehicle, direction, entry_ts, entry_px, qty, cost_usd, exit_ts, pnl_usd, status{} "
               "FROM agent_trades WHERE source='mason' AND COALESCE(record_deleted,0)=0 "
               "AND entry_ts IS NOT NULL ORDER BY entry_ts")
        try:
            rows = [dict(r) for r in conn.execute(sql.format(", features_json" if dividends else "")).fetchall()]
        except sqlite3.Error:
            try:                                       # a table without features_json: no part-sales known
                rows = [dict(r) for r in conn.execute(sql.format("")).fetchall()] if dividends else []
            except sqlite3.Error:
                rows = []
    finally:
        conn.close()
    out = {"as_of": as_of.isoformat(), "n_trades": 0, "n_closed": 0, "n_open": 0, "first_trade_day": None,
           "days_running": 0, "periods": {}, "worst_drop": None, "series": [], "notes": [],
           "enough_to_mean_anything": False, "per_trade": None, "yearly_pace_pct": None}
    trades, approx_spy, unpriced = [], False, 0
    for r in rows:
        ent = ML._aware(r["entry_ts"])
        cost = ML._num(r["cost_usd"]) or ((ML._num(r["entry_px"]) or 0) * (ML._num(r["qty"]) or 0))
        if ent is None or not cost or cost <= 0:
            continue
        ent_et = PP._to_et(ent)
        if ent_et.date() > as_of:
            continue
        closed = r["status"] == "closed" and r["pnl_usd"] is not None and r["exit_ts"]
        ex = ML._aware(r["exit_ts"]) if closed else None
        ex_et = PP._to_et(ex) if ex else None
        if ex_et and ex_et.date() > as_of:
            closed, ex, ex_et = False, None, None
        s_in, exact_in = _px_at_time(prices, bench, ent)
        s_out, exact_out = _px_at_time(prices, bench, ex) if ex else (None, True)
        approx_spy = approx_spy or not exact_in or not exact_out
        trades.append({"r": r, "cost": float(cost), "fee": float(cost) * cost_pct / 100.0,
                       "in_ts": ent.timestamp(), "out_ts": ex.timestamp() if ex else None,
                       "in_day": ent_et.date().isoformat(), "out_day": ex_et.date().isoformat() if ex_et else None,
                       "closed": bool(closed), "pnl": float(r["pnl_usd"]) if closed else None,
                       "spy_in": s_in, "spy_out": s_out})
    out["n_trades"] = len(trades)
    out["n_closed"] = sum(1 for t in trades if t["closed"])
    out["n_open"] = out["n_trades"] - out["n_closed"]
    if not trades:
        out["notes"].append("Mason has not placed a trade of its own yet.")
        return out
    first = date.fromisoformat(min(t["in_day"] for t in trades))
    out["first_trade_day"] = first.isoformat()
    out["days_running"] = (as_of - first).days + 1
    bench_bars = prices.daily(bench, first, as_of)
    bars = {}
    for t in trades:
        sym = str(t["r"]["ticker"] or "").upper()
        if sym not in bars:
            bars[sym] = prices.daily(sym, first, as_of)
    no_div = set()
    for t in trades:
        t["div"], t["sdiv"] = [], []
        if dividends:
            got = _trade_dividends(t, prices, bars, bench_bars, bench, as_of)
            if isinstance(got, str):                   # no record for one side: neither side gets dividends
                no_div.add(got)
            else:
                t["div"], t["sdiv"] = got
    days, d = [], first
    while d <= as_of:
        if PP._is_session_day(d, hol):
            days.append(d.isoformat())
        d += timedelta(days=1)
    stale = set()
    for day in days:
        m_usd = s_usd = 0.0
        work = _peak_at_work(trades, day)
        s_close, s_day = _close_on(bench_bars, day)
        if s_day and s_day != day:
            stale.add(f"S&P 500 fund on {day}")
        for t in trades:
            if t["in_day"] > day:
                continue
            done = t["closed"] and t["out_day"] <= day
            r = t["r"]
            no_price = False
            if done:
                pnl = t["pnl"]
            elif str(r["vehicle"] or "stock").lower() == "stock":
                px, px_day = _close_on(bars.get(str(r["ticker"]).upper()) or {}, day)
                if px is None or not ML._num(r["entry_px"]):
                    pnl = 0.0
                    stale.add(f"{r['ticker']} on {day}")
                else:
                    sign = -1.0 if str(r["direction"] or "").lower() == "short" else 1.0
                    pnl = sign * (px - float(r["entry_px"])) * float(r["qty"] or 0)
                    if px_day != day:
                        stale.add(f"{r['ticker']} on {day}")
            else:
                pnl, no_price = 0.0, True              # an open option stays at what was paid ...
            m_usd += pnl - t["fee"] + sum(v for x, v in t["div"] if x <= day)
            s_px = t["spy_out"] if done else s_close
            if t["spy_in"] and s_px and not no_price:  # ... and is not counted in the S&P 500 either
                s_usd += t["cost"] * (s_px / t["spy_in"] - 1.0) + sum(v for x, v in t["sdiv"] if x <= day)
        out["series"].append({"day": day, "mason_usd": round(m_usd, 2), "spy_usd": round(s_usd, 2),
                              "at_work_usd": round(work, 2)})
    unpriced = sum(1 for t in trades if not t["closed"] and str(t["r"]["vehicle"] or "stock").lower() != "stock")

    def period(n_sessions):
        ser = out["series"]
        win = ser if n_sessions is None else ser[-n_sessions:]
        before = ser[len(ser) - len(win) - 1] if len(win) < len(ser) else {"mason_usd": 0.0, "spy_usd": 0.0}
        base = max(x["at_work_usd"] for x in win) or 0.0
        m = win[-1]["mason_usd"] - before["mason_usd"]
        s = win[-1]["spy_usd"] - before["spy_usd"]
        return {"from_day": win[0]["day"], "to_day": win[-1]["day"], "sessions": len(win),
                "mason_usd": round(m, 2), "spy_usd": round(s, 2), "beat_usd": round(m - s, 2),
                "at_work_usd": round(base, 2),
                "mason_pct": None if not base else round(m / base * 100.0, 2),
                "spy_pct": None if not base else round(s / base * 100.0, 2),
                "beat_pct": None if not base else round((m - s) / base * 100.0, 2),
                "whole_history": len(win) == len(ser)}
    out["periods"] = {"since_start": period(None), "last_week": period(5), "last_month": period(21)}
    base = max(x["at_work_usd"] for x in out["series"]) or 0.0
    md, sd = worst_drop(x["mason_usd"] for x in out["series"]), worst_drop(x["spy_usd"] for x in out["series"])
    out["worst_drop"] = {"mason_usd": round(md, 2), "spy_usd": round(sd, 2), "base_usd": round(base, 2),
                         "mason_pct": None if not base else round(md / base * 100.0, 2),
                         "spy_pct": None if not base else round(sd / base * 100.0, 2),
                         "measured": "at each day's close"}
    # per trade: its own result against the S&P 500 over its own days, grouped by entry day
    pairs, last = [], out["series"][-1]["day"]
    s_last, _ = _close_on(bench_bars, last)
    for t in trades:
        r = t["r"]
        if t["closed"]:
            pnl, s_px = t["pnl"], t["spy_out"]
        elif str(r["vehicle"] or "stock").lower() == "stock":
            px, _ = _close_on(bars.get(str(r["ticker"]).upper()) or {}, last)
            if px is None or not ML._num(r["entry_px"]):
                continue
            sign = -1.0 if str(r["direction"] or "").lower() == "short" else 1.0
            pnl, s_px = sign * (px - float(r["entry_px"])) * float(r["qty"] or 0), s_last
        else:
            continue
        if t["spy_in"] and s_px:
            dv, sv = sum(v for _, v in t["div"]), sum(v for _, v in t["sdiv"])
            pairs.append((t["in_day"], (pnl + dv - t["fee"]) / t["cost"] * 100.0
                          - ((s_px / t["spy_in"] - 1.0) * 100.0 + sv / t["cost"] * 100.0)))
    out["per_trade"] = judge(pairs, min_calls=c["min_closed_trades"], min_days=c["min_days"],
                             min_weeks=c.get("min_weeks", 6))
    out["enough_to_mean_anything"] = (out["n_closed"] >= int(c["min_closed_trades"])
                                      and out["per_trade"]["verdict"] != "too few to tell")
    ss = out["periods"]["since_start"]
    if (out["days_running"] >= int(c["min_days_for_yearly_pace"]) and out["n_closed"] >= int(c["min_closed_trades"])
            and ss["mason_pct"] is not None):
        out["yearly_pace_pct"] = round(ss["mason_pct"] * 365.0 / out["days_running"], 1)
    if unpriced:
        out["notes"].append(f"{unpriced} open option trade{'s are' if unpriced != 1 else ' is'} left out of the "
                            "comparison on both sides until closed: held at what was paid, and the same money is "
                            "not counted in the S&P 500 either (there is no free daily price history for options).")
    if approx_spy:
        out["notes"].append("Where no minute-by-minute S&P 500 price was stored, that day's opening or closing "
                            "price was used for the comparison.")
    if stale:
        out["notes"].append("No closing price in hand yet for: " + ", ".join(sorted(stale)[:6])
                            + (" and others" if len(stale) > 6 else "") + ". The last known price was used.")
    paid = round(sum(v for t in trades for _, v in t["div"]), 2)
    s_paid = round(sum(v for t in trades for _, v in t["sdiv"]), 2)
    out["dividends"] = {"counted": bool(dividends), "mason_usd": paid, "spy_usd": s_paid, "no_record": sorted(no_div)}
    if dividends and (paid or s_paid):
        out["notes"].append(f"Dividends are included: {_money(paid, True)} on Mason's stocks while they were held, "
                            f"{_money(s_paid, True)} on the same money in the S&P 500. These are the published "
                            "dividends, not cash the paper account paid (it pays only some of them).")
    if no_div:
        out["notes"].append("No dividend record yet for: " + ", ".join(sorted(no_div)[:6])
                            + (" and others" if len(no_div) > 6 else "") + ". Dividends are left out for those "
                            "trades on both sides until it arrives.")
    return out


# ── what has worked so far (from learn_outcomes) ─────────────────────────────────

def _band(v, bands):
    if v is None:
        return None
    for lo, hi, name in bands:
        if lo <= v < hi:
            return name
    return None


def _large_universe() -> set:
    """Tickers of the S&P 500 and the Nasdaq 100 (plus Mason's own book) from the list the news
    handling already keeps on disk. Never fetches. Empty when the list is not available."""
    try:
        import mason_triage
        return {str(t).upper() for t in (mason_triage.broad_universe(allow_refresh=False) or {})}
    except Exception:
        return set()


def average_stock(conn, h: int, first_day: str, last_day: str, min_px: float, bench: str = "SPY",
                  dividends: bool = False) -> list:
    """The comparison line: buy ANY large stock at the open of each trading day, sell at the close
    h-1 trading days later (the same span as an idea that appeared before the open), minus the
    S&P 500 fund over the same days. [(day, percent, ticker)] from Mason's own daily prices.
    It shows how far the average stock and the S&P 500 fund moved apart with no idea involved.
    With `dividends`, both sides add the cash dividends of the days after the entry day up to the
    exit day, from the same rows (a row without the free source's record adds none, as before)."""
    uni = _large_universe()
    if not uni or not first_day or not last_day:
        return []
    px = {}
    for r in conn.execute("SELECT ticker, date, open, close, json_extract(xcheck_json, '$.div') dv, "
                          "json_extract(xcheck_json, '$.yf_close') yc FROM price_bars_daily "
                          "WHERE date>=? AND COALESCE(xcheck_status,'')<>'not_found'", (first_day,)):
        t = str(r["ticker"]).upper()
        if t == bench or t in uni:
            # None = no dividend record that day (no free-source close on the row): not known, not "none"
            dv = ((ML._num(r["dv"]) or 0.0) if (ML._num(r["yc"]) or 0) > 0 else None) if dividends else 0.0
            px.setdefault(t, {})[str(r["date"])[:10]] = (ML._num(r["open"]), ML._num(r["close"]), dv)
    b = px.get(bench) or {}
    days = sorted(b)
    out = []
    for i, d in enumerate(days):
        if d > last_day or i + h - 1 >= len(days):
            continue
        x = days[i + h - 1]
        if not (b[d][0] and b[x][1]):
            continue
        held = days[i + 1:i + h]                       # an ex-date after the entry day, up to the exit day
        s0 = b[x][1] / b[d][0] - 1.0
        bdv = [b[k][2] for k in held]
        s = (b[x][1] + sum(bdv)) / b[d][0] - 1.0 if None not in bdv else None
        for t, pt in px.items():
            if t == bench or d not in pt or x not in pt:
                continue
            o, cl = pt[d][0], pt[x][1]
            if o and cl and o >= float(min_px or 0):
                dvs = [pt[k][2] if k in pt else None for k in held]
                if s is None or None in dvs:           # no record on one side: neither side gets dividends
                    out.append((d, ((cl / o - 1.0) - s0) * 100.0, t))
                else:
                    out.append((d, (((cl + sum(dvs)) / o - 1.0) - s) * 100.0, t))
    return out


def what_worked(c: dict, dividends: bool = False) -> dict:
    h = int(c["main_horizon_days"])
    min_px = float(c.get("min_entry_price") or 0)
    exact_only = c.get("exact_entry_only") is not False
    base_pairs = []
    conn = mason_db.get_connection()
    try:
        try:
            cols = ("SELECT source_kind, label, score, confidence, time_of_day, entry_session, direction, ticker, "
                    "direction_basis, entry_exact, entry_px, result_minus_spy_pct, raw_minus_spy_pct")
            try:
                rows_all = [dict(r) for r in conn.execute(
                    cols + ", div_pct, spy_div_pct, div_status FROM learn_outcomes WHERE status='graded' "
                    "AND horizon_days=?", (h,)).fetchall()]
            except sqlite3.OperationalError:          # a table from before the dividend columns: price moves only
                rows_all = [dict(r) for r in conn.execute(
                    cols + " FROM learn_outcomes WHERE status='graded' AND horizon_days=?", (h,)).fetchall()]
            for r in rows_all:                         # dividends paid while held: a long earns, a short owes
                if dividends and r.get("div_status") == "counted":
                    dd = (r.get("div_pct") or 0.0) - (r.get("spy_div_pct") or 0.0)
                    if r["result_minus_spy_pct"] is not None:
                        r["result_minus_spy_pct"] += (-dd if r["direction"] == "short" else dd)
                    if r["raw_minus_spy_pct"] is not None:
                        r["raw_minus_spy_pct"] += dd
            cov = {}
            for r in conn.execute("SELECT source_kind, status, COUNT(*) n, SUM(entry_exact) ex FROM learn_outcomes "
                                  "WHERE horizon_days=? GROUP BY 1, 2", (h,)):
                cov.setdefault(r["source_kind"], {})[r["status"]] = r["n"]
                if r["status"] == "graded":
                    cov[r["source_kind"]]["exact_entry"] = int(r["ex"] or 0)
        except sqlite3.Error:
            rows_all, cov = [], {}
        try:
            ds = [r["entry_session"] for r in rows_all if r["entry_session"]]
            if ds:
                base_pairs = average_stock(conn, h, min(ds), max(ds), min_px, ML._grade_opts(ML.cfg())["benchmark"],
                                           dividends=dividends)
        except Exception as e:
            log.debug("[scoreboard] comparison line skipped: %s", e)
    finally:
        conn.close()
    is_low = lambda r: bool(min_px) and r["entry_px"] is not None and r["entry_px"] < min_px
    low = [r for r in rows_all if is_low(r)]
    inexact = [r for r in rows_all if exact_only and not r["entry_exact"] and not is_low(r)]
    rows = [r for r in rows_all if not is_low(r) and not (exact_only and not r["entry_exact"])]
    kw = {"min_calls": c["min_calls"], "min_days": c["min_days"], "min_weeks": c.get("min_weeks", 6),
          "max_one_stock_share": c.get("max_one_stock_share", 0.25)}

    def one_row(name, obs, n_calls=None):
        """obs = [(day, value, ticker)], already one per stock per day."""
        j = judge([(d, v) for d, v, _ in obs], tickers=[t for _, _, t in obs], **kw)
        return dict(group=name, n_calls=len(obs) if n_calls is None else n_calls, **j)

    def table(key, title, note, items, group_fn, value_key, order=None, name_fn=None, top=None):
        groups, calls = {}, {}
        for r in items:
            g = group_fn(r)
            if g is None or r.get(value_key) is None:
                continue
            # several ideas on one stock entered on one day are one piece of evidence, not several
            groups.setdefault(g, {}).setdefault((r["ticker"], r["entry_session"]), []).append(r[value_key])
            calls[g] = calls.get(g, 0) + 1
        names = list(groups)
        if order:
            names.sort(key=lambda g: order.index(g) if g in order else len(order))
        else:
            names.sort(key=lambda g: -calls[g])
        if top:
            names = names[:top]
        return {"key": key, "title": title, "note": note,
                "rows": [one_row(name_fn(g) if name_fn else g,
                                 [(d, sum(v) / len(v), t) for (t, d), v in groups[g].items()], calls[g])
                         for g in names]}

    directed = [r for r in rows if r["direction"] in ("long", "short")]
    undirected = [r for r in rows if r["direction"] not in ("long", "short")]
    cats = [r for r in rows if r["source_kind"] == "catalyst"]
    src_name = lambda g: _SOURCE_NAMES.get(g, g)
    score_order = [b[2] for b in _SCORE_BANDS]
    tables = []
    if base_pairs:
        tables.append({"key": "comparison", "title": "For comparison: any large stock, no idea at all",
                       "note": "Buying every stock in the S&P 500 and the Nasdaq 100 at the open, whatever the news, "
                               "and holding it the same number of days, minus the S&P 500 fund. When this line is "
                               "not near zero, the average stock and the S&P 500 fund (which a few very large "
                               "companies dominate) moved apart in these weeks, and every line below carries the "
                               "same push. Read the lines below against this one, not against zero.",
                       "rows": [one_row(_BASELINE_NAME, base_pairs)]})
    tables += [
        table("by_source", "By where the idea came from",
              "Ideas that named a direction (up or down). Result = trading that direction, after costs, minus the "
              "S&P 500 traded the same direction. A scored news event is the story as it stood at its last update, "
              "so it describes the end of a story, not the moment an alert went out.",
              directed, lambda r: r["source_kind"], "result_minus_spy_pct", name_fn=src_name),
        table("by_event_type", "By type of news",
              "Scored news events that named a direction, traded in that direction.",
              [r for r in cats if r["direction"] in ("long", "short")],
              lambda r: r["label"], "result_minus_spy_pct", top=14,
              name_fn=lambda g: _EVENT_NAMES.get(str(g), str(g).replace("_", " "))),
        table("by_score_band", "By news score",
              "Scored news events that named a direction, by the score the story had at its last update.",
              [r for r in cats if r["direction"] in ("long", "short")],
              lambda r: _band(r["score"], _SCORE_BANDS), "result_minus_spy_pct", order=score_order),
        table("by_score_band_bought", "By news score, simply buying the stock",
              "Every scored news event, whatever direction it named: buy the stock, no opinion, before costs.",
              cats, lambda r: _band(r["score"], _SCORE_BANDS), "raw_minus_spy_pct", order=score_order),
        table("by_confidence", "By how sure the model said it was",
              "Director calls that named a direction, by the confidence the director stated.",
              [r for r in directed if r["source_kind"] in ("director_call", "director_watch")],
              lambda r: _band(r["confidence"], _CONF_BANDS), "result_minus_spy_pct",
              order=[b[2] for b in _CONF_BANDS]),
        table("by_time_of_day", "By time of day the idea appeared",
              "All ideas that named a direction.", directed, lambda r: r["time_of_day"],
              "result_minus_spy_pct", order=list(_TOD_ORDER)),
        table("no_direction", "Ideas with no direction given, if the stock had simply been bought",
              "No opinion was stated, so this only shows how the stock moved against the S&P 500, before costs.",
              undirected, lambda r: r["source_kind"], "raw_minus_spy_pct", name_fn=src_name),
    ]
    graded = sum(v.get("graded", 0) for v in cov.values())
    exact = sum(v.get("exact_entry", 0) for v in cov.values())
    judged = [r for t in tables if t["key"] != "comparison" for r in t["rows"] if r["verdict"] != "too few to tell"]
    leans = [r for r in judged if r["verdict"].startswith("leaning")]
    return {"horizon_days": h, "tables": tables,
            "leans": {"groups_with_enough_data": len(judged), "leaning": len(leans),
                      "expected_by_luck": int(round(len(judged) / 5.0))},
            "coverage": {"by_source": cov, "graded": graded,
                         "not_gradable": sum(v.get("not_gradable", 0) for v in cov.values()),
                         "waiting": sum(v.get("pending", 0) for v in cov.values()),
                         "exact_entry": exact, "approximate_entry": graded - exact,
                         "min_entry_price": min_px, "left_out_low_price": len(low),
                         "left_out_no_minute_price": len(inexact), "in_tables": len(rows),
                         "dividends_counted": sum(1 for r in rows if r.get("div_status") == "counted") if dividends else 0,
                         "dividends_unknown": sum(1 for r in rows if r.get("div_status") not in ("counted", "none"))
                         if dividends else 0}}


# ── the structured result ────────────────────────────────────────────────────────

def _money(v, sign=False) -> str:
    if v is None:
        return "n/a"
    s = f"${abs(v):,.0f}" if abs(v) >= 100 else f"${abs(v):,.2f}"
    if sign:
        return ("+" if v >= 0 else "-") + s
    return s


def _updown(v) -> str:
    return "about even" if v is None or abs(v) < 0.005 else (f"up {_money(v)}" if v > 0 else f"down {_money(v)}")


def _pct(v, sign=True) -> str:
    return "n/a" if v is None else (f"{v:+.2f}%" if sign else f"{v:.2f}%")


def _nice_day(s) -> str:
    try:
        return date.fromisoformat(str(s)[:10]).strftime("%b %-d, %Y")
    except Exception:
        return str(s)


def headline(own: dict, c: dict) -> dict:
    """The two lines Nico reads first, plus the plain statement of how much they mean."""
    if not own.get("n_trades"):
        return {"line1": "Mason has not placed a trade of its own yet, so there is nothing to compare with the S&P 500.",
                "line2": "Worst drop from a high point: nothing to measure yet.",
                "how_much_it_means": "Nothing to judge yet."}
    p, w = own["periods"]["since_start"], own["worst_drop"]
    n, nc = own["n_trades"], own["n_closed"]
    b = p["beat_usd"]
    verdict = ("level with" if abs(b) < 0.005 else "ahead of" if b > 0 else "behind")
    line1 = (f"Since Mason began trading on {_nice_day(own['first_trade_day'])} ({n} trade{'s' if n != 1 else ''}, "
             f"{nc} finished): Mason is {_updown(p['mason_usd'])} ({_pct(p['mason_pct'])} of the "
             f"{_money(p['at_work_usd'])} it had at work) after trading costs. The same money in the S&P 500 "
             f"over the same days would be {_updown(p['spy_usd'])} ({_pct(p['spy_pct'])}). "
             f"Mason is {verdict} the S&P 500" + ("." if abs(b) < 0.005 else f" by {_money(abs(b))}."))
    line2 = (f"Worst drop from a high point: Mason {_money(w['mason_usd'])} ({_pct(w['mason_pct'], False)}), "
             f"the S&P 500 {_money(w['spy_usd'])} ({_pct(w['spy_pct'], False)}).")
    need = int(c["min_closed_trades"])
    if own["enough_to_mean_anything"]:
        means = (f"{nc} finished trades is enough to start reading this, and the lean is "
                 f"\"{own['per_trade']['verdict']}\".")
    elif nc < need:
        means = (f"This rests on {n} trade{'s' if n != 1 else ''} ({nc} finished) over {own['days_running']} "
                 f"day{'s' if own['days_running'] != 1 else ''}. That is far too few to mean anything: "
                 f"treat it as a running tally. It starts to mean something at about {need} finished trades.")
    else:
        means = (f"{nc} finished trades, but the results are still too mixed to say Mason is better or "
                 "worse than the S&P 500.")
    return {"line1": line1, "line2": line2, "how_much_it_means": means}


def target_progress(own: dict, c: dict) -> dict:
    lo, hi = float(c["target_low_pct"]), float(c["target_high_pct"])
    out = {"target_low_pct": lo, "target_high_pct": hi, "yearly_pace_pct": own.get("yearly_pace_pct"),
           "beating_sp500": None, "text": ""}
    if not own.get("n_trades"):
        out["text"] = f"Target: beat the S&P 500, and grow {lo:.0f}-{hi:.0f}% a year. No trades yet."
        return out
    out["beating_sp500"] = own["periods"]["since_start"]["beat_usd"] > 0
    if own.get("yearly_pace_pct") is None:
        out["text"] = (f"Target: beat the S&P 500, and grow {lo:.0f}-{hi:.0f}% a year. It is too early to turn "
                       f"{own['days_running']} day{'s' if own['days_running'] != 1 else ''} of trading into a "
                       f"yearly figure; that line starts after {int(c['min_days_for_yearly_pace'])} days and "
                       f"{int(c['min_closed_trades'])} finished trades.")
    else:
        pace = own["yearly_pace_pct"]
        where = "below" if pace < lo else "inside" if pace <= hi else "above"
        out["text"] = (f"Target: beat the S&P 500, and grow {lo:.0f}-{hi:.0f}% a year. At the pace so far Mason "
                       f"would make {pace:+.1f}% a year on the money it puts to work, which is {where} the target.")
    return out


def parked_cash(prices, as_of) -> dict | None:
    """Idle-cash rule (mason_idle_cash, 2026-11-16): spare cash parked in the index fund, as its OWN figure.
    It is not in agent_trades, so it is in none of the numbers above; this only reports it. None when nothing
    was ever parked (the scoreboard is then exactly what it was). Never raises."""
    try:
        import mason_idle_cash as IC
        s = IC.summary()
        if not s:
            return None
        px, px_day = None, None
        try:
            d = date.fromisoformat(str(as_of)[:10])
            px, px_day = _close_on(prices.daily(s["fund"], d - timedelta(days=10), d) or {}, d.isoformat())
        except Exception:
            px, px_day = None, None
        if px:
            s = IC.summary({s["fund"]: px}) or s
            s["price_day"] = px_day
        elif s.get("last_fill_px") and s.get("qty"):          # no closing price in hand: valued at the last fill
            s = IC.summary({s["fund"]: s["last_fill_px"]}) or s
            s["price_day"] = None
        s["text"] = IC.scoreboard_line({s["fund"]: s.get("price_used")} if s.get("price_used") else None)
        return s
    except Exception:
        return None


def scoreboard(now: float | None = None, *, cache=None) -> dict:
    """Everything the scoreboard shows, as plain data. Never raises."""
    now = float(now if now is not None else time.time())
    c = cfg()
    out = {"ok": False, "generated_at": datetime.fromtimestamp(now).isoformat(timespec="seconds")}
    prices = None
    try:
        gc = ML.cfg()
        opts = ML._grade_opts(gc)
        divs = gc.get("dividends", True) is not False
        prices = ML.Prices(cache or ML.cache_path(gc), network=False, now=now, benchmark=opts["benchmark"],
                           readonly=True)
        own = own_trades(now, prices, c, float(opts["cost_pct_stock"]), opts["benchmark"], dividends=divs)
        out.update(ok=True, as_of=own["as_of"], cost_pct=float(opts["cost_pct_stock"]), dividends_counted=divs,
                   headline=headline(own, c), target=target_progress(own, c), own_trades=own,
                   what_worked=what_worked(c, dividends=divs))
        pc = parked_cash(prices, own["as_of"])               # the key exists only once something was parked
        if pc:
            out["parked_cash"] = pc
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        _log_once(f"could not build the scoreboard: {out['error']}")
    finally:
        if prices is not None:
            prices.close()
    return out


# ── the page Nico reads ──────────────────────────────────────────────────────────

def render_markdown(d: dict) -> str:
    if not d.get("ok"):
        return "# Mason scoreboard\n\nThe scoreboard could not be built: " + str(d.get("error")) + "\n"
    own, hl, ww = d["own_trades"], d["headline"], d["what_worked"]
    L = [f"# Mason scoreboard, prices to the close of {_nice_day(d['as_of'])}", "",
         "## The two things that matter", "",
         f"**1. Did Mason beat the S&P 500?** {hl['line1']}", "",
         f"**2. {hl['line2']}**", "",
         f"**How much this means:** {hl['how_much_it_means']}", "",
         d["target"]["text"], ""]
    if own.get("n_trades"):
        L += ["## Mason's own trades against the S&P 500", "",
              "| Period | Mason, after costs | Same money in the S&P 500 | Mason ahead (+) or behind (-) |",
              "|---|---|---|---|"]
        seen = False
        for key, name in (("last_week", "Last 5 trading days"), ("last_month", "Last 21 trading days"),
                          ("since_start", "Since it began")):
            p = own["periods"][key]
            if key != "since_start" and p["whole_history"]:
                if not seen:
                    L.append(f"| {name} and longer | same as \"Since it began\": Mason has only traded for "
                             f"{p['sessions']} trading day{'s' if p['sessions'] != 1 else ''} | | |")
                    seen = True
                continue
            L.append(f"| {name} | {_money(p['mason_usd'], True)} ({_pct(p['mason_pct'])}) | "
                     f"{_money(p['spy_usd'], True)} ({_pct(p['spy_pct'])}) | "
                     f"{_money(p['beat_usd'], True)} ({_pct(p['beat_pct'])}) |")
        L += ["", f"Trades: {own['n_trades']} placed, {own['n_closed']} finished, {own['n_open']} still open. "
                  f"Percent is of the most money Mason had at work at one time "
                  f"({_money(own['periods']['since_start']['at_work_usd'])}). "
                  f"Costs: {d['cost_pct']:.2f}% of each of Mason's trades, the same charge the grader uses; the S&P 500 "
                  "side is not charged. "
                  + ("Dividends count on both sides: what Mason's stocks paid while they were held, and what the "
                     "S&P 500 fund would have paid on the same money. They are worked out from the published "
                     "dividends, in full before any tax, not from the cash the paper account received: the paper "
                     "account has paid only some of the dividends it owed, so Mason's side here can be a little "
                     "higher than the account's own result. " if d.get("dividends_counted")
                     else "Dividends are not counted on either side. ")
                  + "The worst drop is measured at each day's closing prices, counting the starting point as the first high."]
        L += [f"- {n}" for n in own["notes"]]
        L.append("")
    if (d.get("parked_cash") or {}).get("text"):               # idle-cash rule: its own line, outside the comparison
        L += ["## Parked cash", "", d["parked_cash"]["text"], ""]
    cov = ww["coverage"]
    ln = ww.get("leans") or {}
    floor = cov.get("min_entry_price") or 0
    L += [f"## What has worked so far, and what has not ({ww['horizon_days']} trading days after each idea)", "",
          "**Nothing in this section is a reason to change a setting.** It is a record to watch week by week.", "",
          "Every idea Mason looked at is followed for a fixed number of trading days as if it had been traded "
          "the simple way: in at the first price available after the idea appeared, out at the close "
          f"{ww['horizon_days']} trading days later. \"Against the S&P 500\" means the result minus what the "
          "S&P 500 did over the same days. These are paper results on ideas, not money made. Each stock counts "
          "once a day in a group, however many ideas named it that day."
          + (" Dividends paid in those days count on both sides (a stock sold short owes them)."
             if d.get("dividends_counted") else "")
          + (f" Ideas on stocks under ${floor:,.0f} and ideas with no price at the entry minute are left out, "
             "because they could not really be traded at the cost assumed." if floor else ""), "",
          "How to read the last column: **leaning good / leaning bad** = the likely range of the average stays "
          "on one side of zero, whether the ideas are grouped by day or by week; **no clear difference** = plenty "
          "of ideas, and the average could still be zero; **too few to tell** = not enough ideas, days or weeks "
          "yet, or one stock makes up more than a quarter of the group.", "",
          (f"A lean is not proof. This page shows {ln.get('leaning', 0)} leans among "
           f"{ln.get('groups_with_enough_data', 0)} groups with enough data; about "
           f"{ln.get('expected_by_luck', 0)} would lean by luck alone even if nothing Mason does mattered. "
           "The groups also overlap (the same ideas are cut several ways) and share the same few weeks of "
           "market, so leans come in bunches. A lean is a reason to keep watching, not to change anything."), ""]
    for t in ww["tables"]:
        if not t["rows"]:
            continue
        L += [f"### {t['title']}", "", t["note"], "",
              "| Group | Ideas | Days | Weeks | Average against the S&P 500 | Middle result | Likely range of the average | Verdict |",
              "|---|---|---|---|---|---|---|---|"]
        for r in t["rows"]:
            rng = "n/a" if r["low_pct"] is None else f"{r['low_pct']:+.2f}% to {r['high_pct']:+.2f}%"
            v = r["verdict"]
            if t["key"] == "comparison":
                v = "(comparison line)"
            elif (r.get("one_stock_share") and r["one_stock_share"] > 0.25 and v == "too few to tell"
                  and r["n"] >= 30 and r["days"] >= 10):
                v += f" (one stock is {r['one_stock_share'] * 100:.0f}% of this group)"
            L.append(f"| {r['group']} | {r['n']:,} | {r['days']} | {r.get('weeks', '')} | {_pct(r['avg_pct'])} | "
                     f"{_pct(r['typical_pct'])} | {rng} | {v} |")
        L.append("")
    L += ["## How much is behind these tables", "",
          f"- Ideas graded at {ww['horizon_days']} trading days: {cov['graded']:,}. "
          f"Could not be graded (no price available): {cov['not_gradable']:,}. Still waiting for time to pass: "
          f"{cov['waiting']:,}.",
          f"- Of the graded ideas, {cov['exact_entry']:,} use a price at the right minute and "
          f"{cov['approximate_entry']:,} use the day's opening price because no minute price was available.",
          f"- Left out of the tables above: {cov.get('left_out_low_price', 0):,} on stocks under "
          f"${floor:,.0f} and {cov.get('left_out_no_minute_price', 0):,} with no price at the entry minute. "
          f"In the tables: {cov.get('in_tables', 0):,}."]
    if d.get("dividends_counted"):
        L.append(f"- Dividends: added for {cov.get('dividends_counted', 0):,} ideas in the tables that were held "
                 f"through a dividend date (the stock's, the S&P 500 fund's or both). For "
                 f"{cov.get('dividends_unknown', 0):,} there was no dividend record yet, so they show the price "
                 "move only, as before.")
    L += ["- \"Middle result\" is the result that half the ideas beat and half did not. When it is far from the "
          "average, a few very large moves are doing the work.", ""]
    return "\n".join(L) + "\n"


# ── weekly file + inbox note ─────────────────────────────────────────────────────

def _week_key(now_et: datetime) -> str:
    """ISO week of the most recent Friday that is over (Saturday and Sunday belong to the week just ended)."""
    d = now_et.date()
    back = (d.weekday() - 4) % 7 or (0 if now_et.hour >= 17 else 7)
    fri = d - timedelta(days=back)
    iy, iw, _ = fri.isocalendar()
    return f"{iy}-W{iw:02d}"


def weekly(now: float | None = None, force: bool = False, *, notifier=None, cache=None) -> dict:
    """Write this week's scoreboard file and send ONE inbox note. Once per week. Never raises."""
    try:
        c = cfg()
        if c.get("enabled") is False and not force:
            return {"skipped": "switched off"}
        now = float(now if now is not None else time.time())
        now_et = datetime.fromtimestamp(now, ML._PP()._et_tz())
        week = _week_key(now_et)
        out_dir = Path(str(c.get("report_dir") or _DEFAULTS["report_dir"]))
        out_dir = out_dir if out_dir.is_absolute() else BASE / out_dir
        path = out_dir / f"SCOREBOARD_{week}.md"
        if path.exists() and not force:
            return {"skipped": "done this week", "week": week}
        data = scoreboard(now, cache=cache)
        if not data.get("ok"):
            return {"error": data.get("error"), "week": week}
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".scoreboard_", suffix=".tmp", dir=str(out_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(render_markdown(data))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        hl = data["headline"]
        body = "\n".join([hl["line1"], hl["line2"], hl["how_much_it_means"],
                          f"Full page: {path.relative_to(BASE) if path.is_relative_to(BASE) else path}"])
        try:
            if notifier is None:
                import mason_notifications as MN
                notifier = MN.add_notification
            notifier("learning_scoreboard", f"Weekly scoreboard {week}: Mason against the S&P 500", body=body,
                     severity="info", created_by="system", dedup_key=f"learning_scoreboard:{week}")
        except Exception as e:
            _log_once(f"weekly note not sent: {type(e).__name__}: {e}")
        return {"ok": True, "week": week, "path": str(path)}
    except Exception as e:
        _log_once(f"weekly scoreboard failed: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def tick(now: float | None = None) -> dict:
    """Hourly hook from the maintenance thread (after the grader). Writes the weekly page
    at most once a week, inside the same nightly window as the grader, and only once
    tonight's grading is finished (when the grader is on). Never raises."""
    try:
        c = cfg()
        if c.get("enabled") is False:
            return {}
        now = float(now if now is not None else time.time())
        gc = ML.cfg()
        now_et = datetime.fromtimestamp(now, ML._PP()._et_tz())
        if not ML._in_window(now_et, gc):
            return {}
        out_dir = Path(str(c.get("report_dir") or _DEFAULTS["report_dir"]))
        out_dir = out_dir if out_dir.is_absolute() else BASE / out_dir
        if (out_dir / f"SCOREBOARD_{_week_key(now_et)}.md").exists():
            return {}
        if gc.get("enabled") is not False:
            st = ML.Prices(ML.cache_path(gc), network=False, now=now, readonly=True)
            try:
                night = st.state_get("night") or {}
            finally:
                st.close()
            if not (night.get("day") == now_et.date().isoformat() and night.get("done")):
                return {}
        res = weekly(now)
        return {"scoreboard": res} if res.get("ok") or res.get("error") else {}
    except Exception as e:
        _log_once(f"weekly step failed: {type(e).__name__}: {e}")
        return {"scoreboard": {"error": f"{type(e).__name__}: {e}"}}


def http_get(args=None):
    """GET /api/learning/scoreboard[?fresh=1][&format=md] -> (payload, status). Read-only."""
    try:
        c = cfg()
        fresh = str((args or {}).get("fresh") or "") in ("1", "true", "yes")
        age = time.time() - _cache["at"]
        # ?fresh=1 skips the 5-minute cache, but never rebuilds more often than every fresh_floor_seconds
        if (_cache["data"] is None or age > float(c.get("cache_seconds") or 300)
                or (fresh and age >= float(c.get("fresh_floor_seconds", 20) or 0))):
            _cache["data"], _cache["at"] = scoreboard(), time.time()
        data = _cache["data"]
        if str((args or {}).get("format") or "") == "md":
            return {"ok": bool(data.get("ok")), "markdown": render_markdown(data)}, 200 if data.get("ok") else 500
        return data, 200 if data.get("ok") else 500
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Print Mason's scoreboard (read-only).")
    ap.add_argument("--db", help="database file to read (default: the live mason.db)")
    ap.add_argument("--cache", help="price cache file written by mason_learn.py")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.db:
        mason_db.DB_FILE = Path(a.db)
        mason_db.reset_schema_cache()
    _d = scoreboard(cache=a.cache)
    print(json.dumps(_d, indent=2, default=str) if a.json else render_markdown(_d))
