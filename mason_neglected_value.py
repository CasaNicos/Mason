"""
mason_neglected_value.py — Mason's "undervalued + underreported" screener.

This is the strategy nothing else in the stack covers: the *neglected-firm effect*.
We hunt small/mid-cap names that are simultaneously

    CHEAP      — low P/E and P/B (the "undervalued" half)
    NEGLECTED  — few analysts covering, thin institutional ownership
                 (the "underreported" half — where un-crowded alpha hides)
    AWAKENING  — institutional ownership ticking UP quarter-over-quarter
                 (early smart money discovering it before the crowd)
    NOT JUNK   — positive/improving revenue & earnings, real ROE, sane debt
                 (a quality gate so we screen out value *traps*)

Each name gets a 0-100 composite from four sub-scores (value / neglect / flow /
quality). We rank, drop obvious traps, and emit pitch-ready candidates.

Data source: Financial Modeling Prep (key from mason.config.json, same as
mason_skills). Everything degrades gracefully — no key, no network, or a bad
response returns the last cached report (or an empty list) and never raises.

Because this leans on small, illiquid names and is slow-moving by nature, the
intended cadence is WEEKLY, not every brief. Per-symbol enrichment is capped
(max_enrich) to protect the FMP quota.

Public API:
    run(max_enrich=20, force=False)      -> {"ok":bool, "data":..., "cached":bool, "error":...}
    candidates(limit=5)                  -> [normalized dicts for mason_ideas]
    render(result=None)                  -> str
    last_cached()                        -> dict | None
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except Exception:                       # requests should exist, but never hard-fail
    requests = None

BASE       = Path(__file__).parent
REPORTS    = BASE / "skill_reports" / "neglected_value"
CACHE_FILE = BASE / "neglected_value_cache.json"
FMP_BASE   = "https://financialmodelingprep.com"
DEFAULT_TTL = 6 * 3600                   # 6h; cadence is weekly but cache is cheap

# Sub-score weights (sum = 1.0). Value + neglect carry the thesis; quality gates
# out traps; flow is the timing kicker.
W_VALUE, W_NEGLECT, W_FLOW, W_QUALITY = 0.30, 0.30, 0.15, 0.25

# Universe prefilter (where neglect actually lives — small/micro, liquid enough)
UNIVERSE_PARAMS = {
    "marketCapMoreThan":  50_000_000,
    "marketCapLowerThan": 2_000_000_000,
    "priceMoreThan":      3,
    "volumeMoreThan":     75_000,
    "exchange":           "NYSE,NASDAQ,AMEX",
    "country":            "US",
    "isActivelyTrading":  "true",
    "isEtf":              "false",
    "isFund":             "false",
    "limit":              400,
}


# ── config / http ────────────────────────────────────────────────────────────────

def _cfg():
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmp_key():
    # B1b: env FMP_API_KEY > secrets file > config. NOTE (RULINGS #24.2): this used to be
    # config-first-then-env; it is env-first now, on purpose.
    import mason_secrets as _S
    return _S.get("FMP_API_KEY", "", cfg=_cfg())


def _fmp_get(path, params=None, timeout=12):
    """GET an FMP endpoint → parsed JSON, or None on any failure."""
    if requests is None:
        return None
    key = _fmp_key()
    if not key:
        return None
    p = dict(params or {})
    p["apikey"] = key
    try:
        r = requests.get(FMP_BASE + path, params=p, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ── disk cache (so a weekly run is instantly available to the brief) ──────────────

def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(d):
    try:
        CACHE_FILE.write_text(json.dumps(d, default=str), encoding="utf-8")
    except Exception:
        pass


def last_cached():
    return _load_cache().get("data")


def _cache_fresh(ttl):
    c = _load_cache()
    if c and (time.time() - c.get("ts", 0)) < ttl:
        return c.get("data")
    return None


def _cache_put(data):
    _save_cache({"ts": time.time(), "data": data})


# ── normalizers ──────────────────────────────────────────────────────────────────

def _band(x, lo, hi, invert=False):
    """Map x within [lo,hi] to 0..1 (clamped). invert=True → lower x scores higher."""
    if x is None:
        return None
    try:
        x = float(x)
    except Exception:
        return None
    if hi == lo:
        return 0.5
    t = (x - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    return 1.0 - t if invert else t


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# ── FMP enrichment (per symbol; guarded, each piece optional) ─────────────────────

def _ratios(sym):
    d = _fmp_get(f"/api/v3/ratios-ttm/{sym}")
    if isinstance(d, list) and d:
        d = d[0]
    if not isinstance(d, dict):
        return {}
    return {
        "pe":  d.get("priceEarningsRatioTTM"),
        "pb":  d.get("priceToBookRatioTTM"),
        "ev_ebitda": d.get("enterpriseValueMultipleTTM"),
        "roe": d.get("returnOnEquityTTM"),
        "de":  d.get("debtEquityRatioTTM"),
        "current_ratio": d.get("currentRatioTTM"),
    }


def _analyst_count(sym):
    """How many analysts cover it — the 'underreported' core signal. Fewer = more neglected."""
    d = _fmp_get(f"/api/v3/analyst-estimates/{sym}", {"limit": 1})
    if isinstance(d, list) and d:
        row = d[0]
        for k in ("numberAnalystEstimatedRevenue", "numberAnalystsEstimatedRevenue",
                  "numberAnalystEstimatedEps", "numberAnalystsEstimatedEps"):
            v = row.get(k)
            if v:
                return int(v)
        return 0
    return None


def _institutional(sym):
    """Institutional ownership level + QoQ trend (rising = early discovery)."""
    d = _fmp_get("/api/v4/institutional-ownership/symbol-ownership",
                 {"symbol": sym, "includeCurrentQuarter": "true"})
    if not (isinstance(d, list) and d):
        return {"inst_pct": None, "inst_trend": None, "investors": None}
    cur = d[0]
    pct = cur.get("ownershipPercent")
    investors = cur.get("investorsHolding")
    trend = None
    if len(d) > 1:
        prev = d[1]
        pp = prev.get("ownershipPercent")
        if pct is not None and pp is not None:
            trend = float(pct) - float(pp)           # +ve = institutions adding
        elif investors is not None and prev.get("investorsHolding") is not None:
            trend = float(investors) - float(prev.get("investorsHolding"))
    return {"inst_pct": pct, "inst_trend": trend, "investors": investors}


def _growth(sym):
    """Revenue / EPS growth — the quality/improvement gate (avoid value traps)."""
    d = _fmp_get(f"/api/v3/financial-growth/{sym}", {"limit": 1})
    if isinstance(d, list) and d:
        row = d[0]
        return {"rev_growth": row.get("revenueGrowth"),
                "eps_growth": row.get("epsgrowth") or row.get("epsGrowth")}
    return {"rev_growth": None, "eps_growth": None}


def _enrich(sym):
    e = {}
    e.update(_ratios(sym))
    e["analysts"] = _analyst_count(sym)
    e.update(_institutional(sym))
    e.update(_growth(sym))
    return e


# ── scoring ──────────────────────────────────────────────────────────────────────

def score_candidate(base, enr):
    """Return composite 0-100 + sub-scores + flags + thesis for one name."""
    pe, pb = enr.get("pe"), enr.get("pb")
    analysts = enr.get("analysts")
    inst_pct, inst_trend = enr.get("inst_pct"), enr.get("inst_trend")
    roe = enr.get("roe")
    de = enr.get("de")
    rev_g, eps_g = enr.get("rev_growth"), enr.get("eps_growth")

    flags = []

    # VALUE — cheaper is better. Negative P/E = no earnings → not "cheap", it's a risk.
    pe_s = _band(pe, 6, 25, invert=True) if (pe is not None and pe > 0) else (0.15 if pe is not None else None)
    pb_s = _band(pb, 0.6, 4.0, invert=True) if (pb is not None and pb > 0) else None
    value = _avg([pe_s, pb_s])
    if pe is not None and pe <= 0:
        flags.append("no-earnings")

    # NEGLECT — few analysts + thin institutional ownership. Sweet spot, not zero.
    if analysts is None:
        an_s = 0.5
    elif analysts == 0:
        an_s, _ = 0.6, flags.append("uncovered")     # genuinely off-radar; noisy though
    elif analysts <= 4:
        an_s = 1.0                                    # the neglected sweet spot
    elif analysts <= 8:
        an_s = 0.7
    elif analysts <= 15:
        an_s = 0.4
    else:
        an_s = 0.1                                    # crowded / well-covered
    inst_s = _band(inst_pct, 0.05, 0.55, invert=True) if inst_pct is not None else 0.5
    if inst_pct is not None and inst_pct < 0.02:
        flags.append("very-thin-inst")               # may be hard to trade
    neglect = _avg([an_s, inst_s])

    # FLOW — institutions adding QoQ = early discovery (the timing kicker).
    if inst_trend is None:
        flow = 0.5
    elif inst_trend > 0:
        flow = min(1.0, 0.6 + inst_trend * 5)         # rising ownership
    else:
        flow = max(0.0, 0.4 + inst_trend * 5)         # bleeding ownership
        if inst_trend < 0:
            flags.append("inst-selling")

    # QUALITY — gate out traps: want positive ROE, growth, manageable debt.
    roe_s = _band(roe, 0.0, 0.20) if roe is not None else None
    rev_s = _band(rev_g, -0.05, 0.20) if rev_g is not None else None
    eps_s = _band(eps_g, -0.05, 0.25) if eps_g is not None else None
    de_s  = _band(de, 0.0, 2.0, invert=True) if de is not None else None
    quality = _avg([roe_s, rev_s, eps_s, de_s])
    trap = ((roe is not None and roe < 0) and (rev_g is not None and rev_g < 0))
    if trap:
        flags.append("value-trap-risk")

    # Composite — fall back to neutral 0.5 for any missing pillar so one gap doesn't
    # zero out a name. Apply a trap penalty.
    parts = [(value, W_VALUE), (neglect, W_NEGLECT), (flow, W_FLOW), (quality, W_QUALITY)]
    composite = sum((p if p is not None else 0.5) * w for p, w in parts)
    if trap:
        composite *= 0.6
    composite = round(composite * 100, 1)

    thesis = _thesis(base, enr, value, neglect, flow, quality, flags)
    return {
        "ticker":   base.get("symbol"),
        "name":     base.get("companyName"),
        "sector":   base.get("sector"),
        "price":    base.get("price"),
        "marketCap": base.get("marketCap"),
        "composite_score": composite,
        "sub": {
            "value":   round((value or 0) * 100),
            "neglect": round((neglect or 0) * 100),
            "flow":    round((flow or 0) * 100),
            "quality": round((quality or 0) * 100),
        },
        "metrics": {"pe": pe, "pb": pb, "analysts": analysts,
                    "inst_pct": inst_pct, "inst_trend": inst_trend,
                    "roe": roe, "rev_growth": rev_g, "eps_growth": eps_g, "de": de},
        "flags":  flags,
        "thesis": thesis,
        "source": "neglected-value",
    }


def _pct(x):
    return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "n/a"


def _thesis(base, enr, value, neglect, flow, quality, flags):
    sym = base.get("symbol")
    pe, pb, an = enr.get("pe"), enr.get("pb"), enr.get("analysts")
    bits = []
    if pe and pe > 0:
        bits.append(f"P/E {pe:.1f}")
    if pb and pb > 0:
        bits.append(f"P/B {pb:.1f}")
    cov = (f"{an} analyst{'s' if an != 1 else ''}" if an is not None else "thin coverage")
    bits.append(cov)
    if enr.get("inst_trend") is not None:
        bits.append("inst " + ("adding" if enr["inst_trend"] > 0 else "trimming"))
    if enr.get("rev_growth") is not None:
        bits.append(f"rev {_pct(enr['rev_growth'])}")
    head = f"{sym}: cheap + under-followed ({', '.join(bits)})."
    if "value-trap-risk" in flags:
        head += " ⚠ shrinking & unprofitable — trap risk, verify before sizing."
    elif quality and quality > 0.55 and flow and flow >= 0.6:
        head += " Quality holding up and smart money starting to nibble."
    return head


# ── run ──────────────────────────────────────────────────────────────────────────

def fetch_universe(params=None):
    p = dict(UNIVERSE_PARAMS)
    if params:
        p.update(params)
    d = _fmp_get("/api/v3/stock-screener", p)
    return d if isinstance(d, list) else []


def run(max_enrich=20, force=False, ttl=DEFAULT_TTL, universe_params=None, _enrich_fn=None):
    """Screen for neglected-value names. Returns mason_skills-style dict."""
    if not force:
        fresh = _cache_fresh(ttl)
        if fresh is not None:
            return {"ok": True, "data": fresh, "cached": True}

    if not _fmp_key():
        stale = last_cached()
        return {"ok": bool(stale), "data": stale, "cached": True,
                "error": "no FMP key — returning last cached" if stale else "no FMP key"}

    uni = fetch_universe(universe_params)
    if not uni:
        stale = last_cached()
        return {"ok": bool(stale), "data": stale, "cached": True,
                "error": "universe fetch failed (network/quota) — returning last cached"
                         if stale else "universe fetch failed"}

    # Prefilter cheaply BEFORE spending per-symbol calls: prefer smaller caps with a
    # dividend or low price (where neglect concentrates), keep it liquid.
    uni = [u for u in uni if u.get("symbol") and (u.get("price") or 0) >= 3]
    uni.sort(key=lambda u: (u.get("marketCap") or 9e18))      # smallest first = most neglected
    short = uni[:max_enrich]

    enrich = _enrich_fn or _enrich
    scored = []
    for u in short:
        try:
            e = enrich(u["symbol"])
        except Exception:
            e = {}
        scored.append(score_candidate(u, e))
        time.sleep(0.05)                                       # be polite to the API

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    data = {
        "results":       scored,
        "universe_size": len(uni),
        "enriched":      len(short),
        "generated_at":  datetime.now().isoformat(),
        "params":        {**UNIVERSE_PARAMS, **(universe_params or {}), "max_enrich": max_enrich},
    }
    _cache_put(data)
    _write_report(data)
    return {"ok": True, "data": data, "cached": False}


def _write_report(data):
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (REPORTS / f"neglected_value_{stamp}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


# ── integration helpers (for mason_ideas / the brief) ────────────────────────────

def candidates(limit=5, min_score=55, exclude_traps=True):
    """Normalized candidates ready for mason_ideas pitch-building."""
    r = run()
    if not (r.get("ok") and r.get("data")):
        return []
    out = []
    for c in r["data"].get("results", []):
        if c["composite_score"] < min_score:
            continue
        if exclude_traps and "value-trap-risk" in c.get("flags", []):
            continue
        price = c.get("price")
        out.append({
            "ticker":  c["ticker"],
            "setup":   "NEGLECTED-VALUE",
            "score":   c["composite_score"],
            "state":   "neglected-value",
            "entry":   price,
            "pivot":   None,
            # Patient value sleeve → wider stop than a breakout (give the thesis room).
            "stop":    round(price * 0.85, 2) if price else None,
            "guidance": c["thesis"],
            "valid":   "value-trap-risk" not in c.get("flags", []),
            "source":  "neglected-value",
        })
        if len(out) >= limit:
            break
    return out


def render(result=None):
    r = result or run()
    if not (r.get("ok") and r.get("data")):
        return f"Neglected-value screen unavailable ({r.get('error', 'unknown')})."
    d = r["data"]
    res = d.get("results", [])
    L = [f"Neglected-Value screen — {len(res)} scored from {d.get('universe_size','?')} "
         f"({'cached' if r.get('cached') else 'fresh'})"]
    for i, c in enumerate(res[:8], 1):
        s = c["sub"]
        flag = (" ⚠" + "/".join(c["flags"])) if c.get("flags") else ""
        L.append(f"{i}. {c['ticker']} {c['composite_score']}/100 "
                 f"[V{s['value']} N{s['neglect']} F{s['flow']} Q{s['quality']}]{flag}")
        L.append(f"   {c['thesis']}")
    return "\n".join(L)


# ── self-test (offline; mocks FMP enrichment so logic is verifiable in the sandbox)─

def _self_test():
    universe = [
        {"symbol": "GEM",  "companyName": "Hidden Gem Co",  "sector": "Industrials",
         "price": 12.0, "marketCap": 300_000_000},
        {"symbol": "CRWD", "companyName": "Crowded Pricey", "sector": "Tech",
         "price": 40.0, "marketCap": 1_800_000_000},
        {"symbol": "TRAP", "companyName": "Value Trap Inc", "sector": "Energy",
         "price": 5.0,  "marketCap": 120_000_000},
    ]
    mock = {
        # cheap, 3 analysts, thin+rising inst, profitable & growing → should WIN
        "GEM":  {"pe": 9.0, "pb": 1.1, "analysts": 3, "inst_pct": 0.18, "inst_trend": 0.03,
                 "roe": 0.16, "rev_growth": 0.14, "eps_growth": 0.20, "de": 0.5},
        # expensive, 22 analysts, heavy inst → neglect/value low → should LOSE
        "CRWD": {"pe": 60.0, "pb": 12.0, "analysts": 22, "inst_pct": 0.85, "inst_trend": -0.01,
                 "roe": 0.22, "rev_growth": 0.30, "eps_growth": 0.25, "de": 0.3},
        # cheap but shrinking & unprofitable → trap penalty → should be flagged/last
        "TRAP": {"pe": -4.0, "pb": 0.7, "analysts": 1, "inst_pct": 0.06, "inst_trend": -0.04,
                 "roe": -0.12, "rev_growth": -0.18, "eps_growth": -0.40, "de": 2.4},
    }
    scored = sorted((score_candidate(u, mock[u["symbol"]]) for u in universe),
                    key=lambda x: x["composite_score"], reverse=True)
    order = [c["ticker"] for c in scored]
    assert order[0] == "GEM", f"expected GEM first, got {order}"
    assert order[-1] == "TRAP", f"expected TRAP last, got {order}"
    assert "value-trap-risk" in dict((c["ticker"], c["flags"]) for c in scored)["TRAP"]
    assert all(0 <= c["composite_score"] <= 100 for c in scored)
    # candidates() normalization via injected universe/enrich
    print("SELF-TEST OK")
    for c in scored:
        print(f"  {c['ticker']:5} {c['composite_score']:5}/100  "
              f"V{c['sub']['value']} N{c['sub']['neglect']} F{c['sub']['flow']} "
              f"Q{c['sub']['quality']}  flags={c['flags']}")
    return scored


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "selftest", "render"])
    ap.add_argument("--max-enrich", type=int, default=20)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    elif a.cmd == "render":
        print(render())
    else:
        out = run(max_enrich=a.max_enrich, force=a.force)
        print(json.dumps({k: (v if k != "data" else f"<{len((v or {}).get('results',[]))} results>")
                          for k, v in out.items()}, indent=2, default=str))
