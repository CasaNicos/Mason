"""
mason_watch_signals.py — the watchlist SIGNALS layer (docs/watchlist/WATCHLIST_CONTRACTS.md §5).

Everything here reads the 1m/1d bars the TAPE (mason_watch_tape) stores and turns them into:

    indicators     vwap · atr_pct · rel_volume · opening_range · tape_stats   (pure, no LLM)
    detectors      detect() → watch_events rows (R6), once per ticker/kind/day
                   (move_step / pos_pnl_step once per step, level_cross once per level)
    notify policy  notify_policy() (D5/R7) → mason_notifications "major_move"
    Opus wake      wake_score() / event_candidates() / mark_consumed()   (R8, shares the 1/day cap)
    qwen digests   build_digest() / run_digests()  — qwen writes ≤3 sentences of prose from the
                   deterministic stats; number_guard() rejects any figure not in them (R9)
    director       tape_section_text() (R11a) · watch_ref() · trade_story() (R12)

Design rules (N1/N2): with `watchlist.enabled=false` every IO entry point is a cheap no-op
(the pure helpers vwap/atr_pct/opening_range/detect/notify_policy/wake_score/number_guard
compute regardless — they touch nothing). Nothing public raises; every mason_db connection
opened here is closed in a finally. No look-ahead: stats "as of" bar t use only bars ≤ t.

Cross-module calls go through the public APIs only, via guarded imports with module-level
hooks tests replace: _TAPE (mason_watch_tape), _WL (mason_watchlist), _alpaca_positions /
_personal_positions (mason_theses seams), _add_notification, _now.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import mason_db

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                  # pragma: no cover
    _ET = None

log = logging.getLogger("mason.watch_signals")

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
_CREATED_BY = "mason_watch_signals"

_DEFAULTS = {
    "enabled": False,
    "sector_etf": {},
    "extended": {"poll_seconds": 120},
    "digests": {"enabled": False, "event_throttle_minutes": 60, "max_busy_wait_seconds": 300,
                "yield_sleep_seconds": 5},
    "opus_wake": {"enabled": False, "min_score": 0.85},
    "notify": {"enabled": False,
               "move_steps_pct": [3, 5, 8, 12], "atr_mult": 1.5, "rvol_spike": 3.0,
               "rvol_min_move_pct": 1.5, "gap_pct": 3.0,
               "position_pnl_steps_pct": [5, 10, 20, 35, 50], "stop_target_proximity_pct": 1.0,
               "rel_strength_pct": 2.0},
}

# ── fixed thresholds (not in the contract config) ─────────────────────────────
_RTH_MINUTES = 390
_OR_MINUTES = 15
_PROFILE_SESSIONS = 20            # rvol: last 20 sessions of stored 1m bars
_PROFILE_MIN_SESSIONS = 3         # fewer same-source sessions than this → daily fallback
_IEX_SHARE_MIN_DAYS = 3           # IEX-share calibration needs ≥3 recorded days
_IEX_REC_WINDOW = ("15:59", "16:10")   # record the day's IEX RTH volume here (before the 16:20 SIP reconcile)
_REPLAY_AGE_S = 300               # a bar >5 min old (or bar["replay"]) is a backfill/catch-up replay;
                                  # yf (extended-hours poll) bars get max(300, extended.poll_seconds + 180)
_MUST_NOTIFY = ("level_cross", "pos_pnl_step", "pos_near_stop", "pos_near_target")
_RVOL_MIN_RTH_BARS = 5            # rvol_spike needs ≥5 RTH minutes (minute-1 rvol is noise)
_ATR_N = 14
_DAILY_FETCH = 210                # tape daily_history(days) = last N completed daily bars; sma200 needs 200
_INSUFFICIENT = "insufficient history"
_BLOCK_MAX_CHARS = 260            # ≈ 60-65 tokens per tape block
_QUIET_MOVE_PCT = 1.0
_DIGEST_MAX_SENTENCES = 3
_SEV_TO_NOTIF = {"info": "info", "notable": "warn", "alert": "critical"}
_SECTOR_TO_ETF = {
    "Information Technology": "XLK", "Technology": "XLK", "Financials": "XLF", "Energy": "XLE",
    "Health Care": "XLV", "Industrials": "XLI", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Utilities": "XLU", "Materials": "XLB", "Real Estate": "XLRE",
    "Communication Services": "XLC", "Telecommunications": "XLC",
}
_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")

# ── injectable hooks (tests replace these) ────────────────────────────────────
_TAPE = None          # mason_watch_tape module (or a fake)
_WL = None            # mason_watchlist module (or a fake)


def _now() -> float:
    return time.time()


def _tape():
    if _TAPE is not None:
        return _TAPE
    try:
        import mason_watch_tape
        return mason_watch_tape
    except Exception as e:
        log.info(f"[watch_signals] tape unavailable: {e}")
        return None


def _wl():
    if _WL is not None:
        return _WL
    try:
        import mason_watchlist
        return mason_watchlist
    except Exception as e:
        log.info(f"[watch_signals] watchlist store unavailable: {e}")
        return None


def _alpaca_positions() -> list:
    import mason_theses
    return mason_theses._alpaca_positions()


def _personal_positions() -> list:
    import mason_theses
    return mason_theses._personal_positions()


def _add_notification(type, title, **kw):
    import mason_notifications
    return mason_notifications.add_notification(type, title, **kw)


def _is_trading_day(dt: datetime) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(dt))
    except Exception:
        return dt.weekday() < 5


def _default_llm(system: str, user: str, **opts) -> str:
    # qwen via the gateway; the local provider sends think:false from llm config (CLAUDE.md)
    import mason_llm
    return mason_llm.complete(system, user, intent="chore", **opts)


def _default_busy() -> bool:
    try:
        import mason_llm
        return bool(mason_llm.interactive_busy())
    except Exception:
        return False


def _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_wait) -> bool:
    try:
        import mason_analyst_desk
        return mason_analyst_desk._gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_wait)
    except ImportError:
        pass
    if not busy_fn():                                  # same semantics as the desk gate
        return True
    start = clock()
    while busy_fn():
        if max_wait is not None and (clock() - start) >= max_wait:
            return False
        sleep_fn(max(0.05, yield_sleep))
    return True


def _model_name() -> str:
    try:
        import mason_llm
        return str(mason_llm._llm_cfg()["local"]["workhorse"])
    except Exception:
        return "qwen"


# ── caches ────────────────────────────────────────────────────────────────────
_PROFILE_CACHE: dict = {}         # (ticker, date) -> (built_epoch, [(src_class, cum[390])])
_PEER_CACHE: dict = {}            # (sym, date, as_of) -> chg_pct
_POS_CACHE = {"at": 0.0, "data": None}
_SECTOR_CACHE: dict = {}
_STATE_LOCK = threading.Lock()


def _reset_caches() -> None:
    _PROFILE_CACHE.clear()
    _PEER_CACHE.clear()
    _SECTOR_CACHE.clear()
    _POS_CACHE.update(at=0.0, data=None)


# ── config (read per call, mtime-cached) ─────────────────────────────────────
_cfg_cache = {"mtime": None, "data": {}}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime_ns
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def _cfg() -> dict:
    try:
        return _merge(_DEFAULTS, _config().get("watchlist") or {})
    except Exception:
        return copy.deepcopy(_DEFAULTS)


def enabled() -> bool:
    try:
        return bool(_cfg().get("enabled", False))
    except Exception:
        return False


def _sub_enabled(name: str) -> bool:
    try:
        c = _cfg()
        return bool(c.get("enabled")) and bool((c.get(name) or {}).get("enabled"))
    except Exception:
        return False


# ── time / bar helpers ────────────────────────────────────────────────────────

def _et(ts) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=_ET)


def _date_et(ts) -> str:
    return _et(ts).strftime("%Y-%m-%d")


def _hm(ts) -> str:
    return _et(ts).strftime("%H:%M")


def _open_ts(date_str: str) -> int:
    y, m, d = map(int, date_str.split("-"))
    return int(datetime(y, m, d, 9, 30, tzinfo=_ET).timestamp())


def _midnight_ts(date_str: str) -> int:
    y, m, d = map(int, date_str.split("-"))
    return int(datetime(y, m, d, 0, 0, tzinfo=_ET).timestamp())


def _iso(ts=None) -> str:
    return datetime.fromtimestamp(float(_now() if ts is None else ts)).isoformat(timespec="seconds")


def _f(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _session_of(bar: dict) -> str:
    s = bar.get("session")
    if s in ("pre", "rth", "post"):
        return s
    hm = _hm(bar["ts"])
    return "pre" if hm < "09:30" else ("rth" if hm < "16:00" else "post")


def _rth(bars) -> list:
    return [b for b in (bars or []) if _session_of(b) == "rth"]


def _sorted_bars(bars, until_ts=None) -> list:
    out = []
    for b in bars or []:
        try:
            if b.get("ts") is None or _f(b.get("c")) is None:
                continue
            if until_ts is not None and int(b["ts"]) > int(until_ts):
                continue
            out.append(b)
        except Exception:
            continue
    out.sort(key=lambda b: int(b["ts"]))
    return out


def _pct(a, b):
    a, b = _f(a), _f(b)
    if a is None or not b:
        return None
    return (a / b - 1.0) * 100.0


def _g(x) -> str:
    """Compact number for keys: 5 → '5', 40.25 → '40.25', 40.0 → '40'."""
    try:
        s = f"{float(x):.2f}".rstrip("0").rstrip(".")
        return s or "0"
    except Exception:
        return str(x)


def _src_class(bars) -> str:
    """'iex' when most bars are IEX-only volume, else 'full' (sip / yf = consolidated)."""
    bars = list(bars or [])
    n_iex = sum(1 for b in bars if str(b.get("source") or "").lower() == "iex")
    return "iex" if bars and n_iex * 2 > len(bars) else "full"


# ═════════════════════════════ indicators (pure) ══════════════════════════════

def vwap(bars) -> float | None:
    """Volume-weighted average price. Uses each bar's own vwap when present, else the
    typical price (h+l+c)/3. None when there is no volume. Never raises."""
    try:
        num = den = 0.0
        for b in bars or []:
            v = _f(b.get("v")) or 0.0
            if v <= 0:
                continue
            px = _f(b.get("vwap"))
            if not px or px <= 0:
                h, l, c = _f(b.get("h")), _f(b.get("l")), _f(b.get("c"))
                if h is None or l is None or c is None:
                    continue
                px = (h + l + c) / 3.0
            num += px * v
            den += v
        return num / den if den > 0 else None
    except Exception:
        return None


def atr_pct(daily, n: int = 14) -> float | None:
    """Simple-average true range over the last n daily bars, as % of the last close.
    TR uses the prior close, so it needs n+1 bars; fewer → None."""
    try:
        bars = [b for b in (daily or []) if _f(b.get("h")) is not None and _f(b.get("l")) is not None
                and _f(b.get("c")) is not None]
        n = int(n)
        if n <= 0 or len(bars) < n + 1:
            return None
        trs = []
        for i in range(len(bars) - n, len(bars)):
            h, l, pc = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i - 1]["c"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        last = float(bars[-1]["c"])
        return (sum(trs) / n) / last * 100.0 if last else None
    except Exception:
        return None


def opening_range(bars, minutes: int = 15):
    """(hi, lo) of the RTH bars in [09:30, 09:30+minutes) — only once the window is
    complete (a bar at/after its last minute exists), else None."""
    try:
        rth = _sorted_bars(_rth(bars))
        if not rth:
            return None
        o = _open_ts(_date_et(rth[0]["ts"]))
        end = o + int(minutes) * 60
        win = [b for b in rth if o <= int(b["ts"]) < end]
        if not win or not any(int(b["ts"]) >= end - 60 for b in rth):
            return None
        return (max(float(b["h"]) for b in win), min(float(b["l"]) for b in win))
    except Exception:
        return None


def _prior_trading_dates(date_str: str, n: int) -> list:
    out = []
    d = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(n * 3 + 10):
        if len(out) >= n:
            break
        d -= timedelta(days=1)
        if _is_trading_day(d):
            out.append(d.strftime("%Y-%m-%d"))
    return out


def _iex_key(ticker, date_str) -> str:
    return f"watch_iexvol:{ticker}:{date_str}"


def _profile(ticker: str, date_str: str) -> list:
    """[(date, src_class, cumulative RTH volume per minute[390], recorded IEX RTH total|None)]
    for the last 20 sessions that have stored 1m bars. Cached per (ticker, date) for 30 min."""
    key = (ticker, date_str)
    hit = _PROFILE_CACHE.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    out = []
    tp = _tape()
    if tp is not None:
        for d in _prior_trading_dates(date_str, _PROFILE_SESSIONS):
            try:
                rth = _rth(tp.session_bars(ticker, d) or [])
            except Exception:
                rth = []
            if not rth:
                continue
            o = _open_ts(d)
            per = [0.0] * _RTH_MINUTES
            for b in rth:
                k = (int(b["ts"]) - o) // 60
                if 0 <= k < _RTH_MINUTES:
                    per[k] += _f(b.get("v")) or 0.0
            cum, run = [], 0.0
            for v in per:
                run += v
                cum.append(run)
            cls = _src_class(rth)
            rec = None
            if cls == "full":
                try:
                    rec = _f(_loads(_get_state(_iex_key(ticker, d))).get("iex_v"))
                except Exception:
                    rec = None
            out.append((d, cls, cum, rec))
    _PROFILE_CACHE[key] = (time.time(), out)
    return out


def _record_iex_volume(ticker: str, bars_today, ts: int) -> None:
    """Near the close, remember today's IEX-only RTH volume (desk_state) so that, once the
    SIP reconcile has overwritten these bars, rel_volume can still compare live IEX volume
    with IEX volume (R5) via the ticker's measured IEX share of full-market volume."""
    try:
        hm = _hm(ts)
        if not (_IEX_REC_WINDOW[0] <= hm < _IEX_REC_WINDOW[1]):
            return
        rth = _rth(bars_today)
        iex = [b for b in rth if str(b.get("source") or "").lower() == "iex"]
        if not rth or len(iex) < 0.9 * len(rth):
            return                               # mixed/backfilled day: share would be unreliable
        _set_state(_iex_key(str(ticker).upper(), _date_et(ts)),
                   json.dumps({"iex_v": sum(_f(b.get("v")) or 0.0 for b in iex), "bars": len(iex)}))
    except Exception as e:
        log.info(f"[watch_signals] record iex volume {ticker}: {e}")


def rel_volume(ticker, bars_today, daily) -> float | None:
    """Cumulative RTH volume so far ÷ the average cumulative volume at the same minute over
    the last 20 sessions of stored 1m bars — SAME volume source only (design R5: live IEX
    volume is compared with IEX volume, never with SIP):
      1. ≥3 sessions of the same source class → direct ratio;
      2. IEX today but the history is SIP (the post-close reconcile overwrote it): scale the
         SIP profile by this ticker's median IEX share (recorded IEX RTH total ÷ SIP RTH total,
         ≥3 recorded days, see _record_iex_volume);
      3. full-market (SIP/yf) today: 20-day average daily volume × elapsed-session fraction;
      else None (not comparable — never faked)."""
    try:
        rth = _sorted_bars(_rth(bars_today))
        if not rth:
            return None
        last_ts = int(rth[-1]["ts"])
        d = _date_et(last_ts)
        m = max(1, min(_RTH_MINUTES, (last_ts - _open_ts(d)) // 60 + 1))
        cum_today = sum(_f(b.get("v")) or 0.0 for b in rth)
        cls = _src_class(rth)
        prof = _profile(str(ticker).upper(), d)
        vals = [cum[m - 1] for _d, c, cum, _r in prof if c == cls]
        if len(vals) >= _PROFILE_MIN_SESSIONS:
            avg = sum(vals) / len(vals)
            return cum_today / avg if avg > 0 else None
        if cls == "iex":
            full = [(cum, rec) for _d, c, cum, rec in prof if c == "full"]
            shares = sorted(rec / cum[-1] for cum, rec in full if rec and cum[-1] > 0)
            fvals = [cum[m - 1] for cum, _r in full]
            if len(shares) >= _IEX_SHARE_MIN_DAYS and len(fvals) >= _PROFILE_MIN_SESSIONS:
                share = shares[len(shares) // 2] if len(shares) % 2 else \
                    (shares[len(shares) // 2 - 1] + shares[len(shares) // 2]) / 2
                exp = share * sum(fvals) / len(fvals)
                return cum_today / exp if exp > 0 else None
            return None
        prior = [b for b in (daily or []) if b.get("ts") is not None and _date_et(b["ts"]) < d
                 and (_f(b.get("v")) or 0) > 0]
        prior.sort(key=lambda b: int(b["ts"]))
        prior = prior[-_PROFILE_SESSIONS:]
        if not prior:
            return None
        exp = (sum(float(b["v"]) for b in prior) / len(prior)) * m / _RTH_MINUTES
        return cum_today / exp if exp > 0 else None
    except Exception:
        return None


# ═════════════════════════════ tape stats ═════════════════════════════════════

def _daily_before(tp, ticker, date_str) -> list:
    try:
        raw = tp.daily_history(ticker, days=_DAILY_FETCH) or []
    except Exception:
        raw = []
    return _sorted_bars([b for b in raw if b.get("ts") is not None and _date_et(b["ts"]) < date_str])


def _prior_close(tp, ticker, date_str, daily=None):
    """Live day → the tape's RTH prior close; historical as_of → the last daily close
    before that date (so a replay never sees a later close)."""
    if date_str == _date_et(_now()):
        try:
            pc = _f(tp.prior_close(ticker))
            if pc:
                return pc
        except Exception:
            pass
    if daily is None:
        daily = _daily_before(tp, ticker, date_str)
    return _f(daily[-1]["c"]) if daily else None


def _peer_chg(tp, sym, date_str, as_of):
    key = (sym, date_str, int(as_of))
    if key in _PEER_CACHE:
        return _PEER_CACHE[key]
    val = None
    try:
        bars = _sorted_bars(tp.session_bars(sym, date_str) or [], as_of)
        if bars:
            val = _pct(bars[-1]["c"], _prior_close(tp, sym, date_str))
    except Exception:
        val = None
    if len(_PEER_CACHE) > 2000:
        _PEER_CACHE.clear()
    _PEER_CACHE[key] = val
    return val


def _sector_etf(ticker: str):
    t = str(ticker).upper()
    m = (_cfg().get("sector_etf") or {})
    if m.get(t):
        return str(m[t]).upper()
    if t in _SECTOR_CACHE:
        return _SECTOR_CACHE[t]
    etf = None
    try:
        import mason_triage
        sec = ((mason_triage.broad_universe(allow_refresh=False) or {}).get(t) or {}).get("sector")
        etf = _SECTOR_TO_ETF.get(sec)
    except Exception:
        etf = None
    _SECTOR_CACHE[t] = etf
    return etf


def _iv_pct(x):
    x = _f(x)
    if x is None:
        return None
    return x * 100.0 if x <= 5 else x          # Alpaca/yf give decimals; store percentage points


def _opt_block(tp, ticker, date_str, as_of):
    try:
        snaps = [s for s in (tp.option_snaps(ticker, _midnight_ts(date_str)) or [])
                 if s.get("ts") is not None and int(s["ts"]) <= as_of and _iv_pct(s.get("atm_iv")) is not None]
        snaps.sort(key=lambda s: int(s["ts"]))
        if not snaps:
            s = tp.latest_option_snap(ticker)
            if s and s.get("ts") is not None and int(s["ts"]) <= as_of and _iv_pct(s.get("atm_iv")) is not None:
                return {"atm_iv": _iv_pct(s.get("atm_iv")), "iv_chg_since_open": None,
                        "implied_move_pct": _f(s.get("implied_move_pct"))}
            return None
        last = snaps[-1]
        first = next((s for s in snaps if int(s["ts"]) >= _open_ts(date_str)), None)
        iv = _iv_pct(last.get("atm_iv"))
        chg = (iv - _iv_pct(first.get("atm_iv"))) if first is not None else None
        return {"atm_iv": iv, "iv_chg_since_open": chg, "implied_move_pct": _f(last.get("implied_move_pct"))}
    except Exception:
        return None


def _stats_from(ticker: str, bars_today, as_of: int, since_ts=None) -> dict:
    """The deterministic stats dict at `as_of` (bars > as_of are ignored)."""
    tp = _tape()
    if tp is None:
        return {}
    T = str(ticker).upper()
    d = _date_et(as_of)
    bars = _sorted_bars(bars_today, as_of)
    daily = _daily_before(tp, T, d)
    pc = _prior_close(tp, T, d, daily)
    rth = _rth(bars)
    last = _f(bars[-1]["c"]) if bars else pc       # no bars yet: last = prior close, but NO change figure
    chg = _pct(last, pc) if bars else None
    hl = rth or bars
    day_hi = max(float(b["h"]) for b in hl) if hl else None
    day_lo = min(float(b["l"]) for b in hl) if hl else None
    vw = vwap(rth) if rth else None
    atr = atr_pct(daily, _ATR_N)
    spy = _peer_chg(tp, "SPY", d, as_of) if T != "SPY" else None
    etf = _sector_etf(T)
    sec = _peer_chg(tp, etf, d, as_of) if (etf and etf != T) else None
    orr = opening_range(bars, _OR_MINUTES)
    closes = [float(b["c"]) for b in daily]
    st = {
        "last": last,
        "prior_close": pc,
        "chg_pct_prev_close": chg,
        "gap_pct": _pct(rth[0].get("o") if rth[0].get("o") is not None else rth[0]["c"], pc) if rth else None,
        "day_hi": day_hi,
        "day_lo": day_lo,
        "range_pos": ((last - day_lo) / (day_hi - day_lo)
                      if (last is not None and day_hi is not None and day_hi > day_lo) else None),
        "vwap": vw,
        "above_vwap": (last > vw) if (last is not None and vw is not None) else None,
        "rvol": rel_volume(T, bars, daily),
        "atr_pct": atr,
        "move_atr_mult": (abs(chg) / atr) if (chg is not None and atr) else None,
        "vs_spy_pct": (chg - spy) if (chg is not None and spy is not None) else None,
        "vs_sector_pct": (chg - sec) if (chg is not None and sec is not None) else None,
        "sector_etf": etf,
        "or_hi": orr[0] if orr else None,
        "or_lo": orr[1] if orr else None,
        "trend_5d_pct": _pct(last, closes[-5]) if len(closes) >= 5 else None,
        "trend_20d_pct": _pct(last, closes[-20]) if len(closes) >= 20 else None,
        "opt": _opt_block(tp, T, d, as_of),
        "session": _session_of(bars[-1]) if bars else "closed",
        "no_trades_today": not bars,
        "as_of": int(bars[-1]["ts"]) if bars else int(as_of),
    }
    for n in (20, 50, 200):
        st[f"sma{n}"] = (sum(closes[-n:]) / n) if len(closes) >= n else _INSUFFICIENT
    if since_ts is not None:
        st["chg_since_pct"] = None
        try:
            ref = _sorted_bars(tp.bars(T, "1m", since_ts=int(since_ts) - 4 * 86400, until_ts=int(since_ts),
                                       limit=5000) or [], int(since_ts))
            if ref:
                st["chg_since_pct"] = _pct(last, ref[-1]["c"])
        except Exception:
            pass
    return st


def tape_stats(ticker, *, since_ts=None, as_of_ts=None) -> dict:
    """Deterministic tape stats for one ticker (§5 keys + prior_close, chg_since_pct when
    since_ts is given). `as_of_ts` (extension) evaluates the stats at a past moment using
    only bars ≤ it. {} when disabled / no tape. Never raises."""
    if not enabled():
        return {}
    try:
        tp = _tape()
        if tp is None or not ticker:
            return {}
        as_of = int(as_of_ts) if as_of_ts is not None else int(_now())
        T = str(ticker).upper()
        return _stats_from(T, tp.session_bars(T, _date_et(as_of)) or [], as_of, since_ts)
    except Exception as e:
        log.info(f"[watch_signals] tape_stats {ticker}: {e}")
        return {}


# ═════════════════════════════ detectors (pure) ═══════════════════════════════

def _levels(item) -> list:
    if not item:
        return []
    lv = item.get("levels")
    if lv is None and item.get("levels_json"):
        try:
            lv = json.loads(item["levels_json"])
        except Exception:
            lv = []
    return [x for x in (lv or []) if isinstance(x, dict) and _f(x.get("price"))]


def _who(set_by) -> str:
    return "Nico" if str(set_by or "").lower() == "nico" else "Mason"


def _level_by_label(item, words) -> float | None:
    for lv in _levels(item):
        lab = str(lv.get("label") or "").lower()
        if any(w in lab for w in words):
            return _f(lv.get("price"))
    return None


def _pos_pnl(position, last):
    if not position:
        return None
    if position.get("is_option"):
        return _f(position.get("pnl_pct"))
    entry = _f(position.get("entry"))
    if not entry or last is None:
        return _f(position.get("pnl_pct"))
    p = (float(last) / entry - 1.0) * 100.0
    return -p if str(position.get("side") or "long") == "short" else p


def detect(ticker, bars_today, stats, item, position, prev_state) -> list:
    """PURE detectors on the bars up to now. Returns event dicts
    {ticker, ts, kind, severity, message, value, dedup, covers, step, step_key}; anything
    whose dedup key is already in prev_state["fired"] is suppressed. Never raises."""
    try:
        T = str(ticker or "").upper()
        cfg = _cfg().get("notify") or {}
        stats = stats or {}
        fired = set((prev_state or {}).get("fired") or [])
        bars = _sorted_bars(bars_today)
        cur = bars[-1] if bars else None
        ts = int(cur["ts"]) if cur else int(stats.get("as_of") or _now())
        last = _f(stats.get("last"))
        if last is None and cur:
            last = float(cur["c"])
        if last is None:
            return []
        rth = _rth(bars)
        in_rth = bool(cur) and _session_of(cur) == "rth"
        prev_px = float(bars[-2]["c"]) if len(bars) >= 2 else _f(stats.get("prior_close"))
        out = []

        def add(kind, severity, message, value, dedup=None, covers=None, step=None, step_key=None):
            dk = dedup or kind
            if dk in fired:
                return
            out.append({"ticker": T, "ts": ts, "kind": kind, "severity": severity, "message": message,
                        "value": value, "dedup": dk, "covers": covers or [dk], "step": step,
                        "step_key": step_key})

        # VWAP cross (RTH): side of the previous close vs the VWAP up to it, vs now
        if in_rth and len(rth) >= 2 and rth[-1] is cur:
            pv = vwap(rth[:-1])
            cv = _f(stats.get("vwap")) or vwap(rth)
            if pv and cv:
                ps = (rth[-2]["c"] > pv) - (rth[-2]["c"] < pv)
                cs = (last > cv) - (last < cv)
                if ps and cs and ps != cs:
                    up = cs > 0
                    add("vwap_cross", "info", f"crossed {'above' if up else 'below'} VWAP {cv:.2f}",
                        {"dir": "up" if up else "down", "vwap": cv, "price": last})

        # opening range (complete) break + new session high/low after it
        orr = ((_f(stats.get("or_hi")), _f(stats.get("or_lo")))
               if stats.get("or_hi") is not None and stats.get("or_lo") is not None
               else opening_range(bars, _OR_MINUTES))
        if in_rth and orr and ts >= _open_ts(_date_et(ts)) + _OR_MINUTES * 60:
            hi, lo = orr
            if last > hi:
                add("or_break_up", "notable", f"broke above the opening range {hi:.2f}",
                    {"or_hi": hi, "price": last, "time": _hm(ts)})
            elif last < lo:
                add("or_break_down", "notable", f"broke below the opening range {lo:.2f}",
                    {"or_lo": lo, "price": last, "time": _hm(ts)})
            prior = rth[:-1]
            if prior and rth[-1] is cur:
                if float(cur["h"]) > max(float(b["h"]) for b in prior):
                    add("new_hod", "info", f"new high of day {float(cur['h']):.2f}", {"price": float(cur["h"])})
                if float(cur["l"]) < min(float(b["l"]) for b in prior):
                    add("new_lod", "info", f"new low of day {float(cur['l']):.2f}", {"price": float(cur["l"])})

        # % move from the prior close, in steps (the highest newly crossed step fires)
        chg = _f(stats.get("chg_pct_prev_close"))
        if chg is not None:
            steps = sorted(float(s) for s in (cfg.get("move_steps_pct") or []))
            crossed = [s for s in steps if abs(chg) >= s]
            new = [s for s in crossed if f"move_step:{_g(s)}" not in fired]
            if new:
                top = max(new)
                stp = int(top) if top == int(top) else top
                add("move_step", "alert" if top >= 8 else "notable",
                    f"{'up' if chg > 0 else 'down'} {abs(chg):.1f}% vs prior close",
                    {"chg_pct": chg, "atr_pct": _f(stats.get("atr_pct")), "rvol": _f(stats.get("rvol")),
                     "step": stp, "dir": "up" if chg > 0 else "down"},
                    dedup=f"move_step:{_g(top)}", covers=[f"move_step:{_g(s)}" for s in crossed],
                    step=stp, step_key=_g(top))

        # relative volume spike
        rv = _f(stats.get("rvol"))
        if in_rth and rv is not None and len(rth) >= _RVOL_MIN_RTH_BARS and rv >= float(cfg.get("rvol_spike", 3.0)):
            add("rvol_spike", "notable", f"relative volume {rv:.1f}x", {"rvol": rv, "chg_pct": chg})

        # gap at the open
        gp = _f(stats.get("gap_pct"))
        gthr = float(cfg.get("gap_pct", 3.0))
        if in_rth and gp is not None and abs(gp) >= gthr:
            add("gap", "alert" if abs(gp) >= 2 * gthr else "notable",
                f"gapped {'up' if gp > 0 else 'down'} {abs(gp):.1f}% at the open", {"gap_pct": gp})

        # relative strength vs the sector ETF (SPY when no sector ETF is known)
        rs, vs = _f(stats.get("vs_sector_pct")), stats.get("sector_etf")
        if rs is None and not vs:
            rs, vs = _f(stats.get("vs_spy_pct")), "SPY"
        if in_rth and rs is not None and abs(rs) >= float(cfg.get("rel_strength_pct", 2.0)):
            add("rel_strength", "info", f"{'outperforming' if rs > 0 else 'underperforming'} {vs} by {abs(rs):.1f} pts",
                {"vs_sector_pct": rs, "vs": vs})

        # Opus / Nico price levels (once per level per day)
        if prev_px is not None:
            for lv in _levels(item):
                p, side = float(lv["price"]), str(lv.get("side") or "").lower()
                hit = ((side == "above" and prev_px <= p < last) or (side == "below" and prev_px >= p > last))
                if hit:
                    lab = str(lv.get("label") or "").strip()
                    add("level_cross", "alert",
                        f"crossed {p:.2f} {side} ({_who(lv.get('set_by'))}{' ' + lab if lab else ''})",
                        {"price": p, "side": side, "label": lab, "set_by": lv.get("set_by"), "from": prev_px,
                         "to": last, "rvol": rv},
                        dedup=f"level_cross:{_g(p)}:{side}", step_key=_g(p))

        # open position: P&L steps, stop / target proximity
        if position:
            pnl = _pos_pnl(position, last)
            steps = [float(x) for x in (cfg.get("position_pnl_steps_pct") or [])]
            upd = _pos_step_update((prev_state or {}).get("pos_steps"), pnl, steps)
            if upd and upd["baseline"]:
                b = upd["base"]["hi"] or upd["base"]["lo"]
                add("pos_baseline", "info", f"baseline: position at {pnl:+.1f}% (step {_sgn(b)}%)",
                    {"pnl_pct": pnl, "step": _num(b), "entry": _f(position.get("entry")),
                     "is_option": bool(position.get("is_option"))}, dedup="pos_baseline")
            elif upd and upd["cross"] is not None:
                c = upd["cross"]
                add("pos_pnl_step", "alert" if abs(c) >= 20 else "notable",
                    f"{'up' if pnl >= 0 else 'down'} {abs(pnl):.1f}% — crossed the {_sgn(c)}% step",
                    {"pnl_pct": pnl, "step": _num(c), "entry": _f(position.get("entry")),
                     "is_option": bool(position.get("is_option"))},
                    dedup=f"pos_pnl_step:{_sgn(c)}", step=_num(c), step_key=_sgn(c))
            prox = float(cfg.get("stop_target_proximity_pct", 1.0)) / 100.0
            short = str(position.get("side") or "long") == "short"
            stop = _f(position.get("stop")) or _level_by_label(item, ("stop",))
            tgt = _f(position.get("target")) or _level_by_label(item, ("target", "take", "tp"))
            if stop:
                near = (last >= stop * (1 - prox)) if short else (last <= stop * (1 + prox))
                if near:
                    add("pos_near_stop", "alert", f"within {prox * 100:.0f}% of the stop {stop:.2f}",
                        {"stop": stop, "price": last, "dist_pct": _pct(last, stop)})
            if tgt:
                near = (last <= tgt * (1 + prox)) if short else (last >= tgt * (1 - prox))
                if near:
                    add("pos_near_target", "alert", f"within {prox * 100:.0f}% of the target {tgt:.2f}",
                        {"target": tgt, "price": last, "dist_pct": _pct(last, tgt)})
        return out
    except Exception as e:
        log.info(f"[watch_signals] detect {ticker}: {e}")
        return []


def _num(x):
    x = float(x)
    return int(x) if x == int(x) else x


def _sgn(step) -> str:
    """+10 → '+10', -5 → '-5', 7.5 → '+7.5'."""
    return f"{'+' if float(step) >= 0 else '-'}{_g(abs(float(step)))}"


def _pos_step_update(ps, pnl, steps):
    """PERSISTENT per-position P&L-step state machine (pure). ps = {"hi": highest positive
    step already notified/baselined, "lo": lowest negative step (≤0)} or None = never seen.
    Returns {"base": state after baseline/re-arm, "cross": new step crossed (signed) | None,
    "final": state after applying the crossing, "baseline": bool} or None when pnl is unknown.
      - first sighting: current steps recorded (baseline=True only if a step is already reached;
        a genuinely new position under the first step just starts at 0/0);
      - hysteresis: hi is re-armed (dropped to the step now reached) only once P&L falls below
        the step UNDER hi (0 for the first step); lo symmetric. So ±oscillation around a step
        never re-alerts, while 10% → 4% → 5.5% re-alerts the +5% step;
      - cross: the highest positive step above hi, or the lowest negative step below lo."""
    if pnl is None:
        return None
    steps = sorted(float(s) for s in (steps or []) if float(s) > 0)
    up = max([s for s in steps if pnl >= s], default=0.0)
    dn = -max([s for s in steps if pnl <= -s], default=0.0)
    if not isinstance(ps, dict):
        base = {"hi": up, "lo": dn}
        return {"base": base, "cross": None, "final": dict(base), "baseline": bool(up or dn)}
    hi, lo = float(ps.get("hi") or 0), float(ps.get("lo") or 0)
    if hi > 0 and pnl < max([s for s in steps if s < hi], default=0.0):
        hi = up
    if lo < 0 and pnl > -max([s for s in steps if s < -lo], default=0.0):
        lo = dn
    base = {"hi": hi, "lo": lo}
    cross = up if up > hi else (dn if dn < lo else None)
    final = dict(base)
    if cross is not None:
        final["hi" if cross > 0 else "lo"] = cross
    return {"base": base, "cross": cross, "final": final, "baseline": False}


def _pos_steps_key(position) -> str:
    """desk_state key of one position identity: ticker + source + symbol + entry (2 dp), so a
    new lot / re-entry (different entry) or another account starts fresh."""
    p = position or {}
    t = str(p.get("ticker") or "").upper()
    return (f"watch_pos_steps:{t}:{p.get('source') or '?'}:{str(p.get('symbol') or t).upper()}:"
            f"{float(p.get('entry') or 0):.2f}")


def _next_state(prev_state, events) -> dict:
    fired = set((prev_state or {}).get("fired") or [])
    for e in events or []:
        fired.update(e.get("covers") or [e.get("dedup") or e.get("kind")])
    return {"fired": sorted(fired)}


# ═════════════════════════════ policy / wake ══════════════════════════════════

_ALWAYS_NOTIFY = ("level_cross", "pos_pnl_step", "pos_near_stop", "pos_near_target")


def notify_policy(event, item, position) -> bool:
    """D5/R7: level crosses and position events always; a 'decent change' move_step
    (|move| ≥ max(3%, atr_mult×ATR%)), rvol spike with |move| ≥ rvol_min_move_pct, or a
    gap ≥ gap_pct. Everything else is recorded silently."""
    try:
        cfg = _cfg().get("notify") or {}
        kind = (event or {}).get("kind")
        v = (event or {}).get("value") or {}
        if kind in _ALWAYS_NOTIFY:
            return True
        if kind == "move_step":
            chg = _f(v.get("chg_pct"))
            if chg is None:
                return False
            floor = min([float(s) for s in (cfg.get("move_steps_pct") or [3])] or [3.0])
            atr = _f(v.get("atr_pct"))
            thr = max(floor, float(cfg.get("atr_mult", 1.5)) * atr) if atr else floor
            return abs(chg) >= thr
        if kind == "rvol_spike":
            chg = _f(v.get("chg_pct"))
            return chg is not None and abs(chg) >= float(cfg.get("rvol_min_move_pct", 1.5))
        if kind == "gap":
            gp = _f(v.get("gap_pct"))
            return gp is not None and abs(gp) >= float(cfg.get("gap_pct", 3.0))
        return False
    except Exception:
        return False


def wake_score(event, stats, item, position) -> float:
    """0..1 — how strongly this event should wake Opus (R8). ≥0.85 (the default
    opus_wake.min_score): level_cross with rvol ≥ 2, a position P&L step of ≥10% either
    way, a decent change on an Opus item. Stop proximity 0.85, target 0.75."""
    try:
        kind = (event or {}).get("kind")
        v = (event or {}).get("value") or {}
        origin = str((item or {}).get("origin") or "")
        if kind == "level_cross":
            rv = _f((stats or {}).get("rvol"))
            if rv is None:
                rv = _f(v.get("rvol"))
            s = 0.9 if (rv is not None and rv >= 2.0) else (0.75 if str(v.get("set_by")) == "opus" else 0.7)
        elif kind == "pos_pnl_step":
            step = _f((event or {}).get("step"))
            step = step if step is not None else _f(v.get("step"))
            s = 0.9 if (step is not None and abs(step) >= 10) else 0.6
        elif kind == "pos_near_stop":
            s = 0.85
        elif kind == "pos_near_target":
            s = 0.75
        elif kind in ("move_step", "rvol_spike", "gap"):
            if notify_policy(event, item, position):
                s = 0.88 if origin == "opus" else (0.8 if position else 0.7)
            else:
                s = {"move_step": 0.5, "rvol_spike": 0.45, "gap": 0.4}[kind]
        else:
            s = {"rel_strength": 0.4, "or_break_up": 0.35, "or_break_down": 0.35, "new_hod": 0.3,
                 "new_lod": 0.3, "vwap_cross": 0.2}.get(kind, 0.1)
        return max(0.0, min(1.0, float(s)))
    except Exception:
        return 0.0


# ═════════════════════════════ positions ══════════════════════════════════════

def _positions_map() -> dict:
    """{UNDERLYING: [normalized position, ...]} from Alpaca paper + personal accounts
    (mason_theses seams). Cached 60 s — on_bar runs every minute per symbol."""
    if _POS_CACHE["data"] is not None and time.time() - _POS_CACHE["at"] < 60:
        return _POS_CACHE["data"]
    out: dict = {}
    try:
        for p in _alpaca_positions() or []:
            sym = str(p.get("ticker") or "").upper()
            m = _OCC_RE.match(sym)
            und = m.group(1) if m else sym
            qty = _f(p.get("qty")) or 0.0
            if not und or not qty:
                continue
            out.setdefault(und, []).append({
                "ticker": und, "symbol": sym, "qty": qty, "entry": _f(p.get("avg_entry")),
                "side": "short" if (qty < 0 or str(p.get("side") or "") == "short") else "long",
                "source": "alpaca", "is_option": bool(m), "pnl_pct": _f(p.get("unrealized_plpc"))})
    except Exception as e:
        log.info(f"[watch_signals] alpaca positions unavailable: {e}")
    try:
        for p in _personal_positions() or []:
            t = str(p.get("ticker") or "").upper()
            qty = _f(p.get("shares")) or 0.0
            if not t or t == "CASH" or not qty:
                continue
            out.setdefault(t, []).append({
                "ticker": t, "symbol": t, "qty": qty, "entry": _f(p.get("cost_basis")), "side": "long",
                "source": "personal", "is_option": False, "pnl_pct": None,
                "account": p.get("account_label")})
    except Exception as e:
        log.info(f"[watch_signals] personal positions unavailable: {e}")
    _POS_CACHE.update(at=time.time(), data=out)
    return out


def _position_for(ticker):
    """One representative position for the detectors: Alpaca paper stock, else the
    personal lots (qty-weighted entry), else the first option on the underlying."""
    try:
        lots = _positions_map().get(str(ticker).upper()) or []
        if not lots:
            return None
        paper = [p for p in lots if p["source"] == "alpaca" and not p["is_option"]]
        if paper:
            return dict(paper[0], lots=lots)
        pers = [p for p in lots if p["source"] == "personal" and p.get("entry")]
        if pers:
            q = sum(p["qty"] for p in pers)
            e = sum(p["qty"] * p["entry"] for p in pers) / q if q else pers[0]["entry"]
            return dict(pers[0], qty=q, entry=e, lots=lots)
        return dict(lots[0], lots=lots)
    except Exception:
        return None


def _bl(label):
    """N6b: a label as a MODEL sees it - the ••NNNN mask dropped (mason_robinhood.brain_label); the stored and UI labels
    keep it. Empty/None stay as they are. If the shared function cannot be loaded the label is replaced, never leaked."""
    if label is None or label == "":
        return label
    try:
        import mason_robinhood
        return mason_robinhood.brain_label(label)
    except Exception:
        return "Account"


def _pos_line(position, last) -> str:
    if not position:
        return ""
    try:
        pnl = _pos_pnl(position, last)
        e = _f(position.get("entry")) or 0.0
        q = _f(position.get("qty")) or 0.0
        qs = f"{q:.0f}" if abs(q - round(q)) < 1e-6 else f"{q:.2f}"
        what = (f"option {position.get('symbol')} ×{qs}" if position.get("is_option") else f"{qs} sh")
        where = "Alpaca paper" if position.get("source") == "alpaca" else (_bl(position.get("account")) or "personal")
        return (f"Position ({where}): {what} @ ${e:,.2f}" + (f" → {pnl:+.1f}%" if pnl is not None else ""))
    except Exception:
        return ""


def _badge(item, position) -> str:
    parts = []
    if item:
        parts.append("Mason-watched" if str(item.get("origin")) == "opus" else "Your watch")
    if position:
        parts.append("Position")
    return " · ".join(parts)


# ═════════════════════════════ state / storage ════════════════════════════════

def _get_state(key: str):
    try:
        import mason_analyst_desk
        return mason_analyst_desk.get_state(key)
    except ImportError:
        pass
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def _set_state(key: str, value: str) -> None:
    try:
        import mason_analyst_desk
        return mason_analyst_desk.set_state(key, value)
    except ImportError:
        pass
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                         (key, str(value), datetime.now().isoformat()))
    finally:
        conn.close()


def _load_state(key: str) -> dict:
    try:
        raw = _get_state(key)
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _store_event(T, ev, stats, wake: float, wake_flag: bool) -> str:
    eid = uuid.uuid4().hex[:12]
    val = dict(ev.get("value") or {})
    val.update({"wake_score": round(wake, 3), "dedup": ev.get("dedup"), "step": ev.get("step"),
                "rvol": _f((stats or {}).get("rvol")), "vs_sector_pct": _f((stats or {}).get("vs_sector_pct")),
                "chg_pct": _f((stats or {}).get("chg_pct_prev_close"))})
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO watch_events (id, ticker, ts, kind, severity, value_json, message, notified, "
                "notification_id, wake_candidate, consumed_by, created_date, created_by) "
                "VALUES (?,?,?,?,?,?,?,0,NULL,?,NULL,?,?)",
                (eid, T, int(ev["ts"]), ev["kind"], ev["severity"], json.dumps(val, default=str),
                 ev.get("message"), 1 if wake_flag else 0, _iso(), _CREATED_BY))
    finally:
        conn.close()
    return eid


def _mark_notified(eid: str, nid) -> None:
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute("UPDATE watch_events SET notified = 1, notification_id = ? WHERE id = ?",
                         (str(nid) if nid else None, eid))
    finally:
        conn.close()


def _query(sql: str, args=()) -> list:
    conn = mason_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _loads(s) -> dict:
    try:
        d = json.loads(s) if s else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ═════════════════════════════ on_bar ═════════════════════════════════════════

def _is_replay(bar, age_s) -> bool:
    """A bar is a replay when TAPE flags it (backfill / catch-up) or it is older than the
    source's normal delivery lag: 300 s for the stream / REST poll (1m bars arrive ≤~70 s
    late), max(300, extended.poll_seconds + 180) for yfinance extended-hours bars (a poll
    every 120 s + Yahoo delay). Wall-clock age, not "behind the newest stored bar": the
    extended poll and rest_poll emit several fresh bars per batch, so every bar but the last
    of a LIVE batch is behind the head and would be misread as a replay."""
    try:
        if bool((bar or {}).get("replay")):
            return True
        lim = _REPLAY_AGE_S
        if str((bar or {}).get("source") or "").lower() == "yf":
            poll = _f(((_cfg().get("extended") or {}).get("poll_seconds"))) or 120.0
            lim = max(_REPLAY_AGE_S, poll + 180)
        return float(age_s) > lim
    except Exception:
        return False


def _newest_price(tp, T, stats):
    """The latest stored 1m close for T (the tape head), else the stats' last."""
    try:
        b = tp.bars(T, "1m", limit=1) or []
        if b and _f(b[-1].get("c")) is not None:
            return float(b[-1]["c"])
    except Exception:
        pass
    return _f((stats or {}).get("last"))


def _still_holds(ev, price, position, item) -> bool:
    """Is a replayed must-notify condition still true at `price`?"""
    try:
        if price is None:
            return False
        v = ev.get("value") or {}
        k = ev["kind"]
        if k == "level_cross":
            p = float(v["price"])
            return price > p if v.get("side") == "above" else price < p
        if k == "pos_pnl_step":
            pnl = _pos_pnl(position, price)
            step = float(ev.get("step") or v.get("step"))
            return pnl is not None and (pnl >= step if step > 0 else pnl <= step)
        prox = float((_cfg().get("notify") or {}).get("stop_target_proximity_pct", 1.0)) / 100.0
        short = str((position or {}).get("side") or "long") == "short"
        if k == "pos_near_stop":
            stop = float(v["stop"])
            return (price >= stop * (1 - prox)) if short else (price <= stop * (1 + prox))
        if k == "pos_near_target":
            tgt = float(v["target"])
            return (price <= tgt * (1 + prox)) if short else (price >= tgt * (1 - prox))
    except Exception:
        pass
    return False


def on_bar(ticker, bar) -> list:
    """TAPE bar listener: detectors → watch_events (+ note_event) → notification per
    policy. Returns the stored events (each with its id).

    Replay bars (bar["replay"] from backfill/catch-up, or older than _is_replay's age limit)
    still update state and record events and are NEVER wake candidates. Notification on
    replay: must-notify kinds (level_cross, pos_*) from TODAY notify once, labelled
    "(caught up — crossed at HH:MM ET)", but only if the condition still holds at the newest
    price; if it no longer holds they are recorded silently WITHOUT burning the dedup key (kept
    in state 'replay_seen', which only suppresses repeats among replay bars), so a later live
    re-cross still alerts. Every other kind is silent on replay. Never raises."""
    if not enabled():
        return []
    try:
        if not ticker or not isinstance(bar, dict) or bar.get("ts") is None:
            return []
        T = str(ticker).upper()
        ts = int(bar["ts"])
        wl = _wl()
        item = None
        try:
            item = wl.get_item(T) if wl is not None else None
        except Exception:
            item = None
        if item and str(item.get("status") or "active") != "active":
            item = None
        position = _position_for(T)
        if not item and not position:
            return []                                  # infra symbols are references, not watched names
        tp = _tape()
        if tp is None:
            return []
        d = _date_et(ts)
        bars = _sorted_bars(tp.session_bars(T, d) or [], ts)
        if not any(int(b["ts"]) == ts for b in bars) and _f(bar.get("c")) is not None:
            bars = _sorted_bars(bars + [bar], ts)
        _record_iex_volume(T, bars, ts)
        stats = _stats_from(T, bars, ts)
        replay = _is_replay(bar, _now() - ts)
        today = _date_et(_now())
        key = f"watch_sig_state:{T}:{d}"
        plan = []                                    # (event, notify_mode) notify_mode: live|caught_up|silent
        pkey = _pos_steps_key(position) if position else None
        with _STATE_LOCK:
            prev = _load_state(key)
            view = dict(prev)
            if replay:                               # replay bars also skip what earlier replays already saw
                view["fired"] = sorted(set(prev.get("fired") or []) | set(prev.get("replay_seen") or []))
            ps = None
            if pkey:
                raw = _get_state(pkey)
                ps = _loads(raw) if raw else None
                view["pos_steps"] = ps
            evs = detect(T, bars, stats, item, position, view)
            if pkey:                                 # persistent P&L-step state (not per day)
                upd = _pos_step_update(ps, _pos_pnl(position, _f(stats.get("last"))),
                                       (_cfg().get("notify") or {}).get("position_pnl_steps_pct") or [])
                if upd:
                    pe = next((e for e in evs if e["kind"] == "pos_pnl_step"), None)
                    silent_replay = bool(pe) and replay and not (
                        pe["kind"] in _MUST_NOTIFY and d == today
                        and _still_holds(pe, _newest_price(tp, T, stats), position, item))
                    # a crossing counts only when its event actually goes out (live / caught-up);
                    # a silent or replay_seen-suppressed replay keeps the step armed
                    applied = upd["cross"] is None or (pe is not None and not silent_replay)
                    newst = upd["final"] if applied else upd["base"]
                    if newst != ps:
                        _set_state(pkey, json.dumps(newst))
            if not evs:
                return []
            burn, seen = [], []
            newest = _newest_price(tp, T, stats) if replay else None
            for ev in evs:
                if ev["kind"] == "pos_baseline":
                    plan.append((ev, "silent"))      # existing state: recorded, never alerted
                elif not replay:
                    plan.append((ev, "live"))
                    burn.append(ev)
                elif ev["kind"] in _MUST_NOTIFY and d == today and _still_holds(ev, newest, position, item):
                    plan.append((ev, "caught_up"))   # still true now → tell Nico once, labelled
                    burn.append(ev)
                elif ev["kind"] in _MUST_NOTIFY and d == today:
                    plan.append((ev, "silent"))      # no longer true → record only; a live re-cross still alerts
                    seen.append(ev)
                else:
                    plan.append((ev, "silent"))
                    burn.append(ev)
            st_new = _next_state(prev, [e for e in burn if e["kind"] != "pos_pnl_step"])  # steps: persistent
            rs = set(prev.get("replay_seen") or [])
            for ev in seen:
                rs.update(ev.get("covers") or [ev.get("dedup")])
            if rs:
                st_new["replay_seen"] = sorted(rs - set(st_new["fired"]))
            _set_state(key, json.dumps(st_new))
        cfg = _cfg()
        min_score = float((cfg.get("opus_wake") or {}).get("min_score", 0.85))
        notify_on = bool((cfg.get("notify") or {}).get("enabled"))
        out = []
        for ev, mode in plan:
            try:
                w = wake_score(ev, stats, item, position)
                if replay:
                    ev = dict(ev, value=dict(ev.get("value") or {}, replay=True))
                if mode == "caught_up":
                    verb = "crossed" if ev["kind"] == "level_cross" else "hit"
                    ev = dict(ev, message=f"{ev.get('message') or ev['kind']} (caught up — {verb} at {_hm(ts)} ET)")
                eid = _store_event(T, ev, stats, w, (w >= min_score) and not replay)
                ev = dict(ev, id=eid, wake_score=w, notified=False, replay=replay)
                try:
                    if wl is not None:
                        wl.note_event(T, datetime.fromtimestamp(ts).isoformat(timespec="seconds"))
                except Exception as e:
                    log.info(f"[watch_signals] note_event {T}: {e}")
                if notify_on and mode != "silent" and notify_policy(ev, item, position):
                    nid = _notify(T, ev, stats, item, position, d)
                    if nid is not None:
                        _mark_notified(eid, nid)
                        ev["notified"], ev["notification_id"] = True, nid
                out.append(ev)
            except Exception as e:
                log.info(f"[watch_signals] store/notify {T} {ev.get('kind')}: {e}")
        return out
    except Exception as e:
        log.info(f"[watch_signals] on_bar {ticker}: {e}")
        return []


def _pos_label(position) -> str:
    try:
        e = _f(position.get("entry")) or 0.0
        q = _f(position.get("qty")) or 0.0
        qs = f"{q:.0f}" if abs(q - round(q)) < 1e-6 else f"{q:.2f}"
        what = (f"option {position.get('symbol')} ×{qs}" if position.get("is_option") else f"{qs} sh")
        where = "Alpaca paper" if position.get("source") == "alpaca" else (_bl(position.get("account")) or "personal")
        return f"Position ({where}, {what} @ ${e:,.2f})"
    except Exception:
        return "Position"


def _notify(T, ev, stats, item, position, date_str):
    last = _f(stats.get("last"))
    chg = _f(stats.get("chg_pct_prev_close"))
    rv = _f(stats.get("rvol"))
    msg = ev.get("message") or ev["kind"]
    is_pos = ev["kind"].startswith("pos_")
    reason = str((item or {}).get("reason") or "").strip()
    lines = [_badge(item, position) + (f" — {reason}" if reason else "")]
    lines.append(f"{_pos_label(position)}: {msg}" if (is_pos and position) else msg)
    tape = []
    if last is not None:
        tape.append(f"last {last:.2f}" + (f" ({chg:+.1f}% vs prior close)" if chg is not None else ""))
    if rv is not None:
        tape.append(f"rvol {rv:.1f}")
    if tape:
        lines.append(" · ".join(tape))
    if position and not is_pos:
        pnl = _pos_pnl(position, last)
        lines.append(_pos_label(position) + (f": {pnl:+.1f}%" if pnl is not None else ""))
    title = f"{T}: {'position ' if is_pos else ''}{msg}"
    dk = f"watch:{T}:{ev['kind']}:{date_str}" + (f":{ev['step_key']}" if ev.get("step_key") else "")
    n = _add_notification("major_move", title, body="\n".join(lines),
                          severity=_SEV_TO_NOTIF.get(ev.get("severity"), "info"), ticker=T,
                          ref_table="watch_events", ref_id=ev["id"], created_by=_CREATED_BY, dedup_key=dk)
    return (n or {}).get("id") if isinstance(n, dict) else None


# ═════════════════════════════ Opus wake ══════════════════════════════════════

def event_candidates(*, since_epoch, limit=5) -> list:
    """Catalyst-shaped wake candidates for mason_director.maybe_run_event (R8/D9):
    unconsumed wake_candidate events since `since_epoch`, highest wake score first."""
    if not _sub_enabled("opus_wake"):
        return []
    try:
        thr = float((_cfg().get("opus_wake") or {}).get("min_score", 0.85))
        rows = _query("SELECT * FROM watch_events WHERE ts >= ? AND wake_candidate = 1 "
                      "AND (consumed_by IS NULL OR consumed_by = '') ORDER BY ts DESC LIMIT 200",
                      (int(float(since_epoch or 0)),))
        out = []
        for r in rows:
            v = _loads(r.get("value_json"))
            sc = _f(v.get("wake_score")) or 0.0
            if sc < thr:
                continue
            out.append({"id": f"watch:{r['id']}", "primary_ticker": r["ticker"], "tickers": [r["ticker"]],
                        "event_kind": "watch_trigger", "score": sc, "first_seen": int(r["ts"]),
                        "status": "open", "headline": r.get("message") or r.get("kind"),
                        "tape_json": {"rel_volume": v.get("rvol"), "vs_sector_pct": v.get("vs_sector_pct"),
                                      "chg_pct": v.get("chg_pct")},
                        "watch_event_id": r["id"]})
        out.sort(key=lambda c: (-c["score"], -c["first_seen"]))
        return out[:max(0, int(limit))]
    except Exception as e:
        log.info(f"[watch_signals] event_candidates: {e}")
        return []


def mark_consumed(event_id, slate_id) -> None:
    """Stamp a watch event as consumed by an Opus slate (accepts 'watch:<eid>' or <eid>)."""
    if not enabled():
        return None
    try:
        eid = str(event_id or "")
        if eid.startswith("watch:"):
            eid = eid[len("watch:"):]
        if not eid:
            return None
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("UPDATE watch_events SET consumed_by = ? WHERE id = ?", (str(slate_id), eid))
        finally:
            conn.close()
    except Exception as e:
        log.info(f"[watch_signals] mark_consumed {event_id}: {e}")
    return None


# ═════════════════════════════ number guard / digests ═════════════════════════

_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])(\$)?(\d[\d,]*(?:\.\d+)?)(\s?%)?")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def _collect_allowed(obj, nums: list, times: set, key=None) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        v = float(obj)
        if v == v:
            nums.append(abs(v))
            if key == "range_pos":
                nums.append(abs(v) * 100.0)
        return
    if isinstance(obj, str):
        for t in _TIME_RE.findall(obj):
            times.add(t)
        for m in _NUM_RE.finditer(_TIME_RE.sub(" ", obj)):
            try:
                nums.append(float(m.group(2).replace(",", "")))
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_allowed(v, nums, times, k)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_allowed(v, nums, times, key)


_UP_RE = re.compile(r"\b(rose|rise|rising|up|gain(?:ed|ing|s)?|higher|climb(?:ed|ing)?|rall(?:ied|y|ying)|"
                    r"jump(?:ed|ing)?|advanc(?:ed|ing)|increas(?:ed|ing)|added|surg(?:ed|ing)|popped)\b", re.I)
_DOWN_RE = re.compile(r"\b(fell|fall(?:ing)?|down|lost|los(?:ing|es)|lower|drop(?:ped|ping)?|declin(?:ed|ing)|"
                      r"slid|slipp(?:ed|ing)|sank|sink(?:ing)?|decreas(?:ed|ing)|shed|tumbl(?:ed|ing))\b", re.I)
_SIGNED_KEYS = ("chg_pct_prev_close", "gap_pct", "vs_spy_pct", "vs_sector_pct", "trend_5d_pct",
                "trend_20d_pct", "chg_since_pct", "iv_chg_since_open")


def _signed_values(obj, out: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _SIGNED_KEYS and _f(v) is not None:
                out.append(float(v))
            elif isinstance(v, (dict, list)):
                _signed_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _signed_values(v, out)


def _stated_direction(text: str, start: int, sign_char: str):
    """+1 / −1 when a % figure states a direction: an explicit sign right before it, else
    the LAST up/down word in the same sentence within 40 chars before it; None otherwise."""
    if sign_char == "+":
        return 1
    if sign_char in ("-", "−"):
        return -1
    pre = text[max(0, start - 40):start]
    pre = re.split(r"[.!?;]\s", pre)[-1]
    best = None
    for rx, d in ((_UP_RE, 1), (_DOWN_RE, -1)):
        for m in rx.finditer(pre):
            if best is None or m.start() > best[0]:
                best = (m.start(), d)
    return best[1] if best else None


def number_guard(text, stats) -> bool:
    """True when every figure in `text` is a (rounded) value from `stats` (searched
    recursively, incl. numbers/times inside strings). Checked tokens: any number with ≥2
    significant digits, and any number carrying % or $. A token with d decimals matches a
    value v when |v − x| ≤ ½·10^-d. A % figure that states a direction (explicit +/− or an
    up/down verb just before it) must agree with the sign of the signed stat it matches
    ('fell 2.3%' ok for −2.34, 'rose 2.3%' rejected). HH:MM times must appear in the stats.
    Single bare digits ('3 sentences') are exempt."""
    try:
        if not text:
            return True
        nums, times, signed = [], set(), []
        _collect_allowed(stats or {}, nums, times)
        _signed_values(stats or {}, signed)
        s = str(text)
        for t in _TIME_RE.findall(s):
            if t not in times:
                return False
        s = _TIME_RE.sub(" ", s)
        for m in _NUM_RE.finditer(s):
            dollar, raw, pct = m.group(1), m.group(2), m.group(3)
            digits = raw.replace(",", "")
            sig = len(digits.replace(".", "").lstrip("0"))
            if not dollar and not pct and sig < 2:
                continue
            x = float(digits)
            dec = len(digits.split(".")[1]) if "." in digits else 0
            tol = 0.5 * 10 ** (-dec) + 1e-9
            if not any(abs(v - x) <= tol for v in nums):
                return False
            if pct and not dollar:                   # a move with a stated direction must match its sign
                sgn = s[m.start() - 1] if m.start() > 0 else ""
                dirn = _stated_direction(s, m.start(), sgn)
                if dirn is not None:
                    same_abs = [v for v in signed if abs(abs(v) - x) <= tol]
                    if same_abs and not any((v > 0) == (dirn > 0) for v in same_abs if v != 0):
                        return False
        return True
    except Exception:
        return False


def _r(x, n=2):
    x = _f(x)
    return None if x is None else round(x, n)


def _events_between(T, since_ts, until_ts=None, limit=20, min_severity=None) -> list:
    sql = "SELECT * FROM watch_events WHERE ticker = ? AND ts >= ?"
    args = [T, int(since_ts)]
    if until_ts is not None:
        sql += " AND ts <= ?"
        args.append(int(until_ts))
    if min_severity:
        sql += " AND severity IN ('notable','alert')"
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    return _query(sql, tuple(args))


def _digest_payload(T, stats, item, events) -> dict:
    keys = ("last", "prior_close", "chg_pct_prev_close", "gap_pct", "day_hi", "day_lo", "vwap",
            "above_vwap", "rvol", "atr_pct", "vs_spy_pct", "vs_sector_pct", "sector_etf", "or_hi",
            "or_lo", "trend_5d_pct", "trend_20d_pct", "sma20", "sma50", "sma200", "session", "no_trades_today")
    p = {"ticker": T}
    for k in keys:
        v = stats.get(k)
        p[k] = _r(v) if isinstance(v, float) else v
    if stats.get("opt"):
        p["options"] = {k: _r(v, 1) for k, v in stats["opt"].items()}
    p["events_today"] = [{"time": _hm(e["ts"]), "kind": e["kind"], "what": e.get("message")}
                         for e in sorted(events, key=lambda e: e["ts"])]
    p["event_count"] = len(events)
    lv = [{"price": _f(x.get("price")), "side": x.get("side"), "label": x.get("label"),
           "set_by": x.get("set_by")} for x in _levels(item)]
    if lv:
        p["watched_levels"] = lv
    if item:
        p["watched_because"] = str(item.get("reason") or "")[:200]
    return p


_DIGEST_SYSTEM = (
    "You write a short tape note for a trader's log. Using ONLY the facts in the JSON, write at "
    "most 3 plain sentences describing what the stock's price and volume did. No advice, no "
    "predictions, no recommendations, no opinions on what to do. Do not state any number that is "
    "not in the facts; prefer words to numbers. No lists, no headings.")


def _template(T, p) -> str:
    if p.get("no_trades_today"):
        pc = p.get("prior_close")
        return f"{T}: no trades yet today" + (f"; prior close {pc:.2f}." if pc is not None else ".")
    bits = [f"{T}"]
    if p.get("last") is not None:
        bits.append(f"last {p['last']:.2f}")
    s = " ".join(bits)
    if p.get("chg_pct_prev_close") is not None:
        s += f", {p['chg_pct_prev_close']:+.1f}% vs prior close"
    if p.get("above_vwap") is not None:
        s += f", {'above' if p['above_vwap'] else 'below'} VWAP"
    if p.get("rvol") is not None:
        s += f", rvol {p['rvol']:.1f}"
    n = int(p.get("event_count") or 0)
    s += f"; {n} tape event{'s' if n != 1 else ''} today."
    return s


def _clean_text(text) -> str:
    s = str(text or "")
    s = re.sub(r"<think>.*?</think>", " ", s, flags=re.S)
    s = s.replace("```", " ").replace("\n", " ").strip().strip('"').strip()
    s = re.sub(r"\s+", " ", s)
    parts = [x for x in re.split(r"(?<=[.!?])\s+", s) if x.strip()]
    return " ".join(parts[:_DIGEST_MAX_SENTENCES]).strip()


def build_digest(ticker, slot, *, llm_fn=None) -> dict:
    """qwen tape digest (R9): stats are deterministic; the LLM writes ≤3 sentences of prose
    (temperature 0.1); number_guard() failure or an LLM error → a deterministic template
    with guard_ok=0. Stored in watch_digests; returns the row. {} when disabled."""
    if not enabled():
        return {}
    try:
        T = str(ticker or "").upper()
        if not T:
            return {}
        now = _now()
        d = _date_et(now)
        stats = tape_stats(T)
        item = None
        try:
            wl = _wl()
            item = wl.get_item(T) if wl is not None else None
        except Exception:
            item = None
        events = _events_between(T, _midnight_ts(d), None, limit=20)
        payload = _digest_payload(T, stats, item, events)
        user = (f"Ticker {T}, {slot} read.\nFacts (JSON):\n{json.dumps(payload, default=str)}\n\n"
                "Write at most 3 sentences about what the tape did.")
        text, model = "", _model_name()
        try:
            text = _clean_text((llm_fn or _default_llm)(_DIGEST_SYSTEM, user, temperature=0.1,
                                                         num_ctx=2048, num_predict=220))
        except Exception as e:
            log.info(f"[watch_signals] digest llm {T}: {e}")
            model = "template"
        ok = bool(text) and number_guard(text, payload)
        if not ok:
            if text:
                log.info(f"[watch_signals] digest {T} rejected by number guard: {text[:200]}")
            text = _template(T, payload)
        row = {"id": uuid.uuid4().hex[:12], "ticker": T, "date": d, "slot": str(slot),
               "stats_json": json.dumps(payload, default=str), "text": text, "model": model,
               "guard_ok": 1 if ok else 0, "created_date": _iso(now), "created_by": _CREATED_BY}
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("INSERT INTO watch_digests (id, ticker, date, slot, stats_json, text, model, "
                             "guard_ok, created_date, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             tuple(row[k] for k in ("id", "ticker", "date", "slot", "stats_json", "text",
                                                    "model", "guard_ok", "created_date", "created_by")))
        finally:
            conn.close()
        return row
    except Exception as e:
        log.info(f"[watch_signals] build_digest {ticker}: {e}")
        return {}


def run_digests(slot, *, llm_fn=None, busy_fn=None, sleep_fn=None, clock=None, max_busy_wait=None) -> dict:
    """Digests for every active item + open position (slot pre|mid|preanchor|close), or —
    slot 'event' — for tickers with a NOTIFIED event inside digests.event_throttle_minutes
    that have no event digest in that window. Each qwen call waits on the desk GPU gate
    (yields to interactive chat); if the gate gives up the run stops with deferred=True and
    the rest are picked up next call (a slot is built once per ticker per day)."""
    res = {"slot": slot, "built": 0, "skipped": 0, "deferred": False, "tickers": []}
    if not _sub_enabled("digests"):
        res["skipped_reason"] = "disabled"
        return res
    try:
        dcfg = _cfg().get("digests") or {}
        now = _now()
        d = _date_et(now)
        wl = _wl()
        tickers = set()
        if wl is not None:
            try:
                tickers |= {str(i.get("ticker")).upper() for i in (wl.list_items(status="active") or [])
                            if i.get("ticker")}
            except Exception:
                pass
            try:
                tickers |= {str(t).upper() for t in (wl.position_symbols() or [])}
            except Exception:
                pass
        if slot == "event":
            win = now - float(dcfg.get("event_throttle_minutes", 60)) * 60
            hot = {r["ticker"] for r in _query("SELECT DISTINCT ticker FROM watch_events WHERE notified = 1 "
                                               "AND ts >= ?", (int(win),))}
            recent = {r["ticker"] for r in _query("SELECT DISTINCT ticker FROM watch_digests WHERE slot = 'event' "
                                                  "AND created_date >= ?", (_iso(win),))}
            todo = sorted(t for t in tickers if t in hot and t not in recent)
        else:
            done = {r["ticker"] for r in _query("SELECT DISTINCT ticker FROM watch_digests WHERE date = ? "
                                                "AND slot = ?", (d, str(slot)))}
            todo = sorted(t for t in tickers if t not in done)
            res["skipped"] = len(tickers) - len(todo)
        busy = busy_fn or _default_busy
        wait = float(dcfg.get("max_busy_wait_seconds", 300)) if max_busy_wait is None else max_busy_wait
        for T in todo:
            if not _gpu_gate(busy, sleep_fn or time.sleep, clock or time.monotonic,
                             float(dcfg.get("yield_sleep_seconds", 5)), wait):
                res["deferred"] = True
                break
            row = build_digest(T, slot, llm_fn=llm_fn)
            if row:
                res["built"] += 1
                res["tickers"].append(T)
        return res
    except Exception as e:
        log.info(f"[watch_signals] run_digests {slot}: {e}")
        res["error"] = str(e)
        return res


# ═════════════════════════════ director-facing ═══════════════════════════════

def _ev_detail(kind, v) -> str:
    try:
        if kind == "level_cross":
            return f"{float(v['price']):.2f} {v.get('side')} ({_who(v.get('set_by'))})"
        if kind == "move_step":
            return f"{v.get('dir', '')} {_g(abs(float(v.get('step'))))}%".strip()
        if kind == "pos_pnl_step":
            return f"{float(v.get('step')):+g}%"
        if kind == "gap":
            return f"{float(v.get('gap_pct')):+.1f}%"
        if kind == "rvol_spike":
            return f"{float(v.get('rvol')):.1f}x"
        if kind == "vwap_cross":
            return str(v.get("dir") or "")
    except Exception:
        pass
    return ""


def _fit(parts, limit) -> str:
    out = ""
    for p in parts:
        if not p:
            continue
        cand = p if not out else f"{out} · {p}"
        if len(cand) <= limit:
            out = cand
    return out


def _block(T, st, item, position, events, digest, mode) -> str:
    badges = []
    if position:
        badges.append("Pos")
    if item:
        badges.append("Mason" if str(item.get("origin")) == "opus" else "You")
        if item.get("horizon"):
            badges.append(str(item["horizon"]))
    head = f"{T} [{' · '.join(badges)}]"
    last, chg = _f(st.get("last")), _f(st.get("chg_pct_prev_close"))
    if st.get("no_trades_today"):
        pc = _f(st.get("prior_close"))
        px = "no trades yet today" + (f" (prior close {pc:.2f})" if pc is not None else "")
    else:
        px = (f"last {last:.2f}" + (f" {chg:+.1f}%" if chg is not None else "")) if last is not None else "no tape"
    vs = []
    if st.get("vs_spy_pct") is not None:
        vs.append(f"vs SPY {st['vs_spy_pct']:+.1f}")
    if st.get("vs_sector_pct") is not None and st.get("sector_etf"):
        vs.append(f"vs {st['sector_etf']} {st['vs_sector_pct']:+.1f}")
    first = f"{head} {px}" + (f" ({', '.join(vs)})" if vs else "")
    parts = []
    if st.get("chg_since_pct") is not None:
        parts.append(f"since last run {st['chg_since_pct']:+.1f}%")
    if position:
        pnl = _pos_pnl(position, last)
        e = _f(position.get("entry"))
        if e:
            parts.append(f"pos @ {e:.2f}" + (f" {pnl:+.1f}%" if pnl is not None else ""))
    if st.get("rvol") is not None:
        parts.append(f"rvol {st['rvol']:.1f}")
    if st.get("above_vwap") is not None:
        parts.append("above VWAP" if st["above_vwap"] else "below VWAP")
    o = st.get("opt") or {}
    if o.get("atm_iv") is not None:
        s = f"IV {o['atm_iv']:.0f}"
        if o.get("iv_chg_since_open") is not None:
            s += f" ({o['iv_chg_since_open']:+.0f} since open)"
        if o.get("implied_move_pct") is not None:
            s += f" implied ±{o['implied_move_pct']:.1f}%"
        parts.append(s)
    if events:
        evs = sorted(events, key=lambda e: e["ts"])[-3:]
        parts.append("events: " + "; ".join(
            " ".join(x for x in (e["kind"], _ev_detail(e["kind"], _loads(e.get("value_json"))), _hm(e["ts"])) if x)
            for e in evs))
    if digest and mode != "event":
        t = str(digest).strip()
        parts.append(f"digest: '{t[:60]}{'…' if len(t) > 60 else ''}'")
    if item and item.get("reason"):
        r = str(item["reason"]).strip()
        parts.append(f"why: {r[:60]}{'…' if len(r) > 60 else ''}")
    return _fit([first] + parts, _BLOCK_MAX_CHARS) or first[:_BLOCK_MAX_CHARS]


def tape_section_text(*, since_epoch, max_tokens=1600, mode="anchor") -> str:
    """WATCHLIST TAPE body for the director package (R11a): one block per active item —
    positions first, then Opus items, then Nico's — each ≲60 tokens; items with no events
    since `since_epoch` and |move| < 1% collapse into one 'quiet:' line. Deterministic for
    the same data; clipped to max_tokens (≈ chars/4). '' when disabled."""
    if not enabled():
        return ""
    try:
        budget = max(0, int(max_tokens) * 4)
        wl = _wl()
        if wl is None or budget <= 0:
            return ""
        try:
            items = {str(i["ticker"]).upper(): i for i in (wl.list_items(status="active") or []) if i.get("ticker")}
        except Exception:
            items = {}
        try:
            pos_syms = {str(t).upper() for t in (wl.position_symbols() or [])}
        except Exception:
            pos_syms = set()
        order = (sorted(pos_syms)
                 + sorted(t for t, i in items.items() if t not in pos_syms and str(i.get("origin")) == "opus")
                 + sorted(t for t, i in items.items() if t not in pos_syms and str(i.get("origin")) != "opus"))
        blocks, quiet = [], []
        for T in order:
            try:
                item, position = items.get(T), (_position_for(T) if T in pos_syms else None)
                if T in pos_syms and position is None:
                    position = {"ticker": T}
                st = tape_stats(T, since_ts=since_epoch)
                events = _events_between(T, since_epoch, None, limit=3)
                chg = _f(st.get("chg_pct_prev_close"))
                if T not in pos_syms and not events and (chg is None or abs(chg) < _QUIET_MOVE_PCT):
                    quiet.append(T)
                    continue
                dg = _query("SELECT text FROM watch_digests WHERE ticker = ? ORDER BY created_date DESC LIMIT 1",
                            (T,))
                blocks.append((T, _block(T, st, item, position, events, dg[0]["text"] if dg else None, mode)))
            except Exception as e:
                log.info(f"[watch_signals] tape block {T}: {e}")
        lines, used, clipped = [], 0, []
        for T, b in blocks:
            need = len(b) + (1 if lines else 0)
            if used + need <= budget:
                lines.append(b)
                used += need
            else:
                clipped.append(T)
        if quiet:
            q = "quiet: " + ", ".join(quiet)
            need = len(q) + (1 if lines else 0)
            if used + need <= budget:
                lines.append(q)
                used += need
            else:
                clipped.extend(quiet)
        if clipped:
            for m in (f"(+{len(clipped)} not shown: {', '.join(clipped)})", f"(+{len(clipped)} not shown)"):
                need = len(m) + (1 if lines else 0)
                if used + need <= budget:
                    lines.append(m)
                    break
        if not lines and blocks:
            lines = [blocks[0][1][:budget]]
        return "\n".join(lines)[:budget]
    except Exception as e:
        log.info(f"[watch_signals] tape_section_text: {e}")
        return ""


def watch_ref(ticker) -> dict | None:
    """{"item_id","origin","reason","events_24h"[≤8],"stats"} for recommendations.extra_json
    (R12); positions without an item get origin 'position'. None when not watched."""
    if not enabled():
        return None
    try:
        T = str(ticker or "").upper()
        if not T:
            return None
        wl = _wl()
        item = None
        try:
            item = wl.get_item(T) if wl is not None else None
        except Exception:
            item = None
        if item and str(item.get("status") or "active") != "active":
            item = None
        pos = _position_for(T)
        if not item and not pos:
            return None
        rows = _events_between(T, int(_now()) - 86400, None, limit=8)
        return {"item_id": (item or {}).get("id"),
                "origin": (item or {}).get("origin") or "position",
                "reason": (item or {}).get("reason") or ("held position" if pos else ""),
                "events_24h": [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "severity": r["severity"],
                                "message": r.get("message")} for r in rows],
                "stats": tape_stats(T)}
    except Exception as e:
        log.info(f"[watch_signals] watch_ref {ticker}: {e}")
        return None


def _parse_moment(s):
    """ISO datetime WITH a time → epoch (naive = server-local, treated as ET); date-only → None."""
    if not s or len(str(s).strip()) <= 10:
        return None
    dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp())


def _intraday_day(tp, T, date_str, lo_ts, hi_ts):
    """A synthetic daily bar for [lo_ts, hi_ts] of one session from stored 1m bars, or None."""
    try:
        m = _sorted_bars(tp.bars(T, "1m", since_ts=int(lo_ts), until_ts=int(hi_ts), limit=2000) or [])
    except Exception:
        m = []
    if not m:
        return None
    return {"ts": _midnight_ts(date_str), "o": _f(m[0].get("o")) or float(m[0]["c"]),
            "h": max(float(b.get("h") or b["c"]) for b in m), "l": min(float(b.get("l") or b["c"]) for b in m),
            "c": float(m[-1]["c"]), "intraday": True}


def _parse_day(s):
    """ISO date/datetime → ET 'YYYY-MM-DD' (naive = server-local, treated as ET)."""
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(_ET)
    return dt.strftime("%Y-%m-%d")


def trade_story(ticker, start_iso, end_iso=None, *, max_chars=500, side="long", entry_price=None) -> str:
    """Deterministic hold-period summary (R12) from 1d bars + watch events + option snaps
    (+ the last digest if room): hold return, max favorable / max adverse excursion % with
    their dates, key events, IV path. Entry = entry_price or the first session's open;
    side 'short' flips the excursions. '' when disabled / no bars."""
    if not enabled():
        return ""
    try:
        T = str(ticker or "").upper()
        d0 = _parse_day(start_iso)
        d1 = _parse_day(end_iso) if end_iso else _date_et(_now())
        if not T or not d0 or not d1 or d1 < d0:
            return ""
        tp = _tape()
        if tp is None:
            return ""
        t0, t1 = _midnight_ts(d0), _midnight_ts(d1) + 86399
        bars = []
        try:
            bars = tp.bars(T, "1d", since_ts=t0, until_ts=t1, limit=400) or []
        except Exception:
            bars = []
        if not bars:
            try:
                bars = [b for b in (tp.daily_history(T, days=_DAILY_FETCH) or [])
                        if b.get("ts") is not None and t0 <= int(b["ts"]) <= t1]
            except Exception:
                bars = []
        bars = _sorted_bars(bars)
        if not bars:
            return ""
        # intraday precision (1m bars, kept 30 days): the entry day counts only from the entry
        # time and the exit day only up to the exit time, so the pre-entry / post-exit range of
        # those sessions never shows up as excursion
        e_ts, x_ts = _parse_moment(start_iso), (_parse_moment(end_iso) if end_iso else None)
        if e_ts is not None and _date_et(bars[0]["ts"]) == d0:
            hi_ts = x_ts if (x_ts is not None and d1 == d0) else _midnight_ts(d0) + 86399
            syn = _intraday_day(tp, T, d0, e_ts, hi_ts)
            if syn:
                bars[0] = syn
        if x_ts is not None and _date_et(bars[-1]["ts"]) == d1 and not (d1 == d0 and bars[-1].get("intraday")):
            syn = _intraday_day(tp, T, d1, _midnight_ts(d1), x_ts)
            if syn:
                bars[-1] = syn
        entry = _f(entry_price) or _f(bars[0].get("o")) or _f(bars[0]["c"])
        if not entry:
            return ""
        hb = max(bars, key=lambda b: float(b.get("h") or b["c"]))
        lb = min(bars, key=lambda b: float(b.get("l") or b["c"]))
        hi, lo, end = float(hb.get("h") or hb["c"]), float(lb.get("l") or lb["c"]), float(bars[-1]["c"])
        short = str(side) == "short"
        if short:
            ret = (entry - end) / entry * 100
            mfe, mfe_d = (entry - lo) / entry * 100, lb["ts"]
            mae, mae_d = (entry - hi) / entry * 100, hb["ts"]
        else:
            ret = (end / entry - 1) * 100
            mfe, mfe_d = (hi / entry - 1) * 100, hb["ts"]
            mae, mae_d = (lo / entry - 1) * 100, lb["ts"]
        mfe, mae = max(0.0, mfe), min(0.0, mae)
        md = lambda ts: _et(ts).strftime("%m-%d")
        segs = [f"{T} {'short' if short else 'long'} {d0[5:]}→{d1[5:]} ({len(bars)} sessions): entry "
                f"{entry:.2f} → {end:.2f} ({ret:+.1f}%)",
                f"MFE {mfe:+.1f}% ({md(mfe_d)}), MAE {mae:+.1f}% ({md(mae_d)})"]
        evs = sorted(_events_between(T, t0, t1, limit=50, min_severity=True), key=lambda e: e["ts"])[:4]
        if evs:
            segs.append("events: " + ", ".join(
                " ".join(x for x in (md(e["ts"]), e["kind"], _ev_detail(e["kind"], _loads(e.get("value_json"))))
                         if x) for e in evs))
        try:
            snaps = [s for s in (tp.option_snaps(T, t0) or [])
                     if s.get("ts") is not None and int(s["ts"]) <= t1 and _iv_pct(s.get("atm_iv")) is not None]
            snaps.sort(key=lambda s: int(s["ts"]))
            if snaps:
                segs.append(f"IV {_iv_pct(snaps[0]['atm_iv']):.0f}→{_iv_pct(snaps[-1]['atm_iv']):.0f}")
        except Exception:
            pass
        dg = _query("SELECT text FROM watch_digests WHERE ticker = ? AND date >= ? AND date <= ? "
                    "ORDER BY created_date DESC LIMIT 1", (T, d0, d1))
        if dg and dg[0].get("text"):
            segs.append(f"last digest: '{str(dg[0]['text'])[:100]}'")
        limit = max(0, int(max_chars))
        out = ""
        for s in segs:
            cand = s if not out else f"{out}; {s}"
            if len(cand) <= limit:
                out = cand
            elif not out:
                out = s[:max(0, limit - 1)] + "…" if limit > 1 else ""
                break
        return out
    except Exception as e:
        log.info(f"[watch_signals] trade_story {ticker}: {e}")
        return ""
