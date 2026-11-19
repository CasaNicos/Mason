"""
mason_news_service.py — Phase 1 of the constantly-scanning news service.
See docs/prompts/NEWS_SERVICE_DESIGN.md.

Responsibilities (Phase 1 only):
  * Poll a curated catalog of RSS/Atom feeds (finance + global events) on an
    interval, with per-feed watermarking + retry, so we stop re-parsing the same
    headlines on every request.
  * Persist new items to mason.db `news_events`, deduped by a stable id
    (sha1 of the normalized headline). Enrich each at ingest with the instant
    impact-dot + ticker-tag heuristics from mason_news (no LLM cost).
  * Expose `recent_events(limit)` so /api/news becomes a cheap DB read shared
    across all clients instead of N live fetches.

Deliberately NOT here yet (later phases): Alpaca/Benzinga WebSocket (P2),
semantic dedup + LLM materiality scoring (P3), Opus ping + calibration (P4),
GDELT + Telegram (P5).

Feed catalog note: the WSJ / NASDAQ / S&P / CNN slugs are lifted from the
`finnews` library (areed1192, MIT) cloned in ~/gitpulls/finance-news-aggregator/.
We reuse its curated feed URLs but keep Mason's own stdlib parser so we pull in
no new runtime dependencies (the env is PEP-668 externally-managed and
finnews needs defusedxml + fake_useragent).
"""

from __future__ import annotations

import json
import time
import random
import hashlib
import threading
import logging
import re
import html as _html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

import mason_db

log = logging.getLogger("mason.news_service")
BASE = Path(__file__).parent

# The websockets client logs every frame at DEBUG — including the outbound auth
# frame, which carries the Alpaca API secret. Keep it at WARNING so the secret
# never lands in mason.log (and to cut per-frame noise).
logging.getLogger("websockets").setLevel(logging.WARNING)

# ── feed catalog ────────────────────────────────────────────────────────────
# (feed_url, source display name, tier)  tier: 1 wire/major, 2 general, 3 fill.
# Tier drives per-fetch item limits (wires get more) and later calibration weight.
# 2026-11-13 (audit R7): seven feeds were dead or frozen and nothing said so. Removed:
#   WSJ ×4 (feeds.a.dj.com — last item January 2025), CNN edition_business (2017),
#   MarketWatch marketpulse (July 2025), Yahoo topfinstories (404).
# Added: free OFFICIAL sources, each checked with a plain GET on 2026-11-13 (fresh
# items, parseable, ETag/Last-Modified). A feed that goes quiet again is now flagged
# (see _feed_health_after_fetch) instead of polling a corpse forever.
FEED_CATALOG = [
    # ── Yahoo Finance ──
    ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC,^IXIC,^DJI&region=US&lang=en-US", "Yahoo Finance", 1),
    # ── CNBC (markets / economy / world) ──
    ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "CNBC", 1),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC", 1),
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html", "CNBC", 2),   # economy
    ("https://www.cnbc.com/id/15839135/device/rss/rss.html", "CNBC", 2),   # world markets
    # ── MarketWatch ──
    ("https://feeds.marketwatch.com/marketwatch/topstories/",  "MarketWatch", 2),
    # ── NASDAQ (slugs from finnews/nasdaq.py) ──
    ("https://www.nasdaq.com/feed/rssoutbound",          "NASDAQ", 2),
    ("https://www.nasdaq.com/feed/nasdaq-original/rss.xml", "NASDAQ", 2),
    # ── Seeking Alpha / Investing.com ──
    ("https://seekingalpha.com/market_currents.xml",   "Seeking Alpha", 2),
    ("https://www.investing.com/rss/news_301.rss",     "Investing.com", 2),
    # ── Global events ──
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC", 1),
    ("https://feeds.bbci.co.uk/news/world/rss.xml",    "BBC", 2),
    ("https://feeds.npr.org/1006/rss.xml",             "NPR", 3),
    ("https://www.theguardian.com/uk/business/rss",    "The Guardian", 3),
    ("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "NYT", 2),
    # ── Official primary sources (free; added 2026-11-13) ──
    ("https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve", 1),
    ("https://www.federalreserve.gov/feeds/speeches.xml",  "Federal Reserve", 2),
    ("https://www.sec.gov/news/pressreleases.rss",         "SEC", 1),
    # FTC competition press releases removed 2026-11-15: that day every ftc.gov address
    # (the home page included) answered this server with a bare "Page Not Found" (the
    # feed's last item was 13 Nov), so no replacement feed could be checked from here. Merger challenges still arrive through
    # the DOJ feed (antitrust filter below) and the newswires. To try it again, put
    # ("https://www.ftc.gov/feeds/press-release-competition.xml", "FTC", 2) back here.
    ("https://www.justice.gov/news/rss?type=press_release&m=1", "DOJ", 2),
    ("https://www.whitehouse.gov/presidential-actions/feed/",   "White House", 1),
    ("https://www.cftc.gov/RSS/RSSGP/rssgp.xml",           "CFTC", 3),
    ("https://www.ecb.europa.eu/rss/press.html",           "ECB", 2),
    ("https://www.census.gov/economic-indicators/indicator.xml", "Census Bureau", 2),
    ("https://apps.bea.gov/rss/rss.xml",                   "BEA", 2),
    ("https://ustr.gov/rss.xml",                           "USTR", 1),
]

# Feeds that publish rarely by nature: their own "gone quiet" limit, in hours
# (the default is news.feeds.stale_hours = 48).
_FEED_MAX_AGE_H = {
    "https://apps.bea.gov/rss/rss.xml": 21 * 24,            # scheduled releases, a few a month
    "https://ustr.gov/rss.xml": 45 * 24,                    # trade announcements come in bursts
    "https://www.federalreserve.gov/feeds/speeches.xml": 14 * 24,
    "https://www.whitehouse.gov/presidential-actions/feed/": 7 * 24,
    "https://www.census.gov/economic-indicators/indicator.xml": 7 * 24,
    "https://www.cftc.gov/RSS/RSSGP/rssgp.xml": 7 * 24,
    "https://www.ecb.europa.eu/rss/press.html": 7 * 24,
    "https://www.federalreserve.gov/feeds/press_all.xml": 7 * 24,
    "https://www.sec.gov/news/pressreleases.rss": 7 * 24,
    "https://www.nasdaq.com/feed/nasdaq-original/rss.xml": 7 * 24,
}
# A general-purpose agency feed is kept only for items on a market topic.
_FEED_INCLUDE = {
    "https://www.justice.gov/news/rss?type=press_release&m=1": re.compile(
        r"(?i)\b(antitrust|merger|acquisition|monopol\w+|price[- ]fixing|cartel|sherman act|clayton act|"
        r"securities fraud|insider trading|accounting fraud|export control\w*|sanctions?|"
        r"foreign corrupt practices|FCPA|bank|crypto\w*|trade secrets?|tariff\w*|"
        r"(?:Inc|Corp|Corporation|Company|Co|LLC|Ltd|plc)\b\.? (?:agrees|to pay|pleads|settles|charged|indicted))\b"),
}

# personal-finance / lifestyle noise filter (same spirit as mason_app._NOISE_WORDS)
_NOISE_WORDS = (
    "grandmother", "inheritance", "annuity", "summer job", "gpa", "college student",
    "co-owner", "siblings", "retire early", "social security", "divorce", "estate",
    "wedding", "vacation", "credit card", "mortgage my", "buy a home",
    "salary negotiat", "job offer", "personal loan", "horoscope", "recipe",
)

_HEADERS = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"},
]

_TIER_LIMIT = {1: 15, 2: 8, 3: 5}   # items to consider per feed, by tier

# ── Step 3 settings (mason.config.json → "news"; read on each call; every switch ON
# when the key is absent). Listed with plain descriptions in the build report. ──
_NEWS_DEFAULTS = {
    "market_class_enabled": True,        # tagged tickers outside the book are 'market', not 'watchlist'
    "dedup": {"window_hours": 6, "by_ticker": True, "merge_cross_outlet": True,
              "threshold": 0.6, "same_event_threshold": 0.34, "max_pool": 8000},
    "scoring": {"priority_enabled": True, "batch_size": 10, "max_per_pass": 60,
                "pass_budget_seconds": 75, "dedicated_thread": True, "idle_sleep_seconds": 20,
                "min_gap_seconds": 1.0, "market_max_age_hours": 12, "lookback_hours": 72,
                "floors_enabled": True, "retry_missing": True, "max_attempts": 2,
                "deterministic_fallback": True, "fallback_after_minutes": 10,
                "write_retries": 4, "write_retry_seconds": 1.5,
                # 2026-11-17: read replies in other shapes (list, mis-copied id …), see
                # read_score_reply; false = today's reader exactly
                "tolerant_reader": True,
                # 2026-11-17: "classify" = the model picks a kind / new / dir per numbered
                # headline and code computes the numbers (see _classify_answers);
                # "classic" = today's prompt, byte for byte
                "mode": "classic", "classify_num_predict": 400, "classify_new_event_floor": 0.4,
                "classify_book_weight": {"holding": 1.0, "watchlist": 0.75, "market": 0.25},
                # review 2026-11-18 (classify mode only): a strong event on a HELD name whose
                # own words rate it >= this floor and that the model also calls a new event of
                # the same kind reaches classify_held_agree_score (the Breaking line); null = off
                "classify_held_agree_floor": 0.7, "classify_held_agree_score": 0.8,
                # smallfix 2026-11-18 (both modes): true = a headline that ENDS on a live-blog marker
                # ("… – follow live") is judged without it, see strip_live_marker; false = today
                "strip_live_marker": False},
    "feeds": {"stale_enabled": True, "stale_hours": 48, "stale_after_failures": 12,
              "stale_retry_hours": 6, "notify": True, "first_fetch_max_age_hours": 48},
    # all_filers_enabled stays OFF for the first week (owner's call, 2026-11-14); the
    # path is fully built and tested and is switched on with this one key
    "edgar": {"all_filers_enabled": False, "all_filers_excerpts_per_scan": 6, "max_pages": 3,
              "backlog_max": 300, "backlog_max_age_hours": 48, "doc_max_bytes": 250000,
              "signal_chars": 1500,
              # 2026-11-16 (feeds): SEC answered slowly for hours (11-15, 11-16: reads of 4-5 s on
              # average, many past 12 s) and the 8-K feed timed out, rested 30 min after 5 misses,
              # and stayed "down" while SEC was still answering most requests. resilient=true:
              # wait up to fetch_timeout_s for the feed; after 2, 3, 4+ misses in a row wait 3.5,
              # 7.5, then max_backoff_s seconds between tries; judge health by the last good read.
              # false = the old rules exactly (12 s, 5 misses then one try every 30 min).
              "resilient": True, "fetch_timeout_s": 25, "max_backoff_s": 600},
    "alpaca_backfill": {"enabled": True, "max_hours": 1, "max_pages": 10, "no_alert_minutes": 60},
}
_news_cfg_cache = {"mtime": None, "data": {}}
_NEWS_OVERRIDES: dict = {}      # tests / replays only: {"section": {...}} laid over the config file


def _ncfg(section: str = None, key: str = None, default=None):
    """news.<section>.<key> from mason.config.json over _NEWS_DEFAULTS. With only
    `section`: the merged section dict (or the scalar). Never raises."""
    try:
        p = BASE / "mason.config.json"
        mt = p.stat().st_mtime
        if _news_cfg_cache["mtime"] != mt:
            _news_cfg_cache["data"] = (json.loads(p.read_text(encoding="utf-8")) or {}).get("news") or {}
            _news_cfg_cache["mtime"] = mt
        user = _news_cfg_cache["data"] if isinstance(_news_cfg_cache["data"], dict) else {}
    except Exception:
        user = {}
    if _NEWS_OVERRIDES:
        user = dict(user)
        for k, v in _NEWS_OVERRIDES.items():
            if isinstance(v, dict) and isinstance(user.get(k), dict):
                user[k] = {**user[k], **v}
            else:
                user[k] = v
    if section is None:
        return user
    base = _NEWS_DEFAULTS.get(section)
    got = user.get(section)
    if isinstance(base, dict):
        merged = dict(base)
        if isinstance(got, dict):
            merged.update({k: v for k, v in got.items() if v is not None})
        if key is None:
            return merged
        return merged.get(key, default)
    val = base if got is None else got
    return default if val is None else val
_FAIL_REST_AFTER = 5                 # consecutive failures before a feed rests
_FAIL_REST_SECONDS = 1800            # ...and how long it rests before a probe

# ── Phase 5: GDELT global events + Telegram aggregators ───────────────────────
# GDELT DOC 2.0 — worldwide event coverage; rate-limited to ~1 req/5s, so we poll
# it at most every _GDELT_EVERY seconds and degrade silently on 429/empty.
_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_GDELT_QUERY = ('("stock market" OR "Federal Reserve" OR "interest rates" OR '
                'tariffs OR recession OR "central bank" OR OPEC OR sanctions OR '
                '"jobs report" OR inflation) sourcelang:eng')
_GDELT_EVERY = 540                   # seconds between GDELT pulls (~9 min)
_gdelt_last = 0.0                    # epoch of the last SUCCESSFUL pull (read by mason_health.check_gdelt)
_gdelt_last_try = 0.0                # epoch of the last attempt (rate-limit pacing only)

# Telegram public web-preview channels (no auth): (url slug, display name, tier).
_TG_CHANNELS = [
    ("WatcherGuru",   "WatcherGuru",   2),
    ("financialjuice", "FinancialJuice", 2),
]

# ── Phase 6a: SEC EDGAR 8-K poller ────────────────────────────────────────────
# An 8-K is the legally-required disclosure of a material corporate event and
# usually hits EDGAR *before or alongside* the press release — minutes to hours
# before CNBC/WSJ rewrite it. It is the earliest primary source we can poll for
# deals (Item 1.01), M&A closings (2.01), and "other events" (8.01 — where trial
# readouts and FDA news land). See NEWS_SERVICE_DESIGN.md §Phase 6.
_EDGAR_ATOM = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               "&type=8-K&count=100&output=atom")
_EDGAR_SOURCE = "SEC 8-K"
# Items that carry tradeable information. Deliberately excludes the routine
# ones (5.02 officer comp, 5.03 bylaws, 5.07 shareholder votes, 9.01 exhibits)
# which are ~half of all 8-Ks and almost never move a stock on their own.
_EDGAR_SIGNAL_ITEMS = {
    "1.01": "Material Definitive Agreement",
    "1.02": "Termination of Material Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Acquisition or Disposition Completed",
    "2.02": "Results of Operations",
    "2.04": "Triggering Event / Acceleration of Obligation",
    "2.06": "Material Impairment",
    "3.01": "Delisting or Listing-Standard Notice",
    "4.02": "Non-Reliance on Prior Financials",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
}
# Step 3 (audit R8): 8-K ingestion used to stop at filers in the book / watchlist
# (8 events in a week). For EVERY listed filer these items are now read:
#   kept on the item code alone      1.03 bankruptcy · 4.01 auditor change ·
#                                    4.02 non-reliance · 2.02 results
#   kept only when the filing TEXT shows a real event (first lines of the item +
#   exhibit titles, via mason_triage's recognisers)
#                                    5.02 officer change (CEO / CFO / chair only) ·
#                                    1.01 / 1.02 agreements (a deal or a big contract,
#                                    not a credit facility) · 7.01 / 8.01 (guidance,
#                                    pre-announcement, or another strong event)
# The text check costs two SEC requests per filing, so it is capped per scan
# (news.edgar.all_filers_excerpts_per_scan) and spent on the most important items
# first. All requests go through mason_edgar._get (declared User-Agent, ≥0.12 s
# apart → well under 10/s; a scan makes at most ~1 + 2×(3+6) requests).
_EDGAR_ALL_DIRECT = {"1.03": "distress", "4.02": "accounting", "4.01": "accounting", "2.02": "earnings"}
_EDGAR_ALL_TEXT = ("5.02", "1.02", "1.01", "7.01", "8.01")
_EDGAR_ALL_LABELS = {"5.02": "Officer or Director Change", "4.01": "Change of Auditor"}
_EDGAR_EXCERPT_MAX = 3        # primary-doc fetches per scan (2 SEC requests each)
_EDGAR_EXCERPT_CHARS = 220    # of item body text folded into the headline
_EDGAR_CIK_TTL = 3600         # re-invert the SEC ticker map at most hourly
_cik_cache = {"ts": 0.0, "map": {}}


def _edgar_watch() -> set:
    """Extra tickers to track on the 8-K feed beyond the book/watchlist — names
    Nico wants to catch *early* without holding them (MRNA, HOOD, biotech
    catalysts…). `news.edgar_watch: ["MRNA", ...]` in mason.config.json.
    Read per scan so edits take effect without a restart. set() on any error."""
    try:
        cfg = json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
        raw = (cfg.get("news", {}) or {}).get("edgar_watch", []) or []
        return {str(t).strip().upper() for t in raw if str(t).strip()}
    except Exception:
        return set()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip an XML namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")


def _decode_entities(s: str) -> str:
    """Fully decode HTML entities so feed text renders as real characters:
    &amp; → &, &#39; → ', &quot; → ", &lt; → <, etc. Decodes a second time to
    catch double-encoded feeds (&amp;#39;), but only while an entity remains, so
    ordinary text is never mangled. Safe on None/empty."""
    if not s:
        return s or ""
    out = _html.unescape(s)
    if "&" in out and _ENTITY_RE.search(out):
        out = _html.unescape(out)
    return out.replace("\xa0", " ")   # normalize &nbsp; to a plain space


def _text(el):
    if el is None:
        return ""
    t = (el.text or "").strip().replace("<![CDATA[", "").replace("]]>", "")
    return _decode_entities(t).strip()


def _parse_pubdate(s: str):
    """RSS RFC822 or Atom ISO8601 -> epoch float, or None."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)          # handles 'Mon, 09 Sep 2026 14:30:00 GMT'
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.strip()[:25], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _parse_feed(content: bytes, limit: int) -> list[dict]:
    """Namespace-agnostic parse of RSS <item> and Atom <entry>. Returns
    [{title, url, pub_epoch}] newest-first as the feed presents them."""
    out = []
    try:
        root = ET.fromstring(content)
    except Exception:
        return out
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = url = pub = desc = ""
        for child in list(node):
            name = _local(child.tag)
            if name == "title" and not title:
                title = _text(child)
            elif name == "link":
                # RSS: link text. Atom: href attribute (prefer rel=alternate).
                href = child.get("href")
                if href:
                    if not url or child.get("rel") in (None, "alternate"):
                        url = href.strip()
                elif not url:
                    url = _text(child) or (child.tail or "").strip()
            elif name in ("pubdate", "published", "updated", "date") and not pub:
                pub = _text(child)
            elif name in ("description", "summary", "content", "encoded") and not desc:
                # the feed's own one-liner/blurb (RSS <description>, Atom <summary>/
                # <content>, content:encoded) — a free "what is this" per article.
                desc = _clean_desc(_text(child))
        if title and len(title) > 10:
            item = {"title": title[:200], "url": url, "pub_epoch": _parse_pubdate(pub)}
            if desc and desc.lower() != title.strip().lower():
                item["desc"] = desc
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _clean_desc(s: str) -> str:
    """A feed description/summary → clean plain text: entity-decode, strip HTML,
    collapse whitespace, drop boilerplate, cap length. '' if nothing useful."""
    if not s:
        return ""
    t = _html.unescape(str(s))
    t = re.sub(r"<[^>]+>", " ", t)                 # strip any HTML markup
    t = re.sub(r"https?://\S+", "", t)             # drop trailing 'read more' urls
    t = " ".join(t.split())
    low = t.lower()
    if len(t) < 20 or low.startswith(("read more", "continue reading", "the post ",
                                      "appeared first")):
        return ""
    return t[:300].rstrip()


def _is_market_relevant(title: str) -> bool:
    low = title.lower()
    return not any(w in low for w in _NOISE_WORDS)


def _iid(headline: str) -> str:
    norm = " ".join(headline.lower().split())[:120]
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]


# ── near-duplicate detection (token-Jaccard, no embeddings — Phase 3) ──────────
# Catches the same story reworded across outlets ("Fed holds rates" x5) that the
# exact-headline PK can't. Cheap set overlap; no model, no new deps.
_STOP = set(
    "the a an and or but of to in on for with at by from as is are was were be been "
    "has have had will would could should may might can do does did after before over "
    "into amid up down out off new news say says said report reports reported its his "
    "her their our your this that these those it he she they we you i us not no more "
    "than then now amid vs about above below near more most less least per".split()
)


def _tokens(headline: str) -> set:
    import re
    toks = re.findall(r"[a-z0-9]{3,}", (headline or "").lower())
    return {t for t in toks if t not in _STOP}


def _near_dup(new_toks: set, existing_sets, threshold: float = 0.6) -> bool:
    if len(new_toks) < 4:
        return False
    for s in existing_sets:
        if len(s) < 4:
            continue
        inter = len(new_toks & s)
        if not inter:
            continue
        if inter / len(new_toks | s) >= threshold:
            return True
    return False


def _last_price(ticker: str):
    """Latest price for a ticker via yfinance (same lib/access the follow-poller
    uses — FastInfo needs bracket/attr access, not .get). Best-effort; None on
    any failure."""
    try:
        import yfinance as yf
        import mason_symbols
        fi = yf.Ticker(mason_symbols.yf_symbol(ticker)).fast_info     # BRK.A → BRK-A (R2-A)
        try:
            px = fi["last_price"]
        except Exception:
            px = getattr(fi, "last_price", None)
        return float(px) if px else None
    except Exception:
        return None


def _recent_token_sets(conn, hours: int = 6, limit: int = 150) -> list:
    cutoff = time.time() - hours * 3600
    try:
        rows = conn.execute(
            "SELECT headline FROM news_events WHERE ingested_at > ? "
            "ORDER BY ingested_at DESC LIMIT ?", (cutoff, limit)).fetchall()
        return [_tokens(r["headline"]) for r in rows]
    except Exception:
        return []


_fetch_errors: dict = {}          # feed url -> short reason of its LAST failed fetch


# ── Step 3 (information audit R8): time-based, ticker-keyed near-duplicate pool ──
# The old pool was "the last 150 headlines" while a burst brings 500 in six minutes,
# so copies slipped through; and it compared every headline with every other, so
# templated lines for DIFFERENT companies ("X Maintains Overweight on A" / "... on B")
# could be dropped as duplicates. Now:
#   * the pool is every headline of the last news.dedup.window_hours (default 6);
#   * a headline is compared only with headlines that share a ticker with it (or,
#     when it names no ticker, with other ticker-less headlines — and there two
#     lines that differ in a proper name or a number are NOT the same story);
#   * a copy from ANOTHER outlet is not stored again: it is recorded on the first
#     event as one more source (raw.also / raw.n_sources) — the source count is a
#     signal triage uses; a copy from the SAME outlet is simply skipped.
_NUM_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)*")   # not the digit of a label (Q3, FY26, H200)
_dedup_stats = {"merged": 0, "skipped": 0}


def _cap_tokens(headline: str) -> set:
    """Lower-cased tokens that are written Capitalised / UPPER in the headline and
    are not its first word — the proper names that tell two templated lines apart."""
    out = set()
    try:
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’&.\-]*", headline or "")
        for w in words[1:]:
            if w[:1].isupper():
                for t in re.findall(r"[a-z0-9]{3,}", w.lower()):
                    if t not in _STOP:
                        out.add(t)
    except Exception:
        pass
    return out


def _first_tokens(headline: str) -> set:
    """Lower-cased tokens of a Capitalised FIRST word — a company name when two
    templated lines differ there ("Carriage Q3 Earnings Preview" / "Verisk Q3 …")."""
    try:
        m = re.match(r"\s*(?:The\s+)?([A-Z0-9][A-Za-z0-9'’&.\-]*)", headline or "")
        if not m:
            return set()
        return {t for t in re.findall(r"[a-z0-9]{3,}", m.group(1).lower()) if t not in _STOP}
    except Exception:
        return set()


# Opposite events must never be folded into one ("cuts guidance" / "raises guidance",
# "FDA approves" / "FDA rejects", a report and its denial): each pair below is two
# directions of one act. Two headlines conflict when, for some pair, one carries only
# the first direction and the other only the second — or when exactly one of them
# negates / denies.
_POL_PAIRS = [
    (r"rais\w+|lift(?:s|ed|ing)?|boost\w*|hik\w+|increas\w+|upp?ed|ups",
     r"cuts?|cutting|lower(?:s|ed|ing)|slash\w*|reduc\w+|trim\w*|withdr\w+|suspend\w*"),
    (r"upgrad\w+", r"downgrad\w+"),
    (r"beats?|tops?|topped|exceed\w*", r"miss(?:es|ed)?|falls? short|fell short"),
    (r"approv\w+|clear(?:s|ed|ance)|authoriz\w+|accept(?:s|ed)?|grants?|granted",
     r"reject\w*|refus\w+|declin(?:es|ed) to (?:approve|file)|complete response|crl"),
    (r"wins?|won|awarded|secures?|secured", r"loses?|lost|cancel\w*|terminat\w+"),
    (r"rises?|rose|rising|jumps?|jumped|surg\w+|soar\w*|gains?|gained|climb\w*|rall\w+|higher",
     r"falls?|fell|falling|drops?|dropped|plung\w+|slid\w*|sinks?|sank|tumbl\w+|slump\w*|lower"),
    (r"buys?|bought|acquir\w+", r"sells?|sold|divest\w*"),
    (r"hir(?:es|ed|ing)|appoint\w+|names?|named", r"fir(?:es|ed)|oust\w*|resign\w*|steps? down"),
    (r"bullish|profit", r"bearish|loss"),
    (r"begins?|began|starts?|started|resum\w+|reopen\w*", r"ends?|ended|halts?|halted|stops?|stopped"),
    (r"agrees?|agreed|accepts?|accepted", r"rejects?|rejected|rebuff\w*|spurn\w*"),
]
_POL_RX = [(re.compile(rf"\b(?:{a})\b", re.I), re.compile(rf"\b(?:{b})\b", re.I)) for a, b in _POL_PAIRS]
_NEG_RX = re.compile(r"\b(?:not|no|never|den(?:y|ies|ied|ial)|refut\w+|false|untrue|dismiss(?:es|ed)|"
                     r"rules? out|ruled out|won't|will not|isn't|doesn't|didn't)\b|n['’]t\b", re.I)


def _polarity(headline: str) -> tuple:
    """(per-pair direction: +1 / -1 / 0, negated?) for one headline."""
    h = headline or ""
    dirs = []
    for up, dn in _POL_RX:
        u, d = bool(up.search(h)), bool(dn.search(h))
        dirs.append(1 if (u and not d) else (-1 if (d and not u) else 0))
    return tuple(dirs), bool(_NEG_RX.search(h))


def _polarity_conflict(a: tuple, b: tuple) -> bool:
    try:
        if a[1] != b[1]:
            return True
        return any(x * y < 0 for x, y in zip(a[0], b[0]))
    except Exception:
        return False


def _nums(headline: str) -> set:
    """The figures in a headline (a bare year is a label, not a figure)."""
    return {n for n in _NUM_RE.findall(headline or "") if not re.fullmatch(r"(?:19|20)\d\d", n)}


class _DedupPool:
    """Recent headlines for near-duplicate detection. Entries:
    {id, ts, toks, tks (frozenset), source, caps, nums, strong}."""

    def __init__(self, window_s: float = 6 * 3600, threshold: float = 0.6,
                 event_threshold: float = 0.34, by_ticker: bool = True):
        self.window_s, self.threshold = float(window_s), float(threshold)
        self.event_threshold, self.by_ticker = float(event_threshold), bool(by_ticker)
        self.all, self.by_tk, self.no_tk = [], {}, []
        self.built = time.time()

    @staticmethod
    def _strong(headline: str):
        try:
            import mason_triage
            if not mason_triage.triage_enabled():
                return None
            b = mason_triage.event_bounds(headline, has_ticker=True)
            return b["kind"] if b.get("strong") else None
        except Exception:
            return None

    def add(self, eid, headline, tickers, source, ts=None, toks=None, strong=False):
        e = {"id": eid, "ts": ts or time.time(), "toks": toks if toks is not None else _tokens(headline),
             "tks": frozenset(t.upper() for t in (tickers or []) if t), "source": source or "",
             "caps": _cap_tokens(headline) | _first_tokens(headline), "nums": _nums(headline), "h": headline,
             "pol": _polarity(headline),
             "strong": strong if strong is not False else None}
        self.all.append(e)
        if e["tks"]:
            for t in e["tks"]:
                self.by_tk.setdefault(t, []).append(e)
        else:
            self.no_tk.append(e)
        return e

    def append(self, toks):          # legacy list protocol (callers that only have token sets)
        self.add(None, "", [], "", toks=toks)

    def _candidates(self, tks, cut: float):
        """Pool entries worth comparing, newest first, none older than `cut`.
        (Entries are appended in time order, so each list is walked backwards and
        left at the first one that is too old — a long-lived pool stays cheap.)"""
        if not self.by_ticker:
            lists = [self.all]
        elif not tks:
            lists = [self.no_tk]
        else:
            lists = [self.by_tk.get(t, ()) for t in tks]
        seen = set()
        for lst in lists:
            for e in reversed(lst):
                if e["ts"] < cut:
                    break
                if id(e) in seen:
                    continue
                seen.add(id(e))
                yield e

    def find(self, headline, toks, tickers, now: float = None, source: str = None):
        """The pool entry this headline duplicates, or None. (`now` exists for replays.)
        Two lines from the SAME outlet are one item only when they do not differ in a
        proper name or a number on both sides ("Needham … to $45" / "Canaccord … to
        $40" on one company are two analyst actions, not a repeat); a CORRECTION that
        repeats the same names and figures still collapses."""
        if len(toks) < 4:
            return None
        tks = frozenset(t.upper() for t in (tickers or []) if t)
        cut = (now if now is not None else time.time()) - self.window_s
        caps = nums = None
        pol = _polarity(headline)
        strong = False                      # False = not computed yet
        for e in self._candidates(tks, cut):
            if len(e["toks"]) < 4:
                continue
            inter = len(toks & e["toks"])
            if not inter:
                continue
            j = inter / len(toks | e["toks"])
            if j >= self.event_threshold and _polarity_conflict(pol, e.get("pol") or ((), False)):
                continue                    # opposite events / a denial: never the same item
            if j >= self.threshold:
                if (not tks or (source and e["source"] == source)) and self.by_ticker:
                    # no ticker to key on, or the same outlet again: two lines that differ
                    # in a proper name or a number on BOTH sides are different items
                    if caps is None:
                        caps, nums = _cap_tokens(headline) | _first_tokens(headline), _nums(headline)
                    da, db = toks - e["toks"], e["toks"] - toks
                    if (da & caps) and (db & e["caps"]):
                        continue
                    # figures: two lines that share fewer than half of their numbers are two
                    # items ("EPS $7.54 … beats by $20M" / "EPS $0.28 … beats by $20M")
                    if nums and e["nums"] and 2 * len(nums & e["nums"]) < len(nums | e["nums"]):
                        continue
                return e
            if tks and self.by_ticker and j >= self.event_threshold:
                # same company, same STRONG event (CEO resigns ...) told in other words
                if strong is False:
                    strong = self._strong(headline)
                if not strong:
                    continue
                if e["strong"] is None and e.get("h"):
                    e["strong"] = self._strong(e["h"]) or ""
                if e["strong"] == strong:
                    if nums is None:
                        caps, nums = _cap_tokens(headline) | _first_tokens(headline), _nums(headline)
                    if nums and e["nums"] and not (nums & e["nums"]):
                        continue
                    return e
        return None


def _recent_pool(conn, hours: float = None) -> "_DedupPool":
    """Build the near-duplicate pool from the last `hours` (news.dedup.window_hours)."""
    dc = _ncfg("dedup")
    hours = float(hours if hours is not None else dc.get("window_hours", 6))
    pool = _DedupPool(hours * 3600, dc.get("threshold", 0.6), dc.get("same_event_threshold", 0.34),
                      dc.get("by_ticker", True))
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT id, headline, tickers, resolved_tickers, source, ingested_at "
            "FROM news_events WHERE ingested_at > ? ORDER BY ingested_at DESC LIMIT ?) "
            "ORDER BY ingested_at ASC",
            (time.time() - hours * 3600, int(dc.get("max_pool", 8000)))).fetchall()
        for r in rows:
            tks = set()
            for col in ("tickers", "resolved_tickers"):
                try:
                    tks.update(json.loads(r[col]) or [])
                except Exception:
                    pass
            pool.add(r["id"], r["headline"] or "", tks, r["source"], r["ingested_at"])
    except Exception:
        pass
    return pool


def _merge_copy(conn, entry: dict, item: dict, source: str) -> bool:
    """Record a cross-outlet copy on the event it duplicates: raw.also gets
    {source, url, ts, h} (≤12 kept), raw.n_sources the number of distinct outlets.
    triaged=0 makes triage recount the story's sources. True when recorded."""
    try:
        eid = entry.get("id")
        if not eid or not source or source == entry.get("source"):
            return False
        row = conn.execute("SELECT raw FROM news_events WHERE id=?", (eid,)).fetchone()
        if row is None:
            return False
        try:
            raw = json.loads(row["raw"]) if row["raw"] else {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        also = [a for a in (raw.get("also") or []) if isinstance(a, dict)]
        if any(a.get("source") == source for a in also):
            return False
        if len(also) < 12:
            also.append({"source": source, "url": item.get("url", ""), "ts": time.time(),
                         "h": (item.get("title") or "")[:160]})
        raw["also"] = also
        raw["n_sources"] = 1 + len({a.get("source") for a in also})
        conn.execute("UPDATE news_events SET raw=?, triaged=0 WHERE id=?", (json.dumps(raw), eid))
        return True
    except Exception:
        return False


def _with_agent_trades(conn, universe, holds) -> tuple:
    """(universe, holds) with the tickers of Mason's own OPEN trades, and of the open
    positions synced from the brokerage (personal_positions), added to both — they
    are as much "the book" as a row in the positions file."""
    try:
        rows = conn.execute("SELECT DISTINCT ticker FROM agent_trades WHERE status='open' "
                            "AND COALESCE(record_deleted,0)=0 AND ticker IS NOT NULL").fetchall()
        mine = {str(r[0]).upper() for r in rows if r[0]}
    except Exception:
        mine = set()
    try:                               # positions synced from the brokerage, when that table is in use
        rows = conn.execute("SELECT DISTINCT ticker FROM personal_positions WHERE status='open' "
                            "AND COALESCE(is_deleted,0)=0 AND ticker IS NOT NULL").fetchall()
        mine |= {str(r[0]).upper() for r in rows if r[0]}
    except Exception:
        pass
    if mine:
        return set(universe) | mine, set(holds) | mine
    return universe, holds


def _fetch(url: str, timeout: int = 8) -> tuple[list[dict], bool]:
    """Fetch + parse one feed with one quick retry. Returns (items, ok). A failure
    is no longer silent: its reason is kept in _fetch_errors[url] (cleared on the
    next success) for the log line and the news_sources row."""
    limit = 20
    why = "no response"
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers=random.choice(_HEADERS))
            if r.status_code == 200 and r.content:
                _fetch_errors.pop(url, None)
                return _parse_feed(r.content, limit), True
            why = f"HTTP {r.status_code}" if r.status_code != 200 else "empty body"
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(0.6)
                continue
            break
        except Exception as e:
            why = type(e).__name__
            if attempt == 0:
                time.sleep(0.5)
                continue
            break
    _fetch_errors[url] = why
    return [], False


# ── Phase 5 fetchers: GDELT + Telegram ─────────────────────────────────────────

def _parse_gdelt_date(s: str):
    """GDELT seendate 'YYYYMMDDTHHMMSSZ' -> epoch, or None."""
    try:
        return datetime.strptime(s.strip(), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _fetch_gdelt(maxrecords: int = 25) -> list[dict]:
    """Recent global finance/geopolitical articles from GDELT DOC 2.0. Defensive:
    a 429 / rate-limit notice / empty body all yield []. Never raises."""
    try:
        r = requests.get(_GDELT_URL, params={
            "query": _GDELT_QUERY, "mode": "ArtList", "maxrecords": maxrecords,
            "sort": "DateDesc", "format": "json", "timespan": "1d",
        }, timeout=12, headers=random.choice(_HEADERS))
        if r.status_code != 200:
            return []
        txt = (r.text or "").lstrip()
        if not txt.startswith("{"):        # rate-limit notice / empty / HTML
            return []
        data = json.loads(txt)
        out = []
        for a in data.get("articles", []):
            title = _decode_entities((a.get("title") or "").strip())
            if not title:
                continue
            out.append({"title": title[:200], "url": a.get("url", ""),
                        "pub_epoch": _parse_gdelt_date(a.get("seendate", ""))})
        return out
    except Exception:
        return []


def _fetch_telegram(slug: str, limit: int = 20) -> list[dict]:
    """Latest posts from a public Telegram channel's t.me/s web preview (no auth).
    Returns [{title, url(permalink), pub_epoch}]. Never raises."""
    try:
        r = requests.get(f"https://t.me/s/{slug}", timeout=10,
                         headers=random.choice(_HEADERS))
        if r.status_code != 200 or not r.text:
            return []
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for msg in soup.select(".tgme_widget_message"):
            textel = msg.select_one(".tgme_widget_message_text")
            if not textel:
                continue
            text = textel.get_text(" ", strip=True)
            if not text or len(text) < 15:
                continue
            timeel = msg.select_one("time")
            pub = _parse_pubdate(timeel.get("datetime", "")) if timeel else None
            datea = msg.select_one("a.tgme_widget_message_date")
            link = datea["href"] if (datea and datea.get("href")) else f"https://t.me/{slug}"
            out.append({"title": text[:200], "url": link, "pub_epoch": pub})
        return out[-limit:]                # preview is oldest→newest; keep the tail
    except Exception:
        return []


# ── Phase 6a fetchers: SEC EDGAR 8-K ──────────────────────────────────────────

def _parse_edgar_atom(content: bytes) -> list[dict]:
    """Parse EDGAR's 'current filings' Atom feed. Each entry ->
    {form, company, cik, accession, filed, items: [(code, title)], url, pub_epoch}.
    Namespace-agnostic; never raises (bad feed -> [])."""
    import re
    import html as _html
    out = []
    try:
        root = ET.fromstring(content)
    except Exception:
        return out
    for el in root.iter():
        if _local(el.tag) != "entry":
            continue
        try:
            title = url = summary = updated = ""
            for ch in el:
                t = _local(ch.tag)
                if t == "title":
                    title = _text(ch)
                elif t == "link" and (ch.get("rel") in (None, "alternate")):
                    url = ch.get("href") or url
                elif t == "summary":
                    summary = _text(ch)
                elif t == "updated":
                    updated = _text(ch)
            # title: "8-K - Company Name (0001234567) (Filer)"
            m = re.match(r"\s*(8-K(?:/A)?)\s*-\s*(.*?)\s*\((\d{10})\)", title)
            if not m:
                continue
            form, company, cik = m.group(1), m.group(2), int(m.group(3))
            text = _html.unescape(summary or "")
            text = re.sub(r"<[^>]+>", " ", text)
            acc = re.search(r"AccNo:\s*([\d-]{20})", text)
            filed = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", text)
            items = [(c, ti.strip()) for c, ti in
                     re.findall(r"Item\s+(\d+\.\d+):\s*([^\n]*?)(?=\s+Item\s+\d+\.\d+:|\s*$)",
                                " ".join(text.split()))]
            out.append({
                "form": form, "company": company, "cik": cik,
                "accession": acc.group(1) if acc else "",
                "filed": filed.group(1) if filed else "",
                "items": items, "url": url,
                "pub_epoch": _parse_pubdate(updated),
            })
        except Exception:
            continue
    return out


def _fetch_edgar_8k() -> tuple[list[dict], bool]:
    """Latest 8-K filings via mason_edgar's SEC-compliant client. (entries, ok)."""
    try:
        import mason_edgar
        t0 = time.time()
        r = mason_edgar._get(_EDGAR_ATOM, timeout=_edgar_fetch_timeout())
        _edgar_note_slow(time.time() - t0, r is not None and bool(r.content))
        if r is None or not r.content:
            _fetch_errors[_EDGAR_ATOM] = ("empty body" if r is not None else
                                          getattr(mason_edgar, "last_error", {}).get(_EDGAR_ATOM) or "no response")
            return [], False
        entries = _parse_edgar_atom(r.content)
        _fetch_errors.pop(_EDGAR_ATOM, None)
        return entries, True
    except Exception as e:
        _fetch_errors[_EDGAR_ATOM] = f"{type(e).__name__} while reading the feed"
        return [], False


def _edgar_resilient() -> bool:
    """news.edgar.resilient (default true): the 2026-11-16 SEC 8-K timeout / back-off / health rules."""
    return _ncfg("edgar", "resilient", True) is not False


def _edgar_fetch_timeout() -> float:
    """Seconds to wait for the 8-K feed: news.edgar.fetch_timeout_s (default 25, kept within 5-60);
    12 (the old value) when news.edgar.resilient is false."""
    if not _edgar_resilient():
        return 12
    try:
        return max(5.0, min(60.0, float(_ncfg("edgar", "fetch_timeout_s", 25) or 25)))
    except Exception:
        return 25.0


_edgar_slow = {"saved": 0, "last_log": 0.0}   # good 8-K reads that took longer than the old 12 s limit


def _edgar_note_slow(secs: float, ok: bool) -> None:
    """A GOOD 8-K read that took more than 12 s (the old limit, which would have dropped it) is
    counted and logged at most once per 30 min, so the effect of the longer wait shows in the
    journal. Never raises."""
    try:
        if not ok or secs <= 12:
            return
        _edgar_slow["saved"] += 1
        if time.time() - _edgar_slow["last_log"] >= 1800:
            _edgar_slow["last_log"] = time.time()
            log.info(f"[news-scan] SEC 8-K feed answered in {secs:.1f} s (the old 12 s limit would have "
                     f"dropped it; {_edgar_slow['saved']} such reads since start)")
    except Exception:
        pass


_EDGAR_BACKOFF_S = (0, 210, 450)     # wait after the 1st, 2nd, 3rd miss in a row (the scan runs every 120 s,
                                     # so one miss is simply tried again on the next pass, as before)


def _edgar_resting(row) -> bool:
    """Back-off for the SEC 8-K feed after failed fetches: after one miss the next pass tries again
    (as before); after 2 and 3 misses in a row wait 3.5 and 7.5 minutes, then
    news.edgar.max_backoff_s (default 600 s = 10 min, kept within 120-3600) between tries,
    for as long as it keeps failing. One good fetch resets the count. Gentler on SEC than the
    generic rule (5 tries in 10 minutes) and quicker to come back (the generic rule then tried
    once every 30 minutes, so one more slow answer kept the feed off for another half hour).
    news.edgar.resilient=false: the generic _resting rule, unchanged."""
    if not _edgar_resilient():
        return _resting(row)
    if not row:
        return False
    fs = int(row["fail_streak"] or 0)
    if fs <= 0:
        return False
    try:
        cap = max(120.0, min(3600.0, float(_ncfg("edgar", "max_backoff_s", 600) or 600)))
    except Exception:
        cap = 600.0
    wait = min(float(_EDGAR_BACKOFF_S[fs - 1]) if fs <= len(_EDGAR_BACKOFF_S) else cap, cap)
    return (time.time() - float(row["last_attempt"] or 0)) < wait


def _cik_to_tickers() -> dict:
    """{cik_int: [TICKER, ...]} inverted from SEC's company_tickers.json (disk-
    cached weekly by mason_edgar; re-inverted here at most hourly). {} on failure."""
    now = time.time()
    if _cik_cache["map"] and (now - _cik_cache["ts"]) < _EDGAR_CIK_TTL:
        return _cik_cache["map"]
    try:
        import mason_edgar
        inv: dict = {}
        for tkr, (cik, _title) in mason_edgar._load_tickers_map().items():
            inv.setdefault(int(cik), []).append(tkr)
        if inv:
            _cik_cache["map"], _cik_cache["ts"] = inv, now
        return inv
    except Exception:
        return _cik_cache["map"] or {}


def _edgar_primary_doc_url(index_url: str) -> str:
    """From a filing's -index.htm, the primary 8-K document URL ('' if not found).
    The index table lists the main doc as type '8-K' (or '8-K/A'), often behind an
    inline-XBRL viewer link '/ix?doc=/Archives/...' which we unwrap."""
    import re
    try:
        import mason_edgar
        r = mason_edgar._get(index_url, timeout=12)
        if r is None:
            return ""
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) < 4 or cells[3] not in ("8-K", "8-K/A"):
                continue
            href = re.search(r'href="([^"]+)"', row)
            if not href:
                continue
            h = href.group(1)
            if "/ix?doc=" in h:
                h = h.split("/ix?doc=", 1)[1]
            if h.startswith("/"):
                h = "https://www.sec.gov" + h
            return h
    except Exception:
        pass
    return ""


def _edgar_excerpt(index_url: str, item_codes, chars: int = _EDGAR_EXCERPT_CHARS) -> str:
    """Best-effort first sentence(s) of the first signal item's body text, so the
    headline carries substance ('entered into a collaboration with …') instead
    of just the item label. '' on any failure — the caller falls back."""
    import re
    try:
        import mason_edgar
        doc = _edgar_primary_doc_url(index_url)
        if not doc:
            return ""
        text = mason_edgar._fetch_doc_text(doc)
        if not text:
            return ""
        for code in item_codes:
            m = re.search(rf"(?im)^\s*Item\s+{re.escape(code)}\b[^\n]*\n", text)
            if not m:
                continue
            body = text[m.end():]
            # stop at the next Item heading / signature block
            stop = re.search(r"(?im)^\s*(Item\s+\d+\.\d+|SIGNATURES?)\b", body)
            if stop:
                body = body[:stop.start()]
            body = " ".join(body.split())
            if len(body) < 20:
                continue
            if len(body) > chars:
                cut = body[:chars]
                dot = cut.rfind(". ")
                body = (cut[:dot + 1] if dot > chars // 2 else cut.rstrip() + "…")
            return body
    except Exception:
        pass
    return ""


def _edgar_doc_text(url: str, max_bytes: int = None) -> str:
    """Text of one filing document, reading at most `max_bytes` of it
    (news.edgar.doc_max_bytes, default 250 kB — an 8-K's items are at its top).
    Same declared SEC User-Agent and pause as mason_edgar._get. '' on failure."""
    try:
        import mason_edgar
        cap = int(max_bytes or _ncfg("edgar", "doc_max_bytes", 250000))
        r = requests.get(url, headers={"User-Agent": mason_edgar._ua(), "Accept-Encoding": "gzip, deflate"},
                         timeout=15, stream=True)
        time.sleep(0.12)                       # SEC politeness, as in mason_edgar._get
        if not r.ok:
            r.close()
            return ""
        buf, got = [], 0
        for chunk in r.iter_content(chunk_size=16384):
            if not chunk:
                continue
            buf.append(chunk)
            got += len(chunk)
            if got >= cap:
                break
        r.close()
        raw = b"".join(buf)[:cap].decode(r.encoding or "utf-8", "replace")
        return mason_edgar.html_to_text(raw) if ("<" in raw and ">" in raw) else raw
    except Exception:
        return ""


def _strip_item_caption(body: str) -> str:
    """Drop the form's own caption from the start of an item's text. An 8-K item
    opens by repeating its official title ("Departure of Directors or Certain
    Officers; Election of Directors; … Compensatory Arrangements of Certain
    Officers.") — 150+ characters that say nothing; the event is the first real
    sentence after it."""
    try:
        m = re.match(r"\s*((?:[A-Z][A-Za-z\-]*|of|or|and|a|an|the|to|in|for|on|by|under|into|from)"
                     r"(?:[\s,;:]+(?:[A-Z][A-Za-z\-]*|of|or|and|a|an|the|to|in|for|on|by|under|into|from|FD)){2,45})"
                     r"[.;:]?\s+(?=(?:\([a-z0-9]\)\s*)?(?:On|Effective|As|In|The|Pursuant|[A-Z][a-z]+ \d|[A-Z]))", body)
        if m and len(m.group(1)) >= 15 and not re.search(r"\b(?:On|Effective) [A-Z][a-z]+ \d", m.group(1)):
            rest = body[m.end():]
            if len(rest) >= 20:
                return rest
    except Exception:
        pass
    return body


def _edgar_peek(index_url: str, item_codes, chars: int = _EDGAR_EXCERPT_CHARS) -> tuple:
    """(excerpt, exhibit_titles, signal_text) for one filing. `excerpt`: the first
    sentence(s) of the first of `item_codes` found in the primary document, caption
    skipped (≤ `chars`, for the headline). `signal_text`: up to
    news.edgar.signal_chars (1,500) of that item — enough to reach the sentence that
    says what happened, which the 220-character excerpt often did not.
    `exhibit_titles`: descriptions of the EX-99 exhibits from the filing index.
    Two SEC requests, the document read under a byte cap. ('', [], '') on failure."""
    import re
    try:
        import mason_edgar
        r = mason_edgar._get(index_url, timeout=12)
        if r is None:
            return "", [], ""
        doc, titles = "", []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) < 4:
                continue
            if cells[3].upper().startswith("EX-99") and cells[1]:
                titles.append(_decode_entities(cells[1])[:120])
            if not doc and cells[3] in ("8-K", "8-K/A"):
                href = re.search(r'href="([^"]+)"', row)
                if href:
                    h = href.group(1)
                    if "/ix?doc=" in h:
                        h = h.split("/ix?doc=", 1)[1]
                    doc = ("https://www.sec.gov" + h) if h.startswith("/") else h
        excerpt = signal = ""
        if doc:
            text = _edgar_doc_text(doc) or ""
            sig_chars = int(_ncfg("edgar", "signal_chars", 1500))
            for code in item_codes:
                m = re.search(rf"(?im)^\s*Item\s+{re.escape(code)}\b[^\n]*\n", text)
                if not m:
                    continue
                body = text[m.end():]
                stop = re.search(r"(?im)^\s*(Item\s+\d+\.\d+|SIGNATURES?)\b", body)
                if stop:
                    body = body[:stop.start()]
                body = _strip_item_caption(" ".join(body.split()))
                if len(body) < 20:
                    continue
                signal = body[:sig_chars]
                if len(body) > chars:
                    cut = body[:chars]
                    dot = cut.rfind(". ")
                    body = (cut[:dot + 1] if dot > chars // 2 else cut.rstrip() + "…")
                excerpt = body
                break
        return excerpt, titles, signal
    except Exception:
        return "", [], ""


def _edgar_text_signal(codes, excerpt: str, titles: list):
    """For an 8-K item that is routine most of the time: the event kind its text
    shows, or None (⇒ skip the filing for a filer outside the book). Uses
    mason_triage's recognisers — no separate word list here."""
    try:
        import mason_triage as T
        text = " ".join([excerpt or ""] + list(titles or [])).strip()
        if not text:
            return None
        # the filing's own sentences, one at a time (a recogniser works on a clause),
        # then the exhibit titles; the first sentence that shows an event decides
        parts = [p.strip() for p in re.split(r"(?<=[.;])\s+(?=[A-Z(])", excerpt or "") if len(p.strip()) > 15]
        parts += [t for t in (titles or []) if t]
        b, kind, floor = {}, None, 0.0
        for part in (parts or [text])[:14]:
            bb = T.event_bounds(part[:400], has_ticker=True, structured=True)
            if float(bb.get("floor") or 0) > floor and bb.get("kind") not in (None, "other"):
                b, kind, floor = bb, bb.get("kind"), float(bb.get("floor") or 0)
        if not kind:
            b = T.event_bounds(text[:400], has_ticker=True, structured=True)
            kind, floor = b.get("kind"), float(b.get("floor") or 0)
        if "5.02" in codes and kind == "mgmt_change" and floor >= 0.6:
            return kind                                   # CEO / CFO / chair, not a director or a pay plan
        if ("1.01" in codes or "1.02" in codes) and kind in ("mna", "contract_award", "contract_loss",
                                                             "distress") and floor >= 0.6:
            return kind
        if "7.01" in codes or "8.01" in codes:
            if kind in ("guidance_cut", "guidance_raise", "guidance"):
                return kind
            if kind in T.STRONG_KINDS and b.get("strong"):
                return kind
            if re.search(r"(?i)\b(preliminary|pre-?announc\w+|updat\w+ (its |the )?(guidance|outlook)|"
                         r"(financial |business )?outlook|guidance)\b", " ".join(titles or [])):
                return "guidance"
        return None
    except Exception:
        return None


def _fetch_edgar_8k_page(start: int) -> tuple[list[dict], bool]:
    """A later page of the current-8-K feed (start=100, 200 …). (entries, ok)."""
    try:
        import mason_edgar
        r = mason_edgar._get(_EDGAR_ATOM + f"&start={int(start)}", timeout=_edgar_fetch_timeout())
        if r is None or not r.content:
            return [], False
        return _parse_edgar_atom(r.content), True
    except Exception:
        return [], False


def _edgar_headline(ticker: str, entry: dict, labels: list, excerpt: str) -> str:
    what = "; ".join(labels)
    if excerpt:
        return f"{ticker} {entry['form']} · {what}: {excerpt}"[:400]
    return f"{ticker} files {entry['form']} · {what}"


_EDGAR_BACKLOG_SRC = "_edgar_backlog"


def _edgar_backlog_load(conn) -> list:
    try:
        row = conn.execute("SELECT payload_json FROM source_snapshots WHERE source=? AND key='queue' "
                           "AND record_deleted=0", (_EDGAR_BACKLOG_SRC,)).fetchone()
        q = json.loads(row[0]) if row and row[0] else []
        return [x for x in q if isinstance(x, dict) and x.get("url")]
    except Exception:
        return []


def _edgar_backlog_save(conn, queue: list) -> None:
    """Caller owns the transaction."""
    try:
        conn.execute(
            "INSERT INTO source_snapshots (source, key, taken_at, payload_json, created_date, created_by, "
            "record_deleted) VALUES (?,?,?,?,?,?,0) ON CONFLICT(source, key) DO UPDATE SET "
            "taken_at=excluded.taken_at, payload_json=excluded.payload_json, record_deleted=0",
            (_EDGAR_BACKLOG_SRC, "queue", time.time(), json.dumps(queue, separators=(",", ":")),
             datetime.now(timezone.utc).isoformat(timespec="seconds"), "news_service"))
    except Exception as e:
        log.debug(f"[news-scan] edgar backlog not saved: {e}")


def _edgar_done_load(conn, max_age: float = 48 * 3600) -> dict:
    """{accession: epoch} of other-filer 8-Ks whose text has already been read (review
    round 2). The feed keeps offering the newest filings (same second as the watermark);
    without this memory they were fetched from the SEC again on every scan."""
    try:
        row = conn.execute("SELECT payload_json FROM source_snapshots WHERE source=? AND key='done' "
                           "AND record_deleted=0", (_EDGAR_BACKLOG_SRC,)).fetchone()
        d = json.loads(row[0]) if row and row[0] else {}
        now = time.time()
        return {str(k): float(v) for k, v in d.items() if (now - float(v)) <= max_age} if isinstance(d, dict) else {}
    except Exception:
        return {}


def _edgar_done_save(conn, done: dict, cap: int = 600) -> None:
    """Caller owns the transaction. Newest `cap` kept."""
    try:
        keep = dict(sorted(done.items(), key=lambda kv: -kv[1])[:cap])
        conn.execute(
            "INSERT INTO source_snapshots (source, key, taken_at, payload_json, created_date, created_by, "
            "record_deleted) VALUES (?,?,?,?,?,?,0) ON CONFLICT(source, key) DO UPDATE SET "
            "taken_at=excluded.taken_at, payload_json=excluded.payload_json, record_deleted=0",
            (_EDGAR_BACKLOG_SRC, "done", time.time(), json.dumps(keep, separators=(",", ":")),
             datetime.now(timezone.utc).isoformat(timespec="seconds"), "news_service"))
    except Exception as e:
        log.debug(f"[news-scan] edgar read-list not saved: {e}")


def _edgar_backlog_order(q: dict, universe) -> tuple:
    """Who is read first: a filer on the book / watchlist, then by item — officer
    change, results, non-reliance, bankruptcy, agreements, and last the catch-all
    7.01 / 8.01 — newest first within each."""
    order = ("5.02", "2.02", "4.02", "1.03", "1.01", "1.02", "4.01", "7.01", "8.01")
    codes = q.get("codes") or []
    pr = min((order.index(c) for c in codes if c in order), default=len(order))
    book = 0 if any(t in universe for t in (q.get("tks") or [])) else 1
    return (book, pr, -(q.get("pub") or 0))


def _edgar_set_error(conn, why) -> None:
    """Record why the last SEC 8-K fetch failed (None = it worked: clear it, stamp last_ok). Never raises."""
    try:
        if not _ensure_source_columns(conn):
            return
        with conn:
            if why:
                conn.execute("UPDATE news_sources SET last_error=? WHERE feed_url=?", (why, _EDGAR_ATOM))
            else:
                conn.execute("UPDATE news_sources SET last_error=NULL, last_ok=? WHERE feed_url=?",
                             (time.time(), _EDGAR_ATOM))
    except Exception as e:
        log.debug(f"[news-scan] edgar error bookkeeping failed: {e}")


def _scan_edgar(conn, universe, holds) -> int:
    """One EDGAR pass: pull the current-8-K feed, keep filings that carry a signal
    item and whose filer maps to a ticker in Nico's universe — and, with
    news.edgar.all_filers_enabled, the important items of EVERY listed filer (see
    _EDGAR_ALL_DIRECT / _EDGAR_ALL_TEXT). Insert as tier-1 '{_EDGAR_SOURCE}'. Returns
    new-row count. Watermarked + rest-on-failure like an RSS feed; never raises.

    Filings of other filers that need a look at their text wait in a small persistent
    backlog (source_snapshots, survives a restart): a scan reads only
    news.edgar.all_filers_excerpts_per_scan of them, most important first, and the
    rest are read on later scans instead of being lost when the watermark moves on.
    Every SEC request is made BEFORE the write transaction opens."""
    row = _get_source_row(conn, _EDGAR_ATOM)
    if _edgar_resting(row):
        return 0
    entries, ok = _fetch_edgar_8k()
    if not ok:
        why = _fetch_errors.get(_EDGAR_ATOM) or "fetch failed"
        with conn:
            _update_source(conn, _EDGAR_ATOM, _EDGAR_SOURCE, 1, ok=False)
        _edgar_set_error(conn, why)
        fs = int((row["fail_streak"] if row else 0) or 0) + 1
        if fs in (1, _FAIL_REST_AFTER) or fs % 25 == 0:
            log.info(f"[news-scan] SEC 8-K fetch failed ({why}), streak {fs}")
        return 0
    cikmap = _cik_to_tickers()
    universe = set(universe) | _edgar_watch()     # book + watchlist + 8-K extras
    wm = (row["last_pub_date"] if row else None) or 0
    newest_pub, newest_url = wm, (row["last_url"] if row else None)
    ec = _ncfg("edgar")
    all_filers = bool(ec.get("all_filers_enabled", False))
    # a busy quarter-hour files more than one page of 8-Ks: read on until the watermark
    if all_filers and wm and entries:
        page = 1
        while page < int(ec.get("max_pages", 3)):
            pubs = [e.get("pub_epoch") for e in entries if e.get("pub_epoch")]
            if not pubs or min(pubs) <= wm:
                break
            more, ok2 = _fetch_edgar_8k_page(100 * page)
            if not ok2 or not more:
                break
            seen = {e.get("accession") for e in entries}
            entries += [e for e in more if e.get("accession") not in seen]
            page += 1
    try:
        import mason_symbols as _MS
    except Exception:
        _MS = None
    now = time.time()
    inserts = []                  # (entry, tickers, labels, excerpt, hint, extra)
    excerpts_left = _EDGAR_EXCERPT_MAX
    backlog = _edgar_backlog_load(conn) if all_filers else []
    done = _edgar_done_load(conn, float(ec.get("backlog_max_age_hours", 48)) * 3600) if all_filers else {}
    queued = {q.get("acc") for q in backlog} | set(done)      # waiting, or already read
    # ── 1. sort the feed (no database writes, SEC requests only for book filers' excerpts) ──
    for e in entries:
        pub = e.get("pub_epoch")
        if pub and pub > newest_pub:
            newest_pub, newest_url = pub, e.get("url")
        # strict '<' (not '<='): filings accepted in the same second as the
        # watermark are still considered; the accession-based id dedups them.
        if pub and wm and pub < wm:
            continue
        all_tk = cikmap.get(e["cik"], [])
        tickers = [t for t in all_tk if t in universe]
        item_codes = [c for c, _ in e["items"]]
        if not tickers:
            # ── a filer outside the book: only the items listed above ──
            if not all_filers or not all_tk:
                continue
            listed = [t for t in all_tk if "-" not in t and (_MS is None or _MS.work_ok(t))]
            if not listed:
                continue
            direct = [c for c in item_codes if c in _EDGAR_ALL_DIRECT]
            texty = [c for c in _EDGAR_ALL_TEXT if c in item_codes]
            if direct:
                labels = [_EDGAR_SIGNAL_ITEMS.get(c) or _EDGAR_ALL_LABELS.get(c, c) for c in direct]
                inserts.append((e, listed[:1], labels, "", _EDGAR_ALL_DIRECT[direct[0]], None))
            elif texty and e.get("url") and e.get("accession") not in queued:
                queued.add(e.get("accession"))
                backlog.append({"acc": e.get("accession"), "url": e.get("url"), "cik": e["cik"],
                                "company": e["company"], "form": e["form"], "items": e["items"],
                                "pub": pub, "tks": listed[:1], "codes": texty, "q_at": now})
            continue
        codes = [c for c in item_codes if c in _EDGAR_SIGNAL_ITEMS]
        if not codes:
            continue
        labels = [_EDGAR_SIGNAL_ITEMS[c] for c in codes]
        excerpt = ""
        if excerpts_left > 0 and e.get("url"):
            excerpts_left -= 1
            try:                       # best-effort; never lose the filing over it
                excerpt = _edgar_excerpt(e["url"], codes) or ""
            except Exception:
                excerpt = ""
        inserts.append((e, tickers, labels, excerpt, None, None))
    # ── 2. read a few backlog filings, most important first (two SEC requests each) ──
    checked = 0
    if all_filers and backlog:
        max_age = float(ec.get("backlog_max_age_hours", 48)) * 3600
        backlog = [q for q in backlog if (now - float(q.get("q_at") or now)) <= max_age]
        backlog.sort(key=lambda q: _edgar_backlog_order(q, universe))
        budget = max(0, int(ec.get("all_filers_excerpts_per_scan", 6)))
        take, backlog = backlog[:budget], backlog[budget:]
        for q in take:
            checked += 1
            if q.get("acc"):
                done[str(q["acc"])] = now
            try:
                got = _edgar_peek(q["url"], q.get("codes") or [])
                excerpt, titles = got[0], got[1]
                signal = got[2] if len(got) > 2 else excerpt
            except Exception:
                excerpt, titles, signal = "", [], ""
            hint = _edgar_text_signal(q.get("codes") or [], signal or excerpt, titles)
            if not hint:
                continue
            e = {"form": q.get("form") or "8-K", "company": q.get("company") or "", "cik": q.get("cik"),
                 "accession": q.get("acc"), "filed": "", "items": [tuple(x) for x in (q.get("items") or [])],
                 "url": q["url"], "pub_epoch": q.get("pub")}
            labels = [_EDGAR_SIGNAL_ITEMS.get(c) or _EDGAR_ALL_LABELS.get(c, c) for c in (q.get("codes") or [])]
            inserts.append((e, q.get("tks") or [], labels, excerpt, hint, {"exhibits": titles[:4]}))
        cap = int(ec.get("backlog_max", 300))
        if len(backlog) > cap:
            log.info(f"[news-scan] edgar backlog over {cap}: {len(backlog) - cap} least important filings let go")
            backlog = backlog[:cap]
    # ── 3. one short write ──
    new = 0
    with conn:
        for e, tks, labels, excerpt, hint, extra in inserts:
            if tks and _edgar_insert(conn, e, tks, labels, excerpt, universe, holds, hint=hint, extra=extra):
                new += 1
        if all_filers:
            _edgar_backlog_save(conn, backlog)
            if checked:
                _edgar_done_save(conn, done)
        _update_source(conn, _EDGAR_ATOM, _EDGAR_SOURCE, 1, ok=True,
                       newest_pub=newest_pub or None, newest_url=newest_url)
    _edgar_set_error(conn, None)                  # after the scan's own transaction has committed
    if all_filers and (checked or backlog):
        log.info(f"[news-scan] edgar: {checked} other-filer 8-Ks text-checked, {len(backlog)} waiting")
    return new


def _edgar_insert(conn, e, tickers, labels, excerpt, universe, holds, hint=None, extra=None) -> bool:
    """Insert one 8-K as a tier-1 event (accession-keyed id). True if new."""
    pub = e.get("pub_epoch")
    ticker = tickers[0]
    item = {"title": _edgar_headline(ticker, e, labels, excerpt),
            "url": e.get("url", ""), "pub_epoch": pub}
    eid = _iid(f"8-K {e.get('accession') or e.get('url')} {ticker}")
    raw = {"form": e["form"], "company": e["company"], "cik": e["cik"],
           "accession": e.get("accession"), "filed": e.get("filed"), "items": e["items"]}
    if hint:
        # a structured, primary-source event: triage trusts the kind (contracts §2)
        raw.update({"catalyst_source": "edgar_8k", "event_kind_hint": hint,
                    "event_key": f"edgar:8k:{e.get('accession') or e.get('url')}",
                    "structured": {"form": e["form"], "items": [c for c, _ in e["items"]]}})
    if extra:
        raw.update(extra)
    # dedup_against=None on purpose: the accession id is the dedup key.
    # Two different filers with the same item combo ("X files 8-K ·
    # Other Events") would otherwise Jaccard-match and the second would
    # be dropped. The scan's near-dup pool is left untouched.
    if _insert_event(conn, item, _EDGAR_SOURCE, 1, universe, holds,
                     explicit_tickers=tickers, dedup_against=None,
                     event_id=eid, noise_filter=False, extra_raw=raw):
        # a signal-item filing is never "calm": floor the instant dot
        # at yellow (the word-regex only knows headline vocabulary).
        conn.execute("UPDATE news_events SET impact='yellow' "
                     "WHERE id=? AND impact='green'", (eid,))
        return True
    return False


# ── source-row (watermark) bookkeeping ─────────────────────────────────────────

def _get_source_row(conn, feed_url: str):
    cur = conn.execute("SELECT * FROM news_sources WHERE feed_url = ?", (feed_url,))
    return cur.fetchone()


def _resting(row) -> bool:
    """A feed that has failed >= _FAIL_REST_AFTER times rests, and is only
    probed again once _FAIL_REST_SECONDS have passed since the last attempt."""
    if not row:
        return False
    if (row["fail_streak"] or 0) < _FAIL_REST_AFTER:
        return False
    last = row["last_attempt"] or 0
    return (time.time() - last) < _FAIL_REST_SECONDS


# ── feed health (audit R7) ────────────────────────────────────────────────────
# news_sources gains a few columns the first time a scan runs after this change
# (additive, idempotent; nothing else reads them). They record what used to be
# invisible: the last error, the age of the newest item, and whether the feed has
# been marked "gone quiet" (stale).
_SRC_COLS = (("stale", "INTEGER DEFAULT 0"), ("stale_reason", "TEXT"), ("stale_since", "REAL"),
             ("newest_item_at", "REAL"), ("last_error", "TEXT"), ("last_ok", "REAL"))
_src_cols_checked: dict = {}


def _ensure_source_columns(conn) -> bool:
    """Add the feed-health columns to news_sources when missing. True when present."""
    try:
        try:
            key = conn.execute("PRAGMA database_list").fetchone()[2] or id(conn)
        except Exception:
            key = id(conn)
        if _src_cols_checked.get(key):
            return True
        have = {r[1] for r in conn.execute("PRAGMA table_info(news_sources)")}
        if not have:
            return False
        with conn:
            for name, decl in _SRC_COLS:
                if name not in have:
                    conn.execute(f"ALTER TABLE news_sources ADD COLUMN {name} {decl}")
        _src_cols_checked[key] = True
        return True
    except Exception as e:
        log.debug(f"[news-scan] feed-health columns unavailable: {e}")
        return False


def _rv(row, name, default=None):
    try:
        v = row[name]
        return default if v is None else v
    except Exception:
        return default


def _stale_wait(row, now: float = None) -> bool:
    """A feed marked stale is only looked at again every news.feeds.stale_retry_hours."""
    if not row or not _rv(row, "stale", 0):
        return False
    try:
        if not _ncfg("feeds", "stale_enabled", True):
            return False
        gap = float(_ncfg("feeds", "stale_retry_hours", 6)) * 3600
        return ((now or time.time()) - float(row["last_attempt"] or 0)) < gap
    except Exception:
        return False


def _age_words(seconds: float) -> str:
    d = seconds / 86400.0
    if d >= 2:
        return f"{int(d)} days"
    return f"{max(1, int(seconds / 3600))} hours"


def _feed_health_after_fetch(conn, feed_url, source, ok, items, went_quiet: list, came_back: list) -> None:
    """After one fetch of one feed: record the error / newest-item age and decide
    whether the feed has gone quiet (newest item older than its limit, or too many
    failures in a row) or has come back. Appends (source, url, reason) to
    `went_quiet` / (source, url) to `came_back` on a CHANGE only, so the caller can
    send one plain notification. Caller owns the transaction. Never raises."""
    try:
        fc = _ncfg("feeds")
        if not fc.get("stale_enabled", True) or not _ensure_source_columns(conn):
            return
        row = _get_source_row(conn, feed_url)
        if row is None:
            return
        now = time.time()
        was = bool(_rv(row, "stale", 0))
        reason = None
        if ok:
            pubs = [it.get("pub_epoch") for it in (items or []) if it.get("pub_epoch")]
            newest = max(pubs) if pubs else None
            limit_h = float(_FEED_MAX_AGE_H.get(feed_url, fc.get("stale_hours", 48)))
            if newest is not None and (now - newest) > limit_h * 3600:
                reason = f"its newest item is {_age_words(now - newest)} old"
            conn.execute("UPDATE news_sources SET newest_item_at=COALESCE(?, newest_item_at), last_ok=?, "
                         "last_error=NULL WHERE feed_url=?", (newest, now, feed_url))
        else:
            why = _fetch_errors.get(feed_url) or "fetch failed"
            fs = int(row["fail_streak"] or 0)
            conn.execute("UPDATE news_sources SET last_error=? WHERE feed_url=?", (why, feed_url))
            if fs >= int(fc.get("stale_after_failures", 12)):
                reason = f"{fs} fetches in a row failed ({why})"
            elif fs in (1, 5) or fs % 25 == 0:
                log.info(f"[news-scan] feed fetch failed ({why}), streak {fs}: {source} {feed_url}")
        if reason and not was:
            conn.execute("UPDATE news_sources SET stale=1, stale_reason=?, stale_since=? WHERE feed_url=?",
                         (reason, now, feed_url))
            went_quiet.append((source, feed_url, reason))
            log.warning(f"[news-scan] feed marked stale ({reason}): {source} {feed_url}")
        elif reason and was:
            conn.execute("UPDATE news_sources SET stale_reason=? WHERE feed_url=?", (reason, feed_url))
        elif was and ok and not reason:
            conn.execute("UPDATE news_sources SET stale=0, stale_reason=NULL, stale_since=NULL "
                         "WHERE feed_url=?", (feed_url,))
            came_back.append((source, feed_url))
            log.info(f"[news-scan] feed is fresh again: {source} {feed_url}")
    except Exception as e:
        log.debug(f"[news-scan] feed-health bookkeeping failed for {feed_url}: {e}")


def _notify_feed_health(went_quiet: list, came_back: list) -> None:
    """ONE plain-words inbox note per scan for feeds that just went quiet, and one
    for feeds that just came back. Never raises."""
    try:
        if not _ncfg("feeds", "notify", True) or not (went_quiet or came_back):
            return
        import mason_notifications as MN
        retry_h = _ncfg("feeds", "stale_retry_hours", 6)
        if went_quiet:
            names = sorted({s for s, _u, _r in went_quiet})
            title = (f"A news source has gone quiet: {names[0]}" if len(names) == 1
                     else f"{len(names)} news sources have gone quiet")
            lines = [f"- {s} — {r}\n  {u}" for s, u, r in went_quiet]
            body = ("Mason is no longer getting new headlines from:\n" + "\n".join(lines) +
                    f"\n\nIt will look again every {retry_h:g} hours and tell you if one comes back. "
                    "The other news sources are not affected.")
            key = hashlib.sha1("|".join(sorted(u for _s, u, _r in went_quiet)).encode()).hexdigest()[:12]
            MN.add_notification(type="source_status", title=title, body=body, severity="warn",
                                created_by="news_service", dedup_key=f"feed_quiet:{key}")
        if came_back:
            names = sorted({s for s, _u in came_back})
            MN.add_notification(
                type="source_status",
                title=(f"A news source is back: {names[0]}" if len(names) == 1
                       else f"{len(names)} news sources are back"),
                body="New headlines are arriving again from:\n" + "\n".join(f"- {s}\n  {u}" for s, u in came_back),
                severity="info", created_by="news_service",
                dedup_key="feed_back:" + hashlib.sha1("|".join(sorted(u for _s, u in came_back)).encode()).hexdigest()[:12])
    except Exception as e:
        log.debug(f"[news-scan] feed-health notification failed: {e}")


def _update_source(conn, feed_url, source, tier, *, ok, newest_pub=None, newest_url=None):
    now = time.time()
    row = _get_source_row(conn, feed_url)
    if row is None:
        conn.execute(
            "INSERT INTO news_sources (feed_url, source, tier, last_pub_date, "
            "last_url, fail_streak, last_attempt, updated_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (feed_url, source, tier, newest_pub, newest_url,
             0 if ok else 1, now, now, "news_service"),
        )
        return
    if ok:
        lp = row["last_pub_date"] or 0
        conn.execute(
            "UPDATE news_sources SET source=?, tier=?, last_pub_date=?, last_url=?, "
            "fail_streak=0, last_attempt=?, updated_at=? WHERE feed_url=?",
            (source, tier,
             max(lp, newest_pub) if newest_pub else row["last_pub_date"],
             newest_url or row["last_url"], now, now, feed_url),
        )
    else:
        conn.execute(
            "UPDATE news_sources SET fail_streak=fail_streak+1, last_attempt=?, "
            "updated_at=? WHERE feed_url=?",
            (now, now, feed_url),
        )


# ── enrichment + insert ─────────────────────────────────────────────────────

def _category(tickers, universe, holds) -> str:
    """holding  — touches a position (mason_news.holdings_set: the journal's open option plays and trades;
                  plus Mason's own open trades and the synced brokerage positions, _with_agent_trades);
    watchlist — a ticker on the real watch universe (mason_news.watchlist_set);
    market    — names some other listed company (was mislabelled 'watchlist': 12,299
                such events in a week covered 5,301 tickers against a 25-name watchlist);
    macro     — names no company.
    news.market_class_enabled=false restores the old two-way label."""
    if any(t in holds for t in tickers):
        return "holding"
    if not tickers:
        return "macro"
    if not _ncfg("market_class_enabled", default=True) or any(t in universe for t in tickers):
        return "watchlist"
    return "market"


def _enrich(headline, universe, holds, explicit=None):
    """(tickers, category, impact) via the existing mason_news heuristics.
    `explicit` (e.g. Alpaca's `symbols`) overrides the headline regex tagging:
    the relevant ones (in Nico's universe) win, else the first few raw symbols."""
    return _enrich2(headline, universe, holds, explicit)[:3]


def _enrich2(headline, universe, holds, explicit=None, desc: str = "", tag_gate: bool = False):
    """_enrich plus a 4th value: {"resolved": tickers Mason's own name resolver reads
    in the headline, "dropped_tags": vendor tags refused by mason_symbols.sane_tags,
    "renamed_tags": vendor tags given as a company's old ticker (mason_symbols.renames)}."""
    info = {"resolved": [], "dropped_tags": [], "renamed_tags": []}
    try:
        import mason_news
        if explicit is not None:
            syms = [s.upper() for s in explicit if s]
            try:                               # a ticker from before a rename (PSKY -> SKYD)
                import mason_symbols
                _rn, _ch = mason_symbols.apply_renames(syms)
                if _ch:                        # nothing renamed: the vendor's list exactly as before
                    syms, info["renamed_tags"] = _rn, _ch
            except Exception:
                pass
            if tag_gate:
                try:                           # R10: foreign listings / unknown symbols
                    import mason_symbols
                    syms, info["dropped_tags"] = mason_symbols.sane_tags(headline, syms, book=universe, desc=desc)
                except Exception:
                    pass
            relevant = [s for s in syms if s in universe]
            tickers = sorted(set(relevant)) or syms[:3]
        else:
            tickers = mason_news._tag_tickers(headline, "", universe)
        # Catalyst Desk triage (catalyst.triage.enabled; off => unchanged): resolve
        # company names / brands / execs to tickers ("Meta's Muse..." -> META).
        # Only symbols inside the watch universe join `tickers`, so category
        # (holding/watchlist/macro) and qwen's book-scoring input stay book-scoped;
        # the full broad-universe resolution is written later by
        # mason_triage.run_triage into news_events.resolved_tickers.
        try:
            import mason_triage
            if mason_triage.triage_enabled():
                info["resolved"] = list(mason_triage.resolve_tickers(headline))
                extra = [t for t in info["resolved"]
                         if t in universe and t not in tickers]
                if extra:
                    tickers = sorted(set(tickers) | set(extra))
        except Exception:
            pass
        touches = any(t in holds for t in tickers)
        impact = mason_news._impact(headline, touches)
        return tickers, _category(tickers, universe, holds), impact, info
    except Exception:
        return (sorted({s.upper() for s in (explicit or []) if s})[:3], "macro", "yellow", info)


def _factcheck_tag_filter(headline, tickers, category, impact, holds, universe=None):
    """Phase 4 Layer 0 (behind analyst_desk.factcheck.tag_filter.enabled — default
    OFF, so this returns its inputs unchanged and is a no-op). Drops irrelevant
    auto-tags (e.g. Benzinga hanging [BAK,...] on a market-wide "Crude Oil Down 3%"
    story) and re-derives category/impact from the CLEANED tag set. Returns
    (tickers, category, impact, dropped). Never raises."""
    try:
        import mason_factcheck as FC
        if not FC.layer_enabled("tag_filter"):
            return tickers, category, impact, []
        kept, dropped = FC.filter_relevant_tickers(headline, "", tickers)
        if not dropped:
            return tickers, category, impact, []
        import mason_news
        touches = any(t in holds for t in kept)
        impact = mason_news._impact(headline, touches)
        if universe is None:
            category = "holding" if touches else ("watchlist" if kept else "macro")
        else:
            category = _category(kept, universe, holds)
        return kept, category, impact, dropped
    except Exception:
        return tickers, category, impact, []


def _insert_event(conn, item, source, tier, universe, holds,
                  explicit_tickers=None, dedup_against=None,
                  event_id=None, noise_filter=True, extra_raw=None, tag_gate=False,
                  ingested_at=None) -> bool:
    """INSERT OR IGNORE one event. Returns True if a new row was written.
    `dedup_against` (a list of token-sets) enables near-duplicate suppression:
    a headline too similar to a recent one is skipped, and accepted ones are
    appended so later items in the same pass dedup against them too.
    Phase 6a: `event_id` overrides the headline-hash id (8-K rows key on the
    accession number so a repeat 'X files 8-K · Other Events' on a later day is
    a new event, not a dup); `noise_filter=False` skips the lifestyle-word
    filter (a filing excerpt mentioning 'estate' is not personal-finance
    noise); `extra_raw` is merged into the stored raw JSON. `tag_gate=True`
    (the Benzinga stream only) runs the vendor's symbol tags through
    mason_symbols.sane_tags; tickers from SEC filings and the structured
    sources are authoritative and skip it. `ingested_at` (stream backfill only)
    stores an OLD headline under the time it was published instead of "now"."""
    headline = item["title"].strip()
    if not headline or (noise_filter and not _is_market_relevant(headline)):
        return False
    toks = _tokens(headline)
    is_pool = isinstance(dedup_against, _DedupPool)
    if dedup_against is not None and not is_pool and _near_dup(toks, dedup_against):
        return False                   # legacy: a plain list of token sets
    eid = event_id or _iid(headline)
    tickers, category, impact, einfo = _enrich2(headline, universe, holds, explicit_tickers,
                                                desc=(item.get("desc") or ""), tag_gate=tag_gate)
    tickers, category, impact, fc_dropped = _factcheck_tag_filter(
        headline, tickers, category, impact, holds, universe)
    dedup_tks = set(tickers) | set(einfo.get("resolved") or [])
    if is_pool:
        hit = dedup_against.find(headline, toks, dedup_tks, source=source)
        if hit is not None:
            if _ncfg("dedup", "merge_cross_outlet", True) and _merge_copy(conn, hit, item, source):
                _dedup_stats["merged"] += 1
            else:
                _dedup_stats["skipped"] += 1
            return False
    now = time.time()
    pub = item.get("pub_epoch")
    latency = (now - pub) if pub else None
    if ingested_at:
        now = float(ingested_at)
    raw_obj = {"src_url": item.get("url", ""), **(extra_raw or {})}
    if fc_dropped:                 # Phase 4 Layer 0: never drop a tag silently
        raw_obj["fc_dropped_tickers"] = fc_dropped
    if einfo.get("dropped_tags"):  # R10: vendor tags refused (foreign listing / unknown symbol)
        raw_obj["dropped_tags"] = einfo["dropped_tags"]
    if einfo.get("renamed_tags"):  # a vendor's old ticker, stored under the new one
        raw_obj["renamed_tags"] = einfo["renamed_tags"]
    _desc = (item.get("desc") or "").strip()
    if _desc:                      # the feed's own blurb — a free per-article summary
        raw_obj["desc"] = _desc
    cur = conn.execute(
        "INSERT OR IGNORE INTO news_events "
        "(id, headline, url, source, source_tier, tickers, category, impact, "
        " published_at, ingested_at, recv_latency, scored, pinged, raw, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)",
        (eid, headline, item.get("url", ""), source, tier,
         json.dumps(tickers), category, impact, pub, now, latency,
         json.dumps(raw_obj), "news_service"),
    )
    inserted = cur.rowcount > 0
    if inserted and dedup_against is not None:
        if is_pool:
            dedup_against.add(eid, headline, dedup_tks, source, time.time(), toks=toks)
        else:
            dedup_against.append(toks)
    return inserted


# ── public API ────────────────────────────────────────────────────────────────

_scan_lock = threading.Lock()


def scan_once(conn=None) -> int:
    """One full pass over the catalog. Returns the number of NEW events stored.
    Never raises — a bad feed is recorded and skipped."""
    if not _scan_lock.acquire(blocking=False):
        return 0                       # a scan is already running; don't stack
    own = conn is None
    try:
        if own:
            conn = mason_db.get_connection()
        try:
            import mason_news
            universe = mason_news.watchlist_set()
            holds = mason_news.holdings_set()
        except Exception:
            universe, holds = set(), set()

        universe, holds = _with_agent_trades(conn, universe, holds)
        # one recent-headline pool for near-dup suppression across the pass
        # (time-based and indexed by ticker — see _DedupPool)
        dedup = _recent_pool(conn)

        total_new = 0
        went_quiet, came_back = [], []
        for feed_url, source, tier in FEED_CATALOG:
            row = _get_source_row(conn, feed_url)
            if _resting(row) or _stale_wait(row):
                continue
            items, ok = _fetch(feed_url)
            if not ok:
                with conn:
                    _update_source(conn, feed_url, source, tier, ok=False)
                    _feed_health_after_fetch(conn, feed_url, source, False, [], went_quiet, came_back)
                continue
            inc = _FEED_INCLUDE.get(feed_url)
            try:                               # (review fix) news.feeds.first_fetch_max_age_hours, default 48
                first_max_age = float(_ncfg("feeds", "first_fetch_max_age_hours", 48) or 48) * 3600
            except Exception:
                first_max_age = 48 * 3600.0

            wm = (row["last_pub_date"] if row else None) or 0
            newest_pub = wm
            newest_url = row["last_url"] if row else None
            with conn:
                for it in items[:_TIER_LIMIT.get(tier, 8)]:
                    pub = it.get("pub_epoch")
                    # watermark short-circuit (dedup is still guaranteed by PK)
                    if pub and wm and pub <= wm:
                        continue
                    if pub and pub > newest_pub:
                        newest_pub = pub
                        newest_url = it.get("url")
                    if not wm and pub and (time.time() - pub) > first_max_age:
                        continue               # first fetch of this feed: old items only set the watermark
                    if inc is not None and not inc.search(f"{it.get('title') or ''} {it.get('desc') or ''}"):
                        continue               # off-topic item of a general agency feed
                    if _insert_event(conn, it, source, tier, universe, holds,
                                     dedup_against=dedup):
                        total_new += 1
                _update_source(conn, feed_url, source, tier, ok=True,
                               newest_pub=newest_pub or None, newest_url=newest_url)
                _feed_health_after_fetch(conn, feed_url, source, True, items, went_quiet, came_back)
        _notify_feed_health(went_quiet, came_back)

        # ── Phase 5: GDELT global events (rate-limited → poll at most ~9 min) ──
        # _gdelt_last used to be stamped BEFORE the fetch, so the health probe was
        # green even when every pull failed (audit R7). Pacing now uses the attempt
        # time; the health stamp moves only when a pull actually returned articles.
        global _gdelt_last, _gdelt_last_try
        if time.time() - _gdelt_last_try > _GDELT_EVERY:
            _gdelt_last_try = time.time()
            gd = _fetch_gdelt()
            if gd:
                _gdelt_last = time.time()
                with conn:
                    for it in gd:
                        if _insert_event(conn, it, "GDELT", 3, universe, holds,
                                         dedup_against=dedup):
                            total_new += 1

        # ── Phase 5: Telegram aggregators (public t.me/s preview, no auth) ──
        for slug, disp, tier in _TG_CHANNELS:
            tg = _fetch_telegram(slug)
            if not tg:
                continue
            with conn:
                for it in tg:
                    if _insert_event(conn, it, disp, tier, universe, holds,
                                     dedup_against=dedup):
                        total_new += 1

        # ── Phase 6a: SEC EDGAR 8-K (primary source; universe filers only) ──
        try:
            total_new += _scan_edgar(conn, universe, holds)
        except Exception as e:
            log.warning(f"[news-scan] edgar 8-K pass failed: {e}")

        # ── Catalyst Desk: free structured sources (mason_sources; flag-gated
        # inside by catalyst.sources.enabled — no-op when off or absent) ──
        try:
            import mason_sources
            total_new += int(mason_sources.scan_sources(conn, universe, holds) or 0)
        except ImportError:
            pass
        except Exception as e:
            log.warning(f"[news-scan] catalyst sources pass failed: {e}")

        if total_new:
            log.info(f"[news-scan] +{total_new} new events")
        return total_new
    except Exception as e:
        log.warning(f"[news-scan] failed: {e}")
        return 0
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _scan_lock.release()


# Phase 4: an event is "material" (worth a ping) when qwen scored it, it touches
# the book, and materiality clears this bar. Tunable.
# 2026-09-12: lowered 0.6 -> 0.4 at Nico's call — a book-relevant, reliably
# sourced item (e.g. the AAPL/NVDA foldable-iPhone roundup, scored 0.4) was
# being kept out of Notifications. Trades some added noise for broader coverage.
_MATERIAL_THRESHOLD = 0.4


def _is_material(row) -> bool:
    try:
        mat = row["materiality"]
        return bool(row["scored"] and mat is not None and mat >= _MATERIAL_THRESHOLD
                    and (row["category"] in ("holding", "watchlist")))
    except Exception:
        return False


def _shape_row(r) -> dict:
    """One news_events row → the bar/notifications item shape (ET time, material)."""
    try:
        tickers = json.loads(r["tickers"]) if r["tickers"] else []
    except Exception:
        tickers = []
    ts = r["published_at"] or r["ingested_at"] or 0
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        # Show the time in Nico's timezone (ET), not UTC.
        try:
            from zoneinfo import ZoneInfo
            time_str = dt.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M%p").lower() + " et"
        except Exception:
            time_str = dt.strftime("%-I:%M%p").lower()
        import mason_news
        overnight = mason_news._is_overnight(dt)
    except Exception:
        time_str, overnight = "", False
    return {
        "id":          r["id"],
        "ts":          ts,
        "time_str":    time_str,
        "tickers":     tickers,
        "title":       r["headline"],
        "headline":    r["headline"],
        "source":      r["source"] or "",
        "url":         r["url"] or "",
        # The dashboard's news lists know three classes (holding / watchlist / macro) and
        # would show a 'market' row in none of them, so it is presented on the macro
        # ("tape") side; the true class travels beside it for any reader that wants it.
        "category":    ("macro" if (r["category"] or "macro") == "market" else (r["category"] or "macro")),
        "news_class":  r["category"] or "macro",
        "impact":      r["impact"] or "yellow",
        "materiality": r["materiality"],
        "direction":   r["direction"],
        "implication": r["implication"],
        "overnight":   overnight,
        "material":    _is_material(r),
        "desc":        _row_desc(r),
        # 2026-11-15 (news quality): what the inbox rules need to place the item
        "ingested_at": _rv(r, "ingested_at"),
        "published_at": _rv(r, "published_at"),
        "catalyst_id": _rv(r, "catalyst_id"),
        "resolved_tickers": _rv(r, "resolved_tickers"),
    }


def _row_desc(r) -> str:
    """The feed's own blurb, stashed in news_events.raw.desc ('' if none)."""
    try:
        raw = r["raw"]
        return (json.loads(raw) or {}).get("desc") or "" if raw else ""
    except Exception:
        return ""


def recent_events(limit: int = 36) -> list[dict]:
    """Newest events across ALL sources for the bar, shaped like the legacy
    /api/news items. Newest-first by publish time so the freshest headlines lead
    and new arrivals prepend into the widget."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM news_events "
            "ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [_shape_row(r) for r in rows]


# ── UI overhaul Task 5 (2026-11-16): day-wide flow for the v2 News page ─────────
# GET /api/news/flow (mason_app.api_news_flow) → the sentiment timeline, the impact
# bubble map and the scrub / bubble-filtered feeds. /api/news stays the 36-item bar
# feed (about 25 minutes of a ~2,600-story day), too short for a day timeline.
# Read-only: one indexed SELECT over the window plus one by-id SELECT for the items.
_FLOW_MAX_ITEMS = 200
_FLOW_TOP_TICKERS = 48
_FLOW_BOOK_TTL = 120.0
_flow_book_mem = {"t": 0.0, "h": frozenset(), "w": frozenset()}
_flow_uni_mem = {"mtime": None, "map": {}}
# The universe mixes GICS (S&P table) and ICB (NDX table) names; fold ICB onto GICS.
_FLOW_SECTOR_CANON = {"technology": "Information Technology",
                      "telecommunications": "Communication Services",
                      "basic materials": "Materials"}
_FLOW_SECTOR_ETF = {"Information Technology": "XLK", "Communication Services": "XLC",
                    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
                    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV",
                    "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
                    "Utilities": "XLU"}


def _flow_day_start(now: float) -> float:
    """Epoch of today's 00:00 in America/New_York (Nico's day)."""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromtimestamp(now, tz=ZoneInfo("America/New_York"))
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    except Exception:
        return now - (now % 86400)


def _flow_universe() -> dict:
    """{TICKER: (sector, name)} from the cached universe_broad.json (S&P 500 +
    Nasdaq-100, refreshed weekly by mason_triage). File read only, never network."""
    try:
        p = BASE / "universe_broad.json"
        mt = p.stat().st_mtime
        if _flow_uni_mem["mtime"] != mt:
            doc = json.loads(p.read_text(encoding="utf-8")) or {}
            m = {}
            for tk, v in (doc.get("tickers") or {}).items():
                if not isinstance(v, dict):
                    continue
                s = (v.get("sector") or "").strip()
                s = _FLOW_SECTOR_CANON.get(s.lower(), s)
                m[str(tk).upper()] = (s, (v.get("name") or "").strip())
            _flow_uni_mem.update(mtime=mt, map=m)
        return _flow_uni_mem["map"]
    except Exception:
        return {}


def _flow_book_sets(now: float):
    """(holdings, watchlist) ticker sets, cached for _FLOW_BOOK_TTL seconds."""
    if now - _flow_book_mem["t"] > _FLOW_BOOK_TTL:
        try:
            import mason_news
            h = frozenset(str(t).upper() for t in mason_news.holdings_set())
            w = frozenset(str(t).upper() for t in mason_news.watchlist_set())
        except Exception:
            h, w = frozenset(), frozenset()
        _flow_book_mem.update(t=now, h=h, w=w)
    return _flow_book_mem["h"], _flow_book_mem["w"]


_FLOW_NAME_SUFFIX = re.compile(
    r"(?i)[,.]?\s+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|sa|se|nv|n\.v|"
    r"holdings?|group|the)\.?$")
_FLOW_GENERIC = frozenset(
    "the first general american united international global national bank new royal digital "
    "energy health capital financial data applied advanced home public southern northern "
    "western eastern consolidated federal realty texas pacific atlantic".split())
_flow_phrase_mem = {"mtime": None, "map": {}}


def _flow_name_phrases() -> dict:
    """{TICKER: compiled regex of its company name's distinctive head} for the universe."""
    _flow_universe()
    if _flow_phrase_mem["mtime"] != _flow_uni_mem["mtime"]:
        m = {}
        for t, (_s, nm) in (_flow_uni_mem["map"] or {}).items():
            n = (nm or "").strip()
            for _ in range(3):
                n = _FLOW_NAME_SUFFIX.sub("", n).strip()
            words = [w for w in re.split(r"\s+", n) if w]
            if words and words[0].lower() == "the":
                words = words[1:]
            if not words:
                continue
            head = words[:2] if (words[0].lower() in _FLOW_GENERIC and len(words) > 1) else words[:1]
            phrase = " ".join(head)
            if len(phrase) >= 4:
                m[t] = (phrase.lower(),
                        re.compile(r"(?i)(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])"))
        _flow_phrase_mem.update(mtime=_flow_uni_mem["mtime"], map=m)
    return _flow_phrase_mem["map"]


_flow_sym_mem = {}


def _flow_sym_rx(t: str):
    """Compiled 'symbol named in a headline' pattern (cached): a one-letter symbol
    ("C", "F") only as "$C"; longer ones as a bare token too."""
    rx = _flow_sym_mem.get(t)
    if rx is None:
        if len(_flow_sym_mem) > 4096:
            _flow_sym_mem.clear()
        rx = _flow_sym_mem[t] = re.compile(
            r"(?<![A-Za-z0-9$])\$" + re.escape(t) + r"(?![A-Za-z0-9])" if len(t) < 2 else
            r"(?<![A-Za-z0-9])\$?" + re.escape(t) + r"(?![A-Za-z0-9])")
    return rx


def _flow_tickers(r) -> list:
    """Symbols a row touches, upper-cased, de-duplicated, tagged ones first: every
    tagged `tickers` symbol, plus a triage `resolved_tickers` symbol only when the
    headline names it (the symbol as a token, or the company name's head). The
    resolver also reads story bodies, where e.g. a "Nov. 16" dateline resolved to
    NOV on ~150 stories a day (resolver fixed 2026-11-16, R2-A; rows stored before
    then keep it until tools/cleanup_dateline_tags.py runs); a map of what the
    headlines are about must not."""
    out = []
    try:
        vals = json.loads(r["tickers"]) if r["tickers"] else []
    except Exception:
        vals = []
    for t in vals if isinstance(vals, list) else []:
        t = str(t or "").strip().upper()
        if t and len(t) <= 16 and t not in out:
            out.append(t)
    try:
        raw = r["resolved_tickers"]
        vals = json.loads(raw) if raw else []
    except Exception:
        vals = []
    if isinstance(vals, list) and vals:
        try:
            head = str(r["headline"] or "")
        except Exception:
            head = ""
        phrases = _flow_name_phrases()
        low = head.lower()
        for t in vals:
            t = str(t or "").strip().upper()
            if not t or len(t) > 16 or t in out:
                continue
            ok = t in head and _flow_sym_rx(t).search(head) is not None
            if not ok:
                ph = phrases.get(t)
                ok = ph is not None and ph[0] in low and ph[1].search(head) is not None
            if ok:
                out.append(t)
    return out


def flow(span: str = "today", bucket_min: float = 15, frm: float = None, to: float = None,
         ticker: str = None, sector: str = None, limit: int = 120, agg: bool = True,
         now: float = None, conn=None) -> dict:
    """Day-wide news flow for the v2 News page (UI overhaul Task 5).

    Window: span 'today' = since 00:00 ET, '24h' = the last 24 hours. Aggregates
    (agg=True) always cover the whole window:
      buckets  — dense, oldest first, bucket_min (5-60) minutes each:
                 {t, n, bull, bear, mat}  (neutral = n - bull - bear)
      tickers  — top _FLOW_TOP_TICKERS by story count:
                 {ticker, n, bull, bear, mat, last, book, sector, etf, name}
      sectors  — GICS sectors of those stories (a story counts once per sector):
                 {sector, etf, n, bull, bear, mat, top:[tickers]}; sector_other =
                 stories whose tickers are all outside the S&P 500 / Nasdaq-100
      day      — {n, bull, bear, mat} for the window
    Items: newest first, at most `limit` (<= 200), narrowed by frm/to (epoch s),
    ticker and sector; each is a /api/news item (_shape_row) plus tk_all,
    event_kind, market_materiality, source_tier. items_total counts every match.
    names: {TICKER: {name, sector}} for the tickers on the returned items.
    Never raises on bad input (clamped); `conn` is for tests/fixtures (not closed)."""
    now = float(now if now is not None else time.time())
    span = span if span in ("today", "24h") else "today"
    since = _flow_day_start(now) if span == "today" else now - 86400.0
    try:
        bucket_s = int(max(5, min(60, int(float(bucket_min or 15))))) * 60
    except (TypeError, ValueError):
        bucket_s = 900
    try:
        limit = max(1, min(_FLOW_MAX_ITEMS, int(limit or 120)))
    except (TypeError, ValueError):
        limit = 120
    tk = re.sub(r"[^A-Z0-9.\-^/]", "", str(ticker or "").strip().upper())[:16] or None
    sec = str(sector or "").strip()[:48] or None
    lo, hi, to_given = since, now, False
    try:
        if frm is not None:
            lo = max(since, float(frm))
        if to is not None:
            hi, to_given = min(now, float(to)), float(to) < now
    except (TypeError, ValueError):
        lo, hi, to_given = since, now, False

    own = conn is None
    if own:
        conn = mason_db.get_connection()
    try:
        sel = ("SELECT id, headline, tickers, resolved_tickers, category, direction, materiality, "
               "scored, COALESCE(published_at, ingested_at) AS ts FROM news_events "
               "WHERE ingested_at >= ? AND COALESCE(published_at, ingested_at) >= ? {} "
               "ORDER BY ts DESC")
        # ingested_at >= since - 6h keeps the index in play: a row is never ingested
        # before it was published (recv latency >= 0), so nothing in the window is lost.
        args = (since - 6 * 3600, since)
        try:
            rows = conn.execute(sel.format("AND COALESCE(is_deleted, 0) = 0"), args).fetchall()
        except Exception:                       # pre-soft-delete schema
            rows = conn.execute(sel.format(""), args).fetchall()

        uni = _flow_universe()
        holds, watch = _flow_book_sets(now) if agg else (frozenset(), frozenset())
        n_b = max(1, int(-(-(now - since) // bucket_s)))
        buckets = [{"t": since + i * bucket_s, "n": 0, "bull": 0, "bear": 0, "mat": 0}
                   for i in range(n_b)]
        day = {"n": 0, "bull": 0, "bear": 0, "mat": 0}
        per_tk, per_sec = {}, {}
        sector_other = 0
        keep_ids, items_total = [], 0
        tk_all_of = {}
        for r in rows:
            try:
                ts = min(float(r["ts"] or 0), now)
            except (TypeError, ValueError):
                continue
            if ts < since:
                continue
            tks = _flow_tickers(r)
            d = r["direction"]
            bull, bear = (1 if d == "bullish" else 0), (1 if d == "bearish" else 0)
            mat = 1 if _is_material(r) else 0
            if agg:
                b = buckets[min(n_b - 1, max(0, int((ts - since) // bucket_s)))]
                for acc in (b, day):
                    acc["n"] += 1
                    acc["bull"] += bull
                    acc["bear"] += bear
                    acc["mat"] += mat
                secs = set()
                for t in tks:
                    a = per_tk.get(t)
                    if a is None:
                        a = per_tk[t] = {"ticker": t, "n": 0, "bull": 0, "bear": 0, "mat": 0, "last": 0.0}
                    a["n"] += 1
                    a["bull"] += bull
                    a["bear"] += bear
                    a["mat"] += mat
                    a["last"] = max(a["last"], ts)
                    s = (uni.get(t) or ("", ""))[0]
                    if s:
                        secs.add(s)
                for s in secs:
                    a = per_sec.get(s)
                    if a is None:
                        a = per_sec[s] = {"sector": s, "etf": _FLOW_SECTOR_ETF.get(s, ""), "n": 0,
                                          "bull": 0, "bear": 0, "mat": 0, "_tk": {}}
                    a["n"] += 1
                    a["bull"] += bull
                    a["bear"] += bear
                    a["mat"] += mat
                    for t in tks:
                        if (uni.get(t) or ("", ""))[0] == s:
                            a["_tk"][t] = a["_tk"].get(t, 0) + 1
                if tks and not secs:
                    sector_other += 1
            # [from, to) like the buckets; with no `to` the window ends at now, inclusive
            if ts < lo or ts > hi or (to_given and ts >= hi):
                continue
            if tk and tk not in tks:
                continue
            if sec and not any((uni.get(t) or ("", ""))[0] == sec for t in tks):
                continue
            items_total += 1
            if len(keep_ids) < limit:
                keep_ids.append(r["id"])
                tk_all_of[r["id"]] = tks

        items = []
        if keep_ids:
            ph = ",".join("?" * len(keep_ids))
            full = {r["id"]: r for r in conn.execute(
                f"SELECT * FROM news_events WHERE id IN ({ph})", keep_ids).fetchall()}
            for i in keep_ids:
                r = full.get(i)
                if r is None:
                    continue
                it = _shape_row(r)
                it["tk_all"] = tk_all_of.get(i, [])
                it["event_kind"] = _rv(r, "event_kind")
                it["market_materiality"] = _rv(r, "market_materiality")
                it["source_tier"] = _rv(r, "source_tier")
                items.append(it)
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass

    names = {}
    for it in items:
        for t in it.get("tk_all") or []:
            if t in uni and t not in names:
                names[t] = {"name": uni[t][1], "sector": uni[t][0]}
    out = {"span": span, "since": since, "until": now, "bucket_s": bucket_s,
           "from": lo, "to": hi, "ticker": tk, "sector": sec,
           "items": items, "items_total": items_total, "names": names}
    if agg:
        def _book(t):
            return "holding" if t in holds else ("watchlist" if t in watch else "")
        top = sorted(per_tk.values(), key=lambda a: (-a["n"], -a["mat"], -a["last"], a["ticker"]))
        for a in top[:_FLOW_TOP_TICKERS]:
            s, nm = uni.get(a["ticker"]) or ("", "")
            a.update(book=_book(a["ticker"]), sector=s, etf=_FLOW_SECTOR_ETF.get(s, ""), name=nm)
        secs_out = []
        for a in sorted(per_sec.values(), key=lambda a: (-a["n"], a["sector"])):
            top_tk = sorted(a.pop("_tk").items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            a["top"] = [t for t, _ in top_tk]
            secs_out.append(a)
        out.update(buckets=buckets, tickers=top[:_FLOW_TOP_TICKERS], sectors=secs_out,
                   sector_other=sector_other, day=day, total=day["n"])
    return out


def notifications(limit: int = 50, hours: int = 48) -> list[dict]:
    """Material book-relevant events for the Notifications page (Phase 4), most
    material first. These are the 'pings' — high-materiality events touching the
    book, carrying the qwen direction/materiality/implication."""
    conn = mason_db.get_connection()
    cutoff = time.time() - hours * 3600
    try:
        rows = conn.execute(
            "SELECT * FROM news_events WHERE scored=1 AND materiality >= ? "
            "AND category IN ('holding','watchlist') AND ingested_at > ? "
            "ORDER BY materiality DESC, ingested_at DESC LIMIT ?",
            (_MATERIAL_THRESHOLD, cutoff, limit),
        ).fetchall()
        items = [_shape_row(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # 2026-11-15 (news.quality.ui_filter): leave out what the inbox refused (foreign
    # listing, routine item, not about the book company) or already shows inside a
    # story, so the Notifications page does not bring them back as loose pings.
    try:
        import mason_news_quality as NQ
        if NQ.on("ui_filter"):
            folded = NQ.folded_news_ids(hours=hours + 24)
            items = [it for it in items if it.get("id") not in folded
                     and NQ.gate_event(it, stream="ui", check_age=False).get("ok")]
    except Exception:
        pass
    return items


def recall_material(tickers=None, query: str = None, days: int = 60,
                    limit: int = 15) -> list[dict]:
    """Conversational recall over the RETAINED material news events (the ~2-3% the
    retention prune keeps forever: scored, materiality >= threshold, book-relevant).
    Powers the recall_material_news LLM tool so Mason can cite past material news
    when spoken to. Filters:
      * tickers — list of symbols; matches any (tickers stored as a JSON array)
      * query   — case-insensitive substring of the headline
      * days    — look-back window (default 60; material is kept indefinitely, so a
                  larger window still resolves)
    Ranked most-material first, then newest. Returns shaped rows (see _shape_row)."""
    try:
        window = max(1, int(days))
    except (TypeError, ValueError):
        window = 60
    cutoff = time.time() - window * 86400
    where = ["scored = 1", "materiality >= ?",
             "category IN ('holding','watchlist')", "ingested_at > ?"]
    params: list = [_MATERIAL_THRESHOLD, cutoff]
    if query and str(query).strip():
        where.append("headline LIKE ?")
        params.append(f"%{str(query).strip()}%")
    tks = [str(t).strip().upper() for t in (tickers or []) if t and str(t).strip()]
    if tks:
        where.append("(" + " OR ".join("UPPER(tickers) LIKE ?" for _ in tks) + ")")
        params.extend([f'%"{t}"%' for t in tks])
    try:
        lim = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        lim = 15
    params.append(lim)
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM news_events WHERE " + " AND ".join(where) +
            " ORDER BY materiality DESC, ingested_at DESC LIMIT ?", params,
        ).fetchall()
        return [_shape_row(r) for r in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def material_events_for_brief(limit: int = 8) -> list[dict]:
    """Unpinged material events for the brief/greeting fold-in. Marks them
    pinged=1 so the same event is never folded into two briefs. Compact shape."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM news_events WHERE scored=1 AND pinged=0 AND materiality >= ? "
            "AND category IN ('holding','watchlist') "
            "ORDER BY materiality DESC, ingested_at DESC LIMIT ?",
            (_MATERIAL_THRESHOLD, limit),
        ).fetchall()
        if not rows:
            return []
        with conn:
            conn.executemany("UPDATE news_events SET pinged=1 WHERE id=?",
                             [(r["id"],) for r in rows])
        out = []
        for r in rows:
            try:
                tickers = json.loads(r["tickers"]) if r["tickers"] else []
            except Exception:
                tickers = []
            out.append({
                "headline": r["headline"], "tickers": tickers,
                "direction": r["direction"], "materiality": r["materiality"],
                "implication": r["implication"], "source": r["source"] or "",
            })
        return out
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def event_count() -> int:
    conn = mason_db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def prune_old(days: int = 30) -> int:
    """Retention: hard-DELETE OLD, NON-MATERIAL news_events (by ingested_at, an
    epoch float). This is the biggest-growing table (continuous ingest) and only
    ~2-3% of it is material, so we prune the noise and KEEP the important events
    indefinitely — the record Mason should still be able to reference. An event is
    KEPT (never pruned) if any of:
      * it's material and book-relevant (scored, materiality >= threshold,
        category in holding/watchlist) — the events that actually matter;
      * it's still referenced by an ACTIVE notification (so detail links don't
        dangle);
      * (memory spine, 2026-09-28) triage scored it MARKET-material
        (market_materiality >= memory.retention.keep_market_material, 0.6);
      * it's linked to a catalyst whose story score >= that same threshold;
      * a thesis references it (theses.source_refs[].id / match_log[].news_id) or a
        director/qwen recommendation cites it (recommendations.source_news_id).
    Everything else older than `days` is removed. Returns rows deleted."""
    cutoff = time.time() - days * 86400
    try:
        import mason_theses
        keep_mm = float(mason_theses.retention_keep_threshold())
    except Exception:
        keep_mm = 0.6
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM news_events WHERE ingested_at < ? "
                # keep the material, book-relevant minority forever
                "  AND NOT (scored = 1 AND materiality >= ? "
                "           AND category IN ('holding','watchlist')) "
                # keep anything an active notification still points at
                "  AND id NOT IN (SELECT ref_id FROM notifications "
                "     WHERE ref_table = 'news_events' AND ref_id IS NOT NULL "
                "       AND record_deleted = 0) "
                # memory spine: keep market-material news (triage's score) ...
                "  AND COALESCE(market_materiality, 0) < ? "
                # ... anything linked to a high-scoring catalyst story ...
                "  AND NOT (catalyst_id IS NOT NULL AND catalyst_id IN "
                "     (SELECT id FROM catalysts WHERE score >= ?)) "
                # ... anything a thesis started from / matched ...
                "  AND id NOT IN (SELECT json_extract(j.value, '$.id') FROM theses t, "
                "     json_each(CASE WHEN json_valid(t.source_refs) THEN t.source_refs ELSE '[]' END) j "
                "     WHERE t.record_deleted = 0 AND json_extract(j.value, '$.id') IS NOT NULL) "
                "  AND id NOT IN (SELECT json_extract(j.value, '$.news_id') FROM theses t, "
                "     json_each(CASE WHEN json_valid(t.match_log) THEN t.match_log ELSE '[]' END) j "
                "     WHERE t.record_deleted = 0 AND json_extract(j.value, '$.news_id') IS NOT NULL) "
                # ... and anything a recommendation cites
                "  AND id NOT IN (SELECT source_news_id FROM recommendations "
                "     WHERE source_news_id IS NOT NULL AND COALESCE(record_deleted, 0) = 0)",
                (cutoff, _MATERIAL_THRESHOLD, keep_mm, keep_mm),
            )
        return cur.rowcount
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── qwen scoring pass (NEWS_SERVICE_DESIGN.md Phase 3) ─────────────────────────
# For book-relevant, not-yet-scored events, ask the LOCAL qwen model (via the
# mason_llm gateway, intent="chore") for a trader read: direction + materiality
# (0-1) + a one-line book-relative implication. Batched + capped so qwen load
# stays bounded; the instant regex `impact` dot is untouched (zero-latency path).
# materiality is what Phase 4 will threshold on to escalate to Opus / notify.

# Step 3 (audit R4) — what changed here, and what did not:
#   * WHICH headlines are sent, and in what ORDER: strong events (management change,
#     guidance cut, halt, bankruptcy …) first, then holdings / Mason's open trades,
#     then the watchlist, then other listed companies ('market'), newest first inside
#     each. The prompt WORDING is unchanged, byte for byte.
#   * HOW MANY per pass: several batches of 10 (the batch the prompt was tuned on)
#     inside a time budget, instead of one batch every two minutes. Measured from
#     mason_trace.jsonl: ~48 output tokens per headline, 8-9 s per batch of 10 on the
#     GPU. Between batches the pass yields to an interactive chat
#     (mason_llm.interactive_busy), exactly like the analyst desk does.
#   * WHAT the model's number may do: final materiality = max(model, floor of the
#     event kind) for a strong event on the book, and min(model, ceiling) for a
#     commentary headline (mason_triage.event_bounds). A headline the model's reply
#     simply left out is retried instead of being stored as "scored 0.0".
_score_lock = threading.Lock()
_PRIO = {"holding": 1, "watchlist": 2, "market": 3}


def _row_raw(r) -> dict:
    try:
        raw = json.loads(r["raw"]) if r["raw"] else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


# Smallfix 2026-11-18 (news.scoring.strip_live_marker, default OFF; NEWS_SCORING_REVIEW.md finding 6). A headline
# that ENDS on a live-blog marker is a live report: the words before the marker say what happened ("Nvidia CEO
# Jensen Huang resigns over scandal and is replaced by CFO Colette Kress on interim basis – follow live"). Today
# the marker alone reads as a recap (mason_triage.non_event_reason: "follow live", "live updates / coverage /
# blog") and caps the score at the commentary ceiling (0.3), under every line. With the switch on the scorer
# judges the headline without a TRAILING marker; everything else in it is judged by the usual rules, so a
# look-back ("– as it happened"), commentary ("What … means – follow live"), a market wrap or a leading
# "… live updates:" label are judged exactly as today. Only the scorer's own limits change (the commentary
# ceiling, the strong-event floor and queue order, the classify held-name agreement): the model reads the very
# same headline and prompt. The catalyst scorer (mason_triage) is not changed.
_LIVE_MARKER_RE = re.compile(
    r"(?:\s+[-–—]+\s*|\s*[|:]\s*|\s*\(\s*)(?:follow\s+live|live\s+(?:updates?|coverage|blog))\s*\)?[\s.!]*$", re.I)


def strip_live_marker(headline: str) -> str:
    """`headline` without a trailing live-blog marker ("… – follow live", "… | Live Updates", "… (Live Coverage)");
    unchanged when there is none or when fewer than three words would be left. Pure; never raises."""
    try:
        h = str(headline or "")
        m = _LIVE_MARKER_RE.search(h)
        if not m:
            return h
        rest = h[:m.start()].rstrip()
        return rest if len(rest.split()) >= 3 else h
    except Exception:
        return str(headline or "")


def _score_bounds(r, raw=None, strip_live=None) -> dict:
    """mason_triage.event_bounds for one queue row ({} when triage is unavailable). strip_live: judge the headline
    without a trailing live-blog marker (news.scoring.strip_live_marker; None = read the setting)."""
    try:
        import mason_triage
        raw = raw if raw is not None else _row_raw(r)
        head = r["headline"] or ""
        if strip_live is None:
            strip_live = _ncfg("scoring", "strip_live_marker") is True
        if strip_live:
            head = strip_live_marker(head)
        return mason_triage.event_bounds(
            head, hint=raw.get("event_kind_hint"), has_ticker=True,
            tier=(r["source_tier"] or 2), structured=bool(raw.get("catalyst_source")))
    except Exception:
        return {}


def score_queue(conn, cap: int = None, now: float = None) -> list:
    """The unscored events to send to the local model next, in priority order:
    strong-kind events, then holdings (incl. Mason's open trades), then watchlist,
    then 'market' (other listed companies), newest first within each. A 'market'
    event that is not strong drops out of the queue after
    news.scoring.market_max_age_hours (a strong one never does, inside the
    lookback). Each item: (row, bounds, priority). Read-only."""
    sc = _ncfg("scoring")
    now = now or time.time()
    cap = int(cap if cap is not None else sc.get("max_per_pass", 60))
    strip = sc.get("strip_live_marker") is True          # smallfix 2026-11-18, default off
    if not sc.get("priority_enabled", True):
        rows = conn.execute(
            "SELECT id, headline, tickers, category, source_tier, raw, ingested_at FROM news_events "
            "WHERE scored=0 AND category IN ('holding','watchlist') "
            "ORDER BY ingested_at DESC LIMIT ?", (cap,)).fetchall()
        return [(r, _score_bounds(r, strip_live=strip), _PRIO.get(r["category"], 3)) for r in rows]
    lookback = now - float(sc.get("lookback_hours", 72)) * 3600
    rows = conn.execute(
        "SELECT id, headline, tickers, category, source_tier, raw, ingested_at FROM news_events "
        "WHERE scored=0 AND category IN ('holding','watchlist','market') AND ingested_at > ? "
        "ORDER BY ingested_at DESC LIMIT 4000", (lookback,)).fetchall()
    market_cut = now - float(sc.get("market_max_age_hours", 12)) * 3600
    out = []
    for r in rows:
        b = _score_bounds(r, strip_live=strip)
        strong = bool(b.get("strong"))
        if r["category"] == "market" and not strong and (r["ingested_at"] or 0) < market_cut:
            continue
        out.append((r, b, 0 if strong else _PRIO.get(r["category"], 3)))
    out.sort(key=lambda x: (x[2], -(x[0]["ingested_at"] or 0)))
    return out[:cap]


def _deterministic_direction(headline: str) -> str:
    try:
        import mason_triage
        return mason_triage._direction(headline)
    except Exception:
        return "neutral"


def score_pending(cap: int = None) -> int:
    """Score up to `cap` unscored events with qwen (default news.scoring.max_per_pass),
    in batches of news.scoring.batch_size, inside news.scoring.pass_budget_seconds,
    yielding to an interactive chat between batches. Returns the number scored.
    Never raises; leaves rows unscored (for retry) if qwen is down."""
    try:
        import mason_llm
    except Exception:
        return 0
    if not _score_lock.acquire(blocking=False):
        return 0                       # another pass (scanner or scorer thread) is running
    conn = None
    try:
        conn = mason_db.get_connection()   # inside the try: the lock is always released (review fix)
        sc = _ncfg("scoring")
        queue = score_queue(conn, cap)
        if not queue:
            return 0
        # Catalyst Desk (catalyst.triage.enabled; off => the prompt below is the
        # original, byte-for-byte). `materiality` stays BOOK-relative (inbox);
        # when triage is on qwen also gives a separate MARKET read ("market":
        # how much this moves the named stock/sector for anyone), stored in
        # raw.qwen_market as one input to mason_triage's market_materiality.
        try:
            import mason_triage
            _tri = mason_triage.triage_enabled()
        except Exception:
            _tri = False
        try:
            import mason_news
            book = ", ".join(sorted(mason_news.holdings_set())) or "(none)"
        except Exception:
            book = "(none)"
        bsize = max(1, int(sc.get("batch_size", 10)))
        budget = float(sc.get("pass_budget_seconds", 75))
        t0 = time.monotonic()
        total = n_material = 0
        for k in range(0, len(queue), bsize):
            if k and (time.monotonic() - t0) > budget:
                break
            if k:
                try:
                    if mason_llm.interactive_busy():
                        break          # the user is chatting: give the GPU back
                except Exception:
                    pass
            got = _score_batch(conn, queue[k:k + bsize], mason_llm, _tri, book, sc)
            if got is None:            # model unavailable — stop this pass, retry later
                break
            total += got[0]
            n_material += got[1]
        if total:
            log.info(f"[news-score] scored {total} events (qwen); {n_material} material")
        return total
    except Exception as e:
        log.warning(f"[news-score] failed: {e}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _score_lock.release()


# ── Tolerant reader for the scorer's reply (2026-11-17, LOCAL_MODEL_STUDY S1) ──
# The model is asked for one JSON object mapping each headline id to its answer.
# On 16 Nov only 72.5% of headline answers were usable as sent: ids copied with a
# character wrong or missing, a list instead of a map, a bare answer for a single
# headline, several objects one after another, a brace left out, prose or fences
# around the JSON. This reader takes those forms, but attaches an answer to a
# headline only when the reply itself says which headline it is for:
#   * the exact id (as a key, as an "id" field, or as "<id>: {...}" / "[<id>]: {...}");
#   * an id one or two characters off, when no other id in the batch is within six AND
#     the answers just before and after it belong to the headlines just before and
#     after it (review 2026-11-17: on 16 Nov the model skipped a line and wrote one
#     headline's id, mis-copied, over the NEXT headline's answer);
#   * a lone answer when the batch has a single headline.
# Never by position in a list (order is the model's guess; it is only used to refuse a
# mis-copied id), never when two answers claim the same headline and disagree, never
# when a key and its "id" field disagree, never when one key names two ids.
# Answers today's reader already understands are used exactly as before.
# Switch: news.scoring.tolerant_reader (false = today's reader, unchanged).
_REPLY_FENCE = re.compile(r"```[A-Za-z]*")
_REPLY_OPEN = re.compile(r"[\[{]")
_REPLY_IDTOK = re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z]{12,20}(?![0-9A-Za-z])")
# "<id>": {   <id>: {   [<id>]: {   ('<id>') = {   — the key written just before an object
_REPLY_OWNER = re.compile(r"""(?<![0-9A-Za-z])([0-9A-Za-z]{12,20})\s*["')\]]?\s*[:=]\s*$""")
# "<id>":"direction":...  (opening brace left out) → "<id>":{"direction":...
_REPLY_NOBRACE = re.compile(
    r'("[0-9A-Za-z]{12,20}"\s*:\s*)(?="(?:direction|materiality|market|implication)"\s*:)')
_REPLY_NEAR = 2          # an id this many edits off still counts …
_REPLY_MARGIN = 6        # … when every other id in the batch is at least this far


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance between a and b, or cap + 1 once it is certainly above cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _reply_id(key, ids: list):
    """(batch id, exact?) that a reply key or "id" field names, else (None, False)."""
    if not isinstance(key, str):
        return None, False
    k = key.strip()
    if k in ids:
        return k, True
    toks = _REPLY_IDTOK.findall(k)
    if len(toks) != 1:
        return None, False
    t = toks[0].lower()
    if t in ids:
        return t, True
    d = sorted((_edit_distance(t, i, _REPLY_MARGIN), i) for i in ids)
    if d and d[0][0] <= _REPLY_NEAR and (len(d) == 1 or d[1][0] >= _REPLY_MARGIN):
        return d[0][1], False
    return None, False


def _reply_walk(v, owner, found: list, depth: int = 0) -> None:
    """Collect (key it sat under, its "id" field, answer) for every answer object in v."""
    if depth > 4:
        return
    if isinstance(v, list):
        for x in v:
            _reply_walk(x, owner if len(v) == 1 else None, found, depth + 1)
    elif isinstance(v, dict):
        if "direction" in v or "materiality" in v:
            found.append((owner, v.get("id"), v))
        else:
            for k, x in v.items():
                if isinstance(x, (dict, list)):
                    # an id key inside another id key's object names neither headline
                    two = (isinstance(owner, str) and isinstance(k, str) and owner.strip() != k.strip()
                           and _REPLY_IDTOK.search(owner) and _REPLY_IDTOK.search(k))
                    _reply_walk(x, None if two else k, found, depth + 1)


def _reply_owner(before: str):
    """The id written just before an object ("<id>": {   <id>: {   [<id>]: {), or None
    when there is none or that key names more than one id ("<id1>,<id2>": {)."""
    m = _REPLY_OWNER.search(before)
    if not m:
        return None
    head = before[:m.end()]
    q = re.search(r'"([^"\n]*)"\s*[:=]\s*$', head)
    key = q.group(1) if q else re.split(r"[\n{},]", head)[-1]
    return m.group(1) if len(_REPLY_IDTOK.findall(key)) == 1 else None


def read_score_reply(text: str, ids: list) -> dict:
    """{headline id: answer object} for every headline the reply answers unambiguously
    (rules above). Pure: no model, no database. Never raises."""
    try:
        ids = [str(i) for i in ids]
        t = _REPLY_NOBRACE.sub(r"\1{", _REPLY_FENCE.sub(" ", text or ""))
        dec = json.JSONDecoder()
        found, pos = [], 0
        while True:
            m = _REPLY_OPEN.search(t, pos)
            if not m:
                break
            s = m.start()
            try:
                val, end = dec.raw_decode(t, s)
            except ValueError:
                pos = s + 1            # not a whole JSON value here: try the next brace
                continue
            _reply_walk(val, _reply_owner(t[max(0, s - 120):s]), found)
            pos = end
        named = []                     # (headline it names or None, exact?, answer), reply order
        for owner, idf, obj in found:
            a, ax = _reply_id(owner, ids) if owner is not None else (None, False)
            b, bx = _reply_id(idf, ids) if idf is not None else (None, False)
            if a and b and a != b:
                a = b = None           # its key and its "id" field name different headlines
            named.append((a or b, ax or bx, obj))
        where = {i: n for n, i in enumerate(ids)}
        exact, near = {}, {}
        for p, (tgt, ex, obj) in enumerate(named):
            if not tgt:
                continue
            if not ex:
                # a mis-copied id counts only where the answers around it line up with
                # the headlines around it (the first answer: the first headline; the
                # last answer: the last headline). A skipped line (id of one headline
                # over the next one's answer) breaks that; the answer then goes nowhere.
                k = where[tgt]
                ok_before = (k == 0) if p == 0 else \
                    (named[p - 1][0] is not None and where[named[p - 1][0]] == k - 1)
                ok_after = (k == len(ids) - 1) if p == len(named) - 1 else \
                    (named[p + 1][0] is not None and where[named[p + 1][0]] == k + 1)
                if not (ok_before and ok_after):
                    continue
            (exact if ex else near).setdefault(tgt, []).append(obj)
        got = {}
        for i in ids:
            objs = exact.get(i) or (near[i] if len(near.get(i, ())) == 1 else None)
            if not objs:
                continue
            seen = {json.dumps([o.get("direction"), o.get("materiality"), o.get("market")],
                               default=str) for o in objs}
            if len(seen) == 1:         # two different answers for one headline: neither
                got[i] = objs[0]
        if len(ids) == 1 and not got and len(found) == 1:
            got[ids[0]] = found[0][2]  # one headline, one answer
        return got
    except Exception:
        return {}


def _read_reply(out, text: str, ids: list):
    """Today's parse `out`, with the tolerant reader filling in the headlines it left
    without an answer. Answers today's parse already gave are kept as they are.
    Returns (answers, how many the tolerant reader added)."""
    strict = out if isinstance(out, dict) else {}
    merged, n = dict(strict), 0
    if not all(isinstance(strict.get(i), dict) for i in ids):
        extra = read_score_reply(text, ids)
        for i in ids:
            if not isinstance(strict.get(i), dict) and isinstance(extra.get(i), dict):
                merged[i] = extra[i]
                n += 1
    for i in ids:
        o = merged.get(i)
        if isinstance(o, dict) and not isinstance(o.get("implication"), (str, type(None))):
            o = dict(o)
            o.pop("implication")       # a non-text implication would stop the whole batch
            merged[i] = o
    return merged, n


# ── Classify mode (2026-11-17, LOCAL_MODEL_STUDY P1): news.scoring.mode = "classify" ──
# The model stops giving numbers. For each NUMBERED headline it fills a small form:
#   kind — one label from _CLASSIFY_LABELS;
#   new  — true when the headline reports something that just happened or was just decided
#          at the company, false for notes, wraps, opinion, explainers, look-backs, previews;
#   dir  — up / down / none.
# Code turns the form into the stored numbers, reusing the catalyst triage floors
# (mason_triage.market_materiality, the same table and wording boosts the catalyst scorer uses):
#   market      = the floor of the event kind for this headline, with the source-tier weight;
#                 a new event of no listed kind gets at least classify_new_event_floor; a headline
#                 that is not new, or a commentary kind, is capped at the commentary ceiling
#                 (catalyst.triage.non_event_ceiling, 0.3);
#   materiality = market x classify_book_weight[category] (holding 1.0, watchlist 0.75, market 0.25);
#   direction   = up → bullish, down → bearish, anything else → neutral;
#   implication = empty (the model writes no prose in this mode).
# After that the classic rules still apply unchanged: the floor of a strong event on the book and
# the commentary ceiling (mason_triage.event_bounds). Keys of the reply are the headline numbers;
# an answer is never matched by its position in a list. "classic" (the default) is today's prompt.
_CLASSIFY_LABELS = (
    "results", "guidance", "merger_or_acquisition", "regulator_decision", "trial_data",
    "leadership_change", "share_offering", "contract_or_partnership", "investigation_or_lawsuit",
    "other_company_event", "analyst_note", "market_wrap", "opinion_or_explainer",
    "preview_or_schedule", "routine_notice", "promotion")
_WORLD_KINDS = ("macro_data", "central_bank", "geopolitical", "trade_policy", "commodity",
                "prediction_market", "other", "analyst_action")
# label → (triage kind it stands for, triage kinds the headline's own wording may refine it to,
#          is it a company event)
_CLASSIFY_MAP = {
    "results": ("earnings", ("earnings",), True),
    "guidance": ("guidance", ("guidance", "guidance_cut", "guidance_raise"), True),
    "merger_or_acquisition": ("mna", ("mna", "activist"), True),
    "regulator_decision": ("legal_regulatory", ("fda_decision", "adcom", "pdufa", "legal_regulatory",
                                                "investigation"), True),
    "trial_data": ("trial_readout", ("trial_readout", "trial_update"), True),
    "leadership_change": ("mgmt_change", ("mgmt_change",), True),
    "share_offering": ("other", (), True),
    "contract_or_partnership": ("deal_partnership", ("contract_award", "deal_partnership",
                                                     "contract_loss"), True),
    "investigation_or_lawsuit": ("legal_regulatory", ("investigation", "legal_regulatory",
                                                      "accounting"), True),
    "other_company_event": ("other", None, True),      # None: any company kind the wording shows
    "analyst_note": ("analyst_action", ("analyst_action",), False),
    "market_wrap": ("other", (), False),
    "opinion_or_explainer": ("other", (), False),
    "preview_or_schedule": ("other", (), False),
    "routine_notice": ("other", (), False),
    "promotion": ("other", (), False),
}
# a few spellings the model may use for a listed label
_CLASSIFY_SYNONYMS = {
    "earnings": "results", "merger": "merger_or_acquisition", "acquisition": "merger_or_acquisition",
    "m&a": "merger_or_acquisition", "mna": "merger_or_acquisition",
    "merger_acquisition": "merger_or_acquisition", "acquisition_or_merger": "merger_or_acquisition",
    "fda_decision": "regulator_decision", "regulatory_decision": "regulator_decision",
    "trial_results": "trial_data", "trial_readout": "trial_data",
    "management_change": "leadership_change", "offering": "share_offering",
    "contract": "contract_or_partnership", "partnership": "contract_or_partnership",
    "investigation": "investigation_or_lawsuit", "lawsuit": "investigation_or_lawsuit",
    "analyst_action": "analyst_note", "opinion": "opinion_or_explainer",
    "explainer": "opinion_or_explainer", "preview": "preview_or_schedule",
    "routine": "routine_notice",
}
# labels under which a government action aimed at the company may be filed (see classify_market)
_CLS_GOV_LABELS = ("regulator_decision", "investigation_or_lawsuit", "other_company_event")
# triage kinds a specific label already names; other_company_event "agrees" only with the rest
# (dividend cut, data breach, recall, halt, bankruptcy …), see _cls_agrees
_CLS_NAMED_KINDS = frozenset(k for v in _CLASSIFY_MAP.values() if v[1] for k in v[1])
_CLS_NOBRACE = re.compile(r'("\d{1,3}"\s*:\s*)(?="(?:kind|new|dir)"\s*:)')
# a key that names exactly ONE headline number: "1", "#1", "[1]", "1.", "1)", "item 1", "headline 1"
_CLS_NUMKEY = re.compile(r"^\s*(?:#|no\.?\s*|item\s*|headline\s*|\[)?\s*(\d{1,3})\s*[\].)]?\s*$", re.I)
_CLS_QKEY = re.compile(r'"([^"\n]*)"\s*[:=]\s*$')


def _cls_pairs(kv):
    """json object_pairs_hook (review 2026-11-18): a key written twice with DIFFERENT values
    ({"1": A, "1": B}) keeps neither (a marker with no answer in it); plain json keeps the
    last copy, so a model that repeats a number after skipping a line put the next
    headline's answer on this one."""
    d = {}
    for k, v in kv:
        d[k] = {"_clash": True} if (k in d and d[k] != v) else v
    return d


def _score_mode(sc: dict) -> str:
    """news.scoring.mode: 'classify' or 'classic' (anything else is classic)."""
    return "classify" if str((sc or {}).get("mode") or "").strip().lower() == "classify" else "classic"


def _classify_prompt(rows: list, tk) -> str:
    """The classify-mode prompt for these queue rows (numbered 1..n; tk(row) → 'NVDA,AMD')."""
    listing = "\n".join(f'{n}. [{tk(r) or "—"}] {r["headline"]}' for n, r in enumerate(rows, 1))
    return (
        "Hi! Here are some news headlines, numbered, each with its stock ticker in brackets. "
        "For each one, please tell me three things:\n"
        f"- kind: the one that fits best, copied exactly from this list: {', '.join(_CLASSIFY_LABELS)}\n"
        "- new: true if the headline reports a new, specific event at that company, something that "
        "just happened or was just decided. false for analyst notes, market wraps, opinion pieces, "
        "explainers, look-backs, previews, dates of future reports, routine dividend or buyback "
        "updates, and promotions.\n"
        "- dir: up, down or none, for which way it's likely to push that company's stock.\n\n"
        "Please reply with one JSON object, with the numbers as keys, like this:\n"
        '{"1":{"kind":"...","new":true,"dir":"up"},...}\n\n'
        f"{listing}\n\nJust the JSON, thanks!"
    )


def _cls_num(key):
    """The one headline number a key or number field names ("1", "#1", "[1]", 1 …) as text,
    else None. "1,2", "1 2", "items 1 and 2" name more than one, so they name none."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return str(key)
    m = _CLS_NUMKEY.match(key) if isinstance(key, str) else None
    return str(int(m.group(1))) if m else None


def _cls_index(key, n: int):
    """The headline number (1..n) a reply key or number field names, else None."""
    k = _cls_num(key)
    return int(k) if k is not None and 1 <= int(k) <= n else None


def _cls_owner(before: str):
    """The key written just before an object ("1": {   1: {   [1]: {   2. {   #3 - {), or None
    when there is none or it names anything but exactly one headline number (review fix,
    2026-11-17: "1,2": {  /  "1 2": {  /  items 1 and 2: {  used to land on headline 2)."""
    q = _CLS_QKEY.search(before)
    if q:
        key = q.group(1)
    else:                              # the label on this line, after the last brace
        key = re.split(r"[\n{}]", before)[-1]
        key = re.sub(r"[\s:=\-]+$", "", key).strip().lstrip(",").strip().strip("'\"")
    return key if _cls_num(key) is not None else None


def _cls_num_field(v: dict):
    """The headline number an answer carries in its own fields, if any."""
    for f in ("n", "num", "number", "no", "item", "index", "id"):
        if f in v:
            return v.get(f)
    return None


def _cls_walk(v, owner, found: list, depth: int = 0, anc=None) -> None:
    """Collect (key it sat under, its number field, answer) for every answer in v. `anc` is the
    number key above it; an answer under two DIFFERENT number keys ({"1": {"2": {...}}}), or
    numbered differently from the key above it, names neither headline (review fix 2026-11-17)
    but still counts as an answer (so a one-headline batch does not take a lone answer)."""
    if depth > 4:
        return
    if isinstance(v, list):
        for x in v:
            _cls_walk(x, owner if len(v) == 1 else None, found, depth + 1, anc)
    elif isinstance(v, dict):
        if "kind" in v or "new" in v:
            nf = _cls_num_field(v)
            nn = _cls_num(nf) if nf is not None else None
            if anc == "?" or (anc and nn and nn != anc):
                found.append(("?", "?", v))
            else:
                found.append((owner, nf, v))
        else:
            for k, x in v.items():
                if isinstance(x, (dict, list)):
                    kn = _cls_num(k)
                    a = anc if kn is None else (kn if anc in (None, kn) else "?")
                    _cls_walk(x, k, found, depth + 1, a)


def read_classify_reply(text: str, n: int, tolerant: bool = True) -> dict:
    """{headline number (1-based): answer object} from a classify-mode reply. Strict: one
    JSON object keyed "1".."n". Tolerant (news.scoring.tolerant_reader) also takes fences and
    prose, several objects, a list of answers carrying their number, "1: {...}" lines, a brace
    left out, and a lone answer for a one-headline batch; never an answer by its position in
    a list, never two different answers for one number. Never raises."""
    got = {}
    try:
        t0 = (text or "").strip()
        if t0.startswith("```"):
            t0 = t0.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            strict = json.loads(t0, object_pairs_hook=_cls_pairs)
        except Exception:
            strict = None
        if isinstance(strict, dict):
            twice = set()
            for k, v in strict.items():
                i = _cls_index(k, n)
                if i and isinstance(v, dict) and ("kind" in v or "new" in v):
                    f = _cls_num_field(v)
                    if f is not None and _cls_index(f, n) not in (None, i):
                        continue           # its key and its own number field disagree
                    if i in twice or (i in got and got[i] != v):
                        got.pop(i, None)   # "1" and "#1" with different answers: neither (review 2026-11-18)
                        twice.add(i)
                        continue
                    got[i] = v
        if not tolerant or len(got) == n:
            return got
        t = _CLS_NOBRACE.sub(r"\1{", _REPLY_FENCE.sub(" ", text or ""))
        dec = json.JSONDecoder(object_pairs_hook=_cls_pairs)
        found, pos = [], 0
        while True:
            m = _REPLY_OPEN.search(t, pos)
            if not m:
                break
            s = m.start()
            try:
                val, end = dec.raw_decode(t, s)
            except ValueError:
                pos = s + 1
                continue
            ow = _cls_owner(t[max(0, s - 120):s])
            _cls_walk(val, ow, found, 0, _cls_num(ow) if ow is not None else None)
            pos = end
        by = {}
        for owner, num, obj in found:
            a = _cls_index(owner, n) if owner is not None else None
            b = _cls_index(num, n) if num is not None else None
            if a and b and a != b:
                continue
            if a or b:
                by.setdefault(a or b, []).append(obj)
        for i, objs in by.items():
            if i in got:
                continue
            seen = {json.dumps([o.get("kind"), o.get("new"), o.get("dir")], default=str) for o in objs}
            if len(seen) == 1:
                got[i] = objs[0]
        if n == 1 and not got and len(found) == 1:
            got[1] = found[0][2]
        return got
    except Exception:
        return got


def _cls_label(kind) -> str:
    k = re.sub(r"[\s\-/]+", "_", str(kind or "").strip().lower())
    return k if k in _CLASSIFY_MAP else _CLASSIFY_SYNONYMS.get(k, "")


def _cls_new(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower() if v is not None else ""
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def _cls_dir(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("up", "bullish", "positive", "higher"):
        return "bullish"
    if s in ("down", "bearish", "negative", "lower"):
        return "bearish"
    return "neutral"


def classify_market(label: str, is_new, headline: str, tier: int = 2, hint=None, sc: dict = None):
    """(triage kind, market score 0..1) for one classified headline — deterministic.
    Two rules (adjusted after the 17 Nov replay):
      * a NEW company event scores at least classify_new_event_floor (0.4, the inbox bar for a
        held name), whatever its kind; anything not new is capped at the commentary ceiling (0.3);
      * a kind whose own floor is above that (merger, trial data, leadership, guidance, an FDA
        decision) counts only when the headline's own words show it (the catalyst triage word
        rules agree); otherwise it is a new event of no listed kind. The model decides WHETHER
        it is news; the headline's words decide how big."""
    sc = sc or {}
    kind, alts, event = _CLASSIFY_MAP.get(label) or ("other", (), False)
    new_event = bool(event and is_new is True)
    try:
        floor_new = float(sc.get("classify_new_event_floor", 0.4))
    except Exception:
        floor_new = 0.4
    try:
        import mason_triage as T
        if new_event:
            # the headline's own wording names the event more exactly (and with its boosts)
            try:
                rk = T.classify_kind(headline or "", hint, has_ticker=True)
            except Exception:
                rk = None
            if rk in ("trade_policy", "geopolitical") and label in _CLS_GOV_LABELS \
                    and T._CO_RESTRICT_RE.search(T._clean(headline or "")):
                # review 2026-11-18: a government action aimed at this company (entity list,
                # export ban, sanctions) keeps the triage's own number for it (0.7), as the
                # catalyst scorer does; before, the label's kind list dropped it to 0.4
                kind = rk
            elif alts is None:                 # other_company_event: any company kind the wording shows
                kind = rk if (rk and rk in T.EVENT_KINDS and rk not in _WORLD_KINDS) else "other"
            elif rk in alts:
                kind = rk
            elif label == "regulator_decision" and T._FDA_CONTEXT_RE.search(T._clean(headline or "")):
                kind = "fda_decision"
            elif T.market_materiality(kind, "", tier=2, has_ticker=True, _raw_floor=True) > floor_new:
                kind = "other"                 # a big kind the headline's words do not show
        m = T.market_materiality(kind, headline or "", tier=tier, has_ticker=True, _raw_floor=True)
        ceiling = float(T._cfg().get("non_event_ceiling", 0.3))
    except Exception:
        m, ceiling = (floor_new if new_event else 0.1), 0.3
    if new_event:
        m = max(m, floor_new)
    else:
        m = min(m, ceiling)
    return kind, round(max(0.0, min(1.0, m)), 3)


# (review 2026-11-18) "X to acquire / to buy / agrees to acquire / acquisition of / takeover of <company>"
_CLS_BUY_RE = re.compile(r"\b(?:(?:agrees?|agreed|plans?|moves?|offers?|is set|set|bids?|seeks?) to "
                         r"(?:acquire|buy|take over)|to acquire|to buy|to take over|acquires|buys|takes over|"
                         r"(?:acquisition|takeover|buyout) of|(?:bid|offer) for)\b", re.I)
# what follows the company's name when only a piece of it is bought ("Jazz's sleep unit", "a stake in")
_CLS_PART_RE = re.compile(r"['’]s?(?:\s|$)|\b(?:stakes?|interest|assets?|portfolio|units?|divisions?|"
                          r"business(?:es)?|brands?|plants?|facilit\w+|stores?|rights|programs?|operations|"
                          r"segment|subsidiar\w+|holdings?)\b", re.I)


def _cls_target_side(headline: str, tickers: list, category: str) -> bool:
    """True when the book company of this row is the one BEING BOUGHT: it is named only after a
    buying verb, within two plain words of it ("Novartis to acquire Jazz Pharmaceuticals for
    $150 per share", "… to buy rival Jazz …"), and what follows its name is not a piece of it
    (a unit, a stake, assets, "Jazz's …"). The triage word rules give the target side its
    high number only for "to be acquired by" / "agrees to buy X for $"; "X to acquire <us>"
    reads as the buyer's side there (0.5). Never raises; False when unsure."""
    try:
        import mason_news_quality as NQ
        t = NQ._norm(headline or "")
        v = _CLS_BUY_RE.search(t)
        if not v or not tickers:
            return False
        tks = [str(x).upper() for x in tickers if x]
        if category in ("holding", "watchlist"):
            try:
                book = NQ.held_set() if category == "holding" else NQ.book_set()
            except Exception:
                book = set()
            tks = [x for x in tks if x in book] or tks[:1]
        else:
            tks = tks[:1]
        hit = False
        for tk in tks:
            sp = NQ.mention_spans(tk, t)
            if not sp:
                continue
            if any(a < v.start() for a, _ in sp):
                return False                    # also named before the verb: it may be the buyer
            a, b = sp[0]
            gap = t[v.end():a]
            if len(gap.split()) > 2 or re.search(r"[\d$%€£]|\bfrom\b", gap, re.I):
                return False
            after = re.split(r"\s(?:for|in|at|after|amid|as|with|valu\w+)\s|[,;:(]", t[b:] + " ", maxsplit=1)[0]
            if _CLS_PART_RE.search(after):
                return False
            hit = True
        return hit
    except Exception:
        return False


def _cls_agrees(label: str, kind) -> bool:
    """True when the model's label names the same kind of event the headline's own words show
    (mason_triage kind): leadership_change ↔ mgmt_change, investigation_or_lawsuit ↔
    investigation, … other_company_event agrees only with kinds no specific label names
    (dividend cut, data breach, product recall, halt, bankruptcy)."""
    m = _CLASSIFY_MAP.get(label or "")
    if not m or not m[2] or not kind:
        return False
    return (kind not in _CLS_NAMED_KINDS) if m[1] is None else (kind in m[1])


def _classify_answers(text: str, rows: list, sc: dict, bounds: dict = None) -> dict:
    """{row id: an answer in the classic shape (direction, materiality, market, implication)
    plus "_class" (the form as the model filled it, for the record)} for the numbered rows
    the reply answered. `bounds` = {row id: mason_triage.event_bounds} (as _score_batch has them).

    Held-name agreement (review 2026-11-18, news.scoring.classify_held_agree_floor, 0.7): on a
    stock Nico HOLDS, a strong event whose own words already rate it at that floor or more
    (a regulator / FBI / SEC probe opened, Phase 3 data, a guidance cut, a finance chief
    leaving, a dividend cut, a major contract lost) and that the model ALSO calls a new event
    of the same kind gets classify_held_agree_score (0.8, the Breaking line). Two independent
    signals must agree; either alone does not lift. null = off."""
    got = read_classify_reply(text, len(rows), tolerant=bool(sc.get("tolerant_reader", True)))
    weights = sc.get("classify_book_weight")
    if not isinstance(weights, dict):
        weights = {}
    try:
        agree_floor = sc.get("classify_held_agree_floor", 0.7)
        agree_floor = None if agree_floor is None or isinstance(agree_floor, bool) else float(agree_floor)
        agree_score = max(0.0, min(1.0, float(sc.get("classify_held_agree_score", 0.8))))
    except Exception:
        agree_floor, agree_score = 0.7, 0.8      # a setting that is not a number: the built-in rule
    bounds = bounds if isinstance(bounds, dict) else {}
    out = {}
    for i, r in enumerate(rows, 1):
        o = got.get(i)
        if not isinstance(o, dict):
            continue
        label, is_new = _cls_label(o.get("kind")), _cls_new(o.get("new"))
        try:
            tier = int(r["source_tier"] or 2)
        except Exception:
            tier = 2
        kind, market = classify_market(label, is_new, r["headline"] or "", tier,
                                       _row_raw(r).get("event_kind_hint"), sc)
        target = False
        if label == "merger_or_acquisition" and is_new is True and kind in ("mna", "other"):
            try:
                tks = json.loads(r["tickers"] or "[]")
            except Exception:
                tks = []
            if _cls_target_side(r["headline"] or "", tks if isinstance(tks, list) else [], r["category"]):
                try:
                    import mason_triage as T
                    tm = T.market_materiality("mna", "to be acquired by", tier=tier, has_ticker=True,
                                              _raw_floor=True)       # the triage's own target-side number
                except Exception:
                    tm = 0.0
                if tm > market:
                    kind, market, target = "mna", round(tm, 3), True
        w = {"holding": 1.0, "watchlist": 0.75}.get(r["category"], 0.25)
        try:
            if r["category"] in weights:
                w = max(0.0, min(1.0, float(weights[r["category"]])))
        except Exception:
            pass                       # a setting that is not a number: keep the built-in weight
        mat = round(market * w, 3)
        b = bounds.get(r["id"]) or {}
        agreed = False
        try:
            if agree_floor is not None and r["category"] == "holding" and is_new is True \
                    and b.get("strong") and float(b.get("floor") or 0) >= agree_floor \
                    and _cls_agrees(label, b.get("kind")) and mat < agree_score:
                mat, agreed = round(agree_score, 3), True
        except Exception:
            pass
        out[r["id"]] = {
            "direction": _cls_dir(o.get("dir")), "materiality": mat,
            "market": market, "implication": None,
            "_class": {"kind": str(o.get("kind"))[:40] if o.get("kind") is not None else None,
                       "label": label or None, "new": is_new,
                       "dir": str(o.get("dir"))[:10] if o.get("dir") is not None else None,
                       "event_kind": kind, "market": market},
        }
        if agreed:
            out[r["id"]]["_class"]["held_agree"] = b.get("kind")
        if target:
            out[r["id"]]["_class"]["target"] = True
    return out


def _score_batch(conn, batch: list, mason_llm, _tri: bool, book: str, sc: dict):
    """One model call for one batch of queue items. Returns (scored, material) or
    None when the model is unavailable."""
    rows = [b[0] for b in batch]
    bounds = {b[0]["id"]: (b[1] or {}) for b in batch}

    def _tk(r):
        try:
            return ",".join(json.loads(r["tickers"]) or [])
        except Exception:
            return ""

    listing = "\n".join(f'{r["id"]}: [{_tk(r) or "—"}] {r["headline"]}' for r in rows)
    if not _tri:
        prompt = (
            f"Nico's book / watchlist: {book}.\n\n"
            f"For each headline below, judge the impact ON HIS POSITIONS. Reply with "
            f"STRICT JSON mapping each id to an object:\n"
            f'  {{"direction":"bullish|bearish|neutral","materiality":<0.0-1.0>,'
            f'"implication":"<=14 words, concrete effect on his book, or empty"}}\n'
            f"materiality = how much this should move his book (0 none, 1 definitive). "
            f"Use neutral + low materiality for headlines that don't really touch him.\n\n"
            f"{listing}\n\nJSON only, no prose."
        )
    else:
        prompt = (
            f"Nico's book / watchlist: {book}.\n\n"
            f"For each headline below, judge the impact ON HIS POSITIONS, and separately "
            f"how much it moves the MARKET for the named stock/sector. Reply with "
            f"STRICT JSON mapping each id to an object:\n"
            f'  {{"direction":"bullish|bearish|neutral","materiality":<0.0-1.0>,'
            f'"market":<0.0-1.0>,'
            f'"implication":"<=14 words, concrete effect on his book, or empty"}}\n'
            f"materiality = how much this should move his book (0 none, 1 definitive). "
            f"Use neutral + low materiality for headlines that don't really touch him.\n"
            f"market = how much this should move the named stock or sector itself, for "
            f"any holder, regardless of his book (0 noise, 1 major repricing).\n\n"
            f"{listing}\n\nJSON only, no prose."
        )
    classify = _score_mode(sc) == "classify"
    npred = (800 if _tri else 600)
    if classify:                       # news.scoring.mode = "classify": a form per headline, no numbers
        prompt = _classify_prompt(rows, _tk)
        npred = int(sc.get("classify_num_predict", 400))
    floors_on = bool(sc.get("floors_enabled", True))
    try:
        raw = mason_llm.complete("", prompt, intent="chore",
                                 num_predict=npred).strip()
    except Exception:
        # qwen down (LocalModelError) or any other failure — leave unscored and retry
        # next pass … except a STRONG event on the book that has already waited
        # news.scoring.fallback_after_minutes: it must not sit invisible for as long
        # as the model is down, so it gets its deterministic floor.
        n = 0
        if floors_on and sc.get("deterministic_fallback", True):
            waited = time.time() - float(sc.get("fallback_after_minutes", 10)) * 60
            try:
                with conn:
                    for r in rows:
                        b = bounds.get(r["id"]) or {}
                        if not b.get("strong") or r["category"] not in ("holding", "watchlist"):
                            continue
                        if (r["ingested_at"] or 0) > waited:
                            continue
                        robj = _row_raw(r)
                        robj["scored_by"] = "deterministic (local model unavailable)"
                        robj["mat_rule"] = f"floor:{b.get('kind')}"
                        conn.execute(
                            "UPDATE news_events SET direction=?, materiality=?, implication=NULL, "
                            "scored=1, raw=? WHERE id=? AND scored=0",
                            (_deterministic_direction(r["headline"] or ""), float(b.get("floor") or 0),
                             json.dumps(robj), r["id"]))
                        n += 1
            except Exception:
                n = 0
            if n:
                log.warning(f"[news-score] local model unavailable — {n} strong event(s) scored at their floor")
        return None
    reply_text = raw
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        out = json.loads(raw)
    except Exception:
        out = {}
    if classify:
        out = _classify_answers(reply_text, rows, sc, bounds)
    elif sc.get("tolerant_reader", True):
        out, n_read = _read_reply(out, reply_text, [r["id"] for r in rows])
        if n_read:
            log.info(f"[news-score] tolerant reader: {n_read} of {len(rows)} answers read "
                     f"from a reply in another shape")

    retry_missing = bool(sc.get("retry_missing", True))
    max_attempts = max(1, int(sc.get("max_attempts", 2)))
    # The answer cost a model call; a database that is briefly locked by another writer
    # must not throw it away. The whole write is one transaction, so a failed try rolls
    # back and is simply done again (news.scoring.write_retries, a short pause between).
    tries = max(1, int(sc.get("write_retries", 4)))
    for attempt in range(tries):
        n = 0
        price_needed = []   # (id, primary_ticker) for material directional events
        try:
            with conn:
                for r in rows:
                    o = out.get(r["id"]) if isinstance(out, dict) else None
                    b = bounds.get(r["id"]) or {}
                    robj = None
                    missing = not isinstance(o, dict)
                    if missing and retry_missing:
                        # the reply left this id out (or was not JSON): that is "no answer",
                        # not "materiality 0.0". Leave it unscored for another try; after
                        # max_attempts fall through to the deterministic bounds.
                        robj = _row_raw(r)
                        attempts = int(robj.get("score_attempts") or 0) + 1
                        robj["score_attempts"] = attempts
                        if attempts < max_attempts:
                            conn.execute("UPDATE news_events SET raw=? WHERE id=?", (json.dumps(robj), r["id"]))
                            continue
                        robj["scored_by"] = "deterministic (no answer from the local model)"
                    o = o if isinstance(o, dict) else {}
                    direction = o.get("direction")
                    if direction not in ("bullish", "bearish", "neutral"):
                        direction = "neutral"
                    try:
                        mat = max(0.0, min(1.0, float(o.get("materiality", 0))))
                    except Exception:
                        mat = 0.0
                    impl = (o.get("implication") or "").strip() or None
                    if floors_on and b:
                        final, rule = mat, None
                        if b.get("ceiling") is not None:
                            if mat > float(b["ceiling"]):
                                final, rule = float(b["ceiling"]), f"ceiling:{b.get('non_event')}"
                        elif b.get("strong") and r["category"] in ("holding", "watchlist") \
                                and mat < float(b.get("floor") or 0):
                            final, rule = float(b["floor"]), f"floor:{b.get('kind')}"
                            if direction == "neutral":
                                direction = _deterministic_direction(r["headline"] or "")
                        if rule:
                            robj = robj if robj is not None else _row_raw(r)
                            robj["qwen_materiality"] = round(mat, 3)      # what the model said, kept for audit
                            robj["mat_rule"] = rule
                            mat = round(final, 3)
                    if classify and isinstance(o.get("_class"), dict):
                        # the form the model filled and what code made of it, for the record
                        robj = robj if robj is not None else _row_raw(r)
                        robj["score_mode"] = "classify"
                        robj["qwen_class"] = o["_class"]
                    # mark scored=1 regardless (even neutral) so we don't reprocess it
                    conn.execute(
                        "UPDATE news_events SET direction=?, materiality=?, implication=?, "
                        "scored=1 WHERE id=?", (direction, mat, impl, r["id"]))
                    n += 1
                    if robj is not None:
                        conn.execute("UPDATE news_events SET raw=? WHERE id=?", (json.dumps(robj), r["id"]))
                    if _tri and o.get("market") is not None:
                        # keep qwen's MARKET read beside the book one; triaged=0 makes
                        # mason_triage re-evaluate this event with it (idempotent —
                        # the event stays on its catalyst)
                        try:
                            qm = max(0.0, min(1.0, float(o.get("market"))))
                            cur = conn.execute("SELECT raw FROM news_events WHERE id=?",
                                               (r["id"],)).fetchone()
                            robj2 = json.loads(cur["raw"]) if cur and cur["raw"] else {}
                            if not isinstance(robj2, dict):
                                robj2 = {}
                            robj2["qwen_market"] = round(qm, 3)
                            conn.execute("UPDATE news_events SET raw=?, triaged=0 WHERE id=?",
                                         (json.dumps(robj2), r["id"]))
                        except Exception:
                            pass
                    if direction in ("bullish", "bearish") and mat >= _MATERIAL_THRESHOLD:
                        try:
                            tks = json.loads(r["tickers"]) or []
                        except Exception:
                            tks = []
                        if tks:
                            price_needed.append((r["id"], tks[0]))
            break
        except Exception as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if attempt + 1 >= tries:
                log.warning(f"[news-score] database stayed locked for {tries} tries; "
                            f"{len(rows)} scored headlines will be asked again")
                return 0, 0
            time.sleep(float(sc.get("write_retry_seconds", 1.5)) * (attempt + 1))
    # Capture the primary ticker's price NOW as the calibration baseline
    # (outside the write txn — this does network I/O). Best-effort.
    for eid, tk in price_needed:
        px = _last_price(tk)
        if px:
            try:
                with conn:
                    conn.execute("UPDATE news_events SET entry_price=? WHERE id=?", (px, eid))
            except Exception:
                pass
    return n, len(price_needed)


# ── macro / world scoring (deterministic, cheap — the "world sensor") ────────────
# The desk is a world sensor, not just a book watcher: macro/market-wide news is
# ingested but never touched Nico's book, so score_pending() skips it. This pass
# gives EVERY macro event a cheap DETERMINISTIC materiality (no qwen, no network),
# so the analyst desk can build an hourly "World Macro" digest + flag breaking macro
# from the material minority. Book queries are unaffected (they all filter
# category IN ('holding','watchlist')); macro still prunes on the normal window.
# Behind analyst_desk.macro.enabled (default OFF → this whole pass is a no-op).

_MACRO_HI = (
    "fed", "fomc", "rate cut", "rate hike", "interest rate", "cpi", "inflation",
    "jobs report", "nonfarm", "payrolls", "unemployment", "recession", "gdp",
    "war", "invasion", "missile", "strike", "tariff", "sanction", "embargo",
    "default", "downgrade", "credit rating", "crash", "circuit breaker",
    "shutdown", "bank failure", "bankruptcy", "opec", "crude", "oil price",
    "treasury yield", "10-year", "the dollar", "china", "taiwan", "russia",
    "iran", "election", "powell", "ecb", "boj", "pboc",
)
_MACRO_MED = (
    "s&p", "nasdaq", "dow", "russell", "futures", "bitcoin", "ethereum", "crypto",
    "gold", "silver", "yields", "bonds", "housing", "retail sales", "pmi",
    "consumer", "earnings season", "vix", "volatility", "sector", "commodities",
)


def _macro_score(headline: str) -> tuple:
    """(materiality 0..1, direction) for a macro headline — deterministic. Base +
    weighted keyword hits; direction from the shared red/green vocabulary."""
    text = (headline or "").lower()
    score = 0.25
    score += 0.22 * sum(1 for k in _MACRO_HI if k in text)
    score += 0.10 * sum(1 for k in _MACRO_MED if k in text)
    score = max(0.0, min(1.0, score))
    direction = "neutral"
    try:
        import mason_news
        if mason_news._RED.search(headline or ""):
            direction = "bearish"
        elif mason_news._GREEN.search(headline or ""):
            direction = "bullish"
    except Exception:
        pass
    return round(score, 3), direction


def _macro_enabled() -> bool:
    try:
        cfg = json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
        return bool(cfg.get("analyst_desk", {}).get("macro", {}).get("enabled", False))
    except Exception:
        return False


def score_macro_pending(cap: int = 250, hours: int = 24) -> int:
    """Deterministically score unscored macro events from the last `hours`. Sets
    materiality + direction + scored=1 (no qwen). Behind analyst_desk.macro.enabled;
    a no-op when off. Never raises. Returns the number scored."""
    if not _macro_enabled():
        return 0
    conn = mason_db.get_connection()
    cutoff = time.time() - hours * 3600
    try:
        rows = conn.execute(
            "SELECT id, headline FROM news_events WHERE scored=0 AND category='macro' "
            "AND ingested_at > ? ORDER BY ingested_at DESC LIMIT ?", (cutoff, cap)).fetchall()
        if not rows:
            return 0
        n = 0
        with conn:
            for r in rows:
                mat, direction = _macro_score(r["headline"] or "")
                conn.execute(
                    "UPDATE news_events SET materiality=?, direction=?, scored=1 WHERE id=?",
                    (mat, direction, r["id"]))
                n += 1
        if n:
            log.info(f"[news-score] macro-scored {n} events (deterministic)")
        return n
    except Exception as e:
        log.warning(f"[news-score] macro scoring failed: {e}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── calibration loop (NEWS_SERVICE_DESIGN.md Phase 4) ──────────────────────────
# Did a material directional call actually play out? For scored events with a
# captured entry_price, once they're old enough, compare the primary ticker's
# price now vs entry: a bullish call is "correct" if it rose (bearish if it
# fell), by more than a small dead-band. Tally per-source hits/misses so future
# materiality can eventually be weighted by which sources actually move the tape.
_CALIB_MIN_AGE_H = 4       # give the move time to develop
_CALIB_MAX_AGE_H = 72      # ...but don't chase week-old news
_CALIB_DEADBAND = 0.005    # ±0.5% counts as "no move" (neutral, skip)


def calibrate(cap: int = 15) -> int:
    """Resolve outcomes for aged material directional events. Returns count
    calibrated. Never raises; needs live price data so it's a no-op off-hours."""
    conn = mason_db.get_connection()
    now = time.time()
    lo, hi = now - _CALIB_MAX_AGE_H * 3600, now - _CALIB_MIN_AGE_H * 3600
    try:
        rows = conn.execute(
            "SELECT id, source, tickers, direction, entry_price FROM news_events "
            "WHERE calibrated=0 AND scored=1 AND entry_price IS NOT NULL "
            "AND direction IN ('bullish','bearish') "
            "AND ingested_at BETWEEN ? AND ? ORDER BY ingested_at ASC LIMIT ?",
            (lo, hi, cap),
        ).fetchall()
        if not rows:
            return 0
        n = 0
        for r in rows:
            try:
                tks = json.loads(r["tickers"]) or []
            except Exception:
                tks = []
            if not tks:
                with conn:
                    conn.execute("UPDATE news_events SET calibrated=1 WHERE id=?", (r["id"],))
                continue
            px = _last_price(tks[0])
            entry = r["entry_price"]
            if not px or not entry:
                continue          # leave for a later pass when price is available
            chg = (px - entry) / entry
            if abs(chg) < _CALIB_DEADBAND:
                correct = None    # no real move — don't credit or penalize
            else:
                up = chg > 0
                correct = (up and r["direction"] == "bullish") or \
                          (not up and r["direction"] == "bearish")
            with conn:
                conn.execute("UPDATE news_events SET calibrated=1 WHERE id=?", (r["id"],))
                if correct is not None and r["source"]:
                    # Tally on the source's feed rows. Approximate: RSS sources
                    # span several feed_urls (each gets +1) and Alpaca/Benzinga
                    # has no news_sources row (not tallied). Fine as a raw signal
                    # — nothing weights on it yet; refine when weighting lands.
                    col = "hits" if correct else "misses"
                    conn.execute(
                        f"UPDATE news_sources SET {col}={col}+1, updated_at=? "
                        "WHERE source=?", (now, r["source"]))
            n += 1
        if n:
            log.info(f"[news-calib] resolved {n} events")
        return n
    except Exception as e:
        log.warning(f"[news-calib] failed: {e}")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── background scanner ─────────────────────────────────────────────────────────

_scanner_started = False
_scanner_lock = threading.Lock()


_last_universe_refresh = 0.0


def _maybe_refresh_universe():
    """Opt-in (analyst_desk.factcheck.universe_refresh.enabled) background rebuild of
    the ticker universe. Runs once on boot then every interval_hours, off-thread so it
    never blocks scanning. No-op + silent when the flag is off. Never raises."""
    global _last_universe_refresh
    try:
        import mason_factcheck as FC
        if not FC.universe_refresh_enabled():
            return
        hours = FC._fc_get(None, ["universe_refresh", "interval_hours"], 24) or 24
        if (time.time() - _last_universe_refresh) < float(hours) * 3600:
            return
        _last_universe_refresh = time.time()

        def _do():
            try:
                FC.refresh_ticker_universe()
            except Exception:
                log.exception("[universe] refresh failed")
        threading.Thread(target=_do, daemon=True, name="universe-refresh").start()
    except Exception:
        log.exception("[universe] refresh scheduling failed")


# The scanner/scorer loops beat the runtime supervisor themselves (mason_runtime then skips its outside hooks).
_RUNTIME_BEATS = True


def _rt_beat(name: str, busy_with: str | None = None) -> None:
    """Heartbeat for the runtime supervisor (mason_runtime, audit O3). Never raises."""
    try:
        import mason_runtime
        if busy_with:
            mason_runtime.busy(name, busy_with)
        else:
            mason_runtime.beat(name)
    except Exception:
        pass


def _scanner_step_failed(step: str, e: Exception) -> None:
    # Audit O3: these steps used to fail silently ("except: pass"); now each failure leaves one log line.
    log.warning(f"[news-scan] {step} failed: {type(e).__name__}: {e}")


def _scanner_loop(interval: int):
    # first pass immediately so the table fills on boot, then every `interval`s
    passes = 0
    while True:
        _rt_beat("news-scanner", "feed scan")
        _maybe_refresh_universe()   # opt-in; once on boot then every interval_hours
        scan_once()
        try:
            if not (_scorer_started and _ncfg("scoring", "dedicated_thread", True)):
                _rt_beat("news-scanner", "scoring")
                score_pending()        # qwen scoring pass (the scorer thread does it when on)
        except Exception as e:
            _scanner_step_failed("scoring", e)
        try:
            score_macro_pending()      # cheap deterministic pass over new macro events (flag-gated)
        except Exception as e:
            _scanner_step_failed("macro scoring", e)
        try:                           # Catalyst Desk triage (catalyst.triage.enabled; no-op when off)
            _rt_beat("news-scanner", "triage")
            import mason_triage
            mason_triage.run_triage()
        except Exception as e:
            _scanner_step_failed("triage", e)
        try:                           # memory spine: match new news to open theses (memory.theses.enabled)
            _rt_beat("news-scanner", "thesis matching")
            import mason_theses
            mason_theses.match_new_events()
        except Exception as e:
            _scanner_step_failed("thesis matching", e)
        passes += 1
        if passes % 10 == 0:           # ~every 20 min: resolve aged material calls
            try:
                calibrate()
            except Exception as e:
                _scanner_step_failed("calibration", e)
        _rt_beat("news-scanner")
        time.sleep(max(30, interval))


_scorer_started = False


def _scorer_loop():
    """Drain the scoring queue on its own thread, so a burst of headlines is scored
    in minutes without holding up the feed scanner. One pass = up to
    news.scoring.max_per_pass headlines inside news.scoring.pass_budget_seconds;
    it backs off while an interactive chat is using the local model and idles for
    news.scoring.idle_sleep_seconds when the queue is empty. With
    news.scoring.dedicated_thread=false it does nothing and the scanner loop scores
    once per pass, as before."""
    while True:
        _rt_beat("news-scorer")
        delay = 20.0
        try:
            sc = _ncfg("scoring")
            busy = False
            if not sc.get("dedicated_thread", True):
                delay = 30.0
            else:
                try:
                    import mason_llm
                    busy = bool(mason_llm.interactive_busy())
                except Exception:
                    busy = False
                if busy:
                    delay = 5.0                    # the user is chatting: leave the GPU alone
                else:
                    n = score_pending()
                    delay = (max(0.2, float(sc.get("min_gap_seconds", 1.0))) if n
                             else max(2.0, float(sc.get("idle_sleep_seconds", 20))))
        except Exception as e:
            log.warning(f"[news-score] scorer pass failed: {e}")
            delay = 20.0
        time.sleep(delay)


def start_scanner(interval: int = 120) -> bool:
    """Start the daemon scanner once (idempotent). Returns True if it started
    this call, False if it was already running. Also starts the scoring thread
    (news.scoring.dedicated_thread, default on)."""
    global _scanner_started, _scorer_started
    with _scanner_lock:
        if _scanner_started:
            return False
        _scanner_started = True
    threading.Thread(target=_scanner_loop, args=(interval,),
                     daemon=True, name="news-scanner").start()
    log.info(f"[news-scan] scanner started (every {interval}s)")
    try:
        if _ncfg("scoring", "dedicated_thread", True):
            _scorer_started = True
            threading.Thread(target=_scorer_loop, daemon=True, name="news-scorer").start()
            log.info("[news-score] scorer thread started")
    except Exception:
        _scorer_started = False
    return True


# ── Alpaca / Benzinga real-time news WebSocket (NEWS_SERVICE_DESIGN.md Phase 2) ──
# Pushes ticker-tagged Benzinga headlines the instant they publish (sub-second),
# using the market-data credentials Mason already holds. Writes into the same
# news_events table as the RSS scanner, so /api/news surfaces them with zero
# extra work. Source label "Alpaca/Benzinga", tier 1. Degrades gracefully: no
# creds -> skip; disconnect -> reconnect with exponential backoff; never raises
# out of the daemon thread.

_ALPACA_NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"
_alpaca_started = False
_alpaca_lock = threading.Lock()
# Liveness beacon for the Source-Health Monitor (ANALYST_DESK_PHASE1). Bumped to
# the epoch of the last news frame handled off the WS. Read via ws_status(); the
# health check never opens a second socket. Additive + backward-compatible.
_alpaca_last_msg_ts = 0.0


def ws_status() -> dict:
    """Snapshot of the Alpaca/Benzinga news WebSocket for the health monitor:
    whether the listener thread is running and the epoch of the last frame seen.
    Read-only, no network. Returns safe defaults if the WS never started."""
    return {"started": bool(_alpaca_started), "last_msg_ts": float(_alpaca_last_msg_ts)}


def _alpaca_creds():
    """(key, secret) from mason.config.json paper_trading block — same source as
    mason_app._alpaca_keys(). Read directly to avoid importing mason_app (which
    imports this module)."""
    try:
        cfg = json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    try:
        import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
        return _S.alpaca_keys(cfg=cfg)
    except Exception:
        return "", ""


_ws_pool_state = {"pool": None, "built": 0.0, "conn": None}


def _ws_pool(conn, max_age: float = 60.0) -> "_DedupPool":
    """The stream's near-duplicate pool: rebuilt from the DB at most once a minute
    (so it sees what the RSS scanner stored), extended in place in between — a
    burst of 500 headlines no longer costs 500 pool rebuilds."""
    st = _ws_pool_state
    now = time.time()
    if st["pool"] is None or st["conn"] is not conn or (now - st["built"]) > max_age:
        st.update(pool=_recent_pool(conn), built=now, conn=conn)
    return st["pool"]


def _insert_alpaca_item(conn, msg, universe, holds, backfill: bool = False) -> bool:
    """One Alpaca news item (stream frame or REST row) → a news_events row.
    `backfill=True` (read back after a disconnect): the row is marked raw.backfill
    with its age on arrival; one older than news.alpaca_backfill.no_alert_minutes is
    stored under its ORIGINAL publish time — not stamped as new — and triage will
    not let it alert by itself."""
    headline = (msg.get("headline") or "").strip()
    if not headline:
        return False
    pub = _parse_pubdate(msg.get("created_at") or msg.get("updated_at") or "")
    item = {"title": headline, "url": msg.get("url", ""), "pub_epoch": pub}
    symbols = msg.get("symbols") or []
    dedup = _ws_pool(conn)
    extra, when = None, None
    if backfill:
        age = max(0.0, time.time() - pub) if pub else 0.0
        extra = {"backfill": {"age_s": round(age), "at": round(time.time())}}
        if pub and age > float(_ncfg("alpaca_backfill", "no_alert_minutes", 60)) * 60:
            when = pub
    with conn:
        return _insert_event(conn, item, "Alpaca/Benzinga", 1,
                             universe, holds, explicit_tickers=symbols,
                             dedup_against=dedup, tag_gate=True, extra_raw=extra, ingested_at=when)


def _handle_alpaca_news(conn, msg, universe, holds) -> bool:
    """Turn one Alpaca `T:"n"` message into a news_events row."""
    global _alpaca_last_msg_ts
    _alpaca_last_msg_ts = time.time()   # liveness beacon for the health monitor
    return _insert_alpaca_item(conn, msg, universe, holds)


# ── backfill after a disconnect (audit R8) ──────────────────────────────────────
# The stream has no replay: whatever Benzinga published while the socket was down
# (or while Mason was restarting) was simply lost. On every (re)connect the gap is
# now read back from Alpaca's news REST endpoint (same free data, same credentials).
_ALPACA_NEWS_REST = "https://data.alpaca.markets/v1beta1/news"


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_alpaca_news_range(key, secret, start_epoch, end_epoch, max_pages: int = 10) -> list:
    """News published in [start, end] as stream-shaped dicts {headline, url,
    created_at, symbols}, returned OLDEST FIRST. It is READ newest-first, so when a
    long gap holds more than max_pages × 50 items the part that is kept is the most
    recent one (the old oldest-first read lost exactly that). Plain GETs, a short
    pause on 429. [] on any failure — never raises, never touches the DB."""
    out, token = [], None
    try:
        pages = 0
        while pages < max(1, int(max_pages)):
            params = {"start": _iso_utc(start_epoch), "end": _iso_utc(end_epoch), "limit": 50,
                      "sort": "desc", "include_content": "false"}
            if token:
                params["page_token"] = token
            r = requests.get(_ALPACA_NEWS_REST, params=params, timeout=10,
                             headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
            pages += 1
            if r.status_code == 429:
                time.sleep(3)
                continue
            if r.status_code != 200:
                break
            body = r.json() or {}
            for n in body.get("news") or []:
                out.append({"headline": n.get("headline"), "url": n.get("url", ""),
                            "created_at": n.get("created_at"), "updated_at": n.get("updated_at"),
                            "symbols": n.get("symbols") or []})
            token = body.get("next_page_token")
            if not token:
                break
        else:
            if token:
                log.info(f"[alpaca-news] backfill stopped at {len(out)} items; older part of the gap not read")
    except Exception:
        pass
    out.reverse()
    return out


def _alpaca_gap_start(conn, now: float = None):
    """Epoch to backfill from, or None when there is nothing worth fetching: the
    last frame this process handled, else the newest stored Benzinga headline;
    never further back than news.alpaca_backfill.max_hours."""
    now = now or time.time()
    last = float(_alpaca_last_msg_ts or 0)
    if not last:
        try:
            row = conn.execute("SELECT MAX(COALESCE(published_at, ingested_at)) FROM news_events "
                               "WHERE source='Alpaca/Benzinga'").fetchone()
            last = float(row[0] or 0) if row else 0.0
        except Exception:
            last = 0.0
    floor = now - float(_ncfg("alpaca_backfill", "max_hours", 1)) * 3600
    start = max(last - 60, floor) if last else floor
    return start if (now - start) > 45 else None


def _backfill_alpaca_items(conn, items: list, universe, holds) -> int:
    n = 0
    for m in items or []:
        try:
            if _insert_alpaca_item(conn, m, universe, holds, backfill=True):
                n += 1
        except Exception as e:
            log.debug(f"[alpaca-news] backfill insert skipped: {e}")
    return n


async def _alpaca_session(key, secret, conn):
    """One connect→auth→subscribe→consume cycle. Raises on disconnect so the
    caller can reconnect."""
    import websockets
    # refresh the book/watchlist once per connection (cheap, and connections are
    # long-lived); also re-pulled on each reconnect.
    try:
        import mason_news
        universe = mason_news.watchlist_set()
        holds = mason_news.holdings_set()
    except Exception:
        universe, holds = set(), set()
    universe, holds = _with_agent_trades(conn, universe, holds)

    async with websockets.connect(_ALPACA_NEWS_WS, ping_interval=20,
                                  ping_timeout=20, close_timeout=5) as ws:
        await ws.recv()                                   # [{"T":"success","msg":"connected"}]
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        auth = json.loads(await ws.recv())
        if not any(m.get("T") == "success" and m.get("msg") == "authenticated"
                   for m in (auth if isinstance(auth, list) else [auth])):
            raise RuntimeError(f"auth failed: {auth}")
        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        log.info("[alpaca-news] connected + subscribed to news:*")

        # subscribed first, THEN read back the gap, so nothing falls between the two
        # (overlap is harmless: the headline-hash id and the dedup pool absorb it)
        try:
            if _ncfg("alpaca_backfill", "enabled", True):
                gap = _alpaca_gap_start(conn)
                if gap:
                    import asyncio
                    items = await asyncio.get_running_loop().run_in_executor(
                        None, _fetch_alpaca_news_range, key, secret, gap, time.time(),
                        int(_ncfg("alpaca_backfill", "max_pages", 10)))
                    got = _backfill_alpaca_items(conn, items, universe, holds)
                    log.info(f"[alpaca-news] backfill after (re)connect: {len(items)} items read back "
                             f"from {_iso_utc(gap)}, {got} new")
        except Exception as e:
            log.warning(f"[alpaca-news] backfill failed: {e}")

        last_refresh = time.time()
        n = 0
        while True:
            raw = await ws.recv()
            try:
                msgs = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msgs, list):
                msgs = [msgs]
            # periodically refresh the universe so newly-added holdings get tagged
            if time.time() - last_refresh > 600:
                try:
                    import mason_news
                    universe = mason_news.watchlist_set()
                    holds = mason_news.holdings_set()
                    universe, holds = _with_agent_trades(conn, universe, holds)
                except Exception:
                    pass
                last_refresh = time.time()
            for m in msgs:
                if m.get("T") == "n":
                    try:
                        if _handle_alpaca_news(conn, m, universe, holds):
                            n += 1
                    except Exception as e:
                        log.debug(f"[alpaca-news] insert skipped: {e}")
            if n and n % 10 == 0:
                log.info(f"[alpaca-news] +{n} events this session")


def _run_alpaca_ws():
    key, secret = _alpaca_creds()
    if not key or not secret:
        log.info("[alpaca-news] no Alpaca creds — real-time news stream disabled")
        return
    import asyncio
    conn = mason_db.get_connection()          # this thread's own sqlite connection
    backoff = 1

    async def _loop():
        nonlocal backoff
        while True:
            try:
                await _alpaca_session(key, secret, conn)
                backoff = 1
            except Exception as e:
                log.warning(f"[alpaca-news] stream error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    try:
        asyncio.run(_loop())
    except Exception as e:
        log.error(f"[alpaca-news] listener stopped: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def start_alpaca_news() -> bool:
    """Start the daemon Alpaca news listener once (idempotent). Returns True if
    it started this call, False if already running or creds are missing."""
    global _alpaca_started
    key, secret = _alpaca_creds()
    if not key or not secret:
        log.info("[alpaca-news] no Alpaca creds — real-time news stream disabled")
        return False
    with _alpaca_lock:
        if _alpaca_started:
            return False
        _alpaca_started = True
    threading.Thread(target=_run_alpaca_ws, daemon=True, name="alpaca-news").start()
    log.info("[alpaca-news] real-time news listener started")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = scan_once()
    print(f"scan_once -> {n} new events; total in db = {event_count()}")
    for it in recent_events(15):
        dot = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(it["impact"], "⚪")
        tags = ("[" + ",".join(it["tickers"]) + "] ") if it["tickers"] else ""
        print(f"  {dot} {it['source']:<14} {tags}{it['headline'][:70]}")
