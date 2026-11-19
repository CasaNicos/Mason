"""
mason_watch_tape.py — the watchlist TAPE (docs/watchlist/WATCHLIST_CONTRACTS.md §4).

Keeps 1-minute bars for every symbol in mason_watchlist.universe() (items ∪ open
positions ∪ infra ETFs) and rolls them up to 5m and 1d in `watch_bars`:

  * IEX live stream   — Alpaca WS wss://stream.data.alpaca.markets/v2/iex, 07:55–17:05 ET
                        on trading days; diff-subscribe as the universe changes; reconnect
                        with exponential backoff (cap 5 min) and REST-backfill the gap.
                        mode "rest_poll" (watchlist.stream.mode) polls /v2/stocks/bars/latest
                        every minute instead, with the same listener contract. Repeated
                        406 "connection limit exceeded" degrades ws → rest_poll for the day.
  * extended hours    — yfinance 1m prepost bars 04:00–08:00 / 17:00–20:00 ET (source 'yf').
  * SIP reconcile     — after the close, re-pull the day from Alpaca historical SIP (free
                        when the window ends >15 min ago); 'sip' rows overwrite 'iex'/'yf'.
  * options snapper   — every N min in RTH: Alpaca indicative option snapshots → ATM IV,
                        ATM straddle mid, implied move for the nearest expiry with dte ≥ min_dte
                        (yfinance chain fallback; no-options symbols negative-cached 1 day).

K1 probe (2026-09-30, docs/watchlist/K1_probe.md): the news WS and /v2/iex count
separately — both authenticate concurrently — but a SECOND /v2/iex connection on the
same account gets 406. This module must be the only /v2/iex consumer.

Everything is behind `watchlist.enabled` (+ per-feature sub-flags), all default false.
Public functions never raise; daemon threads never die; every DB connection is closed.
Network goes through module-level hooks (_ws_connect, _alpaca_data_get, _yf_download,
_yf_option_chain) and the clock through _now, so tests monkeypatch them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db

log = logging.getLogger("mason.watch_tape")
logging.getLogger("websockets").setLevel(logging.WARNING)

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
ET = ZoneInfo("America/New_York")

_IEX_WS = "wss://stream.data.alpaca.markets/v2/iex"
_DATA_BASE = "https://data.alpaca.markets"
_SIP_DELAY_S = 15 * 60 + 30          # SIP allowed once the window END is >15 min old (+30s margin)
_BACKOFF_CAP_S = 300
_LIMIT_DEGRADE_AFTER = 3             # consecutive 406s before ws → rest_poll for the ET day
_HEALTHY_S = 60                      # a WS session this long resets the reconnect backoff
_SUB_CHECK_S = 5                     # how often the WS session diffs the universe
_NEG_CACHE_S = 86400                 # no-options negative cache
_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_BATCH = 100                         # symbols per REST call

# ── config (read per call, mtime-cached, merged over _DEFAULTS) ────────────────
_cfg_cache = {"mtime": None, "data": {}}
_DEFAULTS = {
    "enabled": False,
    "infra_symbols": [],
    "stream": {"enabled": False, "mode": "ws", "backfill_minutes": 120},
    # empty_alert_after_et: from this ET time on, a pre-market in which EVERY Yahoo poll came back
    # empty (all symbols "no data yet") shows as not-ok in /api/health (premarket_health; blank =
    # off). Live 20 Oct-16 Nov: 33+ symbols had bars in every pre-market hour from 04:00, so two
    # hours of nothing is not a quiet morning. empty_alert_min_polls: polls needed to judge.
    "extended": {"enabled": False, "poll_seconds": 120, "empty_alert_after_et": "06:00",
                 "empty_alert_min_polls": 5},
    "sip_reconcile": {"enabled": False, "at_et": "16:20"},
    "options": {"enabled": False, "every_minutes": 15, "min_dte": 7},
    "retention": {"bars_1m_days": 30, "bars_5m_days": 365},
}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime_ns
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    try:
        wl = _config().get("watchlist") or {}
        for k, v in wl.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            elif v is not None:
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return bool(_cfg().get("enabled"))
    except Exception:
        return False


def _sub(name: str) -> dict:
    return _cfg().get(name) or {}


def _creds():
    try:
        import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
        return _S.alpaca_keys(cfg=_config())
    except Exception:
        return "", ""


# ── network + clock hooks (tests monkeypatch these) ────────────────────────────

def _now() -> float:
    return time.time()


def _ws_connect(url):
    """Async context manager for one WS connection."""
    import websockets
    return websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5,
                              open_timeout=10, max_size=2 ** 22)


def _alpaca_data_get(path: str, params: dict):
    """GET data.alpaca.markets<path>. 200 → parsed JSON; other status →
    {"_http_status": code, "message": ...}; transport failure → None. Never raises."""
    try:
        import requests
        key, secret = _creds()
        if not key or not secret:
            return None
        r = requests.get(_DATA_BASE + path, params=params, timeout=10,
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        if r.status_code == 200:
            return r.json()
        return {"_http_status": r.status_code, "message": (r.text or "")[:200]}
    except Exception as e:
        _err(f"alpaca data GET {path}: {e}")
        return None


def _yfs(sym: str) -> str:
    """Yahoo's spelling of a symbol (BRK.B → BRK-B): the shared mason_symbols.yf_symbol (R2-A; this
    module used its own '.'→'-' replace before)."""
    import mason_symbols
    return mason_symbols.yf_symbol(sym)


def _yf_download(tickers, **kw):
    try:
        import yfinance as yf
        return yf.download(list(tickers), **kw)
    except Exception as e:
        _err(f"yf download: {e}")
        return None


def _yf_quiet():
    """Pre-market minute bars: a symbol that has not traded yet this morning comes back empty and
    yfinance logs "possibly delisted" at ERROR (~7,650 a week, all live symbols). Expected, so
    counted by mason_logsetup instead of logged (audit O8)."""
    try:
        import mason_logsetup
        return mason_logsetup.quiet_yf_no_data()
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _yf_option_chain(ticker: str, min_expiry: str):
    """(underlying | None, [{"expiry","type","strike","bid","ask","iv"}]) for the first
    listed expiry ≥ min_expiry. ([], None-underlying) when none."""
    try:
        import yfinance as yf
        tk = yf.Ticker(_yfs(ticker))
        exps = [e for e in (tk.options or ()) if e >= min_expiry]
        if not exps:
            return None, []
        ch = tk.option_chain(exps[0])
        und = None
        try:
            und = float((getattr(ch, "underlying", None) or {}).get("regularMarketPrice"))
        except Exception:
            und = None
        out = []
        for typ, df in (("C", ch.calls), ("P", ch.puts)):
            for _, r in df.iterrows():
                out.append({"expiry": exps[0], "type": typ, "strike": float(r.get("strike")),
                            "bid": _f(r.get("bid")), "ask": _f(r.get("ask")),
                            "iv": _f(r.get("impliedVolatility"))})
        return und, out
    except Exception as e:
        _err(f"yf option chain {ticker}: {e}")
        return None, None


def _watchlist_universe():
    import mason_watchlist
    return mason_watchlist.universe()


# ── runtime state ──────────────────────────────────────────────────────────────
_lock = threading.RLock()
_listeners: list = []
_errors: deque = deque(maxlen=5)
_THREADS: list = []
_started = False
_stop = threading.Event()
_state: dict = {}
_last_seen: dict = {}        # ticker → last stored/emitted 1m ts
_no_opts: dict = {}          # ticker → negative-cache expiry epoch
_daily_fetched: dict = {}    # ticker → (et_date, days) REST daily fetch memo


def _fresh_state() -> dict:
    return {"connected": False, "last_bar_ts": 0, "subscribed": set(), "universe": {},
            "universe_ts": 0.0, "backoff": 1, "limit_hits": 0, "degraded_date": None,
            "last_connect_ts": 0, "ext_last_poll_ts": 0, "ext_last_new": 0,
            "opt_last_run_ts": 0, "opt_snapped": 0, "reconciled": {}, "pruned_date": None,
            "poll_last_ts": 0, "ext_day": None, "ext_pm_polls": 0, "ext_pm_bars": 0,
            "ext_last_bars_ts": 0, "ext_pm_red_ts": 0}


_state.update(_fresh_state())


def _reset_state_for_tests():
    global _started, _stop
    with _lock:
        _stop.set()
        _stop = threading.Event()
        _started = False
        _THREADS.clear()
        _listeners.clear()
        _errors.clear()
        _state.clear()
        _state.update(_fresh_state())
        _last_seen.clear()
        _no_opts.clear()
        _daily_fetched.clear()


def _err(msg: str) -> None:
    try:
        _errors.append(f"{datetime.now(ET).strftime('%H:%M:%S')} {msg}")
        log.warning(f"[watch-tape] {msg}")
    except Exception:
        pass


class ConnectionLimitError(RuntimeError):
    """Alpaca WS answered auth with 406 'connection limit exceeded'."""


# ── small helpers ──────────────────────────────────────────────────────────────

def _f(x):
    try:
        v = float(x)
        return None if v != v else v          # NaN → None
    except Exception:
        return None


def _et_now() -> datetime:
    return datetime.fromtimestamp(_now(), ET)


def _is_trading_day(et: datetime) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(et))
    except Exception:
        return et.weekday() < 5


def _parse_ts(s):
    """RFC3339 (Z / offset / nanosecond fraction) → UTC epoch int, None if bad."""
    try:
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
        if m:
            s = m.group(1) + m.group(3)          # drop fractional seconds
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_of(ts) -> str:
    """pre < 09:30 ≤ rth < 16:00 ≤ post (ET wall clock, DST-aware)."""
    et = datetime.fromtimestamp(int(ts), ET)
    hm = et.hour * 60 + et.minute
    if hm < 9 * 60 + 30:
        return "pre"
    if hm < 16 * 60:
        return "rth"
    return "post"


def _et_midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp())


def _et_date(ts) -> date:
    return datetime.fromtimestamp(int(ts), ET).date()


def _et_at(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def _prev_trading_day(d: date) -> date:
    x = d - timedelta(days=1)
    for _ in range(10):
        if _is_trading_day(datetime(x.year, x.month, x.day, 12, tzinfo=ET)):
            return x
        x -= timedelta(days=1)
    return x


def _in_stream_window(et: datetime) -> bool:
    hm = et.hour * 60 + et.minute
    return _is_trading_day(et) and (7 * 60 + 55) <= hm < (17 * 60 + 5)


def _in_extended_window(et: datetime) -> bool:
    hm = et.hour * 60 + et.minute
    return _is_trading_day(et) and ((4 * 60 <= hm < 8 * 60) or (17 * 60 <= hm < 20 * 60))


def _in_rth(et: datetime) -> bool:
    hm = et.hour * 60 + et.minute
    return _is_trading_day(et) and (9 * 60 + 30) <= hm < 16 * 60


def _feed_for(end_ts) -> str:
    """iex when the window ends <15 min ago (SIP not yet free), else sip."""
    return "sip" if (_now() - float(end_ts)) >= _SIP_DELAY_S else "iex"


def _next_backoff(b) -> int:
    return int(min(max(1, b) * 2, _BACKOFF_CAP_S))


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── universe ───────────────────────────────────────────────────────────────────

def _refresh_universe() -> dict:
    u = {}
    try:
        got = _watchlist_universe()
        if isinstance(got, dict):
            u = {str(k).upper(): (v if isinstance(v, dict) else {}) for k, v in got.items()}
    except Exception as e:
        _err(f"universe unavailable: {e}")
    for s in (_cfg().get("infra_symbols") or []):
        u.setdefault(str(s).upper(), {"role": "infra"})
    u = {k: v for k, v in u.items() if _SYM_RE.match(k)}
    with _lock:
        _state["universe"] = u
        _state["universe_ts"] = _now()
    return u


def _symbols() -> list:
    with _lock:
        u = _state.get("universe") or {}
    if not u:
        u = _refresh_universe()
    return sorted(u)


def _option_symbols() -> list:
    with _lock:
        u = dict(_state.get("universe") or {})
    if not u:
        u = _refresh_universe()
    return sorted(t for t, meta in u.items() if (meta or {}).get("role") in ("item", "position"))


# ── listeners ──────────────────────────────────────────────────────────────────

_BAR_KEYS = ("ts", "o", "h", "l", "c", "v", "vwap", "n", "source", "session")
# "replay" is set by emission PATH, never by age alone (SIGNALS judges age itself):
# True only for catch-up paths — backfill()/reconnect gap backfill, and the older bars of a
# rest_poll / extended poll that is the first of its window or follows a stall. Live bars
# (WS, normal polls — even a 230s-old Yahoo extended bar) are always replay=False.
_LIVE_LAG_S = 180            # a catch-up poll's bars newer than interval + this count as live


def register_bar_listener(fn) -> None:
    try:
        with _lock:
            if callable(fn) and fn not in _listeners:
                _listeners.append(fn)
    except Exception:
        pass


def _emit(ticker: str, bar: dict, replay: bool = False) -> None:
    with _lock:
        fns = list(_listeners)
    b = {k: bar.get(k) for k in _BAR_KEYS}
    b["replay"] = bool(replay)
    for fn in fns:
        try:
            fn(ticker, dict(b))
        except Exception as e:
            _err(f"bar listener {getattr(fn, '__name__', fn)} failed: {e}")


def _note_bar(ticker: str, ts: int) -> None:
    with _lock:
        if ts > _last_seen.get(ticker, 0):
            _last_seen[ticker] = ts
        if ts > _state.get("last_bar_ts", 0):
            _state["last_bar_ts"] = int(ts)


# ── storage ────────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO watch_bars (ticker, tf, ts, o, h, l, c, v, vwap, n, source, session)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(ticker, tf, ts) DO UPDATE SET
  o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v,
  vwap=excluded.vwap, n=excluded.n, source=excluded.source, session=excluded.session
WHERE watch_bars.source IS NOT 'sip' OR excluded.source = 'sip'
"""


def _norm(row: dict, tf: str):
    try:
        t = str(row.get("ticker") or "").upper()
        ts = int(row["ts"])
        c = _f(row.get("c"))
        if not t or c is None:
            return None
        if tf == "5m":
            ts -= ts % 300
        elif tf == "1d":
            ts = _et_midnight(_et_date(ts))
        o, h, l = _f(row.get("o")), _f(row.get("h")), _f(row.get("l"))
        v = _f(row.get("v")) or 0.0
        vw = _f(row.get("vwap"))
        n = row.get("n")
        return {"ticker": t, "ts": ts, "o": o if o is not None else c, "h": h if h is not None else c,
                "l": l if l is not None else c, "c": c, "v": v, "vwap": vw if vw is not None else c,
                "n": int(n) if isinstance(n, (int, float)) and n == n else 0,
                "source": str(row.get("source") or "iex"),
                "session": "rth" if tf == "1d" else session_of(ts)}
    except Exception:
        return None


def _write(conn, r: dict, tf: str) -> None:
    conn.execute(_UPSERT_SQL, (r["ticker"], tf, r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"],
                               r["vwap"], r["n"], r["source"], r["session"]))


def _agg(rows: list, *, daily=False):
    """o=first, h=max, l=min, c=last, v=sum, vwap volume-weighted, n=sum.
    daily=True: the source is never 'sip' — on a 1d row 'sip' is reserved for the
    OFFICIAL Alpaca SIP daily bar (its c = the official close incl. the 16:00 auction,
    which a 1m RTH rollup cannot see), so a rollup can never overwrite it."""
    if not rows:
        return None
    v = sum((r["v"] or 0) for r in rows)
    if v > 0:
        vw = sum(((r["vwap"] if r["vwap"] is not None else r["c"]) * (r["v"] or 0)) for r in rows) / v
    else:
        vw = rows[-1]["c"]
    srcs = {r["source"] for r in rows}
    src = "sip" if srcs == {"sip"} else ("yf" if srcs == {"yf"} else "iex")
    if daily and src == "sip":
        src = "iex"
    return {"o": rows[0]["o"], "h": max(r["h"] for r in rows), "l": min(r["l"] for r in rows),
            "c": rows[-1]["c"], "v": v, "vwap": vw, "n": sum((r["n"] or 0) for r in rows), "source": src}


def _rebuild_rollups(conn, keys5: set, keysd: set) -> None:
    for t, b in sorted(keys5):
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM watch_bars WHERE ticker=? AND tf='1m' AND ts>=? AND ts<? ORDER BY ts",
            (t, b, b + 300)).fetchall()]
        a = _agg(rows)
        if a:
            _write(conn, {**a, "ticker": t, "ts": b, "session": session_of(b)}, "5m")
    today = _et_now().date()
    for t, d in sorted(keysd):
        if d != today:
            continue            # completed days hold the official daily bar, never a 1m rollup
        lo, hi = _et_midnight(d), _et_midnight(d + timedelta(days=1))
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM watch_bars WHERE ticker=? AND tf='1m' AND ts>=? AND ts<? AND session='rth' "
            "ORDER BY ts", (t, lo, hi)).fetchall()]
        a = _agg(rows, daily=True)
        if a:
            _write(conn, {**a, "ticker": t, "ts": lo, "session": "rth"}, "1d")


def upsert_bars(rows, tf="1m", *, conn=None) -> int:
    """Upsert bars (dicts with ticker + bar fields). 1m writes also rebuild the
    touched 5m buckets and the 1d (RTH-only) bar. A 'sip' row is never overwritten
    by 'iex'/'yf'. Returns the number of `tf` rows written."""
    if not enabled() or tf not in ("1m", "5m", "1d"):
        return 0
    norm = [r for r in (_norm(x, tf) for x in (rows or []) if isinstance(x, dict)) if r]
    if not norm:
        return 0
    own = conn is None
    try:
        if own:
            conn = mason_db.get_connection()
        before = conn.total_changes
        keys5, keysd = set(), set()
        for r in norm:
            _write(conn, r, tf)
            if tf == "1m":
                keys5.add((r["ticker"], r["ts"] - r["ts"] % 300))
                keysd.add((r["ticker"], _et_date(r["ts"])))
        written = conn.total_changes - before
        if tf == "1m":
            _rebuild_rollups(conn, keys5, keysd)
        conn.commit()
        return int(written)
    except Exception as e:
        _err(f"upsert_bars: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query(sql: str, args=()) -> list:
    conn = None
    try:
        conn = mason_db.get_connection()
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception as e:
        _err(f"query: {e}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def bars(ticker, tf="1m", *, since_ts=None, until_ts=None, limit=5000) -> list:
    """Ascending bars. With since_ts: the first `limit` from there; without: the
    latest `limit` (still ascending). since/until inclusive."""
    if not enabled():
        return []
    try:
        t = str(ticker).upper()
        lim = max(1, min(int(limit or 5000), 100000))
        where, args = "ticker=? AND tf=?", [t, tf]
        if since_ts is not None:
            where += " AND ts>=?"
            args.append(int(since_ts))
        if until_ts is not None:
            where += " AND ts<=?"
            args.append(int(until_ts))
        cols = "ticker, tf, ts, o, h, l, c, v, vwap, n, source, session"
        if since_ts is not None:
            return _query(f"SELECT {cols} FROM watch_bars WHERE {where} ORDER BY ts ASC LIMIT ?", (*args, lim))
        rows = _query(f"SELECT {cols} FROM watch_bars WHERE {where} ORDER BY ts DESC LIMIT ?", (*args, lim))
        return list(reversed(rows))
    except Exception as e:
        _err(f"bars: {e}")
        return []


def session_bars(ticker, date_et=None, tf="1m") -> list:
    """All bars (pre + rth + post) for one ET date (default today)."""
    if not enabled():
        return []
    try:
        d = date.fromisoformat(date_et) if date_et else _et_now().date()
        return bars(ticker, tf, since_ts=_et_midnight(d),
                    until_ts=_et_midnight(d + timedelta(days=1)) - 1, limit=100000)
    except Exception as e:
        _err(f"session_bars: {e}")
        return []


def _max_ts(ticker: str) -> int:
    with _lock:
        if ticker in _last_seen:
            return _last_seen[ticker]
    rows = _query("SELECT MAX(ts) m FROM watch_bars WHERE ticker=? AND tf='1m'", (ticker,))
    m = int(rows[0]["m"]) if rows and rows[0].get("m") is not None else 0
    with _lock:
        _last_seen[ticker] = max(m, _last_seen.get(ticker, 0))
        return _last_seen[ticker]


# ── REST bars ──────────────────────────────────────────────────────────────────

def _bar_from_alpaca(t: str, b: dict, source: str):
    ts = _parse_ts(b.get("t"))
    if ts is None:
        return None
    return {"ticker": t, "ts": ts, "o": b.get("o"), "h": b.get("h"), "l": b.get("l"), "c": b.get("c"),
            "v": b.get("v"), "vwap": b.get("vw"), "n": b.get("n"), "source": source}


def _fetch_bars(symbols, start_ts, end_ts, feed, timeframe="1Min", adjustment=None):
    """(rows, status) — status 'ok' | 'forbidden' | 'error'. Paginated, bounded."""
    rows = []
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "feed": feed,
              "start": _iso(start_ts), "end": _iso(end_ts), "limit": 10000}
    if adjustment:
        params["adjustment"] = adjustment
    for _ in range(50):
        j = _alpaca_data_get("/v2/stocks/bars", dict(params))
        if not isinstance(j, dict):
            return rows, "error"
        if j.get("_http_status"):
            _err(f"bars {feed} HTTP {j.get('_http_status')}: {j.get('message', '')[:80]}")
            return rows, ("forbidden" if j.get("_http_status") in (401, 403) else "error")
        for t, lst in (j.get("bars") or {}).items():
            for b in (lst or []):
                r = _bar_from_alpaca(str(t).upper(), b, feed)
                if r:
                    rows.append(r)
        tok = j.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    return rows, "ok"


def _existing_ts(tickers, lo, hi) -> set:
    tickers = list(tickers)
    if not tickers:
        return set()
    q = ",".join("?" * len(tickers))
    return {(r["ticker"], int(r["ts"])) for r in _query(
        f"SELECT ticker, ts FROM watch_bars WHERE tf='1m' AND ts>=? AND ts<=? AND ticker IN ({q})",
        (int(lo), int(hi), *tickers))}


def _store_and_emit(rows: list, *, emit=True, replay=False) -> int:
    """Store finished 1m bars; emit the ones not previously stored (ascending).
    replay: bool for all bars, or fn(ts) -> bool per bar."""
    now = _now()
    rows = [r for r in rows if r and int(r["ts"]) + 60 <= now]
    if not rows:
        return 0
    lo, hi = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    known = _existing_ts({r["ticker"] for r in rows}, lo, hi) if emit else set()
    n = upsert_bars(rows)
    if emit:
        for r in sorted(rows, key=lambda x: (x["ts"], x["ticker"])):
            if (r["ticker"], int(r["ts"])) in known:
                continue
            nr = _norm(r, "1m")
            if nr:
                _note_bar(nr["ticker"], nr["ts"])
                rp = replay(nr["ts"]) if callable(replay) else replay
                _emit(nr["ticker"], nr, replay=bool(rp))
    return n


def _backfill_symbols(symbols, start_ts, end_ts) -> int:
    symbols = [s for s in symbols if s]
    if not symbols or end_ts - start_ts < 60:
        return 0
    feed = _feed_for(end_ts)
    total = 0
    for chunk in _chunks(sorted(symbols), _BATCH):
        rows, st = _fetch_bars(chunk, start_ts, end_ts, feed)
        if feed == "sip" and st == "forbidden":
            rows, st = _fetch_bars(chunk, start_ts, end_ts, "iex")
        total += _store_and_emit(rows, replay=True)       # backfill/gap = catch-up
    return total


def backfill(ticker, start_ts, end_ts) -> int:
    """Alpaca REST 1Min bars for [start, end]; feed=iex if the window ends <15 min
    ago, else sip (403 → iex). New finished bars are emitted to listeners."""
    if not enabled():
        return 0
    try:
        return _backfill_symbols([str(ticker).upper()], int(start_ts), int(end_ts))
    except Exception as e:
        _err(f"backfill {ticker}: {e}")
        return 0


def reconcile_sip(date_et) -> int:
    """Re-pull one ET day's 1m bars (04:00–20:00, capped at now-15min) from SIP for the
    universe + every ticker already stored that day; overwrite and rebuild rollups.
    Corrections are not re-emitted to listeners."""
    if not enabled():
        return 0
    try:
        d = date.fromisoformat(str(date_et))
        start = _et_at(d, 4, 0)
        end = min(_et_at(d, 20, 0), int(_now()) - _SIP_DELAY_S)
        if end <= start:
            return 0
        lo, hi = _et_midnight(d), _et_midnight(d + timedelta(days=1))
        syms = set(_symbols()) | {r["ticker"] for r in _query(
            "SELECT DISTINCT ticker FROM watch_bars WHERE tf='1m' AND ts>=? AND ts<?", (lo, hi))}
        total = 0
        for chunk in _chunks(sorted(syms), _BATCH):
            rows, st = _fetch_bars(chunk, start, end, "sip")
            if rows:
                total += upsert_bars(rows)
        try:
            _fetch_official_day(syms, d)      # 1d row = official daily bar, not the 1m rollup
        except Exception as e:
            _err(f"official daily {d}: {e}")
        with _lock:
            _state["reconciled"][d.isoformat()] = {"rows": total, "at": int(_now())}
        return total
    except Exception as e:
        _err(f"reconcile_sip {date_et}: {e}")
        return 0


# ── daily history / prior close ────────────────────────────────────────────────

def _completed_daily(ticker: str, today: date) -> list:
    return _query("SELECT ticker, tf, ts, o, h, l, c, v, vwap, n, source, session FROM watch_bars "
                  "WHERE ticker=? AND tf='1d' AND ts<? ORDER BY ts ASC", (ticker, _et_midnight(today)))


def _fetch_daily(ticker: str, days: int, today: date) -> int:
    """Store OFFICIAL completed daily bars: Alpaca SIP 1Day (c = official close,
    verified vs the exchange close 2026-09-30, see K1_probe.md), yfinance daily
    Close as the fallback. Today's partial bar is never stored here."""
    start = _et_midnight(today - timedelta(days=int(days * 1.5) + 10))
    end = int(_now()) - _SIP_DELAY_S
    today_mid = _et_midnight(today)
    rows, st = _fetch_bars([ticker], start, end, "sip", timeframe="1Day", adjustment="split")
    rows = [r for r in rows if _et_midnight(_et_date(r["ts"])) < today_mid]
    if not rows:
        df = _yf_download([_yfs(ticker)], start=_et_date(start).isoformat(), interval="1d",
                          auto_adjust=False, progress=False, threads=False, group_by="ticker", timeout=10)
        rows = [r for r in _yf_rows(df, [ticker]) if _et_midnight(_et_date(r["ts"])) < today_mid]
    return upsert_bars(rows, tf="1d") if rows else 0


def _fetch_official_day(symbols, d: date) -> int:
    """Official SIP 1Day bars for one ET date (after the close), batched."""
    start = _et_midnight(d)
    end = min(_et_midnight(d + timedelta(days=1)), int(_now()) - _SIP_DELAY_S)
    if end <= start:
        return 0
    n = 0
    for chunk in _chunks(sorted(symbols), _BATCH):
        rows, st = _fetch_bars(chunk, start, end, "sip", timeframe="1Day", adjustment="split")
        rows = [r for r in rows if _et_date(r["ts"]) == d]
        if rows:
            n += upsert_bars(rows, tf="1d")
    return n


def _is_official(row) -> bool:
    return bool(row) and row.get("source") == "sip"


def daily_history(ticker, days=90) -> list:
    """Completed daily bars (ascending, today's partial excluded), up to `days`.
    Completed days hold the OFFICIAL daily bar (source 'sip'; 'yf' if Alpaca was
    unavailable). Missing/stale/unofficial history is fetched at most once per
    ticker per ET day."""
    if not enabled():
        return []
    try:
        t = str(ticker).upper()
        days = max(1, int(days or 90))
        today = _et_now().date()
        rows = _completed_daily(t, today)
        prev = _prev_trading_day(today)
        stale = (not rows) or _et_date(rows[-1]["ts"]) < prev or not _is_official(rows[-1]) \
            or len(rows) < days
        memo = _daily_fetched.get(t)
        if stale and not (memo and memo[0] == today and memo[1] >= days):
            _daily_fetched[t] = (today, days)
            if _fetch_daily(t, days, today):
                rows = _completed_daily(t, today)
        return rows[-days:]
    except Exception as e:
        _err(f"daily_history {ticker}: {e}")
        return []


def prior_close(ticker):
    """OFFICIAL close of the last completed trading day (the SIP daily bar, which
    includes the 16:00 closing auction — never the 15:59 1m close). Fetched when the
    stored row is missing, stale or only a provisional 1m rollup."""
    if not enabled():
        return None
    try:
        t = str(ticker).upper()
        today = _et_now().date()
        rows = _completed_daily(t, today)
        if rows and _is_official(rows[-1]) and _et_date(rows[-1]["ts"]) >= _prev_trading_day(today):
            return _f(rows[-1]["c"])
        h = daily_history(t, days=10)
        if h:
            return _f(h[-1]["c"])
        return _f(rows[-1]["c"]) if rows else None
    except Exception as e:
        _err(f"prior_close {ticker}: {e}")
        return None


# ── IEX WebSocket stream ───────────────────────────────────────────────────────

def _sub_diff(current, desired):
    cur, want = set(current or ()), set(desired or ())
    return sorted(want - cur), sorted(cur - want)


def _handle_ws_messages(msgs) -> int:
    """Handle one decoded WS frame (list of messages). 'b' bars → upsert + emit;
    'u' updatedBars → upsert only; 'error' → recorded. Returns #bars emitted."""
    if not isinstance(msgs, list):
        msgs = [msgs]
    live, corr = [], []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        typ = m.get("T")
        if typ in ("b", "u"):
            r = _bar_from_alpaca(str(m.get("S") or "").upper(), m, "iex")
            if r and r["ticker"]:
                (live if typ == "b" else corr).append(r)
        elif typ == "error":
            _err(f"ws error {m.get('code')}: {m.get('msg')}")
    if corr:
        upsert_bars(corr)
    with _lock:      # skip minutes a reconnect gap-backfill already emitted
        live = [r for r in live if r["ts"] > _last_seen.get(r["ticker"], 0)]
    if not live:
        return 0
    upsert_bars(live)
    n = 0
    for r in live:
        nr = _norm(r, "1m")
        if nr:
            _note_bar(nr["ticker"], nr["ts"])
            _emit(nr["ticker"], nr)
            n += 1
    return n


def _effective_mode() -> str:
    with _lock:
        if _state.get("degraded_date") == _et_now().date().isoformat():
            return "rest_poll"
    m = str(_sub("stream").get("mode") or "ws")
    return m if m in ("ws", "rest_poll") else "ws"


def _note_conn_limit() -> None:
    with _lock:
        _state["limit_hits"] = _state.get("limit_hits", 0) + 1
        if _state["limit_hits"] >= _LIMIT_DEGRADE_AFTER:
            _state["degraded_date"] = _et_now().date().isoformat()
            _err("ws: repeated 406 connection limit → rest_poll for the rest of the day")


def _stream_should_run() -> bool:
    return enabled() and bool(_sub("stream").get("enabled")) and _in_stream_window(_et_now())


def _gap_backfill(symbols) -> int:
    """On (re)connect: backfill [last_bar+60 | now-backfill_minutes, now], never
    before 04:00 ET today."""
    now = int(_now())
    today = _et_now().date()
    lookback = int(_sub("stream").get("backfill_minutes") or 120) * 60
    floor = max(now - lookback, _et_at(today, 4, 0))
    with _lock:
        last = int(_state.get("last_bar_ts") or 0)
    start = max(last + 60, floor) if last else floor
    return _backfill_symbols(symbols, start, now) if now - start >= 60 else 0


async def _ws_session(key, secret, stop=None):
    """One connect→auth→subscribe→(gap backfill)→consume cycle. Returns normally
    when the window closes / mode changes / stop is set; raises on disconnect."""
    import asyncio
    try:
        async with _ws_connect(_IEX_WS) as ws:
            await ws.recv()                                     # [{"T":"success","msg":"connected"}]
            await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            auth = json.loads(await ws.recv())
            auth = auth if isinstance(auth, list) else [auth]
            if any(m.get("T") == "error" and int(m.get("code") or 0) == 406 for m in auth):
                _err("ws auth 406: connection limit exceeded")
                raise ConnectionLimitError("406 connection limit exceeded")
            if not any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in auth):
                raise RuntimeError(f"auth failed: {auth}")
            subscribed = set(_symbols())
            if subscribed:
                await ws.send(json.dumps({"action": "subscribe", "bars": sorted(subscribed)}))
            with _lock:
                _state.update(connected=True, subscribed=set(subscribed), last_connect_ts=int(_now()),
                              limit_hits=0)
            log.info(f"[watch-tape] IEX stream connected, {len(subscribed)} symbols")
            try:
                _gap_backfill(sorted(subscribed))
            except Exception as e:
                _err(f"gap backfill: {e}")
            last_check = _now()
            while True:
                if (stop is not None and stop.is_set()) or not _stream_should_run() \
                        or _effective_mode() != "ws":
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    raw = None
                if raw:
                    try:
                        _handle_ws_messages(json.loads(raw))
                    except Exception as e:
                        _err(f"ws message: {e}")
                if _now() - last_check >= _SUB_CHECK_S:
                    last_check = _now()
                    add, rem = _sub_diff(subscribed, _symbols())
                    if add:
                        await ws.send(json.dumps({"action": "subscribe", "bars": add}))
                    if rem:
                        await ws.send(json.dumps({"action": "unsubscribe", "bars": rem}))
                    if add or rem:
                        subscribed = (subscribed | set(add)) - set(rem)
                        with _lock:
                            _state["subscribed"] = set(subscribed)
    finally:
        with _lock:
            _state["connected"] = False


def _rest_poll_once() -> int:
    """rest_poll mode (fallback for WS 406 / config). Each poll fetches EVERY finished
    1m IEX bar since the last one seen per symbol via /v2/stocks/bars (batched,
    paginated) — not /bars/latest, whose single bar is usually the in-progress minute
    at poll time. Each finished minute is emitted exactly once; the in-progress minute
    never. Same listener contract as the WS."""
    if not enabled():
        return 0
    try:
        now = int(_now())
        syms = _symbols()
        if not syms:
            return 0
        with _lock:
            last_poll = int(_state.get("poll_last_ts") or 0)
        # normal cadence: look back 10 min; first poll / after a stall: backfill_minutes
        catch_up = not (last_poll and now - last_poll <= 600)
        lookback = 600 if not catch_up else int(_sub("stream").get("backfill_minutes") or 120) * 60
        floor = max(now - lookback, _et_at(_et_now().date(), 4, 0))
        start = max(min(_max_ts(s) + 60 for s in syms), floor)
        fresh, ok = [], False
        for chunk in _chunks(syms, _BATCH):
            rows, st = _fetch_bars(chunk, start, now, "iex")
            ok = ok or st == "ok"
            fresh.extend(r for r in rows if r["ts"] + 60 <= now and r["ts"] > _max_ts(r["ticker"]))
        with _lock:
            _state["connected"] = ok
            _state["subscribed"] = set(syms)
            _state["poll_last_ts"] = now
        if catch_up:     # first poll / after a stall: only the newest minutes are live
            cut = now - 60 - _LIVE_LAG_S
            rp = (lambda ts: ts < cut)
        else:
            rp = False
        return _store_and_emit(fresh, replay=rp) and len(fresh)
    except Exception as e:
        _err(f"rest_poll: {e}")
        return 0


# ── extended hours (yfinance) ──────────────────────────────────────────────────

def _yf_rows(df, syms) -> list:
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    cols = df.columns
    multi = getattr(cols, "nlevels", 1) > 1
    for s in syms:
        ys = _yfs(s)
        try:
            if multi:
                if ys in cols.get_level_values(0):
                    sub = df[ys]
                elif ys in cols.get_level_values(1):
                    sub = df.xs(ys, axis=1, level=1)
                else:
                    continue
            elif len(syms) == 1:
                sub = df
            else:
                continue
            for idx, r in sub.iterrows():
                c = _f(r.get("Close"))
                if c is None:
                    continue
                if getattr(idx, "tzinfo", None) is None:
                    idx = idx.tz_localize(ET)           # yf daily index: naive exchange dates
                ts = int(idx.timestamp())
                out.append({"ticker": s, "ts": ts, "o": _f(r.get("Open")), "h": _f(r.get("High")),
                            "l": _f(r.get("Low")), "c": c, "v": _f(r.get("Volume")) or 0.0,
                            "vwap": None, "n": 0, "source": "yf"})
        except Exception as e:
            _err(f"yf parse {s}: {e}")
    return out


def _ext_live_window_s() -> int:
    """Age up to which an extended-hours bar still counts as live on a catch-up poll:
    one poll interval + Yahoo/processing lag."""
    return max(30, int(_sub("extended").get("poll_seconds") or 120)) + _LIVE_LAG_S


def _extended_poll_once() -> int:
    """04:00–08:00 / 17:00–20:00 ET on trading days: yfinance 1m prepost bars;
    stores + emits only finished bars newer than the last stored bar per ticker."""
    try:
        if not enabled() or not _sub("extended").get("enabled"):
            return 0
        et = _et_now()
        if not _in_extended_window(et):
            return 0
        syms = _symbols()
        if not syms:
            return 0
        today_lo = _et_midnight(et.date())
        now = _now()
        fresh = []
        for chunk in _chunks(syms, 50):
            with _yf_quiet():
                df = _yf_download([_yfs(s) for s in chunk], period="1d", interval="1m",
                                  prepost=True, progress=False, threads=False, group_by="ticker",
                                  auto_adjust=False, timeout=10)
            for r in _yf_rows(df, chunk):
                if r["ts"] >= today_lo and r["ts"] + 60 <= now and r["ts"] > _max_ts(r["ticker"]):
                    fresh.append(r)
        with _lock:
            last = int(_state.get("ext_last_poll_ts") or 0)
        live_s = _ext_live_window_s()
        catch_up = (not last) or (now - last > live_s)   # first poll of a window / after a stall
        rp = (lambda ts: ts < now - live_s) if catch_up else False
        n = _store_and_emit(fresh, replay=rp) and len(fresh)
        with _lock:
            _state["ext_last_poll_ts"] = int(now)
            _state["ext_last_new"] = n
            if n:
                _state["ext_last_bars_ts"] = int(now)    # premarket_health: a red verdict clears only on bars
            if et.hour < 12:                     # the pre-market window: per-day tally for premarket_health
                d = et.date().isoformat()
                if _state.get("ext_day") != d:
                    _state.update(ext_day=d, ext_pm_polls=0, ext_pm_bars=0)
                _state["ext_pm_polls"] = int(_state.get("ext_pm_polls") or 0) + 1
                _state["ext_pm_bars"] = int(_state.get("ext_pm_bars") or 0) + int(n or 0)
        return n
    except Exception as e:
        _err(f"extended poll: {e}")
        return 0


# ── options snapper ────────────────────────────────────────────────────────────

def _occ_parse(sym: str):
    """HOOD261009C00113000 → (expiry 'YYYY-MM-DD', 'C'|'P', strike float)."""
    try:
        tail = sym[-15:]
        exp = f"20{tail[0:2]}-{tail[2:4]}-{tail[4:6]}"
        cp = tail[6]
        strike = int(tail[7:]) / 1000.0
        if cp not in ("C", "P"):
            return None
        return exp, cp, strike
    except Exception:
        return None


def _mid(bid, ask):
    bid, ask = _f(bid), _f(ask)
    if ask is None or ask <= 0 or bid is None or bid < 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _underlying(ticker: str):
    now = int(_now())
    rows = _query("SELECT c FROM watch_bars WHERE ticker=? AND tf='1m' AND ts>=? ORDER BY ts DESC LIMIT 1",
                  (ticker, now - 3600))
    if rows:
        return _f(rows[0]["c"])
    j = _alpaca_data_get("/v2/stocks/bars/latest", {"symbols": ticker, "feed": "iex"})
    try:
        return _f(((j or {}).get("bars") or {}).get(ticker, {}).get("c"))
    except Exception:
        return None


def _alpaca_contracts(ticker: str, params: dict):
    """list of normalized contracts, [] if the server says none, None on failure."""
    out = []
    p = dict(params)
    for _ in range(5):
        j = _alpaca_data_get(f"/v1beta1/options/snapshots/{ticker}", dict(p))
        if not isinstance(j, dict):
            return None
        if j.get("_http_status"):
            if j.get("_http_status") in (404, 422):
                return []
            return None
        for sym, s in (j.get("snapshots") or {}).items():
            parsed = _occ_parse(sym)
            if not parsed or not isinstance(s, dict):
                continue
            q = s.get("latestQuote") or {}
            out.append({"expiry": parsed[0], "type": parsed[1], "strike": parsed[2],
                        "bid": q.get("bp"), "ask": q.get("ap"), "iv": s.get("impliedVolatility")})
        tok = j.get("next_page_token")
        if not tok:
            break
        p["page_token"] = tok
    return out


def _pick_atm(ticker, contracts, und, today: date, min_dte: int, source: str):
    if not contracts or not und:
        return None
    by_exp = {}
    for c in contracts:
        try:
            dte = (date.fromisoformat(c["expiry"]) - today).days
        except Exception:
            continue
        if dte >= min_dte:
            by_exp.setdefault(c["expiry"], {}).setdefault(float(c["strike"]), {})[c["type"]] = c
    for exp in sorted(by_exp):
        strikes = {k: v for k, v in by_exp[exp].items() if "C" in v and "P" in v}
        if not strikes:
            continue
        k = min(strikes, key=lambda s: (abs(s - und), s))
        call, put = strikes[k]["C"], strikes[k]["P"]
        cm, pm = _mid(call.get("bid"), call.get("ask")), _mid(put.get("bid"), put.get("ask"))
        straddle = (cm + pm) if (cm is not None and pm is not None) else None
        ivs = [x for x in (_f(call.get("iv")), _f(put.get("iv"))) if x is not None and x > 0]
        return {"ticker": ticker, "ts": int(_now()) - int(_now()) % 60, "expiry": exp,
                "dte": (date.fromisoformat(exp) - today).days, "atm_strike": k,
                "atm_iv": (sum(ivs) / len(ivs)) if ivs else None,
                "straddle_mid": straddle,
                "implied_move_pct": (straddle / und * 100.0) if straddle is not None else None,
                "underlying": und, "source": source}
    return None


def _store_snap(s: dict) -> None:
    conn = None
    try:
        conn = mason_db.get_connection()
        conn.execute("INSERT OR REPLACE INTO watch_option_snaps (ticker, ts, expiry, dte, atm_strike, atm_iv, "
                     "straddle_mid, implied_move_pct, underlying, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (s["ticker"], s["ts"], s["expiry"], s["dte"], s["atm_strike"], s["atm_iv"],
                      s["straddle_mid"], s["implied_move_pct"], s["underlying"], s["source"]))
        conn.commit()
    except Exception as e:
        _err(f"store option snap: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _snap_options(ticker: str):
    """One ATM snapshot for `ticker` (stored), or None. No-options → negative cache 1 day."""
    try:
        t = str(ticker).upper()
        now = _now()
        with _lock:
            if _no_opts.get(t, 0) > now:
                return None
        min_dte = int(_sub("options").get("min_dte") or 7)
        today = _et_now().date()
        gte = today + timedelta(days=min_dte)
        und = _underlying(t)
        contracts, definite_none = None, False
        if und:
            contracts = _alpaca_contracts(t, {
                "feed": "indicative", "limit": 1000,
                "expiration_date_gte": gte.isoformat(),
                "expiration_date_lte": (gte + timedelta(days=45)).isoformat(),
                "strike_price_gte": round(und * 0.8, 2), "strike_price_lte": round(und * 1.2, 2)})
            if contracts == []:
                anyc = _alpaca_contracts(t, {"feed": "indicative", "limit": 10})
                definite_none = anyc == []
                if not definite_none and anyc is not None:
                    return None             # listed, just nothing in the window right now
        source = "indicative"
        if not contracts:
            yund, ycs = _yf_option_chain(t, gte.isoformat())
            if ycs:
                contracts, source = ycs, "yf"
                und = und or _f(yund)
            else:
                if definite_none or ycs == []:
                    with _lock:
                        _no_opts[t] = now + _NEG_CACHE_S
                return None
        s = _pick_atm(t, contracts, und, today, min_dte, source)
        if s:
            _store_snap(s)
        return s
    except Exception as e:
        _err(f"options {ticker}: {e}")
        return None


def latest_option_snap(ticker):
    if not enabled():
        return None
    rows = _query("SELECT * FROM watch_option_snaps WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                  (str(ticker).upper(),))
    return rows[0] if rows else None


def option_snaps(ticker, since_ts) -> list:
    if not enabled():
        return []
    try:
        return _query("SELECT * FROM watch_option_snaps WHERE ticker=? AND ts>=? ORDER BY ts ASC",
                      (str(ticker).upper(), int(since_ts or 0)))
    except Exception as e:
        _err(f"option_snaps: {e}")
        return []


# ── retention ──────────────────────────────────────────────────────────────────

def prune() -> dict:
    """Delete 1m bars older than retention.bars_1m_days and 5m older than
    bars_5m_days. 1d bars and option snaps are kept forever."""
    if not enabled():
        return {}
    conn = None
    try:
        ret = _sub("retention")
        now = int(_now())
        conn = mason_db.get_connection()
        out = {}
        for tf, days in (("1m", ret.get("bars_1m_days") or 30), ("5m", ret.get("bars_5m_days") or 365)):
            cur = conn.execute("DELETE FROM watch_bars WHERE tf=? AND ts<?", (tf, now - int(days) * 86400))
            out[tf] = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()
        return out
    except Exception as e:
        _err(f"prune: {e}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── status ─────────────────────────────────────────────────────────────────────

def _ext_no_data() -> int:
    """Pre-market "no data yet" answers counted today (not logged; see _yf_quiet)."""
    try:
        import mason_logsetup
        c = mason_logsetup.counts()
        if c.get("day") != mason_logsetup._today():    # nothing counted yet today (yesterday's total)
            return 0
        return int(c.get("expected_empty") or 0)
    except Exception:
        return 0


def premarket_health() -> dict:
    """Health probe (mason_health 'watch_tape_premarket'; HYGIENE_REVIEW F3): not ok when, on a
    trading day after watchlist.extended.empty_alert_after_et (ET), at least
    empty_alert_min_polls pre-market polls ran and NOT ONE brought a minute bar for any symbol.
    Before that time, on other days, or with the poller off it is ok (an early quiet stretch is
    normal). A not-ok verdict stays not ok — through the evening, the night, a weekend and the next
    morning's not-yet-judged hours — until an extended poll brings bars again, so the health monitor's
    "recovered" note means bars really came back (review fix 2026-11-16). No network; never raises."""
    try:
        ext = _sub("extended")
        if not enabled() or not ext.get("enabled"):
            with _lock:
                _state["ext_pm_red_ts"] = 0
            return {"ok": True, "detail": "pre-market poller off: nothing to check"}
        after = str(ext.get("empty_alert_after_et") or "").strip()
        if not after:
            with _lock:
                _state["ext_pm_red_ts"] = 0
            return {"ok": True, "detail": "check off (watchlist.extended.empty_alert_after_et is blank)"}
        hh, mm = (int(x) for x in after.split(":")[:2])
        et = _et_now()
        with _lock:
            same_day = _state.get("ext_day") == et.date().isoformat()
            polls = int(_state.get("ext_pm_polls") or 0) if same_day else 0
            nbars = int(_state.get("ext_pm_bars") or 0) if same_day else 0
            red_ts = int(_state.get("ext_pm_red_ts") or 0)
            if red_ts and int(_state.get("ext_last_bars_ts") or 0) > red_ts:
                red_ts = _state["ext_pm_red_ts"] = 0         # bars came back after the red verdict
        if red_ts:
            # still no bars since the red verdict: stay not ok (no false "recovered" note)
            when = datetime.fromtimestamp(red_ts, ET).strftime("%a %d %b %H:%M ET")
            return {"ok": False,
                    "detail": f"pre-market data missing since {when}: no Yahoo poll has brought a bar since "
                              f"(today: {nbars} new bars in {polls} polls). Check Yahoo / the network."}
        if not _is_trading_day(et):
            return {"ok": True, "detail": "not a trading day"}
        so_far = f"pre-market today: {nbars} new bars in {polls} polls"
        if et.hour * 60 + et.minute < hh * 60 + mm:
            return {"ok": True, "detail": f"{so_far} (judged from {after} ET)"}
        if polls >= int(ext.get("empty_alert_min_polls") or 5) and nbars == 0:
            with _lock:
                _state["ext_pm_red_ts"] = int(_now())
            return {"ok": False,
                    "detail": f"pre-market data missing: {polls} Yahoo polls this morning came back empty for "
                              f"all {len(_symbols())} symbols ({_ext_no_data()} 'no data yet' answers). Normal "
                              f"mornings have bars from 04:00 ET; check Yahoo / the network."}
        return {"ok": True, "detail": so_far}
    except Exception as e:
        return {"ok": True, "detail": f"check skipped: {e}"}


def status() -> dict:
    try:
        with _lock:
            st = dict(_state)
            errs = list(_errors)
            no_opts = sorted(t for t, until in _no_opts.items() if until > _now())
        try:
            et = _et_now()
            ext_active = bool(enabled() and _sub("extended").get("enabled") and _in_extended_window(et))
            mode = _effective_mode()
        except Exception:
            ext_active, mode = False, "ws"
        return {
            "enabled": enabled(),
            "stream": {"connected": bool(st.get("connected")), "last_bar_ts": int(st.get("last_bar_ts") or 0),
                       "symbols": len(st.get("subscribed") or ()), "mode": mode,
                       "degraded": st.get("degraded_date") is not None,
                       "last_connect_ts": int(st.get("last_connect_ts") or 0),
                       "universe": len(st.get("universe") or {})},
            "extended": {"active": ext_active, "last_poll_ts": int(st.get("ext_last_poll_ts") or 0),
                         "last_new_bars": int(st.get("ext_last_new") or 0),
                         "no_data_today": _ext_no_data()},
            "options": {"last_run_ts": int(st.get("opt_last_run_ts") or 0),
                        "snapped": int(st.get("opt_snapped") or 0), "no_options": no_opts},
            "reconcile": dict(st.get("reconciled") or {}),
            "errors": errs[-5:],
        }
    except Exception:
        return {"enabled": False, "stream": {"connected": False, "last_bar_ts": 0, "symbols": 0, "mode": "ws"},
                "extended": {}, "options": {}, "errors": []}


# ── daemon threads ─────────────────────────────────────────────────────────────

def _universe_tick(stop) -> float:
    _refresh_universe()
    return 60


def _stream_tick(stop) -> float:
    import asyncio
    if not _stream_should_run():
        with _lock:
            _state["connected"] = False
        return 30
    if _effective_mode() == "rest_poll":
        _rest_poll_once()
        return max(5.0, 63 - (_now() % 60))          # just after the next minute closes
    key, secret = _creds()
    if not key or not secret:
        _err("no Alpaca creds — stream disabled")
        return 300
    with _lock:
        b = int(_state.get("backoff") or 1)
    t0 = _now()
    try:
        asyncio.run(_ws_session(key, secret, stop))
        with _lock:
            _state["backoff"] = 1
        return 1
    except ConnectionLimitError:
        _note_conn_limit()
        b = max(b, 60)
    except Exception as e:
        if _now() - t0 >= _HEALTHY_S:
            b = 1                   # it was a healthy session: start the backoff over
        _err(f"ws stream: {e}; reconnect in {b}s")
    with _lock:
        _state["backoff"] = _next_backoff(b)
    return b


def _extended_tick(stop) -> float:
    if not (enabled() and _sub("extended").get("enabled") and _in_extended_window(_et_now())):
        return 60
    _extended_poll_once()
    return max(30, int(_sub("extended").get("poll_seconds") or 120))


def _options_tick(stop) -> float:
    if not (enabled() and _sub("options").get("enabled") and _in_rth(_et_now())):
        return 60
    every = max(1, int(_sub("options").get("every_minutes") or 15)) * 60
    with _lock:
        last = _state.get("opt_last_run_ts") or 0
    if _now() - last < every:
        return 30
    with _lock:
        _state["opt_last_run_ts"] = int(_now())
    n = 0
    for t in _option_symbols()[:60]:
        if stop.is_set():
            break
        if _snap_options(t):
            n += 1
        stop.wait(0.3)
    with _lock:
        _state["opt_snapped"] = n
    return 30


def _maint_tick(stop) -> float:
    """SIP reconcile at sip_reconcile.at_et (and a 20:20 ET pass for post-market),
    then retention prune once per ET day."""
    if not enabled():
        return 60
    et = _et_now()
    today = et.date().isoformat()
    hm = et.hour * 60 + et.minute
    if _sub("sip_reconcile").get("enabled") and _is_trading_day(et):
        at = str(_sub("sip_reconcile").get("at_et") or "16:20")
        try:
            hh, _, mm = at.partition(":")
            passes = [int(hh) * 60 + int(mm), 20 * 60 + 20]
        except Exception:
            passes = [16 * 60 + 20, 20 * 60 + 20]
        with _lock:
            done = int(((_state.get("reconciled") or {}).get(today) or {}).get("passes", 0))
        due = sum(1 for p in passes if hm >= p)
        if due > done:
            reconcile_sip(today)
            with _lock:
                _state["reconciled"].setdefault(today, {})["passes"] = due
    if hm >= 16 * 60 + 30 and _state.get("pruned_date") != today:
        prune()
        with _lock:
            _state["pruned_date"] = today
    return 60


def _rt_beat() -> None:
    """Heartbeat for the runtime supervisor (mason_runtime), under this thread's name. Never raises."""
    try:
        import mason_runtime
        mason_runtime.beat(threading.current_thread().name)
    except Exception:
        pass


def _guard_loop(tick, stop) -> None:
    while not stop.is_set():
        _rt_beat()
        try:
            secs = tick(stop)
        except Exception as e:
            _err(f"{getattr(tick, '__name__', 'tick')}: {e}")
            secs = 60
        try:
            secs = float(secs)
        except Exception:
            secs = 60
        stop.wait(min(max(secs, 0.5), 600))


def start() -> None:
    """Idempotently start the daemon threads (universe refresher, stream, extended
    poller, SIP reconcile + prune, options snapper). Each idles while its flag is off."""
    global _started
    try:
        if not enabled():
            return None
        with _lock:
            if _started:
                return None
            _started = True
            stop = _stop
            for name, tick in (("watch-universe", _universe_tick), ("watch-stream", _stream_tick),
                               ("watch-extended", _extended_tick), ("watch-maint", _maint_tick),
                               ("watch-options", _options_tick)):
                th = threading.Thread(target=_guard_loop, args=(tick, stop), daemon=True, name=name)
                th.start()
                _THREADS.append(th)
        log.info("[watch-tape] started")
    except Exception as e:
        _err(f"start: {e}")
    return None
