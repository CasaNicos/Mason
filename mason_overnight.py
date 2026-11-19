"""
mason_overnight.py — the overnight and pre-market read (audit R6, 2026-11-16).

Mason used to be blind from 20:00 to 04:00 and at weekends: no index futures, VIX, dollar or rates, so the 07:00
director review got no picture of how the market was set up before the open. This module takes a few cheap,
time-boxed snapshots through the night from FREE sources Mason already uses, stores them, and turns the latest
one into plain facts:

  * Yahoo (yfinance chart data, 15-minute bars over 5 days, pre/post included) for S&P 500 / Nasdaq 100 /
    Russell 2000 / Dow futures, the VIX, the US dollar index, the 10-year Treasury yield (and the 10-year
    Treasury futures, which trade overnight when the yield itself is not quoted), crude oil, gold and bitcoin.
    One request per symbol, one at a time (Yahoo's shared cookie breaks under concurrent requests).
  * Robinhood, read only (mason_robinhood._call, 'live' lane): the VIX and bitcoin (BRTI) index quotes as a second
    source, the 24-hour market quotes of SPY / QQQ / IWM / DIA (a stand-in when the futures are not available),
    and from 04:00 the extended-hours quotes of the stocks held (Mason's and Nico's), watched, and in today's news.
  * The watchlist tape's stored pre-market minute bars (watch_bars) for the shares traded since 04:00; Yahoo
    fills the volume of the biggest movers it does not cover.

SCHEDULE (its own thread "overnight-tape", a check every 30 s, watched by mason_runtime). For each trading day D
the night runs from the evening before ("the eve") to D's 09:30 open. Snapshot slots (overnight.slots_et):
eve 20:00, D 00:00, 04:00, 06:30, 06:50, 08:40, plus eve 18:15 (overnight.weekend_open_et) when the eve is not a
trading day (Sunday, the last day of a long weekend: the first read after the futures reopen at 18:00). A slot
runs once, at most grace_minutes late (a restart inside the grace still catches it; older slots are skipped, never
caught up), and stops starting new requests after budget_seconds. The thread never touches the maintenance thread,
so it cannot delay the 02:30 backup or the 03:15-06:15 grader; its Robinhood calls use the 'live' lane, not the
grader's 'bulk' lane. Friday night and Saturday are quiet by design (no stock market Monday-morning until Sunday's
futures open), as is the night before a holiday.

DEFINITIONS (all Eastern time, DST-safe through zoneinfo)
  prior close  the last NYSE session before D, at its close: 16:00 (13:00 on a half day) from the 15-minute bars;
               the VIX and the 10-year yield use their own last value of that day (16:15 / 15:00). For stocks:
               Robinhood's official close when it is dated that day, else the regular-session closing trade.
  since 8 pm   from the bar that ends at 20:00 on the eve.
  fresh        a quote no older than fresh_minutes (30) when the snapshot ran. Anything else is "not available":
               an old value is never shown as a new one. The VIX and the 10-year yield are not quoted overnight
               by these sources; they are shown as their close, labelled with the day.

OUTPUTS (all OFF by default; installing changes nothing until a switch is turned on)
  overnight.enabled                  the snapshots themselves (the thread idles while false)
  overnight.director_block.enabled   "OVERNIGHT AND PRE-MARKET" section in the 07:00 review (mason_director)
  overnight.desk_check_block.enabled two short lines in quick checks before the open (mason_desk_check)
  overnight.alerts.enabled           inbox notes: S&P / Nasdaq 100 futures beyond +-futures_move_pct, the VIX up
                                     vix_jump_pct, a held stock's pre-market price through its stop. One per event.
  GET /api/overnight                 read-only JSON for a future widget (docs/fable/OVERNIGHT_API.md)
  mason_health 'overnight_tape'      last scheduled read; quiet nights, weekends and holidays are normal

Never places, changes or cancels an order; calls no model. Public functions never raise.

Manual use:
  python3 mason_overnight.py status            what ran, the health line
  python3 mason_overnight.py block             the director block and quick-check lines from what is stored
  python3 mason_overnight.py snapshot          take one snapshot NOW (writes to the database; network)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db

log = logging.getLogger("mason.overnight")

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
ET = ZoneInfo("America/New_York")
_CREATED_BY = "mason_overnight"
_THREAD = "overnight-tape"

_DEFAULTS = {
    "enabled": False,
    "slots_et": ["20:00", "00:00", "04:00", "06:30", "06:50", "08:40"],
    "weekend_open_et": "18:15",
    "grace_minutes": 20,
    "budget_seconds": 60,
    "fresh_minutes": 30,
    "equities_from_et": "04:00",
    "max_equities": 60,
    "catalyst_min_score": 0.65,
    "max_catalysts": 10,
    "movers_min_pct": 1.0,
    "keep_days": 120,
    "director_block": {"enabled": False, "max_tokens": 400, "movers": 5, "max_stops": 4},
    "desk_check_block": {"enabled": False, "max_age_minutes": 180},
    "alerts": {"enabled": False, "futures_move_pct": 1.5, "futures_keys": ["sp500_fut", "nasdaq100_fut"],
               "vix_jump_pct": 20.0, "stop_gap": True},
}

# key, plain label, Yahoo symbol, kind, decimals, unit, extras.
#   kind "fut"     trades almost around the clock (futures, dollar index): prior close = 16:00 value
#        "session" quoted in the day only (VIX, 10-year yield): prior close = that day's last value
#        "crypto"  around the clock, every day
INSTRUMENTS = (
    {"key": "sp500_fut", "label": "S&P 500 futures", "yf": "ES=F", "kind": "fut", "dp": 2, "etf": "SPY", "settle": (16, 0)},
    {"key": "nasdaq100_fut", "label": "Nasdaq 100 futures", "yf": "NQ=F", "kind": "fut", "dp": 2, "etf": "QQQ",
     "settle": (16, 0)},
    {"key": "vix", "label": "VIX", "yf": "^VIX", "kind": "session", "dp": 2, "rh_index": "VIX"},
    {"key": "russell2000_fut", "label": "Russell 2000 futures", "yf": "RTY=F", "kind": "fut", "dp": 1, "etf": "IWM",
     "settle": (16, 0)},
    {"key": "dow_fut", "label": "Dow futures", "yf": "YM=F", "kind": "fut", "dp": 0, "etf": "DIA", "settle": (16, 0)},
    {"key": "dollar", "label": "US dollar index", "yf": "DX-Y.NYB", "kind": "fut", "dp": 2},   # an index: no roll
    {"key": "ten_year", "label": "10-year Treasury yield", "yf": "^TNX", "kind": "session", "dp": 3, "unit": "%",
     "level": True},
    {"key": "ten_year_fut", "label": "10-year Treasury futures", "yf": "ZN=F", "kind": "fut", "dp": 3, "settle": (15, 0)},
    {"key": "crude", "label": "Crude oil", "yf": "CL=F", "kind": "fut", "dp": 2, "unit": "$", "settle": (14, 30)},
    {"key": "gold", "label": "Gold", "yf": "GC=F", "kind": "fut", "dp": 1, "unit": "$", "settle": (13, 30)},
    {"key": "bitcoin", "label": "Bitcoin", "yf": "BTC-USD", "kind": "crypto", "dp": 0, "unit": "$", "rh_index": "BRTI"},
)
_BY_KEY = {i["key"]: i for i in INSTRUMENTS}
ETFS = (("SPY", "S&P 500 fund (SPY)"), ("QQQ", "Nasdaq 100 fund (QQQ)"),
        ("IWM", "Russell 2000 fund (IWM)"), ("DIA", "Dow fund (DIA)"))
# Robinhood index ids measured 2026-11-16 (get_indexes); resolved again once a day, these are the fallback
_RH_INDEX_IDS = {"VIX": "3b912aa2-88f9-4682-8ae3-e39520bdf4db", "BRTI": "1b489c2d-6bb0-433d-bad1-d832b6931b33"}
_HALF_DAYS_FALLBACK = {"2026-11-27", "2026-12-24", "2027-11-26", "2027-12-23"}
_BAR_S = 15 * 60
_SYM_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")
_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")

_TABLES = """
CREATE TABLE IF NOT EXISTS overnight_runs(
    id TEXT PRIMARY KEY, slot_key TEXT UNIQUE, slot_label TEXT, slot_ts REAL, target_day TEXT, ref_day TEXT,
    eve_day TEXT, started_at TEXT, started_epoch REAL, finished_epoch REAL, status TEXT, manual INTEGER DEFAULT 0,
    calls_json TEXT, errors_json TEXT, budget_hit INTEGER DEFAULT 0, n_fresh INTEGER, n_total INTEGER,
    n_equities INTEGER, eq_from_epoch REAL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_overnight_runs_day ON overnight_runs(target_day, slot_ts);
CREATE TABLE IF NOT EXISTS overnight_quotes(
    run_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT, symbol TEXT, label TEXT, source TEXT,
    value REAL, value_ts REAL, status TEXT, ref_close REAL, ref_close_ts REAL, prior_close REAL, value_2000 REAL,
    volume REAL, held_by TEXT, why TEXT, stop REAL, stop_side TEXT, stop_who TEXT, note TEXT,
    PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS overnight_alerts(
    event_key TEXT PRIMARY KEY, created_at TEXT, kind TEXT, ticker TEXT, title TEXT, notification_id TEXT);
"""

_cfg_cache = {"mtime": None, "data": {}}
_schema_done: set = set()
_run_lock = threading.Lock()
_state = {"thread": None, "started": False, "last_tick": None, "last_error": None, "index_ids": None,
          "index_ids_day": None}


# ── hooks (tests replace these) ─────────────────────────────────────────────────

def _now() -> float:
    return time.time()


_mono = time.monotonic


def _yf_bars(symbol: str, period: str = "5d", interval: str = "15m") -> tuple:
    """([(start_epoch, close, volume)], history_metadata) from Yahoo's chart data. Raises on failure.
    The symbol goes through mason_symbols.yf_symbol like every other Yahoo call (BRK.B -> BRK-B; the futures,
    index and crypto symbols here pass unchanged)."""
    import yfinance as yf
    try:
        from mason_symbols import yf_symbol
    except Exception:
        yf_symbol = str
    tk = yf.Ticker(yf_symbol(symbol))
    df = tk.history(period=period, interval=interval, prepost=True, auto_adjust=False, timeout=10)
    try:
        meta = dict(tk.history_metadata or {})
    except Exception:
        meta = {}
    bars = []
    if df is not None and len(df):
        vols = df["Volume"] if "Volume" in df.columns else [0] * len(df)
        for ts, c, v in zip(df.index, df["Close"], vols):
            try:
                c = float(c)
                if c != c or c <= 0:
                    continue
                v = float(v) if v == v else 0.0
                bars.append((int(ts.timestamp()), c, v))
            except Exception:
                continue
    if not bars and not meta.get("regularMarketPrice"):
        raise RuntimeError("Yahoo returned no data")
    bars.sort()
    return bars, meta


def _rh_call(tool: str, args: dict):
    """Robinhood, read only, through Mason's one client (live lane). Raises when switched off or failing."""
    import mason_robinhood as RH
    if not RH.enabled():
        raise RuntimeError("Robinhood is switched off (robinhood.enabled)")
    return RH._call(tool, args, lane="live")


def _within(seconds: float, fn, *args):
    """fn(*args) in a helper thread, waiting at most `seconds` of real time. A call still running after that is left
    to finish in the background (Robinhood's own client ends it within about a minute) and its answer is dropped, so
    a hung source can never hold the snapshot past its share of the time limit. (review fix R6-1)"""
    box = {}

    def run():
        try:
            box["v"] = fn(*args)
        except BaseException as e:            # noqa: BLE001 — handed back to the caller below
            box["e"] = e
    th = threading.Thread(target=run, daemon=True, name="overnight-call")
    th.start()
    th.join(max(0.0, float(seconds)))
    if th.is_alive():
        raise TimeoutError(f"no answer within {max(0.0, float(seconds)):.0f} s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _book() -> dict:
    """Who holds what (Mason's open trades with stops, orders waiting for the open, Nico's lots, the watchlist)."""
    import mason_desk_check as DC
    return DC.load_book(broker=True)


def _catalysts(since_epoch: float, min_score: float, limit: int) -> list:
    import mason_triage as TR
    return TR.top_catalysts(since_epoch=since_epoch, min_score=min_score, limit=limit) or []


def _notify(**kw):
    import mason_notifications as MN
    return MN.add_notification(kw.pop("type"), kw.pop("title"), **kw)


# ── config ──────────────────────────────────────────────────────────────────────

def _merge(defaults: dict, over) -> dict:
    out = dict(defaults)
    for k, v in (over.items() if isinstance(over, dict) else []):
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_cfg() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8")) or {}
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"]
    except Exception:
        return _cfg_cache["data"] or {}


def cfg(full: dict | None = None) -> dict:
    src = full if full is not None else _load_cfg()
    return _merge(_DEFAULTS, (src or {}).get("overnight") if isinstance(src, dict) else None)


def enabled(full: dict | None = None) -> bool:
    try:
        return cfg(full).get("enabled") is True
    except Exception:
        return False


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _hm(s, default="00:00") -> tuple:
    try:
        h, _, m = str(s).strip().partition(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    h, _, m = default.partition(":")
    return int(h), int(m)


# ── calendar ────────────────────────────────────────────────────────────────────

def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), ET)


def _at(d: date, h: int, m: int) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def _as_et(dt: datetime) -> datetime:
    """An Eastern-time datetime from whatever the caller has (a naive one is read as Eastern wall time)."""
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


def is_trading_day(d: date) -> bool:
    """A regular NYSE session (full rule calendar of mason_options, verified 2015-2030)."""
    try:
        import mason_options
        return bool(mason_options.nyse_session_expected(d))
    except Exception:
        pass
    try:
        import mason_director
        return bool(mason_director.is_trading_day(_at(d, 12, 0)))
    except Exception:
        return d.weekday() < 5


def _half_days() -> set:
    try:
        import mason_desk_pipeline as PP
        return set(PP._NYSE_HALF_DAYS) | _HALF_DAYS_FALLBACK
    except Exception:
        return set(_HALF_DAYS_FALLBACK)


def close_hm(d: date) -> tuple:
    return (13, 0) if d.isoformat() in _half_days() else (16, 0)


def prev_trading_day(d: date) -> date:
    x = d - timedelta(days=1)
    for _ in range(12):
        if is_trading_day(x):
            return x
        x -= timedelta(days=1)
    return x


def next_trading_day(d: date) -> date:
    """The first trading day ON or after d."""
    x = d
    for _ in range(12):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    return x


def night_for_target(target: date, c: dict | None = None) -> dict:
    c = c or cfg()
    eve = target - timedelta(days=1)
    start = _at(eve, *_hm(c.get("weekend_open_et"), "18:15")) if not is_trading_day(eve) else _at(eve, 18, 0)
    start = min(start, _at(eve, 18, 0))
    return {"target": target, "eve": eve, "ref_day": prev_trading_day(target), "start": start,
            "end": _at(target, 9, 30)}


def night_for(et: datetime, c: dict | None = None) -> dict | None:
    """The overnight window `et` is in (eve 18:00 -> target 09:30), or None (daytime, Friday night, Saturday ...)."""
    d = et.date()
    target = next_trading_day(d if et < _at(d, 9, 30) else d + timedelta(days=1))
    n = night_for_target(target, c)
    return n if n["start"] <= et < n["end"] else None


def last_night(et: datetime, c: dict | None = None) -> dict:
    """The night `et` is in, else the most recent one that has ended (its morning read is the current one)."""
    n = night_for(et, c)
    if n:
        return n
    d = et.date()
    t = d if (is_trading_day(d) and et >= _at(d, 9, 30)) else prev_trading_day(d)
    return night_for_target(t, c)


def slots_for(night: dict, c: dict | None = None) -> list:
    """[(label, datetime ET)] for one night, in time order. Evening times (12:00 and later) belong to the eve,
    morning times to the target day; the weekend-open slot only when the eve is not a trading day."""
    c = c or cfg()
    out = []
    eve, target = night["eve"], night["target"]
    if not is_trading_day(eve):
        out.append(("weekend_open", _at(eve, *_hm(c.get("weekend_open_et"), "18:15"))))
    for s in c.get("slots_et") or []:
        h, m = _hm(s)
        dt = _at(eve, h, m) if h >= 12 else _at(target, h, m)
        out.append((f"{h:02d}:{m:02d}", dt))
    out = [(lab, dt) for lab, dt in out if night["start"] <= dt < night["end"]]
    seen, uniq = set(), []
    for lab, dt in sorted(out, key=lambda x: x[1]):
        if lab not in seen:
            seen.add(lab)
            uniq.append((lab, dt))
    return uniq


def slot_key(night: dict, label: str) -> str:
    return f"{night['target'].isoformat()} {label}"


def due_slot(now: float, c: dict | None = None, done=None) -> dict | None:
    """The slot to run at `now`: the latest slot of the current night whose time has passed, if it is not older
    than grace_minutes and has not run yet. Older passed slots are never caught up. `done(key) -> bool`."""
    c = c or cfg()
    et = _et(now)
    night = night_for(et, c)
    if not night:
        return None
    grace = max(60.0, float(c.get("grace_minutes") or 20) * 60)
    passed = [(lab, dt) for lab, dt in slots_for(night, c) if dt.timestamp() <= now]
    if not passed:
        return None
    lab, dt = passed[-1]
    if now - dt.timestamp() > grace:
        return None
    key = slot_key(night, lab)
    if done is not None and done(key):
        return None
    return {"key": key, "label": lab, "at": dt, "night": night}


# ── database ────────────────────────────────────────────────────────────────────

def _conn():
    return mason_db.get_connection()


def ensure_schema() -> bool:
    key = str(Path(mason_db.DB_FILE).resolve())
    if key in _schema_done:
        return True
    try:
        conn = _conn()
        try:
            conn.executescript(_TABLES)
            conn.commit()
        finally:
            conn.close()
        _schema_done.add(key)
        return True
    except Exception as e:
        log.warning(f"[overnight] could not create the tables: {e}")
        return False


def _rows(sql, args=()) -> list:
    """Read-only query; [] when the tables do not exist yet (nothing was ever stored)."""
    try:
        conn = _conn()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
    except Exception as e:
        if "no such table" not in str(e):
            log.info(f"[overnight] read failed: {e}")
        return []


def _slot_done(key: str) -> bool:
    return bool(_rows("SELECT 1 FROM overnight_runs WHERE slot_key=? LIMIT 1", (key,)))


# ── parsing ─────────────────────────────────────────────────────────────────────

def _parse_iso(s) -> float | None:
    """RFC 3339 with Z / offset / nanosecond fraction -> epoch, None if unreadable."""
    try:
        s = str(s or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
        frac = 0.0
        if m:
            frac = float("0" + m.group(2)) if m.group(2) else 0.0
            s = m.group(1) + m.group(3)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() + frac
    except Exception:
        return None


def _bar_before(bars: list, end_ts: float, not_before: float | None = None):
    """(start, close) of the last bar that STARTS before end_ts (and not before not_before)."""
    best = None
    for b in bars:
        if b[0] >= end_ts:
            break
        if not_before is not None and b[0] < not_before:
            continue
        best = (b[0], b[1])
    return best


def macro_record(inst: dict, bars: list, meta: dict, night: dict, now: float, c: dict | None = None) -> dict:
    """One instrument from Yahoo bars + chart metadata. Pure. status: ok | closed | stale | error."""
    c = c or cfg()
    rec = {"key": inst["key"], "kind": "macro", "symbol": inst["yf"], "label": inst["label"], "source": "yahoo",
           "value": None, "value_ts": None, "status": "error", "ref_close": None, "ref_close_ts": None,
           "prior_close": None, "value_2000": None, "note": ""}
    bars = sorted(b for b in (bars or []) if b and (_num(b[1]) or 0) > 0)
    meta = meta or {}
    val = vts = None
    rmp, rmt = _num(meta.get("regularMarketPrice")), _num(meta.get("regularMarketTime"))
    if bars:                                   # the last bar's close is "as of" its end, or the last trade inside it
        val, vts = bars[-1][1], min(bars[-1][0] + _BAR_S, now)
        if rmt and bars[-1][0] <= rmt < vts:
            vts = rmt
    if rmp and rmt and rmt <= now + 120 and (vts is None or rmt > vts):
        val, vts = rmp, min(rmt, now)
    if val is None:
        rec["note"] = "no data from Yahoo"
        return rec
    rec["value"], rec["value_ts"] = val, vts
    ref_day = night["ref_day"]
    if inst["kind"] == "session":
        r = _bar_before(bars, _at(ref_day, 23, 59).timestamp(), _at(ref_day, 9, 30).timestamp())
        pd_ = prev_trading_day(ref_day)
        p = _bar_before(bars, _at(pd_, 23, 59).timestamp(), _at(pd_, 9, 30).timestamp())
    else:
        cl = _at(ref_day, *close_hm(ref_day)).timestamp()
        r = _bar_before(bars, cl, cl - 3600)
        pd_ = prev_trading_day(ref_day)
        pcl = _at(pd_, *close_hm(pd_)).timestamp()
        p = _bar_before(bars, pcl, pcl - 3600)
    if r:
        rec["ref_close"], rec["ref_close_ts"] = r[1], min(r[0] + _BAR_S, now)
    if p:
        rec["prior_close"] = p[1]
    # VIX / 10-year: when the latest value is the prior session's close, Yahoo's previousClose is the official close of
    # the session before it (the bars can be a few cents off the official print)
    pcs = _num(meta.get("previousClose"))
    if inst["kind"] == "session" and pcs and _et(vts).date() == ref_day:
        rec["prior_close"] = pcs
    # Futures roll guard: Yahoo's continuous series (ES=F, CL=F ...) switches to the next contract in roll weeks,
    # and a 16:00 bar of the old contract against a quote of the new one shows a move that never happened. The
    # chart's previousClose is the last session's settlement of the contract being quoted. It is compared with the
    # bar AT THAT SETTLEMENT TIME (stock-index futures 16:00, 10-year 15:00, crude 14:30, gold 13:30; review fix R6-2:
    # comparing crude or gold with their 16:00 bar mistook an ordinary afternoon move for a roll); when it is more than
    # 0.25% away (a calendar spread; same-time differences measured 2026-11-16 were at most 0.04%) or the 16:00 bar is
    # missing, the settlement is the reference. The dollar index is not a contract and has no roll.
    # Only when that settlement is the prior close's own session: on a weeknight (eve = the prior session) or after a
    # weekend. After a holiday the futures traded on the eve (Thanksgiving, for one) and their settlement is not it.
    pc = _num(meta.get("previousClose"))
    same_session = night["eve"] == ref_day or night["eve"].weekday() >= 5
    settle = inst.get("settle")
    sb = None
    if settle:
        st = _at(ref_day, *min(tuple(settle), close_hm(ref_day))).timestamp()
        sb = _bar_before(bars, st, st - 3600)
    if inst["kind"] == "fut" and settle and pc and same_session and (rec["ref_close"] is None or sb is None
                                                                     or abs(sb[1] / pc - 1) > 0.0025):
        rec["note"] = ("prior close is the exchange settlement (the 16:00 bar was "
                       + ("missing)" if rec["ref_close"] is None else f"{rec['ref_close']:g}; contract roll?)"))
        rec["ref_close"], rec["ref_close_ts"] = pc, _at(ref_day, *close_hm(ref_day)).timestamp()
    e8 = _at(night["eve"], 20, 0).timestamp()
    if inst["kind"] != "session" and now >= e8:
        b8 = _bar_before(bars, e8, e8 - 3600)
        if b8:
            rec["value_2000"] = b8[1]
    fresh_s = max(60.0, float(c.get("fresh_minutes") or 30) * 60)
    if now - vts <= fresh_s:
        rec["status"] = "ok"
    elif inst["kind"] == "session" and _et(vts).date() == ref_day:
        rec["status"] = "closed"
    else:
        rec["status"] = "stale"
        rec["note"] = f"last quote {_et(vts).strftime('%a %H:%M')} ET"
    return rec


def _stale_or_ok(rec: dict, now: float, c: dict, ref_day: date) -> dict:
    vts = rec.get("value_ts")
    fresh_s = max(60.0, float(c.get("fresh_minutes") or 30) * 60)
    if rec.get("value") is None or vts is None:
        rec["status"] = "error"
    elif now - vts <= fresh_s:
        rec["status"] = "ok"
    elif _et(vts).date() == ref_day and rec.get("kind") == "macro":
        rec["status"] = "closed"
    else:
        rec["status"] = "stale"
        rec["note"] = (rec.get("note") or "") or f"last quote {_et(vts).strftime('%a %H:%M')} ET"
    return rec


def rh_index_values(data, ids: dict | None = None) -> dict:
    """{SYMBOL: (value, venue_epoch)} from a get_index_quotes answer (symbol from the row, else from its id)."""
    out = {}
    by_id = {v: k for k, v in (ids or {}).items()}
    rows = (data or {}).get("quotes") if isinstance(data, dict) else None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        v, ts = _num(r.get("value")), _parse_iso(r.get("venue_timestamp") or r.get("updated_at"))
        sym = str(r.get("symbol") or by_id.get(r.get("instrument_id")) or "").upper()
        if sym and v and ts:
            out[sym] = (v, ts)
    return out


def merge_rh_index(rec: dict, rh: tuple | None, now: float, c: dict, ref_day: date) -> dict:
    """Use Robinhood's index value when it is newer than Yahoo's (or Yahoo had nothing)."""
    if not rh:
        return rec
    v, ts = rh
    if rec.get("value_ts") is None or ts > float(rec["value_ts"]) + 60:
        rec = dict(rec, value=v, value_ts=min(ts, now), source="robinhood" if rec.get("value") is None
                   else "robinhood (newer than Yahoo)", note="")
        rec = _stale_or_ok(rec, now, c, ref_day)
    return rec


def equity_record(r: dict, night: dict, now: float, c: dict | None = None, *, key: str | None = None,
                  premarket_from: float | None = None) -> dict | None:
    """One stock / fund from a get_equity_quotes result row. The reference is the official close of the prior
    session when Robinhood dates it so, else the regular-session closing trade of that day (in the evening
    Robinhood still dates its previous close to the session BEFORE; never trust it undated)."""
    c = c or cfg()
    q = (r or {}).get("quote") or {}
    sym = str(q.get("symbol") or "").upper()
    if not sym:
        return None
    close = (r or {}).get("close") or {}
    ref_day = night["ref_day"]
    reg, reg_ts = _num(q.get("last_trade_price")), _parse_iso(q.get("venue_last_trade_time"))
    ext, ext_ts = _num(q.get("last_non_reg_trade_price")), _parse_iso(q.get("venue_last_non_reg_trade_time"))
    prev, prev_day = (_num(q.get("adjusted_previous_close")) or _num(q.get("previous_close")),
                      str(q.get("previous_close_date") or "")[:10])
    off, off_day = _num(close.get("price")), str(close.get("date") or "")[:10]
    ref = ref_ts = None
    if off and off_day == ref_day.isoformat():
        ref, ref_ts = off, _at(ref_day, *close_hm(ref_day)).timestamp()
    elif prev and prev_day == ref_day.isoformat():
        ref, ref_ts = prev, _at(ref_day, *close_hm(ref_day)).timestamp()
    elif reg and reg_ts:
        cl = _at(ref_day, *close_hm(ref_day))
        if _et(reg_ts).date() == ref_day and reg_ts >= cl.timestamp() - 300:
            ref, ref_ts = reg, reg_ts
    rec = {"key": key or f"eq:{sym}", "kind": "equity", "symbol": sym, "label": sym, "source": "robinhood",
           "value": None, "value_ts": None, "status": "error", "ref_close": ref, "ref_close_ts": ref_ts,
           "prior_close": None, "value_2000": None, "note": ""}
    if ref is None:
        rec["note"] = "no close for the prior session"
    if q.get("has_traded") is False or (q.get("state") and str(q.get("state")).lower() != "active"):
        rec["note"] = (rec["note"] + "; " if rec["note"] else "") + f"state {q.get('state') or 'not traded'}"
    # the latest trade after the prior close: extended-hours (overnight / pre-market) first
    lo = premarket_from if premarket_from is not None else (ref_ts or _at(ref_day, 16, 0).timestamp())
    cand = [(ts, v) for v, ts in ((ext, ext_ts), (reg, reg_ts)) if v and ts and ts > lo + 1]
    if not cand:
        rec["status"] = "no_trade"
        rec["note"] = (rec["note"] + "; " if rec["note"] else "") + "no trade yet in this session"
        return rec
    ts, v = max(cand)
    rec["value"], rec["value_ts"] = v, min(ts, now)
    fresh_s = max(60.0, float(c.get("fresh_minutes") or 30) * 60)
    # a stock's last pre-market trade may be minutes old in a thin name: it is still the latest price, so the
    # test here is that it happened in this session (after `lo`), and it is shown with its time when older
    rec["status"] = "ok" if (now - ts <= fresh_s or premarket_from is not None) else "stale"
    return rec


# ── one snapshot ────────────────────────────────────────────────────────────────

def _universe(book: dict, cats: list, c: dict) -> tuple:
    """[(ticker, held_by, why)] held first, then today's news, then the watchlist; capped at max_equities."""
    who, why = {}, {}

    def add(t, w=None, y=None):
        t = str(t or "").upper().strip()
        m = _OCC_RE.match(t)
        if m:
            t = m.group(1)
        if not t or not _SYM_RE.match(t):
            return
        who.setdefault(t, set())
        why.setdefault(t, set())
        if w:
            who[t].add(w)
        if y:
            why[t].add(y)
    for t in (book.get("mason") or {}):
        add(t, "Mason")
    for t in (book.get("queued") or {}):
        add(t, None, "order waiting for the open")
    for t in (book.get("nico") or {}):
        add(t, "Nico")
    for cat in cats or []:
        add(cat.get("primary_ticker"), None, "in the news")
    for t in (book.get("watch") or {}):
        add(t, None, "watched")
    held = [t for t in who if who[t] or "order waiting for the open" in why[t]]
    news = [t for t in who if t not in held and "in the news" in why[t]]
    rest = [t for t in who if t not in held and t not in news]
    order = held + news + rest
    order = order[:max(1, int(c.get("max_equities") or 60))]
    return [(t, " and ".join(sorted(who[t])), ", ".join(sorted(why[t]))) for t in order], who


def _stops(book: dict) -> dict:
    """{ticker: [(stop, side 'below'|'above', who, qty)]} for held STOCK positions with a stop on record.
    Options are left out: they do not trade before the open, and their stops are option prices."""
    out = {}
    for t, rows in (book.get("mason") or {}).items():
        for r in rows or []:
            if r.get("occ") or str(r.get("vehicle") or "stock").lower() not in ("stock", "shares", ""):
                continue
            s = _num(r.get("stop"))
            if s and s > 0:
                side = "above" if str(r.get("direction") or "long").lower() == "short" else "below"
                out.setdefault(str(t).upper(), []).append(
                    (s, side, f"Mason [{r.get('rec_id') or r.get('id') or '?'}]", _num(r.get("qty"))))
    # (review fix R6-8) a short on Nico's journal has its stop ABOVE the price: read the side from the journal row
    jids = sorted({str(lot.get("journal_id")) for lots in (book.get("nico") or {}).values() for lot in lots or []
                   if lot.get("journal_id") and not lot.get("is_option")})
    shorts = set()
    if jids:
        shorts = {str(r["id"]) for r in _rows(f"SELECT id FROM trades WHERE id IN ({','.join('?' * len(jids))}) "
                                              "AND LOWER(COALESCE(direction,'long'))='short'", tuple(jids))}
    for t, lots in (book.get("nico") or {}).items():
        for lot in lots or []:
            if lot.get("is_option"):
                continue
            s = _num(lot.get("stop"))
            if s and s > 0:
                side = "above" if str(lot.get("journal_id") or "") in shorts else "below"
                out.setdefault(str(t).upper(), []).append(
                    (s, side, f"Nico [{lot.get('journal_id') or '?'}]", _num(lot.get("qty"))))
    return out


def _premarket_volume(tickers: list, since_ts: float, until_ts: float) -> dict:
    """{ticker: shares} from the watchlist tape's stored minute bars (no network)."""
    out = {}
    if not tickers:
        return out
    try:
        conn = _conn()
        try:
            q = ",".join("?" * len(tickers))
            for r in conn.execute(f"SELECT ticker, SUM(v) AS v, COUNT(*) AS n FROM watch_bars WHERE tf='1m' "
                                  f"AND ticker IN ({q}) AND ts >= ? AND ts < ? GROUP BY ticker",
                                  list(tickers) + [int(since_ts), int(until_ts)]).fetchall():
                if r["n"] and float(r["v"] or 0.0) > 0:   # bars with no volume at all = volume unknown, not zero
                    out[str(r["ticker"]).upper()] = float(r["v"])
        finally:
            conn.close()
    except Exception as e:
        log.info(f"[overnight] pre-market volume from the tape unavailable: {e}")
    return out


def _index_ids(calls: dict) -> dict:
    today = _et(_now()).date().isoformat()
    if _state.get("index_ids") and _state.get("index_ids_day") == today:
        return dict(_state["index_ids"])
    ids = dict(_RH_INDEX_IDS)
    try:
        calls["robinhood"] += 1
        data = _rh_call("get_indexes", {"symbols": ",".join(sorted(_RH_INDEX_IDS))})
        for r in (data or {}).get("indexes") or []:
            s, i = str(r.get("symbol") or "").upper(), r.get("id")
            if s in ids and i:
                ids[s] = i
    except Exception as e:
        log.info(f"[overnight] Robinhood index ids not refreshed ({e}); using the known ones")
    _state.update(index_ids=dict(ids), index_ids_day=today)
    return ids


def run_snapshot(slot: dict | None = None, *, now: float | None = None, c: dict | None = None,
                 manual: bool = False, equities: bool | None = None) -> dict:
    """Take one snapshot and store it. `slot` from due_slot(); None = a manual snapshot now. Never raises."""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "another overnight snapshot is running"}
    try:
        return _run_snapshot(slot, now=now, c=c, manual=manual, equities=equities)
    except Exception as e:
        log.warning(f"[overnight] snapshot failed: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _run_lock.release()


def _run_snapshot(slot, *, now=None, c=None, manual=False, equities=None) -> dict:
    c = c or cfg()
    now = _now() if now is None else float(now)
    et = _et(now)
    if slot is None:
        night = night_for(et, c) or last_night(et, c)
        slot = {"key": f"manual {et.isoformat(timespec='seconds')}", "label": "manual", "at": et, "night": night}
        manual = True
    night = slot["night"]
    t0 = _mono()
    budget = max(5.0, float(c.get("budget_seconds") or 60))
    deadline = t0 + budget
    # Robinhood (and the book) go first but may use only 40% of the time, so a slow Robinhood can never leave the
    # futures without their turn; Yahoo then has the rest, most important symbol first
    rh_deadline = t0 + 0.4 * budget
    calls = {"yahoo": 0, "robinhood": 0}
    errors, budget_hit = [], False
    if not ensure_schema():
        return {"ok": False, "error": "tables unavailable"}
    run_id = uuid.uuid4().hex[:12]
    conn = _conn()
    try:
        with conn:
            conn.execute("INSERT INTO overnight_runs (id, slot_key, slot_label, slot_ts, target_day, ref_day, eve_day, "
                         "started_at, started_epoch, status, manual) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, slot["key"], slot["label"], slot["at"].timestamp(), night["target"].isoformat(),
                          night["ref_day"].isoformat(), night["eve"].isoformat(), et.isoformat(timespec="seconds"),
                          now, "running", 1 if manual else 0))
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"slot already taken or database busy: {e}"}
    conn.close()

    def time_left(limit=None) -> bool:
        nonlocal budget_hit
        if _mono() > (deadline if limit is None else limit):
            budget_hit = True
            return False
        return True

    def rh_left() -> float:                    # real seconds left of the Robinhood share (review fix R6-1)
        return max(0.0, rh_deadline - _mono())

    ref_day = night["ref_day"]
    eq_from = _at(night["target"], *_hm(c.get("equities_from_et"), "04:00"))
    want_eq = (et >= eq_from) if equities is None else bool(equities)
    recs = {}
    # 1. Robinhood first (a few fast calls): VIX / bitcoin index values, the four index funds, and the stocks.
    # Every call here waits at most for what is left of the Robinhood share: a hung call is dropped, never waited out
    rh_idx, rh_rows, universe, book, stops = {}, [], [], {}, {}
    if want_eq:
        try:
            book = _within(rh_left(), _book) or {}
        except Exception as e:
            budget_hit = budget_hit or isinstance(e, TimeoutError)
            errors.append(f"book: {type(e).__name__}: {e}")
        try:
            cats = _catalysts(_at(ref_day, *close_hm(ref_day)).timestamp() - 6 * 3600,
                              float(c.get("catalyst_min_score") or 0.65), int(c.get("max_catalysts") or 10))
        except Exception as e:
            cats = []
            errors.append(f"catalysts: {type(e).__name__}: {e}")
        universe, _who = _universe(book, cats, c)
        stops = _stops(book)
    try:
        if time_left(rh_deadline):
            try:
                ids = _within(rh_left(), _index_ids, calls)
            except TimeoutError:
                ids = dict(_RH_INDEX_IDS)
                budget_hit = True
                errors.append("robinhood index list: no answer in time; using the known ids")
            if time_left(rh_deadline):
                calls["robinhood"] += 1
                rh_idx = rh_index_values(_within(rh_left(), _rh_call, "get_index_quotes",
                                                 {"instrument_ids": [ids[k] for k in sorted(ids)]}), ids)
    except Exception as e:
        budget_hit = budget_hit or isinstance(e, TimeoutError)
        errors.append(f"robinhood index quotes: {type(e).__name__}: {str(e)[:160]}")
    syms = [s for s, _ in ETFS] + [t for t, _, _ in universe if t not in dict(ETFS)]
    for i in range(0, len(syms), 20):
        if not time_left(rh_deadline):
            errors.append("robinhood stock quotes: time limit reached")
            break
        try:
            calls["robinhood"] += 1
            data = _within(rh_left(), _rh_call, "get_equity_quotes", {"symbols": syms[i:i + 20]})
            rh_rows += [r for r in ((data or {}).get("results") or []) if isinstance(r, dict)]
        except Exception as e:
            budget_hit = budget_hit or isinstance(e, TimeoutError)
            errors.append(f"robinhood stock quotes: {type(e).__name__}: {str(e)[:160]}")
            break
    by_sym = {}
    for r in rh_rows:
        s = str(((r.get("quote") or {}).get("symbol")) or "").upper()
        if s:
            by_sym[s] = r
    for s, label in ETFS:
        rr = by_sym.get(s)
        rec = equity_record(rr, night, now, c, key=f"etf:{s}") if rr else None
        if rec:
            rec.update(kind="etf", label=label)
            # the fund's price at 20:00 on the eve is not in a quote; it is read from the 20:00 snapshot when shown
            recs[rec["key"]] = rec
    # stock trades count from 04:00 on the target day (pre-market); a snapshot taken by hand in the evening reads
    # the after-hours and overnight trades since the prior close instead, and says so (from one minute after the
    # close, so the closing auction's shares are not counted as after-hours volume)
    pm_from = eq_from.timestamp() if et >= eq_from else _at(ref_day, *close_hm(ref_day)).timestamp() + 60
    if want_eq:
        vols = _premarket_volume([t for t, _, _ in universe], pm_from, now)
        for t, held_by, why in universe:
            rr = by_sym.get(t)
            rec = equity_record(rr, night, now, c, premarket_from=pm_from) if rr else None
            if rec is None:
                rec = {"key": f"eq:{t}", "kind": "equity", "symbol": t, "label": t, "source": "robinhood",
                       "value": None, "value_ts": None, "status": "error", "ref_close": None, "ref_close_ts": None,
                       "prior_close": None, "value_2000": None, "note": "no Robinhood quote"}
            rec.update(held_by=held_by or None, why=why or None, volume=vols.get(t))
            st = stops.get(t)
            if st:
                # several positions in one stock can have different stops: name every one the price is through
                # (the deepest one gives the numbers), else keep the nearest one for the record
                px = rec.get("value") if rec.get("status") == "ok" else None

                def depth(e):
                    if px is None:
                        return -1e9
                    return (e[0] - px) / e[0] * 100 if e[1] == "below" else (px - e[0]) / e[0] * 100
                through = [e for e in st if depth(e) >= 0]
                pick = max(through or st, key=depth)
                group = through or [pick]
                rec.update(stop=pick[0], stop_side=pick[1], stop_who="; ".join(
                    f"{w}{'' if q is None else f' {q:g} shares'}" + (f", stop {s:,.2f}" if len(group) > 1 else "")
                    for s, _, w, q in group))
            recs[rec["key"]] = rec
    # 2. Yahoo, one symbol at a time, most important first
    for inst in INSTRUMENTS:
        if not time_left():
            recs[inst["key"]] = {"key": inst["key"], "kind": "macro", "symbol": inst["yf"], "label": inst["label"],
                                 "source": "yahoo", "value": None, "value_ts": None, "status": "skipped",
                                 "note": "time limit reached before this one"}
            continue
        try:
            calls["yahoo"] += 1
            bars, meta = _yf_bars(inst["yf"])
            rec = macro_record(inst, bars, meta, night, now, c)
        except Exception as e:
            rec = {"key": inst["key"], "kind": "macro", "symbol": inst["yf"], "label": inst["label"],
                   "source": "yahoo", "value": None, "value_ts": None, "status": "error",
                   "note": f"Yahoo: {type(e).__name__}: {str(e)[:120]}"}
            errors.append(f"{inst['yf']}: {rec['note']}")
        if inst.get("rh_index"):
            rec = merge_rh_index(rec, rh_idx.get(inst["rh_index"]), now, c, ref_day)
        recs[inst["key"]] = rec
    # 3. volume for the biggest movers the tape does not cover (Yahoo, a few names)
    if want_eq:
        lo_pct = float(c.get("movers_min_pct") or 1.0) / 100.0
        movers = sorted((r for r in recs.values() if r.get("kind") == "equity" and r.get("status") == "ok"
                         and r.get("ref_close") and r.get("volume") is None
                         and abs(r["value"] / r["ref_close"] - 1) >= lo_pct),
                        key=lambda r: -abs(r["value"] / r["ref_close"] - 1))
        n_fill = int((c.get("director_block") or {}).get("movers") or 5)
        for r in movers[:n_fill]:
            if not time_left():
                break
            try:
                calls["yahoo"] += 1
                bars, _m = _yf_bars(r["symbol"], period="1d", interval="5m")
                v = sum(b[2] for b in bars if pm_from <= b[0] < now)
                if v > 0:                      # Yahoo often reports no extended-hours volume: then it stays unknown
                    r["volume"] = v
                    r["note"] = (r.get("note") or "") + ("; " if r.get("note") else "") + "volume from Yahoo"
            except Exception as e:
                errors.append(f"volume {r['symbol']}: {type(e).__name__}")
    # 4. store
    fin = _now() if not manual else max(_now(), now)
    n_macro = [r for r in recs.values() if r.get("kind") == "macro"]
    n_fresh = sum(1 for r in n_macro if r.get("status") == "ok")
    n_eq = sum(1 for r in recs.values() if r.get("kind") == "equity" and r.get("status") == "ok")
    conn = _conn()
    try:
        with conn:
            for r in recs.values():
                conn.execute(
                    "INSERT OR REPLACE INTO overnight_quotes (run_id, key, kind, symbol, label, source, value, value_ts, "
                    "status, ref_close, ref_close_ts, prior_close, value_2000, volume, held_by, why, stop, stop_side, "
                    "stop_who, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, r["key"], r.get("kind"), r.get("symbol"), r.get("label"), r.get("source"),
                     r.get("value"), r.get("value_ts"), r.get("status"), r.get("ref_close"), r.get("ref_close_ts"),
                     r.get("prior_close"), r.get("value_2000"), r.get("volume"), r.get("held_by"), r.get("why"),
                     r.get("stop"), r.get("stop_side"), r.get("stop_who"), (r.get("note") or "")[:300]))
            conn.execute("UPDATE overnight_runs SET finished_epoch=?, status=?, calls_json=?, errors_json=?, "
                         "budget_hit=?, n_fresh=?, n_total=?, n_equities=?, eq_from_epoch=? WHERE id=?",
                         (fin, "done", json.dumps(calls), json.dumps(errors[:12]), 1 if budget_hit else 0,
                          n_fresh, len(n_macro), n_eq, pm_from if want_eq else None, run_id))
    finally:
        conn.close()
    out = {"ok": True, "run_id": run_id, "slot": slot["key"], "calls": calls, "errors": errors,
           "budget_hit": budget_hit, "fresh": n_fresh, "of": len(n_macro), "equities": n_eq,
           "seconds": round(_mono() - t0, 1)}
    try:
        if not manual:
            out["alerts"] = check_alerts(night, now=now, c=c)
    except Exception as e:
        log.warning(f"[overnight] alerts failed: {e}")
    try:
        if slot["label"] in ("weekend_open", "20:00"):
            _prune(c)
    except Exception:
        pass
    log.info(f"[overnight] {slot['key']}: {n_fresh}/{len(n_macro)} market quotes fresh, {n_eq} stocks, "
             f"calls {calls}, {out['seconds']} s" + (" (time limit reached)" if budget_hit else "")
             + (f"; {len(errors)} problem(s): {'; '.join(errors[:3])}" if errors else ""))
    return out


def _prune(c: dict) -> None:
    keep = max(14, int(c.get("keep_days") or 120))
    cutoff = (_et(_now()).date() - timedelta(days=keep)).isoformat()
    conn = _conn()
    try:
        with conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM overnight_runs WHERE target_day < ?", (cutoff,))]
            for i in ids:
                conn.execute("DELETE FROM overnight_quotes WHERE run_id=?", (i,))
            conn.execute("DELETE FROM overnight_runs WHERE target_day < ?", (cutoff,))
    finally:
        conn.close()


# ── reading a night back ────────────────────────────────────────────────────────

def _runs(target: date, *, include_manual: bool = False, until: float | None = None) -> list:
    rows = _rows("SELECT * FROM overnight_runs WHERE target_day=? AND status='done' "
                 + ("" if include_manual else "AND COALESCE(manual,0)=0 ")
                 + "ORDER BY slot_ts, started_epoch", (target.isoformat(),))
    if until is not None:
        rows = [r for r in rows if float(r.get("started_epoch") or 0) <= until]
    return rows


def _quotes(run_id: str) -> dict:
    return {r["key"]: r for r in _rows("SELECT * FROM overnight_quotes WHERE run_id=?", (run_id,))}


def _pct(a, b):
    a, b = _num(a), _num(b)
    if a is None or not b:
        return None
    return (a / b - 1.0) * 100.0


def night_view(target: date | None = None, *, now: float | None = None, include_manual: bool = False,
               c: dict | None = None) -> dict:
    """Everything known about one night, computed from what is stored (no network). Never raises."""
    try:
        c = c or cfg()
        now = _now() if now is None else float(now)
        night = night_for_target(target, c) if target else last_night(_et(now), c)
        runs = _runs(night["target"], include_manual=include_manual, until=now)
        out = {"night": night, "runs": runs, "latest": None, "quotes": {}, "q2000": {}, "now": now}
        if not runs:
            return out
        latest = runs[-1]
        out["latest"] = latest
        out["quotes"] = _quotes(latest["id"])
        r8 = [r for r in runs if r.get("slot_label") == "20:00"]
        if r8:
            out["q2000"] = _quotes(r8[-1]["id"])
        return out
    except Exception as e:
        log.info(f"[overnight] night view failed: {e}")
        return {"night": None, "runs": [], "latest": None, "quotes": {}, "q2000": {}, "error": str(e)}


def _fmt(v, dp=2, unit="") -> str:
    x = _num(v)
    if x is None:
        return "?"
    s = f"{x:,.{dp}f}"
    return f"${s}" if unit == "$" else (f"{s}%" if unit == "%" else s)


def _sp(p) -> str:
    return "?" if p is None else f"{p:+.2f}%"


def _day_name(d: date) -> str:
    return d.strftime("%A")


def _close_words(d: date) -> str:
    h, m = close_hm(d)
    return f"{_day_name(d)}'s {'1 pm' if (h, m) == (13, 0) else '4 pm'} close"


def _shares(v) -> str:
    x = _num(v)
    if x is None or x <= 0:                    # a stock that traded has volume; 0 means the source did not say
        return "volume not available"
    if x >= 1e6:
        return f"{x / 1e6:.1f} million shares"
    if x >= 1e4:
        return f"{x / 1e3:.0f} thousand shares"
    return f"{x:,.0f} shares"


def instrument_facts(view: dict, c: dict | None = None) -> list:
    """Per instrument: the numbers the text and the API show. A quote that is not fresh has no 'now' value."""
    c = c or cfg()
    out = []
    q, q8 = view.get("quotes") or {}, view.get("q2000") or {}
    for inst in INSTRUMENTS:
        r = q.get(inst["key"])
        f = {"key": inst["key"], "label": inst["label"], "symbol": inst["yf"], "kind": inst["kind"],
             "status": (r or {}).get("status") or "missing", "source": (r or {}).get("source"),
             "value": None, "value_ts": None, "ref_close": None, "change_since_close_pct": None,
             "value_2000": None, "change_since_8pm_pct": None, "close_value": None, "close_day_change_pct": None,
             "prior_close": None, "note": (r or {}).get("note") or ""}
        if r:
            f["ref_close"] = _num(r.get("ref_close"))
            if r.get("status") == "ok":
                f["value"], f["value_ts"] = _num(r.get("value")), _num(r.get("value_ts"))
                f["change_since_close_pct"] = _pct(r.get("value"), r.get("ref_close"))
                f["value_2000"] = _num(r.get("value_2000"))
                f["change_since_8pm_pct"] = _pct(r.get("value"), r.get("value_2000"))
            elif r.get("status") == "closed":
                f["close_value"] = _num(r.get("value"))
                f["prior_close"] = _num(r.get("prior_close"))
                f["close_day_change_pct"] = _pct(r.get("value"), r.get("prior_close"))
        out.append(f)
    for s, label in ETFS:
        r = q.get(f"etf:{s}")
        if not r:
            continue
        f = {"key": f"etf:{s}", "label": label, "symbol": s, "kind": "fund", "status": r.get("status"),
             "source": r.get("source"), "value": None, "value_ts": None, "ref_close": _num(r.get("ref_close")),
             "change_since_close_pct": None, "value_2000": None, "change_since_8pm_pct": None,
             "close_value": None, "close_day_change_pct": None, "prior_close": None, "note": r.get("note") or ""}
        if r.get("status") == "ok":
            f["value"], f["value_ts"] = _num(r.get("value")), _num(r.get("value_ts"))
            f["change_since_close_pct"] = _pct(r.get("value"), r.get("ref_close"))
            r8 = q8.get(f"etf:{s}")
            if r8 and r8.get("status") == "ok":
                f["value_2000"] = _num(r8.get("value"))
                f["change_since_8pm_pct"] = _pct(r.get("value"), r8.get("value"))
        out.append(f)
    return out


def movers_and_stops(view: dict, c: dict | None = None) -> tuple:
    """(movers, stops_crossed) from the stored stock quotes of the latest snapshot."""
    c = c or cfg()
    q = view.get("quotes") or {}
    lo = float(c.get("movers_min_pct") or 1.0)
    movers, crossed = [], []
    for r in q.values():
        if r.get("kind") != "equity" or r.get("status") != "ok" or not _num(r.get("ref_close")):
            continue
        ch = _pct(r.get("value"), r.get("ref_close"))
        m = {"ticker": r["symbol"], "held_by": r.get("held_by"), "why": r.get("why"), "price": _num(r.get("value")),
             "price_ts": _num(r.get("value_ts")), "ref_close": _num(r.get("ref_close")), "change_pct": ch,
             "volume": (_num(r.get("volume")) or None), "stop": _num(r.get("stop")), "stop_side": r.get("stop_side"),
             "stop_who": r.get("stop_who")}
        if ch is not None and abs(ch) >= lo:
            movers.append(m)
        s, px = m["stop"], m["price"]
        if s and px and ((m["stop_side"] == "below" and px <= s) or (m["stop_side"] == "above" and px >= s)):
            m["beyond_stop_pct"] = abs(px / s - 1.0) * 100.0
            crossed.append(m)
    movers.sort(key=lambda m: (-abs(m["change_pct"]), m["ticker"]))
    crossed.sort(key=lambda m: (-m.get("beyond_stop_pct", 0), m["ticker"]))
    return movers, crossed


def _who_words(m: dict) -> str:
    bits = []
    if m.get("held_by"):
        bits.append(f"held by {m['held_by']}")
    for w in str(m.get("why") or "").split(", "):
        if w and w not in bits:
            bits.append(w)
    return "; ".join(bits)


def _instrument_line(f: dict, inst: dict, ref_day: date) -> str:
    dp, unit = inst.get("dp", 2), inst.get("unit", "")
    if f["status"] == "ok":
        if f.get("ref_close") is None:         # a live value with nothing to compare it with (e.g. one source only)
            return f"{f['label']} {_fmt(f['value'], dp, unit)}; change since the close not available"
        if inst.get("level"):                  # a yield: the level then and now, not a % of a %
            s = f"{f['label']} {_fmt(f['value'], dp, unit)} (at {_day_name(ref_day)}'s close {_fmt(f['ref_close'], dp, unit)})"
        else:
            s = f"{f['label']} {_fmt(f['value'], dp, unit)}: {_sp(f['change_since_close_pct'])} since the close"
            if f.get("change_since_8pm_pct") is not None:
                s += f", {_sp(f['change_since_8pm_pct'])} since 8 pm"
        return s
    if f["status"] == "closed":
        s = f"{f['label']} {_fmt(f['close_value'], dp, unit)} at {_day_name(ref_day)}'s close"
        if inst.get("level") and f.get("prior_close") is not None:
            s += f" ({_fmt(f['prior_close'], dp, unit)} the session before)"
        elif f.get("close_day_change_pct") is not None:
            s += f" ({_sp(f['close_day_change_pct'])} on the day)"
        return s + "; not quoted overnight"
    return f"{f['label']}: not available"


def director_lines(view: dict, c: dict | None = None) -> tuple:
    """(title, [lines]) for the morning review, or (None, []) when nothing was stored for this night."""
    c = c or cfg()
    latest = view.get("latest")
    night = view.get("night")
    if not night:
        return None, []
    if not latest:                             # (review fix R6-4) no cause is claimed: it may be a restart, the
        return ("OVERNIGHT AND PRE-MARKET",      # switch going on late or a failed fetch
                ["Not available: Mason has no overnight read stored for this morning."])
    fin_dt = _et(float(latest.get("finished_epoch") or latest.get("started_epoch")))
    as_of = fin_dt.strftime("%H:%M")
    if fin_dt.date() != night["target"]:       # (review fix R6-3) a read from the evening before says which evening
        as_of = f"{_day_name(fin_dt.date())} {as_of}"
    title = f"OVERNIGHT AND PRE-MARKET (as of {as_of} ET)"
    facts = {f["key"]: f for f in instrument_facts(view, c)}
    ref_w = _close_words(night["ref_day"])
    L = [f"Facts only. Changes are since {ref_w} and since 8 pm {_day_name(night['eve'])}. "
         "\"Not available\" means Mason has no fresh quote for it."]
    for k in ("sp500_fut", "nasdaq100_fut", "russell2000_fut", "dow_fut"):
        f = facts.get(k)
        inst = _BY_KEY[k]
        if f and f["status"] == "ok":
            L.append("- " + _instrument_line(f, inst, night["ref_day"]))
            continue
        e = facts.get(f"etf:{inst['etf']}")
        if e and e["status"] == "ok":
            s = (f"- {inst['label']}: not available; {e['label']} in overnight trading {_fmt(e['value'])}: "
                 f"{_sp(e['change_since_close_pct'])} since the close")
            if e.get("change_since_8pm_pct") is not None:
                s += f", {_sp(e['change_since_8pm_pct'])} since 8 pm"
            L.append(s)
        else:
            L.append(f"- {inst['label']}: not available")
    rd = night["ref_day"]
    v = facts.get("vix")
    if v:
        L.append("- " + _instrument_line(dict(v, label="VIX (the market's fear gauge)"), _BY_KEY["vix"], rd))
    for k in ("dollar",):
        f = facts.get(k)
        if f:
            L.append("- " + _instrument_line(f, _BY_KEY[k], rd))
    ty, tf = facts.get("ten_year"), facts.get("ten_year_fut")
    if ty or tf:
        s = "- " + _instrument_line(ty, _BY_KEY["ten_year"], rd) if ty else "- 10-year Treasury yield: not available"
        if tf and tf["status"] == "ok":
            s += (f"; 10-year Treasury futures {_sp(tf['change_since_close_pct'])} since the close"
                  " (futures up means the yield is down)")
        L.append(s)
    for k in ("crude", "gold", "bitcoin"):
        f = facts.get(k)
        if f:
            L.append("- " + _instrument_line(f, _BY_KEY[k], rd))
    # stocks: read from the pre-market (04:00) on
    eq_win = _num(latest.get("eq_from_epoch"))
    eq_from = _at(night["target"], *_hm(c.get("equities_from_et"), "04:00")).timestamp()
    if not eq_win and _num(view.get("now")) and float(view["now"]) >= eq_from:
        # (review fix R6-3) the morning reads did not happen: say the stocks were not checked rather than say nothing
        L.append(f"Pre-market stock prices: not available (no overnight read since {_et(eq_from).strftime('%H:%M')}), "
                 "so moves and stops were not checked.")
    try:
        book_err = any(str(e).startswith("book:") for e in json.loads(latest.get("errors_json") or "[]"))
    except Exception:
        book_err = False
    if eq_win and book_err:
        # (review fix R6-7) Mason could not read its positions and watchlist: "no held stock is through its stop" or
        # "nothing moved" would be a guess
        L.append("Held and watched stocks: not available (Mason could not read its positions and watchlist), so moves "
                 "and stops were not checked.")
        return title, L
    if eq_win:
        pre = eq_win >= _at(night["target"], 0, 0).timestamp()
        win = "pre-market" if pre else "after-hours and overnight"
        since = _et(eq_win).strftime("%H:%M") if pre else f"{ref_w}"
        movers, crossed = movers_and_stops(view, c)
        dc = c.get("director_block") or {}
        fresh_s = max(60.0, float(c.get("fresh_minutes") or 30) * 60)
        snap = float(latest.get("finished_epoch") or latest.get("started_epoch") or 0)

        def when(m):                           # a thin stock's last trade can be old: say when it was
            ts = m.get("price_ts")
            return f" (last trade {_et(ts).strftime('%H:%M')})" if ts and snap - ts > fresh_s else ""
        eq = [r for r in (view.get("quotes") or {}).values() if r.get("kind") == "equity"]
        if eq and not any(r.get("status") in ("ok", "no_trade", "stale") for r in eq):
            # no stock quote came back at all: saying "nothing moved" or "no stop crossed" would be a guess
            L.append(f"Stock prices in {win} trading: not available (Robinhood did not answer), so moves and stops "
                     "were not checked.")
            return title, L
        if crossed:
            L.append(f"Held stocks whose {win} price is through their stop:")
            for m in crossed[:int(dc.get("max_stops") or 4)]:
                L.append(f"- {m['ticker']} ({m['stop_who']}, stop {_fmt(m['stop'])}): {_fmt(m['price'])}{when(m)}, "
                         f"{m['beyond_stop_pct']:.1f}% {m['stop_side']} the stop; {ref_w} {_fmt(m['ref_close'])}")
        else:
            L.append(f"No held stock's {win} price is through its stop.")
        n = int(dc.get("movers") or 5)
        if movers:
            L.append(f"Largest {win} moves among held, watched and in-the-news stocks (from {ref_w}; "
                     f"shares traded since {since}):")
            for m in movers[:n]:
                L.append(f"- {m['ticker']} ({_who_words(m)}): {_fmt(m['price'])}{when(m)}, {_sp(m['change_pct'])}, "
                         f"{_shares(m['volume'])}")
        else:
            L.append(f"No held, watched or in-the-news stock has moved {float(c.get('movers_min_pct') or 1.0):g}% "
                     f"or more in {win} trading.")
    return title, L


def _fit(title: str, lines: list, max_tokens: int) -> list:
    """Drop whole lines from the end until the section fits (the director counts 4 characters a token)."""
    lines = list(lines)
    while len(lines) > 1 and len(f"=== {title} ===\n" + "\n".join(lines)) // 4 > max_tokens:
        lines.pop()
        while len(lines) > 1 and lines[-1].endswith(":"):
            lines.pop()                        # a heading with nothing left under it
    return lines


def director_block(now_et: datetime | None = None, *, c: dict | None = None) -> dict | None:
    """The section for the morning review: {'title', 'lines', 'max_tokens'} on a trading day before the 09:30
    open, else None. The caller (mason_director) checks the switch. Never raises."""
    try:
        c = c or cfg()
        if c.get("enabled") is not True:       # the snapshots are off: nothing to show, nothing added
            return None
        et = _as_et(now_et) if now_et else _et(_now())
        d = et.date()
        if not is_trading_day(d) or et >= _at(d, 9, 30):
            return None
        view = night_view(d, now=et.timestamp(), c=c)
        title, lines = director_lines(view, c)
        if not title:
            return None
        mt = int((c.get("director_block") or {}).get("max_tokens") or 400)
        return {"title": title, "lines": _fit(title, lines, mt), "max_tokens": mt}
    except Exception as e:
        log.info(f"[overnight] director block failed: {e}")
        return None


def desk_check_lines(et: datetime, *, c: dict | None = None) -> list:
    """Short form for quick checks before the open (the caller checks the switch). [] after the open, on a
    non-trading day, or when the latest read is older than desk_check_block.max_age_minutes. Never raises."""
    try:
        c = c or cfg()
        if c.get("enabled") is not True:
            return []
        et = _as_et(et)
        d = et.date()
        if not is_trading_day(d) or et >= _at(d, 9, 30):
            return []
        view = night_view(d, now=et.timestamp(), c=c)
        latest, night = view.get("latest"), view.get("night")
        if not latest or not night:
            return []
        fin = float(latest.get("finished_epoch") or latest.get("started_epoch"))
        if et.timestamp() - fin > float((c.get("desk_check_block") or {}).get("max_age_minutes") or 180) * 60:
            return []
        facts = {f["key"]: f for f in instrument_facts(view, c)}
        bits = []
        for k in ("sp500_fut", "nasdaq100_fut"):
            f = facts.get(k)
            inst = _BY_KEY[k]
            if f and f["status"] == "ok":
                bits.append(f"{inst['label']} {_sp(f['change_since_close_pct'])}")
            else:
                e = facts.get(f"etf:{inst['etf']}")
                bits.append(f"{e['label']} {_sp(e['change_since_close_pct'])}" if e and e["status"] == "ok"
                            else f"{inst['label']} not available")
        v = facts.get("vix")                   # its last close is already in the check's market lines: only a
        if v and v["status"] == "ok":          # fresh overnight value is news here
            bits.append(f"VIX {_fmt(v['value'])} ({_sp(v['change_since_close_pct'])})")
        out = [f"Overnight (as of {_et(fin).strftime('%H:%M')} ET, changes since {_close_words(night['ref_day'])}): "
               + ", ".join(bits) + "."]
        _m, crossed = movers_and_stops(view, c)
        if crossed:
            out.append("Held stocks through their stop in pre-market: " + "; ".join(
                f"{m['ticker']} {_fmt(m['price'])} against a stop of {_fmt(m['stop'])}" for m in crossed[:4]) + ".")
        return out
    except Exception as e:
        log.info(f"[overnight] quick-check lines failed: {e}")
        return []


# ── alerts (inbox, one per event) ───────────────────────────────────────────────

def _alert_once(event_key: str, *, kind: str, title: str, body: str, ticker=None) -> str | None:
    """Post one inbox note per event, ever. The event is recorded only once the note went in, so a note that
    could not be posted is tried again at the next snapshot. (Alerts run only in the scheduler thread, one
    snapshot at a time, so the check and the record cannot race.)"""
    if not ensure_schema():
        return None
    if _rows("SELECT 1 FROM overnight_alerts WHERE event_key=? LIMIT 1", (event_key,)):
        return None
    try:
        n = _notify(type="major_move", title=title[:200], body=body, severity="warn", ticker=ticker,
                    ref_table="overnight_alerts", ref_id=event_key, created_by=_CREATED_BY,
                    dedup_key=f"overnight:{event_key}")
    except Exception as e:
        log.warning(f"[overnight] could not post an alert (tried again at the next snapshot): {e}")
        return None
    nid = (n or {}).get("id") if isinstance(n, dict) else None
    conn = _conn()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO overnight_alerts (event_key, created_at, kind, ticker, title, "
                         "notification_id) VALUES (?,?,?,?,?,?)",
                         (event_key, _et(_now()).isoformat(timespec="seconds"), kind, ticker, title[:200], nid))
    finally:
        conn.close()
    return event_key


def check_alerts(night: dict, *, now: float | None = None, c: dict | None = None) -> list:
    """Inbox notes for this night's latest snapshot when overnight.alerts.enabled. Returns the new event keys."""
    c = c or cfg()
    a = c.get("alerts") or {}
    if a.get("enabled") is not True:
        return []
    now = _now() if now is None else now
    view = night_view(night["target"], now=now, c=c)
    latest = view.get("latest")
    if not latest:
        return []
    sent = []
    day = night["target"].isoformat()
    as_of = _et(float(latest.get("finished_epoch") or now)).strftime("%H:%M")
    ref_w = _close_words(night["ref_day"])
    facts = {f["key"]: f for f in instrument_facts(view, c)}
    thr = abs(float(a.get("futures_move_pct") or 1.5))
    for k in a.get("futures_keys") or []:
        f = facts.get(k)
        if not f or f["status"] != "ok" or f["change_since_close_pct"] is None:
            continue
        ch = f["change_since_close_pct"]
        if abs(ch) < thr:
            continue
        way = "up" if ch > 0 else "down"
        inst = _BY_KEY.get(k) or {}
        body = (f"At {as_of} ET the {f['label']} were at {_fmt(f['value'], inst.get('dp', 2))}, "
                f"{abs(ch):.2f}% {'above' if ch > 0 else 'below'} {ref_w} ({_fmt(f['ref_close'], inst.get('dp', 2))}).")
        if f.get("change_since_8pm_pct") is not None:
            body += f" Since 8 pm {_day_name(night['eve'])}: {_sp(f['change_since_8pm_pct'])}."
        v = facts.get("vix")
        if v and v["status"] == "ok":
            body += f" VIX {_fmt(v['value'])} ({_sp(v['change_since_close_pct'])} since the close)."
        elif v and v["status"] == "closed":
            body += f" VIX {_fmt(v['close_value'])} at the close (not quoted overnight)."
        k2 = _alert_once(f"futures:{day}:{k}:{way}", kind="futures",
                         title=f"{f['label']} {way} {abs(ch):.1f}% overnight", body=body)
        if k2:
            sent.append(k2)
    vj = float(a.get("vix_jump_pct") or 20.0)
    v = facts.get("vix")
    if v and v["status"] == "ok" and v["change_since_close_pct"] is not None and v["change_since_close_pct"] >= vj:
        k2 = _alert_once(f"vix:{day}:up", kind="vix", title=f"VIX up {v['change_since_close_pct']:.0f}% overnight",
                         body=(f"At {as_of} ET the VIX (the market's fear gauge) was {_fmt(v['value'])}, "
                               f"{v['change_since_close_pct']:.1f}% above {_day_name(night['ref_day'])}'s close "
                               f"({_fmt(v['ref_close'])})."))         # (review fix R6-5) its close is 16:15, not 4 pm
        if k2:
            sent.append(k2)
    if a.get("stop_gap", True):
        _m, crossed = movers_and_stops(view, c)
        for m in crossed:
            ids = re.findall(r"\[([^\]]+)\]", m.get("stop_who") or "") or [m["ticker"]]
            k2 = _alert_once(f"stopgap:{day}:{m['ticker']}:{'+'.join(ids)}", kind="stop_gap", ticker=m["ticker"],
                             title=f"{m['ticker']} pre-market price is {m['stop_side']} its stop",
                             body=(f"{m['ticker']} traded at {_fmt(m['price'])} in pre-market "
                                   f"({_et(m['price_ts']).strftime('%H:%M')} ET), {m['beyond_stop_pct']:.1f}% "
                                   f"{m['stop_side']} the stop of {_fmt(m['stop'])} ({m['stop_who']}). "
                                   f"{ref_w}: {_fmt(m['ref_close'])}. Pre-market volume since 04:00: "
                                   f"{_shares(m['volume'])}. This note does not change any order."))
            if k2:
                sent.append(k2)
    return sent


# ── health (mason_health 'overnight_tape'; no network) ──────────────────────────

def feed_health(now: float | None = None, c: dict | None = None) -> dict:
    """ok unless the most recent scheduled read did not run or brought no S&P 500 read (futures or SPY). Nights
    with nothing scheduled (Friday night, Saturday, before a holiday) and the daytime are quiet, not down."""
    try:
        c = c or cfg()
        if c.get("enabled") is not True:
            return {"ok": True, "latency_ms": None, "detail": "switched off (overnight.enabled is false)"}
        now = _now() if now is None else float(now)
        grace = max(60.0, float(c.get("grace_minutes") or 20) * 60)
        et = _et(now)
        last_due, night = None, None
        n = night_for(et, c) or last_night(et, c)
        for _ in range(6):
            past = [(lab, dt) for lab, dt in slots_for(n, c) if dt.timestamp() + grace + 300 <= now]
            if past:
                last_due, night = past[-1], n
                break
            n = night_for_target(prev_trading_day(n["target"]), c)
        if not last_due:
            return {"ok": True, "latency_ms": None, "detail": "no overnight read due yet"}
        lab, dt = last_due
        key = slot_key(night, lab)
        run = _rows("SELECT * FROM overnight_runs WHERE slot_key=? LIMIT 1", (key,))
        quiet = "" if night_for(et, c) else " (quiet now: nothing scheduled until the next trading day's eve)"
        on_since = _state.get("on_since")
        if not run and on_since and dt.timestamp() < float(on_since) - 60:
            # that slot came due before Mason started or before the switch went on: nothing to judge yet
            return {"ok": True, "latency_ms": None,
                    "detail": f"no overnight read has come due since Mason started or the switch went on "
                              f"(the last one was {dt.strftime('%a %H:%M')} ET)"}
        if not run:
            anyrun = _rows("SELECT 1 FROM overnight_runs WHERE COALESCE(manual,0)=0 AND started_epoch >= ? LIMIT 1",
                           (now - 4 * 86400,))
            if not anyrun:
                return {"ok": True, "latency_ms": None, "detail": "not run yet since it was switched on"}
            return {"ok": False, "latency_ms": None,
                    "detail": f"the {dt.strftime('%a %H:%M')} ET overnight read did not run (is the overnight job "
                              f"running? see background_jobs)"}
        r = run[0]
        q = _quotes(r["id"])
        sp = q.get("sp500_fut") or {}
        spy = q.get("etf:SPY") or {}
        core = sp.get("status") == "ok" or spy.get("status") == "ok"
        detail = (f"{dt.strftime('%a %H:%M')} ET read: {r.get('n_fresh') or 0} of {r.get('n_total') or 0} market "
                  f"quotes fresh, {r.get('n_equities') or 0} stocks"
                  + ("; time limit reached" if r.get("budget_hit") else "") + quiet)
        if r.get("status") != "done":
            return {"ok": False, "latency_ms": None, "detail": f"{dt.strftime('%a %H:%M')} ET read did not finish"}
        if not core:
            errs = json.loads(r.get("errors_json") or "[]")
            return {"ok": False, "latency_ms": None,
                    "detail": f"{dt.strftime('%a %H:%M')} ET read has no S&P 500 quote (Yahoo and Robinhood both "
                              f"failed or stale)" + (f": {errs[0][:140]}" if errs else "")}
        if sp.get("status") != "ok":
            detail += "; S&P 500 futures not available, using SPY overnight"
        return {"ok": True, "latency_ms": None, "detail": detail}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "detail": f"overnight status unreadable: {type(e).__name__}"}


# ── HTTP (GET /api/overnight, read-only) ────────────────────────────────────────

def _iso(ts) -> str | None:
    x = _num(ts)
    return _et(x).isoformat(timespec="seconds") if x else None


def http_get(args=None) -> tuple:
    """(payload, status) for GET /api/overnight[?day=YYYY-MM-DD][&manual=1]. Reads stored snapshots only; never
    fetches, never writes. manual=1 also counts snapshots taken by hand. See docs/fable/OVERNIGHT_API.md."""
    args = args or {}
    try:
        c = cfg()
        day = str(args.get("day") or "").strip()
        target = None
        if day:
            try:
                target = date.fromisoformat(day)
            except ValueError:
                return {"ok": False, "error": "day must look like 2026-11-17", "code": "bad_day"}, 400
            if not is_trading_day(target):
                return {"ok": False, "error": f"{day} is not a trading day; the overnight read belongs to the "
                                             "trading day it leads into", "code": "not_trading_day"}, 400
        manual = str(args.get("manual") or "").strip().lower() in ("1", "true", "yes")
        view = night_view(target, c=c, include_manual=manual)
        night = view.get("night")
        if not night:
            return {"ok": False, "error": "Mason could not read the overnight snapshots just now.",
                    "code": "server_error"}, 500
        facts = instrument_facts(view, c)
        movers, crossed = movers_and_stops(view, c)
        latest = view.get("latest")
        series = {}
        for r in view.get("runs") or []:
            for k, q in _quotes(r["id"]).items():
                if q.get("kind") in ("macro", "etf") and q.get("status") == "ok":
                    series.setdefault(k, []).append({"slot": r.get("slot_label"), "at": _iso(q.get("value_ts")),
                                                     "value": _num(q.get("value"))})
        def rnd(d):
            for k, v in list(d.items()):
                if isinstance(v, float):
                    d[k] = round(v, 3 if k.endswith("_pct") else 4)
            return d
        for f in facts:
            f["value_time"] = _iso(f.pop("value_ts", None))
            rnd(f)
        for m in movers + crossed:
            if "price_ts" in m:
                m["price_time"] = _iso(m.pop("price_ts"))
            rnd(m)
        payload = {
            "ok": True, "enabled": c.get("enabled") is True,
            "switches": {"snapshots": c.get("enabled") is True,
                         "director_block": (c.get("director_block") or {}).get("enabled") is True,
                         "desk_check_block": (c.get("desk_check_block") or {}).get("enabled") is True,
                         "alerts": (c.get("alerts") or {}).get("enabled") is True},
            "night": {"target_day": night["target"].isoformat(), "eve_day": night["eve"].isoformat(),
                      "prior_close_day": night["ref_day"].isoformat(),
                      "prior_close_time": f"{close_hm(night['ref_day'])[0]:02d}:00",
                      "starts": night["start"].isoformat(), "ends": night["end"].isoformat(),
                      "slots": [{"label": lab, "at": dt.isoformat()} for lab, dt in slots_for(night, c)]},
            "as_of": _iso((latest or {}).get("finished_epoch")), "slot": (latest or {}).get("slot_label"),
            "instruments": [f for f in facts if f["kind"] != "fund"],
            "funds_overnight": [f for f in facts if f["kind"] == "fund"],
            "movers": movers[:20], "stops_crossed": crossed,
            "runs": [{"slot": r.get("slot_label"), "manual": bool(r.get("manual")),
                      "started": _iso(r.get("started_epoch")),
                      "finished": _iso(r.get("finished_epoch")), "fresh": r.get("n_fresh"), "of": r.get("n_total"),
                      "stocks": r.get("n_equities"), "time_limit_reached": bool(r.get("budget_hit")),
                      "calls": json.loads(r.get("calls_json") or "{}"),
                      "problems": json.loads(r.get("errors_json") or "[]")} for r in view.get("runs") or []],
            "series": series,
            "health": feed_health(c=c),
        }
        return payload, 200
    except Exception as e:
        log.warning(f"[overnight] /api/overnight failed: {e}")
        return {"ok": False, "error": "Mason could not read the overnight snapshots just now.",
                "code": "server_error"}, 500


# ── the background thread ───────────────────────────────────────────────────────

def _beat(busy_with: str | None = None) -> None:
    try:
        import mason_runtime
        if busy_with:
            mason_runtime.busy(_THREAD, busy_with)
        else:
            mason_runtime.beat(_THREAD)
    except Exception:
        pass


def tick(now: float | None = None) -> dict | None:
    """One scheduler pass: run the due slot, if any. Never raises."""
    try:
        now = _now() if now is None else now
        if not enabled():
            _state["on_since"] = None
            return None
        if not _state.get("on_since"):        # health judges only slots due while this process had the switch on
            _state["on_since"] = now
        c = cfg()
        d = due_slot(now, c, done=_slot_done)
        if not d:
            return None
        _beat(f"overnight read {d['label']}")
        return run_snapshot(d, now=now, c=c)
    except Exception as e:
        _state["last_error"] = f"{type(e).__name__}: {e}"
        log.warning(f"[overnight] tick failed: {e}")
        return None


def _loop(tick_seconds: float = 30.0) -> None:
    while True:
        _beat()
        _state["last_tick"] = _now()
        tick()
        _beat()
        time.sleep(tick_seconds)


def _start_thread() -> threading.Thread:
    th = threading.Thread(target=_loop, args=(30.0,), daemon=True, name=_THREAD)
    th.start()
    _state["thread"] = th
    return th


def start() -> bool:
    """Start the scheduler thread (idempotent) and register it with the runtime supervisor. The thread checks the
    switch on every pass, so overnight.enabled takes effect without a restart. Never raises."""
    try:
        if _state["started"]:
            return False
        _state["started"] = True
        _start_thread()
        try:
            import mason_runtime
            mason_runtime.register(_THREAD, label="overnight and pre-market read", interval=30, stale_after=15 * 60,
                                   restart=_start_thread, should_run=lambda: enabled())
        except Exception as e:
            log.info(f"[overnight] not registered with the runtime supervisor: {e}")
        log.info("[overnight] scheduler started (snapshots " + ("on" if enabled() else "off: overnight.enabled false")
                 + ")")
        return True
    except Exception as e:
        log.warning(f"[overnight] could not start: {e}")
        return False


# ── command line ────────────────────────────────────────────────────────────────

def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mason's overnight and pre-market read")
    ap.add_argument("cmd", choices=["status", "block", "snapshot"])
    ap.add_argument("--db", help="database file (default: the live mason.db)")
    ap.add_argument("--day", help="block/status: the trading day the night leads into (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if a.db:
        mason_db.DB_FILE = Path(a.db)
        mason_db.reset_schema_cache()
    if a.cmd == "snapshot":
        print(json.dumps(run_snapshot(), indent=1, default=str))
        return 0
    if a.cmd == "status":
        print(json.dumps(feed_health(), indent=1))
        view = night_view(date.fromisoformat(a.day) if a.day else None, include_manual=True)
        for r in view.get("runs") or []:
            print(f"  {r['slot_key']:<34} {r['status']:<8} fresh {r.get('n_fresh')}/{r.get('n_total')} "
                  f"stocks {r.get('n_equities')} calls {r.get('calls_json')}")
        return 0
    view = night_view(date.fromisoformat(a.day) if a.day else None, include_manual=True)
    title, lines = director_lines(view)
    if title:
        mt = int((cfg().get("director_block") or {}).get("max_tokens") or 400)
        print(f"=== {title} ===\n" + "\n".join(_fit(title, lines, mt)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
