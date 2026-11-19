"""
mason_sources.py — Catalyst Desk free structured data sources (Agent A).
Spec: docs/prompts/CATALYST_DESK_RESEARCH_LAYER.md §2/§4; interfaces:
docs/prompts/CATALYST_DESK_CONTRACTS.md §2.

One public entry point, `scan_sources(conn, universe, holds) -> int`, called by
mason_news_service.scan_once every ~120s. It returns the number of NEW
news_events rows written. Everything is behind mason.config.json
`catalyst.sources.enabled` (default false → return 0, no network, no writes),
plus a per-source sub-flag `catalyst.sources.<key>`.

Why this exists: the META "Muse hits #1 on the App Store" headline reached us only
second-hand and unticked; IPO pricings, trial readouts, FDA approvals and
prediction-market odds swings had no primary feed at all. Each source here is a
free, keyless (FRED/EIA: free key) primary feed turned into either a headline
(news_events, with `explicit_tickers` + a structured `event_kind_hint` triage
trusts) or a scheduled item (calendar_events), or both.

Sources (registry key → what):
  app_charts          Apple App Store US top-free (marketing-tools JSON) + top-grossing
                      (legacy iTunes RSS JSON). Diff vs snapshot → #1 / enters top 10 /
                      jumps ≥20 for public-company apps (publisher → ticker table).
                      Google Play: skipped — no stable free no-auth chart endpoint.
  edgar_*             SEC current-filings Atom: S-1, F-1 (IPO filing), 424B4 (pricing),
                      SCHEDULE 13D (activist), Form 4 (insider open-market buys; XML
                      parsed from the full-submission .txt, drained a few per pass).
  ipo_calendar        NASDAQ IPO calendar JSON → calendar_events + priced / >$500M headlines.
  clinical_trials     ClinicalTrials.gov v2: industry Phase 3 (+ large Phase 2) primary
                      completions in the next 60 days → calendar_events; status flips →
                      trial_update headlines (mapped sponsors only).
  fda_rss / openfda   FDA press + "what's new: drugs" RSS; openFDA novel NDA/BLA approvals.
  wire_*              GlobeNewswire / Business Wire / PR Newswire RSS — only items that
                      map to a US-listed ticker. (Accesswire, now ACCESS Newswire: its robots.txt
                      disallows /rss for every reader — checked 2026-11-18.) news.deals.enabled adds
                      the deal detector (mason_deals): kind + the other companies, see _wire_deal.
  polymarket / kalshi Curated macro/geopolitical markets; ≥10-pt odds move in 24h → event.
  fred / eia          Free-key macro series + weekly crude stocks → new-release events.
  econ_calendar       Forex Factory weekly JSON → calendar_events (USD high/medium).

Cadence: every source has its own poll interval; the last-poll epoch lives in
module state AND source_snapshots (source='_cadence') so a service restart doesn't
re-hit every endpoint. A pass runs at most _MAX_SOURCES_PER_PASS due sources
(most-overdue first) inside a soft _PASS_BUDGET_S, so heavy pulls spread across
passes. Nothing here raises out of scan_sources.
"""

from __future__ import annotations

import json
import re
import time
import hashlib
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger("mason.sources")
BASE = Path(__file__).parent
_CONFIG_PATH = BASE / "mason.config.json"     # tests repoint this
_CREATED_BY = "mason_sources"

_PASS_BUDGET_S = 5.0          # stop starting new sources once a pass has used this
_MAX_SOURCES_PER_PASS = 4     # network sources per pass (the Form 4 drain is extra)
_HTTP_TIMEOUT = 8             # contracts §0: ≤ 10s

_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"


# ── config (mtime-cheap, read per call) ─────────────────────────────────────────

_cfg_cache = {"mtime": None, "path": None, "cfg": {}}


def _cfg() -> dict:
    """Whole mason.config.json, re-read only when its mtime changes. {} on error."""
    try:
        p = Path(_CONFIG_PATH)
        m = p.stat().st_mtime
        if _cfg_cache["mtime"] != m or _cfg_cache["path"] != str(p):
            _cfg_cache.update(mtime=m, path=str(p),
                              cfg=json.loads(p.read_text(encoding="utf-8")) or {})
        return _cfg_cache["cfg"]
    except Exception:
        return {}


def _sources_cfg(cfg: dict) -> dict:
    return ((cfg.get("catalyst") or {}).get("sources") or {})


_SECRET_NAMES = {"fred": "FRED_API_KEY", "eia": "EIA_API_KEY", "fmp": "FMP_API_KEY",
                 "alpha_vantage": "ALPHA_VANTAGE_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "finviz": "FINVIZ_API_KEY"}


def _api_key(cfg: dict, name: str) -> str:
    """B1b: env > private secrets file > ``cfg`` (legacy), stripped exactly as before."""
    try:
        sname = _SECRET_NAMES.get(name)
        if sname:
            import mason_secrets as _S
            v = (cfg.get("apis") or {}).get(name) if isinstance(cfg, dict) else None
            if isinstance(v, dict) or v is None:
                return str(_S.get(sname, "", cfg=cfg) or "").strip()
            # legacy shape "apis": {"fred": "<key>"} (a bare string): env / file first, then as before
            got = _S.get(sname, "", cfg={})
            return str(got or v or "").strip()
        v = (cfg.get("apis") or {}).get(name) or {}
        return str(v.get("key") if isinstance(v, dict) else v or "").strip()
    except Exception:
        return ""


# ── small utils ────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_date(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc).strftime("%Y-%m-%d")


def _eid(natural_key: str) -> str:
    """Deterministic news_events id for a real-world event. Hashed (same 16-hex
    shape as mason_news_service._iid) so ids stay URL-safe; the readable natural
    key is kept in raw.event_key."""
    return hashlib.sha1(("src|" + natural_key).encode("utf-8", "ignore")).hexdigest()[:16]


def _cal_id(source: str, natural_key: str) -> str:
    """calendar_events.id = sha1(source|natural_key)[:16] (schema comment)."""
    return hashlib.sha1(f"{source}|{natural_key}".encode("utf-8", "ignore")).hexdigest()[:16]


def _money(v) -> float | None:
    """'$2,530,000,000.00' → 2530000000.0; None if unparseable."""
    try:
        s = re.sub(r"[^0-9.]", "", str(v or ""))
        return float(s) if s else None
    except Exception:
        return None


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _mdy_to_iso(s: str) -> str | None:
    """'9/30/2026' → '2026-09-30'."""
    try:
        return datetime.strptime(str(s).strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        return None


def _iso_to_epoch(s: str | None) -> float | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _clip(s: str, n: int = 220) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ── HTTP (never raises) ────────────────────────────────────────────────────────

# status / pacing hints of the most recent _http_get (a pass is single-threaded under
# _pass_lock). Lets a caller tell "304 Not Modified" and "429 slow down" from a failure
# without changing what _http_get returns.
_last_http: dict = {"status": None, "retry_after": None}


def _http_get(url, *, params=None, headers=None, timeout=_HTTP_TIMEOUT):
    """GET → response (2xx only) or None. Tests monkeypatch this one function."""
    _last_http.update(status=None, retry_after=None)
    try:
        h = {"User-Agent": _BROWSER_UA, "Accept": "*/*"}
        h.update(headers or {})
        r = requests.get(url, params=params, headers=h, timeout=min(timeout, 10))
        _last_http["status"] = r.status_code
        try:
            ra = r.headers.get("Retry-After")
            _last_http["retry_after"] = float(ra) if ra and str(ra).strip().isdigit() else None
        except Exception:
            pass
        return r if r.ok and r.content else None
    except Exception:
        return None


def _sec_get(url, timeout=_HTTP_TIMEOUT):
    """SEC GET via mason_edgar._get (descriptive UA + politeness sleep). Every caller passes at most
    10 s except the "latest filings" lists (_sec_forms_timeout, opt-in up to 45 s)."""
    try:
        import mason_edgar
        return mason_edgar._get(url, timeout=min(timeout, 45))
    except Exception:
        return None


def _sec_forms_timeout() -> float:
    """catalyst.sources.sec_forms_timeout_s: how long one SEC "latest filings" list (S-1, F-1, 424B4,
    Schedule 13D, Form 4) may take. Default 8 s, unchanged (contracts §0 keeps a sources pass short).
    2026-11-16 (feeds): since 11-13 ~13:00 SEC's list page has answered these slowly (measured 11-16
    evening: Schedule 13D 15 s, S-1 11-30 s, 8-K 0.3-1.5 s, Form 4 ~1 s), so at 8 s the 13D list has
    timed out on every poll (no activist filing stored since 11-13 17:08) and S-1/F-1 mostly too.
    Setting 30 lets them through, at the cost of a news-scan pass running up to ~30 s longer when
    one of them is due (each every 20 min). Kept within 3-45 s."""
    try:
        v = float(_sources_cfg(_cfg()).get("sec_forms_timeout_s", _HTTP_TIMEOUT) or _HTTP_TIMEOUT)
    except Exception:
        v = float(_HTTP_TIMEOUT)
    return max(3.0, min(45.0, v))


def _json(r):
    try:
        return r.json() if r is not None else None
    except Exception:
        return None


# ── snapshots + calendar + insert helpers ──────────────────────────────────────

def _snap_get(conn, source: str, key: str):
    try:
        row = conn.execute("SELECT payload_json FROM source_snapshots WHERE source=? AND key=? "
                           "AND record_deleted=0", (source, key)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None


def _snap_put(conn, source: str, key: str, payload) -> None:
    """Upsert one snapshot row (caller owns the transaction)."""
    conn.execute(
        "INSERT INTO source_snapshots (source, key, taken_at, payload_json, created_date, "
        "created_by, record_deleted) VALUES (?,?,?,?,?,?,0) "
        "ON CONFLICT(source, key) DO UPDATE SET taken_at=excluded.taken_at, "
        "payload_json=excluded.payload_json",
        (source, key, _now(), json.dumps(payload, separators=(",", ":")), _iso_now(), _CREATED_BY))


def _upsert_calendar(conn, source, natural_key, *, event_type, title, event_date,
                     ticker=None, event_time=None, status=None, url=None, details=None):
    """Upsert one calendar_events row. Returns (state, previous_row_dict|None) where
    state ∈ 'new' | 'changed' | 'same'. Caller owns the transaction."""
    cid = _cal_id(source, natural_key)
    now = _now()
    prev = conn.execute("SELECT * FROM calendar_events WHERE id=?", (cid,)).fetchone()
    prev_d = dict(prev) if prev is not None else None
    dj = json.dumps(details or {}, separators=(",", ":"), default=str)
    if prev_d is None:
        conn.execute(
            "INSERT INTO calendar_events (id, source, event_type, ticker, title, event_date, "
            "event_time, status, url, details_json, first_seen, last_updated, created_date, "
            "created_by, record_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (cid, source, event_type, ticker, _clip(title, 300), event_date, event_time,
             status, url, dj, now, now, _iso_now(), _CREATED_BY))
        return "new", None
    changed = (prev_d.get("status") != status or prev_d.get("event_date") != event_date
               or prev_d.get("event_type") != event_type or prev_d.get("ticker") != ticker)
    conn.execute(
        "UPDATE calendar_events SET event_type=?, ticker=?, title=?, event_date=?, event_time=?, "
        "status=?, url=?, details_json=?, last_updated=? WHERE id=?",
        (event_type, ticker, _clip(title, 300), event_date, event_time, status, url, dj, now, cid))
    return ("changed" if changed else "same"), prev_d


def _emit(conn, universe, holds, *, source_key, source_name, tier, kind, title, url,
          natural_key, structured, tickers=None, pub_epoch=None, desc=None) -> bool:
    """One headline into news_events per contracts §2. True if a NEW row landed."""
    try:
        import mason_news_service as NS
        item = {"title": _clip(title, 220), "url": url or "", "pub_epoch": pub_epoch}
        if desc:
            item["desc"] = _clip(desc, 300)
        extra = {"catalyst_source": source_key, "event_kind_hint": kind,
                 "event_key": natural_key, "structured": structured or {}}
        return bool(NS._insert_event(
            conn, item, source_name, tier, set(universe or ()), set(holds or ()),
            explicit_tickers=(list(tickers) if tickers else None), dedup_against=None,
            event_id=_eid(natural_key), noise_filter=False, extra_raw=extra))
    except Exception as e:
        log.debug(f"[sources] emit failed ({source_key}): {e}")
        return False


# ── company name → ticker (precision-guarded) ──────────────────────────────────
# Shared by clinical_trials / fda / pr_wires. Built from mason_edgar's disk-cached
# SEC company_tickers.json (.edgar_tickers_cache.json). Curated aliases win; an
# alias mapping to "" is an explicit BLOCK (private / not SEC-registered / a
# different company that shares a famous name — e.g. Merck KGaA is not MRK).

_ALIASES = [
    (r"\beli lilly\b|^lilly\b", "LLY"),
    (r"\bmerck kgaa\b|\bemd serono\b", ""),
    (r"\bmerck sharp\b|\bmsd\b|^merck\b", "MRK"),
    (r"\bjanssen\b|\bjohnson (and|&) johnson\b|^actelion\b", "JNJ"),
    (r"^loxo oncology\b", "LLY"), (r"^teva\b", "TEVA"),
    (r"\bglaxosmithkline\b|^gsk\b|\bviiv\b", "GSK"),
    (r"\bhoffmann.la roche\b|^roche\b|\bgenentech\b|\bchugai\b", ""),
    (r"\bboehringer\b|\bbayer\b|\bdaiichi\b|\bastellas\b|\beisai\b|\bsandoz\b|\bucb\b|"
     r"\bservier\b|\bipsen\b|\bchiesi\b|\bmenarini\b|\bferring\b|\bgrifols\b|\botsuka\b", ""),
    (r"^novartis\b", "NVS"), (r"^sanofi\b|\bgenzyme\b", "SNY"), (r"^astrazeneca\b|\balexion\b", "AZN"),
    (r"\bbristol.myers\b|\bcelgene\b", "BMY"), (r"^abbvie\b|\ballergan\b", "ABBV"),
    (r"^pfizer\b|\bseagen\b", "PFE"), (r"^novo nordisk\b", "NVO"), (r"^takeda\b", "TAK"),
    (r"^amgen\b", "AMGN"), (r"^gilead\b|\bkite pharma\b", "GILD"), (r"^regeneron\b", "REGN"),
    (r"^vertex pharm", "VRTX"), (r"^biogen\b", "BIIB"), (r"^moderna", "MRNA"),
    (r"^biontech\b", "BNTX"), (r"^beigene\b|^beone\b", "ONC"), (r"^argenx\b", "ARGX"),
]

_LEGAL = set("inc incorporated corp corporation co company companies ltd limited plc ag sa nv "
             "se llc lp llp holdings holding group the de new trust spa ab asa oyj kgaa gmbh "
             "bv pte pty sarl srl ulc".split())
_DESCRIPTORS = set("pharmaceuticals pharmaceutical pharma pharms pharm therapeutics therapeutic "
                   "therap biosciences bioscience biotherapeutics biopharma biopharmaceuticals "
                   "biopharmaceutical bio biotech biotechnology biologics oncology medical "
                   "sciences science labs laboratories laboratory technologies technology tech "
                   "health healthcare international intl usa us global research development "
                   "and medicines".split())
_COMMON_CORES = set("american national general united first global international advanced "
                    "applied new pacific atlantic summit alpha beta gamma delta omega nova apex "
                    "prime premier precision genesis vision frontier pioneer liberty eagle "
                    "universal standard allied central digital energy capital".split())

_name_idx = {"ts": 0.0, "l1": {}, "l2": {}, "tickers": {}}
_NAME_IDX_TTL = 3600


def _norm_tokens(name: str) -> list[str]:
    s = str(name or "").lower().replace("&", " and ").replace(".", "")   # "N.V." → "nv"
    s = re.sub(r"/[a-z]{2}/", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return [t for t in s.split() if t and t not in _LEGAL]


def _core_tokens(toks: list[str]) -> list[str]:
    out = list(toks)
    while out and out[-1] in _DESCRIPTORS:
        out.pop()
    return out


def _load_name_index() -> dict:
    now = _now()
    if _name_idx["l1"] and now - _name_idx["ts"] < _NAME_IDX_TTL:
        return _name_idx
    try:
        import mason_edgar
        tmap = mason_edgar._load_tickers_map()     # {TICKER: (cik, title)}, ranked order
    except Exception:
        tmap = {}
    primary: dict = {}                              # cik → first (primary) ticker
    titles: dict = {}
    for tkr, (cik, title) in tmap.items():
        if cik not in primary:
            primary[cik], titles[cik] = tkr, title
    l1: dict = {}
    l2: dict = {}
    for cik, title in titles.items():
        toks = _norm_tokens(title)
        if not toks:
            continue
        l1.setdefault(" ".join(toks), set()).add(cik)
        core = _core_tokens(toks)
        if core:
            l2.setdefault(" ".join(core), set()).add(cik)
    if l1:
        _name_idx.update(ts=now, l1=l1, l2=l2, tickers=primary,
                         all=set(tmap.keys()))
    return _name_idx


def known_ticker(t: str) -> bool:
    """True if `t` is an SEC-registered ticker (company_tickers.json)."""
    try:
        return str(t or "").upper() in (_load_name_index().get("all") or set())
    except Exception:
        return False


def company_to_ticker(name: str) -> str | None:
    """Company / sponsor name → primary ticker, or None. Precision over recall:
    curated alias → exact normalized-name match → unique descriptor-stripped core
    (≥5 chars, not a generic word). Ambiguous → None."""
    try:
        raw = " ".join(str(name or "").split())
        if not raw:
            return None
        low = raw.lower()
        parent = re.search(r",\s*an? (.+?) company\b", low)     # "Karuna …, a Bristol Myers Squibb company"
        if parent:
            return company_to_ticker(parent.group(1))
        for pat, tkr in _ALIASES:
            if re.search(pat, low):
                return tkr or None
        idx = _load_name_index()
        toks = _norm_tokens(raw)
        if not toks:
            return None
        hits = idx["l1"].get(" ".join(toks))
        if hits and len(hits) == 1:
            return idx["tickers"].get(next(iter(hits)))
        # level 2 only when the QUERY carried a descriptor we stripped ("Novartis
        # Pharmaceuticals", truncated "PRIOVANT THERAP"); a bare "Crescent" must not
        # match "Crescent Biopharma".
        core = _core_tokens(toks)
        key = " ".join(core)
        if (core != toks and len(key.replace(" ", "")) >= 5
                and not (len(core) == 1 and core[0] in _COMMON_CORES)):
            hits = idx["l2"].get(key)
            if hits and len(hits) == 1:
                return idx["tickers"].get(next(iter(hits)))
        return None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. App Store charts
# ══════════════════════════════════════════════════════════════════════════════

_APPLE_CHARTS = {
    # chart key → (url, parser, display)
    "top-free": ("https://rss.marketingtools.apple.com/api/v2/us/apps/top-free/100/apps.json",
                 "v2", "top free"),
    # the v2 marketing-tools API dropped top-grossing (404); the legacy iTunes
    # RSS JSON still serves it.
    "top-grossing": ("https://itunes.apple.com/us/rss/topgrossingapplications/limit=100/json",
                     "legacy", "top grossing"),
}

# publisher (App Store artistName, lowercased) → ticker. "" = private competitive
# signal worth surfacing without a ticker; publishers not listed are ignored.
_PUBLISHERS = [
    (r"^meta platforms|^instagram|^whatsapp|^facebook", "META"),
    (r"^google\b|^alphabet|^youtube", "GOOGL"),
    (r"^microsoft|^linkedin", "MSFT"),
    (r"^amzn mobile|^amazon|^audible", "AMZN"),
    (r"^apple\b", "AAPL"), (r"^netflix", "NFLX"), (r"^uber technologies|^uber\b", "UBER"),
    (r"^doordash", "DASH"), (r"^robinhood", "HOOD"), (r"^paypal|^venmo", "PYPL"),
    (r"^block,? inc|^square,? inc|^cash app", "XYZ"), (r"^snap,? inc", "SNAP"),
    (r"^pinterest", "PINS"), (r"^spotify", "SPOT"), (r"^roblox", "RBLX"),
    (r"^duolingo", "DUOL"), (r"^airbnb", "ABNB"), (r"^coinbase", "COIN"),
    (r"^sofi\b|^social finance", "SOFI"), (r"^reddit", "RDDT"),
    (r"^disney|^espn|^hulu", "DIS"), (r"^walmart", "WMT"), (r"^temu\b|^pdd\b", "PDD"),
    (r"^shopify", "SHOP"), (r"^capital one", "COF"), (r"^ticketmaster|^live nation", "LYV"),
    (r"^life360", "LIF"), (r"^chime\b", "CHYM"), (r"^lyft", "LYFT"), (r"^ebay", "EBAY"),
    (r"^etsy", "ETSY"), (r"^intuit", "INTU"), (r"^zoom\b", "ZM"), (r"^expedia", "EXPE"),
    (r"^booking\.com|^priceline", "BKNG"), (r"^starbucks", "SBUX"), (r"^mcdonald", "MCD"),
    (r"^target\b", "TGT"), (r"^costco", "COST"), (r"^home depot", "HD"),
    (r"^maplebear|^instacart", "CART"), (r"^match group|^tinder|^hinge", "MTCH"),
    (r"^bumble", "BMBL"), (r"^grindr", "GRND"), (r"^draftkings", "DKNG"),
    (r"^fanduel|^flutter", "FLUT"), (r"^take.two|^rockstar games|^zynga", "TTWO"),
    (r"^applovin", "APP"), (r"^nbcuniversal|^peacock|^comcast", "CMCSA"),
    (r"^warner ?bros|^wbd\b|^warnermedia", "WBD"), (r"^paramount", "PSKY"),
    (r"^hims", "HIMS"), (r"^affirm", "AFRM"), (r"^klarna", "KLAR"), (r"^tesla", "TSLA"),
    (r"^rivian", "RIVN"), (r"^verizon", "VZ"), (r"^at&t|^at and t", "T"),
    (r"^t-mobile", "TMUS"), (r"^jpmorgan|^chase\b", "JPM"), (r"^bank of america", "BAC"),
    (r"^wells fargo", "WFC"), (r"^adobe", "ADBE"), (r"^nike", "NKE"), (r"^zillow", "Z"),
    (r"^carvana", "CVNA"), (r"^opendoor", "OPEN"), (r"^nextdoor", "NXDR"),
    (r"^wayfair", "W"), (r"^webull", "BULL"), (r"^etoro", "ETOR"), (r"^figma", "FIG"),
    (r"^sirius ?xm", "SIRI"), (r"^fox\b", "FOXA"), (r"^roku", "ROKU"),
    (r"^circle internet", "CRCL"), (r"^gemini", "GEMI"), (r"^goodrx", "GDRX"),
    # private but a competitive signal (no ticker; triage maps the peer read-through)
    (r"^openai", ""), (r"^anthropic", ""), (r"^x corp|^xai\b", ""), (r"^perplexity", ""),
]


def publisher_ticker(artist: str):
    """(matched, ticker|'') — matched=False for publishers we don't track."""
    low = str(artist or "").strip().lower()
    for pat, tkr in _PUBLISHERS:
        if re.search(pat, low):
            return True, tkr
    return False, ""


def parse_apple_chart(payload, kind: str) -> list[dict]:
    """Chart JSON → [{rank, app_id, name, artist, url}] in rank order. [] on garbage."""
    out = []
    try:
        if kind == "v2":
            rows = ((payload or {}).get("feed") or {}).get("results") or []
            for i, r in enumerate(rows):
                out.append({"rank": i + 1, "app_id": str(r.get("id") or ""),
                            "name": str(r.get("name") or ""),
                            "artist": str(r.get("artistName") or ""), "url": r.get("url") or ""})
        else:
            rows = ((payload or {}).get("feed") or {}).get("entry") or []
            if isinstance(rows, dict):
                rows = [rows]
            for i, e in enumerate(rows):
                aid = (((e.get("id") or {}).get("attributes") or {}).get("im:id")) or ""
                link = e.get("link")
                href = ""
                if isinstance(link, list) and link:
                    href = ((link[0] or {}).get("attributes") or {}).get("href", "")
                elif isinstance(link, dict):
                    href = (link.get("attributes") or {}).get("href", "")
                out.append({"rank": i + 1, "app_id": str(aid),
                            "name": str((e.get("im:name") or {}).get("label") or ""),
                            "artist": str((e.get("im:artist") or {}).get("label") or ""),
                            "url": href})
    except Exception:
        return [r for r in out if r.get("app_id")]
    return [r for r in out if r.get("app_id")]


def diff_app_chart(prev_ranks: dict | None, rows: list[dict]) -> list[dict]:
    """Triggers vs the previous snapshot {app_id: rank}. Returns
    [{row, trigger, prev_rank, ticker}] for tracked publishers only.
    trigger ∈ rank1 | top10 | jump. On the very first snapshot (prev None) only a
    current #1 is reported, so a fresh install doesn't dump the whole chart."""
    out = []
    for r in rows:
        matched, tkr = publisher_ticker(r["artist"])
        if not matched:
            continue
        rank = r["rank"]
        prev = (prev_ranks or {}).get(r["app_id"]) if prev_ranks is not None else None
        trig = None
        if rank == 1 and (prev_ranks is None or prev != 1):
            trig = "rank1"
        elif prev_ranks is not None and rank <= 10 and (prev is None or prev > 10):
            trig = "top10"
        elif prev_ranks is not None and tkr and (
                (prev is not None and prev - rank >= 20) or (prev is None and rank <= 50)):
            trig = "jump"                       # private publishers: rank1/top10 only
        if trig:
            out.append({"row": r, "trigger": trig, "prev_rank": prev, "ticker": tkr})
    return out


def _app_headline(r, trig, prev, tkr, chart_disp) -> str:
    who = f"{r['name']} ({tkr})" if tkr else f"{r['name']} ({r['artist']}, private)"
    frm = f", up from #{prev}" if prev else (", new to the top 100" if trig != "rank1" else "")
    if trig == "rank1":
        what = f"hits #1 on Apple App Store {chart_disp} chart (US){frm}"
    elif trig == "top10":
        what = f"enters the Apple App Store {chart_disp} top 10 at #{r['rank']} (US){frm}"
    else:
        what = f"jumps to #{r['rank']} on Apple App Store {chart_disp} chart (US){frm}"
    return f"{who} {what}"


def _src_app_charts(conn, universe, holds, cfg) -> int | None:
    new, ok_any = 0, False
    today = _utc_date()
    for chart, (url, kind, disp) in _APPLE_CHARTS.items():
        rows = parse_apple_chart(_json(_http_get(url, headers={"Accept": "application/json"})), kind)
        if len(rows) < 10:                      # fetch failed / garbage → keep old snapshot
            continue
        ok_any = True
        snap = _snap_get(conn, "app_charts", f"apple:us:{chart}")
        prev = (snap or {}).get("ranks") if snap else None
        with conn:
            for t in diff_app_chart(prev, rows):
                r = t["row"]
                nk = f"appchart:apple:{chart}:{r['app_id']}:{t['trigger']}:{today}"
                st = {"store": "apple", "country": "us", "chart": chart, "app_id": r["app_id"],
                      "app_name": r["name"], "publisher": r["artist"], "rank": r["rank"],
                      "prev_rank": t["prev_rank"], "trigger": t["trigger"],
                      "ticker": t["ticker"] or None, "private": not t["ticker"]}
                if _emit(conn, universe, holds, source_key="app_charts",
                         source_name="Apple App Store", tier=2, kind="app_rank",
                         title=_app_headline(r, t["trigger"], t["prev_rank"], t["ticker"], disp),
                         url=r["url"], natural_key=nk, structured=st,
                         tickers=[t["ticker"]] if t["ticker"] else None, pub_epoch=_now()):
                    new += 1
            _snap_put(conn, "app_charts", f"apple:us:{chart}",
                      {"taken_at": _now(), "ranks": {r["app_id"]: r["rank"] for r in rows},
                       "names": {r["app_id"]: r["name"] for r in rows[:25]}})
    return new if ok_any else None


# ══════════════════════════════════════════════════════════════════════════════
# 2. SEC EDGAR forms (IPO / activist / insider)
# ══════════════════════════════════════════════════════════════════════════════

_EDGAR_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={t}"
                  "&count={n}&output=atom{extra}")
_EDGAR_TITLE_RE = re.compile(r"^\s*(.+?)\s+-\s+(.*?)\s+\((\d{10})\)\s*\(([^)]+)\)\s*$")
_F4_BUY_MIN = 250_000          # single-filing open-market purchase that emits alone
_F4_CLUSTER_N = 3              # distinct insiders buying within...
_F4_CLUSTER_DAYS = 7
_F4_SELL_HUGE = 50_000_000     # sells only when enormous AND not a 10b5-1 plan
_F4_DRAIN_PER_PASS = 8         # Form 4 .txt fetches per pass
_F4_QUEUE_MAX = 400
_F4_SEEN_MAX = 3000


def parse_edgar_current(content: bytes) -> list[dict]:
    """EDGAR getcurrent Atom → [{form, company, cik, role, accession, filed, url,
    pub_epoch}]. role is Filer | Subject | Filed by | Issuer | Reporting."""
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(content)
    except Exception:
        return out
    loc = lambda tag: tag.rsplit("}", 1)[-1].lower()
    for el in root.iter():
        if loc(el.tag) != "entry":
            continue
        try:
            title = url = summary = updated = ""
            for ch in el:
                t = loc(ch.tag)
                if t == "title":
                    title = (ch.text or "").strip()
                elif t == "link":
                    url = ch.get("href") or url
                elif t == "summary":
                    summary = ch.text or ""
                elif t == "updated":
                    updated = (ch.text or "").strip()
            m = _EDGAR_TITLE_RE.match(title)
            if not m:
                continue
            import html as _h
            text = re.sub(r"<[^>]+>", " ", _h.unescape(summary))
            acc = re.search(r"AccNo:\s*([\d-]{20})", text)
            filed = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", text)
            out.append({"form": m.group(1).strip(), "company": _h.unescape(m.group(2)).strip(),
                        "cik": int(m.group(3)), "role": m.group(4).strip(),
                        "accession": acc.group(1) if acc else "",
                        "filed": filed.group(1) if filed else "", "url": url,
                        "pub_epoch": _iso_to_epoch(updated)})
        except Exception:
            continue
    return out


def _cikmap() -> dict:
    try:
        import mason_news_service as NS
        return NS._cik_to_tickers() or {}
    except Exception:
        return {}


def _edgar_watch_universe(universe) -> set:
    try:
        import mason_news_service as NS
        return set(universe or ()) | NS._edgar_watch()
    except Exception:
        return set(universe or ())


def _edgar_feed(form_type: str, n: int = 100, extra: str = ""):
    r = _sec_get(_EDGAR_CURRENT.format(t=form_type.replace(" ", "+"), n=n, extra=extra),
                 timeout=_sec_forms_timeout())
    if r is None:
        return None
    return parse_edgar_current(r.content)


def _src_edgar_ipo(form_type: str):
    """S-1 / F-1 (IPO filing) and 424B4 (final prospectus = priced) poller."""
    def run(conn, universe, holds, cfg) -> int | None:
        entries = _edgar_feed(form_type, 100)
        if entries is None:
            return None
        cikmap = _cikmap()
        watch = _edgar_watch_universe(universe)
        # S-1/A amendments emit only for names already on the NASDAQ IPO calendar
        cal_names = set()
        try:
            for (t,) in conn.execute("SELECT title FROM calendar_events WHERE source='NASDAQ IPO' "
                                     "AND record_deleted=0"):
                cal_names.add(" ".join(_norm_tokens(str(t).split(" (")[0].split(": ", 1)[-1])))
        except Exception:
            pass
        new = 0
        with conn:
            for e in entries:
                if e["role"] != "Filer":
                    continue
                form = e["form"].upper()
                base = form.split("/")[0]
                if base not in ("S-1", "F-1", "424B4"):
                    continue                              # S-1MEF, F-10, etc.
                tickers = cikmap.get(e["cik"], [])
                amend = form.endswith("/A")
                name_key = " ".join(_norm_tokens(e["company"]))
                if base == "424B4":
                    kind = "ipo_pricing" if not tickers else "other"
                    if tickers and not (set(tickers) & watch):
                        continue                          # follow-ons: only names we follow
                    what = ("prices IPO — final prospectus (424B4)" if not tickers
                            else "prices follow-on offering — final prospectus (424B4)")
                else:
                    if tickers:
                        if not (set(tickers) & watch):
                            continue                      # resale/shelf S-1 of a listed co.
                        kind, what = "other", f"files {form} registration statement (possible offering)"
                    elif amend:
                        if name_key not in cal_names:
                            continue
                        kind, what = "ipo_filing", f"files {form} IPO amendment"
                    else:
                        kind, what = "ipo_filing", f"files {form} for IPO"
                tk = tickers[:1] if tickers else None
                who = f"{e['company']} ({tickers[0]})" if tickers else e["company"]
                nk = f"edgar:{form}:{e['accession'] or e['url']}"
                st = {"form": form, "company": e["company"], "cik": e["cik"],
                      "accession": e["accession"], "filed": e["filed"],
                      "offering": ("ipo" if not tickers else "follow_on")}
                if _emit(conn, universe, holds, source_key="edgar_forms", source_name="SEC EDGAR",
                         tier=1, kind=kind, title=f"{who} {what}", url=e["url"],
                         natural_key=nk, structured=st, tickers=tk, pub_epoch=e["pub_epoch"]):
                    new += 1
        return new
    return run


def _src_edgar_13d(conn, universe, holds, cfg) -> int | None:
    entries = _edgar_feed("SCHEDULE 13D", 100)
    if entries is None:
        return None
    cikmap = _cikmap()
    watch = _edgar_watch_universe(universe)
    filers: dict = {}
    for e in entries:
        if e["role"].lower().startswith("filed by"):
            filers.setdefault(e["accession"], e["company"])
    new = 0
    with conn:
        for e in entries:
            if e["role"] != "Subject":
                continue
            tickers = cikmap.get(e["cik"], [])
            if not tickers:
                continue                                  # no listed subject → not tradeable
            amend = e["form"].upper().endswith("/A")
            if amend and not (set(tickers) & watch):
                continue                                  # amendments: followed names only
            filer = filers.get(e["accession"], "an investor")
            what = "amends Schedule 13D (5%+ holder, may seek influence)" if amend else \
                "files Schedule 13D — 5%+ stake with intent to influence"
            title = f"{tickers[0]}: {filer} {what} in {e['company']}"
            nk = f"edgar:13d:{e['accession'] or e['url']}"
            st = {"form": e["form"], "subject": e["company"], "subject_cik": e["cik"],
                  "filer": filer, "accession": e["accession"], "filed": e["filed"]}
            if _emit(conn, universe, holds, source_key="edgar_forms", source_name="SEC EDGAR",
                     tier=1, kind="activist", title=title, url=e["url"], natural_key=nk,
                     structured=st, tickers=tickers[:1], pub_epoch=e["pub_epoch"]):
                new += 1
    return new


# ── Form 4 ──

def _xml_text(el, path: str) -> str:
    try:
        node = el.find(path)
        return (node.text or "").strip() if node is not None and node.text else ""
    except Exception:
        return ""


def parse_form4(text: str) -> dict | None:
    """Full-submission .txt (or bare XML) of a Form 4 → {issuer_cik, symbol, owners:
    [{name, role}], aff10b5one, tx: [{code, date, shares, price, value, ad}]}.
    None if no ownershipDocument is found."""
    import xml.etree.ElementTree as ET
    try:
        m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text or "", re.S)
        if not m:
            return None
        root = ET.fromstring(m.group(0))
    except Exception:
        return None
    owners = []
    for ro in root.findall("reportingOwner"):
        name = _xml_text(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if _xml_text(rel, "isDirector") in ("1", "true"):
                roles.append("Director")
            if _xml_text(rel, "isOfficer") in ("1", "true"):
                roles.append(_xml_text(rel, "officerTitle") or "Officer")
            if _xml_text(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
        owners.append({"name": name, "role": ", ".join(roles) or "insider"})
    tx = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        try:
            code = _xml_text(t, "transactionCoding/transactionCode")
            sh = float(_xml_text(t, "transactionAmounts/transactionShares/value") or 0)
            px = float(_xml_text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
            tx.append({"code": code, "date": _xml_text(t, "transactionDate/value"),
                       "shares": sh, "price": px, "value": sh * px,
                       "ad": _xml_text(t, "transactionAmounts/transactionAcquiredDisposedCode/value")})
        except Exception:
            continue
    try:
        icik = int(_xml_text(root, "issuer/issuerCik") or 0)
    except Exception:
        icik = 0
    return {"issuer_cik": icik, "issuer": _xml_text(root, "issuer/issuerName"),
            "symbol": _xml_text(root, "issuer/issuerTradingSymbol").upper(),
            "owners": owners, "aff10b5one": _xml_text(root, "aff10b5One") in ("1", "true"),
            "doc_type": _xml_text(root, "documentType"), "tx": tx}


def _f4_txt_url(entry: dict) -> str:
    """Issuer-entry index url → full-submission .txt url (one request, XML inline)."""
    base = entry["url"].rsplit("/", 1)[0]
    return f"{base}/{entry['accession']}.txt"


def _src_edgar_form4(conn, universe, holds, cfg) -> int | None:
    """Poll the Form 4 feed (owner=only → Form 4s only) and QUEUE new issuer
    filings that map to a listed ticker. The XML is fetched by the drain."""
    entries = _edgar_feed("4", 100, "&owner=only")
    if entries is None:
        return None
    cikmap = _cikmap()
    watch = _edgar_watch_universe(universe)
    st = _snap_get(conn, "edgar_form4", "state") or {}
    seen = set(st.get("seen") or [])
    queue = list(st.get("queue") or [])
    queued = {q["acc"] for q in queue}
    for e in entries:
        if e["form"] != "4" or e["role"] != "Issuer" or not e["accession"]:
            continue
        if e["accession"] in seen or e["accession"] in queued:
            continue
        tickers = cikmap.get(e["cik"], [])
        if not tickers:
            continue
        queue.append({"acc": e["accession"], "cik": e["cik"], "ticker": tickers[0],
                      "url": e["url"], "txt": _f4_txt_url(e), "pub": e["pub_epoch"],
                      "pri": 0 if set(tickers) & watch else 1})
        queued.add(e["accession"])
    # followed names first, then newest; bounded
    queue.sort(key=lambda q: (q.get("pri", 1), -(q.get("pub") or 0)))
    st["queue"] = queue[:_F4_QUEUE_MAX]
    st["seen"] = list(seen)[-_F4_SEEN_MAX:]
    with conn:
        _snap_put(conn, "edgar_form4", "state", st)
    return 0


def _f4_queue_len(conn) -> int:
    try:
        return len((_snap_get(conn, "edgar_form4", "state") or {}).get("queue") or [])
    except Exception:
        return 0


def _src_edgar_form4_drain(conn, universe, holds, cfg) -> int | None:
    st = _snap_get(conn, "edgar_form4", "state") or {}
    queue = list(st.get("queue") or [])
    if not queue:
        return 0
    seen = list(st.get("seen") or [])
    new = 0
    t0 = time.monotonic()
    batch, rest = queue[:_F4_DRAIN_PER_PASS], queue[_F4_DRAIN_PER_PASS:]
    # the SEC requests first, with no write transaction open (a held write lock made
    # other writers — the scorer thread — time out); then one short write
    fetched = []
    for i, q in enumerate(batch):
        if time.monotonic() - t0 > 4.0:
            rest = batch[i:] + rest                   # out of time: keep for next pass
            break
        r = _sec_get(q["txt"])
        fetched.append((q, r.text if r is not None else ""))
    with conn:
        for q, text in fetched:
            seen.append(q["acc"])
            doc = parse_form4(text)
            if not doc:
                continue
            new += _f4_evaluate(conn, universe, holds, q, doc)
        st["queue"], st["seen"] = rest, seen[-_F4_SEEN_MAX:]
        _snap_put(conn, "edgar_form4", "state", st)
    return new


def _f4_evaluate(conn, universe, holds, q, doc) -> int:
    """Emit for one parsed Form 4: a ≥$250k open-market buy, a new ≥3-insider buy
    cluster (7d), or an enormous non-10b5-1 sale. Returns events written."""
    tkr = q["ticker"]
    owner = (doc["owners"] or [{"name": "insider", "role": "insider"}])[0]
    buys = [t for t in doc["tx"] if t["code"] == "P" and t["ad"] in ("A", "")]
    sells = [t for t in doc["tx"] if t["code"] == "S"]
    new = 0
    if buys:
        bval = sum(t["value"] for t in buys)
        bsh = sum(t["shares"] for t in buys)
        avg = (bval / bsh) if bsh else 0
        bdate = max(t["date"] for t in buys) or _utc_date()
        # cluster bookkeeping (every buy, any size)
        key = f"buys:{tkr}"
        cs = _snap_get(conn, "edgar_form4", key) or {"buys": []}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_F4_CLUSTER_DAYS)).strftime("%Y-%m-%d")
        cs["buys"] = [b for b in cs.get("buys", []) if b.get("date", "") >= cutoff and b.get("acc") != q["acc"]]
        cs["buys"].append({"owner": owner["name"], "date": bdate, "value": round(bval, 2), "acc": q["acc"]})
        st = {"form": "4", "accession": q["acc"], "issuer": doc["issuer"], "owner": owner["name"],
              "role": owner["role"], "shares": bsh, "avg_price": round(avg, 4),
              "value": round(bval, 2), "tx_date": bdate, "code": "P"}
        if bval >= _F4_BUY_MIN:
            title = (f"{tkr} insider buy: {owner['name']} ({owner['role']}) bought "
                     f"{bsh:,.0f} sh @ ${avg:,.2f} ({_fmt_money(bval)}) on the open market — Form 4")
            if _emit(conn, universe, holds, source_key="edgar_forms", source_name="SEC Form 4",
                     tier=1, kind="insider_buy", title=title, url=q["url"],
                     natural_key=f"edgar:form4:{q['acc']}", structured=st, tickers=[tkr],
                     pub_epoch=q.get("pub")):
                new += 1
        owners = {b["owner"] for b in cs["buys"]}
        last_cluster = cs.get("cluster_date") or ""
        if len(owners) >= _F4_CLUSTER_N and last_cluster < cutoff:
            tot = sum(b["value"] for b in cs["buys"])
            title = (f"{tkr} insider buying cluster: {len(owners)} insiders bought "
                     f"{_fmt_money(tot)} on the open market in {_F4_CLUSTER_DAYS} days — Form 4")
            if _emit(conn, universe, holds, source_key="edgar_forms", source_name="SEC Form 4",
                     tier=1, kind="insider_buy", title=title, url=q["url"],
                     natural_key=f"edgar:form4cluster:{tkr}:{bdate}",
                     structured={"cluster": True, "insiders": sorted(owners),
                                 "total_value": round(tot, 2), "window_days": _F4_CLUSTER_DAYS,
                                 "buys": cs["buys"][-10:]},
                     tickers=[tkr], pub_epoch=q.get("pub")):
                new += 1
            cs["cluster_date"] = bdate
        _snap_put(conn, "edgar_form4", key, cs)
    if sells and not doc.get("aff10b5one"):
        sval = sum(t["value"] for t in sells)
        if sval >= _F4_SELL_HUGE:
            ssh = sum(t["shares"] for t in sells)
            title = (f"{tkr} insider sale: {owner['name']} ({owner['role']}) sold "
                     f"{ssh:,.0f} sh ({_fmt_money(sval)}), not under a 10b5-1 plan — Form 4")
            if _emit(conn, universe, holds, source_key="edgar_forms", source_name="SEC Form 4",
                     tier=1, kind="insider_sell", title=title, url=q["url"],
                     natural_key=f"edgar:form4sell:{q['acc']}",
                     structured={"form": "4", "accession": q["acc"], "owner": owner["name"],
                                 "role": owner["role"], "shares": ssh, "value": round(sval, 2),
                                 "code": "S"},
                     tickers=[tkr], pub_epoch=q.get("pub")):
                new += 1
    return new


# ══════════════════════════════════════════════════════════════════════════════
# 3. NASDAQ IPO calendar
# ══════════════════════════════════════════════════════════════════════════════

_NASDAQ_IPO = "https://api.nasdaq.com/api/ipo/calendar"
_NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                   "Referer": "https://www.nasdaq.com/", "Accept-Language": "en-US,en;q=0.9"}
_IPO_BIG = 500_000_000
_IPO_PRICED_RECENT_DAYS = 3


def parse_nasdaq_ipo(payload) -> list[dict]:
    """NASDAQ calendar JSON → [{deal_id, status: priced|expected|filed|withdrawn,
    symbol, company, exchange, price, shares, value, date}]."""
    out = []
    try:
        d = (payload or {}).get("data") or {}
        sections = [("priced", (d.get("priced") or {}).get("rows"), "pricedDate"),
                    ("expected", ((d.get("upcoming") or {}).get("upcomingTable") or {}).get("rows"),
                     "expectedPriceDate"),
                    ("filed", (d.get("filed") or {}).get("rows"), "filedDate"),
                    ("withdrawn", (d.get("withdrawn") or {}).get("rows"), "withdrawDate")]
        for status, rows, dcol in sections:
            for r in rows or []:
                if not isinstance(r, dict) or not r.get("dealID"):
                    continue
                out.append({"deal_id": str(r["dealID"]), "status": status,
                            "symbol": (r.get("proposedTickerSymbol") or "").strip().upper() or None,
                            "company": (r.get("companyName") or "").strip(),
                            "exchange": r.get("proposedExchange"),
                            "price": r.get("proposedSharePrice"),
                            "shares": r.get("sharesOffered"),
                            "value": _money(r.get("dollarValueOfSharesOffered")),
                            "date": _mdy_to_iso(r.get(dcol))})
    except Exception:
        return out
    return out


def _src_ipo_calendar(conn, universe, holds, cfg) -> int | None:
    now = datetime.now(timezone.utc)
    months = [now.strftime("%Y-%m")]
    if now.day >= 20:
        months.append((now.replace(day=1) + timedelta(days=32)).strftime("%Y-%m"))
    rows, ok = [], False
    for m in months:
        p = _json(_http_get(_NASDAQ_IPO, params={"date": m}, headers=_NASDAQ_HEADERS))
        if p and isinstance(p.get("data"), dict):
            ok = True
            rows += parse_nasdaq_ipo(p)
    if not ok:
        return None
    recent_cut = (now - timedelta(days=_IPO_PRICED_RECENT_DAYS)).strftime("%Y-%m-%d")
    new = 0
    with conn:
        for r in rows:
            if r["status"] == "withdrawn":
                etype, cstatus = "ipo", "withdrawn"
            elif r["status"] == "priced":
                etype, cstatus = "ipo_pricing", "priced"
            elif r["status"] == "filed":
                if (r["value"] or 0) < 100_000_000:
                    continue                          # small filings: EDGAR covers them
                etype, cstatus = "ipo", "filed"
            else:
                etype, cstatus = "ipo", "expected"
            sym = r["symbol"]
            ctitle = f"IPO: {r['company']}" + (f" ({sym})" if sym else "")
            state, prev = _upsert_calendar(
                conn, "NASDAQ IPO", r["deal_id"], event_type=etype, title=ctitle,
                event_date=r["date"], ticker=sym, status=cstatus,
                url=f"https://www.nasdaq.com/market-activity/ipos",
                details={k: r[k] for k in ("deal_id", "exchange", "price", "shares", "value", "company")})
            prev_status = (prev or {}).get("status")
            st = {**{k: r[k] for k in ("deal_id", "company", "exchange", "price", "shares", "value", "date")},
                  "symbol": sym, "status": cstatus, "prev_status": prev_status}
            spac = bool(re.search(r"\bacquisition corp|\bspac\b|\bcapital corp\b", r["company"].lower()))
            if cstatus == "priced" and prev_status != "priced" and (r["date"] or "") >= recent_cut:
                if spac and (r["value"] or 0) < 250_000_000:
                    continue
                title = (f"IPO priced: {r['company']}" + (f" ({sym})" if sym else "") +
                         f" prices {r['shares'] or '?'} shares at ${r['price'] or '?'}, raising "
                         f"{_fmt_money(r['value'])} on {r['exchange'] or 'US exchange'}")
                if _emit(conn, universe, holds, source_key="ipo_calendar", source_name="NASDAQ IPO",
                         tier=2, kind="ipo_pricing", title=title,
                         url="https://www.nasdaq.com/market-activity/ipos",
                         natural_key=f"ipo:priced:{r['deal_id']}", structured=st,
                         tickers=[sym] if sym else None, pub_epoch=_iso_to_epoch(r["date"])):
                    new += 1
            elif cstatus == "expected" and state == "new" and (r["value"] or 0) > _IPO_BIG:
                title = (f"Upcoming IPO: {r['company']}" + (f" ({sym})" if sym else "") +
                         f" expected to price {r['date'] or 'soon'} at ${r['price'] or '?'}, "
                         f"~{_fmt_money(r['value'])} raise on {r['exchange'] or 'US exchange'}")
                if _emit(conn, universe, holds, source_key="ipo_calendar", source_name="NASDAQ IPO",
                         tier=2, kind="ipo_calendar", title=title,
                         url="https://www.nasdaq.com/market-activity/ipos",
                         natural_key=f"ipo:scheduled:{r['deal_id']}", structured=st,
                         tickers=[sym] if sym else None, pub_epoch=_now()):
                    new += 1
    return new


# ══════════════════════════════════════════════════════════════════════════════
# 4. ClinicalTrials.gov
# ══════════════════════════════════════════════════════════════════════════════

_CTGOV = "https://clinicaltrials.gov/api/v2/studies"
_CTGOV_FIELDS = ("NCTId,BriefTitle,OverallStatus,Phase,LeadSponsorName,LeadSponsorClass,"
                 "PrimaryCompletionDate,PrimaryCompletionDateType,Condition,HasResults,"
                 "LastUpdatePostDate,EnrollmentCount")
_CT_WINDOW_DAYS = 60
_CT_MAX_PAGES = 3
_CT_PH2_MIN_ENROLL = 150       # "pivotal" Phase 2 proxy
_CT_FLIP_TO = {"COMPLETED", "TERMINATED", "SUSPENDED", "WITHDRAWN", "ACTIVE_NOT_RECRUITING"}


def parse_ctgov(payload) -> list[dict]:
    out = []
    for s in ((payload or {}).get("studies") or []):
        try:
            p = s.get("protocolSection") or {}
            idm, stm = p.get("identificationModule") or {}, p.get("statusModule") or {}
            spm = (p.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
            dm = p.get("designModule") or {}
            pcd = (stm.get("primaryCompletionDateStruct") or {})
            out.append({"nct": idm.get("nctId"), "title": idm.get("briefTitle") or "",
                        "status": stm.get("overallStatus") or "",
                        "phases": dm.get("phases") or [],
                        "enroll": ((dm.get("enrollmentInfo") or {}).get("count") or 0),
                        "sponsor": spm.get("name") or "", "sponsor_class": spm.get("class") or "",
                        "pcd": pcd.get("date") or "", "pcd_type": pcd.get("type") or "",
                        "has_results": bool(s.get("hasResults")),
                        "conditions": ((p.get("conditionsModule") or {}).get("conditions") or [])[:3],
                        "updated": (stm.get("lastUpdatePostDateStruct") or {}).get("date")})
        except Exception:
            continue
    return [x for x in out if x["nct"]]


def _ct_pcd_iso(pcd: str) -> str | None:
    """'2026-10' → '2026-10-01' (month precision is kept in details); '2026-10-14' as-is."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", pcd or ""):
        return pcd
    if re.fullmatch(r"\d{4}-\d{2}", pcd or ""):
        return pcd + "-01"
    return None


def _ct_phase_label(phases) -> str:
    return "/".join(p.replace("PHASE", "Phase ") for p in phases) or "N/A"


def _ct_query(adv: str) -> list[dict] | None:
    rows, token, ok = [], None, False
    for _ in range(_CT_MAX_PAGES):
        params = {"filter.advanced": adv, "fields": _CTGOV_FIELDS, "pageSize": 200, "format": "json"}
        if token:
            params["pageToken"] = token
        p = _json(_http_get(_CTGOV, params=params, headers={"Accept": "application/json"}))
        if not p or "studies" not in p:
            break
        ok = True
        rows += parse_ctgov(p)
        token = p.get("nextPageToken")
        if not token:
            break
    return rows if ok else None


def _src_clinical_trials(conn, universe, holds, cfg) -> int | None:
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=_CT_WINDOW_DAYS)
    base = "AREA[LeadSponsorClass]INDUSTRY AND AREA[Phase](PHASE3 OR PHASE2)"
    upcoming = _ct_query(f"{base} AND AREA[PrimaryCompletionDate]RANGE[{today},{end}]")
    if upcoming is None:
        return None
    recent = _ct_query(f"{base} AND AREA[LastUpdatePostDate]RANGE[{today - timedelta(days=3)},MAX] "
                       f"AND AREA[PrimaryCompletionDate]RANGE[{today - timedelta(days=365)},{end}]") or []
    by_nct = {r["nct"]: r for r in upcoming + recent}
    state = _snap_get(conn, "clinical_trials", "state") or {}
    now = _now()
    new = 0
    with conn:
        for nct, r in by_nct.items():
            ph = r["phases"]
            if "PHASE3" not in ph and ph != ["PHASE2"]:
                continue                              # Phase 1/2 combos aren't pivotal
            tkr = company_to_ticker(r["sponsor"])
            if ph == ["PHASE2"] and not (tkr and (r["enroll"] or 0) >= _CT_PH2_MIN_ENROLL):
                continue
            prev = state.get(nct)
            ev_date = _ct_pcd_iso(r["pcd"])
            label = _ct_phase_label(ph)
            _upsert_calendar(
                conn, "ClinicalTrials.gov", nct, event_type="trial_completion",
                title=f"{r['sponsor']}: {label} {r['title']} — primary completion {r['pcd']}",
                event_date=ev_date, ticker=tkr, status=r["status"],
                url=f"https://clinicaltrials.gov/study/{nct}",
                details={"nct": nct, "sponsor": r["sponsor"], "phases": ph, "enrollment": r["enroll"],
                         "pcd": r["pcd"], "pcd_type": r["pcd_type"],
                         "date_precision": "day" if len(r["pcd"]) == 10 else "month",
                         "conditions": r["conditions"], "has_results": r["has_results"]})
            if prev and tkr:
                flip = r["status"] != prev.get("status") and r["status"] in _CT_FLIP_TO
                results = r["has_results"] and not prev.get("has_results")
                if flip or results:
                    what = (f"results posted" if results and not flip else
                            f"status {prev.get('status') or '?'} → {r['status']}")
                    title = f"{tkr}: {label} trial {nct} ({_clip(r['title'], 90)}) {what}"
                    st = {"nct": nct, "sponsor": r["sponsor"], "phases": ph,
                          "prev_status": prev.get("status"), "status": r["status"],
                          "has_results": r["has_results"], "pcd": r["pcd"],
                          "conditions": r["conditions"]}
                    if _emit(conn, universe, holds, source_key="clinical_trials",
                             source_name="ClinicalTrials.gov", tier=1, kind="trial_update",
                             title=title, url=f"https://clinicaltrials.gov/study/{nct}",
                             natural_key=f"ctgov:{nct}:{r['status']}:{int(r['has_results'])}",
                             structured=st, tickers=[tkr], pub_epoch=_iso_to_epoch(r.get("updated"))):
                        new += 1
            state[nct] = {"status": r["status"], "has_results": r["has_results"], "pcd": r["pcd"],
                          "seen": now}
        # forget trials not seen for 180 days
        state = {k: v for k, v in state.items() if now - (v.get("seen") or 0) < 180 * 86400}
        _snap_put(conn, "clinical_trials", "state", state)
    return new


# ══════════════════════════════════════════════════════════════════════════════
# 5. FDA
# ══════════════════════════════════════════════════════════════════════════════

_FDA_FEEDS = [
    ("https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
     "FDA press"),
    ("https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
     "FDA drugs"),
]
# verb forms only: "FDA approves X" yes; index pages like "Notable Approvals | Drugs" no.
_FDA_DECISION_RE = re.compile(r"\b(approv(es|ed)|grants?\b.*\b(approval|authorization|designation)|"
                              r"authoriz(es|ed)|clear(s|ed)|complete response|refuses? to file|"
                              r"rejects?)\b", re.I)
_FDA_LEGAL_RE = re.compile(r"\b(recall\w*|warning letter|consent decree|import alert|seiz\w+)\b", re.I)
_FDA_MAX_AGE = 7 * 86400
_OPENFDA = "https://api.fda.gov/drug/drugsfda.json"

# "Company X Inc." / "Company X, Inc" / "(NYSE: ABC)" style mentions in text
_EXCH_TAG_RE = re.compile(r"\((?:NYSE(?: American| Arca)?|NASDAQ|Nasdaq(?:\s?[A-Z]{2})?|"
                          r"Cboe(?: BZX)?)\s*:\s*([A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?)\)")
_CO_SUFFIX_RE = re.compile(r"\b((?:[A-Z][\w\-&.']*\s){0,4}[A-Z][\w\-&.']*),?\s+"
                           r"(?:Inc|Corp|Corporation|Ltd|plc|N\.V|S\.A|AG|SE|Co|LLC|Holdings)\b\.?")


def tickers_in_text(text: str, names_text: str | None = None) -> list[str]:
    """US-exchange tags ('(Nasdaq: ABC)') in `text` first, then 'Name Inc.' mentions in
    `names_text` (default: `text`) resolved via company_to_ticker. Only SEC-registered
    tickers survive. Deduped, ordered, ≤3."""
    out = []
    try:
        for m in _EXCH_TAG_RE.finditer(text or ""):
            raw = m.group(1)
            t = next((c for c in (raw, raw.replace(".", "-")) if known_ticker(c)), None)
            if t and t not in out:
                out.append(t)
        if not out:
            for m in _CO_SUFFIX_RE.finditer(text if names_text is None else names_text):
                # the capitalized run can swallow preceding words ("…Syndrome Regeneron
                # Pharmaceuticals, Inc") — try the longest span first, then shorter tails
                # (tails must keep ≥2 words: "Wriggle Box LLC" must not become "Box" → BOX)
                words = m.group(1).split()
                for i in range(max(1, len(words) - 1)):
                    t = company_to_ticker(" ".join(words[i:]) + " Inc")
                    if t:
                        if t not in out:
                            out.append(t)
                        break
    except Exception:
        pass
    return out[:3]


def _src_fda_rss(conn, universe, holds, cfg) -> int | None:
    import mason_news_service as NS
    new, ok = 0, False
    now = _now()
    pages = []                                        # fetch first, write after (no lock held on the network)
    for url, disp in _FDA_FEEDS:
        r = _http_get(url, headers={"Accept": "application/rss+xml, application/xml, */*"})
        if r is not None:
            pages.append((disp, r.content))
    with conn:
        for disp, content in pages:
            ok = True
            for it in NS._parse_feed(content, 30):
                title, desc = it["title"], it.get("desc") or ""
                pub = it.get("pub_epoch")
                if pub and now - pub > _FDA_MAX_AGE:
                    continue
                if _FDA_DECISION_RE.search(title):
                    kind = "fda_decision"
                elif disp == "FDA press" and _FDA_LEGAL_RE.search(title):
                    kind = "legal_regulatory"
                else:
                    continue                          # facility lists, meetings, guidance docs
                tks = tickers_in_text(f"{title} {desc}")
                if _emit(conn, universe, holds, source_key="fda", source_name="FDA", tier=1,
                         kind=kind, title=title, url=it.get("url"),
                         natural_key=f"fda:rss:{it.get('url') or title}",
                         structured={"feed": disp, "tickers": tks}, tickers=tks or None,
                         pub_epoch=pub, desc=desc):
                    new += 1
    return new if ok else None


def parse_openfda_approvals(payload, since_yyyymmdd: str) -> list[dict]:
    """drugsfda JSON → novel ORIG approvals (NDA/BLA, not ANDA generics) since date."""
    out = []
    for p in ((payload or {}).get("results") or []):
        try:
            app = p.get("application_number") or ""
            if not (app.startswith("NDA") or app.startswith("BLA")):
                continue
            for s in p.get("submissions") or []:
                if (s.get("submission_type") == "ORIG" and s.get("submission_status") == "AP"
                        and (s.get("submission_status_date") or "") >= since_yyyymmdd):
                    cls = s.get("submission_class_code") or ""
                    if app.startswith("NDA") and cls not in ("TYPE 1", "TYPE 1/4"):
                        continue                      # new formulations etc.; keep NMEs
                    prods = p.get("products") or []
                    brand = (prods[0].get("brand_name") if prods else None) or "?"
                    ings = ", ".join(sorted({(a.get("name") or "").lower() for pr in prods[:1]
                                             for a in (pr.get("active_ingredients") or [])}))
                    out.append({"app": app, "sponsor": p.get("sponsor_name") or "",
                                "brand": brand, "ingredient": ings, "class": cls,
                                "date": s.get("submission_status_date")})
        except Exception:
            continue
    return out


def _src_openfda(conn, universe, holds, cfg) -> int | None:
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y%m%d")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    q = (f'submissions.submission_type:"ORIG" AND submissions.submission_status:"AP" '
         f'AND submissions.submission_status_date:[{since} TO {today}]')
    p = _json(_http_get(_OPENFDA, params={"search": q, "limit": 100}))
    if p is None or "results" not in p:
        # openFDA answers 404 {"error": NOT_FOUND} for an empty window — not a failure
        return 0 if p is not None else None
    new = 0
    with conn:
        for a in parse_openfda_approvals(p, since):
            tkr = company_to_ticker(a["sponsor"])
            if not tkr:
                continue
            d = a["date"]
            title = (f"FDA approves {a['brand'].title()}" + (f" ({a['ingredient']})" if a["ingredient"] else "")
                     + f" — {tkr} ({a['sponsor'].title()}), {a['app']}"
                     + (" novel drug" if a["class"].startswith("TYPE 1") else ""))
            if _emit(conn, universe, holds, source_key="fda", source_name="openFDA", tier=1,
                     kind="fda_decision", title=title,
                     url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={re.sub(r'[^0-9]', '', a['app'])}",
                     natural_key=f"openfda:{a['app']}:{d}", structured=a, tickers=[tkr],
                     pub_epoch=_iso_to_epoch(f"{d[:4]}-{d[4:6]}-{d[6:8]}")):
                new += 1
    return new


# ══════════════════════════════════════════════════════════════════════════════
# 6. PR wires
# ══════════════════════════════════════════════════════════════════════════════

_WIRES = {
    # registry key → (url, display)
    "wire_gnw": ("https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
                 "GlobeNewswire%20-%20News%20about%20Public%20Companies", "GlobeNewswire"),
    "wire_bw": ("https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==", "Business Wire"),
    "wire_prn_fin": ("https://www.prnewswire.com/rss/financial-services-latest-news/"
                     "financial-services-latest-news-list.rss", "PR Newswire"),
    "wire_prn_health": ("https://www.prnewswire.com/rss/health-latest-news/"
                        "health-latest-news-list.rss", "PR Newswire"),
    "wire_prn_tech": ("https://www.prnewswire.com/rss/technology-latest-news/"
                      "technology-latest-news-list.rss", "PR Newswire"),
}
# Step 3 (audit R8): the wires were polled every 15 minutes, four sources a pass
# (median delay 8-12 minutes). Checked 2026-11-13:
#   GlobeNewswire  robots.txt does not cover /RssFeed/; Cache-Control max-age 28 s;
#                  Last-Modified honoured (If-Modified-Since → 304).
#   PR Newswire    robots.txt does not cover /rss/; max-age 60 s; 304 honoured.
#   Business Wire  robots.txt DISALLOWS /rss/ for unnamed agents and the feed is
#                  cached 300 s upstream → left at the old 900 s, not sped up.
# So: catalyst.sources.wire_poll_seconds (default 120) for GlobeNewswire and
# PR Newswire, a conditional GET every time (a 304 costs a few hundred bytes), the
# server's own max-age respected as a lower bound, and a doubling back-off on 429.
_WIRE_POLL_DEFAULT = 120
_WIRE_MIN_S = {"wire_bw": 900}
_WIRE_BACKOFF_MAX = 3600
_WIRE_MAX_AGE = 2 * 86400
_WIRE_SPAM_RE = re.compile(r"class action|investor alert|shareholder alert|investors? (who|with) losses|"
                           r"law firm|rosen law|pomerantz|levi & korsinsky|bronstein|deadline alert|"
                           r"form 8\.3|market (size|report|worth)|cagr|\bmarketsandmarkets\b|"
                           r"to (participate|present) (in|at)|investor conferences?|conference call|"
                           r"webcast|announces (date|schedule|timing)|schedule for .*(earnings|results)|"
                           r"declares (quarterly |monthly )?(cash )?dividend", re.I)
_WIRE_KINDS = [
    ("mna", r"\b(to acquire|acquisition of|definitive (merger )?agreement to (acquire|be acquired)|"
            r"merger|to be acquired|tender offer|takeover)\b"),
    ("fda_decision", r"\b(FDA|PDUFA|BLA|NDA|EMA|CHMP)\b.*\b(approv|accept|grant|clear|designation|"
                     r"priority review)|\b(approval|clearance)\b"),
    ("trial_readout", r"\b(topline|top-line|phase [123][ab]?\b.*\b(data|results)|primary endpoint)\b"),
    ("guidance", r"\b(guidance|outlook|reaffirms|raises .*(forecast|outlook)|preliminary .*results)\b"),
    ("earnings", r"\b(reports|announces) (first|second|third|fourth|q[1-4]|fiscal)[\w\s-]*"
                 r"(quarter|year)?[\w\s-]*results\b"),
    ("contract_award", r"\b(awarded|wins .*contract|contract (award|win)|selected by)\b"),
    ("deal_partnership", r"\b(partner(ship|s with)?|collaborat\w+|strategic alliance|"
                         r"licens(e|ing) agreement|joint venture|teams up)\b"),
    ("product_launch", r"\b(launch(es|ed)?|unveil\w*|introduc(es|ing)|debuts?|now available|"
                       r"rolls? out)\b"),
]
_WIRE_LEAD_RE = re.compile(r"^(.{3,80}?)\s+(?:announces?|reports?|launches|to\s|completes|receives|"
                           r"enters|signs|expands|unveils|introduces|appoints|names|prices|"
                           r"files|provides|secures|wins|partners|declares|closes|achieves|"
                           r"presents|joins|selects|acquires|agrees|and\s|,|:)", re.I)


def _nq_label_fixes() -> bool:
    try:
        import mason_news_quality as NQ
        return NQ.on("label_fixes")
    except Exception:
        return False


# 2026-11-15 (news.quality.label_fixes): "approval" / "clearance" is a drug or device decision
# only when a regulator or a medical product is named ("Receives Shareholder Approval" and a
# "ConsumerLab Seal of Approval" are not); "top-line" is a trial readout only beside clinical
# words ("beats top-line and bottom-line estimates" is earnings).
_WIRE_MED_RE = re.compile(r"\b(FDA|EMA|CHMP|European Commission|Health Canada|MHRA|PMDA|NMPA|TGA|PDUFA|NDA|sNDA|"
                          r"BLA|sBLA|ANDA|510\(k\)|PMA|De Novo|EUA|drugs?|therap\w+|treatments?|medicines?|"
                          r"vaccines?|biologics?|biosimilars?|devices?|implants?|indications?|clinical|patients?)\b", re.I)
_WIRE_CLIN_RE = re.compile(r"\b(phase|trial|study|studies|endpoints?|patients?|cohort|placebo|efficacy)\b", re.I)


# 2026-11-16 (label_fixes, follow-up 7): a product that is already "FDA-approved" (a hyphenated
# adjective, e.g. in a licensing deal; "the FDA approved X" is the decision) is not an FDA decision; an agreement for commercialisation rights is
# a deal (same forms as mason_triage._EXTRA_RULES)
_WIRE_FDA_ADJ_RE = re.compile(r"\b(?:FDA|EMA)-(?:approved|cleared|authori[sz]ed)\b", re.I)
_WIRE_COMMERCIAL_RE = re.compile(r"\b(?:agreements?|deals?|rights?) (?:to|for) (?:the )?(?:co-)?commerciali[sz]\w*|"
                                 r"\bcommerciali[sz]ation (?:agreements?|rights|deals?)\b", re.I)


def wire_kind(text: str) -> str:
    fix = _nq_label_fixes()
    for kind, pat in _WIRE_KINDS:
        if re.search(pat, text, re.I) or (fix and kind == "deal_partnership" and _WIRE_COMMERCIAL_RE.search(text)):
            if fix and kind == "fda_decision" and not _WIRE_MED_RE.search(text):
                continue
            if fix and kind == "fda_decision" and _WIRE_FDA_ADJ_RE.search(text) and \
                    not re.search(pat, _WIRE_FDA_ADJ_RE.sub(" ", text), re.I):
                continue
            if fix and kind == "trial_readout" and not _WIRE_CLIN_RE.search(text):
                continue
            return kind
    return "other"


def wire_headline_kind(title: str, desc: str = "") -> str:
    """The kind a wire item is filed under: the HEADLINE decides; the blurb only fills
    in when the headline says nothing ('Announces Third Quarter Results' is earnings even
    when the blurb mentions the merger it is part of). label_fixes off: title+blurb."""
    if not _nq_label_fixes():
        return wire_kind(f"{title} {desc[:200]}")
    k = wire_kind(title or "")
    return k if k != "other" else wire_kind(f"{title} {desc[:200]}")


# news phase 6b (mason_deals, switch news.deals.enabled, default off): the deal a release announces.
# Its kind replaces the headline kind above, except for the drug, trial and results kinds, which say
# more about the release than a deal word in the same headline.
_DEAL_KEEP_KINDS = {"fda_decision", "trial_readout", "trial_update", "earnings", "guidance"}


def _wire_deal(title: str, desc: str, tks):
    """mason_deals.for_wire: a dict for a deal release, None otherwise or while the switch is off.
    Never raises (a detector problem must never cost the scan a release)."""
    try:
        import mason_deals
        return mason_deals.for_wire(title, desc, tks)
    except Exception:
        return None


def wire_tickers(title: str, desc: str) -> list[str]:
    tks = tickers_in_text(f"{title} {desc}", names_text=title)   # names: headline only
    if tks:
        return tks
    m = _WIRE_LEAD_RE.match(title or "")
    if m:
        t = company_to_ticker(m.group(1))
        if t:
            return [t]
    return []


def _src_wire(key: str):
    url, disp = _WIRES[key]

    def run(conn, universe, holds, cfg) -> int | None:
        import mason_news_service as NS
        st = _state.setdefault(key, {})
        hdrs = {"Accept": "application/rss+xml, application/xml, text/xml, */*"}
        if _sources_cfg(cfg).get("wire_conditional_get", True):
            if st.get("etag"):
                hdrs["If-None-Match"] = st["etag"]
            if st.get("lm"):
                hdrs["If-Modified-Since"] = st["lm"]
        _last_http.update(status=None, retry_after=None)
        r = _http_get(url, headers=hdrs)
        if r is None:
            code = _last_http.get("status")
            if code == 304:
                st["backoff"] = 0
                return 0                              # nothing new since the last poll: a success
            if code == 429:                           # asked to slow down: double the wait
                wait = max(float(_last_http.get("retry_after") or 0),
                           min(_WIRE_BACKOFF_MAX, max(300.0, 2.0 * float(st.get("backoff") or 0))))
                st["backoff"] = wait
                st["backoff_until"] = _now() + wait
                log.info(f"[sources] {key}: 429 from the wire, next poll in {int(wait)}s")
            return None
        st["backoff"] = 0
        try:
            hh = getattr(r, "headers", None) or {}
            st["etag"], st["lm"] = hh.get("ETag"), hh.get("Last-Modified")
            m = re.search(r"max-age=(\d+)", hh.get("Cache-Control") or "")
            st["max_age"] = min(900, int(m.group(1))) if m else None
        except Exception:
            pass
        items = NS._parse_feed(r.content, 60)
        if not items and b"<rss" not in r.content[:2000] and b"<feed" not in r.content[:2000]:
            return None                               # HTML error page served as 200
        now, new = _now(), 0
        with conn:
            for it in items:
                title, desc = it["title"], it.get("desc") or ""
                pub = it.get("pub_epoch")
                if pub and now - pub > _WIRE_MAX_AGE:
                    continue
                if _WIRE_SPAM_RE.search(title):
                    continue
                tks = wire_tickers(title, desc)
                if not tks:
                    continue                          # contract: public tickers only
                kind = wire_headline_kind(title, desc)
                structured = {"wire": disp, "tickers": tks}
                deal = _wire_deal(title, desc, tks)   # news phase 6b; always None while news.deals is off
                if deal:
                    structured["deal"] = deal
                    if kind not in _DEAL_KEEP_KINDS:
                        kind = deal["triage_kind"]
                if _emit(conn, universe, holds, source_key="pr_wires", source_name=disp, tier=2,
                         kind=kind, title=title, url=it.get("url"),
                         natural_key=f"wire:{it.get('url') or title}",
                         structured=structured, tickers=tks,
                         pub_epoch=pub, desc=desc):
                    new += 1
        return new
    return run


# ══════════════════════════════════════════════════════════════════════════════
# 7. Prediction markets
# ══════════════════════════════════════════════════════════════════════════════

_POLY_EVENTS = "https://gamma-api.polymarket.com/events"
_POLY_TAGS = ["fed-rates", "tariffs", "china", "geopolitics", "oil", "inflation"]
_POLY_PER_TAG = 12
_PM_KEYWORDS = re.compile(
    r"\b(fed|fomc|federal reserve|rate (cut|hike)s?|interest rates?|bps|tariffs?|trade (deal|"
    r"agreement|war)|china|xi\b|taiwan|recession|oil|crude|wti|brent|opec|hormuz|iran|russia|"
    r"ukraine|inflation|cpi|shutdown|debt ceiling|sanctions?|export controls?|treasury yield)\b", re.I)
_PM_SKIP = re.compile(r"up or down|\bhit\b.*\bon (monday|tuesday|wednesday|thursday|friday)|"
                      r"(close|open)s? (above|below) .* on ", re.I)
_PM_MIN_VOL24 = 20_000
_PM_MIN_LIQ = 50_000
_PM_MAX_TRACKED = 80
_PM_MOVE = 0.10
_PM_WINDOW = 24 * 3600
_KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
_KALSHI_SERIES = ["KXFEDDECISION", "KXRECSSNBER"]
_KALSHI_MIN_VOL24 = 1_000


def parse_polymarket_events(payload) -> list[dict]:
    """gamma /events JSON → [{id, q, p, vol24, liq, end, slug}] binary Yes/No markets."""
    out = []
    for ev in (payload if isinstance(payload, list) else []):
        try:
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("active", True):
                    continue
                outs = json.loads(m.get("outcomes") or "[]")
                prices = json.loads(m.get("outcomePrices") or "[]")
                if len(outs) != 2 or str(outs[0]).lower() != "yes" or len(prices) != 2:
                    continue
                q = m.get("question") or ""
                if not _PM_KEYWORDS.search(q) and not _PM_KEYWORDS.search(ev.get("title") or ""):
                    continue
                if _PM_SKIP.search(q):
                    continue
                out.append({"id": str(m.get("id")), "q": q, "p": float(prices[0]),
                            "vol24": float(m.get("volume24hr") or 0),
                            "liq": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                            "end": m.get("endDate") or "", "event": ev.get("title") or "",
                            "slug": ev.get("slug") or m.get("slug") or ""})
        except Exception:
            continue
    return out


def parse_kalshi_markets(payload) -> list[dict]:
    """Kalshi /markets JSON → nearest-event markets [{id, q, p, vol24, end, event}]."""
    ms = ((payload or {}).get("markets") or []) if isinstance(payload, dict) else []
    if not ms:
        return []
    try:
        nearest = min((m.get("close_time") or "9999") for m in ms)
        nearest_ev = {m.get("event_ticker") for m in ms if (m.get("close_time") or "9999") == nearest}
    except Exception:
        nearest_ev = set()
    out = []
    for m in ms:
        try:
            if nearest_ev and m.get("event_ticker") not in nearest_ev:
                continue
            f = lambda k: float(m.get(k) or 0)
            bid, ask, last = f("yes_bid_dollars"), f("yes_ask_dollars"), f("last_price_dollars")
            p = (bid + ask) / 2 if bid > 0 and ask > 0 and ask - bid <= 0.10 else last
            out.append({"id": m.get("ticker"), "q": m.get("title") or "", "p": p,
                        "vol24": f("volume_24h_fp"), "liq": f("open_interest_fp"),
                        "end": m.get("close_time") or "", "event": m.get("event_ticker") or "",
                        "slug": m.get("event_ticker") or ""})
        except Exception:
            continue
    return out


def odds_move(history: list, p_now: float, now: float) -> tuple[float, float] | None:
    """Largest |p_now - p_then| over the last 24h of [[ts, p], ...] → (delta, p_then)."""
    best = None
    for ts, p in history or []:
        if now - ts > _PM_WINDOW:
            continue
        d = p_now - p
        if best is None or abs(d) > abs(best[0]):
            best = (d, p)
    return best


def _pm_update(conn, universe, holds, venue: str, markets: list[dict]) -> int:
    """Snapshot odds per market and emit on a ≥10-pt move within 24h. Re-alerts only
    after a further ≥10-pt move from the last alerted price."""
    now = _now()
    snap = _snap_get(conn, "prediction_markets", venue) or {}
    markets = sorted(markets, key=lambda m: -m["vol24"])[:_PM_MAX_TRACKED]
    new = 0
    disp = "Polymarket" if venue == "polymarket" else "Kalshi"
    for m in markets:
        s = snap.get(m["id"]) or {"h": []}
        hist = [[ts, p] for ts, p in s.get("h", []) if now - ts <= _PM_WINDOW + 3600]
        mv = odds_move(hist, m["p"], now)
        alert = s.get("alert")                   # [ts, p]
        if mv and abs(mv[0]) >= _PM_MOVE and (
                not alert or abs(m["p"] - alert[1]) >= _PM_MOVE):
            d, p0 = mv
            title = (f"{disp}: \"{_clip(m['q'], 120)}\" odds {p0 * 100:.0f}% → {m['p'] * 100:.0f}% "
                     f"({'+' if d > 0 else ''}{d * 100:.0f} pts in 24h)")
            hour = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H")
            url = (f"https://polymarket.com/event/{m['slug']}" if venue == "polymarket"
                   else f"https://kalshi.com/markets/{m['event'].split('-')[0].lower()}")
            if _emit(conn, universe, holds, source_key="prediction_markets", source_name=disp,
                     tier=2, kind="prediction_market", title=title, url=url,
                     natural_key=f"pm:{venue}:{m['id']}:{hour}",
                     structured={"venue": venue, "market_id": m["id"], "question": m["q"],
                                 "event": m["event"], "p_now": round(m["p"], 4),
                                 "p_24h_ref": round(p0, 4), "delta_pts": round(d * 100, 1),
                                 "volume_24h": m["vol24"], "end": m["end"]},
                     pub_epoch=now):
                new += 1
            s["alert"] = [now, m["p"]]
        hist.append([now, m["p"]])
        s["h"] = hist[-120:]
        s["q"] = m["q"][:160]
        snap[m["id"]] = s
    # drop markets we stopped seeing for 3 days
    snap = {k: v for k, v in snap.items() if v.get("h") and now - v["h"][-1][0] < 3 * 86400}
    _snap_put(conn, "prediction_markets", venue, snap)
    return new


def _src_polymarket(conn, universe, holds, cfg) -> int | None:
    seen, markets, ok = set(), [], False
    for tag in _POLY_TAGS:
        p = _json(_http_get(_POLY_EVENTS, params={"tag_slug": tag, "closed": "false", "active": "true",
                                                  "limit": _POLY_PER_TAG, "order": "volume24hr",
                                                  "ascending": "false"}))
        if not isinstance(p, list):
            continue
        ok = True
        for m in parse_polymarket_events(p):
            if m["id"] in seen:
                continue
            if m["vol24"] < _PM_MIN_VOL24 and m["liq"] < _PM_MIN_LIQ:
                continue
            end = _iso_to_epoch(m["end"])
            if end and end - _now() < 86400:
                continue                              # resolving within a day: noise
            seen.add(m["id"])
            markets.append(m)
    if not ok:
        return None
    with conn:
        return _pm_update(conn, universe, holds, "polymarket", markets)


def _src_kalshi(conn, universe, holds, cfg) -> int | None:
    markets, ok = [], False
    for series in _KALSHI_SERIES:
        p = _json(_http_get(_KALSHI, params={"series_ticker": series, "status": "open", "limit": 200},
                            headers={"Accept": "application/json"}))
        if not isinstance(p, dict) or "markets" not in p:
            continue
        ok = True
        markets += [m for m in parse_kalshi_markets(p) if m["vol24"] >= _KALSHI_MIN_VOL24]
    if not ok:
        return None
    with conn:
        return _pm_update(conn, universe, holds, "kalshi", markets)


# ══════════════════════════════════════════════════════════════════════════════
# 8. FRED / EIA (free keys; empty key ⇒ silently skipped)
# ══════════════════════════════════════════════════════════════════════════════

_FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"
# series → (label, kind: monthly|daily, daily alert threshold over 5 obs)
_FRED_SERIES = {
    "CPIAUCSL": ("CPI (all items)", "monthly", None),
    "PCEPILFE": ("Core PCE price index", "monthly", None),
    "UNRATE": ("Unemployment rate", "monthly", None),
    "T10Y2Y": ("10y-2y Treasury spread", "daily", 0.15),
    "BAMLH0A0HYM2": ("HY OAS credit spread", "daily", 0.30),
}
_EIA_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
_EIA_SERIES = "WCESTUS1"      # US crude stocks excl. SPR, thousand barrels, weekly


def _fred_obs(payload) -> list[tuple[str, float]]:
    """observations (desc) → [(date, value)] skipping '.' placeholders."""
    out = []
    for o in ((payload or {}).get("observations") or []):
        try:
            if o.get("value") not in (None, "", "."):
                out.append((o["date"], float(o["value"])))
        except Exception:
            continue
    return out


def fred_event(sid: str, obs: list[tuple[str, float]], snap: dict) -> tuple[str, dict] | None:
    """(headline, structured) if `obs` holds a NEW release worth a headline vs `snap`
    ({'last_date', 'alert_date'}); None otherwise. Baseline (empty snap) never emits."""
    if not obs:
        return None
    label, freq, thr = _FRED_SERIES[sid]
    d0, v0 = obs[0]
    last = snap.get("last_date")
    if not last or d0 <= last:
        return None
    st = {"series": sid, "label": label, "date": d0, "value": v0}
    if freq == "monthly":
        parts = [f"{v0:,.2f}" if sid != "UNRATE" else f"{v0:.1f}%"]
        if len(obs) > 1:
            d1, v1 = obs[1]
            if sid == "UNRATE":
                parts.append(f"({v0 - v1:+.1f} pt m/m)")
            else:
                parts.append(f"({(v0 / v1 - 1) * 100:+.2f}% m/m")
                if len(obs) > 12:
                    parts[-1] += f", {(v0 / obs[12][1] - 1) * 100:+.2f}% y/y)"
                else:
                    parts[-1] += ")"
            st["prev"] = v1
        return f"FRED: {label} ({sid}) for {d0[:7]} = " + " ".join(parts), st
    # daily series: only regime-relevant moves / sign flips, at most once per 5 days
    if len(obs) < 6:
        return None
    v5 = obs[5][1]
    flip = sid == "T10Y2Y" and (v0 >= 0) != (obs[1][1] >= 0)
    alert = snap.get("alert_date") or ""
    recent_alert = alert and (datetime.strptime(d0, "%Y-%m-%d") -
                              datetime.strptime(alert, "%Y-%m-%d")).days < 5
    if (flip or abs(v0 - v5) >= thr) and not recent_alert:
        st.update(prev_5obs=v5, change=round(v0 - v5, 3), sign_flip=flip)
        what = ("flips " + ("positive (curve un-inverts)" if v0 >= 0 else "negative (curve inverts)")
                if flip else f"moves {v0 - v5:+.2f} over 5 sessions")
        return f"FRED: {label} ({sid}) {what}, now {v0:.2f} ({d0})", st
    return None


def _src_fred(conn, universe, holds, cfg) -> int | None:
    key = _api_key(cfg, "fred")
    if not key:
        return 0
    new, ok = 0, False
    pulled = {}                                       # fetch first, write after (no lock held on the network)
    for sid in _FRED_SERIES:
        pulled[sid] = _json(_http_get(_FRED_OBS, params={"series_id": sid, "api_key": key, "file_type": "json",
                                                         "sort_order": "desc", "limit": 13}))
    with conn:
        for sid in _FRED_SERIES:
            p = pulled.get(sid)
            obs = _fred_obs(p)
            if not obs:
                continue
            ok = True
            snap = _snap_get(conn, "fred", sid) or {}
            ev = fred_event(sid, obs, snap)
            if ev:
                title, st = ev
                kind = "macro_data"
                if _emit(conn, universe, holds, source_key="fred", source_name="FRED", tier=1,
                         kind=kind, title=title,
                         url=f"https://fred.stlouisfed.org/series/{sid}",
                         natural_key=f"fred:{sid}:{obs[0][0]}", structured=st, pub_epoch=_now()):
                    new += 1
                if _FRED_SERIES[sid][1] == "daily":
                    snap["alert_date"] = obs[0][0]
            snap["last_date"] = max(obs[0][0], snap.get("last_date") or "")
            _snap_put(conn, "fred", sid, snap)
    return new if ok else None


def parse_eia(payload) -> list[tuple[str, float]]:
    out = []
    for r in (((payload or {}).get("response") or {}).get("data") or []):
        try:
            out.append((str(r["period"]), float(r["value"])))
        except Exception:
            continue
    out.sort(reverse=True)
    return out


def _src_eia(conn, universe, holds, cfg) -> int | None:
    key = _api_key(cfg, "eia")
    if not key:
        return 0
    params = {"api_key": key, "frequency": "weekly", "data[0]": "value",
              "facets[series][]": _EIA_SERIES, "sort[0][column]": "period",
              "sort[0][direction]": "desc", "length": 3}
    rows = parse_eia(_json(_http_get(_EIA_URL, params=params)))
    if len(rows) < 2:
        return None
    snap = _snap_get(conn, "eia", _EIA_SERIES) or {}
    (d0, v0), (d1, v1) = rows[0], rows[1]
    new = 0
    with conn:
        if snap.get("last_period") and d0 > snap["last_period"]:
            chg = (v0 - v1) / 1000.0
            title = (f"EIA: US crude inventories {'build' if chg >= 0 else 'draw'} of "
                     f"{abs(chg):.1f}M bbl for week ending {d0} (stocks {v0 / 1000:.1f}M bbl ex-SPR)")
            if _emit(conn, universe, holds, source_key="eia", source_name="EIA", tier=1,
                     kind="commodity", title=title,
                     url="https://www.eia.gov/petroleum/supply/weekly/",
                     natural_key=f"eia:{_EIA_SERIES}:{d0}",
                     structured={"series": _EIA_SERIES, "period": d0, "value_kbbl": v0,
                                 "prev_kbbl": v1, "change_mbbl": round(chg, 3)},
                     pub_epoch=_now()):
                new += 1
        snap["last_period"] = max(d0, snap.get("last_period") or "")
        _snap_put(conn, "eia", _EIA_SERIES, snap)
    return new


# ══════════════════════════════════════════════════════════════════════════════
# 9. Economic calendar (Forex Factory) → calendar_events only
# ══════════════════════════════════════════════════════════════════════════════

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_FF_CB_RE = re.compile(r"\b(fomc|federal funds|fed chair|powell|monetary policy|beige book)\b", re.I)


def parse_ff(payload) -> list[dict]:
    out = []
    for r in (payload if isinstance(payload, list) else []):
        try:
            if r.get("country") != "USD" or r.get("impact") not in ("High", "Medium"):
                continue
            dt = datetime.fromisoformat(str(r["date"]))
            out.append({"title": str(r.get("title") or "").strip(), "impact": r["impact"],
                        "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M %z"),
                        "forecast": r.get("forecast") or "", "previous": r.get("previous") or ""})
        except Exception:
            continue
    return out


def _src_econ_calendar(conn, universe, holds, cfg) -> int | None:
    rows = _json(_http_get(_FF_URL, headers={"Accept": "application/json"}))
    if not isinstance(rows, list):
        return None
    with conn:
        for r in parse_ff(rows):
            kind = "central_bank" if _FF_CB_RE.search(r["title"]) else "macro_data"
            _upsert_calendar(conn, "Forex Factory", f"{r['title']}|{r['date']}|{r['time']}",
                             event_type="econ", title=f"USD {r['title']} ({r['impact']} impact)",
                             event_date=r["date"], event_time=r["time"], status=r["impact"].lower(),
                             url="https://www.forexfactory.com/calendar",
                             details={"impact": r["impact"], "forecast": r["forecast"],
                                      "previous": r["previous"], "kind": kind, "country": "USD"})
    return 0                                          # calendar only — no headlines


# ══════════════════════════════════════════════════════════════════════════════
# registry + cadence + public entry point
# ══════════════════════════════════════════════════════════════════════════════

# (registry key, config sub-flag, fetcher, cadence seconds)
_REGISTRY = [
    ("app_charts", "app_charts", _src_app_charts, 1800),
    ("edgar_s1", "edgar_forms", _src_edgar_ipo("S-1"), 1200),
    ("edgar_f1", "edgar_forms", _src_edgar_ipo("F-1"), 1200),
    ("edgar_424b4", "edgar_forms", _src_edgar_ipo("424B4"), 1200),
    ("edgar_13d", "edgar_forms", _src_edgar_13d, 1200),
    ("edgar_form4", "edgar_forms", _src_edgar_form4, 600),
    ("ipo_calendar", "ipo_calendar", _src_ipo_calendar, 3600),
    ("clinical_trials", "clinical_trials", _src_clinical_trials, 6 * 3600),
    ("fda_rss", "fda", _src_fda_rss, 1800),
    ("openfda", "fda", _src_openfda, 6 * 3600),
    *[(k, "pr_wires", _src_wire(k), 900) for k in _WIRES],
    ("polymarket", "prediction_markets", _src_polymarket, 1200),
    ("kalshi", "prediction_markets", _src_kalshi, 1200),
    ("fred", "fred", _src_fred, 3 * 3600),
    ("eia", "eia", _src_eia, 3600),
    ("econ_calendar", "econ_calendar", _src_econ_calendar, 6 * 3600),
]
# drained every pass while it has work, after the network sources, if budget remains
_DRAIN = ("edgar_form4_drain", "edgar_forms", _src_edgar_form4_drain)

_NEEDS_KEY = {"fred": "fred", "eia": "eia"}    # sub-flag → apis.<name>.key

_FAIL_RETRY_S = 900           # a failed source retries sooner than its cadence...
_FAIL_REST_AFTER = 5          # ...until this many straight failures, then rests
_FAIL_REST_S = 3600

_state: dict = {}             # registry key → {"last_poll", "ok", "fail_streak", "n", "ms"}
_state_loaded = False
_pass_lock = threading.Lock()


def _load_state(conn) -> None:
    global _state_loaded
    if _state_loaded:
        return
    try:
        for key, payload in conn.execute("SELECT key, payload_json FROM source_snapshots "
                                         "WHERE source='_cadence' AND record_deleted=0"):
            try:
                _state.setdefault(key, json.loads(payload) or {})
            except Exception:
                continue
        _state_loaded = True
    except Exception:
        pass


def _next_due(key: str, every: int) -> float:
    s = _state.get(key) or {}
    last = s.get("last_poll") or 0
    fs = s.get("fail_streak") or 0
    if fs >= _FAIL_REST_AFTER:
        return last + max(every, _FAIL_REST_S)
    if fs > 0:
        return last + min(every, _FAIL_RETRY_S)
    return last + every


def _cadence(key: str, every: int, sc: dict) -> int:
    """Poll interval for one source. Wires: catalyst.sources.wire_poll_seconds
    (default 120), never below the wire's own floor (_WIRE_MIN_S) or the max-age the
    server last announced. Everything else: the registry value."""
    if key not in _WIRES:
        return every
    try:
        want = int(sc.get("wire_poll_seconds", _WIRE_POLL_DEFAULT) or _WIRE_POLL_DEFAULT)
    except Exception:
        want = _WIRE_POLL_DEFAULT
    st = _state.get(key) or {}
    return int(max(30, want, _WIRE_MIN_S.get(key, 0), st.get("max_age") or 0))


def _record(conn, key: str, ok: bool, n: int, ms: int) -> None:
    s = dict(_state.get(key) or {})
    s["last_poll"] = _now()
    s["ok"] = ok
    s["fail_streak"] = 0 if ok else (s.get("fail_streak") or 0) + 1
    s["n"], s["ms"] = n, ms
    _state[key] = s
    try:
        with conn:
            _snap_put(conn, "_cadence", key, s)
    except Exception:
        pass


def _run_one(conn, universe, holds, cfg, key, fn) -> int:
    t0 = time.monotonic()
    try:
        n = fn(conn, universe, holds, cfg)
    except Exception as e:
        log.warning(f"[sources] {key} failed: {e}")
        n = None
    ms = int((time.monotonic() - t0) * 1000)
    _record(conn, key, n is not None, int(n or 0), ms)
    return int(n or 0)


def scan_sources(conn, universe, holds, *, force: bool = False, only=None) -> int:
    """One bounded pass over the due catalyst sources. Returns NEW news_events rows.
    `force`/`only` (tests + manual live checks) ignore cadence / restrict keys.
    Flag off ⇒ 0 with no network and no writes. Never raises."""
    try:
        cfg = _cfg()
        sc = _sources_cfg(cfg)
        if not sc.get("enabled", False):
            return 0
        if not _pass_lock.acquire(blocking=False):
            return 0
        try:
            _load_state(conn)
            now = _now()
            due = []
            # news phase 6b: one wire can be left out by name (catalyst.sources.wires_off, e.g. ["wire_bw"]: Business
            # Wire's robots.txt asks unnamed readers not to read its feed). Empty or absent = every wire, as before.
            try:
                wires_off = {str(k) for k in (sc.get("wires_off") or []) if str(k) in _WIRES}
            except Exception:
                wires_off = set()
            for key, flag, fn, every in _REGISTRY:
                if only is not None and key not in only:
                    continue
                if not sc.get(flag, True):
                    continue
                if key in wires_off:
                    continue
                if flag in _NEEDS_KEY and not _api_key(cfg, _NEEDS_KEY[flag]):
                    continue                              # free key not set: silently skip
                every = _cadence(key, every, sc)
                nd = _next_due(key, every)
                if key in _WIRES:                         # a wire that answered 429 waits it out
                    nd = max(nd, float((_state.get(key) or {}).get("backoff_until") or 0))
                if force or now >= nd:
                    due.append(((now - nd) / max(every, 1), key, fn))
            due.sort(key=lambda x: -x[0])                 # most overdue first
            try:
                max_per_pass = int(sc.get("max_sources_per_pass", _MAX_SOURCES_PER_PASS))
                budget_s = float(sc.get("pass_budget_seconds", _PASS_BUDGET_S))
            except Exception:
                max_per_pass, budget_s = _MAX_SOURCES_PER_PASS, _PASS_BUDGET_S
            t0, new, ran = time.monotonic(), 0, 0
            # the wires have their own lane: a conditional GET is a fraction of a
            # second, and at a 2-minute cadence they would otherwise take every slot
            # of the pass and starve the slower sources
            for _, key, fn in [d for d in due if d[1] in _WIRES]:
                if not force and time.monotonic() - t0 > budget_s:
                    break
                new += _run_one(conn, universe, holds, cfg, key, fn)
            for _, key, fn in [d for d in due if d[1] not in _WIRES]:
                if not force and (ran >= max_per_pass
                                  or time.monotonic() - t0 > budget_s):
                    break
                new += _run_one(conn, universe, holds, cfg, key, fn)
                ran += 1
            dkey, dflag, dfn = _DRAIN
            if ((only is None or dkey in only) and sc.get(dflag, True)
                    and (force or time.monotonic() - t0 <= budget_s)
                    and _f4_queue_len(conn) > 0):
                new += _run_one(conn, universe, holds, cfg, dkey, dfn)
            if new:
                log.info(f"[sources] +{new} catalyst-source events ({ran} sources polled)")
            return new
        finally:
            _pass_lock.release()
    except Exception as e:
        log.warning(f"[sources] pass failed: {e}")
        return 0


def source_status(conn=None) -> dict:
    """{registry key: {last_poll, ok, fail_streak, n, ms, next_due}} for a status page."""
    own = conn is None
    try:
        if own:
            import mason_db
            conn = mason_db.get_connection()
        _load_state(conn)
        out = {}
        _sc = _sources_cfg(_cfg())
        for key, _flag, _fn, every in _REGISTRY + [(_DRAIN[0], None, None, 0)]:
            s = dict(_state.get(key) or {})
            every = _cadence(key, every, _sc) if every else every
            s["next_due"] = _next_due(key, every) if every else None
            out[key] = s
        return out
    except Exception:
        return {}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
