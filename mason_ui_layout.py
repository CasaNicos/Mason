"""
mason_ui_layout.py — the Dashboard Widget Manager's server-side layout store.

Contract: docs/ui/wm_build/ENGINEER_BRIEF.md §5 C1; design docs/ui/DASHBOARD_WIDGET_MANAGER_DESIGN.md §5.

A Flask Blueprint that the main app registers in a guarded try (next to the watchlist blueprint):

    GET /api/ui/layout   → 200 {"doc": <doc>} | 200 {"doc": null}
                           | 200 {"doc": null, "error": "corrupt"}  (stored value is not a JSON object)
    PUT /api/ui/layout   body = the doc itself ({"v":1,"rev":<base rev>,"instances":[...],"shelf":[...]})
                         → 200 {"ok": true, "rev": n}      stored with rev = stored+1, updated = UTC ISO
                         | 409 {"doc": <server copy|null>} base rev is stale
                         | 400 {"ok": false, "error": "<short reason>"}   invalid doc (rejected, never repaired)

Storage is desk_state key `ui.dashboard.v1`, holding json.dumps(doc). The rev compare and the write
happen inside ONE connection under BEGIN IMMEDIATE (plus a module lock), so two concurrent PUTs on the
same base rev give exactly one 200 and one 409, even across processes.

Per-page grid layouts (UI overhaul R2-C, 2026-11-16): the same route with `?page=<key>` stores the canonical
36-column layout of ONE other grid page, so a layout arranged on one device follows Nico to the others (the
browser keeps localStorage mv_gs3_<key> / mv_gsc3_<key> as its cache and fallback; static/v2/layout-sync.js).
<key> is the page grid's storage key (the router id unless the page bumped it: positions_m, crowd_v2).

    GET /api/ui/layout?page=<key>  → 200 {"page": key, "ids": [its widget ids], "doc": <doc>|null}
                                     (+ "error": "corrupt" as above) | 404 {"ok": false, "error": "unknown page"}
    PUT /api/ui/layout?page=<key>  body {"v":1,"rev":<base rev>,"geom":{id:{"x","y","w","h"}},"collapsed":{id:{"prevH"}}}
                                   → 200 {"ok": true, "rev": n, "page": key} | 409 {"doc": <server copy|null>, "page": key}
                                   | 400 {"ok": false, "error": "<short reason>"} | 404 unknown page

Page keys and each page's widget ids are allowlisted (UI_PAGE_WIDGETS); the doc is validated like the dashboard
doc (reject rather than repair, body cap MAX_PAGE_BODY_BYTES) and stored in desk_state `ui.page.<key>.v1` with
the same rev compare-and-set. A `page` parameter that is present never falls back to the dashboard doc.

No route raises: a DB failure is a 500 JSON answer plus a logged error. Every connection is closed.
The module has no threads, no config flags and no network access.
"""

import json
import logging
import math
import re
import threading
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import mason_db

log = logging.getLogger("mason.ui_layout")

bp = Blueprint("ui_layout", __name__)

STATE_KEY = "ui.dashboard.v1"
MAX_BODY_BYTES = 64 * 1024

# The catalog's own types (design §4.1 + §4.2): the 13 existing widgets, `chart`, and the Phase 2
# feed types (reserved now so saved docs stay forward-compatible). RH types come from
# mason_rhx_integration.RH_WIDGET_TYPES (contracts §6.1) via ui_layout_type_allowed().
UI_CATALOG_TYPES = ("headlines", "kpi1", "kpi2", "posture", "kpi4", "portfolio", "openOptions",
                    "openPositions", "personalPositions", "sectors", "reminders", "following", "watchlist",
                    "chart", "newsFeed", "activityFeed", "notifFeed", "apiHealth",
                    "learning_loop", "calendar", "scoreboard")

MAX_INSTANCES = 60
MAX_SHELF = 60
MAX_CONFIG_KEYS = 20
MAX_CONFIG_STR = 200
MAX_UPDATED_LEN = 64
GRID_COLS = 36

_TOP_KEYS = frozenset({"v", "rev", "updated", "instances", "shelf"})
_INSTANCE_KEYS = frozenset({"iid", "type", "x", "y", "w", "h", "collapsed", "prevH", "config"})
_IID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_CONFIG_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")

# ── per-page grid layouts (R2-C) ──────────────────────────────────────────────
# Every non-dashboard MasonGrid page: storage key → its widget ids (exact, case-sensitive). Sources:
# templates/mason_v2.html POSITIONS_LAYOUT; static/v2/news.js, api.js, activities_notifications.js LAYOUTs;
# the Robinhood pages' ids from ui/src/widgets/*/meta.js `pages` (id, else the widget type). A page that bumps its
# key, or gains a widget, needs an entry here (until then the browser keeps that part in localStorage only).
UI_PAGE_WIDGETS = {
    "positions_m": ("slot0", "slot1", "slot2", "slot3", "secList",
                    "optSlot0", "optSlot1", "optSlot2", "optSlot3", "optList"),
    "news": ("hud", "timeline", "bubbles", "focus", "book", "tape"),
    "api": ("constellation", "opus", "summary", "sources"),
    "activities": ("act_pulse", "act_ledger"),
    "notifications": ("nt_centre", "nt_alerts", "nt_review", "nt_feed"),
    "realmoney": ("rhAccounts", "rhExposure", "rhEarnings", "realmoneyChart", "rhAlertsLog", "rhStatus",
                  "rhPvr", "rhWhatIf", "rhBehavior", "rhStories"),
    "scanners": ("scannerScorecard", "list1", "list2", "list3", "list4", "chart1", "chart2", "chart3", "chart4"),
    "congress": ("congressOverlap", "congressLeaderboard", "congressFeed", "congressChart"),
    "crowd_v2": ("crowdRibbon", "crowdExplainer", "crowdSnapshot", "crowdMovers", "crowdScorecard", "crowdFeed"),
}
PAGE_STATE_KEY = "ui.page.{}.v1"
MAX_PAGE_BODY_BYTES = 16 * 1024
MAX_PAGE_WIDGETS = 40
_PAGE_TOP_KEYS = frozenset({"v", "rev", "updated", "geom", "collapsed"})
_GEOM_KEYS = frozenset({"x", "y", "w", "h"})
_COLLAPSED_KEYS = frozenset({"prevH"})

# Guarded import (S2 FINAL_REPORT §4): if it fails, RH types are rejected and the error is logged.
try:
    import mason_rhx_integration as _RHX
    _RHX.ui_layout_type_allowed  # noqa: B018 — the helper must exist
    RHX_AVAILABLE = True
except Exception as _e:
    _RHX = None
    RHX_AVAILABLE = False
    log.error(f"[ui_layout] mason_rhx_integration unavailable, RH widget types will be rejected: {_e!r}")

_LOCK = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── validation (reject rather than repair) ──────────────────────────────────
class _Invalid(Exception):
    pass


def type_allowed(type_name) -> bool:
    """Exact, case-sensitive allowlist match: a catalog type, or an RH type when RHX imported."""
    if not isinstance(type_name, str):
        return False
    if type_name in UI_CATALOG_TYPES:
        return True
    if _RHX is None:
        return False
    try:
        return bool(_RHX.ui_layout_type_allowed(type_name, UI_CATALOG_TYPES))
    except Exception as e:
        log.error(f"[ui_layout] ui_layout_type_allowed failed: {e!r}")
        return False


# S3-F6 (docs/robinhood/expansion/S3/HANDOFF_F6.md §5): templates/mason_v2.html renders its Robinhood pages, nav
# entries and catalog types only when the Jinja variable `rhx_ui_enabled` is true. App-wide (every render_template)
# because this blueprint is the one already registered for the dashboard. Fail closed: any error → False.
@bp.app_context_processor
def _rhx_ui_ctx():
    try:
        return {"rhx_ui_enabled": bool(_RHX is not None and _RHX.ui_enabled() is True)}
    except Exception as e:
        log.error(f"[ui_layout] rhx_ui_enabled failed, Robinhood UI hidden: {e!r}")
        return {"rhx_ui_enabled": False}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _int_in(v, lo, hi):
    return _is_int(v) and lo <= v <= hi


def _check_config(cfg, where):
    if not isinstance(cfg, dict):
        raise _Invalid(f"{where}.config must be an object")
    if len(cfg) > MAX_CONFIG_KEYS:
        raise _Invalid(f"{where}.config has more than {MAX_CONFIG_KEYS} keys")
    for k, val in cfg.items():
        if not isinstance(k, str) or not _CONFIG_KEY_RE.fullmatch(k):
            raise _Invalid(f"{where}.config has a bad key")
        if isinstance(val, bool):
            continue
        if isinstance(val, str):
            if len(val) > MAX_CONFIG_STR:
                raise _Invalid(f"{where}.config.{k} is longer than {MAX_CONFIG_STR}")
            continue
        if isinstance(val, (int, float)):
            if isinstance(val, float) and not math.isfinite(val):
                raise _Invalid(f"{where}.config.{k} is not finite")
            continue
        raise _Invalid(f"{where}.config.{k} must be a string, number or bool")


def _check_instance(item, i, seen):
    where = f"instances[{i}]"
    if not isinstance(item, dict):
        raise _Invalid(f"{where} must be an object")
    extra = set(item) - _INSTANCE_KEYS
    if extra:
        raise _Invalid(f"{where} has an unknown key")
    iid = item.get("iid")
    if not isinstance(iid, str) or not _IID_RE.fullmatch(iid):
        raise _Invalid(f"{where}.iid is invalid")
    if iid in seen:
        raise _Invalid(f"{where}.iid is a duplicate")
    seen.add(iid)
    if not type_allowed(item.get("type")):
        raise _Invalid(f"{where}.type is not allowed")
    x, y, w, h = (item.get(k) for k in ("x", "y", "w", "h"))
    if not _int_in(x, 0, GRID_COLS - 1):
        raise _Invalid(f"{where}.x is out of range")
    if not _int_in(w, 1, GRID_COLS) or x + w > GRID_COLS:
        raise _Invalid(f"{where}.w is out of range")
    if not _int_in(y, 0, 10000):
        raise _Invalid(f"{where}.y is out of range")
    if not _int_in(h, 1, 200):
        raise _Invalid(f"{where}.h is out of range")
    if not isinstance(item.get("collapsed"), bool):
        raise _Invalid(f"{where}.collapsed must be a bool")
    prev_h = item.get("prevH")
    if prev_h is not None and not _int_in(prev_h, 1, 200):
        raise _Invalid(f"{where}.prevH is out of range")
    _check_config(item.get("config"), where)


def validate(doc):
    """None when `doc` is a valid layout doc, else a short reason string. Never raises."""
    try:
        if not isinstance(doc, dict):
            raise _Invalid("doc must be an object")
        if set(doc) - _TOP_KEYS:
            raise _Invalid("doc has an unknown top-level key")
        if not (_is_int(doc.get("v")) and doc.get("v") == 1):
            raise _Invalid("v must be 1")
        if "rev" in doc and not _int_in(doc["rev"], 0, 2 ** 53):
            raise _Invalid("rev must be a non-negative int")
        upd = doc.get("updated")
        if upd is not None and not (isinstance(upd, str) and len(upd) <= MAX_UPDATED_LEN):
            raise _Invalid("updated must be a short string")
        instances = doc.get("instances")
        if not isinstance(instances, list):
            raise _Invalid("instances must be a list")
        if len(instances) > MAX_INSTANCES:
            raise _Invalid(f"more than {MAX_INSTANCES} instances")
        shelf = doc.get("shelf")
        if not isinstance(shelf, list):
            raise _Invalid("shelf must be a list")
        if len(shelf) > MAX_SHELF:
            raise _Invalid(f"more than {MAX_SHELF} shelf entries")
        for j, t in enumerate(shelf):
            if not type_allowed(t):
                raise _Invalid(f"shelf[{j}] is not an allowed type")
        seen = set()
        for i, item in enumerate(instances):
            _check_instance(item, i, seen)
        return None
    except _Invalid as e:
        return str(e)
    except Exception as e:
        log.error(f"[ui_layout] validate failed: {e!r}")
        return "invalid doc"


def _check_geom(g, where):
    """One widget's canonical 36-column slot: exactly {x, y, w, h}, the dashboard instance ranges."""
    if not isinstance(g, dict) or set(g) != _GEOM_KEYS:
        raise _Invalid(f"{where} must be exactly {{x, y, w, h}}")
    x, y, w, h = g["x"], g["y"], g["w"], g["h"]
    if not _int_in(x, 0, GRID_COLS - 1):
        raise _Invalid(f"{where}.x is out of range")
    if not _int_in(w, 1, GRID_COLS) or x + w > GRID_COLS:
        raise _Invalid(f"{where}.w is out of range")
    if not _int_in(y, 0, 10000):
        raise _Invalid(f"{where}.y is out of range")
    if not _int_in(h, 1, 200):
        raise _Invalid(f"{where}.h is out of range")


def validate_page(doc, page):
    """None when `doc` is a valid layout doc for grid page `page` (an UI_PAGE_WIDGETS key), else a short reason
    string. Widget ids must be that page's own ids; a collapsed widget must have a geom entry. Never raises."""
    try:
        ids = UI_PAGE_WIDGETS.get(page) if isinstance(page, str) else None
        if ids is None:
            raise _Invalid("unknown page")
        if not isinstance(doc, dict):
            raise _Invalid("doc must be an object")
        if set(doc) - _PAGE_TOP_KEYS:
            raise _Invalid("doc has an unknown top-level key")
        if not (_is_int(doc.get("v")) and doc.get("v") == 1):
            raise _Invalid("v must be 1")
        if "rev" in doc and not _int_in(doc["rev"], 0, 2 ** 53):
            raise _Invalid("rev must be a non-negative int")
        upd = doc.get("updated")
        if upd is not None and not (isinstance(upd, str) and len(upd) <= MAX_UPDATED_LEN):
            raise _Invalid("updated must be a short string")
        geom = doc.get("geom")
        if not isinstance(geom, dict):
            raise _Invalid("geom must be an object")
        if len(geom) > MAX_PAGE_WIDGETS:
            raise _Invalid(f"more than {MAX_PAGE_WIDGETS} widgets")
        for wid in geom:
            if wid not in ids:                    # JSON keys are strings; exact, case-sensitive allowlist match
                raise _Invalid("geom has a widget id this page does not have")
            _check_geom(geom[wid], f"geom.{wid}")
        collapsed = doc.get("collapsed")
        if not isinstance(collapsed, dict):
            raise _Invalid("collapsed must be an object")
        for wid, c in collapsed.items():
            if wid not in geom:
                raise _Invalid("collapsed names a widget that has no geom")
            if not isinstance(c, dict) or set(c) != _COLLAPSED_KEYS or not _int_in(c.get("prevH"), 1, 200):
                raise _Invalid(f"collapsed.{wid} must be exactly {{prevH: 1..200}}")
        return None
    except _Invalid as e:
        return str(e)
    except Exception as e:
        log.error(f"[ui_layout] validate_page failed: {e!r}")
        return "invalid doc"


def _reject_constant(name):
    raise ValueError(f"non-finite constant {name}")


def _parse_body(max_bytes=MAX_BODY_BYTES):
    """(doc, None) or (None, reason). Reads at most max_bytes + 1 bytes."""
    too_big = f"body larger than {max_bytes // 1024} KB"
    if request.mimetype != "application/json":
        return None, "Content-Type must be application/json"
    if request.content_length is not None and request.content_length > max_bytes:
        return None, too_big
    raw = request.stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None, too_big
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), None
    except Exception:
        return None, "body is not valid JSON"


# ── storage (desk_state, json.dumps) ────────────────────────────────────────
def _decode_stored(value):
    """(doc, rev) for a good stored value; (None, None) when corrupt."""
    try:
        doc = json.loads(value)
    except Exception:
        return None, None
    if not isinstance(doc, dict) or not _is_int(doc.get("rev")) or doc["rev"] < 0:
        return None, None
    return doc, doc["rev"]


def _read_row(conn, key=STATE_KEY):
    row = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _no_store(resp, status=200):
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _page_arg():
    """R2-C: (None, None) without ?page= (the dashboard doc); (key, None) for an allowlisted page key;
    (None, (status, payload)) for anything else, so a `page` parameter never falls back to the dashboard doc."""
    vals = request.args.getlist("page")
    if not vals:
        return None, None
    if len(vals) != 1:
        return None, (400, {"ok": False, "error": "page given more than once"})
    if vals[0] not in UI_PAGE_WIDGETS:
        return None, (404, {"ok": False, "error": "unknown page"})
    return vals[0], None


# ── routes ──────────────────────────────────────────────────────────────────
@bp.route("/api/ui/layout", methods=["GET"])
def get_layout():
    try:
        page, bad = _page_arg()
        if bad is not None:
            return _no_store(jsonify(bad[1]), bad[0])
        key = STATE_KEY if page is None else PAGE_STATE_KEY.format(page)
        extra = {} if page is None else {"page": page, "ids": list(UI_PAGE_WIDGETS[page])}
        conn = mason_db.get_connection()
        try:
            value = _read_row(conn, key)
        finally:
            conn.close()
        if value is None:
            return _no_store(jsonify(dict(extra, doc=None)))
        doc, _rev = _decode_stored(value)
        if doc is None:
            log.error(f"[ui_layout] stored {key} is corrupt ({len(str(value))} chars); serving doc=null")
            return _no_store(jsonify(dict(extra, doc=None, error="corrupt")))
        return _no_store(jsonify(dict(extra, doc=doc)))
    except Exception as e:
        log.error(f"[ui_layout] GET failed: {e!r}")
        return _no_store(jsonify({"doc": None, "error": "unavailable"}), 500)


@bp.route("/api/ui/layout", methods=["PUT"])
def put_layout():
    try:
        page, bad = _page_arg()
        if bad is not None:
            return _no_store(jsonify(bad[1]), bad[0])
        if page is not None:
            return _put_page(page)
        doc, err = _parse_body()
        if err is None:
            err = validate(doc)
        if err is not None:
            return _no_store(jsonify({"ok": False, "error": err}), 400)
        base_rev = doc.get("rev", 0)
        status, payload = _compare_and_set(doc, base_rev)
        return _no_store(jsonify(payload), status)
    except Exception as e:
        log.error(f"[ui_layout] PUT failed: {e!r}")
        return _no_store(jsonify({"ok": False, "error": "unavailable"}), 500)


def _put_page(page):
    """R2-C: PUT /api/ui/layout?page=<key> — validate against that page's allowlist, then the same compare-and-set."""
    doc, err = _parse_body(MAX_PAGE_BODY_BYTES)
    if err is None:
        err = validate_page(doc, page)
    if err is not None:
        return _no_store(jsonify({"ok": False, "error": err}), 400)
    status, payload = _compare_and_set(doc, doc.get("rev", 0), PAGE_STATE_KEY.format(page))
    if status in (200, 409):
        payload = dict(payload, page=page)
    return _no_store(jsonify(payload), status)


def _compare_and_set(doc, base_rev, key=STATE_KEY):
    """Atomic: one connection, BEGIN IMMEDIATE, read, compare rev, write, commit.
    → (200, {"ok":true,"rev":n}) | (409, {"doc": server copy or None}) | (500, {...}).
    `key` is the desk_state key: the dashboard doc, or one page's (R2-C)."""
    with _LOCK:
        conn = None
        try:
            conn = mason_db.get_connection()
            conn.isolation_level = None                       # explicit transaction control
            conn.execute("BEGIN IMMEDIATE")
            try:
                value = _read_row(conn, key)
                if value is None:
                    stored_doc, stored_rev = None, None
                else:
                    stored_doc, stored_rev = _decode_stored(value)
                    if stored_doc is None:
                        log.error(f"[ui_layout] stored {key} is corrupt; only a rev-0 PUT replaces it")
                # nothing usable stored → only rev 0 (or missing, which arrives here as 0) may write
                expected = 0 if stored_doc is None else stored_rev
                if base_rev != expected:
                    conn.execute("ROLLBACK")
                    return 409, {"doc": stored_doc}
                new_rev = expected + 1
                out = dict(doc)
                out["rev"] = new_rev
                out["updated"] = _now_iso()
                conn.execute(
                    "INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                    (key, json.dumps(out), out["updated"]))
                conn.execute("COMMIT")
                return 200, {"ok": True, "rev": new_rev}
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except Exception as e2:
                    log.error(f"[ui_layout] rollback failed: {e2!r}")
                raise
        except Exception as e:
            log.error(f"[ui_layout] compare-and-set failed: {e!r}")
            return 500, {"ok": False, "error": "unavailable"}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    log.error(f"[ui_layout] close failed: {e!r}")
