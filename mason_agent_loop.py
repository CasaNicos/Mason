"""
mason_agent_loop.py — deterministic (NO LLM) agent loop around mason_autotrade (PAPER ONLY).

P0 pieces
---------
  * calibration + tiers : base_rate() / calibrate() / tier_for() / size_budget_usd()
  * pre-market queue    : queue() / drain_queue()  (rec execution.state 'queued' → placed after open+delay)
  * spine               : sync_spine() mirrors every placed director rec into `agent_trades`
  * exits               : manage_exits() closes OWN positions only (never more than the row's own qty)
  * tick()              : scheduler entry (called from the analyst-desk catalyst_tick); cheap no-op when off
  * status()            : summary for a future UI

Safety rules
------------
  * The account also holds the owner's hand-placed stock/option positions. This loop only ever sells
    `min(agent_trades.qty, position.qty_available)` of a symbol it opened itself, and only cancels the
    child legs of ITS OWN bracket order.
  * Every public function catches exceptions and returns a dict/list instead of raising.
  * Config is read fresh on every call (autotrade block, via mason_autotrade._at_cfg()).
  * All I/O goes through module-level helpers (_clock, _get_position, ...) so tests can monkeypatch them.

Step 1 hardening (2026-11-13, audit X1-X3, X7-X9) — each has a switch, ON when the key is absent
--------------------------------------------------------------------------------------------
  * PRICES: every stop/target check and every exit limit uses mason_quotes (Robinhood's consolidated
    quote first, cross-checked, then validated Alpaca data). A stock stop/target keys on the last
    trade; an exit limit is last/mid minus a small band, never a bid far from the last trade.
  * OPTION STOP (exits.opt_stop_guard): needs a real bid and a fresh two-sided quote inside the
    spread limit, triggers on the MID, must be confirmed on two checks in a row, and is not judged
    in the first minutes after the open. No usable quote = nothing happens this tick.
  * RE-CHECK BEFORE SENDING (exits.revalidate_before_send): a stop/target that was decided earlier is
    checked again on a fresh price before any order goes out and is withdrawn if it no longer
    holds. Flat-by time, day-before-expiry, director close and the time stop are never withdrawn.
  * HALF-DAYS: the flat-by time follows the broker clock's real close (autotrade.session_close_from_clock).
  * EXPIRY (exits.check_settlement_on_expiry): before an expired option is booked at zero the broker
    is asked whether it was sold or exercised.
  * BRACKET LEGS (exits.tolerate_sibling_cancel): a leg the broker already cancelled with its sibling
    no longer raises an "exit stalled" note.
  * The quote behind every exit decision and exit order is stored on the trade (features_json).
"""

import json
import re
import time
import uuid
from datetime import datetime, date, timedelta

_SCHEMA_DONE = set()       # DB files whose schema this process already ensured

_TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"}
_DEAD = {"canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"}
_QUEUEABLE = (None, "", "failed", "unfilled", "queued")   # states a rec may be queued from
_RELEASE_GRACE_S = 240     # after cancelling our legs the broker needs a moment to free the shares
# gates that can clear by themselves shortly after the open (wide spread, missing quote, API hiccup)
_RETRYABLE_GATES = ("option_liquidity", "price", "account", "market_open", "pretrade_checks",
                    # Step 4 (review): "worth too little" counts the buy/sell spread, which is widest in the first
                    # minutes; a queued call refused only for that is tried again until queue_retry_until_et.
                    # The gate exists only when autotrade.value_gate.min_expected_value is on.
                    "expected_value",
                    # Short call placed as a put: no put could be chosen or priced right now (prices missing or too
                    # far apart, or the stock under the call's entry price). Exists only with short_as_put.enabled.
                    "short_put_wait",
                    # Option order handling: the entry filters that can clear shortly after the open (each exists
                    # only while its rule in autotrade.option_orders.filters is switched on).
                    "opt_two_sided", "opt_spread", "opt_quote_age")
_REPRICE_AFTER_S = 300     # a resting exit order older than this is cancelled and re-priced
_SOFT_EXITS = ("stop", "target")   # price-driven: re-checked on a fresh quote before an order is sent
_CONFIRM_MIN_GAP_S = 60    # two option-stop checks closer together than this count as one

_AGENT_TRADES_SQL = """CREATE TABLE IF NOT EXISTS agent_trades(
    id TEXT PRIMARY KEY, source TEXT, rec_id TEXT, journal_table TEXT, journal_id TEXT,
    ticker TEXT, occ TEXT, vehicle TEXT, direction TEXT, horizon TEXT, tier TEXT,
    p_raw REAL, p_cal REAL, exp_move_pct REAL, thesis TEXT, features_json TEXT,
    entry_ts TEXT, entry_px REAL, qty REAL, cost_usd REAL, stop REAL, target REAL,
    time_stop_date TEXT, exit_ts TEXT, exit_px REAL, exit_reason TEXT, exit_order_id TEXT,
    pnl_usd REAL, pnl_pct REAL, r_multiple REAL, mfe_pct REAL, mae_pct REAL, win INTEGER,
    graded_at TEXT, status TEXT, created_date TEXT, created_by TEXT,
    record_deleted INTEGER DEFAULT 0, exit_pending_reason TEXT)"""
_CALIB_SQL = """CREATE TABLE IF NOT EXISTS agent_calibration(
    id INTEGER PRIMARY KEY AUTOINCREMENT, segment TEXT, source TEXT, n INTEGER, wins INTEGER,
    p_cal REAL, expectancy_r REAL, sizing_mult REAL, computed_at TEXT)"""


# ── lazy module access + injectable I/O ──────────────────────────────────────────

_BOOK_SQL = """CREATE TABLE IF NOT EXISTS agent_book_actions(
    id TEXT PRIMARY KEY, slate_id TEXT, symbol TEXT, qty REAL, reason TEXT, state TEXT,
    created_at TEXT, day TEXT, order_id TEXT, attempts INTEGER DEFAULT 0, submitted_at TEXT,
    filled_qty REAL DEFAULT 0, proceeds REAL DEFAULT 0, note TEXT)"""
_STOCK_SYM = re.compile(r"^[A-Z][A-Z.]{0,5}$")


def _AT():
    import mason_autotrade
    return mason_autotrade


def _EX():
    import mason_execution
    return mason_execution


def _OO():
    import mason_option_orders
    return mason_option_orders


def _oo_on(cfg) -> bool:
    """autotrade.option_orders is switched on or recording. False (and nothing is imported) otherwise."""
    try:
        b = (cfg or {}).get("option_orders")
        if isinstance(b, dict) and b.get("enabled") is not True and b.get("shadow") is False:
            return False
        return bool(_OO().active(cfg))
    except Exception:
        return False


def _conn():
    import mason_db
    return mason_db.get_connection()


def _parked():
    """{FUND: shares the idle-cash rule has parked} (mason_idle_cash, 2026-11-16). {} when nothing is parked or
    the module is absent; None when its ledger could not be read. Parked shares are Mason's own, never Nico's,
    and not a director trade. (review fix) A purchase of the rule's that is still working counts in full."""
    try:
        import mason_idle_cash as IC
    except ImportError:
        return {}
    try:
        return IC.owned_map()
    except Exception:
        return None


def _cfg() -> dict:
    return _AT()._at_cfg()


def _now_et() -> datetime:
    return _AT()._now_et()


def _clock() -> dict:
    return _AT()._clock()


def _get_position(symbol):
    return _EX().get_position(symbol)


def _get_order(order_id):
    return _EX().get_order(order_id)


def _close_position(symbol, qty, entry, pitch_id=None):
    return _EX().close_position(symbol, qty=qty, entry=entry, pitch_id=pitch_id)


def _cancel_order(order_id):
    code, _ = _EX()._request("DELETE", f"/orders/{order_id}")
    return code in (200, 204)


def _option_quote(symbol):
    return _AT()._option_quote(symbol)


def _stock_quote(symbol):
    return _AT()._stock_quote(symbol)


def _quote(symbol, is_opt, cfg=None, relaxed=False):
    """The price the loop acts on (mason_quotes via mason_autotrade). `relaxed` is for exits that
    must happen anyway. If the quote module cannot be used, the old Alpaca quote marked legacy."""
    return _AT()._trusted_quote(symbol, is_opt, cfg, relaxed=relaxed)


def _sell_limit(q, attempt, urgent, cfg):
    """(limit | None, how it was derived) for a sell-to-close; old rule (bid) if the quote module is absent."""
    try:
        return _AT()._MQ().sell_limit(q, attempt=attempt, urgent=urgent, cfg=cfg)
    except Exception:
        v = (q or {}).get("bid") or (None if (q or {}).get("kind") == "option" else (q or {}).get("last"))
        return (round(float(v), 2), "bid (old rule)") if v else (None, "no bid")


def _compact_quote(q, **extra):
    try:
        return _AT()._MQ().compact(q, **extra)
    except Exception:
        return dict({k: (q or {}).get(k) for k in ("bid", "ask", "last", "mid", "source")}, **extra)


def _settlement(symbol):
    return _EX().settlement_disposition(symbol)


def _now_s() -> float:
    return time.time()


def _notify(title, body, *, ticker=None, rec_id=None, severity="info", dedup=None):
    _AT()._notify(title, body, ticker=ticker, rec_id=rec_id, severity=severity, dedup=dedup)


def _activity(kind, title, *, ticker, rec_id, extra, body=None):
    _AT()._activity(kind, title, approver="auto", ticker=ticker, rec_id=rec_id, extra=extra, body=body)


def _holidays() -> set:
    try:
        import mason_director
        return set(mason_director._NYSE_HOLIDAYS)
    except Exception:
        return set()


def _f(v):
    return _AT()._f(v)


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── schema ───────────────────────────────────────────────────────────────────────

def ensure_schema(force=False) -> dict:
    try:
        import mason_db
        key = str(getattr(mason_db, "DB_FILE", ""))
        if key in _SCHEMA_DONE and not force:
            return {"ok": True, "cached": True}
        conn = _conn()
        try:
            with conn:
                conn.execute(_AGENT_TRADES_SQL)
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_trades_src_rec "
                             "ON agent_trades(source, rec_id)")
                conn.execute(_CALIB_SQL)
                conn.execute(_BOOK_SQL)
        finally:
            conn.close()
        _SCHEMA_DONE.add(key)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"ensure_schema: {e}"}


def _q(sql, args=()):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _update_trade(tid, fields: dict):
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = _conn()
    try:
        with conn:
            conn.execute(f"UPDATE agent_trades SET {cols} WHERE id=?", list(fields.values()) + [tid])
    finally:
        conn.close()


# ── calibration / tiers / sizing ─────────────────────────────────────────────────

def _verdict_rows() -> tuple:
    """([{outcome 'right'|'wrong', conf, horizon, vehicle}], source) — the graded directional director
    calls that calibration learns from.

    source "shared": mason_desk_pipeline.director_call_verdicts(), the one shared reader of the
    director's verdicts. It honours analyst_desk.opus_director.grading.scorecard_source and by
    default returns the TRADABLE grade (entry at a price that could really be traded), not the old
    grade that credited a 07:00 call with the overnight gap it had already seen.
    source "sql": today's query on the legacy outcome column — used while that reader does not exist
    yet, when autotrade.learning.shared_verdicts is false, or if the reader fails; so this installs
    safely on its own.
    Only right / wrong count: a neutral, never-entered or still-open call says nothing about winning."""
    try:
        if (_cfg().get("learning") or {}).get("shared_verdicts", True):
            import mason_desk_pipeline as PP
            fn = getattr(PP, "director_call_verdicts", None)
            if callable(fn):
                return ([{"outcome": r["outcome"], "conf": _f(r.get("confidence")),
                          "horizon": str(r.get("horizon") or ""), "vehicle": str(r.get("vehicle") or "stock")}
                         for r in (fn() or []) if isinstance(r, dict) and r.get("outcome") in ("right", "wrong")],
                        "shared")
    except Exception:
        pass
    rows = _q("SELECT outcome, json_extract(extra_json,'$.horizon') h, "
              "json_extract(extra_json,'$.trade.vehicle') v, json_extract(extra_json,'$.confidence') c "
              "FROM recommendations WHERE created_by='opus_director' AND direction IN ('long','short') "
              "AND outcome IN ('right','wrong') AND COALESCE(record_deleted,0)=0")
    return ([{"outcome": r["outcome"], "conf": _f(r["c"]), "horizon": str(r["h"] or ""),
              "vehicle": str(r["v"] or "stock")} for r in rows], "sql")


def base_rate() -> dict:
    try:
        cfg = _cfg()
        lc = cfg.get("learning") or {}
        prior_n, prior_p = float(lc.get("prior_n", 20)), float(lc.get("prior_p", 0.5))
        rows, src = _verdict_rows()
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "right")
        confs = [r["conf"] for r in rows if r["conf"] is not None]
        mean_raw = sum(confs) / len(confs) if confs else 0.55
        p_base = (wins + prior_p * prior_n) / (n + prior_n) if (n + prior_n) else prior_p
        return {"n": n, "wins": wins, "mean_raw": mean_raw, "p_base": p_base, "source": src}
    except Exception as e:
        return {"n": 0, "wins": 0, "mean_raw": 0.55, "p_base": 0.5, "error": f"base_rate: {e}"}


def calibrate(p_raw, horizon=None, vehicle=None) -> dict:
    """P0 rule: p_cal = clamp(p_base + (p_raw - mean_raw), 0.01, 0.99), one global segment."""
    try:
        b = base_rate()
        if p_raw is None:
            return {"p_raw": None, "p_cal": None, "segment": "global", "n": b["n"]}
        p = float(p_raw)
        base, seg, n = b["p_base"], "global", b["n"]
        L = (_cfg().get("learning") or {})
        st = segment_rates().get((str(horizon or ""), str(vehicle or "stock")))
        if st and st["n"] >= int(L.get("min_n_segment", 10)):
            pn = float(L.get("prior_n", 20))          # shrink the segment toward the global rate
            base = (st["wins"] + b["p_base"] * pn) / (st["n"] + pn)
            seg, n = f"{horizon}|{vehicle or 'stock'}", st["n"]
        p_cal = min(0.99, max(0.01, base + (p - b["mean_raw"])))
        return {"p_raw": p, "p_cal": p_cal, "segment": seg, "n": n}
    except Exception as e:
        return {"p_raw": p_raw, "p_cal": None, "segment": "global", "n": 0, "error": f"calibrate: {e}"}


def _graded_calls() -> list:
    """Graded directional director calls: [{horizon, vehicle, conf, win}]."""
    rows, _src = _verdict_rows()                   # the shared tradable verdicts when available
    return [{"horizon": r["horizon"], "vehicle": r["vehicle"], "conf": r["conf"],
             "win": 1 if r["outcome"] == "right" else 0} for r in rows]


def segment_rates() -> dict:
    """{(horizon, vehicle): {n, wins}} over graded directional calls."""
    out = {}
    try:
        for g in _graded_calls():
            d = out.setdefault((g["horizon"], g["vehicle"]), {"n": 0, "wins": 0})
            d["n"] += 1
            d["wins"] += g["win"]
    except Exception:
        pass
    return out


def own_graded_n() -> int:
    try:
        r = _q("SELECT COUNT(*) n FROM agent_trades WHERE source='mason' AND status='closed' "
               "AND win IS NOT NULL AND COALESCE(record_deleted,0)=0")
        return int(r[0]["n"])
    except Exception:
        return 0


def own_segment(horizon, vehicle) -> dict:
    """Mason's OWN closed trades in one horizon|vehicle segment: {n, wins, pnl}."""
    try:
        r = _q("SELECT COUNT(*) n, COALESCE(SUM(win),0) w, COALESCE(SUM(pnl_usd),0) p FROM agent_trades "
               "WHERE source='mason' AND status='closed' AND win IS NOT NULL AND COALESCE(record_deleted,0)=0 "
               "AND COALESCE(horizon,'')=? AND LOWER(COALESCE(vehicle,'stock'))=?",
               (str(horizon or ""), str(vehicle or "stock").lower()))[0]
        return {"n": int(r["n"]), "wins": int(r["w"]), "pnl": float(r["p"])}
    except Exception:
        return {"n": 0, "wins": 0, "pnl": 0.0}


def sizing_mult(horizon, vehicle, cfg=None) -> float:
    """Results-driven size multiplier for one segment, inside the configured caps. 1.0 until the
    segment has min_n_segment closed own trades; then up for a paying segment, down for a losing one."""
    try:
        cfg = cfg or _cfg()
        sm = cfg.get("sizing_mult") or {}
        if not sm.get("enabled", True):
            return 1.0
        o = own_segment(horizon, vehicle)
        if o["n"] < int((cfg.get("learning") or {}).get("min_n_segment", 10)):
            return 1.0
        wr = o["wins"] / o["n"]
        m = 1.25 if (wr >= 0.6 and o["pnl"] > 0) else (0.6 if (wr < 0.4 or o["pnl"] < 0) else 1.0)
        return max(float(sm.get("min", 0.5)), min(float(sm.get("max", 1.5)), m))
    except Exception:
        return 1.0


def tier_for(p_cal, cfg=None, horizon=None, vehicle=None):
    """(tier_name | None, tier_cfg). Highest tier with min_p <= p_cal; capped at 'learning'
    until Mason has `normal_requires_own_n` graded own trades — and, when the call's horizon is
    given, until its own horizon|vehicle segment has enough closed trades with a good win rate."""
    try:
        cfg = cfg or _cfg()
        if p_cal is None:
            return None, {}
        tiers = cfg.get("tiers") or {}
        ok = [(float(t.get("min_p", 1.0)), name) for name, t in tiers.items() if float(t.get("min_p", 1.0)) <= p_cal]
        if not ok:
            return None, {}
        name = max(ok)[1]
        if name != "learning" and "learning" in tiers:
            if own_graded_n() < int(cfg.get("normal_requires_own_n", 10)):
                name = "learning"
            elif horizon is not None:
                o = own_segment(horizon, vehicle)
                need = int(cfg.get("normal_requires_own_segment_n", 5))
                if o["n"] < max(1, need) or (o["wins"] / o["n"]) < float(cfg.get("normal_requires_own_segment_win", 0.5)):
                    name = "learning"
        return name, dict(tiers.get(name) or {})
    except Exception:
        return None, {}


def locked_tier(tier_name, cfg=None):
    """Step 4b. The tier a call may really be sized at NOW. tier_for() already keeps every tier above
    'learning' (normal AND high) locked until Mason has `normal_requires_own_n` closed trades of its
    own, but it runs once, when the call is first tiered; the tier is then stored on the call and
    read back when the order is built (a queued call: hours later). This repeats the same count at
    build time, so a stored 'normal' or 'high' can never be used while the lock is on. A learning
    tier (every call so far) costs nothing here: no database read. If the count cannot be made the
    answer is 'learning' (the smaller size)."""
    try:
        if not tier_name or tier_name == "learning":
            return tier_name
        cfg = cfg or _cfg()
        tiers = cfg.get("tiers") or {}
        if "learning" not in tiers or tier_name not in tiers:   # an unknown tier is refused by the caller, as before
            return tier_name
        if own_graded_n() < int(cfg.get("normal_requires_own_n", 10)):
            return "learning"
        return tier_name
    except Exception:
        return "learning"


def option_spend_rows(cfg=None):
    """autotrade.option_spend as a list of bands, shortest first:
    [{"band" (plain words), "min_days", "max_days" (None = open-ended), "learning", "normal", "high"}], or None when
    no table is set or it cannot be read (then the two older premium caps are the rule). The last row
    is always open-ended; a column a row leaves out takes that row's learning amount."""
    try:
        cfg = cfg or _cfg()
        rows = cfg.get("option_spend")
        if not isinstance(rows, list) or not rows:
            return None
        parsed = sorted(((float("inf") if r.get("max_days") is None else float(r["max_days"]), r) for r in rows),
                        key=lambda x: x[0])
        out, lo = [], 0
        for i, (md, r) in enumerate(parsed):
            open_end = (i == len(parsed) - 1) or md == float("inf")
            base = float(r["learning"])
            cell = {"band": f"{lo} days or more" if open_end else f"{lo}-{int(md)} days",
                    "min_days": lo, "max_days": None if open_end else int(md)}
            for col in ("learning", "normal", "high"):
                v = float(r[col]) if r.get(col) is not None else base
                if not (v >= 0):                   # NaN or negative: the table cannot be trusted
                    return None
                cell[col] = v
            out.append(cell)
            if not open_end:
                lo = int(md) + 1
        return out
    except Exception:
        return None


def option_spend(tier_name, dte, cfg=None):
    """Step 4b (Nico's table, 2026-11-15). The most dollars ONE option trade may spend, by calendar
    days to expiry and confidence tier, from autotrade.option_spend: a list of rows
    {"max_days", "learning", "normal", "high"}; the last row is open-ended.

    Returns {"usd", "band" (plain words), "tier" (the column used), "days"} or None. None = no table
    (option_spend is null, absent, empty or unreadable): then the two older caps, call_premium_cap_usd
    and put_premium_cap_usd, are the rule, as before. `dte`: 0 = expires today; None = not known, which
    gives the smallest amount of the column. A tier the table does not name uses the learning column."""
    try:
        rows = option_spend_rows(cfg)
        if not rows:
            return None
        col = str(tier_name or "learning")
        if col not in ("learning", "normal", "high"):
            col = "learning"
        if dte is None:
            c = min(rows, key=lambda x: x[col])
            return {"usd": c[col], "band": "an unknown number of days (the smallest amount is used)", "tier": col, "days": None}
        d = max(0, int(dte))
        for c in rows:
            if c["max_days"] is None or d <= c["max_days"]:
                return {"usd": c[col], "band": c["band"], "tier": col, "days": d}
        return None
    except Exception:
        return None


def size_budget_usd(tier_name, vehicle, equity, cfg=None, horizon=None, dte=None) -> float:
    """Dollars one trade of this tier may spend. Stock: the tier's usd_max (learning: also at most
    max_pct of the account), lowered by the learned multiplier. Option: the same, and never more than
    the spend limit for its days to expiry (`dte`) in autotrade.option_spend; when no such table is
    set, the older call/put premium caps apply instead (Step 4b)."""
    try:
        cfg = cfg or _cfg()
        t = (cfg.get("tiers") or {}).get(tier_name)
        if not t:
            return 0.0
        usd = float(t.get("usd_max") or 0)
        if horizon is not None:                    # learned multiplier: can only LOWER a tier's own cap
            usd = usd * min(sizing_mult(horizon, vehicle, cfg), 1.0)
        if tier_name == "learning" and t.get("max_pct") is not None and equity:
            usd = min(usd, float(equity) * float(t["max_pct"]) / 100.0)
        v = (vehicle or "").lower()
        sp = option_spend(tier_name, dte, cfg) if v in ("call", "put") else None
        if sp is not None:                         # the table replaces the two older caps
            usd = min(usd, float(sp["usd"]))
        elif v == "call" and cfg.get("call_premium_cap_usd") is not None:
            usd = min(usd, float(cfg["call_premium_cap_usd"]))
        elif v == "put" and cfg.get("put_premium_cap_usd") is not None:
            usd = min(usd, float(cfg["put_premium_cap_usd"]))
        return float(usd)
    except Exception:
        return 0.0


# ── pre-market queue ─────────────────────────────────────────────────────────────

def queue(rec_id, info=None) -> dict:
    try:
        cur = (((_AT()._load_rec(rec_id) or {}).get("extra") or {}).get("execution") or {}).get("state")
        if cur not in _QUEUEABLE:
            return {"ok": False, "rec_id": rec_id, "error": f"already {cur} — not queued"}
        ex = _AT()._set_execution(rec_id, {**(info or {}), "state": "queued", "queued_at": _nowiso()})
        return {"ok": ex is not None, "rec_id": rec_id, "execution": ex}
    except Exception as e:
        return {"ok": False, "rec_id": rec_id, "error": f"queue: {e}"}


def _queued_ids() -> list:
    return [r["id"] for r in _q(
        "SELECT id FROM recommendations WHERE kind='director' AND COALESCE(record_deleted,0)=0 "
        "AND json_extract(extra_json,'$.execution.state')='queued'")]


def _block(rec_id, ticker, reason, blocked=None, features=None):
    """A queued call that will not be placed: recorded on the call (with the gates, X4) + one note.
    `features` (Step 4): what the sizing rules worked out for it, kept on the call when present."""
    AT = _AT()
    fields = {"state": "queue_blocked", "queue_block_reason": str(reason)[:500], "queue_blocked_at": _nowiso()}
    why = str(reason)
    try:
        if blocked and AT._at_cfg().get("persist_blocked", True):
            gates = [{"gate": str(b).split(":", 1)[0].strip(),
                      "detail": (str(b).split(":", 1)[1].strip() if ":" in str(b) else "")[:400]} for b in blocked]
            why = "; ".join(AT.plain_block_reasons(gates))
            fields.update({"blocked_gates": gates, "blocked_reason": why})
    except Exception:
        pass
    if isinstance(features, dict) and "sizing_plan" in features:
        fields["features"] = features
    AT._set_execution(rec_id, fields)
    _notify(f"Queued call blocked: {ticker} (rec {rec_id})",
            f"The queued director call was not placed and will not be retried.\nReason: {why}",
            ticker=ticker, rec_id=rec_id, severity="warning", dedup=f"agent_queue_blocked:{rec_id}")


def drain_queue(now_et=None) -> list:
    out = []
    try:
        AT = _AT()
        cfg = _cfg()
        now = now_et or _now_et()
        ids = _queued_ids()
        if not ids:
            return out
        clk = _clock()
        is_open = bool(clk.get("ok") and clk.get("is_open"))
        open_at = now.replace(hour=9, minute=30, second=0, microsecond=0) + \
            timedelta(minutes=float(cfg.get("entry_open_delay_min", 5)))
        for rid in ids:
            try:
                rec = AT._load_rec(rid)
                if not rec:
                    continue
                if AT.trading_days_since(rec.get("created_date"), now.date()) > AT._MAX_TRADING_DAYS_OLD:
                    _block(rid, rec.get("ticker"), "stale — queued call older than "
                           f"{AT._MAX_TRADING_DAYS_OLD} trading days")
                    out.append({"rec_id": rid, "state": "queue_blocked", "reason": "stale"})
                    continue
                if not is_open:
                    out.append({"rec_id": rid, "state": "queued", "reason": "market closed"})
                    continue
                if now < open_at:
                    out.append({"rec_id": rid, "state": "queued", "reason": "waiting for open delay"})
                    continue
                res = AT.approve(rid, approver="auto")
                state_now = ((AT._load_rec(rid) or {}).get("extra") or {}).get("execution", {}).get("state")
                if state_now == "queued":          # blocked by the gates
                    reason = res.get("error") or "blocked"
                    gates = [str(b).split(":", 1)[0].strip() for b in (res.get("blocked") or [])]
                    rh, rm = _hhmm(cfg.get("queue_retry_until_et"), "10:30")
                    same_day = str(rec.get("created_date") or "")[:10] == now.date().isoformat()
                    if gates and all(g in _RETRYABLE_GATES for g in gates) and same_day \
                            and (now.hour, now.minute) < (rh, rm):
                        AT._set_execution(rid, {"queue_last_block": str(reason)[:300],
                                                "queue_attempts": int(((rec.get("extra") or {}).get("execution")
                                                                       or {}).get("queue_attempts") or 0) + 1})
                        out.append({"rec_id": rid, "state": "queued", "reason": "retry: " + str(reason)[:160]})
                        continue
                    _block(rid, rec.get("ticker"), reason, res.get("blocked"),
                           features=(res.get("preview") or {}).get("features"))
                    out.append({"rec_id": rid, "state": "queue_blocked", "reason": reason})
                else:
                    out.append({"rec_id": rid, "state": state_now, "placed": bool(res.get("placed"))})
            except Exception as e:
                out.append({"rec_id": rid, "error": f"{type(e).__name__}: {e}"})
    except Exception as e:
        out.append({"rec_id": None, "error": f"drain_queue: {e}"})
    return out


# ── spine ────────────────────────────────────────────────────────────────────────

def _add_sessions(d: date, n: int, holidays=None) -> date:
    hol = _holidays() if holidays is None else holidays
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in hol:
            n -= 1
    return d


def _sessions_until(today: date, exp: date, holidays=None, stop_at=None) -> int:
    """Trading days after `today` up to and including `exp` (0 when `exp` is today or already past). Weekends and the
    holidays of _holidays() are not trading days. Counting stops once it passes `stop_at` (only "more than that" is
    needed). (2026-11-18) The day-before-expiry exit used to count calendar days, so an option expiring on a Monday,
    or on the day after a holiday, was sold on its expiry day itself instead of on the trading day before it."""
    hol = _holidays() if holidays is None else holidays
    n, d = 0, today
    while d < exp:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in hol:
            n += 1
            if stop_at is not None and n > stop_at:
                break
    return n


def _time_stop_sessions(cfg, horizon, extra) -> int:
    tss = (cfg.get("exits") or {}).get("time_stop_sessions") or {}
    if horizon in tss:
        return int(tss[horizon])
    try:
        return int(extra.get("horizon_days"))
    except (TypeError, ValueError):
        return int(tss.get("swing", 10))


def _opt_snap_feats(cfg, rec, e, tr, feats) -> dict:
    """Job C (2026-11-17, audit L7): what the options market was pricing for the stock when the order was placed, from
    Mason's stored option snapshots only (mason_option_snaps.implied_move_stored: newest snapshot read AT OR BEFORE
    execution.placed_at, read-only connection, at most a 1-second lock wait, no network, never a later snapshot):
      opt_snap                 'ok' | 'none' (nothing usable on record then) | 'no decision time'
      opt_implied_move_pct     the move, up or down, priced in up to that snapshot's expiry
      opt_implied_days         calendar days from the decision to that expiry
      opt_atm_iv_pct           at-the-money implied volatility, percent a year
      opt_snap_age_min         minutes between the snapshot and the decision
      opt_move_vs_implied_day  the move since yesterday's close that the already-moved check recorded at the decision,
                               in the trade's direction, divided by one day's move as implied volatility prices it
                               (IV / sqrt(252)); None when either is missing
    {} unless switched on (autotrade.option_snap_features.enabled true; off by default) and on any failure. Never raises: the trade
    row is written whatever happens here. Kept on the trade only; no model reads it."""
    try:
        oc = cfg.get("option_snap_features")
        oc = oc if isinstance(oc, dict) else {}
        if oc.get("enabled") is not True:                # off unless switched on (ships off)
            return {}
        out = {"opt_snap": "none", "opt_implied_move_pct": None, "opt_implied_days": None, "opt_atm_iv_pct": None,
               "opt_snap_age_min": None, "opt_move_vs_implied_day": None}
        import mason_option_snaps as OS
        t = str(rec.get("ticker") or "").upper().strip()
        raw = str(e.get("placed_at") or "").strip()
        if re.search(r"([Zz]|[+-]\d\d:?\d\d)$", raw):
            at = OS.parse_ts(raw)
        else:
            try:
                at = datetime.fromisoformat(raw).astimezone().timestamp() if raw else None   # naive = this machine's clock
            except ValueError:
                at = None
        if not t or at is None:
            out["opt_snap"] = "no decision time"
            return out
        try:
            age = float(oc.get("max_age_hours", 96)) * 60.0
        except (TypeError, ValueError):
            age = 96 * 60.0
        s = OS.implied_move_stored(t, at, max_age_minutes=age)
        if not s or _f(s.get("ts")) is None or float(s["ts"]) > at:
            return out
        out.update(opt_snap="ok", opt_implied_move_pct=s.get("implied_move_pct"), opt_implied_days=s.get("days_left"),
                   opt_atm_iv_pct=s.get("atm_iv_pct"), opt_snap_age_min=s.get("age_minutes"))
        mv = _f((feats.get("entry_features") or {}).get("move_since_prev_close_pct"))
        day = _f(s.get("daily_move_pct"))
        if mv is not None and day:
            short = str(rec.get("direction") or "").lower() == "short" or str(tr.get("vehicle") or "").lower() == "put"
            out["opt_move_vs_implied_day"] = round((-1.0 if short else 1.0) * mv / day, 3)
        return out
    except Exception:
        return {}


def sync_spine() -> dict:
    out = {"inserted": [], "skipped": 0}
    try:
        ensure_schema()
        cfg = _cfg()
        have = {r["rec_id"] for r in _q("SELECT rec_id FROM agent_trades WHERE source='mason'")}
        rows = _q("SELECT id FROM recommendations WHERE kind='director' AND COALESCE(record_deleted,0)=0 "
                  "AND json_extract(extra_json,'$.execution.state')='placed' "
                  "AND json_extract(extra_json,'$.execution.tier') IS NOT NULL "
                  "AND json_extract(extra_json,'$.execution.approver')='auto'")
        AT = _AT()
        for r in rows:
            rid = r["id"]
            if rid in have:
                out["skipped"] += 1
                continue
            try:
                rec = AT._load_rec(rid)
                ex = rec.get("extra") or {}
                e = ex.get("execution") or {}
                tr = AT._placed_trade(ex)              # the put when a short call was placed as a put; else the call's own block
                is_opt = e.get("asset") == "option"
                mult = 100 if is_opt else 1
                px, qty = _f(e.get("price")), _f(e.get("qty"))
                entry_final = False
                if e.get("order_id"):
                    g = _get_order(e["order_id"])
                    if g.get("ok"):
                        o = g["order"]
                        if _f(o.get("filled_qty")) and _f(o.get("filled_avg_price")):
                            qty, px = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
                        entry_final = (o.get("status") or "").lower() in _TERMINAL
                er = e.get("exit_rules") or {}
                stop = _f(er.get("stop_premium") if er.get("stop_premium") is not None else er.get("stop"))
                target = _f(er.get("target_premium") if er.get("target_premium") is not None else er.get("target"))
                entry_ts = e.get("placed_at") or _nowiso()
                try:
                    entry_d = date.fromisoformat(str(entry_ts)[:10])
                except Exception:
                    entry_d = _now_et().date()
                horizon = ex.get("horizon")
                ts_date = _add_sessions(entry_d, _time_stop_sessions(cfg, horizon, ex)).isoformat()
                feats = dict(e.get("features") or {})
                if e.get("entry_quote"):               # the quote the entry was priced from (cost grading)
                    feats["entry_quote"] = e["entry_quote"]
                feats.update({"anchor_kind": ex.get("anchor_kind") or ex.get("kind"),
                              "catalyst_id": ex.get("catalyst_id"), "confidence": ex.get("confidence"),
                              "slate_id": ex.get("slate_id"), "journal_id": e.get("journal_id"),
                              "journal_table": e.get("journal_table"), "order_id": e.get("order_id"),
                              "entry_final": entry_final})
                tier_ = e.get("tier")
                if e.get("short_as_put"):              # short call placed as a put: what it came from, and its exits
                    feats["short_as_put"] = e["short_as_put"]
                    tier_ = "learning"                 # always sized as a Learning trade, whatever the call was rated
                feats.update(_opt_snap_feats(cfg, rec, e, tr, feats))   # Job C: option snapshot facts at the decision
                conn = _conn()
                try:
                    with conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO agent_trades(id,source,rec_id,journal_table,journal_id,ticker,occ,"
                            "vehicle,direction,horizon,tier,p_raw,p_cal,exp_move_pct,thesis,features_json,entry_ts,entry_px,qty,"
                            "cost_usd,stop,target,time_stop_date,status,created_date,created_by,record_deleted) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                            (uuid.uuid4().hex[:12], "mason", rid, e.get("journal_table"), e.get("journal_id"),
                             (rec.get("ticker") or "").upper(), e.get("symbol") if is_opt else None,
                             tr.get("vehicle"), rec.get("direction"), horizon, tier_,
                             _f(e.get("p_raw")), _f(e.get("p_cal")), _f(ex.get("exp_move_pct")),
                             str(ex.get("prediction") or "")[:2000], json.dumps(feats, default=str),
                             entry_ts, px, qty, (px * qty * mult) if (px is not None and qty is not None) else None,
                             stop, target, ts_date, "open", _nowiso(), "mason_agent_loop"))
                finally:
                    conn.close()
                out["inserted"].append(rid)
            except Exception as ex_:
                out.setdefault("errors", []).append({"rec_id": rid, "error": f"{type(ex_).__name__}: {ex_}"})
    except Exception as e:
        out["error"] = f"sync_spine: {e}"
    return out


# ── exits ────────────────────────────────────────────────────────────────────────

def _occ_expiry(occ, trade) -> date | None:
    try:
        if trade and trade.get("expiry"):
            return date.fromisoformat(str(trade["expiry"])[:10])
    except Exception:
        pass
    try:
        s = occ[-15:-9]
        return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
    except Exception:
        return None


def _hhmm(s, default):
    try:
        h, m = str(s or default).split(":")
        return int(h), int(m)
    except Exception:
        h, m = default.split(":")
        return int(h), int(m)


def _legs(order_id) -> list:
    g = _get_order(order_id)
    if not g.get("ok"):
        return []
    return list((g.get("order") or {}).get("legs") or [])


def _leg_exit(legs):
    """(reason, px) for a FILLED bracket child leg, else (None, None)."""
    for leg in legs:
        if (leg.get("status") or "").lower() == "filled" and leg.get("filled_avg_price"):
            typ = (leg.get("type") or "").lower()
            return ("bracket_stop" if "stop" in typ else "bracket_target"), float(leg["filled_avg_price"])
    return None, None


def _finalize(row, reason, exit_px, order_id, now) -> dict:
    AT = _AT()
    qty = row.get("qty") or 0.0
    entry = row.get("entry_px")
    mult = 100 if row.get("occ") else 1
    f = {"exit_ts": now.isoformat(timespec="seconds") if hasattr(now, "isoformat") else _nowiso(),
         "exit_px": exit_px, "exit_reason": reason, "exit_order_id": order_id,
         "exit_pending_reason": None, "graded_at": _nowiso(), "status": "closed"}
    pnl = None
    parts = [(float(x["qty"]), float(x["px"])) for x in _parts(row)]
    rem = _remaining(row)
    if exit_px is not None and rem > 1e-9:
        parts.append((rem, float(exit_px)))
    if parts:                                   # quantity-weighted exit over every fill
        qty = sum(q for q, _ in parts)
        exit_px = parts[0][1] if len(parts) == 1 else round(sum(q * x for q, x in parts) / qty, 6)
        f["exit_px"] = exit_px
    if exit_px is not None and entry:
        pnl = (exit_px - entry) * qty * mult
        f["pnl_usd"] = pnl
        f["pnl_pct"] = (exit_px - entry) / entry * 100.0
        f["win"] = 1 if pnl > 0 else 0
        stop = row.get("stop")
        if stop is not None and entry > stop:
            f["r_multiple"] = (exit_px - entry) / (entry - stop)
    oj = _oj_before(row)                        # (2026-11-18) its options journal row is closed below (marker first)
    _update_trade(row["id"], f)
    if row.get("occ"):
        _sap_result(row, f, reason)                # short call placed as a put: its own result next to the stock's move
    AT._set_execution(row["rec_id"], {"state": "closed", "closed_at": _nowiso(),
                                      "exit_reason": reason, "pnl_usd": pnl})
    sym = row.get("occ") or row.get("ticker")
    if pnl is not None:
        pct = f["pnl_pct"]
        pl = f"P&L {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pct:+.1f}%)"
    else:
        pl = "P&L n/a (exit price unknown)"
    title = (f"Exit (paper, auto): SELL {qty:g} {sym} @ "
             f"{('%.2f' % exit_px) if exit_px is not None else 'n/a'} — {reason} — {pl} "
             f"— director rec {row['rec_id']}")
    _notify(title, title, ticker=row.get("ticker"), rec_id=row["rec_id"],
            dedup=f"agent_exit:{row['rec_id']}")
    _activity("agent_exit", title, ticker=row.get("ticker"), rec_id=row["rec_id"],
              extra={"reason": reason, "exit_px": exit_px, "pnl_usd": pnl, "order_id": order_id})
    _oj_after(row, f, oj)
    return {"rec_id": row["rec_id"], "action": "closed", "reason": reason, "exit_px": exit_px, "pnl_usd": pnl}


def _oj_before(row):
    """(2026-11-18) Mason's own option trade is about to be closed: mason_option_journal leaves a marker when it has
    an open options journal row of its own. None for anything else. Never raises."""
    if not row.get("occ"):
        return None
    try:
        import mason_option_journal as OJ
        return OJ.before_close(row)
    except Exception:
        return None


def _oj_after(row, closed, token):
    """(2026-11-18) ...and, as the last step, that journal row is closed from Alpaca's records (retried by the fill
    reconcile and the self-check if it cannot be done now). Never raises; the trade is already closed."""
    if token is None:
        return
    try:
        import mason_option_journal as OJ
        OJ.after_close(row, closed, token)
    except Exception:
        pass


# ── short calls placed as puts (mason_short_put): exits judged on the STOCK's price ──

def _sap(row):
    """{"ticker", "stop", "target"} when this row is a put bought for a short call on the stock (its stop and
    target are prices of the STOCK), else None. Read from the trade's own record (features_json.short_as_put),
    so an open trade keeps its exits after the rule is switched off."""
    try:
        if not row.get("occ") or '"short_as_put"' not in str(row.get("features_json") or ""):
            return None
        import mason_short_put as SP
        return SP.levels_of(_feat(row))
    except Exception:
        return None


def _sap_check(row, sap, exits, cfg, now, ctx, reason=None):
    """The stock against the call's stop and target, for a put bought for a short call. Returns "stop" /
    "target" when hit (first decision), or — with `reason`, for the re-check before an order is sent — True /
    False / None for that one level. Never judged without a trustworthy stock price; like an option stop
    (exits.opt_stop_guard), the stop is not judged in the first minutes after the open."""
    import mason_short_put as SP
    q = _quote(sap["ticker"], False, cfg)
    ctx["underlying_quote"] = q
    s, t = SP.underlying_check(q, sap["stop"], sap["target"])
    if s is True and exits.get("opt_stop_guard", True) and _in_open_delay(now, exits):
        s = None
    if reason is not None:
        return s if reason == "stop" else t
    hit = "stop" if s is True else ("target" if t is True else None)
    if hit:
        row.update(_set_feat(row, exit_basis="underlying",
                             exit_trigger_underlying=_compact_quote(q, reason=hit, level=sap[hit])))
    return hit


def _sap_result(row, closed, reason):
    """On close of a put bought for a short call: the put's own result and what the stock did meanwhile, kept
    on the trade (features_json.short_as_put_result). Never raises; nothing for any other row."""
    try:
        rec = _feat(row).get("short_as_put")
        if not isinstance(rec, dict) or rec.get("converted") is not True:
            return
        import mason_short_put as SP
        try:
            q = _quote(row["ticker"], False, _cfg(), relaxed=True)
        except Exception:
            q = None
        row.update(_set_feat(row, short_as_put_result=SP.result(rec, row, closed, reason, q)))
    except Exception:
        pass


def _sap_book_words(row, cfg):
    """Words for the director's list of open positions about a put bought for a short call, or None (= the
    line is written as before). Only with autotrade.short_as_put.enabled AND .tell_director."""
    try:
        if not row.get("occ") or '"short_as_put"' not in str(row.get("features_json") or ""):
            return None
        import mason_short_put as SP
        return SP.book_words(_feat(row), cfg)
    except Exception:
        return None


def _sap_waiting(row, reason):
    """(review) A put bought for a short call whose exit was decided on the STOCK (it reached the call's stop or
    target) but which has no usable price to be sold at right now: one plain-words note a day. Nothing else
    changes: the exit is checked again on the stock and priced again at every tick, and never sent at any price."""
    try:
        sap = _sap(row) or {}
        t = sap.get("ticker") or str(row.get("ticker") or "").upper()
        lvl = _f(sap.get(reason))
        sym = row.get("occ") or t
        _notify(f"Exit STALLED (no price for the put): {sym} — director rec {row['rec_id']}",
                f"{t} has reached the call's {'stop' if reason == 'stop' else 'target'}"
                + (f" ({lvl:g})" if lvl is not None else "")
                + ", so Mason wants to sell this put, but the put's buy and sell prices are missing or too far apart "
                  "right now. Mason does not sell it at any price: it checks again every few minutes and sells as "
                  f"soon as there is a usable price, unless {t} moves back first. You can also sell it yourself.",
                ticker=row.get("ticker"), rec_id=row["rec_id"], severity="warning",
                dedup=f"agent_stall:{row['rec_id']}:no_put_price:{_now_et().date().isoformat()}")
    except Exception:
        pass


def _levels(row, exits) -> tuple:
    """(stop, target) of a row: its own levels, else the configured default percents (None = none)."""
    entry = row.get("entry_px")
    stop, target = row.get("stop"), row.get("target")
    if row.get("occ"):
        if stop is None and entry and exits.get("default_opt_stop_pct") is not None:
            stop = entry * (1 - float(exits["default_opt_stop_pct"]) / 100.0)
        if target is None and entry and exits.get("default_opt_target_pct") is not None:
            target = entry * (1 + float(exits["default_opt_target_pct"]) / 100.0)
    elif stop is None and entry and exits.get("default_stock_stop_pct") is not None:
        stop = entry * (1 - float(exits["default_stock_stop_pct"]) / 100.0)
    return stop, target


def _in_open_delay(now, exits) -> bool:
    """True in the first exits.stop_open_delay_min minutes after the 09:30 ET open."""
    try:
        d = float(exits.get("stop_open_delay_min") or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return False
    return now < now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=d)


def _check_levels(row, q, stop, target, exits, now) -> tuple:
    """What this quote says about the row's stop and target: (stop_hit, target_hit), each True,
    False, or None = cannot be judged right now (no trustworthy quote; no such level; or, for an
    option stop, inside the opening minutes). None never triggers and never withdraws anything.

      stock   last trade (or minute-bar close / tight mid) against the level — never a thin bid.
      option  X1: needs a real bid and a fresh two-sided quote inside the spread limit; the stop is
              judged on the MID (exits.opt_stop_guard off: on the validated bid), the target on the mid.
      legacy  (quotes switched off or unavailable) the old rules on the old Alpaca quote."""
    is_opt = bool(row.get("occ"))
    s = t = None
    if q.get("legacy"):
        if is_opt:
            if q.get("ok") or q.get("bid"):
                if stop is not None and q.get("bid") is not None:
                    s = q["bid"] <= stop
                if target is not None and q.get("mid") is not None:
                    t = q["mid"] >= target
        elif q.get("ok"):
            sp, tp = (q.get("bid") or q.get("last")), (q.get("last") or q.get("bid"))
            if stop is not None and sp is not None:
                s = sp <= stop
            if target is not None and tp is not None:
                t = tp >= target
        return s, t
    if is_opt:
        if not (q.get("ok") and q.get("two_sided") and q.get("bid") and q.get("mid")):
            return None, None
        guard = bool(exits.get("opt_stop_guard", True))
        if stop is not None and not (guard and _in_open_delay(now, exits)):
            s = (q["mid"] if guard else q["bid"]) <= stop
        if target is not None:
            t = q["mid"] >= target
        return s, t
    if not (q.get("ok") and q.get("px")):
        return None, None
    two = bool(q.get("two_sided") and q.get("bid") and q.get("ask"))
    if stop is not None:                               # one bad print cannot sell while the market is bid above the stop
        s = q["px"] <= stop and (not two or q["bid"] <= stop)
    if target is not None:
        t = q["px"] >= target and (not two or q["ask"] >= target)
    return s, t


def _confirm_stop(row, q, exits) -> bool:
    """X1: count one check that saw the option stop hit. True once exits.opt_stop_confirm_ticks checks
    in a row agree (default 2). Two checks less than a minute apart are one observation; a gap longer
    than exits.opt_stop_confirm_window_s starts the count again. Kept on the trade, so a restart
    between the two checks does not lose (or fake) a confirmation. Updates `row` in place."""
    need = max(1, int(exits.get("opt_stop_confirm_ticks") or 2))
    window = float(exits.get("opt_stop_confirm_window_s") or 420)
    now_s = _now_s()
    w = dict(_feat(row).get("stop_watch") or {})
    n, last = int(w.get("n") or 0), _f(w.get("last_at"))
    if n and last is not None:
        gap = now_s - last
        if gap > window or gap < 0:
            n = 0
        elif gap < _CONFIRM_MIN_GAP_S:
            return n >= need
    n += 1
    row.update(_set_feat(row, stop_watch={"n": n, "first_at": (w.get("first_at") if n > 1 else now_s),
                                          "last_at": now_s, "mid": q.get("mid"), "bid": q.get("bid"),
                                          "ask": q.get("ask"), "source": q.get("source")}))
    return n >= need


def _clear_stop_watch(row):
    if _feat(row).get("stop_watch"):
        row.update(_set_feat(row, stop_watch=None))


def _decide(row, execu, trade, cfg, now, clk=None, ctx=None, hard_only=False):
    """First matching exit reason, or None. Quotes are fetched lazily; the quote a stop/target was
    judged on is left in ctx["quote"] so the order is priced from the same observation."""
    ctx = ctx if ctx is not None else {}
    exits = cfg.get("exits") or {}
    today = now.date()
    is_opt = bool(row.get("occ"))
    if execu.get("close_requested"):
        return "director_close"
    exp = _occ_expiry(row.get("occ"), trade) if is_opt else None
    if is_opt and exp:
        if exp == today:
            # X3: "15:15" means 45 minutes before a normal close; on a half-day the flat-by time
            # moves with the broker clock's real close (12:15 for a 13:00 close).
            cut, _txt = _AT().close_cutoff(cfg.get("zero_dte_flat_by_et"), "15:15",
                                           clk if clk is not None else _clock(), now, cfg)
            if _oo_on(cfg):                            # exit stepping ON: start early enough to be AT the bid
                try:                                       # at the cut-off (the same time when it is off)
                    cut = _OO().flat_by_start(cut, now, clk if clk is not None else _clock(), cfg)
                except Exception:
                    pass
            if now >= cut:
                return "flat_by"
        entry_d = str(row.get("entry_ts") or "")[:10]
        dte_n = int(exits.get("opt_exit_dte", 1))          # trading days, not calendar days (2026-11-18): the last
        if _sessions_until(today, exp, stop_at=dte_n) <= dte_n and exp.isoformat() != entry_d \
                and entry_d != today.isoformat():        # trading day before expiry, also over a weekend or a holiday
            return "pre_expiry"
    er = execu.get("exit_rules") or {}
    stop, target = _levels(row, exits)
    sap = None if hard_only else _sap(row)
    if sap:                                        # a put bought for a short call: the stock's price decides
        hit = _sap_check(row, sap, exits, cfg, now, ctx)
        if hit:
            return hit
    if not hard_only and (is_opt or not er.get("enforced_at_broker")) and (stop is not None or target is not None):
        q = _quote(row["occ"] if is_opt else row["ticker"], is_opt, cfg)
        ctx["quote"] = q
        s, t = _check_levels(row, q, stop, target, exits, now)
        if is_opt and not q.get("legacy") and exits.get("opt_stop_guard", True):
            if s is True:
                if _confirm_stop(row, q, exits):
                    return "stop"
            elif s is False:
                _clear_stop_watch(row)             # a good quote says the stop is not hit: start over
        elif s is True:
            return "stop"
        if t is True:
            return "target"
    tsd = row.get("time_stop_date")
    if tsd and today.isoformat() >= str(tsd)[:10]:
        return "time_stop"
    return None


def _revalidate(row, reason, cfg, now, ctx):
    """Does a stop/target decided earlier still hold on a fresh price? True / False / None (cannot be
    judged this tick: no trustworthy quote, or an option stop inside the opening minutes)."""
    exits = cfg.get("exits") or {}
    sap = _sap(row)
    if sap and _feat(row).get("exit_basis") == "underlying":   # decided on the stock's price: re-checked on it
        still = _sap_check(row, sap, exits, cfg, now, ctx, reason=reason) if sap.get(reason) is not None else False
        if still is not True:                      # the withdrawal / the wait is recorded with the STOCK's price
            ctx["quote"] = ctx.get("underlying_quote")
        if still is False:
            row.update(_set_feat(row, exit_basis=None))
        return still
    stop, target = _levels(row, exits)
    if (stop if reason == "stop" else target) is None:
        return False                               # the level itself is gone
    is_opt = bool(row.get("occ"))
    q = _quote(row["occ"] if is_opt else row["ticker"], is_opt, cfg)
    ctx["quote"] = q
    s, t = _check_levels(row, q, stop, target, exits, now)
    return s if reason == "stop" else t


def _withdraw(row, reason, q) -> dict:
    """X1: the stop/target that was waiting no longer holds on a fresh price — take it back. A new
    one has to be decided (and, for an option stop, confirmed) from scratch."""
    n = int(_feat(row).get("exit_withdrawn") or 0) + 1
    row.update(_set_feat(row, exit_withdrawn=n, exit_withdrawn_at=_nowiso(), stop_watch=None,
                         exit_withdrawn_quote=_compact_quote(q, reason=reason)))
    _update_trade(row["id"], {"exit_pending_reason": None})
    row["exit_pending_reason"] = None
    sym = row.get("occ") or row.get("ticker")
    _activity("agent_exit_withdrawn", f"Exit withdrawn: {sym} — the {reason} no longer holds on a fresh price",
              ticker=row.get("ticker"), rec_id=row["rec_id"],
              extra={"reason": reason, "px": (q or {}).get("px"), "source": (q or {}).get("source")})
    return {"rec_id": row["rec_id"], "action": "exit_withdrawn", "reason": reason, "px": (q or {}).get("px")}


def _entry_utc(row):
    """The entry time as an aware UTC datetime (entry_ts is local time unless it carries an offset)."""
    from datetime import timezone
    raw = str(row.get("entry_ts") or "")
    for txt in (raw.replace("Z", "+00:00"), raw[:19]):
        try:
            t0 = datetime.fromisoformat(txt)
            return (t0 if t0.tzinfo else t0.astimezone()).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _own_sales(row, fills) -> tuple:
    """(quantity, dollars per unit x quantity) of the sell fills on this contract that are this row's:
    after its entry, not already booked on it, and never more than its remaining quantity."""
    t0 = _entry_utc(row)
    booked = {x.get("order_id") for x in _parts(row)}
    left, got, notional = _remaining(row), 0.0, 0.0
    try:
        ft = _EX()._fill_time
    except Exception:
        ft = lambda s_: None                       # noqa: E731
    for f in sorted(fills or [], key=lambda f_: str(f_.get("filled_at") or "")):
        fq, fp = _f(f.get("qty")), _f(f.get("price"))
        if not fq or not fp or (f.get("order_id") and f.get("order_id") in booked):
            continue
        when = ft(f.get("filled_at"))
        if t0 is not None and (when is None or when < t0):
            continue
        take = min(fq, left - got)
        if take <= 1e-9:
            break
        got += take
        notional += take * fp
    return got, notional


def _expired(row, exp, cfg, now) -> dict:
    """X8. An option past its expiry is no longer in the account. Before booking it at zero, ask the
    broker how it left (mason_execution.settlement_disposition, read-only):
      sold                  → closed at the real sale price ("closed_outside");
      expired worthless     → booked at 0 ("expired");
      expired with value    → closed with NO price and no win/loss (it was not worthless; it may have
                              been exercised into shares) + one note asking Nico to look;
      no record yet / no answer → wait (exits.expiry_settle_wait_days, default 3), then the old rule."""
    exits = cfg.get("exits") or {}
    if not exits.get("check_settlement_on_expiry", True):
        return _finalize(row, "expired", 0.0, None, now)
    rid, sym = row["rec_id"], row.get("occ")
    try:
        d = _settlement(sym) or {}
    except Exception as e:
        d = {"ok": False, "error": f"{type(e).__name__}"}
    left = _remaining(row)
    sold_q, sold_usd = _own_sales(row, d.get("sell_fills")) if d.get("ok") else (0.0, 0.0)
    if d.get("ok") and left > 0 and sold_q >= left - 1e-9:
        return _finalize(row, "closed_outside", sold_usd / sold_q, None, now)
    op = d.get("opexp") if d.get("ok") else None
    if op is None and d.get("ok"):                 # an exercise is a separate broker record (OPEXC), never an OPEXP
        try:
            code, acts = _AT()._get("/account/activities/OPEXC", params={"page_size": 100})
            if code == 200 and isinstance(acts, list) and any(
                    str(a.get("symbol") or "").upper() == str(sym).upper() for a in acts):
                op = {"net_amount": 1, "exercised": True}      # → "expired with value": no P&L booked, Nico is told
        except Exception:
            pass
    waited = (now.date() - exp).days
    if op is None and waited <= int(exits.get("expiry_settle_wait_days", 3) or 0):
        return {"rec_id": rid, "action": "awaiting_settlement",
                "detail": str(d.get("error") or "the broker has no expiry record for it yet")[:160]}
    net = (_f(op.get("net_amount")) or 0.0) if op is not None else 0.0
    if net != 0:
        row.update(_set_feat(row, expiry_settlement={"net_amount": net, "kind": "expired_with_value"}))
        what = sym
        try:
            o = _AT()._MQ().parse_occ(sym)
            what = f"{o['underlying']} {o['strike']:g} {o['type']} that expired {o['expiry']}"
        except Exception:
            pass
        _notify(f"Check the account: the {what} expired with value",
                f"An option Mason held (the {what}) expired with value instead of expiring worthless. It "
                "may have been turned into shares. Mason booked no profit or loss for it and will not "
                "touch any shares that came from it — please look at the account.",
                ticker=row.get("ticker"), rec_id=rid, severity="warning", dedup=f"agent_expiry_value:{rid}")
        return _finalize(row, "expired_with_value", None, None, now)
    if sold_q > 0 and left > 0:                    # part was sold, the rest expired worthless
        return _finalize(row, "closed_outside", sold_usd / left, None, now)
    return _finalize(row, "expired", 0.0, None, now)


def _feat(row) -> dict:
    try:
        return dict(json.loads(row.get("features_json") or "{}"))
    except Exception:
        return {}


def _set_feat(row, **kv) -> dict:
    feat = _feat(row)
    feat.update(kv)
    js = json.dumps(feat, default=str)
    _update_trade(row["id"], {"features_json": js})
    return dict(row, features_json=js)


def _parts(row) -> list:
    return list(_feat(row).get("partial_exits") or [])


def _remaining(row) -> float:
    """Mason's own quantity still open = entry qty − every exit fill so far."""
    return max(0.0, float(row.get("qty") or 0) - sum(float(x.get("qty") or 0) for x in _parts(row)))


def _stall(row, action, detail) -> dict:
    """An exit was decided but cannot proceed: tell Nico once per rec per day per cause."""
    rid = row["rec_id"]
    sym = row.get("occ") or row.get("ticker")
    title = f"Exit STALLED ({action}): {sym} — director rec {rid}"
    _notify(title, f"Mason decided to exit but could not: {detail}\nIt retries every tick.",
            ticker=row.get("ticker"), rec_id=rid, severity="warning",
            dedup=f"agent_stall:{rid}:{action}:{_now_et().date().isoformat()}")
    return {"rec_id": rid, "action": action, "detail": str(detail)[:200]}


def _apply_exit_fill(row, fq, px, order_id, reason, now):
    """Book one exit fill. Returns (row, result|None); result is set when the row closed."""
    parts = _parts(row) + [{"qty": float(fq), "px": float(px), "order_id": order_id,
                            "at": _nowiso()}]
    row = _set_feat(row, partial_exits=parts)
    _update_trade(row["id"], {"exit_order_id": None, "exit_pending_reason": reason})
    row = dict(row, exit_order_id=None, exit_pending_reason=reason)
    if row.get("occ") and '"exit_orders"' in str(row.get("features_json") or ""):
        try:                                               # option order handling: the fill next to its order's prices
            row = _OO().exit_fill(row, fq, px, order_id, reason)
        except Exception:
            pass
    if _remaining(row) <= 1e-9:
        return row, _finalize(row, reason, None, order_id, now)
    return row, None


def _refresh_entry(row) -> dict:
    """Until the entry order is terminal, keep qty/entry_px equal to its real fill (a
    partial fill marked 'placed' may keep filling)."""
    feat = _feat(row)
    if feat.get("entry_final") or not feat.get("order_id"):
        return row
    g = _get_order(feat["order_id"])
    if not g.get("ok"):
        return row
    o = g["order"]
    fq, px = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
    upd = {}
    if fq and px and (abs(fq - float(row.get("qty") or 0)) > 1e-9 or px != row.get("entry_px")):
        upd = {"qty": fq, "entry_px": px, "cost_usd": fq * px * (100 if row.get("occ") else 1)}
        _update_trade(row["id"], upd)
    row = dict(row, **upd)
    if (o.get("status") or "").lower() in _TERMINAL:
        row = _set_feat(row, entry_final=True)
    return row


def _last_sell_fill(symbol, after_iso):
    """Average price of the most recent FILLED sell of `symbol` after `after_iso` (None if none)."""
    try:
        if not after_iso:
            return None
        from datetime import timezone
        t0 = datetime.fromisoformat(str(after_iso)[:19])
        t0 = (t0 if t0.tzinfo else t0.astimezone()).astimezone(timezone.utc)   # entry_ts is local time
        code, d = _AT()._get("/orders", params={"status": "closed", "symbols": symbol, "limit": 50,
                                               "direction": "desc",
                                               "after": t0.strftime("%Y-%m-%dT%H:%M:%SZ")})
        for o in (d if (code == 200 and isinstance(d, list)) else []):
            if str(o.get("symbol") or "").upper() != str(symbol).upper():
                continue
            try:
                ft = datetime.fromisoformat(str(o.get("filled_at") or o.get("submitted_at")).replace("Z", "+00:00"))
                ft = ft if ft.tzinfo else ft.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ft < t0:
                continue
            if (o.get("side") or "").lower() == "sell" and _f(o.get("filled_qty")) and _f(o.get("filled_avg_price")):
                return _f(o.get("filled_avg_price"))
    except Exception:
        pass
    return None


def _follow_fill(row, execu):
    """X7: once the entry order has filled more than the call (and its journal row) recorded, bring
    both up to the real fill. agent_trades.qty already follows the order (_refresh_entry)."""
    try:
        q_row, q_rec = _f(row.get("qty")), _f(execu.get("qty"))
        if execu.get("state") == "placed" and q_row and q_rec is not None and q_row - q_rec > 1e-9 \
                and row.get("entry_px"):
            return _AT().follow_entry_fill(row["rec_id"], q_row, row["entry_px"])
    except Exception:
        pass
    return None


def _lost_sale(row, wait_on_error=False):
    """(2026-11-18; stock trades too since the gap fixes of 2026-11-18) A sale of THIS trade's option or shares
    that reached the broker but was never written down: Mason stopped
    in the few seconds between sending it (_close_position, client order id 'mason-exit-<rec id>-...') and recording
    its id. Without this look-up, once such a sale has filled, Mason's record still shows the contracts, and the next
    sale of "what is left" reaches someone else's contracts or shares in the same position (Nico's). Read-only: one
    order-list read for this contract or stock.
        None                               nothing of the kind; or the list could not be read (the caller sells as
                                           before) unless `wait_on_error`, then {"wait": True}
        {"open": True, "order_id"}         such a sale is still open: it becomes the trade's sale again
        {"open": False, "order_id", "qty", "px"}   such a sale has filled (at least in part): it is booked
    Never raises."""
    try:
        sym = str(row.get("occ") or row.get("ticker") or "").upper()
        if not sym or not row.get("rec_id"):
            return None
        code, d = _AT()._get("/orders", params={"status": "all", "symbols": sym, "limit": 200, "nested": "true"})
        if code != 200 or not isinstance(d, list):
            return {"wait": True} if wait_on_error else None
        pre = f"mason-exit-{row['rec_id']}-"
        booked = {x.get("order_id") for x in _parts(row)}
        mine = [o for o in d if str(o.get("client_order_id") or "").startswith(pre)
                and str(o.get("side") or "").lower() == "sell" and str(o.get("symbol") or "").upper() == sym
                and o.get("id") and o.get("id") not in booked and o.get("id") != row.get("exit_order_id")]
        for o in mine:
            if str(o.get("status") or "").lower() not in _TERMINAL:
                return {"open": True, "order_id": o["id"], "submitted_at": o.get("submitted_at")}
        for o in mine:
            fq, px = _f(o.get("filled_qty")) or 0.0, _f(o.get("filled_avg_price")) or _f(o.get("limit_price"))
            qty = min(fq, _remaining(row))
            if qty > 1e-9 and px:
                return {"open": False, "order_id": o["id"], "qty": qty, "px": px, "submitted_at": o.get("submitted_at")}
        return None
    except Exception:
        return {"wait": True} if wait_on_error else None


def _local_iso(ts):
    """(review, gap fixes) An order's time as Alpaca sends it (UTC, 'Z', up to 9 decimals) as Mason's own naive local
    time (the form exit_submitted_at is kept in), or None. A time without a zone is taken as local. Never raises."""
    try:
        s = str(ts or "").strip()
        if not s:
            return None
        if s[-1] in "Zz" or re.search(r"[+-]\d\d:?\d\d$", s):
            t = _EX()._fill_time(s)
            return t.astimezone().replace(tzinfo=None).isoformat(timespec="seconds") if t is not None else None
        return datetime.fromisoformat(s[:19]).isoformat(timespec="seconds")
    except Exception:
        return None


def _taken_back(row, lost) -> str:
    """(review, gap fixes) The trade's features once a lost sale that is still OPEN was found: it counts as this exit's
    latest sale. It rests like a sale Mason has just sent (re-priced only _REPRICE_AFTER_S after it really went out),
    and the next sale gets the next order number. Before, the sale found was cancelled at the very next look, and the
    one after it went out under the SAME number; at an unchanged price and size that is the same client order id,
    which the broker refuses, so the exit stalled with nothing left at the broker. (A lost sale that already filled
    is booked as before: the sale after it is for a different size.) Returns the features JSON to write."""
    feat = _feat(row)
    feat["exit_attempts"] = int(feat.get("exit_attempts") or 0) + 1
    feat["exit_submitted_at"] = _local_iso(lost.get("submitted_at")) or _nowiso()
    return json.dumps(feat, default=str)


def _manage_one(row, cfg, now, clk=None) -> dict:
    AT = _AT()
    rid = row["rec_id"]
    rec = AT._load_rec(rid) or {}
    ex = rec.get("extra") or {}
    execu = ex.get("execution") or {}
    trade = AT._placed_trade(ex)                   # the put when a short call was placed as a put; else the call's own block
    exits = cfg.get("exits") or {}
    symbol = row.get("occ") or row.get("ticker")
    is_opt = bool(row.get("occ"))
    row = _refresh_entry(row)
    _follow_fill(row, execu)

    # (2026-11-18, review) Mason stopped after booking an exit fill but before closing the trade. An exit order that
    # is already booked is never booked again (a stop between the two writes of _apply_exit_fill), and a trade whose
    # own remaining quantity is 0 is closed from its booked fills with its own exit reason; nothing is sent.
    booked = _parts(row)
    if booked:
        if row.get("exit_order_id") and row["exit_order_id"] in {x.get("order_id") for x in booked}:
            _update_trade(row["id"], {"exit_order_id": None})
            row = dict(row, exit_order_id=None)
        if not row.get("exit_order_id") and _remaining(row) <= 1e-9:
            return _finalize(row, row.get("exit_pending_reason") or "unknown", None, booked[-1].get("order_id"), now)

    # 0. one of OUR exit orders is out: book what it filled; re-price it if it rests
    if row.get("exit_order_id"):
        oid = row["exit_order_id"]
        g = _get_order(oid)
        if not g.get("ok"):
            return {"rec_id": rid, "action": "exit_pending", "error": str(g.get("error"))[:120]}
        o = g["order"]
        st = (o.get("status") or "").lower()
        if is_opt and st != "filled" and st not in _DEAD and _oo_on(cfg):
            try:                                           # exit stepping ON: cancel, wait for the broker's word, and
                wk = _OO().exit_step(row, o, st, cfg, now, clk)      # re-price in this same look (None when it is off)
            except Exception:
                wk = None
            if wk is not None:
                if wk.get("order") is None:                # nothing to do yet, or the cancel is not confirmed
                    return {"rec_id": rid, "action": wk.get("action") or "exit_pending", "order_status": st}
                o = wk["order"]                            # finished at the broker: booked just below
                st = (o.get("status") or "").lower()
        if st == "filled" or st in _DEAD:
            fq = _f(o.get("filled_qty"))
            # a fill is NEVER dropped: quantity safety comes first, so fall back on price
            px = _f(o.get("filled_avg_price")) or _f(o.get("limit_price")) or _f(row.get("entry_px"))
            pending = row.get("exit_pending_reason") or "unknown"
            if fq and px:
                row, done = _apply_exit_fill(row, fq, px, oid, pending, now)
                if done:
                    return done
                return {"rec_id": rid, "action": "partial_exit", "filled": fq,
                        "remaining": _remaining(row)}
            _update_trade(row["id"], {"exit_order_id": None})
            row = dict(row, exit_order_id=None)        # died unfilled → retry below
        else:
            try:
                age = (datetime.now() - datetime.fromisoformat(
                    str(_feat(row).get("exit_submitted_at")))).total_seconds()
            except Exception:
                age = _REPRICE_AFTER_S + 1
            if age > _REPRICE_AFTER_S:
                _cancel_order(oid)                     # next tick books any fill, then re-prices
                return {"rec_id": rid, "action": "exit_repricing", "order_id": oid}
            return {"rec_id": rid, "action": "exit_pending", "order_status": st}

    # 1. broker bracket leg already filled (stock) → finalize from the leg
    order_id = execu.get("order_id")
    bracket = bool((execu.get("exit_rules") or {}).get("enforced_at_broker")) and not is_opt
    legs = _legs(order_id) if (order_id and bracket) else []
    reason, px = _leg_exit(legs)
    if reason:
        return _finalize(row, reason, px, None, now)

    pos = _get_position(symbol)
    if not pos.get("ok"):
        if row.get("exit_pending_reason"):
            return _stall(row, "position_lookup_error", pos.get("error"))
        return {"rec_id": rid, "action": "error", "error": f"position lookup: {pos.get('error')}"}
    if not pos.get("exists"):
        exp = _occ_expiry(row.get("occ"), trade) if is_opt else None
        if exp and exp < now.date():
            return _expired(row, exp, cfg, now)        # X8: sold / exercised / worthless — ask the broker first
        _r, lpx = _leg_exit(_legs(order_id) if (order_id and not is_opt) else [])
        if lpx is None:                            # sold outside the loop (e.g. by Nico in the UI)
            spx = _last_sell_fill(symbol, row.get("entry_ts"))
            if spx is not None:
                return _finalize(row, "closed_outside", spx, None, now)
        return _finalize(row, "position_gone", lpx, None, now)
    if (pos.get("side") or "long") != "long":
        # a net-short position can only come from the owner; this loop only ever sells-to-close
        return {"rec_id": rid, "action": "skip_not_long"}

    ctx = {}
    reason = row.get("exit_pending_reason")
    if reason:
        if reason in _SOFT_EXITS:
            # an exit that has to happen (flat-by, day before expiry, director close, time stop)
            # outranks a stop/target that is still waiting for a price it can be judged on
            must = _decide(row, execu, trade, cfg, now, clk, None, hard_only=True)
            if must:
                reason = must
                _update_trade(row["id"], {"exit_pending_reason": reason})
                row["exit_pending_reason"] = reason
                row.update(_set_feat(row, stop_watch=None,
                                     exit_attempt_base=int(_feat(row).get("exit_attempts") or 0)))
        # X1: a stop/target decided on an earlier tick is checked again on a fresh price before
        # anything is sent. Flat-by, day-before-expiry, director close and the time stop stand.
        if reason in _SOFT_EXITS and exits.get("revalidate_before_send", True):
            still = _revalidate(row, reason, cfg, now, ctx)
            if still is False:
                return _withdraw(row, reason, ctx.get("quote"))
            if still is None:
                return {"rec_id": rid, "action": "exit_waiting_for_price", "reason": reason,
                        "detail": str((ctx.get("quote") or {}).get("reason") or "cannot be judged right now")[:160]}
    else:
        reason = _decide(row, execu, trade, cfg, now, clk, ctx)
        if not reason:
            return {"rec_id": rid, "action": "hold"}
        _update_trade(row["id"], {"exit_pending_reason": reason})
        row["exit_pending_reason"] = reason
        base = int(_feat(row).get("exit_attempts") or 0)       # band steps count from this decision
        if cfg.get("record_quotes", True) and ctx.get("quote"):
            row.update(_set_feat(row, exit_attempt_base=base,
                                 exit_trigger_quote=_compact_quote(ctx["quote"], reason=reason)))
        else:
            row.update(_set_feat(row, exit_attempt_base=base))
    feat = _feat(row)
    if feat.get("order_id") and not feat.get("entry_final") and not feat.get("entry_cancel_sent"):
        _cancel_order(feat["order_id"])            # stop the entry from filling more after we exit
        _set_feat(row, entry_cancel_sent=_nowiso())
        return {"rec_id": rid, "action": "entry_remainder_cancelled", "reason": reason}

    # our own live bracket legs hold the shares (and would look like a resting sell): cancel
    # exactly those, by id from the parent order, and close on the next tick
    if bracket:
        all_legs = _legs(order_id)                 # fresh: statuses change between ticks
        live = [l for l in all_legs
                if (l.get("status") or "").lower() not in _TERMINAL and l.get("id")]
        if live:
            cancelled = [l["id"] for l in live if _cancel_order(l["id"])]
            failed = [l["id"] for l in live if l["id"] not in cancelled]
            if failed and exits.get("tolerate_sibling_cancel", True):
                # X9: cancelling one bracket leg makes the broker cancel its sibling, and the second
                # cancel request is then refused. Look again: a leg that is no longer live is done.
                seen = {l.get("id"): (l.get("status") or "").lower() for l in _legs(order_id)}
                failed = [i for i in failed if seen.get(i) not in _TERMINAL and seen.get(i) != "pending_cancel"]
            if failed:
                return _stall(row, "leg_cancel_failed", f"{len(failed)} bracket leg(s) not cancelled")
            row = _set_feat(row, legs_cancelled_at=datetime.now().isoformat(timespec="seconds"))
            return {"rec_id": rid, "action": "legs_cancelled", "reason": reason, "cancelled": cancelled}
        booked = {x.get("order_id") for x in _parts(row)}
        for l in all_legs:                         # a leg that part-filled before it was cancelled
            fq = _f(l.get("filled_qty"))
            lpx = _f(l.get("filled_avg_price")) or _f(l.get("limit_price")) or _f(l.get("stop_price"))
            if fq and lpx and l.get("id") and l["id"] not in booked:
                row, done = _apply_exit_fill(row, min(fq, _remaining(row)), lpx, l["id"], reason, now)
                if done:
                    return done

    # (2026-11-18) Before ANY sale of one of Mason's options: a sale of this very trade that went out just before a
    # restart and was never written down is found by its client order id, and booked or picked up instead of selling
    # again (autotrade.exits.find_lost_sales, on unless set false). If the list cannot be read, the sale goes as before.
    # (gap fixes, 2026-11-18) The same look-up now runs before a sale of Mason's SHARES (same switch): a stock sale
    # lost the same way was sent a second time, and that second sale could take shares that are Nico's.
    stepping = False                                   # option order handling: with exit stepping ON it always looks,
    if is_opt and _oo_on(cfg):                         # and nothing is sold while the list cannot be read
        try:
            stepping = bool(_OO().live("exit", cfg))
        except Exception:
            stepping = False
    if stepping or exits.get("find_lost_sales", True) is not False:      # (`stepping` is only ever True for an option)
        lost = _lost_sale(row, wait_on_error=stepping)
        if lost and lost.get("wait"):
            return {"rec_id": rid, "action": "exit_waiting_for_broker", "reason": reason}
        if lost and lost.get("open"):                  # (review) one write: the order's id and its place in this exit
            _update_trade(row["id"], {"exit_order_id": lost["order_id"], "exit_pending_reason": reason,
                                      "features_json": _taken_back(row, lost)})
            return {"rec_id": rid, "action": "exit_order_found", "order_id": lost["order_id"]}
        if lost and lost.get("qty"):
            row, done = _apply_exit_fill(row, lost["qty"], lost["px"], lost["order_id"], reason, now)
            if done:
                return done
            return {"rec_id": rid, "action": "partial_exit", "filled": lost["qty"], "remaining": _remaining(row)}
    rem = _remaining(row)
    avail = float(pos.get("qty_available") or 0)
    qty_close = min(rem, avail)                        # NEVER more than Mason's own open qty
    if qty_close <= 0:
        try:
            waited = (datetime.now() - datetime.fromisoformat(
                str(_feat(row).get("legs_cancelled_at")))).total_seconds()
        except Exception:
            waited = None
        if waited is not None and waited < _RELEASE_GRACE_S:
            return {"rec_id": rid, "action": "waiting_release", "reason": reason}
        return _stall(row, "nothing_available", f"own open qty {rem:g}, available at broker {avail:g}")

    # X2: the limit is the last trade / mid minus a small band (or a validated bid inside that band),
    # never a far-away bid. A stop/target is priced from the quote that just confirmed it; an exit
    # that must happen anyway may use an older price (relaxed) rather than not exit at all.
    hard = reason not in _SOFT_EXITS
    q = ctx.get("quote")
    if q is None or (hard and not q.get("ok") and not q.get("legacy")):
        q = _quote(symbol, is_opt, cfg, relaxed=hard)
    feat = _feat(row)
    n = int(feat.get("exit_attempts") or 0) + 1
    step = max(1, n - int(feat.get("exit_attempt_base") or 0))     # 1 on the first order of this exit
    limit, basis = _sell_limit(q, step, reason == "flat_by", cfg)
    wk = None
    if is_opt and _oo_on(cfg):
        try:                                               # option order handling (None when off). Exit stepping ON:
            wk = _OO().exit_hook(row, q, reason, limit, basis, cfg, now, clk)   # start above the bid, step down to it
            if wk and wk.get("live") and wk.get("limit") and limit:
                limit, basis = wk["limit"], wk["basis"]
            if not limit and not hard and feat.get("exit_basis") == "underlying":
                late = _OO().stuck_put(row, reason, cfg, now, clk, ctx)       # None unless that rule is ON and due
                if late:
                    q, limit, basis = late
        except Exception:
            wk = None
    if not limit:
        if not hard:
            if feat.get("exit_basis") == "underlying":  # (review) a put bought for a short call: the STOCK is through
                _sap_waiting(row, reason)              # the call's level, but the put has no price to sell at: say so
            return {"rec_id": rid, "action": "exit_waiting_for_price", "reason": reason, "detail": str(basis)[:160]}
        r = _stall(row, "no_quote", f"there is no reliable price for {symbol} right now, so no sell order "
                                    "was sent")
        r["why"] = str(basis)[:200]                 # the technical reason stays in the tick result
        return r
    res = _close_position(symbol, qty_close, limit, pitch_id=f"exit-{rid}-{n}")
    if res.get("existing_order_id"):
        return _stall(row, "close_blocked_by_resting_sell",
                      f"another sell order for {symbol} is resting (order {res['existing_order_id']})")
    if not res.get("ok"):
        return _stall(row, "close_failed", res.get("error"))
    oid = res.get("order_id")
    # the order id is written FIRST (a restart right here must find the order, not send a second one)
    _update_trade(row["id"], {"exit_order_id": oid, "exit_pending_reason": reason})
    note = {"exit_attempts": n, "exit_submitted_at": datetime.now().isoformat(timespec="seconds")}
    if cfg.get("record_quotes", True):             # the quote this exit order was priced from
        note["exit_quote"] = _compact_quote(q, limit=limit, basis=basis, reason=reason, attempt=n)
    if is_opt and _oo_on(cfg):
        try:                                       # option order handling: every order of the exit, with its prices
            note.update(_OO().exit_sent(row, q, limit, basis, reason, n, res, qty_close, wk))
        except Exception:
            pass
    row = _set_feat(row, **note)
    row = dict(row, exit_order_id=oid)
    fill = res.get("fill") or {}
    if (fill.get("status") or "").lower() == "filled" and fill.get("filled_avg_price"):
        row, done = _apply_exit_fill(row, _f(fill.get("filled_qty")) or qty_close,
                                     float(fill["filled_avg_price"]), oid, reason, now)
        if done:
            return done
        return {"rec_id": rid, "action": "partial_exit", "remaining": _remaining(row)}
    return {"rec_id": rid, "action": "exit_submitted", "reason": reason, "order_id": oid}


def manage_exits(now_et=None) -> list:
    out = []
    try:
        cfg = _cfg()
        if not (cfg.get("exits") or {}).get("enabled"):
            return out
        clk = _clock()
        if not (clk.get("ok") and clk.get("is_open")):
            return out
        now = now_et or _now_et()
        for row in _q("SELECT * FROM agent_trades WHERE status='open' AND source='mason' "
                      "AND COALESCE(record_deleted,0)=0"):
            try:
                out.append(_manage_one(row, cfg, now, clk))
            except Exception as e:
                out.append({"rec_id": row.get("rec_id"), "action": "error",
                            "error": f"{type(e).__name__}: {e}"})
    except Exception as e:
        out.append({"rec_id": None, "action": "error", "error": f"manage_exits: {e}"})
    return out


def cancel_closed_pending() -> list:
    """The director sent close/invalidate for a call whose ENTRY order is still resting:
    cancel that order (settle_pending then marks it unfilled, or placed if part filled —
    in which case the exit manager closes the filled part)."""
    out = []
    try:
        for r in _q("SELECT id, extra_json FROM recommendations WHERE kind='director' "
                    "AND json_extract(extra_json,'$.execution.state')='approved_pending_fill' "
                    "AND json_extract(extra_json,'$.execution.close_requested') IS NOT NULL "
                    "AND json_extract(extra_json,'$.execution.approver')='auto'"):
            e = (json.loads(r["extra_json"] or "{}").get("execution") or {})
            if e.get("order_id") and not e.get("entry_cancel_sent"):
                ok = _cancel_order(e["order_id"])
                _AT()._set_execution(r["id"], {"entry_cancel_sent": _nowiso()})
                out.append({"rec_id": r["id"], "cancelled": bool(ok)})
    except Exception as ex_:
        out.append({"error": f"cancel_closed_pending: {ex_}"})
    return out


# ── what the director is shown (P1): record, book, same-day chain ────────────────

def _rate(w, n, min_n=10) -> str:
    return f"{w}/{n} ({w / n * 100:.0f}%)" + ("" if n >= min_n else " [n<10: weak]") if n else "0/0"


def what_works_text() -> str:
    """Short numeric record: graded directional calls by horizon/vehicle + realized P&L of
    Mason's own closed trades + the calibration and sizing actually in use."""
    try:
        cfg = _cfg()
        g = _graded_calls()
        L = []
        if g:
            L.append("DIRECTIONAL CALLS graded on the underlying at horizon: all "
                     + _rate(sum(x["win"] for x in g), len(g)))
            for key, label in (("horizon", "by horizon"), ("vehicle", "by vehicle")):
                agg = {}
                for x in g:
                    d = agg.setdefault(x[key] or "?", [0, 0]); d[0] += x["win"]; d[1] += 1
                L.append(f"  {label}: " + "; ".join(f"{k} {_rate(w, n)}" for k, (w, n) in
                                                      sorted(agg.items(), key=lambda kv: -kv[1][1])))
        rows = _q("SELECT horizon, vehicle, tier, pnl_usd, r_multiple, win, exit_reason FROM agent_trades "
                  "WHERE source='mason' AND status='closed' AND win IS NOT NULL AND COALESCE(record_deleted,0)=0")
        if rows:
            L.append(f"PLACED TRADES closed (real P&L): {_rate(sum(r['win'] for r in rows), len(rows))} winners, "
                     f"total ${sum(r['pnl_usd'] or 0 for r in rows):+,.0f}")
            agg = {}
            for r in rows:
                d = agg.setdefault(f"{r['horizon']}/{r['vehicle']}", [0, 0, 0.0])
                d[0] += r["win"]; d[1] += 1; d[2] += r["pnl_usd"] or 0
            L.append("  by horizon/vehicle: " + "; ".join(f"{k} {_rate(w, n)} ${pl:+,.0f}"
                                                          for k, (w, n, pl) in sorted(agg.items(), key=lambda kv: -kv[1][1])))
            ex = {}
            for r in rows:
                d = ex.setdefault(r["exit_reason"] or "?", [0, 0.0]); d[0] += 1; d[1] += r["pnl_usd"] or 0
            L.append("  by exit: " + "; ".join(f"{k} n={n} ${pl:+,.0f}" for k, (n, pl) in ex.items()))
        else:
            L.append("PLACED TRADES closed (real P&L): none yet — the first placed trades are still open.")
        b = base_rate()
        t = cfg.get("tiers") or {}
        size_txt = (f"Size is set by Mason (about ${(t.get('learning') or {}).get('usd_max')} per trade now; "
                    f"calls <= ${cfg.get('call_premium_cap_usd')}, puts <= ${cfg.get('put_premium_cap_usd')}), "
                    "so one option contract must cost less than that.")
        try:                                       # Step 4: say how size is really set once size-by-risk is on
            import mason_sizing as SZ              # (autotrade.sizing.enabled AND .show_book; else the old sentence)
            size_txt = SZ.size_words(cfg, size_txt)
        except Exception:
            pass
        L.append(f"IN USE: your stated confidence c is mapped to p = {b['p_base']:.2f} + (c - {b['mean_raw']:.2f}) "
                 f"(by horizon/vehicle once n>=10). A call is placed when p >= {(t.get('learning') or {}).get('min_p')}. "
                 + size_txt)
        return "\n".join(L)
    except Exception as e:
        return f"(record unavailable: {e})"


def _book_without_parked(positions) -> tuple:
    """(positions with the idle-cash rule's parked shares taken out, dollars parked, sentence about them | None).
    With nothing parked the list comes back as it is, with 0.0 and None. The sentence is given only when
    idle_cash.show_in_book is true (wording approved by Nico); never raises."""
    try:
        pk = _parked()
        if not pk:
            return positions, 0.0, None
        out, usd, funds = [], 0.0, []
        for p_ in positions:
            sym = str(p_.get("symbol") or "").upper()
            q = _f(p_.get("qty")) or 0.0
            mine = min(q, float(pk.get(sym, 0.0))) if (q > 0 and str(p_.get("side") or "long").lower() == "long") else 0.0
            if mine <= 1e-9:
                out.append(p_)
                continue
            px = _f(p_.get("current_price")) or ((abs(_f(p_.get("market_value")) or 0.0) / q) if q else 0.0)
            usd += mine * px
            funds.append(sym)
            rest = q - mine
            if rest > 1e-9:                                  # someone else holds the same fund too: show only that part
                share = rest / q
                out.append(dict(p_, qty=str(rest), market_value=str((_f(p_.get("market_value")) or 0.0) * share),
                                unrealized_pl=str((_f(p_.get("unrealized_pl")) or 0.0) * share)))
        words = None
        if usd > 0:
            try:
                import mason_idle_cash as IC
                ic = IC.cfg()
                if not IC.live(ic):          # (review fix) the money is freed for a trade by itself only while the rule
                    return out, 0.0, None    # runs for real orders; otherwise the cash shown stays the broker's own
                words = IC.book_words(usd, funds) if ic.get("show_in_book") is True else None
            except Exception:
                words = None
        return out, usd, words
    except Exception:
        return positions, 0.0, None


def book_text() -> tuple:
    """(text, symbols) — the whole Alpaca paper book, each position tagged by who opened it."""
    try:
        AT = _AT()
        acct, pos = AT._account(), AT._positions()
        if not (acct.get("ok") and pos.get("ok")):
            return f"(book unavailable: {acct.get('error') or pos.get('error')})", set()
        cfg = _cfg()
        mine = {}
        for r in _q("SELECT rec_id, ticker, occ, qty, tier, stop, target, time_stop_date, horizon, features_json "
                    "FROM agent_trades "
                    "WHERE source='mason' AND status='open' AND COALESCE(record_deleted,0)=0"):
            mine[(r["occ"] or r["ticker"] or "").upper()] = r
        eq = acct.get("equity") or 0.0
        sleeve = AT._sleeve_exposure(pos)
        n_today = len(AT._trades_today(_now_et().date().isoformat()))
        # Idle-cash rule: shares it parked in the index fund are spare cash, not a position of Nico's or of the
        # director's. They are left out of the list below and their value is shown with the cash (the words are
        # the same as before; idle_cash.show_in_book adds one approved sentence instead). Nothing parked = no change.
        positions, park_usd, park_words = _book_without_parked(pos.get("positions") or [])
        cash_shown = acct.get('cash') or 0
        if park_usd and not park_words:
            cash_shown += park_usd
        L = [f"equity ${eq:,.0f} · cash ${cash_shown:,.0f} · day P&L {acct.get('day_pnl_pct') or 0:+.2f}% · "
             f"Mason sleeve ${sleeve:,.0f} ({(sleeve / eq * 100) if eq else 0:.1f}% of {cfg.get('max_open_exposure_pct')}% cap) · "
             f"trades today {n_today}/{cfg.get('max_trades_per_day')}"]
        if park_words:
            L.append(park_words)
        mix = horizon_mix(cfg)
        if mix.get("actual_pct"):
            L.append("Mason open by horizon: " + " / ".join(f"{k} {v:.0f}%" for k, v in mix["actual_pct"].items())
                     + " (target " + " / ".join(f"{k} {v}%" for k, v in mix["target"].items()) + ")")
        syms = set()
        for p_ in sorted(positions, key=lambda x: -abs(_f(x.get("market_value")) or 0)):
            sym = str(p_.get("symbol") or "").upper()
            is_opt = str(p_.get("asset_class") or "").lower().startswith("us_option") or len(sym) > 12
            under = re.match(r"^[A-Z]+", sym).group(0) if is_opt and re.match(r"^[A-Z]+", sym) else sym
            syms.add(under)
            m = mine.get(sym)
            if m:
                lv = _sap_book_words(m, cfg)           # None unless Nico switched that wording on (short_as_put.tell_director)
                tag = (f"[Mason rec {m['rec_id']} · {m['tier']} · {m['horizon']} · "
                       + (lv or f"stop {m['stop']} / target {m['target']}")
                       + f" · time stop {m['time_stop_date']}" + (f" · {m['qty']:g} of {abs(_f(p_.get('qty')) or 0):g} are Mason's"
                                                                if abs((_f(p_.get('qty')) or 0) - (m['qty'] or 0)) > 1e-9 else "") + "]")
            elif is_opt:
                tag = "[Nico's option — never touched by Mason]"
            else:
                tag = "[Nico placed]"
            plpc = _f(p_.get("unrealized_plpc"))
            L.append(f"- {sym} qty {_f(p_.get('qty')):g} @ {_f(p_.get('avg_entry_price'))} → "
                     f"{_f(p_.get('current_price'))} ({(plpc or 0) * 100:+.1f}%, ${_f(p_.get('unrealized_pl')) or 0:+,.0f}) {tag}")
        if len(L) == 1:
            L.append("- no open positions")
        return "\n".join(L), syms
    except Exception as e:
        return f"(book unavailable: {e})", set()


def zero_dte_chain(n_strikes: int = 4) -> dict:
    """{underlying: {"spot": x, "rows": [chain rows expiring TODAY]}} for the configured 0DTE
    underlyings. Only while the market is open and new 0DTE entries are still allowed. Rows use
    the dossier chain_sample keys so the director's contract validator accepts them."""
    out = {}
    try:
        cfg = _cfg()
        if not cfg.get("allow_0dte"):
            return out
        AT = _AT()
        clk = AT._clock()
        now = _now_et()
        if not (clk.get("ok") and clk.get("is_open")):
            return out
        cut, _txt = AT.close_cutoff(cfg.get("zero_dte_last_entry_et"), "14:30", clk, now, cfg)   # X3
        if now.replace(second=0, microsecond=0) > cut:
            return out
        today = now.date().isoformat()
        for u in [str(x).upper() for x in (cfg.get("zero_dte_underlyings") or [])]:
            q = AT._stock_quote(u)
            spot = q.get("last") or q.get("ask") or q.get("bid")
            if not spot:
                continue
            code, d = AT._get("/options/contracts", params={
                "underlying_symbols": u, "expiration_date": today, "status": "active", "limit": 500,
                "strike_price_gte": round(spot * 0.985, 2), "strike_price_lte": round(spot * 1.015, 2)})
            cons = (d.get("option_contracts") or []) if (code == 200 and isinstance(d, dict)) else []
            pick = []
            for typ in ("call", "put"):
                c = [x for x in cons if (x.get("type") or "").lower() == typ and x.get("tradable", True)]
                c.sort(key=lambda x: abs((_f(x.get("strike_price")) or 0) - spot))
                pick += c[:n_strikes]
            if not pick:
                continue
            code, d = AT._data_get(f"{AT._DATA_BASE}/v1beta1/options/snapshots",
                                   {"symbols": ",".join(x["symbol"] for x in pick)})
            snaps = (d.get("snapshots") or {}) if (code == 200 and isinstance(d, dict)) else {}
            rows = []
            for x in pick:
                sn = snaps.get(x["symbol"]) or {}
                qq = sn.get("latestQuote") or {}
                bid, ask = _f(qq.get("bp")), _f(qq.get("ap"))
                last = _f((sn.get("latestTrade") or {}).get("p"))
                if not (bid and ask):
                    continue
                iv = _f(sn.get("impliedVolatility"))
                rows.append({"expiry": today, "dte": 0, "type": (x.get("type") or "").lower(),
                             "strike": _f(x.get("strike_price")), "bid": bid, "ask": ask, "last": last,
                             "mark": round((bid + ask) / 2, 2), "mark_src": "quote",
                             "iv": round(iv * 100, 1) if iv else None,
                             "oi": _f(x.get("open_interest")), "volume": _f((sn.get("dailyBar") or {}).get("v"))})
            if rows:
                rows.sort(key=lambda r: (r["type"], r["strike"]))
                out[u] = {"spot": spot, "rows": rows}
    except Exception as e:
        out["_error"] = f"zero_dte_chain: {e}"
    return out


def zero_dte_text(chain: dict) -> str:
    L = []
    for u, v in chain.items():
        if u.startswith("_"):
            continue
        L.append(f"{u} spot {v['spot']} — expiry {v['rows'][0]['expiry']} (TODAY)")
        for r in v["rows"]:
            L.append(f"  {r['type']} {r['strike']:g}: bid {r['bid']} / ask {r['ask']}"
                     + (f" · iv {r['iv']}%" if r.get("iv") else "") + (f" · vol {r['volume']:g}" if r.get("volume") else ""))
    return "\n".join(L)


def snapshot_calibration(now_et=None) -> dict:
    """Once per trading day after the close: write the numbers the loop learned to
    agent_calibration (global, per segment, Mason's own closed trades) for the record/UI."""
    try:
        now = now_et or _now_et()
        if now.weekday() >= 5 or now.date().isoformat() in _holidays() or (now.hour, now.minute) < (16, 10):
            return {"skipped": "before the close"}
        day = now.date().isoformat()
        if _q("SELECT 1 FROM agent_calibration WHERE substr(computed_at,1,10)=? LIMIT 1", (day,)):
            return {"skipped": "done today"}
        b = base_rate()
        rows = [("global", "director_calls", b["n"], b["wins"], b["p_base"], None, 1.0)]
        for (h, v), st in sorted(segment_rates().items()):
            c = calibrate(b["mean_raw"], h, v)
            rows.append((f"{h}|{v}", "director_calls", st["n"], st["wins"], c.get("p_cal"), None, 1.0))
        own = _q("SELECT horizon, vehicle, win, r_multiple FROM agent_trades WHERE source='mason' "
                 "AND status='closed' AND win IS NOT NULL AND COALESCE(record_deleted,0)=0")
        agg = {}
        for r in own:
            d = agg.setdefault(f"{r['horizon']}|{r['vehicle']}", {"n": 0, "w": 0, "r": []})
            d["n"] += 1; d["w"] += r["win"]
            if r["r_multiple"] is not None:
                d["r"].append(r["r_multiple"])
        for k, d in agg.items():
            rows.append((k, "mason_trades", d["n"], d["w"], (d["w"] + 10 * 0.5) / (d["n"] + 10),
                         (sum(d["r"]) / len(d["r"])) if d["r"] else None, 1.0))
        conn = _conn()
        try:
            with conn:
                conn.executemany("INSERT INTO agent_calibration(segment,source,n,wins,p_cal,expectancy_r,sizing_mult,"
                                 "computed_at) VALUES (?,?,?,?,?,?,?,?)", [r + (now.isoformat(timespec="seconds"),) for r in rows])
        finally:
            conn.close()
        return {"written": len(rows)}
    except Exception as e:
        return {"error": f"snapshot_calibration: {e}"}


def horizon_mix(cfg=None) -> dict:
    """Target vs actual split of Mason's open capital: swing / intraday (0-5DTE) / long (30d+)."""
    cfg = cfg or _cfg()
    target = dict(cfg.get("horizon_mix") or {"swing": 40, "intraday": 30, "long": 30})
    out = {"target": target, "actual_pct": {}}
    try:
        tot = {k: 0.0 for k in target}
        for r in _q("SELECT horizon, cost_usd FROM agent_trades WHERE source='mason' AND status='open' "
                    "AND COALESCE(record_deleted,0)=0"):
            k = {"0-5DTE": "intraday", "swing": "swing"}.get(str(r["horizon"] or ""), "long")
            tot[k] = tot.get(k, 0.0) + float(r["cost_usd"] or 0)
        s_ = sum(tot.values())
        if s_ > 0:
            out["actual_pct"] = {k: v / s_ * 100.0 for k, v in tot.items()}
    except Exception:
        pass
    return out


# ── excursions: how far each open trade ran for / against us ─────────────────────

def track_excursions() -> dict:
    """Once per tick while the market is open: update mfe_pct / mae_pct of every open row
    (Mason's and Nico's) from ONE positions call. 3-minute resolution; read-only at the broker."""
    out = {"updated": 0}
    try:
        AT = _AT()
        clk = _clock()
        if not (clk.get("ok") and clk.get("is_open")):
            return {"skipped": "market closed"}
        pos = AT._positions()
        if not pos.get("ok"):
            return {"error": str(pos.get("error"))[:120]}
        px = {str(p_.get("symbol") or "").upper(): _f(p_.get("current_price")) for p_ in pos.get("positions") or []}
        for r in _q("SELECT id, ticker, occ, entry_px, mfe_pct, mae_pct FROM agent_trades WHERE status='open' "
                    "AND COALESCE(record_deleted,0)=0"):
            last, entry = px.get((r["occ"] or r["ticker"] or "").upper()), r["entry_px"]
            if not last or not entry:
                continue
            pct = (last - entry) / entry * 100.0
            mfe = pct if r["mfe_pct"] is None else max(r["mfe_pct"], pct)
            mae = pct if r["mae_pct"] is None else min(r["mae_pct"], pct)
            if mfe != r["mfe_pct"] or mae != r["mae_pct"]:
                _update_trade(r["id"], {"mfe_pct": mfe, "mae_pct": mae})
                out["updated"] += 1
    except Exception as e:
        out["error"] = f"track_excursions: {e}"
    return out


# ── book actions: the director may sell STOCK positions Nico placed ──────────────

def _mason_open_qty(ticker):
    """Shares of `ticker` that belong to the director's own calls right now, or None when that
    cannot be established (then nothing of Nico's is sold this tick). Counts EVERY live director
    rec on the symbol — placing, resting/part-filled entries and placed — from the entry order's
    real filled_qty, minus exit fills already booked; a spine row is trusted only once its entry
    order is final."""
    sym = str(ticker).upper()
    rows = {r["rec_id"]: r for r in _q(
        "SELECT * FROM agent_trades WHERE source='mason' AND occ IS NULL AND UPPER(ticker)=? "
        "AND COALESCE(record_deleted,0)=0", (sym,))}
    total = 0.0
    for rec in _q("SELECT id, extra_json FROM recommendations WHERE kind='director' AND "
                  "json_extract(extra_json,'$.execution.state') IN ('placing','approved_pending_fill','placed') "
                  "AND UPPER(json_extract(extra_json,'$.execution.symbol'))=?", (sym,)):
        e = (json.loads(rec["extra_json"] or "{}").get("execution") or {})
        if e.get("asset") == "option":
            continue
        row = rows.get(rec["id"])
        if row and row.get("status") != "open":
            continue
        if row and (_feat(row).get("entry_final") or not e.get("order_id")):
            total += _remaining(row)
            continue
        if e.get("state") == "placing" or not e.get("order_id"):
            return None
        g = _get_order(e["order_id"])
        if not g.get("ok"):
            return None
        booked = sum(float(x.get("qty") or 0) for x in _parts(row)) if row else 0.0
        total += max(0.0, (_f(g["order"].get("filled_qty")) or 0.0) - booked)
    pk = _parked()                 # idle-cash rule: shares parked in this symbol are Mason's too (0 when none)
    if pk is None:
        return None                # the ledger could not be read: nothing of Nico's is sold or protected this tick
    return total + float(pk.get(sym, 0.0))


def book_sells_today(day_iso) -> int:
    """Director book sells that count toward max_trades_per_day (submitted or done today)."""
    try:
        return int(_q("SELECT COUNT(*) n FROM agent_book_actions WHERE day=? AND state IN ('working','done')",
                      (day_iso,))[0]["n"])
    except Exception:
        return 0


def apply_book_actions(items, slate_id="") -> list:
    """Record the director's "book" sells (stock positions Nico placed; never options, never
    Mason's own rows — those close via updates by rec_id). Executed by process_book_actions()."""
    out = []
    try:
        cfg = _cfg()
        ba = cfg.get("book_actions") or {}
        if not ba.get("enabled", True) or not isinstance(items, list):
            return out
        ensure_schema()
        day = _now_et().date().isoformat()
        seen = {r["symbol"] for r in _q("SELECT symbol FROM agent_book_actions WHERE day=? AND state IN "
                                        "('pending','working')", (day,))}
        for it in items:
            if len([o for o in out if o.get("accepted")]) >= int(ba.get("max_per_call", 3)):
                out.append({"symbol": str((it or {}).get("symbol"))[:12], "accepted": False,
                            "why": "over max_per_call"})
                continue
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").upper().strip()
            act = str(it.get("action") or "").lower()
            raw_q = it.get("qty")
            qty = _f(raw_q)
            why = None
            if raw_q is not None and qty is None:
                why = f"qty {str(raw_q)[:20]!r} is not a number (use null for all, or a share count)"
            elif not _STOCK_SYM.match(sym):
                why = "not a stock symbol (options are never sold from the book)"
            elif act not in ("sell", "close", "trim"):
                why = f"action {act!r} not supported (sell only)"
            elif qty is not None and qty <= 0:
                why = "qty must be positive or null"
            elif sym in seen:
                why = "already pending today"
            if why:
                out.append({"symbol": sym, "accepted": False, "why": why})
                continue
            bid = uuid.uuid4().hex[:10]
            conn = _conn()
            try:
                with conn:
                    conn.execute("INSERT INTO agent_book_actions(id,slate_id,symbol,qty,reason,state,created_at,day) "
                                 "VALUES (?,?,?,?,?,?,?,?)",
                                 (bid, slate_id, sym, qty, str(it.get("reason") or "")[:300], "pending", _nowiso(), day))
            finally:
                conn.close()
            seen.add(sym)
            out.append({"id": bid, "symbol": sym, "qty": qty, "accepted": True})
    except Exception as e:
        out.append({"accepted": False, "why": f"apply_book_actions: {e}"})
    return out


def _book_set(bid, **kv):
    cols = ", ".join(f"{k}=?" for k in kv)
    conn = _conn()
    try:
        with conn:
            conn.execute(f"UPDATE agent_book_actions SET {cols} WHERE id=?", list(kv.values()) + [bid])
    finally:
        conn.close()


def _book_end(b, state, note):
    _book_set(b["id"], state=state, note=str(note)[:300])
    fq, pr = float(b.get("filled_qty") or 0), float(b.get("proceeds") or 0)
    if state == "done":
        avg = (pr / fq) if (fq > 0 and pr > 0) else None
        title = (f"Sold (paper, director): {fq:g} {b['symbol']} @ {('%.2f' % avg) if avg else 'n/a'} — "
                 f"a position Nico placed — {b.get('reason') or 'no reason given'}")
        sev = "info"
        _close_nico_rows(b["symbol"], fq, avg, b.get("order_id"))
    else:
        title = f"Director sell of {b['symbol']} not done ({state}): {note}"
        sev = "warning"
    _notify(title, title, ticker=b["symbol"], severity=sev, dedup=f"agent_book:{b['id']}:{state}")
    _activity("agent_book_" + state, title, ticker=b["symbol"], rec_id=None,
              extra={"book_action": b["id"], "filled_qty": fq, "note": note})
    return {"id": b["id"], "symbol": b["symbol"], "state": state, "filled_qty": fq, "note": str(note)[:160]}


def _close_nico_rows(symbol, qty, px, order_id):
    """Mason sold shares Nico had placed: mark his "Nico's thesis" rows FIFO so they are graded."""
    try:
        left = float(qty)
        for r in _q("SELECT * FROM agent_trades WHERE source='nico' AND status='open' AND occ IS NULL "
                    "AND UPPER(ticker)=? AND COALESCE(record_deleted,0)=0 ORDER BY entry_ts", (symbol.upper(),)):
            if left <= 1e-9:
                break
            take = min(left, _remaining(r))
            if take <= 0:
                continue
            parts = _parts(r) + [{"qty": take, "px": (float(px) if px else None), "order_id": order_id,
                                  "at": _nowiso()}]
            r = _set_feat(r, partial_exits=parts)
            left -= take
            if _remaining(r) <= 1e-9:
                entry, q = r.get("entry_px"), float(r.get("qty") or 0)
                known = all(x.get("px") for x in parts)
                avg = (sum(x["qty"] * x["px"] for x in parts) / sum(x["qty"] for x in parts)) if known else None
                f = {"exit_ts": _nowiso(), "exit_px": avg, "exit_reason": "mason_sold", "status": "closed",
                     "graded_at": _nowiso()}
                if entry and avg is not None:
                    f.update({"pnl_usd": (avg - entry) * q, "pnl_pct": (avg - entry) / entry * 100.0,
                              "win": 1 if avg > entry else 0})
                _update_trade(r["id"], f)
    except Exception:
        pass


def process_book_actions(now_et=None) -> list:
    out = []
    try:
        rows = _q("SELECT * FROM agent_book_actions WHERE state IN ('pending','working') ORDER BY created_at")
        if not rows:
            return out
        AT = _AT()
        cfg = _cfg()
        now = now_et or _now_et()
        today = now.date().isoformat()
        clk = _clock()
        is_open = bool(clk.get("ok") and clk.get("is_open"))
        open_at = now.replace(hour=9, minute=30, second=0, microsecond=0) + \
            timedelta(minutes=float(cfg.get("entry_open_delay_min", 5)))
        for b in rows:
            try:
                sym = b["symbol"]
                stale = b["day"] != today
                if b.get("order_id"):                       # one of our sells is out (checked FIRST, any day/hour)
                    g = _get_order(b["order_id"])
                    if not g.get("ok"):
                        out.append({"id": b["id"], "state": "working", "note": "order lookup failed"})
                        continue
                    o = g["order"]
                    st = (o.get("status") or "").lower()
                    if st == "filled" or st in _DEAD:
                        fq = _f(o.get("filled_qty")) or 0.0
                        px = _f(o.get("filled_avg_price")) or _f(o.get("limit_price")) or 0.0
                        b = dict(b, filled_qty=(b["filled_qty"] or 0) + fq, proceeds=(b["proceeds"] or 0) + fq * px,
                                 order_id=None)
                        _book_set(b["id"], filled_qty=b["filled_qty"], proceeds=b["proceeds"], order_id=None)
                        if b["qty"] is not None and b["filled_qty"] >= float(b["qty"]) - 1e-9:
                            out.append(_book_end(dict(b, order_id=o.get("id")), "done", "filled"))
                            continue
                    else:
                        try:
                            age = (datetime.now() - datetime.fromisoformat(str(b.get("submitted_at")))).total_seconds()
                        except Exception:
                            age = _REPRICE_AFTER_S + 1
                        if stale or age > _REPRICE_AFTER_S:   # never leave yesterday's sell on the book
                            _cancel_order(b["order_id"])
                        out.append({"id": b["id"], "symbol": sym, "state": "working", "order_status": st})
                        continue
                if stale:
                    out.append(_book_end(b, "done" if (b["filled_qty"] or 0) > 0 else "expired",
                                         "not completed on the day it was decided"))
                    continue
                if not is_open or now < open_at:
                    out.append({"id": b["id"], "symbol": b["symbol"], "state": b["state"], "note": "waiting for the open"})
                    continue
                pos = _get_position(sym)
                if not pos.get("ok"):
                    out.append({"id": b["id"], "state": b["state"], "note": "position lookup failed"})
                    continue
                if not pos.get("exists"):
                    out.append(_book_end(b, "done" if (b["filled_qty"] or 0) > 0 else "refused", "no position in " + sym))
                    continue
                cls = str(pos.get("asset_class") or "us_equity").lower()
                if cls != "us_equity" or (pos.get("side") or "long") != "long" or not _STOCK_SYM.match(sym):
                    out.append(_book_end(b, "refused", f"not a long stock position (class {cls}, side {pos.get('side')})"))
                    continue
                mq = _mason_open_qty(sym)
                if mq is None:                              # an entry of ours is in flight: wait, never guess
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"],
                                "note": "waiting: Mason's own quantity in this symbol is not settled yet"})
                    continue
                if _q("SELECT 1 FROM agent_trades WHERE source='mason' AND status='open' AND occ IS NULL AND "
                      "UPPER(ticker)=? AND exit_pending_reason IS NOT NULL LIMIT 1", (sym,)):
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"],
                                "note": "waiting: Mason's own exit on this symbol goes first"})
                    continue
                # mason_protect may have a protective stop resting on exactly these shares (the broker allows
                # one sell order per share): it is taken off first and this sale waits one tick for the release.
                rel = _opt("mason_protect", "release_for_sale", sym, f"the director's sale {b['id']}")
                if isinstance(rel, dict) and (rel.get("wait") or rel.get("error")):
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"],
                                "note": "waiting: Mason's protective stop on this symbol is being taken off first"})
                    continue
                if isinstance(rel, dict) and rel.get("released"):
                    pos = _get_position(sym)                     # the shares are free again: read them fresh
                    if not pos.get("ok") or not pos.get("exists"):
                        out.append({"id": b["id"], "state": b["state"], "note": "position lookup failed"})
                        continue
                nico_qty = float(pos.get("qty") or 0) - mq       # Mason's own shares are not sold here
                want = nico_qty if b["qty"] is None else float(b["qty"]) - float(b["filled_qty"] or 0)
                q = min(want, nico_qty, float(pos.get("qty_available") or 0))
                if q <= 1e-9:
                    out.append(_book_end(b, "done" if (b["filled_qty"] or 0) > 0 else "refused",
                                         "nothing of Nico's left to sell (Mason's own shares close via updates)"))
                    continue
                n_today = len(AT._trades_today(today)) + book_sells_today(today)
                if b["state"] == "pending" and n_today >= int(cfg.get("max_trades_per_day") or 0):
                    out.append(_book_end(b, "refused", f"max trades/day reached ({n_today})"))
                    continue
                n = int(b.get("attempts") or 0) + 1
                qt = _quote(sym, False, cfg, relaxed=True)      # the director decided this sale already
                limit, _basis = _sell_limit(qt, n, False, cfg)  # X2: last − band, never a far-away bid
                if not limit:
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"], "note": "no quote"})
                    continue
                res = _close_position(sym, q, limit, pitch_id=f"book-{b['id']}-{n}")
                if res.get("existing_order_id"):
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"],
                                "note": "another sell order is resting on this symbol"})
                    continue
                if not res.get("ok"):
                    out.append({"id": b["id"], "symbol": sym, "state": b["state"], "note": str(res.get("error"))[:160]})
                    _book_set(b["id"], attempts=n, note=str(res.get("error"))[:300])
                    continue
                _book_set(b["id"], state="working", order_id=res.get("order_id"), attempts=n,
                          submitted_at=datetime.now().isoformat(timespec="seconds"))
                out.append({"id": b["id"], "symbol": sym, "state": "working", "qty": q, "order_id": res.get("order_id")})
            except Exception as e:
                out.append({"id": b.get("id"), "error": f"{type(e).__name__}: {e}"})
    except Exception as e:
        out.append({"error": f"process_book_actions: {e}"})
    return out


def _opt(module, fn, *a):
    """Call an optional companion module (Nico's theses, reports); absent or failing = no-op."""
    try:
        m = __import__(module)
        return getattr(m, fn)(*a)
    except ImportError:
        return {"skipped": f"{module} not installed"}
    except Exception as e:
        return {"error": f"{module}.{fn}: {type(e).__name__}: {e}"}


# ── tick / status ────────────────────────────────────────────────────────────────

def tick(now_et=None) -> dict:
    out = {}
    try:
        cfg = _cfg()
        ex_on = bool((cfg.get("exits") or {}).get("enabled"))
        if not (cfg.get("tiers_enabled") or cfg.get("queue_premarket") or ex_on):
            return {"skipped": "agent loop off"}
        out["schema"] = ensure_schema()
        steps = [("pending_closes", cancel_closed_pending),
                 ("settle", lambda: _AT().settle_pending()),      # fills reach the spine within one tick
                 ("queue", lambda: drain_queue(now_et)),          # no-op when nothing is queued
                 ("spine", sync_spine)]
        steps.append(("exits", lambda: manage_exits(now_et)))
        steps.append(("book", lambda: process_book_actions(now_et)))
        try:                                             # idle-cash rule: the step exists only while it is enabled
            if (_AT()._cfg().get("idle_cash") or {}).get("enabled") is True:
                steps.append(("idle_cash", lambda: _opt("mason_idle_cash", "tick", now_et)))
        except Exception:
            pass
        try:                                             # option order handling: its steps exist only while it is on
            if _oo_on(cfg) or _OO().tick_needed(cfg):        # or a schedule is still open; the schedules run before
                steps.insert(1, ("option_orders", lambda: _opt("mason_option_orders", "step", now_et)))   # `settle`
                if _oo_on(cfg):                          # (review) the record-only followers read prices: after the
                    at = [n for n, _x in steps].index("exits") + 1          # exit checks, never ahead of them
                    steps.insert(at, ("option_orders_follow", lambda: _opt("mason_option_orders", "follow", now_et)))
        except Exception:
            pass
        steps.append(("excursions", track_excursions))
        steps.append(("calibration", lambda: snapshot_calibration(now_et)))
        steps.append(("nico", lambda: _opt("mason_nico_thesis", "sync", now_et)))
        steps.append(("report", lambda: _opt("mason_agent_report", "tick", now_et)))
        for name, fn in steps:
            try:
                out[name] = fn()
            except Exception as e:
                out[name] = {"error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        out["error"] = f"tick: {e}"
    _note_step_errors(out)                               # (gap fixes) errors are written down; `out` is not changed
    return out


# ── errors of the steps (gap fixes, 2026-11-18) ──────────────────────────────────
# The desk worker throws this module's result away, so an error returned by a step was heard by nobody. Every look,
# the errors in the result are written to Mason's log (the same error of the same step at most once an hour) and
# kept in memory for the health check (mason_selfcheck, selfcheck.agent_loop_errors). Nothing else changes: the
# result and the order of the steps are exactly as before.
_STEP_ERRORS = {}          # step -> {"first_at", "last_at", "count", "error", "rec_id"}
_STEP_WARNED = {}          # (step, error text) -> monotonic time of its last line in the log
_STEP_ERROR_KEEP_S = 6 * 3600


def _find_errors(v, depth=0) -> list:
    """(error text, rec id|None) for every error inside one step's result. Never raises."""
    found = []
    try:
        if isinstance(v, dict):
            if v.get("error"):
                found.append((str(v.get("error"))[:300], v.get("rec_id")))
            if depth < 3:
                for k, x in v.items():
                    if k != "error" and isinstance(x, (dict, list)):
                        found.extend(_find_errors(x, depth + 1))
        elif isinstance(v, list) and depth < 3:
            for x in v:
                if isinstance(x, (dict, list)):
                    found.extend(_find_errors(x, depth + 1))
    except Exception:
        pass
    return found


def _note_step_errors(out) -> None:
    """Write the errors of this look's steps to the log and remember them. Never raises, changes nothing in `out`."""
    try:
        import logging
        lg = logging.getLogger("mason.agent_loop")
        mono, now = time.monotonic(), _nowiso()
        items = [(k, v) for k, v in (out or {}).items() if k != "error"]
        if (out or {}).get("error"):
            items.append(("tick", {"error": out.get("error")}))
        for name, val in items:
            for text, rid in _find_errors(val):
                rec = _STEP_ERRORS.get(name)
                if rec is None or rec.get("error") != text:
                    rec = {"first_at": now, "count": 0, "error": text}
                rec.update(last_at=now, last_s=time.time(), count=int(rec.get("count") or 0) + 1, rec_id=rid)
                _STEP_ERRORS[name] = rec
                key = (name, text[:120])
                if mono - _STEP_WARNED.get(key, -1e12) >= 3600.0:
                    _STEP_WARNED[key] = mono
                    lg.warning("[agent_loop] step '%s' returned an error%s: %s", name,
                               f" (director rec {rid})" if rid else "", text)
        if len(_STEP_WARNED) > 500:
            _STEP_WARNED.clear()
    except Exception:
        pass


def step_errors(max_age_s=None) -> list:
    """The steps whose last error is at most `max_age_s` seconds old (default 6 hours), newest first:
    [{"step", "error", "count", "first_at", "last_at", "rec_id"}]. In memory only (empty after a restart)."""
    try:
        keep = float(_STEP_ERROR_KEEP_S if max_age_s is None else max_age_s)
        now = time.time()
        rows = [dict(v, step=k) for k, v in _STEP_ERRORS.items() if now - float(v.get("last_s") or 0) <= keep]
        for r in rows:
            r.pop("last_s", None)
        return sorted(rows, key=lambda r: str(r.get("last_at")), reverse=True)
    except Exception:
        return []


def status() -> dict:
    try:
        ensure_schema()
        rows = _q("SELECT status, tier, horizon, pnl_usd FROM agent_trades "
                  "WHERE source='mason' AND COALESCE(record_deleted,0)=0")
        by_tier, by_hz = {}, {}
        for r in rows:
            if r["pnl_usd"] is not None:
                by_tier[r["tier"] or "?"] = by_tier.get(r["tier"] or "?", 0.0) + r["pnl_usd"]
                by_hz[r["horizon"] or "?"] = by_hz.get(r["horizon"] or "?", 0.0) + r["pnl_usd"]
        try:
            quotes = _AT()._MQ().status()
        except Exception:
            quotes = None
        return {"ok": True, "base_rate": base_rate(), "own_graded_n": own_graded_n(),
                "open": sum(1 for r in rows if r["status"] == "open"),
                "closed": sum(1 for r in rows if r["status"] == "closed"),
                "pnl_by_tier": by_tier, "pnl_by_horizon": by_hz, "queued": _queued_ids(),
                "quotes": quotes}
    except Exception as e:
        return {"ok": False, "error": f"status: {e}"}
