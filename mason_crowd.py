"""
mason_crowd.py — Crowd Tracker (RH Expansion S2-T2). Going forward only.

Spec: docs/robinhood/RH_EXPANSION_S2_SIGNALS_TOOLS.md §2 T2. Binding contracts:
docs/robinhood/RH_EXPANSION_CONTRACTS.md §2 (boundaries), §5 crowd_snapshots / crowd_events,
§6 GET /api/rh/crowd, §7 rh_expansion.crowd, §9 mandatory tests.

What it does, once a trading day at crowd.snapshot_time_et (default 16:30 ET):
  1. Resolve each configured list (crowd.lists, default "100 Most Popular") from
     get_popular_watchlists. Matching is by NAME, ignoring case (RH calls it "100 most popular"),
     and the id is remembered: next time the remembered id wins while RH still lists it, so a
     renamed list keeps its history.
  2. Read the items (get_watchlist_items, bounded pagination, bulk lane) and write crowd_snapshots,
     rank = list order. An empty, failed, malformed, truncated or suspiciously short read
     (< half of the previous snapshot) writes NOTHING, so a bad response can never look like a
     mass exit.
  3. Rebuild crowd_events for that list by replaying its snapshots: enter = on the list today and
     not a member yesterday; exit = absent for 2 snapshots in a row (debounce). A one-day flicker
     is neither an exit nor a re-enter. The first snapshot ever creates no events.
  4. Grade every event with mason_price_cache.fwd_return(sessions=5|20|60, vs="SPY") — the one
     grading function; no return math here. A snapshot taken after the 16:00 close can only be
     acted on next session, so entry_rule="after"; an intraday snapshot uses "on_or_after".
     Only matured horizons are stored.
  5. Write priors via mason_kb_priors.upsert_prior, family "crowd", key {"event","rank_bucket"},
     and test the hypothesis "entries underperform" from n + CI95. It is reported as supported /
     contradicted / inconclusive / insufficient_data — never assumed.

If a list can't be read without following (PROBE.md says the real list IS readable, so this is the
fallback path): with crowd.allow_follow=false (default) nothing is followed and the status is
"blocked_needs_follow"; with it true, follow_watchlist is called ONCE EVER for that list (a durable
marker in desk_state), through mason_robinhood._call (which enforces the name gate), and logged to
the activity ledger at the decision tier. allow_follow is read once at the start of a run.

Context only: no trading, no recommendations, no event candidates (contracts §2.1/§2.2).

UI overhaul Task 4 (2026-11-16) — for the redesigned #crowd page:
  - GET /api/rh/crowd also returns data.history ({dates, ranks}: every ticker's rank on each snapshot of the
    ?days window, null = not on the list that day) and data.labels ({TICKER: {name, sector, industry}}).
  - GET /api/rh/crowd/ticker?symbol=X returns that ticker's cached company profile (what it does, sector,
    industry, market cap, ...). DB-only, like every route here.
  - The profiles come from Robinhood's read-only get_equity_fundamentals, fetched by the job
    rhx_crowd_profiles (hourly; a no-op unless a ticker the page can show has no profile younger than
    FUND_TTL_DAYS) and cached in desk_state. Names come from Mason's local universe files (no network).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import mason_db
import mason_robinhood as RH

log = logging.getLogger("mason.crowd")

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
ET = ZoneInfo("America/New_York")
MODULE = "mason_crowd"
HORIZONS = (5, 20, 60)                 # trading sessions
HYPOTHESIS_WINDOW = "20s"
MAX_PAGES = 5                          # items pages per list (the real list is one page of 100)
MAX_GRADE_PER_RUN = 2000
GRADE_GIVE_UP_DAYS = 200               # calendar days; 60 sessions ≈ 90 days
PARTIAL_FRACTION = 0.5                 # a read under half the previous snapshot is not trusted
EXIT_ABSENT_SNAPSHOTS = 2
_BLOCKED_RE = re.compile(r"follow|forbidden|permission|not authori[sz]ed|\b403\b|\b404\b|not found", re.I)
# UI overhaul Task 4 (2026-11-16, the #crowd page redesign): rank history in the API + company profiles.
HISTORY_MAX_DATES = 60                 # snapshots in data.history (the ?days window, newest kept)
HISTORY_MAX_TICKERS = 400
FUND_PREFIX = "rhx_crowd_fund:"        # desk_state, one key per ticker: cached RH fundamentals (company profile)
LABELS_KEY = "rhx_crowd_labels"        # desk_state: {TICKER: {name, sector, industry}} shipped in data.labels
FUND_TTL_DAYS = 7                      # a cached profile is refetched after a week
FUND_KEEP_DAYS = 120                   # a profile nobody can see any more is dropped after this
FUND_CHUNK = 10                        # symbols per get_equity_fundamentals call (same as mason_pead)
FUND_MAX_CALLS = 15                    # per warm run (≤ 150 symbols)
PROFILE_EVERY_S = 3600                 # warm job cadence; a run with nothing to fetch is a no-op
_SYM_RE = re.compile(r"[A-Z0-9.\-]{1,12}")

_DEFAULTS = {"enabled": False, "allow_follow": False, "snapshot_time_et": "16:30",
             "lists": ["100 Most Popular"]}

_cfg_cache = {"mtime": None, "data": {}}
_state: dict = {}


def _reset_state() -> None:
    """Forget in-process run state (tests; a restart does the same)."""
    _state.clear()
    _state.update(last_run=None, state=None, errors=[], truncated=0, rate_hits=0, follow_calls=0)


_reset_state()


# ── config ────────────────────────────────────────────────────────────────────
def _config() -> dict:
    try:
        mt = Path(CONFIG).stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        c = ((_config().get("rh_expansion") or {}).get("crowd")) or {}
        for k, v in c.items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    if not isinstance(out.get("lists"), list):
        out["lists"] = list(_DEFAULTS["lists"])
    return out


def enabled() -> bool:
    """crowd.enabled AND the robinhood master switch (contracts §7)."""
    try:
        return _cfg().get("enabled") is True and RH.enabled()
    except Exception:
        return False


def _entry_rule(cfg: dict | None = None) -> str:
    t = str((cfg or _cfg()).get("snapshot_time_et") or "16:30")
    try:
        hh, mm = (int(x) for x in t.split(":")[:2])
    except Exception:
        hh, mm = 16, 30
    return "after" if (hh, mm) >= (16, 0) else "on_or_after"


# ── small helpers ─────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    """Clock hook (tests patch it): when a snapshot was actually observed."""
    return datetime.now(timezone.utc)


def _grade_now() -> datetime:
    """Clock hook (tests patch it): 'now' for the trading-calendar readiness check in grade()."""
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _plain(s):
    """Plain text for JSON: no markup characters from RH-sourced strings reach the API (contracts §6)."""
    return re.sub(r"[<>&]", "", str(s)) if s is not None else s


def _finite(v):
    """A finite float, or None (NaN/inf/non-numeric never reach the DB or the API)."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _clean(x):
    """Strict-JSON copy: non-finite floats become None."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _rule_observed(snap_date: str, observed_iso) -> str | None:
    """Entry rule from WHEN the snapshot was observed (stored per snapshot, so a later config change
    can't relabel old events): seen after the 16:00 ET close, or on a later day → "after"; seen
    during the snap_date session → "on_or_after". None if unknown."""
    try:
        obs = datetime.fromisoformat(str(observed_iso)).astimezone(ET)
        sd = datetime.strptime(snap_date, "%Y-%m-%d").date()
    except Exception:
        return None
    if obs.date() == sd and (obs.hour, obs.minute) < (16, 0):
        return "on_or_after"
    return "after"


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _err(where: str, e) -> str:
    msg = f"{where}: {type(e).__name__}: {e}" if isinstance(e, BaseException) else f"{where}: {e}"
    msg = _plain(msg)[:300]
    _state["errors"] = (_state.get("errors") or [])[-19:] + [msg]
    log.warning(f"[crowd] {msg}")
    return msg


def _event_id(list_id: str, ticker: str, kind: str, date: str) -> str:
    return hashlib.sha1(f"{list_id}|{ticker}|{kind}|{date}".encode()).hexdigest()[:16]


def rank_bucket(rank) -> str:
    try:
        r = int(rank)
    except Exception:
        return "51-100"
    return "top10" if r <= 10 else ("11-50" if r <= 50 else "51-100")


def _loads(s, default):
    try:
        v = json.loads(s) if isinstance(s, str) and s else None
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                 (key, str(value), _utc_iso()))


def _list_key(name: str) -> str:
    return "rhx_crowd_list:" + str(name).strip().lower()


def _follow_key(list_id: str) -> str:
    return "rhx_crowd_followed:" + str(list_id)


# ── Robinhood reads ───────────────────────────────────────────────────────────
def _popular_lists() -> list:
    """[{id, name}] from get_popular_watchlists. Raises on a malformed payload."""
    data = RH._call("get_popular_watchlists", {}, lane="bulk")
    rows = data if isinstance(data, list) else (
        data.get("lists") if isinstance(data, dict) and "lists" in data else
        next((v for v in (data or {}).values() if isinstance(v, list)), None) if isinstance(data, dict) else None)
    if not isinstance(rows, list):
        raise ValueError("get_popular_watchlists: no list of lists in the response")
    out = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("id"):
            nm = r.get("display_name") or r.get("name")
            out.append({"id": r["id"], "name": _plain(nm)[:120] if isinstance(nm, str) else ""})
    return out


def _resolve(conn, name: str, popular: list):
    """(list_id, display_name) for a configured name, or (None, None). The remembered id wins while
    RH still lists it (rename continuity); otherwise match the name, ignoring case and spacing."""
    by_id = {p["id"]: p["name"] for p in popular}
    try:
        known = _kv_get(conn, _list_key(name))
    except Exception:
        known = None
    if known and known in by_id:
        return known, by_id[known]
    want = " ".join(str(name).split()).lower()
    for p in popular:
        if " ".join(p["name"].split()).lower() == want:
            return p["id"], p["name"]
    return None, None


def _read_items(list_id: str) -> tuple:
    """(symbols in rank order, errors, truncated, blocked)."""
    rows, meta = RH.paged_all("get_watchlist_items", {"list_id": list_id}, rows_key="items",
                              max_pages=MAX_PAGES, lane="bulk")
    errors = list(meta.get("errors") or [])
    blocked = any(_BLOCKED_RE.search(e) for e in errors)
    syms, seen = [], set()
    for it in rows:
        s = it.get("symbol")
        if isinstance(s, str):
            s = s.strip().upper()
            if s and s not in seen and re.fullmatch(r"[A-Z0-9.\-]{1,12}", s):
                seen.add(s)
                syms.append(s)
    return syms, errors, bool(meta.get("truncated")), blocked


def _follow_once(conn, list_id: str, list_name: str) -> bool:
    """The one write this module may make (contracts §2.1). Once EVER per list id: the durable
    marker is written before the call, so a timeout can't cause a second follow. The name gate is
    enforced inside mason_robinhood._call."""
    if _kv_get(conn, _follow_key(list_id)):
        return False
    with conn:
        _kv_set(conn, _follow_key(list_id), _utc_iso())
    _state["follow_calls"] = int(_state.get("follow_calls") or 0) + 1
    ok, err = True, None
    try:
        RH._call("follow_watchlist", {"list_id": list_id}, lane="live")
    except (RH.RHForbidden, ValueError) as e:
        # Refused by the wrapper's gate (allow_follow/master off, name not allowed, its resolver
        # failed) or a bad lane: raised BEFORE anything was sent, so no follow reached RH. Release
        # the one-time marker so a later run (with the gate open) can still follow once.
        with conn:
            conn.execute("DELETE FROM desk_state WHERE key = ?", (_follow_key(list_id),))
        _state["follow_calls"] = max(0, int(_state.get("follow_calls") or 0) - 1)
        _err(f"follow_watchlist {list_name!r} refused before sending (will retry next run)", e)
        return False
    except Exception as e:
        # Timeout / transport error: the write may have landed — keep the marker (at most once).
        ok, err = False, _err(f"follow_watchlist {list_name!r}", e)
    try:
        with conn:
            _kv_set(conn, "rhx_crowd_follow_attempt", json.dumps(
                {"list": list_name, "attempted_at": _utc_iso(), "ok": ok, "error": err}))
    except Exception as e:
        _err("follow attempt state", e)
    try:
        import mason_activity
        mason_activity.log_activity(
            "api_write", f"Followed Robinhood list '{list_name}' so the crowd tracker can read it",
            body=None if ok else f"Follow failed: {err}", actor=MODULE, tier="decision",
            ref_table="crowd_snapshots", ref_id=list_id, status="ok" if ok else "error",
            extra={"tool": "follow_watchlist", "list_id": list_id, "list_name": list_name,
                   "config": "rh_expansion.crowd.allow_follow"})
    except Exception as e:
        _err("activity log", e)
    return ok


# ── snapshot + events ─────────────────────────────────────────────────────────
def _write_snapshot(conn, snap_date: str, list_id: str, list_name: str, syms: list) -> None:
    prev = conn.execute("SELECT MIN(created_date) c FROM crowd_snapshots WHERE snap_date = ? AND list_id = ?",
                        (snap_date, list_id)).fetchone()["c"]
    now = prev or _utc_iso()               # first observation time is kept: it fixes the entry rule
    with conn:
        conn.execute("DELETE FROM crowd_snapshots WHERE snap_date = ? AND list_id = ?", (snap_date, list_id))
        conn.executemany(
            "INSERT INTO crowd_snapshots (snap_date, list_id, list_name, ticker, rank, created_date) "
            "VALUES (?,?,?,?,?,?)",
            [(snap_date, list_id, list_name, s, i + 1, now) for i, s in enumerate(syms)])


def _replay(conn, list_id: str) -> dict:
    """{event_id: {ticker, kind, date, rank}} derived from every snapshot of the list, in date order."""
    by_date: dict = {}
    for r in conn.execute("SELECT snap_date, ticker, rank FROM crowd_snapshots WHERE list_id = ? "
                          "ORDER BY snap_date, rank", (list_id,)):
        by_date.setdefault(r["snap_date"], {})[r["ticker"]] = r["rank"]
    out, members = {}, None
    for d in sorted(by_date):
        present = by_date[d]
        if not present:
            continue
        if members is None:                       # first snapshot ever: baseline, no events
            members = {t: {"absent": 0, "rank": rk} for t, rk in present.items()}
            continue
        for t, rk in present.items():
            if t not in members:
                out[_event_id(list_id, t, "enter", d)] = {"ticker": t, "kind": "enter", "date": d, "rank": rk}
                members[t] = {"absent": 0, "rank": rk}
            else:
                members[t].update(absent=0, rank=rk)
        for t in [t for t in members if t not in present]:
            members[t]["absent"] += 1
            if members[t]["absent"] >= EXIT_ABSENT_SNAPSHOTS:
                out[_event_id(list_id, t, "exit", d)] = {"ticker": t, "kind": "exit", "date": d,
                                                         "rank": members[t]["rank"]}
                del members[t]
    return out


def _sync_events(conn, list_id: str) -> int:
    desired = _replay(conn, list_id)
    now = _utc_iso()
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO crowd_events (id, list_id, ticker, kind, event_date, px, fwd_json, "
            "created_date, created_by, record_deleted) VALUES (?,?,?,?,?,NULL,NULL,?,?,0)",
            [(eid, list_id, e["ticker"], e["kind"], e["date"], now, MODULE) for eid, e in desired.items()])
        existing = conn.execute("SELECT id, record_deleted FROM crowd_events WHERE list_id = ?",
                                (list_id,)).fetchall()
        for r in existing:            # a re-run of a date may change which events exist
            want = r["id"] in desired
            if want and r["record_deleted"]:
                conn.execute("UPDATE crowd_events SET record_deleted = 0 WHERE id = ?", (r["id"],))
            elif not want and not r["record_deleted"]:
                conn.execute("UPDATE crowd_events SET record_deleted = 1 WHERE id = ?", (r["id"],))
    return len(desired)


def _snapshot_list(conn, name: str, popular: list, snap_date: str, allow_follow: bool) -> dict:
    res = {"name": name, "id": None, "status": "error", "n": 0, "events": 0, "errors": []}
    list_id, list_name = _resolve(conn, name, popular)
    if not list_id:
        res["errors"].append(_err("resolve", f"list {name!r} not found in get_popular_watchlists"))
        return res
    res.update(id=list_id, list_name=list_name)
    syms, errors, truncated, blocked = _read_items(list_id)
    if blocked and allow_follow and _follow_once(conn, list_id, list_name):
        syms, errors, truncated, blocked = _read_items(list_id)
    if blocked:
        res["status"] = "blocked_needs_follow"
        res["errors"].append(_err("items", f"list {list_name!r} is not readable without following "
                                           f"(crowd.allow_follow={'true' if allow_follow else 'false'})"))
        return res
    if truncated:
        _state["truncated"] = int(_state.get("truncated") or 0) + 1
        res["errors"].append(_err("items", f"list {list_name!r}: pagination stopped at {MAX_PAGES} pages "
                                           f"— snapshot not written"))
        return res
    if errors:
        res["errors"] += [_err("items", e) for e in errors[:3]]
        return res
    if not syms:
        res["errors"].append(_err("items", f"list {list_name!r} came back empty — snapshot not written "
                                           f"(no mass exit)"))
        return res
    prev = conn.execute("SELECT COUNT(*) n FROM crowd_snapshots WHERE list_id = ? AND snap_date = "
                        "(SELECT MAX(snap_date) FROM crowd_snapshots WHERE list_id = ? AND snap_date < ?)",
                        (list_id, list_id, snap_date)).fetchone()["n"]
    if prev and len(syms) < PARTIAL_FRACTION * prev:
        res["errors"].append(_err("items", f"list {list_name!r} returned {len(syms)} items vs {prev} last "
                                           f"time — looks partial, snapshot not written"))
        return res
    _write_snapshot(conn, snap_date, list_id, list_name, syms)
    with conn:
        _kv_set(conn, _list_key(name), list_id)
    res.update(status="ok", n=len(syms), events=_sync_events(conn, list_id))
    return res


def snapshot(snap_date: str | None = None) -> dict:
    """Take today's snapshot of every configured list (idempotent per date). Never raises."""
    if not enabled():
        return {"enabled": False}
    cfg = _cfg()                                      # read ONCE: a mid-run flip changes nothing
    allow_follow = cfg.get("allow_follow") is True
    snap_date = snap_date or _today_et()
    out = {"enabled": True, "snap_date": snap_date, "status": "ok", "lists": [], "errors": []}
    rate0 = int(RH._state.get("rate_hits") or 0)
    conn = None
    try:
        conn = mason_db.get_connection()
        try:
            popular = _popular_lists()
        except Exception as e:
            out.update(status="error", errors=[_err("get_popular_watchlists", e)])
            return out
        for name in [n for n in cfg["lists"] if isinstance(n, str) and n.strip()][:10]:
            try:
                r = _snapshot_list(conn, name, popular, snap_date, allow_follow)
            except Exception as e:
                r = {"name": name, "status": "error", "errors": [_err(f"snapshot {name!r}", e)]}
            out["lists"].append(r)
            out["errors"] += r.get("errors") or []
        sts = [r["status"] for r in out["lists"]]
        out["status"] = ("blocked_needs_follow" if "blocked_needs_follow" in sts
                         else "ok" if sts and all(s == "ok" for s in sts) else "error")
        return out
    except Exception as e:
        out.update(status="error", errors=out["errors"] + [_err("snapshot", e)])
        return out
    finally:
        _state["rate_hits"] = int(_state.get("rate_hits") or 0) + max(0, int(RH._state.get("rate_hits") or 0) - rate0)
        _state["last_run"] = _utc_iso()
        _state["state"] = out["status"]
        _state["run_errors"] = list(out["errors"])
        try:
            if conn is not None:
                with conn:
                    _kv_set(conn, "rhx_crowd_last_run", json.dumps(
                        {"at": _state["last_run"], "status": out["status"], "errors": out["errors"][:10],
                         "lists": [{k: r.get(k) for k in ("name", "id", "list_name", "status", "n")}
                                   for r in out["lists"]]}))
        except Exception as e:
            _err("last_run state", e)
        if conn is not None:
            conn.close()


# ── grading + priors ──────────────────────────────────────────────────────────
def grade() -> dict:
    """Grade events via fwd_return (matured horizons only), then refresh priors + scorecard."""
    if not enabled():
        return {"enabled": False}
    out = {"enabled": True, "graded": 0, "errors": []}
    conn = None
    try:
        try:
            import mason_price_cache as PCm
        except Exception as e:
            out["errors"].append(_err("mason_price_cache unavailable", e))
            return out
        default_rule = _entry_rule()
        conn = mason_db.get_connection()
        now_et = _grade_now().astimezone(ET)
        # Only events that still miss a horizon, oldest first, and only horizons that could have
        # matured by the trading calendar (fwd_return.matured stays the authority). Give up on events
        # older than GRADE_GIVE_UP_DAYS (a 60-session horizon matures in ~90 days → older = never).
        since = (now_et.date() - timedelta(days=GRADE_GIVE_UP_DAYS)).isoformat()
        sessions_since = {}
        rows = conn.execute(
            "SELECT e.id, e.ticker, e.event_date, e.px, e.fwd_json, "
            "(SELECT MIN(s.created_date) FROM crowd_snapshots s WHERE s.list_id = e.list_id "
            " AND s.snap_date = e.event_date) AS observed "
            "FROM crowd_events e WHERE e.record_deleted = 0 AND e.event_date >= ? "
            "AND (e.fwd_json IS NULL OR e.fwd_json NOT LIKE '%\"60\"%') "
            "ORDER BY e.event_date ASC, e.id", (since,)).fetchall()
        done = 0
        for r in rows:
            rule = _rule_observed(r["event_date"], r["observed"]) or default_rule
            fj = _loads(r["fwd_json"], {})
            ed = r["event_date"]
            if ed not in sessions_since:
                sessions_since[ed] = _sessions_after(ed, now_et)
            need = 1 if rule == "after" else 0          # "after" enters one session later
            todo = [h for h in HORIZONS if str(h) not in fj and sessions_since[ed] >= h + need]
            if not todo:
                continue
            if done >= MAX_GRADE_PER_RUN:
                break
            done += 1
            px, changed = r["px"], False
            for h in todo:
                try:
                    g = PCm.fwd_return(r["ticker"], r["event_date"], sessions=h, entry_rule=rule, vs="SPY")
                except Exception as e:
                    out["errors"].append(_err(f"fwd_return {r['ticker']} {r['event_date']} {h}s", e))
                    break
                if not isinstance(g, dict) or g.get("matured") is not True or _finite(g.get("ret")) is None:
                    continue                       # NaN/inf/missing ret = not gradable yet
                fj[str(h)] = {"ret": _finite(g.get("ret")), "xs_spy": _finite(g.get("xs")), "matured": True,
                              "entry_date": g.get("entry_date"), "entry_px": _finite(g.get("entry_px")),
                              "exit_date": g.get("exit_date"), "exit_px": _finite(g.get("exit_px"))}
                if px is None and _finite(g.get("entry_px")) is not None:
                    px = _finite(g.get("entry_px"))
                changed = True
            if changed:
                with conn:
                    conn.execute("UPDATE crowd_events SET fwd_json = ?, px = ? WHERE id = ?",
                                 (json.dumps(fj, sort_keys=True, allow_nan=False), px, r["id"]))
                out["graded"] += 1
        sc = _write_priors(conn, out["errors"])
        if sc is not None:
            with conn:
                _kv_set(conn, "rhx_crowd_scorecard", json.dumps(_clean(sc), allow_nan=False))
        return out
    except Exception as e:
        out["errors"].append(_err("grade", e))
        return out
    finally:
        if conn is not None:
            conn.close()


def _sessions_after(event_date: str, now_et: datetime) -> int:
    """Completed trading sessions strictly after event_date up to now (today counts after 16:15 ET).
    Bounded by GRADE_GIVE_UP_DAYS + 1 calendar days."""
    try:
        d = datetime.strptime(event_date, "%Y-%m-%d").date()
    except Exception:
        return 0
    today = now_et.date()
    n, steps = 0, 0
    d += timedelta(days=1)
    while d <= today and steps <= GRADE_GIVE_UP_DAYS + 1:
        steps += 1
        if d < today or (now_et.hour, now_et.minute) >= (16, 15):
            if _is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=ET)):
                n += 1
        d += timedelta(days=1)
    return n


def _event_ranks(conn) -> dict:
    """{event_id: rank} — enter = rank on the day it entered; exit = last rank it held."""
    ranks = {}
    for (lid,) in conn.execute("SELECT DISTINCT list_id FROM crowd_events WHERE record_deleted = 0").fetchall():
        ranks.update({eid: e["rank"] for eid, e in _replay(conn, lid).items()})
    return ranks


def _stat(row) -> dict:
    st = (row or {}).get("stat_json") if isinstance(row, dict) else None
    if isinstance(st, str):
        st = _loads(st, {})
    return st if isinstance(st, dict) else {}


def _window_summary(row) -> dict:
    st = _stat(row)
    return {"n": int((row or {}).get("n") or 0), "mean_xs": st.get("mean"), "median_xs": st.get("median"),
            "ci95": st.get("ci95"), "hit_rate": st.get("hit_rate"), "shrunk_mean": st.get("shrunk_mean"),
            "min_n_met": bool((row or {}).get("min_n_met"))}


def _pct(x) -> str:
    return f"{x * 100:+.2f}%"


def _hypothesis(summary: dict | None) -> dict:
    """'Entries underperform SPY' — decided only by n + CI95 of the excess return."""
    s = summary or {"n": 0}
    h = {"claim": "Stocks entering the list underperform SPY afterwards", "window": HYPOTHESIS_WINDOW,
         "n": int(s.get("n") or 0), "mean_xs": s.get("mean_xs"), "ci95": s.get("ci95"),
         "verdict": "insufficient_data", "text": ""}
    ci = s.get("ci95") if isinstance(s.get("ci95"), (list, tuple)) and len(s.get("ci95")) == 2 else None
    sess = HYPOTHESIS_WINDOW.rstrip("s")
    if not h["n"] or not s.get("min_n_met") or ci is None or None in ci or s.get("mean_xs") is None:
        h["text"] = (f"Not enough graded entries yet to test this (n={h['n']}); "
                     f"no claim either way.")
        return h
    lo, hi = ci
    m = s["mean_xs"]
    band = f"95% CI {_pct(lo)} to {_pct(hi)}, n={h['n']}"
    if hi < 0:
        h["verdict"] = "supported"
        h["text"] = f"Supported: entries trailed SPY by {_pct(-m)[1:]} over {sess} sessions on average ({band})."
    elif lo > 0:
        h["verdict"] = "contradicted"
        h["text"] = f"Contradicted: entries beat SPY by {_pct(m)[1:]} over {sess} sessions on average ({band})."
    else:
        h["verdict"] = "inconclusive"
        h["text"] = (f"Inconclusive: entries averaged {_pct(m)} vs SPY over {sess} sessions, "
                     f"but the CI includes zero ({band}).")
    return h


def _empty_scorecard() -> dict:
    return {"enter": {"n_events": 0, "windows": {}, "by_bucket": [], "hypothesis": _hypothesis(None)},
            "exit": {"n_events": 0, "windows": {}, "by_bucket": [], "hypothesis": None}}


def _write_priors(conn, errors: list):
    """Upsert family 'crowd' priors from matured excess returns; return the scorecard."""
    try:
        import mason_kb_priors as KB
    except Exception as e:
        errors.append(_err("mason_kb_priors unavailable", e))
        return None
    ranks = _event_ranks(conn)
    groups: dict = {}
    n_events = {"enter": 0, "exit": 0}
    for r in conn.execute("SELECT id, kind, fwd_json FROM crowd_events WHERE record_deleted = 0").fetchall():
        if r["kind"] not in n_events:
            continue
        n_events[r["kind"]] += 1
        fj = _loads(r["fwd_json"], {})
        for h in HORIZONS:
            xs = _finite((fj.get(str(h)) or {}).get("xs_spy"))
            if xs is None:
                continue
            for b in (rank_bucket(ranks.get(r["id"])), "all"):
                g = groups.setdefault((r["kind"], b, f"{h}s"), {"samples": [], "refs": []})
                g["samples"].append(float(xs))
                g["refs"].append(r["id"])
    sc = _empty_scorecard()
    for kind in ("enter", "exit"):
        sc[kind]["n_events"] = n_events[kind]
    errors_before = len(errors)
    try:
        run = KB.begin_run("crowd")          # S1 FINAL_REPORT §7: bracket every recompute
    except Exception as e:
        run = None
        errors.append(_err("begin_run crowd", e))
    for (kind, b, win), g in sorted(groups.items()):
        try:
            row = KB.upsert_prior(
                "crowd", {"event": kind, "rank_bucket": b}, win, g["samples"],
                summary_fmt=(f"Robinhood popular-list {kind} events (rank {b}), {win[:-1]} sessions: "
                             "excess vs SPY mean {mean:+.2%}, n={n}"),
                refs=g["refs"])
        except Exception as e:
            errors.append(_err(f"upsert_prior crowd {kind}/{b}/{win}", e))
            continue
        summ = _window_summary(row)
        if b == "all":
            sc[kind]["windows"][win] = summ
        else:
            sc[kind]["by_bucket"].append({"rank_bucket": b, "window": win, "n": summ["n"],
                                          "mean_xs": summ["mean_xs"], "ci95": summ["ci95"],
                                          "min_n_met": summ["min_n_met"]})
    sc["enter"]["hypothesis"] = _hypothesis(sc["enter"]["windows"].get(HYPOTHESIS_WINDOW))
    # Retire crowd keys this pass no longer produces — only after a clean pass (no grading or upsert
    # errors), or buckets that failed this time would be retired too.
    if run and not errors and len(errors) == errors_before:
        try:
            KB.retire_unrefreshed("crowd", run)
        except Exception as e:
            errors.append(_err("retire_unrefreshed crowd", e))
    _apply_final_shrunk(KB, sc, errors)
    return sc


def _apply_final_shrunk(KB, sc: dict, errors: list) -> None:
    """shrunk_mean depends on the whole family pool, and mason_kb_priors re-shrinks the pool after every
    upsert/retire, so values returned by early upserts are stale by the end of the pass. Read the final
    rows back (as mason_congress._final_shrunk does) and overwrite the pool-dependent stat. n, mean,
    ci95, hit_rate and min_n_met are per-key and unaffected."""
    try:
        q = getattr(KB, "query", None)
        final = {}
        for r in (q("crowd", limit=2000) if q else []) or []:
            k = r.get("key") if isinstance(r, dict) else None
            if isinstance(k, dict):
                final[(k.get("event"), k.get("rank_bucket"), r.get("window"))] = \
                    _finite((r.get("stat") or {}).get("shrunk_mean"))
        for kind in ("enter", "exit"):
            for win, summ in (sc[kind].get("windows") or {}).items():
                if (kind, "all", win) in final:
                    summ["shrunk_mean"] = final[(kind, "all", win)]
    except Exception as e:
        errors.append(_err("kb_priors final read-back", e))


def scorecard() -> dict:
    """Last computed scorecard ({enter, exit}); the empty one before any grading. Never raises."""
    try:
        conn = mason_db.get_connection()
        try:
            sc = _loads(_kv_get(conn, "rhx_crowd_scorecard"), {})
        finally:
            conn.close()
        return sc if {"enter", "exit"} <= set(sc) else _empty_scorecard()
    except Exception as e:
        _err("scorecard", e)
        return _empty_scorecard()


# ── cross-reference ───────────────────────────────────────────────────────────
def _refresh_held() -> None:
    """Job-time only (never inside an API request): RH holdings of Nico's in-scope accounts (Household /
    exclude_accounts and blank labels left out, T-D6) → desk_state, stamped with the scope (RULINGS #13)."""
    try:
        stamp = RH.scope_stamp()
        syms = sorted({str(t).upper() for t in RH.in_scope_position_symbols() if t})
        conn = mason_db.get_connection()
        try:
            with conn:
                _kv_set(conn, "rhx_crowd_held", json.dumps({"scope": stamp, "tickers": syms}))
        finally:
            conn.close()
    except Exception as e:
        _err("holdings refresh (RH)", e)


def _held_tickers() -> set:
    """Tickers Nico really holds (phase 2 A7, RULINGS §21): RH holdings saved by the daily job (in-scope
    accounts only, T-D6) + open, non-deleted, hand-entered personal_positions rows of in-scope accounts
    (same rule as mason_exposure._manual_rows_in_scope: not robinhood_sync, not an RH-synced label, not CASH;
    also not __healthcheck__ and not a blank label). Mason's own book is NOT "held": the trades journal
    (incl. broker paper trades), the options journal and the static positions.csv never count (contracts
    §2.3, paper and real are never mixed). Makes no live RH call, so the API stays DB-only. A saved set
    without the current scope stamp (legacy plain list, or written under another exclude_accounts) counts
    as "no RH holdings" until the next job refresh (RULINGS #13)."""
    s = set()
    try:
        conn = mason_db.get_connection()
        try:
            saved = _loads(_kv_get(conn, "rhx_crowd_held"), {})
            if isinstance(saved, dict) and saved.get("scope") == RH.scope_stamp() \
                    and isinstance(saved.get("tickers"), list):
                s |= {str(t).upper() for t in saved["tickers"] if t}
        finally:
            conn.close()
    except Exception as e:
        _err("holdings (RH, saved)", e)
    try:
        import mason_exposure
        excl = RH.excluded_accounts()
        synced = mason_exposure._synced_labels()
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT ticker, account_label, created_by FROM personal_positions "
                                "WHERE is_deleted=0 AND status='open'").fetchall()
        finally:
            conn.close()
        for r in rows:
            lab = RH.norm_account_label(r["account_label"])
            tkr = str(r["ticker"] or "").strip().upper()
            if not lab or lab in excl or lab == "__healthcheck__" or lab in synced:
                continue
            if (r["created_by"] or "") == "robinhood_sync" or not tkr or tkr == "CASH":
                continue
            s.add(tkr)
    except Exception as e:
        _err("holdings (hand-entered)", e)
    return s


def _watched_tickers() -> set:
    try:
        import mason_watchlist
        if mason_watchlist.enabled():
            return {str(i["ticker"]).upper() for i in mason_watchlist.list_items(status="active")
                    if i.get("ticker")}
    except Exception as e:
        _err("watchlist", e)
    return set()


def _current(conn) -> list:
    """Latest snapshot rows of each configured list: [{ticker, rank, list, list_id, snap_date}]."""
    out = []
    for name in [n for n in _cfg()["lists"] if isinstance(n, str) and n.strip()][:10]:
        lid = _kv_get(conn, _list_key(name))
        if not lid:
            continue
        d = conn.execute("SELECT MAX(snap_date) d FROM crowd_snapshots WHERE list_id = ?", (lid,)).fetchone()["d"]
        if not d:
            continue
        for r in conn.execute("SELECT ticker, rank, list_name FROM crowd_snapshots WHERE list_id = ? "
                              "AND snap_date = ? ORDER BY rank", (lid, d)):
            out.append({"ticker": _plain(r["ticker"]), "rank": r["rank"], "list": _plain(r["list_name"]), "list_id": lid,
                        "snap_date": d})
    return out


def crowded(current: list | None = None) -> list:
    """Held or watched tickers that are on a tracked list right now."""
    if not enabled():
        return []
    try:
        if current is None:
            conn = mason_db.get_connection()
            try:
                current = _current(conn)
            finally:
                conn.close()
        try:
            held = set(_held_tickers())
        except Exception as e:
            held = set()
            _err("holdings", e)
        try:
            watched = set(_watched_tickers())
        except Exception as e:
            watched = set()
            _err("watchlist", e)
        return [{"ticker": c["ticker"], "rank": c["rank"], "list": c["list"],
                 "held": c["ticker"] in held, "watched": c["ticker"] in watched}
                for c in current if c["ticker"] in held or c["ticker"] in watched]
    except Exception as e:
        _err("crowded", e)
        return []


# ── rank history + company profiles (UI overhaul Task 4) ──────────────────────
def _history(conn, cur: list, since: str) -> dict:
    """Rank path of the main list (the first configured list with a snapshot) over the window:
    {"dates": [snap dates >= since, ascending, newest HISTORY_MAX_DATES], "ranks": {TICKER: [rank|None per date]}}.
    None = not on the list that day. DB-only."""
    out = {"dates": [], "ranks": {}}
    if not cur:
        return out
    lid = cur[0]["list_id"]
    dates = [r["snap_date"] for r in conn.execute(
        "SELECT DISTINCT snap_date FROM crowd_snapshots WHERE list_id = ? AND snap_date >= ? "
        "ORDER BY snap_date DESC LIMIT ?", (lid, since, HISTORY_MAX_DATES)).fetchall()][::-1]
    if not dates:
        return out
    idx = {d: i for i, d in enumerate(dates)}
    ranks: dict = {}
    for r in conn.execute("SELECT snap_date, ticker, rank FROM crowd_snapshots WHERE list_id = ? AND snap_date >= ? "
                          "ORDER BY snap_date DESC, rank", (lid, dates[0])):
        i = idx.get(r["snap_date"])
        t = _plain(r["ticker"])
        if i is None or not t:
            continue
        if t not in ranks:
            if len(ranks) >= HISTORY_MAX_TICKERS:
                continue
            ranks[t] = [None] * len(dates)
        ranks[t][i] = r["rank"] if isinstance(r["rank"], int) else None
    out["dates"], out["ranks"] = dates, ranks
    return out


def _labels(conn) -> dict:
    """{TICKER: {name, sector, industry}} saved by warm_profiles (names on hover in the UI). DB-only."""
    raw = _loads(_kv_get(conn, LABELS_KEY), {})
    out = {}
    for t, v in list(raw.items())[:600]:
        if isinstance(t, str) and _SYM_RE.fullmatch(t) and isinstance(v, dict):
            out[t] = {k: (v.get(k) if isinstance(v.get(k), str) else None) for k in ("name", "sector", "industry")}
    return out


def _txt(v, limit: int):
    """Plain, single-spaced text of at most `limit` chars (cut at a word, with an ellipsis), or None."""
    if not isinstance(v, str):
        return None
    s = " ".join(_plain(v).split())
    if not s:
        return None
    return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"


def _int_or_none(v):
    f = _finite(v)
    return int(f) if f is not None and abs(f) < 1e15 else None


_PROFILE_KEYS = ("name", "description", "sector", "industry", "market_cap", "pe_ratio", "dividend_yield",
                 "high_52_weeks", "low_52_weeks", "average_volume", "year_founded", "num_employees",
                 "headquarters", "ceo")


def _fund_clean(r: dict) -> dict:
    """The profile fields the UI shows, from one get_equity_fundamentals row: plain text, finite numbers.
    (RH sends numbers as strings; dividend_yield is already in percent.)"""
    city, state = _txt(r.get("headquarters_city"), 60), _txt(r.get("headquarters_state"), 40)
    return {"description": _txt(r.get("description"), 700),
            "sector": _txt(r.get("sector"), 80), "industry": _txt(r.get("industry"), 80),
            "market_cap": _finite(r.get("market_cap")), "pe_ratio": _finite(r.get("pe_ratio")),
            "dividend_yield": _finite(r.get("dividend_yield")),
            "high_52_weeks": _finite(r.get("high_52_weeks")), "low_52_weeks": _finite(r.get("low_52_weeks")),
            "average_volume": _finite(r.get("average_volume")),
            "year_founded": _int_or_none(r.get("year_founded")), "num_employees": _int_or_none(r.get("num_employees")),
            "headquarters": ", ".join(x for x in (city, state) if x) or None,
            "ceo": _txt(r.get("ceo"), 80)}


def _names_for(tickers) -> dict:
    """{TICKER: company name} from Mason's local universe files: S&P 500 + Nasdaq-100 names, then SEC's ticker
    titles. Job-time only, no network (broad_universe(allow_refresh=False)). {} on any failure."""
    out = {}
    try:
        import mason_triage as TR
        uni = TR.broad_universe(allow_refresh=False) or {}
        for t in tickers:
            n = uni.get(t).get("name") if isinstance(uni.get(t), dict) else None
            if isinstance(n, str) and n.strip():
                out[t] = n.strip()
        missing = [t for t in tickers if t not in out]
        if missing:
            titles = {str(tk).upper(): str(ti) for tk, ti in (TR._edgar_rows() or [])}
            for t in missing:
                n = titles.get(t, "").strip()
                if n:
                    out[t] = n.title() if n.isupper() else n      # "NVIDIA CORP" → "Nvidia Corp"
    except Exception as e:
        _err("company names", e)
    return {t: _txt(n, 80) for t, n in out.items() if _txt(n, 80)}


def _profile_wanted(conn) -> list:
    """Tickers the #crowd page can show: every ticker on a snapshot or in an event of the last 60 days."""
    since = (datetime.now(ET).date() - timedelta(days=60)).isoformat()
    s = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM crowd_snapshots WHERE snap_date >= ?",
                                           (since,)).fetchall()}
    s |= {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM crowd_events WHERE record_deleted = 0 "
                                            "AND event_date >= ?", (since,)).fetchall()}
    return sorted(t for t in s if isinstance(t, str) and _SYM_RE.fullmatch(t))[:300]


def _fund_age_days(entry: dict, today) -> float:
    try:
        return (today - datetime.strptime(str(entry.get("at")), "%Y-%m-%d").date()).days
    except Exception:
        return 1e9


def warm_profiles() -> dict:
    """Job rhx_crowd_profiles: fetch Robinhood fundamentals (read-only) for the page's tickers whose cached
    profile is missing or older than FUND_TTL_DAYS — at most FUND_MAX_CALLS calls of FUND_CHUNK symbols, bulk
    lane — then rebuild the labels blob. {"ran": False} when there was nothing to do (the scheduler logs
    nothing for it). Never raises."""
    if not enabled():
        return {"enabled": False}
    out = {"ran": False, "fetched": 0, "calls": 0, "errors": []}
    conn = None
    try:
        conn = mason_db.get_connection()
        today = datetime.now(ET).date()
        wanted = _profile_wanted(conn)
        cached = {}
        for r in conn.execute("SELECT key, value FROM desk_state WHERE key LIKE ?", (FUND_PREFIX + "%",)).fetchall():
            cached[r["key"][len(FUND_PREFIX):]] = _loads(r["value"], {})
        need = [t for t in wanted if _fund_age_days(cached.get(t) or {}, today) >= FUND_TTL_DAYS]
        need = need[:FUND_CHUNK * FUND_MAX_CALLS]
        names = _names_for(need) if need else {}
        for i in range(0, len(need), FUND_CHUNK):
            chunk = need[i:i + FUND_CHUNK]
            try:
                data = RH._call("get_equity_fundamentals", {"symbols": chunk}, lane="bulk")
                out["calls"] += 1
            except Exception as e:
                out["errors"].append(_err(f"fundamentals {','.join(chunk)}", e))
                break                                   # RH down / rate-limited: try again next run
            res = data.get("results") if isinstance(data, dict) else None
            if not isinstance(res, list):
                out["errors"].append(_err(f"fundamentals {','.join(chunk)}", "malformed payload"))
                continue
            got = {str(r["symbol"]).strip().upper(): r for r in res
                   if isinstance(r, dict) and isinstance(r.get("symbol"), str)}
            with conn:
                for t in chunk:
                    e = {"at": today.isoformat(), "name": names.get(t) or (cached.get(t) or {}).get("name"),
                         "found": t in got}
                    if t in got:
                        e.update(_fund_clean(got[t]))
                    _kv_set(conn, FUND_PREFIX + t, json.dumps(_clean(e), allow_nan=False))
                    cached[t] = e
                    out["fetched"] += 1
        want = set(wanted)
        if out["fetched"] or (cached and not _kv_get(conn, LABELS_KEY)):
            labels = {t: {"name": e.get("name"), "sector": e.get("sector"), "industry": e.get("industry")}
                      for t, e in sorted(cached.items()) if t in want and (e.get("name") or e.get("sector"))}
            with conn:
                _kv_set(conn, LABELS_KEY, json.dumps(_clean(labels), allow_nan=False))
        old = [FUND_PREFIX + t for t, e in cached.items() if t not in want and _fund_age_days(e, today) > FUND_KEEP_DAYS]
        if old:
            with conn:
                conn.executemany("DELETE FROM desk_state WHERE key = ?", [(k,) for k in old])
        out["ran"] = bool(out["calls"] or out["errors"])
        return out
    except Exception as e:
        out["errors"].append(_err("warm_profiles", e))
        out["ran"] = True
        return out
    finally:
        if conn is not None:
            conn.close()


def ticker_payload(symbol) -> dict:
    """Envelope for GET /api/rh/crowd/ticker: one ticker's cached company profile. DB-only. Never raises.
    data.status: "ok" (profile cached) | "not_found" (Robinhood has no fundamentals for it) |
    "pending" (a crowd ticker the next warm run will fetch) | "unknown" (not a crowd ticker: never fetched)."""
    sym = str(symbol or "").strip().upper()
    if not _SYM_RE.fullmatch(sym):
        return {"ok": False, "as_of": _utc_iso(), "data": None, "stale": True,
                "errors": ["symbol: expected 1-12 characters A-Z 0-9 . -"]}
    env = {"ok": True, "as_of": _utc_iso(), "stale": False, "errors": [],
           "data": {"symbol": sym, "status": "unknown", "profile": None, "as_of": None}}
    conn = None
    try:
        conn = mason_db.get_connection()
        e = _loads(_kv_get(conn, FUND_PREFIX + sym), {})
        d = env["data"]
        if e:
            d["as_of"] = e.get("at") if isinstance(e.get("at"), str) else None
            d["status"] = "ok" if e.get("found") is not False else "not_found"
            prof = {k: e.get(k) for k in _PROFILE_KEYS}
            d["profile"] = prof if any(v is not None for v in prof.values()) else None
            env["stale"] = _fund_age_days(e, datetime.now(ET).date()) > FUND_TTL_DAYS * 3
        else:
            d["status"] = "pending" if sym in set(_profile_wanted(conn)) else "unknown"
        return _clean(env)
    except Exception as e:
        env["errors"].append(_err("ticker api", e))
        env["stale"] = True
        return env
    finally:
        if conn is not None:
            conn.close()


# ── API ───────────────────────────────────────────────────────────────────────
def api_payload(days: int = 30) -> dict:
    """The contracts §6 envelope for GET /api/rh/crowd. Never raises."""
    env = {"ok": True, "as_of": _utc_iso(), "data": {"status": "ok", "today": [], "events": [],
                                                    "scorecard": _empty_scorecard(), "crowded": [],
                                                    "list": None, "follow": None,
                                                    "history": {"dates": [], "ranks": {}}, "labels": {}},
           "stale": True, "errors": []}
    conn = None
    try:
        conn = mason_db.get_connection()
        last = _loads(_kv_get(conn, "rhx_crowd_last_run"), {})
        cur = _current(conn)
        d = env["data"]
        d["status"] = "blocked_needs_follow" if last.get("status") == "blocked_needs_follow" else "ok"
        d["today"] = [{"ticker": c["ticker"], "rank": c["rank"], "list": c["list"]} for c in cur]
        names = {}
        for r in conn.execute("SELECT list_id, list_name, MAX(snap_date) d FROM crowd_snapshots "
                              "GROUP BY list_id"):            # SQLite: bare column comes from the MAX row
            names[r["list_id"]] = _plain(r["list_name"])
        since = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
        ranks = _event_ranks(conn)
        for r in conn.execute("SELECT id, list_id, ticker, kind, event_date, fwd_json FROM crowd_events "
                              "WHERE record_deleted = 0 AND event_date >= ? ORDER BY event_date DESC, "
                              "kind, ticker LIMIT 500", (since,)):
            rk = ranks.get(r["id"])
            d["events"].append({"ticker": r["ticker"], "kind": r["kind"], "date": r["event_date"],
                                "list": names.get(r["list_id"]), "rank": rk, "rank_bucket": rank_bucket(rk),
                                "fwd": _loads(r["fwd_json"], {})})
        sc = _loads(_kv_get(conn, "rhx_crowd_scorecard"), {})
        if {"enter", "exit"} <= set(sc):
            d["scorecard"] = sc
        d["crowded"] = crowded(cur)
        fa = _loads(_kv_get(conn, "rhx_crowd_follow_attempt"), {})
        d["follow"] = ({"list": _plain(fa.get("list")), "attempted_at": fa.get("attempted_at"),
                        "ok": bool(fa.get("ok")), "error": _plain(fa.get("error"))} if fa else None)
        try:                                  # Task 4 extras: a failure here is logged, never fails the page
            d["history"] = _history(conn, cur, since)
        except Exception as e:
            _err("history", e)
        try:
            d["labels"] = _labels(conn)
        except Exception as e:
            _err("labels", e)
        if cur:
            d["list"] = {"name": cur[0]["list"], "id": cur[0]["list_id"], "snap_date": cur[0]["snap_date"],
                         "n_items": sum(1 for c in cur if c["list_id"] == cur[0]["list_id"])}
            age = (datetime.now(ET).date() - datetime.strptime(cur[0]["snap_date"], "%Y-%m-%d").date()).days
            env["stale"] = age > 4
        env["errors"] = [_plain(e)[:300] for e in (last.get("errors") or [])][:10]
        if env["errors"]:
            env["stale"] = True
        return _clean(env)
    except Exception as e:
        env["errors"].append(_err("api", e))
        return env
    finally:
        if conn is not None:
            conn.close()


bp = Blueprint("rh_crowd", __name__)


@bp.route("/api/rh/crowd", methods=["GET"])
def api_crowd():
    try:
        if not enabled():
            return jsonify({"enabled": False})
        try:
            days = max(1, min(3650, int(request.args.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        return jsonify(api_payload(days))
    except Exception as e:
        return jsonify({"ok": False, "as_of": _utc_iso(), "data": None, "stale": True,
                        "errors": [_err("api", e)]})


@bp.route("/api/rh/crowd/ticker", methods=["GET"])
def api_crowd_ticker():
    """?symbol=NVDA → the cached company profile for the #crowd Ticker Snapshot (ticker_payload)."""
    try:
        if not enabled():
            return jsonify({"enabled": False})
        env = ticker_payload(request.args.get("symbol"))
        return jsonify(env), (200 if env.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "as_of": _utc_iso(), "data": None, "stale": True,
                        "errors": [_err("ticker api", e)]})


# ── jobs + status ─────────────────────────────────────────────────────────────
def _is_trading_day(et: datetime) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(et))
    except Exception:
        return et.weekday() < 5


def run_daily(now: datetime | None = None) -> dict:
    """The daily job: snapshot (trading days only) then grade. Never raises."""
    if not enabled():
        return {"enabled": False}
    try:
        et = (now or datetime.now(ET)).astimezone(ET)
        if not _is_trading_day(et):
            return {"enabled": True, "skipped": "not a trading day", "date": et.strftime("%Y-%m-%d")}
        snap = snapshot(et.strftime("%Y-%m-%d"))
        _refresh_held()
        return {"enabled": True, "snapshot": snap, "grade": grade()}
    except Exception as e:
        return {"enabled": True, "errors": [_err("run_daily", e)]}


def jobs() -> list:
    """For S2-T8's scheduler: the daily job at crowd.snapshot_time_et, plus (Task 4) the hourly profile warm
    (a no-op unless a page ticker lacks a fresh profile). Empty while off."""
    if not enabled():
        return []
    return [{"name": "rhx_crowd_daily", "at_et": str(_cfg().get("snapshot_time_et") or "16:30"),
             "fn": run_daily, "trading_days_only": True},
            {"name": "rhx_crowd_profiles", "every_s": PROFILE_EVERY_S, "window_et": ("06:00", "22:00"),
             "fn": warm_profiles, "trading_days_only": False,
             "note": "company profiles for the #crowd Ticker Snapshot (RH fundamentals, read-only)"}]


def status() -> dict:
    """contracts §6 /api/rh/status module row. Never raises."""
    out = {"name": "crowd", "enabled": enabled(), "state": _state.get("state"), "last_run": _state.get("last_run"),
           "rows": 0, "events": 0, "errors": list(_state.get("errors") or [])[-10:],
           "truncated": int(_state.get("truncated") or 0), "rate_hits": int(_state.get("rate_hits") or 0),
           "xcheck_mismatch": 0, "follow_calls": int(_state.get("follow_calls") or 0)}
    if not out["enabled"]:
        return out
    try:
        conn = mason_db.get_connection()
        try:
            out["rows"] = conn.execute("SELECT COUNT(*) n FROM crowd_snapshots").fetchone()["n"]
            out["events"] = conn.execute("SELECT COUNT(*) n FROM crowd_events WHERE record_deleted = 0"
                                         ).fetchone()["n"]
            if out["state"] is None:
                last = _loads(_kv_get(conn, "rhx_crowd_last_run"), {})
                out["state"], out["last_run"] = last.get("status"), last.get("at")
        finally:
            conn.close()
    except Exception as e:
        out["errors"] = out["errors"] + [_err("status", e)][-10:]
    return out
