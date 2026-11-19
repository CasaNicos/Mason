"""
mason_ticker_card.py — the data behind the header search popup: GET /api/ticker_card?symbol=NVDA (read-only).

One call bundles a stock's or fund's overview, live quote, key metrics, recent financials, earnings and dividend
dates, and analyst ratings. The price chart is not here: the popup gets bars from /api/bars and the type-ahead from
/api/symbol_search.

Contract (fixed: the front end is built against it)
---------------------------------------------------
200 {"ok": true, "symbol", "as_of",
     "profile":    {name, exchange, sector, industry, description, ceo, employees, hq, founded, website},
     "quote":      {price, change, change_pct, prev_close, open, day_high, day_low, volume, ts, source},
     "metrics":    {market_cap, pe_ratio, forward_pe, eps_ttm, dividend_yield, beta, high_52w, low_52w, avg_volume,
                    shares_outstanding, float, short_pct_float},
     "financials": {"period_type": "quarterly"|"annual"|null,
                    "periods": [{period, end_date, revenue, gross_profit, operating_income, net_income, eps,
                                 free_cash_flow}]},                              newest first, at most 5
     "dates":      {"next_earnings": {date, timing "bmo"|"amc"|null, confirmed}|null,
                    "last_earnings": {date, eps_actual, eps_estimate, surprise_pct}|null,
                    "ex_dividend", "dividend_pay"},
     "analyst":    {buy, hold, sell, target_mean, target_high, target_low} | null,
     "sources":    {section: "robinhood" | "yfinance" | "robinhood+yfinance" | ... | null},
     "missing":    [dotted paths of every null: "metrics.beta", "financials", "financials.periods.0.eps", ...]}
400 / 404 / 500 {"ok": false, "error": "<plain sentence>", "code": "bad_symbol" | "not_found" | "server_error"}

Every leaf is a value a source gave, or plain arithmetic on such values (day change = price - previous close; EPS
surprise = (actual - estimate) / |estimate|), or null. Nothing is defaulted, estimated or filled with 0. Percent
fields (change_pct, dividend_yield, short_pct_float, surprise_pct) are in percent units: 1.25 means 1.25%. Money is
in US dollars; a company that reports in another currency (an ADR such as TSM) gets financials = null.

Where each section comes from (first source that answers wins; Robinhood is the read-only MCP connection)
-------------------------------------------------------------------------------------------------------
  quote       Robinhood get_equity_quotes (last regular-session trade, or the official close once the session has
              closed; previous close = the prior session's, by its date) + get_equity_fundamentals (open, high, low,
              volume of that same session). Late evening and pre-market Robinhood's "previous close" already points at
              the session that just ended, so the day change cannot be told from it: then yfinance .info
              regularMarket* (the source /api/quote shows first). If yfinance also fails, Robinhood's price alone.
  profile     per field: name, exchange, website = yfinance; sector + industry = yfinance's pair, else Robinhood's;
              description, CEO, employees, HQ, founded = Robinhood, else yfinance (no founding year there). Name
              falls back to Mason's SEC company list.
  metrics     market cap + shares + float = Robinhood's set, else yfinance's; P/E + EPS (TTM) + forward P/E =
              yfinance's set (so P/E = price / EPS holds), else Robinhood's P/E alone; dividend yield, 52-week
              range, average volume (30-day) = Robinhood, else yfinance; beta, short % of float = yfinance.
  financials  yfinance quarterly income statement + cash flow (annual when Yahoo has no quarters). Labels are the
              company's own fiscal quarters ("FY2027 Q3"), read off Robinhood's earnings list (each quarter's report
              is the first one after its end date); without that list, calendar quarters ("2026-Q3").
              Robinhood's get_financials is not used: it is not on mason_robinhood's read allowlist, and it has no
              operating income, EPS or free cash flow.
  dates       earnings = Robinhood get_earnings_results (else yfinance .info timestamps + earnings_history);
              ex-dividend + pay date = Robinhood fundamentals (else yfinance .info). Robinhood's ex-date can be the
              NEXT scheduled one while its pay date is the last one paid (SPY), exactly as Robinhood reports them.
  analyst     Robinhood get_equity_analyst_ratings, else yfinance recommendations summary (strong buy + buy, hold,
              sell + strong sell) + .info price targets.

Speed and safety
----------------
  * Every source is fetched in its own daemon thread (single-flight per symbol and source) and cached per symbol in
    memory (LRU, at most CACHE_MAX_SYMBOLS): quote and fundamentals 20 s, earnings and analysts 1 h, yfinance .info
    15 min, yfinance statements 6 h. After a failed refresh the last good answer is used up to its max age.
  * A request waits at most WAIT_S (default 6 s); whatever has not answered by then is null and listed in "missing",
    and fills the cache for the next request. One slow or failing source never fails the card.
  * Robinhood: the "live" lane (the lane mason_robinhood reserves for UI-driven calls), plus this module's own fences
    so a burst of lookups can never crowd out the scanner poller or the trade-trigger quotes: at most
    RH_MAX_CALLS calls per RH_WINDOW_S, at most RH_MAX_INFLIGHT at once, and a pause (60 s, doubling to 10 min)
    after a connection failure. Over a fence = that section comes from yfinance. robinhood.enabled = false = yfinance
    only. mason_quotes is NOT used: it returns only a trigger price (no previous close / open / range / volume),
    rejects any quote outside market hours, and its Robinhood pause is shared with the autotrade loop, so a failed UI
    lookup there would pause Robinhood quotes for trading.
  * yfinance runs in a short-lived subprocess (Mason's rule: no in-process yfinance on Flask request threads; it
    also keeps this lookup off the main process's shared Yahoo cookie), hard-killed after YF_TIMEOUT_S, at most
    YF_MAX_PROCS at once.
  * Read-only: no database, journal or state-file writes; only read tools are called (get_equity_quotes,
    get_equity_fundamentals, get_earnings_results, get_equity_analyst_ratings — all on mason_robinhood's
    allowlist, which also refuses anything else). mason_symbols.mark_not_found (a DB write) is never called.

Never imports mason_app. Tests replace the hooks _rh_call, _rh_enabled, _yf_fetch, _known, _renamed, _sec_title,
_now and call reset().
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, wait as _futures_wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("mason.ticker_card")
BASE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

# ── knobs ───────────────────────────────────────────────────────────────────────
WAIT_S = 6.0                  # longest one request waits for its sources
RH_WAIT_S = 4.0               # ... of which, for Robinhood (it answers in ~0.2 s when healthy)
YF_TIMEOUT_S = 8.0            # the yfinance subprocess is killed after this (it may finish after the reply)
YF_MAX_PROCS = 2              # yfinance subprocesses at once (module-wide)
CACHE_MAX_SYMBOLS = 200
RH_WINDOW_S = 10.0            # Robinhood fence: at most RH_MAX_CALLS calls per RH_WINDOW_S from this module (half
RH_MAX_CALLS = 20             # of the default 4/s live lane; a new card costs 4 calls, a quote refresh 2) ...
RH_MAX_INFLIGHT = 6           # ... and at most this many waiting at once
RH_PAUSE_S = 60.0             # pause after a connection failure, doubling up to RH_PAUSE_MAX_S
RH_PAUSE_MAX_S = 600.0
QUOTE_TTL_S = 20.0
YF_QUOTE_CLOSED_MAX_S = 300.0  # a yfinance quote taken while the regular session was closed stays valid this long

# source unit -> (refresh after s, use at most this old s, after a failure wait this long before trying again s)
UNITS = {
    "rh_quote":   (QUOTE_TTL_S, QUOTE_TTL_S, 10.0),
    "rh_fund":    (QUOTE_TTL_S, 6 * 3600.0, 10.0),     # refreshed with the quote: it carries the session's open/high/low/volume
    "rh_earn":    (3600.0, 24 * 3600.0, 60.0),
    "rh_analyst": (3600.0, 24 * 3600.0, 60.0),
    "yf_info":    (900.0, 6 * 3600.0, 60.0),
    "yf_fin":     (6 * 3600.0, 24 * 3600.0, 120.0),
}
_RH_UNITS = ("rh_quote", "rh_fund", "rh_earn", "rh_analyst")
_FUND_FRESH_S = QUOTE_TTL_S + 15.0   # fundamentals this fresh may supply the quote's open/high/low/volume

PROFILE_KEYS = ("name", "exchange", "sector", "industry", "description", "ceo", "employees", "hq", "founded", "website")
QUOTE_KEYS = ("price", "change", "change_pct", "prev_close", "open", "day_high", "day_low", "volume", "ts", "source")
METRIC_KEYS = ("market_cap", "pe_ratio", "forward_pe", "eps_ttm", "dividend_yield", "beta", "high_52w", "low_52w",
               "avg_volume", "shares_outstanding", "float", "short_pct_float")
PERIOD_KEYS = ("period", "end_date", "revenue", "gross_profit", "operating_income", "net_income", "eps",
               "free_cash_flow")
ANALYST_KEYS = ("buy", "hold", "sell", "target_mean", "target_high", "target_low")
SECTIONS = ("profile", "quote", "metrics", "financials", "dates", "analyst")
_SOURCE_ORDER = ("robinhood", "yfinance", "sec")

# US tickers: 1-5 letters, optionally a share class (BRK.B, or Yahoo's BRK-B) or Yahoo's preferred form (BAC-PL)
_SYM_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]|-[A-Z]{1,2})?$")
_TIMING = {"am": "bmo", "pm": "amc"}
_FREQ = {"monthly": 12, "quarterly": 4, "semi-annually": 2, "semiannually": 2, "semi-annual": 2, "annually": 1,
         "annual": 1}

MSG_BAD = "A symbol is 1 to 5 letters, with an optional share class like BRK.B. Try something like NVDA."
MSG_NOT_FOUND = "Mason could not find a US stock or fund with the symbol {sym}."
MSG_SERVER = "Mason could not load the card for {sym} just now. Try again in a minute."


# ── hooks (tests replace these) ─────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _rh_enabled() -> bool:
    try:
        import mason_robinhood
        return bool(mason_robinhood.enabled())
    except Exception:
        return False


def _rh_call(tool: str, args: dict):
    import mason_robinhood
    return mason_robinhood._call(tool, args, lane="live")


def _known(sym: str) -> bool:
    import mason_symbols
    return bool(mason_symbols.known(sym))


def _renamed(sym: str) -> str:
    import mason_symbols
    return mason_symbols.renamed(sym) or sym


def _sec_title(sym: str):
    """The company name on Mason's SEC company list (the name fallback). None when absent."""
    import mason_symbols
    titles = mason_symbols._titles() or {}
    return titles.get(sym) or titles.get(sym.replace(".", "-"))


def _yf_symbol(sym: str) -> str:
    try:
        import mason_symbols
        return mason_symbols.yf_symbol(sym)
    except Exception:
        m = re.match(r"^([A-Z]{1,5})\.([A-Z])$", sym)
        return f"{m.group(1)}-{m.group(2)}" if m else sym


_YF_SENTINEL = "@@MASON_TICKER_CARD@@"
_YF_CHILD = r'''
import json, logging, math, sys, time
SENT = "@@MASON_TICKER_CARD@@"
logs = []
class _Keep(logging.Handler):
    def emit(self, rec):
        try:
            logs.append(str(rec.getMessage())[:300])
        except Exception:
            pass
_lg = logging.getLogger("yfinance")
_lg.addHandler(_Keep())
_lg.setLevel(logging.WARNING)
_lg.propagate = False
out = {"info": None, "fin": None, "errors": {}, "log": logs}
def emit():
    sys.stdout.write("\n" + SENT + json.dumps(out, allow_nan=False, default=str) + "\n")
    sys.stdout.flush()
def num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None
def err(part, e):
    out["errors"][part] = (type(e).__name__ + ": " + str(e))[:300]
try:
    import yfinance as yf
except Exception as e:
    err("import", e)
    emit()
    sys.exit(0)
ysym, parts = sys.argv[1], set(sys.argv[2].split(","))     # Yahoo's spelling: the parent passes _yf_symbol(sym)
KEYS = ["quoteType", "currency", "financialCurrency", "longName", "shortName", "fullExchangeName", "exchange",
        "sector", "industry", "longBusinessSummary", "fullTimeEmployees", "city", "state", "country", "website",
        "marketCap", "trailingPE", "forwardPE", "trailingEps", "epsTrailingTwelveMonths", "dividendYield",
        "dividendRate", "trailingAnnualDividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
        "averageVolume", "sharesOutstanding", "floatShares", "shortPercentOfFloat", "sharesShort",
        "exDividendDate", "dividendDate", "earningsTimestamp", "earningsTimestampStart", "isEarningsDateEstimate",
        "regularMarketPrice", "regularMarketPreviousClose", "regularMarketOpen", "regularMarketDayHigh",
        "regularMarketDayLow", "regularMarketVolume", "regularMarketTime", "marketState",
        "targetMeanPrice", "targetHighPrice", "targetLowPrice", "numberOfAnalystOpinions"]
if "info" in parts:
    try:
        info = {}
        for attempt in (0, 1):
            n0 = len(logs)
            info = yf.Ticker(ysym).info or {}
            useful = any(info.get(k) for k in ("quoteType", "regularMarketPrice", "longName", "shortName"))
            if useful or attempt or not any(("401" in m or "Invalid Crumb" in m) for m in logs[n0:]):
                break
            time.sleep(0.6)
        d = {}
        for k in KEYS:
            v = info.get(k)
            if isinstance(v, bool) or v is None:
                d[k] = v
            elif isinstance(v, (int, float)):
                d[k] = num(v)
            elif isinstance(v, str):
                d[k] = v[:4000]
            else:
                d[k] = None
        offs = []
        for o in (info.get("companyOfficers") or [])[:25]:
            if isinstance(o, dict):
                offs.append({"name": str(o.get("name") or "")[:120], "title": str(o.get("title") or "")[:160]})
        d["officers"] = offs
        out["info"] = d
    except Exception as e:
        err("info", e)
if "fin" in parts:
    fin = {}
    tk = yf.Ticker(ysym)
    INC = [("revenue", ["Total Revenue", "Operating Revenue"]), ("gross_profit", ["Gross Profit"]),
           ("operating_income", ["Operating Income", "Total Operating Income As Reported"]),
           ("net_income", ["Net Income", "Net Income Common Stockholders"]), ("eps", ["Diluted EPS", "Basic EPS"])]
    CF = [("free_cash_flow", ["Free Cash Flow"])]
    def table(attr, rows):
        try:
            df = getattr(tk, attr)
        except Exception as e:
            err(attr, e)
            return None
        if df is None or getattr(df, "empty", True):
            return []
        res = []
        for col in df.columns:
            rec = {"end_date": str(col)[:10]}
            for key, names in rows:
                val = None
                for n in names:
                    if n in df.index:
                        cell = df.loc[n, col]
                        if hasattr(cell, "iloc"):
                            cell = cell.iloc[0]
                        val = num(cell)
                        if val is not None:
                            break
                rec[key] = val
            res.append(rec)
        return res
    fin["q_inc"] = table("quarterly_income_stmt", INC)
    fin["q_cf"] = table("quarterly_cashflow", CF)
    if not any(any(r.get(k) is not None for k, _ in INC) for r in (fin["q_inc"] or [])):
        fin["a_inc"] = table("income_stmt", INC)
        fin["a_cf"] = table("cashflow", CF)
    try:
        eh = tk.earnings_history
        rows = []
        if eh is not None and not getattr(eh, "empty", True):
            for idx, r in eh.iterrows():
                rows.append({"quarter_end": str(idx)[:10], "eps_actual": num(r.get("epsActual")),
                             "eps_estimate": num(r.get("epsEstimate"))})
        fin["earnings_history"] = rows
    except Exception as e:
        err("earnings_history", e)
    try:
        rs = tk.recommendations_summary
        row = None
        if rs is not None and not getattr(rs, "empty", True):
            for _, r in rs.iterrows():
                if str(r.get("period")) == "0m":
                    row = {k: num(r.get(k)) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
                    break
        fin["recs"] = row
    except Exception as e:
        err("recommendations_summary", e)
    out["fin"] = fin
emit()
'''


def _yf_fetch(ysym: str, parts, timeout: float) -> dict:
    """Run the yfinance child for `ysym` and return its JSON ({"info", "fin", "errors", "log"}). Raises on a
    timeout (the child is killed) or when no result line came back."""
    cmd = [sys.executable, "-P", "-c", _YF_CHILD, ysym, ",".join(sorted(parts))]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE), encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    for line in reversed((r.stdout or "").splitlines()):
        if line.startswith(_YF_SENTINEL):
            return json.loads(line[len(_YF_SENTINEL):])
    raise RuntimeError(f"yfinance child gave no result (rc={r.returncode}): {(r.stderr or '').strip()[-200:]}")


# ── small pure helpers ──────────────────────────────────────────────────────────

def _num(v):
    """A finite float from a number or numeric string, else None (bools are not numbers)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v):
    x = _num(v)
    return x if x is not None and x > 0 else None


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _int(v, positive=True):
    x = _num(v)
    if x is None or (positive and x <= 0) or (not positive and x < 0) or abs(x) >= 1e18:
        return None
    return int(round(x))


def _txt(v, limit=4000):
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s[:limit] if s else None


def _day(v):
    """'YYYY-MM-DD' (validated) from a date-like string, else None."""
    try:
        return date.fromisoformat(str(v)[:10]).isoformat() if v else None
    except (TypeError, ValueError):
        return None


def _utc_day(epoch):
    """Yahoo stores calendar dates (ex-dividend, pay date) as midnight UTC: read them in UTC, never in ET."""
    x = _num(epoch)
    if x is None or x <= 0:
        return None
    try:
        return datetime.fromtimestamp(x, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso(epoch):
    x = _num(epoch)
    if x is None or x <= 0:
        return None
    try:
        return datetime.fromtimestamp(x, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _parse_ts(s):
    """ISO-8601 (Z, offsets, nanoseconds) -> epoch seconds, else None."""
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
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _et_date(epoch) -> date:
    return datetime.fromtimestamp(epoch, ET).date()


def _as_percent(v, expected_pct, default_percent: bool):
    """A yield-like value that a source may give as a fraction (0.0064) or in percent (0.64), in percent units.
    `expected_pct` (percent, from dividend / price or similar) decides which reading is right; without it the
    source's documented unit is assumed (default_percent) or, when that is not trusted, None. 0 / missing -> None."""
    x = _pos(v)
    if x is None:
        return None
    if expected_pct is not None and expected_pct > 0:
        as_pct, as_frac = x, x * 100.0
        return as_pct if abs(math.log(as_pct / expected_pct)) <= abs(math.log(as_frac / expected_pct)) else as_frac
    if default_percent is None:
        return None
    return x if default_percent else x * 100.0


def _join_sources(srcs) -> str | None:
    s = [x for x in _SOURCE_ORDER if x in set(srcs or ())]
    return "+".join(s) if s else None


def normalize_symbol(raw):
    """(canonical symbol, None) or (None, error message). Upper-cases and trims; a one-letter share class is written
    with a dot (BRK-B -> BRK.B, the spelling Robinhood and Alpaca use; yfinance gets BRK-B via mason_symbols)."""
    s = str(raw or "").strip().upper()
    if not s or not _SYM_RE.fullmatch(s):
        return None, MSG_BAD
    m = re.fullmatch(r"([A-Z]{1,5})-([A-Z])", s)
    if m:
        s = f"{m.group(1)}.{m.group(2)}"
    return s, None


# ── cache (per symbol, per source unit) ─────────────────────────────────────────

_lock = threading.Lock()
_cache: "OrderedDict[str, dict]" = OrderedDict()   # sym -> {unit: {"good": result | None, "err_at", "err"}}
_inflight: dict = {}                                # (sym, unit) -> Future
_yf_slots = threading.BoundedSemaphore(YF_MAX_PROCS)
_rh_lock = threading.Lock()
_rh = {"calls": deque(), "inflight": 0, "paused_until": 0.0, "fails": 0, "last_error": None}


def reset() -> None:
    """Forget every cached answer and the Robinhood pause (tests; harmless in production)."""
    global _yf_slots
    with _lock:
        _cache.clear()
        _inflight.clear()
    with _rh_lock:
        _rh.update(calls=deque(), inflight=0, paused_until=0.0, fails=0, last_error=None)
    _yf_slots = threading.BoundedSemaphore(YF_MAX_PROCS)


def _entry(sym: str) -> dict:
    """The symbol's cache entry (created if needed, marked most recently used). Call with _lock held."""
    e = _cache.get(sym)
    if e is None:
        e = _cache[sym] = {}
        while len(_cache) > CACHE_MAX_SYMBOLS:
            _cache.popitem(last=False)
    else:
        _cache.move_to_end(sym)
    return e


def _store(sym: str, unit: str, status: str, data, why=None, at=None) -> None:
    """Record a fetch outcome. ok / empty replace the last good answer; an error keeps it and stamps err_at."""
    at = _now() if at is None else at
    with _lock:
        u = _entry(sym).setdefault(unit, {"good": None, "err_at": None, "err": None})
        if status in ("ok", "empty"):
            u["good"] = {"status": status, "data": data, "at": at}
            u["err_at"], u["err"] = None, None
        elif status == "error":
            u["err_at"], u["err"] = at, why


def _snapshot(sym: str) -> dict:
    with _lock:
        e = _entry(sym)
        return {k: dict(v) for k, v in e.items()}


def _usable(snap: dict, unit: str, now: float, max_age: float = None):
    """The unit's last good answer if it is not older than max_age (default: the unit's own), else None."""
    g = (snap.get(unit) or {}).get("good")
    if not g:
        return None
    lim = UNITS[unit][1] if max_age is None else max_age
    return g if now - g["at"] <= lim else None


def _due(snap: dict, unit: str, now: float, fresher_than: float = None) -> bool:
    """True when the unit should be fetched now: no good answer, or older than its refresh time (or than
    `fresher_than`), and not inside the wait after a failure."""
    ttl, _max_age, retry_after = UNITS[unit]
    u = snap.get(unit) or {}
    if u.get("err_at") is not None and now - u["err_at"] < retry_after:
        return False
    g = u.get("good")
    lim = ttl if fresher_than is None else min(ttl, fresher_than)
    return g is None or now - g["at"] >= lim


def _launch(sym: str, units: tuple, make_job, now: float, fresher_than: float = None) -> list:
    """Single-flight fetch of `units` for `sym`. Under the lock, each unit is either already being fetched (its
    Future is returned to wait on), or still due in the cache as it is NOW (an answer stored after the caller looked
    does not get fetched twice), or skipped. make_job(due_units) builds one job for the due ones; it runs in a daemon
    thread, stores its own results and resolves its Future when done (never raises). Returns the Futures to wait on."""
    with _lock:
        e = _cache.get(sym) or {}
        waits, need = [], []
        for u in units:
            f = _inflight.get((sym, u))
            if f is not None:
                if f not in waits:
                    waits.append(f)
            elif _due(e, u, now, fresher_than):
                need.append(u)
        if not need:
            return waits
        f = Future()
        for u in need:
            _inflight[(sym, u)] = f
    need = tuple(need)
    job = make_job(need)

    def run():
        try:
            job()
        except BaseException as ex:                     # noqa: BLE001 — a job must never kill its thread silently
            for u in need:
                _store(sym, u, "error", None, f"{type(ex).__name__}")
            log.warning(f"[ticker_card] {sym} {'/'.join(need)} fetch failed: {type(ex).__name__}: {ex}")
        finally:
            with _lock:
                for u in need:
                    if _inflight.get((sym, u)) is f:
                        _inflight.pop((sym, u), None)
            f.set_result(True)

    threading.Thread(target=run, name=f"ticker-card-{sym}-{need[0]}", daemon=True).start()
    return waits + [f]


# ── Robinhood (read tools only, live lane, own fences) ──────────────────────────

def _rh_gate() -> str | None:
    """None when a Robinhood call may start now (the slot is then reserved), else why not: 'off' | 'paused' | 'busy'."""
    if not _rh_enabled():
        return "off"
    mono = time.monotonic()
    with _rh_lock:
        if mono < _rh["paused_until"]:
            return "paused"
        q = _rh["calls"]
        while q and mono - q[0] > RH_WINDOW_S:
            q.popleft()
        if len(q) >= RH_MAX_CALLS or _rh["inflight"] >= RH_MAX_INFLIGHT:
            return "busy"
        q.append(mono)
        _rh["inflight"] += 1
    return None


def _rh_error_kind(e) -> str:
    """'missing' (Robinhood does not know the symbol: a 4xx answer such as 404 missing_instruments) or 'down'
    (timeout, no login, connection or server trouble, rate limit) — only 'down' pauses Robinhood for this module."""
    name = type(e).__name__
    if name in ("RHNeedsLogin", "RHForbidden"):
        return "down"
    m = re.search(r"\b(?:API error|status|HTTP)\D{0,3}(\d{3})\b", str(e))
    if m:
        code = int(m.group(1))
        if 400 <= code < 500 and code != 429:
            return "missing"
    return "down"


def _rh_done(failure=None) -> None:
    with _rh_lock:
        _rh["inflight"] = max(0, _rh["inflight"] - 1)
        if failure is None:
            _rh["fails"] = 0
            return
        _rh["fails"] += 1
        pause = min(RH_PAUSE_MAX_S, RH_PAUSE_S * (2 ** (_rh["fails"] - 1)))
        _rh["paused_until"] = time.monotonic() + pause
        _rh["last_error"] = type(failure).__name__
    log.warning(f"[ticker_card] Robinhood paused for {pause:.0f}s after {type(failure).__name__}; "
                f"cards use yfinance meanwhile")


def _rh_read(tool: str, args: dict) -> tuple:
    """(status, data, why): ok | empty (Robinhood says it has no such symbol) | error | off."""
    why = _rh_gate()
    if why:
        return ("off" if why == "off" else "error"), None, f"robinhood {why}"
    try:
        data = _rh_call(tool, args)
    except Exception as e:
        if _rh_error_kind(e) == "missing":
            _rh_done()
            return "empty", None, f"robinhood: {type(e).__name__}"
        _rh_done(e)
        return "error", None, f"robinhood: {type(e).__name__}"
    _rh_done()
    return "ok", data, None


def _rows(data, key="results") -> list:
    rows = data.get(key) if isinstance(data, dict) else (data if isinstance(data, list) else None)
    return [r for r in rows or [] if isinstance(r, dict)]


def _fetch_rh_quote(sym):
    st, data, why = _rh_read("get_equity_quotes", {"symbols": [sym]})
    if st != "ok":
        return st, None, why
    for r in _rows(data):
        if str(((r.get("quote") or {}).get("symbol")) or "").upper() == sym:
            return "ok", r, None
    return "empty", None, "robinhood: no quote row"


def _fetch_rh_fund(sym):
    st, data, why = _rh_read("get_equity_fundamentals", {"symbols": [sym]})
    if st != "ok":
        return st, None, why
    for r in _rows(data):
        if str(r.get("symbol") or "").upper() == sym:
            return "ok", r, None
    return "empty", None, "robinhood: no fundamentals row"


def _fetch_rh_earn(sym):
    st, data, why = _rh_read("get_earnings_results", {"symbol": sym})
    if st != "ok":
        return st, None, why
    nf = data.get("not_found") if isinstance(data, dict) else None
    rows = [r for r in _rows(data) if str(r.get("symbol") or "").upper() == sym]
    if not rows and isinstance(nf, list) and sym in {str(x).upper() for x in nf}:
        return "empty", None, "robinhood: earnings not_found"
    return "ok", rows, None


def _fetch_rh_analyst(sym):
    st, data, why = _rh_read("get_equity_analyst_ratings", {"symbols": [sym]})
    if st != "ok":
        return st, None, why
    for r in _rows(data):
        if str(r.get("symbol") or "").upper() == sym:
            rt = r.get("ratings")
            return "ok", (rt if isinstance(rt, dict) else None), None
    return "ok", None, None


_RH_FETCH = {"rh_quote": _fetch_rh_quote, "rh_fund": _fetch_rh_fund, "rh_earn": _fetch_rh_earn,
             "rh_analyst": _fetch_rh_analyst}


def _rh_job(sym: str, unit: str, gated: dict):
    def job():
        st, data, why = _RH_FETCH[unit](sym)
        if st == "off" or (st == "error" and why and why.startswith("robinhood ")):
            gated[unit] = why                         # off / paused / busy: nothing was asked, nothing stored
            return
        _store(sym, unit, st, data, why)
    return job


# ── yfinance (subprocess) ───────────────────────────────────────────────────────

_NOT_FOUND_RE = re.compile(r"Quote not found for symbol", re.IGNORECASE)   # Yahoo's quote endpoint: no such symbol


def _yf_job(sym: str, parts: tuple):
    def job():
        slots = _yf_slots
        if not slots.acquire(timeout=WAIT_S):
            return                                      # all slots busy: nothing stored, the next request retries
        try:
            res = _yf_fetch(_yf_symbol(sym), parts, YF_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            for p in parts:
                _store(sym, f"yf_{p}", "error", None, "yfinance timed out")
            log.warning(f"[ticker_card] yfinance {sym} timed out after {YF_TIMEOUT_S:g}s ({','.join(parts)})")
            return
        except Exception as e:
            for p in parts:
                _store(sym, f"yf_{p}", "error", None, f"yfinance: {type(e).__name__}")
            log.warning(f"[ticker_card] yfinance {sym} failed: {type(e).__name__}: {e}")
            return
        finally:
            slots.release()
        logs = [str(x) for x in (res.get("log") or [])]
        errors = res.get("errors") or {}
        if "import" in errors:
            for p in parts:
                _store(sym, f"yf_{p}", "error", None, "yfinance not importable")
            log.warning(f"[ticker_card] yfinance not importable in the child: {errors['import']}")
            return
        said_missing = any(_NOT_FOUND_RE.search(m) for m in logs)
        if "info" in parts:
            info = res.get("info")
            useful = isinstance(info, dict) and any(info.get(k) for k in ("quoteType", "regularMarketPrice",
                                                                          "longName", "shortName"))
            if useful and str(info.get("currency") or "USD").upper() != "USD":
                _store(sym, "yf_info", "empty", None, f"not a US-dollar listing ({info.get('currency')})")
            elif useful:
                _store(sym, "yf_info", "ok", info)
            elif said_missing:
                _store(sym, "yf_info", "empty", None, "yahoo: quote not found")
            else:
                _store(sym, "yf_info", "error", None, errors.get("info") or "yahoo gave an empty answer")
        if "fin" in parts:
            fin = res.get("fin")
            if isinstance(fin, dict) and fin.get("q_inc") is not None:
                _store(sym, "yf_fin", "ok", fin)
            else:
                _store(sym, "yf_fin", "error", None, errors.get("quarterly_income_stmt") or "no statements")
    return job


# ── sections (pure: cached source answers in, contract objects out) ──────────────

def _quote_from_rh(qrow, frow, frow_at, now):
    """(full quote | None, price-only quote | None). Full needs the prior session's close, by its date."""
    if not isinstance(qrow, dict):
        return None, None
    q, close = qrow.get("quote") or {}, qrow.get("close") or {}
    reg, reg_ts = _pos(q.get("last_trade_price")), _parse_ts(q.get("venue_last_trade_time"))
    if reg is None or reg_ts is None:
        return None, None
    session = _et_date(reg_ts)
    off, off_day = _pos(close.get("price")), _day(close.get("date"))
    price = off if (off is not None and off_day == session.isoformat()) else reg
    q_out = dict.fromkeys(QUOTE_KEYS)
    q_out.update(price=_r(price), ts=_iso(reg_ts), source="robinhood")
    if (isinstance(frow, dict) and frow_at is not None and now - frow_at <= _FUND_FRESH_S
            and _day(frow.get("market_date")) == session.isoformat()):       # the same session's range and volume
        q_out.update(open=_r(_pos(frow.get("open"))), day_high=_r(_pos(frow.get("high"))),
                     day_low=_r(_pos(frow.get("low"))), volume=_int(frow.get("volume"), positive=False))
    prev = _pos(q.get("adjusted_previous_close")) or _pos(q.get("previous_close"))
    prev_day = _day(q.get("previous_close_date"))
    if prev is not None and prev_day is not None and prev_day < session.isoformat():
        prev_close = prev
    elif off is not None and off_day is not None and off_day < session.isoformat():
        prev_close = off
    else:
        return None, q_out                              # after the evening roll-over: no prior-session close here
    full = dict(q_out)
    full.update(prev_close=_r(prev_close), change=_r(price - prev_close),
                change_pct=_r((price - prev_close) / prev_close * 100.0))
    return full, q_out


def _yf_quote_ok(g, now) -> bool:
    """A yfinance .info answer may stand in for the live quote when it is as fresh as a quote, or when it was taken
    while the regular session was closed (its regularMarket* fields do not move then) and is a few minutes old."""
    if not g or g.get("status") != "ok":
        return False
    age = now - g["at"]
    state = str((g.get("data") or {}).get("marketState") or "").upper()
    return age <= QUOTE_TTL_S + 5 or (state not in ("", "REGULAR") and age <= YF_QUOTE_CLOSED_MAX_S)


def _quote_from_yf(info):
    if not isinstance(info, dict):
        return None
    price = _pos(info.get("regularMarketPrice"))
    if price is None:
        return None
    prev = _pos(info.get("regularMarketPreviousClose"))
    out = dict.fromkeys(QUOTE_KEYS)
    out.update(price=_r(price), prev_close=_r(prev), open=_r(_pos(info.get("regularMarketOpen"))),
               day_high=_r(_pos(info.get("regularMarketDayHigh"))), day_low=_r(_pos(info.get("regularMarketDayLow"))),
               volume=_int(info.get("regularMarketVolume"), positive=False),
               ts=_iso(info.get("regularMarketTime")), source="yfinance")
    if prev is not None:
        out.update(change=_r(price - prev), change_pct=_r((price - prev) / prev * 100.0))
    return out


def _ceo_from_officers(officers):
    for o in officers or []:
        title = str((o or {}).get("title") or "")
        if re.search(r"\bCEO\b|chief executive officer", title, re.IGNORECASE):
            name = re.sub(r"^(?:Mr|Ms|Mrs|Dr|Prof)\.?\s+", "", " ".join(str(o.get("name") or "").split()))
            return name or None
    return None


def _profile(rf, yi):
    p, src = dict.fromkeys(PROFILE_KEYS), {}
    rf, yi = rf or {}, yi or {}

    def put(k, v, s):
        if v is not None and p[k] is None:
            p[k], src[k] = v, s
    put("name", _txt(yi.get("longName"), 200) or _txt(yi.get("shortName"), 200), "yfinance")
    put("exchange", _txt(yi.get("fullExchangeName"), 60) or _txt(yi.get("exchange"), 20), "yfinance")
    ys, yind = _txt(yi.get("sector"), 80), _txt(yi.get("industry"), 120)
    rs, rind = _txt(rf.get("sector"), 80), _txt(rf.get("industry"), 120)
    if ys or yind:
        put("sector", ys, "yfinance")
        put("industry", yind, "yfinance")
    else:
        put("sector", rs, "robinhood")
        put("industry", rind, "robinhood")
    put("description", _txt(rf.get("description")), "robinhood")
    put("description", _txt(yi.get("longBusinessSummary")), "yfinance")
    put("ceo", _txt(rf.get("ceo"), 120), "robinhood")
    put("ceo", _ceo_from_officers(yi.get("officers")), "yfinance")
    put("employees", _int(rf.get("num_employees")), "robinhood")
    put("employees", _int(yi.get("fullTimeEmployees")), "yfinance")
    hq = ", ".join(x for x in (_txt(rf.get("headquarters_city"), 60), _txt(rf.get("headquarters_state"), 60)) if x)
    put("hq", hq or None, "robinhood")
    country = _txt(yi.get("country"), 60)
    yhq = ", ".join(x for x in (_txt(yi.get("city"), 60), _txt(yi.get("state"), 60),
                                country if country and country != "United States" else None) if x)
    put("hq", yhq or None, "yfinance")
    yr = _int(rf.get("year_founded"))
    put("founded", yr if yr and 1600 <= yr <= datetime.now(timezone.utc).year else None, "robinhood")
    put("website", _txt(yi.get("website"), 300), "yfinance")
    return p, set(src.values())


def _metrics(rf, yi, ref_price):
    m, src = dict.fromkeys(METRIC_KEYS), set()
    rf, yi = rf or {}, yi or {}
    # size: market cap, shares, float come from ONE source so market cap ~ price x shares holds
    r_size = (_int(rf.get("market_cap")), _int(rf.get("shares_outstanding")), _int(rf.get("float")))
    y_size = (_int(yi.get("marketCap")), _int(yi.get("sharesOutstanding")), _int(yi.get("floatShares")))
    for size, s in ((r_size, "robinhood"), (y_size, "yfinance")):
        if any(v is not None for v in size):
            m["market_cap"], m["shares_outstanding"], m["float"] = size
            src.add(s)
            break
    # valuation: P/E, EPS and forward P/E from ONE source so P/E = price / EPS holds
    eps = _num(yi.get("trailingEps"))
    eps = eps if eps is not None else _num(yi.get("epsTrailingTwelveMonths"))
    eps = eps if eps not in (None, 0.0) else None
    y_val = (_pos(yi.get("trailingPE")), eps, _pos(yi.get("forwardPE")))
    if any(v is not None for v in y_val):
        m["pe_ratio"], m["eps_ttm"], m["forward_pe"] = (_r(v) for v in y_val)
        src.add("yfinance")
    elif _pos(rf.get("pe_ratio")) is not None:
        m["pe_ratio"] = _r(_pos(rf.get("pe_ratio")))
        src.add("robinhood")
    # dividend yield (percent units; the unit is checked against dividend / price whenever that is possible)
    dps, freq = _pos(rf.get("dividend_per_share")), _FREQ.get(str(rf.get("distribution_frequency") or "").lower())
    exp_rh = dps * freq / ref_price * 100.0 if (dps and freq and ref_price) else None
    dy = _as_percent(rf.get("dividend_yield"), exp_rh, default_percent=True)
    if dy is not None:
        m["dividend_yield"] = _r(dy)
        src.add("robinhood")
    else:
        rate, ypx = _pos(yi.get("dividendRate")), _pos(yi.get("regularMarketPreviousClose")) or ref_price
        tay = _pos(yi.get("trailingAnnualDividendYield"))
        exp_yf = rate / ypx * 100.0 if (rate and ypx) else (tay * 100.0 if tay else None)
        dy = _as_percent(yi.get("dividendYield"), exp_yf, default_percent=None)
        if dy is not None:
            m["dividend_yield"] = _r(dy)
            src.add("yfinance")
    # 52-week range (a pair)
    for hi, lo, s in ((_pos(rf.get("high_52_weeks")), _pos(rf.get("low_52_weeks")), "robinhood"),
                      (_pos(yi.get("fiftyTwoWeekHigh")), _pos(yi.get("fiftyTwoWeekLow")), "yfinance")):
        if hi is not None or lo is not None:
            m["high_52w"], m["low_52w"] = _r(hi), _r(lo)
            src.add(s)
            break
    for v, s in ((_int(rf.get("average_volume_30_days")), "robinhood"), (_int(rf.get("average_volume")), "robinhood"),
                 (_int(yi.get("averageVolume")), "yfinance")):
        if v is not None:
            m["avg_volume"] = v
            src.add(s)
            break
    beta = _num(yi.get("beta"))
    if beta not in (None, 0.0):
        m["beta"] = _r(beta)
        src.add("yfinance")
    short, ss, fl = _pos(yi.get("shortPercentOfFloat")), _pos(yi.get("sharesShort")), _pos(yi.get("floatShares"))
    sp = _as_percent(short, ss / fl * 100.0 if (ss and fl) else None, default_percent=False)
    if sp is not None:
        m["short_pct_float"] = _r(sp)
        src.add("yfinance")
    return m, src


def _calendar_label(end: date, annual: bool) -> str:
    if annual:
        return f"FY{end.year}"
    mid = end - timedelta(days=45)                  # the calendar quarter holding most of the period
    return f"{mid.year}-Q{(mid.month - 1) // 3 + 1}"


def _fiscal_labels(ends: list, rh_rows, annual: bool):
    """The company's own fiscal labels for period end dates (newest first), read off Robinhood's earnings list:
    a period's report is the first one dated 1-100 days after its end. Quarters the list does not reach are
    counted from a matched neighbour when the quarters are consecutive. None unless every period gets a label."""
    reports = []
    for r in rh_rows or []:
        y, qn, d = r.get("year"), r.get("quarter"), _day((r.get("report") or {}).get("date"))
        if isinstance(y, int) and not isinstance(y, bool) and isinstance(qn, int) and not isinstance(qn, bool) \
                and 1990 <= y <= 2100 and 1 <= qn <= 4 and d:
            reports.append((date.fromisoformat(d), y, qn))
    if not reports or not ends:
        return None
    lab = []
    for e in ends:
        c = [(d, y, qn) for d, y, qn in reports if 0 < (d - e).days <= 100 and (not annual or qn == 4)]
        lab.append(min(c)[1:] if c else None)
    if annual:
        return [f"FY{x[0]}" for x in lab] if all(lab) else None
    idx = [None if x is None else x[0] * 4 + (x[1] - 1) for x in lab]
    for i in range(len(ends) - 1):                   # matched neighbours must be as many quarters apart as their dates
        gap_q = max(1, round((ends[i] - ends[i + 1]).days / 91.31))
        if idx[i] is not None and idx[i + 1] is not None and idx[i] - idx[i + 1] != gap_q:
            return None
    changed = True
    while changed and any(v is None for v in idx):
        changed = False
        for i in range(len(ends)):
            if idx[i] is not None:
                continue
            if i > 0 and idx[i - 1] is not None and 75 <= (ends[i - 1] - ends[i]).days <= 105:
                idx[i], changed = idx[i - 1] - 1, True
            elif i + 1 < len(ends) and idx[i + 1] is not None and 75 <= (ends[i] - ends[i + 1]).days <= 105:
                idx[i], changed = idx[i + 1] + 1, True
    if any(v is None for v in idx):
        return None
    return [f"FY{v // 4} Q{v % 4 + 1}" for v in idx]


def _financials(fin, yi, rh_rows):
    """(financials object, sources, reason when empty)."""
    empty = {"period_type": None, "periods": []}
    if not isinstance(fin, dict):
        return empty, set(), "no statements"
    fc = str((yi or {}).get("financialCurrency") or "").upper()
    if fc != "USD":
        return empty, set(), (f"reports in {fc}, not US dollars" if fc else "reporting currency unknown")
    keys = ("revenue", "gross_profit", "operating_income", "net_income", "eps")
    for inc_key, cf_key, ptype in (("q_inc", "q_cf", "quarterly"), ("a_inc", "a_cf", "annual")):
        inc = fin.get(inc_key) or []
        cf = {r.get("end_date"): r.get("free_cash_flow") for r in (fin.get(cf_key) or []) if isinstance(r, dict)}
        rows = []
        for r in inc:
            end = _day((r or {}).get("end_date"))
            if not end or not any(_num(r.get(k)) is not None for k in keys):
                continue
            rows.append((end, r))
        rows.sort(key=lambda x: x[0], reverse=True)
        rows = rows[:5]
        if not rows:
            continue
        ends = [date.fromisoformat(e) for e, _ in rows]
        labels = _fiscal_labels(ends, rh_rows, ptype == "annual")
        src = {"yfinance"} | ({"robinhood"} if labels else set())
        labels = labels or [_calendar_label(e, ptype == "annual") for e in ends]
        periods = []
        for (end, r), label in zip(rows, labels):
            p = dict.fromkeys(PERIOD_KEYS)
            p.update(period=label, end_date=end)
            for k in ("revenue", "gross_profit", "operating_income", "net_income"):
                v = _num(r.get(k))
                p[k] = int(round(v)) if v is not None else None
            p["eps"] = _r(_num(r.get("eps")))
            fcf = _num(cf.get(end))
            p["free_cash_flow"] = int(round(fcf)) if fcf is not None else None
            periods.append(p)
        return {"period_type": ptype, "periods": periods}, src, None
    return empty, set(), "no statements"


def _rh_earnings(rows, today: date):
    """(next_earnings, last_earnings) from Robinhood's earnings list. A row dated after today, or today without an
    actual, is upcoming (an 'actual' on a future row is stale prior-quarter data, RULINGS #35); the last report is
    the newest row on or before today that has an actual."""
    parsed = []
    for r in rows or []:
        rep, eps = r.get("report") or {}, r.get("eps") or {}
        d = _day(rep.get("date"))
        if not d:
            continue
        parsed.append({"date": d, "timing": _TIMING.get(str(rep.get("timing") or "").lower()),
                       "confirmed": rep.get("verified") if isinstance(rep.get("verified"), bool) else None,
                       "est": _num(eps.get("estimate")), "act": _num(eps.get("actual"))})
    t = today.isoformat()
    rank = {"bmo": 0, "amc": 1}
    up = [x for x in parsed if x["date"] > t or (x["date"] == t and x["act"] is None)]
    nxt = None
    if up:
        x = min(up, key=lambda x: (x["date"], rank.get(x["timing"], 2)))
        nxt = {"date": x["date"], "timing": x["timing"], "confirmed": x["confirmed"]}
    done = [x for x in parsed if x["act"] is not None and x["date"] <= t]
    last = None
    if done:
        x = max(done, key=lambda x: (x["date"], rank.get(x["timing"], 2)))
        last = {"date": x["date"], "eps_actual": _r(x["act"]), "eps_estimate": _r(x["est"]),
                "surprise_pct": _r((x["act"] - x["est"]) / abs(x["est"]) * 100.0)
                if x["est"] not in (None, 0.0) else None}
    return nxt, last


def _yf_earnings(yi, eh, now: float):
    """(next_earnings, last_earnings) from yfinance. .info earningsTimestamp is the latest report (or the next one
    once Yahoo has it), earningsTimestampStart the next report window; both are real times, read in ET and compared
    with now. A window already in the past is a stale estimate and is ignored (BRK-B 2026-11-18: report 11-07, stale
    window 11-09). The EPS of the last report is earnings_history's newest quarter, when it ended 1-100 days before."""
    yi = yi or {}
    main, start = _pos(yi.get("earningsTimestamp")), _pos(yi.get("earningsTimestampStart"))
    est_flag = yi.get("isEarningsDateEstimate")
    nxt = last = None
    upcoming = sorted(x for x in (start, main) if x and x > now)
    if upcoming:
        dt = datetime.fromtimestamp(upcoming[0], ET)
        timing = None
        if est_flag is False:
            hm = dt.hour * 60 + dt.minute
            timing = "bmo" if hm < 9 * 60 + 30 else ("amc" if hm >= 16 * 60 else None)
        nxt = {"date": dt.date().isoformat(), "timing": timing,
               "confirmed": (not est_flag) if isinstance(est_flag, bool) else None}
    if main and main <= now:
        d = datetime.fromtimestamp(main, ET).date()
        rows = sorted((r for r in (eh or []) if _day(r.get("quarter_end"))), key=lambda r: r["quarter_end"])
        act = est = None
        if rows:
            r = rows[-1]
            qe = date.fromisoformat(_day(r["quarter_end"]))
            if 0 < (d - qe).days <= 100:
                act, est = _num(r.get("eps_actual")), _num(r.get("eps_estimate"))
        last = {"date": d.isoformat(), "eps_actual": _r(act), "eps_estimate": _r(est),
                "surprise_pct": _r((act - est) / abs(est) * 100.0) if (act is not None and est not in (None, 0.0))
                else None}
    return nxt, last


def _dates(rh_rows, rf, yi, eh, now: float):
    out = {"next_earnings": None, "last_earnings": None, "ex_dividend": None, "dividend_pay": None}
    src = set()
    if rh_rows:
        out["next_earnings"], out["last_earnings"] = _rh_earnings(rh_rows, datetime.fromtimestamp(now, ET).date())
        src.add("robinhood")
    elif yi:
        out["next_earnings"], out["last_earnings"] = _yf_earnings(yi, eh, now)
        if out["next_earnings"] or out["last_earnings"]:
            src.add("yfinance")
    r_ex, r_pay = _day((rf or {}).get("ex_dividend_date")), _day((rf or {}).get("payable_date"))
    if r_ex or r_pay:
        out["ex_dividend"], out["dividend_pay"] = r_ex, r_pay
        src.add("robinhood")
    elif yi:
        y_ex, y_pay = _utc_day(yi.get("exDividendDate")), _utc_day(yi.get("dividendDate"))
        if y_ex or y_pay:
            out["ex_dividend"], out["dividend_pay"] = y_ex, y_pay
            src.add("yfinance")
    return out, src


def _analyst(rh_rt, yi, recs):
    if isinstance(rh_rt, dict):
        a = {"buy": _int(rh_rt.get("num_buy_ratings"), positive=False),
             "hold": _int(rh_rt.get("num_hold_ratings"), positive=False),
             "sell": _int(rh_rt.get("num_sell_ratings"), positive=False),
             "target_mean": _r(_pos(rh_rt.get("mean_price_target"))),
             "target_high": _r(_pos(rh_rt.get("high_price_target"))),
             "target_low": _r(_pos(rh_rt.get("low_price_target")))}
        if any(v is not None for v in a.values()):
            return a, {"robinhood"}
    yi = yi or {}
    a = dict.fromkeys(ANALYST_KEYS)
    if isinstance(recs, dict):
        parts = [_int(recs.get(k), positive=False) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")]
        if any(v is not None for v in parts) and sum(v or 0 for v in parts) > 0:
            sb, b, h, s, ss = parts
            a["buy"] = (sb or 0) + (b or 0) if (sb is not None or b is not None) else None
            a["hold"] = h
            a["sell"] = (s or 0) + (ss or 0) if (s is not None or ss is not None) else None
    a["target_mean"] = _r(_pos(yi.get("targetMeanPrice")))
    a["target_high"] = _r(_pos(yi.get("targetHighPrice")))
    a["target_low"] = _r(_pos(yi.get("targetLowPrice")))
    if any(v is not None for v in a.values()):
        return a, {"yfinance"}
    return None, set()


def _missing(payload: dict) -> list:
    """Dotted paths of every null leaf. A whole object section that is entirely null is named once ("quote")."""
    out = []

    def obj(path, d, keys):
        if d is None:
            out.append(path)
            return
        nulls = [k for k in keys if d.get(k) is None]
        if len(nulls) == len(keys):
            out.append(path)
        else:
            out.extend(f"{path}.{k}" for k in nulls)

    obj("profile", payload["profile"], PROFILE_KEYS)
    obj("quote", payload["quote"], QUOTE_KEYS)
    obj("metrics", payload["metrics"], METRIC_KEYS)
    fin = payload["financials"]
    if not fin.get("periods"):
        out.append("financials")
    else:
        for i, p in enumerate(fin["periods"]):
            out.extend(f"financials.periods.{i}.{k}" for k in PERIOD_KEYS if p.get(k) is None)
    d = payload["dates"]
    if all(d.get(k) is None for k in ("next_earnings", "last_earnings", "ex_dividend", "dividend_pay")):
        out.append("dates")
    else:
        if d.get("next_earnings") is None:
            out.append("dates.next_earnings")
        else:
            out.extend(f"dates.next_earnings.{k}" for k in ("date", "timing", "confirmed")
                       if d["next_earnings"].get(k) is None)
        if d.get("last_earnings") is None:
            out.append("dates.last_earnings")
        else:
            out.extend(f"dates.last_earnings.{k}" for k in ("date", "eps_actual", "eps_estimate", "surprise_pct")
                       if d["last_earnings"].get(k) is None)
        out.extend(f"dates.{k}" for k in ("ex_dividend", "dividend_pay") if d.get(k) is None)
    obj("analyst", payload["analyst"], ANALYST_KEYS)
    return out


# ── the card ────────────────────────────────────────────────────────────────────

def _err(code: str, msg: str, status: int) -> tuple:
    return {"ok": False, "error": msg, "code": code}, status


def card(raw_symbol, *, wait_s: float = None) -> tuple:
    """(payload, http status) for one symbol. See the module docstring for the contract."""
    sym, why = normalize_symbol(raw_symbol)
    if not sym:
        return _err("bad_symbol", why, 400)
    try:
        sym = normalize_symbol(_renamed(sym))[0] or sym        # an old ticker that was renamed (PSKY -> SKYD)
    except Exception:
        pass
    budget = WAIT_S if wait_s is None else max(0.0, float(wait_s))
    t0 = time.monotonic()
    gated: dict = {}                                    # unit -> why Robinhood was not asked (this request only)
    rh_futs, futures = [], []

    now = _now()
    snap = _snapshot(sym)
    rh_on = _rh_enabled()
    for unit in _RH_UNITS:
        if rh_on:
            rh_futs += _launch(sym, (unit,), lambda need, unit=unit: _rh_job(sym, unit, gated), now)
        elif _due(snap, unit, now):
            gated[unit] = "robinhood off"
    futures += _launch(sym, ("yf_info", "yf_fin"), lambda need: _yf_job(sym, tuple(u[3:] for u in need)), now)

    # Robinhood answers first (~0.2 s when healthy); then decide whether the quote needs yfinance's .info
    open_rh = [f for f in rh_futs if not f.done()]
    if open_rh:
        _futures_wait(open_rh, timeout=max(0.0, min(RH_WAIT_S, budget - (time.monotonic() - t0))))
    futures += [f for f in rh_futs if f not in futures]
    now = _now()
    snap = _snapshot(sym)
    rq, rfund = _usable(snap, "rh_quote", now), _usable(snap, "rh_fund", now)
    full_rh, _ = _quote_from_rh((rq or {}).get("data") if rq and rq["status"] == "ok" else None,
                                (rfund or {}).get("data") if rfund and rfund["status"] == "ok" else None,
                                rfund["at"] if rfund else None, now)
    rh_said_missing = bool(rq and rq["status"] == "empty") or bool(rfund and rfund["status"] == "empty")
    yi_g = (snap.get("yf_info") or {}).get("good")
    if full_rh is None and not rh_said_missing and not _yf_quote_ok(yi_g, now):
        futures += [f for f in _launch(sym, ("yf_info",), lambda need: _yf_job(sym, ("info",)), now,
                                       fresher_than=QUOTE_TTL_S) if f not in futures]
    left = budget - (time.monotonic() - t0)
    open_futs = [f for f in futures if not f.done()]
    if open_futs and left > 0:
        _futures_wait(open_futs, timeout=left)

    now = _now()
    snap = _snapshot(sym)
    return _assemble(sym, snap, gated, now)


def _assemble(sym: str, snap: dict, gated: dict, now: float) -> tuple:
    u = {k: _usable(snap, k, now) for k in UNITS}

    def data(k):
        g = u.get(k)
        return g["data"] if g and g["status"] == "ok" else None

    rq, rf, re_rows, ran, yi, yfin = (data("rh_quote"), data("rh_fund"), data("rh_earn"), data("rh_analyst"),
                                      data("yf_info"), data("yf_fin"))
    sources, used_at = {}, []

    full_rh, partial_rh = _quote_from_rh(rq, rf, u["rh_fund"]["at"] if u["rh_fund"] else None, now)
    yi_good = (snap.get("yf_info") or {}).get("good")
    quote = full_rh
    if quote is None and _yf_quote_ok(yi_good, now):
        quote = _quote_from_yf(yi_good["data"])
    quote = quote or partial_rh
    sources["quote"] = quote["source"] if quote else None
    if quote is None:
        quote = dict.fromkeys(QUOTE_KEYS)
    elif quote["source"] == "robinhood":
        used_at.append(u["rh_quote"]["at"])
    else:
        used_at.append(yi_good["at"])

    profile, ps = _profile(rf, yi)
    ref_price = quote.get("price") or (rf and _pos(rf.get("market_cap")) and _pos(rf.get("shares_outstanding"))
                                       and _pos(rf.get("market_cap")) / _pos(rf.get("shares_outstanding")))
    metrics, ms = _metrics(rf, yi, ref_price or None)
    financials, fs, _fin_why = _financials(yfin, yi, re_rows)
    eh = (yfin or {}).get("earnings_history")
    dates, ds = _dates(re_rows, rf, yi, eh, now)
    analyst, as_ = _analyst(ran, yi, (yfin or {}).get("recs"))

    has_data = any(quote.get(k) is not None for k in QUOTE_KEYS if k != "source") or \
        any(v is not None for v in profile.values()) or any(v is not None for v in metrics.values()) or \
        bool(financials["periods"]) or any(v is not None for v in dates.values()) or analyst is not None
    if not has_data:
        said_missing = any(g and g["status"] == "empty" for k, g in u.items() if k in ("rh_quote", "rh_fund",
                                                                                       "yf_info"))
        if said_missing:
            return _err("not_found", MSG_NOT_FOUND.format(sym=sym), 404)
        try:
            known = _known(sym)
        except Exception:
            known = True
        if not known:
            return _err("not_found", MSG_NOT_FOUND.format(sym=sym), 404)
        why = {k: ((snap.get(k) or {}).get("err") or gated.get(k) or "no answer yet") for k in UNITS}
        log.warning(f"[ticker_card] {sym}: no source answered ({'; '.join(f'{k}={v}' for k, v in why.items())})")
        return _err("server_error", MSG_SERVER.format(sym=sym), 500)

    if profile["name"] is None:
        try:
            title = _txt(_sec_title(sym), 200)
        except Exception:
            title = None
        if title:
            profile["name"] = title
            ps = ps | {"sec"}

    sources.update(profile=_join_sources(ps), metrics=_join_sources(ms), financials=_join_sources(fs),
                   dates=_join_sources(ds), analyst=_join_sources(as_))
    for k, srcs in (("rh_fund", ps | ms | ds), ("yf_info", ps | ms | ds | as_), ("rh_earn", ds | fs),
                    ("rh_analyst", as_), ("yf_fin", fs | as_)):
        want = "robinhood" if k.startswith("rh_") else "yfinance"
        if u.get(k) and want in srcs:
            used_at.append(u[k]["at"])
    payload = {"ok": True, "symbol": sym, "as_of": _iso(max(used_at) if used_at else now),
               "profile": profile, "quote": quote, "metrics": metrics, "financials": financials, "dates": dates,
               "analyst": analyst, "sources": {k: sources.get(k) for k in SECTIONS}}
    payload["missing"] = _missing(payload)
    return payload, 200


def http_get(args=None) -> tuple:
    """(payload, status) for GET /api/ticker_card?symbol=NVDA. Read-only."""
    args = args or {}
    raw = args.get("symbol") if hasattr(args, "get") else None
    try:
        return card(raw)
    except Exception:
        log.exception(f"[ticker_card] /api/ticker_card failed for {str(raw)[:12]!r}")
        sym = normalize_symbol(raw)[0] or "that symbol"
        return _err("server_error", MSG_SERVER.format(sym=sym), 500)


def status() -> dict:
    """Small health view: cached symbols, fetches in flight, the Robinhood fence."""
    with _lock:
        n, inflight = len(_cache), len(_inflight)
    with _rh_lock:
        mono = time.monotonic()
        rh = {"paused_s": max(0.0, round(_rh["paused_until"] - mono, 1)), "fails": _rh["fails"],
              "last_error": _rh["last_error"], "inflight": _rh["inflight"],
              "calls_in_window": sum(1 for t in _rh["calls"] if mono - t <= RH_WINDOW_S)}
    return {"cached_symbols": n, "fetches_in_flight": inflight, "robinhood": rh}


if __name__ == "__main__":                              # manual: python3 mason_ticker_card.py NVDA
    for a in sys.argv[1:] or ["NVDA"]:
        t = time.time()
        p, s = card(a)
        print(json.dumps(p, indent=1)[:6000])
        print(f"-- {a}: HTTP {s} in {time.time() - t:.2f}s")
