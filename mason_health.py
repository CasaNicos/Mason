"""
mason_health.py — Mason's Source-Health Monitor.

Part of the 24/7 Analyst Desk (ANALYST_DESK_PHASE1_PROMPT.md, Phase 1). An
extensible registry of cheap liveness checks over the data / broker / llm / feed
sources Mason depends on. A daemon thread runs each check on its own interval and,
on every state TRANSITION (up→down or down→up) only, drops a 'source_status'
notification into the durable inbox (mason_notifications). Steady state is silent,
so a persistently-down source alerts once, not every tick.

Hard constraints (Nico):
  * No quota burning: the Alpha Vantage check NEVER spends the 25/day quota (it
    probes reachability only). FMP / Telegram run on long intervals.
  * No GPU load: the ollama check hits GET /api/tags only — never a generation. The
    local_model_gpu check (2026-11-17) reads GET /api/ps and one nvidia-smi line — also
    never a generation.
  * No second Alpaca WebSocket: the alpaca_news_ws check reads liveness from
    mason_news_service.ws_status() (thread up + age of last frame).
  * A check_fn MUST NEVER raise: run_check wraps it; a raise is treated as "down"
    with the exception text as the detail.
  * The daemon thread never raises out: the loop is wrapped, sleeps, continues.

Testability: HealthMonitor takes injectable `notifier` and `clock`, and checks are
registered with plain callables — tests register fake check_fns (no network) and
drive transitions deterministically. The real, network-touching checks are wired
by register_default_checks(); start() launches the daemon behind the config flag.

Public surface:
    HealthMonitor(notifier=..., clock=...)
      .register(name, kind, check_fn, interval_seconds, note="", fields=())
      .run_check(name) -> dict
      .run_due() -> list[str]           # names actually run this pass
      .snapshot() -> dict               # {"sources":[...], "generated_at": ...}
    register_default_checks(monitor, config=None)
    check_local_model_gpu() -> dict      # is the local model on the graphics card (cached 20 s)
    start(config=None) -> HealthMonitor | None   # honours the config flag
    get_monitor() -> HealthMonitor | None        # the running singleton, if any
    snapshot() -> dict                            # convenience over the singleton
"""

from __future__ import annotations

import json as _json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import mason_notifications
from mason_redact import redact_text as _redact

log = logging.getLogger("mason.health")
BASE = Path(__file__).parent

# The yfinance liveness probe (and its HTTP stack) log a wall of DEBUG lines per
# call — under the service's DEBUG level that floods journalctl every check. Pin
# them to WARNING so a health sweep stays quiet (same tactic mason_news_service
# uses for the websockets client). Purely about log noise; no behaviour change.
for _noisy in ("yfinance", "urllib3", "peewee"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Down on these is a 'critical' notification; everything else is 'warn'. Losing
# the broker or the local brain is materially worse than a single news feed nap.
# local_model_gpu (2026-11-17): the local model off the graphics card runs ~10x slower.
_CRITICAL_SOURCES = {"alpaca_paper", "ollama_qwen", "local_model_gpu"}

# A probe returning {"ok": None} means "not configured" (an optional source with no key):
# its state is this value in snapshot() / /api/health (ok stays None), it is grey in the UI,
# and it never raises a notification. Distinct from a never-checked source (state None).
NOT_CONFIGURED = "not_configured"

# The states every probe shares. A probe may also report its own sub-states (local_model_gpu: on_gpu / idle /
# on_cpu / gpu_missing / down); for inbox notes those count as "up" when ok and "down" when not (_alert_class).
_STD_STATES = ("up", "down", "limit", NOT_CONFIGURED)


def _alert_class(state, ok) -> str:
    """What a state means for inbox notes. The shared states are their own class, so every existing probe behaves
    exactly as before (up→limit and limit→down still alert once each). A probe's own sub-state is "up" when ok and
    "down" when not: moving between two ok sub-states (on_gpu ↔ idle) or two not-ok ones (on_cpu → gpu_missing)
    sends nothing, so an outage is one DOWN note and one recovered note, never a flood."""
    s = str(state or "")
    if s in _STD_STATES:
        return s
    return "up" if ok else "down"

# Freshness thresholds for "read existing state" feed checks (seconds).
_WS_STALE_SECONDS = 3600        # alpaca news WS: no frame in 60 min while market moves
_EDGAR_STALE_SECONDS = 3600     # SEC 8-K poll watermark age
_GDELT_STALE_SECONDS = 3600     # GDELT poll age

_HTTP_TIMEOUT = 6               # short — a liveness probe, not a data pull


# ── config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return _json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _desk_cfg(config: dict | None = None) -> dict:
    cfg = config if config is not None else _load_config()
    return (cfg.get("analyst_desk") or {}) if isinstance(cfg, dict) else {}


# ── the monitor ──────────────────────────────────────────────────────────────

class HealthMonitor:
    def __init__(self, *, notifier=None, clock=time.time,
                 material_news_bridge: bool = False):
        self._checks: dict = {}     # name -> {kind, check_fn, interval, note}
        self._state: dict = {}      # name -> latest status dict
        self._held: dict = {}       # name -> alert class kept while a not-ok reading waits for a second one
        self._lock = threading.RLock()
        self._notifier = notifier if notifier is not None else mason_notifications.add_notification
        self._clock = clock
        self._material_news_bridge = bool(material_news_bridge)
        self._stop = threading.Event()
        self._thread = None

    # -- registry --
    def register(self, name: str, kind: str, check_fn, interval_seconds: float,
                 note: str = "", fields=()) -> None:
        """Register (or replace) a check. Adding a new source later is this one
        line. kind ∈ {data, broker, llm, feed, model}. check_fn() -> {ok, latency_ms,
        detail} (ok None = not configured, see NOT_CONFIGURED). interval_seconds
        staggers how often it runs. fields: extra keys of the check's result to carry
        into the snapshot (/api/health) next to the standard ones — local_model_gpu
        uses it for model / vram_mb / temp_c ...; the default () keeps a source's
        snapshot exactly the standard shape."""
        with self._lock:
            self._checks[name] = {
                "kind": kind,
                "check_fn": check_fn,
                "interval": max(5.0, float(interval_seconds)),
                "note": note or "",
                "fields": tuple(fields or ()),
            }

    def names(self) -> list:
        with self._lock:
            return list(self._checks.keys())

    # -- execution --
    def run_check(self, name: str) -> dict:
        """Run one check now, fold its result into state, and fire a notification
        on a state transition. Never raises."""
        with self._lock:
            spec = self._checks.get(name)
        if spec is None:
            return {}
        now = self._clock()
        started = time.time()
        fields = spec.get("fields") or ()
        extra = {}
        hold = False
        try:
            res = spec["check_fn"]() or {}
            if fields:
                extra = {k: res.get(k) for k in fields}
            # A probe may ask for a second bad reading before a DOWN note (local_model_gpu, review fix 2026-11-17).
            hold = res.get("alert_hold") is True
            if "ok" in res and res["ok"] is None and not res.get("state"):
                # R2-A 2026-11-16: ok=None is the probe saying "not configured" (congress_gov with no
                # API key): grey, never DOWN, never an alert. It used to fall into bool(None) → DOWN.
                ok, state = None, NOT_CONFIGURED
            else:
                ok = bool(res.get("ok"))
                # A check may report a distinct sub-state (e.g. FMP "limit" = quota
                # hit, reachable-but-capped). Absent one, derive it from ok so every
                # other probe keeps the plain up/down behaviour unchanged.
                state = str(res.get("state") or ("up" if ok else "down"))
            latency = res.get("latency_ms")
            detail = str(res.get("detail") or "")
        except Exception as e:               # a raise == down, with the text as detail
            ok = False
            state = "down"
            latency = round((time.time() - started) * 1000, 1)
            detail = f"check raised: {e}"
        # N11: the detail is exception / URL text and ends up in the stored status (the UI), in the notification
        # body and in logs. A requests error carries the full request URL, apikey= included. Strip credentials once,
        # here, before it goes anywhere.
        detail = _redact(detail)

        with self._lock:
            prev = self._state.get(name)
            # Baseline assumption is "up": a source that is not-up on its very
            # first check should alert once (that's the useful signal), then stay
            # silent. Transitions track the full state, so up→limit and limit→down
            # each alert once, not just up↔down.
            prev_state = prev.get("state", "up") if prev else "up"
            prev_ok = prev.get("ok", True) if prev else True
            first = prev is None
            transition = (state != prev_state)
            since = now if (first or transition) else prev.get("since", now)
            status = {
                "name": name,
                "kind": spec["kind"],
                "ok": ok,
                "state": state,
                "latency_ms": latency,
                "detail": detail,
                "note": spec["note"],
                "last_checked": now,
                "since": since,
            }
            for k in fields:                     # registered extra fields; never replace a standard key
                status.setdefault(k, extra.get(k))
            self._state[name] = status

        # Notes follow the alert class (_alert_class): for the shared states it is the state itself, so this is
        # the same rule as before for every existing probe; a probe's own sub-states only alert on ok ↔ not ok.
        cls = _alert_class(state, ok)
        with self._lock:
            held = self._held.pop(name, None)
            prev_cls = held if held is not None else _alert_class(prev_state, prev_ok)
            if hold and cls == "down" and prev_cls != "down":
                # The probe asked for one more bad reading first (alert_hold): the page already shows the bad state,
                # but the DOWN note waits for the next reading, and if that one is fine nothing is sent at all.
                # Probes that never set alert_hold never get here, so their notes are exactly as before.
                self._held[name] = prev_cls
                cls = prev_cls
        # "not configured" is not an outage: entering it is silent, and so is a newly configured
        # source coming up (nothing "recovered"). not_configured → down / limit still alerts.
        quiet = cls == NOT_CONFIGURED or (prev_cls == NOT_CONFIGURED and cls == "up")
        if cls != prev_cls and not quiet:
            self._notify_transition(name, spec["kind"], cls, detail)
        return status

    def _notify_transition(self, name: str, kind: str, state: str, detail: str) -> None:
        detail = _redact(detail)         # N11: also when called directly (defence in depth)
        try:
            if state == "up":
                self._notifier(
                    type="source_status", title=f"{name} recovered",
                    body=(f"{name} ({kind}) is back online. {detail}").strip(),
                    severity="info", ticker=None, ref_table="source", ref_id=name,
                    created_by="health", dedup_key=f"source:{name}:up",
                )
            elif state == "limit":
                # Reachable but rate/quota-limited — a soft, self-healing state, not
                # an outage. Warn (never critical) so it's visible without alarming.
                self._notifier(
                    type="source_status", title=f"{name} limit hit",
                    body=(f"{name} ({kind}) hit its API quota/rate limit. {detail}").strip(),
                    severity="warn", ticker=None, ref_table="source", ref_id=name,
                    created_by="health", dedup_key=f"source:{name}:limit",
                )
            else:
                sev = "critical" if name in _CRITICAL_SOURCES else "warn"
                self._notifier(
                    type="source_status", title=f"{name} is DOWN",
                    body=(f"{name} ({kind}) health check failing. {detail}").strip(),
                    severity=sev, ticker=None, ref_table="source", ref_id=name,
                    created_by="health", dedup_key=f"source:{name}:down",
                )
        except Exception as e:
            log.warning(f"[health] failed to record transition for {name}: {_redact(e)}")

    def run_due(self) -> list:
        """Run every check whose interval has elapsed since its last run. Returns
        the names actually run this pass."""
        now = self._clock()
        due = []
        with self._lock:
            for name, spec in self._checks.items():
                st = self._state.get(name)
                if st is None or (now - st.get("last_checked", 0)) >= spec["interval"]:
                    due.append(name)
        for name in due:
            self.run_check(name)
        return due

    # -- output --
    def snapshot(self) -> dict:
        with self._lock:
            sources = []
            for name in self._checks:
                st = self._state.get(name)
                spec = self._checks[name]
                fields = spec.get("fields") or ()
                if st is None:
                    entry = {
                        "name": name, "kind": spec["kind"], "ok": None,
                        "state": None,
                        "latency_ms": None, "detail": "not yet checked",
                        "note": spec["note"], "last_checked": None,
                        "last_checked_iso": None, "since": None, "since_iso": None,
                    }
                    for k in fields:
                        entry.setdefault(k, None)
                    sources.append(entry)
                    continue
                entry = {
                    "name": st["name"], "kind": st["kind"], "ok": st["ok"],
                    "state": st.get("state", "up" if st["ok"] else "down"),
                    "latency_ms": st["latency_ms"], "detail": st["detail"],
                    "note": st["note"],
                    "last_checked": st["last_checked"],
                    "last_checked_iso": _epoch_to_et_iso(st["last_checked"]),
                    "since": st["since"],
                    "since_iso": _epoch_to_et_iso(st["since"]),
                }
                for k in fields:                 # registered extra fields (local_model_gpu); standard keys win
                    entry.setdefault(k, st.get(k))
                sources.append(entry)
        # kind order then name, for a stable grid ("model" = local_model_gpu, right after the llm row)
        _order = {"broker": 0, "llm": 1, "model": 2, "data": 3, "feed": 4}
        sources.sort(key=lambda s: (_order.get(s["kind"], 9), s["name"]))
        return {"sources": sources, "generated_at": time.time()}

    # -- daemon --
    def _loop(self, tick_seconds: float) -> None:
        while not self._stop.is_set():
            _rt_beat("health-monitor")           # runtime supervisor heartbeat (mason_runtime)
            try:
                self.run_due()
                if self._material_news_bridge:
                    try:
                        mason_notifications.sync_material_news()
                    except Exception as e:
                        log.debug(f"[health] material-news bridge: {_redact(e)}")
            except Exception as e:               # never let the daemon die
                log.warning(f"[health] sweep error: {_redact(e)}")
            self._stop.wait(max(5.0, tick_seconds))

    def start_thread(self, tick_seconds: float = 15.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._tick_seconds = tick_seconds         # the supervisor restarts the loop with the same tick
        self._thread = threading.Thread(
            target=self._loop, args=(tick_seconds,), daemon=True, name="health-monitor")
        self._thread.start()
        log.info(f"[health] monitor started ({len(self._checks)} checks, "
                 f"tick {tick_seconds}s, material_bridge={self._material_news_bridge})")

    def stop(self) -> None:
        self._stop.set()


def _rt_beat(name: str) -> None:
    """Heartbeat for the runtime supervisor (mason_runtime). Never raises."""
    try:
        import mason_runtime
        mason_runtime.beat(name)
    except Exception:
        pass


def _epoch_to_et_iso(ts) -> str | None:
    """Epoch → ISO string in America/New_York (ET) for display, per Nico."""
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            pass
        return dt.isoformat()
    except Exception:
        return None


# ── real, injectable probes (cheap; short timeouts; read state where possible) ──
#
# Each returns {"ok": bool, "latency_ms": float|None, "detail": str}. They may
# raise; run_check() treats a raise as down. "ok": None means NOT CONFIGURED (an
# optional source with no key): state "not_configured", grey, no alert. Tests never
# call these — they register fake check_fns — so no network happens under test.

def _timed(fn):
    """Run fn(), return (result, latency_ms). fn returns the detail string."""
    t0 = time.time()
    detail = fn()
    return detail, round((time.time() - t0) * 1000, 1)


def check_yfinance() -> dict:
    import yfinance as yf
    def _do():
        t = yf.Ticker("SPY")
        px = None
        try:
            px = t.fast_info.get("last_price") or t.fast_info.get("lastPrice")
        except Exception:
            px = None
        if px is None:
            hist = t.history(period="1d")
            px = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if px is None:
            raise RuntimeError("no price returned")
        return f"SPY last ~{float(px):.2f}"
    detail, ms = _timed(_do)
    return {"ok": True, "latency_ms": ms, "detail": detail}


def check_fmp() -> dict:
    """Cheap quote-short/SPY probe.

    FMP's free tier has a hard daily request cap; once it's spent, every call —
    this probe included — comes back HTTP 429 "Limit Reach". That is NOT an
    outage: the key is valid and the service is reachable, it just won't serve
    more data until FMP's daily quota reset. So a 429 is surfaced as a distinct
    "limit" state (amber "Limit Hit" in the UI), NOT a red DOWN, and does not
    raise a critical alert. A genuine non-200 / unreachable is still down.

    Probes the /stable channel (symbol via ?symbol=), NOT legacy /api/v3/: FMP
    retired the v3 surface on 2025-08-31 and keys issued after that date 403
    "Legacy Endpoint" on every v3 call. /stable works for all key vintages, so
    this probe stays valid if the key is ever rotated. Independent of the config
    base_url (which several data modules still point at v3) by design."""
    import requests
    cfg = _load_config()
    import mason_secrets as _S    # B1b: env > secrets file > config
    key = _S.get("FMP_API_KEY", "", cfg=cfg)
    if not key:
        return {"ok": False, "state": "down", "latency_ms": None,
                "detail": "no FMP key in config"}
    t0 = time.time()
    try:
        r = requests.get("https://financialmodelingprep.com/stable/quote-short",
                         params={"symbol": "SPY", "apikey": key},
                         timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return {"ok": False, "state": "down",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "detail": f"unreachable: {_redact(e)}"}   # N11: the error text holds the URL, apikey= included
    ms = round((time.time() - t0) * 1000, 1)
    # 429, or FMP's "Limit Reach" error body (some tiers 200-wrap the error) → quota hit.
    if r.status_code == 429 or "limit reach" in (r.text or "").lower():
        return {"ok": False, "state": "limit", "latency_ms": ms,
                "detail": "quota/rate limit hit (HTTP 429 'Limit Reach') — FMP "
                          "free-tier daily cap spent; recovers at reset, not an outage"}
    if r.status_code != 200:
        return {"ok": False, "state": "down", "latency_ms": ms,
                "detail": f"HTTP {r.status_code}"}
    return {"ok": True, "state": "up", "latency_ms": ms, "detail": "quote-short/SPY 200"}


def check_alpha_vantage() -> dict:
    """Backup source, 25 req/day — DO NOT SPEND THE QUOTA. Reachability only: a
    HEAD to the base URL (no `function=`, so it doesn't count as a data call),
    plus a config-presence check. Low-confidence by design."""
    import requests
    cfg = _load_config()
    av = (cfg.get("apis") or {}).get("alpha_vantage") or {}
    base = av.get("base_url") or "https://www.alphavantage.co/query"
    import mason_secrets as _S    # B1b: presence only
    have_key = bool(_S.get("ALPHA_VANTAGE_API_KEY", "", cfg=cfg))
    def _do():
        r = requests.head(base, timeout=_HTTP_TIMEOUT, allow_redirects=True)
        reach = r.status_code < 500
        note = "reachable" if reach else f"HTTP {r.status_code}"
        if not reach:
            raise RuntimeError(note)
        return f"base URL {note}; key {'present' if have_key else 'MISSING'} (backup, low-confidence probe — quota not spent)"
    detail, ms = _timed(_do)
    return {"ok": True, "latency_ms": ms, "detail": detail}


def check_congress_gov() -> dict:
    """congress.gov API (api.data.gov key): one committee-meeting row. The key travels in the
    X-Api-Key header, never the URL, so it cannot land in an error string or a log line.
    No key = grey "not configured", not a red DOWN (the source is optional). 429 = limit."""
    import requests
    import mason_secrets as _S
    key = _S.get("DATA_GOV_API_KEY", "")
    if not key:
        return {"ok": None, "latency_ms": None,
                "detail": "no DATA_GOV_API_KEY in the secrets file (hearings calendar off)"}
    t0 = time.time()
    try:
        r = requests.get("https://api.congress.gov/v3/committee-meeting",
                         params={"limit": 1, "format": "json"},
                         headers={"X-Api-Key": key}, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        return {"ok": False, "state": "down",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "detail": f"unreachable: {_redact(e)}"}
    ms = round((time.time() - t0) * 1000, 1)
    if r.status_code == 429:
        return {"ok": True, "state": "limit", "latency_ms": ms,
                "detail": "hourly request limit reached (key valid)"}
    if r.status_code in (401, 403):
        return {"ok": False, "state": "down", "latency_ms": ms,
                "detail": f"key rejected (HTTP {r.status_code})"}
    if r.status_code != 200:
        return {"ok": False, "state": "down", "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
    left = r.headers.get("x-ratelimit-remaining")
    return {"ok": True, "latency_ms": ms,
            "detail": "committee-meeting 200" + (f"; {left} requests left this hour" if left else "")}


def check_alpaca_paper() -> dict:
    """Read-only GET /v2/account on the paper base with the paper creds."""
    import requests
    cfg = _load_config()
    pt = cfg.get("paper_trading") or {}
    import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
    key, secret = _S.alpaca_keys(cfg=cfg)
    base = (pt.get("base_url") or "https://paper-api.alpaca.markets/v2").rstrip("/")
    if not key or not secret:
        return {"ok": False, "latency_ms": None, "detail": "no paper creds in config"}
    def _do():
        r = requests.get(f"{base}/account",
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            acct = r.json()
            st = acct.get("status", "?")
        except Exception:
            st = "?"
        return f"account status {st}"
    detail, ms = _timed(_do)
    return {"ok": True, "latency_ms": ms, "detail": detail}


def check_alpaca_news_ws() -> dict:
    """Liveness of the RUNNING alpaca-news thread + age of last frame. Reads
    mason_news_service.ws_status(); opens NO second socket."""
    import mason_news_service as NS
    stt = NS.ws_status()
    if not stt.get("started"):
        # thread not started (no creds, or listener disabled) — not an outage per se
        return {"ok": False, "latency_ms": None,
                "detail": "news WS listener not started (no creds / disabled)"}
    last = stt.get("last_msg_ts") or 0
    if not last:
        return {"ok": True, "latency_ms": None,
                "detail": "connected; awaiting first frame"}
    age = time.time() - last
    if age > _WS_STALE_SECONDS:
        # 2026-11-15: at night and at weekends the stream is connected but simply has no headlines
        # for hours. Silence then is not a failure; only call it down when news should be flowing.
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
            busy = et.weekday() < 5 and 4 <= et.hour < 20
        except Exception:
            busy = True
        if not busy:
            return {"ok": True, "latency_ms": None,
                    "detail": f"connected; no headlines for {int(age)}s (quiet hours, normal)"}
        return {"ok": False, "latency_ms": None,
                "detail": f"last frame {int(age)}s ago (stale > {_WS_STALE_SECONDS}s)"}
    return {"ok": True, "latency_ms": None, "detail": f"last frame {int(age)}s ago"}


def check_ollama_qwen() -> dict:
    """GET /api/tags on the local Ollama — NEVER a generation (no GPU load)."""
    import requests
    cfg = _load_config()
    base = ((cfg.get("llm") or {}).get("local") or {}).get("base_url") \
        or "http://localhost:11434"
    base = base.rstrip("/")
    def _do():
        r = requests.get(f"{base}/api/tags", timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            models = [m.get("name", "") for m in (r.json().get("models") or [])]
            n = len(models)
        except Exception:
            n = 0
        return f"ollama up, {n} model(s) installed"      # /api/tags lists installed models, not loaded ones
    detail, ms = _timed(_do)
    return {"ok": True, "latency_ms": ms, "detail": detail}


def _source_row_age(match_source=None, match_feed_url=None):
    """(age_seconds|None, fail_streak|None, found) from news_sources for a feed —
    reads the watermark the news scanner already keeps; no new network call."""
    import mason_db
    conn = mason_db.get_connection()
    try:
        if match_feed_url:
            row = conn.execute("SELECT * FROM news_sources WHERE feed_url = ?",
                               (match_feed_url,)).fetchone()
        elif match_source:
            row = conn.execute("SELECT * FROM news_sources WHERE source = ? "
                               "ORDER BY updated_at DESC LIMIT 1",
                               (match_source,)).fetchone()
        else:
            row = None
        if row is None:
            return None, None, False
        stamp = row["updated_at"] or row["last_attempt"] or 0
        age = (time.time() - stamp) if stamp else None
        return age, (row["fail_streak"] or 0), True
    finally:
        conn.close()


def _source_last_error(feed_url):
    """The news_sources.last_error text for a feed, or None. Never raises."""
    if not feed_url:
        return None
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            row = conn.execute("SELECT last_error FROM news_sources WHERE feed_url = ?", (feed_url,)).fetchone()
            return (row["last_error"] if row else None) or None
        finally:
            conn.close()
    except Exception:
        return None


def _source_last_ok(feed_url):
    """news_sources.last_ok (epoch of the last good fetch) for a feed, or None. Never raises."""
    if not feed_url:
        return None
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            row = conn.execute("SELECT last_ok FROM news_sources WHERE feed_url = ?", (feed_url,)).fetchone()
            return float(row["last_ok"]) if row and row["last_ok"] else None
        finally:
            conn.close()
    except Exception:
        return None


def _edgar_resilient(ns) -> bool:
    try:
        return bool(ns is not None and ns._edgar_resilient())
    except Exception:
        return False


def _plain_sec_error(why) -> str:
    """'ReadTimeout' / 'HTTP 403' ... in words Nico reads in the health panel."""
    w = str(why or "").strip()
    if not w:
        return "reason not recorded"
    if "ConnectTimeout" in w or "ConnectionError" in w or "SSLError" in w or "no response" in w:
        return "could not reach SEC"
    if "Timeout" in w:
        return "SEC took too long to answer"
    if w == "HTTP 403":
        return "SEC refused the request (HTTP 403: blocked or the contact line was not accepted)"
    if w == "HTTP 429":
        return "SEC said too many requests (HTTP 429)"
    if w.startswith("HTTP 5"):
        return f"SEC server error ({w})"
    if w == "empty body":
        return "SEC answered with an empty page"
    return w


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{sec // 60} min"
    return f"{sec / 3600:.1f} h"


def _edgar_by_last_good(fails: int, good_age: float, why) -> dict:
    """SEC 8-K feed health from the last good read: fine while that is under _EDGAR_STALE_SECONDS
    old (a few slow answers from SEC are normal and the scanner retries on its own); down after."""
    tries = f"last {fails} {'try' if fails == 1 else 'tries'} failed ({_plain_sec_error(why)})"
    if good_age <= _EDGAR_STALE_SECONDS:
        if fails >= 3:                            # (review) not "working" after 3+ misses in a row
            return {"ok": True, "latency_ms": None,
                    "detail": f"struggling: last good read {_ago(good_age)} ago, {tries}; retrying on its own, "
                              f"shown red after {_ago(_EDGAR_STALE_SECONDS)} without a good read"}
        return {"ok": True, "latency_ms": None,
                "detail": f"working; last good read {_ago(good_age)} ago, {tries}; retrying on its own"}
    return {"ok": False, "latency_ms": None,
            "detail": f"no good read for {_ago(good_age)}; {tries}; Mason keeps retrying every few minutes"}


def check_sec_edgar() -> dict:
    """Last successful EDGAR 8-K poll age from news_sources (the scanner's own
    watermark) — no new SEC request, respects their UA/rate rules by not calling."""
    try:
        import mason_news_service as NS
        feed_url = getattr(NS, "_EDGAR_ATOM", None)
    except Exception:
        NS, feed_url = None, None
    age, fails, found = _source_row_age(match_feed_url=feed_url, match_source="SEC 8-K")
    if not found:
        return {"ok": True, "latency_ms": None, "detail": "no poll yet (awaiting first scan)"}
    if fails and feed_url and _edgar_resilient(NS):
        good = _source_last_ok(feed_url)
        if good:                                  # 2026-11-16: judge by the last GOOD read, in plain words
            return _edgar_by_last_good(fails, time.time() - good, _source_last_error(feed_url))
    if fails and fails >= 5:
        why = _source_last_error(feed_url)
        return {"ok": False, "latency_ms": None,
                "detail": f"feed resting after {fails} failures" + (f" (last: {why})" if why else "")}
    if age is not None and age > _EDGAR_STALE_SECONDS:
        return {"ok": False, "latency_ms": None,
                "detail": f"last poll {int(age)}s ago (stale > {_EDGAR_STALE_SECONDS}s)"}
    return {"ok": True, "latency_ms": None,
            "detail": (f"last poll {int(age)}s ago" if age is not None else "polled")}


def check_gdelt() -> dict:
    """Age of the last GDELT pull, read from mason_news_service._gdelt_last (its
    own module watermark). No new GDELT request (respects ~1 req/5s)."""
    try:
        import mason_news_service as NS
        last = float(getattr(NS, "_gdelt_last", 0.0) or 0.0)
    except Exception:
        last = 0.0
    if not last:
        return {"ok": True, "latency_ms": None, "detail": "no pull yet (awaiting first poll)"}
    age = time.time() - last
    if age > _GDELT_STALE_SECONDS:
        return {"ok": False, "latency_ms": None,
                "detail": f"last pull {int(age)}s ago (stale > {_GDELT_STALE_SECONDS}s)"}
    return {"ok": True, "latency_ms": None, "detail": f"last pull {int(age)}s ago"}


def check_telegram() -> dict:
    """Reachability of one public Telegram web-preview channel. Low frequency."""
    import requests
    slug = "WatcherGuru"
    def _do():
        r = requests.get(f"https://t.me/s/{slug}", timeout=_HTTP_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (Mason health probe)"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return f"t.me/s/{slug} 200"
    detail, ms = _timed(_do)
    return {"ok": True, "latency_ms": ms, "detail": detail}


def check_calendar_fed() -> dict:
    """Events calendar: the Federal Reserve feed (FOMC dates, speeches). Reads the feed's own
    health record in mason.db; no network."""
    import mason_calendar
    return mason_calendar.feed_health(["fed"])


def check_calendar_econ() -> dict:
    """Events calendar: the economic release schedule (FRED / BEA / Census). No network."""
    import mason_calendar
    return mason_calendar.feed_health(["econ"])


def check_watch_tape_premarket() -> dict:
    """Watchlist pre-market minute bars (Yahoo): not ok when, well into the pre-market, every
    poll came back empty for every symbol (HYGIENE_REVIEW F3). Reads the watch tape's own
    tally; no network. The thresholds live in watchlist.extended (empty_alert_after_et)."""
    import mason_watch_tape
    return mason_watch_tape.premarket_health()


def check_option_snaps() -> dict:
    """Option price snapshots (mason_option_snaps): not ok only when a snapshot time today passed with nothing stored
    because the pass failed. Quiet outside market hours. Reads mason.db only; no network."""
    import mason_option_snaps
    return mason_option_snaps.health()


# name -> (kind, check_fn, interval_seconds, note). Adding a source is one line.
_DEFAULT_CHECKS = [
    ("alpaca_paper",   "broker", check_alpaca_paper,   300,  "read-only GET /v2/account"),
    ("ollama_qwen",    "llm",    check_ollama_qwen,     120,  "GET /api/tags only — no generation"),
    ("yfinance",       "data",   check_yfinance,        300,  "one SPY quote"),
    ("fmp",            "data",   check_fmp,             900,  "cheap quote-short/SPY; respect tier"),
    ("alpha_vantage",  "data",   check_alpha_vantage,   1800, "backup, low-confidence probe — no quota spent"),
    ("congress_gov",   "data",   check_congress_gov,    1800, "one committee-meeting row; key in header"),
    ("alpaca_news_ws", "feed",   check_alpaca_news_ws,  60,   "WS thread liveness + last-frame age"),
    ("gdelt",          "feed",   check_gdelt,           600,  "last-pull age from news service"),
    ("sec_edgar",      "feed",   check_sec_edgar,       600,  "last-poll watermark from news_sources"),
    ("telegram",       "feed",   check_telegram,        1800, "t.me/s preview reachability"),
    ("calendar_fed",   "feed",   check_calendar_fed,    3600, "events calendar: last good read of the Fed's calendar"),
    ("calendar_econ",  "feed",   check_calendar_econ,   3600, "events calendar: last good read of the release schedules"),
    ("watch_tape_premarket", "feed", check_watch_tape_premarket, 300,
     "pre-market minute bars: all symbols empty after 06:00 ET = not ok"),
    ("option_snaps",   "feed",   check_option_snaps,    300,
     "option price snapshots (09:45/12:30/15:45 ET): a failed snapshot time = not ok"),
]


# ── Overnight and pre-market read (audit R6, mason_overnight). One self-contained additive block, appended to the list
# above so other pieces can add their own rows without touching these lines.
def check_overnight_tape() -> dict:
    """Did the last scheduled overnight snapshot run and bring an S&P 500 quote. Nights with nothing scheduled,
    weekends and holidays are quiet, not down. Reads mason.db only; no network."""
    try:
        import mason_overnight
    except Exception as e:                     # (review fix R6-6) a module that cannot load is only news when it is on
        try:
            on = ((_json.loads(Path(__file__).with_name("mason.config.json").read_text(encoding="utf-8"))
                   .get("overnight") or {}).get("enabled") is True)
        except Exception:
            on = False
        return ({"ok": False, "latency_ms": None, "detail": f"overnight module did not load: {type(e).__name__}"} if on
                else {"ok": True, "latency_ms": None, "detail": "switched off (overnight.enabled is false)"})
    return mason_overnight.feed_health()


_DEFAULT_CHECKS.append(("overnight_tape", "feed", check_overnight_tape, 600,
                        "overnight futures, VIX, dollar, rates and pre-market: last scheduled read (quiet nights are normal)"))


# ── Is the local model really on the graphics card? (LOCAL_MODEL_STUDY S2, 2026-11-17) ──────────────────────────────
# The card fell off the bus twice (kernel Xid 79: 16 Nov 19:50, 17 Nov 14:07). Ollama quietly moved the model to the
# processor (about 10x slower) for 17.5 hours while ollama_qwen stayed green: GET /api/tags only lists the INSTALLED
# models. This check asks Ollama what is LOADED and where (GET /api/ps: size_vram = bytes on the card) and asks the
# driver whether it still sees the card (one nvidia-smi line). Neither runs the model. States, first match wins:
#   down         Ollama does not answer /api/ps                                       ok False
#   gpu_missing  nvidia-smi cannot see the card, whatever is loaded                   ok False
#   on_cpu       a model is loaded with nothing of it on the card (size_vram 0)       ok False
#   on_gpu       the model is loaded with some or all of it on the card               ok True
#   idle         nothing is loaded and the card is visible (models unload when unused) ok True
# Inbox notes: gpu_missing and down must be read twice in a row before the DOWN note (alert_hold, see
# _GPU_CONFIRM_READINGS); on_cpu alerts on the first reading. A card at 83 °C or more for 2 minutes or longer is mentioned in the detail; ok does not change. One self-contained
# block, appended like the overnight one. Switch: analyst_desk.health_monitor.local_model_gpu (default on; false =
# not registered at the next start).

GPU_FIELDS = ("model", "vram_mb", "size_mb", "gpu_name", "temp_c", "power_w", "power_limit_w", "checked_at")
_GPU_CACHE_SECONDS = 20.0        # the whole reading is reused this long, so any caller can poll it
_GPU_PS_TIMEOUT = (1.0, 2.0)     # connect, read: Ollama is local and answers /api/ps in a few ms
_GPU_SMI_TIMEOUT = 2.0
_GPU_HOT_C = 83                  # the card's own target temperature (nvidia-smi -q: target 83 C, slowdown 90 C)
_GPU_HOT_SECONDS = 120           # "sustained": this long at or above _GPU_HOT_C
_GPU_HOT_GAP_SECONDS = 180       # readings further apart than this do not make one hot spell
# Review fix 2026-11-17: a card that cannot be read, or Ollama not answering, must be seen on 2 readings in a row
# (about 30 s apart) before the DOWN note, so one slow nvidia-smi or an Ollama restart sends nothing. The page shows
# the state at once. on_cpu (a model loaded with nothing of it on the card) is a clear answer and alerts at once.
_GPU_CONFIRM_READINGS = 2
_GPU_CONFIRM_STATES = ("gpu_missing", "down")
_SMI_ARGS = ["--query-gpu=name,temperature.gpu,power.draw,power.limit", "--format=csv,noheader,nounits"]
# nvidia-smi text that means the card is gone even when the program itself exits 0
_SMI_LOST = ("gpu is lost", "unknown error", "requires reset", "no devices were found", "unable to determine")
_SLOWER = "about 10× slower"

_gpu_lock = threading.Lock()
_gpu_cache = {"at": None, "result": None, "state": None, "bad": 0}   # bad = not-ok readings in a row
_gpu_hot = {"since": None, "last": None}
_smi_hung = {"proc": None, "since": None}


def _gpu_reset_for_tests() -> None:
    with _gpu_lock:
        _gpu_cache.update(at=None, result=None, state=None, bad=0)
        _gpu_hot.update(since=None, last=None)
        _smi_hung.update(proc=None, since=None)


def _http_get_ps(base: str, timeout):
    """GET <ollama>/api/ps → (HTTP status, parsed JSON or None). Raises when Ollama cannot be reached.
    (Tests replace this function.)"""
    import requests
    r = requests.get(f"{base}/api/ps", timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


def _read_ollama_ps(base: str) -> dict:
    """{"answered", "models", "latency_ms", "why"} from one /api/ps. Never raises."""
    try:
        import requests  # noqa: F401  (loaded before the clock starts, so latency_ms is Ollama's time, not an import)
    except Exception:
        pass
    t0 = time.time()
    try:
        code, body = _http_get_ps(base, _GPU_PS_TIMEOUT)
    except Exception as e:
        return {"answered": False, "models": None, "latency_ms": None, "why": type(e).__name__}
    ms = int(round((time.time() - t0) * 1000))
    if code != 200:
        return {"answered": False, "models": None, "latency_ms": None, "why": f"HTTP {code}"}
    models = (body.get("models") or []) if isinstance(body, dict) else None
    if not isinstance(models, list):
        return {"answered": False, "models": None, "latency_ms": None, "why": "unreadable reply"}
    return {"answered": True, "models": [m for m in models if isinstance(m, dict)], "latency_ms": ms, "why": None}


def _run_nvidia_smi(timeout: float = _GPU_SMI_TIMEOUT):
    """One nvidia-smi reading → (exit code, stdout text, why). Exit code None = no usable answer, and why says
    "missing" (not installed), "timeout", "stuck <seconds>" or "error <name>". Never waits much past `timeout`: on a
    timeout the program is killed and given 0.5 s to end; if it still has not (a card that fell off the bus can leave
    it stuck inside the driver) it is left behind, and no new copy starts until that one ends, so a dead card can
    neither hang the health monitor nor pile up processes. (Tests replace this function.)"""
    hung = _smi_hung["proc"]
    if hung is not None:
        if hung.poll() is None:
            return None, "", f"stuck {int(max(0.0, time.time() - (_smi_hung['since'] or time.time())))}"
        _smi_hung.update(proc=None, since=None)
    try:
        p = subprocess.Popen([shutil.which("nvidia-smi") or "nvidia-smi", *_SMI_ARGS], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    except FileNotFoundError:
        return None, "", "missing"
    except Exception as e:
        return None, "", f"error {type(e).__name__}"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=0.5)
        except Exception:
            _smi_hung.update(proc=p, since=time.time())
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass
        return None, "", "timeout"
    except Exception as e:
        try:
            p.kill()                      # never leave a child behind on an unexpected error
            p.wait(timeout=0.5)
        except Exception:
            pass
        return None, "", f"error {type(e).__name__}"
    text = (out or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        msg = " ".join(((out or b"") + b" " + (err or b"")).decode("utf-8", "replace").split())
        return p.returncode, text, f"exit {p.returncode}: {msg[:160]}"
    return 0, text, None


def _smi_num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None                       # "[N/A]", "[Not Supported]" ...


def _parse_smi(rc, out, why) -> dict:
    """An nvidia-smi answer → {"visible", "gpu_name", "temp_c", "power_w", "power_limit_w", "why"}. The card counts
    as visible only on exit code 0 with a well-formed first line that does not say the card is lost."""
    gone = {"visible": False, "gpu_name": None, "temp_c": None, "power_w": None, "power_limit_w": None,
            "why": why or (f"exit {rc}" if rc not in (0, None) else None)}
    if rc != 0 or why:
        return gone
    line = next((ln.strip() for ln in str(out or "").splitlines() if ln.strip()), "")
    if not line:
        return dict(gone, why="no output")
    if any(s in line.lower() for s in _SMI_LOST):
        return dict(gone, why=f"card error: {line[:120]}")
    parts = [p.strip() for p in line.rsplit(",", 3)]
    if len(parts) != 4 or not parts[0] or parts[0].startswith("["):
        return dict(gone, why=f"unexpected output: {line[:120]}")
    temp, pw, pl = _smi_num(parts[1]), _smi_num(parts[2]), _smi_num(parts[3])
    return {"visible": True, "gpu_name": parts[0][:80],
            "temp_c": int(round(temp)) if temp is not None else None,
            "power_w": round(pw, 2) if pw is not None else None,
            "power_limit_w": round(pl, 2) if pl is not None else None, "why": None}


def _model_key(n) -> str:
    n = str(n or "").strip().lower()
    return n if (":" in n or not n) else n + ":latest"       # Ollama lists "phi4-mini" as "phi4-mini:latest"


def _pick_model(models: list, workhorse: str, fallback: str):
    """The loaded model this check reports on → (entry, role): Mason's workhorse first, then its fallback, then
    whatever else is loaded ("other"). (None, None) when nothing is loaded."""
    for want, role in ((workhorse, "workhorse"), (fallback, "fallback")):
        w = _model_key(want)
        for m in models:
            if w and w in (_model_key(m.get("name")), _model_key(m.get("model"))):
                return m, role
    return (models[0], "other") if models else (None, None)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _mb(n):
    """Bytes → whole MB (1 MB = 1,000,000 bytes, the units `ollama ps` shows)."""
    return int(round(n / 1e6)) if n is not None else None


def _gb(n) -> str:
    return f"{n / 1e9:.1f}"


def _stuck_for(why: str) -> str:
    try:
        return _ago(int(str(why).split()[1]))
    except Exception:
        return "a while"


def _hot_sentence(temp_c, now: float) -> str:
    """'' or the hot-card sentence. Tracks one continuous spell of readings at or above _GPU_HOT_C: a cooler reading,
    no reading (card not visible) or a gap over _GPU_HOT_GAP_SECONDS starts it over. Mentioned once the spell has
    lasted _GPU_HOT_SECONDS. Changes no state and no ok."""
    h = _gpu_hot
    hot = temp_c is not None and temp_c >= _GPU_HOT_C
    if not hot or (h["last"] is not None and now - h["last"] > _GPU_HOT_GAP_SECONDS):
        h["since"] = None
    h["last"] = now
    if not hot:
        return ""
    if h["since"] is None:
        h["since"] = now
    spell = now - h["since"]
    if spell < _GPU_HOT_SECONDS:
        return ""
    return (f" The card has been running hot ({temp_c} °C for {_ago(spell)}) — still working, but worth easing off "
            f"heavy use until it cools.")


def _gpu_verdict(ps: dict, smi: dict, workhorse: str, fallback: str, now: float) -> dict:
    """The check's answer from one /api/ps reading and one nvidia-smi reading (state table above)."""
    card = bool(smi.get("visible"))
    temp = smi.get("temp_c") if card else None
    deg = f"{temp} °C" if temp is not None else ""
    hot = _hot_sentence(temp, now)
    res = {"ok": False, "state": "down", "latency_ms": None, "detail": "",
           "model": None, "vram_mb": None, "size_mb": None,
           "gpu_name": smi.get("gpu_name") if card else None, "temp_c": temp,
           "power_w": smi.get("power_w") if card else None,
           "power_limit_w": smi.get("power_limit_w") if card else None,
           "checked_at": _epoch_to_et_iso(now)}
    if not ps.get("answered"):
        why = str(ps.get("why") or "")
        what = (f"answered with an error ({why})" if why.startswith("HTTP ") else
                "sent a reply Mason could not read" if why == "unreadable reply" else "is not answering")
        if card:
            res["detail"] = (f"Ollama, the program that runs the local model, {what}. "
                             f"Restarting Ollama or the computer usually fixes it." + hot)
        else:
            res["detail"] = (f"Ollama, the program that runs the local model, {what}, and the graphics card cannot "
                             f"be seen either. A reboot usually brings both back.")
        return res
    res["latency_ms"] = ps.get("latency_ms")
    m, role = _pick_model(ps.get("models") or [], workhorse, fallback)
    vram = size = None
    if m is not None:
        vram, size = _int_or_none(m.get("size_vram")), _int_or_none(m.get("size"))
        name = str(m.get("name") or m.get("model") or "")
        res.update(model=(name[:-7] if name.endswith(":latest") else name) or None, vram_mb=_mb(vram),
                   size_mb=_mb(size))
    who = ("the local model" if role in (None, "workhorse") else
           f"the backup local model ({res['model']})" if role == "fallback" else
           f"the model {res['model']}" if res["model"] else "a model")
    who_cap = who[0].upper() + who[1:]
    on_cpu_now = m is not None and not vram
    if not card:
        why = str(smi.get("why") or "")
        res["state"] = "gpu_missing"
        if why == "missing":
            res["detail"] = ("Mason cannot check the graphics card: its status tool (nvidia-smi) is not installed "
                             "or cannot be found.")
        elif "version mismatch" in why.lower():
            # Review fix 2026-11-17: nvidia-smi "Driver/library version mismatch" (exit 18) = the driver package was
            # updated (unattended-upgrades is on here) and the running driver is still the old one. The card has not
            # dropped out; new model loads cannot use it until a reboot.
            res["detail"] = (f"The graphics card driver was updated and the computer has not been restarted since, "
                             f"so {who} {'is running' if on_cpu_now else 'will run'} on the processor"
                             f"{'' if on_cpu_now else ' the next time it loads'} — {_SLOWER}. A reboot finishes "
                             f"the update.")
        else:
            gone = (f"has stopped answering (its status check has been stuck for {_stuck_for(why)})"
                    if why.startswith("stuck") else
                    f"has stopped answering (its status check did not finish within {_GPU_SMI_TIMEOUT:g} seconds)"
                    if why == "timeout" else "has dropped out (the system cannot see it)")
            res["detail"] = (f"The graphics card {gone}, so {who} {'is running' if on_cpu_now else 'will run'} on "
                             f"the processor — {_SLOWER}. A reboot usually brings the card back.")
        return res
    if m is None:
        res.update(ok=True, state="idle",
                   detail=("The local model is not loaded right now (it unloads when unused); the graphics card is "
                           f"ready{f' ({deg})' if deg else ''}." + hot))
        return res
    if on_cpu_now:
        res.update(state="on_cpu",
                   detail=(f"{who_cap} is running on the processor — {_SLOWER}. The graphics card is visible but "
                           f"Ollama is not using it; restarting Ollama or the computer usually fixes it." + hot))
        return res
    if size and vram < size:
        ctx = _int_or_none(m.get("context_length")) or 0
        why = ("that is normal for chat's bigger memory window" if ctx > 16384
               else "it is a little slower than usual")
        on, of = _gb(vram), _gb(size)
        if on == of:                      # a small split would read "3.3 of 3.3 GB": show it to 0.01 GB
            on, of = f"{vram / 1e9:.2f}", f"{size / 1e9:.2f}"
        if vram * 2 < size:               # review fix: less than half on the card is not "a little slower"
            detail = (f"{who_cap} is running mostly on the processor, with only {on} of {of} GB on the graphics "
                      f"card{f' ({deg})' if deg else ''}; it is much slower than usual.")
        else:
            detail = (f"{who_cap} is running on the graphics card, with part of it on the processor "
                      f"({on} of {of} GB on the card{f', {deg}' if deg else ''}); {why}.")
    else:
        detail = f"{who_cap} is running on the graphics card ({_gb(vram)} GB in use{f', {deg}' if deg else ''})."
    res.update(ok=True, state="on_gpu", detail=detail + hot)
    return res


def _gpu_settings() -> tuple:
    """(Ollama base URL, workhorse, fallback) from the llm.local block, the same keys mason_llm reads."""
    cfg = _load_config()
    local = ((cfg.get("llm") or {}).get("local") or {}) if isinstance(cfg, dict) else {}
    return (str(local.get("base_url") or "http://localhost:11434").rstrip("/"),
            str(local.get("workhorse") or "qwen3.5:4b"), str(local.get("fallback") or "phi4-mini"))


def check_local_model_gpu(max_age: float = _GPU_CACHE_SECONDS) -> dict:
    """Is the local model really on the graphics card? One GET /api/ps (what Ollama has loaded, and how much of it
    is on the card) and one nvidia-smi line (can the system still see the card; temperature, power). Never a
    generation, so no load on the card. The whole reading is cached max_age seconds, so the health monitor, the
    page and anyone else can ask as often as they like; a fresh reading takes a few ms, and at most about 6 s when
    Ollama or the card hangs. Returns the /api/health contract: ok, state, detail, latency_ms + GPU_FIELDS."""
    with _gpu_lock:
        now = time.time()
        at, cached = _gpu_cache["at"], _gpu_cache["result"]
        if cached is not None and at is not None and 0 <= now - at < max_age:
            return dict(cached)
        base, workhorse, fallback = _gpu_settings()
        ps = _read_ollama_ps(base)
        smi = _parse_smi(*_run_nvidia_smi(_GPU_SMI_TIMEOUT))
        res = _gpu_verdict(ps, smi, workhorse, fallback, now)
        if res["state"] != _gpu_cache["state"]:       # the raw reasons go to the log once per change, not per reading
            (log.info if res["ok"] else log.warning)(_redact(
                f"[health] local_model_gpu -> {res['state']} (ollama: {ps.get('why') or 'answered'}; "
                f"card: {smi.get('why') or 'visible'})"))
        bad = 0 if res["ok"] else int(_gpu_cache.get("bad") or 0) + 1
        if bad and bad < _GPU_CONFIRM_READINGS and res["state"] in _GPU_CONFIRM_STATES:
            res["alert_hold"] = True                   # the monitor waits for the next reading before a DOWN note
        _gpu_cache.update(at=now, result=dict(res), state=res["state"], bad=bad)
        return res


_DEFAULT_CHECKS.append(("local_model_gpu", "model", check_local_model_gpu, 20,
                        "Ollama /api/ps + one nvidia-smi line about every 30 s — never runs the model"))
_CHECK_FIELDS = {"local_model_gpu": GPU_FIELDS}       # extra snapshot fields per source (see register(fields=))


def _gpu_check_on(config: dict | None = None) -> bool:
    """analyst_desk.health_monitor.local_model_gpu: anything but an explicit false means on."""
    hm = _desk_cfg(config).get("health_monitor") or {}
    return not (isinstance(hm, dict) and hm.get("local_model_gpu", True) is False)


# ── Drug decision dates read from stored news (news phase 6c, mason_biocal). One self-contained block: the line is
# registered only while calendar.feeds.biocal is on (read at start), so installing adds nothing to /api/health.
def check_calendar_biocal() -> dict:
    """Events calendar: the drug-decision-date feed (reads stored news only). No network."""
    import mason_calendar
    return mason_calendar.feed_health(["biocal"])


_DEFAULT_CHECKS.append(("calendar_biocal", "feed", check_calendar_biocal, 3600,
                        "events calendar: drug decision dates (PDUFA) read from stored news"))


def _biocal_on(config: dict | None = None) -> bool:
    cfg = config if config is not None else _load_config()
    try:
        return (((cfg.get("calendar") or {}).get("feeds") or {}).get("biocal") is True) if isinstance(cfg, dict) else False
    except Exception:
        return False


def register_default_checks(monitor: HealthMonitor, config: dict | None = None) -> None:
    gpu_on = _gpu_check_on(config)
    biocal_on = _biocal_on(config)
    for name, kind, fn, interval, note in _DEFAULT_CHECKS:
        if name == "local_model_gpu" and not gpu_on:
            continue
        if name == "calendar_biocal" and not biocal_on:
            continue
        monitor.register(name, kind, fn, interval, note, fields=_CHECK_FIELDS.get(name, ()))


# ── module singleton + start (behind the config flag) ──────────────────────────

_monitor: HealthMonitor | None = None
_start_lock = threading.Lock()


def get_monitor() -> HealthMonitor | None:
    return _monitor


def snapshot() -> dict:
    m = _monitor
    if m is None:
        return {"sources": [], "generated_at": time.time(),
                "detail": "health monitor not started"}
    return m.snapshot()


def start(config: dict | None = None) -> HealthMonitor | None:
    """Start the singleton health monitor behind the analyst_desk config flag.
    Returns the monitor if started, None if disabled or already running. Reads
    config defensively: a missing block means safe defaults (enabled)."""
    global _monitor
    desk = _desk_cfg(config)
    hm_cfg = desk.get("health_monitor") or {}
    if hm_cfg.get("enabled", True) is False:
        log.info("[health] monitor disabled by config (analyst_desk.health_monitor.enabled=false)")
        return None
    tick = float(hm_cfg.get("default_interval_seconds", 120) or 120)
    # The daemon's tick is how often it *checks for due* checks; keep it well under
    # the per-check intervals so staggering is honoured. Cap at 30s.
    tick = min(30.0, max(10.0, tick))
    bridge = bool((desk.get("notifications") or {}).get("material_news_bridge", True))
    with _start_lock:
        if _monitor is not None and _monitor._thread and _monitor._thread.is_alive():
            return None
        _monitor = HealthMonitor(material_news_bridge=bridge)
        register_default_checks(_monitor, config)
        _monitor.start_thread(tick_seconds=tick)
    return _monitor


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    m = HealthMonitor()
    register_default_checks(m)
    print("Running one sweep of all checks (this DOES hit the network)...\n")
    for name in m.names():
        st = m.run_check(name)
        dot = "🟢" if st.get("ok") else ("🔴" if st.get("ok") is False else "⚪")
        lat = f"{st.get('latency_ms')}ms" if st.get("latency_ms") is not None else "-"
        print(f"  {dot} {st['name']:<16} {st['kind']:<7} {lat:>9}  {st['detail']}")
