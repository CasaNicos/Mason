"""
mason_scanner_skills.py — deterministic skill adapters for the Scanner agent
(MULTI_AGENT_ROADMAP.md Section 4.0). One adapter per qualifying skill.

HARD CONSTRAINT: zero LLM calls. Every adapter runs pure deterministic logic —
subprocess a vendored skill script (via mason_skills.run_skill), call a
project screener module, or compute technicals from OHLC. All "reason" strings
are templated from computed values, never model-authored.

Every adapter degrades gracefully: network/API failure returns
{"ok": False, "error": "... requires live environment ..."} and never raises.

Adapter result shape (uniform):
    {
      "ok":              bool,
      "source":          "<skill name>",
      "candidates":      [ {ticker, flagged_by, reason, urgency, signal{...}} ],
      "error":           str | None,
      "attempted_calls": int,     # subprocess/batch attempts, not inner HTTP calls
      "cached":          bool,    # True when data came from mason_skills' cache
      "extra":           dict,    # adapter-specific payload (e.g. raw earnings rows)
    }
"""

import sys
from pathlib import Path

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

LIVE_ENV_MSG = "requires live environment — see LIVE_TEST_INSTRUCTIONS.md"

# Default per-skill subprocess timeout (seconds). Nico's machine: FMP screeners
# can take a few minutes cold; sandbox: they fail fast on denied network.
DEFAULT_SKILL_TIMEOUT = 240


def _res(source, ok=False, candidates=None, error=None, attempted_calls=0,
         cached=False, extra=None):
    return {"ok": bool(ok), "source": source, "candidates": candidates or [],
            "error": error, "attempted_calls": int(attempted_calls),
            "cached": bool(cached), "extra": extra or {}}


def _cand(ticker, source, reason, urgency="routine", signal=None):
    return {"ticker": (ticker or "").upper(), "flagged_by": [source],
            "reason": reason, "urgency": urgency, "signal": signal or {}}


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ── 1. VCP screener (Minervini volatility-contraction breakouts) ─────────────────

def vcp_candidates(min_score=60, timeout=DEFAULT_SKILL_TIMEOUT, universe=None):
    """Flag valid VCPs / high contraction scores from the vendored vcp-screener.

    When `universe` is given it is passed through as the screener's --universe
    (custom symbol list). This avoids the S&P-500-constituents FMP endpoint,
    which is restricted on Nico's FMP tier (live-test finding, 2026-07-21:
    HTTP 402) — and cuts the screener's FMP call count substantially."""
    src = "vcp-screener"
    try:
        import mason_skills as SK
        extra = None
        if universe:
            extra = ["--strict", "--universe"] + sorted(
                {t.upper() for t in universe if t})
        r = SK.run_vcp(timeout=timeout, extra_args=extra)
    except Exception as e:
        return _res(src, error=f"adapter error: {e}", attempted_calls=1)
    if not (r.get("ok") and r.get("data")):
        return _res(src, error=f"{r.get('error') or 'no data'} ({LIVE_ENV_MSG})",
                    attempted_calls=1, cached=bool(r.get("cached")))
    out = []
    for c in (r["data"].get("results") or []):
        sym = (c.get("symbol") or "").upper()
        score = _num(c.get("composite_score"), 0)
        valid = bool(c.get("valid_vcp"))
        if not sym or not (valid or score >= min_score):
            continue
        pivot = _num(c.get("pivot_price"))
        state = (c.get("execution_state") or "").strip()
        near_pivot = any(k in state.lower() for k in ("breakout", "pivot", "buy"))
        reason = (f"VCP contraction score {score:.0f}"
                  + (f", pivot ${pivot:.2f}" if pivot else "")
                  + (f", state: {state}" if state else "")
                  + (" [valid VCP]" if valid else ""))
        out.append(_cand(sym, src, reason,
                         urgency="elevated" if (valid and near_pivot) else "routine",
                         signal={"composite_score": score, "pivot_price": pivot,
                                 "execution_state": state, "valid_vcp": valid,
                                 "price": _num(c.get("price")),
                                 "stop": _num(c.get("stop_loss_price") or c.get("stop"))}))
    return _res(src, ok=True, candidates=out, attempted_calls=1,
                cached=bool(r.get("cached")))


# ── 2. CANSLIM screener (O'Neil growth leaders) ──────────────────────────────────

def canslim_candidates(min_score=70, timeout=DEFAULT_SKILL_TIMEOUT):
    src = "canslim-screener"
    try:
        import mason_skills as SK
        r = SK.run_canslim(timeout=timeout)
    except Exception as e:
        return _res(src, error=f"adapter error: {e}", attempted_calls=1)
    if not (r.get("ok") and r.get("data")):
        return _res(src, error=f"{r.get('error') or 'no data'} ({LIVE_ENV_MSG})",
                    attempted_calls=1, cached=bool(r.get("cached")))
    out = []
    rows = r["data"].get("results") or r["data"].get("candidates") or []
    for c in rows:
        sym = (c.get("symbol") or c.get("ticker") or "").upper()
        score = _num(c.get("composite_score"), 0)
        passes = bool(c.get("passes_all"))
        if not sym or not (passes or score >= min_score):
            continue
        rating = (c.get("rating") or "").strip()
        reason = (f"CANSLIM composite {score:.0f}"
                  + (f" ({rating})" if rating else "")
                  + (" [passes all components]" if passes else ""))
        out.append(_cand(sym, src, reason,
                         urgency="elevated" if passes else "routine",
                         signal={"composite_score": score, "rating": rating,
                                 "passes_all": passes, "price": _num(c.get("price")),
                                 "guidance": c.get("guidance") or ""}))
    return _res(src, ok=True, candidates=out, attempted_calls=1,
                cached=bool(r.get("cached")))


# ── 3. Earnings-trade analyzer (post-earnings momentum / event drift) ────────────

def earnings_candidates(good_grades=("A", "B"), timeout=DEFAULT_SKILL_TIMEOUT):
    """Flag graded post-earnings reactions. extra['raw_results'] carries ALL scored
    rows (any grade) so the position-aware scan can cross-reference held tickers
    for earnings surprises without a second skill run."""
    src = "earnings-trade-analyzer"
    try:
        import mason_skills as SK
        r = SK.run_earnings(timeout=timeout)
    except Exception as e:
        return _res(src, error=f"adapter error: {e}", attempted_calls=1)
    if not (r.get("ok") and r.get("data")):
        err = str(r.get("error") or "no data")
        # Quiet earnings window: the analyzer exits rc=0 without writing a JSON
        # when nothing passes its filters. That is a legitimate empty result,
        # not a failure (live-test finding, 2026-07-21).
        if "No candidates found" in err:
            return _res(src, ok=True, candidates=[], attempted_calls=1,
                        extra={"note": "quiet earnings window — analyzer found "
                                       "no candidates matching criteria"})
        return _res(src, error=f"{err} ({LIVE_ENV_MSG})",
                    attempted_calls=1, cached=bool(r.get("cached")))
    rows = r["data"].get("results") or []
    out = []
    for c in rows:
        sym = (c.get("symbol") or "").upper()
        grade = (c.get("grade") or "").strip().upper()
        if not sym or grade not in good_grades:
            continue
        score = _num(c.get("composite_score"), 0)
        gap = _num(c.get("gap_pct"))
        reason = (f"Post-earnings grade {grade} (score {score:.0f})"
                  + (f", gap {gap:+.1f}%" if gap is not None else "")
                  + (f" on {c.get('earnings_date')}" if c.get("earnings_date") else ""))
        out.append(_cand(sym, src, reason,
                         urgency="elevated" if grade == "A" else "routine",
                         signal={"grade": grade, "composite_score": score,
                                 "gap_pct": gap, "earnings_date": c.get("earnings_date"),
                                 "sector": c.get("sector")}))
    return _res(src, ok=True, candidates=out, attempted_calls=1,
                cached=bool(r.get("cached")), extra={"raw_results": rows})


# ── 4. Theme detector (hot bullish themes → representative names in universe) ────

def theme_candidates(universe, min_heat=60, timeout=DEFAULT_SKILL_TIMEOUT):
    src = "theme-detector"
    uni = {t.upper() for t in (universe or [])}
    try:
        import mason_skills as SK
        r = SK.run_theme(timeout=timeout)
    except Exception as e:
        return _res(src, error=f"adapter error: {e}", attempted_calls=1)
    if not (r.get("ok") and r.get("data")):
        return _res(src, error=f"{r.get('error') or 'no data'} ({LIVE_ENV_MSG})",
                    attempted_calls=1, cached=bool(r.get("cached")))
    themes = ((r["data"].get("themes") or {}).get("bullish")
              or (r["data"].get("themes") or {}).get("all") or [])
    out, seen = [], {}
    for t in themes:
        heat = _num(t.get("heat"), 0)
        if (t.get("direction") or "bullish") != "bullish" or heat < min_heat:
            continue
        name = t.get("name") or "unnamed theme"
        stage = t.get("stage") or t.get("maturity") or ""
        for rs in (t.get("representative_stocks") or []):
            sym = (rs.get("symbol") if isinstance(rs, dict) else rs)
            sym = (sym or "").upper()
            if not sym or sym not in uni or sym in seen:
                continue
            seen[sym] = True
            reason = (f"Representative name in hot theme '{name}' (heat {heat:.0f}"
                      + (f", {stage}" if stage else "") + ")")
            out.append(_cand(sym, src, reason, urgency="routine",
                             signal={"theme": name, "heat": heat, "stage": stage}))
    return _res(src, ok=True, candidates=out, attempted_calls=1,
                cached=bool(r.get("cached")))


# ── 5. Neglected-value screener (Mason's custom undervalued+underreported sleeve) ─

def neglected_value_candidates(limit=5):
    src = "neglected-value"
    try:
        import mason_neglected_value as NV
        rows = NV.candidates(limit=limit)
    except Exception as e:
        return _res(src, error=f"{e} ({LIVE_ENV_MSG})", attempted_calls=1)
    out = []
    for c in (rows or []):
        sym = (c.get("ticker") or "").upper()
        if not sym:
            continue
        score = _num(c.get("score"), 0)
        thesis = (c.get("guidance") or "").strip()
        reason = (f"Neglected-value score {score:.0f}"
                  + (f" — {thesis[:140]}" if thesis else ""))
        out.append(_cand(sym, src, reason, urgency="routine",
                         signal={"score": score, "entry": _num(c.get("entry")),
                                 "stop": _num(c.get("stop"))}))
    # NV.candidates() returns [] both on "no qualifying names" and on failure;
    # treat [] as ok (its own cache layer reports errors internally).
    return _res(src, ok=True, candidates=out, attempted_calls=1)


# ── 6. Technical sweep (own deterministic OHLC logic, yfinance — free, no quota) ─

def technical_snapshots(tickers, period="1y"):
    """One batched yfinance download → per-ticker snapshot dict. Returns
    {"ok", "snapshots": {TICKER: {...}}, "error", "attempted_calls"}.
    Snapshot fields: close, prev_close, day_change_pct, gap_pct, vol_ratio_20d,
    pct_from_52w_high, above_ma50, above_ma200."""
    tickers = sorted({t.upper() for t in (tickers or []) if t})
    if not tickers:
        return {"ok": True, "snapshots": {}, "error": None, "attempted_calls": 0}
    try:
        import yfinance as yf
        import mason_symbols
        ysym = mason_symbols.yf_symbols(tickers)         # BRK.B → BRK-B; results keyed by our symbol (R2-A)
        df = yf.download(list(dict.fromkeys(ysym.values())), period=period, interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        return {"ok": False, "snapshots": {},
                "error": f"yfinance download failed: {e} ({LIVE_ENV_MSG})",
                "attempted_calls": 1}
    if df is None or len(df) == 0:
        return {"ok": False, "snapshots": {},
                "error": f"yfinance returned no data ({LIVE_ENV_MSG})",
                "attempted_calls": 1}
    snaps = {}
    multi = getattr(getattr(df, "columns", None), "nlevels", 1) > 1
    for t in tickers:
        try:
            sub = df[ysym[t]] if multi else df
            sub = sub.dropna(subset=["Close"])
            if len(sub) < 30:
                continue
            close = float(sub["Close"].iloc[-1])
            prev = float(sub["Close"].iloc[-2])
            opn = float(sub["Open"].iloc[-1])
            vol = float(sub["Volume"].iloc[-1])
            v20 = float(sub["Volume"].iloc[-21:-1].mean()) or 0.0
            hi52 = float(sub["High"].max())
            ma50 = float(sub["Close"].rolling(50).mean().iloc[-1]) if len(sub) >= 50 else None
            ma200 = float(sub["Close"].rolling(200).mean().iloc[-1]) if len(sub) >= 200 else None
            snaps[t] = {
                "close": round(close, 2),
                "prev_close": round(prev, 2),
                "day_change_pct": round((close - prev) / prev * 100, 2) if prev else None,
                "gap_pct": round((opn - prev) / prev * 100, 2) if prev else None,
                "vol_ratio_20d": round(vol / v20, 2) if v20 else None,
                "pct_from_52w_high": round((close - hi52) / hi52 * 100, 2) if hi52 else None,
                "above_ma50": (close > ma50) if ma50 else None,
                "above_ma200": (close > ma200) if ma200 else None,
            }
        except Exception:
            continue  # one bad ticker never kills the sweep
    if not snaps:
        return {"ok": False, "snapshots": {},
                "error": f"yfinance parse produced no snapshots ({LIVE_ENV_MSG})",
                "attempted_calls": 1}
    return {"ok": True, "snapshots": snaps, "error": None, "attempted_calls": 1}


def technical_candidates(snapshots, near_high_pct=3.0, gap_min_pct=4.0,
                         vol_spike_min=2.5):
    """PURE function (no I/O — unit-testable offline). Flags momentum/breakout
    setups from technical_snapshots() output."""
    src = "technical-sweep"
    out = []
    for t, s in sorted((snapshots or {}).items()):
        reasons, signal = [], dict(s)
        urgency = "routine"
        d52 = s.get("pct_from_52w_high")
        vr = s.get("vol_ratio_20d")
        gap = s.get("gap_pct")
        chg = s.get("day_change_pct")
        # Breakout watch: near 52w high with participation, in an uptrend.
        if (d52 is not None and d52 >= -near_high_pct and vr is not None and vr >= 1.5
                and s.get("above_ma50")):
            reasons.append(f"within {abs(d52):.1f}% of 52w high on {vr:.1f}x volume")
            if d52 >= -1.0 and vr >= 2.0:
                urgency = "elevated"
        # Large gap with volume behind it.
        if gap is not None and abs(gap) >= gap_min_pct and (vr or 0) >= 1.5:
            reasons.append(f"gapped {gap:+.1f}% on {vr:.1f}x volume")
            urgency = "elevated"
        # Standalone volume spike with a real move.
        if (vr is not None and vr >= vol_spike_min and chg is not None
                and abs(chg) >= 3.0 and not reasons):
            reasons.append(f"volume spike {vr:.1f}x 20d avg, day move {chg:+.1f}%")
        if reasons:
            out.append(_cand(t, src, "Technical: " + "; ".join(reasons),
                             urgency=urgency, signal=signal))
    return _res(src, ok=True, candidates=out, attempted_calls=0)


if __name__ == "__main__":
    # Offline smoke: the pure path only (network adapters exercised by the tests
    # and by mason_scanner.py itself).
    import json as _json
    demo = {"TEST": {"close": 100.0, "prev_close": 92.0, "day_change_pct": 8.7,
                     "gap_pct": 5.4, "vol_ratio_20d": 3.1, "pct_from_52w_high": -0.5,
                     "above_ma50": True, "above_ma200": True}}
    print(_json.dumps(technical_candidates(demo), indent=2))
