"""
mason_idle_cash.py — THE IDLE-CASH RULE (Nico, 2026-11-15): park cash that is not needed in ONE index fund
and take it back out as Mason trades. PAPER ONLY. Deterministic: no model call anywhere in this file.

What it does
------------
  One pass every idle_cash.interval_minutes while the market is open (called as one more step of
  mason_agent_loop.tick, i.e. on the thread that places the director's orders, so the two never interleave):

    reserve   = the larger of  reserve_pct % of the account  and  reserve_learning_trades x one learning trade
                (never less than three learning trades, whatever the settings say),
                plus what it would cost to exercise in-the-money call options that expire within
                reserve_expiring_calls_days (so an exercise does not push the account into borrowing)
    band      = the larger of  band_usd  and  band_pct % of the account
    free cash = the broker's cash minus the cost of buy orders that are still waiting to fill

    free cash above reserve + band   -> BUY the fund with the money above the reserve (whole shares, day limit order)
    free cash below reserve - band   -> SELL enough of the fund to bring cash back to the reserve
    anything in between              -> nothing (this is what stops it from trading back and forth)

  free_cash(need_usd): the synchronous step mason_autotrade.approve calls when an order is refused ONLY because
  cash is short. It sells enough of the fund for the order plus the reserve, waits up to free_cash_timeout_s
  for the fill and returns; approve then checks the order again. On a timeout the order stays refused, as today.
  (review fix) It may sell whenever the waiting trade itself may be placed: from autotrade.entry_open_delay_min
  after the open (09:35, when the morning queue goes out) until 2 minutes before the close.

What it never does
------------------
  * never a market order: buys through mason_execution.place_trade, sells through mason_execution.close_position,
    both day limit orders priced by mason_quotes.buy_limit / sell_limit from a fresh trusted quote;
  * never borrows: a buy only ever spends free cash above the reserve; the reserve is also the safety margin
    for a director order that is being placed at the same moment;
  * never sells or touches anything but its own parked shares: the quantity sold is at most what its own
    ledger (table park_ledger) says it bought and still holds, and at most what the broker says is free;
  * never trades outside regular hours, in the first open_wait_min minutes, in the last close_wait_min minutes
    (the close is the broker clock's real close: 13:00 on a half-day), or when the broker's close is not known;
  * more than max_buys_per_day / max_sells_per_day (1 / 1) a day, except a sale that a waiting trade needs or
    one made because cash has fallen under the price of one trade;
  * anything at all when something is unclear: a broker read failed or looks wrong, the ledger and the broker
    disagree, any open order exists on the fund, its own earlier order cannot be checked, there is no reliable
    price. Then it does nothing and says so once (log; an inbox note only for the cases Nico has to act on).
  The one thing it does besides buying and selling: an order of its OWN that is still unfilled after
  reprice_after_s seconds is cancelled, and the next pass decides afresh (never in record-only mode).

Ownership
---------
  The parked shares are Mason's own but they are NOT a director trade and NOT Nico's:
    * every order carries the client id prefix 'mason-park-' and is recorded in park_ledger (one row per order,
      with the cash, the reserve and the quote it was decided on);
    * parked_qty() / parked_map() / order_ids() are read by the few places that would otherwise take the shares
      for Nico's or for a director trade (journal sync, self-check, protection, book actions, the holdings text
      and the limits). With nothing in the ledger they all return "nothing" and those places behave as before.

Switches (mason.config.json -> "idle_cash"; read on every pass; every key optional)
----------------------------------------------------------------------------------
  enabled                      false   nothing in this file runs, the table is not even created
  shadow                       true    work out and record what it would do, place nothing
  fund                         "SPY"
  reserve_pct                  15      percent of the account kept as cash
  reserve_learning_trades      8       ... or this many learning trades, whichever is larger
  reserve_trade_usd            null    size of one learning trade (null = autotrade.tiers.learning.usd_max)
  reserve_expiring_calls_days  5       0 = do not add the exercise cost of expiring in-the-money calls
  band_usd / band_pct          1000 / 2
  interval_minutes             10
  open_wait_min / close_wait_min   10 / 10
  max_buys_per_day / max_sells_per_day   1 / 1
  max_buy_usd                  null    most dollars one purchase may park (null = all the spare cash at once)
  free_cash_for_trades         true    the synchronous step for a waiting trade
  free_cash_timeout_s          20
  wind_down                    false   true = buy nothing and sell everything that is parked (the way out)
  show_in_book                 false   model-visible wording about the parked money (needs Nico's approval)
  notify                       true    one inbox note per completed purchase or sale

Command line (never places, changes or cancels an order):
  python3 mason_idle_cash.py status            switches + the ledger (no broker call)
  python3 mason_idle_cash.py plan              what a pass would do this minute (broker reads only)
  python3 mason_idle_cash.py correct [--apply] after the note "the records and the broker disagree": one correcting
                                               row so the ledger matches the broker again

Never raises: every public function returns a dict / number / set.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta

log = logging.getLogger("mason.idle_cash")

PREFIX = "mason-park-"                     # client-order-id prefix of every order this module places
TABLE = "park_ledger"
_PAPER_HOST = "paper-api.alpaca.markets"
MIN_RESERVE_TRADES = 3                     # the reserve can never be set lower than this many learning trades

DEFAULTS = {
    "enabled": False,
    "shadow": True,
    "fund": "SPY",
    "reserve_pct": 15.0,
    "reserve_learning_trades": 8,
    "reserve_trade_usd": None,
    "reserve_expiring_calls_days": 5,
    "band_usd": 1000.0,
    "band_pct": 2.0,
    "interval_minutes": 10,
    "open_wait_min": 10,
    "close_wait_min": 10,
    "max_buys_per_day": 1,
    "max_sells_per_day": 1,
    "max_orders_per_day": 6,               # hard stop on orders of one side in a day, whatever the reason
    "max_buy_usd": None,
    "free_cash_for_trades": True,
    "free_cash_timeout_s": 20,
    "fill_wait_s": 8,                      # how long a scheduled pass waits for its own order to fill
    "reprice_after_s": 300,                # an own order still unfilled after this long is cancelled
    "books_wait_s": 5,                     # how long to wait for the journal lock before giving up this pass
    "wind_down": False,
    "show_in_book": False,
    "notify": True,
}

_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
    id TEXT PRIMARY KEY, kind TEXT, fund TEXT, side TEXT, qty REAL, limit_px REAL, reason TEXT,
    coid TEXT, order_id TEXT, state TEXT, filled_qty REAL DEFAULT 0, filled_px REAL, filled_at TEXT,
    day TEXT, created_at TEXT, updated_at TEXT, cash_before REAL, equity REAL, reserve REAL, band REAL,
    parked_before REAL, quote_json TEXT, note TEXT, notified INTEGER DEFAULT 0)"""

_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_STOCK = re.compile(r"^[A-Z][A-Z.]{0,5}$")
_CLOSED = {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}   # any other status = still open
_EPS = 1e-6

_LOCK = threading.RLock()                 # one pass / one free_cash at a time in this process
_state = {"last_pass": None, "last": None, "schema": set(), "once": {}}


# ── lazy modules + injectable I/O (tests replace these) ──────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _EX():
    import mason_execution
    return mason_execution


def _conn():
    import mason_db
    return mason_db.get_connection()


def _now_et() -> datetime:
    return _AT()._now_et()


def _mono() -> float:
    return time.monotonic()


def _sleep(s: float) -> None:
    time.sleep(s)


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _raw_cfg() -> dict:
    try:
        return _AT()._cfg() or {}
    except Exception:
        return {}


def cfg(raw: dict | None = None) -> dict:
    """The idle_cash block over the defaults. Unknown keys are kept, keys starting with '_' are comments."""
    out = dict(DEFAULTS)
    try:
        blk = (raw if raw is not None else _raw_cfg()).get("idle_cash") or {}
        if isinstance(blk, dict):
            out.update({k: v for k, v in blk.items() if not str(k).startswith("_")})
    except Exception:
        pass
    out["fund"] = str(out.get("fund") or "SPY").strip().upper()
    return out


def enabled(c: dict | None = None) -> bool:
    return (c or cfg()).get("enabled") is True


def live(c: dict | None = None) -> bool:
    """True only when the rule may really place orders: enabled AND shadow switched off explicitly."""
    c = c or cfg()
    return c.get("enabled") is True and c.get("shadow") is False


def _num(c, key) -> float:
    v = _f(c.get(key))
    return v if v is not None else float(DEFAULTS[key] or 0)


def _once(key: str, msg: str, level=logging.WARNING) -> bool:
    """Log `msg` once per calendar day per key. True when it was logged now."""
    k = f"{date.today().isoformat()}:{key}"
    if k in _state["once"]:
        return False
    if len(_state["once"]) > 500:
        _state["once"].clear()
    _state["once"][k] = True
    log.log(level, f"[idle-cash] {msg}")
    return True


def _notify(title, body, *, severity="info", dedup=None, ticker=None):
    try:
        import mason_notifications as MN
        MN.add_notification("system", title[:200], body=body, severity=severity, ticker=ticker,
                            ref_table=TABLE, created_by="idle_cash", dedup_key=dedup)
    except Exception:
        pass


def _activity(kind, title, *, ticker=None, extra=None, body=None):
    try:
        import mason_activity as MA
        MA.log_activity(kind, title[:200], body=body, actor="autotrade", category="analyst", ticker=ticker,
                        trigger="autonomous", ref_table=TABLE, extra=extra or {})
    except Exception:
        pass


# ── the ledger ───────────────────────────────────────────────────────────────────

def ensure_schema(force=False) -> dict:
    """Create park_ledger. Called only from an ENABLED pass, so installing the file creates nothing."""
    try:
        import mason_db
        key = str(getattr(mason_db, "DB_FILE", ""))
        if key in _state["schema"] and not force:
            return {"ok": True, "cached": True}
        conn = _conn()
        try:
            with conn:
                conn.execute(_SQL)
                conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_day ON {TABLE}(day, side)")
        finally:
            conn.close()
        _state["schema"].add(key)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"ensure_schema: {type(e).__name__}: {e}"}


def _rows(sql, args=()):
    """Rows as dicts. [] when the table does not exist yet. Raises on any other database error."""
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise
    finally:
        conn.close()


def _exec(sql, args=()):
    conn = _conn()
    try:
        with conn:
            conn.execute(sql, args)
    finally:
        conn.close()


def _set(row_id, **kv):
    kv["updated_at"] = _nowiso()
    cols = ", ".join(f"{k}=?" for k in kv)
    last = None
    for i in range(3):                     # the order is already at the broker: try hard to write it down
        try:
            _exec(f"UPDATE {TABLE} SET {cols} WHERE id=?", list(kv.values()) + [row_id])
            return True
        except Exception as e:
            last = e
            _sleep(0.2 * (i + 1))
    log.error(f"[idle-cash] could not update ledger row {row_id}: {type(last).__name__}: {last}")
    return False


def parked_map():
    """{FUND: shares this rule bought and still holds}. {} when nothing is parked (or the table does not exist).
    None when the ledger could not be read."""
    try:
        out = {}
        for r in _rows(f"SELECT fund, side, COALESCE(filled_qty,0) q FROM {TABLE} WHERE kind='order'"):
            q = float(r["q"] or 0)
            if q <= 0:
                continue
            sym = str(r["fund"] or "").upper()
            out[sym] = out.get(sym, 0.0) + (q if r["side"] == "buy" else -q)
        return {k: v for k, v in out.items() if v > _EPS}
    except Exception as e:
        _once("ledger_read", f"the ledger could not be read ({type(e).__name__}: {e})")
        return None


def owned_map():
    """(review fix) {FUND: shares to treat as this rule's own} for every place that decides WHOSE shares they are
    (protection, the director's sales of Nico's stock, the holdings text and the limits, the quick-check book, the
    self-check). Like parked_map(), plus the whole size of a purchase of its own that is not finished or not yet
    confirmed (the broker may have filled it since the last pass), and an unfinished sale only counted for what is
    known to have filled. So shares the rule bought are never, even for a few minutes, read as Nico's.
    {} when nothing is parked or working (or the table does not exist). None when the ledger could not be read."""
    try:
        out = {}
        for r in _rows(f"SELECT fund, side, state, COALESCE(qty,0) qty, COALESCE(filled_qty,0) q FROM {TABLE} "
                       f"WHERE kind='order'"):
            q = float(r["q"] or 0)
            if r["side"] == "buy" and r["state"] in ("placing", "working"):
                q = max(q, float(r["qty"] or 0))
            if q <= 0:
                continue
            sym = str(r["fund"] or "").upper()
            out[sym] = out.get(sym, 0.0) + (q if r["side"] == "buy" else -q)
        return {k: v for k, v in out.items() if v > _EPS}
    except Exception as e:
        _once("ledger_read", f"the ledger could not be read ({type(e).__name__}: {e})")
        return None


def parked_qty(symbol):
    """Shares of `symbol` this rule holds. 0.0 when none. None when the ledger could not be read."""
    m = parked_map()
    if m is None:
        return None
    return float(m.get(str(symbol or "").upper(), 0.0))


def order_ids():
    """Broker order ids of every order this rule placed (the journal sync never books them as Nico's or Mason's).
    set() when there are none (or the table does not exist). (review fix) None when the ledger exists but could not
    be read: the journal sync then books nothing at all that pass, instead of booking parked fills as Nico's."""
    try:
        return {r["order_id"] for r in _rows(f"SELECT order_id FROM {TABLE} WHERE kind='order' AND "
                                             f"order_id IS NOT NULL AND order_id<>''")}
    except Exception as e:
        _once("ledger_read_ids", f"the ledger could not be read for the journal sync ({type(e).__name__}: {e})")
        return None


def is_park_order(client_order_id) -> bool:
    return str(client_order_id or "").startswith(PREFIX)


def _day_counts(day: str, side: str) -> dict:
    """{'done': orders of `side` today that bought/sold something or are still working, 'all': every attempt}."""
    rows = _rows(f"SELECT state, COALESCE(filled_qty,0) q FROM {TABLE} WHERE kind='order' AND day=? AND side=?",
                 (day, side))
    return {"done": sum(1 for r in rows if r["q"] > 0 or r["state"] in ("placing", "working")), "all": len(rows)}


# ── broker reads (each one validated; a bad read = 'unclear') ────────────────────

def _account() -> dict:
    """The account, straight from the broker, with every number this rule relies on checked."""
    try:
        code, d = _AT()._get("/account")
    except Exception as e:
        return {"ok": False, "error": f"account read failed ({type(e).__name__})"}
    if code != 200 or not isinstance(d, dict):
        return {"ok": False, "error": f"account read failed (HTTP {code})"}
    cash, eq = _f(d.get("cash")), _f(d.get("equity"))
    if cash is None or eq is None or eq <= 0:
        return {"ok": False, "error": "the account answer has no usable cash or account value"}
    if d.get("trading_blocked") or d.get("account_blocked") or d.get("status") not in (None, "ACTIVE"):
        return {"ok": False, "error": f"the account is not tradable (status {d.get('status')})"}
    lmv, smv = _f(d.get("long_market_value")), _f(d.get("short_market_value"))
    if lmv is not None:                    # account value = cash + positions: a cash figure that breaks this is not used
        gap = abs(eq - cash - lmv - (smv or 0.0))
        if gap > max(50.0, 0.005 * eq):
            return {"ok": False, "error": "cash, positions and account value do not add up in the broker's answer"}
    return {"ok": True, "cash": cash, "equity": eq, "long_market_value": lmv,
            "options_buying_power": _f(d.get("options_buying_power")), "buying_power": _f(d.get("buying_power"))}


def _positions() -> dict:
    try:
        p = _AT()._positions()
    except Exception as e:
        return {"ok": False, "error": f"positions read failed ({type(e).__name__})"}
    if not (p or {}).get("ok") or not isinstance(p.get("positions"), list):
        return {"ok": False, "error": f"positions read failed ({(p or {}).get('error')})"}
    for x in p["positions"]:
        if not isinstance(x, dict) or not x.get("symbol") or _f(x.get("qty")) is None:
            return {"ok": False, "error": "the position list failed its checks"}
    return {"ok": True, "positions": p["positions"]}


def _open_orders() -> dict:
    try:
        code, d = _AT()._get("/orders", params={"status": "open", "limit": 500})
    except Exception as e:
        return {"ok": False, "error": f"open orders read failed ({type(e).__name__})"}
    if code != 200 or not isinstance(d, list):
        return {"ok": False, "error": f"open orders read failed (HTTP {code})"}
    flat = []
    for o in d:
        if not isinstance(o, dict) or not o.get("symbol") or not o.get("side"):
            return {"ok": False, "error": "the open-order list failed its checks"}
        flat.append(o)
        flat.extend(l for l in (o.get("legs") or []) if isinstance(l, dict) and l.get("symbol"))
    return {"ok": True, "orders": [o for o in flat if str(o.get("status") or "").lower() not in _CLOSED]}


def _clock() -> dict:
    try:
        return _AT()._clock() or {"ok": False}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}


def _quote(symbol) -> dict:
    AT = _AT()
    return AT._trusted_quote(symbol, False, AT._at_cfg())


def _limits(q) -> tuple:
    """(buy limit, sell limit) from mason_quotes for a fresh trusted quote; (None, None) when it cannot be used."""
    if not q or q.get("legacy") or not q.get("ok"):
        return None, None
    AT = _AT()
    MQ, ac = AT._MQ(), AT._at_cfg()
    b, _ = MQ.buy_limit(q, cfg=ac)
    s, _ = MQ.sell_limit(q, attempt=1, urgent=False, cfg=ac)
    return _f(b), _f(s)


def _get_order(order_id) -> dict:
    return _EX().get_order(order_id)


def _order_by_coid(coid):
    """(order | None, 'ok' | 'not_found' | 'error')."""
    try:
        code, d = _EX()._request("GET", "/orders:by_client_order_id", params={"client_order_id": coid})
    except Exception:
        return None, "error"
    if code == 200 and isinstance(d, dict) and d.get("id"):
        return d, "ok"
    return (None, "not_found") if code == 404 else (None, "error")


def _cancel(order_id) -> bool:
    try:
        code, _ = _EX()._request("DELETE", f"/orders/{order_id}")
        return code in (200, 204)
    except Exception:
        return False


def _paper_ok() -> bool:
    try:
        _, _, base, on = _EX()._creds()
        return bool(on) and _PAPER_HOST in (base or "")
    except Exception:
        return False


# ── the numbers ──────────────────────────────────────────────────────────────────

def _trade_usd(c, at_cfg) -> float:
    v = _f(c.get("reserve_trade_usd"))
    if v and v > 0:
        return v
    try:
        v = _f(((at_cfg.get("tiers") or {}).get("learning") or {}).get("usd_max"))
    except Exception:
        v = None
    return v if (v and v > 0) else 1250.0


def _mason_option_symbols() -> set:
    """OCC symbols of Mason's own open option trades (the loop sells those before they expire)."""
    try:
        conn = _conn()
        try:
            return {str(r[0]).upper() for r in conn.execute(
                "SELECT occ FROM agent_trades WHERE source='mason' AND status='open' AND occ IS NOT NULL "
                "AND COALESCE(record_deleted,0)=0").fetchall() if r[0]}
        finally:
            conn.close()
    except Exception:
        return set()


def exercise_reserve(positions, today: date, days, skip=None) -> tuple:
    """(dollars, [lines]) needed to pay for call options that would be exercised soon: long calls expiring within
    `days` calendar days that are in the money (priced at 1% of the strike or more). Puts bring cash in and are
    ignored; `skip` = symbols left out (Mason's own options, which the loop sells before expiry)."""
    try:
        n = int(days or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0, []
    total, lines = 0.0, []
    for p in positions or []:
        sym = str(p.get("symbol") or "").upper()
        m = _OCC.match(sym)
        if not m or m.group(5) != "C" or sym in (skip or ()):
            continue
        q = _f(p.get("qty")) or 0.0
        if q <= 0 or str(p.get("side") or "long").lower() != "long":
            continue
        try:
            exp = date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        left = (exp - today).days
        if left < 0 or left > n:
            continue
        strike = int(m.group(6)) / 1000.0
        px = _f(p.get("current_price")) or 0.0
        if strike <= 0 or px < 0.01 * strike:
            continue
        cost = strike * 100.0 * q
        total += cost
        lines.append(f"{q:g} {m.group(1)} {strike:g} call(s) expiring {exp.isoformat()}: ${cost:,.0f} to exercise")
    return total, lines


def reserve_usd(equity, c, at_cfg=None, positions=None, today=None, skip=None) -> dict:
    """{'reserve', 'base', 'exercise', 'band', 'trade_usd', 'lines'} for an account worth `equity`."""
    at_cfg = at_cfg if at_cfg is not None else {}
    trade = _trade_usd(c, at_cfg)
    # Whatever the settings say, the reserve is never less than MIN_RESERVE_TRADES learning trades: it is also
    # the safety margin for an order of the director's or of Nico's that goes out while this rule is buying.
    base = max(float(equity) * _num(c, "reserve_pct") / 100.0, _num(c, "reserve_learning_trades") * trade,
               MIN_RESERVE_TRADES * trade)
    ex, lines = exercise_reserve(positions, today or date.today(), c.get("reserve_expiring_calls_days"), skip)
    band = max(_num(c, "band_usd"), float(equity) * _num(c, "band_pct") / 100.0)
    return {"reserve": base + ex, "base": base, "exercise": ex, "band": band, "trade_usd": trade, "lines": lines}


def decide(*, cash_free, reserve, band, parked_qty, avail_qty, buy_limit, sell_limit, buys_today=0, sells_today=0,
           max_buys=1, max_sells=1, urgent_floor=0.0, wind_down=False, max_buy_usd=None) -> dict:
    """PURE. What one pass does with these numbers: {'action': 'buy' | 'sell' | 'none', 'qty', 'usd', 'why', 'urgent'}.
    Never returns a sale larger than min(parked_qty, avail_qty) nor a purchase that leaves less than the reserve."""
    out = {"action": "none", "qty": 0, "usd": 0.0, "why": "", "urgent": False}
    sellable = int(math.floor(max(0.0, min(float(parked_qty or 0), float(avail_qty or 0))) + 1e-9))
    excess = float(cash_free) - float(reserve)
    if wind_down:
        if sellable < 1:
            out["why"] = "winding down: nothing is parked"
            return out
        if not sell_limit:
            out["why"] = "winding down: no price"
            return out
        out.update(action="sell", qty=sellable, usd=sellable * sell_limit, why="winding down: selling everything parked",
                   urgent=True)
        return out
    urgent = float(cash_free) < float(urgent_floor)
    if excess < -float(band) or (urgent and excess < 0):
        if sellable < 1:
            out["why"] = "cash is under the reserve and nothing is parked that could be sold"
            return out
        if not sell_limit:
            out["why"] = "cash is under the reserve but there is no price to sell at"
            return out
        if sells_today >= max_sells and not urgent:
            out["why"] = (f"cash is under the reserve, but the fund was already sold {sells_today} time(s) today and "
                          "cash still covers a trade")
            return out
        qty = min(sellable, int(math.ceil((-excess) / sell_limit - 1e-9)))
        if qty < 1:
            out["why"] = "the shortfall is smaller than one share"
            return out
        out.update(action="sell", qty=qty, usd=qty * sell_limit, urgent=urgent,
                   why=("cash is under the price of one trade" if urgent else "cash is under the reserve"))
        return out
    if excess > float(band):
        if not buy_limit:
            out["why"] = "spare cash, but there is no price to buy at"
            return out
        if buys_today >= max_buys:
            out["why"] = f"spare cash, but the fund was already bought {buys_today} time(s) today"
            return out
        usd = excess if max_buy_usd in (None, "") or _f(max_buy_usd) is None else min(excess, float(max_buy_usd))
        qty = int(math.floor(usd / buy_limit + 1e-9))
        if qty < 1:
            out["why"] = "one share costs more than the spare cash"
            return out
        out.update(action="buy", qty=qty, usd=qty * buy_limit, why="cash above the reserve")
        return out
    out["why"] = "cash is inside the band around the reserve"
    return out


def _entry_delay_min() -> float:
    """autotrade.entry_open_delay_min: minutes after the open before a director entry may be placed (default 5)."""
    try:
        v = _f(_AT()._at_cfg().get("entry_open_delay_min"))
    except Exception:
        v = None
    return max(0.0, v) if v is not None else 5.0


def window(now: datetime, clk: dict, c: dict, for_trade: bool = False) -> tuple:
    """(True, '') when the fund may be traded at `now`; else (False, plain reason). Regular session only, not in
    the first open_wait_min minutes, not in the last close_wait_min minutes of the broker's REAL close.
    (review fix) for_trade: a sale for a trade that is waiting for the money (free_cash) may happen whenever that
    trade itself may be placed: from autotrade.entry_open_delay_min after the open (09:35 today, when the morning
    queue is placed) until 2 minutes before the close. The scheduled passes keep their own limits (09:40 / 15:50)."""
    if not (clk or {}).get("ok"):
        return False, "the market clock could not be read"
    if not clk.get("is_open"):
        return False, "the market is closed"
    try:
        close = _AT()._session_close(clk, now)
    except Exception:
        close = None
    if close is None:
        return False, "the broker's clock did not give today's closing time"
    if now.tzinfo is None:
        close = close.replace(tzinfo=None)
    elif close.tzinfo is None:
        close = close.replace(tzinfo=now.tzinfo)
    open_wait, close_wait = _num(c, "open_wait_min"), _num(c, "close_wait_min")
    if for_trade:
        open_wait, close_wait = min(open_wait, _entry_delay_min()), min(close_wait, 2.0)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=open_wait)
    end = close - timedelta(minutes=close_wait)
    if now < start:
        return False, f"too soon after the open (waits until {start.strftime('%H:%M')} ET)"
    if now > end:
        return False, f"too close to the close (stops at {end.strftime('%H:%M')} ET)"
    return True, ""


# ── reading everything a pass needs ──────────────────────────────────────────────

def _skip(why, *, unclear=False, key=None, note=None, quiet=False) -> dict:
    """The answer of a pass that does nothing. `unclear` = something is not as it should be: said once a day in
    the log, plus ONE inbox note a day when `note` (title, body) is given. `quiet` (read-only views): say nothing."""
    if unclear and not quiet:
        _once(key or why[:60], f"nothing done: {why}")
        if note:
            _notify(note[0], note[1], severity="warn",
                    dedup=f"idle_cash:{key or 'unclear'}:{date.today().isoformat()}")
    return {"ok": not unclear, "action": "none", "skipped": why, "unclear": bool(unclear)}


def _own_orders_settled(c, act: bool):
    """Bring the ledger's unfinished orders up to date from the broker. Returns (True, None) when none of this
    rule's orders is still open, (False, why) when one is still working or could not be checked."""
    rows = _rows(f"SELECT * FROM {TABLE} WHERE kind='order' AND state IN ('placing','working') ORDER BY created_at")
    busy = None
    for r in rows:
        o = None
        if r.get("order_id"):
            g = _get_order(r["order_id"])
            if g.get("ok") and isinstance(g.get("order"), dict):
                o = g["order"]
            elif g.get("status") == 404:
                if act:
                    _set(r["id"], state="failed", note="the broker does not know this order")
                continue
            else:
                return False, "one of its own orders could not be checked at the broker"
        else:
            try:
                age = (datetime.now() - datetime.fromisoformat(str(r.get("created_at")))).total_seconds()
            except Exception:
                age = 1e9
            o, how = _order_by_coid(r.get("coid"))
            if how == "error":
                return False, "one of its own orders could not be checked at the broker"
            if how == "not_found":
                if age < 120:
                    return False, "one of its own orders is being sent right now"
                if act:
                    _set(r["id"], state="failed", note="never reached the broker")
                continue
            if act:
                _set(r["id"], order_id=o.get("id"))
            r["order_id"] = o.get("id")
        st = str(o.get("status") or "").lower()
        fq, px = _f(o.get("filled_qty")) or 0.0, _f(o.get("filled_avg_price"))
        if st in _CLOSED:
            if act:
                _set(r["id"], state="done", filled_qty=fq, filled_px=px, filled_at=o.get("filled_at"),
                     note=("filled" if st == "filled" else f"{st} at the broker"))
                _after_fill(dict(r, filled_qty=fq, filled_px=px), c)
            continue
        if act:
            _set(r["id"], state="working", filled_qty=fq, filled_px=px)
        try:
            age = (datetime.now() - datetime.fromisoformat(str(r.get("created_at")))).total_seconds()
        except Exception:
            age = 0.0
        if act and live(c) and age > _num(c, "reprice_after_s") and r.get("order_id"):
            _cancel(r["order_id"])                  # it is decided afresh once the broker confirms the cancel
                                                    # (never in record-only mode: that mode sends nothing at all)
        busy = "its own earlier order is still open at the broker"
    return (False, busy) if busy else (True, None)


def read_state(c: dict, now: datetime, *, act: bool = True, for_trade: bool = False) -> dict:
    """Everything a pass decides on, or {'skip': {...}} when nothing may be done. `act` False = read only (the
    ledger is not touched: used by plan()). `for_trade` = the hours of a sale for a waiting trade (see window)."""
    fund = c["fund"]

    def sk(why, **kw):
        return {"skip": _skip(why, quiet=not act, **kw)}

    if not _STOCK.match(fund):
        return sk(f"the fund setting {fund!r} is not a stock symbol", unclear=True, key="fund")
    if not _paper_ok():
        return sk("paper trading is off or the broker address is not the paper account", unclear=True, key="paper")
    # Its own unfinished orders are brought up to date FIRST, at any hour, so the ledger (and with it who the
    # shares belong to) is right even when an order finished after the last pass of the day.
    try:
        settled, busy = _own_orders_settled(c, act)
        pm = parked_map()
    except Exception as e:
        return sk(f"the ledger could not be read ({type(e).__name__})", unclear=True, key="ledger")
    if pm is None:
        return sk("the ledger could not be read", unclear=True, key="ledger")
    if now.weekday() >= 5 or not ((9, 30) <= (now.hour, now.minute) < (16, 0)):
        return sk("the market is closed")                # no broker call outside 09:30-16:00 ET on weekdays
    clk = _clock()
    ok, why = window(now, clk, c, for_trade)
    if not ok:
        return sk(why, unclear=(not clk.get("ok") or "closing time" in why), key="clock")
    if not settled:
        return sk(busy, unclear=True, key="own_order")
    other = sorted(k for k in pm if k != fund)
    if other:
        # shares of another fund are still parked (the setting was changed): keep working with that fund
        fund = other[0]
        _once("fund_changed", f"the setting says {c['fund']} but {pm[fund]:g} {fund} are still parked; "
                              f"{fund} stays the fund until those are sold (idle_cash.wind_down sells them)",
              logging.INFO)
    acct = _account()
    if not acct.get("ok"):
        return sk(acct.get("error"), unclear=True, key="account")
    pos = _positions()
    if not pos.get("ok"):
        return sk(pos.get("error"), unclear=True, key="positions")
    od = _open_orders()
    if not od.get("ok"):
        return sk(od.get("error"), unclear=True, key="orders")
    commit = 0.0
    for o in od["orders"]:
        sym = str(o.get("symbol") or "").upper()
        if sym == fund:
            who = "its own" if is_park_order(o.get("client_order_id")) else "another"
            return sk(f"{who} order is open on {fund} at the broker", unclear=True, key="fund_order")
        if str(o.get("side") or "").lower() != "buy":
            continue
        rem = (_f(o.get("qty")) or 0.0) - (_f(o.get("filled_qty")) or 0.0)
        px = _f(o.get("limit_price")) or _f(o.get("stop_price"))
        if _f(o.get("notional")) and not _f(o.get("qty")):
            commit += float(o["notional"])
            continue
        if rem <= 0:
            continue
        if not px:
            return sk(f"an open buy order on {sym} has no price, so the cash it needs is not known",
                      unclear=True, key="buy_no_price")
        commit += rem * px * (100.0 if _OCC.match(sym) else 1.0)
    parked = float(pm.get(fund, 0.0))
    fp = None
    for p in pos["positions"]:
        if str(p.get("symbol") or "").upper() == fund and not _OCC.match(fund):
            fp = p
            break
    held = _f(fp.get("qty")) if fp else 0.0
    if fp and str(fp.get("side") or "long").lower() != "long":
        return sk(f"the account is short {fund}", unclear=True, key="short")
    if (held or 0.0) + _EPS < parked:
        return sk(
            f"the ledger says {parked:g} {fund} are parked but the broker holds {held or 0:g}", unclear=True, key="mismatch",
            note=("Parked cash: the records and the broker disagree",
                  f"Mason's record of parked cash says it holds {parked:g} shares of {fund}, but the broker shows "
                  f"{held or 0:g}. Mason will not buy or sell the fund until this is sorted out. If the shares were "
                  f"sold by hand, run this once in the Mason folder to make the record match the broker: "
                  f"python3 mason_idle_cash.py correct --apply"))
    avail = _f(fp.get("qty_available")) if fp else 0.0
    if avail is None:
        avail = held or 0.0
    AT = _AT()
    at_cfg = AT._at_cfg()
    rs = reserve_usd(acct["equity"], c, at_cfg, pos["positions"], now.date(), skip=_mason_option_symbols())
    day = now.date().isoformat()
    return {"fund": fund, "now": now, "day": day, "account": acct, "cash": acct["cash"],
            "cash_free": acct["cash"] - commit, "open_buy_usd": commit, "equity": acct["equity"],
            "parked": parked, "held": held or 0.0, "avail": avail, "px_hint": _f(fp.get("current_price")) if fp else None,
            "reserve": rs["reserve"], "band": rs["band"], "reserve_detail": rs, "trade_usd": rs["trade_usd"],
            "buys": _day_counts(day, "buy"), "sells": _day_counts(day, "sell")}


def _plan(st: dict, c: dict, *, quote=None) -> dict:
    """decide() on the state `st`. A quote is asked for only when the rough answer (broker's last price) is to act."""
    kw = dict(cash_free=st["cash_free"], reserve=st["reserve"], band=st["band"], parked_qty=st["parked"],
              avail_qty=st["avail"], buys_today=st["buys"]["done"], sells_today=st["sells"]["done"],
              max_buys=int(_num(c, "max_buys_per_day")), max_sells=int(_num(c, "max_sells_per_day")),
              urgent_floor=st["trade_usd"], wind_down=c.get("wind_down") is True, max_buy_usd=c.get("max_buy_usd"))
    hint = st.get("px_hint") or 1.0
    rough = decide(buy_limit=hint, sell_limit=hint, **kw)
    if rough["action"] == "none":
        return rough
    q = quote if quote is not None else _quote(st["fund"])
    b, s = _limits(q)
    if (rough["action"] == "buy" and not b) or (rough["action"] == "sell" and not s):
        why = str((q or {}).get("reason") or "no reliable price right now")[:160]
        if (q or {}).get("legacy"):
            why = "trusted prices are switched off (autotrade.quotes.enabled)"
        return {"action": "none", "qty": 0, "usd": 0.0, "urgent": False, "why": f"no price for {st['fund']}: {why}",
                "no_price": True}
    out = decide(buy_limit=b, sell_limit=s, **kw)
    out["limit"] = b if out["action"] == "buy" else (s if out["action"] == "sell" else None)
    out["quote"] = q
    cap = int(_num(c, "max_orders_per_day"))
    if out["action"] != "none" and st[out["action"] + "s"]["all"] >= cap:
        return {"action": "none", "qty": 0, "usd": 0.0, "urgent": False,
                "why": f"{cap} {out['action']} orders were already sent today; no more until tomorrow", "capped": True}
    return out


# ── placing ──────────────────────────────────────────────────────────────────────

def _money(v) -> str:
    return f"${float(v):,.0f}"


def _after_fill(r: dict, c: dict) -> None:
    """ONE inbox note + one activity row for an order that bought or sold something. Idempotent per ledger row."""
    try:
        fq, px = float(r.get("filled_qty") or 0), _f(r.get("filled_px"))
        if fq <= 0 or not px or r.get("notified"):
            return
        if not _set(r["id"], notified=1):
            return
        fund, usd = r["fund"], fq * px
        left = None
        try:
            a = _account()
            left = a.get("cash") if a.get("ok") else None
        except Exception:
            left = None
        cash_line = (f" Cash in the account now: {_money(left)} (the reserve Mason keeps is {_money(r.get('reserve') or 0)})."
                     if left is not None else "")
        if r["side"] == "buy":
            title = f"Parked {_money(usd)} of spare cash in the S&P 500 fund ({fq:g} {fund} at {px:,.2f})"
            body = (f"Mason bought {fq:g} shares of {fund} at ${px:,.2f} with cash that no trade needed.{cash_line} "
                    "This is not one of the director's trades and it is not counted in Mason's results; Mason sells "
                    "it again by itself when a trade needs the money.")
        else:
            why = {"trade": "a trade was waiting for the money",
                   "wind_down": "the rule is being wound down (idle_cash.wind_down)",
                   "urgent": "cash had fallen under the price of one trade"}.get(
                       str(r.get("reason") or ""), "cash had fallen under the reserve")
            title = f"Took {_money(usd)} out of the S&P 500 fund ({fq:g} {fund} sold at {px:,.2f})"
            body = (f"Mason sold {fq:g} of its parked shares of {fund} at ${px:,.2f} because {why}.{cash_line} "
                    "Only shares Mason had parked itself were sold.")
        if fund != "SPY":
            title = title.replace("the S&P 500 fund", f"the index fund {fund}")
        if c.get("notify", True) is not False:
            _notify(title, body, ticker=fund, dedup=f"idle_cash:{r['id']}")
        _activity("idle_cash_" + r["side"], title, ticker=fund, body=body,
                  extra={"ledger_id": r["id"], "order_id": r.get("order_id"), "qty": fq, "px": px,
                         "reason": r.get("reason")})
    except Exception as e:
        log.warning(f"[idle-cash] note for ledger row {r.get('id')} failed: {type(e).__name__}: {e}")


def _poll_fill(order_id, wait_s: float) -> dict:
    """Wait up to `wait_s` seconds for the order to finish. {'status', 'filled_qty', 'filled_px', 'final', 'waited_s'}."""
    t0 = _mono()
    last = {"status": "unknown", "filled_qty": 0.0, "filled_px": None, "final": False, "filled_at": None}
    while True:
        g = _get_order(order_id)
        if g.get("ok") and isinstance(g.get("order"), dict):
            o = g["order"]
            st = str(o.get("status") or "").lower()
            last = {"status": st, "filled_qty": _f(o.get("filled_qty")) or 0.0, "filled_px": _f(o.get("filled_avg_price")),
                    "final": st in _CLOSED, "filled_at": o.get("filled_at")}
            if last["final"]:
                break
        if _mono() - t0 >= wait_s:
            break
        _sleep(min(1.0, max(0.05, wait_s / 10.0)))
    last["waited_s"] = round(_mono() - t0, 2)
    return last


def _place(st: dict, pl: dict, c: dict, reason: str, wait_s: float) -> dict:
    """Send ONE day limit order for the plan `pl` and record it. Everything is re-checked by the helpers it uses
    (place_trade: buying power; close_position: the live position, never more than is free)."""
    EX = _EX()
    fund, side, qty, limit = st["fund"], pl["action"], int(pl["qty"]), float(pl["limit"])
    if qty < 1 or limit <= 0:
        return {"ok": False, "error": "nothing to place"}
    if side == "buy" and (qty * limit > st["cash_free"] - st["reserve"] + 1e-6 or st["cash_free"] <= 0):
        return {"ok": False, "error": "refused: this purchase would leave less cash than the reserve"}
    if side == "sell" and qty > min(st["parked"], st["avail"]) + _EPS:
        return {"ok": False, "error": "refused: this sale is larger than what is parked and free"}
    rid = uuid.uuid4().hex[:10]
    pitch = f"park-{rid}"
    try:
        coid = EX._client_order_id(fund, pitch, side, float(qty) if side == "sell" else qty, limit, None, None)
    except Exception:
        coid = None
    quote_json = None
    try:
        quote_json = json.dumps(_AT()._MQ().compact(pl.get("quote"), limit=limit), default=str)
    except Exception:
        pass
    with EX.books_lock(_num(c, "books_wait_s")) as got:
        if not got:
            return {"ok": False, "error": "the journal is being updated right now; tried again at the next pass",
                    "busy": True}
        _exec(f"INSERT INTO {TABLE}(id,kind,fund,side,qty,limit_px,reason,coid,state,filled_qty,day,created_at,"
              f"updated_at,cash_before,equity,reserve,band,parked_before,quote_json) "
              f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "order", fund, side, qty, limit, reason, coid, "placing", 0.0, st["day"], _nowiso(), _nowiso(),
               st["cash"], st["equity"], st["reserve"], st["band"], st["parked"], quote_json))
        try:
            if side == "buy":
                res = EX.place_trade(symbol=fund, qty=qty, side="buy", entry=limit, setup="idle_cash", pitch_id=pitch,
                                     order_type="limit", tif="day", force_single=True, respect_regime=False,
                                     verify=False)
            else:
                res = EX.close_position(fund, qty=qty, entry=limit, tif="day", pitch_id=pitch, verify=False)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not res.get("ok") or not res.get("order_id"):
            err = str(res.get("error") or "the broker did not take the order")[:300]
            sent = res.get("client_order_id")       # given only when the order itself was sent to the broker
            if sent:
                # (review fix) an error answer or no answer at all (a time-out) does not prove the broker did not take
                # the order: ask it by the client id, still under the books lock, before anything is written off.
                o, how = _order_by_coid(sent)
                if how == "ok":
                    res = {"ok": True, "order_id": o.get("id"), "client_order_id": o.get("client_order_id") or sent,
                           "qty": o.get("qty")}
                else:                               # not (yet) known there: the next pass settles it by its client id
                    _set(rid, state="placing", note=err, coid=sent)
                    _once(f"order_unconfirmed:{side}", f"the {side} order for {qty} {fund} got no usable answer "
                                                       f"({err}); it is looked up again at the next pass")
                    return {"ok": False, "error": err, "ledger_id": rid, "unconfirmed": True}
            else:
                _set(rid, state="failed", note=err, coid=coid)
                _once(f"order_failed:{side}", f"the {side} order for {qty} {fund} was not placed: {err}")
                return {"ok": False, "error": err, "ledger_id": rid}
        _set(rid, state="working", order_id=res["order_id"], coid=res.get("client_order_id") or coid,
             qty=_f(res.get("qty")) or qty)
    f = _poll_fill(res["order_id"], wait_s)
    row = {"id": rid, "fund": fund, "side": side, "reason": reason, "order_id": res["order_id"],
           "reserve": st["reserve"], "filled_qty": f["filled_qty"], "filled_px": f["filled_px"], "notified": 0}
    if f["final"]:
        _set(rid, state="done", filled_qty=f["filled_qty"], filled_px=f["filled_px"], filled_at=f.get("filled_at"),
             note=("filled" if f["status"] == "filled" else f"{f['status']} at the broker"))
        _after_fill(row, c)
    else:
        _set(rid, filled_qty=f["filled_qty"], filled_px=f["filled_px"])
    log.info(f"[idle-cash] {side} {qty} {fund} @ limit {limit} ({reason}): order {res['order_id']} "
             f"{f['status']}, filled {f['filled_qty']:g}")
    return {"ok": True, "ledger_id": rid, "order_id": res["order_id"], "side": side, "qty": qty, "limit": limit,
            "status": f["status"], "final": f["final"], "filled_qty": f["filled_qty"], "filled_px": f["filled_px"],
            "waited_s": f["waited_s"]}


def _shadow(st: dict, pl: dict) -> None:
    """Record what a live pass would have done (one row per different plan per day; nothing is placed)."""
    try:
        last = _rows(f"SELECT side, qty FROM {TABLE} WHERE kind='shadow' AND day=? ORDER BY created_at DESC LIMIT 1",
                     (st["day"],))
        if last and last[0]["side"] == pl["action"] and int(last[0]["qty"] or 0) == int(pl["qty"]):
            return
        _exec(f"INSERT INTO {TABLE}(id,kind,fund,side,qty,limit_px,reason,state,filled_qty,day,created_at,updated_at,"
              f"cash_before,equity,reserve,band,parked_before,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uuid.uuid4().hex[:10], "shadow", st["fund"], pl["action"], pl["qty"], pl.get("limit"), "shadow",
               "shadow", 0.0, st["day"], _nowiso(), _nowiso(), st["cash"], st["equity"], st["reserve"], st["band"],
               st["parked"], pl.get("why")))
        log.info(f"[idle-cash] record only: would {pl['action']} {pl['qty']} {st['fund']} at about {pl.get('limit')} "
                 f"({pl.get('why')}; cash {st['cash']:,.0f}, reserve {st['reserve']:,.0f})")
    except Exception as e:
        log.warning(f"[idle-cash] could not record the plan: {type(e).__name__}: {e}")


def _public(st: dict, pl: dict) -> dict:
    return {"fund": st["fund"], "cash": round(st["cash"], 2), "cash_free": round(st["cash_free"], 2),
            "open_buy_usd": round(st["open_buy_usd"], 2), "equity": round(st["equity"], 2),
            "reserve": round(st["reserve"], 2), "reserve_exercise": round(st["reserve_detail"]["exercise"], 2),
            "band": round(st["band"], 2), "parked_qty": st["parked"], "action": pl["action"], "qty": pl["qty"],
            "limit": pl.get("limit"), "usd": round(pl.get("usd") or 0.0, 2), "why": pl.get("why"),
            "urgent": bool(pl.get("urgent"))}


# ── public: the scheduled pass, the synchronous step, read-only views ────────────

def run_pass(now_et=None) -> dict:
    """One pass. {'ok', 'action', ...}. Places at most one order. Never raises."""
    try:
        c = cfg()
        if not enabled(c):
            return {"ok": True, "action": "none", "skipped": "off"}
        with _LOCK:
            sch = ensure_schema()
            if not sch.get("ok"):
                return _skip(sch.get("error"), unclear=True, key="schema")
            now = now_et or _now_et()
            st = read_state(c, now)
            if "skip" in st:
                return st["skip"]
            if st["cash"] < 0:
                _notify("The paper account is borrowing money",
                        f"Cash in the paper account is {_money(st['cash'])} below zero, so the account is borrowing. "
                        + ("Mason is selling parked shares of the fund to pay it back."
                           if st["parked"] >= 1 else "Nothing is parked that Mason could sell to pay it back."),
                        severity="warn", dedup=f"idle_cash:margin:{st['day']}")
            pl = _plan(st, c)
            out = dict(_public(st, pl), ok=True, shadow=not live(c))
            if pl.get("no_price"):
                _once("no_price", f"nothing done: {pl['why']}")
            if pl["action"] == "none":
                return out
            if not live(c):
                _shadow(st, pl)
                return out
            reason = "wind_down" if c.get("wind_down") is True else ("urgent" if pl.get("urgent") else
                                                                      ("park" if pl["action"] == "buy" else "top_up"))
            out["order"] = _place(st, pl, c, reason, _num(c, "fill_wait_s"))
            out["ok"] = bool(out["order"].get("ok"))
            return out
    except Exception as e:
        _once("pass_error", f"the pass failed and did nothing more: {type(e).__name__}: {e}")
        return {"ok": False, "action": "none", "error": f"{type(e).__name__}: {e}"}


def tick(now_et=None) -> dict:
    """Scheduler entry (mason_agent_loop.tick). A cheap no-op unless idle_cash.enabled; one pass per interval."""
    try:
        c = cfg()
        if not enabled(c):
            return {"skipped": "off"}
        gap = max(60.0, _num(c, "interval_minutes") * 60.0)
        if _state["last_pass"] is not None and _mono() - _state["last_pass"] < gap - 1:
            return {"skipped": "not due"}
        _state["last_pass"] = _mono()
        _state["last"] = run_pass(now_et)
        return _state["last"]
    except Exception as e:
        return {"error": f"tick: {type(e).__name__}: {e}"}


def free_cash(need_usd, *, why="a trade", now_et=None) -> dict:
    """The synchronous step for a trade that is refused only because cash is short: sell enough of the parked
    fund for `need_usd` plus the reserve and wait (at most idle_cash.free_cash_timeout_s) for the fill.
    {'ok': True, 'freed_usd', ...} when shares were sold; {'ok': False, 'reason'} otherwise (nothing sold, or the
    sale did not fill in time: it then stays at the broker as a day limit order and the trade stays refused)."""
    try:
        c = cfg()
        if not live(c) or c.get("free_cash_for_trades", True) is False:
            return {"ok": False, "reason": "the idle-cash rule is not switched on for real orders"}
        need = _f(need_usd)
        if need is None or need <= 0:
            return {"ok": False, "reason": "no amount given"}
        if not _LOCK.acquire(timeout=_num(c, "books_wait_s")):
            return {"ok": False, "reason": "the idle-cash rule is busy"}
        try:
            sch = ensure_schema()
            if not sch.get("ok"):
                return {"ok": False, "reason": sch.get("error")}
            now = now_et or _now_et()
            st = read_state(c, now, for_trade=True)     # (review fix) the trade's own hours, from 09:35 today
            if "skip" in st:
                return {"ok": False, "reason": st["skip"].get("skipped")}
            short = need - st["cash_free"]
            if short <= 0:                               # (review fix) e.g. a sale for another trade just filled:
                return {"ok": False, "reason": "cash already covers this trade", "cash_free": st["cash_free"],
                        "covered": True}                 # the caller checks the order again instead of refusing it
            if st["parked"] < 1 or st["avail"] < 1:
                return {"ok": False, "reason": "nothing is parked that could be sold"}
            if st["sells"]["all"] >= int(_num(c, "max_orders_per_day")):
                return {"ok": False, "reason": "too many sale orders were already sent today"}
            q = _quote(st["fund"])
            _b, s = _limits(q)
            if not s:
                return {"ok": False, "reason": "no reliable price for the fund right now"}
            sellable = int(math.floor(min(st["parked"], st["avail"]) + 1e-9))
            qty = min(sellable, int(math.ceil((short + st["reserve"]) / s - 1e-9)))
            if qty < 1 or qty * s < short:
                return {"ok": False, "reason": "the parked shares do not cover what this trade is short of"}
            pl = {"action": "sell", "qty": qty, "limit": s, "usd": qty * s, "quote": q, "urgent": True}
            res = _place(st, pl, c, "trade", _num(c, "free_cash_timeout_s"))
            if not res.get("ok"):
                return {"ok": False, "reason": res.get("error"), "order": res}
            got = (res.get("filled_qty") or 0.0) * (res.get("filled_px") or 0.0)
            if got <= 0:
                return {"ok": False, "reason": f"the sale did not fill within {res.get('waited_s')} seconds",
                        "order": res, "timed_out": True}
            log.info(f"[idle-cash] freed ${got:,.2f} for {why}")
            return {"ok": True, "freed_usd": round(got, 2), "order": res, "for": why}
        finally:
            _LOCK.release()
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def plan(now_et=None) -> dict:
    """READ ONLY: what a pass would do right now. Writes nothing, places nothing (works with the rule switched
    off, so it can be looked at before switching anything on)."""
    try:
        c = dict(cfg(), enabled=True)
        now = now_et or _now_et()
        st = read_state(c, now, act=False)
        if "skip" in st:
            return dict(st["skip"], read_only=True)
        return dict(_public(st, _plan(st, c)), ok=True, read_only=True, reserve_lines=st["reserve_detail"]["lines"])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "read_only": True}


def summary(price=None) -> dict | None:
    """The ledger in a few numbers, for the scoreboard and the status page. None when nothing was ever bought.
    `price` = {FUND: last price} (optional): then 'value_usd' and 'result_usd' are filled in."""
    try:
        rows = _rows(f"SELECT fund, side, COALESCE(filled_qty,0) q, filled_px px, day FROM {TABLE} "
                     f"WHERE kind='order' AND COALESCE(filled_qty,0) > 0 ORDER BY created_at")
        if not rows:
            return None
        fund = rows[-1]["fund"]
        out = {"fund": fund, "qty": 0.0, "bought_usd": 0.0, "sold_usd": 0.0, "n_buys": 0, "n_sells": 0,
               "first_day": rows[0]["day"], "last_day": rows[-1]["day"], "last_fill_px": None,
               "value_usd": None, "result_usd": None, "other_funds": {}}
        for r in rows:
            q, px = float(r["q"]), float(r["px"] or 0)
            sign = 1.0 if r["side"] == "buy" else -1.0
            if r["fund"] != fund:
                out["other_funds"][r["fund"]] = out["other_funds"].get(r["fund"], 0.0) + sign * q
                continue
            out["qty"] += sign * q
            out["bought_usd" if sign > 0 else "sold_usd"] += q * px
            out["n_buys" if sign > 0 else "n_sells"] += 1
            out["last_fill_px"] = px or out["last_fill_px"]
        out["qty"] = round(out["qty"], 6)
        px = _f((price or {}).get(fund))
        if px:
            out["value_usd"] = out["qty"] * px
            out["result_usd"] = out["value_usd"] + out["sold_usd"] - out["bought_usd"]
            out["price_used"] = px
        return out
    except Exception as e:
        _once("summary", f"the ledger summary failed: {type(e).__name__}: {e}")
        return None


def scoreboard_line(price=None) -> str:
    """The one line the weekly scoreboard prints about parked cash ('' when nothing was ever parked)."""
    s = summary(price)
    if not s:
        return ""
    name = "the S&P 500 fund (SPY)" if s["fund"] == "SPY" else f"the index fund {s['fund']}"
    line = (f"Parked cash: {s['qty']:g} shares of {name}"
            + (f", worth {_money(s['value_usd'])}" if s.get("value_usd") is not None else "")
            + f". Since {s['first_day']}: {_money(s['bought_usd'])} put in over {s['n_buys']} purchase"
              f"{'s' if s['n_buys'] != 1 else ''}, {_money(s['sold_usd'])} taken out over {s['n_sells']} sale"
              f"{'s' if s['n_sells'] != 1 else ''}")
    if s.get("result_usd") is not None:
        r = s["result_usd"]
        line += f"; result so far {'up' if r >= 0 else 'down'} {_money(abs(r))}"
    return (line + ". This is spare cash waiting in an index fund, not a trade: it is left out of Mason's results "
                   "above and out of the comparison with the S&P 500.")


def book_words(usd, funds) -> str:
    """MODEL-VISIBLE. The one sentence about parked cash in the holdings text the director and the quick checks
    read. Used only while idle_cash.show_in_book is true (wording in docs/fable/IDLE_CASH_PROMPTS_FOR_NICO.md)."""
    fl = sorted({str(f).upper() for f in (funds or []) if f})
    name = "the S&P 500 fund (SPY)" if fl == ["SPY"] else ("the index fund " + ", ".join(fl) if fl else "an index fund")
    return (f"Parked cash: {_money(usd)} of spare cash sits in {name}. It is not a trade and it is not Nico's. "
            "Mason sells it by itself whenever a trade needs the money, so count it as cash.")


def status() -> dict:
    """Switches + ledger, no broker call."""
    try:
        c = cfg()
        day = date.today().isoformat()
        rows = _rows(f"SELECT id, kind, side, qty, limit_px, reason, state, filled_qty, filled_px, created_at, note "
                     f"FROM {TABLE} ORDER BY created_at DESC LIMIT 12")
        return {"ok": True, "enabled": enabled(c), "shadow": not live(c), "fund": c["fund"],
                "parked": parked_map(), "summary": summary(), "today": day, "recent": rows,
                "settings": {k: c.get(k) for k in DEFAULTS}, "last_pass": _state.get("last")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def correct(apply: bool = False) -> dict:
    """BY HAND ONLY (the command line), after the note "the records and the broker disagree": make the ledger say
    what the broker really holds when the broker holds FEWER parked shares than the ledger (they were sold outside
    this rule). It writes one correcting row per fund and never places or changes an order. Without `apply` it
    only reports what it would write."""
    try:
        # (review fix) never while one of its own orders is unfinished or unconfirmed: a sale that filled but is not
        # written down yet would otherwise be taken off twice, and the shares left over would then read as Nico's.
        if _rows(f"SELECT 1 FROM {TABLE} WHERE kind='order' AND state IN ('placing','working') LIMIT 1"):
            return {"ok": False, "error": "one of the rule's own orders is not finished or not yet checked at the "
                                          "broker; nothing was written. Run this again after the next pass (10 minutes)."}
        pm = parked_map()
        if pm is None:
            return {"ok": False, "error": "the ledger could not be read"}
        pos = _positions()
        if not pos.get("ok"):
            return {"ok": False, "error": pos.get("error")}
        held = {}
        for p in pos["positions"]:
            sym = str(p.get("symbol") or "").upper()
            if not _OCC.match(sym) and str(p.get("side") or "long").lower() == "long":
                held[sym] = _f(p.get("qty")) or 0.0
        out = {"ok": True, "applied": bool(apply), "corrections": []}
        for fund, q in sorted(pm.items()):
            gone = q - held.get(fund, 0.0)
            if gone <= _EPS:
                continue
            last = _rows(f"SELECT filled_px FROM {TABLE} WHERE kind='order' AND fund=? AND filled_px IS NOT NULL "
                         f"ORDER BY created_at DESC LIMIT 1", (fund,))
            px = (last[0]["filled_px"] if last else None)
            out["corrections"].append({"fund": fund, "ledger": q, "broker": held.get(fund, 0.0), "removed": gone})
            if apply:
                _exec(f"INSERT INTO {TABLE}(id,kind,fund,side,qty,limit_px,reason,state,filled_qty,filled_px,day,"
                      f"created_at,updated_at,parked_before,note,notified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                      (uuid.uuid4().hex[:10], "order", fund, "sell", gone, px, "correction", "done", gone, px,
                       date.today().isoformat(), _nowiso(), _nowiso(), q,
                       "written by hand: these shares were no longer at the broker; the sale price is not known, "
                       "the last recorded price is used"))
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _reset_for_tests() -> None:
    _state.update(last_pass=None, last=None)
    _state["schema"].clear()
    _state["once"].clear()


if __name__ == "__main__":                 # command line: never places, changes or cancels an order
    import sys
    logging.basicConfig(level=logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif cmd == "plan":
        print(json.dumps(plan(), indent=1, default=str))
    elif cmd == "correct":                  # writes ONE correcting row in the ledger, and only with --apply
        print(json.dumps(correct(apply="--apply" in sys.argv), indent=1, default=str))
    else:
        print("usage: python3 mason_idle_cash.py status | plan        (read-only)\n"
              "       python3 mason_idle_cash.py correct [--apply]   (after the note 'the records and the broker "
              "disagree': makes the record match the broker; places no order)")
