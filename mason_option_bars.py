"""mason_option_bars.py — option price paths + MFE/MAE re-grade (RH Expansion S1-T4, X-G2).

Spec: docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §2 T4; binding rulings in
docs/robinhood/expansion/S1/REVIEW_T1.md (ruling 1: bar granularity).

What it does, in order (backfill()):
  1. resolve_contracts()  every single-leg option in options_trades / recommendations (read-only)
                          and rh_trades / rh_orders → OCC symbol → RH instrument id, cached in
                          option_contracts (resolved_via = chain | rh_order | unresolved).
  2. backfill_bars()      get_option_historicals → option_bars. Expired contracts: `day` bars.
                          Live contracts: `day` + config interval_intraday (5minute) bars.
  3. compute_excursions() MFE/MAE from NON-interpolated bars only → trade_excursions, with
                          settle_px precedence Alpaca fill → option-bar close on expiry →
                          intrinsic off the underlying's expiry-date close (T3 price cache).
  4. excursion_priors()   family `excursion` through mason_kb_priors.upsert_prior (T5).
regrade_report() writes docs/robinhood/expansion/S1/regrade_diff.csv. It writes back to
options_trades / recommendations ONLY when rh_expansion.option_bars.regrade_write is true (Nico's
call after reading the CSV); old values go to extra_json.regrade_rhx_<YYYYMMDD>. The 6/19-6/22
voided plays are reported, never regraded.

Day-bar rule (REVIEW_T1 ruling 1.2): the entry-session and exit-session bars count by their CLOSE
only; full sessions strictly between use high/low; a same-session trade on day bars gets
MFE/MAE = None with grade_json.reason="same_session_day_bars". The same entry/exit-bar rule is
applied to intraday bars (the bar containing the entry/exit contributes its close only).

Boundaries (contracts §2): Robinhood only through mason_robinhood (paged_all → _call), reads only.
Never creates a recommendations row. Alpaca, the price cache and the priors library are reached
only through the module hooks below, each imported lazily. Never raises from a public function.
"""

import csv
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db
import mason_robinhood as _RH

log = logging.getLogger("mason.option_bars")

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
REGRADE_CSV = ROOT / "docs" / "robinhood" / "expansion" / "S1" / "regrade_diff.csv"
CREATED_BY = "mason_option_bars"
NAME = "option_bars"

RH_RETENTION_EDGE = date(2017, 9, 15)          # PROBE bullet 4: oldest expiry RH still resolves
VOID_WINDOW = ("2026-06-19", "2026-06-22")     # audit_2026_09_28: never-executed journal plays
INSTRUMENT_MAX_PAGES = 5
MAX_REFS = 20000
MAX_CONTRACT_LOOKUPS = 2000
MAX_BAR_CALLS = 400
DAY_CHUNK = 10                                 # get_option_historicals: ≤10 instruments per call
DAY_CHUNK_SPAN_DAYS = 400
INTRADAY_LOOKBACK_DAYS = 30                    # ≤ ~64 sessions fits RH's 5,000-bar cap at 5minute
UNRESOLVED_RETRY_DAYS = 7
INTERP_FALLBACK_SHARE = 0.5
_INTERVAL_S = {"15second": 15, "30second": 30, "minute": 60, "5minute": 300, "10minute": 600,
               "30minute": 1800, "hour": 3600, "4hour": 14400}
REF_TABLES = ("options_trades", "recommendations", "rh_trades")

_DEFAULTS = {"enabled": False, "interval_intraday": "5minute", "regrade_write": False}

# ── hooks (tests replace these; production imports lazily) ───────────────────
ALPACA_HOOK = None             # (alpaca_symbol) -> mason_execution.settlement_disposition() dict
UNDERLYING_CLOSE_HOOK = None   # (ticker, "YYYY-MM-DD") -> raw close on/before that date, or None
PRIORS_HOOK = None             # mason_kb_priors.upsert_prior
BEGIN_HOOK = None              # mason_kb_priors.begin_run       (T4-a1, REVIEW_T5 ruling 2)
RETIRE_HOOK = None             # mason_kb_priors.retire_unrefreshed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _import_priors():
    import mason_kb_priors          # T5; may not exist yet
    return mason_kb_priors.upsert_prior


def _import_run_hooks():
    import mason_kb_priors
    return mason_kb_priors.begin_run, mason_kb_priors.retire_unrefreshed


def _default_alpaca(symbol):
    import mason_execution
    return mason_execution.settlement_disposition(symbol)


_raw_close_cache = {}           # (TICKER, day) -> raw close | None (only cached after an error-free answer)


def _session_expected(d: date) -> bool:
    """Was `d` a trading session? A holiday expiry may walk back; a normal one must match exactly.
    RULINGS #30: mason_options.nyse_session_expected (full NYSE rule calendar, verified vs XNYS 2015-2030);
    if that is unavailable or fails → mason_director.is_trading_day (its holiday list starts 2026-11-26)
    → weekday only. Never raises."""
    try:
        import mason_options        # lazy: stdlib + mason_db only at import
        return bool(mason_options.nyse_session_expected(d))
    except Exception:
        pass
    try:
        import mason_director
        return bool(mason_director.is_trading_day(datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)))
    except Exception:
        return d.weekday() < 5


def _pick_close(pairs, day):
    """pairs [(YYYY-MM-DD, close)] → the close ON `day`, or the last one before it only when `day`
    was not a session (holiday expiry)."""
    pairs = sorted(p for p in pairs if p[0] <= day and p[1] is not None)
    if not pairs:
        return None, None
    if pairs[-1][0] != day and _session_expected(date.fromisoformat(day)):
        return None, None
    return pairs[-1]


def _rh_raw_close(ticker, day):
    """D1 (REVIEW_T4): the RAW official expiry-day print — RH get_equity_historicals with
    adjustment_type='none' (read-only, allowlisted, bulk lane), one cached call per (ticker, day)."""
    key = (str(ticker).upper(), day)
    if key in _raw_close_cache:
        return _raw_close_cache[key]
    if not _RH.enabled():
        return None
    d = date.fromisoformat(day)
    rows, meta = _RH.paged_all("get_equity_historicals",
                               {"symbols": [key[0]], "interval": "day", "adjustment_type": "none",
                                "start_time": f"{d - timedelta(days=7)}T00:00:00Z",
                                "end_time": f"{d + timedelta(days=1)}T00:00:00Z", "bounds": "regular"},
                               rows_key="results", max_pages=1, lane="bulk")
    for e in meta.get("errors") or []:
        _err("get_equity_historicals (raw close)", e)
    pairs = []
    for res in rows:
        if not isinstance(res, dict) or str(res.get("symbol") or key[0]).upper() != key[0]:
            continue
        for bar in res.get("bars") or []:
            if isinstance(bar, dict) and bar.get("interpolated") is not True and bar.get("begins_at"):
                pairs.append((str(bar["begins_at"])[:10], _pos(bar.get("close_price"))))
    val = _pick_close(pairs, day)[1]
    if not meta.get("errors"):
        _raw_close_cache[key] = val
    return val


def _t3_unadjusted_close(ticker, day):
    """Fallback: T3's split-adjusted close × Π split ratios dated AFTER the expiry → the raw print."""
    import mason_price_cache        # T3 — never re-implemented here
    d = date.fromisoformat(day)
    rows = mason_price_cache.get_daily(ticker, (d - timedelta(days=7)).isoformat(), _today_et().isoformat()) or []
    pairs, factor = [], 1.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        rd = str(r.get("date") or "")[:10]
        pairs.append((rd, _pos(r.get("close"))))
        if rd > day:
            xj = _jload(r.get("xcheck_json"), {}) or {}
            ratio = _pos(xj.get("split")) if isinstance(xj, dict) else None
            if ratio:
                factor *= ratio
    close = _pick_close(pairs, day)[1]
    return round(close * factor, 6) if close else None


def _default_underlying_close(ticker, day):
    v = _rh_raw_close(ticker, day)
    if v:
        return {"close": v, "basis": "rh_raw"}
    v = _t3_unadjusted_close(ticker, day)
    if v:
        return {"close": v, "basis": "t3_unadjusted"}
    return None


# ── state / errors ───────────────────────────────────────────────────────────
_state = {}


def _reset_state():
    _state.clear()
    _state.update({"last_run": None, "errors": [], "truncated": 0, "unresolved": 0,
                   "priors_error": None, "last_regrade": None, "last_summary": {}})


_reset_state()

_ACCT_RE = re.compile(r"(?<![\w.])\d{8,12}(?![\w.])")     # standalone account-number-length runs


def _redact(s) -> str:
    return _ACCT_RE.sub(lambda m: "••" + m.group(0)[-4:], str(s))


def _err(where, e=None) -> str:
    msg = _redact(f"{where}: {type(e).__name__}: {e}" if e is not None else str(where))[:300]
    _state["errors"] = (_state["errors"] + [msg])[-50:]
    log.warning(f"[option_bars] {msg}")
    return msg


# ── config ───────────────────────────────────────────────────────────────────
def _rhx() -> dict:
    try:
        return _RH._config().get("rh_expansion") or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        for k, v in (_rhx().get("option_bars") or {}).items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def _intraday() -> str:
    iv = _cfg().get("interval_intraday")
    return iv if iv in _INTERVAL_S else "5minute"


def _exclude_accounts() -> set:
    """Normalized excluded labels via the shared parser (mason_robinhood, RULINGS #1). Fails closed."""
    try:
        return set(_RH.resolve_exclude_accounts(_RH._config().get("rh_expansion"),
                                                log=lambda m: _err(m), source="mason_option_bars"))
    except Exception:
        return {"household"}


def enabled() -> bool:
    """Master switch robinhood.enabled AND rh_expansion.option_bars.enabled."""
    try:
        return bool(_RH.enabled()) and _cfg().get("enabled") is True
    except Exception:
        return False


# ── small helpers ────────────────────────────────────────────────────────────
def _pos(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 and f != float("inf") else None


def _nonneg_or_none(v):
    return _pos(v)


def _parse_ts(v):
    """Aware UTC datetime. Naive journal timestamps are Mason-local ET; a bare date means
    'that session' and is pinned to the 09:30 ET open."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        try:
            if len(s) == 10:
                d = date.fromisoformat(s)
                return datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(timezone.utc)
            s = s.replace("Z", "+00:00")
            m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)          # nanoseconds → micro
            if m:
                s = m.group(1) + m.group(2)
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(timezone.utc)


def _iso(dt) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _et_date(dt) -> date:
    return dt.astimezone(ET).date()


def _today_et() -> date:
    return _now().astimezone(ET).date()


def _expiry_close(exp: date) -> datetime:
    return datetime(exp.year, exp.month, exp.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)


def _is_expired(exp: date) -> bool:
    return _now() >= _expiry_close(exp) + timedelta(minutes=15)


def _jload(s, default=None):
    if isinstance(s, (dict, list)):
        return s
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except Exception:
        return default


def _r(v, n=4):
    return None if v is None else round(float(v), n)


# ── OCC symbols ──────────────────────────────────────────────────────────────
_OCC_RE = re.compile(r"^\s*([A-Z][A-Z0-9.]{0,5})\s*(\d{6})([CP])(\d{8})\s*$")


def occ_symbol(underlying, expiry, option_type, strike) -> str | None:
    """'NVDA  261218C00200000' (root padded to 6, YYMMDD, C/P, strike×1000 in 8 digits)."""
    try:
        u = str(underlying or "").strip().upper()
        d = date.fromisoformat(str(expiry)[:10])
        t = str(option_type or "").strip().lower()
        if t not in ("call", "put", "c", "p") or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}", u):
            return None
        k = int(round(float(strike) * 1000))
        if k <= 0 or k >= 10 ** 8:
            return None
        return f"{u:<6}{d:%y%m%d}{t[0].upper()}{k:08d}"
    except Exception:
        return None


def parse_occ(s) -> dict | None:
    """Padded OCC or Alpaca's un-padded form → {underlying, expiry, option_type, strike}."""
    if not isinstance(s, str):
        return None
    m = _OCC_RE.match(s.upper())
    if not m:
        return None
    try:
        d = datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None
    return {"underlying": m.group(1), "expiry": d.isoformat(),
            "option_type": "call" if m.group(3) == "C" else "put",
            "strike": int(m.group(4)) / 1000.0}


def intrinsic(option_type, strike, underlying_close):
    s, k = _pos(underlying_close), _pos(strike)
    if s is None or k is None:
        return None
    return round(max(0.0, s - k) if str(option_type).lower().startswith("c") else max(0.0, k - s), 4)


# ── refs: every trade this module grades (source tables are READ only) ──────
def _is_voided(row) -> bool:
    if VOID_WINDOW[0] <= str(row.get("entry_date") or "")[:10] <= VOID_WINDOW[1]:
        return True
    if "voided" in str(row.get("notes") or "").lower():
        return True
    ex = _jload(row.get("extra_json"), {})
    audit = (ex.get("audit_2026_09_28") if isinstance(ex, dict) else None) or {}
    if isinstance(audit, dict):
        for v in audit.values():
            if isinstance(v, dict) and "VOIDED" in str(v.get("reason") or "").upper():
                return True
    return False


def _pnl_in_share_rows(row) -> bool:
    """(knowledge base review, 2026-11-19) A long option that was exercised into shares and put back into the journal
    by tools/kb_restore_fills.py (extra_json.kb_restore.pnl_in_share_rows = true). Its outcome and P&L stay empty ON
    PURPOSE: the premium is inside the shares' cost, so its money is already counted on the share rows. The regrade
    below must never write a result onto it (that would count the money twice). No row carries the key until the
    tool is run on Nico's yes. Never raises."""
    ex = _jload(row.get("extra_json"), {})
    kr = ex.get("kb_restore") if isinstance(ex, dict) else None
    return isinstance(kr, dict) and kr.get("pnl_in_share_rows") is True


def _ref_base(table, rid):
    return {"ref_table": table, "ref_id": rid, "occ": None, "underlying": None, "option_type": None,
            "strike": None, "expiry": None, "side": "long", "entry_ts": None, "entry_px": None,
            "exit_ts": None, "exit_px": None, "qty": 1.0, "stored_grade": None, "stored_pnl": None,
            "multileg": False, "voided": False, "account": None, "event_kind": None, "stop": None,
            "target": None, "alpaca_symbol": None, "rh_instrument_id": None, "ticker": None,
            "entry_time_known": True, "entry_basis": None, "extra": None,
            "src": {"options_trades": "paper", "recommendations": "rec", "rh_trades": "personal"}[table]}


def _set_contract(ref, underlying, expiry, option_type, strike):
    occ = occ_symbol(underlying, expiry, option_type, strike)
    if occ:
        p = parse_occ(occ)
        ref.update(occ=occ, underlying=p["underlying"], expiry=p["expiry"], option_type=p["option_type"],
                   strike=p["strike"])


def _spread_leg_marked(row) -> bool:
    """RULINGS #34 (amends #5, D3-b): options_trades.extra_json["spread_leg"] says "this row is one leg of a
    spread", so the row is multi-leg (contracts §2.4: out of bars, live capture, excursions, priors and regrade
    writes). FAIL CLOSED: the exact key `spread_leg` with ANY value (the D3 dict, {}, false, null, "", a string,
    a list, a number) marks the row. Only golive_fin.py d3 writes the key, so any other value means a hand edit
    or corruption; the single-leg rule is hard, and excluding one sample is the safe direction. A value that is
    not the canonical D3 dict ({"spread","decision","by","date"}) is still honoured and logs one warning per
    row. Unparseable or non-object extra_json cannot show a key, and a wrong-case or padded key name is not
    the marker → False. To un-mark a row, delete the key (golive_fin.py rollback does). Never raises."""
    ex = _jload(row.get("extra_json"), {})
    if not isinstance(ex, dict) or "spread_leg" not in ex:
        return False
    v = ex["spread_leg"]
    if not (isinstance(v, dict) and set(v) == {"spread", "decision", "by", "date"}):
        rid = row.get("id")
        if rid not in _spread_leg_warned:
            _spread_leg_warned.add(rid)
            log.warning("[option_bars] options_trades %s: non-canonical spread_leg marker — treated as "
                        "multi-leg (RULINGS #34) (value type %s)", rid, type(v).__name__)
    return True


_spread_leg_warned = set()      # options_trades ids already warned about a non-canonical spread_leg (read at call time)


def _refs_options_trades(conn) -> list:
    out = []
    for row in conn.execute("SELECT * FROM options_trades WHERE IFNULL(is_deleted,0)=0 "
                            "ORDER BY id LIMIT ?", (MAX_REFS,)):
        row = dict(row)
        ref = _ref_base("options_trades", row["id"])
        ref["ticker"] = row.get("ticker")
        strikes = _jload(row.get("strikes"), {}) or {}
        legs = [(k, v) for k, v in strikes.items() if v is not None] if isinstance(strikes, dict) else []
        ref["multileg"] = (len(legs) != 1 or "spread" in str(row.get("strategy") or "").lower()
                           or _spread_leg_marked(row))
        ref["voided"] = _is_voided(row)
        if _pnl_in_share_rows(row):                  # (KB review 2026-11-19) read by regrade_report only
            ref["pnl_in_share_rows"] = True
        if len(legs) == 1:
            key, k = legs[0]
            ref["side"] = "short" if str(key).lower().startswith("short") else "long"
            otype = "put" if "put" in str(key).lower() else "call"
            _set_contract(ref, row.get("ticker"), row.get("expiration"), otype, k)
        p = parse_occ(row.get("contract_symbol"))
        if p and not ref["multileg"]:
            _set_contract(ref, p["underlying"], p["expiry"], p["option_type"], p["strike"])
        ref["entry_px"] = _pos(row.get("filled_avg_price")) or _pos(row.get("premium"))
        ref["entry_basis"] = "fill" if _pos(row.get("filled_avg_price")) else "premium"
        ref["entry_ts"] = _parse_ts(row.get("filled_at"))
        if ref["entry_ts"] is None:
            ref["entry_ts"] = _parse_ts(row.get("entry_date"))
            ref["entry_time_known"] = False
        ref["exit_px"] = _pos(row.get("exit_filled_avg_price"))
        ref["exit_ts"] = _parse_ts(row.get("exit_filled_at")) if ref["exit_px"] is not None else None
        if ref["exit_px"] is not None and ref["exit_ts"] is None:
            ref["exit_px"] = None
        ref["qty"] = _pos(row.get("filled_qty")) or _pos(row.get("contracts")) or 1.0
        ref["stored_grade"] = row.get("outcome")
        ref["stored_pnl"] = row.get("actual_pnl")
        if row.get("contract_symbol") and not ref["voided"]:
            ref["alpaca_symbol"] = str(row["contract_symbol"]).strip().upper()
        out.append(ref)
    return out


def _catalyst_kinds(conn, ids) -> dict:
    ids = [i for i in ids if isinstance(i, str) and i]
    out = {}
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT id, event_kind FROM catalysts WHERE id IN "
                                  f"({','.join('?' for _ in chunk)})", chunk):
                out[r["id"]] = r["event_kind"]
    except Exception as e:
        _err("catalyst kinds", e)
    return out


def _fills_index(paper_refs) -> dict:
    """occ → [(fill_ts, fill_px)] of executed paper trades, to find a rec that was actually filled."""
    out = {}
    for r in paper_refs:
        if r["occ"] and r["entry_basis"] == "fill" and r["entry_time_known"] and r["entry_ts"]:
            out.setdefault(r["occ"], []).append((r["entry_ts"], r["entry_px"]))
    return out


def _refs_recommendations(conn, fills=None) -> list:
    raw = []
    for row in conn.execute("SELECT id, created_date, ticker, direction, outcome, extra_json FROM recommendations "
                            "WHERE IFNULL(record_deleted,0)=0 ORDER BY id LIMIT ?", (MAX_REFS,)):
        row = dict(row)
        ex = _jload(row.get("extra_json"), {}) or {}
        tr = ex.get("trade") if isinstance(ex, dict) else None
        if not isinstance(tr, dict) or str(tr.get("vehicle") or "").lower() not in ("call", "put"):
            continue
        raw.append((row, ex, tr))
    kinds = _catalyst_kinds(conn, [ex.get("catalyst_id") for _, ex, _ in raw])
    out = []
    for row, ex, tr in raw:
        ref = _ref_base("recommendations", row["id"])
        ref["ticker"] = row.get("ticker")
        _set_contract(ref, row.get("ticker"), tr.get("expiry"), tr.get("vehicle"), tr.get("strike"))
        legs = tr.get("legs")
        ref["multileg"] = isinstance(legs, list) and len(legs) > 1
        ref["side"] = "short" if str(tr.get("side") or "").lower() in ("sell", "short") else "long"
        # entry price precedence (REVIEW_T4 Q6): actual fill → entry_limit → bid/ask mid. Never the ask,
        # the last trade, or a post-entry price.
        q = tr.get("contract_quote") if isinstance(tr.get("contract_quote"), dict) else {}
        mid = None
        if _pos(q.get("bid")) is not None and _pos(q.get("ask")):
            mid = (_pos(q["bid"]) + _pos(q["ask"])) / 2
        ref["entry_ts"] = _parse_ts(row.get("created_date"))
        fill = None
        if ref["occ"] and ref["entry_ts"]:
            c0 = ref["entry_ts"]
            fill = next((f for f in sorted((fills or {}).get(ref["occ"], []), key=lambda f: f[0])
                         if c0 <= f[0] <= c0 + timedelta(days=1) and f[1]), None)
        if fill:
            ref["entry_px"], ref["entry_basis"], ref["entry_ts"] = fill[1], "fill", fill[0]
        elif _pos(tr.get("entry_limit")):
            ref["entry_px"], ref["entry_basis"] = _pos(tr.get("entry_limit")), "limit"
        elif mid:
            ref["entry_px"], ref["entry_basis"] = mid, "mid"
        ref["extra"] = ex
        ref["stop"] = _pos(tr.get("stop"))
        ref["target"] = _pos(tr.get("target"))
        ref["stored_grade"] = row.get("outcome")
        ref["event_kind"] = ex.get("event_kind") or kinds.get(ex.get("catalyst_id"))
        ref["voided"] = _is_voided({"extra_json": row.get("extra_json")})
        out.append(ref)
    return out


def _refs_rh_trades(conn) -> list:
    orders = {}
    for o in conn.execute("SELECT rh_order_id, account_label, underlying, symbol, option_type, strike, expiry, "
                          "rh_instrument_id FROM rh_orders WHERE kind='option' AND IFNULL(record_deleted,0)=0 "
                          "AND rh_instrument_id IS NOT NULL LIMIT ?", (MAX_REFS,)):
        orders.setdefault(o["rh_order_id"], dict(o))
    out = []
    for row in conn.execute("SELECT * FROM rh_trades WHERE kind='option' AND IFNULL(record_deleted,0)=0 "
                            "ORDER BY id LIMIT ?", (MAX_REFS,)):
        row = dict(row)
        ref = _ref_base("rh_trades", row["id"])
        ref["account"] = row.get("account_label")
        ref["ticker"] = row.get("underlying") or row.get("symbol")
        raw = _jload(row.get("raw_json"), {}) or {}
        ref["multileg"] = bool(row.get("is_multileg")) or (isinstance(raw, dict) and raw.get("parse") == "unverified")
        _set_contract(ref, ref["ticker"], row.get("expiry"), row.get("option_type"), row.get("strike"))
        closing = str(row.get("side") or "").lower()
        ref["side"] = "short" if closing == "buy" else "long"
        q, px, pl = _pos(row.get("qty")), _pos(row.get("price")), row.get("realized_pl")
        try:
            pl = float(pl)
        except (TypeError, ValueError):
            pl = None
        if q and px is not None and pl is not None:
            per = pl / (q * 100.0)
            ref["entry_px"] = _pos(px - per if ref["side"] == "long" else px + per)
            ref["entry_basis"] = "fill_derived"
        ref["qty"] = q or 1.0
        ref["exit_px"] = px
        ref["exit_ts"] = _parse_ts(row.get("closed_at"))
        ref["entry_ts"] = _parse_ts(row.get("opened_at"))
        ref["stored_grade"] = row.get("verdict")
        ref["stored_pnl"] = pl
        for oid in _jload(row.get("open_order_ids"), []) or []:
            o = orders.get(oid)
            if o and o.get("rh_instrument_id"):
                ref["rh_instrument_id"] = o["rh_instrument_id"]
                break
        if not ref["rh_instrument_id"] and ref["occ"]:
            for o in orders.values():
                if (o.get("account_label") == row.get("account_label")
                        and occ_symbol(o.get("underlying") or o.get("symbol"), o.get("expiry"),
                                       o.get("option_type"), o.get("strike")) == ref["occ"]):
                    ref["rh_instrument_id"] = o["rh_instrument_id"]
                    break
        out.append(ref)
    return out


def _load_refs(conn) -> list:
    refs, paper = [], []
    try:
        paper = _refs_options_trades(conn)
        refs.extend(paper)
    except Exception as e:
        _err("_refs_options_trades", e)
    for fn, args in ((_refs_recommendations, (conn, _fills_index(paper))), (_refs_rh_trades, (conn,))):
        try:
            refs.extend(fn(*args))
        except Exception as e:
            _err(fn.__name__, e)
    return refs


def _in_scope(ref, excl) -> bool:
    """Row-side label normalized like the config side (mason_robinhood.norm_account_label, T-D6); a ref
    with no account (paper) stays in scope."""
    lab = _RH.norm_account_label(ref.get("account")) if ref.get("account") else ""
    return not (lab and lab in excl)


# ── RH calls (bulk lane, through paged_all → _call) ──────────────────────────
def _instruments(args, max_pages=INSTRUMENT_MAX_PAGES):
    rows, meta = _RH.paged_all("get_option_instruments", args, cursor_key="cursor", next_key="next",
                               rows_key="instruments", max_pages=max_pages, lane="bulk")
    if meta.get("truncated"):
        _state["truncated"] += 1
    for e in meta.get("errors") or []:
        _err("get_option_instruments", e)
    return rows, meta


def instrument_by_id(iid, *, expired=True) -> dict | None:
    """RH instrument row by id. Expired contracts only come back with state='expired' (PROBE
    bullet 4), so that state is tried first for expired ones, then without it."""
    if not enabled() or not isinstance(iid, str) or not iid:
        return None
    try:
        for st in (("expired", None) if expired else (None, "expired")):
            args = {"ids": iid}
            if st:
                args["state"] = st
            rows, meta = _instruments(args, max_pages=1)
            hit = next((r for r in rows if r.get("id") == iid), None)
            if hit:
                return hit
            if meta.get("errors"):
                return None
    except Exception as e:
        _err("instrument_by_id", e)
    return None


def _lookup_chain(p) -> tuple:
    """(instrument_id|None, complete:bool). complete=False → an error/truncation, don't conclude."""
    exp = date.fromisoformat(p["expiry"])
    states = ("expired", "active") if _is_expired(exp) else ("active", "expired")
    for st in states:
        rows, meta = _instruments({"chain_symbol": p["underlying"], "expiration_dates": p["expiry"],
                                   "state": st, "type": p["option_type"],
                                   "strike_price": f"{p['strike']:.4f}"})
        for r in rows:
            try:
                if (str(r.get("expiration_date"))[:10] == p["expiry"] and r.get("type") == p["option_type"]
                        and abs(float(r.get("strike_price")) - p["strike"]) < 1e-6 and r.get("id")):
                    return r["id"], True
            except (TypeError, ValueError):
                continue
        if meta.get("errors") or meta.get("truncated"):
            return None, False
    return None, True


def _upsert_contract(conn, occ, iid, via):
    p = parse_occ(occ) or {}
    conn.execute(
        "INSERT INTO option_contracts (occ, rh_instrument_id, underlying, option_type, strike, expiry, "
        "resolved_via, created_date) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(occ) DO UPDATE SET rh_instrument_id=excluded.rh_instrument_id, "
        "resolved_via=excluded.resolved_via WHERE option_contracts.rh_instrument_id IS NULL "
        "AND (excluded.rh_instrument_id IS NOT NULL OR option_contracts.resolved_via IS NOT excluded.resolved_via)",
        (occ, iid, p.get("underlying"), p.get("option_type"), p.get("strike"), p.get("expiry"), via,
         _iso(_now())))


def resolve_contracts() -> dict:
    """Cache OCC → RH instrument id in option_contracts for every graded single-leg contract."""
    if not enabled():
        return {}
    out = {"resolved": 0, "cached": 0, "unresolved": 0, "deferred": 0, "errors": []}
    n_err0 = len(_state["errors"])
    conn = None
    try:
        conn = mason_db.get_connection()
        cached = {r["occ"]: dict(r) for r in conn.execute("SELECT * FROM option_contracts")}
        # 1) contracts RH already told us about in rh_orders (no call unless fields are missing)
        for o in conn.execute("SELECT rh_instrument_id, MAX(underlying) u, MAX(symbol) s, MAX(option_type) t, "
                              "MAX(strike) k, MAX(expiry) e FROM rh_orders WHERE kind='option' AND "
                              "IFNULL(record_deleted,0)=0 AND rh_instrument_id IS NOT NULL "
                              "GROUP BY rh_instrument_id LIMIT ?", (MAX_CONTRACT_LOOKUPS,)).fetchall():
            iid = o["rh_instrument_id"]
            occ = occ_symbol(o["u"] or o["s"], o["e"], o["t"], o["k"])
            if not occ:
                exp_known = None
                try:
                    exp_known = date.fromisoformat(str(o["e"])[:10])
                except Exception:
                    pass
                ins = instrument_by_id(iid, expired=exp_known is None or _is_expired(exp_known))
                if ins:
                    occ = occ_symbol(ins.get("chain_symbol"), ins.get("expiration_date"), ins.get("type"),
                                     ins.get("strike_price"))
            if not occ:
                continue
            if (cached.get(occ) or {}).get("rh_instrument_id"):
                out["cached"] += 1
                continue
            _upsert_contract(conn, occ, iid, "rh_order")
            cached[occ] = {"occ": occ, "rh_instrument_id": iid, "resolved_via": "rh_order"}
            out["resolved"] += 1
        # 2) everything else through the chain lookup
        need = sorted({r["occ"] for r in _load_refs(conn) if r["occ"] and not r["multileg"]})
        lookups = 0
        stale = _now() - timedelta(days=UNRESOLVED_RETRY_DAYS)
        for occ in need:
            c = cached.get(occ)
            if c and c.get("rh_instrument_id"):
                out["cached"] += 1
                continue
            if c and c.get("resolved_via") == "unresolved" and (_parse_ts(c.get("created_date")) or stale) > stale:
                out["unresolved"] += 1
                continue
            p = parse_occ(occ)
            if date.fromisoformat(p["expiry"]) < RH_RETENTION_EDGE:
                _upsert_contract(conn, occ, None, "unresolved")
                out["unresolved"] += 1
                continue
            if lookups >= MAX_CONTRACT_LOOKUPS:
                out["deferred"] += 1
                continue
            lookups += 1
            iid, complete = _lookup_chain(p)
            if iid:
                _upsert_contract(conn, occ, iid, "chain")
                out["resolved"] += 1
            elif complete:
                _upsert_contract(conn, occ, None, "unresolved")
                out["unresolved"] += 1
            else:
                out["deferred"] += 1          # error/truncation: not a verdict, retried next run
        conn.commit()
    except Exception as e:
        _err("resolve_contracts", e)
    finally:
        if conn is not None:
            conn.close()
    _state["unresolved"] = out["unresolved"]
    out["errors"] = _state["errors"][n_err0:]
    return out


# ── bars ─────────────────────────────────────────────────────────────────────
def parse_rh_bars(result) -> list:
    """One get_option_historicals `results[]` entry → option_bars rows. A missing `interpolated`
    key means False (PROBE bullet 4). Malformed bars are dropped, never stored."""
    out = []
    if not isinstance(result, dict):
        return out
    iid = result.get("instrument_id")
    interval = result.get("interval")
    if not isinstance(iid, str) or not isinstance(interval, str):
        return out
    for b in result.get("bars") or []:
        if not isinstance(b, dict):
            continue
        dt = _parse_ts(b.get("begins_at"))
        o, h, l, c = (_pos(b.get(k)) for k in ("open_price", "high_price", "low_price", "close_price"))
        if dt is None or None in (o, h, l, c) or h < l:
            continue
        out.append({"rh_instrument_id": iid, "ts": int(dt.timestamp()), "interval": interval,
                    "open": o, "high": h, "low": l, "close": c, "volume": None,
                    "interpolated": 1 if b.get("interpolated") is True else 0})
    return out


def _store_bars(conn, bars) -> int:
    now = _iso(_now())
    n = 0
    for b in bars:
        conn.execute(
            "INSERT INTO option_bars (rh_instrument_id, ts, interval, open, high, low, close, volume, "
            "interpolated, created_date) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(rh_instrument_id, interval, ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume, interpolated=excluded.interpolated",
            (b["rh_instrument_id"], b["ts"], b["interval"], b["open"], b["high"], b["low"], b["close"],
             b.get("volume"), b["interpolated"], now))
        n += 1
    return n


def _fetch_bars(conn, iids, interval, start_iso, end_iso) -> tuple:
    rows, meta = _RH.paged_all("get_option_historicals",
                               {"instrument_ids": list(iids), "interval": interval, "start_time": start_iso,
                                "end_time": end_iso, "bounds": "regular"},
                               rows_key="results", max_pages=1, lane="bulk")
    for e in meta.get("errors") or []:
        _err("get_option_historicals", e)
    per = {}
    for res in rows:
        if isinstance(res, dict) and res.get("instrument_id") in iids:
            k = _store_bars(conn, parse_rh_bars(res))
            per[res["instrument_id"]] = per.get(res["instrument_id"], 0) + k
    return sum(per.values()), not meta.get("errors"), per


def _ckpt_get(conn, iid):
    try:
        r = conn.execute("SELECT value FROM desk_state WHERE key=?", (f"rhx_optbars_day_{iid}",)).fetchone()
        return _jload(r["value"], None) if r else None
    except Exception:
        return None


def _ckpt_set(conn, iid, start, end):
    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                 "SET value=excluded.value, updated_date=excluded.updated_date",
                 (f"rhx_optbars_day_{iid}", json.dumps({"start": start, "end": end}), _iso(_now())))


def _contract_ids(conn) -> dict:
    return {r["occ"]: r["rh_instrument_id"] for r in
            conn.execute("SELECT occ, rh_instrument_id FROM option_contracts WHERE rh_instrument_id IS NOT NULL")}


def _windows(refs, ids) -> dict:
    """iid → {start: date, end: date, expired: bool} over every graded ref on that contract."""
    today = _today_et()
    win = {}
    for r in refs:
        if r["multileg"] or not r["occ"] or r["entry_ts"] is None:
            continue
        iid = r.get("rh_instrument_id") or ids.get(r["occ"])
        if not iid:
            continue
        exp = date.fromisoformat(r["expiry"])
        expired = _is_expired(exp)
        start = _et_date(r["entry_ts"])
        end = min(exp, today)
        w = win.setdefault(iid, {"start": start, "end": end, "expired": expired})
        w["start"] = min(w["start"], start)
        w["end"] = max(w["end"], end)
    return win


def backfill_bars(*, live_only=False) -> dict:
    """Fetch day bars for every graded contract (checkpointed once an expired contract is complete)
    and interval_intraday bars for contracts still live."""
    if not enabled():
        return {}
    out = {"calls": 0, "bars": 0, "skipped_checkpoint": 0, "errors": []}
    n_err0 = len(_state["errors"])
    conn = None
    try:
        conn = mason_db.get_connection()
        win = _windows(_load_refs(conn), _contract_ids(conn))
        todo = []
        for iid, w in sorted(win.items(), key=lambda kv: (kv[1]["start"], kv[0])):
            if live_only and w["expired"]:
                continue
            if w["expired"]:
                ck = _ckpt_get(conn, iid)
                if ck and ck.get("start", "9") <= w["start"].isoformat() and ck.get("end", "") >= w["end"].isoformat():
                    out["skipped_checkpoint"] += 1
                    continue
            todo.append((iid, w))
        # day bars, ≤10 instruments per call, chunk window span bounded
        chunk = []
        chunks = []
        for iid, w in todo:
            if chunk and (len(chunk) >= DAY_CHUNK or
                          (max(w["end"], max(x[1]["end"] for x in chunk)) - min(x[1]["start"] for x in chunk)).days
                          > DAY_CHUNK_SPAN_DAYS):
                chunks.append(chunk)
                chunk = []
            chunk.append((iid, w))
        if chunk:
            chunks.append(chunk)
        for ch in chunks:
            if out["calls"] >= MAX_BAR_CALLS:
                _err("backfill_bars: MAX_BAR_CALLS reached; rest deferred to next run")
                break
            s = min(w["start"] for _, w in ch)
            e = max(w["end"] for _, w in ch)
            out["calls"] += 1
            n, ok, per = _fetch_bars(conn, [i for i, _ in ch], "day", f"{s}T00:00:00Z",
                                     f"{e + timedelta(days=1)}T00:00:00Z")
            out["bars"] += n
            if ok:      # checkpoint only an error-free fetch of an expired contract that stored ≥1 bar (D2)
                for iid, w in ch:
                    if w["expired"] and per.get(iid, 0) > 0:
                        _ckpt_set(conn, iid, w["start"].isoformat(), w["end"].isoformat())
            conn.commit()
        # intraday bars for contracts still live (REVIEW_T1 ruling 1.1)
        iv = _intraday()
        floor = _today_et() - timedelta(days=INTRADAY_LOOKBACK_DAYS)
        for iid, w in todo:
            if w["expired"]:
                continue
            if out["calls"] >= MAX_BAR_CALLS:
                break
            out["calls"] += 1
            n, _ok, _per = _fetch_bars(conn, [iid], iv, f"{max(w['start'], floor)}T00:00:00Z", _iso(_now()))
            out["bars"] += n
            conn.commit()
    except Exception as e:
        _err("backfill_bars", e)
    finally:
        if conn is not None:
            conn.close()
    out["errors"] = _state["errors"][n_err0:]
    return out


# ── the path math (pure) ─────────────────────────────────────────────────────
def _bar_date(b, interval) -> date:
    dt = datetime.fromtimestamp(int(b["ts"]), timezone.utc)
    return dt.date() if interval == "day" else dt.astimezone(ET).date()


def excursion_path(entry_ts, entry_px, exit_ts, bars, *, granularity, side="long", stop=None, target=None,
                   entry_full=None) -> dict:
    """MFE/MAE (percent of entry premium, raw — never clamped) from NON-interpolated bars between
    entry and exit.

    Bars strictly between entry and exit contribute open/high/low. The entry bar contributes its
    close only, unless the entry was at/before the 09:30 ET open (entry_full; pass False when the
    entry time is unknown). The exit bar contributes its close only, unless the exit is at/after
    16:00 ET — expiry settlement included — when it contributes open/high/low/close (REVIEW_T4 Q1).
    With stop/target (long only) the walk exits at the first touch — at the bar OPEN when the
    bar gaps through, else at the level; stop is assumed hit before target inside one bar."""
    out = {"mfe_pct": None, "mfe_ts": None, "mae_pct": None, "mae_ts": None, "mfe_sessions": None,
           "bars_used": 0, "interpolated_share": None, "granularity": granularity, "reason": None,
           "exit_px": None, "exit_ts": None, "exit_reason": None, "entry_bar_rule": None, "exit_bar_rule": None}
    try:
        e_px = _pos(entry_px)
        if not e_px:
            out["reason"] = "no_entry_px"
            return out
        if not isinstance(entry_ts, datetime) or not isinstance(exit_ts, datetime):
            out["reason"] = "no_window"
            return out
        if exit_ts < entry_ts:
            out["reason"] = "exit_before_entry"
            return out
        is_day = granularity == "day"
        step = _INTERVAL_S.get(granularity)
        if not is_day and not step:
            out["reason"] = "unknown_granularity"
            return out
        rows = []
        for b in bars or []:
            try:
                rows.append({"ts": int(b["ts"]), "open": float(b["open"]), "high": float(b["high"]),
                             "low": float(b["low"]), "close": float(b["close"]),
                             "interpolated": 1 if b.get("interpolated") else 0})
            except (TypeError, ValueError, KeyError):
                continue
        rows.sort(key=lambda b: b["ts"])
        e_ep, x_ep = entry_ts.timestamp(), exit_ts.timestamp()
        ed, xd = _et_date(entry_ts), _et_date(exit_ts)
        e_et, x_et = entry_ts.astimezone(ET), exit_ts.astimezone(ET)
        if entry_full is None:
            entry_full = (e_et.hour, e_et.minute) <= (9, 30)
        exit_full = (x_et.hour, x_et.minute) >= (16, 0)
        out["entry_bar_rule"] = "full" if entry_full else "close_only"
        out["exit_bar_rule"] = "full" if exit_full else "close_only"
        if is_day:
            win = [b for b in rows if ed <= _bar_date(b, "day") <= xd]

            def role(b):
                d = _bar_date(b, "day")
                return "entry" if d == ed else "exit" if d == xd else "mid"
        else:
            win = [b for b in rows if b["ts"] + step > e_ep and b["ts"] <= x_ep]

            def role(b):
                if b["ts"] <= e_ep < b["ts"] + step:
                    return "entry"
                return "exit" if b["ts"] <= x_ep < b["ts"] + step else "mid"
        if win:
            out["interpolated_share"] = round(sum(b["interpolated"] for b in win) / len(win), 4)
        if is_day and ed == xd and not (entry_full and exit_full):
            out["reason"] = "same_session_day_bars"
            return out
        real = [b for b in win if not b["interpolated"]]
        if not real:
            out["reason"] = "all_interpolated" if win else "no_bars"
            return out
        long_ = side != "short"
        sim = long_ and (stop is not None or target is not None)
        fmt = (lambda b: _bar_date(b, "day").isoformat()) if is_day else (
            lambda b: _iso(datetime.fromtimestamp(b["ts"], timezone.utc)))
        points, used = [], 0

        def _exit(px, b, why):
            out["exit_px"], out["exit_ts"], out["exit_reason"] = px, fmt(b), why

        for b in real:
            used += 1
            r = role(b)
            if (r == "entry" and entry_full) or (r == "exit" and exit_full):
                r = "full_exit" if r == "exit" else "mid"
            if r in ("entry", "exit"):
                points.append((b["close"], b))
                if sim and stop is not None and b["close"] <= stop:
                    _exit(b["close"], b, "stop")
                    break
                if sim and target is not None and b["close"] >= target:
                    _exit(b["close"], b, "target")
                    break
                continue
            if sim:
                if stop is not None and b["open"] <= stop:
                    points.append((b["open"], b))
                    _exit(b["open"], b, "stop_gap")
                    break
                if target is not None and b["open"] >= target:
                    points.append((b["open"], b))
                    _exit(b["open"], b, "target_gap")
                    break
                if stop is not None and b["low"] <= stop:
                    points += [(b["open"], b), (stop, b)]
                    _exit(stop, b, "stop")
                    break
                if target is not None and b["high"] >= target:
                    points += [(b["open"], b), (b["low"], b), (target, b)]
                    _exit(target, b, "target")
                    break
            points += [(b["open"], b), (b["high"], b), (b["low"], b)]
            if r == "full_exit":
                points.append((b["close"], b))
        fav = (lambda px: (px - e_px) / e_px * 100.0) if long_ else (lambda px: (e_px - px) / e_px * 100.0)
        best = max(points, key=lambda p: (fav(p[0]), -p[1]["ts"]))
        worst = min(points, key=lambda p: (fav(p[0]), p[1]["ts"]))
        out.update(mfe_pct=fav(best[0]), mfe_ts=fmt(best[1]), mae_pct=fav(worst[0]), mae_ts=fmt(worst[1]),
                   bars_used=used)
        gran = "day" if is_day else granularity
        mfe_d = _bar_date(best[1], gran)
        out["mfe_sessions"] = len({_bar_date(b, gran) for b in win if ed < _bar_date(b, gran) <= mfe_d})
    except Exception as e:
        out["reason"] = f"error: {type(e).__name__}"
    return out


# ── settle + one trade's excursion ───────────────────────────────────────────
def _alpaca(sym, cache):
    if sym in cache:
        return cache[sym]
    d = None
    try:
        d = (ALPACA_HOOK or _default_alpaca)(sym)
        if isinstance(d, dict) and not d.get("ok"):
            _err(f"alpaca {sym}", RuntimeError(str(d.get("error") or d.get("kind"))))
    except Exception as e:
        _err(f"alpaca {sym}", e)
    cache[sym] = d if isinstance(d, dict) else None
    return cache[sym]


def _underlying_close(ticker, day, cache):
    """→ (raw close | None, basis). The hook may return a float or {"close", "basis"}."""
    key = (ticker, day)
    if key not in cache:
        v, basis = None, None
        try:
            r = (UNDERLYING_CLOSE_HOOK or _default_underlying_close)(ticker, day)
            if isinstance(r, dict):
                v, basis = _pos(r.get("close")), r.get("basis")
            else:
                v, basis = _pos(r), "hook"
        except Exception as e:
            _err(f"underlying close {ticker} {day}", e)
        cache[key] = (v, basis if v is not None else None)
    return cache[key]


def _opexp_px(opexp, ref):
    if not isinstance(opexp, dict):
        return None
    px = _pos(opexp.get("price"))
    if px:
        return px
    try:
        q = abs(float(opexp.get("qty") or ref.get("qty") or 0))
        net = abs(float(opexp.get("net_amount") or 0))
        return round(net / (q * 100.0), 4) if q > 0 else None
    except (TypeError, ValueError):
        return None


def _alpaca_settle(ref, caches) -> dict | None:
    """Alpaca = settlement truth (memory options-journal-accuracy-fix)."""
    if not ref.get("alpaca_symbol"):
        return None
    exp = date.fromisoformat(ref["expiry"])
    d = _alpaca(ref["alpaca_symbol"], caches["alpaca"])
    if not (d and d.get("ok")):
        return None
    kind = d.get("kind")
    if kind == "sold_early":
        fills = []
        for f in d.get("sell_fills") or []:
            if not isinstance(f, dict):
                continue
            try:
                q = float(f.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if q > 0 and _pos(f.get("price")) is not None:
                fills.append((q, _pos(f.get("price")), _parse_ts(f.get("filled_at"))))
        if fills:
            q = sum(f[0] for f in fills)
            px = round(sum(f[0] * f[1] for f in fills) / q, 4)
            ts = max((f[2] for f in fills if f[2]), default=None)
            return {"px": px, "source": "alpaca", "kind": kind, "ts": ts}
    elif kind == "expired_worthless":
        return {"px": 0.0, "source": "alpaca", "kind": kind, "ts": _expiry_close(exp)}
    elif kind == "expired_itm":
        px = _opexp_px(d.get("opexp"), ref)
        if px is not None:
            return {"px": px, "source": "alpaca", "kind": kind, "ts": _expiry_close(exp)}
    return None


def _expiry_bar_close(day_bars, intraday_bars, exp: date):
    for bars, iv in ((day_bars, "day"), (intraday_bars, _intraday())):
        on_exp = [b for b in bars or [] if not b["interpolated"] and _bar_date(b, iv) == exp]
        if on_exp:
            return on_exp[-1]["close"], iv
    return None, None


def _expiry_settle(ref, day_bars, intraday_bars, caches) -> dict | None:
    """Held to expiry, no Alpaca answer (V1, REVIEW_T4): intrinsic off the underlying's RAW official
    close on the expiry session → else the non-interpolated expiry-day bar close."""
    exp = date.fromisoformat(ref["expiry"])
    bar_px, bar_iv = _expiry_bar_close(day_bars, intraday_bars, exp)
    close, basis = _underlying_close(ref["underlying"], exp.isoformat(), caches["close"])
    px = intrinsic(ref["option_type"], ref["strike"], close)
    if px is not None:
        return {"px": px, "source": "intrinsic", "kind": "intrinsic", "ts": _expiry_close(exp),
                "underlying_close": close, "close_basis": basis, "expiry_bar_close": bar_px}
    if bar_px is not None:
        return {"px": bar_px, "source": "option_bars", "kind": f"{bar_iv}_close", "ts": _expiry_close(exp),
                "expiry_bar_close": bar_px}
    return None


def _settle(ref, day_bars, intraday_bars, expired, caches) -> dict | None:
    """settle precedence (V1): Alpaca fill/OPEXP → intrinsic (raw close) → expiry-day bar close."""
    a = _alpaca_settle(ref, caches)
    if a:
        return a
    if not expired:
        return None
    return _expiry_settle(ref, day_bars, intraday_bars, caches)


def _load_bars(conn, iid, interval) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT ts, open, high, low, close, interpolated FROM option_bars WHERE rh_instrument_id=? AND "
        "interval=? ORDER BY ts", (iid, interval))]


def _grade_words(table, realized):
    if realized is None:
        return None
    if table == "recommendations":
        return "right" if realized > 0 else "wrong" if realized < 0 else "neutral"
    return "win" if realized > 0 else "loss" if realized < 0 else "breakeven"


def _dte_bucket(n):
    if n is None:
        return "unknown"
    return "0-5" if n <= 5 else "6-14" if n <= 14 else "15-45" if n <= 45 else "46+"


def _excursion_id(ref) -> str:
    return hashlib.sha1(f"{ref['ref_table']}|{ref['ref_id']}|{ref['occ']}".encode()).hexdigest()[:16]


def _rec_entry(ref, gran, bars):
    """Q6: a rec enters at the first NON-interpolated bar at/after created_date (an after-hours rec
    enters the next session). → (entry_ts, entry_ts_rule, entry_full)."""
    c0 = ref["entry_ts"]
    real = [b for b in bars if not b["interpolated"]]
    if gran == "day":
        c_et = c0.astimezone(ET)
        cd = c_et.date()
        after_close = (c_et.hour, c_et.minute) >= (16, 0)
        before_open = (c_et.hour, c_et.minute) < (9, 30)
        for b in real:
            d = _bar_date(b, "day")
            if d < cd or (d == cd and after_close):
                continue
            if d == cd and not before_open:
                return c0, "created_in_session", None
            return (datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(timezone.utc),
                    "next_session", False)
        return c0, "created_no_bar", None
    step = _INTERVAL_S.get(gran, 300)
    for b in real:
        if b["ts"] + step > c0.timestamp():
            if b["ts"] <= c0.timestamp():
                return c0, "created_in_session", None
            return datetime.fromtimestamp(b["ts"], timezone.utc), "first_real_bar_after_created", False
    return c0, "created_no_bar", None


def _compute_one(conn, ref, iid, caches) -> dict:
    exp = date.fromisoformat(ref["expiry"])
    expired = _is_expired(exp)
    exp_close = _expiry_close(exp)
    iv = _intraday()
    day_bars = _load_bars(conn, iid, "day")
    intra = _load_bars(conn, iid, iv)
    alp = _alpaca_settle(ref, caches)
    exit_px, exit_ts = ref["exit_px"], ref["exit_ts"]
    settle, exit_reason = None, None
    # V1 scope: a position closed before expiry settles at its REAL exit — never the expiry chain
    if exit_px is not None:
        exit_reason = "recorded"
        settle = alp if (alp and alp["kind"] == "sold_early") else {
            "px": exit_px, "source": None, "kind": "recorded_exit", "ts": exit_ts}
    elif alp and alp["kind"] == "sold_early":
        exit_px, exit_ts, exit_reason, settle = alp["px"], alp["ts"] or exp_close, "alpaca_sell", alp
    path_end = exit_ts or (exp_close if expired else min(_now(), exp_close))
    entry_ts, entry_rule, entry_full = ref["entry_ts"], None, None
    if not ref["entry_time_known"]:
        entry_rule, entry_full = "date_only", False
    elif ref["ref_table"] == "recommendations":
        entry_rule = "fill_time" if ref["entry_basis"] == "fill" else None
    else:
        entry_rule = "fill_time"
    # granularity (REVIEW_T1 ruling 1): intraday when its window is ≤50% interpolated, else day
    gran, fallback = "day", None
    if intra and entry_ts:
        step = _INTERVAL_S[iv]
        w = [b for b in intra if b["ts"] + step > entry_ts.timestamp() and b["ts"] <= path_end.timestamp()]
        if w:
            share = sum(1 for b in w if b["interpolated"]) / len(w)
            if share <= INTERP_FALLBACK_SHARE:
                gran = iv
            else:
                fallback = f"{iv}_interpolated>50%"
    bars = intra if gran == iv else day_bars
    if ref["ref_table"] == "recommendations" and entry_ts and entry_rule is None:
        entry_ts, entry_rule, entry_full = _rec_entry(ref, gran, bars)
    path = excursion_path(entry_ts, ref["entry_px"], path_end, bars, granularity=gran, side=ref["side"],
                          stop=ref["stop"], target=ref["target"], entry_full=entry_full)
    if exit_px is None and path["exit_px"] is not None:
        exit_px, exit_ts, exit_reason = path["exit_px"], _parse_ts(path["exit_ts"]), path["exit_reason"]
        settle = {"px": None, "source": None, "kind": "simulated_exit"}
    expiry_bar_close = _expiry_bar_close(day_bars, intra, exp)[0] if expired else None
    if exit_px is None and expired:
        settle = alp or _expiry_settle(ref, day_bars, intra, caches)
        if settle:
            exit_px, exit_ts, exit_reason = settle["px"], exp_close, "expiry_settle"
    is_open = exit_px is None and not expired
    e_px = _pos(ref["entry_px"])
    realized = None
    if e_px and exit_px is not None:
        realized = ((exit_px - e_px) if ref["side"] != "short" else (e_px - exit_px)) / e_px * 100.0
    mfe, mae = path["mfe_pct"], path["mae_pct"]
    left = (mfe - realized) if (mfe is not None and realized is not None) else None
    bar_pnl = None
    if e_px and exit_px is not None:
        bar_pnl = round(((exit_px - e_px) if ref["side"] != "short" else (e_px - exit_px)) * 100.0
                        * float(ref["qty"] or 1), 2)
    dte = (exp - _et_date(entry_ts)).days if entry_ts else None
    settle = settle or {}
    grade = {"granularity": gran, "reason": path["reason"], "fallback": fallback,
             "bar_grade": _grade_words(ref["ref_table"], realized), "bar_pnl": bar_pnl,
             "stored_grade": ref["stored_grade"], "stored_pnl": ref["stored_pnl"], "voided": ref["voided"],
             "src": ref["src"], "account": ref["account"], "event_kind": ref["event_kind"], "side": ref["side"],
             "option_type": ref["option_type"], "dte_at_entry": dte, "mfe_sessions": path["mfe_sessions"],
             "exit_reason": exit_reason, "open": is_open, "settle_kind": settle.get("kind"),
             "underlying_close": settle.get("underlying_close"), "close_basis": settle.get("close_basis"),
             "expiry_bar_close": expiry_bar_close, "ticker": ref["ticker"],
             "entry_basis": ref["entry_basis"], "entry_ts_rule": entry_rule,
             "entry_bar_rule": path.get("entry_bar_rule"), "exit_bar_rule": path.get("exit_bar_rule")}
    if not fallback:
        grade.pop("fallback")
    return {"id": _excursion_id(ref), "ref_table": ref["ref_table"], "ref_id": ref["ref_id"], "occ": ref["occ"],
            "entry_ts": _iso(entry_ts), "entry_px": e_px, "exit_ts": _iso(exit_ts), "exit_px": exit_px,
            "mfe_pct": _r(mfe, 6), "mfe_ts": path["mfe_ts"], "mae_pct": _r(mae, 6), "mae_ts": path["mae_ts"],
            "realized_pct": _r(realized, 6), "left_on_table_pct": _r(left, 6),
            "settle_px": settle.get("px"), "settle_source": settle.get("source"),
            "bars_used": path["bars_used"], "interpolated_share": path["interpolated_share"],
            "grade_json": json.dumps(grade, sort_keys=True)}


_EX_COLS = ("id", "ref_table", "ref_id", "occ", "entry_ts", "entry_px", "exit_ts", "exit_px", "mfe_pct", "mfe_ts",
            "mae_pct", "mae_ts", "realized_pct", "left_on_table_pct", "settle_px", "settle_source", "bars_used",
            "interpolated_share", "grade_json")


def _upsert_excursion(conn, row):
    cols = list(_EX_COLS) + ["created_date", "created_by", "record_deleted"]
    vals = [row[c] for c in _EX_COLS] + [_iso(_now()), CREATED_BY, 0]
    upd = ", ".join(f"{c}=excluded.{c}" for c in _EX_COLS if c != "id")
    conn.execute(f"INSERT INTO trade_excursions ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
                 f"ON CONFLICT(id) DO UPDATE SET {upd}", vals)


def compute_excursions() -> dict:
    """trade_excursions for every single-leg ref with a resolved contract. Idempotent upsert."""
    if not enabled():
        return {}
    out = {"computed": 0, "skipped_multileg": 0, "unresolved": 0, "errors": []}
    n_err0 = len(_state["errors"])
    conn = None
    try:
        conn = mason_db.get_connection()
        ids = _contract_ids(conn)
        caches = {"alpaca": {}, "close": {}}
        for ref in _load_refs(conn):
            if ref["multileg"]:
                out["skipped_multileg"] += 1
                continue
            iid = ref.get("rh_instrument_id") or ids.get(ref["occ"]) if ref["occ"] else None
            if not iid:
                out["unresolved"] += 1
                continue
            try:
                _upsert_excursion(conn, _compute_one(conn, ref, iid, caches))
                out["computed"] += 1
            except Exception as e:
                _err(f"excursion {ref['ref_table']}:{ref['ref_id']}", e)
        conn.commit()
    except Exception as e:
        _err("compute_excursions", e)
    finally:
        if conn is not None:
            conn.close()
    out["errors"] = _state["errors"][n_err0:]
    return out


def _excursion_map(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT * FROM trade_excursions WHERE IFNULL(record_deleted,0)=0"):
        r = dict(r)
        r["g"] = _jload(r.get("grade_json"), {}) or {}
        out[(r["ref_table"], r["ref_id"])] = r
    return out


# ── priors (family `excursion`, through T5's mason_kb_priors only) ───────────
_PRIOR_METRICS = (("mfe_pct", "median MFE {median:+.0f}%"), ("realized_pct", "median realized {median:+.0f}%"),
                  ("left_on_table_pct", "median left on table {median:+.0f}%"),
                  ("mfe_sessions", "median MFE at {median:.1f} sessions"))


def excursion_priors() -> dict:
    """One prior per (src × DTE bucket × catalyst kind × direction × metric). Multi-leg, voided,
    still-open and out-of-scope (Household) trades are excluded; paper/rec/personal never mix."""
    if not enabled():
        return {}
    try:
        upsert = PRIORS_HOOK or _import_priors()
    except Exception as e:
        msg = _err("excursion_priors: mason_kb_priors unavailable", e)
        _state["priors_error"] = msg
        return {"ok": False, "written": 0, "error": msg}
    _state["priors_error"] = None
    out = {"ok": True, "written": 0, "groups": 0, "failed": 0, "retired": 0, "errors": []}
    n_err0 = len(_state["errors"])
    conn = None
    run, retire = None, None
    try:
        begin, retire = BEGIN_HOOK, RETIRE_HOOK
        if begin is None or retire is None:
            d_begin, d_retire = _import_run_hooks()
            begin, retire = begin or d_begin, retire or d_retire
        run = begin("excursion")
    except Exception as e:
        _err("excursion_priors: begin_run", e)
        out["ok"] = False
    try:
        conn = mason_db.get_connection()
        excl = _exclude_accounts()
        exmap = _excursion_map(conn)
        groups = {}
        for ref in _load_refs(conn):
            ex = exmap.get((ref["ref_table"], ref["ref_id"]))
            if not ex or ref["multileg"] or ref["voided"] or not _in_scope(ref, excl):
                continue
            g = ex["g"]
            if g.get("open") or g.get("voided"):
                continue
            key = (ref["src"], _dte_bucket(g.get("dte_at_entry")), ref["event_kind"] or "unknown",
                   f"{ref['side']}_{ref['option_type']}")
            vals = {"mfe_pct": ex["mfe_pct"], "realized_pct": ex["realized_pct"],
                    "left_on_table_pct": ex["left_on_table_pct"], "mfe_sessions": g.get("mfe_sessions")
                    if ex["mfe_pct"] is not None else None}
            groups.setdefault(key, []).append((f"{ref['ref_table']}:{ref['ref_id']}", vals))
        for (src, dte, cat, direction), members in sorted(groups.items()):
            out["groups"] += 1
            for metric, fmt in _PRIOR_METRICS:
                pairs = [(rid, v[metric]) for rid, v in members if v[metric] is not None]
                if not pairs:
                    continue
                words = direction.replace("_", " ") + "s"
                summary_fmt = (f"single-leg {words}, {dte} DTE, catalyst={cat} ({src}): {fmt} (n={{n}})")
                try:
                    upsert("excursion", {"src": src, "dte": dte, "catalyst": cat, "dir": direction,
                                         "metric": metric}, "all", [float(v) for _, v in pairs],
                           summary_fmt=summary_fmt, refs=[rid for rid, _ in pairs])
                    out["written"] += 1
                except Exception as e:
                    out["failed"] += 1
                    _err(f"upsert_prior {src}/{dte}/{cat}/{direction}/{metric}", e)
    except Exception as e:
        _err("excursion_priors", e)
        out["ok"] = False
    finally:
        if conn is not None:
            conn.close()
    out["errors"] = _state["errors"][n_err0:]
    # retire keys with no samples this run — only after a clean, complete run (REVIEW_T5 ruling 2)
    if run and retire and out["ok"] and not out["failed"] and not out["errors"]:
        try:
            out["retired"] = int(retire("excursion", run) or 0)
        except Exception as e:
            _err("excursion_priors: retire_unrefreshed", e)
            out["errors"] = _state["errors"][n_err0:]
    return out


# ── re-grade report (CSV; optional write-back) ───────────────────────────────
CSV_COLUMNS = ("ref_table", "ref_id", "ticker", "occ", "granularity", "stored_grade", "bar_grade", "stored_pnl",
               "bar_pnl", "delta", "settle_source", "settle_px", "expiry_bar_close", "mfe_pct", "mae_pct",
               "realized_pct", "interpolated_share", "action", "written", "why")
CSV_ACTIONS = ("same", "would_write", "would_write_extra_json_only", "report_only", "void_report_only",
               "skipped_multileg")
_WRITABLE_SETTLE = ("alpaca", "intrinsic")       # REVIEW_T4 Q3: only these may rewrite options_trades


def _option_path_grade(ex) -> dict:
    """extra_json.option_path_grade for a recommendation (REVIEW_T4 Q3) — `at` is stamped on write."""
    g = ex.get("g") or {}
    return {"grade": g.get("bar_grade"), "realized_pct": _r(ex.get("realized_pct"), 4),
            "mfe_pct": _r(ex.get("mfe_pct"), 4), "mae_pct": _r(ex.get("mae_pct"), 4),
            "settle_px": ex.get("settle_px"), "settle_source": ex.get("settle_source"),
            "granularity": g.get("granularity"), "excursion_id": ex.get("id")}


def _same_opg(old, new) -> bool:
    return isinstance(old, dict) and {k: v for k, v in old.items() if k != "at"} == new


def _apply_regrade(conn, writes) -> int:
    """options_trades: outcome/actual_pnl with old values in extra_json.regrade_rhx_<date>.
    recommendations: ONLY extra_json.option_path_grade — no other column is ever written."""
    key = f"regrade_rhx_{_today_et():%Y%m%d}"
    written = 0
    try:
        for w in writes:
            table, rid = w["ref_table"], w["ref_id"]
            if table == "options_trades":
                cur = conn.execute("SELECT outcome, actual_pnl, extra_json FROM options_trades WHERE id=? "
                                   "AND IFNULL(is_deleted,0)=0", (rid,)).fetchone()
            elif table == "recommendations":
                cur = conn.execute("SELECT extra_json FROM recommendations WHERE id=? "
                                   "AND IFNULL(record_deleted,0)=0", (rid,)).fetchone()
            else:
                continue
            if not cur:
                continue
            ex = _jload(cur["extra_json"], {}) if cur["extra_json"] else {}
            if not isinstance(ex, dict):
                _err(f"regrade {table}:{rid}: extra_json is not an object — skipped")
                continue
            if table == "recommendations":
                new = w["opg"]
                old = ex.get("option_path_grade")
                if _same_opg(old, new):
                    continue
                if (isinstance(old, dict) and old.get("excursion_id") == new["excursion_id"]
                        and str(old.get("at") or "") >= w["at"]):
                    continue                      # only a NEWER `at` replaces the same excursion's grade
                ex["option_path_grade"] = dict(new, at=w["at"])
                c = conn.execute("UPDATE recommendations SET extra_json=? WHERE id=? AND IFNULL(record_deleted,0)=0",
                                 (json.dumps(ex), rid))
            else:
                if key in ex:
                    continue
                reason = f"rhx S1-T4 bar-based re-grade ({w['why']})"
                ex[key] = {"outcome": {"old": cur["outcome"], "new": w["bar_grade"], "reason": reason},
                           "actual_pnl": {"old": cur["actual_pnl"], "new": w["bar_pnl"], "reason": reason}}
                c = conn.execute("UPDATE options_trades SET outcome=?, actual_pnl=?, extra_json=? WHERE id=? "
                                 "AND IFNULL(is_deleted,0)=0", (w["bar_grade"], w["bar_pnl"], json.dumps(ex), rid))
            written += c.rowcount or 0
            w["written"] = bool(c.rowcount)
        conn.commit()
    except Exception as e:
        conn.rollback()
        for w in writes:
            w.pop("written", None)
        _err("regrade write-back (rolled back)", e)
        return 0
    return written


def regrade_report(*, out_path=None) -> dict:
    """Stored grade vs bar-based grade for every graded option → CSV. With regrade_write=true:
    options_trades rows settled by alpaca/intrinsic (no interpolated data) are rewritten; recs get
    extra_json.option_path_grade only; voided, multi-leg and option_bars-settled rows never."""
    if not enabled():
        return {}
    path = Path(out_path) if out_path else REGRADE_CSV
    res = {"rows": 0, "would_write": 0, "written": 0, "path": str(path), "errors": []}
    n_err0 = len(_state["errors"])
    conn = None
    try:
        conn = mason_db.get_connection()
        excl = _exclude_accounts()
        exmap = _excursion_map(conn)
        at = _iso(_now())
        lines, writes = [], []
        for ref in sorted(_load_refs(conn), key=lambda r: (r["ref_table"], str(r["ref_id"]))):
            if not _in_scope(ref, excl):
                continue
            ex = exmap.get((ref["ref_table"], ref["ref_id"]))
            g = (ex or {}).get("g") or {}
            line = {"ref_table": ref["ref_table"], "ref_id": ref["ref_id"], "ticker": ref["ticker"],
                    "occ": ref["occ"], "granularity": g.get("granularity"), "stored_grade": ref["stored_grade"],
                    "bar_grade": g.get("bar_grade"), "stored_pnl": ref["stored_pnl"], "bar_pnl": g.get("bar_pnl"),
                    "delta": None, "settle_source": (ex or {}).get("settle_source"),
                    "settle_px": (ex or {}).get("settle_px"), "expiry_bar_close": g.get("expiry_bar_close"),
                    "mfe_pct": _r((ex or {}).get("mfe_pct"), 2), "mae_pct": _r((ex or {}).get("mae_pct"), 2),
                    "realized_pct": _r((ex or {}).get("realized_pct"), 2),
                    "interpolated_share": (ex or {}).get("interpolated_share"), "action": "report_only",
                    "written": False, "why": ""}
            if ref["voided"]:
                line["action"] = "void_report_only"
                line["why"] = ("voided in audit_2026_09_28 (never executed); report only, never regraded"
                               + ("; multi-leg" if ref["multileg"] else ""))
            elif ref["multileg"]:
                line["action"] = "skipped_multileg"
                line["why"] = "multi-leg excluded (single-leg only rule)"
            elif ref.get("pnl_in_share_rows"):       # (KB review 2026-11-19) see _pnl_in_share_rows
                line["why"] = ("exercised into shares: its money is counted on the share rows, so its own outcome "
                               "and P&L stay empty on purpose; never regraded")
            elif not ref["occ"]:
                line["why"] = "no contract (strike/expiry/type missing)"
            elif ex is None:
                line["why"] = "contract unresolved at Robinhood"
            elif g.get("open"):
                line["why"] = "position still open; no exit to grade yet"
            elif ref["ref_table"] == "rh_trades":
                line["why"] = "personal Robinhood trade: the verdict stays Nico's"
            elif g.get("bar_grade") is None:
                line["why"] = f"no bar-based exit ({g.get('reason') or 'no settle source'})"
            else:
                desc = (f"exit {ex.get('exit_px')} via {g.get('exit_reason')}; settle "
                        f"{ex.get('settle_source') or g.get('settle_kind') or 'n/a'}; {g.get('granularity')} bars"
                        + (f" ({g.get('reason')})" if g.get("reason") else ""))
                if ref["ref_table"] == "recommendations":
                    opg = _option_path_grade(ex)
                    old = (ref.get("extra") or {}).get("option_path_grade")
                    same = _same_opg(old, opg)
                    line["delta"] = "same" if g["bar_grade"] == ref["stored_grade"] else "differs"
                    line["action"] = "same" if same else "would_write_extra_json_only"
                    line["why"] = desc + "; outcome column is never written (extra_json.option_path_grade only)"
                    if not same:
                        writes.append({"ref_table": "recommendations", "ref_id": ref["ref_id"], "line": line,
                                       "opg": opg, "at": at})
                else:
                    bar_pnl, st_pnl = g.get("bar_pnl"), ref["stored_pnl"]
                    changed = g["bar_grade"] != ref["stored_grade"]
                    try:
                        line["delta"] = round(float(bar_pnl) - float(st_pnl), 2)
                        changed = changed or abs(line["delta"]) > 0.5
                    except (TypeError, ValueError):
                        changed = changed or bar_pnl is not None
                    writable = (ex.get("settle_source") in _WRITABLE_SETTLE
                                and not (ex.get("interpolated_share") or 0))
                    line["why"] = desc
                    if not changed:
                        line["action"] = "same"
                    elif writable:
                        line["action"] = "would_write"
                        writes.append({"ref_table": "options_trades", "ref_id": ref["ref_id"], "line": line,
                                       "bar_grade": g["bar_grade"], "bar_pnl": bar_pnl, "why": desc})
                    else:
                        line["why"] = desc + ("; report only: settle is not alpaca/intrinsic or path had "
                                              "interpolated bars")
            lines.append(line)
        res["would_write"] = len(writes)
        if writes and _cfg().get("regrade_write") is True:
            res["written"] = _apply_regrade(conn, writes)
            for w in writes:
                if w.get("written"):
                    w["line"]["written"] = True
        res["rows"] = len(lines)
        _write_csv(path, lines)
        _state["last_regrade"] = {"at": at, "rows": res["rows"], "would_write": res["would_write"],
                                  "written": res["written"]}
    except Exception as e:
        _err("regrade_report", e)
    finally:
        if conn is not None:
            conn.close()
    res["errors"] = _state["errors"][n_err0:]
    return res


def _write_csv(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".regrade_", suffix=".csv", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for ln in lines:
                w.writerow({k: ("" if ln.get(k) is None else _redact(ln.get(k)) if k == "why" else ln.get(k))
                            for k in CSV_COLUMNS})
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── orchestration, status, jobs ──────────────────────────────────────────────
def backfill(*, live_only=False) -> dict:
    """resolve → bars → excursions → priors. Idempotent and resumable (cached contracts and
    checkpointed expired contracts are skipped; an RH error never advances a checkpoint)."""
    if not enabled():
        return {}
    n_err0 = len(_state["errors"])
    out = {}
    try:
        out["contracts"] = resolve_contracts()
        out["bars"] = backfill_bars(live_only=live_only)
        out["excursions"] = compute_excursions()
        out["priors"] = excursion_priors()
    except Exception as e:
        _err("backfill", e)
    out["errors"] = _state["errors"][n_err0:]
    _state["last_run"] = _iso(_now())
    _state["last_summary"] = {k: {kk: vv for kk, vv in v.items() if kk != "errors"} if isinstance(v, dict) else v
                              for k, v in out.items() if k != "errors"}
    return out


def snapshot_live() -> dict:
    """Forward-looking capture (REVIEW_T1 ruling 1.5): intraday bars for contracts still live, so new
    trades get intraday MFE/MAE before RH's 5-minute bars on them turn interpolated at expiry."""
    return backfill(live_only=True)


def _live_capture() -> dict:
    """Intraday forward capture (REVIEW_T1 ruling 1.5): resolve any new contract, then fetch interval_intraday
    bars for contracts still live, while RH still serves real (non-interpolated) bars for them. Excursions and
    priors are left to the 15:55 / 16:50 runs. Flag off → {}."""
    if not enabled():
        return {}
    return {"contracts": resolve_contracts(), "bars": backfill_bars(live_only=True)}


def _job(attr: str):
    """Scheduler-safe callable for jobs(): looks `attr` up at call time and never raises (S1-T9)."""
    def _run():
        try:
            return globals()[attr]()
        except Exception as e:
            return {"ok": False, "error": _err(f"job {attr}", e)}
    _run.__name__ = _run.__qualname__ = f"job_{attr}"
    return _run


def jobs() -> list:
    """S1 job contract (mason_rh_history, above jobs()). Forward capture (REVIEW_T1 ruling 1.5) every 30 min
    in market hours plus a final 15:55 pass before 0DTE contracts expire: a failed or missed pass loses at
    most 30 min of an expiring contract's real 5-minute bars, which RH serves only interpolated after expiry.
    The 16:50 daily run needs today's rh_trades (rhx_history_daily, 16:40) and writes `excursion` priors with
    personal samples, so it also runs once on deploy."""
    return [{"name": "rhx_option_bars_live_capture", "every_s": 1800, "window_et": ("09:45", "15:45"),
             "fn": _job("_live_capture"), "note": "resolve + 5-minute bars for live contracts only"},
            {"name": "rhx_option_bars_live_snapshot", "at_et": "15:55", "fn": _job("snapshot_live"),
             "note": "final pre-expiry capture, then excursions/priors"},
            {"name": "rhx_option_bars_daily", "at_et": "16:50", "fn": _job("backfill"),
             "after": ["rhx_history_daily"], "run_on_deploy": True}]


def status() -> dict:
    """For /api/rh/status (contracts §6). Never raises; flag off → no DB access; never calls RH.
    rows: per-table counts (all_status sums them)."""
    base = {"name": NAME, "enabled": False, "last_run": _state.get("last_run"), "rows": {},
            "errors": list(_state.get("errors") or [])[-10:], "truncated": _state.get("truncated", 0),
            "rate_hits": 0, "xcheck_mismatch": 0, "unresolved": _state.get("unresolved", 0),
            "priors_error": _state.get("priors_error"), "last_regrade": _state.get("last_regrade"),
            "regrade_write": False}
    try:
        if not enabled():
            return base
        base["enabled"] = True
        base["regrade_write"] = _cfg().get("regrade_write") is True
        try:
            base["rate_hits"] = int(((_RH.status() or {}).get("rate") or {}).get("rate_hits") or 0)
        except Exception:
            pass
        base["last_summary"] = _state.get("last_summary")
        conn = mason_db.get_connection()
        try:
            for t in ("option_contracts", "option_bars", "trade_excursions"):
                base["rows"][t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            base["unresolved"] = conn.execute(
                "SELECT COUNT(*) FROM option_contracts WHERE resolved_via='unresolved'").fetchone()[0]
            base["granularity"] = {str(k or "none"): n for k, n in conn.execute(
                "SELECT CASE WHEN json_valid(grade_json) THEN json_extract(grade_json, '$.granularity') END AS g,"
                " COUNT(*) FROM trade_excursions WHERE IFNULL(record_deleted,0)=0 GROUP BY g LIMIT 50")}
        finally:
            conn.close()
    except Exception as e:
        _err("status", e)
        base["errors"] = list(_state["errors"])[-10:]
    base["errors"] = [_redact(e) for e in base["errors"]]
    return base


# ── HTTP: GET /api/rh/excursions?ref_table=&ticker= (contracts §6) ───────────
def _query_excursions(ref_table=None, ticker=None, limit=500) -> list:
    if ref_table and ref_table not in REF_TABLES:
        raise ValueError(f"ref_table must be one of {', '.join(REF_TABLES)}")
    t = (ticker or "").strip().upper() or None
    excl = _exclude_accounts()
    conn = mason_db.get_connection()
    try:
        sql = "SELECT * FROM trade_excursions WHERE IFNULL(record_deleted,0)=0"
        args = []
        if ref_table:
            sql += " AND ref_table=?"
            args.append(ref_table)
        sql += " ORDER BY entry_ts DESC, id LIMIT ?"
        args.append(MAX_REFS)
        out = []
        for r in conn.execute(sql, args):
            g = _jload(r["grade_json"], {}) or {}
            if g.get("voided"):
                continue
            if g.get("account") and _RH.norm_account_label(str(g["account"])) in excl:     # T-D6 R2: both sides normalized
                continue
            if t and (r["occ"] or "")[:6].strip().upper() != t:
                continue
            out.append({"ref_id": r["ref_id"], "ref_table": r["ref_table"], "occ": r["occ"],
                        "entry_px": r["entry_px"], "exit_px": r["exit_px"], "mfe_pct": _r(r["mfe_pct"], 2),
                        "mfe_ts": r["mfe_ts"], "mae_pct": _r(r["mae_pct"], 2),
                        "realized_pct": _r(r["realized_pct"], 2), "left_on_table_pct": _r(r["left_on_table_pct"], 2),
                        "settle_source": r["settle_source"], "granularity": g.get("granularity")})
            if len(out) >= max(1, min(int(limit), 2000)):
                break
        return out
    finally:
        conn.close()


def excursions(ref_table=None, ticker=None, limit=500) -> list:
    if not enabled():
        return []
    try:
        return _query_excursions(ref_table, ticker, limit)
    except Exception as e:
        _err("excursions", e)
        return []


try:
    from flask import Blueprint, jsonify, request

    bp = Blueprint("rhx_option_bars", __name__)

    @bp.route("/api/rh/excursions", methods=["GET"])
    def _api_excursions():
        if not enabled():
            return jsonify({"enabled": False})
        as_of = _iso(_now())
        try:
            data = _query_excursions(request.args.get("ref_table") or None, request.args.get("ticker") or None,
                                     request.args.get("limit", 500, type=int) or 500)
            return jsonify({"ok": True, "as_of": as_of, "data": data, "stale": False, "errors": []})
        except Exception as e:
            msg = _err("api excursions", e)
            return jsonify({"ok": False, "as_of": as_of, "data": [], "stale": True,
                            "errors": [msg.replace("<", "").replace(">", "")]})
except Exception:                       # flask missing: module still works, route is skipped
    bp = None


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "backfill":
        if "--live" not in argv:
            print("backfill writes mason.db and calls Robinhood — re-run with --live to confirm "
                  "(needs robinhood.enabled + rh_expansion.option_bars.enabled).")
            return 2
        print(json.dumps(backfill(), indent=2, default=str))
        return 0
    if cmd == "regrade":
        print(json.dumps(regrade_report(), indent=2, default=str))
        return 0
    if cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    print("usage: python3 mason_option_bars.py [status | backfill --live | regrade]")
    return 2


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    sys.exit(_main(sys.argv))
