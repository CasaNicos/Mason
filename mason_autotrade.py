"""
mason_autotrade.py — APPROVE-TO-PLACE for the Opus director's calls (PAPER ONLY).

Owner: Agent P (wealth-manager build, 2026-09-28). Contract:
docs/catalyst_desk/WEALTH_MANAGER_CONTRACTS.md.

What it does
------------
  preview(rec_id)                      the EXACT order that would be placed + every gate result.
  approve(rec_id, approver, dry_run)   gates → place (mason_execution) → verify fill → journal
                                       → record on the rec → activity row + inbox notification.
  reject(rec_id, note)                 execution declined; the PREDICTION keeps being graded.
  maybe_autoplace(rec_ids)             only when autotrade.auto_place AND confidence ≥
                                       autotrade.min_confidence_auto → approve(approver="auto").
  settle_pending(rec_ids=None)         finalise 'approved_pending_fill' recs from Alpaca's real
                                       order state (journal on fill; mark unfilled on expiry).
  risk_state()                         summary for the UI.

Hard rules baked in (Nico's standing rules)
-------------------------------------------
  * PAPER ONLY — refuses unless the Alpaca base_url is the paper endpoint.
  * SINGLE-LEG BUYS ONLY — long stock, long call, long put. No spreads, no short
    stock, no selling to open. (memory: options-no-multileg-spreads)
  * DETERMINISTIC — no LLM anywhere in this path. Every number comes from the rec's
    stored trade block, the live Alpaca account/quotes, and config.
  * ALPACA IS SETTLEMENT TRUTH — a journal row is written only from a verified fill
    (mason_execution.place_option_play / place_trade + verify_fill); a resting order is
    'approved_pending_fill' and is journaled later by settle_pending() from the real order.
  * LIMIT ORDERS ONLY (mason_execution policy).

Design decisions
----------------
  * OUTSIDE RTH → REFUSE (not queue). The safer choice: a queued day-limit would be sized
    and gated off stale quotes (option quotes outside RTH are stale/wide), and would sit
    unattended into a gap. Nico re-approves once the market is open; preview still works.
  * Execution state lives in recommendations.extra_json.execution.state, and the rec's
    `status` column STAYS 'open'. Reason: the director grader
    (mason_desk_pipeline.grade_director_predictions) only grades status='open' rows, and a
    rejected/placed call must keep being graded. States:
      placing → placed | approved_pending_fill | failed ; pending → placed | unfilled
      rejected_by_nico (approve by nico may override; auto never places it)
  * Idempotency, two layers: (1) a BEGIN IMMEDIATE compare-and-set claim on the rec before
    any submit; (2) before submitting, Alpaca's own order list is searched for this rec's
    client_order_id prefix (mason-dir-<rec_id>-...) and an existing order is ADOPTED
    instead of placing a second one.
  * Option exits: Alpaca does not support bracket/OCO orders on options. Stop/target
    premiums are recorded as exit RULES on the rec (extra_json.execution.exit_rules) and
    in the journal notes — they are NOT enforced at the broker.

Step 1 hardening (2026-11-13, audit X2-X7, X14) — each has a switch, ON when the key is absent
--------------------------------------------------------------------------------------------
  * PRICES come from mason_quotes (Robinhood's consolidated quote first, checked against Alpaca,
    then validated Alpaca data): the entry limit is last/mid plus a small band, never a far-away
    ask; an entry needs a reliable live price.              autotrade.quotes.enabled / .use_robinhood
  * HALF-DAYS: the 0DTE last-entry time is the configured number of minutes before the broker
    clock's real close (13:00 on 2026-11-27), not a fixed clock time.   autotrade.session_close_from_clock
  * LOSS STOP ON MASON'S OWN MONEY: daily_loss_stop_pct now measures Mason's own trades (closed
    today + open, from agent_trades) against account equity; a separate, wider whole-account
    breaker also blocks entries.      autotrade.loss_stop_own_only / .loss_stop_base / .account_loss_breaker_pct
  * TRUE EXPOSURE: the sleeve is Mason's own remaining quantity x current price, not the whole
    symbol position.                                         autotrade.exposure_own_qty_only
  * A BLOCKED AUTOMATIC ORDER is recorded on the call (state 'blocked' + the gates) with one
    plain-words inbox note.                                   autotrade.persist_blocked
  * LATER PARTIAL FILLS update the journal row and the call's quantity.  autotrade.journal_follow_fills
  * The quote used is stored with every entry (execution.entry_quote).   autotrade.record_quotes
  Fail-safe everywhere: when the new data is not available the old rule applies, or nothing is done.

Step 4 (2026-11-15, audit X11 + X12) — every switch OFF when the key is absent; shadow mode ON
---------------------------------------------------------------------------------------------
  * SIZE BY RISK, LIMITS, VALUE GATE live in mason_sizing.plan(), called once per order from _build
    (_step4). A rule that is ON changes the quantity or adds a gate; a rule that is OFF is only worked
    out and written on the call and the trade (execution.features.entry_features / .sizing_plan), so
    its effect can be seen before it is switched on.     autotrade.sizing / .limits / .value_gate
  * With every switch off AND autotrade.sizing.shadow false, _step4 returns None and nothing below
    differs from before by a single byte. Any failure in the new code sizes the old way and says so.

Step 4b (2026-11-15, Nico's spend table) — the table is the default; its two rules are switches
--------------------------------------------------------------------------------------------
  * DOLLARS PER TRADE: stock by tier (autotrade.tiers), option by days to expiry and tier
    (autotrade.option_spend, which replaces call/put_premium_cap_usd while it is set).
  * A stored 'normal' / 'high' tier is re-checked against the 10-closed-trades lock when the order is
    built (mason_agent_loop.locked_tier).
  * With autotrade.sizing.enabled, an OPTION is sized by its spend limit, not by the amount at risk
    (autotrade.sizing.options_by_spend, true; false = the earlier rule).
  * HARD CEILING on the loss at the stop, % of the account (autotrade.limits.max_loss, OFF by default,
    recorded in shadow mode like the other rules).

Short calls placed as puts (2026-11-17, Nico's ruling of 2026-11-15) — OFF unless autotrade.short_as_put.enabled
---------------------------------------------------------------------------------------------------------
  * A director call that is short on the STOCK is still never sold short. With the switch on, _build asks
    mason_short_put.convert() for ONE put to buy and then builds that put through the unchanged long-option
    path below (buy-to-open, limit order, Learning size, the option spend table). The call's own trade block
    is never rewritten; what was placed is in execution.short_as_put (written before the order goes out).
  * With the switch off and autotrade.short_as_put.shadow on (the default), an AUTOMATIC short stock call
    that is refused also gets a record of the put that would have been bought (execution.short_as_put_shadow).
    Nothing is placed. With both off, nothing below differs from before by a single byte.

Option order handling (2026-11-17, audit X13 / L10) — OFF unless autotrade.option_orders.enabled; record-only by default
---------------------------------------------------------------------------------------------------------------------
  * mason_option_orders: an option purchase may start at the middle price and be raised in steps to a limit fixed at
    approval (_build sizes and checks the order AT that limit; the schedule itself runs from the agent loop's tick);
    liquidity and greeks filters at entry; the real prices next to every order and fill. With
    autotrade.option_orders.enabled false AND .shadow false none of it runs and nothing below differs by a byte.

Never raises; every DB connection is closed in finally.
"""

import copy
import json
import math
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import mason_db
import mason_execution as EX

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "mason.config.json"

_PAPER_HOST = "paper-api.alpaca.markets"
_DATA_BASE = "https://data.alpaca.markets"
_ACTIVE_STATES = {"placing", "placed", "approved_pending_fill"}
_PLACING_STALE_S = 300          # a 'placing' claim older than this is recoverable
_MAX_TRADING_DAYS_OLD = 2
_OPT_MAX_SPREAD_PCT = 25.0      # (ask-bid)/mid above this → illiquid, refuse
_STOCK_ASK_BUFFER = 0.001       # 0.1% over ask when no entry_limit was given
_CHASE_WARN_PCT = 15.0

_DEFAULTS = {"approve_enabled": False, "auto_place": False, "max_pct_per_trade": 2.0,
             "max_open_exposure_pct": 30.0, "max_trades_per_day": 3, "no_new_margin": True,
             "daily_loss_stop_pct": 3.0, "min_confidence_auto": 0.65,
             # ── agent loop (P0) — all OFF by default: tiers_enabled False = old single-floor behaviour ──
             "tiers_enabled": False,
             # Step 4b (Nico's table, 2026-11-15): most dollars per STOCK trade by confidence tier. normal and
             # high stay locked until Mason has normal_requires_own_n closed trades of its own.
             "tiers": {"learning": {"min_p": 0.53, "usd_max": 1250, "max_pct": 1.5},
                       "normal": {"min_p": 0.65, "usd_max": 2000},
                       "high": {"min_p": 0.72, "usd_max": 4000}},
             "normal_requires_own_n": 10,
             # a call with no level (rated under the lowest tier; only placed when Nico approves it by hand)
             # gets no more than a Learning trade instead of max_pct_per_trade (Nico, 2026-11-15)
             "no_level_learning_cap": True,
             # Step 4b: most dollars per OPTION trade by calendar days to expiry (0 = expires today) and tier;
             # the last row is open-ended. While this table is set it REPLACES the two caps on the next line;
             # "option_spend": null in the config brings those two caps back as the rule.
             "option_spend": [{"max_days": 5, "learning": 1000, "normal": 1000, "high": 1000},
                              {"max_days": 59, "learning": 1250, "normal": 2000, "high": 2000},
                              {"max_days": None, "learning": 1250, "normal": 2000, "high": 4000}],
             # (review) The two older caps are what applies when option_spend is null or cannot be read. Their
             # built-in values follow Nico's ruling of 2026-11-15 ($1,250 each; they were 3000 / 1500), so that a
             # settings file without them can never fall back to more than he allowed. The live file sets both.
             "call_premium_cap_usd": 1250, "put_premium_cap_usd": 1250,
             "allow_0dte": False, "zero_dte_underlyings": ["SPY", "QQQ"],
             "zero_dte_last_entry_et": "14:30", "zero_dte_flat_by_et": "15:15",
             "respect_regime_veto": True,
             "queue_premarket": False, "entry_open_delay_min": 5,
             "exits": {"enabled": False, "opt_exit_dte": 1,
                       "time_stop_sessions": {"0-5DTE": 5, "swing": 10, "30d": 30, "90d+": 90},
                       "default_opt_stop_pct": None, "default_opt_target_pct": None,
                       "default_stock_stop_pct": None,
                       # Step 1 (X1, X8, X9) — all ON by default; false = the old rule
                       "opt_stop_guard": True,            # option stop: real bid, tight two-sided quote, mid, confirmed
                       "opt_stop_confirm_ticks": 2,       # ... on this many checks in a row
                       "opt_stop_confirm_window_s": 420,  # ... no further apart than this
                       "stop_open_delay_min": 5,          # ... and never in the first minutes after the open
                       "revalidate_before_send": True,    # a waiting stop/target is re-checked before the order goes out
                       "check_settlement_on_expiry": True,   # look for a sale / exercise before booking an expiry at 0
                       "expiry_settle_wait_days": 3,      # how long to wait for the broker's expiry record
                       "tolerate_sibling_cancel": True},  # a bracket leg the broker already cancelled is not a failure
             "learning": {"prior_n": 20, "prior_p": 0.5, "min_n_segment": 10,
                          # calibration reads the shared (tradable) director verdicts when that
                          # reader exists; false = the old query on the legacy outcome column
                          "shared_verdicts": True},
             # Nico 2026-11-12: inside a tier the TIER CAP sets the size; the director's
             # "x% of account" hint is ignored unless this is true (then it may only lower it).
             "size_hint_lowers_tier": False,
             # P2: a higher tier also needs Mason's OWN closed trades in that horizon|vehicle
             "normal_requires_own_segment_n": 5, "normal_requires_own_segment_win": 0.5,
             # the director may sell stock positions Nico placed (never his options)
             "book_actions": {"enabled": True, "max_per_call": 3},
             "horizon_mix": {"swing": 40, "intraday": 30, "long": 30},
             "sizing_mult": {"enabled": True, "min": 0.5, "max": 1.5},
             "queue_retry_until_et": "10:30",
             # ── Step 1 hardening (2026-11-13) — every switch is ON when absent; false = the old behaviour ──
             # (price sources and limits live under "quotes": see mason_quotes.DEFAULTS)
             "session_close_from_clock": True,   # X3: 0DTE cut-offs follow the broker's real close (half-days)
             "loss_stop_own_only": True,         # X5: daily_loss_stop_pct measures Mason's OWN trades
             "loss_stop_base": "equity",         # X5: percent of "equity" (account) or of "sleeve" (Mason's own capital)
             "account_loss_breaker_pct": 6.0,    # X5: whole-account day loss that also blocks entries (0 / null = off)
             "exposure_own_qty_only": True,      # X6: sleeve = Mason's own quantity x price
             "persist_blocked": True,            # X4: a blocked automatic order is recorded + one inbox note
             "journal_follow_fills": True,       # X7: later partial fills reach the journal row and the call
             "record_quotes": True}              # the quote used is stored with each entry and exit


# ── config / clock / transport (all injectable in tests) ─────────────────────────

def _cfg() -> dict:
    try:
        return json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _at_cfg() -> dict:
    c = copy.deepcopy(_DEFAULTS)
    try:
        for k, v in (_cfg().get("autotrade") or {}).items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k] = _deep_merge(c[k], v)        # nested blocks (tiers, exits, learning) merge
            else:
                c[k] = v
    except Exception:
        pass
    return c


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return None


def _now_et() -> datetime:
    tz = _et_tz()
    return datetime.now(tz) if tz else datetime.now()


def _get(path, params=None):
    """Alpaca trading-API GET with one retry on 429 (prod shares the rate limit)."""
    code, data = None, None
    for attempt in range(3):
        code, data = EX._request("GET", path, params=params)
        if code != 429:
            break
        time.sleep(0.8 * (attempt + 1))
    return code, data


def _data_get(url, params=None):
    """Alpaca market-data GET → (status, json|text). Monkeypatched in tests."""
    try:
        import requests
        r = requests.get(url, headers=EX._headers(), params=params, timeout=10)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except Exception as e:
        return None, str(e)


# ── DB helpers ───────────────────────────────────────────────────────────────────

def _load_rec(rec_id):
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
    finally:
        conn.close()
    try:
        d["extra"] = json.loads(d.get("extra_json") or "{}")
    except Exception:
        d["extra"] = {}
    return d


def _set_execution(rec_id, execution: dict, *, merge=True):
    """Write extra_json.execution (merge onto what's there). status stays untouched."""
    conn = mason_db.get_connection()
    try:
        with conn:
            r = conn.execute("SELECT extra_json FROM recommendations WHERE id=?",
                             (rec_id,)).fetchone()
            if not r:
                return None
            try:
                ex = json.loads(r["extra_json"] or "{}")
            except Exception:
                ex = {}
            cur = dict(ex.get("execution") or {}) if merge else {}
            cur.update(execution)
            hist = list(ex.get("execution_history") or [])
            hist.append({"at": datetime.now().isoformat(timespec="seconds"),
                         "state": cur.get("state")})
            ex["execution"] = cur
            ex["execution_history"] = hist[-20:]
            conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                         (json.dumps(ex, default=str), rec_id))
            return cur
    finally:
        conn.close()


def _claim(rec_id, approver):
    """Atomic compare-and-set: mark the rec 'placing' unless it is already
    placing/placed/pending. Returns (claimed: bool, existing_execution|None)."""
    conn = mason_db.get_connection()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = conn.execute("SELECT extra_json FROM recommendations WHERE id=?",
                             (rec_id,)).fetchone()
            if not r:
                conn.execute("ROLLBACK")
                return False, {"state": "missing"}
            try:
                ex = json.loads(r["extra_json"] or "{}")
            except Exception:
                ex = {}
            cur = ex.get("execution") or {}
            st = cur.get("state")
            if st in _ACTIVE_STATES:
                stale = False
                if st == "placing":
                    try:
                        age = (datetime.now() - datetime.fromisoformat(cur.get("claimed_at"))).total_seconds()
                        stale = age > _PLACING_STALE_S
                    except Exception:
                        stale = True
                if not stale:
                    conn.execute("ROLLBACK")
                    return False, cur
            new = dict(cur)
            new.update({"state": "placing", "approver": approver,
                        "claimed_at": datetime.now().isoformat(timespec="seconds"),
                        "mode": "paper"})
            ex["execution"] = new
            conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                         (json.dumps(ex, default=str), rec_id))
            conn.execute("COMMIT")
            return True, new
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()


def _trades_today(today_iso):
    """Director recs this module placed (or is placing / has resting) today."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, extra_json FROM recommendations WHERE kind='director' "
            "AND json_extract(extra_json,'$.execution.state') IN "
            "('placing','placed','approved_pending_fill','closed') "
            "AND substr(COALESCE(json_extract(extra_json,'$.execution.placed_at'),"
            "json_extract(extra_json,'$.execution.claimed_at')),1,10)=?",
            (today_iso,)).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


# ── pure helpers ─────────────────────────────────────────────────────────────────

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*%")


def parse_size_hint(hint) -> dict:
    """'1% of account' → notional 1.0; '0.5-1% of account' → 0.5 (lower bound, the
    conservative read); '1% of account risk' → risk 1.0; '~5% notional, ~0.2% account
    risk' → notional 5.0 + risk 0.2. 'none'/'0%'/unparseable → nothing (the
    max_pct_per_trade cap then sizes it). Pure."""
    out = {"notional_pct": None, "risk_pct": None, "raw": hint}
    s = str(hint or "").lower()
    for part in re.split(r"[,;]", s):
        for m in _PCT_RE.finditer(part):
            lo = float(m.group(1))
            if m.group(2):
                lo = min(lo, float(m.group(2)))
            tail = part[m.end():m.end() + 25]
            key = "risk_pct" if "risk" in tail else "notional_pct"
            if out[key] is None:
                out[key] = lo
    return out


def occ_symbol(underlying, expiry, right, strike) -> str:
    """OCC 21-char-style symbol: AKAM261016C00120000. Pure."""
    d = date.fromisoformat(str(expiry)[:10])
    return (f"{underlying.upper()}{d.strftime('%y%m%d')}{'C' if right == 'call' else 'P'}"
            f"{int(round(float(strike) * 1000)):08d}")


def trading_days_since(created_iso, today: date, holidays=None) -> int:
    """NYSE sessions strictly after the creation date, up to and including today."""
    try:
        c = datetime.fromisoformat(str(created_iso))
        tz = _et_tz()
        if tz is not None:
            c = (c if c.tzinfo else c.astimezone()).astimezone(tz)
        cd = c.date()
    except Exception:
        return 99
    if holidays is None:
        try:
            import mason_desk_pipeline as PP
            holidays = PP._nyse_holidays()
        except Exception:
            holidays = set()
    n, d = 0, cd + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += timedelta(days=1)
    return n


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ── live reads ───────────────────────────────────────────────────────────────────

def _account():
    code, d = _get("/account")
    if code == 200 and isinstance(d, dict):
        eq = _f(d.get("equity")) or 0.0
        last = _f(d.get("last_equity")) or eq
        return {"ok": True, "equity": eq, "last_equity": last,
                "cash": _f(d.get("cash")) or 0.0,
                "buying_power": _f(d.get("buying_power")) or 0.0,
                "options_buying_power": _f(d.get("options_buying_power")),
                "options_level": d.get("options_trading_level"),
                "trading_blocked": bool(d.get("trading_blocked")),
                "account_blocked": bool(d.get("account_blocked")),
                "status": d.get("status"),
                "day_pnl": eq - last,
                "day_pnl_pct": ((eq - last) / last * 100.0) if last else 0.0}
    return {"ok": False, "error": f"/account {code}: {str(d)[:160]}"}


def _positions():
    code, d = _get("/positions")
    if code == 200 and isinstance(d, list):
        return {"ok": True, "positions": d,
                "gross": sum(abs(_f(p.get("market_value")) or 0.0) for p in d)}
    return {"ok": False, "error": f"/positions {code}: {str(d)[:160]}"}


def _director_symbols() -> set:
    """Symbols (stock tickers / OCC option symbols) of positions the DIRECTOR placed —
    recs whose execution.state is placed / approved_pending_fill. Nico's own and
    seeded positions are NOT the director's risk budget (2026-09-28, Nico)."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT json_extract(extra_json,'$.execution.symbol') s FROM recommendations "
            "WHERE kind='director' AND json_extract(extra_json,'$.execution.state') "
            "IN ('placed','approved_pending_fill','placing')").fetchall()
    finally:
        conn.close()
    return {str(r["s"]).upper() for r in rows if r["s"]}


def _sleeve_exposure(pos: dict, own=None) -> float:
    """Dollars the director's own calls have at work. New rule (exposure_own_qty_only): Mason's own
    remaining quantity x current price (_own_book). Old rule (switch off, or the own book cannot be
    read): the whole market value of every symbol a director call was placed in — which also
    counts shares Nico holds in the same ticker, so it can only over-state."""
    try:
        if _at_cfg().get("exposure_own_qty_only", True):
            ob = own if own is not None else _own_book(pos)
            if ob.get("ok"):
                return float(ob["exposure"])
    except Exception:
        pass
    syms = _director_symbols()
    return sum(abs(_f(p.get("market_value")) or 0.0) for p in (pos.get("positions") or [])
               if str(p.get("symbol") or "").upper() in syms)


def _clock():
    code, d = _get("/clock")
    if code == 200 and isinstance(d, dict):
        out = {"ok": True, "is_open": bool(d.get("is_open")),
               "next_open": d.get("next_open"), "next_close": d.get("next_close"),
               "timestamp": d.get("timestamp")}
        _note_clock(out)
        return out
    return {"ok": False, "error": f"/clock {code}: {str(d)[:160]}"}


_SESSION_CLOSE = {}          # ET date (iso) -> that day's real closing time, learned from the broker clock
_CLOCK_DRIFT_WARN_S = 30.0


def _note_clock(clk):
    """Side jobs of every successful clock read; never raises. (1) today's real close is remembered,
    (2) the gap between this computer's clock and the broker's is passed to the quote checks, with
    one inbox note a day when it is large enough to make fresh prices look old."""
    try:
        _session_close(clk, _now_et())
    except Exception:
        pass
    try:
        skew = _MQ().note_broker_time(clk.get("timestamp"))
        if skew is not None and abs(skew) >= _CLOCK_DRIFT_WARN_S:
            _notify("This computer's clock is off",
                    f"Mason's clock is {abs(skew):.0f} seconds {'ahead of' if skew > 0 else 'behind'} the "
                    "broker's clock. Prices can look older than they are, so some price checks may be "
                    "skipped until the clock is corrected.", severity="warning",
                    dedup=f"autotrade:clock_drift:{_now_et().date().isoformat()}")
    except Exception:
        pass


def _hhmm(s, default):
    try:
        h, m = str(s or default).split(":")
        return int(h), int(m)
    except Exception:
        h, m = default.split(":")
        return int(h), int(m)


def _session_close(clk, now):
    """Today's REAL closing time in ET, from the broker clock's next_close (13:00 on a half-day), or
    None when the broker has not said. While the market is open, or before it opens, next_close is
    today's close; after the close it is the next session's and is ignored (other date). A value
    learned earlier today is kept, so one clock answer without next_close does not lose it."""
    today = now.date().isoformat()
    try:
        nc = (clk or {}).get("next_close")
        if nc:
            dt = datetime.fromisoformat(str(nc).strip().replace("Z", "+00:00"))
            tz = _et_tz()
            if dt.tzinfo is not None and tz is not None:
                dt = dt.astimezone(tz)
            if dt.date().isoformat() == today:
                _SESSION_CLOSE.clear()
                _SESSION_CLOSE[today] = dt
    except Exception:
        pass
    return _SESSION_CLOSE.get(today)


def close_cutoff(hhmm_cfg, default, clk, now, cfg=None) -> tuple:
    """(cut-off time comparable with `now`, plain description) for a rule written as a clock time
    relative to the close. The configured HH:MM means "this many minutes before a normal 16:00
    close"; on a short day the cut-off is the same number of minutes before the REAL close.
    Broker close unknown (or the switch off) → the configured clock time, exactly as before."""
    cfg = cfg if cfg is not None else _at_cfg()
    h, m = _hhmm(hhmm_cfg, default)
    plain = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not cfg.get("session_close_from_clock", True):
        return plain, f"{h:02d}:{m:02d} ET"
    close = _session_close(clk, now)
    if close is None:
        return plain, f"{h:02d}:{m:02d} ET"
    if now.tzinfo is None:
        close = close.replace(tzinfo=None)
    elif close.tzinfo is None:
        close = close.replace(tzinfo=now.tzinfo)
    before = 16 * 60 - (h * 60 + m)
    cut = close - timedelta(minutes=before)
    if (close.hour, close.minute) == (16, 0):
        return cut, f"{cut.strftime('%H:%M')} ET"
    return cut, (f"{cut.strftime('%H:%M')} ET ({before} minutes before today's early "
                 f"{close.strftime('%H:%M')} close)")


def resolve_option_contract(underlying, expiry, right, strike) -> dict:
    """Resolve + verify the exact OCC contract via Alpaca's option-contracts API."""
    want = occ_symbol(underlying, expiry, right, strike)
    code, d = _get("/options/contracts", params={
        "underlying_symbols": underlying.upper(), "expiration_date": str(expiry)[:10],
        "type": right, "strike_price_gte": strike, "strike_price_lte": strike,
        "status": "active", "limit": 100})
    if code != 200 or not isinstance(d, dict):
        return {"ok": False, "symbol": want, "error": f"option contracts lookup {code}: {str(d)[:160]}"}
    for c in d.get("option_contracts") or []:
        if (c.get("symbol") or "").upper() == want:
            if not c.get("tradable", True) or (c.get("status") or "active") != "active":
                return {"ok": False, "symbol": want, "error":
                        f"{want} exists but is not tradable (status={c.get('status')}, "
                        f"tradable={c.get('tradable')})"}
            return {"ok": True, "symbol": want, "contract": c,
                    "ppind": bool(c.get("ppind")) if "ppind" in c else None}
    return {"ok": False, "symbol": want,
            "error": f"no active Alpaca contract {want} ({underlying} {expiry} {right} {strike})"}


def _option_quote(symbol):
    code, d = _data_get(f"{_DATA_BASE}/v1beta1/options/snapshots", {"symbols": symbol})
    if code == 200 and isinstance(d, dict):
        s = (d.get("snapshots") or {}).get(symbol) or {}
        q = s.get("latestQuote") or {}
        bid, ask = _f(q.get("bp")), _f(q.get("ap"))
        mid = round((bid + ask) / 2, 4) if bid and ask else None
        return {"ok": bool(ask), "bid": bid, "ask": ask, "mid": mid,
                "last": _f((s.get("latestTrade") or {}).get("p")), "as_of": q.get("t")}
    return {"ok": False, "error": f"option snapshot {code}: {str(d)[:160]}"}


def _stock_quote(symbol):
    code, d = _data_get(f"{_DATA_BASE}/v2/stocks/{symbol.upper()}/snapshot")
    if code == 200 and isinstance(d, dict):
        q = d.get("latestQuote") or {}
        last = _f((d.get("latestTrade") or {}).get("p"))
        bid, ask = _f(q.get("bp")), _f(q.get("ap"))
        return {"ok": bool(ask or last), "bid": bid, "ask": ask, "last": last,
                "as_of": q.get("t")}
    return {"ok": False, "error": f"stock snapshot {code}: {str(d)[:160]}"}


def _MQ():
    import mason_quotes
    return mason_quotes


def _trusted_quote(symbol, is_opt, cfg=None, **kw):
    """The price the loop acts on (mason_quotes: Robinhood first, cross-checked, validated).
    If that module cannot be used at all, the old Alpaca quote marked legacy=True — every caller
    then applies its old rule unchanged."""
    try:
        MQ = _MQ()
        return MQ.option_quote(symbol, cfg=cfg, **kw) if is_opt else MQ.stock_quote(symbol, cfg=cfg, **kw)
    except Exception as e:
        q = dict(_option_quote(symbol) if is_opt else _stock_quote(symbol))
        q.update({"legacy": True, "kind": "option" if is_opt else "stock", "source": "alpaca_legacy",
                  "fallback": type(e).__name__})
        return q


def _quote_txt(q) -> str:
    """One short line about a quote for a gate detail."""
    if not q:
        return "no quote"
    if q.get("legacy"):
        return f"bid {q.get('bid')} / ask {q.get('ask')} / last {q.get('last')} (Alpaca)"
    if not q.get("ok"):
        return str(q.get("reason") or q.get("error") or "no reliable price")[:220]
    age = q.get("age_s")
    return (f"{q.get('px_src')} {q.get('px')} from {q.get('source')}"
            + (f", {age:.0f}s old" if age is not None else "")
            + (f"; bid {q.get('bid')} / ask {q.get('ask')}" if q.get("two_sided") else ""))


# ── Mason's OWN book (X5 loss stop, X6 exposure) ─────────────────────────────────

def _own_book(pos: dict, today_iso=None) -> dict:
    """Mason's own money, from agent_trades (source='mason') priced off the broker's position list.

    exposure        Mason's own REMAINING quantity x current price (x100 for options), never more
                    than the broker holds in that symbol — NOT the whole symbol position (Nico may
                    hold more of the same ticker). Director calls placed without a row in
                    agent_trades (approved by hand, or placed before the loop existed) count with the
                    quantity on the call, as they always did.
    realized_today  P&L of every exit fill booked today (ET date).
    unrealized      open quantity x (current price − entry price).
    pnl             realized_today + unrealized — what the daily loss stop measures.
    cost_base       entry cost of everything Mason had open at any time today.
    ok=False (table missing, DB error, no position list) → the callers fall back to the old rule."""
    try:
        if not (pos or {}).get("ok"):
            return {"ok": False, "error": "no position list"}
        today_iso = today_iso or _now_et().date().isoformat()
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT rec_id, ticker, occ, qty, entry_px, cost_usd, status, exit_ts, pnl_usd, features_json "
                "FROM agent_trades WHERE source='mason' AND COALESCE(record_deleted,0)=0 AND "
                "(status='open' OR (status='closed' AND substr(COALESCE(exit_ts,''),1,10)=?))",
                (today_iso,)).fetchall()]
            have = {r["rec_id"] for r in conn.execute(
                "SELECT rec_id FROM agent_trades WHERE source='mason'").fetchall()}
            recs = [dict(r) for r in conn.execute(
                "SELECT id, extra_json FROM recommendations WHERE kind='director' AND "
                "json_extract(extra_json,'$.execution.state') IN ('placed','approved_pending_fill','placing')"
            ).fetchall()]
        finally:
            conn.close()
        held = {}
        for p_ in pos.get("positions") or []:
            sym = str(p_.get("symbol") or "").upper()
            q = _f(p_.get("qty")) or 0.0
            if not sym or q <= 0 or str(p_.get("side") or "long").lower() != "long":
                continue                                   # Mason only ever buys; a short is the owner's
            px = _f(p_.get("current_price"))
            if not px and _f(p_.get("market_value")):
                px = abs(_f(p_.get("market_value"))) / q / (100.0 if len(sym) > 12 else 1.0)
            held[sym] = {"left": q, "px": px}
        out = {"ok": True, "exposure": 0.0, "realized_today": 0.0, "unrealized": 0.0, "cost_base": 0.0,
               "open_rows": 0, "unpriced": 0, "by_symbol": {}}
        for r in rows:
            sym = str(r.get("occ") or r.get("ticker") or "").upper()
            mult = 100.0 if r.get("occ") else 1.0
            entry, qty = _f(r.get("entry_px")), _f(r.get("qty")) or 0.0
            try:
                parts = list(json.loads(r.get("features_json") or "{}").get("partial_exits") or [])
            except Exception:
                parts = []
            out["cost_base"] += _f(r.get("cost_usd")) or ((entry or 0.0) * qty * mult)
            booked_before = 0.0
            for x in parts:
                xq, xp = _f(x.get("qty")) or 0.0, _f(x.get("px"))
                if xp is None or entry is None:
                    continue
                gain = (xp - entry) * xq * mult
                if str(x.get("at") or "")[:10] == today_iso:
                    if r["status"] == "open":
                        out["realized_today"] += gain
                else:
                    booked_before += gain
            if r["status"] == "closed":                    # closed today: its whole P&L minus what was booked earlier
                if r.get("pnl_usd") is not None:
                    out["realized_today"] += float(r["pnl_usd"]) - booked_before
                continue
            out["open_rows"] += 1
            rem = max(0.0, qty - sum(_f(x.get("qty")) or 0.0 for x in parts))
            h = held.get(sym)
            take = min(rem, h["left"]) if h else 0.0
            if take <= 0:
                continue
            h["left"] -= take
            px = h["px"] or entry
            if not h["px"]:
                out["unpriced"] += 1
            if not px:
                continue
            out["exposure"] += take * px * mult
            out["by_symbol"][sym] = out["by_symbol"].get(sym, 0.0) + take * px * mult
            if entry:
                out["unrealized"] += (px - entry) * take * mult
        for rec in recs:                                   # director calls with no agent_trades row
            if rec["id"] in have:
                continue
            try:
                e = (json.loads(rec["extra_json"] or "{}").get("execution") or {})
            except Exception:
                continue
            sym = str(e.get("symbol") or "").upper()
            h = held.get(sym)
            take = min(_f(e.get("qty")) or 0.0, h["left"]) if h else 0.0
            if take <= 0:
                continue
            h["left"] -= take
            px = h["px"] or _f(e.get("price")) or _f(e.get("limit_price"))
            if not px:
                continue
            mult = 100.0 if e.get("asset") == "option" else 1.0
            out["exposure"] += take * px * mult
            out["by_symbol"][sym] = out["by_symbol"].get(sym, 0.0) + take * px * mult
        out["pnl"] = out["realized_today"] + out["unrealized"]
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _loss_stop(acct: dict, own, cfg: dict) -> dict:
    """The daily loss stop. New rule (loss_stop_own_only): Mason's own trades — closed today plus
    open — as a percent of account equity at the last close (or of Mason's own capital when
    loss_stop_base is "sleeve"); the limit is still daily_loss_stop_pct. Old rule (switch off, or
    Mason's own book cannot be read): the whole account's day P&L against the same percent."""
    dl = float(cfg.get("daily_loss_stop_pct") or 0)
    dp = acct.get("day_pnl_pct") or 0.0
    if cfg.get("loss_stop_own_only", True) and own and own.get("ok"):
        sleeve_base = str(cfg.get("loss_stop_base") or "equity").lower() == "sleeve"
        base = own.get("cost_base") if sleeve_base else (acct.get("last_equity") or acct.get("equity"))
        pct = (own["pnl"] / base * 100.0) if base else 0.0
        return {"ok": pct > -dl, "own": True, "pct": pct,
                "detail": (f"Mason's own trades: closed today ${own['realized_today']:+,.2f}, open "
                           f"${own['unrealized']:+,.2f} = {pct:+.2f}% of "
                           f"{'its own capital' if sleeve_base else 'account equity'}; stop at -{dl}% "
                           f"(whole account today {dp:+.2f}%, not counted here)")}
    why = "" if not cfg.get("loss_stop_own_only", True) else \
        f" [Mason's own book unavailable ({(own or {}).get('error') or 'not read'}) — whole account used]"
    return {"ok": dp > -dl, "own": False, "pct": dp,
            "detail": f"day P&L {dp:+.2f}% (${acct.get('day_pnl', 0):+,.2f}); stop at -{dl}%{why}"}


# ── the plan: what would be placed ───────────────────────────────────────────────

def _gate(gates, name, ok, detail):
    gates.append({"gate": name, "ok": bool(ok), "detail": detail})


def _step4(cfg, rec, *, vehicle, ticker, limit, stop, target, qty, budget, equity, pos, quote, approver,
           now, expiry=None, order_symbol=None, tier=None):
    """Step 4 (audit X11 + X12): size by risk, limits and the value gate — mason_sizing.plan().
    None when every switch and shadow mode are off: then nothing runs and the order is built exactly
    as before. Any failure comes back as a plan that changes nothing and says why."""
    try:
        import mason_sizing as SZ
        if not SZ.active(cfg):
            return None
    except Exception:
        return None
    try:
        ex = rec.get("extra") or {}
        execu = ex.get("execution") or {}
        if tier is None:                                     # (_build passes the tier it really sized with)
            tier = execu.get("tier") if cfg.get("tiers_enabled") else None
        mult = 1.0
        if tier and ex.get("horizon") is not None:           # the same learned multiplier the dollar budget gets
            try:
                import mason_agent_loop as AL
                mult = AL.sizing_mult(ex.get("horizon"), vehicle, cfg)
            except Exception:
                mult = 1.0
        p = _f(execu.get("p_cal"))
        if p is None:
            p = _f(ex.get("confidence"))
        return SZ.plan(cfg=cfg, rec=rec, vehicle=vehicle, ticker=ticker, limit=limit, stop=stop, target=target,
                       qty_today=qty, usd_budget=budget, tier=tier, equity=equity, pos=pos, quote=quote,
                       approver=approver, now=now, expiry=expiry, p=p, risk_mult=mult, order_symbol=order_symbol)
    except Exception as e:
        return {"qty": qty, "qty_today": qty, "changed": False, "live_blocks": [], "warnings": [], "rules": [],
                "fallback": [f"step4: {type(e).__name__}: {e} — sized the old way"]}


_STEP4_BASIS_WORDS = {"spend": "cut by the spend and loss limits", "limits": "made smaller by the limits"}


def _step4_qty(plan, qty, sizing):
    """The quantity to place. Today's `qty` unless a Step 4 rule that is ON changed it to a real
    quantity (never to zero here: a refusal is a gate with its reason, added by _step4_gates)."""
    try:
        if plan is None or not plan.get("changed"):
            return qty
        new = min(int(plan.get("qty") or 0), int(qty))       # Step 4 only ever makes an order smaller
        if "risk_qty" in sizing:                             # the director's own risk hint still caps it
            new = min(new, int(sizing["risk_qty"]))
        if new < 1 or new == qty:
            return qty
        sizing["qty_flat"] = qty
        sizing["step4_qty"] = new
        if plan.get("size_basis") == "spend":                # Step 4b: an option sized by its spend limit
            sizing["step4_basis"] = "spend"
        elif not (plan.get("switches") or {}).get("sizing"):  # (review) size-by-risk is off: a limit cut it (the loss
            sizing["step4_basis"] = "limits"                 # ceiling, earnings ...), so the words must not say "by risk"
        return new
    except Exception:
        return qty


def _step4_gates(plan, gates, warnings):
    """Gates of the Step 4 rules that are ON (a passed one is listed too); warnings for a hand approval."""
    try:
        if plan is None:
            return
        failed = {b["gate"]: b["detail"] for b in (plan.get("live_blocks") or [])}
        for r in plan.get("rules") or []:
            g = r.get("rule")                                # a rule is named after its gate
            if g in _GATE_WORDS and r.get("on") and r.get("ok") and g not in failed:
                _gate(gates, g, True, str(r.get("detail") or "")[:400])
        for g, d in failed.items():
            _gate(gates, g, False, str(d)[:400])
        warnings.extend(str(w)[:400] for w in (plan.get("warnings") or []))
    except Exception:
        pass


def _step4_features(plan, feats):
    """Features kept on the call and the trade: today's dict plus what Step 4 worked out."""
    if plan is None:
        return feats
    try:
        import mason_sizing as SZ
        return dict(feats, **SZ.record(plan))
    except Exception as e:
        return dict(feats, sizing_plan={"fallback": list(plan.get("fallback") or []) + [f"record: {type(e).__name__}"]})


# ── option order handling (mason_option_orders) ─────────────────────────────────

def _OO():
    import mason_option_orders
    return mason_option_orders


def _oo_entry(cfg, gates, warnings, **kw):
    """Filters, records and (when switched on) the entry schedule for one option order. None when
    autotrade.option_orders is off and not recording, or on any failure: then _build is untouched."""
    try:
        oo = _OO().build_hook(cfg, **kw)
        if oo:
            for g, ok_, detail in oo.get("gates") or []:
                _gate(gates, g, ok_, detail)
            warnings.extend(oo.get("warnings") or [])
        return oo
    except Exception:
        return None


def _oo_features(feats, oo):
    """The features kept on the call, plus what option order handling recorded (the same dict when it is off)."""
    try:
        if oo and oo.get("features"):
            return dict(feats or {}, **oo["features"])
    except Exception:
        pass
    return feats


# ── short calls placed as puts (mason_short_put) ─────────────────────────────────

def _SP():
    import mason_short_put
    return mason_short_put


def _placed_trade(ex) -> dict:
    """The trade block that was really placed for a call: the put when a short stock call was placed as a
    put (execution.short_as_put), otherwise the call's own trade block, exactly as before."""
    try:
        if ((ex or {}).get("execution") or {}).get("short_as_put"):
            return _SP().placed_trade(ex)
    except Exception:
        pass
    return (ex or {}).get("trade") or {}


def _short_put(cfg, rec, gates, *, approver, now, equity, pos, mkt_closed, force=False):
    """Short call placed as a put. None when the call is not short on the stock, the rule is off, or the call
    is being approved by hand without short_as_put.manual (then nothing is touched: the single_leg_buy gate
    has already refused it, as before). Otherwise the single_leg_buy gate is rewritten (the order, if any,
    will be a put PURCHASE) and one of two new gates says whether a put was found; the conversion comes back
    only when it was."""
    try:
        if not _SP().applies(cfg, rec, approver=approver, force=force):
            return None
    except Exception:
        return None
    for g in gates:
        if g.get("gate") == "single_leg_buy":
            g.update(ok=True, detail="short call on the stock: placed as a put purchase (buy-to-open), never as a "
                                     "short sale")
    # No look-up for a call that cannot be placed anyway: one of the checks above has already refused it (too old,
    # already placed, account not readable ...), or the market is closed and nobody is looking at a preview.
    # A call whose put was already chosen needs no look-up, so it is always shown with that put.
    skip = {"single_leg_buy"} | ({"approve_enabled"} if force else set())
    if _SP().record_of(rec.get("extra")) is None and (
            any(not g.get("ok") and g.get("gate") not in skip for g in gates) or (mkt_closed and approver == "auto")):
        return None
    try:
        sap = _SP().convert(cfg, rec, approver=approver, now=now, equity=equity, pos=pos, mkt_closed=mkt_closed,
                            force=force)
    except Exception:
        sap = None
    if sap is None:                                        # (cannot happen after applies(); refused, never guessed)
        _gate(gates, "short_put_wait", False, "the put for this short call could not be worked out")
        return None
    if not sap.get("ok"):
        _gate(gates, sap.get("gate") or "short_as_put", False, str(sap.get("detail") or "no suitable put")[:400])
        return None
    tr = sap.get("trade") or {}
    if str(tr.get("vehicle")) != "put" or _f(tr.get("strike")) is None or not tr.get("expiry"):
        _gate(gates, "short_as_put", False, "the put for this short call could not be worked out")
        return None
    _gate(gates, "short_as_put", True, str(((sap.get("record") or {}).get("why") or {}).get("text") or "a put was chosen")[:400])
    return sap


def _short_put_stamp(res, sap):
    """What the preview says about a short call that is being placed as a put."""
    try:
        rec = dict(sap["record"])
        o = res.get("order") or {}
        if o:
            rec["order"] = {"qty": o.get("qty"), "limit": o.get("limit_price"), "est_cost": o.get("est_cost")}
        res["short_as_put"] = rec
        res["placed_as"] = sap["trade"]
        res["director_trade"] = sap.get("director_trade") or res.get("director_trade")
        head, sep, tail = str(res.get("summary") or "").partition(" — ")
        res["summary"] = head + " (short call placed as a put)" + sep + tail
    except Exception:
        pass
    return res


def _short_put_shadow(rec_id, p, approver):
    """Record-only mode (autotrade.short_as_put.enabled false, .shadow true): an AUTOMATIC short stock call
    has just been refused. Work out the put that would have been bought, with the same code that would
    build the order, and keep it on the call (execution.short_as_put_shadow). Nothing is placed, the
    refusal stands, no note is sent. Skipped while the market is closed (option prices are old then) and
    once a record exists. Never raises."""
    try:
        if approver != "auto":
            return None
        SP = _SP()
        cfg = _at_cfg()
        if not SP.shadow_on(cfg):
            return None
        rec = _load_rec(rec_id)
        if not rec or not SP.is_short_stock(rec):
            return None
        failed = {str(b).split(":", 1)[0].strip() for b in (p.get("blocked") or [])}
        if "single_leg_buy" not in failed or "market_open" in failed:
            return None
        if ((rec.get("extra") or {}).get("execution") or {}).get("short_as_put_shadow"):
            return None
        would = _build(rec, approver=approver, _short_put_as_if=True)
        gates = [g for g in (would.get("gates") or []) if not g.get("ok") and g.get("gate") != "approve_enabled"]
        shadow = SP.shadow_record(would, plain_block_reasons(gates))
        conn = mason_db.get_connection()
        try:
            with conn:                                         # one key, no state change, no history entry
                r = conn.execute("SELECT extra_json FROM recommendations WHERE id=?", (rec_id,)).fetchone()
                if not r:
                    return None
                ex = json.loads(r["extra_json"] or "{}")
                cur = dict(ex.get("execution") or {})
                cur["short_as_put_shadow"] = shadow
                ex["execution"] = cur
                conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?", (json.dumps(ex, default=str), rec_id))
        finally:
            conn.close()
        return shadow
    except Exception:
        return None


def _build(rec, *, approver="nico", _short_put_as_if=False):
    """Compute order + sizing + every gate. Pure over its I/O. Returns preview dict."""
    cfg = _at_cfg()
    ex = rec.get("extra") or {}
    tr = ex.get("trade") or {}
    gates, warnings = [], []
    now = _now_et()
    today = now.date()
    ticker = (rec.get("ticker") or "").upper()
    direction = (rec.get("direction") or "").lower()
    vehicle = (tr.get("vehicle") or "").lower()
    execu = ex.get("execution") or {}
    conf = _f(ex.get("confidence"))

    # ── flags / account mode ──
    _gate(gates, "approve_enabled", cfg.get("approve_enabled"),
          "autotrade.approve_enabled is ON" if cfg.get("approve_enabled")
          else "autotrade.approve_enabled is OFF in mason.config.json — placement disabled")
    try:
        _, _, base, enabled = EX._creds()
    except Exception:
        base, enabled = "", False
    _gate(gates, "paper_trading_enabled", enabled,
          "paper_trading.enabled" if enabled else "paper_trading.enabled is false")
    is_paper = _PAPER_HOST in (base or "")
    _gate(gates, "paper_only", is_paper,
          f"base_url={base}" if is_paper else f"REFUSED: base_url {base!r} is not the Alpaca PAPER endpoint")

    # ── rec eligibility ──
    _gate(gates, "is_director_rec", rec.get("kind") == "director" and rec.get("created_by") == "opus_director",
          f"kind={rec.get('kind')}, created_by={rec.get('created_by')}"
          + ("" if rec.get("created_by") == "opus_director" else " — only Opus director calls are placeable"))
    _gate(gates, "directional", direction in ("long", "short"),
          f"direction={direction}" + ("" if direction in ("long", "short") else " — watch/neutral calls have no trade"))
    _gate(gates, "status_open", rec.get("status") == "open" and not rec.get("record_deleted"),
          f"status={rec.get('status')}")
    st = execu.get("state")
    if st == "placing":
        try:
            if (datetime.now() - datetime.fromisoformat(execu.get("claimed_at"))).total_seconds() > _PLACING_STALE_S:
                st = "placing_stale"
        except Exception:
            st = "placing_stale"
    if st in _ACTIVE_STATES:
        _gate(gates, "not_already_placed", False,
              f"already {st} (order {execu.get('order_id') or 'in flight'}) — will not place twice")
    elif st == "closed":
        _gate(gates, "not_already_placed", False,
              "already traded and closed — will not place twice")
    elif st == "rejected_by_nico" and approver != "nico":
        _gate(gates, "not_already_placed", False, "Nico rejected this call — auto will not place it")
    else:
        _gate(gates, "not_already_placed", True,
              f"no prior execution" + (f" (previous state {st})" if st else ""))
    age = trading_days_since(rec.get("created_date"), today)
    _gate(gates, "fresh", age <= _MAX_TRADING_DAYS_OLD,
          f"created {str(rec.get('created_date'))[:16]}, {age} trading day(s) ago"
          + ("" if age <= _MAX_TRADING_DAYS_OLD else f" — stale (> {_MAX_TRADING_DAYS_OLD}); refused"))

    # ── single-leg buy only ──
    side_ok, why = False, ""
    if vehicle == "stock":
        side_ok = direction == "long"
        why = "long stock = buy" if side_ok else "short stock = sell-to-open — not a single-leg BUY; refused"
    elif vehicle in ("call", "put"):
        side_ok = (direction, vehicle) in (("long", "call"), ("short", "put"))
        why = (f"buy-to-open {vehicle}" if side_ok else
               f"{direction} via {vehicle} is inconsistent (long→call, short→put)")
    else:
        why = f"vehicle {vehicle!r} unsupported (stock|call|put only)"
    _gate(gates, "single_leg_buy", side_ok, why)

    entry_limit, stop, target = _f(tr.get("entry_limit")), _f(tr.get("stop")), _f(tr.get("target"))
    hint = parse_size_hint(tr.get("size_hint"))
    order, exit_rules, sizing, quote = None, None, {"size_hint": hint}, None
    tier_budget = None
    step4 = None                                             # Step 4 plan (None = switched off entirely)
    oo = None                                                # option order handling (None = switched off entirely)

    acct = _account()
    pos = _positions()
    clk = _clock()
    equity = acct.get("equity") if acct.get("ok") else None
    cap_pct = float(cfg.get("max_pct_per_trade") or 0)
    mkt_closed = bool(clk.get("ok") and not clk.get("is_open"))      # nothing can be placed then
    own = _own_book(pos) if pos.get("ok") else None                  # Mason's own money (X5, X6)

    if not acct.get("ok"):
        _gate(gates, "account", False, f"couldn't read the paper account: {acct.get('error')}")
    elif acct.get("trading_blocked") or acct.get("account_blocked") or (acct.get("status") not in (None, "ACTIVE")):
        _gate(gates, "account", False, f"account not tradable (status={acct.get('status')}, "
                                       f"trading_blocked={acct.get('trading_blocked')})")
    else:
        _gate(gates, "account", True, f"equity ${equity:,.2f}, buying power ${acct['buying_power']:,.2f}")

    # ── short call placed as a put (Nico 2026-11-15). `sap` stays None, and nothing below runs differently,
    #    unless autotrade.short_as_put.enabled is true AND this is a short call on the stock itself. ──
    sap = None
    if not side_ok and vehicle == "stock" and direction == "short":
        sap = _short_put(cfg, rec, gates, approver=approver, now=now, equity=equity, pos=pos,
                         mkt_closed=mkt_closed, force=_short_put_as_if)
        if sap:                                            # a put was found: from here on the order is that put
            tr, vehicle, side_ok = sap["trade"], "put", True
            entry_limit, stop, target = _f(tr.get("entry_limit")), _f(tr.get("stop")), _f(tr.get("target"))

    # ── instrument: resolve + quote + size ──
    if side_ok and equity:
        pct = cap_pct if hint["notional_pct"] is None else min(cap_pct, hint["notional_pct"])
        budget = equity * pct / 100.0
        sizing.update({"equity": equity, "pct_used": pct, "budget": round(budget, 2),
                       "cap_pct": cap_pct})
        tier = execu.get("tier") if cfg.get("tiers_enabled") else None
        if tier:
            tier_stored = tier
            try:
                import mason_agent_loop as AL
                tier = AL.locked_tier(tier, cfg)           # Step 4b: normal / high stay locked (re-checked here)
                opt_dte = None
                if vehicle in ("call", "put"):             # Step 4b: calendar days to expiry, US Eastern (0 = today)
                    try:
                        opt_dte = (date.fromisoformat(str(tr.get("expiry"))[:10]) - today).days
                    except Exception:
                        opt_dte = None                     # not known: the smallest amount of the table
                tb = AL.size_budget_usd(tier, vehicle, equity, cfg, horizon=ex.get("horizon"), dte=opt_dte)
            except Exception:
                tb = None
            if tb:
                budget = tb
                if cfg.get("size_hint_lowers_tier") and hint["notional_pct"] is not None:
                    budget = min(budget, equity * hint["notional_pct"] / 100.0)
                tier_budget = budget
                sizing.update({"budget": round(budget, 2), "tier": tier, "budget_source": "tier"})
                if tier != tier_stored:
                    sizing["tier_stored"] = tier_stored
            else:                                          # never fall back to the % budget
                budget = 0.0
                sizing.update({"budget": 0.0, "tier": tier, "budget_source": "tier"})
                _gate(gates, "tier_budget", False,
                      f"tier {tier!r} has no usable budget in autotrade.tiers — refused")
        elif cfg.get("tiers_enabled") and cfg.get("no_level_learning_cap", True) is not False:   # no level on the call (one Nico approves by hand):
            try:                                           # never more than a Learning trade (Nico, 2026-11-15)
                import mason_agent_loop as AL
                _nl_dte = None
                if vehicle in ("call", "put"):
                    try:
                        _nl_dte = (date.fromisoformat(str(tr.get("expiry"))[:10]) - today).days
                    except Exception:
                        _nl_dte = None                     # not known: the smallest amount of the table
                _nl = AL.size_budget_usd("learning", vehicle, equity, cfg, horizon=ex.get("horizon"), dte=_nl_dte)
                if _nl and _nl < budget:
                    budget = _nl
                    sizing.update({"budget": round(budget, 2), "budget_source": "learning limit (no level on the call)"})
            except Exception:
                pass
        if sap:                                            # always a Learning-size trade, and the whole premium
            if tier and tier != "learning":                # inside the loss ceiling (what is paid for a put can be lost)
                sizing["tier_stored"] = sizing.get("tier_stored") or tier
                tier = "learning"
                sizing["tier"] = tier
            if budget > sap["budget_cap"]:
                budget = sap["budget_cap"]
                sizing.update({"budget": round(budget, 2), "budget_source": "learning size (short call placed as a put)"})
            if tier_budget is not None:
                tier_budget = min(tier_budget, budget)
        if vehicle == "stock":
            # X2: the limit comes from the last trade (plus a small band) or a validated ask — never
            # from a thin single-venue quote. While the market is closed nothing can be placed, so
            # the preview may show an old price (any_age).
            quote = _trusted_quote(ticker, False, cfg, any_age=mkt_closed)
            if quote.get("legacy"):                        # old rule, unchanged
                ask = quote.get("ask") or quote.get("last")
                cap = round(ask * (1 + _STOCK_ASK_BUFFER), 2) if ask else None
                cap_src, live_px, have_px = "live ask +0.1%", (quote.get("last") or quote.get("bid") or ask), True
            else:
                cap, cap_src = _MQ().buy_limit(quote, cfg=cfg)
                ask = live_px = quote.get("px")
                have_px = bool(quote.get("ok") and live_px)
            if entry_limit:
                limit = entry_limit
                if cap:
                    limit = min(entry_limit, cap)
                    if ask and ask > entry_limit * (1 + _CHASE_WARN_PCT / 100):
                        warnings.append(f"live price {ask:.2f} is >{_CHASE_WARN_PCT:.0f}% above the "
                                        f"director's entry {entry_limit:.2f} — limit will likely rest unfilled")
                src = "entry_limit" if limit == entry_limit else f"{cap_src} (below director entry)"
                if quote.get("bid") and limit < quote["bid"] * 0.99:
                    warnings.append(f"limit {limit} is below the live bid {quote['bid']} — the entry "
                                    f"will rest until price comes back (unfilled after "
                                    f"{_MAX_TRADING_DAYS_OLD} trading days it is cancelled by settle_pending)")
            elif cap:
                limit, src = cap, cap_src
            else:
                limit, src = None, "no price"
            if limit and have_px:
                _gate(gates, "price", True, f"limit {limit} ({src}); live: {_quote_txt(quote)}")
            elif limit:                                    # an entry limit but nothing reliable to check it against
                _gate(gates, "price", False, f"no reliable live price right now — {_quote_txt(quote)}")
            else:
                _gate(gates, "price", False, f"no entry_limit and no live quote: {_quote_txt(quote)}")
            if limit:
                qty = int(budget // limit)
                if hint["risk_pct"] and stop and limit > stop \
                        and (tier_budget is None or cfg.get("size_hint_lowers_tier")):
                    rq = int((equity * hint["risk_pct"] / 100.0) // (limit - stop))
                    sizing["risk_qty"] = rq
                    qty = min(qty, rq)
                step4 = _step4(cfg, rec, vehicle=vehicle, ticker=ticker, limit=limit, stop=stop, target=target,
                               qty=qty, budget=budget, equity=equity, pos=pos, quote=quote, approver=approver,
                               now=now, order_symbol=ticker, tier=tier)
                qty = _step4_qty(step4, qty, sizing)
                sizing.update({"qty": qty, "unit_cost": limit, "est_cost": round(qty * limit, 2)})
                bracket = stop is not None and target is not None
                order = {"symbol": ticker, "asset": "stock", "side": "buy", "qty": qty,
                         "type": "limit", "limit_price": limit,
                         "time_in_force": "gtc" if bracket else "day",
                         "order_class": "bracket" if bracket else "simple",
                         "stop_loss": stop if bracket else None,
                         "take_profit": target if bracket else None,
                         "est_cost": round(qty * limit, 2)}
                if not bracket:
                    exit_rules = {"stop": stop, "target": target, "enforced_at_broker": False,
                                  "note": "stop and target not both given — no bracket; exits are rules only"}
                else:
                    exit_rules = {"stop": stop, "target": target, "enforced_at_broker": True,
                                  "note": "bracket order: stop-loss + take-profit legs live at Alpaca"}
                _gate(gates, "qty", qty >= 1,
                      (f"{qty} sh, {'made smaller by the limits' if sizing.get('step4_basis') == 'limits' else 'sized by risk'}"
                       f" (the flat-dollar rule gave {sizing['qty_flat']} = "
                       f"floor(${budget:,.2f} / {limit}))" if "qty_flat" in sizing
                       else f"{qty} sh = floor(${budget:,.2f} / {limit})")
                      + (f", risk-capped {sizing.get('risk_qty')}" if "risk_qty" in sizing else "")
                      + ("" if qty >= 1 else " — budget buys < 1 share; refused"))
                lv_ok, lv = True, []
                live = live_px
                if bracket and not (stop < limit < target):
                    lv_ok = False; lv.append(f"need stop {stop} < limit {limit} < target {target}")
                if live and stop and live <= stop:
                    lv_ok = False; lv.append(f"live {live} already at/below stop {stop} — call invalidated")
                if live and target and live >= target:
                    lv_ok = False; lv.append(f"live {live} already at/above target {target} — no edge left")
                _gate(gates, "levels_valid", lv_ok, "; ".join(lv) or
                      f"stop {stop} < limit {limit} < target {target}; live {live}")
        else:
            expiry, strike = tr.get("expiry"), _f(tr.get("strike"))
            exp_ok, exp_detail = False, None
            try:
                exp_d = date.fromisoformat(str(expiry)[:10])
                exp_ok = exp_d > today
                if exp_d == today:
                    hhmm = now.strftime("%H:%M")
                    # X3: "14:30" means 90 minutes before a normal close; on a half-day the cut-off
                    # moves with the broker clock's real close (11:30 for a 13:00 close).
                    cut, last = close_cutoff(cfg.get("zero_dte_last_entry_et"), "14:30", clk, now, cfg)
                    if not cfg.get("allow_0dte"):
                        exp_detail = f"expiry {expiry} is today — 0DTE refused (allow_0dte is off)"
                    elif ticker not in [str(u).upper() for u in (cfg.get("zero_dte_underlyings") or [])]:
                        exp_detail = f"expiry {expiry} is today — 0DTE refused ({ticker} not in zero_dte_underlyings)"
                    elif now.replace(second=0, microsecond=0) > cut:
                        exp_detail = f"expiry {expiry} is today — 0DTE refused (after {last} last entry; now {hhmm})"
                    else:
                        exp_ok = True
                        exp_detail = (f"expiry {expiry} is today — 0DTE allowed (allow_0dte, {ticker} in "
                                      f"zero_dte_underlyings, {hhmm} <= {last})")
            except Exception:
                pass
            _gate(gates, "expiry", exp_ok, exp_detail or (f"expiry {expiry}" + ("" if exp_ok else " — expired or expiring today; refused")))
            res = resolve_option_contract(ticker, expiry, vehicle, strike) if (exp_ok and strike) \
                else {"ok": False, "error": "missing strike/expiry"}
            _gate(gates, "contract_resolved", res.get("ok"),
                  f"{res.get('symbol')} active+tradable on Alpaca" if res.get("ok") else res.get("error"))
            if res.get("ok"):
                sym = res["symbol"]
                # X2/X14: Robinhood's real option quote first; the entry needs a fresh two-sided quote
                # inside the entry spread limit, and pays mid plus a band at most (never above the ask).
                try:
                    entry_spread = float(_MQ().qcfg(cfg).get("opt_entry_max_spread_pct") or _OPT_MAX_SPREAD_PCT)
                except Exception:
                    entry_spread = _OPT_MAX_SPREAD_PCT
                quote = _trusted_quote(sym, True, cfg, any_age=mkt_closed, max_spread_pct=entry_spread)
                bid, ask, mid = quote.get("bid"), quote.get("ask"), quote.get("mid")
                if quote.get("legacy"):                    # old rule, unchanged
                    liq_ok = bool(bid and ask and mid)
                    spr = ((ask - bid) / mid * 100) if liq_ok else None
                    _gate(gates, "option_liquidity", liq_ok and spr <= _OPT_MAX_SPREAD_PCT,
                          f"bid {bid} / ask {ask}, spread {spr:.1f}% of mid" if liq_ok
                          else f"no two-sided quote: {_quote_txt(quote)}")
                    cap, cap_src = ask, "live ask"
                else:
                    liq_ok = bool(quote.get("ok") and quote.get("two_sided") and bid and ask and mid)
                    spr = quote.get("spread_pct")
                    _gate(gates, "option_liquidity", liq_ok,
                          f"bid {bid} / ask {ask}, spread {spr:.1f}% of mid ({quote.get('source')})" if liq_ok
                          else f"no two-sided quote that can be trusted: {_quote_txt(quote)}")
                    cap, cap_src = _MQ().buy_limit(quote, cfg=cfg) if liq_ok else (None, None)
                if entry_limit:
                    limit = min(entry_limit, cap) if cap else entry_limit
                    src = "entry_limit" if limit == entry_limit else f"{cap_src} (below director entry)"
                    if mid and mid > entry_limit * (1 + _CHASE_WARN_PCT / 100):
                        warnings.append(f"live mid {mid} is >{_CHASE_WARN_PCT:.0f}% above the director's "
                                        f"entry {entry_limit} — limit will likely rest unfilled")
                    if mid and mid < entry_limit * 0.6:
                        warnings.append(f"premium collapsed: live mid {mid} vs director entry "
                                        f"{entry_limit} ({(mid / entry_limit - 1) * 100:.0f}%) — thesis may be stale")
                else:
                    limit, src = mid, "mid"
                    if sap and cap:                        # short call placed as a put: no premium from the director
                        limit, src = cap, cap_src          # to anchor on, so the entry rule's own price is used
                ppind = res.get("ppind")
                if ppind is None:
                    ppind = EX.get_option_contract_meta(sym).get("ppind", False)
                if limit:
                    limit, _tick = EX._round_to_tick(float(limit), ppind, ticker)
                oo = _oo_entry(cfg, gates, warnings, approver=approver, quote=quote, contract=res.get("contract"),
                               sym=sym, vehicle=vehicle, limit=limit, src=src, entry_limit=entry_limit, cap=cap,
                               cap_src=cap_src, sap=sap, ppind=ppind, ticker=ticker, expiry=expiry, today=today)
                if oo and oo.get("work") and oo.get("limit"):  # entry stepping is ON: the order is sized and checked
                    limit, src = oo["limit"], oo["src"]        # at the MOST it may ever pay, sent first at the middle
                unit = (limit or 0) * 100
                qty = int(budget // unit) if unit else 0
                if unit:
                    step4 = _step4(cfg, rec, vehicle=vehicle, ticker=ticker, limit=limit, stop=stop, target=target,
                                   qty=qty, budget=budget, equity=equity, pos=pos, quote=quote, approver=approver,
                                   now=now, expiry=str(expiry)[:10], order_symbol=sym, tier=tier)
                    qty = _step4_qty(step4, qty, sizing)
                sizing.update({"qty": qty, "unit_cost": round(unit, 2), "est_cost": round(qty * unit, 2),
                               "premium_limit": limit, "limit_source": src})
                _gate(gates, "qty", qty >= 1,
                      (f"{qty} contract(s), {_STEP4_BASIS_WORDS.get(sizing.get('step4_basis'), 'sized by risk')}"
                       f" (the flat-dollar rule gave {sizing['qty_flat']} = "
                       f"floor(${budget:,.2f} / (${limit} x 100)))" if "qty_flat" in sizing
                       else f"{qty} contract(s) = floor(${budget:,.2f} / (${limit} x 100))")
                      + ("" if qty >= 1 else f" — one contract costs ${unit:,.2f} > budget; refused"))
                order = {"symbol": sym, "asset": "option", "underlying": ticker, "right": vehicle,
                         "strike": strike, "expiry": str(expiry)[:10], "side": "buy",
                         "position_intent": "buy_to_open", "qty": qty, "type": "limit",
                         "limit_price": limit, "time_in_force": "day", "order_class": "simple",
                         "est_cost": round(qty * unit, 2)}
                if oo and oo.get("work") and oo.get("limit"):  # the first order goes out at the middle price; est_cost
                    order.update(limit_price=oo["work"]["start"], work=oo["work"])   # stays what the limit would cost
                exit_rules = {"stop_premium": stop, "target_premium": target,
                              "enforced_at_broker": False,
                              "note": "Alpaca does not support bracket/OCO orders on options — "
                                      "stop/target premiums are recorded exit RULES, not broker orders"}
                lv_ok, lv = True, []
                if stop is not None and target is not None and limit and not (stop < limit < target):
                    lv_ok = False; lv.append(f"need stop {stop} < limit {limit} < target {target}")
                if mid and stop is not None and mid <= stop:
                    lv_ok = False; lv.append(f"live mid {mid} already at/below stop premium {stop} — call invalidated")
                if mid and target is not None and mid >= target:
                    lv_ok = False; lv.append(f"live mid {mid} already at/above target premium {target}")
                _gate(gates, "levels_valid", lv_ok, "; ".join(lv) or
                      f"stop {stop} < limit {limit} < target {target}; live mid {mid}")
                lvl = acct.get("options_level")
                if lvl is not None:
                    try:
                        _gate(gates, "options_level", int(lvl) >= 2, f"options_trading_level={lvl} (long options need ≥2)")
                    except (TypeError, ValueError):
                        pass
        if sap and order:                                    # exits are judged on the STOCK's price, not the premium
            who = approver                                   # a call that is already placed: whoever placed it
            if execu.get("state") in ("placed", "approved_pending_fill", "closed") and execu.get("approver"):
                who = execu.get("approver")
            exit_rules = _SP().exit_rules(sap["record"], exit_rules, who)
            if who != "auto":                                # by hand: nothing in Mason sells this put
                warnings.append(_SP().MANUAL_WARNING)
        _step4_gates(step4, gates, warnings)                 # Step 4 rules that are ON (nothing when off)
        # ── portfolio risk ──
        est = (order or {}).get("est_cost") or 0.0
        if order:
            tpct = est / equity * 100
            if tier_budget is not None:
                _gate(gates, "max_pct_per_trade", est <= tier_budget + 1e-9,
                      f"trade ${est:,.2f} <= tier budget ${tier_budget:,.2f} ({sizing.get('tier')} tier; "
                      f"{tpct:.2f}% of equity)")
            else:
                _gate(gates, "max_pct_per_trade", tpct <= cap_pct + 1e-9,
                      f"trade ${est:,.2f} = {tpct:.2f}% of equity (cap {cap_pct}%)")
            bp = acct.get("buying_power") or 0.0
            if vehicle in ("call", "put") and acct.get("options_buying_power") is not None:
                bp = acct["options_buying_power"]
            _gate(gates, "buying_power", est <= bp, f"need ${est:,.2f}, have ${bp:,.2f}")
        if pos.get("ok"):
            # DIRECTOR SLEEVE (Nico 2026-09-28): the cap counts only positions the
            # director placed; Nico's own/seeded holdings are not its risk budget.
            cap = float(cfg.get("max_open_exposure_pct") or 0)
            sleeve = _sleeve_exposure(pos, own)                # X6: Mason's own quantity x price
            after = (sleeve + est) / equity * 100
            _gate(gates, "max_open_exposure_pct", after <= cap + 1e-9,
                  f"director sleeve ${sleeve:,.0f} + this ${est:,.0f} = {after:.1f}% of equity (cap {cap}%; "
                  f"whole account ${pos['gross']:,.0f} not counted)")
            if order and cfg.get("no_new_margin", True):
                cash = acct.get("cash") or 0.0
                _gate(gates, "no_new_margin", est <= max(0.0, cash) + 1e-9,
                      f"needs ${est:,.2f} of settled cash; cash is ${cash:,.2f} "
                      f"(director trades never add margin)")
        else:
            _gate(gates, "max_open_exposure_pct", False, f"couldn't read positions: {pos.get('error')}")
        ls = _loss_stop(acct, own, cfg)                        # X5: Mason's own trades, not Nico's book
        _gate(gates, "daily_loss_stop", ls["ok"], ls["detail"])
        ab = _f(cfg.get("account_loss_breaker_pct"))
        if ab:                                                 # X5: the wider whole-account breaker
            dp = acct.get("day_pnl_pct") or 0.0
            _gate(gates, "account_loss_breaker", dp > -ab,
                  f"whole account today {dp:+.2f}% (${acct.get('day_pnl', 0):+,.2f}); new entries stop at -{ab}%")
    elif side_ok and not equity:
        _gate(gates, "sizing", False, "no account equity — cannot size")

    n_today = len(_trades_today(today.isoformat()))
    try:                                         # director sells of Nico-placed stock count too
        import mason_agent_loop as AL
        n_today += AL.book_sells_today(today.isoformat())
    except Exception:
        pass
    mx = int(cfg.get("max_trades_per_day") or 0)
    _gate(gates, "max_trades_per_day", n_today < mx, f"{n_today} placed today (max {mx})")

    # ── market hours: REFUSE outside RTH (see module docstring) ──
    if clk.get("ok"):
        _gate(gates, "market_open", clk["is_open"],
              "market open (RTH)" if clk["is_open"] else
              f"market closed — not queued by design; approve again after {clk.get('next_open')}")
    else:
        _gate(gates, "market_open", False, f"couldn't verify market hours: {clk.get('error')}")

    # ── mason_execution.pretrade_checks (buying power + regime) ──
    regime_feat = {"regime_new_entries": None, "regime_note": None}
    if order and order.get("qty", 0) >= 1:
        try:
            blocks, pw = EX.pretrade_checks(order["symbol"], order["qty"], "buy",
                                            est_price=(sizing.get("unit_cost")), respect_regime=True)
        except Exception as e:
            blocks, pw = [f"pretrade_checks error: {e}"], []
        regime_block = [w for w in pw if w.startswith("regime gate says new entries")]
        pw = [w for w in pw if not w.startswith("market closed")]
        if regime_block:
            m = re.search(r"new entries: (\S+)", regime_block[0])
            regime_feat = {"regime_new_entries": m.group(1) if m else None,
                           "regime_note": regime_block[0]}
        if approver != "nico" and regime_block and cfg.get("respect_regime_veto", True):
            blocks = list(blocks) + [f"(auto) {regime_block[0]}"]
        _gate(gates, "pretrade_checks", not blocks,
              "; ".join(blocks) if blocks else ("ok" + (f" (warnings: {'; '.join(pw)})" if pw else "")))
        warnings.extend(pw)

    blocked = [f"{g['gate']}: {g['detail']}" for g in gates if not g["ok"]]
    res = {"ok": True, "rec_id": rec.get("id"), "ticker": ticker, "direction": direction,
           "vehicle": vehicle, "confidence": conf, "horizon": ex.get("horizon"),
           "slate_id": ex.get("slate_id"), "director_trade": tr, "order": order,
           "exit_rules": exit_rules, "sizing": sizing, "quote": quote,
           "gates": gates, "blocked": blocked, "warnings": warnings,
           "features": _oo_features(_step4_features(step4, regime_feat), oo), "can_place": not blocked,
           "execution": execu or None,
           "summary": _summary(order, blocked)}
    return _short_put_stamp(res, sap) if sap else res


def _summary(order, blocked):
    if not order:
        head = "no order could be built"
    elif order["asset"] == "option":
        head = (f"BUY {order['qty']} {order['underlying']} {order['expiry']} {order['strike']:g}"
                f"{order['right'][0].upper()} ({order['symbol']}) @ limit {order['limit_price']} "
                f"≈ ${order['est_cost']:,.2f}")
        if order.get("work"):                              # a worked order: say what it may climb to
            try:
                head = head.replace(" ≈ $", _OO().summary_words(order) + " ≈ $", 1)
            except Exception:
                pass
    else:
        head = (f"BUY {order['qty']} {order['symbol']} @ limit {order['limit_price']}"
                + (f" bracket stop {order['stop_loss']} / target {order['take_profit']}"
                   if order["order_class"] == "bracket" else "")
                + f" ≈ ${order['est_cost']:,.2f}")
    return head + (" — READY" if not blocked else f" — BLOCKED ({len(blocked)} gate(s))")


# ── public API ───────────────────────────────────────────────────────────────────

def preview(rec_id, *, approver="nico") -> dict:
    """The exact order that WOULD be placed + every risk-gate result. Read-only."""
    try:
        rec = _load_rec(rec_id)
        if not rec:
            return {"ok": False, "rec_id": rec_id, "error": "recommendation not found",
                    "can_place": False, "blocked": ["rec: not found"]}
        return _build(rec, approver=approver)
    except Exception as e:
        return {"ok": False, "rec_id": rec_id, "error": f"preview failed: {e}",
                "can_place": False, "blocked": [f"internal: {e}"]}


def _seed(rec_id):
    return f"dir-{rec_id}"


def _find_existing_order(rec_id, symbol):
    """Alpaca-side idempotency: an order already carrying this rec's client_order_id
    prefix means it was already submitted (e.g. a crash after submit)."""
    code, d = _get("/orders", params={"status": "all", "symbols": symbol,
                                      "limit": 200, "nested": "true"})
    if code == 200 and isinstance(d, list):
        pre = f"mason-{_seed(rec_id)}-"
        for o in d:
            if str(o.get("client_order_id") or "").startswith(pre):
                return o
    return None


def _notify(title, body, *, ticker=None, rec_id=None, severity="info", dedup=None):
    try:
        import mason_notifications as MN
        MN.add_notification("system", title[:200], body=body, severity=severity,
                            ticker=ticker, ref_table="recommendations", ref_id=rec_id,
                            created_by="autotrade", dedup_key=dedup)
    except Exception:
        pass


def _activity(kind, title, *, approver, ticker, rec_id, extra, body=None):
    try:
        import mason_activity as MA
        MA.log_activity(kind, title[:200], body=body, actor="nico" if approver == "nico" else "autotrade",
                        category="analyst", ticker=ticker,
                        trigger="chat" if approver == "nico" else "autonomous",
                        ref_table="recommendations", ref_id=rec_id, extra=extra)
    except Exception:
        pass


def _journal_notes(p, approver):
    er = p.get("exit_rules") or {}
    if p["vehicle"] in ("call", "put"):
        rules = (f"Exit rules (NOT broker-enforced — Alpaca has no option brackets): "
                 f"stop premium {er.get('stop_premium')}, target premium {er.get('target_premium')}.")
    else:
        rules = (f"Exits: stop {er.get('stop')} / target {er.get('target')} "
                 f"({'bracket at Alpaca' if er.get('enforced_at_broker') else 'rules only'}).")
    if p.get("short_as_put"):                              # short call placed as a put: the exits are on the stock's price
        try:
            rules = _SP().journal_words(p)
        except Exception:
            pass
    return (f"Opus director call — rec {p['rec_id']} (slate {p.get('slate_id')}), "
            f"conf {p.get('confidence')}, horizon {p.get('horizon')}. Placed via autotrade, "
            f"approver={approver}. {rules}")


def _link(approver, p):
    return {"director_rec_id": p["rec_id"], "director_slate_id": p.get("slate_id"),
            "autotrade_approver": approver}


def _journal_stock(p, order_id, filled_qty, filled_px, approver):
    """Journal a verified stock fill (or adopt a row reconcile already wrote). Holds mason_execution's books lock,
    so the regular reconcile cannot write its own row for the same order between the check and the write.
    Waits at most 30 s (review fix): this runs on the desk worker before the exit manager's step, and the regular
    reconcile gives a buy 60 minutes before it writes its own row (buy_grace_s), so journaling without the lock
    after 30 s cannot make a second row for a fresh fill."""
    import contextlib
    lock = getattr(EX, "books_lock", None)
    with (lock(timeout=30) if lock else contextlib.nullcontext(True)) as held:
        if not held:
            import logging
            logging.getLogger("mason.autotrade").warning(
                "books lock busy for 30 s; journaling order %s without it", order_id)
        return _journal_stock_locked(p, order_id, filled_qty, filled_px, approver)


def _journal_stock_locked(p, order_id, filled_qty, filled_px, approver):
    import mason_trades as MT
    for t in MT.get_all_trades():
        if t.get("entry_order_id") == order_id:
            t.update(_link(approver, p))
            MT._save_journal([t])
            return t
    tr = p.get("director_trade") or {}
    t = MT.log_trade({
        "ticker": p["ticker"], "direction": "long", "entry_price": filled_px,
        "stop_loss": tr.get("stop") or 0, "target_price": tr.get("target") or 0,
        "shares": filled_qty, "setup_type": "director", "time_horizon": p.get("horizon") or "swing",
        "thesis": ((_load_rec(p["rec_id"]) or {}).get("extra", {}).get("prediction") or "")[:2000],
        "mason_confidence": int(round((p.get("confidence") or 0.5) * 100)),
        "notes": _journal_notes(p, approver), "entry_order_id": order_id,
        "created_by": "nico" if approver == "nico" else "mason"})
    t.update(_link(approver, p))
    MT._save_journal([t])
    return t


def _journal_option_backfill(p, order, approver):
    """Journal a resting option order that filled later (settle_pending)."""
    import mason_options as MO
    # (gap fixes, 2026-11-18) who the row belongs to follows who approved the call, the same as for an option that
    # fills at once (_place_claimed) and for stock rows: before, a call Nico approved by hand that filled LATER was
    # journaled as Mason's.
    owner = {"created_by": "nico" if approver == "nico" else "mason"}
    existing = MO.find_play_by_order_id(order.get("id"))
    if existing:
        return MO.update_play(existing["id"], {**_link(approver, p), **owner})
    o = p["order"]
    second = _journal_option_second_row(MO, p, order, approver, owner)
    if second is not None:
        return second
    # Books fix 2026-11-15 (item D): the cost and size come from the fill, written with the row — the same as
    # place_option_play does. Before, this path left premium/contracts empty (SMCI row d1dbf9a6) and relied on
    # attach_execution filling them in, which it only does since 2026-11-13.
    _fq = float(order.get("filled_qty") or 0)
    play = MO.log_verified_play(
        {"ticker": p["ticker"], "strategy": f"long_{o['right']}",
         "direction": "bullish" if o["right"] == "call" else "bearish",
         "expiration": o["expiry"], "strikes": {f"long_{o['right']}": o["strike"]},
         "premium": float(order["filled_avg_price"]) if order.get("filled_avg_price") else None,
         "contracts": int(_fq) if _fq > 0 else None,
         "premium_type": "debit",
         "thesis": ((_load_rec(p["rec_id"]) or {}).get("extra", {}).get("prediction") or "")[:2000],
         "mason_confidence": int(round((p.get("confidence") or 0.5) * 100)),
         "notes": _journal_notes(p, approver)},
        order_id=order.get("id"), client_order_id=order.get("client_order_id"),
        contract_symbol=(order.get("symbol") or "").upper(),
        filled_qty=float(order.get("filled_qty") or 0),
        filled_avg_price=float(order["filled_avg_price"]) if order.get("filled_avg_price") else None,
        side="buy", filled_at=order.get("filled_at"), position_intent="buy_to_open",
        today=_now_et().date())                            # Mason's own date (was the system clock's)
    if isinstance(play, dict) and play.get("id"):
        play = MO.update_play(play["id"], {**_link(approver, p), **owner}) or play
    return play


def _journal_option_second_row(MO, p, order, approver, owner):
    """(gap fixes, 2026-11-18) A director call's option order filled LATER, and the options journal already has an
    OPEN row on the same contract that belongs to ANOTHER order (an earlier Mason call on that contract, or contracts
    Nico holds). The later-fill journal path (log_verified_play) refuses a second open row on one contract, so this
    call's fill was never booked: it stayed 'approved_pending_fill' for ever, with no trade record, no stop and no
    exits, and the refusal was repeated at every look. An order that fills AT ONCE has always been journaled as its
    own row in this situation (place_option_play: log_play + attach_execution); this writes the later fill the very
    same way, so each call keeps its own row, tied to its own order.
    Returns the row, or None when there is no such other open row (the caller then takes the normal path, unchanged).
    Only when the broker really holds the contract and the order is a buy that filled; otherwise the journal's own
    refusal is raised as before and nothing is written."""
    sym = str(order.get("symbol") or "").upper()
    oid = order.get("id")
    fq = _f(order.get("filled_qty")) or 0.0
    px = _f(order.get("filled_avg_price"))
    if not sym or not oid:
        return None
    try:
        other = MO.find_open_play_by_contract(sym)
    except Exception:
        return None
    if not other or other.get("order_id") == oid:
        return None
    if str(order.get("side") or "buy").lower() != "buy" or fq <= 0 or not px:
        return None                                        # not a filled buy: the normal path refuses it, as before
    pos = EX.get_position(sym)                             # the same question log_verified_play asks: is it held?
    if not (isinstance(pos, dict) and pos.get("ok") and pos.get("exists")
            and (_f(pos.get("qty")) or 0.0) >= fq - 1e-9):
        return None                                        # not held (or fewer than this fill) / broker unreadable:
                                                           # refused by the journal as before
    o = p["order"]
    play = MO.log_play(
        {"ticker": p["ticker"], "strategy": f"long_{o['right']}",
         "direction": "bullish" if o["right"] == "call" else "bearish",
         "expiration": o["expiry"], "strikes": {f"long_{o['right']}": o["strike"]},
         "premium": px, "contracts": int(fq), "premium_type": "debit",
         "underlying_price_at_entry": None, "iv_at_entry": None,
         "thesis": ((_load_rec(p["rec_id"]) or {}).get("extra", {}).get("prediction") or "")[:2000],
         "mason_confidence": int(round((p.get("confidence") or 0.5) * 100)),
         "max_profit": None, "max_loss": None, "breakeven": None,
         "notes": _journal_notes(p, approver)},
        today=_now_et().date())
    play = MO.attach_execution(play["id"], order_id=oid, client_order_id=order.get("client_order_id"),
                               contract_symbol=sym, filled_qty=fq, filled_avg_price=px,
                               filled_at=order.get("filled_at")) or play
    return MO.update_play(play["id"], {**_link(approver, p), **owner}) or play


def _free_parked_cash(rec_id, approver, p):
    """Idle-cash rule (mason_idle_cash, 2026-11-16). An order refused ONLY because cash is short ('no_new_margin'
    is the one failed gate) may be paid for out of the index fund the rule parked spare cash in: the rule sells
    enough of it (a limit order, with its own time limit) and the order is checked again from the start. Anything
    else (the rule off or in record-only mode, another gate failed, nothing parked, the sale not filled in time)
    returns the preview it was given, unchanged, and the order stays refused exactly as before. Never raises.
    (review fix) An option order that is also short of the broker's option buying power ('buying_power' failed
    beside 'no_new_margin') is treated the same way: cash is short, the sale is what the next pass would do anyway,
    and the order is checked again from the start. When another trade's sale has just covered this one ("covered"),
    the order is checked again instead of being refused on its stale preview."""
    try:
        failed = {g["gate"] for g in (p.get("gates") or []) if not g.get("ok")}
        if "no_new_margin" not in failed or not failed <= {"no_new_margin", "buying_power"}:
            return p
        ic = _cfg().get("idle_cash")
        if not isinstance(ic, dict) or ic.get("enabled") is not True:
            return p
        import mason_idle_cash as IC
        if not IC.live():
            return p
        need = _f((p.get("order") or {}).get("est_cost"))
        if not need or need <= 0:
            return p
        res = IC.free_cash(need, why=f"director call {rec_id} ({p.get('ticker')})")
        if not (res.get("ok") or res.get("covered")):
            return p
        p2 = preview(rec_id, approver=approver)
        return p2 if p2.get("ok") else p
    except Exception:
        return p


def approve(rec_id, *, approver="nico", dry_run=False) -> dict:
    """Gates → place → verify → journal → record → activity + notification.
    dry_run=True runs every gate and returns the preview without placing."""
    p, mine = None, False                                  # (gap fixes) what the error handler below needs to know
    try:
        p = preview(rec_id, approver=approver)
        if not p.get("ok"):
            return {"ok": False, "placed": False, "rec_id": rec_id, "error": p.get("error"),
                    "preview": p}
        if dry_run:
            return {"ok": True, "placed": False, "dry_run": True, "rec_id": rec_id,
                    "would_place": p["can_place"], "summary": p["summary"], "preview": p}
        if not p["can_place"]:
            p = _free_parked_cash(rec_id, approver, p)     # idle-cash rule; returns p itself unless it is switched on
        if not p["can_place"]:
            _short_put_shadow(rec_id, p, approver)             # record-only: the put a refused short call would have bought
            return {"ok": False, "placed": False, "rec_id": rec_id, "blocked": p["blocked"],
                    "error": "blocked by risk gates: " + " | ".join(p["blocked"]), "preview": p}
        claimed, cur = _claim(rec_id, approver)
        if not claimed:
            return {"ok": False, "placed": False, "rec_id": rec_id, "idempotent": True,
                    "error": f"already {cur.get('state')} — not placing twice", "execution": cur}
        mine = True                                        # from here on an order of this call may be at the broker
        return _place_claimed(p, approver)
    except Exception as e:
        kept = None
        try:                                               # (gap fixes, 2026-11-18) never "failed" over an order that
            kept = _after_approve_error(rec_id, approver, e, p, mine)                                # went out
        except Exception:
            kept = None
        if kept is not None:
            return kept
        try:
            _set_execution(rec_id, {"state": "failed", "error": f"internal: {e}"})
        except Exception:
            pass
        return {"ok": False, "placed": False, "rec_id": rec_id, "error": f"approve failed: {e}"}


def _after_approve_error(rec_id, approver, err, p, mine):
    """(gap fixes, 2026-11-18) approve() hit an error. Before, the call was marked 'failed' whatever had happened, also
    when its order had already gone out: the order was then live at the broker with nothing in Mason's records (no
    fill follow-up, no trade record, no stop), and an order already written down as placed was overwritten by
    'failed'. Now, read-only at the broker (one order-list read, only when this approve had claimed the call):
        the call already shows an order (placed / waiting for its fill / closed)  -> its state is kept, the error noted
        this approve claimed the call and the broker HAS an order of this call    -> that order is taken into the
                                                 records (as _place_claimed does when it finds one); a fill is
                                                 journaled by settle_pending
        this approve claimed the call and the broker lists NO order of this call  -> None: 'failed', as before
        this approve claimed the call and the broker's list cannot be read        -> the call stays 'placing' (not
                                                 'failed': nobody knows), with the error noted, and Nico is told
        the error came before this approve claimed the call                       -> None ('failed' as before), unless
                                                 another approve of this call is at work: then nothing is touched
    Returns the result for approve() to return, or None for today's behaviour. Never places or cancels anything."""
    msg = f"internal: {type(err).__name__}: {err}"[:300]
    cur = ((_load_rec(rec_id) or {}).get("extra") or {}).get("execution") or {}
    st = cur.get("state")
    if st in ("placed", "approved_pending_fill", "closed") and (cur.get("order_id") or st == "closed"):
        ex = _set_execution(rec_id, {"last_error": msg})
        _log_warning("approve(%s) hit an error after its order %s was recorded (state %s kept): %s",
                     rec_id, cur.get("order_id"), st, msg)
        return {"ok": True, "placed": st != "closed", "state": st, "rec_id": rec_id, "execution": ex or cur,
                "warning": f"an error came up after the order was recorded: {msg}", "error": None}
    if not mine:
        if st == "placing":                                # another approve of this call is at work: hands off
            return {"ok": False, "placed": False, "rec_id": rec_id, "error": f"approve failed: {err}"}
        return None
    sym = str(((p or {}).get("order") or {}).get("symbol") or "")
    code, d = _get("/orders", params={"status": "all", "symbols": sym, "limit": 200, "nested": "true"}) if sym \
        else (None, None)
    if code != 200 or not isinstance(d, list):
        ex = _set_execution(rec_id, {"last_error": msg, "error": "Mason hit an error while sending this order and "
                                     "could not ask the broker whether it went out."})
        _log_warning("approve(%s) hit an error and the broker's order list could not be read; the call stays "
                     "'placing': %s", rec_id, msg)
        _notify(f"Please check: {(p or {}).get('ticker') or rec_id} — Mason could not confirm an order",
                f"Director rec {rec_id}. Mason hit an error while sending this order and could not ask the broker "
                "whether it went out. Nothing more is sent for this call. Please look at the account's open orders.",
                ticker=(p or {}).get("ticker"), rec_id=rec_id, severity="warning",
                dedup=f"autotrade:{rec_id}:send_unknown")
        return {"ok": False, "placed": False, "state": "placing", "rec_id": rec_id, "execution": ex or cur,
                "error": f"approve failed and the order could not be confirmed: {err}"}
    pre = f"mason-{_seed(rec_id)}-"
    own = [o for o in d if str(o.get("client_order_id") or "").startswith(pre)]
    if not own:
        return None                                        # nothing went out: 'failed', as before
    dead = ("rejected", "canceled", "cancelled", "expired", "done_for_day", "replaced")
    live = [o for o in own if (_f(o.get("filled_qty")) or 0) > 0 or str(o.get("status") or "").lower() not in dead]
    if not live:
        return None                                        # only orders that ended without a fill: 'failed', as before
    o = live[0]
    ost = str(o.get("status") or "").lower()
    order = (p or {}).get("order") or {}
    fields = {"approver": approver, "mode": "paper", "symbol": sym, "asset": order.get("asset"),
              "limit_price": order.get("limit_price"), "exit_rules": (p or {}).get("exit_rules"),
              "order_preview": (p or {}).get("summary"), "features": (p or {}).get("features"),
              "state": "approved_pending_fill", "order_id": o.get("id"),
              "client_order_id": o.get("client_order_id"),
              "qty": _f(o.get("filled_qty")) if ost in ("filled", "partially_filled") else _f(o.get("qty")),
              "price": _f(o.get("filled_avg_price")), "fill_status": ost or None,
              "placed_at": o.get("submitted_at") or datetime.now().isoformat(timespec="seconds"),
              "journal_table": "options_trades" if order.get("asset") == "option" else "trades",
              "adopted_existing_order": True, "error": None, "last_error": msg}
    if (p or {}).get("short_as_put"):
        fields["short_as_put"] = p["short_as_put"]
    fields = {k: v for k, v in fields.items() if v is not None or k in ("error",)}
    ex = _set_execution(rec_id, fields)
    _log_warning("approve(%s) hit an error after its order %s went out; the order was taken into the records: %s",
                 rec_id, o.get("id"), msg)
    try:                                                   # a fill is journaled and the call marked 'placed' from
        settle_pending([rec_id])                           # Alpaca's real order, exactly like any resting order
    except Exception:
        pass
    ex = ((_load_rec(rec_id) or {}).get("extra") or {}).get("execution") or ex or fields
    return {"ok": True, "placed": True, "adopted": True, "state": ex.get("state"), "rec_id": rec_id,
            "execution": ex, "summary": (p or {}).get("summary"), "error": None,
            "warning": f"an error came up after the order went out; the order is in the records: {msg}"}


def _log_warning(fmt, *args):
    try:
        import logging
        logging.getLogger("mason.autotrade").warning("[autotrade] " + fmt, *args)
    except Exception:
        pass


def _place_claimed(p, approver):
    rec_id, order = p["rec_id"], p["order"]
    now_iso = datetime.now().isoformat(timespec="seconds")
    base_exec = {"approver": approver, "mode": "paper", "symbol": order["symbol"],
                 "asset": order["asset"], "limit_price": order["limit_price"],
                 "exit_rules": p.get("exit_rules"), "order_preview": p["summary"],
                 "features": p.get("features")}
    try:                                         # the quote this order was priced from (cost grading later)
        if _at_cfg().get("record_quotes", True) and p.get("quote"):
            base_exec["entry_quote"] = _MQ().compact(p["quote"], limit=order["limit_price"])
    except Exception:
        pass

    if p.get("short_as_put"):                              # short call placed as a put
        if order.get("side") != "buy" or order.get("asset") != "option" or order.get("right") != "put" \
                or order.get("position_intent") != "buy_to_open":
            ex = _set_execution(rec_id, {"state": "failed", "error": "short call placed as a put: the order built "
                                                                     "was not a put purchase; nothing was sent"})
            return {"ok": False, "placed": False, "state": "failed", "rec_id": rec_id, "execution": ex,
                    "error": ex.get("error") if ex else "not a put purchase"}
        base_exec["short_as_put"] = p["short_as_put"]
        # the chosen contract is on the call BEFORE any order goes out: a retry after a crash reuses it
        _set_execution(rec_id, {"short_as_put": p["short_as_put"]})
        other = _SP().other_order(rec_id, order["symbol"])
        if other:                                          # never the same call on two contracts
            err = (f"an order of this call already exists on another contract ({other.get('symbol')}, order "
                   f"{other.get('id')}); no second order was sent")
            ex = _set_execution(rec_id, {**base_exec, "state": "failed", "error": err})
            _notify(f"Not placed: {p['ticker']} short call (as a put)", f"Director rec {rec_id}. {err}. Please look "
                    "at the account.", ticker=p["ticker"], rec_id=rec_id, severity="warning",
                    dedup=f"autotrade:{rec_id}:other_contract")
            return {"ok": False, "placed": False, "state": "failed", "rec_id": rec_id, "execution": ex, "error": err}

    existing = _find_existing_order(rec_id, order["symbol"])
    if existing:
        st = (existing.get("status") or "").lower()
        filled = st in ("filled", "partially_filled")
        ex = _set_execution(rec_id, {**base_exec, "state": "placed" if st == "filled" else
                                     ("failed" if st in ("rejected", "canceled", "cancelled", "expired")
                                      else "approved_pending_fill"),
                                     "order_id": existing.get("id"),
                                     "client_order_id": existing.get("client_order_id"),
                                     "qty": _f(existing.get("filled_qty")) if filled else _f(existing.get("qty")),
                                     "price": _f(existing.get("filled_avg_price")),
                                     "placed_at": existing.get("submitted_at") or now_iso,
                                     "adopted_existing_order": True})
        if st == "filled":
            settle_pending([rec_id], _force_journal=True)
        return {"ok": True, "placed": False, "adopted": True, "rec_id": rec_id,
                "execution": ex, "note": "an order for this rec already existed at Alpaca — adopted, not re-placed"}

    journal = None
    if order["asset"] == "option":
        res = EX.place_option_play(
            ticker=p["ticker"], strategy=f"long_{order['right']}",
            direction="bullish" if order["right"] == "call" else "bearish",
            expiration=order["expiry"], strikes={f"long_{order['right']}": order["strike"]},
            contract_symbol=order["symbol"], qty=order["qty"], side="buy",
            entry=order["limit_price"], premium_type="debit",
            thesis=((_load_rec(rec_id) or {}).get("extra", {}).get("prediction") or "")[:2000],
            mason_confidence=int(round((p.get("confidence") or 0.5) * 100)),
            pitch_id=_seed(rec_id), respect_regime=True, notes=_journal_notes(p, approver),
            today=_now_et().date())                        # Mason's own date for the journal's expiry check
        exe = res.get("execution") or {}
        fill = exe.get("fill") or {}
        if res.get("ok"):
            journal = res.get("journal")
            try:
                import mason_options as MO
                if journal and journal.get("id"):
                    journal = MO.update_play(journal["id"], {**_link(approver, p),
                                             "created_by": "nico" if approver == "nico" else "mason"}) or journal
            except Exception:
                pass
    else:
        exe = EX.place_trade(symbol=order["symbol"], qty=order["qty"], side="buy",
                             entry=order["limit_price"], stop=order.get("stop_loss"),
                             target=order.get("take_profit"), setup="director",
                             pitch_id=_seed(rec_id), order_type="limit",
                             tif=order["time_in_force"], respect_regime=True, verify=True)
        res = exe
        fill = exe.get("fill") or {}
        if exe.get("ok") and (fill.get("status") or "") in ("filled", "partially_filled") \
                and fill.get("filled_avg_price"):
            try:
                journal = _journal_stock(p, exe.get("order_id"), fill.get("filled_qty"),
                                         fill.get("filled_avg_price"), approver)
            except Exception as e:
                journal = {"error": f"journal write failed (fill is real; settle_pending/reconcile "
                                    f"will pick it up): {e}"}

    order_id = exe.get("order_id")
    fstatus = (fill.get("status") or "").lower()
    if not order_id:
        state = "failed"
    elif fstatus in ("filled", "partially_filled") and (journal and not journal.get("error")):
        state = "placed"
    elif fstatus in ("rejected", "canceled", "cancelled", "expired"):
        state = "failed"
    else:
        state = "approved_pending_fill"
    execution = {**base_exec, "state": state, "order_id": order_id,
                 "client_order_id": exe.get("client_order_id"),
                 "qty": fill.get("filled_qty") if state == "placed" else order["qty"],
                 "price": fill.get("filled_avg_price"),
                 "fill_status": fstatus or None, "placed_at": now_iso if order_id else None,
                 "journal_id": (journal or {}).get("id") if isinstance(journal, dict) else None,
                 "journal_table": "options_trades" if order["asset"] == "option" else "trades",
                 "error": None if state != "failed" else (res.get("error") or exe.get("error")),
                 "warnings": exe.get("warnings") or []}
    if order["asset"] == "option" and isinstance(execution.get("features"), dict) \
            and "option_entry" in execution["features"]:   # option order handling: records and the schedule's state
        try:
            execution = _OO().placed(execution, p, order, exe, fill, state)
        except Exception:
            pass
    _set_execution(rec_id, execution)

    who = "Nico" if approver == "nico" else "auto-place"
    if state == "failed":
        title = f"Autotrade FAILED ({who}): {p['summary'].split(' — ')[0]}"
        body = (f"Director rec {rec_id}. Nothing filled. Error: {execution['error']}.\n"
                f"The call is still being graded.")
        sev = "warning"
    elif state == "placed":
        title = (f"Placed (paper, {who}): BUY {execution['qty']:g} {order['symbol']} "
                 f"@ {execution['price']} — director rec {rec_id}")
        body = (f"{p['summary']}\nFilled {execution['qty']} @ {execution['price']} "
                f"(Alpaca order {order_id}). Journaled ({execution['journal_table']} "
                f"{execution['journal_id']}).\n"
                + (p["exit_rules"] or {}).get("note", ""))
        sev = "info"
    else:
        title = (f"Order resting (paper, {who}): BUY {order['qty']} {order['symbol']} "
                 f"@ {order['limit_price']} — director rec {rec_id}")
        body = (f"{p['summary']}\nSubmitted (Alpaca order {order_id}), status {fstatus or 'submitted'} — "
                f"not yet filled. It will be journaled from Alpaca's real fill by settle_pending(); "
                f"a day order that never fills expires at the close.\n"
                + (p["exit_rules"] or {}).get("note", ""))
        if order.get("work") and execution.get("work"):    # a worked order: say how the price will be raised
            try:
                title, body = _OO().working_note(p, order, order_id, who, rec_id, title, body)
            except Exception:
                pass
        sev = "info"
    _notify(title, body, ticker=p["ticker"], rec_id=rec_id, severity=sev,
            dedup=f"autotrade:{rec_id}:{state}")
    _activity(f"autotrade_{state}", title, approver=approver, ticker=p["ticker"], rec_id=rec_id,
              extra={"order_id": order_id, "state": state, "summary": p["summary"]})
    return {"ok": state != "failed", "placed": state in ("placed", "approved_pending_fill"),
            "state": state, "rec_id": rec_id, "execution": execution, "journal": journal,
            "summary": p["summary"], "warnings": p.get("warnings"),
            "error": execution["error"]}


_GATE_WORDS = {
    "approve_enabled": "automatic placing is switched off",
    "paper_trading_enabled": "paper trading is switched off",
    "paper_only": "the broker connection is not the paper account",
    "is_director_rec": "it is not a director call",
    "directional": "the call has no direction to trade",
    "status_open": "the call is no longer open",
    "not_already_placed": "it was already placed, closed or declined",
    "fresh": "the call is too old",
    "single_leg_buy": "it is not a simple buy of a stock, a call or a put",
    "account": "the paper account could not be read or is not tradable",
    "tier_budget": "no size is set for this kind of call",
    "price": "there was no reliable live price",
    "qty": "the budget does not cover one share or one contract at this price",
    "levels_valid": "the price is already past the stop or the target",
    "expiry": "the option's expiry date is not allowed (expired, too late in the day, or same-day not allowed)",
    "contract_resolved": "the broker does not list this option contract",
    "option_liquidity": "the option's buy and sell prices are missing or too far apart",
    "options_level": "the account is not approved for buying options",
    "max_pct_per_trade": "the trade is larger than the size limit",
    "buying_power": "there is not enough buying power",
    "max_open_exposure_pct": "Mason's open positions are already at their limit",
    "no_new_margin": "there is not enough cash (Mason never borrows)",
    "daily_loss_stop": "Mason's own trades are down too much today",
    "account_loss_breaker": "the whole account is down too much today",
    "sizing": "the account value could not be read",
    "max_trades_per_day": "today's trade limit is reached",
    "market_open": "the market is closed or its hours could not be checked",
    "pretrade_checks": "a last check before sending the order failed",
    # Step 4 (mason_sizing.GATE_WORDS holds the same lines)
    "risk_size": "the amount Mason may lose on one trade does not cover one share or one contract",
    "symbol_cap": "the account already holds as much of this stock as the limit allows",
    "sector_cap": "the account already holds as much of this sector as the limit allows",
    "earnings_window": "the company reports earnings too soon",
    "exit_plan": "the call has no stop or no target",
    "reward_risk": "the possible gain is too small next to the possible loss",
    "expected_value": "the trade is worth too little on average to be worth placing",
    "priced_in": "the price has already made an unusually large move in this direction",
    # Step 4b ({pct} is filled in from autotrade.limits.max_loss.max_pct by plain_block_reasons)
    "spend_limit": "one contract costs more than Mason may spend on one option trade",
    "max_loss": "this trade could lose more than {pct}% of the account",
    # short calls placed as puts (the gate's own detail is used when there is one: see plain_block_reasons)
    "short_as_put": "no suitable put could be found for this short call",
    "short_put_wait": "a put for this short call could not be chosen or priced right now",
    # option order handling (mason_option_orders.GATE_WORDS holds the same lines; each exists only while its rule is on)
    "opt_two_sided": "there is no real buyer and seller for this option right now",
    "opt_spread": "the option's buy and sell prices are too far apart",
    "opt_open_interest": "too few contracts of this option are open, so it would be hard to sell",
    "opt_quote_age": "the option's price is too old to trust",
    "opt_delta": "this option would move too little with the stock, or almost exactly like the stock",
}
_SHORT_PUT_GATES = ("short_as_put", "short_put_wait")
_BLOCK_RECORDABLE = (None, "", "failed", "unfilled", "queued", "blocked")   # never overwrite a live/closed call


def plain_block_reasons(blocked) -> list:
    """Gate lines ('gate: detail') or gate names → short plain-words reasons, in order, no repeats."""
    out = []
    for b in blocked or []:
        g = str(b.get("gate") if isinstance(b, dict) else b).split(":", 1)[0].strip()
        w = _GATE_WORDS.get(g) or "a safety check failed"
        if g in _SHORT_PUT_GATES:                            # these two say their reason in plain words themselves
            d = (b.get("detail") if isinstance(b, dict) else (str(b).split(":", 1)[1] if ":" in str(b) else "")) or ""
            if str(d).strip():
                w = str(d).strip()[:300]
        if "{pct}" in w:                                     # Step 4b: the loss ceiling names its own percent
            try:
                import mason_sizing as SZ
                w = w.replace("{pct}", f"{float(SZ.scfg(_at_cfg())['limits']['max_loss']['max_pct']):g}")
            except Exception:
                w = w.replace("{pct}% of the account", "the most allowed on one trade")
        if w not in out:
            out.append(w)
    return out


def _what(rec) -> str:
    tr = ((rec or {}).get("extra") or {}).get("trade") or {}
    v = str(tr.get("vehicle") or "").lower()
    t = str((rec or {}).get("ticker") or "").upper()
    if v in ("call", "put"):
        k = _f(tr.get("strike"))
        return f"{t} {('%g' % k) if k is not None else ''} {v} {str(tr.get('expiry') or '')[:10]}".replace("  ", " ").strip()
    try:
        if v == "stock" and _SP().enabled(_at_cfg()) and _SP().is_short_stock(rec):
            return f"{t} short call (as a put)"
    except Exception:
        pass
    return f"{t} shares" if v == "stock" else t


def _record_blocked(rec_id, res, *, state="blocked", note=True):
    """X4: an AUTOMATIC placement the gates refused is written on the call (state + the gate list
    with their details) and raises ONE plain-words inbox note. Never touches a call that is live,
    closed, declined or cancelled. Returns the stored execution block, or None when nothing was done."""
    try:
        if not _at_cfg().get("persist_blocked", True):
            return None
        blocked = [str(b) for b in (res.get("blocked") or [])]
        rec = _load_rec(rec_id)
        if not blocked or not rec:
            return None
        cur = ((rec.get("extra") or {}).get("execution") or {}).get("state")
        if cur not in _BLOCK_RECORDABLE:
            return None
        gates = [{"gate": b.split(":", 1)[0].strip(), "detail": (b.split(":", 1)[1].strip() if ":" in b else "")[:400]}
                 for b in blocked]
        words = plain_block_reasons(gates)
        fields = {"state": state, "blocked_at": datetime.now().isoformat(timespec="seconds"),
                  "blocked_gates": gates, "blocked_reason": "; ".join(words)}
        feats = (res.get("preview") or {}).get("features")
        if isinstance(feats, dict) and "sizing_plan" in feats:   # Step 4: what the rules said, kept on the call
            fields["features"] = feats
        ex = _set_execution(rec_id, fields)
        if note:
            what = _what(rec)
            title = f"Not placed: {what} — {words[0]}"
            body = (f"Mason did not place this trade. Why: {'; '.join(words)}.\n"
                    "Nothing was bought. The call is still tracked and graded, and you can still place it "
                    "yourself from the inbox.")
            try:                                             # (review) a short call placed as a put: by hand only with
                if what.endswith("short call (as a put)") and _SP().scfg(_at_cfg()).get("manual") is not True:
                    body = (f"Mason did not place this trade. Why: {'; '.join(words)}.\n"   # short_as_put.manual on
                            "Nothing was bought. The call is still tracked and graded. A short call on a stock is "
                            "bought as a put only by Mason's automatic placing; approving it by hand from the inbox "
                            "is switched off.")
            except Exception:
                pass
            _notify(title, body, ticker=rec.get("ticker"), rec_id=rec_id, severity="warning",
                    dedup=f"autotrade:{rec_id}:blocked")
            _activity("autotrade_blocked", title, approver="auto", ticker=rec.get("ticker"), rec_id=rec_id,
                      extra={"gates": [g["gate"] for g in gates]}, body=body)
        return ex
    except Exception:
        return None


_PERSIST_SKIPPED_DEFAULT = True     # suites that compare with recordings made before 2026-11-19 set this False


def _record_skipped(rec_id, rec, conf, p_cal, segment, floor, reason):
    """(2026-11-19) A call the automatic placing SKIPPED because its adjusted confidence is under the
    learning floor is written on the call and raises one plain-words inbox note. Before, the only trace
    was a line in the review's activity record (DIS long on 11-17: nobody could see why it was not
    bought). The call's `state` is NOT touched (it stays as it was, so it can still be approved by hand
    and is graded as before); only skip_* fields and the confidence figures are added. Switch:
    autotrade.persist_skipped (on unless set false). Never raises."""
    try:
        if _at_cfg().get("persist_skipped", _PERSIST_SKIPPED_DEFAULT) is False:
            return None
        ex = _set_execution(rec_id, {"skipped_at": datetime.now().isoformat(timespec="seconds"),
                                     "skip_reason": reason, "skip_floor": floor,
                                     "p_raw": conf, "p_cal": p_cal, "cal_segment": segment})
        what = _what(rec)
        pc = ("%.3f" % p_cal) if isinstance(p_cal, (int, float)) else "unknown"
        title = f"Not placed: {what} — confidence a bit too low"
        body = (f"Mason skipped this one. After adjusting for its own track record, the confidence came out at "
                f"{pc}, and it needs {floor} for even a small learning-size trade.\n"
                "Nothing was bought. The call is still tracked and graded, and you can still place it "
                "yourself from the inbox.")
        _notify(title, body, ticker=rec.get("ticker"), rec_id=rec_id, severity="info",
                dedup=f"autotrade:{rec_id}:skipped")
        _activity("autotrade_skipped", title, approver="auto", ticker=rec.get("ticker"), rec_id=rec_id,
                  extra={"p_raw": conf, "p_cal": p_cal, "floor": floor}, body=body)
        return ex
    except Exception:
        return None


def reject(rec_id, note="", *, by="nico") -> dict:
    """Decline EXECUTION only. status stays 'open' so the prediction keeps grading."""
    try:
        rec = _load_rec(rec_id)
        if not rec:
            return {"ok": False, "rec_id": rec_id, "error": "recommendation not found"}
        st = ((rec.get("extra") or {}).get("execution") or {}).get("state")
        if st in _ACTIVE_STATES:
            return {"ok": False, "rec_id": rec_id,
                    "error": f"already {st} — close the position instead of rejecting"}
        ex = _set_execution(rec_id, {"state": "rejected_by_nico", "rejected_at":
                                     datetime.now().isoformat(timespec="seconds"),
                                     "rejected_by": by, "note": str(note or "")[:500]})
        _activity("autotrade_rejected", f"Rejected director call {rec.get('ticker')} "
                  f"{rec.get('direction')} (rec {rec_id})", approver="nico", ticker=rec.get("ticker"),
                  rec_id=rec_id, extra={"note": note})
        return {"ok": True, "rec_id": rec_id, "execution": ex,
                "note": "execution declined; the prediction is still graded"}
    except Exception as e:
        return {"ok": False, "rec_id": rec_id, "error": f"reject failed: {e}"}


def maybe_autoplace(rec_ids) -> list:
    """No-op unless autotrade.auto_place is true; then approve(approver='auto') only
    for recs with confidence ≥ autotrade.min_confidence_auto. Returns one dict per rec."""
    out = []
    try:
        cfg = _at_cfg()
        ids = [r for r in (rec_ids or []) if r]
        if not cfg.get("auto_place"):
            return [{"rec_id": r, "placed": False, "reason": "autotrade.auto_place is off"} for r in ids]
        floor = float(cfg.get("min_confidence_auto") or 1.0)
        for r in ids:
            rec = _load_rec(r)
            if not rec:
                out.append({"rec_id": r, "placed": False, "reason": "not found"})
                continue
            conf = _f((rec.get("extra") or {}).get("confidence"))
            if cfg.get("tiers_enabled"):
                res = _autoplace_tiered(r, rec, conf, cfg)
                out.append(res)
                continue
            if conf is None or conf < floor:
                out.append({"rec_id": r, "placed": False,
                            "reason": f"confidence {conf} < min_confidence_auto {floor}"})
                continue
            res = approve(r, approver="auto")
            bl = _record_blocked(r, res) if res.get("blocked") else None
            out.append({"rec_id": r, "placed": bool(res.get("placed")),
                        "state": "blocked" if bl else res.get("state"),
                        "reason": res.get("error") or res.get("summary"), "result": res})
    except Exception as e:
        out.append({"rec_id": None, "placed": False, "reason": f"maybe_autoplace error: {e}"})
    return out


def _autoplace_tiered(r, rec, conf, cfg) -> dict:
    """tiers_enabled path of maybe_autoplace: calibrate → tier → (queue | approve)."""
    import mason_agent_loop as AL
    ex = rec.get("extra") or {}
    st = (ex.get("execution") or {}).get("state")
    if st not in AL._QUEUEABLE:          # never re-queue / re-tier a live, closed or rejected call
        return {"rec_id": r, "placed": False, "state": st,
                "reason": f"already {st} — auto will not place or queue it again"}
    vehicle = ((ex.get("trade") or {}).get("vehicle") or "").lower() or None
    cal = AL.calibrate(conf, ex.get("horizon"), vehicle)
    tier, _tcfg = AL.tier_for(cal.get("p_cal"), cfg, horizon=ex.get("horizon"), vehicle=vehicle)
    if not tier:
        floor = ((cfg.get("tiers") or {}).get("learning") or {}).get("min_p")
        pc = cal.get("p_cal")
        reason = f"calibrated p {('%.3f' % pc) if pc is not None else None} < learning floor {floor}"
        _record_skipped(r, rec, conf, pc, cal.get("segment"), floor, reason)   # (2026-11-19) never silently skipped
        return {"rec_id": r, "placed": False, "reason": reason}
    _set_execution(r, {"tier": tier, "p_raw": conf, "p_cal": cal.get("p_cal"),
                       "cal_segment": cal.get("segment")})
    clk = _clock()
    if cfg.get("queue_premarket") and clk.get("ok") and not clk.get("is_open"):
        _set_execution(r, {"state": "queued",
                           "queued_at": datetime.now().isoformat(timespec="seconds")})
        return {"rec_id": r, "placed": False, "state": "queued", "tier": tier,
                "reason": "market closed — queued for the open"}
    res = approve(r, approver="auto")
    bl = _record_blocked(r, res) if res.get("blocked") else None      # X4: never silently dropped
    return {"rec_id": r, "placed": bool(res.get("placed")), "state": "blocked" if bl else res.get("state"),
            "tier": tier, "reason": res.get("error") or res.get("summary"), "result": res}


def follow_entry_fill(rec_id, qty, px) -> dict:
    """X7. A call marked 'placed' on a PARTIAL fill keeps filling: agent_trades follows the order,
    and this brings the call's own quantity/price and the journal row along (the SPY row showed 1
    share while the broker held 2). `qty`/`px` are the entry order's real filled quantity and
    average price. Only the OPEN journal row written for this entry order is changed, and only by
    the quantity that arrived since it was written, so a row that a sale has since split or closed
    is never rewritten. Idempotent, also across a restart between the two writes: the journal row
    remembers the fill it was last brought to (entry_fill_qty), and once the call shows `qty`
    nothing more happens."""
    out = {"rec_id": rec_id, "changed": False}
    try:
        if not _at_cfg().get("journal_follow_fills", True):
            return dict(out, skipped="switched off")
        rec = _load_rec(rec_id)
        e = ((rec or {}).get("extra") or {}).get("execution") or {}
        new_q, new_px, old_q = _f(qty), _f(px), _f(e.get("qty"))
        if not rec or e.get("state") != "placed" or not new_q or not new_px or old_q is None:
            return dict(out, skipped="nothing to follow")
        delta = new_q - old_q
        if delta <= 1e-9:
            return dict(out, skipped="no new fill")
        oid, journal = e.get("order_id"), "not found"
        if oid and e.get("asset") == "option":
            import mason_options as MO
            play = MO.find_play_by_order_id(oid)
            if play and play.get("status") == "open" and not play.get("is_deleted"):
                seen = _f(play.get("entry_fill_qty"))
                add = new_q - (old_q if seen is None else seen)       # what the row has not been given yet
                if add > 1e-9:
                    upd = {"filled_qty": (_f(play.get("filled_qty")) or 0.0) + add, "filled_avg_price": new_px,
                           "entry_fill_qty": new_q,
                           "notes": ((play.get("notes") or "") + f" The order kept filling: now {new_q:g} "
                                     f"contracts at an average of {new_px:g} (was {old_q:g}).").strip()}
                    if play.get("contracts") is not None:
                        upd["contracts"] = int(round((_f(play.get("contracts")) or 0.0) + add))
                    if play.get("premium") is not None:
                        upd["premium"] = new_px
                    MO.update_play(play["id"], upd)
                journal = play["id"]
        elif oid:
            import mason_trades as MT
            rows = [t for t in MT.get_all_trades() if t.get("entry_order_id") == oid
                    and t.get("status") == "open" and not t.get("is_deleted")]
            if len(rows) == 1:
                t = rows[0]
                seen = _f(t.get("entry_fill_qty"))
                add = new_q - (old_q if seen is None else seen)       # what the row has not been given yet
                if add > 1e-9:
                    t["shares"] = (_f(t.get("shares")) or 0.0) + add
                    t["entry_fill_qty"] = new_q
                    t["entry_price"] = new_px
                    stop, target = _f(t.get("stop_loss")), _f(t.get("target_price"))
                    if stop is not None and target is not None:
                        t["risk_per_share"] = round(new_px - stop, 4)
                        t["reward_per_share"] = round(target - new_px, 4)
                        t["risk_reward_ratio"] = (round(t["reward_per_share"] / t["risk_per_share"], 2)
                                                  if t["risk_per_share"] > 0 else 0)
                    t["notes"] = ((t.get("notes") or "") + f" The order kept filling: now {new_q:g} shares "
                                  f"at an average of {new_px:g} (was {old_q:g}).").strip()
                    MT._save_journal([t])
                journal = t["id"]
        _set_execution(rec_id, {"qty": new_q, "price": new_px, "fill_followed_at":
                                datetime.now().isoformat(timespec="seconds"), "fill_followed_journal": journal})
        return dict(out, changed=True, qty=new_q, price=new_px, journal=journal)
    except Exception as ex_:
        return dict(out, error=f"{type(ex_).__name__}: {ex_}")


def settle_pending(rec_ids=None, *, _force_journal=False) -> list:
    """Finalise 'approved_pending_fill' recs from Alpaca's REAL order state: filled →
    journal (from the verified order) + state 'placed'; expired/canceled/rejected →
    state 'unfilled'. Safe to call on a schedule (e.g. with the director grader)."""
    out = []
    try:
        conn = mason_db.get_connection()
        try:
            if rec_ids:
                q = ",".join("?" for _ in rec_ids)
                rows = conn.execute(f"SELECT id FROM recommendations WHERE id IN ({q})",
                                    list(rec_ids)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM recommendations WHERE kind='director' AND "
                    "json_extract(extra_json,'$.execution.state')='approved_pending_fill'").fetchall()
            ids = [r["id"] for r in rows]
        finally:
            conn.close()
        for rid in ids:
            try:
                r = _settle_one(rid, _force_journal)
            except Exception as e:
                r = {"rec_id": rid, "settled": False, "error": f"{type(e).__name__}: {e}"}
            if r:
                out.append(r)
    except Exception as e:
        out.append({"rec_id": None, "settled": False, "error": f"settle_pending error: {e}"})
    return out


_OPEN_ORDER_STATES = {"new", "accepted", "pending_new", "held", "accepted_for_bidding", "calculated"}


def _settle_one(rid, _force_journal=False):
    rec = _load_rec(rid)
    ex = (rec or {}).get("extra") or {}
    e = ex.get("execution") or {}
    if not e.get("order_id") or (e.get("state") != "approved_pending_fill"
                                 and not (_force_journal and e.get("state") == "placed")):
        return None
    g = EX.get_order(e["order_id"])
    if not g.get("ok"):
        return {"rec_id": rid, "settled": False, "error": g.get("error")}
    o = g["order"]
    st = (o.get("status") or "").lower()
    dead = st in ("canceled", "cancelled", "expired", "rejected", "done_for_day")
    # A dead order that PART-FILLED is a real position: journal it and mark it placed from the
    # real fill, exactly like a full fill (otherwise it would sit on the account unmanaged).
    if st == "filled" or (dead and _f(o.get("filled_qty")) and _f(o.get("filled_avg_price"))):
        p = {"rec_id": rid, "ticker": rec["ticker"], "vehicle": _placed_trade(ex).get("vehicle"),
             "confidence": _f(ex.get("confidence")), "horizon": ex.get("horizon"),
             "slate_id": ex.get("slate_id"), "director_trade": ex.get("trade") or {},
             "exit_rules": e.get("exit_rules") or {}}
        if e.get("short_as_put"):                          # short call placed as a put: said so in the journal notes
            p["short_as_put"] = e["short_as_put"]
        if e.get("asset") == "option":
            tr = _placed_trade(ex)                         # the put that was placed (the call's own block otherwise)
            p["order"] = {"right": tr.get("vehicle"), "expiry": str(tr.get("expiry"))[:10],
                          "strike": _f(tr.get("strike"))}
            j = _journal_option_backfill(p, o, e.get("approver") or "nico")
        else:
            j = _journal_stock(p, o.get("id"), float(o.get("filled_qty") or 0),
                               float(o.get("filled_avg_price") or 0), e.get("approver") or "nico")
        _set_execution(rid, {"state": "placed", "qty": _f(o.get("filled_qty")),
                             "price": _f(o.get("filled_avg_price")),
                             "filled_at": o.get("filled_at"), "fill_status": st,
                             "journal_id": (j or {}).get("id") if isinstance(j, dict) else None})
        _notify(f"Filled (paper): {rec['ticker']} director rec {rid} @ {o.get('filled_avg_price')}",
                f"Resting order {o.get('id')} filled {o.get('filled_qty')} @ "
                f"{o.get('filled_avg_price')}; journaled.", ticker=rec["ticker"], rec_id=rid,
                dedup=f"autotrade:{rid}:filled")
        if e.get("asset") == "option" and (e.get("work") or "option_entry" in (e.get("features") or {})):
            try:                                           # option order handling: the fill next to its order's prices
                _OO().filled(rid, e, o)
            except Exception:
                pass
        return {"rec_id": rid, "settled": True, "state": "placed"}
    if dead:
        if e.get("work"):                                  # a worked entry between two orders: the old order being
            try:                                           # dead does not mean the call is unfilled yet
                if _OO().holds(e):
                    return {"rec_id": rid, "settled": False, "state": "approved_pending_fill", "order_status": st,
                            "working": True}
            except Exception:
                pass
        _set_execution(rid, {"state": "unfilled", "fill_status": st})
        return {"rec_id": rid, "settled": True, "state": "unfilled"}
    # A stock bracket parent rests GTC (its stop/target legs must outlive the day).
    # Once the call itself is stale, an entry that never filled is cancelled.
    if st in _OPEN_ORDER_STATES and not _f(o.get("filled_qty")) and \
            trading_days_since(e.get("placed_at") or rec.get("created_date"),
                               _now_et().date()) > _MAX_TRADING_DAYS_OLD:
        code, _ = EX._request("DELETE", f"/orders/{o.get('id')}")
        if code in (200, 204):
            _set_execution(rid, {"state": "unfilled", "fill_status": "canceled_stale"})
            return {"rec_id": rid, "settled": True, "state": "unfilled", "canceled_stale": True}
        return {"rec_id": rid, "settled": False, "error": f"stale cancel failed ({code})"}
    return {"rec_id": rid, "settled": False, "state": "approved_pending_fill", "order_status": st}


def risk_state() -> dict:
    """UI summary: flags, limits, live account risk, today's autotrade activity."""
    try:
        cfg = _at_cfg()
        acct, pos, clk = _account(), _positions(), _clock()
        try:
            _, _, base, enabled = EX._creds()
        except Exception:
            base, enabled = "", False
        today = _now_et().date().isoformat()
        placed_today = _trades_today(today)
        conn = mason_db.get_connection()
        try:
            pend = [r["id"] for r in conn.execute(
                "SELECT id FROM recommendations WHERE kind='director' AND "
                "json_extract(extra_json,'$.execution.state')='approved_pending_fill'").fetchall()]
        finally:
            conn.close()
        eq = acct.get("equity") if acct.get("ok") else None
        own = _own_book(pos) if pos.get("ok") else None
        expo = (_sleeve_exposure(pos, own) / eq * 100) if (eq and pos.get("ok")) else None
        gross_pct = (pos["gross"] / eq * 100) if (eq and pos.get("ok")) else None
        ls = _loss_stop(acct, own, cfg) if acct.get("ok") else None
        ab = _f(cfg.get("account_loss_breaker_pct"))
        halted = []
        if not cfg.get("approve_enabled"):
            halted.append("approve_enabled off")
        if _PAPER_HOST not in (base or ""):
            halted.append("not the paper endpoint")
        if ls and not ls["ok"]:
            halted.append("daily loss stop hit")
        if acct.get("ok") and ab and acct["day_pnl_pct"] <= -ab:
            halted.append("account loss breaker hit")
        if len(placed_today) >= int(cfg["max_trades_per_day"]):
            halted.append("max trades/day reached")
        if expo is not None and expo >= float(cfg["max_open_exposure_pct"]):
            halted.append("director sleeve at/over cap")
        if acct.get("ok") and cfg.get("no_new_margin", True) and (acct.get("cash") or 0) <= 0:
            halted.append("no free cash (director trades never add margin)")
        if clk.get("ok") and not clk["is_open"]:
            halted.append("market closed")
        return {"ok": True, "approve_enabled": bool(cfg.get("approve_enabled")),
                "auto_place": bool(cfg.get("auto_place")), "paper": _PAPER_HOST in (base or ""),
                "paper_trading_enabled": bool(enabled),
                "limits": {k: cfg[k] for k in ("max_pct_per_trade", "max_open_exposure_pct",
                                                "max_trades_per_day", "daily_loss_stop_pct",
                                                "min_confidence_auto")},
                "equity": eq, "day_pnl_pct": acct.get("day_pnl_pct"),
                "own_pnl_usd": round(own["pnl"], 2) if (own and own.get("ok")) else None,   # Mason's own trades
                "own_pnl_pct": round(ls["pct"], 3) if (ls and ls.get("own")) else None,
                "loss_stop": ls["detail"] if ls else None,
                "open_exposure_pct": round(expo, 2) if expo is not None else None,   # director sleeve
                "account_gross_pct": round(gross_pct, 2) if gross_pct is not None else None,
                "cash": acct.get("cash"),
                "trades_today": len(placed_today), "placed_today": placed_today,
                "pending_fill": pend, "market_open": clk.get("is_open"),
                "can_place_now": not halted, "halted_reasons": halted}
    except Exception as e:
        return {"ok": False, "error": f"risk_state failed: {e}"}
