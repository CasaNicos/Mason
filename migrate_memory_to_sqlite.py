"""
migrate_memory_to_sqlite.py — one-time backfill of mason_memory.json into mason.db.

Source:
    mason_memory.json  ["facts"]  ->  memory_facts   (is_deleted = 0)

    JSON key  `created`  ->  column  `created_at`
    updated_at           <-  the file's top-level `updated` stamp (fallback: the
                             fact's own `created`)

READ-ONLY against mason_memory.json — this script never modifies or deletes it.
Safe to re-run: each id is checked against the DB first and skipped if already
present.

There is no deleted-side to migrate: forget_fact() was a hard delete before this,
so that history is already gone. mason_memory.json.predupe-*.bak is a pre-dedupe
snapshot, not a source of truth — ignored.

Usage:
    python3 migrate_memory_to_sqlite.py --dry-run   # report only, write nothing
    python3 migrate_memory_to_sqlite.py             # migrate for real
"""

import argparse
import json
from pathlib import Path

import mason_db as DB

BASE = Path(__file__).parent
MEM_FILE = BASE / "mason_memory.json"


def _load_facts():
    if not MEM_FILE.exists():
        return [], None
    data = json.loads(MEM_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return [], None
    facts = data.get("facts", [])
    return (facts if isinstance(facts, list) else []), data.get("updated")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be inserted, write nothing")
    args = ap.parse_args()

    conn = DB.get_connection()
    facts, file_updated = _load_facts()
    existing = {r[0] for r in conn.execute("SELECT id FROM memory_facts").fetchall()}

    rows, skipped, bad = [], 0, 0
    for f in facts:
        if not isinstance(f, dict) or not f.get("id"):
            bad += 1
            continue
        if f["id"] in existing:
            skipped += 1
            continue
        created = f.get("created")
        rows.append({
            "id":            f["id"],
            "text":          (f.get("text") or "").strip(),
            "tag":           f.get("tag") or "note",
            "created_at":    created,
            "updated_at":    file_updated or created,
            "is_deleted":    0,
            "deleted_at":    None,
            "deleted_reason": None,
        })
        existing.add(f["id"])

    print(f"memory_facts: {len(facts)} facts in {MEM_FILE.name} "
          f"-> {len(rows)} new rows to insert "
          f"({skipped} already present, {bad} skipped as malformed)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return
    if not rows:
        print("Nothing to insert.")
        return

    DB.upsert_rows(conn, "memory_facts", rows)
    print(f"memory_facts: inserted {len(rows)} rows.")
    print("\nVerify with:  sqlite3 mason.db "
          "\"SELECT tag, COUNT(*) FROM memory_facts GROUP BY tag\"")


if __name__ == "__main__":
    main()
