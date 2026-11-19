"""
Mason Daily Brief Engine — v2
==============================
Primary data: yfinance (unlimited, no API key)
Secondary:    FMP API (earnings, economic calendar, sector data)
Backup:       Alpha Vantage (25 calls/day — emergency only)

Functions used by Mason every session:
  get_options_chain()        — full options chain for any ticker
  get_52w_opportunities()    — stocks near 52-week lows with upside signals
  get_earnings_this_week()   — upcoming earnings via FMP
  get_economic_calendar()    — macro events this week via FMP
  log_play()                 — log a play to daily_plays.csv
  review_open_plays()        — pull open plays with current prices
  update_play_outcome()      — score a play after reviewing
  plays_performance_summary() — Mason's track record stats
"""

import json
import sys
import csv
import subprocess
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    print("Warning: yfinance not installed. Run: pip install yfinance --break-system-packages")

from mason_symbols import yf_symbol as _yfs   # R2-A: BRK.B → BRK-B for every yfinance call below

BASE   = Path(__file__).parent
CONFIG = json.loads((BASE / "mason.config.json").read_text())
# B1b: keys via mason_secrets (env > private secrets file > this config); never a KeyError at
# import time when a key or block is absent — the brief degrades as it does for an empty key.
import mason_secrets as _S
_APIS = CONFIG.get("apis") or {}
AV_KEY = _S.get("ALPHA_VANTAGE_API_KEY", "", cfg=CONFIG)
AV_URL = (_APIS.get("alpha_vantage") or {}).get("base_url", "https://www.alphavantage.co/query")
FMP_KEY  = _S.get("FMP_API_KEY", "", cfg=CONFIG)
FMP_URL  = (_APIS.get("fmp") or {}).get("base_url", "https://financialmodelingprep.com/api/v3")  # v3 (some endpoints still work)
FMP_V4   = "https://financialmodelingprep.com/api/v4"  # newer endpoints
FMP_STABLE = "https://financialmodelingprep.com/stable" # stable channel


# ═══════════════════════════════════════════════════════════════════
# YFINANCE DATA LAYER (primary)
# ═══════════════════════════════════════════════════════════════════

def get_quote_yf(ticker: str) -> dict:
    """Full, LIVE quote data via yfinance.

    Source of truth is ``Ticker.info``'s ``regularMarket*`` fields — they are the
    real-time price and, crucially, the CORRECT previous close for the day-change
    math. (The old path read ``history(period="1y").Close.iloc[-1]`` as "price",
    which is a lagging/adjusted DAILY close, not the live tick — that was the
    source of the wrong day-change % Nico flagged. ``fast_info.previous_close`` is
    ALSO unreliable: measured 2026-09-11 it returned 110.4 for HOOD vs the correct
    115.28, flipping a real -1.7% day into a fake +2.7%. So use ``.info``.)

    ``.info`` is ~0.2s/ticker (verified) and carries everything Mason needs to talk
    about a name like a real analyst: live price + day change, the session's open
    and high/low, 52-week range, volume vs average volume, market cap, and P/E.

    Returns every key the old shape had (callers depend on them) PLUS the new rich
    fields. Falls back to fast_info + short history, then Alpha Vantage, so a
    ``.info`` hiccup degrades instead of returning a wrong number."""
    if not YF_AVAILABLE:
        return get_quote_av(ticker)

    def _num(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    try:
        tk   = yf.Ticker(_yfs(ticker))
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}

        price      = _num(info.get("regularMarketPrice"))
        prev_close = _num(info.get("regularMarketPreviousClose"))
        high_52    = _num(info.get("fiftyTwoWeekHigh"))
        low_52     = _num(info.get("fiftyTwoWeekLow"))
        day_open   = _num(info.get("regularMarketOpen"))
        day_high   = _num(info.get("regularMarketDayHigh"))
        day_low    = _num(info.get("regularMarketDayLow"))
        volume     = _num(info.get("regularMarketVolume"))
        avg_vol    = _num(info.get("averageVolume") or info.get("averageDailyVolume10Day"))
        mcap       = _num(info.get("marketCap"))
        pe         = _num(info.get("trailingPE"))
        fwd_pe     = _num(info.get("forwardPE"))
        name       = info.get("shortName") or info.get("longName")

        # ── Fallback: .info was empty/blocked. Use fast_info for the live price and
        #    a short history for the 52w range, so we still return a real number. ──
        if not price:
            try:
                fi = tk.fast_info
                price   = _num(fi["last_price"])
                day_open = day_open or _num(fi["open"])
                day_high = day_high or _num(fi["day_high"])
                day_low  = day_low  or _num(fi["day_low"])
                high_52  = high_52  or _num(fi["year_high"])
                low_52   = low_52   or _num(fi["year_low"])
                mcap     = mcap     or _num(fi["market_cap"])
                volume   = volume   or _num(fi["last_volume"])
            except Exception:
                pass
        if not price or not (high_52 and low_52):
            hist = tk.history(period="1y", interval="1d")
            if len(hist) >= 1:
                price   = price   or round(float(hist["Close"].iloc[-1]), 2)
                high_52 = high_52 or round(float(hist["High"].max()), 2)
                low_52  = low_52  or round(float(hist["Low"].min()), 2)
                if not prev_close and len(hist) >= 2:
                    prev_close = round(float(hist["Close"].iloc[-2]), 2)
                if not volume:
                    volume = int(hist["Volume"].iloc[-1])

        if not price:
            return {"ticker": ticker, "error": "no live price", "price": 0}

        price = round(price, 2)
        if prev_close:
            prev_close = round(prev_close, 2)
            change     = round(price - prev_close, 2)
            change_pct = round((change / prev_close * 100) if prev_close else 0, 2)
        else:
            prev_close = price
            change     = 0.0
            change_pct = 0.0

        out = {
            "ticker":       ticker,
            "price":        price,
            "change":       change,
            "change_pct":   change_pct,
            "prev_close":   prev_close,
            "52w_high":     round(high_52, 2) if high_52 else None,
            "52w_low":      round(low_52, 2) if low_52 else None,
            "pct_from_52h": round((price - high_52) / high_52 * 100, 1) if high_52 else None,
            "pct_from_52l": round((price - low_52)  / low_52  * 100, 1) if low_52 else None,
            "volume":       int(volume) if volume else None,
            # ── rich fields (new) ────────────────────────────────────────────────
            "open":         round(day_open, 2) if day_open else None,
            "day_high":     round(day_high, 2) if day_high else None,
            "day_low":      round(day_low, 2) if day_low else None,
            "avg_volume":   int(avg_vol) if avg_vol else None,
            "market_cap":   int(mcap) if mcap else None,
            "pe":           round(pe, 2) if pe else None,
            "forward_pe":   round(fwd_pe, 2) if fwd_pe else None,
            "name":         name,
        }
        return out
    except Exception as e:
        return {"ticker": ticker, "error": str(e), "price": 0}


def get_rsi_yf(ticker: str, period: int = 14) -> float:
    """RSI-14 calculated from yfinance daily closes."""
    if not YF_AVAILABLE:
        return None
    try:
        hist   = yf.Ticker(_yfs(ticker)).history(period="60d", interval="1d")
        closes = hist["Close"]
        delta  = closes.diff()
        gain   = delta.clip(lower=0).rolling(period).mean()
        loss   = (-delta.clip(upper=0)).rolling(period).mean()
        rs     = gain / loss
        rsi    = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except:
        return None


def get_options_chain(ticker: str, expiry: str = None) -> dict:
    """
    Returns full options chain for a ticker.
    expiry: 'YYYY-MM-DD' string, or None for nearest expiry.

    Returns dict with keys: expiry, calls (DataFrame), puts (DataFrame)
    Useful for: finding cheap options, identifying unusual activity,
                building Mason's options play recommendations.
    """
    if not YF_AVAILABLE:
        return {"error": "yfinance not available"}
    try:
        tk       = yf.Ticker(_yfs(ticker))
        expiries = tk.options
        if not expiries:
            return {"error": f"No options available for {ticker}"}

        # Use requested expiry or nearest one
        target = expiry if expiry in expiries else expiries[0]
        chain  = tk.option_chain(target)

        return {
            "ticker":       ticker,
            "expiry":       target,
            "all_expiries": list(expiries[:6]),  # next 6 expiries
            "calls":        chain.calls.to_dict("records"),
            "puts":         chain.puts.to_dict("records"),
            "spot_price":   get_quote_yf(ticker)["price"],
        }
    except Exception as e:
        return {"error": str(e)}


def get_atm_options(ticker: str, expiry: str = None) -> dict:
    """
    Returns the 3 closest-to-money calls and puts for a ticker.
    Mason uses this to quickly describe an options play without drowning in data.
    """
    chain = get_options_chain(ticker, expiry)
    if "error" in chain:
        return chain

    spot   = chain["spot_price"]
    expiry = chain["expiry"]

    def closest_strikes(options: list, n: int = 3) -> list:
        if not options:
            return []
        sorted_opts = sorted(options, key=lambda x: abs(x["strike"] - spot))
        return sorted_opts[:n]

    atm_calls = closest_strikes(chain["calls"])
    atm_puts  = closest_strikes(chain["puts"])

    return {
        "ticker":    ticker,
        "spot":      spot,
        "expiry":    expiry,
        "atm_calls": [{"strike": o["strike"], "bid": o["bid"], "ask": o["ask"],
                        "iv": round(o.get("impliedVolatility", 0) * 100, 1),
                        "delta": round(o.get("delta", 0), 2) if o.get("delta") else "—",
                        "oi": o.get("openInterest", 0)} for o in atm_calls],
        "atm_puts":  [{"strike": o["strike"], "bid": o["bid"], "ask": o["ask"],
                        "iv": round(o.get("impliedVolatility", 0) * 100, 1),
                        "delta": round(o.get("delta", 0), 2) if o.get("delta") else "—",
                        "oi": o.get("openInterest", 0)} for o in atm_puts],
    }


def get_historical(ticker: str, period: str = "6mo") -> list[dict]:
    """
    Daily OHLCV history. Periods: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max
    Used for: trend analysis, support/resistance, backtest play ideas.
    """
    if not YF_AVAILABLE:
        return []
    try:
        hist = yf.Ticker(_yfs(ticker)).history(period=period, interval="1d")
        return [
            {"date": str(idx.date()), "open": round(row.Open, 2),
             "high": round(row.High, 2), "low": round(row.Low, 2),
             "close": round(row.Close, 2), "volume": int(row.Volume)}
            for idx, row in hist.iterrows()
        ]
    except:
        return []


# ═══════════════════════════════════════════════════════════════════
# FMP DATA LAYER (earnings, economic calendar, sector)
# ═══════════════════════════════════════════════════════════════════

def get_earnings_this_week() -> list[dict]:
    """
    Upcoming earnings — tries FMP v4/stable first, falls back to yfinance
    calendar checks on a curated watchlist of high-impact tickers.
    """
    today = date.today()
    end   = today + timedelta(days=7)

    # Try FMP v4 endpoint first
    for base in [FMP_V4, FMP_STABLE]:
        try:
            r    = requests.get(f"{base}/earning-calendar", params={
                "from": today.isoformat(), "to": end.isoformat(), "apikey": FMP_KEY
            }, timeout=8)
            data = r.json()
            if isinstance(data, list) and data and isinstance(data[0], dict) and "symbol" in data[0]:
                cleaned = []
                for e in data:
                    sym = e.get("symbol") or e.get("ticker", "")
                    if not sym:
                        continue
                    cleaned.append({
                        "date":        e.get("date", ""),
                        "symbol":      sym,
                        "eps_est":     e.get("epsEstimated") or e.get("eps", "—"),
                        "revenue_est": e.get("revenueEstimated") or "—",
                        "time":        e.get("time") or e.get("hour") or "—",
                        "source":      "fmp",
                    })
                cleaned.sort(key=lambda x: x["date"])
                return cleaned[:25]
        except Exception:
            continue

    # FMP failed — fall back to yfinance earnings_dates on high-impact watchlist
    WATCHLIST = [
        "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","NFLX",
        "AMD","AVGO","TSM","IONQ","RKLB","LUNR","JPM","GS","BAC",
        "MU","QCOM","INTC","AMAT","CRM","ORCL","SNOW","PLTR",
        "WMT","KO","PEP","JNJ","UNH","HD","V","MA","PYPL","DIS",
    ]
    results = []
    if not YF_AVAILABLE:
        return [{"note": "yfinance not installed — Mason will WebSearch earnings calendar"}]

    # Extend window slightly to catch borderline dates
    window_end = end + timedelta(days=2)

    for sym in WATCHLIST:
        try:
            ed = yf.Ticker(_yfs(sym)).earnings_dates
            if ed is None or ed.empty:
                continue
            for idx in ed.index:
                try:
                    edate = idx.date() if hasattr(idx, "date") else idx
                    if today <= edate <= window_end:
                        row = ed.loc[idx]
                        eps_est = row.get("EPS Estimate")
                        results.append({
                            "date":        str(edate),
                            "symbol":      sym,
                            "eps_est":     round(float(eps_est), 2) if eps_est and str(eps_est) != "nan" else "—",
                            "revenue_est": "—",
                            "time":        "—",
                            "source":      "yfinance",
                        })
                except Exception:
                    continue
        except Exception:
            continue

    results.sort(key=lambda x: x["date"])

    if not results:
        return [{"note": f"No major earnings from watchlist {today.strftime('%b %d')}–{window_end.strftime('%b %d')}. "
                         f"Quiet period between Q1 (ends May) and Q2 (starts mid-July) seasons. "
                         f"Mason will WebSearch for any mid-cap reports."}]
    return results


def get_economic_calendar() -> list[dict]:
    """
    Key macro events this week — Google News RSS keyword search.
    FMP economic calendar endpoint requires a higher-tier plan; skipped.
    """
    today = date.today()

    # Google News RSS fallback for key macro keywords.
    # Not structured data, but better than a blank "unavailable" notice.
    try:
        import urllib.parse as _up
        import re as _re
        week_str = today.strftime("%B %d")
        MACRO_QUERIES = [
            f"Federal Reserve Fed meeting week of {week_str}",
            f"CPI inflation report {today.strftime('%B %Y')}",
            f"jobs report NFP {today.strftime('%B %Y')}",
            f"GDP economic data release {today.strftime('%B %Y')}",
        ]
        seen: set = set()
        headlines: list = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Mason/1.0)"}
        for q in MACRO_QUERIES:
            try:
                url = f"https://news.google.com/rss/search?q={_up.quote(q)}&hl=en-US&gl=US&ceid=US:en"
                r   = requests.get(url, timeout=6, headers=headers)
                if not r.ok:
                    continue
                titles = _re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
                for t in titles[1:3]:   # skip feed-level title; take top 2 per query
                    key = t[:40].lower()
                    if key not in seen:
                        seen.add(key)
                        headlines.append({"event": t, "impact": "High", "source": "news"})
            except Exception:
                continue
        if headlines:
            return headlines[:6]
    except Exception:
        pass

    return [{"note": "Macro calendar unavailable — ask Mason to search for this week's events."}]


def get_sector_performance() -> list[dict]:
    """Today's sector ETF performance — shows what's rotating in/out."""
    SECTOR_ETFS = {
        "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
        "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Cons Discretionary",
        "XLP": "Cons Staples", "XLU": "Utilities", "XLRE": "Real Estate",
        "XLB": "Materials", "XLC": "Comm Services",
    }
    results = []
    for etf, sector in SECTOR_ETFS.items():
        q = get_quote_yf(etf)
        results.append({
            "sector": sector, "etf": etf,
            "price": q.get("price", 0),
            "day_pct": q.get("change_pct", 0),
        })
    results.sort(key=lambda x: x["day_pct"], reverse=True)
    return results


def get_52w_opportunities(universe: list[str] = None) -> list[dict]:
    """
    Scan a list of tickers for stocks near 52-week lows with recovery signals.
    Default universe: S&P 500 sector leaders + Nico's watchlist themes.
    Used by Mason to find dip-buy candidates for the daily plays section.
    """
    if universe is None:
        # Default scan universe — mix of liquid names across Nico's themes
        universe = [
            # AI / Semi
            "NVDA","AMD","AVGO","QCOM","MU","AMAT","LRCX","KLAC","INTC",
            # Space
            "RKLB","LUNR","ASTS","RDW","SPCE",
            # Quantum
            "IONQ","QUBT","RGTI","QBTS",
            # Broad market leaders
            "AAPL","MSFT","GOOGL","META","AMZN","TSLA","NFLX",
            # Financials
            "JPM","GS","BAC",
        ]

    opportunities = []
    for ticker in universe:
        q = get_quote_yf(ticker)
        if "error" in q or q.get("price", 0) == 0:
            continue
        pct_from_low  = q.get("pct_from_52l", 999)
        pct_from_high = q.get("pct_from_52h", 0)

        # Flag if within 20% of 52-week low AND more than 30% off 52-week high
        if pct_from_low <= 20 and pct_from_high <= -30:
            rsi = get_rsi_yf(ticker)
            opportunities.append({
                "ticker":        ticker,
                "price":         q["price"],
                "52w_low":       q["52w_low"],
                "52w_high":      q["52w_high"],
                "pct_from_low":  pct_from_low,
                "pct_from_high": pct_from_high,
                "rsi":           rsi,
                "oversold":      rsi < 35 if rsi else False,
            })

    opportunities.sort(key=lambda x: x["pct_from_low"])
    return opportunities


# ═══════════════════════════════════════════════════════════════════
# ALPHA VANTAGE FALLBACK
# ═══════════════════════════════════════════════════════════════════

def get_quote_av(ticker: str) -> dict:
    """Alpha Vantage fallback — use only if yfinance unavailable."""
    try:
        r = requests.get(AV_URL, params={
            "function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": AV_KEY
        }, timeout=10)
        q = r.json().get("Global Quote", {})
        return {
            "ticker":     ticker,
            "price":      float(q.get("05. price", 0)),
            "change":     float(q.get("09. change", 0)),
            "change_pct": float(q.get("10. change percent", "0%").replace("%", "")),
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e), "price": 0}


def format_options_for_brief(plays: list, quotes: dict) -> str:
    """
    Format open options plays for the brief.
    plays   — list of play dicts from mason_options.get_open_plays()
    quotes  — {ticker: get_quote_yf(ticker) result}
    """
    today     = date.today()
    expiring  = []   # ≤ 7 days to expiry
    running   = []   # > 7 days

    for p in plays:
        ticker    = p.get("ticker", "?")
        strategy  = p.get("strategy", "?").replace("_", " ")
        direction = p.get("direction", "").lower()
        entry_px  = float(p.get("underlying_price_at_entry") or 0)
        breakeven = float(p.get("breakeven") or 0)
        curr_px   = float((quotes.get(ticker) or {}).get("price") or 0)

        try:
            exp_date  = date.fromisoformat(str(p.get("expiration", "")))
            days_left = (exp_date - today).days
        except Exception:
            days_left = 99

        if days_left <= 0:
            continue  # already expired

        # Directional status — is the underlying moving the right way?
        if curr_px and entry_px:
            above_bke = curr_px >= breakeven if breakeven else curr_px >= entry_px
            moved_up  = curr_px > entry_px * 1.002  # 0.2% noise buffer

            if direction in ("bullish", "bull"):
                if above_bke:
                    status = "in range to profit"
                elif moved_up:
                    status = "moving right direction"
                else:
                    status = "under pressure"
            elif direction in ("bearish", "bear"):
                if not above_bke:
                    status = "in range to profit"
                elif not moved_up:
                    status = "moving right direction"
                else:
                    status = "under pressure"
            else:
                status = "neutral — watching"
        else:
            status = "—"

        def _fmt_px(n):
            if not n:
                return "?"
            return f"${n:,.0f}" if n >= 100 else f"${n:.2f}"

        item = {
            "ticker":     ticker,
            "strategy":   strategy,
            "expiration": str(p.get("expiration", "?")),
            "days_left":  days_left,
            "entry_str":  _fmt_px(entry_px),
            "curr_str":   _fmt_px(curr_px),
            "bke_str":    _fmt_px(breakeven),
            "status":     status,
            "confidence": p.get("mason_confidence", "?"),
        }

        if days_left <= 7:
            expiring.append(item)
        else:
            running.append(item)

    lines = []

    if expiring:
        lines.append("  Expiring this week:")
        for it in sorted(expiring, key=lambda x: x["days_left"]):
            day_tag = "TOMORROW" if it["days_left"] == 1 else f"in {it['days_left']}d"
            lines.append(
                f"    {it['ticker']:<6}  {it['strategy']:<22}  exp {it['expiration']} ({day_tag})"
                f"  entry {it['entry_str']} -> now {it['curr_str']}"
                f"  bke {it['bke_str']}  | {it['status'].upper()}"
            )

    if running:
        if expiring:
            lines.append("")
        lines.append("  Still running:")
        for it in sorted(running, key=lambda x: x["days_left"]):
            lines.append(
                f"    {it['ticker']:<6}  {it['strategy']:<22}  exp {it['expiration']}"
                f"  entry {it['entry_str']} -> now {it['curr_str']}"
                f"  bke {it['bke_str']}  | {it['status'].upper()}"
            )

    return "\n".join(lines) if lines else "  No open plays."


def format_sector_for_brief(sectors: list[dict]) -> str:
    """Formatted sector rotation snapshot — no unicode, conversational."""
    winners  = [s for s in sectors if s["day_pct"] > 0]
    losers   = [s for s in sectors if s["day_pct"] <= 0]
    lines = []
    if winners:
        parts = [f"{s['sector']} up {s['day_pct']:.1f}%" for s in winners[:4]]
        lines.append("  Leading today: " + ", ".join(parts))
    if losers:
        parts = [f"{s['sector']} down {abs(s['day_pct']):.1f}%" for s in losers[:4]]
        lines.append("  Lagging today: " + ", ".join(parts))
    return "\n".join(lines) if lines else "  Sector data unavailable."


# ═══════════════════════════════════════════════════════════════════
# DAILY PLAYS LOG
# ═══════════════════════════════════════════════════════════════════

PLAY_FIELDS = [
    "id", "date_logged", "ticker", "play_type", "thesis",
    "entry_price", "target_price", "confidence",
    "option_strike", "option_expiry", "option_type",
    "option_premium", "max_loss", "breakeven", "notes",
    "review_date", "price_at_review", "outcome", "outcome_score", "review_notes",
]

def _next_play_id() -> int:
    plays_file = BASE / "daily_plays.csv"
    if not plays_file.exists():
        return 1
    with open(plays_file) as f:
        rows = list(csv.DictReader(f))
    return len(rows) + 1


def log_play(
    ticker: str,
    play_type: str,           # "call" | "put" | "long" | "short" | "watch"
    thesis: str,
    entry_price: float,
    confidence: str,          # "high" | "medium" | "speculative"
    target_price: float  = None,
    option_strike: float = None,
    option_expiry: str   = None,
    option_type: str     = None,
    option_premium: float= None,
    max_loss: float      = None,
    breakeven: float     = None,
    notes: str           = "",
) -> dict:
    """Log a play to daily_plays.csv. Called by Mason during every brief."""
    play = {
        "id":             _next_play_id(),
        "date_logged":    date.today().isoformat(),
        "ticker":         ticker,
        "play_type":      play_type,
        "thesis":         thesis,
        "entry_price":    entry_price,
        "target_price":   target_price or "",
        "confidence":     confidence,
        "option_strike":  option_strike or "",
        "option_expiry":  option_expiry or "",
        "option_type":    option_type or "",
        "option_premium": option_premium or "",
        "max_loss":       max_loss or "",
        "breakeven":      breakeven or "",
        "notes":          notes,
        "review_date":    "",
        "price_at_review": "",
        "outcome":        "open",
        "outcome_score":  "",
        "review_notes":   "",
    }

    plays_file = BASE / "daily_plays.csv"
    file_exists = plays_file.exists() and plays_file.stat().st_size > 0

    with open(plays_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PLAY_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(play)

    return play


def review_open_plays() -> list[dict]:
    """All open plays with current prices for scoring."""
    plays_file = BASE / "daily_plays.csv"
    if not plays_file.exists():
        return []

    with open(plays_file) as f:
        plays = list(csv.DictReader(f))

    open_plays = [p for p in plays if p.get("outcome") == "open"]

    for play in open_plays:
        q = get_quote_yf(play["ticker"])
        play["current_price"]     = q.get("price", "?")
        play["current_day_pct"]   = q.get("change_pct", "?")
        try:
            entry = float(play["entry_price"])
            curr  = float(play["current_price"])
            play["move_since_logged"] = round(((curr - entry) / entry) * 100, 2)
            play["hit_target"] = (
                float(play["target_price"]) <= curr
                if play.get("target_price") else None
            )
        except:
            play["move_since_logged"] = "?"
            play["hit_target"]        = None

    return open_plays


def update_play_outcome(play_id: int, outcome: str, score: int, review_notes: str = ""):
    """Write outcome back to daily_plays.csv."""
    plays_file = BASE / "daily_plays.csv"
    with open(plays_file) as f:
        plays = list(csv.DictReader(f))

    for play in plays:
        if int(play.get("id", -1)) == play_id:
            play["review_date"]     = date.today().isoformat()
            play["outcome"]         = outcome
            play["outcome_score"]   = score
            play["review_notes"]    = review_notes
            q = get_quote_yf(play["ticker"])
            play["price_at_review"] = q.get("price", "?")

    with open(plays_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PLAY_FIELDS)
        writer.writeheader()
        writer.writerows(plays)


def plays_performance_summary() -> dict:
    """Mason's track record — aggregate stats on all reviewed plays."""
    plays_file = BASE / "daily_plays.csv"
    if not plays_file.exists():
        return {"message": "No plays logged yet"}

    with open(plays_file) as f:
        plays = list(csv.DictReader(f))

    reviewed = [p for p in plays if p.get("outcome") not in ("open", "")]
    if not reviewed:
        return {"total_plays": len(plays), "reviewed": 0, "message": "No plays reviewed yet"}

    scores   = [int(p["outcome_score"]) for p in reviewed if p.get("outcome_score")]
    hits     = [p for p in reviewed if p["outcome"] == "hit"]
    partials = [p for p in reviewed if p["outcome"] == "partial"]
    misses   = [p for p in reviewed if p["outcome"] == "missed"]
    by_conf  = {}
    by_type  = {}

    for p in reviewed:
        for key, val in [("confidence", p.get("confidence")), ("play_type", p.get("play_type"))]:
            bucket = by_conf if key == "confidence" else by_type
            bucket.setdefault(val, {"count": 0, "scores": [], "hits": 0})
            bucket[val]["count"] += 1
            if p.get("outcome_score"):
                bucket[val]["scores"].append(int(p["outcome_score"]))
            if p["outcome"] == "hit":
                bucket[val]["hits"] += 1

    def summarize(bucket):
        return {
            k: {"count": v["count"],
                "hit_rate": round(v["hits"]/v["count"]*100,1),
                "avg_score": round(sum(v["scores"])/len(v["scores"]),1) if v["scores"] else 0}
            for k, v in bucket.items()
        }

    return {
        "total_logged":   len(plays),
        "total_reviewed": len(reviewed),
        "open":           len(plays) - len(reviewed),
        "hit_rate":       round(len(hits)     / len(reviewed) * 100, 1),
        "partial_rate":   round(len(partials) / len(reviewed) * 100, 1),
        "miss_rate":      round(len(misses)   / len(reviewed) * 100, 1),
        "avg_score":      round(sum(scores)/len(scores), 1) if scores else 0,
        "by_confidence":  summarize(by_conf),
        "by_play_type":   summarize(by_type),
    }


# ═══════════════════════════════════════════════════════════════════
# QUICK RUN — test from command line
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--plays" in sys.argv:
        from play_review import print_review_sheet, performance_report
        print_review_sheet()
        print(performance_report())

    elif "--52w" in sys.argv:
        print("Scanning for 52-week low opportunities...\n")
        opps = get_52w_opportunities()
        for o in opps:
            rsi_tag = " ⚡ OVERSOLD" if o.get("oversold") else ""
            print(f"  {o['ticker']:<6} ${o['price']:.2f}  "
                  f"{o['pct_from_low']:+.1f}% from 52L  /  "
                  f"{o['pct_from_high']:+.1f}% from 52H  "
                  f"RSI {o['rsi']}{rsi_tag}")

    elif "--options" in sys.argv and len(sys.argv) > 2:
        ticker = sys.argv[sys.argv.index("--options") + 1].upper()
        print(f"ATM options for {ticker}:\n")
        atm = get_atm_options(ticker)
        if "error" in atm:
            print(f"  Error: {atm['error']}")
        else:
            print(f"  Spot: ${atm['spot']}  |  Expiry: {atm['expiry']}\n")
            print("  CALLS:")
            for c in atm["atm_calls"]:
                print(f"    Strike ${c['strike']}  bid/ask ${c['bid']}/{c['ask']}  IV {c['iv']}%  OI {c['oi']}")
            print("  PUTS:")
            for p in atm["atm_puts"]:
                print(f"    Strike ${p['strike']}  bid/ask ${p['bid']}/{p['ask']}  IV {p['iv']}%  OI {p['oi']}")

    elif "--earnings" in sys.argv:
        print("Earnings this week:\n")
        results = get_earnings_this_week()
        if not results:
            print("  No data returned.")
        elif "note" in results[0]:
            print(f"  ℹ️  {results[0]['note']}")
        elif "error" in results[0]:
            print(f"  ❌ {results[0]['error']}")
        else:
            for e in results:
                src = f" [{e.get('source','?')}]" if e.get("source") else ""
                print(f"  {e.get('date',''):<12}  {e.get('symbol',''):<8}  "
                      f"EPS est: {e.get('eps_est','—'):<8}  "
                      f"Rev est: {e.get('revenue_est','—'):<14}  "
                      f"Time: {e.get('time','—')}{src}")

    elif "--macro" in sys.argv:
        print("Economic calendar this week:\n")
        events = get_economic_calendar()
        for e in events:
            if "note" in e:
                print(f"  {e['note']}")
            elif "error" in e:
                print(f"  {e['error']}")
            else:
                actual  = f"  actual: {e['actual']}" if e.get("actual") else ""
                est     = f"  est: {e.get('estimate','—')}" if e.get("estimate") else ""
                print(f"  {e.get('date',''):<14}  {e.get('event',''):<45}  "
                      f"Impact: {e.get('impact','?')}{est}{actual}")

    elif "--sectors" in sys.argv:
        print(format_sector_for_brief(get_sector_performance()))

    else:
        # ── Full Morning Brief ────────────────────────────────────────
        import random
        now  = datetime.now()
        ts   = now.strftime("%A, %B %d %Y  %I:%M %p")

        INTROS = [
            "Scanning complete. Here is your market summary.\n",
            "Your morning brief. [MASON IS PROUD]\n",
            "Compiled and confirmed. Your brief is below.\n",
            "Mason has been preparing this. [MASON TAKES BRIEFS SERIOUSLY]\n",
            "All data points checked. Your summary:\n",
            "Here is what happened while you were away. [QUITE A BIT]\n",
            "Mason ran the full scan. Results follow.\n",
        ]
        print(random.choice(INTROS))
        print(f"  {ts}")
        print()

        # ETF ticker -> sector name map for display
        ETF_NAME = {
            "XLK": "Tech", "XLF": "Financials", "XLE": "Energy",
            "XLV": "Healthcare", "XLI": "Industrials", "XLY": "Consumer Discretionary",
            "XLP": "Consumer Staples", "XLU": "Utilities", "XLRE": "Real Estate",
            "XLB": "Materials", "XLC": "Comm Services",
        }

        # -- Market Posture
        try:
            posture_script = Path(__file__).parent / "mason_market_posture.py"
            import os
            pr = subprocess.run(
                [sys.executable, str(posture_script)],
                capture_output=True, text=True, timeout=60,
                encoding='utf-8', errors='replace',
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )
            if pr.returncode == 0 and pr.stdout.strip():
                pd2 = json.loads(pr.stdout.strip())
                sec = pd2.get("sector", {})
                tr  = pd2.get("top_risk", {})
                zone = tr.get("zone", "unknown")
                score = tr.get("score", "?")
                phase = sec.get("cycle_phase", "---")
                # Convert ETF tickers to readable names
                leaders_raw  = sec.get("leaders", [])
                laggards_raw = sec.get("laggards", [])
                leaders  = ", ".join(ETF_NAME.get(t, t) for t in leaders_raw)
                laggards = ", ".join(ETF_NAME.get(t, t) for t in laggards_raw)
                comps = tr.get("components", {})

                print("  -- MARKET POSTURE --")
                print(f"  {phase} | Risk {zone.upper()} at {score}/100. {tr.get('summary','')}")
                if leaders:
                    print(f"  Leading sectors: {leaders}.")
                if laggards:
                    print(f"  Lagging sectors: {laggards}.")
                # Highlight any component that's flagging
                flags = []
                for k, v in comps.items():
                    if v.get("score", 0) > 0:
                        flags.append(f"{k.replace('_',' ')}: {v.get('label','')}")
                if flags:
                    print(f"  Flags: {', '.join(flags)}.")
                print()
        except Exception as e:
            print(f"  Market posture unavailable: {e}\n")

        # -- Material news (Phase 4: high-materiality, book-relevant, qwen-scored
        #    events from the news service; fire-once — folded in here, then marked
        #    so they don't repeat in the next brief). See NEWS_SERVICE_DESIGN.md.
        try:
            import mason_news_service as _NS
            _mat = _NS.material_events_for_brief(limit=6)
            if _mat:
                print("  -- MATERIAL NEWS --")
                for _m in _mat:
                    _tks = ", ".join((_m.get("tickers") or [])[:3])
                    _d = (_m.get("direction") or "").upper()
                    _line = f"  [{_d}] {_m.get('headline','')}"
                    if _tks:
                        _line += f" ({_tks})"
                    print(_line)
                    if _m.get("implication"):
                        print(f"      -> {_m['implication']}")
                print()
        except Exception:
            pass

        # -- Sector Rotation (intraday)
        print("  -- SECTOR ROTATION --")
        try:
            import os
            sectors = get_sector_performance()
            print(format_sector_for_brief(sectors))
        except Exception as e:
            print(f"  Sector data unavailable: {e}")
        print()

        # -- Open Stock Trades
        try:
            import mason_trades as _mt
            open_trades = _mt.get_open_trades()
            n = len(open_trades)
            print(f"  -- STOCK TRADES ({n} open) --")
            if open_trades:
                today = date.today()
                for t in open_trades:
                    direction = t.get("direction", "long").upper()
                    ticker    = t.get("ticker", "?")
                    # Get live price
                    q = get_quote_yf(ticker)
                    current = q.get("price") if not q.get("error") else None
                    entry   = t.get("entry_price", 0)
                    stop    = t.get("stop_loss", 0)
                    target  = t.get("target_price", 0)
                    rr      = t.get("risk_reward_ratio", "?")
                    conf    = t.get("mason_confidence", "?")
                    setup   = t.get("setup_type", "")
                    horizon = t.get("time_horizon", "")
                    # Status assessment
                    if current:
                        if direction == "LONG":
                            if current >= target:
                                status = "AT TARGET"
                            elif current <= stop:
                                status = "AT STOP - REVIEW"
                            elif current > entry:
                                status = "WORKING"
                            else:
                                status = "UNDER ENTRY"
                        else:  # SHORT
                            if current <= target:
                                status = "AT TARGET"
                            elif current >= stop:
                                status = "AT STOP - REVIEW"
                            elif current < entry:
                                status = "WORKING"
                            else:
                                status = "ABOVE ENTRY"
                        price_str = f"now ${current:.2f}"
                    else:
                        status    = "?"
                        price_str = "price unavailable"
                    print(
                        f"  [{t['id']}] {ticker} {direction} | {setup} | {horizon} | "
                        f"entry ${entry} -> {price_str} | stop ${stop} | target ${target} | "
                        f"{rr}:1 R:R | {conf}% | {status}"
                    )
            else:
                print("  No open stock trades.")
        except Exception as e:
            print(f"  Stock trades unavailable: {e}")
        print()

        # -- Options Plays
        try:
            import mason_options as _mo
            open_plays = _mo.get_open_plays()
            n = len(open_plays)
            print(f"  -- OPTIONS PLAYS ({n} open) --")
            if open_plays:
                # Batch-fetch current underlying prices (only unique tickers)
                unique_tickers = list({p.get("ticker", "") for p in open_plays if p.get("ticker")})
                opt_quotes: dict = {}
                for t in unique_tickers:
                    q = get_quote_yf(t)
                    if not q.get("error"):
                        opt_quotes[t] = q
                print(format_options_for_brief(open_plays, opt_quotes))
            else:
                print("  No open plays.")
        except Exception as e:
            print(f"  Options unavailable: {e}")
        print()

        # -- Earnings This Week
        print("  -- EARNINGS THIS WEEK --")
        try:
            earnings = get_earnings_this_week()
            if not earnings:
                print("  Nothing major on the earnings calendar this week.")
            elif "note" in earnings[0]:
                print(f"  {earnings[0]['note']}")
            else:
                for e in earnings[:8]:
                    t_str = e.get("time", "")
                    when = "before open" if "pre" in t_str.lower() else ("after close" if "post" in t_str.lower() or "amc" in t_str.lower() else t_str)
                    print(f"  {e.get('date','')}  {e.get('symbol','')} ({e.get('name','')}) -- EPS est {e.get('eps_est','?')}, {when}")
        except Exception as e:
            print(f"  Earnings unavailable: {e}")
        print()

        # -- Macro Calendar
        print("  -- MACRO CALENDAR --")
        try:
            events = get_economic_calendar()
            if not events:
                print("  No major macro events this week.")
            elif "note" in events[0]:
                print(f"  {events[0]['note']}")
            elif events[0].get("source") == "news":
                # Google News fallback — print headlines, no date/estimate fields
                for ev in events[:6]:
                    print(f"  {ev.get('event','')}")
            else:
                # FMP structured data
                for ev in events[:6]:
                    est    = f", est {ev.get('estimate','?')}" if ev.get("estimate") else ""
                    imp    = ev.get("impact", "")
                    imp_tag = " [HIGH IMPACT]" if imp == "High" else ""
                    print(f"  {ev.get('date','')} -- {ev.get('event','')}{est}{imp_tag}")
        except Exception as e:
            print(f"  Macro calendar unavailable: {e}")
        print()
        print("  [MASON IS DONE HERE] -- That is your full brief.")

# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
# mason_brief.py end guard — mount truncation buffer
