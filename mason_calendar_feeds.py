"""
mason_calendar_feeds.py — the deterministic feeds behind the events calendar.

No model is called anywhere in this file. Each feed fetches an official source (or
computes dates from a fixed rule), parses it with a pure function, and hands the list to
mason_calendar.sync_events. Each has its own switch (`calendar.feeds.<key>`), its own
interval (`calendar.hours.<key>`) and its own health record (mason_calendar.status()).

  seed      mason_calendar_seed.json: conferences, company events, summits (dates read
            from the organisers' pages; see the file) + the BLS release schedule.
  computed  monthly options expiry, triple witching, 13F deadlines, expected index
            rebalance days, estimated-tax days, Black Friday / Cyber Monday, election day.
  market    Alpaca /v2/calendar (read-only): market holidays and early closes.
  fed       federalreserve.gov: FOMC meetings (fomccalendars.htm) + speeches, testimony,
            minutes, Beige Book (json/calendar.json).
  econ      release schedules: FRED release/dates (BLS releases, free key), the BEA
            calendar file, the Census indicator calendar; ISM days by its usual rule.
  congress  api.congress.gov committee meetings (key in the X-Api-Key header only),
            kept only when they matter to markets (congress_filter).
  court     supremecourt.gov monthly argument calendars (PDF), business / agency cases.
  fda       Federal Register API: FDA advisory committee meeting notices.
  earnings  bellwether earnings dates, read from the earnings_results table that
            mason_pead already fills from Robinhood (no extra Robinhood call by default).

Network rules: one honest client name, short timeouts, a pause between requests to the
same host, nothing retried inside a pass. Tests replace `_get` and never touch the network.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import mason_calendar as C
import mason_db

log = logging.getLogger("mason.calendar.feeds")
BASE = Path(__file__).parent
SEED_FILE = BASE / "mason_calendar_seed.json"          # tests repoint this
UA = "mason-calendar/1.0 (personal research tool)"
_TIMEOUT = 20                                            # seconds without a byte before a read is given up
_CONNECT_TIMEOUT = 10
_MAX_SECONDS = 45                                        # most seconds one request may take in all
_MAX_BYTES = 8_000_000                                   # largest answer that is read (the biggest real one is ~0.6 MB)
_PAUSE_S = 1.0                                           # between two requests to one host
_last_hit: dict = {}
_deadline = [None]                                       # time.monotonic() after which no request is started


def set_deadline(t) -> None:
    """mason_calendar.tick sets this for the length of one pass (None = no limit), so the
    hourly maintenance thread is never held longer than the pass budget plus one request."""
    _deadline[0] = t


def out_of_time() -> bool:
    return _deadline[0] is not None and time.monotonic() >= _deadline[0]


class _Body:
    """What _get returns: the whole body, already read and bounded."""
    ok = True

    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status

    def json(self):
        return json.loads(self.content.decode("utf-8-sig"))

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
_MON = {m: i + 1 for i, m in enumerate(MONTHS)}
_MON.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})
_MON["sept"] = 9


# ── HTTP (never raises; tests monkeypatch _get) ────────────────────────────────

def _get(url, *, params=None, headers=None, timeout=_TIMEOUT):
    """GET -> an object with .content / .json() (2xx with a body) or None. Bounded three
    ways: no request starts after the pass deadline, one request never takes more than
    _MAX_SECONDS in all (a server that trickles bytes cannot hold the thread), and at
    most _MAX_BYTES are read."""
    try:
        if out_of_time():
            return None
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        wait = _PAUSE_S - (time.monotonic() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        h = {"User-Agent": UA, "Accept": "*/*"}
        h.update(headers or {})
        t_end = time.monotonic() + _MAX_SECONDS
        with requests.get(url, params=params, headers=h, timeout=(_CONNECT_TIMEOUT, timeout), stream=True) as r:
            _last_hit[host] = time.monotonic()
            if not r.ok:
                return None
            # read1 hands back what has arrived (one socket read, bounded by the read timeout), so the
            # clock is looked at between reads; iter_content / read(n) would wait for n bytes and a
            # server that sends one byte at a time could hold the thread for hours
            read1 = getattr(r.raw, "read1", None)
            buf = bytearray()
            while True:
                chunk = read1(65536, decode_content=True) if read1 else r.raw.read(65536, decode_content=True)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_BYTES or time.monotonic() > t_end:
                    return None
            return _Body(bytes(buf), r.status_code) if buf else None
    except Exception:
        return None


def _text(r) -> str:
    try:
        return r.content.decode("utf-8-sig", "replace")
    except Exception:
        return ""


def _strip_tags(h: str) -> str:
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def _write(fn):
    """Run fn(conn) inside one short write transaction on a fresh connection."""
    conn = mason_db.get_connection()
    try:
        with conn:
            return fn(conn)
    finally:
        conn.close()


def _add(total: dict, part: dict) -> dict:
    for k in ("new", "changed", "same", "hidden", "retired", "n"):
        total[k] = total.get(k, 0) + int(part.get(k) or 0)
    total.setdefault("moved", []).extend(part.get("moved") or [])
    return total


# ── calendar arithmetic ─────────────────────────────────────────────────────────

def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31
    da = (h + l - 7 * m + 114) % 31 + 1
    return date(y, mo, da)


def nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month; n=-1 is the last one."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(days=7 * (n - 1))
    d = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """NYSE rule for a fixed-date holiday: Sunday -> Monday, Saturday -> Friday, except
    that a Saturday New Year's Day is not observed at all."""
    if d.weekday() == 6:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return None if (d.month == 1 and d.day == 1) else d - timedelta(days=1)
    return d


def market_holidays(y: int) -> dict:
    """{date: name} of the regular NYSE full-day closures in a year, by rule. Used to NAME
    the closures Alpaca reports and as the business-day test for computed dates."""
    out = {}
    for d, name in ((date(y, 1, 1), "New Year's Day"), (date(y, 6, 19), "Juneteenth"),
                    (date(y, 7, 4), "Independence Day"), (date(y, 12, 25), "Christmas Day")):
        o = _observed(d)
        if o:
            out[o] = name
    ny = _observed(date(y + 1, 1, 1))
    if ny and ny.year == y:
        out[ny] = "New Year's Day (observed)"
    out[nth_weekday(y, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[nth_weekday(y, 2, 0, 3)] = "Presidents' Day"
    out[_easter(y) - timedelta(days=2)] = "Good Friday"
    out[nth_weekday(y, 5, 0, -1)] = "Memorial Day"
    out[nth_weekday(y, 9, 0, 1)] = "Labor Day"
    out[nth_weekday(y, 11, 3, 4)] = "Thanksgiving Day"
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def nth_business_day(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    k = 0
    while True:
        if is_business_day(d):
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


def _months(today: date, n: int):
    y, m = today.year, today.month
    for _ in range(n + 1):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _horizon(today: date, c: dict) -> date:
    return today + timedelta(days=int(c.get("months_ahead") or 12) * 31)


# ══════════════════════════════════════════════════════════════════════════════
# computed
# ══════════════════════════════════════════════════════════════════════════════

def computed_events(today: date, c: dict) -> list:
    end = _horizon(today, c)
    ev = []

    def add(type_, kind, d, title, subtitle, *, imp=2, confirmed=True, tickers=None, key=None, url=None):
        if today <= d <= end:
            ev.append({"type": type_, "kind": kind, "date": d.isoformat(), "title": title, "subtitle": subtitle,
                       "importance": imp, "confirmed": confirmed, "tickers": tickers or [],
                       "key": key or f"{kind}|{d.year}-{d.month:02d}", "url": url})

    for y, m in _months(today, int(c.get("months_ahead") or 12)):
        fri = nth_weekday(y, m, 4, 3)
        exp = fri if is_business_day(fri) else fri - timedelta(days=1)
        if m in (3, 6, 9, 12):
            add("expiry", "triple_witching", exp, "Triple witching",
                "Quarterly expiry of stock options, index options and index futures on the same day; volume and "
                "swings are usually larger, most of all into the close.")
            add("market", "index_rebalance", exp, "S&P 500 quarterly rebalance (expected)",
                "By S&P's usual rule, index changes take effect after this day's close; index funds trade at the "
                "close. S&P announces the changes about two weeks before.", confirmed=False,
                key=f"sp_rebalance|{y}-{m:02d}", url="https://www.spglobal.com/spdji/en/")
            if m == 12:
                add("market", "index_rebalance", exp, "Nasdaq-100 annual reconstitution (expected)",
                    "By Nasdaq's usual rule the yearly changes to the Nasdaq-100 take effect after this day's close.",
                    confirmed=False, key=f"ndx_recon|{y}", tickers=["QQQM"], url="https://www.nasdaq.com/solutions/global-indexes")
        else:
            add("expiry", "opex", exp, "Monthly options expiry",
                "Standard monthly stock and index options expire today.")
        if m == 11:
            tg = nth_weekday(y, 11, 3, 4)
            retail = ["WMT", "AMZN", "TGT", "COST"]
            add("market", "retail_date", tg + timedelta(days=1), "Black Friday",
                "Start of the holiday shopping season; early sales reads move retailers.", imp=1, tickers=retail,
                key=f"black_friday|{y}")
            add("market", "retail_date", tg + timedelta(days=4), "Cyber Monday",
                "Biggest online shopping day of the year; sales figures come out that evening and the next day.",
                imp=1, tickers=retail, key=f"cyber_monday|{y}")
            if y % 2 == 0:
                el = nth_weekday(y, 11, 0, 1) + timedelta(days=1)
                add("congress", "election", el, "U.S. election day",
                    ("Presidential and congressional elections." if y % 4 == 0 else "Congressional midterm elections."),
                    imp=3, key=f"election|{y}")
    # 13F: 45 days after each quarter end, pushed to the next business day
    for y in range(today.year - 1, end.year + 1):
        for (qm, qd, q) in ((3, 31, "Q1"), (6, 30, "Q2"), (9, 30, "Q3"), (12, 31, "Q4")):
            due = next_business_day(date(y, qm, qd) + timedelta(days=45))
            add("expiry", "deadline_13f", due, f"13F filing deadline ({q} {y} holdings)",
                "Funds managing over $100M must disclose their quarter-end stock holdings by today; most file on "
                "the last day, so the big names' positions come out this evening.", key=f"13f|{y}{q}",
                url="https://www.sec.gov/divisions/investment/13ffaq")
    # estimated tax days (individuals): Jan 15, Apr 15, Jun 15, Sep 15, pushed to a business day
    for y in range(today.year, end.year + 1):
        for (m, d, lab) in ((1, 15, "fourth-quarter"), (4, 15, "first-quarter"), (6, 15, "second-quarter"),
                            (9, 15, "third-quarter")):
            due = next_business_day(date(y, m, d))
            name = "Tax day and first-quarter estimated tax due" if m == 4 else f"Estimated tax payment due ({lab})"
            add("expiry", "tax_date", due, name, "Federal income tax payment date for individuals.", imp=1,
                key=f"tax|{y}-{m:02d}", url="https://www.irs.gov/faqs/estimated-tax")
    return ev


def feed_computed(today: date, c: dict) -> dict:
    ev = computed_events(today, c)
    tot = {"ok": True}
    for t in sorted({e["type"] for e in ev}):
        part = _write(lambda conn, t=t: C.sync_events(conn, [e for e in ev if e["type"] == t], source="Computed",
                                                      type_=t, today=today, c=c))
        _add(tot, part)
    return tot


# ══════════════════════════════════════════════════════════════════════════════
# market holidays and early closes (Alpaca, read-only)
# ══════════════════════════════════════════════════════════════════════════════

def parse_alpaca_calendar(rows, start: date, end: date) -> list:
    """Alpaca lists the days the market is OPEN. A weekday missing from the list is a
    holiday; a day whose close is not 16:00 is an early close."""
    open_days = {}
    for r in rows if isinstance(rows, list) else []:
        d = C.parse_date(r.get("date"))
        if d:
            open_days[d] = str(r.get("close") or "16:00")
    if not open_days:
        return []
    lo, hi = max(start, min(open_days)), min(end, max(open_days))
    ev, d = [], lo
    while d <= hi:
        if d.weekday() < 5 and d not in open_days:
            name = market_holidays(d.year).get(d, "Market holiday")
            ev.append({"type": "market", "kind": "holiday", "date": d.isoformat(), "key": f"holiday|{d.isoformat()}",
                       "title": f"Market closed: {name}", "subtitle": "U.S. stock and options markets are closed.",
                       "importance": 2, "confirmed": True, "url": "https://www.nyse.com/markets/hours-calendars"})
        elif d in open_days and open_days[d] not in ("16:00", "1600"):
            hhmm = C.clock_to_hhmm(open_days[d]) or "13:00"
            h, mi = int(hhmm[:2]), hhmm[3:]
            nice = f"{h - 12 if h > 12 else h}:{mi} {'pm' if h >= 12 else 'am'}"
            ev.append({"type": "market", "kind": "early_close", "date": d.isoformat(), "time_et": hhmm,
                       "key": f"early|{d.isoformat()}", "title": f"Market closes early at {nice} ET",
                       "subtitle": "Shortened session; options stop trading early too.",
                       "importance": 2, "confirmed": True, "url": "https://www.nyse.com/markets/hours-calendars"})
        d += timedelta(days=1)
    return ev


def _alpaca_calendar(start: date, end: date):
    import mason_secrets as S
    whole = C._whole_cfg()
    kid, sec = S.alpaca_keys(cfg=whole)
    if not kid or not sec:
        return None, "no Alpaca keys"
    base = ((whole.get("paper_trading") or {}).get("base_url") or "https://paper-api.alpaca.markets/v2").rstrip("/")
    r = _get(base + "/calendar", params={"start": start.isoformat(), "end": end.isoformat()},
             headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
    if r is None:
        return None, "Alpaca calendar did not answer"
    try:
        return r.json(), None
    except Exception:
        return None, "Alpaca calendar answer was not JSON"


def feed_market(today: date, c: dict) -> dict:
    end = _horizon(today, c)
    rows, err = _alpaca_calendar(today, end)
    if rows is None:
        return {"ok": False, "error": err}
    ev = parse_alpaca_calendar(rows, today, end)
    if not rows:
        return {"ok": False, "error": "Alpaca calendar was empty"}
    part = _write(lambda conn: C.sync_events(conn, ev, source="Alpaca", type_="market", today=today, c=c))
    return dict(part, ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Federal Reserve
# ══════════════════════════════════════════════════════════════════════════════

FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_JSON_URL = "https://www.federalreserve.gov/json/calendar.json"
_FOMC_ROW_RE = re.compile(
    r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<(.*?)"
    r"(?=fomc-meeting__month|\Z)", re.S)


def parse_fomc_page(page: str) -> list:
    """[{year, month, first_day, decision_day, projections, press_conference, note}] for
    every meeting listed on the Fed's calendar page. The decision is announced on the
    LAST day. A '*' marks a meeting with a Summary of Economic Projections; in a year
    where no meeting has one yet, `projections` is None (not published)."""
    out = []
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4}) FOMC Meetings", page or "")]
    for i, (pos, year) in enumerate(heads):
        seg = page[pos: heads[i + 1][0] if i + 1 < len(heads) else len(page)]
        rows = []
        for mon, days, rest in _FOMC_ROW_RE.findall(seg):
            mon_s, days_s = _html.unescape(mon).strip(), _html.unescape(days).strip()
            note = _strip_tags(rest)
            if re.search(r"unscheduled|notation vote|cancel", days_s + " " + note[:80], re.I):
                continue
            star = "*" in days_s
            nums = [int(x) for x in re.findall(r"\d+", days_s)]
            mons = [_MON.get(x.strip().lower()[:3]) for x in re.split(r"[/-]", mon_s)]
            if not nums or not mons or mons[0] is None:
                continue
            m1, m2 = mons[0], (mons[-1] or mons[0])
            try:
                first = date(year, m1, nums[0])
                last = date(year + (1 if m2 < m1 else 0), m2, nums[-1])
            except ValueError:
                continue
            rows.append({"year": year, "month": m1, "first_day": first.isoformat(), "decision_day": last.isoformat(),
                         "star": star, "press_conference": True,
                         "strategy_meeting": "Strategy Meeting" in note[:200]})
        any_star = any(r["star"] for r in rows)
        for r in rows:
            r["projections"] = r.pop("star") if any_star else None
        out += rows
    return out


def fomc_events(meetings: list, today: date) -> list:
    ev = []
    upcoming = sorted(m["decision_day"] for m in meetings if m["decision_day"] >= today.isoformat())
    for m in meetings:
        if m["decision_day"] < today.isoformat():
            continue
        f, d = C.parse_date(m["first_day"]), C.parse_date(m["decision_day"])
        sub = "Rate decision at 2:00 pm ET, press conference at 2:30 pm."
        if m["projections"]:
            sub += " New economic projections and the dot plot come with it."
        tentative = bool(upcoming) and m["decision_day"] != upcoming[0]
        if tentative:
            sub += " The Fed treats this date as tentative until the meeting before it."
        ev.append({"type": "fed", "kind": "fomc_decision", "date": d.isoformat(), "time_et": "14:00",
                   "key": f"fomc|{m['year']}-{m['month']:02d}", "title": "Fed rate decision (FOMC)",
                   "subtitle": sub, "importance": 3, "confirmed": True, "url": FED_FOMC_URL,
                   "details": {"meeting_days": f"{f.strftime('%b %-d')}-{d.strftime('%b %-d') if d.month != f.month else d.day}",
                               "first_day": f.isoformat(), "projections": m["projections"],
                               "press_conference": m["press_conference"], "tentative": tentative}})
    return ev


def _fed_clean(s) -> str:
    return _strip_tags(_html.unescape(_html.unescape(str(s or ""))))


def parse_fed_calendar_json(payload, today: date, speech_days: int = 45) -> list:
    """Speeches, testimony, minutes and the Beige Book from the Fed's calendar JSON.
    Times in that file are Eastern time."""
    ev = []
    end = today + timedelta(days=int(speech_days))
    for e in (payload or {}).get("events") or []:
        try:
            typ = e.get("type")
            mon, days = str(e.get("month") or ""), str(e.get("days") or "")
            if typ not in ("Speeches", "Testimony", "FOMC", "Beige") or not re.match(r"^\d{4}-\d{2}$", mon):
                continue
            day = int(re.findall(r"\d+", days)[-1])
            d = date(int(mon[:4]), int(mon[5:7]), day)
            if d < today:
                continue
            title, desc, loc = _fed_clean(e.get("title")), _fed_clean(e.get("description")), _fed_clean(e.get("location"))
            tm = C.clock_to_hhmm(e.get("time"))
            if typ in ("Speeches", "Testimony"):
                if d > end:
                    continue
                m = re.match(r"^(Speech|Discussion|Testimony|Remarks|Panel Discussion|Conversation|Fireside Chat)\s*-+\s*(.+)$",
                             title, re.I)
                what, who = (m.group(1), m.group(2)) if m else (typ.rstrip("es"), title)
                chair = bool(re.search(r"(?<!Vice )\bChair(man)?\b", who)) and "Vice Chair" not in who
                testimony = typ == "Testimony" or what.lower() == "testimony"
                imp = 3 if (chair and testimony) else (2 if chair or testimony else 1)
                verb = "testifies" if testimony else "speaks"
                ev.append({"type": "fed", "kind": "fed_testimony" if testimony else "fed_speech", "date": d.isoformat(),
                           "time_et": tm, "title": f"Fed: {who} {verb}",
                           "subtitle": "; ".join(x for x in (desc, loc) if x)[:240], "importance": imp, "confirmed": True,
                           "url": "https://www.federalreserve.gov/newsevents/calendar.htm",
                           "details": {"speaker": who, "topic": desc, "place": loc}})
            elif typ == "FOMC" and re.search(r"minutes", title, re.I):
                ev.append({"type": "fed", "kind": "fomc_minutes", "date": d.isoformat(), "time_et": tm or "14:00",
                           "key": f"fomc_minutes|{d.isoformat()}", "pair_key": "fomc_minutes", "pair_days": 10,
                           "title": "Fed meeting minutes", "subtitle": (desc + ". " if desc else "") +
                           "The detailed record of the last rate meeting.", "importance": 2, "confirmed": True,
                           "url": FED_FOMC_URL})
            elif typ == "Beige":
                ev.append({"type": "fed", "kind": "beige_book", "date": d.isoformat(), "time_et": tm or "14:00",
                           "key": f"beige|{d.isoformat()}", "pair_key": "beige", "pair_days": 10, "title": "Fed Beige Book",
                           "subtitle": "The Fed's survey of business conditions in its twelve districts.",
                           "importance": 1, "confirmed": True,
                           "url": "https://www.federalreserve.gov/monetarypolicy/publications/beige-book-default.htm"})
        except Exception:
            continue
    return ev


def feed_fed(today: date, c: dict) -> dict:
    tot, parts, ok_any = {"ok": True}, {}, False
    r = _get(FED_FOMC_URL)
    meetings = parse_fomc_page(_text(r)) if r is not None else []
    ev = fomc_events(meetings, today)
    if ev:
        # The Fed's list only grows. If the page yields fewer upcoming meetings than are stored, the
        # page or the parse is wrong: keep what is stored (nothing is retired) and say so.
        stored = len(C.fomc_decision_days(today=today, upcoming_only=True))
        _add(tot, _write(lambda conn: C.sync_events(conn, ev, source="Federal Reserve", type_="fed",
                                                    kinds={"fomc_decision"}, today=today, c=c,
                                                    retire=len(ev) >= stored)))
        ok_any = True
        parts["fomc_meetings"] = len(ev) if len(ev) >= stored else \
            f"failed: the page lists {len(ev)} upcoming meetings, {stored} are stored; none was removed"
    else:
        parts["fomc_meetings"] = "failed: page not read or no meeting found"
    r = _get(FED_JSON_URL)
    try:
        payload = json.loads(_text(r)) if r is not None else None
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("events"):
        ev2 = parse_fed_calendar_json(payload, today, int((c.get("fed") or {}).get("speech_days") or 45))
        _add(tot, _write(lambda conn: C.sync_events(
            conn, ev2, source="Federal Reserve", type_="fed",
            kinds={"fed_speech", "fed_testimony", "fomc_minutes", "beige_book"}, today=today, c=c)))
        parts["speeches_and_releases"], ok_any = len(ev2), True
    else:
        parts["speeches_and_releases"] = "failed: calendar file not read"
    tot["parts"] = parts
    tot["ok"] = ok_any and not any(str(v).startswith("failed") for v in parts.values())
    if not tot["ok"]:
        tot["error"] = "; ".join(f"{k} {v}" for k, v in parts.items() if str(v).startswith("failed"))
    return tot


# ══════════════════════════════════════════════════════════════════════════════
# economic release schedules
# ══════════════════════════════════════════════════════════════════════════════

FRED_URL = "https://api.stlouisfed.org/fred/release/dates"
BEA_ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
CENSUS_URL = "https://www.census.gov/economic-indicators/calendar-listview.html"
# release key -> (FRED release id, usual time ET, agency, agency page)
FRED_RELEASES = {
    "cpi": (10, "08:30", "BLS", "https://www.bls.gov/schedule/news_release/cpi.htm"),
    "ppi": (46, "08:30", "BLS", "https://www.bls.gov/schedule/news_release/ppi.htm"),
    "jobs": (50, "08:30", "BLS", "https://www.bls.gov/schedule/news_release/empsit.htm"),
    "jolts": (192, "10:00", "BLS", "https://www.bls.gov/schedule/news_release/jolts.htm"),
    "claims": (180, "08:30", "Department of Labor", "https://www.dol.gov/ui/data.pdf"),
    "umich": (91, "10:00", "University of Michigan", "https://www.sca.isr.umich.edu/"),
}
FRED_FALLBACK = {"retail_sales": (9, "08:30", "Census Bureau", "https://www.census.gov/retail/"),
                 "gdp": (53, "08:30", "BEA", "https://www.bea.gov/news/schedule"),
                 "pce": (54, "08:30", "BEA", "https://www.bea.gov/news/schedule")}
_BEA_MAP = [(r"^(Gross Domestic Product|GDP)\b(?!\s+by\b)(?=.*\((Advance|Second|Third))", "gdp"),
            (r"^Personal Income and Outlays", "pce"),
            (r"^U\.S\. International Trade in Goods and Services", "trade")]
_CENSUS_MAP = [(r"^Advance Monthly Sales for Retail", "retail_sales"), (r"^Advance Report on Durable Goods", "durable_goods"),
               (r"^New Residential Construction", "housing_starts"), (r"^New Residential Sales", "new_home_sales")]


def _econ_event(rk: str, d: date, time_et, source: str, url: str, *, confirmed=True, period=None, label=None,
                time_usual=False) -> dict:
    title, why, imp, _ = C.RELEASES[rk]
    if label:
        title = f"{title} — {label}"
    sub = why if not period else f"{why} Covers {period}."
    if not confirmed:
        sub += " Date is by the usual rule, not yet published."
    return {"type": "econ", "kind": "econ_release", "date": d.isoformat(), "time_et": time_et,
            "key": f"econ|{rk}|{d.isoformat()}", "pair_key": f"econ|{rk}", "pair_days": 5 if rk == "claims" else 20,
            "title": title, "subtitle": sub, "importance": imp, "confirmed": confirmed, "url": url,
            "source": source, "details": {"release": rk, "period": period, "time_is_usual": bool(time_usual)}}


def parse_fred_release_dates(payload) -> list:
    out = []
    for x in (payload or {}).get("release_dates") or []:
        d = C.parse_date(x.get("date"))
        if d:
            out.append(d)
    return sorted(set(out))


def unfold_ics(text: str) -> list:
    """VEVENT blocks of an .ics file as {NAME: value} (folded lines joined, params dropped)."""
    text = re.sub(r"\r?\n[ \t]", "", text or "")
    out = []
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        ev = {}
        for line in blk.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                ev[k.split(";")[0].strip().upper()] = v.strip().replace("\\,", ",").replace("\\;", ";").replace("\\n", " ")
        out.append(ev)
    return out


def parse_bea_ics(text: str, today: date) -> list:
    """BEA's calendar file. DTSTART is UTC; it is converted to Eastern time by date, so a
    release at 13:30Z is 08:30 in winter and 12:30Z is 08:30 in summer."""
    ev = []
    for e in unfold_ics(text):
        summ, dt = e.get("SUMMARY", ""), e.get("DTSTART", "")
        rk = next((k for pat, k in _BEA_MAP if re.search(pat, summ)), None)
        m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?Z?$", dt)
        if not rk or not m:
            continue
        utc = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)),
                       tzinfo=timezone.utc) if dt.endswith("Z") else None
        if utc is not None:
            d_iso, hhmm = C.utc_to_et(utc)
        else:
            d_iso, hhmm = f"{m.group(1)}-{m.group(2)}-{m.group(3)}", f"{m.group(4)}:{m.group(5)}"
        d = C.parse_date(d_iso)
        if d is None or d < today:
            continue
        period = summ.split(",", 1)[1].strip() if "," in summ else None
        label = None
        if rk == "gdp":
            mm = re.search(r"\((Advance|Second|Third)[^)]*\)", summ)
            label = (mm.group(1).lower() + " estimate") if mm else None
            q = re.search(r"(\d(?:st|nd|rd|th) Quarter(?: and Year)? \d{4})", summ)
            period = q.group(1) if q else None
        x = _econ_event(rk, d, hhmm, "BEA", "https://www.bea.gov/news/schedule", period=period, label=label)
        if rk == "gdp" and label and not label.startswith("advance"):
            x["importance"] = 2
        ev.append(x)
    return ev


def parse_census_calendar(page: str, today: date) -> list:
    """The Census Bureau's indicator calendar (list view). Each row carries the release
    name and a sort key YYYYMMDDHHMM in Eastern time."""
    ev = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S | re.I):
        m = re.search(r'sorttable_customkey="(\d{12})"', row)
        cells = [_strip_tags(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if not m or len(cells) < 4:
            continue
        rk = next((k for pat, k in _CENSUS_MAP if re.search(pat, cells[0])), None)
        if not rk:
            continue
        s = m.group(1)
        try:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            continue
        if d < today:
            continue
        ev.append(_econ_event(rk, d, f"{s[8:10]}:{s[10:12]}", "Census Bureau",
                              "https://www.census.gov/economic-indicators/calendar-listview.html", period=cells[3] or None))
    return ev


def ism_events(today: date, c: dict) -> list:
    """ISM publishes no free machine-readable schedule. Its long-standing rule: the
    manufacturing index on the first business day of the month and the services index on
    the third, both at 10:00 ET. Stored as NOT confirmed."""
    ev = []
    end = today + timedelta(days=186)
    for y, m in _months(today, 7):
        for rk, n in (("ism_mfg", 1), ("ism_svc", 3)):
            d = nth_business_day(y, m, n)
            if today <= d <= end:
                ev.append(_econ_event(rk, d, "10:00", "ISM (usual rule)", "https://www.ismworld.org/", confirmed=False))
    return ev


def feed_econ(today: date, c: dict) -> dict:
    tot, parts = {"ok": True}, {}
    # BEA
    r = _get(BEA_ICS_URL)
    bea = parse_bea_ics(_text(r), today) if r is not None else []
    bea_ok = bool(bea)
    parts["bea"] = len(bea) if bea_ok else "failed: BEA calendar file not read"
    # Census
    r = _get(CENSUS_URL)
    cen = parse_census_calendar(_text(r), today) if r is not None else []
    cen_ok = bool(cen)
    parts["census"] = len(cen) if cen_ok else "failed: Census calendar not read"
    # FRED (BLS releases; plus stand-ins for a source that failed)
    fred, fred_ok = [], False
    try:
        import mason_secrets as S
        key = S.fred_key()
    except Exception:
        key = ""
    if not key:
        parts["fred"] = "skipped: no FRED key"
    else:
        want = dict(FRED_RELEASES)
        near_retail = any(e["details"].get("release") == "retail_sales" and
                          (C.parse_date(e["date"]) - today).days <= 45 for e in cen)
        if not cen_ok or not near_retail:      # the Census list view shows next year only from mid-November on
            want["retail_sales"] = FRED_FALLBACK["retail_sales"]
        if not bea_ok:
            want.update(gdp=FRED_FALLBACK["gdp"], pce=FRED_FALLBACK["pce"])
        got = 0
        for rk, (rid, tm, agency, url) in want.items():
            rr = _get(FRED_URL, params={"api_key": key, "file_type": "json", "release_id": rid,
                                        "include_release_dates_with_no_data": "true",
                                        "realtime_start": today.replace(day=1).isoformat(),
                                        "realtime_end": "9999-12-31", "sort_order": "asc", "limit": 200})
            try:
                dates = parse_fred_release_dates(rr.json()) if rr is not None else None
            except Exception:
                dates = None
            if dates is None:
                continue
            got += 1
            for d in dates:
                if d >= today:
                    fred.append(_econ_event(rk, d, tm, f"{agency} (via FRED)", url, time_usual=True))
        fred_ok = got == len(want)
        parts["fred"] = len(fred) if fred_ok else f"failed: {len(want) - got} of {len(want)} FRED release lists not read"
    ism = ism_events(today, c)
    parts["ism"] = len(ism)
    groups = [("BEA", bea, bea_ok), ("Census Bureau", cen, cen_ok), ("ISM (usual rule)", ism, True)]
    by_src = {}
    for e in fred:
        by_src.setdefault(e["source"], []).append(e)
    if fred_ok:
        for agency in {v[2] for v in list(FRED_RELEASES.values()) + list(FRED_FALLBACK.values())}:
            groups.append((f"{agency} (via FRED)", by_src.get(f"{agency} (via FRED)", []), True))
    for src, evs, ok in groups:
        if not ok:
            continue
        _add(tot, _write(lambda conn, src=src, evs=evs: C.sync_events(
            conn, evs, source=src, type_="econ", kinds={"econ_release"}, today=today, c=c)))
    tot["parts"] = parts
    failed = [f"{k} {v}" for k, v in parts.items() if str(v).startswith("failed")]
    tot["ok"] = not failed
    if failed:
        tot["error"] = "; ".join(failed)
    return tot


# ══════════════════════════════════════════════════════════════════════════════
# Congress
# ══════════════════════════════════════════════════════════════════════════════

CONGRESS_API = "https://api.congress.gov/v3/committee-meeting"
# The principle: a meeting matters when it can change the rules, taxes, spending or
# enforcement that listed companies live under. Two things decide that: WHO is meeting
# (a committee whose job is money, markets or a regulated industry) and WHAT it is about
# (a topic that moves a sector). Everything below is configurable under calendar.congress.
CONGRESS_DEFAULTS = {
    "congress": 119,
    "max_list_pages": 6,              # 50 meetings per page, newest update first
    "max_details_per_pass": 150,
    "lookback_days": 14,              # first run / after a gap: how far back in updates to read
    # committees whose every hearing and markup is kept (money and markets are their whole job)
    # (matched at the START of the committee's name, so "Appropriations Subcommittee on Financial
    # Services" is not mistaken for the Financial Services Committee)
    "core_committees": ["House Financial Services", "Senate Banking", "House Ways and Means", "Senate Finance",
                        "House Budget", "Senate Budget", "Joint Economic"],
    # committees kept only when the title is about a market topic
    "topic_committees": ["Energy and Commerce", "Commerce, Science, and Transportation", "Judiciary",
                         "Appropriations", "Armed Services", "Agriculture", "Energy and Natural Resources",
                         "Health, Education, Labor, and Pensions", "Science, Space, and Technology",
                         "Foreign Affairs", "Foreign Relations", "Homeland Security", "Oversight", "Small Business",
                         "Natural Resources", "Transportation and Infrastructure", "Environment and Public Works",
                         "Education and Workforce", "Aging", "Rules", "Select Committee on the Strategic Competition"],
    # topic -> words (matched as whole words / phrases, case-insensitive)
    "topics": {
        "chips and export controls": ["semiconductor", "semiconductors", "chips", "export control", "export controls",
                                      "advanced computing", "critical minerals", "rare earth", "supply chain"],
        "AI": ["artificial intelligence", "AI", "data center", "data centers", "machine learning"],
        "crypto": ["digital asset", "digital assets", "crypto", "cryptocurrency", "stablecoin", "stablecoins",
                   "bitcoin", "market structure"],
        "banks and markets": ["bank", "banks", "banking", "capital markets", "securities", "Federal Reserve",
                              "monetary policy", "Basel", "deposit insurance", "credit card", "fintech",
                              "Securities and Exchange Commission", "financial stability", "private credit",
                              "housing finance", "insurance"],
        "drug pricing and health": ["drug pricing", "drug prices", "prescription drug", "pharmaceutical",
                                    "pharmaceuticals", "pharmacy benefit", "Medicare", "Medicaid", "FDA",
                                    "biotech", "biotechnology", "health insurance", "340B"],
        "energy": ["energy", "oil", "natural gas", "LNG", "nuclear", "pipeline", "electric grid", "power grid",
                   "utilities", "permitting", "renewable"],
        "trade and tariffs": ["tariff", "tariffs", "trade", "customs", "sanctions", "import controls", "reshoring"],
        "taxes and budget": ["tax", "taxes", "taxation", "debt limit", "debt ceiling", "shutdown", "appropriations",
                             "continuing resolution", "budget resolution", "reconciliation", "deficit",
                             "Social Security"],
        "antitrust and Big Tech": ["antitrust", "competition", "monopoly", "merger", "mergers", "Big Tech",
                                   "social media", "online platforms", "app store", "content moderation",
                                   "Section 230", "data privacy", "online gaming"],
        "defense buying": ["procurement", "defense industrial base", "shipbuilding", "munitions", "acquisition reform",
                           "National Defense Authorization", "space launch", "missile defense"],
        "transport and autos": ["airline", "airlines", "aviation", "autonomous vehicle", "autonomous vehicles",
                                "electric vehicle", "electric vehicles", "rail", "shipping"],
        "telecom and space": ["spectrum", "broadband", "satellite", "satellites", "space", "NASA", "quantum"],
    },
    # topics strong enough to keep a meeting of ANY committee
    "strong_topics": ["chips and export controls", "AI", "crypto", "drug pricing and health", "trade and tariffs",
                      "antitrust and Big Tech"],
    # titles that are housekeeping whatever the committee
    "skip_title": ["pending calendar business", "organizational meeting", "committee business$", "^hearing$",
                   "lease resolutions", "closed briefing", "intelligence matters"],
    "skip_types": ["Closed Hearing"],
    # a money-and-markets committee's nominations meeting whose title does not name the posts ("certain
    # pending nominations") is kept, at importance 1: what these committees confirm is mostly market
    # regulators (Fed, SEC, FDIC, Treasury, IRS) and the title gives no way to tell (2026-11-16)
    "keep_unnamed_core_nominations": True,
    # a nominations meeting is kept only when one of the posts regulates markets or a listed industry
    "regulator_posts": r"Federal Reserve|Board of Governors|Secretary of the Treasury|Department of the Treasury|"
                       r"Securities and Exchange|Commodity Futures|Comptroller of the Currency|Federal Deposit Insurance|"
                       r"Federal Trade Commission|Antitrust Division|Federal Communications Commission|"
                       r"Federal Energy Regulatory|Nuclear Regulatory|"
                       r"(?<!Assistant )(?<!Deputy )(?<!Under )Secretary of (Commerce|Energy|Labor|Health)|"
                       r"Under Secretary of Commerce for Industry and Security|Trade Representative|"
                       r"Council of Economic Advisers|"
                       r"Consumer Financial Protection|Internal Revenue|Food and Drug|Federal Housing Finance|"
                       r"Export-Import Bank|National Labor Relations Board|Surface Transportation Board",
}


def _congress_cfg(c: dict) -> dict:
    return C._merge(CONGRESS_DEFAULTS, c.get("congress"))


CONGRESS_RULE = 2          # bump when congress_filter changes in a way that should re-judge meetings already read
_FETCH_KEYS = ("congress", "max_list_pages", "max_details_per_pass", "lookback_days")


def _congress_rule(cc: dict) -> str:
    """A fingerprint of the filter: its version and every setting it reads. When it differs from the one
    stored with the cursor, the next pass reads the last `lookback_days` of meeting updates again, once, so
    a meeting read under the old rule is judged by the new one (the cursor alone would skip it)."""
    keep = {k: v for k, v in cc.items() if k not in _FETCH_KEYS}
    return f"{CONGRESS_RULE}:{C.event_id('congress_rule', json.dumps(keep, sort_keys=True, default=str))[:10]}"


def _topic_hits(title: str, topics: dict) -> list:
    hits = []
    for name, words in (topics or {}).items():
        for w in words or []:
            w = str(w)
            flags = 0 if (w.isupper() and len(w) <= 4) else re.I        # 'AI', 'FDA', 'LNG' stay case-sensitive
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", title, flags):
                hits.append(name)
                break
    return hits


def congress_filter(meeting: dict, cc: dict) -> dict | None:
    """Decide whether one committee meeting belongs on the calendar. Returns
    {'reason', 'topics', 'importance'} or None. Pure; the same rule the tests and the
    real-sample report use."""
    title = re.sub(r"\s+", " ", str(meeting.get("title") or "")).strip()
    names = [str(x.get("name") or "") for x in meeting.get("committees") or [] if isinstance(x, dict)]
    comm = "; ".join(n for n in names if n)
    if str(meeting.get("type") or "") in (cc.get("skip_types") or []):
        return None
    if str(meeting.get("meetingStatus") or "").lower().startswith("cancel"):
        return None
    if not title or any(re.search(p, title, re.I) for p in cc.get("skip_title") or []):
        return None
    # A list of nominees is judged by the posts, not by stray words in it: the part of the
    # title before "the nomination(s) of" is read for topics, the part after for posts.
    parts = re.split(r"\b(?:the\s+)?nominations?\s+of\b|\bpending nominations\b", title, maxsplit=1, flags=re.I)
    subject, nominees = parts[0], (parts[1] if len(parts) > 1 else "")
    is_nom = len(parts) > 1 or bool(re.search(r"\bnominations?\b", title, re.I))
    topics = _topic_hits(subject, cc.get("topics") or {})
    core = any(n.lower().startswith(str(k).lower()) for n in names for k in cc.get("core_committees") or [])
    listed = any(k.lower() in comm.lower() for k in cc.get("topic_committees") or [])
    strong = [t for t in topics if t in (cc.get("strong_topics") or [])]
    post = re.search(cc.get("regulator_posts") or r"(?!x)x", nominees or title, re.I) if is_nom else None
    named = bool(re.search(r"\bto be\b", nominees or title, re.I))      # "Jane Doe, of Ohio, to be ..." names the post
    if is_nom and post:
        reason, topics = "nomination to a post that regulates markets", topics or ["banks and markets"]
    elif is_nom and not topics and core and not named and cc.get("keep_unnamed_core_nominations", True) is True:
        reason = "nominations in a money-and-markets committee (the posts are not named)"
    elif is_nom and not topics:
        return None
    elif core:
        reason = "money-and-markets committee"
    elif listed and topics:
        reason = "topic in a committee with a say over it"
    elif strong:
        reason = "strong market topic"
    else:
        return None
    imp = 1
    if (core and topics) or (listed and strong) or len(topics) >= 2:
        imp = 2
    if re.search(r"Secretary of the Treasury|Treasury Secretary|oversight of the (Federal Reserve|Securities and Exchange|"
                 r"Federal Trade|Federal Communications|Food and Drug)", title, re.I):
        imp = max(imp, 2)
    if re.search(r"monetary policy report|semi-?annual monetary policy|Chair(man)? of the (Board of Governors|Federal Reserve)",
                 title, re.I):
        imp = 3
    return {"reason": reason, "topics": topics, "importance": imp}


def congress_event(meeting: dict, verdict: dict) -> dict | None:
    """One kept meeting -> a calendar event. The API's time is UTC; 05:00Z (midnight ET)
    is how it writes "time to be announced"."""
    try:
        raw = str(meeting.get("date") or "")
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        d_iso, hhmm = C.utc_to_et(dt)
        if hhmm == "00:00":
            hhmm = None
    except Exception:
        return None
    chamber = str(meeting.get("chamber") or "")
    names = [str(x.get("name") or "") for x in meeting.get("committees") or [] if isinstance(x, dict) and x.get("name")]
    comm = names[0] if names else "Congress"
    title = re.sub(r"\s+", " ", str(meeting.get("title") or "")).strip().strip("“”\"")
    kind = "markup" if re.search(r"markup|business meeting", str(meeting.get("type") or "") + " " + title, re.I) else "hearing"
    loc = meeting.get("location") or {}
    room = " ".join(str(loc.get(k) or "") for k in ("building", "room")).strip() if isinstance(loc, dict) else ""
    eid = str(meeting.get("eventId") or "")
    ch = {"House": "house", "Senate": "senate"}.get(chamber, "joint")
    status = str(meeting.get("meetingStatus") or "Scheduled")
    sub = f"{comm} {kind}. " + ("About: " + ", ".join(verdict["topics"]) + "." if verdict["topics"] else
                                "A committee that writes the rules for banks, markets, taxes or spending.")
    if status.lower().startswith("postpon"):
        sub += " Postponed; new date not set."
    return {"type": "congress", "kind": kind, "date": d_iso, "time_et": hhmm, "key": f"meeting|{ch}|{eid}",
            "title": title[:240], "subtitle": sub, "importance": verdict["importance"],
            "confirmed": not status.lower().startswith("postpon"),
            "url": f"https://www.congress.gov/event/{meeting.get('congress', '')}th-congress/{ch}-event/{eid}",
            "details": {"committee": comm, "committees": names, "chamber": chamber, "meeting_type": meeting.get("type"),
                        "meeting_status": status, "room": room, "topics": verdict["topics"], "why_kept": verdict["reason"]}}


def feed_congress(today: date, c: dict) -> dict:
    """Reads the list (newest update first) back to the last update already seen, fetches
    the detail of each new or updated meeting, and keeps the ones the filter passes. The
    key goes in the X-Api-Key header only; it is never placed in a URL or a log line."""
    cc = _congress_cfg(c)
    try:
        import mason_secrets as S
        key = S.data_gov_key()
    except Exception:
        key = ""
    if not key:
        return {"ok": False, "error": "no DATA_GOV_API_KEY in the secrets file"}
    hdr = {"X-Api-Key": key}
    conn = mason_db.get_connection()
    try:
        st = C._snap_get(conn, "_calendar_congress", "cursor") or {}
    finally:
        conn.close()
    rule = _congress_rule(cc)
    reread = st.get("rule") != rule                        # the filter changed since the last pass: read the window again
    last = "" if reread else str(st.get("last_update") or "")
    floor = last or (today - timedelta(days=int(cc["lookback_days"]))).strftime("%Y-%m-%dT00:00:00Z")
    todo, newest, pages_ok = [], last, 0
    for page in range(int(cc["max_list_pages"])):
        r = _get(f"{CONGRESS_API}/{int(cc['congress'])}", params={"format": "json", "limit": 50, "offset": page * 50},
                 headers=hdr)
        try:
            rows = r.json().get("committeeMeetings") if r is not None else None
        except Exception:
            rows = None
        if rows is None:
            break
        pages_ok += 1
        stop = False
        for m in rows:
            upd = str(m.get("updateDate") or "")
            if upd <= floor:
                stop = True
                break
            newest = max(newest, upd)
            todo.append(m)
        if stop or len(rows) < 50:
            break
    if not pages_ok:
        return {"ok": False, "error": "congress.gov list did not answer"}
    # oldest first: a pass that is cut short (time budget, a failed read, more than the cap)
    # resumes where it stopped instead of starting over or skipping the older ones
    todo.sort(key=lambda m: str(m.get("updateDate") or ""))
    kept, seen, failed, drop, done = [], 0, 0, [], []
    for m in todo[: int(cc["max_details_per_pass"])]:
        if out_of_time():
            break
        ch = str(m.get("chamber") or "").lower()
        ch = ch if ch in ("house", "senate") else "nochamber"
        r = _get(f"{CONGRESS_API}/{int(cc['congress'])}/{ch}/{m.get('eventId')}", params={"format": "json"}, headers=hdr)
        try:
            det = r.json().get("committeeMeeting") if r is not None else None
        except Exception:
            det = None
        if not isinstance(det, dict):
            failed += 1
            break
        seen += 1
        done.append(str(m.get("updateDate") or ""))
        v = congress_filter(det, cc)
        ev = congress_event(det, v) if v else None
        if ev and ev["date"] >= today.isoformat():
            kept.append(ev)
        elif det.get("eventId"):
            chk = {"House": "house", "Senate": "senate"}.get(str(det.get("chamber") or ""), "joint")
            # a meeting that still passes but has taken place (congress.gov updates it afterwards, e.g. to add
            # the video) only takes off a row still dated in the future; a row of a past day stays, so a
            # re-read after a rule change does not wipe the last weeks (review 2026-11-16)
            drop.append((C.event_id("congress.gov", f"meeting|{chk}|{det['eventId']}"), bool(ev)))

    left = todo[len(done):]                                # not read this pass
    if not left:
        cursor = newest
    else:                                                  # the last update that is wholly behind us
        nxt = str(left[0].get("updateDate") or "")
        cursor = max((u for u in done if u < nxt), default="")

    def w(conn):
        tot = {"new": 0, "changed": 0, "same": 0, "hidden": 0, "retired": 0, "moved": []}
        for ev in kept:
            ev = dict(ev, source="congress.gov", type="congress")
            state, cid = C.upsert_event(conn, ev, today=today, c=c)
            if state == "moved":
                d = C._loads(conn.execute("SELECT details_json FROM calendar_events WHERE id=?", (cid,)).fetchone()[0])
                tot["moved"].append({"id": cid, "title": ev["title"], "from": d.get("prev_date"), "to": ev["date"],
                                     "importance": ev["importance"], "tickers": []})
            elif state in tot:
                tot[state] += 1
        for cid, took_place in drop:                       # cancelled, or changed so it no longer passes
            row = conn.execute("SELECT event_date FROM calendar_events WHERE id=?", (cid,)).fetchone()
            if took_place and str((row or [""])[0] or "") < today.isoformat():
                continue                                   # it took place: the row of that day stays
            if C.soft_delete(conn, cid, "feed:gone"):
                tot["retired"] += 1
        old = str(st.get("last_update") or "")
        # after a whole re-read the cursor never moves back; a re-read cut short keeps its own place
        mark = max(cursor or "", old) if (reread and not left) else cursor
        if mark and (reread or mark > old):
            C._snap_put(conn, "_calendar_congress", "cursor", {"last_update": mark, "rule": rule})
        return tot
    tot = _write(w)
    conn = mason_db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM calendar_events WHERE source='congress.gov' AND record_deleted=0 "
                         "AND event_date >= ?", (today.isoformat(),)).fetchone()[0]
    finally:
        conn.close()
    tot.update(ok=not left, n=n, seen=seen, kept=len(kept))
    if left:
        tot["error"] = f"{len(left)} meeting detail(s) not read yet; the next pass goes on from there"
    return tot


# ══════════════════════════════════════════════════════════════════════════════
# Supreme Court argument calendar
# ══════════════════════════════════════════════════════════════════════════════

SCOTUS_INDEX = "https://www.supremecourt.gov/oral_arguments/calendarsandlists.aspx"
SCOTUS_BASE = "https://www.supremecourt.gov/oral_arguments/"
# The calendar gives only the case name, so the test is WHO the parties are: a company,
# a market regulator, or the President / a department whose power over business is the
# question. It cannot tell what a case is about; add words under calendar.court.extra.
COURT_DEFAULTS = {
    "company": r"\b(INC|CORP|CORPORATION|CO|LLC|L\.L\.C|LTD|L\.P|LP|PLC|N\.A|COMPANY|BANK|HOLDINGS|GROUP|"
               r"PHARMACEUTICALS?|LABORATORIES|TECHNOLOGIES|INDUSTRIES|AIRLINES|RAILWAY|RAILROAD|INSURANCE|"
               r"COMMUNICATIONS|ENERGY|PETROLEUM|ASSOCIATION|ASSN)\b",
    "regulator": r"\b(SEC|SECURITIES AND EXCHANGE|FTC|FEDERAL TRADE|FCC|FEDERAL COMMUNICATIONS|FERC|"
                 r"FEDERAL ENERGY|EPA|ENVIRONMENTAL PROTECTION|NLRB|NATIONAL LABOR RELATIONS|CFPB|CONSUMER FINANCIAL|"
                 r"FDA|FOOD AND DRUG|FDIC|COMPTROLLER|FEDERAL RESERVE|TREASURY|COMMERCE|INTERNAL REVENUE|"
                 r"COMMISSIONER OF INTERNAL|PATENT|CFTC|COMMODITY FUTURES|DEPARTMENT OF LABOR|TRADE COMMISSION|"
                 r"TRADE REPRESENTATIVE|CUSTOMS|HEALTH AND HUMAN|NUCLEAR REGULATORY|SURFACE TRANSPORTATION)\b",
    "executive": r"\b(TRUMP|PRESIDENT OF THE UNITED STATES)\b",
    # an "Inc." that is a charity, church, school or campaign group is not a business case
    "not_company": r"\b(CHURCH|MISSIONARIES|MINISTRIES|FOUNDATION|COALITION|COMMITTEE|ALLIANCE|SOCIETY|UNIVERSITY|"
                   r"SCHOOL|COLLEGE|PARTNERS FOR|CITIZENS|CARE|PARENTHOOD|DIOCESE|ARCHDIOCESE|LEAGUE|CAMPAIGN)\b",
    "extra": [],                       # more words / case names the owner wants kept
}


def pdf_text(raw: bytes) -> str:
    """Text of a simple PDF (the Court's calendars are one): inflate every stream and read
    the strings shown by Tj / TJ. No PDF library is installed; this is enough for these files."""
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw or b"", re.S):
        try:
            s = zlib.decompress(m.group(1))
        except Exception:
            continue
        if b"BT" not in s:
            continue
        for tok in re.finditer(rb"\[((?:\\.|[^\]\\])*)\]\s*TJ|\(((?:\\.|[^)\\])*)\)\s*Tj|(T\*|ET)", s):
            if tok.group(1) is not None:
                for st, num in re.findall(rb"\(((?:\\.|[^)\\])*)\)|(-?\d+\.?\d*)", tok.group(1)):
                    if num:
                        if float(num) < -200:
                            parts.append(b" ")
                    else:
                        parts.append(st)
            elif tok.group(2) is not None:
                parts.append(tok.group(2))
            # line and block ends add nothing: these files carry their own spaces, and a
            # word is often split across two blocks ("Novem" + "ber")
    b = b"".join(parts)
    b = re.sub(rb"\\([()\\])", rb"\1", b)
    b = re.sub(rb"\\(\d{3})", lambda mm: bytes([int(mm.group(1), 8)]), b)
    return re.sub(r"\s+", " ", b.decode("cp1252", "replace")).strip()


def parse_scotus_calendar(text: str) -> list:
    """[{date, docket, case}] from one monthly argument calendar. The page has two columns,
    so a long case name that wraps can come out cut short; the docket number is exact."""
    text = re.sub(r"\s+", " ", text or "")
    m = re.search(r"Session Beginning\s+([A-Za-z]+)\s*(\d{1,2})\s*,\s*(\d{4})", text)
    if not m:
        return []
    year, start_month = int(m.group(3)), _MON.get(m.group(1).lower()[:3])
    out = []
    day_re = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday),\s*([A-Za-z]+)\s*(\d{1,2})\b")
    marks = list(day_re.finditer(text))
    for i, dm in enumerate(marks):
        mon = _MON.get(dm.group(2).lower()[:3])
        if not mon:
            continue
        try:
            d = date(year + (1 if start_month and mon < start_month else 0), mon, int(dm.group(3)))
        except ValueError:
            continue
        seg = text[dm.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        seg = re.split(r"Court convenes|LEGAL HOLIDAY|Amended ", seg)[0]
        dockets = list(re.finditer(r"(\d{2}-\d{1,5}|\d{1,3},? ?Orig\.?)\s*\)?\s+", seg))
        for k, cm in enumerate(dockets):
            case = seg[cm.end(): dockets[k + 1].start() if k + 1 < len(dockets) else len(seg)]
            case = re.sub(r"\(\s*\d+\s*\)\s*$", "", case.strip())           # the next case's "(2)"
            case = re.sub(r"\((Consolidated|\d+ h)[^)]*\)?", "", case).strip(" ,.)(")
            if re.search(r"\bV\.", case.upper()) or "IN RE" in case.upper():
                out.append({"date": d.isoformat(), "docket": cm.group(1).replace(" ", ""), "case": case[:160]})
    return out


def court_filter(case: str, cc: dict) -> str | None:
    up = case.upper()
    if cc.get("not_company") and re.search(cc["not_company"], up) and not (
            cc.get("regulator") and re.search(cc["regulator"], up)):
        return next(("on your list" for w in cc.get("extra") or [] if str(w).upper() in up), None)
    for label, pat in (("a market regulator is a party", cc.get("regulator")), ("the President's power is the question", cc.get("executive")),
                       ("a company is a party", cc.get("company"))):
        if pat and re.search(pat, up):
            return label
    for w in cc.get("extra") or []:
        if str(w).upper() in up:
            return "on your list"
    return None


def _party_tickers(case: str) -> list:
    """Tickers of listed companies named as a party (mason_sources' guarded name table)."""
    out = []
    try:
        import mason_sources
        for side in re.split(r"\s+V\.\s+", case, flags=re.I):
            t = mason_sources.company_to_ticker(side.title())
            if t and t not in out:
                out.append(t)
    except Exception:
        pass
    return out


def scotus_session_links(index_html: str) -> list:
    """Links to the monthly argument calendars of the current term (first block on the page)."""
    links = re.findall(r"href=['\"](argument_calendars/MonthlyArgumentCal[A-Za-z]+(\d{4})\.pdf)['\"]", index_html or "")
    if not links:
        return []
    out, term = [], None
    for href, _y in links:
        mm = re.search(r"MonthlyArgumentCal([A-Za-z]+)(\d{4})", href)
        mon, yr = _MON.get(mm.group(1).lower()[:3]), int(mm.group(2))
        t = yr if mon and mon >= 10 else yr - 1                 # the term a session belongs to
        term = t if term is None else term
        if t != term:
            break
        out.append((SCOTUS_BASE + href, date(yr, mon or 1, 1)))
    return out


def feed_court(today: date, c: dict) -> dict:
    cc = C._merge(COURT_DEFAULTS, c.get("court"))
    r = _get(SCOTUS_INDEX)
    links = scotus_session_links(_text(r)) if r is not None else []
    if not links:
        return {"ok": False, "error": "Supreme Court calendar page not read"}
    ev, read, cases, wanted = [], 0, 0, 0
    for url, first in links:
        if first < today.replace(day=1) - timedelta(days=31):
            continue                                              # a session that is over
        wanted += 1
        pr = _get(url)
        if pr is None:
            continue
        read += 1
        for x in parse_scotus_calendar(pdf_text(pr.content)):
            cases += 1
            why = court_filter(x["case"], cc)
            if not why or x["date"] < today.isoformat():
                continue
            name = re.sub(r"\bV\.", "v.", x["case"].title().replace(" V. ", " v. "))
            tk = _party_tickers(x["case"])
            ev.append({"type": "court", "kind": "scotus_argument", "date": x["date"], "time_et": "10:00",
                       "key": f"scotus|{x['docket']}", "title": f"Supreme Court argument: {name}",
                       "subtitle": f"Kept because {why}. Docket {x['docket']}.", "importance": 2 if tk else 1,
                       "confirmed": True, "tickers": tk,
                       "url": f"https://www.supremecourt.gov/docket/docketfiles/html/public/{x['docket']}.html",
                       "details": {"docket": x["docket"], "why_kept": why, "calendar_url": url}})
    if not read:
        return {"ok": False, "error": "no argument calendar could be read"}
    part = _write(lambda conn: C.sync_events(conn, ev, source="Supreme Court", type_="court", today=today, c=c,
                                             retire=bool(ev) and read == wanted))
    return dict(part, ok=True, seen=cases, kept=len(ev), parts={"calendars_read": read, "calendars_listed": wanted})


# ══════════════════════════════════════════════════════════════════════════════
# FDA advisory committee meetings (Federal Register API)
# ══════════════════════════════════════════════════════════════════════════════

FR_URL = "https://www.federalregister.gov/api/v1/documents.json"
_DATE_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:\s*(?:-|–|and|through|to)\s*(\d{1,2}))?,?\s+(\d{4})", re.I)
_CLOCK_RE = re.compile(r"from\s+(\d{1,2}(?::\d{2})?)\s*([ap])\.?m", re.I)


def parse_fr_adcomm(payload, today: date) -> list:
    ev, seen = [], set()
    for x in (payload or {}).get("results") or []:
        title, dates = str(x.get("title") or ""), str(x.get("dates") or "")
        if "advisory committee" not in title.lower() or re.search(r"\b(renewal|request for nominations|vacanc|charter)\b", title, re.I):
            continue
        m = _DATE_RE.search(dates)
        if not m:
            continue
        try:
            d = date(int(m.group(4)), _MON[m.group(1).lower()], int(m.group(2)))
            end = date(int(m.group(4)), _MON[m.group(1).lower()], int(m.group(3))) if m.group(3) else None
        except ValueError:
            continue
        if d < today:
            continue
        comm = re.split(r";", title)[0].strip()
        topic = title.split("-", 1)[1].strip() if "-" in title and "; " in title.split("-", 1)[0] else ""
        key = f"adcomm|{C.norm_title(comm)}|{d.isoformat()}"
        if key in seen:
            continue
        seen.add(key)
        tm = None
        cm = _CLOCK_RE.search(dates)
        if cm:
            tm = C.clock_to_hhmm(f"{cm.group(1)} {cm.group(2)}m")
        company = None
        mm = re.search(r"\b(?:from|submitted by|sponsored by)\s+([A-Z][\w&.,' -]{2,60}?(?:Inc\.?|Corp\.?|Corporation|LLC|Ltd\.?|"
                       r"Pharmaceuticals?|Therapeutics|Biosciences|Company|Co\.))", topic)
        if mm:
            company = mm.group(1).strip(" ,")
        ev.append({"type": "fda", "kind": "adcomm", "date": d.isoformat(), "end_date": end.isoformat() if end else None,
                   "time_et": tm, "key": key, "pair_key": f"adcomm|{C.norm_title(comm)}|{C.norm_title(topic)[:60]}",
                   "pair_days": 45, "title": f"FDA advisory committee: {comm}",
                   "subtitle": (topic[:200] if topic else "Outside experts advise the FDA; a vote here often decides an approval."),
                   "importance": 2 if topic else 1, "confirmed": True, "url": x.get("html_url"),
                   "details": {"committee": comm, "topic": topic, "company": company,
                               "notice": x.get("document_number"), "sentence": dates[:240]}})
    return ev


def _company_tickers(ev: list) -> None:
    """Fill tickers for a notice that names its sponsor, with the precision-guarded name
    table mason_sources already uses. Never raises; no network."""
    try:
        import mason_sources
    except Exception:
        return
    for e in ev:
        co = (e.get("details") or {}).get("company")
        if not co:
            continue
        try:
            t = mason_sources.company_to_ticker(co)
        except Exception:
            t = None
        if t:
            e["tickers"] = [t]


def feed_fda(today: date, c: dict) -> dict:
    r = _get(FR_URL, params=[("conditions[agencies][]", "food-and-drug-administration"), ("conditions[type][]", "NOTICE"),
                             ("conditions[term]", '"advisory committee" "notice of meeting"'), ("order", "newest"),
                             ("per_page", "60"), ("fields[]", "title"), ("fields[]", "dates"), ("fields[]", "html_url"),
                             ("fields[]", "publication_date"), ("fields[]", "document_number")])
    try:
        payload = r.json() if r is not None else None
    except Exception:
        payload = None
    if not isinstance(payload, dict) or "results" not in payload:
        return {"ok": False, "error": "Federal Register did not answer"}
    ev = parse_fr_adcomm(payload, today)
    _company_tickers(ev)
    part = _write(lambda conn: C.sync_events(conn, ev, source="Federal Register", type_="fda", kinds={"adcomm"},
                                             today=today, c=c, retire=False))
    return dict(part, ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# bellwether earnings
# ══════════════════════════════════════════════════════════════════════════════

EARNINGS_DEFAULTS = {
    # about forty companies whose results move the market or a whole sector
    "bellwethers": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "TSM", "AMD", "INTC", "MU",
                    "ORCL", "CRM", "ADBE", "NFLX", "ASML", "QCOM", "JPM", "BAC", "GS", "MS", "V", "MA", "UNH",
                    "JNJ", "LLY", "MRK", "WMT", "COST", "HD", "TGT", "NKE", "DIS", "XOM", "CVX", "CAT", "BA",
                    "GE", "FDX"],
    # leaders in the sectors the book is exposed to (plain label -> tickers)
    "sector_leaders": {
        "chips (NVDA, AVGO, TSM, SMCI, MRVL)": ["AMAT", "LRCX", "ARM", "DELL"],
        "space and defense (RKLB, LMT)": ["LMT", "NOC", "RTX"],
        "quantum (IONQ)": ["IBM", "RGTI"],
        "biotech and pharma (JAZZ, REGN, ABBV, TEVA)": ["AMGN", "VRTX", "PFE"],
        "brokers and crypto (HOOD)": ["COIN", "SCHW", "IBKR"],
        "health care (CVS, XLV)": ["CI", "ELV"],
        "retail and staples (WMT, XLP)": ["PG", "KO"],
        "IT services (ACN)": ["IBM", "CTSH"],
        "optical and networking (COHR, LITE, CSCO)": ["ANET", "CIEN"],
    },
    "rh_fallback": False,              # one extra Robinhood calendar call a day for names with no stored date
}


def earnings_names(c: dict) -> dict:
    ec = C._merge(EARNINGS_DEFAULTS, c.get("earnings"))
    out = {}
    for t in ec.get("bellwethers") or []:
        out[str(t).upper()] = "bellwether"
    for label, ts in (ec.get("sector_leaders") or {}).items():
        for t in ts or []:
            out.setdefault(str(t).upper(), f"sector leader: {label}")
    return out


def earnings_events(rows: list, names: dict, skip: set, today: date) -> list:
    """rows: earnings_results rows (dicts). One event per name: its next unreported date."""
    best = {}
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        d = C.parse_date(r.get("report_date"))
        if t not in names or t in skip or d is None or d < today or r.get("eps_act") is not None and d > today:
            continue
        if r.get("eps_act") is not None:
            continue
        if t not in best or d < C.parse_date(best[t]["report_date"]):
            best[t] = r
    ev = []
    for t, r in sorted(best.items()):
        fp = r.get("fiscal_period")
        sess = {"am": "before the open", "pm": "after the close"}.get(r.get("timing"))
        sub = ("Reports " + sess + ". ") if sess else ""
        sub += "A market bellwether." if names[t] == "bellwether" else names[t][0].upper() + names[t][1:] + "."
        if r.get("verified") in (0, None):
            sub += " Date is Robinhood's estimate; the company has not confirmed it."
        ev.append({"type": "earnings", "kind": "bellwether", "date": str(r["report_date"])[:10], "tickers": [t],
                   "key": f"earn|{t}|{fp or str(r['report_date'])[:7]}", "title": f"{t} earnings" + (f" ({fp})" if fp else ""),
                   "subtitle": sub, "importance": 2, "confirmed": r.get("verified") == 1,
                   "details": {"session": sess, "timing": r.get("timing"), "fiscal_period": fp, "eps_est": r.get("eps_est"),
                               "why": names[t]}})
    return ev


def feed_earnings(today: date, c: dict) -> dict:
    names = earnings_names(c)
    conn = mason_db.get_connection()
    try:
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT ticker, fiscal_period, report_date, timing, eps_est, eps_act, verified FROM earnings_results "
                "WHERE report_date >= ?", (today.isoformat(),))]
        except Exception:
            return {"ok": False, "error": "earnings_results table not readable (mason_pead has not run yet)"}
        # names mason_pead already puts on the calendar (held and watchlist) are left to it
        skip = {str(r[0]).upper() for r in conn.execute(
            "SELECT DISTINCT ticker FROM calendar_events WHERE source='Robinhood' AND event_type='earnings' "
            "AND record_deleted=0 AND event_date >= ? AND ticker IS NOT NULL", (today.isoformat(),))}
    finally:
        conn.close()
    have = {str(r["ticker"]).upper() for r in rows if r.get("eps_act") is None}
    ec = C._merge(EARNINGS_DEFAULTS, c.get("earnings"))
    missing = sorted(set(names) - have - skip)
    if missing and ec.get("rh_fallback") is True:
        rows += _rh_calendar_rows(today, set(missing))
    ev = earnings_events(rows, names, skip, today)
    part = _write(lambda conn: C.sync_events(conn, ev, source="Robinhood earnings calendar", type_="earnings",
                                             kinds={"bellwether"}, today=today, c=c))
    return dict(part, ok=True, parts={"names": len(names), "left_to_mason_pead": len(skip & set(names)),
                                      "no_date_yet": [t for t in missing if t not in {e['tickers'][0] for e in ev}][:20]})


def _rh_calendar_rows(today: date, want: set) -> list:
    """At most one Robinhood earnings-calendar call a day (bulk lane), through mason_pead's
    own parser. Off by default. Never raises."""
    try:
        conn = mason_db.get_connection()
        try:
            st = C._snap_get(conn, "_calendar_feed", "earnings_rh") or {}
            if st.get("day") == today.isoformat():
                return []
            with conn:
                C._snap_put(conn, "_calendar_feed", "earnings_rh", {"day": today.isoformat()})
        finally:
            conn.close()
        import mason_pead
        if not (mason_pead.enabled() and mason_pead._rh_on()):
            return []
        errors = []
        return [dict(r, verified=None) for r in mason_pead._calendar_rows(today, 31, errors)
                if r.get("ticker") in want and r.get("eps_act") is None]
    except Exception as e:
        log.debug(f"[calendar] Robinhood earnings fallback skipped: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# the seed list (conferences, company events, summits) + the BLS schedule
# ══════════════════════════════════════════════════════════════════════════════

def load_seed(path=None) -> dict:
    p = Path(path or SEED_FILE)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("seed file needs an 'events' list")
    return data


def _month_span(ym: str):
    y, m = int(ym[:4]), int(ym[5:7])
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return date(y, m, 1), last


def seed_events(data: dict, today: date, end: date) -> list:
    ev = []
    for s in data.get("events") or []:
        try:
            sid, name = str(s["id"]), str(s["name"])
        except Exception:
            continue
        occs = [o for o in (s.get("next") or []) if isinstance(o, dict)]
        per_year = {}
        for o in occs:
            y = str(o.get("start") or o.get("expected") or "")[:4]
            per_year[y] = per_year.get(y, 0) + 1
        for o in occs:
            exp = str(o.get("expected") or "")
            if o.get("start"):
                d0, d1 = C.parse_date(o["start"]), C.parse_date(o.get("end"))
                title, confirmed = name, o.get("confirmed") is not False
                expected_month = None
            elif re.match(r"^\d{4}-\d{2}$", exp):
                d0, d1 = _month_span(exp)
                title = f"{name} (expected {d0.strftime('%B %Y')})"
                confirmed, expected_month = False, exp
            else:
                continue
            if d0 is None or (d1 or d0) < today or d0 > end:
                continue
            ykey = str(d0.year) if per_year.get(str(d0.year), 0) <= 1 else f"{d0.year}-{d0.month:02d}"
            if o.get("label"):
                title = f"{name}: {o['label']}"
                ykey += "|" + C.norm_title(o["label"])[:30]
            place = o.get("city") or ""
            sub = str(s.get("why") or "")
            if place:
                sub = (sub.rstrip(". ") + f". {place}.") if sub else place
            ev.append({"type": s.get("type") or "conference", "kind": s.get("kind") or "conference",
                       "date": d0.isoformat(), "end_date": d1.isoformat() if d1 and d1 > d0 else None,
                       "time_et": o.get("time_et"), "key": f"seed|{sid}|{ykey}", "title": title, "subtitle": sub,
                       "importance": int(s.get("importance") or 1), "confirmed": confirmed,
                       "tickers": s.get("tickers") or [], "url": o.get("source_url") or s.get("url"),
                       "details": {"seed_id": sid, "organiser": s.get("organiser"), "city": place or None,
                                   "sectors": s.get("sectors") or [], "official_url": s.get("url"),
                                   "source_url": o.get("source_url"), "checked": o.get("checked") or data.get("checked"),
                                   "expected_month": expected_month, "source_note": o.get("note")}})
    return ev


def bls_seed_events(data: dict, today: date, end: date) -> list:
    """The BLS release schedule copied by hand into the seed file (BLS refuses automated
    readers). FRED confirms the nearest dates on its own; this list reaches further out."""
    ev = []
    b = data.get("bls_schedule") or {}
    for row in b.get("releases") or []:
        rk, d = row.get("release"), C.parse_date(row.get("date"))
        if rk not in C.RELEASES or d is None or not (today <= d <= end):
            continue
        tm = row.get("time_et") or ("10:00" if rk == "jolts" else "08:30")
        e = _econ_event(rk, d, tm, "BLS (published schedule)", b.get("source_url") or "https://www.bls.gov/schedule/",
                        period=row.get("period"))
        e["details"]["checked"] = b.get("checked")
        ev.append(e)
    return ev


def feed_seed(today: date, c: dict) -> dict:
    try:
        data = load_seed()
    except Exception as e:
        return {"ok": False, "error": f"seed file not readable: {type(e).__name__}: {str(e)[:120]}"}
    end = _horizon(today, c)
    ev = seed_events(data, today, end)
    bls = bls_seed_events(data, today, end)

    def w(conn):
        tot = {}
        keep = []
        for e in ev:                         # a date the event search confirmed is not overwritten by an older guess
            cid = C.event_id("Event list", e["key"])
            r = conn.execute("SELECT details_json FROM calendar_events WHERE id=? AND record_deleted=0", (cid,)).fetchone()
            det = C._loads(r[0]) if r else {}
            if det.get("found_by") and det.get("confirmed") and not e["confirmed"]:
                keep.append(dict(e, _keep=True))
                continue
            keep.append(e)
        for t in sorted({e["type"] for e in keep}):
            part_ev = [e for e in keep if e["type"] == t]
            fresh = [e for e in part_ev if not e.get("_keep")]
            kept_ids = {C.event_id("Event list", e["key"]) for e in part_ev if e.get("_keep")}
            part = C.sync_events(conn, fresh, source="Event list", type_=t, today=today, c=c, retire=False)
            _add(tot, part)
            live = {C.event_id("Event list", e["key"]) for e in fresh} | kept_ids
            for r in conn.execute("SELECT id, details_json FROM calendar_events WHERE source='Event list' AND event_type=? "
                                  "AND record_deleted=0 AND event_date >= ?", ("cal_" + t, today.isoformat())).fetchall():
                if r["id"] not in live and C._loads(r["details_json"]).get("seed_id"):
                    if C.soft_delete(conn, r["id"], "feed:gone"):
                        tot["retired"] = tot.get("retired", 0) + 1
        if bls:
            _add(tot, C.sync_events(conn, bls, source="BLS (published schedule)", type_="econ", kinds={"econ_release"},
                                    today=today, c=c))
        return tot
    tot = _write(w)
    tot.update(ok=True, parts={"events_in_file": len(data.get("events") or []), "dated": len(ev), "bls_dates": len(bls)})
    return tot
