"""
mason_pead.py — earnings surprises × post-earnings drift (RH Expansion S1-T6,
docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §2 T6; contracts §5 earnings_results, kb_priors family `pead`).

sweep(symbols=None, *, force=False)            RH → earnings_results (+ the sector cache)
  Universe (universe()): rh_expansion.pead.universe ⊆ {sp500, ndx100, holdings, watchlist}.
    sp500 / ndx100 come from the EXISTING Mason constituents source, mason_triage.broad_universe(
    allow_refresh=False) (Wikipedia S&P 500 + Nasdaq-100, refreshed weekly by triage; this module never
    triggers that network refresh — same reading as mason_congress._sp500). Every successful read is
    snapshotted to desk_state 'pead:constituents'; when the source is empty/unavailable the snapshot is used.
    holdings = mason_robinhood.in_scope_position_symbols() (only while robinhood.enabled; Nico's in-scope
    accounts — Household / exclude_accounts left out, T-D6 RULINGS #11). watchlist = mason_watchlist active.
    Hooks for tests: _constituents(), _holdings(), _watchlist().
  Per symbol: mason_robinhood._call("get_earnings_results", {"symbol": S}, lane="bulk") — one symbol per
    call, no pagination (PROBE bullet 6: 7 reported quarters + the next upcoming one; ETFs → []). The bulk
    lane's limiter enforces the T1-measured safe rate (rate.bulk_per_s = 2 → ≈250 s per 500 symbols).
    Rows upsert on (ticker, fiscal_period = "YYYY-Qn"); created_date is kept, so a re-sweep is
    byte-identical. The upcoming row is stored (eps_act NULL) and is NEVER graded.
  Checkpoint (desk_state 'pead:fetched' = {T: YYYY-MM-DD of the last good fetch}): a symbol is due when
    never fetched, fetched > REFETCH_DAYS ago, or a stored row without an actual has report_date ≤ today.
    A symbol whose call errors or returns a malformed payload is NOT stamped (retried next sweep) and the
    error shows in status(). At most MAX_SYMBOLS symbols per sweep (book first); the rest is counted as
    `truncated` and reported in status().
  Sector: get_equity_fundamentals (≤ FUND_CHUNK symbols per call, bulk lane) → desk_state 'pead:sector',
    refreshed after SECTOR_TTL_DAYS. RH's sector taxonomy (e.g. "Electronic Technology").

run() = compute() + priors.   compute() never calls RH (works on stored rows while the master switch is off).
  Events: every stored row with an actual EPS and report_date < today ET, after collapsing same-date rows
  to one per (ticker, report_date) (_dedupe, T6-a1: the row with an actual wins, else the later fiscal
  period) — the collapsed set also feeds the quintile history and the next-report check.
  Entry (the first close after the report) — the exact mapping to mason_price_cache.fwd_return:
      timing "am" (before the open on D)  → fwd_return(T, D, entry_rule="on_or_after")  → close of D
      timing "pm" (after the close on D)  → fwd_return(T, D, entry_rule="after")        → close of D+1 session
      timing missing/other                → not graded, unbucketed "unknown_timing"
  Horizons 1/5/20/60 sessions, ALWAYS through fwd_return (contracts §4; never the price table). Sample =
  `xs` (excess return vs SPY over the same sessions). Point-in-time: a horizon is used only when
  fwd_return says matured=True (which implies report_date ≤ today − horizon in real sessions).
  Outcome per (report, horizon):
      sample        matured, ret and xs present, window clean
      withheld      fwd_return returned an error (incomplete_data / entry_gap / data_gap / …), a malformed
                    result, or no SPY leg — never a sample; passed to upsert_prior(withheld=)
      pending       not matured yet, no error (the horizon has not elapsed) — not used, not withheld
      contaminated  the SAME ticker's next report date falls inside (report, exit] — the window holds the
                    next announcement. Dropped (counted). This also makes one ticker's windows
                    non-overlapping at every horizon (60s is the one where it bites).
  Day-1 gap = open of the entry session / close of the session before it − 1 (both from the same cached
  row source via get_daily); known at the entry close, so no look-ahead. Direction up / down / flat (= 0).
  Surprise = (actual − estimate) / |estimate| × 100; estimate 0/missing → unbucketed "no_surprise".
  Quintile — NO LOOK-AHEAD: breakpoints for a report on D use only the surprises of the SAME sector with
    report_date in [D − LOOKBACK_DAYS, D) (strictly before D: same-day peers are excluded). Midrank
    percentile p = (#below + ½·#equal) / n → quintile 1 + ⌊5p⌋ (capped at 5; Q1 = biggest misses,
    Q5 = biggest beats). Fewer than MIN_HISTORY prior same-sector reports → unbucketed "thin_history".
  Buckets (kb_priors family "pead", window "<h>s"):
      {"metric": "xs", "sector": S, "quintile": q, "gap": g}                 per-sector buckets
      {"metric": "xs_all_sectors", "sector": "all", "quintile": q, "gap": g} pooled across sectors
    The two metrics are separate T5 shrink pools (no double counting). Buckets with samples or withheld
    rows are written; each recompute is bracketed by begin_run("pead") … retire_unrefreshed("pead", run)
    and the retire runs only on an error-free pass. Pool sizes are measured and reported (status()).

SWITCHES: rh_expansion.pead.enabled is this module's flag — off → every public function is a cheap no-op
(no hooks, no DB). robinhood.enabled (master switch) gates sweep(); rh_expansion.priors.enabled gates the
prior writes (compute still runs). rh_expansion.price_cache.enabled must be on for grading.
Public functions never raise. No HTTP routes. status() for /api/rh/status; jobs() for T9.

CLI:  python3 mason_pead.py sweep [--live] | run | report [--out PATH] [--note TEXT] | status
      (sweep without --live is a dry listing with no RH call; --live refuses unless pead + robinhood are on;
       run/report refuse unless pead + price_cache are on)
"""

import json
import logging
import math
import re
import sys
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db

log = logging.getLogger("mason.pead")

NAME = "pead"
FAMILY = "pead"
CREATED_BY = "mason_pead"
CONFIG = Path(__file__).resolve().parent / "mason.config.json"
_ET = ZoneInfo("America/New_York")
_DEFAULTS = {"enabled": False, "universe": ["sp500", "ndx100", "holdings", "watchlist"]}
_cfg_cache = {"mtime": None, "data": None}

HORIZONS = (1, 5, 20, 60)
MIN_HISTORY = 20          # prior same-sector reports needed before a quintile is assigned
LOOKBACK_DAYS = 730       # breakpoint history window (calendar days before D)
MAX_SYMBOLS = 1500        # per sweep (S&P 500 ∪ NDX ∪ book ≈ 530)
MAX_REPORTS = 50000       # rows read per compute
FUND_CHUNK = 10
SECTOR_TTL_DAYS = 90
REFETCH_DAYS = 30
GAP_MAX_LOOKBACK = 10     # calendar days between the previous close and the entry session
POOL_WARN = 2000          # T5 re-shrink is O(n²) per pool; above this OPEN QUESTIONS (REVIEW_T5 Q4)
_STAMP_EVERY = 50
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DS_CONST, _DS_FETCHED, _DS_SECTOR = "pead:constituents", "pead:fetched", "pead:sector"

SUMMARY_FMT = ("PEAD {sector} surprise Q{quintile}/5, day-1 gap {gap}: {window} excess vs SPY mean {mean:+.2%}, "
               "CI95 {ci95_lo:+.2%} to {ci95_hi:+.2%}, hit {hit_rate:.0%}, shrunk {shrunk_mean:+.2%} (n={n})")

_state = {}


def _reset_state() -> None:
    _state.clear()
    _state.update({"last_run": None, "last_sweep_at": None, "errors": [], "truncated": 0,
                   "last_sweep": {}, "last_summary": {}})


_reset_state()

_ACCT_RE = re.compile(r"(?<![\w.])\d{8,}(?![\w.])")


def _redact(s) -> str:
    return _ACCT_RE.sub(lambda m: "••" + m.group(0)[-4:], str(s))


def _err(where, e=None) -> str:
    msg = _redact(f"{where}: {type(e).__name__}: {e}" if e is not None else str(where))[:300]
    _state["errors"] = (_state["errors"] + [msg])[-50:]
    log.warning(f"[pead] {msg}")
    return msg


# ── config / hooks ───────────────────────────────────────────────────────────
def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _rhx() -> dict:
    try:
        return _config().get("rh_expansion") or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        for k, v in (_rhx().get("pead") or {}).items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return _cfg().get("enabled") is True
    except Exception:
        return False


def _sub_on(block) -> bool:
    try:
        return (_rhx().get(block) or {}).get("enabled") is True
    except Exception:
        return False


def _rh_on() -> bool:
    try:
        import mason_robinhood
        return bool(mason_robinhood.enabled())
    except Exception:
        return False


def _sources() -> set:
    v = _cfg().get("universe")
    if not isinstance(v, list):
        v = _DEFAULTS["universe"]
    return {str(x).strip().lower() for x in v if isinstance(x, str)}


def _today_et() -> date:
    return datetime.now(_ET).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pc():
    import mason_price_cache
    return mason_price_cache


def _kbp():
    import mason_kb_priors
    return mason_kb_priors


def _constituents() -> dict:
    """Existing Mason constituents source (cache only — never triggers triage's Wikipedia refresh)."""
    import mason_triage
    return mason_triage.broad_universe(allow_refresh=False) or {}


def _holdings() -> set:
    import mason_robinhood
    return set(mason_robinhood.in_scope_position_symbols())


def _watchlist() -> set:
    import mason_watchlist
    return {it.get("ticker") for it in mason_watchlist.list_items("active") if it.get("ticker")}


def _norm_ticker(t):
    if not isinstance(t, str):
        return None
    t = t.strip().upper()
    return t if _TICKER_RE.match(t) else None


# ── desk_state ───────────────────────────────────────────────────────────────
def _ds_get(key: str, default):
    def f(c):
        return c.execute("SELECT value FROM desk_state WHERE key=?", (key,)).fetchone()
    r = mason_db.with_connection(f)
    if not r or not r[0]:
        return default
    try:
        v = json.loads(r[0])
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def _ds_set(key: str, value) -> None:
    def f(c):
        c.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                  "SET value=excluded.value, updated_date=excluded.updated_date",
                  (key, json.dumps(value, sort_keys=True), _now_iso()))
        c.commit()
    mason_db.with_connection(f)


# ── universe ─────────────────────────────────────────────────────────────────
def _index_sets() -> dict:
    out = {"sp500": set(), "ndx100": set()}
    try:
        bu = _constituents() or {}
        for k, v in (bu.items() if isinstance(bu, dict) else []):
            t = _norm_ticker(k)
            idx = (v or {}).get("index") if isinstance(v, dict) else None
            if not t or not isinstance(idx, list):
                continue
            if "SP500" in idx:
                out["sp500"].add(t)
            if "NDX" in idx:
                out["ndx100"].add(t)
    except Exception as e:
        _err("constituents source", e)
    try:
        if out["sp500"] or out["ndx100"]:
            _ds_set(_DS_CONST, {"as_of": _today_et().isoformat(), "sp500": sorted(out["sp500"]),
                                "ndx100": sorted(out["ndx100"])})
        else:
            snap = _ds_get(_DS_CONST, {})
            for k in out:
                out[k] = {t for t in (_norm_ticker(x) for x in snap.get(k) or []) if t}
    except Exception as e:
        _err("constituents snapshot", e)
    return out


def _book(with_holdings=True) -> set:
    src, out = _sources(), set()
    if "holdings" in src and with_holdings and _rh_on():
        try:
            out |= {t for t in (_norm_ticker(x) for x in (_holdings() or [])) if t}
        except Exception as e:
            _err("universe holdings", e)
    if "watchlist" in src:
        try:
            out |= {t for t in (_norm_ticker(x) for x in (_watchlist() or [])) if t}
        except Exception as e:
            _err("universe watchlist", e)
    return out


def _ordered_universe(with_holdings=True) -> list:
    src = _sources()
    book = _book(with_holdings)
    idx = set()
    if src & {"sp500", "ndx100"}:
        sets = _index_sets()
        for k in ("sp500", "ndx100"):
            if k in src:
                idx |= sets[k]
    return sorted(book) + sorted(idx - book)


def universe() -> list:
    """Sorted symbol list from the configured sources. Flag off → []. Never raises."""
    if not enabled():
        return []
    try:
        return sorted(_ordered_universe())
    except Exception as e:
        _err("universe", e)
        return []


# ── parsing / ingest ─────────────────────────────────────────────────────────
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_results(symbol, payload) -> list:
    """get_earnings_results payload → earnings_results rows for `symbol` (pure; [] on anything malformed)."""
    out, seen = [], set()
    try:
        sym = _norm_ticker(symbol)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not sym or not isinstance(rows, list):
            return []
        for r in rows:
            if not isinstance(r, dict) or _norm_ticker(r.get("symbol")) != sym:
                continue
            y, q = r.get("year"), r.get("quarter")
            if not isinstance(y, int) or isinstance(y, bool) or not isinstance(q, int) or isinstance(q, bool) \
                    or not (1990 <= y <= 2100) or q not in (1, 2, 3, 4):
                continue
            rep = r.get("report") if isinstance(r.get("report"), dict) else {}
            try:
                d = date.fromisoformat(str(rep.get("date"))[:10]).isoformat()
            except (TypeError, ValueError):
                continue
            fp = f"{y}-Q{q}"
            if fp in seen:
                continue
            seen.add(fp)
            eps = r.get("eps") if isinstance(r.get("eps"), dict) else {}
            est, act = _num(eps.get("estimate")), _num(eps.get("actual"))
            sp = (act - est) / abs(est) * 100.0 if (est not in (None, 0.0) and act is not None) else None
            timing = rep.get("timing") if rep.get("timing") in ("am", "pm") else None
            out.append({"ticker": sym, "fiscal_period": fp, "report_date": d, "timing": timing,
                        "eps_est": est, "eps_act": act, "surprise_pct": sp,
                        "verified": 1 if rep.get("verified") is True else 0,
                        "raw_json": json.dumps(r, sort_keys=True, default=str)})
    except Exception as e:
        _err(f"parse {symbol!r}", e)
        return []
    return out


def _upsert_rows(rows: list) -> int:
    if not rows:
        return 0
    now = _now_iso()

    def f(c):
        for r in rows:
            c.execute("INSERT INTO earnings_results (ticker, fiscal_period, report_date, timing, eps_est, eps_act,"
                      " surprise_pct, verified, raw_json, created_date) VALUES (?,?,?,?,?,?,?,?,?,?)"
                      " ON CONFLICT(ticker, fiscal_period) DO UPDATE SET report_date=excluded.report_date,"
                      " timing=excluded.timing, eps_est=excluded.eps_est, eps_act=excluded.eps_act,"
                      " surprise_pct=excluded.surprise_pct, verified=excluded.verified, raw_json=excluded.raw_json",
                      (r["ticker"], r["fiscal_period"], r["report_date"], r["timing"], r["eps_est"], r["eps_act"],
                       r["surprise_pct"], r["verified"], r["raw_json"], now))
        c.commit()
    mason_db.with_connection(f)
    return len(rows)


def _due(sym: str, stamps: dict, open_dates: dict, today: date) -> bool:
    s = stamps.get(sym)
    try:
        last = date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        last = None
    if last is None or (today - last).days > REFETCH_DAYS:
        return True
    return any(d <= today.isoformat() for d in open_dates.get(sym, ()))


def _open_dates() -> dict:
    """{T: [report_date]} for stored rows that still lack an actual (upcoming reports)."""
    def f(c):
        return c.execute("SELECT ticker, report_date FROM earnings_results WHERE eps_act IS NULL").fetchall()
    out = {}
    for r in mason_db.with_connection(f):
        out.setdefault(r[0], []).append(r[1])
    return out


def _refresh_sectors(symbols: list, errors: list) -> int:
    cache = _ds_get(_DS_SECTOR, {})
    today = _today_et()
    need = []
    for s in symbols:
        e = cache.get(s)
        try:
            fresh = isinstance(e, dict) and (today - date.fromisoformat(e.get("as_of"))).days <= SECTOR_TTL_DAYS
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            need.append(s)
    if not need:
        return 0
    import mason_robinhood
    calls = 0
    for i in range(0, len(need), FUND_CHUNK):
        chunk = need[i:i + FUND_CHUNK]
        try:
            data = mason_robinhood._call("get_equity_fundamentals", {"symbols": chunk}, lane="bulk")
            calls += 1
            res = data.get("results") if isinstance(data, dict) else None
            if not isinstance(res, list):
                errors.append(_err(f"fundamentals {','.join(chunk)}: malformed payload"))
                continue
            got = {}
            for r in res:
                t = _norm_ticker(r.get("symbol")) if isinstance(r, dict) else None
                if t in chunk:
                    sec = r.get("sector") if isinstance(r.get("sector"), str) and r.get("sector").strip() else None
                    ind = r.get("industry") if isinstance(r.get("industry"), str) else None
                    got[t] = {"sector": sec.strip() if sec else None, "industry": ind, "as_of": today.isoformat()}
            for t in chunk:
                cache[t] = got.get(t) or {"sector": None, "industry": None, "as_of": today.isoformat()}
        except Exception as e:
            errors.append(_err(f"fundamentals {','.join(chunk)}", e))
    _ds_set(_DS_SECTOR, cache)
    return calls


def sector_of(ticker):
    """Cached RH fundamentals sector (current value) or None. Flag off → None."""
    if not enabled():
        return None
    try:
        e = _ds_get(_DS_SECTOR, {}).get(_norm_ticker(ticker) or "")
        return e.get("sector") if isinstance(e, dict) else None
    except Exception as e:
        _err("sector_of", e)
        return None


def sweep(symbols=None, *, force=False) -> dict:
    """RH earnings → earnings_results (+ sector cache). Idempotent. Never raises."""
    if not enabled():
        return {"enabled": False}
    errors = []
    out = {"ok": False, "symbols": 0, "fetched": 0, "empty": 0, "skipped": 0, "rows": 0, "truncated": 0,
           "fundamentals_calls": 0, "errors": errors}
    try:
        if not _rh_on():
            out["error"] = "robinhood_disabled"
            return out
        import mason_robinhood
        _state["last_sweep_at"] = _now_iso()
        if symbols is None:
            syms = _ordered_universe()
        else:
            syms = []
            for s in symbols if isinstance(symbols, (list, tuple, set)) else []:
                t = _norm_ticker(s)
                if t and t not in syms:
                    syms.append(t)
        if len(syms) > MAX_SYMBOLS:
            out["truncated"] = len(syms) - MAX_SYMBOLS
            syms = syms[:MAX_SYMBOLS]
        out["symbols"] = len(syms)
        today = _today_et()
        stamps = _ds_get(_DS_FETCHED, {})
        open_dates = _open_dates()
        with_rows = []
        for k, sym in enumerate(syms):
            if not force and not _due(sym, stamps, open_dates, today):
                out["skipped"] += 1
                continue
            try:
                data = mason_robinhood._call("get_earnings_results", {"symbol": sym}, lane="bulk")
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    errors.append(_err(f"earnings {sym}: malformed payload"))
                    continue
                rows = parse_results(sym, data)
                out["rows"] += _upsert_rows(rows)
                stamps[sym] = today.isoformat()
                if rows:
                    out["fetched"] += 1
                    with_rows.append(sym)
                else:
                    out["fetched"] += 1
                    out["empty"] += 1
            except Exception as e:
                errors.append(_err(f"earnings {sym}", e))
            if (k + 1) % _STAMP_EVERY == 0:
                _ds_set(_DS_FETCHED, stamps)
        _ds_set(_DS_FETCHED, stamps)
        out["fundamentals_calls"] = _refresh_sectors(with_rows, errors)
        out["ok"] = not errors
    except Exception as e:
        errors.append(_err("sweep", e))
        out["ok"] = False
    finally:
        _state["truncated"] = out.get("truncated", 0)
        _state["last_sweep"] = {k: v for k, v in out.items() if k != "errors"}
        _state["last_sweep"]["n_errors"] = len(errors)
    return out


# ── quintiles ────────────────────────────────────────────────────────────────
def assign_quintile(surprise, history, min_history=None):
    """Midrank-percentile quintile (1 = biggest misses … 5 = biggest beats) of `surprise` within `history`
    (the strictly-earlier same-sector surprises). None below `min_history` (default MIN_HISTORY)."""
    s = _num(surprise)
    mh = MIN_HISTORY if min_history is None else min_history
    h = [x for x in (_num(v) for v in (history or [])) if x is not None]
    if s is None or len(h) < max(1, mh):
        return None
    below = sum(1 for x in h if x < s)
    eq = sum(1 for x in h if x == s)
    p = (below + 0.5 * eq) / len(h)
    return min(5, 1 + int(p * 5))


# ── compute ──────────────────────────────────────────────────────────────────
def _load_reports() -> list:
    def f(c):
        return [dict(r) for r in c.execute(
            "SELECT ticker, fiscal_period, report_date, timing, eps_est, eps_act, surprise_pct FROM earnings_results"
            " ORDER BY report_date, ticker, fiscal_period LIMIT ?", (MAX_REPORTS,))]
    return mason_db.with_connection(f)


def _dedupe(rows: list) -> list:
    """One row per (ticker, report_date) (T6-a1): prefer the row with an actual; if both or neither have
    one, keep the later fiscal period. Order of the input is kept for the survivors."""
    best = {}
    for r in rows:
        k = (r.get("ticker"), r.get("report_date"))
        cur = best.get(k)
        if cur is None or ((r.get("eps_act") is not None, str(r.get("fiscal_period") or ""))
                           > (cur.get("eps_act") is not None, str(cur.get("fiscal_period") or ""))):
            best[k] = r
    keep = {id(v) for v in best.values()}
    return [r for r in rows if id(r) in keep]


def _classify(r) -> dict:
    if not isinstance(r, dict):
        return {"status": "withheld", "error": "malformed grader result", "xs": None, "ret": None,
                "exit_date": None}
    out = {"xs": None, "ret": None, "exit_date": r.get("exit_date"), "error": r.get("error")}
    ret, xs = _num(r.get("ret")), _num(r.get("xs"))
    if r.get("matured") is True and ret is not None:
        if xs is None:
            return dict(out, status="withheld", ret=ret, error=out["error"] or "no_benchmark")
        return dict(out, status="sample", ret=ret, xs=xs)
    if out["error"]:
        return dict(out, status="withheld")
    return dict(out, status="pending")


def _gap(pc, t: str, entry: str):
    e = date.fromisoformat(entry)
    bars = pc.get_daily(t, (e - timedelta(days=GAP_MAX_LOOKBACK + 10)).isoformat(), entry) or []
    bars = [b for b in bars if isinstance(b, dict) and isinstance(b.get("date"), str)]
    bars.sort(key=lambda b: b["date"])
    i = next((k for k, b in enumerate(bars) if b["date"] == entry), None)
    if not i:
        return None
    o, pcl = _num(bars[i].get("open")), _num(bars[i - 1].get("close"))
    if not o or not pcl or o <= 0 or pcl <= 0:
        return None
    if (e - date.fromisoformat(bars[i - 1]["date"])).days > GAP_MAX_LOOKBACK:
        return None
    return o / pcl - 1.0


def compute() -> dict:
    """Grade every stored report (see module docstring). Flag off → {"enabled": False}. Never raises."""
    if not enabled():
        return {"enabled": False}
    errors = []
    hz = {h: {"samples": 0, "withheld": 0, "pending": 0, "contaminated": 0, "skipped": 0} for h in HORIZONS}
    out = {"ok": False, "events": 0, "bucketed": 0, "unbucketed": {}, "horizons": hz, "event_rows": [],
           "errors": errors, "reports": 0, "deduped": 0}
    try:
        pc = _pc()
        if not pc.enabled():
            errors.append(_err("price_cache is disabled (rh_expansion.price_cache.enabled) — nothing graded"))
            return out
        raw = _load_reports()
        rows = _dedupe(raw)
        out["reports"], out["deduped"] = len(raw), len(raw) - len(rows)
        sectors = _ds_get(_DS_SECTOR, {})
        sec = {t: (v.get("sector") if isinstance(v, dict) else None) for t, v in sectors.items()}
        today = _today_et().isoformat()
        hist, next_by = {}, {}
        for r in rows:
            next_by.setdefault(r["ticker"], []).append(r["report_date"])
            s = sec.get(r["ticker"])
            if s and r["eps_act"] is not None and r["surprise_pct"] is not None and r["report_date"] < today:
                hist.setdefault(s, []).append((r["report_date"], r["surprise_pct"]))
        for v in hist.values():
            v.sort()
        hist_dates = {s: [d for d, _ in v] for s, v in hist.items()}
        for v in next_by.values():
            v.sort()
        for r in rows:
            if r["eps_act"] is None or not r["report_date"] or r["report_date"] >= today:
                continue                                  # upcoming / today's unsettled report: never graded
            try:
                out["event_rows"].append(_grade_event(pc, r, sec, hist, hist_dates, next_by, hz))
            except Exception as e:
                errors.append(_err(f"event {r.get('ticker')} {r.get('fiscal_period')}", e))
        out["events"] = len(out["event_rows"])
        for ev in out["event_rows"]:
            if ev["bucket_reason"] is None:
                out["bucketed"] += 1
            else:
                out["unbucketed"][ev["bucket_reason"]] = out["unbucketed"].get(ev["bucket_reason"], 0) + 1
        out["ok"] = True
    except Exception as e:
        errors.append(_err("compute", e))
        out["ok"] = False
    return out


def _grade_event(pc, r, sec, hist, hist_dates, next_by, hz) -> dict:
    t, D = r["ticker"], r["report_date"]
    s = sec.get(t)
    ev = {"ticker": t, "fiscal_period": r["fiscal_period"], "report_date": D, "timing": r["timing"],
          "surprise_pct": r["surprise_pct"], "sector": s, "quintile": None, "history_n": 0, "gap": None,
          "gap_pct": None, "entry_rule": None, "entry_date": None, "bucket_reason": None,
          "h": {h: {"status": "skipped", "xs": None, "ret": None, "error": None, "exit_date": None}
                for h in HORIZONS}}
    if s:
        lo = (date.fromisoformat(D) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        dl = hist_dates.get(s, [])
        a, b = bisect_left(dl, lo), bisect_left(dl, D)          # [D − lookback, D): strictly before D
        prior = [v for _, v in hist[s][a:b]] if s in hist else []
        ev["history_n"] = len(prior)
        ev["quintile"] = assign_quintile(r["surprise_pct"], prior)
    reason = ("no_sector" if not s else "unknown_timing" if r["timing"] not in ("am", "pm")
              else "no_surprise" if r["surprise_pct"] is None else "thin_history" if ev["quintile"] is None
              else None)
    if r["timing"] in ("am", "pm"):
        rule = "on_or_after" if r["timing"] == "am" else "after"
        ev["entry_rule"] = rule
        nxt = next((d for d in next_by.get(t, []) if d > D), None)
        for h in HORIZONS:
            res = pc.fwd_return(t, D, sessions=h, entry_rule=rule)
            c = _classify(res)
            if ev["entry_date"] is None and isinstance(res, dict) and isinstance(res.get("entry_date"), str):
                ev["entry_date"] = res["entry_date"]
            if c["status"] == "sample" and nxt and c.get("exit_date") and nxt <= str(c["exit_date"]):
                c["status"] = "contaminated"
            ev["h"][h] = c
            key = {"sample": "samples"}.get(c["status"], c["status"])
            hz[h][key] += 1
        if ev["entry_date"]:
            g = _gap(pc, t, ev["entry_date"])
            if g is not None:
                ev["gap_pct"] = g * 100.0
                ev["gap"] = "up" if g > 0 else "down" if g < 0 else "flat"
    else:
        for h in HORIZONS:
            hz[h]["skipped"] += 1
    if reason is None and ev["entry_date"] is None:
        reason = "no_entry"
    if reason is None and ev["gap"] is None:
        reason = "no_gap"
    ev["bucket_reason"] = reason
    return ev


# ── priors ───────────────────────────────────────────────────────────────────
def _buckets(event_rows) -> dict:
    """{(h, key_json): {"key", "samples", "withheld", "refs"}} for bucketed events."""
    out = {}
    for ev in event_rows:
        if ev["bucket_reason"] is not None:
            continue
        for h in HORIZONS:
            c = ev["h"][h]
            if c["status"] not in ("sample", "withheld"):
                continue
            for key in ({"metric": "xs", "sector": ev["sector"], "quintile": ev["quintile"], "gap": ev["gap"]},
                        {"metric": "xs_all_sectors", "sector": "all", "quintile": ev["quintile"],
                         "gap": ev["gap"]}):
                b = out.setdefault((h, json.dumps(key, sort_keys=True)),
                                   {"key": key, "samples": [], "withheld": 0, "refs": []})
                if c["status"] == "sample":
                    b["samples"].append(c["xs"])
                    b["refs"].append(f"{ev['ticker']}:{ev['fiscal_period']}")
                else:
                    b["withheld"] += 1
    return out


def _write_priors(res: dict) -> dict:
    out = {"written": 0, "failed": 0, "retired": None, "pools": {}}
    if not _sub_on("priors"):
        return {"skipped": "priors_disabled"}
    kbp = _kbp()
    run_id = kbp.begin_run(FAMILY)
    buckets = _buckets(res["event_rows"])
    errs = []
    for (h, kj) in sorted(buckets):
        b = buckets[(h, kj)]
        xs = b["samples"]
        try:
            row = kbp.upsert_prior(FAMILY, b["key"], f"{h}s", xs, summary_fmt=SUMMARY_FMT if xs else None,
                                   refs=b["refs"], withheld=b["withheld"],
                                   excess_vs_spy=(math.fsum(xs) / len(xs)) if xs else None)
        except Exception as e:
            row = None
            errs.append(_err(f"upsert {h}s {kj}", e))
        if row is None:
            out["failed"] += 1
            if not errs or not errs[-1].startswith(f"upsert {h}s {kj}"):
                errs.append(_err(f"upsert {h}s {kj} returned None"))
            continue
        out["written"] += 1
        p = out["pools"].setdefault(f"{h}s", {})
        p[b["key"]["metric"]] = p.get(b["key"]["metric"], 0) + 1
    for h in HORIZONS:
        res["horizons"][h]["withheld_bucketed"] = sum(
            b["withheld"] for (hh, _), b in buckets.items() if hh == h and b["key"]["metric"] == "xs")
    res["errors"].extend(errs)
    if res["errors"]:
        out["retired"] = "skipped_errors"
    else:
        out["retired"] = int(kbp.retire_unrefreshed(FAMILY, run_id) or 0)
    big = {w: d for w, d in out["pools"].items() if any(n > POOL_WARN for n in d.values())}
    if big:
        out["pool_warning"] = f"pool above {POOL_WARN} rows: {big}"
    return out


def run() -> dict:
    """compute() → kb_priors family `pead`. Idempotent. Never raises."""
    if not enabled():
        return {"enabled": False}
    _state["last_run"] = _now_iso()
    res = compute()
    out = {k: v for k, v in res.items() if k != "event_rows"}
    out["priors"] = {}
    try:
        if res.get("ok"):
            out["priors"] = _write_priors(res)
            out["horizons"] = res["horizons"]
            out["errors"] = res["errors"]
    except Exception as e:
        res["errors"].append(_err("priors", e))
        out["errors"] = res["errors"]
        out["priors"] = {"error": str(e)[:200], "retired": "skipped_errors"}
    finally:
        _state["last_summary"] = {"ok": out.get("ok"), "events": out.get("events"), "bucketed": out.get("bucketed"),
                                  "unbucketed": out.get("unbucketed"), "horizons": out.get("horizons"),
                                  "pools": (out.get("priors") or {}).get("pools"),
                                  "written": (out.get("priors") or {}).get("written"),
                                  "retired": (out.get("priors") or {}).get("retired"),
                                  "n_errors": len(out.get("errors") or [])}
    return out


# ── report ───────────────────────────────────────────────────────────────────
def _prior_rows() -> list:
    def f(c):
        return [dict(r) for r in c.execute(
            "SELECT key_json, window, n, stat_json, min_n_met FROM kb_priors WHERE family=?"
            " AND coalesce(record_deleted,0)=0", (FAMILY,))]
    out = []
    for r in mason_db.with_connection(f):
        try:
            r["key"], r["stat"] = json.loads(r["key_json"]), json.loads(r["stat_json"] or "{}")
        except (TypeError, ValueError):
            continue
        out.append(r)
    return out


def _pct(v, spec="+.2f"):
    v = _num(v)
    return "—" if v is None else format(v * 100, spec) + "%"


def _row_md(r) -> str:
    st, k = r["stat"], r["key"]
    ci = st.get("ci95") or [None, None]
    return (f"| {r['window']} | {k.get('sector')} | Q{k.get('quintile')} | {k.get('gap')} | {r['n']} | "
            f"{_pct(st.get('mean'))} | {_pct(ci[0])} to {_pct(ci[1])} | {_pct(st.get('hit_rate'), '.0f')} | "
            f"{_pct(st.get('shrunk_mean'))} | {st.get('withheld', 0)} |")


_HDR = ("| horizon | sector | surprise quintile | day-1 gap | n | mean xs vs SPY | CI95 (bootstrap) | hit | "
        "shrunk mean | withheld |\n|---|---|---|---|---|---|---|---|---|---|")


def report_markdown(result=None, *, universe_note="") -> str:
    """PEAD_REPORT.md body: provenance, method, 10 strongest / 10 weakest buckets (n + CI). Never raises."""
    try:
        result = result if isinstance(result, dict) else dict(_state.get("last_summary") or {})
        try:
            min_n = max(1, int((_rhx().get("priors") or {}).get("min_n", 10)))
        except (TypeError, ValueError):
            min_n = 10
        rows = _prior_rows()
        sec_rows = [r for r in rows if r["key"].get("metric") == "xs" and r["n"] >= min_n
                    and _num(r["stat"].get("mean")) is not None]
        pooled = [r for r in rows if r["key"].get("metric") == "xs_all_sectors" and r["n"] >= 1]
        strong = sorted(sec_rows, key=lambda r: (-r["stat"]["mean"], r["window"], r["key_json"]))[:10]
        weak = sorted(sec_rows, key=lambda r: (r["stat"]["mean"], r["window"], r["key_json"]))[:10]
        hz = result.get("horizons") or {}
        L = ["# PEAD report: earnings surprise × post-earnings drift (S1-T6)", "",
             f"Generated {_now_iso()} by `mason_pead.py` (`report_markdown`). kb_priors family `pead`.", "",
             "## Data this was produced from", "", f"- **Universe / size:** {universe_note or 'not stated'}",
             f"- Stored report rows: {result.get('reports')}; same-date duplicates collapsed: "
             f"{result.get('deduped', 0)}",
             f"- Reports graded (events): {result.get('events')}; bucketed: {result.get('bucketed')}; "
             f"unbucketed by reason: {json.dumps(result.get('unbucketed') or {}, sort_keys=True)}",
             f"- Bucket rows (n ≥ 1): {len(rows)}; per-sector buckets meeting min_n={min_n}: {len(sec_rows)}",
             f"- Pool sizes (rows per window/metric, the T5 re-shrink pool): "
             f"{json.dumps((result.get('priors') or {}).get('pools') or result.get('pools') or {}, sort_keys=True)}",
             "", "| horizon | samples | withheld (data errors) | pending (not matured) | contaminated (next report "
                 "inside window) | skipped (unknown timing) |", "|---|---|---|---|---|---|"]
        for h in HORIZONS:
            d = hz.get(h) or hz.get(str(h)) or {}
            L.append(f"| {h}s | {d.get('samples', 0)} | {d.get('withheld', 0)} | {d.get('pending', 0)} | "
                     f"{d.get('contaminated', 0)} | {d.get('skipped', 0)} |")
        L += ["", "## Method", "",
              "- **Entry = the first close after the report**, through `mason_price_cache.fwd_return` only:",
              "  - timing `am` (before the open on D) → `entry_rule=\"on_or_after\"` → the close of D;",
              "  - timing `pm` (after the close on D) → `entry_rule=\"after\"` → the close of the next session;",
              "  - no/unknown timing → not graded.",
              "- **Sample** = excess return vs SPY (`xs`) over 1/5/20/60 sessions from the entry close. A horizon "
              "counts only when `fwd_return` says `matured=True`. Results with an `error` (incomplete_data, "
              "entry_gap, data_gap, …) are **withheld** (counted on each prior, never sampled).",
              "- **Surprise** = (actual − estimate) / |estimate|. **Quintile** within the RH sector, from the "
              f"same-sector reports dated strictly before D (trailing {LOOKBACK_DAYS} days; at least "
              f"{MIN_HISTORY} needed, else unbucketed). Midrank percentile; Q1 = biggest misses, Q5 = biggest beats.",
              "- **One event per (ticker, report date):** when two fiscal periods share a date, the row with an "
              "actual wins; if both or neither have one, the later fiscal period wins.",
              "- Surprise uses Robinhood's consensus EPS estimate; other vendors' consensus can differ by about "
              "5–15%.",
              "- CI95 treats events as independent; reports cluster by earnings season, so true intervals are "
              "wider.",
              "- **Day-1 gap** = open of the entry session vs the previous close: up / down, or flat when exactly 0.",
              "- **Overlap:** a report's window that contains the same ticker's next report date is dropped "
              "(`contaminated`), so one ticker's windows never overlap at any horizon.",
              "- Stats: T5 `upsert_prior` (seeded bootstrap CI95 of the mean, 2,000 resamples; empirical-Bayes "
              "shrunk mean, k=10).", "",
              f"## 10 strongest buckets (per sector, n ≥ {min_n}, by mean excess return)", "", _HDR]
        L += [_row_md(r) for r in strong] or ["| — | none meet min_n | | | | | | | | |"]
        L += ["", f"## 10 weakest buckets (per sector, n ≥ {min_n}, by mean excess return)", "", _HDR]
        L += [_row_md(r) for r in weak] or ["| — | none meet min_n | | | | | | | | |"]
        if len(sec_rows) < 20:
            L += ["", f"Only {len(sec_rows)} per-sector bucket rows meet min_n, so the two lists overlap or are "
                      "short. The pooled table below is the better read at this sample size."]
        L += ["", "## Pooled across sectors (surprise quintile × day-1 gap)", "", _HDR]
        L += [_row_md(r) for r in sorted(pooled, key=lambda r: (int(r["window"][:-1]), r["key"]["quintile"],
                                                                 r["key"]["gap"]))] or ["| — | none | | | | | | | | |"]
        L += ["", "## Caveats", "",
              "- **Survivorship:** the universe is *today's* S&P 500 / Nasdaq-100 plus the current book. Names that "
              "left the index (or were acquired/delisted, whose grades stay withheld) are missing, which biases "
              "drift estimates.",
              "- **Sector is a current value** (RH fundamentals today). A company reclassified since an older report "
              "is bucketed under today's sector.",
              "- **CI95 assumes independent samples.** Reports cluster in earnings season, so different tickers' "
              "windows overlap in calendar time and share market/sector moves. The CIs are optimistic.",
              "- **60-session overlap is common.** Quarterly reports are about 63 sessions apart, so a 60-session "
              "window often reaches the next report. Those samples are dropped (`contaminated`), not graded, and "
              "the 60s buckets are thinner than 1s/5s/20s.",
              "- **Depth:** RH returns only the trailing 7 reported quarters per symbol, so this covers about 2 years. "
              "Treat per-sector buckets as weak evidence until n is large.",
              "- Estimates are RH's consensus as served today. A revised estimate after the report can't be "
              "detected.", ""]
        return "\n".join(L)
    except Exception as e:
        return f"# PEAD report\n\nreport failed: {_err('report', e)}\n"


def write_report(path, result=None, *, universe_note="") -> str | None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report_markdown(result, universe_note=universe_note), encoding="utf-8")
        return str(p)
    except Exception as e:
        _err("write_report", e)
        return None


# ── status / jobs / CLI ──────────────────────────────────────────────────────
def status() -> dict:
    """For /api/rh/status (contracts §6). Never raises; flag off → no DB access."""
    base = {"name": NAME, "enabled": False, "last_run": _state.get("last_run"), "rows": 0,
            "errors": [_redact(e) for e in (_state.get("errors") or [])[-10:]],
            "truncated": _state.get("truncated", 0), "rate_hits": 0, "xcheck_mismatch": 0,
            "rh_enabled": False, "last_sweep_at": _state.get("last_sweep_at"),
            "last_sweep": dict(_state.get("last_sweep") or {}), "last_summary": dict(_state.get("last_summary") or {})}
    try:
        if not enabled():
            return base
        base["enabled"] = True
        base["rh_enabled"] = _rh_on()
        try:
            import mason_robinhood
            base["rate_hits"] = int(((mason_robinhood.status() or {}).get("rate") or {}).get("rate_hits") or 0)
        except Exception:
            pass
        base["rows"] = mason_db.with_connection(lambda c: c.execute("SELECT COUNT(*) FROM earnings_results")
                                                .fetchone()[0])
    except Exception as e:
        _err("status", e)
        base["errors"] = [_redact(x) for x in (_state.get("errors") or [])[-10:]]
    return base


# ── earnings as data (Fable step 2, audit R5, 2026-11-14) ───────────────────────────────────────────
# The 09:10 sweep above stays as it is. Added here, each behind its own switch:
#   session_sweep()    every 10 minutes 16:05-18:30 and 06:30-09:15 ET on trading days: fetch results only for
#                      the companies due to report in that session (held and watchlist names first, then index
#                      members), until each has its number. A result that lands for a held, watched or large
#                      company is handed to mason_desk_check with the numbers (raise_trigger).
#   sync_calendar()    earnings dates of held and watchlist names → calendar_events (event_type 'earnings',
#                      source 'Robinhood'), idempotent, for the calendar widget.
#   director_blocks()  the two text blocks the director can be shown ("Reporting today and tonight",
#                      "Reported since your last review"); mason_director sends them only when
#                      analyst_desk.opus_director.v2.earnings_blocks is on.
# Switch: rh_expansion.pead.session_sweep.enabled (default false). Robinhood is read-only here
# (get_earnings_results, get_earnings_calendar) and every call goes through mason_robinhood._call's bulk lane,
# which enforces rh_expansion.rate.bulk_per_s and backs off on a 429.
_SESSION_DEFAULTS = {"enabled": False, "pm_window_et": ["16:05", "18:30"], "am_window_et": ["06:30", "09:15"],
                     "every_s": 600, "max_symbols_per_pass": 40, "calendar": True}
_DS_SESSION_CAL = "pead:session_calendar"
_CAL_SOURCE = "Robinhood"


def _session_cfg() -> dict:
    out = dict(_SESSION_DEFAULTS)
    try:
        for k, v in ((_cfg().get("session_sweep") or {}).items()):
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def session_enabled() -> bool:
    try:
        return enabled() and _session_cfg().get("enabled") is True
    except Exception:
        return False


def _now_et(now=None) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(_ET) if now.tzinfo else now.replace(tzinfo=_ET)
    return datetime.now(_ET) if now is None else datetime.fromtimestamp(float(now), _ET)


def _is_session_day(d: date) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(datetime(d.year, d.month, d.day)))
    except Exception:
        return d.weekday() < 5


def _step_session(d: date, step: int) -> date:
    for _ in range(10):
        d = d + timedelta(days=step)
        if _is_session_day(d):
            return d
    return d


def session_of(now_et: datetime) -> str | None:
    """'pm' inside the after-close window, 'am' inside the before-open window, else None."""
    sc, hm = _session_cfg(), now_et.strftime("%H:%M")
    for name in ("pm", "am"):
        w = sc.get(f"{name}_window_et") or _SESSION_DEFAULTS[f"{name}_window_et"]
        if isinstance(w, (list, tuple)) and len(w) == 2 and str(w[0]) <= hm <= str(w[1]):
            return name
    return None


def _held() -> set:
    """Stocks somebody holds: the Alpaca paper account and the personal accounts (mason_watchlist's
    cached list), plus the in-scope Robinhood positions when Robinhood is on."""
    out = set()
    try:
        import mason_watchlist
        out |= {t for t in (_norm_ticker(x) for x in (mason_watchlist.position_symbols() or [])) if t}
    except Exception as e:
        _err("held (paper/personal)", e)
    if _rh_on():
        try:
            out |= {t for t in (_norm_ticker(x) for x in (_holdings() or [])) if t}
        except Exception as e:
            _err("held (robinhood)", e)
    return out


def _watched() -> set:
    try:
        return {t for t in (_norm_ticker(x) for x in (_watchlist() or [])) if t}
    except Exception as e:
        _err("watched", e)
        return set()


def _large_caps() -> set:
    try:
        sets = _index_sets()
        return set(sets.get("sp500") or ()) | set(sets.get("ndx100") or ())
    except Exception:
        return set()


def _calendar_rows(day: date, days: int, errors: list) -> list:
    """get_earnings_calendar rows [{ticker, report_date, timing, eps_est, eps_act, fiscal_period}] (one RH call)."""
    import mason_robinhood
    out = []
    try:
        args = {"start_date": day.isoformat(), "days": int(days)}
        data = mason_robinhood._call("get_earnings_calendar", args, lane="bulk")
        rows = data.get("results") if isinstance(data, dict) else (data if isinstance(data, list) else None)
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            t = _norm_ticker(r.get("symbol"))
            rep_ = r.get("report") if isinstance(r.get("report"), dict) else {}
            eps = r.get("eps") if isinstance(r.get("eps"), dict) else {}
            try:
                d = date.fromisoformat(str(rep_.get("date") or r.get("date") or "")[:10]).isoformat()
            except ValueError:
                continue
            if not t:
                continue
            y, q = r.get("year"), r.get("quarter")
            fp = f"{y}-Q{q}" if isinstance(y, int) and isinstance(q, int) and not isinstance(y, bool) else None
            act = _num(eps.get("actual"))
            out.append({"ticker": t, "report_date": d, "timing": rep_.get("timing") if rep_.get("timing") in ("am", "pm") else None,
                        "eps_est": _num(eps.get("estimate")), "fiscal_period": fp, "had_actual": act is not None,
                        # an actual on a FUTURE-dated row is last quarter's number, not a result (mason_exposure ruling 35)
                        "eps_act": act if d <= day.isoformat() else None})
        newest = {}
        for r in out:
            if r["fiscal_period"]:
                newest[r["ticker"]] = max(newest.get(r["ticker"], ""), r["fiscal_period"])
        # a row that carries an actual and an OLDER fiscal period than another row of the same company is stale
        out = [r for r in out if not (r["had_actual"] and r["fiscal_period"] and r["fiscal_period"] < newest.get(r["ticker"], ""))]
    except Exception as e:
        errors.append(_err("earnings calendar", e))
    return out


def session_candidates(session: str, today: date, *, extra=None) -> list:
    """Companies due to report in this session that have no result stored yet. Order: held, watchlist, then
    index members. Source: the upcoming rows already in earnings_results (+ `extra` rows from the RH calendar).
    am session: today's before-open reporters, plus yesterday's after-close reporters still without a number."""
    prev = _step_session(today, -1).isoformat()
    ti = today.isoformat()

    def f(c):
        return c.execute("SELECT ticker, report_date, timing FROM earnings_results WHERE eps_act IS NULL "
                         "AND report_date IN (?,?)", (ti, prev)).fetchall()
    rows = [(r[0], r[1], r[2]) for r in mason_db.with_connection(f)]
    rows += [(x["ticker"], x["report_date"], x.get("timing")) for x in (extra or []) if x.get("eps_act") is None]
    due = set()
    for t, d, tm in rows:
        if session == "pm" and d == ti and tm != "am":
            due.add(t)
        elif session == "am" and ((d == ti and tm != "pm") or (d == prev and tm != "am")):
            due.add(t)
    held, watched, big = _held(), _watched(), _large_caps()
    first = sorted(due & held) + sorted((due & watched) - held)
    return first + sorted((due & big) - set(first))


def session_sweep(now=None) -> dict:
    """One pass of the session sweep. Idempotent (upserts; a result raises its desk-check event once).
    Never raises."""
    if not session_enabled():
        return {"enabled": False}
    errors = []
    out = {"ok": False, "session": None, "due": 0, "fetched": 0, "landed": [], "raised": [], "errors": errors}
    try:
        if not _rh_on():
            out["error"] = "robinhood_disabled"
            return out
        import mason_robinhood
        et = _now_et(now)
        today = et.date()
        session = session_of(et)
        out["session"] = session
        if session is None or not _is_session_day(today):
            out["ok"] = True
            return out
        sc = _session_cfg()
        cal = _ds_get(_DS_SESSION_CAL, {})
        if cal.get("day") != today.isoformat():        # one calendar call a day: who reports today
            cal = {"day": today.isoformat(), "rows": _calendar_rows(today, 1, errors)}
            _ds_set(_DS_SESSION_CAL, cal)
        syms = session_candidates(session, today, extra=cal.get("rows"))
        out["due"] = len(syms)
        held, watched = _held(), _watched()
        lo = _step_session(today, -1).isoformat()
        for sym in syms[:max(1, int(sc.get("max_symbols_per_pass") or 40))]:
            try:
                data = mason_robinhood._call("get_earnings_results", {"symbol": sym}, lane="bulk")
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    errors.append(_err(f"session earnings {sym}: malformed payload"))
                    continue
                rows = parse_results(sym, data)
                out["fetched"] += 1
                before = {r[0] for r in mason_db.with_connection(lambda c: c.execute(
                    "SELECT fiscal_period FROM earnings_results WHERE ticker=? AND eps_act IS NOT NULL", (sym,)).fetchall())}
                _upsert_rows(rows)
                for r in rows:
                    if r["eps_act"] is None or r["fiscal_period"] in before or not (lo <= r["report_date"] <= today.isoformat()):
                        continue
                    out["landed"].append(sym)
                    if _raise_result(r, sym in held, sym in watched):
                        out["raised"].append(sym)
            except Exception as e:
                errors.append(_err(f"session earnings {sym}", e))
        if sc.get("calendar", True):
            out["calendar"] = sync_calendar(fetch=False)
        out["ok"] = not errors
    except Exception as e:
        errors.append(_err("session_sweep", e))
    finally:
        _state["last_session_sweep"] = {k: v for k, v in out.items() if k != "errors"}
    return out


def _raise_result(row: dict, held: bool, watched: bool) -> bool:
    """Hand a result that just landed to the desk check, with the numbers. False when the desk check does not
    want it (switch off, or a company nobody holds or watches with a small surprise)."""
    try:
        import mason_desk_check as DC
        if not DC.wants_earnings(row["ticker"], row.get("surprise_pct"), held=held, watched=watched):
            return False
        return DC.raise_trigger(
            "earnings", row["ticker"], f"earnings:{row['ticker']}:{row['fiscal_period']}",
            {"fiscal_period": row["fiscal_period"], "report_date": row["report_date"], "timing": row.get("timing"),
             "eps_est": row.get("eps_est"), "eps_act": row.get("eps_act"), "surprise_pct": row.get("surprise_pct")},
            prio=0 if held else (2 if watched else 3), strong_key=f"{row['ticker']}:earnings")
    except Exception as e:
        _err(f"desk-check event {row.get('ticker')}", e)
        return False


def _cal_id(natural_key: str) -> str:
    import hashlib
    return hashlib.sha1(f"{_CAL_SOURCE}|{natural_key}".encode("utf-8", "ignore")).hexdigest()[:16]


def sync_calendar(fetch=True) -> dict:
    """Earnings dates of held and watchlist names → calendar_events. One row per (ticker, fiscal period): a moved
    date updates the same row. fetch=True adds one RH calendar call (31 days) for names with no stored upcoming
    row. Never raises."""
    if not enabled():
        return {"enabled": False}
    out = {"ok": False, "new": 0, "changed": 0, "same": 0}
    errors = []
    try:
        names = _held() | _watched()
        if not names:
            out["ok"] = True
            return out
        today = _today_et()
        lo = (today - timedelta(days=3)).isoformat()

        def f(c):
            return c.execute("SELECT ticker, fiscal_period, report_date, timing, eps_est, eps_act, surprise_pct, verified "
                             "FROM earnings_results WHERE report_date >= ?", (lo,)).fetchall()
        rows = [dict(zip(("ticker", "fiscal_period", "report_date", "timing", "eps_est", "eps_act", "surprise_pct",
                          "verified"), r)) for r in mason_db.with_connection(f) if r[0] in names]
        for r in rows:                                 # a number on a future-dated row is last quarter's, not a result
            if r["report_date"] > today.isoformat():
                r["eps_act"] = r["surprise_pct"] = None
        have = {r["ticker"] for r in rows if r["eps_act"] is None}
        if fetch and _rh_on() and (names - have):
            first = {}
            for r in _calendar_rows(today, 31, errors):
                if r["ticker"] in names and r["ticker"] not in have and r.get("eps_act") is None \
                        and r["report_date"] >= today.isoformat() \
                        and r["report_date"] < first.get(r["ticker"], {}).get("report_date", "9999"):
                    first[r["ticker"]] = r             # the next report of each name, once
            rows += [dict(r, surprise_pct=None, verified=None) for r in first.values()]
        now_ep, now_iso = datetime.now(timezone.utc).timestamp(), _now_iso()

        def w(c):
            for r in rows:
                cid = _cal_id(f"earnings:{r['ticker']}:{r.get('fiscal_period') or r['report_date']}")
                when = {"am": "before the open", "pm": "after the close"}.get(r.get("timing"))
                status = "reported" if r.get("eps_act") is not None else "scheduled"
                title = f"{r['ticker']} earnings" + (f" ({r['fiscal_period']})" if r.get("fiscal_period") else "")
                det = json.dumps({k: r.get(k) for k in ("fiscal_period", "timing", "eps_est", "eps_act", "surprise_pct",
                                                         "verified")}, separators=(",", ":"), sort_keys=True)
                prev = c.execute("SELECT event_date, event_time, status, details_json FROM calendar_events WHERE id=?",
                                 (cid,)).fetchone()
                if prev is None:
                    c.execute("INSERT INTO calendar_events (id, source, event_type, ticker, title, event_date, event_time, "
                              "status, url, details_json, first_seen, last_updated, created_date, created_by, record_deleted) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                              (cid, _CAL_SOURCE, "earnings", r["ticker"], title, r["report_date"], when, status, None, det,
                               now_ep, now_ep, now_iso, CREATED_BY))
                    out["new"] += 1
                elif (prev[0], prev[1], prev[2], prev[3]) != (r["report_date"], when, status, det):
                    c.execute("UPDATE calendar_events SET title=?, event_date=?, event_time=?, status=?, details_json=?, "
                              "last_updated=?, record_deleted=0 WHERE id=?",
                              (title, r["report_date"], when, status, det, now_ep, cid))
                    out["changed"] += 1
                else:
                    out["same"] += 1
            c.commit()
        mason_db.with_connection(w)
        out["ok"] = not errors
    except Exception as e:
        out["error"] = _err("sync_calendar", e)
    return out


def _last_close(ticker, before_day: str):
    """Last daily close strictly before `before_day`, from the price cache (the only price source this module uses)."""
    try:
        d = date.fromisoformat(before_day)
        bars = [b for b in (_pc().get_daily(ticker, d - timedelta(days=10), d - timedelta(days=1)) or [])
                if _num(b.get("close"))]
        return _num(bars[-1]["close"]) if bars else None
    except Exception:
        return None


def _last_price(ticker):
    """Newest stored price: the live tape's last one-minute bar if the name is watched, else the last daily close."""
    def f(c):
        r = c.execute("SELECT c FROM watch_bars WHERE ticker=? AND tf='1m' ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
        return r[0] if r else None
    try:
        px = _num(mason_db.with_connection(f))
        return px if px is not None else _last_close(ticker, (_today_et() + timedelta(days=1)).isoformat())
    except Exception:
        return None


def _when_words(d: str, timing) -> str:
    try:
        day = date.fromisoformat(d).strftime("%a %m-%d")
    except ValueError:
        day = d
    return f"{day} " + {"am": "before the open", "pm": "after the close"}.get(timing, "(time of day not known)")


def director_blocks(since_epoch=None, *, max_lines: int = 8, now=None) -> list:
    """[(title, body, tickers)] for the director's input: who reports today / tonight / before tomorrow's open, and
    what has been reported since its last review. Held and watchlist names first. [] when there is nothing or the
    module is off. Reads stored data only (no Robinhood call). Never raises."""
    if not enabled():
        return []
    try:
        et = _now_et(now)
        today = et.date()
        ti, nxt = today.isoformat(), _step_session(today, 1).isoformat()
        held, watched, big = _held(), _watched(), _large_caps()

        def tag(t):
            return " [held]" if t in held else (" [watchlist]" if t in watched else "")

        def rank(t):
            return 0 if t in held else (1 if t in watched else 2)

        def f(c):
            # today: not reported yet; the next session: any row (a number on a future-dated row is last quarter's)
            up = c.execute("SELECT ticker, report_date, timing, eps_est FROM earnings_results WHERE "
                           "(report_date = ? AND eps_act IS NULL) OR (report_date = ? AND report_date > ?)",
                           (ti, nxt, ti)).fetchall()
            lo = (today - timedelta(days=6)).isoformat()
            done = c.execute("SELECT ticker, report_date, timing, eps_est, eps_act, surprise_pct FROM earnings_results "
                             "WHERE eps_act IS NOT NULL AND report_date >= ? AND report_date <= ?", (lo, ti)).fetchall()
            return up, done
        up, done = mason_db.with_connection(f)
        out = []
        rows = [r for r in up if (r[1] == ti or r[2] == "am") and (r[0] in held or r[0] in watched or r[0] in big)]
        rows.sort(key=lambda r: (rank(r[0]), r[1], r[2] or "z", r[0]))
        if rows:
            L = []
            for t, d, tm, est in rows[:max_lines]:
                last = mason_db.with_connection(lambda c: c.execute(
                    "SELECT surprise_pct, report_date FROM earnings_results WHERE ticker=? AND eps_act IS NOT NULL "
                    "AND surprise_pct IS NOT NULL AND report_date < ? ORDER BY report_date DESC LIMIT 1", (t, d)).fetchone())
                L.append(f"- {t}{tag(t)}: {_when_words(d, tm)}"
                         + (f"; analysts expect {est:.2f} a share" if est is not None else "")
                         + (f"; last time it {'beat' if last[0] >= 0 else 'missed'} by {abs(last[0]):.0f}% ({last[1]})"
                            if (last and abs(last[0]) <= 300) else ""))   # (review fix R7) no absurd percentages
            if len(rows) > max_lines:
                L.append(f"({len(rows) - max_lines} more large companies report in the same window; not listed.)")
            out.append(("REPORTING TODAY AND TONIGHT (earnings dates from Robinhood; held and watchlist names first)",
                        "\n".join(L), [r[0] for r in rows[:max_lines]]))
        since = float(since_epoch) if since_epoch else (et.timestamp() - 24 * 3600)
        got = []
        for t, d, tm, est, act, sp in done:
            if not (t in held or t in watched or (t in big and sp is not None and abs(sp) >= 15)):
                continue
            hh = {"am": 8, "pm": 16}.get(tm, 12)
            try:
                moment = datetime.combine(date.fromisoformat(d), datetime.min.time(), _ET).timestamp() + hh * 3600 + 1800
            except ValueError:
                continue
            if since <= moment <= et.timestamp():
                got.append((t, d, tm, est, act, sp))
        got.sort(key=lambda r: (rank(r[0]), -(abs(r[5]) if r[5] is not None else 0), r[0]))
        if got:
            L = []
            for t, d, tm, est, act, sp in got[:max_lines]:
                base_day = d if tm == "am" else (date.fromisoformat(d) + timedelta(days=1)).isoformat()
                base, px = _last_close(t, base_day), _last_price(t)
                react = (f"; the price is {(px / base - 1) * 100:+.1f}% against the last close before the report"
                         if (base and px) else "; no price reaction on record yet")
                L.append(f"- {t}{tag(t)}: reported {_when_words(d, tm)}: {act:.2f} a share"
                         + (f" against {est:.2f} expected" if est is not None else "")
                         # (review fix R7) no percentage against an expected value near zero
                         + ((f" ({'beat' if sp >= 0 else 'miss'} of {abs(sp):.0f}%)" if (est is not None and abs(est) >= 0.05 and abs(sp) <= 300)
                             else f" ({'beat' if sp >= 0 else 'miss'})") if sp is not None else "") + react)
            out.append(("REPORTED SINCE YOUR LAST REVIEW (actual against expected earnings per share, and the price reaction; "
                        "revenue is not in this data)", "\n".join(L), [r[0] for r in got[:max_lines]]))
        return out
    except Exception as e:
        _err("director_blocks", e)
        return []


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
    """S1 job contract (mason_rh_history, above jobs()); S1 schedules nothing. The 09:10 sweep picks up
    yesterday's pm and today's early am reports before the open (only due symbols are re-fetched). Priors
    recompute at 17:25, after the close bars settle and after regimes (17:05) has warmed SPY/SPX in
    price_cache, so the two bulk-lane pulls never overlap. Market data only — no run_on_deploy needed."""
    out = [{"name": "rhx_pead_sweep", "at_et": "09:10", "fn": _job("sweep")},
           {"name": "rhx_pead_priors", "at_et": "17:25", "fn": _job("run"), "after": ["rhx_regimes_daily"]}]
    if session_enabled():                      # step 2: results as they land + calendar rows (switch off → unchanged)
        sc = _session_cfg()
        try:
            ev = max(120, int(sc.get("every_s") or 600))
        except (TypeError, ValueError):
            ev = 600
        for name in ("pm", "am"):
            w = sc.get(f"{name}_window_et")
            if not (isinstance(w, (list, tuple)) and len(w) == 2):
                w = _SESSION_DEFAULTS[f"{name}_window_et"]
            out.append({"name": f"rhx_pead_session_{name}", "every_s": ev, "window_et": (str(w[0]), str(w[1])),
                        "fn": _job("session_sweep")})
        if sc.get("calendar", True):
            out.append({"name": "rhx_pead_calendar", "at_et": "09:25", "fn": _job("sync_calendar"),
                        "after": ["rhx_pead_sweep"]})
    return out


def _arg(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if cmd == "sweep":
        if not enabled():
            print("refused: rh_expansion.pead.enabled must be true")
            return 2
        if "--live" not in argv:
            u = _ordered_universe(with_holdings=False)
            print(json.dumps({"dry_run": True, "symbols_excl_holdings": len(u), "max_symbols": MAX_SYMBOLS,
                              "est_seconds_at_bulk_2_per_s": round(min(len(u), MAX_SYMBOLS) / 2.0)}, indent=2))
            return 0
        if not _rh_on():
            print("refused: robinhood.enabled (master switch) must be true for a live sweep")
            return 2
        out = sweep()
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1
    if cmd in ("run", "report"):
        if not enabled() or not _sub_on("price_cache"):
            print("refused: rh_expansion.pead.enabled and rh_expansion.price_cache.enabled must both be true")
            return 2
        out = run()
        if cmd == "report":
            dest = _arg(argv, "--out") or str(Path(__file__).resolve().parent / "docs" / "robinhood" / "expansion"
                                              / "S1" / "PEAD_REPORT.md")
            out["report"] = write_report(dest, out, universe_note=_arg(argv, "--note") or "")
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1
    print("usage: python3 mason_pead.py sweep [--live] | run | report [--out PATH] [--note TEXT] | status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
