"""
mason_live_price.py — the newest price for the symbols on screen, for the chart's live price line (read-only).

    GET /api/live_prices?symbols=SPY,NVDA,AAPL          one request for every chart on screen

Why a new route: nothing else gave a fresh price for several arbitrary symbols at once. /api/quote answers with a
sentence for one ticker, /api/ticker_card with a whole card for one symbol (two Robinhood calls and a yfinance child
each), /api/following/quotes only with the followed list. The chart bars (/api/bars) are Alpaca IEX bars, about 15
minutes behind.

Switch (OFF unless set; read on every request, no restart needed to flip it):

    "ui": {"live_price_overlay": {"enabled": true, "poll_s": 5}}        in mason.config.json

With the switch off the route answers {"enabled": false} and asks no source anything, and the page does not load
the overlay at all (the template gets `live_price_ui` = {"enabled": false, ...} from the context processor below).

Answer (switch on)
------------------
200 {"enabled": true, "ok": true, "as_of": "<UTC ISO>", "poll_s": 5,
     "market": {"state": "pre" | "regular" | "after" | "closed", "open": true|false},     open = not "closed"
     "quotes": {"SPY": {"price": 762.36,            the newest trade Mason's sources know (regular or extended hours)
                        "ts": "<UTC ISO>",          when that trade printed
                        "price_age_s": 2.4,         now - ts, on the server's clock
                        "prev_close": 762.63|null,  the last official close BEFORE that trade (null when not known)
                        "session": "regular" | "extended",
                        "source": "robinhood" | "alpaca_iex",
                        "answer_age_s": 0.8},       how long ago the source gave this answer (cache age)
                "ZZZZ": null},                      nothing usable; the reason is in "problems"
     "problems": {"ZZZZ": "<plain sentence>"},
     "sources": {"robinhood": "ok" | "off" | "paused" | "busy" | "error" | "not asked",
                 "alpaca_iex": "ok" | "error" | "paused" | "not asked"}}
200 {"enabled": false}                               switch off
400 {"enabled": true, "ok": false, "error": "<plain sentence>", "code": "bad_request"}     no usable symbol at all

Sources (first that answers wins, per symbol)
---------------------------------------------
  1. Robinhood get_equity_quotes — ONE call for all the symbols (real-time consolidated last trade, incl. extended
     hours). It goes through mason_ticker_card._rh_read, i.e. mason_robinhood's "live" lane plus the ticker card's own
     fences (at most 20 calls per 10 s, a pause after a connection failure). mason_quotes is NOT used: its Robinhood
     pause is shared with the trading loop, so a failed screen lookup there would pause Robinhood quotes for trading.
  2. Alpaca IEX snapshots — ONE call for the symbols Robinhood did not answer (last IEX trade + daily bars).
A symbol Robinhood does not know makes Robinhood refuse the whole batch; the batch is then asked symbol by symbol
once (at most RH_SPLIT_MAX), and the unknown ones are remembered for an hour and go straight to Alpaca.

Cost and safety
---------------
  * Per-symbol memory cache: TTL_OPEN_S while the market (incl. extended hours) is open, TTL_CLOSED_S when closed.
  * One fetch at a time for the whole process, and never two within MIN_GAP_S: more tabs or more charts do not mean
    more upstream calls. A fetch also refreshes the symbols other requests asked for in the last WANTED_S, so two
    tabs with different charts share one call. In practice 60 / poll_s Robinhood calls a minute (12 at the default
    5 s); closed: 1 a minute.
  * HARD CEILINGS for the whole process, whatever the pages or a script ask (review 2026-11-19): at most
    RH_MAX_PER_MIN Robinhood calls in any 60 s (split calls included), at most ONE Robinhood call of this module
    running at a time (a slow call is not stacked on), none at all while mason_robinhood's lanes are halved after a
    429 (that lane is shared with the stop checks), and at most ALPACA_MAX_PER_MIN Alpaca calls in any 60 s. Over a
    ceiling the cached answer is served as it is (its age grows, the page greys it).
  * A request waits at most WAIT_S for a fetch; the fetch finishes in its own daemon thread and fills the cache.
  * After a failed refresh the last good answer stays (its answer_age_s grows, so the page can grey it), up to
    KEEP_S; then the symbol is null.
  * Read-only: no database, journal, state-file or config writes, no model calls, no orders; the only Robinhood tool
    is get_equity_quotes (on mason_robinhood's read allowlist).

  * A symbol no source could price is not asked for again for RETRY_S, so a mistyped ticker on one chart, or both
    sources being down, cannot turn every poll into upstream calls.
  * Every in-memory table is bounded (cache, recently-asked list, unknown-symbol memory, problems).

Never imports mason_app. Tests replace the hooks _now, _config, _rh_read, _alpaca_get, _trading_day, _close_hm,
_rh_lane_strained and call reset().
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

log = logging.getLogger("mason.live_price")
BASE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

bp = Blueprint("live_price", __name__)

# ── knobs ───────────────────────────────────────────────────────────────────────
POLL_DEFAULT_S = 5
POLL_MIN_S = 2
POLL_MAX_S = 60
TTL_OPEN_S = 3.0              # a cached price is reused this long while the market is open ...
TTL_CLOSED_S = 60.0           # ... and this long while it is closed (it does not move then)
MIN_GAP_S = 2.0               # never two upstream fetches closer together than this
WAIT_S = 3.0                  # longest one request waits for a fetch
RH_WAIT_S = 2.5               # longest a fetch waits for Robinhood before it moves on to Alpaca
RETRY_S = 30.0                # a symbol whose refresh just failed is not asked for again for this long
WANTED_S = 20.0               # symbols asked for this recently ride along on the next fetch
KEEP_S = 900.0                # after failed refreshes the last good answer is still served this long
MAX_SYMBOLS = 24              # per request (extra ones are reported in "problems")
MAX_BATCH = 40                # per upstream call
CACHE_MAX = 300
RH_SPLIT_MAX = 8              # a refused batch is retried one by one only when it has at most this many symbols
RH_UNKNOWN_S = 3600.0         # how long "Robinhood does not know this symbol" is remembered
RH_UNKNOWN_MAX = 500          # ... for at most this many symbols (expired ones are dropped first)
RH_MAX_PER_MIN = 20           # hard ceiling: Robinhood calls from this module in any 60 s (split calls included)
ALPACA_MAX_PER_MIN = 12       # hard ceiling: Alpaca calls from this module in any 60 s
WANTED_MAX = 100              # the recently-asked list never holds more than this many symbols
RAW_MAX_CHARS = 1024          # longest `symbols` value read (24 symbols need under 200 characters)
PROBLEMS_MAX = MAX_SYMBOLS    # most problem lines in one answer
ALPACA_PAUSE_S = 15.0         # after an Alpaca failure, wait this long before asking it again
ALPACA_TIMEOUT_S = 4.0
ALPACA_DATA = "https://data.alpaca.markets"
ALPACA_PREV_MAX_MOVE = 0.4    # an Alpaca (IEX) price more than 40% from its previous daily bar gets no previous close:
                              # on a split day that bar may be the old share price; a wrong colour is worse than none
CLOSING_TRADE_S = 300         # a regular-session trade this close to the bell may stand in for the official close
CLOCK_SLACK_S = 1.0           # a cached answer up to this far "in the future" (small clock adjustments) is still used

_SYM_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")

MSG_NONE = "No usable symbol was given. A symbol is 1 to 5 letters, with an optional share class like BRK.B."
MSG_BAD = "not a stock symbol"
MSG_TOO_MANY = "too many symbols in one request"
MSG_NO_SOURCE = "neither Robinhood nor Alpaca gave a price just now"
MSG_WAIT = "still waiting for the first answer"


# ── hooks (tests replace these) ─────────────────────────────────────────────────

def _now() -> float:
    return time.time()


_cfg_cache = {"mtime": None, "data": {}}


def _config() -> dict:
    """mason.config.json, re-read only when the file changed. Never raises; a missing or broken file reads {}."""
    p = BASE / "mason.config.json"
    try:
        m = p.stat().st_mtime_ns
        if _cfg_cache["mtime"] != m:
            d = json.loads(p.read_text(encoding="utf-8"))
            _cfg_cache.update(mtime=m, data=d if isinstance(d, dict) else {})
        return _cfg_cache["data"]
    except Exception:
        return {}


def _rh_read(tool: str, args: dict) -> tuple:
    """(status, data, why) — the ticker card's fenced Robinhood read: ok | empty | error | off."""
    import mason_ticker_card
    return mason_ticker_card._rh_read(tool, args)


def _rh_lane_strained() -> bool:
    """True while mason_robinhood has halved its rate lanes after a 429. The "live" lane is the one the stop checks
    (mason_quotes) use too, so this screen-only feature then leaves Robinhood alone. Read-only look; False if unknown
    (the other ceilings still hold)."""
    try:
        import mason_robinhood as RH
        return RH._clock() < float(RH._lim.get("halved_until") or 0.0)
    except Exception:
        return False


def _alpaca_get(path: str, params: dict):
    """Alpaca market-data GET -> (status code | None, json | text)."""
    try:
        import requests
        import mason_execution
        r = requests.get(ALPACA_DATA + path, headers=mason_execution._headers(), params=params,
                         timeout=ALPACA_TIMEOUT_S)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None
    except Exception as e:
        return None, type(e).__name__


def _trading_day(d) -> bool:
    try:
        import mason_overnight
        return bool(mason_overnight.is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def _close_hm(d) -> tuple:
    """(hour, minute) of that day's regular close, Eastern: 16:00, or 13:00 on a half day."""
    try:
        import mason_overnight
        h, m = mason_overnight.close_hm(d)
        return int(h), int(m)
    except Exception:
        return 16, 0


# ── the switch ──────────────────────────────────────────────────────────────────

def settings() -> dict:
    """{"enabled": bool, "poll_s": int}. enabled only when ui.live_price_overlay.enabled is literally true."""
    try:
        node = ((_config().get("ui") or {}).get("live_price_overlay")) or {}
        if not isinstance(node, dict):
            node = {}
    except Exception:
        node = {}
    try:
        poll = int(float(node.get("poll_s", POLL_DEFAULT_S)))
    except (TypeError, ValueError, OverflowError):                 # OverflowError: "poll_s": 1e999 (infinity)
        poll = POLL_DEFAULT_S
    return {"enabled": node.get("enabled") is True, "poll_s": max(POLL_MIN_S, min(POLL_MAX_S, poll))}


@bp.app_context_processor
def _live_price_ctx():
    """templates/mason_v2.html reads `live_price_ui` (same way it reads rhx_ui_enabled). Fail closed."""
    try:
        return {"live_price_ui": settings()}
    except Exception as e:                                # pragma: no cover — settings() does not raise
        log.error(f"[live_price] settings failed, overlay off: {e!r}")
        return {"live_price_ui": {"enabled": False, "poll_s": POLL_DEFAULT_S}}


# ── small helpers ───────────────────────────────────────────────────────────────

def _pos(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if (x == x and 0 < x < 1e9) else None


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


def _iso(epoch) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _day(v):
    s = str(v or "")[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _et(epoch) -> datetime:
    return datetime.fromtimestamp(epoch, ET)


def market_state(now: float = None) -> dict:
    """{"state": pre | regular | after | closed, "open": bool} from the clock and Mason's trading calendar:
    04:00-09:30 pre, 09:30-close regular (close = 13:00 on a half day), close-20:00 after (close-17:00 on a half
    day: the after-hours session ends at 17:00 then), else closed."""
    et = _et(_now() if now is None else now)
    d = et.date()
    state = "closed"
    if _trading_day(d):
        mins = et.hour * 60 + et.minute
        ch, cm = _close_hm(d)
        close = ch * 60 + cm
        after_end = 17 * 60 if close < 16 * 60 else 20 * 60
        if 4 * 60 <= mins < 9 * 60 + 30:
            state = "pre"
        elif 9 * 60 + 30 <= mins < close:
            state = "regular"
        elif close <= mins < after_end:
            state = "after"
    return {"state": state, "open": state != "closed"}


def parse_symbols(raw) -> tuple:
    """(usable symbols in request order, no duplicates; {symbol: why not}). Reads at most RAW_MAX_CHARS characters and
    lists at most PROBLEMS_MAX problems, so a huge or junk-filled query costs nothing extra. ASCII letters only
    (str.upper() would turn some non-ASCII letters into ASCII ones)."""
    good, bad = [], {}

    def note(k, why):
        if len(bad) < PROBLEMS_MAX:
            bad[k] = why
    for part in str(raw or "")[:RAW_MAX_CHARS].split(","):
        s = part.strip()
        if not s:
            continue
        if not s.isascii():
            note(s.encode("ascii", "replace").decode("ascii")[:12].upper(), MSG_BAD)
            continue
        s = s.upper()
        m = re.fullmatch(r"([A-Z]{1,5})-([A-Z])", s)
        if m:
            s = f"{m.group(1)}.{m.group(2)}"
        if not _SYM_RE.fullmatch(s):
            note(s[:12], MSG_BAD)
        elif s in good:
            continue
        elif len(good) >= MAX_SYMBOLS:
            note(s, MSG_TOO_MANY)
        else:
            good.append(s)
    return good, bad


def _prev_close(price_ts: float, after_close: bool, candidates, same_day_only: bool = False) -> float | None:
    """The newest official close before the price: candidates = [(YYYY-MM-DD, close)], earlier entries win a tie. A
    price printed after that day's regular close may be compared with the same day's close; anything else only with
    an earlier day's. same_day_only: ONLY the same day's close counts (an after-hours price on a trading day: the
    prior session's close would make the colour flip at Robinhood's evening roll-over), else None."""
    session = _et(price_ts).date().isoformat()
    best = None
    for day, px in candidates:
        if day is None or px is None:
            continue
        ok = day == session if same_day_only else (day < session or (after_close and day == session))
        if ok and (best is None or day > best[0]):
            best = (day, px)
    return round(best[1], 4) if best else None


def _after_close(ts: float) -> bool:
    et = _et(ts)
    ch, cm = _close_hm(et.date())
    return et.hour * 60 + et.minute >= ch * 60 + cm


def _close_ts(d) -> float:
    ch, cm = _close_hm(d)
    return datetime(d.year, d.month, d.day, ch, cm, tzinfo=ET).timestamp()


def quote_from_rh(row) -> dict | None:
    """One get_equity_quotes row -> {price, ts, prev_close, session, source} or None."""
    if not isinstance(row, dict):
        return None
    q, close = row.get("quote") or {}, row.get("close") or {}
    reg, reg_ts = _pos(q.get("last_trade_price")), _parse_ts(q.get("venue_last_trade_time"))
    ext, ext_ts = _pos(q.get("last_non_reg_trade_price")), _parse_ts(q.get("venue_last_non_reg_trade_time"))
    off, off_day = _pos(close.get("price")), _day(close.get("date"))
    have_reg = reg is not None and reg_ts is not None
    have_ext = ext is not None and ext_ts is not None
    if have_ext and (not have_reg or ext_ts > reg_ts):
        price, ts, session = ext, ext_ts, "extended"
    elif have_reg:
        price, ts, session = reg, reg_ts, "regular"
        if off is not None and off_day == _et(reg_ts).date().isoformat():
            price = off                                   # the session has closed: its official close
    else:
        return None
    prev = _pos(q.get("adjusted_previous_close")) or _pos(q.get("previous_close"))
    cands = [(_day(q.get("previous_close_date")), prev), (off_day, off)]
    after = session == "extended" and _after_close(ts)
    same_day = False
    if after:
        d = _et(ts).date()
        if _trading_day(d):
            # Evening trap (review 2026-11-19): from the close until Robinhood's roll-over at about 20:00 ET, its
            # previous close AND its close block are still dated to the session BEFORE (recorded case:
            # Tests/test_overnight_review.py test_evening_previous_close_trap). Only the same day's close counts for an
            # after-hours price: the official one, else that day's closing trade (as mason_overnight.equity_record
            # does), else none — never the day before's, which made the colour flip at 20:00 with no price change.
            same_day = True
            if have_reg and _et(reg_ts).date() == d and reg_ts >= _close_ts(d) - CLOSING_TRADE_S:
                cands.append((d.isoformat(), reg))
    pc = _prev_close(ts, after, cands, same_day_only=same_day)
    return {"price": round(price, 4), "ts": ts, "prev_close": pc, "session": session, "source": "robinhood"}


def quote_from_alpaca(snap) -> dict | None:
    """One Alpaca snapshot -> {price, ts, prev_close, session, source} or None (IEX last trade)."""
    if not isinstance(snap, dict):
        return None
    t, day, prev = snap.get("latestTrade") or {}, snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    price, ts = _pos(t.get("p")), _parse_ts(t.get("t"))
    if price is None or ts is None:
        return None
    cands = []
    for b in (day, prev):
        bts = _parse_ts(b.get("t"))
        if bts is not None:
            cands.append((_et(bts).date().isoformat(), _pos(b.get("c"))))
    et = _et(ts)
    mins = et.hour * 60 + et.minute
    after = _after_close(ts)
    session = "regular" if (9 * 60 + 30 <= mins and not after) else "extended"
    pc = _prev_close(ts, after, cands)
    if pc is not None and abs(price / pc - 1.0) > ALPACA_PREV_MAX_MOVE:
        pc = None                    # split day (the IEX daily bar may be the old share price): no colour, not a wrong one
    return {"price": round(price, 4), "ts": ts, "prev_close": pc, "session": session, "source": "alpaca_iex"}


# ── cache + single-flight fetch ─────────────────────────────────────────────────

_lock = threading.Lock()
_cache: "OrderedDict[str, dict]" = OrderedDict()      # sym -> {"q": quote dict, "at": epoch}
_wanted: "OrderedDict[str, float]" = OrderedDict()    # sym -> epoch it was last asked for (oldest first, <= WANTED_MAX)
_problems: dict = {}                                   # sym -> (plain reason of the last failed refresh, epoch)
_rh_unknown: dict = {}                                 # sym -> epoch until which Robinhood is not asked for it
_rh_times: deque = deque()                             # start times of this module's Robinhood calls, last 60 s
_alp_times: deque = deque()                            # ... and of its Alpaca calls
_st = {"inflight": None, "last_start": 0.0, "alpaca_paused_until": 0.0, "rh_running": False,
       "sources": {"robinhood": "not asked", "alpaca_iex": "not asked"}, "upstream": {"robinhood": 0, "alpaca_iex": 0}}


def reset() -> None:
    """Forget everything (tests; harmless in production)."""
    with _lock:
        _cache.clear()
        _wanted.clear()
        _problems.clear()
        _rh_unknown.clear()
        _rh_times.clear()
        _alp_times.clear()
        _st.update(inflight=None, last_start=0.0, alpaca_paused_until=0.0, rh_running=False,
                   sources={"robinhood": "not asked", "alpaca_iex": "not asked"},
                   upstream={"robinhood": 0, "alpaca_iex": 0})


def _take(times: deque, limit: int, now: float) -> bool:
    """Reserve one call in a sliding 60 s window of at most `limit` calls. Call with _lock held."""
    if times and times[-1] > now:                         # the wall clock was set back: start a new window
        times.clear()
    while times and now - times[0] >= 60.0:
        times.popleft()
    if len(times) >= limit:
        return False
    times.append(now)
    return True


def _remember_unknown(sym: str, now: float) -> None:
    """Robinhood does not know `sym`: skip it there for RH_UNKNOWN_S. Bounded table. Call with _lock held."""
    _rh_unknown[sym] = now + RH_UNKNOWN_S
    if len(_rh_unknown) > RH_UNKNOWN_MAX:
        for k in [k for k, until in _rh_unknown.items() if until <= now]:
            del _rh_unknown[k]
        while len(_rh_unknown) > RH_UNKNOWN_MAX:
            del _rh_unknown[next(iter(_rh_unknown))]


def status() -> dict:
    with _lock:
        return {"cached": len(_cache), "sources": dict(_st["sources"]), "upstream_calls": dict(_st["upstream"])}


def _store(sym: str, q: dict, at: float) -> None:
    with _lock:
        _cache[sym] = {"q": q, "at": at}
        _cache.move_to_end(sym)
        _problems.pop(sym, None)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def _rh_timed(args: dict) -> tuple:
    """_rh_read('get_equity_quotes'), waited for at most RH_WAIT_S. When the connection is down the call itself can
    take much longer (reconnect, one retry); it is then left to finish alone (it keeps the ticker card's own books)
    and this fetch moves on to Alpaca. Ceilings (review 2026-11-19), checked BEFORE anything is sent: none while the
    shared lanes are halved after a 429, only one of this module's calls running at a time (a call left to finish
    keeps the slot until it ends, so a slow Robinhood never gets a pile of them), at most RH_MAX_PER_MIN started in
    any 60 s."""
    if _rh_lane_strained():
        return "error", None, "robinhood strained"
    with _lock:
        if _st["rh_running"]:
            return "error", None, "robinhood busy"
        if not _take(_rh_times, RH_MAX_PER_MIN, _now()):
            return "error", None, "robinhood budget"
        _st["rh_running"] = True
        _st["upstream"]["robinhood"] += 1
    box = {}

    def run():
        try:
            box["v"] = _rh_read("get_equity_quotes", args)
        except BaseException as e:                         # noqa: BLE001
            box["v"] = ("error", None, f"robinhood: {type(e).__name__}")
        finally:
            with _lock:
                _st["rh_running"] = False

    t = threading.Thread(target=run, name="live-price-rh", daemon=True)
    t.start()
    t.join(RH_WAIT_S)
    return box.get("v") or ("error", None, "robinhood: no answer in time")


def _rh_rows(data) -> dict:
    out = {}
    rows = data.get("results") if isinstance(data, dict) else None
    for r in rows or []:
        if isinstance(r, dict):
            s = str(((r.get("quote") or {}).get("symbol")) or "").upper()
            if s:
                out[s] = r
    return out


def _rh_state(st: str, why) -> str:
    if st in ("ok", "empty"):
        return "ok"
    if st == "off":
        return "off"
    w = str(why or "")
    return ("paused" if w.endswith(("paused", "strained")) else "busy" if w.endswith(("busy", "budget"))
            else "error")


def _fetch(symbols: list) -> None:
    """Refresh `symbols`: Robinhood in one call, Alpaca in one call for whatever is left. Never raises."""
    now = _now()
    left = list(symbols)
    with _lock:
        ask_rh = [s for s in symbols if _rh_unknown.get(s, 0) <= now]
    rh_state = "not asked"
    if ask_rh:
        st, data, why = _rh_timed({"symbols": ask_rh})
        rh_state = _rh_state(st, why)
        rows = _rh_rows(data) if st == "ok" else {}
        if st == "empty" and len(ask_rh) == 1:
            with _lock:
                _remember_unknown(ask_rh[0], now)
        elif st == "empty":
            # One unknown symbol makes Robinhood refuse the whole batch: find it once, remember it. A refused batch of
            # more than RH_SPLIT_MAX is asked again in groups of RH_SPLIT_MAX (review 2026-11-19: before, it went to
            # Alpaca on every poll, so one such symbol on a busy page kept every chart on the IEX fallback), and a
            # refused group one by one. Every call counts against RH_MAX_PER_MIN; the search stops at the first call
            # that is not answered (paused / busy / over the ceiling / down) and Alpaca takes the rest.
            groups = ([ask_rh] if len(ask_rh) <= RH_SPLIT_MAX else
                      [ask_rh[i:i + RH_SPLIT_MAX] for i in range(0, len(ask_rh), RH_SPLIT_MAX)])
            for g in groups:
                if len(groups) > 1:
                    stg, dg, _wg = _rh_timed({"symbols": g})
                    if stg == "ok":
                        rows.update(_rh_rows(dg))
                        continue
                    if stg != "empty":
                        break
                    if len(g) == 1:
                        with _lock:
                            _remember_unknown(g[0], now)
                        continue
                answered = True
                for s in g:
                    st1, d1, _w1 = _rh_timed({"symbols": [s]})
                    if st1 == "ok":
                        rows.update(_rh_rows(d1))
                    elif st1 == "empty":
                        with _lock:
                            _remember_unknown(s, now)
                    else:
                        answered = False
                        break
                if not answered:
                    break
        at = _now()
        for s in ask_rh:
            q = quote_from_rh(rows.get(s))
            if q:
                _store(s, q, at)
                left.remove(s)
    alp_state = "not asked"
    if left:
        with _lock:
            paused = 0.0 < _st["alpaca_paused_until"] - _now() <= ALPACA_PAUSE_S     # (a clock set back ends it)
            allowed = not paused and _take(_alp_times, ALPACA_MAX_PER_MIN, _now())
            if allowed:
                _st["upstream"]["alpaca_iex"] += 1
        if paused:
            alp_state = "paused"
        elif not allowed:
            alp_state = "busy"
        else:
            code, d = _alpaca_get("/v2/stocks/snapshots", {"symbols": ",".join(left), "feed": "iex"})
            if code == 200 and isinstance(d, dict):
                alp_state = "ok"
                snaps = d.get("snapshots") if isinstance(d.get("snapshots"), dict) else d
                at = _now()
                for s in list(left):
                    q = quote_from_alpaca(snaps.get(s))
                    if q:
                        _store(s, q, at)
                        left.remove(s)
            else:
                alp_state = "error"
                with _lock:
                    _st["alpaca_paused_until"] = _now() + ALPACA_PAUSE_S
                log.warning(f"[live_price] Alpaca snapshots failed ({code}); paused {ALPACA_PAUSE_S:g}s")
    with _lock:
        _st["sources"] = {"robinhood": rh_state, "alpaca_iex": alp_state}
        at = _now()
        for s in left:
            _problems[s] = (MSG_NO_SOURCE, at)
        if len(_problems) > CACHE_MAX:
            _problems.clear()


def _maybe_fetch(symbols: list, now: float, ttl: float):
    """Start (or join) the one fetch if any of `symbols` is due. Returns a threading.Event to wait on, or None."""
    with _lock:
        for s in symbols:
            _wanted[s] = now
            _wanted.move_to_end(s)
        while _wanted and (now - next(iter(_wanted.values())) > WANTED_S or len(_wanted) > WANTED_MAX):
            _wanted.popitem(last=False)                    # oldest first: bounded, and O(dropped) per request

        # A negative age means the server's wall clock was set back (review 2026-11-19): that answer counts as due,
        # never as fresh, and the gap check lets the fetch through, so a price cannot freeze while looking live.
        def is_due(s):
            p = _problems.get(s)
            if p is not None and 0.0 <= now - p[1] < RETRY_S:
                return False
            return s not in _cache or not (-CLOCK_SLACK_S <= now - _cache[s]["at"] < ttl)
        due = [s for s in symbols if is_due(s)]
        if not due:
            return None
        if _st["inflight"] is not None:
            return _st["inflight"]
        if 0.0 <= now - _st["last_start"] < MIN_GAP_S:
            return None
        batch = list(due)
        for s in reversed(_wanted):                        # the most recently asked first
            if len(batch) >= MAX_BATCH:
                break
            if s not in batch and is_due(s):
                batch.append(s)
        done = threading.Event()
        _st["inflight"], _st["last_start"] = done, now

    def run():
        try:
            _fetch(batch)
        except BaseException as e:                         # noqa: BLE001 — the thread must always release the slot
            log.warning(f"[live_price] fetch failed: {type(e).__name__}: {e}")
        finally:
            with _lock:
                if _st["inflight"] is done:
                    _st["inflight"] = None
            done.set()

    threading.Thread(target=run, name="live-price-fetch", daemon=True).start()
    return done


def get(symbols_raw) -> tuple:
    """(payload, http status) for GET /api/live_prices."""
    cfg = settings()
    if not cfg["enabled"]:
        return {"enabled": False}, 200
    syms, bad = parse_symbols(symbols_raw)
    if not syms:
        return {"enabled": True, "ok": False, "error": MSG_NONE, "code": "bad_request"}, 400
    now = _now()
    market = market_state(now)
    ev = _maybe_fetch(syms, now, TTL_OPEN_S if market["open"] else TTL_CLOSED_S)
    if ev is not None:
        ev.wait(WAIT_S)
    now = _now()
    quotes, problems = {}, dict(bad)
    with _lock:
        for s in syms:
            c = _cache.get(s)
            if c is None or not (-CLOCK_SLACK_S <= now - c["at"] <= KEEP_S):    # future-dated: clock set back
                quotes[s] = None
                problems[s] = (_problems.get(s) or (MSG_WAIT,))[0]
                continue
            q = c["q"]
            quotes[s] = {"price": q["price"], "ts": _iso(q["ts"]), "price_age_s": round(max(0.0, now - q["ts"]), 1),
                         "prev_close": q["prev_close"], "session": q["session"], "source": q["source"],
                         "answer_age_s": round(max(0.0, now - c["at"]), 1)}
        sources = dict(_st["sources"])
    return {"enabled": True, "ok": True, "as_of": _iso(now), "poll_s": cfg["poll_s"], "market": market,
            "quotes": quotes, "problems": problems, "sources": sources}, 200


@bp.route("/api/live_prices")
def api_live_prices():
    try:
        payload, code = get(request.args.get("symbols"))
        return jsonify(payload), code
    except Exception:
        log.exception("[live_price] /api/live_prices failed")       # the detail goes to the log only
        return jsonify({"enabled": True, "ok": False, "code": "server_error",
                        "error": "Mason could not read live prices just now."}), 500
