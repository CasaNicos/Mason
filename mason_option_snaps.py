"""
mason_option_snaps.py — a few read-only snapshots of option prices a day (audit R6 / L7, 2026-11-16).

Why
---
Options monitoring was off: `watch_option_snaps` has 0 rows because `watchlist.options.enabled` is false in
mason.config.json (mason_watch_tape._options_tick idles). That old snapper reads Alpaca's free "indicative" option
quotes (yfinance as fallback), stores only one at-the-money pair per name every 15 minutes and never looks at the
contracts Mason or Nico actually hold. Its rows also feed model-visible text (the watchlist chat line
"Options (...): IV .., implied move ..", watch digests, trade stories), so filling that table would change what the
models read; that needs Nico's approval. This module therefore writes its OWN tables and leaves the old snapper and
its table exactly as they are.

What it does
------------
On trading days, at the times in option_snaps.slots_et (default 09:45, 12:30, 15:45 ET; on a 13:00 half day a time
after 12:45 moves to 12:45 and is dropped when another time is within 30 minutes), one pass:
  (a) every option contract held in the Alpaca paper account (Mason's and Nico's) and, when
      option_snaps.include_robinhood_holdings is on, every option in Nico's in-scope Robinhood accounts:
      bid, ask, mid, mark, volume, open interest, implied volatility, delta/gamma/theta/vega, the stock price
      -> table opt_snap_contracts.  (Robinhood gives no last-trade price for options; `last` stays empty and
      yesterday's close is stored instead.)
  (b) for each held stock, the day's top catalyst names and the watched names (in that order, capped at
      option_snaps.max_names), the at-the-money straddle for (1) the nearest expiry at least min_dte days out and
      (2) the nearest standard monthly expiry at least min_dte days out (one row when they are the same date):
      straddle mid / stock price = the market's expected move to that expiry, plus the at-the-money implied
      volatility -> table opt_snap_straddles.
Every Robinhood read goes through mason_robinhood._call on the "bulk" lane (the lane meant for sweeps), at most
option_snaps.max_calls_per_pass calls a pass and max_calls_per_day a day, each call waited for at most
call_timeout_s, the whole pass time-boxed to pass_time_box_s, and the pass stops at once when Robinhood answers
"too many requests". Never inside option_snaps.blackout_et (default 02:15-06:30 ET: the nightly backup and grader).

Using the data (nothing reads it for a decision yet)
--------------------------------------------------
  implied_move(ticker, as_of=None, which="near")  -> dict or None (None when the newest snapshot at or before as_of
                                                    is older than option_snaps.stale_after_minutes, or quotes were
                                                    too wide). Never looks past as_of (safe for the grader).
  implied_move_pct(ticker, as_of=None, ...)       -> float or None
  http_get(args)                                  -> GET /api/options/snaps (docs/fable/OPTSNAP_API.md)
  health()                                        -> mason_health check "option_snaps"

Settings (mason.config.json "option_snaps", all optional, read on every pass; defaults in DEFAULTS below).
Nothing here places, changes or cancels an order, calls a model, or writes outside its three tables.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("mason.option_snaps")

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
ET = ZoneInfo("America/New_York")

DEFAULTS = {
    "enabled": False,                 # review 2026-11-16: ships OFF (audit rule); read-only, ~120-180 Robinhood
                                      # reads a day once switched on (see OPTSNAP_FOR_NICO.md)
    "slots_et": ["09:45", "12:30", "15:45"],
    "slot_grace_minutes": 60,         # a slot missed (e.g. Mason restarting) still runs this late, never after the close
    "retry_minutes": 15,              # a failed slot is tried again after this long ...
    "max_attempts_per_slot": 2,       # ... at most this many times in all
    "close_buffer_minutes": 15,       # no pass in the last 15 minutes of the session
    "half_days": [],                  # extra 13:00-close days (added to the built-in list)
    "min_dte": 5,                     # straddle expiries at least this many calendar days out
    "monthly": True,                  # also the nearest standard monthly expiry
    "max_names": 30,                  # straddle names per pass (held first, then catalysts, then watched)
    "max_catalyst_names": 5,
    "catalyst_min_score": 0.75,
    "catalyst_lookback_hours": 24,
    "include_watchlist": True,
    "include_robinhood_holdings": True,
    "max_calls_per_pass": 200,
    "max_calls_per_day": 600,
    "pass_time_box_s": 300,
    "call_timeout_s": 20,
    "min_call_gap_s": 0.6,            # this module never asks Robinhood more than ~1.7 times a second
    "max_quote_age_s": 900,           # a quote older than this at snapshot time is not used for a straddle
    "wide_spread_pct": 35.0,          # a straddle leg wider than this (of its mid) marks the row 'wide'
    "stale_after_minutes": 240,       # implied_move(): a snapshot older than this (at as_of) is not used
    "max_instrument_pages": 5,
    "blackout_et": ["02:15-06:30"],   # nightly backup 02:30 and the grader 03:15-06:15 ET
}

# 13:00 ET closes (same list as mason_desk_pipeline._NYSE_HALF_DAYS; config option_snaps.half_days adds more)
_HALF_DAYS = {"2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26"}
_SYM_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_OCC_RE = re.compile(r"^([A-Z]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_THREAD_NAME = "option-snaps"
_MIN_SLOT_GAP_MIN = 30
_LAST_START_BEFORE_CLOSE_MIN = 5      # a due pass may still start until 5 minutes before the close

SCHEMA = """
CREATE TABLE IF NOT EXISTS opt_snap_straddles (
    ticker            TEXT NOT NULL,
    date_et           TEXT NOT NULL,             -- trading day (ET) of the pass
    slot              TEXT NOT NULL,             -- 'HH:MM' ET label of the pass
    expiry            TEXT NOT NULL,
    ts                INTEGER NOT NULL,          -- epoch seconds when the quotes were read
    is_near           INTEGER NOT NULL DEFAULT 0,   -- nearest expiry >= min_dte days out
    is_monthly        INTEGER NOT NULL DEFAULT 0,   -- nearest standard monthly >= min_dte days out
    dte               INTEGER,
    strike            REAL,
    underlying        REAL,                      -- stock price used
    call_id           TEXT, put_id TEXT,
    call_bid REAL, call_ask REAL, call_mid REAL, put_bid REAL, put_ask REAL, put_mid REAL,
    straddle_mid      REAL,
    implied_move_pct  REAL,                      -- straddle_mid / underlying * 100 (move priced in to expiry)
    call_iv REAL, put_iv REAL,
    atm_iv            REAL,                      -- fraction (0.49 = 49%), mean of the two legs
    call_oi REAL, put_oi REAL, call_volume REAL, put_volume REAL,
    quote_ts          INTEGER,                   -- older of the two legs' quote times
    quality           TEXT,                      -- ok | wide
    source            TEXT DEFAULT 'robinhood',
    PRIMARY KEY (ticker, date_et, slot, expiry)
);
CREATE INDEX IF NOT EXISTS idx_opt_snap_straddles_t ON opt_snap_straddles(ticker, ts DESC);
CREATE TABLE IF NOT EXISTS opt_snap_contracts (
    instrument_id     TEXT NOT NULL,             -- Robinhood option instrument id
    date_et           TEXT NOT NULL,
    slot              TEXT NOT NULL,
    ts                INTEGER NOT NULL,
    occ               TEXT,                      -- OCC symbol when the contract is held at Alpaca
    underlying        TEXT, expiry TEXT, strike REAL, type TEXT,
    held_at           TEXT,                      -- alpaca_paper | robinhood
    owner             TEXT,                      -- journal created_by when known (mason / nico ...)
    qty               REAL,
    bid REAL, ask REAL, mid REAL, mark REAL, last REAL, prev_close REAL,
    bid_size REAL, ask_size REAL, volume REAL, open_interest REAL,
    iv REAL,                                     -- fraction
    delta REAL, gamma REAL, theta REAL, vega REAL,
    underlying_px     REAL,
    quote_ts          INTEGER,
    source            TEXT DEFAULT 'robinhood',
    PRIMARY KEY (instrument_id, date_et, slot)
);
CREATE INDEX IF NOT EXISTS idx_opt_snap_contracts_u ON opt_snap_contracts(underlying, ts DESC);
CREATE TABLE IF NOT EXISTS opt_snap_passes (
    date_et TEXT NOT NULL, slot TEXT NOT NULL, attempt INTEGER NOT NULL,
    started_ts INTEGER, finished_ts INTEGER,
    status TEXT,                                 -- ok | partial | failed
    reason TEXT, calls INTEGER, n_contracts INTEGER, n_straddles INTEGER, n_names INTEGER,
    PRIMARY KEY (date_et, slot, attempt)
);
"""


# ── config ─────────────────────────────────────────────────────────────────────

_cfg_cache = {"mtime": None, "data": {}}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime_ns
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8")) or {}
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"]
    except Exception:
        return _cfg_cache["data"] or {}


def cfg(raw: dict | None = None) -> dict:
    """DEFAULTS overlaid with mason.config.json "option_snaps" (or `raw`, a whole config dict, in tests)."""
    out = json.loads(json.dumps(DEFAULTS))
    try:
        src = ((raw if raw is not None else _config()) or {}).get("option_snaps") or {}
        for k, v in src.items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def enabled(c: dict | None = None) -> bool:
    return bool((c or cfg()).get("enabled"))


# ── hooks (tests replace these) ────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _clock() -> float:
    return time.monotonic()


def _sleep(s: float) -> None:
    time.sleep(s)


def _rh_call(tool: str, args: dict):
    import mason_robinhood
    return mason_robinhood._call(tool, args, lane="bulk")


def _rh_enabled() -> bool:
    try:
        import mason_robinhood
        return bool(mason_robinhood.enabled())
    except Exception:
        return False


def _rh_rate_hits() -> int:
    try:
        import mason_robinhood
        return int(mason_robinhood._state.get("rate_hits") or 0)
    except Exception:
        return 0


def _rh_total_calls() -> int:
    """mason_robinhood's own count of every Robinhood call made by this process (all threads)."""
    try:
        import mason_robinhood
        return int(mason_robinhood._state.get("calls") or 0)
    except Exception:
        return 0


def _is_signed_out(e) -> bool:
    """Robinhood refused for a reason every later call shares (signed out / outside the allowlist): stop the pass
    instead of trying the next 30 names (each would try to reconnect)."""
    try:
        import mason_robinhood
        return isinstance(e, (mason_robinhood.RHNeedsLogin, mason_robinhood.RHForbidden))
    except Exception:
        return type(e).__name__ in ("RHNeedsLogin", "RHForbidden")


def _is_rate_limit(e) -> bool:
    try:
        import mason_robinhood
        return mason_robinhood._retry_kind(e) == "429"
    except Exception:
        m = str(e).lower()
        return "too many requests" in m or "rate limit" in m


def _alpaca_positions():
    """Raw Alpaca paper positions, or None when the read failed (read-only GET /v2/positions)."""
    import mason_execution
    r = mason_execution.positions()
    return r.get("positions") if r.get("ok") else None


def _rh_positions() -> list:
    import mason_robinhood
    return mason_robinhood.in_scope_positions()


def _watched() -> list:
    import mason_watchlist
    if not mason_watchlist.enabled():
        return []
    return [it.get("ticker") for it in mason_watchlist.list_items("active") or []]


def _db():
    import mason_db
    conn = mason_db.get_connection()
    _ensure_schema(conn)
    return conn


def _trading_day(d: date) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=ET)))
    except Exception:
        return d.weekday() < 5


# ── small helpers ──────────────────────────────────────────────────────────────

_schema_done: set = set()


def _ensure_schema(conn) -> None:
    try:
        key = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        key = None
    if key and key in _schema_done:
        return
    conn.executescript(SCHEMA)
    if key:
        _schema_done.add(key)


def _f(v):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _iv_frac(v):
    """Implied volatility as a fraction (0.49 = 49%). Robinhood's get_option_quotes always sends a fraction
    ("0.487939"; docs/robinhood/finalization/E34_SHAPES.md), and a contract a few days from expiry can really be
    above 5 (500%), so the value is stored as sent (review 2026-11-16: the earlier "above 5 = percent" rule turned a
    real 7.5 into 0.075). None / <= 0 -> None."""
    x = _f(v)
    if x is None or x <= 0:
        return None
    return x


def parse_ts(s):
    """ISO-8601 / RFC3339Nano text -> epoch seconds, or None."""
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


def _mid(bid, ask):
    b, a = _f(bid), _f(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return round((a + b) / 2.0, 4)


def _hm(s, default=None):
    try:
        hh, mm = str(s).strip().split(":")[:2]
        v = int(hh) * 60 + int(mm)
        return v if 0 <= v < 24 * 60 else default
    except Exception:
        return default


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _et_at(d: date, minutes: int) -> float:
    return datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).timestamp()


def parse_occ(occ):
    """'WMT261218C00110000' (or the padded 'WMT   261218C00110000') -> {underlying, expiry, type, strike} or None."""
    m = _OCC_RE.match(str(occ or "").upper().strip())
    if not m:
        return None
    try:
        exp = date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4))).isoformat()
    except ValueError:
        return None
    return {"underlying": m.group(1), "expiry": exp, "type": "call" if m.group(5) == "C" else "put",
            "strike": int(m.group(6)) / 1000.0}


# ── schedule ───────────────────────────────────────────────────────────────────

def is_half_day(d: date, c: dict | None = None) -> bool:
    extra = {str(x)[:10] for x in ((c or cfg()).get("half_days") or []) if x}
    return d.isoformat() in (_HALF_DAYS | extra)


def close_minutes(d: date, c: dict | None = None) -> int:
    return 13 * 60 if is_half_day(d, c) else 16 * 60


def slots_for(d: date, c: dict | None = None) -> list:
    """[(label 'HH:MM', minutes after midnight ET)] for trading day `d` ([] on a weekend / holiday).
    A time later than (close - close_buffer_minutes) moves to that time; a moved time is dropped when another
    time is within 30 minutes of it. Times before 09:31 are dropped (no quotes before the open)."""
    c = c or cfg()
    if not _trading_day(d):
        return []
    latest = close_minutes(d, c) - int(c.get("close_buffer_minutes") or 0)
    fixed, moved = [], []
    for s in c.get("slots_et") or []:
        m = _hm(s)
        if m is None or m < 9 * 60 + 31:
            continue
        (moved if m > latest else fixed).append(min(m, latest))
    out = sorted(set(fixed))
    for m in sorted(set(moved)):
        if all(abs(m - x) >= _MIN_SLOT_GAP_MIN for x in out):
            out.append(m)
    return [(f"{m // 60:02d}:{m % 60:02d}", m) for m in sorted(out)]


def _in_blackout(et: datetime, c: dict) -> bool:
    hm = et.hour * 60 + et.minute
    for w in c.get("blackout_et") or []:
        a, _, b = str(w).partition("-")
        lo, hi = _hm(a), _hm(b)
        if lo is None or hi is None:
            continue
        if (lo <= hm < hi) if lo <= hi else (hm >= lo or hm < hi):
            return True
    return False


def due_slot(now: float, passes: dict, c: dict | None = None):
    """The slot label to run at `now`, or None. `passes` = {slot: [{"status", "started_ts"}...]} for today.
    Due when: switched on, a trading day, the slot time has come but is not older than slot_grace_minutes,
    not in a blackout window, the slot has no ok/partial pass
    yet, fewer than max_attempts_per_slot attempts, and retry_minutes since the last failed attempt. No pass starts in
    the last 5 minutes of the session."""
    c = c or cfg()
    if not enabled(c):
        return None
    et = _et(now)
    d = et.date()
    if _in_blackout(et, c):
        return None
    hm_now = (now - _et_at(d, 0)) / 60.0
    if hm_now >= close_minutes(d, c) - _LAST_START_BEFORE_CLOSE_MIN:
        return None
    grace = float(c.get("slot_grace_minutes") or 60)
    for label, m in slots_for(d, c):
        if not (m <= hm_now < m + grace):
            continue
        tries = passes.get(label) or []
        if any(t.get("status") in ("ok", "partial") for t in tries):
            continue
        if len(tries) >= int(c.get("max_attempts_per_slot") or 2):
            continue
        last = max((t.get("started_ts") or 0 for t in tries), default=0)
        if last and now - last < float(c.get("retry_minutes") or 15) * 60:
            continue
        return label
    return None


# ── expiry and strike choice (pure) ────────────────────────────────────────────

def is_monthly(d: date) -> bool:
    """A standard monthly expiry: the third Friday, or the Thursday before it when that Friday is a market holiday
    (e.g. 2027-06-17 for Juneteenth 2027-06-18)."""
    if d.weekday() == 4 and 15 <= d.day <= 21:
        return True
    nxt = d + timedelta(days=1)
    return d.weekday() == 3 and nxt.weekday() == 4 and 15 <= nxt.day <= 21 and not _trading_day(nxt)


def choose_expiries(expiries, today: date, min_dte: int, monthly: bool = True) -> dict:
    """{"near": 'YYYY-MM-DD' | None, "monthly": 'YYYY-MM-DD' | None} from a chain's expiration dates."""
    ds = []
    for e in expiries or []:
        try:
            dd = date.fromisoformat(str(e)[:10])
        except ValueError:
            continue
        if (dd - today).days >= int(min_dte):
            ds.append(dd)
    ds = sorted(set(ds))
    near = ds[0].isoformat() if ds else None
    mon = next((x.isoformat() for x in ds if is_monthly(x)), None) if monthly else None
    return {"near": near, "monthly": mon}


def pick_atm(instruments, expiry: str, underlying: float):
    """(strike, call_id, put_id) of the strike nearest the stock price that has BOTH an active standard (x100) call and
    put on `expiry`; ties go to the lower strike. None when no strike has both sides."""
    if not underlying or underlying <= 0:
        return None
    by_k: dict = {}
    for r in instruments or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if str(r.get("expiration_date") or "")[:10] != expiry or r.get("type") not in ("call", "put"):
            continue
        if r.get("state") not in (None, "active"):
            continue
        mult = _f(r.get("trade_value_multiplier"))
        if mult is not None and abs(mult - 100.0) > 1e-6:
            continue
        k = _f(r.get("strike_price"))
        if k is None:
            continue
        by_k.setdefault(round(k, 4), {}).setdefault(r["type"], r["id"])
    both = [k for k, v in by_k.items() if "call" in v and "put" in v]
    if not both:
        return None
    k = min(both, key=lambda s: (abs(s - underlying), s))
    return k, by_k[k]["call"], by_k[k]["put"]


def parse_option_quote(r) -> dict | None:
    """One get_option_quotes result -> plain numbers (iv as a fraction), or None without an instrument id."""
    if not isinstance(r, dict):
        return None
    q = r.get("quote") if isinstance(r.get("quote"), dict) else r
    iid = q.get("instrument_id") or q.get("id")
    if not iid:
        return None
    close = r.get("close") if isinstance(r.get("close"), dict) else {}
    bid, ask = _f(q.get("bid_price")), _f(q.get("ask_price"))
    return {"instrument_id": iid, "bid": bid, "ask": ask, "mid": _mid(bid, ask),
            "mark": _f(q.get("mark_price")) or _f(q.get("adjusted_mark_price")),
            "last": _f(q.get("last_trade_price")),          # Robinhood does not send one today; kept if it ever does
            "prev_close": _f(close.get("price")) or _f(q.get("previous_close_price")),
            "bid_size": _f(q.get("bid_size")), "ask_size": _f(q.get("ask_size")),
            "volume": _f(q.get("volume")), "open_interest": _f(q.get("open_interest")),
            "iv": _iv_frac(q.get("implied_volatility")), "delta": _f(q.get("delta")),
            "gamma": _f(q.get("gamma")), "theta": _f(q.get("theta")), "vega": _f(q.get("vega")),
            "quote_ts": parse_ts(q.get("updated_at"))}


def parse_equity_quote(r, now: float) -> tuple:
    """(symbol, price, ts): the newer of the regular-session and extended-hours last trade that is not in the
    future; (symbol, None, None) when neither is usable."""
    q = (r or {}).get("quote") if isinstance((r or {}).get("quote"), dict) else (r or {})
    sym = str(q.get("symbol") or "").upper()
    best = (None, None)
    for px, t in ((q.get("last_trade_price"), q.get("venue_last_trade_time")),
                  (q.get("last_non_reg_trade_price"), q.get("venue_last_non_reg_trade_time"))):
        p, ts = _f(px), parse_ts(t)
        if p and p > 0 and ts and ts <= now + 10 and (best[1] is None or ts > best[1]):
            best = (p, ts)
    return sym, best[0], best[1]


def build_straddle(ticker, expiry, strike, und, cq, pq, *, today: date, now: float, c: dict):
    """A straddle row from the two parsed leg quotes, or (None, reason)."""
    if not cq or not pq:
        return None, "a leg had no quote"
    max_age = float(c.get("max_quote_age_s") or 900)
    for q in (cq, pq):
        if q.get("mid") is None:
            return None, "a leg had no two-sided quote"
        if q.get("quote_ts") is None or now - q["quote_ts"] > max_age:
            return None, "a leg's quote was stale"
    straddle = cq["mid"] + pq["mid"]
    wide_lim = float(c.get("wide_spread_pct") or 35.0)
    wide = any((q["ask"] - q["bid"]) / q["mid"] * 100.0 > wide_lim for q in (cq, pq))
    ivs = [x for x in (cq.get("iv"), pq.get("iv")) if x]
    return {"ticker": ticker, "expiry": expiry, "dte": (date.fromisoformat(expiry) - today).days,
            "strike": strike, "underlying": und, "call_id": cq["instrument_id"], "put_id": pq["instrument_id"],
            "call_bid": cq["bid"], "call_ask": cq["ask"], "call_mid": cq["mid"],
            "put_bid": pq["bid"], "put_ask": pq["ask"], "put_mid": pq["mid"],
            "straddle_mid": round(straddle, 4), "implied_move_pct": round(straddle / und * 100.0, 4),
            "call_iv": cq.get("iv"), "put_iv": pq.get("iv"),
            "atm_iv": round(sum(ivs) / len(ivs), 6) if ivs else None,
            "call_oi": cq.get("open_interest"), "put_oi": pq.get("open_interest"),
            "call_volume": cq.get("volume"), "put_volume": pq.get("volume"),
            "quote_ts": int(min(cq["quote_ts"], pq["quote_ts"])),
            "quality": "wide" if wide else "ok"}, None


# ── one pass ───────────────────────────────────────────────────────────────────

class _Stop(Exception):
    """The pass must stop now (budget, time box, Robinhood not answering / rate-limited)."""


class _Budget:
    def __init__(self, c: dict, day_used: int):
        self.calls = 0
        self.max_pass = int(c.get("max_calls_per_pass") or 200)
        self.day_left = max(0, int(c.get("max_calls_per_day") or 600) - int(day_used or 0))
        self.deadline = _clock() + float(c.get("pass_time_box_s") or 300)
        self.timeout = float(c.get("call_timeout_s") or 20)
        self.gap = float(c.get("min_call_gap_s") or 0)
        self.last = None
        self.rate_hits0 = _rh_rate_hits()

    def take(self):
        if self.calls >= self.max_pass:
            raise _Stop(f"reached the {self.max_pass}-call limit for one pass")
        if self.calls >= self.day_left:
            raise _Stop("reached the daily Robinhood call limit")
        if _clock() >= self.deadline:
            raise _Stop("ran out of time for this pass")
        if self.gap and self.last is not None:
            wait = self.last + self.gap - _clock()
            if wait > 0:
                _sleep(wait)
        self.calls += 1
        self.last = _clock()


def _call(bud: _Budget, tool: str, args: dict):
    """One Robinhood read under the pass budget and time box. A timeout or a rate-limit answer stops the pass;
    any other error is raised to the caller (that name / contract is skipped)."""
    bud.take()
    box = {}

    def run():
        try:
            box["v"] = _rh_call(tool, args)
        except BaseException as e:                     # noqa: BLE001 — handed back below
            box["e"] = e

    wait = max(0.5, min(bud.timeout, bud.deadline - _clock()))
    th = threading.Thread(target=run, name="option-snaps-rh", daemon=True)
    th.start()
    th.join(wait)
    if th.is_alive():
        raise _Stop(f"Robinhood did not answer within {wait:.0f} s")
    if _rh_rate_hits() > bud.rate_hits0:
        raise _Stop("Robinhood said 'too many requests'")
    if "e" in box:
        e = box["e"]
        if _is_rate_limit(e):
            raise _Stop("Robinhood said 'too many requests'")
        if _is_signed_out(e):
            raise _Stop(f"Robinhood refused ({type(e).__name__}): Mason may need to sign in to Robinhood again")
        raise e
    return box.get("v")


# per-process caches, valid for one ET day: chain expiries, instruments per (ticker, expiry), OCC -> id
_cache = {"day": None, "chains": {}, "instr": {}, "occ": {}, "no_options": set()}
_lock = threading.RLock()


def _day_cache(today: date) -> dict:
    with _lock:
        if _cache["day"] != today.isoformat():
            _cache.update(day=today.isoformat(), chains={}, instr={}, no_options=set())
        return _cache


def _rows_of(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _chain_expiries(bud, t, today):
    cc = _day_cache(today)
    if t in cc["chains"]:
        return cc["chains"][t]
    chains = _rows_of(_call(bud, "get_option_chains", {"underlying_symbol": t}), "chains", "results")
    ok = [ch for ch in chains if isinstance(ch, dict) and str(ch.get("symbol") or "").upper() == t
          and (_f(ch.get("trade_value_multiplier")) in (None, 100.0)) and not ch.get("settle_on_open")]
    exps = sorted({str(e)[:10] for ch in ok for e in (ch.get("expiration_dates") or [])})
    cc["chains"][t] = exps
    if not exps:
        cc["no_options"].add(t)
    return exps


def _next_cursor(data):
    try:
        import mason_robinhood
        return mason_robinhood._next_cursor(data, "cursor", ("next_cursor", "next"))
    except Exception:
        return None


class _ShortList(Exception):
    """The strike list ended (page limit) below the stock price: the at-the-money strike was never seen."""


def _both_sided_at_or_above(rows, expiry, und) -> bool:
    """True when `rows` hold a strike >= und with both a call and a put (pick_atm's own filters)."""
    a = pick_atm([r for r in rows if (_f(r.get("strike_price")) or -1) >= und], expiry, und)
    return a is not None


def _instruments(bud, t, expiry, today, c, und=None):
    """Active contracts of `t` on `expiry`. Robinhood pages them 100 rows at a time in ASCENDING strike order
    (docs/robinhood/finalization/E34_SHAPES.md), so for a name with hundreds of strikes (SPY, QQQ, TSLA) the first
    max_instrument_pages pages can end far BELOW the stock price. Review 2026-11-16: reading stops as soon as a strike
    at or above the stock price has both sides (no later page can hold a nearer strike), and when the page limit is
    reached before that, _ShortList is raised (the name is skipped) instead of handing pick_atm a list whose
    "nearest" strike is deep in the money, which stored a wrong expected move marked 'ok'."""
    cc = _day_cache(today)
    key = (t, expiry)
    hit = cc["instr"].get(key)
    if hit is not None and (hit["complete"] or (und and _both_sided_at_or_above(hit["rows"], expiry, und))):
        return hit["rows"]
    rows, cursor, seen, complete, pages = [], None, set(), False, 0
    for _ in range(max(1, int(c.get("max_instrument_pages") or 5))):
        pages += 1
        args = {"chain_symbol": t, "expiration_dates": expiry, "state": "active"}
        if cursor:
            args["cursor"] = cursor
        data = _call(bud, "get_option_instruments", args)
        rows.extend(r for r in _rows_of(data, "instruments", "results") if isinstance(r, dict))
        nxt = _next_cursor(data)
        if not nxt or nxt in seen:
            complete = True
            break
        if und and _both_sided_at_or_above(rows, expiry, und):
            break
        seen.add(nxt)
        cursor = nxt
    cc["instr"][key] = {"rows": rows, "complete": complete}
    if not complete and und and not _both_sided_at_or_above(rows, expiry, und):
        raise _ShortList(f"strike list longer than {pages} page(s); the at-the-money strike was not reached")
    return rows


def _occ_to_id(bud, occ, today, conn):
    cc = _day_cache(today)
    with _lock:
        if occ in _cache["occ"]:
            return _cache["occ"][occ]
    p = parse_occ(occ)
    if not p:
        return None
    iid = None
    try:                                              # mason_option_bars' cache of known contracts (read only)
        r = conn.execute("SELECT rh_instrument_id FROM option_contracts WHERE replace(occ,' ','')=? "
                         "AND rh_instrument_id IS NOT NULL LIMIT 1", (occ.replace(" ", ""),)).fetchone()
        iid = r[0] if r else None
    except Exception:
        iid = None
    if not iid:
        data = _call(bud, "get_option_instruments", {
            "chain_symbol": p["underlying"], "expiration_dates": p["expiry"],
            "strike_price": f"{p['strike']:.4f}", "type": p["type"], "state": "active"})
        m = [r.get("id") for r in _rows_of(data, "instruments", "results")
             if isinstance(r, dict) and r.get("id") and str(r.get("expiration_date"))[:10] == p["expiry"]
             and r.get("type") == p["type"] and abs((_f(r.get("strike_price")) or -1) - p["strike"]) < 1e-6
             and (_f(r.get("trade_value_multiplier")) in (None, 100.0))]
        iid = m[0] if len(m) == 1 else None
    with _lock:
        cc["occ"][occ] = iid
    return iid


def _quotes(bud, ids) -> dict:
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 20):
        data = _call(bud, "get_option_quotes", {"instrument_ids": ids[i:i + 20]})
        for r in _rows_of(data, "results"):
            q = parse_option_quote(r)
            if q:
                out[q["instrument_id"]] = q
    return out


def _stock_prices(bud, syms, now, c, read_now=None) -> dict:
    """{symbol: last price}. Review 2026-11-17 (snapshot fix): "not in the future" and "not too old" are judged against
    the moment each batch of quotes came back (`read_now()`), no longer against the pass START (`now`). The start can be
    half a minute earlier (a slow positions read right after a restart); every stock that had traded in those seconds
    was then thrown out as "in the future" and skipped as "no current stock price" - on 2026-11-17 that left 26 of 30
    names without a straddle and only thinly traded stocks got through. `read_now` None = the old behaviour (tests)."""
    out = {}
    syms = list(dict.fromkeys(s for s in syms if s))
    max_age = float(c.get("max_quote_age_s") or 900)
    for i in range(0, len(syms), 20):
        data = _call(bud, "get_equity_quotes", {"symbols": syms[i:i + 20]})
        at = float(read_now()) if read_now is not None else now
        for r in _rows_of(data, "results"):
            s, px, ts = parse_equity_quote(r, at)
            if s and px and ts and at - ts <= max_age:
                out[s] = px
    return out


def _journal_owners(conn) -> dict:
    """OCC -> owner of the paper-account contract: 'mason', 'nico' or 'mason+nico'.
    First source: agent_trades (open, not deleted) - the book mason_protect and the exit manager use to tell Mason's
    contracts from Nico's (Nico's own WMT 12/18 110 calls and HOOG 11/20 17 calls are rows with source='nico' there
    and have NO options_trades journal row, so the journal alone left them unattributed; review 2026-11-16).
    Fallback: the options journal's created_by."""
    out = {}
    try:
        rows = conn.execute("SELECT contract_symbol, created_by FROM active_options_trades "
                            "WHERE contract_symbol IS NOT NULL AND contract_symbol != '' "
                            "ORDER BY created_at DESC").fetchall()
        for occ, who in rows:
            out.setdefault(str(occ).replace(" ", "").upper(), who)
    except Exception:
        pass
    try:
        book: dict = {}
        for occ, src in conn.execute("SELECT occ, source FROM agent_trades WHERE occ IS NOT NULL AND occ != '' "
                                     "AND status = 'open' AND COALESCE(record_deleted, 0) = 0").fetchall():
            if src:
                book.setdefault(str(occ).replace(" ", "").upper(), set()).add(str(src).lower())
        for occ, srcs in book.items():
            out[occ] = "+".join(sorted(srcs))
    except Exception:
        pass
    return out


def _top_catalysts(conn, now, c) -> list:
    try:
        rows = conn.execute(
            "SELECT primary_ticker, MAX(score) s FROM catalysts WHERE record_deleted = 0 AND last_seen >= ? "
            "AND primary_ticker IS NOT NULL AND score >= ? GROUP BY primary_ticker ORDER BY s DESC, primary_ticker "
            "LIMIT ?", (now - float(c.get("catalyst_lookback_hours") or 24) * 3600,
                        float(c.get("catalyst_min_score") or 0), int(c.get("max_catalyst_names") or 0) * 3)).fetchall()
        return [str(r[0]).upper() for r in rows]
    except Exception:
        return []


def gather_targets(conn, now, c) -> dict:
    """{"contracts": [{occ?, instrument_id?, underlying, expiry, strike, type, held_at, owner, qty}],
        "names": [ticker, ...] (held, then catalysts, then watched; capped), "notes": [...]}. No Robinhood call
    except the in-scope Robinhood positions read (cached by mason_robinhood)."""
    notes, contracts, held = [], [], []
    owners = _journal_owners(conn)
    try:
        raw = _alpaca_positions()
        if raw is None:
            notes.append("Alpaca positions could not be read")
            raw = []
    except Exception as e:
        notes.append(f"Alpaca positions: {type(e).__name__}")
        raw = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").upper()
        po = parse_occ(sym) if (p.get("asset_class") == "us_option" or len(sym) > 12) else None
        if po:
            contracts.append({"occ": sym, "instrument_id": None, "underlying": po["underlying"], "expiry": po["expiry"],
                              "strike": po["strike"], "type": po["type"], "held_at": "alpaca_paper",
                              "owner": owners.get(sym), "qty": _f(p.get("qty"))})
            held.append(po["underlying"])
        elif sym:
            held.append(sym)
    if c.get("include_robinhood_holdings"):
        try:
            for p in _rh_positions() or []:
                u = str(p.get("underlying") or "").upper()
                if p.get("kind") == "option" and p.get("option_id"):
                    contracts.append({"occ": None, "instrument_id": p["option_id"], "underlying": u,
                                      "expiry": str(p.get("expiry") or "")[:10] or None, "strike": _f(p.get("strike")),
                                      "type": p.get("option_type"), "held_at": "robinhood", "owner": "nico",
                                      "qty": _f(p.get("qty"))})
                if p.get("kind") in ("equity", "option") and u:
                    held.append(u)
        except Exception as e:
            notes.append(f"Robinhood positions: {type(e).__name__}")
    cats = _top_catalysts(conn, now, c)
    watched = []
    if c.get("include_watchlist"):
        try:
            watched = [str(t).upper() for t in (_watched() or []) if t]
        except Exception as e:
            notes.append(f"watchlist: {type(e).__name__}")
    names, n_cat = [], 0
    for group, cap in ((held, None), (cats, int(c.get("max_catalyst_names") or 0)), (watched, None)):
        k = 0
        for t in group:
            if not _SYM_RE.match(t or "") or t in names:
                continue
            if cap is not None and k >= cap:
                break
            names.append(t)
            k += 1
        if cap is not None:
            n_cat = k
    cap_all = int(c.get("max_names") or 30)
    return {"contracts": contracts, "names": names[:cap_all], "n_catalysts": n_cat, "notes": notes}


def _store(conn, date_et, slot, ts, contracts, straddles) -> None:
    """All rows of one pass in one transaction (all or nothing)."""
    with conn:
        for r in contracts:
            conn.execute(
                "INSERT OR REPLACE INTO opt_snap_contracts (instrument_id, date_et, slot, ts, occ, underlying, expiry, "
                "strike, type, held_at, owner, qty, bid, ask, mid, mark, last, prev_close, bid_size, ask_size, volume, "
                "open_interest, iv, delta, gamma, theta, vega, underlying_px, quote_ts, source) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'robinhood')",
                (r["instrument_id"], date_et, slot, int(r.get("ts") or ts), r.get("occ"), r.get("underlying"), r.get("expiry"),
                 r.get("strike"), r.get("type"), r.get("held_at"), r.get("owner"), r.get("qty"), r.get("bid"),
                 r.get("ask"), r.get("mid"), r.get("mark"), r.get("last"), r.get("prev_close"), r.get("bid_size"),
                 r.get("ask_size"), r.get("volume"), r.get("open_interest"), r.get("iv"), r.get("delta"),
                 r.get("gamma"), r.get("theta"), r.get("vega"), r.get("underlying_px"),
                 int(r["quote_ts"]) if r.get("quote_ts") else None))
        for r in straddles:
            conn.execute(
                "INSERT OR REPLACE INTO opt_snap_straddles (ticker, date_et, slot, expiry, ts, is_near, is_monthly, dte, "
                "strike, underlying, call_id, put_id, call_bid, call_ask, call_mid, put_bid, put_ask, put_mid, "
                "straddle_mid, implied_move_pct, call_iv, put_iv, atm_iv, call_oi, put_oi, call_volume, put_volume, "
                "quote_ts, quality, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'robinhood')",
                (r["ticker"], date_et, slot, r["expiry"], int(r.get("ts") or ts), int(bool(r.get("is_near"))), int(bool(r.get("is_monthly"))),
                 r["dte"], r["strike"], r["underlying"], r["call_id"], r["put_id"], r["call_bid"], r["call_ask"],
                 r["call_mid"], r["put_bid"], r["put_ask"], r["put_mid"], r["straddle_mid"], r["implied_move_pct"],
                 r["call_iv"], r["put_iv"], r["atm_iv"], r["call_oi"], r["put_oi"], r["call_volume"], r["put_volume"],
                 r["quote_ts"], r["quality"]))


def _record_pass(conn, date_et, slot, attempt, started, finished, status, reason, calls, nc, ns, nn) -> None:
    try:
        conn.execute("INSERT OR REPLACE INTO opt_snap_passes (date_et, slot, attempt, started_ts, finished_ts, status, "
                     "reason, calls, n_contracts, n_straddles, n_names) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (date_et, slot, attempt, int(started), int(finished), status, (reason or "")[:300], calls,
                      nc, ns, nn))
        conn.commit()
    except Exception as e:
        log.warning(f"[option-snaps] could not record the pass: {type(e).__name__}: {e}")


def passes_today(conn, date_et: str) -> dict:
    out = {}
    try:
        for r in conn.execute("SELECT slot, attempt, started_ts, status, reason, calls FROM opt_snap_passes "
                              "WHERE date_et=? ORDER BY slot, attempt", (date_et,)).fetchall():
            out.setdefault(r[0], []).append({"attempt": r[1], "started_ts": r[2], "status": r[3], "reason": r[4],
                                             "calls": r[5]})
    except Exception:
        pass
    return out


def calls_today(conn, date_et: str) -> int:
    try:
        r = conn.execute("SELECT COALESCE(SUM(calls),0) FROM opt_snap_passes WHERE date_et=?", (date_et,)).fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


_last = {"pass": None}

# why a name got no straddle, in a few plain words for the pass record (build_straddle's reasons shortened)
_LEG_WHY = {"a leg had no quote": "no option quote", "a leg had no two-sided quote": "no two-sided option quote",
            "a leg's quote was stale": "option quote too old"}


def _skip_summary(names, got, why, stopped) -> tuple:
    """(names without a straddle, plain summary or None), e.g. "26 of 30 names got no straddle: 22 no current stock
    price, 4 strike list too long". Review 2026-11-17: the reasons were kept only in memory, so a pass that stored 4 of
    30 names still read "ok" with an empty reason and nobody could see why."""
    miss = [t for t in names if t not in got]
    if not miss:
        return miss, None
    cnt: dict = {}
    for t in miss:
        w = why.get(t) or ("not reached before the pass stopped" if stopped else "skipped")
        cnt[w] = cnt.get(w, 0) + 1
    parts = ", ".join(f"{n} {w}" for w, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0])))
    return miss, f"{len(miss)} of {len(names)} names got no straddle: {parts}"


_INTERRUPTED = "Mason stopped or restarted during this pass"   # stays only when the pass never finished


def run_pass(slot: str, now: float | None = None, *, c: dict | None = None) -> dict:
    """One snapshot pass for `slot` (label). Stores held-contract rows and straddle rows in one transaction at the
    end; when the pass stops early (budget, time box, Robinhood not answering) the finished part is stored and the
    pass is 'partial'; when nothing could be read it is 'failed' and NOTHING is stored. Exactly one log line.
    Never raises."""
    c = c or cfg()
    t0 = _now() if now is None else now
    c0 = _clock()
    et = _et(t0)
    today, date_et = et.date(), et.date().isoformat()
    ts = int(t0)
    res = {"slot": slot, "date_et": date_et, "status": "failed", "reason": None, "calls": 0,
           "n_contracts": 0, "n_straddles": 0, "n_names": 0, "skipped": []}
    conn = None
    bud = None

    def read_now() -> float:
        # this moment (the pass start plus the time since, when the caller fixed `now`)
        return _now() if now is None else t0 + (_clock() - c0)

    def read_ts() -> int:
        # when the quotes were actually read (rounded UP): implied_move(as_of) only returns rows with ts <= as_of,
        # so stamping the pass START (as before the 2026-11-16 review) let a reader at 09:46 see quotes read at 09:49
        return int(math.ceil(read_now()))

    try:
        conn = _db()
        attempt = len(passes_today(conn, date_et).get(slot) or []) + 1
        res["attempt"] = attempt
        if not _rh_enabled():
            raise _Stop("Robinhood is not connected")
        bud = _Budget(c, calls_today(conn, date_et))
        # Review 2026-11-16: the pass is written down BEFORE its first Robinhood call, holding the most calls it may
        # make. A restart (or a crash loop) in the middle of a pass used to leave no row at all, so the calls were
        # not counted against the day and the slot started again from scratch on every boot. Now an interrupted
        # pass counts as an attempt (at most max_attempts_per_slot) and its reserve stays on the day's count.
        _record_pass(conn, date_et, slot, attempt, t0, t0, "running", _INTERRUPTED,
                     min(bud.max_pass, bud.day_left), 0, 0, 0)
        n0 = _rh_total_calls()
        tg = gather_targets(conn, t0, c)
        bud.calls += max(0, _rh_total_calls() - n0)     # Robinhood positions read (usually cached); counted, never capped
        res["n_names"] = len(tg["names"])
        res["skipped"].extend(tg["notes"])
        contracts_out, straddles_out, stop = [], [], None
        why: dict = {}                                   # name -> why it got no straddle (first reason wins)
        try:
            # (a) held contracts
            want = []
            for h in tg["contracts"]:
                iid = h.get("instrument_id")
                if not iid and h.get("occ"):
                    try:
                        iid = _occ_to_id(bud, h["occ"], today, conn)
                    except _Stop:
                        raise
                    except Exception as e:
                        res["skipped"].append(f"{h['occ']}: {type(e).__name__}")
                if iid:
                    want.append(dict(h, instrument_id=iid))
                else:
                    res["skipped"].append(f"{h.get('occ') or h.get('underlying')}: no Robinhood contract found")
            # stock prices for every name and every held contract's stock (one call per 20 symbols)
            und = _stock_prices(bud, list(tg["names"]) + [h["underlying"] for h in want], t0, c, read_now) \
                if (tg["names"] or want) else {}
            if want:
                qs = _quotes(bud, [h["instrument_id"] for h in want])
                rts = read_ts()
                for h in want:
                    q = qs.get(h["instrument_id"])
                    if not q:
                        res["skipped"].append(f"{h.get('occ') or h['instrument_id']}: no quote")
                        continue
                    contracts_out.append(dict(h, **{k: q.get(k) for k in (
                        "bid", "ask", "mid", "mark", "last", "prev_close", "bid_size", "ask_size", "volume",
                        "open_interest", "iv", "delta", "gamma", "theta", "vega", "quote_ts")},
                        underlying_px=und.get(h["underlying"]), ts=rts))
            # (b) straddles, a few names at a time so an early stop keeps the finished ones
            minDte = int(c.get("min_dte") or 5)
            group: list = []

            def flush(group):
                legs = [x for g in group for x in (g["call_id"], g["put_id"])]
                if not legs:
                    return
                qs = _quotes(bud, legs)
                rn = read_now()
                rts = int(math.ceil(rn))
                for g in group:
                    # review 2026-11-17: a leg's age is judged at the moment the legs were read (rn), not at the
                    # pass start, which could pass a leg up to the pass's time box older than max_quote_age_s
                    row, w = build_straddle(g["ticker"], g["expiry"], g["strike"], g["und"], qs.get(g["call_id"]),
                                            qs.get(g["put_id"]), today=today, now=rn, c=c)
                    if row:
                        row.update(is_near=g["is_near"], is_monthly=g["is_monthly"], ts=rts)
                        straddles_out.append(row)
                    else:
                        res["skipped"].append(f"{g['ticker']} {g['expiry']}: {w}")
                        why.setdefault(g["ticker"], _LEG_WHY.get(w, w))

            for t in tg["names"]:
                px = und.get(t)
                if not px:
                    res["skipped"].append(f"{t}: no current stock price")
                    why.setdefault(t, "no current stock price")
                    continue
                try:
                    exps = _chain_expiries(bud, t, today)
                    if not exps:
                        res["skipped"].append(f"{t}: no listed options")
                        why.setdefault(t, "no listed options")
                        continue
                    ch = choose_expiries(exps, today, minDte, bool(c.get("monthly", True)))
                    picks = {}
                    for kind in ("near", "monthly"):
                        e = ch.get(kind)
                        if e:
                            picks.setdefault(e, set()).add(kind)
                    for e, kinds in sorted(picks.items()):
                        try:
                            atm = pick_atm(_instruments(bud, t, e, today, c, px), e, px)
                        except _ShortList as sl:
                            res["skipped"].append(f"{t} {e}: {sl}")
                            why.setdefault(t, "strike list too long")
                            continue
                        if not atm:
                            res["skipped"].append(f"{t} {e}: no strike with both a call and a put")
                            why.setdefault(t, "no strike with both a call and a put")
                            continue
                        group.append({"ticker": t, "expiry": e, "strike": atm[0], "call_id": atm[1], "put_id": atm[2],
                                      "und": px, "is_near": "near" in kinds, "is_monthly": "monthly" in kinds})
                except _Stop:
                    raise
                except Exception as e:
                    res["skipped"].append(f"{t}: {type(e).__name__}")
                    why.setdefault(t, f"read error ({type(e).__name__})")
                if len(group) >= 8:
                    flush(group)
                    group = []
            flush(group)
            group = []
        except _Stop as s:
            stop = str(s)
        res["calls"] = bud.calls
        n = len(contracts_out) + len(straddles_out)
        if n:
            _store(conn, date_et, slot, ts, contracts_out, straddles_out)
        res.update(n_contracts=len(contracts_out), n_straddles=len(straddles_out))
        # Review 2026-11-17: the names left without a straddle, counted by reason, go into the pass record's reason
        # (no new column), and a pass that ran to its end but left MORE THAN HALF of its names without a straddle is
        # 'partial', not 'ok' (logged as a warning). 'partial' is what the scheduler and the health check already
        # treat as done: no retry, no alarm - only the label and the words change.
        miss, summ = _skip_summary(tg["names"], {r["ticker"] for r in straddles_out}, why, bool(stop))
        held_miss = len(tg["contracts"]) - len(contracts_out)
        if held_miss > 0:
            summ = (summ + "; " if summ else "") + f"{held_miss} of {len(tg['contracts'])} held contracts not read"
        if stop and not n:
            res.update(status="failed", reason=stop)
        elif stop:
            res.update(status="partial", reason=stop + (f"; {summ}" if summ else ""))
        elif not n and (tg["contracts"] or tg["names"]):
            res.update(status="failed", reason="nothing could be read" + (f"; {summ}" if summ else ""))
        elif tg["names"] and len(miss) * 2 > len(tg["names"]):
            res.update(status="partial", reason=summ)
        else:
            res.update(status="ok", reason=summ)
    except _Stop as s:
        res.update(status="failed", reason=str(s), calls=bud.calls if bud else 0)
    except Exception as e:
        res.update(status="failed", reason=f"{type(e).__name__}: {e}"[:300], calls=bud.calls if bud else 0,
                   n_contracts=0, n_straddles=0)
    finally:
        if conn is not None:
            fin = _now() if now is None else t0 + (_clock() - c0)
            _record_pass(conn, date_et, slot, res.get("attempt") or 1, t0, fin, res["status"], res["reason"],
                         res["calls"], res["n_contracts"], res["n_straddles"], res["n_names"])
            try:
                conn.close()
            except Exception:
                pass
    msg = (f"[option-snaps] {date_et} {slot}: {res['status']} - {res['n_contracts']} held contract(s), "
           f"{res['n_straddles']} straddle(s) for {res['n_names']} name(s), {res['calls']} Robinhood call(s)"
           + (f"; {res['reason']}" if res["reason"] else "")
           + (f"; skipped {len(res['skipped'])}" if res["skipped"] else ""))
    (log.info if res["status"] == "ok" else log.warning)(msg)
    with _lock:
        _last["pass"] = dict(res, at=t0)
    return res


# ── scheduler thread ───────────────────────────────────────────────────────────

_started = False
_stop_ev = threading.Event()
_thread: threading.Thread | None = None


def _beat(busy: str | None = None) -> None:
    try:
        import mason_runtime
        if busy:
            mason_runtime.busy(_THREAD_NAME, busy)
        else:
            mason_runtime.beat(_THREAD_NAME)
    except Exception:
        pass


def tick(now: float | None = None) -> dict | None:
    """One scheduler check: run the due slot, if any. Returns the pass result or None."""
    c = cfg()
    if not enabled(c):
        return None
    t = _now() if now is None else now
    d = _et(t).date()
    if not _trading_day(d):
        return None
    conn = _db()
    try:
        slot = due_slot(t, passes_today(conn, d.isoformat()), c)
    finally:
        conn.close()
    if not slot:
        return None
    _beat(f"option snapshot {slot}")
    return run_pass(slot, None if now is None else now, c=c)


def _loop(every: float = 30.0) -> None:
    while not _stop_ev.is_set():
        _beat()
        try:
            tick()
        except Exception as e:
            log.warning(f"[option-snaps] scheduler error: {type(e).__name__}: {e}")
        _beat()
        _stop_ev.wait(every)


def _start_thread() -> threading.Thread:
    global _thread
    th = threading.Thread(target=_loop, daemon=True, name=_THREAD_NAME)
    th.start()
    _thread = th
    return th


def start() -> bool:
    """Start the scheduler thread (idles while option_snaps.enabled is false; the switch is read every 30 s, so
    turning it on or off needs no restart) and register it with the runtime supervisor. Idempotent; never raises."""
    global _started
    try:
        with _lock:
            if _started:
                return False
            _started = True
        _start_thread()
        try:
            import mason_runtime
            mason_runtime.register(_THREAD_NAME, label="option price snapshots (3 a day in market hours)",
                                   interval=30, stale_after=20 * 60, thread=lambda: _thread,
                                   restart=_start_thread,
                                   # watched only while switched on: with the switch off the chat self-check
                                   # can never gain a line about this job (review 2026-11-16)
                                   should_run=lambda: _started and not _stop_ev.is_set() and enabled())
        except Exception:
            pass
        log.info("[option-snaps] scheduler started" + ("" if enabled() else " (switched off: option_snaps.enabled)"))
        return True
    except Exception as e:
        log.error(f"[option-snaps] start failed: {e}")
        return False


# ── reading the data ───────────────────────────────────────────────────────────

def _to_ts(as_of):
    if as_of is None:
        return _now()
    if isinstance(as_of, (int, float)):
        return float(as_of)
    if isinstance(as_of, datetime):
        return (as_of if as_of.tzinfo else as_of.replace(tzinfo=ET)).timestamp()
    if isinstance(as_of, date):
        return _et_at(as_of, 16 * 60)
    s = str(as_of).strip()
    if len(s) == 10:
        try:
            return _et_at(date.fromisoformat(s), 16 * 60)
        except ValueError:
            return None
    if re.search(r"([Zz]|[+-]\d\d:?\d\d)$", s):
        return parse_ts(s)
    try:
        return datetime.fromisoformat(s).replace(tzinfo=ET).timestamp()     # naive text = ET
    except ValueError:
        return None


def implied_move(ticker, as_of=None, *, which: str = "near", max_age_minutes=None, allow_wide: bool = False,
                 conn=None) -> dict | None:
    """The market's expected move for `ticker` from the newest straddle snapshot taken AT OR BEFORE `as_of`
    (epoch, datetime, ISO text — naive text is ET — or a date = that day's 16:00 ET; None = now).
    which = "near" (nearest expiry >= min_dte days out) or "monthly".
    None when: no snapshot, the snapshot is older than max_age_minutes (default option_snaps.stale_after_minutes)
    at as_of, the quotes were too wide (unless allow_wide), or no move could be computed. Never raises.
    Returned: {ticker, ts, as_of, age_minutes, expiry, dte, days_left, strike, underlying, straddle_mid,
               implied_move_pct, atm_iv (fraction), atm_iv_pct, daily_move_pct, quality, which, source}"""
    try:
        t = str(ticker or "").upper().strip()
        at = _to_ts(as_of)
        if not t or at is None:
            return None
        c = cfg()
        lim = float(max_age_minutes if max_age_minutes is not None else c.get("stale_after_minutes") or 240)
        col = "is_monthly" if which == "monthly" else "is_near"
        own = conn is None
        conn = conn or _db()
        try:
            _ensure_schema(conn)
            r = conn.execute(f"SELECT * FROM opt_snap_straddles WHERE ticker=? AND ts<=? AND {col}=1 "
                             "ORDER BY ts DESC LIMIT 1", (t, int(at))).fetchone()
            row = dict(zip([d[0] for d in conn.execute("SELECT * FROM opt_snap_straddles LIMIT 0").description], r)) \
                if r is not None else None
        finally:
            if own:
                conn.close()
        if not row:
            return None
        age = (at - row["ts"]) / 60.0
        if age > lim or row.get("implied_move_pct") is None:
            return None
        if row.get("quality") == "wide" and not allow_wide:
            return None
        iv = row.get("atm_iv")
        try:
            days_left = (date.fromisoformat(row["expiry"]) - _et(at).date()).days
        except Exception:
            days_left = None
        return {"ticker": t, "ts": row["ts"], "as_of": int(at), "age_minutes": round(age, 1),
                "expiry": row["expiry"], "dte": row["dte"], "days_left": days_left, "strike": row["strike"],
                "underlying": row["underlying"], "straddle_mid": row["straddle_mid"],
                "implied_move_pct": row["implied_move_pct"], "atm_iv": iv,
                "atm_iv_pct": round(iv * 100.0, 2) if iv else None,
                "daily_move_pct": round(iv * 100.0 / math.sqrt(252.0), 3) if iv else None,
                "quality": row.get("quality"), "which": "monthly" if which == "monthly" else "near",
                "source": row.get("source") or "robinhood"}
    except Exception as e:
        log.debug(f"[option-snaps] implied_move {ticker}: {e}")
        return None


def implied_move_pct(ticker, as_of=None, **kw):
    r = implied_move(ticker, as_of, **kw)
    return r["implied_move_pct"] if r else None


def implied_move_stored(ticker, as_of, *, which: str = "near", max_age_minutes=None, allow_wide: bool = False,
                        timeout_s: float = 1.0) -> dict | None:
    """implied_move() for the order path and the learning features (Job C, 2026-11-17): the same answer, read through
    a READ-ONLY connection that waits at most `timeout_s` for a lock, so it can never write, create a table or hold
    an order up. `as_of` is required (the caller states the moment; there is no "now" default). None on any problem.
    Stored rows only, no network."""
    if as_of is None:
        return None
    try:
        import sqlite3
        import mason_db
        p = Path(mason_db.DB_FILE).resolve()
        if not p.exists():
            return None
        conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, timeout=float(timeout_s))
    except Exception:
        return None
    try:
        return implied_move(ticker, as_of, which=which, max_age_minutes=max_age_minutes, allow_wide=allow_wide,
                            conn=conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _dicts(cur) -> list:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def http_get(args) -> tuple:
    """GET /api/options/snaps (read-only; docs/fable/OPTSNAP_API.md). Returns (payload, http_status)."""
    try:
        t = str((args.get("ticker") if args else "") or "").upper().strip()
        if t and not _SYM_RE.match(t):
            return {"ok": False, "error": "ticker must look like a stock symbol (e.g. WMT)",
                    "code": "bad_ticker"}, 400
        try:
            days = max(1, min(30, int((args.get("days") if args else None) or 5)))
        except (TypeError, ValueError):
            days = 5
        c = cfg()
        now = _now()
        since = int(now - days * 86400)
        conn = _db()
        try:
            out = {"ok": True, "enabled": enabled(c), "generated_at": int(now),
                   "stale_after_minutes": c.get("stale_after_minutes"), "status": status(conn=conn)}
            if t:
                out["ticker"] = t
                out["straddles"] = _dicts(conn.execute(
                    "SELECT * FROM opt_snap_straddles WHERE ticker=? AND ts>=? ORDER BY ts DESC, expiry LIMIT 400",
                    (t, since)))
                out["contracts"] = _dicts(conn.execute(
                    "SELECT * FROM opt_snap_contracts WHERE underlying=? AND ts>=? ORDER BY ts DESC LIMIT 400",
                    (t, since)))
                out["implied_move"] = {"near": implied_move(t, now, conn=conn),
                                       "monthly": implied_move(t, now, which="monthly", conn=conn)}
            else:
                # newest PASS per name (its near and monthly rows can carry read times a few seconds apart)
                out["latest"] = _dicts(conn.execute(
                    "SELECT s.* FROM opt_snap_straddles s JOIN (SELECT x.ticker, x.date_et, x.slot FROM "
                    "opt_snap_straddles x JOIN (SELECT ticker, MAX(ts) mts FROM opt_snap_straddles WHERE ts>=? "
                    "GROUP BY ticker) m ON x.ticker=m.ticker AND x.ts=m.mts GROUP BY x.ticker) n "
                    "ON s.ticker=n.ticker AND s.date_et=n.date_et AND s.slot=n.slot ORDER BY s.ticker, s.expiry",
                    (since,)))
                out["contracts"] = _dicts(conn.execute(
                    "SELECT c.* FROM opt_snap_contracts c JOIN (SELECT date_et, slot FROM opt_snap_contracts "
                    "WHERE ts>=? ORDER BY ts DESC LIMIT 1) m ON c.date_et=m.date_et AND c.slot=m.slot "
                    "ORDER BY c.underlying, c.expiry, c.strike", (since,)))
        finally:
            conn.close()
        return out, 200
    except Exception as e:
        return {"ok": False, "error": f"Mason could not read the option snapshots just now ({type(e).__name__}).",
                "code": "server_error"}, 500


def status(conn=None) -> dict:
    """Today's slots and what each pass did. Never raises."""
    try:
        c = cfg()
        now = _now()
        d = _et(now).date()
        own = conn is None
        conn = conn or _db()
        try:
            ps = passes_today(conn, d.isoformat())
            used = calls_today(conn, d.isoformat())
        finally:
            if own:
                conn.close()
        return {"enabled": enabled(c), "trading_day": _trading_day(d), "half_day": is_half_day(d, c),
                "slots_today": [s for s, _ in slots_for(d, c)], "passes_today": ps,
                "robinhood_calls_today": used, "max_calls_per_day": c.get("max_calls_per_day"),
                "started": _started}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def health() -> dict:
    """mason_health check 'option_snaps'. Not ok only when, on a trading day, a snapshot time has come and gone
    (its grace period over, or its last allowed attempt made) with no stored data because the pass failed.
    Outside market hours, on weekends and holidays, and with the switch off it is ok (quiet is normal). No network."""
    try:
        c = cfg()
        if not enabled(c):
            return {"ok": True, "detail": "switched off (option_snaps.enabled)"}
        now = _now()
        d = _et(now).date()
        if not _trading_day(d):
            return {"ok": True, "detail": "not a trading day"}
        conn = _db()
        try:
            ps = passes_today(conn, d.isoformat())
        finally:
            conn.close()
        slots = slots_for(d, c)
        grace = float(c.get("slot_grace_minutes") or 60)
        hm_now = (now - _et_at(d, 0)) / 60.0
        done = []
        for label, m in slots:
            tries = ps.get(label) or []
            if any(t["status"] in ("ok", "partial") for t in tries):
                done.append(label)
                continue
            if not tries:
                continue                              # not run yet, or Mason was not running then
            over = hm_now >= m + grace or len(tries) >= int(c.get("max_attempts_per_slot") or 2)
            if over:
                return {"ok": False, "detail": f"the {label} ET option snapshot failed: {tries[-1].get('reason')}"}
        if not done:
            first = slots[0][0] if slots else None
            return {"ok": True, "detail": f"no snapshot yet today (first at {first} ET)" if first else "no snapshot times today"}
        return {"ok": True, "detail": f"snapshots stored today at {', '.join(done)} ET"}
    except Exception as e:
        return {"ok": True, "detail": f"check skipped: {type(e).__name__}"}


def _main(argv=None) -> int:
    """Read-only CLI: --status, or --implied-move TICKER. (No manual pass from the command line: a second process
    using Mason's Robinhood login could refresh its sign-in under the running service.)"""
    import argparse
    ap = argparse.ArgumentParser(description="Mason option price snapshots (read-only).")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--implied-move", metavar="TICKER")
    a = ap.parse_args(argv)
    if a.implied_move:
        print(json.dumps(implied_move(a.implied_move), indent=1, default=str))
    else:
        print(json.dumps(status(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
