"""
mason_watchlist.py — the watchlist STORE: one source of truth for what Mason actually
WATCHES (price-streamed), who put it there, and why.

Design: docs/watchlist/WATCHLIST_DESIGN.md · binding API: docs/watchlist/WATCHLIST_CONTRACTS.md §3.

Who decides (origin):
    nico  — chat / UI / Alpaca app / seed. Nico's items are NEVER removed or replaced by
            opus, qwen or the system (A1).
    opus  — the director (apply_director_watchlist). At the cap Opus must SWAP: it names
            one of its own items to drop, or the add is refused and logged (D4).
    qwen  — may only PROPOSE (status 'proposed': not watched, not counted) until Nico or
            Opus promotes it (A2).
    Nico can also PROPOSE (propose(): the Scanners page Propose button, origin nico,
            status 'proposed', channel 'scanner:<title>') — same "not watched, not counted
            until promoted" rule (finalization RULINGS 14).

Cap (D3): `watchlist.cap` (25) counts active nico + opus items only. Open positions
(Alpaca paper incl. option underlyings + personal_positions) and `infra_symbols` are
watched automatically OUTSIDE the cap — see universe().

Nico adding at the cap evicts the Mason item with the fewest events, then the one
watched longest (A1); if all items are Nico's the add is refused with a message.

Every mutation writes an append-only `watchlist_log` row (same transaction as the
change) + a decision-tier mason_activity row, and — when `watchlist.alpaca_sync` is
on — schedules a best-effort push to the Alpaca paper watchlists "Mason" / "Nico" (R13).

Flag `watchlist.enabled` is read per call; off ⇒ every public entry point is a cheap
no-op returning an empty/neutral value. Nothing here raises to the caller, and every
DB connection this module opens is closed in a finally. Network goes through the
module-level hooks _alpaca_get/_alpaca_post/_alpaca_put/_alpaca_delete (tests patch them).
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import mason_db

log = logging.getLogger("mason.watchlist")

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
_CREATED_BY = "mason_watchlist"

ORIGINS = ("nico", "opus", "qwen")
ACTORS = ("nico", "opus", "qwen", "system")
CHANNELS = ("chat", "ui", "alpaca_app", "director", "seed", "desk", "scanner")
_SCANNER_TITLE_MAX = 60               # RULINGS 16: "scanner:<title>", title ≤ 60 chars
_CHANNEL_DROP = frozenset('<>&"\'')    # markup chars removed from a scanner title
HORIZONS = ("intraday", "swing", "long")
SIDES = ("above", "below")
_COUNTED = ("nico", "opus")          # origins that occupy a cap slot when active
_MAX_LEVELS_PER_ACTOR = 10
_SNAP_KEY = "watchlist_alpaca_last"
_CACHE_S = 30.0
_ASSET_TTL_S = 24 * 3600
_ASSET_FAIL_TTL_S = 300
_NOT_POSITIONS = {"CASH", "SPAXX", "SPAXX**", ""}

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,2})?$")
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

# Nico's words (design §1 G5 / A5) — seed_nico_defaults() uses these verbatim.
SEED_REASONS = {
    "HOOD": "long-term: expanding into the full broker/dealer landscape",
    "IONQ": "long-term: could win the quantum computing race",
    "RKLB": "long-term: space exploration could go exponential over the next 10 years",
    "SPCX": "long-term: space exploration could go exponential over the next 10 years "
            "(SpaceX, IPO'd 2026-06-12)",
}
_SEED_SIGNALS = {
    "HOOD": ["broker-dealer", "clearing", "wealth management", "new product", "acquisition", "banking"],
    "IONQ": ["quantum advantage", "qubit", "quantum contract", "error correction", "quantum computer"],
    "RKLB": ["Neutron", "launch", "space contract", "NASA", "satellite"],
    "SPCX": ["Starship", "launch", "Starlink", "space contract", "NASA"],
}

# ── config (read per call, mtime-cached) ────────────────────────────────────────
_cfg_cache = {"mtime": None, "data": {}}
_DEFAULTS = {
    "enabled": False, "cap": 25,
    "infra_symbols": ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP",
                      "XLU", "XLB", "XLRE", "XLC", "SMH", "ARKX", "UFO"],
    "alpaca_sync": {"enabled": False, "pull_seconds": 300, "mason_list": "Mason", "nico_list": "Nico"},
}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        wl = _config().get("watchlist") or {}
        for k, v in wl.items():
            if v is None or str(k).startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            else:
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return bool(_cfg().get("enabled", False))
    except Exception:
        return False


def _cap() -> int:
    try:
        return max(1, int(_cfg().get("cap") or 25))
    except Exception:
        return 25


def _sync_cfg() -> dict:
    try:
        return _cfg().get("alpaca_sync") or {}
    except Exception:
        return {}


def _sync_on() -> bool:
    return enabled() and bool(_sync_cfg().get("enabled", False))


def _list_names() -> tuple:
    sc = _sync_cfg()
    return (str(sc.get("mason_list") or "Mason"), str(sc.get("nico_list") or "Nico"))


# ── small helpers ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(s, default):
    if s is None or s == "":
        return default
    if isinstance(s, (list, dict)):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _raw_ticker(raw) -> str:
    return str(raw or "").strip().lstrip("$").strip().upper()


def _norm_ticker(raw) -> str | None:
    t = _raw_ticker(raw)
    return t if _TICKER_RE.match(t) else None


def _norm_horizon(h) -> str:
    h = str(h or "").strip().lower()
    return h if h in HORIZONS else "swing"


def scanner_channel(title) -> str:
    """'scanner:<title>' as stored (RULINGS 16): the title is NFKC-normalized, stripped of control /
    format / surrogate chars and of < > & " ', whitespace-collapsed, trimmed and cut to 60 chars
    (case kept). An empty title → 'scanner'. The qwen triage path (via add) and Nico's Propose
    path (mason_watch_api) both end here, so the stored strings are identical. Never raises."""
    try:
        t = unicodedata.normalize("NFKC", title if isinstance(title, str) else "")
        t = "".join(ch for ch in t if ch not in _CHANNEL_DROP
                    and unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
        t = re.sub(r"\s+", " ", t).strip()[:_SCANNER_TITLE_MAX].strip()
    except Exception:
        t = ""
    return f"scanner:{t}" if t else "scanner"


def _norm_channel(c) -> str | None:
    if isinstance(c, str):
        head, sep, tail = c.partition(":")
        if sep and head.strip().lower() == "scanner":
            return scanner_channel(tail)
    c = str(c or "").strip().lower()
    return c if c in CHANNELS else None


def _norm_levels(levels, set_by: str) -> list:
    """Clean Opus/Nico levels → [{price, side, label, set_by}]; malformed entries dropped."""
    out = []
    if not isinstance(levels, list):
        return out
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        try:
            p = float(lv.get("price"))
        except Exception:
            continue
        if not math.isfinite(p) or p <= 0:
            continue
        side = str(lv.get("side") or "").strip().lower()
        if side not in SIDES:
            continue
        label = re.sub(r"\s+", " ", str(lv.get("label") or "")).strip()[:40]
        out.append({"price": round(p, 4), "side": side, "label": label, "set_by": set_by})
        if len(out) >= _MAX_LEVELS_PER_ACTOR:
            break
    return out


def _who(actor: str) -> str:
    return {"nico": "Nico", "opus": "Mason (Opus)", "qwen": "qwen", "system": "System"}.get(actor, actor)


# ── network hooks (tests monkeypatch these; paths are relative to the paper /v2 base) ──

def _alpaca_request(method, path, params=None, body=None):
    """Alpaca paper trading API → (status, json|text) or (None, error). ≤10s. Never raises."""
    try:
        import mason_execution as EX
        return EX._request(method, path, params=params, body=body, timeout=10)
    except Exception as e:
        return None, str(e)


def _alpaca_get(path, params=None):
    return _alpaca_request("GET", path, params=params)


def _alpaca_post(path, body=None):
    return _alpaca_request("POST", path, body=body)


def _alpaca_put(path, body=None):
    return _alpaca_request("PUT", path, body=body)


def _alpaca_delete(path):
    return _alpaca_request("DELETE", path)


def _alpaca_positions() -> list | None:
    """Raw Alpaca paper positions; None when the fetch FAILED (≠ an empty book)."""
    code, d = _alpaca_get("/positions")
    return d if code == 200 and isinstance(d, list) else None


def _personal_positions() -> list:
    import mason_personal
    return mason_personal.get_open_positions()


# ── caches ──────────────────────────────────────────────────────────────────────
_asset_cache: dict = {}                      # sym -> (known: bool|None, expires_epoch)
_uni_cache = {"ts": 0.0, "data": None}
_pos_cache = {"ts": 0.0, "data": None}
_pos_last_good = {"alpaca": None, "personal": None}   # per-source fallback when a fetch fails
_asset_offline = {"until": 0.0}              # Alpaca unreachable → skip asset checks briefly
_ASSET_OFFLINE_S = 60.0
_LOCK = threading.RLock()                    # serialises cap checks + mutations
_SYNC_LOCK = threading.RLock()
_tl = threading.local()                      # _tl.no_push: inside sync_pull
_push_state = {"pending": False}
_push_lock = threading.Lock()


def _reset_caches() -> None:
    """Test/ops helper: forget cached asset lookups, positions and the universe."""
    _asset_cache.clear()
    _uni_cache.update(ts=0.0, data=None)
    _pos_cache.update(ts=0.0, data=None)
    _pos_last_good.update(alpaca=None, personal=None)
    _asset_offline["until"] = 0.0


def _asset_known(sym: str) -> bool | None:
    """True = Alpaca knows it, False = Alpaca says no such (active) asset, None = couldn't
    ask (network) → caller accepts a syntactically valid ticker (don't block Nico)."""
    now = time.time()
    hit = _asset_cache.get(sym)
    if hit and hit[1] > now:
        return hit[0]
    if _asset_offline["until"] > now:
        return None                          # breaker open: don't stall a slate 10s per ticker
    try:
        code, d = _alpaca_get(f"/assets/{sym}")
    except Exception:
        code, d = None, None
    if code == 200:
        known = not (isinstance(d, dict) and str(d.get("status") or "active").lower() == "inactive")
        _asset_cache[sym] = (known, now + _ASSET_TTL_S)
    elif code in (404, 422):
        known = False
        _asset_cache[sym] = (known, now + _ASSET_TTL_S)
    else:
        known = None
        _asset_cache[sym] = (None, now + _ASSET_FAIL_TTL_S)
        if code is None or (isinstance(code, int) and (code >= 500 or code == 429)):
            _asset_offline["until"] = now + _ASSET_OFFLINE_S
    return known


def _validate(raw) -> tuple:
    """(ticker|None, why). Syntax first, then the Alpaca assets hook."""
    t = _norm_ticker(raw)
    if not t:
        return None, (f"'{str(raw or '').strip()[:12]}' is not a valid ticker "
                      "(letters/digits, ≤6, optional .X or -X class suffix)")
    if _asset_known(t) is False:
        return None, f"{t} is not a known (active) Alpaca asset"
    return t, ""


# ── row shaping / reads ─────────────────────────────────────────────────────────

def _shape(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["levels"] = _loads(d.pop("levels_json", None), [])
    d["extra"] = _loads(d.pop("extra_json", None), {})
    return d


def _active_row(conn, ticker):
    return conn.execute(
        "SELECT * FROM watchlist WHERE ticker=? AND status='active' AND record_deleted=0 "
        "ORDER BY rowid DESC LIMIT 1", (ticker,)).fetchone()


def _proposed_row(conn, ticker):
    return conn.execute(
        "SELECT * FROM watchlist WHERE ticker=? AND status='proposed' AND record_deleted=0 "
        "ORDER BY rowid DESC LIMIT 1", (ticker,)).fetchone()


def _row_by_id(conn, item_id):
    return conn.execute("SELECT * FROM watchlist WHERE id=? AND record_deleted=0",
                        (str(item_id),)).fetchone()


def _count_active(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM watchlist WHERE status='active' AND record_deleted=0 "
        "AND origin IN ('nico','opus')").fetchone()[0]


def list_items(status="active", origin=None, *, conn=None) -> list:
    """Non-deleted items, oldest first. status: 'active' | 'proposed' | 'removed' |
    None/'all'. origin: None | 'nico' | 'opus' | 'qwen'. levels/extra parsed. Never raises."""
    if not enabled():
        return []
    try:
        sql, args = "SELECT * FROM watchlist WHERE record_deleted=0", []
        if status and status != "all":
            sql += " AND status=?"
            args.append(str(status))
        if origin:
            sql += " AND origin=?"
            args.append(str(origin))
        sql += " ORDER BY added_date ASC, rowid ASC"
        own = conn is None
        c = mason_db.get_connection() if own else conn
        try:
            return [_shape(r) for r in c.execute(sql, args).fetchall()]
        finally:
            if own:
                c.close()
    except Exception as e:
        log.warning(f"[watchlist] list_items failed: {e}")
        return []


def get_item(ticker_or_id) -> dict | None:
    """The ACTIVE row for a ticker, else the row with that id (any status). Never raises."""
    if not enabled():
        return None
    try:
        conn = mason_db.get_connection()
        try:
            t = _norm_ticker(ticker_or_id)
            row = _active_row(conn, t) if t else None
            if row is None and ticker_or_id:
                row = _row_by_id(conn, ticker_or_id)
            return _shape(row)
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[watchlist] get_item failed: {e}")
        return None


def counts() -> dict:
    """{"active","nico","opus","cap","free","proposed"} — active = the capped set. Never raises."""
    cap = _cap()
    out = {"active": 0, "nico": 0, "opus": 0, "cap": cap, "free": cap, "proposed": 0}
    if not enabled():
        return out
    try:
        conn = mason_db.get_connection()
        try:
            for r in conn.execute(
                    "SELECT status, origin, COUNT(*) n FROM watchlist WHERE record_deleted=0 "
                    "AND status IN ('active','proposed') GROUP BY status, origin").fetchall():
                if r["status"] == "proposed":
                    out["proposed"] += r["n"]
                elif r["origin"] in _COUNTED:
                    out[r["origin"]] += r["n"]
                    out["active"] += r["n"]
        finally:
            conn.close()
        out["free"] = max(0, cap - out["active"])
    except Exception as e:
        log.warning(f"[watchlist] counts failed: {e}")
    return out


# ── write helpers (callers hold _LOCK and an open conn) ─────────────────────────

def _log(conn, events, action, actor, ticker, item_id=None, reason="", slate_id=None,
         detail=None, *, channel=None, activity=True):
    """Append one watchlist_log row (caller's transaction) and queue its activity row."""
    detail = dict(detail or {})
    conn.execute(
        "INSERT INTO watchlist_log (id, ts, item_id, ticker, action, actor, reason, slate_id, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex[:12], _now_iso(), item_id, ticker, action, actor,
         str(reason or "")[:500], slate_id, json.dumps(detail, default=str)))
    if activity:
        events.append({"action": action, "actor": actor, "ticker": ticker, "item_id": item_id,
                       "reason": str(reason or "")[:500], "slate_id": slate_id, "detail": detail,
                       "channel": channel})


def _insert(conn, ticker, *, origin, channel, status, horizon, reason, levels,
            thesis_id=None, rec_id=None, slate_id=None, extra=None) -> str:
    iid = uuid.uuid4().hex[:12]
    now = _now_iso()
    conn.execute(
        "INSERT INTO watchlist (id, ticker, origin, channel, status, horizon, reason, levels_json, "
        "thesis_id, rec_id, slate_id, added_date, added_by, event_count, extra_json, created_date, "
        "created_by, record_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,0)",
        (iid, ticker, origin, channel, status, horizon, str(reason or "")[:500],
         json.dumps(levels or []), thesis_id, rec_id, slate_id, now, origin,
         json.dumps(extra or {}), now, origin))
    return iid


def _mark_removed(conn, item_id, *, by, reason, replaced_by=None):
    conn.execute(
        "UPDATE watchlist SET status='removed', removed_date=?, removed_by=?, remove_reason=?, "
        "replaced_by=? WHERE id=?",
        (_now_iso(), by, str(reason or "")[:500], replaced_by, item_id))


def _eviction_victim(conn):
    """The opus item with the fewest events, then the one watched longest (A1)."""
    return conn.execute(
        "SELECT * FROM watchlist WHERE status='active' AND record_deleted=0 AND origin='opus' "
        "ORDER BY COALESCE(event_count,0) ASC, added_date ASC, rowid ASC LIMIT 1").fetchone()


def _merge_levels(existing: list, new: list, actor: str) -> list:
    return [lv for lv in (existing or []) if lv.get("set_by") != actor] + list(new)


def _result(ok, item, action, reason="", evicted=None) -> dict:
    return {"ok": bool(ok), "item": item, "action": action, "evicted": evicted, "reason": reason}


def _trigger_for(ev) -> str:
    ch = ev.get("channel")
    if ch in ("chat", "ui"):
        return "chat"
    if ev.get("action") == "propose" and ev.get("actor") == "nico":
        return "chat"           # Nico's Propose click (channel 'scanner:…') is user-initiated, like Promote
    if ch is None and ev.get("actor") == "nico" and not getattr(_tl, "no_push", False):
        return "chat"
    return "autonomous"


def _activity_title(ev) -> str:
    a, t, who = ev["action"], ev["ticker"], _who(ev["actor"])
    d = ev.get("detail") or {}
    return {
        "add": f"{who} added {t} to the watchlist",
        "propose": f"{who} suggested watching {t}",
        "promote": f"{who} promoted {t} onto the watchlist",
        "remove": f"{who} removed {t} from the watchlist",
        "swap": f"{who} swapped {d.get('dropped', '?')} → {t} on the watchlist",
        "evict": f"{t} evicted from the watchlist for {d.get('for', '?')}",
        "update": f"{who} updated {t} on the watchlist",
        "refused": f"Watchlist refused {who}: {t}",
        "sync_in": f"Alpaca app watchlist changes pulled ({t})",
        "sync_out": "Watchlist pushed to Alpaca",
    }.get(a, f"watchlist {a} {t}")


def _after(events) -> None:
    """Post-commit side effects: activity rows, cache bust, Alpaca push. Never raises."""
    if not events:
        return
    _uni_cache.update(ts=0.0, data=None)
    try:
        import mason_activity as ACT
    except Exception:
        ACT = None
    for ev in events:
        if ACT is None:
            break
        try:
            ACT.log_activity(
                f"watchlist_{ev['action']}", _activity_title(ev), body=ev.get("reason") or None,
                actor=ev["actor"], category="analyst", ticker=ev.get("ticker"),
                trigger=_trigger_for(ev), ref_table="watchlist", ref_id=ev.get("item_id"),
                extra=dict(ev.get("detail") or {}, slate_id=ev.get("slate_id")),
                tier="system" if ev["action"] == "sync_out" else "decision")
        except Exception as e:
            log.debug(f"[watchlist] activity log failed: {e}")
    try:
        if (not getattr(_tl, "no_push", False) and _sync_on()
                and any(ev["action"] not in ("sync_in", "sync_out", "refused", "propose") for ev in events)):
            _schedule_push()
    except Exception:
        pass


def _schedule_push() -> None:
    """Best-effort, coalesced background sync_push (bursts → one push ~1s later)."""
    with _push_lock:
        if _push_state["pending"]:
            return
        _push_state["pending"] = True

    def _run():
        try:
            time.sleep(1.0)
        finally:
            with _push_lock:
                _push_state["pending"] = False
        try:
            sync_push()
        except Exception as e:
            log.debug(f"[watchlist] background push failed: {e}")

    try:
        threading.Thread(target=_run, name="watchlist-push", daemon=True).start()
    except Exception:
        with _push_lock:
            _push_state["pending"] = False


def _mutate(fn, *args, **kwargs):
    """Run fn(conn, events, ...) under the lock on a fresh, always-closed connection,
    then fire the post-commit side effects."""
    events = []
    with _LOCK:
        conn = mason_db.get_connection()
        try:
            with conn:
                res = fn(conn, events, *args, **kwargs)
        finally:
            conn.close()
    _after(events)
    return res


# ── add / exists / swap / evict ─────────────────────────────────────────────────

def add(ticker, *, origin, channel, reason="", horizon="swing", levels=None, thesis_id=None,
        rec_id=None, slate_id=None, replaces=None, replace_reason="") -> dict:
    """Add one item under the cap/swap rules (contracts §3). Returns
    {"ok","item","action":"add"|"swap"|"exists"|"refused"|"proposed"|"evict_add","evicted","reason"}.
    Never raises."""
    if not enabled():
        return _result(False, None, "refused", "watchlist disabled (watchlist.enabled is off)")
    try:
        origin = str(origin or "").strip().lower()
        t, why = _validate(ticker)
        return _mutate(_add, t, why, _raw_ticker(ticker)[:12], origin=origin,
                       channel=_norm_channel(channel), reason=re.sub(r"\s+", " ", str(reason or "")).strip(),
                       horizon=_norm_horizon(horizon), levels=levels, thesis_id=thesis_id,
                       rec_id=rec_id, slate_id=slate_id, replaces=replaces,
                       replace_reason=re.sub(r"\s+", " ", str(replace_reason or "")).strip())
    except Exception as e:
        log.warning(f"[watchlist] add {ticker} failed: {e}")
        return _result(False, None, "refused", f"error: {e}")


def _add(conn, events, t, why, raw, *, origin, channel, reason, horizon, levels, thesis_id,
         rec_id, slate_id, replaces, replace_reason):
    actor = origin if origin in ORIGINS else "system"
    if origin not in ORIGINS:
        why = f"origin must be one of {ORIGINS}"
        if raw:
            _log(conn, events, "refused", actor, raw, reason=why, slate_id=slate_id, channel=channel)
        return _result(False, None, "refused", why)
    if not t:
        if raw:
            _log(conn, events, "refused", actor, raw, reason=why, slate_id=slate_id, channel=channel,
                 detail={"requested_reason": reason})
        return _result(False, None, "refused", why)
    new_levels = _norm_levels(levels, origin)

    # ── already actively watched → merge ──
    cur = _shape(_active_row(conn, t))
    if cur:
        if origin == "qwen":
            return _result(True, cur, "exists", f"{t} is already watched")
        extra = dict(cur.get("extra") or {})
        sets, args, changed = [], [], []
        if levels is not None and new_levels:
            sets.append("levels_json=?")
            args.append(json.dumps(_merge_levels(cur.get("levels"), new_levels, origin)))
            changed.append("levels")
        if reason and reason != cur.get("reason"):
            # auto-generated sync text ("added in the Alpaca app …") never overwrites
            # words Nico wrote himself — it goes to notes like another actor's reason
            if not cur.get("reason") or (cur["origin"] == origin and channel != "alpaca_app"):
                sets.append("reason=?")
                args.append(reason[:500])
            else:
                notes = list(extra.get("notes") or [])[-19:]
                notes.append({"by": origin, "reason": reason[:300], "date": _now_iso(), "slate_id": slate_id})
                extra["notes"] = notes
            changed.append("reason")
        if origin == "nico" and cur["origin"] == "opus":
            sets.append("origin=?")
            args.append("nico")
            extra.update(prev_origin="opus", claimed_by_nico=_now_iso())
            changed.append("origin→nico")
        for col, val in (("thesis_id", thesis_id), ("rec_id", rec_id), ("slate_id", slate_id)):
            if val and val != cur.get(col):
                sets.append(f"{col}=?")
                args.append(val)
                changed.append(col)
        if extra != (cur.get("extra") or {}):
            sets.append("extra_json=?")
            args.append(json.dumps(extra))
        if sets:
            conn.execute(f"UPDATE watchlist SET {', '.join(sets)} WHERE id=?", (*args, cur["id"]))
            _log(conn, events, "update", origin, t, cur["id"], reason or "re-added (already watched)",
                 slate_id, {"changed": changed, "levels": new_levels, "via": "add"}, channel=channel)
        return _result(True, _shape(_row_by_id(conn, cur["id"])), "exists",
                       f"{t} is already watched" + (f" (updated {', '.join(changed)})" if changed else ""))

    # ── qwen → proposal only (A2) ──
    if origin == "qwen":
        prop = _shape(_proposed_row(conn, t))
        if prop:
            return _result(True, prop, "exists", f"{t} is already suggested")
        iid = _insert(conn, t, origin="qwen", channel=channel or "desk", status="proposed",
                      horizon=horizon, reason=reason, levels=new_levels, thesis_id=thesis_id,
                      rec_id=rec_id, slate_id=slate_id)
        _log(conn, events, "propose", "qwen", t, iid, reason, slate_id, {"horizon": horizon}, channel=channel)
        return _result(True, _shape(_row_by_id(conn, iid)), "proposed",
                       f"{t} suggested (not watched until Nico or Mason promotes it)")

    cap = _cap()
    n = _count_active(conn)
    extra = {}
    prop = _proposed_row(conn, t)
    if prop is not None:            # a qwen suggestion for the same name gets superseded
        _mark_removed(conn, prop["id"], by=origin, reason=f"superseded by {origin} add")
        extra["superseded_proposal"] = prop["id"]

    if n < cap:
        if replaces:
            extra["replaces_ignored"] = f"{_raw_ticker(replaces)} kept — list not full ({n}/{cap})"
        iid = _insert(conn, t, origin=origin, channel=channel, status="active", horizon=horizon,
                      reason=reason, levels=new_levels, thesis_id=thesis_id, rec_id=rec_id,
                      slate_id=slate_id, extra=extra)
        _log(conn, events, "add", origin, t, iid, reason, slate_id,
             dict(extra, horizon=horizon, levels=new_levels, count=f"{n + 1}/{cap}"), channel=channel)
        return _result(True, _shape(_row_by_id(conn, iid)), "add", f"{t} added ({n + 1}/{cap})")

    # ── AT CAP ──
    if origin == "opus":
        rep = _norm_ticker(replaces)
        rrow = _shape(_active_row(conn, rep)) if rep else None
        if not rep:
            why = (f"watchlist full ({n}/{cap}): Mason must name one of its own items in "
                   "'replaces' to swap out")
        elif rrow is None:
            why = f"watchlist full ({n}/{cap}): {rep} is not an active watch item to swap out"
        elif rrow["origin"] != "opus":
            why = f"watchlist full ({n}/{cap}): {rep} is Nico's item — Mason can never replace it"
        else:
            why = ""
        if why:
            if prop is not None:    # undo the supersede — nothing was added
                conn.execute("UPDATE watchlist SET status='proposed', removed_date=NULL, removed_by=NULL, "
                             "remove_reason=NULL WHERE id=?", (prop["id"],))
            _log(conn, events, "refused", "opus", t, None, why, slate_id,
                 {"replaces": replaces, "replace_reason": replace_reason, "requested_reason": reason},
                 channel=channel)
            return _result(False, None, "refused", why)
        iid = _insert(conn, t, origin="opus", channel=channel, status="active", horizon=horizon,
                      reason=reason, levels=new_levels, thesis_id=thesis_id, rec_id=rec_id,
                      slate_id=slate_id, extra=dict(extra, replaced=rep))
        rr = replace_reason or f"swapped out for {t}"
        _mark_removed(conn, rrow["id"], by="opus", reason=rr, replaced_by=iid)
        det = {"added": t, "added_id": iid, "dropped": rep, "dropped_id": rrow["id"],
               "why": rr, "add_reason": reason, "count": f"{n}/{cap}"}
        _log(conn, events, "swap", "opus", t, iid, reason, slate_id, det, channel=channel)
        _log(conn, events, "swap", "opus", rep, rrow["id"], rr, slate_id, det, channel=channel,
             activity=False)
        return _result(True, _shape(_row_by_id(conn, iid)), "swap", f"{t} swapped in for {rep}",
                       evicted=_shape(_row_by_id(conn, rrow["id"])))

    # origin nico at cap: evict Mason's quietest, longest-watched item (A1)
    victim = _shape(_eviction_victim(conn))
    if victim is None:
        why = (f"watchlist full ({n}/{cap}) and every item is yours — remove one first "
               f"to add {t}")
        if prop is not None:
            conn.execute("UPDATE watchlist SET status='proposed', removed_date=NULL, removed_by=NULL, "
                         "remove_reason=NULL WHERE id=?", (prop["id"],))
        _log(conn, events, "refused", "nico", t, None, why, slate_id, {"requested_reason": reason},
             channel=channel)
        return _result(False, None, "refused", why)
    iid = _insert(conn, t, origin="nico", channel=channel, status="active", horizon=horizon,
                  reason=reason, levels=new_levels, thesis_id=thesis_id, rec_id=rec_id,
                  slate_id=slate_id, extra=dict(extra, evicted=victim["ticker"]))
    ev_reason = (f"evicted to make room for Nico's {t} (cap {cap}): fewest events "
                 f"({victim.get('event_count') or 0}), watched since {(victim.get('added_date') or '')[:10]}")
    _mark_removed(conn, victim["id"], by="system", reason=ev_reason, replaced_by=iid)
    _log(conn, events, "evict", "system", victim["ticker"], victim["id"], ev_reason, slate_id,
         {"for": t, "for_id": iid, "event_count": victim.get("event_count") or 0}, channel=channel)
    _log(conn, events, "add", "nico", t, iid, reason, slate_id,
         {"horizon": horizon, "levels": new_levels, "evicted": victim["ticker"], "count": f"{n}/{cap}"},
         channel=channel)
    return _result(True, _shape(_row_by_id(conn, iid)), "evict_add",
                   f"{t} added; list was full so Mason's {victim['ticker']} was dropped",
                   evicted=_shape(_row_by_id(conn, victim["id"])))


# ── propose (Nico suggests; not watched until promoted) ─────────────────────────

def propose(ticker, *, actor="nico", channel, reason="", ref=None) -> dict:
    """Nico proposes a name (the Scanners page Propose button, RULINGS 14): status 'proposed',
    origin 'nico', log action 'propose' (actor nico). Not counted toward the cap and not watched
    until promote() (whose cap rules then apply). An existing active OR proposed row for the
    ticker (whoever's) → 'exists', untouched. ref: optional dict kept in extra_json["ref"]
    (e.g. {"source": "scanner", "scan_id", "hit_id"}). Returns {"ok","item","action":
    "proposed"|"exists"|"refused","evicted","reason"}. Never raises."""
    if not enabled():
        return _result(False, None, "refused", "watchlist disabled (watchlist.enabled is off)")
    try:
        actor = str(actor or "").strip().lower()
        t, why = _validate(ticker)
        return _mutate(_propose, t, why, _raw_ticker(ticker)[:12], actor=actor,
                       channel=_norm_channel(channel), reason=re.sub(r"\s+", " ", str(reason or "")).strip(),
                       ref=ref if isinstance(ref, dict) else None)
    except Exception as e:
        log.warning(f"[watchlist] propose {ticker} failed: {e}")
        return _result(False, None, "refused", f"error: {e}")


def _propose(conn, events, t, why, raw, *, actor, channel, reason, ref):
    if actor != "nico":
        why = "only Nico proposes through this path (qwen suggests via add)"
        if raw:
            _log(conn, events, "refused", actor if actor in ACTORS else "system", raw, reason=why,
                 detail={"attempt": "propose", "actor": actor}, channel=channel)
        return _result(False, None, "refused", why)
    if not t:
        if raw:
            _log(conn, events, "refused", "nico", raw, reason=why,
                 detail={"attempt": "propose", "requested_reason": reason}, channel=channel)
        return _result(False, None, "refused", why)
    cur = _shape(_active_row(conn, t))
    if cur:
        return _result(True, cur, "exists", f"{t} is already watched")
    prop = _shape(_proposed_row(conn, t))
    if prop:
        return _result(True, prop, "exists", f"{t} is already suggested")
    extra = {"ref": ref} if ref else {}
    iid = _insert(conn, t, origin="nico", channel=channel, status="proposed", horizon="swing",
                  reason=reason, levels=[], extra=extra)
    _log(conn, events, "propose", "nico", t, iid, reason, None, dict(extra, horizon="swing"),
         channel=channel)
    return _result(True, _shape(_row_by_id(conn, iid)), "proposed",
                   f"{t} suggested (not watched until promoted)")


# ── remove / update_levels / promote / note_event ───────────────────────────────

def remove(ticker, *, actor, reason="", slate_id=None) -> dict:
    """Remove an active (or dismiss a proposed) item. nico: anything; opus: only opus/qwen
    items; system: never a nico item; qwen: nothing. Returns {"ok","item","action","reason"}
    with action 'remove'|'refused'|'not_found'. Never raises."""
    if not enabled():
        return {"ok": False, "item": None, "action": "refused", "reason": "watchlist disabled"}
    try:
        return _mutate(_remove, _norm_ticker(ticker), str(actor or "").strip().lower(),
                       re.sub(r"\s+", " ", str(reason or "")).strip(), slate_id)
    except Exception as e:
        log.warning(f"[watchlist] remove {ticker} failed: {e}")
        return {"ok": False, "item": None, "action": "refused", "reason": f"error: {e}"}


def _remove(conn, events, t, actor, reason, slate_id):
    row = _shape(_active_row(conn, t) or _proposed_row(conn, t)) if t else None
    if row is None:
        return {"ok": False, "item": None, "action": "not_found", "reason": f"{t or 'that'} is not on the watchlist"}
    if actor not in ACTORS:
        why = f"actor must be one of {ACTORS}"
    elif actor == "qwen":
        why = "qwen can only suggest, not remove"
    elif row["origin"] == "nico" and actor != "nico":
        why = f"{t} is Nico's item — only Nico can remove it"
    else:
        why = ""
    if why:
        _log(conn, events, "refused", actor if actor in ACTORS else "system", t, row["id"], why, slate_id,
             {"attempt": "remove", "requested_reason": reason})
        return {"ok": False, "item": row, "action": "refused", "reason": why}
    _mark_removed(conn, row["id"], by=actor, reason=reason or f"removed by {actor}")
    _log(conn, events, "remove", actor, t, row["id"], reason, slate_id,
         {"origin": row["origin"], "was": row["status"]})
    return {"ok": True, "item": _shape(_row_by_id(conn, row["id"])), "action": "remove",
            "reason": f"{t} removed"}


def update_levels(ticker, levels, *, actor, slate_id=None) -> dict:
    """Replace the levels set_by=actor on an active item; the other actor's are kept.
    actor: nico | opus. Never raises."""
    if not enabled():
        return {"ok": False, "item": None, "action": "refused", "reason": "watchlist disabled"}
    try:
        return _mutate(_update_levels, _norm_ticker(ticker), levels, str(actor or "").strip().lower(),
                       slate_id)
    except Exception as e:
        log.warning(f"[watchlist] update_levels {ticker} failed: {e}")
        return {"ok": False, "item": None, "action": "refused", "reason": f"error: {e}"}


def _update_levels(conn, events, t, levels, actor, slate_id):
    if actor not in ("nico", "opus"):
        why = "only nico or opus set levels"
        if t:
            _log(conn, events, "refused", actor if actor in ACTORS else "system", t, None, why, slate_id,
                 {"attempt": "update_levels", "actor": actor})
        return {"ok": False, "item": None, "action": "refused", "reason": why}
    row = _shape(_active_row(conn, t)) if t else None
    if row is None:
        return {"ok": False, "item": None, "action": "not_found", "reason": f"{t or 'that'} is not watched"}
    new = _norm_levels(levels, actor)
    merged = _merge_levels(row.get("levels"), new, actor)
    conn.execute("UPDATE watchlist SET levels_json=? WHERE id=?", (json.dumps(merged), row["id"]))
    _log(conn, events, "update", actor, t, row["id"], "levels", slate_id,
         {"changed": ["levels"], "levels": new, "via": "update_levels"})
    return {"ok": True, "item": _shape(_row_by_id(conn, row["id"])), "action": "update",
            "reason": f"{t} levels updated ({len(new)} from {actor})"}


def promote(ticker, *, actor) -> dict:
    """proposed (qwen's add or Nico's propose()) → active, owned by `actor` (nico|opus). Cap rules apply: nico
    evicts per A1; opus at the cap is refused (swaps go through apply_director_watchlist).
    Never raises."""
    if not enabled():
        return _result(False, None, "refused", "watchlist disabled")
    try:
        return _mutate(_promote, _norm_ticker(ticker), str(actor or "").strip().lower())
    except Exception as e:
        log.warning(f"[watchlist] promote {ticker} failed: {e}")
        return _result(False, None, "refused", f"error: {e}")


def _promote(conn, events, t, actor):
    if actor not in ("nico", "opus"):
        why = "only Nico or Mason can promote a suggestion"
        if t:
            _log(conn, events, "refused", actor if actor in ACTORS else "system", t, None, why, None,
                 {"attempt": "promote", "actor": actor})
        return _result(False, None, "refused", why)
    cur = _shape(_active_row(conn, t)) if t else None
    if cur:
        return _result(True, cur, "exists", f"{t} is already watched")
    prop = _shape(_proposed_row(conn, t)) if t else None
    if prop is None:
        return _result(False, None, "not_found", f"no suggestion for {t or 'that'}")
    cap = _cap()
    n = _count_active(conn)
    evicted = None
    if n >= cap:
        victim = _shape(_eviction_victim(conn)) if actor == "nico" else None
        if victim is None:
            why = (f"watchlist full ({n}/{cap}) and every item is yours — remove one first"
                   if actor == "nico" else
                   f"watchlist full ({n}/{cap}): Mason must swap via the director watchlist field")
            _log(conn, events, "refused", actor, t, prop["id"], why, None, {"attempt": "promote"})
            return _result(False, prop, "refused", why)
        ev_reason = (f"evicted to make room for Nico's {t} (cap {cap}): fewest events "
                     f"({victim.get('event_count') or 0}), watched since {(victim.get('added_date') or '')[:10]}")
        _mark_removed(conn, victim["id"], by="system", reason=ev_reason, replaced_by=prop["id"])
        _log(conn, events, "evict", "system", victim["ticker"], victim["id"], ev_reason, None,
             {"for": t, "for_id": prop["id"], "event_count": victim.get("event_count") or 0})
        evicted = _shape(_row_by_id(conn, victim["id"]))
    extra = dict(prop.get("extra") or {}, proposed_by=prop["origin"], proposed_date=prop.get("added_date"))
    conn.execute("UPDATE watchlist SET status='active', origin=?, added_date=?, added_by=?, extra_json=? "
                 "WHERE id=?", (actor, _now_iso(), actor, json.dumps(extra), prop["id"]))
    _log(conn, events, "promote", actor, t, prop["id"], prop.get("reason") or "", None,
         {"proposed_by": prop["origin"], "evicted": evicted["ticker"] if evicted else None})
    return _result(True, _shape(_row_by_id(conn, prop["id"])), "promote", f"{t} promoted", evicted=evicted)


def note_event(ticker, when_iso) -> None:
    """Bump event_count + last_event_date on the active item (SIGNALS calls it per event).
    Bookkeeping only — not a watchlist decision, so no log row. Never raises."""
    if not enabled():
        return None
    try:
        t = _norm_ticker(ticker)
        if not t:
            return None
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE watchlist SET event_count=COALESCE(event_count,0)+1, last_event_date=? "
                    "WHERE ticker=? AND status='active' AND record_deleted=0",
                    (str(when_iso or _now_iso()), t))
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[watchlist] note_event {ticker} failed: {e}")
    return None


# ── director (Opus JSON "watchlist" field, contracts §6) ────────────────────────

def apply_director_watchlist(items: list, *, slate_id: str, allowed_tickers: set | None = None) -> dict:
    """Apply Opus's watchlist entries: drops first, then level updates, then adds (so a
    drop frees a slot for an add in the same slate). Returns
    {"added":[T], "dropped":[T], "swapped":[{"added","dropped","why"}], "updated":[T],
     "refused":[{"ticker","why"}]}. Never raises."""
    out = {"added": [], "dropped": [], "swapped": [], "updated": [], "refused": []}
    if not enabled() or not isinstance(items, list):
        return out
    try:
        allowed = {str(a).strip().upper() for a in allowed_tickers} if allowed_tickers is not None else None
        drops, updates, adds = [], [], []
        for it in items[:60]:
            if not isinstance(it, dict):
                continue
            act = str(it.get("action") or "").strip().lower()
            t = _raw_ticker(it.get("ticker"))
            if act in ("drop", "remove"):
                drops.append((t, it))
            elif act == "update":
                updates.append((t, it))
            elif act == "add":
                adds.append((t, it))
            else:
                out["refused"].append({"ticker": t or "?", "why": f"unknown action '{act}'"})

        def _refuse(t, why):
            out["refused"].append({"ticker": t or "?", "why": why})

        for t, it in drops:
            r = remove(t, actor="opus", reason=str(it.get("reason") or ""), slate_id=slate_id)
            if r.get("ok"):
                out["dropped"].append(r["item"]["ticker"])
            else:
                _refuse(t, r.get("reason") or "drop failed")
        for t, it in updates:
            if not isinstance(it.get("levels"), list):
                _refuse(t, "update without a levels list")
                continue
            r = update_levels(t, it.get("levels"), actor="opus", slate_id=slate_id)
            if r.get("ok"):
                out["updated"].append(r["item"]["ticker"])
            else:
                _refuse(t, r.get("reason") or "update failed")
        for t, it in adds:
            if allowed is not None and t not in allowed:
                _refuse(t, "not in the director's ticker allow-list")
                continue
            r = add(t, origin="opus", channel="director", reason=str(it.get("reason") or ""),
                    horizon=it.get("horizon") or "swing", levels=it.get("levels"),
                    thesis_id=it.get("thesis_id"), rec_id=it.get("rec_id"), slate_id=slate_id,
                    replaces=it.get("replaces"), replace_reason=str(it.get("replace_reason") or ""))
            a = r.get("action")
            if r.get("ok") and a == "add":
                out["added"].append(r["item"]["ticker"])
            elif r.get("ok") and a == "swap":
                out["swapped"].append({"added": r["item"]["ticker"],
                                       "dropped": (r.get("evicted") or {}).get("ticker"),
                                       "why": (r.get("evicted") or {}).get("remove_reason")
                                       or str(it.get("replace_reason") or "")})
            elif r.get("ok") and a == "exists":
                tk = r["item"]["ticker"]
                if tk not in out["updated"]:
                    out["updated"].append(tk)
            else:
                _refuse(t, r.get("reason") or "add refused")
    except Exception as e:
        log.warning(f"[watchlist] apply_director_watchlist failed: {e}")
    return out


# ── universe (what the tape streams) ────────────────────────────────────────────

def _underlying(sym, asset_class=None) -> str | None:
    s = _raw_ticker(sym)
    m = _OCC_RE.match(s)
    if m:
        return m.group(1)
    if str(asset_class or "") == "us_option":
        return None
    return s if _TICKER_RE.match(s) else None


def position_symbols() -> set:
    """Tickers of open positions: Alpaca paper (option contracts → their underlying) ∪
    personal_positions. ≤30s cache. Never raises."""
    if not enabled():
        return set()
    now = time.time()
    if _pos_cache["data"] is not None and now - _pos_cache["ts"] < _CACHE_S:
        return set(_pos_cache["data"])
    syms = set()
    # a FAILED source keeps its last good set (the tape must not briefly unsubscribe
    # positions because one GET timed out); a successful empty answer does clear it
    try:
        raw = _alpaca_positions()
        if raw is None:
            raise RuntimeError("alpaca positions fetch failed")
        got = set()
        for p in raw:
            if isinstance(p, dict):
                u = _underlying(p.get("symbol") or p.get("ticker"), p.get("asset_class"))
                if u:
                    got.add(u)
        _pos_last_good["alpaca"] = got
    except Exception as e:
        log.debug(f"[watchlist] alpaca positions unavailable: {e}")
    syms |= _pos_last_good["alpaca"] or set()
    try:
        got = set()
        for p in _personal_positions() or []:
            t = _raw_ticker((p or {}).get("ticker"))
            if t not in _NOT_POSITIONS and _TICKER_RE.match(t):
                got.add(t)
        _pos_last_good["personal"] = got
    except Exception as e:
        log.debug(f"[watchlist] personal positions unavailable: {e}")
    syms |= _pos_last_good["personal"] or set()
    _pos_cache.update(ts=now, data=set(syms))
    return syms


def universe() -> dict:
    """{ticker: {"role": "item"|"position"|"infra", "origin", "item_id", "held"}} — every
    symbol the tape must stream. Precedence item > position > infra; `held` marks items
    that are also open positions. ≤30s cache (busted by any mutation). Never raises."""
    if not enabled():
        return {}
    try:
        now = time.time()
        if _uni_cache["data"] is not None and now - _uni_cache["ts"] < _CACHE_S:
            return {k: dict(v) for k, v in _uni_cache["data"].items()}
        out = {}
        for s in _cfg().get("infra_symbols") or []:
            t = _norm_ticker(s)
            if t:
                out[t] = {"role": "infra", "origin": None, "item_id": None, "held": False}
        pos = position_symbols()
        for t in sorted(pos):
            out[t] = {"role": "position", "origin": None, "item_id": None, "held": True}
        for it in list_items("active"):
            if it.get("origin") in _COUNTED:
                out[it["ticker"]] = {"role": "item", "origin": it["origin"], "item_id": it["id"],
                                     "held": it["ticker"] in pos}
        _uni_cache.update(ts=now, data=out)
        return {k: dict(v) for k, v in out.items()}
    except Exception as e:
        log.warning(f"[watchlist] universe failed: {e}")
        return {}


# ── seed (A5) ───────────────────────────────────────────────────────────────────

def _link_thesis(t, item_id, words) -> str | None:
    """Create Nico's long-term thesis for a seeded item and link it. Never raises."""
    try:
        import mason_theses as TH
        if not TH.enabled():
            return None
        review_by = (datetime.now().date() + timedelta(days=365)).isoformat()
        res = TH.create_thesis(f"{t} long-term thesis (Nico)", words, [t], "bullish",
                               _SEED_SIGNALS.get(t), origin="nico", review_by=review_by,
                               extra={"watchlist_item_id": item_id}, created_by="nico")
        if not res.get("ok"):
            log.info(f"[watchlist] seed thesis for {t} not created: {res.get('error')}")
            return None
        tid = res["thesis"]["id"]

        def _do(conn, events):
            conn.execute("UPDATE watchlist SET thesis_id=? WHERE id=?", (tid, item_id))
            _log(conn, events, "update", "nico", t, item_id, "linked Nico's long-term thesis", None,
                 {"changed": ["thesis_id"], "thesis_id": tid, "via": "seed"}, channel="seed")
        _mutate(_do)
        return tid
    except Exception as e:
        log.warning(f"[watchlist] seed thesis {t} failed: {e}")
        return None


def seed_nico_defaults() -> dict:
    """Idempotent: HOOD, IONQ, RKLB, SPCX as Nico items (channel seed, horizon long,
    reason = his words) + a mason_theses thesis each (origin nico) when theses are on.
    A ticker ever seeded before (even if Nico later removed it) is never re-added.
    Returns {"ok","added","claimed","skipped","theses","errors"}. Never raises."""
    out = {"ok": False, "added": [], "claimed": [], "skipped": [], "theses": [], "errors": []}
    if not enabled():
        out["reason"] = "watchlist disabled"
        return out
    try:
        for t, words in SEED_REASONS.items():
            conn = mason_db.get_connection()
            try:
                seeded = conn.execute("SELECT 1 FROM watchlist WHERE ticker=? AND channel='seed' "
                                      "AND record_deleted=0 LIMIT 1", (t,)).fetchone() is not None
                cur = _shape(_active_row(conn, t))
            finally:
                conn.close()
            item = None
            if seeded or (cur and cur["origin"] == "nico"):
                out["skipped"].append(t)
                item = cur
            else:
                r = add(t, origin="nico", channel="seed", reason=words, horizon="long")
                if r.get("ok") and r.get("action") in ("add", "evict_add"):
                    out["added"].append(t)
                elif r.get("ok"):
                    out["claimed"].append(t)
                else:
                    out["errors"].append({"ticker": t, "why": r.get("reason")})
                item = r.get("item")
            if item and item.get("status") == "active" and not item.get("thesis_id"):
                tid = _link_thesis(t, item["id"], words)
                if tid:
                    out["theses"].append({"ticker": t, "thesis_id": tid})
        out["ok"] = True
    except Exception as e:
        log.warning(f"[watchlist] seed failed: {e}")
        out["errors"].append({"ticker": "", "why": str(e)})
    return out


# ── Alpaca two-way sync (R13) ───────────────────────────────────────────────────

def _get_state(conn, key):
    row = conn.execute("SELECT value FROM desk_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key, value):
    conn.execute(
        "INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
        (key, str(value), _now_iso()))


def _desired_lists() -> dict:
    mason_name, nico_name = _list_names()
    items = list_items("active")
    return {mason_name: sorted({i["ticker"] for i in items if i["origin"] == "opus"}),
            nico_name: sorted({i["ticker"] for i in items if i["origin"] == "nico"})}


def _fetch_lists() -> dict | None:
    """{name: {"id", "symbols"}} for OUR two lists on the paper account; None on failure."""
    code, d = _alpaca_get("/watchlists")
    if code != 200 or not isinstance(d, list):
        return None
    names = set(_list_names())
    out = {}
    for w in d:
        if not isinstance(w, dict) or w.get("name") not in names or w.get("name") in out:
            continue
        code2, d2 = _alpaca_get(f"/watchlists/{w.get('id')}")
        if code2 != 200 or not isinstance(d2, dict):
            return None
        syms = [_raw_ticker(a.get("symbol")) for a in (d2.get("assets") or []) if isinstance(a, dict)]
        out[w["name"]] = {"id": w.get("id"), "symbols": [s for s in syms if s]}
    return out


def _save_list_snapshot(name, syms=None, *, failed=None) -> None:
    """Record ONE list's push outcome in the snapshot the moment it is known (REVIEW_A #1).
    Success → snap[name] = syms and the list is clean. Failure → the symbols we TRIED join
    snap["dirty"][name]: the app state of that list is unknown (the push may even have
    applied with a lost reply), so sync_pull never infers a Nico removal from it."""
    events = []
    conn = mason_db.get_connection()
    try:
        with conn:
            snap = _loads(_get_state(conn, _SNAP_KEY), {})
            dirty = dict(snap.get("dirty") or {})
            if failed is None:
                changed = snap.get(name) != syms
                snap[name] = list(syms)
                dirty.pop(name, None)
            else:
                changed = False
                dirty[name] = sorted(set(dirty.get(name) or []) | set(failed))
            snap["dirty"] = dirty
            snap["ts"] = _now_iso()
            _set_state(conn, _SNAP_KEY, json.dumps(snap))
            if changed:
                _log(conn, events, "sync_out", "system", name[:60], None, "pushed to Alpaca", None,
                     {"list": name, "symbols": list(syms)})
    finally:
        conn.close()
    _after(events)


def sync_push() -> dict:
    """Make the Alpaca paper lists "Mason" (opus items) / "Nico" (nico items) equal the DB
    active items. The snapshot is saved PER LIST as each push succeeds (a failed list is
    marked dirty instead), so a partial failure can never masquerade as Nico's change on
    the next pull. Never raises."""
    if not _sync_on():
        return {"ok": False, "skipped": "alpaca_sync disabled"}
    try:
        with _SYNC_LOCK:
            lists = _fetch_lists()
            if lists is None:
                return {"ok": False, "error": "could not read Alpaca watchlists"}
            desired = _desired_lists()
            errors = []
            for name, syms in desired.items():
                have = lists.get(name)
                if have and sorted(have["symbols"]) == syms:
                    _save_list_snapshot(name, syms)
                    continue
                tried = list(syms)
                code, d = (_alpaca_put(f"/watchlists/{have['id']}", {"name": name, "symbols": syms})
                           if have else _alpaca_post("/watchlists", {"name": name, "symbols": syms}))
                if code not in (200, 201, 204):
                    # one symbol Alpaca rejects must not block the whole list
                    ok_syms = [s for s in syms if _asset_known(s) is not False]
                    if ok_syms != syms:
                        code, d = (_alpaca_put(f"/watchlists/{have['id']}", {"name": name, "symbols": ok_syms})
                                   if have else _alpaca_post("/watchlists", {"name": name, "symbols": ok_syms}))
                        desired[name] = ok_syms
                if code in (200, 201, 204):
                    _save_list_snapshot(name, desired[name])
                else:
                    _save_list_snapshot(name, failed=tried)
                    errors.append(f"{name}: {code} {str(d)[:120]}")
            if errors:
                return {"ok": False, "error": "; ".join(errors), "pushed": desired}
            return {"ok": True, "pushed": desired}
    except Exception as e:
        log.warning(f"[watchlist] sync_push failed: {e}")
        return {"ok": False, "error": str(e)}


def sync_pull() -> dict:
    """Diff the Alpaca lists vs the last pushed snapshot: symbols Nico added in the app
    (either list) → add(origin nico, channel alpaca_app); symbols he removed → remove(actor
    nico). Then sync_push() so the app mirrors the DB (DB wins). Never raises."""
    if not _sync_on():
        return {"ok": False, "skipped": "alpaca_sync disabled"}
    out = {"ok": False, "added": [], "removed": [], "refused": []}
    try:
        with _SYNC_LOCK:
            lists = _fetch_lists()
            if lists is None:
                out["error"] = "could not read Alpaca watchlists"
                return out
            mason_name, nico_name = _list_names()
            conn = mason_db.get_connection()
            try:
                snap = _loads(_get_state(conn, _SNAP_KEY), {}) or None
                active = {r["ticker"] for r in conn.execute(
                    "SELECT ticker FROM watchlist WHERE status='active' AND record_deleted=0").fetchall()}
            finally:
                conn.close()
            adds, removes = [], set()     # adds: (symbol, list name)
            if snap is None:              # first run: nothing to diff against — adopt app extras
                for name in (nico_name, mason_name):
                    for s in (lists.get(name) or {}).get("symbols") or []:
                        if s not in active and s not in {a for a, _ in adds}:
                            adds.append((s, name))
            else:
                # A list whose last push failed ("dirty") has an UNKNOWN app state — our own
                # half-applied push must never read as "Nico removed it" (REVIEW_A #1), so
                # removals come only from clean lists, and never for a symbol we were still
                # trying to push anywhere.
                dirty = {k: set(v or []) for k, v in (snap.get("dirty") or {}).items()}
                pending = set().union(*dirty.values()) if dirty else set()
                present = [n for n in (mason_name, nico_name) if n in lists]
                clean = [n for n in present if n not in dirty]
                app_all = set().union(*[set(lists[n]["symbols"]) for n in present]) if present else set()
                last_all = set().union(*[set(snap.get(n) or []) for n in clean]) if clean else set()
                removes = {s for s in last_all - app_all - pending if s in active}
                for name in (nico_name, mason_name):
                    if name not in lists:
                        continue
                    known = set(snap.get(name) or []) | dirty.get(name, set())
                    for s in set(lists[name]["symbols"]) - known:
                        if s in {a for a, _ in adds}:
                            continue
                        if name == mason_name and s in active:
                            continue      # already watched; putting it on Mason's list isn't a claim
                        adds.append((s, name))
            _tl.no_push = True
            try:
                for s in sorted(removes):
                    r = remove(s, actor="nico", reason="removed in the Alpaca app")
                    (out["removed"] if r.get("ok") else out["refused"]).append(
                        s if r.get("ok") else {"ticker": s, "why": r.get("reason")})
                for s, name in adds:
                    r = add(s, origin="nico", channel="alpaca_app",
                            reason=f"added in the Alpaca app ({name} list)")
                    if r.get("ok"):
                        out["added"].append(s)
                    else:
                        out["refused"].append({"ticker": s, "why": r.get("reason")})
                if out["added"] or out["removed"] or out["refused"]:
                    def _do(conn, events):
                        _log(conn, events, "sync_in", "nico",
                             ",".join(out["added"] + out["removed"])[:60] or "alpaca",
                             None, "Alpaca app changes", None,
                             {"added": out["added"], "removed": out["removed"], "refused": out["refused"]},
                             channel="alpaca_app")
                    _mutate(_do)
            finally:
                _tl.no_push = False
        out["push"] = sync_push()
        out["ok"] = True
    except Exception as e:
        log.warning(f"[watchlist] sync_pull failed: {e}")
        out["error"] = str(e)
    return out


# ── chat tools (R15; integrator registers them) ─────────────────────────────────
_LEVELS_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "properties": {
        "price": {"type": "number"},
        "side": {"type": "string", "enum": list(SIDES),
                 "description": "alert when price crosses above / below it"},
        "label": {"type": "string", "description": "e.g. breakout, stop, support"}},
        "required": ["price", "side"]},
    "description": "Optional price levels to watch",
}

CHAT_TOOLS = [
    {
        "name": "watchlist_show",
        "description": (
            "Show Mason's price WATCHLIST — the names being watched live (every minute), who put "
            "each there (Nico or Mason), why, and any price levels; plus qwen's suggestions and "
            "the positions/index ETFs auto-watched outside the 25-item cap. Call when Nico asks "
            "what's on the watchlist / what Mason is watching."
        ),
        "input_schema": {"type": "object", "properties": {
            "include_removed": {"type": "boolean", "description": "Also list recently removed items"}},
            "required": []},
    },
    {
        "name": "watchlist_add",
        "description": (
            "Add a ticker to the live price WATCHLIST for Nico (counts as Nico's pick; Mason can "
            "never remove it). ONLY when Nico explicitly asks to watch / add / keep an eye on a "
            "ticker's price. At the 25-item cap one of Mason's own items is dropped to make room. "
            "For a news hypothesis to track, use add_watch_thesis instead."
        ),
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string", "description": "Ticker symbol, e.g. HOOD"},
            "reason": {"type": "string", "description": "Why Nico wants it watched, in his words"},
            "horizon": {"type": "string", "enum": list(HORIZONS)},
            "levels": _LEVELS_SCHEMA},
            "required": ["ticker"]},
    },
    {
        "name": "watchlist_remove",
        "description": "Remove a ticker from the watchlist (or dismiss a suggestion) because Nico asked to.",
        "input_schema": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "reason": {"type": "string", "description": "Why, if Nico said"}},
            "required": ["ticker"]},
    },
]


def _levels_text(levels) -> str:
    return ", ".join(f"{l['side']} {l['price']:g}" + (f" ({l['label']}, {l['set_by']})" if l.get("label")
                                                       else f" ({l['set_by']})")
                     for l in levels or [])


_PROPOSER = {"nico": "Nico", "opus": "Mason", "qwen": "qwen"}     # watchlist_show suggestion rows


def _item_line(it) -> str:
    badge = "You" if it["origin"] == "nico" else ("Mason" if it["origin"] == "opus" else "qwen")
    s = f"- {it['ticker']} [{badge} · {it.get('horizon') or 'swing'}]"
    if it.get("reason"):
        s += f" why: {it['reason']}"
    if it.get("levels"):
        s += f" · levels: {_levels_text(it['levels'])}"
    if it.get("event_count"):
        s += f" · {it['event_count']} events"
    return s


def handle_chat_tool(name: str, inp: dict | None) -> str:
    """Dispatch one of CHAT_TOOLS (origin/actor nico, channel chat). Plain text. Never raises."""
    inp = inp if isinstance(inp, dict) else {}
    try:
        if name not in {t["name"] for t in CHAT_TOOLS}:
            return f"Unknown tool {name}."
        if not enabled():
            return "The watchlist is turned off (watchlist.enabled is false)."
        if name == "watchlist_show":
            items = list_items("active")
            c = counts()
            if not items:
                s = f"The watchlist is empty (0/{c['cap']})."
            else:
                s = (f"Watchlist {c['active']}/{c['cap']} ({c['nico']} yours, {c['opus']} Mason's; "
                     f"{c['free']} free):\n" + "\n".join(_item_line(i) for i in items))
            props = list_items("proposed")
            if props:
                s += "\nSuggested (not watched until promoted):\n" + "\n".join(
                    f"- {p['ticker']} (by {_PROPOSER.get(p.get('origin'), 'qwen')})"
                    + (f": {p['reason']}" if p.get("reason") else "") for p in props)
            if inp.get("include_removed"):
                rem = list_items("removed")[-10:]
                if rem:
                    s += "\nRecently removed:\n" + "\n".join(
                        f"- {r['ticker']} by {r.get('removed_by')} ({(r.get('removed_date') or '')[:10]})"
                        + (f": {r['remove_reason']}" if r.get("remove_reason") else "") for r in rem)
            try:
                pos = sorted(position_symbols() - {i["ticker"] for i in items})
                if pos:
                    s += "\nAlso auto-watched (open positions, outside the cap): " + ", ".join(pos)
            except Exception:
                pass
            return s
        if name == "watchlist_add":
            r = add(inp.get("ticker"), origin="nico", channel="chat", reason=inp.get("reason") or "",
                    horizon=inp.get("horizon") or "swing", levels=inp.get("levels"))
            a = r.get("action")
            if not r.get("ok"):
                return f"Could not add {_raw_ticker(inp.get('ticker')) or 'that'}: {r.get('reason')}"
            t = r["item"]["ticker"]
            c = counts()
            if a == "exists":
                return f"{t} is already on the watchlist — {r.get('reason')}. It's marked as yours."
            msg = f"Watching {t} for you now ({c['active']}/{c['cap']})."
            if a == "evict_add" and r.get("evicted"):
                msg += f" The list was full, so Mason's {r['evicted']['ticker']} was dropped to make room."
            if r["item"].get("levels"):
                msg += f" Levels: {_levels_text(r['item']['levels'])}."
            return msg
        if name == "watchlist_remove":
            t = _raw_ticker(inp.get("ticker")) or "that"
            r = remove(inp.get("ticker"), actor="nico", reason=inp.get("reason") or "removed via chat")
            if not r.get("ok"):
                return f"Could not remove {t}: {r.get('reason')}"
            return f"Removed {t} from the watchlist."
    except Exception as e:
        log.warning(f"[watchlist] chat tool {name} failed: {e}")
        return f"Watchlist tool error: {e}"
    return "No-op."
