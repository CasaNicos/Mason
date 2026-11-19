"""
mason_market_posture.py
=======================
No-API-key market posture engine for Mason.
Uses yfinance to calculate:
  - Sector rotation (cyclical vs defensive leadership)
  - Market top risk score (0-100) via distribution-day proxy,
    leading-stock health, and defensive rotation signals

Output (stdout JSON):
{
  "sector": {
    "regime": "Cyclical" | "Defensive" | "Neutral",
    "regime_score": 0-100,
    "leaders": ["XLK", ...],
    "laggards": ["XLU", ...],
    "cycle_phase": "Mid Cycle Expansion",
    "details": { "XLK": {"1w": 2.1, "1m": 5.3}, ... }
  },
  "top_risk": {
    "score": 0-100,
    "zone": "Green" | "Yellow" | "Orange" | "Red" | "Critical",
    "zone_color": "#3ddc84" | ...,
    "components": {
      "distribution_days": {"score": 0-25, "value": 3, "label": "3 dist. days"},
      "leading_health":    {"score": 0-20, "value": -1.2, "label": "Growth ETFs -1.2% vs SPY"},
      "defensive_rotation":{"score": 0-15, "value": 0.8, "label": "Defensives +0.8% vs SPY"},
      "breadth":           {"score": 0-15, "value": 61.2, "label": "61% above 50DMA"},
      "index_technical":   {"score": 0-15, "value": "above_50ma", "label": "SPY above 50MA"},
      "sentiment":         {"score": 0-10, "value": 18.2, "label": "VIX 18.2"}
    },
    "summary": "Low risk. Market acting well."
  },
  "posture_line": "Cyclical leading | Risk: Low (18/100)",
  "as_of": "2026-06-17"
}
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    print(json.dumps({"error": "yfinance not installed"}))
    sys.exit(1)

# ── Sector ETFs ────────────────────────────────────────────────
CYCLICAL_SECTORS = {
    "XLK": "Technology",
    "XLY": "Consumer Disc.",
    "XLI": "Industrials",
    "XLF": "Financials",
    "XLC": "Comm. Services",
    "XLE": "Energy",
    "XLB": "Materials",
}
DEFENSIVE_SECTORS = {
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLV": "Health Care",
    "VNQ": "Real Estate",
}
ALL_SECTORS = {**CYCLICAL_SECTORS, **DEFENSIVE_SECTORS}

# ── Leading growth ETFs (health proxy) ────────────────────────
GROWTH_ETFS = ["ARKK", "IGV", "SOXX", "SMH", "XBI"]
DEFENSIVE_ETFS = ["XLU", "XLP", "XLV"]
BENCHMARK = "SPY"
VIX_TICKER = "^VIX"


def _pct_change(hist, days):
    """Return % change over last `days` trading days."""
    if hist is None or len(hist) < days + 1:
        return None
    close = hist["Close"].dropna()
    if len(close) < days + 1:
        return None
    return float((close.iloc[-1] / close.iloc[-(days + 1)] - 1) * 100)


def _fetch(tickers, period="3mo"):
    """Batch download. Returns dict ticker -> DataFrame with a guaranteed 'Close' column."""
    if isinstance(tickers, str):
        tickers = [tickers]
    import mason_symbols
    ysym = mason_symbols.yf_symbols(tickers)      # Yahoo spelling (BRK.B → BRK-B); results keyed by ours (R2-A)
    try:
        raw = yf.download(
            list(dict.fromkeys(ysym.values())), period=period, progress=False, auto_adjust=True
        )
    except Exception:
        return {}

    result = {}
    if raw is None or raw.empty:
        return result

    def _normalise(df):
        """Ensure the frame has a 'Close' column regardless of yfinance column naming."""
        if "Close" in df.columns:
            return df
        if "Adj Close" in df.columns:
            df = df.copy()
            df["Close"] = df["Adj Close"]
            return df
        return df

    if hasattr(raw.columns, "levels") and len(raw.columns.levels) > 1:
        # Multi-ticker: MultiIndex (field, ticker)
        for t in tickers:
            ys = ysym[t]
            try:
                sub = raw.xs(ys, axis=1, level=1)
                result[t] = _normalise(sub)
            except KeyError:
                # Try level=0 slice as fallback
                try:
                    sub = raw[ys] if ys in raw.columns else None
                    if sub is not None:
                        result[t] = _normalise(sub)
                except Exception:
                    pass
    else:
        # Single ticker — flat columns
        result[tickers[0]] = _normalise(raw)

    return result


# ── SECTOR ROTATION ───────────────────────────────────────────

def analyze_sectors():
    tickers = list(ALL_SECTORS.keys()) + [BENCHMARK]
    data = _fetch(tickers)

    spy_1w = _pct_change(data.get(BENCHMARK), 5)
    spy_1m = _pct_change(data.get(BENCHMARK), 21)

    details = {}
    for ticker in ALL_SECTORS:
        hist = data.get(ticker)
        w = _pct_change(hist, 5)
        m = _pct_change(hist, 21)
        details[ticker] = {
            "name": ALL_SECTORS[ticker],
            "1w": round(w, 2) if w is not None else None,
            "1m": round(m, 2) if m is not None else None,
            "1w_vs_spy": round(w - spy_1w, 2) if (w and spy_1w) else None,
            "1m_vs_spy": round(m - spy_1m, 2) if (m and spy_1m) else None,
            "type": "cyclical" if ticker in CYCLICAL_SECTORS else "defensive",
        }

    # Score: avg relative perf cyclical vs defensive (1m weighted 60%, 1w 40%)
    cyc_scores = [
        (d["1w_vs_spy"] or 0) * 0.4 + (d["1m_vs_spy"] or 0) * 0.6
        for t, d in details.items() if d["type"] == "cyclical" and d["1w_vs_spy"] is not None
    ]
    def_scores = [
        (d["1w_vs_spy"] or 0) * 0.4 + (d["1m_vs_spy"] or 0) * 0.6
        for t, d in details.items() if d["type"] == "defensive" and d["1w_vs_spy"] is not None
    ]

    cyc_avg = sum(cyc_scores) / len(cyc_scores) if cyc_scores else 0
    def_avg = sum(def_scores) / len(def_scores) if def_scores else 0
    spread = cyc_avg - def_avg  # positive = cyclical leading

    # Regime score: 0=full defensive, 50=neutral, 100=full cyclical
    regime_score = min(100, max(0, int(50 + spread * 10)))

    if spread > 0.3:
        regime = "Cyclical"
    elif spread < -0.3:
        regime = "Defensive"
    else:
        regime = "Neutral"

    # Cycle phase heuristic
    if regime == "Cyclical" and regime_score > 65:
        phase = "Mid Cycle Expansion"
    elif regime == "Cyclical" and regime_score > 55:
        phase = "Early-Mid Cycle"
    elif regime == "Defensive" and regime_score < 35:
        phase = "Late Cycle / Risk-Off"
    elif regime == "Defensive":
        phase = "Late Cycle"
    else:
        phase = "Transitional"

    # Top 3 leaders and laggards by 1m vs SPY
    ranked = sorted(
        [(t, d) for t, d in details.items() if d["1m_vs_spy"] is not None],
        key=lambda x: x[1]["1m_vs_spy"], reverse=True
    )
    leaders  = [t for t, _ in ranked[:3]]
    laggards = [t for t, _ in ranked[-3:]]

    return {
        "regime": regime,
        "regime_score": regime_score,
        "cyclical_avg_vs_spy": round(cyc_avg, 2),
        "defensive_avg_vs_spy": round(def_avg, 2),
        "leaders": leaders,
        "laggards": laggards,
        "cycle_phase": phase,
        "details": details,
    }


# ── MARKET TOP RISK ───────────────────────────────────────────

def analyze_top_risk():
    tickers = [BENCHMARK, "QQQ", VIX_TICKER] + GROWTH_ETFS + DEFENSIVE_ETFS
    data = _fetch(tickers, period="3mo")

    components = {}
    total = 0

    # 1. Distribution Day proxy (25 pts max)
    # Count days in last 25 sessions where SPY closed down ≥0.2% on above-avg volume
    spy = data.get(BENCHMARK)
    dist_days = 0
    if spy is not None and len(spy) >= 26:
        recent = spy.tail(26)
        avg_vol = recent["Volume"].mean()
        for i in range(1, len(recent)):
            pct = (recent["Close"].iloc[i] / recent["Close"].iloc[i-1] - 1) * 100
            vol = recent["Volume"].iloc[i]
            if pct <= -0.2 and vol > avg_vol * 0.9:
                dist_days += 1
    dd_score = min(25, dist_days * 4)
    total += dd_score
    components["distribution_days"] = {
        "score": dd_score, "max": 25,
        "value": dist_days,
        "label": f"{dist_days} dist. days (last 25 sessions)"
    }

    # 2. Leading stock health (20 pts max)
    spy_1m = _pct_change(spy, 21)
    growth_perfs = []
    for t in GROWTH_ETFS:
        p = _pct_change(data.get(t), 21)
        if p is not None and spy_1m is not None:
            growth_perfs.append(p - spy_1m)
    growth_avg = sum(growth_perfs) / len(growth_perfs) if growth_perfs else 0
    # Negative = growth lagging = bearish
    if growth_avg < -5:   ls_score = 20
    elif growth_avg < -2: ls_score = 14
    elif growth_avg < 0:  ls_score = 8
    elif growth_avg < 2:  ls_score = 3
    else:                 ls_score = 0
    total += ls_score
    components["leading_health"] = {
        "score": ls_score, "max": 20,
        "value": round(growth_avg, 1),
        "label": f"Growth ETFs {growth_avg:+.1f}% vs SPY (1m)"
    }

    # 3. Defensive rotation (15 pts max)
    def_perfs = []
    for t in DEFENSIVE_ETFS:
        p = _pct_change(data.get(t), 21)
        if p is not None and spy_1m is not None:
            def_perfs.append(p - spy_1m)
    def_avg = sum(def_perfs) / len(def_perfs) if def_perfs else 0
    # Positive = defensives outperforming = bearish
    if def_avg > 3:    dr_score = 15
    elif def_avg > 1:  dr_score = 10
    elif def_avg > 0:  dr_score = 5
    else:              dr_score = 0
    total += dr_score
    components["defensive_rotation"] = {
        "score": dr_score, "max": 15,
        "value": round(def_avg, 1),
        "label": f"Defensives {def_avg:+.1f}% vs SPY (1m)"
    }

    # 4. Breadth proxy via sector breadth (15 pts max)
    # Use % of sector ETFs trading above their own 50-day MA
    sector_tickers = list(ALL_SECTORS.keys())
    sector_data = _fetch(sector_tickers)
    above_50 = 0
    checked = 0
    for t, hist in sector_data.items():
        if hist is not None and len(hist) >= 50:
            close = hist["Close"].dropna()
            if len(close) >= 50:
                ma50 = close.tail(50).mean()
                if float(close.iloc[-1]) > float(ma50):
                    above_50 += 1
                checked += 1
    breadth_pct = (above_50 / checked * 100) if checked else 50
    if breadth_pct < 40:   br_score = 15
    elif breadth_pct < 55: br_score = 8
    elif breadth_pct < 70: br_score = 3
    else:                  br_score = 0
    total += br_score
    components["breadth"] = {
        "score": br_score, "max": 15,
        "value": round(breadth_pct, 1),
        "label": f"{breadth_pct:.0f}% of sector ETFs above 50MA"
    }

    # 5. Index technical (15 pts max) — SPY above/below 50MA and 200MA
    it_score = 0
    if spy is not None and len(spy) >= 200:
        close = spy["Close"].dropna()
        last  = float(close.iloc[-1])
        ma50  = float(close.tail(50).mean())
        ma200 = float(close.tail(200).mean())
        if last < ma200:   it_score = 15
        elif last < ma50:  it_score = 10
        else:              it_score = 0
        tech_label = (
            f"SPY {'below' if last < ma50 else 'above'} 50MA, "
            f"{'below' if last < ma200 else 'above'} 200MA"
        )
    else:
        tech_label = "Insufficient data"
    total += it_score
    components["index_technical"] = {
        "score": it_score, "max": 15,
        "value": tech_label,
        "label": tech_label
    }

    # 6. VIX sentiment (10 pts max)
    vix_hist = data.get(VIX_TICKER)
    vix_val  = None
    if vix_hist is not None and len(vix_hist) > 0:
        vix_val = float(vix_hist["Close"].dropna().iloc[-1])
    if vix_val:
        if vix_val > 30:   vx_score = 10
        elif vix_val > 22: vx_score = 6
        elif vix_val > 18: vx_score = 3
        else:              vx_score = 0
        vix_label = f"VIX {vix_val:.1f}"
    else:
        vx_score  = 0
        vix_label = "VIX unavailable"
    total += vx_score
    components["sentiment"] = {
        "score": vx_score, "max": 10,
        "value": round(vix_val, 1) if vix_val else None,
        "label": vix_label
    }

    # Zone classification
    if total <= 20:
        zone, color, summary = "Green",    "#3ddc84", "Low risk. Market acting well."
    elif total <= 40:
        zone, color, summary = "Yellow",   "#f5c842", "Early warning. Tighten stops."
    elif total <= 60:
        zone, color, summary = "Orange",   "#ff9944", "Elevated risk. Consider profit-taking."
    elif total <= 80:
        zone, color, summary = "Red",      "#ff5566", "High probability top forming."
    else:
        zone, color, summary = "Critical", "#ff2244", "Maximum defense. Hedge aggressively."

    return {
        "score": total,
        "zone": zone,
        "zone_color": color,
        "summary": summary,
        "components": components,
    }


# ── BUFFETT INDICATOR ─────────────────────────────────────────

_BASE       = os.path.dirname(os.path.abspath(__file__))
_GDP_CACHE  = os.path.join(_BASE, ".gdp_cache.json")
# Last-known-good quarterly US nominal GDP ($B). ONLY used if both the live fetch
# and the on-disk cache are unavailable, so the Buffett Indicator still renders
# instead of erroring out entirely. GDP is quarterly and barely moves, so a stale
# value here is harmless — refresh it a few times a year. (FRED series GDP.)
_GDP_FALLBACK = {"gdp_billions": 32486.066, "gdp_date": "2026-04-01"}


def _fetch_gdp():
    """Return (gdp_billions, gdp_date, source) where source is 'live' | 'cache' |
    'fallback'. Robust by design: FRED sits behind an Akamai edge that Python's
    urllib/requests stall on (they read-timeout), but curl negotiates it fine — so
    we curl the public CSV, cache the result, and degrade to the on-disk cache and
    then a hardcoded constant. GDP is quarterly, so a slightly stale value must
    never break the whole indicator (the old urllib fetch did exactly that)."""
    # 1. live via curl. Two things matter here, both learned the hard way:
    #  - Use curl, not Python's urllib/requests: FRED's Akamai edge read-times-out
    #    Python's HTTP clients but serves curl fine.
    #  - Do NOT set a custom User-Agent. Akamai's bot management RESETS the HTTP/2
    #    stream (curl error 92) for any UA except curl's own default — a "Mason/1.0"
    #    or a browser UA both fail; the bare "curl/x.y" default succeeds. So we pass
    #    no -A flag on purpose.
    try:
        out = subprocess.run(
            ["curl", "-sS", "-m", "12",
             "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDP"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        gdp_billions = gdp_date = None
        for line in reversed(out.strip().splitlines()):
            parts = line.split(",")
            if len(parts) == 2 and parts[1].strip() not in ("", "."):
                try:
                    gdp_billions = float(parts[1].strip())
                    gdp_date = parts[0].strip()
                    break
                except ValueError:
                    continue   # header row (e.g. "observation_date,GDP")
        if gdp_billions:
            try:
                with open(_GDP_CACHE, "w") as f:
                    json.dump({"gdp_billions": gdp_billions, "gdp_date": gdp_date}, f)
            except Exception:
                pass
            return gdp_billions, gdp_date, "live"
    except Exception:
        pass
    # 2. on-disk cache (last good fetch)
    try:
        with open(_GDP_CACHE) as f:
            c = json.load(f)
        if c.get("gdp_billions"):
            return float(c["gdp_billions"]), c.get("gdp_date", ""), "cache"
    except Exception:
        pass
    # 3. hardcoded fallback — the indicator ALWAYS renders
    return _GDP_FALLBACK["gdp_billions"], _GDP_FALLBACK["gdp_date"], "fallback"


def analyze_buffett():
    """
    Warren Buffett Indicator = Total US Market Cap / GDP * 100
    Uses Wilshire 5000 (^W5000 via yfinance) + GDP from FRED public CSV (no key needed).

    Zones:
      < 75%    Undervalued         -- Market is historically cheap
      75-90%   Fair Value          -- Fairly priced
      90-115%  Overvalued          -- Getting stretched
      115-150% Danger Zone         -- Proceed with caution
      > 150%   Extreme Overvaluation -- Buffett is in cash
    """
    try:
        # ── Wilshire 5000 (total US market cap proxy) ─────────
        # Each index point ≈ $1B total market cap historically.
        # Yahoo renamed the Wilshire 5000 to the FT Wilshire 5000 (^FTW5000);
        # the old ^W5000 ticker now frequently returns nothing. Try the current
        # symbol first, then the legacy ones — all the SAME ~50,000-scale index,
        # so the ratio math below is unaffected.
        wilshire_val = None
        for _sym in ("^FTW5000", "^W5000"):
            try:
                hist = yf.Ticker(_sym).history(period="5d")
                if hist is not None and not hist.empty:
                    closes = hist["Close"].dropna()
                    if len(closes):
                        wilshire_val = float(closes.iloc[-1])
                        break
            except Exception:
                continue
        if wilshire_val is None:
            return {"error": "Wilshire 5000 unavailable from yfinance"}

        # ── GDP (quarterly, $B) — robust fetch, never blocks the indicator ─────
        # US nominal GDP, billions USD, from FRED's public CSV. Fetched via curl
        # with an on-disk cache + hardcoded fallback (see _fetch_gdp): FRED's edge
        # read-times-out Python's urllib/requests, and GDP being stale by a quarter
        # is far better than the whole Buffett widget showing an error.
        gdp_billions, gdp_date, gdp_source = _fetch_gdp()

        # ── Compute ratio ──────────────────────────────────────
        ratio = (wilshire_val / gdp_billions) * 100

        # Zone classification
        if ratio < 75:
            zone   = "Undervalued"
            color  = "#3ddc84"
            verdict = "Market is historically cheap. Buffett would be buying."
        elif ratio < 90:
            zone   = "Fair Value"
            color  = "#6ab4ff"
            verdict = "Fairly priced. Normal entry conditions."
        elif ratio < 115:
            zone   = "Overvalued"
            color  = "#f5c842"
            verdict = "Getting stretched. Be selective on new entries."
        elif ratio < 150:
            zone   = "Danger Zone"
            color  = "#ff9944"
            verdict = "Proceed with caution. Buffett is sitting on cash."
        else:
            zone   = "Extreme"
            color  = "#ff5566"
            verdict = "Historically extreme. Buffett is likely in cash."

        return {
            "ratio":      round(ratio, 1),
            "wilshire":   round(wilshire_val, 0),
            "gdp":        round(gdp_billions, 0),
            "gdp_date":   gdp_date,
            "gdp_source": gdp_source,   # 'live' | 'cache' | 'fallback' (diagnostic)
            "zone":       zone,
            "zone_color": color,
            "verdict":    verdict,
        }

    except Exception as e:
        return {"error": str(e)}


# ── MAIN ──────────────────────────────────────────────────────

def main():
    sector   = analyze_sectors()
    top_risk = analyze_top_risk()
    buffett  = analyze_buffett()

    score = top_risk["score"]
    zone  = top_risk["zone"]
    reg   = sector["regime"]
    posture_line = f"{reg} leading | Risk: {zone} ({score}/100)"

    output = {
        "sector":       sector,
        "top_risk":     top_risk,
        "buffett":      buffett,
        "posture_line": posture_line,
        "as_of":        datetime.now().strftime("%Y-%m-%d"),
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
