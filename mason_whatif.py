"""
mason_whatif.py — RH Expansion S2-T4: What-If Replayer.

"What would the shares I sold be worth now?" For every in-scope equity sell in rh_orders,
value the sold shares at +30 / +90 / +180 calendar days and today, plus the dividends Nico
would have collected. Option sells-to-close are valued once, at expiry.

    dollars ≈ (sold_px - px_h) * qty, exactly -ret * sold_px * qty  (ret is fwd_return's TOTAL return,
                                         so dividends are already in it; dividends_missed is info only)
    negative -> selling cost money (the shares were worth more later); positive -> the exit saved
    money. Same convention as Behavior Coach (Q-T3-2, finalization T-WI: cost = negative everywhere).

Rules (docs/robinhood/RH_EXPANSION_S2_SIGNALS_TOOLS.md §2 T4, RH_EXPANSION_CONTRACTS.md):
  - Prices come ONLY from mason_price_cache.fwd_return(..., days=h, entry_px=sold_px) (§4: one
    grading function). px_h = sold_px * (1 + ret), so a split between the sale and the horizon is
    handled by fwd_return's adjusted closes, never by raw exit prices.
  - Options: expiry close from option_bars when S1-T4 has it, else intrinsic from the
    underlying's expiry-date close (mason_price_cache.get_daily). Horizon is "expiry" only.
  - Excluded: DRIP rows, multi-leg rows, crypto (v1, labeled), and the
    rh_expansion.personal_scope.exclude_accounts accounts (Household by default), applied at read time.
  - A horizon that hasn't elapsed is matured=false and left out of every total.
  - Writes exit_replays (contracts §5) idempotently: id = sha1("whatif|" + rh_orders.id)[:16].
  - Flags: robinhood.enabled (master) AND rh_expansion.whatif.enabled. Off -> every public
    function is a cheap no-op; the route answers {"enabled": false}.
  - Read-only toward Robinhood: this module never calls the RH MCP at all.

Public: enabled(), replay(), summary(account=None, horizon=None, limit=10), jobs(), status(), bp.
Hooks tests monkeypatch: _dividend_history(ticker), _today().
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import mason_db

log = logging.getLogger("mason.whatif")
ET = ZoneInfo("America/New_York")
CONFIG = Path(__file__).resolve().parent / "mason.config.json"
MODULE = "mason_whatif"

EQUITY_HORIZONS = ("30", "90", "180", "today")
ALL_HORIZONS = EQUITY_HORIZONS + ("expiry",)
HORIZON_LABELS = {"30": "+30d", "90": "+90d", "180": "+180d", "today": "today", "expiry": "expiry"}
DEFAULT_HORIZON = "today"
OPTION_MULTIPLIER = 100.0
_TODAY_WALKBACK = 7          # "today" = latest close: step back over weekends/holidays, bounded
_MAX_ROWS = 5000             # sells replayed per run (history is a few dozen rows today)
_STALE_AFTER_H = 36
_YF_TIMEOUT_S = 10           # contracts §4: timeouts ≤ 10 s
_NOTE = ("Negative dollars = selling cost money (the shares were worth more later, dividends "
         "included). Positive = the exit saved money. Future horizons are pending and left out "
         "of totals.")
_CRYPTO_NOTE = "Crypto sells are excluded in v1."

_DEFAULTS = {
    "robinhood": {"enabled": False},
    "rh_expansion": {"personal_scope": {"exclude_accounts": ["Household"]},
                     "whatif": {"enabled": False}},
}

_cfg_cache = {"mtime": None, "data": {}}
_state = {"last_run": None, "rows": 0, "errors": [], "truncated": False}

bp = Blueprint("mason_whatif", __name__)


# ── config (read per call, mtime-cached, merged over _DEFAULTS) ───────────────────────────────
def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _get(*path, default=None):
    node = _config()
    for p in path:
        node = node.get(p) if isinstance(node, dict) else None
    if node is None:
        node = _DEFAULTS
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
    return default if node is None else node


def enabled() -> bool:
    try:
        return (_get("robinhood", "enabled") is True
                and _get("rh_expansion", "whatif", "enabled") is True)
    except Exception:
        return False


def _scope() -> tuple:
    """(excluded account labels, error or None). Build-wide rule (REVIEW_T4 F1), fail-closed:
    a non-list value, or ANY unusable entry (not a non-empty string after strip()), gives the
    usable entries plus "Household", and an error. An explicit [] means include every account."""
    v = _get("rh_expansion", "personal_scope", "exclude_accounts")
    if not isinstance(v, list):
        return ["Household"], "personal_scope.exclude_accounts is not a list; excluding Household"
    good = [a.strip() for a in v if isinstance(a, str) and a.strip()]
    if len(good) == len(v):
        return good, None
    if "household" not in {g.lower() for g in good}:
        good.append("Household")
    return good, "personal_scope.exclude_accounts has unusable entries; excluding Household as well"


def _excluded_accounts() -> set:
    """Normalized (mason_robinhood.norm_account_label) — the same rule on both sides (T-D6 R2)."""
    import mason_robinhood as _RH
    ns = [_RH.norm_account_label(a) for a in _scope()[0]]
    try:                                                                     # A2: labels the last-4 role map excludes
        extra = set(_RH.role_excluded_labels())
    except Exception:                                                        # (A2 not landed yet: no map, no labels)
        extra = set()
    return {n for n in ns if n} | ({"household"} if not all(ns) else set()) | extra     # blank-looking entry → fail closed


def _in_scope(label) -> bool:
    import mason_robinhood as _RH
    return _RH.norm_account_label(str(label or "")) not in _excluded_accounts()


# ── hooks (tests monkeypatch these) ───────────────────────────────────────────────────────────
def _today() -> date:
    return datetime.now(ET).date()


def _dividend_history(ticker: str) -> dict:
    """{"dividends": [(ex_date ISO, amount)], "splits": [(date ISO, ratio)]} from yfinance.
    yfinance semantics: dividend amounts are split-adjusted to today's share count."""
    import yfinance as yf          # lazy: free data source, only reached when the flag is on
    import mason_symbols
    df = yf.Ticker(mason_symbols.yf_symbol(ticker)).history(period="max", interval="1d", auto_adjust=False, actions=True,
                                   timeout=_YF_TIMEOUT_S)
    out = {"dividends": [], "splits": []}
    for col, key in (("Dividends", "dividends"), ("Stock Splits", "splits")):
        if df is not None and col in getattr(df, "columns", ()):
            out[key] = [(ix.date().isoformat(), float(v)) for ix, v in df[col].items() if v]
    return out


# ── helpers ───────────────────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(s) -> str:
    """Plain text for JSON: no markup characters ever reach the API (contracts §6)."""
    return re.sub(r"[<>&]", "", str(s)) if s is not None else s


def _err(errors: list, msg: str) -> None:
    msg = _plain(msg)[:300]
    errors.append(msg)
    log.warning(f"[whatif] {msg}")


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _et_date(iso) -> date | None:
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).date()
    except Exception:
        return None


def _pending(label: str, **extra) -> dict:
    return dict({"px": None, "ret": None, "dollars": None, "dividends": None, "exit_date": None,
                 "matured": False, "label": label}, **extra)


def _r2(x):
    return None if x is None else round(x, 2) + 0.0      # + 0.0: -0.0 → 0.0 (a $0 exit is not a cost)


def _cost_sign(x: float) -> float:
    """Q-T3-2: a sale that cost money is NEGATIVE. Rounded to 4 dp; never -0.0."""
    return round(-x, 4) + 0.0


# ── corporate actions (dividends missed) ──────────────────────────────────────────────────────
def _actions(ticker: str, cache: dict, errors: list) -> dict:
    if ticker not in cache:
        try:
            raw = _dividend_history(ticker) or {}
            cache[ticker] = {
                "dividends": [(str(d)[:10], _num(a)) for d, a in raw.get("dividends") or []
                              if _num(a) is not None and _num(a) > 0],
                "splits": [(str(d)[:10], _num(r)) for d, r in raw.get("splits") or []
                           if _num(r) is not None and _num(r) > 0]}
        except Exception as e:
            _err(errors, f"dividends unavailable for {ticker}: {type(e).__name__}: {e}")
            cache[ticker] = {"dividends": [], "splits": [], "failed": True}
    return cache[ticker]


def _dividends_missed(acts: dict, qty: float, sold: date, end_iso: str) -> float:
    """Cash the sold shares would have collected for ex-dates in (sale date, end]. Selling ON the
    ex-date still collects it. Amounts are split-adjusted to today's shares, so the shares sold
    are scaled by every split after the sale: qty * prod(splits after sale) * amount."""
    s = sold.isoformat()
    mult = 1.0
    for d, r in acts["splits"]:
        if d > s:
            mult *= r
    per_share = sum(a for d, a in acts["dividends"] if s < d <= end_iso)
    return qty * mult * per_share


# ── valuation ─────────────────────────────────────────────────────────────────────────────────
def _value_equity(PC, o: dict, acts: dict, today: date) -> dict:
    sym, qty, sold_px, sold = o["_sym"], o["_qty"], o["_px"], o["_sold"]

    def from_fwd(r, label):
        ret = _num((r or {}).get("ret"))
        if not r or r.get("matured") is not True or ret is None:
            return _pending(label)
        px = sold_px * (1.0 + ret)
        exit_iso = str(r.get("exit_date") or "")[:10] or today.isoformat()
        # Director ruling (round 3b): fwd_return is a TOTAL return (dividends reinvested), so
        # dollars = −ret × sold_px × qty (cost = negative, Q-T3-2); dividends_missed is informational
        # only, never added, and stays a positive amount (finalization ruling 17).
        div = _dividends_missed(acts, qty, sold, exit_iso)
        return {"px": round(px, 4), "ret": round(ret, 6), "dividends": round(div, 4),
                "dollars": _cost_sign(ret * sold_px * qty), "exit_date": exit_iso,
                "matured": True, "label": label}

    out = {}
    for h in ("30", "90", "180"):
        if sold + timedelta(days=int(h)) > today:
            out[h] = _pending(HORIZON_LABELS[h])          # no partial-horizon grades
        else:
            out[h] = from_fwd(PC.fwd_return(sym, sold.isoformat(), days=int(h), entry_px=sold_px),
                              HORIZON_LABELS[h])
    span = (today - sold).days
    out["today"] = _pending("today")
    # fwd_return takes days >= 1: a sell made today has no "today" grade yet (pending, not $0)
    for k in range(span, max(span - _TODAY_WALKBACK, 0), -1):
        v = from_fwd(PC.fwd_return(sym, sold.isoformat(), days=k, entry_px=sold_px), "today")
        if v["matured"]:
            out["today"] = v
            break
    return out


def _option_bar_close(inst_id, exp: date):
    """Close of the expiry-date bar (ET), preferring non-interpolated 'day' bars; ts in s or ms.
    Short read-only connection, closed before returning."""
    if not inst_id:
        return None
    lo = int(datetime(exp.year, exp.month, exp.day, tzinfo=ET).timestamp())
    hi = lo + 86400 - 1
    rows = mason_db.with_connection(lambda c: c.execute(
        "SELECT ts, interval, close, interpolated FROM option_bars WHERE rh_instrument_id=? "
        "AND ((ts BETWEEN ? AND ?) OR (ts BETWEEN ? AND ?))",
        (inst_id, lo, hi, lo * 1000, hi * 1000 + 999)).fetchall())
    best = None
    for r in rows:
        c = _num(r["close"])
        if c is None:
            continue
        key = (0 if r["interpolated"] else 1, 1 if r["interval"] == "day" else 0, r["ts"])
        if best is None or key > best[0]:
            best = (key, c)
    return best[1] if best else None


def _close(row):
    return _num((row or {}).get("close"))


def _later_split_factor(PC, und: str, exp: date, acts: dict, today: date):
    """How much the cache's expiry-date close has been scaled down by splits AFTER expiry, so
    close × factor is back on the expiry-date (strike) basis. Works under both conventions:
    for each later split, a split-adjusted series is continuous across the split day while a
    raw series drops by ~ratio; only splits the series already carries are multiplied in.
    None = can't tell (split history unavailable, or no bars around a split) → leave pending."""
    if acts.get("failed"):
        return None
    factor = 1.0
    for d, r in acts["splits"]:
        if d <= exp.isoformat() or d > today.isoformat() or abs(r - 1.0) < 1e-9:
            continue
        sd = date.fromisoformat(d)
        rows = PC.get_daily(und, (sd - timedelta(days=10)).isoformat(),
                            (sd + timedelta(days=6)).isoformat()) or []
        before = [x for x in rows if str(x.get("date"))[:10] < d and (_close(x) or 0) > 0]
        after = [x for x in rows if str(x.get("date"))[:10] >= d and (_close(x) or 0) > 0]
        if not before or not after:
            return None
        jump = math.log(_close(before[-1]) / _close(after[0]))
        if abs(jump - math.log(r)) >= abs(jump):      # continuous → series carries this split
            factor *= r
    return factor


def _value_option(PC, o: dict, acts: dict, today: date) -> dict:
    exp = _et_date(str(o.get("expiry") or "")[:10] + "T12:00:00")
    strike, otype = _num(o.get("strike")), str(o.get("option_type") or "").lower()
    und = o["_sym"]
    contract = (f"{und} {strike:g}{'C' if otype == 'call' else 'P'} {exp.isoformat()}"
                if exp and strike is not None and otype in ("call", "put") else None)
    pend = _pending("expiry", contract=_plain(contract), source=None)
    if exp is None or strike is None or otype not in ("call", "put") or exp > today:
        return {"expiry": pend}
    px, source = _option_bar_close(o.get("rh_instrument_id"), exp), "option_bars"
    if px is None:
        rows = PC.get_daily(und, exp.isoformat(), exp.isoformat()) or []
        row = next((r for r in rows if str(r.get("date"))[:10] == exp.isoformat()), None)
        spot = _close(row)
        if spot is None or spot <= 0:
            return {"expiry": dict(pend, note="no expiry price yet")}
        factor = _later_split_factor(PC, und, exp, acts, today)
        if factor is None:
            return {"expiry": dict(pend, note="split basis unresolved")}
        spot *= factor                                  # back to the expiry-date basis of the strike
        px = max(0.0, spot - strike) if otype == "call" else max(0.0, strike - spot)
        source = "intrinsic"
    qty, sold_px = o["_qty"], o["_px"]
    return {"expiry": {"px": round(px, 4), "ret": round(px / sold_px - 1.0, 6), "dividends": 0.0,
                       "dollars": _cost_sign((px - sold_px) * qty * OPTION_MULTIPLIER),
                       "exit_date": exp.isoformat(), "matured": True, "label": "expiry",
                       "source": source, "contract": _plain(contract)}}


# ── selection ─────────────────────────────────────────────────────────────────────────────────
def _classify(rows, scoped: bool = True) -> tuple:
    """(replayable, excluded counts) from rh_orders sell rows. Scope is applied here, at read time;
    scoped=False gives every eligible sell regardless of personal_scope (for orphan retirement)."""
    keep, excl = [], {"crypto": 0, "drip": 0, "multileg": 0}
    for o in rows:
        if scoped and not _in_scope(o.get("account_label")):
            continue
        kind = str(o.get("kind") or "").lower()
        if kind == "crypto":
            excl["crypto"] += 1
        elif o.get("is_drip"):
            excl["drip"] += 1
        elif o.get("is_multileg"):
            excl["multileg"] += 1
        elif kind == "equity" or (kind == "option" and str(o.get("position_effect") or "").lower() == "close"):
            keep.append(o)
    return keep, excl


def _sell_rows(conn, account=None) -> list:
    sql = ("SELECT * FROM rh_orders WHERE COALESCE(record_deleted,0)=0 AND lower(side)='sell' "
           "AND (state IS NULL OR lower(state)='filled')")
    args = ()
    if account:
        sql += " AND lower(account_label)=?"
        args = (str(account).strip().lower(),)
    return [dict(r) for r in conn.execute(sql + " ORDER BY filled_at, id", args)]


def _prepare(o: dict):
    """Validated copy of an order row, or a reason it can't be replayed."""
    kind = str(o.get("kind") or "").lower()
    sym = (o.get("underlying") if kind == "option" else o.get("symbol")) or o.get("symbol")
    sym = str(sym or "").strip().upper()
    qty, px, sold = _num(o.get("qty")), _num(o.get("avg_price")), _et_date(o.get("filled_at"))
    if not sym or qty is None or qty <= 0 or px is None or px <= 0 or sold is None:
        return None, f"order {o.get('id')} skipped: missing symbol/qty/price/fill time"
    return dict(o, _sym=sym, _qty=qty, _px=px, _sold=sold, _kind=kind), None


# ── replay job ────────────────────────────────────────────────────────────────────────────────
_UPSERT = (
    "INSERT INTO exit_replays (id, rh_order_id, account_label, ticker, kind, sold_at, sold_px, qty, "
    "horizons_json, dividends_missed, computed_date, created_by, record_deleted) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET "
    "rh_order_id=excluded.rh_order_id, account_label=excluded.account_label, ticker=excluded.ticker, "
    "kind=excluded.kind, sold_at=excluded.sold_at, sold_px=excluded.sold_px, qty=excluded.qty, "
    "horizons_json=excluded.horizons_json, dividends_missed=excluded.dividends_missed, "
    "computed_date=excluded.computed_date, created_by=excluded.created_by, record_deleted=0")


def _rid(order_row_id) -> str:
    return hashlib.sha1(f"whatif|{order_row_id}".encode()).hexdigest()[:16]


def _retire_orphans(conn, eligible: set) -> int:
    """Soft-delete stored replays whose source sell no longer qualifies (order soft-deleted,
    cancelled, re-tagged DRIP/multi-leg/crypto, or malformed). Rows that are merely out of
    personal scope stay: scope is a read-time flip (contracts §2.8)."""
    stale = [r["id"] for r in conn.execute("SELECT id FROM exit_replays WHERE COALESCE(record_deleted,0)=0")
             if r["id"] not in eligible]
    for rid in stale:
        conn.execute("UPDATE exit_replays SET record_deleted=1, computed_date=? WHERE id=?", (_now_iso(), rid))
    return len(stale)


def replay() -> dict:
    """Recompute exit_replays for every in-scope sell. Never raises."""
    if not enabled():
        return {"ok": False, "skipped": "disabled", "rows": 0, "errors": [], "truncated": False}
    errors: list = []
    try:
        import mason_price_cache as PC      # lazy: S1-T3; absence must never break import
    except Exception as e:
        _err(errors, f"price cache unavailable: {type(e).__name__}")
        _state.update(errors=errors[-20:])
        return {"ok": False, "rows": 0, "errors": errors, "truncated": False}
    today, now = _today(), _now_iso()
    truncated, excl = False, {}
    conn = None
    try:
        # 1. read (short): the sells, then the connection is closed
        sells = mason_db.with_connection(_sell_rows)
        keep, excl = _classify(sells)
        eligible = {_rid(o["id"]) for o in _classify(sells, scoped=False)[0] if _prepare(o)[0] is not None}
        truncated = len(keep) > _MAX_ROWS
        keep = keep[-_MAX_ROWS:] if truncated else keep        # newest sells win on truncation
        # 2. compute (price cache + yfinance) with no DB transaction open
        acts_cache: dict = {}
        out_rows = []
        for raw in keep:
            o, why = _prepare(raw)
            if o is None:
                _err(errors, why)
                continue
            try:
                acts = _actions(o["_sym"], acts_cache, errors)
                if o["_kind"] == "option":
                    hz = _value_option(PC, o, acts, today)
                    div_today = 0.0
                else:
                    hz = _value_equity(PC, o, acts, today)
                    last = next((hz[h] for h in ("today", "180", "90", "30") if hz[h]["matured"]), None)
                    div_today = last["dividends"] if last else 0.0
                out_rows.append((_rid(o["id"]), o.get("rh_order_id"), o.get("account_label"), o["_sym"],
                                 o["_kind"], o.get("filled_at"), o["_px"], o["_qty"],
                                 json.dumps(hz, sort_keys=True, allow_nan=False), div_today, now, MODULE))
            except Exception as e:
                _err(errors, f"{o['_sym']} ({o.get('id')}): {type(e).__name__}: {e}")
        # 3. write: one short transaction (upserts + orphan retirement)
        conn = mason_db.get_connection()
        for row in out_rows:
            conn.execute(_UPSERT, row)
        retired = _retire_orphans(conn, eligible)
        conn.commit()
        written = len(out_rows)
        if retired:
            log.info(f"[whatif] retired {retired} replay row(s) whose sell no longer qualifies")
    except Exception as e:
        _err(errors, f"replay failed: {type(e).__name__}: {e}")
        _state.update(errors=errors[-20:])
        return {"ok": False, "rows": 0, "errors": errors, "truncated": truncated}
    finally:
        if conn is not None:
            conn.close()
    _state.update(last_run=now, rows=written, errors=errors[-20:], truncated=truncated)
    return {"ok": True, "rows": written, "errors": errors, "truncated": truncated, "excluded": excl}


# ── read side ─────────────────────────────────────────────────────────────────────────────────
def _item(r: dict, h: str, v: dict) -> dict:
    return {"id": r["id"], "account": _plain(r["account_label"]), "ticker": _plain(r["ticker"]),
            "contract": v.get("contract"), "kind": _plain(r["kind"]), "sold_at": _plain(r["sold_at"]),
            "sold_px": r["sold_px"], "qty": r["qty"], "horizon": h, "horizon_label": HORIZON_LABELS[h],
            "px": v.get("px"), "ret": v.get("ret"), "exit_date": v.get("exit_date"),
            "dividends_missed": _r2(v.get("dividends") or 0.0), "dollars": _r2(v.get("dollars")),
            "matured": True}


def _summary(account=None, horizon=None, limit=10):
    """(data, errors, stale, ok). Never raises."""
    errors: list = []
    h = str(horizon).strip().lower() if horizon not in (None, "") else DEFAULT_HORIZON
    if h not in ALL_HORIZONS:
        _err(errors, f"unknown horizon {h!r}; using {DEFAULT_HORIZON!r}")
        h = DEFAULT_HORIZON
    acct = str(account).strip() if account not in (None, "") else ""
    if acct.lower() == "all":
        acct = ""
    excl_accts, scope_err = _scope()
    if scope_err:
        _err(errors, scope_err)
    data = {"horizon": h, "horizons": list(ALL_HORIZONS), "accounts": [],
            "total_dollars": {k: None for k in ALL_HORIZONS},
            "counts": {k: {"matured": 0, "pending": 0} for k in ALL_HORIZONS},
            "by_account": {}, "costly": [], "best": [], "limit": int(limit),
            "excluded": {"accounts": [_plain(a) for a in excl_accts],
                         "crypto": {"n": 0, "note": _CRYPTO_NOTE}, "drip": 0, "multileg": 0},
            "note": _NOTE}
    if acct and not _in_scope(acct):
        _err(errors, f"account {acct!r} is excluded by personal_scope")
        return data, errors, False, True
    conn = None
    try:
        conn = mason_db.get_connection()
        _keep, excl = _classify(_sell_rows(conn, acct or None))
        data["excluded"]["crypto"]["n"] = excl["crypto"]
        data["excluded"]["drip"], data["excluded"]["multileg"] = excl["drip"], excl["multileg"]
        sql, args = "SELECT * FROM exit_replays WHERE COALESCE(record_deleted,0)=0", ()
        if acct:
            sql, args = sql + " AND lower(account_label)=?", (acct.lower(),)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY sold_at, id", args)]
    except Exception as e:
        _err(errors, f"read failed: {type(e).__name__}: {e}")
        return data, errors, True, False
    finally:
        if conn is not None:
            conn.close()

    totals = {k: [0.0, 0] for k in ALL_HORIZONS}
    by_acct: dict = {}
    items, last_computed, bad = [], None, 0
    for r in rows:
        if not _in_scope(r.get("account_label")):
            continue
        try:
            hz = json.loads(r.get("horizons_json") or "{}")
            assert isinstance(hz, dict)
        except Exception:
            bad += 1
            continue
        last_computed = max(last_computed or "", r.get("computed_date") or "") or None
        a = _plain(r.get("account_label"))
        acc = by_acct.setdefault(a, {k: [0.0, 0] for k in ALL_HORIZONS})
        for k in ALL_HORIZONS:
            v = hz.get(k)
            if not isinstance(v, dict):
                continue
            d = _num(v.get("dollars"))
            if v.get("matured") is True and d is not None:
                data["counts"][k]["matured"] += 1
                totals[k][0] += d
                totals[k][1] += 1
                acc[k][0] += d
                acc[k][1] += 1
                if k == h:
                    items.append(_item(r, k, v))
            else:
                data["counts"][k]["pending"] += 1
    if bad:
        _err(errors, f"{bad} stored replay row(s) unreadable; re-run the replay")
    data["total_dollars"] = {k: (_r2(s) if n else None) for k, (s, n) in totals.items()}
    data["by_account"] = {a: {k: (_r2(s) if n else None) for k, (s, n) in v.items()}
                          for a, v in by_acct.items()}
    data["accounts"] = list(by_acct)
    # Q-T3-2: costly = cost (negative), most negative first; best = saved (positive), largest first
    data["costly"] = sorted((i for i in items if i["dollars"] < 0), key=lambda i: i["dollars"])[:limit]
    data["best"] = sorted((i for i in items if i["dollars"] > 0), key=lambda i: -i["dollars"])[:limit]
    ref = last_computed or _state.get("last_run")
    stale = True
    if ref:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(ref.replace("Z", "+00:00"))
            stale = age > timedelta(hours=_STALE_AFTER_H)
        except Exception:
            stale = True
    return data, errors, stale, True


def summary(account=None, horizon=None, limit=10) -> dict:
    """API `data` (contracts §6) plus "errors". {} when the module is off."""
    if not enabled():
        return {}
    try:
        data, errors, _stale, _ok = _summary(account, horizon, limit)
        return dict(data, errors=errors)
    except Exception as e:
        log.warning(f"[whatif] summary failed: {e}")
        return {"costly": [], "best": [], "errors": [_plain(f"summary failed: {type(e).__name__}")]}


@bp.route("/api/rh/whatif")
def api_whatif():
    if not enabled():
        return jsonify({"enabled": False})
    try:
        data, errors, stale, ok = _summary(request.args.get("account"), request.args.get("horizon"))
    except Exception as e:
        data, errors, stale, ok = {"costly": [], "best": []}, [_plain(f"{type(e).__name__}")], True, False
    return jsonify({"ok": ok, "as_of": _now_iso(), "data": data, "stale": stale, "errors": errors})


# ── integrator hooks ──────────────────────────────────────────────────────────────────────────
def jobs() -> list:
    """For S2-T8 to schedule: one daily replay after the close. [] when off."""
    if not enabled():
        return []
    return [{"name": "rhx_whatif_daily", "at_et": "17:15", "fn": replay}]


def status() -> dict:
    return {"name": MODULE, "enabled": enabled(), "last_run": _state.get("last_run"),
            "rows": _state.get("rows", 0),
            "errors": list(_state.get("errors") or []) + [e for e in [_scope()[1]] if e],
            "truncated": bool(_state.get("truncated")), "rate_hits": 0, "xcheck_mismatch": 0}
