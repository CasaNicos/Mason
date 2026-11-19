"""
mason_calendar.py — the events calendar (2026-11-14).

One place that reads and writes the dated events Mason knows about, and the API the
dashboard calendar widget is built on.

What it owns
  * writing the NEW kinds of rows in `calendar_events` (Fed, economic releases, Congress,
    courts, FDA, market holidays, expiry days, conferences, company events, bellwether
    earnings, the owner's own events). Rows written before this module existed (Forex
    Factory `econ`, NASDAQ `ipo`, ClinicalTrials.gov `trial_completion`, Robinhood
    `earnings` from mason_pead) are left exactly as they are and only READ here.
  * reading everything back in one stable shape: list_events() -> GET /api/calendar.
  * the schedule of the feeds (tick(), called hourly from mason_maintenance.tick),
    one health record per feed, status(), and two plain inbox notes (a feed down for
    more than a day; an important date that moved).
  * calendar_lines(): the same events as short lines for the director and the quick
    checks. Nothing calls it until its switch is turned on (see CALENDAR_PROMPTS_FOR_NICO.md).

Storage rules (no schema change: everything new lives in details_json)
  * id = sha1(source|natural key)[:16], the same recipe as mason_sources._cal_id. The
    natural key is the source's own stable id when it has one (a congress.gov event id,
    "fomc|2027-01", a seed id + year), so a date that moves updates the SAME row and
    details_json keeps `prev_date` + `date_changed_on`. Sources without a stable id key
    on type + date + normalised title; sync_events() then pairs a row that vanished with
    one that appeared under the same pair key and records the move.
  * event_type is always "cal_<type>" for rows written here. None of these values is
    read by the older readers (mason_dossier._MACRO_CAL_TYPES, mason_etf_screen,
    mason_desk_check select 'econ' / 'earnings' / ...), so what the models are shown
    does not change when the feeds are switched on.
  * the `ticker` COLUMN stays NULL on every row written here unless the event's type is
    listed in `calendar.ticker_column_types` (default: empty). mason_triage adds +0.05 to
    a story's score when its ticker has a row dated within 7 days (`WHERE ticker=?`), and
    mason_dossier lists rows by ticker; the tickers an event matters to are kept in
    details_json.tickers instead, so neither changes.
  * soft delete only. A row the owner removed is never brought back by a feed.

Config: mason.config.json -> "calendar" (read on each call; defaults in DEFAULTS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

import mason_db

log = logging.getLogger("mason.calendar")
BASE = Path(__file__).parent
_CONFIG_PATH = BASE / "mason.config.json"      # tests repoint this
CREATED_BY = "mason_calendar"
OWNER_SOURCE = "Nico"

# The small fixed set of types the API speaks. Order = display order.
TYPES = [
    ("earnings", "Earnings"),
    ("fed", "Federal Reserve"),
    ("econ", "Economic data"),
    ("congress", "Congress"),
    ("court", "Courts"),
    ("fda", "FDA and trials"),
    ("market", "Market schedule"),
    ("conference", "Conferences"),
    ("company", "Company events"),
    ("ipo", "IPOs"),
    ("expiry", "Expiry and deadlines"),
    ("personal", "Mine"),
]
TYPE_KEYS = [t for t, _ in TYPES]

# rows written by older modules -> API type
_LEGACY_TYPES = {"econ": "econ", "earnings": "earnings", "ipo": "ipo", "ipo_pricing": "ipo",
                 "trial_completion": "fda", "pdufa": "fda", "adcom": "fda", "fda_decision": "fda"}

DEFAULTS = {
    "enabled": True,                 # the deterministic feeds and the API. No model is called by these.
    "months_ahead": 12,
    "changed_days": 14,              # how long a moved date is flagged as "changed"
    "tick_budget_s": 150,            # most seconds one hourly pass may spend fetching
    "ticker_column_types": [],       # API types allowed to fill the `ticker` column (see module doc)
    # (news phase 6c: the drug-decision-date feed "biocal" is NOT listed here: absent means off, see mason_biocal)
    "feeds": {"seed": True, "computed": True, "market": True, "fed": True, "econ": True,
              "congress": True, "court": True, "fda": True, "earnings": True},
    "hours": {"seed": 24, "computed": 168, "market": 168, "fed": 12, "econ": 24,
              "congress": 12, "court": 168, "fda": 24, "earnings": 12, "biocal": 1},
    # mason_biocal: who may read its rows (show_models false | "held" | "all") and the inbox reminder
    "biocal": {"show_models": False, "director_block": False, "lookback_days": 30, "notify_days": 0},
    "notify": {"feed_down_hours": 24, "date_changes": True},
    "fed": {"speech_days": 45},
    "congress": {},                  # see mason_calendar_feeds.CONGRESS_DEFAULTS
    "court": {},                     # see mason_calendar_feeds.COURT_DEFAULTS
    "earnings": {},                  # see mason_calendar_feeds.EARNINGS_DEFAULTS
    # everything below calls a model or adds text a model reads: OFF until Nico approves
    "tool": {"enabled": False},      # see mason_calendar_tool.TOOL_DEFAULTS
    "chat_tool": {"enabled": False},
    "director_block": {"enabled": False},
    "desk_check_block": {"enabled": False},
}


# ── config ──────────────────────────────────────────────────────────────────────

_cfg_cache = {"mtime": None, "path": None, "cfg": {}}


def _merge(base: dict, over) -> dict:
    out = dict(base)
    if isinstance(over, dict):
        for k, v in over.items():
            if str(k).startswith("_"):
                continue
            out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def _whole_cfg() -> dict:
    try:
        p = Path(_CONFIG_PATH)
        m = p.stat().st_mtime
        if _cfg_cache["mtime"] != m or _cfg_cache["path"] != str(p):
            _cfg_cache.update(mtime=m, path=str(p), cfg=json.loads(p.read_text(encoding="utf-8")) or {})
        return _cfg_cache["cfg"]
    except Exception:
        return {}


def cfg() -> dict:
    """The `calendar` block over DEFAULTS. Read on each call (mtime-cached)."""
    return _merge(DEFAULTS, _whole_cfg().get("calendar"))


def enabled() -> bool:
    return cfg().get("enabled") is True


def chat_tool_enabled() -> bool:
    c = cfg()
    return c.get("enabled") is True and (c.get("chat_tool") or {}).get("enabled") is True


class CalendarInputError(ValueError):
    """Bad input to a calendar write (2026-11-16). str(e) is the short message these functions always
    raised (the chat tool shows that one to the model, so it is unchanged); the dashboard API answers
    with `plain` (a sentence for Nico) and `code` (a short tag the widget can rely on) instead."""

    def __init__(self, message: str, code: str, plain: str):
        super().__init__(message)
        self.code = code
        self.plain = plain


# the plain sentences the API answers with (CALENDAR_API.md §6)
_PLAIN = {
    "no_title": "Give the event a name.",
    "bad_date": "That date does not look right. Use a real day, written like 2026-12-31.",
    "bad_time": "That time does not look right. Use a time like 14:30 or 2:30 pm (Eastern time), or leave it empty.",
    "bad_end_date": "That last day does not look right. Use a real day, written like 2026-12-31, or leave it empty.",
    "end_before_start": "The last day is before the first day. Pick a last day on or after the first day.",
    "no_id": "No event was named, so nothing was changed.",
    "nothing_to_change": "There was nothing to change.",
    # kept as the two words the server always sent: the dashboard of before 2026-11-16 recognises an event removed
    # elsewhere by exactly this text (it then reloads); the newer widget goes by the code and words it itself (review)
    "not_found": "not found",
    "from_position": "This comes from an open position. Change the position, not the calendar entry.",
    "bad_input": "That could not be saved. Check what you typed and try again.",
}


def _creator(v) -> str | None:
    """Who stored a row or reminder: 'nico', 'mason', or the part of Mason that wrote it ('mason_calendar',
    'mason_sources', 'mason_pead'). None when nobody is recorded."""
    s = str(v or "").strip().lower()
    return s[:40] or None


# ── small helpers ───────────────────────────────────────────────────────────────

def today_et(now=None) -> date:
    if isinstance(now, date) and not isinstance(now, datetime):
        return now
    if isinstance(now, datetime):
        return (now.astimezone(ET) if (ET and now.tzinfo) else now).date()
    ts = time.time() if now is None else float(now)
    return (datetime.fromtimestamp(ts, tz=ET) if ET else datetime.fromtimestamp(ts)).date()


def parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def utc_to_et(dt_utc: datetime) -> tuple:
    """An aware (or naive-UTC) datetime -> (ET date ISO, 'HH:MM' ET). Daylight saving is
    handled by the zone database, so 14:00Z is 10:00 in July and 09:00 in December."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    loc = dt_utc.astimezone(ET) if ET else dt_utc
    return loc.date().isoformat(), loc.strftime("%H:%M")


_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\s*$", re.I)


def clock_to_hhmm(s) -> str | None:
    """'2:30 p.m.' / '8:30 AM' / '14:00' -> 'HH:MM'; None when it is not a clock time."""
    s = str(s or "").strip()
    m = _TIME_RE.match(s)
    if m:
        h, mi, ap = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
        if ap == "p":
            h += 12
        return f"{h:02d}:{mi:02d}"
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def norm_title(s) -> str:
    """Lower-case words and digits only; used in natural keys and for pairing."""
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))[:120]


def event_id(source: str, natural_key: str) -> str:
    return hashlib.sha1(f"{source}|{natural_key}".encode("utf-8", "ignore")).hexdigest()[:16]


def _loads(s) -> dict:
    try:
        d = json.loads(s) if s else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _dumps(d) -> str:
    return json.dumps(d, separators=(",", ":"), sort_keys=True, default=str, ensure_ascii=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_tickers(tickers) -> list:
    out = []
    for t in tickers or []:
        t = str(t or "").strip().upper()
        if t and re.match(r"^[A-Z][A-Z0-9.\-]{0,6}$", t) and t not in out:
            out.append(t)
    return out


# ── writing ─────────────────────────────────────────────────────────────────────
# An event handed to upsert_event / sync_events is a dict:
#   source      str   who says so ("Federal Reserve", "congress.gov", ...)
#   type        str   one of TYPE_KEYS
#   kind        str   finer label kept in details ("fomc_decision", "hearing", ...)
#   title       str
#   date        str   YYYY-MM-DD (ET)
#   end_date    str   optional, for multi-day events
#   time_et     str   'HH:MM' ET or None
#   key         str   optional stable natural key from the source
#   pair_key    str   optional: what makes two rows "the same event" when key has the date in it
#   pair_days   int   how far a date may move and still be the same event (default 21)
#   tickers     list  tickers the event matters to (details only, see module doc)
#   confirmed   bool  True = the date comes from the organiser / an official source
#   importance  int   1-3, or None to let the reader's rule decide
#   subtitle    str   one plain line: why it matters
#   url         str
#   details     dict  anything else worth keeping (place, speaker, sentence it came from)

_KEEP_KEYS = ("prev_date", "date_changed_on", "date_history", "done", "done_on", "note")


def _natural_key(ev: dict) -> str:
    if ev.get("key"):
        return str(ev["key"])
    tk = ",".join(_clean_tickers(ev.get("tickers")))[:40]
    return f"{ev['type']}|{ev['date']}|{norm_title(ev['title'])}|{tk}"


def _ticker_column(ev: dict, c: dict):
    allowed = c.get("ticker_column_types") or []
    tk = _clean_tickers(ev.get("tickers"))
    if ev["type"] in allowed and len(tk) == 1:
        return tk[0]
    return None


def upsert_event(conn, ev: dict, *, today: date | None = None, created_by: str = CREATED_BY,
                 c: dict | None = None) -> tuple:
    """Insert or update one event. Returns (state, id) with state in
    new | moved | changed | same | hidden. The caller owns the transaction."""
    c = c if c is not None else cfg()
    today = today or today_et()
    if ev.get("type") not in TYPE_KEYS:
        raise ValueError(f"bad calendar type {ev.get('type')!r}")
    d = parse_date(ev.get("date"))
    if d is None or not str(ev.get("title") or "").strip():
        raise ValueError("an event needs a title and a YYYY-MM-DD date")
    source = str(ev.get("source") or "calendar")
    cid = event_id(source, _natural_key(ev))
    end = parse_date(ev.get("end_date"))
    if end is not None and end <= d:
        end = None
    confirmed = ev.get("confirmed") is not False
    det = dict(ev.get("details") or {})
    det.update(kind=ev.get("kind") or ev["type"], confirmed=confirmed,
               tickers=_clean_tickers(ev.get("tickers")))
    if end is not None:
        det["end_date"] = end.isoformat()
    for k in ("importance", "subtitle", "pair_key"):
        if ev.get(k) not in (None, ""):
            det[k] = ev[k]
    title = str(ev["title"]).strip()[:300]
    tm = clock_to_hhmm(ev.get("time_et")) if ev.get("time_et") else None
    status = ev.get("status") or ("confirmed" if confirmed else "unconfirmed")
    etype = "cal_" + ev["type"]
    tcol = _ticker_column(ev, c)
    now = time.time()
    prev = conn.execute("SELECT * FROM calendar_events WHERE id=?", (cid,)).fetchone()
    if prev is None:
        if ev.get("_prev_date"):                       # sync_events paired it with a row that vanished
            det.update(prev_date=ev["_prev_date"], date_changed_on=today.isoformat())
        conn.execute(
            "INSERT INTO calendar_events (id, source, event_type, ticker, title, event_date, event_time, "
            "status, url, details_json, first_seen, last_updated, created_date, created_by, record_deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (cid, source, etype, tcol, title, d.isoformat(), tm, status, ev.get("url"), _dumps(det),
             now, now, _iso_now(), created_by))
        return ("moved" if ev.get("_prev_date") else "new"), cid
    p = dict(prev)
    if p.get("record_deleted"):
        if not str(p.get("deleted_by") or "").startswith("feed"):
            return "hidden", cid                       # the owner removed it: it stays removed
    pdet = _loads(p.get("details_json"))
    for k in _KEEP_KEYS:
        if k in pdet and k not in det:
            det[k] = pdet[k]
    if pdet.get("done"):
        status = "done"
    state = "same"
    if p.get("event_date") != d.isoformat():
        hist = list(pdet.get("date_history") or [])
        hist.append({"from": p.get("event_date"), "to": d.isoformat(), "on": today.isoformat()})
        det.update(prev_date=p.get("event_date"), date_changed_on=today.isoformat(), date_history=hist[-6:])
        state = "moved"
    new_vals = (etype, tcol, title, d.isoformat(), tm, status, ev.get("url"), _dumps(det))
    old_vals = (p.get("event_type"), p.get("ticker"), p.get("title"), p.get("event_date"), p.get("event_time"),
                p.get("status"), p.get("url"), _dumps(pdet))
    if new_vals == old_vals and not p.get("record_deleted"):
        return "same", cid
    if state == "same":
        state = "changed"
    conn.execute(
        "UPDATE calendar_events SET event_type=?, ticker=?, title=?, event_date=?, event_time=?, status=?, url=?, "
        "details_json=?, last_updated=?, record_deleted=0, deleted_date=NULL, deleted_by=NULL WHERE id=?",
        new_vals + (now, cid))
    return state, cid


def soft_delete(conn, cid: str, deleted_by: str) -> bool:
    cur = conn.execute("UPDATE calendar_events SET record_deleted=1, deleted_date=?, deleted_by=? "
                       "WHERE id=? AND record_deleted=0", (_iso_now(), deleted_by, cid))
    return cur.rowcount > 0


def sync_events(conn, events: list, *, source: str, type_: str, kinds=None, retire: bool = True,
                today: date | None = None, c: dict | None = None) -> dict:
    """Write one feed's full list for (source, type[, kinds]) and tidy what is no longer there.

    * every event is upserted;
    * a FUTURE row of the same scope that is not in the list any more is either the old
      date of an event that moved (same pair_key, within pair_days: the new row records
      prev_date and the old row is retired) or an event that was dropped (retired,
      deleted_by 'feed:gone');
    * nothing is retired when the list looks cut short (empty, or under half of what is
      stored), so a partial fetch cannot wipe the calendar.
    Returns counts + `moved`: [{id, title, from, to, importance}]. Caller owns the transaction."""
    c = c if c is not None else cfg()
    today = today or today_et()
    out = {"new": 0, "moved": [], "changed": 0, "same": 0, "hidden": 0, "retired": 0, "n": len(events)}
    etype = "cal_" + type_
    rows = conn.execute("SELECT id, title, event_date, details_json FROM calendar_events WHERE source=? AND "
                        "event_type=? AND record_deleted=0 AND event_date >= ?",
                        (source, etype, today.isoformat())).fetchall()
    existing = {}
    for r in rows:
        det = _loads(r["details_json"])
        if kinds is None or det.get("kind") in kinds:
            existing[r["id"]] = {"title": r["title"], "date": r["event_date"], "det": det}
    incoming = {}
    for ev in events:
        ev = dict(ev, source=source, type=type_)
        incoming[event_id(source, _natural_key(ev))] = ev
    missing = {i: e for i, e in existing.items() if i not in incoming}
    safe = retire and bool(incoming) and (len(existing) < 4 or len(incoming) * 2 >= len(existing))
    for cid, ev in incoming.items():
        if cid not in existing and ev.get("pair_key") and safe:
            d = parse_date(ev["date"])
            best = None
            for mid, m in missing.items():
                md = parse_date(m["date"])
                if m["det"].get("pair_key") == ev["pair_key"] and md and d and md != d \
                        and abs((md - d).days) <= int(ev.get("pair_days") or 21):
                    if best is None or abs((md - d).days) < best[1]:
                        best = (mid, abs((md - d).days))
            if best:
                ev["_prev_date"] = missing[best[0]]["date"]
                soft_delete(conn, best[0], "feed:moved")
                missing.pop(best[0])
        state, _ = upsert_event(conn, ev, today=today, c=c)
        if state == "moved":
            det = _loads(conn.execute("SELECT details_json FROM calendar_events WHERE id=?", (cid,)).fetchone()[0])
            out["moved"].append({"id": cid, "title": ev["title"], "from": det.get("prev_date"), "to": ev["date"],
                                 "importance": ev.get("importance"), "tickers": _clean_tickers(ev.get("tickers"))})
        elif state in out:
            out[state] += 1
    if safe:
        for mid in missing:
            if soft_delete(conn, mid, "feed:gone"):
                out["retired"] += 1
    return out


# ── the owner's own events ──────────────────────────────────────────────────────

def add_owner_event(title: str, when: str, *, time_et=None, end_date=None, tickers=None, note=None,
                    created_by: str = "nico", url=None, type_: str = "personal", confirmed: bool = True) -> dict:
    """An event the owner (or Mason, on his request in chat) adds by hand. `end_date` = the last day of a
    multi-day event: a real day, not before `when` (the same day as `when` = a one-day event)."""
    title = str(title or "").strip()
    d = parse_date(when)
    if not title:
        raise CalendarInputError("title required", "no_title", _PLAIN["no_title"])
    if d is None:
        raise CalendarInputError(f"bad date: {when!r} (want YYYY-MM-DD)", "bad_date", _PLAIN["bad_date"])
    if time_et and not clock_to_hhmm(time_et):
        raise CalendarInputError(f"bad time: {time_et!r} (want HH:MM, Eastern time)", "bad_time", _PLAIN["bad_time"])
    if type_ not in TYPE_KEYS:
        raise ValueError(f"bad type: {type_!r}")
    end_date = _check_end(end_date, d)
    key = f"owner|{time.time_ns()}"
    ev = {"source": OWNER_SOURCE, "type": type_, "kind": "owner", "title": title, "date": d.isoformat(),
          "end_date": end_date, "time_et": time_et, "tickers": tickers, "confirmed": confirmed, "key": key,
          "url": url, "details": {"note": str(note).strip()[:500]} if note else {}, "importance": 3}
    conn = mason_db.get_connection()
    try:
        with conn:
            _, cid = upsert_event(conn, ev, created_by=created_by)
    finally:
        conn.close()
    return get_event(cid)


def _check_end(end_date, start: date) -> str | None:
    """The last day of an owner event, checked against its first day: None for no end (empty, or the
    same day as the first); CalendarInputError when it is not a day or comes before the first day."""
    if end_date in (None, ""):
        return None
    e = parse_date(end_date)
    if e is None:
        raise CalendarInputError(f"bad end date: {end_date!r}", "bad_end_date", _PLAIN["bad_end_date"])
    if e < start:
        raise CalendarInputError("end date before the date", "end_before_start", _PLAIN["end_before_start"])
    return e.isoformat() if e > start else None


_UNSET = object()


def update_event(cid: str, *, title=_UNSET, when=_UNSET, time_et=_UNSET, end_date=_UNSET, tickers=_UNSET,
                 note=_UNSET, done=_UNSET) -> dict:
    """Edit an event in place. Title / date / time / tickers can only be changed on events
    the owner added; `done` and `note` can be set on any stored event. Raises ValueError
    (bad input; CalendarInputError for the cases the API words for Nico) or KeyError (no such event).
    Moving only the first day of a multi-day event moves its last day too (it keeps its length)."""
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT * FROM calendar_events WHERE id=? AND record_deleted=0", (cid,)).fetchone()
        if r is None:
            raise KeyError(cid)
        r = dict(r)
        src = str(r.get("source") or "") or "another part of Mason"
        if not str(r.get("event_type") or "").startswith("cal_"):
            # rows other modules write (Forex Factory, NASDAQ IPO, ClinicalTrials.gov, mason_pead's earnings)
            # are read by the desks; the calendar shows them and never changes them
            raise CalendarInputError("this entry comes from " + str(r["source"]) + " and cannot be changed here",
                                     "read_only", f"This date comes from {src}, so it cannot be changed here.")
        det = _loads(r["details_json"])
        mine = r["source"] == OWNER_SOURCE
        if not mine and any(x is not _UNSET for x in (title, when, time_et, end_date, tickers)):
            raise CalendarInputError("only events you added can be edited; this one comes from " + str(r["source"]),
                                     "not_editable", f"Only events you added yourself can be changed. This one comes from "
                                                     f"{src}; you can still add a note to it or mark it done.")
        sets, args = [], []
        if title is not _UNSET:
            t = str(title or "").strip()
            if not t:
                raise CalendarInputError("title cannot be blank", "no_title", _PLAIN["no_title"])
            sets.append("title=?"); args.append(t[:300])
        start = parse_date(r["event_date"])
        if when is not _UNSET:
            d = parse_date(when)
            if d is None:
                raise CalendarInputError(f"bad date: {when!r} (want YYYY-MM-DD)", "bad_date", _PLAIN["bad_date"])
            if d.isoformat() != r["event_date"]:
                det.update(prev_date=r["event_date"], date_changed_on=today_et().isoformat())
            old_end = parse_date(det.get("end_date"))
            if end_date is _UNSET and old_end and start and d != start:
                det["end_date"] = (old_end + (d - start)).isoformat()      # same length, later or earlier
            sets.append("event_date=?"); args.append(d.isoformat())
            start = d
        if time_et is not _UNSET:
            tm = clock_to_hhmm(time_et) if time_et else None
            if time_et and not tm:
                raise CalendarInputError(f"bad time: {time_et!r} (want HH:MM, Eastern time)", "bad_time", _PLAIN["bad_time"])
            sets.append("event_time=?"); args.append(tm)
        if end_date is not _UNSET:
            e = _check_end(end_date, start) if start else None
            det.pop("end_date", None)
            if e is not None:
                det["end_date"] = e
        if tickers is not _UNSET:
            det["tickers"] = _clean_tickers(tickers if isinstance(tickers, (list, tuple)) else
                                            re.split(r"[,\s]+", str(tickers or "")))
        if note is not _UNSET:
            det.pop("note", None)
            if str(note or "").strip():
                det["note"] = str(note).strip()[:500]
        if done is not _UNSET:
            det.pop("done", None); det.pop("done_on", None)
            if done:
                det.update(done=True, done_on=today_et().isoformat())
            sets.append("status=?")
            args.append("done" if done else ("confirmed" if det.get("confirmed", True) else "unconfirmed"))
        sets += ["details_json=?", "last_updated=?"]
        args += [_dumps(det), time.time(), cid]
        with conn:
            conn.execute(f"UPDATE calendar_events SET {', '.join(sets)} WHERE id=?", args)
    finally:
        conn.close()
    return get_event(cid)


def delete_event(cid: str, deleted_by: str = "nico") -> bool:
    """Soft-delete a row this module wrote. Rows of other modules are refused (ValueError):
    their writers never bring a deleted row back, and the desks read them."""
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT event_type, source FROM calendar_events WHERE id=? AND record_deleted=0", (cid,)).fetchone()
        if r is None:
            return False
        if not str(r[0] or "").startswith("cal_"):
            raise CalendarInputError("this entry comes from " + str(r[1]) + " and cannot be removed here", "read_only",
                                     f"This date comes from {r[1] or 'another part of Mason'}, so it cannot be removed here.")
        with conn:
            return soft_delete(conn, cid, deleted_by)
    finally:
        conn.close()


def get_event(cid: str) -> dict | None:
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT * FROM calendar_events WHERE id=? AND record_deleted=0", (cid,)).fetchone()
        return _row_item(dict(r), book(conn), cfg(), today_et()) if r is not None else None
    finally:
        conn.close()


# ── who holds what ──────────────────────────────────────────────────────────────

def book(conn=None) -> dict:
    """{'held': set, 'watched': set}. Held = Nico's open positions (personal_positions),
    Mason's open trades (agent_trades) and open option plays. Never raises."""
    own = conn is None
    held, watched = set(), set()
    try:
        if own:
            conn = mason_db.get_connection()
        for sql in ("SELECT DISTINCT ticker FROM personal_positions WHERE is_deleted=0 AND status='open'",
                    "SELECT DISTINCT ticker FROM agent_trades WHERE COALESCE(record_deleted,0)=0 AND status='open'"):
            try:
                held |= {str(r[0]).upper() for r in conn.execute(sql) if r[0]}
            except Exception:
                pass
        try:
            watched = {str(r[0]).upper() for r in conn.execute(
                "SELECT DISTINCT ticker FROM watchlist WHERE record_deleted=0 AND status='active'") if r[0]}
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        import mason_agenda
        held |= {str(p.get("ticker") or "").upper() for p in mason_agenda.open_option_plays() if p.get("ticker")}
    except Exception:
        pass
    held.discard("CASH")
    return {"held": held, "watched": watched - held}


# ── economic releases: one name per release, whoever reports it ─────────────────
# key -> (title, why it matters, importance, Forex Factory title pattern)
RELEASES = {
    "cpi": ("Consumer prices (CPI)", "The main inflation report; it moves rate expectations and the whole market.", 3,
            r"\bcpi\b"),
    "jobs": ("Jobs report", "Payrolls, unemployment and wages for the month; the biggest scheduled data release.", 3,
             r"non-farm employment change|unemployment rate|average hourly earnings"),
    "pce": ("Personal income and spending (PCE inflation)", "PCE is the inflation measure the Fed targets.", 3,
            r"\bpce\b|personal (income|spending)"),
    "gdp": ("GDP", "How fast the economy grew last quarter.", 3, r"\bgdp\b"),
    "ppi": ("Producer prices (PPI)", "Wholesale inflation; feeds into the PCE estimate.", 2, r"\bppi\b"),
    "retail_sales": ("Retail sales", "Consumer spending for the month; matters for retailers and the growth outlook.", 2,
                     r"retail sales"),
    "jolts": ("Job openings (JOLTS)", "Labor demand; the Fed watches it.", 2, r"jolts"),
    "ism_mfg": ("ISM manufacturing index", "First read on factory activity for the month.", 2, r"ism manufacturing"),
    "ism_svc": ("ISM services index", "Activity in the services side of the economy.", 2, r"ism (services|non-manufacturing)"),
    "umich": ("Consumer sentiment (Univ. of Michigan)", "Consumer mood and inflation expectations.", 2,
              r"uom (consumer sentiment|inflation expectations)"),
    "claims": ("Weekly jobless claims", "Weekly read on layoffs.", 1, r"unemployment claims"),
    "durable_goods": ("Durable goods orders", "Orders for long-lasting goods; a read on business investment.", 1,
                      r"durable goods"),
    "housing_starts": ("Housing starts and permits", "New home construction.", 1, r"housing starts|building permits"),
    "new_home_sales": ("New home sales", "Demand for newly built homes.", 1, r"new home sales"),
    "trade": ("Trade balance", "Imports and exports for the month.", 1, r"trade balance"),
}
_FF_FED_RE = re.compile(r"federal funds rate|fomc (statement|press conference|economic projections)", re.I)
_FF_MINUTES_RE = re.compile(r"fomc meeting minutes", re.I)
# "Fed Chairman Warsh Speaks", "FOMC Member Waller Speaks": a Fed speech as Forex Factory names it
_FF_SPEECH_RE = re.compile(r"^(?:fed|fomc)\b.*?\b([a-z][a-z'’\-]+)\s+speaks$", re.I)
_NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def _surname(name) -> str | None:
    """'Chairman Kevin Warsh' -> 'warsh'. None when there is no name to read."""
    words = [w.strip(".,").lower() for w in str(name or "").split()]
    words = [w for w in words if w and w not in _NAME_SUFFIX]
    return words[-1] if words and re.fullmatch(r"[a-z][a-z'’\-]+", words[-1]) else None


def _speech_key(surname, time_et) -> str | None:
    """One Fed speech = same day (the group), same clock time, same speaker's surname."""
    return f"fedspeech:{surname}:{time_et}" if surname and time_et else None


def release_key_for(title: str) -> str | None:
    t = str(title or "").lower()
    for k, (_n, _w, _i, pat) in RELEASES.items():
        if re.search(pat, t):
            return k
    return None


# ── reading: one stable shape ───────────────────────────────────────────────────

def _ff_time_et(event_time, event_date) -> str | None:
    """Forex Factory rows carry 'HH:MM ±hhmm'. Convert through the real offset so the
    answer is Eastern time whatever offset the feed used."""
    m = re.match(r"^(\d{2}):(\d{2})\s*([+-]\d{2}):?(\d{2})$", str(event_time or "").strip())
    d = parse_date(event_date)
    if not m or d is None:
        return clock_to_hhmm(event_time)
    off = timedelta(hours=int(m.group(3)), minutes=int(m.group(3)[0] + m.group(4)))
    dt = datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)), tzinfo=timezone(off))
    return utc_to_et(dt)[1]


def _row_item(r: dict, bk: dict, c: dict, today: date) -> dict | None:
    """One calendar_events row -> one API item (None for a row that should not be shown)."""
    det = _loads(r.get("details_json"))
    et = str(r.get("event_type") or "")
    src = r.get("source") or ""
    tickers = _clean_tickers(det.get("tickers")) or _clean_tickers([r.get("ticker")])
    held = bool(set(tickers) & bk["held"])
    watched = bool(set(tickers) & bk["watched"]) and not held
    title = r.get("title") or ""
    subtitle = det.get("subtitle") or ""
    time_et = clock_to_hhmm(r.get("event_time")) if r.get("event_time") else None
    confirmed = det.get("confirmed", True) is not False
    imp = det.get("importance")
    kind = det.get("kind") or et
    extra = {}
    if et.startswith("cal_"):
        typ = et[4:]
        if typ not in TYPE_KEYS:
            return None
    else:
        typ = _LEGACY_TYPES.get(et)
        if typ is None:
            return None
        kind = et
        if et == "econ":
            time_et = _ff_time_et(r.get("event_time"), r.get("event_date"))
            raw = re.sub(r"\s*\((High|Medium|Low) impact\)\s*$", "", re.sub(r"^USD\s+", "", title))
            rk = release_key_for(raw)
            extra["_ff"] = {"name": raw, "forecast": det.get("forecast") or "", "previous": det.get("previous") or ""}
            if _FF_FED_RE.search(raw):
                typ, extra["_merge"] = "fed", "fomc"
            elif _FF_MINUTES_RE.search(raw):
                typ, extra["_merge"] = "fed", "fomc_minutes"
            elif det.get("kind") == "central_bank":
                typ = "fed"
            sm = _FF_SPEECH_RE.match(raw)
            if sm and "_merge" not in extra and not rk:      # the same speech as the Fed's own calendar row
                sk = _speech_key(_surname(sm.group(1)), time_et)
                if sk:
                    extra["_merge"] = sk
            if rk and typ == "econ":
                extra["_merge"] = rk
                title, subtitle, imp = RELEASES[rk][0], RELEASES[rk][1], RELEASES[rk][2]
            else:
                title = raw
                imp = imp or (2 if str(det.get("impact")) == "High" else 1)
        elif et == "earnings":
            sess = str(r.get("event_time") or "")
            time_et = None
            subtitle = ("Reports " + sess) if sess else "Earnings report"
            if det.get("eps_est") is not None:
                subtitle += f"; expected EPS {det['eps_est']}"
            confirmed = det.get("verified") not in (0, False)
            if (r.get("status") or "") == "reported":
                extra["status"] = "done"
        elif typ == "ipo":
            subtitle = "IPO priced" if et == "ipo_pricing" else "Expected IPO date"
            confirmed = et == "ipo_pricing" or (r.get("status") or "") == "priced"
        elif et == "trial_completion":
            subtitle = "Trial's primary completion date in the registry (the sponsor's estimate)"
            confirmed = False
    if imp is None:
        imp = {"earnings": 2, "market": 2, "expiry": 2, "personal": 3}.get(typ, 1)
    try:
        imp = max(1, min(3, int(imp)))
    except (TypeError, ValueError):
        imp = 1
    if held and typ == "earnings":
        imp = 3
    elif held or (watched and typ in ("earnings", "company", "fda")):
        imp = max(imp, 2)
    changed = None
    pd_, on = det.get("prev_date"), parse_date(det.get("date_changed_on"))
    if pd_ and on and (today - on).days <= int(c.get("changed_days") or 14):
        changed = {"previous_date": pd_, "changed_on": on.isoformat()}
    item = {"id": r["id"], "date": r.get("event_date"), "end_date": det.get("end_date"), "time_et": time_et,
            "all_day": time_et is None, "type": typ, "kind": kind, "title": title, "subtitle": subtitle,
            "tickers": tickers, "held": held, "watched": watched, "importance": imp, "confirmed": confirmed,
            "source": src, "url": r.get("url"), "changed": changed,
            "status": "done" if (det.get("done") or r.get("status") == "done") else extra.get("status", "open"),
            "editable": src == OWNER_SOURCE, "note": det.get("note"), "created_by": _creator(r.get("created_by"))}
    for k in ("_ff", "_merge"):
        if k in extra:
            item[k] = extra[k]
    if kind in ("econ_release",) and det.get("release"):
        item["_merge"] = det["release"]
    if kind in ("fomc_decision", "fomc_minutes"):
        item["_merge"] = "fomc" if kind == "fomc_decision" else "fomc_minutes"
    if kind == "fed_speech" and "_merge" not in item:        # folded with Forex Factory's row for the same speech
        who = det.get("speaker") or re.sub(r"^Fed:\s*|\s+speaks$", "", title)
        sk = _speech_key(_surname(who), time_et)
        if sk:
            item["_merge"] = sk
    if typ == "earnings" and tickers:
        item["_merge"] = "earn:" + tickers[0]
    return item


def _merge_same_day(items: list) -> list:
    """Show one line per (day, release). The official schedule row wins; Forex Factory rows
    for the same release that day are folded into it and contribute forecast / previous.
    A Fed speech listed by both the Fed and Forex Factory is one line too, but only when the day,
    the clock time and the speaker's surname all agree (two speakers, or two times, stay two lines)."""
    groups, out = {}, []
    for it in items:
        mk = it.get("_merge")
        if not mk:
            out.append(it)
            continue
        groups.setdefault((it["date"], mk), []).append(it)
    for (_d, mk), g in groups.items():
        official = [x for x in g if "_ff" not in x]
        ff = [x for x in g if "_ff" in x]
        if mk.startswith("earn:"):
            g.sort(key=lambda x: (x["source"] != "Robinhood", x["id"]))   # mason_pead's own row first
            out.append(g[0])
            continue
        # several official rows for one release that day (FRED and the agency's own list): show one
        official.sort(key=lambda x: (not x["confirmed"], "via FRED" in x["source"], "usual rule" in x["source"], x["id"]))
        head = dict(official[0] if official else ff[0])
        if ff:
            parts = [f"{x['_ff']['name']}: forecast {x['_ff']['forecast']}, previous {x['_ff']['previous']}"
                     for x in ff if x["_ff"].get("forecast") or x["_ff"].get("previous")]
            if parts:
                head["subtitle"] = (head.get("subtitle") or "").rstrip(". ") + ". " + "; ".join(parts[:4])
                head["subtitle"] = head["subtitle"].lstrip(". ")
            if not head.get("time_et"):
                tm = sorted(x["time_et"] for x in ff if x.get("time_et"))
                if tm:
                    head["time_et"], head["all_day"] = tm[0], False
            if mk.startswith("fedspeech:"):               # one speech from two sources: the higher importance of the two
                head["importance"] = max(x["importance"] for x in g)
            if not official:
                head["importance"] = max(x["importance"] for x in ff)
                if mk == "fomc":
                    head.update(title="Fed rate decision", importance=3, kind="fomc_decision",
                                subtitle=head["subtitle"] or "The Fed announces its interest-rate decision.")
                elif mk == "fomc_minutes":
                    head.update(title="Fed meeting minutes", importance=2, kind="fomc_minutes")
        out.append(head)
    for it in out:
        it.pop("_ff", None); it.pop("_merge", None)
    return out


def _agenda_items(d0: date, d1: date, bk: dict) -> list:
    """Reminders with a date, option expiries and time stops, read through mason_agenda."""
    out = []
    try:
        import mason_agenda
        rows = mason_agenda.calendar_items(d0, d1)
    except Exception as e:
        log.debug(f"[calendar] agenda items unavailable: {e}")
        return out
    for r in rows:
        tk = _clean_tickers([r.get("ticker")])
        out.append({"id": r["id"], "date": r["date"], "end_date": None, "time_et": None, "all_day": True,
                    "type": r["type"], "kind": r["kind"], "title": r["title"], "subtitle": r.get("subtitle") or "",
                    "tickers": tk, "held": bool(tk) and r["kind"] != "reminder",
                    "watched": False, "importance": 3 if r["kind"] != "note" else 1, "confirmed": True,
                    "source": r.get("source") or "Mason", "url": None, "changed": None,
                    "status": "done" if r.get("done") else "open", "editable": r["kind"] in ("reminder", "note"),
                    "note": None, "created_by": _creator(r.get("created_by"))})
    return out


def list_events(date_from=None, date_to=None, *, types=None, held: bool = False, min_importance: int = 1,
                include_done: bool = True, today=None, small_trials: bool = True, bk: dict | None = None) -> list:
    """Everything on the calendar between two dates (inclusive), sorted by date then time.
    See CALENDAR_API.md for the shape. Never raises: [] on any failure.
    small_trials=False leaves out the small drug-trial dates (type fda, importance 1; one that touches a
    held or watched stock is importance 2 and stays). `bk` = book() when the caller already has it."""
    today = today_et(today)
    c = cfg()
    d0 = parse_date(date_from) or today
    d1 = parse_date(date_to) or (d0 + timedelta(days=31))
    if d1 < d0:
        d0, d1 = d1, d0
    want = set(types or []) & set(TYPE_KEYS) if types else None
    items = []
    try:
        conn = mason_db.get_connection()
        try:
            bk = bk if bk is not None else book(conn)
            # rows that start up to 45 days before the window, plus any older one that has a last day (a
            # long event still under way; checked against the window below)
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE record_deleted=0 AND event_date <= ? AND (event_date >= ? "
                "OR details_json LIKE ?) ORDER BY event_date",
                (d1.isoformat(), (d0 - timedelta(days=45)).isoformat(), '%"end_date"%')).fetchall()
        finally:
            conn.close()
        for r in rows:
            it = _row_item(dict(r), bk, c, today)
            if it is None:
                continue
            last = it["end_date"] or it["date"]
            if last < d0.isoformat():
                continue
            items.append(it)
        items = _merge_same_day(items)
        items += _agenda_items(d0, d1, bk)
    except Exception as e:
        log.warning(f"[calendar] list_events failed: {e}")
        return []
    out = []
    for it in items:
        if want is not None and it["type"] not in want:
            continue
        if held and not it["held"]:
            continue
        if it["importance"] < int(min_importance or 1):
            continue
        if not small_trials and it["type"] == "fda" and it["importance"] < 2:
            continue
        if not include_done and it["status"] == "done":
            continue
        out.append(it)
    out.sort(key=lambda i: (i["date"], i["time_et"] or "", -i["importance"], i["title"].lower(), i["id"]))
    return out


def meta(date_from=None, date_to=None, today=None) -> dict:
    """The fixed list of types with labels, and how many events each has in the window."""
    today = today_et(today)
    d0 = parse_date(date_from) or today
    d1 = parse_date(date_to) or (d0 + timedelta(days=366))
    counts = {k: 0 for k in TYPE_KEYS}
    n_imp = {1: 0, 2: 0, 3: 0}
    for it in list_events(d0, d1, today=today):
        counts[it["type"]] += 1
        n_imp[it["importance"]] += 1
    return {"from": d0.isoformat(), "to": d1.isoformat(),
            "types": [{"type": k, "label": lab, "count": counts[k]} for k, lab in TYPES],
            "importance": {"1": "worth knowing", "2": "can move a sector or a holding", "3": "can move the whole market, or it is yours"},
            "importance_counts": {str(k): v for k, v in n_imp.items()},
            "total": sum(counts.values())}


# ── HTTP: what the routes in mason_app.py call (contract: CALENDAR_API.md) ──────

def http_get(args) -> tuple:
    """GET /api/calendar -> (payload, status). `trials=0` leaves out the small drug-trial dates;
    `held_tickers` = every stock Nico or Mason holds (what `held` on each event is measured against)."""
    today = today_et()
    raw_from, raw_to = (args.get("from") or "").strip(), (args.get("to") or "").strip()
    d0 = parse_date(raw_from) if raw_from else today
    d1 = parse_date(raw_to) if raw_to else (d0 + timedelta(days=31) if d0 else None)
    if d0 is None or d1 is None:
        return {"error": "The dates asked for must be real days, written like 2026-12-31.", "code": "bad_date", "events": []}, 400
    types = [t.strip().lower() for t in (args.get("types") or "").split(",") if t.strip()]
    bad = [t for t in types if t not in TYPE_KEYS]
    if bad:
        return {"error": f"There is no kind of event called {', '.join(bad[:3])[:60]}. The kinds are: {', '.join(TYPE_KEYS)}.",
                "code": "bad_type", "events": []}, 400
    try:
        mi = max(1, min(3, int(args.get("min_importance") or 1)))
    except (TypeError, ValueError):
        return {"error": "Importance must be 1, 2 or 3.", "code": "bad_importance", "events": []}, 400
    bk = book()
    items = list_events(d0, d1, types=types or None, held=str(args.get("held") or "").lower() in ("1", "true", "yes"),
                        min_importance=mi, include_done=str(args.get("done") or "1").lower() not in ("0", "false", "no"),
                        today=today, small_trials=str(args.get("trials") or "1").lower() not in ("0", "false", "no"), bk=bk)
    return {"from": min(d0, d1).isoformat(), "to": max(d0, d1).isoformat(), "today": today.isoformat(),
            "count": len(items), "events": items, "held_tickers": sorted(bk["held"])}, 200


def _tickers_in(body):
    v = body.get("tickers")
    if v is None:
        return None
    return v if isinstance(v, (list, tuple)) else re.split(r"[,\s]+", str(v))


def _refuse(code: str, status: int = 400, plain: str | None = None) -> tuple:
    """An API error: a plain sentence for Nico plus a short code for the widget (CALENDAR_API.md §6)."""
    return {"error": plain or _PLAIN[code], "code": code}, status


def http_write(method: str, body: dict, args=None) -> tuple:
    """POST / PUT / DELETE /api/calendar -> (payload, status). Reminders (ids starting
    'rem:') are edited through mason_agenda, so there is one copy of each.
    Every refusal is {"error": <plain sentence>, "code": <short tag>}."""
    body = body if isinstance(body, dict) else {}
    try:
        if method == "POST":
            it = add_owner_event(body.get("title"), body.get("date"), time_et=body.get("time") or body.get("time_et") or None,
                                 end_date=body.get("end_date") or None, tickers=_tickers_in(body), note=body.get("note"),
                                 created_by="nico")
            return {"event": it}, 201
        cid = str(body.get("id") or (args.get("id") if args is not None else "") or "").strip()
        if not cid:
            return _refuse("no_id")
        if cid.startswith(("exp:", "stop:")):
            return _refuse("from_position")
        if cid.startswith("rem:"):
            import mason_agenda
            rid = cid[4:]
            if method == "DELETE":
                ok = mason_agenda.delete_reminder(rid, deleted_by="nico")
                return ({"deleted": cid}, 200) if ok else _refuse("not_found", 404)
            kw = {}
            if "title" in body:
                if not str(body.get("title") or "").strip():
                    return _refuse("no_title")
                kw["text"] = body.get("title") or ""
            if "date" in body:
                if body.get("date") and parse_date(body.get("date")) is None:
                    return _refuse("bad_date")
                kw["fire_on"] = body.get("date") or None
            ok = True
            if kw:
                ok = mason_agenda.update_reminder(rid, modified_by="nico", **kw)
            if ok and "done" in body:
                ok = mason_agenda.complete_reminder(rid, bool(body.get("done")))
            if not ok:
                return _refuse("not_found", 404)
            d = parse_date(body.get("date")) if body.get("date") else None
            its = [i for i in _agenda_items(d or date(2000, 1, 1), d or date(2999, 1, 1), book()) if i["id"] == cid]
            return {"event": its[0] if its else None}, 200
        if method == "DELETE":
            return ({"deleted": cid}, 200) if delete_event(cid, "nico") else _refuse("not_found", 404)
        kw = {}
        for src, dst in (("title", "title"), ("date", "when"), ("end_date", "end_date"), ("note", "note"), ("done", "done")):
            if src in body:
                kw[dst] = body.get(src)
        if "time" in body or "time_et" in body:
            kw["time_et"] = body.get("time") if "time" in body else body.get("time_et")
        if "tickers" in body:
            kw["tickers"] = _tickers_in(body) or []
        if not kw:
            return _refuse("nothing_to_change")
        return {"event": update_event(cid, **kw)}, 200
    except KeyError:
        return _refuse("not_found", 404)
    except CalendarInputError as e:
        return _refuse(e.code, 400, e.plain)
    except ValueError as e:
        log.info(f"[calendar] write refused ({method}): {e}")
        return _refuse("bad_input")


# ── lines for the director / the quick checks (switched off until approved) ─────

def _trading_days_ahead(today: date, n: int) -> date:
    """The date n trading days after today (weekends and stored market holidays skipped)."""
    hol = set()
    try:
        conn = mason_db.get_connection()
        try:
            hol = {r[0] for r in conn.execute(
                "SELECT event_date FROM calendar_events WHERE event_type='cal_market' AND record_deleted=0 "
                "AND json_extract(details_json,'$.kind')='holiday' AND event_date >= ?", (today.isoformat(),))}
        finally:
            conn.close()
    except Exception:
        pass
    d, left = today, int(n)
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in hol:
            left -= 1
    return d


def model_items(items: list, mine=()) -> list:
    """The calendar items a MODEL may read (director and quick-check lines, the chat tool). Drug decision
    dates written by mason_biocal (news phase 6c) pass only as calendar.biocal.show_models allows
    (off by default); every other row passes untouched. With none of those rows in `items` the very
    same list comes back, so the model-visible text is unchanged until the switch is turned on."""
    try:
        if not any(it.get("created_by") == "mason_biocal" for it in items):
            return items
        import mason_biocal
        return mason_biocal.coming_items(items, cfg(), mine=mine)
    except Exception:
        return [it for it in items if it.get("created_by") != "mason_biocal"]


def calendar_lines(tickers=None, days: int = 5, max_lines: int = 12, *, today=None, min_importance: int = 2,
                   only_tickers: bool = False, skip_types=()) -> list:
    """Today and the next `days` trading days as short lines: only events of importance 2-3,
    or ones that touch a held / watched name (or one of `tickers`). One line each, soonest
    first; the most important are kept when there are more than max_lines. [] on failure.
    For a check on ONE stock: only_tickers=True keeps other holdings' events out (an event
    then "touches" only the tickers passed in) and min_importance=3 keeps only market-wide
    events beside them; skip_types=("earnings",) leaves out what the caller already says."""
    try:
        today = today_et(today)
        end = _trading_days_ahead(today, days)
        mine = set(_clean_tickers(tickers))
        picked = []
        for it in model_items(list_events(today, end, include_done=False, today=today), mine):
            touches = bool(mine & set(it["tickers"])) or (not only_tickers and (it["held"] or it["watched"]))
            if it["type"] == "personal" or it["type"] in tuple(skip_types or ()):
                continue                                   # reminders reach the model elsewhere
            if only_tickers and it["tickers"] and not touches:
                continue                                   # another company's event
            if it["importance"] < int(min_importance) and not touches:
                continue
            picked.append((it, touches))
        if len(picked) > max_lines:
            keep = sorted(picked, key=lambda p: (-p[0]["importance"], not p[1], p[0]["date"]))[:max_lines]
            ids = {p[0]["id"] for p in keep}
            picked = [p for p in picked if p[0]["id"] in ids]
        out = []
        for it, _t in picked:
            d = parse_date(it["date"])
            day = "today" if d == today else d.strftime("%a %b %-d")
            when = f"{day} {it['time_et']} ET" if it["time_et"] else day
            who = ""
            if it["tickers"]:
                tag = " (held)" if it["held"] else (" (watched)" if it["watched"] else "")
                who = f" [{', '.join(it['tickers'][:4])}{tag}]"
            tail = "" if it["confirmed"] else " (date not confirmed)"
            if it["type"] == "earnings":
                sess = next((x for x in ("before the open", "after the close") if x in (it["subtitle"] or "")), "")
                tail = (", " + sess if sess else "") + tail
            if it["changed"]:
                tail += f" (moved from {it['changed']['previous_date']})"
            title = it["title"] if len(it["title"]) <= 110 else it["title"][:107].rstrip() + "…"
            out.append(f"{when}: {title}{who}{tail}"[:200])
        return out
    except Exception as e:
        log.debug(f"[calendar] calendar_lines failed: {e}")
        return []


def fomc_decision_days(today=None, upcoming_only: bool = False) -> list:
    """ISO dates of the stored FOMC decision days (for mason_agenda). [] when none."""
    try:
        conn = mason_db.get_connection()
        try:
            days = [r[0] for r in conn.execute(
                "SELECT event_date FROM calendar_events WHERE event_type='cal_fed' AND record_deleted=0 "
                "AND json_extract(details_json,'$.kind')='fomc_decision' ORDER BY event_date")]
        finally:
            conn.close()
        if upcoming_only:
            t = today_et(today).isoformat()
            days = [d for d in days if d >= t]
        return days
    except Exception:
        return []


# ── feed health records ─────────────────────────────────────────────────────────

_SNAP_SOURCE = "_calendar_feed"
FEED_LABELS = {
    "seed": "conference and event list", "computed": "computed dates (expiry days, deadlines)",
    "market": "market holidays (Alpaca)", "fed": "Federal Reserve calendar", "econ": "economic release schedule",
    "congress": "Congress hearings (congress.gov)", "court": "Supreme Court argument calendar",
    "fda": "FDA advisory committee meetings", "earnings": "bellwether earnings dates",
    "biocal": "list of FDA decision dates from company news",
    "tool_weekly": "weekly event search", "tool_recheck": "monthly re-check of unconfirmed dates",
}


def _snap_get(conn, source: str, key: str):
    try:
        r = conn.execute("SELECT payload_json FROM source_snapshots WHERE source=? AND key=? AND record_deleted=0",
                         (source, key)).fetchone()
        return json.loads(r[0]) if r and r[0] else None
    except Exception:
        return None


def _snap_put(conn, source: str, key: str, payload) -> None:
    conn.execute(
        "INSERT INTO source_snapshots (source, key, taken_at, payload_json, created_date, created_by, record_deleted) "
        "VALUES (?,?,?,?,?,?,0) ON CONFLICT(source, key) DO UPDATE SET taken_at=excluded.taken_at, "
        "payload_json=excluded.payload_json", (source, key, time.time(), _dumps(payload), _iso_now(), CREATED_BY))


def feed_state(conn, key: str) -> dict:
    return _snap_get(conn, _SNAP_SOURCE, key) or {}


def record_feed(conn, key: str, res: dict, ms: int, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    s = feed_state(conn, key)
    ok = bool(res.get("ok"))
    s.update(last_try=now, ok=ok, ms=int(ms), n=int(res.get("n") or 0),
             fail_streak=0 if ok else int(s.get("fail_streak") or 0) + 1,
             error=None if ok else str(res.get("error") or "failed")[:300],
             counts={k: (len(v) if isinstance(v, list) else v) for k, v in res.items()
                     if k in ("new", "moved", "changed", "same", "retired", "hidden", "kept", "seen")})
    if ok:
        s["last_ok"] = now
    if res.get("parts"):
        s["parts"] = res["parts"]
    with conn:
        _snap_put(conn, _SNAP_SOURCE, key, s)
    return s


def status() -> dict:
    """What every feed did last: when it last worked, how many events, the last error."""
    c = cfg()
    out = {"enabled": c.get("enabled") is True, "feeds": {}, "events": {}, "switches": {
        "tool": bool((c.get("tool") or {}).get("enabled")), "chat_tool": bool((c.get("chat_tool") or {}).get("enabled")),
        "director_block": bool((c.get("director_block") or {}).get("enabled")),
        "desk_check_block": bool((c.get("desk_check_block") or {}).get("enabled"))}}
    try:
        conn = mason_db.get_connection()
        try:
            now = time.time()
            for key, lab in FEED_LABELS.items():
                s = feed_state(conn, key)
                on = bool((c.get("feeds") or {}).get(key, False)) if not key.startswith("tool_") \
                    else bool((c.get("tool") or {}).get("enabled"))
                if key == "biocal" and not on and not s:
                    continue          # news phase 6c: not listed until it is switched on (the answer stays as it was)
                out["feeds"][key] = {
                    "label": lab, "on": on, "ok": s.get("ok"), "events": s.get("n"),
                    "last_ok": _ts_iso(s.get("last_ok")), "last_try": _ts_iso(s.get("last_try")),
                    "hours_since_ok": round((now - s["last_ok"]) / 3600, 1) if s.get("last_ok") else None,
                    "fail_streak": s.get("fail_streak") or 0, "error": s.get("error"),
                    "counts": s.get("counts") or {}, "parts": s.get("parts")}
            for r in conn.execute("SELECT event_type, COUNT(*) FROM calendar_events WHERE record_deleted=0 "
                                  "AND event_date >= ? GROUP BY 1", (today_et().isoformat(),)):
                out["events"][r[0]] = r[1]
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def _ts_iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=ET).isoformat(timespec="minutes") if ts else None
    except Exception:
        return None


def feed_health(keys) -> dict:
    """For mason_health: {'ok', 'detail'} from the stored feed records (no network)."""
    try:
        c = cfg()
        if c.get("enabled") is not True:
            return {"ok": True, "latency_ms": None, "detail": "calendar feeds are switched off"}
        conn = mason_db.get_connection()
        try:
            st = {k: feed_state(conn, k) for k in keys}
        finally:
            conn.close()
        limit_h = float((c.get("notify") or {}).get("feed_down_hours") or 24)
        bits, bad, seen = [], False, False
        for k, s in st.items():
            if not (c.get("feeds") or {}).get(k, False):
                bits.append(f"{k}: off")
                continue
            if not s.get("last_try"):
                bits.append(f"{k}: not run yet")
                continue
            seen = True
            age = (time.time() - float(s.get("last_ok") or 0)) / 3600 if s.get("last_ok") else None
            if age is None or (not s.get("ok") and age > limit_h) or age > max(limit_h, 3 * float((c.get("hours") or {}).get(k, 24))):
                bad = True
                bits.append(f"{k}: no good read for {age:.0f}h ({s.get('error') or 'no error text'})" if age is not None
                            else f"{k}: never read ({s.get('error') or 'no error text'})")
            elif not s.get("ok"):     # (2026-11-19) the last read failed: say so at once (it turns red after limit_h)
                bits.append(f"{k}: the last read failed ({s.get('error') or 'no error text'}); "
                            f"the last good one was {age:.0f}h ago")
            else:
                bits.append(f"{k}: {s.get('n', 0)} events, read {age:.0f}h ago")
        if not seen and not bad:      # switched off, or not run yet: not an outage, so ok=True (ok=None would now read as "not configured")
            return {"ok": True, "latency_ms": None, "detail": "; ".join(bits)}
        return {"ok": not bad, "latency_ms": None, "detail": "; ".join(bits)}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "detail": f"calendar status unreadable: {type(e).__name__}"}


# ── inbox notes ─────────────────────────────────────────────────────────────────

def _notify(title, body, *, dedup, severity="info", ticker=None, notifier=None):
    try:
        if notifier is None:
            import mason_notifications
            notifier = mason_notifications.add_notification
        return notifier(type="system", title=str(title)[:200], body=body, severity=severity, ticker=ticker,
                        created_by=CREATED_BY, dedup_key=dedup)
    except Exception as e:
        log.warning(f"[calendar] notification failed: {e}")
        return None


def _note_moves(moved: list, bk: dict, c: dict, notifier=None) -> int:
    if not (c.get("notify") or {}).get("date_changes", True):
        return 0
    n = 0
    for m in moved or []:
        tk = set(m.get("tickers") or [])
        mine = bool(tk & (bk["held"] | bk["watched"]))
        if int(m.get("importance") or 1) < 2 and not mine:
            continue
        who = f" It matters to {', '.join(sorted(tk))}, which you hold or watch." if mine else ""
        _notify(f"Date changed: {m['title']}"[:200],
                f"{m['title']} moved from {m['from']} to {m['to']}.{who}",
                dedup=f"calendar_moved:{m['id']}:{m['to']}", severity="warn" if mine else "info",
                ticker=(sorted(tk)[0] if len(tk) == 1 else None), notifier=notifier)
        n += 1
    return n


def _note_feed_down(conn, key: str, s: dict, c: dict, now: float, notifier=None) -> bool:
    limit_h = float((c.get("notify") or {}).get("feed_down_hours") or 24)
    if s.get("ok") or not s.get("fail_streak"):
        return False
    first_bad = float(s.get("last_ok") or s.get("first_fail") or now)
    if not s.get("last_ok") and not s.get("first_fail"):
        s["first_fail"] = now
        with conn:
            _snap_put(conn, _SNAP_SOURCE, key, s)
        return False
    if (now - first_bad) / 3600 < limit_h:
        return False
    day = today_et(now).isoformat()
    since = _ts_iso(s.get("last_ok")) or "it was switched on"
    _notify(f"Calendar: the {FEED_LABELS.get(key, key)} has not updated for more than a day",
            f"Mason has not been able to read the {FEED_LABELS.get(key, key)} since {since[:16].replace('T', ' ')}. "
            f"The calendar still shows the dates it already had, so a recent change may be missing.\n"
            f"Last error: {s.get('error') or 'none recorded'}",
            dedup=f"calendar_feed_down:{key}:{day}", severity="warn", notifier=notifier)
    return True


# ── the schedule ────────────────────────────────────────────────────────────────

def _feed_table() -> list:
    """[(key, fn)] in run order. fn(today, c) -> result dict; it opens its own short write."""
    import mason_calendar_feeds as F
    out = [("seed", F.feed_seed), ("computed", F.feed_computed), ("market", F.feed_market), ("fed", F.feed_fed),
           ("econ", F.feed_econ), ("earnings", F.feed_earnings), ("fda", F.feed_fda), ("court", F.feed_court),
           ("congress", F.feed_congress)]
    try:                       # news phase 6c (calendar.feeds.biocal, off by default): reads stored news only
        import mason_biocal as B
        out.append(("biocal", B.feed))
    except Exception as e:
        log.debug(f"[calendar] drug-date feed unavailable: {e}")
    return out


def run_feed(key: str, *, today=None, notifier=None, now: float | None = None) -> dict:
    """Run one feed now, record its health, send the date-change notes. Never raises."""
    c = cfg()
    today = today_et(today)
    fn = dict(_feed_table()).get(key)
    if fn is None:
        return {"ok": False, "error": f"no such feed: {key}"}
    t0 = time.monotonic()
    try:
        res = fn(today, c) or {}
    except Exception as e:
        log.warning(f"[calendar] feed {key} failed: {type(e).__name__}: {e}")
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    ms = int((time.monotonic() - t0) * 1000)
    try:
        conn = mason_db.get_connection()
        try:
            record_feed(conn, key, res, ms, now)
            if res.get("moved"):
                _note_moves(res["moved"], book(conn), c, notifier=notifier)
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[calendar] could not record feed {key}: {e}")
    return res


def tick(*, force: bool = False, only=None, now: float | None = None, notifier=None) -> dict:
    """Run whatever is due (called once an hour by mason_maintenance.tick). A feed that
    failed is tried again on the next pass (after six straight failures: every six
    hours). Stops starting new feeds once the pass has used `tick_budget_s`. Never raises."""
    out = {}
    try:
        c = cfg()
        if c.get("enabled") is not True:
            return {}
        now = time.time() if now is None else now
        today = today_et(now)
        t0 = time.monotonic()
        budget = float(c.get("tick_budget_s") or 150)
        conn = mason_db.get_connection()
        try:
            states = {k: feed_state(conn, k) for k, _ in _feed_table()}
        finally:
            conn.close()
        import mason_calendar_feeds as F
        F.set_deadline(None if force else t0 + budget)   # no request starts after the budget (see F._get)
        try:
            for key, _fn in _feed_table():
                if only is not None and key not in only:
                    continue
                if not (c.get("feeds") or {}).get(key, False):
                    continue
                s = states.get(key) or {}
                every = float((c.get("hours") or {}).get(key, 24)) * 3600
                fs = int(s.get("fail_streak") or 0)
                wait = every if fs == 0 else (3300 if fs < 6 else 6 * 3600)
                if not force and s.get("last_try") and now - float(s["last_try"]) < wait:
                    continue
                if not force and time.monotonic() - t0 > budget:
                    break
                res = run_feed(key, today=today, notifier=notifier, now=now)
                out[key] = {"ok": bool(res.get("ok")), "n": res.get("n"), "error": res.get("error")}
        finally:
            F.set_deadline(None)
        conn = mason_db.get_connection()
        try:
            for key, _fn in _feed_table():
                if (c.get("feeds") or {}).get(key, False):
                    _note_feed_down(conn, key, feed_state(conn, key), c, now, notifier=notifier)
        finally:
            conn.close()
        try:                                             # the model-driven jobs: off unless calendar.tool.enabled.
            import mason_calendar_tool as T              # They run in their own short-lived thread (up to 600 s
            r = T.scheduled_in_background(now=now)       # each), so this pass and the nightly backup never wait.
            if r:
                out["tool"] = r
        except Exception as e:
            log.debug(f"[calendar] tool schedule skipped: {e}")
    except Exception as e:
        log.warning(f"[calendar] tick failed: {e}")
    return out


# ── chat tool (offered only while calendar.chat_tool.enabled is true) ───────────

CHAT_TOOLS = [{
    "name": "calendar",
    "description": (
        "Nico's events calendar: Fed, economic data, earnings, Congress, conferences, expiries, his own events. "
        "action 'list' shows what is scheduled between two dates; 'add' puts an event on it; 'find' looks up "
        "upcoming events for a company, organisation or theme on official pages and adds the ones it can verify."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "find"]},
            "from": {"type": "string", "description": "list: first day, YYYY-MM-DD (default today)"},
            "to": {"type": "string", "description": "list: last day, YYYY-MM-DD (default 7 days later)"},
            "ticker": {"type": "string", "description": "list: only events for this ticker"},
            "title": {"type": "string", "description": "add: what the event is"},
            "date": {"type": "string", "description": "add: YYYY-MM-DD"},
            "time": {"type": "string", "description": "add: HH:MM Eastern, optional"},
            "tickers": {"type": "string", "description": "add: tickers it matters to, comma separated"},
            "subject": {"type": "string", "description": "find: a ticker, company, organisation, event name or theme"},
        },
        "required": ["action"],
    },
}]


def chat_tools() -> list:
    return list(CHAT_TOOLS) if chat_tool_enabled() else []


def _fmt_item(it: dict) -> str:
    d = parse_date(it["date"])
    when = d.strftime("%a %b %-d") + (f"–{parse_date(it['end_date']).strftime('%b %-d')}" if it.get("end_date") else "")
    if it.get("time_et"):
        when += f" {it['time_et']} ET"
    who = f" [{', '.join(it['tickers'][:4])}]" if it["tickers"] else ""
    tail = "" if it["confirmed"] else " (date not confirmed)"
    return f"- {when}: {it['title']}{who}{tail}"


def handle_chat_tool(name: str, inp: dict) -> str:
    if name != "calendar":
        return f"{name} is not a calendar tool"
    act = str(inp.get("action") or "list").lower()
    try:
        if act == "list":
            d0 = parse_date(inp.get("from")) or today_et()
            d1 = parse_date(inp.get("to")) or (d0 + timedelta(days=7))
            tk = str(inp.get("ticker") or "").strip().upper()
            items = model_items(list_events(d0, d1, include_done=False), [tk] if tk else ())
            if tk:
                items = [i for i in items if tk in i["tickers"]]
            else:
                items = [i for i in items if i["importance"] >= 2 or i["held"] or i["watched"]]
            if not items:
                return f"Nothing on the calendar between {d0} and {d1}" + (f" for {tk}." if tk else ".")
            more = f"\n({len(items) - 40} more not shown)" if len(items) > 40 else ""
            return f"Calendar {d0} to {d1}:\n" + "\n".join(_fmt_item(i) for i in items[:40]) + more
        if act == "add":
            it = add_owner_event(inp.get("title"), inp.get("date"), time_et=inp.get("time") or None,
                                 tickers=re.split(r"[,\s]+", str(inp.get("tickers") or "")), created_by="mason")
            return f"Added to the calendar: {_fmt_item(it)[2:]} (id {it['id']})."
        if act == "find":
            import mason_calendar_tool as T
            res = T.find_events(str(inp.get("subject") or ""), source="chat")
            return T.summary_text(res)
        return "calendar: action must be list, add or find"
    except (ValueError, KeyError) as e:
        return f"calendar error: {e}"


# ── command line ────────────────────────────────────────────────────────────────

def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mason events calendar.")
    ap.add_argument("--status", action="store_true", help="what each feed did last")
    ap.add_argument("--refresh", action="store_true", help="run every switched-on feed now")
    ap.add_argument("--feed", help="with --refresh: only this feed (seed, fed, econ, congress, ...)")
    ap.add_argument("--find", metavar="SUBJECT", help='look for events for a subject, e.g. --find "RKLB"')
    ap.add_argument("--recheck", action="store_true", help="re-check stored unconfirmed and expected events")
    ap.add_argument("--list", nargs="*", metavar="DATE", help="print events between two dates")
    ap.add_argument("--lines", action="store_true", help="print calendar_lines() as the director would see them")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if a.refresh:
        r = tick(force=True, only=[a.feed] if a.feed else None)
        print(json.dumps(r, indent=2, default=str))
        return 0 if all(v.get("ok") for k, v in r.items() if isinstance(v, dict) and "ok" in v) else 1
    if a.find or a.recheck:
        import mason_calendar_tool as T
        res = T.find_events(a.find, source="cli") if a.find else T.recheck(force=True)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("ok") else 1
    if a.lines:
        print("\n".join(calendar_lines()) or "(nothing)")
        return 0
    if a.list is not None:
        for it in list_events(*(a.list[:2])):
            print(_fmt_item(it), f"({it['type']}, importance {it['importance']}, {it['source']})")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_main())
