"""
mason_price_cache.py — daily price bars, cross-checked, and THE forward-return grader
(RH Expansion S1-T3, docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §T3; contracts §4 "one grading
function": every forward-return grade in the build — S1, S2, scanners — goes through fwd_return()).

get_daily(ticker, start, end) -> [row]   lazily fills price_bars_daily, then serves from it:
  1. Robinhood day bars (mason_robinhood._call, lane="bulk"): get_equity_historicals
     (adjustment_type="split", ≤6,000-calendar-day windows — RH caps a call at 5,000 bars), or for
     the indexes SPX/NDX/VIX get_indexes → get_index_historicals (≤1,000-day windows; *_value keys,
     calendar days). Interpolated bars are DROPPED (padded weekends/holidays, the unsettled last bar);
     a missing `interpolated` key means False (PROBE.md bullets 4-5, REVIEW_T1).
  2. yfinance (hook _yf_history, auto_adjust=False → both Close and Adj Close, plus Dividends and
     Stock Splits; timeout 10 s). Same call convention as mason_options._expiry_close_price.
  3. Cross-check on close: |rh − yf| / yf > rh_expansion.price_cache.xcheck_tolerance_pct → 'mismatch'
     (both closes + diff in xcheck_json), else 'ok'; one source only → 'single_source'.
  Stored OHLCV come from RH when RH has the bar (source='rh'), else from yfinance (source='yfinance');
  adj_close is yfinance's Adj Close (RH-only rows: adj_close = close, RH has no dividend data).
  xcheck_json always carries rh_close / yf_close / yf_adj_close / diff_pct, plus `div` and `split`
  on ex-dividend and split days (the corporate actions the grader needs).

  Today's bar is never cached (it is unsettled until the next session) — the requested end is
  clamped to yesterday ET. Fetched ranges are tracked per ticker as one contiguous interval in
  desk_state ('price_cache:cov:<T>'); see _refresh for the exact rules. In short: coverage only
  advances to bars actually returned (no holes from empty/lagging answers); a source ERROR never
  advances it; any fetch for a cached ticker also fetches through yesterday (yfinance through today)
  so a split since the cache was filled always resets the cache — one split basis, always.
  Delisted/renamed: 'not_found' marker row (date '0000-00-00'), no retry for 7 days — set when a
  never-seen ticker gets no bar from any source over ≥5 sessions, or when yfinance explicitly answers
  "no data" (raise_errors=True) for a held ticker. Outages/rate limits raise → error → retried.

fwd_return(ticker, entry_date, *, sessions=None, days=None, entry_px=None, entry_rule="on_or_after",
           vs="SPY") -> {ret, xs, entry_date, entry_px, exit_date, exit_px, matured, error}
  * exactly one of `sessions` (Nth trading-session close after entry) or `days` (calendar days from
    the entry session, mapped to the first close on or after) — otherwise a neutral error result.
  * entry = close of the first session on/after entry_date ("on_or_after") or strictly after it
    ("after" — politicians). Sessions are the cached bars themselves (real trading days from the
    data), so holidays/half-days are exactly what the exchange did.
  * return math is dividend- and split-adjusted ("yfinance adj_close wins"): per-row close is the
    yfinance close when yfinance has the row (also on a mismatch), else the RH close, and
        ret = close_X / base × Π_{ex-div d in (E, X]} 1 / (1 − div_d / close_prev(d)) − 1
    which is exactly yfinance's Adj Close ratio adj_X/adj_E, but stays correct when E and X were
    cached by different fetches (a stored adj_close is only coherent within one fetch).
    base = close_E, or for an `entry_px` override (an intraday price in that day's unadjusted
    terms) entry_px / Π splits after E — the same split factor the cached closes carry.
  * xs = ret − vs's ret over the same entry/exit sessions (vs=None → xs None).
  * matured=False until the exit close exists; then ret/xs/exit_* are None — no partial grades.

SWITCHES (decision, S1-T3): rh_expansion.price_cache.enabled is this module's flag; off → every
public function is a cheap no-op (get_daily → [], fwd_return → neutral, no hooks, no DB).
robinhood.enabled (the master switch) gates every Robinhood call here: with it false RH is never
called — but the module keeps working on yfinance alone (rows are 'single_source'). This module is
the shared grader for S2 and the scanners and is not itself RH-only; the master-switch rule covers
the RH-backed part. Once RH is switched on, yfinance-only coverage is re-fetched to be cross-checked.

Public functions never raise. No HTTP routes. status() for /api/rh/status; jobs() → [] (T9).
"""

import json
import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db
import mason_robinhood

log = logging.getLogger("mason.price_cache")

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
_ET = ZoneInfo("America/New_York")
_DEFAULTS = {"enabled": False, "xcheck_tolerance_pct": 0.5}
_cfg_cache = {"mtime": None, "data": None}

_RESULT_KEYS = ("ret", "xs", "entry_date", "entry_px", "exit_date", "exit_px", "matured")
_EQ_WINDOW_DAYS = 6000       # ≈4,130 trading days < RH's 5,000-bar cap per call (PROBE bullet 5)
_IDX_WINDOW_DAYS = 1000      # index day windows: cap is between 1,004 and ~2,100 days → use ≤1,000
_MAX_WINDOWS = 20            # per segment; beyond this the oldest part is skipped and counted as truncated
_EARLIEST = date(1990, 1, 1)
_NOT_FOUND_DATE = "0000-00-00"
_NOT_FOUND_RETRY = timedelta(days=7)
_MAX_SESSIONS = 5000
_MAX_DAYS = 7300
_NOT_FOUND_MIN_SESSIONS = 5  # a window must span this many expected sessions to call a ticker not_found
_MAX_ENTRY_GAP_DAYS = 7     # fwd_return: entry session > 7 calendar days after entry_date → "entry_gap"
_MAX_BAR_GAP_DAYS = 10      # fwd_return: a > 10-day hole between bars inside the horizon → "data_gap"
                            # (longest real market closure in the data era: 2001-09-11 → 09-17, 7 days)
_SUFFIX_RETRY = timedelta(minutes=15)  # a suffix that could not reach yesterday is retried after this
_YF_TIMEOUT_S = 10
_LANE = "bulk"               # grading/backfill work; never the live lane

# RH index symbols → yfinance symbols; ids measured in PROBE.md (fallback if get_indexes fails)
_INDEX_YF = {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX"}
_YF_ALIAS = {v: k for k, v in _INDEX_YF.items()}
_INDEX_IDS_FALLBACK = {"SPX": "432fbbb8-b82c-454a-852d-eb85382c7066",
                       "VIX": "3b912aa2-88f9-4682-8ae3-e39520bdf4db",
                       "NDX": "50c298f7-27a8-44a1-b049-ec153cf2892f"}
_TICKER_RE = re.compile(r"^\^?[A-Z][A-Z0-9.\-]{0,9}$")

_state = {"last_run": None, "errors": [], "truncated": 0, "fetches": 0}
_index_ids = {}
_locks = {}
_locks_guard = threading.Lock()


# ── config / time hooks ──────────────────────────────────────────────────────
def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        blk = ((_config().get("rh_expansion") or {}).get("price_cache") or {})
        out.update({k: v for k, v in blk.items() if v is not None and not str(k).startswith("_")})
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return _cfg().get("enabled") is True
    except Exception:
        return False


def _rh_on() -> bool:
    try:
        return bool(mason_robinhood.enabled())
    except Exception:
        return False


def _tolerance_pct() -> float:
    try:
        t = float(_cfg().get("xcheck_tolerance_pct"))
        return t if t >= 0 else _DEFAULTS["xcheck_tolerance_pct"]
    except Exception:
        return _DEFAULTS["xcheck_tolerance_pct"]


def _today_et() -> date:
    return datetime.now(_ET).date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_trading_day(d: date) -> bool:
    """mason_director.is_trading_day (weekends + its holiday list; half-days count as trading days).
    NOTE: its holiday list starts 2026-11-26, so it is only used for estimates here — session
    counting always uses the real bars."""
    try:
        import mason_director
        return bool(mason_director.is_trading_day(datetime(d.year, d.month, d.day)))
    except Exception:
        return d.weekday() < 5


def _err(where: str, e) -> None:
    msg = f"{_now_utc().isoformat(timespec='seconds')} {where}: {type(e).__name__}: {e}"
    _state["errors"] = (_state["errors"] + [msg])[-10:]
    log.warning("price_cache %s", msg)


def _reset_state() -> None:
    """Test helper."""
    _state.update(last_run=None, errors=[], truncated=0, fetches=0)
    _index_ids.clear()


def _ticker_lock(t: str) -> threading.Lock:
    with _locks_guard:
        if t not in _locks:
            _locks[t] = threading.Lock()
        return _locks[t]


# ── parsing helpers ──────────────────────────────────────────────────────────
def _num(v):
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except Exception:
        return None


def _norm_ticker(t):
    if not isinstance(t, str):
        return None                               # None/bytes/numbers/lists: never stringified into a lookup
    s = t.strip().upper()
    s = _YF_ALIAS.get(s, s)
    return s if _TICKER_RE.match(s) else None


def _yf_symbol(t: str) -> str:
    # R2-A: the shared helper (BRK.B → BRK-B) instead of a blind '.'→'-', which turned DX-Y.NYB into DX-Y-NYB
    import mason_symbols
    return _INDEX_YF.get(t) or mason_symbols.yf_symbol(t)


# ── network hooks ────────────────────────────────────────────────────────────
def _yf_history(symbol: str, start: str, end_excl: str):
    """Network hook (tests monkeypatch it). Returns a yfinance DataFrame (possibly empty)."""
    import yfinance as yf
    # raise_errors=True: an outage / rate limit RAISES (→ 'error', retried) instead of returning an
    # empty frame that would be indistinguishable from "no data" (DEBUG_T3 M2).
    return yf.Ticker(symbol).history(start=start, end=end_excl, interval="1d", auto_adjust=False,
                                     actions=True, timeout=_YF_TIMEOUT_S, raise_errors=True)


def _yf_no_data(e) -> bool:
    """yfinance's own 'no data for this ticker/range' answer (delisted, renamed, pre-IPO) — as opposed
    to an outage, rate limit or timeout, which are errors."""
    try:
        from yfinance.exceptions import YFTickerMissingError
        if isinstance(e, YFTickerMissingError):
            return True
    except Exception:
        pass
    msg = str(e).lower()
    return "delisted" in msg or "no price data" in msg or "no data found" in msg


def _yf_rows(df) -> list:
    """yfinance frame → [{date, open, high, low, close, adj_close, volume, div, split}]."""
    out = []
    try:
        if df is None or getattr(df, "empty", True):
            return []
        for idx, row in df.iterrows():
            try:
                d = idx.date().isoformat()
            except Exception:
                continue
            c = _num(row.get("Close"))
            if c is None or c <= 0:
                continue
            adj = _num(row.get("Adj Close"))
            out.append({"date": d, "open": _num(row.get("Open")), "high": _num(row.get("High")),
                        "low": _num(row.get("Low")), "close": c,
                        "adj_close": adj if adj is not None and adj > 0 else c,
                        "volume": _num(row.get("Volume")),
                        "div": _num(row.get("Dividends")) or 0.0,
                        "split": _num(row.get("Stock Splits")) or 0.0})
    except Exception as e:
        _err("yf_rows", e)
        return []
    return out


def _rh_bars(data, symbol: str, index: bool) -> list:
    """RH historicals payload → non-interpolated [{date, open, high, low, close, volume}]."""
    if not isinstance(data, dict):
        raise ValueError(f"historicals payload is {type(data).__name__}")
    res = data.get("results")
    if not isinstance(res, list):
        raise ValueError("historicals payload has no results list")
    k = "_value" if index else "_price"
    out = []
    for r in res:
        if not isinstance(r, dict) or not isinstance(r.get("bars"), list):
            continue
        if r.get("symbol") and str(r["symbol"]).upper() != symbol:
            continue
        for b in r["bars"]:
            if not isinstance(b, dict) or b.get("interpolated") is True:
                continue
            d = _as_date(b.get("begins_at"))
            c = _num(b.get("close" + k))
            if d is None or c is None or c <= 0:
                continue
            out.append({"date": d.isoformat(), "open": _num(b.get("open" + k)), "high": _num(b.get("high" + k)),
                        "low": _num(b.get("low" + k)), "close": c,
                        "volume": None if index else _num(b.get("volume"))})
    return out


def _index_id(sym: str):
    if sym in _index_ids:
        return _index_ids[sym]
    try:
        data = mason_robinhood._call("get_indexes", {"symbols": ",".join(sorted(_INDEX_YF))}, lane=_LANE)
        for r in (data or {}).get("indexes") or []:
            if isinstance(r, dict) and r.get("id") and r.get("symbol"):
                _index_ids[str(r["symbol"]).upper()] = str(r["id"])
    except Exception as e:
        _err("get_indexes", e)
    return _index_ids.get(sym) or _INDEX_IDS_FALLBACK.get(sym)


def _windows(s: date, e: date, size: int) -> list:
    """Newest-first windows of ≤size days covering [s, e]; capped at _MAX_WINDOWS."""
    out, hi = [], e
    while hi >= s and len(out) < _MAX_WINDOWS:
        lo = max(s, hi - timedelta(days=size))
        out.append((lo, hi))
        hi = lo - timedelta(days=1)
    return out, hi >= s


def _fetch_rh(t: str, s: date, e: date):
    """→ (bars, ok, truncated_from). ok=False on any call error. Bars come back ASCENDING by date, one per
    date, bounded to [s, e] — whatever order the windows are walked in or RH returns bars in (T3-a1/T8-F1:
    windows are walked newest-first, so a plain concatenation is NOT date-ordered)."""
    index = t in _INDEX_YF
    size = _IDX_WINDOW_DAYS if index else _EQ_WINDOW_DAYS
    wins, trunc = _windows(s, e, size)
    bars, ok = [], True
    iid = _index_id(t) if index else None
    if index and not iid:
        return [], False, None
    for lo, hi in wins:
        # RH rejects a window whose start and end fall on the same date (400 "end must be after
        # start" — 1,818 a week, every one a single-day window: the daily one-day suffix). Ask from
        # the day before; the bars are bounded to [s, e] below, so the extra day is never stored.
        q_lo = lo if lo < hi else hi - timedelta(days=1)
        args = {"interval": "day", "start_time": f"{q_lo.isoformat()}T00:00:00Z",
                "end_time": f"{hi.isoformat()}T23:59:59Z"}
        try:
            if index:
                data = mason_robinhood._call("get_index_historicals", dict(args, instrument_ids=[iid]), lane=_LANE)
            else:
                data = mason_robinhood._call("get_equity_historicals",
                                             dict(args, symbols=[t], adjustment_type="split"), lane=_LANE)
            _state["fetches"] += 1
            bars.extend(_rh_bars(data, t, index))
        except Exception as ex:
            ok = False
            _err(f"rh {t} {lo}..{hi}", ex)
    lo_s, hi_s = s.isoformat(), e.isoformat()
    by_date = {b["date"]: b for b in bars if lo_s <= b["date"] <= hi_s}
    oldest = min((w[0] for w in wins), default=None)      # truncation drops the OLDEST part of the range
    return [by_date[d] for d in sorted(by_date)], ok, (oldest if trunc else None)


def _fetch_yf(t: str, s: date, e: date, start: date | None = None, through: date | None = None):
    """→ (rows in [s, e], split dates seen in the whole request, kind) with kind 'ok' | 'no_data' (yfinance's
    explicit no-data/delisted exception) | 'empty_frame' (no exception, no rows — ambiguous) | 'error'.
    `start` (≤ s) and `through` (≥ e, ≤ today) only widen the REQUEST so every split since the cache's last
    basis check — even one whose ex-date is today — is seen; only bars in [s, e] are returned for storage."""
    lo = min(s, start) if start else s
    upto = max(e, through or e)
    try:
        df = _yf_history(_yf_symbol(t), lo.isoformat(), (upto + timedelta(days=1)).isoformat())
        _state["fetches"] += 1
    except Exception as ex:
        if _yf_no_data(ex):
            return [], [], "no_data"
        _err(f"yfinance {t} {lo}..{upto}", ex)
        return [], [], "error"
    allr = _yf_rows(df)
    allr = sorted({r["date"]: r for r in allr}.values(), key=lambda r: r["date"])   # never trust frame order
    rows = [r for r in allr if s.isoformat() <= r["date"] <= e.isoformat()]
    splits = [r["date"] for r in allr if r["split"]]
    return rows, splits, ("ok" if allr else "empty_frame")


# ── storage ──────────────────────────────────────────────────────────────────
def _cov_key(t: str) -> str:
    return f"price_cache:cov:{t}"


def _basis_key(t: str) -> str:
    return f"price_cache:basis:{t}"


def _kv_get(conn, key: str):
    r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
    if not r:
        return None
    try:
        v = json.loads(r["value"])
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _kv_set(conn, key: str, v: dict) -> None:
    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                 (key, json.dumps(v, sort_keys=True), _now_utc().isoformat(timespec="seconds")))


def _dt(v):
    try:
        return datetime.fromisoformat(v) if v else None
    except Exception:
        return None


def _get_cov(conn, t: str):
    """Coverage: the one contiguous [lo, hi] fetched; rh = cross-checked by RH; tried = last suffix attempt
    that could not reach yesterday; nodata = first of two explicit yfinance no-data answers (two-strike)."""
    v = _kv_get(conn, _cov_key(t))
    if not v:
        return None
    try:
        return {"lo": date.fromisoformat(v["lo"]), "hi": date.fromisoformat(v["hi"]), "rh": bool(v.get("rh")),
                "tried": _dt(v.get("tried")), "nodata": _dt(v.get("nodata"))}
    except Exception:
        return None


def _set_cov(conn, t: str, lo: date, hi: date, rh: bool, tried=None, nodata=None) -> None:
    v = {"lo": lo.isoformat(), "hi": hi.isoformat(), "rh": rh}
    if tried:
        v["tried"] = tried.isoformat(timespec="seconds")
    if nodata:
        v["nodata"] = nodata.isoformat(timespec="seconds")
    _kv_set(conn, _cov_key(t), v)


def _get_basis(conn, t: str):
    """Split basis of the cached rows: asof = date of the last successful yfinance split check (every split
    dated ≤ asof that Yahoo had posted is reflected in every cached row); split = newest split applied.
    None (no key) = unknown basis → the next fetch checks from the oldest cached row."""
    v = _kv_get(conn, _basis_key(t))
    if v is None:
        return None
    try:
        return {"asof": date.fromisoformat(v["asof"]) if v.get("asof") else None, "split": v.get("split")}
    except Exception:
        return None


def _set_basis(conn, t: str, asof, split) -> None:
    _kv_set(conn, _basis_key(t), {"asof": asof.isoformat() if asof else None, "split": split})


def _coverage(t: str):
    conn = mason_db.get_connection()
    try:
        return _get_cov(conn, t)
    finally:
        conn.close()


def _merge(rh: list, yf: list, tol: float) -> list:
    rmap = {b["date"]: b for b in rh}
    ymap = {b["date"]: b for b in yf}
    out = []
    for d in sorted(set(rmap) | set(ymap)):
        r, y = rmap.get(d), ymap.get(d)
        x = {}
        if y:
            x.update(yf_close=y["close"], yf_adj_close=y["adj_close"])
            if y["div"]:
                x["div"] = y["div"]
            if y["split"]:
                x["split"] = y["split"]
        if r:
            x["rh_close"] = r["close"]
        if r and y:
            diff = abs(r["close"] - y["close"]) / y["close"] * 100.0
            x["diff_pct"] = diff
            x["tolerance_pct"] = tol
            status = "mismatch" if diff > tol else "ok"
        else:
            status = "single_source"
        src = r or y
        out.append({"date": d, "open": src["open"], "high": src["high"], "low": src["low"], "close": src["close"],
                    "adj_close": y["adj_close"] if y else r["close"], "volume": src["volume"],
                    "source": "rh" if r else "yfinance", "xcheck_status": status,
                    "xcheck_json": json.dumps(x, sort_keys=True)})
    return out


def _upsert(conn, t: str, rows: list) -> None:
    now = _now_utc().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO price_bars_daily (ticker, date, open, high, low, close, adj_close, volume, source, "
        "xcheck_status, xcheck_json, created_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker, date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume, source=excluded.source, "
        "xcheck_status=excluded.xcheck_status, xcheck_json=excluded.xcheck_json",
        [(t, r["date"], r["open"], r["high"], r["low"], r["close"], r["adj_close"], r["volume"], r["source"],
          r["xcheck_status"], r["xcheck_json"], now) for r in rows])


def _sessions_in(s: date, e: date, cap: int = _NOT_FOUND_MIN_SESSIONS) -> int:
    """Expected trading sessions in [s, e] (mason_director calendar), counted up to `cap` (bounded)."""
    n, d = 0, s
    while d <= e and n < cap:
        n += _is_trading_day(d)
        d += timedelta(days=1)
    return n


def _ingest_segment(t: str, s: date, e: date, *, through: date, yf_start: date | None = None,
                    had_rows: bool = False, basis_split: str | None = None) -> dict:
    """Fetch [s, e] from both sources, merge, store. Opens/closes its own connection; raises only on DB
    errors (callers catch). → {ok, empty, kind, rows, rh, last, checked, max_split, new_split, stored, truncated_from}
      ok        False on any source error; on expected sessions with yfinance empty but RH answering
                (retry for the cross-check and dividends); or when the ticker holds rows and the split
                check could not run (nothing is stored then — one split basis, always).
      checked   yfinance answered for [min(s, yf_start), through]: every split in that span was seen.
      new_split the ticker holds rows and a split newer than the recorded basis appeared → caller resets.
      empty     sessions were expected and no source returned a bar.
      last      newest date coverage may advance to: yfinance's last bar, or with RH on and answering,
                the last bar both returned (an unsettled/lagging bar is re-fetched next time)."""
    rh_on = _rh_on()
    rh_bars, rh_ok, trunc = _fetch_rh(t, s, e) if rh_on else ([], True, None)
    yf_bars, splits, kind = _fetch_yf(t, s, e, yf_start, through)
    if trunc:
        _state["truncated"] += 1
        _err(f"windows {t}", RuntimeError(f"range truncated at _MAX_WINDOWS; RH fetched from {trunc}"))
    rows = _merge(rh_bars, yf_bars, _tolerance_pct())
    expected = _sessions_in(s, e, 1) > 0
    ok = rh_ok and kind != "error"
    if ok and expected and not yf_bars and rh_bars:
        ok = False
    checked = kind == "ok"
    max_split = max(splits) if splits else None
    new_split = bool(had_rows and checked and max_split and (basis_split is None or max_split > basis_split))
    if had_rows and not checked:
        store = False                     # cannot prove these rows share the cached basis
        if rows:
            ok = False
    else:
        store = bool(rows) and not new_split
    empty = ok and expected and not rows
    last = None
    if yf_bars:
        last = max(b["date"] for b in yf_bars)
        if rh_on and rh_bars:
            last = min(last, max(b["date"] for b in rh_bars))   # newest bar BOTH returned (T8-F1)
    elif rows and not expected:
        last = max(r["date"] for r in rows)
    if store:
        conn = mason_db.get_connection()
        try:
            _upsert(conn, t, rows)
            conn.execute("DELETE FROM price_bars_daily WHERE ticker=? AND date=?", (t, _NOT_FOUND_DATE))
            conn.commit()
        finally:
            conn.close()
    return {"ok": ok, "empty": empty, "kind": kind, "rows": len(rows), "rh": rh_on and rh_ok,
            "last": date.fromisoformat(last) if last else None, "checked": checked, "max_split": max_split,
            "new_split": new_split, "stored": store, "truncated_from": trunc}


def _extend_hi(last: date, b: date) -> date:
    """Coverage may run past the last bar only over days that are not sessions (weekend/holiday tail)."""
    d = last
    while d < b and not _is_trading_day(d + timedelta(days=1)):
        d += timedelta(days=1)
    return d


def _not_found_recent(conn, t: str) -> bool:
    r = conn.execute("SELECT created_date FROM price_bars_daily WHERE ticker=? AND date=?",
                     (t, _NOT_FOUND_DATE)).fetchone()
    if not r:
        return False
    try:
        return _now_utc() - datetime.fromisoformat(r["created_date"]) < _NOT_FOUND_RETRY
    except Exception:
        return False


def _mark_not_found(conn, t: str) -> None:
    conn.execute("INSERT INTO price_bars_daily (ticker, date, source, xcheck_status, xcheck_json, created_date) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(ticker, date) DO UPDATE SET created_date=excluded.created_date",
                 (t, _NOT_FOUND_DATE, "none", "not_found", json.dumps({"reason": "no bars from any source"}),
                  _now_utc().isoformat(timespec="seconds")))


def _refresh(t: str, s: date, e: date) -> None:
    """Make the cache cover [s, e] (e ≤ yesterday). Bounded: ≤2 passes (the second only after a split reset).

    Split basis (DEBUG_T3 D2/D3/D4): desk_state 'price_cache:basis:<T>' records {asof, split}. Every
    yfinance request for a ticker that holds rows — prefix, suffix or first — spans
    [min(s, asof − 7 d), today], so any split since the last check (even today's ex-date, even one Yahoo
    posts late in the day) is seen. A split newer than basis.split resets the ticker (rows + coverage +
    basis) and pass 2 re-fills in the new basis; the reset records that split, so it never repeats. If the
    check cannot run (yfinance error / no answer), nothing new is stored for a held ticker.
    Coverage (D1): one contiguous [lo, hi]; hi advances only to bars actually returned (plus a
    weekend/holiday tail); errors never advance it; an empty prefix is pre-history; a suffix that could not
    reach yesterday is retried after _SUFFIX_RETRY. not_found: a never-seen ticker with no bar over ≥5
    sessions, or a held ticker after TWO explicit yfinance no-data answers ≥ _SUFFIX_RETRY apart; cached
    rows are still served and older prefixes still fetched.
    """
    with _ticker_lock(t):
        today = _today_et()
        yday = today - timedelta(days=1)
        for _pass in range(2):
            conn = mason_db.get_connection()
            try:
                nf = _not_found_recent(conn, t)
                cov = _get_cov(conn, t)
                basis = _get_basis(conn, t)
                if cov and _rh_on() and not cov["rh"]:
                    cov = None                    # RH switched on since: re-fetch to cross-check
                first_row = conn.execute("SELECT MIN(date) FROM price_bars_daily WHERE ticker=? AND date<>?",
                                         (t, _NOT_FOUND_DATE)).fetchone()[0]
            finally:
                conn.close()
            had_rows = first_row is not None
            now = _now_utc()
            segs = []
            if cov is None:
                if nf:
                    return
                segs.append(("first", s, e))
            else:
                if s < cov["lo"]:
                    segs.append(("prefix", s, cov["lo"] - timedelta(days=1)))
                throttled = bool(cov["tried"] and now - cov["tried"] < _SUFFIX_RETRY)
                if ((segs or e > cov["hi"]) and not nf and not throttled and cov["hi"] < yday
                        and _sessions_in(cov["hi"] + timedelta(days=1), today, 1) > 0):
                    segs.append(("suffix", cov["hi"] + timedelta(days=1), yday))
            if not segs:
                return
            yf_start = None
            if had_rows:
                yf_start = (basis["asof"] - timedelta(days=7)) if basis and basis["asof"] \
                    else date.fromisoformat(first_row)
            bsplit = basis["split"] if basis else None
            _state["last_run"] = now.isoformat(timespec="seconds")
            results = [(k, a, b, _ingest_segment(t, a, b, through=today, yf_start=yf_start, had_rows=had_rows,
                                                 basis_split=bsplit))
                       for k, a, b in segs]
            conn = mason_db.get_connection()
            try:
                if any(r["new_split"] for _, _, _, r in results):
                    conn.execute("DELETE FROM price_bars_daily WHERE ticker=? AND date<>?", (t, _NOT_FOUND_DATE))
                    conn.execute("DELETE FROM desk_state WHERE key IN (?, ?)", (_cov_key(t), _basis_key(t)))
                    conn.commit()
                    log.info("price_cache %s: new split seen — cache reset and re-fetched", t)
                    continue
                checked = [r for _, _, _, r in results if r["checked"]]
                if checked:
                    seen = [r["max_split"] for r in checked if r["max_split"]] + ([bsplit] if bsplit else [])
                    _set_basis(conn, t, today, max(seen) if seen else None)
                elif not had_rows and any(r["stored"] for _, _, _, r in results):
                    _set_basis(conn, t, None, None)   # RH-only first fill: next fetch checks from the start
                lo, hi = (cov["lo"], cov["hi"]) if cov else (None, None)
                rh_cov = cov["rh"] if cov else True
                tried = cov["tried"] if cov else None
                nodata = cov["nodata"] if cov else None
                for k, a, b, r in results:
                    if k == "suffix":
                        tried = None
                    if not r["ok"]:
                        if k == "suffix":
                            tried = now
                        continue                  # incomplete run: coverage must not advance
                    if r["empty"]:
                        if k == "prefix":
                            lo = a                # nothing that old exists: pre-history
                        elif k == "first":
                            if (not had_rows and r["kind"] in ("no_data", "empty_frame")
                                    and _sessions_in(a, b) >= _NOT_FOUND_MIN_SESSIONS):
                                _mark_not_found(conn, t)      # never seen anywhere: delisted/renamed/unknown
                        else:
                            tried = now
                            if r["kind"] == "no_data" and _sessions_in(a, b) >= _NOT_FOUND_MIN_SESSIONS:
                                if nodata and now - nodata >= _SUFFIX_RETRY:
                                    _mark_not_found(conn, t)  # second explicit no-data answer: delisted
                                    nodata = None
                                elif not nodata:
                                    nodata = now              # first strike — Yahoo is sometimes transient
                        continue
                    a = r["truncated_from"] or a  # truncated: only the fetched (newest) part counts
                    top = _extend_hi(r["last"], b) if r["last"] else b
                    if k == "prefix":
                        lo = a                    # prefix ends at lo-1 → stays contiguous
                    elif k == "first":
                        lo, hi = a, top
                    else:
                        hi = max(hi, top)
                        nodata = None
                        if hi < yday:
                            tried = now           # short/lagging answer: retry later, not every call
                    rh_cov = rh_cov and r["rh"]
                if lo is not None:
                    _set_cov(conn, t, lo, hi, rh_cov, tried, nodata)
                conn.commit()
            finally:
                conn.close()
            return


def _read(t: str, s: date, e: date) -> list:
    conn = mason_db.get_connection()
    try:
        rows = conn.execute("SELECT ticker, date, open, high, low, close, adj_close, volume, source, xcheck_status, "
                            "xcheck_json, created_date FROM price_bars_daily WHERE ticker=? AND date>=? AND date<=? "
                            "AND date<>? AND xcheck_status<>'not_found' ORDER BY date",
                            (t, s.isoformat(), e.isoformat(), _NOT_FOUND_DATE)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── public API ───────────────────────────────────────────────────────────────
def get_daily(ticker, start, end) -> list:
    """Cached, cross-checked daily bars for [start, end] (end clamped to yesterday ET). Never raises."""
    try:
        if not enabled():
            return []
        t, s, e = _norm_ticker(ticker), _as_date(start), _as_date(end)
        if not t or s is None or e is None:
            return []
        s = max(s, _EARLIEST)
        e = min(e, _today_et() - timedelta(days=1))
        if s > e:
            return []
        _refresh(t, s, e)
        return _read(t, s, e)
    except Exception as ex:
        _err(f"get_daily {ticker}", ex)
        return []


def _neutral(error=None, **kw) -> dict:
    out = {"ret": None, "xs": None, "entry_date": None, "entry_px": None, "exit_date": None,
           "exit_px": None, "matured": False, "error": error}
    out.update(kw)
    return out


def _px(row: dict):
    """The close used for return math: yfinance's when it has the row (wins on mismatch), else RH's."""
    try:
        x = json.loads(row.get("xcheck_json") or "{}")
    except Exception:
        x = {}
    v = _num(x.get("yf_close"))
    return v if v is not None and v > 0 else _num(row.get("close"))


def _action(row: dict, key: str) -> float:
    try:
        return _num(json.loads(row.get("xcheck_json") or "{}").get(key)) or 0.0
    except Exception:
        return 0.0


def _div_factor(bars: list, i: int, j: int):
    """Π 1/(1 − div/prev_close) over ex-dates in (bars[i], bars[j]]; None if undefined."""
    f = 1.0
    for k in range(i + 1, j + 1):
        dv = _action(bars[k], "div")
        if dv:
            prev = _px(bars[k - 1])
            if not prev or dv >= prev:
                return None
            f /= (1.0 - dv / prev)
    return f


def _pos_int(v, cap):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= cap


def fwd_return(ticker, entry_date, *, sessions=None, days=None, entry_px=None, entry_rule="on_or_after",
               vs="SPY") -> dict:
    """THE forward-return grader (contracts §4). See the module docstring. Never raises."""
    try:
        if not enabled():
            return _neutral("disabled")
        if (sessions is None) == (days is None):
            return _neutral("exactly one of sessions or days is required")
        if sessions is not None and not _pos_int(sessions, _MAX_SESSIONS):
            return _neutral("sessions must be an integer >= 1")
        if days is not None and not _pos_int(days, _MAX_DAYS):
            return _neutral("days must be an integer >= 1")
        if entry_rule not in ("on_or_after", "after"):
            return _neutral("entry_rule must be 'on_or_after' or 'after'")
        t, d0 = _norm_ticker(ticker), _as_date(entry_date)
        if not t or d0 is None:
            return _neutral("bad ticker or entry_date")
        override = None
        if entry_px is not None:
            override = _num(entry_px)
            if override is None or override <= 0:
                return _neutral("entry_px must be a positive number")
        yday = _today_et() - timedelta(days=1)
        span = (sessions * 3 // 2 + 10) if sessions else (days + 10)
        end = d0 + timedelta(days=span + 4)
        if override is not None:
            end = yday                            # current split basis needs every split since entry
        bars = [b for b in get_daily(t, d0, min(end, yday)) if (_px(b) or 0) > 0]
        key = d0.isoformat()
        i = next((k for k, b in enumerate(bars)
                  if (b["date"] >= key if entry_rule == "on_or_after" else b["date"] > key)), None)
        cov = _coverage(t)
        if cov is None or cov["lo"] > d0:
            # [entry_date, …] was never fully fetched (error, throttle, not_found): never grade from a later bar
            return _neutral(None if d0 >= yday - timedelta(days=4) else "incomplete_data")
        if i is None:
            return _neutral(None if d0 >= yday - timedelta(days=4) else "no entry session in data")
        E = bars[i]
        if (date.fromisoformat(E["date"]) - d0).days > _MAX_ENTRY_GAP_DAYS:
            return _neutral("entry_gap")          # IPO / long halt / reused ticker: not this security's entry
        base = _px(E)
        if override is not None:
            split = 1.0
            for b in bars[i + 1:]:
                r = _action(b, "split")
                if r > 0:
                    split *= r
            base = override / split
        res = _neutral(None, entry_date=E["date"], entry_px=override if override is not None else _px(E))
        stale = None if cov["hi"] >= yday - timedelta(days=4) else "incomplete_data"
        if sessions:
            j = i + sessions
            if j >= len(bars):
                return dict(res, error=stale)
        else:
            target = (date.fromisoformat(E["date"]) + timedelta(days=days)).isoformat()
            j = next((k for k in range(i + 1, len(bars)) if bars[k]["date"] >= target), None)
            if j is None:
                return dict(res, error=stale)
        X = bars[j]
        if date.fromisoformat(X["date"]) > cov["hi"]:
            return dict(res, error="incomplete_data")   # sessions before X not all proven present
        if any((date.fromisoformat(bars[k]["date"]) - date.fromisoformat(bars[k - 1]["date"])).days
               > _MAX_BAR_GAP_DAYS for k in range(i + 1, j + 1)):
            return dict(res, error="data_gap")
        df = _div_factor(bars, i, j)
        if df is None or not base:
            return dict(res, error="bad dividend/price data")
        ret = _px(X) / base * df - 1.0
        xs = None
        vt = _norm_ticker(vs) if vs else None
        if vt == t:
            xs = 0.0
        elif vt:
            vb = [b for b in get_daily(vt, date.fromisoformat(E["date"]), date.fromisoformat(X["date"]))
                  if (_px(b) or 0) > 0]
            if len(vb) >= 2 and vb[0]["date"] == E["date"] and vb[-1]["date"] == X["date"]:
                vdf = _div_factor(vb, 0, len(vb) - 1)
                if vdf is not None:
                    xs = ret - (_px(vb[-1]) / _px(vb[0]) * vdf - 1.0)
        return dict(res, ret=ret, xs=xs, exit_date=X["date"], exit_px=_px(X), matured=True)
    except Exception as ex:
        _err(f"fwd_return {ticker}", ex)
        return _neutral(f"{type(ex).__name__}")


_STATUS_ACCT_RE = re.compile(r"(?<![\w.])\d{8,12}(?![\w.])")     # standalone account-number-length runs


def _status_redact(s) -> str:
    """status() errors keep only the last 4 digits of any account-number-length run (contracts §9.6)."""
    return _STATUS_ACCT_RE.sub(lambda m: "••" + m.group(0)[-4:], str(s))[:300]


def status() -> dict:
    """For /api/rh/status (contracts §6). Never raises; flag off → no DB access; never calls RH."""
    out = {"name": "price_cache", "enabled": enabled(), "last_run": _state["last_run"], "rows": 0,
           "errors": list(_state["errors"]), "truncated": _state["truncated"], "rate_hits": 0,
           "xcheck_mismatch": 0, "rh_enabled": _rh_on()}
    if not out["enabled"]:
        return out
    try:
        out["rate_hits"] = int(((mason_robinhood.status() or {}).get("rate") or {}).get("rate_hits") or 0)
    except Exception:
        pass
    try:
        conn = mason_db.get_connection()
        try:
            r = conn.execute("SELECT COUNT(*) AS n, SUM(xcheck_status='mismatch') AS m FROM price_bars_daily "
                             "WHERE date<>?", (_NOT_FOUND_DATE,)).fetchone()
            out["rows"], out["xcheck_mismatch"] = int(r["n"] or 0), int(r["m"] or 0)
        finally:
            conn.close()
    except Exception as e:
        out["errors"] = out["errors"] + [f"status: {type(e).__name__}: {e}"]
    out["errors"] = [_status_redact(e) for e in out["errors"]]
    return out


def jobs() -> list:
    """S1 job contract (mason_rh_history, above jobs()): nothing to schedule — the cache is lazy by design.
    get_daily() refreshes a ticker on demand (missing range, plus a suffix up to yesterday retried every
    15 min), and every consumer already pulls exactly what it needs inside its own job: regimes at 17:05
    (SPX/VIX/RSP/SPY…), PEAD priors at 17:25 (graded tickers + SPY), option_bars underlying closes. A separate
    warm-up job would only duplicate those bulk-lane calls (and fetch tickers nobody reads)."""
    return []
