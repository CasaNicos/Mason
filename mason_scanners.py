"""
mason_scanners.py — Robinhood scanner intake backend (RH Expansion S2-T7).

Spec: docs/scanners/SCANNER_INTAKE_DESIGN.md (the only source of truth), §8 steps 1, 3, 4, 5:
  1. Poller      get_scans discovery (keyed by scan_id, never title) + run_scan per scan in parallel on
                 the "live" rate lane. 60 s regular hours / 300 s extended / off overnight, weekends and
                 holidays (mason_director.is_trading_day). Exponential backoff on error, capped at 10 min.
                 Token failure → status "Robinhood: reconnect" + ONE notification per outage.
     Debounce    confirmed after `enter_polls` (2) consecutive polls; exits after `exit_polls` (5)
                 consecutive absent polls; a same-day re-entry reuses the row (reentries += 1).
     Storage     scanner_scans / scanner_hits (mason_db, created by S1-T0). Raw RH rows (all cells) are
                 stored as JSON. A poll's writes are one transaction; in-memory streaks only advance
                 after the commit, so a failed RH call or DB error never corrupts state.
     API         /api/scanners, /api/scanners/hits, /api/scanners/scorecard — DB reads only, JSON only.
  3. qwen triage only NEW confirmed hits, ≤ triage.max_per_hour, qwen via mason_llm (think:false comes
                 from the llm config; temperature 0.1). score ≥ propose_min_score → watchlist PROPOSAL
                 (origin="qwen"). Quality Pullback is triaged once daily at 15:30 ET.
                 SC-D5: a hit on a ticker Nico holds at RH → one advice-only notification per ticker per
                 scan per day. Advice text only; no recommendation rows, no event pipeline, no orders.
  4. Grading     mason_price_cache.fwd_return(ticker, session_date, sessions=1|5, entry_px=first_price)
                 only; scorecards via mason_kb_priors.upsert_prior(family "scanner").
  5. Opus        opus_section() → plain text "SCANNER HITS TODAY" block (T8 wires it in).

Decisions binding here: SC-D1 observe-first (no trade path), SC-D2 qwen only proposes, SC-D3 no wake
path from scanners, SC-D5 advice only.

Storage notes:
  * Audit columns (finalization D5, RULINGS 8): created_date (UTC ISO, _iso) / created_by (CREATED_BY) /
    record_deleted are stamped on every INSERT; every reader filters COALESCE(record_deleted,0)=0 except
    three deliberate, commented reads (PK lookup before insert, the qwen hourly budget, status() row
    count); every UPDATE … WHERE id=? also requires the row not to be deleted (no resurrection).
  * A soft-deleted scanner_scans row is not polled (_deleted_scan_ids); its stored hits stay as they are.
  * RH "% …" cells are often RATIOS ("-0.0507" = -5.07 %, RULINGS 27 G-S1): stored raw, scaled ×100 only
    where shown (Opus line, qwen prompt, UI).
  * scanner_hits.grades_json is Mason's per-hit JSON blob: {"triage": {direction, why_now, failed},
    "held": [...], "advice": str, "advice_sent": bool, "1": {grade}, "5": {grade}}.
  * last_row_json carries the raw RH row plus one Mason key "_rank" (position in the last poll).

Config: mason.config.json → "scanners" (all false), merged over _DEFAULTS; robinhood.enabled is the
master switch. Public functions never raise. Hooks for tests: _rh, _llm, _holdings, _notify,
_watch_add, _busy, _now.
"""

import re
import json
import math
import hashlib
import logging
import threading
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import mason_db

try:
    from flask import Blueprint, jsonify, request
except Exception:                    # pragma: no cover - flask is a runtime dependency
    Blueprint = None

log = logging.getLogger("mason.scanners")

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
CREATED_BY = "mason_scanners"

QUALITY_PULLBACK_ID = "b955071d-f993-4bca-8726-980b7bf875fc"
_MAX_ROWS = 200                       # rows per scan per poll (bounded; truncation counted in status)
_MAX_SCANS = 20                       # scans per poll (bounded)
_BACKOFF_CAP_S = 600
_DISCOVER_EVERY_S = 900
_HIST_DAYS_MAX = 365
_GRADE_LOOKBACK_DAYS = 45

_DEFAULTS = {
    "enabled": False,
    "poll_seconds": {"rth": 60, "extended": 300},
    "debounce": {"enter_polls": 2, "exit_polls": 5},
    "triage": {"enabled": False, "max_per_hour": 20, "propose_min_score": 0.70,
               "daily_scan_ids": [QUALITY_PULLBACK_ID], "daily_at_et": "15:30"},
    "opus_section": {"enabled": False, "max_items": 8, "tokens": 600},
    "grading": {"enabled": False, "horizons_sessions": [1, 5]},
    "event_wake": {"enabled": False},
    "trade_path": {"enabled": False},
}

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_TAG_RE = re.compile(r"<[^>]{0,500}>")
_PRICE_NAMES = ("price", "last price", "last", "last trade price", "last sale", "mark", "price (last)",
                "current price")
_CHG_NAMES = ("% change", "percent change", "change %", "% chg", "change (%)", "day % change",
              "pct change", "% change today", "today's % change", "%change")


# ══ hooks (tests replace these) ══════════════════════════════════════════════
def _now() -> datetime:
    return datetime.now(UTC)


def _rh(tool: str, args: dict | None = None):
    import mason_robinhood
    return mason_robinhood._call(tool, args or {}, lane="live")


def _llm(system: str, user: str, **opts) -> str:
    import mason_llm
    return mason_llm.complete(system, user, intent="chore", **opts)


def _busy() -> bool:
    try:
        import mason_llm
        return bool(mason_llm.interactive_busy())
    except Exception:
        return False


def _holdings() -> list:
    import mason_robinhood
    return mason_robinhood.positions()


def _notify(type_, title, **kw):
    import mason_notifications
    return mason_notifications.add_notification(type_, title, **kw)


def _watch_add(ticker, **kw):
    import mason_watchlist
    return mason_watchlist.add(ticker, **kw)


# ══ config ═══════════════════════════════════════════════════════════════════
_cfg_cache = {"mtime": None, "path": None, "data": {}}


def _config() -> dict:
    """mtime-cached config. A torn/partial read (bad JSON, non-object) keeps the LAST GOOD config
    and retries on the next call, so a write in progress can never widen scope (DEBUG_T7 D1)."""
    try:
        mt = Path(CONFIG).stat().st_mtime
        if _cfg_cache["mtime"] != mt or _cfg_cache["path"] != str(CONFIG):
            data = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config is not a JSON object")
            _cfg_cache["data"] = data
            _cfg_cache["mtime"], _cfg_cache["path"] = mt, str(CONFIG)
        return _cfg_cache["data"] or {}
    except Exception:
        return _cfg_cache["data"] or {}


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if str(k).startswith("_") or v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _cfg() -> dict:
    try:
        return _merge(_DEFAULTS, _config().get("scanners") or {})
    except Exception:
        return deepcopy(_DEFAULTS)


def enabled() -> bool:
    """scanners.enabled AND the Robinhood master switch (contracts §7)."""
    try:
        c = _config()
        return bool((c.get("robinhood") or {}).get("enabled")) and bool(_cfg().get("enabled"))
    except Exception:
        return False


def _sub(name: str) -> bool:
    try:
        return enabled() and bool((_cfg().get(name) or {}).get("enabled"))
    except Exception:
        return False


_DEFAULT_EXCLUDE = ("Household",)
_bad_scope_seen = set()


def _exclude_accounts() -> set:
    """Normalized account labels excluded from personal advice (contracts §2.8). Parsed by the shared
    mason_robinhood.parse_exclude_accounts (RULINGS 1/2): only an explicit clean list (e.g. []) can
    narrow it; a bare string, a non-list, unusable entries, or a non-object rh_expansion /
    personal_scope → the usable labels plus "household" and ONE logged error per distinct bad value
    (status()["errors"])."""
    try:
        import mason_robinhood
        rx = _config().get("rh_expansion")
        ps = rx.get("personal_scope") if isinstance(rx, dict) else None
        err = None
        if rx is not None and not isinstance(rx, dict):
            out, err = set(mason_robinhood.parse_exclude_accounts(None)[0]), "rh_expansion is not an object"
        elif ps is not None and not isinstance(ps, dict):
            out, err = set(mason_robinhood.parse_exclude_accounts(None)[0]), "personal_scope is not an object"
        else:
            ex, err = mason_robinhood.parse_exclude_accounts(
                (ps or {}).get("exclude_accounts"), present=isinstance(ps, dict) and "exclude_accounts" in ps)
            out = set(ex)
        if err:
            key = repr(ps if isinstance(rx, dict) else rx)[:200]
            if key not in _bad_scope_seen and len(_bad_scope_seen) < 50:
                _bad_scope_seen.add(key)
                _err("config", ValueError(f"malformed rh_expansion.personal_scope.exclude_accounts ({err}); "
                                          f"excluding default {_DEFAULT_EXCLUDE}"))
        return out
    except Exception:
        return {a.casefold() for a in _DEFAULT_EXCLUDE}


def _norm_label(s) -> str:
    try:
        import mason_robinhood
        return mason_robinhood.norm_account_label(s)
    except Exception:
        return str(s or "").strip().casefold()


def _min_n() -> int:
    try:
        return int(((_config().get("rh_expansion") or {}).get("priors") or {}).get("min_n") or 10)
    except Exception:
        return 10


# ══ state ════════════════════════════════════════════════════════════════════
_state_lock = threading.RLock()
_poll_lock = threading.Lock()
_triage_lock = threading.Lock()


def _blank_state() -> dict:
    return {"streaks": {}, "session": None, "scans": {}, "last_discover": None,
            "last_poll": None, "backoff_s": 0, "consecutive_errors": 0,
            "state": "idle", "message": "", "connected": None, "disconnect_notified": False,
            "errors": [], "truncated": 0, "workers": 0, "threads": 0,
            "daily_done": None, "last_run": {"poll": None, "triage": None, "grade": None}}


_st = _blank_state()


def _reset_state():
    """Test helper / process restart: forget in-memory streaks (the DB is the record)."""
    global _st
    with _state_lock:
        _st = _blank_state()


def _threads_started() -> int:
    return _st["threads"]


def _last_poll_workers() -> int:
    return _st["workers"]


def _err(where: str, e) -> str:
    msg = f"{_iso(_now())} {where}: {type(e).__name__}: {str(e)[:200]}"
    with _state_lock:
        _st["errors"] = (_st["errors"] + [msg])[-5:]
    log.warning(f"[scanners] {msg}")
    return msg


# ══ time helpers ═════════════════════════════════════════════════════════════
def _aware(now) -> datetime:
    if now is None:
        now = _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return now


def _et(now=None) -> datetime:
    return _aware(now).astimezone(ET)


def _iso(dt) -> str:
    return _aware(dt).astimezone(UTC).isoformat(timespec="seconds")


def _is_trading_day(et_dt: datetime) -> bool:
    try:
        import mason_director
        return bool(mason_director.is_trading_day(et_dt))
    except Exception:
        return et_dt.weekday() < 5


def poll_window(now=None):
    """'rth' 09:30–16:00 ET, 'extended' 04:00–09:30 and 16:00–20:00 ET, else None (off)."""
    try:
        t = _et(now)
        if not _is_trading_day(t):
            return None
        m = t.hour * 60 + t.minute
        if 570 <= m < 960:
            return "rth"
        if 240 <= m < 570 or 960 <= m < 1200:
            return "extended"
        return None
    except Exception:
        return None


def poll_interval(now=None):
    win = poll_window(now)
    if win is None:
        return None
    try:
        return int((_cfg().get("poll_seconds") or {}).get(win) or _DEFAULTS["poll_seconds"][win])
    except Exception:
        return _DEFAULTS["poll_seconds"][win]


# ══ parsing helpers ══════════════════════════════════════════════════════════
def _clean(s, limit=None) -> str:
    """Plain text: tags removed, any leftover '<' neutralised, whitespace collapsed."""
    if s is None:
        return ""
    s = _TAG_RE.sub("", str(s)).replace("<", "‹")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def _safe(o):
    if isinstance(o, str):
        return _clean(o) if ("<" in o) else o
    if isinstance(o, dict):
        return {(_safe(k) if isinstance(k, str) else k): _safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_safe(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def _num(v):
    """'3.4%' → 3.4, '-1,234.5' → -1234.5, '12.1M' → 1.21e7, '$871.20' → 871.2, {'value':..}."""
    try:
        if isinstance(v, dict):
            for k in ("value", "raw", "number", "display_value", "formatted_value"):
                if k in v:
                    return _num(v[k])
            return None
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, (int, float)):
            f = float(v)
            return f if math.isfinite(f) else None
        s = str(v).strip().replace("−", "-").replace(",", "").replace("$", "").replace("%", "")
        s = s.replace("+", "").strip()
        mult = 1.0
        if s and s[-1].upper() in "KMBT":
            mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1].upper()]
            s = s[:-1]
        f = float(s) * mult
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _cells(raw) -> dict:
    """Ordered {column name: value} from a raw RH row (list-of-cells or dict cells)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    c = raw.get("cells")
    if c is None:
        c = raw.get("columns") if isinstance(raw.get("columns"), (list, dict)) else raw.get("values")
    if isinstance(c, dict):
        for k, v in c.items():
            if k is not None:
                out[str(k)] = v
    elif isinstance(c, list):
        for cell in c:
            if not isinstance(cell, dict):
                continue
            name = next((cell.get(k) for k in ("column", "display_name", "name", "label", "key")
                         if cell.get(k) is not None), None)
            if name is None:
                continue
            val = next((cell[k] for k in ("value", "display_value", "formatted_value", "text")
                        if k in cell), None)
            out[str(name)] = val
    return out


def _cell_by(raw, names):
    for k, v in _cells(raw).items():
        if k.strip().lower() in names:
            n = _num(v)
            if n is not None:
                return n
    return None


def _pct_change(raw):
    """Raw % Change cell value as stored (a ratio for RH's "% …" columns). Sign/peak logic only."""
    return _cell_by(raw, _CHG_NAMES)


_RATIO_NUM_RE = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def _ratio_num(v):
    """The ONE numeric parse shared with the UI (scan.js parseCellNum; parity table
    ui/test/widgets/scannerCellParity.json): trim, U+2212 minus → "-", one leading "+" dropped, ","
    thousands separators dropped, then a plain decimal/exponent number. No K/M/B/T suffix, no "$",
    no NaN/Infinity → None. Real numbers (not bool) pass when finite."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if not isinstance(v, str):
        return None
    t = v.strip().replace("\u2212", "-")
    if t.startswith("+"):
        t = t[1:]
    t = t.replace(",", "")
    if not _RATIO_NUM_RE.match(t):
        return None
    try:
        f = float(t)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _fixed(x: float, dp: int) -> str:
    """JS Number.prototype.toFixed semantics (exact binary value, ties away from zero) with -0 → 0, so
    the server and the UI print identical text and never a negative zero."""
    from decimal import Decimal, ROUND_HALF_UP
    q = Decimal(x).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return f"{q:.{dp}f}"


def _ratio_pct(column, v):
    """RULINGS 27 G-S1: a cell is a RATIO when its column name (after leading spaces) starts with "%"
    and its value carries no "%" sign (RH sends "% Change": "-0.0507…" = -5.07 %). → the value in
    percent points, else None. Applied only where a value is SHOWN; stored cells are never rewritten.
    Same rule as the UI (ui/src/widgets/scannerShared/scan.js ratioPct)."""
    try:
        if not str(column or "").lstrip().startswith("%"):
            return None
        if isinstance(v, str) and "%" in v:
            return None
        n = _ratio_num(v)
        return n * 100.0 if n is not None else None
    except Exception:
        return None


def _pct_points(raw):
    """% Change in percent points for display (ratio cells ×100, percent strings as they are)."""
    for k, v in _cells(raw).items():
        if k.strip().lower() in _CHG_NAMES:
            n = _ratio_pct(k, v)
            if n is None:
                n = _num(v)
            if n is not None:
                return n
    return None


_YMD_FLOAT_RE = re.compile(r"^\s*\d(?:\.\d+)?[eE]\+?0?7\s*$|^\s*\d{8}(?:\.0+)?\s*$")


def _ymd_date(column, v):
    """'Earnings date': '2.0261019e+07' (yyyymmdd as a float) → '2026-10-19'; else None."""
    try:
        if "date" not in str(column or "").lower() or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            f = float(v)
        elif isinstance(v, str) and _YMD_FLOAT_RE.match(v):
            f = float(v)
        else:
            return None
        if not math.isfinite(f) or f != int(f):
            return None
        d = str(int(f))
        return datetime.strptime(d, "%Y%m%d").date().isoformat() if len(d) == 8 else None
    except Exception:
        return None


def _cell_text(column, v) -> str:
    """One RH cell as readable text for qwen: ratios as percent (2 dp, no negative zero), yyyymmdd
    floats as ISO dates, anything else (an invalid date included) as its raw text. Parity with the UI is
    pinned by ui/test/widgets/scannerCellParity.json."""
    r = _ratio_pct(column, v)
    if r is not None:
        return _fixed(r, 2) + "%"
    d = _ymd_date(column, v)
    if d is not None:
        return d
    return "" if v is None else str(v)


def _price(raw):
    p = _cell_by(raw, _PRICE_NAMES)
    if p is None and isinstance(raw, dict):
        p = _num(raw.get("price") or raw.get("last_trade_price"))
    return p


def _ticker(raw):
    if not isinstance(raw, dict):
        return None
    t = raw.get("ticker") or raw.get("symbol")
    if not isinstance(t, str):
        return None
    t = t.strip().upper()
    return t if _TICKER_RE.match(t) else None


_ROW_KEYS = ("results", "rows", "instruments", "items", "data")
_WRAP_KEYS = ("data", "result", "response", "payload")


def _rows_of(data, _depth=0):
    """The instrument rows of a run_scan answer, or None if the answer is malformed/an error.
    One or two wrapper levels ({"data": {"rows": [...]}}) are unwrapped; deeper is malformed."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    if data.get("error") or data.get("errors"):
        return None
    for k in _ROW_KEYS:
        if k in data and isinstance(data[k], list):
            return data[k]
    for k in _WRAP_KEYS:
        if isinstance(data.get(k), dict) and _depth < 2:
            return _rows_of(data[k], _depth + 1)
    return None


def _hit_id(scan_id, ticker, session_date) -> str:
    return hashlib.sha1(f"{scan_id}|{ticker}|{session_date}".encode()).hexdigest()[:16]


def _jl(s, default=None):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except Exception:
        return default


_AUTH_RE = re.compile(
    r"unauthori[sz]ed|invalid[_ ]token|token (?:has )?(?:expired|invalid|revoked)|expired token|"
    r"\bre-?login\b|\blog ?in again\b|not logged in|needs? (?:to )?log ?in|reauthori[sz]", re.I)


def _is_auth_error(e) -> bool:
    """Only real auth failures mark Robinhood disconnected (not e.g. 'rate-limit token bucket')."""
    if type(e).__name__ == "RHNeedsLogin":
        return True
    return bool(_AUTH_RE.search(str(e)))


# ══ discovery ════════════════════════════════════════════════════════════════
def _scan_meta(s: dict):
    if not isinstance(s, dict):
        return None
    sid = s.get("scan_id") or s.get("id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    cols = []
    for i, c in enumerate(s.get("columns") or []):      # G-S2: visible columns only, in RH "order"
        if isinstance(c, dict) and c.get("visible") is False:
            continue
        name = c.get("display_name") or c.get("name") if isinstance(c, dict) else c
        if isinstance(name, str) and name:
            o = c.get("order") if isinstance(c, dict) else None
            o = o if isinstance(o, (int, float)) and not isinstance(o, bool) and math.isfinite(o) else math.inf
            cols.append((o, i, name))
    cols = [n for _, _, n in sorted(cols)]
    return {"scan_id": sid.strip(), "title": _clean(s.get("title") or "", 120),
            "columns": cols, "sorting": s.get("sorting") or s.get("sort")}


def _discover(now, force=False):
    """(scans, error|None). Upserts scanner_scans. Falls back to known scans on error."""
    now = _aware(now)
    with _state_lock:
        last = _st["last_discover"]
        known = dict(_st["scans"])
    if not force and known and last and (now - last).total_seconds() < _DISCOVER_EVERY_S:
        return list(known.values()), None
    try:
        data = _rh("get_scans")
        lst = data.get("scans") or data.get("results") if isinstance(data, dict) else data
        if not isinstance(lst, list):
            raise ValueError("get_scans: malformed answer")
        scans = [m for m in (_scan_meta(s) for s in lst[:_MAX_SCANS]) if m]
        conn = mason_db.get_connection()
        try:
            with conn:
                for m in scans:
                    # the upsert never touches created_* / record_deleted (no resurrection, RULINGS 8)
                    conn.execute(
                        "INSERT INTO scanner_scans (scan_id, title, columns_json, sorting, created_date, created_by, "
                        "record_deleted) VALUES (?,?,?,?,?,?,0) "
                        "ON CONFLICT(scan_id) DO UPDATE SET title=excluded.title, "
                        "columns_json=excluded.columns_json, sorting=excluded.sorting",
                        (m["scan_id"], m["title"], json.dumps(m["columns"]), json.dumps(m["sorting"]),
                         _iso(now), CREATED_BY))
        finally:
            conn.close()
        with _state_lock:
            _st["scans"] = {m["scan_id"]: m for m in scans}
            _st["last_discover"] = now
        return scans, None
    except Exception as e:
        msg = _err("get_scans", e)
        if not known:
            known = {m["scan_id"]: m for m in _db_scans()}
        return list(known.values()), (msg, e)


def _db_scans() -> list:
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM scanner_scans WHERE COALESCE(record_deleted,0)=0 "
                                "ORDER BY title").fetchall()
        finally:
            conn.close()
        return [{"scan_id": r["scan_id"], "title": r["title"] or "",
                 "columns": _jl(r["columns_json"], []), "sorting": _jl(r["sorting"])} for r in rows]
    except Exception:
        return []


def discover(force=False, now=None) -> list:
    """Run get_scans now (or when stale) and return [{scan_id, title, columns, sorting}]."""
    if not enabled():
        return []
    try:
        scans, _ = _discover(now, force=force)
        return scans
    except Exception as e:
        _err("discover", e)
        return []


# ══ poller ═══════════════════════════════════════════════════════════════════
def _fetch(sid):
    try:
        data = _rh("run_scan", {"scan_id": sid})
        rows = _rows_of(data)
        if rows is None:
            err = (data.get("error") if isinstance(data, dict) else None) or "malformed run_scan answer"
            raise ValueError(f"run_scan: {str(err)[:120]}")
        return sid, data, rows, None
    except Exception as e:
        return sid, None, None, e


def _write_poll(conn, ops, session, now_iso, scan_status):
    """All DB effects of one poll (caller wraps in one transaction)."""
    conn.execute("UPDATE scanner_hits SET exited_at = last_seen WHERE exited_at IS NULL AND session_date < ? "
                 "AND COALESCE(record_deleted,0)=0 AND scan_id NOT IN "
                 "(SELECT scan_id FROM scanner_scans WHERE COALESCE(record_deleted,0)<>0)", (session,))
    for sid, (ok, err, cols) in scan_status.items():
        if ok:
            if cols:
                conn.execute("UPDATE scanner_scans SET last_run=?, last_error=NULL, columns_json=? WHERE scan_id=?",
                             (now_iso, json.dumps(cols), sid))
            else:
                conn.execute("UPDATE scanner_scans SET last_run=?, last_error=NULL WHERE scan_id=?", (now_iso, sid))
        else:
            conn.execute("UPDATE scanner_scans SET last_error=? WHERE scan_id=?", (str(err)[:300], sid))
    for op in ops:
        kind, sid, t = op["kind"], op["scan_id"], op["ticker"]
        hid = _hit_id(sid, t, session)
        if kind == "exit":
            conn.execute("UPDATE scanner_hits SET exited_at=? WHERE id=? AND exited_at IS NULL "
                         "AND COALESCE(record_deleted,0)=0", (now_iso, hid))
            continue
        raw = op["raw"]
        raw_txt = json.dumps(raw)
        # Deliberately UNFILTERED (RULINGS 8): primary-key identity lookup before the INSERT below; filtering
        # it would turn a soft-deleted hit into a PK-conflict error. A deleted row is left untouched.
        cur = conn.execute("SELECT exited_at, peak_row_json, record_deleted FROM scanner_hits WHERE id=?",
                           (hid,)).fetchone()
        if cur is None:
            if kind != "confirm":
                continue
            conn.execute(
                "INSERT INTO scanner_hits (id, scan_id, ticker, session_date, first_seen, last_seen, exited_at, "
                "reentries, first_row_json, peak_row_json, last_row_json, first_price, grades_json, "
                "created_date, created_by, record_deleted) "
                "VALUES (?,?,?,?,?,?,NULL,0,?,?,?,?,?,?,?,0)",
                (hid, sid, t, session, op["first_seen"], now_iso, raw_txt, raw_txt, raw_txt, _price(raw), "{}",
                 now_iso, CREATED_BY))
            continue
        if cur["record_deleted"]:
            continue                                    # soft-deleted: never revived, never updated
        peak = cur["peak_row_json"]
        new_chg, old_chg = _pct_change(raw), _pct_change(_jl(peak, {}))
        if new_chg is not None and (old_chg is None or abs(new_chg) > abs(old_chg)):
            peak = raw_txt
        if kind == "confirm" and cur["exited_at"] is not None:
            conn.execute("UPDATE scanner_hits SET exited_at=NULL, reentries=COALESCE(reentries,0)+1, last_seen=?, "
                         "last_row_json=?, peak_row_json=? WHERE id=? AND COALESCE(record_deleted,0)=0",
                         (now_iso, raw_txt, peak, hid))
        else:
            conn.execute("UPDATE scanner_hits SET last_seen=?, last_row_json=?, peak_row_json=? WHERE id=? "
                         "AND COALESCE(record_deleted,0)=0", (now_iso, raw_txt, peak, hid))


def _deleted_scan_ids() -> set:
    """scan_ids whose scanner_scans row is soft-deleted. They are not polled (no run_scan, no new hits,
    no exits/day-roll on their stored hits). On a DB error → empty set (polling continues as before)."""
    try:
        conn = mason_db.get_connection()
        try:
            return {r[0] for r in conn.execute(
                "SELECT scan_id FROM scanner_scans WHERE COALESCE(record_deleted,0)<>0").fetchall()}
        finally:
            conn.close()
    except Exception as e:
        _err("deleted scans read", e)
        return set()


def _seed_streaks(session) -> dict:
    """Confirmed streaks for this session's open rows (process restart / new session)."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute("SELECT scan_id, ticker FROM scanner_hits WHERE session_date=? AND exited_at IS NULL "
                            "AND COALESCE(record_deleted,0)=0", (session,)).fetchall()
    finally:
        conn.close()
    return {(r["scan_id"], r["ticker"]): {"present": 0, "absent": 0, "confirmed": True, "first_seen": None}
            for r in rows}


def poll_once(now=None) -> dict:
    """One poll of every known scan. Returns {confirmed, exited, errors, scans}; {} when disabled."""
    if not enabled():
        return {}
    if not _poll_lock.acquire(blocking=False):
        return {"confirmed": [], "exited": [], "errors": ["poll already running"], "scans": 0}
    try:
        return _poll(now)
    except Exception as e:
        return {"confirmed": [], "exited": [], "errors": [_err("poll", e)], "scans": 0}
    finally:
        _poll_lock.release()


def _poll(now) -> dict:
    now = _aware(now)
    now_iso = _iso(now)
    session = _et(now).strftime("%Y-%m-%d")
    cfg = _cfg()
    enter_n = max(1, int((cfg.get("debounce") or {}).get("enter_polls") or 2))
    exit_n = max(1, int((cfg.get("debounce") or {}).get("exit_polls") or 5))
    errors, auth_fail = [], False

    scans, derr = _discover(now)
    if derr:
        errors.append(derr[0])
        auth_fail = auth_fail or _is_auth_error(derr[1])
    deleted = _deleted_scan_ids()
    scans = [s for s in scans if s["scan_id"] not in deleted][:_MAX_SCANS]   # soft-deleted scans: no run_scan

    results = []
    if scans:
        workers = min(8, len(scans))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanpoll") as ex:
            results = list(ex.map(_fetch, [s["scan_id"] for s in scans]))
        with _state_lock:
            _st["workers"] = workers

    with _state_lock:
        same_session = _st["session"] == session
        streaks = deepcopy(_st["streaks"]) if same_session else None
    if streaks is None:
        streaks = _seed_streaks(session)

    ops, confirmed, exited, scan_status, truncated = [], [], [], {}, 0
    for sid, data, rows, e in results:
        if e is not None:
            errors.append(_err(f"run_scan {sid[:8]}", e))
            auth_fail = auth_fail or _is_auth_error(e)
            scan_status[sid] = (False, f"{type(e).__name__}: {str(e)[:200]}", None)
            continue
        if len(rows) > _MAX_ROWS:
            truncated += 1
            rows = rows[:_MAX_ROWS]
        cols = []
        if isinstance(data, dict) and isinstance(data.get("columns"), list):
            cols = [c.get("display_name") or c.get("name") if isinstance(c, dict) else c
                    for c in data["columns"]]
            cols = [c for c in cols if isinstance(c, str) and c]
        present = {}
        for raw in rows:
            t = _ticker(raw)
            if t and t not in present:
                r = dict(raw)
                r["_rank"] = len(present) + 1
                present[t] = r
        if not cols and present:
            known_cols = (next((s for s in scans if s["scan_id"] == sid), {}) or {}).get("columns")
            if not known_cols:
                cols = list(_cells(next(iter(present.values()))).keys())
        scan_status[sid] = (True, None, cols)
        for t, raw in present.items():
            k = (sid, t)
            s = streaks.get(k) or {"present": 0, "absent": 0, "confirmed": False, "first_seen": None}
            s["present"] += 1
            s["absent"] = 0
            if s["present"] == 1 and not s["confirmed"]:
                s["first_seen"] = now_iso
            if s["confirmed"]:
                ops.append({"kind": "update", "scan_id": sid, "ticker": t, "raw": raw})
            elif s["present"] >= enter_n:
                s["confirmed"] = True
                ops.append({"kind": "confirm", "scan_id": sid, "ticker": t, "raw": raw,
                            "first_seen": s["first_seen"] or now_iso})
                confirmed.append((sid, t))
            streaks[k] = s
        for k in [k for k in streaks if k[0] == sid and k[1] not in present]:
            s = streaks[k]
            if not s["confirmed"]:
                del streaks[k]
                continue
            s["present"] = 0
            s["absent"] += 1
            if s["absent"] >= exit_n:
                ops.append({"kind": "exit", "scan_id": sid, "ticker": k[1]})
                exited.append(k)
                del streaks[k]

    committed = False
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                _write_poll(conn, ops, session, now_iso, scan_status)
            committed = True
        finally:
            conn.close()
    except Exception as e:
        errors.append(_err("db write", e))
        confirmed, exited = [], []

    with _state_lock:
        if committed:
            _st["streaks"] = streaks
            _st["session"] = session
        _st["last_poll"] = now
        _st["last_run"]["poll"] = now_iso
        _st["truncated"] += truncated
        if errors:
            _st["consecutive_errors"] += 1
            base = poll_interval(now) or _DEFAULTS["poll_seconds"]["rth"]
            _st["backoff_s"] = int(min(base * 2 ** (_st["consecutive_errors"] - 1), _BACKOFF_CAP_S))
        else:
            _st["consecutive_errors"] = 0
            _st["backoff_s"] = 0
        if auth_fail:
            _st["state"], _st["message"], _st["connected"] = "disconnected", "Robinhood: reconnect", False
            send = not _st["disconnect_notified"]
            _st["disconnect_notified"] = True
        else:
            send = False
            if errors:
                _st["state"], _st["message"] = "error", f"{len(errors)} error(s) in last poll; backing off"
            else:
                _st["state"], _st["message"], _st["connected"] = "ok", "", True
                _st["disconnect_notified"] = False
    if send:
        try:
            _notify("source_status", "Robinhood: reconnect",
                    body="Scanner polling is paused because the Robinhood token failed. "
                         "Run `python3 mason_robinhood.py login` on the server.",
                    severity="warn", created_by=CREATED_BY, dedup_key="scanners_rh_disconnected")
        except Exception as e:
            _err("notify disconnect", e)
    return {"confirmed": confirmed, "exited": exited, "errors": errors, "scans": len(scans),
            "committed": committed}


def tick(now=None) -> dict:
    """Scheduler entry: polls when the schedule window, interval and backoff say so."""
    if not enabled():
        return {"ran": False, "reason": "disabled"}
    try:
        now = _aware(now)
        interval = poll_interval(now)
        if interval is None:
            return {"ran": False, "reason": "off_hours"}
        with _state_lock:
            last, backoff = _st["last_poll"], _st["backoff_s"]
        wait = backoff or interval
        if last is not None and (now - last).total_seconds() < wait:
            return {"ran": False, "reason": "not_due"}
        r = poll_once(now)
        return {"ran": True, **r}
    except Exception as e:
        return {"ran": False, "reason": _err("tick", e)}


# ══ qwen triage ══════════════════════════════════════════════════════════════
_TRIAGE_SYSTEM = (
    "You triage one stock-scanner hit for a trader. Reply with ONE JSON object and nothing else:\n"
    '{"score": <0..1, how worth a closer look this hit is right now>, '
    '"direction": "long" | "short" | "none", '
    '"note": "<= 200 chars, the evidence in plain words>", '
    '"why_now": "<= 120 chars, what makes it timely, or empty">}\n'
    "Judge from the scanner cells and the context given. A high score needs a concrete reason "
    "(a catalyst, confirming volume, a held or watched name). No catalyst and mixed cells → low score. "
    "This is observation only: never suggest an order.")

_NEUTRAL = {"score": 0.0, "direction": "none", "note": "triage failed", "why_now": "", "failed": True}


def _coerce_score(v):
    if isinstance(v, bool) or v is None:
        return None
    pct = False
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("%"):
            pct, s = True, s[:-1].strip()
        try:
            v = float(s)
        except ValueError:
            return None
    if not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    if pct or 1.0 < f <= 100.0:
        f = f / 100.0
    if f > 1.0:
        return None
    return max(0.0, min(1.0, f))


def _direction(v) -> str:
    d = str(v or "").strip().lower()
    if d in ("long", "bullish", "buy", "up"):
        return "long"
    if d in ("short", "bearish", "sell", "down"):
        return "short"
    return "none"


def parse_triage(text) -> dict:
    """qwen's reply → {score, direction, note, why_now, failed}. Garbage → neutral 'triage failed'."""
    try:
        if not isinstance(text, str) or "{" not in text:
            return dict(_NEUTRAL)
        dec = json.JSONDecoder()
        obj = None
        for m in re.finditer(r"\{", text):
            try:
                cand, _ = dec.raw_decode(text, m.start())
            except ValueError:
                continue
            if isinstance(cand, dict):
                obj = cand
                break
        if obj is None or "score" not in obj:
            return dict(_NEUTRAL)
        score = _coerce_score(obj.get("score"))
        if score is None:
            return dict(_NEUTRAL)
        return {"score": round(score, 4), "direction": _direction(obj.get("direction")),
                "note": _clean(obj.get("note"), 200), "why_now": _clean(obj.get("why_now"), 200),
                "failed": False}
    except Exception:
        return dict(_NEUTRAL)


def _ctx_lines(conn, ticker, now, skip_stories=False) -> list:
    """skip_stories (2026-11-19, scanners.triage.skip_trade_stories exactly true; off = today's text): leave out the
    theses mason_rh_history writes from Nico's answered Robinhood trade stories. Each is about a trade that is
    already CLOSED ("RKLB 2026-08-19 trade (Nico's story)", status 'confirmed' = his verdict was "right"), so it is no
    open thesis; his own long-term theses and the director's stay."""
    out = []
    pat = f'%"{ticker}"%'
    try:
        cutoff = _aware(now).timestamp() - 48 * 3600
        news = conn.execute(
            "SELECT headline FROM news_events WHERE COALESCE(is_deleted,0)=0 AND tickers LIKE ? "
            "AND COALESCE(published_at, ingested_at) >= ? ORDER BY COALESCE(published_at, ingested_at) DESC LIMIT 5",
            (pat, cutoff)).fetchall()
        out.append("News (48h): " + ("; ".join(_clean(r["headline"], 140) for r in news) if news else "none"))
    except Exception:
        out.append("News (48h): unavailable")
    try:
        th = conn.execute(
            "SELECT title, thesis, direction FROM theses WHERE record_deleted=0 AND tickers LIKE ? "
            "AND status IN ('proposed','watching','confirmed') "
            + ("AND COALESCE(created_by,'') <> 'mason_rh_history' " if skip_stories else "") + "LIMIT 3",
            (pat,)).fetchall()
        out.append("Open theses: " + ("; ".join(f"{_clean(r['title'], 80)} ({r['direction'] or 'n/a'}): "
                                                f"{_clean(r['thesis'], 120)}" for r in th) if th else "none"))
    except Exception:
        out.append("Open theses: unavailable")
    try:
        d = conn.execute("SELECT built_at FROM dossiers WHERE subject=? AND record_deleted=0 "
                         "ORDER BY built_at DESC LIMIT 1", (ticker,)).fetchone()
        out.append("Dossier: " + (datetime.fromtimestamp(d["built_at"], ET).strftime("on file, built %Y-%m-%d")
                                  if d and d["built_at"] else "none"))
    except Exception:
        out.append("Dossier: unavailable")
    try:
        w = conn.execute("SELECT status FROM watchlist WHERE ticker=? AND record_deleted=0 "
                         "AND status IN ('active','proposed') LIMIT 1", (ticker,)).fetchone()
        out.append("Watchlist: " + (w["status"] if w else "not watched"))
    except Exception:
        out.append("Watchlist: unavailable")
    return out


def _bl(label):
    """N6b: a label as a MODEL sees it - the ••NNNN mask dropped (mason_robinhood.brain_label); the stored and UI labels
    keep it. Empty/None stay as they are. If the shared function cannot be loaded the label is replaced, never leaked."""
    if label is None or label == "":
        return label
    try:
        import mason_robinhood
        return mason_robinhood.brain_label(label)
    except Exception:
        return "Account"


def _held_names(held) -> str:
    """N6b: the HELD account labels as a model sees them (mask dropped, duplicates collapsed, order kept)."""
    out = []
    for x in held or []:
        n = _bl(x)
        if n and n not in out:
            out.append(n)
    return ", ".join(out)


def _held_accounts(ticker, holdings) -> list:
    """Account labels (in-scope only) holding the ticker as equity or option underlying."""
    ex = _exclude_accounts()
    out = []
    for p in holdings or []:
        try:
            if not isinstance(p, dict) or p.get("kind") not in ("equity", "option"):
                continue
            if str(p.get("underlying") or p.get("ticker") or "").upper() != ticker:
                continue
            acct = str(p.get("account") or "").strip()
            if acct and _norm_label(acct) and _norm_label(acct) not in ex and acct not in out:
                out.append(acct)
        except Exception:
            continue
    return out


def _advice_action(tri, propose_min) -> str:
    if not tri or tri.get("failed"):
        return "hold"
    if tri.get("direction") == "short":
        return "trim"
    if tri.get("direction") == "long" and float(tri.get("score") or 0) >= propose_min:
        return "add"
    return "hold"


def _build_prompt(hit, title, ctx) -> str:
    """G-S3: cells in the scan's columns_json order (RH sends them alphabetically), then any cell not in
    that list; ratios / yyyymmdd dates made readable; the 20-cell cap applies AFTER ordering."""
    cells = _cells(_jl(hit.get("last_row_json"), {}) or _jl(hit.get("first_row_json"), {}))
    order = [c for c in (_jl(hit.get("columns_json"), []) or []) if isinstance(c, str)]
    keys = [c for c in dict.fromkeys(order) if c in cells] + [k for k in cells if k not in set(order)]
    cl = "\n".join(f"  {_clean(k, 60)}: {_clean(_cell_text(k, cells[k]), 40)}" for k in keys[:20])
    return (f"TICKER: {hit['ticker']}\nSCAN: {title}\nFIRST SEEN: {hit['first_seen']}\n"
            f"CELLS (as configured in Robinhood):\n{cl}\nCONTEXT:\n" + "\n".join(ctx) + "\nReply with the JSON object.")


def _triage(now, daily: bool) -> dict:
    now = _aware(now)
    session = _et(now).strftime("%Y-%m-%d")
    cfg = _cfg()
    tc = cfg.get("triage") or {}
    cap = max(0, int(tc.get("max_per_hour") or 0))
    propose_min = float(tc.get("propose_min_score") if tc.get("propose_min_score") is not None else 0.70)
    daily_ids = [str(x) for x in (tc.get("daily_scan_ids") or [])]
    if _busy():
        return {"ran": False, "reason": "gpu busy", "triaged": 0}
    conn = mason_db.get_connection()
    try:
        # Deliberately UNFILTERED (RULINGS 8): the qwen hourly budget counts calls actually made.
        used = conn.execute("SELECT COUNT(*) FROM scanner_hits WHERE triaged_at >= ?",
                            (_iso(now - timedelta(hours=1)),)).fetchone()[0]
        room = cap - int(used or 0)
        if room <= 0:
            return {"ran": False, "reason": "hourly cap", "triaged": 0}
        ph = ",".join("?" for _ in daily_ids) or "''"
        clause = f"scan_id IN ({ph})" if daily else f"scan_id NOT IN ({ph})"
        rows = conn.execute(
            f"SELECT h.*, COALESCE(s.title, '') AS title, s.columns_json AS columns_json FROM scanner_hits h "
            f"LEFT JOIN scanner_scans s ON s.scan_id = h.scan_id "
            f"WHERE h.triaged_at IS NULL AND h.session_date = ? AND h.{clause} "
            f"AND COALESCE(h.record_deleted,0)=0 "
            f"ORDER BY h.first_seen LIMIT ?", (session, *daily_ids, room)).fetchall()
        rows = [dict(r) for r in rows]
        skip = tc.get("skip_trade_stories") is True        # (2026-11-19) off = today's context, byte for byte
        ctx = {r["id"]: _ctx_lines(conn, r["ticker"], now, skip_stories=skip) for r in rows}
    finally:
        conn.close()
    if not rows:
        return {"ran": True, "triaged": 0, "proposed": 0, "advice": 0}
    try:
        holdings = _holdings() or []
    except Exception as e:
        _err("holdings", e)
        holdings = []
    n = proposed = advised = 0
    for h in rows:
        title = h.get("title") or ""
        held = _held_accounts(h["ticker"], holdings)
        c = ctx.get(h["id"], []) + ["Held at Robinhood: " + (_held_names(held) if held else "no")]
        try:
            text = _llm(_TRIAGE_SYSTEM, _build_prompt(h, title, c), temperature=0.1, num_predict=200)
        except Exception as e:
            _err(f"qwen triage {h['ticker']}", e)
            break                                   # model unavailable: retry next run
        tri = parse_triage(text)
        g = _jl(h.get("grades_json"), {}) or {}
        g["triage"] = {"direction": tri["direction"], "why_now": tri["why_now"], "failed": tri["failed"]}
        # Only a status produced by THIS triage is written; None keeps whatever is in the row now
        # (COALESCE below), so a Nico Propose/Promote made during the qwen call is never overwritten
        # with the value read before it (D8D9 binding condition).
        watch_status = None
        if not tri["failed"] and tri["score"] >= propose_min:
            try:
                res = _watch_add(h["ticker"], origin="qwen", channel=f"scanner:{title}",
                                 reason=_clean(f"scanner {title}: {tri['note']}", 280)) or {}
                if res.get("ok"):
                    watch_status = (res.get("item") or {}).get("status") or res.get("action")
                    proposed += 1
            except Exception as e:
                _err(f"watchlist proposal {h['ticker']}", e)
        advice = None
        if held and not g.get("advice_sent"):
            says = tri["note"] if not tri["failed"] else "(qwen triage unavailable)"
            says = (says[:1].lower() + says[1:]) if says else says
            advice = (f"You own {h['ticker']} ({', '.join(held)}): scanner {title} says {says} "
                      f"→ consider {_advice_action(tri, propose_min)}")
            g.update(held=held, advice=advice)
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("UPDATE scanner_hits SET triage_score=?, triage_note=?, triaged_at=?, "
                             "watch_status=COALESCE(?, watch_status), grades_json=? WHERE id=? AND triaged_at IS NULL "
                             "AND COALESCE(record_deleted,0)=0",
                             (tri["score"], tri["note"], _iso(now), watch_status, json.dumps(g), h["id"]))
        except Exception as e:
            _err(f"triage write {h['ticker']}", e)
            continue
        finally:
            conn.close()
        n += 1
        if advice:
            try:
                _notify("recommendation", f"Scanner hit on {h['ticker']} (you own it)", body=advice,
                        severity="info", ticker=h["ticker"], ref_table="scanner_hits", ref_id=h["id"],
                        created_by=CREATED_BY,
                        dedup_key=f"scanner_held:{h['scan_id']}:{h['ticker']}:{h['session_date']}")
                g["advice_sent"] = True
                c2 = mason_db.get_connection()
                try:
                    with c2:
                        c2.execute("UPDATE scanner_hits SET grades_json=? WHERE id=? AND COALESCE(record_deleted,0)=0",
                                   (json.dumps(g), h["id"]))
                finally:
                    c2.close()
                advised += 1
            except Exception as e:
                _err(f"held advice {h['ticker']}", e)
    with _state_lock:
        _st["last_run"]["triage"] = _iso(now)
    return {"ran": True, "triaged": n, "proposed": proposed, "advice": advised}


def triage_pending(now=None) -> dict:
    """Triage NEW confirmed hits (once-daily scans excluded). {} when triage is off."""
    if not _sub("triage"):
        return {}
    if not _triage_lock.acquire(blocking=False):
        return {"ran": False, "reason": "triage already running", "triaged": 0}
    try:
        return _triage(now, daily=False)
    except Exception as e:
        return {"ran": False, "reason": _err("triage", e), "triaged": 0}
    finally:
        _triage_lock.release()


_DAILY_KEY = "scanners_triage_daily"


def _desk_get(key):
    try:
        conn = mason_db.get_connection()
        try:
            r = conn.execute("SELECT value FROM desk_state WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None
        finally:
            conn.close()
    except Exception as e:
        _err("desk_state read", e)
        return None


def _desk_set(key, value):
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                             (key, value, _iso(_now())))
        finally:
            conn.close()
    except Exception as e:
        _err("desk_state write", e)


def triage_daily(now=None) -> dict:
    """Once-daily triage for slow scans (Quality Pullback) at/after triage.daily_at_et (15:30 ET; D7).
    Early-close days still run at 15:30 (the data is final, ruling D7-c)."""
    if not _sub("triage"):
        return {}
    try:
        now = _aware(now)
        t = _et(now)
        at = str((_cfg().get("triage") or {}).get("daily_at_et") or "15:30")
        hh, _, mm = at.partition(":")
        if (t.hour, t.minute) < (int(hh), int(mm or 0)):
            return {"ran": False, "reason": f"before {at}"}
        if not _is_trading_day(t):
            return {"ran": False, "reason": "not a trading day"}
        session = t.strftime("%Y-%m-%d")
        with _state_lock:
            done = _st["daily_done"]
        if done != session:
            done = _desk_get(_DAILY_KEY)
        if done == session:
            with _state_lock:
                _st["daily_done"] = session
            return {"ran": False, "reason": "already ran today"}
        if not _triage_lock.acquire(blocking=False):
            return {"ran": False, "reason": "triage already running", "triaged": 0}
        try:
            r = _triage(now, daily=True)
        finally:
            _triage_lock.release()
        if r.get("ran"):
            with _state_lock:
                _st["daily_done"] = session
            _desk_set(_DAILY_KEY, session)
        return r
    except Exception as e:
        return {"ran": False, "reason": _err("triage_daily", e)}


# ══ grading ══════════════════════════════════════════════════════════════════
def _horizons() -> list:
    try:
        hs = [int(h) for h in (_cfg().get("grading") or {}).get("horizons_sessions") or [1, 5]]
        return [h for h in hs if 0 < h <= 60][:4] or [1, 5]
    except Exception:
        return [1, 5]


def _grade_direction(hit) -> str:
    """qwen's direction; else long for no-direction scans (triage.daily_scan_ids, e.g. Quality Pullback,
    design §4.6); else the sign of % Change; else long."""
    g = _jl(hit.get("grades_json"), {}) or {}
    tri = g.get("triage") or {}
    if tri.get("direction") in ("long", "short") and not tri.get("failed"):
        return tri["direction"]
    try:
        no_dir = {str(x) for x in ((_cfg().get("triage") or {}).get("daily_scan_ids") or [])}
    except Exception:
        no_dir = {QUALITY_PULLBACK_ID}
    if hit.get("scan_id") in no_dir:
        return "long"
    chg = _pct_change(_jl(hit.get("first_row_json"), {}))
    if chg is not None and chg < 0:
        return "short"
    return "long"


def _last_closed_session(t_et: datetime):
    """The most recent trading session whose close (16:00 ET) has passed."""
    d = t_et.date()
    if _is_trading_day(datetime.combine(d, datetime.min.time(), ET)) and (t_et.hour, t_et.minute) >= (16, 0):
        return d
    for _ in range(15):
        d -= timedelta(days=1)
        if _is_trading_day(datetime.combine(d, datetime.min.time(), ET)):
            return d
    return d


def _due_cutoff(last_closed, n):
    """Latest session_date whose +n-session close has happened: n trading sessions before last_closed."""
    d, k = last_closed, 0
    for _ in range(n * 3 + 20):
        if k >= n:
            break
        d -= timedelta(days=1)
        if _is_trading_day(datetime.combine(d, datetime.min.time(), ET)):
            k += 1
    return d.isoformat()


def grade_due(now=None) -> dict:
    """Grade hits at +N sessions via mason_price_cache.fwd_return only; write scanner priors."""
    if not _sub("grading"):
        return {}
    try:
        now = _aware(now)
        t_et = _et(now)
        lo = (t_et.date() - timedelta(days=_GRADE_LOOKBACK_DAYS)).isoformat()
        hz = _horizons()
        last_closed = _last_closed_session(t_et)
        cutoff = {n: _due_cutoff(last_closed, n) for n in hz}
        # Select ONLY rows with an ungraded horizon before the LIMIT, so already graded rows can never
        # starve newer hits (DEBUG_T7 D2). Rows whose horizon is due by the trading calendar come first
        # (newest first), so a backlog of not-yet-matured recent hits can't starve due ones either.
        # The calendar only ORDERS the work: fwd_return's `matured` stays the sole maturity authority.
        # Malformed grades_json counts as ungraded.
        ext = "(CASE WHEN json_valid(grades_json) THEN json_extract(grades_json, '$.\"{n}\"') END) IS NULL"
        need = " OR ".join(ext.format(n=n) for n in hz)
        due = " OR ".join(f"(session_date <= ? AND {ext.format(n=n)})" for n in hz)
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM scanner_hits WHERE session_date >= ? AND session_date < ? "
                f"AND first_price IS NOT NULL AND COALESCE(record_deleted,0)=0 AND ({need}) "
                f"ORDER BY (CASE WHEN ({due}) THEN 0 ELSE 1 END), session_date DESC, first_seen DESC LIMIT 500",
                (lo, t_et.date().isoformat(), *[cutoff[n] for n in hz])).fetchall()]
        finally:
            conn.close()
        import mason_price_cache
        graded, errors, touched = 0, 0, set()
        for h in rows:
            g = _jl(h.get("grades_json"), {}) or {}
            changed = False
            for n in hz:
                if isinstance(g.get(str(n)), dict):
                    continue
                try:
                    r = mason_price_cache.fwd_return(h["ticker"], h["session_date"], sessions=n,
                                                     entry_px=h["first_price"])
                except Exception as e:
                    errors += 1
                    _err(f"fwd_return {h['ticker']} {n}s", e)
                    continue
                if not isinstance(r, dict) or not r.get("matured") or _num(r.get("ret")) is None:
                    continue
                ret = float(r["ret"])
                d = _grade_direction(h)
                signed = ret if d == "long" else -ret
                g[str(n)] = {"ret": ret, "xs": _num(r.get("xs")), "signed": signed, "correct": signed > 0,
                             "direction": d, "exit_date": r.get("exit_date")}
                changed = True
                graded += 1
            if changed:
                c = mason_db.get_connection()
                try:
                    with c:
                        c.execute("UPDATE scanner_hits SET grades_json=?, grade_1d=?, grade_5d=? WHERE id=? "
                                  "AND COALESCE(record_deleted,0)=0",
                                  (json.dumps(g), (g.get("1") or {}).get("signed"), (g.get("5") or {}).get("signed"),
                                   h["id"]))
                    touched.add(h["scan_id"])
                finally:
                    c.close()
        priors = _write_priors()          # full recompute + retire bracket every pass
        with _state_lock:
            _st["last_run"]["grade"] = _iso(now)
        return {"graded": graded, "errors": errors, "priors": priors}
    except Exception as e:
        return {"graded": 0, "errors": 1, "reason": _err("grade", e)}


def _graded_samples(scan_id=None) -> dict:
    """{scan_id: {horizon: [(hit_id, grade dict)]}} from stored grades."""
    out = {}
    conn = mason_db.get_connection()
    try:
        q = "SELECT id, scan_id, grades_json FROM scanner_hits WHERE COALESCE(record_deleted,0)=0"
        rows = conn.execute(q + (" AND scan_id=?" if scan_id else ""), ((scan_id,) if scan_id else ())).fetchall()
    finally:
        conn.close()
    for r in rows:
        g = _jl(r["grades_json"], {}) or {}
        for k, v in g.items():
            if k.isdigit() and isinstance(v, dict) and _num(v.get("signed")) is not None:
                out.setdefault(r["scan_id"], {}).setdefault(int(k), []).append((r["id"], v))
    return out


def _write_priors(_scan_ids=None) -> int:      # _scan_ids: ignored (full recompute); kept for existing callers
    """Recompute EVERY scanner prior (one key per scan_id × configured horizon) inside a
    begin_run/retire_unrefreshed bracket, so keys that stop being produced (a horizon dropped from
    config, a scan whose grades are gone) are retired. The retire is skipped when any upsert failed
    (REAL_S1_RERUN step 4). excess_vs_spy = mean of the direction-signed fwd_return xs."""
    try:
        import mason_kb_priors
    except Exception as e:
        _err("kb_priors import", e)
        return 0
    titles = {s["scan_id"]: s["title"] for s in _db_scans()}
    hz_cfg = set(_horizons())
    run = mason_kb_priors.begin_run("scanner")
    n, failed = 0, False
    for sid, by_h in _graded_samples().items():
        title = str(titles.get(sid) or sid[:8]).replace("{", "(").replace("}", ")")
        for hz, items in by_h.items():
            if hz not in hz_cfg:
                continue
            xs = [(_num(v.get("xs")), v.get("direction")) for _, v in items]
            sx = [x if d != "short" else -x for x, d in xs if x is not None]
            try:
                row = mason_kb_priors.upsert_prior(
                    "scanner", {"scan_id": sid}, f"{hz}s", [float(v["signed"]) for _, v in items],
                    # observational wording only, n always shown: the priors library (min_n_met / CI)
                    # decides whether it is evidence; this text never claims an edge.
                    summary_fmt=f"Scanner '{title}' hits, +{hz} sessions, direction-signed: "
                                "observed hit rate {hit_rate:.0%}, mean {mean:+.2%} (n={n}; "
                                "early signal until min-n is met and the CI excludes 50% / 0)",
                    refs=[{"table": "scanner_hits", "id": i} for i, _ in items],
                    excess_vs_spy=(sum(sx) / len(sx) if sx else None))
                if row is None:
                    failed = True
                else:
                    n += 1
            except Exception as e:
                failed = True
                _err(f"upsert_prior {sid[:8]} {hz}s", e)
    if run and not failed:
        try:
            mason_kb_priors.retire_unrefreshed("scanner", run)
        except Exception as e:
            _err("retire_unrefreshed scanner", e)
    return n


# ══ Opus section (pure: reads only, returns text) ════════════════════════════
def opus_section(session_date=None, *, max_items=None, tokens=None) -> str:
    """'SCANNER HITS TODAY' block for the Opus anchor package; '' when off or empty."""
    if not _sub("opus_section"):
        return ""
    try:
        oc = _cfg().get("opus_section") or {}
        max_items = int(max_items or oc.get("max_items") or 8)
        budget = int(tokens or oc.get("tokens") or 600) * 4          # ≈ 4 chars per token
        session = session_date or _et().strftime("%Y-%m-%d")
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT h.*, COALESCE(s.title,'') AS title FROM scanner_hits h LEFT JOIN scanner_scans s "
                "ON s.scan_id = h.scan_id WHERE h.session_date=? AND COALESCE(h.record_deleted,0)=0 "
                "ORDER BY (h.triage_score IS NULL), h.triage_score DESC, h.first_seen LIMIT ?",
                (session, max_items)).fetchall()]
        finally:
            conn.close()
        if not rows:
            return ""
        head = ("SCANNER HITS TODAY (Robinhood scanners; observe-first context only, not a trade trigger; "
                "scanner-only evidence cannot support a trade idea)")
        out = head[:budget]
        for h in rows:
            g = _jl(h.get("grades_json"), {}) or {}
            tri = g.get("triage") or {}
            parts = [h["ticker"], _clean(h["title"], 40)]
            try:
                parts.append("first " + datetime.fromisoformat(h["first_seen"]).astimezone(ET).strftime("%H:%M ET"))
            except Exception:
                pass
            ts = _num(h.get("triage_score"))
            if ts is not None:
                parts.append(f"qwen {ts:.2f} {tri.get('direction') or 'none'}")
            chg = _pct_points(_jl(h.get("last_row_json"), {}))     # G-S1: ratio cells ×100 here only
            if chg is not None:
                t1 = _fixed(chg, 1)                             # never "-0.0%"
                parts.append(("" if t1.startswith("-") else "+") + t1 + "%")
            if h.get("triage_note"):
                parts.append(_clean(h["triage_note"], 120))
            if g.get("held"):
                parts.append("HELD: " + _held_names(g["held"]))
            if h.get("watch_status"):
                parts.append(f"watch: {h['watch_status']}")
            if h.get("exited_at"):
                parts.append("exited")
            if h.get("reentries"):
                parts.append(f"re-entered x{h['reentries']}")
            line = "\n- " + " · ".join(parts)
            if len(out) + len(line) > budget:
                break
            out += line
        return out if "\n- " in out else ""
    except Exception as e:
        _err("opus_section", e)
        return ""


# ══ API (DB reads only) ═══════════════════════════════════════════════════════
def _env(data, *, stale=False, errors=None) -> dict:
    return {"ok": True, "as_of": _iso(_now()), "data": _safe(data), "stale": bool(stale),
            "errors": [_clean(e, 300) for e in (errors or [])]}


def _hit_out(r: dict, titles: dict, hz: list) -> dict:
    g = _jl(r.get("grades_json"), {}) or {}
    last = _jl(r.get("last_row_json"), {}) or {}
    tri = None
    if r.get("triaged_at"):
        t = g.get("triage") or {}
        tri = {"score": r.get("triage_score"), "direction": t.get("direction") or "none",
               "note": r.get("triage_note") or "", "why_now": t.get("why_now") or "",
               "triaged_at": r["triaged_at"], "failed": bool(t.get("failed"))}
    active = r.get("exited_at") is None
    return {"id": r["id"], "scan_id": r["scan_id"], "title": titles.get(r["scan_id"], ""),
            "ticker": r["ticker"], "session_date": r["session_date"], "first_seen": r["first_seen"],
            "last_seen": r["last_seen"], "exited_at": r.get("exited_at"), "active": active,
            "reentries": int(r.get("reentries") or 0), "cells": _cells(last),
            "first_price": r.get("first_price"), "triage": tri, "watch_status": r.get("watch_status"),
            "held": list(g.get("held") or []), "advice": g.get("advice"),
            "grades": {str(n): g.get(str(n)) for n in hz},
            "rank": (last.get("_rank") if active and isinstance(last.get("_rank"), int) else None)}


def _status_block(now=None) -> dict:
    now = _aware(now)
    with _state_lock:
        lp = _st["last_poll"]
        return {"state": _st["state"], "message": _st["message"], "connected": _st["connected"],
                "window": poll_window(now), "last_poll": _iso(lp) if lp else None,
                "next_poll_s": (_st["backoff_s"] or poll_interval(now)), "backoff_s": _st["backoff_s"],
                "consecutive_errors": _st["consecutive_errors"]}


def api_scanners() -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        now = _now()
        session = _et(now).strftime("%Y-%m-%d")
        scans = _db_scans()
        titles = {s["scan_id"]: s["title"] for s in scans}
        hz = _horizons()
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM scanner_hits WHERE session_date=? AND COALESCE(record_deleted,0)=0 LIMIT 2000",
                (session,)).fetchall()]
            meta = {r["scan_id"]: dict(r) for r in conn.execute(
                "SELECT * FROM scanner_scans WHERE COALESCE(record_deleted,0)=0").fetchall()}
        finally:
            conn.close()
        by = {}
        for r in rows:
            by.setdefault(r["scan_id"], []).append(_hit_out(r, titles, hz))
        out = []
        for s in scans:
            hs = by.get(s["scan_id"], [])
            hs.sort(key=lambda h: (not h["active"], h["rank"] if h["rank"] is not None else 10 ** 6,
                                   "" if h["active"] else "~" + (h["last_seen"] or "")))
            act = [h for h in hs if h["active"]]
            ex = sorted([h for h in hs if not h["active"]], key=lambda h: h["last_seen"] or "", reverse=True)
            m = meta.get(s["scan_id"], {})
            out.append({"scan_id": s["scan_id"], "title": s["title"], "columns": s["columns"],
                        "sorting": s["sorting"], "last_run": m.get("last_run"), "last_error": m.get("last_error"),
                        "hits": act + ex})
        st = _status_block(now)
        cfg = _cfg()
        flags = {k: bool(v.get("enabled")) for k, v in cfg.items() if isinstance(v, dict) and "enabled" in v}
        stale = st["state"] in ("disconnected", "error")
        if st["window"] and st["last_poll"]:
            age = (now - datetime.fromisoformat(st["last_poll"])).total_seconds()
            stale = stale or age > 3 * max(st["next_poll_s"] or 60, 60)
        with _state_lock:
            errs = list(_st["errors"]) if stale else []
        return _env({"status": st, "session_date": session, "flags": flags, "scans": out},
                    stale=stale, errors=errs)
    except Exception as e:
        return {"ok": False, "as_of": _iso(_now()), "data": None, "stale": True, "errors": [_err("api", e)]}


def api_hits(scan_id=None, days=7, ticker=None, limit=200) -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        days = max(1, min(int(days or 7), _HIST_DAYS_MAX))
        limit = max(1, min(int(limit or 200), 1000))
        since = (_et().date() - timedelta(days=days)).isoformat()
        sql, args = "SELECT * FROM scanner_hits WHERE session_date >= ? AND COALESCE(record_deleted,0)=0", [since]
        if scan_id:
            sql += " AND scan_id = ?"
            args.append(str(scan_id))
        if ticker:
            sql += " AND ticker = ?"
            args.append(str(ticker).strip().upper())
        sql += " ORDER BY session_date DESC, first_seen DESC LIMIT ?"
        args.append(limit)
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
        titles = {s["scan_id"]: s["title"] for s in _db_scans()}
        hz = _horizons()
        return _env([_hit_out(r, titles, hz) for r in rows])
    except Exception as e:
        return {"ok": False, "as_of": _iso(_now()), "data": None, "stale": True, "errors": [_err("api_hits", e)]}


def _wilson(k: int, n: int, z: float = 1.96):
    """Wilson score interval [lo, hi] for k successes in n trials (None when n == 0)."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)]


def _horizon_stats(items, min_n: int = 10) -> dict:
    """Aggregates of stored fwd_return grades. `evidence` is "significant" only when n >= min_n AND the
    Wilson CI95 of the hit rate excludes 0.5; otherwise "early_signal" (REVIEW_T7 F2)."""
    n = len(items)
    if not n:
        return {"n": 0, "hit_rate": None, "avg_move_pct": None, "avg_signed_pct": None,
                "hit_rate_ci95": None, "avg_signed_xs_pct": None, "evidence": "early_signal"}
    signed = [float(v["signed"]) for _, v in items]
    moves = [abs(float(v.get("ret") if _num(v.get("ret")) is not None else v["signed"])) for _, v in items]
    xs = [(_num(v.get("xs")), v.get("direction")) for _, v in items]
    sx = [x if d != "short" else -x for x, d in xs if x is not None]
    k = sum(1 for v in signed if v > 0)
    ci = _wilson(k, n)
    sig = n >= min_n and ci is not None and (ci[0] > 0.5 or ci[1] < 0.5)
    return {"n": n, "hit_rate": round(k / n, 4),
            "avg_move_pct": round(sum(moves) / n * 100, 2), "avg_signed_pct": round(sum(signed) / n * 100, 2),
            "hit_rate_ci95": ci, "avg_signed_xs_pct": (round(sum(sx) / len(sx) * 100, 2) if sx else None),
            "evidence": "significant" if sig else "early_signal"}


def api_scorecard() -> dict:
    """Per-scan scorecard: aggregates of stored fwd_return grades (no return math here)."""
    if not enabled():
        return {"enabled": False}
    try:
        scans = _db_scans()
        hz = _horizons()
        samples = _graded_samples()
        min_n = _min_n()
        conn = mason_db.get_connection()
        try:
            counts = {r["scan_id"]: dict(r) for r in conn.execute(
                "SELECT scan_id, COUNT(*) AS n_hits, SUM(triaged_at IS NOT NULL) AS n_triaged, "
                "SUM(watch_status IN ('proposed','active')) AS n_proposed, "
                "SUM(COALESCE(json_extract(grades_json,'$.advice_sent'),0)) AS n_advice, "
                "MIN(session_date) AS since FROM scanner_hits WHERE COALESCE(record_deleted,0)=0 "
                "GROUP BY scan_id").fetchall()}
        finally:
            conn.close()
        out = []
        for s in scans:
            c = counts.get(s["scan_id"], {})
            hs = {str(n): _horizon_stats((samples.get(s["scan_id"]) or {}).get(n, []), min_n) for n in hz}
            out.append({"scan_id": s["scan_id"], "title": s["title"], "n_hits": int(c.get("n_hits") or 0),
                        "n_triaged": int(c.get("n_triaged") or 0), "n_proposed": int(c.get("n_proposed") or 0),
                        "n_held_advice": int(c.get("n_advice") or 0), "horizons": hs,
                        "min_n_met": {k: v["n"] >= min_n for k, v in hs.items()}})
        since = min((c["since"] for c in counts.values() if c.get("since")), default=None)
        return _env({"since": since, "scans": out})
    except Exception as e:
        return {"ok": False, "as_of": _iso(_now()), "data": None, "stale": True,
                "errors": [_err("api_scorecard", e)]}


if Blueprint is not None:
    bp = Blueprint("mason_scanners", __name__)

    def _arg(name, default=None):
        try:
            return request.args.get(name, default)
        except Exception:
            return default

    @bp.route("/api/scanners", methods=["GET"])
    def _r_scanners():
        return jsonify(api_scanners())

    @bp.route("/api/scanners/hits", methods=["GET"])
    def _r_hits():
        def _int(v, d):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        return jsonify(api_hits(scan_id=_arg("scan_id") or None, days=_int(_arg("days"), 7),
                                ticker=_arg("ticker") or None, limit=_int(_arg("limit"), 200)))

    @bp.route("/api/scanners/scorecard", methods=["GET"])
    def _r_scorecard():
        return jsonify(api_scorecard())


# ══ jobs / status ════════════════════════════════════════════════════════════
def jobs() -> list:
    """Job specs for the S2-T8 integrator; [] when off. Each fn takes no args."""
    if not enabled():
        return []
    out = [{"name": "scanners_poll", "every_s": 15, "fn": tick}]
    if _sub("triage"):
        at = str((_cfg().get("triage") or {}).get("daily_at_et") or "15:30")
        out += [{"name": "scanners_triage", "every_s": 60, "fn": triage_pending},
                {"name": "scanners_triage_daily", "at_et": at, "fn": triage_daily}]
    if _sub("grading"):
        out.append({"name": "scanners_grade", "at_et": "16:45", "fn": grade_due})
    return out


def status() -> dict:
    try:
        if not enabled():
            return {"enabled": False, "state": "off", "message": "", "last_poll": None, "backoff_s": 0,
                    "consecutive_errors": 0, "scans": 0, "rows": 0, "errors": [], "truncated": 0,
                    "rate_hits": 0, "last_run": {}}
        rows = 0
        try:
            conn = mason_db.get_connection()
            try:
                # Deliberately UNFILTERED (RULINGS 8): storage row count, deleted rows included.
                rows = int(conn.execute("SELECT COUNT(*) FROM scanner_hits").fetchone()[0])
            finally:
                conn.close()
        except Exception:
            pass
        rate_hits = 0
        try:
            import mason_robinhood
            rate_hits = int((mason_robinhood._state or {}).get("rate_hits") or 0)
        except Exception:
            pass
        st = _status_block()
        with _state_lock:
            return {"enabled": True, **st, "scans": len(_st["scans"]), "rows": rows,
                    "errors": list(_st["errors"]), "truncated": _st["truncated"], "rate_hits": rate_hits,
                    "last_run": dict(_st["last_run"])}
    except Exception as e:
        return {"enabled": False, "state": "error", "message": str(e)[:200], "errors": [str(e)[:200]]}


# ══ finalization T-D8D9 (RULINGS 14) — Propose-button helpers, append-only ═══════
# Used by POST /api/watchlist/<ticker>/propose (mason_watch_api). The soft-delete filter is applied
# only when scanner_hits.record_deleted exists (T-SCN / D5 adds it); a deleted hit is never found
# and never written (no resurrection).
_PROPOSE_WATCH_STATUSES = ("proposed", "active")
_PROPOSE_ID_MAX = 128


def _propose_id_ok(hit_id) -> bool:
    return isinstance(hit_id, str) and bool(hit_id.strip()) and len(hit_id) <= _PROPOSE_ID_MAX


def _hits_live_clause(conn, alias="") -> str:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scanner_hits)").fetchall()}
    return f" AND COALESCE({alias}record_deleted,0)=0" if "record_deleted" in cols else ""


def hit_for_propose(hit_id) -> dict | None:
    """One scanner hit for Nico's Propose click: {id, scan_id, ticker, session_date, title,
    triage_note, triage_score, watch_status}. None when scanners are off (scanners.enabled or
    robinhood.enabled false), the id is malformed or unknown, or the row is soft-deleted.
    Read-only. Never raises."""
    if not enabled() or not _propose_id_ok(hit_id):
        return None
    try:
        conn = mason_db.get_connection()
        try:
            r = conn.execute(
                "SELECT h.id, h.scan_id, h.ticker, h.session_date, h.triage_note, h.triage_score, "
                "h.watch_status, COALESCE(s.title, '') AS title FROM scanner_hits h "
                "LEFT JOIN scanner_scans s ON s.scan_id = h.scan_id WHERE h.id = ?"
                + _hits_live_clause(conn, "h."), (hit_id,)).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None
    except Exception as e:
        _err("hit_for_propose", e)
        return None


def mark_watch_status(hit_id, status) -> bool:
    """Set scanner_hits.watch_status ('proposed' | 'active') after a Propose so the row shows Promote
    (or 'watching') on the next refresh. False when scanners are off, the input is bad, or the hit is
    unknown / soft-deleted. Never raises."""
    if not enabled() or not _propose_id_ok(hit_id) or status not in _PROPOSE_WATCH_STATUSES:
        return False
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                cur = conn.execute("UPDATE scanner_hits SET watch_status = ? WHERE id = ?"
                                   + _hits_live_clause(conn), (status, hit_id))
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as e:
        _err("mark_watch_status", e)
        return False
