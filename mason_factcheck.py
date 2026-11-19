"""
mason_factcheck.py — the Analyst Desk fact-checking toolkit (Phase 4).

Phases 1–3 gave the desk an inbox, a per-ticker qwen analyst, a synthesized Desk
Report and a recommendations pipeline. Phase 4 does NOT add analyst capability —
it makes the existing output *trustworthy*. qwen (a single 4B model playing every
role) invents tickers, numbers and cross-ticker linkages that reach Nico. This
module is the shared, deterministic, offline-testable toolkit the three call sites
use to stop that:

  * Layer 0 (ingestion): `filter_relevant_tickers` drops Benzinga mistags before a
    news_events row is written (kills "Crude Oil Down 3%" auto-tagged [BAK,...]).
  * Layer 1 (analyst): `check_claim` + `qwen_diff` validate each per-ticker analyst
    output before it is stored (kills the invented "PRFX aligns with IONQ" linkage).
  * Layer 2 (report): the same checks run on the assembled Desk Report prose (kills
    "PRFX"→"PRX" and "led cyclical moves"→"leads volatility" at synthesis time).

Design rules baked in (see ANALYST_DESK_PHASE4_PROMPT.md §3/§5):
  * The checker can only SUBTRACT. A deterministic drop is final; the qwen diff may
    only DEMOTE a claim the deterministic pass let through, never PROMOTE a dropped
    one. A 4B checker must not be able to rescue a hallucination.
  * Ground truth is the INGESTED SOURCE TEXT only — no web, no qwen world knowledge.
  * The CITATION RULE (a ticker in a news claim must be in that news_event's tag
    list) alone kills "PRX": no news_event ever contained it.
  * System-sourced lines (regime / portfolio / scanner / recs) are ground truth by
    construction and are exempt from the news-citation rule (they still pass the
    ticker gate).
  * Everything is injectable (llm_fn, price_fn, ticker master, clock) and never
    raises out — every entry point degrades to a safe, conservative outcome.

Nothing here runs a thread or touches the network except through the injected
price function (which defaults to the already-cached spot helper). All config is
read from the `analyst_desk.factcheck` block; every flag defaults OFF so an
unflagged import is inert.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("mason.factcheck")

_BASE = Path(__file__).parent
_EDGAR_CACHE = _BASE / ".edgar_tickers_cache.json"
# Supplement to the EDGAR universe: Alpaca tradable US equities (OTC/ADR/fresh IPOs
# EDGAR's company_tickers.json omits) ∪ auto-learned symbols (news tags). Written by
# refresh_ticker_universe() on a background cadence; read cheaply here on its mtime.
_SUPPLEMENT_CACHE = _BASE / ".ticker_supplement_cache.json"


# ── config ──────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        import mason_app
        return mason_app._load_cfg()
    except Exception:
        try:
            return json.loads((_BASE / "mason.config.json").read_text(encoding="utf-8"))
        except Exception:
            return {}


def _fc_cfg(cfg: dict | None = None) -> dict:
    """The analyst_desk.factcheck block (or {})."""
    cfg = cfg if cfg is not None else _load_config()
    try:
        d = cfg["analyst_desk"]["factcheck"]
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fc_get(cfg, path, default):
    """Nested get inside the factcheck block, e.g. _fc_get(cfg, ['tag_filter','macro_min'], 3)."""
    cur = _fc_cfg(cfg)
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def enabled(cfg=None) -> bool:
    """Master kill-switch. factcheck.enabled=false short-circuits every layer."""
    return bool(_fc_get(cfg, ["enabled"], False))


def layer_enabled(layer: str, cfg=None) -> bool:
    """True only if the master switch AND the specific sub-flag are on.
    layer ∈ {'tag_filter','layer1','layer2','qwen_diff'}."""
    if not enabled(cfg):
        return False
    return bool(_fc_get(cfg, [layer, "enabled"], False)) if layer in (
        "tag_filter", "layer1", "layer2") else bool(_fc_get(cfg, [layer, "enabled"], False))


def chat_enabled(cfg=None) -> bool:
    """Live-chat fact-check (Path B advisory badge). Requires the master switch AND
    analyst_desk.factcheck.chat.enabled. Default OFF ⇒ byte-for-byte current chat."""
    if not enabled(cfg):
        return False
    return bool(_fc_get(cfg, ["chat", "enabled"], False))


def chat_run_diff(cfg=None) -> bool:
    """Whether the live-chat audit runs the extra demote-only qwen source-diff.
    Default OFF — the chat path stays deterministic + fast unless explicitly on."""
    return bool(_fc_get(cfg, ["chat", "run_diff"], False))


def universe_refresh_enabled(cfg=None) -> bool:
    """Whether the background ticker-universe refresh runs (EDGAR ∪ Alpaca ∪ learned →
    .ticker_supplement_cache.json). Default OFF: writing the supplement widens the
    shared ticker master, which also makes the already-live analyst-desk fact-check
    recognize more real tickers — a real behavior change, so it is opt-in."""
    if not enabled(cfg):
        return False
    return bool(_fc_get(cfg, ["universe_refresh", "enabled"], False))


# ── ticker master (citation-first, EDGAR/book/allowlist backstop) ───────────────

# cache: (mtime, master_set, titles_map)
_master_cache: dict = {"mtime": None, "master": None, "titles": None, "extra_key": None}

# words that are ALL-CAPS but are not tickers — so we don't mistake them for
# invented/uncited symbols inside a claim.
_TICKER_STOPWORDS = {
    "AI", "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETFS", "USA", "GDP", "CPI",
    "PPI", "FED", "SEC", "FDA", "FTC", "DOJ", "EPS", "USD", "EUR", "GBP", "JPY",
    "THE", "AND", "FOR", "ALL", "NEW", "NOW", "OUT", "OFF", "ONE", "TWO", "TOP",
    "BUY", "SELL", "HOLD", "YOY", "QOQ", "MOM", "MA", "EV", "AI/ML", "ML", "OK",
    "US", "UK", "EU", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "ATH", "ATL",
    "IMO", "FYI", "NYSE", "AH", "PM", "AM", "ET", "PT", "UTC", "API", "APIS",
    "GAAP", "SAAS", "AWS", "GPU", "CPU", "II", "III", "IV", "P&L", "R&D",
    # common business / prose acronyms that are ALSO real tickers or ticker-shaped
    # (they appear as words far more often than as ticker references)
    "IT", "PR", "HR", "OS", "IND", "NIH", "SARS", "FBI", "CIA", "IRS", "WHO",
    "USDA", "NATO", "OPEC", "ESG", "NAV", "TAM", "ARR", "YTD", "QTD", "MTD",
    "CAGR", "IPOS", "ETFS", "SPAC", "SPACS", "GDPR", "CHIPS", "COVID", "AGI",
}

# macro / market-wide cues: a story carrying one of these is treated as macro, so a
# single primary tag on it is not auto-kept.
_MACRO_KEYWORDS = (
    "crude oil", "oil prices", "brent", "wti", "the dow", "dow jones",
    "s&p 500", "s&p500", "s&p", "nasdaq", "russell", "treasury yield",
    "treasury yields", "10-year", "bond yields", "crypto stocks", "bitcoin",
    "jobs report", "nonfarm", "payrolls", "cpi", "inflation", "rate cut",
    "rate hike", "the fed", "fed rate", "fomc", "gdp", "unemployment",
    "consumer sentiment", "retail sales", "pmi", "yields", "sector rotation",
    "market-wide", "broad market", "stocks rally", "stocks fall", "futures",
)


def _parse_edgar_cache(raw: dict) -> dict:
    """{"0":{cik_str,ticker,title}} → {TICKER: title}. Tolerant of bad rows."""
    out = {}
    for _, row in (raw or {}).items():
        try:
            tk = str(row["ticker"]).upper().strip()
            if tk:
                out[tk] = str(row.get("title", "")).strip()
        except Exception:
            continue
    return out


def _book_sets():
    """(holdings_set, watchlist_set) — empty on any failure."""
    holds, watch = set(), set()
    try:
        import mason_news
        holds = {str(t).upper() for t in mason_news.holdings_set()}
        watch = {str(t).upper() for t in mason_news.watchlist_set()}
    except Exception:
        pass
    return holds, watch


def _personal_held(cfg=None) -> set:
    """Tickers Nico holds in his own real accounts (mason_news.personal_held_set: open personal_positions rows, never
    an excluded account, never CASH). Real listings even when the SEC list lacks them (ETFs such as VOO, VBR, SPMO,
    QQQM). Review fix 2026-11-19 (old-snapshot sweep): those three ETFs were "known" only through the retired positions.csv
    snapshot; without this a headline naming only one of them is dropped as "not a US listing". Switch
    analyst_desk.factcheck.master_personal_holdings (default true; false = the master without them). Empty on any
    failure."""
    if _fc_get(cfg, ["master_personal_holdings"], True) is False:
        return set()
    try:
        import mason_news
        return {str(t).upper() for t in mason_news.personal_held_set() if t}
    except Exception:
        return set()


def load_ticker_titles(cfg=None) -> dict:
    """{TICKER: company title} from the EDGAR cache (for name-matching in Layer 0).
    Cached on the cache file's mtime. {} if the cache is unreadable."""
    load_ticker_master(cfg)          # populates the shared cache
    return _master_cache.get("titles") or {}


def _load_supplement() -> tuple:
    """(symbol_set, mtime) from the supplement cache. ({}, None) on any failure —
    the supplement is purely additive, so a missing/broken file just means the
    universe falls back to EDGAR ∪ book ∪ allowlist."""
    try:
        if _SUPPLEMENT_CACHE.exists():
            mt = _SUPPLEMENT_CACHE.stat().st_mtime
            data = json.loads(_SUPPLEMENT_CACHE.read_text(encoding="utf-8"))
            raw = data.get("tickers") if isinstance(data, dict) else data
            syms = {str(t).upper().strip() for t in (raw or []) if t}
            return syms, mt
    except Exception as e:
        log.warning(f"[factcheck] supplement cache unreadable, ignoring: {e}")
    return set(), None


def load_ticker_master(cfg=None) -> set:
    """The union that defines 'a ticker that could exist':
        EDGAR-cache tickers ∪ holdings ∪ watchlist ∪ config ETF/crypto allowlist
        ∪ supplement (Alpaca tradable equities + auto-learned symbols)
        ∪ Nico's own open personal holdings (_personal_held; review fix 2026-11-19).
    Cached on the EDGAR cache file's mtime (plus the supplement mtime, allowlist, and
    book, which can change independently). Degrades to book ∪ allowlist ∪ supplement
    if the EDGAR cache is unreadable."""
    allow = _fc_get(cfg, ["ticker_allowlist"], []) or []
    allow_set = {str(t).upper() for t in allow if t}
    holds, watch = _book_sets()
    mine = _personal_held(cfg)
    supp, supp_mt = _load_supplement()
    extra_key = (tuple(sorted(allow_set)), tuple(sorted(holds)),
                 tuple(sorted(watch)), supp_mt, tuple(sorted(mine)))

    try:
        mtime = _EDGAR_CACHE.stat().st_mtime if _EDGAR_CACHE.exists() else None
    except Exception:
        mtime = None

    if (_master_cache["master"] is not None
            and _master_cache["mtime"] == mtime
            and _master_cache["extra_key"] == extra_key):
        return _master_cache["master"]

    titles: dict = {}
    try:
        if _EDGAR_CACHE.exists():
            titles = _parse_edgar_cache(json.loads(_EDGAR_CACHE.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning(f"[factcheck] EDGAR ticker cache unreadable, degrading to book∪allowlist∪supplement: {e}")
        titles = {}

    master = set(titles.keys()) | holds | watch | allow_set | supp | mine
    _master_cache.update({"mtime": mtime, "master": master, "titles": titles,
                          "extra_key": extra_key})
    return master


def reset_master_cache() -> None:
    """Drop the in-process ticker-master cache (tests / hot config reload)."""
    _master_cache.update({"mtime": None, "master": None, "titles": None, "extra_key": None})


def _fetch_alpaca_symbols() -> set:
    """All tradable US-equity symbols from Alpaca's /assets. set() on any failure —
    the caller treats the supplement as best-effort."""
    try:
        import mason_execution as EX
        code, data = EX._request("GET", "/assets",
                                 params={"status": "active", "asset_class": "us_equity"})
        if code != 200 or not isinstance(data, list):
            log.warning(f"[factcheck] alpaca /assets returned {code}")
            return set()
        return {str(a.get("symbol", "")).upper().strip()
                for a in data if a.get("tradable") and a.get("symbol")}
    except Exception as e:
        log.warning(f"[factcheck] alpaca assets fetch failed: {e}")
        return set()


def _learned_symbols(limit: int = 5000) -> set:
    """Auto-learned universe: distinct tickers Mason has actually seen tagged in the
    news DB. Cheap read; set() on any failure (table absent, bad JSON, etc.)."""
    out: set = set()
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT tickers FROM news_events WHERE tickers IS NOT NULL "
                "ORDER BY ingested_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            raw = r["tickers"]
            try:
                arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                arr = []
            for t in arr:
                if t:
                    out.add(str(t).upper().strip())
    except Exception as e:
        log.warning(f"[factcheck] learned-symbols read failed: {e}")
    return out


def refresh_ticker_universe(cfg=None) -> dict:
    """Background refresh of the ticker universe (call on a cadence, e.g. daily/weekly
    from the news service). Refreshes the EDGAR cache via mason_edgar (which
    self-throttles to ~weekly) and rebuilds the supplement file from Alpaca tradable
    US equities ∪ auto-learned news-tag symbols. Busts the in-process master cache.
    Never raises — returns a small count dict for logging."""
    out = {"edgar": 0, "alpaca": 0, "learned": 0, "supplement": 0}
    try:
        import mason_edgar
        m = mason_edgar._load_tickers_map()   # writes .edgar_tickers_cache.json if stale
        out["edgar"] = len(m or {})
    except Exception as e:
        log.warning(f"[factcheck] EDGAR refresh failed: {e}")

    supp: set = set()
    alp = _fetch_alpaca_symbols()
    out["alpaca"] = len(alp)
    supp |= alp
    learned = _learned_symbols()
    out["learned"] = len(learned)
    supp |= learned

    if supp:
        try:
            _SUPPLEMENT_CACHE.write_text(
                json.dumps({"tickers": sorted(supp), "updated": time.time(),
                            "sources": {"alpaca": len(alp), "learned": len(learned)}}),
                encoding="utf-8")
            out["supplement"] = len(supp)
        except Exception as e:
            log.warning(f"[factcheck] supplement write failed: {e}")
    reset_master_cache()
    log.info("[factcheck] universe refresh: %s", out)
    return out


# ── text + number normalization ─────────────────────────────────────────────────

def _decode(text) -> str:
    """HTML-entity-decode and strip tags so matching sees plain text."""
    if not text:
        return ""
    s = _html.unescape(str(text))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NUM_RE = re.compile(r"(?<![\w.])(\$?\d[\d,]*(?:\.\d+)?)\s*(¢|c\b)?", re.I)
# A ticker candidate is a STANDALONE token, not a prefix of a longer all-caps word
# (so 'CLARITY' is NOT read as the ticker 'CLARIT'). Two shapes: a bare uppercase
# symbol of 2–5 letters (optional .A class suffix), or an explicit $CASHTAG. The
# lookarounds forbid an adjacent alphanumeric or dot on either side.
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:\$[A-Za-z]{1,7}|[A-Z]{2,5}(?:\.[A-Z]{1,2})?)(?![A-Za-z0-9])")


def normalize_number(token: str):
    """'$6.5' / '6.50' / '65¢' → float dollars. None if not a number.
    A trailing ¢ (or bare 'c') divides by 100."""
    if token is None:
        return None
    s = str(token).strip()
    cents = False
    if s.endswith("¢"):
        cents, s = True, s[:-1].strip()
    elif re.search(r"\d\s*c$", s, re.I):
        cents, s = True, re.sub(r"c$", "", s, flags=re.I).strip()
    s = s.replace("$", "").replace(",", "").strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if cents else v


# market-context cues: a token is being used AS a ticker (not an acronym/word) when
# it is a $cashtag, parenthetical "(XXX)", or sits next to one of these verbs.
_MKT_CUE = (r"(?:'s|’s)?\s+(?:shares?|stock|stocks|equity|rose|fell|gained|lost|"
            r"jumped|surged|plunged|dropped|rallied|climbed|slid|slipped|soared|"
            r"tanked|remains?|remained|trades?|traded|closed|beat|missed|reported|"
            r"announced|guided|advances?|is\s+(?:up|down|higher|lower|bullish|bearish))")


def looks_like_ticker_mention(text: str, sym: str) -> bool:
    """True when `sym` is used AS a ticker in `text` — a $cashtag, a parenthetical
    "(SYM)", or a symbol next to a market verb ("SYM shares/rose/remains ..."). Used
    to spare ordinary acronyms (NIH, SARS, IT) from the invented-ticker / ticker-gate
    rules, while still catching a corrupted symbol presented as a ticker ("PRX
    remains bullish")."""
    if not sym:
        return False
    s = re.escape(sym)
    if re.search(rf"\${s}\b", text, re.I):
        return True
    if re.search(rf"\(\s*{s}\s*\)", text):
        return True
    if re.search(rf"\b{s}{_MKT_CUE}", text, re.I):
        return True
    return False


def candidate_tickers(text: str) -> list:
    """Ticker-shaped tokens in a claim, upper-cased, de-$'d, minus stopwords.
    Deliberately liberal so an INVENTED symbol like 'PRX' is caught for the
    existence/citation rule; the caller decides which are real."""
    out, seen = [], set()
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0).lstrip("$").upper()
        if tok in _TICKER_STOPWORDS:
            continue
        # require it to look like a symbol: 2–7 chars, contains a letter
        if not (2 <= len(tok) <= 7) or not re.search(r"[A-Z]", tok):
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# ── claim extraction ────────────────────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def extract_claims(summary: str, details: dict | None = None) -> list:
    """Split an analyst summary + details into atomic claim dicts [{text}]. Keeps
    only non-trivial sentences. Details' free-text sub-fields are appended."""
    chunks = []
    if summary:
        chunks.append(_decode(summary))
    if isinstance(details, dict):
        for k in ("what_changed", "why_it_matters", "levels_or_refs"):
            v = details.get(k)
            if v and isinstance(v, str) and v.strip().lower() not in ("none stated", "none", "n/a", ""):
                chunks.append(_decode(v))
    claims = []
    for chunk in chunks:
        for part in _SENT_SPLIT.split(chunk):
            t = part.strip()
            if len(t) >= 8:                 # drop fragments / bare labels
                claims.append({"text": t})
    return claims


# ── deterministic per-claim checks ──────────────────────────────────────────────

def ticker_ok(sym: str, source_tickers, master) -> bool:
    """True iff `sym` may appear in a claim.
      * news claim (source_tickers is a list): sym ∈ source_tickers AND sym ∈ master
        — the citation rule that alone kills 'PRX'.
      * system-sourced line (source_tickers is None): sym ∈ master (book already
        folded into master); no citation needed."""
    if not sym:
        return False
    s = sym.upper()
    if source_tickers is None:              # system-sourced
        return s in master
    src = {str(t).upper() for t in (source_tickers or [])}
    return s in src and s in master


def check_claim(claim: dict, source_text: str, source_tickers, master,
                price_fn=None, cfg=None, *, system: bool = False,
                macro: bool = False) -> dict:
    """Deterministic verification of ONE claim. Returns
        {verdict: 'verified'|'dropped', reason: str, text: <possibly edited text>}.
    A verified claim's `text` may have an out-of-band price number stripped.
    Never raises. This pass is FINAL for drops — the qwen diff may only demote a
    'verified' result further, never promote a 'dropped' one.

    Modes:
      * default (news): full ticker citation + linkage + price rules.
      * system=True: auto-trusted ground truth; only the existence ticker-gate runs.
      * macro=True: world/market claims have no cited tickers, so the CITATION and
        LINKAGE rules relax — a real ticker may be named without citation — but an
        INVENTED ticker (used as one, not in master, not in the source) is still
        dropped, and price sanity + the qwen source-diff still audit the claim."""
    try:
        text = _decode(claim.get("text") if isinstance(claim, dict) else claim)
        if not text:
            return {"verdict": "dropped", "reason": "empty", "text": ""}
        src = {str(t).upper() for t in (source_tickers or [])}
        src_text = _decode(source_text).upper()

        def _named_in_source(sym: str) -> bool:
            return bool(re.search(rf"\b{re.escape(sym)}\b", src_text))

        cands = candidate_tickers(text)

        # 1) ticker existence + citation. A candidate is ALLOWED iff it is cited by
        #    this claim's source news (in the tag list) OR literally named in the
        #    source text (ground truth). Otherwise: 'uncited_ticker' if it is a real
        #    symbol, 'invented_ticker' if no such symbol exists. The source-text
        #    escape hatch stops company-name fragments ("PRF Technologies") from
        #    being mistaken for invented tickers, while still killing 'PRX' (never
        #    in the source) and the uncited 'IONQ' linkage.
        recognized = []                       # real tickers the claim legitimately names
        for c in cands:
            in_master = c in master
            if system:
                if not in_master:
                    # an unknown token in a system line fails the ticker gate only
                    # if it is actually used as a ticker (spares NIH/SARS/IT).
                    if looks_like_ticker_mention(text, c):
                        return {"verdict": "dropped", "reason": f"unknown_ticker:{c}", "text": text}
                    continue
                recognized.append(c)
                continue
            allowed = (c in src) or _named_in_source(c)
            if macro:
                # macro: no cited-ticker universe, so citation is relaxed. A real
                # symbol is fine uncited; only an invented ticker used AS a ticker
                # (not in master, not in the source) is a fabrication.
                if not in_master and not allowed and looks_like_ticker_mention(text, c):
                    return {"verdict": "dropped", "reason": f"invented_ticker:{c}", "text": text}
                if in_master:
                    recognized.append(c)
                continue
            if in_master:
                if not allowed:
                    # a REAL symbol, uncited by this source → drop (kills IONQ linkage)
                    return {"verdict": "dropped", "reason": f"uncited_ticker:{c}", "text": text}
                recognized.append(c)
            else:
                # an UNKNOWN token: an invented ticker only if presented as one
                # ("PRX remains bullish"); otherwise it is an acronym / word / name
                # fragment (NIH, SARS, "PRF Technologies") and is ignored.
                if not allowed and looks_like_ticker_mention(text, c):
                    return {"verdict": "dropped", "reason": f"invented_ticker:{c}", "text": text}

        # 2) cross-ticker linkage: a claim naming ≥2 real tickers asserts a link
        #    between them, so EVERY one must be a genuine subject of this source —
        #    i.e. in its tag list (co-cited), not merely name-dropped in the body.
        #    (Step 1 already dropped any ticker neither cited nor named, so this
        #    adds the stricter co-citation bar the linkage rule needs.) Relaxed for
        #    macro, where cross-market linkages are the whole point.
        if not system and not macro and len(set(recognized)) >= 2:
            for c in set(recognized):
                if c not in src:
                    return {"verdict": "dropped", "reason": "uncited_linkage", "text": text}

        # 3) number / price sanity (skip for system lines: ground truth by build)
        if not system and price_fn is not None:
            band = float(_fc_get(cfg, ["price_band_pct"], 5.0) or 5.0) / 100.0
            price_ctx = re.search(r"(entry|stop|target|price|level|trades? at|at\s+\$)", text, re.I)
            edited = text
            for m in _NUM_RE.finditer(text):
                raw = (m.group(1) + (m.group(2) or "")).strip()
                looks_price = raw.startswith("$") or (m.group(2) is not None) or bool(price_ctx)
                if not looks_price:
                    continue
                val = normalize_number(raw)
                if val is None:
                    continue
                ref_tkr = recognized[0] if recognized else (list(src)[0] if src else None)
                if not ref_tkr:
                    continue
                try:
                    ref = price_fn(ref_tkr)
                except Exception:
                    ref = None
                if ref is None or ref <= 0:
                    continue
                if abs(val - ref) / ref > band:
                    edited = edited.replace(raw, "").replace("  ", " ").strip()
            if edited != text:
                # if stripping the number gutted the claim (it WAS the point), drop it
                residue = re.sub(r"(entry|stop|target|price|level|at)\b", "", edited, flags=re.I)
                if len(re.sub(r"[^A-Za-z]", "", residue)) < 6:
                    return {"verdict": "dropped", "reason": "fabricated_price", "text": text}
                return {"verdict": "verified", "reason": "price_stripped", "text": edited}

        return {"verdict": "verified", "reason": "ok", "text": text}
    except Exception as e:               # fail-closed: a checker crash never crashes the caller
        log.warning(f"[factcheck] check_claim error, dropping claim: {e}")
        return {"verdict": "dropped", "reason": f"checker_error:{e}", "text": ""}


# ── qwen source-diff (demote-only) ──────────────────────────────────────────────

_DIFF_SYS = (
    "You are a strict source-checker. You are given the ONLY permitted source text "
    "and a numbered list of claims. For EACH claim decide, USING ONLY THE SOURCE "
    "TEXT and NO outside knowledge, whether it is:\n"
    "  supported      — the source text states or clearly implies it,\n"
    "  contradicted   — the source text says the opposite,\n"
    "  not-in-source  — the source text does not contain it.\n"
    "Do NOT use anything you know beyond the source text. Reply with STRICT JSON "
    'only, mapping each claim number to its verdict, e.g. {"1":"supported",'
    '"2":"not-in-source"}. No prose.'
)


def _parse_diff_json(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return None
    try:
        d = json.loads(s[a:b + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def qwen_diff(claims: list, source_text: str, llm_fn, cfg=None) -> dict:
    """ONE intent='chore' diff of `claims` against `source_text`. Returns
        {index: {'verdict': 'supported'|'contradicted'|'not-in-source'|'unverified',
                 'reason': str}}  for index 0..len-1.
    Fail-closed: an unparseable / empty / malformed reply yields 'unverified' for
    every claim (the caller then keeps the deterministic result — NO demotion, NO
    fabricated verdict). This function can only ever be used to DEMOTE."""
    n = len(claims or [])
    if n == 0:
        return {}
    src = _decode(source_text)
    lines = []
    for i, c in enumerate(claims):
        t = _decode(c.get("text") if isinstance(c, dict) else c)
        lines.append(f"{i + 1}. {t}")
    user = ("SOURCE TEXT (the only thing you may use):\n"
            f"\"\"\"\n{src or '(no source text provided)'}\n\"\"\"\n\n"
            "CLAIMS:\n" + "\n".join(lines) + "\n\nReturn the strict-JSON verdict map now.")
    try:
        raw = llm_fn(_DIFF_SYS, user, num_predict=280, timeout=90)
    except Exception as e:
        log.warning(f"[factcheck] qwen_diff llm error, fail-closed: {e}")
        return {i: {"verdict": "unverified", "reason": "diff_llm_error"} for i in range(n)}
    d = _parse_diff_json(raw)
    if not d:
        return {i: {"verdict": "unverified", "reason": "diff_unparseable"} for i in range(n)}
    out = {}
    for i in range(n):
        v = d.get(str(i + 1))
        if v is None:
            v = d.get(i + 1)
        vv = str(v or "").strip().lower().replace("_", "-")
        if vv not in ("supported", "contradicted", "not-in-source"):
            out[i] = {"verdict": "unverified", "reason": "diff_missing"}
        else:
            out[i] = {"verdict": vv, "reason": "diff"}
    return out


# ── audit builder ────────────────────────────────────────────────────────────────

def audit(verified: list, dropped: list) -> dict:
    """Structured audit for storage + metrics. `verified`/`dropped` are lists of
    claim dicts (each at least {text}; dropped carries {reason})."""
    checked = len(verified) + len(dropped)
    return {
        "checked": checked,
        "verified": len(verified),
        "dropped": [{"text": (d.get("text") or "")[:240], "reason": d.get("reason", "")}
                    for d in dropped],
        "verified_ratio": round(len(verified) / checked, 3) if checked else 1.0,
    }


def verify_claims(claims: list, source_text: str, source_tickers, master,
                  *, price_fn=None, llm_fn=None, cfg=None, system: bool = False,
                  macro: bool = False, run_diff: bool = False) -> dict:
    """Run the full check on a list of claims: deterministic `check_claim` on each,
    then (iff run_diff and llm_fn) a single demote-only `qwen_diff`. Returns
        {verified: [claim...], dropped: [claim...(+reason)], audit: {...}}.
    Verified claims carry their possibly-edited `text`. Never raises out.
    `macro=True` relaxes the ticker citation/linkage rules (see check_claim)."""
    verified, dropped = [], []
    det = []
    for c in (claims or []):
        r = check_claim(c, source_text, source_tickers, master,
                        price_fn=price_fn, cfg=cfg, system=system, macro=macro)
        det.append(r)

    survivors, surv_idx = [], []
    for i, (c, r) in enumerate(zip(claims or [], det)):
        if r["verdict"] == "verified":
            survivors.append({"text": r["text"], "reason": r["reason"]})
            surv_idx.append(i)
        else:
            base = dict(c) if isinstance(c, dict) else {"text": str(c)}
            base["reason"] = r["reason"]
            dropped.append(base)

    # qwen diff can only DEMOTE survivors of the deterministic pass
    if run_diff and llm_fn is not None and survivors:
        verdicts = qwen_diff(survivors, source_text, llm_fn, cfg=cfg)
        kept = []
        for j, claim in enumerate(survivors):
            v = (verdicts.get(j) or {}).get("verdict", "unverified")
            if v in ("contradicted", "not-in-source"):
                d = dict(claim)
                d["reason"] = f"diff_{v}"
                dropped.append(d)
            else:                            # supported OR unverified → keep (no promotion)
                kept.append(claim)
        survivors = kept

    verified = survivors
    return {"verified": verified, "dropped": dropped, "audit": audit(verified, dropped)}


# ── Layer 0: ingestion tag-relevance filter ─────────────────────────────────────

def _is_macro(text_lc: str, n_tags: int, macro_min: int) -> bool:
    if n_tags >= macro_min:
        return True
    return any(kw in text_lc for kw in _MACRO_KEYWORDS)


def _title_tokens(title: str) -> list:
    """Significant lowercase tokens of a company title (drops corp suffixes)."""
    stop = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
            "holdings", "holding", "group", "the", "&", "sa", "ag", "nv", "class",
            "common", "stock", "technologies", "technology", "industries",
            "international", "systems", "solutions", "and"}
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())
    return [t for t in toks if len(t) >= 4 and t not in stop]


def filter_relevant_tickers(headline: str, body: str, tickers: list, master=None,
                            cfg=None, titles: dict | None = None) -> tuple:
    """Layer 0. Keep a tagged ticker iff:
       * its SYMBOL appears as a standalone token in headline+body, OR
       * its EDGAR company TITLE (a significant token of it) appears there, OR
       * it is the SOLE tag, OR
       * it is the PRIMARY (first) tag AND the story is not macro/market-wide.
    Deterministic, no qwen. Returns (kept, dropped) preserving order. Never raises.
    HTML entities are decoded before matching."""
    try:
        tks = [str(t).upper() for t in (tickers or []) if t]
        if len(tks) <= 1:
            return list(tks), []
        titles = titles if titles is not None else load_ticker_titles(cfg)
        text = _decode((headline or "") + "  " + (body or ""))
        text_lc = text.lower()
        macro_min = int(_fc_get(cfg, ["tag_filter", "macro_min"], 3) or 3)
        macro = _is_macro(text_lc, len(tks), macro_min)

        kept, dropped = [], []
        for i, tk in enumerate(tks):
            named = re.search(rf"\b{re.escape(tk)}\b", text) is not None
            if not named:
                for tok in _title_tokens(titles.get(tk, "")):
                    if re.search(rf"\b{re.escape(tok)}\b", text_lc):
                        named = True
                        break
            if named:
                kept.append(tk)
            elif i == 0 and not macro:
                kept.append(tk)             # trust the primary tag on a single-name story
            else:
                dropped.append(tk)
        return kept, dropped
    except Exception as e:
        log.warning(f"[factcheck] filter_relevant_tickers error, keeping all: {e}")
        return list(tickers or []), []


# ── thin-note helper ─────────────────────────────────────────────────────────────

def thin_note(ticker: str | None, n_dropped: int) -> str:
    """The honest fallback stored when everything a claim-set contained was stripped.
    Never blank, never the raw hallucination."""
    who = ticker or "the book"
    tail = f" ({n_dropped} unverifiable claim{'s' if n_dropped != 1 else ''} dropped)" if n_dropped else ""
    return f"No independently verifiable claim in source for {who} this cycle{tail}."


def materiality_floor(cfg=None) -> float:
    """Below this news-event materiality a claim set is checked deterministically
    only — no qwen diff — so low-value rows cost zero GPU."""
    try:
        return float(_fc_get(cfg, ["materiality_floor"], 0.5) or 0.5)
    except Exception:
        return 0.5


def _default_price(ticker: str):
    """The already-cached spot helper (best-effort, None on failure). Injected in
    tests — this module never hits the network directly."""
    try:
        import mason_news_service as NS
        return NS._last_price(ticker)
    except Exception:
        return None


def _source_from_events(events: list) -> tuple:
    """(source_text, source_tickers) for a per-ticker analyst output. source_text is
    the concatenated headline + implication of every event; source_tickers is the
    UNION of every event's tag list (the citation universe for these claims)."""
    texts, tks = [], set()
    for ev in (events or []):
        h = _decode(ev.get("headline") or "")
        impl = _decode(ev.get("implication") or "")
        if h:
            texts.append(h)
        if impl:
            texts.append(impl)
        raw = ev.get("tickers")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = [raw]
        for t in (raw or []):
            if t:
                tks.add(str(t).upper())
    return "\n".join(texts), sorted(tks)


# ── Layer 1: analyst-output fact check ──────────────────────────────────────────

def factcheck_analysis(summary: str, sentiment: str, details: dict | None,
                       events: list, *, master=None, price_fn=None, llm_fn=None,
                       cfg=None, ticker: str | None = None,
                       run_diff: bool = False) -> tuple:
    """Layer 1. Validate ONE per-ticker analyst output against its own source events.
    Returns (verified_summary, details_out, audit_dict).
      * verified_summary: rebuilt from ONLY the verified claims; the honest thin
        note if nothing survives — never blank, never the raw hallucination.
      * details_out: the original details PLUS `raw_summary` (the pre-check text, kept
        for audit) and `claims` (per-claim {text, verdict, reason}).
      * audit_dict: audit(...) for extra_json.factcheck + metrics.
    The caller is responsible for the GPU yield before this runs a diff (pass
    run_diff only after gating). Never raises out — degrades to the raw output on
    any internal error so the scan never dies."""
    try:
        master = master if master is not None else load_ticker_master(cfg)
        price_fn = price_fn if price_fn is not None else _default_price
        source_text, source_tickers = _source_from_events(events)
        claims = extract_claims(summary, details)
        if not claims:
            details_out = dict(details or {})
            details_out["raw_summary"] = summary
            details_out["claims"] = []
            return (summary or thin_note(ticker, 0), details_out, audit([], []))
        res = verify_claims(claims, source_text, source_tickers, master,
                            price_fn=price_fn, llm_fn=llm_fn, cfg=cfg,
                            system=False, run_diff=run_diff)
        verified = res["verified"]
        # correct a mangled ticker in a verified claim to a cited source ticker is
        # unnecessary here: the citation rule already dropped any claim naming an
        # uncited/invented symbol, so what survives cites only source tickers.
        if verified:
            verified_summary = " ".join(c["text"] for c in verified).strip()
        else:
            verified_summary = thin_note(ticker, len(res["dropped"]))
        details_out = dict(details or {})
        details_out["raw_summary"] = summary
        details_out["claims"] = [{"text": c["text"], "verdict": "verified",
                                  "reason": c.get("reason", "ok")} for c in verified] + \
                                [{"text": (c.get("text") or "")[:240], "verdict": "dropped",
                                  "reason": c.get("reason", "")} for c in res["dropped"]]
        return verified_summary, details_out, res["audit"]
    except Exception as e:
        log.warning(f"[factcheck] factcheck_analysis error, keeping raw output: {e}")
        details_out = dict(details or {})
        details_out["raw_summary"] = summary
        return summary, details_out, audit([], [])


def factcheck_digest(summary: str, source_text: str, *, master=None, price_fn=None,
                     llm_fn=None, cfg=None, run_diff: bool = False) -> tuple:
    """Layer-1 fact-check for a MACRO / world DIGEST (macro mode — no cited-ticker
    universe). Splits the digest into claims, runs the macro-mode deterministic
    check (relaxed ticker citation/linkage; invented tickers + bad numbers still
    caught) plus the optional demote-only source-diff, and rebuilds a verified
    digest. Returns (verified_summary, details, audit_dict). Never raises out."""
    try:
        master = master if master is not None else load_ticker_master(cfg)
        price_fn = price_fn if price_fn is not None else _default_price
        claims = extract_claims(summary, None)
        if not claims:
            return (summary or thin_note("the macro tape", 0),
                    {"raw_summary": summary, "claims": []}, audit([], []))
        res = verify_claims(claims, source_text, None, master, price_fn=price_fn,
                            llm_fn=llm_fn, cfg=cfg, macro=True, run_diff=run_diff)
        verified = res["verified"]
        vs = (" ".join(c["text"] for c in verified).strip()
              if verified else thin_note("the macro tape", len(res["dropped"])))
        details = {"raw_summary": summary,
                   "claims": [{"text": c["text"], "verdict": "verified",
                               "reason": c.get("reason", "ok")} for c in verified] +
                             [{"text": (c.get("text") or "")[:240], "verdict": "dropped",
                               "reason": c.get("reason", "")} for c in res["dropped"]]}
        return vs, details, res["audit"]
    except Exception as e:
        log.warning(f"[factcheck] factcheck_digest error, keeping raw: {e}")
        return summary, {"raw_summary": summary, "claims": []}, audit([], [])


def audit_reply(reply_text: str, source_text: str, *, master=None, price_fn=None,
                cfg=None, run_diff: bool = False) -> dict:
    """READ-ONLY advisory audit of a LIVE CHAT reply (Path B).

    Unlike factcheck_analysis / factcheck_digest, this NEVER rewrites the reply — the
    reply has already streamed to the user. It only runs the deterministic claim
    check (macro mode: relaxed ticker citation, but invented tickers + bad numbers +
    price sanity still caught) and returns a compact verdict the UI turns into a
    post-reply badge/note. Per Nico's spec, an unknown/uncited ticker surfaces as
    'unverified' — it is reported, never corrected. Never raises.

    Returns {status, checked, items[]} where:
      * status: 'clear' (nothing to flag) | 'flagged' (items present) | 'error'
      * checked: number of claims examined
      * items: [{text, reason}] for each claim that could not be verified
    """
    try:
        if not (reply_text or "").strip():
            return {"status": "clear", "checked": 0, "items": []}
        master = master if master is not None else load_ticker_master(cfg)
        price_fn = price_fn if price_fn is not None else _default_price
        claims = extract_claims(reply_text, None)
        if not claims:
            return {"status": "clear", "checked": 0, "items": []}
        res = verify_claims(claims, source_text or "", None, master,
                            price_fn=price_fn, cfg=cfg, macro=True, run_diff=run_diff)
        dropped = res.get("dropped", [])
        if not dropped:
            return {"status": "clear", "checked": len(claims), "items": []}
        items = [{"text": (d.get("text") or "")[:200], "reason": d.get("reason", "")}
                 for d in dropped]
        return {"status": "flagged", "checked": len(claims), "items": items,
                "audit": res.get("audit", {})}
    except Exception as e:
        log.warning(f"[factcheck] audit_reply error, no verdict: {e}")
        return {"status": "error", "checked": 0, "items": []}


# ── metrics (rolling day-windowed counters in desk_state, injected accessors) ────

def _day_key(now=None) -> str:
    ts = now if now is not None else time.time()
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def roll_metrics(audit_dict: dict, get_state, set_state, *, now=None) -> None:
    """Add one audit's counts into the day-windowed desk_state counters
    (factcheck_<day>_checked/_verified/_dropped) plus all-time totals. Injectable
    get_state/set_state so it is testable without a DB. Never raises."""
    try:
        day = _day_key(now)
        checked = int(audit_dict.get("checked", 0) or 0)
        verified = int(audit_dict.get("verified", 0) or 0)
        dropped = checked - verified
        for scope in (f"factcheck_{day}", "factcheck_all"):
            for field, inc in (("checked", checked), ("verified", verified), ("dropped", dropped)):
                key = f"{scope}_{field}"
                try:
                    cur = int(float(get_state(key, 0) or 0))
                except (TypeError, ValueError):
                    cur = 0
                set_state(key, cur + inc)
    except Exception as e:
        log.warning(f"[factcheck] roll_metrics error: {e}")


def _clean_line(s: str) -> str:
    return re.sub(r"^\s*[-+!*•]\s*", "", s or "").strip()


def _sentences(text: str) -> list:
    return [s.strip() for s in _SENT_SPLIT.split(_decode(text)) if len(s.strip()) >= 8]


_LEAD_SYS = (
    "You are Mason's desk editor. You are given a set of ALREADY-VERIFIED claims. "
    "Write ONE tight lead paragraph (2–4 sentences) that only RESTATES and connects "
    "these claims for a busy trader. You are a renderer, not an author: do NOT add "
    "any ticker, number, price, or linkage that is not already present in the claims. "
    "Plain prose, no JSON, no bullet points."
)


def assemble_report(kind: str, label: str, news_claims: list, contexts: dict, *,
                    master=None, llm_fn=None, cfg=None, run_diff: bool = False,
                    ticker: str | None = None) -> tuple:
    """Layer 2. Build the Desk Report DETERMINISTICALLY from Layer-1-verified news
    claims + system-sourced context lines (template assembly), optionally add a
    qwen-phrased connective lead that is diff-verified back against the claim set,
    run the ticker gate on the final text, and fall back to the honest thin note if
    nothing survives.

    Returns (report_text, verified_claim_set, audit). `verified_claim_set` is the
    cited claim list attached to the director package. qwen may only PHRASE verified
    claims — it can never introduce a ticker/number/linkage the diff + ticker gate
    do not find in the claim set. Never raises out."""
    try:
        master = master if master is not None else load_ticker_master(cfg)
        news_claims = [c for c in (news_claims or []) if (c.get("text") or "").strip()]

        # system claim set from the deterministic (non-news) contexts — ground truth
        system_claims = []
        _SECTIONS = [("macro", "World & macro"), ("sentiment", "Risk & regime"),
                     ("analytics", "Positions & signals"), ("fundamentals", "Portfolio"),
                     ("positioning", "Positioning & flow")]
        for key, _lbl in _SECTIONS:
            v = contexts.get(key) if isinstance(contexts, dict) else None
            if not v or "unavailable" in (v or "")[:40]:
                continue
            for ln in str(v).splitlines():
                c = _clean_line(ln)
                if len(c) >= 8 and not c.endswith(":"):
                    system_claims.append({"source": "system", "section": key, "text": c})

        verified_claim_set = (
            [{"source": "news", "ticker": c.get("ticker"), "text": c["text"]} for c in news_claims]
            + system_claims)

        if not verified_claim_set:
            return (thin_note(ticker, 0), [], audit([], []))

        # the source of truth for the Layer-2 diff: the verified claims themselves
        claim_source_text = "\n".join(c["text"] for c in verified_claim_set)
        # tickers the report is allowed to name (news tickers are the cited set)
        allowed = {str(c.get("ticker")).upper() for c in news_claims if c.get("ticker")}

        # ── deterministic assembly (the report is always built from claims) ──
        sections = []
        if news_claims:
            seen, lines = set(), []
            for c in news_claims:
                key = (c.get("ticker"), c["text"])
                if key in seen:
                    continue
                seen.add(key)
                tk = c.get("ticker")
                lines.append(f"  - {tk + ': ' if tk else ''}{c['text']}")
            sections.append("What moved (verified per-ticker reads):\n" + "\n".join(lines))
        for key, lbl in _SECTIONS:
            v = contexts.get(key) if isinstance(contexts, dict) else None
            if v and "unavailable" not in (v or "")[:40]:
                sections.append(str(v).strip())

        # ── optional qwen connective lead, diff-verified + ticker-gated ──
        lead_verified, lead_dropped = [], []
        lead_text = ""
        if run_diff and llm_fn is not None and news_claims:
            bullets = "\n".join(f"- {(c.get('ticker') + ': ') if c.get('ticker') else ''}{c['text']}"
                                for c in news_claims)
            try:
                raw = llm_fn(_LEAD_SYS, "VERIFIED CLAIMS:\n" + bullets +
                             "\n\nWrite the lead paragraph now.", num_predict=220, timeout=120)
            except Exception as e:
                log.warning(f"[factcheck] layer2 lead llm error: {e}")
                raw = ""
            sents = _sentences(raw)
            if sents:
                verdicts = qwen_diff(sents, claim_source_text, llm_fn, cfg=cfg)
                for i, s in enumerate(sents):
                    v = (verdicts.get(i) or {}).get("verdict", "unverified")
                    # ticker gate: a ticker actually MENTIONED in the sentence must
                    # be an allowed (cited) or real (master) symbol — an unknown
                    # symbol used as a ticker (PRFX→PRX) is a synthesis fabrication.
                    bad_ticker = any((t not in allowed and t not in master
                                      and looks_like_ticker_mention(s, t))
                                     for t in candidate_tickers(s))
                    if v == "supported" and not bad_ticker:
                        lead_verified.append({"text": s})
                    else:
                        reason = "layer2_bad_ticker" if bad_ticker else f"layer2_diff_{v}"
                        lead_dropped.append({"text": s, "reason": reason})
                if lead_verified:
                    lead_text = " ".join(c["text"] for c in lead_verified)

        header = f"{label} Desk Report — assembled from {len(verified_claim_set)} verified claim(s)."
        parts = [header]
        if lead_text:
            parts.append(lead_text)
        parts.extend(sections)
        report_text = "\n\n".join(parts).strip()

        aud = audit(lead_verified, lead_dropped)
        aud["news_claims"] = len(news_claims)
        aud["system_claims"] = len(system_claims)
        return report_text, verified_claim_set, aud
    except Exception as e:
        log.warning(f"[factcheck] assemble_report error: {e}")
        return (thin_note(ticker, 0), [], audit([], []))


def read_metrics(get_state, *, now=None, days: int = 1) -> dict:
    """Read the rolling counters for a stats endpoint. Returns
    {today:{checked,verified,dropped,hallucination_rate}, all_time:{...}}."""
    def _grab(scope):
        out = {}
        for f in ("checked", "verified", "dropped"):
            try:
                out[f] = int(float(get_state(f"{scope}_{f}", 0) or 0))
            except (TypeError, ValueError):
                out[f] = 0
        c = out["checked"]
        out["hallucination_rate"] = round(out["dropped"] / c, 3) if c else 0.0
        return out
    return {"today": _grab(f"factcheck_{_day_key(now)}"), "all_time": _grab("factcheck_all")}
