"""
mason_activity.py — Mason's append-only Activity Ledger.

Part of the 24/7 Analyst Desk. Owns the `activity` table in mason.db (defined in
mason_db._SCHEMA). This is the WORK LEDGER: the durable record of everything the
models actually DO, as distinct from mason_notifications.py, which is the ATTENTION
INBOX. The line between them (Nico's rule):

  * Activity = a discrete unit of analytical or operational work a model performed,
    wherever it was triggered (the autonomous loop OR a chat request). qwen scanned;
    qwen began an analysis; qwen finished; qwen flagged something; the router
    escalated to Opus; a chat started. It is a ledger you SCROLL — append-only,
    immutable, never read/unread, never deleted.
  * Notification = the small curated subset that DEMANDS Nico's attention. Those
    live in mason_notifications.py with read/unread + soft-delete + dedup.

The two are independent writes that MAY link: when a finished analysis produces a
`completed_analysis` notification, the analysis_finished activity row carries a
forward link (ref_table='notifications', ref_id=<notif id>) to it. The link is
optional — 'qwen began analysis' has nothing to point at yet.

Two categories share the one table (Nico's call: one log, filtered views):
  * 'analyst'  — model cognition (scan / analysis_started / analysis_finished / flag)
  * 'plumbing' — gateway internals (route / retry / escalate / local_down / truncated),
                 folded in from mason_llm.record_activity so a flag caused by a
                 plumbing event can sit in the same timeline.

`trigger` records what set the work off:
  * 'autonomous' — the background desk loop
  * 'chat'       — a request Nico made in a conversation (still real work → still
                   activity; the trigger just lets the UI mark it "you asked for this")

Design (mirrors mason_notifications.py):
  * Every write goes through mason_db.get_connection() — a FRESH connection per
    call, closed in a finally. Flask is multithreaded; connections are never shared.
  * APPEND-ONLY. There is deliberately no read/unread and no soft-delete. The table
    has no such columns. If you want to hide something from Nico, don't write it.

Public API:
    log_activity(type, title, *, body=None, actor="system", category="analyst",
                 ticker=None, trigger="autonomous", ref_table=None, ref_id=None,
                 extra=None) -> dict
    record_plumbing(event: dict) -> dict | None   # adapter for mason_llm gateway events
    list_activity(*, category=None, types=None, trigger=None, ticker=None,
                  since=None, limit=100) -> list[dict]
    get(activity_id) -> dict | None
    count(*, category=None, since=None) -> int
"""

from __future__ import annotations

import contextlib
import contextvars
import json as _json
import re as _re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta

import mason_db

# Smallfix 2026-11-18 (CHATSIGHT_REVIEW.md "Open": an activity row kept a recoverable account mask). A Robinhood
# account mask (the two-bullet prefix mason_robinhood.MASK_PREFIX + the last digits of an account number) belongs on
# Nico's own screens, never in this ledger: the one row that had one (an RH job's error list, JSON-escaped in the
# body) could be read back by a model through the chat look-up with replace()/hex(). log_activity drops every mask,
# literal or JSON-escaped, with the digits after it (and the brackets around it when nothing else is inside), from
# the title, the body and every text in `extra`. Text without a mask is stored byte for byte. Rows already stored are
# not rewritten; tools/count_activity_masks.py counts them.
_B = chr(0x2022)                       # the bullet; an account mask starts with two (mason_robinhood.MASK_PREFIX)
_MASK_ANY_RE = _re.compile(_B + r"|\\u2022", _re.I)
_MASK_RE = _re.compile(r"\s*\(\s*(?:" + _B * 2 + r"|\\u2022\\u2022)\s*\d*\s*\)|\s*(?:" + _B * 2 +
                       r"|\\u2022\\u2022)\s*\d*", _re.I)


def _no_masks(v):
    """`v` with every account mask removed: strings directly, dict keys and values and list items recursively.
    Anything else is returned as it is. Never raises (on a failure every bullet and every run of 4+ digits after
    one goes)."""
    try:
        if isinstance(v, str):
            return _MASK_RE.sub("", v) if _MASK_ANY_RE.search(v) else v
        if isinstance(v, dict):
            return {_no_masks(k): _no_masks(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return type(v)(_no_masks(x) for x in v)
        return v
    except Exception:
        return _re.sub(r"(?:" + _B + r"|\\u2022)+\s*\d*", "", str(v)) if isinstance(v, str) else v

# ── Activity v2 (2026-09-28, Nico) ───────────────────────────────────────────────
# "Anything done anywhere — a recommendation, a write-up, an order, an API call —
# should be there", organised so the page doesn't drown:
#   tier 'decision' — recommendations, write-ups (summary + link), orders and any
#                     Alpaca write, journal writes, reminders, theses, follow-ups,
#                     lessons, alerts sent. Kept FOREVER.
#   tier 'work'     — analyses, debates, scans, chat tool calls, model calls (with
#                     cost). Kept 90 days.
#   tier 'system'   — hourly per-source summaries of routine outside reads, gateway
#                     plumbing, errors. Kept 14 days.
# Multi-step work (a chat message, a desk job, a director run) is a RUN: every row
# written inside `with run(...)` carries its run_id, and one 'run' header row is
# appended when it ends. Dashboard polling (Flask's own endpoints) is never logged.
# Config `activity.*` (read per call): enabled, api_summaries, chat_tool_calls.

TIERS = ("decision", "work", "system")
_DECISION_TYPES = {
    "opus_director", "opus_director_v2", "desk_report", "macro_digest", "review",
    "recommendation", "recommendation_created", "writeup", "allocator", "thesis_created",
    "thesis_matched", "thesis_closed", "followup_set", "lesson_recorded", "alert_sent",
    "order", "api_write", "journal_write", "reminder_set", "reminder_done", "reminder_deleted",
    "memory_fact", "trade_intent", "trade_order", "director_graded", "recs_graded",
    "opus_director_toggle", "config_change",
}
_SYSTEM_TYPES = {"api_summary", "api_error", "health", "route", "retry", "escalate",
                 "local_down", "truncated", "opus_director_seam", "opus_director_budget_skip"}


def tier_for(type_: str, category: str = "analyst") -> str:
    t = str(type_ or "")
    if t in _DECISION_TYPES or t.startswith("autotrade_"):
        return "decision"
    if t in _SYSTEM_TYPES or category == "plumbing":
        return "system"
    return "work"


_TIER_RANK = {"system": 0, "work": 1, "decision": 2}
_current_run: contextvars.ContextVar = contextvars.ContextVar("mason_activity_run", default=None)


def _cfg() -> dict:
    try:
        from pathlib import Path
        c = _json.loads((Path(__file__).parent / "mason.config.json").read_text(encoding="utf-8"))
        return c.get("activity") or {}
    except Exception:
        return {}


def enabled(key: str = "enabled", default: bool = True) -> bool:
    return bool(_cfg().get(key, default))

_VALID_CATEGORY = {"analyst", "plumbing"}
_VALID_TRIGGER = {"autonomous", "chat"}

# Gateway event types (from mason_llm.record_activity) are all 'plumbing'. This maps
# a raw event dict's `type` to a human title; unknown types fall back to the type
# string itself so nothing is ever dropped.
_PLUMBING_TITLES = {
    "route": "Router picked a provider",
    "escalate": "Escalated to a stronger model",
    "local_down": "Local model unavailable",
    "local_dead": "Local model gave up",
    "local_retry": "Retrying local model",
    "forced_v1_truncated": "Response truncated",
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _load_extra(raw) -> dict:
    if not raw:
        return {}
    try:
        d = _json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _row_to_dict(r) -> dict:
    """One activity row → the plain dict the API/UI consume. extra_json is parsed
    into `extra`."""
    d = dict(r)
    d["extra"] = _load_extra(d.pop("extra_json", None))
    return d


# ── writes ─────────────────────────────────────────────────────────────────────

def log_activity(type: str, title: str, *, body: str = None, actor: str = "system",
                 category: str = "analyst", ticker: str = None,
                 trigger: str = "autonomous", ref_table: str = None,
                 ref_id: str = None, extra: dict = None, tier: str = None,
                 run_id: str = None, status: str = None) -> dict:
    """Append one activity row and return it (built in memory — no read-back, one
    lean INSERT). Append-only. `ref_table`/`ref_id` link forward to the record the
    work produced (notification, recommendation, journal row…). Inside a `run(...)`
    block the row inherits that run's id and trigger unless given explicitly.
    Never raises (a ledger hiccup must not break the work it records)."""
    if category not in _VALID_CATEGORY:
        category = "analyst"
    cur = _current_run.get()
    if cur is not None:
        run_id = run_id or cur["id"]
        if trigger not in _VALID_TRIGGER:
            trigger = cur["trigger"]
        elif trigger == "autonomous" and cur["trigger"] == "chat":
            trigger = "chat"
    if trigger not in _VALID_TRIGGER:
        trigger = "autonomous"
    tier = tier if tier in TIERS else tier_for(type, category)
    aid = uuid.uuid4().hex[:12]
    # smallfix 2026-11-18: no account mask is ever stored (see _no_masks); text without one is unchanged
    title, body, extra = _no_masks(str(title)), _no_masks(body), _no_masks(extra)
    row = {"id": aid, "created_date": _now_iso(), "actor": actor, "category": category,
           "type": type, "title": str(title)[:500], "body": body, "ticker": ticker,
           "trigger": trigger, "ref_table": ref_table, "ref_id": ref_id,
           "extra": extra or {}, "tier": tier, "run_id": run_id, "status": status}
    if cur is not None:
        cur["steps"] += 1
        if _TIER_RANK[tier] > _TIER_RANK[cur["tier"]]:
            cur["tier"] = tier
        if status == "error":
            cur["errors"] += 1
        if ticker and ticker not in cur["tickers"]:
            cur["tickers"].append(ticker)
    if not enabled():
        return row
    try:
        # _no_masks again on the JSON text: an object in `extra` turned into text by default=str may carry one too
        extra_json = _no_masks(_json.dumps(extra, default=str)) if extra else None
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO activity (id, created_date, actor, category, type, title, body, "
                    "ticker, trigger, ref_table, ref_id, extra_json, tier, run_id, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, row["created_date"], actor, category, type, row["title"], body, ticker,
                     trigger, ref_table, ref_id, extra_json, tier, run_id, status))
        finally:
            conn.close()
    except Exception:
        pass
    return row


@contextlib.contextmanager
def run(title: str, *, actor: str = "mason", trigger: str = "autonomous", kind: str = "run",
        ticker: str = None, extra: dict = None):
    """Group everything logged inside this block into one expandable run. The
    header row (type 'run') is appended when the block ends, carrying the highest
    tier of its steps, step/error counts and duration. Nested runs just join the
    outer one. Yields a dict whose 'summary' / 'ref_table' / 'ref_id' / 'title'
    the caller may set before exit."""
    outer = _current_run.get()
    if outer is not None:
        yield outer
        return
    r = {"id": uuid.uuid4().hex[:12], "trigger": trigger if trigger in _VALID_TRIGGER else "autonomous",
         "tier": "work", "steps": 0, "errors": 0, "tickers": [ticker] if ticker else [],
         "summary": None, "ref_table": None, "ref_id": None, "title": title, "t0": _time.time()}
    tok = _current_run.set(r)
    failed = None
    try:
        yield r
    except Exception as e:
        failed = e
        raise
    finally:
        _current_run.reset(tok)
        if r["steps"] or r.get("summary") or failed is not None:
            st = "error" if (failed is not None or r["errors"]) else "ok"
            log_activity("run", r.get("title") or title, body=r.get("summary"), actor=actor,
                         trigger=r["trigger"], ticker=(r["tickers"][0] if r["tickers"] else None),
                         ref_table=r.get("ref_table"), ref_id=r.get("ref_id"),
                         tier=r["tier"], run_id=r["id"], status=st,
                         extra={**(extra or {}), "kind": kind, "steps": r["steps"],
                                "errors": r["errors"], "tickers": r["tickers"],
                                "seconds": round(_time.time() - r["t0"], 2),
                                **({"exception": str(failed)[:300]} if failed is not None else {})})


def begin_run(title: str, *, actor: str = "mason", trigger: str = "autonomous", kind: str = "run",
              ticker: str = None, extra: dict = None) -> dict | None:
    """Explicit-lifetime run for code that can't use a `with` block (a streamed chat
    reply). Returns a handle for end_run(); None if a run is already active (the
    caller then simply joins it)."""
    if _current_run.get() is not None:
        return None
    r = {"id": uuid.uuid4().hex[:12], "trigger": trigger if trigger in _VALID_TRIGGER else "autonomous",
         "tier": "work", "steps": 0, "errors": 0, "tickers": [ticker] if ticker else [],
         "summary": None, "ref_table": None, "ref_id": None, "title": title, "t0": _time.time(),
         "actor": actor, "kind": kind, "extra": extra or {}}
    r["_token"] = _current_run.set(r)
    return r


def end_run(r: dict | None, *, summary: str = None, title: str = None, status: str = None,
            ref_table: str = None, ref_id: str = None, always: bool = False):
    """Close a begin_run handle and append its header row (skipped when nothing
    was logged inside it, unless always=True). Never raises."""
    if not r:
        return
    try:
        _current_run.reset(r.pop("_token"))
    except Exception:
        try:
            _current_run.set(None)
        except Exception:
            pass
    if not (r["steps"] or always):
        return
    st = status or ("error" if r["errors"] else "ok")
    log_activity("run", title or r["title"], body=summary, actor=r.get("actor", "mason"),
                 trigger=r["trigger"], ticker=(r["tickers"][0] if r["tickers"] else None),
                 ref_table=ref_table, ref_id=ref_id, tier=r["tier"], run_id=r["id"], status=st,
                 extra={**(r.get("extra") or {}), "kind": r.get("kind", "run"), "steps": r["steps"],
                        "errors": r["errors"], "tickers": r["tickers"],
                        "seconds": round(_time.time() - r["t0"], 2)})


def current_run_id():
    cur = _current_run.get()
    return cur["id"] if cur else None


# ── outside-API accounting: consequential calls get a row; routine reads are
# counted in memory and flushed as ONE summary row per source per hour ─────────
_api_lock = threading.Lock()
_api_counts: dict = {}          # (hour, source) -> {n, err, ms, last_err, methods}
_api_hour = None


def _flush_api(hour_key: str):
    rows = []
    with _api_lock:
        for (h, src), c in list(_api_counts.items()):
            if h == hour_key:
                rows.append((src, c))
                del _api_counts[(h, src)]
    for src, c in rows:
        avg = (c["ms"] / c["n"]) if c["n"] else 0
        title = (f"{src}: {c['n']:,} call{'s' if c['n'] != 1 else ''}"
                 + (f" ({c['err']} error{'s' if c['err'] != 1 else ''})" if c["err"] else "")
                 + f" · {hour_key[11:13]}:00–{hour_key[11:13]}:59")
        log_activity("api_summary", title, actor="system", category="plumbing", tier="system",
                     status="error" if c["err"] else "ok",
                     extra={"source": src, "hour": hour_key, "calls": c["n"], "errors": c["err"],
                            "avg_ms": round(avg, 1), "last_error": c.get("last_err"),
                            "methods": c.get("methods")})


def flush_api_summaries(force: bool = False):
    """Write summary rows for every completed hour (or all, when force=True)."""
    now_h = datetime.now().strftime("%Y-%m-%dT%H")
    with _api_lock:
        hours = sorted({h for (h, _s) in _api_counts})
    for h in hours:
        if force or h < now_h:
            _flush_api(h)


def record_api_call(source: str, *, method: str = "GET", ok: bool = True, ms: float = 0.0,
                    consequential: bool = False, title: str = None, detail: dict = None,
                    ticker: str = None, error: str = None):
    """One outside call. consequential → its own decision-tier row now (orders,
    cancels, anything that changes state or costs money). Routine → counted and
    folded into the hourly summary. Never raises."""
    global _api_hour
    try:
        if consequential:
            log_activity("api_write", title or f"{source}: {method}", actor="mason", category="analyst",
                         tier="decision", ticker=ticker, status="ok" if ok else "error",
                         extra={"source": source, "method": method, "ms": round(ms, 1),
                                **({"error": error} if error else {}), **(detail or {})})
            return
        if not enabled("api_summaries"):
            return
        h = datetime.now().strftime("%Y-%m-%dT%H")
        with _api_lock:
            c = _api_counts.setdefault((h, source), {"n": 0, "err": 0, "ms": 0.0, "last_err": None,
                                                     "methods": {}})
            c["n"] += 1
            c["ms"] += float(ms or 0)
            c["methods"][method] = c["methods"].get(method, 0) + 1
            if not ok:
                c["err"] += 1
                c["last_err"] = (error or "")[:200]
            roll = _api_hour is not None and _api_hour != h
            _api_hour = h
        if roll:
            flush_api_summaries()
    except Exception:
        pass


def record_plumbing(event) -> dict | None:
    """Adapter for mason_llm.record_activity's free-form gateway event dicts. Folds
    one into the ledger as a category='plumbing' row. Never raises (the gateway must
    never break because logging failed); returns the row, or None if it couldn't
    write. The full event dict is preserved in `extra`."""
    try:
        if isinstance(event, dict):
            etype = str(event.get("type") or "plumbing")
            title = _PLUMBING_TITLES.get(etype, etype)
            # enrich the title with the most useful field when present
            for k in ("provider", "stage", "intent", "model", "mode"):
                v = event.get(k)
                if v:
                    title = f"{title} — {v}"
                    break
            actor = event.get("provider") or event.get("model") or "router"
            return log_activity(etype, title, actor=str(actor),
                                category="plumbing", trigger="autonomous",
                                extra=event)
        return log_activity(str(event), str(event), category="plumbing")
    except Exception:
        return None


# ── reads ──────────────────────────────────────────────────────────────────────

def get(activity_id: str) -> dict | None:
    """Fetch one activity row by id."""
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM activity WHERE id = ?", (activity_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_activity(*, category: str = None, types=None, trigger: str = None,
                  ticker: str = None, since: str = None,
                  limit: int = 100) -> list[dict]:
    """Newest-first slice of the ledger. Filters (all optional, AND-combined):
      category — 'analyst' (default UI view) or 'plumbing'
      types    — str or iterable of type strings
      trigger  — 'autonomous' | 'chat'
      ticker   — exact ticker match
      since    — ISO timestamp; only rows created strictly after it
    """
    where: list[str] = []
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if trigger:
        where.append("trigger = ?")
        params.append(trigger)
    if ticker:
        where.append("ticker = ?")
        params.append(ticker)
    if since:
        where.append("created_date > ?")
        params.append(since)
    if types:
        if isinstance(types, str):
            types = [t.strip() for t in types.split(",") if t.strip()]
        types = list(types)
        if types:
            where.append("type IN (%s)" % ",".join("?" for _ in types))
            params.extend(types)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        lim = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        lim = 100
    params.append(lim)
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM activity {clause} ORDER BY created_date DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def count(*, category: str = None, since: str = None) -> int:
    """Count ledger rows, optionally by category and/or since an ISO timestamp."""
    where: list[str] = []
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if since:
        where.append("created_date > ?")
        params.append(since)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM activity {clause}", params
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ── global outbound-HTTP accounting (installed once at service start) ─────────
_SOURCE_BY_HOST = (
    ("paper-api.alpaca.markets", "Alpaca trading"), ("api.alpaca.markets", "Alpaca trading"),
    ("data.alpaca.markets", "Alpaca market data"), ("stream.data.alpaca", "Alpaca news stream"),
    ("finance.yahoo.com", "Yahoo Finance"), ("yahoo.com", "Yahoo Finance"),
    ("sec.gov", "SEC EDGAR"), ("api.anthropic.com", "Anthropic"),
    ("localhost:11434", "Ollama (qwen)"), ("127.0.0.1:11434", "Ollama (qwen)"),
    ("api.nasdaq.com", "NASDAQ"), ("clinicaltrials.gov", "ClinicalTrials.gov"),
    ("api.fda.gov", "openFDA"), ("fda.gov", "FDA"), ("stlouisfed.org", "FRED"),
    ("api.eia.gov", "EIA"), ("gdeltproject.org", "GDELT"), ("t.me", "Telegram"),
    ("polymarket.com", "Polymarket"), ("kalshi.com", "Kalshi"),
    ("marketingtools.apple.com", "App Store charts"), ("itunes.apple.com", "App Store charts"),
    ("faireconomy.media", "Econ calendar"), ("financialmodelingprep.com", "FMP"),
    ("alphavantage.co", "Alpha Vantage"), ("wikipedia.org", "Wikipedia"),
)
_SELF_HOSTS = ("localhost:5050", "127.0.0.1:5050", "0.0.0.0:5050")
_hooks_installed = False


def _source_for(url: str) -> str | None:
    try:
        from urllib.parse import urlparse
        u = urlparse(str(url))
        host = (u.netloc or "").lower()
    except Exception:
        return "other"
    if any(host.startswith(h) for h in _SELF_HOSTS):
        return None                       # the dashboard talking to Mason: never logged
    for frag, name in _SOURCE_BY_HOST:
        if frag in host:
            return name
    return "News & feeds"


def _alpaca_write_title(method: str, url: str, body, resp_json) -> tuple[str, str | None]:
    """Human title for a consequential Alpaca trading call."""
    path = str(url).split("/v2", 1)[-1] if "/v2" in str(url) else str(url)
    b = body if isinstance(body, dict) else {}
    r = resp_json if isinstance(resp_json, dict) else {}
    sym = b.get("symbol") or r.get("symbol") or (path.rsplit("/", 1)[-1] if "/positions/" in path else None)
    if method == "POST" and path.startswith("/orders"):
        px = b.get("limit_price") or b.get("stop_price")
        t = (f"Alpaca order: {str(b.get('side', '')).upper()} {b.get('qty') or b.get('notional')} {sym} "
             f"{b.get('type', '')}{(' @ ' + str(px)) if px else ''} ({b.get('time_in_force', '')})"
             f" → {r.get('status') or 'sent'}{(' · order ' + str(r.get('id'))[:8]) if r.get('id') else ''}")
    elif method == "DELETE" and path.startswith("/orders"):
        t = f"Alpaca: cancel order {path.rsplit('/', 1)[-1][:8] if path.count('/') > 1 else '(all)'}"
    elif method == "DELETE" and path.startswith("/positions"):
        t = f"Alpaca: close position {sym or '(all)'}"
    else:
        t = f"Alpaca: {method} {path[:60]}"
    return t, (str(sym).upper() if sym and "/" not in str(sym) and len(str(sym)) <= 6 else None)


def _observe(method, url, kwargs, resp, err, ms):
    src = _source_for(url)
    if src is None:
        return
    m = str(method or "GET").upper()
    status = getattr(resp, "status_code", None)
    ok = err is None and (status is None or status < 400)
    if src == "Alpaca trading" and m in ("POST", "DELETE", "PATCH", "PUT"):
        body = kwargs.get("json")
        try:
            rj = resp.json() if resp is not None else None
        except Exception:
            rj = None
        title, tk = _alpaca_write_title(m, url, body, rj)
        record_api_call(src, method=m, ok=ok, ms=ms, consequential=True, title=title, ticker=tk,
                        error=(str(err) if err else (None if ok else f"HTTP {status}: {str(rj)[:200]}")),
                        detail={"status": status, "path": str(url).split("/v2", 1)[-1][:120],
                                "request": body if isinstance(body, dict) else None,
                                "response": {k: rj.get(k) for k in ("id", "status", "symbol", "qty",
                                                                     "filled_qty", "limit_price",
                                                                     "client_order_id")}
                                if isinstance(rj, dict) else None})
        return
    record_api_call(src, method=m, ok=ok, ms=ms,
                    error=(str(err)[:200] if err else (None if ok else f"HTTP {status}")))


def install_http_hooks() -> bool:
    """Wrap requests.Session.request and curl_cffi's Session.request (yfinance) so
    EVERY outside call is accounted for, wherever it happens. Idempotent; never raises."""
    global _hooks_installed
    if _hooks_installed:
        return True
    try:
        import requests

        _orig = requests.Session.request

        def _wrapped(self, method, url, *a, **kw):
            t0 = _time.time()
            resp, err = None, None
            try:
                resp = _orig(self, method, url, *a, **kw)
                return resp
            except Exception as e:
                err = e
                raise
            finally:
                try:
                    _observe(method, url, kw, resp, err, (_time.time() - t0) * 1000)
                except Exception:
                    pass
        requests.Session.request = _wrapped
    except Exception:
        pass
    try:
        from curl_cffi import requests as _cr
        _corig = _cr.Session.request

        def _cwrapped(self, method, url, *a, **kw):
            t0 = _time.time()
            resp, err = None, None
            try:
                resp = _corig(self, method, url, *a, **kw)
                return resp
            except Exception as e:
                err = e
                raise
            finally:
                try:
                    _observe(method, url, kw, resp, err, (_time.time() - t0) * 1000)
                except Exception:
                    pass
        _cr.Session.request = _cwrapped
    except Exception:
        pass
    _hooks_installed = True
    return True


# ── write hooks: every state-changing function logs itself (installed at start) ──
# (module, function, activity type, label, tier) — the wrapper logs AFTER a
# successful call, with a short arg summary and a forward link to what it created.
_WRITE_HOOKS = [
    ("mason_trades", "log_trade", "journal_write", "Stock journal: logged trade", "decision"),
    ("mason_trades", "close_trade", "journal_write", "Stock journal: closed trade", "decision"),
    ("mason_trades", "delete_trade", "journal_write", "Stock journal: deleted trade", "decision"),
    ("mason_options", "log_play", "journal_write", "Options journal: logged play", "decision"),
    ("mason_options", "log_verified_play", "journal_write", "Options journal: logged verified play", "decision"),
    ("mason_options", "log_verified_closed_play", "journal_write", "Options journal: logged closed play", "decision"),
    ("mason_options", "update_play", "journal_write", "Options journal: updated play", "decision"),
    ("mason_options", "set_hold_directive", "journal_write", "Options journal: hold directive", "decision"),
    ("mason_options", "close_play_early", "journal_write", "Options journal: closed play early", "decision"),
    ("mason_options", "delete_play", "journal_write", "Options journal: deleted play", "decision"),
    ("mason_options", "record_expiry_review", "writeup", "Options expiry review", "decision"),
    ("mason_options", "record_periodic_review", "writeup", "Options periodic review", "decision"),
    ("mason_personal", "add_position_direct", "journal_write", "Personal account: added position", "decision"),
    ("mason_personal", "add_cash_direct", "journal_write", "Personal account: added cash", "decision"),
    ("mason_personal", "close_position_direct", "journal_write", "Personal account: closed position", "decision"),
    ("mason_personal", "edit_position", "journal_write", "Personal account: edited position", "decision"),
    ("mason_personal", "delete_position", "journal_write", "Personal account: deleted position", "decision"),
    ("mason_personal", "stage_position", "journal_write", "Personal account: staged position", "decision"),
    ("mason_personal", "stage_close", "journal_write", "Personal account: staged close", "decision"),
    ("mason_personal", "confirm_position", "journal_write", "Personal account: confirmed", "decision"),
    ("mason_memory_store", "add_fact", "memory_fact", "Memory: saved fact", "decision"),
    ("mason_memory_store", "update_fact", "memory_fact", "Memory: updated fact", "decision"),
    ("mason_memory_store", "forget_fact", "memory_fact", "Memory: forgot fact", "decision"),
    ("mason_agenda", "add_reminder", "reminder_set", "Reminder set", "decision"),
    ("mason_agenda", "complete_reminder", "reminder_done", "Reminder completed", "decision"),
    ("mason_agenda", "update_reminder", "reminder_set", "Reminder updated", "decision"),
    ("mason_agenda", "delete_reminder", "reminder_deleted", "Reminder deleted", "decision"),
    ("mason_desk_pipeline", "_insert_rec", "recommendation_created", "Recommendation (qwen desk)", "decision"),
    ("mason_desk_pipeline", "_insert_director_rec", "recommendation_created", "Director call", "decision"),
    ("mason_director", "add_followups", "followup_set", "Director follow-ups scheduled", "decision"),
    ("mason_director", "record_lessons", "lesson_recorded", "Director lessons recorded", "decision"),
    ("mason_theses", "create_thesis", "thesis_created", "Thesis opened", "decision"),
    ("mason_theses", "close_thesis", "thesis_closed", "Thesis closed", "decision"),
    ("mason_allocator", "allocate", "allocator", "Allocator answer (Opus)", "decision"),
    ("mason_notifications", "add_notification", "alert_sent", "Inbox", None),   # tier by notif type
]
_NOTIF_TIER = {"huge_event": "decision", "thesis_match": "decision", "system": "decision",
               "fill_divergence": "decision",     # A9c-f: a journal/broker divergence is audit trail, kept forever
               "major_move": "work", "completed_analysis": "work", "source_status": "system",
               "recommendation": None}          # None = skip (recommendation hooks cover it)
_ARG_KEYS = ("ticker", "symbol", "text", "title", "trade_id", "play_id", "position_id", "fid", "rid",
             "account_label", "fire_on", "exit_price", "outcome", "shares", "direction", "question")


def _arg_summary(fn, args, kwargs) -> tuple[str, str | None, dict]:
    import inspect
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
    except Exception:
        bound = dict(kwargs)
    flat = {}
    for k, v in bound.items():
        if isinstance(v, dict):                      # e.g. log_trade(trade_data={...})
            for kk in _ARG_KEYS:
                if kk in v and v[kk] not in (None, ""):
                    flat[kk] = v[kk]
        elif k in _ARG_KEYS and v not in (None, ""):
            flat[k] = v
    tk = flat.get("ticker") or flat.get("symbol")
    parts = [f"{k}={str(v)[:60]}" for k, v in flat.items() if k not in ("ticker", "symbol")][:4]
    return (", ".join(parts), (str(tk).upper() if tk else None), flat)


def _result_ref(res) -> tuple[str | None, str | None]:
    if isinstance(res, dict):
        rid = res.get("id") or res.get("rec_id") or res.get("alloc_id")
        return (None, str(rid)) if rid else (None, None)
    if isinstance(res, str) and 6 <= len(res) <= 40 and " " not in res:
        return None, res
    return None, None


def _make_hook(modname, fname, fn, typ, label, tier):
    import functools

    @functools.wraps(fn)
    def _hooked(*args, **kwargs):
        res = fn(*args, **kwargs)
        try:
            if modname == "mason_notifications":
                ntype = kwargs.get("type") or (args[0] if args else None)
                t = _NOTIF_TIER.get(ntype, "work")
                if t is None:
                    return res
                title = kwargs.get("title") or (args[1] if len(args) > 1 else "")
                log_activity("alert_sent", f"Inbox ({ntype}): {str(title)[:160]}", actor="mason", tier=t,
                             ticker=kwargs.get("ticker"), ref_table="notifications",
                             ref_id=(res or {}).get("id") if isinstance(res, dict) else None,
                             extra={"notification_type": ntype, "severity": kwargs.get("severity")})
                return res
            summ, tk, flat = _arg_summary(fn, args, kwargs)
            if isinstance(res, (list, tuple)) and not res and fname in ("add_followups", "record_lessons"):
                return res
            if fname in ("add_followups", "record_lessons") and isinstance(res, list):
                summ = f"{len(res)} item(s): " + "; ".join(
                    str(x.get("ticker") or "") + " " + str(x.get("due_date") or x.get("lesson") or x.get("action") or "")[:50]
                    for x in res[:3] if isinstance(x, dict))
            actor = "nico" if str(kwargs.get("created_by") or kwargs.get("deleted_by") or "") == "nico" else "mason"
            _rt, rid = _result_ref(res)
            ok = not (isinstance(res, dict) and res.get("ok") is False)
            log_activity(typ, f"{label}{': ' + summ if summ else ''}"[:300], actor=actor, tier=tier,
                         ticker=tk, ref_id=rid, status="ok" if ok else "error",
                         extra={"func": f"{modname}.{fname}", "args": {k: str(v)[:200] for k, v in flat.items()},
                                **({"error": str(res.get("error"))[:200]} if isinstance(res, dict) and res.get("error") else {})})
        except Exception:
            pass
        return res
    _hooked._mason_activity_hooked = True
    return _hooked


def install_write_hooks() -> list:
    """Wrap every function in _WRITE_HOOKS (module attribute replaced, so both
    external `mod.fn()` callers and in-module calls go through it). Idempotent."""
    import importlib
    done = []
    for modname, fname, typ, label, tier in _WRITE_HOOKS:
        try:
            mod = importlib.import_module(modname)
            fn = getattr(mod, fname, None)
            if fn is None or getattr(fn, "_mason_activity_hooked", False):
                continue
            setattr(mod, fname, _make_hook(modname, fname, fn, typ, label, tier))
            done.append(f"{modname}.{fname}")
        except Exception:
            continue
    return done


def install_all() -> dict:
    """Service start: outbound-HTTP accounting + write hooks."""
    return {"http": install_http_hooks(), "writes": install_write_hooks()}


_RETENTION_DAYS = {"work": 90, "system": 14}     # decision tier: kept forever


def backfill_tiers() -> int:
    """One-time/idempotent: give pre-v2 rows a tier from their type."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute("SELECT id, type, category FROM activity WHERE tier IS NULL").fetchall()
        if not rows:
            return 0
        with conn:
            conn.executemany("UPDATE activity SET tier=? WHERE id=?",
                             [(tier_for(r["type"], r["category"]), r["id"]) for r in rows])
        return len(rows)
    finally:
        conn.close()


def prune_old(days: int = None, *, work_days: int = None, system_days: int = None) -> int:
    """Tiered retention: decision rows are kept FOREVER; work rows > work_days (90);
    system rows > system_days (14). `days` (legacy callers) is ignored for the
    decision tier. Returns rows deleted."""
    wd = int(work_days or _RETENTION_DAYS["work"])
    sd = int(system_days or _RETENTION_DAYS["system"])
    now = datetime.now()
    try:
        backfill_tiers()
    except Exception:
        pass
    conn = mason_db.get_connection()
    try:
        with conn:
            n = conn.execute("DELETE FROM activity WHERE tier='work' AND created_date < ?",
                             ((now - timedelta(days=wd)).isoformat(),)).rowcount
            n += conn.execute("DELETE FROM activity WHERE tier='system' AND created_date < ?",
                              ((now - timedelta(days=sd)).isoformat(),)).rowcount
        return n
    finally:
        conn.close()


def feed(*, tier: str = "decision", ticker: str = None, actor: str = None, trigger: str = None,
         since: str = None, before: str = None, q: str = None, limit: int = 60) -> dict:
    """The Activities page feed, GROUPED: each item is either a run (header + its
    steps, newest first) or a standalone row. `tier` = decision | work | system | all.
    A run is shown in a tier if its header's tier matches (headers carry the
    highest tier of their steps), so a chat turn that placed an order is a Decision.
    Paging: pass the last item's created_date as `before`."""
    try:
        backfill_tiers()
    except Exception:
        pass
    lim = max(1, min(int(limit or 60), 300))
    where, params = ["(run_id IS NULL OR type='run')"], []
    if tier in TIERS:
        where.append("tier = ?")
        params.append(tier)
    for col, val in (("ticker", ticker), ("actor", actor), ("trigger", trigger)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if since:
        where.append("created_date > ?")
        params.append(since)
    if before:
        where.append("created_date < ?")
        params.append(before)
    if q:
        where.append("(title LIKE ? OR body LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    conn = mason_db.get_connection()
    try:
        heads = [_row_to_dict(r) for r in conn.execute(
            f"SELECT * FROM activity WHERE {' AND '.join(where)} ORDER BY created_date DESC LIMIT ?",
            params + [lim]).fetchall()]
        run_ids = [h["run_id"] for h in heads if h.get("type") == "run" and h.get("run_id")]
        steps = {}
        if run_ids:
            qm = ",".join("?" * len(run_ids))
            for r in conn.execute(f"SELECT * FROM activity WHERE run_id IN ({qm}) AND type != 'run' "
                                  "ORDER BY created_date", run_ids).fetchall():
                d = _row_to_dict(r)
                steps.setdefault(d["run_id"], []).append(d)
        counts = {t: 0 for t in TIERS}
        day_ago = (datetime.now() - timedelta(days=1)).isoformat()
        for r in conn.execute("SELECT tier, COUNT(*) n FROM activity WHERE created_date > ? "
                              "AND (run_id IS NULL OR type='run') GROUP BY tier", (day_ago,)).fetchall():
            if r["tier"] in counts:
                counts[r["tier"]] = r["n"]
    finally:
        conn.close()
    items = []
    for h in heads:
        if h.get("type") == "run":
            items.append({"kind": "run", "run": h, "steps": steps.get(h["run_id"], [])})
        else:
            items.append({"kind": "row", "row": h})
    return {"items": items, "counts_24h": counts,
            "next_before": heads[-1]["created_date"] if len(heads) == lim else None}


if __name__ == "__main__":
    # Manual smoke: python3 mason_activity.py
    a = log_activity("analysis_started", "qwen began analysis — NVDA",
                     actor="qwen", ticker="NVDA")
    print("logged:", a["id"], a["title"])
    b = record_plumbing({"type": "route", "intent": "vision", "provider": "opus"})
    print("plumbing:", b["id"] if b else None, b["title"] if b else None)
    print("analyst rows:", count(category="analyst"))
    for it in list_activity(limit=5):
        print(f"  [{it['category']}] {it['type']:<18} {it['title']}")
