"""
mason_protect.py — protection for the positions Nico entered himself (PAPER ONLY; no model call).

Ruling (Nico, 2026-11-13): "Place protection on the positions I place; recommendations can be made."
What that means here:
  * STOCK Nico entered  -> a standing sell order at the broker at the stop RECORDED IN THE JOURNAL, for exactly
                           Nico's own quantity. Mason's own shares in the same symbol are never counted, never
                           covered and never sold by it (they have their own exit orders).
  * no recorded stop, or the price is already at / below the recorded stop
                        -> nothing is placed and nothing is sold. ONE inbox note proposes a level; Accept records
                           that level in the journal and the order is then placed like any other recorded stop.
  * OPTIONS Nico holds  -> never sold by Mason on its own. Notes before expiry say what happens if nothing is
                           done; Accept on such a note sells that contract at a limit from mason_quotes.
  * a few fixed suggestions (through the target, earnings close, one position too large). No model call.

How it runs
-----------
  run(act)          one pass, called from mason_selfcheck.run() — the 15-minute self-check job on the analyst
                    desk's single worker, the same worker that runs Mason's exit manager and the director's
                    sells, so the three can never run at the same moment. act=False (the API route and the chat
                    tool) only reports: no order, no note, no journal or state write.
  on_review(...)    called from mason_notifications.review() when Nico presses Accept / Reject on one of this
                    module's notes.
  release_for_sale  called from mason_agent_loop.process_book_actions(): takes Mason's protective order off a
                    symbol before the director sells shares Nico placed (the broker allows one sell order per
                    share).
  booked_order_ids  read by mason_execution._sync_fills_to_journal so a protective sale is booked once, here.

The order
---------
  Default: a stop-limit sell, good until cancelled, tagged client_order_id "mason-protect-stop-<SYM>-…".
  Stop = the journal stop. Limit = stop − protect.limit_band_pct (3%). Why not a plain stop:
    * a plain stop becomes a market order. Mason's standing rule is limit orders only, and the paper engine
      fills against thin quotes (JAZZ showed a bid 6% under the real market);
    * a stock split or a stale level would turn a plain stop into an immediate sale at any price; a stop-limit
      whose limit is above the market just waits;
    * the cost: if the price jumps more than the band below the stop in one move, it does not sell. Then the
      position is "below its stop", which by Nico's ruling waits for him anyway: the order stays where it
      is and he gets one note.
  protect.order_type = "stop" switches to the plain stop.

Fail-safe
---------
  * protect.enabled (default FALSE) = nothing at all. protect.place_orders (default FALSE) = notes and the
    protection table only; no order is placed or cancelled.
  * Any broker error, any quantity Mason cannot account for, no reliable price, the market closed, the first
    minutes after the open: nothing is placed, the reason is shown in /api/selfcheck -> "protection".
  * The broker itself refuses a second sell order on the same shares ("insufficient qty available"); on top
    of that every order is sized from qty_available minus what Mason's own trade needs.
  * Every order is written to protect_orders BEFORE it is sent (client_order_id included). After a crash or
    a timeout the next pass looks the id up at the broker and adopts the order instead of sending a second.
  * An order cancelled outside Mason is not put back at that level without Nico's Accept (a different stop
    recorded in the journal afterwards is a new instruction and is placed).
  * Journal and broker must agree: when the journal shows MORE of Nico's shares than the broker holds, nothing
    new is placed for that symbol until the journal is reconciled; shares the journal does not know are
    never covered.
  * Never raises.

Tables (created on first use): protect_orders, protect_notes, protect_flags.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, date, timedelta, timezone

PREFIX = "mason-protect-"
STOP_PREFIX = "mason-protect-stop-"        # stock stops (the same literal is in mason_execution.close_position)
CLOSE_SEED = "protect-close-"              # option sales Nico approved: client id "mason-protect-close-…"
_PAPER_HOST = "paper-api.alpaca.markets"
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_NUM_RE = re.compile(r"^\s*\$?\s*(\d{1,7}(?:\.\d{1,4})?)\s*$")
_LIVE = ("placing", "working", "cancelling")
_EPS = 1e-6
_LOCK = threading.RLock()
_SCHEMA_DONE = set()

DEFAULTS = {
    "enabled": False,                     # master switch; false = this module does nothing
    "place_orders": False,                # may place / cancel protective orders at the broker
    "approve_enabled": True,              # Accept on one of Mason's protection notes may act
    "recommend": True,                    # stop proposals and the other suggestions
    "expiry_watch": True,                 # notes before Nico's options expire
    "withdraw_all": False,                # true = cancel every protective order Mason placed, place none
    "order_type": "stop_limit",           # "stop_limit" or "stop"
    "limit_band_pct": 3.0,                # stop_limit: lowest price accepted = stop minus this percent
    "stop_margin_pct": 1.0,               # a stop is only placed while the price is more than this % above it
    "open_delay_min": 5,                  # nothing is placed in the first minutes after the open
    "price_sanity_pct": 3.0,              # trusted price vs the broker's own mark: further apart = no action
    "max_new_orders_per_pass": 10,
    "retry_rejected_min": 60,             # wait after the broker refused an order
    "max_notes_per_pass": 20,
    "recommend_every_trading_days": 3,    # the same proposal is not repeated sooner ...
    "recommend_again_move_pct": 5.0,      # ... unless the price moved this much since the last one
    "after_reject_trading_days": 10,      # after Reject
    "info_every_trading_days": 10,        # target / size suggestions
    "proposal_atr_mult": 2.0,             # proposed stop = price − this × average daily range ...
    "proposal_atr_days": 14,
    "proposal_swing_days": 10,            # ... or just under the low of this many days when that is near
    "proposal_min_pct": 2.0,              # a proposal is never closer than this to the price
    "proposal_max_pct": 25.0,             # ... and never further
    "down_alert_pct": 15.0,               # "down more than X% with no stop"
    "earnings_within_trading_days": 5,
    "concentration_pct": 20.0,            # one position above this share of the account
    "expiry_first_note_trading_days": 5,
    "expiry_early_note_trading_days": 30,  # extra early note when exercising would cost more than the cash
    "option_close_wait_min": 20,          # an approved option sale not filled after this is withdrawn
    "typed_max_under_pct": 50.0,          # a typed stop further under the price than this is refused as a slip
    "option_reprice_confirm_pct": 10.0,   # option price this far under the note's price: Accept must be pressed twice
}

_ORDERS_SQL = """CREATE TABLE IF NOT EXISTS protect_orders(
    id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, lot_key TEXT, journal_ids TEXT,
    qty REAL, stop REAL, limit_px REAL, order_type TEXT, coid TEXT UNIQUE, order_id TEXT,
    state TEXT, filled_qty REAL DEFAULT 0, filled_notional REAL DEFAULT 0, booked_qty REAL DEFAULT 0,
    last_error TEXT, note TEXT, created_at TEXT, updated_at TEXT, closed_at TEXT)"""
_NOTES_SQL = """CREATE TABLE IF NOT EXISTS protect_notes(
    id TEXT PRIMARY KEY, key TEXT, kind TEXT, symbol TEXT, notif_id TEXT, action_json TEXT,
    ref_px REAL, level REAL, day TEXT, created_at TEXT, state TEXT, result TEXT, decided_at TEXT)"""
_FLAGS_SQL = "CREATE TABLE IF NOT EXISTS protect_flags(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"


# ── lazy modules (tests replace their I/O, not ours) ─────────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _EX():
    import mason_execution
    return mason_execution


def _AL():
    import mason_agent_loop
    return mason_agent_loop


def _MT():
    import mason_trades
    return mason_trades


def _MN():
    import mason_notifications
    return mason_notifications


def _conn():
    import mason_db
    return mason_db.get_connection()


# ── config, schema, small helpers ────────────────────────────────────────────────

def cfg() -> dict:
    """The protect block over DEFAULTS, read fresh. An unreadable config = every default = switched off."""
    c = dict(DEFAULTS)
    try:
        for k, v in ((_AT()._cfg().get("protect")) or {}).items():
            if not str(k).startswith("_"):
                c[k] = v
    except Exception:
        return dict(DEFAULTS)
    # REVIEW FIX 1: the two switches that can lead to an order are ON only for the JSON value true. A quoted
    # "false" (a string, which Python reads as true) used to switch protection AND order placing on.
    for k in ("enabled", "place_orders"):
        c[k] = c.get(k) is True
    # the "take everything off" switch errs the other way: any clear yes withdraws
    c["withdraw_all"] = c.get("withdraw_all") is True or str(c.get("withdraw_all")).strip().lower() in ("true", "1", "yes", "on")
    return c


def _num(c, key) -> float:
    try:
        v = float(c.get(key))
        return v if math.isfinite(v) else float(DEFAULTS[key])
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def ensure_schema(force=False) -> bool:
    try:
        import mason_db
        key = str(getattr(mason_db, "DB_FILE", ""))
        if key in _SCHEMA_DONE and not force:
            return True
        conn = _conn()
        try:
            with conn:
                conn.execute(_ORDERS_SQL)
                conn.execute(_NOTES_SQL)
                conn.execute(_FLAGS_SQL)
                conn.execute("CREATE INDEX IF NOT EXISTS ix_protect_orders_state ON protect_orders(state)")
                conn.execute("CREATE INDEX IF NOT EXISTS ix_protect_notes_key ON protect_notes(key, created_at)")
        finally:
            conn.close()
        try:
            _AL().ensure_schema()                     # agent_trades / agent_book_actions are read below
        except Exception:
            pass
        _SCHEMA_DONE.add(key)
        return True
    except Exception:
        return False


def _rows(sql, args=()) -> list:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _exec(sql, args=()) -> int:
    conn = _conn()
    try:
        with conn:
            return conn.execute(sql, args).rowcount
    finally:
        conn.close()


def _set_row(rid, **kv):
    kv["updated_at"] = _nowiso()
    cols = ", ".join(f"{k}=?" for k in kv)
    _exec(f"UPDATE protect_orders SET {cols} WHERE id=?", list(kv.values()) + [rid])


def _flag_get(key):
    try:
        r = _rows("SELECT value FROM protect_flags WHERE key=?", (key,))
        return r[0]["value"] if r else None
    except Exception:
        return None


def _flag_set(key, value):
    _exec("INSERT INTO protect_flags(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
          "value=excluded.value, updated_at=excluded.updated_at", (key, str(value), _nowiso()))


def _flag_clear(key):
    _exec("DELETE FROM protect_flags WHERE key=?", (key,))


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_et() -> datetime:
    return _AT()._now_et()


def _age_s(iso) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso)[:19])).total_seconds()
    except Exception:
        return 1e9


def _et_day(iso):
    """ET date (YYYY-MM-DD) of a broker timestamp, or None."""
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        m = re.search(r"\.(\d+)", s)
        if m:
            s = s[:m.start(1)] + m.group(1)[:6].ljust(6, "0") + s[m.end(1):]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = _AT()._et_tz()
        return (dt.astimezone(tz) if tz else dt).date().isoformat()
    except Exception:
        return None


def _holidays() -> set:
    try:
        return set(_AL()._holidays())
    except Exception:
        return set()


def _sessions_between(a: date, b: date, hol=None) -> int:
    """Trading sessions after `a` up to and including `b` (0 when b <= a)."""
    hol = _holidays() if hol is None else hol
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in hol:
            n += 1
    return n


def _px(v) -> str:
    v = _f(v)
    if v is None:
        return "n/a"
    return f"${v:,.4f}" if (0 < abs(v) < 1 and abs(round(v, 2) - v) > 1e-9) else f"${v:,.2f}"


def _usd(v) -> str:
    v = _f(v)
    if v is None:
        return "n/a"
    return f"${abs(v):,.0f}" if abs(v) >= 100 else f"${abs(v):,.2f}"


def _signed(v) -> str:
    v = _f(v) or 0.0
    return ("+" if v >= 0 else "-") + _usd(v)


def _qs(v) -> str:
    return f"{float(v):g}"


def _pstr(v) -> str:
    return f"{v:.2f}" if v >= 1 else f"{v:.4f}"


def _round_px(v):
    return round(v, 2) if v >= 1 else round(v, 4)


def _stop_of(t):
    v = _f(t.get("stop_loss"))
    return _round_px(v) if (v is not None and v > 0) else None


def _lot_key(sym, stop) -> str:
    return f"{sym}@{stop:.4f}" if stop else f"{sym}@none"


def _limit_for(stop, c):
    band = max(0.0, _num(c, "limit_band_pct"))
    lim = _round_px(stop * (1.0 - band / 100.0))
    return lim if lim < stop else _round_px(stop - (0.01 if stop >= 1 else 0.0001))


def parse_occ(sym):
    m = _OCC_RE.match(str(sym or "").upper())
    if not m:
        return None
    try:
        exp = date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None
    return {"underlying": m.group(1), "expiry": exp, "right": "call" if m.group(5) == "C" else "put",
            "strike": int(m.group(6)) / 1000.0}


def _paper_ok() -> bool:
    try:
        _k, _s, base, enabled = _EX()._creds()
        return bool(enabled) and _PAPER_HOST in (base or "")
    except Exception:
        return False


def _get(path, params=None):
    """(data, None) on a clean 200; (None, 'not_found') on 404; else (None, reason)."""
    try:
        code, data = _AT()._get(path, params)
    except Exception as e:
        return None, type(e).__name__
    if code == 200:
        return data, None
    if code == 404:
        return None, "not_found"
    return None, ("no answer from the broker" if code is None else f"HTTP {code}")


def _tell(title, body, *, ticker=None, severity="info", dedup=None, kind="system"):
    try:
        return _MN().add_notification(kind, str(title)[:200], body=body, severity=severity, ticker=ticker,
                                      created_by="mason_protect", dedup_key=dedup)
    except Exception:
        return None


def _activity(kind, title, *, ticker=None, extra=None):
    try:
        _AT()._activity(kind, title, approver="auto", ticker=ticker, rec_id=None, extra=extra or {})
    except Exception:
        pass


def _mason_qty(sym):
    """Shares of `sym` that belong to Mason's own trades, or None when that cannot be established."""
    try:
        v = _AL()._mason_open_qty(sym)
        return None if v is None else float(v)
    except Exception:
        return None


def _mason_option_qty(occ):
    """Contracts of `occ` that belong to Mason's own trades, or None when not settled."""
    try:
        AL = _AL()
        total, have = 0.0, set()
        for r in AL._q("SELECT * FROM agent_trades WHERE source='mason' AND UPPER(occ)=? "
                       "AND COALESCE(record_deleted,0)=0", (occ,)):
            have.add(r["rec_id"])
            if r["status"] == "open":
                total += AL._remaining(r)
        for rec in AL._q("SELECT id, extra_json FROM recommendations WHERE kind='director' AND "
                         "json_extract(extra_json,'$.execution.state') IN ('placing','approved_pending_fill','placed') "
                         "AND UPPER(json_extract(extra_json,'$.execution.symbol'))=?", (occ,)):
            if rec["id"] in have:
                continue
            e = (json.loads(rec["extra_json"] or "{}").get("execution") or {})
            if e.get("state") == "placing":
                return None
            total += _f(e.get("qty")) or 0.0
        return total
    except Exception:
        return None


def _mason_order_ids(sym):
    """Ids of Mason's own entry orders and exit legs on this symbol; None = could not be read."""
    try:
        AL, EX = _AL(), _EX()
        ids = set()
        for rec in AL._q("SELECT extra_json FROM recommendations WHERE kind='director' AND "
                         "json_extract(extra_json,'$.execution.state') IN ('placing','approved_pending_fill','placed') "
                         "AND UPPER(json_extract(extra_json,'$.execution.symbol'))=?", (sym,)):
            oid = (json.loads(rec["extra_json"] or "{}").get("execution") or {}).get("order_id")
            if not oid:
                continue
            g = EX.get_order(oid)
            if not g.get("ok"):
                return None
            ids.add(oid)
            ids |= {l.get("id") for l in (g["order"].get("legs") or []) if isinstance(l, dict) and l.get("id")}
        for r in AL._q("SELECT exit_order_id FROM agent_trades WHERE source='mason' AND status='open' AND "
                       "occ IS NULL AND UPPER(ticker)=? AND exit_order_id IS NOT NULL", (sym,)):
            ids.add(r["exit_order_id"])
        return ids
    except Exception:
        return None


# ── one pass ─────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, c, act, preview=False):
        self.c, self.act, self.preview = c, bool(act), bool(preview)
        self.live = False                 # may place / cancel
        self.can_cancel = False
        self.market_ok, self.market_note = False, ""
        self.now = _now_et()
        self.today = self.now.date()
        self.snap = {}
        self.units, self.rows = [], []
        self.entries, self.issues, self.actions, self.would_send = [], [], [], []
        self.notes_sent = self.placed = 0
        self.quotes, self.doubt = {}, {}
        self.journal = None
        self.skipped = {}                 # symbol -> journal rows read as another account's (never lots)
        self.hol = _holidays()


def _orders_left_when_off() -> list:
    """Mason's protective stock orders the table still shows as live. Read-only; [] when the table does not exist."""
    try:
        return [{"symbol": r["symbol"], "qty": float(r["qty"] or 0) - float(r["filled_qty"] or 0), "stop": r["stop"],
                 "order_id": r["order_id"]}
                for r in _rows("SELECT symbol, qty, filled_qty, stop, order_id FROM protect_orders WHERE "
                               "kind='stock_stop' AND state IN ('placing','working','cancelling') ORDER BY symbol")]
    except Exception:
        return []


def run(act: bool = True, preview: bool = False) -> dict:
    """One protection pass. act=False reports only. Never raises."""
    out = {"enabled": False, "at": _nowiso()}
    try:
        c = cfg()
        if not c.get("enabled"):
            out["state_text"] = "Protection of the positions you entered is switched off (protect.enabled)."
            # REVIEW FIX 2: switching off does not take orders off the broker. Say which ones are still there
            # (the table only; no broker call, and nothing is created when the module was never used).
            left = _orders_left_when_off()
            if left:
                out["orders_left_at_broker"] = left
                out["state_text"] += (
                    f" {len(left)} protective order(s) Mason placed earlier are still at the broker ("
                    + ", ".join(f"{x['symbol']} {_qs(x['qty'])} shares at {_px(x['stop'])}" for x in left[:8])
                    + "). They still sell if their price is reached, and Mason is not following them while this is "
                      "switched off. To take them off, switch protection back on with protect.withdraw_all set to "
                      "true for one check, or cancel them at the broker.")
            return out
        out["enabled"] = True
        if not _LOCK.acquire(timeout=60 if act else 10):
            out.update(ok=False, error="Another protection pass is still running.")
            return out
        try:
            return _run_locked(c, act, preview, out)
        finally:
            _LOCK.release()
    except Exception as e:
        out.update(ok=False, error=f"The protection pass itself failed ({type(e).__name__}: {e}). Nothing was changed "
                                   f"after the point of failure.")
        return out


def _run_locked(c, act, preview, out) -> dict:
    ensure_schema()
    ctx = _Ctx(c, act, preview)
    paper = _paper_ok()
    ctx.live = bool(ctx.act and c.get("place_orders") and paper and not c.get("withdraw_all"))
    ctx.can_cancel = bool(ctx.act and paper and (c.get("place_orders") or c.get("withdraw_all")))
    out.update(place_orders=bool(c.get("place_orders")), report_only=not ctx.act, positions=[], issues=ctx.issues,
               actions=ctx.actions)
    snap = _snapshot(ctx)
    if not snap.get("ok"):
        out.update(ok=False, error=snap.get("error"))
        if ctx.act:
            _tell("Protection check skipped: the broker's data could not be read",
                  f"{snap.get('error')} Mason placed, changed and cancelled nothing. Orders already at the broker "
                  f"stay in place. It tries again at the next check.", severity="warn",
                  dedup="protect:broker_unavailable:" + ctx.today.isoformat())
        return out
    ctx.snap = snap
    _market(ctx)
    _refresh_orders(ctx)                                 # an order's own record is good evidence by itself
    if not snap["positions"]:
        # An empty position list is only believed when nothing says otherwise (a protective sale that was just
        # booked above explains it). Otherwise it is bad data: nothing is placed and nothing is withdrawn.
        try:
            odd = bool(ctx.rows) or any(_EX()._is_alpaca_row(t) for t in _MT().get_open_trades())
        except Exception:
            odd = True
        if odd:
            out.update(ok=False, error="The broker reported no positions at all while the journal still has open "
                                       "rows. Treated as bad data; nothing was placed or withdrawn.")
            return out
    _stocks(ctx)
    _options(ctx)
    _suggestions(ctx)
    n = {"protected": 0, "waiting for your decision": 0, "not protectable": 0, "not protected yet": 0}
    for e in ctx.entries:
        if e.get("state") in n:
            n[e["state"]] += 1
    out.update(ok=True, market_open=ctx.market_ok, market_note=ctx.market_note, positions=ctx.entries,
               summary=n, orders_placed=ctx.placed, notes_sent=ctx.notes_sent)
    if preview:
        out["would_send"] = ctx.would_send
    return out


def _valid_positions(data) -> bool:
    if not isinstance(data, list):
        return False
    return all(isinstance(p, dict) and p.get("symbol") and _f(p.get("qty")) is not None for p in data)


def _valid_orders(data) -> bool:
    if not isinstance(data, list):
        return False
    return all(isinstance(o, dict) and o.get("symbol") and o.get("side") and o.get("id") for o in data)


def _unit(o) -> dict:
    legs = [l for l in (o.get("legs") or []) if isinstance(l, dict)]
    typ = str(o.get("type") or o.get("order_type") or "").lower()
    cls = str(o.get("order_class") or "").lower()
    qty, fq = _f(o.get("qty")) or 0.0, _f(o.get("filled_qty")) or 0.0
    leg_stop = next((_f(l.get("stop_price")) for l in legs if _f(l.get("stop_price"))), None)
    has_stop = (typ.startswith(("stop", "trailing")) or cls in ("bracket", "oco")
                or any(str(l.get("type") or "").lower().startswith(("stop", "trailing")) for l in legs))
    return {"id": o.get("id"), "coid": str(o.get("client_order_id") or ""),
            "symbol": str(o.get("symbol") or "").upper(), "side": str(o.get("side") or "").lower(),
            "type": typ, "cls": cls, "status": str(o.get("status") or "").lower(), "qty": qty, "filled": fq,
            "rem": max(0.0, qty - fq), "limit": _f(o.get("limit_price")),
            "stop": _f(o.get("stop_price")) or leg_stop, "has_stop": has_stop, "raw": o}


def _snapshot(ctx) -> dict:
    AT = _AT()
    pos, perr = _get("/positions")
    if perr or not _valid_positions(pos):
        return {"ok": False, "error": f"The broker's list of positions could not be read "
                                      f"({perr or 'the data failed its checks'})."}
    orders, oerr = _get("/orders", {"status": "open", "limit": 500, "nested": "true"})
    if oerr or not _valid_orders(orders):
        return {"ok": False, "error": f"The broker's list of open orders could not be read "
                                      f"({oerr or 'the data failed its checks'})."}
    try:
        clk = AT._clock()
    except Exception:
        clk = {"ok": False}
    try:
        acct = AT._account()
    except Exception:
        acct = {"ok": False}
    ctx.units = [_unit(o) for o in orders]
    return {"ok": True, "positions": pos, "orders": orders, "clock": clk or {"ok": False},
            "account": acct or {"ok": False}}


def _market(ctx):
    clk = ctx.snap.get("clock") or {}
    if not clk.get("ok"):
        ctx.market_note = "The market clock could not be read, so nothing new is placed."
        return
    if not clk.get("is_open"):
        ctx.market_note = "The market is closed. New protective orders are placed after it opens."
        return
    delay = max(0.0, _num(ctx.c, "open_delay_min"))
    if ctx.now < ctx.now.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=delay):
        ctx.market_note = f"The market has just opened. Mason waits {delay:g} minutes before placing anything."
        return
    ctx.market_ok = True


def _quote(ctx, sym) -> dict:
    """{'px': trusted price or None, 'why': plain words when there is none}. Only asked while the market is open."""
    if sym in ctx.quotes:
        return ctx.quotes[sym]
    out = {"px": None, "why": ctx.market_note or "no price"}
    if ctx.market_ok:
        try:
            AT = _AT()
            q = AT._trusted_quote(sym, False, AT._at_cfg())
            if q and q.get("legacy"):
                out["why"] = "trusted prices are switched off (autotrade.quotes.enabled)"
            elif q and q.get("ok") and _f(q.get("px")):
                out = {"px": float(q["px"]), "why": None, "source": q.get("source")}
            else:
                out["why"] = str((q or {}).get("reason") or "no reliable price right now")[:200]
        except Exception as e:
            out["why"] = f"price lookup failed ({type(e).__name__})"
    ctx.quotes[sym] = out
    return out


# ── phase A: what happened to the orders Mason placed ────────────────────────────

def _lookup(r):
    if r.get("order_id"):
        return _get(f"/orders/{r['order_id']}", {"nested": "true"})
    return _get("/orders:by_client_order_id", {"client_order_id": r["coid"]})


def _upd(ctx, r, **kv) -> dict:
    if ctx.act and kv:
        _set_row(r["id"], **kv)
    return dict(r, **kv)


def _refresh_orders(ctx, only_symbol=None):
    rows = _rows("SELECT * FROM protect_orders WHERE state IN ('placing','working','cancelling') "
                 "OR COALESCE(filled_qty,0) > COALESCE(booked_qty,0) + 1e-9 ORDER BY created_at, id")
    by_id = {u["id"]: u["raw"] for u in ctx.units}
    by_coid = {u["coid"]: u["raw"] for u in ctx.units if u["coid"]}
    live = []
    for r in rows:
        if only_symbol and r["symbol"] != only_symbol:
            continue
        o = by_id.get(r["order_id"]) if r.get("order_id") else by_coid.get(r["coid"])
        if o is None:
            o, err = _lookup(r)
            if err == "not_found":
                if r["state"] == "placing":            # written down but never reached the broker
                    r = _upd(ctx, r, state="unsent", closed_at=_nowiso(), note="never reached the broker")
                    if ctx.act and r["kind"] == "option_close":      # REVIEW FIX 6: Nico was told he would hear
                        _tell(f"{_words(r['symbol'])}: the sale you approved did not reach the broker",
                              f"Mason checked at the broker: the order to sell {_qs(r.get('qty') or 0)} contract(s) of "
                              f"the {_words(r['symbol'])} never arrived there. Nothing was sold. Open the note again "
                              f"and press Accept to try once more at the current price.",
                              ticker=_underlying(r["symbol"]), severity="warn", dedup=f"protect:close_unsent:{r['id']}")
                else:
                    r = _upd(ctx, r, state="gone", closed_at=_nowiso(), note="the broker no longer knows this order")
                    ctx.issues.append(f"{r['symbol']}: the broker no longer knows Mason's protective order "
                                      f"{str(r.get('order_id'))[:8]}.")
                continue
            if err or not isinstance(o, dict):
                ctx.doubt[r["symbol"]] = (f"Mason's own order on {r['symbol']} could not be checked at the broker "
                                          f"({err or 'unexpected data'}).")
                if r["state"] in _LIVE:
                    live.append(r)
                continue
        r = _apply_status(ctx, r, o)
        if r["state"] in _LIVE:
            live.append(r)
    ctx.rows = live


def _apply_status(ctx, r, o) -> dict:
    st = str(o.get("status") or "").lower()
    fq, avg = _f(o.get("filled_qty")) or 0.0, _f(o.get("filled_avg_price"))
    upd = {}
    if not r.get("order_id") and o.get("id"):
        upd["order_id"] = o["id"]
    if abs(fq - float(r.get("filled_qty") or 0)) > 1e-9:
        upd["filled_qty"] = fq
    if st == "filled":
        new = "filled"
    elif st in ("canceled", "cancelled"):
        new = "cancelled" if r["state"] == "cancelling" else "owner_cancelled"
    elif st == "expired":
        new = "expired"
    elif st == "replaced":
        new = "owner_replaced"
    elif st == "rejected":
        new = "rejected"
    elif st == "pending_cancel":
        new = "cancelling"
    else:
        new = "cancelling" if r["state"] == "cancelling" else "working"
    old = r["state"]
    if old not in _LIVE:
        new = old                                       # a finished order is only looked at again to book a sale
    if new != old:
        upd["state"] = new
        if new not in _LIVE:
            upd["closed_at"] = _nowiso()
    r = _upd(ctx, r, **upd)
    if ctx.act and avg and fq > float(r.get("booked_qty") or 0) + 1e-9:
        r = _book_fill(ctx, r, o, fq, avg)
    if ctx.act and new != old:
        _on_transition(ctx, r, old, new, o)
    if ctx.act and r["kind"] == "option_close" and new == "working" and \
            _age_s(r.get("created_at")) > 60.0 * _num(ctx.c, "option_close_wait_min"):
        if _send_cancel(r) in (200, 204):
            r = _upd(ctx, r, state="cancelling", note="not filled in time")
    elif ctx.act and new == "cancelling" and st != "pending_cancel" and _age_s(r.get("updated_at")) > 90:
        _send_cancel(r)                               # a cancel the broker has not carried out: ask again
    return r


def _position_qty(ctx, sym) -> float:
    for p in ctx.snap.get("positions") or []:
        if str(p.get("symbol") or "").upper() == sym:
            return _f(p.get("qty")) or 0.0
    return 0.0


def _on_transition(ctx, r, old, new, o):
    sym, sid = r["symbol"], str(r.get("order_id") or "")[:8]
    if r["kind"] == "stock_stop":
        if old == "placing" and new == "working":
            _tell(f"Protective stop placed: {sym}, sells {_qs(r.get('qty') or 0)} shares if the price falls to "
                  f"{_px(r.get('stop'))}",
                  f"Mason's standing order for the {_qs(r.get('qty') or 0)} {sym} shares you entered is in place at "
                  f"the broker (the broker's answer only arrived on this check): it sells if the price falls to "
                  f"{_px(r.get('stop'))}" + (f", but not for less than {_px(r.get('limit_px'))}" if r.get("limit_px")
                                             else "") + f". Broker order {sid}.", ticker=sym,
                  dedup=f"protect:placed:{r['id']}")
        elif new == "owner_cancelled" and _position_qty(ctx, sym) > 0:
            _flag_set(f"hold:{sym}", json.dumps({"since": _nowiso(), "order": r.get("order_id"),
                                                 "stop": r.get("stop"), "qty": r.get("qty")}))
            _activity("protect_owner_cancel", f"Protective stop on {sym} was cancelled outside Mason", ticker=sym,
                      extra={"order_id": r.get("order_id")})
        elif new == "owner_replaced":
            _tell(f"{sym}: you changed Mason's protective order, so it is now yours",
                  f"The protective order Mason placed on your {sym} shares (order {sid}) was changed at the broker. "
                  f"Mason treats the changed order as your own and leaves it alone.",
                  ticker=sym, dedup=f"protect:replaced:{r['id']}")
        elif new == "rejected":
            _tell(f"{sym}: the broker refused Mason's protective order",
                  f"The broker refused the protective order for your {sym} shares. Nothing is in place. Mason tries "
                  f"again in about {_num(ctx.c, 'retry_rejected_min'):g} minutes.", ticker=sym, severity="warn",
                  dedup=f"protect:rejected:{sym}:{ctx.today.isoformat()}")
    elif r["kind"] == "option_close" and new in ("cancelled", "expired", "owner_cancelled", "rejected"):
        left = float(r.get("qty") or 0) - float(r.get("filled_qty") or 0)
        if left > _EPS:
            _tell(f"{_words(sym)}: the sale you approved did not go through" + (" completely" if left < r["qty"] else ""),
                  f"Mason offered {_qs(r['qty'])} contract(s) of the {_words(sym)} at {_px(r.get('limit_px'))} each, as you "
                  f"approved. {_qs(left)} did not sell at that price and the order was withdrawn. Open the note "
                  f"again and press Accept to try once more at the current price.", ticker=_underlying(sym),
                  severity="warn", dedup=f"protect:close_unfilled:{r['id']}")


def _underlying(sym):
    occ = parse_occ(sym)
    return occ["underlying"] if occ else sym


def _words(sym) -> str:
    """An option contract in words ('WMT Dec 18 2026 $110 call'); any other symbol unchanged."""
    occ = parse_occ(sym)
    if not occ:
        return str(sym)
    return (f"{occ['underlying']} {occ['expiry'].strftime('%b')} {occ['expiry'].day} {occ['expiry'].year} "
            f"${occ['strike']:g} {occ['right']}")


def _book_fill(ctx, r, o, fq, avg) -> dict:
    """Book the part of an order's fill that is not booked yet (journal first; the order row last)."""
    booked = float(r.get("booked_qty") or 0)
    delta = fq - booked
    px = (avg * fq - float(r.get("filled_notional") or 0)) / delta if delta > 0 else avg
    if not (px and px > 0):
        px = avg
    sym, oid = r["symbol"], o.get("id") or r.get("order_id")
    info = {}
    if r["kind"] == "stock_stop":
        ok, info = _journal_stop_out(ctx, r, delta, px, oid, o.get("filled_at") or o.get("updated_at"))
        told = f"told_unbooked:{r['id']}:{fq:g}"
        if not ok:
            ctx.issues.append(f"{sym}: Mason's protective order sold {_qs(delta)} shares but the journal could not "
                              f"be updated ({info.get('error')}). It is tried again at the next check.")
            # REVIEW FIX 4: the sale itself is always announced, once, even while the journal cannot take it
            # (before, a refused or failing journal write meant Nico was never told his shares were sold).
            if not _flag_get(told):
                n = _tell(f"Protective stop sold {_qs(delta)} {sym} at {_px(px)}",
                          f"The standing order at the broker (your recorded stop of {_px(r.get('stop'))}) sold "
                          f"{_qs(delta)} of the {sym} shares you entered at {_px(px)} each ({_usd(delta * px)}). "
                          f"Mason could not write this sale into the journal yet "
                          f"({str(info.get('error'))[:160]}); it tries again at every check. Until then the journal "
                          f"still shows these shares as held.", ticker=sym, severity="warn",
                          dedup=f"protect:fill_unbooked:{r['id']}:{fq:g}")
                try:
                    if n:
                        _flag_set(told, _nowiso())
                except Exception:
                    pass
            return _upd(ctx, r, last_error=str(info.get("error"))[:300])
        if _flag_get(told):
            info = dict(info, late=True)
            try:
                _flag_clear(told)
            except Exception:
                pass
        _close_thesis_rows(sym, False, delta, px, oid, "protective_stop")
    else:
        _close_thesis_rows(sym, True, delta, px, oid, "nico_approved_sale")
    r = _upd(ctx, r, booked_qty=fq, filled_notional=avg * fq, filled_qty=fq, last_error=None)
    _tell_fill(ctx, r, delta, px, fq, info)
    return r


def _journal_stop_out(ctx, r, qty, px, oid, at):
    """Close the journal rows this protective order covered, as a stop-out, through the same routine the
    journal sync uses for a hand sale (FIFO, a part-sold row is split). Returns (ok, info)."""
    try:
        MT, EX = _MT(), _EX()
        sym = r["symbol"]
        ctx.journal = None                                   # read the journal fresh
        lots, _problem = _nico_lots(ctx, sym)
        lots = lots or []
        ids = set(json.loads(r.get("journal_ids") or "[]"))
        # REVIEW FIX 3: shares the journal ALREADY shows as sold by this very broker order (an earlier attempt
        # that failed after its journal write, or the general journal sync) are never booked a second time.
        # Before, the retry fell through to "any other open lot of the symbol", closed a lot that was still
        # held at the broker and so made Mason withdraw that lot's own protective order.
        done = _rows("SELECT COALESCE(SUM(shares), 0) AS s FROM trades WHERE exit_order_id=? AND status='closed'",
                     (oid,))
        ahead = max(0.0, float((done[0]["s"] if done else 0) or 0) - float(r.get("booked_qty") or 0))
        todo = max(0.0, float(qty) - ahead)
        day = _et_day(at) or ctx.today.isoformat()
        text = (f"Stop-out. The protective order Mason placed at the recorded stop of {_px(r.get('stop'))} "
                f"triggered and sold at {_px(px)} (broker order {oid}).")
        info = {}
        if todo > 1e-9:
            pick = ([t for t in lots if _lot_key(sym, t["stop"]) == r.get("lot_key")]
                    or [t for t in lots if t["id"] in ids] or lots)
            if not pick:
                info = {"note": "no open journal row was left to close"}
            else:
                pick.sort(key=lambda t: (t.get("entry_date") or "", t.get("created_at") or ""))
                left, cost = todo, 0.0
                for t in pick:
                    take = min(left, t["qty"])
                    cost += take * (t.get("entry") or 0.0)
                    left -= take
                    if left <= 1e-9:
                        break
                raw = [MT.get_trade_by_id(t["id"]) for t in pick]
                res = EX._apply_sell_fill(MT, sym, todo, round(float(px), 4), oid, {sym: [t for t in raw if t]})
                if isinstance(res, EX.SellRefused):
                    return False, {"error": res.reason}
                booked = todo - max(0.0, left)
                if ahead <= 1e-9 and booked > 0:           # the amounts are only exact when all of it was booked now
                    info = {"pnl": px * booked - cost, "entry": cost / booked}
        conn = _conn()
        try:
            with conn:                                     # every row this order closed that is not marked yet
                conn.execute("UPDATE trades SET exit_reason='stop_hit', exit_date=?, review_text=? "
                             "WHERE exit_order_id=? AND status='closed' AND COALESCE(exit_reason,'') IN ('', 'manual')",
                             [day, text, oid])
        finally:
            conn.close()
        ctx.journal = None
        return True, info
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def _close_thesis_rows(symbol, is_option, qty, px, order_id, reason):
    """The learning loop's "Nico's thesis" rows (agent_trades, source='nico') for what was just sold, FIFO."""
    try:
        AL = _AL()
        left = float(qty)
        where = "UPPER(occ)=?" if is_option else "occ IS NULL AND UPPER(ticker)=?"
        for row in AL._q(f"SELECT * FROM agent_trades WHERE source='nico' AND status='open' AND {where} "
                         f"AND COALESCE(record_deleted,0)=0 ORDER BY entry_ts, id", (symbol.upper(),)):
            if left <= 1e-9:
                break
            take = min(left, AL._remaining(row))
            if take <= 0:
                continue
            parts = AL._parts(row) + [{"qty": take, "px": float(px), "order_id": order_id, "at": _nowiso()}]
            row = AL._set_feat(row, partial_exits=parts)
            left -= take
            if AL._remaining(row) <= 1e-9:
                entry, q = row.get("entry_px"), float(row.get("qty") or 0)
                mult = 100.0 if row.get("occ") else 1.0
                avg = sum(x["qty"] * x["px"] for x in parts) / sum(x["qty"] for x in parts)
                f = {"exit_ts": _nowiso(), "exit_px": avg, "exit_reason": reason, "status": "closed",
                     "graded_at": _nowiso()}
                if entry:
                    f.update({"pnl_usd": (avg - entry) * q * mult, "pnl_pct": (avg - entry) / entry * 100.0,
                              "win": 1 if avg > entry else 0})
                AL._update_trade(row["id"], f)
    except Exception:
        pass


def _tell_fill(ctx, r, delta, px, fq, info):
    sym = r["symbol"]
    left = float(r.get("qty") or 0) - fq
    if r["kind"] == "stock_stop":
        pl = ""
        if info.get("pnl") is not None and info.get("entry"):
            pl = f" Against your entry of about {_px(info['entry'])} that is {_signed(info['pnl'])}."
        rest = (f" {_qs(left)} more shares of that order have not sold yet; the order stays in place for them."
                if left > _EPS else "")
        jn = (" The journal row was closed as a stop-out." if not info.get("note") else
              " There was no open journal row to close for it.")
        title = f"Protective stop sold {_qs(delta)} {sym} at {_px(px)}"
        stop = _f(r.get("stop")) or 0.0
        if info.get("late"):                               # REVIEW FIX 4: the sale was announced before; this is the journal
            title = f"{sym}: the journal now shows the protective sale of {_qs(delta)} shares at {_px(px)}"
        if stop and px > stop * 1.001:
            # REVIEW FIX 7: sold ABOVE the stop means the market never traded down to it in an orderly way: one
            # reported trade at or below the stop is enough to trigger the order (Mason's own SPY stop, 2026-11-13).
            head = (f"The standing order at the broker for your {sym} shares was triggered at your recorded stop of "
                    f"{_px(stop)} and sold {_qs(delta)} of the shares you entered at {_px(px)} each "
                    f"({_usd(delta * px)}). The sale price is above the stop: a single trade at or below {_px(stop)} "
                    f"was reported, which is enough to trigger the order, while the market itself was higher. If you "
                    f"did not want to be out, you can buy the shares back.")
        else:
            head = (f"{sym} fell to your recorded stop of {_px(stop)} and the standing order at the broker sold "
                    f"{_qs(delta)} of the shares you entered at {_px(px)} each ({_usd(delta * px)}).")
        body = f"{head}{pl}{jn}{rest}"
        _tell(title, body, ticker=sym, severity="warn", dedup=f"protect:fill:{r['id']}:{fq:g}")
        _activity("protect_stop_filled", title, ticker=sym,
                  extra={"order_id": r.get("order_id"), "qty": delta, "px": px, "pnl": info.get("pnl")})
    else:
        title = f"Sold {_qs(delta)} contract(s) of the {_words(sym)} at {_px(px)} each, as you approved"
        body = (f"The sale you approved filled: {_qs(delta)} contract(s) of the {_words(sym)} at {_px(px)} each "
                f"({_usd(delta * px * 100)} in total)."
                + (f" {_qs(left)} contract(s) have not sold yet." if left > _EPS else ""))
        _tell(title, body, ticker=_underlying(sym), dedup=f"protect:fill:{r['id']}:{fq:g}")
        _activity("protect_option_sold", title, ticker=_underlying(sym),
                  extra={"order_id": r.get("order_id"), "qty": delta, "px": px})


def _send_cancel(r):
    try:
        code, _d = _EX()._request("DELETE", f"/orders/{r['order_id']}")
        return code
    except Exception:
        return None


def _cancel(ctx, r, reason) -> bool:
    """Withdraw one of Mason's OWN protective orders (never anything else)."""
    plan = {"do": "cancel", "symbol": r["symbol"], "order_id": r.get("order_id"), "why": reason, "done": False}
    ctx.actions.append(plan)
    if not ctx.can_cancel or r.get("state") != "working" or not r.get("order_id"):
        if ctx.act and not ctx.can_cancel and r.get("state") == "working":
            ctx.issues.append(f"{r['symbol']}: Mason's protective order at {_px(r.get('stop'))} should be withdrawn "
                              f"({reason}) but placing and cancelling orders is switched off (protect.place_orders). "
                              f"It is still at the broker.")
        return False
    _set_row(r["id"], state="cancelling", note=str(reason)[:300])
    r["state"] = "cancelling"
    code = _send_cancel(r)
    if code == 404:
        _set_row(r["id"], state="gone", closed_at=_nowiso())
        r["state"] = "gone"
    plan["done"] = code in (200, 204, 404)
    _activity("protect_stop_cancelled", f"Protective order on {r['symbol']} withdrawn: {reason}", ticker=r["symbol"],
              extra={"order_id": r.get("order_id"), "http": code})
    return plan["done"]


# ── phase B: stocks ──────────────────────────────────────────────────────────────

def _journal_rows(ctx):
    if ctx.journal is None:
        rows = _MT().get_open_trades()
        try:
            mids = {r["journal_id"] for r in _AL()._q(
                "SELECT journal_id FROM agent_trades WHERE source='mason' AND journal_id IS NOT NULL "
                "AND COALESCE(journal_table,'trades')='trades'")}
        except Exception:
            mids = set()
        ctx.journal = (rows, mids)
    return ctx.journal


def _nico_lots(ctx, sym):
    """(lots, problem): the open journal rows of `sym` that are Nico's and held at Alpaca. Mason's own rows
    (linked to a director call) and personal-account rows are never in the list."""
    rows, mids = _journal_rows(ctx)
    EX, MT = _EX(), _MT()
    lots = []
    for t in rows:
        if str(t.get("ticker") or "").upper() != sym or (t.get("status") or "open") != "open":
            continue
        if t.get("id") in mids or t.get("director_rec_id") or str(t.get("setup_type") or "").lower() == "director":
            continue
        if not EX._is_alpaca_row(t):
            # The shared rule reads a row as another account's when its text names one. Such a row is never a
            # lot here; it is remembered so Nico can be told exactly which row, in case the wording is the cause.
            word = (EX.non_alpaca_marker(t) if hasattr(EX, "non_alpaca_marker") else None) or "another account"
            keep = ctx.skipped.setdefault(sym, [])
            if t.get("id") not in [x["id"] for x in keep]:
                keep.append({"id": t.get("id"), "qty": _f(t.get("shares")) or 0.0, "word": word})
            continue
        found, refusal = MT.close_refusal(t["id"])
        if not found or refusal:
            continue
        if str(t.get("direction") or "long").lower() != "long":
            return None, "the journal has a short row for this symbol"
        sh = _f(t.get("shares"))
        if not sh or sh <= 0:
            return None, "a journal row for this symbol has no share count"
        tgt = _f(t.get("target_price"))
        lots.append({"id": t["id"], "qty": sh, "entry": _f(t.get("entry_price")), "stop": _stop_of(t),
                     "target": tgt if (tgt and tgt > 0) else None, "entry_date": t.get("entry_date"),
                     "created_at": t.get("created_at")})
    return lots, None


def _stocks(ctx):
    stock_pos = {}
    for p in ctx.snap["positions"]:
        sym = str(p.get("symbol") or "").upper()
        if parse_occ(sym) or str(p.get("asset_class") or "us_equity").lower() != "us_equity":
            continue
        stock_pos[sym] = p
    ours = {}
    for r in ctx.rows:
        if r["kind"] == "stock_stop":
            ours.setdefault(r["symbol"], []).append(r)
    for sym in sorted(set(stock_pos) | set(ours)):
        try:
            e = _stock_symbol(ctx, sym, stock_pos.get(sym), ours.get(sym, []))
        except Exception as ex:
            e = {"symbol": sym, "kind": "stock", "state": "not protectable", "lots": [],
                 "reason": f"The check of this position failed ({type(ex).__name__}); nothing was changed."}
        ctx.entries.append(e)


def _stock_symbol(ctx, sym, pos, ours) -> dict:
    c = ctx.c
    e = {"symbol": sym, "kind": "stock", "lots": []}
    if pos is None:                                     # Mason still has an order on a position that is gone
        _p, err = _get(f"/positions/{sym}")
        if err != "not_found":                          # only the broker's plain "no such position" counts
            e.update(state="not protectable", reason=f"{sym} is missing from the broker's position list but the broker "
                                                     f"did not confirm it is closed. Nothing was changed.")
            return e
        for r in ours:
            _cancel(ctx, r, "the position is closed")
        e.update(state="closed", reason="The position is closed. Mason's leftover protective order is being withdrawn.")
        return e
    qty = _f(pos.get("qty")) or 0.0
    side = str(pos.get("side") or ("short" if qty < 0 else "long")).lower()
    cur = _f(pos.get("current_price"))
    e.update(qty=abs(qty), price=cur)
    if side != "long" or qty <= 0:
        e.update(held_by="you", state="not protectable",
                 reason="This is a short position. Mason leaves short positions alone.")
        return e
    if ctx.c.get("withdraw_all"):
        for r in ours:
            _cancel(ctx, r, "protect.withdraw_all is switched on")
    M = _mason_qty(sym)
    if M is None:
        e.update(held_by="you and Mason", state="not protected yet",
                 reason="Mason's own quantity in this symbol is not settled yet. Checked again at the next pass.")
        return e
    if M > qty + _EPS:
        e.update(held_by="Mason", mason_qty=M, state="not protectable",
                 reason=f"Mason's own records show {_qs(M)} shares of its own but the broker holds only {_qs(qty)}. "
                        f"Nothing is placed until that is sorted out.")
        return e
    N = qty - M
    e.update(mason_qty=M, nico_qty=N, held_by=("Mason" if N <= _EPS else ("you and Mason" if M > _EPS else "you")))
    if M > _EPS:
        e["mason_note"] = f"{_qs(M)} of these shares belong to Mason's own trade, which has its own exit orders."
    if N <= _EPS:
        for r in ours:
            _cancel(ctx, r, "none of these shares are yours any more")
        e.update(state="Mason's own", reason="All of these shares belong to Mason's own trade, which has its own "
                                             "exit orders.")
        return e

    lots, problem = _nico_lots(ctx, sym)
    q = _quote(ctx, sym)
    px = q.get("px")
    if px is not None and cur and abs(px / cur - 1.0) * 100.0 > _num(c, "price_sanity_pct"):
        q = {"px": None, "why": f"two price sources disagree ({_px(px)} against the broker's {_px(cur)})"}
        px = None
    e["price"] = px if px is not None else cur
    doubt = ctx.doubt.get(sym) or (f"The journal cannot be used for {sym}: {problem}." if problem else None)

    # who holds the shares at the broker right now
    sells = [u for u in ctx.units if u["symbol"] == sym and u["side"] == "sell"]
    plain = [u for u in sells if not u["coid"].startswith((STOP_PREFIX, "mason-book-", "mason-exit-", "mason-dir-",
                                                           "mason-park-"))]      # mason-park- = the idle-cash rule
    mids = set()
    if plain:
        got = _mason_order_ids(sym)
        if got is None:
            doubt = doubt or f"Mason's own orders on {sym} could not be read at the broker."
        else:
            mids = got
    who = {}
    for u in sells:
        if u["coid"].startswith(STOP_PREFIX):
            who[u["id"]] = "ours"
        elif u["coid"].startswith("mason-book-"):
            who[u["id"]] = "book"
        elif u["coid"].startswith(("mason-exit-", "mason-dir-", "mason-park-")) or u["id"] in mids:
            who[u["id"]] = "mason"
        else:
            who[u["id"]] = "owner"
    ours_rem = sum(u["rem"] for u in sells if who[u["id"]] == "ours")
    owner = [u for u in sells if who[u["id"]] == "owner"]
    owner_held = sum(u["rem"] for u in owner)
    book_held = sum(u["rem"] for u in sells if who[u["id"]] == "book")
    avail = _f(pos.get("qty_available"))
    if avail is None:
        doubt = doubt or "The broker did not say how many shares are free of other orders."
        avail = 0.0
    mason_held = (qty - avail) - ours_rem - owner_held - book_held
    if not doubt and (mason_held < -_EPS or mason_held > M + _EPS):
        doubt = (f"The broker is holding {_qs(qty - avail)} {sym} shares for sell orders and Mason cannot account "
                 f"for all of them.")
    nico_free = max(0.0, min(avail, N - ours_rem - owner_held - book_held))

    # an order of ours that the table does not know (e.g. the table was restored): adopt it
    known = {r.get("order_id") for r in ours} | {r.get("coid") for r in ours}
    for u in sells:
        if who[u["id"]] == "ours" and u["id"] not in known and u["coid"] not in known and u["stop"]:
            row = {"id": uuid.uuid4().hex[:10], "kind": "stock_stop", "symbol": sym,
                   "lot_key": _lot_key(sym, _round_px(u["stop"])), "journal_ids": "[]", "qty": u["qty"],
                   "stop": _round_px(u["stop"]), "limit_px": u["limit"], "order_type": u["type"], "coid": u["coid"],
                   "order_id": u["id"], "state": "working", "filled_qty": u["filled"], "booked_qty": u["filled"],
                   "filled_notional": 0.0, "note": "adopted from the broker"}
            if ctx.act:
                try:
                    _exec("INSERT INTO protect_orders(id,kind,symbol,lot_key,journal_ids,qty,stop,limit_px,order_type,"
                          "coid,order_id,state,filled_qty,booked_qty,note,created_at,updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (row["id"], row["kind"], sym, row["lot_key"], "[]", row["qty"], row["stop"], row["limit_px"],
                           row["order_type"], row["coid"], row["order_id"], "working", row["filled_qty"],
                           row["booked_qty"], row["note"], _nowiso(), _nowiso()))
                except sqlite3.IntegrityError:          # the table had written this order off: take it back
                    _exec("UPDATE protect_orders SET state='working', order_id=?, closed_at=NULL, updated_at=?, "
                          "note='found again at the broker' WHERE coid=?", (u["id"], _nowiso(), u["coid"]))
                    back = _rows("SELECT * FROM protect_orders WHERE coid=?", (u["coid"],))
                    row = back[0] if back else row
                except sqlite3.Error:
                    pass
            ours.append(row)

    lots = lots or []
    J = sum(t["qty"] for t in lots)
    groups = {}
    for t in lots:
        g = groups.setdefault(_lot_key(sym, t["stop"]), {"stop": t["stop"], "qty": 0.0, "ids": [], "lots": [],
                                                         "order": None})
        g["qty"] += t["qty"]
        g["ids"].append(t["id"])
        g["lots"].append(t)
    if not doubt and J - N > _EPS:
        doubt = (f"The journal shows {_qs(J)} {sym} shares of yours but the broker holds {_qs(N)} for you. Until the "
                 f"journal matches the broker (ask Mason to reconcile the journal), Mason cannot tell which stop "
                 f"belongs to the shares that are left.")

    # match Mason's orders to the lots they were placed for
    for r in ours:
        if r["state"] not in _LIVE:
            continue
        g = groups.get(r.get("lot_key"))
        if doubt:
            continue                                    # in doubt nothing is withdrawn either
        if g is None or g["stop"] is None:
            _cancel(ctx, r, "the stop recorded in the journal changed, or that lot is closed")
        elif g["order"] is not None:
            _cancel(ctx, r, "a second order for the same lot")
        else:
            g["order"] = r

    try:
        busy = bool(_AL()._q("SELECT 1 FROM agent_trades WHERE source='mason' AND status='open' AND occ IS NULL AND "
                             "UPPER(ticker)=? AND (exit_pending_reason IS NOT NULL OR exit_order_id IS NOT NULL) "
                             "LIMIT 1", (sym,)))
        booking = bool(_AL()._q("SELECT 1 FROM agent_book_actions WHERE UPPER(symbol)=? AND state IN "
                                "('pending','working') LIMIT 1", (sym,)))
    except Exception:
        busy = booking = True
    # "hold": Mason's order at this stop was cancelled outside Mason, so that level is not put back unasked.
    # A different level recorded in the journal since then is a new instruction and is not held back.
    hold = _flag_get(f"hold:{sym}")
    held_stop = None
    if hold:
        try:
            held_stop = _f(json.loads(hold).get("stop"))
        except Exception:
            held_stop = None
        if held_stop is not None and not any(g["stop"] is not None and abs(g["stop"] - held_stop) < 1e-9
                                             for g in groups.values()):
            if ctx.act and not doubt:
                _flag_clear(f"hold:{sym}")              # that level is no longer in the journal: nothing to hold
            hold = None
    margin = max(0.0, _num(c, "stop_margin_pct"))
    undecided = []

    for key in sorted(groups, key=lambda k: -(groups[k]["stop"] or 0.0)):
        g = groups[key]
        whole = int(math.floor(g["qty"] + 1e-9))
        tgt = next((t["target"] for t in g["lots"] if t["target"]), None)
        lot = {"qty": g["qty"], "recorded_stop": g["stop"], "target": tgt, "order": None}
        e["lots"].append(lot)
        r = g["order"]
        if r is not None and r["state"] == "working":
            rem = float(r.get("qty") or 0) - float(r.get("filled_qty") or 0)
            unbooked = float(r.get("filled_qty") or 0) > float(r.get("booked_qty") or 0) + _EPS
            if abs(rem - whole) > _EPS and not doubt and not unbooked:
                _cancel(ctx, r, f"the number of shares changed (the order was for {_qs(rem)}, the lot is {_qs(whole)})")
                lot.update(state="not protected yet",
                           state_text="The number of shares changed; Mason is replacing its protective order.")
                continue
            lot["order"] = {"id": r.get("order_id"), "stop": r.get("stop"), "limit": r.get("limit_px"),
                            "qty": rem, "placed_by": "Mason", "type": r.get("order_type")}
            lot.update(state="protected",
                       state_text=f"A standing order at the broker sells {_qs(rem)} shares if the price falls to "
                                  f"{_px(r.get('stop'))}" + (f", but not for less than {_px(r.get('limit_px'))}."
                                                             if r.get("limit_px") else "."))
            if px is not None and px < float(r.get("stop") or 0):
                lot["state_text"] = (f"The price ({_px(px)}) fell through the stop of {_px(r.get('stop'))} without "
                                     f"the order selling. It is still in place.")
                _gap_note(ctx, sym, r, px, rem)
            if g["qty"] - whole > _EPS:
                lot["state_text"] += (f" {_qs(g['qty'] - whole)} of a share is not covered (a part of a share cannot "
                                      f"carry a standing order).")
            continue
        if r is not None:                               # placing / cancelling: in transition
            lot.update(state="not protected yet", state_text="Mason's order for this lot is being changed at the broker.")
            continue
        why = None
        if doubt:
            lot.update(state="not protectable", state_text=doubt)
            continue
        if owner:
            covered = sum(u["rem"] for u in owner if u["has_stop"])
            desc = ", ".join(f"{u['type'].replace('_', ' ')} for {_qs(u['rem'])}"
                             + (f" at {_px(u['stop'] or u['limit'])}" if (u['stop'] or u['limit']) else "")
                             for u in owner[:3])
            if covered + _EPS >= N - ours_rem:
                lot.update(state="protected", order={"placed_by": "you", "qty": covered},
                           state_text=f"You have your own stop order on {sym} ({desc}). Mason leaves it alone.")
            else:
                lot.update(state="not protectable",
                           state_text=f"You have your own sell order on {sym} ({desc}). The broker allows one sell "
                                      f"order per share, so Mason places nothing next to it.")
            continue
        if g["stop"] is None:
            lot.update(state="waiting for your decision", state_text="No stop is recorded for these shares.")
            undecided.append(g)
            continue
        if hold and (held_stop is None or abs(g["stop"] - held_stop) < 1e-9):
            lot.update(state="waiting for your decision",
                       state_text="Mason's protective order was cancelled outside Mason. It is not put back until "
                                  "you say so.")
            continue
        if whole < 1:
            lot.update(state="not protectable", state_text="Less than one whole share; a standing stop order needs "
                                                           "whole shares.")
            continue
        if any(x.get("state") in ("placing", "cancelling") for x in ours):
            why = (f"Mason's earlier protective order on {sym} is still being changed at the broker. The new one "
                   f"follows at the next check.")
        elif not ctx.market_ok:
            why = ctx.market_note
        elif px is None:
            why = f"No reliable price for {sym} right now ({q.get('why')}). Tried again at the next check."
        elif px <= g["stop"]:
            lot.update(state="waiting for your decision",
                       state_text=f"The price ({_px(px)}) is at or below the recorded stop of {_px(g['stop'])}. "
                                  f"Nothing is sold and no order is placed that would sell straight away.")
            undecided.append(g)
            continue
        elif px <= g["stop"] * (1.0 + margin / 100.0):
            why = (f"The price ({_px(px)}) is within {margin:g}% of the stop ({_px(g['stop'])}). Mason does not place "
                   f"a stop that close; it looks again at the next check.")
        elif busy or booking:
            why = f"Mason is selling {sym} shares right now; the protective order waits until that is finished."
        else:
            why = _buy_conflict(ctx, sym, g["stop"]) or _recent_reject(ctx, key)
        if why is None and nico_free + _EPS < whole:
            lot.update(state="not protectable",
                       state_text=f"Only {_qs(nico_free)} of your {_qs(whole)} shares are free of other orders at the "
                                  f"broker.")
            continue
        if why:
            lot.update(state="not protected yet", state_text=why)
            continue
        res, text = _place(ctx, sym, g, whole, px)
        if res:
            nico_free -= whole
            lot["order"] = {"id": res.get("order_id"), "stop": res["stop"], "limit": res.get("limit_px"),
                            "qty": whole, "placed_by": "Mason", "type": res["order_type"]}
            lot.update(state="protected", state_text=text)
        else:
            lot.update(state="not protected yet", state_text=text)

    if N - J > _EPS:
        text = (f"{_qs(N - J)} of your {sym} shares are not in the journal, so there is no recorded stop for them. "
                f"Ask Mason in the chat to add them to the journal.")
        other = [x for x in ctx.skipped.get(sym, []) if x["qty"] > 0]
        other = [x for x in other if abs(x["qty"] - (N - J)) < _EPS] or other      # the row of exactly that size first
        if other:
            rows_txt = ", ".join(f"a row for {_qs(x['qty'])} share{'s' if x['qty'] != 1 else ''} ({x['id']}, its "
                                 f"text says \"{x['word']}\")" for x in other[:3])
            text = (f"{_qs(N - J)} of your {sym} shares have no journal row that Mason can use. The journal does have "
                    f"{rows_txt}, which Mason reads as belonging to another account and never touches. If that row "
                    f"is this Alpaca position, reword its notes so they no longer name the other account; Mason then "
                    f"treats it like your other positions.")
            _note(ctx, f"unjournaled:{sym}", "unjournaled", sym,
                  f"{sym}: {_qs(N - J)} of your shares have no stop and Mason cannot suggest one yet",
                  text + f"\nUntil then nothing at the broker limits a fall in these {_qs(N - J)} shares"
                  + (f" (now {_px(e.get('price'))} each)." if e.get("price") else "."),
                  severity="warn", every=int(_num(c, "info_every_trading_days")))
        elif N - J >= 1.0 - _EPS:
            # REVIEW FIX 9: shares Nico added by hand (the journal sync adds no row to a symbol that already has
            # one) or a position with no row at all were left unprotected with no word in the inbox.
            how = ("Ask Mason in the chat to reconcile the journal; the position then gets a row and Mason suggests a "
                   "stop." if J <= _EPS else
                   "If you bought more by hand, ask Mason in the chat to add those shares to the journal with the stop "
                   "you want. The usual journal sync does not add shares to a symbol that already has a row.")
            _note(ctx, f"unjournaled:{sym}", "unjournaled", sym,
                  f"{sym}: {_qs(N - J)} of your shares are not in the journal, so they have no stop",
                  f"The broker holds {_qs(N)} {sym} shares for you and the journal has {_qs(J)}. Mason protects, and "
                  f"suggests stops for, only the shares that are in the journal, so nothing at the broker limits a "
                  f"fall in the other {_qs(N - J)}" + (f" (now {_px(e.get('price'))} each)." if e.get("price") else ".")
                  + f"\n{how}", severity="warn", every=int(_num(c, "info_every_trading_days")))
        e["lots"].append({"qty": N - J, "recorded_stop": None, "target": None, "order": None,
                          "state": "not protectable", "state_text": text})
    stuck = f"mismatch:{sym}"
    if doubt and (problem or J - N > _EPS) and not owner and not any(r.get("state") == "working" for r in ours):
        # REVIEW FIX 9: e.g. after the director sold part of the position Mason's stop was taken off for the sale
        # and never came back (the journal still showed the old quantity), with no note. One warning, rate-limited,
        # and only once the mismatch has lasted (a sale of Mason's own shares is booked a few minutes after it fills).
        since = _flag_get(stuck)
        if not since:
            if ctx.act:
                _flag_set(stuck, _nowiso())
        elif _age_s(since) >= 20 * 60:
            _note(ctx, f"unprotected:{sym}", "unprotected", sym,
                  f"{sym}: your {_qs(N)} shares have no stop at the broker and Mason cannot place one",
                  f"{doubt}\nNothing at the broker limits a fall in these shares until that is sorted out"
                  + (f" (now {_px(e.get('price'))} each)." if e.get("price") else "."),
                  severity="warn", every=int(_num(c, "info_every_trading_days")))
    elif ctx.act and not doubt and _flag_get(stuck):
        _flag_clear(stuck)
    if hold and not doubt and not owner:
        _hold_note(ctx, sym, N, hold)
    if undecided and not doubt and not hold and not owner:
        _propose(ctx, sym, undecided, px, cur)
    for g in groups.values():                           # through the recorded target
        tgt = next((t["target"] for t in g["lots"] if t["target"]), None)
        if tgt and px is not None and px >= tgt:
            _target_note(ctx, sym, g, tgt, px)
    _roll_up(e)
    return e


def _roll_up(e):
    states = [l.get("state") for l in e["lots"]]
    order = ("waiting for your decision", "not protectable", "not protected yet", "protected")
    pick = next((s for s in order if s in states), None)
    if pick is None:
        e.update(state="not protectable", reason="No journal row was found for these shares.")
        return
    if pick == "protected" and any(s != "protected" for s in states):
        pick = "not protected yet"
    e["state"] = pick
    e["reason"] = next(l.get("state_text") for l in e["lots"] if l.get("state") == pick)


def _buy_conflict(ctx, sym, stop):
    """The broker refuses a sell stop next to a buy order of the same account that it could meet."""
    limit = _limit_for(stop, ctx.c) if str(ctx.c.get("order_type")).lower() != "stop" else None
    for u in ctx.units:
        if u["symbol"] != sym or u["side"] != "buy" or u["cls"] in ("bracket", "oco", "oto"):
            continue
        if limit is None or u["type"] != "limit" or u["limit"] is None or u["limit"] >= limit:
            return (f"A buy order for {sym} is working at the broker. The broker does not allow a sell stop next to "
                    f"it, so the protective order waits until that buy order is filled or cancelled.")
    return None


def _recent_reject(ctx, key):
    try:
        r = _rows("SELECT state, closed_at, last_error FROM protect_orders WHERE lot_key=? ORDER BY created_at DESC, "
                  "rowid DESC LIMIT 1", (key,))
        if r and r[0]["state"] == "rejected" and _age_s(r[0]["closed_at"]) < 60.0 * _num(ctx.c, "retry_rejected_min"):
            return (f"The broker refused this order a short while ago ({str(r[0]['last_error'] or 'no reason given')[:120]}). "
                    f"Mason tries again later.")
    except Exception:
        return "Mason's own order table could not be read."
    return None


def _place(ctx, sym, g, whole, px):
    """Place one protective order. Returns (row | None, plain words)."""
    c = ctx.c
    stop = g["stop"]
    typ = "stop" if str(c.get("order_type") or "").lower() == "stop" else "stop_limit"
    limit = _limit_for(stop, c) if typ == "stop_limit" else None
    words = (f"sell {whole} {sym} if the price falls to {_px(stop)}"
             + (f", but not for less than {_px(limit)}" if limit else ""))
    plan = {"do": "place", "symbol": sym, "qty": whole, "stop": stop, "limit": limit, "type": typ, "done": False}
    ctx.actions.append(plan)
    if not ctx.live:
        return None, (f"Mason would place: {words}. "
                      + ("(Report only.)" if not ctx.act else
                         ("Mason's protective orders are being withdrawn (protect.withdraw_all)."
                          if c.get("withdraw_all") else "Placing orders is switched off (protect.place_orders).")))
    if ctx.placed >= int(_num(c, "max_new_orders_per_pass")):
        return None, "Enough new orders for one check; this one is placed at the next check."
    key = _lot_key(sym, stop)
    gen = _rows("SELECT COUNT(*) n FROM protect_orders WHERE lot_key=?", (key,))[0]["n"]
    coid = f"{STOP_PREFIX}{sym}-{int(round(stop * 10000))}-{whole}-{gen}"
    rid = uuid.uuid4().hex[:10]
    row = {"id": rid, "kind": "stock_stop", "symbol": sym, "lot_key": key, "qty": float(whole), "stop": stop,
           "limit_px": limit, "order_type": typ, "coid": coid, "order_id": None, "state": "placing"}
    try:                                                # written down BEFORE anything is sent
        _exec("INSERT INTO protect_orders(id,kind,symbol,lot_key,journal_ids,qty,stop,limit_px,order_type,coid,state,"
              "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "stock_stop", sym, key, json.dumps(g["ids"]), float(whole), stop, limit, typ, coid, "placing",
               _nowiso(), _nowiso()))
    except sqlite3.IntegrityError:
        return None, "This order is already being placed."
    payload = {"symbol": sym, "qty": str(whole), "side": "sell", "type": typ, "time_in_force": "gtc",
               "stop_price": _pstr(stop), "client_order_id": coid, "position_intent": "sell_to_close"}
    if limit:
        payload["limit_price"] = _pstr(limit)
    EX = _EX()
    try:
        code, data = EX._request("POST", "/orders", body=payload)
    except Exception as ex:
        code, data = None, type(ex).__name__
    if code in (200, 201) and isinstance(data, dict) and data.get("id"):
        _set_row(rid, state="working", order_id=data["id"])
        row.update(state="working", order_id=data["id"])
        ctx.placed += 1
        plan["done"] = True
        try:
            EX._audit("protect_stop_submitted", payload, {"ok": True, "order_id": data["id"], "symbol": sym})
        except Exception:
            pass
        gap = (px / stop - 1.0) * 100.0
        title = f"Protective stop placed: {sym}, sells {whole} shares if the price falls to {_px(stop)}"
        body = (f"Mason placed a standing order at the broker for the {whole} {sym} shares you entered. It stays "
                f"until it is cancelled. If the price falls to your recorded stop of {_px(stop)}, the shares are sold"
                + (f", but not for less than {_px(limit)}. If the price ever jumps below {_px(limit)} in one move, "
                   f"the order waits instead of selling and Mason tells you. It then stays in place and sells if the "
                   f"price climbs back to {_px(limit)}, unless you cancel it." if limit else ".")
                + f" {sym} is at {_px(px)} now, {gap:.1f}% above the stop.\n"
                # REVIEW FIX 7: the two things Nico has to know about a standing stop on this account
                  f"The broker triggers this order on the first trade it sees at or below {_px(stop)}, even a single "
                  f"odd one away from the market; the shares are then sold at about the market price of that moment.\n"
                  f"While this order is in place the broker refuses other plain orders in {sym} from this account, "
                  f"a sale of these shares or a new purchase, until it is cancelled. To sell or add by hand, cancel "
                  f"it at the broker first; Mason then asks before putting it back. Broker order "
                  f"{str(data['id'])[:8]}.")
        _tell(title, body, ticker=sym, dedup=f"protect:placed:{rid}")
        _activity("protect_stop_placed", title, ticker=sym,
                  extra={"order_id": data["id"], "qty": whole, "stop": stop, "limit": limit, "price": px})
        return row, f"Placed just now: {words}."
    if code is None:                                    # no answer: it may or may not be there; looked up next pass
        return None, ("The broker did not answer when the order was sent. Mason checks at the next pass whether it "
                      "arrived before trying again.")
    msg = (data.get("message") if isinstance(data, dict) else str(data)) or f"HTTP {code}"
    if code == 422 and "client_order_id" in str(msg):   # already there under this id (an earlier pass got no answer)
        return None, "The broker already has this order under the same id; Mason picks it up at the next check."
    _set_row(rid, state="rejected", closed_at=_nowiso(), last_error=str(msg)[:300])
    try:
        EX._audit("protect_stop_refused", payload, {"ok": False, "error": str(msg)[:300], "symbol": sym})
    except Exception:
        pass
    _tell(f"{sym}: the broker refused Mason's protective order",
          f"Mason tried to place a standing order for your {whole} {sym} shares ({words}) and the broker refused it: "
          f"\"{str(msg)[:200]}\". Nothing is in place for these shares. Mason tries again in about "
          f"{_num(c, 'retry_rejected_min'):g} minutes.", ticker=sym, severity="warn",
          dedup=f"protect:rejected:{sym}:{ctx.today.isoformat()}")
    return None, f"The broker refused the order ({str(msg)[:120]}). Mason tries again later."


# ── notes ────────────────────────────────────────────────────────────────────────

_GATED = ("proposal", "target", "earnings", "concentration", "unjournaled")


def _note(ctx, key, kind, symbol, title, body, *, severity="info", action=None, ref_px=None, level=None,
          every=None, once=False) -> bool:
    """One inbox note per key, rate-limited. Returns True when a note was written."""
    c = ctx.c
    if kind in _GATED and not c.get("recommend", True):
        return False
    if kind == "expiry" and not c.get("expiry_watch", True):
        return False
    try:
        last = _rows("SELECT * FROM protect_notes WHERE key=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (key,))
    except Exception:
        return False
    last = last[0] if last else None
    if last:
        if once:
            return False
        rejected = last["state"] == "rejected"
        try:
            gap = _sessions_between(date.fromisoformat(str(last["day"])[:10]), ctx.today, ctx.hol)
        except Exception:
            gap = 0
        need = int(_num(c, "after_reject_trading_days")) if rejected else int(
            every if every is not None else _num(c, "recommend_every_trading_days"))
        move = _num(c, "recommend_again_move_pct") * (2.0 if rejected else 1.0)
        moved = bool(ref_px and last.get("ref_px") and abs(ref_px / last["ref_px"] - 1.0) * 100.0 >= move)
        if gap < need and not moved:
            return False
    if not ctx.act:
        if ctx.preview:
            ctx.would_send.append({"key": key, "kind": kind, "symbol": symbol, "title": title, "body": body,
                                   "needs_your_answer": bool(action)})
        return False
    if ctx.notes_sent >= int(_num(c, "max_notes_per_pass")):
        return False
    MN = _MN()
    if last and last["state"] == "sent" and last.get("notif_id"):
        try:                                            # one live note per subject: the older one leaves the inbox
            MN.soft_delete(last["notif_id"], deleted_by="mason_protect")
        except Exception:
            pass
        _exec("UPDATE protect_notes SET state='superseded' WHERE id=?", (last["id"],))
    n = _tell(title, body, ticker=symbol, severity=severity, dedup=f"protect:{key}",
              kind="recommendation" if action else "system")
    if not n:
        return False
    _exec("INSERT INTO protect_notes(id,key,kind,symbol,notif_id,action_json,ref_px,level,day,created_at,state) "
          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (uuid.uuid4().hex[:10], key, kind, symbol, n.get("id"), json.dumps(action) if action else None,
           ref_px, level, ctx.today.isoformat(), _nowiso(), "sent"))
    ctx.notes_sent += 1
    return True


def _daily_bars(sym) -> list:
    """Completed daily bars, oldest first: [{'d','h','l','c'}]. Consolidated feed first, single venue second."""
    try:
        AT = _AT()
        now = datetime.now(timezone.utc)
        params = {"timeframe": "1Day", "adjustment": "split", "limit": 200,
                  "start": (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": (now - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        today = _now_et().date().isoformat()
        for feed in ("sip", "iex"):
            code, d = AT._data_get(f"{AT._DATA_BASE}/v2/stocks/{sym}/bars", dict(params, feed=feed))
            if code != 200 or not isinstance(d, dict):
                continue
            out = []
            for b in d.get("bars") or []:
                h, l, cl, day = _f(b.get("h")), _f(b.get("l")), _f(b.get("c")), str(b.get("t") or "")[:10]
                if h and l and cl and h >= l > 0 and day and day < today:
                    out.append({"d": day, "h": h, "l": l, "c": cl})
            if out:
                return sorted(out, key=lambda x: x["d"])
    except Exception:
        pass
    return []


def proposal_level(px, bars, c):
    """A simple, defensible stop for a position that has none: `mult` average daily ranges under the price, or
    just under the recent low when that low sits between 1 and 3 ranges under the price. None without data."""
    n, k, sd = int(_num(c, "proposal_atr_days")), _num(c, "proposal_atr_mult"), int(_num(c, "proposal_swing_days"))
    if not px or px <= 0 or n < 2 or len(bars) < n + 1:
        return None
    use = bars[-(n + 1):]
    trs = [max(use[i]["h"] - use[i]["l"], abs(use[i]["h"] - use[i - 1]["c"]), abs(use[i]["l"] - use[i - 1]["c"]))
           for i in range(1, len(use))]
    atr = sum(trs) / len(trs)
    if atr <= 0:
        return None
    swing = min(b["l"] for b in bars[-max(1, sd):])
    under = swing * 0.995
    if px - 3 * atr <= under <= px - atr:
        level, basis = under, f"just under the lowest price of the last {sd} trading days ({_px(swing)})"
    else:
        level, basis = px - k * atr, (f"{k:g} times the average daily price swing of the last {n} trading days "
                                      f"({_px(atr)}) under the current price")
    lo, hi = px * (1 - _num(c, "proposal_max_pct") / 100.0), px * (1 - _num(c, "proposal_min_pct") / 100.0)
    if level < lo:
        level, basis = lo, f"{_num(c, 'proposal_max_pct'):g}% under the current price (the stock swings a lot)"
    if level > hi:
        level, basis = hi, f"{_num(c, 'proposal_min_pct'):g}% under the current price (the stock barely moves)"
    return {"level": _round_px(level), "basis": basis, "atr": atr, "swing_low": swing}


def _propose(ctx, sym, groups, px, cur):
    """ONE note for the lots of a symbol that need Nico's decision (no stop, or already below it)."""
    if not ctx.market_ok or px is None:
        return
    c = ctx.c
    qty = sum(g["qty"] for g in groups)
    whole = int(math.floor(qty + 1e-9))
    ids = [i for g in groups for i in g["ids"]]
    cost = sum(t["qty"] * (t["entry"] or 0.0) for g in groups for t in g["lots"])
    entry = (cost / qty) if (qty > 0 and cost > 0) else None
    below = [g for g in groups if g["stop"] is not None]
    key = f"stop:{sym}"
    if ctx.act or ctx.preview:                          # bars are only fetched when a note may be due
        last = _rows("SELECT day, state, ref_px FROM protect_notes WHERE key=? ORDER BY created_at DESC, rowid DESC "
                     "LIMIT 1", (key,))
        if last and not _due(ctx, last[0], px):
            return
    else:
        return
    prop = proposal_level(px, _daily_bars(sym), c)
    level = prop["level"] if prop else None
    down = ((px / entry - 1.0) * 100.0) if entry else None
    head = f"You hold {_qs(qty)} {sym} shares"
    if entry:
        head += (f", bought at about {_px(entry)}. The price is {_px(px)} ({down:+.1f}%, "
                 f"{_signed((px - entry) * qty)}).")
    else:
        head += f". The price is {_px(px)}."
    lines = [head]
    if below and len(below) == len(groups):
        stops = ", ".join(_px(g["stop"]) for g in below)
        lines.append(f"The stop recorded in the journal is {stops}, and the price is already at or below it. Mason has "
                     f"not sold anything and does not place a stop that would sell straight away.")
        if any(px < 0.6 * g["stop"] for g in below):
            lines.append("That recorded stop is far above today's price. If the stock was split, it is simply out of "
                         "date.")
        title = f"{sym} is below your stop of {stops}. Nothing was sold"
    elif below:
        lines.append(f"Part of these shares has a recorded stop the price is already below; the rest has no stop "
                     f"recorded. Mason has not sold anything.")
        title = f"{sym}: {_qs(qty)} of your shares have no working stop"
    else:
        lines.append("No stop is recorded for them, so nothing at the broker limits a further fall.")
        title = f"{sym}: no stop on your {_qs(qty)} shares"
        if down is not None and down <= -_num(c, "down_alert_pct"):
            title += f" (down {abs(down):.0f}%)"
    limit = _limit_for(level, c) if (level and str(c.get("order_type")).lower() != "stop") else None
    if level:
        title += f"; suggested stop {_px(level)}"
        lines.append(f"Suggested stop: {_px(level)}. That is {prop['basis']}, {(1 - level / px) * 100:.1f}% under "
                     f"today's price.")
        loss = (f"If the price falls to it, the {_qs(qty)} shares are sold for about {_usd(level * qty)}: "
                f"{_usd((px - level) * qty)} less than they are worth now")
        if entry:
            diff = (level - entry) * qty
            loss += f", and {_usd(diff)} {'less' if diff < 0 else 'more'} than you paid"
        lines.append(loss + ".")
        how = (f"places a standing sell order at the broker for {whole} shares (it sells if the price falls to "
               f"{_px(level)}" + (f", but not for less than {_px(limit)})" if limit else ")"))
        if not c.get("place_orders"):
            how = "does not send an order, because placing orders at the broker is switched off"
        lines.append(f"Accept: Mason records {_px(level)} as your stop in the journal and {how}. To use a different "
                     f"price, type just the number in the note box and then press Accept.")
    else:
        lines.append("Mason could not work out a suggested level (no recent price history). To set one, type the "
                     "stop price in the note box and press Accept.")
    lines.append(f"Reject: leave it as it is. Mason asks again in about {int(_num(c, 'after_reject_trading_days'))} "
                 f"trading days, or sooner after a big move.")
    sev = "warn" if (below or (down is not None and down <= -_num(c, "down_alert_pct"))) else "info"
    _note(ctx, key, "proposal", sym, title, "\n".join(lines), severity=sev, ref_px=px, level=level,
          action={"type": "set_stop", "symbol": sym, "journal_ids": ids, "level": level, "qty": qty})


def _due(ctx, last, px) -> bool:
    c = ctx.c
    rejected = last["state"] == "rejected"
    try:
        gap = _sessions_between(date.fromisoformat(str(last["day"])[:10]), ctx.today, ctx.hol)
    except Exception:
        return False
    need = int(_num(c, "after_reject_trading_days")) if rejected else int(_num(c, "recommend_every_trading_days"))
    move = _num(c, "recommend_again_move_pct") * (2.0 if rejected else 1.0)
    moved = bool(px and last.get("ref_px") and abs(px / last["ref_px"] - 1.0) * 100.0 >= move)
    return gap >= need or moved


def _hold_note(ctx, sym, qty, hold):
    try:
        h = json.loads(hold)
    except Exception:
        h = {}
    _note(ctx, f"hold:{sym}", "hold", sym,
          f"{sym}: Mason's protective order was cancelled outside Mason and has not been put back",
          f"The standing order Mason had placed on your {sym} shares (stop {_px(h.get('stop'))}) was cancelled at the "
          f"broker, not by Mason. Mason takes that as your decision and has not put it back, so the {_qs(qty)} "
          f"shares you hold now have no stop at the broker.\n"
          f"Accept: Mason protects the shares you hold now again, at the stops recorded in the journal.\n"
          f"Reject: leave them as they are.",
          severity="warn", action={"type": "resume", "symbol": sym})


def _gap_note(ctx, sym, r, px, rem):
    lim = r.get("limit_px")
    if lim and px >= float(lim):
        # REVIEW FIX 7: between the stop and the lowest accepted price nothing has "dropped past" anything; the
        # old text told Nico the order would only sell on the way back UP, which is wrong here.
        _note(ctx, f"late:{r['id']}", "gap", sym,
              f"{sym} is below your stop of {_px(r.get('stop'))} and the protective order has not sold yet",
              f"{sym} is at {_px(px)}, below your stop of {_px(r.get('stop'))}, by the prices Mason trusts. The "
              f"protective order at the broker has not sold yet: the broker triggers it on its own price data, on the "
              f"first trade it sees at or below {_px(r.get('stop'))}, and then sells your {_qs(rem)} shares for "
              f"{_px(lim)} or better. It is still in place and can sell at any moment. Mason is not doing anything "
              f"else on its own.", severity="warn", ref_px=px, once=True)
        return
    if lim:
        body = (f"{sym} is at {_px(px)}, below your stop of {_px(r.get('stop'))}. The protective order only sells at "
                f"{_px(lim)} or better, and the price dropped past that in one move, so nothing has been sold. The "
                f"order is still in place: it sells your {_qs(rem)} shares if the price comes back up to {_px(lim)}.\n"
                f"If you would rather keep the shares, cancel that order at the broker; Mason then asks before "
                f"placing another. If you want out at today's price, cancel it and sell them yourself. Mason does "
                f"neither on its own.")
    else:
        body = (f"{sym} is at {_px(px)}, below your stop of {_px(r.get('stop'))}, and the protective order has not "
                f"sold yet. It is still in place at the broker. Mason is not doing anything else on its own.")
    _note(ctx, f"gap:{r['id']}", "gap", sym,
          f"{sym} fell through your stop of {_px(r.get('stop'))} and the protective order has not sold",
          body, severity="warn", ref_px=px, once=True)


def _target_note(ctx, sym, g, tgt, px):
    qty = g["qty"]
    cost = sum(t["qty"] * (t["entry"] or 0.0) for t in g["lots"])
    entry = cost / qty if (qty and cost) else None
    gain = f" That is {_signed((px - entry) * qty)} ({(px / entry - 1) * 100:+.1f}%) on your entry of {_px(entry)}." if entry else ""
    stop = (f"Your recorded stop is {_px(g['stop'])}, {(1 - g['stop'] / px) * 100:.1f}% under today's price."
            if g["stop"] else "No stop is recorded for these shares.")
    _note(ctx, f"target:{sym}:{tgt:g}", "target", sym,
          f"{sym} is above your target of {_px(tgt)} (now {_px(px)})",
          f"{sym} is at {_px(px)}, above the target of {_px(tgt)} recorded for your {_qs(qty)} shares.{gain} {stop}\n"
          f"Suggestion: decide whether to take the profit or to move the stop up so that part of it is kept. Mason "
          f"does not sell these shares by itself.",
          every=int(_num(ctx.c, "info_every_trading_days")))


# ── options Nico holds: expiry watch ─────────────────────────────────────────────

def _when(left) -> str:
    return "today" if left == 0 else ("tomorrow" if left == 1 else f"in {left} trading days")


def _options(ctx):
    acct = ctx.snap.get("account") or {}
    for p in ctx.snap["positions"]:
        sym = str(p.get("symbol") or "").upper()
        occ = parse_occ(sym)
        if not occ:
            continue
        try:
            ctx.entries.append(_option_position(ctx, sym, occ, p, acct))
        except Exception as ex:
            ctx.entries.append({"symbol": sym, "kind": "option", "state": "not protectable",
                                "reason": f"The check of this position failed ({type(ex).__name__})."})


def _option_position(ctx, sym, occ, p, acct) -> dict:
    c = ctx.c
    qty = _f(p.get("qty")) or 0.0
    side = str(p.get("side") or ("short" if qty < 0 else "long")).lower()
    u, strike, exp, right = occ["underlying"], occ["strike"], occ["expiry"], occ["right"]
    left = _sessions_between(ctx.today, exp, ctx.hol)
    e = {"symbol": sym, "name": _words(sym), "kind": "option", "qty": abs(qty), "underlying": u, "strike": strike, "right": right,
         "expiry": exp.isoformat(), "trading_days_left": left, "price": _f(p.get("current_price"))}
    if side != "long" or qty <= 0:
        e.update(held_by="you", state="not protectable", reason="This is a short option position. Mason leaves it alone.")
        return e
    mq = _mason_option_qty(sym)
    if mq is None:
        e.update(held_by="you and Mason", state="not protected yet",
                 reason="Mason's own quantity in this contract is not settled yet.")
        return e
    nq = qty - mq
    e.update(mason_qty=mq, nico_qty=max(0.0, nq), held_by=("Mason" if nq <= _EPS else ("you and Mason" if mq > _EPS else "you")))
    if nq <= _EPS:
        e.update(state="Mason's own", reason="Mason's own trade. Its exits are handled inside Mason, not by an order "
                                             "at the broker.")
        return e
    e.update(state="not protectable",
             reason=f"Mason places no stop orders on options and never sells yours on its own. It watches the expiry "
                    f"date ({exp.isoformat()}, {_when(left)}) and sends you a note before it.")
    if exp < ctx.today or not ctx.market_ok:
        return e
    pxu = _quote(ctx, u).get("px")
    if pxu is None:
        return e
    call = right == "call"
    itm_by = (pxu - strike) if call else (strike - pxu)
    itm = itm_by >= 0.01
    shares = 100.0 * nq
    cost = strike * shares
    cash, bp = _f(acct.get("cash")), _f(acct.get("buying_power"))
    short_cash = bool(call and itm and cash is not None and cost > cash)
    stage = None
    if left == 0:
        stage = "last"
    elif left == 1:
        stage = "eve"
    elif left <= int(_num(c, "expiry_first_note_trading_days")):
        stage = "first"
    elif left <= int(_num(c, "expiry_early_note_trading_days")) and short_cash:
        stage = "early"
    e["if_nothing_is_done"] = _expiry_outcome(u, strike, exp, call, itm, itm_by, pxu, shares, cost, cash, bp, ctx, nq)
    if not stage:
        return e
    mark, paid = _f(p.get("current_price")), _f(p.get("avg_entry_price"))
    limit = None
    try:
        AT = _AT()
        oq = AT._trusted_quote(sym, True, AT._at_cfg())
        limit, _basis = AT._MQ().sell_limit(oq)
        limit = _floor_tick(sym, limit) if limit else None
    except Exception:
        limit = None
    kind = "calls" if call else "puts"
    when = exp.strftime("%a %b %d").replace(" 0", " ")
    worth = f"They are worth about {_px(mark)} each now ({_usd(mark * shares)} in total" if mark else ""
    if worth:
        worth += f"; you paid {_usd(paid * shares)})." if paid else ")."
    lines = [f"You hold {_qs(nq)} {u} {kind[:-1]} contract(s), strike {_px(strike)}, expiring {when} ({_when(left)}). "
             f"{u} is at {_px(pxu)}. {worth}".strip(), e["if_nothing_is_done"]]
    if itm and short_cash:
        title = (f"{u} {kind} expire {when} ({_when(left)}): exercising costs {_usd(cost)}, the account has "
                 f"{_usd(cash)} in cash")
        lines.append("Suggestion: sell the contracts before the last day, unless you want to own the shares and are "
                     "content to borrow for them.")
    elif itm:
        title = f"{u} {kind} expire {when} ({_when(left)}): at today's price they would be exercised"
        lines.append("Suggestion: decide before the last day whether you want that to happen. If not, sell the "
                     "contracts.")
    else:
        title = f"{u} {kind} expire {when} ({_when(left)}): at today's price they would expire worthless"
        lines.append("Suggestion: nothing has to be done. Selling now keeps what they are still worth; holding risks "
                     "that amount for the chance that the price gets there in time.")
    at = f"at {_px(limit)} each or better" if limit else "at a price close to the current market price"
    more = {"early": f"Mason reminds you again {int(_num(c, 'expiry_first_note_trading_days'))} trading days before "
                     f"they expire and on the last two days.",
            "first": "Mason reminds you again on the last two days.",
            "eve": "Mason reminds you once more on the last day.", "last": "This is the last reminder."}[stage]
    lines.append(f"Accept: Mason sells all {_qs(nq)} contract(s) now, {at} (only while the market is open). "
                 f"Reject: keep them. {more}")
    sev = "warn" if (short_cash or stage in ("eve", "last")) else "info"
    _note(ctx, f"expiry:{sym}:{stage}", "expiry", u, title, "\n".join(lines), severity=sev, once=True,
          action={"type": "close_option", "occ": sym, "qty": nq, "limit": limit, "mark": mark})   # REVIEW FIX 6
    return e


def _expiry_outcome(u, strike, exp, call, itm, itm_by, pxu, shares, cost, cash, bp, ctx, nq) -> str:
    day = exp.strftime("%b %d").replace(" 0", " ")
    if not itm:
        way = "above" if call else "below"
        return (f"If nothing is done and {u} is not {way} {_px(strike)} at the close on {day}, the contracts expire "
                f"worthless and what they are worth now is lost. Today {u} is {_px(abs(itm_by))} away from that.")
    if call:
        s = (f"If nothing is done and {u} is still above {_px(strike)} at the close on {day}, the broker exercises "
             f"them: the account buys {_qs(shares)} {u} shares at {_px(strike)}, which costs {_usd(cost)}.")
        if cash is None:
            return s + " Mason could not read the account's cash to compare."
        if cost <= cash:
            return s + f" The account has {_usd(cash)} in cash, enough to pay for that."
        if bp is not None and cost <= bp:
            return s + (f" The account has only {_usd(cash)} in cash, so about {_usd(cost - cash)} would have to be "
                        f"borrowed from the broker (today the account could buy up to {_usd(bp)} in total, its "
                        f"own cash plus what the broker lends). If the broker decides the account cannot carry "
                        f"that, it sells the contracts itself in the last hour before they expire, at whatever "
                        f"price it gets.")
        return s + (f" The account cannot pay for that: it has {_usd(cash)} in cash and, with what the broker "
                    f"lends, could buy {_usd(bp) if bp is not None else 'an unknown amount'} at most. The broker "
                    f"then sells the contracts itself in the last hour before they expire, at whatever price it "
                    f"gets.")
    held = _position_qty(ctx, u)
    s = (f"If nothing is done and {u} is still below {_px(strike)} at the close on {day}, the broker exercises them: "
         f"the account sells {_qs(shares)} {u} shares at {_px(strike)}.")
    if held + _EPS >= shares:
        return s + f" You hold {_qs(held)} {u} shares, so those would be sold."
    return s + (f" The account holds only {_qs(held)} {u} shares, so the broker will most likely sell the contracts "
                f"itself in the last hour before they expire, at whatever price it gets.")


# ── fixed suggestions: earnings close, one position too large ────────────────────

def _next_earnings(sym, today):
    try:
        r = _rows("SELECT report_date, timing FROM earnings_results WHERE UPPER(ticker)=? AND report_date>=? AND "
                  "eps_act IS NULL ORDER BY report_date LIMIT 1", (sym, today.isoformat()))
        if r and r[0]["report_date"]:
            return date.fromisoformat(str(r[0]["report_date"])[:10]), str(r[0]["timing"] or "").lower()
    except Exception:
        pass
    return None


def _suggestions(ctx):
    if not ctx.market_ok or not ctx.c.get("recommend", True):
        return
    c = ctx.c
    eq = _f((ctx.snap.get("account") or {}).get("equity"))
    within = int(_num(c, "earnings_within_trading_days"))
    seen = set()
    for e in ctx.entries:
        if (e.get("nico_qty") or 0) <= _EPS:
            continue
        sym = e.get("underlying") or e["symbol"]
        px = _quote(ctx, sym).get("px")
        if e["kind"] == "stock" and px and eq and eq > 0:
            share = e["qty"] * px / eq * 100.0
            if share >= _num(c, "concentration_pct"):
                _note(ctx, f"concentration:{sym}", "concentration", sym,
                      f"{sym} is {share:.0f}% of the account",
                      f"{sym} is worth {_usd(e['qty'] * px)} of the account's {_usd(eq)}, which is {share:.1f}%. Above "
                      f"{_num(c, 'concentration_pct'):g}% in one name, one bad day in it moves the whole account: a 10% "
                      f"fall in {sym} would cost about {_usd(e['qty'] * px * 0.10)}.\n"
                      f"Suggestion: decide whether that size is what you want. Mason does not sell these shares by "
                      f"itself.", every=int(_num(c, "info_every_trading_days")))
        if sym in seen:
            continue
        seen.add(sym)
        ne = _next_earnings(sym, ctx.today)
        if not ne:
            continue
        d, timing = ne
        left = _sessions_between(ctx.today, d, ctx.hol)
        if left > within:
            continue
        tm = {"am": " before the market opens", "pm": " after the market closes"}.get(timing, "")
        held = (f"{_qs(e['nico_qty'])} shares" if e["kind"] == "stock"
                else f"{_qs(e['nico_qty'])} option contract(s) expiring {e.get('expiry')}")
        stops = [l for l in e.get("lots", []) if l.get("state") == "protected"]
        cover = (" A standing stop order is in place, but it cannot protect against a jump while the market is closed: "
                 "it sells at the next price available, and not at all if that price is below its lowest accepted "
                 "price." if stops else
                 (" There is no stop at the broker for them." if e["kind"] == "stock" else ""))
        _note(ctx, f"earnings:{sym}:{d.isoformat()}", "earnings", sym,
              f"{sym} reports earnings {d.strftime('%a %b %d').replace(' 0', ' ')} ({_when(left)})",
              f"{sym} reports earnings on {d.strftime('%A %b %d').replace(' 0', ' ')}{tm}, {_when(left)}, by the "
              f"earnings calendar Mason keeps. You hold {held}. Prices often jump on earnings.{cover}\n"
              f"Suggestion: decide before then whether to hold the full size through the report.", once=True)


# ── Nico's answer to a note (called from mason_notifications.review) ─────────────

def on_review(notif_id, verdict, note=None) -> dict:
    """Accept / Reject on one of this module's notes. Returns {'text': plain words} ({} when the note carries
    no action). Never raises."""
    try:
        c = cfg()
        if not c.get("enabled"):
            # REVIEW FIX 8: an Accept pressed while protection is off used to be stored as "accepted" with no word
            # that nothing happened. Only a note that carries an action gets the sentence (read-only lookup).
            try:
                had = _rows("SELECT 1 FROM protect_notes WHERE notif_id=? AND action_json IS NOT NULL LIMIT 1",
                            (str(notif_id),))
            except Exception:
                had = []
            return ({"text": "Nothing was done: protection of the positions you entered is switched off "
                             "(protect.enabled)."} if (had and verdict == "accept") else {})
        if not ensure_schema():
            return {}
        rows = _rows("SELECT * FROM protect_notes WHERE notif_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                     (str(notif_id),))
        if not rows or not rows[0].get("action_json"):
            return {}
        row = rows[0]
        if verdict not in ("accept", "reject"):
            return {"text": "Nothing was changed."}
        if row["state"] == "done":
            return {"text": f"Already done earlier: {row.get('result') or ''}".strip()}
        if row["state"] == "superseded":
            return {"text": "This note is out of date; Mason sent a newer one. Nothing was changed."}
        if verdict == "reject":
            _exec("UPDATE protect_notes SET state='rejected', decided_at=?, result=? WHERE id=?",
                  (_nowiso(), "left as it is", row["id"]))
            if row.get("kind") == "expiry":
                return {"text": "Kept. Nothing was sold; the later reminders named in the note still come."}
            return {"text": f"Left as it is. Mason does not ask again for about "
                            f"{int(_num(c, 'after_reject_trading_days'))} trading days unless the price moves a lot."}
        if not c.get("approve_enabled", True):
            return {"text": "Nothing was done: acting on these notes is switched off (protect.approve_enabled)."}
        if not _LOCK.acquire(timeout=45):
            return {"text": "Mason is busy with a check right now. Nothing was changed; press Accept again in a minute."}
        try:
            action = json.loads(row["action_json"])
            kind = action.get("type")
            if kind == "set_stop":
                ok, text = _accept_set_stop(c, row, action, note)
            elif kind == "resume":
                ok, text = _accept_resume(c, row, action)
            elif kind == "close_option":
                ok, text = _accept_close_option(c, row, action)
            else:
                ok, text = False, "Mason does not know what this note asks for. Nothing was changed."
        finally:
            _LOCK.release()
        _exec("UPDATE protect_notes SET state=?, decided_at=?, result=? WHERE id=?",
              ("done" if ok else "sent", _nowiso(), str(text)[:500], row["id"]))
        return {"text": text}
    except Exception as e:
        return {"text": f"Something went wrong and nothing further was done ({type(e).__name__}). Check the "
                        f"protection list before trying again."}


def _typed_level(note):
    m = _NUM_RE.match(str(note or ""))
    return float(m.group(1)) if m else None


def _ref_price(sym):
    """(price, fresh): a trusted price when the market is open, else the broker's last mark (for a sanity check)."""
    AT = _AT()
    try:
        q = AT._trusted_quote(sym, False, AT._at_cfg())
        if q and q.get("ok") and not q.get("legacy") and _f(q.get("px")):
            return float(q["px"]), True
    except Exception:
        pass
    p, err = _get(f"/positions/{sym}")
    if not err and isinstance(p, dict) and _f(p.get("current_price")):
        return _f(p.get("current_price")), False
    return None, False


def _accept_set_stop(c, row, action, note):
    MT = _MT()
    sym = str(action.get("symbol") or "").upper()
    # REVIEW FIX 5: the inbox re-shows an earlier answer as "<what was typed> | <Mason's reply>", so only the part
    # before the first " | " is what Nico typed. A note that holds digits but is not one clean price ("5%",
    # "use 640", "640,5", "-640") used to be ignored and the SUGGESTED level was recorded and placed instead.
    head = str(note or "").split(" | ")[0].strip()
    typed = _typed_level(head)
    if typed is None and re.search(r"\d", head):
        return False, ("Mason could not read a stop price from the note box, so nothing was changed. Clear the box "
                       "and type only the price, for example 58.50, then press Accept again. Leave the box empty to "
                       "use the suggested price.")
    level = typed if typed is not None else _f(action.get("level"))
    if not level or level <= 0:
        return False, "There is no stop price to use. Type a price in the note box and press Accept again."
    level = _round_px(level)
    ctx = _Ctx(c, False)
    live_lots, _p = _nico_lots(ctx, sym)
    ids = set(action.get("journal_ids") or [])
    lots = [t for t in (live_lots or []) if t["id"] in ids]
    if not lots:
        return False, f"Those {sym} shares are no longer open in the journal. Nothing was changed."
    px, _fresh = _ref_price(sym)
    if px is None:
        return False, f"Mason could not get a price for {sym} to check the stop against. Nothing was changed."
    margin = max(0.0, _num(c, "stop_margin_pct"))
    if px <= level * (1.0 + margin / 100.0):
        return False, (f"{sym} is at {_px(px)}. A stop at {_px(level)} is at or too close to that and could sell at "
                       f"once. Nothing was changed. Type a lower price in the note box and press Accept again.")
    far = _num(c, "typed_max_under_pct")
    if typed is not None and far > 0 and level < px * (1.0 - far / 100.0):
        # REVIEW FIX 5: "5" typed for a $676 stock (meant as 5%) used to be recorded and placed as a $5.00 stop
        return False, (f"{sym} is at {_px(px)}. A stop at {_px(level)} is more than {far:g}% under that, which looks "
                       f"like a slip (a percentage typed instead of a price?). Nothing was changed. Type the stop as a "
                       f"price, for example {_px(px * 0.9)}, and press Accept again.")
    # The note may be old. If a stop that the price is still above has been recorded since, that newer stop is
    # the instruction; only a price typed now replaces it.
    newer = sorted({t["stop"] for t in lots if t["stop"] is not None and px > t["stop"]})
    if newer and typed is None:
        return False, (f"This note is out of date: a stop of {', '.join(_px(x) for x in newer)} is already recorded "
                       f"for these {sym} shares. Nothing was changed. To replace it, type the new price in the note "
                       f"box and press Accept again.")
    for t in lots:
        raw = MT.get_trade_by_id(t["id"])
        if not raw or (raw.get("status") or "open") != "open":
            continue
        old = _f(raw.get("stop_loss")) or 0.0
        entry = _f(raw.get("entry_price")) or 0.0
        raw["stop_loss"] = level
        if entry:
            raw["risk_per_share"] = round(entry - level, 4)
            rw = _f(raw.get("reward_per_share"))
            raw["risk_reward_ratio"] = round(rw / (entry - level), 2) if (rw and entry - level > 0) else 0
        raw["notes"] = ((raw.get("notes") or "") + f" [stop set to {_px(level)} on {date.today().isoformat()} from a "
                        f"Mason protection note, approved by Nico; it was {_px(old) if old else 'not set'}]").strip()
        MT._save_journal([raw])
    _flag_clear(f"hold:{sym}")
    _activity("protect_stop_recorded", f"{sym}: stop {_px(level)} recorded in the journal (approved by Nico)",
              ticker=sym, extra={"level": level, "journal_ids": [t["id"] for t in lots]})
    head = f"Recorded {_px(level)} as your stop for {sym} in the journal."
    return True, head + " " + _place_after_accept(c, sym, level)


def _place_after_accept(c, sym, level) -> str:
    if not c.get("place_orders"):
        return "No order was sent: placing orders at the broker is switched off (protect.place_orders)."
    out = _run_locked(c, True, False, {"enabled": True, "at": _nowiso()})
    if not out.get("ok"):
        return f"The order is not placed yet ({out.get('error')}). Mason tries at the next check."
    for e in out.get("positions") or []:
        if e.get("symbol") != sym:
            continue
        for lot in e.get("lots") or []:
            if level is None or lot.get("recorded_stop") == level:
                if lot.get("state") == "protected":
                    o = lot.get("order") or {}
                    return (f"The order is in place at the broker: {lot.get('state_text')} "
                            f"(order {str(o.get('id'))[:8]}).")
                return f"The order is not placed yet: {lot.get('state_text')}"
        return f"The order is not placed yet: {e.get('reason')}"
    return "The order is not placed yet; Mason tries at the next check."


def _accept_resume(c, row, action):
    sym = str(action.get("symbol") or "").upper()
    _flag_clear(f"hold:{sym}")
    return True, "Mason protects these shares again. " + _place_after_accept(c, sym, None)


def _accept_close_option(c, row, action):
    AT, EX = _AT(), _EX()
    occ = str(action.get("occ") or "").upper()
    if not parse_occ(occ):
        return False, "That is not an option contract. Nothing was done."
    if not c.get("place_orders") or not _paper_ok():
        return False, "Nothing was sold: placing orders at the broker is switched off (protect.place_orders)."
    if _rows("SELECT 1 FROM protect_orders WHERE kind='option_close' AND symbol=? AND state IN "
             "('placing','working','cancelling') LIMIT 1", (occ,)):
        return False, "A sale of this contract is already working at the broker. Nothing more was sent."
    clk = AT._clock()
    if not (clk.get("ok") and clk.get("is_open")):
        return False, "The market is closed, so nothing was sent. Press Accept again while the market is open."
    pos = EX.get_position(occ)
    if not pos.get("ok"):
        return False, "The broker's position could not be read, so nothing was sent. Try again in a minute."
    if not pos.get("exists") or (pos.get("side") or "long") != "long":
        sold = _rows("SELECT 1 FROM protect_orders WHERE kind='option_close' AND symbol=? AND "
                     "COALESCE(filled_qty,0) > 0 LIMIT 1", (occ,))
        return True, ("You no longer hold this contract: the sale you approved earlier went through." if sold else
                      "You no longer hold this contract. Nothing was sold.")
    mq = _mason_option_qty(occ)
    if mq is None:
        return False, "Mason's own quantity in this contract is not settled yet. Nothing was sent; try again shortly."
    qty = min(float(action.get("qty") or 0), float(pos["qty"]) - mq, float(pos.get("qty_available") or 0))
    qty = float(int(qty + 1e-9))
    if qty < 1:
        return False, "None of your contracts are free to sell right now (another order may be holding them)."
    q = AT._trusted_quote(occ, True, AT._at_cfg())
    limit, basis = AT._MQ().sell_limit(q)
    if not limit:
        return False, (f"There is no reliable price for this contract right now ({str(basis)[:120]}), so nothing "
                       f"was sent. Press Accept again later.")
    limit = _floor_tick(occ, limit)
    # REVIEW FIX 6: the note names a price ("at $18.25 each or better") but Accept may be pressed days later. A
    # sale more than option_reprice_confirm_pct under the note's price needs a second Accept, which shows the
    # price it would really go at. (Before: one click sold at $9.10 against a note that said $18.25.)
    ref, drop = _f(action.get("limit")) or _f(action.get("mark")), _num(c, "option_reprice_confirm_pct")
    if ref and drop > 0 and limit < ref * (1.0 - drop / 100.0):
        key, seen = f"confirm:{row['id']}", None
        try:
            seen = json.loads(_flag_get(key) or "null")
        except Exception:
            seen = None
        fresh = bool(seen and _age_s(seen.get("at")) <= 900 and
                     limit >= (_f(seen.get("px")) or 0.0) * (1.0 - drop / 100.0))
        if not fresh:
            _flag_set(key, json.dumps({"px": limit, "at": _nowiso()}))
            return False, (f"The price has fallen since this note was written: the note said about {_px(ref)} each, "
                           f"the market is now about {_px(limit)} each ({_usd(limit * qty * 100)} for all "
                           f"{_qs(qty)}). Nothing was sold. If you still want to sell at about {_px(limit)}, press "
                           f"Accept again within 15 minutes.")
        _flag_clear(key)
    seed = f"{CLOSE_SEED}{row['id']}-{_count_closes(occ)}"
    rid = uuid.uuid4().hex[:10]
    coid = EX._client_order_id(occ, seed, "sell", qty, limit, None, None)
    try:
        _exec("INSERT INTO protect_orders(id,kind,symbol,lot_key,journal_ids,qty,limit_px,order_type,coid,state,"
              "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "option_close", occ, f"{occ}@close", "[]", qty, limit, "limit", coid, "placing", _nowiso(),
               _nowiso()))
    except sqlite3.IntegrityError:
        return False, "This sale is already being sent. Nothing more was done."
    res = EX.close_position(occ, qty=qty, entry=limit, pitch_id=seed)
    if not res.get("ok") and res.get("client_order_id"):
        # REVIEW FIX 6: the order WAS sent and no clean yes came back (a timeout looks the same as a refusal here).
        # Ask the broker whether it has it before saying "nothing was sold". (Before: the order could arrive and
        # fill while Nico was told nothing was sold, and no later note ever corrected that.)
        sent = str(res["client_order_id"])
        o, err = _get("/orders:by_client_order_id", {"client_order_id": sent})
        if not err and isinstance(o, dict) and o.get("id"):
            res = {"ok": True, "order_id": o["id"], "client_order_id": sent, "limit_price": o.get("limit_price"),
                   "fill": {"status": o.get("status"), "filled_qty": o.get("filled_qty"),
                            "filled_avg_price": o.get("filled_avg_price")}}
        elif err != "not_found":                           # still unknown: the next check looks it up by its id
            try:
                _set_row(rid, coid=sent, last_error=str(res.get("error"))[:300])
            except sqlite3.Error:
                pass
            return False, ("The broker did not answer clearly when the sale was sent, so Mason does not know yet "
                           "whether it arrived. It checks at the next pass and tells you either way. Please do not "
                           "press Accept again before then.")
    if not res.get("ok"):
        _set_row(rid, state="rejected", closed_at=_nowiso(), last_error=str(res.get("error"))[:300])
        return False, f"The broker did not take the order ({str(res.get('error'))[:160]}). Nothing was sold."
    lim = _f(res.get("limit_price")) or limit
    try:
        _set_row(rid, state="working", order_id=res.get("order_id"), limit_px=lim,
                 coid=res.get("client_order_id") or coid)
    except sqlite3.Error:
        _set_row(rid, state="working", order_id=res.get("order_id"), limit_px=lim)
    _activity("protect_option_close_sent", f"Sale of {_qs(qty)} {_words(occ)} sent at {_px(lim)} (approved by Nico)",
              ticker=_underlying(occ), extra={"order_id": res.get("order_id"), "qty": qty, "limit": lim})
    fill = res.get("fill") or {}
    if str(fill.get("status") or "").lower() == "filled" and fill.get("filled_avg_price"):
        ctx = _Ctx(c, True)
        r = _rows("SELECT * FROM protect_orders WHERE id=?", (rid,))[0]
        _apply_status(ctx, r, {"id": res.get("order_id"), "status": "filled",
                               "filled_qty": fill.get("filled_qty") or qty,
                               "filled_avg_price": fill["filled_avg_price"]})
        return True, (f"Sold {_qs(qty)} contract(s) of the {_words(occ)} at {_px(fill['filled_avg_price'])} each "
                      f"({_usd(float(fill['filled_avg_price']) * qty * 100)} in total).")
    return True, (f"The order is at the broker: sell {_qs(qty)} contract(s) of the {_words(occ)} at {_px(lim)} each or better. It "
                  f"has not filled yet. Mason tells you when it does, and withdraws it if it has not filled within "
                  f"about {_num(c, 'option_close_wait_min'):g} minutes.")


def _floor_tick(occ, limit):
    """The limit on the contract's price step, rounded down. mason_execution rounds to the NEAREST step, which
    can lift a sell above the bid it was priced from."""
    try:
        EX = _EX()
        tick = EX._option_tick_size(float(limit), EX.get_option_contract_meta(occ).get("ppind", False),
                                    EX._underlying_from_occ(occ))
        v = round(math.floor(float(limit) / tick + 1e-9) * tick, 2)
        return v if v > 0 else limit
    except Exception:
        return limit


def _count_closes(occ) -> int:
    try:
        return int(_rows("SELECT COUNT(*) n FROM protect_orders WHERE kind='option_close' AND symbol=?", (occ,))[0]["n"])
    except Exception:
        return 0


# ── hooks for the other modules ──────────────────────────────────────────────────

def release_for_sale(symbol, why="") -> dict:
    """Take Mason's protective order(s) off `symbol` so shares Nico placed can be sold through the director's
    book action. {'wait': True} while an order of ours still holds shares; {'wait': False} when none does
    ('released': True when the last one was seen gone during this call). Works whenever such an order exists,
    also with protect.enabled off: an order left at the broker must not block a sale that was asked for."""
    try:
        sym = str(symbol or "").upper()
        try:
            had = _rows("SELECT 1 FROM protect_orders WHERE kind='stock_stop' AND symbol=? AND state IN "
                        "('placing','working','cancelling') LIMIT 1", (sym,))
        except sqlite3.Error:
            return {"wait": False}                      # no table: this module never placed anything
        if not had:
            return {"wait": False}
        if not _LOCK.acquire(timeout=20):
            return {"wait": True, "why": "busy"}
        try:
            ctx = _Ctx(cfg(), True)
            ctx.snap = {"positions": [{"symbol": sym, "qty": "1"}]}
            _refresh_orders(ctx, only_symbol=sym)       # books a fill / notices a finished cancel first
            n = 0
            for r in ctx.rows:
                if r["kind"] != "stock_stop":
                    continue
                n += 1
                if r["state"] == "working" and r.get("order_id") and _paper_ok():
                    _set_row(r["id"], state="cancelling", note=str(why or "a sale of these shares")[:300])
                    code = _send_cancel(r)
                    if code == 404:
                        _set_row(r["id"], state="gone", closed_at=_nowiso())
                    _activity("protect_stop_cancelled", f"Protective order on {sym} withdrawn for {why or 'a sale'}",
                              ticker=sym, extra={"order_id": r.get("order_id"), "http": code})
            return {"wait": True, "orders": n} if n else {"wait": False, "released": True}
        finally:
            _LOCK.release()
    except Exception as e:
        return {"wait": True, "why": f"{type(e).__name__}"}


def booked_order_ids() -> set:
    """Broker order ids of Mason's protective stock stops. Their sales are booked in this module (as a stop-out
    of the lot they covered), so the general journal sync must leave them alone."""
    try:
        if not cfg().get("enabled"):
            return set()
        return {r["order_id"] for r in _rows("SELECT order_id FROM protect_orders WHERE kind='stock_stop' AND "
                                             "order_id IS NOT NULL")}
    except Exception:
        return set()


def summary_lines(p) -> list:
    """Plain lines for the chat tool, from a run() result."""
    if not isinstance(p, dict) or not p.get("enabled"):
        if isinstance(p, dict) and p.get("orders_left_at_broker") and p.get("state_text"):
            return [str(p["state_text"])]                 # REVIEW FIX 2: off, but orders are still at the broker
        return ["Protection of the positions you entered: switched off (protect.enabled)."]
    if not p.get("ok"):
        return [f"Protection of the positions you entered: not checked ({p.get('error')})."]
    out = ["Protection of the positions you entered"
           + ("" if p.get("place_orders") else " (placing orders is switched off)") + ":"]
    for e in p.get("positions") or []:
        if e.get("state") in ("Mason's own", "closed"):
            continue
        q = e.get("nico_qty") if e.get("nico_qty") is not None else e.get("qty")
        out.append(f"  {e.get('name') or e['symbol']} ({_qs(q or 0)} yours): {e.get('state')}. "
                   f"{e.get('reason') or ''}".rstrip())
    return out


if __name__ == "__main__":
    print(json.dumps(run(act=False, preview=True), indent=1, default=str))
