"""
mason_nico_thesis.py — "Nico's thesis": the owner's hand-placed Alpaca (web UI) trades as a
LABELED learning source in `agent_trades` (source='nico'), kept separate from Mason's own record.

READ-ONLY toward Alpaca: this module only ever GETs /orders and /positions. It never places,
modifies or cancels anything, and never touches rows with source='mason'.

  sync()         import UI buys (one row per buy order), close rows FIFO on UI sells (and on Mason's
                 sell-list sales of Nico's stock), close
                 expired options from the expiry day's close (in the money: 'exercised' at that value; else $0
                 'expired'; no usable close: left open with the reason) — throttled to once per 15 min per process
  seed()         apply the owner's known theses to matching rows that have none
  set_thesis()   add/replace a row's thesis later (owner / UI)
  thesis_text()  compact table for the director's package
  stats()        {n_open, n_closed, wins, win_rate, pnl_usd}

Every public function catches exceptions and returns a dict/str; nothing raises.
"""

import json
import re
import time
import uuid
from datetime import datetime, date, timedelta

SYNC_EVERY_S = 900
_LAST_SYNC = 0.0                 # module-level throttle; tests reset it (or pass force=True)
_BACKFILL_MARK = "_backfill_120d_done"
_FILLED_TERMINAL = {"filled", "canceled", "cancelled", "expired", "done_for_day", "replaced"}
_OCC_RE = re.compile(r"^([A-Z0-9.]{1,6})(\d{6})([CP])(\d{8})$")
_SELLS_SQL = "CREATE TABLE IF NOT EXISTS agent_nico_sells(order_id TEXT PRIMARY KEY, processed_at TEXT)"

HEADER = ("NICO'S THESES (the owner's own hand-placed trades — a separate, labeled source; "
          "learn from them, do not count them as your record)")

SEEDS = [
    {"underlying": "HOOG", "vehicle": "call", "strike": 17.0, "expiry": "2026-11-20",
     "thesis": "Nico's thesis: long-term pro-HOOD; HOOG should rise (higher risk). "
               "Earlier round trip: bought 24, sold 32."},
    {"underlying": "WMT", "vehicle": "call", "strike": None, "expiry": "2026-12-30",
     "thesis": "Nico's thesis: four years of Walmart holiday-season strength; cheap contract."},
]


# ── lazy module access ───────────────────────────────────────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _AL():
    import mason_agent_loop
    return mason_agent_loop


def _f(v):
    return _AT()._f(v)


def _nowiso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _q(sql, args=()):
    return _AL()._q(sql, args)


def _exec(sql, args=()):
    conn = _AL()._conn()
    try:
        with conn:
            conn.execute(sql, args)
    finally:
        conn.close()


def _ensure():
    _AL().ensure_schema()
    _exec(_SELLS_SQL)


# ── pure helpers ─────────────────────────────────────────────────────────────────

def parse_occ(sym):
    """'HOOG261120C00017000' → {underlying, expiry(date), vehicle, strike} or None."""
    m = _OCC_RE.match(str(sym or "").upper())
    if not m:
        return None
    try:
        yy, mm, dd = int(m.group(2)[:2]), int(m.group(2)[2:4]), int(m.group(2)[4:])
        return {"underlying": m.group(1), "expiry": date(2000 + yy, mm, dd),
                "vehicle": "call" if m.group(3) == "C" else "put", "strike": int(m.group(4)) / 1000.0}
    except Exception:
        return None


def _feat(row) -> dict:
    try:
        return dict(json.loads(row.get("features_json") or "{}"))
    except Exception:
        return {}


def _remaining(row) -> float:
    return max(0.0, float(row.get("qty") or 0) -
               sum(float(x.get("qty") or 0) for x in (_feat(row).get("partial_exits") or [])))


def _seed_for(underlying, vehicle, expiry, strike):
    for s in SEEDS:
        if s["underlying"] != (underlying or "").upper() or s["vehicle"] != vehicle:
            continue
        if not expiry or str(expiry)[:10] != s["expiry"]:
            continue
        if s.get("strike") is not None and (strike is None or abs(float(strike) - s["strike"]) > 1e-6):
            continue
        return s["thesis"]
    return None


def _is_ui(o) -> bool:
    src = o.get("source")
    return (src in (None, "")) and not str(o.get("client_order_id") or "").startswith("mason-")


# Books follow-up 4 (2026-11-16): client-id prefix of the sells Mason places of NICO's shares that nothing else
# applies to these rows: the hand-approved sell list. Review fix (BOOKS2_REVIEW F1): mason_protect's protective
# stops ('mason-protect-stop-') and director "book" sales ('mason-book-') are NOT listed: they already reduce these
# rows when they fill (mason_protect._close_thesis_rows, mason_agent_loop._book_end -> _close_nico_rows), and
# listing them here applied the same sale a second time.
_MASON_SELLS_OF_NICO = ("mason-nico-sell-",)


def _is_mason_sale_of_nico_stock(o) -> bool:
    """A top-level STOCK sell Mason placed of Nico's shares (see _MASON_SELLS_OF_NICO). Options are never included:
    Nico's own contracts are his alone."""
    sym = str(o.get("symbol") or "").upper()
    return (str(o.get("side") or "").lower() == "sell"
            and str(o.get("client_order_id") or "").startswith(_MASON_SELLS_OF_NICO)
            and o.get("asset_class") in (None, "", "us_equity") and not parse_occ(sym))


def _fill(o):
    """(qty, px) of a usable fill, or None. A still-partially_filled order is deferred until final."""
    st = str(o.get("status") or "").lower()
    q, px = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
    if not q or q <= 0 or px is None:
        return None
    if st in ("filled",) or st in _FILLED_TERMINAL:
        return q, px
    return None                                      # partially_filled / new / … → not final yet


def _update(tid, fields):
    _AL()._update_trade(tid, fields)


# ── import / close ───────────────────────────────────────────────────────────────

def _insert_buy(o, q, px) -> bool:
    if _q("SELECT 1 FROM agent_trades WHERE source='nico' AND rec_id=?", (o["id"],)):
        return False
    sym = str(o.get("symbol") or "").upper()
    occ = parse_occ(sym) if (o.get("asset_class") == "us_option" or parse_occ(sym)) else None
    if occ:
        ticker, vehicle, osym = occ["underlying"], occ["vehicle"], sym
        direction = "short" if vehicle == "put" else "long"
        mult = 100
        thesis = _seed_for(ticker, vehicle, occ["expiry"].isoformat(), occ["strike"]) or ""
    else:
        ticker, vehicle, osym, direction, mult, thesis = sym, "stock", None, "long", 1, ""
    feats = {"order_id": o["id"], "source": "alpaca_ui"}
    conn = _AL()._conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO agent_trades(id,source,rec_id,ticker,occ,vehicle,direction,horizon,tier,"
                "thesis,features_json,entry_ts,entry_px,qty,cost_usd,status,created_date,created_by,record_deleted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (uuid.uuid4().hex[:12], "nico", o["id"], ticker, osym, vehicle, direction, None, None,
                 thesis, json.dumps(feats), o.get("filled_at") or _nowiso(), px, q, px * q * mult,
                 "open", _nowiso(), "mason_nico_thesis"))
            return cur.rowcount > 0
    finally:
        conn.close()


def _close_row(row, last_qty, last_px, reason, at, order_id=None, extra=None):
    """Close a nico row: exit px = qty-weighted over earlier partial exits + this last fill."""
    mult = 100 if row.get("occ") else 1
    parts = [(float(x["qty"]), float(x["px"])) for x in (_feat(row).get("partial_exits") or [])]
    if last_qty > 1e-9:
        parts.append((float(last_qty), float(last_px)))
    tq = sum(q for q, _ in parts) or float(row.get("qty") or 0)
    exit_px = (sum(q * p for q, p in parts) / tq) if parts and tq else float(last_px)
    entry = _f(row.get("entry_px"))
    f = {"exit_ts": at, "exit_px": round(exit_px, 6), "exit_reason": reason, "exit_order_id": order_id,
         "graded_at": _nowiso(), "status": "closed"}
    if entry:
        pnl = (exit_px - entry) * tq * mult
        f.update(pnl_usd=round(pnl, 6), pnl_pct=(exit_px - entry) / entry * 100.0, win=1 if pnl > 0 else 0)
    f.update(extra or {})
    _update(row["id"], f)
    return f


def _open_rows(sym, occ):
    if occ:
        return _q("SELECT * FROM agent_trades WHERE source='nico' AND status='open' AND occ=? "
                  "AND COALESCE(record_deleted,0)=0 ORDER BY entry_ts, created_date, id", (sym,))
    return _q("SELECT * FROM agent_trades WHERE source='nico' AND status='open' AND occ IS NULL AND ticker=? "
              "AND COALESCE(record_deleted,0)=0 ORDER BY entry_ts, created_date, id", (sym,))


def _apply_sell(o, q, px, entered_before_sale=False) -> dict:
    oid = o["id"]
    if _q("SELECT 1 FROM agent_nico_sells WHERE order_id=?", (oid,)):
        return {"order_id": oid, "skipped": "already processed"}
    sym = str(o.get("symbol") or "").upper()
    occ = bool(parse_occ(sym)) and (o.get("asset_class") in (None, "", "us_option"))
    at = o.get("filled_at") or _nowiso()
    # Review fix (BOOKS2_REVIEW F2), Mason's sales of Nico's stock only: a row entered AFTER the sale (a re-buy) is
    # never reduced by it. The first sync after install re-reads 5 days of these sales, and one whose row was
    # already closed as "no longer held" must not land on a newer row. UI sells keep the old walk.
    sold_at = _ts_utc(at) if entered_before_sale else None
    left, closed, partial = float(q), [], []
    for row in _open_rows(sym, occ):
        if left <= 1e-9:
            break
        if sold_at is not None:
            t0 = _ts_utc(row.get("entry_ts"))
            if t0 is None or t0 > sold_at:
                continue
        rem = _remaining(row)
        if rem <= 1e-9:
            continue
        take = min(rem, left)
        if take >= rem - 1e-9:
            _close_row(row, take, px, "nico_sold", at, oid)
            closed.append(row["id"])
        else:
            feat = _feat(row)
            feat["partial_exits"] = list(feat.get("partial_exits") or []) + [
                {"qty": take, "px": px, "order_id": oid, "at": at}]
            _update(row["id"], {"features_json": json.dumps(feat, default=str)})
            partial.append(row["id"])
        left -= take
    _exec("INSERT OR IGNORE INTO agent_nico_sells(order_id, processed_at) VALUES (?,?)", (oid, _nowiso()))
    return {"order_id": oid, "closed": closed, "partial": partial, "unmatched_qty": round(left, 6)}


_EXPIRY_RETRY_S = 3600              # a close that could not be read is looked up again at most once an hour
_EXPIRY_TRIED = {}                  # row id -> time.time() of the last unusable look-up (tests clear it)
_ITM_MIN = 0.01                     # the broker exercises a contract in the money by $0.01 or more at expiry


def _expiry_close(underlying, expiry, today, opened) -> dict:
    """The underlying's close ON the expiry day, read the way expiry reviews read it (mason_options: the raw print,
    split-safe, walks back over a holiday only): {"price", "status", "reason", "detail"}. Never raises."""
    try:
        import mason_options
        return mason_options._expiry_close_quote(underlying, expiry, today=today, opened=opened)
    except Exception as e:
        return {"price": None, "status": "unavailable", "reason": "processing_error", "detail": str(e)[:160]}


def _filled_sells(fetched) -> list:
    """[(symbol, filled_at UTC, px, order id)] of the filled sells (bracket legs too) in the orders a sync read."""
    out = []
    for o in fetched or []:
        for x in [o] + [l for l in (o.get("legs") or []) if isinstance(l, dict)]:
            fl = _fill(x)
            if fl and str(x.get("side") or "").lower() == "sell":
                out.append((str(x.get("symbol") or "").upper().replace("/", ""), _ts_utc(x.get("filled_at")),
                            fl[1], x.get("id")))
    return out


def _booked_sale_ids(sym, ids) -> set:
    """(review 2026-11-19) Which of these sale order ids are already booked on ANY of Nico's records of this symbol
    (a close's exit order, a part sale) or were already applied by _apply_sell (agent_nico_sells, which spreads a
    website sale over his records oldest first). Such a sale is not the sale of a record that stayed open: with two
    buys of one contract, the sale that closed the first must never close the second at its price. Never raises."""
    ids = {str(i) for i in ids if i}
    if not ids:
        return set()
    booked = set()
    try:
        for r in _q("SELECT exit_order_id, features_json FROM agent_trades WHERE source='nico' "
                    "AND UPPER(REPLACE(COALESCE(occ, ticker, ''), '/', ''))=?", (sym,)):
            if r.get("exit_order_id"):
                booked.add(str(r["exit_order_id"]))
            for x in (_feat(r).get("partial_exits") or []):
                if isinstance(x, dict) and x.get("order_id"):
                    booked.add(str(x["order_id"]))
    except Exception:
        pass
    try:
        marks = ",".join("?" * len(ids))
        booked |= {str(r["order_id"]) for r in _q(f"SELECT order_id FROM agent_nico_sells WHERE order_id IN ({marks})",
                                                  tuple(sorted(ids)))}
    except Exception:
        pass
    return booked & ids


def _unbooked_sales(row, sells) -> list:
    """The sells of this row's contract after its entry that no record has booked yet (a part sale booked on this
    row, a sale that closed or reduced another of his records of the same contract, or one _apply_sell already
    applied is not counted), oldest first."""
    t0 = _ts_utc(row.get("entry_ts"))
    sym = str(row.get("occ") or row.get("ticker") or "").upper().replace("/", "")
    cand = [s for s in sells if s[0] == sym and s[1] is not None and t0 is not None and s[1] >= t0]
    if not cand:
        return []
    done = _booked_sale_ids(sym, [s[3] for s in cand])
    return sorted([s for s in cand if str(s[3]) not in done], key=lambda s: s[1])


def _expire(today: date, positions, fetched=None, waiting=None) -> list:
    """Close Nico's option records whose contract is gone after its expiry date. Only Mason's own record about his
    trade is written: nothing is ever sent to the broker.
    (2026-11-19) The underlying's close ON the expiry day decides. In the money by a cent or more -> closed at that
    intrinsic value, 'exercised' (the contract turned into shares; before, it was booked at $0 like a worthless one).
    Otherwise -> $0 'expired', as before. When the close can't be read safely (no data yet, no bar for the session, a
    split during the contract's life, an adjusted contract) the record stays open, the reason is written on it
    (features 'expiry_wait') and added to `waiting`, and it is looked at again at most once an hour. A sale of the
    contract that is not booked on the record yet is left to the 'no longer held' close, which books the sale."""
    held = {str(p.get("symbol") or "").upper() for p in positions}
    sells = _filled_sells(fetched)
    out = []
    for row in _q("SELECT * FROM agent_trades WHERE source='nico' AND status='open' AND occ IS NOT NULL "
                  "AND COALESCE(record_deleted,0)=0"):
        occ = parse_occ(row["occ"])
        if not occ or occ["expiry"] >= today or row["occ"].upper() in held:
            continue
        if _unbooked_sales(row, sells) or time.time() - _EXPIRY_TRIED.get(row["id"], 0.0) < _EXPIRY_RETRY_S:
            continue
        exp, und, feat = occ["expiry"].isoformat(), occ["underlying"], _feat(row)
        if und[-1:].isdigit():          # OCC gives an adjusted contract a root like 'WMT1' (split, merger, ...)
            q = {"status": "unsafe", "reason": "adjusted_contract", "price": None,
                 "detail": f"{row['occ']} is an adjusted contract: what it pays isn't just the stock's close minus "
                           f"the strike"}
        else:
            q = _expiry_close(und, exp, today, str(row.get("entry_ts") or "")[:10] or None)
        close = q.get("price") if q.get("status") == "ok" else None
        if close is None:
            _EXPIRY_TRIED[row["id"]] = time.time()
            why = (f"{und}'s close on {exp} couldn't be used ({q.get('reason')}: {str(q.get('detail') or '')[:140]}),"
                   f" so this record stays open until it can; nothing was sent to the broker")
            item = {"id": row["id"], "occ": row["occ"], "reason": q.get("reason"), "why": why}
            if (feat.get("expiry_wait") or {}).get("why") != why:
                feat["expiry_wait"] = {"at": _nowiso(), "reason": q.get("reason"), "why": why}
                _update(row["id"], {"features_json": json.dumps(feat, default=str)})
                import logging
                logging.getLogger("mason.nico_thesis").warning("[nico_thesis] %s (%s): %s", row["occ"], row["id"], why)
            if waiting is not None:
                waiting.append(item)
            continue
        value = max(0.0, (close - occ["strike"]) if occ["vehicle"] == "call" else (occ["strike"] - close))
        value = round(value, 4) if value >= _ITM_MIN else 0.0
        feat.pop("expiry_wait", None)
        feat["expiry_close"] = {"date": exp, "close": round(close, 6), "value": value,
                                "detail": str(q.get("detail") or "")[:120], "source": "mason_options expiry close"}
        _close_row(row, _remaining(row), value, "exercised" if value > 0 else "expired", f"{exp}T16:00:00",
                   extra={"features_json": json.dumps(feat, default=str)})
        _EXPIRY_TRIED.pop(row["id"], None)
        out.append(row["id"])
    return out


NOT_HELD = "not_held_at_alpaca"
_NOT_HELD_GRACE_S = 15 * 60          # a row entered in the last 15 minutes is left alone (broker data catching up)


def _ts_utc(s):
    """An ISO time as an aware UTC datetime ('Z', an offset, or naive = this machine's local time), or None."""
    try:
        from datetime import timezone
        t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00")[:32])
        return (t if t.tzinfo else t.astimezone()).astimezone(timezone.utc)
    except Exception:
        return None


def _journal_exit(rec_id):
    """(px, exit order id, exit date) of a CLOSED journal row bought by the same Alpaca order, when no open journal
    row of that order remains; else None. Evidence from Mason's own books, no broker call."""
    if not rec_id:
        return None
    rows = _q("SELECT status, exit_price, exit_order_id, exit_date FROM trades WHERE entry_order_id=? "
              "AND COALESCE(is_deleted,0)=0", (rec_id,))
    if not rows or any(r["status"] == "open" for r in rows):
        return None
    done = [r for r in rows if r["exit_price"]]
    if not done:
        return None
    r = max(done, key=lambda x: str(x["exit_date"] or ""))
    return float(r["exit_price"]), r["exit_order_id"], r["exit_date"]


def _close_not_held(positions, fetched, today=None) -> list:
    """Books fix 2026-11-15 (item E). Close the open 'Nico's thesis' rows whose stock or contract Alpaca no longer
    holds at all. Before, only sells Nico placed in Alpaca's web UI closed these rows, so a position that left by any
    other order (Mason's liquidation of the CVS share on 07-24, the 'mason-nico-sell' list, a chat 'close it')
    stayed open in the learning loop forever.

    Exit price, in order: the newest filled sell of that symbol after the row's entry in the orders this sync
    fetched (any source); else the journal's own closed row for the same buy order; else unknown (closed with no
    P&L and no win/loss). exit_reason 'not_held_at_alpaca'. Callers pass the positions only when Alpaca answered;
    an EMPTY position list is not trusted (bad data looks like that) and nothing is closed.
    (2026-11-19) A contract gone on or after its expiry date is closed here only by a sale not yet booked on it;
    otherwise the expiry rule (_expire) books it from the expiry day's close, from the day after expiry."""
    if not positions:
        return []
    # review fix: '/' is dropped on both sides (crypto: orders say 'BTC/USD', /positions says 'BTCUSD')
    held = {str(p.get("symbol") or "").upper().replace("/", "") for p in positions}
    from datetime import timezone
    now = datetime.now(timezone.utc)
    if today is None:
        try:
            today = _AT()._now_et().date()
        except Exception:
            today = date.today()
    sells = _filled_sells(fetched)
    out = []
    for row in _q("SELECT * FROM agent_trades WHERE source='nico' AND status='open' "
                  "AND COALESCE(record_deleted,0)=0"):
        sym = str(row.get("occ") or row.get("ticker") or "").upper().replace("/", "")
        if not sym or sym in held:
            continue
        t0 = _ts_utc(row.get("entry_ts"))
        if t0 is None or (now - t0).total_seconds() < _NOT_HELD_GRACE_S:
            continue
        occ = parse_occ(row.get("occ")) if row.get("occ") else None
        mine = sorted([s for s in sells if s[0] == sym and s[1] is not None and s[1] >= t0], key=lambda s: s[1])
        if occ and occ["expiry"] <= today:
            mine = _unbooked_sales(row, sells)       # (review) only a sale no record has booked closes it here
            if not mine:
                continue
        if mine:
            px, oid, at = mine[-1][2], mine[-1][3], mine[-1][1].isoformat()
        else:
            ev = None if row.get("occ") else _journal_exit(row.get("rec_id"))
            px, oid, at = (ev[0], ev[1], f"{ev[2]}T16:00:00" if ev[2] else _nowiso()) if ev else (None, None, _nowiso())
        if px is not None:
            _close_row(row, _remaining(row), px, NOT_HELD, at, oid)
        else:
            _update(row["id"], {"exit_ts": at, "exit_reason": NOT_HELD, "graded_at": _nowiso(), "status": "closed"})
        out.append(row["id"])
    return out


def _fetch_orders(after_iso) -> list:
    AT = _AT()
    out, until = [], None
    for _ in range(10):                                     # page back while a full page comes back
        # nested=true: bracket child legs come INSIDE their parent, never as separate orders
        params = {"status": "closed", "limit": 500, "direction": "desc", "after": after_iso, "nested": "true"}
        if until:
            params["until"] = until
        code, data = AT._get("/orders", params)
        if code != 200 or not isinstance(data, list):
            raise RuntimeError(f"/orders {code}")
        out.extend(data)
        if len(data) < 500:
            break
        until = data[-1].get("submitted_at") or data[-1].get("created_at")
        if not until:
            break
    seen, uniq = set(), []
    for o in out:
        if o.get("id") and o["id"] not in seen:
            seen.add(o["id"])
            uniq.append(o)
    return uniq


def sync(now_et=None, force=False) -> dict:
    global _LAST_SYNC
    try:
        if not force and time.time() - _LAST_SYNC < SYNC_EVERY_S:
            return {"skipped": "throttled"}
        _LAST_SYNC = time.time()
        _ensure()
        now = now_et or _AT()._now_et()
        first = not _q("SELECT 1 FROM agent_nico_sells WHERE order_id=?", (_BACKFILL_MARK,))
        after = (now - timedelta(days=120 if first else 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Only top-level orders Nico placed in the UI, plus the legs of THOSE orders. The legs of a
        # Mason bracket (created by Alpaca, no "mason-" id, possibly no source) are never looked at.
        orders = []
        fetched = _fetch_orders(after)
        for o in fetched:
            if _is_ui(o):
                orders.append(o)
                orders.extend(l for l in (o.get("legs") or []) if isinstance(l, dict) and l.get("id"))
            elif _is_mason_sale_of_nico_stock(o):
                # books follow-up 4: a PART sale of Nico's shares by Mason now reduces his row (a full one used to
                # wait for the "no longer held" close below, which never comes while Mason holds the same stock)
                orders.append(o)
        orders.sort(key=lambda o: str(o.get("filled_at") or ""))
        out = {"inserted": [], "sells": [], "expired": [], "deferred": 0}
        for o in orders:
            fl = _fill(o)
            if not fl:
                if str(o.get("status") or "").lower() == "partially_filled":
                    out["deferred"] += 1
                continue
            side = str(o.get("side") or "").lower()
            intent = str(o.get("position_intent") or "").lower()
            if side == "buy" and intent in ("", "buy_to_open"):
                if _insert_buy(o, *fl):
                    out["inserted"].append(o["id"])
            elif side == "sell" and intent in ("", "sell_to_close"):
                r = _apply_sell(o, *fl, entered_before_sale=_is_mason_sale_of_nico_stock(o))
                if "skipped" not in r:
                    out["sells"].append(r)
        pos = _AT()._positions()
        if pos.get("ok"):
            waiting = []
            out["expired"] = _expire(now.date(), pos.get("positions") or [], fetched, waiting)
            if waiting:
                out["expiry_waiting"] = waiting
            out["not_held"] = _close_not_held(pos.get("positions") or [], fetched, now.date())
        if first:
            _exec("INSERT OR IGNORE INTO agent_nico_sells(order_id, processed_at) VALUES (?,?)",
                  (_BACKFILL_MARK, _nowiso()))
        out["seeded"] = seed().get("updated", [])
        return out
    except Exception as e:
        return {"error": f"sync: {type(e).__name__}: {e}"}


# ── theses ───────────────────────────────────────────────────────────────────────

def seed() -> dict:
    try:
        _ensure()
        upd = []
        for row in _q("SELECT id, occ FROM agent_trades WHERE source='nico' AND COALESCE(thesis,'')='' "
                      "AND occ IS NOT NULL AND COALESCE(record_deleted,0)=0"):
            o = parse_occ(row["occ"])
            t = o and _seed_for(o["underlying"], o["vehicle"], o["expiry"].isoformat(), o["strike"])
            if t:
                _update(row["id"], {"thesis": t})
                upd.append(row["id"])
        return {"ok": True, "updated": upd}
    except Exception as e:
        return {"ok": False, "error": f"seed: {e}"}


def set_thesis(row_id, text) -> dict:
    try:
        _ensure()
        if not _q("SELECT 1 FROM agent_trades WHERE id=? AND source='nico'", (row_id,)):
            return {"ok": False, "error": "no such nico row"}
        _update(row_id, {"thesis": str(text or "")[:2000]})
        return {"ok": True, "id": row_id}
    except Exception as e:
        return {"ok": False, "error": f"set_thesis: {e}"}


def _rows():
    return _q("SELECT * FROM agent_trades WHERE source='nico' AND COALESCE(record_deleted,0)=0")


def stats() -> dict:
    try:
        _ensure()
        rows = _rows()
        closed = [r for r in rows if r["status"] == "closed"]
        wins = sum(1 for r in closed if r["win"] == 1)
        graded = [r for r in closed if r["win"] is not None]      # a close with no known price is not a loss
        return {"n_open": sum(1 for r in rows if r["status"] == "open"), "n_closed": len(closed),
                "wins": wins, "win_rate": (wins / len(graded)) if graded else None,
                "pnl_usd": round(sum(r["pnl_usd"] or 0.0 for r in closed), 2)}
    except Exception as e:
        return {"n_open": 0, "n_closed": 0, "wins": 0, "win_rate": None, "pnl_usd": 0.0, "error": f"stats: {e}"}


def thesis_text(max_rows=8) -> str:
    try:
        _ensure()
        rows = _rows()
        if not rows:
            return ""
        last = {}
        try:
            pos = _AT()._positions()
            if pos.get("ok"):
                last = {str(p.get("symbol") or "").upper(): _f(p.get("current_price"))
                        for p in pos.get("positions") or []}
        except Exception:
            last = {}
        st = stats()
        wr = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "n/a"
        L = [HEADER, f"record: closed {st['n_closed']}, winners {st['wins']} ({wr}), total ${st['pnl_usd']:+,.2f}"]
        op = sorted([r for r in rows if r["status"] == "open"], key=lambda r: str(r["entry_ts"] or ""), reverse=True)
        cl = sorted([r for r in rows if r["status"] == "closed"], key=lambda r: str(r["exit_ts"] or ""), reverse=True)
        for r in (op + cl)[:max_rows]:
            sym = r["occ"] or r["ticker"]
            entry = r["entry_px"]
            px = last.get(str(sym).upper()) if r["status"] == "open" else r["exit_px"]
            if px is not None and entry:
                mv = f"{px:.2f} ({(px - entry) / entry * 100:+.1f}%)"
            else:
                mv = "n/a"
            state = "open" if r["status"] == "open" else f"closed {r['exit_reason'] or ''}".strip()
            ent = f"{entry:.2f}" if entry is not None else "n/a"
            L.append(f"- {sym} {r['vehicle']} qty {float(r['qty'] or 0):g} @ {ent} → {mv} [{state}] — "
                     f"{(r['thesis'] or '').strip() or 'no thesis given'}")
        return "\n".join(L)
    except Exception:
        return ""
