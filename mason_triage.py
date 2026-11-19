"""
mason_triage.py — Catalyst Desk TOOL 1: deterministic (no-LLM) catalyst triage.
Spec: docs/prompts/CATALYST_DESK_RESEARCH_LAYER.md §2/§3.1; binding interface:
docs/prompts/CATALYST_DESK_CONTRACTS.md §3.

Why this exists (the diagnosed misses, 2026-09-14→09-24): 79 "Muse" headlines about
META produced zero META analyses and the Trump/Xi summit never mapped to anything,
because (1) the only materiality was BOOK-relative qwen scoring, (2) macro scoring
was keyword-count only (0.25 ceiling for product/app/trial news), (3) nothing
resolved company names → tickers, (4) every headline was scored in isolation, and
(5) the tag universe was locked to holdings/watchlist.

This module fixes those, deterministically, over every `news_events` row:
  * resolve_tickers()    — company name / brand / exec / product alias → ticker,
                           precision-guarded (word boundaries, ambiguous-word guard,
                           broad-universe preference).
  * classify_kind()      — contracts §1 event_kind via ordered regexes; trusts a
                           structured source's `event_kind_hint`.
  * market materiality   — MARKET-relative 0..1 per event (per-kind floors). The
                           existing `materiality` column stays BOOK-relative (inbox).
  * story clustering     — `catalysts` rows upserted by story_key; velocity (last 6h,
                           distinct-source weighted) vs a trailing 7-day baseline;
                           tape confirmation (move vs sector ETF + relative volume).
  * alerts               — ONE inbox notification when a catalyst first crosses
                           catalyst.triage.alert_threshold.

Everything is behind `catalyst.triage.enabled` (default false): flag off ⇒
run_triage() returns immediately and mason_news_service behaves as before.
Never imports mason_app (heavy Flask app). Public functions never raise.
"""

from __future__ import annotations

import collections
import html as _html
import io
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mason_db

log = logging.getLogger("mason.triage")
BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
UNIVERSE_FILE = BASE / "universe_broad.json"
EDGAR_CACHE = BASE / ".edgar_tickers_cache.json"
_CREATED_BY = "mason_triage"

# ── config (read per call, mtime-cheap — same idea as _macro_enabled) ───────────
_cfg_cache = {"mtime": None, "data": {}}

_DEFAULTS = {
    "enabled": False,
    "alert_threshold": 0.7,
    "story_window_hours": 72,
    "velocity_window_hours": 6,     # "recent" attention window
    "baseline_days": 7,             # trailing baseline for velocity
    "lookback_hours": 72,           # never triage rows older than this (first-enable backlog guard)
    "tape_min_score": 0.45,         # only pay for tape lookups on stories already this hot
    "max_tape_lookups": 6,          # uncached tape fetches per run
    "tape_ttl_seconds": 300,
    # ── Step 3 (audit R3/R4, 2026-11-13). Every switch is ON when the key is absent. ──
    "strong_kinds_enabled": True,   # recognise management change, guidance cut, halt … (False ⇒ old vocabulary)
    "strong_min_floor": 0.6,        # an event is "strong" when its kind is strong AND its own floor is this high
    "kind_floors": {},              # {kind: floor} overrides of the built-in per-kind floors
    "non_event_ceiling_enabled": True,
    "non_event_ceiling": 0.3,       # opinion / "why X is moving" / listicle / recap headlines never score above this
    "solicitation_ceiling": 0.15,   # plaintiff-firm "investor alert" advertising
    "no_attach_consumed": True,     # a new event never joins a story the director already used
    "strong_kind_new_story": True,  # a strong event never joins a story of a different kind
    "story_max_age_days": 5,        # hard cap on a story's life, counted from its FIRST headline
    "realert_enabled": True,        # a story may alert again …
    "realert_score_step": 0.2,      # … when its score rises this much above the best alert already sent
    "realert_min_minutes": 120,     # … but the same story not more often than this
    # inbox budget (review round 2): what has already been alerted for a TICKER counts,
    # not only for the story — see _alert_decision
    "alert_budget_enabled": True,
    "alert_budget_per_ticker": 3,   # alerts per ticker (or macro family) per rolling window
    "alert_budget_hours": 24,
    "alert_budget_strong_overflow": 2,   # extra room, only for a strong kind not yet alerted for the ticker
    "alert_headline_similarity": 0.6,    # a headline this close to one already alerted is "unchanged"
    "follow_consumed_enabled": True,     # a story that continues a used one is measured against its alerts
    "backfill_no_alert_minutes": 60,     # a headline read back after a disconnect and older than this never alerts
    # companies that are NOT on the book (holdings, watchlist, Mason's open trades): the
    # inbox hears about them only for a confirmed strong event, or an exceptional score
    "alert_offbook_mode": "strong",      # "strong" | "all" (= the old behaviour: any story over the line)
    "alert_offbook_min_floor": 0.7,      # how strong the event itself must be (a strong kind, or a primary-source item)
    "alert_offbook_score": 0.85,         # … or the story score that gets through anyway
    "alert_escalation_floor_step": 0.15,  # a strong kind already alerted counts as new again when the event is this much stronger
    "ticker_gate_enabled": True,    # no story for a symbol that is not a known US listing / is known-dead
}


def _cfg() -> dict:
    """catalyst.triage block merged over _DEFAULTS. Never raises."""
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        tri = ((_cfg_cache["data"].get("catalyst") or {}).get("triage") or {})
    except Exception:
        tri = {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in tri.items() if v is not None})
    return out


def triage_enabled() -> bool:
    try:
        return bool(_cfg().get("enabled", False))
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── §1 vocabulary + families ────────────────────────────────────────────────────
EVENT_KINDS = (
    "product_launch", "app_rank", "ipo_filing", "ipo_pricing", "ipo_calendar",
    "trial_readout", "trial_update", "fda_decision", "pdufa", "adcom",
    "deal_partnership", "mna", "activist", "insider_buy", "insider_sell",
    "guidance", "earnings", "analyst_action", "contract_award", "legal_regulatory",
    "macro_data", "central_bank", "geopolitical", "trade_policy", "commodity",
    "prediction_market", "other",
    # Step 3 (audit R3): events that used to fall into 'other' and get filed under
    # whatever old story the ticker had. Recognisers: _STRONG_RULES below.
    "mgmt_change", "guidance_cut", "guidance_raise", "halt", "distress",
    "accounting", "investigation", "dividend_cut", "buyback", "contract_loss",
    "data_breach", "labor_strike", "product_recall",
    # news phase 6b (2026-11-18): a sale of new shares (or of notes that turn into shares). Only the
    # deal detector (mason_deals, switch news.deals.enabled) files a release under it; before, and
    # with the switch off, such a release is 'other' with the dilution floor below (_OFFERING_RE).
    "offering",
)

# Kinds whose arrival is news in its own right. An event is treated as STRONG only
# when its kind is here AND its own deterministic floor reaches strong_min_floor
# (so "FDA accepts filing" or "senator urges CEO to resign" are not), and it is not
# a commentary headline. Strong events open their own story and are scored first.
STRONG_KINDS = frozenset({
    "mgmt_change", "guidance_cut", "halt", "distress", "mna",
    "accounting", "investigation", "dividend_cut", "fda_decision", "adcom",
    "trial_readout", "contract_award", "contract_loss", "data_breach",
    "labor_strike", "product_recall",
})

# plain words for inbox text (new kinds only; older kinds keep their id)
KIND_LABEL = {
    "mgmt_change": "management change", "guidance_cut": "guidance cut",
    "guidance_raise": "guidance raise", "halt": "trading halt",
    "distress": "bankruptcy / delisting risk", "accounting": "accounting problem",
    "investigation": "investigation (regulator or board)", "dividend_cut": "dividend cut",
    "buyback": "share buyback", "contract_loss": "contract lost",
    "data_breach": "data breach", "labor_strike": "strike", "product_recall": "product recall",
    "offering": "share sale",
}

# story_key groups kinds into families so e.g. "launch" + "#1 on the App Store"
# about one product land in ONE story, and a US-China summit + tariff talk too.
_FAMILY = {
    "mgmt_change": "mgmt", "guidance_cut": "guidance_cut", "guidance_raise": "fundamentals",
    "halt": "halt", "distress": "distress", "accounting": "accounting",
    "investigation": "investigation", "dividend_cut": "payout", "buyback": "payout",
    "contract_loss": "deal", "data_breach": "breach", "labor_strike": "strike",
    "product_recall": "recall",
    "product_launch": "product", "app_rank": "product",
    "ipo_filing": "ipo", "ipo_pricing": "ipo", "ipo_calendar": "ipo",
    "trial_readout": "bio", "trial_update": "bio", "fda_decision": "bio",
    "pdufa": "bio", "adcom": "bio",
    "deal_partnership": "deal", "mna": "mna", "activist": "mna",
    "contract_award": "deal", "offering": "offering",
    "insider_buy": "insider", "insider_sell": "insider",
    "guidance": "fundamentals", "earnings": "fundamentals",
    "analyst_action": "street",
    "legal_regulatory": "legal",
    "macro_data": "macro", "central_bank": "cb",
    "geopolitical": "geo", "trade_policy": "geo",
    "commodity": "cmdty", "prediction_market": "pm",
    "other": "news",
}


def kind_family(kind: str) -> str:
    return _FAMILY.get(kind or "other", "news")


# ── broad universe (S&P 500 + Nasdaq-100 + book) ────────────────────────────────
_WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_NDX = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
_WIKI_UA = {"User-Agent": "Mason/1.0 (personal research tool; catalyst triage)"}
_UNIVERSE_MAX_AGE = 7 * 86400          # refresh at most weekly
_UNIVERSE_RETRY_AFTER = 6 * 3600       # after a failed refresh, don't hammer
_universe_mem = {"mtime": None, "data": None, "last_attempt": 0.0}

# GICS sector (S&P table) / ICB industry (NDX table) → SPDR sector ETF.
SECTOR_ETF = {
    "information technology": "XLK", "technology": "XLK",
    "communication services": "XLC", "telecommunications": "XLC",
    "consumer discretionary": "XLY", "consumer staples": "XLP",
    "energy": "XLE", "financials": "XLF", "health care": "XLV",
    "industrials": "XLI", "materials": "XLB", "basic materials": "XLB",
    "real estate": "XLRE", "utilities": "XLU",
}


def sector_etf(ticker: str) -> str:
    """Sector ETF for a ticker from the broad universe; SPY when unknown."""
    try:
        meta = broad_universe(allow_refresh=False).get((ticker or "").upper()) or {}
        return SECTOR_ETF.get((meta.get("sector") or "").strip().lower(), "SPY")
    except Exception:
        return "SPY"


def _read_html_tables(text: str):
    import pandas as pd
    return pd.read_html(io.StringIO(text), flavor="lxml")


def _pick_col(cols, *names):
    low = {str(c).lower(): c for c in cols}
    for n in names:
        for lc, c in low.items():
            if lc.startswith(n):
                return c
    return None


def _fetch_constituents() -> dict:
    """{ticker: {name, sector, industry, index:[...]}} from Wikipedia. Raises on
    failure (caller falls back). Two GETs, timeout 10s each."""
    import requests
    out = {}
    for url, idx in ((_WIKI_SP500, "SP500"), (_WIKI_NDX, "NDX")):
        r = requests.get(url, headers=_WIKI_UA, timeout=10)
        r.raise_for_status()
        table = None
        for t in _read_html_tables(r.text):
            sym = _pick_col(t.columns, "symbol", "ticker")
            if sym is not None and len(t) >= 90:
                table = t
                break
        if table is None:
            raise RuntimeError(f"no constituents table at {url}")
        c_sym = _pick_col(table.columns, "symbol", "ticker")
        c_name = _pick_col(table.columns, "security", "company")
        c_sec = _pick_col(table.columns, "gics sector", "icb industry")
        c_ind = _pick_col(table.columns, "gics sub", "icb subsector")
        for _, row in table.iterrows():
            tk = str(row[c_sym]).strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", tk):
                continue
            e = out.setdefault(tk, {"name": "", "sector": "", "industry": "", "index": []})
            # the S&P (GICS) row wins on sector naming; NDX (ICB) fills gaps
            if not e["name"] and c_name is not None:
                e["name"] = str(row[c_name]).strip()
            if not e["sector"] and c_sec is not None:
                e["sector"] = str(row[c_sec]).strip()
            if not e["industry"] and c_ind is not None:
                e["industry"] = str(row[c_ind]).strip()
            if idx not in e["index"]:
                e["index"].append(idx)
    if len(out) < 400:
        raise RuntimeError(f"constituents too small ({len(out)})")
    return out


def _edgar_rows() -> list:
    """[(ticker, title)] from .edgar_tickers_cache.json, EDGAR's own order (≈ by
    market cap). Empty list on any failure."""
    try:
        d = json.loads(EDGAR_CACHE.read_text(encoding="utf-8"))
        vals = d.values() if isinstance(d, dict) else d
        return [(str(v.get("ticker", "")).upper(), str(v.get("title", "")))
                for v in vals if isinstance(v, dict) and v.get("ticker")]
    except Exception:
        return []


def _edgar_fallback_universe(n: int = 600) -> dict:
    """Degraded universe: the first `n` EDGAR entries (largest filers), one per CIK."""
    out, seen = {}, set()
    for tk, title in _edgar_rows():
        if len(out) >= n:
            break
        if "-" in tk or title in seen:
            continue
        seen.add(title)
        out[tk] = {"name": title, "sector": "", "industry": "", "index": ["edgar_top"]}
    return out


def _book_sets():
    try:
        import mason_news
        return set(mason_news.holdings_set()), set(mason_news.watchlist_set())
    except Exception:
        return set(), set()


_DICT_PATHS = ("/usr/share/hunspell/en_US.dic", "/usr/share/myspell/en_US.dic",
               "/var/snap/firefox/common/snap-hunspell/en_US.dic")


def _dict_words() -> set:
    """Lowercase entries of a system hunspell en_US dictionary, if one exists
    (first found). Empty set otherwise — the headline lexicon still applies."""
    import glob
    paths = list(_DICT_PATHS) + sorted(glob.glob("/snap/*/*/usr/share/hunspell/en_US.dic"))
    for p in paths:
        try:
            out = set()
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    w = line.split("/", 1)[0].strip()
                    if w.isalpha() and w.islower():
                        out.add(w)
            if len(out) > 10000:
                return out
        except Exception:
            continue
    return set()


def _build_lexicon(limit: int = 20000) -> list:
    """Everyday words that also occur as company-name tokens. A single-word name
    that is an everyday word ('target', 'block', 'visa', 'freight', 'patrick')
    needs extra context to resolve. Sources: words seen LOWERCASE in ≥3 recent
    headlines (data-derived) ∪ a system English dictionary when present,
    restricted to tokens of EDGAR / constituent names so the stored list stays
    small. Read-only DB access; [] on failure."""
    counts = {}
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT headline FROM news_events ORDER BY ingested_at DESC "
                                "LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            h = _html.unescape(r["headline"] or "")
            for w in set(re.findall(r"(?<![A-Za-z'’])([a-z][a-z\-]{2,})(?![A-Za-z])", h)):
                counts[w] = counts.get(w, 0) + 1
    except Exception:
        counts = {}
    lex = {w for w, c in counts.items() if c >= 3}
    try:
        name_toks = set()
        for _, title in _edgar_rows():
            name_toks.update(_norm_name(title).split())
        lex |= (_dict_words() & name_toks)
    except Exception:
        pass
    return sorted(lex)


def broad_universe(*, force_refresh: bool = False, allow_refresh: bool = True) -> dict:
    """{TICKER: {"name","sector","industry","index":[...]}} — S&P 500 + Nasdaq-100
    (Wikipedia) merged with holdings/watchlist, cached to universe_broad.json and
    refreshed at most weekly. On failure: the cached file (even stale), else the
    EDGAR top-600 fallback. `allow_refresh=False` never touches the network (the
    ingest-path resolver uses that so a feed insert never waits on Wikipedia).
    Never raises."""
    try:
        now = time.time()
        cached = None
        try:
            mt = UNIVERSE_FILE.stat().st_mtime
            if _universe_mem["mtime"] == mt and _universe_mem["data"] is not None:
                cached = _universe_mem["data"]
            else:
                cached = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
                _universe_mem.update(mtime=mt, data=cached)
        except Exception:
            cached = None
        fresh = bool(cached) and (now - float(cached.get("built_epoch", 0))) < _UNIVERSE_MAX_AGE
        tickers = dict((cached or {}).get("tickers") or {})
        if allow_refresh and (force_refresh or not fresh) and \
                (force_refresh or now - _universe_mem["last_attempt"] > _UNIVERSE_RETRY_AFTER):
            _universe_mem["last_attempt"] = now
            try:
                tickers = _fetch_constituents()
                doc = {"schema": "universe_broad/1", "built_epoch": now,
                       "built_at": _now_iso(), "source": "wikipedia:sp500+ndx",
                       "n": len(tickers), "tickers": tickers,
                       "lexicon_common": _build_lexicon() or
                       list((cached or {}).get("lexicon_common") or [])}
                tmp = UNIVERSE_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(doc, indent=0, sort_keys=True), encoding="utf-8")
                tmp.replace(UNIVERSE_FILE)
                _universe_mem.update(mtime=UNIVERSE_FILE.stat().st_mtime, data=doc)
                _resolver_mem["key"] = None          # rebuild the name index
                log.info(f"[triage] broad universe refreshed: {len(tickers)} tickers")
            except Exception as e:
                log.warning(f"[triage] universe refresh failed ({e}); using cache/fallback")
        if not tickers:
            tickers = _edgar_fallback_universe()
        out = {k: dict(v) for k, v in tickers.items()}
        holds, watch = _book_sets()
        names = None
        for tk in (holds | watch):
            tk = tk.upper()
            if tk in out:
                out[tk]["index"] = sorted(set(out[tk].get("index", [])) | {"book"})
                continue
            if names is None:
                names = {t: n for t, n in _edgar_rows()}
            out[tk] = {"name": names.get(tk, ""), "sector": "", "industry": "", "index": ["book"]}
        return out
    except Exception:
        return {}


def _lexicon() -> set:
    try:
        broad_universe(allow_refresh=False)  # loads the cache into _universe_mem
        data = _universe_mem.get("data") or {}
        return set(data.get("lexicon_common") or [])
    except Exception:
        return set()


# ── ticker resolution ────────────────────────────────────────────────────────────
# Curated aliases: (pattern, ticker|None, needs_context_regex|None). A None ticker
# is a PRIVATE company — it consumes the span so its words can't partially match
# some unrelated public filer (OpenAI ≠ "Open Text"). Patterns are case-SENSITIVE
# unless they carry (?i). Products/brands with an everyday meaning carry a context
# guard (Muse the rock band vs Meta's Muse agent).
_AI_CTX = r"\b(AI|A\.I\.|agent|agents|app|apps|download\w*|chatbot|assistant|Meta|Zuckerberg|App Store|iOS)\b"
_ALIASES = [
    # Meta
    (r"\bMeta Platforms\b", "META", None),
    (r"\bMeta\b(?![- ]?analys)", "META", None),
    (r"\b(Facebook|Instagram|WhatsApp|Zuckerberg|Oculus)\b", "META", None),
    (r"\bMuse\b", "META", _AI_CTX),
    # Alphabet
    (r"\b(Alphabet|Google|Waymo|YouTube|DeepMind|Sundar Pichai|Pichai)\b", "GOOGL", None),
    (r"\bGemini\b", "GOOGL", r"\b(AI|Google|model|chatbot|app)\b"),
    # Big tech / semis
    (r"\b(Nvidia|NVIDIA|Jensen Huang)\b", "NVDA", None),
    (r"\b(Apple|iPhone|iPad|Tim Cook|Vision Pro)\b(?! Hospitality)", "AAPL", None),
    (r"\b(Microsoft|Satya Nadella|Nadella|Azure|Xbox|LinkedIn)\b", "MSFT", None),
    (r"\b(Amazon|AWS|Andy Jassy)\b(?! rainforest)", "AMZN", None),
    (r"\b(Tesla|Cybertruck)\b", "TSLA", None),
    (r"\bAMD\b|\bAdvanced Micro Devices\b|\bLisa Su\b", "AMD", None),
    (r"\bIntel\b", "INTC", None),
    (r"\bBroadcom\b", "AVGO", None),
    (r"\b(TSMC|Taiwan Semiconductor)\b", "TSM", None),
    (r"\bASML\b", "ASML", None),
    (r"\bMicron\b", "MU", None),
    (r"\b(Qualcomm|Snapdragon)\b", "QCOM", None),
    (r"\b(Oracle|Larry Ellison)\b", "ORCL", None),
    (r"\b(Salesforce|Benioff)\b", "CRM", None),
    (r"\bNetflix\b", "NFLX", None),
    (r"\bDisney\b", "DIS", None),
    (r"\bBoeing\b", "BA", None),
    (r"\b(Super Micro|Supermicro)\b", "SMCI", None),
    (r"\bPalantir\b", "PLTR", None),
    (r"\bUber\b", "UBER", None),
    (r"\bDoorDash\b", "DASH", None),
    (r"\bCoinbase\b", "COIN", None),
    (r"\bRobinhood\b", "HOOD", None),
    (r"\bArm Holdings\b", "ARM", None),
    (r"\bIonQ\b", "IONQ", None),
    (r"\bRocket Lab\b", "RKLB", None),
    (r"\bLockheed( Martin)?\b", "LMT", None),
    (r"\bInstacart\b", "CART", None),
    (r"\bTrump Media\b", "DJT", None),
    # Banks / payments
    (r"\b(JPMorgan|JP Morgan|J\.P\. Morgan|Jamie Dimon)\b", "JPM", None),
    (r"\bGoldman( Sachs)?\b", "GS", None),
    (r"\bMorgan Stanley\b", "MS", None),
    (r"\b(Bank of America|BofA)\b", "BAC", None),
    (r"\bWells Fargo\b", "WFC", None),
    (r"\bCitigroup\b", "C", None),
    (r"\bMastercard\b", "MA", None),
    (r"\bPayPal\b", "PYPL", None),
    (r"\bShopify\b", "SHOP", None),
    (r"\bSchwab\b", "SCHW", None),
    # Pharma
    (r"\b(Eli Lilly|Lilly|Zepbound|Mounjaro)\b", "LLY", None),
    (r"\b(Novo Nordisk|Wegovy|Ozempic)\b", "NVO", None),
    (r"\b(Johnson & Johnson|J&J)\b", "JNJ", None),
    (r"\bPfizer\b", "PFE", None),
    (r"\bMerck\b", "MRK", None),
    (r"\bUnitedHealth\b", "UNH", None),
    # Consumer / energy / China ADRs
    (r"\bWalmart\b", "WMT", None),
    (r"\bCostco\b", "COST", None),
    (r"\bHome Depot\b", "HD", None),
    (r"\bExxon(Mobil)?\b", "XOM", None),
    (r"\bChevron\b", "CVX", None),
    (r"\bOccidental\b", "OXY", None),
    (r"\bMcDonald'?s\b", "MCD", None),
    (r"\bStarbucks\b", "SBUX", None),
    (r"\bCoca-Cola\b", "KO", None),
    (r"\bPepsiCo\b", "PEP", None),
    (r"\bGeneral Motors\b", "GM", None),
    (r"\bAlibaba\b", "BABA", None),
    (r"\b(Temu|Pinduoduo)\b", "PDD", None),
    (r"\bBaidu\b", "BIDU", None),
    # Private / not listed — consume the span, resolve to nothing
    (r"\b(OpenAI|ChatGPT|SpaceX|xAI|Grok|Anthropic|Claude|Stripe|Databricks|"
     r"ByteDance|TikTok|Starlink|Neuralink|Perplexity|Mistral|Cerebras|Shein)\b", None, None),
]
_ALIASES_C = [(re.compile(p), t, re.compile(c) if c else None) for p, t, c in _ALIASES]

# Everyday words that are also single-word company names in the S&P/NDX — they
# only resolve with a finance cue next to them ("Target shares", "Visa Inc",
# "Block's", "$TGT"). Always ambiguous regardless of the data-derived lexicon.
_AMBIGUOUS_BASE = {
    "target", "visa", "block", "snap", "gap", "ball", "match", "progressive",
    "southern", "discover", "carrier", "crown", "waters", "fox", "news", "unity",
    "mosaic", "equity", "general", "american", "first", "united", "public",
    "global", "national", "international", "regions", "citizens", "principal",
    "sun", "constellation", "fortune", "arch", "dover", "ross", "shell", "chase",
    "trust", "select", "origin", "genius", "nano", "evolution", "digital", "energy",
    "gold", "silver", "copper", "oil", "bank", "capital", "financial", "health",
    "summit", "vista", "apex", "pioneer", "liberty", "patriot", "frontier", "live",
    "smart", "compass", "anywhere", "zoom", "square", "delta", "alaska", "spirit",
    "here", "grab", "beta", "revolution", "agree", "vertical", "planet",
}
# Non-company capitalized words that must never resolve as an EDGAR name.
_NEVER_NAME = {
    "trump", "biden", "xi", "putin", "china", "chinese", "us", "u.s", "america",
    "fed", "federal", "reserve", "treasury", "congress", "senate", "house",
    "white", "iran", "israel", "russia", "ukraine", "taiwan", "japan", "india",
    "europe", "stock", "stocks", "market", "markets", "wall", "street", "dow",
    "nasdaq", "nyse", "futures", "ai", "ceo", "report", "reuters", "bloomberg",
    "cnbc", "breaking", "update", "exclusive", "week", "today", "morning",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "msci", "s&p", "russell", "morningstar", "spdr", "ishares", "vanguard",
}
# ETF / fund distribution notices name the SPONSOR, not a catalyst subject
_FUND_NOTICE_RE = re.compile(r"(?i)\b(ETF|Fund|Trust|Portfolio|Shares)\b.{0,80}\b(declares|distribution)\b")
# Uppercase tokens that are tickers but are also acronyms/words in headlines.
_BARE_TICKER_STOP = {
    "AI", "CEO", "CFO", "US", "UK", "EU", "UN", "IPO", "ETF", "GDP", "CPI", "PPI",
    "FDA", "SEC", "FED", "FOMC", "EV", "IT", "ON", "ALL", "ARE", "NOW", "CAT",
    "KEY", "LOW", "HAS", "PM", "AM", "TV", "OR", "SO", "DAY", "EAT", "FAST", "BE",
    "GO", "UP", "IR", "PR", "AN", "CE", "BIG", "NEW", "OUT", "ONE", "TWO", "WELL",
    "TRUE", "FUN", "SEE", "MAN", "CAN", "WAY", "OPEN", "REAL", "LIFE", "PLAY",
    "RUN", "HOPE", "LOVE", "GOOD", "BEST", "CASH", "GAIN", "HUGE", "PEAK", "RARE",
    "AGI", "API", "GPU", "CPU", "USA", "NATO", "OPEC", "WTI", "ECB", "BOJ", "IMF",
    "DOJ", "FTC", "FBI", "ICE", "EPS", "YOY", "QOQ", "ESG", "USD", "EUR", "CEOS",
    "UAE", "GOP", "NFL", "NBA", "BTC", "ETH", "XRP", "SOL", "LLC", "INC", "CORP",
    "COO", "CTO", "PT", "ATH", "EST", "ET", "PST", "IPOS", "ETFS", "Q1", "Q2", "Q3",
    "Q4", "H1", "H2", "FY", "YTD", "DTE", "OTC", "M&A", "LNG", "EIA", "MSCI",
    "NDA", "BLA", "CRL", "RMAT", "EUA", "PDUFA", "NYSE", "SPAC", "REIT", "ADR",
    "JUST", "IN", "NEWS", "ALERT", "WATCH", "HOT", "LIVE", "TOP", "BUY", "SELL",
    "IP", "MS", "TD", "KEY", "FTA", "NOW", "PSA", "EA", "CF", "IQ", "UK", "IDEA", "USB",
}
# R2-A 2026-11-16: acronyms that collide with an S&P ticker as a BARE word only. Measured on
# 93.5k stored stories, every bare hit was something else: "DOW 30" (the index), "NI 43-101"
# (the Canadian mining standard), "Wegovy HD" / "HD Hyundai", "Lazard CL" / "CL=F", "CMS" (the
# Medicare agency), "862 PH/s" (hashrate), "Article 19 MAR" (EU rule), "EL SEGUNDO" (a dateline
# city), "Measure ES". The companies still resolve by name ("Home Depot", "NiSource", "CMS
# Energy") and by explicit markers ($HD, "(HD)", "(NYSE: HD)"), so unlike _BARE_TICKER_STOP
# these stay allowed inside parentheses.
_BARE_ONLY_STOP = {"DOW", "CMS", "NI", "HD", "CL", "PH", "MAR", "EL", "ES"}
# ... except inside a comma list of tickers ("Earnings Week Ahead: BABA, BIDU, HD, and more").
_TKLIST_PREV_RE = re.compile(r"(?<![A-Za-z0-9&.\-'])([A-Z]{1,5}),\s*$")
_TKLIST_NEXT_RE = re.compile(r"^,\s*([A-Z]{1,5})(?![A-Za-z0-9])")


def _in_ticker_list(text: str, a: int, b: int, known) -> bool:
    """True when text[a:b] sits next to another known ticker in a comma list."""
    m = _TKLIST_PREV_RE.search(text[:a])
    if m and m.group(1) in known:
        return True
    m = _TKLIST_NEXT_RE.match(text[b:])
    return bool(m and m.group(1) in known)
# R2-A 2026-11-16: calendar words. "Nov" normalises to NOV Inc.'s EDGAR name, so wire
# datelines ("NEW YORK, Nov. 16, 2026 /PRNewswire/", "(Reuters) Nov 16 -", "payable Nov. 6")
# tagged NOV on ~150 stories a day (2,071 of the 2,077 NOV hits in the stored news). A month
# or weekday word written in mixed case ("Nov", "Nov.", "Sept.") is the calendar word, never a
# name (NOV Inc. writes itself in capitals), unless a corporate designator follows it ("Nov
# Inc."). In capitals ("NOV") it is a date only next to a day number or a year ("NOV 16",
# "16 NOV", "NOV. 16, 2026", "NOV 2026", "Mon NOV"). Explicit markers ($NOV, "(NYSE: NOV)",
# "(NOV)") never reach this check, and "NOV Inc." / "NOV shares" still resolve.
_MONTH_WORDS = {"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
                "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
                "oct", "october", "nov", "november", "dec", "december"}
_DAY_WORDS = {"mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu", "thur",
              "thurs", "thursday", "fri", "friday", "sat", "saturday", "sun", "sunday"}
_CALENDAR_WORDS = _MONTH_WORDS | _DAY_WORDS
_DAY_NUM = r"(?:3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?"
_DATE_NEXT_RE = re.compile(r"^\s*\.?\s*,?\s*(?:" + _DAY_NUM + r"(?![0-9A-Za-z])|(?:19|20)\d\d\b|['’]\d\d\b)", re.I)
_DATE_PREV_RE = re.compile(r"(?:\b" + _DAY_NUM + r"|\b(?:" + "|".join(sorted(_DAY_WORDS, key=len, reverse=True))
                           + r")\.?,?)[\s\-/]*$", re.I)
_CORP_AFTER_RE = re.compile(r"^\s+(?:Inc|Corp|Corporation|Co|Company|Ltd|plc|Holdings|Group)\b")
# A non-US listing glued to a name token ("Novo Nordisk (NVO, NOVOB.CO, NOV.DE)"): the token is
# that exchange's symbol, not the US company whose name it spells.
_FOREIGN_SYM_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}\.(?:DE|F|L|PA|AS|SW|MI|MC|BR|LS|VI|CO|ST|OL|HE|IR|TO|V|CN|"
                             r"NE|AX|NZ|HK|SS|SZ|T|KS|KQ|TW|NS|BO|SI|JK|BK|KL|SA|MX|JO|TA|IS|WA|AT|SG|IL)$")


def _calendar_use(text: str, w: str, a: int, b: int) -> bool:
    """True when the month / weekday token w = text[a:b] reads as a calendar word, not a company."""
    if not re.sub(r"'s$", "", w).rstrip(".").isupper():   # "Nov", "Nov.", "Sept." (NOV Inc. is in capitals)
        return not _CORP_AFTER_RE.match(text[b:])
    return bool(_DATE_NEXT_RE.match(text[b:]) or _DATE_PREV_RE.search(text[:a]))


# Broker/bank names that are usually the ACTOR on a rating/PT headline
# ("JP Morgan Maintains Overweight on T-Mobile") — dropped when so.
_BROKERS = {"JPM", "GS", "MS", "BAC", "C", "WFC", "BCS", "UBS", "DB", "JEF", "RJF",
            "EVR", "PIPR", "SF", "HLI", "LAZ", "MUFG", "HSBC", "CS", "SCHW", "NMR", "OPY",
            "TFC", "KEY", "RY", "TD", "BMO", "BNS", "CM", "MFG", "RILY", "CFG"}
_BROKER_ACT_RE = re.compile(
    r"^(?:'s)?\s+(?:\w+\s+){0,2}?(maintains|upgrades?|downgrades?|initiates?|reiterates?|"
    r"resumes|assumes|says|sees|expects|analysts?|strategists?|(raises|lowers|cuts|boosts|lifts|"
    r"trims|hikes|ups|resets|sets|slashes)|reinstates|initiated|reiterated|raised|lowered\s+(\w+\s+){0,3}?(price target|PT|target|estimates?|rating|forecast))\b", re.I)
# Market-wrap / list headlines: a basket of tickers is not ONE company's story.
_ROUNDUP_RE = re.compile(
    r"(?i)\b(stocks? (in focus|moving|to watch|movers)|movers|stock market today|futures|"
    r"pre-?market|mid-?day|after-hours|biggest (movers|stocks)|top (gainers|losers)|"
    r"leading and lagging|market (wrap|update|close)|big stocks moving|stocks moving|"
    r"(these|top) \d+ stocks|\d+ stocks (to|on|that)|stocks on investors)\b")
# EDGAR tails that are generic industry words — "Teva Pharmaceutical Industries"
# is also just "Teva", "Philip Morris International" also "Philip Morris".
_GENERIC_TAIL = {
    "therapeutics", "pharmaceutical", "pharmaceuticals", "pharma", "industries",
    "technologies", "technology", "biosciences", "bioscience", "biotherapeutics",
    "biopharma", "biopharmaceuticals", "bio", "medical", "sciences", "international",
    "intl", "systems", "networks", "semiconductor", "semiconductors", "motors", "motor",
    "airlines", "financial", "bancorp", "bancshares", "communications", "entertainment",
    "brands", "labs", "laboratories", "health", "healthcare", "software", "resources",
    "energy", "oncology", "genomics", "robotics", "devices", "solutions", "enterprises",
    "worldwide", "global", "partners", "capital", "aerospace", "automotive", "mining",
    "minerals", "metals", "petroleum", "oil", "gas", "materials", "interactive", "media",
    "platforms", "services", "pharmaceutica",
}
_NAME_SUFFIX = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "holdings", "holding", "group", "sa", "nv", "ag", "se", "lp", "llc", "com",
    "the", "de", "md", "nj", "ny", "del", "adr", "ads", "platforms", "class",
}
_FIN_CUE_AFTER = re.compile(
    r"^(?:'s\b|’s\b|\s+(?:Inc|Corp|Co\b|Company|shares?|stock|stocks|Holdings|"
    r"earnings|results|CEO|CFO|board|investors|beats?|misses|reports?|to acquire|"
    r"is (?:acquiring|buying)|price target))", re.I)


def _norm_name(title: str) -> str:
    """'Meta Platforms, Inc.' → 'meta'; 'HOME DEPOT, INC.' → 'home depot'."""
    s = _html.unescape(title or "").lower().replace("’", "'")
    s = re.sub(r"\([^)]*\)", " ", s)                     # '(Class A)'
    s = re.sub(r"/[a-z]{2,3}/?", " ", s)                  # EDGAR state tags /DE/
    s = re.sub(r"[^a-z0-9&' ]+", " ", s)
    toks = [t for t in s.split() if t]
    while toks and (toks[-1] in _NAME_SUFFIX or len(toks[-1]) == 1 or toks[-1] in ("and", "&")):
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


_resolver_mem = {"key": None, "names": {}, "known": set(), "maxn": 1}


def _resolver_index():
    """(name→ticker map, known-ticker set, broad set, max n-gram). Rebuilt when the
    EDGAR cache or universe file changes."""
    try:
        k1 = EDGAR_CACHE.stat().st_mtime
    except Exception:
        k1 = None
    try:
        k2 = UNIVERSE_FILE.stat().st_mtime
    except Exception:
        k2 = None
    key = (k1, k2)
    if _resolver_mem["key"] == key and _resolver_mem["names"]:
        return _resolver_mem
    names, known, seen_titles = {}, set(), set()
    broad = broad_universe(allow_refresh=False)
    # broad-universe names first so an S&P member wins a name collision
    for tk, meta in broad.items():
        n = _norm_name(meta.get("name", ""))
        if n and n not in names:
            names[n] = tk
        known.add(tk)
    for tk, title in _edgar_rows():
        known.add(tk.replace("-", "."))
        if "-" in tk or title in seen_titles:            # one name per filer, skip preferreds
            continue
        seen_titles.add(title)
        n = _norm_name(title)
        if n and n not in names:
            names[n] = tk
    lex = _lexicon()
    # short-form aliases for names with a generic tail ("teva", "philip morris")
    short = {}
    for n, tk in list(names.items()):
        if tk not in broad:
            continue                              # short forms only for S&P/NDX/book names
        toks = n.split()
        for k in (2, 1):
            if len(toks) > k and all(t in _GENERIC_TAIL for t in toks[k:]):
                sf = " ".join(toks[:k])
                first = toks[0]
                if (len(first) >= 4 and first not in lex and first not in _AMBIGUOUS_BASE
                        and first not in _NEVER_NAME and sf not in names):
                    short.setdefault(sf, set()).add(tk)
    for sf, tks in short.items():
        if len(tks) == 1:                        # unambiguous short form only
            names[sf] = next(iter(tks))
    maxn = max((len(n.split()) for n in names), default=1)
    _resolver_mem.update(key=key, names=names, known=known,
                         broad=set(broad), maxn=min(maxn, 6), lex=lex)
    return _resolver_mem


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.\-']*")
_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Z]{1,5}(?:\.[A-Z])?)\b")
_EXCH_RE = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE(?: American| Arca)?|AMEX|NYSEAMERICAN|"
                      r"OTC\w*|CBOE)\s*[:：]\s*([A-Z]{1,5}(?:\.[A-Z])?)\)")
# R2-A 2026-11-16: a NON-US exchange marker names that market's symbol, which is often a different
# US company: (TSX: T) is TELUS (US: TU), not AT&T; (TSX: CNR) is CN (US: CNI); (TSX: ARE) is Aecon;
# (DB:MAR) is a Frankfurt line. The span is consumed and resolves to nothing (TSX used to be in
# _EXCH_RE above); a dual listing still resolves through its own "(NYSE: X)".
_FOREIGN_EXCH_RE = re.compile(r"\((?:TSX(?:V|-V| Venture)?|CSE|NEO|LSE|AIM|ASX|HKEX|SEHK|SGX|FSE|FWB|XETRA|XTRA|"
                              r"ETR|Euronext|EPA|SIX|TASE|JSE|NSE|BSE|TYO|TSE|KRX|KOSDAQ|DB|BIT|BME|OMX|"
                              r"OSE|WSE|BVMF|BMV)\s*[:：]\s*[^()\s]{1,12}\)")
_PAREN_TK_RE = re.compile(r"\(([A-Z]{1,5})\)")


def _clean(text: str) -> str:
    return _html.unescape(text or "").replace("’", "'").replace("‘", "'")


def resolve_tickers(headline: str, desc: str = "") -> list:
    """Company name / brand / exec / product alias → tickers, in order of first
    appearance in the headline (then desc). Precision-guarded:
      * word boundaries + capitalization (a lowercase word never resolves);
      * everyday-word names (Target, Visa, Block, Snap…) need a finance cue;
      * single-word names outside the S&P/NDX must not be everyday words;
      * bare uppercase tokens only for broad-universe members, never acronyms;
      * private companies (OpenAI, SpaceX…) consume their span.
    Never raises; [] on failure."""
    try:
        return _resolve(_clean(headline), _clean(desc))[0]
    except Exception:
        return []


def _resolve(headline: str, desc: str) -> tuple:
    """(tickers, private_leads) — private_leads is True when a private company
    (OpenAI, Anthropic…) is named BEFORE any public ticker in the headline, i.e.
    the story's subject is not investable directly."""
    if _FUND_NOTICE_RE.search(headline or ""):
        return [], False
    idx = _resolver_index()
    names, known, broad = idx["names"], idx["known"], idx["broad"]
    lex = idx.get("lex") or set()
    hits = []                                  # (position, ticker)
    private_pos = []
    for text, base in ((headline, 0), (desc[:400], 10_000)):
        if not text:
            continue
        taken = []                             # consumed spans

        def _free(a, b):
            return all(b <= x or a >= y for x, y in taken)

        for m in _CASHTAG_RE.finditer(text):
            tk = m.group(1)
            if tk in known or tk in broad:
                hits.append((base + m.start(), tk))
                taken.append(m.span())
        for m in _EXCH_RE.finditer(text):
            hits.append((base + m.start(), m.group(1)))
            taken.append(m.span())
        for m in _FOREIGN_EXCH_RE.finditer(text):      # "(TSX: T)": consumed, never a US ticker
            if _free(*m.span()):
                taken.append(m.span())
        for m in _PAREN_TK_RE.finditer(text):
            tk = m.group(1)
            s0, e0 = m.span()
            if len(tk) == 1 and ((s0 > 0 and (text[s0 - 1].isalnum() or text[s0 - 1] == "-")) or
                                 (e0 < len(text) and (text[e0].isalnum() or text[e0] == "-"))):
                continue                       # "anti-PD-(L)1", "(Z)-Endoxifen": a label, not a ticker
            if (tk in broad or tk in known) and tk not in _BARE_TICKER_STOP and _free(*m.span()):
                hits.append((base + m.start(), tk))
                taken.append(m.span())
        for rx, tk, ctx in _ALIASES_C:
            for m in rx.finditer(text):
                if not _free(*m.span()):
                    continue
                if ctx is not None and not ctx.search(text):
                    continue
                taken.append(m.span())
                if tk:
                    hits.append((base + m.start(), tk))
                elif base == 0:
                    private_pos.append(m.start())
        # EDGAR / constituent names: longest-first n-gram match over capitalized words
        words = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
        n_words = len(words)
        caps = sum(1 for w, _, _ in words if w[:1].isupper())
        mostly_upper = n_words >= 4 and sum(1 for w, _, _ in words if w.isupper() and len(w) > 1) > n_words * 0.6
        title_case = n_words > 0 and caps >= n_words * 0.7
        i = 0
        while i < n_words:
            matched = False
            for n in range(min(idx["maxn"], n_words - i), 0, -1):
                seg = words[i:i + n]
                a, b = seg[0][1], seg[-1][2]
                if not _free(a, b):
                    continue
                # every word Capitalized (connectors like '&'/'of'/'and' allowed)
                if not all(w[:1].isupper() or w[:1].isdigit() or w.lower() in ("&", "of", "and", "de")
                           for w, _, _ in seg):
                    continue
                raw_words = [re.sub(r"'s$", "", w) for w, _, _ in seg]
                key = _norm_name(" ".join(raw_words))
                if not key or key not in names:
                    continue
                tk = names[key]
                toks = key.split()
                after = text[b:]
                if raw_words[-1] != seg[-1][0]:          # possessive stripped
                    after = "'s" + after
                if len(toks) == 1:
                    t0 = toks[0]
                    if t0 in _NEVER_NAME or t0 in _GENERIC_TAIL or len(t0) < 3:
                        continue
                    # a date, not NOV Inc.: "Nov. 16", "Nov 5", "NOV 16", "16 NOV", "Sept." (R2-A)
                    if t0 in _CALENDAR_WORDS and any(
                            _norm_name(re.sub(r"'s$", "", w)) == t0 and _calendar_use(text, w, wa, wb)
                            for w, wa, wb in seg):
                        continue
                    # "NOV.DE" is Novo Nordisk's Frankfurt symbol, not NOV Inc. (R2-A)
                    if any(_FOREIGN_SYM_RE.match(w) for w, _, _ in seg):
                        continue
                    # a person, not the company: "Governor Abbott", "Dave Ramsey"
                    if re.search(r"(?i)\b(governor|gov\.|president|senator|sen\.|rep\.|mayor|judge|"
                                 r"mr\.?|mrs\.?|ms\.?|dr\.?|coach|minister)\s+$", text[:a]):
                        continue
                    nxt_w = words[i + n][0] if i + n < n_words else ""
                    if (not title_case and raw_words[-1] == seg[-1][0] and nxt_w[:1].isupper()
                            and not nxt_w.isupper() and not _FIN_CUE_AFTER.search(after)):
                        continue
                    prev = text[max(0, a - 7):a].lower()
                    ambiguous = t0 in _AMBIGUOUS_BASE or t0 in lex or \
                        (mostly_upper and tk not in broad)
                    if ambiguous:
                        tk_cue = re.search(rf"(\${re.escape(tk)}\b|\({re.escape(tk)}\))", text)
                        if tk not in broad:
                            if not tk_cue:     # small-cap everyday word: explicit ticker only
                                continue
                        else:
                            cue = tk_cue or _FIN_CUE_AFTER.search(after)
                            if not cue or "price" in prev:
                                continue
                else:
                    if all(t in _NEVER_NAME for t in toks):
                        continue
                    if tk not in broad and title_case and len(toks) == 2 and \
                            all(t in lex or t in _AMBIGUOUS_BASE for t in toks):
                        continue                      # "Global Energy" in a Title Case line
                hits.append((base + a, tk))
                taken.append((a, b))
                i += n
                matched = True
                break
            if not matched:
                i += 1
        # bare uppercase ticker tokens ("META's Zuckerberg", "AMD stock") — not when
        # the token opens a longer name ("RF Industries" ≠ Regions Financial)
        if not mostly_upper:
            for j, (w, a, b) in enumerate(words):
                ww = re.sub(r"'s$", "", w)
                nxt = words[j + 1][0].lower() if j + 1 < n_words else ""
                if (len(ww) >= 2 and ww.isupper() and ww.isalpha() and ww in broad
                        and ww not in _BARE_TICKER_STOP and _free(a, b)
                        and (ww not in _BARE_ONLY_STOP or _in_ticker_list(text, a, b, known))
                        and nxt not in _GENERIC_TAIL
                        and not (ww.lower() in _CALENDAR_WORDS and _calendar_use(text, ww, a, b))):
                    hits.append((base + a, ww))
                    taken.append((a, b))
    # a broker that is the ACTOR of a rating headline is not its subject
    kept = []
    for pos, tk in sorted(hits, key=lambda x: x[0]):
        if tk in _BROKERS and pos < 10_000 and re.search(r"(?i)\b(at|by|from|per)\s+$", headline[:pos]):
            continue
        if tk in _BROKERS and pos < 10_000:
            tail = headline[pos:]
            wm = _WORD_RE.match(tail)
            rest = tail[wm.end():] if wm else tail
            # skip the rest of a multi-word broker name ("Morgan Stanley", "Wells Fargo")
            rest = re.sub(r"^(\s+(Morgan|Sachs|Stanley|Fargo|of America|Securities|& Co\.?))+", "", rest)
            if _BROKER_ACT_RE.match(rest):
                continue
        kept.append((pos, tk))
    out = []
    for _, tk in kept:
        tk = tk.upper()
        if tk not in out:
            out.append(tk)
    first_pub = min((p for p, _ in kept if p < 10_000), default=None)
    private_leads = bool(private_pos) and (first_pub is None or min(private_pos) < first_pub)
    return out[:8], private_leads


# ── event-kind classifier (ordered; first match wins) ──────────────────────────────
_GEO_ENTITY = (r"\b(China|Chinese|Beijing|Xi|Xi Jinping|Russia\w*|Putin|Kremlin|Moscow|"
               r"Ukrain\w*|Zelensky\w*|Kyiv|Iran\w*|Tehran|Israel\w*|Gaza|Hamas|Hezbollah|"
               r"Houthi\w*|Yemen|Saudi\w*|Middle East|Taiwan\w*|North Korea\w*|Pyongyang|"
               r"Venezuela\w*|India|Pakistan|BRICS|NATO|G7|G20|APEC|Kim Jong|Netanyahu|"
               r"Strait of Hormuz|Hormuz|Red Sea|Syria\w*|Lebanon|Cuba|Mexico|Canada|EU|"
               r"European Union|Japan\w*|Tokyo|Takaichi|South Korea\w*|Seoul|Denmark|Greenland|"
               r"Wang Yi|He Lifeng|Li Qiang|Rubio|Kushner|Witkoff)\b")
_GEO_ENTITY_RE = re.compile(_GEO_ENTITY)
_GEO_WORDS_RE = re.compile(r"(?i)\b(summit|talks|meeting|meets?|visit\w*|wars?|invasion|invade\w*|"
                           r"missile\w*|airstrikes?|strikes? on|ceasefire|troops|military|sanction\w*|"
                           r"embargo|coup|nuclear|diplomat\w*|state (visit|dinner)|bilateral|"
                           r"tensions?|conflict|attack\w*|drone\w*|escalat\w*|deal|truce|"
                           r"negotiat\w*|envoy|accord|treaty|election|foreign minister|"
                           r"secretary of state|(phone )?call with|spoke with|agreement|"
                           r"blockade|hostage|peace|red carpet|dinner|delegation)\b")

_R = lambda p: re.compile(p, re.I)  # noqa: E731
# a deal that fell apart, or an offer turned down (Step 3)
_MNA_BROKEN = (r"\b(terminat\w+|abandon\w*|scraps?|scrapped|calls? off|called off|walks? away from|"
               r"withdraws?|withdrew|drops?|dropped)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(merger|acquisition|takeover|buyout|"
               r"tender offer|offer (for|to (buy|acquire))|bid (for|to (buy|acquire)))\b|"
               r"\b(merger|acquisition|takeover|buyout)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,25}\b(terminated|called off|collapses?|"
               r"collapsed|falls? apart|fell through|scrapped|abandoned|blocked)\b|"
               r"\b(rejects?|rejected|rebuffs?|spurns?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(offer|bid|proposal|approach|takeover)\b|"
               r"\b(sues?|suing|moves?|seeks?|files?) to block\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,60}\b(merger|acquisition|takeover|"
               r"buyout|deal)\b")
# (review fix) "turns down an offer / proposal" is the weakest M&A reading: with no
# deal wording beside it, a headline the world rules recognise ("Trump rejects Iran
# ceasefire proposal") is that world event, not a broken takeover.
_MNA_REJECT_RE = _R(r"\b(?:rejects?|rejected|rebuffs?|spurns?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:offer|bid|proposal|approach|takeover)\b")
_MNA_CORP_RE = _R(r"\b(?:merger|merg(?:e|es|ed|ing)|acqui\w+|takeover|buyout|tender offer|per share|a share|/share|"
                  r"go(?:ing|es)?[- ]private|shareholders?|stockholders?|unsolicited|hostile)\b|\$\s?\d")


def _mna_reject_only(text: str) -> bool:
    return bool(_MNA_REJECT_RE.search(text) and not _MNA_CORP_RE.search(text))


# company-level kinds (need a subject company to mean much)
_COMPANY_RULES = [
    ("fda_decision", _R(r"\b(FDA|EMA|CHMP)\b.{0,60}\b(approv\w*|clear(s|ed|ance)|authoriz\w*|"
                        r"reject\w*|declin\w*|grants?|granted|complete response)|"
                        r"\bcomplete response letter\b|\bFDA\b.{0,40}\bCRL\b|\bapprov\w*\b.{0,40}\b(by|from) "
                        r"(the )?(FDA|EMA)\b")),
    ("adcom", _R(r"\b(FDA|drug|vaccine)\b.{0,40}\b(advisory committee|AdCom|advisory panel|panel)\b|"
                 r"\bAdCom\b|\b(FDA|advisory) panel (votes?|backs|recommends)\b")),
    ("pdufa", _R(r"\bPDUFA\b|\btarget action date\b")),
    ("trial_readout", _R(r"\b(top[- ]?line|primary endpoint|met (its |the )?(primary|main) (goal|endpoint)|"
                         r"pivotal|clinical (results|data)|overall survival|progression[- ]free|"
                         r"(extends|improves|prolongs) survival|reduces risk of death)\b|"
                         r"\bphase\s?(3|III|2b|IIb|2|II)\b.{0,60}\b(data|results?|readout|succeed\w*|"
                         r"fail\w*|met|meets|miss\w*|positive|negative)\b")),
    ("trial_update", _R(r"\b(phase\s?(1|2|3|I|II|III)|clinical trial|clinical hold)\b")),
    ("ipo_pricing", _R(r"\b(prices?|priced|pricing)\b.{0,50}\b(IPO|initial public offering)\b|"
                       r"\bIPO\b.{0,50}\b(priced|prices|pricing|opens? (at|for)|debut\w*|soar\w*|pops?|"
                       r"trading debut)\b|\b(stock|shares|market) debut\b|\bdebut\w* on (the )?(Nasdaq|NYSE)\b")),
    ("ipo_filing", _R(r"\b(files?|filed|filing|submits?)\b.{0,40}\b(IPO|S-1|F-1|to go public)\b|"
                      r"\bconfidentially (files|filed|submits)\b|\bplans? (an |to )?(IPO|go public)\b|"
                      r"\bIPO\b.{0,40}\b(plans?|seeks?|targets?|valuation|eyes|weighs|hires)\b|\bS-1\b")),
    ("ipo_calendar", _R(r"\bIPOs?\b.{0,20}\b(calendar|this week|next week|lineup|pipeline)\b")),
    ("mna", _R(r"\b(to acquire|acquires|acquired by|agrees? to (buy|acquire|be (acquired|bought))|"
               r"acquisition of|merger|to merge|takeover|buyout|tender offer|all-cash deal|"
               r"per share in cash|go(ing)?[- ]private|explor\w* (a )?sale|bid for|deal to buy|"
               r"in talks (to|over|for) .{0,30}(buy|acquire|deal|acquisition)|to be acquired|"
               r"letter of intent|divest\w*|to sell (its )?.{0,25}(unit|business|division|segment|"
               r"assets?|pipeline|stake))\b|" + _MNA_BROKEN + r"|\b(unsolicited|hostile|sweetened|"
               r"raised|revised|rival|competing) (offer|bid|proposal)\b")),
    ("activist", _R(r"\b(activist|13D|proxy fight|board seats?)\b")),
    ("insider_buy", _R(r"\b(insider|director|CEO|CFO|chairman|officer|executive)s?\b.{0,40}"
                       r"\b(buys|bought|purchases?d?|acquires?)\b.{0,30}\bshares\b|\binsider (buying|buy)\b")),
    ("insider_sell", _R(r"\b(insider|director|CEO|CFO|chairman|officer|executive)s?\b.{0,40}"
                        r"\b(sells|sold|dumps|unloads)\b.{0,30}\bshares\b|\binsider selling\b|\bForm 4\b")),
    ("app_rank", _R(r"(\b(No\.?\s?1|number one|#1|top|tops|topped|topping|climbs?|hits?|surges?|"
                    r"overtakes?)\b.{0,60}\b(App Store|Google Play|app charts?|download charts?)\b)|"
                    r"\bmost[- ]downloaded\b|\b(App Store|Google Play)\b.{0,30}\b(No\.?\s?1|#1|top spot|"
                    r"top of|chart)\b|\bdownloads?\b.{0,40}\b(surg\w*|top\w*|beat\w*|outpac\w*|record)\b|"
                    r"\b(tops?|beat\w*|outpac\w*)\b.{0,50}\bdownloads?\b|\b#1 (iOS|iPhone|Android) app\b")),
    ("guidance", _R(r"\b(raises?|raised|lifts?|boosts?|hikes?|cuts?|lowers?|lowered|slash\w*|"
                    r"reaffirms?|affirms?|reiterates?|withdraws?|pulls|issues?|sees|trims?|ups)\b.{0,40}"
                    r"\b(guidance|outlook|forecast|full[- ]year|FY\s?\d*)\b|"
                    r"\bguides? (above|below|higher|lower)\b|\bprofit warning\b")),
    ("earnings", _R(r"\b(Q[1-4]|H[12]|quarterly|first[- ]quarter|second[- ]quarter|third[- ]quarter|"
                    r"fourth[- ]quarter)\b.{0,60}\b(earnings|results|EPS|revenue|profit|sales|loss)\b|"
                    r"\b(beats?|misses?|tops?|missed)\b.{0,40}\b(estimates?|expectations|consensus|views?)\b|"
                    r"\bEPS\b|\bearnings\b")),
    ("contract_award", _R(r"\b(awarded|awards|wins? .{0,40}contract|contract (award|win)|"
                          r"(Pentagon|DoD|Army|Navy|Air Force|NASA) (contract|deal|order))\b|"
                          r"\$[\d.]+ ?(billion|million|bn|mn|[bm])\b.{0,40}\bcontract\b")),
    ("analyst_action", _R(r"\b(upgrades|upgraded|downgrades|downgraded|(upgrade|downgrade)s? (to|from)|"
                          r"price[- ]target|PT (raised|cut|to|boost)|raises? .{0,20}(target|PT)|"
                          r"initiat\w* (coverage|at)|reinstat\w* (coverage|at|buy|sell|hold)|reiterat\w* (buy|outperform|overweight)|"
                          r"maintains (buy|sell|hold|neutral|outperform|overweight|underweight|"
                          r"equal-weight|market perform|sector perform)|"
                          r"(to|at) (overweight|underweight|outperform|underperform)|"
                          r"(buy|sell|hold|neutral|outperform) rating|analysts? (says|sees|bullish|bearish)|"
                          r":\s*analyst)\b")),
    ("product_launch", _R(r"\b(launch\w*|unveil\w*|rolls? out|rolling out|rollout|debut\w*|"
                          r"introduc\w*|releases?|released|reveals?|showcas\w*|goes on sale|"
                          r"now available|available (now|today)|announces new|new (app|model|chip|product|"
                          r"device|phone|agent|glasses|gadget))\b")),
    ("deal_partnership", _R(r"\b(partner\w*|collaborat\w*|team(s|ed)? up|teams with|alliance|"
                            r"joint venture|signs? (a )?deal|strikes? (a )?deal|licens\w* (deal|agreement)|"
                            r"integrat\w* (with|into)|(supply|distribution) (deal|agreement)|"
                            r"agreement with|stake in)\b")),
    ("legal_regulatory", _R(r"\b(lawsuit|sues?|sued|antitrust|probe|investigat\w*|charges|charged|"
                            r"settle\w*|fined?|penalt\w*|ruling|judge|court|DOJ|FTC|regulators?|"
                            r"recalls?|bans?|banned|blocks?|blocked|subpoena)\b")),
]
# world-level kinds
_WORLD_RULES = [
    ("prediction_market", _R(r"\b(Polymarket|Kalshi|prediction markets?|betting odds)\b")),
    ("trade_policy", _R(r"\b(tariffs?|trade (deal|war|talks|truce|agreement|pact|deficit|tensions)|"
                        r"export controls?|export (curbs|restrictions|ban)|import (duties|curbs)|"
                        r"entity list|chip (curbs|exports?|restrictions)|section 232|section 301|"
                        r"rare earths?)\b")),
    ("geopolitical", None),     # needs a geo entity + a geo word
    ("central_bank", re.compile(r"\b(Fed|FOMC|Federal Reserve|Powell|ECB|Lagarde|BOJ|BoJ|Bank of "
                                r"(England|Japan|Canada)|PBOC|RBA|RBI|SNB|BoC|Warsh)\b|"
                                r"(?i:\brate (cut|hike|decision)s?\b|\bbasis points?\b)")),
    ("macro_data", re.compile(r"\b(CPI|PPI|PCE|JOLTS|ISM|PMI|GDP|NFP)\b|(?i:\b(nonfarm|inflation "
                              r"(data|rate|report|reading)|jobs report|payrolls|jobless claims|"
                              r"unemployment (rate|claims)|retail sales|consumer (confidence|sentiment)|"
                              r"housing starts|durable goods|trade balance)\b)")),
    ("commodity", _R(r"\b(OPEC\+?|crude|oil prices?|Brent|WTI|natural gas|gasoline|oil inventor\w*|"
                     r"crude inventor\w*|gold prices?|copper|silver prices?|wheat|soybeans?|corn|"
                     r"cotton|grains?|oil (falls|rises|jumps|slides|tumbles|surges|dips|climbs))\b")),
]
_COMPANY_RULES_D = dict(_COMPANY_RULES)
_LAUNCH_NOT_PRODUCT_RE = _R(r"\b(?:launch\w*|open\w*|unveil\w*|introduc\w*)\b(?: \w+){0,3} (?:investigation|probe|inquiry|"
                            r"review|lawsuit|legal action|appeal|bid|offer|tender|takeover|attack|strike|search|"
                            r"buyback|offering|campaign against|proxy|recall|restructuring|layoffs?)\b")
# an "employee / voluntary buyout" is a staff reduction, not a takeover
_STAFF_BUYOUT_RE = _R(r"\b(?:employee|employees|voluntary|staff|worker|workers|early[- ]retirement|separation)\b"
                      r"(?: \w+){0,2} buyouts?\b|\bbuyouts? (?:offers? )?(?:to|for) (?:\w+ ){0,3}(?:employees|staff|workers)\b")
_INDEX_ETFS = {"SPY", "QQQ", "DIA", "IWM", "VIX", "VOO", "VTI", "RSP", "XLK", "XLC", "XLY", "XLP",
               "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU", "SMH", "SOXX", "TLT", "GLD", "USO"}
_WORLD_FAMILIES = {"geo", "cb", "macro", "cmdty", "pm"}
_TICKERLESS_OK = _WORLD_FAMILIES | {"ipo"}      # a private issuer's IPO is still a story
_BIO_IPO_MNA = {"fda_decision", "adcom", "pdufa", "trial_readout", "trial_update", "ipo_pricing",
                "ipo_filing", "ipo_calendar", "mna"}


# ── Step 3 (audit R3/R4): strong event kinds + commentary ("non-event") detection ──
# Project rule: recognisers state the PRINCIPLE (who did what to what), they are not
# lists of phrases lifted from the headlines that exposed the gap. Each rule below is
# "an actor/object class" + "a predicate class" within one clause, plus the guards
# that separate the event from look-alikes. They were written against a quarter of
# the stored headlines and measured on the other three quarters (hand-checked hits
# and misses; the numbers, and the fixes that check prompted, are in the Step 3
# build report and under tools_step3/out/ of the staging copy).

# NOTE on the clause class used throughout this block,
#   (?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|…)
# = "any character that does not end the clause": a full stop inside a number
# ($1.90), an initial (Darren E. Guidry) or an abbreviation (Inc.) is not an end.
# (Found on the held-out check: "Appoints President Darren E. Guidry As CEO" was missed.)
# top officers. Bare "president" is excluded (heads of state, vice presidents);
# "chair" of a public body is excluded by _MG_PUBLIC_RE.
_MG_TOP = r"(?:CEO|co-CEO|chief executive(?: officer)?|founder|co-founder)"
_MG_SEC = (r"(?:CFO|chief financial officer|finance chief|COO|chief operating officer|"
           r"(?:executive |board )?chair(?:man|woman|person)?(?: of the board)?|"
           r"(?<!vice )president and (?:CEO|COO|chief (?:executive|operating|financial) officer))")
_MG_ROLE = rf"(?:{_MG_TOP}|{_MG_SEC})"
# leaving the job, suddenly ...
_MG_ABRUPT = (r"(?:resign(?:s|ed|ing|ation)?|step(?:s|ped|ping)? (?:down|aside)|stand(?:s|ing)? down|"
              r"depart(?:s|ed|ing|ures?)?|oust(?:s|ed|er|ing)|fired(?!\s+(?:a|an|back|up|off|at|shots?)\b)|"
              r"fires|terminat(?:es|ed|ion)|removed|dismiss(?:es|ed|al)|sack(?:s|ed)|quits?|out as|"
              r"placed on (?:administrative )?leave|suspended|di(?:es|ed)|has died|death|pass(?:es|ed) away|"
              r"exits?|leaves|is leaving|to leave|replac(?:e|es|ed|ing))")
# ... or on a schedule
_MG_PLANNED = r"(?:retir(?:e|es|ed|ing|ement)|succession|transition|successor|search for)"
# taking the job (the role must FOLLOW the verb: "Appoints X as CFO", "Names new CEO")
_MG_APPOINT = (r"(?:appoint(?:s|ed|ing|ment of)?|nam(?:es|ed|ing)|hir(?:es|ed|ing)|tap(?:s|ped)|"
               r"elect(?:s|ed)|promot(?:es|ed)|elevat(?:es|ed)|install(?:s|ed)|to become|becomes|"
               r"takes? over as|succeed(?:s|ed|ing)?(?: as)?|returns? as|rejoins? as)")
_MG_SELF = (r"(?:resign(?:s|ed|ing|ation)?|step(?:s|ped|ping)? (?:down|aside)|stand(?:s|ing)? down|"
            r"depart(?:s|ed|ing|ures?)?|quits?|out as|is out|placed on (?:administrative )?leave|"
            r"di(?:es|ed)|has died|death|pass(?:es|ed) away|leaves|is leaving|to leave|"
            r"(?:is|was|been|gets?|got|to be) (?:fired|ousted|removed|terminated|dismissed|sacked|replaced|suspended)|"
            r"(?:fired|ousted|removed|dismissed|sacked)(?!\s+(?:a|an|back|up|off|at|shots?|by the)\b)|"
            r"terminated (?:for|without|effective|by)|ouster|exits?(?= (?:as|the company|company|after|amid|following|role|post)\b|[,.;]|$))")
_MG_RX = [
    _R(rf"\b{_MG_ROLE}\b(?![:,]?\s+(?:says|said|sees|told|tells|warns|expects|on\b))(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,60}}?\b{_MG_SELF}\b"),
    _R(rf"\b{_MG_ABRUPT}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,25}}?\b(?:as|of|from|its|the|their|his|her)?\s*{_MG_ROLE}\b"),
    _R(rf"\b{_MG_ROLE}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,40}}?\b(?:to retire|retir(?:es|ing|ement)|to step down)\b"),
    _R(rf"\b{_MG_ROLE} (?:transition|succession|change|departure|search|shake-?up|exit)\b|"
       rf"\b(?:leadership|management|executive) (?:transition|succession|shake-?up|change)s?\b"),
    _R(rf"\b{_MG_APPOINT}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,70}}?\b(?:as |to |its |new |next |interim |permanent )*{_MG_ROLE}\b"),
    _R(rf"\b(?:new|next|interim|acting|incoming|permanent|outgoing) {_MG_ROLE}\b"),
    # the noun form with "of": "Retirement of CFO", "Appointment of Jane Doe as CEO"
    _R(rf"\b(?:retirement|resignation|departure|appointment|death|termination|removal|ouster|succession|"
       rf"replacement) of (?:[\w.&'’-]+ ){{0,5}}{_MG_ROLE}\b"),
]
_MG_PUBLIC_RE = _R(r"\b(?:Fed|FOMC|Federal Reserve|SEC|FCC|FTC|FDIC|CFTC|FDA|ECB|BOJ|House|Senate|committee|"
                   r"party|Democrat\w*|Republican\w*|GOP|DNC|RNC|council|commission|board of (?:education|supervisors)|"
                   r"university|school|league|club|football|NFL|NBA|FIFA|minister|ministry|parliament)\b"
                   r"(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}\bchair")
# somebody wants it / it might happen / it is denied — not the event itself
_SOFT_RE = _R(r"\b(?:urg(?:es|ed|ing)|calls? (?:for|on)|called (?:for|on)|demands?|wants?|should|must|"
              r"pressure(?:d|s)? (?:to|on)|could|may|might|if|whether|rumou?r\w*|speculat\w+|"
              r"den(?:y|ies|ied)|won'?t|will not|not (?:planning|going|expected) to|no plans?|rules? out|"
              r"refus(?:es|ed)|consider(?:s|ed|ing)?|weigh(?:s|ed|ing)?|mull(?:s|ed|ing)?|reportedly|said to|"
              r"poised to|expected to|"
              r"prepar(?:es|ing|ed) (?:to|for|the|an?)?|plans? to|planning to|set to|about to|on the verge|"
              r"close to|amid reports|reports? (?:that|say|says|suggest\w*)|sources? (?:say|says|said)|"
              r"according to (?:sources|reports?)|to (?:decide|determine|consider|vote on|discuss)|"
              r"express\w* interest|interest(?:ed)? in|eye(?:s|ing)|look(?:s|ing) to|hop(?:es|ing) to|"
              r"potential(?:ly)?|possibl[ey]|signals?|ready to|in talks|nears?|nearing|"
              # an unconfirmed media report: "WSJ reports …", "… - report", "…: sources"
              r"(?:WSJ|Wall Street Journal|Bloomberg|Reuters|FT|Financial Times|CNBC|NYT|New York Times|Semafor|"
              r"The Information|Politico|Axios|Barron'?s|media|press) (?:reports?|says|said)|"
              r"reports? (?:WSJ|Bloomberg|Reuters|FT|CNBC|NYT|Semafor|The Information|Politico|Axios))\b|"
              r"[-–—:,]\s*(?:reports?|sources?|WSJ|Bloomberg|Reuters|FT|CNBC|Semafor|Politico|Axios|"
              r"The Information|media reports?)\.?\s*(?:\$[A-Z]+\s*)?(?:\|\s*\w+\s*)?$")
# "... after / following / amid X": X is background, the main clause is the news
_REMOTE_RE = _R(r"\bsince\b|\b(?:weeks|months|years|decades) (?:ago|after|since|later)\b|"
                r"\ba (?:week|month|year|decade) (?:ago|after|since|later)\b|\blast (?:year|month|quarter)\b|"
                r"\bin (?:19|20)\d\d\b|\bearlier this year\b|\bback in\b|\banniversary\b|\bonce\b|"
                # the event belongs to ANOTHER company: "… in sympathy with FMC after that company cut …"
                r"\b(?:in|amid) sympathy with\b|\bafter (?:that company|rival|peer|competitor)\b")
_BACKGROUND_SPLIT_RE = _R(r"\s(?:after|following|amid|amidst|despite|since|in (?:the )?wake of|on (?:the )?heels of|"
                          r"(?:in )?fallout from|in aftermath of|ahead of|before)\s")

# a main clause that only reports the share-price move
_PRICE_REACTION_RE = _R(r"\b(?:shares?|stock)\b.{0,40}\b(?:trading (?:higher|lower)|(?:are|is) (?:down|up|lower|higher)|"
                        r"falls?|fell|drops?|dropped|slides?|slid|sinks?|sank|plunges?|plunged|tumbles?|tumbled|"
                        r"jumps?|jumped|surges?|surged|soars?|soared|rises?|rose|gains?|gained|climbs?|climbed|"
                        r"slips?|slipped|declines?|declined|craters?|cratered|rall(?:y|ies|ied)|pops?|popped|"
                        r"closes? (?:down|up|lower|higher))\b|"
                        r"^.{0,40}\b(?:falls?|drops?|slides?|sinks?|plunges?|tumbles?|jumps?|surges?|soars?|slips?|"
                        r"declines?|craters?|rallies|pops?) \d")

_G_OBJ = (r"(?:guidance|outlook|forecasts?|full[- ]year|fiscal[- ]year|FY\s?'?\d{0,4}|"
          r"(?:revenue|sales|profit|earnings|EPS|EBITDA|margin|production|deliver(?:y|ies)|capex|"
          r"bookings|subscriber|volume|same-store sales|comparable sales) (?:guidance|outlook|forecasts?|"
          r"targets?|view|expectations?|projections?|goal))")
_G_DOWN = (r"(?:cuts?|cutting|lower(?:s|ed|ing)|slash(?:es|ed|ing)?|reduc(?:es|ed|ing)|trims?|trimmed|"
           r"withdr(?:aws?|ew|awing)|suspend(?:s|ed|ing)?|pulls?|pulled|scraps?|scrapped|abandons?|"
           r"downgrades|narrows .{0,20}lower)")
_G_UP = (r"(?:rais(?:es|ed|ing)|lifts?|lifted|boosts?|boosted|hikes?|hiked|ups|upped|increas(?:es|ed|ing)|"
         r"upgrades)")
_G_REF = r"(?:estimates?|consensus|expectations|the street|street|views?|forecasts?|analysts?)"
# the one doing the cutting is not the company (a broker, an agency, a world body) or
# the thing cut is not company guidance (a price target, a credit outlook, GDP, oil)
_G_NOT_COMPANY_RE = _R(r"\b(?:analysts?|brokerages?|economists?|strategists?|IMF|World Bank|OECD|OPEC\+?|IEA|EIA|"
                       r"Fed|ECB|BOJ|central bank|government|ministry|Moody'?s|Fitch|S&P Global|DBRS|"
                       r"price[- ]targets?|target price|PT|ratings?|credit|outlook to (?:negative|stable|positive)|"
                       r"(?:cut|raised|lifted|upgraded|downgraded) to (?:buy|sell|hold|neutral|outperform|underperform|"
                       r"overweight|underweight|market perform|equal[- ]weight)|valuation|investor \w+ \w+ (?:lowers|raises|cuts)|"
                       r"estimates? (?:on|for)|GDP|growth forecast|oil|crude|inflation|rate (?:cut|hike)|"
                       r"weather|crop|harvest|demand forecast|dividend|economic forecasts?|"
                       r"(?:lithium|copper|gold|silver|iron ore|steel|commodity|price) (?:price )?(?:outlook|forecasts?)|"
                       r"lower (?:end|half|bound)|(?:forecasts?|outlook|estimates?) (?:cut|raised|lowered|lifted) at)\b|"
                       r"\b(?:outlook|view|forecasts?|targets?) (?:on|for) (?:the )?(?:US|U\.S\.|global|world|stocks|"
                       r"equities|markets?|economy|bonds|treasur\w+|gold|silver|copper|S&P|Nasdaq|sector|industry)\b|"
                       r"\b(?:opex|operating expenses?|expenses?|costs?|capex|capital expenditures?|tax rate|"
                       r"net loss|loss|cash burn|leverage|debt) (?:guidance|outlook|forecasts?|targets?|view)\b")

_STRONG_RULES = [
    ("halt", _R(r"\btrading (?:halt(?:ed|s)?|suspend(?:ed|s)|suspension|paused?)\b|"
                r"\bhalt(?:s|ed)? (?:the )?trading\b|\b(?:shares|stock|trading)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\bhalted\b|"
                r"\bhalted\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:pending|volatility|news|circuit breaker|LULD)\b|"
                r"\b(?:volatility|LULD|circuit[- ]breaker) (?:halt|pause)\b|"
                r"\b(?:trading|shares) (?:to |will )?resum(?:e|es|ed|ption)\b|\bresum(?:e|es|ed|ption of) trading\b")),
    ("distress", _R(r"\b(?:files?|filed|filing) (?:for )?(?:voluntary )?(?:chapter (?:11|7|15)|bankruptcy|"
                    r"creditor protection|insolvency|administration)\b|"
                    r"\bchapter (?:11|7|15)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:fil\w+|bankruptcy|protection|petition|plan|case|"
                    r"court|reorgani[sz]ation|proceedings?|emerg\w+)\b|"
                    r"\b(?:fil\w+|bankruptcy|enters?|entered|exits?|emerg\w+ from|under|planned|potential)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,25}"
                    r"\bchapter (?:11|7|15)\b|\bgoing[- ]concern\b|\bsubstantial doubt\b|"
                    r"\b(?:fil\w+|declar\w+|enters?|entered|seeks?|sought|faces?|facing|weighs?|consider\w+|"
                    r"prepar\w+|potential|possible|nears?|nearing|brink of|verge of|goes|going|went|into|risk of)"
                    r"(?: \w+){0,3} bankrupt(?:cy)?\b|\bbankruptcy (?:filing|protection|petition|proceedings?|plan|"
                    r"risk|fears?|warning|looms?|odds)\b|\b(?:is|are|was|goes|went|declared|going) bankrupt\b|"
                    # a delisting FOR CAUSE (the exchange acts, or warns) — not a voluntary / post-takeover one
                    r"\bdelist\w*\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,25}\b(?:determination|notice|notification|proceedings?|risk|warning|"
                    r"threat|list|suspension|decision)\b|"
                    r"\b(?:faces?|facing|risks?|receiv\w+|gets?|to be|will be|threat\w* of|warn\w+ of|"
                    r"Nasdaq|NYSE|exchange)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\bdelist\w*\b|"
                    r"\b(?:notice|notification|letter)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,50}\b(?:non-?compliance|deficiency|minimum bid|"
                    r"listing (?:rule|standard|requirement)s?)\b|"
                    r"\b(?:Nasdaq|NYSE)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:deficiency|non-?compliance|minimum bid)\b|"
                    r"\b(?:insolven\w+|receivership|liquidation proceedings|to liquidate|"
                    r"ceas(?:e|es|ing) (?:all )?operations(?!\s+(?:in|at)\s+\w)|wind(?:s|ing)?[- ]down (?:of )?(?:its |the )?(?:operations|business))\b|"
                    r"\b(?:defaults? on|in default|miss(?:es|ed)? (?:an? )?(?:interest|debt|bond|coupon) payment|"
                    r"forbearance|restructuring support agreement|covenant (?:breach|default))\b|"
                    # the debtor and its lenders are renegotiating
                    r"\bdebt restructuring\b|\brestructur\w+ (?:of )?(?:its )?(?:\$[\d.,]+ ?\w+ )?(?:of )?debt\b|"
                    r"\bcourt[- ]supervised (?:restructuring|sale|process)\b|\bcreditor protection\b|"
                    r"\b(?:asks?|asked|seeks?|sought|in talks with|negotiat\w+ with)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,15}"
                    r"\b(?:creditors|lenders|bondholders)\b")),
    ("accounting", _R(r"\brestat(?:e|es|ed|ing|ements?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,50}\b(?:financial|results|earnings|statements?|"
                      r"filings?|reports?|quarters?|10-[KQ]|revenue)\b|\b(?:to|will|must) restate\b|\bnon-?reliance\b|"
                      r"\b(?:no longer|not) be relied (?:up)?on\b|"
                      r"\bauditors?\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:resign\w*|dismiss\w*|quits?|terminat\w+|steps? down|withdraws?)\b|"
                      r"\b(?:dismiss\w+|replac\w+|chang(?:es|ed|e of)|fires?|resignation of|terminat\w+)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}"
                      r"\b(?:auditors?|independent (?:registered )?(?:public )?account\w+)\b|"
                      r"\bmaterial weakness(?:es)?\b|\baccounting (?:irregularit\w+|errors?|fraud|scandal|problems?|"
                      r"issues?|probe|investigation|review|misstatements?)\b|"
                      r"\b(?:delays?|delayed|postpon(?:es|ed)|unable to (?:timely )?file|late fil\w+|"
                      r"will not (?:timely )?file|NT 10-[KQ])\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:10-[KQ]|annual report|quarterly report|"
                      r"financial (?:results|statements)|earnings (?:release|report))\b|"
                      r"\b(?:short[- ]seller|short report)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,80}\b(?:report|alleg\w+|accus\w+|targets?|"
                      r"short position|takes? aim|claims?|says|calls?)\b|"
                      r"\b(?:alleg\w+|accus\w+)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:accounting fraud|inflat\w+ (?:revenue|sales)|"
                      r"cooking the books|round[- ]tripping|fabricat\w+ (?:revenue|sales))\b")),
    ("investigation", _R(r"\b(?:board|audit committee|special committee|independent directors)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}"
                         r"\b(?:launch\w*|open\w*|begin\w*|start\w*|order\w*|initiat\w*|conduct\w*)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}"
                         r"\b(?:investigation|probe|inquiry|review of (?:allegations|conduct))\b|"
                         r"\b(?:SEC|DOJ|Justice Dept\.?|Justice Department|Department of Justice|Dept\.? of Justice|"
                         r"FBI|FTC|CFPB|FINRA|NHTSA|FAA|OSHA|EPA|IRS|feds|federal (?:investigators|authorities|agents|"
                         r"regulators)|U\.S\. attorney|"
                         r"federal prosecutors?|prosecutors|attorneys? general|grand jury|regulators?|"
                         r"European Commission|EU|CMA|antitrust (?:regulators?|authorit\w+|watchdog|enforcers?))\b"
                         r"(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,60}\b(?:investigat\w+|prob(?:e|es|ed|ing)|subpoena\w*|charg(?:es|ed)|indict\w+|"
                         r"raid\w*|sues?|sued|Wells notice|inquiry|open(?:s|ed)? (?:a |an )?(?:case|review))\b|"
                         r"\b(?:investigat\w+|probe|subpoena\w*|inquiry|indict\w+|Wells notice|raid)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}"
                         r"\b(?:by|from) (?:the )?(?:SEC|DOJ|Justice Department|FBI|FTC|CFPB|federal|regulators?|"
                         r"prosecutors|attorneys? general|European Commission)\b|"
                         r"\b(?:receiv(?:es|ed)|disclos(?:es|ed)|gets?|served with)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}\b(?:subpoena|"
                         r"Wells notice|civil investigative demand|grand jury)\b|"
                         r"\b(?:under|faces?|facing) (?:federal |criminal |regulatory |an? |a federal )?"
                         r"(?:investigation|probe|indictment)\b|\b(?:indicted|criminal charges)\b")),
    ("guidance_cut", None),      # composed in _guidance_dir (needs the not-the-company guard)
    ("guidance_raise", None),
    ("dividend_cut", _R(r"\b(?:cuts?|cutting|slash(?:es|ed|ing)?|suspend(?:s|ed|ing)?|eliminat(?:es|ed|ing)|"
                        r"halv(?:es|ed|ing)|reduc(?:es|ed|ing)|omits?|omitted|scraps?|scrapped|pauses?|paused|"
                        r"cancels?|cancel+ed|lowers?|lowered)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:dividends?|payout)\b|"
                        r"\b(?:dividends?|payout)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}\b(?:cut|suspended|suspension|eliminated|halved|"
                        r"reduced|omitted|scrapped|paused|slashed)\b")),
    ("mgmt_change", None),       # composed in _mgmt_match
    ("data_breach", _R(r"\b(?:data breach|security breach|cyber[- ]?attack|cyber(?:security)? incident|"
                       r"ransomware|hacked|hackers|unauthorized access|data (?:leak|theft))\b")),
    ("labor_strike", _R(r"\b(?:workers?|unions?|employees|pilots|machinists|dockworkers|longshore\w*|"
                        r"flight attendants|drivers|staff|nurses|actors|writers|crews?|miners|engineers|"
                        r"UAW|Teamsters|IAM|ILA|ILWU)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,50}\b(?:strikes?|striking|walk ?outs?|walk off|"
                        r"picket\w*|work stoppage)\b|"
                        r"\b(?:strike|walkout|lockout|work stoppage)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:begins?|starts?|ends?|ended|"
                        r"looms?|nears?|vote|authoriz\w+|deadline|averted|continues|enters|threat|notice)\b|"
                        r"\b(?:go(?:es)?|going|went) on strike\b|\bvotes? to (?:strike|authorize a strike|walk out)\b|"
                        r"\b(?:lockout|work stoppage|industrial action)\b")),
    ("product_recall", _R(r"\b(?:recalls?|recalling|recalled)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,60}\b(?:vehicles?|cars?|trucks?|SUVs?|"
                          r"units|products?|lots?|batch(?:es)?|devices?|models?|pounds|bottles|packages|EVs?|"
                          r"airbags?|batteries|pumps?|implants?|drugs?|medication|tablets|injection|formula|"
                          r"toys?|strollers?|due to|over (?:fire|safety|risk|defect))\b|"
                          r"\b(?:voluntary|safety|nationwide|expanded|expands|Class I{1,2}|product|vehicle|"
                          r"massive|global|urgent)\s+recalls?\b|\bNHTSA\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:recall\w*|investigat\w+|probe)\b")),
    ("contract_loss", _R(r"\b(?:loses?|lost|losing)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:contract|customer|client|order|award)s?\b|"
                         r"\b(?:contract|order|award|supply agreement)s?\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:cancel+ed|cancel+ation|"
                         r"terminated|rescinded|not (?:be )?renewed|non-renewal|revoked)\b|"
                         r"\b(?:cancels?|terminates?|rescinds?|revokes?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:contract|order|award|"
                         r"supply agreement)s?\b|\bstop[- ]work order\b")),
    ("buyback", _R(r"\b(?:share|stock) (?:repurchase|buy-?back)s?\b|\bbuy-?backs?\b|"
                   r"\brepurchase (?:program|plan|authorization)\b|\baccelerated share repurchase\b|"
                   r"\b(?:repurchase|buy back)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:shares|stock)\b")),
]
_STRONG_GUARDS = {
    "halt": _R(r"\bhalt(?:s|ed)? (?:production|sales|shipments?|deliver\w+|enrol+ment|dosing|construction|work)\b"),
    "distress": _R(r"\b(?:bankruptcy (?:court|judge) (?:approves|oks|clears) (?:sale|purchase) (?:to|by)|"
                   r"out of bankruptcy|bankruptcy auction)\b|"
                   r"\b(?:voluntar\w+|applies for|application for|approval (?:for|of)|plan of arrangement)\b"
                   r"[^;|]{0,60}\bdelist\w*\b|\bdelist\w*\b[^;|]{0,60}\b(?:ETF|fund|series|acquisition|merger|"
                   r"takeover|arrangement|scheme)\b|\b(?:acquisition|merger|takeover|arrangement)\b[^;|]{0,60}"
                   r"\bdelist\w*\b|\bnot (?:in talks|considering|planning)\b"),
    "data_breach": _R(r"\b(?:launch\w*|unveil\w*|introduc\w*|protect\w*|solutions?|platform|report (?:finds|shows)|"
                      r"survey|study|research|award\w*|partner\w*|guide|webinar|how to|tips|trends|market size|"
                      r"defen[cs]e against|prevent\w*|against (?:ransomware|hackers|cyber)|insurance|readiness|"
                      r"resilien\w+|training|simulation|whitepaper|publish\w*|threat report|"
                      r"report\b(?: \w+){0,3} (?:finds|shows|reveals)|index|predictions|awareness)\b"),
    "labor_strike": _R(r"\b(?:air ?strikes?|missile|drone|military|army|Israel\w*|Iran\w*|Russia\w*|Ukrain\w*|"
                       r"Houthi\w*|Gaza|Hamas|Hezbollah|strike price|lightning|"
                       r"strikes? (?:a|an|back|gold|oil|deal|agreement|partnership|balance|tone|on)\b)"),
    "product_recall": _R(r"\brecall (?:election|petition|vote|effort|campaign)\b"),
    "buyback": _R(r"\b(?:notes|bonds|debentures|debt|warrants)\b"),
    "dividend_cut": _R(r"\b(?:tax|rate|price|job|cost|output|production) cuts?\b|"
                       r"\b(?:never|not|no|without|hasn'?t|has not|won'?t|avoid\w*)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,25}"
                       r"\b(?:cut|suspend\w*|reduc\w+|slash\w*)\b"),
    "contract_loss": _R(r"\b(?:merger|acquisition|takeover)\b"),
    "investigation": _R(r"\bon behalf of\b|\b(?:merger|acquisition|takeover|buyout|deal)\b"),
}
_STRONG_RULES_D = {k: rx for k, rx in _STRONG_RULES}


_MG_NOT_RE = _R(r"\binducement\b|\b(?:equity|stock|option) (?:grants?|awards?)\b|"
                r"\bchair\w*(?: of)?(?: the)? (?:\w+ ){0,3}(?:committee|council|panel|task force|working group)\b|"
                r"\bto chair\b")


# (a) a participle on the role — "ousted CEO X testifies"; (b) the change as a noun
# that something is said ABOUT — "CEO's exit raises doubts", "CEO exit has limited
# impact, says <analyst>": the headline's own verb is not the change.
_MG_ABOUT = (r"(?:has|have|had|is|was|were|will|would|could|may|might|raises?|raised|puts?|drives?|drove|"
             r"weighs?|sparks?|sparked|means?|leaves?|left|hits?|sends?|sent|rattles?|rattled|shakes?|shook|"
             r"tests?|prompts?|prompted|triggers?|triggered|fuels?|adds?|casts?|clouds?|overshadows?|dents?|"
             r"knocks?|wipes?|erases?|spooks?|comes?|came|follows?|followed|marks?|caps?|ends?|shows?)")
_MG_PAST_RE = _R(rf"\b{_MG_ROLE}(?:'s|’s)? (?:[\w.’'-]+ ){{0,3}}?(?:exit|departure|resignation|ouster|transition|"
                 rf"succession|change|shake-?up) {_MG_ABOUT}\b|"
                 rf"\b(?:ousted|former|ex|outgoing|departing|departed|fired|retired|late|then)[- ]"
                 rf"(?:[\w.&]+(?:'s|’s)? ){{0,4}}{_MG_ROLE}\b|\b{_MG_ROLE}(?:'s|’s) (?:exit|departure|resignation|ouster)\b")


def _mgmt_match(text: str) -> bool:
    if _MG_PUBLIC_RE.search(text) or _MG_NOT_RE.search(text):
        return False
    return any(rx.search(text) for rx in _MG_RX)


def _mgmt_is_backstory(text: str) -> bool:
    """True when the only management-change wording is a label on a person or a
    noun about something already known ("ousted CEO Jensen Huang testifies",
    "after the CEO's exit") — coverage of the earlier event, not a new one."""
    try:
        if not _MG_PAST_RE.search(text):
            return False
        return not _mgmt_match(_MG_PAST_RE.sub(" ", text))
    except Exception:
        return False


# (review round 2) "outlook raised" / "guidance cut on weak demand" — the participle closes the
# phrase. "issues conservative outlook, increased 2026 spend" does not: there the participle
# has its own object, so it says nothing about the outlook.
_G_PP_END = (r"(?=\s*(?:$|[.,;:|!?)\]–—-]|\$?\d[\d.,]*\s?%|(?:to|by|from|on|as|after|amid|for|at|again|and|with|"
             r"following|due|despite|ahead|but|while|in|of|versus|vs|below|above|over|under|since|because)\b))")


def _guidance_dir(text: str):
    """'guidance_cut' | 'guidance_raise' | None — the COMPANY moved its own forward view."""
    if _G_NOT_COMPANY_RE.search(text):
        return None
    down = re.search(
        rf"(?i)\b{_G_DOWN}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,45}}\b{_G_OBJ}\b|\b{_G_OBJ}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,25}}\b(?:cut|lowered|reduced|withdrawn|"
        rf"suspended|slashed|pulled)\b{_G_PP_END}|\bprofit warning\b|\bwarns (?:of|on|about)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,30}}\b(?:profit|sales|"
        rf"revenue|earnings|demand|margins?|shortfall)\b|\bguides? (?:below|lower|down)\b|"
        rf"\b(?:sees|expects|forecasts?|guides?|projects?|outlook|guidance)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,70}}\b(?:below|short of|under|"
        rf"miss(?:es|ing)?|trails?|lags?|disappoints?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,20}}\b{_G_REF}\b|"
        rf"\bpreliminary\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,50}}\b(?:below|short of|miss\w*|shortfall)\b|"
        rf"\b(?:guidance|outlook|forecast)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,15}}\b(?:disappoints?|misses|falls short|comes up short)\b|"
        rf"\b(?:weak|weaker|soft|softer|disappointing|downbeat|gloomy|cautious|bleak|dour|tepid|muted|light|"
        rf"below[- ]consensus|worse[- ]than[- ]expected|lowered|reduced)(?:[- ]than[- ]expected)? "
        rf"(?:[\w-]+ ){{0,2}}(?:guidance|outlook|forecast)\b", text)
    if down:
        return "guidance_cut"
    up = re.search(
        rf"(?i)\b{_G_UP}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,45}}\b{_G_OBJ}\b|\b{_G_OBJ}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,25}}\b(?:raised|lifted|increased|boosted|"
        rf"hiked|upped)\b{_G_PP_END}|\bguides? (?:above|higher|up)\b|"
        rf"\b(?:sees|expects|forecasts?|guides?|projects?|outlook|guidance)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,70}}\b(?:above|ahead of|"
        rf"exceed(?:s|ing)?|tops?|topping|beats?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,20}}\b{_G_REF}\b|"
        rf"\bpreliminary\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,50}}\b(?:above|ahead of|exceed\w*|beat\w*)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,20}}\b{_G_REF}\b|"
        rf"\b(?:strong|stronger|upbeat|rosy|bullish|robust|above[- ]consensus|better[- ]than[- ]expected|raised)"
        rf"(?:[- ]than[- ]expected)? (?:[\w-]+ ){{0,2}}(?:guidance|outlook|forecast)\b", text)
    return "guidance_raise" if up else None


def _strong_kind_in(text: str):
    """First strong kind whose rule matches `text` (ordered), or None."""
    for kind, rx in _STRONG_RULES:
        if kind == "mgmt_change":
            hit = _mgmt_match(text)
        elif kind == "guidance_cut":
            hit = _guidance_dir(text) == "guidance_cut"
        elif kind == "guidance_raise":
            hit = _guidance_dir(text) == "guidance_raise"
        else:
            hit = bool(rx.search(text))
        if not hit:
            continue
        g = _STRONG_GUARDS.get(kind)
        if g is not None and g.search(text):
            continue
        return kind
    return None


def strong_kind(headline: str) -> tuple:
    """(kind | None, background: bool). The MAIN clause is tried first; a kind found
    only in an "after / following / amid ..." tail is reported with background=True
    (follow-up coverage of an earlier event: same story, not a new strong event)."""
    try:
        text = _clean(headline)
        m = _BACKGROUND_SPLIT_RE.search(text)
        if m and m.start() >= 12:
            head = text[:m.start()]
            k = _strong_kind_in(head)
            if k:
                return k, bool(k == "mgmt_change" and _mgmt_is_backstory(head))
            k = _strong_kind_in(text)
            if not k:
                return None, False
            # "X shares are trading lower after the company lowered its guidance": the
            # main clause is only the price reaction, so the clause after it IS the news —
            # unless the clause points back in time ("… since Buffett announced he would
            # step down", "a year after …"): that is history, not an event
            if _REMOTE_RE.search(text):
                return k, True
            return k, not bool(_PRICE_REACTION_RE.search(head))
        k = _strong_kind_in(text)
        if k == "mgmt_change" and _mgmt_is_backstory(text):
            return k, True
        if k and _REMOTE_RE.search(text) and re.search(r"(?i)\b(?:since|after|when|as)\b", text):
            return k, True
        return (k, False) if k else (None, False)
    except Exception:
        return None, False


# Commentary / recap / advertising headlines: the FORM says "this is someone talking
# about news", not "something happened". They keep their event kind (so they join the
# right story) but can never score above the ceiling, whatever a model says.
_NE_SOLICIT_RE = _R(
    r"\b(?:lead plaintiff|(?:investor|shareholder|stockholder)s? (?:alert|notice|reminder|deadline)|"
    r"deadline (?:alert|reminder|approaching)|securities (?:fraud )?class action|shareholder (?:class action|"
    r"investigation|rights (?:alert|firm|law firm))|class[- ]action (?:lawsuit )?(?:filed|deadline|reminder|notice))\b|"
    r"^(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,70}\b(?:law (?:firm|offices?)s?|LLP|LLC|PLLC|P\.C\.|P\.A\.)(?<=\w|\.)(?!\w),?(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,25}\b(?:investigat\w+|remind\w+|"
    r"encourag\w+|urg\w+|notif\w+|alert\w+|initiat\w+|announces? (?:shareholder |securities |class |an? )?"
    r"(?:investigation|class action|lawsuit))\b|"
    r"\binvestigat\w+(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,60}\bon behalf of\b|"
    r"\bencourag(?:es|ing)(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:investors|shareholders|stockholders)\b|"
    r"\b(?:investors|shareholders|stockholders) (?:who|with) (?:lost|losses|purchased|suffered|bought)\b|"
    r"\brecover (?:your|their) losses\b|\byou may be entitled\b")
_NE_SOLICIT_NOT_RE = _R(r"\b(?:hires?|hired|retains?|retained|taps|engag\w+|appoints?)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\blaw (?:firm|offices?)\b")
_NE_LABEL_RE = _R(r"^\s*(?:opinion|analysis|column|commentary|editorial|podcast|video|watch|listen|explainer|"
                  r"q&a|interview|transcript|review|preview|recap|roundup|newsletter|charts?|prediction|"
                  r"my take|the big question|fact ?check|quiz|photos|in pictures|timeline)\s*[:|\-–—]")
_NE_INTERROG_RE = re.compile(r"^\s*(?:Why|How|What|When|Where|Who|Which|Should|Is|Are|Can|Could|Will|Would|Does|Do|"
                             r"Did|Has|Have|Here's|Here’s|Here is|Here are|Meet|Inside|Behind|Think|Thinking|"
                             r"Wondering|Want|Looking|Forget|Got|Missed|Worried)\s+[A-Za-z0-9$'\"“‘]")
_NE_PHRASE_RE = _R(
    r"\bwhat (?:it|this|that|they) (?:means?|could mean)\b|\bwhat to (?:know|expect|watch|do|make of)\b|"
    r"\bwhat(?:'s|’s| is) (?:next|going on|happening|behind|driving|at stake)\b|\bhere(?:'s|’s| is| are) "
    r"(?:why|what|how|where|when|the)\b|\bwhy (?:\w+(?:'s|’s)?\s+){0,4}?(?:stock|shares|stocks)\b|"
    r"\b(?:stock|shares) (?:is|are) (?:moving|trading|up|down|falling|rising|soaring|plunging|surging|tumbling|"
    r"jumping|sinking|sliding|rallying|dropping|popping|skyrocketing|climbing|crashing|slipping|gaining)\b"
    r"(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,20}\b(?:today|this week|(?:mon|tues|wednes|thurs|fri)day)\b|"
    r"\b(?:is it|it(?:'s|’s)) (?:time|too late|a good time) to (?:buy|sell)\b|\btime to (?:buy|sell)\b|"
    r"\bbuy,? (?:sell,? )?or hold\b|\bshould (?:you|investors|i|we)\b|(?<!to )\b(?:a|better|top|screaming) "
    r"buy\b|(?<!to )(?<!at )\bstrong buy\b(?! rating)|\b(?-i:best buy)\b|\b(?:the|a) best buy\b|\bstocks? to (?:buy|watch|sell|avoid|own|consider|play)\b|\bto buy (?:now|today|right now|and hold|"
    r"before|hand over fist)\b|\b(?:bull|bear) case\b|\b(?:things|reasons|takeaways|charts|lessons|ways|questions|"
    r"picks|numbers|facts|signs|mistakes|tips) (?:to|that|you|for|from|why|investors|every)\b|"
    r"\bif you(?:'d|’d| had)? invested\b|\bcould (?:make you|be the next|turn|soar|double|triple)\b|\bmillionaire\b|"
    r"\b(?:earnings|conference) call transcript\b|\bcall transcript\b|\bsince last earnings\b|"
    r"\bearnings preview\b|\bwhat analysts (?:are saying|think|expect)\b|\banalysts (?:react|weigh in|"
    r"(?:raise|boost|lift|cut|lower|slash|reset|update|revise)s? (?:their )?(?:price )?(?:targets?|forecasts|estimates))\b|"
    r"\blive (?:coverage|updates|blog)\b|\bfollow live\b|\bas it happened\b|\bweek(?:ly)? (?:in )?review\b|"
    r"\bweekly (?:recap|wrap|roundup)\b|\bweek ahead\b|\bneed to know\b|\bmid-?day\b|"
    r"\bmorning (?:brief|bid|report|movers|wrap)\b|\bmarket (?:wrap|recap|minute)\b|\bstock market today\b|"
    r"\bstocks? (?:making|on) the (?:biggest )?moves?\b|\b(?:biggest|top|pre-?market|after-hours) movers\b|"
    r"\beverything (?:you need to|we) know\b|"
    r"\bexplained\b|\bdeep dive\b|\ba closer look\b|\bkey takeaways\b|\bwho is\b|\bwho are\b|"
    r"\$[\d,]+ (?:invested )?in\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,80}\b(?:is|would be|are) (?:now )?worth\b|"
    r"\banalysts? update (?:price )?targets\b|\b\d+ (?:top |best )?analyst (?:forecasts|picks|calls)\b")
_NE_LISTICLE_RE = _R(r"^\s*(?:top\s+|these\s+|the\s+)?(?:\d{1,3}|two|three|four|five|six|seven|eight|nine|ten)\s+"
                     r"(?:\w+[- ]){0,3}?(?:stocks?|reasons?|things|ways|charts?|takeaways|picks|ETFs?|names|"
                     r"companies|lessons|questions|numbers|facts|signs|tips|trends|themes|ideas|plays|winners|losers)\b")
_NE_RATING_SAME_RE = _R(r"\b(?:maintains|reiterates|reaffirms)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\b(?:buy|sell|hold|neutral|"
                        r"outperform|underperform|overweight|underweight|equal[- ]weight|market perform|"
                        r"sector perform|in-?line|peer perform)\b['\"’”]?(?: rating)? (?:on|for|at|and)\b|"
                        r"\b(?:maintains|reiterates|reaffirms)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,40}\brating\b")
_NE_RATING_NEW_RE = _R(r"\b(?:rais\w+|lower\w+|cuts?|boost\w+|lift\w+|trim\w+|hik\w+|slash\w+|ups|upgrad\w+|"
                       r"downgrad\w+|initiat\w+|adjust\w+|increas\w+|reduc\w+|decreas\w+)\b|"
                       r"\bto \$[\d,.]+ from \$[\d,.]+")

# The grammatical SUBJECT is the market, not a company: "Dow Jones Futures Rise After
# Market Weathers Nvidia CEO Huang's Exit", "Wall Street ends lower as …", "US Equity
# Indexes Fall as …". A company named in such a line is a reason given for the
# market's move; the line must not score, open a story or alert under that ticker.
_MARKET_LEAD_RE = _R(r"^\s*(?:(?:update|breaking|just in|live|recap|wrap)\s*[:\-–—]\s*)?(?:the )?"
                     r"(?:stock market|stock futures|stocks|equities|equity (?:futures|indexes|markets?)|"
                     r"(?:u\.?s\.?|american|european|asian|global|world|canadian|japanese|chinese|uk|german|"
                     r"australian|indian) (?:stocks?|shares|equit\w+|futures|markets?|indexes|bourses)|"
                     r"wall street|dow jones(?: futures)?|dow futures|dow|s&p(?: 500)?(?: futures)?|nasdaq(?: futures)?|"
                     r"russell 2000|futures|markets?|indexes|indices|tsx(?: futures)?|ftse(?: 100)?|dax|nikkei|"
                     r"stoxx(?: 600)?|hang seng|asx(?: 200)?|sensex|nifty|treasury yields|treasuries|bond yields)\b"
                     # … doing what a market does (so "Nasdaq Halts Trading in Acme" or
                     # "Nasdaq to acquire …" — the exchange company acting — is not a wrap)
                     r"(?:[\s,:;\-–—]+[\w.%&'’$]+){0,5}?[\s,:;\-–—]+(?:today|live|wrap|open(?:s|ed)?|clos(?:e|es|ed)|"
                     r"ends?|ended|finish(?:es|ed)?|settles?|settled|rises?|rose|falls?|fell|gains?|gained|"
                     r"drops?|dropped|declines?|declined|slips?|slipped|slides?|slid|climbs?|climbed|jumps?|jumped|sinks?|sank|"
                     r"tumbles?|tumbled|rall(?:y|ies|ied)|rebounds?|rebounded|retreats?|retreated|wavers?|"
                     r"mixed|edges?|edged|drifts?|drifted|slumps?|slumped|surges?|surged|soars?|soared|"
                     r"plunges?|plunged|pares?|pared|extends?|extended|hits?|higher|lower|flat|steady|"
                     r"subdued|muted|under pressure|sell-?off|rout|record|weathers?|shrugs?|digests?|braces?|"
                     r"awaits?|eyes?|struggles?|pauses?|stalls?|whipsaws?|swings?|treads?|holds?|recovers?|"
                     r"bounces?|dips?|dipped|pulls? back|point(?:s)? (?:to|higher|lower)|"
                     r"trad(?:e|es|ing) (?:higher|lower|mixed|flat|near|at|little changed)|"
                     r"(?:is|are|was|were) (?:up|down|higher|lower|mixed|flat|little changed)|up|down)\b")


# 2026-11-15: leads that can only be the market itself (index FUTURES, "Wall Street", "the stock
# market today"). For these the verb does not matter — "Dow Jones Futures Loom, Bitcoin Dives ...;
# Nvidia Earnings ... In Focus" slipped past the verb list above and alerted under NVDA. Bare
# "Nasdaq" / "Dow" / "markets" stay on the verb rule, because the exchange company can be the actor.
_MARKET_LEAD_SURE_RE = _R(r"^\s*(?:(?:update|breaking|just in|live|recap|wrap)\s*[:\-–—]\s*)?(?:the )?"
                          r"(?:(?:dow jones|dow|s&p(?: 500)?|nasdaq(?: 100)?|russell 2000|stock|equity|index|"
                          r"u\.?s\.?(?: stock| equity| index)?) futures|stock market today|wall street)\b"
                          r"(?!\s+(?:journal|banks?|firms?|analysts?|veteran|legend|giant|exchange|contracts?|commission|regulators?|traders?|brokers?)\b)")


# 2026-11-15 (news.quality.label_fixes): a professional-corporation law firm ("& Associates
# PC") advertising an investigation; and a feature / explainer whose headline turns on a colon
# into "how / why / what / inside ...".
_NQ_FIRM_PC_RE = _R(r"^(?:[^.;|?!]){0,70}\b(?-i:PC|P\.C)\b,?(?:[^.;|?!]){0,25}\b(?:investigat\w+|remind\w+|"
                    r"encourag\w+|urg\w+|notif\w+|alert\w+)\b")
_NQ_COLON_EXPLAINER_RE = _R(r"[:—–]\s*(?:how|why|what|inside|the (?:inside |real )?story|a look)\b")


def is_market_wrap(headline: str) -> bool:
    """True when the headline is about the market as a whole (see _MARKET_LEAD_RE)."""
    try:
        t = _clean(headline)
        return bool(_MARKET_LEAD_SURE_RE.search(t) or _MARKET_LEAD_RE.search(t))
    except Exception:
        return False


def non_event_reason(headline: str, company: bool = True):
    """Why a headline is commentary rather than a report of something that happened,
    or None. 'solicitation' (plaintiff-firm advertising), 'question', 'explainer',
    'listicle', 'recap', 'rating_unchanged', and — only when it is being read as
    news about a company (`company=True`) — 'market_wrap'. Never raises."""
    try:
        text = _clean(headline).strip()
        if not text:
            return None
        if company and (_MARKET_LEAD_SURE_RE.search(text) or _MARKET_LEAD_RE.search(text)):
            return "market_wrap"
        if _NE_SOLICIT_RE.search(text) and not _NE_SOLICIT_NOT_RE.search(text):
            return "solicitation"
        fix = _nq_on("label_fixes")
        if fix and _NQ_FIRM_PC_RE.search(text) and not _NE_SOLICIT_NOT_RE.search(text):
            return "solicitation"          # "Monteverde & Associates PC Investigates ... Merger"
        if _NE_LISTICLE_RE.search(text):
            return "listicle"
        if _NE_LABEL_RE.search(text):
            return "explainer"
        if fix and _NQ_COLON_EXPLAINER_RE.search(text):
            return "explainer"             # "'... lost his mind': how AI's smartest man fell for ..."
        if text.rstrip("\"'”’) ").endswith("?") or re.search(r"\?\s+[A-Z(\"“]", text):
            return "question"
        if _NE_INTERROG_RE.search(text):
            return "explainer"
        if _NE_PHRASE_RE.search(text):
            return "recap"
        if _NE_RATING_SAME_RE.search(text) and not _NE_RATING_NEW_RE.search(text):
            return "rating_unchanged"
        return None
    except Exception:
        return None


def event_bounds(headline: str, *, kind: str = None, hint: str = None, has_ticker: bool = True,
                 tier: int = 2, structured: bool = False, cfg: dict = None) -> dict:
    """Deterministic limits for one headline, used wherever a model's score is taken:
        final = min(ceiling, max(model score, floor))     (ceiling only for non-events)
    Returns {kind, floor, ceiling|None, strong, non_event|None, background}. `floor` is
    the market-materiality floor of the kind (with its boosts / caps, no model input);
    `strong` is True only for a real strong event (see STRONG_KINDS). Never raises."""
    out = {"kind": kind or "other", "floor": 0.0, "ceiling": None, "strong": False,
           "non_event": None, "background": False}
    try:
        cfg = cfg if cfg is not None else _cfg()
        if not kind:
            kind = classify_kind(headline, hint, has_ticker=has_ticker)
        out["kind"] = kind
        ne = None
        if cfg.get("non_event_ceiling_enabled", True) and not structured:
            ne = non_event_reason(headline, company=has_ticker)
        floor = market_materiality(kind, headline, tier=tier, has_ticker=has_ticker, _raw_floor=True)
        out["floor"] = floor
        bg = False
        if kind in STRONG_KINDS:
            sk, bg = strong_kind(headline)
            bg = bool(bg and sk == kind)
        out["background"] = bg
        if ne:
            out["non_event"] = ne
            out["ceiling"] = float(cfg.get("solicitation_ceiling", 0.15) if ne == "solicitation"
                                   else cfg.get("non_event_ceiling", 0.3))
        out["strong"] = bool(cfg.get("strong_kinds_enabled", True) and kind in STRONG_KINDS and not ne
                             and not bg and floor >= float(cfg.get("strong_min_floor", 0.6)))
        return out
    except Exception:
        return out


# ── 2026-11-15 news quality (news.quality.label_fixes / symbol_gate) ─────────────
# Principles, not phrase lists: an approval is a DRUG / DEVICE decision only when a
# regulator or a medical product is named; a trial READOUT reports data that exists now
# ("top-line" beside revenue / estimates is an earnings word; data "expected in December"
# is a schedule); and a company named only as a part supplier is not the subject.

def _nq_on(key: str) -> bool:
    try:
        import mason_news_quality as NQ
        return NQ.on(key)
    except Exception:
        return False


_FDA_CONTEXT_RE = _R(r"\b(?:FDA|EMA|CHMP|European Commission|Health Canada|MHRA|PMDA|NMPA|TGA|ANVISA|Swissmedic|"
                     r"NDA|sNDA|BLA|sBLA|ANDA|510\(k\)|PMA|De Novo|EUA|drugs?|therap\w+|treatments?|medicines?|"
                     r"vaccines?|biologics?|biosimilars?|devices?|implants?|injections?|tablets?|capsules?|"
                     r"indications?|clinical|patients?|disease|cancer|tumou?rs?|syndrome|disorders?|infections?)\b")
_EARNINGS_TOPLINE_RE = _R(r"\b(?:top|bottom)[- ]?line\b(?:[^.;|]){0,40}\b(?:estimates?|expectations|consensus|beats?|"
                          r"miss\w*|revenue|sales|EPS|growth|results)\b|\b(?:beats?|miss\w*|tops?)\b(?:[^.;|]){0,20}"
                          r"\btop[- ]?line\b|\brevenue (?:top[- ]?line|topline)\b|\btop[- ]?line (?:revenue|growth|sales)\b")
_CLINICAL_WORD_RE = _R(r"\b(?:phase|trial|study|studies|endpoints?|patients?|cohort|placebo|efficacy|dos(?:e|ing))\b")
_TRIAL_DATA_RE = _R(r"\b(?:data|readouts?|top[- ]?line|endpoints?|goals?|(?:trial|study|phase\s?\w+) results?|outcomes?)\b")
_FUTURE_DATA_RE = _R(r"\b(?:expected|expects?|anticipat\w+|planned|plans? to|upcoming|on track|slated|scheduled|due|"
                     r"runway|through|ahead of|awaits?|awaiting|by (?:year[- ]end|the end of)|"
                     r"in (?:Q[1-4]|[12]H|H[12]|early|mid|late|the (?:first|second) half)(?:[- ]\d{4})?)\b")
_READOUT_FACT_RE = _R(r"\b(?:met|meets|missed|misses|achiev\w+|succeed\w*|fail\w*|positive|negative|showed|shows|"
                      r"demonstrat\w+|reports?|reported|announc\w+|presents?|presented|unveil\w*|releases?|released|"
                      r"posts?|delivers?|improv\w+|significan\w+|updat\w+|statistically)\b")


def _readout_fix(text: str):
    """None when a trial-readout match really reports trial data; otherwise the
    better kind: 'earnings' (the 'top-line' is revenue), 'trial_update' (the data is
    still to come, or only a pivotal trial's design is named)."""
    try:
        if _EARNINGS_TOPLINE_RE.search(text) and not _CLINICAL_WORD_RE.search(text):
            return "earnings"
        ments = list(_TRIAL_DATA_RE.finditer(text))
        if not ments:
            return "trial_update" if re.search(r"(?i)\bpivotal\b", text) else None
        real = future = 0
        for m in ments:
            before, after = text[max(0, m.start() - 50):m.start()], text[m.end():m.end() + 30]
            if _READOUT_FACT_RE.search(before):
                real += 1
            elif _FUTURE_DATA_RE.search(before) or _FUTURE_DATA_RE.search(after):
                future += 1
        if real:
            return None
        if future:
            return "trial_update"
        if re.search(r"(?i)\bpivotal\b", text) and not re.search(
                r"(?i)\b(?:data|results?|readout|met|meets|endpoint|goals?|succeed\w*|fail\w*|positive|negative)\b", text):
            return "trial_update"
        return None
    except Exception:
        return None


# 2026-11-16 (news.quality.label_fixes, follow-up 7): a rule hit that rests only on a phrase
# that is NOT that event — an entertainment / venue "event takeover" is not M&A; a product
# already "FDA-approved" (an adjective) is not an FDA decision; a "press release" is not a
# product release — does not decide the kind (the next rule gets a look).
_EVENT_TAKEOVER_RE = _R(r"\b(?:event|arena|stadium|venue|city|store|festival|weekend|halftime|pop[- ]up|fan|"
                        r"social media|channel)\s+takeovers?\b")
_FDA_ADJ_RE = _R(r"\b(?:FDA|EMA)-(?:approved|cleared|authori[sz]ed)\b")
_PRESS_RELEASE_RE = _R(r"\b(?:press|news) releases?\b")
_NOT_THE_EVENT = {"mna": _EVENT_TAKEOVER_RE, "fda_decision": _FDA_ADJ_RE, "product_launch": _PRESS_RELEASE_RE}
# (hyphenated only: "the FDA approved X" is the decision itself). And two plain forms the rules
# missed: an 8-K item 2.02 "Results of Operations" is earnings; an agreement for
# commercialisation rights is a deal
_EXTRA_RULES = {
    "earnings": _R(r"\bresults of operations\b"),
    "deal_partnership": _R(r"\b(?:agreements?|deals?|rights?) (?:to|for) (?:the )?(?:co-)?commerciali[sz]\w*|"
                           r"\bcommerciali[sz]ation (?:agreements?|rights|deals?)\b"),
}


def _rule_hits(kind: str, rx, text: str, fix: bool) -> bool:
    """Does company rule (kind, rx) decide `text`? With label_fixes: not when its only cue is a
    phrase that is not that event (_NOT_THE_EVENT), and also on the _EXTRA_RULES forms."""
    if rx.search(text):
        if fix and kind in _NOT_THE_EVENT:
            cue = _NOT_THE_EVENT[kind]
            if cue.search(text) and not rx.search(cue.sub(" ", text)):
                return False
        return True
    return bool(fix and kind in _EXTRA_RULES and _EXTRA_RULES[kind].search(text))


def _hint_ok(hint: str, text: str) -> bool:
    """Is a structured source's kind hint backed by the headline itself?"""
    if hint == "fda_decision":
        return bool(_FDA_CONTEXT_RE.search(text))
    if hint == "trial_readout":
        return bool(_CLINICAL_WORD_RE.search(text) or _TRIAL_DATA_RE.search(text)) and _readout_fix(text) is None
    return True


def _readout_or(text: str, fallback_ok: bool = True):
    """For a trial_readout rule hit: the kind to return, or None to keep scanning."""
    fx = _readout_fix(text)
    if fx is None:
        return "trial_readout"
    if fx == "earnings":
        return None
    # data still to come: a quarterly report that mentions it is an earnings item
    if _COMPANY_RULES_D["earnings"].search(text):
        return "earnings"
    return "trial_update"


def _nq_subject(headline: str, desc: str, primary: str, resolved: list) -> str | None:
    """The story's company must be the one the headline is about: never a part supplier
    ('... Camera Featuring onsemi Sensor'), and a vendor tag the text does not name is
    not evidence on its own. Returns the subject, or None (no company story)."""
    try:
        import mason_news_quality as NQ
        text = _clean(headline)
        order = ([primary] + [t for t in resolved if t != primary]) if primary in resolved else [primary]
        for t in order:
            sp = NQ.mention_spans(t, text)
            if sp and NQ._supplier_only(sp, text):
                continue
            if t not in resolved:
                import mason_symbols
                # a name Nico or Mason HOLDS keeps its vendor tag even when the text names a
                # subsidiary or only a product (Beckman Coulter for DHR): its desk checks and
                # dossiers depend on the story existing (review fix 2026-11-15)
                if not mason_symbols.issuer_named(t, f"{text}  {_clean(desc or '')}") and \
                        t not in NQ.held_set():
                    continue
            return t
        return None
    except Exception:
        return primary


def classify_kind(headline: str, hint: str = None, *, has_ticker: bool = True) -> str:
    """contracts §1 event_kind. A structured source's hint wins when valid. With
    no company resolved (`has_ticker=False`) the world kinds (trade/geo/central
    bank/data/commodity) are checked before the generic company kinds, so
    "US reaches agreement with Denmark" is geopolitical, not deal_partnership."""
    try:
        text = _clean(headline)
        if hint and hint in EVENT_KINDS:
            if not (hint in ("fda_decision", "trial_readout") and _nq_on("label_fixes") and not _hint_ok(hint, text)):
                return hint

        def _world():
            for kind, rx in _WORLD_RULES:
                if kind == "geopolitical":
                    if _GEO_ENTITY_RE.search(text) and _GEO_WORDS_RE.search(text):
                        return kind
                elif rx.search(text):
                    return kind
            return None

        strong, _bg = (None, False)
        try:
            if _cfg().get("strong_kinds_enabled", True):
                strong, _bg = strong_kind(text)
        except Exception:
            strong = None
        # a rating note about the event ("downgrades X after CEO exit") is street news
        if strong and _COMPANY_RULES_D["analyst_action"].search(text) and \
                re.search(r"(?i)\b(upgrades?|downgrades?|initiat\w+|maintains|reiterates|price target|PT)\b", text):
            strong = None
        # M&A / FDA / trial headlines keep their own (older, tuned) rules
        if strong in ("contract_loss", "buyback", "halt") and any(
                _COMPANY_RULES_D[k].search(text) for k in ("fda_decision", "trial_readout")):
            strong = None
        if has_ticker:
            if strong and not _bg:
                return strong
            fix = _nq_on("label_fixes")
            for kind, rx in _COMPANY_RULES:
                if _rule_hits(kind, rx, text, fix):
                    if kind == "trial_readout" and fix:
                        k2 = _readout_or(text)
                        if k2 is None:
                            continue
                        if k2 != "trial_readout":
                            return k2
                    if kind == "mna" and _mna_reject_only(text):
                        _w = _world()
                        if _w:
                            return _w
                    if kind == "mna" and _STAFF_BUYOUT_RE.search(text):
                        continue
                    if kind == "product_launch" and _LAUNCH_NOT_PRODUCT_RE.search(text):
                        continue
                    if kind == "mna" and re.search(r"(?i)\btender offer\b.{0,40}\b(notes|bonds|debt)\b|"
                                                   r"\b(notes|bonds|debt)\b.{0,60}\btender offer\b", text):
                        continue
                    if kind == "mna" and _COMPANY_RULES_D["analyst_action"].search(text) and \
                            re.search(r"(?i)\b(rating|price target|upgrades?|downgrades?|initiat)", text):
                        return "analyst_action"
                    if kind == "mna" and re.search(r"(?i)\b(offering|issuer bid|buyback|repurchase|"
                                                   r"acquisition of [\d,.]+ .{0,40}shares|warrant|"
                                                   r"to acquire up to [\d,.]+ .{0,20}shares)\b", text):
                        return "deal_partnership" if re.search(r"(?i)\bshares\b", text) else "other"
                    if kind == "mna" and re.search(r"(?i)\bstake\b", text) and \
                            not re.search(r"(?i)\b(controlling|majority|remaining)\b", text):
                        return "deal_partnership"
                    if kind in ("earnings", "guidance", "product_launch", "deal_partnership",
                                "legal_regulatory") and strong and _bg:
                        return strong          # "... falls after CEO resigns": the follow-up joins that story
                    return kind
            return _world() or strong or "other"
        fix = _nq_on("label_fixes")
        for kind, rx in _COMPANY_RULES:
            if kind in _BIO_IPO_MNA and _rule_hits(kind, rx, text, fix):
                if kind == "mna" and _mna_reject_only(text) and _world():
                    continue
                if kind == "trial_readout" and _nq_on("label_fixes"):
                    k2 = _readout_or(text)
                    if k2 is None:
                        continue
                    return k2
                return kind
        w = _world()
        if w:
            return w
        if strong:
            return strong
        for kind, rx in _COMPANY_RULES:
            if kind not in _BIO_IPO_MNA and _rule_hits(kind, rx, text, fix):
                return kind
        return "other"
    except Exception:
        return "other"


# ── market materiality (per event, MARKET-relative) ──────────────────────────────
_FLOORS = {
    "fda_decision": 0.55, "adcom": 0.7, "pdufa": 0.55, "trial_readout": 0.65,
    "trial_update": 0.3, "ipo_pricing": 0.5, "ipo_filing": 0.45, "ipo_calendar": 0.25,
    "mna": 0.75, "activist": 0.55, "insider_buy": 0.35, "insider_sell": 0.2,
    "app_rank": 0.5, "guidance": 0.6, "earnings": 0.35, "analyst_action": 0.25,
    "contract_award": 0.4, "product_launch": 0.4, "deal_partnership": 0.4,
    "legal_regulatory": 0.35, "central_bank": 0.35, "macro_data": 0.3,
    "trade_policy": 0.55, "geopolitical": 0.45, "commodity": 0.3,
    "prediction_market": 0.35, "other": 0.1,
    # Step 3 strong kinds (overridable: catalyst.triage.kind_floors)
    "mgmt_change": 0.5, "guidance_cut": 0.75, "guidance_raise": 0.6, "halt": 0.55,
    "distress": 0.6, "accounting": 0.7, "investigation": 0.7, "dividend_cut": 0.7,
    "buyback": 0.3, "contract_loss": 0.55, "data_breach": 0.6, "labor_strike": 0.6,
    "product_recall": 0.55,
    "offering": 0.45,          # the same number a share sale filed as 'other' gets from _OFFERING_RE
}
# (pattern, v): v>0 lifts the floor to v; v<0 caps it at |v| (weaker reading)
_BOOSTS = {
    "app_rank": [(_R(r"\b(No\.?\s?1|number one|#1|top spot|top of|tops|topped|overtakes?)\b"), 0.72)],
    "fda_decision": [(_R(r"\b(approv\w*|reject\w*|complete response|CRL)\b"), 0.82),
                     (_R(r"\b(CHMP|EMA|EU approval|European Commission|positive opinion)\b"), -0.6),
                     (_R(r"\b(generic|ANDA|abbreviated|biosimilar|supplemental|sNDA|sBLA|label|"
                         r"prescribing information|assay|manufacturing|facility|potency|expanded|"
                         r"expands?|additional|new indication|pediatric)\b"), -0.5),
                     (_R(r"\b(designation|fast track|breakthrough therapy|orphan|RMAT|accepts?|"
                         r"priority review|clearance|510\(k\))\b"), -0.45)],
    "trial_readout": [(_R(r"\b(phase\s?(3|III)|pivotal|primary endpoint|overall survival)\b"), 0.78),
                      (_R(r"\b(to present|will present|presentations?|poster|conference|abstract|"
                          r"publication|published|enrollment|enrolls|last patient|initiat\w*|"
                          r"advances? toward|subgroup|post[- ]hoc|long-term follow|real-world|"
                          r"sees .{0,20}moment|symposium|expected|expects?|anticipat\w+|on track|upcoming|"
                          r"planned|plans? to)\b"), -0.4)],
    # M&A: the TARGET side is the big mover; an acquirer buying something small,
    # a completion, a review or a rumour is much less
    "mna": [(_R(r"\b(to be acquired|agrees? to be (acquired|bought)|acquired by|takeover (bid|offer|"
                r"approach)|receives? .{0,20}(takeover|buyout|acquisition) (offer|proposal|bid)|"
                r"buyout offer|go(ing)?[- ]private (proposal|offer|deal)|tender offer (for|to purchase)|"
                r"per share in cash|all-cash deal|to buy .{0,40}for \$)"), 0.8),
            (_R(r"\$\s?\d{2,}(\.\d+)?\s?(billion|bn|B)\b"), 0.75),
            (_R(r"\b(acquires|to acquire|acquisition of|agrees? to acquire|buys|announces .{0,20}"
                r"acquisition)\b"), -0.5),
            (_R(r"\b(explor\w*|consider\w*|weigh\w*|talks|report(ed|edly)?|rumou?rs?|interest|"
                r"non-binding|letter of intent|LOI|divest\w*|to sell|odds|could|may|might|"
                r"speculat\w*|urges?)\b"), -0.6),
            (_R(r"\b(complet\w*|closes|closed|close of|is done|finali[sz]\w*|approv\w*|review|"
                r"probe\w*|regulator\w*|stockholders|shareholders|vote|FCC|STB|board|"
                r"Acquisition (Corp|Announces|Merger)|SPAC|blank[- ]check|merger partner)\b"), -0.5),
            (_R(_MNA_BROKEN), 0.75)],
    "ipo_pricing": [(_R(r"\$\s?\d+(\.\d+)?\s?(billion|bn|B)\b|valu\w*|raises? \$"), 0.62)],
    "guidance": [(_R(r"\b(reaffirm\w*|affirm\w*|reiterat\w*|maintain\w*|long-term|sees .{0,30}growth)\b"), -0.35)],
    "earnings": [(_R(r"\b(beats?|misses?|missed|tops|EPS of|revenue of|reports? (Q[1-4]|results)|"
                     r"posts .{0,20}results|surpass\w*|record (revenue|quarter))\b"), 0.5),
                 (_R(r"\b(transcript|preview|imminent|ahead of|since last earnings|what to expect|"
                     r"earnings (date|call)|to report|will report|scheduled)\b"), -0.2)],
    "central_bank": [(_R(r"\b(cuts?|raises?|hikes?|holds?|leaves?|keeps?|decided to raise|decided to cut)"
                         r" (its |the )?(benchmark |policy |interest |key )*(lending )?rates?\b|"
                         r"\brates? (unchanged|on hold|steady)\b|\bemergency (cut|meeting)\b|"
                         r"\b(bps|basis[- ]points?) (cut|hike)\b|\b(cuts?|hikes?|raises?) .{0,20}"
                         r"(bps|basis points?)\b"), 0.75),
                     (_R(r"\b(could|may|might|expected|expects?|expectations?|likely|odds|bets?|should|"
                         r"would|seen|to (raise|hike|cut)|ahead of|before|await\w*|straight|"
                         r"consecutive|as expected|call|forecasts?|predicts?|if)\b"), -0.45)],
    "macro_data": [(re.compile(r"(?=.*\b(US|U\.S\.|USA|American)\b)(?=.*(\bCPI\b|(?i:nonfarm|payrolls|"
                               r"jobs report)|\bPCE\b|\bGDP\b|(?i:retail sales)|\bJOLTS\b))"
                               r"(?=.*(?i:\bactual\b|\brose\b|\bfell\b|\brises\b|\bfalls\b|\bjumps\b|"
                               r"\bslows\b|accelerat|\bcools\b|hotter|\bbeats?\b|\bmisses\b|came in|"
                               r"unexpectedly))"), 0.6)],
    "commodity": [(_R(r"\bOPEC\+?\b.{0,40}\b(cut|output|production|agree\w*|decid\w*)"), 0.62)],
    "legal_regulatory": [(_R(r"\b(antitrust|DOJ sues|FTC sues|ban(s|ned)?|blocks? (merger|deal)|"
                             r"indict\w*|breakup)\b"), 0.5)],
    "deal_partnership": [(_R(r"\$\s?\d+(\.\d+)?\s?(billion|bn)\b"), 0.55)],
    # ── Step 3 strong kinds. Order matters: later entries win. ──
    "mgmt_change": [
        # a top officer leaving on a schedule (retirement, announced succession)
        (_R(rf"\b{_MG_TOP}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,60}}\b{_MG_PLANNED}\b|\b{_MG_PLANNED}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,30}}\b{_MG_TOP}\b"), 0.65),
        # a new chief executive
        (_R(rf"\b{_MG_APPOINT}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,70}}\b{_MG_TOP}\b|\b(?:new|next|permanent|incoming) {_MG_TOP}\b"), 0.65),
        # finance chief / chair / operating chief leaving
        (_R(rf"\b{_MG_SEC}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,60}}\b{_MG_SELF}\b|\b{_MG_ABRUPT}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,25}}\b{_MG_SEC}\b"), 0.7),
        # the chief executive (or founder) leaving now
        (_R(rf"\b{_MG_TOP}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,60}}\b{_MG_SELF}\b|\b{_MG_ABRUPT}\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){{0,25}}\b{_MG_TOP}\b"), 0.85),
        (_SOFT_RE, -0.5),
    ],
    "guidance_cut": [(_R(r"\b(withdr\w+|suspend\w+|pulls?|pulled|scraps?|profit warning|slash\w+)\b"), 0.8)],
    "halt": [(_R(r"\bresum\w+\b"), -0.55)],
    "distress": [
        (_R(r"\b(fil(?:es|ed|ing) (?:for )?(?:voluntary )?(?:chapter|bankruptcy|creditor protection|insolvency)|"
            r"chapter (?:11|7)|bankruptcy (?:filing|protection|petition)|going[- ]concern|substantial doubt|"
            r"insolven\w+|receivership|ceas\w+ (?:all )?operations|to liquidate|defaults? on|in default|"
            r"miss\w* (?:an? )?(?:interest|debt|bond|coupon) payment)\b"), 0.85),
        (_R(r"\b(to be delisted|will be delisted|delisting determination|delist\w+ (?:from|its|the))\b"), 0.7),
        (_R(r"\b(notice|notification|letter|minimum bid|deficiency|extension|hearing|appeal|plan to regain|plans?|"
            r"voluntar\w+ delist\w*|transfer\w* (?:its )?listing)\b"), -0.5),
        (_R(r"\b(regain\w*|emerg\w+ from|exits? (?:chapter|bankruptcy)|complet\w+ (?:its )?(?:restructuring|"
            r"chapter)|avoid\w*|no longer|clos(?:es|ed) (?:the )?delisting|compliance with)\b"), -0.4),
        (_R(r"\bdelist\w*\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,80}\b(acquisition|merger|takeover|buyout|deal|going[- ]private|"
            r"completion|closing)\b|\b(acquisition|merger|takeover|buyout|deal)\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,60}\bdelist\w*\b"), -0.3),
        (_R(r"\b(odds|risk of|fears?|speculation|rumou?rs?|could|may|might|if|whether|den(?:y|ies|ied)|"
            r"potential|possible|possibly)\b"), -0.5),
        (_SOFT_RE, -0.5),
    ],
    "accounting": [
        (_R(r"\b(restat\w+|non-?reliance|relied (?:up)?on|auditors?\b(?:[^.;|?!]|\.(?=\d)|(?<=\b[A-Z])\.|(?<=\b(?:Inc|Ltd|Mrs))\.|(?<=\b(?:Co|Jr|Dr|Mr|Ms|St|No))\.|(?<=\b(?:Corp|Dept))\.){0,30}\b(?:resign\w*|quits?|withdraws?)|"
            r"accounting (?:fraud|irregularit\w+|scandal))\b"), 0.8),
        (_R(r"\b(material weakness\w*|remediat\w+)\b"), -0.5),
        # a routine switch of audit firm (nobody resigned or was dismissed)
        (_R(r"\b(change of auditors?|chang\w+ (?:its )?auditors?|appoints? (?:new )?auditors?|new auditors?)\b"), -0.5),
    ],
    "investigation": [(_R(r"\b(clos\w+|ends?|ended|drops?|dropped|conclud\w+|clear\w+|no (?:action|charges)|"
                          r"settl\w+|resolv\w+|dismiss\w+)\b"), -0.55),
                      (_SOFT_RE, -0.55)],
    "dividend_cut": [(_SOFT_RE, -0.5)],
    "buyback": [(_R(r"\b(announc\w+|authoriz\w+|approv\w+|launch\w+|new|increas\w+|expand\w+|boost\w+|adds?|"
                    r"\$\s?\d)\b"), 0.5),
                (_R(r"\$\s?\d+(\.\d+)?\s?(billion|bn|B)\b"), 0.6),
                (_R(r"\b(transactions?|weekly|daily|interim|periodic|execution|notification|week \d+|"
                    r"report on|disclosure of|update|status of|commences?|early start|complet\w+|progress)\b"), -0.2)],
    "contract_award": [(_R(r"\$\s?\d+(\.\d+)?\s?(billion|bn|B)\b"), 0.65)],
    "contract_loss": [(_R(r"\$\s?\d+(\.\d+)?\s?(billion|bn|B)\b|\b(largest|biggest|major|key|top) "
                          r"(customer|client|contract)\b"), 0.7)],
    "data_breach": [(_R(r"\b(ransomware|millions? (?:of )?(?:customers?|users?|records|accounts|patients)|"
                        r"operations (?:halted|disrupted|suspended)|systems? (?:down|offline)|stolen|shut\w* down)\b"), 0.7)],
    "labor_strike": [(_R(r"\b(ends?|ended|averted|avoid\w*|tentative|ratif\w+|approv\w+ (?:new )?contract|"
                         r"reach\w* (?:a )?(?:deal|agreement))\b"), -0.5)],
    "product_recall": [(_R(r"\b(Class I\b|deaths?|injur\w+|fires?|crash\w*|\d+(\.\d+)? ?million|"
                           r"\d{3},\d{3})"), 0.7)],
    "product_launch": [(_R(r"\b(new (chip|model|phone|device)|unveil\w*)\b"), 0.45)],
}
_CN_SIDE = r"\b(China|Chinese|Beijing|Xi|He Lifeng|Li Qiang|Wang Yi)\b"
_US_SIDE = r"\b(Trump|U\.S\.|US|USA|United States|Washington|White House|Bessent|Rubio|American|Greer|Lutnick)\b"
# geopolitical / trade_policy on the big transmission themes → ≥0.7
_BIG_GEO_RE = re.compile(
    rf"({_CN_SIDE}.*{_US_SIDE})|({_US_SIDE}.*{_CN_SIDE})|(?i:"
    r"\b(imposes?|announces?|raises?|hikes?|slaps?|threatens?|lifts?|removes?|cuts?|delays?|pauses?|"
    r"new|retaliatory|reciprocal|sweeping)\b.{0,30}\btariffs?\b|\btariffs?\b.{0,30}\b(take effect|"
    r"hike|increase)|\b(new|tightens?|expands?|imposes?|eases?|lifts?|loosens?) .{0,20}export "
    r"(controls?|curbs|restrictions)|\b(invades?|begins? (an )?invasion|launch\w* (an )?invasion|"
    r"declares? war|nuclear (test|strike)|Strait of Hormuz|Taiwan Strait)\b|\bOPEC\+?\b)")
# company-specific restrictions keep a company as the subject of a geo/trade event
_CO_RESTRICT_RE = re.compile(r"(?i)\b(export (controls?|curbs|ban|licen[cs]es?)|entity list|blacklist\w*|"
                             r"sanction\w*|banned|ban on|tariffs? on (its|the company))\b")
_TIER_W = {1: 1.0, 2: 1.0, 3: 0.9}


_OFFERING_RE = re.compile(
    r"(?i)\b(follow-on offering|secondary offering|public offering|stock offering|equity offering|"
    r"share offering|underwritten offering|registered direct|at-the-market|ATM program|"
    r"prices? .{0,40}offering|offering of [\d,.]+ (m(ln|illion)? )?shares|"
    r"registration statement \(possible offering\)|dilut\w*)\b")
_PM_MACRO_RE = re.compile(
    r"(?i)\b(Fed|FOMC|rate (cut|hike)s?|interest rates?|recession|inflation|CPI|GDP|tariffs?|trade deal|"
    r"China|Xi|Taiwan|Russia|Ukraine|Iran|Israel|war|ceasefire|shutdown|default|debt ceiling|"
    r"election|president|Senate|House|OPEC|oil|bitcoin|S&P|Nasdaq|unemployment|jobs)\b")
_PM_TRIVIA_RE = re.compile(
    r"(?i)\b(NFL|NBA|MLB|NHL|UFC|FIFA|World Cup|Super Bowl|Oscars?|Grammys?|Emmys?|album|movie|"
    r"box office|tweet|celebrity|Taylor Swift|win the .{0,20}(game|match|series|cup|championship))\b")


def _pm_materiality(text: str, st: dict) -> float:
    """Prediction-market odds swing → materiality by the SIZE of the move and the
    MACRO relevance of the market. 10 pts ≈ 0.35 … 40+ pts ≈ 0.65 (+0.1 macro)."""
    try:
        d = abs(float(st.get("delta_pts")))
    except Exception:
        mm = re.search(r"([+-]?\d+(?:\.\d+)?) pts", text or "")
        d = abs(float(mm.group(1))) if mm else 10.0
    q = f"{st.get('question') or ''} {text or ''}"
    m = 0.35 + 0.01 * max(0.0, min(30.0, d - 10.0))
    if _PM_TRIVIA_RE.search(q):
        return round(min(m, 0.15), 3)
    if _PM_MACRO_RE.search(q):
        m += 0.1
    return round(min(m, 0.75), 3)


def market_materiality(kind: str, headline: str, *, tier: int = 2, structured: dict = None,
                       qwen_market: float = None, book_mat: float = None,
                       has_ticker: bool = False, _raw_floor: bool = False,
                       is_structured: bool = False) -> float:
    """0..1 MARKET-relative materiality for one event (how much it should move the
    named instrument / sector, for anyone). Deterministic; qwen reads only lift it
    halfway toward their value. A commentary headline (non_event_reason) is capped
    at catalyst.triage.non_event_ceiling whatever the model said. `_raw_floor=True`
    returns the deterministic part only (no model input, no ceiling). Never raises."""
    try:
        text = _clean(headline)
        try:
            _c = _cfg()
            _ov = _c.get("kind_floors") or {}
        except Exception:
            _c, _ov = {}, {}
        m = _FLOORS.get(kind, 0.1)
        if kind in _ov:
            try:
                m = max(0.0, min(1.0, float(_ov[kind])))
            except Exception:
                pass
        for rx, v in _BOOSTS.get(kind, []):
            if rx.search(text):
                m = min(m, -v) if v < 0 else max(m, v)
        if kind in ("geopolitical", "trade_policy") and _BIG_GEO_RE.search(text):
            m = max(m, 0.7)
        if kind in ("geopolitical", "trade_policy") and has_ticker:
            # a company caught in a world event is its story only when it is the
            # direct target (export ban, entity list, sanctions)
            m = max(m, 0.7) if _CO_RESTRICT_RE.search(text) else min(m, 0.5)
        st = structured or {}
        if kind == "app_rank":
            try:
                rank = int(st.get("rank")) if st.get("rank") is not None else None
            except Exception:
                rank = None
            if rank is not None:
                m = 0.72 if rank <= 1 else (0.6 if rank <= 5 else (0.5 if rank <= 10 else 0.4))
            if st.get("private"):
                m = min(m, 0.2)    # ChatGPT/OpenAI #1: a competitive signal, not a tradeable subject
        if kind == "prediction_market":
            m = _pm_materiality(text, st)
        if kind == "other" and (st.get("offering") == "follow_on" or _OFFERING_RE.search(text)):
            m = max(m, 0.45)       # dilution: moderate, bearish-leaning
        dl = st.get("deal") if isinstance(st, dict) else None
        if isinstance(dl, dict) and dl.get("floor"):
            # news phase 6b: a press-wire deal with a big listed partner (mason_deals; the floor is only
            # written while news.deals.big_partner_floor is above 0, so without it nothing changes here)
            try:
                m = max(m, max(0.0, min(0.95, float(dl["floor"]))))
            except (TypeError, ValueError):
                pass
        m *= _TIER_W.get(int(tier or 2), 1.0)
        if _raw_floor:
            return round(max(0.0, min(1.0, m)), 3)
        for q in (qwen_market, book_mat):
            if q is not None:
                try:
                    q = max(0.0, min(1.0, float(q)))
                    m = max(m, (m + q) / 2.0)
                except Exception:
                    pass
        if _c.get("non_event_ceiling_enabled", True) and not is_structured:
            ne = non_event_reason(text, company=has_ticker)
            if ne:
                m = min(m, float(_c.get("solicitation_ceiling", 0.15) if ne == "solicitation"
                                 else _c.get("non_event_ceiling", 0.3)))
        return round(max(0.0, min(1.0, m)), 3)
    except Exception:
        return 0.0


# ── macro themes (story key for ticker-less stories) ─────────────────────────────
_THEMES = [
    ("us-china", re.compile(r"\b(China|Chinese|Beijing|Xi|Xi Jinping|Taiwan\w*|He Lifeng|Li Qiang)\b")),
    ("russia-ukraine", re.compile(r"\b(Russia\w*|Putin|Kremlin|Moscow|Ukrain\w*|Zelensky\w*|Kyiv)\b")),
    ("middle-east", re.compile(r"\b(Iran\w*|Tehran|Israel\w*|Gaza|Hamas|Hezbollah|Houthi\w*|Yemen|"
                               r"Saudi\w*|Middle East|Hormuz|Red Sea|Syria\w*|Lebanon|Netanyahu)\b")),
    ("korea", re.compile(r"\b(North Korea\w*|Pyongyang|Kim Jong)\b")),
    ("venezuela", re.compile(r"\bVenezuela\w*\b")),
    ("india-pakistan", re.compile(r"\b(India|Pakistan)\b")),
    ("europe", re.compile(r"\b(EU|European Union|Brussels|Europe)\b")),
    ("north-america", re.compile(r"\b(Mexico|Canada)\b")),
]
_CB_THEMES = [("fed", r"\b(Fed|FOMC|Federal Reserve|Powell|Warsh|Fed's)\b"), ("ecb", r"\b(ECB|Lagarde)\b"),
              ("boj", r"(?i)\b(BOJ|Bank of Japan)\b"), ("boe", r"(?i)\b(Bank of England|BoE)\b"),
              ("pboc", r"(?i)\bPBOC\b"), ("boc", r"(?i)\b(Bank of Canada|BoC)\b"),
              ("rba", r"\bRBA\b"), ("snb", r"\bSNB\b")]
_DATA_THEMES = [("cpi", r"\b(CPI|PCE|PPI|inflation)\b"), ("jobs", r"(?i)\b(nonfarm|payrolls|jobs report|"
                r"jobless|unemployment|NFP|JOLTS)\b"), ("gdp", r"\bGDP\b"),
                ("pmi", r"\b(ISM|PMI)\b"), ("consumer", r"(?i)\b(retail sales|consumer)\b")]
_CMDTY_THEMES = [("oil", r"(?i)\b(OPEC\+?|crude|oil|Brent|WTI|gasoline)\b"), ("gas", r"(?i)\bnatural gas|LNG\b"),
                 ("metals", r"(?i)\b(gold|silver|copper)\b"), ("ags", r"(?i)\b(wheat|soybeans?|corn|cotton|grains?)\b")]


def _macro_theme(kind: str, headline: str) -> str:
    text = _clean(headline)
    fam = kind_family(kind)
    if fam == "geo":
        for name, rx in _THEMES:
            if rx.search(text):
                if name == "us-china":       # summit vs trade vs security are distinct stories
                    if re.search(r"(?i)\b(summit|meeting|meets?|visit\w*|dinner|red carpet|state visit|"
                                 r"Bessent|He Lifeng|delegation|arriv\w*)\b", text):
                        return "us-china-summit"
                    if re.search(r"(?i)\b(Taiwan\w*|military|militar\w*|navy|missile|drills?|spy|"
                                 r"South China Sea)\b", text):
                        return "us-china-security"
                    return "us-china-trade"
                return name
        return "tariffs" if re.search(r"(?i)\btariff", text) else "world"
    if fam == "ipo":                  # a private issuer: key on its name ("oura")
        m = re.match(r"\s*(.+?)\s+(files|filed|prices|priced|sets|launches|plans|to |opens|debuts|"
                     r"targets|seeks|IPO|\()", text)
        name = _norm_name(m.group(1)) if m else ""
        return re.sub(r"[^a-z0-9]+", "-", name)[:40].strip("-") or "unnamed"
    if fam == "pm":                   # odds markets join the world theme they are about
        for name, rx in _THEMES:
            if rx.search(text):
                return name
        for name, p in _CB_THEMES + _CMDTY_THEMES:
            if re.search(p, text):
                return name
        q = re.search(r'"([^"]{4,})"', text)
        toks = re.findall(r"[a-z]{4,}", (q.group(1) if q else text).lower())
        return "-".join(toks[:3]) or "general"
    for fam_k, table in (("cb", _CB_THEMES), ("macro", _DATA_THEMES), ("cmdty", _CMDTY_THEMES)):
        if fam == fam_k:
            for name, p in table:
                if re.search(p, text):
                    return name
            return "general"
    toks = sorted({w.lower() for w in re.findall(r"[A-Za-z]{5,}", text)
                   if w.lower() not in ("stock", "stocks", "market", "markets", "shares")})
    return "-".join(toks[:2]) or "general"


def story_key(primary_ticker, event_kind: str, headline: str) -> str:
    """Deterministic cluster key. Ticker stories key on ticker + kind family
    ('t:META:product'); ticker-less stories on family + macro theme
    ('m:geo:us-china', 'm:cb:fed', 'm:cmdty:oil')."""
    fam = kind_family(event_kind)
    if fam == "news" and _OFFERING_RE.search(_clean(headline)):
        fam = "offering"
    if primary_ticker:
        return f"t:{str(primary_ticker).upper()}:{fam}"
    return f"m:{fam}:{_macro_theme(event_kind, headline or '')}"


# ── tape confirmation (Alpaca snapshot → yfinance fallback; historical for replay) ──
_tape_cache = {}           # key → (ts, dict)
_hist_cache = {}           # ticker → pandas DataFrame of daily bars (replay mode)


def _alpaca_creds():
    """(key, secret) from mason.config.json paper_trading — the same source
    mason_news_service._alpaca_creds reads (mason_app is never imported)."""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    try:
        import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
        return _S.alpaca_keys(cfg=cfg)
    except Exception:
        return "", ""


def _et(now_epoch: float) -> datetime:
    """`now_epoch` as New York wall-clock time (zoneinfo: correct in winter and
    summer). Falls back to a date-based EST/EDT rule only if tz data is missing."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now_epoch, ZoneInfo("America/New_York"))
    except Exception:
        u = datetime.fromtimestamp(now_epoch, timezone.utc)
        # US rule: second Sunday of March 07:00 UTC → first Sunday of November 06:00 UTC
        mar = datetime(u.year, 3, 8, 7, tzinfo=timezone.utc)
        mar += timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(u.year, 11, 1, 6, tzinfo=timezone.utc)
        nov += timedelta(days=(6 - nov.weekday()) % 7)
        return u.astimezone(timezone(timedelta(hours=-4 if mar <= u < nov else -5)))


def _session_frac(now_epoch: float) -> float:
    """Elapsed fraction of the regular session (New York time), for scaling
    today's partial volume. 1.0 outside the session."""
    et = _et(now_epoch)
    mins = et.hour * 60 + et.minute - 570
    if et.weekday() >= 5 or mins <= 0 or mins >= 390:
        return 1.0
    return max(0.1, mins / 390.0)


def _alpaca_tape(tickers: list, now_epoch: float) -> dict:
    import requests
    key, secret = _alpaca_creds()
    if not key or not secret:
        return {}
    r = requests.get("https://data.alpaca.markets/v2/stocks/snapshots",
                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                     params={"symbols": ",".join(tickers), "feed": "iex"}, timeout=8)
    if r.status_code != 200:
        return {}
    body = r.json() or {}
    snaps = body.get("snapshots", body)
    out = {}
    frac = _session_frac(now_epoch)
    for tk in tickers:
        s = snaps.get(tk) if isinstance(snaps, dict) else None
        if not isinstance(s, dict):
            continue
        lt, db, pdb = s.get("latestTrade") or {}, s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
        px, prev = lt.get("p") or db.get("c"), pdb.get("c")
        if not px or not prev:
            continue
        rv = None
        if db.get("v") and pdb.get("v"):
            rv = round(float(db["v"]) / (float(pdb["v"]) * frac), 2)
        out[tk] = {"chg_pct": round((px - prev) / prev * 100, 2), "rel_volume": rv}
    return out


def _yf_hist(ticker: str):
    import yfinance as yf
    df = _hist_cache.get(ticker)
    if df is None:
        import mason_symbols
        df = yf.Ticker(mason_symbols.yf_symbol(ticker)).history(period="6mo", interval="1d", auto_adjust=False)
        _hist_cache[ticker] = df
    return df


def _yf_tape(tickers: list, now_epoch: float) -> dict:
    """Daily-bar tape AS OF `now_epoch`: the last session that had CLOSED by then
    (16:00 ET), vs its prior close; rel volume vs the 20 sessions before it.
    Used as the live fallback and for historical replays (no lookahead)."""
    out = {}
    cutoff = _et(now_epoch)
    for tk in tickers:
        try:
            df = _yf_hist(tk)
            if df is None or len(df) < 3:
                continue
            idx = [d for d in df.index
                   if (d.tz_convert("America/New_York") if d.tzinfo else d).date() < cutoff.date()
                   or ((d.tz_convert("America/New_York") if d.tzinfo else d).date() == cutoff.date()
                       and cutoff.hour >= 16)]
            if len(idx) < 2:
                continue
            sub = df.loc[:idx[-1]]
            c, pc = float(sub["Close"].iloc[-1]), float(sub["Close"].iloc[-2])
            vol = float(sub["Volume"].iloc[-1])
            base = sub["Volume"].iloc[-21:-1]
            rv = round(vol / float(base.mean()), 2) if len(base) and float(base.mean()) > 0 else None
            out[tk] = {"chg_pct": round((c - pc) / pc * 100, 2), "rel_volume": rv,
                       "session": str(idx[-1].date())}
        except Exception:
            continue
    return out


def tape_for(ticker: str, *, now_epoch: float = None, historical: bool = None) -> dict:
    """{move_pct, vs_sector_pct, sector_etf, rel_volume, confirmed, as_of, source}
    for a ticker, or {} if unavailable. Live (now within the last day): Alpaca IEX
    snapshot, yfinance fallback. Historical: yfinance daily as-of. Cached ~5 min
    (live) / per session (historical). Never raises."""
    try:
        now_epoch = float(now_epoch or time.time())
        tk = (ticker or "").upper()
        if not tk:
            return {}
        etf = sector_etf(tk)
        if historical is None:
            historical = (time.time() - now_epoch) > 86400
        ttl = float(_cfg().get("tape_ttl_seconds", 300))
        ckey = (tk, datetime.fromtimestamp(now_epoch).strftime("%Y-%m-%d %H")) if historical else tk
        c = _tape_cache.get(ckey)
        if c and (historical or time.time() - c[0] < ttl):
            return dict(c[1])
        syms = [tk] if etf == tk else [tk, etf]
        data, src = {}, None
        if not historical:
            try:
                data = _alpaca_tape(syms, now_epoch)
                src = "alpaca_iex" if tk in data else None
            except Exception:
                data = {}
        if tk not in data:
            data = _yf_tape(syms, now_epoch)
            src = "yfinance_daily"
        if tk not in data:
            _tape_cache[ckey] = (time.time(), {})
            return {}
        mv = data[tk]["chg_pct"]
        sec = (data.get(etf) or {}).get("chg_pct")
        rel = round(mv - sec, 2) if sec is not None else mv
        rv = data[tk].get("rel_volume")
        confirmed = abs(rel) >= 2.0 or (rv is not None and rv >= 2.0 and abs(rel) >= 1.0)
        out = {"move_pct": mv, "sector_etf": etf, "sector_move_pct": sec, "vs_sector_pct": rel,
               "rel_volume": rv, "confirmed": bool(confirmed), "source": src,
               "session": data[tk].get("session"),
               "as_of": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(timespec="seconds")}
        _tape_cache[ckey] = (time.time(), out)
        return dict(out)
    except Exception:
        return {}


# ── story scoring ────────────────────────────────────────────────────────────────

def _tape_bonus(tape: dict) -> float:
    if not tape:
        return 0.0
    rel = abs(float(tape.get("vs_sector_pct") or 0))
    rv = tape.get("rel_volume") or 0
    if rel >= 4.0 or (rel >= 2.0 and rv >= 2.0):
        return 0.12
    if rel >= 2.0 or (rel >= 1.0 and rv >= 1.5):
        return 0.06
    return 0.0


def _velocity_bonus(vel: float, recent_w: float) -> float:
    if vel >= 8 and recent_w >= 8:
        return 0.15
    if vel >= 4 and recent_w >= 5:
        return 0.1
    if vel >= 2 and recent_w >= 3:
        return 0.05
    return 0.0


def story_score(*, base: float, n_sources: int, velocity: float, recent_w: float,
                tape: dict = None, analyst_48h: int = 0, in_broad: bool = True,
                is_ticker: bool = True, family: str = None, calendar: bool = False) -> tuple:
    """(score 0..1, parts). base = max linked-event market materiality; lifted by a
    ≥3 same-direction analyst-action cluster (48h), source breadth, attention
    velocity, tape; a name outside S&P/NDX/book is shaded ×0.85. A pure
    analyst-action story is capped at 0.62 unless the tape confirms it."""
    b = float(base or 0)
    if analyst_48h >= 3:
        b = max(b, 0.55)
    breadth = min(0.12, 0.04 * max(0, int(n_sources or 0) - 1))
    vb = _velocity_bonus(float(velocity or 0), float(recent_w or 0))
    tb = _tape_bonus(tape or {}) if is_ticker else 0.0
    uf = 1.0 if (in_broad or not is_ticker) else 0.85
    cb = 0.05 if (calendar and b >= 0.4) else 0.0     # a scheduled catalyst within 7 days
    raw_s = b + breadth + vb + cb
    if family == "street" and not (tape or {}).get("confirmed"):
        raw_s = min(raw_s, 0.62)
    s = round(max(0.0, min(1.0, (raw_s + tb) * uf)), 3)
    return s, {"base": round(b, 3), "breadth": round(breadth, 3), "velocity": vb,
               "tape": tb, "calendar": cb, "universe_factor": uf}


# ── velocity index (built once per run from recent triaged rows) ─────────────────

class _AttentionIndex:
    """ticker/story-key → [(ingested_at, source)] for rows in the baseline window."""

    def __init__(self, conn, now: float, cfg: dict):
        self.now = now
        self.recent_h = float(cfg.get("velocity_window_hours", 6))
        self.base_d = float(cfg.get("baseline_days", 7))
        self.by_tk, self.by_cat = {}, {}
        lo = now - self.base_d * 86400 - self.recent_h * 3600
        rows = conn.execute(
            "SELECT resolved_tickers, catalyst_id, source, ingested_at FROM news_events "
            "WHERE ingested_at > ? AND ingested_at <= ? AND triaged = 1",
            (lo, now)).fetchall()
        for r in rows:
            self.add(r["resolved_tickers"], r["catalyst_id"], r["source"], r["ingested_at"])

    def add(self, resolved, catalyst_id, source, ts):
        try:
            tks = json.loads(resolved) if isinstance(resolved, str) else (resolved or [])
        except Exception:
            tks = []
        for t in tks or []:
            self.by_tk.setdefault(t, []).append((ts, source))
        if catalyst_id:
            self.by_cat.setdefault(catalyst_id, []).append((ts, source))

    def velocity(self, series) -> tuple:
        """(ratio, recent_weighted). recent_w = distinct sources in the recent window
        + 0.25 per extra event; baseline = prior-window events per recent-window slot."""
        cut = self.now - self.recent_h * 3600
        recent = [(t, s) for t, s in (series or []) if t > cut]
        older = [(t, s) for t, s in (series or []) if t <= cut]
        srcs = {s for _, s in recent}
        recent_w = len(srcs) + 0.25 * max(0, len(recent) - len(srcs))
        slots = max(1.0, self.base_d * 24.0 / self.recent_h)
        baseline = len(older) / slots
        return round(recent_w / max(baseline, 0.5), 2), round(recent_w, 2)


# ── main pass ────────────────────────────────────────────────────────────────────

def _parse_json(s, default):
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return default if v is None else v
    except Exception:
        return default


def _direction(headline: str, qdir: str = None) -> str:
    if qdir in ("bullish", "bearish"):
        return qdir
    if re.match(r"\s*(Polymarket|Kalshi)\b", headline or ""):
        return "neutral"               # odds headlines: 'rate cut' is not bearish news
    if _OFFERING_RE.search(_clean(headline)):
        return "bearish"               # dilution
    try:
        import mason_news
        if mason_news._RED.search(headline or ""):
            return "bearish"
        if mason_news._GREEN.search(headline or ""):
            return "bullish"
    except Exception:
        pass
    return "neutral"


def _story_limits(cfg: dict, now: float) -> tuple:
    """(statuses a NEW event may not join, oldest allowed first_seen epoch)."""
    closed = ["expired"]
    if cfg.get("no_attach_consumed", True):
        closed.append("consumed")
    try:
        days = float(cfg.get("story_max_age_days", 5) or 0)
    except Exception:
        days = 5.0
    return closed, (now - days * 86400 if days > 0 else 0.0)


def _story_has_kind(cat: dict, kind: str) -> bool:
    if cat.get("event_kind") == kind:
        return True
    try:
        return kind in ((_parse_json(cat.get("extra_json"), {}) or {}).get("kinds") or {})
    except Exception:
        return False


def _find_story(conn, key: str, now: float, window_h: float, cache: dict, cfg: dict = None,
                kind: str = None, strong: bool = False):
    """The live story a new event with `key` may join, or None (⇒ start a new one).
    Never a story the director already consumed, never an expired one, never one
    whose first headline is older than story_max_age_days. A STRONG event only
    joins a story that already holds its own kind."""
    cfg = cfg if cfg is not None else _cfg()
    need_kind = kind if (strong and kind and cfg.get("strong_kind_new_story", True)) else None
    ckey = (key, need_kind)
    if ckey in cache:
        return cache[ckey]
    closed, oldest = _story_limits(cfg, now)
    rows = conn.execute(
        f"SELECT * FROM catalysts WHERE story_key=? AND record_deleted=0 "
        f"AND status NOT IN ({','.join('?' * len(closed))}) AND last_seen >= ? "
        f"AND COALESCE(first_seen, last_seen) >= ? ORDER BY last_seen DESC LIMIT 8",
        (key, *closed, now - window_h * 3600, oldest)).fetchall()
    found = None
    for row in rows:
        d = dict(row)
        if need_kind is None or _story_has_kind(d, need_kind):
            found = d
            break
    cache[ckey] = found
    return found


def _open_ticker_story(conn, ticker: str, now: float, window_h: float, cfg: dict = None):
    """The ticker's best live story for an unclassified ('other') headline — under
    the same limits as _find_story (not consumed, not expired, not past the age cap)."""
    cfg = cfg if cfg is not None else _cfg()
    closed, oldest = _story_limits(cfg, now)
    row = conn.execute(
        f"SELECT * FROM catalysts WHERE primary_ticker=? AND record_deleted=0 "
        f"AND status NOT IN ({','.join('?' * len(closed))}) AND last_seen >= ? "
        f"AND COALESCE(first_seen, last_seen) >= ? ORDER BY score DESC, last_seen DESC LIMIT 1",
        (ticker, *closed, now - window_h * 3600, oldest)).fetchone()
    return dict(row) if row else None


def _ticker_work_ok(ticker: str, cfg: dict, book: set) -> bool:
    """Step 3 / R10: may this symbol carry a story? Book names always; otherwise it
    must be a known US listing that is not remembered as dead (mason_symbols)."""
    try:
        if not ticker or not cfg.get("ticker_gate_enabled", True) or ticker in book:
            return True
        import mason_symbols
        return bool(mason_symbols.work_ok(ticker))
    except Exception:
        return True


def run_triage(conn=None, *, cap: int = 400, now: float = None) -> dict:
    """Triage untriaged news_events (newest `cap`, processed oldest→newest):
    resolve → classify → market materiality → cluster into catalysts → recompute
    story stats → alert once. Behind catalyst.triage.enabled (off ⇒ immediate
    no-op). `now` (epoch) exists for historical replays. Never raises."""
    empty = {"triaged": 0, "new_catalysts": 0, "updated": 0, "alerts": []}
    try:
        cfg = _cfg()
        if not cfg.get("enabled"):
            return empty
    except Exception:
        return empty
    own = conn is None
    try:
        if own:
            conn = mason_db.get_connection()
        return _run(conn, cfg, int(cap), float(now or time.time()))
    except Exception as e:
        log.warning(f"[triage] run failed: {e}")
        return empty
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run(conn, cfg, cap, now) -> dict:
    window_h = float(cfg.get("story_window_hours", 72))
    lookback = now - float(cfg.get("lookback_hours", 72)) * 3600
    rows = conn.execute(
        "SELECT id, headline, source, source_tier, tickers, category, materiality, direction, "
        "scored, raw, ingested_at, published_at, catalyst_id FROM news_events "
        "WHERE triaged = 0 AND ingested_at > ? AND ingested_at <= ? "
        "ORDER BY ingested_at DESC LIMIT ?", (lookback, now, cap)).fetchall()
    res = {"triaged": 0, "new_catalysts": 0, "updated": 0, "alerts": []}
    if not rows:
        with conn:
            _expire(conn, now, window_h, cfg)
        return res
    rows = sorted(rows, key=lambda r: r["ingested_at"])
    _TAPE_BUDGET["left"] = int(cfg.get("max_tape_lookups", 6))
    att = _AttentionIndex(conn, now, cfg)
    broad = set(broad_universe())
    try:
        _h, _w = _book_sets()
        book = {t.upper() for t in (_h | _w)}
    except Exception:
        book = set()
    try:                           # Mason's own open trades are the book too (review fix)
        for _r in conn.execute("SELECT DISTINCT ticker FROM agent_trades WHERE status='open' "
                               "AND COALESCE(record_deleted,0)=0 AND ticker IS NOT NULL").fetchall():
            if _r[0]:
                book.add(str(_r[0]).upper())
    except Exception:
        pass
    try:                           # … and the positions synced from the brokerage (personal_positions)
        for _r in conn.execute("SELECT DISTINCT ticker FROM personal_positions WHERE status='open' "
                               "AND COALESCE(is_deleted,0)=0 AND ticker IS NOT NULL").fetchall():
            if _r[0]:
                book.add(str(_r[0]).upper())
    except Exception:
        pass
    story_cache, touched, new_ids = {}, set(), set()
    loud = set()             # catalysts touched by at least one event that may alert
    strong_new = {}          # catalyst id -> {strong kind: headline} that arrived in this run
    with conn:
        for r in rows:
            raw = _parse_json(r["raw"], {})
            if not isinstance(raw, dict):
                raw = {}
            headline = r["headline"] or ""
            structured = raw.get("structured") if isinstance(raw.get("structured"), dict) else {}
            is_struct = bool(raw.get("catalyst_source"))
            try:
                resolved, private_leads = _resolve(_clean(headline), _clean(raw.get("desc") or ""))
            except Exception:
                resolved, private_leads = [], False
            tagged = [t.upper() for t in _parse_json(r["tickers"], []) if t]
            # primary: structured source's explicit ticker > headline resolution >
            # a short explicit tag list (Benzinga symbols not named in the text).
            # A market-wrap listing ≥3 tickers has no single subject.
            roundup = bool(_ROUNDUP_RE.search(_clean(headline))) and (
                len(resolved) >= 3 or len(re.findall(r"\b[A-Z]{2,5}\b(?=,| and | Stocks)", headline)) >= 3)
            # a market wrap ("Dow Jones Futures Rise After … Nvidia CEO Huang's Exit") is about
            # the market; the company it mentions is not its subject
            wrap = (not is_struct) and is_market_wrap(headline)
            if wrap:
                roundup = True
            if is_struct and tagged:
                primary = tagged[0]
            elif roundup or private_leads:
                primary = None     # a basket, or a private company's story (OpenAI, Anthropic…)
            elif resolved:
                primary = resolved[0]
            elif tagged and len(tagged) <= 2 and r["category"] != "macro":
                primary = tagged[0]
            else:
                primary = None
            all_tks = list(dict.fromkeys(resolved + [t for t in tagged if t in resolved or is_struct
                                                      or primary == t]))
            if primary and primary not in all_tks:
                all_tks.insert(0, primary)
            if primary in _INDEX_ETFS:
                primary = None     # SPY/QQQ tags on market-wide items are not a subject
            if primary and not is_struct and not r["catalyst_id"] and _nq_on("symbol_gate"):
                # 2026-11-15: not a part supplier, and a vendor tag must be named in the text
                primary = _nq_subject(headline, raw.get("desc") or "", primary, resolved)
                if primary is None:
                    all_tks = [t for t in all_tks if t in resolved]
            if primary and not r["catalyst_id"] and not _ticker_work_ok(primary, cfg, book):
                primary = None     # not a US listing / remembered as dead: no story, no dossier work
            kind = classify_kind(headline, raw.get("event_kind_hint"), has_ticker=bool(primary))
            if primary and not is_struct:
                # world events belong to the theme story; the company stays in
                # resolved_tickers for the dossier's transmission map
                if kind in ("central_bank", "macro_data"):
                    primary = None
                elif kind in ("geopolitical", "trade_policy") and not _CO_RESTRICT_RE.search(_clean(headline)):
                    primary = None
                elif kind == "commodity" and primary not in resolved:
                    primary = None
            if headline.startswith("[TEST]"):
                kind, primary = "other", None      # fixture rows seen in the live DB
            book_mat = None
            if r["scored"] and r["category"] in ("holding", "watchlist") and r["materiality"] is not None:
                book_mat = r["materiality"]
            mm = market_materiality(kind, headline, tier=r["source_tier"] or 2,
                                    structured=structured, qwen_market=raw.get("qwen_market"),
                                    book_mat=book_mat, has_ticker=bool(primary),
                                    is_structured=is_struct)
            if wrap and (resolved or tagged) and cfg.get("non_event_ceiling_enabled", True):
                # a market wrap that names a company: whatever a model said about it, it
                # is not news about that company
                mm = min(mm, float(cfg.get("non_event_ceiling", 0.3)))
            bounds = event_bounds(headline, kind=kind, has_ticker=bool(primary),
                                  tier=r["source_tier"] or 2, structured=is_struct, cfg=cfg)
            is_strong = bool(bounds.get("strong"))
            if bounds.get("background") and not is_struct and mm > 0.5:
                # "… since Buffett announced he would step down", "ousted CEO testifies": the
                # strong event is only referred to — it must not lend its floor to this line
                mm = round(max(0.5, min(mm, float(market_materiality(
                    "other", headline, tier=r["source_tier"] or 2, qwen_market=raw.get("qwen_market"),
                    book_mat=book_mat, has_ticker=bool(primary))))), 3)
            # macro kinds with a ticker (e.g. "Boeing falls ahead of Trump-Xi") stay
            # on the ticker; ticker-less 'other' / weak items never open a story
            if kind_family(kind) == "cb" and not primary and _macro_theme(kind, headline) == "general":
                mm = min(mm, 0.45)     # an unnamed / minor central bank
            cat = None
            if r["catalyst_id"]:
                cat = conn.execute("SELECT * FROM catalysts WHERE id=?", (r["catalyst_id"],)).fetchone()
                cat = dict(cat) if cat else None
            if cat is None:
                if not primary and kind_family(kind) not in _TICKERLESS_OK:
                    cat = None     # a company-kind event with no resolvable company: no story
                elif (kind == "other" or kind_family(kind) == "news") and \
                        not (primary and _OFFERING_RE.search(_clean(headline))):
                    if primary:
                        cat = _open_ticker_story(conn, primary, now, window_h, cfg)
                        if cat is None:
                            ser = att.by_tk.get(primary, []) + [(r["ingested_at"], r["source"])]
                            vel, rw = att.velocity(ser)
                            if vel >= 4 and rw >= 6:          # attention-only story
                                cat = _new_story(conn, story_key(primary, "other", headline),
                                                 primary, "other", headline, r, now, window_h, cfg)
                                new_ids.add(cat["id"])
                else:
                    key = story_key(primary, kind, headline)
                    cat = _find_story(conn, key, now, window_h, story_cache, cfg,
                                      kind=kind, strong=is_strong)
                    if cat is None:
                        cat = _new_story(conn, key, primary, kind, headline, r, now, window_h, cfg)
                        # later events of this run find it (with or without the kind rule)
                        story_cache[(key, None)] = cat
                        story_cache[(key, kind)] = cat
                        new_ids.add(cat["id"])
            cid = cat["id"] if cat else None
            conn.execute(
                "UPDATE news_events SET event_kind=?, market_materiality=?, resolved_tickers=?, "
                "catalyst_id=?, triaged=1 WHERE id=?",
                (kind, mm, json.dumps(all_tks), cid, r["id"]))
            att.add(all_tks, cid, r["source"], r["ingested_at"])
            for _src, _ts in _also_sources(raw):       # other outlets that ran the same headline
                att.add(all_tks, cid, _src, _ts or r["ingested_at"])
            if cid:
                touched.add(cid)
                # a headline read back long after it was published (stream backfill)
                # joins its story but must not, by itself, ring the bell
                if not _is_quiet(raw, r, now, cfg):
                    loud.add(cid)
                    if is_strong:
                        # per kind, the STRONGEST event of this run speaks for it ("names an
                        # interim CEO" 0.65 and "CEO resigns" 0.85 can arrive in the same pass)
                        d = strong_new.setdefault(cid, {})
                        fl = float(bounds.get("floor") or 0)
                        if kind not in d or fl > float(d[kind].get("floor") or 0):
                            d[kind] = {"h": headline, "floor": fl}
                    elif is_struct and mm >= 0.7 and not bounds.get("non_event"):
                        # a primary-source item (an app-store #1, a filing, an FDA notice)
                        # that is material by itself is as solid as a strong headline
                        d = strong_new.setdefault(cid, {})
                        d["_solid"] = max(float(d.get("_solid") or 0), float(mm))
            res["triaged"] += 1
        # phase 1 (inside the write transaction): re-aggregate WITHOUT any price lookup
        plans = {}
        for cid in touched:
            plans[cid] = _recompute(conn, cid, now, cfg, att, broad, strong_new.get(cid), fetch_tape=False)
        _expire(conn, now, window_h, cfg)
    # phase 2 (no transaction open): the price lookups are network calls — holding the
    # write lock across them made the scorer thread time out and lose a model batch
    tapes = {}
    for cid in touched:
        tk = (plans.get(cid) or {}).get("tape_for")
        if not tk or _TAPE_BUDGET["left"] <= 0:
            continue
        _TAPE_BUDGET["left"] -= 1              # bounded uncached lookups per run
        try:
            tapes[cid] = tape_fn(tk, now_epoch=now) or {}
        except Exception:
            tapes[cid] = {}                    # tape is an enrichment; never let it sink the run
    # phase 3: fold the readings in
    if tapes:
        with conn:
            for cid, tp in tapes.items():
                plans[cid] = _recompute(conn, cid, now, cfg, att, broad, strong_new.get(cid),
                                        fetch_tape=False, tape=tp)
    res["new_catalysts"] = len(new_ids)
    res["updated"] = len(touched - new_ids)
    # notifications use their own connection — only after our txn committed
    for cid in sorted(touched, key=lambda c: -float((plans.get(c) or {}).get("score") or 0)):
        if cid not in loud:
            continue
        why = _alert_decision(conn, cid, now, cfg, strong_new.get(cid), book=book, broad=broad)
        if why and _alert(cid, conn, now, cfg, why):
            res["alerts"].append(cid)
    if res["triaged"]:
        log.info(f"[triage] {res['triaged']} events → {res['new_catalysts']} new / "
                 f"{res['updated']} updated catalysts, {len(res['alerts'])} alerts")
    return res


def _is_quiet(raw: dict, r, now: float, cfg: dict) -> bool:
    """True for a headline that was read back after a stream disconnect and was
    already older than backfill_no_alert_minutes when it arrived."""
    try:
        bf = raw.get("backfill") if isinstance(raw, dict) else None
        if not bf:
            return False
        age = float(bf.get("age_s")) if isinstance(bf, dict) and bf.get("age_s") is not None else \
            float(now - (r["published_at"] or r["ingested_at"] or now))
        return age > float(cfg.get("backfill_no_alert_minutes", 60)) * 60
    except Exception:
        return False


def _new_story(conn, key, primary, kind, headline, r, now, window_h: float = 72, cfg: dict = None) -> dict:
    cid = uuid.uuid4().hex[:12]
    ts = r["ingested_at"]
    fam = kind_family(kind)
    extra = {"family": fam, "theme": key.split(":", 2)[-1] if not primary else None}
    # Continuation: the newest earlier story with the same key (same ticker + same kind
    # family) inside the story window — typically one the director has just consumed.
    # The new story collects the follow-up coverage for the next director package; its
    # alerts are measured against what the earlier one already sent (_alert_decision).
    try:
        if (cfg or {}).get("follow_consumed_enabled", True):
            prev = conn.execute(
                "SELECT id FROM catalysts WHERE story_key=? AND record_deleted=0 AND last_seen >= ? "
                "ORDER BY last_seen DESC LIMIT 1", (key, now - float(window_h) * 3600)).fetchone()
            if prev:
                extra["follows"] = prev[0]
    except Exception:
        pass
    conn.execute(
        "INSERT INTO catalysts (id, story_key, primary_ticker, tickers, event_kind, title, "
        "first_seen, last_seen, n_events, n_sources, velocity, market_materiality, direction, "
        "tape_json, score, status, extra_json, created_date, created_by, record_deleted) "
        "VALUES (?,?,?,?,?,?,?,?,0,0,0,0,'neutral',NULL,0,'open',?,?,?,0)",
        (cid, key, primary, json.dumps([primary] if primary else []), kind, headline[:220],
         ts, ts, json.dumps(extra), _now_iso(), _CREATED_BY))
    return {"id": cid, "story_key": key, "primary_ticker": primary, "event_kind": kind,
            "status": "open", "first_seen": ts}


def _upcoming_calendar(conn, ticker, now) -> list:
    """calendar_events for `ticker` dated within the next 7 days (≤3). [] on any
    failure (table empty / absent on an old DB)."""
    try:
        d0 = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        d1 = datetime.fromtimestamp(now + 7 * 86400, timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT event_type, event_date, title FROM calendar_events WHERE ticker=? "
            "AND record_deleted=0 AND event_date >= ? AND event_date <= ? ORDER BY event_date LIMIT 3",
            (ticker, d0, d1)).fetchall()
        return [{"type": r["event_type"], "date": r["event_date"], "title": (r["title"] or "")[:120]}
                for r in rows]
    except Exception:
        return []


def _also_sources(raw) -> list:
    """[(source, epoch|None)] of other outlets that ran the same headline (written by
    mason_news_service when it collapses a cross-outlet copy into this event)."""
    try:
        r = _parse_json(raw, {}) if not isinstance(raw, dict) else raw
        out = []
        for a in (r.get("also") or []):
            if isinstance(a, dict) and a.get("source"):
                out.append((str(a["source"]), a.get("ts")))
        return out
    except Exception:
        return []


def _recompute(conn, cid, now, cfg, att, broad, strong_new=None, fetch_tape: bool = True, tape=None) -> dict:
    """Re-aggregate one catalyst from its linked events and store it. Returns
    {"score", "tape_for"}: `tape_for` is the ticker whose price reading is wanted
    and stale when `fetch_tape` is False (the caller fetches it OUTSIDE the write
    transaction and calls again with `tape=`). Whether to alert is decided
    afterwards by _alert_decision. {} when the catalyst has no events."""
    given_tape = tape
    cat = conn.execute("SELECT * FROM catalysts WHERE id=?", (cid,)).fetchone()
    if not cat:
        return {}
    cat = dict(cat)
    evs = conn.execute(
        "SELECT id, headline, source, ingested_at, published_at, event_kind, market_materiality, "
        "resolved_tickers, direction, scored, raw FROM news_events "
        "WHERE ingested_at >= ? AND catalyst_id = ? ORDER BY ingested_at",
        ((cat["first_seen"] or 0) - 1, cid)).fetchall()
    if not evs:
        return {}
    best = max(evs, key=lambda e: (e["market_materiality"] or 0, -(e["ingested_at"] or 0)))
    kinds, tks, dirs = {}, {}, {}
    for e in evs:
        kinds[e["event_kind"]] = kinds.get(e["event_kind"], 0) + 1
        for t in _parse_json(e["resolved_tickers"], []):
            tks[t] = tks.get(t, 0) + 1
        d = _direction(e["headline"], e["direction"] if e["scored"] else None)
        dirs[d] = dirs.get(d, 0) + 1
    srcs = {e["source"] for e in evs}
    for e in evs:                                  # cross-outlet copies count as sources, not events
        srcs.update(src for src, _ in _also_sources(e["raw"]))
    _an = collections.Counter(_direction(e["headline"]) for e in evs
                              if e["event_kind"] == "analyst_action"
                              and (e["ingested_at"] or 0) > now - 48 * 3600)
    analyst_48h = max((v for k, v in _an.items() if k != "neutral"), default=0)
    primary = cat["primary_ticker"]
    if primary:
        vel, rw = att.velocity(att.by_tk.get(primary, []))
    else:
        vel, rw = att.velocity(att.by_cat.get(cid, []))
    # story kind = kind of the most material linked event (not 'other' if avoidable)
    kind = best["event_kind"] if best["event_kind"] != "other" else \
        max((k for k in kinds if k != "other"), key=lambda k: kinds[k], default=cat["event_kind"])
    base = float(best["market_materiality"] or 0)
    if kind == "other" and primary and vel >= 4 and rw >= 6:
        base = max(base, 0.5 if vel < 8 else 0.58)   # attention-only story floor
    extra = _parse_json(cat["extra_json"], {})
    tape = _parse_json(cat["tape_json"], {}) or {}
    fam = kind_family(kind)
    pre, _ = story_score(base=base, n_sources=len(srcs), velocity=vel, recent_w=rw,
                         analyst_48h=analyst_48h, in_broad=(primary in broad) if primary else True,
                         is_ticker=bool(primary), family=fam)
    want_tape = None
    if given_tape is not None:
        if given_tape:
            tape = dict(given_tape)
        extra["_tape_fetch_ts"] = now
    elif primary and pre >= float(cfg.get("tape_min_score", 0.45)):
        fetched = float(extra.get("_tape_fetch_ts", 0) or 0)
        stale = (not tape) or (now - fetched > float(cfg.get("tape_ttl_seconds", 300)))
        if stale and not fetch_tape:
            want_tape = primary
        elif stale and _TAPE_BUDGET["left"] > 0:
            _TAPE_BUDGET["left"] -= 1          # bounded uncached lookups per run
            try:
                t2 = tape_fn(primary, now_epoch=now)
            except Exception:
                t2 = {}            # tape is an enrichment; never let it sink the run
            if t2:
                tape = t2
            extra["_tape_fetch_ts"] = now
    cal = _upcoming_calendar(conn, primary, now) if primary else []
    if cal:
        extra["calendar"] = cal
    score, parts = story_score(base=base, n_sources=len(srcs), velocity=vel, recent_w=rw,
                               tape=tape, analyst_48h=analyst_48h,
                               in_broad=(primary in broad) if primary else True,
                               is_ticker=bool(primary), family=fam, calendar=bool(cal))
    ranked = sorted(dirs.items(), key=lambda x: -x[1])
    nz = {k: v for k, v in dirs.items() if k != "neutral"}
    if len(nz) == 2 and min(nz.values()) >= max(nz.values()) * 0.5:
        direction = "mixed"
    elif nz:
        direction = max(nz, key=nz.get)
    else:
        direction = ranked[0][0] if ranked else "neutral"
    tickers = [primary] if primary else []
    tickers += [t for t, _ in sorted(tks.items(), key=lambda x: -x[1]) if t != primary][:4]
    if strong_new:
        kinds_now = {k for k in strong_new if not str(k).startswith("_")}
        if kinds_now:
            extra["strong_kinds"] = sorted(set(extra.get("strong_kinds") or []) | kinds_now)
        try:
            extra["strong_floor"] = max([float(extra.get("strong_floor") or 0), float(strong_new.get("_solid") or 0)] + [
                float(v.get("floor") or 0) for v in strong_new.values() if isinstance(v, dict)])
            fl = dict(extra.get("strong_floors") or {})
            for k, v in strong_new.items():
                if isinstance(v, dict) and not str(k).startswith("_"):
                    fl[k] = max(float(fl.get(k) or 0), float(v.get("floor") or 0))
            extra["strong_floors"] = fl
        except Exception:
            pass
    extra.update({"kinds": kinds, "sources": sorted(srcs)[:20], "analyst_actions_48h": analyst_48h,
                  "velocity_recent_w": rw, "score_parts": parts,
                  "top_event_id": best["id"], "max_score": max(score, extra.get("max_score", 0))})
    conn.execute(
        "UPDATE catalysts SET tickers=?, event_kind=?, title=?, first_seen=?, last_seen=?, "
        "n_events=?, n_sources=?, velocity=?, market_materiality=?, direction=?, tape_json=?, "
        "score=?, extra_json=? WHERE id=?",
        (json.dumps(tickers if primary else []), kind, (best["headline"] or "")[:220],
         evs[0]["ingested_at"], evs[-1]["ingested_at"], len(evs), len(srcs), vel,
         round(float(best["market_materiality"] or 0), 3), direction,
         json.dumps(tape) if tape else None, score, json.dumps(extra), cid))
    return {"score": score, "tape_for": want_tape}


def _hl_tokens(h: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]{3,}", _clean(h or "").lower())
            if t not in ("the", "and", "for", "with", "from", "that", "this", "its", "says", "said",
                         "after", "amid", "over", "into", "update", "breaking", "just", "report")}


def _same_headline(a: str, b: str, thr: float = 0.6) -> bool:
    """Is headline `a` the same as, or a near-duplicate of, `b`?"""
    try:
        na, nb = " ".join(_clean(a or "").lower().split()), " ".join(_clean(b or "").lower().split())
        if not na or not nb:
            return False
        if na == nb or (len(na) > 25 and na in nb) or (len(nb) > 25 and nb in na):
            return True
        ta, tb = _hl_tokens(a), _hl_tokens(b)
        if len(ta) < 3 or len(tb) < 3:
            return False
        # (review round 2) two templated lines that carry different figures are different
        # items: "US CPI MOM ACTUAL 0.4% (FORECAST 0.3%)" is not last month's "… 0.6% (… 0.4%)"
        fa = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)*", na)); fb = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)*", nb))
        if fa and fb and 2 * len(fa & fb) < len(fa | fb):
            return False
        return len(ta & tb) / len(ta | tb) >= thr
    except Exception:
        return False


def _story_alerts(extra: dict, row: dict) -> list:
    """The alerts one catalyst has sent: [{at, score, h, kinds}] (newest last)."""
    al = [a for a in (extra.get("alerts") or []) if isinstance(a, dict) and a.get("at")]
    if not al and row.get("alerted_at"):
        one = extra.get("alert") if isinstance(extra.get("alert"), dict) else {}
        # alerted before the ledger existed: score / headline as they stand now
        al = [{"at": float(one.get("at") or row["alerted_at"]),
               "score": float(one.get("score") if one.get("score") is not None else (row.get("score") or 0)),
               "h": one.get("h") or row.get("title") or "", "kinds": list(one.get("kinds") or [])}]
    return al


def _alert_ledger(conn, cat: dict, extra: dict, now: float, cfg: dict) -> list:
    """Everything already alerted that a new alert for this story must be measured
    against: (1) every alert for the same TICKER — any story — in the last
    alert_budget_hours (for a ticker-less story: the same macro family), and (2) the
    alerts of the story this one continues (extra.follows, a few links back),
    whatever their age. Each entry: {cid, at, score, h, kinds, recent}."""
    cut = now - float(cfg.get("alert_budget_hours", 24)) * 3600
    rows = []
    try:
        if cat.get("primary_ticker"):
            rows = conn.execute("SELECT id, alerted_at, score, title, extra_json, story_key FROM catalysts WHERE primary_ticker=? "
                                "AND record_deleted=0 AND alerted_at >= ?", (cat["primary_ticker"], cut)).fetchall()
        else:
            fam = ":".join(str(cat.get("story_key") or "").split(":")[:2])
            rows = conn.execute("SELECT id, alerted_at, score, title, extra_json, story_key FROM catalysts WHERE primary_ticker IS NULL "
                                "AND story_key LIKE ? AND record_deleted=0 AND alerted_at >= ?",
                                (fam + ":%", cut)).fetchall()
    except Exception:
        rows = []
    rows = [dict(r) for r in rows]
    seen = {r["id"] for r in rows}
    # the story LINE: this story, the stories it continues, and any story with the same key
    line_ids = {cat["id"]} | {r["id"] for r in rows if r.get("story_key") and r.get("story_key") == cat.get("story_key")}
    if cat["id"] not in seen:                 # the story's own alerts always count, whatever their age
        seen.add(cat["id"])
        rows.append({"id": cat["id"], "alerted_at": cat.get("alerted_at"), "score": cat.get("score"),
                     "title": cat.get("title"), "extra_json": None})
    if cfg.get("follow_consumed_enabled", True):
        nxt, hops = extra.get("follows"), 0
        while nxt and hops < 4:
            hops += 1
            try:
                r = conn.execute("SELECT id, alerted_at, score, title, extra_json FROM catalysts WHERE id=?",
                                 (nxt,)).fetchone()
            except Exception:
                r = None
            if not r:
                break
            r = dict(r)
            line_ids.add(r["id"])
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
            nxt = (_parse_json(r.get("extra_json"), {}) or {}).get("follows")
    out = []
    for r in rows:
        ex = extra if r["id"] == cat["id"] else (_parse_json(r.get("extra_json"), {}) or {})
        for a in _story_alerts(ex, r):
            out.append({"cid": r["id"], "at": float(a["at"]), "score": float(a.get("score") or 0),
                        "h": a.get("h") or "", "kinds": list(a.get("kinds") or []),
                        "floors": dict(a.get("floors") or {}), "recent": float(a["at"]) >= cut,
                        "line": r["id"] in line_ids, "kind": a.get("kind")})
    return out


def _nq_routine(h: str, tk, score) -> bool:
    """2026-11-15 (news.quality.routine_filter): a routine headline — rating / price-target
    note, conference presentation, look-back, preview, commentary — never carries an
    alert, except a rating CHANGE or a presentation on a HELD name with a clearly
    material score."""
    try:
        import mason_news_quality as NQ
        return bool(NQ.routine_blocks(h, tk, score, company=bool(tk)))
    except Exception:
        return False


def _nq_non_event(h: str, tk, company: bool):
    """non_event_reason with the inbox's held-name rule (mason_news_quality.non_event_blocks,
    news.quality.shared_form_rule): a HELD name's event headline that ends "Here's what to
    know" or "- follow live" may carry an alert. The plain rule if that module fails."""
    try:
        import mason_news_quality as NQ
        return NQ.non_event_blocks(h, tk, company=company)
    except Exception:
        return non_event_reason(h, company=company)


def _kind_alert_count(entries: list, kind: str) -> int:
    """How many of these ledger alerts went out for `kind` (older entries, written before
    alerts recorded their kind, count when the kind was confirmed at the time)."""
    n = 0
    for e in entries:
        k = e.get("kind")
        if k == kind or (k is None and kind in (e.get("kinds") or [])):
            n += 1
    return n


def _stems(h: str) -> set:
    stop = {"the", "and", "for", "with", "from", "that", "this", "its", "says", "said", "will", "after", "amid",
            "over", "into", "has", "have", "are", "was", "were", "been", "his", "her", "their", "new", "just"}
    return {t[:5] for t in re.findall(r"[a-z0-9]{3,}", _clean(h or "").lower()) if t not in stop}


def _distinct_event(h: str, told: list, thr: float = 0.25) -> bool:
    """True when headline `h` shares less than `thr` of its word stems with every
    headline already alerted: a different event, not another outlet's account of the
    same one. (0.25, not the news dedup's 0.34: two accounts of one resignation —
    "CEO Huang resigns, effective immediately; CFO Kress takes over" / "CEO Jensen
    Huang Resigns; Stock Down 10%" — share 0.33; the board's probe and the FBI's share 0.20.)"""
    a = _stems(h)
    if len(a) < 3:
        return False
    for x in told:
        b = _stems(x)
        if b and len(a & b) / len(a | b) >= thr:
            return False
    return True


def _alert_decision(conn, cid, now, cfg, strong_new=None, book=None, broad=None):
    """Should this catalyst alert NOW, and why? None, or {"why", "h", "kind", "was"}.

    A company that is not on the book (`book`: holdings, watchlist, Mason's open
    trades; None = no such test) alerts only when the story holds a solid event — a
    confirmed strong event, or a primary-source item — whose own floor reaches
    alert_offbook_min_floor, or when its score reaches alert_offbook_score.
    alert_offbook_mode="all" switches this off. (`broad` is accepted for callers
    that have it; the rule does not depend on index membership.)

    The owner's inbox is budgeted per TICKER, not per story:
      first      nothing has been alerted for the ticker in the last 24 h and the
                 story it continues (if any) never alerted;
      new_kind   a strong event of a kind not yet alerted for the ticker — the one
                 case that may exceed the budget (by alert_budget_strong_overflow);
      new_event  the story continues one that alerted more than 24 h ago and now
                 holds a strong event with a materially different headline;
      score_step the score beats the best score already alerted by realert_score_step.
    Never: when the headline it would carry is the same as (or a near-duplicate of)
    one already alerted; when the headline is commentary or a market wrap; past the
    budget; the same story twice inside realert_min_minutes (a new strong kind is
    exempt); once the director has consumed the story or it has expired.
    `strong_new`: {kind: headline} of the strong events that arrived in this run."""
    try:
        row = conn.execute("SELECT * FROM catalysts WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        cat = dict(row)
        score = float(cat.get("score") or 0)
        if score < float(cfg.get("alert_threshold", 0.7)):
            return None
        if cat.get("status") not in ("open", "alerted", "dossier_ready"):
            return None
        extra = _parse_json(cat.get("extra_json"), {}) or {}
        sfloor = {k: float(v.get("floor") or 0) for k, v in (strong_new or {}).items()
                  if isinstance(v, dict) and not str(k).startswith("_")}
        strong_new = {k: (v.get("h") if isinstance(v, dict) else v) for k, v in (strong_new or {}).items()
                      if not str(k).startswith("_")}
        tk = cat.get("primary_ticker")
        if tk and book is not None and tk not in book and str(cfg.get("alert_offbook_mode", "strong")) != "all":
            if float(extra.get("strong_floor") or 0) < float(cfg.get("alert_offbook_min_floor", 0.7)) and \
                    score < float(cfg.get("alert_offbook_score", 0.85)):
                return None
        mine = _story_alerts(extra, cat)
        if mine and not cfg.get("realert_enabled", True):
            return None
        sim = float(cfg.get("alert_headline_similarity", 0.6))
        company = bool(cat.get("primary_ticker"))
        title = cat.get("title") or ""
        budget_on = bool(cfg.get("alert_budget_enabled", True))
        ledger = _alert_ledger(conn, cat, extra, now, cfg) if budget_on else \
            [dict(a, cid=cid, recent=True, kinds=list(a.get("kinds") or [])) for a in mine]
        told = [e["h"] for e in ledger if e.get("h")]

        def fresh(h):
            return bool(h) and not _nq_non_event(h, tk, company) and \
                not any(_same_headline(h, x, sim) for x in told) and not _nq_routine(h, tk, score)

        cand = [(k, h) for k, h in (strong_new or {}).items() if fresh(h)]
        title_ok = fresh(title)
        if not ledger:
            if cand:
                return {"why": "first", "h": cand[0][1], "kind": cand[0][0], "was": None}
            return {"why": "first", "h": title, "kind": None, "was": None} if title_ok else None
        recent = [e for e in ledger if e["recent"]]
        kinds_told = {k for e in ledger for k in e["kinds"]}
        # how strong a form of each kind has been alerted ("considers an interim chief"
        # is 0.65; "CEO resigns, effective immediately" is 0.85 — that IS news again)
        told_floor = {}
        for e in ledger:
            for k in e["kinds"]:
                told_floor[k] = max(told_floor.get(k, 0.0), float((e.get("floors") or {}).get(k, 1.0)))
        esc = float(cfg.get("alert_escalation_floor_step", 0.15))
        best = max(e["score"] for e in ledger)
        budget = int(cfg.get("alert_budget_per_ticker", 3))
        new_kind = [(k, h) for k, h in cand
                    if k not in kinds_told or (k in sfloor and sfloor[k] >= told_floor.get(k, 1.0) + esc)]
        if not new_kind and cand and _nq_on("second_strong_same_kind"):
            # 2026-11-15: a SECOND strong event of a kind already alerted for this ticker today
            # (the board's probe, then the FBI's) may alert once — when its headline is a
            # different event, not another outlet's version of the first
            recent_kinds = {kk for e in recent for kk in e["kinds"]} | {e.get("kind") for e in recent}
            for k, h in cand:
                # only inside the day (an older alert is handled by the 'new_event' rule below)
                if k in recent_kinds and k in sfloor and _kind_alert_count(recent, k) < 2 and \
                        _distinct_event(h, told):
                    new_kind = [(k, h)]
                    break
        if new_kind:
            if budget_on and len(recent) >= budget + int(cfg.get("alert_budget_strong_overflow", 2)):
                return None
            return {"why": "new_kind", "h": new_kind[0][1], "kind": new_kind[0][0], "was": best}
        if budget_on and len(recent) >= budget:
            return None
        if mine and (now - max(a["at"] for a in mine)) < float(cfg.get("realert_min_minutes", 120)) * 60:
            return None
        step = float(cfg.get("realert_score_step", 0.2))
        # (review round 2) The budget above is per TICKER; the score bar is per STORY LINE
        # (this story, the ones it continues, and any story with the same key). What OTHER
        # stories on the ticker scored is not a bar a different subject has to clear — the
        # budget, the headline test and the off-book rule still apply to it.
        line = [e for e in ledger if e.get("line", True)]
        line_recent = [e for e in line if e["recent"]]
        if not mine and not line_recent:
            if not line and (cand or title_ok):
                # another subject on the ticker (results the day a deal rumour alerted)
                return {"why": "first", "h": cand[0][1] if cand else title,
                        "kind": cand[0][0] if cand else None, "was": None}
            if line and cand:
                # continues a line that alerted more than a day ago; a strong event with a
                # materially different headline is a new development of it
                return {"why": "new_event", "h": cand[0][1], "kind": cand[0][0],
                        "was": max(e["score"] for e in line)}
            if line and title_ok and (now - max(e["at"] for e in line)) > float(cfg.get("story_window_hours", 72)) * 3600:
                # the line last alerted longer ago than a story lives (this month's CPI print
                # against last month's): not follow-up coverage any more
                return {"why": "new_event", "h": title, "kind": None, "was": max(e["score"] for e in line)}
        ref = max(e["score"] for e in (line_recent or line)) if line else best
        if score >= ref + step and (cand or title_ok):
            return {"why": "score_step", "h": cand[0][1] if cand else title,
                    "kind": cand[0][0] if cand else None, "was": ref}
        return None
    except Exception:
        return None


_TAPE_BUDGET = {"left": 6}
tape_fn = tape_for          # module seam: tests / replays swap this


def _expire(conn, now, window_h, cfg: dict = None):
    """Close stories nobody consumed: idle for story_window_hours, OR older than
    story_max_age_days counted from their FIRST headline (so a busy ticker's story
    cannot live for weeks by being topped up). dossier_ready / consumed rows keep
    their status (the director's bookkeeping); they simply stop accepting events."""
    try:
        conn.execute("UPDATE catalysts SET status='expired' WHERE status IN ('open','alerted') "
                     "AND record_deleted=0 AND last_seen < ?", (now - window_h * 3600,))
        cfg = cfg if cfg is not None else _cfg()
        _closed, oldest = _story_limits(cfg, now)
        if oldest > 0:
            conn.execute("UPDATE catalysts SET status='expired' WHERE status IN ('open','alerted') "
                         "AND record_deleted=0 AND first_seen IS NOT NULL AND first_seen < ?", (oldest,))
    except Exception:
        pass


def _alert_sources_block(conn, cid, extra=None, limit: int = 6) -> str:
    """Clickable 'Sources:' block for a catalyst's inbox alert — the same markdown
    shape the desk digests use ("- [headline](url)  — source"); the notifications
    UI linkifies it (linkifyBody in mason_v2.html). Pulled from the news_events
    clustered into this catalyst: top event first, then best tier / newest, one
    line per distinct URL. Returns "" when no clustered headline carries a URL."""
    try:
        rows = conn.execute(
            "SELECT id, headline, url, source FROM news_events WHERE catalyst_id=? "
            "AND COALESCE(is_deleted,0)=0 AND url IS NOT NULL AND url != '' "
            "ORDER BY COALESCE(source_tier,2), ingested_at DESC", (cid,)).fetchall()
    except Exception:
        return ""
    top = (extra or {}).get("top_event_id")
    rows = sorted(rows, key=lambda r: r["id"] != top)      # stable: top event first
    lines, seen = [], set()
    for r in rows:
        url = (r["url"] or "").strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        head = _html.unescape(r["headline"] or "").replace("[", "(").replace("]", ")")
        head = " ".join(head.split())[:150] or "article"
        lines.append(f"- [{head}]({url})  — {r['source'] or 'source'}")
        if len(lines) >= limit:
            break
    return ("\nSources:\n" + "\n".join(lines)) if lines else ""


def _alert(cid, conn, now, cfg, why: dict = None) -> bool:
    """Send the inbox notification _alert_decision allowed and write it to the
    story's ledger (extra.alerts). Called directly (older callers / tests) it asks
    _alert_decision itself."""
    try:
        if why is None:
            why = _alert_decision(conn, cid, now, cfg, None)
            if not why:
                return False
        cat = conn.execute("SELECT * FROM catalysts WHERE id=?", (cid,)).fetchone()
        if not cat:
            return False
        cat = dict(cat)
        if cat["status"] not in ("open", "alerted", "dossier_ready"):
            return False
        extra = _parse_json(cat.get("extra_json"), {}) or {}
        prev = extra.get("alert") or {}
        again = bool(cat["alerted_at"])                 # this very story has alerted before
        tape = _parse_json(cat["tape_json"], {}) or {}
        subj = cat["primary_ticker"] or cat["story_key"].split(":", 2)[-1]
        head = why.get("h") or cat["title"] or ""
        if cat["primary_ticker"] and _nq_on("symbol_gate"):
            import mason_news_quality as NQ
            if not NQ.tradable(cat["primary_ticker"]):
                log.info(f"[triage] alert for {cid} skipped: {cat['primary_ticker']} is not a US listing")
                return False
        # 2026-11-15 (news.quality.label_fixes): the label describes the headline the alert
        # carries — a headline that joined a story keeps its OWN event kind
        label_kind, lead_id = cat["event_kind"], None
        try:
            lrow = conn.execute("SELECT id, raw FROM news_events WHERE catalyst_id=? AND headline=? "
                                "ORDER BY ingested_at DESC LIMIT 1", (cid, head)).fetchone()
            lead_id = lrow["id"] if lrow else None
            if _nq_on("label_fixes"):
                own = why.get("kind")
                if not own:
                    hint = (_parse_json(lrow["raw"], {}) or {}).get("event_kind_hint") if lrow else None
                    own = classify_kind(head, hint, has_ticker=bool(cat["primary_ticker"]))
                if own and own != "other":
                    label_kind = own
        except Exception:
            pass
        label = KIND_LABEL.get(label_kind, label_kind)
        reason = why.get("why")
        title = f"{'Catalyst update' if again else 'Catalyst'}: {subj} — {label} ({cat['score']:.2f})"
        if reason == "new_kind" and (again or why.get("was") is not None):
            lead = f"Now confirmed: {KIND_LABEL.get(why.get('kind'), why.get('kind'))}. {head}"
        elif reason == "new_event":
            lead = f"New development: {head}"
        elif reason == "score_step":
            was = why.get("was")
            lead = ("This story has grown since the last alert"
                    + (f" (score was {float(was):.2f}, now {cat['score']:.2f})" if was is not None else "")
                    + f". Latest: {head}")
        else:
            lead = head
        bits = [lead, f"{cat['n_events']} headlines / {cat['n_sources']} sources, "
                f"velocity {cat['velocity']}x, direction {cat['direction']}"]
        if tape:
            bits.append(f"tape {tape.get('move_pct')}% ({tape.get('vs_sector_pct')}% vs "
                        f"{tape.get('sector_etf')}), rel vol {tape.get('rel_volume')}")
        src_block = _alert_sources_block(conn, cid, extra)
        if src_block:
            bits.append(src_block)
        sev ="critical" if cat["score"] >= float(cfg.get("event_call_threshold", 0.85)) else "warn"
        n_alerts = int(prev.get("n") or (1 if again else 0)) + 1
        try:
            import mason_notifications as MN
            payload = dict(type="huge_event", title=title, body="\n".join(bits),
                           severity=sev, ticker=cat["primary_ticker"],
                           ref_table="catalysts", ref_id=cid, created_by=_CREATED_BY,
                           dedup_key=(f"catalyst:{cid}" if not again else f"catalyst:{cid}:{n_alerts}"))
            if cat["primary_ticker"] and _nq_on("triage_merge"):
                # one inbox item per story: an unread news / Breaking item for this story is
                # upgraded to this alert; an article Nico already read is not posted again
                import mason_news_quality as NQ
                new = NQ.new_item(stream="triage", ticker=cat["primary_ticker"], headline=head,
                                  news_ids=[lead_id] if lead_id else [], catalyst_ids=[cid], kind=label_kind,
                                  strong=bool(why.get("kind")), ts=now)
                placed = NQ.place(new=new, payload=payload, merge_key="triage_merge")
                if placed.get("action") != "new":
                    log.info(f"[triage] alert for {cid} {placed.get('action')} into inbox item "
                             f"{placed.get('id')} ({placed.get('how')})")
            else:
                MN.add_notification(**payload)
        except Exception as e:
            log.warning(f"[triage] alert notification failed for {cid}: {e}")
        # "kinds": the strong kinds the story had CONFIRMED when this alert went out;
        # "h": the headline the alert carried (so it is never alerted again)
        entry = {"score": cat["score"], "at": now, "n": n_alerts, "h": head[:220], "why": reason,
                 "kind": why.get("kind"),
                 "kinds": sorted(extra.get("strong_kinds") or []),
                 "floors": dict(extra.get("strong_floors") or {})}
        extra["alert"] = entry
        extra["alerts"] = (_story_alerts(dict(extra, alert=prev), cat) if again else [])[-7:] + [entry]
        with conn:
            conn.execute("UPDATE catalysts SET status=CASE WHEN status='open' THEN 'alerted' "
                         "ELSE status END, alerted_at=?, extra_json=? WHERE id=? "
                         "AND status IN ('open','alerted','dossier_ready')", (now, json.dumps(extra), cid))
        return True
    except Exception:
        return False


# ── read / write API for the desk + director ─────────────────────────────────────

def _shape(row) -> dict:
    d = dict(row)
    d["tickers"] = _parse_json(d.get("tickers"), [])
    d["tape_json"] = _parse_json(d.get("tape_json"), {}) or {}
    d["extra_json"] = _parse_json(d.get("extra_json"), {}) or {}
    d["dossier_ids"] = _parse_json(d.get("dossier_ids"), []) or []
    return d


def top_catalysts(*, since_epoch: float = None, min_score: float = 0.0, limit: int = 10,
                  statuses=("open", "alerted", "dossier_ready")) -> list:
    """Highest-scoring live catalysts as dicts (tickers / tape_json / extra_json
    parsed). Never raises; [] on failure."""
    conn = None
    try:
        conn = mason_db.get_connection()
        sts = list(statuses or ())
        q = ("SELECT * FROM catalysts WHERE record_deleted=0 AND score >= ? "
             + (f"AND status IN ({','.join('?' * len(sts))}) " if sts else "")
             + ("AND last_seen >= ? " if since_epoch else "")
             + "ORDER BY score DESC, last_seen DESC LIMIT ?")
        args = [float(min_score)] + sts + ([float(since_epoch)] if since_epoch else []) + [int(limit)]
        return [_shape(r) for r in conn.execute(q, args).fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def mark_catalyst(catalyst_id: str, *, status: str = None, dossier_ids=None,
                  consumed_by: str = None) -> bool:
    """Update a catalyst's status / dossier links / consumer. dossier_ids are
    merged (deduped) into the existing list. Returns True if a row changed."""
    conn = None
    try:
        conn = mason_db.get_connection()
        row = conn.execute("SELECT dossier_ids FROM catalysts WHERE id=?", (catalyst_id,)).fetchone()
        if not row:
            return False
        sets, args = [], []
        if status:
            if status not in ("open", "alerted", "dossier_ready", "consumed", "expired"):
                return False
            sets.append("status=?")
            args.append(status)
        if dossier_ids:
            cur = _parse_json(row["dossier_ids"], []) or []
            merged = list(dict.fromkeys(list(cur) + [str(x) for x in dossier_ids]))
            sets.append("dossier_ids=?")
            args.append(json.dumps(merged))
        if consumed_by:
            sets.append("consumed_by=?")
            args.append(str(consumed_by))
        if not sets:
            return False
        with conn:
            cur = conn.execute(f"UPDATE catalysts SET {', '.join(sets)} WHERE id=?",
                               args + [catalyst_id])
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":        # manual: refresh the universe / try a headline
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--universe":
        u = broad_universe(force_refresh=True)
        print(f"{len(u)} tickers; e.g. META={u.get('META')}")
    else:
        for h in sys.argv[1:] or ["Meta's personal AI agent Muse hits No. 1 on Apple's App Store"]:
            k = classify_kind(h)
            print(h, "→", resolve_tickers(h), k, market_materiality(k, h))
