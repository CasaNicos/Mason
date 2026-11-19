"""
mason_option_orders.py — OPTION ORDER HANDLING (audit X13, L10, L7; 2026-11-17; PAPER ONLY; no model call).

Why
---
An option purchase used to be sent once, as a day limit order, and then rested all day (at the director's
price, or at the middle price when the director named none); a sale went straight to the bid; the only
liquidity check was a 25% spread; and nothing recorded what the real prices were when Mason traded.

What this module adds (each part has its own switch; with everything off nothing here runs)
-------------------------------------------------------------------------------------------
  A. ENTRIES THAT WORK A PRICE   The first order goes out at the middle of the real buy and sell prices and is
                                 raised in a few fixed steps, one per look (Mason looks every 3 minutes), up to a
                                 hard limit that is fixed when the order is approved and never moves afterwards.
                                 The order is sized and checked against that limit, so no step can push the cost
                                 past anything that applied at approval. (work_entry / tick)
  B. EXITS THAT WORK A PRICE     A sale starts above the bid and steps down to it. An exit that must happen (day
                                 before expiry, same-day cut-off, director close, time stop) is at the bid by a
                                 fixed time and from then on is priced exactly as before. (exit_hook / exit_step)
  C. FILTERS AT ENTRY            Two-sided real quote, spread, open interest, quote age, delta: worked out from
                                 Robinhood's quote (Alpaca's when Robinhood cannot be read) and recorded on the
                                 call; each rule refuses an order only when it is switched on. (entry_filters)
  D. RECORDS                     The bid, ask, middle price, their source and age and the limit sent, at every
                                 order, and the fill next to them: on the call (execution.features.option_entry,
                                 which the trade row copies) and on the trade (features_json.exit_orders,
                                 .option_cost). (placed / filled / exit_sent / exit_fill)
  +  STUCK PUT                   A put bought for a short call whose STOCK is through the call's level but which
                                 has no price it can be sold at: after a while a wider, still real quote is
                                 accepted. (stuck_put)
  +  "PLACING" RECOVERY          A call left in state 'placing' by a crash: the order is looked for at the broker
                                 by its client order id and adopted, or the claim is released. (recover_placing)

Safety rules built in
---------------------
  * Single-leg LONG options only: the only orders this module ever sends are a BUY-to-open of the call's own
    contract (through mason_execution.place_trade) and nothing else; sales are sent by the exit manager's
    own, unchanged close_position call. It cancels only order ids it finds on Mason's own call or trade row.
  * Entries: only calls placed by the automatic path (approver 'auto'). Exits: only Mason's own rows in
    agent_trades (the exit manager never looks at any other row).
  * One call never has two entry orders with a fill: once an order has any fill, re-pricing stops; the rest of
    that order keeps its price until the schedule ends and is then cancelled. So the journal, the reconcile,
    the self-check and the protection code keep seeing one entry order per call, as before.
  * A cancel counts only when the broker's own record of the order says it is finished. What filled meanwhile
    is read from that record. A new order is written down (step, quantity, price, client order id) BEFORE it is
    sent, so a lost answer or a restart finds the same order again instead of sending a second one.
  * No sleep longer than `confirm_wait_s` (2 s) inside the desk worker: the schedule is driven by the looks.
  * Nothing here raises into its callers: every hook falls back to the behaviour before this change.

Config (mason.config.json -> autotrade.option_orders; see DEFAULTS for every number)
----------------------------------------------------------------------------------
  enabled false   master switch. Off = no order is priced, sent, changed or cancelled differently.
  shadow  true    record-only: what the schedule and the filters WOULD have done is recorded; nothing else.
  record  true    keep the real prices next to every order and fill (only while enabled or shadow is on).
  entry.enabled / exit.enabled / filters.<rule>.enabled / stuck_put.enabled / placing_recovery.enabled
                  each false; a part is live only when `enabled` AND its own switch are true.
With enabled false AND shadow false nothing in this module runs and nothing is written.
"""

import json
import logging
import math
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("mason.option_orders")

MODULE = "mason_option_orders"
VERSION = 1

DEFAULTS = {
    "enabled": False,                     # master switch
    "shadow": True,                       # record what each part would have done while it is off
    "record": True,                       # real prices next to every order and fill (audit L10)
    "entry": {
        "enabled": False,
        "rungs": [0.0, 0.34, 0.67, 1.0],  # share of the way from the first price (the middle) to the limit
        "min_rest_s": 120,                # an order rests at least this long before it is re-priced
        "rest_at_limit_s": 170,           # how long the last order rests at the limit before the end rule
        "max_run_s": 1800,                # whatever happens, the schedule ends this long after the first order
        "at_limit_director_price": "rest",   # the director named the price: "rest" there until the close | "cancel"
        "at_limit_own_price": "cancel",      # Mason set the limit itself (no director price; a short call as a put)
        "confirm_wait_s": 2.0,            # longest wait inside one look for the broker to confirm a cancel
        "fill_wait_s": 1.0,               # ... and for a new order to report a fill
        "cancel_retry_s": 150,            # a cancel the broker has not confirmed is sent again after this long
        "max_send_tries": 2,              # a re-priced order the broker does not take is tried this often
    },
    "exit": {
        "enabled": False,
        "target_rungs": [1.0, 0.5, 0.0],  # share of the way from the bid UP to the middle: 1 = middle, 0 = bid
        "stop_rungs": [0.0],              # (review) a stop goes out at the bid, as fast as before; [0.5, 0.0] = halfway
                                          # first, one look (about 3 minutes) later at the bid
        "must_rungs": [1.0, 0.5, 0.0],    # exits that must happen
        "min_rest_s": 120,
        "step_s": 180,                    # the time one rung is given when a deadline is counted back
        "must_bid_before_close_min": 30,  # a must-do exit is at the bid from this long before the real close
        "flat_by_lead_s": 360,            # same-day cut-off: start this long before it (0 = at the cut-off, as before)
        "confirm_wait_s": 2.0,
    },
    "filters": {
        "apply_to_manual": False,         # a rule that is on also refuses an order Nico approves by hand
        "unknown_blocks": False,          # a rule that cannot be judged (no data) refuses the order
        "two_sided": {"enabled": False},
        "spread": {"enabled": False, "max_pct": 15.0},
        "open_interest": {"enabled": False, "min": 100},
        "quote_age": {"enabled": False, "max_s": 120},
        "delta": {"enabled": False, "min": 0.25, "max": 0.80},   # only for a contract the director picked itself
    },
    "stuck_put": {
        "enabled": False,
        "after_min": 60,                  # minutes of "stock through the level, put cannot be priced" before it acts
        "max_spread_pct": 100.0,          # the wider quote it then accepts: buy and sell prices this far apart at most
        "max_age_s": 900,                 # ... no older than this
        "min_share_of_exercise_value": 0.9,   # ... and a bid no lower than this share of what the put is worth exercised
    },
    "placing_recovery": {"enabled": False, "after_s": 600},
    "shadow_max_looks": 8,                # record-only: how many looks one order is followed for
}

GATE_WORDS = {
    "opt_two_sided": "there is no real buyer and seller for this option right now",
    "opt_spread": "the option's buy and sell prices are too far apart",
    "opt_open_interest": "too few contracts of this option are open, so it would be hard to sell",
    "opt_quote_age": "the option's price is too old to trust",
    "opt_delta": "this option would move too little with the stock, or almost exactly like the stock",
}
FILTER_RULES = ("two_sided", "spread", "open_interest", "quote_age", "delta")
RETRYABLE_GATES = ("opt_two_sided", "opt_spread", "opt_quote_age")   # can clear by themselves shortly after the open

_OPEN = ("new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "calculated",
         "pending_cancel", "pending_replace")
_DEAD = ("canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced")
_WORK_OPEN = ("working", "cancelling", "sending")
_SOFT = ("stop", "target")
_HALF_DAYS = {"2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26"}
_ppind = {}                               # OCC -> penny-pilot flag (per process; a failed look-up is not kept)
_WARNED = {}                              # (review) place -> monotonic time of its last warning in the log


# ── lazy module access + injectable I/O (replaced in tests) ──────────────────────────────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _AL():
    import mason_agent_loop
    return mason_agent_loop


def _EX():
    import mason_execution
    return mason_execution


def _MQ():
    import mason_quotes
    return mason_quotes


def _now_s() -> float:
    return time.time()


def _sleep(s) -> None:
    time.sleep(s)


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_order(order_id) -> dict:
    return _EX().get_order(order_id)


def _cancel(order_id):
    """Ask the broker to cancel. Returns the HTTP code (None = no answer). The answer is never trusted as
    "cancelled": only the order's own record decides (see _settled)."""
    try:
        code, _ = _EX()._request("DELETE", f"/orders/{order_id}")
        return code
    except Exception:
        return None


def _order_by_coid(coid):
    """(order | None, 'ok' | 'not_found' | 'error')."""
    try:
        code, d = _EX()._request("GET", "/orders:by_client_order_id", params={"client_order_id": coid})
    except Exception:
        return None, "error"
    if code == 200 and isinstance(d, dict) and d.get("id"):
        return d, "ok"
    return (None, "not_found") if code == 404 else (None, "error")


def _place_buy(sym, qty, limit, pitch) -> dict:
    """ONE buy-to-open limit day order through the same path every other Mason order takes."""
    return _EX().place_trade(symbol=sym, qty=qty, side="buy", entry=limit, pitch_id=pitch, respect_regime=True,
                             verify=False)


def _quote(sym, cfg, *, relaxed=False, max_spread_pct=None):
    kw = {"relaxed": relaxed}
    if max_spread_pct is not None:
        kw["max_spread_pct"] = max_spread_pct
    return _AT()._trusted_quote(sym, True, cfg, **kw)


def _record_only_read(read):
    """(review) A price read made ONLY to write a record (the record-only followers, the stuck-put record). It goes
    through the same price reader as the stop checks, but a refused or failed Robinhood answer to THIS read does not
    pause Robinhood for every price check in Mason (mason_quotes pauses it after any error). A read that waited its
    full time and got no answer still does: Robinhood really is not answering (the same rule as the short-put
    look-ups, review of 2026-11-17)."""
    MQ = _MQ()
    try:
        before = dict(MQ._breaker)
    except Exception:
        before = None
    q = read()
    try:
        b = MQ._breaker
        if before is not None and int(b.get("fails") or 0) > int(before.get("fails") or 0) \
                and b.get("last_error") != "TimeoutError":
            b.update(before)
    except Exception:
        pass
    return q


def _warn(where, err):
    """(review) A failure inside option order handling is swallowed (the caller falls back to today's behaviour), but
    it is written to Mason's log as a warning, at most once an hour for the same place, so a part that keeps failing
    is seen."""
    try:
        now = time.monotonic()
        if now - _WARNED.get(where, -1e12) >= 3600.0:
            _WARNED[where] = now
            log.warning("[option_orders] %s failed: %s: %s", where, type(err).__name__, str(err)[:300])
    except Exception:
        pass


def _notify(title, body, *, ticker=None, rec_id=None, severity="info", dedup=None):
    _AT()._notify(title, body, ticker=ticker, rec_id=rec_id, severity=severity, dedup=dedup)


def _activity(kind, title, *, ticker, rec_id, extra):
    _AT()._activity(kind, title, approver="auto", ticker=ticker, rec_id=rec_id, extra=extra)


def reset():
    """Forget the per-process caches (tests; harmless in production)."""
    _ppind.clear()


# ── small pure helpers ───────────────────────────────────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _merge(base: dict, over) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ocfg(cfg=None) -> dict:
    """autotrade.option_orders over DEFAULTS. `cfg` = the autotrade config dict (None = read it)."""
    try:
        if cfg is None:
            cfg = _AT()._at_cfg()
        v = (cfg or {}).get("option_orders")
        return _merge(DEFAULTS, v if isinstance(v, dict) else {})
    except Exception:
        return _merge(DEFAULTS, {})


def _oc(x) -> dict:
    """Accept either an option_orders block already merged (it has 'entry') or an autotrade config / None."""
    if isinstance(x, dict) and "entry" in x and "exit" in x and "filters" in x and "option_orders" not in x:
        return x
    return ocfg(x)


def _on(block) -> bool:
    return isinstance(block, dict) and block.get("enabled") is True


def active(cfg=None) -> bool:
    """False = nothing in this module runs for an order (master off and record-only off)."""
    try:
        oc = _oc(cfg)
        return oc.get("enabled") is True or oc.get("shadow") is True
    except Exception:
        return False


def live(part, cfg=None) -> bool:
    """A part acts for real only when the master switch AND its own switch are on."""
    try:
        oc = _oc(cfg)
        return oc.get("enabled") is True and _on(oc.get(part))
    except Exception:
        return False


def shadowed(part, cfg=None) -> bool:
    """A part is recorded only (not acted on)."""
    try:
        oc = _oc(cfg)
        return oc.get("shadow") is True and not live(part, oc)
    except Exception:
        return False


def recording(cfg=None) -> bool:
    try:
        oc = _oc(cfg)
        return active(oc) and oc.get("record") is not False
    except Exception:
        return False


def rule_live(name, cfg=None) -> bool:
    try:
        oc = _oc(cfg)
        return oc.get("enabled") is True and _on((oc.get("filters") or {}).get(name))
    except Exception:
        return False


def tick_size(px, ppind, underlying=None) -> float:
    return _EX()._option_tick_size(float(px), ppind, underlying)


def floor_tick(px, ppind, underlying=None) -> float:
    """The highest valid option price at or under `px` (0.01 / 0.05 for a penny contract, 0.05 / 0.10 otherwise)."""
    px = float(px)
    t = tick_size(px, ppind, underlying)
    return round(math.floor(px / t + 1e-9) * t, 2)


def ceil_tick(px, ppind, underlying=None) -> float:
    """The lowest valid option price at or above `px`."""
    px = float(px)
    t = tick_size(px, ppind, underlying)
    v = round(math.ceil(px / t - 1e-9) * t, 2)
    t2 = tick_size(v, ppind, underlying)                   # rounding up may cross into the coarser grid at 3.00
    return v if abs(t2 - t) < 1e-12 else round(math.ceil(v / t2 - 1e-9) * t2, 2)


def _fractions(rungs, first, last) -> list:
    """Clean a list of rung shares: numbers in [0, 1], in the direction first -> last, the ends forced."""
    out = []
    for r in rungs if isinstance(rungs, (list, tuple)) else []:
        v = _f(r)
        if v is not None:
            out.append(min(1.0, max(0.0, v)))
    out = sorted(set(out), reverse=first > last)
    if not out or abs(out[0] - first) > 1e-9:
        out.insert(0, first)
    if abs(out[-1] - last) > 1e-9:
        out.append(last)
    return out


def entry_ladder(start, cap, rungs, ppind, underlying=None) -> list:
    """The prices of an entry schedule: from `start` up to `cap`, each on a valid tick, strictly rising, the last
    one exactly `cap`. One price when there is nothing to climb (start at or above the cap)."""
    start, cap = float(start), float(cap)
    if start >= cap - 1e-9:
        return [round(cap, 2)]
    out = []
    for fr in _fractions(rungs, 0.0, 1.0):
        p = cap if fr >= 1.0 - 1e-9 else floor_tick(start + fr * (cap - start), ppind, underlying)
        p = min(round(p, 2), round(cap, 2))
        if not out or p > out[-1] + 1e-9:
            out.append(p)
    if out[-1] < cap - 1e-9:
        out.append(round(cap, 2))
    return out


def entry_start(mid, cap, ppind, underlying=None) -> float:
    """The first price of an entry: the middle price, on a valid tick at or under it, never above the cap and
    never under the smallest tick."""
    cap = float(cap)
    s = floor_tick(min(float(mid), cap), ppind, underlying)
    if s <= 0:
        s = tick_size(0.0, ppind, underlying)
    return round(min(s, cap), 2)


def exit_rungs(reason, oc) -> list:
    x = _oc(oc)["exit"]
    key = "stop_rungs" if reason == "stop" else ("target_rungs" if reason == "target" else "must_rungs")
    return _fractions(x.get(key), max([_f(v) or 0.0 for v in (x.get(key) or [0.0])] + [0.0]), 0.0)


def exit_rung_index(n_rungs, orders_sent, to_bid_s, step_s) -> int:
    """Which rung a sale is on. One rung per order sent; and never fewer rungs ahead than the time left before
    the sale must be at the bid allows (`to_bid_s`; None = no such time; <= 0 = at the bid now)."""
    last = max(0, int(n_rungs) - 1)
    k = min(last, max(0, int(orders_sent or 0)))
    if to_bid_s is not None:
        if to_bid_s <= 0:
            return last
        step_s = max(1.0, float(step_s or 180))
        k = max(k, last - min(last, int(math.ceil(float(to_bid_s) / step_s))))
    return k


def exit_price(bid, mid, share, prev=None, ppind=False, underlying=None) -> float:
    """The limit of a sale on one rung: `share` of the way from the bid up to the middle price, on a valid tick,
    never under the bid, and never above the previous limit of the same sale (a sale's price only goes down; when
    the bid has risen above that previous limit the order simply keeps it: it sells at the bid)."""
    bid, mid = float(bid), float(mid)
    floor = floor_tick(bid, ppind, underlying)             # (a limit at or under the bid: it sells at the bid)
    p = floor
    if share > 1e-9 and mid > bid:
        p = max(floor, ceil_tick(bid + min(1.0, share) * (mid - bid), ppind, underlying))
    if prev is not None:
        p = min(p, float(prev))
    return round(p, 2)


def real_close(now, clk=None):
    """Today's real closing time (same kind of datetime as `now`): the earlier of the broker clock's close for
    today (when it has said one) and the calendar's (13:00 on the listed short days, else 16:00)."""
    cal = now.replace(hour=13 if now.date().isoformat() in _half_days() else 16, minute=0, second=0, microsecond=0)
    try:
        c = _AT()._session_close(clk, now)
        if c is not None:
            if now.tzinfo is None:
                c = c.replace(tzinfo=None)
            elif c.tzinfo is None:
                c = c.replace(tzinfo=now.tzinfo)
            return min(c, cal)
    except Exception:
        pass
    return cal


def _half_days() -> set:
    try:
        import mason_desk_pipeline as PP
        return set(PP._NYSE_HALF_DAYS) | _HALF_DAYS
    except Exception:
        return set(_HALF_DAYS)


def must_bid_by(reason, expiry, now, clk, cfg, oc):
    """The time from which a must-do sale is priced at the bid (exactly as before this change):
       same-day cut-off  -> the cut-off itself;
       anything else     -> `must_bid_before_close_min` before the real close, and for a contract that expires
                            today never later than the same-day cut-off."""
    AT = _AT()
    cut, _txt = AT.close_cutoff((cfg or {}).get("zero_dte_flat_by_et"), "15:15", clk, now, cfg)
    if cut > real_close(now, clk):                         # the broker did not say today is short, the calendar does
        cut = real_close(now, clk) - timedelta(minutes=45)
    if reason == "flat_by":
        return cut
    d = real_close(now, clk) - timedelta(minutes=float(_oc(oc)["exit"].get("must_bid_before_close_min") or 0))
    try:
        if expiry is not None and expiry == now.date():
            d = min(d, cut)
    except Exception:
        pass
    return d


def flat_by_start(cut, now, clk, cfg=None):
    """When the sale of a contract that expires today starts. `cut` is the exit manager's own cut-off time.
    Unless exit stepping is switched on this is `cut` itself, untouched. With it on: `flat_by_lead_s` earlier, so
    that the sale has walked its steps and is AT the bid at the cut-off; and on a short day the calendar knows about
    but the broker's clock did not announce, the cut-off is counted from the calendar's close."""
    try:
        oc = _oc(cfg)
        if not live("exit", oc) or not _MQ().qcfg(cfg).get("enabled", True):
            return cut                                     # (with the price reader switched off nothing is stepped)
        cal = real_close(now, clk) - timedelta(minutes=45)
        if cal < cut:
            cut = cal
        return cut - timedelta(seconds=max(0.0, float(oc["exit"].get("flat_by_lead_s") or 0)))
    except Exception:
        return cut


def _qfacts(q) -> dict:
    """The few facts of a quote kept next to an order."""
    q = q or {}
    out = {k: q.get(k) for k in ("bid", "ask", "mid", "source", "age_s", "spread_pct")}
    for k in ("delta", "iv", "oi", "volume", "bid_size", "ask_size", "relaxed", "wide"):
        if q.get(k) is not None:
            out[k] = q[k]
    if q and not q.get("ok") and not q.get("legacy"):
        out["no_price"] = str(q.get("reason") or "no usable price")[:160]
    return out


def _cap_words(src) -> str:
    s = str(src or "")
    if s == "entry_limit":
        return "the director's price"
    if "below director entry" in s:
        return "the asking price, which is under the director's price"
    if "ask" in s:
        return "the asking price when the order was approved"
    if "mid +" in s:
        return "the middle price when the order was approved plus " + s.split("+", 1)[1].strip()
    return "the middle price when the order was approved" if s == "mid" else (s or "the price when the order was approved")


def _money(v) -> str:
    v = _f(v)
    return "n/a" if v is None else f"{v:.2f}"


# ── DB helpers (no execution_history entry: these are records, not state changes of the call) ───────────────

def _conn():
    import mason_db
    return mason_db.get_connection()


def _patch(rec_id, fn):
    """Read-modify-write of recommendations.extra_json.execution in one transaction. fn(execution: dict) changes
    it in place. Returns the new execution block, or None when the call does not exist."""
    conn = _conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = conn.execute("SELECT extra_json FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            if not r:
                conn.execute("ROLLBACK")
                return None
            try:
                ex = json.loads(r["extra_json"] or "{}")
            except Exception:
                ex = {}
            cur = dict(ex.get("execution") or {})
            fn(cur)
            ex["execution"] = cur
            conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?", (json.dumps(ex, default=str), rec_id))
            conn.execute("COMMIT")
            return cur
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


def _rows(sql, args=()):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _work_ids() -> list:
    return [r["id"] for r in _rows(
        "SELECT id FROM recommendations WHERE kind='director' AND "
        "json_extract(extra_json,'$.execution.work.state') IN ('working','cancelling','sending')")]


def _shadow_ids() -> list:
    return [r["id"] for r in _rows(
        "SELECT id FROM recommendations WHERE kind='director' AND "
        "json_extract(extra_json,'$.execution.features.option_entry.shadow.state')='tracking'")]


def _exit_shadow_rows() -> list:
    try:
        return _rows("SELECT * FROM agent_trades WHERE source='mason' AND occ IS NOT NULL AND "
                     "json_extract(features_json,'$.exit_shadow.state')='tracking'")
    except Exception:
        return []


def _penny(sym, known=None, net=True):
    """Penny-pilot flag of a contract (decides its tick). A failed look-up answers False (the coarser tick) and is
    not remembered. net=False (review: a price worked out only for a record): no look-up at the broker, the flag
    already known or the coarser tick."""
    if known is not None:
        _ppind[sym] = bool(known)
        return bool(known)
    if sym in _ppind:
        return _ppind[sym]
    if not net:
        return False
    try:
        m = _EX().get_option_contract_meta(sym)
        if m.get("ok"):
            _ppind[sym] = bool(m.get("ppind"))
            return _ppind[sym]
    except Exception:
        pass
    return False


def _underlying(sym) -> str:
    try:
        return _EX()._underlying_from_occ(sym)
    except Exception:
        return ""


# ══ C. filters at entry ══════════════════════════════════════════════════════════════════════════════════════

def _snap_oi(sym, today_iso):
    """Open interest of this contract from TODAY's stored option snapshot (mason_option_snaps), or None. Open
    interest is a once-a-day number, so a row of today is as fresh as it gets."""
    try:
        r = _rows("SELECT open_interest FROM opt_snap_contracts WHERE occ=? AND date_et=? AND open_interest IS NOT NULL "
                  "ORDER BY ts DESC LIMIT 1", (sym, today_iso))
        return _f(r[0]["open_interest"]) if r else None
    except Exception:
        return None


def entry_filters(cfg, *, quote, contract=None, sym=None, sap=False, approver="auto", today=None) -> dict:
    """What the five entry rules say about this contract right now. Pure over its arguments except for one read
    of today's stored snapshot (open interest, only when the quote has none). Never raises.

    Returns {"v", "source", "facts", "rules": [{"rule", "on", "ok" (True / False / None = could not be judged),
    "detail"}], "would_block": [...], "blocked_by": [...], "live_blocks": [{"gate", "detail"}], "warnings": [...]}.
    A rule that is ON and fails adds a gate (a warning for a hand approval unless filters.apply_to_manual)."""
    oc = _oc(cfg)
    fc = oc["filters"]
    q = quote or {}
    out = {"v": VERSION, "source": q.get("source"), "facts": {}, "rules": [], "would_block": [], "blocked_by": [],
           "live_blocks": [], "warnings": []}
    try:
        manual = approver != "auto" and fc.get("apply_to_manual") is not True
        bid, ask, mid = _f(q.get("bid")), _f(q.get("ask")), _f(q.get("mid"))
        usable = bool(q.get("ok") and not q.get("legacy") and bid and ask and mid)
        spr = (ask - bid) / mid * 100.0 if usable else _f(q.get("spread_pct"))
        age = _f(q.get("age_s"))
        oi, oi_src = _f(q.get("oi")), "the quote"
        if oi is None and sym:
            oi, oi_src = _snap_oi(sym, (today or _AT()._now_et().date()).isoformat()), "today's stored snapshot"
        if oi is None and isinstance(contract, dict):
            oi, oi_src = _f(contract.get("open_interest")), "the broker's contract record (the day before)"
        delta = _f(q.get("delta"))
        src = {"robinhood": "Robinhood", "alpaca": "Alpaca (Robinhood could not be read)"}.get(q.get("source"), "no source")
        out["facts"] = {"bid": bid, "ask": ask, "mid": mid, "spread_pct": round(spr, 2) if spr is not None else None,
                        "age_s": age, "open_interest": oi, "open_interest_from": oi_src if oi is not None else None,
                        "delta": delta, "iv": _f(q.get("iv")), "volume": _f(q.get("volume")),
                        "bid_size": _f(q.get("bid_size")), "ask_size": _f(q.get("ask_size")), "source": q.get("source")}

        def rule(name, ok, detail):
            on = rule_live(name, oc)
            refused = ok is False or (ok is None and fc.get("unknown_blocks") is True)
            out["rules"].append({"rule": name, "on": on, "ok": ok, "detail": detail})
            if not refused:
                return
            gate = "opt_" + name
            if not on:
                out["would_block"].append(name)
            elif manual:
                out["warnings"].append(f"{GATE_WORDS[gate]} — {detail}")
            else:
                out["blocked_by"].append(name)
                out["live_blocks"].append({"gate": gate, "detail": detail})

        if q.get("legacy"):                                # the price reader is switched off (autotrade.quotes.enabled
            for name in FILTER_RULES:                      # false): the old Alpaca quote carries no age and no checks
                rule(name, None, "not checked: the price reader is switched off, only the old Alpaca quote is there")
            return out
        # 1. a real buyer and a real seller
        bs, as_ = _f(q.get("bid_size")), _f(q.get("ask_size"))
        if not usable:
            rule("two_sided", False, "there is no usable buy and sell price for it right now ("
                 + str(q.get("reason") or "no quote")[:140] + ")")
        elif (bs is not None and bs <= 0) or (as_ is not None and as_ <= 0):
            rule("two_sided", False, f"buy price {bid:.2f} for {bs or 0:g} contracts, sell price {ask:.2f} for "
                                     f"{as_ or 0:g}: one side is empty ({src})")
        else:
            rule("two_sided", True, f"buyers at {bid:.2f} and sellers at {ask:.2f} ({src})")
        # 2. how far apart
        lim = _f((fc.get("spread") or {}).get("max_pct"))
        if spr is None or lim is None:
            rule("spread", None, "not checked: no buy and sell price to measure")
        else:
            rule("spread", spr <= lim + 1e-9,
                 f"buy and sell prices are {spr:.1f}% apart ({bid:.2f} / {ask:.2f}); the limit is {lim:g}%")
        # 3. contracts open
        need = _f((fc.get("open_interest") or {}).get("min"))
        if oi is None or need is None:
            rule("open_interest", None, "not checked: the number of open contracts is not known")
        else:
            rule("open_interest", oi >= need, f"{oi:g} contracts are open (from {oi_src}); at least {need:g} are wanted")
        # 4. how old the price is
        mx = _f((fc.get("quote_age") or {}).get("max_s"))
        if age is None or mx is None or not usable:
            rule("quote_age", None, "not checked: the price carries no usable time")
        else:
            rule("quote_age", age <= mx + 1e-9, f"the price is {age:.0f} seconds old; the limit is {mx:g} seconds")
        # 5. how closely it follows the stock (only a contract the director picked itself)
        dc = fc.get("delta") or {}
        lo, hi = _f(dc.get("min")), _f(dc.get("max"))
        if sap:
            out["rules"].append({"rule": "delta", "on": False, "ok": True,
                                 "detail": "not used: Mason picked this put itself, by how closely it follows the stock"})
        elif delta is None or lo is None or hi is None:
            rule("delta", None, "not checked: the quote does not say how closely the option follows the stock")
        else:
            d = abs(delta)
            rule("delta", lo - 1e-9 <= d <= hi + 1e-9,
                 f"it moves about ${d:.2f} for each $1 in the stock; wanted between ${lo:.2f} and ${hi:.2f}")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["live_blocks"], out["blocked_by"] = [], []
    return out


def filters_record(fl: dict) -> dict:
    """The compact form kept on the call and the trade."""
    return {k: fl.get(k) for k in ("v", "source", "facts", "rules", "would_block", "blocked_by", "error")
            if fl.get(k) not in (None, [], {}) or k in ("rules", "would_block", "blocked_by")}


# ══ A. the entry: plan, at approval ══════════════════════════════════════════════════════════════════════════

def build_hook(cfg, *, approver, quote, contract, sym, vehicle, limit, src, entry_limit, cap, cap_src, sap, ppind,
               ticker, expiry, today) -> dict | None:
    """Called by mason_autotrade._build for every option order it builds, right after today's limit is worked out.
    None when nothing is switched on (then _build is untouched). Otherwise:
      {"features": {...}        records for the call (filters, the entry plan and the quote it came from),
       "gates": [(name, ok, detail)], "warnings": [...]     from filter rules that are ON,
       "limit", "src"           only when entry stepping is ON: the price the order is SIZED AND CHECKED at
                                (the most it may ever pay),
       "work": {...}            only when entry stepping is ON: the schedule; _build then sends the first order at
                                work["start"]}."""
    oc = ocfg(cfg)
    if not active(oc):
        return None
    out = {"features": {}, "gates": [], "warnings": []}
    q = quote or {}
    try:
        fl = entry_filters(oc, quote=q, contract=contract, sym=sym, sap=bool(sap), approver=approver, today=today)
        any_rule = any(rule_live(n, oc) for n in FILTER_RULES)
        if any_rule or oc.get("shadow") is True:
            out["features"]["option_filters"] = dict(filters_record(fl), shadow=not any_rule)
        failed = {b["gate"]: b["detail"] for b in fl.get("live_blocks") or []}
        for r in fl.get("rules") or []:                    # a rule that is ON and passed is listed too
            g = "opt_" + r["rule"]
            if r.get("on") and g not in failed and r.get("ok") is not False:
                out["gates"].append((g, True, str(r.get("detail") or "")[:400]))
        for g, d in failed.items():
            out["gates"].append((g, False, str(d)[:400]))
        out["warnings"] = [str(w)[:400] for w in fl.get("warnings") or []]
    except Exception as e:
        _warn("build_filters", e)
        out["features"]["option_filters"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        rec = {"v": VERSION, "mode": "record"}
        if recording(oc):
            rec["quote"] = _MQ().compact(q)
            rec["today"] = {"limit": limit, "basis": src}
        mid = _f(q.get("mid"))
        plan = None
        if approver == "auto" and limit and mid and q.get("ok") and q.get("two_sided") and not q.get("legacy"):
            cap_px, cap_basis = float(limit), src
            if not entry_limit and not sap and cap:        # no price from the director: today sends the middle price;
                cap_px = max(floor_tick(float(cap), ppind, ticker),            # the schedule may climb to today's own
                             entry_start(mid, float(cap), ppind, ticker))      # buy limit (ask, or middle + band),
                cap_basis = cap_src                                            # on a tick at or under it
            start = entry_start(mid, cap_px, ppind, ticker)
            same_day = False
            try:
                same_day = date.fromisoformat(str(expiry)[:10]) == today
            except Exception:
                pass
            plan = {"cap": round(cap_px, 2), "cap_basis": _cap_words(cap_basis), "cap_src": cap_basis, "start": start,
                    "rungs": entry_ladder(start, cap_px, oc["entry"].get("rungs"), ppind, ticker),
                    "director_price": bool(entry_limit), "same_day": bool(same_day), "sap": bool(sap),
                    "ppind": bool(ppind), "underlying": ticker, "sym": sym,
                    "bid": _f(q.get("bid")), "ask": _f(q.get("ask")), "mid": mid}
        if plan and live("entry", oc):
            rec.update(mode="live", cap=plan["cap"], cap_basis=plan["cap_basis"], start=plan["start"],
                       rungs=plan["rungs"])
            out["limit"], out["src"] = plan["cap"], plan["cap_src"]
            out["work"] = plan
        elif plan and oc.get("shadow") is True:
            rec.update(mode="shadow", cap=plan["cap"], cap_basis=plan["cap_basis"], start=plan["start"],
                       rungs=plan["rungs"], director_price=plan["director_price"], same_day=plan["same_day"],
                       ppind=plan["ppind"], underlying=ticker)
        if len(rec) > 2:
            out["features"]["option_entry"] = rec
    except Exception as e:
        _warn("build_plan", e)
        out["features"]["option_entry"] = {"v": VERSION, "error": f"{type(e).__name__}: {e}"}
        out.pop("limit", None)
        out.pop("work", None)
    return out


def summary_words(order) -> str:
    """Added to the one-line order summary when the order is worked."""
    w = (order or {}).get("work") or {}
    if not w or len(w.get("rungs") or []) < 2:
        return ""
    return f", raised in steps to {w['cap']:g} at most"


def working_note(p, order, order_id, who, rec_id, title, body):
    """The inbox note for an order that is being worked (instead of 'Order resting')."""
    try:
        w = order.get("work") or {}
        rungs = w.get("rungs") or []
        if not w:
            return title, body
        oc = ocfg()
        if len(rungs) < 2:
            how = (f"Mason asked {w['cap']:.2f} at once: that is the most it may pay ({w['cap_basis']}), and it is at or "
                   "under the middle of the buy and sell prices, so there is nothing to raise.")
        else:
            how = (f"Mason asked {w['start']:.2f} first, the middle of the buy and sell prices ({_money(w.get('bid'))} / "
                   f"{_money(w.get('ask'))}). If nobody sells at that price it raises its price at each look, about "
                   f"every 3 minutes, in {len(rungs) - 1} step(s) up to {w['cap']:.2f} at most ({w['cap_basis']}).")
        end = _end_rule(w, oc)
        tail = (f" If it has still not been filled then, the order stays in the market at {w['cap']:.2f} until the "
                "close." if end == "rest" else
                " If it has still not been filled then, the order is cancelled and nothing is bought.")
        t = (f"Order working (paper, {who}): BUY {order['qty']} {order['symbol']} — first price {order['limit_price']:.2f}, "
             f"at most {w['cap']:.2f} — director rec {rec_id}")
        b = (f"{p['summary']}\nSubmitted (Alpaca order {order_id}). {how}{tail}\n"
             + (p.get("exit_rules") or {}).get("note", ""))
        return t, b
    except Exception:
        return title, body


def _end_rule(w, oc) -> str:
    """'rest' or 'cancel' when the limit is reached without a fill."""
    e = _oc(oc)["entry"]
    if w.get("same_day"):
        return "cancel"                                    # an option that expires today is never left resting
    rule = e.get("at_limit_director_price") if w.get("director_price") else e.get("at_limit_own_price")
    return "rest" if str(rule or "").lower() == "rest" else "cancel"


# ══ D. records at placement and at the fill ══════════════════════════════════════════════════════════════════

def _order_entry(step, limit, qty, order_id, coid, q, **extra) -> dict:
    out = {"step": step, "at": _nowiso(), "limit": _f(limit), "qty": _f(qty), "order_id": order_id, "coid": coid}
    out.update(_qfacts(q))
    out.update(extra)
    return out


def _fill_record(entry, qty, px, at=None, seen=None, lag_s=None) -> dict:
    """A fill next to the prices its order was sent on (cost against the middle price: audit L10)."""
    px, qty = _f(px), _f(qty)
    out = {"qty": qty, "px": px, "at": at or _nowiso(), "order_id": (entry or {}).get("order_id"),
           "step": (entry or {}).get("step"), "limit": (entry or {}).get("limit")}
    bid, ask, mid = _f((entry or {}).get("bid")), _f((entry or {}).get("ask")), _f((entry or {}).get("mid"))
    if px is not None and mid:
        out.update({"mid_at_order": mid, "paid_over_mid": round(px - mid, 4),
                    "paid_over_mid_pct": round((px - mid) / mid * 100.0, 3)})
        if qty:
            out["cost_vs_mid_usd"] = round((px - mid) * qty * 100.0, 2)
    if bid and ask:
        out["half_spread_at_order"] = round((ask - bid) / 2.0, 4)
    if seen is not None:
        out["quote_seen_after"] = dict(_qfacts(seen), lag_s=lag_s)
    return out


def placed(execution, p, order, exe, fill, state):
    """Called by _place_claimed just before the call's execution block is written. Adds the records of the first
    order (and its fill, when it filled at once) and, when entry stepping is on, the schedule's state. Returns the
    same dict untouched when nothing is switched on."""
    try:
        if not isinstance(order, dict) or order.get("asset") != "option":
            return execution
        feats = execution.get("features")
        oe = feats.get("option_entry") if isinstance(feats, dict) else None
        if not isinstance(oe, dict):
            return execution
        oc = ocfg()
        oe = dict(oe)
        q = p.get("quote") or {}
        oid = (exe or {}).get("order_id")
        if recording(oc) and oid:
            ent = _order_entry(0, order.get("limit_price"), order.get("qty"), oid, (exe or {}).get("client_order_id"), q)
            oe["orders"] = [ent]
            fs = str((fill or {}).get("status") or "").lower()
            if fs in ("filled", "partially_filled") and _f((fill or {}).get("filled_avg_price")):
                oe["fill"] = _fill_record(ent, fill.get("filled_qty"), fill.get("filled_avg_price"))
        work = order.get("work")
        now_s = _now_s()
        fs = str((fill or {}).get("status") or "").lower()
        if isinstance(work, dict) and execution.get("approver") == "auto" and oid:
            w = dict(work)
            w.update({"v": VERSION, "step": 0, "order_id": oid, "limit": _f(order.get("limit_price")),
                      "qty": _f(order.get("qty")), "started_s": now_s, "sent_s": now_s, "started_at": _nowiso(),
                      "day": _AT()._now_et().date().isoformat(),     # (review) a schedule never sends on a later day
                      "stop": _f((p.get("exit_rules") or {}).get("stop_premium")),
                      "target": _f((p.get("exit_rules") or {}).get("target_premium")), "errors": 0})
            if w.get("sap"):                               # a put bought for a short call: its estimated premium levels
                er = p.get("exit_rules") or {}
                w["stop"], w["target"] = _f(er.get("est_stop_premium")), _f(er.get("est_target_premium"))
            if state == "approved_pending_fill" or (state == "placed" and fs == "partially_filled"):
                w["state"] = "working"
            else:
                w.update(state="done", ended_at=_nowiso(),
                         outcome="filled at the first price" if state == "placed" else "the first order was not taken")
                oe["outcome"] = w["outcome"]
            execution["work"] = w
        elif oe.get("mode") == "shadow" and oid and state in ("approved_pending_fill", "placed"):
            if len(oe.get("rungs") or []) < 2:
                oe["shadow"] = {"state": "done", "outcome": "nothing to step: the limit is at or under the middle "
                                                             "price, so the schedule would have sent the same order"}
            else:
                oe["shadow"] = {"state": "tracking", "started_s": now_s, "step": 0, "step_s": now_s,
                                "limit": oe.get("start"), "looks": []}
        execution["features"] = dict(feats, option_entry=oe)
    except Exception as e:
        _warn("placed", e)
        pass
    return execution


def holds(e) -> bool:
    """True while the schedule is between two orders (a cancel on its way, or a new order not yet confirmed): the
    old order being dead does NOT mean the call is unfilled yet."""
    try:
        w = (e or {}).get("work")
        return isinstance(w, dict) and w.get("state") in ("cancelling", "sending")
    except Exception:
        return False


def filled(rec_id, e, o):
    """Called by _settle_one after a resting entry order was booked as filled. Records the fill next to the
    prices its order was sent on, and ends the schedule. Never raises."""
    try:
        oe = ((e or {}).get("features") or {}).get("option_entry")
        w = (e or {}).get("work")
        if not isinstance(oe, dict) and not isinstance(w, dict):
            return
        oc = ocfg()
        seen = lag = None
        # (review) the price seen at the booking is read only while entry stepping is ON: in record-only mode this
        # runs inside settle_pending, before the exit checks of the same look, and a read there could hold them up
        if recording(oc) and isinstance(oe, dict) and live("entry", oc):
            try:
                seen = _quote(str(o.get("symbol") or e.get("symbol") or "").upper(), _AT()._at_cfg())
                ft = _MQ().parse_ts(o.get("filled_at"))
                lag = round(max(0.0, _now_s() - ft), 1) if ft else None
            except Exception:
                seen = None

        def fn(cur):
            f = dict(cur.get("features") or {})
            rec = f.get("option_entry")
            if isinstance(rec, dict) and recording(oc):
                rec = dict(rec)
                ent = next((x for x in reversed(rec.get("orders") or []) if x.get("order_id") == o.get("id")), None)
                rec["fill"] = _fill_record(ent, o.get("filled_qty"), o.get("filled_avg_price"), o.get("filled_at"),
                                           seen, lag)
                if isinstance(cur.get("work"), dict):
                    rec["outcome"] = f"filled at step {int((ent or {}).get('step') or 0) + 1}"
                f["option_entry"] = rec
                cur["features"] = f
            wk = cur.get("work")
            if isinstance(wk, dict) and wk.get("state") in _WORK_OPEN:
                part = (_f(o.get("filled_qty")) or 0) < (_f(wk.get("qty")) or 0) - 1e-9
                cur["work"] = dict(wk, state="done", ended_at=_nowiso(), cancel=None, pending=None,
                                   outcome="filled in part; the rest was not bought" if part else "filled")
        _patch(rec_id, fn)
    except Exception as e:
        _warn("filled", e)
        pass


# ══ A. the entry schedule, look by look ══════════════════════════════════════════════════════════════════════

def tick_needed(cfg=None) -> bool:
    """Does the agent loop need this module's step this look? True while anything is switched on, and while a
    schedule is still open (so that switching the feature off in mid-schedule ends it cleanly)."""
    try:
        if active(cfg):
            return True
        return bool(_work_ids())
    except Exception:
        return False


def step(now_et=None) -> dict:
    """(review) The agent loop's step BEFORE `settle`: the open entry schedules and the 'placing' recovery only. The
    record-only followers are a separate step after the exit checks (`follow`), so their price reads can never hold
    up a stop check. Never raises."""
    return tick(now_et, follow=False)


def follow(now_et=None) -> dict:
    """(review) The agent loop's step AFTER the exit checks: the record-only followers only. Never raises."""
    return tick(now_et, live_work=False)


def tick(now_et=None, follow=True, live_work=True) -> dict:
    """One look: every open entry schedule, the record-only followers, the 'placing' recovery. Never raises.
    (review) The agent loop runs it in two parts: `step` (the schedules) before settle, `follow` after the exits."""
    out = {}
    try:
        AT = _AT()
        cfg = AT._at_cfg()
        oc = ocfg(cfg)
        now = now_et or AT._now_et()
        ids = _work_ids() if live_work else []
        sh_ids, sh_rows = (_shadow_ids(), _exit_shadow_rows()) if (follow and oc.get("shadow") is True) else ([], [])
        clk = AT._clock() if (ids or sh_ids or sh_rows) else None      # (nothing to look at: no broker call at all)
        if ids:
            out["entries"] = []
            for rid in ids:
                try:
                    out["entries"].append(work_entry(rid, cfg=cfg, oc=oc, now=now, clk=clk))
                except Exception as e:
                    _warn("entry_schedule", e)
                    out["entries"].append({"rec_id": rid, "error": f"{type(e).__name__}: {e}"})
        if sh_ids or sh_rows:
            sh = []
            for rid in sh_ids:
                try:
                    sh.append(_shadow_entry(rid, cfg, oc, now, clk))
                except Exception as e:
                    _warn("follow_entry", e)
                    sh.append({"rec_id": rid, "error": f"{type(e).__name__}: {e}"})
            for row in sh_rows:
                try:
                    sh.append(_shadow_exit(row, cfg, oc, now, clk))
                except Exception as e:
                    _warn("follow_exit", e)
                    sh.append({"rec_id": row.get("rec_id"), "error": f"{type(e).__name__}: {e}"})
            if sh:
                out["shadow"] = sh
        if live_work and live("placing_recovery", oc):
            r = recover_placing(cfg=cfg, oc=oc)
            if r:
                out["placing"] = r
    except Exception as e:
        _warn("tick", e)
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _save(rec_id, w, extra_fn=None):
    def fn(cur):
        cur["work"] = w
        if extra_fn:
            extra_fn(cur)
    return _patch(rec_id, fn)


def _log_entry_order(cur, ent):
    f = dict(cur.get("features") or {})
    rec = dict(f.get("option_entry") or {"v": VERSION, "mode": "live"})
    rec["orders"] = (list(rec.get("orders") or []) + [ent])[-12:]
    f["option_entry"] = rec
    cur["features"] = f


def _set_outcome(cur, words, **extra):
    f = dict(cur.get("features") or {})
    rec = f.get("option_entry")
    if isinstance(rec, dict):
        f["option_entry"] = dict(rec, outcome=words, **extra)
        cur["features"] = f


def _what(rec) -> str:
    try:
        return _AT()._what(rec)
    except Exception:
        return str((rec or {}).get("ticker") or "the option")


def _settled(order_id, wait_s=0.0):
    """The broker's own record of an order, waited for at most `wait_s` to reach a final state.
    Returns (order | None, status): order None = the look-up got no answer."""
    left = max(0.0, float(wait_s or 0))
    while True:
        g = _get_order(order_id)
        if not g.get("ok"):
            return None, None
        o = g["order"]
        st = str(o.get("status") or "").lower()
        if st == "filled" or st in _DEAD or left <= 1e-9:
            return o, st
        _sleep(min(0.4, left))
        left -= 0.4


def work_entry(rec_id, *, cfg=None, oc=None, now=None, clk=None) -> dict:
    """One look at one worked entry. A small state machine kept on the call (execution.work):

        working     an order is at the broker. Filled / dead -> done. Otherwise: wait `min_rest_s`, then raise the
                    price one rung (cancel -> cancelling), or, at the limit, apply the end rule.
        cancelling  a cancel was asked for. Nothing else happens until the broker's record of the order is final;
                    what filled meanwhile is read from that record. A fill of any size ends the schedule.
        sending     the next order was written down and sent, but its arrival is not confirmed: it is looked up by
                    its client order id (and sent again under the SAME id if the broker does not know it).
        done        nothing more to do (filled, part filled, resting at the limit, cancelled, taken back ...).

    Every state is written before the action that follows it, so a restart resumes at the same point."""
    AT = _AT()
    cfg = cfg if cfg is not None else AT._at_cfg()
    oc = _oc(oc if oc is not None else cfg)
    ec = oc["entry"]
    now = now or AT._now_et()
    rec = AT._load_rec(rec_id)
    e = ((rec or {}).get("extra") or {}).get("execution") or {}
    w = dict(e.get("work") or {})
    res = {"rec_id": rec_id}
    if not rec or w.get("state") not in _WORK_OPEN:
        return dict(res, action="nothing")
    sym = str(w.get("sym") or e.get("symbol") or "").upper()
    ticker = rec.get("ticker")
    now_s = _now_s()
    entry_live = live("entry", oc)

    def done(outcome, note=None, action="ended", **extra):
        w.update(state="done", outcome=outcome, ended_at=_nowiso(), cancel=None, pending=None)
        _save(rec_id, w, lambda cur: _set_outcome(cur, outcome, **extra))
        if note:
            _notify(note[0], note[1], ticker=ticker, rec_id=rec_id, severity=note[2] if len(note) > 2 else "info",
                    dedup=f"option_orders:{rec_id}:{action}")
        _activity("option_order_" + action, f"{_what(rec)}: {outcome}", ticker=ticker, rec_id=rec_id,
                  extra={"step": w.get("step"), "limit": w.get("limit"), "cap": w.get("cap")})
        return dict(res, action=action, outcome=outcome)

    if e.get("approver") != "auto":                        # only Mason's own automatic calls are ever worked
        return done("not an automatic call of Mason's: left alone")
    if e.get("state") not in ("approved_pending_fill", "placed"):
        return done(f"the call is now '{e.get('state')}': nothing left to work")
    clock_known = bool((clk or {}).get("ok"))
    is_open = bool(clock_known and (clk or {}).get("is_open"))
    just_cancelled = False
    # (review) a schedule belongs to the trading day it started on: on a later day (Mason was stopped in between)
    # nothing is sent or re-priced any more. Option orders are day orders, so the old one has lapsed by then.
    day0 = str(w.get("day") or w.get("started_at") or "")[:10]
    new_day = bool(day0) and day0 != now.date().isoformat()

    for _ in range(6):                                      # a few transitions per look at most
        st = w.get("state")

        # ── an order written down and sent whose arrival is not confirmed ──
        if st == "sending":
            pend = dict(w.get("pending") or {})
            o, how = _order_by_coid(pend.get("coid")) if pend.get("coid") else (None, "not_found")
            if how == "error":
                w["errors"] = int(w.get("errors") or 0) + 1
                _save(rec_id, w)
                return dict(res, action="waiting", detail="the broker did not say whether the new order arrived")
            if how == "ok":
                _adopt(rec_id, w, o, pend, None)
                continue
            if new_day:
                return done("the trading day ended before the next order was sent; nothing was bought")
            if not clock_known:                             # (a clock that cannot be read says nothing: wait)
                return dict(res, action="waiting", detail="the broker's clock could not be read")
            if not entry_live or e.get("close_requested") or e.get("entry_cancel_sent") or not is_open:
                return done("ended before the next order was sent" if is_open else "the market closed before the next "
                                                                                 "order was sent")
            if int(pend.get("tries") or 0) >= int(ec.get("max_send_tries") or 2):
                words = "the broker did not take the re-priced order"
                return done(words, _note_unfilled(rec, w, words, None, "refused", pend.get("error")), "unfilled")
            pend["tries"] = int(pend.get("tries") or 0) + 1
            w["pending"] = pend
            _save(rec_id, w)                               # written BEFORE the order goes out
            r = _place_buy(sym, int(pend["qty"]), float(pend["limit"]), pend["pitch"])
            if r.get("ok") and r.get("order_id"):
                o2, _st2 = _settled(r["order_id"], ec.get("fill_wait_s"))
                _adopt(rec_id, w, o2 or {"id": r["order_id"], "client_order_id": r.get("client_order_id"),
                                         "status": r.get("submitted_status"), "limit_price": r.get("limit_price")},
                       pend, r)
                continue
            sent = r.get("client_order_id")
            pend["error"] = str(r.get("error") or "no answer")[:200]
            if sent and sent != pend.get("coid"):          # the id really used (should be the same): look that one up
                pend["coid"] = sent
            w["pending"] = pend
            _save(rec_id, w)
            if sent:
                o, how = _order_by_coid(sent)
                if how == "ok":
                    _adopt(rec_id, w, o, pend, None)
                    continue
            return dict(res, action="waiting", detail="the new order is not confirmed yet: " + pend["error"][:120])

        # ── a cancel on its way ──
        if st == "cancelling":
            c = dict(w.get("cancel") or {})
            o, ost = _settled(c.get("order_id"), ec.get("confirm_wait_s") if just_cancelled else 0)
            just_cancelled = False
            if o is None:
                w["errors"] = int(w.get("errors") or 0) + 1
                _save(rec_id, w)
                return dict(res, action="waiting", detail="the broker did not answer about the order being cancelled")
            if ost != "filled" and ost not in _DEAD:        # still open: the cancel has not gone through
                if now_s - float(c.get("at_s") or now_s) >= float(ec.get("cancel_retry_s") or 150) \
                        and int(c.get("tries") or 1) < 4:
                    c.update(tries=int(c.get("tries") or 1) + 1, at_s=now_s)
                    w["cancel"] = c
                    _save(rec_id, w)
                    _cancel(c["order_id"])
                    just_cancelled = True
                    continue
                return dict(res, action="waiting", detail="waiting for the broker to confirm the cancel")
            fq = _f(o.get("filled_qty")) or 0.0
            if ost == "filled" or fq > 0:                   # the fill won the race: the call has its position
                w.update(state="done", cancel=None, pending=None, ended_at=_nowiso(),
                         outcome="filled" if ost == "filled" else "filled in part while its price was being changed; "
                                                                  "the rest was not bought")
                _save(rec_id, w)                            # (settle_pending books it this same look)
                return dict(res, action="filled" if ost == "filled" else "part_filled", order_id=o.get("id"))
            why = c.get("why")
            if why != "step":
                words = c.get("words") or "cancelled"
                return done(words, _note_unfilled(rec, w, words, c.get("ask"), why) if why in ("end", "invalid") else None,
                            "unfilled" if why in ("end", "invalid") else "ended")
            if not clock_known:
                return dict(res, action="waiting", detail="the broker's clock could not be read")
            if not entry_live or e.get("close_requested") or e.get("entry_cancel_sent") or not is_open or new_day:
                return done("ended between two prices; nothing was bought")
            step = int(c["next_step"])
            pitch = f"{AT._seed(rec_id)}-w{step}"
            qty = int(round(float(w.get("qty") or 0)))
            limit = float(c["next_limit"])
            coid = None
            try:
                coid = _EX()._client_order_id(sym, pitch, "buy", qty, limit, None, None)
            except Exception:
                pass
            w.update(state="sending", cancel=None,
                     pending={"step": step, "limit": limit, "qty": qty, "pitch": pitch, "coid": coid, "tries": 0,
                              "decided_s": now_s, "quote": c.get("quote")})
            _save(rec_id, w)                               # written BEFORE the order goes out
            continue

        # ── an order at the broker ──
        o, ost = _settled(w.get("order_id"), 0)
        if o is None:
            w["errors"] = int(w.get("errors") or 0) + 1
            _save(rec_id, w)
            return dict(res, action="waiting", detail="the broker did not answer about the order")
        fq = _f(o.get("filled_qty")) or 0.0
        if ost == "filled":
            w.update(state="done", outcome="filled", ended_at=_nowiso())
            _save(rec_id, w)
            return dict(res, action="filled", order_id=o.get("id"))
        if ost in _DEAD:
            if fq > 0:
                w.update(state="done", outcome="filled in part; the rest was not bought", ended_at=_nowiso())
                _save(rec_id, w)
                return dict(res, action="part_filled", order_id=o.get("id"))
            return done("the order ended at the broker without a fill (" + ost + ")")
        age, run = now_s - float(w.get("sent_s") or now_s), now_s - float(w.get("started_s") or now_s)

        def ask_cancel(why, words=None, **kw):
            w.update(state="cancelling", cancel=dict({"order_id": o.get("id"), "why": why, "words": words,
                                                      "at_s": now_s, "tries": 1}, **kw))
            _save(rec_id, w)                               # written BEFORE the cancel goes out
            _cancel(o.get("id"))

        if e.get("close_requested") or e.get("entry_cancel_sent"):
            # The agent loop cancels the order it knows when the director takes a call back. An order that is still
            # open here is one it did not know yet (it arrived while its answer was lost): cancelled here.
            ask_cancel("stop", "the director took the call back; the order was cancelled")
            just_cancelled = True
            continue
        if not entry_live:
            return done("entry stepping was switched off: the order is left as it is")
        if new_day:
            return done("a new trading day: the order is left as it is")
        if not is_open:
            return dict(res, action="hold", detail="the market is not open")
        rungs = [float(x) for x in (w.get("rungs") or [w.get("cap")])]
        step = int(w.get("step") or 0)
        last = step >= len(rungs) - 1
        over = run >= float(ec.get("max_run_s") or 1800)

        if fq > 0:                                          # part filled and still open: its price trades, so it keeps
            if not w.get("frozen"):                         # that price; nothing is re-priced any more
                w["frozen"] = True
                _save(rec_id, w)
            end_s = (len(rungs) - 1) * 180.0 + float(ec.get("rest_at_limit_s") or 170)
            if run < min(end_s, float(ec.get("max_run_s") or 1800)):
                return dict(res, action="hold", detail="part filled: the rest keeps its price until the schedule ends")
            ask_cancel("freeze_end", "filled in part; the rest was cancelled when the schedule ended")
            just_cancelled = True
            continue
        if w.get("same_day"):                               # an option that expires today: never past the last-entry time
            cut, _txt = AT.close_cutoff(cfg.get("zero_dte_last_entry_et"), "14:30", clk, now, cfg)
            if now.replace(second=0, microsecond=0) > cut:
                ask_cancel("end", "cancelled at the last time a same-day option may be bought, without a fill")
                just_cancelled = True
                continue
        if not last and not over:
            if age < float(ec.get("min_rest_s") or 120):
                return dict(res, action="hold", detail="resting")
            try:
                lim_spread = float(_MQ().qcfg(cfg).get("opt_entry_max_spread_pct") or 25.0)
            except Exception:
                lim_spread = 25.0
            q = _quote(sym, cfg, max_spread_pct=lim_spread)
            mid = _f(q.get("mid"))
            if not (q.get("ok") and q.get("two_sided") and mid and not q.get("legacy")):
                return dict(res, action="hold", detail="no usable price right now: the order stays where it is")
            stop, target = _f(w.get("stop")), _f(w.get("target"))
            if stop is not None and mid <= stop:
                ask_cancel("invalid", f"cancelled: the option's price fell to the call's stop ({stop:.2f}) before the "
                                      "order filled", ask=_f(q.get("ask")))
                just_cancelled = True
                continue
            if target is not None and mid >= target:
                ask_cancel("invalid", f"cancelled: the option's price rose to the call's target ({target:.2f}) before "
                                      "the order filled", ask=_f(q.get("ask")))
                just_cancelled = True
                continue
            far, _how = _MQ().buy_limit(q, cfg=cfg)        # never above the asking price, never above middle + band
            if not far:
                return dict(res, action="hold", detail="no usable price right now: the order stays where it is")
            new = min(rungs[step + 1], floor_tick(far, w.get("ppind"), w.get("underlying")), float(w["cap"]))
            if new <= float(w.get("limit") or 0) + 1e-9:    # sellers are at or under Mason's price already: the rung
                w.update(step=step + 1, sent_s=now_s)       # is counted, the order stays
                _save(rec_id, w)
                return dict(res, action="hold", detail="the market is at Mason's price: nothing to raise")
            ask_cancel("step", next_step=step + 1, next_limit=round(new, 2), quote=_qfacts(q))
            just_cancelled = True
            continue
        # at the limit (or out of time)
        if not over and age < float(ec.get("rest_at_limit_s") or 170):
            return dict(res, action="hold", detail="resting at the limit")
        if _end_rule(w, oc) == "rest" and not last and float(w.get("limit") or 0) < float(w["cap"]) - 1e-9:
            # (review) out of time BELOW the director's price (looks came late, or there was no usable price): the
            # order goes to the director's price and waits there until the close, which is where today's order is
            ask_cancel("step", next_step=len(rungs) - 1, next_limit=round(float(w["cap"]), 2))
            just_cancelled = True
            continue
        ask = None
        try:
            ask = _f(_quote(sym, cfg).get("ask"))
        except Exception:
            pass
        if _end_rule(w, oc) == "rest":
            at = float(w.get("limit") or w["cap"])
            words = (f"not filled at its limit {at:.2f}; the order stays there until the close" if last else
                     f"the schedule ran out of time at {at:.2f}; the order stays there until the close")
            note = None
            if len(rungs) > 1:                              # (one price only: the first note already said all this)
                note = (f"Still not filled: {_what(rec)} — waiting at {at:.2f} until the close",
                        _story(w, ask) + (f" {at:.2f} is {w.get('cap_basis') or 'its limit'}, so the" if last else " The")
                        + " order stays in the market at that price until the close. If it never fills, nothing is "
                          "bought.")
            return done(words, note, "resting")
        ask_cancel("end", (f"cancelled at its limit {float(w['cap']):.2f} without a fill" if last else
                           f"cancelled at {float(w.get('limit') or 0):.2f} when the schedule ran out of time"), ask=ask)
        just_cancelled = True
    return dict(res, action="waiting", detail="more steps than one look allows; continued at the next look")


def _adopt(rec_id, w, o, pend, placed_res):
    """The next order is at the broker: it becomes the call's order. One write."""
    oid = o.get("id")
    limit = _f(o.get("limit_price")) or _f(pend.get("limit"))
    ent = _order_entry(int(pend.get("step") or 0), limit, pend.get("qty"), oid,
                       o.get("client_order_id") or pend.get("coid"), None, **(pend.get("quote") or {}))
    w.update(state="working", step=int(pend.get("step") or 0), order_id=oid, limit=limit, sent_s=_now_s(),
             pending=None, cancel=None, errors=0)
    keep = recording()

    def fn(cur):
        cur["work"] = w
        cur["order_id"] = oid
        cur["client_order_id"] = o.get("client_order_id") or pend.get("coid")
        cur["limit_price"] = limit
        cur["fill_status"] = str(o.get("status") or "").lower() or None
        if keep:
            _log_entry_order(cur, ent)
    _patch(rec_id, fn)


def _story(w, ask) -> str:
    rungs = w.get("rungs") or []
    mins = max(1, int(round((_now_s() - float(w.get("started_s") or _now_s())) / 60.0)))
    s = (f"Mason raised its price from {float(w.get('start') or rungs[0]):.2f} to {float(w['cap']):.2f} in "
         f"{max(0, int(w.get('step') or 0))} step(s) over about {mins} minute(s) and nobody sold at that price.")
    if ask:
        s += f" Sellers were asking {ask:.2f} at the last look."
    return s


def _note_unfilled(rec, w, words, ask, why="end", error=None):
    """(title, body, severity) of the one inbox note for an entry that ended without a fill."""
    cap = float(w.get("cap") or 0)
    title = f"Not filled: {_what(rec)} — {words}"[:200]
    tail = " Nothing was bought. The call is still tracked and graded."
    if why == "invalid":
        body = ("The order had not filled yet, and buying now would be buying a trade whose exit has already been "
                "reached, so Mason cancelled it." + tail)
    elif why == "refused":
        body = ("Mason cancelled its order to raise the price, and the broker did not take the new one"
                + (f" ({str(error)[:160]})" if error else "") + "." + tail)
    else:
        body = (_story(w, ask) + f" Mason does not pay more than {cap:.2f} for this trade ("
                f"{w.get('cap_basis') or 'its limit'}), so the order was cancelled." + tail)
    return title, body, "warning"


# ── record-only: what the entry schedule would have done ─────────────────────────────────────────────────────

def _shadow_entry(rec_id, cfg, oc, now, clk) -> dict:
    """Record-only mode: follow one order for a few looks with the price the schedule WOULD have had out, and
    whether the sellers' price reached it. One read-only quote per look; nothing is sent or cancelled."""
    AT = _AT()
    rec = AT._load_rec(rec_id)
    e = ((rec or {}).get("extra") or {}).get("execution") or {}
    oe = dict(((e.get("features") or {}).get("option_entry")) or {})
    sh = dict(oe.get("shadow") or {})
    if sh.get("state") != "tracking":
        return {"rec_id": rec_id, "action": "nothing"}
    ec = oc["entry"]
    now_s = _now_s()
    rungs = [float(x) for x in (oe.get("rungs") or [])]
    looks = list(sh.get("looks") or [])
    sym = str(e.get("symbol") or "").upper()

    def end(words, **kw):
        sh.update(state="done", outcome=words, **kw)
        _patch(rec_id, lambda cur: _put_shadow(cur, sh))
        return {"rec_id": rec_id, "action": "shadow_done", "outcome": words}

    if not rungs or not sym:
        return end("nothing to follow")
    if e.get("state") not in ("approved_pending_fill", "placed", "closed"):
        return end(f"the call is now '{e.get('state')}'")
    if not (clk or {}).get("ok"):
        return {"rec_id": rec_id, "action": "shadow_wait"}
    if not (clk or {}).get("is_open"):
        return end("the market closed")
    if len(looks) >= int(oc.get("shadow_max_looks") or 8) or now_s - float(sh.get("started_s") or now_s) > float(
            ec.get("max_run_s") or 1800):
        return end("followed long enough without the sellers' price reaching it")
    step, limit = int(sh.get("step") or 0), float(sh.get("limit") or rungs[0])
    if now_s - float(sh.get("step_s") or now_s) < float(ec.get("min_rest_s") or 120):
        return {"rec_id": rec_id, "action": "shadow_wait"}
    try:
        lim_spread = float(_MQ().qcfg(cfg).get("opt_entry_max_spread_pct") or 25.0)
    except Exception:
        lim_spread = 25.0
    q = _record_only_read(lambda: _quote(sym, cfg, max_spread_pct=lim_spread))
    look = dict(_qfacts(q), at=_nowiso(), step=step, limit=limit)
    ask = _f(q.get("ask"))
    usable = bool(q.get("ok") and q.get("two_sided") and ask and not q.get("legacy"))
    if usable and ask <= limit + 1e-9:
        look["fill"] = True
        looks.append(look)
        sh["looks"] = looks
        real = _f(e.get("price"))
        px = limit                                          # (a resting order fills at its own price)
        return end(f"would have been filled at {px:.2f} on step {step + 1}"
                   + (f"; the order really sent was filled at {real:.2f}" if real else "; the order really sent has not "
                                                                                     "filled"),
                   would_fill_px=px, would_fill_step=step, real_fill_px=real,
                   saving_per_contract=round((real - px) * 100.0, 2) if real else None)
    look["fill"] = False
    if usable and step < len(rungs) - 1:
        far, _how = _MQ().buy_limit(q, cfg=cfg)
        if far:
            new = min(rungs[step + 1], floor_tick(far, oe.get("ppind"), oe.get("underlying")), float(oe.get("cap") or far))
            step += 1
            limit = max(limit, round(new, 2))
            look["next_limit"] = limit
            if ask <= limit + 1e-9:                        # the next price reaches the sellers: filled as it goes out
                looks.append(look)
                sh["looks"] = looks
                real = _f(e.get("price"))
                return end(f"would have been filled at {limit:.2f} on step {step + 1}"
                           + (f"; the order really sent was filled at {real:.2f}" if real else "; the order really sent "
                                                                                             "has not filled"),
                           would_fill_px=limit, would_fill_step=step, real_fill_px=real,
                           saving_per_contract=round((real - limit) * 100.0, 2) if real else None)
    elif usable:
        looks.append(look)
        sh["looks"] = looks
        rule = _end_rule(oe, oc)
        return end(f"would have reached its limit {float(oe.get('cap') or limit):.2f} without a fill and "
                   + ("stayed there until the close" if rule == "rest" else "been cancelled"), would_end=rule)
    looks.append(look)
    sh.update(step=step, limit=limit, step_s=now_s, looks=looks)
    _patch(rec_id, lambda cur: _put_shadow(cur, sh))
    return {"rec_id": rec_id, "action": "shadow_look", "step": step, "limit": limit}


def _put_shadow(cur, sh):
    f = dict(cur.get("features") or {})
    rec = f.get("option_entry")
    if isinstance(rec, dict):
        f["option_entry"] = dict(rec, shadow=sh)
        cur["features"] = f


# ══ B. exits ═════════════════════════════════════════════════════════════════════════════════════════════════

def _expiry_of(row):
    try:
        return _AL()._occ_expiry(row.get("occ"), None)
    except Exception:
        return None


_KEEP = object()


def _same_episode(xw, feat, now) -> bool:
    """(review) The last sale recorded on the trade belongs to the exit being priced now: sent the same trading day,
    and no stop or target was taken back since (a new decision starts a new sale from the top)."""
    try:
        return bool(xw) and str(xw.get("day") or "") == now.date().isoformat() \
            and int(xw.get("withdrawn") or 0) == int((feat or {}).get("exit_withdrawn") or 0)
    except Exception:
        return False


def _exit_plan(oc, cfg, row, q, reason, now, clk, xw, *, net=True, prev=_KEEP, prev_share=None) -> dict | None:
    """The price a worked sale would have out right now, or None when it cannot be worked out (then today's
    rule stands). `xw` = this exit's own state (orders sent so far, the last limit). net=False (review): the price
    is only recorded, so no look-up at the broker is made for it. `prev`: the price the sale may not go above
    (default: the last limit of the same reason in `xw`). `prev_share` (review): the step the sale had reached
    before another reason took over; the new ladder starts no higher than that step (a stop already at the bid
    hands over at the bid)."""
    if not q or q.get("legacy") or not q.get("ok") or q.get("kind") != "option":
        return None
    bid, mid = _f(q.get("bid")), _f(q.get("mid"))
    if not (bid and mid):
        return None
    sym = str(row.get("occ") or "").upper()
    rec_pp = None
    if not net:
        try:
            rec_pp = (_AL()._feat(row).get("option_entry") or {}).get("ppind")
        except Exception:
            rec_pp = None
    pp, und = _penny(sym, known=rec_pp if isinstance(rec_pp, bool) else None, net=net), _underlying(sym)
    hard = reason not in _SOFT
    rungs = exit_rungs(reason, oc)
    sent = int((xw or {}).get("orders") or 0)
    to_bid = by = None
    if hard:
        by = must_bid_by(reason, _expiry_of(row), now, clk, cfg, oc)
        to_bid = (by - now).total_seconds()
    k = exit_rung_index(len(rungs), sent, to_bid, oc["exit"].get("step_s"))
    share = rungs[k]
    if prev_share is not None and share > prev_share + 1e-9:     # (review) never a higher step than the sale had
        k = next((i for i, r in enumerate(rungs) if r <= prev_share + 1e-9), len(rungs) - 1)   # reached already
        share = rungs[k]
    if not q.get("two_sided") or q.get("wide"):            # a wide quote accepted for a must-do exit: the bid, as before
        share, k = 0.0, len(rungs) - 1
    if prev is _KEEP:
        prev = _f((xw or {}).get("limit")) if (xw or {}).get("reason") == reason else None
    limit = exit_price(bid, mid, share, prev, pp, und)
    words = "bid" if share <= 1e-9 or limit <= floor_tick(bid, pp, und) + 1e-9 else (
        "middle price" if share >= 1.0 - 1e-9 else f"{share * 100:.0f}% of the way from the bid to the middle price")
    return {"limit": limit, "basis": f"{words} (step {k + 1} of {len(rungs)})", "rung": k, "rungs": len(rungs),
            "share": share, "must_bid_by": by.isoformat(timespec="seconds") if by is not None else None}


def exit_hook(row, q, reason, limit, basis, cfg, now, clk) -> dict | None:
    """Called by the exit manager where it prices a sale of an option. None when nothing is switched on (or the
    row is not an option). Otherwise {"live": bool, "limit", "basis", "plan"}: with live False the caller keeps
    today's price and the plan is only recorded."""
    try:
        if not row.get("occ"):
            return None
        oc = ocfg(cfg)
        if not active(oc):
            return None
        feat = _AL()._feat(row)
        xw_all = feat.get("exit_work") or {}
        same = _same_episode(xw_all, feat, now)
        xw = xw_all if (same and xw_all.get("reason") == reason) else {}
        # (review) a sale's price never goes up within one exit, also when an exit that must happen (time stop, day
        # before expiry, director close) takes over from a stop or a target whose sale was already out
        prev = _f(xw_all.get("limit")) if same else None
        prev_share = _f(xw_all.get("share")) if (same and xw_all.get("reason") != reason) else None
        is_live = live("exit", oc)
        plan = _exit_plan(oc, cfg, row, q, reason, now, clk, xw, net=is_live, prev=prev, prev_share=prev_share)
        if plan is None:
            return {"live": False, "plan": None}
        return {"live": is_live, "limit": plan["limit"], "basis": plan["basis"], "plan": plan}
    except Exception as e:
        _warn("exit_hook", e)
        return None


def exit_step(row, o, st, cfg, now, clk) -> dict | None:
    """Called by the exit manager when one of Mason's own sales of an option is still open at the broker.
    None unless exit stepping is ON (then the old 5-minute re-price is not used for this row). Otherwise:
      {"order": None, "action": ...}   nothing to do this look (resting; no usable price; cancel not confirmed);
      {"order": <final order>}         the old order is finished at the broker (cancelled, or filled meanwhile):
                                       the caller books what it filled and prices the next order in this same look."""
    try:
        if not row.get("occ"):
            return None
        oc = ocfg(cfg)
        if not live("exit", oc):
            return None
        AL = _AL()
        xc = oc["exit"]
        feat = AL._feat(row)
        xw = dict(feat.get("exit_work") or {})
        reason = row.get("exit_pending_reason") or xw.get("reason") or "unknown"
        now_s = _now_s()
        sent_s = _f(xw.get("sent_s"))
        if sent_s is None:
            try:
                sent_s = datetime.fromisoformat(str(feat.get("exit_submitted_at"))).timestamp()
            except Exception:
                sent_s = now_s - 10_000.0
        hard = reason not in _SOFT
        if not hard and (feat.get("sap_wait") or {}).get("acted_at"):
            return None                                    # a sale the stuck-put rule started: priced by that rule
        by = None
        if hard:
            by = must_bid_by(reason, _expiry_of(row), now, clk, cfg, oc)
        late = by is not None and now >= by
        if now_s - sent_s < float(xc.get("min_rest_s") or 120) and not late:
            return {"order": None, "action": "exit_pending"}
        sym = str(row.get("occ")).upper()
        q = _AL()._quote(sym, True, cfg, relaxed=hard)
        cur = _f(o.get("limit_price")) or _f(xw.get("limit"))
        # this sale's own state; an order that went out before exit stepping was switched on has none: it counts as
        # the first order of the sale, at the price it rests at (so the next price can only be lower, never higher)
        mine = xw if xw.get("reason") == reason else {}
        mine = dict(mine, reason=reason, orders=max(1, int(mine.get("orders") or 1)),
                    limit=mine.get("limit") if mine.get("limit") is not None else cur,
                    day=now.date().isoformat(), withdrawn=int(feat.get("exit_withdrawn") or 0))   # (review) the sale out
        plan = _exit_plan(oc, cfg, row, q, reason, now, clk, mine)                              # now is this exit's
        if plan is None:                                    # no usable price: the order stays where it is
            return {"order": None, "action": "exit_pending"}
        # A stop or a target that is waiting was re-checked every five minutes before this change (cancel, look
        # again, send again or take it back). That stays: after `recheck_after_s` the order is cancelled even when
        # its price would not change, so the exit manager's own re-check runs before anything is sent again.
        recheck = (not hard) and now_s - float(xw.get("checked_s") or sent_s) >= float(AL._REPRICE_AFTER_S)
        if cur is not None and abs(plan["limit"] - cur) < 0.005 and not recheck:
            xw = dict(mine, orders=int(mine["orders"]) + 1, sent_s=now_s,       # the rung is counted, the order stays
                      started_s=xw.get("started_s") or sent_s, checked_s=xw.get("checked_s") or sent_s)
            row.update(AL._set_feat(row, exit_work=xw))
            return {"order": None, "action": "exit_pending"}
        if not xw.get("limit") and cur is not None:         # remember the old order's price for the next one
            row.update(AL._set_feat(row, exit_work=dict(mine, sent_s=sent_s, started_s=xw.get("started_s") or sent_s,
                                                        checked_s=xw.get("checked_s") or sent_s)))
        _cancel(o.get("id"))
        o2, st2 = _settled(o.get("id"), xc.get("confirm_wait_s"))
        if o2 is None or (st2 != "filled" and st2 not in _DEAD):
            return {"order": None, "action": "exit_repricing"}
        return {"order": o2}
    except Exception as e:
        _warn("exit_step", e)
        return None


def exit_recover(row, cfg, always=False) -> dict | None:
    """A sale of THIS trade (client order id 'mason-exit-<rec id>-...') that reached the broker just before Mason
    stopped, before its id could be written down. (review) The look-up itself lives in the exit manager
    (mason_agent_loop._lost_sale), which runs it before every sale of an option: with exit stepping ON, and by default
    (autotrade.exits.find_lost_sales), so that it also works with this module switched off or not installed. This
    name stays for any caller of the earlier version: with exit stepping ON (or `always`) it answers {"wait": True}
    when the broker's list cannot be read, else as mason_agent_loop._lost_sale."""
    try:
        if not row.get("occ") or not (always or live("exit", cfg)):
            return None
        return _AL()._lost_sale(row, wait_on_error=True)
    except Exception as e:
        _warn("exit_recover", e)
        return {"wait": True}


def exit_sent(row, q, limit, basis, reason, n, res, qty, wk) -> dict:
    """Called by the exit manager right after a sale was sent: what to add to the trade's features. {} when
    nothing is switched on. Keeps every order of the exit (not only the last), with the real prices it was
    sent on, and this exit's own state; in record-only mode also what the schedule would have asked."""
    try:
        if not row.get("occ"):
            return {}
        oc = ocfg()
        if not active(oc):
            return {}
        AL = _AL()
        feat = AL._feat(row)
        out = {}
        sub = _f((res or {}).get("limit_price")) or _f(limit)
        plan = (wk or {}).get("plan") if isinstance(wk, dict) else None
        is_live = bool(isinstance(wk, dict) and wk.get("live"))
        if recording(oc):
            ent = {"n": n, "at": _nowiso(), "reason": reason, "limit": sub, "basis": basis, "qty": _f(qty),
                   "order_id": (res or {}).get("order_id"), "worked": is_live}
            ent.update(_qfacts(q))
            if plan and not is_live and oc.get("shadow") is True:
                ent["would"] = {"limit": plan["limit"], "basis": plan["basis"]}
            if plan and plan.get("must_bid_by"):
                ent["must_bid_by"] = plan["must_bid_by"]
            out["exit_orders"] = (list(feat.get("exit_orders") or []) + [ent])[-30:]
        xw = dict(feat.get("exit_work") or {})
        now = _AT()._now_et()
        if xw.get("reason") != reason or not _same_episode(xw, feat, now):
            xw = {"reason": reason, "started_s": _now_s(), "orders": 0}
        xw.update(orders=int(xw.get("orders") or 0) + 1, limit=sub, sent_s=_now_s(), checked_s=_now_s(),
                  day=now.date().isoformat(), withdrawn=int(feat.get("exit_withdrawn") or 0))   # (review) its exit
        if plan and is_live:                               # (review) the step this order is on, so the next one (and a
            xw["orders"] = max(int(xw["orders"]), int(plan.get("rung") or 0) + 1)   # reason taking over) goes on
            xw["share"] = plan.get("share")                # from there
        else:
            xw.pop("share", None)
        out["exit_work"] = xw
        if plan and not is_live and oc.get("shadow") is True and not (feat.get("exit_shadow") or {}).get("state"):
            out["exit_shadow"] = {"state": "tracking", "reason": reason, "started_s": _now_s(), "step": plan["rung"],
                                  "step_s": _now_s(), "limit": plan["limit"], "rungs": plan["rungs"],
                                  "sold_at_limit": sub, "looks": []}
        return out
    except Exception as e:
        _warn("exit_sent", e)
        return {}


def exit_fill(row, fq, px, order_id, reason):
    """Called by the exit manager when a sale fill is booked: the fill next to the prices its order was sent on,
    and, once the whole position is sold, what the round trip cost against the middle price (on the trade and on
    the call). Returns the row (the same one when nothing is switched on)."""
    try:
        if not row.get("occ"):
            return row
        AL = _AL()
        feat = AL._feat(row)
        orders = list(feat.get("exit_orders") or [])
        if not orders or not recording():
            return row
        fq, px = _f(fq), _f(px)
        for ent in reversed(orders):
            if ent.get("order_id") == order_id:
                mid = _f(ent.get("mid"))
                fl = {"qty": fq, "px": px, "at": _nowiso()}
                if mid and px is not None:
                    fl.update({"got_under_mid": round(mid - px, 4), "got_under_mid_pct": round((mid - px) / mid * 100.0, 3),
                               "cost_vs_mid_usd": round((mid - px) * (fq or 0) * 100.0, 2)})
                ent["fill"] = fl
                break
        kv = {"exit_orders": orders}
        if AL._remaining(row) <= 1e-9:
            cost = _round_trip(feat, orders, row)
            if cost:
                kv["option_cost"] = cost
                try:
                    _patch(row["rec_id"], lambda cur: cur.__setitem__("option_cost", cost))
                except Exception:
                    pass
        return AL._set_feat(row, **kv)
    except Exception as e:
        _warn("exit_fill", e)
        return row


def _round_trip(feat, orders, row) -> dict | None:
    """Cost of the whole trade against the middle price at each order (audit L10): what was paid over the
    middle on the way in, what was received under it on the way out."""
    out = {"v": VERSION}
    ent = (feat.get("option_entry") or {}).get("fill") or {}
    eq = feat.get("entry_quote") or {}
    e_mid = _f(ent.get("mid_at_order")) or _f(eq.get("mid"))
    e_px = _f(row.get("entry_px"))
    if e_mid and e_px is not None:
        out.update({"entry_px": e_px, "entry_mid": e_mid, "entry_over_mid": round(e_px - e_mid, 4),
                    "entry_spread_pct": _f(eq.get("spread_pct"))})
    fills = [(x["fill"], x) for x in orders if isinstance(x.get("fill"), dict) and _f(x["fill"].get("qty"))]
    qsum = sum(_f(f["qty"]) for f, _x in fills)
    if fills and qsum:
        out["exit_px"] = round(sum(_f(f["qty"]) * _f(f["px"]) for f, _x in fills) / qsum, 4)
        mids = [(_f(f["qty"]), _f(x.get("mid"))) for f, x in fills if _f(x.get("mid"))]
        if mids:
            msum = sum(q for q, _m in mids)
            out["exit_mid"] = round(sum(q * m for q, m in mids) / msum, 4)
            out["exit_under_mid"] = round(out["exit_mid"] - out["exit_px"], 4)
        out["exit_orders"] = len(orders)
    if "entry_over_mid" in out and "exit_under_mid" in out:
        q = _f(row.get("qty")) or qsum
        out["round_trip_cost_usd"] = round((out["entry_over_mid"] + out["exit_under_mid"]) * q * 100.0, 2)
    return out if len(out) > 1 else None


def _shadow_exit(row, cfg, oc, now, clk) -> dict:
    """Record-only mode: after a sale went out the old way, follow the contract for a few looks with the price the
    schedule WOULD have asked, and whether the buyers' price reached it. One read-only quote per look."""
    AL = _AL()
    feat = AL._feat(row)
    sh = dict(feat.get("exit_shadow") or {})
    rid = row.get("rec_id")
    if sh.get("state") != "tracking":
        return {"rec_id": rid, "action": "nothing"}
    xc = oc["exit"]
    now_s = _now_s()
    looks = list(sh.get("looks") or [])

    def end(words, **kw):
        sh.update(state="done", outcome=words, **kw)
        AL._set_feat(row, exit_shadow=sh)
        return {"rec_id": rid, "action": "shadow_done", "outcome": words}

    if not (clk or {}).get("ok"):
        return {"rec_id": rid, "action": "shadow_wait"}
    if not (clk or {}).get("is_open"):
        return end("the market closed")
    if len(looks) >= int(oc.get("shadow_max_looks") or 8):
        return end("followed long enough without the buyers' price reaching it")
    if now_s - float(sh.get("step_s") or now_s) < float(xc.get("min_rest_s") or 120):
        return {"rec_id": rid, "action": "shadow_wait"}
    sym = str(row.get("occ")).upper()
    reason = sh.get("reason") or "unknown"
    q = _record_only_read(lambda: AL._quote(sym, True, cfg, relaxed=reason not in _SOFT))
    bid = _f(q.get("bid"))
    limit, step = float(sh.get("limit") or 0), int(sh.get("step") or 0)
    look = dict(_qfacts(q), at=_nowiso(), step=step, limit=limit)
    usable = bool(q.get("ok") and bid and not q.get("legacy"))
    sold = _f(row.get("exit_px")) or _f(sh.get("sold_at_limit"))
    if usable and bid >= limit - 1e-9:
        look["fill"] = True
        looks.append(look)
        sh["looks"] = looks
        px = limit                                          # (a resting order fills at its own price)
        return end(f"would have been sold at {px:.2f} on step {step + 1}" + (f"; it was really sold at {sold:.2f}" if sold else ""),
                   would_fill_px=px, would_fill_step=step, real_fill_px=sold,
                   gain_per_contract=round((px - sold) * 100.0, 2) if sold else None)
    look["fill"] = False
    looks.append(look)
    if usable:
        plan = _exit_plan(oc, cfg, row, q, reason, now, clk, {"reason": reason, "orders": step + 1, "limit": limit},
                          net=False)
        if plan:
            step = min(step + 1, int(sh.get("rungs") or 1) - 1)
            sh.update(step=step, limit=plan["limit"])
            if bid >= plan["limit"] - 1e-9:                 # the next price reaches the buyers: sold as it goes out
                look["next_limit"] = plan["limit"]
                sh["looks"] = looks
                return end(f"would have been sold at {plan['limit']:.2f} on step {step + 1}"
                           + (f"; it was really sold at {sold:.2f}" if sold else ""),
                           would_fill_px=plan["limit"], would_fill_step=step, real_fill_px=sold,
                           gain_per_contract=round((plan["limit"] - sold) * 100.0, 2) if sold else None)
    sh.update(step_s=now_s, looks=looks)
    AL._set_feat(row, exit_shadow=sh)
    return {"rec_id": rid, "action": "shadow_look", "step": sh["step"], "limit": sh["limit"]}


# ══ the stuck put ════════════════════════════════════════════════════════════════════════════════════════════

def stuck_put(row, reason, cfg, now, clk, ctx) -> tuple | None:
    """A put bought for a short call: the STOCK is through the call's stop (or target), the sale was decided, but
    the put has no price that passes the usual checks. Called by the exit manager at that point.

    Keeps the time this has lasted on the trade. After `after_min` minutes of market time on the same day it looks
    for a WIDER but still real quote: a real bid with size, buy and sell prices no more than `max_spread_pct`
    apart, no older than `max_age_s`, and a bid no lower than `min_share_of_exercise_value` of what the put is
    worth if exercised now. With the rule ON it returns (quote, limit, words) and the sale goes out, worked like
    an exit that must happen. With the rule off (record-only) it writes once what it would have sold at and
    returns None. It never returns a price without such a quote."""
    try:
        oc = ocfg(cfg)
        is_live = live("stuck_put", oc)
        if not (is_live or oc.get("shadow") is True) or not row.get("occ"):
            return None
        AL = _AL()
        sp = oc["stuck_put"]
        feat = AL._feat(row)
        w = dict(feat.get("sap_wait") or {})
        now_s = _now_s()
        today = now.date().isoformat()
        taken_back = int(feat.get("exit_withdrawn") or 0)   # how often a waiting stop/target of this trade was taken back
        if w.get("reason") != reason or w.get("day") != today or int(w.get("taken_back") or 0) != taken_back:
            w = {"reason": reason, "day": today, "since_s": now_s, "since": _nowiso(), "looks": 0,
                 "taken_back": taken_back}                   # (the stock came back in between: the clock starts again)
        w["looks"] = int(w.get("looks") or 0) + 1
        waited = now_s - float(w["since_s"])
        if waited < float(sp.get("after_min") or 60) * 60.0:
            row.update(AL._set_feat(row, sap_wait=w))
            return None
        sym = str(row.get("occ")).upper()
        if is_live:
            q = AL._quote(sym, True, cfg, relaxed=True)
        else:                                               # (review) a read for the record only
            q = _record_only_read(lambda: AL._quote(sym, True, cfg, relaxed=True))
        bid, ask, mid = _f(q.get("bid")), _f(q.get("ask")), _f(q.get("mid"))
        why = None
        if not (q.get("ok") and bid and ask and mid) or q.get("legacy"):
            why = "the put still has no real buy and sell price"
        else:
            spr = (ask - bid) / mid * 100.0
            age = _f(q.get("age_s"))
            if spr > float(sp.get("max_spread_pct") or 100.0) + 1e-9:
                why = f"its buy and sell prices are {spr:.0f}% apart ({bid:.2f} / {ask:.2f})"
            elif age is None or age > float(sp.get("max_age_s") or 900):
                why = "its price is too old"
            else:
                worth = _exercise_value(sym, (ctx or {}).get("underlying_quote"))
                share = float(sp.get("min_share_of_exercise_value") or 0.9)
                if worth is None:
                    why = "the stock's price is not known, so the put's price cannot be checked against it"
                elif bid < worth * share - 1e-9:
                    why = f"the bid {bid:.2f} is under what the put is worth if exercised now ({worth:.2f})"
        if why:
            w["last_refusal"] = why
            row.update(AL._set_feat(row, sap_wait=w))
            return None
        plan = _exit_plan(oc, cfg, row, dict(q, two_sided=True, wide=None), "time_stop", now, clk,
                          {"reason": "time_stop", "orders": int(w.get("orders") or 0), "limit": w.get("limit")},
                          net=is_live)
        limit = plan["limit"] if plan else floor_tick(bid, _penny(sym, net=is_live), _underlying(sym))
        words = (f"a wider quote accepted after {waited / 60.0:.0f} minutes without a usable price: "
                 + (plan["basis"] if plan else "bid"))
        if not is_live:
            if not w.get("would_sell"):
                w["would_sell"] = dict(_qfacts(q), at=_nowiso(), limit=limit, after_min=round(waited / 60.0, 1))
            row.update(AL._set_feat(row, sap_wait=w))
            return None
        w.update(orders=int(w.get("orders") or 0) + 1, limit=limit, acted_at=_nowiso())
        row.update(AL._set_feat(row, sap_wait=w))
        return q, limit, words
    except Exception as e:
        _warn("stuck_put", e)
        return None


def _exercise_value(sym, stock_quote):
    """What a put is worth if exercised now: strike minus the stock's price, never below zero. None when the
    stock's price is not known."""
    try:
        p = _MQ().parse_occ(sym)
        px = _f((stock_quote or {}).get("px"))
        if not p or px is None or not (stock_quote or {}).get("ok"):
            return None
        return max(0.0, p["strike"] - px) if p["type"] == "put" else max(0.0, px - p["strike"])
    except Exception:
        return None


# ══ a call stuck in 'placing' ════════════════════════════════════════════════════════════════════════════════

def recover_placing(*, cfg=None, oc=None) -> list:
    """A call whose state is 'placing' for longer than `after_s`: Mason stopped between claiming the call and
    writing down its order. For each such AUTOMATIC call, ask the broker for orders carrying the call's own
    client-order-id prefix (mason-dir-<rec id>-):
        one exists            -> adopt it (the same thing _place_claimed does when it finds one): the call gets
                                 the order's id and state, and a filled order is journaled by settle_pending;
        the broker lists none -> release the claim: state 'failed', nothing was placed, one inbox note;
        the broker did not answer -> nothing this look.
    Never places an order and never cancels one."""
    out = []
    try:
        AT = _AT()
        cfg = cfg if cfg is not None else AT._at_cfg()
        oc = _oc(oc if oc is not None else cfg)
        after = max(float(AT._PLACING_STALE_S), float(oc["placing_recovery"].get("after_s") or 600))
        for r in _rows("SELECT id FROM recommendations WHERE kind='director' AND "
                       "json_extract(extra_json,'$.execution.state')='placing' AND "
                       "json_extract(extra_json,'$.execution.approver')='auto'"):
            rid = r["id"]
            try:
                rec = AT._load_rec(rid)
                ex = rec.get("extra") or {}
                e = ex.get("execution") or {}
                age = (datetime.now() - datetime.fromisoformat(str(e.get("claimed_at")))).total_seconds()
                if age < after:
                    continue
                code, d = AT._get("/orders", params={"status": "all", "limit": 500, "nested": "true"})
                if code != 200 or not isinstance(d, list):
                    out.append({"rec_id": rid, "action": "waiting", "detail": "the broker's order list could not be read"})
                    continue
                pre = f"mason-{AT._seed(rid)}-"
                mine = [o for o in d if str(o.get("client_order_id") or "").startswith(pre)]
                if not mine and not _list_reaches_back(d, e.get("claimed_at")):
                    # (review, gap fixes) the broker's newest 500 orders do not go back to this call: its order may be
                    # older than the list, so "none" proves nothing. Nothing is released (the call stays 'placing',
                    # as with the switch off); written to the log once an hour.
                    _warn("placing_recovery", RuntimeError(f"call {rid}: the broker's list of its last 500 orders does "
                                                           "not reach back to this call; not released"))
                    out.append({"rec_id": rid, "action": "waiting",
                                "detail": "the broker's list of its last 500 orders does not reach back to this call"})
                    continue
                if not mine:
                    err = ("Mason stopped while this order was being sent. The broker has no order for this call, so "
                           "nothing was placed.")
                    AT._set_execution(rid, {"state": "failed", "error": err, "placing_recovered": "released"})
                    _notify(f"Not placed: {_what(rec)} — Mason was interrupted before the order went out",
                            err + " The call is still tracked and graded.", ticker=rec.get("ticker"), rec_id=rid,
                            severity="warning", dedup=f"option_orders:{rid}:placing_released")
                    out.append({"rec_id": rid, "action": "released"})
                    continue
                live_ = [o for o in mine if (_f(o.get("filled_qty")) or 0) > 0
                         or str(o.get("status") or "").lower() not in _DEAD]
                o = (live_ or mine)[0]
                st = str(o.get("status") or "").lower()
                tr = AT._placed_trade(ex)
                is_opt = bool(_EX()._is_option_symbol(str(o.get("symbol") or "")))
                fields = {"state": "placed" if st == "filled" else ("failed" if st in _DEAD and not (
                              _f(o.get("filled_qty")) or 0) else "approved_pending_fill"),
                          "order_id": o.get("id"), "client_order_id": o.get("client_order_id"),
                          "symbol": str(o.get("symbol") or "").upper(), "asset": "option" if is_opt else "stock",
                          "limit_price": _f(o.get("limit_price")),
                          "qty": _f(o.get("filled_qty")) if st in ("filled", "partially_filled") else _f(o.get("qty")),
                          "price": _f(o.get("filled_avg_price")), "placed_at": e.get("claimed_at"),
                          "adopted_existing_order": True, "placing_recovered": "adopted"}
                sapr = e.get("short_as_put")
                if not e.get("exit_rules") and is_opt and isinstance(sapr, dict) and sapr.get("converted") is True:
                    try:                                    # (review) a put bought for a short call: its exits are the
                        import mason_short_put as SP        # STOCK's levels, as when it is placed normally (the
                        fields["exit_rules"] = SP.exit_rules(sapr, None, "auto")   # premium stop/target stay empty)
                    except Exception:
                        fields["exit_rules"] = {"stop_premium": None, "target_premium": None, "enforced_at_broker": False,
                                                "basis": "underlying"}
                elif not e.get("exit_rules"):
                    fields["exit_rules"] = ({"stop_premium": _f(tr.get("stop")), "target_premium": _f(tr.get("target")),
                                             "enforced_at_broker": False} if is_opt else
                                            {"stop": _f(tr.get("stop")), "target": _f(tr.get("target")),
                                             "enforced_at_broker": bool(o.get("legs"))})
                AT._set_execution(rid, fields)
                if st == "filled" or (st in _DEAD and (_f(o.get("filled_qty")) or 0) > 0):
                    if fields["state"] != "placed":
                        AT._set_execution(rid, {"state": "approved_pending_fill"})
                    AT.settle_pending([rid], _force_journal=True)
                _notify(f"Found again: the order for {_what(rec)}",
                        "Mason stopped while this order was being sent and had no record of it. The order is at the "
                        f"broker ({st}); Mason has taken it back into its records.", ticker=rec.get("ticker"),
                        rec_id=rid, dedup=f"option_orders:{rid}:placing_adopted")
                out.append({"rec_id": rid, "action": "adopted", "order_id": o.get("id"), "status": st})
            except Exception as ex_:
                _warn("placing_recovery", ex_)
                out.append({"rec_id": rid, "error": f"{type(ex_).__name__}: {ex_}"})
    except Exception as e:
        _warn("placing_recovery", e)
        out.append({"error": f"{type(e).__name__}: {e}"})
    return out


def _list_reaches_back(orders, claimed_at, limit=500) -> bool:
    """(review, gap fixes) True when `orders` (the broker's newest orders, at most `limit` of them) is complete back to
    the moment the call was claimed: fewer than `limit` came back (that is all of them), or the oldest one listed was
    sent no later than the claim. Only then does "no order of this call in the list" mean that none went out.
    `claimed_at` is Mason's own local time. Never raises (False when it cannot be told)."""
    try:
        if len(orders) < limit:
            return True
        from datetime import timezone
        t0 = datetime.fromisoformat(str(claimed_at)[:19]).astimezone(timezone.utc)
        times = [_EX()._fill_time(str(o.get("submitted_at") or "")) for o in orders]
        times = [t for t in times if t is not None]
        return bool(times) and min(times) <= t0
    except Exception:
        return False


def status() -> dict:
    """For /api and the docs: the switches as they are now, and what is open."""
    try:
        oc = ocfg()
        return {"ok": True, "enabled": oc.get("enabled") is True, "shadow": oc.get("shadow") is True,
                "live": {p: live(p, oc) for p in ("entry", "exit", "stuck_put", "placing_recovery")},
                "filters_on": [n for n in FILTER_RULES if rule_live(n, oc)],
                "open_schedules": _work_ids(), "followed_record_only": _shadow_ids()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
