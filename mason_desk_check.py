"""Mason — desk checks between the two scheduled director calls (Fable step 2, 2026-11-14).

The director (Claude Opus 5.5) decides at two fixed times a day. Nothing looked at the
book in between. This module adds a short, cheap check by Claude Sonnet 5.5 whenever
something happens that could matter before the next scheduled call:

  news        a story that just reached the inbox level (or a strong kind of event) on a
              stock Mason holds, Nico holds, or that is on the watchlist; for the very
              strongest kinds, optionally any liquid US stock
  move        a sharp move or a volume spike on a held or watched stock (read from the
              watch_events the live tape already records; no new polling)
  position    a held position close to its stop or target; earnings due tonight or
              tomorrow morning for a held stock; a Fed decision today
  earnings    an earnings result that just landed (raised by mason_pead's session sweep,
              with the numbers)
  after_close / before_open   two fixed light passes over the whole book

A check answers, per stock, with one of: ignore, watch, close, tighten_stop, recommend,
escalate. It never opens a position. close / tighten_stop act only on Mason's own
positions and go through the exit manager exactly like a director "close". For a
position Nico entered a check can only write him a note. escalate asks Opus for a full
decision through mason_director.run_escalation (the existing event-call path).

Everything is OFF by default (desk_check.enabled and one switch per trigger). Every
check, its inputs, its answer, its cost and what was done with it is stored in
desk_checks so its usefulness can be graded against what the price did next
(grade_outcomes, nightly). Spend goes into the director's ledger (mode "check") and is
held under a per-day count, a per-day dollar cap and the overall daily model budget.

Failure isolation: tick() never raises; a model error, a timeout, bad JSON or a missing
quote ends that one check and nothing else. It runs as its own job on the desk worker
queue, never in the scheduler thread, and never inside the exit-manager tick.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import mason_db

log = logging.getLogger("mason.desk_check")

BASE = Path(__file__).parent
_CREATED_BY = "mason_desk_check"
ACTIONS = ("ignore", "watch", "close", "tighten_stop", "recommend", "escalate")
_OCC = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")
_STALE_RUNNING_S = 180          # a check still "running" after this was killed by a restart
_PASSES = ("after_close", "before_open", "fed_today")   # whole-book checks at fixed times
_SCHEMA_DONE = set()

_STRONG_KINDS = ["mgmt_change", "guidance_cut", "guidance_raise", "halt", "distress", "mna",
                 "accounting", "investigation", "dividend_cut", "fda_decision", "adcom",
                 "trial_readout", "contract_loss", "data_breach", "product_recall", "labor_strike"]
_STRONGEST = ["mgmt_change", "guidance_cut", "halt", "distress", "accounting", "mna", "fda_decision"]

# plain words for the kinds of story Mason's news desk files (anything else: the id with spaces)
_KIND_WORDS = {"mgmt_change": "a change of management", "guidance_cut": "the company lowered its own forecast",
               "guidance_raise": "the company raised its own forecast", "guidance": "company forecast",
               "halt": "trading in the stock was halted", "distress": "bankruptcy or delisting risk",
               "mna": "merger or takeover", "accounting": "accounting problem", "investigation": "government investigation",
               "dividend_cut": "dividend cut", "fda_decision": "FDA decision", "adcom": "FDA advisory panel",
               "trial_readout": "drug trial result", "contract_loss": "lost contract", "data_breach": "data breach",
               "product_recall": "product recall", "labor_strike": "strike", "analyst_action": "analyst rating change",
               "earnings": "earnings", "product_launch": "product launch", "deal_partnership": "deal or partnership",
               "insider_buy": "insider buying", "insider_sell": "insider selling", "contract_award": "contract won",
               "legal_regulatory": "legal or regulatory news"}

_DEFAULTS = {
    "enabled": False,                 # master switch: nothing below runs while this is false
    "model": "claude-sonnet-5-5",
    "effort": "low",
    "max_output_tokens": 4000,
    "timeout_s": 45,                  # one check can hold the desk worker this long at most (no retry in the call)
    "hours_et": ["04:00", "20:00"],   # trading days; outside: only the strongest news on held names
    "max_checks_per_day": 16,         # event checks a day (the two fixed passes are not counted)
    "max_other_checks_per_day": 4,    # of those: checks on stocks nobody holds or watches
    "daily_usd_cap": 0.90,
    "est_usd_per_check": 0.02,        # used for the cap test before a call (measured 2026-11-14: $0.008-0.015)
    "max_checks_per_pass": 3,         # per 3-minute pass
    "per_ticker_window_min": 45,      # one check per stock in this window, unless a new strong event
    "trigger_max_age_min": 30,        # an event older than this when its turn comes is dropped
    "apply_actions": True,            # false: close / tighten_stop / escalate are reported, not done
    "notes_to_director": False,       # show "watch" notes and handed-over events to Opus (new prompt text)
    "archive_prompts": True,
    "tighten_min_gap_pct": 0.5,       # a raised stop must stay at least this far under the price
    "max_closes_per_day": 2,          # (review fix R2) sales / cancelled orders a day that checks may cause
    "news": {"enabled": False, "min_score": 0.70, "strong_kinds": list(_STRONG_KINDS),
             "strong_min_score": 0.60, "any_ticker": False, "any_ticker_kinds": list(_STRONGEST),
             "any_ticker_min_score": 0.85, "off_hours_kinds": list(_STRONGEST) + ["investigation", "dividend_cut"],
             "max_headlines": 6},
    # a move counts when it is this many "normal days" big (the stock's average daily range): less for a held
    # stock, more for one that is only watched; a move that is just the market moving does not count
    "move": {"enabled": False, "atr_mult_held": 1.5, "atr_mult": 2.5, "min_move_pct": 3.0, "rvol": 3.0,
             "rvol_min_move_pct": 3.0, "min_vs_sector_pct": 2.0, "max_per_day": 6},
    "position": {"enabled": False, "near_pct": 1.5, "earnings_check_et": "15:20", "fed_check_et": "13:40"},
    "earnings": {"enabled": False, "large_cap": True, "large_cap_min_surprise_pct": 15.0},
    "passes": {"after_close": {"enabled": False, "at_et": "16:20"},
               "before_open": {"enabled": False, "at_et": "08:45"}, "window_min": 60,
               "nico_min_move_pct": 3.0},
    "grading": {"enabled": True},     # nightly: fill in what the price did after each check (no model call)
}

_TABLES = """
CREATE TABLE IF NOT EXISTS desk_checks(
    id TEXT PRIMARY KEY, created_at TEXT, created_epoch REAL, day TEXT,
    trigger TEXT, trigger_key TEXT, ticker TEXT, holder TEXT, off_hours INTEGER DEFAULT 0,
    status TEXT, skip_reason TEXT, trigger_json TEXT, input_summary TEXT,
    model TEXT, effort TEXT, prompt_hash TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, seconds REAL, output_json TEXT, raw_text TEXT, action TEXT, outcome_json TEXT,
    ref_px REAL, escalation_state TEXT, escalation_slate_id TEXT,
    note_state TEXT, note_slate_id TEXT,
    move_1s_pct REAL, move_5s_pct REAL, spy_1s_pct REAL, spy_5s_pct REAL, graded_at TEXT,
    attempts INTEGER DEFAULT 1, grade_done INTEGER DEFAULT 0, record_deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_desk_checks_day ON desk_checks(day);
CREATE INDEX IF NOT EXISTS idx_desk_checks_ticker ON desk_checks(ticker, created_epoch);
CREATE TABLE IF NOT EXISTS desk_check_triggers(
    key TEXT PRIMARY KEY, trigger TEXT, ticker TEXT, created_epoch REAL, prio INTEGER DEFAULT 5,
    strong_key TEXT, payload_json TEXT, state TEXT DEFAULT 'pending', check_id TEXT, note TEXT,
    attempts INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_desk_check_triggers_state ON desk_check_triggers(state, created_epoch);
CREATE TABLE IF NOT EXISTS desk_check_stops(
    rec_id TEXT PRIMARY KEY, ticker TEXT, level REAL, set_at TEXT, check_id TEXT,
    state TEXT DEFAULT 'active', ended_at TEXT, note TEXT);
"""

# What the model must return. The API enforces this shape (mason_llm.complete_check);
# validate() checks it again and adds the rules a schema cannot express.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "action": {"type": "string", "enum": list(ACTIONS)},
            "reason": {"type": "string"},
            "stop_level": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "direction": {"anyOf": [{"type": "string", "enum": ["long", "short"]}, {"type": "null"}]},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["ticker", "action", "reason", "stop_level", "direction", "note"],
        "additionalProperties": False}}},
    "required": ["decisions"],
    "additionalProperties": False,
}

# ── the prompt (see PROMPTS_FOR_NICO.md; needs Nico's approval before desk_check.enabled) ──
SYSTEM_PROMPT = """You do quick checks for Mason, a small automated trading desk that runs one person's paper trading account. The owner is Nico.

A senior decision-maker, called the director, reviews everything twice a day. Between those reviews nobody is looking. Your job is to look at one thing that just happened and say what, if anything, should be done about it before the director's next review.

You are shown: what happened, the current price, any position in the stock and who owns it, the director's last view on it, and what is scheduled next. Work only from what you are shown. Do not make up prices, levels or facts. If something you need is missing, say so in the reason and choose the more cautious answer.

Give one answer per stock:
- "ignore": this changes nothing. It is old, already in the price, or does not matter for the position.
- "watch": the director should look at this at the next review, but nothing needs doing before then.
- "close": sell Mason's own position now (at the next open if the market is closed), or cancel Mason's order that is waiting for the open. Only for something marked "Mason". Use it when the reason for the trade no longer holds, or holding is clearly no longer worth the risk.
- "tighten_stop": move the stop on Mason's own position closer to the price. Put the new stop in "stop_level". Only for a position marked "Mason". The new stop must be under the current price and above the old stop. For a call or put, the stop is a price of the option itself, not of the stock.
- "recommend": Nico holds this himself and you may not trade it. Write him a short note, three sentences at most, saying what you would do and why, for example sell part of it, set a stop at a price, or hold. Put the note in "note". If you would set a stop, also put the price in "stop_level".
- "escalate": this needs a full decision from the director now, not at the next review. Use it for a new trade worth taking, or a position that needs more than a simple sale. Say which way you expect the price to go in "direction": "long" (up) or "short" (down).

Rules:
- You never open a new position yourself. If one should be opened, escalate.
- A full director decision costs about ten times as much as this check, and only a few can be run in a day. Escalate only when waiting for the next review would probably lose money or miss the move.
- Most checks should end in "ignore" or "watch". Reacting to noise costs more than missing a small move.
- If the director's last view says it is waiting for something to be settled, and that has now happened, escalate. The director asked to be told.
- A big move on the news is not by itself a reason to act. Ask what is still not in the price.
- For earnings, compare the result with what was expected and look at how the price reacted afterwards. A good number with a falling price is a warning.
- If a stock is held by both Mason and Nico, answer for Mason's position; add what you would tell Nico in "note".
- Write each reason as one plain sentence that can be read in five seconds. No jargon. No abbreviations except stock symbols.

Answer with JSON only, in exactly this shape:
{"decisions": [{"ticker": "XYZ", "action": "ignore", "reason": "one sentence", "stop_level": null, "direction": null, "note": null}]}"""


# ── small helpers ───────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge(defaults: dict, over) -> dict:
    out = dict(defaults)
    for k, v in (over or {}).items() if isinstance(over, dict) else []:
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def cfg(full: dict | None = None) -> dict:
    return _merge(_DEFAULTS, (full if full is not None else _load()).get("desk_check"))


def enabled(full: dict | None = None) -> bool:
    try:
        return cfg(full).get("enabled") is True
    except Exception:
        return False


def _DIR():
    import mason_director
    return mason_director


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _loads(s, default=None):
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _now_et(now=None) -> datetime:
    import mason_analyst_desk as AD
    return AD._now_et(now)


def _day(et) -> str:
    return et.strftime("%Y-%m-%d")


def _hm(et) -> str:
    return et.strftime("%H:%M")


def _px(v) -> str:
    x = _f(v)
    return "?" if x is None else (f"{x:,.2f}" if abs(x) >= 1 else f"{x:.3f}")


def _pct(v, signed=True) -> str:
    x = _f(v)
    return "?" if x is None else (f"{x:+.1f}%" if signed else f"{x:.1f}%")


def _qty(v) -> str:
    x = _f(v) or 0.0
    return f"{x:g}"


def _clock_words(ts) -> str:
    try:
        return _now_et(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _conn():
    return mason_db.get_connection()


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


def _state_get(key, default=None):
    try:
        import mason_analyst_desk as AD
        return AD.get_state(key, default)
    except Exception:
        return default


def _state_set(key, value):
    try:
        import mason_analyst_desk as AD
        AD.set_state(key, value)
    except Exception as e:
        log.info(f"[desk_check] state write {key}: {e}")


def ensure_schema(force=False) -> bool:
    try:
        key = str(Path(mason_db.DB_FILE).resolve())
        if key in _SCHEMA_DONE and not force:
            return True
        conn = _conn()
        try:
            conn.executescript(_TABLES)
            conn.commit()
        finally:
            conn.close()
        _SCHEMA_DONE.add(key)
        return True
    except Exception as e:
        log.warning(f"[desk_check] schema: {e}")
        return False


# ── seams (tests and the replay swap these) ─────────────────────────────────────

def _broker_positions() -> dict:
    import mason_execution
    return mason_execution.positions()


def _quote(ticker) -> dict:
    import mason_quotes
    return mason_quotes.stock_quote(ticker, relaxed=True)


def _strict_quote(ticker) -> dict:
    """Fresh quotes only (no relaxed age): what a raised stop is judged on."""
    import mason_quotes
    return mason_quotes.stock_quote(ticker)


def _option_quote(occ) -> dict:
    import mason_quotes
    return mason_quotes.option_quote(occ, relaxed=True)


def _strict_option_quote(occ) -> dict:
    """Fresh, two-sided, inside the spread limit: the only option price a stop may be judged on."""
    import mason_quotes
    return mason_quotes.option_quote(occ)


def _tape(ticker) -> dict:
    try:
        import mason_watch_signals as SG
        return SG.tape_stats(ticker) or {}
    except Exception:
        return {}


def _watch_items() -> list:
    try:
        import mason_watchlist as WL
        return WL.list_items("active") or []
    except Exception:
        return []


def _liquid() -> set:
    """Large, liquid US stocks: the index members mason_triage already keeps (cache only)."""
    try:
        import mason_triage as TR
        try:
            u = TR.broad_universe(allow_refresh=False)
        except TypeError:
            u = TR.broad_universe()
        return {str(k).upper() for k in (u or {})}
    except Exception:
        return set()


def _autotrade_cfg() -> dict:
    try:
        import mason_autotrade
        return mason_autotrade._at_cfg()
    except Exception:
        return (_load().get("autotrade") or {})


def _holdings_and_limits(ticker=None) -> list:
    """Step 4: the lines that show what the account holds (per stock and per sector, with the
    distance to each stop) and the limits on new trades. [] unless autotrade.sizing.show_book is on
    (OFF until Nico approves the wording). One stock: only that stock's holdings are listed."""
    try:
        import mason_sizing as SZ
        cfg = _autotrade_cfg()
        if not SZ.show_book(cfg):
            return []
        blk = SZ.book_block(cfg=cfg, tickers=[ticker] if ticker else None)
        return ["", blk["title"], blk["body"]] if blk.get("body") else []
    except Exception as e:
        log.info(f"[desk_check] holdings-and-limits lines unavailable: {e}")
        return []


def _model_call(system, user, c) -> dict:
    import mason_llm
    # The instructions are cached only when another check follows in the same pass (seconds later); a
    # lone check would pay the cache-write surcharge for an entry nobody reads back within 5 minutes.
    return mason_llm.complete_check(system, user, model=c["model"], effort=c["effort"],
                                    max_tokens=int(c["max_output_tokens"]), schema=OUTPUT_SCHEMA,
                                    timeout_s=float(c["timeout_s"]), cache_system=bool(c.get("_several_in_pass")))


def _notify(notifier, title, body, *, ticker=None, severity="info", dedup=None, kind="system",
            ref_id=None):
    try:
        if notifier is None:
            import mason_notifications
            notifier = mason_notifications.add_notification
        return notifier(type=kind, title=str(title)[:200], body=body, severity=severity, ticker=ticker,
                        ref_table="desk_checks" if ref_id else None, ref_id=ref_id,
                        created_by=_CREATED_BY, dedup_key=dedup)
    except Exception as e:
        log.warning(f"[desk_check] notification failed: {e}")
        return None


# ── who holds what ──────────────────────────────────────────────────────────────

def load_book(*, broker=True) -> dict:
    """{'mason': {T: [agent_trades row]}, 'queued': {T: [waiting order]}, 'nico': {T: [lot]},
    'watch': {T: item}, 'broker_ok': bool}. Mason's rows are the ones agent_trades says it
    opened; everything else at the broker, in the personal accounts or logged as Nico's is
    Nico's. Never raises."""
    bk = {"mason": {}, "queued": {}, "nico": {}, "watch": {}, "broker_ok": False}
    own_qty, own_journal = {}, set()
    try:
        for r in _rows("SELECT * FROM agent_trades WHERE source='mason' AND status='open' "
                       "AND COALESCE(record_deleted,0)=0"):
            t = str(r.get("ticker") or "").upper()
            if not t:
                continue
            bk["mason"].setdefault(t, []).append(r)
            sym = str(r.get("occ") or t).upper()
            own_qty[sym] = own_qty.get(sym, 0.0) + (_f(r.get("qty")) or 0.0)
            if r.get("journal_id"):
                own_journal.add(str(r["journal_id"]))
    except Exception as e:
        log.info(f"[desk_check] agent_trades unavailable: {e}")
    try:
        for r in _rows("SELECT id, ticker, direction, created_date, extra_json FROM recommendations "
                       "WHERE kind='director' AND COALESCE(record_deleted,0)=0 "
                       "AND json_extract(extra_json,'$.execution.state')='queued'"):
            ex = _loads(r.get("extra_json"))
            tr = ex.get("trade") or {}
            bk["queued"].setdefault(str(r["ticker"]).upper(), []).append({
                "rec_id": r["id"], "direction": r.get("direction"), "vehicle": tr.get("vehicle") or "stock",
                "strike": tr.get("strike"), "expiry": tr.get("expiry"), "entry_limit": tr.get("entry_limit"),
                "stop": tr.get("stop"), "target": tr.get("target"),
                "thesis": str(ex.get("prediction") or "")[:200], "created": str(r.get("created_date") or "")[:16]})
    except Exception as e:
        log.info(f"[desk_check] queued orders unavailable: {e}")
    journal = {}
    try:
        for r in _rows("SELECT id, ticker, shares, entry_price, stop_loss, target_price, thesis, entry_date "
                       "FROM trades WHERE status='open' AND COALESCE(is_deleted,0)=0"):
            if str(r["id"]) not in own_journal:
                journal.setdefault(str(r["ticker"]).upper(), []).append(r)
    except Exception:
        pass
    pos = {}
    if broker:
        try:
            pos = _broker_positions() or {}
        except Exception as e:
            pos = {"ok": False, "error": str(e)}
    if pos.get("ok"):
        bk["broker_ok"] = True
        try:                                           # idle-cash rule: shares parked in the index fund are spare
            import mason_idle_cash as _IC              # cash, not a holding of Nico's ({} when nothing is parked)
            parked = dict(_IC.owned_map() or {})       # (review fix) a working purchase of the rule counts in full
        except Exception:
            parked = {}
        for p in pos.get("positions") or []:
            sym = str(p.get("symbol") or "").upper()
            m = _OCC.match(sym)
            und = m.group(1) if m else sym
            rest = abs(_f(p.get("qty")) or 0.0) - own_qty.get(sym, 0.0) - (0.0 if m else parked.get(sym, 0.0))
            if not und or rest <= 1e-9:
                continue
            j = (journal.get(und) or [{}])[0] if not m else {}
            bk["nico"].setdefault(und, []).append({
                "symbol": sym, "qty": rest, "entry": _f(p.get("avg_entry_price")), "is_option": bool(m),
                "where": "Alpaca paper account", "px": _f(p.get("current_price")),
                "stop": _f(j.get("stop_loss")), "target": _f(j.get("target_price")),
                "thesis": str(j.get("thesis") or "")[:200], "journal_id": j.get("id")})
    else:                                           # broker unreachable: the journal is the fallback
        for und, js in journal.items():
            for j in js:
                bk["nico"].setdefault(und, []).append({
                    "symbol": und, "qty": _f(j.get("shares")) or 0.0, "entry": _f(j.get("entry_price")),
                    "is_option": False, "where": "Alpaca paper account (from the journal)", "px": None,
                    "stop": _f(j.get("stop_loss")), "target": _f(j.get("target_price")),
                    "thesis": str(j.get("thesis") or "")[:200], "journal_id": j.get("id")})
    try:
        for r in _rows("SELECT ticker, account_label, shares, cost_basis FROM personal_positions "
                       "WHERE status='open' AND COALESCE(is_deleted,0)=0"):
            t = str(r.get("ticker") or "").upper()
            if t and t != "CASH" and (_f(r.get("shares")) or 0) > 0:
                bk["nico"].setdefault(t, []).append({
                    "symbol": t, "qty": _f(r["shares"]), "entry": _f(r.get("cost_basis")), "is_option": False,
                    "where": str(r.get("account_label") or "personal account"), "px": None,
                    "stop": None, "target": None, "thesis": "", "journal_id": None})
    except Exception:
        pass
    try:
        for it in _watch_items():
            t = str(it.get("ticker") or "").upper()
            if t:
                bk["watch"][t] = {"origin": it.get("origin"), "reason": str(it.get("reason") or "")[:160],
                                  "added": str(it.get("added_date") or "")[:10]}
    except Exception:
        pass
    return bk


def holder_of(bk, t) -> str:
    m, n = t in bk["mason"] or t in bk["queued"], t in bk["nico"]
    if m and n:
        return "both"
    return "mason" if m else ("nico" if n else ("watchlist" if t in bk["watch"] else "none"))


# ── time gates ──────────────────────────────────────────────────────────────────

def in_hours(et, c) -> bool:
    lo, hi = (c.get("hours_et") or ["04:00", "20:00"])[:2]
    return _DIR().is_trading_day(et) and str(lo) <= _hm(et) <= str(hi)


def _slot_due(et, at, window_min) -> bool:
    try:
        hh, _, mm = str(at).partition(":")
        start = int(hh) * 60 + int(mm)
    except ValueError:
        return False
    return start <= et.hour * 60 + et.minute <= start + int(window_min)


def _next_session_day(et) -> str:
    d = et
    for _ in range(10):
        d = d + timedelta(days=1)
        if _DIR().is_trading_day(d):
            return _day(d)
    return _day(et + timedelta(days=1))


# ── triggers ────────────────────────────────────────────────────────────────────

def raise_trigger(trigger, ticker, key, payload=None, *, prio=5, strong_key=None, now=None) -> bool:
    """Queue one event for the next pass. Idempotent on `key`. Used by the collectors here
    and by mason_pead.session_sweep (earnings results). Returns True when a NEW row landed.
    Never raises."""
    try:
        if not ensure_schema():
            return False
        n = _exec("INSERT OR IGNORE INTO desk_check_triggers(key, trigger, ticker, created_epoch, prio, "
                  "strong_key, payload_json, state) VALUES (?,?,?,?,?,?,?,'pending')",
                  (str(key), str(trigger), (str(ticker).upper() if ticker else None),
                   float(now if now is not None else time.time()), int(prio), strong_key,
                   json.dumps(payload or {}, default=str)))
        return n > 0
    except Exception as e:
        log.info(f"[desk_check] raise_trigger {key}: {e}")
        return False


def wants_earnings(ticker, surprise_pct, *, held=False, watched=False, full: dict | None = None) -> bool:
    """Should an earnings result for `ticker` raise a check? (asked by mason_pead)"""
    c = cfg(full)
    e = c.get("earnings") or {}
    if not (c.get("enabled") and e.get("enabled")):
        return False
    if held or watched:
        return True
    sp = _f(surprise_pct)
    return bool(e.get("large_cap") and sp is not None and abs(sp) >= float(e.get("large_cap_min_surprise_pct", 15)))


def _headlines(catalyst_id=None, ticker=None, since=None, limit=6) -> list:
    try:
        if catalyst_id:
            rows = _rows("SELECT headline, source, ingested_at, published_at FROM news_events WHERE catalyst_id=? "
                         "ORDER BY ingested_at DESC LIMIT ?", (catalyst_id, int(limit)))
        else:
            rows = _rows("SELECT headline, source, ingested_at, published_at FROM news_events WHERE ingested_at >= ? "
                         "AND resolved_tickers LIKE ? ORDER BY ingested_at DESC LIMIT ?",
                         (float(since or 0), f'%"{ticker}"%', int(limit)))
        out = [{"at": r.get("published_at") or r.get("ingested_at"), "source": r.get("source"),
                "text": str(r.get("headline") or "")[:200]} for r in rows]
        return sorted(out, key=lambda h: -(_f(h["at"]) or 0))
    except Exception:
        return []


def news_qualifies(cat: dict, bk: dict, c: dict, *, hours_ok: bool, now: float, liquid=None) -> dict | None:
    """Pure rule for one catalysts row → trigger dict or None. Codes against the table's
    columns only (id, primary_ticker, event_kind, score, status, first_seen, last_seen,
    alerted_at, extra_json.strong_kinds / extra_json.alert.n)."""
    n = c["news"]
    t = str(cat.get("primary_ticker") or "").upper()
    if not t or cat.get("status") in ("consumed", "expired"):
        return None
    score = _f(cat.get("score")) or 0.0
    extra = _loads(cat.get("extra_json"))
    # (review fix R6) A "strong kind" is one the news desk CONFIRMED on this story (extra.strong_kinds).
    # event_kind is only the story's label and is inherited by headlines that join the story, so it is not
    # evidence of a strong event. And the trigger no longer waits for the inbox flag: the inbox is rationed
    # per ticker (3 alerts a day), which is exactly when a held name has the most news.
    kinds = {str(k) for k in (extra.get("strong_kinds") or [])}
    strong = sorted(kinds & set(n.get("strong_kinds") or []))
    since = now - float(c["trigger_max_age_min"]) * 60
    al = _f(cat.get("alerted_at"))
    fresh_alert = al is not None and al >= since
    fresh_story = ((_f(cat.get("last_seen")) or 0) >= since
                   and (_f(cat.get("first_seen")) or 0) >= now - 86400)
    if not (fresh_alert or fresh_story):
        return None
    if not fresh_alert and not strong:             # not an inbox alert, no confirmed event: commentary / wraps out
        try:
            import mason_triage as TR
            fn = getattr(TR, "non_event_reason", None)
            if callable(fn) and fn(str(cat.get("title") or ""), company=True):
                return None
        except Exception:
            pass
    hold = holder_of(bk, t)
    if hold != "none":
        if not (score >= float(n["min_score"]) or (strong and score >= float(n["strong_min_score"]))):
            return None
    else:
        if not (n.get("any_ticker") and (kinds & set(n.get("any_ticker_kinds") or []))
                and score >= float(n["any_ticker_min_score"])
                and t in (liquid if liquid is not None else _liquid())):
            return None
    if not hours_ok and not (hold in ("mason", "nico", "both") and (kinds & set(n.get("off_hours_kinds") or []))):
        return None
    alert_n = (extra.get("alert") or {}).get("n")
    seq = int(alert_n) if alert_n else (1 if al else 0)
    # once per story and alert; a newly confirmed strong kind on the same story is a new event
    return {"trigger": "news", "ticker": t, "key": f"news:{cat.get('id')}:{seq}" + (f":{'+'.join(strong)}" if strong else ""),
            "prio": {"mason": 0, "both": 0, "nico": 1, "watchlist": 2}.get(hold, 3),
            "strong_key": (f"{t}:{','.join(strong)}" if strong else None),
            "payload": {"catalyst_id": cat.get("id"), "kind": cat.get("event_kind"), "strong_kinds": strong,
                        "score": score, "title": cat.get("title"), "n_events": cat.get("n_events"),
                        "n_sources": cat.get("n_sources"), "first_seen": cat.get("first_seen"),
                        "direction": cat.get("direction"), "off_hours": not hours_ok}}


def move_qualifies(ev: dict, bk: dict, c: dict) -> dict | None:
    """Pure rule for one watch_events row (move_step / rvol_spike / gap) → trigger or None."""
    m = c["move"]
    t = str(ev.get("ticker") or "").upper()
    hold = holder_of(bk, t)
    if hold == "none":
        return None
    v = _loads(ev.get("value_json"))
    kind = ev.get("kind")
    chg = _f(v.get("chg_pct") if kind != "gap" else v.get("gap_pct"))
    atr, rv, vs = _f(v.get("atr_pct")), _f(v.get("rvol")), _f(v.get("vs_sector_pct"))
    mult = (abs(chg) / atr) if (chg is not None and atr) else None
    need = float(m["atr_mult_held"] if hold in ("mason", "nico", "both") else m["atr_mult"])
    big = chg is not None and abs(chg) >= float(m["min_move_pct"]) and mult is not None and mult >= need
    vol = (kind == "rvol_spike" and rv is not None and rv >= float(m["rvol"])
           and chg is not None and abs(chg) >= float(m["rvol_min_move_pct"]))
    if not (big or vol):
        return None
    if vs is not None and abs(vs) < float(m.get("min_vs_sector_pct", 0) or 0):
        return None                                  # the whole sector / market moved; nothing particular to this stock
    day = _day(_now_et(float(ev.get("ts") or time.time())))
    step = v.get("step") if kind == "move_step" else kind
    return {"trigger": "move", "ticker": t, "key": f"move:{t}:{day}:{step}",
            "prio": {"mason": 1, "both": 1, "nico": 2}.get(hold, 4), "strong_key": None,
            "payload": {"event": kind, "chg_pct": chg, "atr_pct": atr, "atr_mult": mult, "rvol": rv,
                        "message": ev.get("message"), "at": ev.get("ts")}}


def _last_bar(ticker, max_age_s=600, now=None):
    """Newest 1-minute close the live tape stored for `ticker` (no network)."""
    try:
        r = _rows("SELECT c, ts FROM watch_bars WHERE ticker=? AND tf='1m' ORDER BY ts DESC LIMIT 1", (ticker,))
        if r and (float(now if now is not None else time.time()) - float(r[0]["ts"])) <= max_age_s:
            return _f(r[0]["c"])
    except Exception:
        pass
    return None


def near_level(px, stop, target, near_pct) -> str | None:
    """'stop' / 'target' when a long position's price is within near_pct of that level
    (and has not crossed it), else None."""
    px, stop, target = _f(px), _f(stop), _f(target)
    if px is None:
        return None
    k = float(near_pct) / 100.0
    if stop and stop < px <= stop * (1 + k):
        return "stop"
    if target and target * (1 - k) <= px < target:
        return "target"
    return None


def _earnings_due(ticker, today, nxt) -> dict | None:
    """Unreported earnings for `ticker` after today's close or before the next open."""
    try:
        # a number on a row dated in the future is last quarter's, not a result (mason_exposure ruling 35)
        for r in _rows("SELECT report_date, timing, fiscal_period, eps_est, eps_act FROM earnings_results WHERE ticker=? "
                       "AND report_date IN (?,?) ORDER BY report_date", (ticker, today, nxt)):
            tm = r.get("timing")
            if (r["report_date"] == today and tm != "am" and r.get("eps_act") is None) \
                    or (r["report_date"] == nxt and nxt > today and tm == "am"):
                return r
    except Exception:
        pass
    return None


def _next_earnings(ticker, today) -> dict | None:
    try:
        r = _rows("SELECT report_date, timing FROM earnings_results WHERE ticker=? AND (report_date > ? OR "
                  "(report_date = ? AND eps_act IS NULL)) ORDER BY report_date LIMIT 1", (ticker, today, today))
        if r:
            return r[0]
        r = _rows("SELECT event_date AS report_date, event_time AS timing FROM calendar_events WHERE ticker=? "
                  "AND event_type='earnings' AND event_date >= ? AND record_deleted=0 ORDER BY event_date LIMIT 1",
                  (ticker, today))
        return r[0] if r else None
    except Exception:
        return None


def _fed_today(today) -> str | None:
    try:
        for r in _rows("SELECT title, event_time FROM calendar_events WHERE event_type='econ' AND event_date=? "
                       "AND record_deleted=0", (today,)):
            if re.search(r"federal funds rate|fomc statement|fomc press", str(r.get("title") or ""), re.I):
                return f"{r.get('title')} at {str(r.get('event_time') or '')[:5]}"
    except Exception:
        pass
    return None


def collect(et, c, bk, *, now=None) -> int:
    """Look at what the rest of Mason has already stored and queue the events that deserve
    a check. Returns the number of new events. Cheap: a few indexed reads, no network."""
    now = float(now if now is not None else time.time())
    hours_ok = in_hours(et, c)
    today, n = _day(et), 0
    since = now - float(c["trigger_max_age_min"]) * 60
    if c["news"].get("enabled"):
        try:
            floor = min(float(c["news"]["min_score"]), float(c["news"]["strong_min_score"]))
            liquid = _liquid() if c["news"].get("any_ticker") else set()
            for cat in _rows("SELECT * FROM catalysts WHERE COALESCE(record_deleted,0)=0 AND primary_ticker IS NOT NULL "
                             "AND score >= ? AND (alerted_at >= ? OR last_seen >= ?)", (floor, since, since)):
                tr = news_qualifies(cat, bk, c, hours_ok=hours_ok, now=now, liquid=liquid)
                if tr and raise_trigger(tr["trigger"], tr["ticker"], tr["key"], tr["payload"], prio=tr["prio"],
                                        strong_key=tr["strong_key"], now=now):
                    n += 1
        except Exception as e:
            log.info(f"[desk_check] news collect: {e}")
    if hours_ok and (c["move"].get("enabled") or c["position"].get("enabled")):
        try:
            for ev in _rows("SELECT id, ticker, ts, kind, value_json, message FROM watch_events WHERE ts >= ? AND kind IN "
                            "('move_step','rvol_spike','gap','pos_near_stop','pos_near_target') ORDER BY ts", (since,)):
                t = str(ev.get("ticker") or "").upper()
                if ev["kind"].startswith("pos_near"):
                    if c["position"].get("enabled") and holder_of(bk, t) in ("mason", "nico", "both"):
                        side = ev["kind"].replace("pos_near_", "")
                        if raise_trigger("position", t, f"near_{side}:{t}:{today}",
                                         {"near": side, "message": ev.get("message"), **_loads(ev.get("value_json"))},
                                         prio=1, now=now):
                            n += 1
                elif c["move"].get("enabled"):
                    tr = move_qualifies(ev, bk, c)
                    if tr and raise_trigger(tr["trigger"], tr["ticker"], tr["key"], tr["payload"], prio=tr["prio"], now=now):
                        n += 1
        except Exception as e:
            log.info(f"[desk_check] watch-event collect: {e}")
    if hours_ok and c["position"].get("enabled"):
        near = float(c["position"].get("near_pct", 1.5))
        rth = "09:35" <= _hm(et) < "16:00"           # thin pre-market prints do not count as "near the stop"
        try:                                         # Mason's stock positions against their own levels
            for t, rows in (bk["mason"].items() if rth else []):
                for r in rows:
                    if r.get("occ"):
                        continue
                    side = near_level(_last_bar(t, now=now), r.get("stop"), r.get("target"), near)
                    if side and raise_trigger("position", t, f"near_{side}:{r.get('rec_id')}:{today}",
                                              {"near": side, "stop": r.get("stop"), "target": r.get("target"),
                                               "who": "Mason"}, prio=0, now=now):
                        n += 1
            for t, lots in (bk["nico"].items() if rth else []):   # Nico's stock positions against the journal levels
                for lot in lots:
                    if lot.get("is_option") or not (lot.get("stop") or lot.get("target")):
                        continue
                    side = near_level(_last_bar(t, now=now), lot.get("stop"), lot.get("target"), near)
                    if side and raise_trigger("position", t, f"near_{side}:nico:{t}:{today}",
                                              {"near": side, "stop": lot.get("stop"), "target": lot.get("target"),
                                               "who": "Nico"}, prio=1, now=now):
                        n += 1
        except Exception as e:
            log.info(f"[desk_check] level collect: {e}")
        try:                                         # earnings tonight / tomorrow morning for a held stock
            if _slot_due(et, c["position"].get("earnings_check_et", "15:20"), 60):
                nxt = _next_session_day(et)
                for t in sorted(set(bk["mason"]) | set(bk["nico"])):
                    due = _earnings_due(t, today, nxt)
                    if due and raise_trigger("event_today", t, f"event_today:{t}:{due['report_date']}",
                                             {"report_date": due["report_date"], "timing": due.get("timing"),
                                              "eps_est": due.get("eps_est")}, prio=1, now=now):
                        n += 1
            if bk["mason"] and _slot_due(et, c["position"].get("fed_check_et", "13:40"), 20):
                fed = _fed_today(today)
                if fed and raise_trigger("fed_today", None, f"fed:{today}", {"event": fed}, prio=1, now=now):
                    n += 1
        except Exception as e:
            log.info(f"[desk_check] scheduled-event collect: {e}")
    if _DIR().is_trading_day(et):
        p = c["passes"]
        for name in ("after_close", "before_open"):
            sub = p.get(name) or {}
            if sub.get("enabled") and _slot_due(et, sub.get("at_et"), p.get("window_min", 60)):
                if (bk["mason"] or bk["queued"] or bk["nico"]) and raise_trigger(name, None, f"{name}:{today}", {},
                                                                              prio=0, now=now):
                    n += 1
    return n


# ── what the model is shown ─────────────────────────────────────────────────────

def _prev_close(ticker, today):
    try:
        r = _rows("SELECT close, date FROM price_bars_daily WHERE ticker=? AND date < ? AND close IS NOT NULL "
                  "ORDER BY date DESC LIMIT 1", (ticker, today))
        return (_f(r[0]["close"]), r[0]["date"]) if r else (None, None)
    except Exception:
        return None, None


def _rth_close_today(ticker, et):
    try:
        day0 = et.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = _rows("SELECT c FROM watch_bars WHERE ticker=? AND tf='1m' AND session='rth' AND ts >= ? "
                  "ORDER BY ts DESC LIMIT 1", (ticker, day0))
        return _f(r[0]["c"]) if r else None
    except Exception:
        return None


def price_facts(ticker, et) -> dict:
    """Current price and today's move for one stock. Quote first (mason_quotes: Robinhood,
    Alpaca as fallback), the live tape for the move and volume. Missing pieces stay None."""
    out = {"ticker": ticker, "px": None, "src": None, "age_s": None, "prev_close": None, "day_chg_pct": None,
           "ext_chg_pct": None, "rvol": None, "atr_pct": None, "atr_mult": None, "vs_spy_pct": None}
    try:
        q = _quote(ticker) or {}
    except Exception:
        q = {}
    st = _tape(ticker) or {}
    if q.get("ok") and _f(q.get("px")):
        out.update(px=_f(q["px"]), src=str(q.get("source") or "quote"), age_s=_f(q.get("age_s")))
    elif _f(st.get("last")):
        out.update(px=_f(st["last"]), src="last one-minute bar", age_s=None)
    pc = _f(st.get("prior_close")) or _prev_close(ticker, _day(et))[0]
    out["prev_close"] = pc
    if out["px"] and pc:
        out["day_chg_pct"] = (out["px"] / pc - 1.0) * 100.0
    if _hm(et) >= "16:00" and out["px"]:
        rc = _rth_close_today(ticker, et)
        if rc:
            out["ext_chg_pct"] = (out["px"] / rc - 1.0) * 100.0
            out["rth_close"] = rc
    out["rvol"], out["atr_pct"] = _f(st.get("rvol")), _f(st.get("atr_pct"))
    if out["day_chg_pct"] is not None and out["atr_pct"]:
        out["atr_mult"] = abs(out["day_chg_pct"]) / out["atr_pct"]
    out["vs_spy_pct"] = _f(st.get("vs_spy_pct"))
    return out


def _price_line(p) -> str:
    t = p["ticker"]
    if not p.get("px"):
        return f"{t}: no reliable price right now."
    src = {"rh": "Robinhood quote", "robinhood": "Robinhood quote", "alpaca": "Alpaca quote"}.get(
        str(p.get("src") or "").lower(), str(p.get("src") or "quote"))
    age = f", {p['age_s']:.0f} seconds old" if p.get("age_s") is not None and p["age_s"] < 3600 else ""
    s = f"{t} {_px(p['px'])} ({src}{age})."
    if p.get("day_chg_pct") is not None:
        s += (f" Previous close {_px(p['prev_close'])}, so it is "
              f"{'up' if p['day_chg_pct'] >= 0 else 'down'} {abs(p['day_chg_pct']):.1f}% today.")
    if p.get("ext_chg_pct") is not None:
        s += f" Since today's 4pm close ({_px(p.get('rth_close'))}) it has moved {_pct(p['ext_chg_pct'])} after hours."
    if p.get("rvol") is not None:
        s += f" Volume is {p['rvol']:.1f} times normal for this time of day."
    if p.get("atr_pct"):
        s += f" A normal day's range for this stock is about {p['atr_pct']:.1f}%"
        s += (f", so today's move is {p['atr_mult']:.1f} normal days." if p.get("atr_mult") is not None else ".")
    return s


def _market_lines(et) -> list:
    out, bits = [], []
    for sym, name in (("SPY", "S&P 500 fund (SPY)"), ("QQQ", "Nasdaq 100 fund (QQQ)")):
        st = _tape(sym) or {}
        chg = _f(st.get("chg_pct_prev_close"))
        if chg is not None:
            bits.append(f"{name} {_pct(chg)} today")
    if bits:
        out.append("Market: " + ", ".join(bits) + ".")
    try:
        r = _rows("SELECT close, date FROM price_bars_daily WHERE ticker='VIX' AND close IS NOT NULL "
                  "ORDER BY date DESC LIMIT 1")
        if r and (et.date() - datetime.fromisoformat(r[0]["date"]).date()).days <= 5:
            out.append(f"The VIX (the market's fear gauge; under 15 is calm, over 25 is nervous) last closed at "
                       f"{_f(r[0]['close']):.1f} on {r[0]['date'][5:]}.")
    except Exception:
        pass
    # Overnight and pre-market read, short form (audit R6; switch overnight.desk_check_block.enabled; wording for
    # Nico's approval in docs/fable/OVERNIGHT_PROMPTS_FOR_NICO.md). Before the 09:30 open only.
    if ((_load().get("overnight") or {}).get("desk_check_block") or {}).get("enabled") is True:
        try:
            import mason_overnight as OVN
            out += OVN.desk_check_lines(et)
        except Exception:
            pass
    return out


def _session_words(et) -> str:
    D = _DIR()
    hm = _hm(et)
    if not D.is_trading_day(et):
        return f"the market is closed today; it next opens {_next_session_day(et)} 09:30"
    if hm < "09:30":
        return "before the open; the market opens at 09:30"
    if hm < "16:00":
        return "the market is open"
    return f"after the close; the market next opens {_next_session_day(et)} 09:30"


def _mason_line(r, px=None) -> str:
    opt = bool(r.get("occ"))
    what = (f"{_qty(r.get('qty'))} {r.get('vehicle')} contract(s) {r.get('occ')}" if opt
            else f"{_qty(r.get('qty'))} shares")
    unit = " (option price)" if opt else ""
    levels = (f"Stop {_px(r.get('stop')) if r.get('stop') is not None else 'none'}, "
              f"target {_px(r.get('target')) if r.get('target') is not None else 'none'}{unit}.")
    if opt and '"short_as_put"' in str(r.get("features_json") or ""):
        try:                                   # a put bought for a short call: wording only when Nico switched it on
            import mason_short_put as SP       # (autotrade.short_as_put.tell_quick_checks); otherwise the line is as before
            levels = SP.check_words(_loads(r.get("features_json")) or {}, _autotrade_cfg()) or levels
        except Exception:
            pass
    s = (f"- Mason [{r.get('rec_id')}]: {what}, bought {str(r.get('entry_ts') or '')[:10]} at {_px(r.get('entry_px'))}{unit}. "
         f"{levels} "
         f"Planned hold: {r.get('horizon') or '?'}, to be closed by {str(r.get('time_stop_date') or '?')[:10]} at the latest.")
    if r.get("thesis"):
        s += f" Reason for the trade: \"{str(r['thesis'])[:220]}\""
    return s


def _queued_line(q) -> str:
    veh = (f"{q['vehicle']} {q.get('strike')} expiring {q.get('expiry')}" if q.get("vehicle") in ("call", "put")
           else "stock")
    return (f"- Mason, order waiting for the open [{q['rec_id']}]: {q.get('direction')} via {veh}, decided {q.get('created')}. "
            f"Stop {_px(q.get('stop')) if q.get('stop') is not None else 'none'}, "
            f"target {_px(q.get('target')) if q.get('target') is not None else 'none'}. "
            f"Reason: \"{q.get('thesis') or ''}\"")


def _nico_line(lot, px=None) -> str:
    what = f"{_qty(lot.get('qty'))} option contract(s) {lot.get('symbol')}" if lot.get("is_option") \
        else f"{_qty(lot.get('qty'))} share{'' if (lot.get('qty') or 0) == 1 else 's'}"
    s = f"- Nico: {what} in the {lot.get('where')}"
    if lot.get("entry"):
        s += f", bought at about {_px(lot['entry'])}"
        if px and not lot.get("is_option"):
            s += f" ({_pct((px / lot['entry'] - 1) * 100)} since)"
    s += ". " + (f"Stop on record {_px(lot['stop'])}." if lot.get("stop") else "No stop on record.")
    if lot.get("thesis"):
        s += f" His reason: \"{lot['thesis']}\""
    return s


def _position_lines(bk, t, px=None) -> list:
    L = [_mason_line(r, px) for r in bk["mason"].get(t, [])]
    L += [_queued_line(q) for q in bk["queued"].get(t, [])]
    L += [_nico_line(lot, px) for lot in bk["nico"].get(t, [])]
    if not L:
        w = bk["watch"].get(t)
        L.append("- Nobody holds it."
                 + (f" It is on the watchlist (added {w.get('added')} by {'Nico' if w.get('origin') == 'nico' else 'the director'}"
                    + (f": {w['reason']}" if w.get("reason") else "") + ")." if w else ""))
    return L


def _director_view(t) -> str:
    try:
        r = _rows("SELECT created_date, direction, status, extra_json FROM recommendations WHERE kind='director' "
                  "AND created_by='opus_director' AND ticker=? AND COALESCE(record_deleted,0)=0 "
                  "ORDER BY created_date DESC LIMIT 1", (t,))
        if not r:
            return "The director has not given a view on this stock."
        ex = _loads(r[0].get("extra_json"))
        conf = _f(ex.get("confidence"))
        return (f"{str(r[0]['created_date'])[:16].replace('T', ' ')}: {r[0].get('direction')} {t}"
                + (f", confidence {conf:.0%}" if conf is not None else "") + f". \"{str(ex.get('prediction') or '')[:260]}\"")
    except Exception:
        return "The director's last view is not available."


def _schedule_lines(t, et) -> list:
    D, L = _DIR(), []
    if t:
        ne = _next_earnings(t, _day(et))
        if ne:
            when = {"am": "before the open", "pm": "after the close"}.get(str(ne.get("timing") or "").lower(), "time not known")
            L.append(f"{t} reports earnings on {ne['report_date']} ({when}).")
    # Events calendar lines (wording approved by Nico 2026-11-15; switch calendar.desk_check_block.enabled)
    if ((_load().get("calendar") or {}).get("desk_check_block") or {}).get("enabled") is True:
        try:
            import mason_calendar as CAL
            L += (CAL.calendar_lines(tickers=[t], days=5, max_lines=6, min_importance=3, only_tickers=True,
                                     skip_types=("earnings",)) if t else CAL.calendar_lines(days=5, max_lines=8))
        except Exception:
            pass
    mins = D.minutes_to_next_anchor(et)
    if mins != float("inf"):
        L.append(f"Next scheduled director review: today, in {int(mins // 60)}h{int(mins % 60):02d}m.")
    else:
        times = sorted(D._anchor_times())
        L.append(f"Next scheduled director review: {_next_session_day(et)} at {times[0] if times else '07:00'} ET.")
    return L


def _why_lines(tr, c) -> list:
    p, kind, t = tr.get("payload") or {}, tr["trigger"], tr.get("ticker")
    L = []
    if kind == "news":
        k0 = str((p.get("strong_kinds") or [p.get("kind")])[0] or "news")
        label = _KIND_WORDS.get(k0, k0.replace("_", " "))
        L.append(f"News on {t}: {label}. Mason's news desk rates the story {(_f(p.get('score')) or 0):.2f} on a 0-1 scale "
                 f"(0.70 and up reaches Nico's inbox).")
        heads = _headlines(catalyst_id=p.get("catalyst_id"), limit=int(c["news"].get("max_headlines", 6)))
        if heads:
            L.append("Headlines, newest first (Eastern time):")
            L += [f"- {_clock_words(h['at'])} {h.get('source') or '?'}: {h['text']}" for h in heads]
        L.append(f"{p.get('n_events') or len(heads)} headline(s) from {p.get('n_sources') or '?'} source(s) since "
                 f"{_clock_words(p.get('first_seen'))}.")
    elif kind == "move":
        L.append(f"{t} is moving sharply with no fresh story in Mason's news feed: {p.get('message') or ''} "
                 + (f"({(_f(p.get('atr_mult')) or 0):.1f} normal days' range" if p.get("atr_mult") else "(")
                 + (f", volume {p['rvol']:.1f} times normal)." if p.get("rvol") else ")."))
        heads = _headlines(ticker=t, since=time.time() - 6 * 3600, limit=3)
        if heads:
            L.append("Most recent headlines that mention it:")
            L += [f"- {_clock_words(h['at'])} {h.get('source') or '?'}: {h['text']}" for h in heads]
    elif kind == "position":
        whose = f"{p['who']}'s position" if p.get("who") else "A position"
        L.append(f"{whose} in {t} is close to its {p.get('near')} "
                 f"(stop {_px(p.get('stop')) if p.get('stop') else 'none'}, target {_px(p.get('target')) if p.get('target') else 'none'}).")
    elif kind == "event_today":
        when = "before tomorrow's open" if p.get("timing") == "am" else "after today's close"
        L.append(f"{t} reports earnings {when} ({p.get('report_date')}) and there is an open position in it. "
                 + (f"Analysts expect earnings of {_px(p['eps_est'])} per share. " if p.get("eps_est") is not None else "")
                 + "Decide whether to hold through the report.")
    elif kind == "earnings":
        act, est, sp = _f(p.get("eps_act")), _f(p.get("eps_est")), _f(p.get("surprise_pct"))
        L.append(f"{t} just reported earnings ({p.get('fiscal_period') or ''}): {_px(act)} per share against "
                 f"{_px(est)} expected"
                 # (review fix R7) no percentage against an expected value near zero: 0.29 against
                 # -0.01 is "a beat of 3000%" by the formula and tells the reader nothing
                 + ((f", a {'beat' if sp >= 0 else 'miss'} of {abs(sp):.0f}%" if (est is not None and abs(est) >= 0.05 and abs(sp) <= 300)
                     else f", a {'beat' if sp >= 0 else 'miss'}") if sp is not None else "")
                 + ". Revenue is not in Mason's data.")
    return L


def build_context(group: list, bk: dict, et, c) -> dict:
    """The user message for one check. Returns {"user", "summary", "allowed", "ref_px", "facts"}."""
    first = group[0]
    kind, t = first["trigger"], first.get("ticker")
    head = f"QUICK CHECK · {et.strftime('%a %Y-%m-%d %H:%M')} Eastern · {_session_words(et)}"
    L, ref_px, allowed = [head, ""], {}, set()
    if t:                                                   # one stock
        p = price_facts(t, et)
        ref_px[t] = p.get("px")
        allowed.add(t)
        L.append("WHAT HAPPENED")
        for tr in group:
            L += _why_lines(tr, c)
        L += ["", "PRICE", _price_line(p)] + _market_lines(et)
        L += ["", "POSITIONS IN THIS STOCK"] + _position_lines(bk, t, p.get("px"))
        L += ["", "THE DIRECTOR'S LAST VIEW", _director_view(t)]
        L += ["", "WHAT IS SCHEDULED"] + _schedule_lines(t, et)
        L += _holdings_and_limits(t)
        L += ["", f"Give one decision for {t}."]
        summary = f"{kind} on {t}: " + "; ".join(str((tr.get('payload') or {}).get('title') or (tr.get('payload') or {}).get('message')
                                                      or tr['trigger'])[:120] for tr in group)
    else:                                                   # the whole book
        tickers = sorted(set(bk["mason"]) | set(bk["queued"]) | set(bk["nico"]))
        ask = {"after_close": "END OF DAY. Say what reported, what moved after hours, and what should be done at tomorrow's open.",
               "before_open": "BEFORE THE OPEN. Say what changed overnight for the positions and for the orders waiting for the open.",
               "fed_today": f"FED DAY. {(first.get('payload') or {}).get('event') or 'A Federal Reserve decision is due today'}. "
                            "Say whether any of Mason's positions should be reduced before it."}.get(kind, kind)
        L += ["WHY YOU ARE BEING ASKED", ask, ""] + _market_lines(et)
        if kind == "before_open":
            try:
                ev = _rows("SELECT title, event_time, details_json FROM calendar_events WHERE event_type='econ' "
                           "AND event_date=? AND record_deleted=0 ORDER BY event_time LIMIT 6", (_day(et),))
                if ev:
                    L.append("Economic releases today (Mason does not have the actual numbers; judge from the market's move):")
                    for e in ev:
                        d = _loads(e.get("details_json"))
                        L.append(f"- {str(e.get('event_time') or '')[:5]} {e.get('title')}"
                                 + (f", expected {d['forecast']}, last {d.get('previous')}" if d.get("forecast") else ""))
            except Exception:
                pass
        L += ["", "MASON'S POSITIONS AND WAITING ORDERS"]
        thr = float(c["passes"].get("nico_min_move_pct", 3.0))
        quiet, nico_lines = 0, []
        for tk in tickers:
            p = price_facts(tk, et)
            ref_px[tk] = p.get("px")
            mine = bk["mason"].get(tk) or bk["queued"].get(tk)
            ne = _earnings_due(tk, _day(et), _next_session_day(et))
            rep = _reported_today(tk, _day(et))
            extra = (f" Reports earnings {ne['report_date']} ({ne.get('timing') or 'time not known'})." if ne else "") \
                + (f" Reported today: {_px(rep['eps_act'])} per share against {_px(rep['eps_est'])} expected." if rep else "")
            if mine:
                allowed.add(tk)
                L.append(_price_line(p) + extra)
                L += [_mason_line(r, p.get("px")) for r in bk["mason"].get(tk, [])]
                L += [_queued_line(q) for q in bk["queued"].get(tk, [])]
                L += [_nico_line(lot, p.get("px")) for lot in bk["nico"].get(tk, [])]
            else:
                big = max(abs(p.get("day_chg_pct") or 0.0), abs(p.get("ext_chg_pct") or 0.0)) >= thr
                if big or ne or rep:
                    allowed.add(tk)
                    nico_lines.append(_price_line(p) + extra)
                    nico_lines += [_nico_line(lot, p.get("px")) for lot in bk["nico"].get(tk, [])]
                else:
                    quiet += 1
        if not (bk["mason"] or bk["queued"]):
            L.append("- none")
        L += ["", "NICO'S POSITIONS WITH SOMETHING GOING ON"] + (nico_lines or ["- none"])
        if quiet:
            L.append(f"({quiet} other position(s) of Nico's moved less than {thr:g}% and have no report due; not shown.)")
        L += ["", "WHAT IS SCHEDULED"] + _schedule_lines(None, et)
        L += _holdings_and_limits(None)
        L += ["", "Give a decision only for the stocks that need one. Leave out the ones where nothing changes."]
        summary = f"{kind}: {len(allowed)} stock(s) shown"
    user = "\n".join(L)
    facts = "\n".join(L[2:-2]) if t else ""
    return {"user": user, "summary": summary[:400], "allowed": allowed, "ref_px": ref_px, "facts": facts}


def _reported_today(ticker, today) -> dict | None:
    try:
        r = _rows("SELECT eps_est, eps_act, surprise_pct, timing FROM earnings_results WHERE ticker=? "
                  "AND report_date=? AND eps_act IS NOT NULL LIMIT 1", (ticker, today))
        return r[0] if r else None
    except Exception:
        return None


# ── the answer ──────────────────────────────────────────────────────────────────

def parse_output(text) -> tuple:
    """(obj | None, error | None). Accepts the bare JSON the API guarantees; tolerates a
    fenced block so a fake or a replay with free text still parses."""
    s = str(text or "").strip()
    if not s:
        return None, "empty answer"
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S)
    if m:
        s = m.group(1)
    elif not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None, "no JSON object in the answer"
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except Exception as e:
        return None, f"bad JSON: {e}"
    return (obj, None) if isinstance(obj, dict) else (None, "the answer is not a JSON object")


def validate(obj, allowed: set, *, single: str | None = None) -> tuple:
    """(decisions, error). Any malformed decision makes the WHOLE answer invalid: on an
    invalid answer nothing is done. `single` = the one stock a single-stock check is about."""
    if not isinstance(obj, dict) or not isinstance(obj.get("decisions"), list):
        return [], "no decisions list"
    out, seen = [], set()
    for d in obj["decisions"]:
        if not isinstance(d, dict):
            return [], "a decision is not an object"
        t = str(d.get("ticker") or "").upper().strip()
        a = str(d.get("action") or "").lower().strip()
        reason = " ".join(str(d.get("reason") or "").split())
        if a not in ACTIONS:
            return [], f"unknown action {a!r}"
        if not t or t not in allowed:
            return [], f"decision for {t or '?'}, which was not in the check"
        if t in seen:
            return [], f"two decisions for {t}"
        if not reason:
            return [], f"no reason given for {t}"
        seen.add(t)
        lvl = _f(d.get("stop_level"))
        if a == "tighten_stop" and (lvl is None or lvl <= 0):
            return [], f"tighten_stop for {t} without a usable stop_level"
        direction = str(d.get("direction") or "").lower().strip() or None
        if direction not in (None, "long", "short"):
            return [], f"direction {direction!r} for {t}"
        note = " ".join(str(d.get("note") or "").split()) or None
        if note and len(note) > 600:               # keep whole sentences
            cut = note[:600]
            note = cut[:cut.rfind(". ") + 1] if ". " in cut[200:] else cut
        out.append({"ticker": t, "action": a, "reason": reason[:300], "stop_level": lvl if (lvl and lvl > 0) else None,
                    "direction": direction, "note": note})
    if single is not None and [d["ticker"] for d in out] != [single]:
        return [], f"expected exactly one decision, for {single}"
    return out, None


# ── acting on a decision ────────────────────────────────────────────────────────

def _exec_state(rec_id):
    try:
        r = _rows("SELECT extra_json FROM recommendations WHERE id=?", (rec_id,))
        return (_loads(r[0]["extra_json"]).get("execution") or {}) if r else {}
    except Exception:
        return {}


def _can_act(c) -> tuple:
    at = _autotrade_cfg()
    if not c.get("apply_actions", True):
        return False, "acting on desk checks is switched off (desk_check.apply_actions)"
    if not at.get("auto_place"):
        return False, "automatic trading is switched off (autotrade.auto_place)"
    if not (at.get("exits") or {}).get("enabled"):
        return False, "Mason's exit manager is switched off (autotrade.exits.enabled)"
    return True, ""


def _closes_today(day) -> int:
    """Sales and cancelled waiting orders that checks have caused today (stored outcomes)."""
    n = 0
    try:
        for r in _rows("SELECT outcome_json FROM desk_checks WHERE day=? AND outcome_json IS NOT NULL", (day,)):
            n += sum(1 for o in _loads(r.get("outcome_json"), []) if isinstance(o, dict)
                     and o.get("action") == "close" and o.get("done"))
    except Exception:
        return 10 ** 6                              # cannot count: allow none
    return n


def _close_mason(check_id, t, d, bk, c, notifier, closed_now: int = 0) -> dict:
    recs = [r.get("rec_id") for r in bk["mason"].get(t, []) if r.get("rec_id")] \
        + [q["rec_id"] for q in bk["queued"].get(t, [])]
    ok, why = _can_act(c)
    if ok:                                          # (review fix R2) a daily limit on what checks may sell
        mx = int(c.get("max_closes_per_day", 2))
        if _closes_today(_day(_now_et())) + int(closed_now) >= mx:
            ok, why = False, (f"quick checks have already closed {mx} position{'' if mx == 1 else 's'} today, the most "
                              f"allowed in one day (desk_check.max_closes_per_day); the director decides at the next review")
    if not ok:
        _notify(notifier, f"{t}: a quick check wanted to sell Mason's position; nothing was done",
                f"Why it wanted to sell: {d['reason']}\nNothing was done because {why}.",
                ticker=t, severity="warn", dedup=f"desk_check:{check_id}:{t}", ref_id=check_id)
        return {"done": False, "why": why}
    _DIR()._apply_updates([{"rec_id": rid, "action": "close", "note": f"desk check {check_id}: {d['reason']}"}
                           for rid in recs], f"check:{check_id}")
    applied = [rid for rid in recs if (_exec_state(rid).get("close_requested")
                                       or _exec_state(rid).get("state") == "cancelled_by_director")]
    if not applied:
        return {"done": False, "why": "the position is not in a state the exit manager can close", "recs": recs}
    waiting = bool(bk["queued"].get(t)) and not bk["mason"].get(t)
    _notify(notifier, (f"Mason cancelled its waiting order in {t}" if waiting else f"Mason is selling its {t} position"),
            f"Why: {d['reason']}\n"
            + ("The order that was waiting for the open will not be placed."
               if waiting else
               "Mason sells only what it bought itself, at its next price check while the market is open "
               "(within about three minutes), or at the next open if the market is closed now. "
               "Anything you hold in this stock is not touched.")
            + "\nDecided by a quick check between the director's two daily reviews.",
            ticker=t, severity="warn", dedup=f"desk_check:{check_id}:{t}", ref_id=check_id)
    return {"done": True, "recs": applied}


def _tighten_mason(check_id, t, d, bk, c, notifier, px) -> dict:
    rows = bk["mason"].get(t, [])
    if len(rows) != 1:
        return {"done": False, "why": f"Mason has {len(rows)} positions in {t}; a stop can be moved only when there is exactly one"}
    r, lvl = rows[0], float(d["stop_level"])
    ok, why = _can_act(c)
    if not ok:
        return {"done": False, "why": why}
    opt = bool(r.get("occ"))
    # (review fix R2) The level is judged on a FRESH price fetched now, never on the price the model was
    # shown (a relaxed quote up to 15 minutes old, a one-minute bar of unknown age, or, after a restart,
    # the price stored with the check): a stop set above the real price is sold at the next price check.
    # Only while the market is open: outside it there is no price a stop can be judged on.
    et = _now_et()
    if not (_DIR().is_trading_day(et) and "09:35" <= _hm(et) < "15:55"):
        return {"done": False, "why": "a stop is only moved while the market is open (09:35 to 15:55 Eastern)"}
    try:
        q = (_strict_option_quote(r["occ"]) if opt else _strict_quote(t)) or {}
    except Exception:
        q = {}
    cur = (_f(q.get("mid")) if opt else _f(q.get("px"))) if q.get("ok") else None
    old = _f(r.get("stop"))
    if cur is None:
        return {"done": False, "why": "no reliable price right now to check the new stop against"}
    if lvl >= cur * (1 - float(c.get("tighten_min_gap_pct", 0.5)) / 100.0):
        return {"done": False, "why": f"a stop at {_px(lvl)} is at or above the price {_px(cur)} and would sell at once"}
    if old is not None and lvl <= old:
        return {"done": False, "why": f"{_px(lvl)} is not above the stop already in place ({_px(old)})"}
    execu = _exec_state(r.get("rec_id"))
    at_broker = bool((execu.get("exit_rules") or {}).get("enforced_at_broker")) and not opt
    now_iso = datetime.now().isoformat(timespec="seconds")
    if at_broker:
        # The broker holds this stop as part of the entry order and Mason has no code path that
        # moves it. The higher level is kept here and judged on every pass (watch_stops); when
        # the price reaches it the position is closed the same way a director close is.
        _exec("INSERT INTO desk_check_stops(rec_id, ticker, level, set_at, check_id, state) VALUES (?,?,?,?,?,'active') "
              "ON CONFLICT(rec_id) DO UPDATE SET level=excluded.level, set_at=excluded.set_at, "
              "check_id=excluded.check_id, state='active', ended_at=NULL, note=NULL", (r["rec_id"], t, lvl, now_iso, check_id))
        how = ("The stop order at the broker stays where it is as a backstop. Mason itself now watches the higher "
               "level and sells if the price reaches it.")
    else:
        n = _exec("UPDATE agent_trades SET stop=? WHERE id=? AND source='mason' AND status='open'", (lvl, r["id"]))
        if not n:
            return {"done": False, "why": "the position is no longer open"}
        how = "Mason's exit manager uses the new level from its next price check."
    unit = " (price of the option)" if opt else ""
    _notify(notifier, f"Mason raised its stop on {t} to {_px(lvl)}",
            f"Why: {d['reason']}\nThe stop was {_px(old) if old is not None else 'not set'}; the price is {_px(cur)}{unit}. "
            f"Nothing was sold. {how}\nDecided by a quick check between the director's two daily reviews.",
            ticker=t, dedup=f"desk_check:{check_id}:{t}", ref_id=check_id)
    return {"done": True, "old": old, "new": lvl, "watched_here": at_broker, "rec_id": r.get("rec_id")}


def _recommend(check_id, t, d, bk, notifier, px, trig_words) -> dict:
    lots = bk["nico"].get(t, [])
    stock = [lot for lot in lots if not lot.get("is_option")]
    qty = sum(lot.get("qty") or 0 for lot in stock)
    cost = sum((lot.get("qty") or 0) * (lot.get("entry") or 0) for lot in stock if lot.get("entry"))
    head = ""
    if qty:
        entry = (cost / qty) if cost else None
        head = f"You hold {_qty(qty)} {t} shares" + (f", bought at about {_px(entry)}" if entry else "") + "."
        if px:
            head += f" The price is {_px(px)}" + (f" ({_pct((px / entry - 1) * 100)})." if entry else ".")
    elif lots:
        head = f"You hold {len(lots)} option position(s) in {t}."
    body = "\n".join(x for x in [head, d.get("note") or d["reason"],
                                 (f"Suggested stop: {_px(d['stop_level'])}." if d.get("stop_level") else ""),
                                 f"What prompted this: {trig_words}",
                                 "Mason has not changed anything. This is a suggestion from a quick check between the "
                                 "director's two daily reviews."] if x)
    title = f"{t}: a suggestion on the position you hold"
    done_by = "note"
    try:                                # Accept flow, when mason_protect offers the hook (see report)
        import mason_protect
        hook = getattr(mason_protect, "suggest_stop_from_check", None)
        if callable(hook) and d.get("stop_level") and stock:
            if hook(t, float(d["stop_level"]), title=title, body=body, ref_px=px, key=f"check:{check_id}:{t}"):
                done_by = "note with Accept"
    except Exception as e:
        log.info(f"[desk_check] protect hook unavailable: {e}")
    if done_by == "note":
        _notify(notifier, title, body, ticker=t, dedup=f"desk_check:{check_id}:{t}:note", ref_id=check_id)
    return {"done": True, "how": done_by}


def _not_run(check, d, c, notifier, why, *, handoff) -> dict:
    """A full decision was asked for and is not run on its own. With desk_check.notes_to_director
    on, the next scheduled review is shown the request; otherwise Nico is only told."""
    handed = bool(handoff and c.get("notes_to_director"))
    if handoff:
        tail = ("It is put in front of the director at the next scheduled review." if handed else
                "It was not passed on either: showing these to the director is switched off "
                "(desk_check.notes_to_director).")
        _notify(notifier, f"{d['ticker']}: a quick check asked for a full director decision; it was not run on its own",
                f"Why it asked: {d['reason']}\nNot run on its own because {why}. {tail}", ticker=d["ticker"],
                dedup=f"desk_check:{check['id']}:{d['ticker']}", ref_id=check["id"])
    return {"done": False, "state": "handed_off" if handed else "skipped", "why": why}


def _escalate(check, d, c, notifier, facts, et=None) -> dict:
    D = _DIR()
    et = et or _now_et()
    ok, why = _can_act(c)
    if not ok:
        _notify(notifier, f"{d['ticker']}: a quick check asked for a full director decision; it was not requested",
                f"Why it asked: {d['reason']}\nNot requested because {why}.", ticker=d["ticker"],
                dedup=f"desk_check:{check['id']}:{d['ticker']}", ref_id=check["id"])
        return {"done": False, "state": "skipped", "why": why}
    gate, gwhy = D.escalation_gate(et=et)
    if gate != "run":
        return _not_run(check, d, c, notifier, gwhy, handoff=(gate == "handoff"))
    # (review fix R4) the director's own count reads the ledger, which is written only when a call comes
    # back; a call killed by a restart was paid for and leaves no row. Count what was STARTED as well.
    try:
        started = int(_rows("SELECT COUNT(*) AS n FROM desk_checks WHERE day=? AND id<>? AND escalation_state IN "
                            "('started','done','error')", (_day(et), check["id"]))[0]["n"])
        mx = int(D.escalation_cfg().get("max_per_day", 3))
    except Exception:
        started, mx = 0, 1
    if started >= mx:
        return _not_run(check, d, c, notifier, f"{started} such decisions have already been started today, the most "
                                               f"allowed in one day", handoff=True)
    _exec("UPDATE desk_checks SET escalation_state='started' WHERE id=?", (check["id"],))
    res = D.run_escalation({"id": check["id"], "ticker": d["ticker"], "trigger": check.get("trigger"),
                            "reason": d["reason"], "direction": d.get("direction"), "facts": facts,
                            "catalyst_id": (check.get("payload") or {}).get("catalyst_id"),
                            "at": et.strftime("%H:%M")}, et=et, notifier=notifier) or {}
    if res.get("handoff"):
        return _not_run(check, d, c, notifier, str(res.get("skipped")), handoff=True)
    if res.get("skipped") or res.get("error"):
        why = str(res.get("skipped") or res.get("error"))
        _notify(notifier, f"{d['ticker']}: a quick check asked for a full director decision; it could not be run",
                f"Why it asked: {d['reason']}\nIt was not run: {why}", ticker=d["ticker"], severity="warn",
                dedup=f"desk_check:{check['id']}:{d['ticker']}", ref_id=check["id"])
        return {"done": False, "state": "skipped" if res.get("skipped") else "error", "why": why}
    return {"done": True, "state": "done", "slate_id": res.get("slate_id"), "cost_usd": res.get("cost_usd"),
            "calls": res.get("n_predictions"), "rec_ids": res.get("rec_ids")}


def apply_decisions(check: dict, decisions: list, bk: dict, c: dict, notifier, ctx: dict, *,
                    resume: bool = False) -> list:
    """Do what each decision says, or record in plain words why it was not done. A
    decision the holder rule does not allow is turned into the nearest allowed one."""
    out, escalated, closed_now = [], False, 0
    for d in decisions:
        t, a = d["ticker"], d["action"]
        mine = bool(bk["mason"].get(t) or bk["queued"].get(t))
        his = bool(bk["nico"].get(t))
        px = (ctx.get("ref_px") or {}).get(t)
        res = {"ticker": t, "asked": a, "action": a, "ref_px": px}
        try:
            if a in ("close", "tighten_stop") and not mine:
                a = res["action"] = "recommend" if his else "watch"
                res["changed"] = "this is not Mason's position, so it became " + ("a note to Nico" if his else "a watch note")
            if a == "tighten_stop" and not bk["mason"].get(t):
                a = res["action"] = "watch"
                res["changed"] = "the order has not been placed yet, so there is no stop to move"
            if a == "recommend" and not his:
                a = res["action"] = "watch"
                res["changed"] = "Nico does not hold this, so there is nobody to advise"
            if a == "close":
                res.update(_close_mason(check["id"], t, d, bk, c, notifier, closed_now))
                closed_now += 1 if res.get("done") else 0
            elif a == "tighten_stop":
                res.update(_tighten_mason(check["id"], t, d, bk, c, notifier, px))
            elif a == "recommend":
                res.update(_recommend(check["id"], t, d, bk, notifier, px, ctx.get("summary") or ""))
            elif a == "escalate":
                if resume:
                    res.update(_not_run(check, d, c, notifier, "a restart interrupted the check", handoff=True))
                elif escalated:
                    res.update({"done": False, "state": "skipped", "why": "one full decision per check"})
                else:
                    escalated = True
                    res.update(_escalate(check, d, c, notifier, ctx.get("facts") or ctx.get("user") or "",
                                         ctx.get("et")))
            else:
                res["done"] = True
            if mine and his and d.get("note") and a in ("close", "tighten_stop"):
                _recommend(check["id"], t, dict(d, action="recommend"), bk, notifier, px, ctx.get("summary") or "")
                res["also_note_to_nico"] = True
        except Exception as e:
            res.update({"done": False, "why": f"error: {type(e).__name__}: {e}"})
            log.warning(f"[desk_check] applying {a} on {t} failed: {e}")
        out.append(res)
    return out


# ── stops raised on positions whose stop sits at the broker ─────────────────────

def watch_stops(et, c, notifier=None) -> list:
    """A stop a check raised on a stock whose own stop is a broker order is judged here,
    on a trustworthy quote, with the exit manager's rule (last trade at or under the
    level, and the bid too when the quote is two-sided). When reached, the position is
    closed through the director-close path. Never raises."""
    out = []
    try:
        rows = _rows("SELECT * FROM desk_check_stops WHERE state='active'")
        if not rows:
            return out
        D = _DIR()
        for s in rows:
            open_row = _rows("SELECT id FROM agent_trades WHERE rec_id=? AND source='mason' AND status='open'",
                             (s["rec_id"],))
            if not open_row or _exec_state(s["rec_id"]).get("close_requested"):
                _exec("UPDATE desk_check_stops SET state='ended', ended_at=? WHERE rec_id=?",
                      (datetime.now().isoformat(timespec="seconds"), s["rec_id"]))
                continue
            if not (D.is_trading_day(et) and "09:35" <= _hm(et) < "16:00") or not _can_act(c)[0]:
                continue
            try:
                q = _strict_quote(s["ticker"]) or {}
            except Exception:
                q = {}
            px, bid = _f(q.get("px")), _f(q.get("bid"))
            if not (q.get("ok") and px):
                continue
            hit = px <= s["level"] and (not (q.get("two_sided") and bid) or bid <= s["level"])
            # (review fix R5) two readings at least a minute apart: a close asked for here is final (the
            # exit manager does not re-check a director close), so one print must not be enough
            seen = _f(str(s.get("note") or "").partition("first_hit:")[2])
            now_s = et.timestamp()
            if not hit:
                if seen is not None:
                    _exec("UPDATE desk_check_stops SET note=NULL WHERE rec_id=?", (s["rec_id"],))
                continue
            if seen is None or now_s < seen or now_s - seen > 900:
                _exec("UPDATE desk_check_stops SET note=? WHERE rec_id=?", (f"first_hit:{now_s}", s["rec_id"]))
                continue
            if now_s - seen < 60:
                continue
            D._apply_updates([{"rec_id": s["rec_id"], "action": "close",
                               "note": f"stop raised by desk check {s['check_id']} to {s['level']} was reached at {px}"}],
                             f"check:{s['check_id']}")
            _exec("UPDATE desk_check_stops SET state='hit', ended_at=?, note=? WHERE rec_id=?",
                  (datetime.now().isoformat(timespec="seconds"), f"px {px}", s["rec_id"]))
            _notify(notifier, f"Mason is selling its {s['ticker']} position: the raised stop was reached",
                    f"The stop a quick check raised to {_px(s['level'])} was reached (price {_px(px)}). "
                    f"Mason sells its own shares at its next price check. Anything you hold is not touched.",
                    ticker=s["ticker"], severity="warn", dedup=f"desk_check_stop:{s['rec_id']}")
            out.append({"rec_id": s["rec_id"], "ticker": s["ticker"], "hit_at": px})
    except Exception as e:
        log.warning(f"[desk_check] watch_stops: {e}")
    return out


# ── limits ──────────────────────────────────────────────────────────────────────

def _counts_today(day) -> dict:
    """Event checks started today (the fixed passes are not counted): all, on price moves, and on
    stocks nobody holds or watches."""
    out = {"all": 0, "move": 0, "other": 0}
    try:
        for r in _rows("SELECT trigger, holder FROM desk_checks WHERE day=? AND status IN "
                       "('running','decided','done','invalid','error','interrupted')", (day,)):
            if r["trigger"] in _PASSES:
                continue
            out["all"] += 1
            out["move"] += r["trigger"] == "move"
            out["other"] += r["holder"] == "none"
    except Exception:
        pass
    return out


def count_limit(trigger, holder, n: dict, c) -> tuple:
    """(code, plain sentence) of the daily COUNT limit that stops this check, or (None, '').
    Pure, so the replay tool applies exactly the live rule."""
    if trigger in _PASSES:
        return None, ""
    if n["all"] >= int(c["max_checks_per_day"]):
        return "count", (f"Mason ran {n['all']} quick check{'' if n['all'] == 1 else 's'} today, the most allowed in one "
                         f"day (desk_check.max_checks_per_day).")
    if trigger == "move" and n["move"] >= int(c["move"].get("max_per_day", 6)):
        return "move_count", (f"Mason ran {n['move']} quick checks on sharp price moves today, the most allowed "
                              f"in one day (desk_check.move.max_per_day).")
    if holder == "none" and n["other"] >= int(c.get("max_other_checks_per_day", 4)):
        return "other_count", (f"Mason ran {n['other']} quick checks today on stocks nobody holds or watches, the "
                               f"most allowed in one day (desk_check.max_other_checks_per_day).")
    return None, ""


def caps_ok(et, c, trigger=None, holder=None) -> tuple:
    """(ok, code, plain sentence). Counts per day, dollars per day for checks, and the
    overall daily model budget with money kept back for the scheduled calls still to come."""
    D = _DIR()
    code, plain = count_limit(trigger, holder, _counts_today(_day(et)), c)
    if code:
        return False, code, plain
    s = D.spend(et)
    est = float(c.get("est_usd_per_check", 0.03))
    if s.get("check_usd", 0.0) + est > float(c["daily_usd_cap"]):
        return False, "check_usd", (f"Quick checks have cost ${s.get('check_usd', 0.0):.2f} today, and the limit for them is "
                                    f"${float(c['daily_usd_cap']):.2f} a day (desk_check.daily_usd_cap).")
    cap = D.overall_daily_cap()
    res = D.scheduled_reserve_usd(et)
    if s.get("all_day_usd", 0.0) + est + res > cap:
        return False, "overall", (f"Model spending today is ${s.get('all_day_usd', 0.0):.2f}; with ${res:.2f} kept back for the "
                                  f"scheduled director reviews still to come, the daily limit of ${cap:.2f} "
                                  f"(model_budget.daily_usd_cap) leaves no room for another check.")
    return True, "", ""


def _cap_note(code, plain, day, notifier):
    key = f"desk_check_cap_note:{code}"
    if _state_get(key) == day:
        return
    _state_set(key, day)
    title = {"move_count": "No more quick checks on price moves today: the daily limit was reached",
             "other_count": "No more quick checks today on stocks nobody holds or watches: the daily limit was reached"
             }.get(code, "Quick checks have stopped for today: a daily limit was reached")
    _notify(notifier, title,
            f"{plain}\nSuch events later today wait for the next scheduled director review. Positions, stops and "
            f"the scheduled reviews are not affected.", severity="warn", dedup=f"desk_check_cap:{code}:{day}")


# ── one check ───────────────────────────────────────────────────────────────────

def _finish_triggers(keys, state, check_id=None, note=None):
    for k in keys:
        _exec("UPDATE desk_check_triggers SET state=?, check_id=COALESCE(?, check_id), note=? WHERE key=?",
              (state, check_id, note, k))


def run_check(group: list, bk: dict, et, c, *, notifier=None, model_fn=None, now=None) -> dict:
    """One model call for one stock (or one pass over the book) and what follows from it."""
    D = _DIR()
    now_s = float(now if now is not None else time.time())
    first, keys = group[0], [g["key"] for g in group]
    t, cid = first.get("ticker"), uuid.uuid4().hex[:12]
    real = model_fn is None
    model_fn = model_fn or _model_call
    off = 0 if in_hours(et, c) else 1
    ctx = build_context(group, bk, et, c)
    ctx["et"] = et
    if not ctx["allowed"]:
        _finish_triggers(keys, "skipped", note="nothing to decide on")
        return {"id": None, "skipped": "nothing to decide on"}
    phash = hashlib.sha256((SYSTEM_PROMPT + json.dumps(OUTPUT_SCHEMA, sort_keys=True)).encode()).hexdigest()[:16]
    _exec("INSERT INTO desk_checks(id, created_at, created_epoch, day, trigger, trigger_key, ticker, holder, off_hours, "
          "status, trigger_json, input_summary, model, effort, prompt_hash, ref_px, attempts) "
          "VALUES (?,?,?,?,?,?,?,?,?,'running',?,?,?,?,?,?,?)",
          (cid, datetime.now().isoformat(timespec="seconds"), now_s, _day(et), first["trigger"], first["key"], t,
           (holder_of(bk, t) if t else "book"), off,
           json.dumps({"keys": keys, "triggers": group, "spy_ref": _f((_tape("SPY") or {}).get("last"))}, default=str),
           ctx["summary"], c["model"], c["effort"], phash, (ctx["ref_px"].get(t) if t else None),
           1 + max(int(g.get("attempts") or 0) for g in group)))
    _finish_triggers(keys, "checking", cid)
    if real and c.get("archive_prompts", True):
        try:                                         # same gzip archive the director's prompts go to
            D.save_slate_prompt(f"check_{cid}", system=SYSTEM_PROMPT, user=ctx["user"], tools=[], model=c["model"],
                                effort=c["effort"], mode="check", kind=first["trigger"],
                                extra={"output_schema": OUTPUT_SCHEMA, "ticker": t, "max_tokens": int(c["max_output_tokens"])})
        except Exception as e:
            log.info(f"[desk_check] prompt not archived: {e}")
    try:
        res = model_fn(SYSTEM_PROMPT, ctx["user"], c) or {}
    except Exception as e:
        why = (str(e) if getattr(e, "kind", None) else f"{type(e).__name__}: {e}")[:300]
        _exec("UPDATE desk_checks SET status='error', skip_reason=? WHERE id=?", (why, cid))
        for k in keys:                               # one more try on the next pass, then dropped
            _exec("UPDATE desk_check_triggers SET state=CASE WHEN attempts < 1 THEN 'pending' ELSE 'skipped' END, "
                  "attempts=attempts+1, check_id=?, note='model call failed' WHERE key=?", (cid, k))
        log.warning(f"[desk_check] {first['trigger']} {t or 'book'}: model call failed: {why}")
        day = _day(et)
        if _state_get("desk_check_error_note") != day:
            _state_set("desk_check_error_note", day)
            _notify(notifier, "A quick check could not reach the model",
                    f"The check on {t or 'the book'} was dropped ({why}). Nothing was changed. Further failures today "
                    f"are counted on the status page and not reported again.", severity="warn",
                    dedup=f"desk_check_error:{day}")
        return {"id": cid, "status": "error", "error": why}
    usage = res.get("usage") or {}
    cost = float(res.get("cost_usd") or 0.0)
    try:
        if cost > 0:
            D.record_spend(cost, mode="check", kind=f"check:{first['trigger']}", slate_id=cid, note=t or "book", et=et)
    except Exception as e:
        log.warning(f"[desk_check] spend not recorded: {e}")
    _exec("UPDATE desk_checks SET input_tokens=?, output_tokens=?, cost_usd=?, seconds=?, model=? WHERE id=?",
          (int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0)
           + int(usage.get("cache_creation_input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0),
           cost, _f(res.get("seconds")), res.get("model") or c["model"], cid))
    stop = res.get("stop_reason")
    obj, err = (None, f"the model stopped early ({stop})") if stop in ("refusal", "max_tokens") \
        else parse_output(res.get("text"))
    decisions = []
    if obj is not None:
        decisions, err = validate(obj, ctx["allowed"], single=t)
    if err:
        _exec("UPDATE desk_checks SET status='invalid', skip_reason=?, raw_text=? WHERE id=?",
              (err[:300], str(res.get("text") or "")[:4000], cid))
        _finish_triggers(keys, "checked", cid, "invalid answer")
        log.warning(f"[desk_check] {first['trigger']} {t or 'book'}: invalid answer, nothing done: {err}")
        return {"id": cid, "status": "invalid", "error": err, "cost_usd": cost}
    acts = sorted({d["action"] for d in decisions})
    headline = acts[0] if len(acts) == 1 else ("ignore" if not acts else "mixed")
    _exec("UPDATE desk_checks SET status='decided', output_json=?, action=? WHERE id=?",
          (json.dumps(decisions), headline, cid))
    check = {"id": cid, "trigger": first["trigger"], "ticker": t, "payload": first.get("payload") or {}}
    outcome = apply_decisions(check, decisions, bk, c, notifier, ctx)
    _store_outcome(cid, decisions, outcome, c)
    _finish_triggers(keys, "checked", cid)
    return {"id": cid, "status": "done", "action": headline, "decisions": decisions, "outcome": outcome,
            "cost_usd": cost, "tokens": usage}


def _store_outcome(cid, decisions, outcome, c):
    esc = next((o for o in outcome if o.get("asked") == "escalate" and o.get("action") == "escalate"), None)
    note = None
    if c.get("notes_to_director"):
        if any(o["action"] == "watch" for o in outcome) or (esc and esc.get("state") == "handed_off"):
            note = "pending"
    _exec("UPDATE desk_checks SET status='done', outcome_json=?, escalation_state=?, escalation_slate_id=?, note_state=? "
          "WHERE id=?", (json.dumps(outcome, default=str), (esc or {}).get("state"), (esc or {}).get("slate_id"), note, cid))


# ── recovery after a restart ────────────────────────────────────────────────────

def recover(et, c, bk=None, notifier=None, now=None) -> dict:
    """A restart can kill a check between its steps. 'running' (the model call never came
    back): the check is marked interrupted and its event gets ONE more try if still fresh.
    'decided' (the answer was stored but not acted on): acted on now if it is under 15
    minutes old (every action is idempotent), else closed as not acted on. An Opus
    decision that was started and has no ledger row is NOT started again; it is handed to
    the next scheduled review as a note."""
    out = {"interrupted": 0, "resumed": 0, "handed_off": 0}
    try:
        now = float(now if now is not None else time.time())
        for r in _rows("SELECT * FROM desk_checks WHERE status='running' AND created_epoch < ?", (now - _STALE_RUNNING_S,)):
            _exec("UPDATE desk_checks SET status='interrupted', skip_reason='restart during the model call' WHERE id=?",
                  (r["id"],))
            for k in (_loads(r.get("trigger_json")).get("keys") or []):
                _exec("UPDATE desk_check_triggers SET state=CASE WHEN attempts < 1 THEN 'pending' ELSE 'skipped' END, "
                      "attempts=attempts+1, note='restart during the check' WHERE key=? AND state='checking'", (k,))
            out["interrupted"] += 1
        for r in _rows("SELECT * FROM desk_checks WHERE status='decided' AND created_epoch < ?", (now - _STALE_RUNNING_S,)):
            decisions = _loads(r.get("output_json"), [])
            keys = _loads(r.get("trigger_json")).get("keys") or []
            if now - float(r["created_epoch"] or 0) <= 900 and decisions:
                bk = bk or load_book()
                trig = (_loads(r.get("trigger_json")).get("triggers") or [{}])[0]
                check = {"id": r["id"], "trigger": r["trigger"], "ticker": r["ticker"], "payload": trig.get("payload") or {}}
                ctx = {"ref_px": {r["ticker"]: r.get("ref_px")} if r.get("ticker") else {}, "summary": r.get("input_summary"),
                       "facts": r.get("input_summary"), "et": et}
                _store_outcome(r["id"], decisions,
                               apply_decisions(check, decisions, bk, c, notifier, ctx, resume=True), c)
                out["resumed"] += 1
            else:
                _exec("UPDATE desk_checks SET status='done', outcome_json=? WHERE id=?",
                      (json.dumps([{"done": False, "why": "not acted on: a restart interrupted it and it was too old to resume"}]),
                       r["id"]))
            _finish_triggers(keys, "checked", r["id"])
        ledger = None
        for r in _rows("SELECT id, ticker, created_epoch FROM desk_checks WHERE escalation_state='started' AND created_epoch < ?",
                       (now - 600,)):
            if ledger is None:
                ledger = {str(x.get("note")): x for x in _DIR()._ledger_rows()}
            row = ledger.get(f"check:{r['id']}")
            if row:
                _exec("UPDATE desk_checks SET escalation_state='done', escalation_slate_id=? WHERE id=?",
                      (row.get("slate_id"), r["id"]))
            else:
                _exec("UPDATE desk_checks SET escalation_state='handed_off', note_state=? WHERE id=?",
                      ("pending" if c.get("notes_to_director") else None, r["id"]))
                out["handed_off"] += 1
    except Exception as e:
        out["error"] = str(e)
        log.warning(f"[desk_check] recover: {e}")
    return out


# ── the pass (desk worker job, every 3 minutes) ─────────────────────────────────

def _recent_check(ticker, since_epoch) -> dict | None:
    r = _rows("SELECT id, trigger_json, created_epoch FROM desk_checks WHERE ticker=? AND created_epoch >= ? "
              "AND status IN ('running','decided','done','invalid') ORDER BY created_epoch DESC LIMIT 1",
              (ticker, since_epoch))
    return r[0] if r else None


def tick(now=None, *, notifier=None, model_fn=None) -> dict:
    """One pass: recover, watch raised stops, collect events, run at most
    max_checks_per_pass checks. Never raises."""
    out = {"ran": [], "skipped": {}, "new_events": 0}
    try:
        c = cfg()
        if not c.get("enabled"):
            return {"skipped": "desk checks are switched off"}
        if not ensure_schema():
            return {"error": "tables unavailable"}
        now_s = float(now if now is not None else time.time())
        et = _now_et(now_s)
        out["recovered"] = recover(et, c, notifier=notifier, now=now_s)
        out["stops"] = watch_stops(et, c, notifier)
        has_pending = bool(_rows("SELECT 1 FROM desk_check_triggers WHERE state='pending' LIMIT 1"))
        want_broker = has_pending or any((c[k] or {}).get("enabled") for k in ("news", "move", "position")) \
            or any((c["passes"].get(k) or {}).get("enabled") for k in ("after_close", "before_open"))
        bk = load_book(broker=want_broker)
        out["new_events"] = collect(et, c, bk, now=now_s)
        pend = _rows("SELECT * FROM desk_check_triggers WHERE state='pending' ORDER BY prio, created_epoch")
        groups, order = {}, []
        for p in pend:
            p["payload"] = _loads(p.get("payload_json"))
            gk = p["ticker"] or f"#{p['trigger']}"
            if gk not in groups:
                groups[gk] = []
                order.append(gk)
            groups[gk].append(p)
        max_age = float(c["trigger_max_age_min"]) * 60
        window = float(c["per_ticker_window_min"]) * 60
        ran = 0
        # (review fix R4) A scheduled director call that is due but has not finished is waiting behind this
        # job on the same worker (this job outranks it in the queue). Nothing is checked until it is done;
        # the events stay queued and are taken up on the next pass.
        try:
            if _DIR().scheduled_calls_pending(et):
                out["waiting"] = "a scheduled director review is due; checks wait for it"
                order = []
        except Exception:
            pass
        c = dict(c, _several_in_pass=len(order) > 1)

        def skip(group, why):
            _finish_triggers([g["key"] for g in group], "skipped", note=why)
            out["skipped"][why] = out["skipped"].get(why, 0) + len(group)

        for gk in order:
            group = groups[gk]
            fresh = [g for g in group if now_s - float(g["created_epoch"] or 0) <= max_age]
            if len(fresh) < len(group):
                skip([g for g in group if g not in fresh], "too old")
            if not fresh:
                continue
            group = fresh
            t = group[0]["ticker"]
            if not in_hours(et, c) and any(g["trigger"] != "news" for g in group):
                skip([g for g in group if g["trigger"] != "news"], "outside the checking hours")
                group = [g for g in group if g["trigger"] == "news"]
                if not group:
                    continue
            if t:
                last = _recent_check(t, now_s - window)
                if last:
                    seen = {str(x.get("strong_key")) for x in (_loads(last.get("trigger_json")).get("triggers") or [])}
                    news = [g for g in group if g.get("strong_key") and g["strong_key"] not in seen]
                    if not news:
                        skip(group, "this stock was checked a short while ago")
                        continue
            if ran >= int(c["max_checks_per_pass"]):
                break                                       # stays pending for the next pass
            ok, code, plain = caps_ok(et, c, group[0]["trigger"], (holder_of(bk, t) if t else "book"))
            if not ok:
                skip(group, f"daily limit ({code})")
                _cap_note(code, plain, _day(et), notifier)
                continue
            try:
                res = run_check(group, bk, et, c, notifier=notifier, model_fn=model_fn, now=now_s)
            except Exception as e:                          # one bad check never stops the pass
                log.warning(f"[desk_check] check on {gk} failed: {e}")
                _finish_triggers([g["key"] for g in group], "skipped", note=f"error: {type(e).__name__}")
                res = {"error": f"{type(e).__name__}: {e}"}
            ran += 1
            out["ran"].append({"key": group[0]["key"], "ticker": t, **{k: res.get(k) for k in
                                                                      ("id", "status", "action", "error", "cost_usd")}})
            if res.get("status") == "error":                # the model is not answering: do not queue up more
                break                                       # waits behind it; the rest stay for the next pass
            if any(o.get("asked") == "escalate" and o.get("state") in ("done", "error")
                   for o in (res.get("outcome") or [])):    # (review fix R4) one full decision per pass: the
                break                                       # exit manager's turn comes before the next one
        if now_s - (_f(_state_get("desk_check_pruned")) or 0) > 86400:
            _state_set("desk_check_pruned", now_s)
            _exec("DELETE FROM desk_check_triggers WHERE created_epoch < ? AND state <> 'pending'", (now_s - 21 * 86400,))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning(f"[desk_check] pass failed: {e}")
    return out


# ── notes for the next scheduled director call ──────────────────────────────────

def director_notes(max_items: int = 8, max_age_days: float = 4.0, now=None) -> tuple:
    """(text, ids, tickers) of "watch" answers and handed-over events the director has not
    seen yet. ('', [], set()) when the switch is off or there is nothing. Never raises."""
    try:
        c = cfg()
        if not (c.get("enabled") and c.get("notes_to_director")) or not ensure_schema():
            return "", [], set()
        rows = _rows("SELECT * FROM desk_checks WHERE note_state='pending' AND created_epoch >= ? "
                     "ORDER BY created_epoch DESC LIMIT ?",
                     (float(now if now is not None else _now_et().timestamp()) - max_age_days * 86400, int(max_items)))
        L, ids, tk = [], [], set()
        for r in reversed(rows):
            ids.append(r["id"])
            for d, o in zip(_loads(r.get("output_json"), []), _loads(r.get("outcome_json"), [])):
                if o.get("action") == "watch":
                    L.append(f"- {_clock_words(r['created_epoch'])} {d['ticker']} ({r['trigger'].replace('_', ' ')}): "
                             f"worth a look. {d['reason']}")
                    tk.add(d["ticker"])
                elif o.get("asked") == "escalate" and o.get("state") == "handed_off":
                    L.append(f"- {_clock_words(r['created_epoch'])} {d['ticker']} ({r['trigger'].replace('_', ' ')}): "
                             f"the check wanted a full decision at once"
                             + (f", expecting the price to {'rise' if d.get('direction') == 'long' else 'fall'}" if d.get("direction") else "")
                             + f". It was left for this review instead ({o.get('why') or 'no separate run'}). {d['reason']}")
                    tk.add(d["ticker"])
        return "\n".join(L), ids, tk
    except Exception as e:
        log.info(f"[desk_check] director notes: {e}")
        return "", [], set()


def mark_notes_shown(ids, slate_id) -> int:
    n = 0
    try:
        for i in ids or []:
            n += _exec("UPDATE desk_checks SET note_state='shown', note_slate_id=? WHERE id=? AND note_state='pending'",
                       (str(slate_id), i))
    except Exception as e:
        log.info(f"[desk_check] mark notes: {e}")
    return n


# ── grading (nightly, no model call) ────────────────────────────────────────────

def _closes_after(ticker, day, n=6) -> list:
    """Daily closes of the sessions AFTER `day`, oldest first (the price cache Mason already keeps)."""
    try:
        import mason_price_cache as PC
        d0 = datetime.fromisoformat(day).date()
        rows = PC.get_daily(ticker, d0 + timedelta(days=1), d0 + timedelta(days=14)) or []
        return [(r["date"], _f(r.get("close"))) for r in rows if _f(r.get("close"))][:n]
    except Exception:
        return []


def grade_outcomes(limit: int = 200) -> dict:
    """Fill in what the price did after each check: the move from the price at the check
    to the close one session later and five sessions later (per stock decided on, and for
    SPY over the same days). Re-run nightly until both are in. Never raises."""
    out = {"graded": 0, "pending": 0}
    try:
        if not (cfg().get("grading") or {}).get("enabled", True) or not ensure_schema():
            return {"skipped": "grading is switched off"}
        rows = _rows("SELECT * FROM desk_checks WHERE status IN ('done','invalid') AND COALESCE(grade_done,0)=0 "
                     "AND created_epoch >= ? ORDER BY created_epoch LIMIT ?", (time.time() - 45 * 86400, int(limit)))
        spy_cache = {}
        for r in rows:
            day = r["day"]
            oc = _loads(r.get("outcome_json"), [])
            items = oc if oc else ([{"ticker": r["ticker"], "ref_px": r.get("ref_px")}] if r.get("ticker") else [])
            done5 = True
            for o in items:
                t, ref = o.get("ticker"), _f(o.get("ref_px"))
                if not t or not ref:
                    continue
                cl = _closes_after(t, day)
                if len(cl) >= 1:
                    o["move_1s_pct"] = round((cl[0][1] / ref - 1) * 100, 2)
                if len(cl) >= 5:
                    o["move_5s_pct"] = round((cl[4][1] / ref - 1) * 100, 2)
                else:
                    done5 = False
            if day not in spy_cache:
                spy_cache[day] = (_prev_close("SPY", (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d"))[0],
                                  _closes_after("SPY", day))
            base, sc = spy_cache[day]
            base = _f(_loads(r.get("trigger_json")).get("spy_ref")) or base    # SPY at the moment of the check, when known
            s1 = round((sc[0][1] / base - 1) * 100, 2) if (base and len(sc) >= 1) else None
            s5 = round((sc[4][1] / base - 1) * 100, 2) if (base and len(sc) >= 5) else None
            one = next((o for o in items if o.get("ticker") == r.get("ticker")), None) if r.get("ticker") else None
            full = bool(done5 and s5 is not None)
            _exec("UPDATE desk_checks SET outcome_json=COALESCE(?, outcome_json), move_1s_pct=?, move_5s_pct=?, spy_1s_pct=?, "
                  "spy_5s_pct=?, graded_at=?, grade_done=? WHERE id=?",
                  (json.dumps(oc, default=str) if oc else None, (one or {}).get("move_1s_pct"),
                   (one or {}).get("move_5s_pct"), s1, s5,
                   datetime.now().isoformat(timespec="seconds"), 1 if full else 0, r["id"]))
            out["graded" if full else "pending"] += 1
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ── status (for /api/agent_loop/status or /api/opus_director) ───────────────────

def status(now=None) -> dict:
    """Today's checks by outcome, spend against the limits, the last full decision asked for."""
    try:
        c = cfg()
        et = _now_et(now)
        day = _day(et)
        out = {"enabled": bool(c.get("enabled")),
               "switches": {"news": bool(c["news"].get("enabled")), "move": bool(c["move"].get("enabled")),
                            "position": bool(c["position"].get("enabled")), "earnings": bool(c["earnings"].get("enabled")),
                            "after_close": bool((c["passes"].get("after_close") or {}).get("enabled")),
                            "before_open": bool((c["passes"].get("before_open") or {}).get("enabled")),
                            "notes_to_director": bool(c.get("notes_to_director")), "apply_actions": bool(c.get("apply_actions"))},
               "model": c.get("model")}
        if not ensure_schema():
            return dict(out, error="tables unavailable")
        D = _DIR()
        s = D.spend(et)
        rows = _rows("SELECT trigger, status, action, cost_usd FROM desk_checks WHERE day=?", (day,))
        by_action, by_trigger, by_status = {}, {}, {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_trigger[r["trigger"]] = by_trigger.get(r["trigger"], 0) + 1
            if r["status"] == "done":
                by_action[r["action"] or "?"] = by_action.get(r["action"] or "?", 0) + 1
        sk = _rows("SELECT note, COUNT(*) AS n FROM desk_check_triggers WHERE state='skipped' AND created_epoch >= ? GROUP BY note",
                   (et.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),))
        le = _rows("SELECT id, created_at, ticker, trigger, escalation_state, escalation_slate_id FROM desk_checks "
                   "WHERE escalation_state IS NOT NULL ORDER BY created_epoch DESC LIMIT 1")
        out.update({"day": day, "checks_today": len(rows), "max_checks_per_day": int(c["max_checks_per_day"]),
                    "by_action": by_action, "by_trigger": by_trigger, "by_status": by_status,
                    "skipped_today": {str(x["note"]): x["n"] for x in sk},
                    "spend": {"checks_usd": round(s.get("check_usd", 0.0), 4), "checks_cap_usd": float(c["daily_usd_cap"]),
                              "opus_usd": round(s.get("day_usd", 0.0), 4), "all_usd": round(s.get("all_day_usd", 0.0), 4),
                              "overall_cap_usd": D.overall_daily_cap(),
                              "escalations_today": s.get("escalations_today", 0)},
                    "last_escalation": (le[0] if le else None),
                    "pending_events": _rows("SELECT COUNT(*) AS n FROM desk_check_triggers WHERE state='pending'")[0]["n"],
                    "raised_stops": _rows("SELECT rec_id, ticker, level, set_at FROM desk_check_stops WHERE state='active'")})
        return out
    except Exception as e:
        return {"enabled": False, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2, default=str))
    elif cmd == "grade":
        print(json.dumps(grade_outcomes(), indent=2, default=str))
    else:
        print("usage: python3 mason_desk_check.py status | grade")
