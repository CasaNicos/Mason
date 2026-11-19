"""
mason_analyst_desk.py — the 24/7 qwen Analyst Desk (Phase 2).

ANALYST_DESK_PHASE2_PROMPT.md. Phase 1 gave us the tables, the durable inbox
(mason_notifications) and the source-health monitor. Phase 2 is the qwen analyst
layer end-to-end:

  * a delta-driven **analyst scan** that reads only NEW material news events
    (past a watermark), analyzes them per-ticker with ONE batched qwen call each,
    and writes `qwen_news_analysis` rows;
  * a **breaking fast-path** (materiality ≥ 0.8) that jumps the queue and drops a
    `huge_event` notification;
  * five read-only **analyst context builders** (news / sentiment / analytics /
    fundamentals / positioning) that each degrade to "unavailable" independently;
  * a consolidated **Desk Report** (`build_desk_report`) posted to the inbox as a
    `completed_analysis` notification — a light rolling one hourly, the full one at
    the 07:00 / 13:00 ET anchors;
  * a single-worker priority queue + an ET scheduler, both crash-safe daemons.

NO Opus at runtime, NO debate/recommendations/orders (those are Phase 3). Every
qwen call is `mason_llm.complete(intent="chore")` — local qwen only.

GPU reality (one RTX 2060, OLLAMA_NUM_PARALLEL=1 → one qwen call at a time): the
desk **yields to the interactive chat**. Before every qwen dispatch it checks
`mason_llm.interactive_busy()` and defers while the user is streaming. There is
ever only ONE desk worker.

Design for testability: every external effect — the LLM call, the busy gate, the
clock, sleep, the notifier, and each data source — is an injectable parameter with
a real default, so the whole module runs offline and deterministically (see
test_mason_analyst_desk.py), mirroring mason_debate.py / mason_order_builder.py.
"""

from __future__ import annotations

import json
import queue
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import mason_db
import mason_notifications
import mason_activity

log = logging.getLogger("mason.analyst_desk")

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    _ET = None

_MODEL_NOTE = "qwen (analyst desk)"                 # stamped on qwen_news_analysis.model


# ── config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        import mason_app
        return mason_app._load_cfg()
    except Exception:
        try:
            from pathlib import Path
            return json.loads((Path(__file__).parent / "mason.config.json").read_text(encoding="utf-8"))
        except Exception:
            return {}


def _desk_cfg(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else _load_config()
    d = cfg.get("analyst_desk") if isinstance(cfg, dict) else None
    return d if isinstance(d, dict) else {}


def _cfg_get(cfg, path, default):
    """Nested get with a safe default, e.g. _cfg_get(desk, ['scan','max_tickers_per_cycle'], 8)."""
    cur = cfg or {}
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


# ── desk_state key/value store ──────────────────────────────────────────────────

def get_state(key: str, default=None):
    conn = mason_db.get_connection()
    try:
        row = conn.execute("SELECT value FROM desk_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_state(key: str, value) -> None:
    conn = mason_db.get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                (key, str(value), datetime.now().isoformat()),
            )
    finally:
        conn.close()


def _get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_state(key, default) or default)
    except (TypeError, ValueError):
        return default


# ── time helpers (ET) ─────────────────────────────────────────────────────────

def _now_et(now: float | None = None) -> datetime:
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.astimezone(_ET) if _ET else dt


def _now_iso() -> str:
    return datetime.now().isoformat()


# ── Phase 4 fact-check stats (for GET /api/factcheck/stats) ─────────────────────

def factcheck_stats(last_n: int = 15) -> dict:
    """Rolling hallucination-rate counters (today + all-time) from desk_state, plus
    the last-N dropped claims pulled from recent qwen_news_analysis audits. Read-
    only, additive; never raises."""
    try:
        import mason_factcheck as FC
        metrics = FC.read_metrics(get_state)
    except Exception as e:
        metrics = {"error": str(e)}
    recent = []
    try:
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT ticker, created_date, extra_json FROM qwen_news_analysis "
                "WHERE analysis_type='ticker_deep' AND extra_json IS NOT NULL "
                "AND COALESCE(record_deleted,0)=0 ORDER BY created_date DESC LIMIT 60"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                fc = (json.loads(r["extra_json"]) or {}).get("factcheck") or {}
            except Exception:
                fc = {}
            for d in (fc.get("dropped") or []):
                recent.append({"ticker": r["ticker"], "at": r["created_date"],
                               "text": d.get("text"), "reason": d.get("reason")})
                if len(recent) >= last_n:
                    break
            if len(recent) >= last_n:
                break
    except Exception:
        pass
    return {"metrics": metrics, "recent_dropped": recent}


# ── LLM / gate indirection (injectable) ─────────────────────────────────────────

def _default_llm(system: str, user: str, **opts) -> str:
    import mason_llm
    return mason_llm.complete(system, user, intent="chore", **opts)


def _default_busy() -> bool:
    try:
        import mason_llm
        return bool(mason_llm.interactive_busy())
    except Exception:
        return False


# ── news delta (reuses mason_news_service's material gate — no hardcoded copy) ──

def _material_threshold() -> float:
    try:
        import mason_news_service as NS
        return float(NS._MATERIAL_THRESHOLD)
    except Exception:
        return 0.4


def _primary_ticker(ev: dict):
    tks = ev.get("tickers")
    if isinstance(tks, str):
        try:
            tks = json.loads(tks)
        except Exception:
            tks = []
    if isinstance(tks, list) and tks:
        return str(tks[0]).upper()
    return None


def material_delta(watermark: float, *, limit: int = 200) -> list[dict]:
    """Material, book-relevant news_events with ingested_at > watermark, oldest
    first. Uses mason_news_service._is_material as the canonical gate (falling back
    to the equivalent SQL predicate if the import fails). Read-only."""
    thresh = _material_threshold()
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM news_events WHERE ingested_at > ? AND scored = 1 "
            "AND materiality >= ? AND category IN ('holding','watchlist') "
            "AND COALESCE(is_deleted,0) = 0 "
            "ORDER BY ingested_at ASC LIMIT ?",
            (watermark, thresh, limit),
        ).fetchall()
    finally:
        conn.close()
    try:
        import mason_news_service as NS
        rows = [r for r in rows if NS._is_material(r)]
    except Exception:
        pass
    out = []
    for r in rows:
        d = dict(r)
        try:                              # the feed's own blurb, stashed in raw
            desc = (json.loads(d.get("raw") or "{}") or {}).get("desc") or ""
        except Exception:
            desc = ""
        out.append({
            "id": d["id"], "headline": d.get("headline") or "",
            "tickers": d.get("tickers"), "category": d.get("category"),
            "materiality": d.get("materiality"), "direction": d.get("direction"),
            "implication": d.get("implication"), "source": d.get("source") or "",
            "url": d.get("url") or "", "desc": desc,
            "ingested_at": d.get("ingested_at") or 0,
        })
    return out


def _analyzed_news_ids(news_ids: list[str]) -> set:
    """Which of these news_ids already have a ticker_deep analysis row."""
    if not news_ids:
        return set()
    conn = mason_db.get_connection()
    try:
        marks = ",".join("?" for _ in news_ids)
        rows = conn.execute(
            f"SELECT DISTINCT news_id FROM qwen_news_analysis WHERE analysis_type='ticker_deep' "
            f"AND COALESCE(record_deleted,0)=0 AND news_id IN ({marks})",
            news_ids,
        ).fetchall()
        return {r["news_id"] for r in rows}
    finally:
        conn.close()


def _write_analysis(ev: dict, ticker: str, summary: str, sentiment: str,
                    details: dict, model: str = _MODEL_NOTE, extra: dict | None = None) -> str:
    """Insert one qwen_news_analysis (ticker_deep) row for (news_id, ticker).
    Idempotent: a duplicate (news_id, ticker, ticker_deep) is skipped. `extra`
    (Phase 4: the fact-check audit) is stored additively in extra_json; when None
    the column is left NULL exactly as before."""
    conn = mason_db.get_connection()
    try:
        dup = conn.execute(
            "SELECT 1 FROM qwen_news_analysis WHERE analysis_type='ticker_deep' "
            "AND news_id=? AND ticker=? AND COALESCE(record_deleted,0)=0 LIMIT 1",
            (ev["id"], ticker),
        ).fetchone()
        if dup:
            return ""
        nid = uuid.uuid4().hex[:12]
        with conn:
            conn.execute(
                "INSERT INTO qwen_news_analysis (id, created_date, created_by, news_id, "
                "ticker, analysis_type, summary, sentiment, materiality, details_json, "
                "model, record_deleted, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (nid, _now_iso(), "mason", ev["id"], ticker, "ticker_deep",
                 summary, sentiment, ev.get("materiality"),
                 json.dumps(details, default=str) if details else None, model,
                 json.dumps(extra, default=str) if extra else None),
            )
        return nid
    finally:
        conn.close()


# ── per-ticker qwen analysis ────────────────────────────────────────────────────

_TICKER_SYS = (
    "You are one of Mason's overnight equity analysts. You read fresh news about a "
    "single ticker and write a TIGHT, factual read for a busy trader. You are the "
    "local model: stay COARSE — no invented price targets, no precise entries, no "
    "fabricated numbers. Only reason from what the headlines actually say. "
    "Respond with STRICT JSON only, no prose around it:\n"
    '{"summary": "<=2 sentences on what changed and why it matters to the book", '
    '"sentiment": "bullish|bearish|neutral", '
    '"details": {"what_changed": "...", "why_it_matters": "...", '
    '"levels_or_refs": "any price/level explicitly in the text, else \'none stated\'"}}'
)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    # strip ``` fences if present
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return None
    try:
        d = json.loads(s[a:b + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def analyze_ticker(llm_fn, ticker: str, events: list[dict]) -> tuple[str, str, dict]:
    """One batched qwen call for a ticker's fresh events. Returns
    (summary, sentiment, details). Never raises — a bad/empty model reply degrades
    to a plain summary + neutral."""
    lines = []
    for e in events[:8]:
        mat = e.get("materiality")
        mats = f" [{round(mat*100)}%]" if isinstance(mat, (int, float)) else ""
        impl = f" — {e['implication']}" if e.get("implication") else ""
        lines.append(f"- {e.get('headline','')}{mats}{impl}  ({e.get('source','')})")
    user = (f"Ticker: {ticker}\nFresh headlines touching the book:\n"
            + "\n".join(lines) + "\n\nWrite the JSON read now.")
    try:
        raw = llm_fn(_TICKER_SYS, user, num_predict=320, timeout=90)
    except Exception as e:
        log.warning(f"[desk] ticker analysis llm error for {ticker}: {e}")
        heads = "; ".join(x.get("headline", "") for x in events[:3])
        return (f"{ticker}: {heads}"[:400], "neutral",
                {"error": f"llm unavailable: {e}"})
    d = _extract_json(raw)
    if not d:
        return ((raw or "").strip()[:400] or f"{ticker}: no analysis produced",
                "neutral", {"raw": (raw or "")[:500]})
    summary = str(d.get("summary") or "").strip()[:600] or f"{ticker}: (no summary)"
    sentiment = str(d.get("sentiment") or "neutral").strip().lower()
    if sentiment not in ("bullish", "bearish", "neutral"):
        sentiment = "neutral"
    details = d.get("details") if isinstance(d.get("details"), dict) else {}
    return summary, sentiment, details


# ── the delta scan (Deliverable B) ──────────────────────────────────────────────

def run_analyst_scan(*, llm_fn=None, busy_fn=None, notifier=None, cfg=None,
                     sleep_fn=time.sleep, clock=time.time, now: float | None = None,
                     max_busy_wait: float | None = None) -> dict:
    """One delta-scan cycle. Reads new material events past the watermark, groups
    by primary ticker, analyzes up to `max_tickers_per_cycle` (breaking ≥0.8 first,
    then by materiality), writes qwen_news_analysis rows, emits a huge_event
    notification per breaking event, and advances the watermark ONLY when the whole
    delta has been analyzed (restart-safe — a crash mid-cycle just re-derives the
    unanalyzed set next time). Yields the GPU to interactive chat before each qwen
    call (see _gpu_gate).

    Returns a summary dict. Never raises out (callers may still catch)."""
    llm_fn = llm_fn or _default_llm
    busy_fn = busy_fn if busy_fn is not None else _default_busy
    notifier = notifier or mason_notifications.add_notification
    desk = _desk_cfg(cfg)
    if _cfg_get(desk, ["scan", "enabled"], True) is False:
        return {"skipped": "scan disabled", "processed": 0}
    cap = int(_cfg_get(desk, ["scan", "max_tickers_per_cycle"], 8) or 8)
    breaking_thresh = float(_cfg_get(desk, ["scan", "breaking_threshold"], 0.8) or 0.8)
    min_between = float(_cfg_get(desk, ["worker", "min_seconds_between_calls"], 3) or 0)
    yield_sleep = float(_cfg_get(desk, ["worker", "yield_sleep_seconds"], 5) or 5)

    watermark = _get_float("news_watermark", 0.0)
    delta = material_delta(watermark)
    if not delta:
        return {"processed": 0, "tickers": 0, "breaking": 0,
                "watermark": watermark, "deferred": False}

    analyzed = _analyzed_news_ids([e["id"] for e in delta])
    unanalyzed = [e for e in delta if e["id"] not in analyzed]

    # group unanalyzed by primary ticker; drop events with no ticker (shouldn't
    # happen for holding/watchlist rows, but be safe)
    groups: dict[str, list] = {}
    for e in unanalyzed:
        tk = _primary_ticker(e)
        if tk:
            groups.setdefault(tk, []).append(e)

    def _group_key(item):
        tk, evs = item
        maxmat = max((ev.get("materiality") or 0) for ev in evs)
        is_break = maxmat >= breaking_thresh
        return (0 if is_break else 1, -maxmat)

    ordered = sorted(groups.items(), key=_group_key)
    to_process = ordered[:cap]

    processed_events = 0
    breaking_notifs = 0
    breaking_items = []          # (news_id, ticker) for the Phase-3 debate pipeline
    deferred = False
    last_call = 0.0

    for tk, evs in to_process:
        # yield to the interactive chat before dispatching this ticker's qwen call
        if not _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
            deferred = True
            break
        # min-interval floor so the desk never machine-guns the GPU when idle
        if min_between and last_call:
            gap = clock() - last_call
            if gap < min_between:
                sleep_fn(min_between - gap)
        maxmat = max((ev.get("materiality") or 0) for ev in evs)
        # analyst ledger: work is beginning on this ticker
        try:
            mason_activity.log_activity(
                "analysis_started", f"Qwen began analysis — {tk}", actor="qwen",
                category="analyst", ticker=tk, trigger="autonomous",
                body=f"{len(evs)} fresh event{'s' if len(evs) != 1 else ''}; "
                     f"max materiality {maxmat:.2f}",
                extra={"news_ids": [ev["id"] for ev in evs]})
        except Exception:
            pass
        summary, sentiment, details = analyze_ticker(llm_fn, tk, evs)
        last_call = clock()

        # Phase 4 Layer 1: validate the analyst output against its own source
        # events before it is stored (behind factcheck.layer1.enabled). Deterministic
        # checks are free; the optional qwen source-diff runs only for material rows
        # and only after another GPU yield-to-chat.
        fc_extra = None
        try:
            import mason_factcheck as FC
            if FC.layer_enabled("layer1", cfg):
                run_diff = (FC.layer_enabled("qwen_diff", cfg)
                            and maxmat >= FC.materiality_floor(cfg))
                if run_diff:
                    if not _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
                        run_diff = False               # defer the diff; deterministic-only
                    else:
                        if min_between and last_call:
                            gap = clock() - last_call
                            if gap < min_between:
                                sleep_fn(min_between - gap)
                summary, details, fc_audit = FC.factcheck_analysis(
                    summary, sentiment, details, evs, ticker=tk,
                    llm_fn=llm_fn, cfg=cfg, run_diff=run_diff)
                if run_diff:
                    last_call = clock()
                fc_extra = {"factcheck": fc_audit}
                try:
                    FC.roll_metrics(fc_audit, get_state, set_state)
                except Exception:
                    pass
                try:
                    mason_activity.log_activity(
                        "factcheck", f"Fact-check — {tk}", actor="qwen",
                        category="analyst", ticker=tk, trigger="autonomous",
                        body=(f"{fc_audit.get('verified')}/{fc_audit.get('checked')} "
                              f"claims verified"),
                        extra=fc_audit)
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[desk] layer1 factcheck failed for {tk}, storing raw: {e}")
            fc_extra = None

        for ev in evs:
            _write_analysis(ev, tk, summary, sentiment, details, extra=fc_extra)
            processed_events += 1
        # breaking fast-path: one huge_event notification per breaking event
        first_notif_id = None
        for ev in evs:
            if (ev.get("materiality") or 0) >= breaking_thresh:
                # 2026-11-15: commentary and market wraps ("Dow Jones Futures Loom ... Nvidia Earnings")
                # are not breaking events about the ticker they mention. The news triage already refuses
                # to alert on them; this path trusted the local model's own score. Same rule here,
                # with the inbox's held-name rule (2026-11-16, news.quality.shared_form_rule): a held
                # name's event headline that ends "Here's what to know" / "- follow live" goes on.
                _excused = False
                try:
                    import mason_news_quality as _NQf
                    _hd = (ev.get("headline") or "").strip()
                    _why = _NQf.non_event_blocks(_hd, tk)
                    if not _why:
                        # let past the skip ONLY by the held-name rule (review fix 2026-11-16)
                        import mason_triage as _TRf
                        _excused = bool(_TRf.non_event_reason(_hd))
                except Exception:
                    _why = None
                if _why:
                    log.info(f"[desk] breaking alert skipped for {tk} (not an event: {_why})")
                    continue
                # 2026-11-15 (news.quality): the same rules as the news bridge — a US-listed
                # subject, no routine items, nothing older than max_age_hours — and one inbox
                # item per story: a Breaking read of an article already in the inbox upgrades
                # that item (or is attached to it once read) instead of posting it again.
                _gate, _NQ, _btk = None, None, tk
                try:
                    import mason_news_quality as _NQ
                    if _NQ.on("enabled"):
                        _gate = _NQ.gate_event(ev, stream="breaking", check_age=True,
                                               now=(now if now is not None else clock()))
                        if not _gate.get("ok"):
                            log.info(f"[desk] breaking alert skipped for {tk} ({_gate.get('why')})")
                            # the gate decides the INBOX only: the Phase-3 debate pipeline still
                            # gets the event, exactly as before (review fix 2026-11-15) -- except a
                            # commentary-form headline that only the held-name rule let past the
                            # skip: the gate says it is not that held name's own event (another
                            # company's preview tagged NVDA), so no debate under this ticker
                            # either, as before 2026-11-16 (review fix 2026-11-16)
                            if not _excused:
                                breaking_items.append((ev["id"], tk))
                            continue
                        _btk = _gate.get("ticker") or tk
                    else:
                        _NQ = None
                except Exception:
                    _gate, _NQ = None, None
                try:
                    head = (ev.get("headline") or "").strip()
                    blurb = (ev.get("desc") or "").strip()
                    url = ev.get("url") or ""
                    src = ev.get("source", "") or "source"
                    src_line = f"[{src}]({url})" if url else src
                    # full title + feed blurb + the verified analyst read + a
                    # clickable source (the UI linkifies the markdown link).
                    body = head + "\n\n"
                    if blurb:
                        body += blurb + "\n\n"
                    body += (summary or "").strip() + f"\n\nSource: {src_line}"
                    payload = dict(type="huge_event", severity="warn", ticker=_btk,
                                   title=f"Breaking: {_btk} — {head}",
                                   body=body, ref_table="news_events", ref_id=ev["id"],
                                   created_by="mason", dedup_key=f"breaking:{ev['id']}")
                    if _NQ is not None and _gate is not None:
                        _new = _NQ.new_item(stream="breaking", ticker=_btk, headline=head,
                                            news_ids=[ev["id"]], kind=_gate.get("kind"),
                                            family=_gate.get("family"), strong=_gate.get("strong"),
                                            mat=ev.get("materiality"))
                        placed = _NQ.place(new=_new, payload=payload, notifier=notifier, url=url,
                                           source=src, note=(summary or "").strip(),
                                           merge_key="breaking_merge")
                        notif = {"id": placed.get("id")} if placed.get("id") else None
                        if placed.get("action") != "new":
                            log.info(f"[desk] breaking read for {_btk} {placed.get('action')} into "
                                     f"inbox item {placed.get('id')} ({placed.get('how')})")
                    else:
                        notif = notifier(**payload)
                    if first_notif_id is None and isinstance(notif, dict):
                        first_notif_id = notif.get("id")
                    breaking_notifs += 1
                    breaking_items.append((ev["id"], tk))   # → Phase-3 debate pipeline
                except Exception as e:
                    log.warning(f"[desk] breaking notify failed: {e}")
        # analyst ledger: work finished — link forward to the notification it raised
        try:
            mason_activity.log_activity(
                "analysis_finished", f"Qwen finished analysis — {tk}", actor="qwen",
                category="analyst", ticker=tk, trigger="autonomous", body=summary,
                ref_table="notifications" if first_notif_id else None,
                ref_id=first_notif_id,
                extra={"sentiment": sentiment, "max_materiality": maxmat,
                       "events": len(evs)})
        except Exception:
            pass

    # advance the watermark only when every unanalyzed ticker was handled this
    # cycle (i.e. no backlog remains). Otherwise leave it so the next cycle picks
    # up the rest — nothing skipped, and a crash can't strand un-analyzed events.
    advanced = False
    if not deferred and len(groups) <= cap:
        newwm = max(e["ingested_at"] for e in delta)
        if newwm > watermark:
            set_state("news_watermark", newwm)
            watermark = newwm
            advanced = True

    return {"processed": processed_events, "tickers": len(to_process),
            "breaking": breaking_notifs, "breaking_items": breaking_items,
            "watermark": watermark,
            "backlog_tickers": max(0, len(groups) - len(to_process)),
            "deferred": deferred, "watermark_advanced": advanced}


def _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep: float,
              max_wait: float | None) -> bool:
    """Block while the interactive chat is streaming, yielding the GPU. Returns
    True when clear to proceed, False if it gave up after `max_wait` seconds (the
    caller then defers the work to the next cycle). max_wait=None waits as long as
    needed; max_wait=0 checks once and defers immediately if busy (test path)."""
    if not busy_fn():
        return True
    start = clock()
    while busy_fn():
        if max_wait is not None and (clock() - start) >= max_wait:
            return False
        sleep_fn(max(0.05, yield_sleep))
    return True


# ── five analyst context builders (Deliverable C) — best-effort, never raise ────

def _ctx_news(since_iso: str | None = None, limit: int = 15) -> str:
    try:
        conn = mason_db.get_connection()
        try:
            if since_iso:
                rows = conn.execute(
                    "SELECT ticker, sentiment, summary, materiality FROM qwen_news_analysis "
                    "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
                    "AND created_date > ? ORDER BY created_date DESC LIMIT ?",
                    (since_iso, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ticker, sentiment, summary, materiality FROM qwen_news_analysis "
                    "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
                    "ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        if not rows:
            return "News desk: no new per-ticker analyses since last report."
        out = ["News desk (recent per-ticker reads):"]
        for r in rows:
            mat = r["materiality"]
            mats = f" [{round(mat*100)}%]" if isinstance(mat, (int, float)) else ""
            out.append(f"  - {r['ticker']} ({r['sentiment']}{mats}): {(r['summary'] or '')[:180]}")
        return "\n".join(out)
    except Exception as e:
        return f"News desk: unavailable ({e})."


def _ctx_sentiment() -> str:
    parts = []
    try:
        import mason_intelligence as MI
        parts.append(MI.brief().strip())
    except Exception as e:
        parts.append(f"Intelligence brief: unavailable ({e}).")
    try:
        from pathlib import Path
        pc = json.loads((Path(__file__).parent / ".posture_cache.json").read_text(encoding="utf-8"))
        line = pc.get("posture_line") or ""
        if line:
            parts.append(f"Posture: {line} (as of {pc.get('as_of','?')})")
    except Exception:
        parts.append("Posture: unavailable.")
    return "Sentiment / regime:\n  " + "\n  ".join(p for p in parts if p)


def _ctx_analytics() -> str:
    try:
        import mason_scanner as SCN
        scan = SCN.get_latest_scan()
        if not scan.get("ok"):
            return f"Analytics/scanner: unavailable ({scan.get('error')})."
        data = scan.get("data") or {}
        nc = data.get("new_candidates") or []
        pa = data.get("position_alerts") or []
        out = [f"Analytics/scanner ({scan.get('age_note','')}):"]
        for c in nc[:6]:
            out.append(f"  + candidate {c.get('ticker')}: {(c.get('reason') or '')[:120]} "
                       f"[{c.get('urgency','')}]")
        for a in pa[:6]:
            out.append(f"  ! {a.get('ticker')} alert: {(a.get('detail') or a.get('trigger') or '')[:120]} "
                       f"[{a.get('urgency','')}]")
        if not nc and not pa:
            out.append("  (no new candidates or position alerts)")
        return "\n".join(out)
    except Exception as e:
        return f"Analytics/scanner: unavailable ({e})."


def _ctx_fundamentals() -> str:
    # Its only data was the total of the retired positions.csv snapshot (mason_brief.portfolio_snapshot, removed
    # 2026-11-19, Nico's 11-11 ruling); what is left is the builder's own "no detail" line.
    return "Fundamentals/portfolio:\n  (no position detail available)"


def _ctx_positioning() -> str:
    # LIGHT + signal-only: open options plays + any already-cached institutional
    # flow. No heavy/slow network on this path (no live 13F).
    try:
        import mason_options as O
        plays = O.get_open_plays() or []
        out = ["Positioning & flow (signal-only):"]
        if not plays:
            out.append("  no open option plays.")
        for p in plays[:10]:
            out.append(f"  - {p.get('ticker')} {p.get('strategy','')} {p.get('direction','')} "
                       f"exp {p.get('expiration','')}")
        # cached institutional flow if present (best-effort, no network)
        try:
            from pathlib import Path
            fp = Path(__file__).parent / "institutional_flow_cache.json"
            if fp.exists():
                flow = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(flow, dict) and flow:
                    out.append(f"  cached inst. flow present ({len(flow)} keys)")
        except Exception:
            pass
        return "\n".join(out)
    except Exception as e:
        return f"Positioning & flow: unavailable ({e})."


_CTX_BUILDERS = {
    "news": _ctx_news,
    "sentiment": _ctx_sentiment,
    "analytics": _ctx_analytics,
    "fundamentals": _ctx_fundamentals,
    "positioning": _ctx_positioning,
}


def build_contexts(ctx_overrides: dict | None = None, *, since_iso: str | None = None) -> dict:
    """Assemble all five analyst contexts. Each degrades independently — a builder
    that raises becomes 'unavailable' and never blocks the others. ctx_overrides
    (tests) replaces a builder with a fixed string or a callable."""
    out = {}
    for name, fn in _CTX_BUILDERS.items():
        if ctx_overrides and name in ctx_overrides:
            ov = ctx_overrides[name]
            try:
                out[name] = ov() if callable(ov) else str(ov)
            except Exception as e:
                out[name] = f"{name}: unavailable ({e})."
            continue
        try:
            out[name] = fn(since_iso=since_iso) if name == "news" else fn()
        except Exception as e:
            out[name] = f"{name}: unavailable ({e})."
    # ── RH Expansion S2-T8 ──
    # the ONE composed "Robinhood & evidence" block, only while
    # rh_expansion.brain_context.enabled (+ robinhood.enabled); otherwise no key at all.
    if ctx_overrides and "rh_evidence" in ctx_overrides:
        ov = ctx_overrides["rh_evidence"]
        try:
            out["rh_evidence"] = str(ov() if callable(ov) else ov)
        except Exception as e:
            log.info(f"[desk] RH evidence override failed: {e}")
    else:
        try:
            import mason_rhx_integration as RHX
            blk = RHX.desk_context()
            if blk:
                out["rh_evidence"] = blk
        except Exception as e:
            log.info(f"[desk] RH evidence block unavailable: {e}")
    # ── end S2-T8 ──
    return out


# ── consolidated Desk Report (Deliverable D) ───────────────────────────────────

_REPORT_SYS = (
    "You are Mason's overnight analyst desk lead, writing a consolidated briefing "
    "for Nico from your analysts' inputs. Be concise, concrete and honest. You are "
    "the local model: you may say 'watch X' or 'the tape seems to favor Y', but you "
    "must NOT invent precise order prices, entries, or fabricated numbers — the "
    "director sets those. Write plain readable prose with short sections. Do NOT "
    "wrap in JSON or code fences."
)

_KIND_FRAMING = {
    "hourly": "This is a light hourly rolling update — what changed this hour.",
    "anchor_0700": "This is the 07:00 ET open briefing — the overnight picture and "
                   "what to watch into the open.",
    "anchor_1300": "This is the 13:00 ET midday briefing — the picture so far and "
                   "what to watch into the afternoon.",
}

# Step 2 (2026-11-14): the two scheduled calls may be moved (analyst_desk.anchors.times_et).
# Their kind keeps following the clock time ("anchor_0840"), which is what the scheduler,
# mason_selfcheck and the dashboard already derive. Before this, a kind other than the two
# above was silently turned into an hourly report and the director was never called.
_ANCHOR_KIND = re.compile(r"^anchor_([01]\d|2[0-3])([0-5]\d)$")


def is_anchor_kind(kind) -> bool:
    return bool(_ANCHOR_KIND.match(str(kind or "")))


def anchor_kind(t: str) -> str:
    """Kind of the scheduled call configured at ET time "HH:MM"."""
    return "anchor_" + str(t).replace(":", "")


def _kind_label(kind) -> str:
    m = _ANCHOR_KIND.match(str(kind or ""))
    return f"{m.group(1)}:{m.group(2)}" if m else "Hourly"


def _kind_framing(kind) -> str:
    if kind in _KIND_FRAMING:
        return _KIND_FRAMING[kind]
    m = _ANCHOR_KIND.match(str(kind or ""))
    if not m:
        return _KIND_FRAMING["hourly"]
    if int(m.group(1)) < 12:
        return (f"This is the {m.group(1)}:{m.group(2)} ET open briefing — the overnight picture and "
                "what to watch into the open.")
    return (f"This is the {m.group(1)}:{m.group(2)} ET midday briefing — the picture so far and "
            "what to watch into the afternoon.")


def anchor_times(cfg: dict | None = None) -> list:
    """The configured scheduled-call times, valid "HH:MM" only, in clock order."""
    t = _cfg_get(_desk_cfg(cfg), ["anchors", "times_et"], ["07:00", "13:00"])
    raw = t if isinstance(t, list) else []
    out = [x.strip() for x in raw
           if isinstance(x, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x.strip())]
    if len(out) != len(raw):
        log.warning(f"[desk] analyst_desk.anchors.times_et has entries that are not \"HH:MM\"; ignored: "
                    f"{[x for x in raw if not (isinstance(x, str) and x.strip() in out)]}")
    return sorted(dict.fromkeys(out))


def _slot_of(hhmm: str, times: list) -> int:
    """Which call of the day a clock time is: its place in today's configured times, or,
    for a time that is no longer configured, 0 before noon and 1 after (capped)."""
    if hhmm in times:
        return times.index(hhmm)
    return min(0 if hhmm < "12:00" else 1, max(0, len(times) - 1))


def anchor_slots_done(today: str, times: list) -> set:
    """Slots (0 = first call of the day, 1 = second, ...) that already FINISHED today under
    any clock time. Moving a call's time during the day must not run it a second time:
    last_anchor_0700 = today means the morning call is done even if it is now set to 08:40."""
    done = set()
    conn = mason_db.get_connection()
    try:
        for r in conn.execute("SELECT key FROM desk_state WHERE key LIKE 'last_anchor_%' AND value = ?",
                              (today,)).fetchall():
            k = r["key"][len("last_anchor_"):]
            if len(k) == 4 and k.isdigit():
                done.add(_slot_of(f"{k[:2]}:{k[2:]}", times))
    finally:
        conn.close()
    return done


def _recent_verified_claims(since_iso: str | None, limit: int = 40) -> list:
    """Phase 4 Layer 2 input: the Layer-1-VERIFIED per-ticker claims feeding this
    report, pulled from qwen_news_analysis rows' details_json.claims. Returns
    [{ticker, text}] (verified only). Rows written before Layer 1 (no claims key)
    contribute nothing — the report stays claim-grounded. Read-only; never raises."""
    out = []
    try:
        conn = mason_db.get_connection()
        try:
            if since_iso:
                rows = conn.execute(
                    "SELECT ticker, details_json FROM qwen_news_analysis "
                    "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
                    "AND created_date > ? ORDER BY created_date DESC LIMIT ?",
                    (since_iso, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ticker, details_json FROM qwen_news_analysis "
                    "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 "
                    "ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                det = json.loads(r["details_json"]) if r["details_json"] else {}
            except Exception:
                det = {}
            for c in (det.get("claims") or []):
                if isinstance(c, dict) and c.get("verdict") == "verified" and (c.get("text") or "").strip():
                    out.append({"ticker": r["ticker"], "text": c["text"].strip()})
    except Exception as e:
        log.warning(f"[desk] _recent_verified_claims failed: {e}")
    return out


def build_desk_report(kind: str, *, llm_fn=None, busy_fn=None, notifier=None,
                      cfg=None, ctx_overrides: dict | None = None,
                      sleep_fn=time.sleep, clock=time.time, now: float | None = None,
                      max_busy_wait: float | None = None) -> dict:
    """Assemble the five contexts + accumulated analyses and write ONE qwen desk
    report to the inbox as a completed_analysis notification (deduped per
    kind/hour). kind ∈ {hourly, anchor_HHMM (anchor_0700, anchor_1300, or the clock time
    a call was moved to)}. Never raises out."""
    if kind not in _KIND_FRAMING and not is_anchor_kind(kind):
        kind = "hourly"
    llm_fn = llm_fn or _default_llm
    busy_fn = busy_fn if busy_fn is not None else _default_busy
    notifier = notifier or mason_notifications.add_notification
    desk = _desk_cfg(cfg)
    yield_sleep = float(_cfg_get(desk, ["worker", "yield_sleep_seconds"], 5) or 5)

    since_iso = get_state("last_report_at") or None
    contexts = build_contexts(ctx_overrides, since_iso=since_iso)
    # widen-macro: fold the world/macro context into the report ONLY when enabled,
    # so with the flag off the report is byte-for-byte unchanged.
    if _cfg_get(desk, ["macro", "enabled"], False) and not (ctx_overrides and "macro" in ctx_overrides):
        try:
            contexts["macro"] = _ctx_macro(since_iso=since_iso)
        except Exception:
            pass
    # Catalyst Desk: let the qwen desk see the Opus director's open theses + follow-ups
    # (only when director v2 is on, so with it off the report is byte-for-byte unchanged).
    if (_cfg_get(desk, ["opus_director", "v2", "enabled"], False)
            and _cfg_get(desk, ["opus_director", "v2", "share_with_desk"], True)
            and not (ctx_overrides and "director" in ctx_overrides)):
        try:
            contexts["director"] = _ctx_director()
        except Exception:
            pass
    hourly_light = _cfg_get(desk, ["report", "hourly_light"], True)

    ctx_text = "\n\n".join(f"[{k.upper()}]\n{v}" for k, v in contexts.items())
    framing = _kind_framing(kind)
    depth = ("Keep it tight — a few lines per area, lead with what moved."
             if (kind == "hourly" and hourly_light) else
             "Give the full consolidated picture across all five areas.")
    user = (f"{framing}\n{depth}\n\nAnalyst inputs:\n\n{ctx_text}\n\n"
            "Write the desk report now (plain prose, clearly a qwen desk report).")

    # yield to interactive chat before the (single) report call
    if not _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
        return {"posted": False, "deferred": True, "kind": kind}

    # Phase 4 Layer 2: assemble the report deterministically from Layer-1-verified
    # claims + system-sourced context lines (qwen only phrases, then a diff strips
    # any synthesis-time fabrication) — behind factcheck.layer2.enabled. With the
    # flag off this whole branch is skipped and the free-prose path below is used
    # byte-for-byte as before.
    fc_verified_claims, fc_report_audit = [], None
    fc_layer2 = False
    try:
        import mason_factcheck as FC
        fc_layer2 = FC.layer_enabled("layer2", cfg)
    except Exception:
        fc_layer2 = False

    if fc_layer2:
        label_now = _kind_label(kind)
        try:
            news_claims = _recent_verified_claims(since_iso)
            report, fc_verified_claims, fc_report_audit = FC.assemble_report(
                kind, label_now, news_claims, contexts, llm_fn=llm_fn, cfg=cfg,
                run_diff=FC.layer_enabled("qwen_diff", cfg))
            try:
                FC.roll_metrics(fc_report_audit, get_state, set_state)
            except Exception:
                pass
        except Exception as e:
            log.warning(f"[desk] layer2 assembly failed ({kind}), falling back to raw: {e}")
            report = ("(Qwen desk model unavailable — raw analyst inputs below)\n\n" + ctx_text)
    else:
        try:
            report = llm_fn(_REPORT_SYS, user, num_predict=900, timeout=180)
        except Exception as e:
            log.warning(f"[desk] report llm error ({kind}): {e}")
            report = None
        if not report or not report.strip():
            # degrade: still post a report assembled from the raw contexts so the desk
            # never goes silent on an anchor.
            report = ("(Qwen desk model unavailable — raw analyst inputs below)\n\n" + ctx_text)

    et = _now_et(now)
    stamp = et.strftime("%Y-%m-%d-%H")
    label = _kind_label(kind)
    n_tickers = 0
    try:
        import re as _re
        n_tickers = len(_re.findall(r"^\s*[-+!]\s", contexts.get("news", ""), _re.M))
    except Exception:
        pass
    unavailable = [k for k, v in contexts.items() if "unavailable" in (v or "")[:40]]
    title = f"{label} Desk Report — {et.strftime('%a %-I%p ET')}"
    if unavailable:
        n_un = len(unavailable)
        title += f" ({n_un} section{'s' if n_un != 1 else ''} unavailable)"

    body = f"Qwen desk report · {kind} · {et.strftime('%Y-%m-%d %-I:%M%p ET')}\n\n{report.strip()}"
    report_notif_id = None
    try:
        notif = notifier(type="completed_analysis", severity="info", title=title,
                 body=body, created_by="mason", ref_table="qwen_news_analysis",
                 dedup_key=f"deskreport:{kind}:{stamp}")
        if isinstance(notif, dict):
            report_notif_id = notif.get("id")
    except Exception as e:
        log.warning(f"[desk] report notify failed: {e}")
        return {"posted": False, "error": str(e), "kind": kind}

    set_state("last_report_at", _now_iso())
    # analyst ledger: the desk report is analyst work; link it to the notification.
    try:
        mason_activity.log_activity(
            "desk_report", f"qwen posted the {label} desk report", actor="qwen",
            category="analyst", trigger="autonomous", body=title,
            ref_table="notifications" if report_notif_id else None,
            ref_id=report_notif_id,
            extra={"kind": kind, "unavailable_sections": unavailable,
                   "n_tickers": n_tickers})
    except Exception:
        pass

    # Phase 3 (Deliverable F): at the anchors, assemble the director package and
    # touch the (disabled) Opus director seam. No Opus is called.
    director = None
    if is_anchor_kind(kind):
        try:
            import mason_desk_pipeline as PP
            package = PP.assemble_director_package(kind, dict(contexts, report=report.strip()),
                                                   cfg=cfg, now=now)
            package["report_text"] = report.strip()
            # Phase 4: hand the future Opus director the CLEANED, cited input.
            if fc_verified_claims:
                package["verified_claims"] = fc_verified_claims
            if fc_report_audit:
                package["factcheck_audit"] = fc_report_audit
            director = PP.persist_director_package(package)
            PP._maybe_run_opus_director(package, cfg=cfg)
        except Exception as e:
            log.warning(f"[desk] director package assembly failed: {e}")

    return {"posted": True, "kind": kind, "title": title,
            "unavailable_sections": unavailable, "deferred": False,
            "director_package_id": director}


# ── macro / world sensor: hourly digest + breaking macro (the "widen macro") ────
#
# The desk is a world sensor, not just a book watcher. mason_news_service scores
# macro events deterministically; here we roll ONE qwen "World Macro" digest per
# cycle over the material macro since a watermark, fact-check it in MACRO MODE
# (Layer 1 relaxed — no cited tickers, but invented tickers/numbers/unsupported
# claims still stripped), store it, and flag BREAKING macro to the inbox. Behind
# analyst_desk.macro.enabled (default OFF → every entry point is a no-op).

_MACRO_SYS = (
    "You are Mason's macro / world desk. You are given fresh MARKET-WIDE and WORLD "
    "news headlines — not tied to any single holding. Write a TIGHT world-macro read "
    "for a trader: what is actually moving globally, the risk backdrop, and what it "
    "could mean for markets. You are the local model: stay COARSE — do NOT invent "
    "tickers, price levels, or numbers. Reason ONLY from the headlines given. Plain "
    "prose, a few short lines, no JSON, no code fences."
)


def _macro_delta(watermark: float, floor: float, limit: int) -> tuple:
    """Material macro events since the watermark, de-duplicated by headline tokens,
    most-material first. Returns (events, max_materiality, max_ingested_at). Read-only."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, headline, url, raw, materiality, direction, source, ingested_at "
            "FROM news_events WHERE category='macro' AND scored=1 AND materiality >= ? "
            "AND ingested_at > ? AND COALESCE(is_deleted,0)=0 "
            "ORDER BY materiality DESC, ingested_at DESC LIMIT ?",
            (floor, watermark, max(limit * 4, 40))).fetchall()
    finally:
        conn.close()
    out, seen_tok, maxmat, maxing = [], [], 0.0, watermark
    for r in rows:
        d = dict(r)
        try:                              # the feed's own blurb, stashed in raw
            d["desc"] = (json.loads(d.get("raw") or "{}") or {}).get("desc") or ""
        except Exception:
            d["desc"] = ""
        maxing = max(maxing, d.get("ingested_at") or 0)
        maxmat = max(maxmat, d.get("materiality") or 0)
        toks = set(w for w in (d.get("headline") or "").lower().split() if len(w) > 3)
        if any(len(toks & s) >= max(3, int(0.6 * min(len(toks), len(s) or 1))) for s in seen_tok):
            continue                       # near-duplicate of one already taken
        seen_tok.append(toks)
        out.append(d)
        if len(out) >= limit:
            break
    return out, maxmat, maxing


def run_macro_digest(*, llm_fn=None, busy_fn=None, notifier=None, cfg=None,
                     sleep_fn=time.sleep, clock=time.time, now: float | None = None,
                     max_busy_wait: float | None = None) -> dict:
    """One macro-digest cycle. Reads material macro since the macro watermark, writes
    ONE qwen 'World Macro' digest (fact-checked in macro mode), posts it as a
    completed_analysis notification, flags breaking macro to the inbox, and advances
    the watermark. Behind analyst_desk.macro.enabled. Never raises out."""
    llm_fn = llm_fn or _default_llm
    busy_fn = busy_fn if busy_fn is not None else _default_busy
    notifier = notifier or mason_notifications.add_notification
    desk = _desk_cfg(cfg)
    if not _cfg_get(desk, ["macro", "enabled"], False):
        return {"skipped": "macro disabled", "posted": False}
    floor = float(_cfg_get(desk, ["macro", "materiality_floor"], 0.5) or 0.5)
    breaking = float(_cfg_get(desk, ["macro", "breaking_threshold"], 0.8) or 0.8)
    cap = int(_cfg_get(desk, ["macro", "max_per_digest"], 25) or 25)
    notify_breaking = _cfg_get(desk, ["macro", "notify_breaking"], True)
    yield_sleep = float(_cfg_get(desk, ["worker", "yield_sleep_seconds"], 5) or 5)

    watermark = _get_float("macro_watermark", 0.0)
    events, maxmat, maxing = _macro_delta(watermark, floor, cap)
    if not events:
        return {"posted": False, "events": 0, "watermark": watermark}

    # breaking macro → inbox (deterministic; one dedup'd notification per event)
    breaking_notifs = 0
    if notify_breaking:
        for ev in events:
            if (ev.get("materiality") or 0) >= breaking:
                try:
                    src = ev.get("source", "") or "source"
                    url = ev.get("url") or ""
                    # a clickable source link (markdown; the UI linkifies it)
                    head = (ev.get("headline") or "").strip()
                    blurb = (ev.get("desc") or "").strip()
                    src_line = f"[{src}]({url})" if url else src
                    blurb_line = f"{blurb}\n\n" if blurb else ""
                    notifier(type="huge_event", severity="warn", ticker=None,
                             title=f"Macro: {head}",
                             body=f"{head}\n\n{blurb_line}"
                                  f"Market-wide event (materiality {ev.get('materiality')}).\n"
                                  f"Source: {src_line}",
                             ref_table="news_events", ref_id=ev["id"], created_by="mason",
                             dedup_key=f"macro_breaking:{ev['id']}")
                    breaking_notifs += 1
                except Exception as e:
                    log.warning(f"[desk] macro breaking notify failed: {e}")

    # yield the GPU before the (single) digest call
    if not _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
        return {"posted": False, "deferred": True, "events": len(events)}

    lines = []
    for ev in events:
        mat = ev.get("materiality")
        mats = f" [{round(mat*100)}%]" if isinstance(mat, (int, float)) else ""
        line = f"- {ev.get('headline','')}{mats} ({ev.get('source','')})"
        blurb = (ev.get("desc") or "").strip()
        if blurb:                          # give qwen the feed's own blurb as context
            line += f"\n    {blurb[:220]}"
        lines.append(line)
    # ground truth for the fact-check diff = headline + the feed's blurb
    source_text = "\n".join(
        ((ev.get("headline", "") or "") + " " + (ev.get("desc", "") or "")).strip()
        for ev in events)
    user = ("Fresh market-wide / world headlines:\n" + "\n".join(lines) +
            "\n\nWrite the world-macro read now.")
    try:
        digest = llm_fn(_MACRO_SYS, user, num_predict=500, timeout=150)
    except Exception as e:
        log.warning(f"[desk] macro digest llm error: {e}")
        digest = None
    if not digest or not digest.strip():
        digest = "(Macro model unavailable — raw headlines below)\n" + "\n".join(lines)

    # Layer-1 macro-mode fact-check (behind factcheck.layer1)
    details = {"raw_summary": digest, "events": [e["id"] for e in events]}
    fc_audit = None
    try:
        import mason_factcheck as FC
        if FC.layer_enabled("layer1", cfg):
            run_diff = FC.layer_enabled("qwen_diff", cfg) and maxmat >= FC.materiality_floor(cfg)
            if run_diff and not _gpu_gate(busy_fn, sleep_fn, clock, yield_sleep, max_busy_wait):
                run_diff = False
            vs, fdet, fc_audit = FC.factcheck_digest(digest, source_text, llm_fn=llm_fn,
                                                     cfg=cfg, run_diff=run_diff)
            digest = vs
            details.update(fdet)
            try:
                FC.roll_metrics(fc_audit, get_state, set_state)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[desk] macro layer1 factcheck failed, keeping raw: {e}")

    # store the digest as a durable macro_digest analysis row
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO qwen_news_analysis (id, created_date, created_by, "
                    "analysis_type, summary, materiality, details_json, model, "
                    "record_deleted, extra_json) VALUES (?,?,?,?,?,?,?,?,0,?)",
                    (uuid.uuid4().hex[:12], _now_iso(), "mason", "macro_digest",
                     digest.strip(), maxmat, json.dumps(details, default=str),
                     _MODEL_NOTE, json.dumps({"factcheck": fc_audit}, default=str) if fc_audit else None))
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[desk] macro digest store failed: {e}")

    # Sources block: clickable links to the articles behind this digest (the UI
    # linkifies markdown links). Deduped, capped, only rows that carry a URL.
    src_lines = []
    for ev in events:
        url = ev.get("url") or ""
        head = (ev.get("headline") or "").strip()
        src = ev.get("source", "") or "source"
        blurb = (ev.get("desc") or "").strip()
        line = (f"- [{head}]({url})  — {src}" if url else f"- {head}  — {src}") if head else ""
        if not line:
            continue
        if blurb:
            line += f"\n    {blurb[:220]}"
        src_lines.append(line)
    sources_block = ("\n\nSources:\n" + "\n".join(src_lines[:15])) if src_lines else ""

    et = _now_et(now)
    title = f"World Macro Digest — {et.strftime('%a %-I%p ET')}"
    body = (f"Qwen macro desk · {et.strftime('%Y-%m-%d %-I:%M%p ET')}\n\n"
            f"{digest.strip()}{sources_block}")
    notif_id = None
    try:
        notif = notifier(type="completed_analysis", severity="info", title=title,
                         body=body, created_by="mason", ref_table="qwen_news_analysis",
                         dedup_key=f"macrodigest:{et.strftime('%Y-%m-%d-%H')}")
        if isinstance(notif, dict):
            notif_id = notif.get("id")
    except Exception as e:
        log.warning(f"[desk] macro digest notify failed: {e}")

    if maxing > watermark:
        set_state("macro_watermark", maxing)

    try:
        mason_activity.log_activity(
            "macro_digest", "qwen posted the World Macro digest", actor="qwen",
            category="analyst", trigger="autonomous", body=title,
            ref_table="notifications" if notif_id else None, ref_id=notif_id,
            extra={"events": len(events), "breaking": breaking_notifs,
                   "max_materiality": maxmat,
                   "factcheck": fc_audit} )
    except Exception:
        pass

    return {"posted": True, "events": len(events), "breaking": breaking_notifs,
            "watermark": maxing, "title": title}


def _ctx_director(limit: int = 10) -> str:
    """The Opus DIRECTOR's current book of theses + its scheduled follow-ups, so the
    qwen desk knows what the director is watching (and can flag news that confirms
    or invalidates it). Deterministic, read-only; never raises. 2026-09-27."""
    try:
        import mason_director as DIR
        preds = DIR._open_predictions(limit=limit)
        fus = DIR.upcoming_followups(days=30, limit=8)
        if not preds and not fus:
            return "Director: no open Opus theses or follow-ups."
        out = ["Opus director — open theses (watch for news that confirms or breaks them):"]
        for p in preds:
            out.append(f"  - {p['direction']} {p['ticker']} ({p.get('horizon')}, conf {p.get('confidence')}) "
                       f"since {p.get('date')}: {(p.get('thesis') or '')[:160]}")
        if fus:
            out.append("  Scheduled director follow-ups:")
            for f in fus:
                out.append(f"  - {f.get('due_date')} {f.get('ticker') or 'macro'}: {(f.get('action') or '')[:140]}")
        return "\n".join(out)
    except Exception as e:
        return f"Director: unavailable ({e})."


def _ctx_macro(since_iso: str | None = None, limit: int = 2) -> str:
    """World/macro context for the Desk Report: the most recent macro digests plus a
    few top material macro headlines. Best-effort; never raises."""
    try:
        conn = mason_db.get_connection()
        try:
            digs = conn.execute(
                "SELECT summary, created_date FROM qwen_news_analysis "
                "WHERE analysis_type='macro_digest' AND COALESCE(record_deleted,0)=0 "
                "ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()
            heads = conn.execute(
                "SELECT headline, materiality FROM news_events WHERE category='macro' "
                "AND scored=1 AND materiality >= 0.5 AND COALESCE(is_deleted,0)=0 "
                "ORDER BY ingested_at DESC LIMIT 6").fetchall()
        finally:
            conn.close()
        if not digs and not heads:
            return "World / macro: no material macro this cycle."
        out = ["World / macro:"]
        for d in digs:
            out.append(f"  {(d['summary'] or '')[:400]}")
        if heads:
            out.append("  recent material macro headlines:")
            for h in heads:
                mat = h["materiality"]
                mats = f" [{round(mat*100)}%]" if isinstance(mat, (int, float)) else ""
                out.append(f"    - {(h['headline'] or '')[:110]}{mats}")
        return "\n".join(out)
    except Exception as e:
        return f"World / macro: unavailable ({e})."


# ── single-worker priority queue + ET scheduler (Deliverable A) ─────────────────
#
# Work items: (priority, seq, kind, payload). priority 0 = breaking, 1 = scan,
# 2 = roundup. The queue exists so the scheduler can enqueue jobs and a breaking
# item can jump ahead; the actual per-qwen-call yield lives inside run_analyst_scan
# / build_desk_report via _gpu_gate. There is ever only ONE worker thread.

_PRIORITY = {"breaking": 0, "debate": 0, "scan": 1, "macro": 2, "roundup": 2, "grade": 3, "watch": 2,
             "desk_check": 1, "check_grade": 3}


def _watch_slot_due(et: datetime, at: str, window_min: int = 90) -> bool:
    """A digest slot runs in [at, at + 90 min] — after a late restart the missed
    slots are skipped instead of all firing at once."""
    try:
        hh, _, mm = str(at).partition(":")
        start = int(hh) * 60 + int(mm)
    except ValueError:
        return False
    now = et.hour * 60 + et.minute
    return start <= now <= start + window_min


def run_watch_job(payload: dict, *, llm_fn=None, busy_fn=None, clock=time.time) -> dict:
    """One watchlist desk job. {"digest": slot} → qwen digests for every watched name;
    {"tick": ...} → event digests for newly NOTIFIED watch events (throttled per ticker)
    + Alpaca two-way sync pull. Never raises."""
    out = {}
    try:
        cfg = _load_config().get("watchlist") or {}
        if not cfg.get("enabled"):
            return {"skipped": "watchlist disabled"}
        import mason_watch_signals as SG
        if payload.get("digest"):
            out["digests"] = SG.run_digests(payload["digest"], llm_fn=llm_fn, busy_fn=busy_fn)
            return out
        dcfg = cfg.get("digests") or {}
        if dcfg.get("enabled"):
            thr = float(dcfg.get("event_throttle_minutes", 60) or 60) * 60
            wm = _get_float("watch_event_digest_wm", clock() - 600)
            conn = mason_db.get_connection()
            try:
                rows = conn.execute("SELECT ticker, MAX(ts) AS ts FROM watch_events WHERE notified=1 "
                                    "AND ts > ? GROUP BY ticker", (wm,)).fetchall()
            finally:
                conn.close()
            done, top = [], wm
            mbw = float(dcfg.get("max_busy_wait_seconds", 300) or 0)
            ysl = float(dcfg.get("yield_sleep_seconds", 5) or 5)
            for r in sorted(rows, key=lambda x: float(x["ts"] or 0))[:10]:
                t, ts = r["ticker"], float(r["ts"] or 0)
                last = _get_float(f"watch_event_digest_last:{t}", 0.0)
                if clock() - last < thr:
                    top = max(top, ts)
                    continue
                # qwen shares the GPU with chat: yield like every desk job (N5); if chat
                # stays busy, stop here and leave the watermark so the next tick retries
                if not _gpu_gate(busy_fn or _default_busy, time.sleep, clock, ysl, mbw):
                    out["deferred"] = "gpu busy"
                    break
                top = max(top, ts)
                SG.build_digest(t, "event", llm_fn=llm_fn)
                set_state(f"watch_event_digest_last:{t}", clock())
                done.append(t)
            set_state("watch_event_digest_wm", top)
            out["event_digests"] = done
        if (cfg.get("alpaca_sync") or {}).get("enabled"):
            import mason_watchlist as WL
            out["sync"] = WL.sync_pull()
    except Exception as e:
        out["error"] = str(e)
    return out


class AnalystDesk:
    def __init__(self, *, cfg=None, llm_fn=None, busy_fn=None, notifier=None,
                 clock=time.time, sleep_fn=time.sleep):
        self.cfg = cfg
        self.llm_fn = llm_fn or _default_llm
        self.busy_fn = busy_fn if busy_fn is not None else _default_busy
        self.notifier = notifier or mason_notifications.add_notification
        self.clock = clock
        self.sleep_fn = sleep_fn
        self._q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None
        self._scheduler = None

    # -- queue --
    def submit(self, kind: str, payload: dict | None = None, *, priority: int | None = None):
        p = priority if priority is not None else _PRIORITY.get(kind, 5)
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._q.put((p, seq, kind, payload or {}))

    def _handle(self, kind: str, payload: dict) -> dict:
        """Activity v2: each desk job is one grouped run in the ledger."""
        try:
            import mason_activity as _ACT
            label = {"scan": "Desk scan", "breaking": "Breaking-news scan", "roundup": "Desk report",
                     "debate": "Bull/bear debate", "grade": "Grading", "macro": "World macro digest",
                     "catalyst_tick": "Catalyst tick", "watch": "Watchlist",
                     "selfcheck": "Self-check", "desk_check": "Quick checks",
                     "check_grade": "Quick-check follow-up"}.get(kind, f"Desk job: {kind}")
            detail = (payload or {}).get("kind") or (payload or {}).get("ticker") or ""
            with _ACT.run(f"{label}{(' — ' + str(detail)) if detail else ''}",
                          actor=("sonnet" if kind == "desk_check" else "qwen"),
                          trigger="autonomous", kind=f"desk_{kind}",
                          ticker=(payload or {}).get("ticker")):
                return self._handle_inner(kind, payload)
        except ImportError:
            return self._handle_inner(kind, payload)

    def _handle_inner(self, kind: str, payload: dict) -> dict:
        desk = _desk_cfg(self.cfg)
        mbw = float(_cfg_get(desk, ["worker", "max_busy_wait_seconds"], 300) or 300)
        if kind in ("scan", "breaking"):
            res = run_analyst_scan(llm_fn=self.llm_fn, busy_fn=self.busy_fn,
                                   notifier=self.notifier, cfg=self.cfg,
                                   sleep_fn=self.sleep_fn, clock=self.clock,
                                   max_busy_wait=mbw)
            # Phase 3: enqueue a debate pipeline item for each breaking event
            # (jumps the queue at priority 0), behind the debate flag.
            if _cfg_get(desk, ["debate", "enabled"], True):
                for nid, tk in (res.get("breaking_items") or []):
                    self.submit("debate", {"news_id": nid, "ticker": tk}, priority=0)
            return res
        if kind == "roundup":
            return build_desk_report(payload.get("kind", "hourly"),
                                     llm_fn=self.llm_fn, busy_fn=self.busy_fn,
                                     notifier=self.notifier, cfg=self.cfg,
                                     sleep_fn=self.sleep_fn, clock=self.clock,
                                     max_busy_wait=mbw)
        if kind == "selfcheck":
            import mason_selfcheck
            return mason_selfcheck.run()
        if kind == "desk_check":                     # Sonnet checks between the scheduled calls (own job: a
            import mason_desk_check                  # failure here can never reach the exit-manager tick)
            return mason_desk_check.tick(notifier=self.notifier)
        if kind == "check_grade":                    # what the price did after each check (no model call)
            import mason_desk_check
            return mason_desk_check.grade_outcomes()
        if kind == "debate":                         # Phase 3: news→thesis→debate→arbiter
            import mason_desk_pipeline as PP
            return PP.run_debate_pipeline(
                payload.get("news_id"), payload.get("ticker"),
                notifier=self.notifier, cfg=self.cfg, busy_fn=self.busy_fn,
                sleep_fn=self.sleep_fn, clock=self.clock, max_busy_wait=mbw)
        if kind == "grade":                          # Phase 3: track-record grading
            import mason_desk_pipeline as PP
            res = PP.grade_recommendations(cfg=self.cfg)
            try:                                     # Opus-director dry-run: multi-horizon
                res["director"] = PP.grade_director_predictions(cfg=self.cfg)
            except Exception as e:
                res["director_error"] = str(e)
            try:                                     # approve-to-place: journal real fills, cancel stale
                import mason_autotrade
                if (_load_config().get("autotrade") or {}).get("approve_enabled"):
                    res["autotrade_settled"] = mason_autotrade.settle_pending()
            except Exception as e:
                res["autotrade_error"] = str(e)
            return res
        if kind == "catalyst_tick":                  # Catalyst Desk: dossier warm-up + event director
            out = {}
            try:
                import mason_director as DIR
                out["dossiers_built"] = DIR.prebuild_dossiers()
                try:                                  # whole-DB memory index (no-op when flag off; throttled)
                    import mason_memory_search as MS
                    out["search_index"] = (MS.maybe_refresh() or {}).get("caught_up")
                except Exception as e:
                    out["search_index_error"] = str(e)
                out["event"] = DIR.maybe_run_event(notifier=self.notifier)
            except Exception as e:
                out["error"] = str(e)
            try:                                      # agent loop (queue drain / spine / exits); cheap no-op when off
                import mason_agent_loop
                out["agent_loop"] = mason_agent_loop.tick()
            except Exception as e:
                out["agent_loop_error"] = str(e)
            return out
        if kind == "watch":                          # watchlist: qwen digests + Alpaca two-way sync
            return run_watch_job(payload, llm_fn=self.llm_fn, busy_fn=self.busy_fn, clock=self.clock)
        if kind == "macro":                          # widen-macro: world digest + breaking
            return run_macro_digest(llm_fn=self.llm_fn, busy_fn=self.busy_fn,
                                    notifier=self.notifier, cfg=self.cfg,
                                    sleep_fn=self.sleep_fn, clock=self.clock,
                                    max_busy_wait=mbw)
        return {"skipped": f"unknown kind {kind}"}

    def run_once(self, block: bool = False, timeout: float | None = None) -> dict | None:
        """Pop and handle one item. Never raises out."""
        try:
            item = self._q.get(block=block, timeout=timeout)
        except queue.Empty:
            return None
        _, seq, kind, payload = item
        try:
            res = self._handle(kind, payload)
            if kind == "roundup" and (payload or {}).get("_anchor_t") \
                    and isinstance(res, dict) and res.get("deferred"):
                try:                                  # GPU was busy, nothing ran: give the start back
                    tk_ = "anchor_try_" + str(payload["_anchor_t"]).replace(":", "")
                    d_, _, n_ = str(get_state(tk_) or "").partition(":")
                    if d_ == payload.get("_anchor_day") and int(n_) > 0:
                        set_state(tk_, f"{d_}:{int(n_) - 1}")
                except Exception:
                    pass
            if kind == "roundup" and (payload or {}).get("_anchor_key") \
                    and not (isinstance(res, dict) and res.get("deferred")):
                try:                                  # anchor finished: now it counts as done
                    set_state(payload["_anchor_key"], payload.get("_anchor_day"))
                except Exception as e:
                    log.warning(f"[desk] anchor done-mark failed: {e}")
            return res
        except Exception as e:
            log.warning(f"[desk] worker item {kind} failed: {e}")
            return {"error": str(e), "kind": kind}
        finally:
            if kind == "roundup" and (payload or {}).get("_anchor_t"):
                self.__dict__.setdefault("_anchor_inflight", set()).discard(payload["_anchor_t"])
            try:
                self._q.task_done()
            except Exception:
                pass

    # -- daemon loops --
    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                res = self.run_once(block=True, timeout=2.0)
                if res is None:
                    continue
            except Exception as e:                  # never die
                log.warning(f"[desk] worker loop error: {e}")
                self._stop.wait(2.0)

    def _scheduler_loop(self):
        # first scan shortly after boot so the desk warms up
        booted = self.clock()
        first_done = False
        while not self._stop.is_set():
            try:
                self._scheduler_tick(first_kickoff=(not first_done and self.clock() - booted > 30))
                first_done = first_done or (self.clock() - booted > 30)
            except Exception as e:
                log.warning(f"[desk] scheduler error: {e}")
            self._stop.wait(30.0)

    def _scheduler_tick(self, first_kickoff: bool = False):
        try:                                   # Activity v2: write last hour's API summaries
            import mason_activity as _ACT
            _ACT.flush_api_summaries()
        except Exception:
            pass
        desk = _desk_cfg(self.cfg)
        now = self.clock()
        et = _now_et(now)
        today = et.strftime("%Y-%m-%d")

        # hourly delta scan
        if _cfg_get(desk, ["scan", "enabled"], True) and _cfg_get(desk, ["scan", "hourly"], True):
            last_h = get_state("last_hourly_run") or ""
            this_h = et.strftime("%Y-%m-%d-%H")
            if first_kickoff or last_h != this_h:
                self.submit("scan")
                set_state("last_hourly_run", this_h)

        # widen-macro: hourly world-macro digest (deduped by hour), behind the flag
        if _cfg_get(desk, ["macro", "enabled"], False) and _cfg_get(desk, ["macro", "digest_hourly"], True):
            last_m = get_state("last_macro_run") or ""
            this_h = et.strftime("%Y-%m-%d-%H")
            if first_kickoff or last_m != this_h:
                self.submit("macro")
                set_state("last_macro_run", this_h)

        # anchors at the configured ET times
        if _cfg_get(desk, ["anchors", "enabled"], True):
            times = anchor_times(self.cfg)
            slots_done = None
            for t in (times if isinstance(times, list) else []):
                key = "last_anchor_" + t.replace(":", "")
                hh, _, mm = t.partition(":")
                try:
                    anchor_passed = (et.hour, et.minute) >= (int(hh), int(mm))
                except Exception:
                    continue
                if anchor_passed and get_state(key) != today:
                    # Self-healing (2026-11-13): an anchor is marked done when its job
                    # FINISHES (run_once), not when it is queued. A restart that kills the
                    # job mid-call (the queue is in memory) leaves it un-done, so the next
                    # tick after boot runs it again. Guards: one in flight at a time, at
                    # most 3 starts per day (persisted, so a kill counts), and a missed
                    # anchor is dropped once a later anchor time has passed.
                    inflight = self.__dict__.setdefault("_anchor_inflight", set())
                    if t in inflight:
                        continue
                    # A call whose time was moved during the day: the same call of the day
                    # (first, second, ...) already ran or is running under its old time.
                    try:
                        if slots_done is None:
                            slots_done = anchor_slots_done(today, times)
                        slot = _slot_of(t, times)
                        if slot in slots_done or any(_slot_of(x, times) == slot for x in inflight):
                            if slot in slots_done:
                                set_state(key, today)
                                log.info(f"[desk] anchor {t}: this call of the day already ran under an earlier time")
                            continue
                    except Exception as e:
                        log.warning(f"[desk] anchor slot check failed ({e}); not starting {t} this tick")
                        continue
                    later_passed = False
                    for t2 in (times if isinstance(times, list) else []):
                        try:
                            h2, _, m2 = str(t2).partition(":")
                            if (int(h2), int(m2)) > (int(hh), int(mm)) and \
                                    (et.hour, et.minute) >= (int(h2), int(m2)):
                                later_passed = True
                        except Exception:
                            pass
                    if later_passed:
                        set_state(key, today)
                        log.info(f"[desk] anchor {t} missed today; a later anchor covers it")
                        continue
                    try_key = "anchor_try_" + t.replace(":", "")
                    tries = 0
                    try:
                        d_, _, n_ = str(get_state(try_key) or "").partition(":")
                        tries = int(n_) if d_ == today else 0
                    except Exception:
                        continue                      # cannot read the counter: do not risk extra billed starts
                    if tries >= 3:
                        set_state(key, today)
                        log.warning(f"[desk] anchor {t} started {tries}x today without finishing; giving up")
                        continue
                    set_state(try_key, f"{today}:{tries + 1}")
                    if tries:
                        log.warning(f"[desk] anchor {t} did not finish earlier today; re-running (start {tries + 1}/3)")
                    kind = anchor_kind(t)
                    inflight.add(t)
                    self.submit("roundup", {"kind": kind, "_anchor_key": key,
                                            "_anchor_day": today, "_anchor_t": t})

        # Catalyst Desk: every ~3 min, warm dossiers for fresh catalysts and check the
        # event-director gate. Enqueued only when catalyst.dossier or event_call is on,
        # so with both off the scheduler is byte-for-byte unchanged.
        try:
            import mason_director as DIR
            ccfg = DIR.catalyst_cfg()
            agent_on = False
            try:                                      # agent loop on => tick even with the catalyst flags off
                import mason_autotrade as _AT
                _ac = _AT._at_cfg()
                agent_on = bool(_ac.get("tiers_enabled") or _ac.get("queue_premarket")
                                or (_ac.get("exits") or {}).get("enabled"))
            except Exception:
                agent_on = False
            if ((ccfg.get("dossier") or {}).get("enabled")
                    or (DIR.enabled() and (ccfg.get("event_call") or {}).get("enabled"))
                    or agent_on):
                slot = f"{et.strftime('%Y-%m-%d-%H')}-{et.minute // 3}"
                if get_state("last_catalyst_tick") != slot:
                    self.submit("catalyst_tick", priority=1)
                    set_state("last_catalyst_tick", slot)
        except Exception:
            pass

        # Desk checks (mason_desk_check, Step 2): a pass every 3 minutes while desk_check.enabled,
        # as its OWN queue item, and once an evening the follow-up that fills in what the price
        # did after each check. With the switch off nothing is enqueued and nothing is imported.
        try:
            if (_load_config().get("desk_check") or {}).get("enabled") is True:
                slot3 = f"{et.strftime('%Y-%m-%d-%H')}-{et.minute // 3}"
                if get_state("last_desk_check_tick") != slot3:
                    self.submit("desk_check")
                    set_state("last_desk_check_tick", slot3)
                if (et.hour, et.minute) >= (18, 40) and get_state("last_check_grade") != today:
                    self.submit("check_grade")
                    set_state("last_check_grade", today)
        except Exception:
            pass

        # Watchlist (docs/watchlist/WATCHLIST_DESIGN.md R9/R13): qwen digests at the
        # configured ET slots on trading days, event digests + Alpaca sync pull every
        # 5 min. Enqueued only while watchlist.enabled, so the scheduler is otherwise
        # byte-for-byte unchanged.
        try:
            wcfg = (_load_config().get("watchlist") or {})
            if wcfg.get("enabled"):
                import mason_director as DIR
                slots = ((wcfg.get("digests") or {}).get("slots_et") or {})
                if (wcfg.get("digests") or {}).get("enabled") and DIR.is_trading_day(et):
                    for slot, at in (slots.items() if isinstance(slots, dict) else []):
                        key = f"last_watch_digest_{slot}"
                        if _watch_slot_due(et, at) and get_state(key) != today:
                            self.submit("watch", {"digest": slot})
                            set_state(key, today)
                five = f"{et.strftime('%Y-%m-%d-%H')}-{et.minute // 5}"
                if get_state("last_watch_tick") != five:
                    self.submit("watch", {"tick": five})
                    set_state("last_watch_tick", five)
        except Exception:
            pass

        # Self-check (2026-11-13): every 15 min compare journal/schedule against Alpaca and
        # the ledger, repair what Alpaca proves, report the rest. No model call.
        try:
            q15 = f"{et.strftime('%Y-%m-%d-%H')}-{et.minute // 15}"
            if get_state("last_selfcheck_tick") != q15:
                self.submit("selfcheck", priority=3)
                set_state("last_selfcheck_tick", q15)
        except Exception:
            pass

        # ── RH Expansion S2-T8 ──
        # RHX module jobs (S1 + S2 jobs() + the personal reconcile), each gated by
        # robinhood.enabled and its own flag. Every flag off → tick() returns at once: no job, no thread,
        # no DB access, no RH call. Due jobs run on mason_rhx_integration's own lanes, not this desk queue.
        try:
            import mason_rhx_integration as RHX
            RHX.tick(now)
        except Exception as e:
            log.warning(f"[desk] RH expansion jobs tick failed: {e}")
        # ── end S2-T8 ──

        # Phase 3: track-record grading, roughly every 30 min (deduped by half-hour)
        if _cfg_get(desk, ["recommendations", "enabled"], True):
            half = f"{et.strftime('%Y-%m-%d-%H')}-{0 if et.minute < 30 else 30}"
            if first_kickoff or get_state("last_grade_run") != half:
                self.submit("grade")
                set_state("last_grade_run", half)

    # -- lifecycle --
    def start(self):
        if self._worker and self._worker.is_alive():
            return False
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="analyst-desk-worker")
        self._scheduler = threading.Thread(target=self._scheduler_loop, daemon=True,
                                           name="analyst-desk-scheduler")
        self._worker.start()
        self._scheduler.start()
        log.info("[desk] analyst desk started (worker + scheduler)")
        return True

    def stop(self):
        self._stop.set()


# ── module singleton + start behind config flags ───────────────────────────────

_desk_singleton: AnalystDesk | None = None
_start_lock = threading.Lock()


def get_desk() -> AnalystDesk | None:
    return _desk_singleton


def start(config: dict | None = None) -> AnalystDesk | None:
    """Start the desk (worker + scheduler) behind the analyst_desk config flags.
    Returns the desk if started, None if fully disabled or already running."""
    global _desk_singleton
    desk_cfg = _desk_cfg(config)
    scan_on = _cfg_get(desk_cfg, ["scan", "enabled"], True)
    anchors_on = _cfg_get(desk_cfg, ["anchors", "enabled"], True)
    if not scan_on and not anchors_on:
        log.info("[desk] disabled by config (scan + anchors both off)")
        return None
    with _start_lock:
        if _desk_singleton is not None and _desk_singleton._worker \
                and _desk_singleton._worker.is_alive():
            return None
        _desk_singleton = AnalystDesk(cfg=config)
        _desk_singleton.start()
    return _desk_singleton


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    what = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if what == "scan":
        print(json.dumps(run_analyst_scan(max_busy_wait=0), indent=2, default=str))
    elif what == "report":
        kind = sys.argv[2] if len(sys.argv) > 2 else "hourly"
        print(json.dumps(build_desk_report(kind, max_busy_wait=0), indent=2, default=str))
    elif what == "contexts":
        for k, v in build_contexts().items():
            print(f"\n=== {k} ===\n{v}")
