"""
mason_transcript.py — append-only conversation transcript (mason.db `transcript`).

One row per line exchanged with Nico: every message he sends and every line Mason
shows or speaks (chat reply, greeting, spoken callout, brief recap, direct answer,
upload analysis). This is the clean, human-readable record — mason_trace.jsonl is
the debug firehose (full prompts, tool payloads) and rotates; this does not.

Public API:
    log(speaker, channel, text, *, source=None, model=None, turn_id=None, meta=None)
        -> None. NEVER raises, never blocks the request: rows are queued and written
        by one daemon thread. turn_id defaults to the active mason_trace turn.
    recent(limit=100, since=None, channel=None) -> list[dict]   newest first
    flush(timeout=5.0) -> bool   wait until queued rows are written (tests / shutdown)
    strip_injected_context(text) -> str   what Nico actually typed

speaker: 'nico' | 'mason'.   channel: 'chat' | 'greeting' | 'callout' | 'brief' |
'direct_answer' | 'voice' | 'upload' | 'system'.
"""

import json
import logging
import queue
import re
import threading
from datetime import datetime

import mason_db

log_ = logging.getLogger("mason.transcript")

_Q = queue.Queue(maxsize=5000)
_worker = None
_worker_lock = threading.Lock()

# The runtime appends "[AUTOMATED CONTEXT — injected by Mason's runtime, not typed
# by Nico]" blocks to the user turn; the transcript logs only what Nico typed.
_INJECTED_RE = re.compile(r"\n*\[AUTOMATED CONTEXT[^\]]*\].*\Z", re.S)

# Created lazily as well as in mason_db._SCHEMA: the running service applies
# _SCHEMA once per process, so a table added after boot must self-create.
_DDL = """
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, speaker TEXT NOT NULL,
    channel TEXT NOT NULL, source TEXT, model TEXT, turn_id TEXT, text TEXT NOT NULL,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcript_ts ON transcript(ts DESC);
CREATE INDEX IF NOT EXISTS idx_transcript_channel ON transcript(channel, ts DESC);
"""
_ddl_done = set()


def strip_injected_context(text) -> str:
    """Drop runtime-injected context from a user message. Accepts a plain string or
    an Anthropic-style content list ([{type:'text',text:...}, ...])."""
    if isinstance(text, list):
        parts = []
        for b in text:
            t = b.get("text", "") if isinstance(b, dict) else str(b)
            if t.lstrip().startswith("[AUTOMATED CONTEXT"):
                continue
            parts.append(t)
        text = "\n".join(p for p in parts if p)
    text = str(text or "")
    return _INJECTED_RE.sub("", text).strip()


def _conn():
    conn = mason_db.get_connection()
    key = str(mason_db.DB_FILE)
    if key not in _ddl_done:
        conn.executescript(_DDL)
        conn.commit()
        _ddl_done.add(key)
    return conn


def _write(row: dict) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO transcript (ts, speaker, channel, source, model, turn_id, text, meta_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row["ts"], row["speaker"], row["channel"], row["source"], row["model"],
             row["turn_id"], row["text"], row["meta_json"]))
        conn.commit()
    finally:
        conn.close()


def _run():
    while True:
        row = _Q.get()
        try:
            if row is not None:
                _write(row)
        except Exception as e:
            log_.warning("transcript write failed: %s", e)
        finally:
            _Q.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="transcript-writer")
            _worker.start()


def log(speaker, channel, text, *, source=None, model=None, turn_id=None, meta=None) -> None:
    """Queue one transcript row. Never raises; empty text is skipped."""
    try:
        if speaker == "nico":
            text = strip_injected_context(text)
        text = str(text or "").strip()
        if not text:
            return
        if turn_id is None:
            try:
                import mason_trace
                turn_id = mason_trace.current_turn()
            except Exception:
                turn_id = None
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "speaker": str(speaker),
            "channel": str(channel),
            "source": source,
            "model": model,
            "turn_id": turn_id,
            "text": text,
            "meta_json": json.dumps(meta, default=str) if meta else None,
        }
        _ensure_worker()
        _Q.put_nowait(row)
    except Exception as e:
        try:
            log_.warning("transcript log dropped: %s", e)
        except Exception:
            pass


def flush(timeout: float = 5.0) -> bool:
    """Block until every queued row is written (or timeout). True if drained."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if _Q.unfinished_tasks == 0:
            return True
        time.sleep(0.02)
    return _Q.unfinished_tasks == 0


def recent(limit: int = 100, since: str = None, channel: str = None) -> list:
    """Newest-first rows. `since` is an ISO timestamp (rows strictly after it)."""
    try:
        limit = max(1, min(int(limit or 100), 5000))
    except (TypeError, ValueError):
        limit = 100
    sql, args = "SELECT * FROM transcript WHERE 1=1", []
    if since:
        sql += " AND ts > ?"
        args.append(str(since))
    if channel:
        sql += " AND channel = ?"
        args.append(str(channel))
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    conn = _conn()
    try:
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            mj = d.pop("meta_json", None)
            try:
                d["meta"] = json.loads(mj) if mj else None
            except Exception:
                d["meta"] = mj
            out.append(d)
        return out
    finally:
        conn.close()
