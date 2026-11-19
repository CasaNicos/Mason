"""
mason_execution.py — Mason's execution-integrity layer for Alpaca paper trading.

The promise this module keeps: when Mason says he placed an order, it was really
submitted; we then confirm it actually filled and at what price; and Alpaca is the
single source of truth — the local journal is reconciled to mirror real fills.

Design:
  • ONE order path. place_trade() decides single vs BRACKET automatically:
    a stock trade that carries both a stop and a target becomes a bracket order
    (entry + stop-loss leg + take-profit leg) so risk is enforced at the broker.
    Anything without a clean stop+target (or an option) is a single order.
  • IDEMPOTENT. Every order carries a deterministic client_order_id so a retry or
    double-fire can't create a duplicate position.
  • PRE-CHECKS. Buying power, market clock, and the regime gate's new-entries
    policy are checked first; hard blocks stop the order, soft warnings ride along.
  • VERIFIED. verify_fill() polls until the order is filled / partially filled /
    rejected and records the REAL filled qty and average price.
  • AUDITED. Every intent → order_id → fill is appended to mason_executions.json,
    so "orders Mason spoke about" are provably tied to a broker order and a fill.
  • RECONCILED. reconcile() pulls Alpaca orders, positions and fill activities and
    syncs the trade journal to them, so the learning loop runs off real fills.

Network/transport is funnelled through _request(), which self-tests monkeypatch.
Nothing here raises into Mason — failures return {"ok": False, "error": ...}.
"""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
import hashlib
from datetime import datetime, date, timezone
from pathlib import Path

try:
    import requests
except Exception:
    requests = None

_log = logging.getLogger("mason.execution")   # child of mason_app's "mason" logger (A9 r2 item 3)

BASE         = Path(__file__).parent
EXEC_LEDGER  = BASE / "mason_executions.json"     # canonical audit: intent → order → fill

# Terminal Alpaca order states.
_DONE   = {"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"}
_FILLED = {"filled", "partially_filled"}

# Client-order-id prefix of the protective stop orders mason_protect places on shares Nico entered
# (the same literal is mason_protect.STOP_PREFIX). Such an order holds exactly its own shares at the
# broker; it is not a close attempt of anyone else's.
_PROTECT_STOP_PREFIX = "mason-protect-stop-"


# ── config / transport ───────────────────────────────────────────────────────────

def _cfg():
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _creds():
    pt = (_cfg().get("paper_trading", {}) or {})
    base = (pt.get("base_url") or "https://paper-api.alpaca.markets/v2").rstrip("/")
    import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading (byte-exact legacy)
    key, secret = _S.alpaca_keys(cfg={"paper_trading": pt})
    return key, secret, base, bool(pt.get("enabled"))


def _headers():
    k, s, _, _ = _creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


# Requests made by THIS thread while _req_count.on is set (reconcile() counts its Alpaca calls per pass).
_req_count = threading.local()


def _request(method, path, params=None, body=None, timeout=12):
    """Single choke point for all Alpaca HTTP. Returns (status_code, parsed_json|text)
    or (None, error_str) when the transport itself fails. Monkeypatched in tests."""
    if getattr(_req_count, "on", False):
        _req_count.n = getattr(_req_count, "n", 0) + 1
    if requests is None:
        return None, "requests unavailable"
    k, s, base, enabled = _creds()
    if not k:
        return None, "no Alpaca credentials"
    url = base + path
    try:
        r = requests.request(method, url, headers=_headers(),
                             params=params, json=body, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except Exception as e:
        return None, str(e)


# ── thin read wrappers (Alpaca = source of truth) ────────────────────────────────

def account():
    code, data = _request("GET", "/account")
    if code == 200 and isinstance(data, dict):
        return {"ok": True, "equity": float(data.get("equity", 0) or 0),
                "cash": float(data.get("cash", 0) or 0),
                "buying_power": float(data.get("buying_power", 0) or 0),
                "status": data.get("status")}
    return {"ok": False, "error": data}


def clock():
    code, data = _request("GET", "/clock")
    if code == 200 and isinstance(data, dict):
        return {"ok": True, "is_open": bool(data.get("is_open")),
                "next_open": data.get("next_open"), "next_close": data.get("next_close")}
    return {"ok": False, "error": data}


def positions():
    code, data = _request("GET", "/positions")
    if code == 200 and isinstance(data, list):
        return {"ok": True, "positions": data}
    return {"ok": False, "error": data}


def get_order(order_id):
    code, data = _request("GET", f"/orders/{order_id}", params={"nested": "true"})
    if code == 200 and isinstance(data, dict):
        return {"ok": True, "order": data}
    # books follow-up 2 (2026-11-16): "status" (the HTTP code, None when Alpaca never answered) lets a caller tell
    # "Alpaca says no such order" (404) apart from "Alpaca did not answer"
    return {"ok": False, "error": data, "status": code}


def verify_and_backfill_order(order_id):
    """Ground-truth lookup for backfilling a journal entry from an order that
    ALREADY exists in Alpaca (placed outside place_option_play, or whose
    journal write failed at the time — see mason_options.log_verified_play).

    Added 2026-07-28 (Bug 8). Never trust a model's typed-out order_id/fill
    claim for something that will be marked execution_verified=True — pull
    the real order from Alpaca and return exactly what it says. Returns
    {"ok": False, "error": ...} if the order_id doesn't exist or hasn't
    filled; never guesses or fills in a plausible-looking number."""
    g = get_order(order_id)
    if not g.get("ok"):
        return {"ok": False, "error": f"order {order_id} not found in Alpaca: {g.get('error')}"}
    o = g["order"]
    status = (o.get("status") or "").lower()
    if status not in _FILLED:
        return {"ok": False, "error": f"order {order_id} status is '{status}', not filled — "
                                      f"nothing to backfill yet"}
    return {
        "ok": True,
        "order_id": o.get("id") or order_id,
        "client_order_id": o.get("client_order_id"),
        "contract_symbol": (o.get("symbol") or "").upper(),
        "filled_qty": float(o.get("filled_qty") or 0),
        "filled_avg_price": (float(o["filled_avg_price"])
                             if o.get("filled_avg_price") else None),
        "filled_at": o.get("filled_at") or datetime.now().isoformat(),
        "side": (o.get("side") or "").lower(),
    }


# ── order construction ───────────────────────────────────────────────────────────

def _is_option_symbol(sym):
    import re
    return bool(re.search(r"\d{6}[CP]\d{8}", sym or ""))


def _client_order_id(symbol, pitch_id=None, side="buy", qty=None,
                     entry=None, stop=None, target=None):
    """Deterministic per *logical* order.

    A retry or double-fire of the SAME order (same symbol/side/qty/prices on the
    same day) produces the SAME client_order_id, so Alpaca's client_order_id
    uniqueness rejects the duplicate instead of opening a second position — the
    idempotency this module promises. Genuinely different orders (different side,
    size, or price levels) hash differently and pass through normally.

    The previous implementation appended a random uuid, which made every submit
    unique and silently defeated de-duplication.
    """
    seed = pitch_id or symbol
    day  = date.today().isoformat()
    sig  = "|".join(str(x) for x in (seed, symbol, side, qty, entry, stop, target, day))
    h    = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    return f"mason-{seed}-{day}-{h}"


# ── options tick-size rounding (Bug 3 fix, 2026-07-23) ────────────────────────────
# Alpaca enforces real price increments on option limit orders — a non-conforming
# price is rejected outright if Alpaca's NOM auto-route also fails to place it.
# Source: alpaca.markets/support/options-pricing-increments-and-options-order-
# handling (checked 2026-07-23). This was the actual cause of the "limit price
# parameter error" Mason kept hitting on XLE: $0.31 / $0.32 are $0.01-increment
# prices, but XLE (not SPY/QQQ/IWM, and not confirmed penny-pilot) requires $0.05
# increments under $3 — the valid ticks nearest those prices are $0.30 / $0.35.
#   Non-penny-pilot: $0.05 under $3, $0.10 at/above $3.
#   Penny-pilot (or underlying is SPY/QQQ/IWM, which are ALWAYS penny): $0.01
#   under $3, $0.05 at/above $3.
# Per Nico's call (2026-07-23): auto-round to the nearest valid tick rather than
# rejecting and forcing a re-quote — but always report the adjustment so the
# submitted price is never silently different from what was requested.
_ALWAYS_PENNY_UNDERLYINGS = {"SPY", "QQQ", "IWM"}


def _underlying_from_occ(symbol):
    """Strip an OCC option symbol down to its underlying ticker —
    'XLE260724C00060500' -> 'XLE'. Used only for the SPY/QQQ/IWM always-penny
    exception; returns the symbol unchanged if it doesn't parse as OCC."""
    import re
    m = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", symbol or "")
    return m.group(1) if m else (symbol or "")


def _option_tick_size(price, ppind, underlying=None):
    """Pure — the increment a given option price must land on."""
    is_penny = bool(ppind) or (underlying or "").upper() in _ALWAYS_PENNY_UNDERLYINGS
    if is_penny:
        return 0.01 if price < 3 else 0.05
    return 0.05 if price < 3 else 0.10


def _round_to_tick(price, ppind, underlying=None):
    """Pure — nearest-tick rounding. Returns (rounded_price, tick_used)."""
    tick = _option_tick_size(price, ppind, underlying)
    steps = round(price / tick)
    return round(steps * tick, 2), tick


def get_option_contract_meta(contract_symbol):
    """Single-contract lookup — currently just for ppind (penny-pilot indicator),
    used to round a limit price to Alpaca's real tick before submission. Fails
    SAFE to ppind=False (the coarser, stricter tick) on any lookup error or
    unmocked/unexpected response, so a transient failure can never let an
    invalid fine-grained price through unchecked."""
    code, data = _request("GET", f"/options/contracts/{(contract_symbol or '').upper()}")
    if code == 200 and isinstance(data, dict):
        return {"ok": True, "ppind": bool(data.get("ppind"))}
    return {"ok": False, "ppind": False}


_VALID_INTENTS = {"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"}


def build_order(symbol, qty, side="buy", entry=None, stop=None, target=None,
                order_type=None, tif=None, pitch_id=None, force_single=False,
                intent=None, ppind=None):
    """Construct the Alpaca order payload, choosing bracket vs single. Pure — testable.
    Makes no network calls itself — callers that know a contract's penny-pilot
    status (place_trade, close_position) look it up and pass it in as ppind.

    intent: explicit position_intent — one of buy_to_open / sell_to_open /
    buy_to_close / sell_to_close. Pass this whenever the order is CLOSING a
    position (close_position() always does) so Alpaca's own inference can't
    reject it as a mismatch, and so a close is never silently miscoded as an
    open. When omitted, falls back to the old side-only inference (buy→open,
    sell→open) for options — correct ONLY for a fresh opening trade, which is
    the sole case every existing caller other than close_position() uses.

    ppind: option penny-pilot indicator (True/False), or None if unknown/not
    an option. When set on an option limit order, the entry price is rounded
    to Alpaca's real tick size (see _round_to_tick) before it's ever sent —
    this is the fix for "limit price parameter error" on non-penny-pilot
    names like XLE, where a $0.01-increment price is simply invalid. When
    None (the default — every pre-2026-07-23 caller), behavior is unchanged:
    no rounding, price passed through exactly as given.
    """
    symbol = (symbol or "").upper()
    is_opt = _is_option_symbol(symbol)
    side = (side or "buy").lower()

    if intent is not None and intent not in _VALID_INTENTS:
        return None, f"invalid intent '{intent}' — must be one of {sorted(_VALID_INTENTS)}"

    # Bracket only makes sense for an opening stock trade with both protective legs.
    use_bracket = (not force_single and not is_opt and side == "buy"
                   and stop is not None and target is not None)

    # Limit-only policy (Nico mandate, 2026-07-20): Mason never places a market
    # order, equity or option, entry or exit. No silent fallback — every order
    # rests at an explicit limit price, like a real investor's ticket.
    otype = (order_type or "limit").lower()
    if otype == "market":
        return None, "market orders are disabled by policy — provide a limit price (entry) instead"
    # Bracket parents must rest as GTC; options must be DAY.
    tif = (tif or ("day" if is_opt else ("gtc" if use_bracket else "day"))).lower()
    if is_opt:
        tif = "day"

    payload = {"symbol": symbol, "qty": str(qty), "side": side, "type": otype,
               "time_in_force": tif,
               "client_order_id": _client_order_id(symbol, pitch_id, side, qty,
                                                    entry, stop, target)}
    if otype == "limit":
        if entry is None:
            return None, "limit order needs an entry/limit price"
        entry_val = float(entry)
        if is_opt and ppind is not None:
            underlying = _underlying_from_occ(symbol)
            rounded, tick = _round_to_tick(entry_val, ppind, underlying)
            if abs(rounded - entry_val) > 1e-9:
                payload["_tick_adjustment"] = {
                    "requested": entry_val, "submitted": rounded, "tick": tick,
                    "penny_pilot": bool(ppind) or underlying in _ALWAYS_PENNY_UNDERLYINGS,
                }
            entry_val = rounded
        # .2f enforces Alpaca's separate, unconditional rule that ANY option
        # price with 3+ decimals is rejected outright, pilot status aside.
        payload["limit_price"] = f"{entry_val:.2f}" if is_opt else str(entry)
    # position_intent: an explicit intent (always supplied by close_position(),
    # optionally by any other caller) wins outright — this is what lets Alpaca
    # tell a real close from an open instead of guessing from side alone, which
    # is the bug that made every options sell-to-close get rejected as a
    # mismatched sell_to_open (2026-07-22). Falls back to the old options-only,
    # side-based guess for a plain opening buy/sell with no explicit intent.
    if intent is not None:
        payload["position_intent"] = intent
    elif is_opt:
        payload["position_intent"] = "buy_to_open" if side == "buy" else "sell_to_open"
    if use_bracket:
        payload["order_class"] = "bracket"
        payload["take_profit"] = {"limit_price": str(target)}
        payload["stop_loss"]   = {"stop_price": str(stop)}
    return payload, None


# ── pre-trade checks ─────────────────────────────────────────────────────────────

def pretrade_checks(symbol, qty, side, est_price=None, respect_regime=True):
    """Returns (blocks, warnings). blocks stop the order; warnings ride along."""
    blocks, warnings = [], []

    if side == "buy" and est_price:
        acct = account()
        if acct.get("ok"):
            need = float(est_price) * float(qty)
            if need > acct["buying_power"]:
                blocks.append(f"insufficient buying power: need ~${need:,.2f}, "
                              f"have ${acct['buying_power']:,.2f}")
        else:
            warnings.append("couldn't verify buying power")

    ck = clock()
    if ck.get("ok") and not ck["is_open"]:
        warnings.append(f"market closed — order queues for next open ({ck.get('next_open')})")
    elif not ck.get("ok"):
        warnings.append("couldn't verify market hours")

    if respect_regime and side == "buy":
        try:
            import mason_regime_gate as RG
            g = RG.gate()
            policy = g.get("new_entries")
            if policy in ("no", "adds/hedges-only"):
                warnings.append(f"regime gate says new entries: {policy} "
                                f"({g.get('risk_zone')} top-risk) — small / defensive only")
        except Exception:
            pass
    return blocks, warnings


# ── place ────────────────────────────────────────────────────────────────────────

def place_trade(symbol, qty, side="buy", entry=None, stop=None, target=None,
                setup=None, pitch_id=None, order_type=None, tif=None,
                force_single=False, respect_regime=True, verify=True, intent=None):
    """Submit one order (single or bracket) to Alpaca, verify the fill, audit it.

    intent: optional explicit position_intent passthrough to build_order() —
    see its docstring. Every existing caller opens a position and can leave
    this as None (the old side-only inference still applies); close_position()
    is the one place that always supplies it."""
    _, _, _, enabled = _creds()
    if not enabled:
        return {"ok": False, "error": "paper_trading disabled in mason.config.json"}

    # Options only: look up penny-pilot status so build_order can round the
    # limit price to a real tick before it ever reaches Alpaca (Bug 3 fix,
    # 2026-07-23 — see _round_to_tick). Stocks are untouched by this lookup.
    ppind = None
    if _is_option_symbol(symbol) and (order_type or "limit").lower() == "limit":
        ppind = get_option_contract_meta(symbol).get("ppind", False)

    payload, err = build_order(symbol, qty, side, entry, stop, target,
                               order_type, tif, pitch_id, force_single, intent=intent,
                               ppind=ppind)
    if err:
        return {"ok": False, "error": err}
    price_adjustment = payload.pop("_tick_adjustment", None)

    # (gap fixes, 2026-11-18) `entry` is the price of ONE share. One option contract costs 100 times its quoted
    # price, so the buying-power check gets the price of one contract (before, a $1,400 option order was checked as
    # if it cost $14, and only the broker's own check caught a real shortfall). Stocks are unchanged.
    est = entry
    try:
        if entry and side == "buy" and _is_option_symbol(symbol):
            est = float(entry) * 100.0
    except (TypeError, ValueError):
        est = entry
    blocks, warnings = pretrade_checks(symbol, qty, side, est_price=est, respect_regime=respect_regime)
    if blocks:
        return {"ok": False, "error": "; ".join(blocks), "warnings": warnings, "blocked": True}

    code, data = _request("POST", "/orders", body=payload)
    if code not in (200, 201) or not isinstance(data, dict) or data.get("id") is None:
        msg = data.get("message") if isinstance(data, dict) else data
        rec = {"ok": False, "error": f"order rejected: {msg}", "warnings": warnings,
               "client_order_id": payload.get("client_order_id"),
               "price_adjustment": price_adjustment}
        _audit("submit_failed", payload, rec)
        return rec

    order_id = data.get("id")
    result = {
        "ok": True,
        "order_id": order_id,
        "client_order_id": payload.get("client_order_id"),
        "symbol": payload["symbol"], "side": payload["side"], "qty": payload["qty"],
        "limit_price": payload.get("limit_price"),
        "price_adjustment": price_adjustment,
        "bracket": payload.get("order_class") == "bracket",
        "submitted_status": data.get("status"),
        "submitted_at": data.get("submitted_at"),
        "setup": setup, "pitch_id": pitch_id, "warnings": warnings,
    }
    _audit("submitted", payload, result)

    if verify:
        v = verify_fill(order_id)
        result["fill"] = v
        _audit("verified", payload, {"order_id": order_id, "fill": v})
    return result


# ── verify ───────────────────────────────────────────────────────────────────────

def verify_fill(order_id, timeout_s=10, interval=1.0):
    """Poll until the order reaches a terminal/filled state; record real fill data.

    Tightened window (was 20 s / 2 s): paper fills on liquid names land in <1 s, so
    a shorter ceiling + faster interval cuts the blocking time before Mason's reply
    streams. Orders that don't fill in the window (resting limits, market closed)
    return their current status and are picked up later by reconcile(), which is the
    source of truth — nothing is lost by not waiting the full old 20 s."""
    deadline = time.time() + timeout_s
    last = None
    while True:
        g = get_order(order_id)
        if not g.get("ok"):
            return {"status": "unknown", "error": g.get("error")}
        o = g["order"]
        last = o
        status = (o.get("status") or "").lower()
        if status in _FILLED or status in _DONE or time.time() >= deadline:
            break
        time.sleep(interval)

    o = last or {}
    legs = []
    for leg in (o.get("legs") or []):
        legs.append({"type": leg.get("type"), "status": leg.get("status"),
                     "order_id": leg.get("id"),
                     "filled_avg_price": leg.get("filled_avg_price")})
    return {
        "status": (o.get("status") or "unknown").lower(),
        "filled_qty": float(o.get("filled_qty") or 0),
        "filled_avg_price": (float(o["filled_avg_price"])
                             if o.get("filled_avg_price") else None),
        "legs": legs,
        "checked_at": datetime.now().isoformat(),
    }


# ── audit ledger ─────────────────────────────────────────────────────────────────

def _audit(event, payload, result):
    try:
        log = []
        if EXEC_LEDGER.exists():
            log = json.loads(EXEC_LEDGER.read_text(encoding="utf-8"))
        log.append({"ts": datetime.now().isoformat(), "event": event,
                    "client_order_id": payload.get("client_order_id"),
                    "symbol": payload.get("symbol"),
                    "order_id": (result or {}).get("order_id"),
                    "detail": result})
        if len(log) > 1000:
            log = log[-800:]
        EXEC_LEDGER.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def audit_trail(limit=20):
    try:
        return json.loads(EXEC_LEDGER.read_text(encoding="utf-8"))[-limit:]
    except Exception:
        return []


# ── reconcile (make the journal mirror Alpaca's real fills) ──────────────────────


def fetch_fills(lookback_days=7):
    code, data = _request("GET", "/account/activities/FILL",
                          params={"page_size": 100})
    if code == 200 and isinstance(data, list):
        return data
    return []


def fetch_option_positions():
    """Alpaca's real open option positions, filtered from /positions by symbol shape."""
    pos = positions()
    if not pos.get("ok"):
        return []
    return [p for p in pos["positions"] if _is_option_symbol(p.get("symbol"))]


# ONE lock for everything that books Alpaca fills into the journal (2026-11-15, regular reconcile): reconcile(),
# the self-check's journal repair + protective-stop booking (mason_selfcheck.run) and autotrade's journaling of a
# verified fill (mason_autotrade._journal_stock). Each of them reads the journal, decides, then writes; two at once
# could book the same sell twice or against the wrong row. Re-entrant, so a holder may call another of them.
_BOOKS_LOCK = threading.RLock()


@contextmanager
def books_lock(timeout=120.0):
    """Hold the books lock. Yields True when held, False when `timeout` seconds passed first (0 = do not wait).
    The caller decides what to do without it (skip, or report only)."""
    got = _BOOKS_LOCK.acquire(timeout=timeout) if timeout and timeout > 0 else _BOOKS_LOCK.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            _BOOKS_LOCK.release()


def reconcile(sync_journal=True, wait=120.0, buy_grace_s=0):
    """Pull Alpaca's real positions + fills; optionally sync the trade journal so the
    learning loop scores off broker truth. Returns a summary; never raises.

    With sync_journal it holds the books lock (see books_lock): it waits up to `wait` seconds for another
    reconcile / journal repair to finish (0 = do not wait) and otherwise returns
    {"ok": False, "busy": True, ...} without touching anything. Safe to call repeatedly: an order already in
    the journal is skipped, a held sell is retried, inbox items are de-duplicated (Tests/test_books_fix.py).
    "alpaca_requests" counts the Alpaca calls the pass made (2 + one per order lookup).
    buy_grace_s (review fix, the regular 5-minute pass passes 3600): a BUY fill younger than this does not get an
    'alpaca-sync' row yet (it is retried at the next pass), so the writers that journal a buy with its full details
    (autotrade's verified fill / settle_pending every 30 min, the chat order builder, Nico's own log) go first and the
    reconcile never writes a second row for the same buy. Sells are booked at once. 0 = no wait (boot, chat tool).

    Stock and option fills are handled by two SEPARATE sync paths
    (_sync_fills_to_journal / _sync_option_fills_to_journal) so an option fill can
    never get mis-logged into the equity journal, and options_journal.json is no
    longer left out of reconciliation entirely."""
    out = {"ok": True, "fills_seen": 0, "positions": 0, "journal_synced": 0,
           "options_synced": 0, "options_orphan_fills": [], "options_stale_verified": [],
           "refused_fills": [],          # A9c: [{order, sym, reason}] sells the close guard refused or held back
           "deferred_fills": [],         # books fix: [{order, sym, reason}] sells held because their owner was unknown
           "errors": [], "as_of": datetime.now().isoformat()}
    if not sync_journal:
        return _reconcile_pass(out, sync_journal, buy_grace_s)
    with books_lock(wait) as got:
        if not got:
            out.update(ok=False, busy=True, errors=["another reconcile or journal repair is running"])
            return out
        return _reconcile_pass(out, sync_journal, buy_grace_s)


def _reconcile_pass(out, sync_journal, buy_grace_s=0):
    _req_count.on, _req_count.n = True, 0
    try:
        return _reconcile_body(out, sync_journal, buy_grace_s)
    finally:
        _req_count.on = False
        out["alpaca_requests"] = getattr(_req_count, "n", 0)


def _reconcile_body(out, sync_journal, buy_grace_s=0):
    pos = positions()
    held_equity = None
    if pos.get("ok"):
        out["positions"] = len(pos["positions"])
        held_equity = {p.get("symbol", "").upper() for p in pos["positions"]
                       if not _is_option_symbol(p.get("symbol"))}
    else:
        out["errors"].append(f"positions: {pos.get('error')}")

    fills = fetch_fills()
    out["fills_seen"] = len(fills)

    stock_fills  = [f for f in fills if not _is_option_symbol(f.get("symbol"))]
    option_fills = [f for f in fills if _is_option_symbol(f.get("symbol"))]

    if sync_journal and stock_fills:
        try:
            # held_equity gates buy-side auto-creation: a filled buy for a name
            # Alpaca no longer holds is history, not a live entry (see the
            # function's docstring / the 2026-09-05 phantom-row incident).
            # (the review-fix arguments are passed only by the regular pass, so the call is unchanged otherwise)
            _kw = {"buy_grace_s": buy_grace_s, "waiting": out.setdefault("waiting_fills", [])} if buy_grace_s else {}
            out["journal_synced"] = _sync_fills_to_journal(stock_fills, held_equity, out["refused_fills"],
                                                           out["deferred_fills"], **_kw)
        except Exception as e:
            out["errors"].append(f"journal sync: {e}")
        # Review fix: a sell held for longer than _HELD_ALERT_AFTER_S is put in the inbox too (fill_divergence,
        # one per order), so a hold that never clears cannot stay silent until the fill leaves fetch_fills()'s window.
        _notify_refused_fills(out["refused_fills"] + _stale_deferred(out["deferred_fills"]))

    if sync_journal:
        try:
            opt_result = _sync_option_fills_to_journal(option_fills)
            out["options_synced"]         = opt_result["linked"]
            out["options_orphan_fills"]   = opt_result["orphan_fills"]
            out["options_stale_verified"] = opt_result["stale_verified"]
        except Exception as e:
            out["errors"].append(f"options journal sync: {e}")
        try:                                   # (2026-11-18) one of Mason's own option trades ended but its options
            import mason_option_journal as _OJ  # journal row could not be closed then: tried again here (nothing
            _oj = _OJ.retry_due()               # to do = one small database read, no Alpaca call)
            if _oj:
                out["option_journal_closed"] = [r.get("row") for r in _oj]
        except Exception as e:
            _log.warning("option journal retry skipped: %s", e)

    out["ok"] = not out["errors"]
    return out


# Rows that live in the stock journal but are NOT Alpaca positions (e.g. a share held at another brokerage and
# imported from a CSV) must never be closed by an Alpaca sell fill. 2026-11-19: neutral words only; the explicit,
# word-free mark is a setup_type naming another account ('other-account').
_NON_ALPACA_MARKERS = ("roth", "personal account", "brokerage csv", "other account", "another account",
                       "outside account", "brokerage account", "investing account")
# A piece of a row's text that talks about ANOTHER journal row ("separate from the 1-share NVDA row
# 0a1b2c3d") says nothing about where THIS row is held (books fix 2026-11-15, item B).
_TEXT_PIECES_RE = re.compile(r"[()\[\];\n]|(?<!\d)\.(?!\d)")
_ROW_ID_RE = re.compile(r"\b[0-9a-f]{8}\b")
_OTHER_ROW_RE = re.compile(r"\b(?:separate|another|other)\b")
# Review fix: Alpaca order ids are UUIDs. Only an id of that shape is Alpaca evidence; a free-text id typed into the
# chat log_stock_trade tool (e.g. another broker's order number) must not override another account's note.
_ALPACA_OID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
# Books follow-up 2: a WHOLE Alpaca order id. Only for such an id is a 404 read as "this order does not exist".
_FULL_OID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def non_alpaca_marker(t):
    """The word that makes journal row `t` a row of ANOTHER account (Roth, another brokerage, ...), or None when the row is
    held at Alpaca. The ONE rule every module uses (sell booking, self-check, protect, sizing). Books fix 2026-11-15.

    In this order:
      1. setup_type itself names another account ('other-account', 'brokerage-csv' ...; '-'/'_' read as
         spaces)                                                                 -> that word (not Alpaca)
      2. the row carries Alpaca evidence: an Alpaca-shaped (UUID) entry_order_id, a 'director'
         setup_type, or a director link                                          -> None (Alpaca), whatever the words
         (an 'alpaca-*' setup_type alone is NOT enough: it is also a default label, and the phase-2 A9 tests
         require a row that names another broker's account to stay excluded whatever its setup_type)
      3. the row's own notes/thesis name another account                        -> that word (not Alpaca)
         A sentence or bracket that is about ANOTHER journal row (it says 'row' and names another row id, or
         'separate/another/other ... row') is not read: "separate from the row 0a1b2c3d held elsewhere" does not
         make this row another account's row.
      4. otherwise                                                               -> None (Alpaca)
    Before 2026-11-15 any mention of a marker anywhere in the text excluded the row; that hid Nico's 50-share
    NVDA Alpaca row ca1288a5 from sell booking because its note mentioned the other account's row next to it."""
    setup = re.sub(r"[-_]+", " ", str(t.get("setup_type") or "").strip().lower())
    for m in _NON_ALPACA_MARKERS:
        if m in setup:
            return m
    if _ALPACA_OID_RE.match(str(t.get("entry_order_id") or "").strip()) or setup == "director" \
            or t.get("director_rec_id"):
        return None
    own = str(t.get("id") or "").lower()
    for k in ("notes", "thesis"):
        for piece in _TEXT_PIECES_RE.split(str(t.get(k) or "").lower()):
            if re.search(r"\brows?\b", piece) and (
                    any(x != own for x in _ROW_ID_RE.findall(piece)) or _OTHER_ROW_RE.search(piece)):
                continue                                  # a cross-reference to another row
            for m in _NON_ALPACA_MARKERS:
                if m in piece:
                    return m
    return None


_HELD_ALERT_AFTER_S = 4 * 3600      # review fix: a held sell older than this (by fill time) is put in the inbox


def _stale_deferred(deferred, now=None) -> list:
    """The held sells (reconcile()["deferred_fills"]) whose fill is older than _HELD_ALERT_AFTER_S (or has no
    parsable time), as {order, sym, reason} items for _notify_refused_fills. Pure; never raises."""
    out = []
    try:
        now = now or datetime.now(timezone.utc)
        for d in deferred or []:
            t = _fill_time(d.get("filled_at"))
            if t is None or (now - t).total_seconds() >= _HELD_ALERT_AFTER_S:
                out.append({"order": d.get("order"), "sym": d.get("sym"),
                            "reason": f"held, not booked yet: {d.get('reason')}. It is retried at every reconcile, "
                                      f"but it is lost once it leaves the last 100 fills Alpaca returns"})
    except Exception:
        _log.exception("could not judge the age of held sells")
    return out


def _is_alpaca_row(t) -> bool:
    """True when journal row `t` is held at Alpaca (see non_alpaca_marker)."""
    return non_alpaca_marker(t) is None


is_alpaca_row = _is_alpaca_row


def _fill_time(s):
    """A9c-r2 (RULINGS #47.1 R2-1): an Alpaca fill's transaction_time as a timezone-aware UTC datetime, or None.

    'Z' means +00:00, an offset is honoured and converted, a fraction is padded or truncated to 6 digits (Alpaca
    sends RFC3339Nano, with trailing zeros trimmed), a naive time is read as UTC. Anything missing or unparsable
    gives None; it never raises. Sorting on this, not on the raw string, is what keeps '...43.6Z' before
    '...43.627Z' and '...43Z' before '...43.5Z'."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    if t[-1] in "Zz":
        t = t[:-1] + "+00:00"
    m = re.search(r"\.(\d+)", t)
    if m:
        t = t[:m.start(1)] + m.group(1)[:6].ljust(6, "0") + t[m.end(1):]
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


_T_LAST = datetime.max.replace(tzinfo=timezone.utc)      # sort key for an order with no parsable time
_T_FIRST = datetime.min.replace(tzinfo=timezone.utc)     # (books follow-up 6) an untimed piece is never the "last"


# ── whose shares a sell sold (books fix 2026-11-15, item A) ──────────────────────
# Nico and Mason can hold the same stock in the one paper account (NVDA: Nico's 50-share row, Mason's own 9-share
# director trade). A sell must close the rows of the one it belongs to, not simply the oldest row.

# Client-order-id prefixes of sells of NICO's shares: the hand-approved sell list, mason_protect's protective stops,
# and director "book" sales of Nico's positions.
_NICO_SELL_PREFIXES = ("mason-nico-sell-", _PROTECT_STOP_PREFIX, "mason-book-")
# Mason's own exit orders (mason_agent_loop: pitch 'exit-<rec>-<n>') and director orders ('dir-<rec>').
_MASON_SELL_RE = re.compile(r"^mason-(?:exit|dir)-([0-9a-z]+)-")


class SellOwner:
    """Who a sell belongs to. kind 'mason' (entry_oid / row_id name the Mason trade, when known) or 'nico'.
    mason_rows / mason_oids: the journal row ids and entry order ids of Mason's own stock trades (from agent_trades),
    so the ordering can tell Mason's rows from Nico's. `why` is a short plain reason, for the logs and the notes."""

    __slots__ = ("kind", "entry_oid", "row_id", "mason_rows", "mason_oids", "why")

    def __init__(self, kind, entry_oid=None, row_id=None, mason_rows=(), mason_oids=(), why=""):
        self.kind, self.entry_oid, self.row_id, self.why = kind, entry_oid, row_id, why
        self.mason_rows, self.mason_oids = frozenset(mason_rows), frozenset(mason_oids)

    def __repr__(self):
        return f"SellOwner({self.kind!r}, entry={self.entry_oid!r}, row={self.row_id!r}, why={self.why!r})"


def _is_mason_row(t, owner=None) -> bool:
    """Journal row `t` is one of Mason's own trades: a director row, or linked to Mason's agent_trades record."""
    if t.get("director_rec_id") or str(t.get("setup_type") or "").strip().lower() == "director":
        return True
    if owner is not None:
        if t.get("id") is not None and t.get("id") in owner.mason_rows:
            return True
        if t.get("entry_order_id") and t.get("entry_order_id") in owner.mason_oids:
            return True
    return False


def _alpaca_rows(sym, open_by_ticker, owner=None):
    """THE order in which a sell of `sym` consumes the open Alpaca rows. The pre-flight (_sell_refusal), the
    simulation (_sim_after_sell) and the write loop (_apply_sell_fill) all walk this one list, so they can never
    disagree.

      owner kind 'mason' -> the rows of that Mason trade (same entry order, or the same row), then Mason's other
                            rows, then Nico's rows
      owner 'nico' / None -> Nico's rows, then Mason's rows (only if shares remain)
    Oldest first (entry_date, created_at) inside each group. owner=None is a sell of unknown origin: Nico's first."""
    rows = [t for t in open_by_ticker.get(sym, []) if _is_alpaca_row(t)]

    def key(t):
        mason = _is_mason_row(t, owner)
        if owner is not None and owner.kind == "mason":
            mine = bool((owner.entry_oid and t.get("entry_order_id") == owner.entry_oid)
                        or (owner.row_id and t.get("id") == owner.row_id))
            group = 0 if mine else (1 if mason else 2)
        else:
            group = 1 if mason else 0
        return (group, t.get("entry_date") or "", t.get("created_at") or "")

    rows.sort(key=key)
    return rows


class _SellOwners:
    """Finds who each sell order belongs to, for ONE reconcile run (every Alpaca lookup is cached for the run).

    Alpaca's fill activities carry the order id but not its client_order_id nor its parent order, so:
      1. Mason's own records: agent_trades.exit_order_id, or an order id in its partial exits  -> that Mason trade
      2. GET /orders/{id}: client id 'mason-nico-sell-' / protect stop / 'mason-book-'         -> Nico
                           client id 'mason-exit-<rec>-' / 'mason-dir-<rec>-'                   -> that Mason trade
      3. GET /orders/{entry}?nested=true for each Mason trade open on the symbol: the sell is one of
         the legs (stop / target) of that trade's bracket                                       -> that Mason trade
      4. anything else (a hand-placed sell, a chat 'close it' order ...)                        -> Nico
    Lookups are made only when it matters: when the symbol has open rows of Mason's AND of Nico's (or of two Mason
    trades). FALLBACK: if a lookup fails and nothing above already decided, the answer is None (undecided) when Nico
    has rows on the symbol, and the caller HOLDS the sell for the next reconcile (nothing written, retried); with
    only Mason rows on the symbol it falls back to Mason's rows oldest first. A 404 on a Mason trade's ENTRY order
    is not a failed lookup (books follow-up 2): the sell is not one of that order's legs, and rule 4 applies. Only
    for a whole Alpaca order id (_FULL_OID_RE): a truncated or typed-in id that 404s still counts as a failure."""

    def __init__(self, get=None):
        self._get = get or get_order
        self._orders, self._legs = {}, {}
        self._missing = set()                     # order ids Alpaca answered 404 for (books follow-up 2)
        self._links = None

    def links(self):
        """Mason's own stock trades: [{rec_id, journal_id, entry_oid, exit_oids}] (any status; [] if unreadable)."""
        if self._links is None:
            out = []
            try:
                import mason_db
                conn = mason_db.get_connection()
                try:
                    rows = conn.execute(
                        "SELECT a.rec_id, a.journal_id, a.features_json, a.exit_order_id, t.entry_order_id AS j_oid "
                        "FROM agent_trades a LEFT JOIN trades t ON t.id = a.journal_id "
                        "WHERE a.source='mason' AND a.journal_table='trades' "
                        "AND COALESCE(a.record_deleted,0)=0").fetchall()
                finally:
                    conn.close()
                for r in rows:
                    try:
                        feat = json.loads(r["features_json"] or "{}") or {}
                    except Exception:
                        feat = {}
                    exits = {x.get("order_id") for x in (feat.get("partial_exits") or []) if isinstance(x, dict)}
                    exits.add(r["exit_order_id"])
                    exits.discard(None)
                    out.append({"rec_id": r["rec_id"], "journal_id": r["journal_id"],
                                "entry_oid": r["j_oid"] or feat.get("order_id"), "exit_oids": exits})
            except Exception:
                _log.debug("sell owners: agent_trades not readable; Mason rows known by setup_type only")
            self._links = out
        return self._links

    def blank(self, kind="nico", why=""):
        L = self.links()
        return SellOwner(kind, mason_rows={l["journal_id"] for l in L if l["journal_id"]},
                         mason_oids={l["entry_oid"] for l in L if l["entry_oid"]}, why=why)

    def _owner(self, link, why):
        o = self.blank("mason", why)
        if link is not None:
            o.entry_oid, o.row_id = link["entry_oid"], link["journal_id"]
        return o

    def _order(self, oid):
        if oid not in self._orders:
            try:
                g = self._get(oid)
            except Exception as e:
                g = {"ok": False, "error": f"{type(e).__name__}"}
            self._orders[oid] = g.get("order") if (isinstance(g, dict) and g.get("ok")
                                                    and isinstance(g.get("order"), dict)) else None
            if self._orders[oid] is None and isinstance(g, dict) and g.get("status") == 404 \
                    and _FULL_OID_RE.match(str(oid or "")):
                self._missing.add(oid)            # (a truncated / typed id that 404s stays a failed lookup)
        return self._orders[oid]

    def _leg_ids(self, entry_oid):
        if entry_oid not in self._legs:
            o = self._order(entry_oid)
            if o is None and entry_oid in self._missing:
                # books follow-up 2: Alpaca says this entry order does not exist (404). That is an answer, not a
                # failed lookup: the sell cannot be one of its legs, so the next rule decides (it used to HOLD).
                self._legs[entry_oid] = set()
            elif o is None:
                self._legs[entry_oid] = None
            else:
                ids = set()
                for leg in (o.get("legs") or []):
                    if isinstance(leg, dict):
                        ids.add(leg.get("id"))
                        ids.add(leg.get("replaced_by"))
                ids.discard(None)
                self._legs[entry_oid] = ids
        return self._legs[entry_oid]

    def of(self, oid, sym, open_by_ticker):
        """SellOwner for sell order `oid` on `sym`, or None = undecided (hold the sell; see the class note).
        Never raises: an unexpected error is logged and counts as undecided."""
        try:
            return self._of(oid, sym, open_by_ticker)
        except Exception:
            _log.exception("sell owner lookup for order %s %s failed; the sell is held", oid, sym)
            return None

    def _of(self, oid, sym, open_by_ticker):
        base = self.blank()
        rows = [t for t in open_by_ticker.get(sym, []) if _is_alpaca_row(t)]
        mason = [t for t in rows if _is_mason_row(t, base)]
        nico = [t for t in rows if not _is_mason_row(t, base)]
        trades = {t.get("entry_order_id") or t.get("id") for t in mason}
        if not mason:
            return self.blank("nico", "only Nico's rows are open")
        if not nico and len(trades) == 1:
            return self.blank("mason", "only one Mason trade is open")
        links = self.links()
        for l in links:                                              # 1. Mason's own records
            if oid in l["exit_oids"]:
                return self._owner(l, "Mason's records name it as this trade's exit")
        failed = False
        o = self._order(oid)                                         # 2. the order's own client id
        if o is None:
            failed = True
        else:
            coid = str(o.get("client_order_id") or "")
            if coid.startswith(_NICO_SELL_PREFIXES):
                return self.blank("nico", f"client id {coid[:24]}... is a sale of Nico's shares")
            m = _MASON_SELL_RE.match(coid)
            if m:
                link = next((l for l in links if str(l["rec_id"] or "") == m.group(1)), None)
                return self._owner(link, f"client id {coid[:24]}... is Mason's own exit")
        for t in mason:                                              # 3. a leg of a Mason bracket
            eo = t.get("entry_order_id")
            if not eo:
                continue
            legs = self._leg_ids(eo)
            if legs is None:
                failed = True
            elif oid in legs:
                link = next((l for l in links if l["entry_oid"] == eo), None) or {
                    "entry_oid": eo, "journal_id": t.get("id")}
                return self._owner(link, "a stop/target leg of Mason's bracket order")
        if failed:
            if nico:
                return None
            return self.blank("mason", "lookup failed; only Mason rows are open (oldest first)")
        return self.blank("nico", "not one of Mason's orders (hand-placed or unknown)")


_LEG_GONE = {"canceled", "cancelled", "expired", "rejected"}     # books follow-up 1: a bracket leg that is gone


def chat_close_pitch(symbol, qty):
    """Books follow-up 1 (2026-11-16): the pitch id ('exit-<rec>-chat') for a chat close_paper_position that sells
    exactly Mason's own trade on a stock Nico also holds, else None (the order keeps its plain 'mason-<SYM>-' id).

    Why: the reconcile reads a plain chat close as a sale of Nico's shares (Nico's rows first). That is right while
    Mason's bracket legs rest, because then only Nico's shares are free (and close_position refuses a second sell on
    the symbol anyway). Once those legs are cancelled, a chat close of Mason's 9 NVDA shares was booked against
    Nico's 50. With this pitch the order's client id is 'mason-exit-<rec>-chat-...', which _SellOwners (rule 2) and
    mason_protect already read as Mason's own exit.

    Tagged only when every one of these holds (anything else = None = exactly the old behaviour):
      a stock symbol; a positive size was given; the symbol has open Alpaca rows of Nico's AND of exactly one Mason
      trade; the size equals that Mason trade's open shares and differs from Nico's open shares; the trade's
      director rec id is known; and Alpaca confirms the trade's bracket legs are all cancelled / expired (or it has
      none). A leg still resting (or replaced, or the lookup failing) = None: while a leg rests, the free shares a
      close can sell are Nico's. Reads the journal and makes one Alpaca GET; never raises."""
    try:
        sym = str(symbol or "").strip().upper()
        if not sym or _is_option_symbol(sym) or qty is None or isinstance(qty, bool):
            return None
        try:
            q = float(qty)
        except (TypeError, ValueError):
            return None
        if not q > 0:
            return None
        import mason_trades as MT
        owners = _SellOwners()
        base = owners.blank()
        rows = [t for t in MT.get_open_trades() if (t.get("ticker") or "").upper() == sym and _is_alpaca_row(t)]
        mason = [t for t in rows if _is_mason_row(t, base)]
        nico_q = sum(float(t.get("shares") or 0) for t in rows if not _is_mason_row(t, base))
        trades = {t.get("entry_order_id") or t.get("id") for t in mason}
        if len(trades) != 1 or nico_q <= 1e-9:
            return None
        mason_q = sum(float(t.get("shares") or 0) for t in mason)
        if abs(q - mason_q) > 1e-6 or abs(q - nico_q) <= 1e-6:
            return None
        oid, ids = mason[0].get("entry_order_id"), {t.get("id") for t in mason}
        link = next((l for l in owners.links() if (oid and l["entry_oid"] == oid) or l["journal_id"] in ids), None)
        rec = str((link or {}).get("rec_id") or mason[0].get("director_rec_id") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-z]+", rec) or not oid:
            return None
        g = get_order(oid)
        if not (isinstance(g, dict) and g.get("ok") and isinstance(g.get("order"), dict)):
            return None
        legs = [l for l in (g["order"].get("legs") or []) if isinstance(l, dict)]
        if any(str(l.get("status") or "").lower() not in _LEG_GONE for l in legs):
            return None
        return f"exit-{rec}-chat"
    except Exception:
        _log.exception("chat close pitch for %s %s could not be worked out; the close keeps its plain id",
                       symbol, qty)
        return None


def _sell_walk(rows, qty):
    """The FIFO walk of a sell over `rows` (already filtered and sorted), lazily: yields (row, whole, left, shares)
    for every row the sell touches. `whole` = the row is closed in full (a row of size 0 / unknown counts as whole and
    ends the sell); otherwise it is SPLIT and the walk ends. The arithmetic is the one _apply_sell_fill's write loop
    uses. Lazy on purpose: a row's `shares` is read, and the caller acts on it, before the next row is read."""
    left = float(qty)
    for tr in rows:
        if left <= 1e-9:
            return
        sh = float(tr.get("shares") or 0)
        whole = sh <= 0 or sh <= left + 1e-6
        yield tr, whole, left, sh
        left = max(0.0, left - sh) if (sh > 0 and whole) else 0.0


def _sell_refusal(MT, sym, qty, open_by_ticker, owner=None):
    """Side-effect-free pre-flight of a sell (A9 r2 item 3, factored out in A9c-r2): None if the close guard allows
    every row a sell of `qty` on `sym` would touch, else the FIRST refusal reason (mason_trades.close_refusal).
    Reads only; writes nothing. A row with no id (a simulated remainder, see _sim_after_sell) is never asked about."""
    for tr, _whole, _left, _sh in _sell_walk(_alpaca_rows(sym, open_by_ticker, owner), qty):
        if tr.get("id") is None:
            continue
        _found, why = MT.close_refusal(tr["id"])
        if why:
            return why
    return None


def _sim_after_sell(sym, qty, open_by_ticker, owner=None):
    """A COPY of open_by_ticker as it would stand after a sell of `qty` on `sym`: touched rows leave, a split row is
    replaced by a clean remainder (id None, sorted last among its entry date, as log_trade's new row would be).
    Pure: the argument and its rows are never modified."""
    gone, remainders = set(), []
    for tr, whole, left, sh in _sell_walk(_alpaca_rows(sym, open_by_ticker, owner), qty):
        gone.add(id(tr))
        if not whole:
            r = dict(tr)
            r["id"], r["shares"], r["created_at"] = None, round(sh - left, 6), "9999-12-31T23:59:59"
            remainders.append(r)
    out = dict(open_by_ticker)
    out[sym] = [t for t in open_by_ticker.get(sym, []) if id(t) not in gone] + remainders
    return out


def _group_refusal(MT, sym, members, open_by_ticker):
    """Dry-check a group of same-time sells on one ticker IN ORDER against the state each would really meet (every
    earlier member simulated as applied). members: (order id, qty) or (order id, qty, SellOwner). Returns (order id,
    reason) of the first member refused, else None."""
    sim = open_by_ticker
    for m in members:
        m_oid, m_qty = m[0], m[1]
        m_owner = m[2] if len(m) > 2 else None
        why = _sell_refusal(MT, sym, m_qty, sim, m_owner)
        if why:
            return m_oid, why
        sim = _sim_after_sell(sym, m_qty, sim, m_owner)
    return None


class SellRefused(int):
    """Result of _apply_sell_fill when the close_trade guard REFUSED the fill (A9c, RULINGS #46.2).

    It is falsy and == False, so every existing `if _apply_sell_fill(...)` caller behaves exactly as with the plain
    False it replaces, but unlike the plain False of "nothing changed" it can be told apart
    (`isinstance(r, SellRefused)`), and `.reason` is the guard's message ("trade <id> ...; not changed")."""

    def __new__(cls, reason):
        obj = super().__new__(cls, 0)
        obj.reason = reason
        return obj

    def __repr__(self):
        return f"SellRefused({self.reason!r})"


def _apply_sell_fill(MT, sym, qty, price, oid, open_by_ticker) -> bool:
    """Apply one aggregated Alpaca SELL fill whose owner is not known (signature unchanged for existing callers:
    mason_protect._journal_stop_out, which passes only Nico's lots, and the A9 tests): Nico's rows first, Mason's only
    if shares remain. See _apply_owned_sell_fill."""
    return _apply_owned_sell_fill(MT, sym, qty, price, oid, open_by_ticker, None)


def _apply_owned_sell_fill(MT, sym, qty, price, oid, open_by_ticker, owner) -> bool:
    """Apply one aggregated Alpaca SELL fill to the stock journal (2026-09-29 fix).

    Before: any sell closed the FIRST open row for the ticker in full — a 75-of-100
    QQQ trim closed all 100 shares and booked P&L on 100. Now: rows are taken in the order
    _alpaca_rows gives (books fix 2026-11-15: the rows of the one the sell belongs to first, see
    SellOwner; oldest first inside that); a row the sell fully covers is closed; a row it only partly covers
    is SPLIT — the sold part closes with P&L on exactly the sold shares, and the
    remainder stays open on a new row with the same cost basis / entry date. Every
    closed row records exit_order_id so a re-run never re-applies the same sell.
    Non-Alpaca rows (another account's) are never touched. Returns True if anything changed.
    owner=None (a caller that does not know): Nico's rows first, Mason's only if shares remain.
    (Books fix 2026-11-15: this was _apply_sell_fill; that name keeps its old signature and calls this.)

    Phase-2 A9 round 2 (RULINGS #33.3): BEFORE any write, every row this fill would touch is put to
    mason_trades.close_refusal() (the close_trade guard). If any is refused, the WHOLE fill is skipped: one
    logged error (no traceback), nothing written, open_by_ticker untouched, and returns SellRefused(reason) (falsy,
    == False; A9c: tells a refusal apart from the plain False of "nothing changed"). Without this a
    guarded open row met in the partial branch would get its remainder row and its shares rewrite written and
    then close_trade would raise, leaving a half-applied fill."""
    note = f"Reconciled exit from Alpaca fill (order {oid})."
    changed = False
    # ── A9 r2 item 3: pre-flight. The same walk over the same ordered rows as the loop below, but read-only. ──
    _why = _sell_refusal(MT, sym, qty, open_by_ticker, owner)
    if _why:
        _log.error("sell fill %s %s (order %s) skipped, nothing written: %s", sym, qty, oid, _why)
        return SellRefused(_why)
    for tr, whole, remaining, sh in _sell_walk(_alpaca_rows(sym, open_by_ticker, owner), qty):
        if whole:                                       # whole row sold (or size unknown)
            MT.close_trade(tr["id"], price, "manual", note)
        else:                                           # partial: split the row
            keep = round(sh - remaining, 6)
            nt = MT.log_trade({
                "ticker": sym, "direction": tr.get("direction") or "long",
                "entry_price": tr.get("entry_price"), "stop_loss": tr.get("stop_loss") or 0,
                "target_price": tr.get("target_price") or 0,
                "setup_type": tr.get("setup_type") or "alpaca-sync",
                "time_horizon": tr.get("time_horizon") or "swing",
                "thesis": tr.get("thesis") or "Remainder after a partial Alpaca sell.",
                "mason_confidence": tr.get("mason_confidence") or 0, "shares": keep,
                "entry_order_id": tr.get("entry_order_id"),
                "notes": ((tr.get("notes") or "") + f" [split from {tr['id']}: {keep:g} sh remain after "
                          f"partial sell of {remaining:g} sh, order {oid}]").strip()})
            nt["entry_date"] = tr.get("entry_date") or nt.get("entry_date")
            MT._save_journal([nt])
            cur = MT.get_trade_by_id(tr["id"])
            cur["shares"] = round(remaining, 6)
            MT._save_journal([cur])
            MT.close_trade(tr["id"], price, "manual",
                           note + f" Partial exit: {remaining:g} of {sh:g} sh; remainder on {nt['id']}.")
        done = MT.get_trade_by_id(tr["id"])
        if done is not None:
            done["exit_order_id"] = oid
            MT._save_journal([done])
        changed = True
    open_by_ticker[sym] = [t for t in MT.get_open_trades()
                           if (t.get("ticker") or "").upper() == sym]
    if not open_by_ticker[sym]:
        del open_by_ticker[sym]
    return changed


def _notify_refused_fills(refused):
    """A9c (RULINGS #46.3, #47.1 R2-3): one notifications-inbox item per refused / held / raised order id,
    deduplicated by order id, so the journal/broker divergence stays visible after the order falls out of
    fetch_fills()'s last-100 window. Type 'fill_divergence' (the Alerts bucket), severity 'warn'.
    Never raises (a failing inbox must not break reconcile); a failure is logged with its traceback.

    Dedup: dedup_key 'refused_fill:<order>'. An order that already has a fill_divergence notification of ANY state
    (unread, read or dismissed) is not notified again while that row exists. RETENTION, honestly: alert-bucket rows
    are kept 90 days (mason_notifications.prune_old keep_days), but a DISMISSED row is purged 7 days after its
    deletion (purge_soft_deleted_days). After such a purge, an order still inside fetch_fills()'s last 100 is notified
    again; that is intended, because the divergence is unresolved. (The existence check reads the newest 500
    fill_divergence items; beyond that the inbox's own unread-window dedup still collapses repeats.)"""
    if not refused:
        return
    try:
        import mason_notifications as MN
        have = {n.get("dedup_key") for n in MN.list_notifications(types="fill_divergence", include_deleted=True, limit=500)}
    except Exception:
        _log.exception("refused-fill notifications unavailable; %d refused order(s) not notified", len(refused))
        return
    for r in refused:
        key = f"refused_fill:{r.get('order')}"
        if key in have:
            continue
        try:
            MN.add_notification(
                "fill_divergence", f"Sell fill {r.get('sym')} not journaled (order {str(r.get('order'))[:12]})"[:200],
                body=(f"Alpaca sell order {r.get('order')} on {r.get('sym')} was NOT applied to the journal: "
                      f"{r.get('reason')}. The journal and the broker now disagree until this is resolved by hand."),
                severity="warn", ticker=r.get("sym"), created_by="execution", dedup_key=key)
            have.add(key)
        except Exception:
            _log.exception("could not notify refused fill %s %s", r.get("sym"), r.get("order"))


# Review fix (runtime hardening): Alpaca order states in which an order can still fill more shares. An order whose fill
# activities all say 'partial_fill' is booked only once its status is NOT one of these (filled / cancelled / expired ...).
_STILL_FILLING = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "calculated",
                  "pending_replace", "pending_cancel", "done_for_day", "filled"}


def _deleted_row_order_ids() -> set:
    """Order ids on SOFT-DELETED journal rows (review fix, runtime hardening). Such an order was booked once and a
    person (or a repair) removed the row: reconcile must not write it again (a deleted buy row re-created, or a sell
    whose closed row was deleted booked a second time against the other rows). Never raises."""
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT entry_order_id, exit_order_id FROM trades WHERE is_deleted = 1").fetchall()
        finally:
            conn.close()
        return {x for r in rows for x in (r[0], r[1]) if x}
    except Exception:
        _log.debug("deleted journal rows not readable; their order ids are not counted as booked")
        return set()


def _sync_fills_to_journal(fills, held_symbols=None, refused=None, deferred=None, owners=None, buy_grace_s=0,
                           waiting=None, now=None):
    """Map Alpaca STOCK fills onto mason_trades. A buy for a name we don't already
    journal → log_trade at the REAL entry price; a sell on a name we hold open →
    close_trade at the REAL exit price.

    2026-09-05 rewrite. The old version had two duplication bugs that put ~150
    phantom rows in the journal (see rebuild_alpaca_sync_rows.py):
      1. Alpaca sends ONE fill activity per partial-fill chunk, all sharing the
         order_id. The old loop logged EACH chunk as its own trade row.  → We now
         AGGREGATE fills by order_id first (qty-weighted price, summed qty) and
         act once per order.
      2. `seen` was built only from existing rows' order ids and never updated
         inside the loop, so within one run nothing stopped a re-log, and rows
         written before order-id persistence (commit 84e3d0b) let the WHOLE fill
         history re-log on every reconcile.  → `seen` is now updated the moment
         an order is journaled.

    held_symbols: if given (the set reconcile() gets from /positions), a BUY for
    a symbol Alpaca does NOT currently hold is treated as history, not a live
    entry, and skipped. A filled buy in the order log is not a position.

    Callers pre-filter option symbols (reconcile() does). Idempotent; re-runnable.

    A9c (RULINGS #46.2-46.4): `refused`, if given, is a list that receives {order, sym, reason} for every sell the
    close guard REFUSED and every later sell on the same ticker HELD BACK in this run. Once a sell on `sym` is
    refused, every later sell on `sym` in the run is held back (one ERROR each, nothing written, not counted).
    Fills are applied in PARSED UTC transaction-time order (_fill_time: Z / offsets / short fractions honoured;
    an order with no parsable time sorts after every timed one and logs one WARNING 'has no parsable time') and a
    refused order is never recorded as seen, so the refused fill is re-refused FIRST on every run and the
    quarantine holds across runs with no persisted state.

    A9c-r2 (RULINGS #47.1): sells on one ticker with the SAME parsed time (or all untimed) have no reliable order,
    so before the first of them is applied the whole group is dry-checked in sequence (_group_refusal); if any
    member would be refused, that member is recorded as refused and the whole group is held. A sell that RAISES is
    logged with its traceback (it may have half-applied), also quarantines its ticker and is listed in `refused`
    with reason 'raised mid-apply: <ExceptionTypeName>' (the type only, never the message).

    Books fix 2026-11-15: each sell is booked against the rows of the one it belongs to (_SellOwners / SellOwner:
    a Mason bracket leg or exit closes Mason's row; a sale of Nico's shares or an unknown sell closes Nico's rows
    first). When that cannot be decided because an Alpaca order lookup failed (and Nico has rows on the ticker), the
    sell is HELD: nothing written, not marked seen (so the next reconcile retries it), later sells on the same ticker
    wait too, and it is listed in `deferred` ({order, sym, reason}) if given. `owners` lets tests inject a
    _SellOwners; by default one is made per call (its lookups are cached for the call).

    Review fixes (runtime hardening, reconcile every 5 minutes):
      * an order still FILLING (every fill activity seen says 'partial_fill' / leaves_qty > 0, and Alpaca's order
        status says it can still fill) is not booked yet and not marked seen: booking the part filled so far would
        stamp the order id on the rows and the rest of the order would never be booked. Listed in `waiting`.
      * buy_grace_s: a buy younger than this gets no 'alpaca-sync' row yet (see reconcile()). Listed in `waiting`.
      * order ids on soft-deleted rows count as booked (_deleted_row_order_ids).
    """
    import mason_trades as MT
    owners = owners or _SellOwners()
    try:
        seen = {t.get("entry_order_id") for t in MT.get_all_trades()}
        seen |= {t.get("exit_order_id") for t in MT.get_all_trades()}
    except Exception:
        seen = set()
    seen |= _deleted_row_order_ids()
    seen.discard(None)
    seen.discard("")
    try:
        # A sale by one of mason_protect's protective stops is booked there, as a stop-out of the lot the
        # order covered. Booking it here as well (FIFO over every row of the ticker, reason "manual") would
        # count it twice or against the wrong row.
        import mason_protect as _MP
        seen |= _MP.booked_order_ids()
    except Exception:
        pass
    try:
        # Idle-cash rule (2026-11-16): purchases and sales of the parked index fund are recorded in their own
        # ledger (mason_idle_cash, table park_ledger). They are neither Nico's nor a director trade, so they get
        # no journal row and close none. An empty ledger adds nothing here.
        import mason_idle_cash as _IC
        _park_ids = _IC.order_ids()
    except Exception:
        _park_ids = set()
    if _park_ids is None:
        # (review fix) its ledger exists but could not be read: book nothing this pass rather than book a parked
        # purchase as Nico's or close Nico's / Mason's rows with a parked sale. The next reconcile tries again.
        _log.warning("journal sync skipped this pass: the idle-cash ledger could not be read")
        return 0
    seen |= _park_ids
    held = {s.upper() for s in held_symbols} if held_symbols is not None else None

    open_by_ticker = {}
    try:
        for t in MT.get_open_trades():
            open_by_ticker.setdefault((t.get("ticker") or "").upper(), []).append(t)
    except Exception:
        pass

    # ── aggregate fills into one record per order_id ──────────────────────────
    orders = {}
    for f in fills:
        oid = f.get("order_id")
        sym = (f.get("symbol") or "").upper()
        side = (f.get("side") or "").lower()
        price = float(f.get("price") or 0)
        qty = float(f.get("qty") or 0)
        if not oid or not sym or price <= 0 or qty <= 0:
            continue
        o = orders.setdefault(oid, {"sym": sym, "side": side, "qty": 0.0, "notional": 0.0, "t": None,
                                    "partial": False, "complete": False})
        o["qty"] += qty
        o["notional"] += qty * price
        # review fix: does Alpaca say the order is done? ('fill' / leaves_qty 0 = last chunk; 'partial_fill' = more
        # may come). Activities without these fields count as done, exactly as before.
        _typ = str(f.get("type") or "").lower()
        try:
            _lq = float(f.get("leaves_qty")) if f.get("leaves_qty") not in (None, "") else None
        except (TypeError, ValueError):
            _lq = None
        if _typ == "fill" or (_lq is not None and _lq <= 0) or str(f.get("order_status") or "").lower() == "filled":
            o["complete"] = True
        elif _typ == "partial_fill" or (_lq is not None and _lq > 0):
            o["partial"] = True
        # (the old raw-string "when" min() is gone: it was only the sort key, and it raised TypeError on a JSON-null
        #  transaction_time)
        _ft = _fill_time(f.get("transaction_time"))       # A9c-r2: the order's time = its earliest PARSED chunk time
        if _ft is not None and (o["t"] is None or _ft < o["t"]):
            o["t"] = _ft

    synced = 0
    _now = now or datetime.now(timezone.utc)

    def _wait(oid_, sym_, why_):
        _log.info("fill %s (order %s) not booked yet, retried at the next reconcile: %s", sym_, oid_, why_)
        if waiting is not None:
            _t = orders.get(oid_, {}).get("t")
            waiting.append({"order": oid_, "sym": sym_, "reason": why_,
                            "filled_at": _t.isoformat() if _t is not None else None})

    def _still_filling(oid_):
        """Review fix: True while order `oid_` may still fill more shares (book it once it is done)."""
        o_ = orders[oid_]
        if not o_.get("partial") or o_.get("complete"):
            return False
        look = getattr(owners, "_order", None)
        try:
            od = look(oid_) if look is not None else None
        except Exception:
            od = None
        if not isinstance(od, dict):
            return True                               # cannot tell: wait for the next reconcile
        st = str(od.get("status") or "").lower()
        if st == "filled":                            # done, but this pass has not seen its last chunk yet
            try:
                return float(od.get("filled_qty") or 0) > o_["qty"] + 1e-6
            except (TypeError, ValueError):
                return True
        return st in _STILL_FILLING or not st
    # sym -> (order id, reason, group key|None): a sell on this ticker was refused / raised this run (A9c, A9c-r2)
    quarantined = {}
    held_for_owner = {}                           # sym -> order id: whose sell it was could not be decided (books fix)

    def _hold(sym_, oid_, why_):
        _log.warning("sell fill %s (order %s) held for the next reconcile, nothing written: %s", sym_, oid_, why_)
        if deferred is not None:
            _t = orders.get(oid_, {}).get("t")
            deferred.append({"order": oid_, "sym": sym_, "reason": why_,
                             "filled_at": _t.isoformat() if _t is not None else None})
    # A9c-r2 R2-1: sort on the parsed UTC time; no parsable time sorts last (stable: input order breaks ties)
    ordered = sorted(orders.items(), key=lambda kv: (kv[1]["t"] is None, kv[1]["t"] or _T_LAST))
    for oid, o in ordered:
        if o["t"] is None and oid not in seen:
            _log.warning("fill order %s has no parsable time; sorted after all timed orders", oid)
    groups = {}                                   # (sym, parsed time | None) -> sell order ids, when there are >= 2
    for oid, o in ordered:
        if oid in seen or o["side"] not in ("sell", "sell_to_close") or o["qty"] <= 0:
            continue
        groups.setdefault((o["sym"], o["t"]), []).append(oid)
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    group_done = set()
    for oid, o in ordered:
        if oid in seen:
            continue
        sym, side = o["sym"], o["side"]
        qty = o["qty"]
        price = round(o["notional"] / qty, 4) if qty else 0.0
        if qty <= 0 or price <= 0:
            continue

        if side in ("buy", "buy_to_open"):
            if sym in open_by_ticker:
                seen.add(oid)                     # already have an open row for it
                continue
            if held is not None and sym not in held:
                seen.add(oid)                     # a buy for a name we don't hold = history
                continue
            if buy_grace_s and o["t"] is not None and (_now - o["t"]).total_seconds() < buy_grace_s:
                _wait(oid, sym, f"buy younger than {int(buy_grace_s // 60)} min: its own writer journals it first")
                continue
            if _still_filling(oid):
                _wait(oid, sym, "the order is still filling at Alpaca")
                continue
            try:
                tr = MT.log_trade({
                    "ticker": sym, "direction": "long", "setup_type": "alpaca-sync",
                    "shares": qty, "entry_price": price, "entry_order_id": oid,
                    "thesis": "Reconciled from Alpaca fill.",
                    "notes": f"auto-synced {datetime.now().date()} (order {oid})"})
                open_by_ticker.setdefault(sym, []).append(tr)
                seen.add(oid)
                synced += 1
            except Exception:
                pass
        elif side in ("sell", "sell_to_close") and sym in open_by_ticker:
            if sym in held_for_owner:                 # an earlier sell on this ticker is held: keep the order
                _hold(sym, oid, f"an earlier sell on {sym} (order {held_for_owner[sym]}) is held")
                continue
            if _still_filling(oid):                   # review fix: book the whole sale once it is done
                _wait(oid, sym, "the sell order is still filling at Alpaca")
                continue
            gkey = (sym, o["t"])
            if gkey in groups and gkey not in group_done and sym not in quarantined:
                # A9c-r2 R2-1: same-time sells have no reliable order -> dry-check the whole group first.
                g_owner = {m: owners.of(m, sym, open_by_ticker) for m in groups[gkey]}
                if any(v is None for v in g_owner.values()):
                    held_for_owner[sym] = oid
                    _hold(sym, oid, "could not look up at Alpaca whose shares a same-time sell sold")
                    continue
                group_done.add(gkey)
                try:
                    hit = _group_refusal(MT, sym, [(m, orders[m]["qty"], g_owner[m]) for m in groups[gkey]],
                                         open_by_ticker)
                    g_oid, g_why = hit if hit else (None, None)
                except Exception as e:
                    _log.exception("sell fill %s (order %s): the same-time group pre-check raised; holding %s",
                                   sym, oid, sym)
                    g_oid, g_why = oid, f"raised in pre-check: {type(e).__name__}"
                if g_oid is not None:
                    _log.error("sell fill %s %s (order %s) skipped, nothing written: %s",
                               sym, orders[g_oid]["qty"], g_oid, g_why)
                    quarantined[sym] = (g_oid, g_why, gkey)
                    if refused is not None:
                        refused.append({"order": g_oid, "sym": sym, "reason": g_why})
                    seen.add(g_oid)
                    if g_oid == oid:
                        continue
            if sym in quarantined:                # A9c: an earlier / same-time sell on this ticker did not apply
                q_oid, q_why, q_gkey = quarantined[sym]
                lead = (f"a same-time sell on {sym}" if q_gkey == gkey else f"an earlier sell on {sym}")
                why = f"held back: {lead} (order {q_oid}) was not applied ({q_why})"
                _log.error("sell fill %s %s (order %s) %s; nothing written", sym, qty, oid, why)
                if refused is not None:
                    refused.append({"order": oid, "sym": sym, "reason": why})
                seen.add(oid)
                continue
            owner = owners.of(oid, sym, open_by_ticker)
            if owner is None:
                held_for_owner[sym] = oid
                _hold(sym, oid, "could not look up at Alpaca whose shares this sell sold")
                continue
            try:
                if any(_is_mason_row(t, owner) for t in open_by_ticker.get(sym, []) if _is_alpaca_row(t)):
                    res = _apply_owned_sell_fill(MT, sym, qty, price, oid, open_by_ticker, owner)
                else:                             # no Mason row on the ticker: plain oldest-first, exactly as before
                    res = _apply_sell_fill(MT, sym, qty, price, oid, open_by_ticker)
                if res:
                    synced += 1
                elif isinstance(res, SellRefused):
                    quarantined[sym] = (oid, res.reason, None)
                    if refused is not None:
                        refused.append({"order": oid, "sym": sym, "reason": res.reason})
                seen.add(oid)
            except Exception as e:
                # A9c N1 (never silent) + A9c-r2 R2-2: the sell may have been HALF-applied (a row guarded between
                # the pre-flight and the write, QA A4). The ticker is quarantined for the rest of the run and the
                # order is listed in `refused` with the exception TYPE only (never str(e): no row content may
                # reach the inbox or the boot JSON). With nothing applied the order stays unseen, so a transient
                # error costs only a delay: the next reconcile retries it. After a MULTI-row half-apply the
                # earlier rows were already stamped exit_order_id=<this order>, so the order counts as seen on
                # the next run and its remainder is not re-examined; the fill_divergence item (90 days) is the
                # durable trace (RULINGS #47.4: recorded known limit, post-phase-2 backlog).
                _log.exception("sell fill %s %s (order %s) raised mid-apply; the journal may be half-applied",
                               sym, qty, oid)
                why = f"raised mid-apply: {type(e).__name__}"
                quarantined[sym] = (oid, why, None)
                if refused is not None:
                    refused.append({"order": oid, "sym": sym, "reason": why})
        else:
            seen.add(oid)                         # sell with nothing open / short side — leave it
    return synced


def _sync_option_fills_to_journal(option_fills):
    """Reconcile Alpaca's REAL option fills against options_journal.json.

    This is the check that closes the log/broker drift bug: for every real Alpaca
    option fill, either
      (a) it matches an existing journal entry by order_id/client_order_id —
          backfill attach_execution() if that entry wasn't already verified, or
      (b) no journal entry references this order_id at all — that's an ORPHAN FILL:
          Alpaca actually filled an option order that never made it into Mason's
          journal. Flagged, not auto-created, because we don't have the strategy/
          strikes/thesis metadata to write a meaningful play record — Nico/Mason
          need to backfill that by hand using log_options_play with the real fill
          data attached.

    Also flags entries marked execution_verified=True whose order_id no longer
    shows up in Alpaca's recent fills window — a "stale_verified" entry worth a
    second look (could just be outside the lookback window, so this is advisory).
    """
    import mason_options as MO

    result = {"linked": 0, "orphan_fills": [], "stale_verified": []}

    # Books follow-up 6 (2026-11-16): Alpaca sends ONE fill activity per piece of a fill. The pieces are gathered
    # per order first (summed quantity, quantity-weighted price, the last piece's time), so a play is verified with
    # the WHOLE fill, and only once the order cannot fill any more (_option_fill_total). Before, the first piece alone
    # was attached and, the play then being verified, the rest never was.
    seen_order_ids = set()
    by_order = {}
    for f in option_fills:
        oid = f.get("order_id")
        sym = (f.get("symbol") or "").upper()
        price = float(f.get("price") or 0)
        if not oid or not sym or price <= 0:
            continue
        seen_order_ids.add(oid)
        by_order.setdefault(oid, []).append(f)

    # (gap fixes, 2026-11-18) A sale that is already written on a journal row as that row's EXIT (exit order id, or one
    # of the pieces Mason's own exit recorded) is booked, not an orphan. Before, every option sale was listed as an
    # "orphan fill" because only ENTRY order ids were looked up.
    known_exits = set()
    try:
        for p in MO.get_all_plays():
            if p.get("exit_order_id"):
                known_exits.add(str(p["exit_order_id"]))
            mx = p.get("mason_exit")
            for piece in (mx.get("pieces") or []) if isinstance(mx, dict) else []:
                if isinstance(piece, dict) and piece.get("order_id"):
                    known_exits.add(str(piece["order_id"]))
    except Exception:
        known_exits = set()

    for oid, pieces in by_order.items():
        if str(oid) in known_exits and all((f.get("side") or "").lower() == "sell" for f in pieces):
            continue
        play = MO.find_play_by_order_id(oid)
        if play:
            if not play.get("execution_verified"):
                total = _option_fill_total(oid, pieces)
                if total is None:
                    _log.info("option order %s is still filling (or its state is unknown); attached at the next "
                              "reconcile", oid)
                    continue
                qty, price, at = total
                MO.attach_execution(play["id"], order_id=oid, client_order_id=oid,
                                    contract_symbol=(pieces[0].get("symbol") or "").upper(), filled_qty=qty,
                                    filled_avg_price=price,
                                    filled_at=at)
                result["linked"] += 1
            continue

        # No journal entry references this order at all — real money(paper) moved,
        # nothing recorded it. (Listed piece by piece, as before.)
        for f in pieces:
            result["orphan_fills"].append({
                "order_id": oid, "symbol": (f.get("symbol") or "").upper(), "side": (f.get("side") or "").lower(),
                "price": float(f.get("price") or 0), "qty": float(f.get("qty") or 0),
                "transaction_time": f.get("transaction_time"),
            })

    # Advisory pass: verified journal entries whose order_id wasn't seen in this
    # fills window at all (could be legit — just older than lookback).
    for p in MO.get_all_plays():
        if p.get("execution_verified") and p.get("order_id") and p["order_id"] not in seen_order_ids:
            result["stale_verified"].append({"play_id": p["id"], "ticker": p.get("ticker"),
                                              "order_id": p["order_id"]})

    return result


def _option_fill_total(oid, pieces):
    """Books follow-up 6: (filled qty, average price, time of the last piece) of option order `oid` from its fill
    activities `pieces`, or None while the order may still fill more (or its state cannot be read).

    Done when a piece says so ('fill' / leaves_qty 0 / order_status filled) or carries no piece fields at all (old
    activities: counted as whole, exactly as before). When every piece says 'partial_fill', Alpaca's order is asked
    once: filled / cancelled / expired -> the order's own filled_qty and average price (the broker's total);
    anything else, or no answer -> None (retried at the next reconcile)."""
    qty = sum(float(f.get("qty") or 0) for f in pieces)
    notional = sum(float(f.get("qty") or 0) * float(f.get("price") or 0) for f in pieces)
    price = round(notional / qty, 6) if qty > 0 else float(pieces[0].get("price") or 0)
    last = max(pieces, key=lambda f: (_fill_time(f.get("transaction_time")) or _T_FIRST))
    at = last.get("transaction_time")
    complete = partial = False
    for f in pieces:
        typ = str(f.get("type") or "").lower()
        try:
            lq = float(f.get("leaves_qty")) if f.get("leaves_qty") not in (None, "") else None
        except (TypeError, ValueError):
            lq = None
        if typ == "fill" or (lq is not None and lq <= 0) or str(f.get("order_status") or "").lower() == "filled":
            complete = True
        elif typ == "partial_fill" or (lq is not None and lq > 0):
            partial = True
    # Review fix (BOOKS2_REVIEW F3): fetch_fills reads only the newest 100 activities, so the earlier pieces of an
    # order can be missing from `pieces`. A piece whose cum_qty (the order's running total) is above the pieces' sum
    # says so: the order's own totals are read then; if that read fails, the pieces seen are used (as before).
    cum = 0.0
    for f in pieces:
        try:
            cum = max(cum, float(f.get("cum_qty") or 0))
        except (TypeError, ValueError):
            pass
    done = complete or not partial
    if done and cum <= qty + 1e-6:
        return qty, price, at
    g = get_order(oid)
    od = g.get("order") if isinstance(g, dict) and g.get("ok") else None
    if not isinstance(od, dict):
        return (qty, price, at) if done else None
    st = str(od.get("status") or "").lower()
    if not st or (st != "filled" and st in _STILL_FILLING):
        return None
    try:
        fq, fp = float(od.get("filled_qty") or 0), float(od.get("filled_avg_price") or 0)
    except (TypeError, ValueError):
        fq = fp = 0.0
    if fq > 0 and fp > 0:
        return fq, fp, od.get("filled_at") or at
    return qty, price, at


def place_option_play(ticker, strategy, direction, expiration, strikes,
                      contract_symbol, qty, side="buy", entry=None,
                      premium_type="debit", thesis="", mason_confidence=None,
                      max_profit=None, max_loss=None, breakeven=None,
                      pitch_id=None, respect_regime=True, notes="", today=None):
    """
    THE single verified path for an options play that Nico wants both placed AND
    journaled. Order created → submitted to Alpaca → polled to a confirmed fill →
    ONLY THEN is a journal entry written, pre-populated with the REAL fill price/
    qty/order_id (never the estimated numbers Mason quoted beforehand).

    If the order doesn't fill (rejected, blocked, still resting/pending), NOTHING
    is written to options_journal.json — this is the guarantee that stops the
    journal from recording plays that were never actually executed.

    today: the date the journal's "has this contract expired" check counts from (2026-11-17). None = the
    system clock's date, exactly as before; mason_autotrade passes Mason's own date (_now_et).
    """
    exec_result = place_trade(symbol=contract_symbol, qty=qty, side=side, entry=entry,
                              pitch_id=pitch_id, respect_regime=respect_regime, verify=True)

    if not exec_result.get("ok"):
        return {"ok": False, "stage": "order", "execution": exec_result,
               "journal": None,
               "error": exec_result.get("error", "order failed"),
               "note": "Nothing was journaled — no confirmed fill."}

    fill = exec_result.get("fill") or {}
    fill_status = (fill.get("status") or "").lower()
    if fill_status not in _FILLED:
        return {"ok": False, "stage": "fill", "execution": exec_result,
               "journal": None,
               "error": f"order not filled (status={fill_status or 'unknown'})",
               "note": "Order was submitted but never confirmed filled — nothing "
                       "was journaled. Check get_paper_orders / reconcile_alpaca "
                       "later; if it fills, run reconcile to backfill the journal."}

    import mason_options as MO
    play_data = {
        "ticker": ticker, "strategy": strategy, "direction": direction,
        "expiration": expiration, "strikes": strikes,
        "premium": fill.get("filled_avg_price"), "premium_type": premium_type,
        "contracts": int(fill.get("filled_qty") or qty),
        "underlying_price_at_entry": None, "iv_at_entry": None,
        "thesis": thesis, "mason_confidence": mason_confidence,
        "max_profit": max_profit, "max_loss": max_loss, "breakeven": breakeven,
        "notes": notes,
    }
    # A REAL FILL MUST NEVER BE LOST TO A JOURNAL REFUSAL.
    #
    # log_play() gained integrity guards on 2026-07-30 and can now raise. On
    # every other path a refusal is exactly right — nothing was written, nothing
    # was lost. Here it is not: the order is already FILLED. Refusing the write
    # would leave a real position with no journal row, which is the orphan-fill
    # problem those very guards exist to clean up.
    #
    # In practice only guard 2 can fire here (an order-id-shaped UUID in the
    # caller's notes/thesis), and only guard 1 could if a contract somehow filled
    # after expiry. Either way the correct response is to quarantine the offending
    # text, not to drop the fill. The row is attached to the real order two lines
    # below, so it ends up execution_verified regardless.
    try:
        play = MO.log_play(play_data, today=today)      # `today`: Mason's own date for the journal's expiry check
                                                        # (None = the system date, exactly as before)
    except MO.JournalIntegrityError as e:
        # Scrub, then retry ONCE. The scrub must remove the very thing the guard
        # objected to — otherwise the retry re-raises. UUIDs are redacted in
        # place so the surrounding prose survives and stays readable.
        def _scrub(v):
            return MO._UUID_RE.sub("[order-id redacted — see the row's order_id]",
                                   str(v or ""))
        play_data["notes"] = (
            f"[JOURNAL GUARD FIRED ON A REAL FILL — text scrubbed, the fill was "
            f"kept. The real order id lives on this row's order_id field, where "
            f"code can actually read it.]\n"
            f"guard said: {_scrub(e)}\n"
            f"original notes: {_scrub(play_data.get('notes'))}"
        )
        play_data["thesis"] = _scrub(play_data.get("thesis"))
        play = MO.log_play(play_data, today=today)
    play = MO.attach_execution(play["id"], order_id=exec_result.get("order_id"),
                               client_order_id=exec_result.get("client_order_id"),
                               contract_symbol=contract_symbol,
                               filled_qty=fill.get("filled_qty"),
                               filled_avg_price=fill.get("filled_avg_price"),
                               filled_at=fill.get("checked_at"))

    return {"ok": True, "execution": exec_result, "journal": play}


# ── close / liquidate (position-aware — never trust a caller-supplied qty) ───────
# Built 2026-07-22 after Mason's sell-to-close path oversold IONQ/RGTI into an
# accidental net-short position (repeated close attempts across sessions each
# resubmitted the full original qty with no idea what was actually still held,
# and options closes were separately rejected outright — see build_order()'s
# intent handling above). Both new entry points below always read Alpaca's
# LIVE position first and derive side/qty/intent from that, never from a
# caller's guess.

def get_position(symbol):
    """Live lookup of ONE position — source of truth for any close. A 404 means
    flat (not a transport error). qty_available excludes shares/contracts
    already committed to a resting order, so calling this again while an
    earlier close is still open correctly reports less (or zero) available
    instead of the stale full size."""
    symbol = (symbol or "").upper()
    code, data = _request("GET", f"/positions/{symbol}")
    if code == 404:
        return {"ok": True, "exists": False, "qty": 0.0, "qty_available": 0.0, "side": None}
    if code == 200 and isinstance(data, dict):
        return {
            "ok": True, "exists": True,
            "qty": abs(float(data.get("qty", 0) or 0)),
            "qty_available": abs(float(data.get("qty_available", data.get("qty", 0)) or 0)),
            "side": (data.get("side") or "long").lower(),
            "avg_entry_price": float(data.get("avg_entry_price", 0) or 0),
            "asset_class": data.get("asset_class"),
        }
    return {"ok": False, "error": data}


def _open_orders(symbol, side=None):
    """Open (unfilled/resting) orders on a symbol, optionally filtered to one side."""
    code, data = _request("GET", "/orders", params={"status": "open", "symbols": symbol})
    if code == 200 and isinstance(data, list):
        return [o for o in data if not side or (o.get("side") or "").lower() == side]
    return []


def settlement_disposition(symbol):
    """How did this option position actually LEAVE the book, per Alpaca?

    The broker gate in record_expiry_review only asks "is it still live?" — a
    flat position passes. But "flat" hides HOW it went flat, and that is exactly
    what got MSFT 5c29d5a8 (sold early 2026-07-30 @ $50) and AMD 8cf33e40
    (expired worthless 2026-08-21, OPEXP net $0) both written as held-to-expiry
    WINS on 2026-09-22. This reads the real disposition so the review can be
    forced to match it.

    Returns {ok, kind, sell_fills, opexp, error} where kind is one of:
      'sold_early'        — a filled SELL exists → this is a closed_early, not an
                            expiry review; sell_fills carries the real exit fills.
      'expired_worthless' — an OPEXP activity with net_amount 0 → full-premium loss.
      'expired_itm'       — an OPEXP activity with non-zero net → settled in-the-money.
      'unknown'           — Alpaca ANSWERED both queries but shows no sell and no
                            OPEXP. Callers treat this as "do not block".
      'unavailable'       — Alpaca did NOT answer (429 rate limit, 5xx, transport
                            error) even after retries. Returned with ok=False so
                            callers can tell "broker says nothing" apart from
                            "broker never replied" (2026-09-28: a 429 used to come
                            back as ok=True/'unknown' and silently skip the guard).
    """
    symbol = (symbol or "").upper()

    def _get_with_retry(path, params):
        # Retry the transient failures (rate limit, 5xx, transport) with a short
        # backoff, honoring Retry-After when Alpaca sends it. Anything else
        # (200, 4xx other than 429) returns immediately.
        code, data = None, None
        for attempt in range(3):
            code, data = _request("GET", path, params=params)
            if code == 200:
                return code, data
            if code is not None and code != 429 and code < 500:
                return code, data
            if attempt < 2:
                delay = 2 ** attempt
                if code == 429 and isinstance(data, dict):
                    try:
                        delay = max(delay, float(data.get("retry_after") or 0))
                    except (TypeError, ValueError):
                        pass
                time.sleep(min(delay, 8))
        return code, data

    try:
        sell_fills = []
        code, orders = _get_with_retry("/orders",
                                       {"status": "all", "symbols": symbol,
                                        "limit": 200, "nested": "true"})
        if code != 200 or not isinstance(orders, list):
            return {"ok": False, "kind": "unavailable", "sell_fills": [], "opexp": None,
                    "error": f"Alpaca /orders did not answer (HTTP {code}): {str(orders)[:200]}"}
        for o in orders:
            if (o.get("side") or "").lower() != "sell":
                continue
            fq = float(o.get("filled_qty") or 0)
            if o.get("status") == "filled" or fq > 0:
                sell_fills.append({
                    "qty": fq,
                    "price": float(o.get("filled_avg_price") or 0),
                    "filled_at": o.get("filled_at"),
                    "order_id": o.get("id"),
                    "client_order_id": o.get("client_order_id"),
                })

        opexp = None
        code2, acts = _get_with_retry("/account/activities/OPEXP", {"page_size": 100})
        if code2 != 200 or not isinstance(acts, list):
            return {"ok": False, "kind": "unavailable", "sell_fills": [], "opexp": None,
                    "error": f"Alpaca OPEXP activities did not answer (HTTP {code2}): {str(acts)[:200]}"}
        for a in acts:
            if (a.get("symbol") or "").upper() == symbol:
                opexp = a
                break

        if sell_fills:
            kind = "sold_early"
        elif opexp is not None:
            net = float(opexp.get("net_amount") or 0)
            kind = "expired_worthless" if net == 0 else "expired_itm"
        else:
            kind = "unknown"
        return {"ok": True, "kind": kind, "sell_fills": sell_fills,
                "opexp": opexp, "error": None}
    except Exception as e:
        return {"ok": False, "kind": "unavailable", "sell_fills": [],
                "opexp": None, "error": str(e)}


def close_position(symbol, qty=None, entry=None, tif=None, pitch_id=None, verify=True):
    """THE safe, position-aware close — for both stock and option positions,
    long or short. Reads Alpaca's LIVE position for `symbol` right now and
    derives side + qty + position_intent from what is ACTUALLY held, instead
    of trusting a caller-supplied qty/side the way place_trade() does. Two
    independent guards close the IONQ/RGTI failure mode:
      1. qty is always clamped to qty_available, so a retry after the first
         attempt already filled sees the reduced (or zero) real size instead
         of blindly reselling the original qty.
      2. a second call while a close order from the first attempt is still
         resting on the book is refused outright, not queued as a duplicate.
    If neither guard fires there is nothing to close and the call is a no-op
    that costs nothing — this can safely be called speculatively.

    qty: optional PARTIAL close size. Omit to close the full position.
    Still LIMIT-ONLY per policy (pass a real quote-based entry price). For an
    immediate, no-price-control flatten instead, use liquidate_position().
    """
    symbol = (symbol or "").upper()
    _, _, _, enabled = _creds()
    if not enabled:
        return {"ok": False, "error": "paper_trading disabled in mason.config.json"}

    pos = get_position(symbol)
    if not pos.get("ok"):
        return {"ok": False, "error": f"couldn't read live position for {symbol}: {pos.get('error')}"}
    if not pos["exists"] or pos["qty_available"] <= 0:
        if pos["exists"]:
            # every share is committed: say so plainly when it is Mason's own protective stop holding them
            held = [o.get("id") for o in _open_orders(symbol)
                    if str(o.get("client_order_id") or "").startswith(_PROTECT_STOP_PREFIX)]
            if held:
                return {"ok": False, "held_by_protective_stop": held,
                        "error": f"no {symbol} shares are free to sell: Mason's protective stop order "
                                 f"({', '.join(str(h) for h in held)}) is holding them. Cancel that order first "
                                 f"(cancel_paper_order) if these shares should be sold now; Mason asks before "
                                 f"putting the stop back."}
        return {"ok": False, "error": f"no open position in {symbol} (or it's already fully "
                                       f"committed to a resting order) — nothing to close.",
                "already_flat": True}

    close_qty = pos["qty_available"] if qty is None else min(float(qty), pos["qty_available"])
    if close_qty <= 0:
        return {"ok": False, "error": f"requested qty resolves to 0 (qty_available={pos['qty_available']})"}

    is_long = pos["side"] == "long"
    side    = "sell" if is_long else "buy"
    intent  = "sell_to_close" if is_long else "buy_to_close"

    # A protective stop of mason_protect covers a DIFFERENT quantity (shares Nico entered) and holds it at
    # the broker, so it is not a duplicate of this close; qty_available above already leaves its shares out.
    existing = [o for o in _open_orders(symbol, side)
                if not str(o.get("client_order_id") or "").startswith(_PROTECT_STOP_PREFIX)]
    if existing:
        o = existing[0]
        return {"ok": False,
                "error": f"a {side} order for {symbol} is already open (order {o.get('id')}, "
                         f"qty {o.get('qty')}) — not submitting a duplicate close. Cancel it first "
                         f"with cancel_paper_order if you want to re-price.",
                "existing_order_id": o.get("id")}

    # Options only: same penny-pilot tick lookup as place_trade (Bug 3 fix,
    # 2026-07-23) — a close order's limit price is just as subject to Alpaca's
    # tick-size rejection as an opening order's.
    ppind = None
    if _is_option_symbol(symbol):
        ppind = get_option_contract_meta(symbol).get("ppind", False)

    payload, err = build_order(symbol, close_qty, side, entry=entry, order_type="limit",
                               tif=tif, pitch_id=pitch_id, force_single=True, intent=intent,
                               ppind=ppind)
    if err:
        return {"ok": False, "error": err}
    price_adjustment = payload.pop("_tick_adjustment", None)

    blocks, warnings = pretrade_checks(symbol, close_qty, side, est_price=entry, respect_regime=False)
    if blocks:
        return {"ok": False, "error": "; ".join(blocks), "warnings": warnings, "blocked": True}

    code, data = _request("POST", "/orders", body=payload)
    if code not in (200, 201) or not isinstance(data, dict) or data.get("id") is None:
        msg = data.get("message") if isinstance(data, dict) else data
        rec = {"ok": False, "error": f"close order rejected: {msg}", "warnings": warnings,
               "client_order_id": payload.get("client_order_id"),
               "price_adjustment": price_adjustment}
        _audit("close_submit_failed", payload, rec)
        return rec

    order_id = data.get("id")
    result = {
        "ok": True, "order_id": order_id,
        "client_order_id": payload.get("client_order_id"),
        "symbol": symbol, "side": side, "qty": close_qty, "position_intent": intent,
        "limit_price": payload.get("limit_price"),
        "price_adjustment": price_adjustment,
        "position_qty_before": pos["qty"],
        "submitted_status": data.get("status"), "submitted_at": data.get("submitted_at"),
        "warnings": warnings,
    }
    _audit("close_submitted", payload, result)
    if verify:
        v = verify_fill(order_id)
        result["fill"] = v
        _audit("close_verified", payload, {"order_id": order_id, "fill": v})
    return result


def liquidate_position(symbol, qty=None, percentage=None, confirm=False):
    """EMERGENCY flatten via Alpaca's native close-position endpoint
    (DELETE /positions/{symbol}) — fills immediately against the book with no
    limit-price control, so this deliberately bypasses the limit-only policy
    that governs every other order in this module. Use it to correct an
    unwanted position (this is how the IONQ/RGTI accidental net-short,
    2026-07-22, should have been fixed) or when Nico explicitly says
    liquidate/flatten/flush now. Never use this as a routine, price-conscious
    exit — that's close_position().

    qty and percentage are mutually exclusive (Alpaca rejects both together).
    Omit both to close the entire position. Requires confirm=True — this is
    the one function in this module that can move size at an uncontrolled price.
    """
    if not confirm:
        return {"ok": False, "error": "confirm=True required — this submits an immediate, "
                                       "price-uncontrolled close, bypassing the limit-only policy."}
    symbol = (symbol or "").upper()
    _, _, _, enabled = _creds()
    if not enabled:
        return {"ok": False, "error": "paper_trading disabled in mason.config.json"}
    if qty is not None and percentage is not None:
        return {"ok": False, "error": "qty and percentage are mutually exclusive — pass one or neither."}

    params = {}
    if qty is not None:
        params["qty"] = str(qty)
    if percentage is not None:
        params["percentage"] = str(percentage)

    code, data = _request("DELETE", f"/positions/{symbol}", params=params)
    if code == 404:
        return {"ok": False, "error": f"no open position in {symbol} — nothing to liquidate.",
                "already_flat": True}
    ok = code in (200, 207)
    result = {"ok": ok, "symbol": symbol, "status_code": code}
    if not ok:
        result["error"] = (data.get("message") if isinstance(data, dict) else data) or "liquidate failed"
        _audit("liquidate_failed", {"symbol": symbol}, result)
        return result

    if isinstance(data, dict) and data.get("id"):
        result["order_id"] = data["id"]
        result["fill"] = verify_fill(data["id"])
    _audit("liquidate_submitted", {"symbol": symbol}, result)
    return result


# ── self-test (offline; monkeypatches _request, never hits Alpaca or journals) ───

def _self_test():
    global _request, EXEC_LEDGER

    # 1) build_order: stock w/ stop+target → bracket; option → single + intent
    p, err = build_order("AAPL", 2, "buy", entry=190, stop=180, target=210, pitch_id="x1")
    assert err is None and p["order_class"] == "bracket"
    assert p["take_profit"]["limit_price"] == "210" and p["stop_loss"]["stop_price"] == "180"
    assert p["client_order_id"].startswith("mason-x1-")
    p2, _ = build_order("AAPL", 5, "buy", entry=190)               # no stop/target → single
    assert "order_class" not in p2 and p2["type"] == "limit"
    p3, err3 = build_order("RKLB250731C00105000", 1, "buy")        # option, no price → rejected (limit-only policy)
    assert p3 is None and "limit" in err3
    p3b, _ = build_order("RKLB250731C00105000", 1, "buy", entry=4.50)  # option w/ limit → single, day, intent
    assert ("order_class" not in p3b and p3b["type"] == "limit"
            and p3b["time_in_force"] == "day" and p3b["position_intent"] == "buy_to_open")
    p4, err4 = build_order("AAPL", 5, "buy", entry=190, order_type="market")  # explicit market → rejected
    assert p4 is None and "market" in err4

    # 1c) explicit intent overrides the side-only guess — this is the options
    # sell-to-close fix: without it every sell built "sell_to_open" regardless
    # of context, which Alpaca rejects outright against a real long position.
    p5, _ = build_order("RKLB250731C00105000", 1, "sell", entry=5.00, intent="sell_to_close")
    assert p5["position_intent"] == "sell_to_close"
    p6, _ = build_order("AAPL", 10, "sell", entry=190, intent="buy_to_close")  # stock gets it too now
    assert p6["position_intent"] == "buy_to_close"
    p7, err7 = build_order("AAPL", 10, "sell", entry=190, intent="not_a_real_intent")
    assert p7 is None and "invalid intent" in err7

    # 1d) tick-size rounding (Bug 3 fix, 2026-07-23) — pure, no network. This is
    # the actual XLE case: $0.31/$0.32 are $0.01-increment prices, invalid on a
    # non-penny-pilot name where the real tick is $0.05 under $3.
    assert _option_tick_size(0.31, False) == 0.05      # non-penny, under $3
    assert _option_tick_size(0.31, True)  == 0.01      # penny-pilot, under $3
    assert _option_tick_size(5.00, False) == 0.10      # non-penny, at/above $3
    assert _option_tick_size(5.00, True)  == 0.05      # penny-pilot, at/above $3
    assert _option_tick_size(0.31, False, "SPY") == 0.01   # SPY always penny, ppind aside
    r, t = _round_to_tick(0.31, False); assert r == 0.30 and t == 0.05, (r, t)
    r, t = _round_to_tick(0.32, False); assert r == 0.30 and t == 0.05, (r, t)
    r, t = _round_to_tick(0.33, False); assert r == 0.35 and t == 0.05, (r, t)
    r, t = _round_to_tick(0.30, False); assert r == 0.30 and t == 0.05, (r, t)  # already on-tick

    # build_order rounds an option's limit price when ppind is supplied and
    # reports the adjustment; an already-valid price gets no adjustment marker;
    # ppind=None (every pre-fix caller, and every other test above) is untouched.
    p8, _ = build_order("XLE260724C00060500", 1, "buy", entry=0.31, ppind=False)
    assert p8["limit_price"] == "0.30", p8
    assert p8["_tick_adjustment"]["requested"] == 0.31 and p8["_tick_adjustment"]["submitted"] == 0.30
    p9, _ = build_order("XLE260724C00060500", 1, "buy", entry=0.30, ppind=False)
    assert p9["limit_price"] == "0.30" and "_tick_adjustment" not in p9, p9
    p10, _ = build_order("SPY250815C00500000", 1, "buy", entry=0.31, ppind=False)  # always-penny
    assert p10["limit_price"] == "0.31" and "_tick_adjustment" not in p10, p10
    assert p3b["limit_price"] == "4.50"  # ppind=None path (existing case above) — .2f, unrounded

    # 1b) idempotency: identical logical order → identical client_order_id (Alpaca
    # de-dups the retry); a different size/price → different id (allowed through).
    a, _ = build_order("AAPL", 2, "buy", entry=190, stop=180, target=210, pitch_id="x1")
    b, _ = build_order("AAPL", 2, "buy", entry=190, stop=180, target=210, pitch_id="x1")
    assert a["client_order_id"] == b["client_order_id"], "retry must reuse the id"
    c, _ = build_order("AAPL", 3, "buy", entry=190, stop=180, target=210, pitch_id="x1")
    assert c["client_order_id"] != a["client_order_id"], "different qty must differ"
    d, _ = build_order("AAPL", 2, "sell", entry=190, pitch_id="x1")
    assert d["client_order_id"] != a["client_order_id"], "different side must differ"

    # 2) mocked submit + verify lifecycle
    state = {"phase": 0}
    def fake_request(method, path, params=None, body=None, timeout=12):
        if method == "GET" and path == "/account":
            return 200, {"equity": "10000", "cash": "10000", "buying_power": "10000", "status": "ACTIVE"}
        if method == "GET" and path == "/clock":
            return 200, {"is_open": True}
        if method == "POST" and path == "/orders":
            return 201, {"id": "ord_123", "status": "accepted",
                         "submitted_at": "2026-06-29T14:00:00Z"}
        if method == "GET" and path == "/orders/ord_123":
            state["phase"] += 1                                   # first poll pending, then filled
            if state["phase"] >= 2:
                return 200, {"status": "filled", "filled_qty": "2",
                             "filled_avg_price": "190.12", "legs": [
                                 {"type": "take_profit", "status": "held", "id": "leg_tp"},
                                 {"type": "stop_loss", "status": "held", "id": "leg_sl"}]}
            return 200, {"status": "pending_new", "filled_qty": "0"}
        if method == "GET" and path == "/options/contracts/XLE260724C00060500":
            return 200, {"symbol": "XLE260724C00060500", "ppind": False}   # Bug 3 fix test
        return 404, {"message": "unmocked"}

    orig = _request
    _request = fake_request
    try:
        # point the audit ledger at a temp file so we don't touch the real one
        real_ledger = EXEC_LEDGER
        EXEC_LEDGER = BASE / ".exec_selftest.json"
        res = place_trade("AAPL", 2, "buy", entry=190, stop=180, target=210,
                          setup="VCP", pitch_id="x1", verify=True)
        assert res["ok"] and res["bracket"] and res["order_id"] == "ord_123"
        assert res["fill"]["status"] == "filled"
        assert res["fill"]["filled_avg_price"] == 190.12 and res["fill"]["filled_qty"] == 2.0
        assert len(res["fill"]["legs"]) == 2
        # blocked when buying power short
        _request = lambda m, p, params=None, body=None, timeout=12: (
            (200, {"buying_power": "10", "cash": "10", "equity": "10"}) if p == "/account"
            else (200, {"is_open": True}) if p == "/clock" else (404, {}))
        blocked = place_trade("AAPL", 100, "buy", entry=190, stop=180, target=210, verify=False)
        assert not blocked["ok"] and blocked.get("blocked"), blocked

        # Bug 3 fix, end-to-end: place_trade on the REAL XLE contract from Nico's
        # report, at the exact rejected price ($0.31). Proves the ppind lookup +
        # rounding actually reaches the submitted order, not just build_order in
        # isolation — this is the literal path place_and_log_option_play uses.
        _request = fake_request
        state["phase"] = 0
        xle = place_trade("XLE260724C00060500", 1, "buy", entry=0.31, verify=False)
        assert xle["ok"], xle
        assert xle["limit_price"] == "0.30", xle          # rounded from the invalid $0.31
        assert xle["price_adjustment"]["requested"] == 0.31
        assert xle["price_adjustment"]["submitted"] == 0.30
        assert xle["price_adjustment"]["penny_pilot"] is False
    finally:
        _request = orig
        try:
            (BASE / ".exec_selftest.json").unlink()
        except Exception:
            pass
        EXEC_LEDGER = real_ledger

    # 3) close_position: reads the LIVE position, computes side/intent from what
    # is actually held, clamps to qty_available, and refuses a duplicate close
    # while one is still resting — the exact IONQ/RGTI failure mode, closed.
    positions_db  = {"IONQ": {"qty": "100", "qty_available": "100", "side": "long",
                              "avg_entry_price": "10.00", "asset_class": "us_equity"}}
    open_orders_db = []
    close_phase = {"n": 0}

    def fake_close(method, path, params=None, body=None, timeout=12):
        if method == "GET" and path.startswith("/positions/"):
            sym = path.split("/")[-1]
            p = positions_db.get(sym)
            return (200, dict(p)) if p else (404, {"message": "position does not exist"})
        if method == "GET" and path == "/orders":
            sym = (params or {}).get("symbols")
            return 200, [o for o in open_orders_db if o["symbol"] == sym]
        if method == "POST" and path == "/orders":
            return 201, {"id": "close_ord_1", "status": "accepted",
                         "submitted_at": "2026-07-22T14:00:00Z"}
        if method == "GET" and path == "/orders/close_ord_1":
            close_phase["n"] += 1
            if close_phase["n"] >= 2:
                return 200, {"status": "filled", "filled_qty": "100",
                             "filled_avg_price": "12.00", "legs": []}
            return 200, {"status": "pending_new", "filled_qty": "0"}
        if method == "DELETE" and path == "/positions/RKLB":
            return 200, {"id": "liq_ord_1", "status": "accepted", "symbol": "RKLB"}
        if method == "GET" and path == "/orders/liq_ord_1":
            return 200, {"status": "filled", "filled_qty": "10",
                         "filled_avg_price": "50.00", "legs": []}
        if method == "DELETE" and path == "/positions/GHOST":
            return 404, {"message": "position does not exist"}
        if method == "GET" and path == "/options/contracts/XLE260724C00060500":
            return 200, {"symbol": "XLE260724C00060500", "ppind": False}   # Bug 3 fix test
        return 404, {"message": "unmocked " + path}

    _request = fake_close
    try:
        real_ledger = EXEC_LEDGER
        EXEC_LEDGER = BASE / ".exec_selftest.json"

        res = close_position("IONQ", entry=12.00, verify=True)
        assert res["ok"] and res["side"] == "sell" and res["position_intent"] == "sell_to_close", res
        assert res["qty"] == 100.0 and res["fill"]["status"] == "filled"

        # already flat (position gone after the fill) → refuses, does not resubmit
        positions_db.pop("IONQ")
        flat = close_position("IONQ", entry=12.00, verify=False)
        assert not flat["ok"] and flat.get("already_flat"), flat

        # a resting close order on the book → refuses the duplicate, doesn't pile on
        positions_db["RGTI"] = {"qty": "50", "qty_available": "50", "side": "long",
                                "avg_entry_price": "20.00", "asset_class": "us_equity"}
        open_orders_db.append({"id": "rgti_ord_1", "symbol": "RGTI", "side": "sell", "qty": "50"})
        dup = close_position("RGTI", entry=22.00, verify=False)
        assert not dup["ok"] and dup.get("existing_order_id") == "rgti_ord_1", dup

        # short position → closes with a BUY at buy_to_close, not sell
        positions_db["QCOM250815C00170000"] = {"qty": "20", "qty_available": "20", "side": "short",
                                                "avg_entry_price": "5.00", "asset_class": "us_option"}
        close_phase["n"] = 0
        short_close = close_position("QCOM250815C00170000", entry=4.50, verify=False)
        assert short_close["ok"] and short_close["side"] == "buy" \
            and short_close["position_intent"] == "buy_to_close", short_close

        # 4) liquidate_position: requires confirm=True, hits the native DELETE
        # close-position endpoint, and treats an already-flat symbol as a no-op.
        blocked = liquidate_position("RKLB", confirm=False)
        assert not blocked["ok"] and "confirm=True" in blocked["error"]
        liq = liquidate_position("RKLB", confirm=True)
        assert liq["ok"] and liq["order_id"] == "liq_ord_1" and liq["fill"]["status"] == "filled", liq
        already = liquidate_position("GHOST", confirm=True)
        assert not already["ok"] and already.get("already_flat"), already

        # Bug 3 fix, end-to-end via the CLOSE side too — a close order's limit
        # price is just as subject to Alpaca's tick rejection as an opening one.
        positions_db["XLE260724C00060500"] = {"qty": "1", "qty_available": "1", "side": "long",
                                               "avg_entry_price": "0.28", "asset_class": "us_option"}
        xle_close = close_position("XLE260724C00060500", entry=0.31, verify=False)
        assert xle_close["ok"], xle_close
        assert xle_close["limit_price"] == "0.30", xle_close
        assert xle_close["price_adjustment"]["requested"] == 0.31
        assert xle_close["price_adjustment"]["submitted"] == 0.30
    finally:
        _request = orig
        try:
            (BASE / ".exec_selftest.json").unlink()
        except Exception:
            pass
        EXEC_LEDGER = real_ledger

    print("SELF-TEST OK")
    print("  bracket payload, single payload, option payload — correct")
    print("  submit → poll(pending→filled) → captured $190.12 x2 with 2 bracket legs")
    print("  buying-power pre-check blocks oversized order")
    print("  explicit intent overrides side-only guess; invalid intent rejected")
    print("  close_position: live-position-derived side/intent, qty_available clamp,")
    print("    already-flat + duplicate-open-order guards, short-side buy_to_close")
    print("  liquidate_position: confirm-gated, native DELETE close, already-flat no-op")
    print("  tick-size rounding (Bug 3): pure _option_tick_size/_round_to_tick correct;")
    print("    build_order rounds+reports when ppind given, leaves on-tick prices alone;")
    print("    place_trade + close_position both round the real XLE $0.31 case to $0.30")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="account",
                    choices=["account", "clock", "positions", "reconcile", "audit", "selftest"])
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    elif a.cmd == "reconcile":
        print(json.dumps(reconcile(), indent=2, default=str))
    elif a.cmd == "audit":
        print(json.dumps(audit_trail(), indent=2, default=str))
    elif a.cmd == "positions":
        print(json.dumps(positions(), indent=2, default=str))
    elif a.cmd == "clock":
        print(json.dumps(clock(), indent=2, default=str))
    else:
        print(json.dumps(account(), indent=2, default=str))
