"""
mason_memory_store.py — Mason's durable long-term memory (facts & threads).

Distinct from:
  - conversation_state.json (raw chat continuity, handled in mason_app)
  - mason_reminders.json     (dated reminders, handled in mason_agenda)

This stores distilled facts Mason chooses to remember about Nico, his book,
preferences, and ongoing theses — loaded into the system prompt every session.

Persistence is SQLite (mason.db, table `memory_facts`) via mason_db.py — the same
store the trade / options journals use. The pre-2026-09 JSON file
(mason_memory.json) was migrated by migrate_memory_to_sqlite.py and is kept in
_backups/ only.

Public API:
    add_fact(text, tag="note")        -> dict
    list_facts()                      -> list
    forget_fact(fid, reason=...)      -> bool   (SOFT delete — row kept, hidden)
    update_fact(fid, text=, tag=)     -> dict | None
    list_forgotten_facts()            -> list
    render_for_prompt()               -> str    (memory section for the system prompt)

forget_fact() used to physically drop the fact. It now soft-deletes: the row
stays in memory_facts with is_deleted=1 / deleted_at / deleted_reason, out of
list_facts() and render_for_prompt() but recoverable (see list_forgotten_facts).
"""

import uuid
from datetime import datetime
from pathlib import Path

import mason_db as DB

# Vestigial — retained so anything importing the constant still resolves. The
# file itself is no longer read or written; migrate_memory_to_sqlite.py moved its
# contents into mason.db.
MEM_FILE = Path(__file__).parent / "mason_memory.json"
_MAX_FACTS_IN_PROMPT = 50


def _facts_from_rows(rows) -> list:
    """DB rows -> the list shape callers already expect: `created` (not
    `created_at`), no updated_at / soft-delete bookkeeping."""
    out = []
    for r in rows:
        d = DB.row_to_dict(r, "memory_facts")
        d["created"] = d.pop("created_at", None)
        d.pop("updated_at", None)
        out.append(d)
    return out


def list_facts() -> list:
    """Every remembered fact, oldest first. Reads the active_memory_facts view,
    so a forgotten (soft-deleted) fact can never appear here."""
    conn = DB.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM active_memory_facts ORDER BY created_at, id"
        ).fetchall()
        return _facts_from_rows(rows)
    finally:
        conn.close()


def add_fact(text: str, tag: str = "note") -> dict:
    """Remember a new fact. If a byte-equivalent fact is already remembered
    (same text after whitespace/case normalisation, ignoring a redundant leading
    "(tag) " prefix), returns that one and writes nothing — keeps the
    every-prompt memory block from bloating with repeats."""
    text = (text or "").strip()
    tag = tag or "note"

    norm_new = _norm(text)
    if norm_new:
        for f in list_facts():
            if _norm(f.get("text", "")) == norm_new:
                return f

    now = datetime.now().isoformat()
    # created_by: who saved the fact — 'mason' (the tool wrote it, the default),
    # 'nico', or 'system'. Pairs with created_at for the creation audit trail.
    fact = {"id": str(uuid.uuid4())[:8], "text": text, "tag": tag,
            "created_at": now, "created_by": "mason", "updated_at": now}
    DB.with_connection(DB.upsert_rows, "memory_facts", [fact])
    fact["created"] = fact.pop("created_at")
    fact.pop("updated_at", None)
    return fact


def update_fact(fid: str, text: str = None, tag: str = None) -> dict | None:
    """Edit a remembered fact in place. Returns the updated fact, or None if the
    id is unknown / already forgotten."""
    conn = DB.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM active_memory_facts WHERE id = ?", (fid,)
        ).fetchone()
        if row is None:
            return None
        cur = dict(row)
        if text is not None:
            cur["text"] = text.strip()
        if tag is not None:
            cur["tag"] = tag or "note"
        cur["updated_at"] = datetime.now().isoformat()
        DB.upsert_rows(conn, "memory_facts", [cur])
        return _facts_from_rows([cur])[0]
    finally:
        conn.close()


def forget_fact(fid: str, reason: str = "forgotten via tool",
                deleted_by: str = "nico") -> bool:
    """Soft-delete a fact: is_deleted=1, deleted_at=now, deleted_reason=reason,
    deleted_by=who ('nico' by default; 'system' from dedupe, 'mason' if the
    assistant decided). Returns True if a live fact was forgotten, False for an
    unknown id or one already forgotten. The row is not removed — see
    list_forgotten_facts()."""
    conn = DB.get_connection()
    try:
        row = conn.execute(
            "SELECT is_deleted FROM memory_facts WHERE id = ?", (fid,)
        ).fetchone()
        if row is None or row[0]:
            return False
        DB.soft_delete(conn, "memory_facts", fid, reason or "forgotten via tool",
                       datetime.now().isoformat(), deleted_by=deleted_by)
        return True
    finally:
        conn.close()


def list_forgotten_facts() -> list:
    """Every soft-deleted fact, most recently forgotten first. The undo trail for
    forget_fact()."""
    conn = DB.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM memory_facts WHERE is_deleted = 1 "
            "ORDER BY deleted_at DESC, id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["created"] = d.get("created_at")
            out.append({k: d.get(k) for k in
                        ("id", "text", "tag", "created", "deleted_at",
                         "deleted_reason", "deleted_by")})
        return out
    finally:
        conn.close()


def render_for_prompt() -> str:
    facts = list_facts()
    if not facts:
        return ""
    lines = [f"- ({f.get('tag', 'note')}) {f.get('text', '')}"
             for f in facts[-_MAX_FACTS_IN_PROMPT:]]
    return ("Saved facts about Nico and his book (remembered across sessions):\n"
            + "\n".join(lines))


# ── helpers ──────────────────────────────────────────────────────────────────

import re as _re

_TAG_PREFIX = _re.compile(r"^\(\s*[a-z_]{3,15}\s*\)\s*", _re.I)


def _norm(text: str) -> str:
    """Whitespace-collapsed, case-folded, leading '(tag) ' stripped. Two facts
    with the same _norm() are the same fact for dedupe purposes. Matches
    dedupe_memory.py."""
    t = _TAG_PREFIX.sub("", str(text or "").strip())
    return _re.sub(r"\s+", " ", t).casefold()


if __name__ == "__main__":
    import json
    print(json.dumps({"facts": list_facts(),
                      "forgotten": list_forgotten_facts()}, indent=2, default=str))
