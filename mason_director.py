"""Mason — Opus DIRECTOR v2 (Catalyst Desk, 2026-09-25).

Replaces the thin v1 director call (≈2.7K tokens, "<=2 sentences", ONE play) with a
research-grade director on claude-opus-5-5:

  * ANCHOR calls (07:00 / 13:00 ET, trading days only) — the full desk package
    (untruncated report, all five raw analyst contexts, verified claims, analyses,
    open predictions) PLUS the top catalysts since the last director run with their
    deterministic research dossiers (mason_dossier) and a macro dossier, under a
    token budget.
  * EVENT calls (≤ catalyst.event_call.max_per_day) — "as it drops": a high-score,
    tape-confirmed catalyst far enough from the next anchor gets its own call.
  * Optional server-side web_search / web_fetch (Opus does the research; qwen never).
  * Output = a pro-trader research BRIEF (markdown) + a JSON slate of 1–5 predictions,
    each with horizon, confidence, invalidation and a concrete trade (stock or a
    SINGLE-LEG call/put chosen from the real chain in the dossier — never spreads).
  * Hard $ caps (daily + monthly) enforced BEFORE every call from a spend ledger in
    desk_state; actual cost recorded from API usage after.

Every prediction lands as a kind='director' rec and is graded by the existing
mason_desk_pipeline.grade_director_predictions (multi-horizon, on the underlying).
When autotrade.auto_place is on, long/short calls are auto-placed on the Alpaca PAPER
account via mason_autotrade (tiered sizing + exits managed by mason_agent_loop);
otherwise they stay observe-only (nothing staged or placed).

Gate: analyst_desk.opus_director.v2.enabled (default false ⇒ v1 path unchanged).
Interfaces consumed (lazy, absence-tolerant): mason_triage.top_catalysts /
mark_catalyst / broad_universe; mason_dossier.build_dossier / build_macro_dossier /
dossier_text. Contracts: docs/prompts/CATALYST_DESK_CONTRACTS.md.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import mason_db
import mason_notifications
import mason_activity

log = logging.getLogger("mason.director")

BASE = Path(__file__).parent
_SCHEMA = "director_decision/2.0"

# NYSE full-day closures for the remainder of the build horizon (weekends handled
# separately). Early-close days still count as trading days.
_NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18",
                  "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
                  "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}

_V2_DEFAULTS = {
    "enabled": False,
    "model": "claude-opus-5-5",
    "effort": "medium",
    "max_output_tokens": 16000,
    "expected_output_tokens": 14000,      # for the pre-call $ estimate (measured 13-14K on 09-28 anchors)
    "input_token_factor": 2.1,            # real tokens / (chars/4): dense numeric tables tokenize ~2 chars/token
    "anchor_input_token_budget": 32000,
    "event_input_token_budget": 14000,
    "max_dossiers_anchor": 6,
    "min_dossier_score": 0.65,            # a ticker catalyst must clear this to earn a dossier slot
    "max_followup_dossiers": 3,           # due follow-ups may take at most this many dossier slots
    "max_dossiers_event": 2,
    "max_predictions_anchor": 5,
    "max_predictions_event": 3,
    "daily_usd_cap": 1.00,
    "monthly_usd_cap": 20.0,
    "trading_days_only": True,
    "ab_qwen": False,                     # 2026-09-25 Nico: off — qwen 4B returns no calls on a v2-size package
    "qwen_input_token_budget": 12000,
    # Review fix 2026-09-25: one web_search on the 13:00 anchor cost ~$0.21 of a $0.51
    # call (41K cache-write tokens of results) and still "failed" at max_uses=1. Web now
    # runs on EVENT calls only by default (web_anchor=false), with 2 searches + 1 fetch.
    "web_anchor": False,
    "web_event": True,
    # P1 (Nico 2026-11-12): LEAN anchors — short brief, trimmed desk prose + dossiers, and the
    # WHAT WORKS record, live BOOK and (when the market is open) the same-day SPY/QQQ chain.
    # lean_kinds lists the anchor kinds that run lean, e.g. ["anchor_1300"]; ["all"] = every anchor.
    "lean_kinds": [],
    "lean_dossier_tokens": 1600,
    "lean_expected_output_tokens": 7000,
    "web": {"enabled": True, "max_searches": 2, "max_fetches": 1,
            "search_cost_usd": 0.01,
            "blocked_domains": ["reddit.com", "x.com", "twitter.com", "tiktok.com",
                                "youtube.com", "facebook.com", "instagram.com",
                                "quora.com", "pinterest.com", "stocktwits.com"]},
}

_EVENT_DEFAULTS = {"enabled": False, "max_per_day": 1, "min_minutes_to_next_anchor": 90,
                   "window_et": ["06:00", "17:00"], "min_rel_volume": 1.5,
                   "max_story_age_hours": 12,     # 2026-09-27: first manual event run hit a 2.5-day-old (priced) story
                   "min_abs_vs_sector_pct": 2.0,
                   # Step 2 (2026-11-14): a full decision asked for by a desk check (mason_desk_check)
                   # runs through this same event path, with its own switch and limits and a small,
                   # focused input. The keys above keep governing the old automatic event call.
                   "escalation": {"enabled": False, "max_per_day": 3, "window_et": ["04:00", "20:00"],
                                  "min_minutes_to_scheduled_call": 20, "input_token_budget": 7000,
                                  "max_predictions": 2, "expected_output_tokens": 5000,
                                  "dossier_tokens": 2200, "web": False, "memory_tools": False,
                                  "book_actions": False, "timeout_s": 240}}

# The one limit over everything a model costs in a day: the scheduled director calls, the
# desk checks and the decisions they ask for. Top-level config key "model_budget".
_BUDGET_DEFAULTS = {"daily_usd_cap": 3.50, "reserve_per_scheduled_call_usd": 0.65}


# ── config (read fresh each call so live edits apply without a restart) ─────────

def _cfg() -> dict:
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge(defaults: dict, over: dict | None) -> dict:
    out = dict(defaults)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def v2_cfg(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else _cfg()
    od = ((c.get("analyst_desk") or {}).get("opus_director") or {})
    return _merge(_V2_DEFAULTS, od.get("v2"))


def event_cfg(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else _cfg()
    return _merge(_EVENT_DEFAULTS, (c.get("catalyst") or {}).get("event_call"))


def catalyst_cfg(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else _cfg()
    return c.get("catalyst") or {}


def enabled(cfg: dict | None = None) -> bool:
    return bool(v2_cfg(cfg).get("enabled"))


# ── time helpers ────────────────────────────────────────────────────────────────

def _now_et(now: float | None = None) -> datetime:
    import mason_analyst_desk as AD
    return AD._now_et(now)


def is_trading_day(et: datetime) -> bool:
    return et.weekday() < 5 and et.strftime("%Y-%m-%d") not in _NYSE_HOLIDAYS


def _anchor_times(cfg: dict | None = None) -> list:
    c = cfg if cfg is not None else _cfg()
    t = (((c.get("analyst_desk") or {}).get("anchors") or {}).get("times_et")
         or ["07:00", "13:00"])
    return [x for x in t if isinstance(x, str) and ":" in x]


def minutes_to_next_anchor(et: datetime, cfg: dict | None = None) -> float:
    """Minutes until the next anchor today; inf if none remain today."""
    best = float("inf")
    for t in _anchor_times(cfg):
        hh, _, mm = t.partition(":")
        at = et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if at > et:
            best = min(best, (at - et).total_seconds() / 60.0)
    return best


def anchors_remaining_today(et: datetime, cfg: dict | None = None) -> int:
    n = 0
    for t in _anchor_times(cfg):
        hh, _, mm = t.partition(":")
        if (et.hour, et.minute) < (int(hh), int(mm)):
            n += 1
    return n


# The scheduled calls are named after their clock time ("anchor_0700", "anchor_1300";
# mason_analyst_desk, mason_selfcheck and the dashboard all derive the name from the
# configured time). Settings that mean "the morning call" or "the midday call" must keep
# working when a time is moved, so a kind is also known by its ROLE: the first configured
# time of the day is "morning", the second "midday" (a third and later: "call3", ...).
_OLD_KIND_ROLE = {"anchor_0700": "morning", "anchor_1300": "midday"}


def anchor_role(kind, cfg: dict | None = None) -> str | None:
    """'morning' | 'midday' | 'callN' for a scheduled-call kind, None for anything else
    (manual runs, event runs, hourly reports). Accepts the role names themselves."""
    k = str(kind or "")
    if k in ("morning", "midday") or re.fullmatch(r"call\d+", k):
        return k
    m = re.fullmatch(r"anchor_(\d{2})(\d{2})", k)
    if not m:
        return None
    times = sorted(_anchor_times(cfg))
    hhmm = f"{m.group(1)}:{m.group(2)}"
    if hhmm in times:
        i = times.index(hhmm)
        return ("morning", "midday")[i] if i < 2 else f"call{i + 1}"
    return _OLD_KIND_ROLE.get(k) or ("morning" if int(m.group(1)) < 12 else "midday")


def scheduled_calls_pending(et: datetime | None = None, cfg: dict | None = None, grace_min: float = 30.0) -> int:
    """(review fix R4) Scheduled calls whose time has passed today but which have not FINISHED yet: queued
    behind other desk work, running, or waiting for a retry. By the clock they are no longer "still to come",
    yet they have not been paid for and must not be overtaken. A call is finished when the desk worker has
    written last_anchor_HHMM = today; one that is still open `grace_min` after its time is no longer waited for.
    Never raises (0 on any doubt about the config, 1 per call it cannot read the state of)."""
    try:
        et = et or _now_et()
        c = cfg if cfg is not None else _cfg()
        v2 = v2_cfg(cfg)
        if not v2.get("enabled") or (v2.get("trading_days_only", True) and not is_trading_day(et)):
            return 0
        if (((c.get("analyst_desk") or {}).get("anchors") or {}).get("enabled", True)) is False:
            return 0
        today, n = et.strftime("%Y-%m-%d"), 0
        for t in _anchor_times(cfg):
            try:
                hh, _, mm = t.partition(":")
                at = et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            except ValueError:
                continue
            if at <= et and (et - at).total_seconds() <= float(grace_min) * 60:
                try:
                    done = _state_get("last_anchor_" + t.replace(":", "")) == today
                except Exception:
                    done = False
                n += 0 if done else 1
        return n
    except Exception:
        return 0


# ── spend ledger (desk_state) ───────────────────────────────────────────────────

def _state_get(key, default=None):
    import mason_analyst_desk as AD
    return AD.get_state(key, default)


def _state_set(key, value):
    import mason_analyst_desk as AD
    AD.set_state(key, value)


# The spend ledger is an append-only JSONL file, NOT desk_state: on 2026-09-26 a paid
# Opus call finished and then died writing its cost into a locked mason.db, losing the
# brief AND the spend record. A file append can't hit a DB lock; spend() sums it.
SPEND_LEDGER = BASE / "director_spend.jsonl"
_ledger_lock = __import__("threading").Lock()


def _ledger_rows() -> list:
    rows = []
    try:
        with open(SPEND_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except FileNotFoundError:
        pass
    return rows


def spend(et: datetime | None = None) -> dict:
    """Today's and this month's spend from the ledger. day_usd / month_usd / day_calls /
    event_calls_today are the DIRECTOR's own calls, as before. Desk checks (rows with mode
    "check") are reported apart (check_usd, check_calls) and in the all_* totals."""
    et = et or _now_et()
    d, m = et.strftime("%Y-%m-%d"), et.strftime("%Y-%m")
    rows = _ledger_rows()

    def usd(rs):
        return round(sum(float(r.get("cost_usd") or 0) for r in rs), 6)

    day_all = [r for r in rows if r.get("day") == d]
    day = [r for r in day_all if r.get("mode") != "check"]
    chk = [r for r in day_all if r.get("mode") == "check"]
    mon = [r for r in rows if str(r.get("day", "")).startswith(m)]
    esc = sum(1 for r in day if str(r.get("kind") or "").startswith("escalation:"))
    ev = sum(1 for r in day if r.get("mode") == "event")
    return {"day": d, "month": m,
            "day_usd": usd(day),
            "month_usd": usd([r for r in mon if r.get("mode") != "check"]),
            "day_calls": len(day),
            "event_calls_today": ev,
            "escalations_today": esc, "auto_event_calls_today": ev - esc,
            "check_usd": usd(chk), "check_calls": len(chk),
            "all_day_usd": usd(day_all), "all_month_usd": usd(mon)}


def budget_cfg(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else _cfg()
    return _merge(_BUDGET_DEFAULTS, c.get("model_budget"))


def overall_daily_cap(cfg: dict | None = None) -> float:
    """The daily limit over ALL model spend in the ledger (model_budget.daily_usd_cap,
    default 3.50). Never lower than the director's own daily cap, so adding this limit
    cannot stop a call the director's cap allows."""
    try:
        return max(float(budget_cfg(cfg)["daily_usd_cap"]), float(v2_cfg(cfg)["daily_usd_cap"]))
    except (TypeError, ValueError):
        return float(_BUDGET_DEFAULTS["daily_usd_cap"])


def scheduled_reserve_usd(et: datetime | None = None, cfg: dict | None = None) -> float:
    """Money kept back for the scheduled director calls still to come today, so desk
    checks and the decisions they ask for can never starve one: calls remaining x the
    average cost of the last six scheduled calls (model_budget.reserve_per_scheduled_call_usd
    when there is no history)."""
    try:
        et = et or _now_et()
        v2 = v2_cfg(cfg)
        if not v2.get("enabled") or (v2.get("trading_days_only", True) and not is_trading_day(et)):
            return 0.0
        # (review fix R4) + calls that are due but not finished (queued or running): by the clock they were
        # "not remaining", so in the minutes after 07:00 / 13:00 nothing was kept back for them
        n = anchors_remaining_today(et, cfg) + scheduled_calls_pending(et, cfg)
        if not n:
            return 0.0
        costs = [float(r.get("cost_usd") or 0) for r in _ledger_rows()
                 if r.get("mode") == "anchor" and str(r.get("kind") or "").startswith("anchor_")][-6:]
        costs = [c for c in costs if c > 0]
        # (review fix R4) the dearest of the last six plus 10%, not their average: the scheduled call is
        # tested against its own ESTIMATE (about the cost of a full-format call), and an average pulled
        # down by the cheaper short-format calls left less than that
        per = (max(costs) * 1.10) if costs else float(budget_cfg(cfg)["reserve_per_scheduled_call_usd"])
        return round(n * per, 4)
    except Exception:
        return 0.0


def record_spend(cost_usd: float, *, event: bool = False, et: datetime | None = None,
                 kind: str = "", slate_id: str = "", note: str = "", mode: str | None = None):
    """mode: None keeps the old meaning ("event" / "anchor"); "check" marks a desk check."""
    et = et or _now_et()
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "day": et.strftime("%Y-%m-%d"),
           "cost_usd": round(float(cost_usd or 0), 6), "mode": mode or ("event" if event else "anchor"),
           "kind": kind, "slate_id": slate_id, "note": note}
    with _ledger_lock:
        with open(SPEND_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def estimate_cost(system: str, user: str, v2: dict) -> float:
    import mason_llm
    web = v2.get("web") or {}
    web_in = 0
    if _search_tools_on():   # tool schemas (~1.8K) + up to N results (~1.5K each) + cached re-reads
        web_in += 1800 + int(_search_cfg().get("max_tool_calls", 4) or 4) * 1500
    if web.get("enabled"):
        # measured 2026-09-25: one web_search pulled ~7.6K result tokens (billed as a
        # cache write, 1.25x input); budget 10K/search and 12K/fetch at the input rate.
        web_in = int(web.get("max_searches", 0)) * 10000 + int(web.get("max_fetches", 0)) * 12000
    fac = float(v2.get("input_token_factor", 2.1) or 1.0)
    usage = {"input_tokens": int((_tok(system) + _tok(user)) * fac) + web_in,
             "output_tokens": int(v2.get("expected_output_tokens", 8000))}
    n_s = int(web.get("max_searches", 0)) if web.get("enabled") else 0
    return mason_llm.research_cost_usd(v2.get("model"), usage, web_search_requests=n_s,
                                       search_cost_usd=web.get("search_cost_usd", 0.01))


def budget_ok(est_usd: float, v2: dict, *, reserve_usd: float = 0.0,
              et: datetime | None = None, cfg: dict | None = None) -> tuple[bool, str]:
    s = spend(et)
    if s["day_usd"] + est_usd + reserve_usd > float(v2["daily_usd_cap"]):
        return False, (f"daily cap: spent ${s['day_usd']:.2f} + est ${est_usd:.2f}"
                       f" + reserve ${reserve_usd:.2f} > ${float(v2['daily_usd_cap']):.2f}")
    if s["month_usd"] + est_usd > float(v2["monthly_usd_cap"]):
        return False, (f"monthly cap: spent ${s['month_usd']:.2f} + est ${est_usd:.2f}"
                       f" > ${float(v2['monthly_usd_cap']):.2f}")
    cap_all = overall_daily_cap(cfg)           # everything in the ledger: director calls + desk checks
    if s["all_day_usd"] + est_usd + reserve_usd > cap_all:
        return False, (f"overall daily model budget: spent ${s['all_day_usd']:.2f} (director ${s['day_usd']:.2f}"
                       f" + desk checks ${s['check_usd']:.2f}) + est ${est_usd:.2f} + reserve ${reserve_usd:.2f}"
                       f" > ${cap_all:.2f}")
    return True, "ok"


# ── package assembly ────────────────────────────────────────────────────────────

def _tok(s: str) -> int:
    return len(s or "") // 4


def _clip_tokens(s: str, max_tok: int) -> str:
    s = s or ""
    if _tok(s) <= max_tok:
        return s
    return s[: max_tok * 4].rstrip() + "\n  …(truncated for budget)"


def _open_predictions(limit: int = 12) -> list:
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_date, ticker, direction, entry_ref_price, extra_json "
            "FROM recommendations WHERE kind='director' AND status='open' "
            "AND created_by='opus_director' AND COALESCE(record_deleted,0)=0 "
            "AND direction IN ('long','short') ORDER BY created_date DESC LIMIT ?",
            (limit,)).fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ex = json.loads(r.get("extra_json") or "{}")
        except Exception:
            ex = {}
        o = {"rec_id": r["id"], "date": (r["created_date"] or "")[:16],
             "ticker": r["ticker"], "direction": r["direction"],
             "entry": r["entry_ref_price"], "horizon": ex.get("horizon"),
             "confidence": ex.get("confidence"),
             "thesis": (ex.get("prediction") or "")[:200],
             "grades": ex.get("grades") or {}}
        note = _no_put_note(r, ex)                 # None unless a short call got no put AND that wording is on
        if note:
            o["no_put"] = note
        out.append(o)
    return out


def _no_put_note(row, ex):
    """(Nico 2026-11-18) A short call on a stock for which Mason bought no put: one sentence for that call's line
    in YOUR OPEN PREDICTIONS, or None. mason_short_put.no_put_words decides (only with
    autotrade.short_as_put.enabled AND .tell_director, only for a call the automatic placing refused)."""
    try:
        if str(row.get("direction") or "").lower() != "short":
            return None
        import mason_short_put as SP
        return SP.no_put_words({"kind": "director", "created_by": "opus_director", "direction": row.get("direction"),
                                "ticker": row.get("ticker"), "extra": ex})
    except Exception:
        return None


# ── follow-ups (the director's memory for deferred work) ────────────────────────

def due_followups(today: str | None = None, limit: int = 10) -> list:
    """Open follow-ups whose due_date has arrived (<= today). A weekend/holiday due
    date simply surfaces at the next run on or after it."""
    today = today or _now_et().strftime("%Y-%m-%d")
    conn = mason_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM director_followups WHERE status='open' AND record_deleted=0 "
            "AND due_date <= ? ORDER BY due_date, created_date LIMIT ?", (today, limit)).fetchall()]
    except Exception as e:
        log.info(f"[director] follow-ups unavailable: {e}")
        return []
    finally:
        conn.close()


def upcoming_followups(days: int = 45, limit: int = 20) -> list:
    today = _now_et().date()
    conn = mason_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM director_followups WHERE status='open' AND record_deleted=0 "
            "AND due_date <= ? ORDER BY due_date LIMIT ?",
            ((today + timedelta(days=days)).isoformat(), limit)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def add_followups(items, *, slate_id: str = "", rec_ids: dict | None = None,
                  allowed: set | None = None, today=None, max_items: int = 5,
                  created_by: str = "opus_director") -> list:
    """Deterministically store the director's deferred work + a user-visible
    reminder for each. Validation: real date, after today, within 200 days; ticker
    in the package/universe (else stored as macro); non-empty action; deduped
    against open follow-ups for the same ticker+date. Never raises."""
    import mason_agenda
    today = today or _now_et().date()
    out = []
    for it in (items or [])[:max_items]:
        try:
            if not isinstance(it, dict):
                continue
            action = str(it.get("action") or "").strip()[:300]
            try:
                due = datetime.strptime(str(it.get("date") or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if not action or due <= today or due > today + timedelta(days=200):
                continue
            t = str(it.get("ticker") or "").upper().strip() or None
            if t in ("NULL", "NONE", "N/A", "MACRO"):
                t = None
            if t and allowed and t not in allowed:
                t = None
            conn = mason_db.get_connection()
            try:
                dup = conn.execute(
                    "SELECT id FROM director_followups WHERE status='open' AND record_deleted=0 "
                    "AND COALESCE(ticker,'')=? AND due_date=?", (t or "", due.isoformat())).fetchone()
            finally:
                conn.close()
            if dup:
                continue
            label = t or "macro"
            rem = mason_agenda.add_reminder(
                f"[Opus follow-up] {label}: {action} (director re-researches this at the "
                f"{due.isoformat()} anchor)", fire_on=due.isoformat(), created_by="mason")
            fid = uuid.uuid4().hex[:12]
            conn = mason_db.get_connection()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO director_followups (id, ticker, due_date, action, reason, "
                        "source_slate_id, source_rec_id, status, reminder_id, created_date, "
                        "created_by, record_deleted) VALUES (?,?,?,?,?,?,?,'open',?,?,?,0)",
                        (fid, t, due.isoformat(), action, str(it.get("reason") or "")[:300],
                         slate_id, (rec_ids or {}).get(t), (rem or {}).get("id"),
                         datetime.now().isoformat(), created_by))
            finally:
                conn.close()
            out.append({"id": fid, "ticker": t, "due_date": due.isoformat(), "action": action,
                        "reminder_id": (rem or {}).get("id")})
        except Exception as e:
            log.warning(f"[director] follow-up not stored ({it}): {e}")
    return out


def mark_followups_done(ids, slate_id: str):
    if not ids:
        return
    rem_ids = []
    conn = mason_db.get_connection()
    try:
        with conn:
            for fid in ids:
                conn.execute("UPDATE director_followups SET status='done', surfaced_slate_id=?, "
                             "surfaced_date=? WHERE id=? AND status='open'",
                             (slate_id, datetime.now().isoformat(), fid))
            rem_ids = [r["reminder_id"] for r in conn.execute(
                f"SELECT reminder_id FROM director_followups WHERE id IN ({','.join('?' * len(ids))}) "
                "AND reminder_id IS NOT NULL", list(ids)).fetchall()]
    finally:
        conn.close()
    # the director handled it → tick the mirrored "[Opus follow-up]" reminder too
    # (Nico 2026-09-28), so the widget doesn't keep showing work that's done.
    try:
        import mason_agenda
        for rid in rem_ids:
            mason_agenda.complete_reminder(rid, True)
    except Exception as e:
        log.info(f"[director] follow-up reminder auto-complete failed: {e}")


# ── director MEMORY (2026-09-27) ────────────────────────────────────────────────
# Opus has no state between calls. These deterministic sections give it continuity
# (its previous read), its per-ticker record, a calibrated scorecard, the calls that
# just closed (so it can write a lesson) and the stored lessons most relevant now.
# Grades are READ ONLY here (mason_desk_pipeline owns grading). Each section is
# token-capped; the whole memory block stays under ~2.5K tokens (~$0.01/call).

_MEM_CAPS = {"prev": 450, "history": 650, "scorecard": 400, "scorecard_event": 90,
             "closed": 600, "lessons": 350}
_MIN_SAMPLE = 10               # below this, every stat is flagged "insufficient sample"
_LESSONS_MAX = 6               # lessons accepted per run (and NEWLY CLOSED rows shown)
_LESSON_MAX_ASKS = 2           # a closed call Opus skipped is re-offered at most this often
_CLOSED_WINDOW_DAYS = 14       # NEWLY CLOSED looks back this far for lesson-less closes
_LESSON_ASKS_KEY = "opus_v2_lesson_asks"


def _loads(s) -> dict:
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _is_manual(kind) -> bool:
    return str(kind or "").startswith("manual")


def _director_rows(created_by: str = "opus_director", limit: int = 2000) -> list:
    """All non-deleted director recs for one author, newest first, extra parsed."""
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_date, ticker, direction, status, outcome, entry_ref_price, "
            "eval_price, outcome_checked_date, extra_json FROM recommendations "
            "WHERE kind='director' AND created_by=? AND COALESCE(record_deleted,0)=0 "
            "ORDER BY created_date DESC LIMIT ?", (created_by, limit)).fetchall()]
    except Exception as e:
        log.info(f"[director] memory: recs unavailable: {e}")
        rows = []
    finally:
        conn.close()
    for r in rows:
        r["ex"] = _loads(r.pop("extra_json", None))
    return rows


def _lesson_rows(limit: int = 500) -> list:
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM director_lessons WHERE COALESCE(record_deleted,0)=0 "
            "ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()]
    except Exception as e:
        log.info(f"[director] memory: lessons unavailable: {e}")
        rows = []
    finally:
        conn.close()
    for r in rows:
        try:
            t = json.loads(r.get("tags") or "[]")
            r["tags"] = [str(x) for x in t] if isinstance(t, list) else []
        except Exception:
            r["tags"] = []
    return rows


def _catalyst_kinds(ids) -> dict:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    conn = mason_db.get_connection()
    try:
        return {r["id"]: r["event_kind"] for r in conn.execute(
            f"SELECT id, event_kind FROM catalysts WHERE id IN ({','.join('?' for _ in ids)})",
            ids).fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def _grade_keys(ex: dict) -> list:
    keys = set((ex.get("grades") or {}).keys()) | set((ex.get("grades_legacy") or {}).keys())

    def n(k):
        try:
            return int(str(k).strip("+d"))
        except ValueError:
            return 10 ** 6
    return sorted(keys, key=n)


def _pick_grade(ex: dict, key: str) -> dict | None:
    """A checkpoint grade, preferring the trading-session grade (method=session_close)
    over a calendar-era one wherever both exist (grades vs grades_legacy)."""
    cands = [g for g in ((ex.get("grades") or {}).get(key), (ex.get("grades_legacy") or {}).get(key))
             if isinstance(g, dict)]
    for g in cands:
        if g.get("method") == "session_close":
            return g
    return cands[0] if cands else None


def _final_grade(r: dict) -> dict | None:
    ex = r["ex"]
    hd = ex.get("horizon_days")
    g = _pick_grade(ex, f"+{int(hd)}d") if isinstance(hd, (int, float)) else None
    if g is None:
        keys = _grade_keys(ex)
        g = _pick_grade(ex, keys[-1]) if keys else None
    return g


def _signed(direction, move):
    m = _num(move)
    if m is None:
        return None
    return -m if direction == "short" else m


def _vehicle(ex: dict) -> str:
    v = str(((ex.get("trade") or {}).get("vehicle")) or "stock").lower()
    return v if v in ("stock", "call", "put") else "stock"


def _fmt_grade(key: str, g: dict | None) -> str:
    if not g or g.get("outcome") is None:
        return ""
    mv = _num(g.get("move_pct"))
    sd = g.get("session_date") if g.get("method") == "session_close" else None
    return (f"{key} {g['outcome']}" + (f" {mv:+.2f}%" if mv is not None else "")
            + (f" (close {str(sd)[5:10]})" if sd else ""))


def _conf(v) -> str:
    c = _num(v)
    return f"{c:.2f}" if c is not None else "n/a"


def _short_date(s) -> str:
    return str(s or "")[5:16].replace("T", " ")


def _market_read_excerpt(brief: str, max_chars: int = 700) -> str:
    m = re.search(r"##\s*Market read\s*\n(.*?)(?=\n##\s|\Z)", brief or "", re.S | re.I)
    body = " ".join((m.group(1) if m else "").split())
    return body[:max_chars].rstrip() + ("…" if len(body) > max_chars else "")


def _proves_wrong(r: dict, slate_brief: str = "") -> str:
    inv = str(r["ex"].get("invalidation") or "").strip()
    if inv:
        return inv[:160]
    if slate_brief and r.get("ticker"):
        m = re.search(rf"##\s*{re.escape(r['ticker'])}\b.*?\*\*What proves me wrong\*\*:?\s*(.*?)(?=\n\s*\n|\n##|\Z)",
                      slate_brief, re.S)
        if m:
            return " ".join(m.group(1).split())[:160]
    return ""


def _slates(recs: list) -> list:
    """Group recs into slates (v2 slate_id, v1 ab_pair_id), newest first."""
    by = {}
    for r in recs:
        ex = r["ex"]
        sid = ex.get("slate_id") or ex.get("ab_pair_id") or r["id"]
        s = by.setdefault(sid, {"slate_id": sid, "recs": [], "kind": ex.get("anchor_kind"),
                                "at": r.get("created_date") or ""})
        s["recs"].append(r)
        s["at"] = min(s["at"], r.get("created_date") or s["at"])
    out = sorted(by.values(), key=lambda s: s["at"], reverse=True)
    for s in out:
        s["recs"].sort(key=lambda r: (r["ex"].get("slate_index") if isinstance(r["ex"].get("slate_index"), int) else 99,
                                      r.get("created_date") or ""))
        head = next((r for r in s["recs"] if r["ex"].get("brief")), s["recs"][0])
        s["brief"] = head["ex"].get("brief") or ""
        s["market_view"] = head["ex"].get("market_view") or ""
        s["continuity"] = head["ex"].get("continuity") or ""
    return out


def _call_line(r: dict) -> str:
    ex = r["ex"]
    tr = ex.get("trade") or {}
    veh = _vehicle(ex)
    vs = f"{veh} {tr.get('strike')} {tr.get('expiry')}" if veh in ("call", "put") else "stock"
    return f"{r['ticker']} {r.get('direction')} conf {_conf(ex.get('confidence'))} {ex.get('horizon') or '?'} via {vs}"


def _market_view_from_inbox(rec_id: str) -> str:
    """Pre-memory slates didn't store market_view on the rec; it's in their inbox
    notification body ("**Market view:** ..."). Read-only fallback."""
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT body FROM notifications WHERE ref_id=? AND created_by='opus_director' "
                         "ORDER BY created_date DESC LIMIT 1", (rec_id,)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    m = re.search(r"\*\*Market view:\*\*\s*(.+)", (r["body"] if r else "") or "")
    return m.group(1).strip() if m else ""


def _render_prev_read(slates: list, now: datetime) -> tuple[str, str | None]:
    if not slates:
        return "", None
    sched = [s for s in slates if not _is_manual(s["kind"])]
    pick = sched[0] if sched else slates[0]
    manual_since = sum(1 for s in slates if _is_manual(s["kind"]) and s["at"] > pick["at"])
    try:
        age_h = (now.replace(tzinfo=None) - datetime.fromisoformat(pick["at"][:19])).total_seconds() / 3600
        age = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h / 24:.1f}d ago"
    except Exception:
        age = "?"
    lines = [f"{pick['at'][:16].replace('T', ' ')} ({pick['kind'] or '?'}"
             + (", MANUAL TEST — the only prior read" if _is_manual(pick["kind"]) else "")
             + f", {age}) · slate {pick['slate_id']}"]
    mv = pick["market_view"] or _market_view_from_inbox(pick["recs"][0]["id"])
    if mv:
        lines.append(f"Market view: {mv[:300]}")
    if pick["continuity"]:
        lines.append(f"Continuity then: {pick['continuity'][:250]}")
    lines.append("Calls: " + "; ".join(f"[{r['id']}] {_call_line(r)}" for r in pick["recs"][:6]))
    ex = _market_read_excerpt(pick["brief"])
    if ex:
        lines.append(f"Market read (excerpt): {ex}")
    if manual_since:
        lines.append(f"({manual_since} manual test run(s) since — not shown)")
    return "\n".join(lines), pick["slate_id"]


def _render_history(recs: list, lessons: list, tickers: list, max_tickers: int = 8) -> tuple[str, set]:
    by_t = {}
    for r in recs:                                   # recs are newest first
        by_t.setdefault(str(r.get("ticker") or "").upper(), []).append(r)
    les_t = {}
    for l in lessons:
        les_t.setdefault(str(l.get("ticker") or "").upper(), []).append(l)
    out, shown, n = [], set(), 0
    trad = _memory_source() == "tradable"
    dead = _dead_band() if trad else 0.5
    for t in tickers:
        calls, ls = by_t.get(t, [])[:3], les_t.get(t, [])[:2]
        if not calls and not ls:
            continue
        n += 1
        if n > max_tickers:
            break
        out.append(f"{t}:")
        for r in calls:
            ex = r["ex"]
            fmt = (lambda k, g, r=r: _fmt_grade_tradable(r, k, g, dead)) if trad else _fmt_grade
            grades = " ".join(filter(None, (fmt(k, _pick_grade(ex, k)) for k in _grade_keys(ex))))
            tl = _trade_result_line(r)
            fin = (f"RESULT: {tl}" if tl else
                   f"FINAL {r['outcome']}" if r.get("status") == "graded" and r.get("outcome")
                   else r.get("status") or "open")
            upd = (ex.get("director_updates") or [])
            note = (f" | your last note ({str(upd[-1].get('at', ''))[5:10]} {upd[-1].get('action')}): "
                    f"{str(upd[-1].get('note') or '')[:100]}") if upd else ""
            out.append(f"  - {_short_date(r.get('created_date'))} {r.get('direction')} conf "
                       f"{_conf(ex.get('confidence'))} {ex.get('horizon') or '?'} [{r['id']}] — "
                       f"{str(ex.get('prediction') or '')[:120]} | {grades or 'no grades yet'} | {fin}{note}")
        for l in ls:
            shown.add(l["id"])
            out.append(f"  lesson {str(l.get('created_date') or '')[:10]} ({l.get('outcome') or 'interim'}): "
                       f"{str(l.get('lesson') or '')[:200]}")
    return "\n".join(out), shown


def _flag(n: int) -> str:
    return " — insufficient sample (n<10)" if n < _MIN_SAMPLE else ""


# Which grade feeds the scorecard (Fable Step 1, audit L1/L9). "tradable" (the default
# when the key is absent) reads extra_json.grade_v2 — entry at the first price Mason
# could have traded, entry limit honoured, stop/target aware, net of cost — written by
# mason_desk_pipeline. "legacy" reads the outcome column as before. Only the NUMBERS in
# the scorecard change; its wording is untouched.
# Key: analyst_desk.opus_director.grading.scorecard_source, read per call.

def _score_source(cfg: dict | None = None) -> str:
    try:
        c = cfg if cfg is not None else _cfg()
        g = (((c.get("analyst_desk") or {}).get("opus_director") or {}).get("grading") or {})
        return "legacy" if str(g.get("scorecard_source") or "").strip().lower() == "legacy" else "tradable"
    except Exception:
        return "tradable"


def _dead_band(cfg: dict | None = None) -> float:
    try:
        c = cfg if cfg is not None else _cfg()
        return float(((c.get("analyst_desk") or {}).get("recommendations") or {}).get("dead_band_pct") or 0.5)
    except Exception:
        return 0.5


def _v2_grade(r: dict) -> dict | None:
    g = r["ex"].get("grade_v2")
    return g if isinstance(g, dict) and g.get("grade_version") == 2 else None


_EXIT_WORDS = {"stop": "your stop was hit", "stop_gap": "it opened past your stop",
               "target": "your target was reached", "target_gap": "it opened past your target",
               "horizon_close": "held to the end of your time frame"}


def _trade_result_line(r: dict) -> str | None:
    """One plain sentence saying how this call did as a real trade, or None when the
    trade-based grade does not apply (old grading selected, not graded yet, still running)."""
    try:
        if _score_source() != "tradable":
            return None
        g = _v2_grade(r)
        if g is None:
            return None
        st = g.get("status")
        if st == "not_entered":
            return f"never entered ({g.get('reason') or 'the entry price was not reachable'}); not scored"
        if st == "ungraded":
            return f"could not be scored ({g.get('reason') or 'no usable prices'})"
        if st != "graded":
            return None
        ent, ext = _num(g.get("tradable_entry_px")), _num(g.get("exit_px"))
        net, spy = _num(g.get("tradable_return_net_pct")), _num(g.get("spy_return_pct"))
        how = _EXIT_WORDS.get(str(g.get("exit_rule") or ""), "closed")
        line = (f"in at {ent if ent is not None else '?'} on {g.get('fill_session') or '?'}, "
                f"out at {ext if ext is not None else '?'} on {g.get('exit_session') or '?'} ({how})")
        if net is not None:
            line += f" = {net:+.2f}% after costs"
        if spy is not None:
            line += f"; S&P 500 over the same days {spy:+.2f}%"
        if g.get("option_vehicle"):
            line += " (scored on the stock, not the option)"
        return line + f" → {str(g.get('tradable_outcome') or '?').upper()}"
    except Exception:
        return None


# Knowledge base item 2 (2026-11-18). The memory sections still showed two numbers from the OLD grade (measured from
# the reference price, so a 07:00 call was credited with the overnight gap): the right/wrong label on each lesson
# and the per-checkpoint grades in YOUR HISTORY. Key analyst_desk.opus_director.grading.memory_source, read per call:
# "legacy" (absent = today, byte-identical) or "tradable" (both measured the way the scorecard already is).
_V2_LABEL = {"not_entered": "never entered", "ungraded": "not scored", "pending": "not scored yet"}


def _memory_source(cfg: dict | None = None) -> str:
    try:
        c = cfg if cfg is not None else _cfg()
        g = (((c.get("analyst_desk") or {}).get("opus_director") or {}).get("grading") or {})
        return "tradable" if str(g.get("memory_source") or "").strip().lower() == "tradable" else "legacy"
    except Exception:
        return "legacy"


# (Knowledge base review, 2026-11-19) Lessons imported from Nico's Thesis Log (tools/import_theses.py, tag src:public)
# are other people's public ideas, not the director's calls. They stay out of the director's input until
# analyst_desk.opus_director.lessons.include_public is exactly true (absent = today: none exist, nothing changes).
# On, they can show among RELEVANT LESSONS, whose heading then says so instead of "your own, from past calls".
_LESSONS_HEAD = "RELEVANT LESSONS (your own, from past calls)"
_LESSONS_HEAD_PUBLIC = "RELEVANT LESSONS (from past calls: your own, plus public ideas from others tagged src:public)"


def _public_lessons_on(cfg: dict | None = None) -> bool:
    try:
        c = cfg if cfg is not None else _cfg()
        return ((((c.get("analyst_desk") or {}).get("opus_director") or {}).get("lessons") or {})
                .get("include_public") is True)
    except Exception:
        return False


def _tradable_lesson_labels(lessons: list, recs: list) -> list:
    """Copies of the lessons whose call has a trade-based grade, labelled with it (right / wrong / neutral, or
    never entered / not scored). Interim lessons and lessons with no call of their own are left as they are. The
    stored rows are not touched."""
    by = {r["id"]: r for r in recs}
    out = []
    for l in lessons:
        r = by.get(l.get("rec_id") or "")
        g = _v2_grade(r) if (r is not None and l.get("outcome") is not None) else None
        if g is None:
            out.append(l)
            continue
        lab = (g.get("tradable_outcome") if g.get("status") == "graded"
               and g.get("tradable_outcome") in ("right", "wrong", "neutral") else _V2_LABEL.get(g.get("status")))
        out.append(dict(l, outcome=lab) if lab and lab != l.get("outcome") else l)
    return out


def _fmt_grade_tradable(r: dict, key: str, g: dict | None, dead: float) -> str:
    """A checkpoint measured from the trade's real entry price (the scorecard's rule, _checkpoint_outcome); a call
    the trade grader has not looked at yet keeps the old format; a checkpoint before the entry, or of a call that was
    never entered, is left out."""
    if not g or g.get("outcome") is None:
        return ""
    v2 = _v2_grade(r)
    if v2 is None:
        return _fmt_grade(key, g)
    oc = _checkpoint_outcome(r, g, "tradable", dead)
    if oc is None:
        return ""
    sd = g.get("session_date") if g.get("method") == "session_close" else None
    when = f" (close {str(sd)[5:10]})" if sd else ""
    if v2.get("status") == "graded" and sd and sd >= str(v2.get("exit_session") or "9"):
        return f"{key} {oc} (the trade closed by then)"
    entry, px = _num(v2.get("tradable_entry_px")), _num(g.get("price"))
    mv = (px - entry) / entry * 100.0 if entry and px is not None else None
    return f"{key} {oc}" + (f" {mv:+.2f}%" if mv is not None else "") + when


def _final_verdict(r: dict, source: str) -> tuple | None:
    """(outcome, move % in the call's direction) that counts as this call's final
    verdict, or None when it has none. tradable: the grade_v2 result — a call that was
    never entered, could not be graded or is still running is left out; a call the
    tradable grader has not looked at yet falls back to the legacy verdict."""
    if source == "tradable":
        import mason_desk_pipeline as PP          # the one rule shared with every other reader
        v = PP.tradable_verdict(r["ex"])
        if v is not None:
            return (v[1], _num(v[2])) if v[0] == "count" else None
    if r.get("status") == "graded" and r.get("outcome") in ("right", "wrong", "neutral"):
        return r["outcome"], _signed(r["direction"], (_final_grade(r) or {}).get("move_pct"))
    return None


def _checkpoint_outcome(r: dict, g: dict, source: str, dead: float):
    """One interim checkpoint's outcome. tradable: re-measured from the tradable entry
    (so a morning call gets no credit for the overnight gap); a call that was never
    entered has no checkpoints, and a checkpoint after the trade's own exit shows the
    trade's result."""
    if source != "tradable":
        return g.get("outcome")
    v2 = _v2_grade(r)
    if v2 is None:
        return g.get("outcome")
    entry, px, sd = _num(v2.get("tradable_entry_px")), _num(g.get("price")), str(g.get("session_date") or "")
    if not v2.get("entered") or not entry or px is None or g.get("outcome") is None:
        return None
    if sd and sd < str(v2.get("fill_session") or ""):
        return None
    if v2.get("status") == "graded" and sd and sd >= str(v2.get("exit_session") or "9"):
        return v2.get("tradable_outcome")
    mv = (px - entry) / entry * 100.0 * (-1.0 if r.get("direction") == "short" else 1.0)
    return "neutral" if abs(mv) <= dead else ("right" if mv > 0 else "wrong")


def scorecard(recs: list | None = None, kinds: dict | None = None, source: str | None = None) -> dict:
    """Opus's own directional record, computed from grades the pipeline wrote.
    FINAL = the declared-horizon verdict on graded rows; checkpoints = every snapshot
    (interim progress). Pure: never writes. `source` = "tradable" | "legacy"
    (default: _score_source())."""
    source = source or _score_source()
    dead = _dead_band()
    recs = _director_rows() if recs is None else recs
    recs = [r for r in recs if r.get("direction") in ("long", "short")]
    if kinds is None:
        kinds = _catalyst_kinds(r["ex"].get("catalyst_id") for r in recs)
    ck = {}
    for r in recs:
        for k in _grade_keys(r["ex"]):
            g = _pick_grade(r["ex"], k)
            oc = _checkpoint_outcome(r, g, source, dead) if g else None
            if oc in ("right", "wrong", "neutral"):
                ck.setdefault(k, {"right": 0, "wrong": 0, "neutral": 0})[oc] += 1
    finals = []
    for r in recs:
        fv = _final_verdict(r, source)
        if fv is not None:
            finals.append({"r": r, "outcome": fv[0], "conf": _num(r["ex"].get("confidence")),
                           "signed": fv[1],
                           "horizon": r["ex"].get("horizon") or "?", "vehicle": _vehicle(r["ex"]),
                           "kind": kinds.get(r["ex"].get("catalyst_id")) or "none"})

    def agg(items):
        n = len(items)
        right = sum(1 for f in items if f["outcome"] == "right")
        return {"n": n, "right": right, "wrong": sum(1 for f in items if f["outcome"] == "wrong"),
                "neutral": sum(1 for f in items if f["outcome"] == "neutral"),
                "hit": (right / n) if n else None}

    def group(key):
        g = {}
        for f in finals:
            g.setdefault(f[key], []).append(f)
        return {k: agg(v) for k, v in sorted(g.items())}

    buckets = {}
    for f in finals:
        c = f["conf"]
        b = ("<0.50" if c is None or c < 0.5 else "0.50-0.59" if c < 0.6
             else "0.60-0.69" if c < 0.7 else "0.70+")
        buckets.setdefault(b, []).append(f)
    calib = {}
    for b in ("<0.50", "0.50-0.59", "0.60-0.69", "0.70+"):
        if b in buckets:
            a = agg(buckets[b])
            cs = [f["conf"] for f in buckets[b] if f["conf"] is not None]
            a["avg_conf"] = (sum(cs) / len(cs)) if cs else None
            calib[b] = a
    wins = [f["signed"] for f in finals if f["outcome"] == "right" and f["signed"] is not None]
    loss = [f["signed"] for f in finals if f["outcome"] == "wrong" and f["signed"] is not None]
    allm = [f["signed"] for f in finals if f["signed"] is not None]
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None
    return {"source": source,
            "final": agg(finals), "checkpoints": ck, "by_horizon": group("horizon"),
            "by_vehicle": group("vehicle"), "by_kind": group("kind"), "calibration": calib,
            "avg_win_pct": avg(wins), "n_win": len(wins), "avg_loss_pct": avg(loss), "n_loss": len(loss),
            "avg_per_call_pct": avg(allm), "n_moves": len(allm)}


def _pct(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "n/a"


def _ck_line(ck: dict) -> str:
    return " · ".join(f"{k} n={sum(v.values())}: {v['right']}R/{v['wrong']}W/{v['neutral']}N"
                      for k, v in sorted(ck.items(), key=lambda kv: int(str(kv[0]).strip('+d') or 0)))


def scorecard_headline(sc: dict) -> str:
    f = sc["final"]
    return (f"final verdicts n={f['n']}" + (f", right {_pct(f['hit'])}" if f["n"] else "")
            + _flag(f["n"]) + (f" · checkpoints {_ck_line(sc['checkpoints'])}" if sc["checkpoints"] else ""))


def _render_scorecard(sc: dict, *, brief: bool = False) -> str:
    f = sc["final"]
    if brief:
        return "Your record: " + scorecard_headline(sc)
    trad = _score_source() == "tradable"
    if trad:
        lines = ["Long and short calls only (watch calls are not scored). Each call is scored as a real "
                 "trade: entered at the first price available after you made the call, and only if your "
                 "entry price could actually be reached. The trade ends at your stop or your target if the "
                 "price touched one, otherwise at the end of your stated time frame. A trading cost of "
                 "0.10% is taken off. Calls that could never have been entered are left out. Checkpoints "
                 "show progress so far. About 100 finished calls are needed before these numbers can be "
                 "trusted; right-rate = right / n (neutral counts as not right)."]
    else:
        lines = ["Directional calls only (watch excluded). FINAL = grade at the declared horizon; "
                 "checkpoints are interim progress. Stats need ~100 final verdicts to be reliable; "
                 "right-rate = right / n (neutral counts as not right)."]
    lines.append(f"FINAL: n={f['n']} · {f['right']}R/{f['wrong']}W/{f['neutral']}N"
                 + (f" · right {_pct(f['hit'])}" if f["n"] else "") + _flag(f["n"]))
    if sc["checkpoints"]:
        lines.append("Checkpoints (interim, incl. open calls): " + _ck_line(sc["checkpoints"]))
    if not f["n"]:
        lines.append("No call has reached its declared horizon yet — do NOT adjust confidence from this scorecard.")
        return "\n".join(lines)

    def grp(label, g):
        return f"{label}: " + " · ".join(f"{k} n={a['n']} right {_pct(a['hit'])}{' (n<10)' if a['n'] < _MIN_SAMPLE else ''}"
                                         for k, a in g.items())
    lines.append(grp("By horizon", sc["by_horizon"]))
    lines.append(grp("By vehicle", sc["by_vehicle"]))
    lines.append(grp("By catalyst kind", sc["by_kind"]))
    lines.append("Calibration (stated conf → realized right-rate): " + " · ".join(
        f"{b} (avg {_conf(a['avg_conf'])}) n={a['n']} → {_pct(a['hit'])}{' (n<10)' if a['n'] < _MIN_SAMPLE else ''}"
        for b, a in sc["calibration"].items()))
    fm = lambda x: f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"
    label = ("Average result per trade, after costs" if trad
             else "Expectancy proxy (underlying move in your direction)")
    lines.append(f"{label}: avg win {fm(sc['avg_win_pct'])} "
                 f"(n={sc['n_win']}) · avg loss {fm(sc['avg_loss_pct'])} (n={sc['n_loss']}) · "
                 f"avg per call {fm(sc['avg_per_call_pct'])} (n={sc['n_moves']}){_flag(sc['n_moves'])}")
    return "\n".join(lines)


def _lesson_asks() -> dict:
    try:
        v = json.loads(_state_get(_LESSON_ASKS_KEY, "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _newly_closed(recs: list, lessons: list, kinds: dict, now: datetime,
                  asks: dict | None = None) -> list:
    """Graded directional calls with no final (non-interim) lesson yet, closed within
    the look-back window and offered < _LESSON_MAX_ASKS times. Oldest first, ≤6."""
    have = {l.get("rec_id") for l in lessons if "interim" not in (l.get("tags") or [])}
    asks = _lesson_asks() if asks is None else asks
    cutoff = (now.replace(tzinfo=None) - timedelta(days=_CLOSED_WINDOW_DAYS)).isoformat()
    out = []
    for r in recs:
        if (r.get("status") != "graded" or r.get("outcome") not in ("right", "wrong", "neutral")
                or r.get("direction") not in ("long", "short") or r["id"] in have
                or int(asks.get(r["id"], 0) or 0) >= _LESSON_MAX_ASKS):
            continue
        closed_at = r.get("outcome_checked_date") or r.get("created_date") or ""
        if closed_at < cutoff:
            continue
        out.append(dict(r, closed_at=closed_at, event_kind=kinds.get(r["ex"].get("catalyst_id"))))
    out.sort(key=lambda r: r["closed_at"])
    return out[:_LESSONS_MAX]


def _render_closed(closed: list, slate_briefs: dict) -> str:
    out = []
    for r in closed:
        ex = r["ex"]
        g = _final_grade(r) or {}
        px = _num(r.get("eval_price")) or _num(g.get("price"))
        ent = _num(r.get("entry_ref_price"))
        mv = _num(g.get("move_pct"))
        when = (f" at {g.get('session_date')} close" if g.get("method") == "session_close" and g.get("session_date")
                else f" checked {str(r['closed_at'])[:10]}")
        wrong_if = _proves_wrong(r, slate_briefs.get(ex.get("slate_id") or "", ""))
        tl = _trade_result_line(r)
        result = (f"    {tl}" if tl else
                  f"    entry {ent if ent is not None else '?'} → {px if px is not None else '?'}"
                  + (f" ({mv:+.2f}%)" if mv is not None else "") + f"{when} → {str(r['outcome']).upper()}")
        out.append(f"- [{r['id']}] {_short_date(r.get('created_date'))} {_call_line(r)}"
                   + (f" · kind {r['event_kind']}" if r.get("event_kind") else "")
                   + f" — {str(ex.get('prediction') or '')[:140]}\n"
                   + result
                   + (f" | you said wrong if: {wrong_if}" if wrong_if else ""))
        if watch_enabled():
            story = _trade_story(r.get("ticker") or "", r.get("created_date") or "", r.get("closed_at"),
                                 direction=r.get("direction"), entry=r.get("entry_ref_price"))
            if story:
                out.append(f"    tape during the hold: {story}")
    return "\n".join(out)


def _render_lessons(lessons: list, tickers: list, kinds: set, skip: set, limit: int = 8) -> str:
    tset = set(tickers)
    pool = [l for l in lessons if l.get("id") not in skip]
    pool.sort(key=lambda l: (2 if str(l.get("ticker") or "").upper() in tset
                             else 1 if l.get("event_kind") and l.get("event_kind") in kinds else 0,
                             l.get("created_date") or ""), reverse=True)
    return "\n".join(f"- {str(l.get('created_date') or '')[:10]} {l.get('ticker') or 'MACRO'}"
                     f" ({l.get('outcome') or 'interim'}{', ' + l['event_kind'] if l.get('event_kind') else ''})"
                     + (f" [{', '.join(l['tags'][:4])}]" if l.get("tags") else "")
                     + f": {str(l.get('lesson') or '')[:220]}" for l in pool[:limit])


def memory_sections(mode: str, *, tickers: list, event_kinds: set, now: datetime | None = None,
                    focused: bool = False) -> tuple[list, dict]:
    """[(title, body, cap)] for the package + meta {closed: {rec_id: row}, prev_slate_id,
    scorecard_headline}. Read-only; any failure just drops that section."""
    now = now or _now_et()
    out, meta = [], {"closed": {}, "prev_slate_id": None}
    try:
        recs = _director_rows()
        lessons = _lesson_rows()
        kinds = _catalyst_kinds(r["ex"].get("catalyst_id") for r in recs)
    except Exception as e:
        log.info(f"[director] memory unavailable: {e}")
        return out, meta
    if _memory_source() == "tradable":
        try:
            lessons = _tradable_lesson_labels(lessons, recs)
        except Exception as e:
            log.info(f"[director] lesson labels unchanged: {e}")
    if not _public_lessons_on():                 # (KB review 2026-11-19) see _LESSONS_HEAD_PUBLIC
        lessons = [l for l in lessons if "src:public" not in (l.get("tags") or [])]
    slates = _slates(recs)
    try:
        body, meta["prev_slate_id"] = _render_prev_read(slates, now)
        if body:
            out.append(("PREVIOUS READ (your most recent director run — state continuity vs this)",
                        body, _MEM_CAPS["prev"]))
    except Exception as e:
        log.info(f"[director] memory prev-read failed: {e}")
    shown = set()
    try:
        body, shown = _render_history(recs, lessons, tickers)
        if body:
            out.append(("YOUR HISTORY ON THESE TICKERS (last ≤3 calls each + lessons)",
                        body, _MEM_CAPS["history"]))
    except Exception as e:
        log.info(f"[director] memory history failed: {e}")
    try:
        sc = scorecard(recs, kinds)
        meta["scorecard_headline"] = scorecard_headline(sc)
        if any(r.get("direction") in ("long", "short") for r in recs):
            if mode == "anchor":
                out.append(("YOUR SCORECARD (Opus's own graded record)", _render_scorecard(sc),
                            _MEM_CAPS["scorecard"]))
            else:
                out.append(("YOUR SCORECARD", _render_scorecard(sc, brief=True), _MEM_CAPS["scorecard_event"]))
    except Exception as e:
        log.info(f"[director] memory scorecard failed: {e}")
    try:
        closed = [] if focused else _newly_closed(recs, lessons, kinds, now)
        if closed:
            briefs = {s["slate_id"]: s["brief"] for s in slates if s["brief"]}
            meta["closed"] = {r["id"]: r for r in closed}
            out.append(("NEWLY CLOSED CALLS (write one lesson each in \"lessons\")",
                        _render_closed(closed, briefs), _MEM_CAPS["closed"]))
    except Exception as e:
        log.info(f"[director] memory newly-closed failed: {e}")
    try:
        body = _render_lessons(lessons, tickers, set(event_kinds or ()), shown)
        if body:
            head = _LESSONS_HEAD_PUBLIC if "[src:public" in body else _LESSONS_HEAD
            out.append((head, body, _MEM_CAPS["lessons"]))
    except Exception as e:
        log.info(f"[director] memory lessons failed: {e}")
    return out, meta


def record_lessons(items, *, closed: dict, open_ids=(), slate_id: str = "",
                   created_by: str = "opus_director") -> list:
    """Validate Opus's "lessons" and write director_lessons rows. rec_id must be a
    NEWLY CLOSED call (final lesson) or one of its open calls shown in the package
    (tagged "interim"). ≤6, lesson non-empty ≤300 chars, one final + one interim
    lesson per rec max. outcome/ticker/event_kind come from the rec, not the model.
    Never raises."""
    out = []
    if not isinstance(items, list):
        return out
    open_ids = set(open_ids or ())
    try:
        existing = _lesson_rows(limit=5000)
    except Exception:
        existing = []
    have_final = {l.get("rec_id") for l in existing if "interim" not in (l.get("tags") or [])}
    have_interim = {l.get("rec_id") for l in existing if "interim" in (l.get("tags") or [])}
    open_rows = None
    for it in items[:_LESSONS_MAX]:
        try:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("rec_id") or "").strip()
            text = " ".join(str(it.get("lesson") or "").split())[:300]
            if not rid or not text:
                continue
            tags = [str(t).strip().lower()[:40] for t in (it.get("tags") or []) if str(t).strip()][:6] \
                if isinstance(it.get("tags"), list) else []
            if rid in closed:
                if rid in have_final:
                    continue
                rec = closed[rid]
                tags = [t for t in tags if t != "interim"]
                outcome = rec.get("outcome")
                have_final.add(rid)
            elif rid in open_ids:
                if rid in have_interim:
                    continue
                if open_rows is None:
                    open_rows = {r["id"]: r for r in _director_rows()}
                rec = open_rows.get(rid)
                if not rec:
                    continue
                tags = ["interim"] + [t for t in tags if t != "interim"]
                outcome = None
                have_interim.add(rid)
            else:
                log.info(f"[director] lesson rejected — rec {rid} not in NEWLY CLOSED/open calls")
                continue
            kind = rec.get("event_kind") or _catalyst_kinds([rec["ex"].get("catalyst_id")]).get(
                rec["ex"].get("catalyst_id"))
            lid = uuid.uuid4().hex[:12]
            conn = mason_db.get_connection()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO director_lessons (id, rec_id, ticker, event_kind, outcome, lesson, "
                        "tags, source_slate_id, created_date, created_by, record_deleted) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                        (lid, rid, rec.get("ticker"), kind, outcome, text, json.dumps(tags),
                         slate_id, datetime.now().isoformat(), created_by))
            finally:
                conn.close()
            out.append({"id": lid, "rec_id": rid, "ticker": rec.get("ticker"), "outcome": outcome,
                        "lesson": text, "tags": tags})
        except Exception as e:
            log.warning(f"[director] lesson not stored ({it}): {e}")
    return out


def _bump_lesson_asks(rec_ids):
    if not rec_ids:
        return
    asks = _lesson_asks()
    for rid in rec_ids:
        asks[rid] = int(asks.get(rid, 0) or 0) + 1
    _state_set(_LESSON_ASKS_KEY, json.dumps(asks))


def memory_summary() -> dict:
    """Small summary for /api/opus_director."""
    try:
        recs = _director_rows()
        lessons = _lesson_rows(limit=5000)
        if not _public_lessons_on():             # (KB review 2026-11-19) the director's memory, as memory_sections
            lessons = [l for l in lessons if "src:public" not in (l.get("tags") or [])]
        return {"lessons": len(lessons),
                "lessons_interim": sum(1 for l in lessons if "interim" in l.get("tags", [])),
                "scorecard": scorecard_headline(scorecard(recs)),
                "latest_lesson": ({k: lessons[0].get(k) for k in ("created_date", "ticker", "outcome", "lesson")}
                                  if lessons else None)}
    except Exception as e:
        return {"error": str(e)}


def _catalysts_since(since_epoch: float | None, limit: int) -> list:
    try:
        import mason_triage as TR
        return TR.top_catalysts(since_epoch=since_epoch, limit=limit) or []
    except Exception as e:
        log.info(f"[director] catalysts unavailable: {e}")
        return []


def _dossier_for(ticker: str) -> dict | None:
    try:
        import mason_dossier as DS
        return DS.build_dossier(ticker)
    except Exception as e:
        log.info(f"[director] dossier {ticker} unavailable: {e}")
        return None


def _macro_dossier_for(kinds: list, hint: str = "") -> dict | None:
    try:
        import mason_dossier as DS
        return DS.build_macro_dossier(kinds, headline_hint=hint)
    except Exception as e:
        log.info(f"[director] macro dossier unavailable: {e}")
        return None


def _keep_option_rows_on() -> bool:
    """analyst_desk.opus_director.v2.writeups_keep_option_rows is exactly true (OFF by default). Never raises."""
    try:
        return v2_cfg().get("writeups_keep_option_rows") is True
    except Exception:
        return False


def _drop_empty_debate_on() -> bool:
    """(2026-11-19) analyst_desk.opus_director.v2.writeups_drop_empty_debate is exactly true (OFF by default): a
    company write-up with no usable debate leaves its DEBATE section out instead of the "no usable debate/verdict
    for … in the last 14d" line (mason_dossier._render_ticker, payload flag omit_empty_debate). Never raises."""
    try:
        return v2_cfg().get("writeups_drop_empty_debate") is True
    except Exception:
        return False


def _dossier_text(d: dict, max_tokens: int) -> str:
    try:
        import mason_dossier as DS
        if _keep_option_rows_on():             # (2026-11-18) a write-up short on room gives up its debate before any
            try:                               # option row (mason_dossier._keep_options_steps); every director run,
                return DS.dossier_text(d, max_tokens=max_tokens, keep_options=True)   # incl. the room rules' re-renders
            except TypeError:                  # a mason_dossier without that order: the text as before
                pass
        return DS.dossier_text(d, max_tokens=max_tokens)
    except Exception:
        return _clip_tokens(json.dumps(d, default=str), max_tokens)


_MACRO_KINDS = {"macro_data", "central_bank", "geopolitical", "trade_policy",
                "commodity", "prediction_market"}


def _cat_line(c: dict) -> str:
    tape = c.get("tape_json") or {}
    if isinstance(tape, str):
        try:
            tape = json.loads(tape)
        except Exception:
            tape = {}
    tk = ",".join(c.get("tickers") or []) if isinstance(c.get("tickers"), list) else (c.get("tickers") or "")
    first = datetime.fromtimestamp(c["first_seen"]).strftime("%m-%d %H:%M") if c.get("first_seen") else "?"
    if c.get("event_kind") == "watch_trigger":        # watchlist tape event, not a news story
        mv, rv, vs = _num(tape.get("chg_pct")), _num(tape.get("rel_volume")), _num(tape.get("vs_sector_pct"))
        bits = [f"move {mv:+.1f}% today" if mv is not None else None,
                f"relvol {rv:.1f}" if rv is not None else None,
                f"vs-sector {vs:+.1f}%" if vs is not None else None]
        return (f"- [{c.get('id')}] watch_trigger · {c.get('primary_ticker')} · score {c.get('score')}"
                f" · fired {first} ET" + "".join(f" · {b}" for b in bits if b)
                + f"\n    {c.get('headline') or c.get('title') or ''}")
    tp = ""
    if tape:
        tp = (f" | tape move {tape.get('move_pct')}% vs-sector {tape.get('vs_sector_pct')}%"
              f" relvol {tape.get('rel_volume')}")
    return (f"- [{c.get('id')}] {c.get('event_kind')} · {c.get('primary_ticker') or 'MACRO'}"
            f" ({tk}) · score {c.get('score')} · {c.get('n_events')} items/"
            f"{c.get('n_sources')} sources since {first} · velocity {c.get('velocity')}{tp}"
            f"\n    {c.get('title')}")


def build_package_text(*, mode: str, package: dict | None = None,
                       catalysts: list | None = None, budget_tokens: int = 32000,
                       max_dossiers: int = 4, followups: list | None = None,
                       memory: bool = True, lean: bool = False,
                       focus: dict | None = None) -> tuple[str, dict]:
    """Deterministic, budgeted text payload for the director. Returns (text, meta)
    where meta carries the ticker allow-list, the option chains the trade validator
    uses, catalyst ids, per-section token counts and (memory=True) the director
    memory bookkeeping (NEWLY CLOSED recs, open rec ids, memory token counts)."""
    package = package or {}
    catalysts = list(catalysts or [])
    followups = list(followups or [])
    sections, meta = [], {"tickers": set(), "chains": {}, "catalyst_ids": [],
                          "dossier_subjects": [], "sections": {}, "memory_tokens": {},
                          "closed": {}, "open_rec_ids": [], "prev_slate_id": None,
                          "followup_ids": [f["id"] for f in followups if f.get("id")]}

    def add(name, body, cap):
        body = _clip_tokens(body.strip(), cap)
        if body:
            sections.append(f"=== {name} ===\n{body}")
            meta["sections"][name] = _tok(body)

    now_et = _now_et()
    add("CONTEXT", f"mode={mode} · now {now_et.strftime('%a %Y-%m-%d %H:%M ET')} · "
        f"paper account (Alpaca) · " + ("your long/short calls are auto-placed" if lean else "observe-only"), 200)
    book_syms = set()
    if lean:                                   # P1: the live book + the measured record, up front
        try:
            import mason_agent_loop as AL
            btxt, book_syms = AL.book_text()
            add("BOOK (live Alpaca paper account)", btxt, 700)
            add("WHAT WORKS (your measured record — weight it above priors)", AL.what_works_text(), 450)
            nt = AL._opt("mason_nico_thesis", "thesis_text")
            if isinstance(nt, str) and nt.strip():
                add("NICO'S THESES (the owner's own hand trades — separate, labeled source)", nt, 500)
        except Exception as e:
            log.info(f"[director] lean book/record sections unavailable: {e}")
    if mode == "anchor" or focus:              # Step 4: what is held, per stock and per sector, and the limits on
        try:                                   # new trades (autotrade.sizing.show_book; OFF until Nico approves the text)
            import mason_sizing as SZ
            if SZ.show_book():
                # (review) a run asked for by a quick check has a small budget and is cut from the END, where
                # YOUR OPEN PREDICTIONS sits: it lists only its own stock's holdings (sector totals still cover
                # the whole account) under a smaller limit, like the quick check itself.
                _ftk = str((focus or {}).get("ticker") or "").upper()
                blk = SZ.book_block(tickers=[_ftk]) if (focus and _ftk) else SZ.book_block()
                if blk.get("body"):
                    add(blk["title"], blk["body"], 900 if focus else 1300)
        except Exception as e:
            log.info(f"[director] holdings-and-limits section unavailable: {e}")
    ft = str((focus or {}).get("ticker") or "").upper()
    if focus:                                  # a desk check asked for this run: say why, with its facts
        if ft:
            meta["tickers"].add(ft)
        add("WHY YOU ARE BEING ASKED NOW", _focus_text(focus), 1500)
    note_tickers = []
    if mode == "anchor" and not focus:         # "watch" answers and handed-over events from the desk checks
        try:
            import mason_desk_check as DC
            ntxt, nids, ntk = DC.director_notes()
            if ntxt:
                add("NOTES FROM THE QUICK CHECKS SINCE YOUR LAST REVIEW (each was a short look at one event between "
                    "your reviews; say what you make of each)", ntxt, 500)
                meta["check_note_ids"] = list(nids)
                note_tickers = sorted(ntk)
                meta["tickers"] |= set(note_tickers)
        except Exception as e:
            log.info(f"[director] desk-check notes unavailable: {e}")
    if mode == "anchor" and not focus and v2_cfg().get("earnings_blocks"):
        try:                                   # earnings as data (audit R5): dates and results, not headlines
            import mason_pead as PD
            for title, body, tks in PD.director_blocks(since_epoch=_last_run_epoch()):
                add(title, body, 350)
                meta["tickers"] |= {str(t).upper() for t in tks}
        except Exception as e:
            log.info(f"[director] earnings blocks unavailable: {e}")
    # Events calendar: dates only (wording approved by Nico 2026-11-15; switch calendar.director_block.enabled)
    if mode == "anchor" and not focus and ((_cfg().get("calendar") or {}).get("director_block") or {}).get("enabled") is True:
        try:
            import mason_calendar as CAL
            cal_lines = CAL.calendar_lines(days=5, max_lines=12)
            if cal_lines:
                add("ON THE CALENDAR (today and the next five trading days)", "\n".join(cal_lines), 300)
        except Exception as e:
            log.info(f"[director] calendar block unavailable: {e}")
    # Drug decision dates (news phase 6c; wording for Nico's approval in docs/fable/NEWS6_PROMPTS_FOR_NICO.md; switch
    # calendar.biocal.director_block, off by default: then nothing is added and the input is exactly as before)
    if mode == "anchor" and not focus:
        try:
            import mason_biocal as BIO
            BIO.director_block(add, mode=mode, focus=focus)
        except Exception as e:
            log.info(f"[director] drug-date block unavailable: {e}")
    # Overnight and pre-market read (audit R6; wording for Nico's approval in docs/fable/OVERNIGHT_PROMPTS_FOR_NICO.md;
    # switch overnight.director_block.enabled). The module adds it only on a trading day before the 09:30 open.
    ovn = None
    if mode == "anchor" and not focus and ((_cfg().get("overnight") or {}).get("director_block") or {}).get("enabled") is True:
        try:
            import mason_overnight as OVN
            ob = OVN.director_block(now_et)
            if ob and ob.get("lines"):
                add(ob["title"], "\n".join(ob["lines"]), int(ob.get("max_tokens") or 400))
                ovn = (ob["title"], list(ob["lines"]))
        except Exception as e:
            log.info(f"[director] overnight block unavailable: {e}")

    # follow-ups the director scheduled for itself (its only memory between runs)
    if followups:
        for f in followups:
            if f.get("ticker"):
                meta["tickers"].add(str(f["ticker"]).upper())
        add("DUE FOLLOW-UPS (you scheduled these — address each explicitly)",
            "\n".join(f"- [{f.get('id')}] due {f.get('due_date')} · {f.get('ticker') or 'MACRO'}: "
                      f"{f.get('action')}" + (f" — why: {f.get('reason')}" if f.get('reason') else "")
                      + f" (asked {str(f.get('created_date') or '')[:10]})" for f in followups), 800)

    # director MEMORY — continuity, per-ticker record, scorecard, closed calls, lessons
    if memory:
        mem_tickers = [str(f["ticker"]).upper() for f in followups if f.get("ticker")]
        for c in sorted(catalysts, key=lambda x: -(float(x.get("score") or 0))):
            if c.get("primary_ticker"):
                mem_tickers.append(str(c["primary_ticker"]).upper())
        for c in catalysts:
            mem_tickers += [str(t).upper() for t in (c.get("tickers") or []) if t]
        if ft:
            mem_tickers.insert(0, ft)
        mem_tickers = list(dict.fromkeys(mem_tickers))
        try:
            msecs, mmeta = memory_sections(mode, tickers=mem_tickers,
                                           event_kinds={c.get("event_kind") for c in catalysts if c.get("event_kind")},
                                           focused=bool(focus))
        except Exception as e:
            log.info(f"[director] memory sections failed: {e}")
            msecs, mmeta = [], {}
        for name, body, cap in msecs:
            add(name, _clip_tokens(body, max(20, cap - 10)), cap)   # room for the "truncated" marker
            meta["memory_tokens"][name.split(" (")[0]] = meta["sections"].get(name, 0)
        meta["closed"] = mmeta.get("closed") or {}
        meta["prev_slate_id"] = mmeta.get("prev_slate_id")
        # long-lived theses ("things we're watching for") — mason_theses, flag memory.theses.enabled
        try:
            import mason_theses as TH
            if TH.enabled():
                tx = TH.theses_section_text(tickers=mem_tickers, max_tokens=600)
                if tx and tx.strip():
                    add("OPEN THESES (early hints Mason is watching for confirmation)", tx, 600)
        except Exception as e:
            log.info(f"[director] theses section unavailable: {e}")

    # catalysts (the "as it drops" layer)
    if catalysts:
        meta["catalyst_ids"] = [c.get("id") for c in catalysts if c.get("id")]
        for c in catalysts:
            for t in (c.get("tickers") or []):
                meta["tickers"].add(str(t).upper())
            if c.get("primary_ticker"):
                meta["tickers"].add(c["primary_ticker"].upper())
        add("CATALYSTS (ranked by score)", "\n".join(_cat_line(c) for c in catalysts), 2500)

    # live-watched names: what each did since the last run (watchlist.enabled; design R11)
    if watch_enabled() and not focus:
        wt = _watch_section(mode, meta)
        if wt:
            add("WATCHLIST TAPE (live-watched names since your last run; [Mason]=yours [You]=Nico [Pos]=position)",
                wt, 1600 if mode == "anchor" else 600)

    # dossiers for the top ticker catalysts, then one macro dossier if any macro story
    dossier_budget = int(budget_tokens * (0.55 if mode == "anchor" else 0.65))
    ds_added = []                              # (name, dossier) of each write-up added, in order (see "keep_when_full")
    # due follow-ups get dossiers first, but at most max_followup_dossiers of them
    # (Nico 2026-09-28) so fresh overnight catalysts keep slots; the rest are still
    # listed in DUE FOLLOW-UPS, just without a dossier.
    fu_cap = int(v2_cfg().get("max_followup_dossiers", 3))
    tick_order = list(dict.fromkeys(str(f["ticker"]).upper() for f in followups if f.get("ticker")))[:fu_cap]
    if ft:                                     # the stock the check is about always gets the dossier
        tick_order = [ft] + [t for t in tick_order if t != ft]
    min_ds = float(v2_cfg().get("min_dossier_score", 0.65))
    for c in sorted(catalysts, key=lambda x: -(float(x.get("score") or 0))):
        t = c.get("primary_ticker")
        if t and float(c.get("score") or 0) >= min_ds and t.upper() not in tick_order:
            tick_order.append(t.upper())
    for t in note_tickers:                     # last in line: a note never takes a slot from a catalyst
        if t not in tick_order:
            tick_order.append(t)
    tick_order = tick_order[:max_dossiers]
    macro_kinds = sorted({c.get("event_kind") for c in catalysts
                          if c.get("event_kind") in _MACRO_KINDS})
    n_d = len(tick_order) + (1 if macro_kinds else 0)
    per = max(1500, dossier_budget // max(1, n_d)) if n_d else 0
    if lean and per:                           # the dossier ladder keeps tape/estimates/IV/chain, drops bulk news
        per = min(per, int(escalation_cfg().get("dossier_tokens", 2200)) if focus
                  else int(v2_cfg().get("lean_dossier_tokens", 1600)))
    for t in tick_order:
        d = _dossier_for(t)
        if not d:
            continue
        # (2026-11-19, v2.writeups_drop_empty_debate) a copy; every render of it, the room rules' too, leaves an empty
        # DEBATE section out
        if isinstance(d, dict) and _drop_empty_debate_on():
            d = dict(d, omit_empty_debate=True)
        meta["dossier_subjects"].append(t)
        meta["tickers"].add(t)
        for p in (d.get("peers") or []):
            if isinstance(p, dict) and p.get("ticker"):
                meta["tickers"].add(str(p["ticker"]).upper())
        chain = ((d.get("options") or {}).get("chain_sample") or []) \
            if isinstance(d.get("options"), dict) else []
        meta["chains"][t] = chain
        add(f"DOSSIER {t}", _dossier_text(d, per), per)
        ds_added.append((f"DOSSIER {t}", d))
    if macro_kinds:
        hint = next((c.get("title") or "" for c in catalysts
                     if c.get("event_kind") in _MACRO_KINDS), "")
        md = _macro_dossier_for(macro_kinds, hint)
        if md:
            for sym in (md.get("cross_asset") or {}):
                meta["tickers"].add(str(sym).upper())
            add("MACRO DOSSIER", _dossier_text(md, per), per)
            ds_added.append(("MACRO DOSSIER", md))

    # index desk (flag index_desk.enabled): SPY/QQQ/IWM state for 0-5 DTE calls + where idle cash would go
    if mode == "anchor" and (_cfg().get("index_desk") or {}).get("enabled"):
        try:
            import mason_etf_screen as ES
            add("INDEX OUTLOOK (deterministic facts for 1-5 session index calls)", ES.index_outlook_text(max_tokens=700), 700)
            add("ETF SCREEN (top 12, short-term momentum-with-trend)", ES.etf_screen_text(max_tokens=500, top=12), 500)
            for sym in ("SPY", "QQQ", "IWM"):
                meta["tickers"].add(sym)
            for r in (ES.etf_screen().get("rows") or [])[:12]:
                if r.get("symbol"):
                    meta["tickers"].add(str(r["symbol"]).upper())
        except Exception as e:
            log.info(f"[director] index desk sections unavailable: {e}")
    if lean and mode == "anchor":              # same-day SPY/QQQ contracts (only while the market is open)
        try:
            import mason_agent_loop as AL
            z = AL.zero_dte_chain()
            ztxt = AL.zero_dte_text(z)
            if ztxt:
                add("0DTE CHAIN (expires TODAY; Mason is flat by 15:15 ET)", ztxt, 500)
                for u, v in z.items():
                    if not u.startswith("_"):
                        meta["tickers"].add(u)
                        meta["chains"][u] = list(meta["chains"].get(u) or []) + v["rows"]
        except Exception as e:
            log.info(f"[director] 0DTE chain unavailable: {e}")

    # anchor-only desk material (untruncated where the budget allows)
    if mode == "anchor":
        ctx = package.get("contexts") or {}
        report = package.get("report_text") or ctx.get("report") or ""
        # lean: junior-desk prose is cut to the names actually in front of the director
        # (review fix R1) this set was named `focus`, which overwrote the new `focus` parameter: every
        # scheduled call then hid YOUR OPEN PREDICTIONS as if it were a run asked for by a desk check
        shown = set(tick_order) | book_syms | {str(c.get("primary_ticker") or "").upper() for c in catalysts}
        if lean:
            try:
                shown |= {str(o.get("ticker") or "").upper() for o in _open_predictions()}
            except Exception:
                pass
        add("QWEN DESK REPORT (junior analyst prose — verify before trusting)", report, 600 if lean else 3000)
        # ── RH Expansion S2-T8 ── (skip the desk copy of the RH block: the package carries it once, below)
        raw = "\n\n".join(f"[{k.upper()}]\n{v}" for k, v in ctx.items()
                          if k not in ("report", "director", "rh_evidence") and isinstance(v, str))
        # ── end S2-T8 ──
        add("RAW ANALYST CONTEXTS (system-sourced)", raw, 800 if lean else 3500)
        vc = package.get("verified_claims") or []
        an = package.get("recent_analyses") or []
        for a in an:
            if a.get("ticker"):
                meta["tickers"].add(str(a["ticker"]).upper())
        if lean:
            vc = [c for c in vc if str(c.get("ticker") or "").upper() in shown]
            an_show = [a for a in an if str(a.get("ticker") or "").upper() in shown]
        else:
            an_show = an
        add("VERIFIED CLAIMS (fact-checked against ingested sources)",
            "\n".join(f"- {c.get('ticker')}: {c.get('text')}" for c in vc[:(40 if lean else 100)]),
            900 if lean else 3000)
        add("RECENT PER-TICKER ANALYSES (qwen)",
            "\n".join(f"- {a.get('ticker')} ({a.get('sentiment')}, mat {a.get('materiality')}):"
                      f" {(a.get('summary') or '')[:(240 if lean else 400)]}"
                      for a in an_show[:(15 if lean else 40)]), 900 if lean else 2500)
        for r in (package.get("recommendations") or []):
            if r.get("ticker"):
                meta["tickers"].add(str(r["ticker"]).upper())

    # ── RH Expansion S2-T8 ──
    # the ONE composed "Robinhood & evidence" block (rh_expansion.brain_context, anchor
    # only). Its tickers stay OUT of meta["tickers"]: validate_predictions rejects block-only ideas.
    if mode == "anchor":
        try:
            import mason_rhx_integration as RHX
            blk = RHX.brain_block()
            if blk.get("text"):
                add(blk["title"], blk["body"], RHX.brain_max_tokens() + 20)
                meta["rhx_block_tickers"] = set(blk.get("tickers") or ())
                meta["rhx_block_dropped"] = list(blk.get("dropped") or ())
        except Exception as e:
            log.info(f"[director] RH evidence block unavailable: {e}")
    # ── end S2-T8 ──

    _OP_MAX = 30                               # (11-17, Nico) show every open call up to 30, newest first
    op = _open_predictions(limit=_OP_MAX + 1)
    op_more = max(0, len(op) - _OP_MAX)
    op = op[:_OP_MAX]
    meta["open_rec_ids"] = [o["rec_id"] for o in op]
    if op:
        for o in op:
            if o.get("ticker"):
                meta["tickers"].add(str(o["ticker"]).upper())
        won = watch_enabled()
        hidden = 0
        if focus:                              # a focused run sees only its own stock's open calls
            hidden = sum(1 for o in op if str(o.get("ticker") or "").upper() != ft)
            op = [o for o in op if str(o.get("ticker") or "").upper() == ft]
        if hidden:
            add("YOUR OTHER OPEN PREDICTIONS", f"{hidden} open call(s) on other stocks are not shown in this run; "
                "they wait for your next scheduled review.", 60)
    if op:
        add("YOUR OPEN PREDICTIONS (reaffirm / invalidate / close each if warranted)",
            "\n".join(f"- [{o['rec_id']}] {o['date']} {o['direction']} {o['ticker']} @ {o['entry']}"
                      f" ({o['horizon']}, conf {o['confidence']}) grades={json.dumps(o['grades'])}"
                      f" — {o['thesis']}"
                      + (lambda st: f"\n    since entry: {st}" if st else "")(
                          _trade_story(o["ticker"], o["date"], None, max_chars=160,
                                       direction=o.get("direction"), entry=o.get("entry")) if won else "")
                      + (f"\n    {o['no_put']}" if o.get("no_put") else "")   # (11-18) a short call that got no put
                      for o in op)
            + (f"\n({op_more} older open call(s) not shown; they are still graded at their horizon.)" if op_more else ""),
            max(1500, 60 + 175 * len(op))          # (11-17, Nico) sized by the number of calls, so none is cut
            + sum(_tok(o["no_put"]) + 4 for o in op if o.get("no_put")))   # (11-18) plus room for those sentences

    as_today = bool(ds_added) and _keep_option_rows_on() and _tok("\n\n".join(sections)) > budget_tokens
    if as_today:
        # (2026-11-18, writeups_keep_option_rows; review) Over its limit, every write-up first gets today's text back,
        # and the overnight section, the room rules and the end cut below run exactly as with the switch off (the
        # room rules re-render in today's order: render=_dossier_text_today). Then each write-up is written again in
        # the new order inside the room it ended up with (_writeups_new_order_within, just before the end cut). So no
        # write-up is longer, or shows fewer option rows or headlines, than with the switch off, and no other section
        # is shorter. (The first version re-rendered in the new order inside the room rules: under heavy pressure the
        # shared cut then fell differently, and the top-ranked write-up could lose an expiry that it kept with the
        # switch off.)
        _writeups_as_today(sections, meta, ds_added, per)
    render = _dossier_text_today if as_today else None

    if ovn:                                    # (overnight) it never takes room from the rest: over budget, it gives
        hdr, lines = f"=== {ovn[0]} ===\n", ovn[1]   # up its last lines, then goes, before the clip below can bite
        i = next((k for k, s in enumerate(sections) if s.startswith(hdr)), None)
        ob_cfg = (_cfg().get("overnight") or {}).get("director_block") or {}
        if i is not None and (ob_cfg.get("keep_when_full") is True or _room_rule_on(mode, focus)):
            # (Nico 2026-11-18: "Keep overnight section") Over budget, the company write-ups give up room first
            # (_writeups_give_room); only when that is not enough does the overnight section give way as before.
            # With both switches absent or false nothing here runs and the package is what it was.
            trimmed = _writeups_give_room(sections, meta, ds_added, budget_tokens,
                                          ob_cfg.get("dossier_floor_tokens") if ob_cfg.get("keep_when_full") is True
                                          else v2_cfg().get("writeup_floor_tokens"), render=render)
            if trimmed:
                meta["overnight_kept_by_trimming"] = trimmed
        while i is not None and _tok("\n\n".join(sections)) > budget_tokens:
            lines = lines[:-1]
            while len(lines) > 1 and lines[-1].endswith(":"):
                lines = lines[:-1]
            if len(lines) > 1:
                sections[i] = hdr + "\n".join(lines)
                meta["sections"][ovn[0]] = _tok("\n".join(lines))
            else:
                sections.pop(i)
                meta["sections"].pop(ovn[0], None)
                i = None
        meta["overnight_lines"] = len(lines) if i is not None else 0
    if not ovn and _room_rule_on(mode, focus) and sections and sections[-1].startswith("=== YOUR OPEN PREDICTIONS ("):
        # (Nico 2026-11-18: "Applying the same rule: yes") A scheduled review with no overnight section (13:00, or
        # that section off): over its limit, the write-ups make the room before the END of the input, which is
        # YOUR OPEN PREDICTIONS, is cut by the clip below. (review) Only when that list really is the end: with no
        # open calls the end is the Robinhood evidence block, and no write-up gives room for that. (review) And no
        # write-up loses an option row for it (rows_kept): what that cannot free is cut from the end as before.
        trimmed = _writeups_give_room(sections, meta, ds_added, budget_tokens, v2_cfg().get("writeup_floor_tokens"),
                                      rows_kept=True, render=render)
        if trimmed:
            meta["writeups_gave_room"] = trimmed
    if as_today:                               # (writeups_keep_option_rows; review) see as_today above
        kept = _writeups_new_order_within(sections, meta, ds_added, budget_tokens)
        if kept:
            meta["writeups_kept_to_size"] = kept
    text = "\n\n".join(sections)
    text = _clip_tokens(text, budget_tokens)
    meta["tokens"] = _tok(text)
    return text, meta


def _dossier_text_today(d: dict, max_tokens: int) -> str:
    """(2026-11-18 review, writeups_keep_option_rows) A write-up in today's order, whatever the switch says: the room
    rules use it when the input is over its limit with the switch on, so they decide exactly as with it off."""
    try:
        import mason_dossier as DS
        return DS.dossier_text(d, max_tokens=max_tokens)
    except Exception:
        return _clip_tokens(json.dumps(d, default=str), max_tokens)


def _writeups_as_today(sections, meta, ds_added, per) -> list:
    """(2026-11-18 review, writeups_keep_option_rows) The input is over its limit: each write-up that is exactly what
    the keep-option-rows order (mason_dossier._keep_options_steps) wrote at `per` gets today's text back, so what
    follows (overnight section, room rules, end cut) starts from the switch-off input. Changes `sections` and
    meta["sections"] in place; returns the names it changed. Never raises."""
    out = []
    try:
        import mason_dossier as DS
        for name, d in ds_added:
            dh = f"=== {name} ===\n"
            k = next((j for j, s in enumerate(sections) if s.startswith(dh)), None)
            if k is None or not per:
                continue
            on = _clip_tokens(DS.dossier_text(d, max_tokens=per, keep_options=True).strip(), per)
            if sections[k] != dh + on:
                continue
            off = _clip_tokens(DS.dossier_text(d, max_tokens=per).strip(), per)
            if off and off != on:
                sections[k] = dh + off
                meta["sections"][name] = _tok(off)
                out.append(name)
    except Exception:
        pass
    return out


def _writeups_new_order_within(sections, meta, ds_added, budget_tokens) -> list:
    """(2026-11-18 review, writeups_keep_option_rows) After the room rules: each write-up that is today's text is
    written again in the keep-option-rows order in no more characters than it has now, but only where that order
    writes something different at this size (today's order went past its last step before the option rows, so the
    new text keeps every line but the debate's and at least as many option rows and headlines). If the input is
    still over its limit, the one write-up the end cut lands in keeps today's text (the new order puts more option
    rows before the headlines, so the same cut would show fewer of them). Changes `sections` and meta["sections"] in
    place; returns the names it rewrote. Never raises."""
    out, old = [], {}
    try:
        import mason_dossier as DS
        for name, d in ds_added:
            dh = f"=== {name} ===\n"
            k = next((j for j, s in enumerate(sections) if s.startswith(dh)), None)
            if k is None or not isinstance(d, dict):
                continue
            cur = sections[k][len(dh):]
            n = len(cur)
            hi = -(-n // 4)                         # the smallest size whose room holds this text
            if not cur or cur.endswith("…[truncated]") or DS.dossier_text(d, max_tokens=hi).strip() != cur:
                continue                            # not today's whole text at this size: left alone
            if DS.dossier_text(d, max_tokens=hi, keep_options=True).strip() == cur:
                continue                            # the new order writes the same text here
            body = DS.dossier_text(d, max_tokens=n // 4, keep_options=True).strip()
            if body and len(body) <= n:
                old[k] = (name, sections[k], meta["sections"].get(name))
                sections[k] = dh + body
                meta["sections"][name] = _tok(body)
                out.append(name)
        if old and _tok("\n\n".join(sections)) > budget_tokens:
            limit, pos = budget_tokens * 4, 0       # _clip_tokens keeps the first budget_tokens * 4 characters
            for j, s in enumerate(sections):
                if pos < limit < pos + len(s) and j in old:
                    name, sections[j], meta["sections"][name] = old[j]
                    out.remove(name)
                pos += len(s) + 2
    except Exception:
        pass
    return out


def _room_rule_on(mode, focus) -> bool:
    """analyst_desk.opus_director.v2.writeups_give_room_when_full is true and this is a scheduled review (not an
    event run, not a run asked for by a quick check). Never raises."""
    try:
        return mode == "anchor" and not focus and v2_cfg().get("writeups_give_room_when_full") is True
    except Exception:
        return False


def _writeups_give_room(sections, meta, ds_added, budget_tokens, floor_setting=None, rows_kept=False,
                        render=None) -> list:
    """The input is over its limit: the company write-ups (DOSSIER sections) give up room, each written again
    smaller (asked for no less than the floor, 1500 = the least a write-up is given anyway; a write-up shortens in
    whole steps, so it can come out somewhat smaller). (review 2026-11-18) The cut is shared out over the write-ups,
    the lowest-ranked first, and in the first round a write-up gives only what it can give without losing
    option-chain rows (one "exp YYYY-MM-DD (" line per expiry in mason_dossier's text); then shared without that
    guard; then the last ones first, as needed. Changes `sections` and meta["sections"] in place; returns the names
    of the write-ups that got shorter ([] when the input fits or nothing could be shortened). Never raises.
    rows_kept=True (review 2026-11-18; the rule without an overnight section): only the first round, so no write-up
    loses an option row to make room; what is still over is cut from the end as before.
    render (review 2026-11-18): the function that writes a write-up at a size; None = _dossier_text (as before).
    build_package_text passes _dossier_text_today when writeups_keep_option_rows is on and the input is full."""
    render = render or _dossier_text
    trimmed = []
    try:
        try:
            floor = max(200, int(floor_setting or 1500))
        except Exception:                      # (review) also OverflowError: Infinity is legal in the JSON settings
            floor = 1500
        n_exp = lambda s: len(re.findall(r"^exp \d{4}-\d\d-\d\d \(", s, re.M))   # noqa: E731
        order = [(n, d) for n, d in reversed(ds_added) if (meta["sections"].get(n) or 0) > floor]
        for share, keep_chain in (((True, True),) if rows_kept else ((True, True), (True, False), (False, False))):
            for n_left, (name, d) in zip(range(len(order), 0, -1), order):
                over = _tok("\n\n".join(sections)) - budget_tokens
                if over <= 0:
                    break
                dh = f"=== {name} ===\n"
                k = next((j for j, s in enumerate(sections) if s.startswith(dh)), None)
                cur = meta["sections"].get(name)
                if k is None or not cur or cur <= floor:
                    continue
                want = -(-over // n_left) if share else over
                cap = max(floor, cur - want - 12)          # 12: room for the "truncated" marker
                try:
                    body = _clip_tokens(render(d, cap).strip(), cap)
                except Exception:
                    body = ""
                if keep_chain and n_exp(body) < n_exp(sections[k]):
                    continue
                if body and _tok(body) < cur:
                    sections[k] = dh + body
                    meta["sections"][name] = _tok(body)
                    if name not in trimmed:
                        trimmed.append(name)
    except Exception:
        pass
    return trimmed


# ── prompts ─────────────────────────────────────────────────────────────────────
# Frozen text (no timestamps) so the system block caches across calls.

_SYSTEM = """You are the DIRECTOR of Mason, a one-person equity + options trading desk run for Nico (an Alpaca PAPER account, ~$80k; his goal is +$20k by year end). A 24/7 junior desk (a small local model plus deterministic tools) has detected catalysts and assembled research dossiers. You are the senior, accountable decision-maker. Think like a professional trader at a broker-dealer: catalysts, positioning, second-order effects, risk/reward, timing.

WHAT YOU RECEIVE: ranked CATALYSTS (stories clustered from many sources, with velocity and tape confirmation), per-ticker DOSSIERS (profile, financials, estimates, street views, tape/technicals, options chain sample, peers, linkages, 30-day news, calendar, bull/bear debate verdict, track record), a MACRO DOSSIER (cross-asset moves + an event→asset transmission map), and on anchor runs the desk's raw contexts, verified claims and analyses, plus YOUR OPEN PREDICTIONS.

HOW TO WORK
1. Lead with what matters most right now. Explain WHY each name is moving: the catalyst, its timeline and how early we are.
2. For each name you act on, cover: fundamentals and outlook (growth, margins, estimates and revisions); competitors and peers (who wins or loses); what it affects and what affects it (suppliers, customers, sector ETF, macro sensitivities); sentiment and positioning (news tone, IV, implied move, flow if present); the bull case vs the bear case (engage the debate verdict when present); THE TRADE; and what would prove you wrong.
3. For macro or geopolitical events, reason through the transmission map to concrete instruments (e.g. crude, XLE, FXI/KWEB, SOXX, TLT) with direction and horizon.
4. Web research tools may be available, with a small budget. Use them only to verify a catalyst's key fact or to fill a decisive gap (a press release, filing, trial result, communiqué, app ranking). Prefer primary sources. Do not browse aimlessly.

HARD RULES
- Ground every number in the package or in a source you fetched. Never invent prices, levels, strikes or dates. Name the source of important facts.
- Trades are REAL PAPER ORDERS: Mason places your long/short calls automatically on the Alpaca paper account in small learning-size positions, enforces the exits you give, and grades the realized result. Vehicle is "stock", "call" or "put" (SINGLE-LEG ONLY: no spreads, straddles, condors or any multi-leg structure). For an option, choose the expiry and strike EXACTLY from that ticker's dossier options chain_sample. If no suitable contract is listed, use stock. Before the open, rows show "last X (indicative)" instead of a live bid/ask: those are valid picks. Use the last trade as your entry reference; the order is priced from live quotes when it is approved.
- Tickers must appear in the package (catalysts, dossiers, peers, macro instruments, analyses or your open predictions).
- Confidence = your probability that the trade makes money by its horizon (0.5 = coin flip). Use the full range honestly; do not default to 0.55. Take a directional view (long or short) whenever you see a skew of 0.53 or better: this account exists to learn from real, graded trades, so a small-edge call that is placed teaches more than a "watch". Use "watch" only when you have no view.
- Market regime / posture is information to weigh in sizing and confidence. It is NOT a veto on new entries.
- Every long/short call needs an exit plan: give "stop" and "target" in the trade block, set from this name's own volatility and levels (not a fixed percent), and state the invalidation. Mason enforces them and also closes a position when you send an update with action "close" or "invalidate". For a call or put, "entry_limit", "stop" and "target" in the trade block are the OPTION PREMIUM per contract (per share, as quoted in chain_sample); for stock they are the share price. A short view is expressed by buying a put (short stock is refused).
- 0DTE (same-day expiry) calls and puts on SPY or QQQ are allowed, only when the contract is listed in the package.
- Keep each open prediction honest: reaffirm, invalidate or close it, with a one-line reason.
- YOU HAVE NO MEMORY BETWEEN RUNS. If you defer anything to a future date or event (e.g. "look again ahead of 10-29 earnings", "re-check after the trial readout"), you MUST add it to "follow_ups" with a concrete date (the trading day you want to re-research it, usually 1-2 days BEFORE the event). Mason stores it, sets a reminder, and puts it back in front of you on that day, with a fresh dossier. A deferral that isn't in follow_ups will be forgotten.
- A "DUE FOLLOW-UPS" section lists things you asked to revisit. Address each one explicitly in the brief (act, pass, or reschedule with a new follow_up).
- INDEX OUTLOOK / ETF SCREEN (when present): include at least one index call (SPY, QQQ or IWM; horizon "0-5DTE") or say why there's no edge (direction "watch"), and one line on where idle cash would go now from the ETF SCREEN. These are graded like every other call.
- THESES: when a headline HINTS at a future development that is not yet confirmed (e.g. "NVDA said to be accelerating consumer GPU supply"), add it to "theses" with the concrete phrases that would CONFIRM it and those that would BREAK it. Mason watches every future headline for them and alerts Nico, with position P&L, when one lands. The OPEN THESES section shows what is already being watched: build on those, don't duplicate them.

MEMORY (your only record of past runs; Mason builds it deterministically from its database)
- PREVIOUS READ: your most recent run (its market view, calls and Market read excerpt). YOUR HISTORY ON THESE TICKERS: your last calls on names in this package, with grades and your own update notes. YOUR SCORECARD: your graded record. NEWLY CLOSED CALLS: calls that just reached their declared horizon. RELEVANT LESSONS: lessons you wrote earlier.
- Continuity: open the brief with ONE line stating whether your view is consistent with or changed from PREVIOUS READ, and why. Reference prior calls explicitly when relevant (e.g. "as I said at 07:00...", "this reverses my 09-25 AKAM call because..."). Do NOT invent prior statements: only reference what PREVIOUS READ / HISTORY actually shows. If a section is absent, you have no record of it.
- Calibration: use the scorecard to calibrate confidence. FINAL verdicts count; checkpoints are interim. Ignore any stat flagged "insufficient sample (n<10)". If a category (horizon, vehicle, catalyst kind, confidence bucket) underperforms with n>=10, say so and adjust. Judge yourself on expectancy (average move captured), not win rate alone.
- Lessons: for EACH NEWLY CLOSED call, write one lesson: specific and reusable (what to repeat or avoid next time on this kind of setup), not a recap. You may also add a lesson on a still-open call whose thesis clearly broke (e.g. invalidation hit). Apply RELEVANT LESSONS where they fit.

OUTPUT FORMAT
First, a markdown research brief that opens with one line "**Continuity:** ..." and then a "## Market read" section, then one "## <TICKER> — <long|short|watch>" section per name. Use these bold sub-heads: **Why it's moving**, **Fundamentals & outlook**, **Competitors & peers**, **What it affects / what affects it**, **Sentiment & positioning**, **Bull vs bear**, **The trade**, **What proves me wrong**. Be dense and specific; aim for 150-300 words per name.
Then END with exactly one fenced ```json block, and nothing after it:
{"market_view": "<=40 words", "continuity": "<=40 words: consistent with / changed from PREVIOUS READ, and why",
 "predictions": [{"ticker": "XYZ", "direction": "long|short|watch", "horizon": "0-5DTE|swing|30d|90d+",
   "confidence": 0.0, "thesis": "<=60 words", "catalyst_id": "<id from CATALYSTS or null>",
   "target": null, "invalidation": "<price or event>",
   "trade": {"vehicle": "stock|call|put", "strike": null, "expiry": "YYYY-MM-DD or null",
             "entry_limit": null, "stop": null, "target": null, "size_hint": "e.g. 1-2% of account"},
   "evidence": ["<dossier section or source URL>"]}],
 "updates": [{"rec_id": "<id>", "action": "reaffirm|invalidate|close", "note": "<one line>"}],
 "follow_ups": [{"ticker": "XYZ or null for macro", "date": "YYYY-MM-DD", "action": "<what to research/decide then>", "reason": "<why then>"}],
 "lessons": [{"rec_id": "<id from NEWLY CLOSED, or an open call whose thesis broke>", "lesson": "<=30 words, specific and reusable", "tags": ["e.g. stale_catalyst", "iv_rich"]}],
 "theses": [{"title": "<short name>", "thesis": "<the hypothesis>", "tickers": ["XYZ"], "direction": "bullish|bearish|neutral", "confirm_signals": ["<phrase>", "..."], "break_signals": ["<phrase>", "..."], "review_by": "YYYY-MM-DD"}]}"""


# Watchlist addendum (docs/watchlist/WATCHLIST_DESIGN.md R11). Appended ONLY when
# watchlist.enabled, so with the flag off the cached system block is byte-for-byte _SYSTEM.
_WATCH_RULES = """

WATCHLIST (a WATCHLIST TAPE section is present)
- Mason streams 1-minute bars on every watched name and records deterministic events (VWAP crosses, opening-range breaks, level crosses, volume spikes, moves vs SPY/sector, position P&L steps), a short qwen digest and the options IV / implied move. The WATCHLIST TAPE section shows what each name did since your last run. Use it: it is your eyes between runs.
- Badges: [Mason] = you added it, [You] = Nico added it, [Pos] = an open position. You can NEVER drop a [You] item.
- To start watching a name that needs real-time attention (a setup forming, a level you want to act on, a catalyst ahead), add it to "watchlist" with the price levels that matter. Drop names whose reason is gone. Update levels as they change.
- The list is capped (shared with Nico). When the header shows it is full, an add MUST name one of YOUR [Mason] items in "replaces" plus "replace_reason"; otherwise the add is refused. Every swap is reported to Nico.
- Add to the final JSON:
 "watchlist": [{"action": "add|drop|update", "ticker": "XYZ", "reason": "<why watch / why drop>", "horizon": "intraday|swing|long", "levels": [{"price": 0.0, "side": "above|below", "label": "breakout|stop|support|target"}], "replaces": "<your [Mason] ticker to drop when full, else null>", "replace_reason": "<why that one goes>"}]"""


def _watch_cfg(cfg: dict | None = None) -> dict:
    try:
        return (cfg or _cfg()).get("watchlist") or {}
    except Exception:
        return {}


def watch_enabled(cfg: dict | None = None) -> bool:
    return bool(_watch_cfg(cfg).get("enabled"))


_LEAN_BRIEF = ('First, a SHORT markdown brief: one line "**Continuity:** ...", then a "## Market read" section '
               '(at most 100 words), then one "## <TICKER> — <long|short|watch>" section per name of AT MOST 50 words: '
               'the catalyst and why now, your edge, and what proves you wrong. No sub-heads and no recap of the '
               'dossier. Do the full analysis in your reasoning; the JSON carries the decision.')
_LEAN_RULES = """
- WHAT WORKS is your measured record (graded calls and the real P&L of placed trades). Weight it above your priors: lean toward the horizons, vehicles and setups that have paid, and say so when you go against it.
- BOOK is the live Alpaca paper account. [Mason rec ...] positions are yours: manage each one (reaffirm, or close/invalidate via "updates" with its rec_id). [Nico placed] STOCK positions are also yours to manage: to sell all or part of one, add it to "book" (at most 3 per run; "qty": null sells all of it) with a one-line reason. Options tagged "never touched" cannot be sold.
- NICO'S THESES (when present) are the owner's own hand trades with his reasoning and results: a separate, labeled source. Learn from what worked for him; never count them as your record.
- 0DTE CHAIN (when present) lists today's SPY/QQQ contracts with live quotes. A 0DTE call or put must use one of those rows exactly, horizon "0-5DTE"; Mason closes it by 15:15 ET the same day.
- "exp_move_pct" = the move you expect in the UNDERLYING, in percent, in your direction by the horizon (e.g. 3.5). Position size is set by Mason, so "size_hint" is ignored; make sure one option contract costs less than the cap shown in WHAT WORKS."""


def _lean_system() -> str:
    s = _SYSTEM
    a = s.index("First, a markdown research brief")
    b = s.index("aim for 150-300 words per name.") + len("aim for 150-300 words per name.")
    s = s[:a] + _LEAN_BRIEF + s[b:]
    k = "\n- YOU HAVE NO MEMORY BETWEEN RUNS."
    assert s.count(k) == 1 and s.count('"confidence": 0.0, ') == 1
    s = s.replace(k, _LEAN_RULES + k)
    k2 = "2. For each name you act on, cover:"
    k3 = '\n "updates": ['
    assert s.count(k2) == 1 and s.count(k3) == 1
    s = s.replace(k2, "2. For each name you act on, weigh (in your reasoning, NOT in the brief):")
    s = s.replace(k3, '\n "book": [{"symbol": "XYZ", "action": "sell", "qty": null, "reason": "<one line>"}],' + k3)
    return s.replace('"confidence": 0.0, ', '"confidence": 0.0, "exp_move_pct": 0.0, ')


def _is_lean(kind: str, v2: dict | None = None) -> bool:
    """lean_kinds entries may be "all", an exact kind, or a role: "morning" / "midday", or
    an old kind name standing for its role ("anchor_1300" keeps meaning the midday call
    after its time is moved to, say, 12:30). Manual runs match only by exact name."""
    kinds = [str(k) for k in ((v2 or v2_cfg()).get("lean_kinds") or [])]
    if "all" in kinds or str(kind) in kinds:
        return True
    role = anchor_role(kind)
    return bool(role) and any(anchor_role(k) == role for k in kinds)


def _system(cfg: dict | None = None, lean: bool = False, watch: bool = True) -> str:
    """watch=False (a run asked for by a desk check): no WATCHLIST TAPE section is sent,
    so the addendum that describes it is left out too."""
    base = _lean_system() if lean else _SYSTEM
    base = _short_put_words(base)              # untouched unless autotrade.short_as_put.enabled AND .tell_director
    return base + _WATCH_RULES if (watch and watch_enabled(cfg)) else base


def _short_put_words(base: str) -> str:
    """Short calls placed as puts (wording for Nico's approval: docs/fable/SHORTPUTS_PROMPTS_FOR_NICO.md).
    With either switch off, or on any failure, the text comes back exactly as it went in."""
    try:
        import mason_short_put as SP
        return SP.director_words(base)
    except Exception:
        return base


def _last_run_epoch() -> float:
    """Epoch of the newest Opus director slate (the WATCHLIST TAPE window starts there);
    24h ago when there is none. Never raises."""
    try:
        conn = mason_db.get_connection()
        try:
            r = conn.execute("SELECT MAX(created_date) AS d FROM recommendations WHERE kind='director' "
                             "AND created_by='opus_director' AND COALESCE(record_deleted,0)=0").fetchone()
        finally:
            conn.close()
        if r and r["d"]:
            return datetime.fromisoformat(str(r["d"])[:19]).timestamp()
    except Exception:
        pass
    return time.time() - 86400


def _watch_section(mode: str, meta: dict) -> str:
    """WATCHLIST TAPE body: slot header (used/cap, swappable [Mason] items) + the
    deterministic tape text from mason_watch_signals. Watched symbols join the ticker
    allow-list. '' when unavailable. Never raises."""
    try:
        import mason_watchlist as WL
        import mason_watch_signals as SG
    except Exception as e:
        log.info(f"[director] watchlist modules unavailable: {e}")
        return ""
    head = ""
    try:
        c = WL.counts() or {}
        mine = [str(i.get("ticker")).upper() for i in (WL.list_items(status="active", origin="opus") or [])
                if i.get("ticker")]
        head = (f"Slots used {c.get('active', 0)}/{c.get('cap', 25)} (Nico {c.get('nico', 0)}, "
                f"yours {c.get('opus', 0)}); positions + index/sector ETFs are watched outside the cap."
                + (f" Your [Mason] items (droppable / swappable): {', '.join(mine)}." if mine
                   else " You have no [Mason] items yet."))
        if int(c.get("free", 1) or 0) <= 0:
            head += " LIST IS FULL: any add must name 'replaces'."
    except Exception as e:
        log.info(f"[director] watchlist counts unavailable: {e}")
    try:
        for t in (WL.universe() or {}):
            meta["tickers"].add(str(t).upper())
    except Exception:
        pass
    body = ""
    try:
        cap = 1600 if mode == "anchor" else 600
        body = SG.tape_section_text(since_epoch=_last_run_epoch(), max_tokens=cap - 120, mode=mode) or ""
    except Exception as e:
        log.info(f"[director] watch tape unavailable: {e}")
    return (head + "\n" + body).strip() if (head or body) else ""


def _watch_ref(ticker: str) -> dict | None:
    try:
        import mason_watch_signals as SG
        return SG.watch_ref(ticker)
    except Exception:
        return None


def _trade_story(ticker: str, start_iso: str, end_iso: str | None = None, max_chars: int = 240,
                 *, direction: str | None = None, entry=None) -> str:
    """Hold-period story from the CALL's side and entry (a short's favorable move is down)."""
    try:
        import mason_watch_signals as SG
        side = "short" if str(direction or "").lower() == "short" else "long"
        return SG.trade_story(ticker, start_iso, end_iso, max_chars=max_chars, side=side,
                              entry_price=_num(entry)) or ""
    except Exception:
        return ""


def _store_trade_stories(closed: dict) -> int:
    """R12: persist the hold-period tape story on each newly closed rec
    (extra_json.trade_story) — the intraday detail it rests on is pruned after 30 days.
    Idempotent: an existing story is kept. Returns rows written. Never raises."""
    n = 0
    for rid, r in (closed or {}).items():
        try:
            story = _trade_story(r.get("ticker") or "", r.get("created_date") or "", r.get("closed_at"),
                                 max_chars=500, direction=r.get("direction"),
                                 entry=r.get("entry_ref_price"))
            if not story:
                continue
            conn = mason_db.get_connection()
            try:
                with conn:
                    row = conn.execute("SELECT extra_json FROM recommendations WHERE id=?", (rid,)).fetchone()
                    if not row:
                        continue
                    ex = _loads(row["extra_json"])
                    if ex.get("trade_story"):
                        continue
                    ex["trade_story"] = {"text": story, "at": datetime.now().isoformat(timespec="seconds"),
                                         "window": [r.get("created_date"), r.get("closed_at")]}
                    conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                                 (json.dumps(ex, default=str), rid))
                    n += 1
            finally:
                conn.close()
        except Exception as e:
            log.info(f"[director] trade story {rid}: {e}")
    return n


def _render_watch_changes(ch: dict | None) -> list:
    """Notification lines for what the director did to the watchlist (D4: swaps say
    what was dropped and why)."""
    if not isinstance(ch, dict):
        return []
    out = []
    if ch.get("added"):
        out.append("Added: " + ", ".join(str(a.get("ticker") if isinstance(a, dict) else a) for a in ch["added"]))
    for s in ch.get("swapped") or []:
        out.append(f"Swapped: dropped **{s.get('dropped')}** for **{s.get('added')}** — {s.get('why') or 'no reason given'}")
    if ch.get("dropped"):
        out.append("Dropped: " + "; ".join(
            (f"{d.get('ticker')} — {d.get('why') or d.get('reason') or ''}" if isinstance(d, dict) else str(d))
            for d in ch["dropped"]))
    if ch.get("updated"):
        out.append("Levels updated: " + ", ".join(str(u.get("ticker") if isinstance(u, dict) else u)
                                                  for u in ch["updated"]))
    if ch.get("refused"):
        out.append("Refused: " + "; ".join(f"{r.get('ticker')} — {r.get('why')}" for r in ch["refused"]
                                           if isinstance(r, dict)))
    return (["", "**Watchlist changes (Opus):**"] + [f"- {x}" for x in out]) if out else []


_FOCUS_HEAD = ("DECISION ASKED FOR BETWEEN YOUR TWO DAILY REVIEWS. A quick check on {ticker} asked for your "
               "decision now. Decide on {ticker} and on anything in the book this event changes. Leave everything "
               "else for your next scheduled review. If nothing should be done, say \"watch\" and why.")


def _focus_text(focus: dict) -> str:
    """Body of the section that tells the director why it was called out of schedule."""
    d = {"long": "It expects the price to rise.", "short": "It expects the price to fall."}.get(
        str(focus.get("direction") or "").lower(), "")
    L = [f"At {focus.get('at') or '?'} Eastern a quick check on {focus.get('ticker')} asked for a full decision "
         f"now instead of waiting for your next scheduled review.",
         f"Its reason: {focus.get('reason') or 'not given'}"]
    if d:
        L.append(d)
    if focus.get("facts"):
        L += ["What the check was shown:", str(focus["facts"])]
    return "\n".join(L)


def _user_prompt(mode: str, kind: str, text: str, max_preds: int, web: bool = False,
                 focus: dict | None = None, memory_tools: bool | None = None) -> str:
    """memory_tools: None = say so whenever Mason's memory tools are switched on (as before);
    False = this run is sent without them, so they are not mentioned."""
    head = {"anchor": f"ANCHOR RUN ({kind}). Give the full slate for the book and the catalysts below.",
            "event": "EVENT RUN — a catalyst just crossed the desk's threshold. Decide fast and precisely."}[mode]
    if focus:
        head = _FOCUS_HEAD.format(ticker=str(focus.get("ticker") or "this stock").upper())
    web_note = ("Web tools are available this run (small budget — verify the decisive fact only)."
                if web else "No web tools this run: work from the package only and say what you'd verify.")
    if _search_tools_on() if memory_tools is None else memory_tools:
        web_note += (" Mason's memory tools are available: search_memory (every past headline, brief, call, "
                     "lesson, thesis, journal) and query_db (read-only SQL) — use them when the package "
                     "lacks context you need (e.g. what you said before on a name, how a past thesis played out); "
                     "a few targeted calls, not a sweep.")
    return (f"{head} At most {max_preds} predictions. {web_note}\n\n{text}\n\n"
            "Write the brief, then the final JSON block.")


# ── parse + validate ────────────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_output(text: str) -> tuple[str, dict | None, str | None]:
    """(brief_markdown, obj, error). Takes the LAST fenced json block."""
    text = text or ""
    blocks = list(_JSON_FENCE.finditer(text))
    if not blocks:
        # tolerate a bare trailing object
        i = text.rfind('{"market_view"')
        if i < 0:
            return text.strip(), None, "no JSON block"
        raw, brief = text[i:], text[:i]
    else:
        m = blocks[-1]
        raw, brief = m.group(1), text[:m.start()]
    try:
        return brief.strip(), json.loads(raw), None
    except Exception as e:
        return brief.strip(), None, f"bad JSON: {e}"


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def validate_predictions(obj: dict, meta: dict, *, max_preds: int,
                         universe: set | None = None) -> list:
    """Normalize + enforce the hard rules deterministically. Never raises."""
    import mason_desk_pipeline as PP
    allowed = set(meta.get("tickers") or set()) | set(universe or set())
    out = []
    for p in (obj.get("predictions") or [])[:max_preds]:
        if not isinstance(p, dict):
            continue
        notes = []
        t = str(p.get("ticker") or "").upper().strip()
        if not t or t in ("NONE", "N/A", "NA", "NULL", "-"):
            continue
        # ── RH Expansion S2-T8 ── (SC-D1 + RH-D3): block-only support → rejected, never persisted
        if "rhx_block_tickers" in meta:
            try:
                import mason_rhx_integration as RHX
                why = RHX.only_block_support(dict(p, ticker=t), meta)
            except Exception:
                why = None
            if why:
                meta.setdefault("rhx_rejected", []).append({"ticker": t, "why": why})
                log.info(f"[director] slate item {t} rejected: {why}")
                continue
        # ── end S2-T8 ──
        direction = PP._norm_direction(p.get("direction"))
        if allowed and t not in allowed:
            notes.append("ticker not in package/universe → watch")
            direction = "watch"
        hlabel, hdays = PP._canon_horizon(p.get("horizon"))
        conf = _num(p.get("confidence"))
        conf = max(0.0, min(1.0, conf)) if conf is not None else None
        trade = p.get("trade") if isinstance(p.get("trade"), dict) else {}
        vehicle = str(trade.get("vehicle") or "stock").lower().strip()
        if vehicle not in ("stock", "call", "put"):
            notes.append(f"vehicle '{vehicle}' not allowed (single-leg only) → stock")
            vehicle = "stock"
        strike, expiry = _num(trade.get("strike")), (trade.get("expiry") or None)
        if vehicle in ("call", "put"):
            chain = (meta.get("chains") or {}).get(t) or []
            hit = next((c for c in chain
                        if str(c.get("type", "")).lower().startswith(vehicle[0])
                        and _num(c.get("strike")) == strike
                        and str(c.get("expiry")) == str(expiry)), None)
            if hit is None:
                notes.append("option contract not in dossier chain_sample → stock")
                vehicle, strike, expiry = "stock", None, None
            else:
                trade["contract_quote"] = {k: hit.get(k) for k in ("bid", "ask", "last", "mark", "mark_src",
                                                                   "iv", "oi", "volume")}
                if hit.get("mark_src") == "last_trade":
                    notes.append("indicative price (no live quote at run time) — live quote used at approval")
        out.append({
            "ticker": t, "direction": direction, "horizon": hlabel, "horizon_days": hdays,
            "confidence": conf, "exp_move_pct": _num(p.get("exp_move_pct")),
            "thesis": str(p.get("thesis") or "").strip()[:600],
            "catalyst_id": p.get("catalyst_id") if p.get("catalyst_id") in (meta.get("catalyst_ids") or []) else None,
            "target": _num(p.get("target")), "invalidation": str(p.get("invalidation") or "")[:300],
            "trade": {"vehicle": vehicle, "strike": strike, "expiry": expiry,
                      "entry_limit": _num(trade.get("entry_limit")), "stop": _num(trade.get("stop")),
                      "target": _num(trade.get("target")),
                      "size_hint": str(trade.get("size_hint") or "")[:80],
                      **({"contract_quote": trade["contract_quote"]} if "contract_quote" in trade else {})},
            "evidence": [str(e)[:200] for e in (p.get("evidence") or [])][:8],
            "validation_notes": notes,
        })
    return out


# ── persistence + notification ──────────────────────────────────────────────────

def _persist(preds, *, created_by, tier, model_id, kind, slate_id, brief, cost,
             updates, citations, price_fn, horizons, market_view: str = "",
             continuity: str = "", prompt_hash: str | None = None, origin: dict | None = None) -> list:
    import mason_desk_pipeline as PP
    rows = []
    # entry_ref_price is the quote at decision time. Outside market hours that is the
    # LAST CLOSE — a reference, not a price Mason could trade at; the tradable entry is
    # set later by the grader (extra_json.grade_v2). entry_ref_kind says which it is.
    try:
        ref_kind = ("decision_quote" if PP._entry_session(datetime.now())[1] == "intraday"
                    else "reference_market_closed")
    except Exception:
        ref_kind = None
    for i, p in enumerate(preds):
        entry = None
        if p["direction"] in ("long", "short"):
            try:
                entry = float(price_fn(p["ticker"]))
            except (TypeError, ValueError):
                entry = None
        extra = {"schema": _SCHEMA, "anchor_kind": kind, "ab_pair_id": slate_id,
                 "slate_id": slate_id, "slate_index": i, "tier": tier, "model_id": model_id,
                 "prediction": p["thesis"], "sentiment": {"long": "bullish", "short": "bearish"}.get(p["direction"], "neutral"),
                 "confidence": p["confidence"], "horizon": p["horizon"], "horizon_days": p["horizon_days"],
                 "checkpoints_days": sorted(d for d in horizons if d <= p["horizon_days"]),
                 "grades": {}, "ok": True, "error": None,
                 "trade": p["trade"], "target": p["target"], "invalidation": p["invalidation"],
                 "catalyst_id": p["catalyst_id"], "evidence": p["evidence"],
                 "validation_notes": p["validation_notes"],
                 "exp_move_pct": p.get("exp_move_pct"),
                 # the run's cost is shared across its calls, so cost per call/trade is measurable
                 "cost_usd": round(float(cost or 0) / max(1, len(preds)), 6)}
        if prompt_hash:                      # which prompt version made this call (replay, audit L6)
            extra["prompt_hash"] = prompt_hash
        if origin:                           # a desk check asked for this run: kept so the result can be graded
            extra["escalated_by_check"] = origin.get("id")
            extra["escalation_trigger"] = origin.get("trigger")
        if entry is not None and ref_kind:
            extra["entry_ref_kind"] = ref_kind
        if watch_enabled():
            wr = _watch_ref(p["ticker"])
            if wr:
                extra["watch_ref"] = wr
        if i == 0:
            extra.update({"brief": brief, "updates": updates, "citations": citations,
                          "market_view": market_view, "continuity": continuity})
        who = "Opus" if created_by == "opus_director" else "qwen (shadow)"
        tr = p["trade"]
        veh = (f"{tr['vehicle']} {tr.get('strike')} {tr.get('expiry')}"
               if tr["vehicle"] in ("call", "put") else "stock")
        cs = f", conf {p['confidence']:.0%}" if isinstance(p["confidence"], float) else ""
        action = (f"{who} director v2: {p['direction']} {p['ticker']} via {veh} "
                  f"({p['horizon']}{cs}). {_exec_mode_line().split(':')[0]}. {p['thesis']}")
        rid = PP._insert_director_rec(created_by=created_by, ticker=p["ticker"],
                                      direction=p["direction"], action_text=action,
                                      entry_ref_price=entry, extra=extra)
        rows.append({"rec_id": rid, **p, "entry": entry})
    return rows


def _apply_updates(updates: list, slate_id: str):
    """Record the director's reaffirm/invalidate/close notes on its open recs
    (annotation only — grading still runs to each row's declared horizon)."""
    if not updates:
        return
    conn = mason_db.get_connection()
    try:
        with conn:
            for u in updates:
                if not isinstance(u, dict) or not u.get("rec_id"):
                    continue
                r = conn.execute("SELECT extra_json FROM recommendations WHERE id=? AND kind='director'",
                                 (u["rec_id"],)).fetchone()
                if not r:
                    continue
                try:
                    ex = json.loads(r["extra_json"] or "{}")
                except Exception:
                    ex = {}
                _now = datetime.now().isoformat(timespec="seconds")
                ex.setdefault("director_updates", []).append(
                    {"at": _now, "slate_id": slate_id,
                     "action": str(u.get("action") or "")[:20], "note": str(u.get("note") or "")[:300]})
                _st = (ex.get("execution") or {}).get("state")
                if str(u.get("action") or "").lower() in ("close", "invalidate") \
                        and _st in ("placed", "approved_pending_fill", "queued"):
                    ex["execution"]["close_requested"] = {
                        "at": _now, "slate_id": slate_id,
                        "action": str(u.get("action") or "").lower()[:20],
                        "note": str(u.get("note") or "")[:300]}
                    if _st == "queued":            # not placed yet: simply never place it
                        ex["execution"]["state"] = "cancelled_by_director"
                conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                             (json.dumps(ex, default=str), u["rec_id"]))
    finally:
        conn.close()


def _exec_mode_line() -> str:
    """How these calls can be executed right now (autotrade flags, read per call)."""
    at = _cfg().get("autotrade") or {}
    if at.get("auto_place") and at.get("tiers_enabled"):
        floor = ((at.get("tiers") or {}).get("learning") or {}).get("min_p", 0.53)
        return (f"AUTO-PLACE ON (paper): every long/short call with calibrated probability ≥ {floor} "
                "is placed automatically (learning-size until results earn more), within the risk limits.")
    if at.get("auto_place"):
        return (f"AUTO-PLACE ON (paper): calls with confidence ≥ {at.get('min_confidence_auto')} "
                "are placed automatically within the risk limits.")
    if at.get("approve_enabled"):
        return "Approve-to-place (paper): nothing is placed until you approve a call."
    return "Observe-only — nothing staged or placed."


def _notify(notifier, *, mode, kind, slate_id, rows, brief, cost, market_view, citations,
            error=None, qwen_rows=None, tool_errors=None, followups=None, followups_due=0,
            continuity="", lessons=None, watch_changes=None, focus=None):
    heads = ", ".join(f"{r['ticker']} {r['direction']}" for r in rows) or "no calls"
    tag = " [MANUAL TEST]" if str(kind).startswith("manual") else ""
    title = (f"Director {'event' if mode == 'event' else kind}{tag} (Opus 5.5): {heads}")[:200]
    lines = [f"{_exec_mode_line()} Cost ${cost:.3f}.", ""]
    if focus:
        title = (f"Director decision asked for by a quick check on {focus.get('ticker')} (Opus 5.5): {heads}")[:200]
        lines = [f"Asked for at {focus.get('at') or '?'} Eastern by a quick check. Its reason: {focus.get('reason') or ''}",
                 ""] + lines
    if continuity:
        lines += [f"**Continuity:** {continuity}", ""]
    if market_view:
        lines += [f"**Market view:** {market_view}", ""]
    for r in rows:
        tr = r["trade"]
        veh = (f"{tr['vehicle'].upper()} {tr.get('strike')} exp {tr.get('expiry')}"
               if tr["vehicle"] in ("call", "put") else "STOCK")
        cs = f"{r['confidence']:.0%}" if isinstance(r["confidence"], float) else "n/a"
        unit = "premium" if tr["vehicle"] in ("call", "put") else "px"
        ent = f"{r['entry']:.2f}" if isinstance(r.get("entry"), (int, float)) else "—"
        lvl = (f" · entry {unit} {tr.get('entry_limit')} · stop {unit} {tr.get('stop')} · target {unit} {tr.get('target')}"
               if r["direction"] in ("long", "short") else "")
        lines.append(f"- **{r['direction'].upper()} {r['ticker']}** · {veh} · {r['horizon']} · conf {cs}"
                     f" · underlying ref {ent}{lvl}"
                     + (f" · ⚠ {'; '.join(r['validation_notes'])}" if r.get("validation_notes") else ""))
    if qwen_rows is not None:
        lines += ["", "qwen shadow: " + (", ".join(f"{q['ticker']} {q['direction']}" for q in qwen_rows) or "no calls")]
    if error:
        lines += ["", f"⚠ {error}"]
    if followups:
        lines += ["", "**Follow-ups scheduled (reminder set; re-injected on the day):** "
                  + "; ".join(f"{f['due_date']} {f['ticker'] or 'macro'}: {f['action']}" for f in followups)]
    if followups_due:
        lines += ["", f"Addressed {followups_due} due follow-up(s) from earlier runs."]
    if lessons is not None:
        lines += ["", f"Lessons recorded: {len(lessons)}"
                  + ("".join(f"\n- {l['ticker']} ({l.get('outcome') or 'interim'}): {l['lesson']}"
                             for l in lessons) if lessons else "")]
    try:
        lines += _render_watch_changes(watch_changes)
    except Exception as e:
        lines += ["", f"⚠ watchlist changes not rendered: {e}"]
    if tool_errors:
        lines += ["", "⚠ web tool errors: " + ", ".join(f"{e['tool']}:{e['error_code']}" for e in tool_errors)]
    lines += ["", "---", "", brief or "(no brief)"]
    if citations:
        lines += ["", "Sources: " + " · ".join(c["url"] for c in citations[:12])]
    body = "\n".join(lines)[:24000]
    try:
        notifier(type="recommendation", severity="info",
                 ticker=(rows[0]["ticker"] if rows else None), title=title, body=body,
                 ref_table="recommendations", ref_id=(rows[0]["rec_id"] if rows else None),
                 created_by="opus_director", dedup_key=f"director2:{slate_id}")
    except Exception as e:
        log.warning(f"[director] notification failed: {e}")


# ── prompt archive (Fable Step 1, audit L6): every live call can be replayed ────
# A live run used to keep only its OUTPUT; the prompt was written on dry runs only, so
# no decision could be re-run against a new prompt or model. Now every live run saves
# the full system text, the user/package text, the tool definitions, the model id, the
# effort and a prompt-version hash, gzip-compressed, in one file per slate OUTSIDE the
# repo and the database (so mason.db does not grow):
#     <dir>/<YYYY-MM>/<slate_id>.json.gz      dir default ~/.local/share/mason/director_prompts
# All text goes through mason_redact.redact_text first. Old files are removed after
# retention_days. Settings (read per call, on when absent):
#     analyst_desk.opus_director.v2.prompt_archive = {enabled, dir, retention_days}

_ARCHIVE_DEFAULTS = {"enabled": True, "dir": "~/.local/share/mason/director_prompts",
                     "retention_days": 365}
_ARCHIVE_SCHEMA = "director_prompt/1"
_PACKAGE_MARK = "<<PACKAGE>>"


def archive_cfg(cfg: dict | None = None) -> dict:
    over = v2_cfg(cfg).get("prompt_archive")
    return _merge(_ARCHIVE_DEFAULTS, over if isinstance(over, dict) else None)


def _archive_dir(cfg: dict | None = None) -> Path:
    return Path(os.path.expanduser(str(archive_cfg(cfg).get("dir") or _ARCHIVE_DEFAULTS["dir"])))


def _tool_defs(v2_call: dict) -> list:
    """The tool definitions a run sends, rebuilt exactly the way
    mason_llm.complete_research builds them from the same inputs (web tools from
    v2["web"], then the memory client tools). Keep the two in step."""
    tools = []
    web = v2_call.get("web") or {}
    if web.get("enabled"):
        dom = {}
        if web.get("allowed_domains"):
            dom["allowed_domains"] = list(web["allowed_domains"])
        elif web.get("blocked_domains"):
            dom["blocked_domains"] = list(web["blocked_domains"])
        if int(web.get("max_searches", 0) or 0) > 0:
            tools.append({"type": "web_search_20260209", "name": "web_search",
                          "max_uses": int(web["max_searches"]), **dom})
        if int(web.get("max_fetches", 0) or 0) > 0:
            tools.append({"type": "web_fetch_20260209", "name": "web_fetch",
                          "max_uses": int(web["max_fetches"]), **dom})
    if v2_call.get("search_tools"):
        try:
            import mason_memory_search as MS
            tools += list(MS.director_tools() or [])
        except Exception as e:
            log.info(f"[director] prompt archive: memory tool schemas unavailable: {e}")
    return tools


def prompt_hash(system: str, user_template: str, tools: list) -> str:
    """Version of the PROMPT (not of the day's data): the system text, the user
    instruction wrapper with the package cut out, and the tool definitions. It changes
    only when someone edits the prompt or the tools."""
    blob = json.dumps({"system": system, "user_template": user_template, "tools": tools},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _prune_archive(root: Path, retention_days: float) -> int:
    if not retention_days or retention_days <= 0:
        return 0
    cutoff, n = time.time() - float(retention_days) * 86400, 0
    for f in root.glob("*/*.json.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    for d in root.iterdir():
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return n


def save_slate_prompt(slate_id: str, *, system: str, user: str, tools: list, model, effort,
                      mode: str, kind: str, lean: bool = False, user_template: str = "",
                      extra: dict | None = None, cfg: dict | None = None) -> dict:
    """Save one live run's full prompt for replay. Returns {"ok", "prompt_hash",
    "package_hash", "path", "bytes"} — or {"ok": False, "prompt_hash", "skipped" | "error"}.
    Never raises: a failed save must never stop a director run."""
    out = {"ok": False, "prompt_hash": None}
    try:
        out["prompt_hash"] = prompt_hash(system, user_template, tools)
        out["package_hash"] = hashlib.sha256((user or "").encode("utf-8")).hexdigest()[:16]
        ac = archive_cfg(cfg)
        if not ac.get("enabled", True):
            return dict(out, skipped="prompt archive is switched off")
        sid = re.sub(r"[^0-9A-Za-z_-]", "", str(slate_id or ""))
        if not sid:
            return dict(out, error="no slate id")
        import mason_redact
        tools_json = json.dumps(tools, default=str)
        clean = {"system": mason_redact.redact_text(system), "user": mason_redact.redact_text(user),
                 "user_template": mason_redact.redact_text(user_template),
                 "tools_json": mason_redact.redact_text(tools_json)}
        raw = {"system": system, "user": user, "user_template": user_template, "tools_json": tools_json}
        rec = {"schema": _ARCHIVE_SCHEMA, "slate_id": sid,
               "saved_at": datetime.now().isoformat(timespec="seconds"),
               "mode": mode, "kind": kind, "lean": bool(lean), "model": model, "effort": effort,
               "prompt_hash": out["prompt_hash"], "package_hash": out["package_hash"],
               "redacted": [k for k in clean if clean[k] != raw[k]],
               **clean, **(extra or {})}
        root = _archive_dir(cfg)
        day = root / datetime.now().strftime("%Y-%m")
        day.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = day / f"{sid}.json.gz"
        tmp = day / f".{sid}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(rec, f, default=str)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        out.update({"ok": True, "path": str(path), "bytes": path.stat().st_size})
        try:
            _prune_archive(root, float(ac.get("retention_days") or 0))
        except Exception as e:
            log.info(f"[director] prompt archive prune failed: {e}")
    except Exception as e:
        out["error"] = str(e)
        log.warning(f"[director] prompt not archived for slate {slate_id}: {e}")
    return out


def load_slate_prompt(slate_id: str, cfg: dict | None = None) -> dict | None:
    """The archived prompt of one live director run, for a replay tool:
    {slate_id, system, user, user_template, tools (list), model, effort, mode, kind,
    lean, prompt_hash, package_hash, saved_at, path, ...}. None when nothing was saved
    for that slate (dry runs, runs before this was installed, files past retention)."""
    sid = re.sub(r"[^0-9A-Za-z_-]", "", str(slate_id or ""))
    if not sid:
        return None
    try:
        files = sorted(_archive_dir(cfg).glob(f"*/{sid}.json.gz"), reverse=True)
    except Exception:
        return None
    for p in files:
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                rec = json.load(f)
            try:
                rec["tools"] = json.loads(rec.get("tools_json") or "[]")
            except Exception:
                rec["tools"] = None           # redaction changed the JSON; tools_json still holds the text
            rec["path"] = str(p)
            return rec
        except Exception as e:
            log.info(f"[director] prompt archive unreadable {p}: {e}")
    return None


# ── the calls ───────────────────────────────────────────────────────────────────

def _web_for(mode, v2):
    web = dict(v2.get("web") or {})
    on = web.get("enabled") and v2.get("web_anchor" if mode == "anchor" else "web_event", True)
    return web if on else {}


def _search_tools_on() -> bool:
    try:
        import mason_memory_search as MS
        return bool(MS.enabled() and MS.director_tools_enabled())
    except Exception:
        return False


def _search_cfg() -> dict:
    return ((_cfg().get("memory") or {}).get("search") or {})


def _research_call(system, user, v2):
    """v2["web"] arrives already resolved per mode by _run (see _web_for).
    v2["search_tools"] (set by _run) attaches the whole-DB memory tools
    (search_memory + read-only query_db) as client tools Opus can call mid-run."""
    import mason_llm
    web = v2.get("web") or {}
    kw = {}
    if v2.get("search_tools"):
        import mason_memory_search as MS
        kw = {"client_tools": MS.director_tools(), "tool_handler": MS.handle_director_tool,
              "max_tool_calls": int(_search_cfg().get("max_tool_calls", 4) or 4)}
    return mason_llm.complete_research(
        system, user, model=v2["model"], effort=v2["effort"],
        max_tokens=int(v2["max_output_tokens"]),
        web=(web if web.get("enabled") else None),
        **({"deadline_s": float(v2["deadline_s"])} if v2.get("deadline_s") else {}), **kw)


def _qwen_call(system, user):
    import mason_predictor as MP
    return MP._call_model(system, user, "local")


def _universe() -> set:
    try:
        import mason_triage as TR
        return set((TR.broad_universe() or {}).keys())
    except Exception:
        return set()


def _run(mode: str, kind: str, *, package=None, catalysts=None, research_fn=None,
         qwen_fn=None, notifier=None, price_fn=None, reserve_usd=0.0, cfg=None,
         dry_run: bool = False, consume: bool = True, preview_path=None,
         followups: list | None = None, archive_prompt: bool | None = None,
         focus: dict | None = None) -> dict:
    """archive_prompt: None (default) saves the prompt of every run that makes the REAL
    model call (so a test with a fake research_fn never writes into the archive);
    True / False force it.
    focus: set by run_escalation. The run is about one stock a desk check raised: short
    output format, the live book, one dossier, no watchlist tape, no web or memory tools
    unless catalyst.event_call.escalation says so."""
    import mason_desk_pipeline as PP
    v2 = v2_cfg(cfg)
    if archive_prompt is None:
        archive_prompt = research_fn is None or research_fn is _research_call
    notifier = notifier or mason_notifications.add_notification
    price_fn = price_fn or PP._default_last_price
    research_fn = research_fn or _research_call
    budget = int(v2["anchor_input_token_budget"] if mode == "anchor" else v2["event_input_token_budget"])
    maxd = int(v2["max_dossiers_anchor"] if mode == "anchor" else v2["max_dossiers_event"])
    maxp = int(v2["max_predictions_anchor"] if mode == "anchor" else v2["max_predictions_event"])
    lean = mode == "anchor" and _is_lean(kind, v2)
    web_cfg = _web_for(mode, v2)
    exp_out = {"expected_output_tokens": int(v2.get("lean_expected_output_tokens", 7000))} if lean else {}
    if focus:
        es = escalation_cfg(cfg)
        budget, maxd, maxp, lean = int(es["input_token_budget"]), 1, int(es["max_predictions"]), True
        web_cfg = web_cfg if es.get("web") else {}
        # (review fix R4) the budget test uses the most this call can cost (max_output_tokens), not the
        # expected 5,000 tokens: what it really spends must never eat the money kept for a scheduled call
        exp_out = {"expected_output_tokens": max(int(es["expected_output_tokens"]), int(v2["max_output_tokens"]))}
    text, meta = build_package_text(mode=mode, package=package, catalysts=catalysts,
                                    budget_tokens=budget, max_dossiers=maxd, followups=followups,
                                    lean=lean, focus=focus)
    mem_tools = None                              # None = as before: follow the memory-tools switch
    if focus and not escalation_cfg(cfg).get("memory_tools"):
        mem_tools = False                         # a focused run is sent without them, and is not told about them
    user = _user_prompt(mode, kind, text, maxp, web=bool(web_cfg), focus=focus, memory_tools=mem_tools)
    system = _system(cfg, lean=lean, watch=not focus)
    est = estimate_cost(system, user, dict(v2, web=web_cfg, **exp_out))
    ok, why = budget_ok(est, v2, reserve_usd=reserve_usd, cfg=cfg)
    slate_id = uuid.uuid4().hex[:12]
    base = {"mode": mode, "kind": kind, "slate_id": slate_id, "est_usd": round(est, 4),
            "input_tokens_est": _tok(system) + _tok(user), "sections": meta["sections"], "lean": lean,
            "memory_tokens": meta.get("memory_tokens") or {},
            "memory_tokens_total": sum((meta.get("memory_tokens") or {}).values())}
    if dry_run:                        # free: package + estimate only, NO model call
        if preview_path:
            try:
                Path(preview_path).parent.mkdir(parents=True, exist_ok=True)
                Path(preview_path).write_text(
                    f"# Director v2 {mode} DRY RUN ({kind}) — {datetime.now():%Y-%m-%d %H:%M} ET — NO Opus call\n"
                    f"# input ≈ {base['input_tokens_est']} tokens · est ${est:.3f} · budget: {why}\n"
                    f"# sections: {json.dumps(meta['sections'])}\n"
                    f"# memory: {base['memory_tokens_total']} tokens {json.dumps(base['memory_tokens'])}\n\n"
                    f"================ SYSTEM ================\n{system}\n\n"
                    f"================ USER ================\n{user}", encoding="utf-8")
                base["preview"] = str(preview_path)
            except Exception as e:
                base["preview_error"] = str(e)
        return {**base, "dry_run": True, "budget_ok": ok, "budget_note": why,
                "dossiers": meta.get("dossier_subjects"), "catalysts": meta.get("catalyst_ids"),
                "spend": spend()}
    if not ok:
        log.warning(f"[director] {mode} skipped — {why}")
        try:
            mason_activity.log_activity("opus_director_budget_skip", f"Director {mode} skipped: {why}",
                                        actor="system", category="plumbing", trigger="autonomous",
                                        extra=base)
        except Exception:
            pass
        return {**base, "skipped": why}

    started = time.time()
    v2_call = dict(v2, web=web_cfg, search_tools=_search_tools_on() and mem_tools is not False)
    if focus:      # asked for by a desk check: runs on the worker the exit manager shares, so bound it
        try:
            v2_call["deadline_s"] = max(60.0, float(escalation_cfg(cfg).get("timeout_s", 240) or 240))
        except Exception:
            v2_call["deadline_s"] = 240.0
    # the full prompt goes to the archive BEFORE the paid call, so even a run whose
    # output is lost or unparseable can be replayed (audit L6)
    phash = None
    try:
        tools = _tool_defs(v2_call)
        tmpl = _user_prompt(mode, kind, _PACKAGE_MARK, maxp, web=bool(web_cfg), focus=focus, memory_tools=mem_tools)
        if archive_prompt:
            arch = save_slate_prompt(
                slate_id, system=system, user=user, tools=tools, model=v2.get("model"),
                effort=v2.get("effort"), mode=mode, kind=kind, lean=lean, user_template=tmpl, cfg=cfg,
                extra={"max_tokens": int(v2["max_output_tokens"]), "max_predictions": maxp,
                       "asked_by_check": (focus or {}).get("id"),
                       "web": v2_call.get("web") or {}, "search_tools": bool(v2_call.get("search_tools")),
                       "sections": meta["sections"], "input_tokens_est": base["input_tokens_est"]})
            phash = arch.get("prompt_hash")
            base["prompt_archive"] = arch.get("path") or arch.get("skipped") or f"not saved: {arch.get('error')}"
        else:
            phash = prompt_hash(system, tmpl, tools)
    except Exception as e:
        log.warning(f"[director] prompt archive step failed: {e}")
    base["prompt_hash"] = phash
    try:
        res = research_fn(system, user, v2_call)
    except Exception as e:
        log.warning(f"[director] research call failed: {e}")
        return {**base, "error": f"model call failed: {e}"}
    cost = float(res.get("cost_usd") or 0.0)
    errors = []
    # 1) the paid output goes to disk FIRST — nothing after this can lose it
    raw_path = PREVIEW_DIR / f"director_output_{mode}_{datetime.now():%Y%m%d-%H%M%S}_{slate_id}.md"
    try:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(f"<!-- {kind} · {res.get('model')} · ${cost:.4f} · usage {json.dumps(res.get('usage'))} "
                            f"· web {res.get('web_search_requests')} · tool_errors {json.dumps(res.get('tool_errors'))} -->\n\n"
                            + (res.get("text") or ""), encoding="utf-8")
    except Exception as e:
        errors.append(f"raw save: {e}")
    # 2) the ledger is a file append (can't hit a DB lock)
    try:
        record_spend(cost, event=(mode == "event"), kind=kind, slate_id=slate_id,
                     note=(f"check:{focus.get('id')}" if focus else ""))
    except Exception as e:
        errors.append(f"spend ledger: {e}")
    brief, obj, perr = parse_output(res.get("text") or "")
    continuity = " ".join(str((obj or {}).get("continuity") or "").split())[:300]
    preds = validate_predictions(obj or {}, meta, max_preds=maxp, universe=_universe()) if obj else []
    horizons = [1, 5, 30, 90]
    try:
        od = ((cfg or _cfg()).get("analyst_desk") or {}).get("opus_director") or {}
        horizons = od.get("horizons_days") or horizons
    except Exception:
        pass
    rows = []
    for attempt in range(3):                     # DB writes retry through a transient lock
        try:
            rows = _persist(preds, created_by="opus_director", tier="opus", model_id=res.get("model"),
                            kind=kind, slate_id=slate_id, brief=brief, cost=cost,
                            updates=(obj or {}).get("updates") or [], citations=res.get("citations") or [],
                            price_fn=price_fn, horizons=horizons,
                            market_view=str((obj or {}).get("market_view") or "")[:400],
                            continuity=continuity, prompt_hash=phash, origin=focus)
            break
        except Exception as e:
            if attempt == 2:
                errors.append(f"persist recs: {e}")
                log.warning(f"[director] could not persist predictions ({e}); output saved at {raw_path}")
            else:
                time.sleep(5 * (attempt + 1))
    exec_results = []
    if rows:                                   # approve-to-place: no-op unless autotrade.auto_place
        try:
            import mason_autotrade
            exec_results = mason_autotrade.maybe_autoplace(
                [r["rec_id"] for r in rows if r.get("direction") in ("long", "short")]) or []
        except Exception as e:
            errors.append(f"autoplace: {e}")
    try:
        _apply_updates((obj or {}).get("updates") or [], slate_id)
    except Exception as e:
        errors.append(f"apply updates: {e}")
    book_set = []
    if lean and obj is not None and (obj or {}).get("book"):   # sells of stock positions Nico placed
        if focus and not escalation_cfg(cfg).get("book_actions"):
            # (review fix R3) a run a desk check asked for must not sell what Nico placed: the check itself
            # may only write him a note, and this run has a reduced input. Recorded, shown, not done.
            book_set = [{"symbol": str((b or {}).get("symbol") or "")[:12].upper(), "accepted": False,
                         "why": "not done: a decision asked for by a quick check does not sell positions Nico placed "
                                "(catalyst.event_call.escalation.book_actions)"}
                        for b in ((obj or {}).get("book") or []) if isinstance(b, dict)]
            if book_set:
                errors.append("the director wanted to sell " + ", ".join(b["symbol"] for b in book_set)
                              + " from Nico's positions; not done between the scheduled reviews")
        else:
            try:
                import mason_agent_loop as AL
                book_set = AL.apply_book_actions((obj or {}).get("book") or [], slate_id)
            except Exception as e:
                errors.append(f"book actions: {e}")
    # the director's deferred work → DB + reminder (its memory for next time)
    fu_set = []
    try:
        fu_set = add_followups((obj or {}).get("follow_ups") or [], slate_id=slate_id,
                               rec_ids={r["ticker"]: r["rec_id"] for r in rows},
                               allowed=set(meta.get("tickers") or set()) | _universe())
    except Exception as e:
        errors.append(f"follow-ups: {e}")
    # lessons on closed (or clearly broken open) calls → director_lessons (manual runs too:
    # they are real analysis). NEWLY CLOSED rows shown count as "asked" either way.
    lessons_set = []
    if obj is not None:
        try:
            lessons_set = record_lessons((obj or {}).get("lessons") or [], closed=meta.get("closed") or {},
                                         open_ids=meta.get("open_rec_ids") or [], slate_id=slate_id)
        except Exception as e:
            errors.append(f"lessons: {e}")
        try:
            _bump_lesson_asks(list((meta.get("closed") or {}).keys()))
        except Exception as e:
            errors.append(f"lesson asks: {e}")
    theses_set = []
    if obj is not None and (obj or {}).get("theses"):
        try:
            import mason_theses as TH
            if TH.enabled():
                r = TH.apply_director_theses((obj or {}).get("theses") or [], slate_id=slate_id,
                                             allowed_tickers=set(meta.get("tickers") or set()) | _universe())
                theses_set = (r or {}).get("created") if isinstance(r, dict) else (r or [])
        except Exception as e:
            errors.append(f"theses: {e}")
    if watch_enabled(cfg) and meta.get("closed"):
        _store_trade_stories(meta.get("closed") or {})       # knowledge base (R12)
    watch_changes = None
    if obj is not None and watch_enabled(cfg) and (obj or {}).get("watchlist"):
        try:
            import mason_watchlist as WL
            watch_changes = WL.apply_director_watchlist(
                (obj or {}).get("watchlist") or [], slate_id=slate_id,
                allowed_tickers=set(meta.get("tickers") or set()) | _universe())
        except Exception as e:
            errors.append(f"watchlist: {e}")
    if consume and obj is not None:
        try:
            mark_followups_done(meta.get("followup_ids") or [], slate_id)
        except Exception as e:
            errors.append(f"follow-ups done: {e}")
        if meta.get("check_note_ids"):         # the desk-check notes this run was shown are not shown again
            try:
                import mason_desk_check as DC
                DC.mark_notes_shown(meta["check_note_ids"], slate_id)
            except Exception as e:
                errors.append(f"desk-check notes: {e}")

    qrows = None
    if mode == "anchor" and v2.get("ab_qwen"):
        try:
            qtext, qmeta = build_package_text(mode=mode, package=package, catalysts=catalysts,
                                              budget_tokens=int(v2["qwen_input_token_budget"]),
                                              max_dossiers=min(2, maxd), memory=False)
            qraw = (qwen_fn or _qwen_call)(_SYSTEM, _user_prompt(mode, kind, qtext, maxp))
            _qb, qobj, _qe = parse_output(qraw or "")
            qpreds = validate_predictions(qobj or {}, qmeta, max_preds=maxp,
                                          universe=_universe()) if qobj else []
            qrows = _persist(qpreds, created_by="qwen_director", tier="local", model_id="qwen3.5:4b",
                             kind=kind, slate_id=slate_id, brief=_qb, cost=0.0, updates=[],
                             citations=[], price_fn=price_fn, horizons=horizons)
        except Exception as e:
            log.info(f"[director] qwen shadow failed: {e}")
            qrows = []

    for cid in (meta.get("catalyst_ids") or []) if consume else []:
        if str(cid).startswith("watch:"):
            try:
                import mason_watch_signals as SG
                SG.mark_consumed(str(cid)[len("watch:"):], slate_id)
            except Exception as e:
                errors.append(f"watch consume: {e}")
            continue
        try:
            import mason_triage as TR
            TR.mark_catalyst(cid, status="consumed", consumed_by=slate_id)
        except Exception:
            break
    if errors:
        perr = "; ".join(filter(None, [perr] + errors))
    _notify(notifier, mode=mode, kind=kind, slate_id=slate_id, rows=rows, brief=brief, cost=cost,
            market_view=(obj or {}).get("market_view"), citations=res.get("citations") or [],
            error=perr, qwen_rows=qrows, tool_errors=res.get("tool_errors"), followups=fu_set,
            followups_due=len(meta.get("followup_ids") or []), continuity=continuity,
            lessons=lessons_set, watch_changes=watch_changes, focus=focus)
    info = {**base, "cost_usd": cost, "usage": res.get("usage"),
            "web_searches": res.get("web_search_requests"), "web_fetches": res.get("web_fetch_requests"),
            "tool_errors": res.get("tool_errors"), "followups_set": fu_set,
            "memory_tool_calls": res.get("tool_calls"),
            "autoplace": exec_results, "book_actions": book_set,
            "followups_due": meta.get("followup_ids"), "continuity": continuity,
            "lessons_recorded": lessons_set, "theses_opened": theses_set, "watchlist": watch_changes, "newly_closed": list((meta.get("closed") or {}).keys()),
            "prev_slate_id": meta.get("prev_slate_id"), "asked_by_check": (focus or {}).get("id"),
            "n_predictions": len(rows), "parse_error": perr, "output_file": str(raw_path),
            "errors": errors,
            "seconds": round(time.time() - started, 1), "rec_ids": [r["rec_id"] for r in rows]}
    try:
        mason_activity.log_activity("opus_director_v2", f"Opus 5.5 director {mode} ({kind}): "
                                    f"{len(rows)} call(s), {len(lessons_set)} lesson(s), ${cost:.3f}", actor="opus",
                                    category="analyst", trigger="autonomous",
                                    ticker=(rows[0]["ticker"] if rows else None),
                                    ref_table="recommendations",
                                    ref_id=(rows[0]["rec_id"] if rows else None), extra=info)
    except Exception:
        pass
    return info


def run_anchor(package: dict, *, cfg=None, **kw) -> dict | None:
    """Anchor director v2. Called from mason_desk_pipeline._maybe_run_opus_director
    when v2 is enabled. Skips non-trading days (Monday 07:00 folds in the weekend)."""
    v2 = v2_cfg(cfg)
    et = _now_et()
    kind = package.get("kind") or "anchor"
    if v2.get("trading_days_only", True) and not is_trading_day(et):
        log.info(f"[director] {kind} skipped — not a trading day")
        return {"skipped": "not a trading day", "kind": kind}
    since = _state_get("opus_v2_last_director_at")
    try:
        since_epoch = float(since) if since else time.time() - 24 * 3600
    except ValueError:
        since_epoch = time.time() - 24 * 3600
    cats = _catalysts_since(since_epoch, limit=12)
    res = _run("anchor", kind, package=package, catalysts=cats, cfg=cfg,
               followups=due_followups(et.strftime("%Y-%m-%d")), **kw)
    if not res.get("skipped") and not res.get("error"):
        try:
            _state_set("opus_v2_last_director_at", str(time.time()))
        except Exception as e:
            log.warning(f"[director] watermark write failed (next anchor re-reads a wider window): {e}")
    return res


# ── manual / forced runs (testing) ──────────────────────────────────────────────

_run_lock = __import__("threading").Lock()
PREVIEW_DIR = BASE / "docs" / "catalyst_desk"


def _latest_desk_package() -> dict:
    conn = mason_db.get_connection()
    try:
        r = conn.execute("SELECT details_json FROM qwen_news_analysis WHERE "
                         "analysis_type='director_package' AND COALESCE(record_deleted,0)=0 "
                         "ORDER BY created_date DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    try:
        return json.loads(r["details_json"]) if r else {}
    except Exception:
        return {}


def run_now(mode: str = "anchor", *, catalyst_id: str | None = None, dry_run: bool = False,
            notifier=None, cfg=None, **kw) -> dict:
    """Force a director run for testing — bypasses the trading-day / anchor-time /
    event gates and the v2.enabled flag, but NOT the $ caps. Uses the latest desk
    package + catalysts from the last 24h (anchor) or one catalyst (event). Does
    not consume catalysts or move the anchor watermark, so the next scheduled run
    is unaffected. dry_run=True builds the package + estimate and writes a preview
    to docs/catalyst_desk/ with NO model call (free)."""
    mode = "event" if mode == "event" else "anchor"
    if not dry_run:
        try:
            import mason_activity as _ACT
            with _ACT.run(f"Manual director {mode} run", actor="opus", trigger="chat",
                          kind="director_manual"):
                return _run_now_inner(mode, catalyst_id=catalyst_id, dry_run=dry_run,
                                      notifier=notifier, cfg=cfg, **kw)
        except ImportError:
            pass
    return _run_now_inner(mode, catalyst_id=catalyst_id, dry_run=dry_run, notifier=notifier, cfg=cfg, **kw)


def _run_now_inner(mode, *, catalyst_id=None, dry_run=False, notifier=None, cfg=None, **kw) -> dict:
    if not _run_lock.acquire(blocking=False):
        return {"error": "a manual director run is already in progress"}
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        preview = PREVIEW_DIR / f"{'dryrun' if dry_run else 'manual'}_{mode}_{stamp}.txt"
        if mode == "anchor":
            pkg = _latest_desk_package()
            cats = _catalysts_since(time.time() - 24 * 3600, limit=12)
            return _run("anchor", f"manual_{pkg.get('kind') or 'anchor'}", package=pkg,
                        catalysts=cats, cfg=cfg, dry_run=dry_run, consume=False,
                        preview_path=preview, notifier=notifier,
                        followups=due_followups(), **kw)
        cats = _catalysts_since(time.time() - 24 * 3600, limit=20)
        if catalyst_id:
            cats = [c for c in cats if c.get("id") == catalyst_id]
            if not cats:
                return {"error": f"catalyst {catalyst_id} not found in the last 24h"}
        cats = cats[:1]
        if not cats:
            return {"error": "no catalysts in the last 24h"}
        return _run("event", f"manual_event:{cats[0].get('id')}", catalysts=cats, cfg=cfg,
                    dry_run=dry_run, consume=False, preview_path=preview,
                    notifier=notifier, **kw)
    finally:
        _run_lock.release()


def thesis_matched_catalysts(hours: float = 24.0) -> set:
    """Catalyst ids whose news CONFIRMED or BROKE an open thesis in the last `hours`
    (mason_theses match_log → news_events.catalyst_id). Never raises."""
    out = set()
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = mason_db.get_connection()
        try:
            rows = conn.execute("SELECT match_log FROM theses WHERE record_deleted=0 "
                                "AND last_match_date >= ?", (cutoff[:19],)).fetchall()
            nids = []
            for r in rows:
                for m in (json.loads(r["match_log"] or "[]") or []):
                    if isinstance(m, dict) and m.get("verdict") in ("confirm", "break") \
                            and str(m.get("date") or "") >= cutoff[:10]:
                        nids.append(m.get("news_id"))
            nids = [n for n in nids if n]
            if nids:
                q = ",".join("?" * len(nids))
                out = {r["catalyst_id"] for r in conn.execute(
                    f"SELECT catalyst_id FROM news_events WHERE id IN ({q}) AND catalyst_id IS NOT NULL",
                    nids).fetchall()}
        finally:
            conn.close()
    except Exception:
        pass
    return out


def event_eligible(c: dict, *, et: datetime | None = None, cfg=None) -> tuple[bool, str]:
    """Deterministic gate for an EVENT call on one catalyst."""
    v2, ev, cat = v2_cfg(cfg), event_cfg(cfg), catalyst_cfg(cfg)
    et = et or _now_et()
    if not (v2.get("enabled") and ev.get("enabled")):
        return False, "disabled"
    if v2.get("trading_days_only", True) and not is_trading_day(et):
        return False, "not a trading day"
    lo, hi = ev.get("window_et") or ["06:00", "17:00"]
    hm = et.strftime("%H:%M")
    if not (lo <= hm <= hi):
        return False, f"outside window {lo}-{hi}"
    if spend(et)["auto_event_calls_today"] >= int(ev.get("max_per_day", 1)):
        return False, "event cap reached"
    if minutes_to_next_anchor(et, cfg) < float(ev.get("min_minutes_to_next_anchor", 90)):
        return False, "next anchor soon — defer"
    if c.get("status") == "consumed":
        return False, "already consumed"
    if c.get("event_kind") == "watch_trigger":             # watchlist wake (design R8/D9)
        wk = _watch_cfg(cfg)
        if not (wk.get("enabled") and (wk.get("opus_wake") or {}).get("enabled")):
            return False, "watch wake disabled"
        thr_w = float((wk.get("opus_wake") or {}).get("min_score", 0.85))
        if float(c.get("score") or 0) < thr_w:
            return False, f"watch score {c.get('score')} < {thr_w}"
        try:
            if (time.time() - float(c.get("first_seen"))) / 3600.0 > 2.0:
                return False, "stale watch trigger"
        except (TypeError, ValueError):
            pass
        return True, "watch trigger"
    if c.get("id") and c.get("id") in thesis_matched_catalysts():
        return True, "confirms/breaks an open thesis"      # early hint just resolved: act on it
    thr = float((cat.get("triage") or {}).get("event_call_threshold", 0.85))
    if float(c.get("score") or 0) < thr:
        return False, f"score {c.get('score')} < {thr}"
    try:
        age_h = (time.time() - float(c.get("first_seen"))) / 3600.0
    except (TypeError, ValueError):
        age_h = None
    if age_h is not None and age_h > float(ev.get("max_story_age_hours", 12)):
        return False, f"stale story (broke {age_h:.0f}h ago)"
    tape = c.get("tape_json") or {}
    if isinstance(tape, str):
        try:
            tape = json.loads(tape)
        except Exception:
            tape = {}
    confirmed = ((_num(tape.get("rel_volume")) or 0) >= float(ev.get("min_rel_volume", 1.5))
                 or abs(_num(tape.get("vs_sector_pct")) or 0) >= float(ev.get("min_abs_vs_sector_pct", 2.0)))
    if c.get("primary_ticker") and not confirmed:
        return False, "tape not confirming"
    return True, "ok"


def escalation_cfg(cfg: dict | None = None) -> dict:
    return _merge(_EVENT_DEFAULTS["escalation"], event_cfg(cfg).get("escalation"))


def _hhmm_ok(s) -> bool:
    return isinstance(s, str) and bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s.strip()))


def escalation_gate(*, et: datetime | None = None, cfg=None) -> tuple[str, str]:
    """May a desk check have its own full decision right now?
      ("run", "ok")            yes
      ("handoff", reason)      not on its own: leave it for the next scheduled call
      ("no", reason)           the feature is off
    The reasons are plain words; mason_desk_check puts them in the inbox."""
    v2, es = v2_cfg(cfg), escalation_cfg(cfg)
    et = et or _now_et()
    if not (v2.get("enabled") and es.get("enabled")):
        return "no", "full decisions between the scheduled reviews are switched off (catalyst.event_call.escalation.enabled)"
    if v2.get("trading_days_only", True) and not is_trading_day(et):
        return "handoff", "today is not a trading day"
    win = es.get("window_et") or []
    lo, hi = (win + ["04:00", "20:00"])[:2] if len(win) < 2 else win[:2]
    if not (_hhmm_ok(lo) and _hhmm_ok(hi)):
        lo, hi = "04:00", "20:00"
    if not (lo <= et.strftime("%H:%M") <= hi):
        return "handoff", f"it is outside the hours for these decisions ({lo} to {hi} Eastern)"
    n = spend(et)["escalations_today"]
    if n >= int(es.get("max_per_day", 3)):
        return "handoff", f"{n} such decisions have already been made today, the most allowed in one day"
    mins = minutes_to_next_anchor(et, cfg)
    gap = float(es.get("min_minutes_to_scheduled_call", 20))
    if mins < gap:
        return "handoff", f"the next scheduled review is {int(mins)} minutes away"
    if scheduled_calls_pending(et, cfg):       # (review fix R4) due, but still queued or running
        return "handoff", "a scheduled review is due now and has not finished yet"
    return "run", "ok"


def run_escalation(check: dict, *, cfg=None, et: datetime | None = None, **kw) -> dict:
    """A full Opus decision that a desk check asked for, through the event path: same
    director, same validation, same auto-placing (queued for the open when the market is
    closed), same ledger. check = {"id", "ticker", "trigger", "reason", "direction",
    "facts", "catalyst_id", "at"}. Returns _run's result, or {"skipped": reason,
    "handoff": bool} when it must not run now. Never raises."""
    try:
        et = et or _now_et()
        gate, why = escalation_gate(et=et, cfg=cfg)
        if gate != "run":
            return {"skipped": why, "handoff": gate == "handoff"}
        cats = []
        if check.get("catalyst_id"):
            cats = [c for c in _catalysts_since(time.time() - 48 * 3600, limit=40)
                    if c.get("id") == check["catalyst_id"]][:1]
        res = _run("event", f"escalation:{check.get('id')}", catalysts=cats,
                   reserve_usd=scheduled_reserve_usd(et, cfg), cfg=cfg, focus=dict(check), **kw)
        if res.get("skipped"):
            try:
                mason_activity.log_activity("opus_escalation_skip", f"Decision asked for by a desk check not run: "
                                            f"{res['skipped']}", actor="system", category="plumbing",
                                            trigger="autonomous", ticker=check.get("ticker"))
            except Exception:
                pass
        return res
    except Exception as e:
        log.warning(f"[director] escalation failed: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def maybe_run_event(*, cfg=None, **kw) -> dict | None:
    """Scheduler hook: at most one event call per tick, highest score first."""
    if not enabled(cfg) or not event_cfg(cfg).get("enabled"):
        return None
    et = _now_et()
    since = time.time() - 6 * 3600
    cands = list(_catalysts_since(since, limit=8))
    wk = _watch_cfg(cfg)
    if wk.get("enabled") and (wk.get("opus_wake") or {}).get("enabled"):
        try:                                   # watch triggers compete for the SAME 1/day slot (D9)
            import mason_watch_signals as SG
            cands += list(SG.event_candidates(since_epoch=time.time() - 2 * 3600, limit=5) or [])
        except Exception as e:
            log.info(f"[director] watch candidates unavailable: {e}")
        cands.sort(key=lambda x: -float(x.get("score") or 0))
    for c in cands:
        ok, why = event_eligible(c, et=et, cfg=cfg)
        if not ok:
            continue
        v2 = v2_cfg(cfg)
        # keep enough budget for the anchors still to come today
        per_anchor = estimate_cost(_system(cfg), "x" * int(v2["anchor_input_token_budget"]) * 4,
                                   dict(v2, web=_web_for("anchor", v2)))
        reserve = per_anchor * anchors_remaining_today(et, cfg)
        res = _run("event", f"event:{c.get('id')}", catalysts=[c], reserve_usd=reserve, cfg=cfg, **kw)
        return res
    return None


def prebuild_dossiers(*, cfg=None, max_builds: int = 2) -> int:
    """Scheduler hook: warm dossiers for fresh catalysts ≥ dossier_threshold so the
    anchor/event call doesn't pay build latency. Free (no LLM). Bounded per tick."""
    cat = catalyst_cfg(cfg)
    if not (cat.get("dossier") or {}).get("enabled"):
        return 0
    thr = float((cat.get("triage") or {}).get("dossier_threshold", 0.6))
    n = 0
    for f in due_followups():                     # follow-ups due today get a warm dossier
        if n >= max_builds or not f.get("ticker"):
            continue
        if _dossier_for(f["ticker"]):
            n += 1
    for c in _catalysts_since(time.time() - 12 * 3600, limit=10):
        if n >= max_builds:
            break
        t = c.get("primary_ticker")
        if not t or float(c.get("score") or 0) < thr or c.get("status") in ("dossier_ready", "consumed"):
            continue
        d = _dossier_for(t)
        if d and d.get("id"):
            try:
                import mason_triage as TR
                TR.mark_catalyst(c["id"], status="dossier_ready", dossier_ids=[d["id"]])
            except Exception:
                pass
            n += 1
    return n


def status(cfg=None) -> dict:
    """For /api/opus_director — v2 flags + spend."""
    v2 = v2_cfg(cfg)
    s = spend()
    return {"v2_enabled": bool(v2.get("enabled")), "model": v2.get("model"),
            "effort": v2.get("effort"), "daily_usd_cap": v2.get("daily_usd_cap"),
            "monthly_usd_cap": v2.get("monthly_usd_cap"),
            "event_calls_enabled": bool(event_cfg(cfg).get("enabled")),
            "escalations_enabled": bool(escalation_cfg(cfg).get("enabled")),
            "overall_daily_usd_cap": overall_daily_cap(cfg),
            "followups_upcoming": [{k: f.get(k) for k in ("id", "ticker", "due_date", "action")}
                                   for f in upcoming_followups()],
            "memory": memory_summary(), **s}
