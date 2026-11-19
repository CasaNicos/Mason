"""
mason_rh_history.py — Nico's real Robinhood history: orders, realized trades, account snapshots,
alert log, Trade Stories and personal priors (RH Expansion S1-T2, X-G1).

Spec: docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §2 T2. Contracts: RH_EXPANSION_CONTRACTS.md
§2 (boundaries), §5 (rh_orders / rh_trades / rh_account_snaps / rh_alert_log), §6 (routes), §7 (config).
Rulings: docs/robinhood/expansion/S1/REVIEW_T1.md (natural_v1 trade key, synthetic option shape,
fail-safe multi-leg, crypto "no crypto account" = 0 rows, DRIP = placed_agent "drip").

  - backfill()           every tracked account (Household too; scope is applied at READ time) → rh_orders
                         (equity, option legs, crypto) + rh_trades (get_pnl_trade_history). Walks use
                         mason_robinhood.paged_all(lane="bulk"). Idempotent (hash keys + upsert) and
                         resumable: desk_state rhx_backfill_<last4>_<tool> records each (account, tool)
                         walk of the current run; a walk with errors or truncation is NOT marked done,
                         so the next run re-walks only what is missing.
  - match_trades()       FIFO-matches every realized trade against opening fills in the same account
                         and instrument (options: same contract). Fills opened_at / holding_days /
                         open_order_ids. A pnl row that carries its own open date wins (match "rh_row").
                         Not enough opening fills → NULL, never a guess (counted in status()).
  - Multi-leg / unverified option legs get is_multileg=1 (fail safe: excluded from lessons, priors,
    behavior). DRIP buys get is_drip=1 (placed_agent "drip"; date heuristic only when the marker is
    absent). The realized P&L is always Robinhood's own number, never recomputed here.
  - reconcile_report()   personal_positions vs Robinhood (reuses mason_robinhood.sync_personal_positions
                         dry-run). Report only; this module never writes personal_positions.
  - snapshot_accounts()  daily rh_account_snaps (16:30 ET job). ingest_alert_log() → rh_alert_log.
  - stories_queue() / answer_story()   Trade Stories (5/day, biggest dollars first, W/L alternating,
                         fixed for the ET day). An answer writes rh_trades + director_lessons + theses
                         in ONE transaction.
  - personal_priors()    family "personal" samples → mason_kb_priors.upsert_prior (imported lazily).

Paper and real are never mixed: this module reads only Robinhood (Nico's real accounts).
Flags: robinhood.enabled (master) AND rh_expansion.history.enabled; personal_priors also needs
rh_expansion.priors.enabled. Off → every public function is a cheap no-op, every route {"enabled": false}.
Public functions never raise. Account numbers are stored and shown masked (••NNNN) only.

CLI (Nico's go-live step only):  python3 mason_rh_history.py backfill --live
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, request

import mason_db
import mason_robinhood as RH

log = logging.getLogger("mason.rh_history")
ET = ZoneInfo("America/New_York")
CONFIG = Path(__file__).resolve().parent / "mason.config.json"
MODULE = "mason_rh_history"

_MAX_PAGES = 200                     # per (account, tool) walk; truncation surfaces in status()
_MATCH_SLACK_S = 2.0                 # pnl timestamps are fill timestamps truncated to the second
_QTY_EPS = 1e-6
_RUN_STALE_H = 24                    # an unfinished run older than this is abandoned, not resumed
_MAX_ROWS = 50000                    # bounded reads
_THESIS_MAX = 280
_NOTE_MAX = 1000
VERDICTS = ("right", "wrong", "luck", "timing")
_OUTCOME = {"right": "right", "wrong": "wrong", "luck": "neutral", "timing": "wrong"}
_THESIS_STATUS = {"right": "confirmed", "wrong": "broken", "luck": "confirmed", "timing": "broken"}
_SINGLE_LEG_STRATEGIES = {None, "", "long_call", "long_put", "short_call", "short_put"}
_ETF_INDUSTRY_RE = re.compile(r"investment trusts|mutual funds|exchange traded", re.I)
_NO_CRYPTO_RE = re.compile(r"no crypto account", re.I)
_OPEN_KEYS = ("opened_at", "open_date", "opening_timestamp", "open_timestamp")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_OCC_RE = re.compile(r"^([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})$")
_ACCT_NUM_RE = re.compile(r"\d{5,}(\d{4})")

# (tool, account-number field, rows key, next-cursor keys)
_TOOLS = (
    ("get_equity_orders", "number", "orders", ("next", "next_cursor")),
    ("get_option_orders", "number", "orders", ("next", "next_cursor")),
    ("get_crypto_orders", "rhs_number", "results", ("next", "next_cursor")),
    ("get_pnl_trade_history", "rhs_number", "trades", ("next_cursor", "next")),
)
_ARG_NAME = {"get_crypto_orders": "rhs_account_number"}

# every module whose status() feeds /api/rh/status (imported lazily; not-built-yet = absent)
_STATUS_MODULES = ("mason_price_cache", "mason_option_bars", "mason_kb_priors", "mason_pead",
                   "mason_regimes", "mason_congress", "mason_crowd", "mason_behavior", "mason_whatif",
                   "mason_pvr", "mason_exposure", "mason_scanners")

_DEFAULTS = {
    "personal_scope": {"exclude_accounts": ["Household"]},
    "history": {"enabled": False, "backfill_span": "all", "stories_per_day": 5, "story_min_abs_pl": 25},
    "priors": {"enabled": False, "min_n": 10},
}

_cfg_cache = {"mtime": None, "data": {}}
_lock = threading.Lock()
_state = {"last_run": None, "errors": [], "truncated": False, "verify": {}}


# ── config ────────────────────────────────────────────────────────────────────────────────────
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
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        for k, v in (_config().get("rh_expansion") or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            elif v is not None:
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    """Master switch robinhood.enabled AND rh_expansion.history.enabled (contracts §7)."""
    try:
        master = (_config().get("robinhood") or {}).get("enabled") is True
        return master and (_cfg().get("history") or {}).get("enabled") is True
    except Exception:
        return False


def _priors_enabled() -> bool:
    try:
        return enabled() and (_cfg().get("priors") or {}).get("enabled") is True
    except Exception:
        return False


def _excluded() -> set:
    """Normalized excluded labels via the shared parser (mason_robinhood, RULINGS #1/#3): a bare string is
    one label (never split into letters) and any falsy / malformed value fails CLOSED (Household excluded).
    The raw rh_expansion node is read so a missing key resolves to the parser's ["Household"] default."""
    try:
        return set(RH.resolve_exclude_accounts(_config().get("rh_expansion"),
                                                log=lambda m: _err("config", m), source="mason_rh_history"))
    except Exception:
        return {"household"}


def _in_scope(label) -> bool:
    return RH.norm_account_label(str(label or "")) not in _excluded()


def _int_cfg(block, key, default, lo=0, hi=10**9):
    try:
        return max(lo, min(hi, int((_cfg().get(block) or {}).get(key, default))))
    except (TypeError, ValueError):
        return default


def _float_cfg(block, key, default):
    try:
        return float((_cfg().get(block) or {}).get(key, default))
    except (TypeError, ValueError):
        return default


# ── small helpers ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _today_et_real() -> date:
    return datetime.now(ET).date()


_today_et = _today_et_real           # hook: tests pin the ET day


def _redact(s) -> str:
    return _ACCT_NUM_RE.sub(lambda m: "••" + m.group(1), str(s))


def _err(where: str, e) -> str:
    msg = _redact(f"{where}: {type(e).__name__}: {e}" if isinstance(e, BaseException) else f"{where}: {e}")[:300]
    with _lock:
        _state["errors"] = (_state["errors"] + [msg])[-20:]
    log.warning(f"[rh_history] {msg}")
    return msg


def _reset_state():
    with _lock:
        _state.update(last_run=None, errors=[], truncated=False, verify={})


def _f(v, default=None):
    try:
        if v in (None, "") or isinstance(v, bool):
            return default
        x = float(v)
        return x if x == x else default            # NaN → default
    except (TypeError, ValueError):
        return default


def _s(v) -> str | None:
    """The exact string Robinhood sent (numbers that arrive as JSON numbers are str()'d once)."""
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    return v if isinstance(v, str) else str(v)


def _dt(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(ts.strip()[:10])
        except ValueError:
            return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _iso(ts) -> str | None:
    d = _dt(ts)
    return d.isoformat() if d else None


def _et_date(ts) -> date | None:
    if isinstance(ts, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", ts):
        try:
            return date.fromisoformat(ts.strip())          # a calendar date stays that date
        except ValueError:
            return None
    d = _dt(ts)
    return d.astimezone(ET).date() if d else None


def _sha16(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _jl(s, default=None):
    try:
        v = json.loads(s) if isinstance(s, str) else None
        return v if v is not None else default
    except (TypeError, ValueError):
        return default


def _ds_get(conn, key):
    r = conn.execute("SELECT value FROM desk_state WHERE key=?", (key,)).fetchone()
    return _jl(r["value"]) if r else None


def _ds_set(conn, key, val):
    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                 (key, json.dumps(val, sort_keys=True), _now_iso()))


def _crypto_sym(s) -> str:
    s = str(s or "").upper().strip()
    for suf in ("-USD", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def _occ(sym):
    """(underlying, expiry ISO, 'call'|'put', strike) from an OCC-style symbol, else None."""
    m = _OCC_RE.match(str(sym or "").upper().strip())
    if not m:
        return None
    try:
        exp = datetime.strptime(m.group(2), "%y%m%d").date().isoformat()
    except ValueError:
        return None
    return m.group(1), exp, ("call" if m.group(3) == "C" else "put"), int(m.group(4)) / 1000.0


# ── RH reads (bulk lane) ──────────────────────────────────────────────────────────────────────
def _walk(tool: str, acct: dict, span: str):
    """(rows, meta) for one (account, tool) walk via paged_all on the bulk lane."""
    field = next(f for t, f, _, _ in _TOOLS if t == tool)
    rows_key = next(k for t, _, k, _ in _TOOLS if t == tool)
    next_key = next(n for t, _, _, n in _TOOLS if t == tool)
    args = {_ARG_NAME.get(tool, "account_number"): acct.get(field) or acct.get("number")}
    if tool == "get_pnl_trade_history":
        args["span"] = span
    rows, meta = RH.paged_all(tool, args, cursor_key="cursor", next_key=next_key, max_pages=_MAX_PAGES,
                              lane="bulk", rows_key=rows_key)
    meta = dict(meta)
    meta["errors"] = [_redact(e) for e in meta.get("errors") or []]
    if tool == "get_crypto_orders" and meta["pages"] == 0 and len(meta["errors"]) == 1 \
            and _NO_CRYPTO_RE.search(meta["errors"][0]):
        meta["errors"] = []                     # REVIEW_T1: only THIS error means "0 rows"
        meta["no_crypto_account"] = True
    return rows, meta


def _dividend_schedule_rh(symbol: str) -> dict:
    try:
        d = RH._call("get_equity_fundamentals", {"symbols": [symbol]}, lane="bulk") or {}
        rows = d.get("results") or [] if isinstance(d, dict) else []
        return next((r for r in rows if isinstance(r, dict) and str(r.get("symbol") or "").upper() == symbol), {})
    except Exception as e:
        _err(f"dividend schedule {symbol}", e)
        return {}


_dividend_schedule = _dividend_schedule_rh       # hook


def _instrument_classes_rh(symbols) -> dict:
    """{SYMBOL: 'etf'|'equity'} from get_equity_fundamentals industry (≤10 symbols per call)."""
    out = {}
    syms = sorted({s for s in symbols if s})
    for i in range(0, len(syms), 10):
        chunk = syms[i:i + 10]
        try:
            d = RH._call("get_equity_fundamentals", {"symbols": chunk}, lane="bulk") or {}
        except Exception as e:
            _err("instrument class", e)
            continue
        got = {}
        for r in (d.get("results") or []) if isinstance(d, dict) else []:
            if isinstance(r, dict) and r.get("symbol"):
                ind = f"{r.get('industry') or ''} {r.get('sector') or ''}"
                got[str(r["symbol"]).upper()] = "etf" if _ETF_INDUSTRY_RE.search(ind) else "equity"
        for s in chunk:
            out[s] = got.get(s, "equity")
    return out


_instrument_classes = _instrument_classes_rh     # hook


_instr_cache = {}


def _option_instrument(ins_id: str) -> dict:
    if ins_id in _instr_cache:
        return _instr_cache[ins_id]
    row = {}
    for extra in ({}, {"state": "expired"}):     # PROBE: an expired contract needs state="expired"
        try:
            d = RH._call("get_option_instruments", dict({"ids": ins_id}, **extra), lane="bulk") or {}
            rows = (d.get("instruments") or d.get("results") or []) if isinstance(d, dict) else []
            row = next((r for r in rows if isinstance(r, dict) and r.get("id") == ins_id), {})
        except Exception as e:
            _err("option instrument", e)
        if row:
            break
    _instr_cache[ins_id] = row
    return row


# ── order normalization ──────────────────────────────────────────────────────────────────────
def _fills(execs, *, fallback_qty=None, fallback_ts=None) -> list:
    out = []
    for e in execs if isinstance(execs, list) else []:
        if not isinstance(e, dict):
            continue
        ts, qty = _iso(e.get("timestamp")), _f(e.get("quantity"))
        if ts and qty and qty > 0:
            out.append({"ts": ts, "qty": qty, "px": _f(e.get("price"))})
    if not out and fallback_qty and fallback_qty > 0 and _iso(fallback_ts):
        out.append({"ts": _iso(fallback_ts), "qty": fallback_qty, "px": None})
    return sorted(out, key=lambda x: x["ts"])


def _drip(o: dict, qty, filled_at, sched_cache: dict):
    """(is_drip, method). Marker first (PROBE: placed_agent "drip"); the pay-date heuristic runs only
    when Robinhood sent no placed_agent at all."""
    agent = o.get("placed_agent")
    if agent == "drip":
        return 1, "placed_agent"
    if "placed_agent" in o and agent is not None:
        return 0, None
    if str(o.get("side")).lower() != "buy" or not qty or abs(qty - round(qty)) < _QTY_EPS or not filled_at:
        return 0, None
    sym = str(o.get("symbol") or "").upper()
    if sym not in sched_cache:
        sched_cache[sym] = _dividend_schedule(sym) or {}
    sch = sched_cache[sym]
    fill_d = _et_date(filled_at)
    for k in ("payable_date", "ex_dividend_date"):
        d = _et_date(sch.get(k)) if isinstance(sch.get(k), str) else None
        if fill_d and d and timedelta(0) <= fill_d - d <= timedelta(days=3):
            return 1, "fractional_on_pay_date"
    return 0, None


def _base_order_row(acct, oid, leg_idx, kind):
    return {"id": _sha16(f"{acct['masked']}|{oid}|{leg_idx}"), "account_label": acct["label"],
            "account_mask": acct["masked"], "rh_order_id": oid, "leg_idx": leg_idx, "kind": kind,
            "position_effect": None, "option_type": None, "strike": None, "expiry": None,
            "rh_instrument_id": None, "is_multileg": 0, "is_drip": 0, "fees": None}


def _equity_rows(acct, orders, sched_cache) -> tuple:
    out, bad = [], 0
    for o in orders:
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            bad += 1
            continue
        cum = _f(o.get("cumulative_quantity"))
        fills = _fills(o.get("executions"), fallback_qty=cum, fallback_ts=o.get("last_transaction_at"))
        qty = cum if cum is not None else sum(x["qty"] for x in fills)
        filled_at = fills[-1]["ts"] if fills else None
        drip, method = _drip(o, qty, filled_at, sched_cache)
        sym = str(o.get("symbol") or "").upper() or None
        r = _base_order_row(acct, oid, 0, "equity")
        r.update(symbol=sym, underlying=sym, side=str(o.get("side") or "").lower() or None, qty=qty,
                 avg_price=_f(o.get("average_price")), fees=_f(o.get("fees")), filled_at=filled_at,
                 rh_instrument_id=o.get("instrument_id"), is_drip=drip, state=o.get("state"),
                 raw_json=json.dumps({"order": o, "fills": fills, "drip_method": method}, sort_keys=True))
        out.append(r)
    return out, bad


def _crypto_rows(acct, orders) -> tuple:
    out, bad = [], 0
    for o in orders:
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            bad += 1
            continue
        cum = _f(o.get("cumulative_quantity"))
        fills = _fills(o.get("executions"), fallback_qty=cum, fallback_ts=o.get("last_transaction_at"))
        sym = _crypto_sym(o.get("symbol") or o.get("currency_code")) or None
        r = _base_order_row(acct, oid, 0, "crypto")
        r.update(symbol=sym, underlying=sym, side=str(o.get("side") or "").lower() or None,
                 qty=cum if cum is not None else sum(x["qty"] for x in fills),
                 avg_price=_f(o.get("average_price")), filled_at=fills[-1]["ts"] if fills else None,
                 rh_instrument_id=o.get("currency_pair_id"), state=o.get("state"),
                 raw_json=json.dumps({"order": o, "fills": fills}, sort_keys=True))
        out.append(r)
    return out, bad


def _option_rows(acct, orders) -> tuple:
    """One rh_orders row per leg. Unexpected shapes are stored with parse="unverified" and
    is_multileg=1 (REVIEW_T1 ruling 4: when unsure, exclude)."""
    out, bad = [], 0
    for o in orders:
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            bad += 1
            continue
        chain = str(o.get("chain_symbol") or "").upper() or None
        legs = o.get("legs")
        strategies = {o.get("opening_strategy"), o.get("closing_strategy")}
        odd_strategy = any(s not in _SINGLE_LEG_STRATEGIES for s in strategies)
        if not isinstance(legs, list) or not legs or not all(isinstance(l, dict) for l in legs):
            r = _base_order_row(acct, oid, 0, "option")
            r.update(symbol=chain, underlying=chain, side=None, qty=_f(o.get("processed_quantity")),
                     avg_price=_f(o.get("price")), filled_at=None, is_multileg=1, state=o.get("state"),
                     raw_json=json.dumps({"order": o, "leg_idx": 0, "fills": [], "parse": "unverified",
                                          "why": "legs missing or not a list of objects"}, sort_keys=True))
            out.append(r)
            continue
        multileg = 1 if (len(legs) > 1 or odd_strategy) else 0
        oq = _f(o.get("processed_quantity"))
        for idx, leg in enumerate(legs):
            ins = leg.get("option_id") if isinstance(leg.get("option_id"), str) else None
            if not ins and isinstance(leg.get("option"), str):
                m = _UUID_RE.search(leg["option"])
                ins = m.group(0).lower() if m else None
            strike, exp, typ = _f(leg.get("strike_price")), leg.get("expiration_date"), leg.get("option_type")
            if ins and (strike is None or not exp or not typ):
                meta = _option_instrument(ins)
                strike = strike if strike is not None else _f(meta.get("strike_price"))
                exp = exp or meta.get("expiration_date")
                typ = typ or meta.get("type")
            exp_d = _et_date(exp) if isinstance(exp, str) else None
            side = str(leg.get("side") or "").lower()
            eff = str(leg.get("position_effect") or "").lower()
            ratio = _f(leg.get("ratio_quantity"), 1.0) or 1.0
            fills = _fills(leg.get("executions"),
                           fallback_qty=(oq * ratio) if (oq and o.get("state") == "filled") else None,
                           fallback_ts=o.get("updated_at"))
            parsed = bool(ins and strike and strike > 0 and exp_d and typ in ("call", "put")
                          and side in ("buy", "sell") and eff in ("open", "close"))
            fq = sum(x["qty"] for x in fills)
            px = (sum(x["qty"] * x["px"] for x in fills if x["px"] is not None) / fq
                  if fills and fq and all(x["px"] is not None for x in fills) else _f(o.get("price")))
            r = _base_order_row(acct, oid, idx, "option")
            r.update(symbol=chain, underlying=chain, side=side or None, position_effect=eff or None,
                     qty=fq if fills else ((oq or 0.0) * ratio), avg_price=px,
                     filled_at=fills[-1]["ts"] if fills else None,
                     option_type=typ if typ in ("call", "put") else None, strike=strike,
                     expiry=exp_d.isoformat() if exp_d else None, rh_instrument_id=ins,
                     is_multileg=1 if (multileg or not parsed) else 0, state=o.get("state"),
                     raw_json=json.dumps({"order": o, "leg_idx": idx, "fills": fills,
                                          "parse": "ok" if parsed else "unverified"}, sort_keys=True))
            out.append(r)
    return out, bad


_ORDER_COLS = ("id", "account_label", "account_mask", "rh_order_id", "leg_idx", "kind", "symbol", "underlying",
               "side", "position_effect", "qty", "avg_price", "fees", "filled_at", "option_type", "strike",
               "expiry", "rh_instrument_id", "is_multileg", "is_drip", "state", "raw_json")


def _upsert_orders(conn, rows) -> int:
    now = _now_iso()
    sets = ", ".join(f"{c}=excluded.{c}" for c in _ORDER_COLS if c != "id")
    sql = (f"INSERT INTO rh_orders ({', '.join(_ORDER_COLS)}, created_date, created_by, record_deleted) "
           f"VALUES ({', '.join('?' * len(_ORDER_COLS))}, ?, ?, 0) ON CONFLICT(id) DO UPDATE SET {sets}")
    for r in rows:
        conn.execute(sql, [r.get(c) for c in _ORDER_COLS] + [now, MODULE])
    return len(rows)


def _pnl_rows(acct, trades) -> tuple:
    """natural_v1 keys (REVIEW_T1 ruling 3): sha1(mask|timestamp|symbol|side|quantity|price|realized_gain)
    over the EXACT strings RH sent; the 2nd+ identical row in one response gets "|#n"."""
    out, bad, seen = [], 0, {}
    for t in trades:
        fields = [_s(t.get(k)) for k in ("timestamp", "symbol", "side", "quantity", "price", "realized_gain")]
        if any(v is None for v in fields) or _dt(fields[0]) is None or _f(fields[5]) is None:
            bad += 1
            continue
        base = "|".join([acct["masked"]] + fields)
        seen[base] = seen.get(base, 0) + 1
        n = seen[base]
        sym = fields[1].upper().strip()
        out.append({"id": _sha16(base if n == 1 else f"{base}|#{n}"), "account_label": acct["label"],
                    "account_mask": acct["masked"], "kind": "equity", "symbol": sym, "underlying": sym,
                    "side": fields[2].lower(), "qty": _f(fields[3]), "price": _f(fields[4]),
                    "realized_pl": _f(fields[5]), "closed_at": _iso(fields[0]),
                    "raw_json": json.dumps({"row": t, "key": "natural_v1", "ordinal": n}, sort_keys=True)})
    return out, bad


def _upsert_trades(conn, rows) -> int:
    """Insert new trades; on re-ingest refresh only RH's own fields. Story answers (verdict, thesis,
    lesson links, story_status) and match results are never clobbered here."""
    now = _now_iso()
    data = ("account_label", "account_mask", "symbol", "side", "qty", "price", "realized_pl", "closed_at")
    sql = ("INSERT INTO rh_trades (id, account_label, account_mask, kind, symbol, underlying, side, qty, price,"
           " realized_pl, closed_at, raw_json, created_date, created_by, record_deleted, story_status)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'none') ON CONFLICT(id) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in data))
    for r in rows:
        conn.execute(sql, (r["id"], r["account_label"], r["account_mask"], r["kind"], r["symbol"], r["underlying"],
                           r["side"], r["qty"], r["price"], r["realized_pl"], r["closed_at"], r["raw_json"],
                           now, MODULE))
    return len(rows)


# ── backfill ──────────────────────────────────────────────────────────────────────────────────
def _ingest(conn, tool, acct, rows, sched_cache) -> tuple:
    rows = [r for r in rows if isinstance(r, dict)]
    if tool == "get_equity_orders":
        out, bad = _equity_rows(acct, rows, sched_cache)
        return _upsert_orders(conn, out), bad
    if tool == "get_option_orders":
        out, bad = _option_rows(acct, rows)
        return _upsert_orders(conn, out), bad
    if tool == "get_crypto_orders":
        out, bad = _crypto_rows(acct, rows)
        return _upsert_orders(conn, out), bad
    out, bad = _pnl_rows(acct, rows)
    return _upsert_trades(conn, out), bad


def backfill(*, accounts=None, span=None) -> dict:
    """Pull every tracked account's orders + realized trades into rh_orders / rh_trades, then FIFO-match
    and verify Σ realized_gain against get_realized_pnl.total_returns per account."""
    if not enabled():
        return {"enabled": False}
    res = {"ok": False, "run_id": None, "accounts": {}, "orders": 0, "trades": 0, "errors": [],
           "truncated": False, "verify": {}}
    try:
        span = span or (_cfg().get("history") or {}).get("backfill_span") or "all"
        accts = RH.accounts()
        if accounts:
            want = {str(a).strip().lower() for a in accounts}
            accts = [a for a in accts if str(a.get("label", "")).lower() in want]
        if not accts:
            res["errors"].append("no Robinhood accounts returned (disconnected or filtered out)")
            return _finish(res)
        sched_cache = {}
        conn = mason_db.get_connection()
        try:
            run = _ds_get(conn, "rhx_backfill_run") or {}
            started = _dt(run.get("started"))
            if run.get("complete", True) or not started or _now() - started > timedelta(hours=_RUN_STALE_H):
                run = {"run_id": uuid.uuid4().hex[:12], "started": _now_iso(), "complete": False}
            res["run_id"] = run["run_id"]
            _ds_set(conn, "rhx_backfill_run", run)
            conn.commit()
            for acct in accts:
                rep = res["accounts"].setdefault(acct["label"], {})
                last4 = str(acct.get("masked") or "")[-4:]
                for tool, *_ in _TOOLS:
                    key = f"rhx_backfill_{last4}_{tool}"
                    ck = _ds_get(conn, key) or {}
                    if ck.get("run_id") == run["run_id"] and ck.get("done"):
                        rep[tool] = {"skipped": True, "rows": ck.get("rows", 0)}
                        continue
                    rows, meta = _walk(tool, acct, span)
                    n, bad = 0, 0
                    try:
                        n, bad = _ingest(conn, tool, acct, rows, sched_cache)
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        meta["errors"].append(_err(f"ingest {tool} {acct['masked']}", e))
                    if bad:
                        meta["errors"].append(f"{tool} {acct['masked']}: {bad} malformed row(s) skipped")
                    done = not meta["errors"] and not meta["truncated"]
                    res["errors"].extend(meta["errors"])
                    res["truncated"] = res["truncated"] or bool(meta["truncated"])
                    res["orders" if tool != "get_pnl_trade_history" else "trades"] += n
                    rep[tool] = {"rows": n, "pages": meta.get("pages", 0), "done": done}
                    _ds_set(conn, key, {"run_id": run["run_id"], "done": done, "pages": meta.get("pages", 0),
                                        "rows": n, "truncated": bool(meta["truncated"]),
                                        "errors": meta["errors"][:5], "at": _now_iso()})
                    conn.commit()
            _classify_instruments(conn)
            conn.commit()
        finally:
            conn.close()
        m = match_trades()
        if m.get("error"):
            res["errors"].append(m["error"])
        res["unmatched"] = m.get("unmatched")
        pl = prelink_lessons()
        if pl.get("error"):
            res["errors"].append(pl["error"])
        res["prelink"] = pl
        res["verify"] = _verify(accts)
        for lab, v in res["verify"].items():
            if v.get("ok") is False:
                res["errors"].append(_redact(f"verify {lab}: {v.get('why')}"))
        res["ok"] = not res["errors"] and not res["truncated"]
        if res["ok"]:
            conn = mason_db.get_connection()
            try:
                run["complete"] = True
                run["finished"] = _now_iso()
                _ds_set(conn, "rhx_backfill_run", run)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        res["errors"].append(_err("backfill", e))
    return _finish(res)


def _finish(res: dict) -> dict:
    with _lock:
        _state["last_run"] = _now_iso()
        _state["truncated"] = bool(res.get("truncated"))
        _state["verify"] = res.get("verify") or {}
        _state["errors"] = [_redact(e)[:300] for e in res.get("errors") or []][-20:]
    if res.get("errors"):
        log.warning(f"[rh_history] backfill incomplete: {len(res['errors'])} error(s)")
    return res


def _classify_instruments(conn):
    """ETF vs single stock for equity symbols, cached in desk_state (rhx_instrument_class)."""
    cache = _ds_get(conn, "rhx_instrument_class") or {}
    syms = {r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM rh_trades WHERE coalesce(record_deleted,0)=0 AND symbol IS NOT NULL")}
    syms |= {r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM rh_orders WHERE kind='equity' AND coalesce(record_deleted,0)=0"
        " AND symbol IS NOT NULL")}
    missing = sorted(s for s in syms if s not in cache)
    if missing:
        got = _instrument_classes(missing) or {}
        cache.update({k: v for k, v in got.items() if v in ("etf", "equity")})
        _ds_set(conn, "rhx_instrument_class", cache)


def _verify(accts) -> dict:
    """Σ realized_gain (exact strings, Decimal) per account vs get_realized_pnl.total_returns."""
    out = {}
    conn = mason_db.get_connection()
    try:
        for a in accts:
            sums = Decimal(0)
            for r in conn.execute("SELECT raw_json FROM rh_trades WHERE account_label=? AND coalesce(record_deleted,0)=0",
                                  (a["label"],)):
                try:
                    sums += Decimal(str((_jl(r["raw_json"], {}).get("row") or {}).get("realized_gain")))
                except (InvalidOperation, AttributeError):
                    pass
            try:
                d = RH._call("get_realized_pnl", {"account_number": a.get("rhs_number") or a.get("number"),
                                                  "span": "all"}, lane="bulk") or {}
                tot = Decimal(str(d.get("total_returns")))
            except (InvalidOperation, AttributeError) as e:
                out[a["label"]] = {"ok": None, "sum": str(sums), "why": f"total_returns unreadable ({type(e).__name__})"}
                continue
            except Exception as e:
                out[a["label"]] = {"ok": None, "sum": str(sums), "why": _redact(f"{type(e).__name__}: {e}")[:200]}
                continue
            ok = sums == tot
            out[a["label"]] = {"ok": ok, "sum": str(sums), "total_returns": str(tot),
                               "why": None if ok else f"sum realized_gain {sums} != total_returns {tot}"}
    finally:
        conn.close()
    return out


# ── FIFO matching ─────────────────────────────────────────────────────────────────────────────
def _lot_key(o: dict):
    if o["kind"] == "option":
        if o.get("rh_instrument_id"):
            return ("option", o["rh_instrument_id"])
        return ("option", o.get("underlying"), o.get("option_type"), o.get("strike"), o.get("expiry"))
    return (o["kind"], o.get("symbol"))


def match_trades() -> dict:
    """Recompute kind / instrument / opened_at / holding_days / open_order_ids / close_order_id /
    is_multileg for every rh_trades row from rh_orders. Pure DB work, deterministic, idempotent."""
    if not enabled():
        return {"enabled": False}
    try:
        return mason_db.with_connection(_match_all)
    except Exception as e:
        return {"ok": False, "error": _err("match_trades", e), "unmatched": None}


def _match_all(conn) -> dict:
    classes = _ds_get(conn, "rhx_instrument_class") or {}
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM rh_orders WHERE coalesce(record_deleted,0)=0 ORDER BY filled_at, id LIMIT ?", (_MAX_ROWS,))]
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM rh_trades WHERE coalesce(record_deleted,0)=0 ORDER BY closed_at, id LIMIT ?", (_MAX_ROWS,))]
    by_acct = {}
    for o in orders:
        o["_fills"] = (_jl(o.get("raw_json"), {}) or {}).get("fills") or []
        by_acct.setdefault(o["account_label"], []).append(o)
    unmatched = 0
    lots_by_acct = {}
    for acct, os_ in by_acct.items():
        lots = {}
        for o in os_:
            opening = (o["kind"] in ("equity", "crypto") and o.get("side") == "buy") or \
                      (o["kind"] == "option" and o.get("position_effect") == "open")
            if not opening:
                continue
            for f in o["_fills"]:
                t = _dt(f.get("ts"))
                if t and _f(f.get("qty"), 0) > 0:
                    lots.setdefault(_lot_key(o), []).append(
                        {"t": t, "ts": f["ts"], "qty": float(f["qty"]), "px": _f(f.get("px")),
                         "order": o["rh_order_id"], "o": o})
        for v in lots.values():
            v.sort(key=lambda x: (x["t"], x["order"]))
        lots_by_acct[acct] = lots
    for t in trades:
        raw = _jl(t.get("raw_json"), {}) or {}
        row = raw.get("row") if isinstance(raw.get("row"), dict) else {}
        upd = _match_one(t, row, by_acct.get(t["account_label"], []), lots_by_acct.get(t["account_label"], {}))
        info = {"row": row, "key": raw.get("key", "natural_v1"), "ordinal": raw.get("ordinal", 1)}
        if raw.get("prelinked_lesson"):
            info["prelinked_lesson"] = raw["prelinked_lesson"]
        info.update(upd.pop("_raw"))
        if upd["kind"] == "equity":
            info["instrument"] = classes.get(t["symbol"], "equity")
            if t["symbol"] not in classes:
                info["instrument_via"] = "default"
        else:
            info["instrument"] = upd["kind"]
        if upd["opened_at"] is None:
            unmatched += 1
        conn.execute("UPDATE rh_trades SET kind=?, underlying=?, opened_at=?, holding_days=?, open_order_ids=?,"
                     " close_order_id=?, is_multileg=?, option_type=?, strike=?, expiry=?, raw_json=? WHERE id=?",
                     (upd["kind"], upd["underlying"], upd["opened_at"], upd["holding_days"], upd["open_order_ids"],
                      upd["close_order_id"], upd["is_multileg"], upd["option_type"], upd["strike"], upd["expiry"],
                      json.dumps(info, sort_keys=True), t["id"]))
    conn.commit()
    return {"ok": True, "trades": len(trades), "unmatched": unmatched}


def _match_one(t, row, orders, lots) -> dict:
    sym = str(t.get("symbol") or "").upper()
    closed = _dt(t.get("closed_at"))
    qty = _f(t.get("qty"), 0.0) or 0.0
    occ = _occ(sym)
    und = occ[0] if occ else sym
    out = {"kind": "equity", "underlying": sym, "opened_at": None, "holding_days": None, "open_order_ids": None,
           "close_order_id": None, "is_multileg": 0, "option_type": None, "strike": None, "expiry": None,
           "_raw": {"match": None}}
    raw = out["_raw"]
    # 1. the closing order: same account + symbol, a fill within the slack, same quantity
    close = None
    if closed is not None:
        best = None
        for o in orders:
            closing = (o["kind"] in ("equity", "crypto") and o.get("side") == "sell") or \
                      (o["kind"] == "option" and o.get("position_effect") == "close")
            if not closing:
                continue
            osym = o.get("underlying") if o["kind"] == "option" else o.get("symbol")
            if osym != und:
                continue
            if occ and o["kind"] == "option" and (o.get("expiry") != occ[1] or o.get("option_type") != occ[2]
                                                  or abs((o.get("strike") or 0) - occ[3]) > 1e-6):
                continue
            qtys = [o.get("qty") or 0.0] + [float(f["qty"]) for f in o["_fills"]]
            if not any(abs(q - qty) <= _QTY_EPS * max(1.0, qty) for q in qtys):
                continue
            for f in o["_fills"]:
                ft = _dt(f.get("ts"))
                if ft is None:
                    continue
                gap = abs((ft - closed).total_seconds())
                if gap <= _MATCH_SLACK_S and (best is None or gap < best[0]):
                    best = (gap, o)
        close = best[1] if best else None
    key = None
    if close is not None:
        out["kind"] = close["kind"]
        out["close_order_id"] = close["rh_order_id"]
        out["is_multileg"] = int(close.get("is_multileg") or 0)
        key = _lot_key(close)
        if close["kind"] == "option":
            out.update(underlying=close.get("underlying") or und, option_type=close.get("option_type"),
                       strike=close.get("strike"), expiry=close.get("expiry"))
            if _jl(close.get("raw_json"), {}).get("parse") == "unverified":
                raw["parse"] = "unverified"
        raw["kind_via"] = "close_order"
    else:
        # 2. no closing order: an option that expired / was assigned, matched by expiry date
        cdate = closed.astimezone(ET).date().isoformat() if closed else None
        opt_keys = {k: v for k, v in lots.items() if k[0] == "option" and v and v[0]["o"].get("underlying") == und}
        if occ:
            opt_keys = {k: v for k, v in opt_keys.items() if v[0]["o"].get("expiry") == occ[1]}
        cands = {k: v for k, v in opt_keys.items() if cdate and v[0]["o"].get("expiry") == cdate
                 and sum(l["qty"] for l in v) > _QTY_EPS}
        if opt_keys and not lots.get(("equity", sym)) and not lots.get(("crypto", sym)):
            out["kind"] = "option"
            raw["close_via"] = "expiry_or_assignment"
            raw["kind_via"] = "option_lots"
            if len(cands) == 1:
                key = next(iter(cands))
                o0 = cands[key][0]["o"]
                out.update(option_type=o0.get("option_type"), strike=o0.get("strike"), expiry=o0.get("expiry"))
            else:
                out["is_multileg"] = 1                   # ambiguous → fail safe (REVIEW_T1 ruling 4)
                raw["parse"] = "unverified"
                raw["why"] = f"{len(cands)} candidate contracts expiring {cdate}"
        elif lots.get(("crypto", sym)) and not lots.get(("equity", sym)):
            out["kind"] = "crypto"
            key = ("crypto", sym)
            raw["kind_via"] = "crypto_lots"
        else:
            key = ("equity", sym)
            raw["kind_via"] = "default"
    # 3. FIFO over the opening fills (consumed even when the pnl row carries its own open date)
    fifo = _consume(lots.get(key) if key else None, qty, closed)
    if fifo and any(int(l["o"].get("is_multileg") or 0) for l in fifo["lots_raw"]):
        out["is_multileg"] = 1
    opened_row = next((_iso(row.get(k)) for k in _OPEN_KEYS if _iso(row.get(k))), None)
    if opened_row:
        out["opened_at"] = opened_row
        raw["match"] = "rh_row"
    elif fifo and fifo["covered"]:
        detail = {"lots": [{"order_id": l["order"], "ts": l["ts"], "qty": l["used"], "px": l["px"]}
                           for l in fifo["lots_raw"]]}
        ok, chk = _reconciles(out["kind"], qty, _f(t.get("price")), _f(t.get("realized_pl")), fifo["lots_raw"])
        detail["cost_check"] = chk
        raw["match_detail"] = detail
        if ok:
            out["opened_at"] = fifo["lots_raw"][0]["ts"]
            out["open_order_ids"] = json.dumps(list(dict.fromkeys(l["order"] for l in fifo["lots_raw"])))
            raw["match"] = "fifo"
        else:
            raw["match"] = "fifo_unreconciled"        # lots consumed, but they are not the ones RH sold
    elif fifo:
        raw["match_detail"] = {"short_qty": round(fifo["short"], 9)}
    if out["opened_at"] and closed:
        o_dt = _dt(out["opened_at"])
        out["holding_days"] = round((closed - o_dt).total_seconds() / 86400.0, 6) if o_dt else None
    return out


def _reconciles(kind, qty, price, realized_pl, used) -> tuple:
    """Do the FIFO lots reproduce Robinhood's own P&L? RH's implied cost = qty × pnl price − realized_pl
    (× 100 for option contracts) must equal Σ lot qty × fill price within $0.02/share + $0.005
    (contracts §8). A lot with no fill price cannot be checked, so it does not count as a match."""
    mult = 100.0 if kind == "option" else 1.0
    if price is None or realized_pl is None or any(l.get("px") is None for l in used):
        return False, {"ok": False, "why": "no fill price or pnl price to check against"}
    implied = qty * price * mult - realized_pl
    lots = sum(l["used"] * l["px"] for l in used) * mult
    tol = 0.02 * qty * mult + 0.005
    ok = abs(implied - lots) <= tol
    return ok, {"ok": ok, "implied_cost": round(implied, 6), "lots_cost": round(lots, 6), "tol": round(tol, 6)}


def _consume(lots, qty, closed):
    """Take qty from the oldest lots filled at or before the close. Returns the lots used."""
    if lots is None or closed is None or qty <= 0:
        return None
    need, used = qty, []
    for l in lots:
        if need <= _QTY_EPS * max(1.0, qty):
            break
        if l["qty"] <= _QTY_EPS or (l["t"] - closed).total_seconds() > 1.0:
            continue
        take = min(l["qty"], need)
        l["qty"] -= take
        need -= take
        used.append({"ts": l["ts"], "order": l["order"], "used": round(take, 9), "px": l.get("px"), "o": l["o"]})
    covered = need <= _QTY_EPS * max(1.0, qty)
    return {"covered": covered, "short": max(0.0, need), "lots_raw": used}


# ── reconcile / snapshots / alert log ────────────────────────────────────────────────────────
def reconcile_report() -> dict:
    """Hand-entered personal_positions (Play/Roth/Savings) vs open Robinhood positions. Report only:
    reuses mason_robinhood.sync_personal_positions(dry_run=True); nothing is written."""
    if not enabled():
        return {"enabled": False}
    try:
        r = RH.sync_personal_positions(dry_run=True) or {}
        out = {k: list(r.get(k) or []) for k in ("match", "changed", "hand_only", "rh_only", "cash")}
        out.update(ok="error" not in r, written=0, as_of=_now_iso())
        if "error" in r:
            out["error"] = _redact(r["error"])[:300]
        return out
    except Exception as e:
        return {"ok": False, "written": 0, "error": _err("reconcile_report", e)}


def snapshot_accounts() -> dict:
    """One rh_account_snaps row per tracked account for today's ET date (re-run = overwrite)."""
    if not enabled():
        return {"enabled": False}
    try:
        snap = RH.snapshot() or {}
        day = _today_et().isoformat()
        now = _now_iso()
        rows = 0
        conn = mason_db.get_connection()
        try:
            for label, s in snap.items():
                eq, cash = _f(s.get("total_value")), _f(s.get("cash"))
                if eq is None and cash is None:
                    _err("snapshot_accounts", f"{s.get('masked')}: no portfolio values")
                    continue
                pv = (eq - cash) if (eq is not None and cash is not None) else None
                raw = {"masked": s.get("masked"), "total_value": eq, "cash": cash,
                       "buying_power": _f(s.get("buying_power")), "n_positions": len(s.get("positions") or [])}
                conn.execute(
                    "INSERT INTO rh_account_snaps (account_label, snap_date, equity, cash, buying_power, positions_value,"
                    " raw_json, created_date, created_by) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_label, snap_date)"
                    " DO UPDATE SET equity=excluded.equity, cash=excluded.cash, buying_power=excluded.buying_power,"
                    " positions_value=excluded.positions_value, raw_json=excluded.raw_json",
                    (label, day, eq, cash, _f(s.get("buying_power")), pv, json.dumps(raw, sort_keys=True), now, MODULE))
                rows += 1
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "rows": rows, "snap_date": day}
    except Exception as e:
        return {"ok": False, "rows": 0, "error": _err("snapshot_accounts", e)}


def ingest_alert_log() -> dict:
    """get_alert_log (every page, bulk lane) → rh_alert_log, idempotent on alert_log_id."""
    if not enabled():
        return {"enabled": False}
    try:
        events, meta = RH.paged_all("get_alert_log", {"limit": 100}, cursor_key="cursor", next_key="next_cursor",
                                    max_pages=_MAX_PAGES, lane="bulk", rows_key="events")
        errors = [_redact(e) for e in meta.get("errors") or []]
        now = _now_iso()
        n = 0
        conn = mason_db.get_connection()
        try:
            for ev in events:
                trig = ev.get("trigger") if isinstance(ev.get("trigger"), dict) else {}
                lid = ev.get("alert_log_id")
                nat = lid if isinstance(lid, str) and lid else \
                    f"{ev.get('alert_id')}|{ev.get('symbol')}|{ev.get('triggered_at')}"
                cond = str(ev.get("condition_type") or "") or None
                if cond and trig.get("direction"):
                    cond = f"{cond}:{trig['direction']}"
                conn.execute(
                    "INSERT INTO rh_alert_log (id, rh_alert_id, symbol, condition, level, fired_at, price_at_fire, raw_json,"
                    " created_date, created_by, record_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET"
                    " rh_alert_id=excluded.rh_alert_id, symbol=excluded.symbol, condition=excluded.condition,"
                    " level=excluded.level, fired_at=excluded.fired_at, price_at_fire=excluded.price_at_fire,"
                    " raw_json=excluded.raw_json",
                    (_sha16(f"rh_alert_log|{nat}"), ev.get("alert_id"), str(ev.get("symbol") or "").upper() or None,
                     cond, _f(trig.get("target_price")), _iso(ev.get("triggered_at")), _f(trig.get("triggered_price")),
                     json.dumps(ev, sort_keys=True), now, MODULE))
                n += 1
            conn.commit()
        finally:
            conn.close()
        for e in errors:
            _err("ingest_alert_log", e)
        return {"ok": not errors and not meta.get("truncated"), "rows": n, "errors": errors,
                "truncated": bool(meta.get("truncated"))}
    except Exception as e:
        return {"ok": False, "rows": 0, "error": _err("ingest_alert_log", e)}


# ── reads ─────────────────────────────────────────────────────────────────────────────────────
_TRADE_FIELDS = ("id", "account", "kind", "symbol", "side", "qty", "price", "realized_pl", "closed_at",
                 "holding_days", "verdict", "thesis_text", "story_status", "is_multileg")


def _item(r: dict) -> dict:
    return {"id": r["id"], "account": r["account_label"], "kind": r["kind"], "symbol": r["symbol"],
            "side": r["side"], "qty": r["qty"], "price": r["price"], "realized_pl": r["realized_pl"],
            "closed_at": r["closed_at"], "holding_days": r["holding_days"], "verdict": r["verdict"],
            "thesis_text": r["thesis_text"], "story_status": r["story_status"] or "none",
            "is_multileg": bool(r["is_multileg"])}


def trades(account=None, symbol=None, verdict=None, limit=100) -> list:
    """Realized trades, newest first. Personal scope by default; an explicit account= shows that
    account, except an excluded one (Household) → [] (T-D6, RULINGS #10: no on-request Household path; Household's rows
    stay stored and tagged, contracts §2.8)."""
    if not enabled():
        return []
    try:
        if account and RH.norm_account_label(str(account)) in _excluded():
            return []
        try:
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            limit = 100
        sql, args = "SELECT * FROM rh_trades WHERE coalesce(record_deleted,0)=0", []
        if account:
            sql += " AND lower(account_label)=?"
            args.append(str(account).strip().lower())
        if symbol:
            sql += " AND (upper(symbol)=? OR upper(underlying)=?)"
            args += [str(symbol).strip().upper()] * 2
        if verdict:
            sql += " AND verdict=?"
            args.append(str(verdict).strip().lower())
        sql += " ORDER BY closed_at DESC, id LIMIT ?"
        rows = mason_db.with_connection(lambda c: [dict(r) for r in c.execute(sql, args + [_MAX_ROWS])])
        if not account:
            rows = [r for r in rows if _in_scope(r["account_label"])]
        return [_item(r) for r in rows[:limit]]
    except Exception as e:
        _err("trades", e)
        return []


# ── existing personal lessons (Director ruling N2: never a duplicate lesson) ─────────────────
_PRELINK_DAYS = 3
# Selling more shares than the lesson's hand-entered position held suggests a different trade. Hand
# rows are often estimates (e.g. "Unspecified (backfill)"), so by default this is recorded on the link
# (prelinked_lesson.qty_match) rather than used to block it. True turns it into a hard guard.
_PRELINK_QTY_GUARD = False
_REF_TAG_RE = re.compile(r"^(personal|trade):([0-9a-f]{6,32})$", re.I)


def _resolve_lesson_ref(conn, tags, ticker):
    """What a lesson's personal:<id> / trade:<id> tag points at: {date, account, shares, via} or None.
    personal_positions is the canonical realized record (Nico's real trades). A trade:<id> tag counts
    only when that trades row is a REAL personal trade (linked to personal_positions, or
    exit_reason='personal_account_trade'); a Mason paper trade never links (contracts §2.3)."""
    refs = [(m.group(1).lower(), m.group(2).lower()) for m in (_REF_TAG_RE.match(str(t)) for t in tags) if m]

    def _position(pid):
        rows = conn.execute("SELECT ticker, account_label, close_date, shares FROM personal_positions WHERE id LIKE ?"
                            " AND coalesce(is_deleted,0)=0 LIMIT 2", (pid + "%",)).fetchall()
        if len(rows) == 1 and str(rows[0]["ticker"] or "").upper() == ticker and rows[0]["close_date"]:
            d = _et_date(str(rows[0]["close_date"])[:10])
            if d:
                return {"date": d, "account": rows[0]["account_label"], "shares": _f(rows[0]["shares"]),
                        "via": f"personal:{pid}"}
        return None

    for kind, ref in refs:                              # personal_positions first: it is canonical
        if kind == "personal":
            hit = _position(ref)
            if hit:
                return hit
    for kind, ref in refs:
        if kind != "trade":
            continue
        rows = conn.execute("SELECT ticker, exit_date, exit_reason, shares, extra_json FROM trades WHERE id LIKE ?"
                            " AND coalesce(is_deleted,0)=0 LIMIT 2", (ref + "%",)).fetchall()
        if len(rows) != 1 or str(rows[0]["ticker"] or "").upper() != ticker:
            continue
        link = ((_jl(rows[0]["extra_json"], {}) or {}).get("_link") or {}).get("personal_positions")
        if isinstance(link, str) and link:
            hit = _position(link.lower())
            if hit:
                return hit
        if rows[0]["exit_reason"] == "personal_account_trade" and rows[0]["exit_date"]:
            d = _et_date(str(rows[0]["exit_date"])[:10])
            if d:
                return {"date": d, "account": None, "shares": _f(rows[0]["shares"]), "via": f"trade:{ref}"}
    return None


def _label_map() -> dict:
    """robinhood.personal_positions_sync.label_map (hand label -> RH account name), read from config."""
    try:
        lm = RH._cfg()["personal_positions_sync"].get("label_map") or {}
        return {str(k).strip().lower(): str(v).strip() for k, v in lm.items() if str(k).strip() and str(v).strip()}
    except Exception:
        return {}


def _rh_account_for(label, rh_names: set):
    """(RH account name | None, how). how: label_map | rh_name | unresolved | non_rh."""
    lab = str(label or "").strip()
    if not lab or lab.lower().startswith("unspecified"):
        return None, "unresolved"
    lm = _label_map()
    if lab.lower() in lm:
        return lm[lab.lower()], "label_map"
    exact = {n.lower(): n for n in rh_names | set(lm.values())}
    if lab.lower() in exact:
        return exact[lab.lower()], "rh_name"
    return None, "non_rh"                           # e.g. Schwab: can never be an RH trade


def _lesson_kind(tags) -> str:
    inst = next((str(t).split(":", 1)[1].lower() for t in tags if str(t).lower().startswith("instrument:")), "")
    return "option" if "option" in inst else "equity"


def _qty_fits(t, ref) -> bool:
    return not ref.get("shares") or (t.get("qty") or 0) <= ref["shares"] * (1 + 1e-6) + _QTY_EPS


def _prelink_lessons(conn) -> dict:
    """Link rh_trades to src:personal director_lessons that already cover them (Director ruling N2).
    A candidate trade must match ALL of: same symbol; same instrument kind (option lesson → option
    trade, otherwise equity); a closing SELL; ET close date within ±3 days of the date the lesson's tag
    resolves to; (only with _PRELINK_QTY_GUARD) quantity ≤ the tagged position's shares; and the SAME RH account when the lesson's
    account resolves (label_map or exact RH name; a non-RH account never links). If the account does
    not resolve, candidates come from in-scope accounts only and the match must be unique across them.
    Links are 1:1 both ways; anything ambiguous is counted, never linked. A linked trade keeps
    verdict NULL and story_status 'none'. T-D6 / RULINGS #36: a trade is a candidate only when it is in
    personal scope (both branches), and a lesson whose resolved account is excluded (Household) links nothing and is
    counted in skipped_excluded; after exclude_accounts → [] the next run links it (no re-ingest)."""
    lessons = [dict(r) for r in conn.execute(
        "SELECT id, ticker, tags, source_slate_id FROM director_lessons WHERE coalesce(record_deleted,0)=0"
        " AND tags LIKE '%src:personal%' LIMIT ?", (_MAX_ROWS,))]
    lessons = [l for l in lessons if not str(l.get("source_slate_id") or "").startswith("rh_trades:")]
    linked_ids = {r["lesson_id"] for r in conn.execute(
        "SELECT lesson_id FROM rh_trades WHERE lesson_id IS NOT NULL")}
    trades_ = [dict(r) for r in conn.execute(
        "SELECT id, account_label, kind, side, qty, symbol, underlying, closed_at, raw_json FROM rh_trades"
        " WHERE coalesce(record_deleted,0)=0 AND lesson_id IS NULL LIMIT ?", (_MAX_ROWS,))]
    rh_names = {str(r[0]) for r in conn.execute("SELECT DISTINCT account_label FROM rh_trades") if r[0]}
    pairs, how_by_lesson, skipped_excluded = {}, {}, 0
    for l in lessons:
        if l["id"] in linked_ids:
            continue
        tick = str(l.get("ticker") or "").upper()
        tags = _jl(l.get("tags"), []) or []
        tags = tags if isinstance(tags, list) else []
        ref = _resolve_lesson_ref(conn, tags, tick) if tick else None
        if not ref:
            continue
        acct, how = _rh_account_for(ref["account"], rh_names)
        if how == "non_rh":
            continue
        if acct is not None and not _in_scope(acct):
            skipped_excluded += 1
            continue
        kind = _lesson_kind(tags)
        how_by_lesson[l["id"]] = (how, acct, ref["via"], ref)
        for t in trades_:
            if tick not in {str(t.get("symbol") or "").upper(), str(t.get("underlying") or "").upper()}:
                continue
            if (t.get("kind") or "equity") != kind or str(t.get("side") or "").lower() != "sell":
                continue
            if not _in_scope(t.get("account_label")):
                continue
            if acct is not None and RH.norm_account_label(str(t.get("account_label") or "")) \
                    != RH.norm_account_label(str(acct)):
                continue
            if _PRELINK_QTY_GUARD and not _qty_fits(t, ref):
                continue
            cd = _et_date(t.get("closed_at"))
            if cd and abs((cd - ref["date"]).days) <= _PRELINK_DAYS:
                pairs.setdefault(l["id"], []).append(t["id"])
    by_trade = {}
    for lid, tids in pairs.items():
        for tid in tids:
            by_trade.setdefault(tid, []).append(lid)
    linked, ambiguous = 0, 0
    for lid, tids in sorted(pairs.items()):
        if len(tids) != 1 or len(by_trade[tids[0]]) != 1:
            ambiguous += 1
            continue
        tid = tids[0]
        how, acct, via, ref = how_by_lesson[lid]
        trow = next(t for t in trades_ if t["id"] == tid)
        raw = _jl(trow["raw_json"], {}) or {}
        raw["prelinked_lesson"] = {"lesson_id": lid, "at": _now_iso(), "rule": "tag_close_date_pm3d",
                                   "via": via, "account_match": how, "account": acct,
                                   "qty_match": _qty_fits(trow, ref)}
        conn.execute("UPDATE rh_trades SET lesson_id=?, raw_json=?, story_status=CASE WHEN story_status="
                     "'requested' THEN 'none' ELSE story_status END WHERE id=? AND lesson_id IS NULL",
                     (lid, json.dumps(raw, sort_keys=True), tid))
        linked += 1
    total = conn.execute("SELECT count(*) FROM rh_trades WHERE coalesce(record_deleted,0)=0 AND raw_json LIKE"
                         " '%\"prelinked_lesson\"%'").fetchone()[0]
    out = {"linked_now": linked, "linked_total": total, "ambiguous": ambiguous,
           "skipped_excluded": skipped_excluded, "at": _now_iso()}
    _ds_set(conn, "rhx_lesson_prelink", out)
    return out


def prelink_lessons() -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        def _run(c):
            r = _prelink_lessons(c)
            c.commit()
            return r
        return mason_db.with_connection(_run)
    except Exception as e:
        return {"ok": False, "error": _err("prelink_lessons", e)}


# ── Trade Stories ─────────────────────────────────────────────────────────────────────────────
def _story_eligible(r: dict, *, scope=True) -> bool:
    raw = _jl(r.get("raw_json"), {}) or {}
    return (not r.get("is_multileg") and raw.get("parse") != "unverified" and r.get("verdict") is None
            and not r.get("lesson_id")
            and (r.get("story_status") or "none") in ("none", "requested") and r.get("realized_pl") is not None
            and abs(r["realized_pl"]) >= _float_cfg("history", "story_min_abs_pl", 25.0)
            and (not scope or _in_scope(r.get("account_label"))))


def _top(rows, n):
    w = sorted([r for r in rows if r["realized_pl"] > 0], key=lambda r: (-r["realized_pl"], r["id"]))[:n]
    l = sorted([r for r in rows if r["realized_pl"] < 0], key=lambda r: (r["realized_pl"], r["id"]))[:n]
    return [r["id"] for r in w + l]


def _build_queue(conn, today: str) -> list:
    _prelink_lessons(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM rh_trades WHERE coalesce(record_deleted,0)=0 LIMIT ?", (_MAX_ROWS,))]
    last_close = max((r["closed_at"] or "" for r in rows), default="")
    pool = _ds_get(conn, "rhx_story_pool")
    if not pool:
        # seed: the top 20 winners + 20 losers (in scope, and overall so a scope flip needs no re-seed)
        ids = list(dict.fromkeys(_top([r for r in rows if _story_eligible(r)], 20)
                                 + _top([r for r in rows if _story_eligible(r, scope=False)], 20)))
        pool = {"seeded": today, "ids": ids, "cutoff": last_close}
    else:
        new = [r["id"] for r in rows if (r["closed_at"] or "") > (pool.get("cutoff") or "")
               and _story_eligible(r, scope=False) and r["id"] not in pool["ids"]]
        pool["ids"] = list(pool.get("ids") or []) + sorted(new)
        pool["cutoff"] = max(pool.get("cutoff") or "", last_close)
    _ds_set(conn, "rhx_story_pool", pool)
    member = set(pool["ids"])
    cands = [r for r in rows if r["id"] in member and _story_eligible(r)]
    w = sorted([r for r in cands if r["realized_pl"] > 0], key=lambda r: (-r["realized_pl"], r["id"]))
    l = sorted([r for r in cands if r["realized_pl"] < 0], key=lambda r: (r["realized_pl"], r["id"]))
    n = _int_cfg("history", "stories_per_day", 5, lo=0, hi=50)
    out = []
    while len(out) < n and (w or l):
        side = "winner" if len(out) % 2 == 0 else "loser"
        src = w if side == "winner" else l
        if not src:
            src, side = (l, "loser") if side == "winner" else (w, "winner")
        out.append({"id": src.pop(0)["id"], "slot": side})
    return out


def stories_queue() -> list:
    """Today's Trade Stories (≤ stories_per_day), fixed for the ET day. Scope is re-applied on read."""
    if not enabled():
        return []
    try:
        today = _today_et().isoformat()
        conn = mason_db.get_connection()
        try:
            qd = _ds_get(conn, "rhx_story_queue")
            if not qd or qd.get("date") != today:
                items = _build_queue(conn, today)
                qd = {"date": today, "items": items}
                _ds_set(conn, "rhx_story_queue", qd)
                for it in items:
                    conn.execute("UPDATE rh_trades SET story_status='requested' WHERE id=? AND "
                                 "coalesce(story_status,'none')='none'", (it["id"],))
                conn.commit()
            out = []
            for it in qd.get("items") or []:
                r = conn.execute("SELECT * FROM rh_trades WHERE id=? AND coalesce(record_deleted,0)=0",
                                 (it.get("id"),)).fetchone()
                if r is None:
                    continue
                r = dict(r)
                if not _in_scope(r["account_label"]) or r.get("is_multileg") or \
                        (r.get("lesson_id") and r.get("story_status") != "answered"
                         and (r.get("story_status") or "none") != "skipped" and r.get("verdict") is None):
                    continue
                out.append(dict(_item(r), slot=it.get("slot")))
            return out
        finally:
            conn.close()
    except Exception as e:
        _err("stories_queue", e)
        return []


def _horizon(hold) -> str:
    if hold is None:
        return "unknown"
    return "short" if hold < 7 else ("swing" if hold < 60 else "long")


def _instrument(r: dict) -> str:
    if r.get("kind") in ("option", "crypto"):
        return r["kind"]
    return "etf" if (_jl(r.get("raw_json"), {}) or {}).get("instrument") == "etf" else "equity"


def _insert_lesson(conn, row: dict):
    cols = list(row)
    conn.execute(f"INSERT INTO director_lessons ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])


def _insert_thesis(conn, row: dict):
    cols = list(row)
    conn.execute(f"INSERT INTO theses ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])


def _validate(body):
    if not isinstance(body, dict):
        return None, ["body must be a JSON object: {thesis, verdict, note} or {skip: true}"]
    if body.get("skip") is True:
        return {"skip": True}, []
    errs = []
    thesis, verdict, note = body.get("thesis"), body.get("verdict"), body.get("note")
    if not isinstance(thesis, str) or not thesis.strip():
        errs.append("thesis is required (text, at most 280 characters)")
    elif len(thesis.strip()) > _THESIS_MAX:
        errs.append(f"thesis must be at most {_THESIS_MAX} characters")
    if verdict not in VERDICTS:
        errs.append("verdict must be one of right, wrong, luck, timing")
    if note is not None and not isinstance(note, str):
        errs.append("note must be text")
    elif isinstance(note, str) and len(note) > _NOTE_MAX:
        errs.append(f"note must be at most {_NOTE_MAX} characters")
    if errs:
        return None, errs
    return {"skip": False, "thesis": thesis.strip(), "verdict": verdict, "note": (note or "").strip()}, []


def answer_story(trade_id, body) -> tuple:
    """(ok, data | [errors], http_status). Answer → rh_trades + director_lessons + theses, one transaction."""
    if not enabled():
        return False, {"enabled": False}, 200
    v, errs = _validate(body)
    if errs:
        return False, errs, 400
    conn = None
    try:
        conn = mason_db.get_connection()
        r = conn.execute("SELECT * FROM rh_trades WHERE id=? AND coalesce(record_deleted,0)=0",
                         (str(trade_id),)).fetchone()
        if r is None:
            return False, ["trade not found"], 404
        r = dict(r)
        if not _in_scope(r["account_label"]):
            return False, ["this account is outside Nico's personal scope (rh_expansion.personal_scope)"], 403
        if r.get("is_multileg") or (_jl(r.get("raw_json"), {}) or {}).get("parse") == "unverified":
            return False, ["multi-leg or unverified trades are excluded from lessons"], 403
        if r.get("lesson_id") or r.get("story_status") == "answered":
            why = ("this trade's story is already answered" if r.get("story_status") == "answered"
                   else "this trade is already covered by an existing lesson; no second lesson was written")
            return False, {"trade_id": r["id"], "lesson_id": r.get("lesson_id"), "thesis_id": r.get("thesis_id"),
                           "errors": [why]}, 409
        tid = r["id"]
        if v["skip"]:
            conn.execute("UPDATE rh_trades SET story_status='skipped' WHERE id=?", (tid,))
            conn.commit()
            return True, {"trade_id": tid, "lesson_id": None, "thesis_id": None}, 200
        verdict, thesis, note = v["verdict"], v["thesis"], v["note"]
        now = _now_iso()
        ticker = r.get("underlying") or r.get("symbol")
        tags = ["src:personal", f"horizon:{_horizon(r.get('holding_days'))}", f"instrument:{_instrument(r)}",
                f"verdict:{verdict}"]
        lesson = {"id": uuid.uuid4().hex[:12], "rec_id": None, "ticker": ticker, "event_kind": None,
                  "outcome": _OUTCOME[verdict], "lesson": f"{thesis} → {verdict}: {note}" if note else f"{thesis} → {verdict}",
                  "tags": json.dumps(tags), "source_slate_id": f"rh_trades:{tid}", "created_date": now,
                  "created_by": MODULE, "record_deleted": 0}
        bearish = r.get("kind") == "option" and r.get("option_type") == "put"
        thesis_row = {"id": uuid.uuid4().hex[:12], "tickers": json.dumps([ticker]),
                      "title": f"{ticker} {str(r.get('closed_at') or '')[:10]} trade (Nico's story)",
                      "thesis": thesis, "direction": "bearish" if bearish else "bullish",
                      "confirm_signals": "[]", "break_signals": "[]",
                      "source_refs": json.dumps([{"table": "rh_trades", "id": tid, "date": str(r.get("closed_at") or "")[:10]}]),
                      "origin": "nico", "status": _THESIS_STATUS[verdict], "review_by": None, "linked_recs": "[]",
                      "extra_json": json.dumps({"rh_trade_id": tid, "verdict": verdict, "note": note,
                                                "lesson_id": lesson["id"], "tags": ["src:personal", f"verdict:{verdict}"]}),
                      "created_date": now, "created_by": MODULE, "record_deleted": 0}
        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE rh_trades SET thesis_text=?, verdict=?, verdict_note=?, story_status='answered',"
                         " thesis_id=?, lesson_id=? WHERE id=?",
                         (thesis, verdict, note, thesis_row["id"], lesson["id"], tid))
            _insert_lesson(conn, lesson)
            _insert_thesis(conn, thesis_row)
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, [_err("answer_story (rolled back)", e)], 500
        return True, {"trade_id": tid, "lesson_id": lesson["id"], "thesis_id": thesis_row["id"]}, 200
    except Exception as e:
        try:
            if conn is not None and conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        return False, [_err("answer_story", e)], 500
    finally:
        if conn is not None:
            conn.close()


# ── personal priors (family "personal", via mason_kb_priors) ────────────────────────────────
def _kb_module():
    return importlib.import_module("mason_kb_priors")


def _dte_bucket(d: int) -> str:
    return "0-5" if d <= 5 else ("6-30" if d <= 30 else ("31-90" if d <= 90 else "90+"))


_SUMMARY_FMT = "{n} trades: hit rate {hit_rate:.0%}, average P&L ${mean:+,.2f}"


def personal_priors() -> dict:
    """Hit rate / average P&L samples for Nico's in-scope, single-leg, verified trades → upsert_prior.
    Every bucket with n ≥ 1 is sent; mason_kb_priors sets min_n_met against priors.min_n."""
    if not _priors_enabled():
        return {"enabled": False}
    try:
        kb = _kb_module()
        upsert = getattr(kb, "upsert_prior")
    except Exception as e:
        msg = _err("personal_priors: mason_kb_priors unavailable", e)
        return {"ok": False, "written": 0, "errors": [msg]}
    try:
        rows = mason_db.with_connection(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM rh_trades WHERE coalesce(record_deleted,0)=0 AND coalesce(is_multileg,0)=0"
            " AND realized_pl IS NOT NULL LIMIT ?", (_MAX_ROWS,))])
        rows = [r for r in rows if _in_scope(r["account_label"])
                and (_jl(r.get("raw_json"), {}) or {}).get("parse") != "unverified"]
        groups = {}

        def add(key, r):
            k = json.dumps(key, sort_keys=True)
            groups.setdefault(k, (key, []))[1].append(r)

        for r in rows:
            inst = _instrument(r)
            hold = r.get("holding_days")
            if hold is not None:
                h = _horizon(hold)
                add({"dim": "instrument_hold", "instrument": inst, "hold": h, "account": r["account_label"]}, r)
                add({"dim": "instrument_hold", "instrument": inst, "hold": h, "account": "all"}, r)
            if inst in ("equity", "etf"):
                add({"dim": "equity_vs_etf", "instrument": inst}, r)
            if r.get("kind") == "option" and r.get("expiry") and r.get("opened_at"):
                try:
                    dte = (date.fromisoformat(r["expiry"][:10]) - _et_date(r["opened_at"])).days
                except (TypeError, ValueError):
                    dte = None
                if dte is not None and dte >= 0:
                    add({"dim": "option_dte", "dte": _dte_bucket(dte)}, r)
        min_n = _int_cfg("priors", "min_n", 10, lo=1)
        written, met, below, errs = 0, 0, 0, []
        # T2-a1 / T5: bracket the pass so buckets that stop being produced (e.g. per-account Household rows
        # after a scope flip) are retired. The retire runs only after a pass with no errors.
        begin, retire = getattr(kb, "begin_run", None), getattr(kb, "retire_unrefreshed", None)
        run = begin("personal") if callable(begin) and callable(retire) else None
        for k in sorted(groups):
            key, rs = groups[k]
            rs = sorted(rs, key=lambda r: r["id"])
            try:
                row = upsert("personal", key, "all", [float(r["realized_pl"]) for r in rs],
                             summary_fmt=_SUMMARY_FMT, refs=[r["id"] for r in rs])
                if row is None:
                    raise RuntimeError("upsert_prior refused the row (returned None)")
                written += 1
                met, below = (met + 1, below) if len(rs) >= min_n else (met, below + 1)
            except Exception as e:
                errs.append(_err(f"personal_priors upsert {k}", e))
        retired = 0
        if run and not errs:
            retired = int(retire("personal", run) or 0)
        elif run is None:
            log.info("[rh_history] personal_priors: retire skipped (no begin_run/retire_unrefreshed, or priors off)")
        return {"ok": not errs, "written": written, "retired": retired, "min_n_met": met, "below_min_n": below,
                "trades": len(rows), "errors": errs}
    except Exception as e:
        return {"ok": False, "written": 0, "errors": [_err("personal_priors", e)]}


# ── status / jobs ─────────────────────────────────────────────────────────────────────────────
def status() -> dict:
    on = enabled()
    out = {"name": "rh_history", "enabled": on, "last_run": None, "rows": 0, "errors": [], "truncated": False,
           "rate_hits": 0, "xcheck_mismatch": 0, "unmatched": 0, "unverified_legs": 0, "multileg": 0,
           "lesson_prelinked": 0, "lesson_match_ambiguous": 0, "verify": {}}
    with _lock:
        out.update(last_run=_state["last_run"], errors=list(_state["errors"]), truncated=_state["truncated"],
                   verify=dict(_state["verify"]))
    try:
        out["rate_hits"] = int((RH.status().get("rate") or {}).get("rate_hits") or 0)
    except Exception:
        pass
    if not on:
        return out
    try:
        def _counts(c):
            one = lambda sql: c.execute(sql).fetchone()[0]
            return {"rows": one("SELECT count(*) FROM rh_trades WHERE coalesce(record_deleted,0)=0"),
                    "unmatched": one("SELECT count(*) FROM rh_trades WHERE coalesce(record_deleted,0)=0"
                                     " AND opened_at IS NULL"),
                    "multileg": one("SELECT count(*) FROM rh_orders WHERE coalesce(record_deleted,0)=0"
                                    " AND is_multileg=1"),
                    "unverified_legs": one("SELECT count(*) FROM rh_orders WHERE coalesce(record_deleted,0)=0"
                                           " AND kind='option' AND raw_json LIKE '%\"parse\": \"unverified\"%'"),
                    "lesson_prelinked": one("SELECT count(*) FROM rh_trades WHERE coalesce(record_deleted,0)=0"
                                            " AND raw_json LIKE '%\"prelinked_lesson\"%'"),
                    "lesson_match_ambiguous": int((_ds_get(c, "rhx_lesson_prelink") or {}).get("ambiguous") or 0)}
        out.update(mason_db.with_connection(_counts))
    except Exception as e:
        out["errors"] = (out["errors"] + [_redact(f"status counts: {type(e).__name__}: {e}")[:300]])[-20:]
    return out


def _daily():
    """16:40 ET: backfill → alert log → personal priors (reads the rh_trades this run just wrote).
    Each step is isolated so one failure never stops the next; never raises."""
    out = {}
    steps = (("backfill", backfill, None), ("alert_log", ingest_alert_log, None),
             ("personal_priors", personal_priors, _priors_enabled))
    for key, fn, gate in steps:
        try:
            if gate is None or gate():
                out[key] = fn()
        except Exception as e:
            out[key] = {"ok": False, "error": _err(f"job {key}", e)}
    return out


# ── S1 job contract (S1-T9) — the ONE shape every S1 module's jobs() returns; S2-T8's integrator consumes it.
#   S1 schedules nothing itself. Each entry is a dict with:
#     name          str, "rhx_*", unique across all modules
#     fn            zero-arg callable; NEVER raises; returns a dict summary. The flag checks live inside fn
#                   (rh_expansion.<block>.enabled AND robinhood.enabled), so the scheduler calls it
#                   unconditionally and a flag-off run is a cheap no-op ({} / {"enabled": False}).
#     at_et         "HH:MM" (24 h, America/New_York): once per trading day (Mon-Fri; skip NYSE holidays).
#       — or —
#     every_s       int seconds between runs, ONLY inside window_et;
#     window_et     ("HH:MM", "HH:MM") inclusive ET window on trading days (required with every_s, never alone).
#     after         optional list of job names that must have finished today before this one starts. The at_et
#                   times below already encode the order; `after` makes it explicit for a dependency-aware
#                   scheduler (when a predecessor overruns, wait for it rather than run on stale data).
#     run_on_deploy optional bool: also run ONCE right after a deploy/first start. Set on every personal-data
#                   prior writer, because mason_kb_priors hides personal aggregates written before T5-a1
#                   (no _sample_scope) until their writer re-runs (deploy note, mason_kb_priors.py:57-69).
#     note          optional str for operators.
#   Daily order (ET, trading days) — data before the priors computed from it:
#     09:10        rhx_pead_sweep                RH earnings → earnings_results (yesterday pm + today am reports)
#     09:45-15:45  rhx_option_bars_live_capture  every 30 min: 5-minute bars for contracts still live
#     15:55        rhx_option_bars_live_snapshot final pre-expiry capture + excursions for same-day plays
#     16:30        rhx_history_snapshot          rh_account_snaps (live lane, via frozen mason_robinhood.snapshot())
#     16:40        rhx_history_daily             rh_orders/rh_trades → alert log → family `personal` priors
#     16:50        rhx_option_bars_daily         needs today's rh_trades; day bars → excursions → `excursion` priors
#     17:05        rhx_regimes_daily             close bars settled; SPX/VIX/RSP/SPY via price_cache → labels → `regime`
#     17:25        rhx_pead_priors               grades stored reports via price_cache → family `pead`
#   mason_price_cache and mason_kb_priors return [] (lazy cache / writer-driven store; see their jobs()).
def jobs() -> list:
    """For S2's integrator to schedule (S1 touches no scheduler). Shape: the S1 job contract above."""
    return [{"name": "rhx_history_snapshot", "at_et": "16:30", "fn": snapshot_accounts,
             "note": "live lane: goes through the frozen mason_robinhood.snapshot() (REVIEW_T8 accepted)"},
            {"name": "rhx_history_daily", "at_et": "16:40", "fn": _daily, "run_on_deploy": True,
             "note": "backfill, then alert log, then personal priors (if rh_expansion.priors.enabled)"}]


def _norm_status(name: str, st) -> dict:
    st = st if isinstance(st, dict) else {}

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    errs = st.get("errors") or []
    errs = errs if isinstance(errs, list) else [errs]
    rows = st.get("rows")
    if isinstance(rows, dict):                  # per-table counts (option_bars) → their total
        rows = sum(_i(v) for v in rows.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
    return {"name": str(st.get("name") or name), "enabled": st.get("enabled") is True,
            "last_run": st.get("last_run") if isinstance(st.get("last_run"), (str, type(None))) else str(st.get("last_run")),
            "rows": _i(rows), "errors": [_redact(e)[:300] for e in errs][:5],
            "truncated": bool(st.get("truncated")), "rate_hits": _i(st.get("rate_hits")),
            "xcheck_mismatch": _i(st.get("xcheck_mismatch"))}


_PATH_RE = re.compile(r"""[^\s'"(),;<>]*[/\\][^\s'"(),;<>]*""")       # any token with a / or \ (abs, rel, Windows)
_FILE_RE = re.compile(r"""[^\s'"(),;<>/\\]+\.(?:json|tmp|pem|key|txt|db|sqlite3?|cfg|conf|ini|ya?ml|log|bak)\b""",
                      re.IGNORECASE)                                       # a bare file name (token file, …)


def _scrub_paths(msg) -> str:
    """Remove every path-like reference — absolute, relative, ~ or Windows-style, or a bare file name — and the
    token file's own name/stem (T-D6 R2, QA DEBUG_T-D6). Never raises."""
    s = str(msg)
    try:
        names = set()
        try:                                   # B1b: also the token path as mason_secrets resolves it
            import mason_secrets as _S
            _sp = _S.robinhood_token_file("")
        except Exception:
            _sp = ""
        for p in (RH._token_path(), (RH._cfg() or {}).get("token_file"), _sp):
            if p:
                q = Path(str(p).replace("\\", "/"))
                names |= {q.name, q.stem}
        s = _FILE_RE.sub("<path>", _PATH_RE.sub("<path>", s))
        for n in sorted((n for n in names if len(n) >= 4), key=len, reverse=True):
            s = s.replace(n, "<path>")
    except Exception:
        s = _FILE_RE.sub("<path>", _PATH_RE.sub("<path>", s))
    return s


def _rh_status_row() -> dict:
    """mason_robinhood.status() as an /api/rh/status module row (e.g. the F-ACCT "configured account … not
    found" warning, RULINGS #28). Errors lose file paths (token file) here; account numbers are masked by
    _norm_status. Never calls RH. Only name/enabled/errors carry data: rows / rate_hits / last_run stay 0/None so
    the rhStatus widget's summed pills (rows, rate-limit hits — already copied into every module row — and last
    sync) do not change."""
    st = RH.status() or {}
    return {"name": "robinhood", "enabled": st.get("enabled") is True, "last_run": None, "rows": 0,
            "truncated": False, "rate_hits": 0,
            "errors": [_scrub_paths(e) for e in (st.get("errors") or [])]}


def all_status() -> dict:
    """{connected, modules:[...]} over this module and every other RH-expansion module that exists.
    Every module's status() is cheap (bounded queries, no RH call, never raises) and carries at least
    name, enabled, last_run, rows, errors, truncated, rate_hits, xcheck_mismatch; _norm_status trims each to
    exactly those keys (rows: int, a per-table dict is summed; truncated: bool; errors: redacted, ≤5).
    The matching jobs() shape is the S1 job contract comment above jobs()."""
    mods = [_norm_status("rh_history", status())]
    try:                                        # T-D6 / RULINGS #28 + #31(a): mason_robinhood's own errors
        mods.append(_norm_status("robinhood", _rh_status_row()))
    except Exception as e:
        mods.append(_norm_status("robinhood", {"errors": [f"status() failed: {type(e).__name__}"]}))
    for modname in _STATUS_MODULES:
        short = modname.replace("mason_", "", 1)
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            if e.name == modname:
                continue                                    # not built yet → absent
            mods.append(_norm_status(short, {"errors": [f"import failed: {type(e).__name__}: {e}"]}))
            continue
        except Exception as e:
            mods.append(_norm_status(short, {"errors": [f"import failed: {type(e).__name__}: {e}"]}))
            continue
        fn = getattr(mod, "status", None)
        if not callable(fn):
            mods.append(_norm_status(short, {"errors": ["module has no status()"]}))
            continue
        try:
            mods.append(_norm_status(short, fn()))
        except Exception as e:
            mods.append(_norm_status(short, {"errors": [f"status() failed: {type(e).__name__}: {e}"]}))
    try:
        connected = RH.status().get("connected") is True
    except Exception:
        connected = False
    return {"connected": connected, "modules": mods}


# ── HTTP (contracts §6) ───────────────────────────────────────────────────────────────────────
bp = Blueprint("rh_history", __name__)


def _json(obj, code=200) -> Response:
    """JSON with <, > and & escaped as \\u003c/\\u003e/\\u0026 so no response ever carries raw HTML,
    while the parsed strings stay exactly what Nico typed."""
    body = json.dumps(obj, ensure_ascii=True, default=str)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Response(body, status=code, mimetype="application/json")


def _env(data, *, errors=None, ok=True, stale=False, code=200) -> Response:
    return _json({"ok": ok, "as_of": _now_iso(), "data": data, "stale": stale, "errors": errors or []}, code)


@bp.route("/api/rh/history/trades", methods=["GET"])
def api_trades():
    if not enabled():
        return _json({"enabled": False})
    try:
        a = request.args
        return _env(trades(account=a.get("account") or None, symbol=a.get("symbol") or None,
                           verdict=a.get("verdict") or None, limit=a.get("limit") or 100))
    except Exception as e:
        return _env([], ok=True, stale=True, errors=[_err("api trades", e)])


@bp.route("/api/rh/history/stories", methods=["GET"])
def api_stories():
    if not enabled():
        return _json({"enabled": False})
    try:
        return _env(stories_queue())
    except Exception as e:
        return _env([], stale=True, errors=[_err("api stories", e)])


@bp.route("/api/rh/history/stories/<trade_id>", methods=["POST"])
def api_story_answer(trade_id):
    if not enabled():
        return _json({"enabled": False})
    try:
        body = request.get_json(silent=True)
        ok, data, code = answer_story(trade_id, body)
        if ok:
            return _env(data)
        if isinstance(data, dict) and "errors" in data:
            errs = data.pop("errors")
            return _env(data, ok=False, errors=errs, code=code)
        return _env(None, ok=False, errors=data if isinstance(data, list) else [str(data)], code=code)
    except Exception as e:
        return _env(None, ok=False, errors=[_err("api story answer", e)], code=500)


@bp.route("/api/rh/status", methods=["GET"])
def api_status():
    if not enabled():
        return _json({"enabled": False})
    try:
        return _env(all_status())
    except Exception as e:
        return _env({"connected": False, "modules": []}, stale=True, errors=[_err("api status", e)])


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = ("usage: python3 mason_rh_history.py backfill --live | status | reconcile\n"
             "  backfill needs --live, robinhood.enabled and rh_expansion.history.enabled (Nico's go-live step)")
    if not argv:
        print(usage)
        return 2
    verb = argv[0]
    if verb == "backfill":
        if "--live" not in argv:
            print(usage)
            return 2
        if not enabled():
            print("refused: robinhood.enabled and rh_expansion.history.enabled must both be true")
            return 2
        res = backfill()
        res["alert_log"] = ingest_alert_log()
        print(json.dumps(res, indent=1, default=str))
        return 0 if res.get("ok") else 1
    if verb == "status":
        print(json.dumps(all_status(), indent=1, default=str))
        return 0
    if verb == "reconcile":
        print(json.dumps(reconcile_report(), indent=1, default=str))
        return 0
    print(usage)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
