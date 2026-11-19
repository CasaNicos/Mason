"""
mason_biocal.py — drug decision dates (PDUFA) on the events calendar (news phase 6c, 2026-11-18).

Why: a drug's FDA decision date ("PDUFA target action date of March 25, 2027") is the most
important scheduled event a small drug company has, and it is printed in the company's own news
weeks or months ahead. Mason already stores that news (press wires, Benzinga, Nasdaq/RTTNews,
Seeking Alpha, SEC 8-K excerpts) but nothing read the date out, so the calendar had trial
completion dates (ClinicalTrials.gov, mason_sources) and FDA advisory meetings (Federal Register,
mason_calendar_feeds) but no decision dates. On 18 Nov 2026 the stored news named eleven of them
(PolyPid Nov 28, Capricor Nov 22, Mineralys Dec 22, uniQure May 1 2027, ...).

What it does (no network, no model): one calendar feed, key "biocal", run by mason_calendar.tick
like the other feeds (hourly, its own health record). Each run reads the news rows stored since the
last run (the first run looks back calendar.biocal.lookback_days), finds the PDUFA / target-action
date sentences, works out the company's ticker from the text and writes one calendar row per drug
decision (type "fda", kind "pdufa", source "Company news", created_by "mason_biocal"):
  * ticker: a US exchange tag in the text ("(Nasdaq: UNCY)"), a "(AGIO)" right after the name,
    the row's own stored ticker when the text names that company, or the company named at the start
    of the headline when exactly one SEC-listed company has that distinctive first name ("Mineralys",
    "Capricor"). Nothing is guessed: no ticker, no row.
  * date: a written year is used as is; without one ("Nov. 22 PDUFA date") the next such day after
    the article is taken and the row is marked "date not confirmed". Dates before the article, or
    more than 15 months after it, are dropped. A headline saying the FDA already decided is skipped.
    (Review 2026-11-19) The date must be stated AS the decision date ("PDUFA target action date of
    May 1, 2027", "the December 22 PDUFA date"); a submission target, a quarter end, a conference day,
    "PDUFA VII" or a fee's start date near the word PDUFA is not read.
  * one row per decision: key ticker + drug (ticker + date when the drug is not named). One company on
    one date is one decision even when two texts name the drug differently ("CAP-1002" / "deramiocel");
    a later "new PDUFA goal date" moves the row (the calendar's usual "changed" flag), also when the new
    text uses the drug's other name and the company has just that one upcoming row for it.
  * (Review 2026-11-19) when the FDA decides BEFORE the date (approval or complete response letter in a
    headline that names the drug, within 45 days of the date) the row is marked done: the widget shows it
    as done and no model-facing list or reminder treats it as coming up.
  * confirmed = the date comes from the company's own release (press wire or SEC filing) with a year.
Rows keep the `ticker` COLUMN empty (tickers are in details_json, like every row mason_calendar
writes), so dossiers and the triage +0.05 calendar nudge do not change unless the owner lists "fda"
in calendar.ticker_column_types.

Who sees the rows (mason.config.json → "calendar"):
  feeds.biocal          false   write the rows (the widget then shows them; models still do not)
  hours.biocal          1       how often (hours)
  biocal.lookback_days  30      how far back the first run reads
  biocal.show_models    false   false: the director, quick checks and the chat calendar tool never see
                                these rows; "held": only rows for stocks Nico or Mason hold or watch
                                (and, in a quick check, the stock being checked); "all": every row,
                                like any other importance-2 date. Wording: NEWS6_PROMPTS_FOR_NICO.md.
  biocal.director_block false   true: the morning and midday reviews get a short block "FDA DECISION DATES
                                COMING UP" (the next 14 days, at most 6 lines) — wording in
                                NEWS6_PROMPTS_FOR_NICO.md; off = the director's input is exactly as before
  biocal.notify_days    0       > 0: an inbox note this many days before a drug decision, a
                                trial's completion date or an FDA advisory meeting of a held or
                                watched stock (one note per event and date)
Nothing here places an order, calls a model or reaches the network. Public functions never raise.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import mason_calendar as C
import mason_db

log = logging.getLogger("mason.biocal")
CREATED_BY = "mason_biocal"
SOURCE = "Company news"
FEED_KEY = "biocal"
LABEL = "list of FDA decision dates from company news"   # (review) one noun: "the list ... has not updated"
DEFAULTS = {"show_models": False, "director_block": False, "lookback_days": 30, "notify_days": 0,
            "max_rows_per_run": 4000}
# outlets that publish the company's own words (press wires, SEC filings)
_PRIMARY = {"GlobeNewswire", "PR Newswire", "Business Wire", "SEC 8-K", "Accesswire"}


def bcfg(c: dict | None = None) -> dict:
    """calendar.biocal over DEFAULTS."""
    c = c if c is not None else C.cfg()
    out = dict(DEFAULTS)
    blk = c.get("biocal") if isinstance(c.get("biocal"), dict) else {}
    out.update({k: v for k, v in blk.items() if not str(k).startswith("_")})
    return out


def show_models(c: dict | None = None) -> str:
    """'off' | 'held' | 'all' — how far these rows reach the director, quick checks and chat."""
    v = bcfg(c).get("show_models")
    if v is True or str(v).lower() == "all":
        return "all"
    if str(v).lower() == "held":
        return "held"
    return "off"


# ── reading a date out of text ──────────────────────────────────────────────────
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_MON = (r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE = _MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\b"
# (review 2026-11-19) The date must BE the decision date the sentence names, not any date near the word PDUFA (a
# submission target, a quarter end, a conference day, "PDUFA VII", a fee's start date, a resubmission date):
#  * after the cue: "PDUFA / Prescription Drug User Fee Act (PDUFA) [target|goal] [action] date", "target action
#    date", "FDA goal date", then only "for <drug>" and linking words (of, is, set for, extended to, ...) before it;
#  * before the cue: the date, at most three words that name the drug or its use ("infant AD"), then "PDUFA",
#    "target action date" or "FDA goal date"; a word that starts another clause (and, ahead, to, which, by, ...) breaks it.
_CUE_AFTER = (r"(?:PDUFA|Prescription\s+Drug\s+User\s+Fee\s+Act)(?:\s*\(PDUFA\))?(?:\s+(?:target|goal))?(?:\s+action)?"
              r"(?:\s+goal)?\s+date|(?:FDA\s+)?target\s+action\s+date|(?:FDA|user[- ]fee)\s+goal\s+date")
_LINK = (r"(?:\s*\([^()]{0,24}\))?(?:\s+for\s+[^,.;:!?()]{1,50}?)?(?:\s*(?:,|:|-|–)?\s*(?:of|is|was|are|on|at|to|"
         r"now|remains|set\s+(?:for|as|at|to)|(?:has\s+been\s+|have\s+been\s+|was\s+|been\s+)?(?:extended|changed|moved|"
         r"revised|pushed|reset)\s+to|(?:is\s+|was\s+)?(?:scheduled|expected)\s+(?:for|on))\b){0,2}\s*[,:\-–]?\s*")
_AFTER_CUE = re.compile(r"\b(?:" + _CUE_AFTER + r")" + _LINK + _DATE, re.I)
_CLAUSE = (r"(?:and|or|but|which|that|who|to|ahead|before|after|following|by|its|their|his|her|our|will|would|could|"
           r"may|might|plans?|expects?|anticipat\w*|discuss\w*|prepar\w*|set|sets|at|on|in|of|from|with|for|the|a|an|"
           r"new|meeting|conference|data|topline|submission|submit\w*|resubmi\w*|present\w*|host\w*)")
_BEFORE_CUE = re.compile(r"\b" + _DATE + r"(?:\s+(?!" + _CLAUSE + r"\b)[\w’'+/-]+){0,3}\s+(?:PDUFA|(?:FDA\s+)?target\s+"
                         r"action\s+date|FDA\s+goal\s+date)\b", re.I)
_DONE_RE = re.compile(r"\b(?:FDA )?approv(?:es|ed)\b|\breceives? (?:FDA |full |accelerated )?approval\b|"
                      r"\bgrant(?:s|ed) (?:full |accelerated |traditional )?approval\b|\bcomplete response letter\b|"
                      r"\bCRL\b", re.I)
# (review 2026-11-19) "FDA-approved" as an adjective ("Acme's FDA-Approved X Gets New PDUFA Date") is not a decision
_APPROVED_ADJ_RE = re.compile(r"\b(?:FDA|EMA)-(?:approved|cleared|authori[sz]ed)\b", re.I)


def _decided(headline: str) -> bool:
    """Does the headline say the FDA already decided (approval, complete response letter)?"""
    return bool(_DONE_RE.search(_APPROVED_ADJ_RE.sub(" ", str(headline or ""))))


_MOVE_RE = re.compile(r"\b(?:extend\w*|extension|new (?:\w+ ){0,2}(?:PDUFA|goal|target|action)|revised|moved|delay\w*|"
                      r"push\w* (?:back|out))\b", re.I)
_INN = (r"(?:mab|nib|tide|stat|vir|mide|cept|vec|platin|lisib|gene|cel|vat|sertib|parin|setron|tidine|prazole|"
        r"triptan|lukast|afil|semide|vastatin|sartan|pril|olol|azole|cycline|mycin|oxacin|dipine|plase|kin|tinib|"
        r"ciclib|degib|rafenib|zomib|lutamide|parib|tegravir|previr|buvir|fungin|gliflozin|gliptin|glutide|lukin|"
        r"rsen|siran|stim|ase)")
_CODE = r"[A-Z]{2,6}-\d{2,5}[A-Za-z]?"
_DRUG_PATTERNS = [
    # "NDA for oxylanthanum carbonate (OLC)", "sNDA for REVTORPYK™ (gedatolisib)", "BLA for ifezuntirgene inilparvovec"
    re.compile(r"\b(?:NDA|BLA|sNDA|sBLA|MAA|New Drug Application|Biologics License Application|application|"
               r"resubmission|submission)\s*(?:\((?:NDA|BLA|sNDA|sBLA)\)\s*)?(?:for|of|seeking approval of)\s+"
               r"(?:its |the )?(?!(?:New|Biologics|Drug|Supplemental|Marketing|Abbreviated)\b)((?:" + _CODE + r")|"
               r"[A-Za-z][\w\-®™₀-₉]*(?:\s(?!for\b|in\b|to\b|as\b|with\b|and\b|"
               r"the\b)[a-z][\w\-]{3,})?)"),
    # "Deramiocel BLA", "D-PLEX₁₀₀ NDA", "AXS-12 NDA"
    re.compile(r"\b((?:" + _CODE + r")|[A-Z][\w\-₀-₉®™]{2,})\s+(?:NDA|BLA|sNDA|sBLA)\b"),
    # "... target action date for lorundrostat"
    re.compile(r"\b(?:PDUFA|action date|goal date)[^.;]{0,40}?\bfor\s+((?:" + _CODE + r")|[a-z]{4,}" + _INN + r")\b",
               re.I),
]
_DRUG_STOP = {"the", "its", "a", "an", "approval", "review", "priority", "drug", "company", "fda", "u.s.", "pdufa",
              "treatment", "patients", "use", "marketing", "standard", "resubmission", "acceptance", "nda", "bla",
              "application", "new", "biologics", "license"}


_SUBSCRIPT = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def _norm_drug(s) -> str | None:
    s = re.sub(r"[®™]", "", str(s or "")).translate(_SUBSCRIPT).strip(" .,;:-")
    if not s or s.lower() in _DRUG_STOP or len(s) < 3:
        return None
    if re.fullmatch(r"[A-Z][a-z]{3,}" + _INN, s):
        s = s.lower()                      # a generic name written in a Title Case headline: "Mitapivat" -> "mitapivat"
    return s[:60]


def _owner(name: str) -> str:
    """'PolyPid' -> "PolyPid's", 'Mineralys Therapeutics' -> "Mineralys Therapeutics'"."""
    return f"{name}'" if name.endswith("s") else f"{name}'s"


_ANY_DATE_RE = re.compile(_DATE, re.I)


def _find_drug(text: str, pos: int) -> str | None:
    """The drug the PDUFA sentence whose date starts at `pos` is about, or None (nearest mention wins)."""
    best = None
    for rx in _DRUG_PATTERNS:
        for m in rx.finditer(text):
            d = _norm_drug(m.group(1))
            if not d:
                continue
            dist = abs(m.start() - pos)
            if dist <= 220 and (best is None or dist < best[0]):
                best = (dist, d)
    if best is None:
        # "BBP-418 November 27 PDUFA", "as AXS-12 nears May 1, 2027 PDUFA": the last code or INN name
        # in the few words before the date, never reaching back past another date
        win = text[max(0, pos - 45):pos]
        cut = [m.end() for m in _ANY_DATE_RE.finditer(win)]
        win = win[cut[-1]:] if cut else win
        hits = re.findall(r"((?:" + _CODE + r")|\b[a-z]{4,}" + _INN + r"\b)", win, re.I)
        if hits:
            return _norm_drug(hits[-1])
    return best[1] if best else None


def _mk_date(mon: str, day: str, year: str | None, pub: date) -> tuple:
    """(date|None, year_given)."""
    try:
        m = _MONTHS[mon.lower()[:3]]
        d = int(day)
        if year:
            return date(int(year), m, d), True
        cand = date(pub.year, m, d)
        if cand < pub - timedelta(days=7):
            cand = date(pub.year + 1, m, d)
        return cand, False
    except (KeyError, ValueError):
        return None, False


def extract(text: str, published: date) -> list:
    """[{date, year_given, drug, pos, sentence, moved_word}] for every PDUFA date sentence in `text`.
    Dates before `published` or more than 450 days after it are dropped. Never raises."""
    out, seen = [], set()
    try:
        t = " ".join(str(text or "").replace(" ", " ").split())
        owned = []                                  # cue words already claimed by the date just before them
        for rx in (_BEFORE_CUE, _AFTER_CUE):
            for m in rx.finditer(t):
                g = m.groups()
                mon, day, yr = g[-3], g[-2], g[-1]
                if rx is _BEFORE_CUE:
                    owned.append((m.start(), m.end()))
                elif any(a <= m.start() < b for a, b in owned):
                    continue        # "November 27 PDUFA and infigratinib February 4": that PDUFA is November's
                d, given = _mk_date(mon, day, yr, published)
                if d is None or d < published or d > published + timedelta(days=450):
                    continue
                pos = m.start(len(g) - 2)           # where the date itself starts (month group)
                drug = _find_drug(t, pos)
                k = (d, (drug or "").lower())
                if k in seen:
                    continue
                seen.add(k)
                a, b = max(0, pos - 120), min(len(t), m.end() + 80)
                out.append({"date": d.isoformat(), "year_given": given, "drug": drug, "pos": pos,
                            "sentence": t[a:b].strip(), "moved_word": bool(_MOVE_RE.search(t[max(0, pos - 160):m.end()]))})
        # a mention without a drug name on the same date as one with it is the same decision
        named = {e["date"] for e in out if e["drug"]}
        out = [e for e in out if e["drug"] or e["date"] not in named]
        # one date per drug per text: when a drug has two dates here, the one that says it is new wins
        best = {}
        for e in out:
            key = (e["drug"] or "").lower() or e["date"]
            if key not in best or (e["moved_word"] and not best[key]["moved_word"]):
                best[key] = e
        return sorted(best.values(), key=lambda e: e["pos"])
    except Exception as e:
        log.debug(f"[biocal] extract failed: {e}")
        return []


# ── whose date is it ────────────────────────────────────────────────────────────
_US_TAG_RE = re.compile(r"\([^()]{0,40}?\b(?:NASDAQ|Nasdaq|NYSE(?: American| Arca)?|NYSEAMERICAN|AMEX|Cboe(?: BZX)?)"
                        r"\s*[:：]\s*([A-Z]{1,5}(?:\.[A-Z])?)\s*(?=[,;)])")
_PAREN_RE = re.compile(r"[A-Za-z.,']\s\(([A-Z]{2,5})\)")
_first_tok = {"key": None, "idx": {}}


def _known(sym: str) -> bool:
    try:
        import mason_symbols
        return bool(mason_symbols.known(sym))
    except Exception:
        return False


def _first_token_index() -> dict:
    """{distinctive first word of an SEC company name: ticker} for names whose first word belongs to
    exactly one listed company and is not an everyday word ('mineralys' → MLYS; 'american' → none)."""
    try:
        import mason_triage as T
        k = T.EDGAR_CACHE.stat().st_mtime
    except Exception:
        return {}
    if _first_tok["key"] == k:
        return _first_tok["idx"]
    idx, bad = {}, set()
    try:
        lex = T._lexicon()
    except Exception:
        lex = set()
    try:
        seen_cik = set()
        raw = json.loads(T.EDGAR_CACHE.read_text(encoding="utf-8"))
        rows = raw.values() if isinstance(raw, dict) else raw
        for r in rows:
            try:
                cik, tk, title = r.get("cik_str"), str(r["ticker"]).upper(), str(r.get("title") or "")
            except Exception:
                continue
            if cik in seen_cik or "-" in tk:
                continue
            seen_cik.add(cik)
            w = re.sub(r"[^a-z0-9]", "", (title.split() or [""])[0].lower())
            if len(w) < 5 or w in lex or w in T._AMBIGUOUS_BASE:
                continue
            if w in idx and idx[w] != tk:
                bad.add(w)
            idx.setdefault(w, tk)
        for w in bad:
            idx.pop(w, None)
    except Exception:
        idx = {}
    _first_tok.update(key=k, idx=idx)
    return idx


def _lead_company(headline: str) -> str | None:
    """The ticker of the company named first in the headline, by its distinctive first word."""
    m = re.match(r"\s*([A-Z][A-Za-z0-9]{4,})\b", headline or "")
    if not m:
        return None
    tk = _first_token_index().get(m.group(1).lower())
    return tk if tk and _known(tk) else None


def ticker_for(headline: str, desc: str, stored) -> str | None:
    """The listed company a PDUFA text is about, derived from the text only. None when not sure."""
    text = f"{headline or ''} {desc or ''}"
    for m in _US_TAG_RE.finditer(text):
        if _known(m.group(1)):
            return m.group(1)
    for m in _PAREN_RE.finditer(text):
        # "Agios Pharmaceuticals (AGIO)" counts; a drug's short name in brackets ("... carbonate (OLC)", "(AMT)")
        # that happens to spell some listed ticker does not: the text must also name that ticker's company
        if _known(m.group(1)) and _issuer_named(m.group(1), text, name_only=True):
            return m.group(1)
    st = [str(t).upper() for t in (stored or []) if t]
    if len(st) == 1 and _issuer_named(st[0], text):
        return st[0]
    return _lead_company(headline)


def _issuer_named(sym: str, text: str, *, name_only: bool = False) -> bool:
    """mason_symbols.issuer_named; name_only=True first removes the symbol itself from the text, so only the
    company's own name counts as evidence (the bracketed symbol is what is being checked)."""
    try:
        import mason_symbols
        if name_only:
            text = re.sub(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", " ", text or "")
        return bool(mason_symbols.issuer_named(sym, text))
    except Exception:
        return False


# ── the feed ────────────────────────────────────────────────────────────────────
_PREFILTER = ("%PDUFA%", "%action date%", "%goal date%", "%User Fee%")


def _news_rows(conn, since: float, limit: int) -> list:
    like = " OR ".join(["headline LIKE ?", "raw LIKE ?"] * len(_PREFILTER))
    args = []
    for p in _PREFILTER:
        args += [p, p]
    return [dict(r) for r in conn.execute(
        f"SELECT id, headline, source, url, tickers, raw, published_at, ingested_at FROM news_events "
        f"WHERE ingested_at > ? AND COALESCE(is_deleted,0)=0 AND ({like}) ORDER BY ingested_at LIMIT ?",
        [since] + args + [int(limit)]).fetchall()]


def _short_company(tk: str) -> str:
    try:
        import mason_factcheck as FC
        t = FC.load_ticker_titles().get(tk) or ""
    except Exception:
        t = ""
    t = re.sub(r"(?i),?\s+(inc|corp|corporation|ltd|limited|plc|n\.?v|s\.?a|ag|co|holdings?|group)\.?$", "", t.strip())
    return (t.title() if t.isupper() else t) or tk


def events_from_rows(rows: list, today: date) -> list:
    """Calendar events (mason_calendar.upsert_event shape) from news rows. Pure; no database."""
    out = {}
    for r in rows:
        try:
            raw = json.loads(r.get("raw") or "{}")
        except Exception:
            raw = {}
        h = str(r.get("headline") or "")
        desc = str(raw.get("desc") or "")
        if _decided(h):
            continue                                   # the FDA already decided: the date is history
        pub_ts = r.get("published_at") or r.get("ingested_at") or time.time()
        pub = C.today_et(float(pub_ts))
        found = extract(f"{h}. {desc}", pub)
        if not found:
            continue
        try:
            stored = json.loads(r.get("tickers") or "[]")
        except Exception:
            stored = []
        tk = ticker_for(h, desc, stored)
        if not tk:
            continue
        src = str(r.get("source") or "")
        primary = src in _PRIMARY
        for f in found:
            if f["date"] < today.isoformat():
                continue
            drug = f["drug"]
            key = f"pdufa|{tk}|{(drug or '').lower()}" if drug else f"pdufa|{tk}|date:{f['date']}"
            confirmed = bool(f["year_given"] and primary)
            ev = out.get(key)
            better = ev is None or (confirmed and not ev["confirmed"]) or (
                f["moved_word"] and (pub_ts or 0) >= ev["details"]["_ts"])
            if ev is not None and not better:
                ev["details"]["news_ids"] = (ev["details"]["news_ids"] + [r["id"]])[-8:]
                if src not in ev["details"]["sources"]:
                    ev["details"]["sources"].append(src)
                continue
            co = _short_company(tk)
            prev = ev["details"] if ev else {}
            out[key] = {
                "type": "fda", "kind": "pdufa", "key": key, "date": f["date"],
                "title": f"FDA decision due (PDUFA): {drug} — {co}" if drug else f"FDA decision due (PDUFA) — {co}",
                "subtitle": "", "tickers": [tk], "confirmed": confirmed or bool(ev and ev["confirmed"]),
                "importance": 2, "url": r.get("url") or None,
                "details": {"drug": drug, "company": co, "sentence": f["sentence"][:300],
                            "year_given": f["year_given"] or bool(prev.get("year_given")),
                            "from_company": primary or bool(prev.get("from_company")),
                            "news_ids": (prev.get("news_ids", []) + [r["id"]])[-8:],
                            "sources": sorted(set(prev.get("sources", [])) | {src}), "_ts": pub_ts,
                            "said_on": pub.isoformat(), "says_new_date": f["moved_word"]}}
        # (no network: everything above is the stored text)
    # one company, one date: one decision (review 2026-11-19: also when two texts name the drug differently,
    # "CAP-1002" / "deramiocel", "PYRUKYND" / "mitapivat"). The row that keeps the key: a named drug first, then the
    # company's own release; the other names are kept in details.also_named.
    by_day = {}
    for k, ev in out.items():
        by_day.setdefault((ev["tickers"][0], ev["date"]), []).append(k)
    for keys in by_day.values():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda k: (not out[k]["details"].get("drug"), not out[k]["details"].get("from_company"),
                                 not out[k]["confirmed"], k))
        ev = out[keys[0]]
        for k in keys[1:]:
            gone = out.pop(k)
            ev["details"]["sources"] = sorted(set(ev["details"]["sources"]) | set(gone["details"]["sources"]))
            ev["details"]["news_ids"] = (ev["details"]["news_ids"] + gone["details"]["news_ids"])[-8:]
            ev["confirmed"] = ev["confirmed"] or gone["confirmed"]
            ev["details"]["from_company"] = ev["details"]["from_company"] or gone["details"]["from_company"]
            ev["details"]["year_given"] = ev["details"]["year_given"] or gone["details"]["year_given"]
            other = gone["details"].get("drug")
            if other and other.lower() != str(ev["details"].get("drug") or "").lower():
                ev["details"]["also_named"] = sorted(set(ev["details"].get("also_named") or []) | {other})
    for ev in out.values():
        ev["details"].pop("_ts", None)
        ev["subtitle"] = _subtitle(ev)
    return list(out.values())


def _subtitle(ev: dict) -> str:
    d = ev["details"]
    what = d.get("drug") or "its drug"
    co = d.get("company") or (ev.get("tickers") or ["the company"])[0]
    if d.get("from_company"):
        s = f"The day the FDA has said it will decide on {what}, from {_owner(co)} own news release"
    else:
        s = f"The day the FDA has said it will decide on {what}, as reported by {', '.join(d.get('sources') or []) or 'the news'}"
    if not d.get("year_given"):
        s += " (no year was given, so this is the next such date)"
    return s[:200] + "."


def _merge_with_stored(conn, ev: dict) -> dict:
    """Keep what earlier runs learned: sources and news ids add up; a date the company gave keeps
    its place unless the new text says the date changed."""
    cid = C.event_id(SOURCE, C._natural_key(dict(ev, source=SOURCE)))
    row = conn.execute("SELECT event_date, details_json, record_deleted FROM calendar_events WHERE id=?",
                       (cid,)).fetchone()
    if row is None:
        return ev
    det = C._loads(row["details_json"])
    ev = dict(ev)
    ev["details"] = dict(ev["details"])
    ev["details"]["sources"] = sorted(set(det.get("sources") or []) | set(ev["details"].get("sources") or []))
    ev["details"]["news_ids"] = list(dict.fromkeys((det.get("news_ids") or []) + ev["details"]["news_ids"]))[-8:]
    if row["event_date"] != ev["date"] and det.get("confirmed") and not ev["details"].get("says_new_date"):
        ev["details"]["other_date_seen"] = ev["date"]       # a stray mention does not move a company's date
        ev["date"] = row["event_date"]
    if row["event_date"] == ev["date"]:
        # the same date again: what an earlier mention established stays (a later news report does not
        # undo the company's own confirmation)
        ev["confirmed"] = bool(ev.get("confirmed") or det.get("confirmed"))
        for k in ("from_company", "year_given"):
            ev["details"][k] = bool(ev["details"].get(k) or det.get(k))
    ev["subtitle"] = _subtitle(ev)
    return ev


# ── (review 2026-11-19) one decision = one row, and a decision that came early leaves "due" ─────────────────────
def _stored_upcoming(conn, ticker: str, today: date) -> list:
    """This module's upcoming (not done, not removed) rows for one ticker: [{id, date, det}]."""
    out = []
    for r in conn.execute("SELECT id, event_date, details_json FROM calendar_events WHERE created_by=? AND "
                          "record_deleted=0 AND event_date >= ?", (CREATED_BY, today.isoformat())).fetchall():
        det = C._loads(r["details_json"])
        if ticker in (det.get("tickers") or []) and not det.get("done"):
            out.append({"id": r["id"], "date": r["event_date"], "det": det})
    return out


def _key_for(ticker: str, det: dict, day: str) -> str:
    drug = det.get("drug")
    return f"pdufa|{ticker}|{drug.lower()}" if drug else f"pdufa|{ticker}|date:{day}"


def _fold_same_day(conn, ev: dict) -> dict:
    """A stored row of the same company on the same date under another key (another name of the drug, or no name)
    is the same decision: fold it into this one. When the stored row came from the company's own release and this
    mention did not, this mention joins the stored row instead (its key, drug name and title stay)."""
    tk, day = ev["tickers"][0], ev["date"]
    my_id = C.event_id(SOURCE, ev["key"])
    for r in conn.execute("SELECT id, details_json FROM calendar_events WHERE created_by=? AND record_deleted=0 "
                          "AND event_date=? AND id<>?", (CREATED_BY, day, my_id)).fetchall():
        od = C._loads(r["details_json"])
        if tk not in (od.get("tickers") or []) or od.get("done"):
            continue
        ev = dict(ev, details=dict(ev["details"]))
        old_key = _key_for(tk, od, day)
        keep_old = (C.event_id(SOURCE, old_key) == r["id"] and od.get("from_company")
                    and not ev["details"].get("from_company") and (od.get("drug") or not ev["details"].get("drug")))
        mine_drug, old_drug = ev["details"].get("drug"), od.get("drug")
        if keep_old:
            ev.update(key=old_key, title=f"FDA decision due (PDUFA): {old_drug} — {ev['details']['company']}" if old_drug
                      else f"FDA decision due (PDUFA) — {ev['details']['company']}")
            ev["details"]["drug"] = old_drug
        ev["confirmed"] = bool(ev["confirmed"] or od.get("confirmed"))
        for k in ("from_company", "year_given"):
            ev["details"][k] = bool(ev["details"].get(k) or od.get(k))
        ev["details"]["sources"] = sorted(set(ev["details"]["sources"]) | set(od.get("sources") or []))
        ev["details"]["news_ids"] = list(dict.fromkeys((od.get("news_ids") or []) + ev["details"]["news_ids"]))[-8:]
        names = set(ev["details"].get("also_named") or []) | set(od.get("also_named") or [])
        for n in (mine_drug, old_drug):
            if n and n.lower() != str(ev["details"].get("drug") or "").lower():
                names.add(n)
        if names:
            ev["details"]["also_named"] = sorted(names)
        ev["subtitle"] = _subtitle(ev)
        if not keep_old:
            C.soft_delete(conn, r["id"], "feed:merged")
        else:
            my_id = r["id"]
    return ev


def _names_match(a: dict, b: dict) -> bool:
    """Do two decision records name the same drug? True when either names none, or one's drug name (or another
    name it was seen under) appears in the other's title or sentence."""
    def names(d):
        return {str(x).lower() for x in [d.get("drug")] + list(d.get("also_named") or []) if x}
    def text(d, title=""):
        return f"{title} {d.get('sentence') or ''}".lower().translate(_SUBSCRIPT)
    na, nb = names(a["det"]), names(b["det"])
    if not na or not nb:
        return True
    return any(n in text(b["det"], b.get("title", "")) for n in na) or any(n in text(a["det"], a.get("title", "")) for n in nb)


def _carry_moved_date(conn, ev: dict, today: date) -> dict:
    """A text that says the decision date is NEW ("extended", "new PDUFA goal date") and names it under a key with no
    stored row: when the company has exactly one other upcoming row for the same drug, that row's date moved. The old
    row goes and this one is written with the old date as its previous date (the calendar's usual 'moved' note)."""
    if not ev["details"].get("says_new_date"):
        return ev
    if conn.execute("SELECT 1 FROM calendar_events WHERE id=?", (C.event_id(SOURCE, ev["key"]),)).fetchone():
        return ev                                   # the same key is stored: _merge_with_stored moves it
    others = [o for o in _stored_upcoming(conn, ev["tickers"][0], today) if o["date"] != ev["date"]]
    me = {"det": dict(ev["details"]), "title": ev["title"]}
    if len(others) != 1 or not _names_match(me, dict(others[0], title=others[0]["det"].get("drug") or "")):
        return ev
    old = others[0]
    C.soft_delete(conn, old["id"], "feed:moved")
    ev = dict(ev, details=dict(ev["details"]), _prev_date=old["date"])
    ev["details"]["sources"] = sorted(set(ev["details"]["sources"]) | set(old["det"].get("sources") or []))
    ev["details"]["news_ids"] = list(dict.fromkeys((old["det"].get("news_ids") or []) + ev["details"]["news_ids"]))[-8:]
    if old["det"].get("drug") and old["det"]["drug"].lower() != str(ev["details"].get("drug") or "").lower():
        ev["details"]["also_named"] = sorted(set(ev["details"].get("also_named") or []) | {old["det"]["drug"]})
    return ev


def decisions_from_rows(rows: list) -> list:
    """News rows saying the FDA already decided (approval, complete response letter): [(ticker, text, day)]. Pure."""
    out = []
    for r in rows:
        h = str(r.get("headline") or "")
        if not _decided(h):
            continue
        try:
            raw = json.loads(r.get("raw") or "{}")
        except Exception:
            raw = {}
        desc = str(raw.get("desc") or "")
        try:
            stored = json.loads(r.get("tickers") or "[]")
        except Exception:
            stored = []
        tk = ticker_for(h, desc, stored)
        if tk:
            day = C.today_et(float(r.get("published_at") or r.get("ingested_at") or time.time()))
            out.append((tk, f"{h}. {desc}", day))
    return out


def _close_decided(conn, decided: list, today: date) -> int:
    """The FDA decided before the date: that company's upcoming row for the drug the news names is marked done (the
    widget shows it as done; the models no longer read it as due). Only within 45 days before the date (an approval
    months earlier is another application), and only when the news names the row's drug (or the row has no drug name
    and is the company's only upcoming one)."""
    n = 0
    for tk, text, day in decided:
        low = text.lower().translate(_SUBSCRIPT)
        rows = _stored_upcoming(conn, tk, today)
        for o in rows:
            d = C.parse_date(o["date"])
            if d is None or (d - day).days > 45 or d < day:
                continue
            names = [x for x in [o["det"].get("drug")] + list(o["det"].get("also_named") or []) if x]
            if names and not any(x.lower().translate(_SUBSCRIPT) in low for x in names):
                continue
            if not names and len(rows) != 1:
                continue
            det = dict(o["det"], done=True, done_on=day.isoformat(),
                       note=f"The FDA decided on {day.isoformat()}, before this date (news: {text[:140]})")
            conn.execute("UPDATE calendar_events SET details_json=?, status='done', last_updated=? WHERE id=?",
                         (C._dumps(det), time.time(), o["id"]))
            n += 1
    return n


def _upcoming_for_book(conn, today: date, days: int, bk: dict) -> list:
    """Drug decisions, trial completion dates and FDA advisory meetings of held / watched stocks
    dated today .. today+days: [(id, date, title, tickers, kind)]."""
    mine = set(bk.get("held") or set()) | set(bk.get("watched") or set())
    if not mine or days <= 0:
        return []
    end = (today + timedelta(days=days)).isoformat()
    try:                       # one trial rarely moves an S&P 500 / Nasdaq-100 drug maker: no reminder for those
        import mason_triage as T
        broad = {t for t, m in T.broad_universe(allow_refresh=False).items()
                 if set((m or {}).get("index") or []) & {"SP500", "NDX"}}      # the index members, not the book
    except Exception:
        broad = set()
    out = []
    for r in conn.execute(
            "SELECT id, event_date, title, ticker, event_type, details_json FROM calendar_events WHERE record_deleted=0 "
            "AND event_date >= ? AND event_date <= ? AND (event_type IN ('trial_completion','pdufa','adcom') OR "
            "(event_type='cal_fda' AND json_extract(details_json,'$.kind') IN ('pdufa','adcomm')))",
            (today.isoformat(), end)).fetchall():
        det = C._loads(r["details_json"])
        if det.get("done"):
            continue                   # (review) already decided or done: no "coming up" reminder
        tks = C._clean_tickers(det.get("tickers")) or C._clean_tickers([r["ticker"]])
        if r["event_type"] == "trial_completion" and set(tks) & broad:
            continue
        if set(tks) & mine:
            out.append((r["id"], r["event_date"], r["title"], tks, det.get("kind") or r["event_type"]))
    return out


def _note_soon(conn, today: date, c: dict, notifier=None) -> int:
    days = int(bcfg(c).get("notify_days") or 0)
    if days <= 0:
        return 0
    bk = C.book(conn)
    n = 0
    for cid, d, title, tks, kind in _upcoming_for_book(conn, today, days, bk):
        dd = C.parse_date(d)
        left = (dd - today).days if dd else None
        when = "today" if left == 0 else ("tomorrow" if left == 1 else dd.strftime("%A %b %-d"))
        if kind == "trial_completion":
            body = (f"{title}\nThis is the trial's main completion date in the registry, the sponsor's own estimate. "
                    f"Results usually come out some weeks after it, and the date often slips.")
        elif kind in ("adcom", "adcomm"):
            body = f"{title}\nOutside experts meet to advise the FDA; their vote often decides an approval."
        else:
            body = f"{title}\nThe FDA's decision is due {when}. It can come a little early, and the date can be extended."
        head = f"Coming up {when}: {title}"
        C._notify(head[:200], body, dedup=f"biocal_soon:{cid}:{d}", severity="info",
                  ticker=(tks[0] if len(tks) == 1 else None), notifier=notifier)
        n += 1
    return n


def feed(today: date, c: dict, *, notifier=None) -> dict:
    """The calendar feed (mason_calendar._feed_table key 'biocal'). Reads stored news only. Returns
    the usual counts; `moved` lists dates that changed (reported to the inbox only for held / watched
    stocks: their importance is given as 1 there)."""
    b = bcfg(c)
    started = time.time()
    limit = int(b.get("max_rows_per_run") or 4000)
    conn = mason_db.get_connection()
    try:
        st = C.feed_state(conn, FEED_KEY)
        since = float(st.get("watermark") or 0) or (started - float(b.get("lookback_days") or 30) * 86400)
        rows = _news_rows(conn, max(0.0, since - 300), limit)
    finally:
        conn.close()
    evs = events_from_rows(rows, today)
    decided = decisions_from_rows(rows)
    res = {"ok": True, "n": 0, "new": 0, "moved": [], "changed": 0, "same": 0, "hidden": 0, "seen": len(rows),
           "decided": 0}

    def _write(conn):
        for ev in evs:
            ev = _fold_same_day(conn, ev)            # (review) the same decision stored under another name
            ev = _carry_moved_date(conn, ev, today)  # (review) "new date" for the company's one other upcoming row
            ev = _merge_with_stored(conn, ev)
            state, cid = C.upsert_event(conn, dict(ev, source=SOURCE), today=today, created_by=CREATED_BY, c=c)
            res["n"] += 1
            if state == "moved":
                det = C._loads(conn.execute("SELECT details_json FROM calendar_events WHERE id=?", (cid,)).fetchone()[0])
                res["moved"].append({"id": cid, "title": ev["title"], "from": det.get("prev_date"), "to": ev["date"],
                                     "importance": 1, "tickers": ev["tickers"]})
            elif state in res:
                res[state] += 1
        res["decided"] = _close_decided(conn, decided, today)   # (review) the FDA decided before the date
        # the next run starts where this one stopped reading: after the newest row read when the run was cut at
        # max_rows_per_run, otherwise ten minutes before this run began (rows written while it ran are read
        # again next time; a quiet spell does not make the next run read everything since the last PDUFA mention)
        s = C.feed_state(conn, FEED_KEY)
        newest = max((float(r["ingested_at"] or 0) for r in rows), default=0.0)
        wm = newest if len(rows) >= limit else max(newest, started - 600)
        s["watermark"] = max(float(s.get("watermark") or 0), wm)
        C._snap_put(conn, C._SNAP_SOURCE, FEED_KEY, s)

    conn = mason_db.get_connection()
    try:
        with conn:
            _write(conn)
        res["parts"] = {"drug_dates": len(evs), "notes": _note_soon(conn, today, c, notifier=notifier)}
    finally:
        conn.close()
    return res


def coming_items(items: list, c: dict | None = None, *, mine=(), bk: dict | None = None) -> list:
    """Filter calendar API items for a MODEL-facing reader (mason_calendar.calendar_lines and the chat
    tool): drug-date rows written here are dropped while calendar.biocal.show_models is off; with
    "held" only those touching a held / watched stock or one of `mine` stay. Every other row passes
    untouched, so with the switch off the output is exactly what it was before this module."""
    mode = show_models(c)
    if mode == "all":
        return items
    keep = []
    mine = set(mine or ())
    for it in items:
        if it.get("created_by") != CREATED_BY:
            keep.append(it)
            continue
        if mode == "held" and (it.get("held") or it.get("watched") or (mine & set(it.get("tickers") or []))):
            keep.append(it)
    return keep


# ── the director's block (calendar.biocal.director_block, off by default) ───────
DIRECTOR_TITLE = "FDA DECISION DATES COMING UP (next two weeks; the FDA can decide a little early or push a date back)"


def director_lines(now=None, *, days: int = 14, max_lines: int = 6, c: dict | None = None) -> list:
    """Drug decision dates of the next `days` days, one short line each, soonest first; when there are more
    than max_lines, held / watched stocks and dates the company gave are kept first. [] while
    calendar.biocal.director_block is off, or when nothing is due. Never raises."""
    try:
        c = c if c is not None else C.cfg()
        if bcfg(c).get("director_block") is not True:
            return []
        today = C.today_et(now)
        items = [i for i in C.list_events(today, today + timedelta(days=days), types=["fda"], include_done=False,
                                          today=today) if i.get("created_by") == CREATED_BY]
        if len(items) > max_lines:
            keep = sorted(items, key=lambda i: (not (i["held"] or i["watched"]), not i["confirmed"], i["date"]))[:max_lines]
            ids = {i["id"] for i in keep}
            items = [i for i in items if i["id"] in ids]
        det = {}
        if items:
            conn = mason_db.get_connection()
            try:
                qs = ",".join("?" * len(items))
                det = {r["id"]: C._loads(r["details_json"]) for r in conn.execute(
                    f"SELECT id, details_json FROM calendar_events WHERE id IN ({qs})", [i["id"] for i in items])}
            finally:
                conn.close()
        out = []
        for i in items:
            d = C.parse_date(i["date"])
            day = "today" if d == today else ("tomorrow" if d == today + timedelta(days=1) else d.strftime("%a %b %-d"))
            tk = ", ".join(i["tickers"][:2])
            tag = " (held)" if i["held"] else (" (watched)" if i["watched"] else "")
            dd = det.get(i["id"]) or {}
            if i["confirmed"]:
                tail = ""
            elif dd.get("from_company") and not dd.get("year_given"):
                tail = " (the company gave the day, not the year)"
            else:
                tail = " (date from a news report, may be off)"
            out.append(f"{day}: {i['title']} [{tk}{tag}]{tail}"[:220])
        return out
    except Exception as e:
        log.debug(f"[biocal] director lines failed: {e}")
        return []


def director_block(add, *, mode: str, focus=None, now=None) -> bool:
    """mason_director's hook. Adds the block through the director's own add() on a scheduled review (anchor, no
    focus) when the switch is on and something is due. With the switch off `add` is never called, so the
    director's input is byte-for-byte what it was. Returns True when the block was added."""
    if mode != "anchor" or focus:
        return False
    lines = director_lines(now)
    if not lines:
        return False
    add(DIRECTOR_TITLE, "\n".join(lines), 200)
    return True
