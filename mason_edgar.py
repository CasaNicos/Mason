"""
mason_edgar.py — Mason's SEC EDGAR filing finder + analyzer.

Closes the capability gap Mason flagged: pull a company's SEC filing autonomously
by ticker OR company name, extract the sections that matter, and hand them back as
plain text for Mason (Opus) to reason over live in the conversation.

Primary use case: IPO / prospectus analysis. Ask "analyze the Reddit S-1" or
"pull RKLB's latest 10-K risk factors" and Mason gets the real filing text —
risk factors, use of proceeds, dilution, business, MD&A, underwriting — not just
headlines from get_news.

Design (mirrors mason_news.py conventions):
  - 100% key-free. EDGAR's data.sec.gov / www.sec.gov APIs are public and free.
  - SEC requires a descriptive User-Agent (name + contact). Read from config
    (apis.sec.user_agent), else a sane default. SEC throttles anonymous / missing
    UAs, so this matters.
  - TTL + on-disk caching: the ticker->CIK map (~1 MB, changes rarely) is cached
    to disk for a week; submissions JSON is memory-cached for the session.
  - Everything degrades gracefully and NEVER raises. On any failure it returns a
    human-readable diagnostic string so Mason can tell Nico what went wrong.
  - Returns TEXT, not a pre-baked summary. Nico chose "extract sections, Mason
    reasons live" — cheapest and most flexible.

Public API:
    analyze_filing(query, form_type=None, sections=None, list_only=False,
                   limit=8, max_chars=42000) -> str
    resolve_cik(query) -> (cik_int, title) | (None, reason)

Endpoints used (all public, no auth):
    https://www.sec.gov/files/company_tickers.json         ticker -> CIK map
    https://data.sec.gov/submissions/CIK##########.json    filing history
    https://efts.sec.gov/LATEST/search-index?q=...          full-text name search
    https://www.sec.gov/Archives/edgar/data/<cik>/<acc>/<doc>   filing document
"""

import json
import re
import time
import html as _htmllib
from pathlib import Path

import requests

BASE          = Path(__file__).parent
CONFIG        = BASE / "mason.config.json"
TICKERS_CACHE = BASE / ".edgar_tickers_cache.json"

# ── caches ─────────────────────────────────────────────────────────────────────
_TICKERS_TTL  = 7 * 24 * 3600          # ticker->CIK map: refresh weekly
_SUBS_TTL     = 15 * 60                # submissions JSON: 15 min memory cache
_DOC_TTL      = 60 * 60               # downloaded documents: 1 hr memory cache
_subs_cache   = {}                    # cik -> {"ts":, "data":}
_doc_cache    = {}                    # url -> {"ts":, "text":}

# ── prospectus / IPO form set (the default coverage) ───────────────────────────
# S-1 = domestic IPO registration; F-1 = foreign issuer; 424B* = the final
# prospectus filed after effectiveness; S-11 = real-estate (REIT) IPOs.
_PROSPECTUS_FORMS = [
    "S-1", "S-1/A", "F-1", "F-1/A", "S-11", "S-11/A",
    "424B1", "424B2", "424B3", "424B4", "424B5",
    "DRS", "DRSLTR",          # confidential draft registration statements
]

# Friendly aliases Nico (or Opus) might pass as form_type.
_FORM_ALIASES = {
    "prospectus": _PROSPECTUS_FORMS,
    "ipo":        _PROSPECTUS_FORMS,
    "s1":         ["S-1", "S-1/A"],
    "s-1":        ["S-1", "S-1/A"],
    "f1":         ["F-1", "F-1/A"],
    "f-1":        ["F-1", "F-1/A"],
    "10k":        ["10-K", "10-K/A"],
    "10-k":       ["10-K", "10-K/A"],
    "annual":     ["10-K", "10-K/A"],
    "10q":        ["10-Q", "10-Q/A"],
    "10-q":       ["10-Q", "10-Q/A"],
    "quarterly":  ["10-Q", "10-Q/A"],
    "8k":         ["8-K", "8-K/A"],
    "8-k":        ["8-K", "8-K/A"],
    "any":        None,       # None = match every form
    "all":        None,
}

# ── section extraction map ─────────────────────────────────────────────────────
# Prospectuses use ALL-CAPS headings ("RISK FACTORS"); 10-K/10-Q use "Item 1A."
# style. Each canonical section lists heading patterns that mark its START.
# Order matters: a section runs until the NEXT matched heading of ANY section.
_SECTION_PATTERNS = {
    "Prospectus Summary":       [r"prospectus summary", r"summary of the offering", r"the offering"],
    "Risk Factors":             [r"risk factors", r"item\s*1a[\.\s]"],
    "Use of Proceeds":          [r"use of proceeds"],
    "Dividend Policy":          [r"dividend policy"],
    "Dilution":                 [r"\bdilution\b"],
    "Capitalization":           [r"\bcapitalization\b"],
    "Management Discussion":    [r"management.?s discussion and analysis",
                                 r"item\s*7[\.\s].{0,40}discussion"],
    "Business":                 [r"^business$", r"item\s*1[\.\s]+business", r"our business"],
    "Underwriting":             [r"\bunderwriting\b", r"plan of distribution"],
    "Selected Financial Data":  [r"selected financial data",
                                 r"selected consolidated financial",
                                 r"summary financial data"],
}

# Default sections returned when the caller doesn't narrow it down.
_DEFAULT_SECTIONS = [
    "Prospectus Summary", "Risk Factors", "Use of Proceeds",
    "Dilution", "Management Discussion", "Business", "Underwriting",
]


# ── config / headers ───────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ua() -> str:
    """SEC requires a descriptive User-Agent (name + contact email).

    Precedence: apis.sec.user_agent > built from mason.owner > safe default.
    A missing/blank UA gets throttled or 403'd by SEC, so never send empty.

    2026-09-12: the contact must look like a real address. `mason@localhost`
    was being answered with 403 "Your Request Originates from an Undeclared
    Automated Tool" on every call (verified live) — which silently broke CIK
    resolution, institutional flow, and the 8-K poller. `@mason.local` is
    accepted; set `apis.sec.user_agent` in mason.config.json to a real contact
    email to comply fully with SEC's fair-access policy.
    """
    cfg = _cfg()
    import mason_secrets as _S    # B1b: env SEC_USER_AGENT > secrets file > apis.sec.user_agent
    ua = _S.get("SEC_USER_AGENT", "", cfg=cfg)
    if ua and ua.strip():
        return ua.strip()
    owner = str(cfg.get("mason", {}).get("owner", "") or "").strip()
    if owner:
        return f"Mason Investment Research ({owner}) research@mason.local"
    return "Mason Investment Research research@mason.local"


# url -> short reason its LAST request failed ("HTTP 403", "ReadTimeout"); cleared on success.
# Lets callers that only see None say why (SEC 8-K feed error recording, 2026-11-16).
last_error: dict = {}


def _get(url: str, timeout: int = 15):
    """GET with SEC-compliant headers. Returns response or None (never raises).
    On None, last_error[url] holds the reason."""
    h = {"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"}
    try:
        r = requests.get(url, headers=h, timeout=timeout)
        # SEC politeness: max ~10 req/s. A tiny sleep keeps us well under.
        time.sleep(0.12)
        if r.ok:
            last_error.pop(url, None)
            return r
        last_error[url] = f"HTTP {r.status_code}"
        return None
    except Exception as e:
        last_error[url] = type(e).__name__
        return None


# ── ticker -> CIK resolution ───────────────────────────────────────────────────

def _load_tickers_map() -> dict:
    """Return {TICKER: (cik_int, title)} from SEC's company_tickers.json.

    Cached to disk for a week. Falls back to a stale cache if the network fails.
    """
    # fresh disk cache?
    try:
        if TICKERS_CACHE.exists() and (time.time() - TICKERS_CACHE.stat().st_mtime) < _TICKERS_TTL:
            raw = json.loads(TICKERS_CACHE.read_text(encoding="utf-8"))
            return _parse_tickers_json(raw)
    except Exception:
        pass

    r = _get("https://www.sec.gov/files/company_tickers.json")
    if r is not None:
        try:
            raw = r.json()
            TICKERS_CACHE.write_text(json.dumps(raw), encoding="utf-8")
            return _parse_tickers_json(raw)
        except Exception:
            pass

    # network failed — use whatever stale cache exists
    try:
        if TICKERS_CACHE.exists():
            return _parse_tickers_json(json.loads(TICKERS_CACHE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return {}


def _parse_tickers_json(raw: dict) -> dict:
    """Pure parser (unit-testable). {"0":{cik_str,ticker,title}} -> map."""
    out = {}
    for _, row in (raw or {}).items():
        try:
            tk = str(row["ticker"]).upper().strip()
            out[tk] = (int(row["cik_str"]), str(row.get("title", "")).strip())
        except Exception:
            continue
    return out


def _looks_like_ticker(q: str) -> bool:
    q = q.strip()
    return bool(re.fullmatch(r"[A-Za-z]{1,6}(\.[A-Za-z])?", q))


def resolve_cik(query: str):
    """Resolve a ticker OR company name to (cik_int, title).

    Strategy: exact ticker match -> title substring match -> EDGAR full-text
    company search (covers brand-new IPO filers not yet in the ticker map).
    Returns (None, reason_string) on failure.
    """
    q = (query or "").strip()
    if not q:
        return None, "empty query"

    tmap = _load_tickers_map()

    # 1) exact ticker
    if _looks_like_ticker(q):
        hit = tmap.get(q.upper())
        if hit:
            return hit[0], hit[1]

    # 2) case-insensitive company-title substring (e.g. "Rocket Lab")
    ql = q.lower()
    subs = [(cik, title) for tk, (cik, title) in tmap.items() if ql in title.lower()]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        # prefer the shortest title (usually the parent/primary entity)
        subs.sort(key=lambda x: len(x[1]))
        return subs[0][0], subs[0][1] + f"  (+{len(subs)-1} other name matches)"

    # 3) EDGAR full-text search — catches pre-ticker IPO registrants
    cik = _fulltext_company_search(q)
    if cik:
        return cik, q + "  (via EDGAR full-text search)"

    return None, (f"could not resolve '{query}' to a CIK. Try the exact ticker, or the "
                  f"registrant's legal name as it appears on the cover of the filing.")


def _fulltext_company_search(name: str):
    """Use EDGAR full-text search to find a CIK by company name. Returns int|None."""
    import urllib.parse as _u
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{_u.quote(name)}%22"
    r = _get(url)
    if r is None:
        return None
    try:
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        for h in hits:
            cik = h.get("_source", {}).get("cik") or (h.get("_id", "") or "").split(":")[0]
            if cik:
                return int(str(cik).lstrip("0") or "0")
    except Exception:
        return None
    return None


# ── submissions / filing selection ─────────────────────────────────────────────

def _fetch_submissions(cik_int: int) -> dict:
    """Fetch (memory-cached) the submissions JSON for a CIK. {} on failure."""
    key = int(cik_int)
    now = time.time()
    c = _subs_cache.get(key)
    if c and (now - c["ts"]) < _SUBS_TTL:
        return c["data"]
    url = f"https://data.sec.gov/submissions/CIK{key:010d}.json"
    r = _get(url)
    data = {}
    if r is not None:
        try:
            data = r.json()
        except Exception:
            data = {}
    _subs_cache[key] = {"ts": now, "data": data}
    return data


def _expand_forms(form_type):
    """Normalize form_type arg -> list of accepted forms, or None for 'any'."""
    if form_type is None:
        return _PROSPECTUS_FORMS[:]
    key = str(form_type).strip().lower()
    if key in _FORM_ALIASES:
        return _FORM_ALIASES[key]           # may be None (any) or a list
    # a raw form code like "424B4" or "10-K" — match it (and its /A amendment)
    raw = str(form_type).strip().upper()
    return [raw, raw + "/A"]


def _select_filings(subs: dict, forms, limit: int):
    """Return a list of filing dicts (newest first) matching `forms`.

    forms=None means match every form. Each dict: form, date, accession,
    primary_document, description, cik.
    """
    recent = (subs.get("filings", {}) or {}).get("recent", {}) or {}
    forms_col = recent.get("form", []) or []
    accs      = recent.get("accessionNumber", []) or []
    docs      = recent.get("primaryDocument", []) or []
    dates     = recent.get("filingDate", []) or []
    descs     = recent.get("primaryDocDescription", []) or []
    cik       = int(subs.get("cik", 0) or 0)

    fset = None if forms is None else {f.upper() for f in forms}
    out = []
    for i in range(len(forms_col)):
        f = str(forms_col[i]).upper()
        if fset is not None and f not in fset:
            continue
        out.append({
            "form":        forms_col[i],
            "date":        dates[i] if i < len(dates) else "",
            "accession":   accs[i] if i < len(accs) else "",
            "primary_document": docs[i] if i < len(docs) else "",
            "description": descs[i] if i < len(descs) else "",
            "cik":         cik,
        })
        if len(out) >= limit:
            break
    return out


def _doc_url(cik: int, accession: str, primary_document: str) -> str:
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{primary_document}"


# ── HTML -> text + section extraction ──────────────────────────────────────────

def html_to_text(raw: str) -> str:
    """Strip an EDGAR HTML document down to readable text (pure, testable)."""
    if not raw:
        return ""
    s = raw
    # drop scripts/styles/head wholesale
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
    # tables and block elements -> newlines so headings land on their own lines
    s = re.sub(r"(?i)</(p|div|tr|br|h[1-6]|li|table)>", "\n", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    # remaining tags -> gone
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    # unescape entities (&nbsp; &amp; &#160; etc.)
    s = _htmllib.unescape(s)
    s = s.replace("\xa0", " ")
    # collapse whitespace but keep line structure
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln != "")


def _find_section_starts(text: str, wanted):
    """Return sorted [(pos, section_name)] for every wanted-section heading found.

    A heading matches when one of its patterns appears at/near the start of a line
    (prospectus ALL-CAPS heading or 10-K 'Item 1A.' style). We anchor on line
    starts to avoid matching passing mentions inside body paragraphs.
    """
    starts = []
    for name in wanted:
        for pat in _SECTION_PATTERNS.get(name, []):
            # match the pattern occurring at the beginning of a line
            rx = re.compile(r"(?im)^[\s\|>]{0,6}(" + pat + r")\b.{0,60}$")
            m = rx.search(text)
            if m:
                starts.append((m.start(), name))
                break   # first pattern that hits wins for this section
    starts.sort(key=lambda x: x[0])
    return starts


def extract_sections(text: str, wanted=None, per_section_chars: int = 7000) -> dict:
    """Split filing text into {section_name: text}. Pure + unit-testable.

    Each section runs from its heading to the next detected heading (of any
    known section, so we don't overrun into the next part), capped per section.
    """
    if wanted is None:
        wanted = list(_SECTION_PATTERNS.keys())
    # find starts for BOTH the wanted set and all others (others act as stoppers)
    all_names = list(_SECTION_PATTERNS.keys())
    all_starts = _find_section_starts(text, all_names)
    if not all_starts:
        return {}
    # boundaries = all detected heading positions, sorted
    positions = [p for p, _ in all_starts] + [len(text)]
    result = {}
    for pos, name in all_starts:
        if name not in wanted or name in result:
            continue
        # next boundary strictly after pos
        nxt = min((p for p in positions if p > pos), default=len(text))
        body = text[pos:nxt].strip()
        if len(body) > per_section_chars:
            body = body[:per_section_chars].rstrip() + "\n… [section truncated]"
        result[name] = body
    return result


def _fetch_doc_text(url: str) -> str:
    """Download + strip a filing document (memory-cached). '' on failure."""
    now = time.time()
    c = _doc_cache.get(url)
    if c and (now - c["ts"]) < _DOC_TTL:
        return c["text"]
    r = _get(url, timeout=25)
    text = ""
    if r is not None:
        try:
            raw = r.text
            text = html_to_text(raw) if ("<" in raw and ">" in raw) else raw
        except Exception:
            text = ""
    _doc_cache[url] = {"ts": now, "text": text}
    return text


# ── public entry point ─────────────────────────────────────────────────────────

def _norm_wanted(sections):
    """Map free-text section requests to canonical section names."""
    if not sections:
        return None
    canon = {k.lower(): k for k in _SECTION_PATTERNS}
    aliases = {
        "risks": "Risk Factors", "risk": "Risk Factors",
        "proceeds": "Use of Proceeds",
        "mda": "Management Discussion", "md&a": "Management Discussion",
        "summary": "Prospectus Summary", "offering": "Prospectus Summary",
        "financials": "Selected Financial Data", "financial": "Selected Financial Data",
    }
    out = []
    for s in sections:
        sl = str(s).strip().lower()
        if sl in canon:
            out.append(canon[sl])
        elif sl in aliases:
            out.append(aliases[sl])
        else:
            for k in canon:                 # loose contains
                if sl in k or k in sl:
                    out.append(canon[k]); break
    return out or None


def analyze_filing(query, form_type=None, sections=None, list_only=False,
                   limit=8, max_chars=42000) -> str:
    """Find + extract an SEC filing for `query`, formatted for Mason to reason over.

    query      : ticker ('RKLB') or company/registrant name ('Rocket Lab', 'Reddit')
    form_type  : None -> prospectus/IPO set; or 'S-1','424B4','10-K','10-Q','8-K',
                 or an alias ('prospectus','annual','quarterly','any')
    sections   : optional list to narrow the extract (e.g. ['risk factors','dilution'])
    list_only  : True -> just list recent matching filings (no download/parse)
    limit      : max filings to list (list_only) or to search for a parseable one
    max_chars  : overall cap on returned filing text
    """
    try:
        cik, title = resolve_cik(query)
        if cik is None:
            return f"SEC EDGAR: {title}"

        subs = _fetch_submissions(cik)
        if not subs:
            return (f"SEC EDGAR: resolved '{query}' to CIK {cik} ({title}) but couldn't "
                    f"fetch its filing history (network or SEC rate limit). Try again shortly.")

        forms = _expand_forms(form_type)
        filings = _select_filings(subs, forms, limit=max(limit, 8))
        label = ("any form" if forms is None
                 else "prospectus/IPO forms" if form_type is None
                 else str(form_type))

        if not filings:
            # tell Mason what DOES exist so it can suggest an alternative form
            avail = (subs.get("filings", {}).get("recent", {}) or {}).get("form", [])[:12]
            avail_str = ", ".join(dict.fromkeys(avail)) if avail else "none found"
            return (f"SEC EDGAR: {title} (CIK {cik}) has no {label} on file. "
                    f"Most recent forms available: {avail_str}. "
                    f"Pass form_type to target one of those.")

        header = (f"SEC EDGAR filing search — {title} (CIK {cik})\n"
                  f"Matched {len(filings)} filing(s) for {label}.\n")

        if list_only:
            lines = [header, "Recent matching filings (newest first):"]
            for f in filings[:limit]:
                url = _doc_url(f["cik"], f["accession"], f["primary_document"]) if f["primary_document"] else "(no primary doc)"
                lines.append(f"  • {f['form']:<8} filed {f['date']}  acc {f['accession']}  "
                             f"{f['description']}\n    {url}")
            return "\n".join(lines)

        # find the newest filing that actually has a parseable primary document
        for f in filings:
            if not f["primary_document"]:
                continue
            url = _doc_url(f["cik"], f["accession"], f["primary_document"])
            text = _fetch_doc_text(url)
            if not text or len(text) < 500:
                continue

            wanted = _norm_wanted(sections) or _DEFAULT_SECTIONS
            extracted = extract_sections(text, wanted=wanted)

            out = [header,
                   f"Analyzing: {f['form']} filed {f['date']}  (acc {f['accession']})",
                   f"Document: {url}", ""]

            if not extracted:
                # no headings matched — hand back a capped raw slice so Mason still
                # has substance to work with (unusual layout / scanned exhibit).
                snippet = text[:max_chars].rstrip()
                out.append("Could not detect standard section headings; returning the "
                           "opening text of the filing:\n")
                out.append(snippet)
                return "\n".join(out)

            budget = max_chars
            for name in wanted:
                if name in extracted and budget > 0:
                    body = extracted[name]
                    if len(body) > budget:
                        body = body[:budget].rstrip() + "\n… [truncated — ask to expand this section]"
                    out.append(f"═══ {name.upper()} ═══\n{body}\n")
                    budget -= len(body)
            missing = [n for n in wanted if n not in extracted]
            if missing:
                out.append(f"(Sections not found in this filing: {', '.join(missing)})")
            return "\n".join(out)

        # nothing parseable among the matches
        newest = filings[0]
        url = _doc_url(newest["cik"], newest["accession"], newest["primary_document"]) if newest["primary_document"] else "(no primary doc)"
        return (f"{header}\nFound matching filings but couldn't extract readable text from the "
                f"newest one ({newest['form']} filed {newest['date']}). Primary document:\n{url}\n"
                f"It may be an XBRL-only or image-based exhibit.")
    except Exception as e:
        return f"SEC EDGAR error: {e}"


# ── manual smoke test (run on a machine with internet: `python mason_edgar.py`) ─
if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "RKLB"
    ft = sys.argv[2] if len(sys.argv) > 2 else None
    print(analyze_filing(q, form_type=ft, list_only=("--list" in sys.argv)))
