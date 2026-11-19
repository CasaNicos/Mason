"""
mason_maintenance.py — periodic retention so the append-only tables on mason.db
don't grow without bound.

Background: notifications, news_events and the activity ledger are all
append-only (soft-delete only, and soft-deleted rows stay in the table). Nothing
ever reclaimed them, so news_events in particular (continuous RSS ingest) grows
unbounded and the notifications "feed" pile never drains. This module adds the
missing retention pass:

  * notifications — mason_notifications.prune_old (feed rows > 30d, review/alert
    rows > 90d, soft-deleted rows > 7d)
  * news_events   — mason_news_service.prune_old (> 30d, skipping any still
    referenced by an active notification)
  * activity      — mason_activity.prune_old (> 30d)
  * a weekly VACUUM to actually reclaim the freed pages.
  * a nightly safety copy of the database (mason_backup.py, audit O1): once a night
    from 02:30 ET, to a folder outside the repo, health-checked and compressed.
  * catalysts / dossiers (audit O9) — prune_unreferenced(): expired catalysts and old
    dossiers that NOTHING else in the database mentions. Switched OFF by default
    (`maintenance.prune_catalysts` / `maintenance.prune_dossiers`): these rows are the
    raw material for the planned nightly grader, and the measured saving is small.

Scheduling is a single daemon thread with day/week guards persisted to a small
JSON state file, so a restart doesn't re-run a prune it already did today. It is
deliberately independent of the analyst-desk scheduler (isolated blast radius):
one crash here can't take the desk down and vice-versa. Everything is best-effort
and swallows its own errors — a maintenance hiccup must never break a request.

Public API:
    tick(*, force=False, now=None) -> dict     # run whatever is due; returns what ran
    run_prune() -> dict
    run_vacuum() -> bool
    run_backup(now=None) -> dict                # the nightly safety copy, right now
    backup_due(st, now, cfg=None) -> bool
    prune_unreferenced(conn, *, dry_run=True, ...) -> dict
    start(check_interval=3600) -> None          # launch the daemon (idempotent)

Manual use (see --help):
    python3 mason_maintenance.py --backup-now [--db PATH --out DIR]
    python3 mason_maintenance.py --prune-refs --db PATH [--apply]
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path

import mason_db

log = logging.getLogger("mason.maintenance")

_STATE_FILE = Path(__file__).parent / ".mason_maintenance.json"
_CONFIG_FILE = Path(__file__).parent / "mason.config.json"

PRUNE_INTERVAL = 24 * 3600        # nightly
VACUUM_INTERVAL = 7 * 24 * 3600   # weekly

# retention windows (days) — 30-day baseline; review/alert kept longer
FEED_DAYS = 30
KEEP_DAYS = 90
SOFT_DELETED_DAYS = 7
NEWS_DAYS = 30
ACTIVITY_DAYS = 30

# catalysts / dossiers (audit O9). OFF unless the config switches them on.
_MAINT_DEFAULTS = {
    "prune_catalysts": False,       # expired catalysts nothing else mentions
    "prune_dossiers": False,        # old dossiers nothing else mentions
    "catalyst_expired_days": 30,    # days since the story EXPIRED (last news + 72 h)
    "dossier_days": 30,             # days since the dossier was built
}
_CATALYST_EXPIRY_LAG_S = 72 * 3600  # mason_triage expires a story 72 h after its last news
BACKUP_RETRY_GAP = 3 * 3600         # after a failed backup, wait this long before retrying
BACKUP_STALE_AFTER = 36 * 3600      # no good backup for this long -> catch up outside the window


def _maint_cfg() -> dict:
    """`maintenance` block of mason.config.json over the defaults. Read on each call."""
    cfg = dict(_MAINT_DEFAULTS)
    try:
        raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")).get("maintenance") or {}
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    except Exception:
        pass
    return cfg


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(st))
    except Exception:
        pass


def run_prune() -> dict:
    """Run every table's retention pass. Each is isolated so one failure doesn't
    abort the rest. Returns a per-table summary of what was removed."""
    out: dict = {}
    try:
        import mason_notifications as N
        out["notifications"] = N.prune_old(
            feed_days=FEED_DAYS, keep_days=KEEP_DAYS,
            purge_soft_deleted_days=SOFT_DELETED_DAYS)
    except Exception as e:
        log.warning(f"[maintenance] notifications prune failed: {e}")
        out["notifications_error"] = str(e)
    try:
        import mason_news_service as NS
        out["news_events"] = NS.prune_old(days=NEWS_DAYS)
    except Exception as e:
        log.warning(f"[maintenance] news_events prune failed: {e}")
        out["news_events_error"] = str(e)
    try:
        import mason_activity as A
        out["activity"] = A.prune_old(days=ACTIVITY_DAYS)
    except Exception as e:
        log.warning(f"[maintenance] activity prune failed: {e}")
        out["activity_error"] = str(e)
    try:
        cfg = _maint_cfg()
        if cfg.get("prune_catalysts") or cfg.get("prune_dossiers"):
            conn = mason_db.get_connection()
            try:
                r = prune_unreferenced(
                    conn, dry_run=False,
                    catalysts=bool(cfg.get("prune_catalysts")),
                    dossiers=bool(cfg.get("prune_dossiers")),
                    catalyst_days=float(cfg.get("catalyst_expired_days", 30)),
                    dossier_days=float(cfg.get("dossier_days", 30)))
            finally:
                conn.close()
            out["catalysts"] = r["catalysts"]["deleted"]
            out["dossiers"] = r["dossiers"]["deleted"]
    except Exception as e:
        log.warning(f"[maintenance] catalyst/dossier prune failed: {e}")
        out["catalysts_dossiers_error"] = str(e)
    return out


# ── catalysts / dossiers: prune only what nothing else mentions (audit O9) ───────
# Ids are 12 lowercase hex chars (uuid4().hex[:12]). Instead of trusting a hand-kept
# list of "tables that point at a catalyst", the check reads EVERY text column of
# every table (bar tables excepted: ticker/number columns only) and collects every
# standalone 12-hex token. A candidate whose id shows up anywhere is kept. This errs
# toward keeping: a chance match keeps a row, it can never delete one.
_HEX12 = re.compile(r"(?<![0-9A-Za-z_])[0-9a-f]{12}(?![0-9A-Za-z_])")
_SCAN_SKIP_TABLES = {"price_bars_daily", "watch_bars", "option_bars"}
_PRUNE_TABLES = ("catalysts", "dossiers")


def _mentioned_ids(conn, candidates: set) -> dict:
    """{candidate id: "table.column" of the first place that mentions it}."""
    found: dict = {}
    if not candidates:
        return found
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table in _SCAN_SKIP_TABLES:
            continue
        try:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error:
            continue
        cols = []
        for c in info:
            name, ctype = c[1], (c[2] or "").upper()
            if table in _PRUNE_TABLES and name == "id":
                continue                              # a row's own key is not a reference
            if any(t in ctype for t in ("INT", "REAL", "FLOA", "DOUB", "NUM", "BOOL")):
                continue
            cols.append(name)
        if not cols:
            continue
        try:
            cur = conn.execute("SELECT " + ", ".join(f'"{c}"' for c in cols) + f' FROM "{table}"')
        except sqlite3.Error as e:
            # cannot read a table -> cannot prove anything is unreferenced
            raise RuntimeError(f"reference check could not read table {table}: {e}")
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                for i, v in enumerate(row):
                    if isinstance(v, bytes):
                        try:
                            v = v.decode("utf-8", "ignore")
                        except Exception:
                            continue
                    if not isinstance(v, str) or len(v) < 12:
                        continue
                    for tok in _HEX12.findall(v):
                        if tok in candidates and tok not in found:
                            found[tok] = f"{table}.{cols[i]}"
    return found


def _keep_score() -> float:
    try:
        import mason_theses
        return float(mason_theses.retention_keep_threshold())
    except Exception:
        return 0.6


def prune_unreferenced(conn, *, dry_run: bool = True, catalysts: bool = True,
                       dossiers: bool = True, catalyst_days: float = 30,
                       dossier_days: float = 30, now: float | None = None,
                       keep_score: float | None = None) -> dict:
    """Remove expired catalysts and old dossiers that nothing else refers to.

    A catalyst is a candidate only if ALL hold: status 'expired'; it expired more than
    `catalyst_days` ago (last news + 72 h); no director run consumed it; it has no
    dossier; it was never alerted; its score is below the memory keep threshold (news
    linked to a higher-scoring story is kept forever and needs the story row).
    A dossier is a candidate only if it was built more than `dossier_days` ago and is
    not the newest dossier of its subject.
    Every candidate is then dropped from the list if its id is mentioned in ANY other
    text column of the database (_mentioned_ids). `dry_run=True` deletes nothing.
    Returns per table: candidates, kept_referenced, deleted (or would_delete), bytes,
    kept_examples."""
    now = now if now is not None else time.time()
    keep_score = _keep_score() if keep_score is None else float(keep_score)
    cat_cut = now - float(catalyst_days) * 86400 - _CATALYST_EXPIRY_LAG_S
    dos_cut = now - float(dossier_days) * 86400
    cand = {"catalysts": {}, "dossiers": {}}
    if catalysts:
        for r in conn.execute(
                "SELECT id, length(CAST(COALESCE(story_key,'') AS BLOB)) + length(CAST(COALESCE(tickers,'') AS BLOB))"
                " + length(CAST(COALESCE(title,'') AS BLOB)) + length(CAST(COALESCE(tape_json,'') AS BLOB))"
                " + length(CAST(COALESCE(extra_json,'') AS BLOB)) + 120 FROM catalysts "
                "WHERE status='expired' AND last_seen IS NOT NULL AND last_seen < ? "
                "AND (consumed_by IS NULL OR consumed_by='') "
                "AND (dossier_ids IS NULL OR dossier_ids IN ('', '[]', 'null')) "
                "AND alerted_at IS NULL AND COALESCE(score, 0) < ?", (cat_cut, keep_score)):
            cand["catalysts"][r[0]] = int(r[1] or 0)
    if dossiers:
        for r in conn.execute(
                "SELECT d.id, length(CAST(COALESCE(d.payload_json,'') AS BLOB))"
                " + length(CAST(COALESCE(d.errors_json,'') AS BLOB)) + 100 FROM dossiers d "
                "WHERE d.built_at IS NOT NULL AND d.built_at < ? AND d.built_at < "
                "(SELECT MAX(x.built_at) FROM dossiers x WHERE x.subject = d.subject AND x.kind = d.kind)",
                (dos_cut,)):
            cand["dossiers"][r[0]] = int(r[1] or 0)
    all_ids = set(cand["catalysts"]) | set(cand["dossiers"])
    mentioned = _mentioned_ids(conn, all_ids)
    out = {"dry_run": bool(dry_run)}
    for table in _PRUNE_TABLES:
        ids = [i for i in cand[table] if i not in mentioned]
        kept = [i for i in cand[table] if i in mentioned]
        where: dict = {}
        for i in kept:
            where[mentioned[i]] = where.get(mentioned[i], 0) + 1
        info = {"candidates": len(cand[table]), "kept_referenced": len(kept),
                "kept_by": dict(sorted(where.items(), key=lambda kv: -kv[1])[:8]),
                "bytes": sum(cand[table][i] for i in ids),
                "would_delete" if dry_run else "deleted": len(ids)}
        if not dry_run and ids:
            n = 0
            guard = ("status='expired' AND (consumed_by IS NULL OR consumed_by='') AND alerted_at IS NULL"
                     if table == "catalysts" else "built_at < %f" % dos_cut)
            with conn:
                for k in range(0, len(ids), 400):
                    chunk = ids[k:k + 400]
                    n += conn.execute(
                        f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(chunk))}) AND {guard}",
                        chunk).rowcount
            info["deleted"] = n
        if dry_run:
            info.setdefault("deleted", 0)
        out[table] = info
    return out


# ── nightly safety copy (audit O1) — the work lives in mason_backup.py ──────────

def _backup_tz(cfg: dict):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(cfg.get("timezone") or "America/New_York"))
    except Exception:
        return None


def _market_hours(local: datetime) -> bool:
    """Regular US session, with a few minutes either side (Mon-Fri 09:25-16:05 ET)."""
    if local.weekday() >= 5:
        return False
    hm = local.hour * 60 + local.minute
    return 9 * 60 + 25 <= hm <= 16 * 60 + 5


def backup_due(st: dict, now: float, cfg: dict | None = None) -> bool:
    """True when the nightly backup should run on this tick:
      * inside tonight's window (default 02:30-06:30 ET) and no good backup yet since
        the window opened; or
      * no good backup for 36 h (first install, or the box was off overnight) and the
        market is closed.
    A failed attempt is retried at most every 3 hours."""
    if cfg is None:
        import mason_backup
        cfg = mason_backup.config()
    if not cfg.get("enabled", True):
        return False
    last_ok = float(st.get("last_backup_ok", 0) or 0)
    last_try = float(st.get("last_backup_try", 0) or 0)
    if now - last_try < BACKUP_RETRY_GAP:
        return False
    local = datetime.fromtimestamp(now, _backup_tz(cfg))
    try:
        hh, mm = (int(x) for x in str(cfg.get("time", "02:30")).split(":")[:2])
    except Exception:
        hh, mm = 2, 30
    start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)                    # still inside last night's window?
    end = start + timedelta(hours=float(cfg.get("window_hours", 4) or 4))
    if start <= local < end and last_ok < start.timestamp():
        return True
    return (now - last_ok) > BACKUP_STALE_AFTER and not _market_hours(local)


def run_backup(now: float | None = None) -> dict:
    """Run the nightly safety copy now (live database, configured folder). Never raises."""
    try:
        import mason_backup
        return mason_backup.run(db_path=mason_db.DB_FILE, now=now)
    except Exception as e:
        log.warning(f"[maintenance] backup failed to start: {e}")
        return {"ok": False, "error": str(e), "parts": []}


def run_vacuum() -> bool:
    """Reclaim freed pages. VACUUM can't run inside a transaction, so switch the
    connection to autocommit first. Best-effort."""
    try:
        conn = mason_db.get_connection()
        try:
            conn.isolation_level = None   # autocommit — required for VACUUM
            conn.execute("VACUUM")
            return True
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[maintenance] vacuum failed: {e}")
        return False


def tick(*, force: bool = False, now: float | None = None) -> dict:
    """Run whatever is due: prune if a day has passed since the last one, vacuum
    if a week has. `force` runs both regardless. Persists the run times so a
    restart doesn't repeat today's work. Returns what actually ran."""
    now = now if now is not None else time.time()
    st = _load_state()
    result: dict = {}
    # The safety copy goes FIRST, so tonight's prune/vacuum never runs without one.
    # `force` (the manual prune+vacuum) does not force a backup; use --backup-now.
    try:
        if backup_due(st, now):
            st["last_backup_try"] = now
            _save_state(st)                           # a crash mid-backup must not loop
            b = run_backup()
            if b.get("ok"):
                st["last_backup_ok"] = now
            result["backup"] = {"ok": bool(b.get("ok")), "error": b.get("error"),
                                "files": [p.get("path") for p in b.get("parts", []) if p.get("ok")]}
    except Exception as e:
        log.warning(f"[maintenance] backup step failed: {e}")
    if force or (now - float(st.get("last_prune", 0))) >= PRUNE_INTERVAL:
        result["prune"] = run_prune()
        st["last_prune"] = now
    if force or (now - float(st.get("last_vacuum", 0))) >= VACUUM_INTERVAL:
        result["vacuum"] = run_vacuum()
        st["last_vacuum"] = now
    if result:
        _save_state(st)
    return result


def _learning_step(now: float | None = None) -> dict:
    """Step 5 (audit L5 / L9): the nightly grader of what Mason looked at but did not trade
    (mason_learn.tick) and the weekly scoreboard (mason_scoreboard.tick). Runs LAST in the
    hourly pass, so the safety copy above always goes first. Each has its own switch
    (learning.grader.enabled / learning.scoreboard.enabled), works only inside its nightly
    window (from 03:15 ET) and holds itself to a time budget. Nothing here can raise into
    the maintenance thread."""
    out: dict = {}
    for name in ("mason_learn", "mason_scoreboard"):
        try:
            r = __import__(name).tick(now)
            if r:
                out.update(r)
        except Exception as e:
            log.warning(f"[maintenance] learning step ({name}) failed: {e}")
    if out:
        log.info(f"[maintenance] learning step ran {out}")
    return out


_started = False
_start_lock = threading.Lock()


def _beat(busy_with: str | None = None) -> None:
    """Heartbeat for the runtime supervisor (mason_runtime). Never raises."""
    try:
        import mason_runtime
        if busy_with:
            mason_runtime.busy("mason-maintenance", busy_with)
        else:
            mason_runtime.beat("mason-maintenance")
    except Exception:
        pass


def _maintenance_loop(check_interval: int = 3600, initial_delay: float = 120) -> None:
    """The retention daemon's loop (module level so the runtime supervisor can restart it). Never raises."""
    _beat()
    time.sleep(initial_delay)   # let boot settle before the first (possibly large) prune
    while True:
        _beat("backup / prune / vacuum")
        try:
            r = tick()
            if r:
                log.info(f"[maintenance] ran {r}")
        except Exception as e:
            log.warning(f"[maintenance] tick failed: {e}")
        _beat("calendar feeds")
        try:                           # events calendar: each feed has its own switch and interval
            import mason_calendar
            cal = mason_calendar.tick()
            if cal:
                log.info(f"[maintenance] calendar feeds ran {cal}")
        except Exception as e:
            log.warning(f"[maintenance] calendar step failed: {e}")
        _beat("nightly grader / scoreboard")
        _learning_step()
        _beat()
        time.sleep(check_interval)


def start(check_interval: int = 3600) -> None:
    """Launch the retention daemon (idempotent). Wakes every `check_interval`
    seconds and runs anything due. A short initial delay keeps it out of the boot
    critical path."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    threading.Thread(target=_maintenance_loop, args=(check_interval, 120),
                     name="mason-maintenance", daemon=True).start()
    log.info("[maintenance] retention loop started "
             f"(prune≈{PRUNE_INTERVAL//3600}h, vacuum≈{VACUUM_INTERVAL//86400}d, "
             "database backup nightly)")


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Mason maintenance. With no option: force a full prune + vacuum of the "
                    "live database now (the old behaviour).")
    ap.add_argument("--backup-now", action="store_true",
                    help="make a safety copy now (health-checked, compressed, old copies thinned)")
    ap.add_argument("--db", help="database file to work on (default: the live mason.db). When "
                                 "given, nothing is written to the live database.")
    ap.add_argument("--out", help="backup folder (default: backup.dir from the config)")
    ap.add_argument("--no-offsite", action="store_true", help="skip backup.offsite_cmd")
    ap.add_argument("--with-search-db", action="store_true",
                    help="also copy mason_search.db from the same folder as --db")
    ap.add_argument("--prune-refs", action="store_true",
                    help="expired catalysts / old dossiers nothing refers to: report what "
                         "would be removed (needs --db; add --apply to really delete)")
    ap.add_argument("--apply", action="store_true", help="with --prune-refs: delete for real")
    ap.add_argument("--catalyst-days", type=float, default=None)
    ap.add_argument("--dossier-days", type=float, default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if a.backup_now:
        import mason_backup
        r = mason_backup.run(db_path=a.db or mason_db.DB_FILE, out_dir=a.out,
                             record=not a.db, offsite=not a.no_offsite,
                             search_db=True if a.with_search_db else None,
                             allow_inside_repo=bool(a.out))
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("ok") else 1
    if a.prune_refs:
        if not a.db:
            ap.error("--prune-refs needs --db PATH (use a copy to try it out)")
        cfg = _maint_cfg()
        uri = f"file:{a.db}" + ("" if a.apply else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            r = prune_unreferenced(
                conn, dry_run=not a.apply,
                catalyst_days=a.catalyst_days if a.catalyst_days is not None
                else float(cfg.get("catalyst_expired_days", 30)),
                dossier_days=a.dossier_days if a.dossier_days is not None
                else float(cfg.get("dossier_days", 30)))
        finally:
            conn.close()
        print(json.dumps(r, indent=2))
        return 0
    if a.db or a.out:
        ap.error("--db/--out only apply to --backup-now or --prune-refs")
    # Manual: python3 mason_maintenance.py  → force a full prune+vacuum now
    print("forcing maintenance:", tick(force=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
