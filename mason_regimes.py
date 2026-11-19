"""
mason_regimes.py — deep day history & market-regime labels (RH Expansion S1-T7,
docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §2 T7; contracts §5 market_regimes, kb_priors family `regime`).

run()
  1. Deep history. get_daily(sym, HISTORY_START, yesterday) for every symbol in UNIVERSE — SPX, NDX, VIX,
     SPY, QQQ, IWM, the 11 sector SPDRs, TLT, HYG, plus RSP (breadth proxy). Prices come ONLY through
     mason_price_cache.get_daily / fwd_return (REVIEW_T3: never direct SQL on the price-cache table). The price cache
     owns RH (bulk lane, index windows ≤1,000 days, interpolated bars dropped), the yfinance cross-check and
     the master switch; this module makes no network call of its own. SPX is required; RSP/SPY/VIX missing
     only turn their component to "na"; any other symbol failing is reported in status() and not fatal.
  2. Label every SPX session D (compute_labels, a pure function) and upsert market_regimes (date PK;
     a recompute updates in place and keeps the first created_date, so an identical rerun is byte-identical).
  3. Priors, family `regime`, through mason_kb_priors.upsert_prior only (imported lazily):
       {"label": L, "metric": "spx_fwd_20s"}, window "20s": fwd_return("SPX", D, sessions=20, vs=None) for
         every labelled D (matured only — the one grading function, contracts §4);
       {"label": L, "metric": "personal_hit_rate"}, window "all": realized_pl of Nico's in-scope rh_trades,
         keyed by the regime of the session the trade was OPENED in.
     A label with any "na" component gets no prior. Unmatured dates are counted as `pending`.

NO LOOK-AHEAD — the exact definition. A label for session D uses closes dated ≤ D only (D's own close
included; nothing after it):
  trend       SPX close_D vs SMA200_D = mean of the 200 SPX closes ending at D (inclusive), and the 50-day
              slope = SMA50_D / SMA50_{D−10 sessions} − 1 (both SMAs end at their own date, inclusive).
              uptrend: close > SMA200 and slope > 0; downtrend: close < SMA200 and slope < 0; else sideways.
              Fewer than 200 SPX closes ≤ D → "na". (Sessions are counted from the real SPX bars, never from
              mason_director.is_trading_day — its holiday list starts 2026-11-26.)
  vol_regime  v = the last VIX close dated ≤ D (stale > STALE_DAYS calendar days → "na"). Cut points q1, q2 =
              terciles (statistics.quantiles n=3, "inclusive") of the VIX closes dated in (D − 5 years, D].
              v ≤ q1 low_vol; v > q2 high_vol; else mid_vol. Fewer than VIX_MIN_OBS closes in the window → "na".
  breadth     on the sessions where both RSP and SPY have a close: rel = (RSP/SPY)_t / (RSP/SPY)_{t−60} − 1 at
              the last common session t ≤ D (stale > STALE_DAYS → "na"; < 61 common sessions → "na").
              rel ≥ 0 broad, rel < 0 narrow.
  label       f"{trend}/{vol_regime}/{breadth}", e.g. "uptrend/low_vol/broad".
Every feature is a ratio (or the VIX level), so a retro-active split re-basing of RSP/SPY in the price cache
cannot change a label. Sums use math.fsum over the exact window, so a label does not depend on how much
earlier history happens to be cached. Proven by tests: truncate the inputs at D → the label for D is
identical; append or shock data after D → labels ≤ D unchanged.

Personal regime = the regime KNOWN AT ENTRY (DEBUG_T7 D1): opened_at (UTC) → America/New_York. Entered
before 16:00 ET on labelled session D (or opened_at is date-only) → the label of the PREVIOUS labelled
session, because D's label uses D's close. At/after 16:00 ET on D → D's label. Non-session day → the last
labelled session before it. At most STALE_DAYS back. The rule used is counted in priors.personal_rule.
Market prior samples are NON-OVERLAPPING per label (see regime_priors) so the CI95 is honest. Scope: record_deleted=0,
is_multileg=0, raw_json.parse != "unverified", realized_pl present, account not in
rh_expansion.personal_scope.exclude_accounts (default ["Household"]; read per call, so a flip needs no re-ingest).
opened_at NULL (fifo_unreconciled) → excluded and counted as withheld.no_opened_at.

SWITCHES: rh_expansion.regimes.enabled is this module's flag; off → every public function is a cheap no-op
(run/regime_priors → {"enabled": False}, regime_on → None, no hooks, no DB). Priors are additionally gated by
rh_expansion.priors.enabled (labels still land). Like the price cache (REVIEW_T3 OQ1) this module keeps
working on yfinance alone while robinhood.enabled is false — it never calls RH itself.
Public functions never raise. No HTTP routes. status() for /api/rh/status; jobs() for T9.

CLI:  python3 mason_regimes.py run | status      (run refuses unless regimes + price_cache are enabled)
"""

import json
import logging
import math
import re
import statistics
import sys
from bisect import bisect_right, insort
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import mason_db
import mason_robinhood as _RH

log = logging.getLogger("mason.regimes")

NAME = "regimes"
CONFIG = Path(__file__).resolve().parent / "mason.config.json"
_ET = ZoneInfo("America/New_York")
_DEFAULTS = {"enabled": False}
_cfg_cache = {"mtime": None, "data": None}

SECTORS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
UNIVERSE = ("SPX", "VIX", "SPY", "RSP", "NDX", "QQQ", "IWM") + SECTORS + ("TLT", "HYG")
CORE = ("SPX", "VIX", "RSP", "SPY")
HISTORY_START = "1990-01-01"          # = mason_price_cache._EARLIEST; RH index monthly bars start 1991/1993/2002
MAX_DATES = 20000                     # labelled sessions per run (≈79 years) — bounded loop
SMA_LONG, SMA_SHORT, SLOPE_LAG = 200, 50, 10
VIX_YEARS, VIX_MIN_OBS = 5, 504       # trailing 5-year window; ≥ 2 years of closes before a tercile is trusted
BREADTH_LAG = 60
STALE_DAYS = 5                        # an as-of value older than this (calendar days) is not used
FWD_SESSIONS = 20
NA = "na"
MAX_TRADES = 50000

PRIORS_HOOK = None                    # tests: recording stub; production: mason_kb_priors.upsert_prior

_state = {}


def _reset_state() -> None:
    _state.clear()
    _state.update({"last_run": None, "errors": [], "truncated": 0, "symbols": {}, "priors_error": None,
                   "last_summary": {}})


_reset_state()

_ACCT_RE = re.compile(r"(?<![\w.])\d{8,}(?![\w.])")


def _redact(s) -> str:
    return _ACCT_RE.sub(lambda m: "••" + m.group(0)[-4:], str(s))


def _err(where, e=None) -> str:
    msg = _redact(f"{where}: {type(e).__name__}: {e}" if e is not None else str(where))[:300]
    _state["errors"] = (_state["errors"] + [msg])[-50:]
    log.warning(f"[regimes] {msg}")
    return msg


# ── config / time hooks ──────────────────────────────────────────────────────
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


def enabled() -> bool:
    try:
        return (_rhx().get("regimes") or {}).get("enabled") is True
    except Exception:
        return False


def _priors_on() -> bool:
    try:
        return (_rhx().get("priors") or {}).get("enabled") is True
    except Exception:
        return False


def _exclude_accounts() -> set:
    """Normalized excluded labels via the shared parser (mason_robinhood, RULINGS #1). Fails closed."""
    try:
        return set(_RH.resolve_exclude_accounts(_config().get("rh_expansion"),
                                                log=lambda m: _err(m), source=NAME))
    except Exception:
        return {"household"}


def _today_et() -> date:
    return datetime.now(_ET).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pc():
    import mason_price_cache
    return mason_price_cache


def _import_priors():
    import mason_kb_priors          # S1-T5; may not exist yet
    return mason_kb_priors.upsert_prior


# ── pure labelling ───────────────────────────────────────────────────────────
def _clean(points) -> list:
    """[(YYYY-MM-DD, close)] → sorted, valid, de-duplicated (first value wins). Bad points are dropped."""
    seen, out = set(), []
    for p in points or []:
        try:
            d, c = p[0], float(p[1])
            d = date.fromisoformat(str(d)[:10]).isoformat() if d else None
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not d or d in seen or not math.isfinite(c) or c <= 0:
            continue
        seen.add(d)
        out.append((d, c))
    out.sort()
    return out


def _years_back(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:              # Feb 29 → Feb 28
        return d.replace(year=d.year - years, day=28)


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _r(v, nd=6):
    return None if v is None else round(v, nd)


def compute_labels(spx, vix, rsp, spy) -> list:
    """Pure. Inputs: [(date, close)]. Output, one dict per SPX session (ascending):
    {date, trend, vol_regime, breadth, label, features}. See the module docstring for the exact,
    look-ahead-free definitions. Never reads anything dated after the session it labels."""
    spx, vix = _clean(spx), _clean(vix)
    rsp_m, spy_m = dict(_clean(rsp)), dict(_clean(spy))
    common = sorted(set(rsp_m) & set(spy_m))
    ratio = [(d, rsp_m[d] / spy_m[d]) for d in common]
    spx = spx[-MAX_DATES - SMA_LONG:]
    closes = [c for _, c in spx]
    out = []
    vi, win, win_lo = 0, [], 0          # VIX pointer (bars ≤ D added), sorted window, oldest index still in it
    bi = 0                              # breadth pointer: ratio[:bi] are dated ≤ D
    for i, (d, c) in enumerate(spx):
        feats = {"spx_close": _r(c)}
        # trend — closes[i-199 .. i]
        trend = NA
        if i + 1 >= SMA_LONG:
            sma200 = math.fsum(closes[i + 1 - SMA_LONG:i + 1]) / SMA_LONG
            feats["sma200"] = _r(sma200)
            if i + 1 >= SMA_SHORT + SLOPE_LAG:
                s_now = math.fsum(closes[i + 1 - SMA_SHORT:i + 1]) / SMA_SHORT
                s_then = math.fsum(closes[i + 1 - SMA_SHORT - SLOPE_LAG:i + 1 - SLOPE_LAG]) / SMA_SHORT
                slope = s_now / s_then - 1.0
                feats["sma50_slope"] = _r(slope, 8)
                trend = ("uptrend" if c > sma200 and slope > 0 else
                         "downtrend" if c < sma200 and slope < 0 else "sideways")
        feats.setdefault("sma200", None)
        feats.setdefault("sma50_slope", None)
        # vol — VIX closes in (D − 5y, D]
        while vi < len(vix) and vix[vi][0] <= d:
            insort(win, vix[vi][1])
            vi += 1
        lo5 = _years_back(date.fromisoformat(d), VIX_YEARS).isoformat()
        while win_lo < vi and vix[win_lo][0] <= lo5:
            win.pop(bisect_right(win, vix[win_lo][1]) - 1)
            win_lo += 1
        vol = NA
        feats.update(vix=None, vix_date=None, vix_q1=None, vix_q2=None, vix_n=len(win))
        if vi:
            vd, v = vix[vi - 1]
            feats.update(vix=_r(v), vix_date=vd)
            if _days_between(vd, d) <= STALE_DAYS and len(win) >= VIX_MIN_OBS:
                q1, q2 = statistics.quantiles(win, n=3, method="inclusive")
                feats.update(vix_q1=_r(q1), vix_q2=_r(q2))
                vol = "low_vol" if v <= q1 else ("high_vol" if v > q2 else "mid_vol")
        # breadth — RSP/SPY relative over 60 common sessions ending at the last common session ≤ D
        while bi < len(ratio) and ratio[bi][0] <= d:
            bi += 1
        br = NA
        feats.update(rsp_spy_rel60=None, breadth_date=None)
        if bi > BREADTH_LAG:
            bd, now = ratio[bi - 1]
            rel = now / ratio[bi - 1 - BREADTH_LAG][1] - 1.0
            feats.update(rsp_spy_rel60=_r(rel, 8), breadth_date=bd)
            if _days_between(bd, d) <= STALE_DAYS:
                br = "broad" if rel >= 0 else "narrow"
        out.append({"date": d, "trend": trend, "vol_regime": vol, "breadth": br,
                    "label": f"{trend}/{vol}/{br}", "features": feats})
    return out[-MAX_DATES:]


def _complete(label: str) -> bool:
    return bool(label) and NA not in label.split("/")


# ── history ──────────────────────────────────────────────────────────────────
def _pull_history(errors: list) -> dict:
    pc = _pc()
    end = (_today_et() - timedelta(days=1)).isoformat()
    series, symbols = {}, {}
    trunc0 = int(pc._state.get("truncated") or 0) if isinstance(getattr(pc, "_state", None), dict) else 0
    for sym in UNIVERSE:
        try:
            raw = pc.get_daily(sym, HISTORY_START, end) or []
            pts = _clean((r.get("date"), r.get("close")) for r in raw if isinstance(r, dict))
        except Exception as e:
            errors.append(_err(f"get_daily {sym}", e))
            pts = None
        series[sym] = pts or []
        symbols[sym] = {"bars": len(pts or []), "first": pts[0][0] if pts else None,
                        "last": pts[-1][0] if pts else None}
        if pts == [] and sym != "SPX":
            errors.append(_err(f"no bars for {sym}"))
    try:
        trunc1 = int(pc._state.get("truncated") or 0)
        _state["truncated"] = max(0, trunc1 - trunc0)
    except Exception:
        _state["truncated"] = 0
    _state["symbols"] = symbols
    return series


def _upsert_labels(labels: list) -> int:
    now = _now_iso()

    def _w(conn):
        for r in labels:
            conn.execute("INSERT INTO market_regimes(date, trend, vol_regime, breadth, label, features_json, "
                         "created_date) VALUES (?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                         "trend=excluded.trend, vol_regime=excluded.vol_regime, breadth=excluded.breadth, "
                         "label=excluded.label, features_json=excluded.features_json",
                         (r["date"], r["trend"], r["vol_regime"], r["breadth"], r["label"],
                          json.dumps(r["features"], sort_keys=True), now))
        conn.commit()
        return len(labels)
    return mason_db.with_connection(_w)


# ── priors ───────────────────────────────────────────────────────────────────
_CLOSE_ET = (16, 0)


def _et_entry(iso):
    """opened_at → (ET date 'YYYY-MM-DD', after_close: bool) or (None, False). A date-only value (no time)
    is treated as INTRADAY (after_close False) — the conservative choice (DEBUG_T7 D1)."""
    try:
        s = str(iso).strip()
        if len(s) <= 10:
            return date.fromisoformat(s).isoformat(), False
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)      # store convention: UTC ISO (contracts §4)
        et = dt.astimezone(_ET)
        return et.date().isoformat(), (et.hour, et.minute) >= _CLOSE_ET
    except (TypeError, ValueError):
        return None, False


def _entry_label_index(dates: list, opened_at) -> tuple:
    """Index into the sorted labelled sessions of the regime KNOWN AT ENTRY, plus the rule used:
      entry before 16:00 ET (or date-only) on a labelled session D → the previous labelled session
        ("prior_session"; D's label needs D's close, which comes after the entry);
      entry at/after 16:00 ET on D → D ("same_session_after_close");
      entry on a non-session day (weekend/holiday/unlabelled) → the last labelled session < that day
        ("non_session_asof").
    The chosen session must be ≤ STALE_DAYS calendar days before the entry date, else (-1, rule).
    Known limit: a 13:00 ET half-day close still counts as intraday until 16:00 (conservative)."""
    od, after_close = _et_entry(opened_at)
    if not od:
        return -1, "non_session_asof"
    j = bisect_right(dates, od) - 1
    if j >= 0 and dates[j] == od:
        if after_close:
            k, rule = j, "same_session_after_close"
        else:
            k, rule = j - 1, "prior_session"
    else:
        k, rule = j, "non_session_asof"
    if k < 0 or _days_between(dates[k], od) > STALE_DAYS:
        return -1, rule
    return k, rule


def _personal_rows() -> list:
    def _q(conn):
        return [dict(r) for r in conn.execute(
            "SELECT id, account_label, opened_at, realized_pl, raw_json, is_multileg FROM rh_trades "
            "WHERE IFNULL(record_deleted,0)=0 AND realized_pl IS NOT NULL "
            "ORDER BY id LIMIT ?", (MAX_TRADES,))]
    return mason_db.with_connection(_q)


def _retire_api():
    """(begin_run, retire_unrefreshed) from mason_kb_priors (S1-T5), or (None, None) if unavailable.
    REVIEW_T7 Q3 / T7-a1: every regime write is bracketed by begin_run("regime") … retire_unrefreshed, so a key
    that was not refreshed this run (e.g. a label whose only trades were Household's, after the scope flips back
    to ["Household"]; or a label with no matured sample any more) is soft-deleted instead of lingering."""
    try:
        import mason_kb_priors as kb
        return kb.begin_run, kb.retire_unrefreshed
    except Exception as e:
        _err("regime_priors: retire API unavailable", e)
        return None, None


def regime_priors(labels=None) -> dict:
    """Write family `regime` priors from `labels` (compute_labels output; default: market_regimes table)."""
    if not enabled():
        return {"enabled": False}
    if not _priors_on():
        return {"enabled": False, "reason": "rh_expansion.priors.enabled is false"}
    try:
        upsert = PRIORS_HOOK or _import_priors()
    except Exception as e:
        msg = _err("regime_priors: mason_kb_priors unavailable", e)
        _state["priors_error"] = msg
        return {"ok": False, "error": msg, "market_written": 0, "personal_written": 0}
    _state["priors_error"] = None
    out = {"ok": True, "market_written": 0, "personal_written": 0, "pending": 0, "graded": 0, "retired": 0,
           "retire": "not_run",
           "personal_trades": 0,
           "personal_withheld": {"no_opened_at": 0, "no_regime": 0, "out_of_scope": 0, "multileg": 0,
                                 "unverified": 0},
           "personal_rule": {"prior_session": 0, "same_session_after_close": 0, "non_session_asof": 0},
           "errors": []}
    try:
        if labels is None:
            labels = mason_db.with_connection(lambda c: [dict(r) for r in c.execute(
                "SELECT date, label FROM market_regimes ORDER BY date LIMIT ?", (MAX_DATES,))])
        labels = [{"date": r["date"], "label": r["label"]} for r in labels]
        pc = _pc()
        begin_run, retire_unrefreshed = _retire_api()
        run_id = None
        if begin_run is not None:
            try:
                run_id = begin_run("regime")
            except Exception as e:
                out["errors"].append(_err("begin_run regime", e))
        # 1. SPX forward 20-session returns per label (fwd_return — the one grader, contracts §4).
        #    NON-OVERLAPPING (DEBUG_T7 Q2): within a label, a date is sampled only if it is ≥ FWD_SESSIONS
        #    SPX sessions after that label's previous sample, so no two samples share any forward window
        #    (daily samples overlap 19/20 and would make the bootstrap CI ~√20 too narrow). Session distance
        #    is the position in the label list = real SPX bars. A candidate that cannot be graded (not
        #    matured / error) is skipped without advancing, so the next date of that label is tried.
        groups, last_taken = {}, {}
        out["skipped_overlap"] = 0
        for i, r in enumerate(labels):
            if not _complete(r["label"]):
                continue
            lt = last_taken.get(r["label"])
            if lt is not None and i - lt < FWD_SESSIONS:
                out["skipped_overlap"] += 1
                continue
            g = pc.fwd_return("SPX", r["date"], sessions=FWD_SESSIONS, vs=None) or {}
            try:
                ret = float(g["ret"]) if g.get("matured") and g.get("ret") is not None else None
            except (TypeError, ValueError):
                ret = None
            if ret is None or not math.isfinite(ret):
                out["pending"] += 1
                continue
            last_taken[r["label"]] = i
            groups.setdefault(r["label"], []).append((r["date"], ret))
            out["graded"] += 1
        for lab in sorted(groups):
            pairs = groups[lab]
            key = {"label": lab, "metric": "spx_fwd_20s"}
            fmt = (f"In {lab}, SPX's next {FWD_SESSIONS} sessions: median {{median:+.2%}}, "
                   f"up {{hit_rate:.0%}} of the time (n={{n}} non-overlapping windows)")
            try:
                if upsert("regime", key, f"{FWD_SESSIONS}s", [v for _, v in pairs], summary_fmt=fmt,
                          refs=[d for d, _ in pairs]) is None:
                    out["errors"].append(_err(f"upsert_prior regime {lab} spx_fwd_20s returned None (not stored)"))
                    continue
                out["market_written"] += 1
            except Exception as e:
                out["errors"].append(_err(f"upsert_prior regime {lab} spx_fwd_20s", e))
        # 2. Nico's personal hit rate per label (regime of the opened_at session)
        dates = [r["date"] for r in labels]
        lab_by_date = {r["date"]: r["label"] for r in labels}
        excl = _exclude_accounts()
        wh = out["personal_withheld"]
        pgroups = {}
        for t in _personal_rows():
            try:
                raw = json.loads(t.get("raw_json") or "{}") or {}
            except (TypeError, ValueError):
                raw = {}
            if int(t.get("is_multileg") or 0):
                wh["multileg"] += 1                    # §2.4: single-leg only
                continue
            if isinstance(raw, dict) and raw.get("parse") == "unverified":
                wh["unverified"] += 1
                continue
            if _RH.norm_account_label(str(t.get("account_label") or "")) in excl:     # T-D6: both sides normalized
                wh["out_of_scope"] += 1
                continue
            if not t.get("opened_at"):
                wh["no_opened_at"] += 1               # fifo_unreconciled: no honest entry session
                continue
            k, rule = _entry_label_index(dates, t["opened_at"])
            if k < 0 or not _complete(lab_by_date[dates[k]]):
                wh["no_regime"] += 1
                continue
            try:
                pl = float(t["realized_pl"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(pl):
                continue
            pgroups.setdefault(lab_by_date[dates[k]], []).append((t["id"], pl))
            out["personal_trades"] += 1
            out["personal_rule"][rule] += 1
        for lab in sorted(pgroups):
            pairs = sorted(pgroups[lab])
            key = {"label": lab, "metric": "personal_hit_rate"}
            fmt = (f"Your trades opened in {lab} (regime known at entry) win {{hit_rate:.0%}} (n={{n}}), "
                   f"average P&L ${{mean:+,.2f}}")
            try:
                if upsert("regime", key, "all", [v for _, v in pairs], summary_fmt=fmt,
                          refs=[i for i, _ in pairs]) is None:
                    out["errors"].append(_err(f"upsert_prior regime {lab} personal_hit_rate returned None"))
                    continue
                out["personal_written"] += 1
            except Exception as e:
                out["errors"].append(_err(f"upsert_prior regime {lab} personal_hit_rate", e))
        # retire only after a clean pass: any error means some keys may be missing for the wrong reason
        if out["errors"]:
            out["retire"] = "skipped_errors"
        elif not run_id or retire_unrefreshed is None:
            out["retire"] = "unavailable"
        else:
            out["retired"] = int(retire_unrefreshed("regime", run_id) or 0)
            out["retire"] = "done"
    except Exception as e:
        out["ok"] = False
        out["retire"] = "skipped_errors"
        out["errors"].append(_err("regime_priors", e))
    return out


# ── public API ───────────────────────────────────────────────────────────────
def run() -> dict:
    """Pull history → label → upsert market_regimes → priors. Idempotent. Never raises."""
    if not enabled():
        return {"enabled": False}
    errors = []
    out = {"ok": False, "labels": 0, "written": 0, "errors": errors, "priors": {}}
    try:
        _state["last_run"] = _now_iso()
        pc = _pc()
        if not pc.enabled():
            errors.append(_err("price_cache is disabled (rh_expansion.price_cache.enabled) — no history"))
            return out
        series = _pull_history(errors)
        if not series.get("SPX"):
            errors.append(_err("no SPX history — nothing labelled, nothing written"))
            return out
        labels = compute_labels(series["SPX"], series.get("VIX"), series.get("RSP"), series.get("SPY"))
        out["labels"] = len(labels)
        out["written"] = _upsert_labels(labels)
        out["ok"] = True
        out["priors"] = regime_priors(labels)
        out["label_counts"] = {}
        for r in labels:
            out["label_counts"][r["label"]] = out["label_counts"].get(r["label"], 0) + 1
    except Exception as e:
        out["ok"] = False
        errors.append(_err("run", e))
    finally:
        _state["last_summary"] = {k: out.get(k) for k in ("ok", "labels", "written")}
        _state["last_summary"]["priors"] = {k: v for k, v in (out.get("priors") or {}).items() if k != "errors"}
    return out


def regime_on(day) -> dict | None:
    """The regime in force on `day`: the last labelled session ≤ day (≤ STALE_DAYS back). None if unknown."""
    if not enabled():
        return None
    try:
        d = date.fromisoformat(str(day)[:10]).isoformat()

        def _q(conn):
            return conn.execute("SELECT date, trend, vol_regime, breadth, label, features_json FROM market_regimes "
                                "WHERE date<=? ORDER BY date DESC LIMIT 1", (d,)).fetchone()
        r = mason_db.with_connection(_q)
        if r is None or _days_between(r["date"], d) > STALE_DAYS:
            return None
        out = dict(r)
        out["features"] = json.loads(out.pop("features_json") or "{}")
        return out
    except Exception as e:
        _err(f"regime_on {day!r}", e)
        return None


def status() -> dict:
    """For /api/rh/status (contracts §6). Never raises; flag off → no DB access."""
    base = {"name": NAME, "enabled": False, "last_run": _state.get("last_run"), "rows": 0,
            "errors": [_redact(e) for e in (_state.get("errors") or [])[-10:]],
            "truncated": _state.get("truncated", 0), "rate_hits": 0, "xcheck_mismatch": 0,
            "symbols": dict(_state.get("symbols") or {}), "priors_error": _state.get("priors_error"),
            "last_summary": dict(_state.get("last_summary") or {}), "latest": None}
    try:
        if not enabled():
            return base
        base["enabled"] = True
        # rate_hits stays 0: regimes makes no RH call of its own (boundary: RH only through price_cache), so the
        # bulk-lane history traffic and its rate-limit hits are reported on the price_cache row.

        def _q(conn):
            n = conn.execute("SELECT COUNT(*) FROM market_regimes").fetchone()[0]
            last = conn.execute("SELECT date, label FROM market_regimes ORDER BY date DESC LIMIT 1").fetchone()
            return n, (dict(last) if last else None)
        base["rows"], base["latest"] = mason_db.with_connection(_q)
    except Exception as e:
        _err("status", e)
        base["errors"] = [_redact(x) for x in (_state.get("errors") or [])[-10:]]
    return base


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
    """S1 job contract (mason_rh_history, above jobs()); S1 schedules nothing. 17:05 ET: the day's close bars
    have settled, and rhx_history_daily (16:40) has written today's rh_trades, which the personal hit rate per
    label joins. `regime` priors with metric=personal_hit_rate are personal data, so it also runs on deploy."""
    return [{"name": "rhx_regimes_daily", "at_et": "17:05", "fn": _job("run"), "after": ["rhx_history_daily"],
             "run_on_deploy": True}]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if cmd == "run":
        try:
            pc_on = bool(_pc().enabled())
        except Exception:
            pc_on = False
        if not enabled() or not pc_on:
            print("refused: rh_expansion.regimes.enabled and rh_expansion.price_cache.enabled must both be true")
            return 2
        out = run()
        print(json.dumps({k: v for k, v in out.items()}, indent=2, default=str))
        return 0 if out.get("ok") else 1
    print("usage: python3 mason_regimes.py run | status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
