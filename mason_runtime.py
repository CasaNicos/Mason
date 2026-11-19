"""
mason_runtime.py — keeps Mason's background jobs running (audit O3 / O7 / regular fill reconcile, 2026-11-15).

Three pieces, all inside the one Mason process:

1. SUPERVISOR ("mason-supervisor" thread, every 30 s)
   Every long-running background loop beats a heartbeat here (beat(name)), with the interval it normally runs at.
   The supervisor
     * restarts a loop whose thread has DIED (backoff 30 s, 60 s, 120 s ...; at most
       runtime.supervisor.max_restarts_per_hour restarts per loop per hour, so a crash-loop cannot spin);
       when the cap is reached it stops trying for that hour and puts ONE item in the inbox;
     * reports a loop that is alive but has not beaten for its stale_after window (HUNG). Python cannot stop a
       stuck thread from outside, so this is report-only: one inbox item per loop per day ("a restart of Mason
       clears it"), and the loop is shown as stale in /api/health and the chat self-check;
     * writes the table to .mason_runtime.json (and keeps it in memory: snapshot()) so /api/health can show it;
     * pings systemd's watchdog (WATCHDOG=1) on every pass when the unit asks for it (WatchdogSec set ->
       NOTIFY_SOCKET in the environment). Without that setting the ping is a no-op.
   Loops in files this module may not edit (mason_news_service, mason_analyst_desk) are watched from outside:
   their per-pass functions are wrapped at start() so each pass beats (see _hook_news / _hook_desk).

2. REGULAR FILL RECONCILE ("fill-reconcile" thread)
   Fills used to reach the journal only at boot or through the chat reconcile tool. This thread runs
   mason_execution.reconcile() every execution.periodic_reconcile.interval_minutes (default 5) between
   09:30 and 16:15 ET on trading days, plus one pass from 16:20 ET. A pass is skipped (and retried 30 s later)
   while another reconcile / journal repair holds mason_execution's books lock. Switch:
   execution.periodic_reconcile.enabled (default true).

3. SHORT SERVER CACHE for the hot dashboard endpoints (TTLCache / hot_ttl())
   A dashboard tab polls /api/paper/positions, /api/sectors/live and /api/following/quotes every second.
   Their Alpaca reads are served from a ~5 s cache (30 s outside 04:00-20:00 ET on weekdays), with one fetch at
   a time per key. Settings: runtime.hot_cache.ttl_seconds / closed_ttl_seconds.

Settings (mason.config.json, all optional; read on every pass):
  runtime.supervisor.enabled               default true   (false: no restarts / alerts; the watchdog ping still runs)
  runtime.supervisor.tick_seconds          default 30
  runtime.supervisor.max_restarts_per_hour default 3
  runtime.supervisor.notify                default true
  runtime.hot_cache.ttl_seconds            default 5
  runtime.hot_cache.closed_ttl_seconds     default 30
  execution.periodic_reconcile.enabled     default true
  execution.periodic_reconcile.interval_minutes  default 5
  execution.periodic_reconcile.start_et / end_et / final_et   default "09:30" / "16:15" / "16:20"
  execution.periodic_reconcile.buy_grace_minutes default 60 (a buy gets an 'alpaca-sync' row only after this long,
                                           so autotrade / the chat order / Nico journal it first; sells at once)

Nothing here places an order, calls a model, or exits the process.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("mason.runtime")

BASE = Path(__file__).parent
STATE_FILE = BASE / ".mason_runtime.json"

_SUP_DEFAULTS = {"enabled": True, "tick_seconds": 30, "max_restarts_per_hour": 3, "notify": True}
_CACHE_DEFAULTS = {"ttl_seconds": 5, "closed_ttl_seconds": 30}
_RC_DEFAULTS = {"enabled": True, "interval_minutes": 5, "start_et": "09:30", "end_et": "16:15",
                "final_et": "16:20", "final_window_minutes": 180, "alert_after_failed_passes": 6,
                "buy_grace_minutes": 60}
RESTART_BACKOFF_S = (30, 60, 120, 240, 480)


# ── config ─────────────────────────────────────────────────────────────────────

_cfg_cache = {"mtime": None, "data": {}}


def _load_cfg() -> dict:
    p = BASE / "mason.config.json"
    try:
        mt = p.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(p.read_text(encoding="utf-8")) or {}
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"]
    except Exception:
        return _cfg_cache["data"] or {}


def _section(cfg, path, defaults) -> dict:
    node = cfg if isinstance(cfg, dict) else {}
    for k in path:
        node = node.get(k) if isinstance(node, dict) else None
    out = dict(defaults)
    if isinstance(node, dict):
        out.update({k: v for k, v in node.items() if not str(k).startswith("_")})
    return out


def sup_cfg(cfg=None) -> dict:
    return _section(cfg if cfg is not None else _load_cfg(), ("runtime", "supervisor"), _SUP_DEFAULTS)


def cache_cfg(cfg=None) -> dict:
    return _section(cfg if cfg is not None else _load_cfg(), ("runtime", "hot_cache"), _CACHE_DEFAULTS)


def rc_cfg(cfg=None) -> dict:
    return _section(cfg if cfg is not None else _load_cfg(), ("execution", "periodic_reconcile"), _RC_DEFAULTS)


def _now_et(now: float | None = None) -> datetime:
    ts = time.time() if now is None else now
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York"))
    except Exception:
        return datetime.fromtimestamp(ts)


def _hm(s, default) -> int:
    try:
        h, _, m = str(s).partition(":")
        return int(h) * 60 + int(m)
    except Exception:
        h, _, m = default.partition(":")
        return int(h) * 60 + int(m)


# ── heartbeats ─────────────────────────────────────────────────────────────────

_lock = threading.RLock()
_jobs: dict = {}           # name -> spec + state (see register)


def register(name: str, *, interval: float, stale_after: float | None = None, thread_name: str | None = None,
             thread=None, restart=None, should_run=None, label: str | None = None, now: float | None = None) -> None:
    """Watch loop `name`.
    interval     how often it normally beats (seconds; shown to Nico)
    stale_after  seconds without a beat before it counts as hung (None = liveness only, e.g. a websocket that
                 blocks inside one long session)
    thread       callable returning the loop's Thread (preferred), else thread_name is looked up by name
    restart      callable that starts a fresh thread for the loop and returns it (None = report only)
    should_run   callable -> bool; False means the loop is meant to be off (not watched this pass)"""
    t = time.time() if now is None else now
    with _lock:
        j = _jobs.get(name) or {"last_beat": None, "beats": 0, "busy": None, "busy_since": None,
                                "restarts": [], "next_try": 0.0, "status": "ok", "stale_noted": None,
                                "cap_noted": None, "dead_since": None, "last_error": None}
        j.update({"name": name, "label": label or name, "interval": float(interval),
                  "stale_after": float(stale_after) if stale_after else None,
                  "thread_name": thread_name or name, "thread": thread, "restart": restart,
                  "should_run": should_run, "registered": j.get("registered") or t})
        j.pop("unregistered", None)
        _jobs[name] = j


def beat(name: str, now: float | None = None) -> None:
    """One pass of loop `name` happened (clears its 'busy' note). Cheap; never raises."""
    try:
        t = time.time() if now is None else now
        with _lock:
            j = _jobs.get(name)
            if j is None:                     # beats before registration are kept (registered later)
                j = _jobs[name] = {"name": name, "label": name, "interval": 0.0, "stale_after": None,
                                   "thread_name": name, "thread": None, "restart": None, "should_run": None,
                                   "registered": t, "last_beat": None, "beats": 0, "busy": None,
                                   "busy_since": None, "restarts": [], "next_try": 0.0, "status": "ok",
                                   "stale_noted": None, "cap_noted": None, "dead_since": None, "last_error": None,
                                   "unregistered": True}
            j["last_beat"] = t
            j["beats"] = int(j.get("beats") or 0) + 1
            j["busy"] = j["busy_since"] = None
    except Exception:
        pass


def busy(name: str, what: str, now: float | None = None) -> None:
    """Loop `name` started a long piece of work `what` (shown when it is reported as stuck). Also a beat."""
    beat(name, now)
    try:
        with _lock:
            j = _jobs.get(name)
            if j is not None:
                j["busy"] = str(what)[:80]
                j["busy_since"] = time.time() if now is None else now
    except Exception:
        pass


def _find_thread(j) -> threading.Thread | None:
    try:
        if j.get("thread"):
            th = j["thread"]()
            if th is not None:
                return th
    except Exception:
        pass
    for th in threading.enumerate():
        if th.name == j.get("thread_name") and th.is_alive():
            return th
    return None


# ── the supervisor pass ────────────────────────────────────────────────────────

def _fmt_age(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s} seconds"
    if s < 7200:
        return f"{s // 60} minutes"
    return f"{s / 3600:.1f} hours"


def _default_notifier(**kw):
    import mason_notifications as MN
    return MN.add_notification("system", kw.pop("title"), **kw)


def check_once(now: float | None = None, *, notifier=None, find_thread=None, cfg: dict | None = None) -> dict:
    """One supervisor pass over every registered loop. Returns {name: action} for what it did.
    Injectable for tests (now / notifier / find_thread / cfg). Never raises."""
    t = time.time() if now is None else now
    sc = sup_cfg(cfg)
    notify = notifier or _default_notifier
    finder = find_thread or _find_thread
    actions: dict = {}
    if sc.get("enabled") is False:
        return actions
    cap = max(0, int(sc.get("max_restarts_per_hour", 3) or 0))
    day = _now_et(t).strftime("%Y-%m-%d")
    with _lock:
        jobs = [j for j in _jobs.values() if not j.get("unregistered")]
    for j in jobs:
        name = j["name"]
        try:
            want = True
            if j.get("should_run"):
                try:
                    want = bool(j["should_run"]())
                except Exception:
                    want = False
            if not want:
                with _lock:
                    j["status"] = "off"
                continue
            th = finder(j)
            if th is None or not th.is_alive():
                actions[name] = _handle_dead(j, t, cap, day, notify, sc)
                continue
            with _lock:
                if j["status"] in ("dead", "restarting", "gave_up"):
                    j["status"] = "ok"
                j["dead_since"] = None
            sa = j.get("stale_after")
            if not sa:
                continue
            last = j.get("last_beat") or j.get("registered") or t
            age = t - last
            if age > sa:
                actions[name] = "stale"
                first = j["status"] != "stale"
                with _lock:
                    j["status"] = "stale"
                if first:
                    log.warning(f"[runtime] {name} has not checked in for {_fmt_age(age)} "
                                f"(normally every {_fmt_age(j['interval'])})"
                                + (f"; busy with {j['busy']}" if j.get("busy") else ""))
                if j.get("stale_noted") != day and sc.get("notify", True):
                    with _lock:
                        j["stale_noted"] = day
                    _say(notify, title=f"Background job looks stuck: {j['label']}",
                         body=(f"Mason's background job '{j['label']}' is still running but has not checked in for "
                               f"{_fmt_age(age)}; it normally checks in every {_fmt_age(j['interval'])}."
                               + (f" It has been working on '{j['busy']}' since "
                                  f"{_now_et(j['busy_since']).strftime('%H:%M')} ET." if j.get("busy") else "")
                               + " Mason cannot safely restart a stuck job on its own. If it is still stuck at the "
                               "next check, restarting Mason clears it (sudo systemctl restart mason)."),
                         severity="warn", dedup_key=f"runtime_stale:{name}:{day}")
            elif j["status"] == "stale":
                log.info(f"[runtime] {name} is checking in again")
                with _lock:
                    j["status"] = "ok"
        except Exception as e:
            log.warning(f"[runtime] supervising {name} failed: {e}")
    return actions


def _say(notify, **kw):
    try:
        notify(created_by="mason_runtime", **kw)
    except Exception as e:
        log.warning(f"[runtime] could not post to the inbox: {e}")


def _handle_dead(j, t, cap, day, notify, sc) -> str:
    name = j["name"]
    with _lock:
        if j.get("dead_since") is None:
            j["dead_since"] = t
        j["restarts"] = [r for r in j["restarts"] if t - r < 3600]
        recent = len(j["restarts"])
    if j.get("restart") is None:
        with _lock:
            first = j["status"] != "dead"
            j["status"] = "dead"
        if first:
            log.error(f"[runtime] {name} has stopped (no automatic restart for this one)")
        if j.get("stale_noted") != day and sc.get("notify", True):
            with _lock:
                j["stale_noted"] = day
            _say(notify, title=f"Background job stopped: {j['label']}",
                 body=(f"Mason's background job '{j['label']}' has stopped and cannot be restarted on its own. "
                       "Restarting Mason brings it back (sudo systemctl restart mason)."),
                 severity="warn", dedup_key=f"runtime_dead:{name}:{day}")
        return "dead"
    if recent >= cap:
        with _lock:
            j["status"] = "gave_up"
            noted = j.get("cap_noted") == day
            j["cap_noted"] = day
        if not noted:
            log.error(f"[runtime] {name} stopped {recent} times in the last hour; not restarting it for now")
            if sc.get("notify", True):
                _say(notify, title=f"Background job keeps stopping: {j['label']}",
                     body=(f"Mason's background job '{j['label']}' stopped {recent} times within an hour. Mason has "
                           "stopped restarting it for now and will try again once the hour has passed. "
                           + (f"Last error: {j['last_error']}. " if j.get("last_error") else "")
                           + "The service log has the details (journalctl -u mason)."),
                     severity="warn", dedup_key=f"runtime_gave_up:{name}:{day}")
        return "gave_up"
    if t < float(j.get("next_try") or 0):
        with _lock:
            j["status"] = "restarting"
        return "waiting"
    try:
        th = j["restart"]()
        err = None if th is not None else "restart returned nothing"
    except Exception as e:
        th, err = None, f"{type(e).__name__}: {e}"
    with _lock:
        j["restarts"].append(t)
        n = len(j["restarts"])
        j["next_try"] = t + RESTART_BACKOFF_S[min(n - 1, len(RESTART_BACKOFF_S) - 1)]
        j["status"] = "restarting"
        j["last_error"] = err or j.get("last_error")
        j["last_beat"] = None
        j["registered"] = t                 # stale grace starts again with the new thread
    if err:
        log.error(f"[runtime] restarting {name} failed: {err}")
        return "restart_failed"
    log.warning(f"[runtime] {name} had stopped; restarted it (restart {n} this hour)")
    if sc.get("notify", True):
        _say(notify, title=f"Background job restarted: {j['label']}",
             body=(f"Mason's background job '{j['label']}' had stopped. Mason restarted it on its own "
                   f"(restart {n} in the last hour; at most {cap} per hour)."),
             severity="info", dedup_key=f"runtime_restart:{name}:{day}")
    return "restarted"


def snapshot(now: float | None = None) -> dict:
    """The table shown in /api/health ('background_jobs') and the chat self-check. Never raises."""
    t = time.time() if now is None else now
    rows = []
    try:
        with _lock:
            jobs = [dict(j) for j in _jobs.values() if not j.get("unregistered")]
        for j in sorted(jobs, key=lambda x: x["name"]):
            last = j.get("last_beat")
            rows.append({
                "name": j["name"], "label": j["label"], "status": j["status"],
                "every_seconds": j["interval"], "stale_after_seconds": j.get("stale_after"),
                "last_beat_age_seconds": round(t - last, 1) if last else None,
                "beats": j.get("beats", 0), "busy_with": j.get("busy"),
                "busy_for_seconds": round(t - j["busy_since"], 1) if j.get("busy_since") else None,
                "restarts_last_hour": len([r for r in j.get("restarts") or [] if t - r < 3600]),
                "last_error": j.get("last_error"),
            })
    except Exception as e:
        return {"ok": None, "error": str(e), "jobs": []}
    bad = [r["name"] for r in rows if r["status"] in ("stale", "dead", "gave_up", "restarting")]
    return {"ok": not bad, "problems": bad, "jobs": rows, "supervisor_running": _sup_alive(),
            "watchdog": dict(_wd), "reconcile": dict(_rc_state.get("last") or {}),
            "generated_at": datetime.now().isoformat(timespec="seconds")}


def summary_lines(snap: dict | None = None) -> list:
    """Plain lines for the chat self-check: only the loops that need attention."""
    s = snap or snapshot()
    out = []
    for r in s.get("jobs") or []:
        if r["status"] == "stale":
            out.append(f"  [warn] background job '{r['label']}' has not checked in for "
                       f"{_fmt_age(r['last_beat_age_seconds'] or 0)}"
                       + (f" (busy with {r['busy_with']})" if r.get("busy_with") else ""))
        elif r["status"] in ("dead", "gave_up", "restarting"):
            out.append(f"  [warn] background job '{r['label']}' is stopped ({r['status']}; "
                       f"{r['restarts_last_hour']} restart(s) in the last hour)")
    if not s.get("supervisor_running") and _started:
        out.append("  [warn] the background-job supervisor itself is not running")
    return out


def _write_state(snap: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.debug(f"[runtime] state file not written: {e}")


# ── systemd watchdog (sd_notify without a dependency) ─────────────────────────

_wd = {"enabled": False, "pings": 0, "last_ping": None}


def sd_notify(msg: str) -> bool:
    """Send one sd_notify datagram. False (no-op) when systemd did not ask for it. Never raises."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except Exception as e:
        # (2026-11-16, leftovers 2; HYGIENE_REVIEW F2) with a watchdog on, a lost ping gets Mason restarted by systemd
        (log.warning if _wd["enabled"] else log.debug)(f"[runtime] sd_notify failed: {e}")
        return False


def _watchdog_wanted() -> bool:
    if not os.environ.get("NOTIFY_SOCKET") or not os.environ.get("WATCHDOG_USEC"):
        return False
    pid = os.environ.get("WATCHDOG_PID")
    return not pid or pid == str(os.getpid())


def _watchdog_ping() -> None:
    if _wd["enabled"] and sd_notify("WATCHDOG=1"):
        _wd["pings"] += 1
        _wd["last_ping"] = datetime.now().isoformat(timespec="seconds")


# ── short server cache for hot endpoints (O7) ─────────────────────────────────

class TTLCache:
    """{key: (time, value)} with a per-key lock, so concurrent callers of a missing key wait for ONE compute."""

    def __init__(self, clock=time.time):
        self._d: dict = {}
        self._locks: dict = {}
        self._lock = threading.Lock()
        self._clock = clock
        self.computes = 0

    def get(self, key, compute, ttl: float):
        with self._lock:
            hit = self._d.get(key)
            kl = self._locks.setdefault(key, threading.Lock())
        if hit is not None and self._clock() - hit[0] < ttl:
            return hit[1]
        with kl:
            with self._lock:
                hit = self._d.get(key)
            if hit is not None and self._clock() - hit[0] < ttl:
                return hit[1]
            val = compute()
            with self._lock:
                self._d[key] = (self._clock(), val)
                self.computes += 1
            return val

    def clear(self, key=None) -> None:
        with self._lock:
            if key is None:
                self._d.clear()
            else:
                self._d.pop(key, None)


def hot_ttl(now: float | None = None, cfg: dict | None = None) -> float:
    """Seconds the hot endpoints may reuse an Alpaca read: ttl_seconds (5) on weekdays 04:00-20:00 ET (the
    extended session), closed_ttl_seconds (30) otherwise."""
    c = cache_cfg(cfg)
    et = _now_et(now)
    open_ish = et.weekday() < 5 and 4 * 60 <= et.hour * 60 + et.minute < 20 * 60
    try:
        return max(0.0, float(c["ttl_seconds"] if open_ish else c["closed_ttl_seconds"]))
    except Exception:
        return 5.0


HOT_CACHE = TTLCache()


# ── regular fill reconcile ─────────────────────────────────────────────────────

_rc_state: dict = {"last_run": None, "final_done": None, "fails": 0, "alerted": None, "last": None}


def _trading_day(et: datetime) -> bool:
    try:
        import mason_director as DIR
        return bool(DIR.is_trading_day(et))
    except Exception:
        return et.weekday() < 5


def reconcile_due(now: float, state: dict, cfg: dict, trading_day=_trading_day) -> str | None:
    """'intraday' / 'final' when a pass is due at `now`, else None. Pure (state/cfg passed in)."""
    if not cfg.get("enabled", True):
        return None
    et = _now_et(now)
    if not trading_day(et):
        return None
    hm = et.hour * 60 + et.minute
    start, end = _hm(cfg.get("start_et"), "09:30"), _hm(cfg.get("end_et"), "16:15")
    final = _hm(cfg.get("final_et"), "16:20")
    try:
        every = max(60.0, float(cfg.get("interval_minutes", 5)) * 60)
    except Exception:
        every = 300.0
    if start <= hm <= end:
        last = state.get("last_run")
        if last is None or now - last >= every - 1:
            return "intraday"
        return None
    today = et.strftime("%Y-%m-%d")
    win = float(cfg.get("final_window_minutes", 180) or 180)
    if final <= hm < final + win and state.get("final_done") != today:
        return "final"
    return None


def run_reconcile_pass(kind: str, now: float | None = None, *, reconcile=None, notifier=None,
                       cfg: dict | None = None) -> dict:
    """Run one scheduled reconcile. Skipped (not counted as run) while another holds the books lock."""
    t = time.time() if now is None else now
    if reconcile is None:
        import mason_execution as EX
        _c = cfg if cfg is not None else rc_cfg()
        try:
            grace = max(0.0, float(_c.get("buy_grace_minutes", 60)) * 60)
        except (TypeError, ValueError):
            grace = 3600.0
        reconcile = lambda: EX.reconcile(sync_journal=True, wait=0, buy_grace_s=grace)   # noqa: E731 — never wait
    try:
        res = reconcile() or {}
    except Exception as e:
        res = {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}
    if res.get("busy"):
        log.info("[reconcile] another reconcile or journal repair is running; this pass waits 30 s")
        return res
    et = _now_et(t)
    _rc_state["last_run"] = t
    if kind == "final":
        _rc_state["final_done"] = et.strftime("%Y-%m-%d")
    _rc_state["last"] = {"at": et.isoformat(timespec="seconds"), "kind": kind, "ok": bool(res.get("ok")),
                         "fills_seen": res.get("fills_seen"), "journal_synced": res.get("journal_synced"),
                         "options_synced": res.get("options_synced"),
                         "held": len(res.get("deferred_fills") or []),
                         "refused": len(res.get("refused_fills") or []),
                         "waiting": len(res.get("waiting_fills") or []),
                         "alpaca_requests": res.get("alpaca_requests"),
                         "errors": [str(e)[:200] for e in (res.get("errors") or [])][:3]}
    if res.get("ok"):
        _rc_state["fails"] = 0
        if res.get("journal_synced") or res.get("options_synced"):
            log.info(f"[reconcile] {kind}: booked {res.get('journal_synced')} stock / "
                     f"{res.get('options_synced')} option fill(s) into the journal")
    else:
        _rc_state["fails"] += 1
        log.warning(f"[reconcile] {kind} pass failed ({_rc_state['fails']} in a row): {res.get('errors')}")
        c = cfg if cfg is not None else rc_cfg()
        n = int(c.get("alert_after_failed_passes", 6) or 6)
        day = et.strftime("%Y-%m-%d")
        if _rc_state["fails"] >= n and _rc_state.get("alerted") != day:
            _rc_state["alerted"] = day
            _say(notifier or _default_notifier,
                 title="Fills are not reaching the journal",
                 body=(f"Mason's regular check of Alpaca fills has failed {_rc_state['fails']} times in a row "
                       f"(about every {c.get('interval_minutes', 5)} minutes). New buys and sells may be missing "
                       f"from the journal until it works again. Last error: "
                       f"{'; '.join(str(e)[:160] for e in (res.get('errors') or [])[:2]) or 'unknown'}."),
                 severity="warn", dedup_key=f"reconcile_failing:{day}")
    return res


def _reconcile_loop(tick: float = 30.0) -> None:
    while True:
        beat("fill-reconcile")
        try:
            kind = reconcile_due(time.time(), _rc_state, rc_cfg())
            if kind:
                busy("fill-reconcile", f"{kind} reconcile")
                run_reconcile_pass(kind)
                beat("fill-reconcile")
        except Exception as e:
            log.warning(f"[reconcile] scheduler error: {e}")
        time.sleep(tick)


def _start_reconcile_thread() -> threading.Thread:
    th = threading.Thread(target=_reconcile_loop, daemon=True, name="fill-reconcile")
    th.start()
    return th


# ── watching loops in modules this file does not own ──────────────────────────

def _hook_news() -> bool:
    """Beat from the news scanner (each scan_once on its thread) and the scorer (each pass reads its settings via
    _ncfg on its thread). Wraps module functions the loops look up at call time; no-op if the module beats itself."""
    try:
        import mason_news_service as NS
    except Exception:
        return False
    if getattr(NS, "_RUNTIME_BEATS", False) or getattr(NS, "_runtime_hooked", False):
        return True
    orig_scan, orig_ncfg = NS.scan_once, NS._ncfg

    def scan_once(*a, **k):
        if threading.current_thread().name == "news-scanner":
            busy("news-scanner", "news pass (feeds, triage, theses)")
        return orig_scan(*a, **k)

    def _ncfg(*a, **k):
        if threading.current_thread().name == "news-scorer":
            beat("news-scorer")
        return orig_ncfg(*a, **k)

    scan_once.__wrapped__, _ncfg.__wrapped__ = orig_scan, orig_ncfg
    NS.scan_once, NS._ncfg = scan_once, _ncfg
    NS._runtime_hooked = True
    return True


def _hook_desk() -> bool:
    """Beat from the analyst desk: the scheduler on every tick (30 s), the worker on every queue poll (2 s) and,
    while it handles a job, a 'busy with <kind>' note. Instance attributes shadow the class methods."""
    try:
        import mason_analyst_desk as AD
        d = AD.get_desk()
    except Exception:
        return False
    if d is None:
        return False
    if getattr(d, "_runtime_hooked", False):
        return True
    o_tick, o_once, o_handle = d._scheduler_tick, d.run_once, d._handle

    def _scheduler_tick(*a, **k):
        beat("desk-scheduler")
        return o_tick(*a, **k)

    def run_once(*a, **k):
        if threading.current_thread().name == "analyst-desk-worker":
            beat("desk-worker")
        return o_once(*a, **k)

    def _handle(kind, payload, *a, **k):
        if threading.current_thread().name == "analyst-desk-worker":
            busy("desk-worker", kind)
        return o_handle(kind, payload, *a, **k)

    d._scheduler_tick, d.run_once, d._handle = _scheduler_tick, run_once, _handle
    d._runtime_hooked = True
    return True


def _has_beaten(name):
    """should_run for a loop with no 'started' flag: watched once it has beaten at least once."""
    def f():
        with _lock:
            return int((_jobs.get(name) or {}).get("beats") or 0) > 0
    return f


def _mod(name):
    import sys
    return sys.modules.get(name)


def _thread_starter(target, name, args=()):
    def go():
        th = threading.Thread(target=target, args=args, daemon=True, name=name)
        th.start()
        return th
    return go


def _desk_restart(which: str):
    def go():
        AD = _mod("mason_analyst_desk")
        d = AD.get_desk() if AD else None
        if d is None:
            return None
        if which == "worker":
            th = threading.Thread(target=d._worker_loop, daemon=True, name="analyst-desk-worker")
            d._worker = th
        else:
            th = threading.Thread(target=d._scheduler_loop, daemon=True, name="analyst-desk-scheduler")
            d._scheduler = th
        th.start()
        return th
    return go


def _desk_on():
    AD = _mod("mason_analyst_desk")
    d = AD.get_desk() if AD else None
    return d is not None and not d._stop.is_set()


def _health_restart():
    HM = _mod("mason_health")
    m = getattr(HM, "_monitor", None) if HM else None
    if m is None:
        return None
    m.start_thread(tick_seconds=getattr(m, "_tick_seconds", 30.0))
    return m._thread


def register_defaults(app_module=None) -> list:
    """Register every long-running loop Mason starts at boot. Safe to call more than once."""
    names = []

    def reg(name, **kw):
        register(name, **kw)
        names.append(name)

    reg("fill-reconcile", label="regular fill reconcile", interval=30, stale_after=20 * 60,
        restart=_start_reconcile_thread)
    NS = _mod("mason_news_service")
    if NS is not None:
        reg("news-scanner", label="news feed scanner", interval=120, stale_after=30 * 60,
            restart=_thread_starter(lambda: NS._scanner_loop(120), "news-scanner"),
            should_run=lambda: bool(getattr(NS, "_scanner_started", False)))
        reg("news-scorer", label="news scorer", interval=20, stale_after=30 * 60,
            restart=_thread_starter(NS._scorer_loop, "news-scorer"),
            should_run=lambda: bool(getattr(NS, "_scorer_started", False)))
        reg("alpaca-news", label="Alpaca real-time news stream", interval=60, stale_after=None,
            restart=_thread_starter(NS._run_alpaca_ws, "alpaca-news"),
            should_run=lambda: bool(getattr(NS, "_alpaca_started", False)))
    if _mod("mason_analyst_desk") is not None:
        reg("desk-scheduler", label="analyst desk scheduler (director, self-check, desk checks, agent loop)",
            interval=30, stale_after=15 * 60, thread_name="analyst-desk-scheduler",
            thread=lambda: getattr(_mod("mason_analyst_desk").get_desk(), "_scheduler", None),
            restart=_desk_restart("scheduler"), should_run=_desk_on)
        reg("desk-worker", label="analyst desk worker (runs the desk's jobs one at a time)",
            interval=2, stale_after=60 * 60, thread_name="analyst-desk-worker",
            thread=lambda: getattr(_mod("mason_analyst_desk").get_desk(), "_worker", None),
            restart=_desk_restart("worker"), should_run=_desk_on)
    HM = _mod("mason_health")
    if HM is not None:
        reg("health-monitor", label="source-health monitor", interval=30, stale_after=15 * 60,
            thread=lambda: getattr(getattr(HM, "_monitor", None), "_thread", None),
            restart=_health_restart,
            should_run=lambda: getattr(HM, "_monitor", None) is not None
            and not HM._monitor._stop.is_set())
    MM = _mod("mason_maintenance")
    if MM is not None and hasattr(MM, "_maintenance_loop"):
        reg("mason-maintenance", label="nightly maintenance (backup, prune, calendar feeds, grader)",
            interval=3600, stale_after=4 * 3600,
            restart=_thread_starter(MM._maintenance_loop, "mason-maintenance", (3600, 5)),
            should_run=lambda: bool(getattr(MM, "_started", False)))
    WT = _mod("mason_watch_tape")
    if WT is not None and hasattr(WT, "_guard_loop"):
        for tname, tick_name, stale in (("watch-universe", "_universe_tick", 45 * 60),
                                        ("watch-stream", "_stream_tick", None),
                                        ("watch-extended", "_extended_tick", 45 * 60),
                                        ("watch-maint", "_maint_tick", 45 * 60),
                                        ("watch-options", "_options_tick", 45 * 60)):
            tick = getattr(WT, tick_name, None)
            if tick is None:
                continue
            reg(tname, label=f"watchlist tape ({tname[6:]})", interval=60, stale_after=stale,
                restart=_thread_starter(WT._guard_loop, tname, (tick, WT._stop)),
                should_run=lambda: bool(getattr(WT, "_started", False)) and not WT._stop.is_set())
    if app_module is not None:
        A = app_module
        if hasattr(A, "_auto_review_bg"):
            reg("auto-review", label="options review reminder + journal check", interval=6 * 3600,
                stale_after=7 * 3600, restart=_thread_starter(A._auto_review_bg, "auto-review"),
                should_run=_has_beaten("auto-review"))      # watched once it has run (boot starts it)
        if hasattr(A, "_following_poller"):
            reg("follow-poller", label="followed-tickers price poller", interval=1, stale_after=15 * 60,
                restart=_thread_starter(A._following_poller, "follow-poller"),
                should_run=lambda: bool(getattr(A, "_follow_poller_started", False)))
    return names


# ── start ──────────────────────────────────────────────────────────────────────

_started = False
_start_lock = threading.Lock()
_sup_thread: threading.Thread | None = None


def _sup_alive() -> bool:
    return bool(_sup_thread is not None and _sup_thread.is_alive())


def _supervisor_loop() -> None:
    while True:
        _watchdog_ping()                     # first: proves this process is not wedged
        tick = 30.0
        try:
            sc = sup_cfg()
            tick = max(5.0, float(sc.get("tick_seconds", 30) or 30))
            _hook_desk()                      # the desk may start after us; hooking is idempotent
            check_once()
            _write_state(snapshot())
        except Exception as e:
            log.warning(f"[runtime] supervisor pass failed: {e}")
        time.sleep(tick)


def start(app_module=None) -> bool:
    """Hook the outside loops, start the fill-reconcile thread and the supervisor. Idempotent; never raises."""
    global _started, _sup_thread
    with _start_lock:
        if _started:
            return False
        _started = True
    try:
        _hook_news()
        _hook_desk()
        _start_reconcile_thread()
        register_defaults(app_module)
        _wd["enabled"] = _watchdog_wanted()
        if _wd["enabled"]:
            sd_notify("READY=1")
            log.info("[runtime] systemd watchdog requested: pinging it every supervisor pass")
        _sup_thread = threading.Thread(target=_supervisor_loop, daemon=True, name="mason-supervisor")
        _sup_thread.start()
        log.info(f"[runtime] supervisor started ({len(_jobs)} background jobs watched)")
        return True
    except Exception as e:
        log.error(f"[runtime] start failed: {e}")
        return False


def _reset_for_tests() -> None:
    global _started, _sup_thread
    with _lock:
        _jobs.clear()
    _rc_state.update({"last_run": None, "final_done": None, "fails": 0, "alerted": None, "last": None})
    _wd.update({"enabled": False, "pings": 0, "last_ping": None})
    HOT_CACHE.clear()
    _started, _sup_thread = False, None
