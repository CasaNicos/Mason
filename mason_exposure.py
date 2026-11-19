"""
mason_exposure.py — RH Expansion S2-T6: data for four widgets.

  GET /api/rh/accounts          the tracked Robinhood accounts NOT in personal_scope.exclude_accounts, plus the FAMILY
                                account as ONE display-only row tagged scope "family" (phase 2 A1, RULINGS RP-4/RP-5:
                                equity, cash, cash %, n_positions and the masked last-4 only; every analytics field null)
  GET /api/rh/exposure?account= true exposure: direct + ETF look-through, with coverage_pct. account=<family label>
                                returns the family view: direct equity holdings weighted AT COST, no quote, no look-through
                                (fetched or cached), no dividend / implied-move / earnings. Never in the default / "all" view
  GET /api/rh/earnings?account= earnings strip for held tickers (8 quarters, next date, implied move)
  GET /api/rh/alerts/log?days=  rh_alert_log fires + patterns (informational; levels only proposed)

Spec: docs/robinhood/RH_EXPANSION_S2_SIGNALS_TOOLS.md §2 T6, contracts §6 (shapes) and §2 (boundaries).
Read-only on Robinhood: every RH read goes through mason_robinhood (snapshot/positions/quotes/
option_quotes/paged_all/_call). Nothing here places, changes or mirrors anything at Robinhood, and
nothing here is a trade trigger. Network hooks (tests replace them): the mason_robinhood network hook (RH),
_sec_get (SEC EDGAR, existing Mason SEC User-Agent), _yf_top_holdings / _yf_dividends (yfinance).

Config: rh_expansion.exposure (all false by default). robinhood.enabled is the master switch.
"""

import json
import math
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db
import mason_robinhood as _rh

try:
    from flask import Blueprint, Response, request
except Exception:                                       # flask missing → module still imports
    Blueprint = None

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
ET = ZoneInfo("America/New_York")
MODULE = "mason_exposure"

_DEFAULTS = {
    "enabled": False,
    "etf_source": ["sec_nport", "yfinance_top"],
    "etf_refresh_days": 30,          # monthly refresh
    "idle_cash_pct": 5.0,            # a snapshot day is idle when cash >= 5% of equity
    "idle_history_days": 30,         # idle_since shown until this many days of snapshots exist
    "held_through_days": 14,
    "implied_move_max_days": 31,     # only price straddles for reports inside the calendar window
    "implied_move_ttl_s": 3600,
    "max_lazy_etf_fetch": 3,
    "etf_retry_hours": 6,            # a failed lazy ETF fetch is not retried by requests for this long
    "implied_move_max_per_request": 3,   # uncached straddle lookups per GET; the job warms the rest         # ETFs fetched inline by a request; the rest wait for the job
    "max_nodes": 50,
    "family_accounts": ["Household"],     # legacy label list (display only); in last-4 map mode the role comes from the private map
}
_ETF_INDUSTRY_RE = re.compile(r"investment trusts or mutual funds|exchange traded", re.I)
_MAX_OPTION_PAGES = 5
_EARN_TTL_S = 12 * 3600
_CAL_TTL_S = 6 * 3600
_DIV_TTL_S = 12 * 3600
_SEC_MAP_TTL_S = 24 * 3600

_cfg_cache = {"mtime": None, "data": {}}
_lock = threading.Lock()


def _now() -> datetime:                                 # hook: tests pin the clock
    return datetime.now(timezone.utc)


def _today_et():
    return _now().astimezone(ET).date()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── config ────────────────────────────────────────────────────────────────────
def _config() -> dict:
    """mason.config.json, mtime-cached. A torn read (editor truncate-then-write) never yields an
    empty or partial config: up to 3 quick re-reads, then the last good copy. The mtime only
    advances on a successful parse, so the next call re-reads."""
    for attempt in range(3):
        try:
            mt = Path(CONFIG).stat().st_mtime
            if _cfg_cache["mtime"] != mt:
                data = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("config is not an object")
                _cfg_cache["data"], _cfg_cache["mtime"] = data, mt
            return _cfg_cache["data"] or {}
        except Exception:
            if _cfg_cache["data"]:
                return _cfg_cache["data"]
            time.sleep(0.02 * (attempt + 1))
    return {}


def _cfg() -> dict:
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        for k, v in ((_config().get("rh_expansion") or {}).get("exposure") or {}).items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def _excluded() -> set:
    """personal_scope.exclude_accounts via the shared parser (mason_robinhood, RULINGS #1/#2), failing
    CLOSED: [] = nobody; missing / null → Household; a bare string (even "Roth IRA"), a non-list or any unusable
    entry → the usable labels PLUS Household, with an error logged once per distinct bad value. Never widens scope."""
    try:
        return set(_rh.resolve_exclude_accounts(_config().get("rh_expansion"),
                                                log=lambda m: _err("config", m), source=MODULE))
    except Exception:
        return {"household"}


def enabled() -> bool:
    try:
        return bool(_rh.enabled()) and _cfg().get("enabled") is True
    except Exception:
        return False


NOT_AVAILABLE = "account not available to Mason"


def _excluded_account(account) -> bool:
    """True when the scope argument names an excluded account explicitly (e.g. account=Household)."""
    a = _rh.norm_account_label(account) if isinstance(account, str) else ""
    return bool(a) and a not in ("default", "scope", "all") and a in _excluded()


def _in_scope(account, *, manual=False):
    """Predicate on an account label (both sides normalized by mason_robinhood.norm_account_label).
    None/''/'all' = every account NOT in personal_scope.exclude_accounts (T-D6, RULINGS #10: 'all' no longer
    includes Household); an excluded account named explicitly = nothing; anything else = that one account.
    A blank/missing label is out for RH rows (RULINGS #12); hand-entered rows (manual=True) keep it in scope."""
    a = _rh.norm_account_label(account) if isinstance(account, str) else ""
    ex = _excluded()

    def ok(lab):
        n = _rh.norm_account_label(lab)
        return manual if not n else n not in ex
    if a in ("", "default", "scope", "all"):
        return ok
    if a in ex:
        return lambda lab: False
    return lambda lab: ok(lab) and _rh.norm_account_label(lab) == a


# ── the family account: display-only (phase 2 A1; RULINGS RP-4 / RP-5) ───────────────────────────────────────
# "family" is a DISPLAY role. It never widens scope: the family account stays in personal_scope.exclude_accounts and in
# the role map's exclusion, so every analytics / chat / brain / cross-ref reader keeps dropping it. These helpers only
# decide which already-excluded account the two display endpoints may SHOW, and they read nothing but the cached
# mason_robinhood.accounts() / positions() (the same reads the personal views already make).
def _family_cfg() -> set:
    """Normalized labels of the legacy `family_accounts` list (display tag in legacy mode). A bare string is one label,
    any other non-list value is nothing; never character-wise matching."""
    raw = _cfg().get("family_accounts")
    raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    return {n for n in (_rh.norm_account_label(a) for a in raw if isinstance(a, str)) if n}


def _account_infos() -> dict:
    """{normalized live label: {"label", "masked", "role"}} from mason_robinhood.accounts() (cached; one get_accounts at
    most, the call snapshot() makes anyway). role is the last-4 map role ("personal" / "family") in map mode and None in
    legacy mode. Never raises."""
    out = {}
    try:
        for r in _rh.accounts() or []:
            if not isinstance(r, dict):
                continue
            lab = str(r.get("label") or "")
            n = _rh.norm_account_label(lab)
            if n and n not in out:
                out[n] = {"label": lab, "masked": r.get("masked"), "role": r.get("role")}
    except Exception:
        return {}
    return out


def _family_accounts() -> dict:
    """{normalized label: info} of the accounts the Accounts / Exposure widgets show as FAMILY (display-only).
    Map mode: role "family" from the private last-4 map (A2). Legacy mode: the label is in the legacy `family_accounts`
    list AND is excluded from analytics (an account that is NOT excluded is in scope and keeps the base behaviour, so
    `exclude_accounts: []` still restores everything). An excluded account that is not family stays hidden."""
    infos = _account_infos()
    out, fam_cfg, excl = {}, None, None
    for n, info in infos.items():
        if info["role"] is not None:
            if info["role"] == "family":
                out[n] = info
            continue
        fam_cfg = _family_cfg() if fam_cfg is None else fam_cfg
        excl = _excluded() if excl is None else excl
        if n in fam_cfg and n in excl:
            out[n] = info
    return out


def _family_target(account):
    """The family account named explicitly by `account` (resolved through norm_account_label, so zero-width / fullwidth /
    case variants give the same account), else None. The default / scope / all tokens never name one."""
    a = _rh.norm_account_label(account) if isinstance(account, str) else ""
    if not a or a in ("default", "scope", "all"):
        return None
    return _family_accounts().get(a)


def _family_account_row(label: str, v: dict) -> dict:
    """Accounts row for a family account (RP-5): equity, cash, cash %, n_positions and the masked last-4 only. Every field
    that would need quotes, idle history or dividend data is null; none of that is looked up."""
    equity, cash = _f(v.get("total_value"), 0.0), _f(v.get("cash"), 0.0)
    return {"account": _plain(label), "account_masked": _plain(v.get("masked")), "scope": "family",
            "equity": round(equity, 2), "day_pl": None, "cash": round(cash, 2),
            "cash_pct": _r(cash / equity * 100) if equity > 0 else None,
            "idle_days": None, "idle_since": None, "n_positions": len(v.get("positions") or []),
            "top": None, "dividends": None}


# ── status ────────────────────────────────────────────────────────────────────
_st = {"last_run": None, "errors": [], "notes": [], "truncated": 0, "normalized": 0}
_caches = {"etf_flag": {}, "earn": {}, "cal": None, "imp": {}, "div": {}, "split": {}, "etf_fail": {},
           "sec_mf": None, "sec_names": None}


def _reset():
    """Test helper: forget caches and status counters."""
    with _lock:
        _st.update(last_run=None, errors=[], notes=[], truncated=0, normalized=0)
        _caches.update(etf_flag={}, earn={}, cal=None, imp={}, div={}, split={}, etf_fail={},
                       sec_mf=None, sec_names=None)


def _plain(s):
    """Plain text for JSON: no markup character from RH / SEC / yfinance ever reaches the API (§6)."""
    return re.sub(r"[<>&]", "", str(s)) if s is not None else None


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _ticker(s):
    """Upper-cased ticker if it looks like one, else None (hostile/garbage identifiers are dropped)."""
    t = str(s or "").strip().upper()
    return t if _TICKER_RE.match(t) else None


def _timed(fn, *args, timeout: float = 10.0):
    """Run a blocking third-party call (yfinance) with a hard timeout (contracts §4: ≤ 10 s)."""
    box = {}

    def run():
        try:
            box["v"] = fn(*args)
        except Exception as e:
            box["e"] = e
    th = threading.Thread(target=run, daemon=True, name="mason_exposure_hook")
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', 'hook')} exceeded {timeout:g}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _err(where: str, e, errs: list | None = None) -> str:
    msg = f"{where}: {type(e).__name__}: {e}" if isinstance(e, BaseException) else f"{where}: {e}"
    msg = _plain(msg)[:300]
    with _lock:
        _st["errors"] = (_st["errors"] + [msg])[-20:]
    if errs is not None:
        errs.append(msg)
    return msg


def _note(msg: str):
    with _lock:
        _st["notes"] = (_st["notes"] + [_plain(msg)[:300]])[-50:]


def _f(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _r(x, nd=6):
    """Rounded finite float, else None (NaN/inf never reach the API)."""
    x = _f(x)
    return None if x is None else round(x, nd)


def _finite(o):
    """Final sweep for every route: any non-finite float anywhere becomes null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


_CACHE_MAX = 2000                      # per-ticker caches are cleared if they ever grow past this


def _put(name: str, key, val):
    c = _caches[name]
    if len(c) >= _CACHE_MAX and key not in c:
        c.clear()
    c[key] = val


def _db(fn):
    conn = mason_db.get_connection()
    try:
        return fn(conn)
    finally:
        conn.close()


def _rows(sql: str, args=()) -> list:
    return _db(lambda c: [dict(r) for r in c.execute(sql, args).fetchall()])


def _first_list(data) -> list:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        v = next((v for v in data.values() if isinstance(v, list)), [])
        return [r for r in v if isinstance(r, dict)]
    return []


# ══ 1. Accounts ═══════════════════════════════════════════════════════════════
def _idle(label: str) -> dict:
    """Consecutive most-recent rh_account_snaps days with cash >= idle_cash_pct of equity.
    Snapshots start at go-live (forward only); until idle_history_days of history exist the
    streak start is returned as idle_since, because the count can't see before go-live."""
    cfg = _cfg()
    thr = float(cfg["idle_cash_pct"]) / 100.0
    snaps = _rows("SELECT snap_date, equity, cash FROM rh_account_snaps WHERE account_label=? "
                  "ORDER BY snap_date DESC", (label,))
    n, since = 0, None
    for s in snaps:
        eq, cash = _f(s.get("equity"), 0.0), _f(s.get("cash"), 0.0)
        if eq > 0 and cash >= thr * eq:
            n += 1
            since = s["snap_date"]
        else:
            break
    short = len(snaps) < int(cfg["idle_history_days"])
    return {"idle_days": n, "idle_since": since if (n and short) else None, "history_days": len(snaps)}


def _dividends(ticker: str) -> list:
    """[(ex_date, amount per CURRENT share)] — yfinance amounts are split-adjusted."""
    hit = _caches["div"].get(ticker)
    if hit and time.time() - hit[0] < _DIV_TTL_S:
        return hit[1]
    out = []
    raw = _timed(_yf_dividends, ticker)
    for item in raw if isinstance(raw, (list, tuple)) else []:
        try:
            d, amt = str(item[0])[:10], _f(item[1])
            if amt and amt > 0 and re.match(r"\d{4}-\d{2}-\d{2}$", d):
                out.append((d, amt))
        except Exception:
            continue
    _put("div", ticker, (time.time(), out))
    return out


def _splits(ticker: str) -> tuple:
    """([(split_date, ratio)], ok) e.g. 10.0 for a 10-for-1; cached. ok=False when the yfinance
    fetch failed (cached for 1 h only), so callers can tell "no splits" from "don't know"."""
    hit = _caches["split"].get(ticker)
    if hit and time.time() - hit[0] < (_DIV_TTL_S if hit[2] else 3600):
        return hit[1], hit[2]
    out, ok = [], True
    try:
        raw = _timed(_yf_splits, ticker)
        for item in raw if isinstance(raw, (list, tuple)) else []:
            try:
                d, r = str(item[0])[:10], _f(item[1])
                if r and r > 0 and abs(r - 1.0) > 1e-9 and re.match(r"\d{4}-\d{2}-\d{2}$", d):
                    out.append((d, r))
            except Exception:
                continue
    except Exception as e:
        ok = False
        _err(f"splits {ticker}", e)
    _put("split", ticker, (time.time(), out, ok))
    return out, ok


def _fill_basis(t: str, qty: float, fills: list, errs: list) -> list:
    """rh_orders fills for one ticker as [(date, signed qty on the CURRENT share basis)].

    rh_orders quantities are raw (as filled); current qty and yfinance dividends are post-split.
    Each fill is scaled by the product of split ratios yfinance records after it. No split is ever
    inferred from the ledger: a ledger that doesn't match the holding (transfer-in, ACATS,
    pre-backfill history) keeps raw fills and is noted; received stays an estimate."""
    rows = []
    for f in fills:
        if (f.get("symbol") or "").upper() != t:
            continue
        q = _f(f.get("qty"), 0.0)
        d = str(f.get("filled_at") or "")[:10]
        if not d or q <= 0:
            continue
        rows.append((d, q if (f.get("side") or "").lower() == "buy" else -q))
    if not rows:
        return []
    first = min(d for d, _ in rows)
    all_sp, ok = _splits(t)
    sp = [(d, r) for d, r in all_sp if d > first]
    if sp:
        out = []
        for d, q in rows:
            k = 1.0
            for sd, r in sp:
                if sd > d:
                    k *= r
            out.append((d, q * k))
        return out
    if not ok:
        # split history unknown: no guessing (a transfer-in looks like a split) — raw fills, flagged
        errs.append(f"dividends {t}: split history unavailable — received is a raw-fill estimate")
        return rows
    if abs(sum(q for _, q in rows) - qty) > 1e-6:
        errs.append(f"dividends {t}: ledger ≠ holdings (transfer-in or missing history)")
    return rows


def _account_dividends(label: str, equities: list, errs: list) -> dict:
    """received: estimated (no Robinhood dividend source in the S1 probe) = Σ shares held on each
    ex-date in the trailing year × dividend; shares on the ex-date = current qty minus net
    rh_orders fills after it, on the current (post-split) share basis. projected_12m = current
    shares × trailing-12-month dividend/share."""
    res = {"received": 0.0, "received_is_est": True, "projected_12m": 0.0}
    if not equities:
        return res
    cutoff = (_today_et() - timedelta(days=365)).isoformat()
    try:
        fills = _rows("SELECT symbol, side, qty, filled_at FROM rh_orders WHERE account_label=? AND kind='equity' "
                      "AND COALESCE(record_deleted,0)=0", (label,))
    except Exception as e:
        _err(f"dividends {label} orders", e, errs)
        fills = []
    for p in equities:
        t, qty = p["ticker"], _f(p.get("qty"), 0.0)
        try:
            divs = [(d, a) for d, a in _dividends(t) if d >= cutoff]
            if not divs:
                continue
            basis = _fill_basis(t, qty, fills, errs)
        except Exception as e:
            _err(f"dividends {t}", e, errs)
            continue
        res["projected_12m"] += qty * sum(a for _, a in divs)
        for d, a in divs:
            after = sum(q for fd, q in basis if fd > d)
            res["received"] += max(0.0, qty - after) * a
    res["received"] = round(res["received"], 2)
    res["projected_12m"] = round(res["projected_12m"], 2)
    return res


def accounts_view():
    """→ (rows, errors, stale). One row per tracked RH account (mason_robinhood.accounts() filter:
    deactivated accounts never appear). Excluded accounts and blank labels are dropped before any quote / dividend /
    idle lookup (T-D6, RULINGS #10/#12), EXCEPT the family account, which phase 2 A1 (RP-5) shows as one display-only
    row tagged scope "family": equity, cash, cash %, n_positions and the masked last-4; no quote, idle or dividend
    lookup is made for it and its holdings are never part of the quote request. The personal rows ("Mine") are
    unchanged."""
    if not enabled():
        return [], [], False
    errs = []
    try:
        snap = _rh.snapshot() or {}
    except Exception as e:
        _err("snapshot", e, errs)
        snap = {}
    if not snap:
        errs.append("no Robinhood accounts returned")
        return [], errs, True
    keep = _in_scope(None)
    fam_acc = _family_accounts()
    fam_snap, own = {}, {}
    for k, v in snap.items():
        info = fam_acc.get(_rh.norm_account_label(k))
        if info is not None and v.get("masked") == info.get("masked"):
            fam_snap[k] = v                          # display-only: never enters the quote / idle / dividend path
        elif keep(k):
            own[k] = v
    snap = own
    fam_cfg = _family_cfg()
    infos = _account_infos()
    order = [a.lower() for a in (_rh._cfg().get("accounts") or [])]
    try:
        eq_syms = sorted({p["ticker"] for v in snap.values() for p in v.get("positions") or []
                          if p.get("kind") == "equity" and p.get("ticker")})
        qts = _rh.quotes(eq_syms) if eq_syms else {}
    except Exception as e:
        _err("quotes", e, errs)
        qts = {}
    out = []
    for label, v in snap.items():
        try:
            pos = v.get("positions") or []
            equity = _f(v.get("total_value"), 0.0)
            cash = _f(v.get("cash"), 0.0)
            vals, day_pl = {}, 0.0
            equities = [p for p in pos if p.get("kind") == "equity"]
            for p in equities:
                q = qts.get(p["ticker"]) or {}
                last, prev, qty = _f(q.get("last")), _f(q.get("prev_close")), _f(p.get("qty"), 0.0)
                if last is not None:
                    vals[p["ticker"]] = vals.get(p["ticker"], 0.0) + qty * last
                    if prev is not None:
                        day_pl += qty * (last - prev)
            top = []
            if equity > 0:
                top = [{"ticker": _plain(t), "pct": _r(val / equity * 100)}
                       for t, val in sorted(vals.items(), key=lambda kv: -kv[1])[:3]]
            try:
                idle = _idle(label)
            except Exception as e:
                _err(f"idle {label}", e, errs)
                idle = {"idle_days": None, "idle_since": None}
            n = _rh.norm_account_label(label)
            role = (infos.get(n) or {}).get("role")
            # scope tag of an IN-SCOPE row: the map role when there is one, else the legacy label list (an account that
            # is not excluded but is listed as family keeps its tag, and the base behaviour, under the [] escape hatch)
            is_fam = (role == "family") if role is not None else (n in fam_cfg)
            out.append({"account": _plain(label), "account_masked": _plain(v.get("masked")),
                        "scope": "family" if is_fam else "personal",
                        "equity": round(equity, 2), "day_pl": round(day_pl, 2), "cash": round(cash, 2),
                        "cash_pct": _r(cash / equity * 100) if equity > 0 else None,
                        "idle_days": idle["idle_days"], "idle_since": idle["idle_since"],
                        "n_positions": len(pos), "top": top,
                        "dividends": _account_dividends(label, equities, errs)})
        except Exception as e:
            _err(f"account {label}", e, errs)
    out.sort(key=lambda r: order.index(r["account"].lower()) if r["account"].lower() in order else 99)
    for label, v in fam_snap.items():                # display-only family rows follow the personal ones
        try:
            out.append(_family_account_row(label, v))
        except Exception as e:
            _err(f"account {label}", e, errs)
    return out, errs, bool(errs)


# ══ 2. True exposure ══════════════════════════════════════════════════════════
def _synced_labels() -> set:
    """Account labels whose holdings come from Robinhood: RH labels + label_map keys/values (RH-A1)."""
    rcfg = _rh._cfg()
    lmap = (rcfg.get("personal_positions_sync") or {}).get("label_map") or {}
    labs = set(lmap.keys()) | set(lmap.values()) | set(rcfg.get("accounts") or [])
    out = {str(x).strip().lower() for x in labs if str(x).strip()}
    try:                                            # phase 2 A2 / A7 carry: every label a synced account ever had
        out |= set(_rh.synced_alias_labels())
    except Exception:
        pass
    return out


def _manual_rows_in_scope(account) -> list:
    """Hand-entered personal_positions rows for accounts NOT synced from Robinhood. Rows written
    by the RH cutover (created_by='robinhood_sync') and any row for a synced account are skipped —
    mason_robinhood.positions() already has them, so counting both would double count."""
    synced, keep = _synced_labels(), _in_scope(account, manual=True)
    rows = _rows("SELECT id, account_label, ticker, shares, cost_basis, created_by FROM personal_positions "
                 "WHERE is_deleted=0 AND status='open' ORDER BY account_label, ticker")
    out = []
    for r in rows:
        lab = str(r.get("account_label") or "").strip()
        if ((r.get("created_by") or "") == "robinhood_sync" or lab.lower() in synced
                or _rh.norm_account_label(lab) in synced):
            continue
        if (r.get("ticker") or "").upper() == "CASH" or not keep(lab):
            continue
        out.append(r)
    return out


def _holdings(etfs) -> dict:
    """{etf: (weights{holding: frac}, source, as_of)} from etf_holdings; sec_nport preferred."""
    etfs = sorted({e for e in etfs if e})
    if not etfs:
        return {}
    marks = ",".join("?" * len(etfs))
    rows = _rows(f"SELECT etf, holding, weight, as_of, source FROM etf_holdings WHERE etf IN ({marks})", etfs)
    by = {}
    for r in rows:
        by.setdefault(r["etf"], {}).setdefault(r["source"], []).append(r)
    out = {}
    for etf, srcs in by.items():
        src = "sec_nport" if "sec_nport" in srcs else sorted(srcs)[0]
        rr = srcs[src]
        out[etf] = ({r["holding"]: _f(r["weight"], 0.0) for r in rr}, src,
                    min((r["as_of"] for r in rr if r.get("as_of")), default=None))
    return out


def _looks_etf(tickers, errs) -> set:
    """ETF check via RH fundamentals (industry 'Investment Trusts Or Mutual Funds'); cached."""
    want = [t for t in tickers if t not in _caches["etf_flag"]]
    for i in range(0, len(want), 20):
        chunk = want[i:i + 20]
        try:
            res = _first_list(_rh._call("get_equity_fundamentals", {"symbols": chunk}))
            got = {str(r.get("symbol") or "").upper(): r for r in res}
            for t in chunk:
                r = got.get(t)
                if r is not None:
                    _put("etf_flag", t, bool(_ETF_INDUSTRY_RE.search(str(r.get("industry") or ""))))
        except Exception as e:
            _err("fundamentals", e, errs)
    return {t for t in tickers if _caches["etf_flag"].get(t)}


def _neutral_exposure():
    return {"nodes": [], "coverage_pct": 0.0, "as_of_holdings": None}


FAMILY_NOTE = "weights at cost - family account, no look-through"


def _family_neutral() -> dict:
    return {"nodes": [], "coverage_pct": None, "as_of_holdings": None, "view": "family", "basis": "cost",
            "note": FAMILY_NOTE}


def _family_exposure(info: dict):
    """→ (data, errors, stale): RP-4. The family account's DIRECT equity holdings from the already-synced positions
    (mason_robinhood.positions(), cached), weighted AT COST (qty x avg_cost / sum). No quote (so no quote cache is
    warmed), no ETF look-through (fetched or cached), no fundamentals, dividends, implied move or earnings. The
    result never includes another account's rows: a position must carry the family label AND its masked last-4."""
    errs = []
    try:
        lab_n = _rh.norm_account_label(info.get("label"))
        agg, bad = {}, 0
        for p in _rh.positions() or []:
            if (not isinstance(p, dict) or p.get("kind") != "equity"
                    or _rh.norm_account_label(p.get("account")) != lab_n or p.get("account_masked") != info.get("masked")):
                continue
            t = _ticker(p.get("ticker"))
            if t is None:
                bad += 1
                continue
            qty, cost = _f(p.get("qty"), 0.0), _f(p.get("avg_cost"))
            if qty <= 0:
                continue
            if cost is None or cost <= 0:
                errs.append(f"{t}: no cost basis - not weighted")
                continue
            d = agg.setdefault(t, {"qty": 0.0, "val": 0.0})
            d["qty"] += qty
            d["val"] += qty * cost
        if bad:
            errs.append("dropped holding(s) with an invalid ticker")
        total = sum(d["val"] for d in agg.values())
        data = _family_neutral()
        if total > 0:
            label = _plain(info.get("label"))
            nodes = [{"ticker": t, "kind": "equity", "qty": _r(d["qty"]), "direct_pct": _r(d["val"] / total * 100),
                      "via": [], "total_pct": _r(d["val"] / total * 100), "day_chg_pct": None, "accounts": [label]}
                     for t, d in agg.items()]
            nodes.sort(key=lambda n: (-n["total_pct"], n["ticker"]))
            data["nodes"] = nodes[:int(_cfg()["max_nodes"])]
        return data, [_plain(e)[:300] for e in errs], bool(errs)
    except Exception as e:
        _err("exposure", e, errs)
        return _family_neutral(), errs, True


def exposure(account=None):
    """→ (data, errors, stale). `account` naming the family account returns the display-only family view (RP-4). total_pct = direct + Σ(ETF weight × holding weight), in % of the
    in-scope equity holdings value (options/crypto/cash are not in the base). coverage_pct = share of
    that value whose underlying names are known (direct stocks + the known part of each ETF)."""
    if not enabled():
        return _neutral_exposure(), [], False
    fam = _family_target(account)
    if fam is not None:                              # phase 2 A1 / RP-4: the display-only family view
        return _family_exposure(fam)
    if _excluded_account(account):
        return _neutral_exposure(), [NOT_AVAILABLE], False
    errs, cfg = [], _cfg()
    try:
        keep = _in_scope(account)
        direct = {}                                  # ticker -> {"val", "accounts"}
        legs = []
        for p in _rh.positions() or []:
            if p.get("kind") == "equity" and p.get("ticker") and keep(p.get("account")):
                legs.append((_ticker(p["ticker"]), _plain(p["account"]), _f(p.get("qty"), 0.0),
                             _f(p.get("avg_cost"))))
        try:
            for r in _manual_rows_in_scope(account):
                legs.append((_ticker(r.get("ticker")), _plain(r["account_label"]), _f(r.get("shares"), 0.0),
                             _f(r.get("cost_basis"))))
        except Exception as e:
            _err("personal_positions", e, errs)
        if any(l[0] is None for l in legs):
            errs.append("dropped holding(s) with an invalid ticker")
            legs = [l for l in legs if l[0] is not None]
        if not legs:
            return _neutral_exposure(), errs, bool(errs)
        qts = _rh.quotes(sorted({l[0] for l in legs})) or {}
        for t, acct, qty, cost in legs:
            px = _f((qts.get(t) or {}).get("last"))
            if px is None:
                px = cost
                errs.append(f"{t}: no quote — valued at cost")
            if px is None or qty <= 0:
                continue
            d = direct.setdefault(t, {"val": 0.0, "accounts": set()})
            d["val"] += qty * px
            d["accounts"].add(acct)
        V = sum(d["val"] for d in direct.values())
        if V <= 0:
            return _neutral_exposure(), errs, bool(errs)

        known = _holdings(direct)
        rest = [t for t in direct if t not in known]
        unknown_etfs = _looks_etf(rest, errs)
        # N2: a ticker whose type couldn't be checked (fundamentals down) is NOT assumed to be a
        # single stock — it might be an ETF, so it counts as uncovered and the answer is stale.
        untyped = {t for t in rest if t not in _caches["etf_flag"]}
        # D3: ETFs whose fetch failed recently are not retried by a request (negative cache);
        # the daily job still retries them.
        retry_s = float(cfg["etf_retry_hours"]) * 3600
        now_s = time.time()
        cands = [t for t in unknown_etfs if now_s - _caches["etf_fail"].get(t, 0.0) >= retry_s]
        lazy = sorted(cands, key=lambda t: -direct[t]["val"])[:int(cfg["max_lazy_etf_fetch"])]
        if lazy:
            res = refresh_etf_holdings(lazy, force=True)
            errs.extend(res.get("errors") or [])
            known = _holdings(direct)
            for t in lazy:
                if t not in known:
                    _put("etf_fail", t, now_s)
        uncovered = sorted((unknown_etfs | untyped) - set(known))

        nodes, covered = {}, 0.0
        for t, d in direct.items():
            if t in known:
                w, _, _ = known[t]
                covered += d["val"] * min(1.0, sum(w.values()))
                for h, wt in w.items():
                    n = nodes.setdefault(h, {"direct": 0.0, "via": {}, "accounts": set()})
                    n["via"][t] = n["via"].get(t, 0.0) + d["val"] * wt
                continue
            if t not in uncovered:
                covered += d["val"]
            n = nodes.setdefault(t, {"direct": 0.0, "via": {}, "accounts": set()})
            n["direct"] += d["val"]
            n["accounts"] |= d["accounts"]
        out = []
        for t, n in nodes.items():
            via = sorted(({"etf": e, "pct": v / V * 100} for e, v in n["via"].items()), key=lambda x: -x["pct"])
            total = n["direct"] / V * 100 + sum(x["pct"] for x in via)
            out.append({"ticker": t, "direct_pct": _r(n["direct"] / V * 100),
                        "via": [{"etf": x["etf"], "pct": _r(x["pct"])} for x in via],
                        "total_pct": _r(total), "day_chg_pct": None, "accounts": sorted(n["accounts"])})
        out.sort(key=lambda x: (-x["total_pct"], x["ticker"]))
        out = out[:int(cfg["max_nodes"])]
        need = [n["ticker"] for n in out if n["ticker"] not in qts]
        if need:
            try:
                qts.update(_rh.quotes(need) or {})
            except Exception as e:
                _err("quotes", e, errs)
        for n in out:
            n["day_chg_pct"] = _r((qts.get(n["ticker"]) or {}).get("chg_pct"))
        if uncovered:
            errs.append(f"holdings unknown (ETF not loaded or type unchecked): {', '.join(uncovered)} "
                        "(counted as uncovered)")
        used = [known[t][2] for t in direct if t in known and known[t][2]]
        data = {"nodes": out, "coverage_pct": _r(covered / V * 100), "as_of_holdings": min(used) if used else None}
        return data, [_plain(e)[:300] for e in errs], bool(uncovered)
    except Exception as e:
        _err("exposure", e, errs)
        return _neutral_exposure(), errs, True


# ── ETF constituents: SEC N-PORT primary, yfinance top-10 fallback ────────────
def _sec_get(url: str):
    """GET an SEC URL with Mason's existing SEC User-Agent (mason_edgar._ua). Text or None."""
    import requests
    import mason_edgar
    try:
        r = requests.get(url, headers={"User-Agent": mason_edgar._ua(), "Accept-Encoding": "gzip, deflate"},
                         timeout=10)
        time.sleep(0.12)                             # SEC fair-access politeness
        return r.text if r.ok else None
    except Exception:
        return None


def _yf_top_holdings(etf: str) -> list:
    """[(ticker, weight_fraction)] — yfinance's top holdings (top 10 only)."""
    import yfinance as yf
    import mason_symbols
    df = yf.Ticker(mason_symbols.yf_symbol(etf)).funds_data.top_holdings
    out = []
    for sym, row in df.iterrows():
        out.append((str(sym).upper(), float(row.get("Holding Percent"))))
    return out


def _yf_dividends(ticker: str) -> list:
    """[(ex_date 'YYYY-MM-DD', amount_per_share)] from yfinance (split-adjusted). Called via _timed."""
    import yfinance as yf
    import mason_symbols
    s = yf.Ticker(mason_symbols.yf_symbol(ticker)).dividends
    return [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in s.items()]


def _yf_splits(ticker: str) -> list:
    """[(split_date 'YYYY-MM-DD', ratio)] from yfinance. Called via _timed."""
    import yfinance as yf
    import mason_symbols
    s = yf.Ticker(mason_symbols.yf_symbol(ticker)).splits
    return [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in s.items() if v]


def _sec_mf_map() -> dict:
    hit = _caches["sec_mf"]
    if hit and time.time() - hit[0] < _SEC_MAP_TTL_S:
        return hit[1]
    raw = json.loads(_sec_get("https://www.sec.gov/files/company_tickers_mf.json") or "{}")
    fields = raw.get("fields") or []
    ix = {f: i for i, f in enumerate(fields)}
    out = {}
    for row in raw.get("data") or []:
        try:
            out[str(row[ix["symbol"]]).upper()] = (int(row[ix["cik"]]), str(row[ix["seriesId"]]))
        except Exception:
            continue
    _caches["sec_mf"] = (time.time(), out)
    return out


_NAME_SUFFIX_RE = re.compile(r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|HOLDINGS?|GROUP|CLASS [A-C]|"
                             r"CL [A-C]|THE|SA|NV|AG|ADR)\b")


def _norm_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper())
    return " ".join(_NAME_SUFFIX_RE.sub(" ", s).split())


def _sec_name_map() -> dict:
    hit = _caches["sec_names"]
    if hit and time.time() - hit[0] < _SEC_MAP_TTL_S:
        return hit[1]
    out = {}
    try:
        import mason_edgar
        raw = json.loads(_sec_get("https://www.sec.gov/files/company_tickers.json") or "{}")
        for tk, (_, title) in mason_edgar._parse_tickers_json(raw).items():
            out.setdefault(_norm_name(title), tk)
    except Exception as e:
        _err("sec name map", e)
    _caches["sec_names"] = (time.time(), out)
    return out


def _parse_nport(xml_text: str) -> tuple:
    """NPORT-P primary_doc.xml → ([(ticker, weight_fraction)], as_of). Common equity only;
    a holding with no ticker identifier is mapped by issuer name via SEC company_tickers.json."""
    import xml.etree.ElementTree as ET_
    root = ET_.fromstring(xml_text)

    def local(el):
        return el.tag.rsplit("}", 1)[-1]

    as_of = next((el.text.strip() for el in root.iter() if local(el) == "repPdDate" and el.text), None)
    out, names = [], None
    for sec in (el for el in root.iter() if local(el) == "invstOrSec"):
        kids = {}
        tk = None
        for ch in sec.iter():
            n = local(ch)
            if n == "ticker" and ch.get("value"):
                tk = _ticker(ch.get("value"))
            elif n not in kids and ch.text and ch.text.strip():
                kids[n] = ch.text.strip()
        cat = kids.get("assetCat")
        if cat and cat != "EC":
            continue
        pct = _f(kids.get("pctVal"))
        if pct is None or pct <= 0:
            continue
        if not tk:
            if names is None:
                names = _sec_name_map()
            tk = _ticker(names.get(_norm_name(kids.get("name") or kids.get("title"))))
        if tk:
            out.append((tk, pct / 100.0))
    return out, as_of


def _fetch_nport(etf: str):
    """Latest NPORT-P for the ETF's series via EDGAR → (rows, as_of) or None."""
    hit = _sec_mf_map().get(etf)
    if not hit:
        return None
    cik, series = hit
    atom = _sec_get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
                    f"{series}&type=NPORT-P&dateb=&owner=include&count=5&output=atom") or ""
    m = re.search(r"<accession-number>\s*([\d-]+)\s*</accession-number>", atom)
    if not m:
        return None
    acc = m.group(1).replace("-", "")
    xml_text = _sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml")
    if not xml_text:
        return None
    rows, as_of = _parse_nport(xml_text)
    return (rows, as_of) if rows else None


def _clean_weights(etf: str, rows, source: str) -> dict:
    """Validate, merge duplicates, drop a self-holding (rescale the rest by 1/(1-w_self)), and
    normalize to 100% when the weights sum above 100%. Each correction is logged in status()."""
    w = {}
    for item in rows or []:
        try:
            t, x = _ticker(item[0]), _f(item[1])
        except Exception:
            continue
        if t and x is not None and x > 0:
            w[t] = w.get(t, 0.0) + x
    self_w = w.pop(etf.upper(), 0.0)
    if self_w > 0:
        if self_w >= 1.0:
            _note(f"{etf} {source}: ETF holds itself at {self_w * 100:.1f}% — no usable holdings")
            return {}
        w = {t: x / (1.0 - self_w) for t, x in w.items()}
        _note(f"{etf} {source}: ETF holds itself ({self_w * 100:.2f}%) — dropped, rest rescaled")
    total = sum(w.values())
    if total > 1.0 + 1e-6:
        w = {t: x / total for t, x in w.items()}
        with _lock:
            _st["normalized"] += 1
        _note(f"{etf} {source}: weights summed to {total * 100:.1f}% — normalized to 100%")
    return w


def _store(etf: str, weights: dict, source: str, as_of: str):
    now = _iso(_now())

    def tx(conn):
        conn.execute("DELETE FROM etf_holdings WHERE etf=? AND source=?", (etf, source))
        conn.executemany("INSERT INTO etf_holdings (etf, holding, weight, as_of, source, created_date) "
                         "VALUES (?,?,?,?,?,?)",
                         [(etf, h, round(x, 10), as_of, source, now) for h, x in sorted(weights.items())])
        conn.commit()
    _db(tx)


def refresh_etf_holdings(etfs=None, force: bool = False) -> dict:
    """Refresh etf_holdings for the given ETFs (default: held ETFs, in-scope accounts only). Monthly cadence:
    an ETF refreshed within etf_refresh_days is skipped unless force. Idempotent (rows for an
    (etf, source) are replaced as a set)."""
    res = {"refreshed": [], "fresh": [], "failed": [], "normalized": [], "errors": []}
    if not enabled():
        return res
    cfg = _cfg()
    try:
        if etfs is None:
            keep = _in_scope(None)
            held = sorted({p["ticker"].upper() for p in _rh.positions() or []
                           if p.get("kind") == "equity" and p.get("ticker") and keep(p.get("account"))})
            etfs = sorted(set(_holdings(held)) | _looks_etf(held, res["errors"]))
        cutoff = _iso(_now() - timedelta(days=int(cfg["etf_refresh_days"])))
        for etf in [str(e).upper() for e in etfs if e]:
            try:
                if not force:
                    last = _rows("SELECT MAX(created_date) AS d FROM etf_holdings WHERE etf=?", (etf,))
                    if last and last[0]["d"] and last[0]["d"] >= cutoff:
                        res["fresh"].append(etf)
                        continue
                done = False
                for src in cfg.get("etf_source") or []:
                    try:
                        if src == "sec_nport":
                            got = _fetch_nport(etf)
                            rows, as_of = got if got else ([], None)
                        elif src == "yfinance_top":
                            rows, as_of = _timed(_yf_top_holdings, etf) or [], None
                        else:
                            continue
                    except Exception as e:
                        _err(f"{etf} {src}", e, res["errors"])
                        continue
                    n_before = _st["normalized"]
                    w = _clean_weights(etf, rows, src)
                    if not w:
                        continue
                    if _st["normalized"] > n_before:
                        res["normalized"].append(f"{etf} {src}")
                    _store(etf, w, src, as_of or _today_et().isoformat())
                    res["refreshed"].append(etf)
                    done = True
                    break
                if not done:
                    res["failed"].append(etf)
            except Exception as e:
                _err(f"refresh {etf}", e, res["errors"])
                res["failed"].append(etf)
        with _lock:
            _st["last_run"] = _iso(_now())
    except Exception as e:
        _err("refresh_etf_holdings", e, res["errors"])
    return res


# ══ 3. Earnings strip ═════════════════════════════════════════════════════════
def _held(account) -> dict:
    keep, out = _in_scope(account), {}
    for p in _rh.positions() or []:
        if p.get("kind") in ("equity", "option") and keep(p.get("account")):
            t = _ticker(p.get("underlying") or p.get("ticker"))
            if t:
                out.setdefault(t, set()).add(_plain(p["account"]))
    return out


def _rh_earnings_rows(t: str) -> list:
    """Direct get_earnings_results fetch (S1-T6 hasn't covered the ticker) → earnings_results-shaped rows."""
    hit = _caches["earn"].get(t)
    if hit and time.time() - hit[0] < _EARN_TTL_S:
        return hit[1]
    out = []
    for r in _first_list(_rh._call("get_earnings_results", {"symbol": t})):
        eps, rep = r.get("eps") or {}, r.get("report") or {}
        est, act = _f(eps.get("estimate")), _f(eps.get("actual"))
        y, qn = r.get("year"), r.get("quarter")
        out.append({"fiscal_period": f"{y}Q{qn}" if y and qn else None, "report_date": rep.get("date"),
                    "timing": rep.get("timing"), "eps_est": est, "eps_act": act, "surprise_pct": None})
    _put("earn", t, (time.time(), out))
    return out


def _calendar(errs) -> dict:
    """{ticker: (date, timing)}: the next UNREPORTED earnings per ticker from get_earnings_calendar, 31-day
    window starting ET today (cached 6 h, never across ET midnight). A row whose eps.actual is set has
    already reported only when the row is dated today (ET); earlier rows are dropped anyway and a future row's actual
    is ignored (RULINGS #27 G-E2, #35). A future row that carries an actual AND an older (year, quarter) than another
    candidate of the same symbol is stale and skipped (LIMAF); without an actual, or without a parseable fiscal
    period, a row is never skipped (a late filer's report is real). Pick: earliest date, then timing am < pm < null."""
    today = _today_et()
    ti = today.isoformat()
    hit = _caches["cal"]
    if hit and hit[2] == ti and time.time() - hit[0] < _CAL_TTL_S:
        return hit[1]
    out, cand = {}, {}
    try:
        # live schema {start_date: YYYY-MM-DD, days: ±1..31 incl. start_date, filter}; end_date is REJECTED (G-E1)
        data = _rh._call("get_earnings_calendar", {"start_date": ti, "days": 31})
        for r in _first_list(data):
            t = (r.get("symbol") or "").upper()
            rep = r.get("report") if isinstance(r.get("report"), dict) else {}
            eps = r.get("eps") if isinstance(r.get("eps"), dict) else {}
            d = str(rep.get("date") or r.get("report_date") or r.get("date") or "")[:10]
            act = _f(eps.get("actual")) is not None
            if not t or not d or d < ti or (d == ti and act):
                continue                     # an actual on a FUTURE row is stale prior-quarter data (ruling 35)
            y, q = r.get("year"), r.get("quarter")
            try:
                fq = None if isinstance(y, (bool, float)) or isinstance(q, (bool, float)) else (int(y), int(q))
                fq = fq if fq and 1 <= fq[1] <= 4 else None
            except (TypeError, ValueError):
                fq = None
            cand.setdefault(t, []).append((d, rep.get("timing") or r.get("timing"), fq, act))
        rank = {"am": 0, "pm": 1}
        for t, rows in cand.items():
            newest = max((x[2] for x in rows if x[2]), default=None)
            keep = [x for x in rows if not (x[3] and x[2] and x[2] < newest)] or rows
            d, tm, _, _ = min(keep, key=lambda x: (x[0], rank.get(str(x[1]).lower(), 2) if x[1] else 3,
                                                   str(x[1] or "").lower(), str(x[1] or "")))   # order-independent
            out[t] = (d, tm)
        _caches["cal"] = (time.time(), out, ti)
    except Exception as e:
        _err("earnings calendar", e, errs)
    return out


def _mid(q: dict):
    b, a = _f(q.get("bid_price")), _f(q.get("ask_price"))
    if b is not None and a is not None and a > 0 and b >= 0:
        return (a + b) / 2.0
    return _f(q.get("mark_price")) or _f(q.get("adjusted_mark_price"))


def _implied_move(t: str, spot, report_date: str, errs, lane: str = "live"):
    """ATM straddle mid ÷ spot (%), nearest expiry on/after the report date (else nearest), cached 1 h."""
    ttl = float(_cfg()["implied_move_ttl_s"])
    hit = _caches["imp"].get(t)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = None
    try:
        if spot and spot > 0:
            today = _today_et().isoformat()
            chains = _first_list(_rh._call("get_option_chains", {"underlying_symbol": t}, lane=lane))
            ch = next((c for c in chains if (c.get("symbol") or "").upper() == t), chains[0] if chains else {})
            exps = sorted(str(e)[:10] for e in ch.get("expiration_dates") or [] if str(e)[:10] >= today)
            exp = next((e for e in exps if e >= (report_date or today)), exps[0] if exps else None)
            if exp:
                rows, meta = _rh.paged_all("get_option_instruments",
                                           {"chain_symbol": t, "expiration_dates": exp, "state": "active"},
                                           max_pages=_MAX_OPTION_PAGES, lane=lane)
                if meta.get("truncated"):
                    with _lock:
                        _st["truncated"] += 1
                    errs.append(f"{t}: option instruments truncated at {_MAX_OPTION_PAGES} pages")
                by_strike = {}
                for r in rows:
                    k = _f(r.get("strike_price"))
                    if k is not None and r.get("type") in ("call", "put") and r.get("id"):
                        by_strike.setdefault(k, {})[r["type"]] = r["id"]
                pairs = [(k, v) for k, v in by_strike.items() if "call" in v and "put" in v]
                if pairs:
                    k, ids = min(pairs, key=lambda kv: abs(kv[0] - spot))
                    oq = _rh.option_quotes([ids["call"], ids["put"]]) or {}
                    c, p = _mid(oq.get(ids["call"]) or {}), _mid(oq.get(ids["put"]) or {})
                    if c is not None and p is not None:
                        val = _r((c + p) / spot * 100)
    except Exception as e:
        _err(f"implied move {t}", e, errs)
    _put("imp", t, (time.time(), val))
    return val


def earnings_strip(account=None, *, max_lookups=None):
    """→ (rows, errors, stale). Held tickers (same account scope as exposure).
    A request prices at most implied_move_max_per_request uncached straddles (each is several live
    RH calls); the rest come back null with stale=true until warm_earnings() (a job) fills the cache."""
    if not enabled():
        return [], [], False
    if _excluded_account(account):
        return [], [NOT_AVAILABLE], False
    errs, cfg = [], _cfg()
    budget = int(cfg["implied_move_max_per_request"]) if max_lookups is None else max_lookups
    pending = []
    try:
        held = _held(account)
        if not held:
            return [], errs, bool(errs)
        today = _today_et()
        cal = _calendar(errs)
        try:
            qts = _rh.quotes(sorted(held)) or {}
        except Exception as e:
            _err("quotes", e, errs)
            qts = {}
        out = []
        for t in sorted(held):
            try:
                try:
                    rows = _rows("SELECT fiscal_period, report_date, timing, eps_est, eps_act, surprise_pct "
                                 "FROM earnings_results WHERE ticker=?", (t,))
                except Exception as e:
                    _err(f"earnings_results {t}", e, errs)
                    rows = []
                if not rows:
                    try:
                        rows = _rh_earnings_rows(t)
                    except Exception as e:
                        _err(f"get_earnings_results {t}", e, errs)
                        rows = []
                quarters, upcoming = [], []
                for r in rows:
                    d = str(r.get("report_date") or "")[:10]
                    est, act = _f(r.get("eps_est")), _f(r.get("eps_act"))
                    if act is None:
                        if d >= today.isoformat():
                            upcoming.append((d, r.get("timing")))
                        continue
                    sp = _f(r.get("surprise_pct"))
                    if sp is None and est not in (None, 0):
                        sp = (act - est) / abs(est) * 100
                    quarters.append({"period": _plain(r.get("fiscal_period")), "surprise_pct": _r(sp), "report_date": d})
                quarters.sort(key=lambda x: x["report_date"], reverse=True)
                nxt = cal.get(t) or (min(upcoming) if upcoming else None)
                nd = None
                if nxt:
                    try:
                        nd = (datetime.strptime(nxt[0], "%Y-%m-%d").date() - today).days
                    except ValueError:
                        nd = None
                imp = None
                if nd is not None and 0 <= nd <= int(cfg["implied_move_max_days"]):
                    hit = _caches["imp"].get(t)
                    if hit and time.time() - hit[0] < float(cfg["implied_move_ttl_s"]):
                        imp = hit[1]
                    elif budget is None or budget < 0 or budget > 0:
                        if budget is not None and budget > 0:
                            budget -= 1
                        imp = _implied_move(t, _f((qts.get(t) or {}).get("last")), nxt[0], errs,
                                            lane="bulk" if budget == -1 else "live")
                    else:
                        pending.append(t)
                out.append({"ticker": t, "held_accounts": sorted(held[t]), "quarters": quarters[:8],
                            "next": {"date": _plain(nxt[0]), "timing": _plain(nxt[1])} if nxt else None,
                            "held_through": bool(nd is not None and 0 <= nd <= int(cfg["held_through_days"])),
                            "implied_move_pct": imp})
            except Exception as e:
                _err(f"earnings {t}", e, errs)
        out.sort(key=lambda r: (not r["held_through"], (r["next"] or {}).get("date") or "9999", r["ticker"]))
        if pending:
            errs.append(f"implied move pending (warming): {', '.join(pending)}")
        return out, [_plain(e)[:300] for e in errs], bool(pending)
    except Exception as e:
        _err("earnings_strip", e, errs)
        return [], errs, True


# ══ 4. Alerts log ═════════════════════════════════════════════════════════════
def _is_round(level: float) -> bool:
    if abs(level - round(level)) > 1e-9:
        return False
    return level < 100 or int(round(level)) % 5 == 0


def alerts_log(days=30):
    """→ (data, errors, stale). rh_alert_log fires in the window + per-ticker patterns: typical
    offset of alert levels from the current price, round-number bias. Informational only: a
    proposed level is a suggestion for Nico (auto_set=false); nothing is ever set at Robinhood."""
    neutral = {"fires": [], "patterns": [], "informational": True}
    if not enabled():
        return neutral, [], False
    errs = []
    try:
        try:
            days = max(1, min(3650, int(days)))
        except (TypeError, ValueError):
            days = 30
        cutoff = (_now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = _rows("SELECT id, symbol, condition, level, fired_at, price_at_fire, raw_json FROM rh_alert_log "
                     "WHERE COALESCE(record_deleted,0)=0 AND fired_at >= ? ORDER BY fired_at DESC", (cutoff,))
        fires = []
        for r in rows:
            direction = None
            try:
                direction = ((json.loads(r.get("raw_json") or "{}") or {}).get("trigger") or {}).get("direction")
            except Exception:
                direction = None
            fires.append({"id": _plain(r["id"]), "symbol": _plain(str(r.get("symbol") or "").upper()),
                          "condition": _plain(r.get("condition")), "direction": _plain(direction),
                          "level": _f(r.get("level")), "price_at_fire": _f(r.get("price_at_fire")),
                          "fired_at": _plain(r.get("fired_at"))})
        syms = sorted({f["symbol"] for f in fires if f["symbol"]})
        try:
            qts = _rh.quotes(syms) if syms else {}
        except Exception as e:
            _err("quotes", e, errs)
            qts = {}
        patterns = []
        for s in syms:
            fs = [f for f in fires if f["symbol"] == s]
            levels = sorted({f["level"] for f in fs if f["level"] is not None})
            last = _f((qts.get(s) or {}).get("last"))
            typical = None
            if last and levels:
                typical = _r(statistics.median(abs(l - last) / last * 100 for l in levels))
            rb = _r(sum(1 for l in levels if _is_round(l)) / len(levels)) if levels else None
            proposed = []
            if typical and last:
                lvl = round(last * (1 - typical / 100.0), 2)
                proposed.append({"level": lvl, "basis": f"about {typical:.1f}% below the last price, "
                                                        "a typical offset for this ticker", "auto_set": False})
            n_round = sum(1 for l in levels if _is_round(l))
            patterns.append({"ticker": s, "n_fires": len(fs), "n_levels": len(levels), "typical_offset_pct": typical,
                             "round_number_bias": rb,
                             "note": f"{n_round} of {len(levels)} alert levels are round numbers; "
                                     f"{len(fs)} fires in the last {days} days.",
                             "proposed_levels": proposed})
        patterns.sort(key=lambda p: (-p["n_fires"], p["ticker"]))
        return {"fires": fires, "patterns": patterns, "informational": True}, [_plain(e)[:300] for e in errs], False
    except Exception as e:
        _err("alerts_log", e, errs)
        return neutral, errs, True


# ══ jobs / status ═════════════════════════════════════════════════════════════
def warm_earnings() -> dict:
    """Job: price every held ticker's implied move (no per-request cap) so GETs serve from cache."""
    if not enabled():
        return {"rows": 0}
    rows, errs, _ = earnings_strip("all", max_lookups=-1)
    with _lock:
        _st["last_run"] = _iso(_now())
    return {"rows": len(rows), "errors": errs}


def jobs() -> list:
    """For the S2-T8 integrator. ETF refresh: daily check, each ETF at most monthly.
    Earnings warm: every 30 min so implied moves (1 h cache) are ready before a GET."""
    if not enabled():
        return []
    return [{"name": "rhx_exposure_etf_refresh", "at_et": "06:15", "fn": refresh_etf_holdings},
            {"name": "rhx_exposure_earnings_warm", "every_s": 1800, "fn": warm_earnings}]


def status() -> dict:
    try:
        rows = _rows("SELECT COUNT(*) AS n FROM etf_holdings")[0]["n"]
    except Exception:
        rows = None
    with _lock:
        return {"name": MODULE, "enabled": enabled(), "last_run": _st["last_run"], "rows": rows,
                "errors": list(_st["errors"]), "notes": list(_st["notes"]), "truncated": _st["truncated"],
                "normalized": _st["normalized"], "rate_hits": (_rh._state or {}).get("rate_hits", 0),
                "xcheck_mismatch": 0}


# ══ HTTP (contracts §6) ═══════════════════════════════════════════════════════
def _json(obj):
    """Strict JSON (allow_nan=False) after the non-finite sweep — no route can emit NaN/Infinity."""
    return Response(json.dumps(_finite(obj), allow_nan=False, ensure_ascii=False),
                    mimetype="application/json")


def _respond(fn, *args):
    if not enabled():
        return _json({"enabled": False})
    try:
        data, errs, stale = fn(*args)
        return _json({"ok": True, "as_of": _iso(_now()), "data": data, "stale": bool(stale), "errors": errs})
    except Exception as e:
        return _json({"ok": False, "as_of": _iso(_now()), "data": None, "stale": True,
                      "errors": [_err("route", e)]})


if Blueprint is not None:
    bp = Blueprint("rh_exposure", __name__)

    @bp.route("/api/rh/accounts")
    def _r_accounts():
        return _respond(accounts_view)

    @bp.route("/api/rh/exposure")
    def _r_exposure():
        return _respond(exposure, request.args.get("account") or None)

    @bp.route("/api/rh/earnings")
    def _r_earnings():
        return _respond(earnings_strip, request.args.get("account") or None)

    @bp.route("/api/rh/alerts/log")
    def _r_alerts_log():
        return _respond(alerts_log, request.args.get("days") or 30)
