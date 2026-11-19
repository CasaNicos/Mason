"""
mason_dossier.py — Catalyst Desk TOOL 2: deterministic research dossiers (NO LLM).
See docs/prompts/CATALYST_DESK_RESEARCH_LAYER.md §3.2 and the binding interface in
docs/prompts/CATALYST_DESK_CONTRACTS.md §4.

Why this exists (Nico's reviews, research-layer doc §0): the director was reasoning
from a 2.5K-char qwen paraphrase with no competitors, no "what affects it / what it
affects", no bull/bear debate, no recent history and no real option contracts to
pick from. A dossier is the fact pack that fixes that: every number comes from a
named source and carries an as_of stamp, and the director picks single-leg
calls/puts ONLY from `options.chain_sample` (real, quoted contracts).

Public API (contracts §4):
  * build_dossier(ticker, *, max_age_seconds=None, force=False) -> dict
  * build_macro_dossier(event_kinds, *, headline_hint="", max_age_seconds=None, force=False) -> dict
  * dossier_text(d, *, max_tokens=4000) -> str      (handles ticker AND macro payloads)
  * macro_dossier_text(d, *, max_tokens=4000) -> str (alias, for readability at call sites)
  * dossier_enabled() -> bool                        (catalyst.dossier.enabled, for the integrator)

Design rules:
  * Never raises. Each section degrades independently to {"error": "..."}; the
    error also lands in payload["errors"] as "section: msg".
  * Sections run in a thread pool against one global build deadline
    (BUILD_BUDGET_S), so one slow Yahoo/SEC endpoint can't blow the ~15s target.
  * Cached in the `dossiers` table (TTL from catalyst.dossier.*_ttl_seconds);
    shared daily series (SPY, sector ETFs, peers) are also cached in-process for
    _SERIES_TTL seconds so a multi-ticker run downloads them once.
  * The functions work when called directly; catalyst.dossier.enabled gates only
    AUTOMATIC use, and the integrator does that gating (dossier_enabled()).
  * Network access goes through the small hook functions below (_ticker,
    _download, _live_quote, _edgar_json, _edgar_cik) so the tests can swap in
    offline fixtures.
"""

from __future__ import annotations

import html
import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, date, timedelta
from pathlib import Path

import mason_db

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "mason.config.json"
UNIVERSE_FILE = BASE / "universe_broad.json"
DEBATES_DIR = BASE / "Debates"
VERDICTS_DIR = BASE / "Verdicts"

SCHEMA_TICKER = "dossier/1.0"
SCHEMA_MACRO = "macro_dossier/1.0"
CREATED_BY = "mason_dossier"

BUILD_BUDGET_S = 14.0        # whole-dossier deadline (target < 15s per ticker)
INFO_WAIT_S = 7.0            # stage 2 (tape/options/peers) needs .info first
HIST_PERIOD = "1y"           # one period everywhere so the series cache is shared
_SERIES_TTL = 600            # in-process daily-series cache (seconds)
_DEFAULT_TICKER_TTL = 1800
_DEFAULT_MACRO_TTL = 900

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


# ── config (read per call, mtime-cheap — same idea as mason_news_service) ─────

_cfg_cache = {"mtime": None, "cfg": {}}


def _dossier_cfg() -> dict:
    try:
        m = CONFIG_FILE.stat().st_mtime
        if _cfg_cache["mtime"] != m:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            _cfg_cache["cfg"] = ((cfg.get("catalyst") or {}).get("dossier") or {})
            _cfg_cache["mtime"] = m
        return _cfg_cache["cfg"]
    except Exception:
        return {}


def dossier_enabled() -> bool:
    """catalyst.dossier.enabled — gates AUTOMATIC dossier builds (integrator's call)."""
    return bool(_dossier_cfg().get("enabled", False))


def _ttl(kind: str) -> int:
    cfg = _dossier_cfg()
    key, dflt = (("ticker_ttl_seconds", _DEFAULT_TICKER_TTL) if kind == "ticker"
                 else ("macro_ttl_seconds", _DEFAULT_MACRO_TTL))
    try:
        return int(cfg.get(key, dflt))
    except Exception:
        return dflt


# ── network hooks (tests monkeypatch these) ───────────────────────────────────

# Yahoo shares ONE cookie+crumb per process; when too many requests are in flight a
# 401 "Invalid Crumb" makes yfinance refresh it, which 401s the others (measured
# 2026-09-25 with ~10 concurrent calls: options/estimates came back empty). Cap
# concurrent Yahoo work; DB/SEC sections are unaffected.
_YF_SEM = threading.BoundedSemaphore(3)


class _yf_slot:
    def __enter__(self):
        _YF_SEM.acquire()

    def __exit__(self, *exc):
        _YF_SEM.release()
        return False


def _yf():
    import yfinance as yf
    return yf


def _raw_ticker(sym: str):
    import mason_symbols
    return _yf().Ticker(mason_symbols.yf_symbol(sym))      # BRK.B → BRK-B (R2-A)


def _yf_refusals() -> int:
    try:
        import mason_logsetup
        return mason_logsetup.yf_refusals()
    except Exception:
        return 0


def _retry_on_refusal_enabled() -> bool:
    """catalyst.dossier.yahoo_retry_on_refusal (default true; false = the plain yf.Ticker, as before)."""
    return _dossier_cfg().get("yahoo_retry_on_refusal", True) is not False


_REFUSAL_PAUSE_S = 0.6


class _RefusalRetryTicker:
    """yf.Ticker stand-in (2026-11-16, feeds): a read or call that Yahoo REFUSED (HTTP 401 Invalid
    Crumb / "unable to access this feature"; yfinance logs it and hands back an empty answer, which
    looks exactly like "Yahoo has no data") is asked once more, after a short pause, on a FRESH
    yf.Ticker (the first object keeps the empty answer in its own cache). Refusals are told apart
    from real "no data" by mason_logsetup.yf_refusals() (per-thread count of yfinance's 401 lines),
    so nothing is asked twice unless Yahoo actually refused. A second refusal is logged once per
    symbol/part/day (mason_logsetup.note_yf_loss). Measured 11-16: 19 refusals in dossier builds;
    NVDA's 14:06 pack lost AVGO's peer numbers, other packs lost analyst-action / calendar items."""

    def __init__(self, sym: str):
        self._sym = sym
        self._tk = _raw_ticker(sym)

    def _again(self, name, call):
        time.sleep(_REFUSAL_PAUSE_S)
        fresh = _raw_ticker(self._sym)
        n1 = _yf_refusals()
        try:
            out = call(fresh)
        finally:
            if _yf_refusals() > n1:
                try:
                    import mason_logsetup
                    mason_logsetup.note_yf_loss("dossier", self._sym, name)
                except Exception:
                    pass
        self._tk = fresh
        return out

    def __getattr__(self, name):
        if name in ("_tk", "_sym"):             # not set yet (copy/pickle build the object without
            raise AttributeError(name)          # __init__): never recurse through __getattr__
        if name.startswith("_"):
            return getattr(self._tk, name)
        n0 = _yf_refusals()
        try:
            val, exc = getattr(self._tk, name), None
        except Exception as e:
            val, exc = None, e
        if callable(val) and exc is None:
            return self._method(name)
        if _yf_refusals() == n0:
            if exc is not None:
                raise exc
            return val
        return self._again(name, lambda tk: getattr(tk, name))

    def _method(self, name):
        def call(*a, **kw):
            n0 = _yf_refusals()
            try:
                out, exc = getattr(self._tk, name)(*a, **kw), None
            except Exception as e:
                out, exc = None, e
            if _yf_refusals() == n0:
                if exc is not None:
                    raise exc
                return out
            return self._again(name, lambda tk: getattr(tk, name)(*a, **kw))
        return call


def _ticker(sym: str):
    if _retry_on_refusal_enabled():
        return _RefusalRetryTicker(sym)
    return _raw_ticker(sym)


def _download(symbols: list) -> dict:
    """Batch daily OHLCV for `symbols` → {sym: DataFrame[Open,High,Low,Close,Volume]}.
    One yf.download call; symbols Yahoo can't serve are simply absent. Keyed by the
    caller's symbols; Yahoo is asked for its own spelling (BRK.B → BRK-B)."""
    if not symbols:
        return {}
    import mason_symbols
    ysym = mason_symbols.yf_symbols(symbols)
    with _yf_slot():
        df = _yf().download(list(dict.fromkeys(ysym.values())), period=HIST_PERIOD, interval="1d",
                            group_by="ticker", auto_adjust=True, progress=False,
                            threads=False, timeout=10)
    out = {}
    if df is None or len(df) == 0:
        return out
    cols = getattr(df, "columns", None)
    multi = cols is not None and getattr(cols, "nlevels", 1) > 1
    for s in symbols:
        try:
            sub = df[ysym[s]] if multi else df
            sub = sub.dropna(subset=["Close"])
            if len(sub):
                out[s] = sub
        except Exception:
            continue
    return out


def _live_quote(sym: str) -> dict:
    """mason_brief.get_quote_yf — the accurate live quote (.info regularMarket*;
    NOT fast_info.previous_close, which is wrong)."""
    from mason_brief import get_quote_yf
    with _yf_slot():
        return get_quote_yf(sym)


def _edgar_json(url: str):
    import mason_edgar
    r = mason_edgar._get(url, timeout=6)
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _edgar_cik(sym: str):
    """Ticker → CIK from SEC's cached company_tickers map only (no full-text search)."""
    import mason_edgar
    hit = mason_edgar._load_tickers_map().get(sym.upper())
    return hit[0] if hit else None


# ── small helpers ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _num(v):
    """float or None (NaN/inf/garbage → None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _r(v, nd=2):
    f = _num(v)
    return None if f is None else round(f, nd)


def _pct(a, b):
    """(a/b - 1) * 100, or None."""
    a, b = _num(a), _num(b)
    if a is None or not b:
        return None
    return (a / b - 1.0) * 100.0


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except Exception:
        return None


# ── in-process series cache (SPY / sector ETFs / peers shared within a run) ───

_series_cache: dict = {}
_series_lock = threading.Lock()


def clear_series_cache():
    with _series_lock:
        _series_cache.clear()


def _history(symbols: list) -> dict:
    """{sym: DataFrame} for symbols, downloading only what isn't cached & fresh."""
    now = time.time()
    out, missing = {}, []
    with _series_lock:
        for s in symbols:
            hit = _series_cache.get(s)
            if hit and now - hit[0] < _SERIES_TTL:
                out[s] = hit[1]
            elif s not in missing:
                missing.append(s)
    if missing:
        got = _download(missing) or {}
        with _series_lock:
            for s, df in got.items():
                _series_cache[s] = (time.time(), df)
                out[s] = df
    return out


def _col(df, name) -> list:
    """A DataFrame column as a plain float list (NaN dropped)."""
    try:
        return [f for f in (_num(x) for x in df[name].tolist()) if f is not None]
    except Exception:
        return []


def _bar_dates(df) -> list:
    try:
        return [d.date() if hasattr(d, "date") else _to_date(d) for d in df.index]
    except Exception:
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Pure math (unit-tested on fixtures)
# ═════════════════════════════════════════════════════════════════════════════

def sma(values: list, n: int):
    if not values or len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def rsi_wilder(closes: list, n: int = 14):
    """RSI with Wilder smoothing: seed = simple mean of the first n gains/losses,
    then avg = (prev*(n-1) + x)/n. Needs n+1 closes."""
    if not closes or len(closes) < n + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def atr_wilder(highs: list, lows: list, closes: list, n: int = 14):
    """ATR with Wilder smoothing. TR_i = max(H-L, |H-Cprev|, |L-Cprev|) for i>=1;
    seed = mean of the first n TRs. Needs n+1 bars."""
    m = min(len(highs), len(lows), len(closes))
    if m < n + 1:
        return None
    trs = []
    for i in range(1, m):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr = (atr * (n - 1) + trs[i]) / n
    return atr


def pct_change_n(closes: list, n: int):
    """% change over the last n bars: closes[-1] / closes[-1-n] - 1."""
    if not closes or len(closes) < n + 1:
        return None
    return _pct(closes[-1], closes[-1 - n])


def realized_vol_series(closes: list, n: int = 20) -> list:
    """Rolling n-day close-to-close realized vol (annualized, sample stdev of log
    returns × √252), one value per window end."""
    if not closes or len(closes) < n + 1:
        return []
    rets = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        rets.append(math.log(b / a) if a and b and a > 0 and b > 0 else 0.0)
    out = []
    for end in range(n, len(rets) + 1):
        w = rets[end - n:end]
        mu = sum(w) / n
        var = sum((x - mu) ** 2 for x in w) / (n - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out


def iv_rank_proxy(atm_iv, closes: list, n: int = 20) -> dict:
    """IV-rank PROXY (free data has no IV history): where today's ATM IV sits in
    the trailing ~1y RANGE OF 20-DAY REALIZED VOL:
        proxy = clip((ATM_IV - min HV20) / (max HV20 - min HV20) * 100, 0, 100)
    Also returns IV/HV20 (current). >100 raw (IV above every realized regime of the
    year) is clipped to 100 and flagged. NOT a true IV rank — it compares implied to
    realized, so earnings/event premium inflates it by design."""
    iv = _num(atm_iv)
    hv = realized_vol_series(closes, n)[-252:]
    if iv is None or not hv:
        return {"value": None, "method": "unavailable (need ATM IV + 1y closes)"}
    lo, hi, now = min(hv), max(hv), hv[-1]
    raw = ((iv - lo) / (hi - lo) * 100.0) if hi > lo else None
    val = None if raw is None else max(0.0, min(100.0, raw))
    return {
        "value": _r(val, 0),
        "raw": _r(raw, 0),
        "clipped": raw is not None and (raw > 100 or raw < 0),
        "hv20_now": _r(now * 100, 1),
        "hv20_1y_min": _r(lo * 100, 1),
        "hv20_1y_max": _r(hi * 100, 1),
        "iv_hv_ratio": _r(iv / now, 2) if now else None,
        "method": "ATM IV vs trailing-1y range of 20d realized vol (proxy, not true IV rank)",
    }


def est_change_pct(cur, base):
    """Estimate revision in % of |base| (so -0.58 → -0.80 reads as -37.9%, i.e. WORSE),
    None when the estimate crossed zero or the base is ~0 (a % is meaningless there)."""
    c, b = _num(cur), _num(base)
    if c is None or b is None or abs(b) < 0.01 or (c * b < 0):
        return None
    return (c - b) / abs(b) * 100.0


def option_mid(bid, ask, last=None):
    """Mid if both sides quoted, else last trade, else None."""
    b, a = _num(bid), _num(ask)
    if b is not None and a is not None and b > 0 and a > 0 and a >= b:
        return (a + b) / 2.0
    l = _num(last)
    return l if l and l > 0 else None


def implied_move(call_mid, put_mid, spot) -> dict:
    """ATM straddle → expected move to expiry: ±straddle, ±straddle/spot."""
    c, p, s = _num(call_mid), _num(put_mid), _num(spot)
    if c is None or p is None or not s:
        return {}
    st = c + p
    return {"straddle": _r(st, 2), "move_usd": _r(st, 2), "move_pct": _r(st / s * 100, 2)}


# ═════════════════════════════════════════════════════════════════════════════
# Static knowledge tables (curated 2026-09; best-effort — labelled as such)
# ═════════════════════════════════════════════════════════════════════════════

SECTOR_ETF = {
    "Technology": "XLK", "Communication Services": "XLC", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Healthcare": "XLV", "Financial Services": "XLF",
    "Industrials": "XLI", "Energy": "XLE", "Utilities": "XLU", "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    # GICS spellings (universe_broad.json comes from Wikipedia GICS tables)
    "Information Technology": "XLK", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Health Care": "XLV", "Financials": "XLF",
    "Materials": "XLB",
}

# More specific benchmark when the industry has a liquid ETF (used for vs_sector).
INDUSTRY_ETF = {
    "Semiconductors": "SOXX", "Semiconductor Equipment & Materials": "SOXX",
    "Biotechnology": "XBI", "Software - Infrastructure": "IGV", "Software - Application": "IGV",
    "Banks - Regional": "KRE", "Aerospace & Defense": "ITA", "Oil & Gas E&P": "XOP",
    "Airlines": "JETS", "Internet Retail": "XLY", "Gold": "GDX", "Solar": "TAN",
}

# Ticker-level overrides (curated). Checked before universe/industry matching.
PEER_MAP = {
    "META": ["GOOGL", "SNAP", "PINS", "RDDT", "AMZN"],
    "GOOGL": ["META", "MSFT", "AMZN", "AAPL", "SNAP"], "GOOG": ["META", "MSFT", "AMZN", "AAPL", "SNAP"],
    "MSFT": ["GOOGL", "AMZN", "ORCL", "CRM", "AAPL"], "AAPL": ["MSFT", "GOOGL", "AMZN", "META", "DELL"],
    "AMZN": ["WMT", "MSFT", "GOOGL", "SHOP", "COST"], "NFLX": ["DIS", "WBD", "ROKU", "SPOT", "GOOGL"],
    "NVDA": ["AMD", "AVGO", "INTC", "QCOM", "MRVL"], "AMD": ["NVDA", "INTC", "AVGO", "QCOM", "ARM"],
    "AVGO": ["NVDA", "MRVL", "QCOM", "AMD", "TXN"], "TSM": ["UMC", "GFS", "INTC", "ASML", "AVGO"],
    "INTC": ["AMD", "NVDA", "TSM", "QCOM", "TXN"], "MU": ["WDC", "STX", "SNDK", "NVDA", "LRCX"],
    "ASML": ["AMAT", "LRCX", "KLAC", "TER", "TSM"], "AMAT": ["LRCX", "KLAC", "ASML", "TER", "TSM"],
    "LRCX": ["AMAT", "KLAC", "ASML", "TER", "MU"], "ARM": ["QCOM", "NVDA", "AMD", "AVGO", "INTC"],
    "SMCI": ["DELL", "HPE", "NVDA", "ANET", "VRT"], "DELL": ["HPE", "SMCI", "HPQ", "NVDA", "ANET"],
    "ORCL": ["MSFT", "AMZN", "GOOGL", "CRM", "SAP"], "PLTR": ["SNOW", "CRM", "NOW", "DDOG", "AI"],
    "CRWV": ["NBIS", "ORCL", "MSFT", "SMCI", "DELL"],
    "IONQ": ["RGTI", "QBTS", "QUBT", "IBM", "GOOGL"], "RGTI": ["IONQ", "QBTS", "QUBT", "IBM", "GOOGL"],
    "QBTS": ["IONQ", "RGTI", "QUBT", "IBM", "GOOGL"],
    "TSLA": ["RIVN", "LCID", "GM", "F", "TM"], "RKLB": ["ASTS", "LUNR", "PL", "BA", "LMT"],
    "HOOD": ["SCHW", "IBKR", "COIN", "SOFI", "VIRT"], "COIN": ["HOOD", "MSTR", "IBKR", "SCHW", "CME"],
    "LLY": ["NVO", "MRK", "ABBV", "AMGN", "PFE"], "NVO": ["LLY", "AMGN", "MRK", "PFE", "VKTX"],
    "LMT": ["NOC", "RTX", "GD", "LHX", "BA"],
}

INDUSTRY_PEERS = {
    "Semiconductors": ["NVDA", "AMD", "AVGO", "QCOM", "TXN", "INTC", "MU", "MRVL", "ADI"],
    "Semiconductor Equipment & Materials": ["ASML", "AMAT", "LRCX", "KLAC", "TER"],
    "Internet Content & Information": ["GOOGL", "META", "SNAP", "PINS", "RDDT"],
    "Software - Infrastructure": ["MSFT", "ORCL", "PLTR", "SNOW", "NET", "CRWD"],
    "Software - Application": ["CRM", "ADBE", "NOW", "INTU", "WDAY"],
    "Drug Manufacturers - General": ["LLY", "NVO", "JNJ", "MRK", "ABBV", "PFE", "AZN"],
    "Biotechnology": ["VRTX", "REGN", "AMGN", "GILD", "BIIB", "MRNA"],
    "Computer Hardware": ["DELL", "HPE", "SMCI", "ANET", "IONQ", "RGTI"],
    "Consumer Electronics": ["AAPL", "SONY", "GRMN"],
    "Auto Manufacturers": ["TSLA", "GM", "F", "RIVN", "TM"],
    "Banks - Diversified": ["JPM", "BAC", "C", "WFC"],
    "Capital Markets": ["GS", "MS", "SCHW", "HOOD", "IBKR"],
    "Aerospace & Defense": ["LMT", "RTX", "NOC", "GD", "BA", "RKLB"],
    "Oil & Gas Integrated": ["XOM", "CVX", "SHEL", "BP"],
    "Oil & Gas E&P": ["COP", "EOG", "OXY", "DVN"],
    "Internet Retail": ["AMZN", "BABA", "PDD", "MELI", "EBAY"],
    "Entertainment": ["NFLX", "DIS", "WBD", "SPOT"],
}

# Suppliers / customers / partners — curated for mega caps + the semis/AI chain.
# relation is FROM the subject's point of view ("supplier" = sells to the subject).
SUPPLY_CHAIN = {
    "NVDA": [("TSM", "supplier", "foundry + CoWoS packaging"), ("MU", "supplier", "HBM memory"),
             ("MSFT", "customer", "hyperscaler GPU capex"), ("META", "customer", "hyperscaler GPU capex"),
             ("AMZN", "customer", "hyperscaler GPU capex"), ("GOOGL", "customer", "hyperscaler GPU capex"),
             ("ORCL", "customer", "OCI GPU capex"), ("SMCI", "customer", "server OEM"),
             ("DELL", "customer", "server OEM")],
    "TSM": [("AAPL", "customer", "largest foundry customer"), ("NVDA", "customer", "AI GPUs, CoWoS"),
            ("AMD", "customer", "CPUs/GPUs"), ("AVGO", "customer", "custom ASICs"),
            ("QCOM", "customer", "mobile SoCs"), ("ASML", "supplier", "EUV lithography"),
            ("AMAT", "supplier", "deposition/etch tools"), ("LRCX", "supplier", "etch tools"),
            ("KLAC", "supplier", "process control")],
    "AMD": [("TSM", "supplier", "foundry"), ("MSFT", "customer", "MI-series GPUs"),
            ("META", "customer", "MI-series GPUs"), ("ORCL", "customer", "MI-series GPUs")],
    "AVGO": [("TSM", "supplier", "foundry"), ("GOOGL", "customer", "TPU custom ASIC"),
             ("META", "customer", "MTIA custom ASIC"), ("AAPL", "customer", "RF/wireless")],
    "META": [("NVDA", "supplier", "AI GPUs (largest capex line)"), ("AVGO", "supplier", "MTIA custom ASIC"),
             ("AMD", "supplier", "MI-series GPUs"), ("ANET", "supplier", "datacenter networking"),
             ("VRT", "supplier", "datacenter power/cooling"),
             ("GOOGL", "competitor", "digital ad budgets"), ("AMZN", "competitor", "digital ad budgets")],
    "AAPL": [("TSM", "supplier", "A/M-series chips"), ("AVGO", "supplier", "RF components"),
             ("QCOM", "supplier", "modems"), ("SWKS", "supplier", "RF front-end"),
             ("CRUS", "supplier", "audio/haptics ICs")],
    "MSFT": [("NVDA", "supplier", "AI GPUs"), ("AMD", "supplier", "AI GPUs"),
             ("CRWV", "supplier", "leased GPU capacity")],
    "GOOGL": [("AVGO", "supplier", "TPU co-design"), ("NVDA", "supplier", "AI GPUs"),
              ("TSM", "supplier", "TPU foundry")],
    "AMZN": [("NVDA", "supplier", "AI GPUs"), ("MRVL", "supplier", "custom silicon"),
             ("AMD", "supplier", "CPUs/GPUs")],
    "ORCL": [("NVDA", "supplier", "AI GPUs"), ("AMD", "supplier", "AI GPUs")],
    "MU": [("NVDA", "customer", "HBM"), ("AMD", "customer", "HBM"), ("AAPL", "customer", "DRAM/NAND"),
           ("AMAT", "supplier", "fab tools"), ("LRCX", "supplier", "fab tools")],
    "ASML": [("TSM", "customer", "EUV"), ("INTC", "customer", "EUV/High-NA"), ("MU", "customer", "EUV")],
    "AMAT": [("TSM", "customer", "fab tools"), ("INTC", "customer", "fab tools"), ("MU", "customer", "fab tools")],
    "LRCX": [("TSM", "customer", "etch"), ("MU", "customer", "etch (memory heavy)"), ("INTC", "customer", "etch")],
    "SMCI": [("NVDA", "supplier", "GPUs"), ("AMD", "supplier", "GPUs/CPUs")],
    "DELL": [("NVDA", "supplier", "GPUs"), ("CRWV", "customer", "AI servers")],
    "CRWV": [("NVDA", "supplier", "GPUs (also investor)"), ("DELL", "supplier", "servers"),
             ("MSFT", "customer", "GPU capacity"), ("META", "customer", "GPU capacity")],
    "VRT": [("MSFT", "customer", "DC power/cooling"), ("META", "customer", "DC power/cooling"),
            ("GOOGL", "customer", "DC power/cooling"), ("AMZN", "customer", "DC power/cooling")],
    "ANET": [("META", "customer", "DC networking"), ("MSFT", "customer", "DC networking")],
    "IONQ": [("AMZN", "partner", "AWS Braket distribution"), ("MSFT", "partner", "Azure Quantum distribution"),
             ("GOOGL", "partner", "Google Cloud marketplace")],
    "TSLA": [("NVDA", "supplier", "training compute"), ("ALB", "supplier", "lithium")],
    "LLY": [("NVO", "competitor", "GLP-1 obesity/diabetes duopoly")],
    "NVO": [("LLY", "competitor", "GLP-1 obesity/diabetes duopoly")],
}

# Macro sensitivities: industry beats sector; ticker extras appended.
_SECTOR_SENS = {
    "Technology": ["10Y yield ↑ compresses long-duration multiples", "USD ↑ = FX headwind on overseas revenue",
                   "AI-capex cycle / hyperscaler spend"],
    "Communication Services": ["digital ad spend tracks consumer/SMB cycle", "regulatory/antitrust (US, EU DMA)",
                               "10Y yield ↑ = multiple pressure"],
    "Consumer Cyclical": ["consumer spending / real wages", "tariffs on imported goods", "rates → financing costs"],
    "Consumer Defensive": ["input-cost inflation", "USD ↑ = FX headwind", "defensive bid in risk-off"],
    "Healthcare": ["drug-pricing policy (IRA negotiation, MFN)", "pharma tariffs", "FDA/regulatory calendar"],
    "Financial Services": ["yield-curve slope (NIM)", "credit spreads / defaults", "capital-markets activity"],
    "Industrials": ["ISM/PMI manufacturing cycle", "tariffs & supply chains", "defense/infrastructure budgets"],
    "Energy": ["crude & nat-gas prices", "OPEC+ supply policy", "Middle-East/Russia supply risk"],
    "Utilities": ["10Y yield (bond proxy)", "AI-datacenter power demand"],
    "Real Estate": ["10Y yield / mortgage rates", "credit availability"],
    "Basic Materials": ["China demand / stimulus", "USD (commodity pricing)", "commodity prices"],
}
_INDUSTRY_SENS = {
    "Semiconductors": ["US export controls on China", "Taiwan Strait geopolitical risk",
                       "AI-capex cycle", "memory pricing cycle", "tariffs on chips (Sec. 232)"],
    "Semiconductor Equipment & Materials": ["US export controls on China (big China revenue share)",
                                            "foundry/memory capex cycle", "Taiwan/Korea geopolitics"],
    "Biotechnology": ["FDA decisions/PDUFA & trial readouts", "rates (XBI is rate-sensitive)", "M&A appetite"],
    "Drug Manufacturers - General": ["drug-pricing policy (MFN/IRA)", "pharma tariffs", "GLP-1 pricing/competition"],
    "Internet Content & Information": ["digital ad cycle", "EU DMA / US antitrust", "AI capex vs. ROI narrative"],
    "Computer Hardware": ["AI server demand", "speculative risk appetite (small caps)", "component supply (GPUs/HBM)"],
}
# Names whose Yahoo industry label misleads (IONQ = "Computer Hardware"): use only
# the ticker list.
_TICKER_SENS_REPLACE = {"IONQ", "RGTI", "QBTS"}
_TICKER_SENS = {
    "IONQ": ["speculative/momentum risk appetite (retail flows)", "government quantum funding & contracts",
             "rates (unprofitable long-duration growth)"],
    "RGTI": ["speculative/momentum risk appetite", "government quantum funding"],
    "QBTS": ["speculative/momentum risk appetite", "government quantum funding"],
    "TSM": ["Taiwan Strait risk (direct)", "US tariffs on chips / Arizona fab economics", "TWD/USD"],
    "LLY": ["GLP-1 Medicare coverage / pricing policy", "compounded-GLP-1 competition"],
    "META": ["AI-capex guidance (FCF sensitivity)", "EU regulatory fines/DMA"],
}


# ═════════════════════════════════════════════════════════════════════════════
# Macro transmission map (static; keyed on event_kind + headline keywords)
# ═════════════════════════════════════════════════════════════════════════════
# Each rule: key, kinds it belongs to, keyword regex (None ⇒ generic fallback for
# its kinds), and assets [(sym, sign, rationale)]. sign ∈ '+', '-', '±'.
# Keyword rules match on the headline hint REGARDLESS of kind (an OPEC headline
# tagged 'geopolitical' still gets the OPEC map); generic rules only fire for a
# requested kind that no keyword rule of that kind matched.

_CN = r"(?:china|chinese|beijing|xi\b|xi jinping)"
_TALK = r"(?:summit|talks?|meet(?:ing|s)?|deal|truce|d[ée]tente|agreement|call|visit|framework)"

TRANSMISSION_RULES = [
    {"key": "us_china_detente", "kinds": ["geopolitical", "trade_policy"],
     "kw": rf"(?:{_CN}.{{0,60}}{_TALK}|{_TALK}.{{0,60}}{_CN}|trump[- ]xi)",
     "assets": [
         ("FXI", "+", "China large caps re-rate on lower tariff/sanction risk"),
         ("KWEB", "+", "China internet most sensitive to US-China risk premium"),
         ("BABA", "+", "bellwether ADR; delisting/tariff premium compresses"),
         ("ZS=F", "+", "soybeans: Chinese ag purchases are the usual first concession"),
         ("HG=F", "+", "copper: China demand/industrial optimism"),
         ("SOXX", "±", "tariff relief + China sales vs. export-control concessions uncertain"),
         ("CL=F", "±", "demand optimism vs. no supply change — ambiguous, reason it out"),
         ("AAPL", "+", "China supply chain + ~20% Greater-China revenue"),
         ("DX-Y.NYB", "-", "risk-on softens safe-haven USD; CNY firming"),
         ("NOTE", "±", "if talks stall/collapse, every sign above flips"),
     ]},
    {"key": "tariff_escalation", "kinds": ["trade_policy"],
     "kw": r"(?:tariffs?.{0,40}(?:hike|rais|impos|escalat|threat|doubl|increas|slap)"
           r"|(?:impos|slap|hike|rais|threat|doubl|new|higher|sweeping)\w*.{0,30}tariffs?"
           r"|trade war|retaliat|section 301|section 232)",
     "assets": [
         ("SPY", "-", "margin/earnings hit + growth scare"),
         ("FXI", "-", "direct target of US tariffs"),
         ("XLI", "-", "input costs + supply-chain disruption"),
         ("XLY", "-", "import-heavy retail/autos pay the tariff"),
         ("ZS=F", "-", "ag exports are the usual retaliation target"),
         ("DX-Y.NYB", "+", "terms-of-trade + haven bid"),
         ("GC=F", "+", "uncertainty hedge"),
     ]},
    {"key": "tariff_relief", "kinds": ["trade_policy"],
     "kw": r"(?:tariffs?.{0,40}(?:cut|paus|exempt|lift|remov|delay|reduc|roll ?back|suspend)"
           r"|(?:cut|paus|exempt|lift|remov|delay|reduc|roll ?back|suspend)\w*.{0,30}tariffs?"
           r"|trade deal|trade agreement)",
     "assets": [
         ("SPY", "+", "earnings-risk premium falls"),
         ("XLY", "+", "import-cost relief for retail/autos"),
         ("XLI", "+", "supply-chain relief"),
         ("FXI", "+", "if China included"),
         ("GC=F", "-", "less uncertainty hedging"),
     ]},
    {"key": "export_controls", "kinds": ["trade_policy", "geopolitical", "legal_regulatory"],
     "kw": r"(?:export control|export ban|chip ban|entity list|(?:chip|semiconductor|nvidia|gpu).{0,40}(?:ban|restrict|curb|licen))",
     "assets": [
         ("SOXX", "-", "China is 20-40% of revenue for many semis"),
         ("NVDA", "-", "China data-center GPU sales at risk"),
         ("AMAT", "-", "equipment makers have the highest China exposure"),
         ("LRCX", "-", "equipment makers have the highest China exposure"),
         ("FXI", "-", "tech-war escalation"),
     ]},
    {"key": "taiwan_tension", "kinds": ["geopolitical"],
     "kw": r"(?:taiwan.{0,50}(?:strait|military|drill|blockade|invasion|tension|warship|pla)|pla.{0,30}taiwan)",
     "assets": [
         ("TSM", "-", "direct: ~90% of leading-edge capacity on-island"),
         ("SOXX", "-", "whole chain depends on TSMC supply"),
         ("ITA", "+", "defense spending bid"),
         ("LMT", "+", "defense prime"),
         ("GC=F", "+", "haven"),
         ("^VIX", "+", "tail-risk hedging"),
     ]},
    {"key": "middle_east_conflict", "kinds": ["geopolitical", "commodity"],
     "kw": r"(?:iran|israel|hormuz|houthi|red sea|hezbollah|gaza|saudi.{0,30}(?:attack|strike))",
     "assets": [
         ("CL=F", "+", "supply-risk premium (Hormuz carries ~20% of seaborne crude)"),
         ("BZ=F", "+", "Brent carries the Middle-East risk premium first"),
         ("XLE", "+", "producers re-rate on crude"),
         ("GC=F", "+", "haven"),
         ("JETS", "-", "jet-fuel cost"),
         ("ITA", "+", "defense"),
         ("^VIX", "+", "risk-off"),
     ]},
    {"key": "russia_ukraine", "kinds": ["geopolitical", "commodity"],
     "kw": r"(?:russia|ukrain|putin|kremlin|zelensk)",
     "assets": [
         ("NG=F", "±", "EU gas flows; US LNG export demand"),
         ("ZW=F", "+", "Black Sea wheat supply risk (flips on ceasefire)"),
         ("GC=F", "+", "haven (flips on ceasefire)"),
         ("ITA", "+", "European/US defense budgets"),
         ("CL=F", "±", "sanctions on Russian crude vs. demand"),
     ]},
    {"key": "ceasefire_peace", "kinds": ["geopolitical"],
     "kw": r"(?:ceasefire|cease-fire|peace (?:deal|talks|plan|agreement)|armistice)",
     "assets": [
         ("CL=F", "-", "war-risk premium unwinds"),
         ("GC=F", "-", "haven demand fades"),
         ("ITA", "-", "defense premium fades"),
         ("SPY", "+", "risk-on"),
     ]},
    {"key": "opec_cut", "kinds": ["commodity", "geopolitical"],
     "kw": r"(?:opec.{0,40}(?:cut|reduc|curb)|(?:output|production) cuts?)",
     "assets": [
         ("CL=F", "+", "lower supply"), ("BZ=F", "+", "lower supply"), ("XLE", "+", "producers"),
         ("JETS", "-", "fuel cost"), ("XLY", "-", "gasoline tax on the consumer"),
     ]},
    {"key": "opec_hike", "kinds": ["commodity", "geopolitical"],
     "kw": r"(?:opec.{0,40}(?:increas|hike|boost|rais|unwind)|(?:output|production) (?:increase|hike|boost))",
     "assets": [
         ("CL=F", "-", "more supply"), ("XLE", "-", "producers"), ("JETS", "+", "fuel cost relief"),
     ]},
    {"key": "hot_inflation", "kinds": ["macro_data"],
     "kw": r"(?:(?:cpi|pce|ppi|inflation).{0,40}(?:hot|above|beat|higher|accelerat|rise|rises|jump|surge|tops)|hotter.{0,20}(?:cpi|inflation))",
     "assets": [
         ("^TNX", "+", "fewer/later cuts priced"), ("TLT", "-", "duration sells off"),
         ("QQQ", "-", "long-duration growth de-rates"), ("IWM", "-", "small caps most rate-sensitive"),
         ("DX-Y.NYB", "+", "rate differential"), ("GC=F", "±", "inflation hedge vs. higher real yields"),
     ]},
    {"key": "cool_inflation", "kinds": ["macro_data"],
     "kw": r"(?:(?:cpi|pce|ppi|inflation).{0,40}(?:cool|below|soft|lower|slow|eas|miss|decelerat))",
     "assets": [
         ("^TNX", "-", "cuts pulled forward"), ("TLT", "+", "duration rallies"),
         ("QQQ", "+", "growth re-rates"), ("IWM", "+", "small caps most rate-sensitive"),
         ("DX-Y.NYB", "-", "rate differential narrows"),
     ]},
    {"key": "strong_jobs", "kinds": ["macro_data"],
     "kw": r"(?:(?:payroll|nfp|jobs|hiring).{0,40}(?:beat|strong|surge|above|jump|tops|hot)|unemployment.{0,30}(?:fall|drop|lower))",
     "assets": [
         ("^TNX", "+", "fewer cuts"), ("DX-Y.NYB", "+", "rate differential"),
         ("TLT", "-", "duration"), ("SPY", "±", "growth good vs. rates bad"),
     ]},
    {"key": "weak_jobs", "kinds": ["macro_data"],
     "kw": r"(?:(?:payroll|nfp|jobs|hiring).{0,40}(?:miss|weak|below|slump|fell|drop|contract)|unemployment.{0,30}(?:rise|jump|climb|higher))",
     "assets": [
         ("^TNX", "-", "cuts priced in"), ("TLT", "+", "duration"), ("GC=F", "+", "lower real yields"),
         ("SPY", "±", "cuts good vs. recession fear"), ("IWM", "±", "rate relief vs. growth fear"),
     ]},
    {"key": "fed_dovish", "kinds": ["central_bank"],
     "kw": r"(?:(?:fed|fomc|powell|rate).{0,40}(?:cut|dovish|eas|lower rates)|rate cut)",
     "assets": [
         ("IWM", "+", "floating-rate debt relief"), ("TLT", "+", "duration"),
         ("DX-Y.NYB", "-", "rate differential"), ("GC=F", "+", "lower real yields"),
         ("KRE", "+", "funding-cost relief"), ("XLF", "±", "NIM compression vs. credit relief"),
     ]},
    {"key": "fed_hawkish", "kinds": ["central_bank"],
     "kw": r"(?:(?:fed|fomc|powell).{0,40}(?:hike|hawkish|higher for longer|no cut|hold|pause))",
     "assets": [
         ("TLT", "-", "duration"), ("QQQ", "-", "long-duration growth"),
         ("DX-Y.NYB", "+", "rate differential"), ("IWM", "-", "small caps rate-sensitive"),
     ]},
    {"key": "china_stimulus", "kinds": ["central_bank", "macro_data", "geopolitical"],
     "kw": r"(?:(?:china|pboc|beijing).{0,40}(?:stimulus|rate cut|rrr|easing|support package|bazooka))",
     "assets": [
         ("FXI", "+", "direct"), ("KWEB", "+", "high beta to China policy"),
         ("HG=F", "+", "construction/industrial demand"), ("FCX", "+", "copper miner"),
         ("XLB", "+", "materials demand"),
     ]},
    {"key": "sanctions_oil", "kinds": ["geopolitical", "commodity", "trade_policy"],
     "kw": r"(?:sanction.{0,50}(?:oil|crude|russia|iran|venezuela)|(?:oil|crude).{0,30}sanction)",
     "assets": [
         ("CL=F", "+", "sanctioned barrels leave the market"), ("BZ=F", "+", "same"),
         ("XLE", "+", "US producers fill the gap"),
     ]},
    {"key": "gulf_hurricane", "kinds": ["commodity", "other"],
     "kw": r"(?:hurricane|tropical storm).{0,60}(?:gulf|texas|louisiana)?",
     "assets": [
         ("NG=F", "+", "Gulf production/LNG export shut-ins"), ("CL=F", "+", "offshore GoM output"),
         ("XLE", "±", "producers up vs. refinery outages"),
     ]},
    # ── generic fallbacks (kw None) ──
    {"key": "geopolitical_generic", "kinds": ["geopolitical"], "kw": None,
     "assets": [("GC=F", "+", "default haven bid"), ("^VIX", "+", "risk-off hedging"),
                ("TLT", "+", "flight to quality"), ("SPY", "-", "risk-off (weak default)")]},
    {"key": "trade_policy_generic", "kinds": ["trade_policy"], "kw": None,
     "assets": [("FXI", "±", "direction depends on escalation vs. relief"),
                ("SPY", "±", "same"), ("DX-Y.NYB", "±", "same")]},
    {"key": "central_bank_generic", "kinds": ["central_bank"], "kw": None,
     "assets": [("TLT", "±", "hawkish −, dovish +"), ("DX-Y.NYB", "±", "hawkish +, dovish −"),
                ("IWM", "±", "most rate-sensitive equity")]},
    {"key": "macro_data_generic", "kinds": ["macro_data"], "kw": None,
     "assets": [("^TNX", "±", "hot data +, soft data −"), ("QQQ", "±", "inverse of yields"),
                ("DX-Y.NYB", "±", "tracks yields")]},
    {"key": "commodity_generic", "kinds": ["commodity"], "kw": None,
     "assets": [("CL=F", "±", "check which commodity the headline names"), ("XLE", "±", "")]},
]

for _rule in TRANSMISSION_RULES:
    _rule["_re"] = re.compile(_rule["kw"], re.I) if _rule["kw"] else None


def transmission_for(event_kinds, headline_hint: str = "") -> list:
    """Static transmission lookup → [{asset, expected_sign, rationale, rule}].
    Deterministic: rule-definition order, first occurrence of an (asset, rule) pair."""
    kinds = [str(k) for k in (event_kinds or [])]
    hint = str(headline_hint or "")
    matched, kinds_hit = [], set()
    for rule in TRANSMISSION_RULES:
        if rule["_re"] is not None and hint and rule["_re"].search(hint):
            matched.append(rule)
            kinds_hit.update(k for k in rule["kinds"] if k in kinds)
    for rule in TRANSMISSION_RULES:
        if rule["_re"] is None and any(k in kinds and k not in kinds_hit for k in rule["kinds"]):
            matched.append(rule)
    out = []
    for rule in matched:
        for sym, sign, why in rule["assets"]:
            out.append({"asset": sym, "expected_sign": sign, "rationale": why, "rule": rule["key"]})
    return out


CROSS_ASSET = [
    ("CL=F", "WTI crude"), ("BZ=F", "Brent crude"), ("NG=F", "US nat gas"), ("GC=F", "Gold"),
    ("HG=F", "Copper"), ("ZS=F", "Soybeans"), ("DX-Y.NYB", "US dollar index"),
    ("^TNX", "US 10Y yield (%)"), ("^VIX", "VIX"), ("SPY", "S&P 500"), ("QQQ", "Nasdaq-100"),
    ("IWM", "Russell 2000"), ("FXI", "China large caps"), ("KWEB", "China internet"),
    ("SOXX", "Semis"), ("XLE", "Energy"), ("XLF", "Financials"), ("TLT", "20Y+ Treasuries"),
]


# ═════════════════════════════════════════════════════════════════════════════
# Ticker sections — each takes plain inputs, returns a dict/list, may raise
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_info(t: str) -> dict:
    with _yf_slot():
        return _fetch_info_raw(t)


def _fetch_info_raw(t: str) -> dict:
    tk = _ticker(t)
    info = tk.info or {}
    cal = {}
    try:
        cal = tk.calendar or {}
    except Exception:
        cal = {}
    if not isinstance(cal, dict):
        cal = {}
    return {"info": info, "calendar": cal}


def _next_earnings(info: dict, cal: dict):
    """(date_iso, is_estimate) — Yahoo calendar first, then earningsTimestampStart,
    then earningsTimestamp; only dates today or later count."""
    today = date.today()
    cands = []
    for v in (cal.get("Earnings Date") or []):
        d = _to_date(v)
        if d:
            cands.append(d)
    for k in ("earningsTimestampStart", "earningsTimestamp"):
        ts = _num(info.get(k))
        if ts:
            cands.append(datetime.fromtimestamp(ts).date())
    fut = sorted(d for d in cands if d >= today)
    if not fut:
        return None, None
    return fut[0].isoformat(), bool(info.get("isEarningsDateEstimate"))


def _section_profile(bundle: dict) -> dict:
    info, cal = bundle.get("info") or {}, bundle.get("calendar") or {}
    if not info or not (info.get("shortName") or info.get("longName") or info.get("symbol")):
        raise ValueError("empty yfinance .info")
    ne, est = _next_earnings(info, cal)
    summ = str(info.get("longBusinessSummary") or "").strip()
    if len(summ) > 300:
        summ = summ[:297].rsplit(" ", 1)[0] + "…"
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"), "industry": info.get("industry"),
        "market_cap": _num(info.get("marketCap")),
        "float": _num(info.get("floatShares")),
        "short_pct": _r((_num(info.get("shortPercentOfFloat")) or 0) * 100, 2)
        if _num(info.get("shortPercentOfFloat")) is not None else None,
        "short_ratio_days": _r(info.get("shortRatio"), 1),
        "next_earnings": ne, "next_earnings_is_estimate": est,
        "beta": _r(info.get("beta"), 2), "pe": _r(info.get("trailingPE"), 1),
        "fwd_pe": _r(info.get("forwardPE"), 1),
        # Yahoo divides a USD market cap by local-currency revenue for ADRs → junk P/S.
        "ps": _r(info.get("priceToSalesTrailing12Months"), 1)
        if (info.get("financialCurrency") or "USD") == (info.get("currency") or "USD") else None,
        "employees": info.get("fullTimeEmployees"), "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "currency": info.get("currency"), "financial_currency": info.get("financialCurrency"),
        "quote_type": info.get("quoteType"),
        "summary": summ or None,
    }


def _frame_get(df, row_names, col):
    for rn in row_names:
        try:
            if rn in df.index:
                return _num(df.loc[rn, col])
        except Exception:
            continue
    return None


def _section_financials(t: str) -> dict:
    with _yf_slot():
        return _section_financials_raw(t)


def _section_financials_raw(t: str) -> dict:
    tk = _ticker(t)
    inc = tk.quarterly_income_stmt
    if inc is None or getattr(inc, "empty", True):
        raise ValueError("no quarterly_income_stmt")
    cf = None
    try:
        cf = tk.quarterly_cashflow
    except Exception:
        cf = None
    quarters = []
    for col in list(inc.columns)[:8]:
        rev = _frame_get(inc, ["Total Revenue", "Operating Revenue"], col)
        if rev is None:
            continue
        gp = _frame_get(inc, ["Gross Profit"], col)
        op = _frame_get(inc, ["Operating Income", "Total Operating Income As Reported"], col)
        eps = _frame_get(inc, ["Diluted EPS", "Basic EPS"], col)
        fcf = None
        if cf is not None and not getattr(cf, "empty", True) and col in cf.columns:
            fcf = _frame_get(cf, ["Free Cash Flow"], col)
        quarters.append({
            "period": _to_date(col).isoformat() if _to_date(col) else str(col),
            "revenue": rev, "eps": _r(eps, 2),
            "gross_margin": _r(gp / rev * 100, 1) if gp is not None and rev else None,
            "op_margin": _r(op / rev * 100, 1) if op is not None and rev else None,
            "fcf": fcf, "src": "yfinance",
        })
    surprises = []
    try:
        ed = tk.get_earnings_dates(limit=8)
        if ed is not None and len(ed):
            for idx, row in ed.iterrows():
                rep = _num(row.get("Reported EPS"))
                if rep is None:
                    continue
                surprises.append({"date": _to_date(idx).isoformat() if _to_date(idx) else str(idx),
                                  "eps_est": _r(row.get("EPS Estimate"), 2), "eps_actual": _r(rep, 2),
                                  "surprise_pct": _r(row.get("Surprise(%)"), 1)})
                if len(surprises) >= 4:
                    break
    except Exception:
        pass
    return {"quarters": quarters, "eps_surprises": surprises}


def _yoy(quarters: list) -> dict:
    """YoY for the latest two quarters: match each to the quarter ~1y earlier (±25d)."""
    def find_prior(q):
        d = _to_date(q["period"])
        if not d:
            return None
        for p in quarters:
            pd_ = _to_date(p["period"])
            if pd_ and abs((d - pd_).days - 365) <= 25:
                return p
        return None
    out = {}
    for label, q in zip(("latest", "prev"), quarters[:2]):
        p = find_prior(q)
        if not p:
            continue
        out[f"{label}_period"] = q["period"]
        out[f"{label}_revenue_yoy_pct"] = _r(_pct(q.get("revenue"), p.get("revenue")), 1)
        if q.get("eps") is not None and p.get("eps") not in (None, 0) and p["eps"] > 0:
            out[f"{label}_eps_yoy_pct"] = _r(_pct(q["eps"], p["eps"]), 1)
    return out


_EDGAR_REV = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")
_QFRAME = re.compile(r"^CY\d{4}Q[1-4]$")


def _edgar_quarterlies(js) -> dict:
    """companyconcept JSON → {end_date_iso: value} for calendar-quarter frames only."""
    out = {}
    try:
        units = (js or {}).get("units") or {}
        for _unit, rows in units.items():
            for row in rows or []:
                if _QFRAME.match(str(row.get("frame") or "")) and row.get("end"):
                    out[str(row["end"])[:10]] = _num(row.get("val"))
    except Exception:
        return {}
    return out


def _section_edgar(t: str) -> dict:
    cik = _edgar_cik(t)
    if not cik:
        raise ValueError("no SEC CIK (foreign/private filer?)")
    base = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/us-gaap"
    best, best_concept = {}, None
    for concept in _EDGAR_REV:
        q = _edgar_quarterlies(_edgar_json(f"{base}/{concept}.json"))
        if q and (not best or max(q) > max(best)):
            best, best_concept = q, concept
    if not best:
        raise ValueError("no quarterly us-gaap revenue frames (IFRS / 20-F filer?)")
    eps = _edgar_quarterlies(_edgar_json(f"{base}/EarningsPerShareDiluted.json"))
    ends = sorted(best, reverse=True)[:8]
    return {"cik": int(cik), "revenue_concept": best_concept,
            "quarters": [{"period": e, "revenue": best[e], "eps": _r(eps.get(e), 2)} for e in ends]}


def _merge_edgar(fin: dict, edg: dict) -> dict:
    """Cross-check yfinance quarters against EDGAR (match end dates ±10d); fill
    missing older quarters from EDGAR so we reach 8 when possible."""
    if not isinstance(fin, dict) or "quarters" not in fin or not isinstance(edg, dict) or "quarters" not in edg:
        return fin
    yq = fin["quarters"]
    used = set()
    for q in yq:
        d = _to_date(q["period"])
        for i, e in enumerate(edg["quarters"]):
            ed = _to_date(e["period"])
            if d and ed and abs((d - ed).days) <= 10:
                used.add(i)
                q["rev_edgar"] = e["revenue"]
                diff = _pct(q.get("revenue"), e["revenue"])
                q["rev_check"] = "ok" if diff is not None and abs(diff) <= 1.0 else (
                    f"MISMATCH {diff:+.1f}% vs EDGAR" if diff is not None else "n/a")
                if q.get("eps") is None and e.get("eps") is not None:
                    q["eps"] = e["eps"]
                break
    oldest = min((_to_date(q["period"]) for q in yq if _to_date(q["period"])), default=None)
    for i, e in enumerate(edg["quarters"]):
        if i in used or len(yq) >= 8:
            continue
        ed = _to_date(e["period"])
        if oldest is None or (ed and ed < oldest):
            yq.append({"period": e["period"], "revenue": e["revenue"], "eps": e.get("eps"),
                       "gross_margin": None, "op_margin": None, "fcf": None, "src": "edgar"})
    yq.sort(key=lambda q: q["period"], reverse=True)
    fin["quarters"] = yq[:8]
    fin["edgar_cik"] = edg.get("cik")
    return fin


def _est_row(df, period, cols):
    try:
        if df is None or getattr(df, "empty", True) or period not in df.index:
            return None
        row = df.loc[period]
        return {c: (row.get(c) if c == "currency" else
                    _num(row.get(c)) if c == "numberOfAnalysts" else _r(row.get(c), 4)) for c in cols}
    except Exception:
        return None


def _section_estimates(t: str) -> dict:
    with _yf_slot():
        return _section_estimates_raw(t)


def _section_estimates_raw(t: str) -> dict:
    tk = _ticker(t)
    ee = rv = et = er = None
    for attr in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
        try:
            v = getattr(tk, attr)
        except Exception:
            v = None
        if attr == "earnings_estimate":
            ee = v
        elif attr == "revenue_estimate":
            rv = v
        elif attr == "eps_trend":
            et = v
        else:
            er = v
    ecols = ["avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth", "currency"]
    rcols = ["avg", "low", "high", "yearAgoRevenue", "numberOfAnalysts", "growth", "currency"]
    out = {
        "eps_next_q": _est_row(ee, "0q", ecols), "eps_q_plus1": _est_row(ee, "+1q", ecols),
        "eps_fy": _est_row(ee, "0y", ecols), "eps_ny": _est_row(ee, "+1y", ecols),
        "rev_next_q": _est_row(rv, "0q", rcols), "rev_fy": _est_row(rv, "0y", rcols),
        "rev_ny": _est_row(rv, "+1y", rcols),
    }
    trend = {}
    for p in ("0q", "0y"):
        r = _est_row(et, p, ["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"])
        if r:
            r["chg_90d_pct"] = _r(est_change_pct(r.get("current"), r.get("90daysAgo")), 1)
            r["chg_30d_pct"] = _r(est_change_pct(r.get("current"), r.get("30daysAgo")), 1)
            trend[p] = r
    out["eps_trend"] = trend
    rev = {}
    for p in ("0q", "0y"):
        r = _est_row(er, p, ["upLast7days", "upLast30days", "downLast30days", "downLast7Days"])
        if r:
            rev[p] = {k: (int(v) if v is not None else None) for k, v in r.items()}
    out["revisions"] = rev
    if not any(out[k] for k in ("eps_next_q", "eps_fy", "rev_fy")) and not trend:
        raise ValueError("no analyst estimates on Yahoo")
    return out


def _street_not_found(t: str, mark: bool = False) -> bool:
    """Step 3 / R10: "no analyst coverage" is remembered for 24 h (mason_symbols).
    Never raises — without mason_symbols the street section works as it always did."""
    try:
        import mason_symbols
        if mark:
            return bool(mason_symbols.mark_not_found(t, "street", "no analyst coverage on Yahoo"))
        return bool(mason_symbols.is_not_found(t, "street"))
    except Exception:
        return False


def _section_street(t: str) -> dict:
    if _street_not_found(t):               # remembered: no repeat Yahoo lookup
        return _street_finish(t, {}, asked=False)
    with _yf_slot():
        out = _street_yahoo(t)
    return _street_finish(t, out)


def _street_yahoo(t: str) -> dict:
    tk = _ticker(t)
    out = {}
    try:
        pt = tk.analyst_price_targets or {}
    except Exception:
        pt = {}
    out.update({"pt_mean": _r(pt.get("mean"), 2), "pt_median": _r(pt.get("median"), 2),
                "pt_high": _r(pt.get("high"), 2), "pt_low": _r(pt.get("low"), 2)})
    try:
        rec = tk.recommendations
        if rec is not None and len(rec):
            r0 = rec.iloc[0]
            out["rec_counts_0m"] = {k: int(_num(r0.get(k)) or 0)
                                    for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
            if len(rec) > 1:
                r1 = rec.iloc[1]
                out["rec_counts_1m_ago"] = {k: int(_num(r1.get(k)) or 0)
                                            for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception:
        pass
    actions = []
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and len(ud):
            ud = ud.sort_index(ascending=False)
            for idx, row in ud.head(10).iterrows():
                actions.append({
                    "date": _to_date(idx).isoformat() if _to_date(idx) else str(idx),
                    "firm": row.get("Firm"), "action": row.get("Action"),
                    "from": row.get("FromGrade") or None, "to": row.get("ToGrade") or None,
                    "pt": _r(row.get("currentPriceTarget"), 2) or None,
                    "prior_pt": _r(row.get("priorPriceTarget"), 2) or None,
                    "pt_action": row.get("priceTargetAction") or None,
                })
    except Exception:
        pass
    out["recent_actions"] = actions
    return out


def _street_finish(t: str, out: dict, asked: bool = True) -> dict:
    """Stale-feed check + news fallback (DB only — runs outside the Yahoo slot)."""
    actions = out.get("recent_actions") or []
    newest = _to_date(actions[0]["date"]) if actions else None
    out["actions_src"] = "yfinance"
    if newest is None or (date.today() - newest).days > _STALE_ACTIONS_DAYS:
        try:
            fresh = _analyst_actions_from_news(t)
        except Exception:
            fresh = []
        out["yf_actions_newest"] = newest.isoformat() if newest else None
        if fresh:
            actions, out["actions_src"] = fresh, "news_events (Benzinga headlines, 90d)"
        else:
            # a 2-year-old PT change misleads more than it informs — drop it, say why
            out["actions_src"] = (f"none fresh (yfinance feed stale, newest {newest}; "
                                  f"no Benzinga analyst headlines in 90d)" if newest else "none")
            actions = []
    out["recent_actions"] = actions
    if out.get("pt_mean") is None and not actions and "rec_counts_0m" not in out:
        if asked:      # only a lookup that was really made renews the 24 h (a skipped one must not, or it never expires)
            _street_not_found(t, mark=True)
        raise ValueError("no street data on Yahoo")
    return out


# Yahoo's free upgrades_downgrades feed is frozen (measured 2026-09-25: META's
# newest row is 2024-09-30). Benzinga analyst headlines in news_events are current
# and follow a rigid template, so they're parsed as the fresher source.
_AA_RE = re.compile(
    r"^(?P<firm>[A-Z][\w&.,'\- ]{1,60}?) (?P<action>Maintains|Reiterates|Upgrades|Downgrades|"
    r"Initiates Coverage On|Resumes Coverage On|Assumes Coverage On) (?P<rest>.+)$")
_AA_PT_RE = re.compile(
    r"(?:(?P<dir>Raises|Lowers) Price Target to (?:C\$|\$)(?P<pt1>[\d,.]+)"
    r"|Maintains (?:C\$|\$)(?P<pt2>[\d,.]+) Price Target"
    r"|Announces Price Target of (?:C\$|\$)(?P<pt3>[\d,.]+)"
    r"|Announces (?:C\$|\$)(?P<pt4>[\d,.]+) Price Target)", re.I)
_STALE_ACTIONS_DAYS = 120


def parse_analyst_headline(headline: str):
    """Benzinga analyst template → {firm, action, to, pt, pt_action} or None."""
    h = html.unescape(headline or "").strip()
    m = _AA_RE.match(h)
    if not m:
        return None
    act, rest = m.group("action"), m.group("rest")
    grade = None
    if act in ("Maintains", "Reiterates"):
        g = re.match(r"(.+?) on .+", rest)
        grade = g.group(1) if g else None
    elif act in ("Upgrades", "Downgrades"):
        g = re.search(r" to ([A-Z][\w\- ]+?)(?:,|$)", rest)
        grade = g.group(1) if g else None
    else:
        g = re.search(r" with ([\w\- ]+?) Rating", rest)
        grade = g.group(1) if g else None
        act = "Initiates"
    pm = _AA_PT_RE.search(rest)
    pt, pt_action = None, None
    if pm:
        raw = next(x for x in (pm.group("pt1"), pm.group("pt2"), pm.group("pt3"), pm.group("pt4")) if x)
        pt = _num(raw.replace(",", ""))
        pt_action = pm.group("dir") or ("Maintains" if pm.group("pt2") else "Announces")
    return {"firm": m.group("firm").strip(), "action": act, "to": grade, "pt": pt, "pt_action": pt_action}


def _analyst_actions_from_news(t: str, days: int = 90, limit: int = 10) -> list:
    conn = mason_db.get_connection()
    try:
        cols = _cols(conn, "news_events")
        dele = "AND COALESCE(is_deleted,0)=0" if "is_deleted" in cols else ""
        rows = conn.execute(
            f"SELECT headline, COALESCE(published_at, ingested_at) AS ts FROM news_events "
            f"WHERE tickers LIKE ? AND COALESCE(published_at, ingested_at) >= ? {dele} "
            f"AND (headline LIKE '%Price Target%' OR headline LIKE '% Upgrades %' "
            f"OR headline LIKE '% Downgrades %' OR headline LIKE '%Initiates Coverage%') "
            f"ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT 200",
            (f'%"{t}"%', time.time() - days * 86400)).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for r in rows:
        a = parse_analyst_headline(r["headline"])
        if not a:
            continue
        a["date"] = datetime.fromtimestamp(float(r["ts"] or 0)).strftime("%Y-%m-%d")
        key = (a["firm"].lower(), a["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": a["date"], "firm": a["firm"], "action": a["action"], "from": None,
                    "to": a["to"], "pt": a["pt"], "prior_pt": None, "pt_action": a["pt_action"],
                    "src": "news_events"})
        if len(out) >= limit:
            break
    return out


def _street_from_info(street: dict, info: dict, price) -> dict:
    if not isinstance(street, dict) or "error" in street:
        return street
    street["rating"] = info.get("recommendationKey")
    street["rating_mean"] = _r(info.get("recommendationMean"), 2)  # 1=strong buy … 5=sell
    street["n_analysts"] = info.get("numberOfAnalystOpinions")
    if street.get("pt_mean") is None:
        street["pt_mean"] = _r(info.get("targetMeanPrice"), 2)
        street["pt_high"] = _r(info.get("targetHighPrice"), 2)
        street["pt_low"] = _r(info.get("targetLowPrice"), 2)
    street["upside_to_mean_pct"] = _r(_pct(street.get("pt_mean"), price), 1)
    return street


def _anchor_closes(df, live_price):
    """Daily closes with the last value set to the LIVE price: if the final bar is
    today's (partial) bar it's replaced, else the live price is appended."""
    closes = _col(df, "Close")
    dates = _bar_dates(df)
    lp = _num(live_price)
    if lp and closes:
        if dates and dates[-1] == date.today():
            closes[-1] = lp
        else:
            closes.append(lp)
    return closes


def _section_tape(t: str, hist: dict, quote: dict, etf) -> dict:
    df = hist.get(t)
    if df is None or len(df) < 30:
        raise ValueError("insufficient daily history")
    q = quote or {}
    price = _num(q.get("price")) if not q.get("error") else None
    closes = _anchor_closes(df, price)
    highs, lows = _col(df, "High"), _col(df, "Low")
    vols = _col(df, "Volume")
    dates = _bar_dates(df)
    last = closes[-1]
    ytd = None
    try:
        y0 = date(date.today().year, 1, 1)
        prior = [c for d, c in zip(dates, _col(df, "Close")) if d and d < y0]
        if prior:
            ytd = _pct(last, prior[-1])
    except Exception:
        ytd = None

    def rel(sym):
        sdf = hist.get(sym) if sym else None
        if sdf is None or len(sdf) < 22:
            return None, None
        sc = _col(sdf, "Close")
        mine, theirs = pct_change_n(closes, 21), pct_change_n(sc, 21)
        if mine is None or theirs is None:
            return None, _r(theirs, 2)
        return _r(mine - theirs, 2), _r(theirs, 2)

    vs_spy, spy_1m = rel("SPY")
    vs_sec, sec_1m = rel(etf)
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    atr = atr_wilder(highs, lows, _col(df, "Close"), 14)
    today_bar = bool(dates and dates[-1] == date.today())
    prior_vols = vols[:-1] if today_bar else vols
    avg20 = sma(prior_vols, 20)
    vol_now = _num(q.get("volume")) or (vols[-1] if today_bar and vols else None)
    chg_1d = _num(q.get("change_pct")) if price else pct_change_n(closes, 1)
    return {
        "price": _r(last, 2), "price_src": "get_quote_yf" if price else "daily close",
        "chg_1d": _r(chg_1d, 2), "chg_5d": _r(pct_change_n(closes, 5), 2),
        "chg_1m": _r(pct_change_n(closes, 21), 2), "chg_3m": _r(pct_change_n(closes, 63), 2),
        "chg_ytd": _r(ytd, 2),
        "vs_spy_1m": vs_spy, "spy_1m": spy_1m, "vs_sector_1m": vs_sec, "sector_1m": sec_1m,
        "sector_etf": etf,
        "sma20": _r(s20, 2), "sma50": _r(s50, 2), "sma200": _r(s200, 2),
        "vs_sma20_pct": _r(_pct(last, s20), 1), "vs_sma50_pct": _r(_pct(last, s50), 1),
        "vs_sma200_pct": _r(_pct(last, s200), 1),
        "rsi14": _r(rsi_wilder(closes, 14), 1),
        "atr14": _r(atr, 2), "atr14_pct": _r(atr / last * 100, 2) if atr and last else None,
        "rel_volume": _r(vol_now / avg20, 2) if vol_now and avg20 else None,
        "rel_volume_note": "today's volume (intraday partial if market open) / prior 20d avg",
        "high_52w": _r(q.get("52w_high"), 2), "low_52w": _r(q.get("52w_low"), 2),
        "pct_from_52w_high": _r(q.get("pct_from_52h"), 1),
        "day_range": [q.get("day_low"), q.get("day_high")] if q.get("day_low") else None,
        "last_bar": dates[-1].isoformat() if dates and dates[-1] else None,
        "method": "returns vs live price; 1m=21, 3m=63 bars; RSI/ATR Wilder(14)",
    }


def _pick_sample_expiries(exps: list, event_exp=None) -> list:
    """≤3 expiries within 0–45 DTE: the nearest (≥1 DTE if possible), the
    post-event expiry if in range, and the furthest ≤45 DTE; gaps filled evenly."""
    cand = [(e, d) for e, d in exps if 0 <= d <= 45]
    if not cand:
        return []
    near = next(((e, d) for e, d in cand if d >= 1), cand[0])
    picks = [near]
    if event_exp and any(e == event_exp for e, _ in cand):
        picks.append(next(x for x in cand if x[0] == event_exp))
    picks.append(cand[-1])
    seen, out = set(), []
    for p in picks:
        if p[0] not in seen:
            seen.add(p[0])
            out.append(p)
    rest = [c for c in cand if c[0] not in seen]
    while len(out) < 3 and rest:
        mid = rest[len(rest) // 2]
        out.append(mid)
        seen.add(mid[0])
        rest = [c for c in rest if c[0] not in seen]
    return sorted(out[:3], key=lambda x: x[1])


def _chain_rows(frame, typ: str) -> dict:
    """{strike: row-dict} for one side of a chain."""
    out = {}
    if frame is None or getattr(frame, "empty", True):
        return out
    for _, r in frame.iterrows():
        k = _num(r.get("strike"))
        if k is None:
            continue
        out[k] = {"type": typ, "strike": k, "bid": _r(r.get("bid"), 2), "ask": _r(r.get("ask"), 2),
                  "last": _r(r.get("lastPrice"), 2), "iv": _r((_num(r.get("impliedVolatility")) or 0) * 100, 1),
                  "oi": int(_num(r.get("openInterest")) or 0), "volume": int(_num(r.get("volume")) or 0)}
    return out


def _section_options(t: str, spot, next_event: dict, closes: list) -> dict:
    with _yf_slot():
        return _section_options_raw(t, spot, next_event, closes)


def _section_options_raw(t: str, spot, next_event: dict, closes: list) -> dict:
    spot = _num(spot)
    if not spot:
        raise ValueError("no spot price")
    tk = _ticker(t)
    raw = list(tk.options or [])
    if not raw:
        raise ValueError("no listed options")
    today = date.today()
    exps = []
    for e in raw:
        d = _to_date(e)
        if d:
            exps.append((e, (d - today).days))
    exps.sort(key=lambda x: x[1])
    ev_date = _to_date((next_event or {}).get("date"))
    # First expiry STRICTLY after the event date, so an after-close report is inside
    # the straddle (slightly overstates a before-open report — documented).
    event_exp = None
    if ev_date:
        event_exp = next((e for e, d in exps if _to_date(e) > ev_date and d <= 100), None)
    sample = _pick_sample_expiries(exps, event_exp)
    # ATM IV reference expiry: closest to 30 DTE (≥7) among listed ≤60 DTE.
    ref = min((x for x in exps if 7 <= x[1] <= 60), key=lambda x: abs(x[1] - 30), default=None)
    need = [e for e, _ in sample]
    for e in (event_exp, ref[0] if ref else None):
        if e and e not in need:
            need.append(e)
    chains = {}
    for e in need[:5]:
        try:
            oc = tk.option_chain(e)
            chains[e] = (_chain_rows(oc.calls, "C"), _chain_rows(oc.puts, "P"))
        except Exception:
            continue
    if not chains:
        raise ValueError("option_chain fetch failed")

    def atm_strike(calls, puts):
        ks = sorted(set(calls) & set(puts)) or sorted(set(calls) | set(puts))
        return min(ks, key=lambda k: (abs(k - spot), k)) if ks else None

    def atm_pair(e):
        calls, puts = chains.get(e, ({}, {}))
        k = atm_strike(calls, puts)
        return k, calls.get(k), puts.get(k)

    atm_iv, atm_iv_exp = None, None
    for e in ([ref[0]] if ref else []) + [x for x in need]:
        if e not in chains:
            continue
        k, c, p = atm_pair(e)
        ivs = [x["iv"] for x in (c, p) if x and x.get("iv") and 1.0 < x["iv"] < 500.0]
        if ivs:
            atm_iv, atm_iv_exp = sum(ivs) / len(ivs), e
            break
    if atm_iv is None:  # straddle approximation: IV ≈ straddle / (0.8 · S · √T)
        for e in need:
            k, c, p = atm_pair(e)
            dte = next((d for x, d in exps if x == e), None)
            if c and p and dte and dte > 0:
                cm, pm = option_mid(c["bid"], c["ask"], c["last"]), option_mid(p["bid"], p["ask"], p["last"])
                if cm and pm:
                    atm_iv, atm_iv_exp = (cm + pm) / (0.8 * spot * math.sqrt(dte / 365.0)) * 100, e
                    break

    move = {}
    mv_exp = event_exp if event_exp in chains else (sample[0][0] if sample and sample[0][0] in chains else None)
    if mv_exp:
        k, c, p = atm_pair(mv_exp)
        if c and p:
            move = implied_move(option_mid(c["bid"], c["ask"], c["last"]),
                                option_mid(p["bid"], p["ask"], p["last"]), spot)
            if move:
                move.update({"expiry": mv_exp, "strike": k,
                             "dte": next((d for x, d in exps if x == mv_exp), None),
                             "event": (next_event or {}).get("kind") if event_exp == mv_exp else "none ≤100 DTE — nearest expiry",
                             "event_date": ev_date.isoformat() if ev_date and event_exp == mv_exp else None})

    c_oi = sum(r["oi"] for e in chains for r in chains[e][0].values())
    p_oi = sum(r["oi"] for e in chains for r in chains[e][1].values())
    c_v = sum(r["volume"] for e in chains for r in chains[e][0].values())
    p_v = sum(r["volume"] for e in chains for r in chains[e][1].values())

    rows = []
    for e, dte in sample:
        if e not in chains:
            continue
        calls, puts = chains[e]
        ks = sorted(set(calls) | set(puts))
        k0 = atm_strike(calls, puts)
        if k0 is None:
            continue
        i = ks.index(k0)
        for k in ks[max(0, i - 3): i + 4]:
            for side in (calls, puts):
                r = side.get(k)
                if r:
                    # Pre-market / after-hours the chain has NO live bid/ask (0.00/0.00),
                    # which made every contract look unpickable at the 07:00 anchor
                    # (2026-09-29). Carry the last trade as an INDICATIVE mark instead;
                    # the real order is still priced from live quotes at approval.
                    live = (_num(r["bid"]) or 0) > 0 and (_num(r["ask"]) or 0) > 0
                    mark = option_mid(r["bid"], r["ask"], r["last"])
                    rows.append({"expiry": e, "dte": dte, "type": r["type"], "strike": k,
                                 "bid": r["bid"], "ask": r["ask"], "last": r["last"],
                                 "mark": _r(mark, 2) if mark else None,
                                 "mark_src": "quote" if live else ("last_trade" if mark else None),
                                 "iv": r["iv"], "oi": r["oi"], "volume": r["volume"]})
    rows.sort(key=lambda r: (r["dte"], r["type"], r["strike"]))
    return {
        "spot": _r(spot, 2),
        "atm_iv": _r(atm_iv, 1), "atm_iv_expiry": atm_iv_exp,
        "iv_rank_proxy": iv_rank_proxy((atm_iv or 0) / 100.0 if atm_iv else None, closes),
        "implied_move_next_event": move or None,
        "put_call_oi": _r(p_oi / c_oi, 2) if c_oi else None,
        "put_call_volume": _r(p_v / c_v, 2) if c_v else None,
        "pc_scope": f"summed over fetched expiries {sorted(chains)}",
        "nearest_expiries": [{"expiry": e, "dte": d} for e, d in exps[:6]],
        "chain_sample": rows,
        "note": "single-leg calls/puts ONLY from chain_sample; iv in %, prices per share (×100/contract)",
    }


def _load_universe() -> dict:
    try:
        if not UNIVERSE_FILE.exists():
            return {}
        raw = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("tickers"), dict):
            raw = raw["tickers"]
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def select_peers(t: str, industry, universe: dict = None, limit: int = 5):
    """(peers, method). Curated ticker map → universe same-industry → curated industry list."""
    t = t.upper()
    if t in PEER_MAP:
        return [p for p in PEER_MAP[t] if p != t][:limit], "curated ticker map"
    uni = universe if universe is not None else _load_universe()
    ind_u = ((uni.get(t) or {}).get("industry")) if uni else None
    ind = ind_u or industry
    if uni and ind:
        same = [s for s, v in uni.items() if s != t and isinstance(v, dict) and v.get("industry") == ind]
        if same:
            pref = INDUSTRY_PEERS.get(industry or "", [])
            same.sort(key=lambda s: (pref.index(s) if s in pref else 99,
                                     -len((uni[s] or {}).get("index") or []), s))
            return same[:limit], "universe_broad same-industry"
    if industry in INDUSTRY_PEERS:
        return [p for p in INDUSTRY_PEERS[industry] if p != t][:limit], "curated industry list"
    return [], "no peer mapping"


def _section_peers(t: str, industry, hist_fn=None) -> dict:
    peers, method = select_peers(t, industry)
    if not peers:
        return {"peers": [], "method": method}
    hist = (hist_fn or _history)(peers)

    def one(p):
        try:
            with _yf_slot():
                info = _ticker(p).info or {}
        except Exception:
            info = {}
        closes = _col(hist[p], "Close") if p in hist else []
        return {"ticker": p, "name": info.get("shortName") or info.get("longName"),
                "chg_1m": _r(pct_change_n(closes, 21), 2), "pe": _r(info.get("trailingPE"), 1),
                "fwd_pe": _r(info.get("forwardPE"), 1), "market_cap": _num(info.get("marketCap"))}

    out = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(one, p) for p in peers]
        for f in futs:
            try:
                out.append(f.result(timeout=6))
            except Exception:
                continue
    return {"peers": out, "method": method}


def _section_linkages(t: str, sector, industry) -> dict:
    etf = INDUSTRY_ETF.get(industry or "") or SECTOR_ETF.get(sector or "")
    sc = [{"ticker": a, "relation": r, "note": n} for a, r, n in SUPPLY_CHAIN.get(t.upper(), [])]
    tsens = list(_TICKER_SENS.get(t.upper(), []))
    base = [] if t.upper() in _TICKER_SENS_REPLACE else list(
        _INDUSTRY_SENS.get(industry or "") or _SECTOR_SENS.get(sector or "") or [])
    sens = tsens + [x for x in base if x not in tsens]
    return {"sector_etf": etf, "broad_sector_etf": SECTOR_ETF.get(sector or ""),
            "suppliers_customers": sc, "macro_sensitivities": sens,
            "note": "curated static tables (2026-09), best-effort — verify before relying"}


# ── DB-backed sections ────────────────────────────────────────────────────────

def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _norm_headline(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (h or "").lower())[:70]


def _section_news(t: str, days: int = 30, limit: int = 20) -> dict:
    conn = mason_db.get_connection()
    try:
        cols = _cols(conn, "news_events")
        pat = f'%"{t}"%'
        sel = ["headline", "source", "source_tier", "materiality", "direction",
               "COALESCE(published_at, ingested_at) AS ts"]
        sel.append("event_kind" if "event_kind" in cols else "NULL AS event_kind")
        sel.append("market_materiality" if "market_materiality" in cols else "NULL AS market_materiality")
        if "resolved_tickers" in cols:
            tick = ("(resolved_tickers LIKE ? OR ((resolved_tickers IS NULL OR resolved_tickers IN ('','[]')) "
                    "AND tickers LIKE ?))")
            params = [pat, pat]
        else:
            tick, params = "tickers LIKE ?", [pat]
        dele = "AND COALESCE(is_deleted,0)=0" if "is_deleted" in cols else ""
        since = time.time() - days * 86400
        rows = conn.execute(
            f"SELECT {', '.join(sel)} FROM news_events WHERE {tick} "
            f"AND COALESCE(published_at, ingested_at) >= ? {dele} "
            f"ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT 500",
            params + [since]).fetchall()
    finally:
        conn.close()
    seen, items = set(), []
    for r in rows:
        key = _norm_headline(r["headline"])
        if not key or key in seen:
            continue
        seen.add(key)
        mat = r["market_materiality"] if r["market_materiality"] is not None else r["materiality"]
        items.append({"ts": float(r["ts"] or 0),
                      "date": datetime.fromtimestamp(float(r["ts"] or 0)).strftime("%Y-%m-%d %H:%M"),
                      "source": r["source"], "headline": html.unescape(r["headline"] or "")[:180],
                      "event_kind": r["event_kind"], "materiality": _r(mat, 2),
                      "direction": r["direction"]})
    now = time.time()
    stats = {"n_30d": len(items), "n_7d": sum(1 for i in items if now - i["ts"] <= 7 * 86400),
             "n_1d": sum(1 for i in items if now - i["ts"] <= 86400)}
    ranked = sorted(items, key=lambda i: (-(i["materiality"] or 0), -i["ts"]))[:limit]
    ranked.sort(key=lambda i: -i["ts"])
    return {"items": ranked, "stats": stats}


def _section_calendar(t: str, days: int = 60) -> list:
    conn = mason_db.get_connection()
    try:
        today = date.today()
        rows = conn.execute(
            "SELECT event_date, event_time, event_type, title, status, source FROM calendar_events "
            "WHERE UPPER(ticker)=? AND event_date >= ? AND event_date <= ? "
            "AND COALESCE(record_deleted,0)=0 ORDER BY event_date, id LIMIT 20",
            (t.upper(), today.isoformat(), (today + timedelta(days=days)).isoformat())).fetchall()
    finally:
        conn.close()
    return [{"date": r["event_date"], "time": r["event_time"], "type": r["event_type"],
             "title": (r["title"] or "")[:160], "status": r["status"], "source": r["source"]} for r in rows]


_STAMP_HEAD_RE = re.compile(r"(\d{8}_\d{6})")


def _usable_verdict(v: dict) -> bool:
    """Skip test fixtures (MOCKED-MODEL reasoning) and verdicts that never arbitrated."""
    txt = f"{v.get('reasoning') or ''} {v.get('model_id') or ''}".upper()
    return bool(v.get("verdict")) and "MOCK" not in txt and v.get("arbitrated", True) is not False


def _usable_debate(d: dict) -> bool:
    return (d.get("status") == "complete"
            and bool(((d.get("bull_case") or {}).get("argument") or "").strip())
            and bool(((d.get("bear_case") or {}).get("argument") or "").strip()))


def _files_newest_first(dirpath: Path, prefix: str, t: str, cap: int = 25) -> list:
    """<prefix>_<T>_<YYYYMMDD_HHMMSS>*.json newest-first by the embedded stamp."""
    try:
        found = []
        for p in dirpath.glob(f"{prefix}_{t}_*.json"):
            m = _STAMP_HEAD_RE.match(p.name[len(prefix) + len(t) + 2:])   # after "<prefix>_<T>_"
            if m:
                found.append((m.group(1), p.name, p))
        found.sort(reverse=True)
        return [p for _, _, p in found[:cap]]
    except Exception:
        return []


def _trim(s, n):
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n - 1].rsplit(" ", 1)[0] + "…"


def _section_debate(t: str, max_age_days: int = 14) -> dict:
    """Newest USABLE arbiter verdict + bull/bear debate for T (Verdicts/, Debates/).
    Skipped/incomplete debates and mocked test verdicts are passed over."""
    out = {}
    cutoff = datetime.now() - timedelta(days=max_age_days)
    for vp in _files_newest_first(VERDICTS_DIR, "verdict", t):
        v = json.loads(vp.read_text(encoding="utf-8"))
        ts = _to_datetime(v.get("generated_at"))
        if ts is not None and ts < cutoff:
            break
        if not _usable_verdict(v):
            continue
        rp = v.get("revised_plan") or {}
        out["verdict"] = {
            "verdict": v.get("verdict"), "generated_at": v.get("generated_at"),
            "reasoning": _trim(v.get("reasoning"), 420),
            "regime_gate": v.get("regime_gate_check"), "correlation": v.get("correlation_check"),
            "plan": {k: rp.get(k) for k in ("direction", "instrument", "timeframe", "entry", "stop",
                                            "target", "calibration_adjusted_confidence") if rp.get(k) is not None} or None,
            "model": v.get("model_id"), "file": vp.name}
        break
    for dp in _files_newest_first(DEBATES_DIR, "debate", t):
        d = json.loads(dp.read_text(encoding="utf-8"))
        ts = _to_datetime(d.get("generated_at"))
        if ts is not None and ts < cutoff:
            break
        if not _usable_debate(d):
            continue
        out["debate"] = {
            "generated_at": d.get("generated_at"), "status": d.get("status"),
            "bull": _trim((d.get("bull_case") or {}).get("argument"), 360),
            "bear": _trim((d.get("bear_case") or {}).get("argument"), 360),
            "model": d.get("model_id"), "file": dp.name}
        break
    if not out:
        return {"none": f"no usable debate/verdict for {t} in the last {max_age_days}d"}
    return out


def _to_datetime(v):
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except Exception:
        return None


def _section_track_record(t: str) -> dict:
    import mason_desk_pipeline
    return mason_desk_pipeline.ticker_track_record(t)


# ═════════════════════════════════════════════════════════════════════════════
# Cache (dossiers table)
# ═════════════════════════════════════════════════════════════════════════════

def _cache_get(subject: str, kind: str, max_age: float, match=None):
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, built_at, payload_json FROM dossiers WHERE subject=? AND kind=? "
                "AND COALESCE(record_deleted,0)=0 AND built_at >= ? ORDER BY built_at DESC LIMIT 5",
                (subject, kind, time.time() - max_age)).fetchall()
        finally:
            conn.close()
        for r in rows:
            p = json.loads(r["payload_json"] or "{}")
            if match is None or match(p):
                p["id"] = r["id"]
                p["cache_hit"] = True
                return p
    except Exception:
        return None
    return None


def _cache_put(payload: dict, subject: str, kind: str) -> str:
    did = uuid.uuid4().hex[:12]
    try:
        text = dossier_text(payload, max_tokens=10 ** 6)
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO dossiers (id, subject, kind, schema_version, built_at, payload_json, "
                    "token_estimate, errors_json, created_date, created_by, record_deleted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (did, subject, kind, payload.get("schema_version"), time.time(),
                     json.dumps(payload, default=str), len(text) // 4,
                     json.dumps(payload.get("errors") or []), _now_iso(), CREATED_BY))
        finally:
            conn.close()
    except Exception as e:
        payload.setdefault("errors", []).append(f"cache_write: {e}")
    return did


# ═════════════════════════════════════════════════════════════════════════════
# Orchestration
# ═════════════════════════════════════════════════════════════════════════════

_SECTION_ORDER = ["profile", "financials", "estimates", "street", "tape", "options", "peers",
                  "linkages", "news_30d", "calendar", "debate", "track_record"]
_SOURCES = {"profile": "yfinance", "financials": "yfinance+edgar", "estimates": "yfinance",
            "street": "yfinance", "tape": "yfinance+get_quote_yf", "options": "yfinance",
            "peers": "yfinance", "linkages": "static", "news_30d": "db", "calendar": "db",
            "debate": "files", "track_record": "db"}


def _retry(fn, *args, tries: int = 2, pause: float = 0.4):
    """Call fn(*args); on an exception, or an empty .info bundle, retry once.
    yfinance often swallows a transient 401 and hands back an empty frame, which
    our sections turn into a ValueError — that is exactly the case worth one retry."""
    last = None
    for i in range(tries):
        try:
            out = fn(*args)
            if isinstance(out, dict) and "info" in out and not out.get("info") and i + 1 < tries:
                time.sleep(pause)
                continue
            return out
        except Exception as e:
            last = e
            if i + 1 < tries:
                time.sleep(pause)
    if last is not None:
        raise last
    return out


def _err(e) -> dict:
    if isinstance(e, str):
        return {"error": e[:240]}
    msg = str(e) or e.__class__.__name__
    return {"error": f"{e.__class__.__name__}: {msg}"[:240]}


def build_dossier(ticker, *, max_age_seconds=None, force=False) -> dict:
    """Deterministic research pack for one ticker (contracts §4). Never raises."""
    t0 = time.time()
    t = str(ticker or "").strip().upper()
    if not _TICKER_RE.match(t):
        return {"schema_version": SCHEMA_TICKER, "subject": t, "built_at": _now_iso(), "id": None,
                "errors": [f"invalid ticker {ticker!r}"], "sources": {}, "as_of": {}}
    try:
        if not force:
            ttl = max_age_seconds if max_age_seconds is not None else _ttl("ticker")
            hit = _cache_get(t, "ticker", ttl)
            if hit:
                return hit
        payload = _build_ticker(t, t0)
        payload["id"] = _cache_put(payload, t, "ticker")
        payload["cache_hit"] = False
        return payload
    except Exception as e:  # last-resort guard — a dossier never fails whole
        return {"schema_version": SCHEMA_TICKER, "subject": t, "built_at": _now_iso(), "id": None,
                "errors": [f"build: {e}"], "sources": {}, "as_of": {}}


def _build_ticker(t: str, t0: float) -> dict:
    deadline = t0 + BUILD_BUDGET_S
    payload = {"schema_version": SCHEMA_TICKER, "subject": t, "built_at": _now_iso()}
    as_of, errors = {}, []
    ex = ThreadPoolExecutor(max_workers=12, thread_name_prefix=f"dossier-{t}")
    try:
        # .info goes first, alone: yfinance lazily fetches Yahoo's cookie+crumb, and
        # concurrent first calls race on it (measured: 401 "Invalid Crumb" → empty
        # options/estimates). DB/SEC sections don't touch Yahoo, so they start now.
        f = {
            "info": ex.submit(_retry, _fetch_info, t),
            "edgar": ex.submit(_section_edgar, t),
            "news_30d": ex.submit(_section_news, t),
            "calendar": ex.submit(_section_calendar, t),
            "debate": ex.submit(_section_debate, t),
            "track_record": ex.submit(_section_track_record, t),
        }

        def res(key, timeout):
            try:
                return f[key].result(timeout=max(0.05, timeout)), None
            except Exception as e:
                return None, e

        bundle, e_info = res("info", min(INFO_WAIT_S, deadline - time.time()))
        f["quote"] = ex.submit(_live_quote, t)
        f["hist_base"] = ex.submit(_history, [t, "SPY"])
        f["financials"] = ex.submit(_retry, _section_financials, t)
        f["estimates"] = ex.submit(_retry, _section_estimates, t)
        f["street"] = ex.submit(_retry, _section_street, t)
        info = (bundle or {}).get("info") or {}
        sector, industry = info.get("sector"), info.get("industry")
        link = _section_linkages(t, sector, industry)
        etf = link["sector_etf"]
        try:
            profile = _section_profile(bundle or {})
        except Exception as e:
            profile = _err(e_info or e)
        payload["profile"] = profile
        as_of["profile"] = _now_iso()

        cal_rows, _ = res("calendar", 2.0)
        next_event = {}
        if isinstance(profile, dict) and profile.get("next_earnings"):
            next_event = {"kind": "earnings", "date": profile["next_earnings"]}
        for c in (cal_rows or []):  # an earlier scheduled catalyst (PDUFA, trial, …) wins
            if c.get("date") and (not next_event or c["date"] < next_event["date"]):
                next_event = {"kind": c.get("type") or "calendar", "date": c["date"]}
                break

        def tape_task():
            q, _e = res("quote", deadline - time.time())
            res("hist_base", deadline - time.time())
            h = _history([t, "SPY"] + ([etf] if etf else []))
            return _section_tape(t, h, q or {}, etf)

        def options_task():
            q, _e = res("quote", deadline - time.time())
            spot = (q or {}).get("price") or info.get("regularMarketPrice") or info.get("currentPrice")
            hb, _e = res("hist_base", deadline - time.time())
            closes = _col((hb or {}).get(t), "Close") if (hb or {}).get(t) is not None else []
            return _section_options(t, spot, next_event, closes)

        f["tape"] = ex.submit(tape_task)
        f["options"] = ex.submit(_retry, options_task)
        f["peers"] = ex.submit(_section_peers, t, industry)

        for key in ("financials", "estimates", "street", "tape", "options", "peers",
                    "news_30d", "debate", "track_record"):
            val, e = res(key, deadline - time.time())
            if e is not None:
                payload[key] = _err(e if not isinstance(e, TimeoutError) else TimeoutError("section timeout"))
            else:
                payload[key] = val
            as_of[key] = _now_iso()
        payload["calendar"] = cal_rows if cal_rows is not None else _err(res("calendar", 0.05)[1] or "timeout")
        as_of["calendar"] = _now_iso()

        # ── post-merge ──
        edg, e_edg = res("edgar", max(0.05, deadline - time.time()))
        if isinstance(payload.get("financials"), dict) and "error" not in payload["financials"]:
            fin = payload["financials"]
            if edg:
                fin = _merge_edgar(fin, edg)
            else:
                fin["edgar_check"] = f"skipped: {e_edg}"[:160]
            fin["yoy"] = _yoy(fin.get("quarters") or [])
            fin["currency"] = info.get("financialCurrency") or "USD"
            if fin["currency"] != (info.get("currency") or "USD"):
                fin["currency_note"] = (f"statements in {fin['currency']} (quote in {info.get('currency')}); "
                                        f"EPS is per ORDINARY share — ADR ratio may differ")
            if info:
                fin["yoy"]["yf_revenue_growth_pct"] = _r((_num(info.get("revenueGrowth")) or 0) * 100, 1) \
                    if info.get("revenueGrowth") is not None else None
            payload["financials"] = fin
        price = (payload.get("tape") or {}).get("price") if isinstance(payload.get("tape"), dict) else None
        payload["street"] = _street_from_info(payload.get("street"), info, price or info.get("regularMarketPrice"))
        # Review fix 2026-09-25: old verdicts carry entry levels from a different price
        # regime (NVDA plan @135.5 vs live 224.9). Drop plan levels >20% off live px.
        try:
            dbt = payload.get("debate") or {}
            plan = ((dbt.get("verdict") or {}).get("plan") or {}) if isinstance(dbt, dict) else {}
            ent, px = _num(plan.get("entry")), _num(price or info.get("regularMarketPrice"))
            if ent and px and abs(ent - px) / px > 0.20:
                dbt["verdict"]["plan"] = None
                dbt["verdict"]["stale_levels"] = (f"plan levels dropped: entry {ent:g} is "
                                                  f"{(ent - px) / px:+.0%} from live {px:g}")
        except Exception:
            pass
        peers = payload.get("peers")
        if isinstance(peers, dict) and "peers" in peers:
            payload["peers_method"] = peers.get("method")
            payload["peers"] = peers["peers"]
        news = payload.get("news_30d")
        if isinstance(news, dict) and "items" in news:
            payload["news_stats"] = news["stats"]
            payload["news_30d"] = news["items"]
        payload["linkages"] = link
        as_of["linkages"] = "2026-09 (static)"
        payload["next_event"] = next_event or None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    ordered = {k: payload[k] for k in ("schema_version", "subject", "built_at")}
    for k in _SECTION_ORDER:
        v = payload.get(k)
        if v is None:
            v = _err(TimeoutError("section missing"))
        ordered[k] = v
        if isinstance(v, dict) and "error" in v and len(v) == 1:
            errors.append(f"{k}: {v['error']}")
    for k in ("next_event", "news_stats", "peers_method"):
        if k in payload:
            ordered[k] = payload[k]
    ordered["sources"] = dict(_SOURCES)
    ordered["as_of"] = as_of
    ordered["errors"] = errors
    ordered["build_seconds"] = round(time.time() - t0, 2)
    return ordered


def _macro_subject(kinds: list) -> str:
    return "MACRO:" + "+".join(sorted(set(kinds))) if kinds else "MACRO:none"


def _section_cross_asset() -> dict:
    """Price + 1d from the LIVE quote (get_quote_yf); 5d/1m from daily bars anchored
    to the live price. Continuous futures (=F) roll contracts inside Yahoo's daily
    series (measured 2026-09-25: BZ=F bars showed -6.6% 1d on the Nov→Dec roll vs a
    real -0.8%), so a bar-vs-quote 1d gap > 2pp is flagged as a roll."""
    syms = [s for s, _ in CROSS_ASSET]
    hist = _history(syms)
    quotes = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {s: ex.submit(_live_quote, s) for s in syms}
        for s, fut in futs.items():
            try:
                q = fut.result(timeout=max(0.5, BUILD_BUDGET_S - 4))
                quotes[s] = q if q and not q.get("error") and _num(q.get("price")) else None
            except Exception:
                quotes[s] = None
    out = {}
    for s, label in CROSS_ASSET:
        df = hist.get(s)
        bars = _col(df, "Close") if df is not None else []
        q = quotes.get(s)
        if len(bars) < 2 and not q:
            out[s] = {"label": label, "error": "no data"}
            continue
        c = _anchor_closes(df, q.get("price")) if (q and df is not None) else list(bars)
        bar_1d = pct_change_n(bars, 1) if len(bars) >= 2 else None
        chg_1d = _num(q.get("change_pct")) if q else bar_1d
        nd = 3 if s == "^TNX" else 2
        row = {"label": label, "price": _r(c[-1] if c else (q or {}).get("price"), nd),
               "price_src": "get_quote_yf" if q else "daily close",
               "chg_1d": _r(chg_1d, 2), "chg_5d": _r(pct_change_n(c, 5), 2),
               "chg_1m": _r(pct_change_n(c, 21), 2),
               "last_bar": (_bar_dates(df)[-1].isoformat() if df is not None and _bar_dates(df) else None)}
        if s.endswith("=F") and q and bar_1d is not None and chg_1d is not None and abs(bar_1d - chg_1d) > 2.0:
            row["roll_warning"] = (f"contract roll in daily bars (bars 1d {bar_1d:+.1f}% vs quote {chg_1d:+.1f}%); "
                                   f"5d/1m include the roll gap")
        if s == "^TNX" and len(c) >= 2:
            prev = _num((q or {}).get("prev_close")) or c[-2]
            row["chg_1d_bp"] = _r((c[-1] - prev) * 100, 1)
            row["chg_5d_bp"] = _r((c[-1] - c[-6]) * 100, 1) if len(c) > 5 else None
        out[s] = row
    if not any("price" in v for v in out.values()):
        raise ValueError("cross-asset download returned nothing")
    return out


_MACRO_CAL_TYPES = ("econ", "central_bank", "fomc", "macro", "econ_release", "cpi", "jobs")


def _section_macro_calendar(days: int = 7) -> list:
    conn = mason_db.get_connection()
    try:
        today = date.today()
        q = ",".join("?" * len(_MACRO_CAL_TYPES))
        rows = conn.execute(
            f"SELECT event_date, event_time, event_type, title, status, source FROM calendar_events "
            f"WHERE LOWER(event_type) IN ({q}) AND event_date >= ? AND event_date <= ? "
            f"AND COALESCE(record_deleted,0)=0 ORDER BY event_date, event_time, id LIMIT 40",
            list(_MACRO_CAL_TYPES) + [today.isoformat(), (today + timedelta(days=days)).isoformat()]).fetchall()
    finally:
        conn.close()
    return [{"date": r["event_date"], "time": r["event_time"], "type": r["event_type"],
             "title": (r["title"] or "")[:160], "status": r["status"], "source": r["source"]} for r in rows]


def build_macro_dossier(event_kinds, *, headline_hint="", max_age_seconds=None, force=False) -> dict:
    """Cross-asset snapshot + static transmission map + next-7d macro calendar. Never raises."""
    t0 = time.time()
    kinds = sorted({str(k).strip() for k in (event_kinds or []) if str(k).strip()})
    hint = str(headline_hint or "").strip()[:300]
    subject = _macro_subject(kinds)
    try:
        if not force:
            ttl = max_age_seconds if max_age_seconds is not None else _ttl("macro")
            hit = _cache_get(subject, "macro", ttl, match=lambda p: p.get("headline_hint", "") == hint)
            if hit:
                return hit
        payload = {"schema_version": SCHEMA_MACRO, "subject": subject, "kinds": kinds,
                   "headline_hint": hint, "built_at": _now_iso()}
        as_of, errors = {}, []
        ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dossier-macro")
        try:
            fx = ex.submit(_section_cross_asset)
            fc = ex.submit(_section_macro_calendar)
            for key, fut in (("cross_asset", fx), ("calendar", fc)):
                try:
                    payload[key] = fut.result(timeout=max(0.05, t0 + BUILD_BUDGET_S - time.time()))
                except Exception as e:
                    payload[key] = _err(e if not isinstance(e, TimeoutError) else TimeoutError("section timeout"))
                    errors.append(f"{key}: {payload[key]['error']}")
                as_of[key] = _now_iso()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        try:
            payload["transmission"] = transmission_for(kinds, hint)
        except Exception as e:
            payload["transmission"] = _err(e)
            errors.append(f"transmission: {payload['transmission']['error']}")
        as_of["transmission"] = "2026-09 (static map)"
        payload = {k: payload[k] for k in ("schema_version", "subject", "kinds", "headline_hint",
                                           "built_at", "cross_asset", "transmission", "calendar")}
        payload["sources"] = {"cross_asset": "yfinance", "transmission": "static", "calendar": "db"}
        payload["as_of"] = as_of
        payload["errors"] = errors
        payload["build_seconds"] = round(time.time() - t0, 2)
        payload["id"] = _cache_put(payload, subject, "macro")
        payload["cache_hit"] = False
        return payload
    except Exception as e:
        return {"schema_version": SCHEMA_MACRO, "subject": subject, "kinds": kinds, "id": None,
                "built_at": _now_iso(), "errors": [f"build: {e}"], "sources": {}, "as_of": {}}


# ═════════════════════════════════════════════════════════════════════════════
# Text rendering for the LLM prompt (deterministic)
# ═════════════════════════════════════════════════════════════════════════════

def _m(v, cur=None):
    """Compact money: $1.91T / $63.3B / $412M / $12.3K / $7.21; non-USD → 'TWD 1.27T'."""
    f = _num(v)
    if f is None:
        return "n/a"
    a, s = abs(f), "-" if f < 0 else ""
    sym = "$" if not cur or cur == "USD" else f"{cur} "
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{s}{sym}{a / div:.2f}{suf}"
    return f"{s}{sym}{a:.2f}"


def _n(v, nd=2):
    f = _num(v)
    if f is None:
        return "n/a"
    a = abs(f)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            return f"{f / div:.2f}{suf}"
    return f"{f:.{nd}f}"


def _p(v, nd=2, signed=True):
    f = _num(v)
    if f is None:
        return "n/a"
    return f"{f:+.{nd}f}%" if signed else f"{f:.{nd}f}%"


def _hdr(d, sec, label=None):
    src = (d.get("sources") or {}).get(sec, "?")
    ao = (d.get("as_of") or {}).get(sec, d.get("built_at", "?"))
    return f"## {label or sec.upper()} [{src} | {ao}]"


def _is_err(v):
    return isinstance(v, dict) and "error" in v and len(v) == 1


def _render_ticker(d: dict, lim: dict) -> str:
    L = []
    t = d.get("subject")
    prof = d.get("profile") if isinstance(d.get("profile"), dict) else {}
    L.append(f"=== DOSSIER {t} · {prof.get('name') or ''} · built {d.get('built_at')} · {d.get('schema_version')} ===")
    L.append("Conventions: $=USD; %=percent; pp=percentage points; each header shows [source | as_of]. "
             "Missing data = n/a. Numbers are facts from the named source, not estimates by Mason.")

    # PROFILE
    L.append(_hdr(d, "profile"))
    if _is_err(d.get("profile")):
        L.append(f"ERROR {d['profile']['error']}")
    else:
        ne = prof.get("next_earnings") or "n/a"
        if prof.get("next_earnings_is_estimate"):
            ne += " (est.)"
        L.append(f"{prof.get('sector')} / {prof.get('industry')} · mcap {_m(prof.get('market_cap'))} · "
                 f"float {_n(prof.get('float'))} sh · short {_p(prof.get('short_pct'), 1, False)} of float "
                 f"({_n(prof.get('short_ratio_days'), 1)}d to cover) · beta {_n(prof.get('beta'))}")
        L.append(f"P/E {_n(prof.get('pe'), 1)} · fwd P/E {_n(prof.get('fwd_pe'), 1)} · P/S {_n(prof.get('ps'), 1)} · "
                 f"next earnings {ne}")
        if lim.get("summary") and prof.get("summary"):
            L.append(f"Biz: {prof['summary']}")
    ne = d.get("next_event")
    if ne:
        L.append(f"Next scheduled catalyst: {ne.get('kind')} {ne.get('date')}")

    # TAPE
    tp = d.get("tape")
    L.append(_hdr(d, "tape"))
    if _is_err(tp) or not isinstance(tp, dict):
        L.append(f"ERROR {(tp or {}).get('error') if isinstance(tp, dict) else 'n/a'}")
    else:
        L.append(f"px {_n(tp.get('price'))} ({tp.get('price_src')}) · 1d {_p(tp.get('chg_1d'))} · 5d {_p(tp.get('chg_5d'))} · "
                 f"1m {_p(tp.get('chg_1m'))} · 3m {_p(tp.get('chg_3m'))} · YTD {_p(tp.get('chg_ytd'))}")
        L.append(f"1m rel: vs SPY {_n(tp.get('vs_spy_1m'))}pp (SPY {_p(tp.get('spy_1m'))}) · vs {tp.get('sector_etf')} "
                 f"{_n(tp.get('vs_sector_1m'))}pp ({tp.get('sector_etf')} {_p(tp.get('sector_1m'))})")
        L.append(f"SMA20 {_n(tp.get('sma20'))} ({_p(tp.get('vs_sma20_pct'), 1)}) · SMA50 {_n(tp.get('sma50'))} "
                 f"({_p(tp.get('vs_sma50_pct'), 1)}) · SMA200 {_n(tp.get('sma200'))} ({_p(tp.get('vs_sma200_pct'), 1)})")
        L.append(f"RSI14 {_n(tp.get('rsi14'), 1)} · ATR14 {_n(tp.get('atr14'))} ({_p(tp.get('atr14_pct'), 2, False)} of px) · "
                 f"rel vol {_n(tp.get('rel_volume'))}x (today vs prior-20d avg; partial if intraday) · "
                 f"52w {_n(tp.get('low_52w'))}–{_n(tp.get('high_52w'))} ({_p(tp.get('pct_from_52w_high'), 1)} from high) · "
                 f"last bar {tp.get('last_bar')}")

    # FINANCIALS
    fin = d.get("financials")
    L.append(_hdr(d, "financials"))
    if _is_err(fin) or not isinstance(fin, dict):
        L.append(f"ERROR {(fin or {}).get('error') if isinstance(fin, dict) else 'n/a'}")
    else:
        cur = fin.get("currency")
        if fin.get("currency_note"):
            L.append(f"NOTE: {fin['currency_note']}")
        L.append(f"period | revenue | EPS dil ({cur or 'USD'}) | GM% | OpM% | FCF | src/check")
        for q in (fin.get("quarters") or [])[:lim["quarters"]]:
            chk = q.get("src", "")
            if q.get("rev_check"):
                chk += f" edgar:{q['rev_check']}"
            L.append(f"{q.get('period')} | {_m(q.get('revenue'), cur)} | {_n(q.get('eps'))} | {_n(q.get('gross_margin'), 1)} | "
                     f"{_n(q.get('op_margin'), 1)} | {_m(q.get('fcf'), cur)} | {chk}")
        y = fin.get("yoy") or {}
        if y:
            L.append(f"YoY: latest ({y.get('latest_period')}) rev {_p(y.get('latest_revenue_yoy_pct'), 1)} "
                     f"EPS {_p(y.get('latest_eps_yoy_pct'), 1)} · prior q rev {_p(y.get('prev_revenue_yoy_pct'), 1)} "
                     f"· yf revenueGrowth {_p(y.get('yf_revenue_growth_pct'), 1)}")
        if fin.get("eps_surprises"):
            L.append("EPS surprises: " + "; ".join(
                f"{s['date']} est {_n(s.get('eps_est'))} act {_n(s.get('eps_actual'))} ({_p(s.get('surprise_pct'), 1)})"
                for s in fin["eps_surprises"]))
        if fin.get("edgar_check"):
            L.append(f"EDGAR cross-check: {fin['edgar_check']}")

    # ESTIMATES
    es = d.get("estimates")
    L.append(_hdr(d, "estimates", "ESTIMATES (consensus)"))
    if _is_err(es) or not isinstance(es, dict):
        L.append(f"ERROR {(es or {}).get('error') if isinstance(es, dict) else 'n/a'}")
    else:
        def er(lbl, r, money=False):
            if not r:
                return None
            c = r.get("currency")
            f = (lambda v: _m(v, c)) if money else (lambda v: _n(v) + (f" {c}" if c and c != "USD" else ""))
            ya = r.get("yearAgoRevenue") if money else r.get("yearAgoEps")
            return (f"{lbl} avg {f(r.get('avg'))} (lo {f(r.get('low'))} hi {f(r.get('high'))}, n={_n(r.get('numberOfAnalysts'), 0)}, "
                    f"yr-ago {f(ya)}, growth {_p((_num(r.get('growth')) or 0) * 100 if r.get('growth') is not None else None, 1)})")
        for lbl, key, money in (("EPS curQ", "eps_next_q", False), ("EPS nextQ", "eps_q_plus1", False),
                                ("EPS FY", "eps_fy", False), ("EPS NY", "eps_ny", False),
                                ("Rev curQ", "rev_next_q", True), ("Rev FY", "rev_fy", True), ("Rev NY", "rev_ny", True)):
            s = er(lbl, es.get(key), money)
            if s:
                L.append(s)
        for p, r in (es.get("eps_trend") or {}).items():
            L.append(f"EPS trend {p}: now {_n(r.get('current'))} vs 30d {_n(r.get('30daysAgo'))} vs 90d {_n(r.get('90daysAgo'))} "
                     f"({_p(r.get('chg_30d_pct'), 1)} /30d, {_p(r.get('chg_90d_pct'), 1)} /90d)")
        for p, r in (es.get("revisions") or {}).items():
            L.append(f"Revisions {p}: up7 {r.get('upLast7days')} up30 {r.get('upLast30days')} "
                     f"down7 {r.get('downLast7Days')} down30 {r.get('downLast30days')}")

    # STREET
    st = d.get("street")
    L.append(_hdr(d, "street"))
    if _is_err(st) or not isinstance(st, dict):
        L.append(f"ERROR {(st or {}).get('error') if isinstance(st, dict) else 'n/a'}")
    else:
        rc = st.get("rec_counts_0m") or {}
        L.append(f"rating {st.get('rating')} (mean {_n(st.get('rating_mean'))}, 1=StrongBuy..5=Sell, n={st.get('n_analysts')}) · "
                 f"SB/B/H/S/SS {rc.get('strongBuy')}/{rc.get('buy')}/{rc.get('hold')}/{rc.get('sell')}/{rc.get('strongSell')} · "
                 f"PT mean {_n(st.get('pt_mean'))} med {_n(st.get('pt_median'))} hi {_n(st.get('pt_high'))} lo {_n(st.get('pt_low'))} "
                 f"(upside to mean {_p(st.get('upside_to_mean_pct'), 1)})")
        for a in (st.get("recent_actions") or [])[:lim["actions"]]:
            grade = a.get("to") or ""
            if a.get("from") and a.get("from") != a.get("to"):
                grade = f"{a['from']}→{a.get('to')}"
            pt = ""
            if a.get("pt"):
                pt = f" PT {_n(a.get('prior_pt'))}→{_n(a['pt'])}" if a.get("prior_pt") else f" PT {_n(a['pt'])}"
            L.append(f"- {a.get('date')} {a.get('firm')} {a.get('action')} {grade}{pt}")

    # OPTIONS
    op = d.get("options")
    L.append(_hdr(d, "options"))
    if _is_err(op) or not isinstance(op, dict):
        L.append(f"ERROR {(op or {}).get('error') if isinstance(op, dict) else 'n/a'}")
    else:
        ivr = op.get("iv_rank_proxy") or {}
        L.append(f"ATM IV {_p(op.get('atm_iv'), 1, False)} (exp {op.get('atm_iv_expiry')}) · IV-rank PROXY {_n(ivr.get('value'), 0)}/100 "
                 f"[{ivr.get('method')}; HV20 now {_p(ivr.get('hv20_now'), 1, False)}, 1y range "
                 f"{_p(ivr.get('hv20_1y_min'), 1, False)}–{_p(ivr.get('hv20_1y_max'), 1, False)}; IV/HV20 {_n(ivr.get('iv_hv_ratio'))}"
                 f"{'; clipped' if ivr.get('clipped') else ''}]")
        mv = op.get("implied_move_next_event") or {}
        if mv:
            L.append(f"Implied move: ±{_n(mv.get('move_usd'))} (±{_n(mv.get('move_pct'))}%) via {mv.get('expiry')} {_n(mv.get('strike'))} "
                     f"straddle ({mv.get('dte')} DTE) · event: {mv.get('event')} {mv.get('event_date') or ''}".rstrip())
        L.append(f"P/C OI {_n(op.get('put_call_oi'))} · P/C vol {_n(op.get('put_call_volume'))} ({op.get('pc_scope')})")
        L.append("Expiries: " + ", ".join(f"{x['expiry']}({x['dte']}d)" for x in (op.get("nearest_expiries") or [])))
        rows = op.get("chain_sample") or []
        exps = []
        for r in rows:
            if r["expiry"] not in exps:
                exps.append(r["expiry"])
        keep = exps[:lim["chain_expiries"]]
        if keep:
            L.append("CHAIN_SAMPLE (single-leg candidates ONLY from here; px per share, ×100/contract) "
                     "fmt: TYPE STRIKE bid/ask iv% oi vol")
            if any(r.get("mark_src") == "last_trade" for r in rows if r["expiry"] in keep):
                L.append("⚠ NO LIVE BID/ASK (pre-market/after-hours): rows marked 'last X (indicative)' show the "
                         "LAST TRADE. They are valid picks; use the last trade as the indicative entry — the order "
                         "is priced from live quotes when it is approved.")
        for e in keep:
            er_ = [r for r in rows if r["expiry"] == e]
            L.append(f"exp {e} ({er_[0]['dte']} DTE):")
            for r in er_:
                px = (f"last {_n(r.get('last'))} (indicative)" if r.get("mark_src") == "last_trade"
                      else f"{_n(r['bid'])}/{_n(r['ask'])}")
                L.append(f"  {r['type']} {_n(r['strike'])} {px} iv{_n(r['iv'], 1)} "
                         f"oi{r['oi']} v{r['volume']}")
        if len(exps) > len(keep):
            L.append(f"(chain rows for {len(exps) - len(keep)} later expiries omitted for length)")

    # PEERS
    L.append(_hdr(d, "peers") + (f" method: {d.get('peers_method')}" if d.get("peers_method") else ""))
    pe = d.get("peers")
    if _is_err(pe):
        L.append(f"ERROR {pe['error']}")
    elif not pe:
        L.append("none mapped")
    else:
        for p in pe[:lim["peers"]]:
            L.append(f"- {p.get('ticker')} {p.get('name') or ''}: 1m {_p(p.get('chg_1m'))} · P/E {_n(p.get('pe'), 1)} · "
                     f"fwd {_n(p.get('fwd_pe'), 1)} · mcap {_m(p.get('market_cap'))}")

    # LINKAGES
    lk = d.get("linkages") or {}
    L.append(_hdr(d, "linkages", "LINKAGES (what affects it / what it affects)"))
    if _is_err(lk):
        L.append(f"ERROR {lk['error']}")
    else:
        L.append(f"sector ETF {lk.get('sector_etf')} (broad {lk.get('broad_sector_etf')})")
        sc = lk.get("suppliers_customers") or []
        if sc:
            L.append("chain: " + "; ".join(f"{x['ticker']} {x['relation']} ({x['note']})" for x in sc[:lim["chain_links"]]))
        if lk.get("macro_sensitivities"):
            L.append("macro sens: " + "; ".join(lk["macro_sensitivities"]))

    # NEWS
    ns = d.get("news_stats") or {}
    L.append(_hdr(d, "news_30d", "NEWS_30D") + (f" counts 1d/7d/30d {ns.get('n_1d')}/{ns.get('n_7d')}/{ns.get('n_30d')}" if ns else ""))
    nw = d.get("news_30d")
    if _is_err(nw):
        L.append(f"ERROR {nw['error']}")
    elif not nw:
        L.append("no ticker-tagged headlines in 30d")
    else:
        items = nw
        if len(items) > lim["news"]:
            top = sorted(items, key=lambda i: (-(i.get("materiality") or 0), -(i.get("ts") or 0)))[:lim["news"]]
            ids = {id(x) for x in top}
            items = [x for x in nw if id(x) in ids]
        for i in items:
            tag = "/".join(str(x) for x in (i.get("event_kind"), i.get("direction")) if x)
            L.append(f"- {i.get('date')} [{i.get('source')}] {i.get('headline')} (mat {_n(i.get('materiality'))}{' ' + tag if tag else ''})")
        if len(nw) > len(items):
            L.append(f"({len(nw) - len(items)} lower-materiality headlines omitted)")

    # CALENDAR
    L.append(_hdr(d, "calendar", "CALENDAR (next 60d)"))
    ca = d.get("calendar")
    if _is_err(ca):
        L.append(f"ERROR {ca['error']}")
    elif not ca:
        L.append("no scheduled calendar_events (earnings date is in PROFILE)")
    else:
        for c in ca:
            L.append(f"- {c.get('date')} {c.get('time') or ''} {c.get('type')}: {c.get('title')} [{c.get('source')}]".replace("  ", " "))

    # DEBATE
    n_debate = len(L)
    L.append(_hdr(d, "debate", "DEBATE (latest bull/bear + arbiter)"))
    db = d.get("debate") or {}
    if _is_err(db):
        L.append(f"ERROR {db['error']}")
    elif db.get("none"):
        L.append(db["none"])
    else:
        v = db.get("verdict")
        if v:
            L.append(f"Arbiter {v.get('verdict')} @ {v.get('generated_at')} ({v.get('model')}): {v.get('reasoning')}")
            if v.get("plan"):
                L.append("plan: " + ", ".join(f"{k}={v['plan'][k]}" for k in v["plan"]))
        dd = db.get("debate")
        if dd:
            L.append(f"Bull ({dd.get('generated_at')}): {dd.get('bull')}")
            L.append(f"Bear: {dd.get('bear')}")
    if lim.get("debate"):              # (2026-11-18) only on the keep-option-rows ladder; see _debate_lines_kept
        L[n_debate:] = _debate_lines_kept(d, lim["debate"])
    if d.get("omit_empty_debate") is True and not _is_err(db) and not db.get("verdict") and not db.get("debate"):
        del L[n_debate:]               # (2026-11-19) set by the director only with v2.writeups_drop_empty_debate: no
                                       # usable debate -> no DEBATE section (not the "no usable debate" line)

    # TRACK RECORD
    tr = d.get("track_record")
    L.append(_hdr(d, "track_record"))
    if _is_err(tr):
        L.append(f"ERROR {tr['error']}")
    else:
        L.append((tr or {}).get("line") or "n/a")

    if d.get("errors"):
        L.append("## SECTION ERRORS: " + " | ".join(d["errors"]))
    return "\n".join(L)


def _render_macro(d: dict, lim: dict) -> str:
    L = [f"=== MACRO DOSSIER {'+'.join(d.get('kinds') or [])} · hint: {d.get('headline_hint') or '-'} · "
         f"built {d.get('built_at')} · {d.get('schema_version')} ===",
         "Conventions: px + 1d from live quote, 5d/1m from daily bars; chg in %; ^TNX px is yield % "
         "(bp changes shown); signs: + up, - down, ± ambiguous."]
    L.append(_hdr(d, "cross_asset", "CROSS_ASSET"))
    ca = d.get("cross_asset")
    if _is_err(ca):
        L.append(f"ERROR {ca['error']}")
    else:
        L.append("sym label | px | 1d | 5d | 1m")
        for s, _lbl in CROSS_ASSET:
            r = (ca or {}).get(s)
            if not r:
                continue
            if "error" in r:
                L.append(f"{s} {r.get('label')} | n/a ({r['error']})")
                continue
            extra = f" ({_n(r.get('chg_1d_bp'), 1)}bp 1d, {_n(r.get('chg_5d_bp'), 1)}bp 5d)" if s == "^TNX" else ""
            if r.get("roll_warning"):
                extra += f" ⚠ {r['roll_warning']}"
            L.append(f"{s} {r.get('label')} | {_n(r.get('price'), 3 if s == '^TNX' else 2)} | {_p(r.get('chg_1d'))} | "
                     f"{_p(r.get('chg_5d'))} | {_p(r.get('chg_1m'))}{extra}")
    L.append(_hdr(d, "transmission", "TRANSMISSION (static map: expected sign if the event plays out as headlined)"))
    tm = d.get("transmission")
    if _is_err(tm):
        L.append(f"ERROR {tm['error']}")
    elif not tm:
        L.append("no rule matched these kinds/keywords")
    else:
        cur = None
        for x in tm[:lim["transmission"]]:
            if x["rule"] != cur:
                cur = x["rule"]
                L.append(f"[{cur}]")
            L.append(f"  {x['asset']} {x['expected_sign']} — {x['rationale']}")
    L.append(_hdr(d, "calendar", "MACRO CALENDAR (next 7d)"))
    cal = d.get("calendar")
    if _is_err(cal):
        L.append(f"ERROR {cal['error']}")
    elif not cal:
        L.append("no econ/central-bank calendar_events in the next 7d")
    else:
        for c in cal:
            L.append(f"- {c.get('date')} {c.get('time') or ''} {c.get('type')}: {c.get('title')} [{c.get('source')}]".replace("  ", " "))
    if d.get("errors"):
        L.append("## SECTION ERRORS: " + " | ".join(d["errors"]))
    return "\n".join(L)


# Truncation ladder — applied in order until the text fits (contracts: chain rows
# beyond nearest expiry, news beyond 10, peers beyond 3 go first).
# Review fix 2026-09-25: chains were cut to the nearest expiry FIRST, so the director
# saw only a 3-DTE NVDA chain and couldn't pick a contract past the catalyst. Chains
# are now cut last-but-one, and to 2 expiries before 1.
# 2026-11-17 (Nico approved): the last news lines now go BEFORE the chain drops below 2 expiries.
# The old order cut every option row first, so on a heavy-news day (NVDA/MS/MU in the 11-16 and 11-17
# 13:00 lean runs) the director saw zero contracts and could not pick an option. Ticker news is also in
# the CATALYSTS section; the chain is nowhere else in the package.
_TICKER_STEPS = [
    {"news": 10}, {"peers": 3}, {"actions": 5}, {"quarters": 4}, {"news": 5},
    {"chain_links": 4}, {"summary": False}, {"chain_expiries": 2}, {"news": 0},
    {"chain_expiries": 1}, {"chain_expiries": 0},
]
_MACRO_STEPS = [{"transmission": 20}, {"transmission": 10}]

# (2026-11-18; used only when the caller asks for it: dossier_text(..., keep_options=True), which the director does
# when analyst_desk.opus_director.v2.writeups_keep_option_rows is true, OFF by default) The DEBATE section gives way
# BEFORE any option row. On 11-16..11-18 at 13:00 NVDA (and MS, MU on the old order) showed no contract at all while
# its debate, about 320 tokens of a failed arbiter verdict and day-old bull/bear text, stayed. The three debate
# steps go right before the first step that cuts option rows; every other step keeps today's place and order, so a
# write-up that fits before the chain is touched comes out exactly as with _TICKER_STEPS, and one that does not never
# shows fewer option rows than _TICKER_STEPS gives it (each later stage is today's minus debate lines).
_DEBATE_STEPS = [{"debate": "judged"}, {"debate": "fresh"}, {"debate": "off"}]
_DEBATE_OLD_HOURS = 24          # a verdict or bull/bear older than this, before the write-up was built, is "old"


def _keep_options_steps(steps: list) -> list:
    """`steps` with _DEBATE_STEPS put in right before the first step that cuts option rows (chain_expiries)."""
    i = next((k for k, s in enumerate(steps) if "chain_expiries" in s), len(steps))
    return list(steps[:i]) + [dict(s) for s in _DEBATE_STEPS] + list(steps[i:])


def _verdict_judged(v: dict) -> bool:
    """False for an arbiter 'verdict' that is no judgement of the stock: its model call failed (mason_arbiter writes
    "REJECT: arbiter model call failed after …", mason_arbiter.py:613) or a code rule decided it without asking a
    model (no model recorded; "… Hard gate decided in code …; no model call was made.", mason_arbiter.py:581-583).
    Checked on all 694 stored verdicts (2026-11-18): the 565 judged ones all name their model, the 127 code-rule
    ones name none, and one is a failed call."""
    r = str(v.get("reasoning") or "")
    return not (r.startswith("REJECT: arbiter model call failed") or not v.get("model")
                or "no model call was made" in r)


def _debate_lines_kept(d: dict, level) -> list:
    """(keep-option-rows ladder only) The DEBATE section's lines with its least useful parts left out. The levels
    add up:
      "judged": leave out an arbiter verdict that is no judgement (_verdict_judged) with its plan line, the empty
                "no usable debate/verdict …" line and an ERROR line;
      "fresh":  also leave out a verdict or a bull/bear older than _DEBATE_OLD_HOURS before the write-up was built
                (its own built_at, not the clock; a time that cannot be read counts as old);
      anything else ("off"): no DEBATE section.
    What is kept is written exactly as today, and the header stays only while something is left under it. No line
    says what was left out (like the peers, analyst-actions, quarters and description steps). Never raises; on an
    unexpected payload the section is left out."""
    try:
        if level not in ("judged", "fresh"):
            return []
        db = d.get("debate")
        if not isinstance(db, dict) or _is_err(db) or db.get("none"):
            return []
        built = _to_datetime(d.get("built_at"))

        def recent(item) -> bool:
            if level != "fresh":
                return True
            try:
                ts = _to_datetime(item.get("generated_at"))
                return bool(built and ts and (built - ts).total_seconds() <= _DEBATE_OLD_HOURS * 3600)
            except Exception:              # e.g. one time with a zone and one without: old
                return False

        out = []
        v = db.get("verdict")
        if isinstance(v, dict) and v and _verdict_judged(v) and recent(v):
            out.append(f"Arbiter {v.get('verdict')} @ {v.get('generated_at')} ({v.get('model')}): {v.get('reasoning')}")
            if v.get("plan"):
                out.append("plan: " + ", ".join(f"{k}={v['plan'][k]}" for k in v["plan"]))
        dd = db.get("debate")
        if isinstance(dd, dict) and dd and recent(dd):
            out.append(f"Bull ({dd.get('generated_at')}): {dd.get('bull')}")
            out.append(f"Bear: {dd.get('bear')}")
        return [_hdr(d, "debate", "DEBATE (latest bull/bear + arbiter)")] + out if out else []
    except Exception:
        return []


def dossier_text(d: dict, *, max_tokens: int = 4000, keep_options: bool = False) -> str:
    """Compact, labelled, deterministic prompt text for a ticker OR macro payload.
    Token estimate = len/4; sections dropped per the ladder, then a hard cut.
    keep_options=True (ticker only): the ladder with the DEBATE steps before any option row (_keep_options_steps)."""
    try:
        if not isinstance(d, dict):
            return "DOSSIER unavailable (bad payload)"
        macro = str(d.get("schema_version", "")).startswith("macro")
        if macro:
            lim, steps, render = {"transmission": 999}, _MACRO_STEPS, _render_macro
        else:
            lim = {"chain_expiries": 3, "news": 20, "peers": 5, "actions": 10, "quarters": 8,
                   "chain_links": 99, "summary": True}
            steps, render = _TICKER_STEPS, _render_ticker
            if keep_options is True:
                steps = _keep_options_steps(_TICKER_STEPS)
        budget = max(1, int(max_tokens)) * 4
        text = render(d, lim)
        for step in steps:
            if len(text) <= budget:
                break
            lim.update(step)
            text = render(d, lim)
        if len(text) > budget:
            text = text[:max(0, budget - 16)].rstrip() + "\n…[truncated]"
        return text
    except Exception as e:
        return f"DOSSIER {d.get('subject') if isinstance(d, dict) else '?'} render error: {e}"


def macro_dossier_text(d: dict, *, max_tokens: int = 4000) -> str:
    return dossier_text(d, max_tokens=max_tokens)


def token_estimate(text: str) -> int:
    return len(text or "") // 4


if __name__ == "__main__":  # manual smoke: python3 mason_dossier.py META
    import sys
    args = sys.argv[1:] or ["META"]
    if args[0].lower() == "macro":
        out = build_macro_dossier(args[1:] or ["geopolitical"], headline_hint="", force=True)
    else:
        out = build_dossier(args[0], force=True)
    txt = dossier_text(out)
    print(txt)
    print(f"\n[build {out.get('build_seconds')}s · ~{token_estimate(txt)} tokens · errors {out.get('errors')}]")
