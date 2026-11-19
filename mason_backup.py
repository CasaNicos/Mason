"""
mason_backup.py — nightly safety copy of Mason's SQLite database (audit O1).

What one run does, in order:
  1. Copies the live database with SQLite's online backup API (sqlite3.Connection.backup),
     so the copy is a consistent snapshot even while Mason keeps writing.
       * WAL mode (what mason.db uses): one pass. In WAL a reader never blocks a writer,
         so nobody waits, and a one-pass copy cannot be restarted by a concurrent write.
       * any other journal mode: small steps with a short sleep between them, so writers
         get the lock between steps. If concurrent writes keep restarting the copy it
         falls back to one pass.
  2. Switches the COPY to journal_mode=DELETE (one self-contained file, no -wal/-shm) and
     runs PRAGMA integrity_check on it (or quick_check, by config).
  3. Compresses it (zstd from the standard library when available, else gzip) and reads
     the compressed file back to prove it decompresses to the same bytes.
  4. Keeps the newest N daily + M weekly files and removes the rest. Only files this
     module named are ever removed, and nothing is removed after a failed run.
  5. Optionally hands the finished file to `backup.offsite_cmd` (no shell, timeout, its
     output is never logged).
  6. Writes one activity/log line; raises a plain-words notification only on failure.

Files go to a directory OUTSIDE the repository (default ~/.local/share/mason/db_backups,
mode 0700, files 0600).

Restore (Mason stopped):
    zstd -d  mason-YYYYMMDD-HHMMSS.db.zst -o mason.db      (or: gunzip -c X.db.gz > mason.db)
    rm -f mason.db-wal mason.db-shm
Mason switches the file back to WAL by itself on the next start.

Public API:
    config() -> dict
    backup_database(db_path, out_dir, *, prefix="mason", ...) -> dict     # raises BackupError
    apply_retention(out_dir, prefix, keep_daily, keep_weekly, protect=()) -> list[str]
    run_offsite(cmd, path, timeout_s) -> dict
    run(...) -> dict                                                      # never raises
    newest_backup(out_dir, prefix="mason") -> Path | None
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("mason.backup")

_HERE = Path(__file__).resolve().parent
_CONFIG_PATH = _HERE / "mason.config.json"

DEFAULTS = {
    "enabled": True,
    "time": "02:30",                 # local (America/New_York) start of the nightly window
    "window_hours": 4,               # a missed 02:30 can still run until 06:30
    "timezone": "America/New_York",
    "dir": "~/.local/share/mason/db_backups",
    "keep_daily": 7,
    "keep_weekly": 4,
    "compress": "auto",              # auto | zstd | gzip | none
    "integrity": "full",             # full (PRAGMA integrity_check) | quick (quick_check)
    "search_db": "never",            # never | weekly — mason_search.db is a rebuildable index
    "offsite_cmd": "",               # e.g. "rclone copy --quiet {path} remote:mason" or "rsync -a"
    "offsite_timeout_s": 900,
    "min_free_mb": 2048,             # refuse to run if the target disk would drop below this
    "step_pages": 2048,              # non-WAL only: pages per step (8 MB at 4 KiB pages)
    "step_sleep_s": 0.02,            # non-WAL only: pause between steps
    "copy_deadline_s": 900,
}

_NAME_RE_TMPL = r"^{prefix}-(\d{{8}})-(\d{{6}})\.db(?:\.zst|\.gz)?$"
_CHUNK = 1 << 20
_run_lock = threading.Lock()


class BackupError(Exception):
    """A backup step failed. The message is written for the owner, in plain words."""


# ── config ──────────────────────────────────────────────────────────────────────

def config() -> dict:
    """The `backup` block of mason.config.json over DEFAULTS. Read on every call, so a
    config edit applies to the next run without a restart. Never raises."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")).get("backup") or {}
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    except Exception:
        pass
    return cfg


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── small helpers ───────────────────────────────────────────────────────────────

def _mb(n) -> str:
    return f"{(n or 0) / 1e6:.0f} MB"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if (path.stat().st_mode & 0o777) != 0o700 and path.stat().st_uid == os.getuid():
            os.chmod(path, 0o700)
    except OSError:
        pass


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(_HERE)
        return True
    except ValueError:
        return False


def _create_private(path: Path) -> None:
    """Create an empty 0600 file (fails if it exists), so no step ever writes database
    bytes into a file other users could read."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def _fsync_path(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _unlink_quiet(*paths) -> None:
    for p in paths:
        try:
            if p is not None:
                os.unlink(str(p))
        except OSError:
            pass


def _zstd():
    try:
        from compression import zstd       # Python 3.14+
        return zstd
    except Exception:
        return None


def _pick_method(wanted: str) -> str:
    wanted = (wanted or "auto").lower()
    if wanted == "none":
        return "none"
    if wanted == "gzip":
        return "gzip"
    return "zstd" if _zstd() is not None else "gzip"     # auto, or zstd without the module


def _open_compressed(path: Path, mode: str, method: str):
    if method == "zstd":
        z = _zstd()
        return z.open(str(path), mode, level=3) if "w" in mode else z.open(str(path), mode)
    if "w" in mode:
        return gzip.open(str(path), mode, compresslevel=4)
    return gzip.open(str(path), mode)


def _sha256_stream(fh) -> str:
    h = hashlib.sha256()
    while True:
        b = fh.read(_CHUNK)
        if not b:
            break
        h.update(b)
    return h.hexdigest()


# ── step 1+2: online copy and health check ──────────────────────────────────────

def _online_copy(src_path: Path, dst_path: Path, *, step_pages: int, step_sleep: float,
                 deadline_s: float) -> dict:
    """Copy src -> dst with the online backup API. Returns {"mode", "strategy", "restarts"}."""
    uri = f"file:{quote(str(src_path))}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        mode = str(src.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        t0 = time.time()

        def one_pass():
            dst = sqlite3.connect(str(dst_path), timeout=30)
            try:
                src.backup(dst, pages=-1)
            finally:
                dst.close()

        if mode == "wal":
            one_pass()
            return {"mode": mode, "strategy": "one pass (WAL: writers are never blocked)", "restarts": 0}

        state = {"last": None, "restarts": 0}

        class _Restarting(Exception):
            pass

        def progress(_status, remaining, _total):
            if state["last"] is not None and remaining > state["last"]:
                state["restarts"] += 1               # a concurrent write restarted the copy
                if state["restarts"] > 3:
                    raise _Restarting()
            state["last"] = remaining
            if time.time() - t0 > deadline_s:
                raise BackupError(f"the copy did not finish within {int(deadline_s)} seconds")
            if remaining and step_sleep > 0:
                time.sleep(step_sleep)                # the source lock is free between steps

        dst = sqlite3.connect(str(dst_path), timeout=30)
        try:
            src.backup(dst, pages=max(1, int(step_pages)), progress=progress, sleep=0.05)
            return {"mode": mode, "strategy": "stepped", "restarts": state["restarts"]}
        except _Restarting:
            pass
        finally:
            dst.close()
        # writes kept restarting the stepped copy: start clean and take it in one pass
        _unlink_quiet(dst_path)
        _create_private(dst_path)
        one_pass()
        return {"mode": mode, "strategy": "one pass (stepped copy kept restarting)",
                "restarts": state["restarts"]}
    finally:
        src.close()


def _check_copy(path: Path, integrity: str) -> dict:
    """Make the copy a single self-contained file and run the health check on it."""
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        pragma = "quick_check" if str(integrity).lower() == "quick" else "integrity_check"
        t0 = time.time()
        rows = [str(r[0]) for r in conn.execute(f"PRAGMA {pragma}").fetchall()]
        secs = time.time() - t0
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()
    if rows != ["ok"]:
        raise BackupError("the copy did not pass its health check "
                          f"({pragma}: {'; '.join(rows[:3])[:200]})")
    if not tables or not pages:
        raise BackupError("the copy is empty (no tables in it)")
    return {"check": pragma, "check_s": round(secs, 2), "tables": int(tables), "pages": int(pages)}


# ── step 3: compress and verify ─────────────────────────────────────────────────

def _compress_and_verify(raw: Path, out_tmp: Path, method: str) -> dict:
    t0 = time.time()
    h = hashlib.sha256()
    with open(raw, "rb") as fin, _open_compressed(out_tmp, "wb", method) as fout:
        while True:
            b = fin.read(_CHUNK)
            if not b:
                break
            h.update(b)
            fout.write(b)
    _fsync_path(out_tmp)
    t1 = time.time()
    with _open_compressed(out_tmp, "rb", method) as fin:
        back = _sha256_stream(fin)
    if back != h.hexdigest():
        raise BackupError("the compressed file does not decompress back to the same data")
    return {"sha256": h.hexdigest(), "compress_s": round(t1 - t0, 2),
            "verify_s": round(time.time() - t1, 2)}


def backup_database(db_path, out_dir, *, prefix: str = "mason", compress: str = "auto",
                    integrity: str = "full", step_pages: int = 2048, step_sleep: float = 0.02,
                    deadline_s: float = 900, min_free_mb: int = 2048, now: float | None = None,
                    allow_inside_repo: bool = False) -> dict:
    """Copy one database into out_dir as <prefix>-YYYYMMDD-HHMMSS.db[.zst|.gz].
    Returns a result dict; raises BackupError (plain-words message) on any failure, after
    removing its temporary files. Never writes to the source database."""
    db_path, out_dir = Path(db_path).expanduser(), Path(out_dir).expanduser()
    if not db_path.is_file():
        raise BackupError(f"the database file {db_path} does not exist")
    if _inside_repo(out_dir) and not allow_inside_repo:
        raise BackupError(f"the backup folder {out_dir} is inside Mason's own folder; "
                          "choose one outside it (backup.dir)")
    try:
        _ensure_private_dir(out_dir)
    except OSError as e:
        raise BackupError(f"the backup folder {out_dir} could not be created ({e.strerror or e})")
    db_bytes = db_path.stat().st_size
    free = shutil.disk_usage(out_dir).free
    need = int(db_bytes * 1.6) + int(min_free_mb) * 1024 * 1024
    if free < need:
        raise BackupError(f"not enough free disk space in {out_dir}: {free / 1e9:.1f} GB free, "
                          f"{need / 1e9:.1f} GB needed")

    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    method = _pick_method(compress)
    ext = {"zstd": ".db.zst", "gzip": ".db.gz", "none": ".db"}[method]
    final = out_dir / f"{prefix}-{stamp}{ext}"
    raw = out_dir / f".tmp-{prefix}-{stamp}-{os.getpid()}.db"
    packed = out_dir / f".tmp-{prefix}-{stamp}-{os.getpid()}{ext}.part"
    if final.exists():
        raise BackupError(f"a backup named {final.name} already exists")
    res = {"ok": True, "db": str(db_path), "db_bytes": db_bytes, "method": method}
    try:
        _create_private(raw)
        t0 = time.time()
        res.update(_online_copy(db_path, raw, step_pages=step_pages, step_sleep=step_sleep,
                                deadline_s=deadline_s))
        res["copy_s"] = round(time.time() - t0, 2)
        res.update(_check_copy(raw, integrity))
        _unlink_quiet(Path(str(raw) + "-journal"), Path(str(raw) + "-wal"), Path(str(raw) + "-shm"))
        res["copy_bytes"] = raw.stat().st_size
        if method == "none":
            with open(raw, "rb") as fh:
                res["sha256"] = _sha256_stream(fh)
            _fsync_path(raw)
            os.replace(raw, final)
        else:
            _create_private(packed)
            res.update(_compress_and_verify(raw, packed, method))
            os.replace(packed, final)
            _unlink_quiet(raw)
        os.chmod(final, 0o600)
        _fsync_path(out_dir)
        res["path"] = str(final)
        res["bytes"] = final.stat().st_size
        return res
    except BackupError:
        _unlink_quiet(raw, packed, Path(str(raw) + "-journal"))
        raise
    except Exception as e:
        _unlink_quiet(raw, packed, Path(str(raw) + "-journal"))
        raise BackupError(f"{type(e).__name__}: {e}") from e


# ── step 4: retention ───────────────────────────────────────────────────────────

def list_backups(out_dir, prefix: str = "mason") -> list:
    """[(datetime, Path)] newest first — only files this module names."""
    rx = re.compile(_NAME_RE_TMPL.format(prefix=re.escape(prefix)))
    out = []
    try:
        for p in Path(out_dir).expanduser().iterdir():
            m = rx.match(p.name)
            if m and p.is_file() and not p.is_symlink():
                try:
                    out.append((datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), p))
                except ValueError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def newest_backup(out_dir, prefix: str = "mason"):
    b = list_backups(out_dir, prefix)
    return b[0][1] if b else None


def plan_retention(files: list, keep_daily: int, keep_weekly: int) -> tuple:
    """files = [(datetime, Path)] newest first. Keep the newest file of each of the
    `keep_daily` most recent days, plus the newest file of each of the `keep_weekly`
    most recent ISO weeks that lie before those days. Returns (keep, drop)."""
    keep, days, weeks = [], [], []
    oldest_daily = None
    for ts, p in files:
        d = ts.date()
        if d in days:
            continue                                   # an older file of a day already kept
        if len(days) < max(1, int(keep_daily)):
            days.append(d)
            keep.append(p)
            oldest_daily = d
    for ts, p in files:
        d = ts.date()
        if oldest_daily is None or d >= oldest_daily:
            continue
        wk = d.isocalendar()[:2]
        if wk in weeks:
            continue
        if len(weeks) < max(0, int(keep_weekly)):
            weeks.append(wk)
            keep.append(p)
    kept = set(keep)
    return keep, [p for _ts, p in files if p not in kept]


def apply_retention(out_dir, prefix: str, keep_daily: int, keep_weekly: int, protect=()) -> list:
    """Delete everything plan_retention drops (never a path in `protect`). Also clears
    this module's own temp files older than a day. Returns the deleted file names."""
    out_dir = Path(out_dir).expanduser()
    _keep, drop = plan_retention(list_backups(out_dir, prefix), keep_daily, keep_weekly)
    guard = {str(Path(p)) for p in protect}
    deleted = []
    for p in drop:
        if str(p) in guard:
            continue
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError:
            pass
    try:
        for p in out_dir.glob(f".tmp-{prefix}-*"):
            if p.is_file() and time.time() - p.stat().st_mtime > 86400:
                _unlink_quiet(p)
    except OSError:
        pass
    return deleted


# ── step 5: optional off-site hand-off ──────────────────────────────────────────

def run_offsite(cmd: str, path, timeout_s: int = 900) -> dict:
    """Run the owner's off-site command with the backup path. No shell; a timeout; the
    command's output is discarded and never logged. `{path}` in the command is replaced
    by the backup path; without it the path is appended as the last argument."""
    try:
        argv = shlex.split(cmd or "")
    except ValueError:
        return {"ok": False, "why": "the off-site command could not be read (unbalanced quotes)"}
    if not argv:
        return {"ok": True, "skipped": True}
    path = str(path)
    if any("{path}" in a for a in argv):
        argv = [a.replace("{path}", path) for a in argv]
    else:
        argv.append(path)
    argv[0] = os.path.expanduser(argv[0])
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "USER", "LOGNAME") if k in os.environ}
    t0 = time.time()
    try:
        cp = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=max(1, int(timeout_s)), env=env)
        ok, why = cp.returncode == 0, f"it stopped with exit code {cp.returncode}"
    except subprocess.TimeoutExpired:
        ok, why = False, f"it did not finish within {int(timeout_s)} seconds"
    except OSError as e:
        ok, why = False, f"it could not be started ({e.strerror or type(e).__name__})"
    out = {"ok": ok, "seconds": round(time.time() - t0, 1)}
    if not ok:
        out["why"] = why
    return out


# ── the whole job ───────────────────────────────────────────────────────────────

def _notify_failure(title: str, body: str, dedup: str) -> None:
    try:
        import mason_notifications as N
        N.add_notification("system", title, body=body, severity="warn",
                           created_by="maintenance", dedup_key=dedup)
    except Exception as e:
        log.warning(f"[backup] could not raise the failure notification: {e}")


def _activity(title: str, extra: dict, status: str) -> None:
    try:
        import mason_activity as A
        A.log_activity("db_backup", title, actor="system", category="plumbing",
                       tier="system", extra=extra, status=status)
    except Exception:
        pass


def _summary(parts: list) -> str:
    bits = []
    for p in parts:
        if p.get("ok"):
            bits.append(f"{p['label']}: {_mb(p['db_bytes'])} copied to {_mb(p['bytes'])}, "
                        f"health check passed")
        else:
            bits.append(f"{p['label']}: FAILED")
    return " · ".join(bits)


def run(db_path=None, out_dir=None, *, cfg: dict | None = None, record: bool = True,
        offsite: bool = True, search_db: bool | None = None, now: float | None = None,
        allow_inside_repo: bool = False) -> dict:
    """One full backup run. Never raises. `record=False` (used when a database path is
    given by hand, e.g. a test on a copy) writes no activity line and no notification,
    so nothing touches the live database.
    Returns {"ok", "parts": [...], "deleted": [...], "offsite": {...}, "error"?}."""
    cfg = dict(cfg or config())
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a backup is already running", "parts": []}
    try:
        out = Path(out_dir or cfg["dir"]).expanduser()
        main_db = Path(db_path) if db_path else (_HERE / "mason.db")
        jobs = [("mason", "main database", main_db)]
        if search_db is None:
            search_db = (str(cfg.get("search_db", "never")).lower() == "weekly"
                         and datetime.fromtimestamp(now or time.time()).weekday() == 6)
        if search_db:
            jobs.append(("mason_search", "search index", main_db.parent / "mason_search.db"))
        result = {"ok": True, "parts": [], "deleted": [], "dir": str(out)}
        t0 = time.time()
        for prefix, label, path in jobs:
            try:
                r = backup_database(
                    path, out, prefix=prefix, compress=cfg.get("compress", "auto"),
                    integrity=cfg.get("integrity", "full"),
                    step_pages=_int(cfg.get("step_pages"), 2048),
                    step_sleep=float(cfg.get("step_sleep_s", 0.02) or 0),
                    deadline_s=float(cfg.get("copy_deadline_s", 900) or 900),
                    min_free_mb=_int(cfg.get("min_free_mb"), 2048), now=now,
                    allow_inside_repo=allow_inside_repo)
                r["label"] = label
                result["parts"].append(r)
                # prune only after a good new file exists, and never the new file
                result["deleted"] += apply_retention(
                    out, prefix, _int(cfg.get("keep_daily"), 7), _int(cfg.get("keep_weekly"), 4),
                    protect=[r["path"]])
            except BackupError as e:
                result["ok"] = False
                result["parts"].append({"ok": False, "label": label, "db": str(path), "error": str(e)})
                log.error(f"[backup] {label} FAILED: {e}")
        result["seconds"] = round(time.time() - t0, 1)

        main = result["parts"][0] if result["parts"] else {}
        if main.get("ok") and offsite and str(cfg.get("offsite_cmd") or "").strip():
            result["offsite"] = run_offsite(cfg["offsite_cmd"], main["path"],
                                            _int(cfg.get("offsite_timeout_s"), 900))
            if not result["offsite"].get("ok"):
                log.error(f"[backup] off-site copy failed: {result['offsite'].get('why')}")

        title = "Database backup: " + _summary(result["parts"])
        if result["deleted"]:
            title += f" · removed {len(result['deleted'])} old file(s)"
        if result.get("offsite") and not result["offsite"].get("skipped"):
            title += " · off-site copy " + ("done" if result["offsite"]["ok"] else "FAILED")
        log.info(f"[backup] {title} ({result['seconds']}s)")
        if record:
            extra = {"dir": str(out), "seconds": result["seconds"], "deleted": result["deleted"],
                     "offsite": result.get("offsite"),
                     "parts": [{k: p.get(k) for k in ("label", "ok", "db_bytes", "bytes", "copy_s",
                                                      "check", "check_s", "compress_s", "verify_s",
                                                      "method", "strategy", "restarts", "sha256",
                                                      "path", "error")} for p in result["parts"]]}
            _activity(title, extra, "ok" if result["ok"] else "error")
            if not result["ok"]:
                bad = [p for p in result["parts"] if not p.get("ok")]
                newest = newest_backup(out, "mason")
                have = (f"The newest good copy is {newest.name}." if newest
                        else "There is no good copy yet.")
                _notify_failure(
                    "Database backup failed",
                    "Mason could not make its nightly safety copy of the "
                    + " and the ".join(p["label"] for p in bad) + ". Reason: "
                    + "; ".join(p.get("error", "unknown") for p in bad) + ". " + have
                    + " The live database was not changed and trading is not affected. "
                      "Mason will try again in a few hours.",
                    "db_backup_failed")
            elif result.get("offsite") and not result["offsite"].get("ok"):
                _notify_failure(
                    "Off-site copy of the database backup failed",
                    "Last night's backup itself is fine and is stored on this machine, but the "
                    f"off-site copy command did not work: {result['offsite'].get('why')}.",
                    "db_backup_offsite_failed")
        if not result["ok"]:
            result["error"] = "; ".join(p.get("error", "") for p in result["parts"] if not p.get("ok"))
        return result
    except Exception as e:                      # belt and braces: the job must never raise
        log.error(f"[backup] unexpected failure: {type(e).__name__}: {e}")
        if record:
            _notify_failure("Database backup failed",
                            "Mason could not make its nightly safety copy of the database "
                            f"({type(e).__name__}). The live database was not changed.",
                            "db_backup_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "parts": []}
    finally:
        _run_lock.release()
