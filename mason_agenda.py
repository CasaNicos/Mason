"""
mason_agenda.py — Mason's date/event awareness engine.

Computes a prioritized agenda of things Nico should be reminded of THIS session:
  - option expiries  (T-1 trading-day warning + day-of)  from options_journal.json
  - option plays in their review window (expiry / 30d / 60d)
  - custom dated reminders Nico (or Mason) set        from mason_reminders.json
  - FOMC decision days (from the Fed's calendar page via mason_calendar; the static
    2026/2027 lists below are the fallback when that has no upcoming date)
  - upcoming earnings for watched tickers (optional cache, non-blocking)

Everything is LOCAL + INSTANT by default so the /api/greeting endpoint stays fast.

Each agenda item is a dict:
    {kind, urgency, ticker, when, days, line}
      urgency: 0 = today / overdue  (lead with it)
               1 = tomorrow / next trading day
               2 = this week
               3 = heads-up (within ~5 days)

Public API:
    build_agenda(today=None)      -> list[item]   (sorted, most urgent first)
    greeting_payload(today=None)  -> dict         (ready for the greeting endpoint)
    add_reminder(text, fire_on)   -> dict
    list_reminders()              -> list
    complete_reminder(rid)        -> bool
"""

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import mason_options
except Exception:          # pragma: no cover - defensive import
    mason_options = None

_DIR            = Path(__file__).parent
REMINDERS_FILE  = _DIR / "mason_reminders.json"
EARNINGS_CACHE  = _DIR / "earnings_cache.json"   # optional: [{"ticker","date"}]

# ── FOMC 2026 decision days (the market-moving 2nd day of each meeting) ─────────
# Verified against federalreserve.gov/monetarypolicy/fomccalendars.htm (June 2026).
FOMC_2026 = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]
# 2027 decision days as published on the same page on 2026-11-14 ("each meeting date is
# tentative until confirmed at the meeting immediately preceding it").
FOMC_2027 = [
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-09",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-11-17",
]
# These two lists are only the FALLBACK. The live dates come from the Fed's own calendar
# page through mason_calendar (feed "fed"); see fomc_days().
FOMC_FALLBACK = FOMC_2026 + FOMC_2027


def fomc_days(today=None) -> list:
    """FOMC decision days as ISO dates. The calendar's stored days (read from the Fed's
    page) are used when it holds at least one that is today or later; otherwise the
    fallback lists above, so the agenda never goes empty when the fetch has failed."""
    t = _today(today).isoformat()
    try:
        import mason_calendar
        days = set(mason_calendar.fomc_decision_days())
        if any(d >= t for d in days):
            # A meeting the lists below know and the calendar has no row for in that month (the row
            # was removed by hand, or a bad read of the page dropped it) still warns: the warning
            # must never disappear. A meeting that moved inside its month follows the calendar.
            have = {d[:7] for d in days}
            return sorted(days | {d for d in FOMC_FALLBACK if d[:7] not in have})
    except Exception:
        pass
    return list(FOMC_FALLBACK)


# ── small helpers ──────────────────────────────────────────────────────────────

def _today(today=None) -> date:
    if today is None:
        return date.today()
    if isinstance(today, date):
        return today
    return date.fromisoformat(str(today))


def _parse(d):
    try:
        return date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def _prev_business_day(d: date) -> date:
    """Most recent weekday strictly before d (ignores market holidays)."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:        # 5 = Sat, 6 = Sun
        p -= timedelta(days=1)
    return p


def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ── reminders store (shared with Mason's set_reminder tool) ─────────────────────

# ── Reminders now live in mason.db (table `reminders`), not mason_reminders.json.
# Every consumer (greeting, widget /api/reminders, the set_reminder tool, the brief)
# goes through these functions, so the storage swap is transparent. The returned
# dict keeps the historical keys (id/text/fire_on/created/done) and adds the new
# ones (created_by/completed/record_deleted). See mason_db._SCHEMA.
import mason_db


def _rem_row_to_dict(r) -> dict:
    d = dict(r)
    return {
        "id":             d.get("reminderid"),
        "text":           d.get("reminder_detail"),
        "fire_on":        d.get("reminder_date"),      # may be None (no date)
        "created":        d.get("createddate"),
        "created_by":     d.get("createdby"),
        "completed":      bool(d.get("completed")),
        "completed_date": d.get("completed_date"),     # when checked off (None if open)
        "done":          bool(d.get("completed")),    # back-compat: 'done' == completed
        "record_deleted": bool(d.get("record_deleted")),
        "deletedby":      d.get("deletedby"),
        "modified":       d.get("modifieddate"),       # last text/date edit, if any
        "modified_by":    d.get("modifiedby"),
        "kind":           d.get("kind") or "reminder",   # 'reminder' | 'note'
    }


def list_reminders(include_completed: bool = True) -> list:
    """Active (not record_deleted) reminders. Completed ones are INCLUDED by default
    so the widget can keep showing them checked off; pass include_completed=False for
    the greeting/agenda, which should not nag about ones already done."""
    sweep_completed_reminders()
    conn = mason_db.get_connection()
    try:
        q = "SELECT * FROM reminders WHERE record_deleted = 0"
        if not include_completed:
            q += " AND completed = 0"
        rows = conn.execute(q + " ORDER BY reminder_date IS NULL, reminder_date").fetchall()
    finally:
        conn.close()
    return [_rem_row_to_dict(r) for r in rows]


REMINDER_KINDS = ("reminder", "note")


def _norm_kind(kind) -> str:
    k = (str(kind or "reminder")).strip().lower()
    if k not in REMINDER_KINDS:
        raise ValueError(f"bad kind: {kind!r} (want one of {REMINDER_KINDS})")
    return k


def add_reminder(text: str, fire_on: str = None, created_by: str = "mason",
                 kind: str = "reminder") -> dict:
    """fire_on: 'YYYY-MM-DD' or None/'' for a dateless reminder. created_by: 'nico'
    (added in the UI) or 'mason' (set by the tool). kind: 'reminder' (default) or
    'note' — a note-to-self that shows in the widget but is never spoken in the
    greeting and never enters the agenda."""
    kind = _norm_kind(kind)
    rid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    fo = None
    if fire_on:
        p = _parse(fire_on)
        fo = p.isoformat() if p else (str(fire_on) or None)
    conn = mason_db.get_connection()
    try:
        conn.execute(
            "INSERT INTO reminders (reminderid, createddate, createdby, reminder_detail, "
            "reminder_date, record_deleted, completed, kind) VALUES (?,?,?,?,?,0,0,?)",
            (rid, now, created_by, (text or "").strip(), fo, kind),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": rid, "text": (text or "").strip(), "fire_on": fo, "created": now,
            "created_by": created_by, "completed": False, "done": False,
            "record_deleted": False, "kind": kind}


def complete_reminder(rid: str, completed: bool = True) -> bool:
    """Check a reminder off (or un-check with completed=False). It stays visible for
    COMPLETED_RETENTION_DAYS, then sweep_completed_reminders() soft-deletes it.
    Un-checking clears completed_date, so the countdown restarts if re-checked."""
    conn = mason_db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE reminders SET completed = ?, completed_date = ? "
            "WHERE reminderid = ? AND record_deleted = 0",
            (1 if completed else 0, datetime.now().isoformat() if completed else None, rid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_UNSET = object()


def update_reminder(rid: str, text: str = None, fire_on=_UNSET,
                    modified_by: str = "nico", kind: str = None) -> bool:
    """Edit a reminder's text and/or date in place. text=None leaves the text alone
    (blank text is rejected — delete it instead). fire_on left unset keeps the date;
    fire_on=None/'' clears it (dateless). Stamps modifieddate/modifiedby (audit);
    completed/record_deleted are untouched. Soft-deleted rows can't be edited.
    Returns False when no active row matched. Raises ValueError on bad input."""
    sets, args = [], []
    if text is not None:
        t = str(text).strip()
        if not t:
            raise ValueError("text cannot be blank")
        sets.append("reminder_detail = ?"); args.append(t)
    if fire_on is not _UNSET:
        fo = None
        if fire_on:
            p = _parse(fire_on)
            if not p:
                raise ValueError(f"bad date: {fire_on!r} (want YYYY-MM-DD)")
            fo = p.isoformat()
        sets.append("reminder_date = ?"); args.append(fo)
    if kind is not None:
        sets.append("kind = ?"); args.append(_norm_kind(kind))
    if not sets:
        raise ValueError("nothing to update")
    sets += ["modifieddate = ?", "modifiedby = ?"]
    args += [datetime.now().isoformat(), modified_by, rid]
    conn = mason_db.get_connection()
    try:
        cur = conn.execute(
            f"UPDATE reminders SET {', '.join(sets)} "
            "WHERE reminderid = ? AND record_deleted = 0", args)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_reminder(rid: str, deleted_by: str = "nico") -> bool:
    """Soft-remove: sets record_deleted + deleted_date + deletedby so it disappears
    from every view but the row is retained (audit). deleted_by: 'nico' | 'mason'."""
    conn = mason_db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE reminders SET record_deleted = 1, deleted_date = ?, deletedby = ? "
            "WHERE reminderid = ? AND record_deleted = 0",
            (datetime.now().isoformat(), deleted_by, rid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


COMPLETED_RETENTION_DAYS = 5
_last_sweep = None   # datetime of the last sweep; throttles it to once an hour


def sweep_completed_reminders(force: bool = False) -> int:
    """Soft-delete reminders checked off more than COMPLETED_RETENTION_DAYS ago
    (deletedby='mason-auto', row kept for audit/restore). Runs from list_reminders(),
    at most once an hour unless force=True. Returns how many were removed."""
    global _last_sweep
    now = datetime.now()
    if not force and _last_sweep and (now - _last_sweep) < timedelta(hours=1):
        return 0
    _last_sweep = now
    cutoff = (now - timedelta(days=COMPLETED_RETENTION_DAYS)).isoformat()
    conn = mason_db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE reminders SET record_deleted = 1, deleted_date = ?, deletedby = 'mason-auto' "
            "WHERE record_deleted = 0 AND completed = 1 "
            "AND completed_date IS NOT NULL AND completed_date < ?",
            (now.isoformat(), cutoff),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── agenda sources ──────────────────────────────────────────────────────────────

def _option_expiry_items(today: date) -> list:
    items = []
    if mason_options is None:
        return items
    try:
        plays = mason_options.get_open_plays()
    except Exception:
        return items

    for p in plays:
        exp = _parse(p.get("expiration"))
        if not exp:
            continue
        days   = (exp - today).days
        ticker = p.get("ticker", "?")
        strat  = (p.get("strategy") or "play").replace("_", " ")
        when   = exp.strftime("%a %b %-d") if hasattr(exp, "strftime") else str(exp)

        if days < 0:
            continue   # past expiry is handled by the review queue
        if days == 0:
            items.append({
                "kind": "expiry", "urgency": 0, "ticker": ticker, "when": when, "days": 0,
                "line": f"Your {ticker} {strat} expires TODAY. We should settle the exit before close.",
            })
        elif today == _prev_business_day(exp):
            tom = "tomorrow" if days == 1 else exp.strftime("%A")
            items.append({
                "kind": "expiry", "urgency": 1, "ticker": ticker, "when": when, "days": days,
                "line": f"Your {ticker} {strat} expires {tom} ({when}). Worth planning the exit now.",
            })
        elif 0 < days <= 5:
            items.append({
                "kind": "expiry", "urgency": 3, "ticker": ticker, "when": when, "days": days,
                "line": f"Your {ticker} {strat} expires in {days} days ({when}).",
            })
    return items


def _review_items(today: date) -> list:
    items = []
    if mason_options is None:
        return items
    try:
        pending = mason_options.get_plays_needing_review()
    except Exception:
        return items
    for it in pending:
        p  = it.get("play", {})
        rt = it.get("review_type", "expiry")
        ticker = p.get("ticker", "?")
        label  = {"expiry": "expiry review", "30d": "30-day review", "60d": "60-day review"}.get(rt, rt)
        items.append({
            "kind": "review", "urgency": 0, "ticker": ticker,
            "when": p.get("expiration", "?"), "days": None,
            "line": f"{ticker} is due for its {label} — let's score how it played out.",
        })
    return items


def _reminder_items(today: date) -> list:
    items = []
    for r in list_reminders():
        if r.get("done") or r.get("kind") == "note":
            continue   # notes-to-self never enter the agenda (widget-only)
        fire = _parse(r.get("fire_on"))
        if fire and fire <= today:
            items.append({
                "kind": "reminder", "urgency": 0, "ticker": None,
                "when": r.get("fire_on"), "days": (fire - today).days, "id": r.get("id"),
                "line": f"Reminder you set: {r.get('text')}",
            })
    return items


def _fomc_items(today: date) -> list:
    items = []
    for ds in fomc_days(today):
        d = _parse(ds)
        if not d:
            continue
        days = (d - today).days
        when = d.strftime("%a %b %-d")
        if days == 0:
            items.append({"kind": "fomc", "urgency": 0, "ticker": None, "when": when, "days": 0,
                          "line": f"FOMC decision lands TODAY ({when}). Expect a volatility spike around 2pm ET."})
        elif days == 1:
            items.append({"kind": "fomc", "urgency": 1, "ticker": None, "when": when, "days": 1,
                          "line": f"FOMC decision is tomorrow ({when}). Size accordingly into the print."})
        elif 2 <= days <= 5:
            items.append({"kind": "fomc", "urgency": 3, "ticker": None, "when": when, "days": days,
                          "line": f"FOMC decision is {days} days out ({when})."})
    return items


def _earnings_items(today: date) -> list:
    """Optional, non-blocking. Reads earnings_cache.json if present: [{ticker,date}]."""
    items = []
    cache = _load_json(EARNINGS_CACHE, [])
    for e in cache if isinstance(cache, list) else []:
        d = _parse(e.get("date"))
        t = e.get("ticker", "?")
        if not d:
            continue
        days = (d - today).days
        when = d.strftime("%a %b %-d")
        if days == 0:
            items.append({"kind": "earnings", "urgency": 0, "ticker": t, "when": when, "days": 0,
                          "line": f"{t} reports earnings TODAY ({when})."})
        elif days == 1:
            items.append({"kind": "earnings", "urgency": 1, "ticker": t, "when": when, "days": 1,
                          "line": f"{t} reports earnings tomorrow ({when})."})
        elif 2 <= days <= 5:
            items.append({"kind": "earnings", "urgency": 3, "ticker": t, "when": when, "days": days,
                          "line": f"{t} reports earnings in {days} days ({when})."})
    return items


# ── dated items for the calendar (mason_calendar reads these; nothing is stored twice) ──

def open_option_plays() -> list:
    """Open option plays (the same list the expiry warnings use). [] on any failure."""
    if mason_options is None:
        return []
    try:
        return list(mason_options.get_open_plays() or [])
    except Exception:
        return []


def _open_time_stops() -> list:
    """Mason's open trades that carry a time stop: [{id, ticker, date}]."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, ticker, occ, time_stop_date FROM agent_trades WHERE COALESCE(record_deleted,0)=0 "
            "AND status='open' AND time_stop_date IS NOT NULL AND time_stop_date <> ''").fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [{"id": r["id"], "ticker": r["ticker"], "occ": r["occ"], "date": str(r["time_stop_date"])[:10]} for r in rows]


def calendar_items(date_from, date_to) -> list:
    """Reminders that have a date, option expiries and time stops of open positions between
    two dates (inclusive), as plain dicts {id, type, kind, date, title, subtitle, ticker,
    done, source, created_by}. Ids are prefixed (rem: / exp: / stop:) so the calendar can route an
    edit back to the right store. Notes-to-self are included (kind 'note'). created_by is who set a
    reminder ('nico' / 'mason'); expiries and time stops have none."""
    d0, d1 = _parse(date_from), _parse(date_to)
    if not d0 or not d1:
        return []
    out = []
    try:
        for r in list_reminders():
            f = _parse(r.get("fire_on"))
            if f and d0 <= f <= d1:
                out.append({"id": f"rem:{r['id']}", "type": "personal", "kind": r.get("kind") or "reminder",
                            "date": f.isoformat(), "title": r.get("text") or "", "ticker": None,
                            "subtitle": "Note to self" if r.get("kind") == "note" else "Reminder",
                            "done": bool(r.get("done")), "source": "Reminders", "created_by": r.get("created_by")})
    except Exception:
        pass
    for p in open_option_plays():
        exp = _parse(p.get("expiration"))
        if exp and d0 <= exp <= d1:
            strat = (p.get("strategy") or "play").replace("_", " ")
            out.append({"id": f"exp:{p.get('id')}", "type": "expiry", "kind": "option_expiry",
                        "date": exp.isoformat(), "title": f"{p.get('ticker', '?')} {strat} expires",
                        "subtitle": "An open option position expires this day", "ticker": p.get("ticker"),
                        "done": False, "source": "Options journal"})
    try:
        for s in _open_time_stops():
            d = _parse(s["date"])
            if d and d0 <= d <= d1:
                out.append({"id": f"stop:{s['id']}", "type": "expiry", "kind": "time_stop", "date": d.isoformat(),
                            "title": f"{s['ticker']} time stop", "ticker": s["ticker"], "done": False,
                            "subtitle": "Mason closes this position on this day if it is still open",
                            "source": "Mason's trades"})
    except Exception:
        pass
    return out


# ── assembly ─────────────────────────────────────────────────────────────────────

def build_agenda(today=None) -> list:
    t = _today(today)
    items = []
    items += _reminder_items(t)
    items += _option_expiry_items(t)
    items += _review_items(t)
    items += _fomc_items(t)
    items += _earnings_items(t)
    # most urgent first; within a tier, soonest first
    items.sort(key=lambda i: (i["urgency"], i["days"] if i["days"] is not None else 99))
    return items


def greeting_payload(today=None) -> dict:
    """
    Returns:
      {
        "has_agenda": bool,
        "lead_line":  str,        # the single most important thing (or "")
        "items":      [ {kind,urgency,ticker,when,days,line}, ... up to 4 ],
        "summary":    str,        # one-paragraph spoken-friendly roll-up
      }
    """
    items = build_agenda(today)
    top   = items[:4]
    lead  = top[0]["line"] if top else ""
    if not top:
        summary = ""
    elif len(top) == 1:
        summary = top[0]["line"]
    else:
        summary = top[0]["line"] + " Also on the radar: " + \
                  "; ".join(i["line"] for i in top[1:])
    return {
        "has_agenda": bool(top),
        "lead_line":  lead,
        "items":      top,
        "summary":    summary,
    }


if __name__ == "__main__":
    import sys
    sim = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Agenda for {_today(sim)}:\n")
    for it in build_agenda(sim):
        print(f"  [{it['urgency']}] {it['kind']:8} {it['line']}")
    print("\nGreeting payload:")
    print(json.dumps(greeting_payload(sim), indent=2, default=str))
