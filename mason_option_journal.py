"""
mason_option_journal.py — when one of Mason's OWN option trades ends, close its row in the options journal (2026-11-18).

The defect this repairs: the exit manager (mason_agent_loop) closed Mason's own trade record (agent_trades) but never
the options journal row (options_trades) written for the same trade. Row d1dbf9a6 (SMCI 12/04 26 put, 2 contracts at
1.90) stayed "open" after Mason sold both contracts on 2026-11-17 at 3.05.

How a Mason option trade reaches the journal now
  * mason_agent_loop._finalize (every way the exit manager ends an option trade: its own sale, a sale found by the
    lost-sale guard, expiry, exercise, a sale made outside Mason, the position gone) calls before_close() just before
    it closes the trade and after_close() as its last step. before_close() writes a small "journal close due" marker
    (desk_state key optjournal_due:<trade id>) only when the trade has an open journal row of its own; after_close()
    closes that row (close_for_trade) and removes the marker. Neither ever raises into the exit path, and the trade
    is already closed when the journal is written, so a failed journal write can never stop or repeat a sale.
  * A marker that is still there (books lock busy, Alpaca unreadable, a restart in between, a refusal) is retried by
    retry_due() from the 5-minute fill reconcile (mason_execution, its own thread). The 5-minute reconcile itself never
    books an option sale (it lists them as "orphan fills"); it is only a retry point here. (review 2026-11-18) The
    15-minute self-check runs on the desk worker, next to every exit check, so it only REPORTS (selfcheck_rows: no
    Alpaca read, no write), and only with selfcheck.option_rows true. A marker that needs a person (refused) is tried
    again at most every RETRY_REFUSED_S; one retry pass spends at most about RETRY_BUDGET_S on Alpaca reads.
  * (review) The exit path's own Alpaca reads never wait on a rate limit and share EXIT_READ_BUDGET_S seconds in all
    (after at most 5 s for the books lock); whatever is left is read by the 5-minute reconcile (the marker stays).
  * Rows left open BEFORE this code existed (no marker; row d1dbf9a6) are flagged by the self-check only. They are
    fixed by the one-off tools/repair_option_exits.py, after Nico's yes.

What is written, and only from Alpaca's own records (Nico's rule, 2026-11-13)
  * Mason's own sale orders for this trade: client order id 'mason-exit-<rec id>-...' (the exit manager, the
    lost-sale guard, a chat close of a Mason trade). Read once per close: GET /orders for that contract.
  * The rest of the contracts expired worthless: an OPEXP activity for the contract with net amount 0.
  * Sold outside Mason (by hand): only when the contract is no longer held at all AND the sales of it after Mason's
    entry that carry no Mason id add up to exactly Mason's remaining contracts. Anything else is left open + flagged.
  * Expired with value / exercised (OPEXC), or the position gone with no sale found: left open + flagged (the normal
    expiry review in chat grades an exercise against the real expiry-day close).
  The row ends with the total contracts, the quantity-weighted price (contracts that expired count at 0), the last
  sale's order id / client order id / fill time, the profit or loss from the row's own premium, a status that follows
  the journal's conventions ('closed_early' = sold before expiry, written like close_play_early writes it: "Closed
  early. ..."; 'expired' = ran to expiry, in full or for the rest), and a short plain note saying why Mason sold.
  Every piece (order id, client id, contracts, price, time) is kept in the row's extra field "mason_exit".

Never the wrong row: the row must be the one written for THIS trade (its journal link, else its entry order id), made
by Mason (created_by 'mason'), on the same contract, and carry this trade's director call id when it has one. Never
by ticker, never by contract alone. Nico's rows (his own contracts, rows he logs in chat) are never matched.

Partial sales: nothing is written while Mason's trade is open. The exit manager adds every fill to the trade
(agent_trades.features_json partial_exits, written per fill, so it survives a restart); the journal row is closed once,
when the trade is closed (Mason holds none of its contracts any more), with the totals.

Switches (read on every call):
  autotrade.exits.option_journal_close   absent = on; false = the exit path and the retries write nothing (the
                                         markers too)
  selfcheck.option_rows                  absent = OFF (review 2026-11-18: new text, waits for Nico's approval);
                                         true = the self-check reports open options journal rows whose trade ended
"""

import json
import logging
import re
import time
from datetime import datetime, date, timezone

log = logging.getLogger("mason.option_journal")

DUE_PREFIX = "optjournal_due:"
EXIT_READ_BUDGET_S = 8.0           # (review) all of the exit path's Alpaca reads together, no wait on a rate limit
RETRY_BUDGET_S = 10.0              # (review) one retry pass (inside the books lock): its Alpaca reads share this
RETRY_REFUSED_S = 6 * 3600         # (review) a marker that needs a person is tried again at most this often
EXPIRY_RECORD_WAIT_DAYS = 3        # (review) no expiry record this many days after expiry: it needs a person
RECOMPUTE_ACCURACY = True          # the options accuracy ledger is recomputed after a close (the repair tool on a
                                   # database copy turns it off: that file belongs to this repo's own database)
_TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"}
_EXPIRED_KINDS = ("expired", "expired_with_value")
_REASON_WORDS = {
    "stop": "it fell to its stop",
    "target": "it reached its target",
    "time_stop": "it ran out of time (the time stop)",
    "pre_expiry": "it was the last trading day before expiry",
    "director_close": "the director closed the call",
    "flat_by": "it expired that day (the same-day cut-off)",
    "closed_outside": "it was sold outside Mason's exit rules",
}
_T_MIN = datetime.min.replace(tzinfo=timezone.utc)
_UNDERLYING_WORDS = {"stop": "the stock reached the call's stop", "target": "the stock reached the call's target"}


# ── small helpers ────────────────────────────────────────────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _conn():
    import mason_db
    return mason_db.get_connection()


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    try:
        import mason_autotrade as AT
        return AT._now_et().date()
    except Exception:
        return date.today()


def _feat(trade) -> dict:
    try:
        return dict(json.loads(trade.get("features_json") or "{}"))
    except Exception:
        return {}


def _utc(s):
    """An Alpaca time (or Mason's own local time) as an aware UTC datetime, or None."""
    t = str(s or "").strip()
    if not t:
        return None
    try:
        if t[-1] in "Zz" or re.search(r"[+-]\d\d:?\d\d$", t[10:]):
            import mason_execution as EX
            return EX._fill_time(t)
        return datetime.fromisoformat(t).astimezone(timezone.utc)          # Mason's own times are local
    except Exception:
        return None


def _et_day(s) -> str:
    """(review) The Eastern calendar day of a time (Alpaca's are UTC: 03:00Z is the evening before in New York);
    a bare date stays as it is."""
    raw = str(s or "").strip()
    if len(raw) <= 10:
        return raw[:10]
    t = _utc(raw)
    if t is None:
        return raw[:10]
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return raw[:10]


def _age_s(iso) -> float:
    """Seconds since one of this module's own local times (_nowiso), or infinity when it cannot be read."""
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso))).total_seconds()
    except Exception:
        return float("inf")


def _switch_exit() -> bool:
    try:
        import mason_autotrade as AT
        return (AT._at_cfg().get("exits") or {}).get("option_journal_close", True) is not False
    except Exception:
        return True


def _contract_words(occ) -> str:
    try:
        import mason_options as MO
        p = MO.parse_occ_symbol(occ)
        if p.get("ok"):
            return f"{p['ticker']} {p['strike']:g} {p['option_type']} expiring {p['expiration']}"
    except Exception:
        pass
    return str(occ)


def _n_contracts(q) -> str:
    q = float(q)
    if abs(q - 2) < 1e-9:
        return "both contracts"
    if abs(q - 1) < 1e-9:
        return "the contract"
    return f"{q:g} contracts"


def _usd_pl(v) -> str:
    v = float(v)
    if abs(v) < 0.005:
        return "No profit or loss"
    return f"{'Profit' if v > 0 else 'Loss'} ${abs(v):,.2f}"


# ── the "journal close due" markers (desk_state) ─────────────────────────────────────────────────────────────────

def _due_all() -> dict:
    conn = _conn()
    try:
        rows = conn.execute("SELECT key, value FROM desk_state WHERE key LIKE ?", (DUE_PREFIX + "%",)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            out[r["key"][len(DUE_PREFIX):]] = json.loads(r["value"] or "{}")
        except Exception:
            out[r["key"][len(DUE_PREFIX):]] = {}
    return out


def _due_get(tid):
    conn = _conn()
    try:
        r = conn.execute("SELECT value FROM desk_state WHERE key=?", (DUE_PREFIX + str(tid),)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    try:
        return json.loads(r["value"] or "{}")
    except Exception:
        return {}


def _due_set(tid, d):
    conn = _conn()
    try:
        with conn:
            conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                         "SET value=excluded.value, updated_date=excluded.updated_date",
                         (DUE_PREFIX + str(tid), json.dumps(d, default=str), datetime.now().isoformat()))
    finally:
        conn.close()


def _due_del(tid):
    try:
        conn = _conn()
        try:
            with conn:
                conn.execute("DELETE FROM desk_state WHERE key=?", (DUE_PREFIX + str(tid),))
        finally:
            conn.close()
    except Exception as e:
        log.warning("[option journal] could not clear the marker of trade %s: %s", tid, e)


def _due_note(tid, state, why, refused=False):
    """Update an EXISTING marker with the outcome of an attempt (never creates one). Returns True when this is a
    refusal whose reason was never written to the refusal log for this marker (each reason is written once, however
    the attempts alternate). (review) Once a marker needs a person it keeps saying so ("refused" stays true) until
    the row is closed or the marker removed: a later failed read must not turn it back into "Mason tries again"."""
    try:
        d = _due_get(tid)
        if d is None:
            return False
        logged = [x for x in (d.get("logged") or []) if isinstance(x, str)]
        new = bool(refused) and why not in logged
        if new:
            logged.append(why)
        d.update(tries=int(d.get("tries") or 0) + 1, state=state, last=why, last_at=_nowiso(),
                 refused=bool(refused) or bool(d.get("refused")), logged=logged[-10:])
        _due_set(tid, d)
        return new
    except Exception:
        return False


# ── which journal row belongs to a Mason trade ───────────────────────────────────────────────────────────────────

def _row_by(sql, args):
    import mason_db as DB
    conn = _conn()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [DB.row_to_dict(r, "options_trades") for r in rows]


def owns(trade, row):
    """(True, "") when journal row `row` is the one written for Mason trade `trade`; else (False, plain reason)."""
    rec = str(trade.get("rec_id") or "")
    occ = str(trade.get("occ") or "").strip().upper()
    if not row:
        return False, "no journal row"
    if str(row.get("created_by") or "") != "mason":
        return False, "the journal row was not written by Mason"
    if str(row.get("contract_symbol") or "").strip().upper() != occ or not occ:
        return False, "the journal row is on another contract"
    rid = row.get("director_rec_id")
    if rid and str(rid) != rec:
        return False, "the journal row belongs to another director call"
    coid = str(row.get("client_order_id") or "")
    if not rid and not coid.startswith(f"mason-dir-{rec}-"):
        return False, "the journal row does not name this director call"
    oid = _feat(trade).get("order_id")
    if oid and row.get("order_id") and str(row.get("order_id")) != str(oid):
        return False, "the journal row was written for another entry order"
    return True, ""


def find_row(trade):
    """(row | None, why) — the options journal row of THIS Mason trade (any status), never by ticker or contract
    alone: the trade's own journal link first, else (no link, or the link names a row that is not this trade's) its
    entry order id. Every candidate must pass owns()."""
    if str(trade.get("source") or "") != "mason" or not trade.get("occ"):
        return None, "not one of Mason's option trades"
    why = "this trade has no options journal row"
    jid, oid = trade.get("journal_id"), _feat(trade).get("order_id")
    looks = []
    if jid and str(trade.get("journal_table") or "options_trades") == "options_trades":
        looks.append(("SELECT * FROM options_trades WHERE id=? AND COALESCE(is_deleted,0)=0", (str(jid),)))
    if oid:
        looks.append(("SELECT * FROM options_trades WHERE (order_id=? OR client_order_id=?) "
                      "AND COALESCE(is_deleted,0)=0", (str(oid), str(oid))))
    for sql, args in looks:
        cands = _row_by(sql, args)
        good = [r for r in cands if owns(trade, r)[0]]
        if len(good) == 1:
            return good[0], ""
        if len(good) > 1:
            return None, "more than one journal row claims this trade"
        if cands:
            why = owns(trade, cands[0])[1]
    return None, why


# ── Alpaca reads (injectable) ────────────────────────────────────────────────────────────────────────────────────

def _valid_orders(d) -> bool:
    return isinstance(d, list) and all(isinstance(o, dict) and o.get("id") and o.get("symbol") and o.get("side")
                                       for o in d)


def _valid_positions(d) -> bool:
    return isinstance(d, list) and all(isinstance(p, dict) and p.get("symbol") and _f(p.get("qty")) is not None
                                       for p in d)


def execution_fetch(kind, sym=None, retry=True, timeout=8.0):
    """Read-only Alpaca reads through mason_execution's one choke point. (data, None) or (None, reason).
    retry=False (the exit path, review): no second try and no wait after a rate limit."""
    import mason_execution as EX
    if kind == "orders":
        path, params = "/orders", {"status": "all", "symbols": sym, "limit": 500, "nested": "true"}
    elif kind == "positions":
        path, params = "/positions", None
    elif kind in ("OPEXP", "OPEXC"):
        path, params = f"/account/activities/{kind}", {"page_size": 100}
    else:
        return None, f"unknown read {kind}"
    code, data = None, None
    for attempt in range(2 if retry else 1):
        code, data = EX._request("GET", path, params=params, timeout=timeout)
        if code != 429 or not retry:
            break
        time.sleep(1.0)
    if code != 200:
        return None, f"HTTP {code}"
    if kind == "orders" and not _valid_orders(data):
        return None, "the order list failed validation"
    if kind == "positions" and not _valid_positions(data):
        return None, "the position list failed validation"
    if not isinstance(data, list):
        return None, "unexpected answer"
    return data, None


def exit_path_fetch(budget_s=None):
    """(review 2026-11-18) The reads made on the exit path (the desk worker that also runs every other exit check):
    no wait after a rate limit, and all of them together within `budget_s` seconds (EXIT_READ_BUDGET_S). A read that
    would not fit is not made: the plan then waits and the 5-minute reconcile reads it (the marker stays)."""
    end = time.monotonic() + float(EXIT_READ_BUDGET_S if budget_s is None else budget_s)

    def fetch(kind, sym=None):
        left = end - time.monotonic()
        if left < 1.0:
            return None, "left for the next check"
        return execution_fetch(kind, sym, retry=False, timeout=min(8.0, left))
    return fetch


# ── the plan: what the row becomes (pure) ────────────────────────────────────────────────────────────────────────

def plan(trade, row, data, today=None, other_ids=frozenset()) -> dict:
    """What closing `row` for Mason trade `trade` means, from Alpaca's data. Pure.
    data: {"orders": [...] or None, "orders_err": str, "OPEXP": [...], "OPEXC": [...], "positions": [...],
           "from_record": bool (offline: the trade's own fills stand in for the order list)}.
    Returns {"state": "write"|"done"|"wait"|"manual"|"refuse"|"need", "why": plain words, "updates": {...},
             "need": [...]}."""
    import mason_options as MO
    today = today or _today()
    rec = str(trade.get("rec_id") or "")
    occ = str(trade.get("occ") or "").strip().upper()
    ok, why = owns(trade, row)
    if not ok:
        return {"state": "refuse", "why": why}
    if row.get("status") != "open":
        mx = row.get("mason_exit") if isinstance(row.get("mason_exit"), dict) else {}
        mine = mx.get("agent_trade_id") == trade.get("id")
        return {"state": "done", "why": "already closed by Mason" if mine else
                f"the journal row is already '{row.get('status')}' (closed some other way); nothing to change"}
    if str(trade.get("status") or "") != "closed":
        return {"state": "wait", "why": "Mason's trade is still open"}
    if str(trade.get("exit_reason") or "") == "expired_with_value":
        return {"state": "manual", "why": "it expired with value (it may have been exercised into shares), so its "
                                          "result needs the normal expiry review"}
    chk = MO.check_contract_consistency(row, occ)
    if not chk.get("ok"):
        return {"state": "refuse", "why": "the journal row's own fields disagree with its contract"}
    contracts = _f(row.get("contracts")) or _f(row.get("filled_qty"))
    tq = _f(trade.get("qty"))
    if not contracts or contracts <= 0:
        return {"state": "refuse", "why": "the journal row has no contract count"}
    if tq is None or abs(tq - contracts) > 1e-9:
        return {"state": "refuse", "why": f"the journal row has {contracts:g} contracts but Mason's trade has "
                                          f"{(tq or 0):g}"}
    premium = _f(row.get("premium"))
    if premium is None:
        premium = _f(row.get("filled_avg_price"))
    if premium is None or premium <= 0:
        return {"state": "refuse", "why": "the journal row has no cost (premium)"}

    parts = list(_feat(trade).get("partial_exits") or [])
    pieces = []
    if data.get("from_record"):                       # offline (repair tool): the trade's own record of its fills
        for x in parts:
            q, px = _f(x.get("qty")), _f(x.get("px"))
            if q and px:
                at, u = x.get("at"), _utc(x.get("at"))     # (review) Mason's local time, written in UTC like the
                if u is not None and len(str(at or "").strip()) > 10:   # journal's other fill times ("...Z")
                    at = u.strftime("%Y-%m-%dT%H:%M:%SZ")
                pieces.append({"kind": "mason_sale", "order_id": x.get("order_id"), "client_order_id": None,
                               "qty": q, "price": px, "at": at, "source": "Mason's trade record"})
    else:
        orders = data.get("orders")
        if orders is None:
            return {"state": "wait", "why": f"Alpaca's orders could not be read ({data.get('orders_err') or 'no answer'})"}
        pre = f"mason-exit-{rec}-"
        mine = [o for o in orders if str(o.get("symbol") or "").upper() == occ and str(o.get("side") or "").lower()
                == "sell" and str(o.get("client_order_id") or "").startswith(pre)]
        if any(str(o.get("status") or "").lower() not in _TERMINAL for o in mine):
            return {"state": "wait", "why": "a sale of this trade is still working at Alpaca"}
        ids = {o.get("id") for o in mine}
        missing = [x.get("order_id") for x in parts if x.get("order_id") and x.get("order_id") not in ids]
        if missing:
            return {"state": "wait", "why": f"Alpaca's order list does not show Mason's sale {str(missing[0])[:8]}"}
        booked = {}
        for x in parts:
            booked[x.get("order_id")] = booked.get(x.get("order_id"), 0.0) + (_f(x.get("qty")) or 0.0)
        for o in sorted(mine, key=lambda o_: _utc(o_.get("filled_at")) or _T_MIN):
            fq, px = _f(o.get("filled_qty")) or 0.0, _f(o.get("filled_avg_price"))
            if fq <= 1e-9:
                continue
            if px is None or px <= 0 or not o.get("filled_at"):
                return {"state": "wait", "why": f"Alpaca has no fill price or time for sale {str(o['id'])[:8]} yet"}
            q = min(fq, booked[o["id"]]) if o["id"] in booked else fq     # never more than Mason booked as its own
            pieces.append({"kind": "mason_sale", "order_id": o["id"], "client_order_id": o.get("client_order_id"),
                           "qty": q, "price": px, "at": o.get("filled_at"), "source": "Alpaca order"})
    # never more than the row's contracts, oldest first
    left, kept = contracts, []
    for p in pieces:
        if left <= 1e-9:
            break
        q = min(p["qty"], left)
        kept.append(dict(p, qty=q))
        left -= q
    pieces = kept
    need = left

    if need > 1e-9 and not data.get("from_record"):
        exp = MO.parse_occ_symbol(occ).get("expiration")
        try:
            expd = date.fromisoformat(exp) if exp else None
        except ValueError:
            expd = None
        t0 = _utc(trade.get("entry_ts"))
        pre = f"mason-exit-{rec}-"
        outside = []
        for o in data.get("orders") or []:
            coid = str(o.get("client_order_id") or "")
            if str(o.get("symbol") or "").upper() != occ or str(o.get("side") or "").lower() != "sell" \
                    or coid.startswith("mason-") or o.get("id") in other_ids:
                continue
            fq, px, ft = _f(o.get("filled_qty")) or 0.0, _f(o.get("filled_avg_price")), _utc(o.get("filled_at"))
            if fq <= 1e-9 or not px or ft is None or (t0 is not None and ft < t0):
                continue
            outside.append({"kind": "outside_sale", "order_id": o["id"], "client_order_id": coid or None, "qty": fq,
                            "price": px, "at": o.get("filled_at"), "source": "Alpaca order"})
        if expd is not None and expd < today:
            if "OPEXP" not in data or "OPEXC" not in data:
                return {"state": "need", "need": ["OPEXP", "OPEXC"]}
            if data["OPEXP"] is None or data["OPEXC"] is None:
                return {"state": "wait", "why": "Alpaca's expiry records could not be read"}
            exc = [a for a in data["OPEXC"] if str(a.get("activity_type") or "OPEXC").upper() == "OPEXC"]
            exp_ = [a for a in data["OPEXP"] if str(a.get("activity_type") or "OPEXP").upper() == "OPEXP"]
            if any(str(a.get("symbol") or "").upper() == occ for a in exc):
                return {"state": "manual", "why": "Alpaca shows it was exercised, so its result needs the normal "
                                                  "expiry review"}
            act = next((a for a in exp_ if str(a.get("symbol") or "").upper() == occ), None)
            if act is None:
                # (review) no expiry record: sales outside Mason that cover it mean it was SOLD, not expired, and
                # that is never booked after the fact; and a record still missing days later is not coming.
                if outside and abs(sum(p["qty"] for p in outside) - need) < 1e-9:
                    return {"state": "manual", "why": "Mason cannot tell from Alpaca's records which sale was Mason's"}
                late = (today - expd).days
                if late > EXPIRY_RECORD_WAIT_DAYS:
                    return {"state": "manual", "why": f"Alpaca still has no expiry record for it {late} days after "
                                                      f"it expired"}
                return {"state": "wait", "why": "Alpaca has no expiry record for it yet"}
            if (_f(act.get("net_amount")) or 0.0) != 0.0:
                return {"state": "manual", "why": "Alpaca shows it expired with value, so its result needs the "
                                                  "normal expiry review"}
            if outside:
                return {"state": "manual", "why": "part of it was sold outside Mason before it expired, so Mason "
                                                  "cannot tell which contracts expired"}
            pieces.append({"kind": "expired", "order_id": None, "client_order_id": None, "qty": need, "price": 0.0,
                           "at": act.get("transaction_time") or act.get("date") or exp,
                           "source": "Alpaca expiry record", "activity_id": act.get("id")})
            need = 0.0
        else:
            if "positions" not in data:
                return {"state": "need", "need": ["positions"]}
            if data["positions"] is None:
                return {"state": "wait", "why": "Alpaca's positions could not be read"}
            held = sum(abs(_f(p.get("qty")) or 0.0) for p in data["positions"]
                       if str(p.get("symbol") or "").upper() == occ)
            tot = sum(p["qty"] for p in outside)
            if held > 1e-9 or not outside or abs(tot - need) > 1e-9:
                return {"state": "manual", "why": "Mason cannot tell from Alpaca's records which sale was Mason's"}
            pieces.extend(outside)
            need = 0.0
    if need > 1e-9:
        return {"state": "wait" if not data.get("from_record") else "manual",
                "why": f"Alpaca's records cover only {contracts - need:g} of {contracts:g} contracts"}
    for p in pieces:
        if p.get("order_id") and p["order_id"] in other_ids:
            return {"state": "refuse", "why": f"order {str(p['order_id'])[:8]} is already recorded on another row"}

    proceeds = sum(p["qty"] * p["price"] for p in pieces)
    avg = round(proceeds / contracts, 6)
    pnl = round(proceeds * 100.0 - premium * contracts * 100.0, 2)
    expired = [p for p in pieces if p["kind"] == "expired"]
    sold = [p for p in pieces if p["kind"] != "expired"]
    last = max(pieces, key=lambda p: _utc(p.get("at")) or _T_MIN)
    last_sale = max(sold, key=lambda p: _utc(p.get("at")) or _T_MIN) if sold else None
    status = "expired" if expired else "closed_early"
    reason = str(trade.get("exit_reason") or "")
    feat = _feat(trade)
    words = (_UNDERLYING_WORDS.get(reason) if feat.get("exit_basis") == "underlying" else None) \
        or _REASON_WORDS.get(reason) or "of Mason's exit rules"
    what = "put" if occ[-9:-8] == "P" else "call"
    day = lambda p: _et_day(p.get("at"))                            # noqa: E731  (review: the Eastern day)
    bits = []
    if sold:
        sq = sum(p["qty"] for p in sold)
        sp = sum(p["qty"] * p["price"] for p in sold) / sq
        n_orders = len({p["order_id"] for p in sold})
        many = f" in {n_orders} orders" if n_orders > 1 else ""
        qty_w = _n_contracts(sq) if abs(sq - contracts) < 1e-9 else f"{sq:g} of the {contracts:g} contracts"
        if all(p["kind"] == "mason_sale" for p in sold):
            bits.append(f"Mason sold {qty_w} at {'an average of ' if many else ''}${sp:.2f}{many} on "
                        f"{day(last_sale)} because {words}.")
        else:
            bits.append(f"{qty_w[0].upper() + qty_w[1:]} sold outside Mason at {'an average of ' if many else ''}"
                        f"${sp:.2f}{many} on {day(last_sale)}.")
    if expired:
        eq = sum(p["qty"] for p in expired)
        if sold:
            bits.append(f"The other {eq:g} expired worthless on {day(expired[-1])}.")
        else:
            bits.append(f"The {what} expired worthless on {day(expired[-1])}; Mason held {_n_contracts(eq)} "
                        f"to the end.")
    bits.append(f"{_usd_pl(pnl)} (cost ${premium:.2f} a contract).")
    note = " ".join(bits)
    updates = {
        "status": status,
        "outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven"),
        "actual_pnl": pnl,
        "expiry_review": ("Closed early. " if status == "closed_early" else "Expired. ") + note,
        "expiry_review_date": today.isoformat(),
        "exit_order_id": last_sale["order_id"] if last_sale else None,
        "exit_client_order_id": last_sale.get("client_order_id") if last_sale else None,
        "exit_filled_qty": contracts,
        "exit_filled_avg_price": avg,
        "exit_filled_at": last.get("at"),
        "mason_exit": {"agent_trade_id": trade.get("id"), "rec_id": rec, "exit_reason": reason or None,
                       "pieces": pieces, "agent_pnl_usd": _f(trade.get("pnl_usd")),
                       "closed_by": "mason_option_journal", "closed_at": _nowiso()},
    }
    return {"state": "write", "why": note, "updates": updates}


def _other_ids(row_id, trade_id):
    """Order ids already recorded as an entry or an exit on ANOTHER options journal row, or as a fill of ANOTHER
    Mason trade (so one sale is never booked twice)."""
    out = set()
    conn = _conn()
    try:
        for r in conn.execute("SELECT order_id, exit_order_id FROM options_trades WHERE id<>? AND "
                              "COALESCE(is_deleted,0)=0", (str(row_id),)).fetchall():
            out |= {x for x in (r["order_id"], r["exit_order_id"]) if x}
        for r in conn.execute("SELECT features_json, exit_order_id FROM agent_trades WHERE id<>? AND "
                              "COALESCE(record_deleted,0)=0 AND occ IS NOT NULL", (str(trade_id),)).fetchall():
            if r["exit_order_id"]:
                out.add(r["exit_order_id"])
            try:
                out |= {x.get("order_id") for x in (json.loads(r["features_json"] or "{}").get("partial_exits") or [])
                        if x.get("order_id")}
            except Exception:
                pass
    finally:
        conn.close()
    return out


def _write_close(row_id, updates):
    """The one write: ONLY the row named, ONLY while it is still open (compare-and-set), the closure columns and the
    'mason_exit' record in its extra field. Then the options accuracy ledger is recomputed as after every journal
    write. Returns the closed row, or None when the row was no longer open."""
    import mason_options as MO
    conn = _conn()
    try:
        cur = conn.execute("SELECT status, extra_json FROM options_trades WHERE id=? AND COALESCE(is_deleted,0)=0",
                           (str(row_id),)).fetchone()
        if cur is None or cur["status"] != "open":
            return None
        try:
            extra = json.loads(cur["extra_json"] or "{}")
            extra = extra if isinstance(extra, dict) else {}
        except Exception:
            extra = {}
        extra["mason_exit"] = updates["mason_exit"]
        cols = {k: v for k, v in updates.items() if k != "mason_exit"}
        cols["extra_json"] = json.dumps(extra, default=str)
        with conn:
            n = conn.execute(f"UPDATE options_trades SET {', '.join(f'{k}=?' for k in cols)} "
                             "WHERE id=? AND status='open' AND COALESCE(is_deleted,0)=0",
                             list(cols.values()) + [str(row_id)]).rowcount
    finally:
        conn.close()
    if n != 1:
        return None
    try:
        if RECOMPUTE_ACCURACY:
            MO._recompute_accuracy(MO._load_journal())
    except Exception as e:
        log.warning("[option journal] accuracy ledger not recomputed: %s", e)
    return MO.get_play_by_id(row_id)


# ── closing one trade's row ──────────────────────────────────────────────────────────────────────────────────────

def close_for_trade(trade, fetch=None, lock_timeout=5.0, today=None, dry_run=False, from_record=False) -> dict:
    """Close the options journal row of Mason's (closed) option trade `trade` from Alpaca's records. Never raises.
    Returns {"state": "closed"|"done"|"wait"|"manual"|"refuse"|"no_row"|"off"|"busy"|"error"|"would_write",
             "why", "row", "trade_id", "updates"}. With dry_run nothing is written (the repair tool's preview)."""
    tid = trade.get("id") if isinstance(trade, dict) else None
    out = {"trade_id": tid, "rec_id": (trade or {}).get("rec_id"), "row": None, "state": "error", "why": ""}
    try:
        if not dry_run and not _switch_exit():
            return dict(out, state="off", why="switched off (autotrade.exits.option_journal_close)")
        row, why = find_row(trade)
        if row is None:
            if not dry_run:
                _due_del(tid)
            return dict(out, state="no_row", why=why)
        out["row"] = row["id"]
        import contextlib
        try:
            import mason_execution as EX
            lock = EX.books_lock(lock_timeout)
        except Exception:
            lock = contextlib.nullcontext(True)
        with (contextlib.nullcontext(True) if dry_run else lock) as held:
            if not held:
                if not dry_run:
                    _due_note(tid, "busy", "another booking of fills was running")
                return dict(out, state="busy", why="another booking of fills was running; tried again later")
            fresh = _row_by("SELECT * FROM options_trades WHERE id=? AND COALESCE(is_deleted,0)=0", (row["id"],))
            if not fresh:
                if not dry_run:
                    _due_del(tid)
                return dict(out, state="no_row", why="the journal row is gone")
            row = fresh[0]
            fetch = fetch or execution_fetch
            data = {"from_record": bool(from_record)}
            if not from_record and row.get("status") == "open" and str(trade.get("status") or "") == "closed":
                data["orders"], data["orders_err"] = fetch("orders", str(trade.get("occ") or "").upper())
            p = plan(trade, row, data, today, _other_ids(row["id"], tid))
            for _ in range(3):
                if p.get("state") != "need":
                    break
                for k in p.get("need") or []:
                    data[k], _e = fetch(k, str(trade.get("occ") or "").upper())
                p = plan(trade, row, data, today, _other_ids(row["id"], tid))
            st = p.get("state")
            if st == "need":
                st, p = "wait", dict(p, why="Alpaca's records could not be gathered")
            out.update(why=p.get("why") or "", updates=p.get("updates"))
            if dry_run:
                return dict(out, state="would_write" if st == "write" else st)
            if st == "write":
                done = _write_close(row["id"], p["updates"])
                if done is None:
                    _due_del(tid)
                    return dict(out, state="done", why="the journal row was closed in the meantime; nothing written")
                _due_del(tid)
                return dict(out, state="closed")
            if st == "done":
                _due_del(tid)
                return dict(out, state="done")
            new = _due_note(tid, st, p.get("why") or "", refused=st in ("refuse", "manual"))
            if st in ("refuse", "manual") and new:
                try:
                    import mason_options as MO
                    MO._append_refusal_log({"refused_at": datetime.now().isoformat(),
                                            "action": "close_mason_option_exit", "play_id": row["id"],
                                            "contract_symbol": trade.get("occ"), "agent_trade_id": tid,
                                            "rec_id": trade.get("rec_id"), "refused_because": st,
                                            "error": p.get("why")})
                except Exception:
                    pass
            return dict(out, state=st)
    except Exception as e:
        log.warning("[option journal] closing the journal row of trade %s failed: %s", tid, e)
        try:
            _due_note(tid, "error", f"{type(e).__name__}: {e}"[:200])
        except Exception:
            pass
        return dict(out, state="error", why=f"{type(e).__name__}: {e}"[:200])


# ── the exit path's two calls (mason_agent_loop._finalize) ───────────────────────────────────────────────────────

def before_close(trade):
    """Just before the exit manager closes Mason option trade `trade`: when it has an OPEN journal row of its own,
    leave a marker so the row is closed even if Mason stops right after the trade is closed. Returns the row id
    (or None: nothing to do). Never raises."""
    try:
        if not trade or not trade.get("occ") or str(trade.get("source") or "mason") != "mason" or not _switch_exit():
            return None
        row, _why = find_row(dict(trade, source=trade.get("source") or "mason"))
        if row is None or row.get("status") != "open":
            return None
        try:
            _due_set(trade["id"], {"rec_id": trade.get("rec_id"), "row": row["id"], "since": _nowiso(), "tries": 0})
        except Exception as e:                       # (review) no marker (database busy): the row is still closed
            log.warning("[option journal] marker for trade %s not written: %s", trade.get("id"), e)   # below, now
        return row["id"]
    except Exception as e:
        log.warning("[option journal] marker for trade %s not written: %s", (trade or {}).get("id"), e)
        return None


def after_close(trade, closed_fields, token) -> dict | None:
    """The exit manager's last step after closing the trade: close its journal row. Never raises; a failure leaves
    the marker for retry_due()."""
    if token is None:
        return None
    try:
        t = dict(trade)
        t.update(closed_fields or {})
        t["source"] = t.get("source") or "mason"
        return close_for_trade(t, fetch=exit_path_fetch())     # (review) bounded reads on the desk worker
    except Exception as e:
        log.warning("[option journal] after_close failed for trade %s: %s", (trade or {}).get("id"), e)
        return None


# ── retries and the self-check rule ──────────────────────────────────────────────────────────────────────────────

def _trade(tid):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM agent_trades WHERE id=? AND COALESCE(record_deleted,0)=0", (str(tid),)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def retry_due(fetch=None, lock_timeout=5.0, budget_s=None) -> list:
    """Try again every marked trade that is closed. Returns the results of the closes that wrote (empty when there
    is nothing to do: one small database read, no Alpaca call). Never raises.
    (review 2026-11-18) Called inside the books lock (the self-check on the desk worker waits for it): the pass's
    Alpaca reads share `budget_s` (RETRY_BUDGET_S) seconds, never waiting out a rate limit, and no new close is
    started after it (the rest at the next pass); a marker that needs a person is tried again at most every
    RETRY_REFUSED_S seconds, so a stuck row does not cost Alpaca reads every 5 minutes for ever."""
    done = []
    try:
        if not _switch_exit():
            return done
        t0 = time.monotonic()
        budget = float(RETRY_BUDGET_S if budget_s is None else budget_s)
        for tid, d in _due_all().items():
            left = budget - (time.monotonic() - t0)
            if left <= 0:
                break
            if d.get("refused") and _age_s(d.get("last_at")) < RETRY_REFUSED_S:
                continue
            t = _trade(tid)
            if t is None:
                _due_del(tid)
                continue
            if str(t.get("status") or "") != "closed":
                continue                                   # the exit manager is still on it
            r = close_for_trade(t, fetch=fetch or exit_path_fetch(max(2.0, left)), lock_timeout=lock_timeout)
            if r.get("state") == "closed":
                done.append(r)
    except Exception as e:
        log.warning("[option journal] retry failed: %s", e)
    return done


def _mason_trade_for_row(row):
    rec = row.get("director_rec_id")
    occ = str(row.get("contract_symbol") or "").strip().upper()
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM agent_trades WHERE source='mason' AND COALESCE(record_deleted,0)=0 AND "
                            "UPPER(occ)=? AND (journal_id=? OR rec_id=?)",
                            (occ, str(row.get("id")), str(rec or ""))).fetchall()
    finally:
        conn.close()
    good = [dict(r) for r in rows if owns(dict(r), row)[0]]
    return good[0] if len(good) == 1 else None


def _qty_words(q) -> str:
    q = float(q or 0)
    return "1 contract" if abs(q - 1) < 1e-9 else f"{q:g} contracts"


_REASON_SHORT = {"stop": "its stop", "target": "its target", "time_stop": "its time stop",
                 "pre_expiry": "the day before expiry", "director_close": "the director closed it",
                 "flat_by": "the same-day cut-off", "closed_outside": "sold outside Mason", "expired": "expired",
                 "expired_with_value": "expired with value", "position_gone": "the position was gone"}


def selfcheck_rows(positions) -> list:
    """The self-check rule, REPORT ONLY (review 2026-11-18: the self-check runs on the desk worker next to every exit
    check, so it makes no Alpaca read and writes nothing; the 5-minute reconcile does the retries, from Alpaca's own
    records). `positions` is a good, validated Alpaca position list. Returns issues in mason_selfcheck's shape.
      * a Mason trade that ended and whose row is still open: reported (with the marker's last reason when it is
        marked; with the one-off fix named when it ended before this code, i.e. has no marker), never changed here;
      * any other open row (not Mason's) whose contract Alpaca does not hold: reported only, never changed."""
    issues = []
    import mason_options as MO
    rows = [r for r in MO.get_open_plays() if r.get("execution_verified") and r.get("contract_symbol")]
    if rows and not positions:
        issues.append({"key": "optjournal_empty_book", "severity": "warn",
                       "text": f"Alpaca reported zero positions while the options journal has {len(rows)} open "
                               f"row(s). Treated as bad data; nothing was changed."})
        return issues
    if not rows:
        return issues
    held = {}
    for p in positions or []:
        s_ = str(p.get("symbol") or "").upper()
        held[s_] = held.get(s_, 0.0) + abs(_f(p.get("qty")) or 0.0)
    due = _due_all()
    for r in rows:
        occ = str(r["contract_symbol"]).strip().upper()
        tk = r.get("ticker") or MO.parse_occ_symbol(occ).get("ticker")
        what = f"{_contract_words(occ)} (options journal row {r['id']})"
        t = _mason_trade_for_row(r) if r.get("created_by") == "mason" else None
        if t is not None:
            if str(t.get("status") or "") != "closed":
                continue                                   # Mason still manages it (the exit manager books its end)
            px, pnl = _f(t.get("exit_px")), _f(t.get("pnl_usd"))
            reason = str(t.get("exit_reason") or "")
            sold = (f"Mason's trade ended on {str(t.get('exit_ts') or '')[:10]} ("
                    + ", ".join([_REASON_SHORT.get(reason, reason or "no reason recorded")]
                                + ([f"{_qty_words(t.get('qty'))} at ${px:.2f}"] if px else [])
                                + ([_usd_pl(pnl).lower()] if pnl is not None else [])) + ")")
            d = due.get(t["id"])
            if d is None:
                issues.append({"key": f"optjournal:{r['id']}", "severity": "warn", "ticker": tk,
                               "text": f"{what}: {sold}, but the options journal still shows it open. Nothing was "
                                       f"changed; it needs the one-off fix (tools/repair_option_exits.py)."})
                continue
            since = _utc(d.get("since"))
            old = since is None or (datetime.now(timezone.utc) - since).total_seconds() > 1800
            stuck = bool(d.get("refused"))
            why = d.get("last") or "not closed yet"
            issues.append({"key": f"optjournal:{r['id']}", "severity": "warn" if (stuck or old) else "info",
                           "ticker": tk,
                           "text": f"{what}: {sold}, but its options journal row is still open: {why}. "
                                   + ("Please look at it; nothing was changed." if stuck else
                                      "Mason tries again every 5 minutes while the market is open.")})
            continue
        if held.get(occ, 0.0) > 1e-9:
            continue
        exp = MO.parse_occ_symbol(occ).get("expiration")
        past = bool(exp and exp < _today().isoformat())
        issues.append({"key": f"optjournal:{r['id']}", "severity": "warn", "ticker": tk,
                       "text": f"{what} is open in the options journal, but Alpaca holds no such contract. It is not "
                               f"linked to one of Mason's own trades, so nothing was changed."
                               + (f" It expired on {exp}; review it in chat." if past else "")})
    return issues
