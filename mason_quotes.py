"""
mason_quotes.py — ONE trustworthy price for a stock and ONE for an option (read-only; no LLM).

Why this exists (audit 2026-11-13, X1 / X2 / X14)
-------------------------------------------------
The free Alpaca plan serves single-venue (IEX) stock quotes and "indicative" option quotes. A thin
IEX quote can sit several percent away from the real market (JAZZ: bid 223.63 / ask 239.37 with the
last trade at 238.74), and the agent loop used that bid for stops and for exit limits. Robinhood's
read-only tools give the real consolidated quote, so every stop/target trigger and every entry/exit
limit of the loop now asks this module for its price.

Order of sources (first one that passes every check wins)
---------------------------------------------------------
  stock  : Robinhood quote (last trade; quote mid when the last trade is old but the quote is tight)
           → Alpaca last trade → Alpaca latest minute-bar close
           → Alpaca quote mid, only when that quote is two-sided, fresh and tight.
  option : Robinhood option quote (by instrument id; OCC → id is cached)
           → Alpaca indicative quote, only when two-sided, fresh and inside the spread limit.
A Robinhood price is cross-checked against Alpaca's last trade / minute bar (stocks) or Alpaca's
mid (options) when Alpaca has something recent; if the two disagree by more than the configured
percent, NEITHER is trusted and the answer is "no price" (the caller does nothing this tick).

A quote is rejected when it is stale, has no timestamp, has a zero or missing side, is crossed, or
is wider than the configured percent. A rejected bid/ask is never returned in `bid`/`ask`/`mid`
(they stay None), so no caller can price off it by accident.

What the callers get
--------------------
  {ok, symbol, kind, source, px, px_src, bid, ask, mid, last, ts, age_s, two_sided, spread_pct,
   xcheck, relaxed, reason, tried}
    px        the price to trigger on: stock = last trade (or minute-bar close / tight mid);
              option = mid of a validated two-sided quote.
    two_sided True only when bid and ask passed every check (then bid/ask/mid are set).
    reason    plain words when ok is False.

Modes
-----
  strict (default)  stops, targets, entries.
  relaxed=True      exits that must happen anyway (flat-by time, day before expiry, director close,
                    time stop): an older price is accepted (quotes.relaxed_max_age_s), a wide option
                    quote is accepted as long as it has a real bid, and a Robinhood/Alpaca
                    disagreement does not veto Robinhood.
  any_age=True      preview while the market is closed (nothing can be placed then): age is ignored.

Fail-safe
---------
  * autotrade.quotes.enabled = false  → the old Alpaca-only quote, marked legacy=True; the callers
    then use their old rules unchanged.
  * Robinhood off, logged out, slow or rate-limited → Alpaca data with the same checks. One Robinhood
    answer is waited for at most quotes.rh_timeout_s; after a failure Robinhood is skipped for
    quotes.rh_retry_after_s (doubling up to 15 minutes), so a dead connection cannot stall the tick.
  * Nothing here ever raises, places, cancels or writes anything.

All I/O goes through module hooks (_alpaca_get, _rh_call, _rh_enabled, _wall) so tests can fake them.
"""

import re
import threading
import time
from datetime import datetime, timezone

DEFAULTS = {
    "enabled": True,                  # false = old Alpaca-only quotes and old limit prices
    "use_robinhood": True,            # false = Alpaca data only (still validated)
    "max_age_s": 180,                 # older than this = not used for a stop, a target or an entry
    "relaxed_max_age_s": 900,         # age accepted for exits that must happen anyway
    "stock_max_spread_pct": 1.5,      # a stock bid/ask wider than this is ignored (last trade is used)
    "opt_max_spread_pct": 35.0,       # an option quote wider than this cannot fire a stop or a target
    "opt_entry_max_spread_pct": 25.0, # ... and wider than this blocks a new option entry
    "stock_max_divergence_pct": 2.0,  # Robinhood vs Alpaca last trade: more than this = no price
    "opt_max_divergence_pct": 25.0,   # Robinhood mid vs Alpaca mid: more than this = no price
    "xcheck_max_age_s": 60,           # Alpaca data older than this is not used for the cross-check
    "stock_limit_band_pct": 0.5,      # entry: last + band; exit: last − band (per attempt)
    "stock_limit_band_max_pct": 2.0,  # the band never grows past this on re-priced exits
    "opt_limit_band_pct": 10.0,       # entry: mid + band (never above the ask); exit: mid − band (never below the bid)
    "rh_retry_after_s": 120,          # pause after a Robinhood failure (doubles up to 900 s)
    "rh_timeout_s": 8,                # longest the loop waits for one Robinhood answer
}

_OPT_ABS_DIVERGENCE = 0.05            # options: a 5-cent difference is never a disagreement
_FUTURE_TOL_S = 10.0                  # a timestamp further in the future than this is unusable
_SKEW_MIN_S = 5.0                     # clock differences below this are network latency, ignored
_NEG_ID_TTL_S = 600.0                 # how long "no Robinhood contract for this OCC" is remembered
_ASK_BUFFER = 0.001                   # stock entry: 0.1% over a validated ask (unchanged old rule)
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

_lock = threading.Lock()
_occ_ids = {}                         # OCC -> (instrument id | None, monotonic expiry | None)
_breaker = {"fails": 0, "until": 0.0, "last_error": None}
_skew = {"s": 0.0}                    # this computer's clock minus the broker's clock, seconds
_state = {"rh_ok": 0, "rh_fail": 0, "robinhood": 0, "alpaca": 0, "rejected": 0, "legacy": 0}


# ── hooks (replaced in tests) ────────────────────────────────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _alpaca_get(url, params=None):
    return _AT()._data_get(url, params)


def _rh_call(tool, args):
    import mason_robinhood
    return mason_robinhood._call(tool, args, lane="live")


def _rh_enabled() -> bool:
    """Robinhood is used only when the SAME config file autotrade reads has robinhood.enabled, and
    the Robinhood module agrees. (A test config without a robinhood block never reaches Robinhood.)"""
    try:
        if not ((_AT()._cfg().get("robinhood") or {}).get("enabled")):
            return False
        import mason_robinhood
        return bool(mason_robinhood.enabled())
    except Exception:
        return False


def _wall() -> float:
    return time.time()


# ── small pure helpers ───────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def qcfg(cfg=None) -> dict:
    """The autotrade.quotes block over DEFAULTS. `cfg` is an autotrade config dict (or None = read it)."""
    out = dict(DEFAULTS)
    try:
        src = cfg if cfg is not None else _AT()._at_cfg()
        for k, v in ((src or {}).get("quotes") or {}).items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def parse_ts(s):
    """ISO-8601 / RFC3339(Nano) text → epoch seconds, or None when missing or unreadable.
    'Z' is UTC, an offset is honoured, a fraction is cut to microseconds, a naive time is UTC."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip()
    if t[-1] in "Zz":
        t = t[:-1] + "+00:00"
    m = re.search(r"\.(\d+)", t)
    if m:
        t = t[:m.start(1)] + m.group(1)[:6].ljust(6, "0") + t[m.end(1):]
    try:
        dt = datetime.fromisoformat(t)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds") if ts else None
    except Exception:
        return None


def note_broker_time(ts_text):
    """Remember how far this computer's clock is from the broker's own clock (its /clock timestamp).
    Returns the difference in seconds (local minus broker), 0.0 when it is only network latency."""
    t = parse_ts(ts_text)
    if t is None:
        return None
    d = _wall() - t
    _skew["s"] = d if (_SKEW_MIN_S <= abs(d) < 86400.0) else 0.0
    return _skew["s"]


def age_s(ts):
    """Seconds since `ts`, read the CAUTIOUS way: if this computer's clock runs behind the broker's,
    the difference is added (a quote is never judged younger than it is). None = unusable timestamp."""
    if ts is None:
        return None
    a = _wall() + max(0.0, -_skew["s"]) - float(ts)
    if a < -_FUTURE_TOL_S:
        return None
    return max(0.0, a)


def parse_occ(occ):
    """'SMCI261204P00026000' → {underlying, expiry 'YYYY-MM-DD', type 'put', strike 26.0} or None."""
    m = _OCC_RE.match(str(occ or "").upper().strip())
    if not m:
        return None
    try:
        exp = datetime(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4))).date().isoformat()
    except ValueError:
        return None
    return {"underlying": m.group(1), "expiry": exp, "type": "call" if m.group(5) == "C" else "put",
            "strike": int(m.group(6)) / 1000.0}


def check_two_sided(bid, ask, ts, max_spread_pct, max_age, any_age=False, bid_size=None):
    """(ok, spread_pct, age_s, why). ok only when: a real bid, a real ask, not crossed, fresh, and
    (when a limit is given) no wider than max_spread_pct of the mid."""
    a = age_s(ts)
    if not (bid and bid > 0) or (bid_size is not None and bid_size <= 0):
        return False, None, a, "no bid"
    if not (ask and ask > 0):
        return False, None, a, "no ask"
    if ask < bid:
        return False, None, a, "crossed (ask below bid)"
    spr = (ask - bid) / ((ask + bid) / 2.0) * 100.0
    if not any_age:
        if a is None:
            return False, spr, None, "no usable timestamp"
        if a > max_age:
            return False, spr, a, f"stale ({a:.0f}s old)"
    if max_spread_pct is not None and spr > float(max_spread_pct) + 1e-9:
        return False, spr, a, f"spread {spr:.1f}% is over the {float(max_spread_pct):g}% limit"
    return True, spr, a, None


def _px_round(v):
    v = float(v)
    return round(v, 2) if v >= 1.0 else round(v, 4)


def _blank(sym, kind, reason=None, tried=None, relaxed=False):
    return {"ok": False, "symbol": sym, "kind": kind, "source": None, "px": None, "px_src": None,
            "bid": None, "ask": None, "mid": None, "last": None, "ts": None, "age_s": None,
            "two_sided": False, "spread_pct": None, "xcheck": None, "relaxed": bool(relaxed),
            "reason": reason, "tried": list(tried or [])}


def _result(sym, kind, c, relaxed, tried):
    a = age_s(c.get("ts"))
    out = _blank(sym, kind, None, tried, relaxed)
    out.update({"ok": True, "source": c["source"], "px": c["px"], "px_src": c["px_src"],
                "bid": c.get("bid"), "ask": c.get("ask"), "mid": c.get("mid"), "last": c.get("last"),
                "ts": _iso(c.get("ts")), "age_s": round(a, 1) if a is not None else None,
                "two_sided": bool(c.get("two_sided")),
                "spread_pct": c.get("spread_pct"), "xcheck": c.get("xcheck")})
    for k in ("xcheck_ref", "xcheck_div_pct", "last_age_s", "iv", "delta", "oi", "volume",
              "bid_size", "ask_size", "wide", "instrument_id"):
        if c.get(k) is not None:
            out[k] = c[k]
    return out


def reset():
    """Forget every cache and the Robinhood pause (tests; also harmless in production)."""
    with _lock:
        _occ_ids.clear()
    _breaker.update(fails=0, until=0.0, last_error=None)
    _skew["s"] = 0.0
    for k in _state:
        _state[k] = 0


# ── Robinhood availability ───────────────────────────────────────────────────────

def _rh_usable(qc):
    """(usable, why_not)."""
    if not qc.get("use_robinhood", True):
        return False, "switched off (quotes.use_robinhood)"
    if time.monotonic() < _breaker["until"]:
        return False, f"paused after an error ({_breaker['last_error']})"
    if not _rh_enabled():
        return False, "not connected"
    return True, None


def _rh_ok():
    _state["rh_ok"] += 1
    if _breaker["fails"]:
        _breaker.update(fails=0, until=0.0)


def _rh(tool, args, timeout):
    """One Robinhood read, waited for at most `timeout` seconds. The call itself (mason_robinhood._call:
    rate limiter, reconnect, one retry) can take much longer when the connection is down; the loop's
    tick must not wait for that, so it runs in a helper thread that is simply left behind on a timeout."""
    box = {}

    def run():
        try:
            box["v"] = _rh_call(tool, args)
        except BaseException as e:                 # noqa: BLE001 — handed to the caller below
            box["e"] = e

    t = threading.Thread(target=run, name="mason-quotes-rh", daemon=True)
    t.start()
    t.join(max(0.05, float(timeout)))
    if t.is_alive():
        raise TimeoutError(f"no Robinhood answer within {float(timeout):g}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _rh_fail(e, qc):
    _state["rh_fail"] += 1
    _breaker["fails"] += 1
    base = max(1.0, float(qc.get("rh_retry_after_s") or 120))
    _breaker["until"] = time.monotonic() + min(900.0, base * (2 ** (_breaker["fails"] - 1)))
    _breaker["last_error"] = type(e).__name__          # the type only: never text that may hold account data


# ── raw fetches ──────────────────────────────────────────────────────────────────

def _rh_stock_raw(sym, timeout=8.0):
    data = _rh("get_equity_quotes", {"symbols": [sym]}, timeout) or {}
    for r in (data.get("results") or []) if isinstance(data, dict) else []:
        q = (r or {}).get("quote") or {}
        if str(q.get("symbol") or "").upper() != sym:
            continue
        bt, at = parse_ts(q.get("venue_bid_time")), parse_ts(q.get("venue_ask_time"))
        return {"bid": _f(q.get("bid_price")), "ask": _f(q.get("ask_price")),
                "q_ts": min(bt, at) if (bt and at) else None,
                "reg": _f(q.get("last_trade_price")), "reg_ts": parse_ts(q.get("venue_last_trade_time")),
                "ext": _f(q.get("last_non_reg_trade_price")),
                "ext_ts": parse_ts(q.get("venue_last_non_reg_trade_time")),
                "state": q.get("state"), "has_traded": q.get("has_traded")}
    return None


def _alp_stock_raw(sym):
    AT = _AT()
    code, d = _alpaca_get(f"{AT._DATA_BASE}/v2/stocks/{sym}/snapshot")
    if code != 200 or not isinstance(d, dict):
        return None
    q, t, b = d.get("latestQuote") or {}, d.get("latestTrade") or {}, d.get("minuteBar") or {}
    # A minute bar is stamped with its START; its close printed somewhere in the following minute. The
    # start is used as the bar's time, so a bar close is never judged younger than it can be.
    return {"bid": _f(q.get("bp")), "ask": _f(q.get("ap")), "q_ts": parse_ts(q.get("t")),
            "last": _f(t.get("p")), "last_ts": parse_ts(t.get("t")),
            "bar": _f(b.get("c")), "bar_ts": parse_ts(b.get("t"))}


def _rh_option_id(occ, timeout=8.0):
    """Robinhood instrument id for an OCC symbol (cached for the life of the process). None when
    Robinhood has no single, standard (x100) active contract that matches exactly."""
    with _lock:
        hit = _occ_ids.get(occ)
    if hit and (hit[1] is None or time.monotonic() < hit[1]):
        return hit[0]
    p = parse_occ(occ)
    if not p:
        return None
    data = _rh("get_option_instruments", {
        "chain_symbol": p["underlying"], "expiration_dates": p["expiry"],
        "strike_price": f"{p['strike']:.4f}", "type": p["type"], "state": "active"}, timeout) or {}
    rows = (data.get("instruments") or data.get("results") or []) if isinstance(data, dict) else []
    match = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        k = _f(r.get("strike_price"))
        mult = _f(r.get("trade_value_multiplier"))
        if (str(r.get("expiration_date"))[:10] == p["expiry"] and r.get("type") == p["type"]
                and k is not None and abs(k - p["strike"]) < 1e-6
                and str(r.get("chain_symbol") or p["underlying"]).upper() == p["underlying"]
                and (mult is None or abs(mult - 100.0) < 1e-6)):
            match.append(r["id"])
    iid = match[0] if len(match) == 1 else None
    with _lock:
        _occ_ids[occ] = (iid, None if iid else time.monotonic() + _NEG_ID_TTL_S)
    return iid


def _rh_option_raw(iid, timeout=8.0):
    data = _rh("get_option_quotes", {"instrument_ids": [iid]}, timeout) or {}
    for r in (data.get("results") or []) if isinstance(data, dict) else []:
        q = (r or {}).get("quote") or r or {}
        if (q.get("instrument_id") or q.get("id")) != iid:
            continue
        return {"bid": _f(q.get("bid_price")), "ask": _f(q.get("ask_price")),
                "q_ts": parse_ts(q.get("updated_at")),
                "bid_size": _f(q.get("bid_size")), "ask_size": _f(q.get("ask_size")),
                "iv": _f(q.get("implied_volatility")), "delta": _f(q.get("delta")),
                "oi": _f(q.get("open_interest")), "volume": _f(q.get("volume")), "instrument_id": iid}
    return None


def _alp_option_raw(occ):
    AT = _AT()
    code, d = _alpaca_get(f"{AT._DATA_BASE}/v1beta1/options/snapshots", {"symbols": occ})
    if code != 200 or not isinstance(d, dict):
        return None
    s = (d.get("snapshots") or {}).get(occ) or {}
    if not s:
        return None
    q, t = s.get("latestQuote") or {}, s.get("latestTrade") or {}
    return {"bid": _f(q.get("bp")), "ask": _f(q.get("ap")), "q_ts": parse_ts(q.get("t")),
            "bid_size": _f(q.get("bs")), "ask_size": _f(q.get("as")),
            "last": _f(t.get("p")), "last_ts": parse_ts(t.get("t")),
            "iv": _f(s.get("impliedVolatility")), "delta": _f((s.get("greeks") or {}).get("delta"))}


# ── evaluation ───────────────────────────────────────────────────────────────────

def _eval_stock(raw, *, source, max_age, any_age, spread_lim):
    """Pick the trigger price from one source's raw data. c['px'] is None when nothing passes."""
    bid, ask = raw.get("bid"), raw.get("ask")
    two, spr, _qa, why_q = check_two_sided(bid, ask, raw.get("q_ts"), spread_lim, max_age, any_age)
    c = {"source": source, "bid": bid if two else None, "ask": ask if two else None,
         "mid": round((bid + ask) / 2.0, 4) if two else None, "two_sided": two,
         "spread_pct": round(spr, 3) if spr is not None else None,
         "last": raw.get("last"), "px": None, "px_src": None, "ts": None, "why": None}
    last, l_age = raw.get("last"), age_s(raw.get("last_ts"))
    bar, b_age = raw.get("bar"), age_s(raw.get("bar_ts"))
    c["last_age_s"] = round(l_age, 1) if l_age is not None else None
    if last and last > 0 and (any_age or (l_age is not None and l_age <= max_age)):
        c.update(px=last, px_src="last", ts=raw.get("last_ts"))
    elif bar and bar > 0 and (any_age or (b_age is not None and b_age <= max_age)):
        c.update(px=bar, px_src="minute_bar", ts=raw.get("bar_ts"))
    elif two:
        if last and bid <= last <= ask:            # an older print that still sits inside a tight, live market
            c.update(px=last, px_src="last", ts=raw.get("q_ts"))
        else:
            c.update(px=c["mid"], px_src="mid", ts=raw.get("q_ts"))
    else:
        lt = f"{l_age:.0f}s old" if l_age is not None else ("missing" if not last else "undated")
        c["why"] = f"last trade {lt}; bid/ask not usable ({why_q})"
    return c


def _eval_option(raw, *, source, max_age, any_age, spread_lim, relaxed):
    bid, ask = raw.get("bid"), raw.get("ask")
    ok, spr, _a, why = check_two_sided(bid, ask, raw.get("q_ts"), spread_lim, max_age, any_age,
                                       bid_size=raw.get("bid_size"))
    usable, wide = ok, False
    if not ok and relaxed:                         # an exit that must happen: any spread, but a real bid
        usable, spr, _a, why = check_two_sided(bid, ask, raw.get("q_ts"), None, max_age, any_age,
                                               bid_size=raw.get("bid_size"))
        wide = usable
    c = {"source": source, "bid": bid if usable else None, "ask": ask if usable else None,
         "mid": round((bid + ask) / 2.0, 4) if usable else None, "two_sided": ok,
         "spread_pct": round(spr, 2) if spr is not None else None,
         "last": raw.get("last"), "px": None, "px_src": None, "ts": None, "why": why,
         "wide": True if wide else None}
    for k in ("iv", "delta", "oi", "volume", "bid_size", "ask_size", "instrument_id"):
        c[k] = raw.get(k)
    if usable:
        c.update(px=c["mid"], px_src="mid", ts=raw.get("q_ts"), why=None)
    return c


def _legacy(sym, is_opt):
    """quotes.enabled=false: the old Alpaca-only quote, unchanged, marked legacy."""
    AT = _AT()
    q = dict(AT._option_quote(sym) if is_opt else AT._stock_quote(sym))
    q.update({"legacy": True, "symbol": sym, "kind": "option" if is_opt else "stock",
              "source": "alpaca_legacy", "two_sided": bool(q.get("bid") and q.get("ask")),
              "px": (q.get("mid") if is_opt else (q.get("last") or q.get("bid"))),
              "px_src": "legacy", "ts": q.get("as_of"), "age_s": None, "relaxed": False,
              "reason": q.get("error")})
    q.setdefault("mid", None)
    q.setdefault("last", None)
    _state["legacy"] += 1
    return q


# ── public: quotes ───────────────────────────────────────────────────────────────

def stock_quote(symbol, *, relaxed=False, any_age=False, cfg=None) -> dict:
    sym = str(symbol or "").upper().strip()
    try:
        qc = qcfg(cfg)
        if not qc.get("enabled", True):
            return _legacy(sym, False)
        out = _stock(sym, qc, bool(relaxed), bool(any_age))
    except Exception as e:
        out = _blank(sym, "stock", f"price lookup failed ({type(e).__name__})", None, relaxed)
    _state[out["source"] if out.get("ok") and out.get("source") in _state else "rejected"] += 1
    return out


def _stock(sym, qc, relaxed, any_age):
    max_age = float(qc["relaxed_max_age_s"] if relaxed else qc["max_age_s"])
    lim = float(qc["stock_max_spread_pct"])
    tried = []
    try:
        alp = _alp_stock_raw(sym)
    except Exception:
        alp = None                                 # an Alpaca hiccup must not cost the Robinhood price
    rh = None
    usable, why_not = _rh_usable(qc)
    if usable:
        try:
            rh = _rh_stock_raw(sym, float(qc.get("rh_timeout_s") or 8))
            _rh_ok()
            if rh is None:
                tried.append({"source": "robinhood", "why": "no quote for this symbol"})
        except Exception as e:
            _rh_fail(e, qc)
            tried.append({"source": "robinhood", "why": f"unavailable ({type(e).__name__})"})
    else:
        tried.append({"source": "robinhood", "why": why_not})
    if rh and (rh.get("state") not in (None, "active") or rh.get("has_traded") is False):
        tried.append({"source": "robinhood", "why": f"not trading (state {rh.get('state')})"})
        rh = None
    if rh:
        raw = dict(rh, last=rh.get("reg"), last_ts=rh.get("reg_ts"))
        if any_age and rh.get("ext") and rh.get("ext_ts") and (not rh.get("reg_ts") or rh["ext_ts"] > rh["reg_ts"]):
            raw.update(last=rh["ext"], last_ts=rh["ext_ts"])      # closed-market preview: newest print
        c = _eval_stock(raw, source="robinhood", max_age=max_age, any_age=any_age, spread_lim=lim)
        if c["px"]:
            c["xcheck"] = "unavailable"
            div = ref = None
            if alp:
                c["xcheck"] = "skipped"
                for k, tk in (("last", "last_ts"), ("bar", "bar_ts")):
                    v, a = alp.get(k), age_s(alp.get(tk))
                    if v and v > 0 and a is not None and a <= float(qc["xcheck_max_age_s"]):
                        ref = v
                        break
                if ref:
                    div = abs(c["px"] - ref) / ref * 100.0
                    c["xcheck"] = "ok" if div <= float(qc["stock_max_divergence_pct"]) else "diverged"
                    c["xcheck_ref"], c["xcheck_div_pct"] = ref, round(div, 3)
            if c["xcheck"] != "diverged" or relaxed or any_age:
                return _result(sym, "stock", c, relaxed, tried)
            tried.append({"source": "robinhood",
                          "why": f"{c['px']} disagrees with Alpaca's last trade {ref} by {div:.1f}%"})
            return _blank(sym, "stock", "Robinhood and Alpaca prices disagree — neither is trusted right now",
                          tried, relaxed)
        tried.append({"source": "robinhood", "why": c["why"]})
    if alp:
        c = _eval_stock(alp, source="alpaca", max_age=max_age, any_age=any_age, spread_lim=lim)
        if c["px"]:
            return _result(sym, "stock", c, relaxed, tried)
        tried.append({"source": "alpaca", "why": c["why"]})
    else:
        tried.append({"source": "alpaca", "why": "no snapshot"})
    return _blank(sym, "stock", "no reliable price: " + "; ".join(f"{t['source']} {t['why']}" for t in tried),
                  tried, relaxed)


def option_quote(occ, *, relaxed=False, any_age=False, max_spread_pct=None, cfg=None) -> dict:
    sym = str(occ or "").upper().strip()
    try:
        qc = qcfg(cfg)
        if not qc.get("enabled", True):
            return _legacy(sym, True)
        lim = float(qc["opt_max_spread_pct"] if max_spread_pct is None else max_spread_pct)
        out = _option(sym, qc, bool(relaxed), bool(any_age), lim)
    except Exception as e:
        out = _blank(sym, "option", f"price lookup failed ({type(e).__name__})", None, relaxed)
    _state[out["source"] if out.get("ok") and out.get("source") in _state else "rejected"] += 1
    return out


def _option(sym, qc, relaxed, any_age, lim):
    max_age = float(qc["relaxed_max_age_s"] if relaxed else qc["max_age_s"])
    tried = []
    try:
        alp = _alp_option_raw(sym)
    except Exception:
        alp = None
    rh = None
    usable, why_not = _rh_usable(qc)
    if usable:
        try:
            wait = float(qc.get("rh_timeout_s") or 8)
            iid = _rh_option_id(sym, wait)
            if iid:
                rh = _rh_option_raw(iid, wait)
            _rh_ok()
            if not iid:
                tried.append({"source": "robinhood", "why": "no matching contract"})
            elif rh is None:
                tried.append({"source": "robinhood", "why": "no quote for this contract"})
        except Exception as e:
            _rh_fail(e, qc)
            tried.append({"source": "robinhood", "why": f"unavailable ({type(e).__name__})"})
    else:
        tried.append({"source": "robinhood", "why": why_not})
    if rh:
        c = _eval_option(rh, source="robinhood", max_age=max_age, any_age=any_age, spread_lim=lim,
                         relaxed=relaxed)
        if c["px"]:
            c["xcheck"] = "unavailable"
            diff = amid = None
            if alp:
                c["xcheck"] = "skipped"
                a_ok, _s, _a, _w = check_two_sided(alp.get("bid"), alp.get("ask"), alp.get("q_ts"), None,
                                                   float(qc["xcheck_max_age_s"]))
                if a_ok:
                    amid = (alp["bid"] + alp["ask"]) / 2.0
                    diff = abs(c["mid"] - amid)
                    tol = max(_OPT_ABS_DIVERGENCE, float(qc["opt_max_divergence_pct"]) / 100.0 * c["mid"])
                    c["xcheck"] = "ok" if diff <= tol + 1e-9 else "diverged"
                    c["xcheck_ref"] = round(amid, 4)
                    c["xcheck_div_pct"] = round(diff / c["mid"] * 100.0, 2) if c["mid"] else None
                if alp.get("last"):
                    c["last"] = alp["last"]
                    la = age_s(alp.get("last_ts"))
                    c["last_age_s"] = round(la, 1) if la is not None else None
            if c["xcheck"] != "diverged" or relaxed or any_age:
                return _result(sym, "option", c, relaxed, tried)
            tried.append({"source": "robinhood",
                          "why": f"mid {c['mid']} disagrees with Alpaca's mid {amid:.2f}"})
            return _blank(sym, "option", "Robinhood and Alpaca option prices disagree — neither is trusted right now",
                          tried, relaxed)
        tried.append({"source": "robinhood", "why": c["why"]})
    if alp:
        c = _eval_option(alp, source="alpaca", max_age=max_age, any_age=any_age, spread_lim=lim,
                         relaxed=relaxed)
        if c["px"]:
            la = age_s(alp.get("last_ts"))
            c["last_age_s"] = round(la, 1) if la is not None else None
            return _result(sym, "option", c, relaxed, tried)
        tried.append({"source": "alpaca", "why": c["why"]})
    else:
        tried.append({"source": "alpaca", "why": "no snapshot"})
    return _blank(sym, "option", "no reliable two-sided quote: "
                  + "; ".join(f"{t['source']} {t['why']}" for t in tried), tried, relaxed)


# ── public: limit prices ─────────────────────────────────────────────────────────

def sell_limit(q, *, attempt=1, urgent=False, cfg=None):
    """(limit price | None, how it was derived) for a sell-to-close.
    stock : last − band (band grows with each re-priced attempt, capped), or the validated bid when
            that bid is inside the band — never a bid that sits further from the last trade.
    option: mid − band but never below the validated bid (attempt 2+ walks toward the bid);
            `urgent` (flat-by) or a wide quote accepted for a must-do exit goes straight to the bid."""
    if q and q.get("legacy"):
        v = q.get("bid") or (None if q.get("kind") == "option" else q.get("last"))
        return (round(float(v), 2), "bid (old rule)") if v else (None, "no bid")
    if not q or not q.get("ok"):
        return None, (q or {}).get("reason") or "no usable price"
    qc = qcfg(cfg)
    n = max(1, int(attempt or 1))
    if q.get("kind") == "option":
        bid, mid = q.get("bid"), q.get("mid")
        if not (bid and mid):
            return None, "no two-sided quote"
        if urgent or not q.get("two_sided"):
            return round(bid, 2), "bid"
        band = float(qc["opt_limit_band_pct"]) * n
        lim = max(bid, mid * (1.0 - band / 100.0))
        return round(lim, 2), ("bid" if lim <= bid + 1e-9 else f"mid −{band:g}%")
    px = q.get("px")
    if not px:
        return None, "no price"
    band = min(float(qc["stock_limit_band_pct"]) * n, float(qc["stock_limit_band_max_pct"]))
    lim = px * (1.0 - band / 100.0)
    if q.get("two_sided") and q.get("bid") and q["bid"] >= lim:
        return _px_round(q["bid"]), "bid (inside the band around the last price)"
    return _px_round(lim), f"{q.get('px_src')} −{band:g}%"


def buy_limit(q, *, cfg=None):
    """(the most to pay right now | None, how it was derived) for a buy-to-open.
    stock : last + band, or a validated ask +0.1% when that is lower.
    option: mid + band, never above the validated ask."""
    if q and q.get("legacy"):
        if q.get("kind") == "option":
            return (q.get("ask"), "live ask") if q.get("ask") else (None, "no ask")
        a = q.get("ask") or q.get("last")
        return (round(a * (1 + _ASK_BUFFER), 2), "live ask +0.1%") if a else (None, "no price")
    if not q or not q.get("ok"):
        return None, (q or {}).get("reason") or "no usable price"
    qc = qcfg(cfg)
    if q.get("kind") == "option":
        ask, mid = q.get("ask"), q.get("mid")
        if not (ask and mid and q.get("two_sided")):
            return None, "no two-sided quote"
        band = float(qc["opt_limit_band_pct"])
        cap = mid * (1.0 + band / 100.0)
        return (round(ask, 2), "live ask") if ask <= cap + 1e-9 else (round(cap, 2), f"mid +{band:g}%")
    px = q.get("px")
    if not px:
        return None, "no price"
    band = float(qc["stock_limit_band_pct"])
    cap = px * (1.0 + band / 100.0)
    if q.get("two_sided") and q.get("ask"):
        a = q["ask"] * (1 + _ASK_BUFFER)
        if a <= cap:
            return round(a, 2), "live ask +0.1%"
    return round(cap, 2), f"{q.get('px_src')} +{band:g}%"


def compact(q, **extra) -> dict:
    """The few fields worth keeping on a trade so trading cost can be graded later."""
    q = q or {}
    out = {k: q.get(k) for k in ("bid", "ask", "last", "mid", "px", "px_src", "source", "age_s", "ts",
                                 "spread_pct", "xcheck", "two_sided")}
    for k in ("iv", "delta", "oi", "volume", "relaxed", "legacy", "reason"):
        if q.get(k):
            out[k] = q[k]
    out["at"] = datetime.now().isoformat(timespec="seconds")
    out.update(extra)
    return out


def status() -> dict:
    return {"counts": dict(_state), "robinhood_paused": time.monotonic() < _breaker["until"],
            "robinhood_last_error": _breaker["last_error"], "clock_skew_s": round(_skew["s"], 1),
            "cached_option_ids": len(_occ_ids)}
