"""
mason_etf_screen.py — INDEX DESK (Agent I, 2026-09-28): a curated ETF screen for
SHORT-TERM (2-8 week) allocation + a deterministic index outlook for 1-5 session
index calls (0-5 DTE SPY/QQQ/IWM single-leg ideas). NO LLM anywhere in this file.

Contracts: docs/catalyst_desk/WEALTH_MANAGER_CONTRACTS.md (builder I).

Public API (never raises; plain dicts / strings):
  * etf_screen(*, force=False) -> dict
  * etf_screen_text(max_tokens=900, *, category=None, top=None) -> str
  * index_outlook_text(max_tokens=700) -> str
  * UNIVERSE / universe() — the curated symbol set the allocator validates picks against
  * pure math: max_drawdown_pct, short_term_score, etf_metrics, regime_notes,
    atm_straddle, econ_flags

Data: yfinance daily bars (1y, via mason_dossier._history — the shared in-process
series cache, so a director run and a screen download each symbol once), the
.posture_cache.json regime written by mason_market_posture, and calendar_events
(econ rows) read through a READ-ONLY connection. Expense ratios are a static table
(approx., 2026-09) — labelled as such in the output.

Caching: the screen payload is cached in-process AND in .etf_screen_cache.json with a
1h TTL (index_desk.screen_ttl_seconds overrides). The outlook's option-chain implied
moves are cached in-process for 15 min.

Network hooks (tests monkeypatch these): _bars(symbols), _fetch_chain(sym).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import mason_db
import mason_dossier as D

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "mason.config.json"
CACHE_FILE = BASE / ".etf_screen_cache.json"
POSTURE_FILE = BASE / ".posture_cache.json"
SCHEMA = "etf_screen/1.0"
DEFAULT_TTL = 3600
CHAIN_TTL = 900

# ── curated universe (category, short label, approx. expense ratio % — static 2026-09) ──
_U = [
    # broad
    ("SPY", "broad", "S&P 500", 0.09), ("VOO", "broad", "S&P 500 (Vanguard)", 0.03),
    ("VTI", "broad", "Total US market", 0.03), ("QQQ", "broad", "Nasdaq-100", 0.20),
    ("QQQM", "broad", "Nasdaq-100 (mini)", 0.15), ("IWM", "broad", "Russell 2000", 0.19),
    ("DIA", "broad", "Dow 30", 0.16), ("RSP", "broad", "S&P 500 equal-wt", 0.20),
    # sectors
    ("XLK", "sector", "Technology", 0.08), ("XLF", "sector", "Financials", 0.08),
    ("XLE", "sector", "Energy", 0.08), ("XLV", "sector", "Health care", 0.08),
    ("XLI", "sector", "Industrials", 0.08), ("XLY", "sector", "Cons. discretionary", 0.08),
    ("XLP", "sector", "Cons. staples", 0.08), ("XLU", "sector", "Utilities", 0.08),
    ("XLB", "sector", "Materials", 0.08), ("XLC", "sector", "Comm. services", 0.08),
    ("XLRE", "sector", "Real estate", 0.08), ("SMH", "sector", "Semis (VanEck)", 0.35),
    ("SOXX", "sector", "Semis (iShares)", 0.34),
    # factors
    ("MTUM", "factor", "US momentum", 0.15), ("SPMO", "factor", "S&P 500 momentum", 0.13),
    ("QUAL", "factor", "US quality", 0.15), ("USMV", "factor", "US min-vol", 0.15),
    ("VLUE", "factor", "US value", 0.15), ("VBR", "factor", "Small-cap value", 0.07),
    # themes
    ("ARKK", "theme", "ARK innovation", 0.75), ("IGV", "theme", "Software", 0.39),
    ("CIBR", "theme", "Cybersecurity", 0.59), ("ITA", "theme", "Aerospace & defense", 0.38),
    ("XBI", "theme", "Biotech (equal-wt)", 0.35), ("TAN", "theme", "Solar", 0.67),
    ("URA", "theme", "Uranium", 0.69),
    # international
    ("VXUS", "intl", "Total ex-US", 0.05), ("EFA", "intl", "Developed ex-US", 0.32),
    ("EEM", "intl", "Emerging mkts", 0.70), ("FXI", "intl", "China large caps", 0.74),
    ("KWEB", "intl", "China internet", 0.70), ("EWJ", "intl", "Japan", 0.50),
    # bonds
    ("SHY", "bond", "1-3y Treasuries", 0.15), ("IEF", "bond", "7-10y Treasuries", 0.15),
    ("TLT", "bond", "20y+ Treasuries", 0.15), ("HYG", "bond", "High-yield corp", 0.49),
    ("TIP", "bond", "TIPS", 0.18),
    # commodities
    ("GLD", "commodity", "Gold", 0.40), ("SLV", "commodity", "Silver", 0.50),
    ("USO", "commodity", "WTI crude (futures)", 0.60), ("DBC", "commodity", "Broad commodities", 0.85),
    # crypto
    ("IBIT", "crypto", "Spot bitcoin", 0.25),
]
UNIVERSE = {s: {"category": c, "label": l, "expense_ratio": er} for s, c, l, er in _U}
CATEGORIES = ("broad", "sector", "factor", "theme", "intl", "bond", "commodity", "crypto")
MACRO_SYMS = ["^VIX", "^TNX", "DX-Y.NYB"]
OUTLOOK_IDX = ["SPY", "QQQ", "IWM"]
OUTLOOK_EXTRA = ["^VIX", "^VIX9D", "^VIX3M", "^TNX", "DX-Y.NYB"]
_DURATION = {"IEF", "TLT", "TIP"}
_USD_SENSITIVE_CATS = {"intl", "commodity"}


def universe() -> set:
    return set(UNIVERSE)


# ── config ──────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        return json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8")).get("index_desk") or {}
    except Exception:
        return {}


def _ttl() -> int:
    try:
        return int(_cfg().get("screen_ttl_seconds") or DEFAULT_TTL)
    except Exception:
        return DEFAULT_TTL


# ── hooks (tests monkeypatch) ─────────────────────────────────────────────────────

def _bars(symbols: list) -> dict:
    """{sym: DataFrame[Open,High,Low,Close,Volume]} — 1y daily, shared dossier cache."""
    return D._history(list(symbols))


def _pick_expiries(exps: list, today: date) -> list:
    """[nearest expiry with >= 1 calendar day left, nearest FRIDAY weekly >= 1 day]
    (deduped; 0DTE only when nothing else is listed)."""
    ds = []
    for e in exps or []:
        try:
            ds.append(datetime.strptime(e, "%Y-%m-%d").date())
        except (TypeError, ValueError):
            continue
    ds = sorted(ds)
    fut = [d for d in ds if (d - today).days >= 1]
    out = []
    if fut:
        out.append(fut[0])
        fri = next((d for d in fut if d.weekday() == 4), None)
        if fri and fri not in out:
            out.append(fri)
    elif ds:
        out.append(ds[0])
    return [d.isoformat() for d in out]


def _fetch_chain(sym: str) -> list:
    """-> [(expiry 'YYYY-MM-DD', calls [{strike,bid,ask,last}], puts [...]), ...] for
    _pick_expiries (next session + the Friday weekly). May raise."""
    def rows(df):
        return [{"strike": D._num(r.get("strike")), "bid": D._num(r.get("bid")),
                 "ask": D._num(r.get("ask")), "last": D._num(r.get("lastPrice"))}
                for _, r in df.iterrows()]
    out = []
    with D._yf_slot():
        t = D._ticker(sym)
        for e in _pick_expiries(list(t.options or []), date.today()):
            ch = t.option_chain(e)
            out.append((e, rows(ch.calls), rows(ch.puts)))
    if not out:
        raise ValueError("no expiries")
    return out


_now = time.time          # tests monkeypatch for TTL checks


# ═══════════════════════════════════════════════════════════════════════════════
# Pure math
# ═══════════════════════════════════════════════════════════════════════════════

def max_drawdown_pct(closes: list):
    """Largest peak-to-trough decline inside `closes`, as a negative % (0.0 if none)."""
    if not closes or len(closes) < 2:
        return None
    peak, mdd = closes[0], 0.0
    for x in closes:
        if x is None:
            continue
        peak = max(peak, x)
        if peak > 0:
            mdd = min(mdd, (x / peak - 1.0) * 100.0)
    return mdd


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def short_term_score(m: dict):
    """Composite for a 2-8 week hold: momentum-with-trend, risk-adjusted.
      mom   = 0.2*r1w + 0.5*r1m + 0.3*(r3m/3)          (% per ~month, recent-weighted)
      vol_m = max(vol20, 5) / sqrt(12)                   (monthly % vol)
      mom_z = clip(mom / vol_m, ±3)                      momentum per unit of risk
      rs    = clip(vs_spy_1m / vol_m, ±2)                relative strength vs SPY
      trend = [close>50d] + [50d>200d] + 0.5*[close>200d] - 1.25  (−1.25..+1.25)
      rsi   = −1.0 if RSI≥78, −0.5 if ≥72, +0.25 if 50–72, −0.5 if <40
      dd    = −0.75 if 3m max drawdown < −15%, −0.35 if < −8%
      raw   = mom_z + 0.5*rs + 0.8*trend + rsi + dd;  score = 100/(1+e^(−raw/1.5))
    Returns (score 0-100, raw) or (None, None) without enough data."""
    r1w, r1m, r3m = m.get("r1w"), m.get("r1m"), m.get("r3m")
    if r1m is None:
        return None, None
    vol = m.get("vol20")
    vol_m = max(vol if vol is not None else 20.0, 5.0) / math.sqrt(12)
    mom = 0.2 * (r1w or 0.0) + 0.5 * r1m + 0.3 * ((r3m / 3.0) if r3m is not None else r1m)
    mom_z = _clip(mom / vol_m, -3, 3)
    rs = _clip((m.get("vs_spy_1m") or 0.0) / vol_m, -2, 2)
    c, s50, s200 = m.get("last"), m.get("sma50"), m.get("sma200")
    trend = 0.0
    if c is not None and s50 is not None:
        trend += 1.0 if c > s50 else 0.0
    if s50 is not None and s200 is not None:
        trend += 1.0 if s50 > s200 else 0.0
    elif s50 is not None and c is not None:          # short history: half credit on price-vs-50
        trend += 0.5 if c > s50 else 0.0
    if c is not None and s200 is not None:
        trend += 0.5 if c > s200 else 0.0
    trend -= 1.25
    rsi = m.get("rsi14")
    ra = 0.0
    if rsi is not None:
        if rsi >= 78:
            ra = -1.0
        elif rsi >= 72:
            ra = -0.5
        elif rsi >= 50:
            ra = 0.25
        elif rsi < 40:
            ra = -0.5
    dd = m.get("maxdd3m")
    da = -0.75 if (dd is not None and dd < -15) else (-0.35 if (dd is not None and dd < -8) else 0.0)
    raw = mom_z + 0.5 * rs + 0.8 * trend + ra + da
    return int(round(100.0 / (1.0 + math.exp(-raw / 1.5)))), round(raw, 3)


def _trend_label(c, s50, s200) -> str:
    if c is None or s50 is None:
        return "n/a"
    if s200 is None:
        return "up" if c > s50 else "down"
    if c > s50 > s200:
        return "up"
    if c < s50 < s200:
        return "down"
    return "mixed"


def etf_metrics(sym: str, df, spy_closes: list | None = None) -> dict:
    """All screen metrics for one symbol from its daily bars (DataFrame or dict of lists)."""
    closes = _closes(df)
    if len(closes) < 22:
        return {"symbol": sym, "error": f"only {len(closes)} bars"}
    last = closes[-1]
    s50, s200 = D.sma(closes, 50), D.sma(closes, 200)
    rv = D.realized_vol_series(closes, 20)
    m = {"symbol": sym, "last": _r(last), "last_date": _last_date(df),
         "r1w": _r(D.pct_change_n(closes, 5)), "r1m": _r(D.pct_change_n(closes, 21)),
         "r3m": _r(D.pct_change_n(closes, 63)), "r6m": _r(D.pct_change_n(closes, 126)),
         "sma50": _r(s50), "sma200": _r(s200),
         "dist50": _r(D._pct(last, s50)) if s50 else None,
         "dist200": _r(D._pct(last, s200)) if s200 else None,
         "rsi14": _r(D.rsi_wilder(closes, 14), 1),
         "vol20": _r(rv[-1] * 100, 1) if rv else None,
         "maxdd3m": _r(max_drawdown_pct(closes[-64:]))}
    m["trend"] = _trend_label(last, s50, s200)
    if spy_closes and len(spy_closes) >= 22:
        sr1m, sr3m = D.pct_change_n(spy_closes, 21), D.pct_change_n(spy_closes, 63)
        m["vs_spy_1m"] = _r(m["r1m"] - sr1m) if (m["r1m"] is not None and sr1m is not None) else None
        m["vs_spy_3m"] = _r(m["r3m"] - sr3m) if (m["r3m"] is not None and sr3m is not None) else None
    else:
        m["vs_spy_1m"] = m["vs_spy_3m"] = None
    m["score"], m["score_raw"] = short_term_score(m)
    return m


def regime_notes(sym: str, m: dict, ctx: dict) -> list:
    """≤3 short, deterministic regime-fit notes from posture + macro context."""
    info = UNIVERSE.get(sym) or {}
    cat = info.get("category")
    notes = []
    leaders, laggards = set(ctx.get("leaders") or []), set(ctx.get("laggards") or [])
    if sym in leaders:
        notes.append("posture leader")
    if sym in laggards or (sym == "XLRE" and "VNQ" in laggards):
        notes.append("posture laggard")
    zone = str(ctx.get("zone") or "")
    hi_risk = zone in ("Orange", "Red")
    vol = m.get("vol20")
    if hi_risk and (cat in ("theme", "crypto") or (vol is not None and vol >= 30)):
        notes.append(f"high-vol vs {zone} top-risk")
    if hi_risk and sym in ("USMV", "QUAL", "SHY", "XLP", "XLV"):
        notes.append(f"defensive fit ({zone} risk)")
    tnx = ctx.get("tnx_5d_bp")
    if sym in _DURATION and tnx is not None and abs(tnx) >= 10:
        notes.append(f"10Y {tnx:+.0f}bp 5d: duration {'headwind' if tnx > 0 else 'tailwind'}")
    dxy = ctx.get("dxy_1m")
    if cat in _USD_SENSITIVE_CATS and dxy is not None and abs(dxy) >= 1.5:
        notes.append(f"USD {dxy:+.1f}% 1m: {'headwind' if dxy > 0 else 'tailwind'}")
    rsi = m.get("rsi14")
    if rsi is not None and rsi >= 75:
        notes.append("overbought")
    if m.get("trend") == "down":
        notes.append("downtrend")
    return notes[:3]


def atm_straddle(calls: list, puts: list, spot) -> dict:
    """ATM straddle from chain rows (common strike nearest spot) → implied move."""
    s = D._num(spot)
    if not s or not calls or not puts:
        return {}
    pmap = {p.get("strike"): p for p in puts if p.get("strike") is not None}
    strikes = sorted((c for c in calls if c.get("strike") in pmap), key=lambda c: abs(c["strike"] - s))
    for c in strikes[:3]:
        p = pmap[c["strike"]]
        cm, pm = D.option_mid(c.get("bid"), c.get("ask"), c.get("last")), D.option_mid(p.get("bid"), p.get("ask"), p.get("last"))
        if cm and pm:
            out = D.implied_move(cm, pm, s)
            if out:
                out["strike"] = c["strike"]
                return out
    return {}


_FLAG_RULES = [
    ("FOMC", re.compile(r"\b(FOMC (statement|press|economic projections|meeting minutes|minutes)|"
                        r"federal funds rate|fed(eral)? (interest )?rate decision|interest rate decision)\b", re.I)),
    ("FED-SPEAK", re.compile(r"\b(FOMC member|fed chair|powell|fed governor)\b.*\bspeaks?\b", re.I)),
    ("CPI", re.compile(r"\bCPI\b|consumer price index", re.I)),
    ("NFP", re.compile(r"^(?!.*\bADP\b).*non-?farm (employment|payrolls)", re.I)),
    ("PCE", re.compile(r"\bPCE\b", re.I)),
    ("PPI", re.compile(r"\bPPI\b", re.I)),
]


def econ_flags(title: str, impact: str | None = None) -> list:
    t = title or ""
    flags = []
    for name, rx in _FLAG_RULES:
        if rx.search(t):
            if name == "FED-SPEAK" and "FOMC" in flags:
                continue
            flags.append(name)
    if "FOMC" in flags and "FED-SPEAK" in flags:
        flags.remove("FED-SPEAK")
    if (impact or "").lower() == "high" or "high impact" in t.lower():
        flags.append("HIGH")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _r(v, nd=2):
    return D._r(v, nd)


def _closes(df) -> list:
    if df is None:
        return []
    if isinstance(df, dict):
        return [float(x) for x in (df.get("Close") or []) if x is not None]
    return D._col(df, "Close")


def _col(df, name) -> list:
    if df is None:
        return []
    if isinstance(df, dict):
        return [float(x) for x in (df.get(name) or []) if x is not None]
    return D._col(df, name)


def _last_date(df):
    try:
        if isinstance(df, dict):
            ds = df.get("dates") or []
            return str(ds[-1]) if ds else None
        ds = D._bar_dates(df)
        return ds[-1].isoformat() if ds else None
    except Exception:
        return None


def _read_posture() -> dict:
    """First JSON object in .posture_cache.json (the file may hold trailing objects)."""
    try:
        txt = Path(POSTURE_FILE).read_text(encoding="utf-8")
        obj, _ = json.JSONDecoder().raw_decode(txt.lstrip())
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _posture_ctx(p: dict) -> dict:
    sec, tr = p.get("sector") or {}, p.get("top_risk") or {}
    return {"regime": sec.get("regime"), "cycle_phase": sec.get("cycle_phase"),
            "leaders": sec.get("leaders") or [], "laggards": sec.get("laggards") or [],
            "zone": tr.get("zone"), "risk_score": tr.get("score"),
            "buffett_zone": (p.get("buffett") or {}).get("zone"),
            "posture_line": p.get("posture_line"), "as_of": p.get("as_of")}


def _macro_ctx(hist: dict) -> dict:
    out = {}
    vix = _closes(hist.get("^VIX"))
    if vix:
        out["vix"] = _r(vix[-1])
        out["vix_5d"] = _r(D.pct_change_n(vix, 5))
    tnx = _closes(hist.get("^TNX"))
    if len(tnx) >= 6:
        out["tnx"] = _r(tnx[-1], 3)
        out["tnx_5d_bp"] = _r((tnx[-1] - tnx[-6]) * 100, 1)
    dxy = _closes(hist.get("DX-Y.NYB"))
    if dxy:
        out["dxy"] = _r(dxy[-1])
        out["dxy_1m"] = _r(D.pct_change_n(dxy, 21))
    return out


def _ro_conn():
    conn = sqlite3.connect(f"file:{Path(mason_db.DB_FILE)}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _tok(s: str) -> int:
    return len(s or "") // 4


# ═══════════════════════════════════════════════════════════════════════════════
# ETF screen
# ═══════════════════════════════════════════════════════════════════════════════

_mem_lock = threading.Lock()
_mem: dict = {}                    # {"ts": epoch, "payload": dict}


def _cache_read(ttl: int):
    now = _now()
    with _mem_lock:
        hit = _mem.get("payload")
        if hit and now - _mem.get("ts", 0) < ttl:
            return dict(hit, cache_hit="memory")
    try:
        obj = json.loads(Path(CACHE_FILE).read_text(encoding="utf-8"))
        ts = float(obj.get("_ts") or 0)
        if obj.get("schema_version") == SCHEMA and now - ts < ttl:
            with _mem_lock:
                _mem.update(ts=ts, payload=obj)
            return dict(obj, cache_hit="file")
    except Exception:
        pass
    return None


def _cache_write(payload: dict):
    with _mem_lock:
        _mem.update(ts=payload["_ts"], payload=payload)
    try:
        tmp = Path(str(CACHE_FILE) + ".tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


def clear_cache(file_too: bool = False):
    with _mem_lock:
        _mem.clear()
    if file_too:
        try:
            Path(CACHE_FILE).unlink()
        except Exception:
            pass


def etf_screen(*, force: bool = False) -> dict:
    """Ranked screen of the curated universe. Never raises."""
    t0 = time.time()
    ttl = _ttl()
    if not force:
        hit = _cache_read(ttl)
        if hit:
            return hit
    try:
        syms = list(UNIVERSE) + [s for s in MACRO_SYMS if s not in UNIVERSE]
        hist = _bars(syms) or {}
        spy = _closes(hist.get("SPY"))
        posture = _posture_ctx(_read_posture())
        macro = _macro_ctx(hist)
        ctx = dict(posture, **macro)
        rows, errors = [], []
        for s in UNIVERSE:
            try:
                m = etf_metrics(s, hist.get(s), spy)
            except Exception as e:
                m = {"symbol": s, "error": f"metrics: {e}"}
            m.update(category=UNIVERSE[s]["category"], label=UNIVERSE[s]["label"],
                     expense_ratio=UNIVERSE[s]["expense_ratio"])
            if "error" in m:
                errors.append(f"{s}: {m['error']}")
            else:
                m["notes"] = regime_notes(s, m, ctx)
            rows.append(m)
        ranked = sorted([r for r in rows if r.get("score") is not None],
                        key=lambda r: (-r["score"], -(r.get("score_raw") or 0), r["symbol"]))
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        missing = [r for r in rows if r.get("score") is None]
        last_dates = sorted({r.get("last_date") for r in ranked if r.get("last_date")})
        payload = {"schema_version": SCHEMA, "built_at": datetime.now().isoformat(timespec="seconds"),
                   "_ts": _now(), "ttl_seconds": ttl, "rows": ranked + missing,
                   "posture": posture, "macro": macro, "errors": errors[:20],
                   "last_bar": last_dates[-1] if last_dates else None,
                   "sources": {"bars": "yfinance daily (1y)", "posture": ".posture_cache.json",
                               "expense_ratio": "static table (approx., 2026-09)"},
                   "build_seconds": round(time.time() - t0, 2)}
        if ranked:                         # never cache an empty (network-down) screen
            _cache_write(payload)
        return dict(payload, cache_hit=False)
    except Exception as e:
        return {"schema_version": SCHEMA, "built_at": datetime.now().isoformat(timespec="seconds"),
                "rows": [], "errors": [f"build: {e}"], "posture": {}, "macro": {}, "cache_hit": False,
                "build_seconds": round(time.time() - t0, 2)}


def _p(v, nd=1):
    return "n/a" if v is None else f"{v:+.{nd}f}"


def _f(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def _row_line(r: dict) -> str:
    er = r.get("expense_ratio")
    trend = f"{r.get('trend')} {_p(r.get('dist50'), 0)}/{_p(r.get('dist200'), 0)}"
    notes = "; ".join(r.get("notes") or []) or "-"
    return (f"{r.get('rank', '-')}. {r['symbol']} {r.get('label')} [{r.get('category')}] | {r.get('score')} | "
            f"${_f(r.get('last'), 2)} | {_p(r.get('r1w'))}/{_p(r.get('r1m'))}/{_p(r.get('r3m'))}/{_p(r.get('r6m'))} | "
            f"{_p(r.get('vs_spy_1m'))} | {trend} | {_f(r.get('rsi14'), 0)} | {_f(r.get('vol20'), 0)} | "
            f"{_f(r.get('maxdd3m'), 1)} | {'n/a' if er is None else f'{er:.2f}'} | {notes}")


def etf_screen_text(max_tokens: int = 900, *, category: str | None = None, top: int | None = None,
                    screen: dict | None = None) -> str:
    """Dense ranked table, source-stamped, ≤ max_tokens (len/4). Never raises."""
    try:
        sc = screen if screen is not None else etf_screen()
        rows = [r for r in sc.get("rows") or [] if r.get("score") is not None]
        if category:
            rows = [r for r in rows if r.get("category") == str(category).lower()]
        if top:
            rows = rows[:max(1, int(top))]
        po, mc = sc.get("posture") or {}, sc.get("macro") or {}
        head = [f"=== ETF SCREEN (short-term 2-8wk momentum-with-trend) · last bar {sc.get('last_bar')} · "
                f"built {sc.get('built_at')} · src yfinance daily; posture .posture_cache.json "
                f"({po.get('as_of')}); ER static approx ===",
                f"Regime: {po.get('posture_line') or 'n/a'} · phase {po.get('cycle_phase') or 'n/a'} · "
                f"leaders {','.join(po.get('leaders') or []) or 'n/a'} · laggards {','.join(po.get('laggards') or []) or 'n/a'} · "
                f"Buffett {po.get('buffett_zone') or 'n/a'} | VIX {_f(mc.get('vix'), 1)} ({_p(mc.get('vix_5d'))}% 5d) · "
                f"10Y {_f(mc.get('tnx'), 3)}% ({_p(mc.get('tnx_5d_bp'), 0)}bp 5d) · DXY {_f(mc.get('dxy'), 2)} ({_p(mc.get('dxy_1m'))}% 1m)",
                "Cols: rank. sym label [cat] | score0-100 | last | ret% 1w/1m/3m/6m | vs SPY 1m pp | "
                "trend (vs50d/vs200d %) | RSI14 | vol20 ann% | maxDD 3m % | ER% | regime notes"]
        if not rows:
            return "\n".join(head + ["no rows (" + "; ".join(sc.get("errors") or ["no data"])[:200] + ")"])
        budget = max(1, int(max_tokens)) * 4
        lines = list(head)
        used = len("\n".join(lines))
        shown = 0
        for r in rows:
            ln = _row_line(r)
            if used + len(ln) + 1 + 60 > budget:
                break
            lines.append(ln)
            used += len(ln) + 1
            shown += 1
        if shown < len(rows):
            lines.append(f"…{len(rows) - shown} lower-ranked rows omitted for budget")
        if sc.get("errors"):
            tail = "errors: " + "; ".join(sc["errors"])[:160]
            if used + len(tail) < budget:
                lines.append(tail)
        text = "\n".join(lines)
        return text if len(text) <= budget else text[:budget - 14].rstrip() + "\n…[truncated]"
    except Exception as e:
        return f"ETF SCREEN unavailable: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Index outlook (facts only — the director makes the call)
# ═══════════════════════════════════════════════════════════════════════════════

_chain_lock = threading.Lock()
_chain_cache: dict = {}            # sym -> (ts, result)


def _implied_move(sym: str, spot) -> list:
    now = _now()
    with _chain_lock:
        hit = _chain_cache.get(sym)
        if hit and now - hit[0] < CHAIN_TTL:
            return hit[1]
    try:
        res = []
        for exp, calls, puts in _fetch_chain(sym):
            mv = atm_straddle(calls, puts, spot)
            res.append(dict(mv, expiry=exp) if mv else {"expiry": exp, "error": "no two-sided ATM quotes"})
        res = res or [{"error": "no chains"}]
    except Exception as e:
        res = [{"error": str(e)[:80]}]
    with _chain_lock:
        _chain_cache[sym] = (now, res)
    return res


def _trading_days(start: date, n: int) -> list:
    import mason_director as MD
    out, d = [], start
    while len(out) < n:
        if MD.is_trading_day(datetime(d.year, d.month, d.day)):
            out.append(d)
        d += timedelta(days=1)
    return out


def econ_calendar(n_sessions: int = 5, *, today: date | None = None) -> list:
    """Econ/central-bank calendar_events over the next n trading sessions (incl. today),
    with FOMC/CPI/NFP/PCE/HIGH flags. READ-ONLY connection; [] on any failure."""
    today = today or date.today()
    days = _trading_days(today, n_sessions)
    end = days[-1] if days else today
    conn = None
    try:
        conn = _ro_conn()
        q = ",".join("?" * len(D._MACRO_CAL_TYPES))
        rows = conn.execute(
            f"SELECT event_date, event_time, event_type, title, status, source FROM calendar_events "
            f"WHERE LOWER(event_type) IN ({q}) AND event_date >= ? AND event_date <= ? "
            f"AND COALESCE(record_deleted,0)=0 ORDER BY event_date, event_time, id LIMIT 60",
            list(D._MACRO_CAL_TYPES) + [today.isoformat(), end.isoformat()]).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for r in rows:
        title = (r["title"] or "")[:120]
        out.append({"date": r["event_date"], "time": r["event_time"], "type": r["event_type"],
                    "title": title, "impact": r["status"], "source": r["source"],
                    "flags": econ_flags(title, r["status"])})
    return out


def _idx_state(sym: str, df) -> dict:
    c, h, l = _closes(df), _col(df, "High"), _col(df, "Low")
    if len(c) < 22:
        return {"error": f"only {len(c)} bars"}
    s20, s50 = D.sma(c, 20), D.sma(c, 50)
    atr = D.atr_wilder(h, l, c, 14) if len(h) == len(c) == len(l) else None
    rv = D.realized_vol_series(c, 20)
    return {"last": c[-1], "last_date": _last_date(df), "chg_1d": D.pct_change_n(c, 1),
            "chg_5d": D.pct_change_n(c, 5), "d20": D._pct(c[-1], s20) if s20 else None,
            "d50": D._pct(c[-1], s50) if s50 else None,
            "atr_pct": (atr / c[-1] * 100) if atr and c[-1] else None,
            "rv20": rv[-1] * 100 if rv else None, "rsi": D.rsi_wilder(c, 14),
            "hi20": max(c[-20:]), "lo20": min(c[-20:])}


def index_outlook(*, today: date | None = None) -> dict:
    t0 = time.time()
    out = {"built_at": datetime.now().isoformat(timespec="seconds"), "errors": []}
    try:
        hist = _bars(OUTLOOK_IDX + OUTLOOK_EXTRA) or {}
    except Exception as e:
        hist = {}
        out["errors"].append(f"bars: {e}")
    out["index"] = {}
    for s in OUTLOOK_IDX:
        try:
            out["index"][s] = _idx_state(s, hist.get(s))
        except Exception as e:
            out["index"][s] = {"error": str(e)[:80]}
    ex = {}
    for s in OUTLOOK_EXTRA:
        c = _closes(hist.get(s))
        if len(c) >= 2:
            ex[s] = {"last": c[-1], "chg_1d": D.pct_change_n(c, 1), "chg_5d": D.pct_change_n(c, 5),
                     "d1": c[-1] - c[-2], "d5": (c[-1] - c[-6]) if len(c) >= 6 else None}
    out["extra"] = ex
    v9, v, v3 = (ex.get("^VIX9D") or {}).get("last"), (ex.get("^VIX") or {}).get("last"), (ex.get("^VIX3M") or {}).get("last")
    if v and v3:
        if v9 and v9 > v:
            term = "front inverted (VIX9D > VIX: near-term stress priced)"
        elif v > v3:
            term = "backwardation (VIX > VIX3M: stress)"
        else:
            term = "contango (calm term structure)"
        out["vix_term"] = f"VIX9D {_f(v9, 1)} / VIX {_f(v, 1)} / VIX3M {_f(v3, 1)} → {term}"
    out["implied"] = {}
    for s in ("SPY", "QQQ"):
        spot = (out["index"].get(s) or {}).get("last")
        out["implied"][s] = _implied_move(s, spot) if spot else [{"error": "no spot"}]
    out["calendar"] = econ_calendar(5, today=today)
    out["posture"] = _posture_ctx(_read_posture())
    out["build_seconds"] = round(time.time() - t0, 2)
    return out


def index_outlook_text(max_tokens: int = 700) -> str:
    """Deterministic index state for 1-5 session calls. Never raises."""
    try:
        o = index_outlook()
        L = [f"=== INDEX OUTLOOK FACTS (for 1-5 session calls; deterministic) · built {o['built_at']} · "
             f"src yfinance daily + option chain, calendar_events, .posture_cache.json ==="]
        L.append("sym | last (bar date) | 1d | 5d | vs20d | vs50d | ATR14% | RV20 | RSI | 20d range")
        for s in OUTLOOK_IDX:
            r = o["index"].get(s) or {}
            if "error" in r:
                L.append(f"{s} | n/a ({r['error']})")
                continue
            L.append(f"{s} | {_f(r['last'], 2)} ({r.get('last_date')}) | {_p(r['chg_1d'], 2)}% | {_p(r['chg_5d'], 2)}% | "
                     f"{_p(r['d20'])}% | {_p(r['d50'])}% | {_f(r['atr_pct'], 2)} | {_f(r['rv20'], 1)} | "
                     f"{_f(r['rsi'], 0)} | {_f(r['lo20'], 2)}-{_f(r['hi20'], 2)}")
        ex = o.get("extra") or {}
        bits = []
        if "^VIX" in ex:
            bits.append(f"VIX {_f(ex['^VIX']['last'], 2)} ({_p(ex['^VIX']['chg_1d'])}% 1d, {_p(ex['^VIX']['chg_5d'])}% 5d)")
        if "^TNX" in ex:
            t = ex["^TNX"]
            bits.append(f"10Y {_f(t['last'], 3)}% ({_p(t['d1'] * 100, 1)}bp 1d, "
                        f"{_p(t['d5'] * 100 if t['d5'] is not None else None, 1)}bp 5d)")
        if "DX-Y.NYB" in ex:
            bits.append(f"DXY {_f(ex['DX-Y.NYB']['last'], 2)} ({_p(ex['DX-Y.NYB']['chg_1d'], 2)}% 1d, {_p(ex['DX-Y.NYB']['chg_5d'], 2)}% 5d)")
        if bits:
            L.append(" · ".join(bits))
        if o.get("vix_term"):
            L.append("Term: " + o["vix_term"])
        for s, ims in (o.get("implied") or {}).items():
            parts = []
            for im in ims:
                if im.get("move_pct") is not None:
                    parts.append(f"to {im.get('expiry')} ±{_f(im['move_pct'], 2)}% (±${_f(im['move_usd'], 2)}, ATM {im.get('strike')})")
                else:
                    parts.append(f"{im.get('expiry') or ''} n/a ({im.get('error')})".strip())
            L.append(f"{s} implied move (ATM straddle mid): " + "; ".join(parts))
        po = o.get("posture") or {}
        L.append(f"Posture ({po.get('as_of')}): {po.get('posture_line') or 'n/a'} · phase {po.get('cycle_phase') or 'n/a'} · "
                 f"leaders {','.join(po.get('leaders') or []) or 'n/a'} · laggards {','.join(po.get('laggards') or []) or 'n/a'}")
        cal = o.get("calendar") or []
        L.append("Next 5 sessions econ/central-bank (ET):" if cal else "Next 5 sessions: no econ/central-bank calendar_events on file")
        for c in cal:
            fl = f" [{'/'.join(c['flags'])}]" if c.get("flags") else ""
            tm = (c.get("time") or "").replace(" -0400", "").replace(" -0500", "")
            L.append(f"- {c['date']} {tm} {c['title']}{fl}".replace("  ", " "))
        if o.get("errors"):
            L.append("errors: " + "; ".join(o["errors"])[:160])
        budget = max(1, int(max_tokens)) * 4
        text = "\n".join(L)
        if len(text) > budget:          # drop medium-impact calendar lines first, then hard cut
            L2 = [x for x in L if not (x.startswith("- ") and "[" not in x)]
            text = "\n".join(L2)
        if len(text) > budget:
            text = text[:budget - 14].rstrip() + "\n…[truncated]"
        return text
    except Exception as e:
        return f"INDEX OUTLOOK unavailable: {e}"


if __name__ == "__main__":      # manual smoke: python3 mason_etf_screen.py
    t = time.time()
    s = etf_screen(force=True)
    print(f"[etf_screen {time.time() - t:.1f}s rows={len(s.get('rows', []))} errors={s.get('errors')}]")
    txt = etf_screen_text()
    print(txt, f"\n[~{_tok(txt)} tokens]\n")
    t = time.time()
    o = index_outlook_text()
    print(o, f"\n[~{_tok(o)} tokens · {time.time() - t:.1f}s]")
