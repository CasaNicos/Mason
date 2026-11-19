"""
mason_desk_pipeline.py — Analyst Desk Phase 3: the desk's red-team + call.

ANALYST_DESK_PHASE3_PROMPT.md. On *breaking* news (materiality ≥ 0.8 AND ticker ∈
holdings ∪ watchlist) the Phase-2 worker runs this pipeline, all local qwen:

    news_event + qwen_news_analysis
        → synthetic scanner candidate (news→thesis adapter)
        → mason_predictor.run_predictor(model_tier="local")     [qwen thesis/1.0]
        → mason_debate.run_debate(model_tier="local")           [qwen Bull/Bear]
        → mason_arbiter.run_arbiter(model_tier="local")         [qwen verdict + HARD gates]
        → a coarse recommendation (recommendations table) + a 'recommendation' inbox notif

Nothing is suppressed: a REJECT (regime-gate / correlation / inconclusive debate)
still surfaces as a `kind='watch'` recommendation carrying the reason. APPROVE /
REVISE carry a labelled, coarse, deterministic `order_preview` (built via
mason_order_builder.default_qty + a live limit) — a *qwen draft*, never placed.

Also here: dedup/cooldown/re-proposal ("AAPL is still…"), track-record grading of
the desk's own recommendations vs later price (±0.5% dead-band), and the Opus
director SEAM.

NO Opus at runtime. NO real placement. Equity-only. Every model call is
intent="chore" (local qwen), on the Phase-2 single-worker queue that yields to
chat and honors the min-interval + a per-hour/day rate limit. mason_debate.py /
mason_arbiter.py / mason_regime_gate.py / mason_execution.py / mason_order_builder.py
/ mason_learning.py are reused UNMODIFIED (their public functions only).

──────────────────────────────────────────────────────────────────────────────
OPUS DIRECTOR HANDOFF CONTRACT (Deliverable F — seam only, disabled in Phase 3)
──────────────────────────────────────────────────────────────────────────────
At the 07:00 / 13:00 ET anchors, build_desk_report() assembles a stable
`director_package/1.0` (assemble_director_package) and calls
_maybe_run_opus_director(package), which is a NO-OP returning None while
`analyst_desk.opus_director.enabled` is false (always, in Phase 3).

When Nico flips the flag later, _maybe_run_opus_director should:
  INPUT  (what Opus receives): the director_package dict —
    { schema_version:"director_package/1.0", kind, generated_at,
      report_text, contexts{news,sentiment,analytics,fundamentals,positioning},
      recent_analyses[], recommendations[], track_record{}, posture{} }
  CALL   : mason_llm.complete(intent="deep", ...) ONCE per anchor (the only Opus
           spend), returning {prediction, play, sentiment}.
  OUTPUT : route the chosen `play` through the DETERMINISTIC
           mason_order_builder → mason_execution path (never qwen/opus freehand),
           first as a pre-staged `confirm order …`, then — on Nico's word —
           auto-place (paper) with per-trade caps.
Flipping the flag + implementing that one function is the only remaining work;
everything upstream (package shape, persistence) is already produced here.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import mason_db
import mason_notifications
import mason_activity
import mason_analyst_desk as AD          # reuse config/state/time/gate helpers

log = logging.getLogger("mason.desk_pipeline")


# ── config helpers (reuse AD's) ─────────────────────────────────────────────────

def _cfg_get(desk, path, default):
    return AD._cfg_get(desk, path, default)


def _desk(cfg=None):
    return AD._desk_cfg(cfg)


# ── injectable defaults ─────────────────────────────────────────────────────────

def _default_last_price(ticker):
    try:
        import mason_news_service as NS
        return NS._last_price(ticker)
    except Exception:
        return None


def _default_holdings():
    try:
        import mason_news
        return mason_news.holdings_set()
    except Exception:
        return set()


def _default_watchlist():
    try:
        import mason_news
        return mason_news.watchlist_set()
    except Exception:
        return set()


# ── rate limiting via desk_state ────────────────────────────────────────────────

def _rate_ok(now_et: datetime, desk) -> tuple[bool, str]:
    max_h = int(_cfg_get(desk, ["debate", "max_per_hour"], 4) or 4)
    max_d = int(_cfg_get(desk, ["debate", "max_per_day"], 20) or 20)
    hkey = "debate_count:" + now_et.strftime("%Y-%m-%d-%H")
    dkey = "debate_count_day:" + now_et.strftime("%Y-%m-%d")
    h = int(AD.get_state(hkey, 0) or 0)
    d = int(AD.get_state(dkey, 0) or 0)
    if h >= max_h:
        return False, f"hourly debate cap reached ({h}/{max_h})"
    if d >= max_d:
        return False, f"daily debate cap reached ({d}/{max_d})"
    return True, ""


def _rate_incr(now_et: datetime):
    hkey = "debate_count:" + now_et.strftime("%Y-%m-%d-%H")
    dkey = "debate_count_day:" + now_et.strftime("%Y-%m-%d")
    AD.set_state(hkey, int(AD.get_state(hkey, 0) or 0) + 1)
    AD.set_state(dkey, int(AD.get_state(dkey, 0) or 0) + 1)


# ── news → synthetic scanner candidate ──────────────────────────────────────────

def _load_news_and_analysis(news_id: str, ticker: str) -> tuple[dict | None, dict | None]:
    conn = mason_db.get_connection()
    try:
        ev = conn.execute("SELECT * FROM news_events WHERE id = ?", (news_id,)).fetchone()
        an = conn.execute(
            "SELECT * FROM qwen_news_analysis WHERE news_id = ? AND ticker = ? "
            "AND analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
            "ORDER BY created_date DESC LIMIT 1", (news_id, ticker)).fetchone()
        return (dict(ev) if ev else None), (dict(an) if an else None)
    finally:
        conn.close()


def _synthetic_scan(news_id, ticker, ev, an, is_holding: bool) -> dict:
    """Build a scanner_output-shaped dict with one candidate carrying the news as
    its reason. A held ticker → position_alerts (management thesis); otherwise a
    new_candidates entry. source_candidate traces back to the news id downstream."""
    headline = (ev or {}).get("headline") or ""
    implication = (ev or {}).get("implication") or ""
    summary = (an or {}).get("summary") or ""
    reason = " · ".join([x for x in (headline, implication, summary) if x])[:500] or headline
    signals = {"materiality": (ev or {}).get("materiality"),
               "sentiment": (an or {}).get("sentiment"),
               "direction": (ev or {}).get("direction")}
    context = {"news_id": news_id, "source": (ev or {}).get("source"),
               "flagged_by": ["news"]}
    scan = {"cycle_id": f"news:{news_id}", "timestamp": datetime.now().isoformat(timespec="seconds"),
            "new_candidates": [], "position_alerts": []}
    if is_holding:
        scan["position_alerts"] = [{
            "ticker": ticker, "held_since": None, "trigger": "breaking_news",
            "detail": reason, "urgency": "needs_attention_now",
            "signals": signals, "context": context}]
    else:
        scan["new_candidates"] = [{
            "ticker": ticker, "flagged_by": ["news"], "reason": reason,
            "urgency": "needs_attention_now", "signals": signals, "context": context}]
    return scan


# ── recommendation persistence ──────────────────────────────────────────────────

def _open_rec_for_ticker(ticker: str, cooldown_hours: float) -> dict | None:
    cutoff = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat()
    conn = mason_db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE ticker = ? AND status = 'open' "
            "AND COALESCE(record_deleted,0)=0 AND created_date >= ? "
            "ORDER BY created_date DESC LIMIT 1", (ticker, cutoff)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _insert_rec(rec: dict) -> str:
    rid = uuid.uuid4().hex[:12]
    cols = ("id", "created_date", "created_by", "ticker", "kind", "direction",
            "instrument", "action_text", "order_preview", "materiality",
            "source_news_id", "thesis_id", "status", "entry_ref_price", "notes",
            "record_deleted")
    vals = (rid, datetime.now().isoformat(), "mason", rec["ticker"], rec.get("kind"),
            rec.get("direction"), rec.get("instrument", "equity"),
            rec.get("action_text"), rec.get("order_preview"), rec.get("materiality"),
            rec.get("source_news_id"), rec.get("thesis_id"), rec.get("status", "open"),
            rec.get("entry_ref_price"), rec.get("notes"), 0)
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                f"INSERT INTO recommendations ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})", vals)
        return rid
    finally:
        conn.close()


def _supersede_rec(rec_id: str, note: str):
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE recommendations SET status='superseded', notes = "
                "COALESCE(notes,'') || ? WHERE id = ?", (f"\n[superseded] {note}", rec_id))
    finally:
        conn.close()


def _touch_rec(rec_id: str, materiality, note: str):
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE recommendations SET materiality = COALESCE(?, materiality), "
                "notes = COALESCE(notes,'') || ? WHERE id = ?",
                (materiality, f"\n[update] {note}", rec_id))
    finally:
        conn.close()


# ── track record (Deliverable E) ────────────────────────────────────────────────

_TR_CFG = {"mtime": None, "src": "legacy"}


def _track_record_source() -> str:
    """analyst_desk.recommendations.track_record_source, read per call from mason.config.json (mtime-cached; never
    through mason_app). Knowledge base item 2 (2026-11-18). "legacy" (absent = today) keeps the old line, which
    counts every graded rec of the ticker by its OLD grade: the 30-minute grade_recommendations path for the local
    desk's calls (stale prices, preview-limit entries) and the director's reference-price grade. "tradable" counts
    only the director's long/short calls, by the trade-based grade every other reader already uses (grade_v2)."""
    try:
        from pathlib import Path
        p = Path(__file__).parent / "mason.config.json"
        m = p.stat().st_mtime
        if _TR_CFG["mtime"] != m:
            c = json.loads(p.read_text(encoding="utf-8"))
            v = (((c.get("analyst_desk") or {}).get("recommendations") or {}).get("track_record_source"))
            _TR_CFG.update(mtime=m, src="tradable" if str(v or "").strip().lower() == "tradable" else "legacy")
        return _TR_CFG["src"]
    except Exception:
        return "legacy"


def _tradable_track_record(ticker: str) -> dict:
    """The director's long/short calls on `ticker`, scored as trades (tradable_verdict, the one shared rule).
    Calls still running, never entered or not scored are counted apart, never as right or wrong.
    (Review 2026-11-19) Only the Opus director's own calls (created_by 'opus_director'), the same set its scorecard
    counts (director_call_verdicts): the local model's stand-in director runs of 09-23..09-25 are not its calls. The
    wording is kept short because the line sits inside a write-up with a fixed budget, where a longer line can push a
    whole option expiry out (measured on the stored write-ups: 8 of 4,040 renders with the first wording)."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT extra_json FROM recommendations WHERE ticker = ? AND kind='director' "
            "AND created_by='opus_director' AND direction IN ('long','short') AND COALESCE(record_deleted,0)=0",
            (ticker,)).fetchall()
    finally:
        conn.close()
    tally = {"right": 0, "wrong": 0, "neutral": 0}
    other = 0
    for r in rows:
        try:
            ex = json.loads(r["extra_json"] or "{}")
        except Exception:
            ex = {}
        v = tradable_verdict(ex if isinstance(ex, dict) else {})
        if v is not None and v[0] == "count" and v[1] in tally:
            tally[v[1]] += 1
        else:
            other += 1
    n = sum(tally.values())
    more = f" (+{other} open or not scored)" if other else ""
    if not n and not other:
        line = f"No director call on {ticker} yet"
    elif not n:
        line = f"Director's {ticker} calls as trades: none scored yet" + more
    else:
        line = (f"Director's {ticker} calls as trades: {tally['right']} right / {tally['wrong']} wrong"
                + (f" / {tally['neutral']} neutral" if tally["neutral"] else "") + more)
    return {"n": n, "right": tally["right"], "wrong": tally["wrong"], "neutral": tally["neutral"],
            "not_scored": other, "source": "tradable", "line": line}


def ticker_track_record(ticker: str) -> dict:
    """Graded desk recs for a ticker → {n, right, wrong, neutral, line}."""
    if _track_record_source() == "tradable":
        return _tradable_track_record(ticker)
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT direction, outcome FROM recommendations WHERE ticker = ? "
            "AND status='graded' AND COALESCE(record_deleted,0)=0", (ticker,)).fetchall()
    finally:
        conn.close()
    n = len(rows)
    right = sum(1 for r in rows if r["outcome"] == "right")
    wrong = sum(1 for r in rows if r["outcome"] == "wrong")
    neutral = sum(1 for r in rows if r["outcome"] == "neutral")
    if not n:
        line = f"Desk has no graded calls on {ticker} yet"
    else:
        line = f"Desk called {ticker} {n}× → {right} right / {wrong} wrong" + (
            f" / {neutral} neutral" if neutral else "")
    return {"n": n, "right": right, "wrong": wrong, "neutral": neutral, "line": line}


# ── coarse order preview (deterministic, labelled) ──────────────────────────────

_DRAFT_LABEL = "⚠ qwen draft — not a precise entry; review before sending."


def _build_order_preview(ticker, direction, thesis, quote_price_fn, notional):
    """Deterministic, COARSE confirm line (Nico's C8): live limit + notional-sized
    qty via mason_order_builder.default_qty, round numbers, labelled a draft.
    Returns (labelled_preview_str|None, limit|None)."""
    try:
        import mason_order_builder as OB
    except Exception:
        return None, None
    price = quote_price_fn(ticker)
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, None
    if not price or price <= 0:
        return None, None
    limit = round(price, 2)
    qty = OB.default_qty(limit, notional)
    if not qty:
        return None, None
    side = "buy" if direction == "long" else "sell"
    # COARSE by design (Nico's C8): live limit + notional-sized qty only. We do NOT
    # bracket with the thesis's stop/target — qwen's levels are unreliable (it has
    # fabricated levels that contradict the price, e.g. a target below entry on a
    # long), and precise entries are the future Opus director's job. A plain
    # limit line keeps the draft honest; the label says review before sending.
    line = f"confirm order {ticker} {side} {qty} limit {limit:g}"
    return f"{_DRAFT_LABEL}\n{line}", limit


# ── recommendation synthesis (Deliverable C + D) ────────────────────────────────

def _verdict_summary(verdict: dict) -> str:
    v = verdict.get("verdict", "?")
    rg = verdict.get("regime_gate_check", "?")
    cc = verdict.get("correlation_check", "?")
    reason = (verdict.get("reasoning") or "").strip()
    if len(reason) > 300:
        reason = reason[:300] + "…"
    return f"Verdict {v} (regime gate: {rg}, correlation: {cc}). {reason}"


def synthesize_recommendation(*, news_id, ticker, materiality, verdict, thesis,
                              thesis_id, notifier, cfg, quote_price_fn, now=None) -> dict:
    """Write one recommendations row + one 'recommendation' notification from the
    (possibly REVISE-adjusted) thesis + verdict, honouring cooldown/dedup/re-proposal.
    A REJECT surfaces as a 'watch' rec with the reason (never suppressed)."""
    desk = _desk(cfg)
    notifier = notifier or mason_notifications.add_notification
    quote_price_fn = quote_price_fn or _default_last_price
    cooldown_hours = float(_cfg_get(desk, ["recommendations", "cooldown_hours"], 12) or 12)
    notional = float(_order_notional(cfg))

    v = verdict.get("verdict", "REJECT")
    actionable = v in ("APPROVE", "REVISE")
    direction = (thesis.get("direction") or "").lower() if actionable else "watch"
    is_mgmt = "#position_alert:" in str(thesis.get("source_candidate") or "")
    tr = ticker_track_record(ticker)

    # ── Deliverable D: cooldown / re-proposal ────────────────────────────────
    existing = _open_rec_for_ticker(ticker, cooldown_hours)
    if existing and actionable:
        if existing.get("direction") == direction:
            _touch_rec(existing["id"], materiality,
                       f"thesis intact after news {news_id} ({v})")
            body = (f"{ticker} is still favored {direction} — thesis intact after the "
                    f"latest event.\n{_verdict_summary(verdict)}\n{tr['line']}")
            notifier(type="recommendation", severity="info", ticker=ticker,
                     title=f"{ticker} is still favored {direction}", body=body,
                     ref_table="recommendations", ref_id=existing["id"],
                     created_by="mason", dedup_key=f"recupdate:{ticker}:{existing['id']}")
            return {"updated": existing["id"], "action": "reaffirmed", "direction": direction}
        elif existing.get("direction") in ("long", "short") and existing["direction"] != direction:
            _supersede_rec(existing["id"], f"reversed to {direction} after news {news_id}")
            # fall through to create a fresh rec noting the flip

    # ── build the new rec ────────────────────────────────────────────────────
    order_preview, limit = (None, None)
    entry_ref_price = None
    if actionable:
        order_preview, limit = _build_order_preview(ticker, direction, thesis,
                                                    quote_price_fn, notional)
        entry_ref_price = limit
        kind = "manage_position" if is_mgmt else "new_idea"
        reason = (thesis.get("reasoning") or "").strip()
        if len(reason) > 220:
            reason = reason[:220] + "…"
        action_text = (f"Qwen desk favors a {direction} on {ticker}"
                       + (f" — {reason}" if reason else "")
                       + ". Coarse draft; confirm/adjust before acting.")
        severity = "warn"
    else:
        kind = "watch"
        action_text = (f"Surfaced for awareness (not actioning): "
                       + (verdict.get("reasoning") or "debate/verdict did not approve.").strip())
        severity = "info"
        if entry_ref_price is None:
            entry_ref_price = None

    rec_id = _insert_rec({
        "ticker": ticker, "kind": kind, "direction": direction, "instrument": "equity",
        "action_text": action_text, "order_preview": order_preview,
        "materiality": materiality, "source_news_id": news_id, "thesis_id": thesis_id,
        "status": "open", "entry_ref_price": entry_ref_price,
        "notes": f"verdict={v}",
    })

    body_parts = [action_text, _verdict_summary(verdict)]
    if order_preview:
        body_parts.append(order_preview)
    body_parts.append(tr["line"])
    notifier(type="recommendation", severity=severity, ticker=ticker,
             title=(f"{ticker}: Qwen favors {direction}" if actionable
                    else f"{ticker}: Watch — {v.lower()}"),
             body="\n\n".join(body_parts), ref_table="recommendations", ref_id=rec_id,
             created_by="mason", dedup_key=f"rec:{rec_id}")
    return {"rec_id": rec_id, "verdict": v, "actionable": actionable,
            "direction": direction, "kind": kind}


def _order_notional(cfg=None):
    try:
        c = cfg if cfg is not None else AD._load_config()
        return (c.get("order_builder") or {}).get("default_notional", 1000)
    except Exception:
        return 1000


# ── the pipeline (Deliverable B) ────────────────────────────────────────────────

def run_debate_pipeline(news_id: str, ticker: str, *, call_model=None, notifier=None,
                        quote_price_fn=None, cfg=None, holdings=None, watchlist=None,
                        busy_fn=None, sleep_fn=None, clock=None, now=None,
                        max_busy_wait=None,
                        predictor_call=None, debate_call=None, arbiter_call=None) -> dict:
    """Run the full news→thesis→debate→arbiter→recommendation pipeline for one
    breaking event. Every stage is isolated; a failure degrades to a safe,
    informative outcome and never crashes the worker. Returns a summary dict.

    call_model      one fake model used for all three stages (tests). None in prod
                    → predictor/debate/arbiter use their own local-qwen _call_model.
    predictor_call/debate_call/arbiter_call  per-stage overrides (default: call_model)
    """
    import time as _time
    notifier = notifier or mason_notifications.add_notification
    quote_price_fn = quote_price_fn or _default_last_price
    busy_fn = busy_fn if busy_fn is not None else AD._default_busy
    sleep_fn = sleep_fn or _time.sleep
    clock = clock or _time.time
    ticker = (ticker or "").upper()
    desk = _desk(cfg)

    if _cfg_get(desk, ["debate", "enabled"], True) is False:
        return {"skipped": "debate disabled", "ticker": ticker}

    ev, an = _load_news_and_analysis(news_id, ticker)
    if ev is None:
        return {"skipped": "news event not found", "news_id": news_id}

    # ── Step 1: breaking gate (materiality + book membership) ────────────────
    thresh = float(_cfg_get(desk, ["debate", "breaking_threshold"], 0.8) or 0.8)
    materiality = ev.get("materiality") or 0
    if materiality < thresh:
        return {"skipped": f"not breaking ({materiality} < {thresh})", "ticker": ticker}
    holds = holdings if holdings is not None else _default_holdings()
    watch = watchlist if watchlist is not None else _default_watchlist()
    is_holding = ticker in holds
    if ticker not in holds and ticker not in watch:
        return {"skipped": "ticker not in holdings/watchlist", "ticker": ticker}

    now_et = AD._now_et(now)
    ok, why = _rate_ok(now_et, desk)
    if not ok:
        try:
            mason_activity.log_activity(
                "debate_skipped", f"Skipped debating {ticker} — {why}", actor="qwen",
                category="analyst", ticker=ticker, trigger="autonomous",
                extra={"reason": why})
        except Exception:
            pass
        return {"skipped": f"rate limit: {why}", "ticker": ticker}

    # yield the GPU to interactive chat before spending ~4-5 qwen calls
    yield_sleep = float(_cfg_get(desk, ["worker", "yield_sleep_seconds"], 5) or 5)
    if not AD._gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
        return {"deferred": True, "ticker": ticker}

    # analyst ledger: a full debate is beginning on this ticker
    try:
        mason_activity.log_activity(
            "debate_started", f"qwen began a debate — {ticker}", actor="qwen",
            category="analyst", ticker=ticker, trigger="autonomous",
            extra={"news_id": news_id, "materiality": materiality})
    except Exception:
        pass

    pcall = predictor_call or call_model
    dcall = debate_call or call_model
    acall = arbiter_call or call_model

    # ── Step 2+3: synthetic candidate → provisional qwen thesis ──────────────
    import mason_predictor as P
    scan = _synthetic_scan(news_id, ticker, ev, an, is_holding)
    try:
        pres = P.run_predictor(model_tier="local", scan=scan, max_candidates=1,
                               call_model=pcall, write=True)
        result0 = pres[0] if pres else {"ok": False, "error": "no candidate"}
    except Exception as e:
        result0 = {"ok": False, "error": f"predictor crashed: {e}"}
    _rate_incr(now_et)   # a debate attempt has begun (counts toward the cap)

    if not result0.get("ok"):
        return _watch_outcome(news_id, ticker, materiality, notifier,
                              f"could not form a thesis: {result0.get('error')}",
                              cfg=cfg)
    thesis = result0["thesis"]
    thesis_path = result0.get("path")
    thesis_id = (thesis_path.split("/")[-1] if thesis_path else result0.get("source_candidate"))

    # ── Step 4: Bull/Bear debate (qwen) ──────────────────────────────────────
    import mason_debate as DBT
    try:
        transcript = DBT.run_debate(thesis_path or thesis, model_tier="local",
                                    call_model=dcall, write=True)
    except Exception as e:
        return _watch_outcome(news_id, ticker, materiality, notifier,
                              f"debate crashed: {e}", cfg=cfg)
    ok_t, _probs = DBT.validate_transcript(transcript)
    if transcript.get("skipped") or not ok_t:
        return _watch_outcome(news_id, ticker, materiality, notifier,
                              "debate was inconclusive/incomplete — surfacing for awareness only",
                              cfg=cfg)

    # ── Step 5: Arbiter (qwen) — hard gates intact ───────────────────────────
    import mason_arbiter as ARB
    try:
        verdict = ARB.run_arbiter(transcript, model_tier="local",
                                  call_model=acall, write=True)
    except Exception as e:
        return _watch_outcome(news_id, ticker, materiality, notifier,
                              f"arbiter crashed: {e}", cfg=cfg)

    # ── recommendation synthesis ─────────────────────────────────────────────
    final_thesis = verdict.get("revised_plan") if verdict.get("verdict") == "REVISE" else thesis
    final_thesis = final_thesis or thesis
    out = synthesize_recommendation(
        news_id=news_id, ticker=ticker, materiality=materiality, verdict=verdict,
        thesis=final_thesis, thesis_id=thesis_id, notifier=notifier, cfg=cfg,
        quote_price_fn=quote_price_fn, now=now)
    # analyst ledger: the debate is analyst cognition; link forward to the
    # recommendation it produced.
    try:
        rec_id = out.get("rec_id") if isinstance(out, dict) else None
        mason_activity.log_activity(
            "debate_finished", f"Qwen debated {ticker} → {verdict.get('verdict')}",
            actor="qwen", category="analyst", ticker=ticker, trigger="autonomous",
            body=(out.get("action_text") if isinstance(out, dict) else None),
            ref_table="recommendations" if rec_id else None, ref_id=rec_id,
            extra={"verdict": verdict.get("verdict"),
                   "actionable": out.get("actionable") if isinstance(out, dict) else None})
    except Exception:
        pass
    out.update(ticker=ticker, news_id=news_id)
    return out


def _watch_outcome(news_id, ticker, materiality, notifier, reason, *, cfg=None) -> dict:
    """A non-actionable outcome (no/inconclusive verdict) still surfaces as a
    'watch' rec + notification — never silently hidden (Nico's E13)."""
    rec_id = _insert_rec({
        "ticker": ticker, "kind": "watch", "direction": "watch", "instrument": "equity",
        "action_text": f"Surfaced for awareness (not actioning): {reason}",
        "order_preview": None, "materiality": materiality, "source_news_id": news_id,
        "thesis_id": None, "status": "open", "entry_ref_price": None,
        "notes": "watch (non-actionable)"})
    tr = ticker_track_record(ticker)
    notifier(type="recommendation", severity="info", ticker=ticker,
             title=f"{ticker}: Watch", body=f"{reason}\n\n{tr['line']}",
             ref_table="recommendations", ref_id=rec_id, created_by="mason",
             dedup_key=f"rec:{rec_id}")
    return {"rec_id": rec_id, "verdict": "WATCH", "actionable": False,
            "direction": "watch", "kind": "watch", "ticker": ticker, "news_id": news_id}


# ── Deliverable E: grading ──────────────────────────────────────────────────────

def grade_recommendations(*, price_fn=None, cfg=None, now=None) -> dict:
    """Grade aged open, actionable recs vs current price (±dead_band_pct). Recs
    older than grade_max_hours that never resolved are expired. Watch recs are not
    graded (no directional call). Returns a summary dict."""
    price_fn = price_fn or _default_last_price
    desk = _desk(cfg)
    min_h = float(_cfg_get(desk, ["recommendations", "grade_min_hours"], 4) or 4)
    max_h = float(_cfg_get(desk, ["recommendations", "grade_max_hours"], 72) or 72)
    dead = float(_cfg_get(desk, ["recommendations", "dead_band_pct"], 0.5) or 0.5)
    now_ts = now if now is not None else __import__("time").time()

    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendations WHERE status='open' "
            "AND COALESCE(record_deleted,0)=0").fetchall()]
    finally:
        conn.close()

    graded = expired = 0
    for rec in rows:
        if rec.get("kind") == "director":
            continue                                   # graded by grade_director_predictions (multi-horizon)
        try:
            created = datetime.fromisoformat(rec["created_date"])
        except Exception:
            continue
        age_h = (datetime.fromtimestamp(now_ts) - created).total_seconds() / 3600.0
        if age_h < min_h:
            continue
        direction = rec.get("direction")
        if direction not in ("long", "short"):
            continue                                  # watch recs aren't graded
        if age_h > max_h:
            _set_rec_status(rec["id"], "expired", eval_price=None, outcome=None, now_ts=now_ts)
            expired += 1
            continue
        entry = rec.get("entry_ref_price")
        price = price_fn(rec["ticker"])
        try:
            entry = float(entry); price = float(price)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or price <= 0:
            continue
        move_pct = (price - entry) / entry * 100.0
        if abs(move_pct) <= dead:
            outcome = "neutral"
        elif direction == "long":
            outcome = "right" if move_pct > 0 else "wrong"
        else:  # short
            outcome = "right" if move_pct < 0 else "wrong"
        _set_rec_status(rec["id"], "graded", eval_price=round(price, 4),
                        outcome=outcome, now_ts=now_ts)
        graded += 1
    if graded or expired:
        try:
            mason_activity.log_activity(
                "recs_graded",
                f"Qwen graded {graded} recommendation{'s' if graded != 1 else ''}"
                + (f", expired {expired}" if expired else ""),
                actor="qwen", category="analyst", trigger="autonomous",
                extra={"graded": graded, "expired": expired,
                       "considered": len(rows)})
        except Exception:
            pass
    return {"graded": graded, "expired": expired, "considered": len(rows)}


def _set_rec_status(rec_id, status, *, eval_price, outcome, now_ts):
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE recommendations SET status=?, outcome=?, eval_price=?, "
                "outcome_checked_date=? WHERE id=?",
                (status, outcome, eval_price,
                 datetime.fromtimestamp(now_ts).isoformat(), rec_id))
    finally:
        conn.close()


# ── Deliverable F: director package + Opus seam (STUB, OFF) ─────────────────────

def assemble_director_package(kind: str, contexts: dict, *, cfg=None, now=None) -> dict:
    """Assemble the stable director_package/1.0 the future Opus director will
    consume (see the module docstring's handoff contract). Reads recent
    qwen_news_analysis + recommendations; no Opus is called."""
    since_iso = AD.get_state("last_director_package_at") or \
        (datetime.now() - timedelta(hours=12)).isoformat()
    conn = mason_db.get_connection()
    try:
        analyses = [dict(r) for r in conn.execute(
            "SELECT ticker, sentiment, summary, materiality, created_date FROM qwen_news_analysis "
            "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
            "AND created_date > ? ORDER BY created_date DESC LIMIT 40", (since_iso,)).fetchall()]
        recs = [dict(r) for r in conn.execute(
            "SELECT id, ticker, kind, direction, status, outcome, action_text, materiality, "
            "created_date FROM recommendations WHERE COALESCE(record_deleted,0)=0 "
            "AND created_date > ? ORDER BY created_date DESC LIMIT 40", (since_iso,)).fetchall()]
    finally:
        conn.close()
    tickers = sorted({r["ticker"] for r in recs if r.get("ticker")})
    track = {t: ticker_track_record(t) for t in tickers}
    package = {
        "schema_version": "director_package/1.0",
        "kind": kind,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contexts": contexts,
        "recent_analyses": analyses,
        "recommendations": recs,
        "track_record": track,
        "note": "Assembled for the future Opus director. Opus is NOT called in "
                "Phase 3 (seam disabled). See mason_desk_pipeline handoff contract.",
    }
    return package


def persist_director_package(package: dict) -> str:
    """Store the package as a qwen_news_analysis analysis_type='director_package'
    row for durable history + Phase-4 handoff. Returns the row id."""
    rid = uuid.uuid4().hex[:12]
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO qwen_news_analysis (id, created_date, created_by, "
                "analysis_type, summary, details_json, model, record_deleted) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (rid, datetime.now().isoformat(), "mason", "director_package",
                 f"director package ({package.get('kind')})",
                 json.dumps(package, default=str), "n/a"))
    finally:
        conn.close()
    AD.set_state("last_director_package_at", datetime.now().isoformat())
    return rid


# ── Opus DIRECTOR dry-run (controlled A/B, observe-only) ────────────────────────
# Spec: docs/prompts/OPUS_DIRECTOR_DRYRUN_TRIAL.md. When
# analyst_desk.opus_director.enabled is true, each 07:00/13:00 anchor makes ONE
# billed Opus deep call (the real director) and — when ab_qwen — one FREE qwen
# "shadow director" on the IDENTICAL package as a benchmark ONLY (never acted on).
# Both land as kind='director' recs (created_by opus_director/qwen_director, linked
# by ab_pair_id), OBSERVE-ONLY: order_preview is NULL, nothing is staged or placed.
# grade_director_predictions() scores each at +1d/+5d/+30d/+90d into
# extra_json.grades; a row closes only at its declared horizon.

_DIRECTOR_SCHEMA = "director_decision/1.0"


def _director_call_model(system_prompt, user_prompt, tier):
    """Tier-aware director call via the predictor's ONE gateway touchpoint:
    tier "opus" -> intent="deep" (billed Opus); tier "local" -> intent="chore"
    (free qwen). Never hit by tests (they inject call_model)."""
    import mason_predictor as MP
    return MP._call_model(system_prompt, user_prompt, tier)


def _director_model_id(tier: str) -> str:
    try:
        import mason_predictor as MP
        return MP.MODEL_TIERS[tier]["model"]
    except Exception:
        return {"opus": "claude-opus-5-5", "local": "qwen3.5:4b"}.get(tier, tier)


def _norm_direction(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("long", "buy", "bull", "bullish", "call"):
        return "long"
    if s in ("short", "sell", "bear", "bearish", "put"):
        return "short"
    return "watch"


def _canon_horizon(v):
    """Canonicalize any horizon phrasing → (label, days). Checks long buckets
    before short so '3 month' doesn't match the generic swing default."""
    key = str(v or "").strip().lower().replace(" ", "").replace("-", "").replace("+", "")
    if any(t in key for t in ("90d", "3m", "3month", "position", "longterm", "long")):
        return "90d+", 90
    if any(t in key for t in ("30d", "1m", "1month", "month")):
        return "30d", 30
    if any(t in key for t in ("0dte", "05dte", "0to5", "dte")):
        return "0-5DTE", 5
    return "swing", 5


def _director_user_payload(package: dict) -> str:
    """Bounded JSON payload of the package for the director prompt."""
    contexts = package.get("contexts") or {}
    report = (package.get("report_text") or contexts.get("report") or "")[:2500]
    analyses = [{"ticker": a.get("ticker"), "sentiment": a.get("sentiment"),
                 "summary": (a.get("summary") or "")[:240],
                 "materiality": a.get("materiality")}
                for a in (package.get("recent_analyses") or [])[:15]]
    recs = [{"ticker": r.get("ticker"), "direction": r.get("direction"),
             "kind": r.get("kind"), "outcome": r.get("outcome")}
            for r in (package.get("recommendations") or [])[:15]]
    track = {t: (tr.get("line") if isinstance(tr, dict) else tr)
             for t, tr in (package.get("track_record") or {}).items()}
    payload = {"anchor": package.get("kind"), "desk_report": report,
               "recent_analyses": analyses, "recent_recommendations": recs,
               "track_record": track}
    if package.get("verified_claims"):
        payload["verified_claims"] = package["verified_claims"][:20]
    return json.dumps(payload, default=str)


def _director_prompts(package: dict):
    kind = package.get("kind")
    system = (
        "You are Mason's DIRECTOR — the final decision-maker at the top of a 24/7 "
        "equity analyst desk. Junior qwen analysts have already consolidated news, "
        "sentiment, analytics, fundamentals and positioning into the package below. "
        f"At this {kind} anchor, make ONE clear, accountable call for the book "
        "(holdings + watchlist). Ground EVERY claim ONLY in the package — do NOT "
        "invent tickers, prices or levels absent from it. Reply with ONE JSON "
        "object and nothing else:\n"
        '{"prediction": "<=2 sentences: what you expect and why, grounded in the package",\n'
        ' "play": {"ticker": "<one ticker present in the package>", "direction": "long|short"},\n'
        ' "sentiment": "bullish|bearish|neutral",\n'
        ' "confidence": <0.0-1.0 calibrated conviction>,\n'
        ' "horizon": "0-5DTE|swing|30d|90d+"}\n'
        'If nothing is actionable, set play.direction to "watch" and confidence <= 0.3.')
    return system, _director_user_payload(package)


def run_director(package: dict, tier: str, *, call_model=None) -> dict:
    """Run ONE director decision from the package at the given tier
    ("opus" billed / "local" free). Returns the normalized decision. Never raises;
    a bad model reply degrades to a non-directional 'watch'. call_model(system,
    user, tier) is injectable for tests."""
    caller = call_model or _director_call_model
    system, user = _director_prompts(package)
    base = {"schema": _DIRECTOR_SCHEMA, "tier": tier,
            "model_id": _director_model_id(tier),
            "play": {"ticker": None, "direction": "watch"},
            "prediction": "", "sentiment": "neutral", "confidence": None,
            "horizon": "swing", "horizon_days": 5, "ok": False, "error": None,
            "raw": ""}
    try:
        text = caller(system, user, tier)
    except Exception as e:
        base["error"] = f"model call failed: {e}"
        return base
    base["raw"] = text or ""
    import mason_predictor as MP
    obj, err = MP.parse_model_response(text)
    if obj is None:
        base["error"] = err or "unparseable director response"
        return base
    play = obj.get("play") if isinstance(obj.get("play"), dict) else {}
    ticker = (play.get("ticker") or obj.get("ticker") or "")
    ticker = ticker.upper().strip() if isinstance(ticker, str) else None
    if ticker in ("", "NONE", "N/A", "NA", "-", "TICKER", "NULL"):
        ticker = None                                   # abstain placeholder, not a symbol
    hlabel, hdays = _canon_horizon(obj.get("horizon") or play.get("horizon"))
    try:
        conf = float(obj.get("confidence"))
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None
    base.update({
        "ok": True,
        "prediction": str(obj.get("prediction") or "").strip(),
        "play": {"ticker": ticker or None,
                 "direction": _norm_direction(play.get("direction") or obj.get("direction"))},
        "sentiment": str(obj.get("sentiment") or "neutral").strip().lower(),
        "confidence": conf, "horizon": hlabel, "horizon_days": hdays})
    return base


def _insert_director_rec(*, created_by, ticker, direction, action_text,
                         entry_ref_price, extra) -> str:
    rid = uuid.uuid4().hex[:12]
    cols = ("id", "created_date", "created_by", "ticker", "kind", "direction",
            "instrument", "action_text", "order_preview", "materiality", "status",
            "entry_ref_price", "notes", "extra_json", "record_deleted")
    vals = (rid, datetime.now().isoformat(), created_by, ticker or "N/A", "director",
            direction, "equity", action_text, None, None, "open", entry_ref_price,
            f"director dry-run ({extra.get('anchor_kind')})",
            json.dumps(extra, default=str), 0)
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                f"INSERT INTO recommendations ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})", vals)
        return rid
    finally:
        conn.close()


def _persist_director_row(out, created_by, kind, ab_pair_id, package_id,
                          price_fn, desk) -> dict:
    play = out.get("play") or {}
    ticker = play.get("ticker")
    direction = play.get("direction") or "watch"
    entry = None
    if ticker and direction in ("long", "short"):       # only price real, directional calls
        try:
            entry = float(price_fn(ticker))
        except (TypeError, ValueError):
            entry = None
    horizons = _cfg_get(desk, ["opus_director", "horizons_days"], [1, 5, 30, 90]) \
        or [1, 5, 30, 90]
    hdays = int(out.get("horizon_days") or 90)
    extra = {"schema": out.get("schema"), "anchor_kind": kind, "ab_pair_id": ab_pair_id,
             "package_id": package_id, "tier": out.get("tier"),
             "model_id": out.get("model_id"), "prediction": out.get("prediction"),
             "sentiment": out.get("sentiment"), "confidence": out.get("confidence"),
             "horizon": out.get("horizon"), "horizon_days": hdays,
             "checkpoints_days": sorted(d for d in horizons if d <= hdays),
             "grades": {}, "ok": out.get("ok"), "error": out.get("error")}
    who = "Opus" if created_by == "opus_director" else "qwen (shadow)"
    if direction in ("long", "short") and ticker:
        conf = out.get("confidence")
        cs = f", conf {conf:.0%}" if isinstance(conf, (int, float)) else ""
        action_text = (f"{who} director call: {direction} {ticker} "
                       f"({out.get('horizon')}{cs}). Observe-only — nothing staged. "
                       + (out.get("prediction") or ""))
    else:
        action_text = (f"{who} director: no actionable call "
                       + (f"({out.get('error')})" if out.get("error") else "(watch).")).strip()
    rid = _insert_director_rec(created_by=created_by, ticker=ticker, direction=direction,
                               action_text=action_text, entry_ref_price=entry, extra=extra)
    return {"rec_id": rid, "ticker": ticker, "direction": direction, "entry": entry}


def _notify_director(notifier, kind, ab_pair_id, results, rows):
    def _line(tag, out):
        p = (out or {}).get("play") or {}
        conf = (out or {}).get("confidence")
        cs = f"{conf:.0%}" if isinstance(conf, (int, float)) else "n/a"
        return f"{tag}: {p.get('direction', '?')} {p.get('ticker') or '—'} " \
               f"({(out or {}).get('horizon', '?')}, conf {cs})"

    opus = results.get("opus") or {}
    qwen = results.get("qwen")
    op = opus.get("play") or {}
    lines = [_line("Opus", opus)]
    if qwen is not None:
        lines.append(_line("qwen (shadow)", qwen))
        qp = qwen.get("play") or {}
        if op.get("ticker") and op.get("ticker") == qp.get("ticker"):
            lines.append("Agreement: " + ("AGREE"
                         if op.get("direction") == qp.get("direction")
                         else "DISAGREE (same ticker, opposite call)"))
        else:
            lines.append("Agreement: different tickers")
    body = ("Director dry-run (observe-only — nothing staged or placed).\n\n"
            + "\n".join(lines) + "\n\nOpus thesis: " + (opus.get("prediction") or "—"))
    title = f"Director {kind}: Opus {op.get('direction', '?')} {op.get('ticker') or '—'}"
    ref_id = (rows.get("opus_director") or {}).get("rec_id")
    try:
        notifier(type="recommendation", severity="info", ticker=op.get("ticker"),
                 title=title, body=body, ref_table="recommendations", ref_id=ref_id,
                 created_by="opus_director", dedup_key=f"director:{ab_pair_id}")
    except Exception as e:
        log.warning(f"[desk] director notification failed: {e}")


def _maybe_set_review_reminder(desk):
    """Once, at trial start, set a Mason reminder at the longest horizon to review
    the long-horizon (30d/90d) director grades that mature after the 5-day window."""
    if not _cfg_get(desk, ["opus_director", "review_reminder"], True):
        return
    if AD.get_state("opus_director_review_reminder_set"):
        return
    horizons = _cfg_get(desk, ["opus_director", "horizons_days"], [1, 5, 30, 90]) \
        or [1, 5, 30, 90]
    longest = max(horizons)
    try:
        import mason_agenda
        fire = (datetime.now() + timedelta(days=longest)).date().isoformat()
        mason_agenda.add_reminder(
            f"Review Opus-director dry-run long-horizon grades (+{longest}d). "
            "See docs/prompts/OPUS_DIRECTOR_DRYRUN_TRIAL.md.",
            fire_on=fire, created_by="mason")
        AD.set_state("opus_director_review_reminder_set", "1")
    except Exception as e:
        log.warning(f"[desk] could not set director review reminder: {e}")


def _maybe_run_opus_director(package: dict, *, cfg=None, call_model=None,
                             notifier=None, price_fn=None):
    """Opus DIRECTOR seam. DISABLED (default) → verbatim no-op returning None, NO
    Opus call. ENABLED → the observe-only A/B dry-run: one billed Opus director
    call + (ab_qwen) one free qwen shadow on the identical package; both persisted
    as kind='director' recs and surfaced in ONE notification. Nothing is staged or
    placed. See docs/prompts/OPUS_DIRECTOR_DRYRUN_TRIAL.md."""
    desk = _desk(cfg)
    enabled = _cfg_get(desk, ["opus_director", "enabled"], False)
    if not enabled:
        log.info("[desk] opus director seam ready (disabled) — package assembled (%s), "
                 "Opus NOT called", package.get("kind"))
        try:
            mason_activity.log_activity(
                "opus_director_seam", "Opus Director Seam Ready (Disabled)",
                actor="system", category="plumbing", trigger="autonomous",
                extra={"state": "disabled", "kind": package.get("kind")})
        except Exception:
            pass
        return None

    # ── Catalyst Desk director v2 (Opus 5.5 + dossiers + web research) ─────────
    # opus_director.v2.enabled routes the anchor to mason_director; off ⇒ the v1
    # dry-run below runs byte-for-byte as before.
    # The v2 flag is taken from the cfg the caller passed (the desk's snapshot, which
    # /api/opus_director refreshes); only a cfg-less call reads the file. This keeps an
    # injected test cfg from being routed into billed v2 calls by the LIVE config.
    try:
        import mason_director as DIR
        v2_on = (_cfg_get(desk, ["opus_director", "v2", "enabled"], False)
                 if cfg is not None else DIR.enabled())
        if v2_on:
            return DIR.run_anchor(package, notifier=notifier, price_fn=price_fn)
    except Exception as e:
        log.warning(f"[desk] director v2 failed, not falling back to v1: {e}")
        return {"error": f"director v2 failed: {e}", "kind": package.get("kind")}

    # ── ENABLED: observe-only A/B director dry-run ───────────────────────────────
    notifier = notifier or mason_notifications.add_notification
    price_fn = price_fn or _default_last_price
    ab_qwen = _cfg_get(desk, ["opus_director", "ab_qwen"], True)
    kind = package.get("kind")
    ab_pair_id = uuid.uuid4().hex[:12]
    package_id = package.get("package_id")

    results, rows = {}, {}
    opus = run_director(package, "opus", call_model=call_model)        # the real director (billed)
    results["opus"] = opus
    rows["opus_director"] = _persist_director_row(
        opus, "opus_director", kind, ab_pair_id, package_id, price_fn, desk)
    if ab_qwen:
        qwen = run_director(package, "local", call_model=call_model)   # free shadow benchmark
        results["qwen"] = qwen
        rows["qwen_director"] = _persist_director_row(
            qwen, "qwen_director", kind, ab_pair_id, package_id, price_fn, desk)

    _notify_director(notifier, kind, ab_pair_id, results, rows)
    try:
        op = (opus.get("play") or {})
        mason_activity.log_activity(
            "opus_director", f"Opus director call ({kind})", actor="opus",
            category="analyst", trigger="autonomous",
            ticker=op.get("ticker"), ref_table="recommendations",
            ref_id=(rows.get("opus_director") or {}).get("rec_id"),
            extra={"ab_pair_id": ab_pair_id, "kind": kind, "ab_qwen": bool(ab_qwen),
                   "opus_ok": opus.get("ok"), "opus_direction": op.get("direction"),
                   "opus_confidence": opus.get("confidence")})
    except Exception:
        pass
    _maybe_set_review_reminder(desk)
    return {"ab_pair_id": ab_pair_id, "kind": kind, "rows": rows,
            "opus": opus, "qwen": results.get("qwen")}


# ── director grading: trading-session math (Agent G, 2026-09-27) ────────────────
# Checkpoint "+Nd" = the official CLOSE of the Nth NYSE session after "session 0"
# (the session whose price is the entry). Keys stay "+1d/+5d/+30d/+90d" but mean
# trading sessions via the session_map {1:1, 5:5, 30:21, 90:63}.

_SESSION_MAP_DEFAULT = {1: 1, 5: 5, 30: 21, 90: 63}
_PAST_NYSE_HOLIDAYS = {"2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17",
                       "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
                       "2025-09-01", "2025-11-27", "2025-12-25",
                       "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                       "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07"}
_GRADE_AFTER_HHMM = (16, 10)          # a session is gradeable at/after 16:10 ET


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


def _nyse_holidays() -> set:
    try:
        import mason_director as _MD
        return set(_MD._NYSE_HOLIDAYS) | _PAST_NYSE_HOLIDAYS
    except Exception:
        return set(_PAST_NYSE_HOLIDAYS)


def _is_session_day(d, holidays=None) -> bool:
    hol = holidays if holidays is not None else _nyse_holidays()
    return d.weekday() < 5 and d.isoformat() not in hol


def _to_et(dt: datetime) -> datetime:
    """Naive timestamps (created_date is datetime.now().isoformat()) are system-local."""
    return (dt if dt.tzinfo else dt.astimezone()).astimezone(_et_tz())


def _session0(created: datetime, holidays=None):
    """Session whose price is the entry: created during RTH (09:30–16:00 ET) on a
    trading day → that day; otherwise the most recent completed session."""
    hol = holidays if holidays is not None else _nyse_holidays()
    et = _to_et(created)
    d = et.date()
    mins = et.hour * 60 + et.minute
    if _is_session_day(d, hol) and mins >= 9 * 60 + 30:
        return d                         # intraday, or after today's close
    d -= timedelta(days=1)
    while not _is_session_day(d, hol):
        d -= timedelta(days=1)
    return d


def _nth_session_after(d0, n: int, holidays=None):
    hol = holidays if holidays is not None else _nyse_holidays()
    d, k = d0, 0
    while k < n:
        d += timedelta(days=1)
        if _is_session_day(d, hol):
            k += 1
    return d


def _session_closed(sess_date, now_et: datetime) -> bool:
    tz = _et_tz()
    gate = datetime(sess_date.year, sess_date.month, sess_date.day,
                    *_GRADE_AFTER_HHMM, tzinfo=tz)
    return now_et >= gate


def _session_map(desk) -> dict:
    raw = _cfg_get(desk, ["opus_director", "grading", "session_map"], None)
    out = dict(_SESSION_MAP_DEFAULT)
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
    return out


def _grading_method(desk) -> str:
    m = str(_cfg_get(desk, ["opus_director", "grading", "method"], "session_close")
            or "session_close").strip().lower()
    return "calendar" if m == "calendar" else "session_close"


def _yf_session_close(ticker: str, sess_date):
    try:
        import yfinance as yf
        import mason_symbols
        h = yf.Ticker(mason_symbols.yf_symbol(ticker)).history(start=sess_date.isoformat(),
                                      end=(sess_date + timedelta(days=4)).isoformat(),
                                      interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None
        for idx, row in h.iterrows():
            if idx.date() == sess_date:
                c = float(row["Close"])
                return c if c > 0 else None
    except Exception as e:
        log.debug("yf session close %s %s failed: %s", ticker, sess_date, e)
    return None


def _alpaca_session_close(ticker: str, sess_date):
    try:
        import requests
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent / "mason.config.json").read_text(encoding="utf-8"))
        import mason_secrets as _S    # B1b: env APCA_* > secrets file > paper_trading
        key, secret = _S.alpaca_keys(cfg=cfg)
        if not key or not secret:
            return None
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         params={"timeframe": "1Day", "start": sess_date.isoformat(),
                                 "end": (sess_date + timedelta(days=1)).isoformat(),
                                 "adjustment": "raw", "feed": "sip", "limit": 5},
                         timeout=8)
        if r.status_code != 200:
            return None
        for b in (r.json() or {}).get("bars") or []:
            if str(b.get("t", ""))[:10] == sess_date.isoformat():
                c = float(b.get("c") or 0)
                return c if c > 0 else None
    except Exception as e:
        log.debug("alpaca session close %s %s failed: %s", ticker, sess_date, e)
    return None


def _default_session_close(ticker: str, sess_date):
    """Official daily close for `sess_date` (yfinance raw daily bar, Alpaca daily
    bar fallback). None if unavailable — caller skips and retries next run."""
    c = _yf_session_close(ticker, sess_date)
    return c if c is not None else _alpaca_session_close(ticker, sess_date)


def _outcome(direction, move, dead):
    if abs(move) <= dead:
        return "neutral"
    if direction == "long":
        return "right" if move > 0 else "wrong"
    return "right" if move < 0 else "wrong"


def _session_grade_row(rec: dict, extra: dict, *, now_et: datetime, close_fn,
                       smap: dict, horizons, dead: float, grades: dict | None = None,
                       holidays=None) -> dict:
    """Pure-ish core shared by the live grader and regrade_director_predictions.py.
    Fills `grades` (a dict of already-valid session_close grades) with every matured
    checkpoint whose close is fetchable. Returns {grades, new_keys, declared_key,
    declared_matured, final}. Never grades off a live price."""
    hol = holidays if holidays is not None else _nyse_holidays()
    grades = dict(grades or {})
    created = datetime.fromisoformat(rec["created_date"])
    s0 = _session0(created, hol)
    direction = rec.get("direction")
    declared = int(extra.get("horizon_days") or 90)
    checkpoints = sorted({int(d) for d in horizons if int(d) <= declared} | {declared})
    entry = rec.get("entry_ref_price")
    try:
        e = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        e = None
    new_keys, closes = [], {}
    for c in checkpoints:
        key = f"+{c}d"
        n = int(smap.get(c, c))
        sd = _nth_session_after(s0, n, hol)
        g = grades.get(key)
        if isinstance(g, dict) and g.get("method") == "session_close" \
                and g.get("session_date") == sd.isoformat():
            continue
        if not _session_closed(sd, now_et):
            grades.pop(key, None)
            continue
        if direction in ("long", "short") and e and e > 0:
            if sd not in closes:
                try:
                    closes[sd] = close_fn(rec["ticker"], sd)
                except Exception:
                    closes[sd] = None
            try:
                p = float(closes[sd]) if closes[sd] is not None else None
            except (TypeError, ValueError):
                p = None
            if not p or p <= 0:
                grades.pop(key, None)       # skip → retry next run, never a bogus grade
                continue
            move = (p - e) / e * 100.0
            grades[key] = {"outcome": _outcome(direction, move, dead), "price": round(p, 4),
                           "move_pct": round(move, 3), "session_date": sd.isoformat(),
                           "sessions": n, "checked_at": now_et.replace(tzinfo=None).isoformat(),
                           "method": "session_close"}
        else:
            grades[key] = {"outcome": None,
                           "note": ("non-directional" if direction not in ("long", "short")
                                    else "no entry price"),
                           "session_date": sd.isoformat(), "sessions": n,
                           "checked_at": now_et.replace(tzinfo=None).isoformat(),
                           "method": "session_close"}
        new_keys.append(key)
    dkey = f"+{declared}d"
    dsd = _nth_session_after(s0, int(smap.get(declared, declared)), hol)
    matured = _session_closed(dsd, now_et)
    final = grades.get(dkey) if matured and dkey in grades else None
    return {"grades": grades, "new_keys": new_keys, "declared_key": dkey,
            "declared_matured": matured, "final": final, "session0": s0.isoformat(),
            "declared_session_date": dsd.isoformat()}


def grade_director_predictions(*, price_fn=None, cfg=None, now=None, close_fn=None,
                               daily_fn=None, minute_fn=None) -> dict:
    """Multi-horizon grader for kind='director' recs. Scores each open director
    prediction at every configured checkpoint (≤ its declared horizon) it has
    reached, recording a snapshot into extra_json.grades["+Nd"]. Closes the row
    (status='graded', final outcome = its declared-horizon grade) only once its
    declared horizon passes. Leaves grade_recommendations' breaking-item recs
    untouched.

    method (analyst_desk.opus_director.grading.method, read per call):
      "session_close" (default) — "+Nd" = official close of the Nth NYSE session
          after session 0 (session_map 1→1, 5→5, 30→21, 90→63), graded only once
          that session has closed (≥16:10 ET). Price via close_fn(ticker, date).
      "calendar" — the old behaviour (calendar days, price_fn(ticker) = now).

    After either method the TRADABLE grade pass runs (grade_director_tradable →
    extra_json.grade_v2, legacy fields untouched). It uses daily_fn / minute_fn; when a
    caller injects price_fn / close_fn but no daily_fn (offline tests) it is skipped so
    it can never reach for live market data behind an injected price source."""
    desk = _desk(cfg)
    v2 = dict(cfg=cfg, now=now, daily_fn=daily_fn, minute_fn=minute_fn,
              skip=daily_fn is None and (price_fn is not None or close_fn is not None))
    if _grading_method(desk) == "calendar":
        return _with_tradable(_grade_director_calendar(price_fn=price_fn, desk=desk, now=now), **v2)
    close_fn = close_fn or _default_session_close
    dead = float(_cfg_get(desk, ["recommendations", "dead_band_pct"], 0.5) or 0.5)
    horizons = _cfg_get(desk, ["opus_director", "horizons_days"], [1, 5, 30, 90]) \
        or [1, 5, 30, 90]
    smap = _session_map(desk)
    now_ts = now if now is not None else __import__("time").time()
    now_et = datetime.fromtimestamp(now_ts, _et_tz())
    now_local = datetime.fromtimestamp(now_ts)
    hol = _nyse_holidays()

    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendations WHERE kind='director' AND status='open' "
            "AND COALESCE(record_deleted,0)=0").fetchall()]
    finally:
        conn.close()

    scored = closed = 0
    for rec in rows:
        try:
            extra = json.loads(rec.get("extra_json") or "{}")
        except Exception:
            extra = {}
        old = extra.get("grades") or {}
        keep, legacy_moved = {}, False
        for k, g in old.items():                 # calendar-era grades → grades_legacy
            if isinstance(g, dict) and g.get("method") == "session_close":
                keep[k] = g
            else:
                leg = extra.setdefault("grades_legacy", {})
                if k not in leg:
                    leg[k] = g
                legacy_moved = True
        try:
            res = _session_grade_row(rec, extra, now_et=now_et, close_fn=close_fn,
                                     smap=smap, horizons=horizons, dead=dead,
                                     grades=keep, holidays=hol)
        except Exception as ex:
            log.debug("director grade %s skipped: %s", rec.get("id"), ex)
            continue
        grades = res["grades"]
        scored += sum(1 for k in res["new_keys"] if grades[k].get("outcome") is not None)
        do_close = res["final"] is not None
        if not (res["new_keys"] or do_close or legacy_moved or grades != keep):
            continue
        extra["grades"] = grades
        conn = mason_db.get_connection()
        try:
            with conn:
                if do_close:
                    conn.execute(
                        "UPDATE recommendations SET extra_json=?, status='graded', "
                        "outcome=?, eval_price=?, outcome_checked_date=? WHERE id=?",
                        (json.dumps(extra, default=str), res["final"].get("outcome"),
                         res["final"].get("price"), now_local.isoformat(), rec["id"]))
                else:
                    conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                                 (json.dumps(extra, default=str), rec["id"]))
        finally:
            conn.close()
        if do_close:
            closed += 1
    if scored or closed:
        try:
            mason_activity.log_activity(
                "director_graded",
                f"Director grades: {scored} checkpoint snapshot(s), {closed} closed",
                actor="system", category="analyst", trigger="autonomous",
                extra={"scored": scored, "closed": closed, "considered": len(rows),
                       "method": "session_close"})
        except Exception:
            pass
    return _with_tradable({"scored": scored, "closed": closed, "considered": len(rows),
                           "method": "session_close"}, **v2)


def _grade_director_calendar(*, price_fn=None, desk=None, now=None) -> dict:
    """Legacy calendar-day grader (rollback: grading.method="calendar")."""
    price_fn = price_fn or _default_last_price
    dead = float(_cfg_get(desk, ["recommendations", "dead_band_pct"], 0.5) or 0.5)
    horizons = _cfg_get(desk, ["opus_director", "horizons_days"], [1, 5, 30, 90]) \
        or [1, 5, 30, 90]
    now_ts = now if now is not None else __import__("time").time()
    now_dt = datetime.fromtimestamp(now_ts)

    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendations WHERE kind='director' AND status='open' "
            "AND COALESCE(record_deleted,0)=0").fetchall()]
    finally:
        conn.close()

    scored = closed = 0
    for rec in rows:
        try:
            extra = json.loads(rec.get("extra_json") or "{}")
        except Exception:
            extra = {}
        try:
            created = datetime.fromisoformat(rec["created_date"])
        except Exception:
            continue
        direction = rec.get("direction")
        declared = int(extra.get("horizon_days") or 90)
        grades = extra.get("grades") or {}
        age_days = (now_dt - created).total_seconds() / 86400.0
        entry = rec.get("entry_ref_price")
        dirty = False
        for c in sorted(d for d in horizons if d <= declared):
            key = f"+{c}d"
            if key in grades or age_days < c:
                continue
            if direction in ("long", "short") and entry:
                price = price_fn(rec["ticker"])
                try:
                    e = float(entry); p = float(price)
                except (TypeError, ValueError):
                    continue
                if e <= 0 or p <= 0:
                    continue
                move = (p - e) / e * 100.0
                if abs(move) <= dead:
                    outcome = "neutral"
                elif direction == "long":
                    outcome = "right" if move > 0 else "wrong"
                else:
                    outcome = "right" if move < 0 else "wrong"
                grades[key] = {"outcome": outcome, "price": round(p, 4),
                               "move_pct": round(move, 3), "checked_at": now_dt.isoformat()}
                scored += 1
            else:
                grades[key] = {"outcome": None, "note": "non-directional",
                               "checked_at": now_dt.isoformat()}
            dirty = True
        do_close = age_days >= declared
        if dirty or do_close:
            extra["grades"] = grades
            final = (grades.get(f"+{declared}d") or {}).get("outcome") if do_close else None
            eval_price = (grades.get(f"+{declared}d") or {}).get("price") if do_close else None
            conn = mason_db.get_connection()
            try:
                with conn:
                    if do_close:
                        conn.execute(
                            "UPDATE recommendations SET extra_json=?, status='graded', "
                            "outcome=?, eval_price=?, outcome_checked_date=? WHERE id=?",
                            (json.dumps(extra, default=str), final, eval_price,
                             now_dt.isoformat(), rec["id"]))
                    else:
                        conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                                     (json.dumps(extra, default=str), rec["id"]))
            finally:
                conn.close()
            if do_close:
                closed += 1
    if scored or closed:
        try:
            mason_activity.log_activity(
                "director_graded",
                f"Director grades: {scored} checkpoint snapshot(s), {closed} closed",
                actor="system", category="analyst", trigger="autonomous",
                extra={"scored": scored, "closed": closed, "considered": len(rows)})
        except Exception:
            pass
    return {"scored": scored, "closed": closed, "considered": len(rows)}


# ── director grading v2: the TRADABLE grade (Fable Step 1 — audit L1, L10) ───────
# The grade above answers "did the underlying close the right way from the reference
# price". For a call made before the open that reference is the PRIOR close, so the
# call is credited with an overnight gap it had already seen. The tradable grade
# answers "what would the trade have made from the first price Mason could have hit":
#   entry   pre-open / after-close / weekend call → that session's price at the open
#           plus `entry_delay_min` (09:35) when minute bars exist, else the session open;
#           intraday call → the price at decision time (the stamped quote, checked
#           against the minute bar).
#   limit   a stock call with an entry limit only counts as entered if the limit was
#           reachable while the order would have been live; otherwise "not_entered".
#   exits   stop / target touched before the horizon end the trade there (stop first
#           when one daily bar holds both; a gap through a level fills at the open).
#   result  gross and net (round-trip cost) return, SPY over the same window, excess.
# It is stored ALONGSIDE the legacy grade in extra_json.grade_v2. This code never
# changes the outcome column, entry_ref_price or extra_json.grades.

_NYSE_HALF_DAYS = {"2025-07-03", "2025-11-28", "2025-12-24",
                   "2026-11-27", "2026-12-24", "2027-11-26"}      # 13:00 ET closes
_GRADE_V2 = 2
_V2_FINAL = ("graded", "not_entered")


def _v2_storable(g: dict) -> bool:
    """A result worth storing: final (graded / not entered), ungraded with its reason,
    or still running but already ENTERED (so an open call carries its tradable entry)."""
    st = (g or {}).get("status")
    return st in _V2_FINAL or st == "ungraded" or (st == "pending" and bool(g.get("entered")))


def _fpos(v):
    try:
        f = float(v)
        return f if f == f and f > 0 else None
    except (TypeError, ValueError):
        return None


def _tradable_opts(desk=None) -> dict:
    """Settings for the tradable grade (analyst_desk.opus_director.grading.tradable,
    read per call). Every key is optional; an absent key means the default."""
    raw = _cfg_get(desk or {}, ["opus_director", "grading", "tradable"], None)
    raw = raw if isinstance(raw, dict) else {}

    def num(key, default, lo=0.0):
        try:
            v = float(raw.get(key, default))
            return v if v == v and v >= lo else default
        except (TypeError, ValueError):
            return default
    half = set(_NYSE_HALF_DAYS)
    if isinstance(raw.get("half_days"), list):
        half |= {str(d)[:10] for d in raw["half_days"]}
    return {"enabled": raw.get("enabled", True) is not False,
            "entry_delay_min": num("entry_delay_min", 5.0),
            "entry_valid_sessions": int(num("entry_valid_sessions", 3, 1)),
            "cost_pct_stock": num("cost_pct_stock", 0.10),
            "quote_tolerance_pct": num("quote_tolerance_pct", 1.0),
            "lookback_days": int(num("lookback_days", 150, 1)),
            "retry_ungraded_hours": num("retry_ungraded_hours", 6.0),
            "max_rows_per_run": int(num("max_rows_per_run", 40, 1)),
            "benchmark": str(raw.get("benchmark") or "SPY").upper(),
            "half_days": half,
            "dead": float(_cfg_get(desk or {}, ["recommendations", "dead_band_pct"], 0.5) or 0.5),
            "smap": _session_map(desk or {})}


def _close_minutes(d, half_days) -> int:
    return 13 * 60 if d.isoformat() in (half_days or ()) else 16 * 60


def _entry_session(created: datetime, holidays=None, half_days=None):
    """(session date, kind) of the first session Mason could trade a call in.
    kind: pre_open (before 09:30 on a session day) · intraday (09:30 → the close) ·
    after_close · non_session (weekend / holiday). The last two map to the NEXT session."""
    hol = holidays if holidays is not None else _nyse_holidays()
    half = half_days if half_days is not None else _NYSE_HALF_DAYS
    et = _to_et(created)
    d = et.date()
    mins = et.hour * 60 + et.minute
    if _is_session_day(d, hol):
        if mins < 9 * 60 + 30:
            return d, "pre_open"
        if mins < _close_minutes(d, half):
            return d, "intraday"
        kind = "after_close"
    else:
        kind = "non_session"
    d += timedelta(days=1)
    while not _is_session_day(d, hol):
        d += timedelta(days=1)
    return d, kind


def _ohlc(o, h, l, c):
    o, h, l, c = _fpos(o), _fpos(h), _fpos(l), _fpos(c)
    if None in (o, h, l, c):
        return None
    return {"o": o, "h": max(h, o, c), "l": min(l, o, c), "c": c}


def _default_minute_bars(ticker: str, sess_date, conn=None) -> list:
    """Regular-session minute bars Mason already holds for one day (watch_bars; 1m when
    it is about as complete as the 5m series, else 5m), oldest first:
    [{ts (UTC epoch of bar start), o, h, l, c, sec}]. [] when there are none. Read-only.
    `conn` lets the re-grade script pass its own read-only handle (left open)."""
    tz = _et_tz()
    t0 = int(datetime(sess_date.year, sess_date.month, sess_date.day, 9, 30, tzinfo=tz).timestamp())
    try:
        own = conn is None
        conn = mason_db.get_connection() if own else conn
        try:
            rows = conn.execute(
                "SELECT tf, ts, o, h, l, c FROM watch_bars WHERE ticker=? AND tf IN ('1m','5m') "
                "AND ts>=? AND ts<? ORDER BY ts", (str(ticker).upper(), t0, t0 + 390 * 60)).fetchall()
        finally:
            if own:
                conn.close()
    except Exception:
        return []
    by = {"1m": [], "5m": []}
    for r in rows:
        b = _ohlc(r["o"], r["h"], r["l"], r["c"])
        if b and r["tf"] in by:
            by[r["tf"]].append(dict(b, ts=int(r["ts"]), sec=60 if r["tf"] == "1m" else 300))
    one, five = by["1m"], by["5m"]
    return one if one and len(one) >= 4 * len(five) else (five or one)


def _yf_daily_bars(ticker: str, start, end) -> dict:
    out = {}
    try:
        import yfinance as yf
        import mason_symbols
        h = yf.Ticker(mason_symbols.yf_symbol(ticker)).history(start=start.isoformat(),
                                      end=(end + timedelta(days=1)).isoformat(),
                                      interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return out
        for idx, row in h.iterrows():
            b = _ohlc(row["Open"], row["High"], row["Low"], row["Close"])
            if b:
                out[idx.date().isoformat()] = b
    except Exception as e:
        log.debug("yf daily bars %s %s..%s failed: %s", ticker, start, end, e)
    return out


def _default_daily_bars(ticker: str, start, end) -> dict:
    """{YYYY-MM-DD: {o, h, l, c}} raw daily bars for [start, end]: the cross-checked
    price cache first, yfinance for any session the cache does not hold (it never
    serves today's bar). {} when nothing is available — the caller leaves the call
    ungraded and retries later."""
    out = {}
    try:
        import mason_price_cache as PC
        for r in PC.get_daily(ticker, start, end) or []:
            b = _ohlc(r.get("open"), r.get("high"), r.get("low"), r.get("close"))
            if b:
                out[str(r.get("date"))[:10]] = b
    except Exception as e:
        log.debug("price cache daily %s failed: %s", ticker, e)
    hol = _nyse_holidays()
    d = start
    while d <= end:
        if _is_session_day(d, hol) and d.isoformat() not in out:
            for k, b in _yf_daily_bars(ticker, start, end).items():
                if start.isoformat() <= k <= end.isoformat():
                    out.setdefault(k, b)
            break
        d += timedelta(days=1)
    return out


def _px_at(bars: list, t0: int, max_wait_s: int = 600):
    """(open, ts) of the first bar starting at/after t0 — the first print Mason could
    have hit — or (None, None) if none starts within max_wait_s."""
    for b in bars:
        if b["ts"] >= t0:
            return (b["o"], b["ts"]) if b["ts"] < t0 + max_wait_s else (None, None)
    return None, None


def _covers(post: list, t0: int, t_close: int) -> bool:
    """True when the minute bars after t0 are complete enough (≥ half the expected
    count) to trust their high/low for the rest of the session."""
    if not post:
        return False
    return len(post) >= 0.5 * max(1, (t_close - t0) // post[0]["sec"])


def _tradable_grade_row(rec: dict, extra: dict, *, now_et: datetime, daily_fn,
                        minute_fn=None, opts: dict | None = None, holidays=None) -> dict:
    """Pure core of the tradable grade for one director call (shared by the live
    grader and regrade_director_predictions.py). Never writes, never guesses.
    Returns a dict with grade_version=2 and status:
      graded        entered and exited — carries entry/exit prices, gross + net return,
                    SPY over the same window, excess, tradable_outcome
      not_entered   the entry limit was never reachable (or the call was already
                    through its stop / at its target at the first tradable moment)
      pending       not decidable yet (a needed session has not closed)
      ungraded      a needed price is missing — `reason` says which
      not_applicable  not a long/short call"""
    o = opts or _tradable_opts({})
    hol = holidays if holidays is not None else _nyse_holidays()
    direction = rec.get("direction")
    if direction not in ("long", "short"):
        return {"grade_version": _GRADE_V2, "status": "not_applicable"}
    long_ = direction == "long"
    sign = 1.0 if long_ else -1.0
    ticker = str(rec.get("ticker") or "").upper()
    created = datetime.fromisoformat(rec["created_date"])
    et = _to_et(created)
    es, kind = _entry_session(created, hol, o["half_days"])
    declared = int(extra.get("horizon_days") or 90)
    xd = _nth_session_after(_session0(created, hol), int(o["smap"].get(declared, declared)), hol)
    if xd < es:
        xd = es
    out = {"grade_version": _GRADE_V2, "entry_kind": kind, "entry_session": es.isoformat(),
           "horizon_session": xd.isoformat()}

    def done(status, reason=None, **kw):
        out.update(kw)
        out["status"] = status
        if reason:
            out["reason"] = reason
        return out

    if not _session_closed(es, now_et):
        return done("pending", "the entry session has not finished")
    sessions = [es]
    while sessions[-1] < xd:
        sessions.append(_nth_session_after(sessions[-1], 1, hol))
    closed = {d for d in sessions if _session_closed(d, now_et)}
    try:
        bars = daily_fn(ticker, es, max(closed)) or {}
    except Exception:
        bars = {}

    def bar(d):
        b = bars.get(d.isoformat())
        return b if isinstance(b, dict) and all(_fpos(b.get(k)) for k in "ohlc") else None

    def no_bar(d):
        return done("ungraded", f"no daily price bar for {ticker} on {d.isoformat()}")

    b0 = bar(es)
    if not b0:
        return no_bar(es)

    # ── the first tradable price ──
    tz = _et_tz()
    open_ts = int(datetime(es.year, es.month, es.day, 9, 30, tzinfo=tz).timestamp())
    close_ts = open_ts + (_close_minutes(es, o["half_days"]) - 570) * 60
    delay = float(o["entry_delay_min"])
    t0 = int(et.timestamp()) if kind == "intraday" else open_ts + int(delay * 60)
    try:
        mins = list(minute_fn(ticker, es) or []) if minute_fn else []
    except Exception:
        mins = []
    post = [m for m in mins if t0 <= m["ts"] < close_ts]
    mpx, _ = _px_at(post, t0)
    have_min = _covers(post, t0, close_ts)
    if kind == "intraday":
        ref = _fpos(rec.get("entry_ref_price"))
        if ref and mpx and abs(ref - mpx) / mpx * 100.0 > o["quote_tolerance_pct"]:
            p0, rule = mpx, "decision_minute"      # the stamped quote disagrees with the tape
            out["entry_ref_mismatch_pct"] = round((ref - mpx) / mpx * 100.0, 3)
        elif ref and (mpx or b0["l"] <= ref <= b0["h"]):
            p0, rule = ref, "decision_quote"
        elif mpx:
            p0, rule = mpx, "decision_minute"
        else:
            return done("ungraded", "no usable price at the time of the call")
    elif mpx:
        hh, mm = divmod(570 + int(delay), 60)
        p0, rule = mpx, f"minute_{hh:02d}{mm:02d}"
    else:
        p0, rule = b0["o"], "session_open"
    # Daily bars are re-based after a split; minute bars and the stamped quote are not. A price
    # well outside the entry day's own range means the two disagree: never grade on it.
    try:
        if not (b0["l"] * 0.98 <= p0 <= b0["h"] * 1.02):
            return done("ungraded", "entry price outside the day's range (split?)")
    except Exception:
        return done("ungraded", "entry day's price range is unusable")
    out.update({"first_tradable_px": round(p0, 4), "first_tradable_rule": rule})

    # ── levels (stock only: an option call's entry/stop/target are premium prices) ──
    tr = extra.get("trade") if isinstance(extra.get("trade"), dict) else {}
    veh = str(tr.get("vehicle") or "stock").lower()
    is_opt = veh in ("call", "put")
    L = S = T = None
    if is_opt:
        out["option_vehicle"] = True
        out["vehicle_note"] = ("graded on the underlying stock: this is not the option's profit "
                               "or loss, and the option's entry/stop/target premiums are not applied")
    else:
        L, S, T = _fpos(tr.get("entry_limit")), _fpos(tr.get("stop")), _fpos(tr.get("target"))
    out.update({"vehicle": veh if is_opt else "stock", "entry_limit": L, "stop": S, "target": T})

    def past_stop(px):
        return S is not None and sign * (px - S) <= 0

    def at_target(px):
        return T is not None and sign * (px - T) >= 0

    def limit_ok(px):                         # a price at which the limit order fills
        return sign * (px - L) <= 0

    def adv(b):                               # the bar's worst price for this direction
        return b["l"] if long_ else b["h"]

    def fav(b):
        return b["h"] if long_ else b["l"]

    def not_entered(reason, **kw):
        return done("not_entered", reason, entered=False, **kw)

    if S is not None and T is not None and sign * (T - S) <= 0:
        return not_entered("the stop and the target are on the wrong sides of each other")
    if past_stop(p0):
        return not_entered("the price was already through the stop at the first tradable moment")
    if at_target(p0):
        return not_entered("the price was already at the target at the first tradable moment")

    # ── entry: at the market, or a resting limit that has to be reached ──
    fill_px = fill_day = fill_how = fill_ts = mode = None
    after = post
    if L is None or limit_ok(p0):
        fill_px, fill_day, fill_how, fill_ts = p0, es, rule, t0
        mode = "minute" if have_min else ("full" if kind != "intraday" else "close_only")
    else:
        if S is not None and sign * (L - S) <= 0:
            return not_entered("the entry limit sits beyond the stop")
        valid = o["entry_valid_sessions"] if (S is not None and T is not None) else 1
        window = sessions[:max(1, valid)]
        for i, d in enumerate(window):
            if d not in closed:
                return done("pending", "the entry limit is still working")
            b = bar(d)
            if not b:
                return no_bar(d)
            if i == 0 and have_min:
                hit = next((k for k, m in enumerate(post) if limit_ok(adv(m))), None)
                if hit is not None:
                    fill_px, fill_how, fill_ts, mode, after = L, "limit", post[hit]["ts"], "minute", post[hit:]
            elif i == 0 and kind == "intraday":
                if limit_ok(b["c"]):                 # it closed through the limit: reached after the call
                    fill_px, fill_how, mode = L, "limit", "after_limit"
                elif limit_ok(adv(b)):               # the day's range reached it, but maybe before the call
                    return done("ungraded", "no minute data to tell whether the entry limit "
                                            "was reached after the call")
            elif i == 0:
                if limit_ok(adv(b)):
                    fill_px, fill_how, mode = L, "limit", "after_limit"
            elif limit_ok(b["o"]):                   # gapped through the limit: filled at the open
                fill_px, fill_how, mode = b["o"], "limit_gap", "full"
            elif limit_ok(adv(b)):
                fill_px, fill_how, mode = L, "limit", "after_limit"
            if fill_px is not None:
                fill_day = d
                break
        if fill_px is None:
            return not_entered("the entry limit was never reached while the order would have been live",
                               entry_window_end=window[-1].isoformat())

    # ── exit: stop / target if touched, else the horizon close ──
    exit_px = exit_rule = exit_day = exit_ts = None
    b = bar(fill_day)
    if mode == "minute":
        for k, m in enumerate(after):
            if past_stop(adv(m)):
                exit_px, exit_rule, exit_ts = S, "stop", m["ts"]
            elif not (k == 0 and fill_how == "limit") and at_target(fav(m)):
                exit_px, exit_rule, exit_ts = T, "target", m["ts"]
            if exit_px is not None:
                break
    elif mode == "full":                      # the whole daily bar came after the fill
        if past_stop(fill_px):
            exit_px, exit_rule = fill_px, "stop_gap"
        elif past_stop(adv(b)):
            exit_px, exit_rule = S, "stop"
        elif at_target(fav(b)):
            exit_px, exit_rule = T, "target"
    elif mode == "after_limit":               # filled at the limit at an unknown time that day
        if past_stop(adv(b)):                 # certain: the stop lies beyond the limit
            exit_px, exit_rule = S, "stop"
        elif at_target(b["c"]):               # certain only if it closed there
            exit_px, exit_rule = T, "target"
    else:                                     # intraday call, no minute data: only what the close proves
        out["entry_day_range_unknown"] = True
        if past_stop(b["c"]):
            exit_px, exit_rule = S, "stop"
        elif at_target(b["c"]):
            exit_px, exit_rule = T, "target"
    if exit_px is not None:
        exit_day = fill_day
    entry_info = {"entered": True, "tradable_entry_px": round(fill_px, 4),
                  "tradable_entry_rule": fill_how, "fill_session": fill_day.isoformat()}
    if exit_px is None:
        for d in sessions[sessions.index(fill_day) + 1:]:
            if d not in closed:
                return done("pending", "the horizon has not been reached", **entry_info)
            b = bar(d)
            if not b:
                return no_bar(d)
            if past_stop(b["o"]):
                exit_px, exit_rule = b["o"], "stop_gap"
            elif at_target(b["o"]):
                exit_px, exit_rule = b["o"], "target_gap"
            elif past_stop(adv(b)):
                exit_px, exit_rule = S, "stop"
            elif at_target(fav(b)):
                exit_px, exit_rule = T, "target"
            if exit_px is not None:
                exit_day = d
                break
    if exit_px is None:
        if xd not in closed:
            return done("pending", "the horizon has not been reached", **entry_info)
        exit_px, exit_rule, exit_day = bar(xd)["c"], "horizon_close", xd

    gross = sign * (exit_px - fill_px) / fill_px * 100.0
    cost = float(o["cost_pct_stock"])
    net = gross - cost

    # ── the benchmark over the same window ──
    bench = o["benchmark"]
    spy_ret = spy_note = None
    try:
        bb = daily_fn(bench, es, exit_day) or {}
        se = sx = None
        if fill_day == es:
            try:
                bm = [m for m in (minute_fn(bench, es) or [])] if minute_fn else []
            except Exception:
                bm = []
            se, _ = _px_at([m for m in bm if m["ts"] < close_ts], fill_ts or t0)
            if se is None and kind != "intraday":
                se = (bb.get(es.isoformat()) or {}).get("o")
            if se is not None and exit_ts is not None:
                sx, _ = _px_at([m for m in bm if m["ts"] < close_ts], exit_ts)
        else:
            se = (bb.get(fill_day.isoformat()) or {}).get("o")
        if sx is None:
            sx = (bb.get(exit_day.isoformat()) or {}).get("o" if exit_rule.endswith("_gap") else "c")
        se, sx = _fpos(se), _fpos(sx)
        if se and sx:
            spy_ret = (sx - se) / se * 100.0
        else:
            spy_note = f"no {bench} price for the same window"
    except Exception as e:
        spy_note = f"{bench} unavailable: {e}"
    res = dict(entry_info, exit_rule=exit_rule, exit_px=round(exit_px, 4),
               exit_session=exit_day.isoformat(),
               tradable_return_pct=round(gross, 3), cost_pct=round(cost, 3),
               tradable_return_net_pct=round(net, 3),
               tradable_outcome=_outcome("long", net, o["dead"]), benchmark=bench,
               spy_return_pct=None if spy_ret is None else round(spy_ret, 3),
               excess_return_pct=None if spy_ret is None else round(gross - sign * spy_ret, 3),
               excess_return_net_pct=None if spy_ret is None else round(net - sign * spy_ret, 3))
    if spy_note:
        res["benchmark_note"] = spy_note
    return done("graded", **res)


def tradable_verdict(extra: dict):
    """How one call's tradable grade counts in hit-rate statistics — the ONE rule every
    reader uses (the director scorecard, director_call_verdicts):
      ("count", outcome, net_return_pct)   graded: it counts, with the tradable outcome
      ("skip", None, None)                 it has a grade_v2 but was never entered, could
                                           not be graded or is still running: left out
      None                                 no grade_v2 at all: the caller falls back to
                                           the legacy outcome column"""
    g = (extra or {}).get("grade_v2")
    if not isinstance(g, dict) or g.get("grade_version") != _GRADE_V2:
        return None
    if g.get("status") == "graded" and g.get("tradable_outcome") in ("right", "wrong", "neutral"):
        return "count", g["tradable_outcome"], g.get("tradable_return_net_pct")
    return "skip", None, None


def director_call_verdicts(source: str | None = None, cfg=None, created_by: str = "opus_director") -> list:
    """Final verdicts of the director's long/short calls, for anything that computes a
    hit rate or a calibration base rate from them (mason_agent_loop.base_rate /
    _graded_calls). One dict per counted call:
      {id, outcome ('right'|'wrong'|'neutral'), confidence, horizon, vehicle,
       return_pct, excess_pct, source ('tradable'|'legacy')}
    source "tradable" (the default; analyst_desk.opus_director.grading.scorecard_source,
    read per call) uses extra_json.grade_v2 — never-entered / ungraded / still-running
    calls are left out, a call with no grade_v2 yet falls back to its legacy outcome.
    source "legacy" reads the outcome column exactly as before. Read-only."""
    if source is None:
        source = str(_cfg_get(_desk(cfg), ["opus_director", "grading", "scorecard_source"], "tradable")
                     or "tradable").strip().lower()
    source = "legacy" if source == "legacy" else "tradable"
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, status, outcome, direction, entry_ref_price, eval_price, extra_json "
            "FROM recommendations WHERE kind='director' AND created_by=? "
            "AND direction IN ('long','short') AND COALESCE(record_deleted,0)=0 "
            "ORDER BY created_date", (created_by,)).fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ex = json.loads(r.get("extra_json") or "{}")
        except Exception:
            ex = {}
        tr = ex.get("trade") if isinstance(ex.get("trade"), dict) else {}
        item = {"id": r["id"], "confidence": ex.get("confidence"), "horizon": ex.get("horizon"),
                "vehicle": str(tr.get("vehicle") or "stock").lower()}
        v = tradable_verdict(ex) if source == "tradable" else None
        if v is not None:
            if v[0] == "count":
                g = ex["grade_v2"]
                out.append(dict(item, outcome=v[1], return_pct=v[2],
                                excess_pct=g.get("excess_return_net_pct"), source="tradable"))
            continue
        if r.get("status") == "graded" and r.get("outcome") in ("right", "wrong", "neutral"):
            e, x = _fpos(r.get("entry_ref_price")), _fpos(r.get("eval_price"))
            mv = ((x - e) / e * 100.0 * (1.0 if r["direction"] == "long" else -1.0)) if e and x else None
            out.append(dict(item, outcome=r["outcome"], return_pct=None if mv is None else round(mv, 3),
                            excess_pct=None, source="legacy"))
    return out


def _same_grade(a, b) -> bool:
    strip = lambda g: {k: v for k, v in (g or {}).items() if k != "checked_at"}
    return isinstance(a, dict) and isinstance(b, dict) and strip(a) == strip(b)


def grade_director_tradable(*, cfg=None, now=None, daily_fn=None, minute_fn=None,
                            force: bool = False) -> dict:
    """Live pass of the tradable grade: every long/short director call inside the
    look-back that has no final grade_v2 yet gets one as soon as it can be decided
    (a stop-out or a missed entry is final before the horizon); a call that is
    entered but still running is stored as "pending" with its entry. Writes ONLY
    extra_json.grade_v2. Switch: analyst_desk.opus_director.grading.tradable.enabled
    (on when absent)."""
    desk = _desk(cfg)
    o = _tradable_opts(desk)
    if not o["enabled"]:
        return {"enabled": False}
    daily_fn = daily_fn or _default_daily_bars
    minute_fn = minute_fn or _default_minute_bars
    now_ts = now if now is not None else __import__("time").time()
    now_et = datetime.fromtimestamp(now_ts, _et_tz())
    now_local = datetime.fromtimestamp(now_ts)
    hol = _nyse_holidays()
    cutoff = (now_local - timedelta(days=o["lookback_days"])).isoformat()
    memo: dict = {}

    def _daily(t, s, e):
        if (t, s, e) not in memo:
            memo[(t, s, e)] = daily_fn(t, s, e)
        return memo[(t, s, e)]

    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_date, ticker, direction, entry_ref_price, extra_json "
            "FROM recommendations WHERE kind='director' AND direction IN ('long','short') "
            "AND status IN ('open','graded') AND COALESCE(record_deleted,0)=0 "
            "AND created_date>=? ORDER BY created_date DESC", (cutoff,)).fetchall()]
    finally:
        conn.close()
    stats = {"enabled": True, "considered": 0, "graded": 0, "not_entered": 0, "ungraded": 0,
             "pending": 0}
    for rec in rows:
        try:
            extra = json.loads(rec.get("extra_json") or "{}")
        except Exception:
            extra = {}
        cur = extra.get("grade_v2")
        if not force and isinstance(cur, dict) and cur.get("grade_version") == _GRADE_V2 \
                and cur.get("status") in _V2_FINAL:
            continue
        retry = isinstance(cur, dict) and cur.get("status") == "ungraded"
        if retry and not force:                # a missing price is re-tried a few times a day, not every run
            try:
                age_h = (now_local - datetime.fromisoformat(str(cur.get("checked_at")))).total_seconds() / 3600.0
            except (TypeError, ValueError):
                age_h = None
            if age_h is not None and 0 <= age_h < o["retry_ungraded_hours"]:
                continue
        if stats["considered"] >= o["max_rows_per_run"]:
            break
        stats["considered"] += 1
        try:
            res = _tradable_grade_row(rec, extra, now_et=now_et, daily_fn=_daily,
                                      minute_fn=minute_fn, opts=o, holidays=hol)
        except Exception as ex:
            log.debug("director tradable grade %s skipped: %s", rec.get("id"), ex)
            continue
        same = _same_grade(cur, res)
        if not _v2_storable(res) or (same and not retry):
            continue
        res["checked_at"] = now_local.isoformat(timespec="seconds")
        if _write_grade_v2(rec["id"], res) and not same:
            stats[res["status"]] += 1
    if stats["graded"] or stats["not_entered"]:
        try:
            mason_activity.log_activity(
                "director_graded_v2",
                f"Director tradable grades: {stats['graded']} graded, "
                f"{stats['not_entered']} not entered",
                actor="system", category="analyst", trigger="autonomous", extra=stats)
        except Exception:
            pass
    return stats


def _write_grade_v2(rec_id: str, grade: dict) -> bool:
    """Set extra_json.grade_v2 on one row, re-reading the row first so nothing else
    in extra_json (execution state, director updates) is overwritten."""
    conn = mason_db.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")      # take the write lock BEFORE reading: no lost update
        r = conn.execute("SELECT extra_json FROM recommendations WHERE id=? AND kind='director'",
                         (rec_id,)).fetchone()
        if not r:
            conn.rollback()
            return False
        try:
            ex = json.loads(r["extra_json"] or "{}")
        except Exception:
            ex = {}
        ex["grade_v2"] = grade
        conn.execute("UPDATE recommendations SET extra_json=? WHERE id=?",
                     (json.dumps(ex, default=str), rec_id))
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.debug("director tradable grade %s not stored: %s", rec_id, e)
        return False
    finally:
        conn.close()


def _with_tradable(res: dict, *, cfg=None, now=None, daily_fn=None, minute_fn=None,
                   skip: bool = False) -> dict:
    """Run the tradable pass after the legacy grader and attach its counts. It can
    never break the legacy result: any failure is reported under tradable_error."""
    if skip:
        return res
    try:
        res["tradable"] = grade_director_tradable(cfg=cfg, now=now, daily_fn=daily_fn,
                                                  minute_fn=minute_fn)
    except Exception as e:
        res["tradable_error"] = str(e)
    return res
