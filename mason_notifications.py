"""
mason_notifications.py — Mason's durable, typed Notification Inbox.

Part of the 24/7 Analyst Desk (ANALYST_DESK_PHASE1_PROMPT.md, Phase 1). Owns the
`notifications` table in mason.db (defined in mason_db._SCHEMA). This is the inbox
the Source-Health Monitor writes source up/down transitions into, and where the
material-news bridge folds high-materiality book-relevant events so Nico sees one
consolidated feed. Phases 2/3 add 'completed_analysis' / 'recommendation' entries.

Design (mirrors mason_agenda.py's reminder helpers):
  * Every write goes through mason_db.get_connection() — a FRESH connection per
    call, closed in a finally. Flask is multithreaded; connections are never
    shared across threads. WAL is already on.
  * Soft delete only (record_deleted = 1 + deleted_date/deleted_by). Rows never
    physically leave the table. The active list filters record_deleted = 0.
  * Audit convention is the `reminders` spelling — created_date / created_by /
    record_deleted / deleted_date / deleted_by — per Nico's rule, NOT the trade
    tables' is_deleted spelling.
  * dedup_key (optional): a flapping source must not spam the inbox. When a key is
    given and an UNREAD, not-deleted notification with the same key exists inside a
    short window (_DEDUP_WINDOW_SECONDS), that row is UPDATED in place instead of a
    new one being inserted. The key rides in extra_json and is matched with
    SQLite's json_extract (JSON1, compiled into stdlib sqlite3). This is race-safe
    for the single health-monitor writer thread; it is not a hard cross-thread
    lock, which Phase 1 does not need.

Public API:
    add_notification(type, title, *, body=None, severity="info", ticker=None,
                     ref_table=None, ref_id=None, created_by="system",
                     dedup_key=None) -> dict
    list_notifications(*, unread_only=False, types=None, limit=50,
                       include_deleted=False) -> list[dict]
    get(notif_id) -> dict | None
    mark_read(notif_id) -> bool
    mark_read_many(notif_ids) -> int
    mark_all_read() -> int
    mark_unread(notif_id) -> bool
    mark_unread_many(notif_ids) -> int
    unread_count() -> int
    summary_counts() -> dict   # uncapped per-category unread/total for the badges
    soft_delete(notif_id, deleted_by="nico") -> bool
    soft_delete_many(notif_ids, deleted_by="nico") -> int
    clear_feed(deleted_by="nico", return_ids=False) -> int | list[str]  # soft-delete all feed-bucket rows
    restore_many(notif_ids) -> int                  # undo a soft delete (the inbox Undo toast)
    prune_old(feed_days=30, keep_days=90, ...) -> dict   # retention: hard-delete aged rows
    get_review_detail(notif_id) -> dict | None      # notif + full linked record
    review(notif_id, verdict, note=None, reviewed_by="nico") -> dict | None

Bridge (called from the health-monitor loop, NOT from the news scan loop):
    sync_material_news(limit=50) -> int

One inbox item per story (2026-11-15; the rules live in mason_news_quality):
    attach_story_item(notif_id, entry=...) -> bool    # fold a headline under a row
    upgrade_story_item(notif_id, payload=..., ...) -> bool   # replace an UNREAD row in place
"""

from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timedelta

import mason_db
from mason_redact import redact_text as _redact_text

# A flapping source shouldn't create a fresh inbox row every tick: an unread
# notification with the same dedup_key inside this window is updated in place.
_DEDUP_WINDOW_SECONDS = 6 * 3600

_VALID_SEVERITY = {"info", "warn", "critical"}
_SEV_RANK = {"info": 0, "warn": 1, "critical": 2}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _cutoff_iso(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat()


def _load_extra(raw) -> dict:
    if not raw:
        return {}
    try:
        d = _json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _row_to_dict(r) -> dict:
    """One notifications row → the plain dict the API/UI consume. is_read /
    record_deleted come back as bools; extra_json is parsed into `extra` and the
    dedup_key surfaced for convenience."""
    d = dict(r)
    extra = _load_extra(d.pop("extra_json", None))
    d["is_read"] = bool(d.get("is_read"))
    d["record_deleted"] = bool(d.get("record_deleted"))
    d["extra"] = extra
    d["dedup_key"] = extra.get("dedup_key")
    return d


# ── reads ────────────────────────────────────────────────────────────────────

def get(notif_id: str) -> dict | None:
    """Fetch one notification by id (regardless of read/deleted state)."""
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (notif_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_notifications(*, unread_only: bool = False, types=None, limit: int = 50,
                       include_deleted: bool = False) -> list[dict]:
    """Newest-first list. Filters:
      unread_only     — only is_read = 0
      types           — str or iterable of type strings to include
      include_deleted — include soft-deleted rows (default: hide them)
    """
    where = []
    params: list = []
    if not include_deleted:
        where.append("record_deleted = 0")
    if unread_only:
        where.append("is_read = 0")
    if types:
        if isinstance(types, str):
            types = [t.strip() for t in types.split(",") if t.strip()]
        types = list(types)
        if types:
            where.append("type IN (%s)" % ",".join("?" for _ in types))
            params.extend(types)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        lim = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        lim = 50
    params.append(lim)
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM notifications {clause} "
            f"ORDER BY created_date DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def unread_count() -> int:
    """Count of active (not deleted), unread notifications."""
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE record_deleted = 0 AND is_read = 0"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# Column routing for the notifications UI — kept in sync with mason_v2.html
# (reviewable() and VP_ALERT_TYPES there). Three buckets:
#   * Needs review — analyses/recommendations Nico signs off.
#   * Alerts       — the big, eyes-on stuff only: source outages + huge events.
#   * The Feed     — everything else active (major moves + misc), the ambient
#                    stream, shown alongside external material-news pings.
_REVIEWABLE_TYPES = ("completed_analysis", "recommendation")
_ALERT_TYPES = ("huge_event", "source_status", "outage", "fill_divergence")


def _bucket(ntype: str) -> str:
    if ntype in _REVIEWABLE_TYPES:
        return "review"
    if ntype in _ALERT_TYPES:
        return "alerts"
    return "feed"


def summary_counts() -> dict:
    """Category counts for the inbox badges, computed over the WHOLE active table
    (never a fetched page) so the UI numbers don't cap at the list limit. Buckets
    match the notifications UI (see _bucket): reviewable → 'Needs review',
    outages/huge events → 'Alerts', everything else → 'The Feed'. Returns unread
    AND total per bucket, plus overall totals (the gauge reads `unread`/`total`):

        {review_unread, review_total, alerts_unread, alerts_total,
         feed_unread, feed_total, unread, total}
    """
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT type, COUNT(*) AS c, "
            "SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) AS u "
            "FROM notifications WHERE record_deleted = 0 GROUP BY type"
        ).fetchall()
    finally:
        conn.close()
    acc = {"review": [0, 0], "alerts": [0, 0], "feed": [0, 0]}   # bucket -> [unread, total]
    for r in rows:
        c = int(r["c"] or 0)
        u = int(r["u"] or 0)
        b = acc[_bucket(r["type"])]
        b[0] += u
        b[1] += c
    return {
        "review_unread": acc["review"][0], "review_total": acc["review"][1],
        "alerts_unread": acc["alerts"][0], "alerts_total": acc["alerts"][1],
        "feed_unread":   acc["feed"][0],   "feed_total":   acc["feed"][1],
        "unread": acc["review"][0] + acc["alerts"][0] + acc["feed"][0],
        "total":  acc["review"][1] + acc["alerts"][1] + acc["feed"][1],
    }


# ── writes ─────────────────────────────────────────────────────────────────────

def add_notification(type: str, title: str, *, body: str = None,
                     severity: str = "info", ticker: str = None,
                     ref_table: str = None, ref_id: str = None,
                     created_by: str = "system", dedup_key: str = None,
                     extra: dict = None) -> dict:
    """Insert a notification (or, when dedup_key collapses onto a recent unread
    one, update that row in place). Returns the resulting notification dict.
    `extra` (optional, 2026-11-15 news quality) is stored in extra_json beside the
    dedup_key — e.g. the 'story' block mason_news_quality uses to keep one inbox
    item per story."""
    if severity == "warning":                 # (11-17) callers that write "warning" mean "warn"
        severity = "warn"
    if severity not in _VALID_SEVERITY:
        severity = "info"
    # N11: the last gate before the DB. Callers build title/body from exception, URL and header text, and a requests
    # error carries the request URL with its apikey=. Redact credentials here so no notifier can store one (the search
    # indexer copies this table into mason_search.db). Clean text comes back unchanged.
    if isinstance(title, str):
        title = _redact_text(title)
    if isinstance(body, str):
        body = _redact_text(body)
    now = _now_iso()
    conn = mason_db.get_connection()
    try:
        if dedup_key:
            cutoff = _cutoff_iso(_DEDUP_WINDOW_SECONDS)
            existing = conn.execute(
                "SELECT * FROM notifications WHERE record_deleted = 0 AND is_read = 0 "
                "AND json_extract(extra_json, '$.dedup_key') = ? AND created_date >= ? "
                "ORDER BY created_date DESC LIMIT 1",
                (dedup_key, cutoff),
            ).fetchone()
            if existing is not None:
                prev_extra = _load_extra(existing["extra_json"])
                if isinstance(extra, dict):
                    prev_extra.update(extra)
                extra = prev_extra
                extra["dedup_key"] = dedup_key
                with conn:
                    conn.execute(
                        "UPDATE notifications SET created_date = ?, created_by = ?, "
                        "type = ?, severity = ?, title = ?, body = ?, ticker = ?, "
                        "ref_table = ?, ref_id = ?, extra_json = ? WHERE id = ?",
                        (now, created_by, type, severity, title, body, ticker,
                         ref_table, ref_id, _json.dumps(extra), existing["id"]),
                    )
                return get(existing["id"])

        nid = uuid.uuid4().hex[:12]
        ex = dict(extra) if isinstance(extra, dict) else {}
        if dedup_key:
            ex["dedup_key"] = dedup_key
        extra_json = _json.dumps(ex) if ex else None
        with conn:
            conn.execute(
                "INSERT INTO notifications (id, created_date, created_by, type, "
                "severity, title, body, ticker, ref_table, ref_id, is_read, "
                "read_date, record_deleted, deleted_date, deleted_by, extra_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,0,NULL,NULL,?)",
                (nid, now, created_by, type, severity, title, body, ticker,
                 ref_table, ref_id, extra_json),
            )
        return get(nid)
    finally:
        conn.close()


def mark_read(notif_id: str) -> bool:
    """Mark one notification read. Returns True if a row changed."""
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET is_read = 1, read_date = ? "
                "WHERE id = ? AND is_read = 0 AND record_deleted = 0",
                (_now_iso(), notif_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_all_read() -> int:
    """Mark every active unread notification read. Returns how many changed."""
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET is_read = 1, read_date = ? "
                "WHERE is_read = 0 AND record_deleted = 0",
                (_now_iso(),),
            )
        return cur.rowcount
    finally:
        conn.close()


def mark_unread(notif_id: str) -> bool:
    """Mark one notification unread (undo a mis-click). Returns True if a row changed."""
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET is_read = 0, read_date = NULL "
                "WHERE id = ? AND is_read = 1 AND record_deleted = 0",
                (notif_id,),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_unread_many(notif_ids: list[str]) -> int:
    """Mark several notifications unread in one go — the 'Undo' for mark_all_read.
    Returns how many changed."""
    ids = [i for i in (notif_ids or []) if i]
    if not ids:
        return 0
    conn = mason_db.get_connection()
    try:
        placeholders = ",".join("?" for _ in ids)
        with conn:
            cur = conn.execute(
                f"UPDATE notifications SET is_read = 0, read_date = NULL "
                f"WHERE id IN ({placeholders}) AND record_deleted = 0",
                ids,
            )
        return cur.rowcount
    finally:
        conn.close()


_VALID_VERDICT = {"accept", "reject", "needs_work"}

# The full record a reviewable notification points at, so the review surface can
# show the whole analysis/recommendation — not just the one-line notification.
_DETAIL_TABLES = {"recommendations", "qwen_news_analysis", "news_events"}


def get_review_detail(notif_id: str) -> dict | None:
    """The review surface payload for one notification: the notification itself PLUS
    the full linked record it produced (recommendation / qwen analysis / news event),
    resolved via ref_table + ref_id. Returns {notification, detail, detail_table} or
    None if the notification doesn't exist. `detail` is None when there's nothing to
    link to (e.g. a plain source_status notification)."""
    n = get(notif_id)
    if n is None:
        return None
    detail = None
    ref_table = n.get("ref_table")
    ref_id = n.get("ref_id")
    if ref_table in _DETAIL_TABLES and ref_id:
        conn = mason_db.get_connection()
        try:
            row = conn.execute(
                f"SELECT * FROM {ref_table} WHERE id = ?", (ref_id,)
            ).fetchone()
            detail = dict(row) if row else None
        except Exception:
            detail = None
        finally:
            conn.close()
    return {"notification": n, "detail": detail, "detail_table": ref_table}


def review(notif_id: str, verdict: str, *, note: str = None,
           reviewed_by: str = "nico") -> dict | None:
    """Record Nico's review of a completed analysis / recommendation. verdict ∈
    {'accept','reject','needs_work'}. Stores the verdict + note + timestamp in the
    notification's extra_json.review, marks the notification read, reflects the
    decision on a linked recommendation (accept → 'accepted', reject → 'rejected';
    an already-graded rec is left alone), and — because a review is itself an action
    Nico took — appends a 'review' row to the activity ledger (actor='nico',
    trigger='chat'). Returns the updated notification, or None if not found / bad
    verdict."""
    if verdict not in _VALID_VERDICT:
        return None
    n = get(notif_id)
    if n is None:
        return None
    now = _now_iso()
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            "SELECT extra_json FROM notifications WHERE id = ?", (notif_id,)
        ).fetchone()
        extra = _load_extra(row["extra_json"] if row else None)
        extra["review"] = {"verdict": verdict, "note": note or None,
                           "by": reviewed_by or "nico", "date": now}
        with conn:
            conn.execute(
                "UPDATE notifications SET extra_json = ?, is_read = 1, "
                "read_date = COALESCE(read_date, ?) WHERE id = ?",
                (_json.dumps(extra), now, notif_id),
            )
            # reflect accept/reject on a linked recommendation (only if still open)
            if n.get("ref_table") == "recommendations" and n.get("ref_id") and verdict in ("accept", "reject"):
                new_status = "accepted" if verdict == "accept" else "rejected"
                conn.execute(
                    "UPDATE recommendations SET status = ? WHERE id = ? "
                    "AND status = 'open'",
                    (new_status, n["ref_id"]),
                )
    finally:
        conn.close()

    # A note written by mason_protect carries an action (record a stop, sell an option Nico
    # holds, put a protective order back). Accept / Reject is Nico's decision on it; the plain-words
    # outcome is stored as the review note, which the inbox shows next to the verdict.
    if n.get("created_by") == "mason_protect":
        try:
            import mason_protect
            res = mason_protect.on_review(notif_id, verdict, note=note)
            if isinstance(res, dict) and res.get("text"):
                conn = mason_db.get_connection()
                try:
                    with conn:
                        row = conn.execute("SELECT extra_json FROM notifications WHERE id = ?",
                                           (notif_id,)).fetchone()
                        extra = _load_extra(row["extra_json"] if row else None)
                        rv = dict(extra.get("review") or {})
                        rv["note"] = _redact_text(((note + " | ") if note else "") + str(res["text"]))
                        extra["review"] = rv
                        conn.execute("UPDATE notifications SET extra_json = ? WHERE id = ?",
                                     (_json.dumps(extra), notif_id))
                finally:
                    conn.close()
        except Exception:
            pass

    # the review is an activity (Nico did it, from a chat) — link back to the notif
    try:
        import mason_activity
        tk = n.get("ticker")
        label = {"accept": "accepted", "reject": "rejected",
                 "needs_work": "flagged for rework"}[verdict]
        title = f"Nico {label}: {n.get('title', '')[:80]}"
        mason_activity.log_activity(
            "review", title, actor="nico", category="analyst", ticker=tk,
            trigger="chat", body=note or None, ref_table="notifications",
            ref_id=notif_id, extra={"verdict": verdict})
    except Exception:
        pass
    return get(notif_id)


def soft_delete(notif_id: str, deleted_by: str = "nico") -> bool:
    """Soft-remove: record_deleted = 1 + deleted_date/deleted_by. The row stays in
    the table (audit) but drops out of every active list. Returns True if changed."""
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET record_deleted = 1, deleted_date = ?, "
                "deleted_by = ? WHERE id = ? AND record_deleted = 0",
                (_now_iso(), deleted_by or "nico", notif_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def soft_delete_many(notif_ids, deleted_by: str = "nico") -> int:
    """Soft-delete several notifications in one statement — the bulk 'Delete' from
    the inbox's multi-select. Same soft semantics as soft_delete (record_deleted = 1
    + deleted_date/deleted_by; rows stay for audit). Already-deleted rows are skipped
    by the WHERE. Returns how many rows changed."""
    ids = [i for i in (notif_ids or []) if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE notifications SET record_deleted = 1, deleted_date = ?, "
                f"deleted_by = ? WHERE id IN ({placeholders}) AND record_deleted = 0",
                (_now_iso(), deleted_by or "nico", *ids),
            )
        return cur.rowcount
    finally:
        conn.close()


def mark_read_many(notif_ids) -> int:
    """Mark several notifications read in one statement — the bulk 'Mark read' from
    the inbox's multi-select (the counterpart to mark_unread_many). Only flips rows
    that are currently unread and not deleted. Returns how many changed."""
    ids = [i for i in (notif_ids or []) if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE notifications SET is_read = 1, read_date = ? "
                f"WHERE id IN ({placeholders}) AND is_read = 0 AND record_deleted = 0",
                (_now_iso(), *ids),
            )
        return cur.rowcount
    finally:
        conn.close()


# The set of types NOT in the feed bucket, used to select feed-bucket rows with a
# NOT IN (…) clause (feed is the catch-all: not reviewable, not an alert).
_NON_FEED_TYPES = tuple(_REVIEWABLE_TYPES) + tuple(_ALERT_TYPES)


def clear_feed(deleted_by: str = "nico", return_ids: bool = False):
    """Soft-delete every ACTIVE feed-bucket notification (the ambient stream:
    major moves + misc — everything that isn't reviewable or an alert). This is
    the 'Clear feed' action: the stream can pile up without bound, and unlike
    Mark-all-read (which only flips is_read) this actually empties the column.
    Rows stay in the table (audit) until the retention prune reclaims them.
    Returns how many rows were cleared, or (return_ids=True) the list of cleared
    ids so the inbox Undo toast can hand them to restore_many()."""
    placeholders = ",".join("?" for _ in _NON_FEED_TYPES)
    conn = mason_db.get_connection()
    try:
        with conn:
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM notifications WHERE record_deleted = 0 "
                f"AND type NOT IN ({placeholders})", _NON_FEED_TYPES).fetchall()]
            if ids:
                id_ph = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE notifications SET record_deleted = 1, deleted_date = ?, "
                    f"deleted_by = ? WHERE id IN ({id_ph}) AND record_deleted = 0",
                    (_now_iso(), deleted_by or "nico", *ids),
                )
        return ids if return_ids else len(ids)
    finally:
        conn.close()


def restore_many(notif_ids) -> int:
    """Undo a soft delete — the inbox's 4-second Undo toast after a delete / bulk
    delete / Clear feed. Clears record_deleted + deleted_date/deleted_by so the rows
    rejoin every active list exactly as they were (read state untouched). Only
    flips rows that are currently deleted. Returns how many changed."""
    ids = [i for i in (notif_ids or []) if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = mason_db.get_connection()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE notifications SET record_deleted = 0, deleted_date = NULL, "
                f"deleted_by = NULL WHERE id IN ({placeholders}) AND record_deleted = 1",
                tuple(ids),
            )
        return cur.rowcount
    finally:
        conn.close()


def prune_old(*, feed_days: int = 30, keep_days: int = 90,
              purge_soft_deleted_days: int = 7) -> dict:
    """Retention: hard-DELETE aged notifications so the table doesn't grow without
    bound (this system is otherwise append-only). Three passes, all by created/
    deleted date:
      * feed-bucket rows (major moves + misc) older than `feed_days`
      * reviewable + alert rows older than `keep_days` (kept longer — they matter
        more, so a wider window)
      * anything already soft-deleted whose deleted_date is older than
        `purge_soft_deleted_days` (reclaim the audit rows eventually)
    IMPORTANT rows are exempt from ALL passes regardless of age: anything Nico
    reviewed (a verdict in extra_json.review) or that produced a recommendation
    (ref_table='recommendations'). Those carry a decision worth keeping for
    reference, so retention never removes them.
    Returns {feed, kept, soft} counts removed. Referenced news_events are pruned
    in lockstep by mason_news_service.prune_old (same window), so detail links
    don't dangle for rows still inside the window."""
    feed_cut = _cutoff_iso(feed_days * 86400)
    keep_cut = _cutoff_iso(keep_days * 86400)
    soft_cut = _cutoff_iso(purge_soft_deleted_days * 86400)
    non_feed = _NON_FEED_TYPES
    ph = ",".join("?" for _ in non_feed)
    # "Important" rows are never hard-deleted at any age: anything Nico acted on
    # (a review verdict recorded) or that produced a recommendation (ref_table).
    # The decision record it carries is worth keeping for reference. Appended to
    # every pass's WHERE so retention only ever removes ambient/unacted rows.
    _IMPORTANT = (" AND json_extract(extra_json, '$.review') IS NULL "
                  " AND NOT (ref_table = 'recommendations' AND ref_id IS NOT NULL) ")
    conn = mason_db.get_connection()
    try:
        with conn:
            feed_n = conn.execute(
                f"DELETE FROM notifications WHERE type NOT IN ({ph}) "
                f"AND created_date < ?" + _IMPORTANT, (*non_feed, feed_cut)).rowcount
            kept_n = conn.execute(
                f"DELETE FROM notifications WHERE type IN ({ph}) "
                f"AND created_date < ?" + _IMPORTANT, (*non_feed, keep_cut)).rowcount
            # soft-deleted purge also spares acted-on/linked rows (audit of a decision)
            soft_n = conn.execute(
                "DELETE FROM notifications WHERE record_deleted = 1 "
                "AND COALESCE(deleted_date, created_date) < ?" + _IMPORTANT,
                (soft_cut,)).rowcount
        return {"feed": feed_n, "kept": kept_n, "soft": soft_n}
    finally:
        conn.close()


# ── one inbox item per story (2026-11-15 news quality; rules in mason_news_quality) ──

_ALSO_CAP = 25          # folded entries kept in extra.story.also
_ALSO_BODY_LINES = 6    # of them listed in the body ("More on this story:")
_MORE_HDR = "More on this story:"


def _also_line(e: dict) -> str:
    h = " ".join(str(e.get("h") or "").replace("[", "(").replace("]", ")").split())[:160]
    url = str(e.get("url") or "").strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    src = str(e.get("src") or "").strip()
    line = f"- [{h}]({url})" if url.startswith(("http://", "https://")) else f"- {h}"
    return line + (f"  — {src}" if src else "")


def _body_with_more(body: str, also: list) -> str:
    """`body` with its 'More on this story:' block rebuilt from `also`."""
    base = (body or "").split("\n\n" + _MORE_HDR)[0].rstrip()
    listed = [a for a in also if a.get("h") and not a.get("repeat_of_lead")]
    shown = listed[-_ALSO_BODY_LINES:]
    if not shown:
        return base
    lines = [_also_line(a) for a in shown]
    extra_n = len(listed) - len(shown)
    if extra_n > 0:
        lines.append(f"(+{extra_n} more)")
    return base + "\n\n" + _MORE_HDR + "\n" + "\n".join(lines)


def _merge_story(st: dict, entry: dict) -> dict:
    st = dict(st or {})
    # news_ids: every article the row covers (the bridge's "seen" set, same-article match).
    # catalyst_ids stay the row's OWN: a folded headline's triage bucket is kept on its
    # 'also' entry only, or the row would match every bucket it ever absorbed (review fix)
    st["news_ids"] = sorted(set(st.get("news_ids") or []) | set(entry.get("news_ids") or []))
    st["catalyst_ids"] = sorted(set(st.get("catalyst_ids") or []))
    if entry.get("kind"):
        st["kinds"] = sorted(set(st.get("kinds") or []) | {entry["kind"]})
    if entry.get("family"):
        st["families"] = sorted(set(st.get("families") or []) | {entry["family"]})
    also = list(st.get("also") or [])
    also.append({k: v for k, v in entry.items() if v not in (None, "", [])})
    st["also"] = also[-_ALSO_CAP:]
    return st


def _norm_h(h) -> str:
    return " ".join(str(h or "").lower().split())


def _repeats_lead(row, st: dict, entry: dict) -> bool:
    """Is `entry` the row's own lead article (same news id, or the very same headline)?
    news.quality.skip_repeat_lead; never raises."""
    try:
        import mason_news_quality as NQ
        if not NQ.on("skip_repeat_lead"):
            return False
        folded = set()
        for a in st.get("also") or []:
            if not a.get("repeat_of_lead"):       # a repeat IS the lead article, not a folded one
                folded |= set(a.get("news_ids") or [])
        lead_ids = set(st.get("news_ids") or []) - folded
        if row["ref_table"] == "news_events" and row["ref_id"]:
            lead_ids.add(row["ref_id"])
        ids = set(entry.get("news_ids") or [])
        if ids and ids <= lead_ids:
            return True
        lead_h = st.get("h") or ""
        if not lead_h:
            lead_h = str(row["title"] or "")
            if lead_h.startswith("Breaking:") and " — " in lead_h:
                lead_h = lead_h.split(" — ", 1)[1]
        return bool(entry.get("h")) and _norm_h(entry.get("h")) == _norm_h(lead_h)
    except Exception:
        return False


def attach_story_item(notif_id: str, *, entry: dict) -> bool:
    """Fold another headline of the same story under an existing inbox row: it is
    recorded in extra.story.also and listed under 'More on this story:' in the body.
    The row's read state and date are NOT touched — a folded headline is not a new
    alert. Returns True when the row was updated."""
    conn = mason_db.get_connection()
    try:
        row = conn.execute("SELECT * FROM notifications WHERE id = ? AND record_deleted = 0",
                           (notif_id,)).fetchone()
        if row is None:
            return False
        extra = _load_extra(row["extra_json"])
        st = extra.get("story") if isinstance(extra.get("story"), dict) else {}
        if not st:
            st = {"news_ids": [row["ref_id"]] if row["ref_table"] == "news_events" and row["ref_id"] else [],
                  "catalyst_ids": [row["ref_id"]] if row["ref_table"] == "catalysts" and row["ref_id"] else []}
        if _repeats_lead(row, st, entry):
            # follow-up 3 (2026-11-16): the row's OWN article again (e.g. a Breaking read of an
            # article Nico already read) is recorded, but not listed under the row a second time
            entry = dict(entry, repeat_of_lead=True)
        st = _merge_story(st, entry)
        extra["story"] = st
        body = row["body"] or ""
        if entry.get("h"):
            body = _body_with_more(body, st["also"])
        with conn:
            conn.execute("UPDATE notifications SET body = ?, extra_json = ? WHERE id = ?",
                         (_redact_text(body) if body else body, _json.dumps(extra), notif_id))
        return True
    except Exception:
        return False
    finally:
        conn.close()


def upgrade_story_item(notif_id: str, *, payload: dict, entry: dict = None, story: dict = None) -> bool:
    """Replace an UNREAD inbox row with a stronger item for the same story (a
    Breaking read of the same article, or the triage Catalyst alert): one row, the
    new title/body/type, dated now; the headline it replaces is kept under 'More on
    this story:'. A read row is never upgraded (returns False — the caller folds or
    posts instead)."""
    conn = mason_db.get_connection()
    try:
        row = conn.execute("SELECT * FROM notifications WHERE id = ? AND record_deleted = 0 AND is_read = 0",
                           (notif_id,)).fetchone()
        if row is None:
            return False
        if row["created_by"] == "mason_triage" and (payload.get("created_by") or "") != "mason_triage":
            return False      # never replace a Catalyst alert with a weaker item (two streams racing)
        old_extra = _load_extra(row["extra_json"])
        old_st = old_extra.get("story") if isinstance(old_extra.get("story"), dict) else {}
        st = dict(story or {})
        for k in ("news_ids", "catalyst_ids", "kinds", "families"):
            st[k] = sorted(set(st.get(k) or []) | set(old_st.get(k) or []))
        if row["ref_table"] == "news_events" and row["ref_id"]:
            st["news_ids"] = sorted(set(st["news_ids"]) | {row["ref_id"]})
        if row["ref_table"] == "catalysts" and row["ref_id"]:
            st["catalyst_ids"] = sorted(set(st["catalyst_ids"]) | {row["ref_id"]})
        old_h = old_st.get("h") or row["title"] or ""
        also = list(old_st.get("also") or [])
        new_h = (story or {}).get("h") or ""
        if old_h and not (new_h and " ".join(old_h.lower().split()) in " ".join(str(payload.get("title") or "").lower().split())):
            src = ""
            for ln in (row["body"] or "").splitlines():
                if ln.startswith("Source: "):
                    src = ln[8:].split("](")[0].lstrip("[")
            url = ""
            try:
                import re as _re
                m = _re.search(r"\]\((https?://[^)\s]+)\)", row["body"] or "")
                url = m.group(1) if m else ""
            except Exception:
                pass
            also.append({"h": old_h[:300], "src": src, "url": url, "stream": old_st.get("stream") or row["created_by"],
                         "at": row["created_date"], "how": "upgraded_from"})
        if entry and entry.get("note"):
            st["note"] = entry["note"]
        st["also"] = also[-_ALSO_CAP:]
        extra = dict(old_extra)
        extra["story"] = st
        extra["upgraded_from"] = {"type": row["type"], "created_by": row["created_by"], "title": row["title"],
                                  "created_date": row["created_date"]}
        if payload.get("dedup_key"):
            extra["dedup_key"] = payload["dedup_key"]
        sev = payload.get("severity") or row["severity"]
        if sev not in _VALID_SEVERITY:
            sev = "info"
        ntype = payload.get("type") or row["type"]
        if _SEV_RANK.get(row["severity"], 0) > _SEV_RANK.get(sev, 0):
            # review fix 2026-11-16: a re-title never lowers the row — a red news row
            # (huge_event / warn) taken over by a stronger-titled yellow headline stays red
            sev, ntype = row["severity"], row["type"]
        title = payload.get("title") or row["title"]
        body = _body_with_more(payload.get("body") if payload.get("body") is not None else (row["body"] or ""), st["also"])
        with conn:
            cur = conn.execute(
                "UPDATE notifications SET created_date = ?, created_by = ?, type = ?, severity = ?, title = ?, "
                "body = ?, ticker = ?, ref_table = ?, ref_id = ?, extra_json = ? WHERE id = ? AND is_read = 0",
                (_now_iso(), payload.get("created_by") or row["created_by"], ntype, sev,
                 _redact_text(title) if isinstance(title, str) else title,
                 _redact_text(body) if isinstance(body, str) else body,
                 payload.get("ticker") or row["ticker"], payload.get("ref_table") or row["ref_table"],
                 payload.get("ref_id") or row["ref_id"], _json.dumps(extra), notif_id))
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


def _story_news_ids(conn) -> set:
    """news_ids already covered by an inbox row's story (own ref or folded)."""
    out = set()
    try:
        for r in conn.execute("SELECT j.value FROM notifications n, json_each(n.extra_json, '$.story.news_ids') j "
                              "WHERE n.extra_json IS NOT NULL").fetchall():
            if r[0]:
                out.add(r[0])
        for r in conn.execute("SELECT a.value FROM notifications n, json_each(n.extra_json, '$.story.also') a "
                              "WHERE n.extra_json IS NOT NULL").fetchall():
            try:
                out |= set((_json.loads(r[0]) or {}).get("news_ids") or [])
            except Exception:
                pass
    except Exception:
        pass
    return out


# ── material-news → inbox bridge (least-invasive; called from the health loop) ──

def sync_material_news(limit: int = 50, *, source=None) -> int:
    """Fold material, book-relevant news events into the durable inbox as
    type='huge_event' / 'news' (was 'major_move' before 2026-11-15) so they appear
    alongside source-health and analysis notifications. Idempotent: an event already
    recorded (matched by ref_table='news_events' + ref_id=news_id, or folded into a
    story row, read or not) is skipped, so this can run every health tick without
    duplicating.

    2026-11-15 (news quality, behind news.quality.*): each event first passes
    mason_news_quality.gate_event — US-listed subject, routine items held back, news
    older than max_age_hours not posted — and is then placed with
    mason_news_quality.place: a story already in the inbox gets this headline folded
    under it instead of a new row.

    Reads mason_news_service.notifications() by default; `source` is injectable for
    tests (a zero-arg callable returning the same shape). Does NOT touch the news
    scan loop. Returns the number of new inbox rows created."""
    if source is None:
        try:
            import mason_news_service as _NS
            source = lambda: _NS.notifications(limit=limit)
        except Exception:
            return 0
    try:
        events = source() or []
    except Exception:
        return 0
    if not events:
        return 0

    try:
        import mason_news_quality as NQ
        quality = NQ.on("enabled")
    except Exception:
        NQ, quality = None, False

    conn = mason_db.get_connection()
    try:
        existing_rows = conn.execute(
            "SELECT ref_id FROM notifications WHERE ref_table = 'news_events' "
            "AND ref_id IS NOT NULL"
        ).fetchall()
        seen = {r["ref_id"] for r in existing_rows}
        if quality:
            seen |= _story_news_ids(conn)
    finally:
        conn.close()

    created = 0
    for ev in events:
        nid = ev.get("id")
        if not nid or nid in seen:
            continue
        headline = ev.get("headline") or ev.get("title") or ""
        if not headline:
            continue
        impact = (ev.get("impact") or "").lower()
        # red = a genuinely big move on the book -> 'huge_event'/critical-ish;
        # anything else material is a 'major_move'. Severity stays honest: red is a
        # warn (it's news, not a system fault -> not 'critical'), else info.
        ntype = "huge_event" if impact == "red" else "major_move"
        severity = "warn" if impact == "red" else "info"
        tickers = ev.get("tickers") or []
        ticker = tickers[0] if tickers else None
        gate = None
        if quality:
            gate = NQ.gate_event(ev, stream="news")
            if not gate.get("ok"):
                seen.add(nid)
                continue
            ticker = gate.get("ticker") or ticker
            if ntype == "major_move" and NQ.on("relabel_news"):
                ntype = "news"         # a headline is news; 'major_move' is a price move (mason_watch_signals)
        impl = ev.get("implication")
        src = ev.get("source") or "News"
        url = ev.get("url") or ""
        blurb = (ev.get("desc") or "").strip()
        # a clickable source link (markdown; the UI linkifies it) + the feed blurb
        src_line = f"[{src}]({url})" if url else src
        parts = []
        if blurb:
            parts.append(blurb)
        if impl:
            parts.append(impl)
        parts.append(f"Source: {src_line}")
        body = "\n\n".join(parts)
        payload = dict(type=ntype, title=headline, body=body, severity=severity, ticker=ticker,
                       ref_table="news_events", ref_id=nid, created_by="news_service",
                       dedup_key=f"news:{nid}")
        if quality and gate is not None:
            new = NQ.new_item(stream="news", ticker=ticker, headline=headline, news_ids=[nid],
                              catalyst_ids=[ev.get("catalyst_id")] if ev.get("catalyst_id") else [],
                              kind=gate.get("kind"), family=gate.get("family"), strong=gate.get("strong"),
                              mat=ev.get("materiality"))
            r = NQ.place(new=new, payload=payload, url=url, source=src)
            seen.add(nid)
            if r.get("action") == "new":
                created += 1
            continue
        add_notification(**payload)
        seen.add(nid)
        created += 1
    return created


if __name__ == "__main__":
    # Manual smoke: python3 mason_notifications.py
    n = add_notification("system", "Notification inbox online",
                         body="mason_notifications.py smoke test",
                         severity="info", created_by="system")
    print("added:", n["id"], n["title"])
    print("unread:", unread_count())
    for it in list_notifications(limit=5):
        print(f"  [{it['severity']}] {it['type']:<18} {it['title']}")
