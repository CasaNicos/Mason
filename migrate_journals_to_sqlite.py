"""
migrate_journals_to_sqlite.py — one-time backfill of the JSON journals into mason.db.

Sources:
    trades_journal.json            -> trades           (is_deleted = 0)
    trades_journal_deleted.json    -> trades           (is_deleted = 1)
    options_journal.json           -> options_trades   (is_deleted = 0)
    options_journal_deleted.json   -> options_trades   (is_deleted = 1)

READ-ONLY against every JSON file — this script never modifies or deletes them.
Safe to re-run: each id is checked against the DB first and skipped if already
present, so a partial run followed by a rerun will not duplicate rows.
options_journal_refusals.json is intentionally NOT migrated — it is a log of
refused *attempts*, not trade records.

Usage:
    python3 migrate_journals_to_sqlite.py --dry-run   # report only, write nothing
    python3 migrate_journals_to_sqlite.py             # migrate for real
"""

import argparse
import json
from pathlib import Path

import mason_db as DB

BASE = Path(__file__).parent


def _load(name):
    p = BASE / name
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _existing_ids(conn, table):
    return {r[0] for r in conn.execute(f"SELECT id FROM {table}").fetchall()}


def _migrate(conn, table, live_file, deleted_file, deleted_key, dry_run):
    live = _load(live_file)
    deleted_log = _load(deleted_file)
    existing = _existing_ids(conn, table)

    rows, skipped, bad = [], 0, 0

    for t in live:
        if not isinstance(t, dict) or not t.get("id"):
            bad += 1
            continue
        if t["id"] in existing:
            skipped += 1
            continue
        row = dict(t)
        row["is_deleted"] = 0
        row["deleted_at"] = None
        row["deleted_reason"] = None
        rows.append(row)
        existing.add(row["id"])

    for entry in deleted_log:
        t = entry.get(deleted_key, {}) if isinstance(entry, dict) else {}
        if not isinstance(t, dict) or not t.get("id"):
            bad += 1
            continue
        if t["id"] in existing:
            skipped += 1
            continue
        row = dict(t)
        row["is_deleted"] = 1
        row["deleted_at"] = entry.get("deleted_at")
        row["deleted_reason"] = entry.get("reason") or "migrated from deleted-log"
        rows.append(row)
        existing.add(row["id"])

    print(f"{table}: {len(live)} live + {len(deleted_log)} in {deleted_file} "
          f"-> {len(rows)} new rows to insert "
          f"({skipped} already present, {bad} skipped as malformed)")

    if dry_run or not rows:
        return len(rows)

    DB.upsert_rows(conn, table, rows)
    print(f"{table}: inserted {len(rows)} rows.")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be inserted, write nothing")
    args = ap.parse_args()

    conn = DB.get_connection()

    _migrate(conn, "trades",
             "trades_journal.json", "trades_journal_deleted.json", "trade",
             args.dry_run)
    _migrate(conn, "options_trades",
             "options_journal.json", "options_journal_deleted.json", "play",
             args.dry_run)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
    else:
        print("\nDone. Verify with:  sqlite3 mason.db \"SELECT status, is_deleted, "
              "COUNT(*) FROM trades GROUP BY status, is_deleted\"")


if __name__ == "__main__":
    main()
