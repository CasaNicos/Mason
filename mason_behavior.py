"""
mason_behavior.py — Behavior Coach (RH Expansion S2-T3).

Spec: docs/robinhood/RH_EXPANSION_S2_SIGNALS_TOOLS.md §2 T3; binding contracts
docs/robinhood/RH_EXPANSION_CONTRACTS.md §2 (boundaries), §5 behavior_findings,
§6 GET /api/rh/behavior, §9 mandatory tests.

What it does: reads Nico's in-scope real trading history (rh_trades + rh_orders,
written by S1-T2) and measures recurring habits — each as a pure function that returns
(value, baseline, n, ci95, dollars):

  disposition    median holding days of losers ÷ winners ("you sell winners 3.0x faster")
  weekday        P&L by entry weekday vs the all-days mean (the worst weekday is surfaced)
  chase_5/20     forward return of buys after a >= +5% prior day vs other buys
  dip_5/20       same, after a <= -5% prior day
  earnings_hold  P&L of trades held through an earnings report vs not (earnings_results)
  size_drift     position size after a loss ÷ size after a win (revenge sizing)
  round_trip     P&L of trades closed in under 2 days vs longer holds
  regime_fit     LINK to S1-T7's family 'regime' personal priors (read, never recomputed)
  exit_quality   share of the best move captured: realized ÷ max favourable excursion
                 over the holding window (daily bars from mason_price_cache)

Results go to behavior_findings (one row per metric, upserted) and to kb_priors family
'behavior' via mason_kb_priors.upsert_prior (S1-T5). Wording: a finding reads as a fact
only when n >= rh_expansion.priors.min_n AND the CI95 excludes the baseline; otherwise it
starts "Early signal (n=7): ...". Every finding = the behavior, its $ effect, and one
concrete rule worded as a suggestion to Nico. Nothing here acts on it.

Boundaries:
  * Account scope (contracts §2.8): rh_expansion.personal_scope.exclude_accounts
    (default ["Household"]) is applied at READ time, so flipping it needs no re-ingest.
  * Multi-leg rows (is_multileg=1) and DRIP buys (is_drip=1) are excluded.
  * Forward returns come only from mason_price_cache.fwd_return (contracts §4).
  * No RH calls at all — this module only reads the local DB.
  * Flags: robinhood.enabled (master) AND rh_expansion.behavior.enabled; otherwise every
    public function is a cheap neutral no-op.

mason_price_cache / mason_kb_priors are imported lazily inside functions so this module
imports cleanly before S1 lands them (S2 TEAM_BRIEF).
"""

import hashlib
import json
import logging
import math
import random
import statistics
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify

import mason_db

log = logging.getLogger("mason.behavior")

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
CREATED_BY = "mason_behavior"
WINDOW = "all"
MAX_ROWS = 20000              # bounded reads; a hit is reported as truncated in status()
MAX_PRICE_LOOKUPS = 4000      # bounded price-cache calls per run
BOOT_RESAMPLES = 1000
CHASE_PCT = 0.05
QUICK_DAYS = 2.0
EXIT_BASELINE = 0.5           # "half the move" reference line for exit quality
STALE_AFTER_H = 48
_ET = ZoneInfo("America/New_York")
_EPS = 1e-9

METRIC_ORDER = ("disposition", "weekday", "chase_5", "chase_20", "dip_5", "dip_20",
                "earnings_hold", "size_drift", "round_trip", "regime_fit", "exit_quality")
_DAY = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DEFAULTS = {
    "behavior": {"enabled": False},
    "priors": {"min_n": 10},
    "personal_scope": {"exclude_accounts": ["Household"]},
}

_cfg_cache: dict = {}
_lock = threading.Lock()
_STATUS = {"last_run": None, "errors": [], "truncated": False}


# ── config (read per call, mtime-cached, merged over _DEFAULTS) ──────────────────

def _config() -> dict:
    try:
        st = CONFIG.stat()
        key = (str(CONFIG), st.st_mtime_ns, st.st_size)
        if _cfg_cache.get("key") != key:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8")) or {}
            _cfg_cache["key"] = key
        return _cfg_cache.get("data") or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        for k, v in ((_config().get("rh_expansion") or {}).items()):
            if v is None or str(k).startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            else:
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    """Master switch robinhood.enabled AND rh_expansion.behavior.enabled (contracts §7)."""
    try:
        master = (_config().get("robinhood") or {}).get("enabled") is True
        return master and (_cfg().get("behavior") or {}).get("enabled") is True
    except Exception:
        return False


def _min_n() -> int:
    try:
        return max(1, int((_cfg().get("priors") or {}).get("min_n", 10)))
    except Exception:
        return 10


def _excluded() -> list:
    """Normalized (mason_robinhood.norm_account_label) account labels to leave out of personal stats (§2.8).
    Unified fail-closed rule (S2 Director): if the value isn't a list, or ANY entry is not a
    non-empty string after strip(), the result is the usable entries + "household", with an error
    recorded. An explicit [] means include everyone."""
    try:
        import mason_robinhood as _RH                 # T-D6 R2: same normalization as the row side
        xs = (_cfg().get("personal_scope") or {}).get("exclude_accounts", ["Household"])
        bad = None
        if isinstance(xs, str):
            xs, bad = [xs], "exclude_accounts must be a list, got str"
        elif not isinstance(xs, (list, tuple)):
            xs, bad = [], f"exclude_accounts must be a list, got {type(xs).__name__}"
        elif any(not (isinstance(x, str) and _RH.norm_account_label(x)) for x in xs):
            bad = "exclude_accounts has unusable entries"
        out = {_RH.norm_account_label(x) for x in xs if isinstance(x, str)} - {""}
        if bad:
            _err("config", ValueError(bad))
            out.add("household")
        out |= set(_RH.role_excluded_labels())            # A2: labels the last-4 role map excludes
        return sorted(out)
    except Exception as e:
        _err("config", e)
        try:                                              # A2 (QA R3): the failure path never drops map labels
            import mason_robinhood as _RH2
            return sorted({"household"} | set(_RH2.role_excluded_labels()))
        except Exception:
            return ["household"]


# ── status / errors ─────────────────────────────────────────────────────────────

def _reset_status():
    with _lock:
        _STATUS.update({"last_run": None, "errors": [], "truncated": False})


def _err(where: str, e) -> None:
    msg = f"{where}: {type(e).__name__}: {e}"[:300]
    log.warning(f"[behavior] {msg}")
    with _lock:
        _STATUS["errors"] = (_STATUS["errors"] + [msg])[-20:]


def _truncated():
    with _lock:
        _STATUS["truncated"] = True


# ── small helpers ───────────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _et_dt(iso):
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET)
    except (TypeError, ValueError):
        return None


def _et_date(iso):
    dt = _et_dt(iso)
    return dt.date() if dt else None


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _r(x, nd=6):
    return None if x is None else round(float(x), nd)


def _pctl(sorted_xs, q):
    return sorted_xs[min(len(sorted_xs) - 1, max(0, int(round(q * (len(sorted_xs) - 1)))))]


def _boot(stat, groups, seed):
    """Seeded percentile bootstrap CI95 of stat(*groups). None when too little data."""
    if any(len(g) < 2 for g in groups):
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(BOOT_RESAMPLES):
        try:
            v = stat(*[[g[rng.randrange(len(g))] for _ in g] for g in groups])
        except (ZeroDivisionError, statistics.StatisticsError, TypeError):
            v = None
        if v is not None:
            vals.append(v)
    if len(vals) < BOOT_RESAMPLES // 2:
        return None
    vals.sort()
    return [_r(_pctl(vals, 0.025)), _r(_pctl(vals, 0.975))]


_Z = statistics.NormalDist()


def _t_over_z(p, df):
    """Student-t quantile ÷ normal quantile at p (Cornish-Fisher expansion; no scipy)."""
    z = _Z.inv_cdf(p)
    if df <= 0:
        return 1.0
    t = z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)
    return t / z


def _diff_ci(a, b, seed, alpha=0.05):
    """CI of mean(a) − mean(b): seeded bootstrap SE resampling BOTH groups, × the Student-t
    quantile (df = smaller group − 1) with the n/(n−1) correction, so a null difference is
    called 'significant' about alpha of the time. None when either group has < 2 values."""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    d = _mean(a) - _mean(b)
    vals = [sum(rng.choices(a, k=na)) / na - sum(rng.choices(b, k=nb)) / nb
            for _ in range(BOOT_RESAMPLES)]
    se = statistics.pstdev(vals)
    m = min(na, nb)
    # bootstrap SE (both groups resampled) × t quantile, with the n/(n-1) variance correction
    half = _Z.inv_cdf(1 - alpha / 2) * _t_over_z(1 - alpha / 2, m - 1) * se * math.sqrt(m / (m - 1))
    return [d - half, d + half]


def _shift_ci(base, dci):
    """§6 keeps ci95 on the value scale: baseline + the difference interval, so 'CI excludes
    the baseline' ⇔ 'difference CI excludes 0'."""
    if dci is None or base is None:
        return None
    return [_r(base + dci[0]), _r(base + dci[1])]


def _seed(name):
    return int(hashlib.sha1(name.encode()).hexdigest()[:8], 16)


def _ratio_of(fn):
    def stat(a, b):
        da = fn(a)
        return None if not da else fn(b) / da
    return stat


def _tuple(r):
    return (r["value"], r["baseline"], r["n"], r["ci95"], r["dollars"])


def _res(value, baseline, n, ci95, dollars, *, samples=(), refs=(), extra=None, n_gate=None):
    # n_gate = the n the significance gate uses: for two-group comparisons the SMALLER
    # group, so 30 winners + 2 losers can never read as a fact.
    return {"value": _r(value), "baseline": _r(baseline), "n": int(n), "ci95": ci95,
            "n_gate": int(n if n_gate is None else n_gate),
            "dollars": None if dollars is None else round(float(dollars), 2),
            "samples": [float(s) for s in samples], "refs": [str(x) for x in refs][:50],
            "extra": extra or {}}


# ── metrics (pure: lists of normalized dicts in, result out) ────────────────────
# Trade dict keys: id, kind, symbol, pl, hold, qty, price, cost, entry_px, exit_px,
# opened_at, closed_at (+ mfe_px when annotated). Buy dict keys: id, symbol, day,
# prior_move, fwd {sessions: ret}, notional.

def _disposition(trades):
    w = [t for t in trades if t.get("pl") is not None and t["pl"] > 0 and t.get("hold") is not None]
    l = [t for t in trades if t.get("pl") is not None and t["pl"] < 0 and t.get("hold") is not None]
    n = len(w) + len(l)
    if not w or not l:
        return _res(None, 1.0, n, None, 0.0, extra={"winners": len(w), "losers": len(l)}, n_gate=0)
    mw = statistics.median([t["hold"] for t in w])
    ml = statistics.median([t["hold"] for t in l])
    extra = {"winners": len(w), "losers": len(l), "median_win": mw, "median_loss": ml}
    if mw <= 0:
        return _res(None, 1.0, n, None, 0.0, extra=extra)
    hw, hl = [t["hold"] for t in w], [t["hold"] for t in l]
    ci = _boot(_ratio_of(statistics.median), [hw, hl], _seed("disposition"))
    dollars = sum(t["pl"] for t in l if t["hold"] > mw)
    return _res(ml / mw, 1.0, n, ci, dollars, samples=[h / mw for h in hl],
                refs=[t["id"] for t in w + l], extra=extra, n_gate=min(len(w), len(l)))


def disposition(trades):
    """Median holding days of losers ÷ winners; 3.0 = 'you sell winners 3.0x faster'."""
    return _tuple(_disposition(trades))


def _weekday(trades, weekday, *, alpha=0.05, with_ci=True):
    """Trades opened on `weekday` vs trades opened on all OTHER days (the complement)."""
    dated = [(t, _et_dt(t.get("opened_at"))) for t in trades if t.get("pl") is not None]
    dated = [(t, d) for t, d in dated if d is not None]
    sub = [t["pl"] for t, d in dated if d.weekday() == weekday]
    rest = [t["pl"] for t, d in dated if d.weekday() != weekday]
    base = _mean(rest)
    if not sub:
        return _res(None, base, 0, None, 0.0, extra={"weekday": weekday}, n_gate=0)
    v = _mean(sub)
    ci = _shift_ci(base, _diff_ci(sub, rest, _seed(f"weekday{weekday}"), alpha)) if with_ci else None
    dollars = len(sub) * (v - base) if base is not None else 0.0
    return _res(v, base, len(sub), ci, dollars, samples=sub,
                refs=[t["id"] for t, d in dated if d.weekday() == weekday],
                extra={"weekday": weekday}, n_gate=min(len(sub), len(rest)))


def weekday_effect(trades, weekday):
    """P&L of trades opened on `weekday` (0=Mon, ET) vs trades opened on the other days."""
    return _tuple(_weekday(trades, weekday))


def _weekday_worst(trades):
    """(worst-day result, [(day, result) for every day with trades]). The worst day is picked
    on the point estimate, then tested at a Bonferroni level 1 − 0.05/k (k = days with trades),
    because picking the worst of k days is k comparisons."""
    live = [(i, r) for i, r in ((i, _weekday(trades, i, with_ci=False)) for i in range(7))
            if r["n"] > 0]
    if not live:
        return None, []
    j, _ = min(live, key=lambda x: x[1]["value"] - (x[1]["baseline"]
                                                    if x[1]["baseline"] is not None else x[1]["value"]))
    return _weekday(trades, j, alpha=0.05 / len(live)), live


def _move_split(buys, sessions, pick):
    ok = [b for b in buys if b.get("prior_move") is not None
          and (b.get("fwd") or {}).get(sessions) is not None]
    treat = [b for b in ok if pick(b["prior_move"])]
    other = [b for b in ok if not pick(b["prior_move"])]
    if not treat:
        return _res(None, _mean([b["fwd"][sessions] for b in other]), 0, None, 0.0,
                    extra={"sessions": sessions})
    rt = [b["fwd"][sessions] for b in treat]
    v, base = _mean(rt), _mean([b["fwd"][sessions] for b in other])
    ref = base if base is not None else 0.0
    dollars = sum((b["fwd"][sessions] - ref) * (b.get("notional") or 0.0) for b in treat)
    ro = [b["fwd"][sessions] for b in other]
    ci = _shift_ci(base, _diff_ci(rt, ro, _seed(f"move{sessions}{pick(1.0)}")))
    return _res(v, base, len(treat), ci, dollars, samples=rt, refs=[b.get("id") for b in treat],
                extra={"sessions": sessions}, n_gate=min(len(treat), len(other)))


def chase(buys, sessions, threshold=CHASE_PCT):
    """Return of buys made after a >= +threshold prior-day move vs other buys."""
    return _tuple(_move_split(buys, sessions, lambda m: m >= threshold - _EPS))


def dip_buy(buys, sessions, threshold=CHASE_PCT):
    """Return of buys made after a <= -threshold prior-day move vs other buys."""
    return _tuple(_move_split(buys, sessions, lambda m: m <= -threshold + _EPS))


def _held_through(t, reports):
    o, c = _et_date(t.get("opened_at")), _et_date(t.get("closed_at"))
    if not o or not c:
        return None
    for rd, timing in reports.get(t.get("symbol") or "", ()):
        if (timing == "am" and o < rd <= c) or (timing != "am" and o <= rd < c):
            return True
    return False


def _earnings_hold(trades, reports):
    held, other = [], []
    for t in trades:
        if t.get("pl") is None:
            continue
        h = _held_through(t, reports)
        if h is True:
            held.append(t)
        elif h is False:
            other.append(t)
    base = _mean([t["pl"] for t in other])
    if not held:
        return _res(None, base, 0, None, 0.0)
    pls = [t["pl"] for t in held]
    v = _mean(pls)
    ci = _shift_ci(base, _diff_ci(pls, [t["pl"] for t in other], _seed("earnings_hold")))
    dollars = len(held) * (v - (base if base is not None else 0.0))
    return _res(v, base, len(held), ci, dollars, samples=pls, refs=[t["id"] for t in held],
                n_gate=min(len(held), len(other)))


def earnings_hold(trades, reports):
    """P&L of trades held through a report vs not. reports = {ticker: [(date, timing)]}."""
    return _tuple(_earnings_hold(trades, reports))


def _size_drift(trades):
    ts = [t for t in trades if (t.get("cost") or 0) > 0 and _et_dt(t.get("opened_at"))
          and t.get("pl") is not None]
    ts.sort(key=lambda t: _et_dt(t["opened_at"]))
    closed = sorted([t for t in ts if _et_dt(t.get("closed_at"))],
                    key=lambda t: _et_dt(t["closed_at"]))
    after_loss, after_win = [], []
    for t in ts:
        o = _et_dt(t["opened_at"])
        prev = None
        for p in closed:
            if p is t:
                continue
            if _et_dt(p["closed_at"]) <= o:
                prev = p
            else:
                break
        if prev is None or prev["pl"] == 0:
            continue
        (after_loss if prev["pl"] < 0 else after_win).append(t)
    if not after_loss or not after_win:
        return _res(None, 1.0, len(after_loss), None, 0.0, n_gate=0)
    sl, sw = [t["cost"] for t in after_loss], [t["cost"] for t in after_win]
    mw = _mean(sw)
    ci = _boot(_ratio_of(_mean), [sw, sl], _seed("size_drift"))
    dollars = sum(t["pl"] for t in after_loss if t["cost"] > 1.25 * mw)
    return _res(_mean(sl) / mw, 1.0, len(after_loss), ci, dollars, samples=[s / mw for s in sl],
                refs=[t["id"] for t in after_loss], n_gate=min(len(after_loss), len(after_win)))


def size_drift(trades):
    """Mean position size (cost) after a losing trade ÷ after a winning trade."""
    return _tuple(_size_drift(trades))


def _round_trip(trades):
    known = [t for t in trades if t.get("hold") is not None and t.get("pl") is not None]
    quick = [t for t in known if t["hold"] < QUICK_DAYS]
    longer = [t for t in known if t["hold"] >= QUICK_DAYS]
    base = _mean([t["pl"] for t in longer])
    if not quick:
        return _res(None, base, 0, None, 0.0)
    pls = [t["pl"] for t in quick]
    v = _mean(pls)
    ci = _shift_ci(base, _diff_ci(pls, [t["pl"] for t in longer], _seed("round_trip")))
    dollars = len(quick) * (v - base) if base is not None else 0.0
    return _res(v, base, len(quick), ci, dollars, samples=pls, refs=[t["id"] for t in quick],
                n_gate=min(len(quick), len(longer)))


def round_trip(trades):
    """P&L of trades closed in under 2 days vs longer holds."""
    return _tuple(_round_trip(trades))


def _is_personal_key(k):
    """S1-T7's personal regime rows: key {"label": L, "metric": "personal_hit_rate"}
    (mason_regimes.py:499; kb_priors.is_personal_data rule). The SPX market rows
    (metric spx_fwd_20s) are not Nico's trades and are ignored."""
    return bool(k.get("label")) and k.get("metric") == "personal_hit_rate"


def _regime_fit(prior_rows):
    rows = []
    for r in prior_rows or []:
        k, st = r.get("key") or {}, r.get("stat") or {}
        hr, n = _f(st.get("hit_rate")), int(r.get("n") or 0)
        if isinstance(k, dict) and _is_personal_key(k) and hr is not None and n > 0:
            rows.append((k["label"], hr, n, st.get("ci95")))
    if not rows:
        return _res(None, None, 0, None, None)
    label, hr, n, ci = min(rows, key=lambda x: x[1])
    base = sum(h * m for _, h, m, _ in rows) / sum(m for _, _, m, _ in rows)
    ci = [_r(ci[0]), _r(ci[1])] if isinstance(ci, (list, tuple)) and len(ci) == 2 else None
    return _res(hr, base, n, ci, None, extra={"label": str(label)})


def regime_fit(prior_rows):
    """Worst personal regime label (from S1-T7 family 'regime' priors) vs the n-weighted
    average — a link to those priors, not a recomputation."""
    return _tuple(_regime_fit(prior_rows))


def _exit_quality(trades):
    caps, left, refs = [], 0.0, []
    skipped = sum(1 for t in trades if t.get("mfe_skip"))
    for t in trades:
        e, x, m, q = t.get("entry_px"), t.get("exit_px"), t.get("mfe_px"), t.get("qty")
        if t.get("kind") != "equity" or not e or e <= 0 or x is None or m is None or not q or q <= 0:
            continue
        m = max(m, x)
        if m - e <= _EPS:
            continue                      # never in profit → capture undefined
        caps.append((x - e) / (m - e))
        left += (m - x) * q
        refs.append(t["id"])
    if not caps:
        return _res(None, EXIT_BASELINE, 0, None, 0.0, extra={"skipped": skipped})
    ci = _boot(statistics.median, [caps], _seed("exit_quality"))
    return _res(statistics.median(caps), EXIT_BASELINE, len(caps), ci, -left, samples=caps,
                refs=refs, extra={"skipped": skipped})


def exit_quality(trades):
    """Median share of the max favourable excursion captured at exit (equity, daily bars)."""
    return _tuple(_exit_quality(trades))


# ── wording ─────────────────────────────────────────────────────────────────────

def is_significant(n, ci95, baseline, min_n) -> bool:
    """n >= min_n AND a finite CI95 that excludes a finite baseline. NaN/inf → False."""
    try:
        if n is None or n < min_n or not isinstance(ci95, (list, tuple)) or len(ci95) != 2:
            return False
        lo, hi, b = float(ci95[0]), float(ci95[1]), float(baseline)
        if not all(math.isfinite(x) for x in (lo, hi, b)):
            return False
        return not (lo <= b <= hi)
    except (TypeError, ValueError):
        return False


def _money(x):
    return "n/a" if x is None else f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _body(metric, v, b, extra):
    """(behavior sentence, suggestion) for one metric."""
    if metric == "disposition":
        if v is None:
            if not extra.get("losers"):
                return "No losing trades to compare against your winners yet", None
            if not extra.get("winners"):
                return "No winning trades to compare against your losers yet", None
            return "Not enough holding-day data to compare winners and losers yet", None
        med = ""
        if extra.get("median_win") is not None and extra.get("median_loss") is not None:
            med = f" (median {extra['median_win']:.1f} vs {extra['median_loss']:.1f} days)"
        if v >= 1:
            s = f"You sell winners {v:.1f}x faster than losers{med}"
            return s, ("when a loser reaches the number of days you usually give a winner, "
                       "review it the same way you would review a winner.")
        return (f"You hold winners {1 / v:.1f}x longer than losers{med}",
                "your exits on losers are already quicker than on winners; consider keeping that habit.")
    if metric == "weekday":
        day = _DAY[extra.get("weekday", 0)]
        s = f"Trades opened on {day[:3]} average {_money(v)} vs {_money(b)} on other days"
        if b is not None and v < b:
            return s, f"consider a smaller size or one extra check before opening a position on a {day}."
        return s, "no change suggested."
    if metric.startswith("chase") or metric.startswith("dip"):
        up = metric.startswith("chase")
        s = (f"Buys made after a {'+' if up else '-'}5% day returned {_pct(v)} over "
             f"{extra.get('sessions')} sessions vs {_pct(b)} for your other buys")
        worse = b is not None and v < b
        if up:
            return s, ("after a +5% day, consider setting a limit near the prior close instead of "
                       "buying at market." if worse else "no change suggested.")
        return s, ("after a -5% day, consider waiting one more session for the drop to settle "
                   "before buying." if worse else
                   "your dip buys are working so far; consider keeping the same size rule for them.")
    if metric == "earnings_hold":
        s = (f"Trades held through an earnings report average {_money(v)} vs {_money(b)} "
             "for trades that were not")
        return s, ("before a report, consider deciding in advance whether to hold, trim or exit."
                   if b is not None and v < b else
                   "holding through reports has worked so far; consider keeping size modest into them.")
    if metric == "size_drift":
        if v is None:
            return "No trade after a loss to compare with your size after a win yet", None
        s = f"After a loss your next position is {v:.1f}x the size you take after a win"
        return s, ("after a losing trade, consider keeping the next position at or below your usual size."
                   if v > 1 else "no change suggested.")
    if metric == "round_trip":
        s = f"Trades closed in under 2 days average {_money(v)} vs {_money(b)} for longer holds"
        return s, ("before closing a trade on the same or next day, consider writing down why the plan "
                   "changed." if b is not None and v < b else "no change suggested.")
    if metric == "regime_fit":
        s = (f"Your trades opened in {extra.get('label')} win {v:.0%} vs {b:.0%} across all "
             "regimes (see the regime priors)")
        return s, ("in that regime, consider smaller size or waiting for a clearer setup."
                   if v < b else "no change suggested.")
    if metric == "exit_quality":
        s = f"You capture {v:.0%} of the best move during your holds (median)"
        return s, ("consider setting a trailing exit once a position is up by half of your target."
                   if v < 1 else "no change suggested.")
    return f"{metric}: {v}", None


def finding_text(metric, value, baseline, n, ci95, dollars, *, min_n, extra=None,
                 n_gate=None) -> str:
    """Behavior + $ effect + one suggestion. Fact only when significant, else 'Early signal'.
    n_gate (default n) is the n checked against min_n — the smaller group for comparisons."""
    extra = extra or {}
    s, rule = _body(metric, value, baseline, extra)
    gate = n if n_gate is None else n_gate
    if is_significant(gate, ci95, baseline, min_n) and value is not None:
        out = f"{s} (n={n})."
    else:
        out = f"Early signal (n={n}): {s[0].lower()}{s[1:]}."
    if dollars:
        label = ("Left on the table" if metric == "exit_quality"
                 else "Cost" if dollars < 0 else "Gain")
        out += f" {label}: about {'-' if dollars < 0 else '+'}${abs(dollars):,.0f}."
    if rule and value is not None:
        out += f" Suggestion for Nico: {rule}"
    return out.replace("{", "(").replace("}", ")").replace("<", "").replace(">", "")


# ── reads (scope applied here, bounded) ─────────────────────────────────────────

def _scope_sql():
    xs = _excluded()
    if not xs:
        return "", []
    # the DB-side label goes through mason_robinhood.norm_account_label (registered per connection in _fetch;
    # T-D6 R2): NFKC, Cf/fillers dropped, trimmed, casefolded — " Household", "Household\t", "Ａｌｅｘ" are Household
    col = "mason_norm_label(coalesce(account_label,''))"
    return f" AND {col} NOT IN ({','.join('?' * len(xs))})", xs


def _fetch(sql, args):
    conn = mason_db.get_connection()
    try:
        import mason_robinhood as _RH
        conn.create_function("mason_norm_label", 1, _RH.norm_account_label, deterministic=True)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    if len(rows) > MAX_ROWS:
        _truncated()
        rows = rows[:MAX_ROWS]
    return rows


def _norm_trade(r):
    pl = _f(r.get("realized_pl"))
    if pl is None:
        return None
    kind = str(r.get("kind") or "").lower()
    qty, price = _f(r.get("qty")), _f(r.get("price"))
    mult = 100.0 if kind == "option" else 1.0
    cost = entry = None
    if qty and qty > 0 and price is not None and price >= 0:
        cost = qty * price * mult - pl
        entry = cost / (qty * mult) if cost > 0 else None
    hold = _f(r.get("holding_days"))
    return {"id": r.get("id"), "kind": kind,
            "symbol": str(r.get("underlying") or r.get("symbol") or "").upper(),
            "pl": pl, "hold": hold if hold is not None and hold >= 0 else None,
            "qty": qty, "price": price, "cost": cost, "entry_px": entry, "exit_px": price,
            "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at")}


def load_trades() -> list:
    """In-scope, single-leg realized trades (newest first), normalized."""
    if not enabled():
        return []
    where, args = _scope_sql()
    rows = _fetch("SELECT id, kind, symbol, underlying, qty, price, realized_pl, closed_at,"
                  " opened_at, holding_days FROM rh_trades WHERE coalesce(record_deleted,0)=0"
                  " AND coalesce(is_multileg,0)=0" + where +
                  " ORDER BY closed_at DESC LIMIT ?", args + [MAX_ROWS + 1])
    return [t for t in (_norm_trade(r) for r in rows) if t]


def load_buys() -> list:
    """In-scope equity buy orders, DRIP and multi-leg excluded (newest first)."""
    if not enabled():
        return []
    where, args = _scope_sql()
    rows = _fetch("SELECT id, symbol, qty, avg_price, filled_at FROM rh_orders"
                  " WHERE coalesce(record_deleted,0)=0 AND coalesce(is_multileg,0)=0"
                  " AND coalesce(is_drip,0)=0 AND lower(coalesce(side,''))='buy'"
                  " AND lower(coalesce(kind,''))='equity'"
                  " AND (state IS NULL OR lower(state) IN ('filled','partially_filled'))" + where +
                  " ORDER BY filled_at DESC LIMIT ?", args + [MAX_ROWS + 1])
    out = []
    for r in rows:
        qty, px, d = _f(r.get("qty")), _f(r.get("avg_price")), _et_date(r.get("filled_at"))
        sym = str(r.get("symbol") or "").upper().strip()
        if qty and qty > 0 and px and px > 0 and d and sym:
            out.append({"id": r.get("id"), "symbol": sym, "day": d.isoformat(), "px": px,
                        "notional": qty * px})
    return out


def load_reports(symbols) -> dict:
    syms = sorted({s for s in symbols if s})
    out = {}
    for i in range(0, len(syms), 500):
        chunk = syms[i:i + 500]
        rows = _fetch(f"SELECT ticker, report_date, timing FROM earnings_results WHERE ticker IN"
                      f" ({','.join('?' * len(chunk))}) LIMIT ?", chunk + [MAX_ROWS + 1])
        for r in rows:
            d = _et_date(str(r.get("report_date") or "")[:10] + "T12:00:00-04:00")
            if d:
                out.setdefault(str(r["ticker"]).upper(), []).append(
                    (d, str(r.get("timing") or "").lower()))
    return out


def load_regime_priors() -> list:
    """Read-only view of S1-T7's family 'regime' priors, through mason_kb_priors.query() so
    retired rows and personal aggregates built under a different exclude_accounts scope are
    already hidden (S1-T5 A1-D1). Never written from here. priors off / unavailable → []."""
    import mason_kb_priors as KP
    out = []
    for r in KP.query("regime", limit=2000) or []:
        k, st = r.get("key"), r.get("stat")
        if isinstance(k, dict) and isinstance(st, dict):
            out.append({"key": k, "n": r.get("n"), "stat": st})
    return out


# ── price-cache annotation (the only place mason_price_cache is touched) ────────

class _Budget:
    def __init__(self, n):
        self.left = n

    def take(self):
        if self.left <= 0:
            _truncated()
            return False
        self.left -= 1
        return True


def _session_dates(rows):
    """Sorted bar dates with a usable close (dates only — no price math on raw rows)."""
    return sorted({str(r.get("date"))[:10] for r in rows or []
                   if (_f(r.get("close")) or 0) > 0})


def _annotate_buys(buys, budget):
    import mason_price_cache as PC
    memo = {}
    for b in buys:
        key = (b["symbol"], b["day"])
        if key not in memo:
            memo[key] = None
            if budget.take():
                try:
                    # Prior-day move = the total return of the session before the buy, graded by
                    # THE grading function: fwd_return(<two sessions back>, sessions=1). It is
                    # split- and dividend-correct and coherent across fetches (adj_close is only
                    # coherent within one fetch; plain close misreads ex-dividend days).
                    d = date.fromisoformat(b["day"])
                    rows = PC.get_daily(b["symbol"], (d - timedelta(days=14)).isoformat(),
                                        (d - timedelta(days=1)).isoformat())
                    ds = [x for x in _session_dates(rows) if x < b["day"]]
                    if len(ds) >= 2 and budget.take():
                        g = PC.fwd_return(b["symbol"], ds[-2], sessions=1, vs=None)
                        r = _f(g.get("ret")) if isinstance(g, dict) and g.get("matured") else None
                        if r is not None and g.get("exit_date") == ds[-1]:
                            q = 1.0 + r
                            # a raw-bar split looks like a ~k or ~1/k day: not a move, skip it
                            if (0.7 <= q <= 1.43) or _split_ratio(q, tol=0.05) is None:
                                memo[key] = r
                except Exception as e:
                    _err(f"get_daily {b['symbol']}", e)
        b["prior_move"] = memo[key]
        b["fwd"] = {}
        if b["prior_move"] is None:
            continue
        for s in (5, 20):
            if not budget.take():
                break
            try:
                g = PC.fwd_return(b["symbol"], b["day"], sessions=s, entry_px=b["px"])
                if isinstance(g, dict) and g.get("matured") and _f(g.get("ret")) is not None:
                    b["fwd"][s] = _f(g["ret"])
            except Exception as e:
                _err(f"fwd_return {b['symbol']}", e)
    return buys


_SPLIT_KS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)
_SPLIT_RATIOS = tuple(sorted({float(k) for k in _SPLIT_KS} | {1.0 / k for k in _SPLIT_KS}))


def _split_ratio(r, tol=0.06):
    """The split ratio r is closest to (k or 1/k), if within tol (log distance); else None."""
    if not r or r <= 0:
        return None
    best = min(_SPLIT_RATIOS, key=lambda c: abs(math.log(r / c)))
    return best if abs(math.log(r / best)) <= tol else None


def _snap_basis(fill, bar_close):
    """Factor f with fill ≈ f × bar_close: 1.0 when they're on the same share basis (fills can
    sit up to 25% off the day's close), else the nearest split ratio within 10%. None = can't
    tell (the caller skips and counts)."""
    if not fill or not bar_close or fill <= 0 or bar_close <= 0:
        return None
    r = fill / bar_close
    if 0.8 <= r <= 1.25:
        return 1.0
    return _split_ratio(r, tol=math.log(1.1))


def _bars_on_last_basis(rows):
    """Daily bars (date, high, close) restated on the LAST bar's share basis. With S1's
    convention (split-adjusted close/high) this is a no-op; with raw bars a split inside the
    window shows as a close-to-close jump of ~k or ~1/k, and every earlier bar is rescaled."""
    bars = []
    for r in sorted(rows or [], key=lambda x: str(x.get("date"))):
        h, c = _f(r.get("high")), _f(r.get("close"))
        if h and c and h > 0 and c > 0:
            bars.append([str(r.get("date"))[:10], h, c])
    for i in range(len(bars) - 1, 0, -1):
        q = bars[i][2] / bars[i - 1][2]
        k = _split_ratio(q, tol=0.05) if not (0.7 <= q <= 1.43) else None
        if k:
            for b in bars[:i]:
                b[1] *= k
                b[2] *= k
    return bars


def _annotate_mfe(trades, budget):
    """t['mfe_px'] = the max daily high over the hold, on the FILL basis. The entry price is
    derived from the closing row (cost / sold qty), so both fills share the exit-day basis:
    bars are restated on the exit day's basis, then snapped to the fill via exit_px ÷ the
    exit-day close (nearest split ratio). Unknowable basis → t['mfe_skip'] (counted)."""
    import mason_price_cache as PC
    for t in trades:
        o, c = _et_date(t.get("opened_at")), _et_date(t.get("closed_at"))
        if t.get("kind") != "equity" or not o or not c or not t.get("entry_px") or not t["symbol"]:
            continue
        if not budget.take():
            break
        try:
            bars = _bars_on_last_basis(PC.get_daily(t["symbol"], o.isoformat(), c.isoformat()))
            if not bars:
                continue
            if bars[-1][0] != c.isoformat():
                t["mfe_skip"] = "no exit-day bar"
                continue
            f = _snap_basis(t.get("exit_px"), bars[-1][2])
            if f is None:
                t["mfe_skip"] = "fill/bar basis unknown"
                continue
            t["mfe_px"] = max(b[1] for b in bars) * f
        except Exception as e:
            _err(f"get_daily {t['symbol']}", e)
    return trades


# ── compute + write ─────────────────────────────────────────────────────────────

def _finding(metric, res, min_n, prior_keys):
    sig = (is_significant(res["n_gate"], res["ci95"], res["baseline"], min_n)
           and res["value"] is not None)
    return {"metric": metric, "value": res["value"], "baseline": res["baseline"], "n": res["n"],
            "ci95": res["ci95"], "significant": bool(sig), "dollars": res["dollars"],
            "finding": finding_text(metric, res["value"], res["baseline"], res["n"], res["ci95"],
                                    res["dollars"], min_n=min_n, extra=res["extra"],
                                    n_gate=res["n_gate"]),
            "_n_gate": res["n_gate"], "_extra": res["extra"], "_priors": prior_keys}


def _write(findings, now_iso, scope):
    conn = mason_db.get_connection()
    try:
        for f in findings:
            fid = hashlib.sha1(f"behavior|{f['metric']}|{WINDOW}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT OR REPLACE INTO behavior_findings (id, metric, value, baseline, n, ci95_json,"
                " significant, finding, window, computed_date, created_by, record_deleted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (fid, f["metric"], f["value"], f["baseline"], f["n"],
                 json.dumps({"ci95": f["ci95"], "dollars": f["dollars"], "n_gate": f["_n_gate"],
                             "extra": f["_extra"], "scope": scope}), int(f["significant"]),
                 f["finding"], WINDOW, now_iso, CREATED_BY))
        keep = [f["metric"] for f in findings] or [""]
        conn.execute(f"UPDATE behavior_findings SET record_deleted=1 WHERE created_by=? AND window=?"
                     f" AND metric NOT IN ({','.join('?' * len(keep))})", [CREATED_BY, WINDOW] + keep)
        conn.commit()
    finally:
        conn.close()


# str.format templates rendered by mason_kb_priors._render (stat fields + scalar key fields).
# The samples behind each key are listed next to it in compute().
_PRIOR_FMT = {
    "disposition": "Nico held losing trades {mean:.1f}x as long as his median winning trade "
                   "(average across losers, n={n})",
    "weekday": "Nico's trades opened on {weekday} averaged {mean:+.2f} USD realized P&L (n={n})",
    "chase": "Nico's buys after a +5% day returned {mean:+.1%} on average over {sessions} sessions "
             "(median {median:+.1%}, n={n})",
    "dip": "Nico's buys after a -5% day returned {mean:+.1%} on average over {sessions} sessions "
           "(median {median:+.1%}, n={n})",
    "earnings_hold": "Nico's trades held through an earnings report averaged {mean:+.2f} USD "
                     "realized P&L (n={n})",
    "size_drift": "After a losing trade Nico's next position averaged {mean:.2f}x his after-win "
                  "size (n={n})",
    "round_trip": "Nico's trades closed in under 2 days averaged {mean:+.2f} USD realized P&L (n={n})",
    "exit_quality": "Nico's exits captured {median:.0%} of the best move during the hold "
                    "(median, n={n})",
}


def _write_priors(findings, pass_ok=True):
    """Upsert every behavior key, bracketed by begin_run/retire_unrefreshed so keys this pass
    no longer produces (e.g. Household-scoped ones after Household is excluded again) are retired.
    The retire is skipped when the pass had any error, so failed buckets aren't retired."""
    try:
        import mason_kb_priors as KP
    except Exception as e:
        _err("mason_kb_priors import", e)
        return
    run, ok = None, pass_ok
    try:
        run = KP.begin_run("behavior")
    except Exception as e:
        _err("begin_run behavior", e)
        ok = False
    for f in findings:
        for key, samples, refs in f["_priors"]:
            if not samples:
                continue
            fmt = _PRIOR_FMT.get(key.get("metric"), "Nico behavior {metric}: mean {mean:.3g} (n={n})")
            try:
                row = KP.upsert_prior("behavior", key, WINDOW, samples, summary_fmt=fmt, refs=refs)
                if row is None:
                    ok = False
            except Exception as e:
                _err(f"upsert_prior {f['metric']}", e)
                ok = False
    if run and ok:
        try:
            KP.retire_unrefreshed("behavior", run)
        except Exception as e:
            _err("retire_unrefreshed behavior", e)


def compute(*, write: bool = True) -> list:
    """Recompute every metric from in-scope history; upsert behavior_findings + priors.
    Returns the §6 rows. Never raises; flags off → []."""
    if not enabled():
        return []
    with _lock:
        _STATUS.update({"errors": [], "truncated": False})
    try:
        trades = load_trades()
        buys = load_buys()
    except Exception as e:
        _err("load", e)
        return []
    min_n = _min_n()
    budget = _Budget(MAX_PRICE_LOOKUPS)
    out = []

    def add(metric, thunk, prior_key=None):
        try:
            res = thunk()
        except Exception as e:
            _err(metric, e)
            return
        if res and res["n"] > 0:
            pk = [(prior_key, res["samples"], res["refs"])] if prior_key else []
            out.append(_finding(metric, res, min_n, pk))

    add("disposition", lambda: _disposition(trades), {"metric": "disposition"})

    try:
        worst, live = _weekday_worst(trades)
        if worst is not None:
            f = _finding("weekday", worst, min_n,
                         [({"metric": "weekday", "weekday": _DAY[j][:3]}, r["samples"], r["refs"])
                          for j, r in live])
            out.append(f)
    except Exception as e:
        _err("weekday", e)

    try:
        _annotate_buys(buys, budget)
    except Exception as e:
        _err("annotate buys", e)
    for s in (5, 20):
        add(f"chase_{s}", lambda s=s: _move_split(buys, s, lambda m: m >= CHASE_PCT - _EPS),
            {"metric": "chase", "sessions": s})
        add(f"dip_{s}", lambda s=s: _move_split(buys, s, lambda m: m <= -CHASE_PCT + _EPS),
            {"metric": "dip", "sessions": s})

    add("earnings_hold", lambda: _earnings_hold(trades, load_reports(t["symbol"] for t in trades)),
        {"metric": "earnings_hold"})
    add("size_drift", lambda: _size_drift(trades), {"metric": "size_drift"})
    add("round_trip", lambda: _round_trip(trades), {"metric": "round_trip"})
    add("regime_fit", lambda: _regime_fit(load_regime_priors()))        # linked, not duplicated
    try:
        _annotate_mfe(trades, budget)
    except Exception as e:
        _err("annotate mfe", e)
    add("exit_quality", lambda: _exit_quality(trades), {"metric": "exit_quality"})

    out.sort(key=lambda f: METRIC_ORDER.index(f["metric"]))
    if write:
        try:
            _write(out, datetime.now(timezone.utc).isoformat(timespec="seconds"), _excluded())
        except Exception as e:
            _err("write behavior_findings", e)
            return []
        with _lock:
            pass_ok = not _STATUS["errors"]
        _write_priors(out, pass_ok=pass_ok)
    return [{k: v for k, v in f.items() if not k.startswith("_")} for f in out]


def _read_findings() -> tuple:
    """(rows in the §6 shape, newest computed_date, scope_changed).
    Read time follows the CURRENT config: significance and wording are re-derived with the
    current priors.min_n, and rows computed under a different exclude_accounts scope are
    withheld (scope_changed=True) until the next run recomputes them."""
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT metric, value, baseline, n, ci95_json, significant, finding, computed_date"
            " FROM behavior_findings WHERE coalesce(record_deleted,0)=0 AND window=? LIMIT 100",
            (WINDOW,)).fetchall()]
    finally:
        conn.close()
    out, newest, scope_changed = [], None, False
    scope, min_n = _excluded(), _min_n()
    for r in rows:
        try:
            cj = json.loads(r.get("ci95_json") or "{}")
        except (TypeError, ValueError):
            cj = {}
        cj = cj if isinstance(cj, dict) else {"ci95": cj}
        if cj.get("scope") != scope:
            scope_changed = True
            continue
        n = int(r["n"] or 0)
        gate = cj.get("n_gate") if isinstance(cj.get("n_gate"), int) else n
        extra = cj.get("extra") if isinstance(cj.get("extra"), dict) else {}
        val, base, ci, dol = r["value"], r["baseline"], cj.get("ci95"), cj.get("dollars")
        sig = is_significant(gate, ci, base, min_n) and val is not None
        try:
            text = finding_text(r["metric"], val, base, n, ci, dol, min_n=min_n, extra=extra,
                                n_gate=gate)
        except Exception:
            text = r["finding"] or ""
            if not sig and not text.startswith("Early signal"):
                text = f"Early signal (n={n}): {text}"
        out.append({"metric": r["metric"], "finding": text, "value": val, "baseline": base,
                    "n": n, "ci95": ci, "significant": bool(sig), "dollars": dol})
        newest = max(newest or "", r.get("computed_date") or "")
    rank = {m: i for i, m in enumerate(METRIC_ORDER)}
    out.sort(key=lambda f: rank.get(f["metric"], 99))
    return out, newest, scope_changed


def findings() -> list:
    """Stored findings in the §6 shape. Flags off or any error → []."""
    if not enabled():
        return []
    try:
        return _read_findings()[0]
    except Exception as e:
        _err("read findings", e)
        return []


# ── jobs / status / API ─────────────────────────────────────────────────────────

def run() -> dict:
    """Nightly job body (scheduled by S2-T8)."""
    if not enabled():
        return {"enabled": False}
    n = len(compute())
    with _lock:
        _STATUS["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"ok": True, "findings": n}


def jobs() -> list:
    if not enabled():
        return []
    return [{"name": "rhx_behavior_nightly", "at_et": "17:15", "fn": run}]


def status() -> dict:
    on = enabled()
    rows = 0
    if on:
        try:
            rows = len(_read_findings()[0])
        except Exception as e:
            _err("status", e)
    with _lock:
        return {"name": "behavior", "enabled": on, "last_run": _STATUS["last_run"], "rows": rows,
                "errors": list(_STATUS["errors"]), "truncated": bool(_STATUS["truncated"]),
                "rate_hits": 0, "xcheck_mismatch": 0}


bp = Blueprint("rh_behavior", __name__)


@bp.route("/api/rh/behavior", methods=["GET"])
def api_behavior():
    if not enabled():
        return jsonify({"enabled": False})
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        data, newest, scope_changed = _read_findings()
        stale, errors = False, []
        if newest:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(newest)
            stale = age > timedelta(hours=STALE_AFTER_H)
        if scope_changed:
            stale = True
            errors.append("account scope changed since the last run; findings return after the "
                          "next nightly run")
        return jsonify({"ok": True, "as_of": as_of, "data": data, "stale": stale,
                        "errors": errors})
    except Exception as e:
        _err("api", e)
        return jsonify({"ok": True, "as_of": as_of, "data": [], "stale": True,
                        "errors": [f"behavior findings unavailable: {type(e).__name__}"]})
