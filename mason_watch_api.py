"""
mason_watch_api.py — HTTP API for the Watchlist widget (docs/watchlist/WATCHLIST_CONTRACTS.md §7).

A Flask Blueprint the integrator registers in mason_app.py:

    import mason_watch_api
    app.register_blueprint(mason_watch_api.bp)

Routes (all JSON; every Nico mutation carries origin/actor 'nico', channel 'ui'):
    GET    /api/watchlist                      items + positions + proposed, each with
                                               stats (tape_stats, cached 30s), spark (5m
                                               closes today), events_today, latest digest;
                                               plus counts() and tape status()
    POST   /api/watchlist                      {ticker, reason, horizon, levels} → add()
    DELETE /api/watchlist/<ticker>             {reason} → remove(actor='nico')
    POST   /api/watchlist/<ticker>/levels      {levels:[...]} → update_levels(actor='nico')
    POST   /api/watchlist/<ticker>/promote     → promote(actor='nico')   (addition to §7:
                                               the widget's "Suggested" row needs it)
    POST   /api/watchlist/<ticker>/propose     {source:'scanner', scan_id, hit_id} →
                                               propose(actor='nico', channel='scanner:<title>')
                                               (S3 K2, finalization RULINGS 14/16)
    GET    /api/watchlist/<ticker>/tape?tf=    bars + events + option snaps + digests
    GET    /api/watchlist/log?limit=&ticker=   watchlist_log rows, newest first

Behaviour:
  - watchlist.enabled=false (or mason_watchlist not importable) → every route answers
    200 {"enabled": false, ...} and calls nothing that mutates.
  - Bad ticker / tf / horizon / levels → 400 {"ok": false, "error"}.
  - A refused add/promote/remove → 409 with a readable "message".
  - No exception escapes: route-level failure → 500 {"ok": false, "error"}; a single
    row's stats failure just leaves that row's stats null.
  - NaN/inf never reach the JSON (the browser's JSON.parse would reject the payload).

STORE / TAPE / SIGNALS (mason_watchlist, mason_watch_tape, mason_watch_signals) are
imported lazily per request via their public APIs only; watch_events / watch_digests /
watchlist_log are read (SELECT only) straight from mason.db, closed in `finally`.
"""

import json
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, request

import mason_db

bp = Blueprint("watch", __name__)

_ET = ZoneInfo("America/New_York")
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,3})?$")
_HORIZONS = ("intraday", "swing", "long")
_SIDES = ("above", "below")
_TFS = ("1m", "5m", "1d")
_STATS_TTL = 30.0          # seconds — tape_stats is recomputed at most this often per ticker
_DISABLED_MSG = "Watchlist is turned off (watchlist.enabled = false)."

_now = time.time           # test hook
_STATS_CACHE = {}          # ticker → (fetched_at, stats, spark)


def _reset_cache():
    _STATS_CACHE.clear()


# ── helpers ───────────────────────────────────────────────────────────────────
def _log_error(msg):
    try:
        current_app.logger.error(msg)
    except Exception:
        pass


def _mod(name):
    """Lazy guarded import of a sibling watchlist module → module or None."""
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None


def _store():
    """mason_watchlist if importable AND enabled, else None."""
    W = _mod("mason_watchlist")
    if W is None:
        return None
    try:
        return W if W.enabled() else None
    except Exception as e:
        _log_error(f"[watchlist] enabled() failed: {e}")
        return None


def _disabled(**extra):
    out = {"enabled": False, "ok": False, "message": _DISABLED_MSG}
    out.update(extra)
    return jsonify(out)


def _clean(v):
    """Recursively replace NaN/inf floats with None so the payload is valid JSON."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _bad(msg):
    return jsonify({"ok": False, "error": msg}), 400


def _norm_ticker(raw):
    """Upper-cased ticker or None when it isn't a plausible symbol."""
    if not isinstance(raw, str):
        return None
    t = raw.strip().upper()
    return t if _TICKER_RE.match(t) else None


def _norm_levels(raw):
    """Validate a levels list → (levels, error). Every level is tagged set_by='nico'."""
    if not isinstance(raw, list):
        return None, "levels must be a list"
    out = []
    for lv in raw[:20]:
        if not isinstance(lv, dict):
            return None, "each level must be an object"
        try:
            price = float(lv.get("price"))
        except (TypeError, ValueError):
            return None, "level price must be a number"
        if not math.isfinite(price) or price <= 0:
            return None, "level price must be positive"
        side = str(lv.get("side") or "").strip().lower()
        if side not in _SIDES:
            return None, "level side must be 'above' or 'below'"
        label = str(lv.get("label") or "").strip()[:40]
        out.append({"price": price, "side": side, "label": label, "set_by": "nico"})
    return out, None


def _body():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def _et_day_start_epoch():
    d = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def _safe_counts(W):
    try:
        return W.counts()
    except Exception as e:
        _log_error(f"[watchlist] counts failed: {e}")
        return {}


def _counter_text(counts):
    try:
        return f" ({counts['active']}/{counts['cap']})"
    except Exception:
        return ""


def _item_ticker(d):
    return (d or {}).get("ticker") if isinstance(d, dict) else None


def _result_message(verb, ticker, res, counts):
    """A one-line, human-readable summary of an add/promote result for the widget.
    Any result carrying `evicted` (evict_add, swap, or the STORE's promote at the cap)
    names the dropped Mason item — an eviction is always reported (D4/A1)."""
    action = res.get("action") or ""
    reason = (res.get("reason") or "").strip()
    ctr = _counter_text(counts)
    if not res.get("ok") or action == "refused":
        why = reason or "the list is full and every slot is yours — remove one first"
        return f"Couldn't {verb} {ticker}: {why}."
    ev = _item_ticker(res.get("evicted")) if res.get("evicted") else None
    if action == "exists":
        # STORE reason: "T is already watched" [+ " (updated reason, levels, origin→nico)"]
        m = re.search(r"\(updated ([^)]*)\)", reason)
        changed = [c.strip() for c in m.group(1).split(",")] if m else []
        claimed = any(c.startswith("origin") for c in changed)
        rest = [c for c in changed if not c.startswith("origin")]
        msg = f"{ticker} is already on the watchlist"
        if claimed:
            msg += " — it's now yours (was Mason's)"
        if rest:
            msg += (" and " if claimed else " — ") + "updated " + ", ".join(rest)
        if not changed:
            msg += " — nothing changed"
        return msg + "."
    if action == "proposed":
        return f"{ticker} was added as a suggestion (not watched yet)."
    if ev or action in ("evict_add", "swap"):
        return (f"Watching {ticker}{ctr}. The list was full, so Mason's {ev or 'lowest-priority item'} "
                f"was dropped (fewest events, watched longest).")
    return f"Watching {ticker}{ctr}."


def _stats_and_spark(ticker, SIG, TAPE):
    """tape_stats + today's 5m closes, cached _STATS_TTL seconds per ticker."""
    now = _now()
    hit = _STATS_CACHE.get(ticker)
    if hit and now - hit[0] < _STATS_TTL:
        return hit[1], hit[2]
    stats = None
    spark = []
    if SIG is not None:
        try:
            stats = SIG.tape_stats(ticker)
        except Exception as e:
            _log_error(f"[watchlist] tape_stats {ticker} failed: {e}")
    if TAPE is not None:
        try:
            spark = [b.get("c") for b in (TAPE.session_bars(ticker, tf="5m") or [])
                     if isinstance(b, dict) and b.get("c") is not None]
        except Exception as e:
            _log_error(f"[watchlist] spark {ticker} failed: {e}")
    _STATS_CACHE[ticker] = (now, stats, spark)
    return stats, spark


def _in_clause(tickers):
    return ",".join("?" * len(tickers))


def _events_today(tickers):
    """{ticker: [{ts,kind,severity,message}, ...]} for today's ET session, ascending."""
    out = {t: [] for t in tickers}
    if not tickers:
        return out
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT ticker, ts, kind, severity, message FROM watch_events "
            f"WHERE ts >= ? AND ticker IN ({_in_clause(tickers)}) ORDER BY ts ASC LIMIT 2000",
            [_et_day_start_epoch()] + list(tickers)).fetchall()
    finally:
        conn.close()
    for r in rows:
        out.setdefault(r["ticker"], []).append(
            {"ts": r["ts"], "kind": r["kind"], "severity": r["severity"], "message": r["message"]})
    return out


def _latest_digests(tickers):
    """{ticker: {date, slot, text, guard_ok, created_date}} — newest digest per ticker."""
    out = {}
    if not tickers:
        return out
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT ticker, date, slot, text, guard_ok, created_date FROM watch_digests "
            f"WHERE ticker IN ({_in_clause(tickers)}) "
            f"ORDER BY date DESC, created_date DESC LIMIT 1000", list(tickers)).fetchall()
    finally:
        conn.close()
    for r in rows:
        if r["ticker"] not in out:
            out[r["ticker"]] = {"date": r["date"], "slot": r["slot"], "text": r["text"],
                                "guard_ok": r["guard_ok"], "created_date": r["created_date"]}
    return out


# ── routes ────────────────────────────────────────────────────────────────────
@bp.route("/api/watchlist", methods=["GET"])
def api_watchlist():
    """Everything the Watchlist widget renders in one poll (every 30s)."""
    try:
        W = _store()
        if W is None:
            return _disabled(items=[], positions=[], proposed=[], counts={}, tape={})
        TAPE = _mod("mason_watch_tape")
        SIG = _mod("mason_watch_signals")

        items = W.list_items(status="active") or []
        try:
            proposed = W.list_items(status="proposed") or []
        except Exception as e:
            _log_error(f"[watchlist] proposed list failed: {e}")
            proposed = []
        try:
            universe = W.universe() or {}
        except Exception as e:
            _log_error(f"[watchlist] universe failed: {e}")
            universe = {}
        held = {t for t, u in universe.items() if (u or {}).get("role") == "position"}
        try:
            held |= set(W.position_symbols() or ())
        except Exception:
            pass

        item_tickers = [i.get("ticker") for i in items if i.get("ticker")]
        positions = [{"ticker": t, "origin": "position", "role": "position"}
                     for t in sorted(held) if t not in set(item_tickers)]
        rows = items + positions
        tickers = [r["ticker"] for r in rows if r.get("ticker")]
        try:
            events = _events_today(tickers)
        except Exception as e:
            _log_error(f"[watchlist] events_today failed: {e}")
            events = {}
        try:
            digests = _latest_digests(tickers)
        except Exception as e:
            _log_error(f"[watchlist] digests failed: {e}")
            digests = {}

        for r in rows:
            t = r.get("ticker")
            r["stats"], r["spark"] = _stats_and_spark(t, SIG, TAPE)
            r["events_today"] = events.get(t, [])
            r["digest"] = digests.get(t)
            r["held"] = t in held

        tape = {}
        if TAPE is not None:
            try:
                tape = TAPE.status() or {}
            except Exception as e:
                _log_error(f"[watchlist] tape status failed: {e}")
        return jsonify(_clean({"enabled": True, "items": items, "positions": positions,
                               "proposed": proposed, "counts": _safe_counts(W), "tape": tape}))
    except Exception as e:
        _log_error(f"[watchlist] list failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    """Nico adds a name from the widget → add(origin='nico', channel='ui').
    200 on add/exists/evict_add, 409 (with message) when the store refuses."""
    try:
        W = _store()
        if W is None:
            return _disabled()
        b = _body()
        ticker = _norm_ticker(b.get("ticker"))
        if not ticker:
            return _bad("invalid ticker (letters/digits, ≤6, optional .X or -X class)")
        horizon = (str(b.get("horizon") or "swing")).strip().lower()
        if horizon not in _HORIZONS:
            return _bad("horizon must be intraday, swing or long")
        levels = None
        if b.get("levels") not in (None, []):
            levels, err = _norm_levels(b.get("levels"))
            if err:
                return _bad(err)
        reason = str(b.get("reason") or "").strip()[:500]
        res = W.add(ticker, origin="nico", channel="ui", reason=reason,
                    horizon=horizon, levels=levels) or {}
        counts = _safe_counts(W)
        ok = bool(res.get("ok")) and res.get("action") != "refused"
        out = {"enabled": True, "ok": ok, "action": res.get("action"), "item": res.get("item"),
               "evicted": res.get("evicted"), "reason": res.get("reason") or "",
               "message": _result_message("add", ticker, res, counts), "counts": counts}
        _reset_cache_for(ticker)
        return jsonify(_clean(out)), (200 if ok else 409)
    except Exception as e:
        _log_error(f"[watchlist] add failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist/<ticker>", methods=["DELETE"])
def api_watchlist_remove(ticker):
    """Nico removes any item (his, Mason's, or a suggestion) → remove(actor='nico')."""
    try:
        W = _store()
        if W is None:
            return _disabled()
        t = _norm_ticker(ticker)
        if not t:
            return _bad("invalid ticker")
        reason = str(_body().get("reason") or request.args.get("reason") or "").strip()[:500]
        res = W.remove(t, actor="nico", reason=reason) or {}
        ok = bool(res.get("ok"))
        why = (res.get("reason") or "").strip()
        msg = (f"Stopped watching {t}." if ok
               else f"Couldn't remove {t}: {why or 'it is not on the watchlist'}.")
        _reset_cache_for(t)
        return jsonify(_clean({"enabled": True, "ok": ok, "item": res.get("item"), "reason": why,
                               "message": msg, "counts": _safe_counts(W)})), (200 if ok else 409)
    except Exception as e:
        _log_error(f"[watchlist] remove failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist/<ticker>/levels", methods=["POST"])
def api_watchlist_levels(ticker):
    """Replace Nico's price levels on an item (Opus's levels are kept by the store)."""
    try:
        W = _store()
        if W is None:
            return _disabled()
        t = _norm_ticker(ticker)
        if not t:
            return _bad("invalid ticker")
        b = _body()
        if "levels" not in b:
            return _bad("levels is required (use [] to clear)")
        levels, err = _norm_levels(b.get("levels"))
        if err:
            return _bad(err)
        res = W.update_levels(t, levels, actor="nico") or {}
        ok = bool(res.get("ok"))
        why = (res.get("reason") or "").strip()
        msg = (f"Levels updated on {t}." if ok
               else f"Couldn't update levels on {t}: {why or 'it is not on the watchlist'}.")
        return jsonify(_clean({"enabled": True, "ok": ok, "item": res.get("item"), "reason": why,
                               "message": msg})), (200 if ok else 409)
    except Exception as e:
        _log_error(f"[watchlist] levels failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist/<ticker>/promote", methods=["POST"])
def api_watchlist_promote(ticker):
    """Promote a qwen suggestion (status 'proposed') to active → promote(actor='nico').
    Cap rules apply exactly as for an add (may evict a Mason item, or be refused)."""
    try:
        W = _store()
        if W is None:
            return _disabled()
        t = _norm_ticker(ticker)
        if not t:
            return _bad("invalid ticker")
        res = W.promote(t, actor="nico") or {}
        counts = _safe_counts(W)
        ok = bool(res.get("ok")) and res.get("action") != "refused"
        _reset_cache_for(t)
        return jsonify(_clean({"enabled": True, "ok": ok, "action": res.get("action"),
                               "item": res.get("item"), "evicted": res.get("evicted"),
                               "reason": res.get("reason") or "",
                               "message": _result_message("promote", t, res, counts),
                               "counts": counts})), (200 if ok else 409)
    except Exception as e:
        _log_error(f"[watchlist] promote failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


_PROPOSE_ID_MAX = 128
_PROPOSE_REASON_MAX = 280


def _propose_id(v):
    """A scan_id / hit_id from the body → str, or None when missing / not a short string."""
    return v if isinstance(v, str) and v.strip() and len(v) <= _PROPOSE_ID_MAX else None


def _propose_refused(ticker, why, item=None):
    return jsonify(_clean({"enabled": True, "ok": False, "action": "refused", "item": item, "reason": why,
                           "message": f"Couldn't propose {ticker}: {why}."})), 409


@bp.route("/api/watchlist/<ticker>/propose", methods=["POST"])
def api_watchlist_propose(ticker):
    """Nico proposes a scanner hit from the Scanners page (S3 K2; finalization RULINGS 14/16) →
    propose(actor='nico', channel='scanner:<title>'): status 'proposed', NOT watched and NOT counted
    until promoted. Body (JSON only): {"source": "scanner", "scan_id", "hit_id"}.
    200 {enabled, ok, action: proposed|exists, item, message} · 409 refused (hit unknown / deleted /
    scanners off, scan or ticker mismatch, store refusal) · 400 bad ticker / source / ids / non-JSON."""
    try:
        W = _store()
        if W is None:
            return _disabled()
        t = _norm_ticker(ticker)
        if not t:
            return _bad("invalid ticker")
        b = request.get_json(silent=True) if request.is_json else None
        if not isinstance(b, dict):
            return _bad("a JSON object body is required: {source, scan_id, hit_id}")
        if b.get("source") != "scanner":
            return _bad("source must be 'scanner'")
        scan_id, hit_id = _propose_id(b.get("scan_id")), _propose_id(b.get("hit_id"))
        if not scan_id or not hit_id:
            return _bad("scan_id and hit_id are required")

        SC = _mod("mason_scanners")
        hit = None
        if SC is not None:
            try:
                hit = SC.hit_for_propose(hit_id)
            except Exception as e:
                _log_error(f"[watchlist] hit_for_propose {hit_id} failed: {e}")
        if not isinstance(hit, dict):
            return _propose_refused(t, "scanner hit not found")
        if str(hit.get("scan_id") or "") != scan_id:
            return _propose_refused(t, "that scanner hit belongs to a different scan")
        hit_t = _norm_ticker(hit.get("ticker"))
        if hit_t != t:
            return _propose_refused(t, f"that scanner hit is for {hit_t or 'another ticker'}, not {t}")

        channel = W.scanner_channel(hit.get("title"))
        label = channel.partition(":")[2]
        note = re.sub(r"\s+", " ", str(hit.get("triage_note") or "")).strip()
        note = note.replace("<", "\u2039").replace(">", "\u203a") or "proposed by Nico"
        reason = (f"scanner {label}: {note}" if label else f"scanner: {note}")[:_PROPOSE_REASON_MAX]
        res = W.propose(t, actor="nico", channel=channel, reason=reason,
                        ref={"source": "scanner", "scan_id": scan_id, "hit_id": hit_id}) or {}
        action = res.get("action")
        if not res.get("ok") or action not in ("proposed", "exists"):
            return _propose_refused(t, (res.get("reason") or "").strip() or "the watchlist refused it",
                                    res.get("item"))
        item = res.get("item") or {}
        status = item.get("status") or "proposed"
        try:
            SC.mark_watch_status(hit_id, status)
        except Exception as e:
            _log_error(f"[watchlist] mark_watch_status {hit_id} failed: {e}")
        if action == "proposed":
            msg = f"{t} proposed — a suggestion, not watched until you promote it."
        elif status == "active":
            msg = f"{t} is already on the watchlist."
        else:
            msg = f"{t} is already suggested — promote it to start watching."
        _reset_cache_for(t)
        return jsonify(_clean({"enabled": True, "ok": True, "action": action, "item": res.get("item"),
                               "reason": res.get("reason") or "", "message": msg,
                               "counts": _safe_counts(W)})), 200
    except Exception as e:
        _log_error(f"[watchlist] propose failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist/<ticker>/tape", methods=["GET"])
def api_watchlist_tape(ticker):
    """Drill-down for one name: bars (tf=1m|5m today, 1d ≈ last year) + last 7 days of
    events + option snaps (5 days) + the 5 latest digests + the item + stats."""
    try:
        W = _store()
        if W is None:
            return _disabled(bars=[], events=[], option_snaps=[], digests=[])
        t = _norm_ticker(ticker)
        if not t:
            return _bad("invalid ticker")
        tf = (request.args.get("tf") or "1m").strip()
        if tf not in _TFS:
            return _bad("tf must be 1m, 5m or 1d")
        TAPE = _mod("mason_watch_tape")
        SIG = _mod("mason_watch_signals")
        now = int(_now())

        bars, opt_snaps, opt_latest = [], [], None
        if TAPE is not None:
            try:
                if tf == "1d":
                    bars = TAPE.bars(t, "1d", since_ts=now - 400 * 86400, limit=400) or []
                else:
                    bars = TAPE.session_bars(t, tf=tf) or []
            except Exception as e:
                _log_error(f"[watchlist] bars {t} failed: {e}")
            try:
                opt_snaps = TAPE.option_snaps(t, now - 5 * 86400) or []
                opt_latest = TAPE.latest_option_snap(t)
            except Exception as e:
                _log_error(f"[watchlist] option snaps {t} failed: {e}")

        conn = mason_db.get_connection()
        try:
            ev = conn.execute(
                "SELECT id, ticker, ts, kind, severity, value_json, message, notified "
                "FROM watch_events WHERE ticker = ? AND ts >= ? ORDER BY ts DESC LIMIT 200",
                (t, now - 7 * 86400)).fetchall()
            dg = conn.execute(
                "SELECT id, date, slot, text, model, guard_ok, created_date FROM watch_digests "
                "WHERE ticker = ? ORDER BY date DESC, created_date DESC LIMIT 5", (t,)).fetchall()
        finally:
            conn.close()
        events = []
        for r in ev:
            d = dict(r)
            try:
                d["value"] = json.loads(d.pop("value_json") or "null")
            except Exception:
                d["value"] = None
            events.append(d)

        try:
            item = W.get_item(t)
        except Exception as e:
            _log_error(f"[watchlist] get_item {t} failed: {e}")
            item = None
        stats, _ = _stats_and_spark(t, SIG, TAPE)
        return jsonify(_clean({"enabled": True, "ticker": t, "tf": tf, "item": item, "stats": stats,
                               "bars": bars, "events": events, "option_snaps": opt_snaps,
                               "option_latest": opt_latest, "digests": [dict(r) for r in dg]}))
    except Exception as e:
        _log_error(f"[watchlist] tape failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/watchlist/log", methods=["GET"])
def api_watchlist_log():
    """Append-only mutation history (watchlist_log), newest first. ?limit=1..500, ?ticker=."""
    try:
        if _store() is None:
            return _disabled(items=[])
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        t = _norm_ticker(request.args.get("ticker") or "")
        sql = "SELECT * FROM watchlist_log"
        params = []
        if t:
            sql += " WHERE ticker = ?"
            params.append(t)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json") or "null")
            except Exception:
                d["detail"] = None
            items.append(d)
        return jsonify(_clean({"enabled": True, "items": items}))
    except Exception as e:
        _log_error(f"[watchlist] log failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def _reset_cache_for(ticker):
    """Drop one ticker's cached stats after a mutation so the next poll is fresh."""
    _STATS_CACHE.pop(ticker, None)
