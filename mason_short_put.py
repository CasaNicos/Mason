"""
mason_short_put.py — SHORT CALLS PLACED AS PUTS (Nico's ruling, 2026-11-15; PAPER ONLY; no model call).

Why
---
The director sometimes makes a "short the stock" call (vehicle "stock", direction "short"). Mason never
sells a stock short, so those calls were refused by the single_leg_buy gate, never traded and never
learned from (9 of the first 82 director calls). Nico's ruling: place them as PUT PURCHASES at learning
size, under the option spend table.

What this module does
---------------------
  convert(cfg, rec, ...)   called by mason_autotrade._build for every order it builds. For a director call
                           that is short on the stock itself it chooses ONE put to buy (expiry from the call's
                           horizon, strike by how closely the put follows the stock, liquidity rules, one
                           contract must fit the spend limit) or refuses with a plain reason. Everything after
                           that is the existing, unchanged long-put order path (buy-to-open, limit order).
  choose_put(...)          the judgement, as a pure function over plain data (tested without any network).
  underlying_check(...)    the exit rule: the call's stop and target are prices of the STOCK, so the put is
                           sold when the stock crosses them (mason_agent_loop._decide / _revalidate).
  result(...)              on close: the put's own result next to the stock's move (audit L7).
  director_words(...)      the model-visible sentence, only when Nico has switched it on.

What it never does
------------------
  * It never sells anything to open, never builds a spread: its only output is the description of ONE put
    to BUY. The order itself is built and sent by the existing long-option path in mason_autotrade.
  * It never changes the director's call: extra_json.trade stays exactly as the director wrote it, so the
    honest grade (grade_v2), the calibration and the scorecard keep judging the call on the stock.
  * It never picks a contract the account already holds (so nothing Nico placed can be added to or sold).
  * Robinhood is read-only here (option list, option quotes), through mason_quotes' own guarded reader.

Where the conversion is recorded
--------------------------------
  recommendations.extra_json.execution.short_as_put         the put that was bought, what it was converted
                                                            from, and why that contract (written BEFORE the
                                                            order goes out, and kept: a retry reuses it)
  recommendations.extra_json.execution.short_as_put_shadow  record-only mode: what WOULD have been bought
  agent_trades.features_json.short_as_put                   the same record on the trade
  agent_trades.features_json.short_as_put_result            on close: the put's result and the stock's move

Config (mason.config.json -> autotrade.short_as_put; see DEFAULTS for every number)
----------------------------------------------------------------------------------
  enabled        false   place the put. Off = a short stock call is refused exactly as before.
  shadow         true    while `enabled` is false: when an AUTOMATIC short stock call is refused, work out and
                         record what put would have been bought (a few read-only look-ups). Nothing is placed.
  manual         false   also convert a short stock call that Nico approves BY HAND. Off = a hand approval of
                         such a call is refused exactly as before. (Nothing in Mason sells a trade approved by
                         hand, so such a put would be Nico's to sell before it expires.)
  tell_director  false   model-visible wording for the director (needs Nico's approval of the text).
  tell_quick_checks false  model-visible wording for the quick checks (needs Nico's approval of the text).
With enabled false AND shadow false nothing in this module runs for an order.

Never raises from a public function.
"""

import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta

MODULE = "mason_short_put"
VERSION = 1

_PLACED_STATES = ("placed", "approved_pending_fill", "closed")   # the call's order is out, filled or finished
GATE = "short_as_put"            # a final refusal: this call will not be placed as a put
GATE_WAIT = "short_put_wait"     # "not right now": prices missing or too far apart, stock under the entry price
GATES = (GATE, GATE_WAIT)

DEFAULTS = {
    "enabled": False,
    "shadow": True,
    "manual": False,                  # a short stock call approved BY HAND is bought as a put too (nothing in Mason
                                      # sells a trade approved by hand: that put is then Nico's to manage)
    "tell_director": False,
    "tell_quick_checks": False,
    # ── expiry ──
    # calendar days to expiry Mason aims for, by the call's horizon
    "target_days": {"0-5DTE": 21, "swing": 30, "30d": 60, "90d+": 150},
    "target_days_default": 30,
    "min_days_past_time_stop": 7,     # the put must still be alive this many calendar days after the call's time stop
    "min_days": 14,                   # and never fewer than this many calendar days to expiry
    "max_days_mult": 2.0,             # and never further out than the target x this
    "prefer_monthly": True,           # a standard monthly expiry (third Friday) is preferred ...
    "monthly_window_days": 21,        # ... when it is no more than this many days past the target
    "max_expiries_tried": 3,          # how many expiry dates are looked at before giving up
    # ── earnings ──
    "earnings_days_before": 2,        # an expiry this close before the company's report, or
    "earnings_days_after": 7,         # this close after it, is avoided unless the call is an earnings trade
    "earnings_refuse_sessions": 10,   # a report this many trading sessions away or fewer that the put would still be
                                      # open for: not bought unless the call is an earnings trade (null / 0 = not
                                      # looked at; the same count as autotrade.limits.earnings.option_sessions_before)
    # ── strike (how closely the put follows the stock: 0.50 = at the stock's price, more = in the money) ──
    "delta_target": 0.55,
    "delta_min": 0.45,
    "delta_max": 0.65,
    # ── liquidity ──
    "min_open_interest": 100,         # contracts open
    "max_spread_pct": 15.0,           # (ask - bid) / mid, percent
    # ── the call's own levels ──
    "require_levels": True,           # a short call with no stop or no target is not placed as a put
    "entry_limit_tolerance_pct": 0.5,  # the put is bought only while the stock is at or above the call's entry
                                      # price, less this percent (null = the entry price is not looked at)
    # ── the loss ceiling (percent of the account) when autotrade.limits.max_loss.max_pct is not set ──
    "max_loss_pct_fallback": 2.0,
    # ── data ──
    "quote_batch": 20,                # strikes priced per look-up
    "max_quote_batches": 2,           # per expiry date
    "max_instrument_pages": 6,        # pages of 100 strikes read per expiry date
    "strike_window_pct": 25.0,        # strikes further than this above the stock's price are not read
    "strike_window_low_pct": 10.0,    # ... and the list must reach at least this far below it
    "lookup_budget_s": 25.0,          # most seconds one order may wait for all the look-ups together
    "shadow_lookup_budget_s": 10.0,   # ... and the most a record-only look (nothing is placed) may take
    "cache_s": 240,                   # a choice (or a refusal) is reused for this long
    "risk_free_pct": 4.0,
}

_lock = threading.Lock()
_memo = {}                            # rec id -> (monotonic expiry, underlying price, result)
_day_cache = {"day": None, "expiries": {}, "puts": {}}


# ── small helpers ────────────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$?"
    return f"${v:,.0f}" if abs(v) >= 100 or abs(v - round(v)) < 0.005 else f"${v:,.2f}"


def _g(v) -> str:
    x = _f(v)
    return "?" if x is None else f"{x:g}"


def _AT():
    import mason_autotrade
    return mason_autotrade


def _AL():
    import mason_agent_loop
    return mason_agent_loop


def _MQ():
    import mason_quotes
    return mason_quotes


def _mono() -> float:
    return time.monotonic()


def reset():
    """Forget every cached choice and option list (tests; harmless in production)."""
    with _lock:
        _memo.clear()
        _day_cache.update(day=None, expiries={}, puts={})


# ── config ───────────────────────────────────────────────────────────────────────

def scfg(cfg=None) -> dict:
    """autotrade.short_as_put over DEFAULTS. `cfg` = the autotrade config dict (None = read it)."""
    out = json.loads(json.dumps(DEFAULTS))
    try:
        if cfg is None:
            cfg = _AT()._at_cfg()
        blk = (cfg or {}).get("short_as_put")
        if isinstance(blk, dict):
            for k, v in blk.items():
                if str(k).startswith("_"):
                    continue
                if k == "target_days" and isinstance(v, dict):
                    out["target_days"].update({str(a): b for a, b in v.items() if _f(b) is not None})
                else:
                    out[k] = v
    except Exception:
        pass
    return out


def enabled(cfg=None) -> bool:
    try:
        return scfg(cfg).get("enabled") is True
    except Exception:
        return False


def shadow_on(cfg=None) -> bool:
    """Record-only mode: the rule is off and the record is on."""
    try:
        s = scfg(cfg)
        return s.get("enabled") is not True and s.get("shadow") is True
    except Exception:
        return False


def applies(cfg, rec, *, approver="auto", force=False) -> bool:
    """Would convert() look for a put for this call? True only for a director call that is short on the stock
    itself while the rule is on (or `force`: the record-only look), and, for a call approved by hand, only
    with autotrade.short_as_put.manual. A call that WAS placed as a put (its record is stored and its order
    is out, filled or closed) is always shown as that put, whoever looks and whatever the switches say now:
    it cannot be placed again, so this only makes its preview say what was really bought. Never raises."""
    try:
        if not is_short_stock(rec):
            return False
        if force:
            return True
        ex = rec.get("extra") or {}
        if record_of(ex) is not None and (ex.get("execution") or {}).get("state") in _PLACED_STATES:
            return True
        s = scfg(cfg)
        if s.get("enabled") is not True:
            return False
        return approver == "auto" or s.get("manual") is True
    except Exception:
        return False


# ── what counts, and what was placed ─────────────────────────────────────────────

def is_short_stock(rec) -> bool:
    """A director call that is short on the stock itself (the only thing this module ever converts)."""
    try:
        if not isinstance(rec, dict):
            return False
        if rec.get("kind") != "director" or rec.get("created_by") != "opus_director":
            return False
        if str(rec.get("direction") or "").lower() != "short":
            return False
        tr = (rec.get("extra") or {}).get("trade") or {}
        return str(tr.get("vehicle") or "").lower() == "stock"
    except Exception:
        return False


def record_of(extra) -> dict | None:
    """The stored conversion record of a call (execution.short_as_put), or None."""
    try:
        r = ((extra or {}).get("execution") or {}).get("short_as_put")
        if isinstance(r, dict) and r.get("converted") is True and isinstance(r.get("contract"), dict) \
                and r["contract"].get("strike") is not None and r["contract"].get("expiry"):
            return r
    except Exception:
        pass
    return None


def trade_from(record, director_trade=None) -> dict:
    """The trade block of the put a record describes (what mason_autotrade builds the order from)."""
    c, x = record.get("contract") or {}, record.get("exits") or {}
    return {"vehicle": "put", "strike": _f(c.get("strike")), "expiry": str(c.get("expiry"))[:10],
            "entry_limit": None, "stop": _f(x.get("est_stop_premium")), "target": _f(x.get("est_target_premium")),
            "size_hint": (director_trade or {}).get("size_hint"), "short_as_put": True}


def placed_trade(extra) -> dict:
    """The trade block that was really PLACED for a call: the put when a short call was placed as a put,
    otherwise the call's own trade block, untouched (the same object the callers read before)."""
    tr = (extra or {}).get("trade") or {}
    try:
        r = record_of(extra)
        if r is not None:
            return trade_from(r, tr)
    except Exception:
        pass
    return tr


# ── dates ────────────────────────────────────────────────────────────────────────

def _holidays() -> set:
    try:
        return set(_AL()._holidays())
    except Exception:
        return set()


def _is_session(d: date, hol) -> bool:
    return d.weekday() < 5 and d.isoformat() not in hol


def add_sessions(d: date, n: int, holidays=None) -> date:
    hol = _holidays() if holidays is None else holidays
    n = max(0, int(n))
    while n > 0:
        d += timedelta(days=1)
        if _is_session(d, hol):
            n -= 1
    return d


def is_monthly(d: date, holidays=None) -> bool:
    """A standard monthly expiry: the third Friday, or the Thursday before it when that Friday is a
    market holiday."""
    hol = holidays if holidays is not None else set()
    if d.weekday() == 4 and 15 <= d.day <= 21:
        return True
    nxt = d + timedelta(days=1)
    return d.weekday() == 3 and 15 <= nxt.day <= 21 and nxt.isoformat() in hol


def is_friday_expiry(d: date, holidays=None) -> bool:
    """An ordinary end-of-week expiry: a Friday, or the Thursday before a Friday the market is closed."""
    hol = holidays if holidays is not None else set()
    return d.weekday() == 4 or (d.weekday() == 3 and (d + timedelta(days=1)).isoformat() in hol)


def expiry_window(today: date, horizon, time_stop_sessions: int, s: dict, *, opt_exit_dte=1, holidays=None) -> dict:
    """How far out the put has to expire. Pure.
      time_stop_date  the day the exit manager closes the trade at the latest (the call's time stop)
      min_days        the put must outlive that by min_days_past_time_stop days (and by the day-before-expiry
                      exit), and is never shorter than min_days
      target_days     what Mason aims for, from the call's horizon
      max_days        never further out than this"""
    hol = holidays if holidays is not None else set()
    ts_date = add_sessions(today, int(time_stop_sessions), hol)
    to_ts = (ts_date - today).days
    min_days = max(int(s.get("min_days") or 0), to_ts + int(s.get("min_days_past_time_stop") or 0),
                   to_ts + int(opt_exit_dte or 0) + 1)
    tgt = _f((s.get("target_days") or {}).get(str(horizon)))
    if tgt is None:
        tgt = _f(s.get("target_days_default")) or 30.0
    target = max(int(tgt), min_days)
    max_days = max(int(round(target * float(s.get("max_days_mult") or 1.0))), min_days)
    return {"time_stop_sessions": int(time_stop_sessions), "time_stop_date": ts_date.isoformat(),
            "min_days": min_days, "target_days": target, "max_days": max_days,
            "min_date": (today + timedelta(days=min_days)).isoformat(),
            "max_date": (today + timedelta(days=max_days)).isoformat()}


def near_earnings(expiry: date, earnings, s: dict) -> bool:
    """True when `expiry` falls within a few days of the company's next report (earnings = {"date": ...})."""
    try:
        if not earnings or not earnings.get("date"):
            return False
        rd = date.fromisoformat(str(earnings["date"])[:10])
        return (rd - timedelta(days=int(s.get("earnings_days_before") or 0))) <= expiry <= \
               (rd + timedelta(days=int(s.get("earnings_days_after") or 0)))
    except Exception:
        return False


def sessions_to(today: date, day: date, holidays=None) -> int:
    """Trading sessions after `today` up to and including `day` (0 when `day` is today or earlier). Pure."""
    hol = holidays if holidays is not None else set()
    n, d = 0, today
    while d < day:
        d += timedelta(days=1)
        if _is_session(d, hol):
            n += 1
    return n


def runs_past_report(expiry: date, today: date, earnings, s: dict, holidays=None) -> bool:
    """True when the company's next report is due within earnings_refuse_sessions trading sessions AND a put
    expiring on `expiry` would still be open when it comes out (the report is before the expiry day, or on it
    and not after the close). Pure. No known report date, or the setting at null / 0: False."""
    try:
        n = _f(s.get("earnings_refuse_sessions"))
        if not n or n <= 0 or not earnings or not earnings.get("date"):
            return False
        rd = date.fromisoformat(str(earnings["date"])[:10])
        if rd < today:
            return False
        sess = _f(earnings.get("sessions"))
        if sess is None:
            sess = sessions_to(today, rd, holidays)
        if sess > n:
            return False
        tm = str(earnings.get("timing") or "").lower()
        after_close = tm == "pm" or "after" in tm
        return rd < expiry or (rd == expiry and not after_close)
    except Exception:
        return False


def rank_expiries(expiries, today: date, win: dict, s: dict, *, earnings=None, earnings_trade=False,
                  holidays=None) -> tuple:
    """(ranked, skipped). ranked: the expiry dates that may be used, best first:
    [{"expiry", "days", "monthly"}]; skipped: {"too_soon": n, "too_far": n, "earnings": n, "report_soon": n
    (those of the earnings ones left out because the put would be held through a report due soon)}. Pure.
    Standard expiries first: a monthly expiry (third Friday) no more than monthly_window_days past the
    target; then the end-of-week (Friday) expiries; last the mid-week and other odd dates that only the
    largest names list. Inside each group the date closest to the target wins. An expiry within a few days
    of the earnings report is left out unless the call is an earnings trade; so is every expiry that would
    leave the put open through a report due within earnings_refuse_sessions trading sessions."""
    hol = holidays if holidays is not None else set()
    ok, skipped = [], {"too_soon": 0, "too_far": 0, "earnings": 0, "report_soon": 0}
    seen = set()
    for e in expiries or []:
        try:
            d = date.fromisoformat(str(e)[:10])
        except ValueError:
            continue
        if d in seen:
            continue
        seen.add(d)
        days = (d - today).days
        if days < win["min_days"]:
            skipped["too_soon"] += 1
            continue
        if days > win["max_days"]:
            skipped["too_far"] += 1
            continue
        if not earnings_trade and runs_past_report(d, today, earnings, s, hol):
            skipped["earnings"] += 1
            skipped["report_soon"] += 1
            continue
        if not earnings_trade and near_earnings(d, earnings, s):
            skipped["earnings"] += 1
            continue
        monthly = is_monthly(d, hol)
        std = s.get("prefer_monthly", True) is not False     # one switch for "standard expiries first"
        pref = 0 if (std and monthly and days <= win["target_days"] + int(s.get("monthly_window_days") or 0)) else \
            (1 if (not std or monthly or is_friday_expiry(d, hol)) else 2)
        ok.append({"expiry": d.isoformat(), "days": days, "monthly": monthly, "_pref": pref})
    ok.sort(key=lambda x: (x["_pref"], abs(x["days"] - win["target_days"]), x["days"]))
    return [{k: v for k, v in x.items() if not k.startswith("_")} for x in ok], skipped


# ── option maths (for the two estimated premium levels only; never for an order price) ──

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(spot, strike, years, vol, rate=0.04) -> float:
    """Black-Scholes value of a European put. Pure. At or past expiry, or with no volatility: what the
    put is worth if exercised."""
    spot, strike = float(spot), float(strike)
    if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return max(strike - spot, 0.0)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sq
    d2 = d1 - sq
    return strike * math.exp(-rate * years) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_put_delta(spot, strike, years, vol, rate=0.04):
    """Black-Scholes delta of a put (negative), or None when it cannot be worked out. Pure."""
    try:
        spot, strike = float(spot), float(strike)
        if years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
            return None
        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * math.sqrt(years))
        return _ncdf(d1) - 1.0
    except Exception:
        return None


def premium_if(stock_then, *, mid, stock_now, strike, days, iv=None, delta=None, gamma=None, rate=0.04) -> tuple:
    """(estimated premium of the put if the stock were at `stock_then` today, how it was estimated). Pure.
    AN APPROXIMATION, used only to give the size and value rules a stop and a target in option-price terms:
    it assumes the move happens now, with the option's implied volatility unchanged, and ignores the time
    that passes. The put is never sold on this number (it is sold when the STOCK crosses the call's level).
      1. today's mid plus the change in the put's model value (its own implied volatility), or
      2. without a usable implied volatility: mid + delta x move + half gamma x move squared, or delta only.
    Never below what the put would be worth if exercised at that stock price, never below one cent."""
    mid, s0, s1, k = float(mid), float(stock_now), float(stock_then), float(strike)
    years = max(float(days), 0.0) / 365.0
    vol = _f(iv)
    est, how = None, None
    if vol is not None and 0.01 <= vol <= 10.0 and years > 0:
        est = mid + bs_put(s1, k, years, vol, rate) - bs_put(s0, k, years, vol, rate)
        how = "repriced at the option's own implied volatility, as if the stock moved today"
    else:
        d = _f(delta)
        if d is not None:
            move = s1 - s0
            g = _f(gamma) or 0.0
            if g > 0 and abs(move) > abs(d) / g:      # past the point where this short-cut turns round: stop there
                move = math.copysign(abs(d) / g, move)
            est = mid + d * move + 0.5 * g * move * move
            how = "today's price plus the put's sensitivity to the stock (delta and gamma)" if g else \
                "today's price plus the put's sensitivity to the stock (delta only)"
    if est is None:
        return None, "no implied volatility and no delta: not estimated"
    est = max(est, max(k - s1, 0.0), 0.01)
    return round(est, 2), how


# ── the judgement: which put ─────────────────────────────────────────────────────

def pick_put(rows, s: dict, *, spend_usd, band_pct=10.0, held=()) -> dict:
    """Choose one put from the quoted puts of ONE expiry date. Pure.
    rows: [{"strike", "bid", "ask", "delta", "gamma", "iv", "oi", "volume", "occ", "id"}].
    A put qualifies when it has a real two-sided quote, its delta (how closely it follows the stock) is
    inside the band, at least min_open_interest contracts are open, its bid/ask spread is no wider than
    max_spread_pct of the mid, and the account does not already hold it. Among the qualifying puts of
    which ONE contract fits `spend_usd`, the one whose delta is closest to the target wins (then the more
    open contracts, then the tighter spread).
    Returns {"pick": row | None, "qualifying": n, "cheapest": row | None (cheapest qualifying), "counts"}."""
    lo, hi, tgt = float(s["delta_min"]), float(s["delta_max"]), float(s["delta_target"])
    min_oi, max_spr = float(s.get("min_open_interest") or 0), float(s.get("max_spread_pct") or 0)
    held = {str(h).upper().replace(" ", "") for h in (held or ())}
    counts = {"rows": 0, "no_quote": 0, "no_greeks": 0, "delta": 0, "held": 0, "open_interest": 0, "spread": 0,
              "spend": 0}
    qual = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        counts["rows"] += 1
        bid, ask, k = _f(r.get("bid")), _f(r.get("ask")), _f(r.get("strike"))
        if k is None or k <= 0 or not (bid and bid > 0 and ask and ask > 0 and ask >= bid):
            counts["no_quote"] += 1
            continue
        d = _f(r.get("delta"))
        if d is None:
            counts["no_greeks"] += 1
            continue
        ad = abs(d)
        if not (lo - 1e-9 <= ad <= hi + 1e-9):
            counts["delta"] += 1
            continue
        if str(r.get("occ") or "").upper().replace(" ", "") in held:
            counts["held"] += 1
            continue
        if (_f(r.get("oi")) or 0.0) < min_oi:
            counts["open_interest"] += 1
            continue
        mid = (bid + ask) / 2.0
        spr = (ask - bid) / mid * 100.0
        if max_spr and spr > max_spr + 1e-9:
            counts["spread"] += 1
            continue
        limit = min(ask, mid * (1.0 + float(band_pct) / 100.0))
        qual.append(dict(r, bid=bid, ask=ask, strike=k, mid=round(mid, 4), spread_pct=round(spr, 2),
                         abs_delta=round(ad, 4), est_limit=round(limit, 2), est_cost=round(limit * 100.0, 2)))
    fits = [q for q in qual if q["est_cost"] <= float(spend_usd) + 1e-9]
    counts["spend"] = len(qual) - len(fits)
    pick = min(fits, key=lambda q: (abs(q["abs_delta"] - tgt), -(_f(q.get("oi")) or 0.0), q["spread_pct"],
                                    q["strike"])) if fits else None
    cheapest = min(qual, key=lambda q: q["est_cost"]) if qual else None
    return {"pick": pick, "qualifying": len(qual), "cheapest": cheapest, "counts": counts}


def _exp_words(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except Exception:
        return str(iso)


def _refusal(ticker, e, res, s, spend) -> tuple:
    """(plain reason, can it clear by itself shortly?) for one expiry date on which no put was picked."""
    c = res["counts"]
    when = _exp_words(e["expiry"])
    if res["qualifying"] and res["cheapest"]:
        ch = res["cheapest"]
        return (f"the cheapest suitable put (the {ticker} {_g(ch['strike'])} put expiring {when}) costs about "
                f"{_money(ch['est_cost'])} for one contract, more than the {_money(spend)} Mason may spend on this "
                f"trade"), False
    if c["rows"] == 0:
        return f"no puts near the stock's price are listed for {when}", False
    priced = c["rows"] - c["no_quote"]
    if priced == 0:
        return f"the puts expiring {when} have no buy and sell prices right now", True
    if c["no_greeks"] and priced - c["no_greeks"] == 0:
        return f"the puts expiring {when} carry no price sensitivity figures right now, so none can be chosen", True
    in_band = priced - c["no_greeks"] - c["delta"]
    if in_band <= 0:
        return (f"no put expiring {when} sits close enough to the stock's price (Mason looks for one that moves "
                f"{float(s['delta_min']):.2f} to {float(s['delta_max']):.2f} dollars for each dollar in the stock)"), False
    if c["held"] and in_band - c["held"] <= 0:
        return f"the account already holds the put Mason would pick for {when}, and Mason does not add to it", False
    if c["spread"] and not c["open_interest"]:
        return (f"the buy and sell prices of the puts expiring {when} are more than {float(s['max_spread_pct']):g}% "
                f"apart right now"), True
    if c["open_interest"] and not c["spread"]:
        return (f"too few contracts are open in the puts expiring {when} near the stock's price (fewer than "
                f"{int(float(s['min_open_interest']))})"), False
    return (f"no put expiring {when} near the stock's price is liquid enough (fewer than "
            f"{int(float(s['min_open_interest']))} contracts open, or buy and sell prices more than "
            f"{float(s['max_spread_pct']):g}% apart)"), bool(c["spread"])


def choose_put(*, ticker, today: date, horizon, time_stop_sessions, px, expiries, rows_for, spend_for, s,
               earnings=None, earnings_trade=False, held=(), holidays=None, opt_exit_dte=1, band_pct=10.0) -> dict:
    """The whole choice, pure over what it is given.
      expiries      the expiry dates the option list offers
      rows_for(e)   -> the quoted puts for ranked expiry e ({"expiry", "days", "monthly"}); may raise LookupError
                       with a plain reason when they cannot be read right now
      spend_for(d)  -> the most dollars one contract may cost for an option expiring in d calendar days
    Returns {"ok": True, "pick", "expiry", "window", "tried", "skipped"} or
            {"ok": False, "retry": bool, "detail": plain words, "window", "tried", "skipped"}."""
    hol = holidays if holidays is not None else set()
    win = expiry_window(today, horizon, time_stop_sessions, s, opt_exit_dte=opt_exit_dte, holidays=hol)
    ranked, skipped = rank_expiries(expiries, today, win, s, earnings=earnings, earnings_trade=earnings_trade,
                                    holidays=hol)
    base = {"window": win, "skipped": skipped, "tried": []}
    if not ranked:
        if skipped["report_soon"]:
            return dict(base, ok=False, retry=False, detail=(
                f"{ticker} reports earnings on {_exp_words(earnings.get('date'))}, and a put bought for this call "
                f"would still be held when the report comes out; the call is not an earnings trade"))
        if skipped["earnings"]:
            return dict(base, ok=False, retry=False, detail=(
                f"every expiry date that suits this call falls within a few days of {ticker}'s earnings report on "
                f"{_exp_words(earnings.get('date'))}, and the call is not an earnings trade"))
        if not expiries:
            return dict(base, ok=False, retry=False, detail=f"{ticker} has no listed options")
        return dict(base, ok=False, retry=False, detail=(
            f"{ticker} has no option expiring between {_exp_words(win['min_date'])} and {_exp_words(win['max_date'])} "
            f"(the put has to outlive the call's time stop on {_exp_words(win['time_stop_date'])} by "
            f"{int(s.get('min_days_past_time_stop') or 0)} days)"))
    first, retry_any = None, False
    for e in ranked[:max(1, int(s.get("max_expiries_tried") or 1))]:
        spend = float(spend_for(e["days"]) or 0.0)
        try:
            rows = rows_for(e)
        except LookupError as le:
            why, retry = str(le), True
            base["tried"].append({"expiry": e["expiry"], "days": e["days"], "result": why})
            first = first or why
            retry_any = True
            continue
        res = pick_put(rows, s, spend_usd=spend, band_pct=band_pct, held=held)
        if res["pick"]:
            base["tried"].append({"expiry": e["expiry"], "days": e["days"], "result": "chosen",
                                  "qualifying": res["qualifying"]})
            return dict(base, ok=True, pick=res["pick"], expiry=e, spend_usd=round(spend, 2))
        why, retry = _refusal(ticker, e, res, s, spend)
        base["tried"].append({"expiry": e["expiry"], "days": e["days"], "result": why, "counts": res["counts"]})
        first = first or why
        retry_any = retry_any or retry
    n = len(base["tried"])
    return dict(base, ok=False, retry=retry_any,
                detail=first + (f" ({n - 1} other expiry date{'s were' if n > 2 else ' was'} tried as well)" if n > 1 else ""))


def check_levels(px, stop, target, entry_limit, s: dict, ticker="the stock") -> tuple:
    """(ok, retry, plain reason) — are the call's own levels usable for a put bought now? Pure.
    The stop and the target of a short call are prices of the STOCK: the stop above the price, the target
    below it."""
    px, stop, target, lim = _f(px), _f(stop), _f(target), _f(entry_limit)
    if stop is None or target is None:
        if s.get("require_levels", True) is not False:
            missing = " and no ".join(w for w, v in (("stop", stop), ("target", target)) if v is None)
            return False, False, f"the call gives no {missing}, so the put would have no exit plan"
    if not px or px <= 0:
        return False, True, f"there is no reliable price for {ticker} right now"
    if stop is not None and target is not None and stop <= target:
        return False, False, (f"the call's stop ({_g(stop)}) is not above its target ({_g(target)}): for a short "
                              f"call the stop belongs above the price and the target below it")
    if stop is not None and px >= stop:
        return False, False, (f"{ticker} is at {_g(px)}, already at or above the call's stop ({_g(stop)}): the call "
                              f"is already wrong, or its levels are not stock prices")
    if target is not None and px <= target:
        return False, False, (f"{ticker} is at {_g(px)}, already at or below the call's target ({_g(target)}): "
                              f"nothing is left of the move")
    tol = _f(s.get("entry_limit_tolerance_pct"))
    if lim is not None and lim > 0 and tol is not None and px < lim * (1.0 - tol / 100.0):
        return False, True, (f"{ticker} is at {_g(px)}, below the call's entry price of {_g(lim)}: the put is "
                             f"only bought while the stock is at or above it")
    return True, False, ""


# ── Robinhood (read-only): option list and quotes ────────────────────────────────

class _NoData(LookupError):
    """The option list or its prices cannot be read right now (the reason is plain words)."""


def _rh_words(e) -> str:
    """Why a Robinhood read failed, in plain words, from the KIND of error only (never its text, which could
    hold account details)."""
    n = type(e).__name__
    if isinstance(e, TimeoutError) or "Timeout" in n:
        return "no answer in time"
    if "NeedsLogin" in n:
        return "Mason is signed out of Robinhood"
    if "Forbidden" in n:
        return "that look-up is not allowed"
    try:
        import mason_robinhood
        if mason_robinhood._retry_kind(e) == "429":
            return "Robinhood said there were too many requests"
    except Exception:
        pass
    return "Robinhood did not answer properly"


def _rh_read(tool, args, cfg, deadline):
    """One read through mason_quotes' guarded Robinhood reader (time limit, pause after a failure). Read-only:
    mason_robinhood._call refuses every tool that is not on its read list."""
    MQ = _MQ()
    qc = MQ.qcfg(cfg)
    usable, why = MQ._rh_usable(qc)
    if not usable:
        why = ("Robinhood prices are switched off in the settings" if str(why).startswith("switched off") else
               "Robinhood is paused for a few minutes after an error" if str(why).startswith("paused") else
               "Robinhood is not connected")
        raise _NoData(f"the list of options could not be read from Robinhood right now ({why})")
    left = deadline - _mono()
    if left <= 0.2:
        raise _NoData("the option look-ups took too long")
    full = float(qc.get("rh_timeout_s") or 8)
    wait = min(full, left)
    try:
        data = MQ._rh(tool, args, wait)
        MQ._rh_ok()
        return data
    except Exception as e:
        # Robinhood is only marked as failing (which pauses it for every price check in Mason, the stop checks
        # included) when it had its FULL time to answer and gave none. (review) Any other error belongs to THIS
        # look-up (an option list Robinhood will not serve, signed out, too many requests, a refused argument):
        # the price checks find out about those with their own calls, and a look-up for a put (in record-only
        # mode not even an order) must never pause the prices the stops are judged on. Running out of this
        # order's own time budget says nothing about Robinhood either.
        if isinstance(e, TimeoutError) and wait >= full - 1e-6:
            MQ._rh_fail(e, qc)
        raise _NoData(f"the list of options could not be read from Robinhood right now ({_rh_words(e)})")


def _rows(data, *keys) -> list:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _cache(today: date) -> dict:
    with _lock:
        if _day_cache["day"] != today.isoformat():
            _day_cache.update(day=today.isoformat(), expiries={}, puts={})
        return _day_cache


def read_expiries(ticker, cfg, today: date, deadline) -> list:
    cc = _cache(today)
    if ticker in cc["expiries"]:
        return cc["expiries"][ticker]
    chains = _rows(_rh_read("get_option_chains", {"underlying_symbol": ticker}, cfg, deadline), "chains", "results")
    ok = [ch for ch in chains if str(ch.get("symbol") or "").upper() == ticker
          and (_f(ch.get("trade_value_multiplier")) in (None, 100.0)) and not ch.get("settle_on_open")
          and ch.get("can_open_position") is not False]
    exps = sorted({str(e)[:10] for ch in ok for e in (ch.get("expiration_dates") or [])})
    cc["expiries"][ticker] = exps
    return exps


def _next_cursor(data):
    if not isinstance(data, dict):
        return None
    nxt = data.get("next_cursor") or data.get("next")
    if isinstance(nxt, str) and nxt.startswith("http"):
        try:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(nxt).query).get("cursor")
            return q[0] if q else None
        except Exception:
            return None
    return nxt if isinstance(nxt, (str, int)) and nxt != "" else None


def read_puts(ticker, expiry, px, cfg, s, today: date, deadline) -> list:
    """The standard (x100), active, tradable puts of `ticker` on `expiry`: [{"id", "strike"}], strike ascending.
    Robinhood sends 100 strikes a page in ascending order; reading stops once the strikes in hand reach from
    strike_window_low_pct below the stock's price to strike_window_pct above it (no put Mason wants lies
    outside that). The same test holds if the pages ever came in another order."""
    cc = _cache(today)
    key = (ticker, expiry)
    hi = float(px) * (1.0 + float(s.get("strike_window_pct") or 25.0) / 100.0)
    lo = float(px) * (1.0 - float(s.get("strike_window_low_pct") or 10.0) / 100.0)
    hit = cc["puts"].get(key)
    if hit is not None and (hit["complete"] or (hit["top"] >= hi and hit["bottom"] <= lo)):
        return hit["rows"]
    out, cursor, seen, complete, top, bottom = {}, None, set(), False, 0.0, float("inf")
    for _ in range(max(1, int(s.get("max_instrument_pages") or 6))):
        args = {"chain_symbol": ticker, "expiration_dates": expiry, "type": "put", "state": "active"}
        if cursor:
            args["cursor"] = cursor
        data = _rh_read("get_option_instruments", args, cfg, deadline)
        for r in _rows(data, "instruments", "results"):
            k, mult = _f(r.get("strike_price")), _f(r.get("trade_value_multiplier"))
            if (not r.get("id") or k is None or k <= 0 or r.get("type") != "put"
                    or str(r.get("expiration_date") or "")[:10] != expiry
                    or str(r.get("chain_symbol") or ticker).upper() != ticker
                    or r.get("state") not in (None, "active")
                    or r.get("tradability") not in (None, "tradable")
                    or (mult is not None and abs(mult - 100.0) > 1e-6)):
                continue
            out[round(k, 4)] = {"id": r["id"], "strike": round(k, 4)}
            top, bottom = max(top, k), min(bottom, k)
        nxt = _next_cursor(data)
        if not nxt or nxt in seen:
            complete = True
            break
        if top >= hi and bottom <= lo:             # both sides of the stock's price are in hand, whatever the order
            break
        seen.add(nxt)
        cursor = nxt
    rows = [out[k] for k in sorted(out)]
    cc["puts"][key] = {"rows": rows, "complete": complete, "top": top, "bottom": bottom}
    if not complete and not (top >= float(px) and bottom <= float(px)):
        raise _NoData(f"the list of {ticker} strikes for {_exp_words(expiry)} is too long to read as far as the "
                      f"stock's price")
    return rows


def _parse_quote(r) -> dict | None:
    q = r.get("quote") if isinstance(r.get("quote"), dict) else r
    iid = q.get("instrument_id") or q.get("id")
    if not iid:
        return None
    return {"id": iid, "bid": _f(q.get("bid_price")), "ask": _f(q.get("ask_price")),
            "bid_size": _f(q.get("bid_size")), "delta": _f(q.get("delta")), "gamma": _f(q.get("gamma")),
            "iv": _f(q.get("implied_volatility")), "oi": _f(q.get("open_interest")), "volume": _f(q.get("volume")),
            "quote_ts": q.get("updated_at")}


def read_quotes(ids, cfg, deadline) -> dict:
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 20):
        data = _rh_read("get_option_quotes", {"instrument_ids": ids[i:i + 20]}, cfg, deadline)
        for r in _rows(data, "results"):
            q = _parse_quote(r)
            if q:
                out[q["id"]] = q
    return out


def quoted_puts(ticker, e, px, cfg, s, today: date, deadline, *, max_age_s=None, any_age=False) -> list:
    """The puts of one expiry with their quotes, nearest the stock's price first, in at most
    max_quote_batches look-ups of quote_batch strikes. The first batch is centred just above the stock's
    price (where a put that follows the stock by about half sits); a second batch is read only when the
    first shows that the wanted puts lie further up or further down."""
    AT, MQ = _AT(), _MQ()
    puts = read_puts(ticker, e["expiry"], px, cfg, s, today, deadline)
    if not puts:
        return []
    n = max(1, int(s.get("quote_batch") or 20))
    centre = float(px) * 1.02
    order = sorted(puts, key=lambda r: (abs(r["strike"] - centre), r["strike"]))
    lo, hi = float(s["delta_min"]), float(s["delta_max"])
    got, used = {}, set()
    batch = order[:n]
    for turn in range(max(1, int(s.get("max_quote_batches") or 1))):
        if not batch:
            break
        q = read_quotes([r["id"] for r in batch], cfg, deadline)
        for r in batch:
            used.add(r["strike"])
            if r["id"] in q:
                got[r["strike"]] = dict(q[r["id"]], strike=r["strike"])
        ds = [abs(v["delta"]) for v in got.values() if v.get("delta") is not None]
        if not ds or any(lo <= d <= hi for d in ds):
            break
        rest = [r for r in puts if r["strike"] not in used]
        if max(ds) < lo:                           # every put read follows the stock too little: look higher up
            batch = sorted((r for r in rest if r["strike"] > max(used)), key=lambda r: r["strike"])[:n]
        elif min(ds) > hi:                         # every put read is too deep in the money: look lower down
            batch = sorted((r for r in rest if r["strike"] < min(used)), key=lambda r: -r["strike"])[:n]
        else:
            break
    limit = float(max_age_s if max_age_s is not None else MQ.qcfg(cfg).get("max_age_s") or 180)
    out = []
    for k in sorted(got):
        v = got[k]
        if not any_age:                            # a quote that is old (or undated) is no quote
            ts = MQ.parse_ts(v.get("quote_ts")) if isinstance(v.get("quote_ts"), str) else None
            age = MQ.age_s(ts)
            if age is None or age > limit:
                v = dict(v, bid=None, ask=None)
        if v.get("bid_size") is not None and v["bid_size"] <= 0:
            v = dict(v, bid=None)
        out.append(dict(v, occ=AT.occ_symbol(ticker, e["expiry"], "put", k)))
    return out


# ── sizing: always a Learning-size trade, the whole premium inside the loss ceiling ──

def loss_ceiling_pct(cfg, s=None) -> float:
    try:
        import mason_sizing as SZ
        p = _f((SZ.scfg(cfg)["limits"].get("max_loss") or {}).get("max_pct"))
        if p is not None and p > 0:
            return p
    except Exception:
        pass
    return float((s or scfg(cfg)).get("max_loss_pct_fallback") or 2.0)


def learning_budget(cfg, equity, days, horizon=None, s=None) -> float:
    """The most dollars the put trade may spend: the Learning amount for an option expiring in `days` days
    (autotrade.tiers.learning and autotrade.option_spend, exactly what a put call of the director gets),
    and never more than the loss ceiling on the WHOLE premium (limits.max_loss.max_pct percent of the
    account): what was paid for a put is what can be lost."""
    try:
        AL = _AL()
        eq = _f(equity) or 0.0
        b = _f(AL.size_budget_usd("learning", "put", eq, cfg, horizon=horizon, dte=days)) or 0.0
        if b <= 0:                                 # no Learning size in the settings: the spend table / the put cap alone
            sp = AL.option_spend("learning", days, cfg)
            b = _f(sp["usd"]) if sp else (_f((cfg or {}).get("put_premium_cap_usd")) or 0.0)
            if eq > 0:
                b = min(b or 0.0, eq * 1.5 / 100.0)
        if eq > 0:
            b = min(b, eq * loss_ceiling_pct(cfg, s) / 100.0)
        return max(0.0, float(b))
    except Exception:
        return 0.0


# ── the conversion (called from mason_autotrade._build) ──────────────────────────

def _fail(detail, retry, **kw) -> dict:
    return dict({"ok": False, "gate": GATE_WAIT if retry else GATE, "retry": bool(retry), "detail": str(detail)[:400]}, **kw)


def _stock_px(q) -> float | None:
    if not q or not q.get("ok"):
        return None
    if q.get("legacy"):
        return _f(q.get("last")) or _f(q.get("bid")) or _f(q.get("ask"))
    return _f(q.get("px"))


def _why_text(ticker, c, e, win, s) -> str:
    d = abs(_f(c.get("delta")) or 0.0)
    return (f"{ticker} short call placed as a put: the {_g(c['strike'])} put expiring {_exp_words(c['expiry'])} "
            f"({e['days']} days away{', a standard monthly expiry' if e.get('monthly') else ''}; the call's time stop "
            f"is {_exp_words(win['time_stop_date'])}). It moves about ${d:.2f} for each $1 in the stock, "
            f"{int(_f(c.get('open_interest')) or 0):,} contracts are open, and its buy and sell prices are "
            f"{_f(c.get('spread_pct')) or 0:.1f}% apart. One contract costs about {_money(c.get('est_cost_usd'))}.")


MANUAL_WARNING = ("Approved by hand, this put is yours to manage: Mason does not sell a trade you approve yourself, "
                  "not at the stop, not at the target and not before it expires, and it sends you no reminder before "
                  "it expires either. A put that is still held when it expires, and is in the money then, is turned "
                  "into a short position in the stock.")


def exit_rules(record, built=None, approver="auto") -> dict:
    """The exit rules stored on the call for a short call placed as a put. The option-price stop and target
    are deliberately None: the exit manager judges this trade on the STOCK's price. A call approved by hand
    is not watched by the exit manager at all (as with every hand approval), and the note says so."""
    x, u = record.get("exits") or {}, record.get("underlying") or {}
    t = u.get("symbol") or (record.get("contract") or {}).get("underlying") or "the stock"
    out = {"stop_premium": None, "target_premium": None, "enforced_at_broker": False, "basis": "underlying",
           "underlying": t, "underlying_stop": _f(x.get("stop")), "underlying_target": _f(x.get("target")),
           "est_stop_premium": _f(x.get("est_stop_premium")), "est_target_premium": _f(x.get("est_target_premium"))}
    if approver == "auto":
        out["note"] = (f"Short call placed as a put. Mason sells the put when {t} trades at or above {_g(x.get('stop'))} "
                       f"(the call's stop) or at or below {_g(x.get('target'))} (its target), on the call's time stop, "
                       f"the day before the put expires, or when the director closes the call. These are rules Mason "
                       f"watches itself, not orders at the broker.")
    else:
        out["watched_by_mason"] = False
        out["note"] = (f"Short call placed as a put. The call's own levels: stop when {t} trades at or above "
                       f"{_g(x.get('stop'))}, target when it trades at or below {_g(x.get('target'))}. "
                       + MANUAL_WARNING)
    return out


def journal_words(p) -> str:
    note = str((p.get("exit_rules") or {}).get("note") or "")
    if note.startswith("Short call placed as a put. "):
        note = note[len("Short call placed as a put. "):]
    return ("Short call on the stock, placed as a put purchase. " + note).strip()


def convert(cfg, rec, *, approver="auto", now=None, equity=None, pos=None, mkt_closed=False, force=False,
            providers=None) -> dict | None:
    """The put to buy for a director call that is short on the stock, or why not.
    None                                   the call is not short on the stock, the rule is switched off, or the
                                           call is being approved by hand without autotrade.short_as_put.manual
    {"ok": True, "trade", "record", "budget_cap", "director_trade"}
    {"ok": False, "gate", "retry", "detail"}   refused; `detail` is plain words
    `force` (record-only mode) runs the conversion although the rule is off. `providers` (tests / replay) may
    replace: stock_quote(ticker), expiries(ticker), rows(expiry_info, px), earnings(ticker, today),
    earnings_trade(rec), holidays (set)."""
    try:
        if not applies(cfg, rec, approver=approver, force=force):
            return None
        s = scfg(cfg)
    except Exception:
        return None
    try:
        return _convert(cfg, rec, s, approver, now, equity, pos, bool(mkt_closed), providers or {}, bool(force))
    except Exception as e:                         # fail closed: no put, a reason, and nothing else changes
        return _fail(f"the put for this short call could not be worked out ({type(e).__name__})", True)


def _convert(cfg, rec, s, approver, now, equity, pos, mkt_closed, pv, shadow=False) -> dict:
    AT, AL = _AT(), _AL()
    ex = rec.get("extra") or {}
    tr = ex.get("trade") or {}
    ticker = str(rec.get("ticker") or "").upper()
    now = now or AT._now_et()
    today = now.date() if isinstance(now, datetime) else now
    stop, target, lim = _f(tr.get("stop")), _f(tr.get("target")), _f(tr.get("entry_limit"))
    hol = pv["holidays"] if pv.get("holidays") is not None else _holidays()
    horizon = ex.get("horizon")
    exits_cfg = (cfg or {}).get("exits") or {}

    eq = _f(equity)
    kept = record_of(ex)

    def finish(record):
        days = (date.fromisoformat(str(record["contract"]["expiry"])[:10]) - today).days
        cap = learning_budget(cfg, eq, days, horizon, s)
        if cap <= 0:
            return _fail("no Learning size is set for an option trade (autotrade.tiers.learning / option_spend)", False)
        record = dict(record, budget_cap_usd=round(cap, 2))
        return {"ok": True, "trade": trade_from(record, tr), "record": record, "budget_cap": cap,
                "director_trade": tr}

    # 0. the call's order is already out, filled or finished: it is shown as the put that was bought. Nothing is
    #    looked up or judged again (it cannot be placed a second time: the not_already_placed gate has refused it).
    if kept is not None and (ex.get("execution") or {}).get("state") in _PLACED_STATES:
        return finish(kept)

    # 1. the stock's price and the call's own levels (checked on every build, never cached)
    if (stop is None or target is None) and s.get("require_levels", True) is not False:
        return _fail(check_levels(1.0, stop, target, None, s, ticker)[2], False)      # no look-up at all for this one
    q = pv["stock_quote"](ticker) if pv.get("stock_quote") else AT._trusted_quote(ticker, False, cfg, any_age=mkt_closed)
    px = _stock_px(q)
    ok, retry, why = check_levels(px, stop, target, lim, s, ticker)
    if not ok:
        return _fail(why, retry)
    if not eq or eq <= 0:
        return _fail("the account value could not be read, so the put cannot be sized", True)
    held = {str(p_.get("symbol") or "").upper().replace(" ", "") for p_ in ((pos or {}).get("positions") or [])}

    # 2. a put already chosen for this call (written before its order went out) is kept: never a second contract
    if kept is not None:
        try:
            if date.fromisoformat(str(kept["contract"]["expiry"])[:10]) <= today:
                return _fail("the put chosen for this call earlier has expired", False)
        except ValueError:
            return _fail("the put chosen for this call earlier cannot be read", False)
        return finish(kept)

    # 3. a choice made moments ago is reused (the queue retries every tick; a preview is followed by an approve)
    rid = rec.get("id")
    with _lock:
        m = _memo.get(rid) if rid else None
    if m and _mono() < m[0] and abs(px - m[1]) <= 0.015 * m[1]:
        return finish(m[2]["record"]) if m[2].get("ok") else dict(m[2])

    # 4. choose
    deadline = _mono() + max(1.0, float((s.get("shadow_lookup_budget_s") if shadow else None)
                                        or s.get("lookup_budget_s") or 25.0))
    sessions = AL._time_stop_sessions(cfg, horizon, ex)
    try:
        import mason_sizing as SZ
        earn = pv["earnings"](ticker, today) if pv.get("earnings") else SZ.next_earnings(ticker, today, hol)
        etrade = pv["earnings_trade"](rec) if pv.get("earnings_trade") else SZ.is_earnings_trade(rec, SZ.scfg(cfg))
    except Exception:
        earn, etrade = None, False
    try:
        band = float(_MQ().qcfg(cfg).get("opt_limit_band_pct") or 10.0)
    except Exception:
        band = 10.0
    try:
        expiries = pv["expiries"](ticker) if pv.get("expiries") else read_expiries(ticker, cfg, today, deadline)
    except LookupError as le:
        return _fail(str(le), True)

    def rows_for(e):
        if pv.get("rows"):
            return pv["rows"](e, px)
        return quoted_puts(ticker, e, px, cfg, s, today, deadline, any_age=mkt_closed)

    ch = choose_put(ticker=ticker, today=today, horizon=horizon, time_stop_sessions=sessions, px=px,
                    expiries=expiries, rows_for=rows_for,
                    spend_for=lambda d: learning_budget(cfg, eq, d, horizon, s), s=s, earnings=earn,
                    earnings_trade=bool(etrade), held=held, holidays=hol,
                    opt_exit_dte=int(exits_cfg.get("opt_exit_dte", 1) or 0), band_pct=band)
    why_base = {"horizon": horizon, **ch["window"], "expiries_listed": len(expiries or []),
                "expiries_skipped": ch["skipped"], "earnings_date": (earn or {}).get("date"),
                "earnings_sessions_away": (earn or {}).get("sessions"),
                "earnings_trade": bool(etrade), "delta_target": float(s["delta_target"]),
                "delta_band": [float(s["delta_min"]), float(s["delta_max"])],
                "min_open_interest": float(s.get("min_open_interest") or 0),
                "max_spread_pct": float(s.get("max_spread_pct") or 0), "tried": ch["tried"]}
    if not ch["ok"]:
        out = _fail(ch["detail"], ch["retry"], why=why_base)
        _remember(rid, px, out, s)
        return out
    p, e = ch["pick"], ch["expiry"]
    rate = float(s.get("risk_free_pct") or 0.0) / 100.0
    kw = dict(mid=p["mid"], stock_now=px, strike=p["strike"], days=e["days"], iv=p.get("iv"), delta=p.get("delta"),
              gamma=p.get("gamma"), rate=rate)
    est_stop, how = premium_if(stop, **kw) if stop is not None else (None, None)
    est_tgt, how_t = premium_if(target, **kw) if target is not None else (None, None)
    contract = {"occ": p.get("occ") or AT.occ_symbol(ticker, e["expiry"], "put", p["strike"]), "underlying": ticker,
                "right": "put", "strike": p["strike"], "expiry": e["expiry"], "days_to_expiry": e["days"],
                "monthly": bool(e.get("monthly")), "delta": p.get("delta"), "gamma": p.get("gamma"), "iv": p.get("iv"),
                "open_interest": p.get("oi"), "volume": p.get("volume"), "bid": p["bid"], "ask": p["ask"],
                "mid": p["mid"], "spread_pct": p["spread_pct"], "est_limit": p["est_limit"],
                "est_cost_usd": p["est_cost"], "quote_source": "robinhood" if not pv.get("rows") else "provided",
                "market_open": not mkt_closed}
    record = {"v": VERSION, "converted": True, "at": datetime.now().isoformat(timespec="seconds"),
              "from": {"vehicle": "stock", "direction": "short", "entry_limit": lim, "stop": stop, "target": target},
              "contract": contract,
              "underlying": {"symbol": ticker, "px": round(px, 4), "source": (q or {}).get("source")},
              "exits": {"basis": "underlying", "stop": stop, "target": target, "est_stop_premium": est_stop,
                        "est_target_premium": est_tgt, "est_method": how or how_t},
              "why": dict(why_base, spend_limit_usd=ch.get("spend_usd"),
                          text=_why_text(ticker, contract, e, ch["window"], s))}
    out = finish(record)
    _remember(rid, px, out, s)
    return out


def _remember(rid, px, out, s):
    if not rid:
        return
    try:
        ttl = float(s.get("cache_s") or 0)
        if ttl > 0:
            with _lock:
                if len(_memo) > 200:
                    _memo.clear()
                _memo[rid] = (_mono() + ttl, float(px), out)
    except Exception:
        pass


# ── record-only mode ─────────────────────────────────────────────────────────────

def shadow_record(preview: dict, plain_reasons=None) -> dict:
    """What a forced build says the put order would have been, in the compact form kept on the call."""
    rec = preview.get("short_as_put") or {}
    order, sizing = preview.get("order") or {}, preview.get("sizing") or {}
    blocked = [str(b) for b in (preview.get("blocked") or [])]
    other = [b for b in blocked if b.split(":", 1)[0].strip() not in ("approve_enabled",)]
    out = {"v": VERSION, "at": datetime.now().isoformat(timespec="seconds"), "placed": False,
           "would_place": bool(order) and not other,
           "summary": str(preview.get("summary") or "")[:300],
           "contract": rec.get("contract"), "from": rec.get("from"), "underlying": rec.get("underlying"),
           "exits": rec.get("exits"), "why": rec.get("why"),
           "qty": order.get("qty"), "limit": order.get("limit_price"), "est_cost": order.get("est_cost"),
           "budget": sizing.get("budget"),
           "not_placed_because": list(plain_reasons if plain_reasons is not None else other)[:8]}
    return {k: v for k, v in out.items() if v is not None}


# ── exits: the call's stop and target are prices of the STOCK ────────────────────

def levels_of(features) -> dict | None:
    """{"ticker", "stop", "target"} for a trade whose exits are judged on the stock's price, from the trade's
    own stored record (so it keeps working after the rule is switched off), else None."""
    try:
        r = (features or {}).get("short_as_put")
        if not isinstance(r, dict) or r.get("converted") is not True:
            return None
        x = r.get("exits") or {}
        if x.get("basis") != "underlying":
            return None
        stop, target = _f(x.get("stop")), _f(x.get("target"))
        t = str((r.get("underlying") or {}).get("symbol") or (r.get("contract") or {}).get("underlying") or "").upper()
        if not t or (stop is None and target is None):
            return None
        return {"ticker": t, "stop": stop, "target": target}
    except Exception:
        return None


def underlying_check(q, stop, target) -> tuple:
    """(stop_hit, target_hit) for a SHORT view judged on the stock's quote `q` (mason_quotes.stock_quote):
    each True, False, or None = cannot be judged right now (no trustworthy price, or no such level). None
    never triggers and never withdraws anything. The mirror image of the stock rule in
    mason_agent_loop._check_levels: the stop is hit when the stock trades at or ABOVE it (and, with a live
    two-sided market, the ask is there too: one bad print cannot sell); the target when it trades at or
    BELOW it (and the bid is there too)."""
    s = t = None
    stop, target = _f(stop), _f(target)
    if not q:
        return s, t
    if q.get("legacy"):
        if q.get("ok"):
            sp, tp = (q.get("ask") or q.get("last")), (q.get("last") or q.get("ask"))
            if stop is not None and sp is not None:
                s = sp >= stop
            if target is not None and tp is not None:
                t = tp <= target
        return s, t
    if not (q.get("ok") and q.get("px")):
        return s, t
    two = bool(q.get("two_sided") and q.get("bid") and q.get("ask"))
    if stop is not None:
        s = q["px"] >= stop and (not two or q["ask"] >= stop)
    if target is not None:
        t = q["px"] <= target and (not two or q["bid"] <= target)
    return s, t


def result(record, row, closed, reason, stock_quote) -> dict:
    """On close: the put's own result next to what the stock did, so the choice of vehicle can be judged
    apart from the call (audit L7). `closed` = the fields mason_agent_loop._finalize just wrote."""
    u0 = _f((record.get("underlying") or {}).get("px"))
    u1 = _stock_px(stock_quote) if stock_quote else None
    move = ((u1 / u0 - 1.0) * 100.0) if (u0 and u1) else None
    out = {"v": VERSION, "exit_reason": reason,
           "put_entry_px": _f(row.get("entry_px")), "put_exit_px": _f(closed.get("exit_px")),
           "put_pnl_usd": _f(closed.get("pnl_usd")), "put_pnl_pct": _f(closed.get("pnl_pct")),
           "stock_px_when_chosen": u0, "stock_px_at_exit": u1,
           "stock_move_pct": round(move, 3) if move is not None else None,
           # what selling the stock short would have made over the same days, before costs
           "stock_short_return_pct": round(-move, 3) if move is not None else None,
           "delta_when_chosen": _f((record.get("contract") or {}).get("delta")),
           "days_to_expiry_when_chosen": (record.get("contract") or {}).get("days_to_expiry")}
    pnl = out["put_pnl_usd"]
    if move is not None and pnl is not None:       # was the call right while the put was held, and did the put pay?
        fell, paid = move < 0, pnl > 0
        out["stock_fell"], out["put_made_money"] = fell, paid
        out["call_and_put"] = ("the stock fell and the put made money" if fell and paid else
                               "the stock fell but the put lost money" if fell else
                               "the stock did not fall but the put made money" if paid else
                               "the stock did not fall and the put lost money")
    try:
        d0 = date.fromisoformat(str(row.get("entry_ts"))[:10])
        d1 = date.fromisoformat(str(closed.get("exit_ts"))[:10])
        out["days_held"] = (d1 - d0).days
    except Exception:
        pass
    return out


def other_order(rec_id, symbol, created_iso=None):
    """An order of this call that sits on a DIFFERENT contract and was filled or is still open, or None.
    A second safeguard behind the stored record: a call is never bought on two contracts."""
    try:
        AT = _AT()
        params = {"status": "all", "limit": 200, "nested": "true"}
        code, d = AT._get("/orders", params=params)
        if code != 200 or not isinstance(d, list):
            return None
        pre = f"mason-{AT._seed(rec_id)}-"
        dead = ("canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced")
        for o in d:
            if not str(o.get("client_order_id") or "").startswith(pre):
                continue
            if str(o.get("symbol") or "").upper() == str(symbol).upper():
                continue
            if (_f(o.get("filled_qty")) or 0) > 0 or (o.get("status") or "").lower() not in dead:
                return o
    except Exception:
        pass
    return None


# ── model-visible wording (each behind its own switch; off = the text is not touched) ──

DIRECTOR_OLD = "A short view is expressed by buying a put (short stock is refused)."
# The three texts below are the wording Nico approved on 2026-11-17 at 15:00 ET ("slightly less formal and more
# friendly"; docs/fable/SHORTPUTS_PROMPTS_FOR_NICO.md). Each is shown only behind its own switch.
DIRECTOR_NEW = ("If you think a stock is going down, buy a put. Can't find a good put in chain_sample? Then just make "
                "the call short with vehicle \"stock\" and give your \"stop\" and \"target\" as SHARE prices (stop "
                "above the current price, target below it; \"entry_limit\" is optional and is the lowest share price "
                "you'd still enter at). Mason will pick a put for you: strike around the stock's price or a little in "
                "the money, expiring after your horizon, in a small learning size. It sells the put once the stock "
                "reaches your stop or your target. You're still graded on what the stock does. Mason never shorts the "
                "stock itself.")


def _sold_once(lv, whose) -> str:
    """'XYZ trades at 105 or higher (your stop) or 90 or lower (your target)'; a level the call does not have is
    left out (possible only with short_as_put.require_levels switched off), never written as '?'."""
    parts = []
    if lv.get("stop") is not None:
        parts.append(f"{_g(lv['stop'])} or higher ({whose} stop)")
    if lv.get("target") is not None:
        parts.append(f"{_g(lv['target'])} or lower ({whose} target)")
    return f"{lv['ticker']} trades at " + " or ".join(parts)


def director_words(system_text: str, cfg=None) -> str:
    """The director's rules with the short-call sentence replaced — only when autotrade.short_as_put.enabled
    AND .tell_director are both true and the old sentence is there exactly once. Otherwise the text comes
    back untouched (the same object)."""
    try:
        s = scfg(cfg)
        if s.get("enabled") is True and s.get("tell_director") is True and system_text.count(DIRECTOR_OLD) == 1:
            return system_text.replace(DIRECTOR_OLD, DIRECTOR_NEW)
    except Exception:
        pass
    return system_text


NO_PUT_WORDS = "Heads up: Mason didn't buy a put for this short call"   # (review) without the time of the attempt
_NO_PUT_MAX = 300                       # the most characters of the reason the director is shown


def _no_put_reason(e) -> str:
    """(review 2026-11-18) The reason stored on a refused call, in words fit for the director: the put search's own
    words first, then the plain reason Mason stored for the inbox (blocked_reason, built by
    mason_autotrade.plain_block_reasons). The queue's own text (queue_block_reason) is never passed on as it is:
    its one known case, "stale — queued call older than N trading days", is said in plain words, anything else is
    left out. A setting's name or an internal error's name in brackets is dropped. Over 300 characters, whole
    reasons are kept (cut at "; "), or the one long reason is cut at a word and ends with "…"."""
    why = ""
    for g in e.get("blocked_gates") or []:
        if isinstance(g, dict) and g.get("gate") in GATES and str(g.get("detail") or "").strip():
            why = str(g["detail"])
            break
    why = why or str(e.get("blocked_reason") or "")
    if not why.strip() and e.get("state") == "queue_blocked":
        m = re.match(r"\s*stale\b.*?(\d+) trading days", str(e.get("queue_block_reason") or ""))
        if m:
            why = f"it was still waiting to be placed more than {m.group(1)} trading days after the call"
    why = " ".join(why.split())
    why = re.sub(r"\s*\((?:[a-z_]+\.)+[a-z_]+[^)]*\)", "", why)          # (autotrade.tiers.learning / option_spend)
    why = re.sub(r"\s*\([A-Za-z]*(?:Error|Exception)\)", "", why)         # (KeyError)
    if len(why) > _NO_PUT_MAX:
        out = ""
        for part in why.split("; "):
            cand = part if not out else out + "; " + part
            if len(cand) > _NO_PUT_MAX:
                break
            out = cand
        why = out or (why[:_NO_PUT_MAX - 2].rsplit(" ", 1)[0].rstrip(" .;:,") + " …")
    return why.rstrip(" .;:,")


def no_put_words(rec, cfg=None):
    """(Nico 2026-11-18, his choice "B") For the director's list of its own open calls: a short call on a stock
    that Mason's automatic placing refused, so no put was bought. One sentence with the reason, in the plain words
    already stored on the call, or None (= the call's line stays as it is). Only with enabled AND tell_director;
    never for a call whose put was placed; nothing for a call that was never tried (no refusal on record).
    (review 2026-11-18) Nothing either for a call refused while the rule did not apply to it (its refusal names the
    single_leg_buy check: refused as short stock, before short calls were placed as puts), and the sentence does
    not say Mason looked for a put: some refusals (the call too old, automatic placing off, the account unreadable,
    the market closed) come before any look-up. It says when Mason tried, so "right now" and "today" in a reason
    read true at a later review."""
    try:
        s = scfg(cfg)
        if not (s.get("enabled") is True and s.get("tell_director") is True):
            return None
        if not is_short_stock(rec):
            return None
        ex = rec.get("extra") or {}
        e = ex.get("execution") or {}
        if not isinstance(e, dict) or e.get("state") not in ("blocked", "queue_blocked"):
            return None
        if record_of(ex) is not None:                       # a put was chosen and written down: not this case
            return None
        if any(isinstance(g, dict) and g.get("gate") == "single_leg_buy" for g in e.get("blocked_gates") or []):
            return None                                     # refused as short stock: the rule did not apply then
        why = _no_put_reason(e)
        if not why:
            return None
        at = e.get("queue_blocked_at") if e.get("state") == "queue_blocked" else e.get("blocked_at")
        try:                                                # Mason's clock is US Eastern (naive local time)
            d = datetime.fromisoformat(str(at or "")[:19])
            why = why.replace(" right now", "")             # the time is said: "right now" would read as today
            return (f"Heads up: when Mason tried to place this short call on {d:%b} {d.day} at {d:%H:%M} ET, it "
                    f"didn't buy a put because {why}. So nothing is held for it.")
        except Exception:
            return f"{NO_PUT_WORDS} because {why}. So nothing is held for it."
    except Exception:
        return None


def book_words(features, cfg=None):
    """For the director's list of open positions: the words that replace "stop X / target Y" on a put that
    came from a short call, or None (= leave the line as it is). Only with enabled AND tell_director."""
    try:
        s = scfg(cfg)
        if not (s.get("enabled") is True and s.get("tell_director") is True):
            return None
        lv = levels_of(features)
        if not lv:
            return None
        return f"your short call, held as a put · Mason sells it once {_sold_once(lv, 'your')}"
    except Exception:
        return None


def check_words(features, cfg=None):
    """For the quick checks' line about one of Mason's positions: the sentence that replaces "Stop ..., target
    ... (option price)" on a put that came from a short call, or None. Only with enabled AND
    tell_quick_checks."""
    try:
        s = scfg(cfg)
        if not (s.get("enabled") is True and s.get("tell_quick_checks") is True):
            return None
        lv = levels_of(features)
        if not lv:
            return None
        both = lv.get("stop") is not None and lv.get("target") is not None
        return (f"Mason bought this put because of a short call on {lv['ticker']} shares. It gets sold once "
                f"{_sold_once(lv, 'the')}. Heads up: "
                + ("those two are share prices, not option prices." if both else "that is a share price, not an option price."))
    except Exception:
        return None
