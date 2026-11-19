"""
mason_pvr.py — Paper vs Real (RH Expansion S2-T5, goal X-G6).

An honest, running report card: Mason's paper side (Alpaca paper portfolio history +
`recommendations`) next to Nico's real side (Robinhood orders), with no mixing of the two.

  Paper series  Alpaca /account/portfolio/history (the same call as /api/paper/portfolio_history
                in mason_app.py), daily equity → daily returns. Fetched through the module-level
                hook `_fetch_paper_history` (tests monkeypatch it).
  Real series   RH has no portfolio-history tool, so it is RECONSTRUCTED: in-scope `rh_orders`
                (equity only, multi-leg excluded, Household excluded per personal_scope) are replayed
                into daily holdings and valued at `price_bars_daily` adjusted closes
                (mason_price_cache.get_daily). The daily figure is a time-weighted return on
                positions; trade dollars are netted as flows, so deposits/withdrawals/idle cash are
                excluded by design (said in `caveats`). From go-live it is reconciled against the
                latest `rh_account_snaps.positions_value`; drift > 1% → stale:true + reason.
  Overlap       Each `recommendations` row (READ-ONLY) with an in-scope RH order in the same
                ticker + direction within ±10 sessions → equal-length windows from each side's own
                entry, both graded by mason_price_cache.fwd_return (the one grading function).
                winner = mason | nico | tie (within 0.5 pct-pt); unmatured → null (pending).

This module writes no rows and makes no Robinhood calls; it only reads the DB.
Contract: docs/robinhood/RH_EXPANSION_CONTRACTS.md §6 GET /api/rh/pvr?range=.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import mason_db
import mason_robinhood as _RHN          # norm_account_label only (T-D6 row-side normalization)

log = logging.getLogger("mason.pvr")
ET = ZoneInfo("America/New_York")
CONFIG = Path(__file__).resolve().parent / "mason.config.json"
NAME = "mason_pvr"

_DEFAULTS = {
    "enabled": False,
    "overlap_window_sessions": 20,     # matched-window length (sessions from each side's entry)
    "overlap_match_sessions": 10,      # rec ↔ RH order must be within ±N sessions
    "tie_pct": 0.5,                    # |paper − real| ≤ this (pct-pt) → tie
    "drift_pct": 1.0,                  # reconcile tolerance vs rh_account_snaps.positions_value
    "default_range": "3M",
    "cache_s": 600,
    "refresh_at_et": "16:50",
}
_RANGES = {"1M": ("1M", 31), "3M": ("3M", 92), "6M": ("6M", 183), "1Y": ("1A", 366),
           "YTD": (None, None), "ALL": ("5A", None)}
_MAX_ORDERS = 50000                    # bounded read of rh_orders (truncation surfaces in status)
_MAX_RECS = 5000
_MAX_TICKERS = 400
_FILLED_STATES = {"", "filled", "partially_filled", "confirmed"}
_BUY_DIRS = {"long", "buy"}
_SELL_DIRS = {"short", "trim", "exit", "sell"}

CAVEATS = [
    "Real returns are reconstructed from Robinhood orders valued at daily adjusted closes; "
    "Robinhood has no portfolio-history feed.",
    "Time-weighted return on positions only: deposits, withdrawals and idle cash are excluded by "
    "design, so this is not account-level performance.",
    "Equity positions only: options and crypto are not priced in the real series.",
    "Dividend reinvestment buys are treated as ordinary position changes.",
    "Paper is Alpaca paper trading and never enters personal stats.",
    "Paper is whole-account Alpaca equity (cash included); real is positions only.",
    "Head-to-head compares matched windows of equal length from each side's own entry; unmatured "
    "windows are pending and not counted.",
]

# ── config (read per call, mtime-cached) ──────────────────────────────────────
_cfg_cache = {"mtime": None, "data": {}}
_lock = threading.Lock()
_cache: dict = {}                      # range → (ts, envelope)
_status = {"last_run": None, "rows": 0, "errors": [], "truncated": False, "skipped_fills": 0,
           "snapped_fills": 0}


def _config() -> dict:
    """mason.config.json, mtime-cached. A half-written file (an editor's truncate-then-write
    race) never yields an empty config: up to 3 quick re-reads, then the last good copy; the
    mtime is only advanced on a successful parse, so the next call re-reads."""
    for attempt in range(3):
        try:
            mt = Path(CONFIG).stat().st_mtime
            if _cfg_cache["mtime"] != mt:
                data = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
                _cfg_cache["data"], _cfg_cache["mtime"] = data, mt
            return _cfg_cache["data"] or {}
        except Exception:
            if _cfg_cache["data"]:
                return _cfg_cache["data"]
            time.sleep(0.02 * (attempt + 1))
    return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        node = ((_config().get("rh_expansion") or {}).get("pvr") or {})
        out.update({k: v for k, v in node.items() if v is not None and not str(k).startswith("_")})
    except Exception:
        pass
    return out


def _excluded(errors: list | None = None) -> list:
    """personal_scope.exclude_accounts — fails CLOSED (privacy), parsed by the shared
    mason_robinhood.parse_exclude_accounts (Finalization RULINGS #1/#2, ratified R8).
    An explicit [] means include everyone; missing / null → Household. A bare string (even "Roth IRA")
    or any unusable entry ([None], [5], ["  "], …) or a non-list value keeps the usable labels,
    ADDS "Household" and records an error. Returns display labels (first spelling, trimmed)."""
    import unicodedata
    import mason_robinhood as _RH

    def disp(x):
        x = unicodedata.normalize("NFKC", x)
        return "".join(ch for ch in x if ord(ch) not in _RH._INVISIBLE_FILLERS
                       and unicodedata.category(ch) != "Cf").strip()
    try:
        rx = _config().get("rh_expansion")
        ps = rx.get("personal_scope") if isinstance(rx, dict) else None
        raw = None
        if (rx is not None and not isinstance(rx, dict)) or (ps is not None and not isinstance(ps, dict)):
            excl, err = frozenset({"household"}), "personal_scope.exclude_accounts unreadable (not an object)"
        else:
            present = isinstance(ps, dict) and "exclude_accounts" in ps
            raw = ps.get("exclude_accounts") if present else None
            excl, err = _RH.parse_exclude_accounts(raw, present=present)
        if err and errors is not None:
            errors.append(f"{err}; Household excluded by default")
        out, seen = [], set()
        for x in (raw if isinstance(raw, list) else [raw]):
            n = _RH.norm_account_label(x)
            if n and n in excl and n not in seen:
                seen.add(n)
                out.append(disp(x))
        if "household" in excl and "household" not in seen:
            out.append("Household")
        for lab in sorted(_RH.role_excluded_labels()):          # A2: labels the last-4 role map excludes
            if lab not in seen and lab != "household":
                seen.add(lab)
                out.append(lab)
        return out
    except Exception:
        if errors is not None:
            errors.append("personal_scope.exclude_accounts unreadable; Household excluded by default")
        out = ["Household"]
        try:                                                    # A2 (QA R3): the failure path never drops map labels
            out += sorted(x for x in _RH.role_excluded_labels() if x != "household")
        except Exception:
            pass
        return out


def _model_names(labels) -> list:
    """N6b: the excluded display labels as a model (or any text consumer) may see them: the ••NNNN mask dropped, duplicates
    collapsed, order kept. ONLY the rendered report uses this; _excluded() keeps the exact labels the filter matches on."""
    out = []
    for x in labels or []:
        n = _RHN.brain_label(x)
        if n not in out:
            out.append(n)
    return out


def enabled() -> bool:
    """robinhood.enabled is the master switch (contracts §7), then rh_expansion.pvr.enabled."""
    try:
        c = _config()
        return bool((c.get("robinhood") or {}).get("enabled")) and bool(_cfg().get("enabled"))
    except Exception:
        return False


def _reset_cache():
    with _lock:
        _cache.clear()


def _reset():
    _reset_cache()
    _cfg_cache.update(mtime=None, data={})
    _status.update(last_run=None, rows=0, errors=[], truncated=False, skipped_fills=0,
                   snapped_fills=0)


def _today() -> date:
    return datetime.now(ET).date()


# ── small helpers ─────────────────────────────────────────────────────────────
def _f(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _et_date(iso) -> str | None:
    """UTC ISO (or YYYY-MM-DD) → ET calendar date string."""
    try:
        s = str(iso or "").strip()
        if len(s) == 10:
            return date.fromisoformat(s).isoformat()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).date().isoformat()
    except Exception:
        return None


def _pct(x, nd=4):
    return None if x is None or not math.isfinite(x) else round(x * 100.0, nd)


def _price_cache():
    import mason_price_cache            # lazy: S1-T3 module (contract signatures only)
    return mason_price_cache


def _get_daily(ticker, start, end) -> list:
    return list(_price_cache().get_daily(ticker, start, end) or [])


def _fwd_return(ticker, entry_date, *, sessions, entry_px=None) -> dict:
    return _price_cache().fwd_return(ticker, entry_date, sessions=sessions, entry_px=entry_px) or {}


# ── paper (Alpaca) ────────────────────────────────────────────────────────────
def _fetch_paper_history(period: str, timeframe: str = "1D") -> dict:
    """Network hook. Same endpoint + params as mason_app.api_paper_portfolio_history
    (`{base}/account/portfolio/history`, period, timeframe, extended_hours) with daily bars.
    Raises on any failure; the caller turns that into `paper: null` + an error."""
    import requests
    cfg = _config()
    pt = cfg.get("paper_trading") or {}
    import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
    key, secret = _S.alpaca_keys(cfg=cfg)
    if not key:
        raise RuntimeError("Alpaca not configured")
    base = str(pt.get("base_url") or "https://paper-api.alpaca.markets/v2").rstrip("/")
    r = requests.get(f"{base}/account/portfolio/history",
                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                     params={"period": period, "timeframe": timeframe, "extended_hours": "true"},
                     timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Alpaca portfolio history HTTP {r.status_code}")
    return r.json()


def _paper_daily(period: str) -> tuple:
    """→ ({date: daily_ret or None}, base_value, error|None). Gaps (null equity) give None for
    that date; the next return is measured against the last known equity."""
    try:
        raw = _fetch_paper_history(period, "1D")
        if not isinstance(raw, dict) or not isinstance(raw.get("timestamp"), list):
            return None, None, "Alpaca portfolio history: unexpected payload"
        ts, eq = raw.get("timestamp") or [], raw.get("equity") or []
        out, last = {}, None
        for i, t in enumerate(ts[:5000]):
            try:
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(ET).date().isoformat()
            except Exception:
                continue                  # one bad point is skipped, not the whole series
            e = _f(eq[i]) if i < len(eq) else None
            if e is None or e <= 0:
                out.setdefault(d, None)
                continue
            out[d] = (e / last - 1.0) if last else None
            last = e
        if not out:
            return None, None, "Alpaca portfolio history: no data"
        return out, _f(raw.get("base_value")), None
    except Exception as e:
        return None, None, f"Alpaca portfolio history unavailable: {str(e)[:120]}"


# ── DB reads (read-only) ──────────────────────────────────────────────────────
def _ex_norm() -> set:
    """_excluded() display labels → the normalized set (mason_robinhood.norm_account_label, T-D6)."""
    return {n for n in (_RHN.norm_account_label(x) for x in _excluded()) if n}


def _norm_fn(conn) -> None:
    """Register mason_norm_label(label) on this connection = mason_robinhood.norm_account_label (pure)."""
    conn.create_function("mason_norm_label", 1, _RHN.norm_account_label, deterministic=True)


def _read_orders(errors: list) -> list:
    """In-scope, filled, single-leg equity orders → [{acct, sym, side, qty, px, date, drip}]."""
    ex = _ex_norm()
    states = sorted(_FILLED_STATES)
    conn = None
    try:
        conn = mason_db.get_connection()
        # scope + state filters run in SQL BEFORE the LIMIT, so a truncation can only ever drop
        # in-scope filled rows (Python re-checks below as defence in depth). The account label is
        # normalized in SQL by the SAME Python function as the config side (T-D6, QA DEBUG_T-E1).
        _norm_fn(conn)
        ex_sql = ",".join("?" * len(ex)) or "''"
        rows = conn.execute(
            "SELECT account_label, symbol, side, qty, avg_price, filled_at, state, is_drip "
            "FROM rh_orders WHERE COALESCE(record_deleted,0)=0 AND kind='equity' "
            "AND COALESCE(is_multileg,0)=0 "
            f"AND mason_norm_label(COALESCE(account_label,'Unknown')) NOT IN ({ex_sql}) "
            f"AND LOWER(TRIM(COALESCE(state,''))) IN ({','.join('?' * len(states))}) "
            "ORDER BY filled_at, id LIMIT ?",
            (*sorted(ex), *states, _MAX_ORDERS + 1)).fetchall()
    except Exception as e:
        errors.append(f"rh_orders read failed (db): {str(e)[:120]}")
        return []
    finally:
        if conn is not None:
            conn.close()
    if len(rows) > _MAX_ORDERS:
        rows = rows[:_MAX_ORDERS]
        _status["truncated"] = True
        errors.append(f"rh_orders truncated at {_MAX_ORDERS} rows")
    out = []
    for r in rows:
        acct = str(r["account_label"] or "Unknown")
        if _RHN.norm_account_label(acct) in ex:
            continue
        if str(r["state"] or "").strip().lower() not in _FILLED_STATES:
            continue
        sym = str(r["symbol"] or "").strip().upper()
        side = str(r["side"] or "").lower()
        qty, d = _f(r["qty"]), _et_date(r["filled_at"])
        if not sym or side not in ("buy", "sell") or qty is None or qty <= 0 or not d:
            continue                      # qty=0 / malformed rows are harmless no-ops
        out.append({"acct": acct, "sym": sym, "side": side, "qty": qty,
                    "px": _f(r["avg_price"]), "date": d, "drip": bool(r["is_drip"])})
    return out


def _read_recs(start: str, errors: list) -> list:
    conn = None
    try:
        conn = mason_db.get_connection()
        rows = conn.execute(
            "SELECT id, created_date, ticker, direction, entry_ref_price FROM recommendations "
            "WHERE COALESCE(record_deleted,0)=0 AND COALESCE(instrument,'equity')='equity' "
            "ORDER BY created_date, id LIMIT ?", (_MAX_RECS,)).fetchall()
    except Exception as e:
        errors.append(f"recommendations read failed (db): {str(e)[:120]}")
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for r in rows:
        d, dirn = _et_date(r["created_date"]), str(r["direction"] or "").lower()
        if not d or d < start or not r["ticker"]:
            continue
        side = "buy" if dirn in _BUY_DIRS else "sell" if dirn in _SELL_DIRS else None
        if side:
            out.append({"id": str(r["id"]), "date": d, "sym": str(r["ticker"]).upper(),
                        "dir": dirn, "side": side, "px": _f(r["entry_ref_price"])})
    return out


def _read_snaps(start: str, end: str, errors: list) -> dict:
    """Latest in-scope rh_account_snaps row per account within [start, end]."""
    ex = _ex_norm()
    conn = None
    try:
        conn = mason_db.get_connection()
        rows = conn.execute(
            "SELECT account_label, snap_date, positions_value FROM rh_account_snaps "
            "WHERE snap_date >= ? AND snap_date <= ? ORDER BY snap_date", (start, end)).fetchall()
    except Exception as e:
        errors.append(f"rh_account_snaps read failed (db): {str(e)[:120]}")
        return {}
    finally:
        if conn is not None:
            conn.close()
    out = {}
    for r in rows:
        a = str(r["account_label"] or "")
        if a and _RHN.norm_account_label(a) not in ex and _f(r["positions_value"]) is not None:
            out[a] = (str(r["snap_date"]), _f(r["positions_value"]))
    return out


# ── real-series reconstruction ────────────────────────────────────────────────
# Common split ratios a fill price may differ from the bar `close` by (n:1 and 1:n reverse).
_SPLIT_RATIOS = sorted({float(n) for n in (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)}
                       | {1.5} | {1.0 / n for n in (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40,
                                                       50, 100)} | {2.0 / 3.0})
_SAME_BASIS = (0.67, 1.5)              # fill/close inside this band → same share basis (s = 1)
_SNAP_TOL = 0.10                       # |ratio/split − 1| ≤ 10% → snap to that split ratio


def _snap_ratio(r):
    """Nearest common split ratio within _SNAP_TOL of r, else None."""
    if r is None or not math.isfinite(r) or r <= 0:
        return None
    best = min(_SPLIT_RATIOS, key=lambda k: abs(math.log(r / k)))
    return best if abs(r / best - 1.0) <= _SNAP_TOL else None


def _xcheck(row) -> dict:
    try:
        x = row.get("xcheck_json")
        x = json.loads(x) if isinstance(x, str) else x
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


class _Px:
    """Per-ticker bars normalised to ONE convention whatever S1 stores in `close`.

    `close` may be raw as-traded or already split-adjusted (yfinance auto_adjust=False is
    split-adjusted). Splits in a raw series show up as a jump in close/adj_close between two
    sessions; those jumps are snapped to a split ratio and divided out, giving `sclose` =
    split-adjusted, dividend-UNadjusted close (today's share basis). `adj` (split+dividend
    adjusted) is untouched and drives the time-weighted return; `sclose` drives the share basis
    of fills and the market-value reconcile."""

    def __init__(self, rows):
        # Real S1 rows (mason_price_cache.get_daily) carry xcheck_json: the return-math close is
        # yfinance's when it has the row (S1 _px), and the dividend sits on the ex-date (`div`).
        # A stored adj_close is only coherent within ONE S1 fetch, so with xcheck data the
        # dividend-adjusted series is rebuilt from close + div (S1's own _div_factor chain).
        # Rows without xcheck data (older/foreign rows, test doubles) fall back to adj_close.
        good, has_x = [], False
        for r in rows or []:
            x = _xcheck(r)
            has_x = has_x or bool(x)
            yc = _f(x.get("yf_close"))
            c = yc if yc is not None and yc > 0 else _f(r.get("close"))
            a = _f(r.get("adj_close"))
            if r.get("date") and c is not None and c > 0:
                good.append((str(r["date"])[:10], c, a if a is not None and a > 0 else c,
                             _f(x.get("div"), 0.0) or 0.0))
        good.sort()
        self.dates = [g[0] for g in good]
        close = [g[1] for g in good]
        if has_x and good:
            tr = [close[0]]
            for i in range(1, len(good)):
                dv, prev = good[i][3], close[i - 1]
                f = 1.0 / (1.0 - dv / prev) if 0 < dv < prev else 1.0
                tr.append(tr[-1] * close[i] / prev * f)
            scale = close[-1] / tr[-1]
            self.adj = [t * scale for t in tr]
        else:
            self.adj = [g[2] for g in good]
        # cumulative split factor AFTER each session (raw-close convention only)
        k = [1.0] * len(good)
        for i in range(len(good) - 1, 0, -1):
            jump = (close[i] / self.adj[i]) / (close[i - 1] / self.adj[i - 1])
            split = _snap_ratio(jump) if abs(jump - 1.0) > 0.25 else None
            k[i - 1] = k[i] * (split or 1.0)
        self.sclose = [c * kk for c, kk in zip(close, k)]

    def _idx(self, d):
        return bisect.bisect_right(self.dates, d) - 1

    def _near(self, d):
        i = self._idx(d)
        return 0 if i < 0 else i

    def adj_on(self, d):
        i = self._idx(d)
        return self.adj[i] if i >= 0 else None

    def sclose_on(self, d):
        i = self._idx(d)
        return self.sclose[i] if i >= 0 else None

    def snap_fill(self, d, qty, fill):
        """→ (qty_b, px_b, div_factor) in the bar basis, or None if the fill can't be snapped.
        div_factor = sclose/adj on the fill session converts bar-basis shares to adj-basis."""
        if not self.dates:
            return None
        i = self._near(d)
        c = self.sclose[i]
        if not fill or fill <= 0:
            fill = c
        r = fill / c
        if _SAME_BASIS[0] <= r < _SAME_BASIS[1]:
            sp = 1.0
        else:
            sp = _snap_ratio(r)
            if sp is None:
                return None
        return qty * sp, fill / sp, c / self.adj[i]


def _reconstruct(orders: list, cal: list, start: str, px: dict, counters: dict) -> dict:
    """Replay one account's orders over the session calendar.
    Returns {date: (V_prev, V, B, S, Vmkt)} for every calendar date >= start:
      V      adj-basis position value at that close (drives the time-weighted return)
      B, S   the day's buy / sell flows, netted out of the return
      Vmkt   bar-basis shares × split-adjusted close (market value; reconcile only)
    Two ledgers per ticker: hold_b (bar-basis shares) and hold_a (adj-basis shares). A sell
    removes the same fraction from both; its flow is booked in adj terms so that a sale at the
    close is return-neutral (dividends already credited through adj_close are not re-counted)."""
    hold_b: dict = {}
    hold_a: dict = {}
    by_day: dict = {}
    for o in orders:
        # map a fill to the first session on/after its ET date (weekend fills roll forward)
        k = bisect.bisect_left(cal, o["date"])
        if k >= len(cal):
            continue
        by_day.setdefault(cal[k], []).append(o)

    def value(d):
        v = vm = 0.0
        for t, sh in hold_a.items():
            if sh > 1e-12 and t in px:
                a = px[t].adj_on(d)
                if a:
                    v += sh * a
                c = px[t].sclose_on(d)
                if c:
                    vm += hold_b.get(t, 0.0) * c
        return v, vm

    out = {}
    v_prev = 0.0
    for d in cal:
        b = s = 0.0
        for o in by_day.get(d, []):
            p = px.get(o["sym"])
            if p is None or not p.dates:
                counters["unpriced"].add(o["sym"])
                continue
            snapped = p.snap_fill(d, o["qty"], o["px"])
            if snapped is None:
                counters["skipped_fills"] += 1
                continue
            qb, pb, f = snapped
            if abs(qb - o["qty"]) > 1e-9:
                counters["snapped_fills"] += 1
            t = o["sym"]
            if o["side"] == "buy":
                hold_b[t] = hold_b.get(t, 0.0) + qb
                hold_a[t] = hold_a.get(t, 0.0) + qb * f
                b += qb * pb
            else:
                have = hold_b.get(t, 0.0)
                if qb > have + 1e-9:
                    counters["unmatched_sells"] += 1
                    qb = have
                if have <= 1e-12 or qb <= 0:
                    continue
                frac = qb / have
                sold_a = hold_a.get(t, 0.0) * frac
                hold_b[t] = have - qb
                hold_a[t] = hold_a.get(t, 0.0) - sold_a
                a_d, c_d = p.adj_on(d), p.sclose_on(d)
                if a_d and c_d:
                    s += sold_a * a_d * (pb / c_d)
                else:
                    s += qb * pb
        v, vm = value(d)
        if d >= start:
            out[d] = (v_prev, v, b, s, vm)
        v_prev = v
    return out


def _daily_ret(v_prev, v, b, s) -> float:
    den = v_prev + b
    if den <= 0:
        return 0.0                        # no positions → 0%, never NaN
    r = (v - v_prev - b + s) / den
    return r if math.isfinite(r) else 0.0


# ── calendar + range ──────────────────────────────────────────────────────────
def _range_start(rng: str, today: date, earliest: str | None) -> str:
    if rng == "YTD":
        return date(today.year, 1, 1).isoformat()
    if rng == "ALL":                      # 5-year floor; `earliest` (if any) narrows it
        floor = (today - timedelta(days=5 * 366)).isoformat()
        return max(earliest or floor, floor)
    return (today - timedelta(days=_RANGES[rng][1])).isoformat()


def _paper_period(rng: str, start: str, today: date) -> str:
    if rng == "YTD":
        return f"{max(1, (today - date.fromisoformat(start)).days + 1)}D"
    return _RANGES[rng][0]


def _calendar(lo: str, end: str, errors: list) -> list:
    """Session dates from SPY bars (authoritative); weekday fallback when unavailable."""
    try:
        rows = _get_daily("SPY", lo, end)
        ds = sorted({str(r.get("date"))[:10] for r in rows if r.get("date")})
        if ds:
            return ds
    except Exception as e:
        errors.append(f"price cache unavailable (SPY calendar): {str(e)[:100]}")
    out, d = [], date.fromisoformat(lo)
    stop = date.fromisoformat(end)
    while d <= stop and len(out) < 3000:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ── overlap ───────────────────────────────────────────────────────────────────
def _signed(ret, side):
    if ret is None:
        return None
    return -ret if side == "sell" else ret


def _window(sym, d, side, px, n, errors=None) -> dict:
    try:
        g = _fwd_return(sym, d, sessions=n, entry_px=px)
    except Exception as e:
        log.warning("[pvr] fwd_return %s %s failed: %s", sym, d, e)
        g = {}
    if g.get("error") and errors is not None and len(errors) < 50:
        errors.append(f"overlap grade {sym} {d}: {str(g['error'])[:60]}")
    matured = bool(g.get("matured")) and _f(g.get("ret")) is not None
    return {"entry_date": g.get("entry_date") or d, "entry_px": _f(g.get("entry_px"), px),
            "exit_date": g.get("exit_date") if matured else None,
            "exit_px": _f(g.get("exit_px")) if matured else None,
            "ret_pct": _pct(_signed(_f(g.get("ret")), side)) if matured else None,
            "matured": matured}


def _overlap(recs, orders, cal, c, errors=None) -> list:
    n_win = max(1, int(c.get("overlap_window_sessions") or 20))   # S1: sessions must be >= 1
    n_match = int(c.get("overlap_match_sessions") or 10)
    tie = float(c.get("tie_pct") or 0.5)

    def sidx(d):
        return bisect.bisect_left(cal, d)

    decisions = [o for o in orders if not o["drip"]]
    by_key: dict = {}
    for j, o in enumerate(decisions):
        by_key.setdefault((o["sym"], o["side"]), []).append((j, o))
    # one-to-one greedy matching: every rec and every real order is used at most once, pairs
    # taken by smallest session gap (tie-break: earliest rec, rec id, earliest order)
    cands = []
    for r in recs:
        ri = sidx(r["date"])
        for j, o in by_key.get((r["sym"], r["side"]), []):
            gap = abs(sidx(o["date"]) - ri)
            if gap <= n_match:
                cands.append((gap, r["date"], r["id"], o["date"], j, r, o))
    cands.sort(key=lambda x: x[:5])
    used_r, used_o, pairs = set(), set(), {}
    for gap, _rd, rid, _od, j, r, o in cands:
        if rid in used_r or j in used_o:
            continue
        used_r.add(rid)
        used_o.add(j)
        pairs[rid] = (gap, o)
    out = []
    for r in recs:
        if r["id"] not in pairs:
            continue
        gap, o = pairs[r["id"]]
        paper = _window(r["sym"], r["date"], r["side"], r["px"], n_win, errors)
        real = _window(o["sym"], o["date"], o["side"], o["px"], n_win, errors)
        real = {"account": o["acct"], **real}
        diff = winner = None
        if paper["matured"] and real["matured"]:
            diff = round(paper["ret_pct"] - real["ret_pct"], 4)
            winner = "tie" if abs(diff) <= tie else ("mason" if diff > 0 else "nico")
        out.append({"ticker": r["sym"], "direction": r["dir"], "rec_id": r["id"],
                    "sessions_apart": gap, "window_sessions": n_win,
                    "paper": paper, "real": real, "diff_pct": diff, "winner": winner})
    return out


# ── the report ────────────────────────────────────────────────────────────────
def _compute(rng: str) -> dict:
    c = _cfg()
    errors: list = []
    stale = False
    _status["truncated"] = False
    today = _today()
    if rng not in _RANGES:
        errors.append("unknown range; using 3M")
        rng = "3M" if "3M" in _RANGES else str(c.get("default_range"))

    orders = _read_orders(errors)
    if rng == "ALL":
        paper_map, base_value, perr = _paper_daily(_RANGES["ALL"][0])
        recs = _read_recs("0000-00-00", errors)
        cands = [x for x in (min((o["date"] for o in orders), default=None),
                             min(paper_map) if paper_map else None,
                             recs[0]["date"] if recs else None) if x]
        floor = _range_start("ALL", today, None)
        start = max(min(cands), floor) if cands else today.isoformat()
    else:
        start = _range_start(rng, today, None)
        paper_map, base_value, perr = _paper_daily(_paper_period(rng, start, today))
        recs = _read_recs(start, errors)
    if perr:
        errors.append(perr)
    end = today.isoformat()

    # price everything ever held up to `end` (holdings at range start depend on older orders)
    lo = (date.fromisoformat(start) - timedelta(days=14)).isoformat()
    orders = [o for o in orders if o["date"] <= end]
    tickers = sorted({o["sym"] for o in orders})[:_MAX_TICKERS]
    first_fill = {}
    for o in orders:
        first_fill.setdefault(o["sym"], o["date"])
    hist_lo = min([lo] + list(first_fill.values()))
    try:
        _pc = _price_cache()
        if hasattr(_pc, "enabled") and not _pc.enabled():
            errors.append("price cache is off (rh_expansion.price_cache.enabled); real side unpriced")
    except Exception as e:
        errors.append(f"price cache unavailable: {str(e)[:80]}")
    cal_all = _calendar(hist_lo, end, errors)
    px = {}
    for t in tickers:
        try:
            px[t] = _Px(_get_daily(t, min(lo, first_fill[t]), end))
        except Exception as e:
            errors.append(f"price cache unavailable for {t}: {str(e)[:80]}")
    last_bar = max((p.dates[-1] for p in px.values() if p.dates), default=None)
    if last_bar and cal_all and cal_all[-1] > last_bar:
        cal_all = [d for d in cal_all if d <= last_bar]   # no carry-forward past the last close
    series_dates = [d for d in cal_all if d >= start]

    excluded = sorted(_excluded(errors))
    accts = sorted({o["acct"] for o in orders})
    counters = {"unmatched_sells": 0, "unpriced": set(), "skipped_fills": 0,
                "snapped_fills": 0}
    per = {a: _reconstruct([o for o in orders if o["acct"] == a], cal_all, start, px, counters)
           for a in accts}
    if counters["skipped_fills"]:
        errors.append(f"{counters['skipped_fills']} fill(s) skipped: price not reconcilable with "
                      "the bar close (no plausible split ratio)")
    _status["skipped_fills"] = counters["skipped_fills"]
    _status["snapped_fills"] = counters["snapped_fills"]
    if counters["unpriced"]:
        errors.append("no price bars for: " + ", ".join(sorted(counters["unpriced"])[:20]))

    series, cum = [], {a: 1.0 for a in accts + ["all"]}
    pcum, pdays = 1.0, 0
    pkeys = sorted(paper_map) if paper_map else []
    prev_d = None
    for d in series_dates:
        rr = {}
        tot = [0.0, 0.0, 0.0, 0.0]
        for a in accts:
            vp, v, b, s = per[a].get(d, (0.0, 0.0, 0.0, 0.0, 0.0))[:4]
            r = _daily_ret(vp, v, b, s)
            rr[a] = r
            cum[a] *= 1 + r
            for i, x in enumerate((vp, v, b, s)):
                tot[i] += x
        rr["all"] = _daily_ret(*tot)
        cum["all"] *= 1 + rr["all"]
        # every Alpaca day in (previous series date, d] compounds into d — a session missing from
        # the price calendar must not drop a paper return (both sides stay like-for-like)
        lo_k = bisect.bisect_right(pkeys, prev_d) if prev_d else bisect.bisect_left(pkeys, start)
        hi_k = bisect.bisect_right(pkeys, d)
        pr = None
        for pk in pkeys[lo_k:hi_k]:
            x = paper_map[pk]
            if x is not None:
                pr = (1 + (pr or 0.0)) * (1 + x) - 1
                pdays += 1
        if pr is not None:
            pcum *= 1 + pr
        prev_d = d
        series.append({"date": d, "paper_ret": _pct(pr),
                       "paper_cum": _pct(pcum - 1) if paper_map else None,
                       "real_ret": {k: _pct(v) for k, v in rr.items()},
                       "real_cum": {k: _pct(cum[k] - 1) for k in rr}})

    # reconcile vs rh_account_snaps (go-live forward)
    reconcile = []
    drift_tol = float(c.get("drift_pct") or 1.0)
    for a, (sd, pv) in sorted(_read_snaps(start, end, errors).items()):
        k = bisect.bisect_right(cal_all, sd) - 1
        rv = 0.0
        if a in per and k >= 0:
            rv = per[a].get(cal_all[k], (0.0, 0.0, 0.0, 0.0, 0.0))[4]   # market value
        if pv <= 0:                       # zero/negative RH value can't validate anything
            drift, ok = None, False
            reason = f"RH positions_value is {pv:.2f} on {sd} ({a}); reconcile not possible"
        else:
            drift = abs(rv - pv) / pv * 100.0
            ok = drift <= drift_tol
            reason = f"reconstruction drift {drift:.2f}% vs RH positions_value on {sd} ({a})"
        row = {"account": a, "snap_date": sd, "recon_value": round(rv, 2),
               "positions_value": round(pv, 2),
               "drift_pct": round(drift, 4) if drift is not None else None, "ok": ok}
        reconcile.append(row)
        if not ok:
            stale = True
            errors.append(reason)

    overlap = _overlap(recs, orders, cal_all, c, errors) if recs else []
    done = [o for o in overlap if o["winner"]]
    h2h = {"mason": sum(o["winner"] == "mason" for o in done),
           "nico": sum(o["winner"] == "nico" for o in done),
           "tie": sum(o["winner"] == "tie" for o in done),
           "pending": len(overlap) - len(done)}
    n = len(done)
    hit = {"paper": round(sum(o["paper"]["ret_pct"] > 0 for o in done) / n, 4) if n else None,
           "real": round(sum(o["real"]["ret_pct"] > 0 for o in done) / n, 4) if n else None,
           "n": n}
    paper_cum = _pct(pcum - 1) if paper_map else None
    data = {
        "range": rng, "start": start, "end": series_dates[-1] if series_dates else end,
        "series": series,
        "paper": ({"base_value": base_value, "cum_pct": paper_cum, "n_days": pdays}
                  if paper_map else None),
        "overlap": overlap,
        "summary": {
            "units": "pct",
            "paper_cum_pct": paper_cum,
            "real_cum_pct": {k: _pct(v - 1) for k, v in cum.items()},
            "hit_rate": hit,
            "head_to_head": h2h,
            "accounts_in_scope": accts,
            "excluded_accounts": _model_names(excluded),
            "reconcile": reconcile,
            "unmatched_sells": counters["unmatched_sells"],
        },
        "caveats": list(CAVEATS),
    }
    _status.update(last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   rows=len(orders), errors=list(errors)[:20])
    return {"ok": True, "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": data, "stale": stale, "errors": errors}


def report(rng: str | None = None) -> dict:
    """The /api/rh/pvr envelope. {"enabled": False} when off. Never raises."""
    if not enabled():
        return {"enabled": False}
    try:
        # D4: ?range= is user input — normalize to a known key BEFORE the cache/Alpaca
        want = str(rng or _cfg().get("default_range") or "3M").strip().upper()
        key = want if want in _RANGES else "3M"
        # D3: key on the config file's mtime so a scope flip applies on the next request
        _config()
        ckey = (key, _cfg_cache["mtime"])
        ttl = float(_cfg().get("cache_s") or 0)
        with _lock:
            hit = _cache.get(ckey)
        if hit and time.time() - hit[0] < ttl:
            env = hit[1]
        else:
            env = _compute(key)
            with _lock:
                for k in [k for k in _cache if k[1] != ckey[1]]:
                    _cache.pop(k, None)   # drop entries from an older config
                _cache[ckey] = (time.time(), env)
        if want != key:
            env = dict(env, errors=list(env.get("errors") or []) + ["unknown range; using 3M"])
        return dict(env)
    except Exception as e:
        log.error("[pvr] report failed: %s", e, exc_info=True)
        _status["errors"] = [f"report failed: {str(e)[:120]}"]
        return {"ok": False, "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data": None, "stale": True, "errors": [f"report failed: {str(e)[:120]}"]}


def refresh():
    """Scheduled job body: recompute the default range into the cache. No-op when off."""
    if not enabled():
        return None
    _reset_cache()
    env = report(None)
    return {"errors": len(env.get("errors") or []), "stale": env.get("stale")}


def jobs() -> list:
    if not enabled():
        return []
    return [{"name": "rhx_pvr_daily", "at_et": str(_cfg().get("refresh_at_et") or "16:50"),
             "fn": refresh}]


def status() -> dict:
    return {"name": NAME, "enabled": enabled(), "last_run": _status["last_run"],
            "rows": _status["rows"], "errors": list(_status["errors"]),
            "truncated": bool(_status["truncated"]), "rate_hits": 0, "xcheck_mismatch": 0,
            "skipped_fills": int(_status.get("skipped_fills") or 0),
            "snapped_fills": int(_status.get("snapped_fills") or 0)}


# ── HTTP ──────────────────────────────────────────────────────────────────────
bp = Blueprint("mason_pvr", __name__)


@bp.route("/api/rh/pvr")
def api_rh_pvr():
    return jsonify(report(request.args.get("range")))
