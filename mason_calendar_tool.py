"""
mason_calendar_tool.py — the events tool: look up upcoming events for a subject on
official pages, and add the ones that can be verified to the calendar.

    find_events(subject, months_ahead=12)

  subject   a seed event ("Computex", "wwdc"), a company or ticker ("RKLB"), or a theme
            ("semiconductor conferences", "quantum").
  1. pages  the seed list's official URLs; for a company, its investor-relations site as
            filed with the SEC (the `investorWebsite` / `website` fields of the company's
            submissions record, read through mason_edgar) plus the event links found on
            it; or URLs listed under calendar.tool.pages. (2026-11-16) SEC leaves those fields blank,
            so for a company: calendar.tool.ir_pages first, then SEC, then the website Yahoo lists ->
            its investor site -> its events page (remembered per ticker). There is no web search here:
            the project has no free one, and a search result is not an official page.
  2. fetch  politely: robots.txt is obeyed, one honest client name, a pause per host, and
            the page text is cached (source_snapshots) for calendar.tool.cache_hours.
  3. ask    the selected model (calendar.tool.model) reads the date-bearing parts of the
            page and proposes events as strict JSON. Default: the local model, free.
            "claude-sonnet-5-5" is allowed; its cost goes into the same ledger as the
            quick checks (mason_director.record_spend, mode "check") under a daily cap.
  4. check  every proposal is verified WITHOUT a model before it is stored:
              - the date is in the future and inside the window;
              - that same date is written on the page (found by a deterministic date
                reader, any common format) — if it is not, the event is DROPPED: a model
                can never put a date on the calendar that the page does not show;
              - a distinctive word of the title is on the page close to that date ->
                confirmed; on the page but not close, or not on the page -> stored as
                "not confirmed" (or dropped, calendar.tool.store_unconfirmed false).
            A stored event keeps the page URL and the sentence the date came from.
  5. screen (2026-11-16, calendar.tool.strict_check) — also without a model: media releases, news
            headlines, nameless titles ("Third Quarter Earnings Conference Call"), days written without
            a year, a title the date's own lines do not mention, and an event the page does not tie to
            the subject are left out. One row per event (calendar.tool.merge_duplicates): an event
            already on the calendar on that date (seed list, or found earlier under another subject)
            is not stored again; a second finding adds its subject and tickers to the earlier row.

Everything here is behind calendar.tool.enabled (default false): the prompt below is
text a model reads, and Nico approves those first (CALENDAR_PROMPTS_FOR_NICO.md).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.robotparser
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import mason_calendar as C
import mason_calendar_feeds as F
import mason_db

log = logging.getLogger("mason.calendar.tool")
SOURCE = "Event search"

TOOL_DEFAULTS = {
    "enabled": False,
    "model": "local",                 # "local" = the local model through mason_llm; or "claude-sonnet-5-5"
    "daily_usd_cap": 0.10,            # only matters for a paid model
    "est_usd_per_call": 0.01,
    "store_unconfirmed": True,        # false = drop what cannot be confirmed instead of storing it flagged
    "max_pages_per_subject": 3,
    "max_chunks_per_page": 2,
    "chunk_chars": 3200,
    "cache_hours": 24,
    "proximity_chars": 400,           # how close a title word must be to the date on the page
    "weekly": True,                   # the weekly job (seed list, holdings, watchlist, `subjects`)
    "weekly_days": 7,
    "recheck_days": 30,               # the re-check of stored unconfirmed / expected events
    "night_window_et": ["20:00", "06:00"],   # scheduled runs stay out of the trading day
    "pass_budget_s": 600,
    "subjects": [],                   # standing subjects, e.g. ["quantum conferences", "ASML"]
    "pages": {},                      # subject -> [official URLs] the owner wants read for it
    # (2026-11-16) after the 11-15 weekly run stored a TV premiere, mis-dated shopping events and the same
    # conference three times: screen() drops what is not a dated event of the subject, and _store() keeps one
    # row per event (same date + same name) across subjects and weeks. false = the behaviour before.
    "strict_check": True,
    "merge_duplicates": True,
    # (2026-11-16, leftovers 2) with merge_duplicates on, an earnings call / release found for a ticker that the
    # earnings feeds already list on the same day (mason_pead's 'Robinhood' row, the bellwether feed's row) is that
    # row, so it is not stored again (_twin). false = only seed-list and events-tool rows count as the same event.
    "earnings_twins": True,
    # (2026-11-16, events 2) company pages. SEC's submissions record leaves the website fields blank, so on 11-15
    # every held/watched company had no page at all. Now, for a ticker, in this order: the owner's own list
    # (ir_pages {"AMZN": ["https://ir.aboutamazon.com/events/default.aspx"]}), SEC, then the company's website as
    # Yahoo lists it -> its investor site -> that site's events page (find_company_pages; false = the old way).
    # The events page that worked is remembered per ticker; a miss is not looked up again for company_retry_days.
    "ir_pages": {},
    "find_company_pages": True,
    "discover_max_fetches": 4,        # pages read to find one company's events page
    "discover_max_per_run": 60,       # and for all companies in one pass of the weekly job
    "company_retry_days": 7,
    "skip_funds": True,               # the weekly job leaves out ETFs and funds (book() already drops the cash line)
    # (review 2026-11-16) a remembered page that loads but shows no upcoming date in this many weekly runs in a
    # row is looked for again (other candidate links first); a chat lookup stops finding pages after this long.
    # Scheduled runs also wait out a site's robots.txt Crawl-delay (at most 15 s); chat lookups keep the 1 s pause.
    "company_empty_runs": 4,
    "discover_chat_seconds": 30,
}

# ── the prompt (model-visible; shown in full in CALENDAR_PROMPTS_FOR_NICO.md) ───

SYSTEM_PROMPT = """You read the text of a web page and list the upcoming events it announces.

Rules:
- List only events whose date is written in the text. Copy the date as the page gives it. Never work out or guess a date.
- An event is something people attend or watch: a conference, a keynote, an investor day, a product launch, an earnings call, a shareholder meeting, a summit.
- Leave out things that already happened, registration or submission deadlines, and news articles.
- If the text shows no dated upcoming event, return an empty list.

Answer with JSON only, in exactly this form:
{"events": [{"title": "name of the event", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD or null", "location": "city, or online, or null", "date_text": "the date exactly as written on the page", "reason": "one short sentence saying what it is"}]}"""

USER_TEMPLATE = """Today is {today}.
Subject: {subject}
Page: {url}

Text of the page:
---
{text}
---

List the upcoming events in this text as JSON."""

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["events"],
    "properties": {"events": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "start_date", "end_date", "location", "date_text", "reason"],
        "properties": {"title": {"type": "string"}, "start_date": {"type": "string"},
                       "end_date": {"type": ["string", "null"]}, "location": {"type": ["string", "null"]},
                       "date_text": {"type": "string"}, "reason": {"type": "string"}}}}},
}


def tool_cfg(c: dict | None = None) -> dict:
    c = c if c is not None else C.cfg()
    return C._merge(TOOL_DEFAULTS, c.get("tool"))


def enabled() -> bool:
    c = C.cfg()
    return c.get("enabled") is True and tool_cfg(c).get("enabled") is True


# ── reading dates out of page text (deterministic) ──────────────────────────────

_MON_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_D = r"(\d{1,2})(?:st|nd|rd|th)?"
_SEP = r"\s*(?:-|•|·|to|through|thru|until|and|&)\s*"
_PATTERNS = [
    # February 28 – March 3, 2027
    ("mdmdy", re.compile(rf"\b({_MON_RE})\s+{_D}{_SEP}({_MON_RE})\s+{_D},?\s+(20\d\d)\b", re.I)),
    # January 6-9, 2027  /  Jan. 6, 2027
    ("mddy", re.compile(rf"\b({_MON_RE})\s+{_D}(?:{_SEP}{_D})?,?\s+(20\d\d)\b", re.I)),
    # 28 February – 3 March 2027
    ("dmdmy", re.compile(rf"\b{_D}\s+({_MON_RE}){_SEP}{_D}\s+({_MON_RE}),?\s+(20\d\d)\b", re.I)),
    # 2-5 March 2027  /  1 March 2027
    ("ddmy", re.compile(rf"\b{_D}(?:{_SEP}{_D})?\s+({_MON_RE}),?\s+(20\d\d)\b", re.I)),
    ("iso", re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")),
    ("us", re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")),
    # June 8-12 (no year: the year is taken from the text right around it, never assumed)
    ("mdd", re.compile(rf"\b({_MON_RE})\s+{_D}(?:{_SEP}{_D})?\b(?!\s*,?\s*20\d\d)", re.I)),
]


def _mon(s: str):
    return F._MON.get(s.strip(". ").lower()[:3])


def _mk(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _norm_text(text: str) -> str:
    """Same length as the input: odd dashes become '-', odd spaces become ' '."""
    return re.sub(r"[\u2010-\u2015\u2212]", "-", re.sub(r"[\u00a0\u2007\u2009\u202f]", " ", text or ""))


def find_dates(text: str, yearless: bool = False) -> list:
    """Every date or date range written in `text`: [{start, end, pos, text}] (dates are
    datetime.date). A date written without a year is kept only when exactly one year is
    written within 80 characters of it; nothing is assumed. With yearless=True the dates
    that have no year near them are returned too, as {month, day, end_day, pos, text}
    with start None, for validate() to match against "<event name> <year>" on the page."""
    text = _norm_text(text)
    found, taken = [], []

    def free(a, b):
        return not any(a < y and x < b for x, y in taken)

    for name, rx in _PATTERNS:
        for m in rx.finditer(text):
            if not free(m.start(), m.end()):
                continue
            g, s, e = m.groups(), None, None
            if name == "mdmdy":
                s, e = _mk(g[4], _mon(g[0]), g[1]), _mk(g[4], _mon(g[2]), g[3])
                if s and e and e < s:
                    s = _mk(int(g[4]) - 1, _mon(g[0]), g[1])
            elif name == "mddy":
                s = _mk(g[3], _mon(g[0]), g[1])
                e = _mk(g[3], _mon(g[0]), g[2]) if g[2] else None
            elif name == "dmdmy":
                s, e = _mk(g[4], _mon(g[1]), g[0]), _mk(g[4], _mon(g[3]), g[2])
                if s and e and e < s:
                    s = _mk(int(g[4]) - 1, _mon(g[1]), g[0])
            elif name == "ddmy":
                s = _mk(g[3], _mon(g[2]), g[0])
                e = _mk(g[3], _mon(g[2]), g[1]) if g[1] else None
            elif name == "iso":
                s = _mk(g[0], g[1], g[2])
            elif name == "us":
                s = _mk(g[2], g[0], g[1])
            elif name == "mdd":
                near = text[max(0, m.start() - 80): m.end() + 80]
                years = sorted({int(y) for y in re.findall(r"\b(20\d\d)\b", near)})
                if len(years) != 1:
                    if yearless and _mon(g[0]) and 1 <= int(g[1]) <= 31:
                        taken.append((m.start(), m.end()))
                        found.append({"start": None, "end": None, "month": _mon(g[0]), "day": int(g[1]),
                                      "end_day": int(g[2]) if g[2] else None, "pos": m.start(), "text": m.group(0)})
                    continue
                s = _mk(years[0], _mon(g[0]), g[1])
                e = _mk(years[0], _mon(g[0]), g[2]) if g[2] else None
            if s is None:
                continue
            if e is not None and (e <= s or (e - s).days > 45):
                e = None
            taken.append((m.start(), m.end()))
            found.append({"start": s, "end": e, "pos": m.start(), "text": m.group(0)})
            if name == "mdd":             # the year is not written with the day: it was read from the text around it
                found[-1]["year_from_context"] = True
    found.sort(key=lambda x: x["pos"])
    return found


_STOP = set("""the a an and or of for to in on at by with from annual event events conference conferences meeting
meetings summit day days call live virtual online global international world forum week show expo keynote
investor investors relations presentation presentations webcast session sessions quarter quarterly results
earnings fiscal first second third fourth january february march april may june july august september october
november december 2026 2027 2028 inc corp company corporation ltd llc plc holdings group""".split())


def title_tokens(title: str) -> list:
    words = re.findall(r"[a-z0-9][a-z0-9+&.'-]*", str(title or "").lower())
    words = [w.strip(".'-") for w in words]
    strong = [w for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit()]
    return strong or [w for w in words if len(w) >= 3 and not w.isdigit()]


def same_event(title: str, known_name: str) -> bool:
    """True when `title` is the known event itself ("CES 2027" for "CES", "45th Annual J.P.
    Morgan Healthcare Conference") and not something that merely happens at it ("Chrysler
    to reveal the Pacifica at the New York Auto Show"): every distinctive word of the
    known name is in the title and the title adds at most two more."""
    want, got = set(title_tokens(known_name)), set(title_tokens(title))
    return bool(want) and want <= got and len(got - want) <= 2


def _sentence(text: str, pos: int, width: int = 240) -> str:
    a = max(0, pos - width // 2)
    return re.sub(r"\s+", " ", text[a: a + width]).strip()


def validate(cand: dict, page_text: str, *, today: date, months_ahead: int = 12, proximity: int = 400,
             extra_tokens=None, known_name: str | None = None) -> dict:
    """Check one proposed event against the page it came from. No model.
    Returns {'verdict': confirmed|unconfirmed|dropped, 'why', 'start', 'end', 'sentence'}."""
    start = C.parse_date(cand.get("start_date"))
    if start is None:
        return {"verdict": "dropped", "why": "no usable date"}
    if start < today:
        return {"verdict": "dropped", "why": "the date is in the past"}
    if start > today + timedelta(days=int(months_ahead) * 31):
        return {"verdict": "dropped", "why": "the date is beyond the window"}
    page_text = _norm_text(page_text)
    dates = find_dates(page_text, yearless=True)
    hits = [d for d in dates if d["start"] == start]
    low = page_text.lower()
    toks = title_tokens(cand.get("title")) + [str(t).lower() for t in (extra_tokens or [])]
    toks = [t for t in dict.fromkeys(toks) if t]
    no_year = False
    if not hits:
        # "January 6-9" with the year only in the event's name ("CES 2027"): the day and month are
        # written, and the year stands right next to a word of the title somewhere on the page
        no_year = True
        yl = [d for d in dates if d["start"] is None and d["month"] == start.month and d["day"] == start.day]
        named = any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"\W{0,3}" + str(start.year) + r"(?!\d)|(?<!\d)" +
                              str(start.year) + r"\W{0,3}" + re.escape(t) + r"(?![a-z0-9])", low) for t in toks)
        if yl and named:
            hits = [dict(d, start=start, end=_mk(start.year, d["month"], d["end_day"]) if d.get("end_day") else None)
                    for d in yl]
            for h in hits:
                if h["end"] is not None and h["end"] <= start:
                    h["end"] = None
    if not hits:
        return {"verdict": "dropped", "why": "that date is not written on the page"}
    spots = {t: [m.start() for m in re.finditer(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low)] for t in toks}
    best, near = hits[0], False
    for h in hits:
        if any(abs(p - h["pos"]) <= proximity for ps in spots.values() for p in ps):
            best, near = h, True
            break
    # Pairing (near is not enough: on a page that lists several events, a real date can stand next to
    # another event's name). The name belongs to this date only when every distinctive word of the
    # PROPOSED TITLE that is on the page has an occurrence whose nearest date -- edge to edge, and
    # strictly nearer than any different date -- is this one. The seed's own name (extra_tokens) does
    # not count: it is all over its own page. A page with one date pairs trivially.
    # STRICTER FORM (approved by Nico 2026-11-15): "strictly nearer" is not enough either. A name that
    # sits between two dates ("Sept 9-10 / Q2B Copenhagen / Dec 8-10") is a few characters nearer to one
    # of them by accident of layout. Any OTHER date must be clearly farther away: more than twice as far
    # as this one, and at least 60 characters farther. A tightly packed list of events therefore comes
    # out "date not confirmed" instead of confirmed.
    def _gap(p, n, d):
        a, b = d["pos"], d["pos"] + len(d["text"])
        return (a - (p + n)) if a >= p + n else ((p - b) if p >= b else 0)

    def _same(d):
        return d["start"] == start if d["start"] is not None else (d["month"], d["day"]) == (start.month, start.day)

    others = [d for d in dates if not _same(d)]
    own = [t for t in dict.fromkeys(title_tokens(cand.get("title"))) if spots.get(t)]

    def _paired(t):
        for p in spots[t]:
            for h in hits:
                g = _gap(p, len(t), h)
                if g <= proximity and all(_gap(p, len(t), o) > max(2 * g, g + 60) for o in others):
                    return h
        return None
    pairs = [_paired(t) for t in own]
    paired = bool(own) and all(pairs)
    if paired:
        best = pairs[0]
    year_from_context = all(h.get("year_from_context") for h in hits)
    want_end = C.parse_date(cand.get("end_date"))
    end = best["end"] if best["end"] else None            # a range is kept only as the page writes it
    out = {"start": start.isoformat(), "end": end.isoformat() if end else None, "pos": best["pos"],
           "sentence": _sentence(page_text, best["pos"]), "date_text": best["text"],
           "end_dropped": bool(want_end and (end is None or end != want_end))}
    if (no_year or year_from_context) and not (near and known_name and same_event(cand.get("title"), known_name)):
        # a day written without a year is only trusted for the known event itself; anything else
        # (a session, a launch at the show, an old news item) could belong to another year
        return dict(out, verdict="unconfirmed", why="the page writes the day without a year")
    if near and (paired or (known_name and same_event(cand.get("title"), known_name) and not others)):
        return dict(out, verdict="confirmed", why="the date and the name are written together on the page")
    if near:
        return dict(out, verdict="unconfirmed", why="the name stands near this date, but the page lists other dates as close to it")
    if any(spots.values()):
        return dict(out, verdict="unconfirmed", why="the date and the name are both on the page, but not together")
    return dict(out, verdict="unconfirmed", why="the date is on the page, the name is not")


def check(cand: dict, page_text: str, **kw) -> dict:
    """validate(), plus one deterministic repair: when the page writes the day without a
    year ("January 6-9") and the model put last year on it, try the next year. That only
    passes when the page itself prints that year next to the event's name ("CES 2027");
    a date the page writes WITH a year is never shifted."""
    v = validate(cand, page_text, **kw)
    if v["verdict"] == "dropped" and v["why"] == "the date is in the past":
        d = C.parse_date(cand.get("start_date"))
        try:
            nxt = d.replace(year=d.year + 1)
        except (AttributeError, ValueError):
            return v
        if not any(x["start"] == d for x in find_dates(page_text)):      # not written with that (past) year
            v2 = validate(dict(cand, start_date=nxt.isoformat(), end_date=None), page_text, **kw)
            if v2["verdict"] != "dropped":
                v2["why"] += "; the year is the one printed next to the event's name"
                return v2
    return v


# ── screen: is this a dated event OF THE SUBJECT? (2026-11-16, calendar.tool.strict_check) ─────
# validate() proves the date is on the page. These rules, also without a model, catch what the 11-15
# weekly run let through: a TV premiere, shopping-news items dated with a day the page wrote without a
# year, a title the date's own sentence does not mention ("Amazon Prime Day" on the Cyber Monday date),
# a news headline, and an earnings call that names no company.

_GENERIC = _STOP | set("""stockholders stockholder shareholders shareholder analyst analysts financial business update
general special fireside chat""".split())
_MEDIA_RE = re.compile(r"\bpremier(?:e|es|ing)\b|\bseason\s+\d+\b|\btrailer\b|\bepisodes?\b|\bseries finale\b|\bfilms?\b|"
                       r"\bmovies?\b|\balbum\b|\bbox office\b|\bprime video\b|\bstreaming\b|\btv (?:show|series)\b", re.I)
_NEWS_RE = re.compile(r"\bupdates\b|\bnews\b|everything you need to know|\bhere'?s\b|\?|^(?:watch|how to|what to)\b", re.I)
_MARKET_TYPES = {"market", "fed", "econ", "congress", "court", "fda"}
# (review 2026-11-16) "film", "streaming", "news", "updates", "trailer", "watch" are also words in real event and
# company names ("International Conference on Thin Films", "News Corp Annual Meeting of Stockholders", "Roku
# Streaming Investor Day"). A title that names a kind of market event is not dropped for those words alone; a
# clear headline or release mark (a question, "here's", "premiere", "Season 3", "episode") still drops it.
_EVENT_WORD_RE = re.compile(r"\b(?:conference|summit|symposium|congress|convention|forum|expo|keynote|upfront|"
                            r"(?:investor|analyst|capital markets) day|(?:annual|special|shareholders'?|stockholders'?) meeting|"
                            r"earnings (?:call|webcast))s?\b", re.I)
_STRONG_RE = re.compile(r"\bpremier(?:e|es|ing)\b|\bseason\s+\d+\b|\bepisodes?\b|\bseries finale\b|\bbox office\b|"
                        r"everything you need to know|\bhere'?s\b|\?|^(?:how to|what to)\b", re.I)


def identity_names(seed: dict | None) -> list:
    """The names that make a finding "the seed event itself". (review 2026-11-16) For a company's own item
    (seed type "company") an alias that is only the company's name ("AMD" on AMD Advancing AI, "Tesla",
    "Intel", "Berkshire") routes the subject to the item but does not name the event: with it, "AMD Financial
    Analyst Day" or "AMD Q4 Earnings Call" counted as AMD Advancing AI and moved that row to their date."""
    if not seed:
        return []
    org = set(title_tokens(seed.get("organiser"))) if seed.get("type") == "company" and seed.get("organiser") else set()
    out = [seed.get("name")] + [a for a in (seed.get("aliases") or []) if not (org and set(title_tokens(a)) <= org)]
    return [n for n in out if n]


def generic_title(title: str) -> bool:
    """True when every word of the title is generic ("Third Quarter Earnings Conference Call",
    "Q3 2026 Earnings Call", "Annual Meeting of Stockholders"): it names no company or event."""
    words = [w.strip(".'-") for w in re.findall(r"[a-z0-9][a-z0-9+&.'-]*", str(title or "").lower())]
    return not [w for w in words if len(w) >= 2 and w not in _GENERIC and not w.isdigit()
                and not re.match(r"^(?:q[1-4]|h[12]|fy\d{0,4}|\d+(?:st|nd|rd|th))$", w)]


def _has(word: str, text: str) -> bool:
    """`word` starts a word in `text` ("tech" is in "Technology")."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word), text) is not None


def date_block(text: str, pos: int, reach: int = 2) -> str:
    """The lines that carry the date at `pos`, lower-cased: its own line and up to `reach` lines either
    side, stopping before a line that carries a different date (that is another item of the list)."""
    import bisect
    lines, starts, p = _norm_text(text or "").split("\n"), [], 0
    for ln in lines:
        starts.append(p)
        p += len(ln) + 1
    i = max(0, bisect.bisect_right(starts, pos) - 1)
    own = {d["text"] for d in find_dates(lines[i], yearless=True)}

    def other_date(j):
        return any(d["text"] not in own for d in find_dates(lines[j], yearless=True))
    a = b = i
    while a > 0 and i - a < reach and not other_date(a - 1):
        a -= 1
    while b < len(lines) - 1 and b - i < reach and not other_date(b + 1):
        b += 1
    return "\n".join(lines[a: b + 1]).lower()


_QWORD = {"first": "q1", "second": "q2", "third": "q3", "fourth": "q4"}


def _quarters(text: str) -> set:
    """The quarters a text names: 'Q3', 'third quarter' -> {'q3'}."""
    low = str(text or "").lower()
    return set(re.findall(r"(?<![a-z0-9])(q[1-4])(?![0-9])", low)) | {
        _QWORD[w] for w in re.findall(r"\b(first|second|third|fourth)[- ]quarter\b", low)}


def _block_names(title: str, need: list, blk: str) -> bool:
    """At least half the title's own words are in the block, and (review 2026-11-16) a quarter the title names
    is not contradicted by the block: title_tokens() drops 'Q4' as too short, so 'FY2027 Q4 Earnings Release'
    matched the lines of 'FY2027 Q3 Earnings Release' and could take the Q3 date."""
    if sum(_has(t, blk) for t in need) < (len(need) + 1) // 2:
        return False
    want, got = _quarters(title), _quarters(blk)
    return not (want and got and not (want & got))


def _other_spot_names(title: str, need: list, v: dict, page_text: str) -> bool:
    """(2026-11-16, events 2) True when another place on the page that writes this same date carries the
    title. Walmart's events page shows a badge "Nov / 19" above "Nov. 19, 2026 | 7:00 a.m. | US/Central |
    FY2027 Q3 Earnings Release"; validate() picked the badge, whose lines do not reach the title."""
    day = C.parse_date(v.get("start"))
    if day is None:
        return False
    for d in find_dates(page_text, yearless=True):
        if d["start"] == day and d["pos"] != int(v.get("pos") or 0):
            if _block_names(title, need, date_block(page_text, d["pos"], reach=4)):
                return True
    return False


def screen(title: str, v: dict, page_text: str, res: dict, seed: dict | None = None) -> str | None:
    """None = keep. Otherwise the plain reason the proposal is left out. `v` is check()'s answer
    (it carries the date's position on the page), `res` the resolved subject, `seed` the seed
    event whose page this is (seed and theme subjects)."""
    known = [n for n in ([seed.get("name")] + list(seed.get("aliases") or []) if seed else []) if n]
    itself = any(same_event(title, n) for n in identity_names(seed))   # the seed event itself (its name is Nico's own list)
    if not itself and not (_EVENT_WORD_RE.search(title) and not _STRONG_RE.search(title)):
        if _MEDIA_RE.search(title):
            return "a TV show, film or other media release, not a market event"
        if _NEWS_RE.search(title):
            return "a news headline, not a scheduled event"
    if not itself:
        if generic_title(title):
            return "the title names no company or event"
    if v.get("verdict") != "confirmed":                # confirmed = validate() found the name and this date written together
        if str(v.get("why") or "").startswith("the page writes the day without a year"):
            return "the page gives the day but not the year for it, and it is not the subject's own event"
        block = date_block(page_text, int(v.get("pos") or 0), reach=4)   # (review) date / time / place / title cards
        top = _norm_text(page_text or "").split("\n", 1)[0][:200].lower()          # the page's own name
        owner = set(title_tokens(top)) | {t.lower() for t in (res.get("tickers") or [])}   # who runs the page / the company
        if seed and seed.get("organiser"):
            owner |= set(title_tokens(seed["organiser"]))
        need = [t for t in dict.fromkeys(title_tokens(title)) if t not in owner]
        if (need or _quarters(title)) and not _block_names(title, need, block) and not _other_spot_names(title, need, v, page_text):
            return "the sentence that carries this date is about something else"
    if not itself and res.get("kind") in ("seed", "theme") and (seed or {}).get("type") not in _MARKET_TYPES:
        # a company's own investor site, or a page the owner listed, is the subject's page by construction
        words = set(title_tokens(res.get("label"))) | {w for n in known for w in title_tokens(n)}
        if seed and seed.get("organiser"):
            words |= set(title_tokens(seed["organiser"]))
        near = date_block(page_text, int(v.get("pos") or 0), reach=1)   # the item itself, not its neighbour's name
        head = _norm_text(page_text or "")[:300].lower()                # the page's own name and headline
        if words and not any(_has(w, near) or _has(w, head) for w in words):
            return f"the page does not connect it to {res.get('label')}"
    return None


# ── pages ───────────────────────────────────────────────────────────────────────

_robots: dict = {}
_CRAWL_DELAY_MAX_S = 15.0          # (review 2026-11-16) a robots.txt Crawl-delay is honoured up to this, in scheduled runs


def robots_allowed(url: str, fetch=None) -> bool:
    """True when the site's robots.txt lets a generic client read `url` (or has none)."""
    try:
        u = urlparse(url)
        root = f"{u.scheme}://{u.netloc}"
        rp = _robots.get(root)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            r = (fetch or F._get)(root + "/robots.txt")
            body = F._text(r) if r is not None else ""
            rp.parse(body.splitlines() if body and "<html" not in body[:300].lower() else [])
            _robots[root] = rp
        return rp.can_fetch(F.UA, url) and rp.can_fetch("*", url)
    except Exception:
        return True


def crawl_delay(url: str) -> float:
    """The Crawl-delay the site's robots.txt asks of this client (read by robots_allowed), capped at
    _CRAWL_DELAY_MAX_S; 0 when it asks none. Many investor sites ask 10 s."""
    try:
        u = urlparse(url)
        rp = _robots.get(f"{u.scheme}://{u.netloc}")
        d = (rp.crawl_delay(F.UA) or rp.crawl_delay("*")) if rp is not None else None
        return min(max(float(d or 0), 0.0), _CRAWL_DELAY_MAX_S)
    except Exception:
        return 0.0


def _polite_wait(url: str) -> None:
    """(review 2026-11-16) Before a scheduled run's request: wait out the site's Crawl-delay, counted from
    the last request to that host (F._get keeps the time; its own 1 s pause still applies)."""
    d = crawl_delay(url)
    if d > F._PAUSE_S:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        wait = d - (time.monotonic() - F._last_hit.get(host, -1e9))
        if wait > 0:
            _sleep(wait)


_sleep = time.sleep                 # tests replace it


def extract_text(html_text: str) -> str:
    """Visible text of a page, one block per line."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text or "", "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "template"]):
            t.decompose()
        raw = soup.get_text("\n")
    except Exception:
        raw = re.sub(r"<[^>]+>", "\n", re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text or "", flags=re.S | re.I))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def event_links(html_text: str, base_url: str, limit: int = 2) -> list:
    """Links on an investor-relations page that look like its events page (same site)."""
    out, host = [], urlparse(base_url).netloc.split(":")[0].removeprefix("www.")
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html_text or "", re.S | re.I):
        href, label = m.group(1), F._strip_tags(m.group(2)).lower()
        if not re.search(r"event|calendar|presentation|investor[- ]day", href.lower() + " " + label):
            continue
        if re.search(r"\.(pdf|ics|pptx?|xlsx?|zip)(\?|$)|mailto:|javascript:|\{\{", href, re.I):
            continue                                  # ({{ = a script template's placeholder, not a link)
        full = urljoin(base_url, href)
        h = urlparse(full).netloc.split(":")[0].removeprefix("www.")
        if (h == host or h.endswith("." + host) or host.endswith("." + h)) and full not in out and full.rstrip("/") != base_url.rstrip("/"):
            out.append(full)
        if len(out) >= limit:
            break
    return out


def base_domain(url_or_host: str) -> str:
    """'https://corporate.walmart.com/x' -> 'walmart.com'; 'www.bbc.co.uk' -> 'bbc.co.uk'."""
    h = (urlparse(url_or_host).netloc if "//" in str(url_or_host) else str(url_or_host)).split(":")[0].lower().strip(".")
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "ne", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_IR_LABEL_RE = re.compile(r"^(?:investors?(?: relations)?|investor (?:information|center|centre)|shareholders?|stockholders?)$", re.I)
_IR_HOST_RE = re.compile(r"^(?:investors?|ir|stock|shareholders?)\.", re.I)


def investor_links(html_text: str, base_url: str, limit: int = 3) -> list:
    """Links on a company's home page to its investor site ("Investors", investor.<domain>, ir.<domain>,
    <domain>/investors), same company domain only."""
    out, dom = [], base_domain(base_url)
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html_text or "", re.S | re.I):
        href, label = m.group(1).strip(), F._strip_tags(m.group(2)).strip()
        full = urljoin(base_url, href)
        u = urlparse(full)
        if u.scheme not in ("http", "https") or base_domain(u.netloc) != dom:
            continue
        host_ir = bool(_IR_HOST_RE.match(u.netloc.removeprefix("www.")))
        path_ir = bool(re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?investors?(?:-relations)?/?$", u.path, re.I))
        if not (host_ir or path_ir or _IR_LABEL_RE.match(label)):
            continue
        if re.search(r"\.(pdf|ics|pptx?|xlsx?|zip)(\?|$)", u.path, re.I):
            continue
        if full not in out:
            out.append(full)
        if len(out) >= limit:
            break
    return out


def fetch_page(url: str, *, cache_hours: float = 24, fetch=None, now: float | None = None, polite: bool = False) -> dict:
    """{'ok', 'url', 'text', 'html', 'cached', 'why'}. Cached text is reused inside cache_hours.
    polite=True (the weekly / re-check runs): the site's robots.txt Crawl-delay is waited out first."""
    now = time.time() if now is None else now
    key = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:20]
    try:
        conn = mason_db.get_connection()
        try:
            hit = C._snap_get(conn, "_calendar_page", key)
        finally:
            conn.close()
    except Exception:
        hit = None
    if hit and now - float(hit.get("at") or 0) < float(cache_hours) * 3600:
        return {"ok": True, "url": url, "text": hit.get("text") or "", "html": "", "cached": True, "links": hit.get("links") or [],
                "ir_links": hit.get("ir_links") or []}
    if not url.lower().startswith("https://") and not url.lower().startswith("http://"):
        return {"ok": False, "url": url, "why": "not a web address"}
    if not robots_allowed(url, fetch):
        return {"ok": False, "url": url, "why": "the site's robots.txt does not allow it"}
    if polite:
        _polite_wait(url)
    r = (fetch or F._get)(url)
    if r is None:
        return {"ok": False, "url": url, "why": "the page did not load"}
    html_text = F._text(r)
    text = extract_text(html_text)
    if len(text) < 200:
        return {"ok": False, "url": url, "why": "the page has no readable text (it is probably built by scripts)"}
    links = event_links(html_text, url)
    ir = investor_links(html_text, url)
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                C._snap_put(conn, "_calendar_page", key, {"at": now, "url": url, "text": text[:60000], "links": links,
                                                          "ir_links": ir})
        finally:
            conn.close()
    except Exception:
        pass
    return {"ok": True, "url": url, "text": text[:60000], "html": html_text, "cached": False, "links": links, "ir_links": ir}


def date_windows(text: str, chunk_chars: int = 3200, max_chunks: int = 2, today: date | None = None,
                 subject: str | None = None) -> list:
    """The parts of a page that carry dates, as at most `max_chunks` pieces of about
    `chunk_chars`: the top of the page (its name and headline), then each line with an
    upcoming date plus the two lines before and after it. A small local model reads this
    far better than a whole page of menus."""
    text = text or ""
    lines = text.splitlines()
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    keep = set()
    import bisect
    for d in find_dates(text, yearless=True):
        if today is not None and d["start"] is not None and (d["end"] or d["start"]) < today:
            continue
        a = bisect.bisect_right(starts, d["pos"]) - 1
        b = bisect.bisect_right(starts, d["pos"] + len(d["text"])) - 1
        keep.update(range(max(0, a - 2), min(len(lines), b + 3)))
    if not keep:
        return []
    head = "Top of the page: " + re.sub(r"\s+", " ", text[:300]).strip() + "\n…\n"
    if subject:                       # the line that names the event with its year ("CES 2027"), when there is one
        for t in title_tokens(subject):
            m = re.search(r"(?im)^.*(?<![a-z0-9])" + re.escape(t) + r"\W{0,3}20\d\d(?!\d).*$", text)
            if m and m.group(0).strip() not in head:
                head += m.group(0).strip()[:200] + "\n…\n"
                break
    chunks, cur, last = [], head, None
    for i in sorted(keep):
        piece = ("" if last is None or i == last + 1 else "…\n") + lines[i][:400] + "\n"
        if len(cur) + len(piece) > chunk_chars and cur != head:
            chunks.append(cur)
            cur = head
            if len(chunks) >= max_chunks:
                break
        cur += piece
        last = i
    if cur != head and len(chunks) < max_chunks:
        chunks.append(cur)
    return chunks


# ── asking the model ────────────────────────────────────────────────────────────

def parse_model_json(raw: str) -> list:
    """The `events` list out of a model answer (code fences and stray text tolerated)."""
    s = str(raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return []
    try:
        d = json.loads(s[a: b + 1])
    except Exception:
        return []
    ev = d.get("events") if isinstance(d, dict) else None
    return [e for e in ev if isinstance(e, dict)] if isinstance(ev, list) else []


def _spent_today() -> float:
    import mason_director as D
    day = D._now_et().strftime("%Y-%m-%d")
    return round(sum(float(r.get("cost_usd") or 0) for r in D._ledger_rows()
                     if r.get("day") == day and str(r.get("kind") or "").startswith("calendar")), 6)


def budget_ok(tc: dict) -> tuple:
    """(ok, plain reason). The tool's own daily cap, then the overall daily model budget
    with the money kept back for the scheduled director reviews (the quick checks' rule)."""
    import mason_director as D
    est = float(tc.get("est_usd_per_call") or 0.01)
    mine = _spent_today()
    if mine + est > float(tc.get("daily_usd_cap") or 0.10):
        return False, (f"The events tool has spent ${mine:.2f} today and its limit is "
                       f"${float(tc.get('daily_usd_cap') or 0.10):.2f} a day (calendar.tool.daily_usd_cap).")
    s = D.spend()
    cap, res = D.overall_daily_cap(), D.scheduled_reserve_usd()
    if s.get("all_day_usd", 0.0) + est + res > cap:
        return False, (f"Model spending today is ${s.get('all_day_usd', 0.0):.2f}; with ${res:.2f} kept back for the "
                       f"scheduled reviews, the daily limit of ${cap:.2f} (model_budget.daily_usd_cap) leaves no room.")
    return True, ""


def ask_model(subject: str, url: str, text: str, tc: dict, today: date) -> dict:
    """One model call for one piece of page text. {'events': [...], 'model', 'cost_usd', 'error'}."""
    user = USER_TEMPLATE.format(today=today.isoformat(), subject=subject, url=url, text=text)
    model = str(tc.get("model") or "local")
    import mason_llm
    if model in ("local", "qwen", "qwen3.5:4b"):
        try:
            raw = mason_llm.complete(SYSTEM_PROMPT, user, intent="chore", temperature=0.1, num_predict=900)
        except Exception as e:
            return {"events": [], "model": "local", "cost_usd": 0.0, "error": f"local model: {str(e)[:160]}"}
        return {"events": parse_model_json(raw), "model": "local", "cost_usd": 0.0, "error": None}
    ok, why = budget_ok(tc)
    if not ok:
        return {"events": [], "model": model, "cost_usd": 0.0, "error": why, "capped": True}
    try:
        res = mason_llm.complete_check(SYSTEM_PROMPT, user, model=model, effort="low", max_tokens=1500,
                                       schema=OUTPUT_SCHEMA, timeout_s=45.0)
    except Exception as e:
        return {"events": [], "model": model, "cost_usd": 0.0, "error": f"{model}: {str(e)[:160]}"}
    cost = float(res.get("cost_usd") or 0)
    try:
        import mason_director as D
        D.record_spend(cost, mode="check", kind="calendar:find", note=str(subject)[:40])
    except Exception as e:
        log.warning(f"[calendar] could not record tool spend: {e}")
    return {"events": parse_model_json(res.get("text")), "model": model, "cost_usd": cost, "error": None}


# ── what a subject is, and which pages to read for it ───────────────────────────

def _seed_index() -> list:
    try:
        return [s for s in F.load_seed().get("events") or [] if isinstance(s, dict) and s.get("id")]
    except Exception:
        return []


def _ir_site(ticker: str) -> list:
    """The company's investor site as filed with the SEC (submissions record), read
    through mason_edgar's own polite client. [] when not found. (2026-11-16) SEC leaves
    both fields blank for nearly every filer; a miss is logged at info with the reason."""
    try:
        import mason_edgar as E
        got = E.resolve_cik(ticker)
        cik = got[0] if isinstance(got, (tuple, list)) else got
        if isinstance(cik, dict):
            cik = cik.get("cik")
        if not cik:
            log.info(f"[calendar] no investor site from SEC for {ticker}: no SEC company number (CIK) for the ticker")
            return []
        subs = E._fetch_submissions(int(cik))
        if not subs:
            log.info(f"[calendar] no investor site from SEC for {ticker}: the SEC record did not load")
            return []
        out = []
        for k in ("investorWebsite", "website"):
            u = str((subs or {}).get(k) or "").strip()
            if u:
                out.append(u if u.startswith("http") else "https://" + u)
        if not out:
            log.info(f"[calendar] no investor site from SEC for {ticker}: the SEC record leaves its website fields blank")
        return out[:1]
    except Exception as e:
        log.info(f"[calendar] no investor site from SEC for {ticker}: {type(e).__name__}: {str(e)[:160]}")
        return []


# ── company pages when SEC has none (2026-11-16, calendar.tool.find_company_pages) ──────────────
# The website Yahoo lists for the ticker (its irWebsite values are dead old links and are not used) ->
# the company's investor site (a link on the home page, or investor./investors./ir.<domain>) -> the events
# page linked from it. What worked is remembered per ticker in source_snapshots ('_calendar_company'), so
# later weeks go straight to it; a miss is not looked up again for company_retry_days.

_CO_SNAP = "_calendar_company"
_disc_run = {"used": 0}                    # pages read to find company sites in this pass (reset by _run_queue;
                                           # the cap applies to the scheduled runs only, not to a chat or CLI lookup)
_EV_PATHS = ("/events", "/news-events", "/events-and-presentations")


def _co_get(t: str) -> dict:
    try:
        conn = mason_db.get_connection()
        try:
            return C._snap_get(conn, _CO_SNAP, str(t).upper()) or {}
        finally:
            conn.close()
    except Exception:
        return {}


def _co_put(t: str, d: dict) -> None:
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                C._snap_put(conn, _CO_SNAP, str(t).upper(), d)
        finally:
            conn.close()
    except Exception as e:
        log.info(f"[calendar] could not remember the company page for {t}: {e}")


def forget_company_page(t: str, why: str, avoid: bool = False) -> None:
    """The remembered events page did not load: look for it again next time. avoid=True (review 2026-11-16):
    it loads but has shown no upcoming date for company_empty_runs weekly runs -- the next look prefers
    another candidate link when the investor site has one."""
    mem = _co_get(t)
    if mem.get("events_url"):
        log.info(f"[calendar] forgetting the events page of {t} ({mem['events_url']}): {why}")
        upd = dict(mem, events_url=None, checked_at=None, empty_runs=0, why=f"the remembered page stopped loading: {why}")
        if avoid:
            upd.update(avoid=(list(mem.get("avoid") or []) + [mem["events_url"]])[-3:], why=f"the remembered page {why}")
        _co_put(t, upd)


def note_company_page_empty(t: str, empty: bool, tc: dict) -> None:
    """(review 2026-11-16) A weekly run read the remembered events page. `empty` = it loaded but shows no
    upcoming date at all. After company_empty_runs such runs in a row (default 4: about a month) the page is
    looked for again, in case the ranking picked a page that never carries events (a heuristic can be wrong
    and would otherwise stick for ever). A page with dates on it resets the count."""
    mem = _co_get(t)
    if not mem.get("events_url"):
        return
    n = int(mem.get("empty_runs") or 0) + 1 if empty else 0
    if n == int(mem.get("empty_runs") or 0) == 0:
        return
    limit = int(tc.get("company_empty_runs") or 4)
    if n >= limit:
        forget_company_page(t, f"showed no upcoming date in {n} weekly runs", avoid=True)
    else:
        _co_put(t, dict(mem, empty_runs=n))


_YAHOO_WAIT_S = 30                  # most seconds to wait for one of Mason's three Yahoo slots
_YAHOO_RETRY_S = 6 * 3600           # Yahoo failed or answered with nothing: ask again after this, not a week


def yahoo_info(t: str) -> dict:
    """{'website', 'quote_type'} for a ticker from yfinance .info, inside Mason's own limit of three Yahoo
    requests at a time (mason_dossier._YF_SEM; waits at most _YAHOO_WAIT_S for a slot). Raises on failure,
    and on an empty answer (yfinance can return a near-empty dict when Yahoo throttles)."""
    import mason_dossier as D
    sem = getattr(D, "_YF_SEM", None)
    if sem is not None and not sem.acquire(timeout=_YAHOO_WAIT_S):
        raise TimeoutError("no free Yahoo slot")
    try:
        info = D._ticker(t).info or {}
    finally:
        if sem is not None:
            sem.release()
    out = {"website": str(info.get("website") or "").strip(), "quote_type": str(info.get("quoteType") or "").upper()}
    if not out["website"] and not out["quote_type"]:
        raise LookupError("Yahoo answered with no profile")
    return out


def fund_reason(t: str, conn=None) -> str | None:
    """Why `t` is not a company to search, from what Mason already has (no network), or None.
    Mason's ETF list (mason_etf_screen); a fund whose holdings Mason keeps (etf_holdings);
    a quote type other than EQUITY in Mason's latest dossier or in this tool's own company memory."""
    t = str(t or "").upper()
    # (review 2026-11-16) No rule for "CASH" here: book() already drops the account's cash line from the
    # holdings, so a CASH that reaches this list is on the watch list -- Pathward Financial (NASDAQ: CASH).
    try:
        import mason_etf_screen as E
        if t in E.UNIVERSE:
            return "an ETF (on Mason's ETF list)"
    except Exception:
        pass
    own = conn is None
    try:
        if own:
            conn = mason_db.get_connection()
        try:
            if conn.execute("SELECT 1 FROM etf_holdings WHERE etf=? LIMIT 1", (t,)).fetchone():
                return "a fund (Mason keeps its holdings list)"
        except Exception:
            pass
        qt = None
        try:
            r = conn.execute("SELECT json_extract(payload_json, '$.profile.quote_type') FROM dossiers WHERE subject=? "
                             "AND kind='ticker' AND record_deleted=0 ORDER BY built_at DESC LIMIT 1", (t,)).fetchone()
            qt = r[0] if r else None
        except Exception:
            pass
        if not qt:
            qt = (C._snap_get(conn, _CO_SNAP, t) or {}).get("quote_type")
        if qt and str(qt).upper() != "EQUITY":
            return f"quote type {str(qt).upper()}, not a company"
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def _rank_event_links(pg: dict, base: str) -> list:
    """The events-page links of an investor site, best first: a path about events (not press releases,
    not past events), then presentations / calendar, then the rest."""
    cands = event_links(pg.get("html") or "", base, limit=8) if pg.get("html") else list(pg.get("links") or [])

    def rank(u):
        p = urlparse(u).path.lower()
        if re.search(r"past|archive|press|release|ical|/20\d\d/", p):    # old items, news articles, calendar files
            return 9
        ir = bool(_IR_HOST_RE.match(urlparse(u).netloc.removeprefix("www.")) or re.search(r"investor|shareholder|stock", p))
        if re.search(r"event", p):
            return 0 if ir else 1                 # corporate.walmart.com/investors/events before /news/events
        if re.search(r"presentation|calendar", p):
            return 2
        return 3
    return [u for u in sorted(cands, key=rank) if rank(u) < 9]


def discover_events_page(website: str, *, fetch=None, max_fetches: int = 4, run_cap: int = 60,
                         cache_hours: float = 24, polite: bool = False, max_seconds: float | None = None,
                         avoid=()) -> dict:
    """From a company's website to its investor events page. {'url', 'root', 'tried': [[url, result]], 'why'}.
    Reads at most `max_fetches` pages (and stops when this pass has read `run_cap`), each through fetch_page
    (robots.txt, the per-host pause, the page cache). url None = not found, `why` says what was tried.
    (review 2026-11-16) polite = wait out each site's Crawl-delay (scheduled runs); max_seconds = start no
    page after this long (a chat lookup); avoid = links tried before that never showed a date, put last.
    'cut_short' = the pass cap or the time limit stopped it, not this company's own page limit."""
    out = {"url": None, "root": None, "tried": [], "why": None, "budget": False, "cut_short": False}
    t0 = time.monotonic()
    home = str(website or "").strip()
    if home and not home.lower().startswith(("http://", "https://")):
        home = "https://" + home
    dom = base_domain(home) if home else ""
    if "." not in dom:
        out["why"] = "the website listed for it is not a web address"
        return out

    def get(u):
        if len(out["tried"]) >= max_fetches:
            out["budget"] = True
            return None
        if _disc_run["used"] >= run_cap or (max_seconds is not None and time.monotonic() - t0 > max_seconds):
            out["budget"] = out["cut_short"] = True
            return None
        _disc_run["used"] += 1
        pg = fetch_page(u, cache_hours=cache_hours, fetch=fetch, polite=polite)
        out["tried"].append([u, "ok" if pg["ok"] else str(pg.get("why"))])
        return pg if pg["ok"] else None

    roots = []
    pg = get(home)
    if pg:
        roots += [u for u in pg.get("ir_links") or [] if u.rstrip("/") != home.rstrip("/")]
    roots += [f"https://{p}.{dom}/" for p in ("investor", "investors", "ir")]
    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        pg = get(root)
        if not pg:
            if out["budget"]:
                break
            continue
        out["root"] = root
        ev = _rank_event_links(pg, root)
        ev = [x for x in ev if x not in avoid] + [x for x in ev if x in avoid]
        if ev:
            out["url"] = ev[0]
            return out
        u = urlparse(root)
        for path in _EV_PATHS:
            p2 = get(f"{u.scheme}://{u.netloc}{path}")
            if p2 and find_dates(p2["text"]):
                out["url"] = f"{u.scheme}://{u.netloc}{path}"
                return out
        if find_dates(pg["text"]):
            out["url"] = root
            return out
        out["why"] = f"the investor site {root} links to no events page and shows no dates"
        return out
    out["why"] = ("the page budget ran out before an investor site was found" if out["budget"] and not out["tried"]
                  else "no investor site could be read (" + "; ".join(f"{u}: {w}" for u, w in out["tried"])[:300] + ")")
    return out


def company_pages(t: str, tc: dict, *, fetch=None, info_lookup=None, now: float | None = None) -> dict:
    """The events page for a ticker when SEC lists none. {'pages': [...], 'how': remembered|found|None, 'why'}."""
    now = time.time() if now is None else now
    t = str(t).upper()
    mem = _co_get(t)
    if mem.get("events_url"):
        return {"pages": [mem["events_url"]], "how": "remembered", "why": None}
    retry = float(mem.get("retry_s") or float(tc.get("company_retry_days") or 7) * 86400)
    if mem.get("checked_at") and now - float(mem["checked_at"]) < retry:
        why = f"{mem.get('why') or 'not found'} (looked up {datetime.fromtimestamp(float(mem['checked_at'])).date()})"
        log.info(f"[calendar] no events page for {t}: {why}")
        return {"pages": [], "how": None, "why": why}
    mem = dict(mem, ticker=t, retry_s=None)
    if not mem.get("website") and (mem.get("quote_type") or "EQUITY") == "EQUITY":   # at most once per retry window
        try:
            info = (info_lookup or yahoo_info)(t) or {}
            mem.update(website=info.get("website") or "", quote_type=info.get("quote_type") or "", info_at=now)
        except Exception as e:                      # (review) asked again after _YAHOO_RETRY_S, not a week
            mem.update(checked_at=now, retry_s=_YAHOO_RETRY_S, why=f"Yahoo did not answer ({type(e).__name__})")
            _co_put(t, mem)
            log.info(f"[calendar] no events page for {t}: {mem['why']}")
            return {"pages": [], "how": None, "why": mem["why"]}
    if mem.get("quote_type") and mem["quote_type"] != "EQUITY":
        why = f"quote type {mem['quote_type']}, not a company"
    elif not mem.get("website"):
        why = "Yahoo lists no website for it"
    else:
        in_run = bool(tc.get("_in_run"))
        d = discover_events_page(mem["website"], fetch=fetch, max_fetches=int(tc.get("discover_max_fetches") or 4),
                                 run_cap=int(tc.get("discover_max_per_run") or 60) if in_run else 10 ** 9,
                                 cache_hours=24.0 if tc.get("cache_hours") is None else float(tc["cache_hours"]),
                                 polite=in_run, max_seconds=None if in_run else float(tc.get("discover_chat_seconds") or 30),
                                 avoid=tuple(mem.get("avoid") or ()))
        if d["url"]:
            mem.update(events_url=d["url"], root=d["root"], tried=d["tried"], found_at=now, checked_at=now, why=None,
                       empty_runs=0)
            _co_put(t, mem)
            log.info(f"[calendar] events page for {t}: {d['url']} (from {mem['website']}, {len(d['tried'])} pages read)")
            return {"pages": [d["url"]], "how": "found", "why": None}
        why = d["why"]
        if d.get("cut_short"):                      # (review) the pass cap or the chat time limit stopped it part-way
            #                                         (or before any page): try again on the next pass, not next week
            log.info(f"[calendar] no events page for {t} yet: {why}")
            _co_put(t, mem)
            return {"pages": [], "how": None, "why": why}
        mem["tried"] = d["tried"]
    mem.update(checked_at=now, why=why)
    _co_put(t, mem)
    log.info(f"[calendar] no events page for {t}: {why}")
    return {"pages": [], "how": None, "why": why}


def _owner_pages(tc: dict, t: str) -> list:
    for k, urls in (tc.get("ir_pages") or {}).items():
        if str(k).strip().upper() == t:
            return [u for u in ([urls] if isinstance(urls, str) else list(urls or [])) if isinstance(u, str) and u.strip()]
    return []


def resolve(subject: str, tc: dict | None = None, *, ir_lookup=None, fetch=None, info_lookup=None) -> dict:
    """{'kind': seed|company|theme|pages|none, 'label', 'pages', 'tickers', 'type', 'seeds'}; for a company
    also 'how' (owner|sec|remembered|found) and, when there is no page, 'why_none'."""
    tc = tc if tc is not None else tool_cfg()
    subj = str(subject or "").strip()
    low, norm = subj.lower(), C.norm_title(subj)
    out = {"kind": "none", "label": subj, "pages": [], "tickers": [], "type": "conference", "seeds": []}
    if not subj:
        return out
    for k, urls in (tc.get("pages") or {}).items():
        if str(k).strip().lower() == low and urls:
            out.update(kind="pages", pages=[u for u in urls if u])
    seeds = _seed_index()
    for s in seeds:
        names = [s["id"], s.get("name", "")] + list(s.get("aliases") or [])
        if any(C.norm_title(n) == norm for n in names if n):
            out.update(kind="seed", label=s.get("name") or subj, tickers=s.get("tickers") or [],
                       type=s.get("type") or "conference", seeds=[s],
                       pages=out["pages"] + [u for u in [s.get("url")] + list(s.get("pages") or []) if u])
            return out
    if out["kind"] == "pages":
        return out
    if re.match(r"^[A-Za-z][A-Za-z.\-]{0,5}$", subj):
        t = subj.upper()
        bk = C.book()
        owner = _owner_pages(tc, t)
        known = bool(owner) or t in bk["held"] or t in bk["watched"]
        if not known:
            try:
                import mason_sources
                known = bool(mason_sources.known_ticker(t))
            except Exception:
                known = False
        if known:
            out.update(kind="company", label=t, tickers=[t], type="company")
            if owner:                                   # calendar.tool.ir_pages: the owner's own list comes first
                out.update(pages=owner, how="owner")
                return out
            pages = (ir_lookup or _ir_site)(t)
            if pages:
                out.update(pages=pages, how="sec")
                return out
            # SEC lists no site (the usual case). Tests that inject ir_lookup reach the network fallback only
            # when they also inject info_lookup.
            if tc.get("find_company_pages", True) is not False and (ir_lookup is None or info_lookup is not None):
                cp = company_pages(t, tc, fetch=fetch, info_lookup=info_lookup)
                out.update(pages=cp["pages"], how=cp["how"], why_none=cp["why"])
            else:
                out["why_none"] = "SEC lists no website for it"
            return out
    words = [w for w in re.findall(r"[a-z0-9]+", low) if len(w) >= 3 and w not in ("conference", "conferences", "events", "event", "the", "and")]
    hits = []
    for s in seeds:
        hay = " ".join([s.get("name", ""), " ".join(s.get("sectors") or []), " ".join(s.get("tickers") or []),
                        s.get("organiser") or ""]).lower()
        if words and any(re.search(r"(?<![a-z0-9])" + re.escape(w[:6]), hay) for w in words):
            hits.append(s)
    if hits:
        out.update(kind="theme", seeds=hits[:8], pages=[s["url"] for s in hits[:8] if s.get("url")])
    return out


# ── find_events ─────────────────────────────────────────────────────────────────

def find_events(subject: str, months_ahead: int = 12, *, source: str = "manual", today=None, store: bool = True,
                fetch=None, ask=None, ir_lookup=None, info_lookup=None, force: bool = False) -> dict:
    """Look for upcoming events for `subject` and add the verified ones to the calendar.
    Returns {'ok', 'subject', 'kind', 'pages': [...], 'found': [...], 'dropped': [...], 'error'}, and
    'error_kind': no_page | page_failed | nothing_found | None (for the weekly run's list; the model
    only ever sees summary_text()). `fetch`, `ask`, `ir_lookup`, `info_lookup` are for tests; `force` lets the evaluation harness run
    while the switch is off (it is never passed by the service). Never raises."""
    out = {"ok": False, "subject": subject, "kind": None, "pages": [], "found": [], "dropped": [], "cost_usd": 0.0,
           "error_kind": None}
    try:
        c = C.cfg()
        tc = tool_cfg(c)
        if not force and not (c.get("enabled") is True and tc.get("enabled") is True):
            out["error"] = "The events tool is switched off (calendar.tool.enabled)."
            return out
        today = C.today_et(today)
        strict = tc.get("strict_check", True) is not False
        res = resolve(subject, dict(tc, _in_run=source in ("tool_weekly", "tool_recheck")), ir_lookup=ir_lookup, fetch=fetch,
                      info_lookup=info_lookup)
        out["kind"] = res["kind"]
        if res.get("how"):
            out["how"] = res["how"]
        if not res["pages"]:
            out["error"] = ("No official page is known for this subject. Add one under calendar.tool.pages, "
                            "or add the event to mason_calendar_seed.json.")
            out["error_kind"] = "no_page"
            out["why_no_page"] = res.get("why_none") or ("not a known event, company or theme" if res["kind"] == "none" else None)
            return out
        pages = []
        for u in res["pages"]:
            if u not in pages:
                pages.append(u)
        limit = int(tc.get("max_pages_per_subject") or 3) if res["kind"] != "theme" else 8
        seen_keys, done, first = set(), 0, list(pages)
        while pages and done < limit:
            url = pages.pop(0)
            done += 1
            ch_h = tc.get("cache_hours")
            pg = fetch_page(url, cache_hours=24.0 if ch_h is None else float(ch_h), fetch=fetch,
                            polite=source in ("tool_weekly", "tool_recheck"))
            rec = {"url": url, "ok": pg["ok"], "why": pg.get("why"), "cached": pg.get("cached", False), "proposed": 0}
            out["pages"].append(rec)
            if not pg["ok"]:
                continue
            if res["kind"] == "company" and url in first:        # follow event links from the investor site only
                for link in pg.get("links") or []:
                    if link not in pages and all(p["url"] != link for p in out["pages"]):
                        pages.append(link)
            seed = None
            if res["kind"] in ("seed", "theme"):
                seed = next((s for s in res["seeds"] if s.get("url") == url or url in (s.get("pages") or [])), None)
            label = (seed or {}).get("name") or res["label"]
            chunks = date_windows(pg["text"], int(tc.get("chunk_chars") or 3200), int(tc.get("max_chunks_per_page") or 2), today,
                                  subject=label)
            if not chunks:
                rec["why"] = "no upcoming date is written on the page"
                continue
            extra = title_tokens(label) if seed else []
            known = label if seed else None
            for ch in chunks:
                ans = (ask or ask_model)(label, url, ch, tc, today)
                out["cost_usd"] = round(out["cost_usd"] + float(ans.get("cost_usd") or 0), 6)
                if ans.get("error"):
                    rec["why"] = ans["error"]
                    if ans.get("capped"):
                        pages = []
                    break
                for cand in ans.get("events") or []:
                    rec["proposed"] += 1
                    v = check(cand, pg["text"], today=today, months_ahead=months_ahead,
                              proximity=int(tc.get("proximity_chars") or 400), extra_tokens=extra, known_name=known)
                    title = re.sub(r"\s+", " ", str(cand.get("title") or "")).strip()[:160]
                    if strict and res["kind"] == "company" and title and generic_title(title):
                        title = f"{res['label']} {title}"       # "Q3 Earnings Call" on AMD's own site -> "AMD Q3 Earnings Call"
                    elif (strict and seed and seed.get("organiser") and title and generic_title(title)
                          and any(same_event(f"{seed['organiser']} {title}", n) for n in identity_names(seed))):
                        # (review) the item's own event written without its organiser's name on the organiser's page:
                        # "Annual Shareholders' Meeting" on stock.walmart.com is Walmart's annual shareholders' meeting.
                        # Only when the renamed title IS the seed item; any other nameless title is still left out.
                        title = f"{seed['organiser']} {title}"
                    row = {"title": title, "date": v.get("start") or cand.get("start_date"), "end_date": v.get("end"),
                           "verdict": v["verdict"], "why": v["why"], "url": url, "sentence": v.get("sentence"),
                           "location": (re.sub(r"\s+", " ", cand["location"]).strip()[:80] or None)
                           if isinstance(cand.get("location"), str) else None, "model": ans.get("model")}
                    if v["verdict"] == "dropped" or not title or (
                            v["verdict"] == "unconfirmed" and not tc.get("store_unconfirmed", True)):
                        out["dropped"].append(row)
                        continue
                    why_not = screen(title, v, pg["text"], res, seed) if strict else None
                    if why_not:
                        row.update(verdict="dropped", why=why_not)
                        out["dropped"].append(row)
                        continue
                    k = (row["date"], C.norm_title(title)[:40])
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    row["_seed"] = seed
                    row["_reason"] = str(cand.get("reason") or "")[:200]
                    out["found"].append(row)
        if store and out["found"]:
            _store(out["found"], res, subject, today, c)
        for r in out["found"]:
            r.pop("_seed", None); r.pop("_reason", None)
        out["ok"] = any(p["ok"] for p in out["pages"])
        if not out["ok"]:
            out["error"] = "None of the pages could be read: " + "; ".join(f"{p['url']} ({p['why']})" for p in out["pages"])[:400]
            out["error_kind"] = "page_failed"
            if res.get("how") in ("remembered", "found") and out["pages"]:
                forget_company_page(res["label"], str(out["pages"][0].get("why")))
        elif not out["found"]:
            out["error_kind"] = "nothing_found"
        if out["ok"] and source == "tool_weekly" and res.get("how") in ("remembered", "found") and out["pages"]:
            p0 = out["pages"][0]                       # (review) the remembered page itself: loaded, but no date ahead
            note_company_page_empty(res["label"], bool(p0.get("ok") and not out["found"] and
                                                       p0.get("why") == "no upcoming date is written on the page"), tc)
    except Exception as e:
        log.warning(f"[calendar] find_events({subject!r}) failed: {type(e).__name__}: {e}")
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["error_kind"] = "crashed"
    return out


def _store(found: list, res: dict, subject: str, today: date, c: dict) -> None:
    tc = tool_cfg(c)
    merge = tc.get("merge_duplicates", True) is not False
    earn_twins = tc.get("earnings_twins", True) is not False        # (leftovers 2) see _twin
    conn = mason_db.get_connection()
    try:
        with conn:
            for r in found:
                seed = r.get("_seed")
                d = C.parse_date(r["date"])
                det = {"source_url": r["url"], "sentence": r.get("sentence"), "found_by": r.get("model"),
                       "checked": today.isoformat(), "subject": res["label"], "city": r.get("location"),
                       "check": r["why"]}
                confirmed = r["verdict"] == "confirmed"
                names = identity_names(seed)
                if seed and confirmed and any(same_event(r["title"], n) for n in names if n):
                    # the seed event itself: update its own row, so the "expected" entry becomes the real date
                    per_year = sum(1 for o in seed.get("next") or [] if str(o.get("start") or o.get("expected") or "")[:4] == str(d.year))
                    ykey = str(d.year) if per_year <= 1 else f"{d.year}-{d.month:02d}"
                    det.update(seed_id=seed["id"], organiser=seed.get("organiser"), official_url=seed.get("url"),
                               sectors=seed.get("sectors") or [])
                    sub = str(seed.get("why") or "")
                    if r.get("location"):
                        sub = (sub.rstrip(". ") + f". {r['location']}.") if sub else str(r["location"])
                    ev = {"source": "Event list", "type": seed.get("type") or "conference", "kind": seed.get("kind") or "conference",
                          "key": f"seed|{seed['id']}|{ykey}", "title": seed["name"], "date": r["date"], "end_date": r.get("end_date"),
                          "tickers": seed.get("tickers") or [], "confirmed": True, "importance": int(seed.get("importance") or 1),
                          "subtitle": sub, "url": r["url"], "details": det}
                else:
                    ev = {"source": SOURCE, "type": res["type"] if res["kind"] == "company" else (seed or {}).get("type", res["type"]),
                          "kind": "company_event" if res["kind"] == "company" else "found_event",
                          "key": f"found|{C.norm_title(res['label'])[:40]}|{C.norm_title(r['title'])[:60]}|{d.year}",
                          "title": r["title"], "date": r["date"], "end_date": r.get("end_date"),
                          "tickers": res["tickers"] or (seed or {}).get("tickers") or [], "confirmed": confirmed,
                          "importance": 2 if res["kind"] == "company" else 1,
                          "subtitle": r.get("_reason") or "", "url": r["url"], "details": det}
                    if merge:
                        det["subjects"] = [res["label"]]
                        cid = C.event_id(SOURCE, C._natural_key(ev))
                        twin = _twin(conn, ev["title"], ev["date"], cid,
                                     earnings_owner(res, seed, ev["title"], ev["tickers"]) if earn_twins else None)
                        if twin is not None and twin["id"] != cid:
                            r["stored"], r["id"] = _merge_into(conn, twin, ev), twin["id"]
                            continue
                        if twin is not None:            # its own row from an earlier week: keep who else found it
                            old = C._loads(twin["details_json"])
                            ev["tickers"] = list(ev["tickers"]) + [t for t in old.get("tickers") or [] if t not in ev["tickers"]]
                            det["subjects"] = list(dict.fromkeys(det["subjects"] + list(old.get("subjects") or [])))
                state, cid = C.upsert_event(conn, ev, today=today, c=c)
                r["stored"], r["id"] = state, cid
    finally:
        conn.close()


# (2026-11-16, leftovers 2) a company's earnings call / release / results, as an investor site titles it
# ("AMD Q4 2026 Earnings Call", "FY2027 Q3 Earnings Release", "Third Quarter Fiscal 2027 Financial Results",
# "Fourth Quarter and Full Year 2026 Results"); "Phase 3 results" or "Annual Meeting" are not.
_EARNINGS_TITLE_RE = re.compile(r"\bearnings\b|\bfinancial results\b|\b(?:q[1-4]|quarter(?:ly)?|full[- ]year|"
                                r"fiscal(?: year)?|fy\s?\d{2,4})\b(?:\W+(?:fy\s?)?\d{2,4})?\W+results\b", re.I)
# (leftovers 2 review) a title that also names another event ("Investor Day and Q4 Results", "Q4 2026 Earnings Call
# & Capital Markets Day", "Annual Meeting of Shareholders and Q1 Results") is more than the earnings row says: it
# is stored as before, so the day / meeting is not lost under the earnings row.
_OTHER_EVENT_RE = re.compile(r"\b(?:days?|meetings?|keynotes?|summits?|symposium|showcase|forum|convention|expo)\b", re.I)


def earnings_title(title: str) -> bool:
    """An earnings call / release / results and nothing else (see _OTHER_EVENT_RE)."""
    t = str(title or "")
    return bool(_EARNINGS_TITLE_RE.search(t)) and not _OTHER_EVENT_RE.search(t)


def earnings_owner(res: dict, seed, title: str, tickers=None) -> list:
    """(leftovers 2 review) [the ticker whose earnings row a find titled `title` can be the same event as], or [].
    A company subject: its own ticker (the first of `tickers`, else of res['tickers']). A seed or theme item: its
    first ticker only when that ticker is written as a word in the item's name or aliases or in the find's title
    ("AMD Advancing AI", alias "AMD", is AMD's); a seed list's first ticker is otherwise not the item's own company
    ("Samsung Galaxy Unpacked" lists QCOM first, a conference lists whoever presents)."""
    tk = C._clean_tickers(tickers if tickers is not None else (res or {}).get("tickers"))[:1]
    if not tk or (res or {}).get("kind") == "company":
        return tk
    names = [title] + ([seed.get("name")] + list(seed.get("aliases") or []) if isinstance(seed, dict) else [])
    word = re.compile(r"(?<![A-Za-z0-9])" + re.escape(tk[0]) + r"(?![A-Za-z0-9])")
    return tk if any(word.search(str(n or "")) for n in names) else []


def _twin(conn, title: str, day: str, cid: str, tickers=None):
    """The row already on the calendar for this event, or None: its own row (same id), else a row of the
    seed list or of this tool on the same date whose name is the same event either way round
    ("Barclays 24th Annual Global Technology Conference" = "Barclays Global Technology Conference"), else
    (2026-11-16, leftovers 2) for an earnings call / release (earnings_title), the earnings feeds' row of the
    finding's own ticker (the first of `tickers`, which callers take from earnings_owner: a company subject's own
    ticker, a seed item's first only when the item names it) on that date: mason_pead's 'Robinhood' row
    (event_type 'earnings', the ticker in the `ticker` column) or the bellwether feed's row ('cal_earnings', the
    ticker in details_json).
    A row the owner removed counts (it stays removed); one a feed retired does not."""
    want = set(C._clean_tickers(tickers)[:1]) if tickers and earnings_title(title) else set()
    earn = " OR event_type IN ('earnings', 'cal_earnings')" if want else ""
    rows = [dict(x) for x in conn.execute(
        "SELECT id, source, event_type, ticker, title, status, details_json, record_deleted, deleted_by "
        "FROM calendar_events WHERE id=? OR (event_date=? AND (source IN ('Event list', ?)" + earn + ")) "
        "ORDER BY id=? DESC, source, first_seen", (cid, day, SOURCE, cid)).fetchall()]
    if want:        # after its own row, the earnings feed's row comes before an older events-tool row of the same call
        rows.sort(key=lambda x: (x["id"] != cid, x["source"] in ("Event list", SOURCE)))
    for x in rows:
        if x["id"] == cid:
            return x
        if x["record_deleted"] and str(x.get("deleted_by") or "").startswith("feed"):
            continue
        det = C._loads(x["details_json"])
        if x["source"] not in ("Event list", SOURCE):          # an earnings feed's row (only read when `want`)
            if want & set(C._clean_tickers(det.get("tickers")) or C._clean_tickers([x["ticker"]])):
                return x
            continue
        if det.get("expected_month"):
            continue        # (review) a seed item's "expected <month>" placeholder on the 1st is not a date for that event
        if same_event(title, x["title"]) or same_event(x["title"], title):
            return x
    return None


def _merge_into(conn, twin: dict, ev: dict) -> str:
    """A second finding of an event already on the calendar. A seed-list row, or one the owner removed,
    is left as it is ('already listed' / 'hidden'); a row of this tool gets the new subject and its
    tickers added (and becomes confirmed when this finding is)."""
    if twin["record_deleted"]:
        return "hidden"
    if twin["source"] != SOURCE:
        return "already listed"
    det = C._loads(twin["details_json"])
    tk = list(det.get("tickers") or [])
    had = list(det.get("subjects") or ([det["subject"]] if det.get("subject") else []))
    new = dict(det, tickers=tk + [t for t in C._clean_tickers(ev.get("tickers")) if t not in tk],
               subjects=list(dict.fromkeys(had + list(ev["details"].get("subjects") or []))))
    status = twin["status"]
    if ev.get("confirmed") and status == "unconfirmed":
        status, new["confirmed"], new["check"] = "confirmed", True, ev["details"].get("check")
    if new == det and status == twin["status"]:
        return "same"
    conn.execute("UPDATE calendar_events SET details_json=?, status=?, last_updated=? WHERE id=?",
                 (C._dumps(new), status, time.time(), twin["id"]))
    return "merged"


def summary_text(res: dict) -> str:
    """A find_events result in plain words (for the chat tool and the command line)."""
    if res.get("error") and not res.get("found"):
        return f"Could not look up events for {res.get('subject')}: {res['error']}"
    lines = []
    for r in res.get("found") or []:
        when = r["date"] + (f" to {r['end_date']}" if r.get("end_date") else "")
        tag = "confirmed on the official page" if r["verdict"] == "confirmed" else f"not confirmed ({r['why']})"
        lines.append(f"- {when}: {r['title']} — {tag}. Source: {r['url']}")
    n_drop = len(res.get("dropped") or [])
    head = f"Events found for {res.get('subject')}: {len(lines)} added to the calendar"
    if n_drop:
        head += f", {n_drop} left out because the page did not back them up"
    if not lines:
        read = [p["url"] for p in res.get("pages") or [] if p.get("ok")]
        return head + ". Pages read: " + (", ".join(read) or "none") + "."
    return head + ":\n" + "\n".join(lines)


# ── the weekly job and the monthly re-check ─────────────────────────────────────

def weekly_subjects(tc: dict | None = None) -> list:
    """Seed events with no confirmed upcoming date first, then holdings and watchlist
    companies, then the standing subjects from the config."""
    tc = tc if tc is not None else tool_cfg()
    out = []
    for s in _seed_index():
        if not any(o.get("start") and o.get("confirmed") is not False and str(o["start"]) >= C.today_et().isoformat()
                   for o in s.get("next") or []):
            out.append(s["id"])
    bk = C.book()
    tickers = sorted(t for t in (bk["held"] | bk["watched"]) if re.match(r"^[A-Z]{1,5}$", t))
    if tc.get("skip_funds", True) is not False:        # (2026-11-16) ETFs and funds have no events page
        skipped = {}
        try:
            conn = mason_db.get_connection()
        except Exception:
            conn = None
        try:
            for t in tickers:
                why = fund_reason(t, conn)
                if why:
                    skipped[t] = why
        finally:
            if conn is not None:
                conn.close()
        if skipped:
            log.info("[calendar] weekly events run leaves out " + ", ".join(f"{t} ({w})" for t, w in skipped.items()))
        tickers = [t for t in tickers if t not in skipped]
    out += tickers
    out += [str(x) for x in tc.get("subjects") or [] if str(x).strip()]
    return list(dict.fromkeys(out))


def recheck_subjects() -> list:
    """Stored unconfirmed / expected events that are still ahead: the subject to search again."""
    out = []
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT details_json FROM calendar_events WHERE record_deleted=0 AND event_date >= ? "
                                "AND source IN ('Event list', ?) AND status='unconfirmed'",
                                (C.today_et().isoformat(), SOURCE)).fetchall()
        finally:
            conn.close()
        for r in rows:
            d = C._loads(r[0])
            s = d.get("seed_id") or d.get("subject")
            if s and s not in out:
                out.append(s)
    except Exception:
        pass
    return out


def _in_window(now: float, win) -> bool:
    try:
        t = datetime.fromtimestamp(now, tz=C.ET).strftime("%H:%M")
        a, b = str(win[0]), str(win[1])
        return (a <= t <= b) if a <= b else (t >= a or t <= b)
    except Exception:
        return True


def _run_queue(key: str, subjects_fn, every_days: float, tc: dict, now: float, force: bool) -> dict | None:
    conn = mason_db.get_connection()
    try:
        st = C.feed_state(conn, key)
    finally:
        conn.close()
    queue = list(st.get("queue") or [])
    if not queue:
        if not force and st.get("started") and now - float(st["started"]) < every_days * 86400:
            return None
        queue = subjects_fn()
        st.update(started=now, total=len(queue), done=0, found=0, errors=0, problems=[], by_kind={})
    t0, ran = time.monotonic(), 0
    _disc_run["used"] = 0                              # calendar.tool.discover_max_per_run is per pass
    while queue and time.monotonic() - t0 < float(tc.get("pass_budget_s") or 600):
        subj = queue.pop(0)
        r = find_events(subj, source=key)
        ran += 1
        st["done"] = int(st.get("done") or 0) + 1
        st["found"] = int(st.get("found") or 0) + len(r.get("found") or [])
        st["errors"] = int(st.get("errors") or 0) + (1 if r.get("error") else 0)
        kind = r.get("error_kind")
        if kind:                                       # (2026-11-16) which kind of failure each one was
            bk_ = dict(st.get("by_kind") or {})
            bk_[kind] = int(bk_.get(kind) or 0) + 1
            st["by_kind"] = bk_
            why = r.get("why_no_page") if kind == "no_page" else (r.get("error") if kind != "nothing_found" else
                  "; ".join(f"{p['url']}: {p.get('why') or str(p.get('proposed') or 0) + ' proposed, none kept'}"
                            for p in r.get("pages") or [] if p.get("ok")))
            label = {"no_page": "no page known", "page_failed": "page failed to load",
                     "nothing_found": "nothing found", "crashed": "crashed"}.get(kind, kind)
            st["problems"] = (list(st.get("problems") or []) + [{"subject": subj, "kind": label,
                                                                    "why": str(why or "")[:240]}])[-150:]
        if any(p.get("why") and "limit" in str(p["why"]) for p in r.get("pages") or []):
            queue.insert(0, subj)                      # the daily cap was reached: go on tomorrow
            break
    st["queue"] = queue
    res = {"ok": True, "n": int(st.get("found") or 0), "seen": int(st.get("done") or 0)}
    conn = mason_db.get_connection()
    try:
        s2 = C.record_feed(conn, key, res, int((time.monotonic() - t0) * 1000), now)
        s2.update({k: st.get(k) for k in ("queue", "started", "total", "done", "found", "errors", "problems", "by_kind")})
        with conn:
            C._snap_put(conn, C._SNAP_SOURCE, key, s2)
    finally:
        conn.close()
    return {"subjects_this_pass": ran, "left": len(queue), "found": st.get("found")}


def scheduled(now: float | None = None, force: bool = False) -> dict:
    """The weekly search and the monthly re-check, run here and now (mason_calendar.tick reaches this
    through scheduled_in_background). Does nothing unless calendar.tool.enabled is true."""
    if not enabled():
        return {}
    tc = tool_cfg()
    now = time.time() if now is None else now
    if not force and not _in_window(now, tc.get("night_window_et") or ["20:00", "06:00"]):
        return {}
    out = {}
    if tc.get("weekly", True):
        r = _run_queue("tool_weekly", lambda: weekly_subjects(tc), float(tc.get("weekly_days") or 7), tc, now, force)
        if r:
            out["weekly"] = r
    r = _run_queue("tool_recheck", recheck_subjects, float(tc.get("recheck_days") or 30), tc, now, force)
    if r:
        out["recheck"] = r
    return out


# The weekly search and the monthly re-check can each run for pass_budget_s (600 s). They used to run
# inside the hourly maintenance pass, which also carries the nightly database backup; they now get a
# short-lived thread of their own (2026-11-15). One at a time: while a run is alive a new one is not started.
_bg_lock = threading.Lock()
_bg = {"thread": None, "started": None, "last": None, "last_error": None}


def _due(key: str, every_days: float, now: float) -> bool:
    """True when _run_queue(key, ...) would do something: subjects are still waiting, or the interval is over."""
    conn = mason_db.get_connection()
    try:
        st = C.feed_state(conn, key)
    finally:
        conn.close()
    return bool(st.get("queue")) or not st.get("started") or now - float(st["started"]) >= every_days * 86400


def scheduled_in_background(now: float | None = None, force: bool = False) -> dict:
    """What mason_calendar.tick calls. Starts scheduled() in its own daemon thread and returns at once:
    {"started": True}; {"running": True} when the previous run has not finished (never two at once);
    {} when the tool is off, it is outside the night window, or nothing is due (no thread is started).
    The budgets are scheduled()'s own (pass_budget_s per job, the daily dollar cap). A crash inside the
    run is logged once and ends that thread only. Never raises."""
    try:
        if not enabled():
            return {}
        tc = tool_cfg()
        now = time.time() if now is None else now
        if not force and not _in_window(now, tc.get("night_window_et") or ["20:00", "06:00"]):
            return {}
        with _bg_lock:
            t = _bg["thread"]
            if t is not None and t.is_alive():
                return {"running": True}
            if not force and not ((tc.get("weekly", True) and _due("tool_weekly", float(tc.get("weekly_days") or 7), now))
                                  or _due("tool_recheck", float(tc.get("recheck_days") or 30), now)):
                return {}

            def work():
                try:
                    r = scheduled(now=now, force=force)
                    _bg["last"], _bg["last_error"] = r, None
                    if r:
                        log.info(f"[calendar] event search run finished {r}")
                except Exception as e:
                    _bg["last_error"] = f"{type(e).__name__}: {e}"
                    log.warning(f"[calendar] event search run failed: {type(e).__name__}: {e}")
            th = threading.Thread(target=work, name="mason-calendar-tool", daemon=True)
            _bg["thread"], _bg["started"] = th, now
            th.start()
        return {"started": True}
    except Exception as e:
        log.warning(f"[calendar] event search run could not be started: {type(e).__name__}: {e}")
        return {}


def recheck(force: bool = True) -> dict:
    if not enabled():
        return {"ok": False, "error": "The events tool is switched off (calendar.tool.enabled)."}
    r = _run_queue("tool_recheck", recheck_subjects, float(tool_cfg().get("recheck_days") or 30), tool_cfg(), time.time(), force)
    return dict(r or {}, ok=True)
