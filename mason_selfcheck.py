"""
mason_selfcheck.py — Mason's self-check and self-repair job (2026-11-13).

Runs every ~15 minutes from the analyst desk scheduler. No model call, no cost.
It compares what Mason BELIEVES (journal, schedule, reminders) with what is TRUE
(Alpaca, the spend ledger, the service log), fixes the cases that have exactly one
right answer, and reports the rest to the inbox.

Repair rule (Nico, 2026-11-13): a journal row may be closed automatically ONLY
when Alpaca's own data proves it. Nothing is repaired when Alpaca answers with an
error (4xx/5xx), a rate limit, a timeout, or data that looks wrong. In those
cases the run records "broker data unavailable" and changes nothing.

Extra safety on top of that rule:
  * a row must be seen as a ghost in TWO runs at least 10 minutes apart;
  * an empty position list while the journal has open rows counts as bad data;
  * the closing price and date come from Alpaca's filled sell order, never a guess;
  * rows tied to a personal (non-Alpaca) account are never touched.

Switches (mason.config.json, read every run, all optional):
  selfcheck.enabled             default true   — run the checks at all
  selfcheck.auto_close_journal  default true   — allow the journal repair above
  selfcheck.option_rows         default false  — report open options journal rows whose trade ended (2026-11-18;
                                                 report only, see mason_option_journal.selfcheck_rows)

Protection of the positions Nico entered (mason_protect, 2026-11-13) runs as the FIRST step of
every pass and has its own switches (protect.enabled, protect.place_orders; both default false).
Only the scheduled run may place, change or cancel an order; a report-only run (the API route,
the chat tool) lists what it would do. Its result is the "protection" section of the payload.

Public: run() -> dict, last() -> dict, summary_text() -> str.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import mason_db

log = logging.getLogger("mason.selfcheck")
BASE = Path(__file__).parent
STATE_KEY = "selfcheck_last"
SEEN_KEY = "selfcheck_ghost_seen"
MIN_CONFIRM_SECONDS = 600


# ── small helpers ───────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        return json.loads((BASE / "mason.config.json").read_text())
    except Exception:
        return {}


def _hcfg() -> dict:
    return (_cfg().get("selfcheck") or {})


def _state_get(key, default=None):
    try:
        import mason_analyst_desk as AD
        v = AD.get_state(key)
        return default if v is None else v
    except Exception:
        return default


def _state_set(key, value):
    try:
        import mason_analyst_desk as AD
        AD.set_state(key, value)
    except Exception as e:
        log.warning(f"[selfcheck] state write failed ({key}): {e}")


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def _issue(key, severity, text, **extra) -> dict:
    return {"key": key, "severity": severity, "text": text, **extra}


# ── broker reads: every one is validated; a bad read returns (None, reason) ─────

def _alpaca(path, params=None):
    """GET one Alpaca endpoint. Returns (data, None) on a clean 200 with a list/dict
    body, else (None, reason). Retries twice through a rate limit only."""
    try:
        import alpaca_integration as A
        if not A.ALPACA_KEY or not A.ALPACA_SECRET:
            return None, "Alpaca keys not configured"
        r = None
        for i in range(3):
            r = requests.get(f"{A.ALPACA_URL}/v2/{path}", headers=A._headers(),
                             params=params or {}, timeout=10)
            if r.status_code != 429:
                break
            time.sleep(2 * (i + 1))
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        if not isinstance(data, (list, dict)):
            return None, "unexpected body"
        return data, None
    except Exception as e:
        return None, f"{type(e).__name__}"


def _valid_positions(data) -> bool:
    if not isinstance(data, list):
        return False
    for p in data:
        if not isinstance(p, dict) or not p.get("symbol") or _f(p.get("qty")) is None:
            return False
    return True


def _valid_orders(data) -> bool:
    if not isinstance(data, list):
        return False
    for o in data:
        if not isinstance(o, dict) or not o.get("symbol") or not o.get("side") or not o.get("id"):
            return False
    return True


# ── check 1: journal vs Alpaca (stock rows), with the one automatic repair ──────

def _et_day(iso):
    """Alpaca stamps fills in UTC; the journal's dates are Eastern. Returns YYYY-MM-DD (ET) or None."""
    try:
        from zoneinfo import ZoneInfo
        s_ = str(iso).replace("Z", "+00:00")
        if "." in s_:                                   # trim nanoseconds to microseconds
            head, _, rest = s_.partition(".")
            frac, tz = rest[:-6], rest[-6:]
            s_ = f"{head}.{frac[:6]}{tz}"
        return datetime.fromisoformat(s_).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return None


def _is_alpaca_row(t) -> bool:
    """False for rows that live in the journal but not at Alpaca (another brokerage, Roth, CSV imports).
    If the shared filter cannot be loaded, treat the row as NOT Alpaca's (never repair it)."""
    try:
        import mason_execution as EX
        return bool(EX._is_alpaca_row(t))
    except Exception:
        return False


def _used_exit_order_ids(ticker):
    """Order ids already recorded as the exit of any journal row for this ticker, or None on a read error."""
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT exit_order_id FROM trades WHERE UPPER(ticker)=? AND "
                                "exit_order_id IS NOT NULL AND exit_order_id<>''", (ticker.upper(),)).fetchall()
        finally:
            conn.close()
        return {r["exit_order_id"] for r in rows}
    except Exception:
        return None


def _open_journal_rows() -> list:
    import mason_trades as T
    return [t for t in T.get_open_trades() if (t.get("status") or "open") == "open"]


def _sell_fills(symbol, after_date):
    """Filled SELL orders for symbol on/after after_date, straight from Alpaca.
    Returns (fills, None) or (None, reason)."""
    data, err = _alpaca("orders", {"status": "closed", "symbols": symbol, "limit": 500,
                                   "direction": "desc", "nested": "true"})
    if err:
        return None, err
    if not _valid_orders(data):
        return None, "orders data failed validation"
    flat = []
    for o in data:
        flat.append(o)
        for leg in (o.get("legs") or []):
            if isinstance(leg, dict):
                flat.append(dict(leg, _leg=True, _parent=o.get("id")))
    fills = []
    for o in flat:
        if str(o.get("symbol")).upper() != symbol.upper() or o.get("side") != "sell":
            continue
        fq, px, at = _f(o.get("filled_qty")), _f(o.get("filled_avg_price")), str(o.get("filled_at") or "")
        if not fq or fq <= 0 or px is None or px <= 0 or len(at) < 10:
            continue
        day = _et_day(at)
        if day is None or day < str(after_date or "")[:10]:
            continue
        fills.append({"id": o.get("id"), "qty": fq, "px": px, "at": at, "day": day,
                      "type": o.get("type"), "leg": bool(o.get("_leg")),
                      "coid": str(o.get("client_order_id") or ""), "parent": o.get("_parent")})
    return fills, None


def _owner_keys(trs, fills):
    """Books follow-up 3 (2026-11-16): whose sale each fill was and whose each row is, for the ghost close below,
    so that on a ticker Nico and Mason both held each row gets the price of its OWN sale.

    Returns ({row id: key}, {fill id: key}) where key is a Mason trade's entry order id (or "mason" when that is
    unknown) or "nico". A fill is Mason's when Mason's records name it as a trade's exit, its client id is
    'mason-exit-<rec>-' / 'mason-dir-<rec>-', or it is a leg of the bracket bought by a Mason row's entry order;
    any other fill is "nico" (the reconcile's rule: a sale of unknown origin is Nico's). Returns (None, None) when
    no row is Mason's or the owner rules cannot be loaded: then the plan is exactly the old one."""
    try:
        import mason_execution as EX
        owners = EX._SellOwners()
        base = owners.blank()
        mason = [t for t in trs if EX._is_mason_row(t, base)]
        if not mason:
            return None, None
        links = owners.links()
        rows = {t["id"]: ((t.get("entry_order_id") or "mason") if EX._is_mason_row(t, base) else "nico")
                for t in trs}
        entries = {t.get("entry_order_id") for t in mason if t.get("entry_order_id")}
        out = {}
        for f in fills:
            key = "nico"
            link = next((l for l in links if f["id"] in l["exit_oids"]), None)
            m = EX._MASON_SELL_RE.match(f.get("coid") or "")
            if link is None and m:
                link = next((l for l in links if str(l["rec_id"] or "") == m.group(1)), None) or {"entry_oid": None}
            if link is not None:
                key = link.get("entry_oid") or "mason"
            elif f.get("parent") and f["parent"] in entries:
                key = f["parent"]
            out[f["id"]] = key
        return rows, out
    except Exception as e:
        log.debug(f"[selfcheck] owner rules unavailable ({e}); plain newest-first plan")
        return None, None


def _check_journal(issues, repairs, auto_close: bool):
    import mason_trades as T
    positions, err = _alpaca("positions")
    if err or not _valid_positions(positions):
        issues.append(_issue("alpaca_unavailable", "warn",
                             f"Broker data unavailable ({err or 'positions data failed validation'}). "
                             f"No journal checks or repairs were made this run."))
        return None
    rows = _open_journal_rows()
    stock_pos = {str(p["symbol"]).upper(): p for p in positions
                 if (p.get("asset_class") or "us_equity") == "us_equity"}
    if rows and not positions:
        issues.append(_issue("alpaca_empty_book", "warn",
                             f"Alpaca reported zero positions while the journal has {len(rows)} open "
                             f"rows. Treated as bad data; nothing was changed."))
        return None

    try:                                   # idle-cash rule: shares parked in the index fund have their own ledger
        import mason_idle_cash as _IC      # (park_ledger) and no journal row; {} when nothing is parked
        parked = dict(_IC.owned_map() or {})   # (review fix) a working purchase of the rule counts in full
    except Exception:
        parked = {}
    by_ticker = {}
    for t in rows:
        # Books fix 2026-11-15 (item C): a row of another account (a share held elsewhere) is neither counted
        # against Alpaca's shares nor ever a candidate for the "not held at Alpaca" repair below.
        if not _is_alpaca_row(t):
            continue
        by_ticker.setdefault(str(t.get("ticker") or "").upper(), []).append(t)

    try:
        seen = json.loads(_state_get(SEEN_KEY, "{}") or "{}")
    except Exception:
        seen = {}
    now = time.time()
    still = {}

    for tk, trs in by_ticker.items():
        jq = sum(_f(t.get("shares")) or 0 for t in trs)
        pos = stock_pos.get(tk)
        if pos is not None:
            aq = abs(_f(pos.get("qty")) or 0) - parked.get(tk, 0.0)
            if abs(aq - jq) > 1e-6:
                issues.append(_issue(f"qty_mismatch:{tk}", "info",
                                     f"{tk}: journal shows {jq:g} shares open across {len(trs)} row(s), "
                                     f"Alpaca holds {aq:g}.", ticker=tk))
            continue
        # ticker is open in the journal but not held at Alpaca: judge all its rows together
        elig = []
        for t in trs:
            found, refusal = T.close_refusal(t["id"])
            if not found or refusal or not _is_alpaca_row(t):
                continue                                  # personal-account rows are not Alpaca's to judge
            shares = _f(t.get("shares")) or 0
            if (t.get("direction") or "long") != "long" or shares <= 0:
                issues.append(_issue(f"ghost:{t['id']}", "warn",
                                     f"{tk}: journal row {t['id']} is open but Alpaca holds no {tk}. Not a "
                                     f"plain long row; needs a manual look.", ticker=tk))
                continue
            elig.append(t)
        if not elig:
            continue

        def _flag(sev, tail):
            for t in elig:
                issues.append(_issue(f"ghost:{t['id']}", sev,
                                     f"{tk}: journal row {t['id']} is open ({_f(t.get('shares')) or 0:g} sh @ "
                                     f"{t.get('entry_price')}) but Alpaca holds no {tk}. {tail}", ticker=tk))

        fills, ferr = _sell_fills(tk, min(str(t.get("entry_date") or "") for t in elig))
        if ferr:
            _flag("warn", f"Could not read Alpaca's orders ({ferr}); nothing changed.")
            continue
        used_ids = _used_exit_order_ids(tk)
        if used_ids is None:
            _flag("warn", "Could not read the journal's earlier exits; nothing changed.")
            continue
        fills = [dict(f) for f in fills if f["id"] not in used_ids     # each fill closes shares once
                 and not str(f.get("coid") or "").startswith("mason-park-")]   # (review fix) a sale of parked cash
        #                                                                  is never the exit of a journal row
        # newest rows take the newest fills; a fill only counts for a row entered on/before it.
        # Books follow-up 3: when Mason's and Nico's rows meet here, every row first takes the fills of its own
        # owner (the same Mason trade; then any Mason fill for a Mason row), and only then any fill left. Without a
        # Mason row there is one pass over every fill, exactly the old plan.
        row_key, fill_key = _owner_keys(elig, fills)
        if row_key is None:
            passes = [lambda t, f: True]
        else:
            passes = [lambda t, f: fill_key.get(f["id"]) == row_key[t["id"]],
                      lambda t, f: row_key[t["id"]] != "nico" and fill_key.get(f["id"]) != "nico",
                      lambda t, f: True]
        plan, okay = {}, True
        pool = sorted(fills, key=lambda x: x["at"], reverse=True)
        need = {t["id"]: _f(t.get("shares")) or 0 for t in elig}
        newest_first = sorted(elig, key=lambda x: str(x.get("entry_date") or ""), reverse=True)
        for ok_for in passes:
            for t in newest_first:
                used = plan.setdefault(t["id"], [])
                for f in pool:
                    if need[t["id"]] <= 1e-9:
                        break
                    if f["qty"] <= 1e-9 or f["day"] < str(t.get("entry_date") or "")[:10] or not ok_for(t, f):
                        continue
                    q = min(need[t["id"]], f["qty"])
                    f["qty"] -= q
                    need[t["id"]] -= q
                    used.append((q, f))
        okay = all(v <= 1e-6 for v in need.values())
        if not okay:
            _flag("warn", f"Alpaca's sell fills since entry do not cover all {len(elig)} open row(s) of {tk}, "
                          f"so nothing is closed automatically.")
            continue
        for t in elig:
            tid = t["id"]
            first = _f(seen.get(tid))
            still[tid] = first or now
        if not auto_close:
            _flag("warn", "Alpaca's sell fills cover it, but automatic closing is off for this run; the "
                          "scheduled check closes it if switched on.")
            continue
        if any((not _f(seen.get(t["id"]))) or now - _f(seen.get(t["id"])) < MIN_CONFIRM_SECONDS for t in elig):
            _flag("info", "Alpaca's sell fills cover it; it will be closed on a later check if this still holds.")
            continue
        for t in elig:
            tid, shares, used = t["id"], _f(t.get("shares")) or 0, plan[t["id"]]
            px = round(sum(q * f["px"] for q, f in used) / shares, 4)
            last = max(used, key=lambda x: x[1]["at"])[1]
            typ = str(last.get("type") or "")
            reason = ("stop_hit" if typ.startswith(("stop", "trailing")) else
                      ("target_hit" if typ == "limit" and last.get("leg") else "manual"))
            try:
                T.close_trade(tid, px, reason,
                              f"Closed automatically by Mason's self-check on "
                              f"{datetime.now().strftime('%Y-%m-%d')}: Alpaca holds no {tk} and its order "
                              f"history shows {shares:g} shares sold at an average ${px} "
                              f"(last fill {last['day']}, order {last['id']}).")
            except Exception as e:
                issues.append(_issue(f"ghost:{tid}", "warn",
                                     f"{tk}: automatic close of journal row {tid} refused: {e}", ticker=tk))
                continue
            try:
                conn = mason_db.get_connection()
                try:
                    with conn:
                        conn.execute("UPDATE trades SET exit_date=?, exit_order_id=COALESCE(exit_order_id, ?) "
                                     "WHERE id=?", (last["day"], last["id"], tid))
                finally:
                    conn.close()
            except Exception as e:
                log.warning(f"[selfcheck] closed {tid} but could not stamp exit date/order: {e}")
            still.pop(tid, None)
            repairs.append({"kind": "journal_auto_close", "ticker": tk, "trade_id": tid,
                            "exit_px": px, "exit_date": last["day"], "order_id": last["id"],
                            "text": f"Closed journal row {tid}: {tk} {shares:g} sh, exit ${px} on "
                                    f"{last['day']} (from Alpaca order {str(last['id'])[:8]})."})
    _state_set(SEEN_KEY, json.dumps(still))

    for tk, pos in stock_pos.items():
        if tk not in by_ticker:
            if parked.get(tk) and abs(abs(_f(pos.get("qty")) or 0) - parked[tk]) <= 1e-6:
                continue                   # every share here is parked cash (recorded in park_ledger)
            issues.append(_issue(f"unjournaled:{tk}", "info",
                                 f"{tk}: Alpaca holds {pos.get('qty')} shares with no open journal row.",
                                 ticker=tk))
    try:                                   # the learning loop's own ledger vs Alpaca (report only)
        held = {str(p["symbol"]).upper() for p in positions}
        conn = mason_db.get_connection()
        try:
            arows = conn.execute("SELECT id, source, ticker, occ, qty FROM agent_trades WHERE status='open' "
                                 "AND COALESCE(record_deleted,0)=0").fetchall()
        finally:
            conn.close()
        for r in arows:
            sym = str(r["occ"] or r["ticker"] or "").upper()
            if sym and sym not in held:
                who = "Mason's" if r["source"] == "mason" else "Nico's"
                issues.append(_issue(f"loop_ghost:{r['id']}", "warn",
                                     f"{sym}: the learning loop lists {who} trade {r['id']} ({r['qty']:g}) as "
                                     f"open, but Alpaca holds no {sym}.", ticker=r["ticker"]))
    except Exception as e:
        log.debug(f"[selfcheck] loop ledger check skipped: {e}")
    return {"positions": positions, "rows": rows, "stock_pos": stock_pos}


# ── check 1b: options journal rows vs Alpaca (2026-11-18) ───────────────────────

def _check_option_rows(issues, ctx):
    """An open options journal row whose trade has ended: REPORTED ONLY (selfcheck.option_rows true; off by default
    until Nico approves the wording). This check runs on the desk worker next to every exit check, so it reads
    nothing from Alpaca and writes nothing (review 2026-11-18); the 5-minute fill reconcile closes Mason's own rows
    from Alpaca's own records (mason_option_journal.retry_due). Runs only after a good, validated positions read."""
    import mason_option_journal as OJ
    issues.extend(OJ.selfcheck_rows(ctx["positions"]))


def _check_agent_loop(issues):
    """(gap fixes, 2026-11-18) Errors returned by the steps of Mason's trading loop (placing queued calls, booking
    fills, the exit checks) in the last 20 minutes. Before, nobody heard about them. REPORTED ONLY; off unless
    selfcheck.agent_loop_errors is true (new wording, waits for Nico's approval). Reads memory only."""
    import mason_agent_loop as AL
    names = {"pending_closes": "cancelling orders of closed calls", "option_orders": "option order handling",
             "settle": "booking fills", "queue": "placing queued calls", "spine": "the trade records",
             "exits": "the exit checks", "book": "the director's sell instructions", "idle_cash": "the idle-cash rule",
             "option_orders_follow": "option order records", "excursions": "tracking open trades",
             "calibration": "the confidence records", "nico": "Nico's theses", "report": "the daily report",
             "tick": "the trading loop itself"}
    for e in AL.step_errors(20 * 60):
        step = str(e.get("step"))
        n = int(e.get("count") or 1)
        issues.append(_issue(f"agent_loop_error:{step}", "warn",
                             f"Heads up: Mason's trading loop tripped up in {names.get(step, step)} "
                             f"({n} time{'s' if n != 1 else ''}, last at {str(e.get('last_at'))[11:16]}): "
                             f"{str(e.get('error'))[:160]}"))


# ── check 2: positions under their journal stop with no broker stop order ───────

def _check_stops(issues, ctx):
    if not ctx:
        return
    orders, err = _alpaca("orders", {"status": "open", "limit": 200, "nested": "true"})
    if err or not _valid_orders(orders):
        return
    guarded = set()
    for o in orders:
        for x in [o] + [l for l in (o.get("legs") or []) if isinstance(l, dict)]:
            if x.get("side") == "sell" and str(x.get("type") or "").startswith(("stop", "trailing")):
                guarded.add(str(x.get("symbol")).upper())
    for t in ctx["rows"]:
        tk = str(t.get("ticker") or "").upper()
        stop, pos = _f(t.get("stop_loss")), ctx["stock_pos"].get(tk)
        if pos is None or (t.get("direction") or "long") != "long" or not _is_alpaca_row(t):
            continue                                     # (a row of another account has no stop at Alpaca)
        if not stop:
            issues.append(_issue(f"no_stop:{t['id']}", "info",
                                 f"{tk}: journal row {t['id']} has no stop recorded.", ticker=tk))
            continue
        cur = _f(pos.get("current_price"))
        if cur is not None and cur < stop and tk not in guarded:
            issues.append(_issue(f"stop_pierced:{tk}", "warn",
                                 f"{tk} is at ${cur:g}, below the journal stop of ${stop:g}, and there is "
                                 f"no stop order at Alpaca. The stop exists on paper only.", ticker=tk))


# ── check 3: scheduled director calls actually finished ─────────────────────────

def _check_anchors(issues):
    try:
        import mason_analyst_desk as AD
        import mason_director as DIR
        cfg = _cfg()
        od = ((cfg.get("analyst_desk") or {}).get("opus_director") or {})
        if not od.get("enabled") or not (od.get("v2") or {}).get("enabled"):
            return
        et = AD._now_et(time.time())
        if not DIR.is_trading_day(et):
            return
        today = et.strftime("%Y-%m-%d")
        ran = set()
        p = BASE / "director_spend.jsonl"
        if p.exists():
            # desk checks write to the same ledger (up to ~25 lines a day): 40 lines no longer reach back to
            # the 07:00 call on a busy day or after max_checks_per_day is raised, which would report a call
            # that DID run as missing and invite a second paid run
            for line in p.read_text().splitlines()[-600:]:
                try:
                    d = json.loads(line)
                    if d.get("day") == today:
                        ran.add(d.get("kind"))
                except Exception:
                    pass
        times = (((cfg.get("analyst_desk") or {}).get("anchors") or {}).get("times_et")) or ["07:00", "13:00"]
        for t in times:
            hh, _, mm = str(t).partition(":")
            due = et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if et < due + timedelta(minutes=25):
                continue
            kind = "anchor_" + str(t).replace(":", "")
            if kind not in ran:
                issues.append(_issue(f"anchor_missing:{today}:{kind}", "warn",
                                     f"The {t} ET director call has no entry in today's spend ledger. It "
                                     f"did not finish. Use 'Run director now (live)' to run it."))
    except Exception as e:
        log.debug(f"[selfcheck] anchor check skipped: {e}")


# ── check 4: errors in the service log, reminders ────────────────────────────────

def _check_log(issues):
    try:
        out = subprocess.run(["journalctl", "-u", "mason", "--since", "-16 min", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return
    tb = sum(1 for l in out.splitlines() if l.startswith("Traceback (most recent call last)"))
    tool_err = {}
    for l in out.splitlines():
        if '"TRACE tool_exec"' in l or "TRACE tool_exec" in l:
            i = l.find('"result": "Tool ')
            if i > 0 and " error: " in l[i:i + 120]:
                name = l[i + 16:].split(" ", 1)[0]
                tool_err[name] = tool_err.get(name, 0) + 1
    if tb:
        issues.append(_issue("log_tracebacks", "warn",
                             f"{tb} crash trace(s) in the service log in the last 15 minutes."))
    for name, n in tool_err.items():
        issues.append(_issue(f"tool_error:{name}", "warn",
                             f"The chat tool '{name}' failed {n} time(s) in the last 15 minutes."))


def _check_reminders(issues):
    try:
        import mason_agenda
        rems = [r for r in mason_agenda.list_reminders() if not r.get("done")]
        today = datetime.now().date().isoformat()
        overdue = [r for r in rems if r.get("fire_on") and str(r["fire_on"]) < today]
        if len(overdue) >= 5:
            issues.append(_issue("reminders_overdue", "info",
                                 f"{len(overdue)} reminders are past their date and still open."))
    except Exception as e:
        issues.append(_issue("reminders_unreadable", "warn", f"Reminders could not be read: {e}"))


# ── run ─────────────────────────────────────────────────────────────────────────

def run(notify: bool = True, allow_repair: bool = True) -> dict:
    """One full pass. Never raises. allow_repair=False reports only (used by the chat tool and
    the API route, so every repair comes from the scheduled run and always reaches the inbox)."""
    h = _hcfg()
    issues, repairs = [], []
    # The repairing run books fills into the journal (protective sales, ghost-row closes), so it holds the same
    # books lock as mason_execution.reconcile(): the two can never book one sale twice. If a reconcile holds it
    # for over a minute, this run only reports, and the next scheduled run repairs.
    # Books follow-up 7 (2026-11-16): the lock covers only the two steps that write (protection, the journal check
    # with its repair). The read-only checks after them (stops, anchors, service log, reminders) run without it, so
    # a reconcile or autotrade's journaling no longer waits for the 20-second journalctl read.
    def _step(name, fn):
        try:
            return fn()
        except Exception as e:
            log.warning(f"[selfcheck] {name} check failed: {e}")
            issues.append(_issue(f"check_failed:{name}", "warn", f"The {name} check itself failed: {e}"))
            return None

    with _books_lock(allow_repair) as held:
        repair = bool(allow_repair and held)
        if allow_repair and not held:
            issues.append(_issue("selfcheck_books_busy", "info",
                                 "A fill reconcile was running, so this check only reported; the next one repairs."))
        # Protective orders on the positions Nico entered: first, so that a protective sale is booked
        # as a stop-out of the right journal row before the ghost check below looks at the journal.
        protection = _protection(act=bool(notify and repair))
        if h.get("enabled") is False:
            return {"enabled": False, "protection": protection}
        ctx = _step("journal", lambda: _check_journal(issues, repairs,
                                                      repair and h.get("auto_close_journal") is not False))
        if ctx and h.get("option_rows") is True:          # (2026-11-18) options journal rows vs Alpaca: reported
            _step("options journal", lambda: _check_option_rows(issues, ctx))     # only; off unless switched on
    if h.get("agent_loop_errors") is True:                # (2026-11-18, gap fixes) off unless switched on
        _step("trading loop", lambda: _check_agent_loop(issues))
    for name, fn in (("journal", lambda: _check_stops(issues, ctx)),     # (reads only; nothing when ctx is None)
                     ("anchors", lambda: _check_anchors(issues)),
                     ("log", lambda: _check_log(issues)),
                     ("reminders", lambda: _check_reminders(issues))):
        _step(name, fn)
    out = {"enabled": True, "at": datetime.now().isoformat(timespec="seconds"),
           "ok": not any(i["severity"] != "info" for i in issues),
           "issues": issues, "repairs": repairs, "protection": protection}
    prev = last()
    _state_set(STATE_KEY, json.dumps(out))
    if notify or repairs:                    # a repair is always announced
        try:
            _notify(out, prev if notify else {"issues": out["issues"]})
        except Exception as e:
            log.warning(f"[selfcheck] notify failed: {e}")
    log.info(f"[selfcheck] {len(issues)} issue(s), {len(repairs)} repair(s)")
    return out


def _books_lock(want: bool):
    """mason_execution's books lock (waits up to 60 s) when this run may repair; a no-op context otherwise."""
    import contextlib
    if not want:
        return contextlib.nullcontext(False)
    try:
        import mason_execution
        return mason_execution.books_lock(timeout=60)
    except Exception as e:
        log.warning(f"[selfcheck] books lock unavailable ({e}); running without it")
        return contextlib.nullcontext(True)


def _protection(act: bool) -> dict:
    """mason_protect's pass (its own switches; off = a one-line result). Never raises."""
    try:
        import mason_protect
        return mason_protect.run(act=act)
    except Exception as e:
        log.warning(f"[selfcheck] protection pass failed: {e}")
        return {"enabled": None, "ok": False, "error": f"The protection pass could not run ({type(e).__name__})."}


def _notify(out, prev):
    import mason_notifications as N
    for r in out["repairs"]:
        N.add_notification("system", f"Self-repair: {r['text']}", body=r["text"], severity="info",
                           ticker=r.get("ticker"), created_by="mason_selfcheck",
                           dedup_key=f"selfcheck_repair:{r.get('trade_id')}")
    warn = sorted(i["key"] for i in out["issues"] if i["severity"] != "info")
    before = sorted(i["key"] for i in (prev.get("issues") or []) if i.get("severity") != "info")
    if warn and warn != before:
        lines = [i["text"] for i in out["issues"] if i["severity"] != "info"]
        N.add_notification("system", f"Health check: {len(lines)} thing(s) need attention",
                           body="\n".join("- " + x for x in lines), severity="warn",
                           created_by="mason_selfcheck",
                           dedup_key="selfcheck:" + datetime.now().strftime("%Y-%m-%d"))


def last() -> dict:
    try:
        return json.loads(_state_get(STATE_KEY, "{}") or "{}")
    except Exception:
        return {}


def summary_text(fresh: bool = False) -> str:
    """Plain-text report for the chat tool."""
    d = run(notify=False, allow_repair=False) if fresh else (last() or run(notify=False, allow_repair=False))
    prot = []
    try:
        if (d or {}).get("protection") is not None:
            import mason_protect
            prot = mason_protect.summary_lines(d.get("protection"))
    except Exception:
        prot = []
    try:                                          # background loops that are stuck or stopped (mason_runtime)
        import mason_runtime
        prot = prot + mason_runtime.summary_lines()
    except Exception:
        pass
    if not d or d.get("enabled") is False:
        return "\n".join(["Health check is switched off (selfcheck.enabled=false)."] + prot)
    lines = [f"Health check at {d.get('at')}: " + ("all clear." if not d.get("issues") else
             f"{len(d['issues'])} finding(s), {len(d.get('repairs') or [])} automatic repair(s).")]
    for r in d.get("repairs") or []:
        lines.append(f"  REPAIRED: {r['text']}")
    for i in d.get("issues") or []:
        lines.append(f"  [{i['severity']}] {i['text']}")
    return "\n".join(lines + prot)


if __name__ == "__main__":
    print(summary_text(fresh=True))
