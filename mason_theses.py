"""
mason_theses.py — the MEMORY SPINE: long-lived theses + a deterministic matcher.

A *thesis* is a "thing we're watching for" that outlives the 30-day news window:
    "NVDA is accelerating consumer-GPU supply" — opened 09-28 off an early hint
    headline, with confirm_signals / break_signals and a review_by date.

When a later headline lands that matches it (ticker overlap AND enough signal hits),
Mason fires ONE inbox notification per thesis per verdict:

    "Remember the NVDA consumer-GPU thesis from 09-28? Confirmed by: <headline>.
     Your NVDA (Roth 3.23 sh @ $224.30) is +x%; director call NVDA long +y%."

Who creates theses (origin):
    opus_director  — Opus JSON field "theses" (apply_director_theses), status 'watching'
    nico           — chat tool add_watch_thesis ("keep an eye on NVDA consumer GPUs")
    qwen_proposal  — low-trust; ALWAYS stored as 'proposed' (never matched until promoted)

Status vocabulary: proposed | watching | confirmed | broken | expired | closed.

Everything is deterministic (no LLM). Flag `memory.theses.enabled` is read per call;
off ⇒ writers/matcher/section are no-ops. Nothing here raises to the caller, and every
DB connection this module opens is closed in a finally.
Contracts: docs/catalyst_desk/WEALTH_MANAGER_CONTRACTS.md (builder T).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import mason_db

log = logging.getLogger("mason.theses")

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
_CREATED_BY = "mason_theses"

STATUSES = ("proposed", "watching", "confirmed", "broken", "expired", "closed")
OPEN_STATUSES = ("proposed", "watching")
ORIGINS = ("opus_director", "nico", "qwen_proposal")
DIRECTIONS = ("bullish", "bearish", "neutral")
_DIR_ALIASES = {"long": "bullish", "bull": "bullish", "up": "bullish", "positive": "bullish",
                "short": "bearish", "bear": "bearish", "down": "bearish", "negative": "bearish",
                "flat": "neutral", "mixed": "neutral", "": "neutral"}

_WATERMARK_KEY = "theses_news_watermark"
_MAX_SIGNALS = 12
_MAX_DIRECTOR_THESES = 3
_MAX_EVENTS_PER_RUN = 1000
_MAX_ALERTS_PER_RUN = 10
_FIRST_RUN_LOOKBACK_S = 72 * 3600

# ── config (read per call, mtime-cached) ────────────────────────────────────────
_cfg_cache = {"mtime": None, "data": {}}
_DEFAULTS = {"enabled": False, "match_min_overlap": 2, "default_review_days": 45}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        th = ((_config().get("memory") or {}).get("theses") or {})
        out.update({k: v for k, v in th.items() if v is not None})
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return bool(_cfg().get("enabled", False))
    except Exception:
        return False


def retention_keep_threshold() -> float:
    """memory.retention.keep_market_material (default 0.6) — used by
    mason_news_service.prune_old. Never raises."""
    try:
        v = ((_config().get("memory") or {}).get("retention") or {}).get("keep_market_material")
        return float(v) if v is not None else 0.6
    except Exception:
        return 0.6


# ── small helpers ───────────────────────────────────────────────────────────────

def _now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time()).isoformat(timespec="seconds")


def _today(now: float | None = None) -> date:
    return datetime.fromtimestamp(now if now is not None else time.time()).date()


def _loads(s, default):
    if s is None or s == "":
        return default
    if isinstance(s, (list, dict)):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _iso_epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def _short_date(s) -> str:
    s = str(s or "")
    return s[5:10] if len(s) >= 10 else s


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return []
        if v.startswith("["):
            parsed = _loads(v, [])
            if parsed:
                return parsed
        return [p for p in re.split(r"[,;|\n]", v) if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from",
         "is", "are", "was", "be", "its", "it", "as", "that", "this", "will", "into", "than", "over",
         "about", "after", "new", "says", "say", "said", "more", "less", "up", "down", "watch",
         "keep", "eye", "thesis", "we", "our", "us", "nico", "mason"}
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 6:
        return w[:6]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _tokens(text: str) -> list:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t]


def _content_stems(text: str) -> list:
    out = []
    for t in _tokens(text):
        if t in _STOP or (len(t) < 3 and not t.isdigit()):
            continue
        s = _stem(t)
        if s and s not in out:
            out.append(s)
    return out


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(_content_stems(a)), set(_content_stems(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _clean_signals(v) -> list:
    out, seen = [], set()
    for s in _as_list(v):
        s = re.sub(r"\s+", " ", str(s or "")).strip().strip("\"'")
        if len(s) < 2 or len(s) > 80:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= _MAX_SIGNALS:
            break
    return out


def _norm_direction(d) -> str | None:
    d = str(d or "").strip().lower()
    if d in DIRECTIONS:
        return d
    return _DIR_ALIASES.get(d)


def _parse_review_by(v, now: float | None, default_days: int) -> str:
    today = _today(now)
    try:
        d = datetime.strptime(str(v or "")[:10], "%Y-%m-%d").date()
        if today < d <= today + timedelta(days=366):
            return d.isoformat()
    except Exception:
        pass
    return (today + timedelta(days=int(default_days or 45))).isoformat()


# ── ticker validation (reuses mason_triage's resolver; never edits it) ─────────
_SYM_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def _known_symbols() -> set:
    known = set()
    try:
        import mason_triage
        known |= set(mason_triage.broad_universe(allow_refresh=False).keys())
        try:
            known |= set(mason_triage._resolver_index().get("known") or ())
        except Exception:
            pass
    except Exception:
        pass
    try:
        known |= {str(p.get("ticker") or "").upper() for p in _personal_positions()}
    except Exception:
        pass
    known.discard("CASH")
    return known


def resolve_thesis_tickers(values, *, allowed: set | None = None) -> tuple[list, list]:
    """(tickers, rejected). Symbols pass when known to the broad universe / EDGAR /
    Nico's book; company names ('Nvidia') resolve via mason_triage.resolve_tickers.
    Placeholder/hallucination shapes ('TICKER1') are rejected. `allowed` (director)
    further restricts. Never raises."""
    out, rejected = [], []
    try:
        known = _known_symbols()
    except Exception:
        known = set()
    for raw in _as_list(values):
        s = str(raw or "").strip().lstrip("$")
        if not s:
            continue
        got = []
        up = s.upper()
        if _SYM_RE.match(up) and (up in known or (not known and s == up)):
            got = [up]
        if not got:
            try:
                import mason_triage
                cand = s if any(c.isupper() for c in s) else s.title()
                got = [t for t in mason_triage.resolve_tickers(cand) if t]
            except Exception:
                got = []
        if not got and _SYM_RE.match(up) and s == up and not known:
            got = [up]
        if not got:
            rejected.append(s)
            continue
        for t in got:
            t = t.upper()
            if allowed is not None and t not in allowed:
                rejected.append(t)
                continue
            if t not in out:
                out.append(t)
    return out[:8], rejected


# ── row shaping ─────────────────────────────────────────────────────────────────

def _shape(row) -> dict:
    r = dict(row)
    for k in ("tickers", "confirm_signals", "break_signals", "source_refs", "linked_recs", "match_log"):
        r[k] = _loads(r.get(k), [])
    r["extra"] = _loads(r.pop("extra_json", None), {})
    for k in ("record_deleted", "deleted_date", "deleted_by"):
        r.pop(k, None)
    return r


def _get(conn, thesis_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM theses WHERE id=? AND record_deleted=0", (thesis_id,)).fetchone()
    return _shape(row) if row else None


def _derive_signals(title: str, thesis: str, tickers: list) -> list:
    tk = {t.lower() for t in tickers}
    words = [w for w in _tokens(f"{title} {thesis}") if w not in _STOP and len(w) >= 3 and w not in tk]
    out = []
    for w in words:
        if w not in out:
            out.append(w)
    return out[:6]


# ── CRUD ────────────────────────────────────────────────────────────────────────

def create_thesis(title, thesis, tickers=None, direction="neutral", confirm_signals=None,
                  break_signals=None, origin="nico", source_refs=None, review_by=None,
                  status=None, *, linked_recs=None, extra=None, allowed_tickers=None,
                  created_by=None, now: float | None = None, conn=None) -> dict:
    """Validate + insert one thesis. Returns {"ok": True, "thesis": {...}} or
    {"ok": False, "error": "..."}. qwen_proposal ⇒ status forced to 'proposed'.
    Never raises."""
    try:
        if not enabled():
            return {"ok": False, "error": "theses disabled (memory.theses.enabled is off)"}
        cfg = _cfg()
        title = re.sub(r"\s+", " ", str(title or "")).strip()[:120]
        thesis = re.sub(r"\s+", " ", str(thesis or "")).strip()[:800]
        if not thesis and title:
            thesis = title
        if not title and thesis:
            title = thesis[:80]
        if not title:
            return {"ok": False, "error": "a thesis needs a title or description"}
        origin = str(origin or "nico").strip().lower()
        if origin not in ORIGINS:
            return {"ok": False, "error": f"origin must be one of {ORIGINS}"}
        d = _norm_direction(direction)
        if d is None:
            return {"ok": False, "error": f"direction must be one of {DIRECTIONS}"}
        tks, rejected = resolve_thesis_tickers(tickers, allowed=allowed_tickers)
        if _as_list(tickers) and not tks:
            return {"ok": False, "error": f"none of the tickers resolved: {rejected}"}
        conf = _clean_signals(confirm_signals) or _derive_signals(title, thesis, tks)
        brk = _clean_signals(break_signals)
        if not conf:
            return {"ok": False, "error": "no confirm signals (phrases that would confirm it)"}
        if origin == "qwen_proposal":
            st = "proposed"
        else:
            st = str(status or "watching").strip().lower()
            if st not in OPEN_STATUSES:
                st = "watching"
        refs = [r for r in _as_list(source_refs) if isinstance(r, dict)][:10]
        tid = uuid.uuid4().hex[:12]
        row = (tid, json.dumps(tks), title, thesis, d, json.dumps(conf), json.dumps(brk),
               json.dumps(refs), origin, st,
               _parse_review_by(review_by, now, cfg.get("default_review_days", 45)),
               json.dumps([str(x) for x in _as_list(linked_recs)][:20]), None, json.dumps([]),
               json.dumps(dict(extra or {}, rejected_tickers=rejected) if rejected else (extra or {})),
               _now_iso(now), created_by or origin)
        own = conn is None
        c = mason_db.get_connection() if own else conn
        try:
            with c:
                c.execute(
                    "INSERT INTO theses (id, tickers, title, thesis, direction, confirm_signals, "
                    "break_signals, source_refs, origin, status, review_by, linked_recs, "
                    "last_match_date, match_log, extra_json, created_date, created_by, record_deleted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)", row)
            out = _get(c, tid)
        finally:
            if own:
                c.close()
        res = {"ok": True, "thesis": out}
        if rejected:
            res["rejected_tickers"] = rejected
        return res
    except Exception as e:
        log.warning(f"[theses] create failed: {e}")
        return {"ok": False, "error": str(e)}


def list_theses(status=None, tickers=None, *, limit: int = 50, conn=None) -> list:
    """Non-deleted theses, newest first. status: None (all) | 'open' (proposed+watching)
    | one status | list. tickers: filter to theses touching any of them. Never raises."""
    try:
        if status == "open":
            sts = list(OPEN_STATUSES)
        elif isinstance(status, str) and status:
            sts = [status]
        elif isinstance(status, (list, tuple, set)):
            sts = list(status)
        else:
            sts = None
        q = "SELECT * FROM theses WHERE record_deleted=0"
        args: list = []
        if sts:
            q += f" AND status IN ({','.join('?' * len(sts))})"
            args += sts
        q += " ORDER BY created_date DESC LIMIT ?"
        args.append(max(1, int(limit)) * (4 if tickers else 1))
        own = conn is None
        c = mason_db.get_connection() if own else conn
        try:
            rows = [_shape(r) for r in c.execute(q, args).fetchall()]
        finally:
            if own:
                c.close()
        if tickers:
            want = {str(t).upper() for t in _as_list(tickers)}
            rows = [r for r in rows if want & set(r["tickers"])]
        return rows[:int(limit)]
    except Exception as e:
        log.warning(f"[theses] list failed: {e}")
        return []


_UPDATABLE = {"title", "thesis", "tickers", "direction", "confirm_signals", "break_signals",
              "review_by", "status", "linked_recs", "source_refs", "extra"}


def update_thesis(thesis_id: str, *, now: float | None = None, conn=None, **fields) -> dict:
    """Patch a thesis. Validates each field like create. {"ok", "thesis"|"error"}."""
    try:
        if not enabled():
            return {"ok": False, "error": "theses disabled"}
        bad = set(fields) - _UPDATABLE
        if bad:
            return {"ok": False, "error": f"unknown fields: {sorted(bad)}"}
        own = conn is None
        c = mason_db.get_connection() if own else conn
        try:
            cur = _get(c, thesis_id)
            if not cur:
                return {"ok": False, "error": f"no thesis {thesis_id}"}
            sets = {}
            if "title" in fields:
                t = re.sub(r"\s+", " ", str(fields["title"] or "")).strip()[:120]
                if not t:
                    return {"ok": False, "error": "empty title"}
                sets["title"] = t
            if "thesis" in fields:
                t = re.sub(r"\s+", " ", str(fields["thesis"] or "")).strip()[:800]
                if not t:
                    return {"ok": False, "error": "empty thesis"}
                sets["thesis"] = t
            if "tickers" in fields:
                tks, rej = resolve_thesis_tickers(fields["tickers"])
                if _as_list(fields["tickers"]) and not tks:
                    return {"ok": False, "error": f"none of the tickers resolved: {rej}"}
                sets["tickers"] = json.dumps(tks)
            if "direction" in fields:
                d = _norm_direction(fields["direction"])
                if d is None:
                    return {"ok": False, "error": "bad direction"}
                sets["direction"] = d
            for k in ("confirm_signals", "break_signals"):
                if k in fields:
                    sig = _clean_signals(fields[k])
                    if k == "confirm_signals" and not sig:
                        return {"ok": False, "error": "confirm_signals cannot be empty"}
                    sets[k] = json.dumps(sig)
            if "review_by" in fields:
                sets["review_by"] = _parse_review_by(fields["review_by"], now,
                                                     _cfg().get("default_review_days", 45))
            if "status" in fields:
                st = str(fields["status"] or "").strip().lower()
                if st not in STATUSES:
                    return {"ok": False, "error": f"status must be one of {STATUSES}"}
                sets["status"] = st
            if "linked_recs" in fields:
                sets["linked_recs"] = json.dumps([str(x) for x in _as_list(fields["linked_recs"])][:20])
            if "source_refs" in fields:
                sets["source_refs"] = json.dumps([r for r in _as_list(fields["source_refs"])
                                                  if isinstance(r, dict)][:10])
            if "extra" in fields and isinstance(fields["extra"], dict):
                ex = dict(cur.get("extra") or {})
                ex.update(fields["extra"])
                sets["extra_json"] = json.dumps(ex)
            if sets:
                with c:
                    c.execute(f"UPDATE theses SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                              (*sets.values(), thesis_id))
            return {"ok": True, "thesis": _get(c, thesis_id)}
        finally:
            if own:
                c.close()
    except Exception as e:
        log.warning(f"[theses] update failed: {e}")
        return {"ok": False, "error": str(e)}


def close_thesis(thesis_id: str, *, reason: str = "", status: str = "closed",
                 by: str = "nico", now: float | None = None, conn=None) -> dict:
    """Close (or mark expired/confirmed/broken by hand) a thesis."""
    if status not in ("closed", "expired", "confirmed", "broken"):
        return {"ok": False, "error": "status must be closed|expired|confirmed|broken"}
    return update_thesis(thesis_id, status=status, now=now, conn=conn,
                         extra={"closed_reason": str(reason or "")[:300], "closed_by": by,
                                "closed_date": _now_iso(now)})


def expire_due(*, now: float | None = None, conn=None) -> int:
    """Open theses whose review_by has passed → 'expired'. Returns count. Never raises."""
    try:
        if not enabled():
            return 0
        own = conn is None
        c = mason_db.get_connection() if own else conn
        try:
            with c:
                cur = c.execute(
                    "UPDATE theses SET status='expired' WHERE record_deleted=0 AND "
                    "status IN ('proposed','watching') AND review_by IS NOT NULL AND review_by < ?",
                    (_today(now).isoformat(),))
            return cur.rowcount or 0
        finally:
            if own:
                c.close()
    except Exception as e:
        log.warning(f"[theses] expire failed: {e}")
        return 0


# ── Opus director JSON ("theses": [...]) ────────────────────────────────────────

def apply_director_theses(items, *, slate_id: str = "", allowed_tickers=None,
                          now: float | None = None, max_items: int = _MAX_DIRECTOR_THESES) -> list:
    """Validate Opus JSON items {title, thesis, tickers, direction, confirm_signals[],
    break_signals[], review_by} → create (origin opus_director). ≤3 per run; tickers
    must be in allowed_tickers (when given); needs ≥1 confirm signal; deduped against
    open theses (ticker overlap + title similarity) and within the batch.
    Returns the created thesis dicts. Never raises."""
    out = []
    try:
        if not enabled() or not isinstance(items, list):
            return out
        allowed = {str(t).upper() for t in allowed_tickers} if allowed_tickers else None
        existing = list_theses(status="open", limit=200)
        for it in items[:max(0, int(max_items))]:
            try:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or "").strip()
                body = str(it.get("thesis") or "").strip()
                conf = _clean_signals(it.get("confirm_signals"))
                if not title or not body or not conf:
                    continue
                if _norm_direction(it.get("direction")) is None:
                    continue
                if not isinstance(it.get("confirm_signals"), list):
                    continue
                raw_tk = _as_list(it.get("tickers"))
                tks, _rej = resolve_thesis_tickers(raw_tk, allowed=allowed)
                if raw_tk and not tks:
                    continue
                if _is_duplicate(title, body, tks, existing + [x for x in out]):
                    continue
                res = create_thesis(title, body, tks, it.get("direction"), conf,
                                    it.get("break_signals"), origin="opus_director",
                                    source_refs=[{"table": "director_slate", "id": slate_id,
                                                  "date": _now_iso(now)[:10]}],
                                    review_by=it.get("review_by"), created_by="opus_director",
                                    extra={"slate_id": slate_id}, now=now)
                if res.get("ok"):
                    out.append(res["thesis"])
            except Exception as e:
                log.warning(f"[theses] director item skipped ({it}): {e}")
    except Exception as e:
        log.warning(f"[theses] apply_director_theses failed: {e}")
    return out


def _is_duplicate(title: str, body: str, tickers: list, existing: list) -> bool:
    for ex in existing:
        ex_t = set(ex.get("tickers") or [])
        same_scope = (set(tickers) & ex_t) if tickers else not ex_t
        if not same_scope:
            continue
        if (_jaccard(title, ex.get("title", "")) >= 0.5 or
                _jaccard(f"{title} {body}", f"{ex.get('title', '')} {ex.get('thesis', '')}") >= 0.45):
            return True
    return False


# ── the matcher ─────────────────────────────────────────────────────────────────

def _signal_hit(signal: str, text_lower: str, stems: set) -> bool:
    s = signal.lower().strip()
    if not s:
        return False
    if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text_lower):
        return True
    need = _content_stems(s)
    return bool(need) and all(n in stems for n in need)


def score_event(thesis: dict, headline: str, desc: str = "", event_tickers=None,
                *, min_overlap: int = 2) -> dict:
    """Pure, deterministic verdict for one (thesis, headline) pair:
    {"verdict": confirm|break|related|None, "confirm_hits": [...], "break_hits": [...],
     "ticker_overlap": [...]}. Ticker theses need ticker overlap; ticker-less (macro)
    theses need min_overlap+1 signal hits instead."""
    text = f"{headline or ''} {desc or ''}"
    tl = text.lower()
    stems = {_stem(t) for t in _tokens(tl)}
    ch = [s for s in thesis.get("confirm_signals") or [] if _signal_hit(s, tl, stems)]
    bh = [s for s in thesis.get("break_signals") or [] if _signal_hit(s, tl, stems)]
    th_tk = set(thesis.get("tickers") or [])
    ev_tk = {str(t).upper() for t in (event_tickers or [])}
    overlap = sorted(th_tk & ev_tk)
    out = {"verdict": None, "confirm_hits": ch, "break_hits": bh, "ticker_overlap": overlap}
    need = int(min_overlap)
    if th_tk:
        if not overlap:
            return out
    else:
        need += 1
    if len(bh) >= need and len(bh) >= len(ch):
        out["verdict"] = "break"
    elif len(ch) >= need and len(ch) > len(bh):
        out["verdict"] = "confirm"
    elif ch or bh:
        out["verdict"] = "related" if th_tk or (len(ch) + len(bh)) >= need else None
    return out


def _get_state(conn, key):
    row = conn.execute("SELECT value FROM desk_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key, value):
    with conn:
        conn.execute(
            "INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
            (key, str(value), datetime.now().isoformat()))


def match_new_events(conn=None, *, now: float | None = None, cap: int = _MAX_EVENTS_PER_RUN,
                     notify: bool = True) -> dict:
    """Scan news_events newer than the watermark against 'watching' theses.
    confirm/break ⇒ match_log + last_match_date + status confirmed/broken + ONE inbox
    notification (dedup_key thesis:<id>:<verdict>) with position context. related ⇒
    match_log only. Also expires theses past review_by. Bounded; never raises.
    Returns {"matched": [...], "alerts": [...], "expired": n, "scanned": n}."""
    result = {"matched": [], "alerts": [], "expired": 0, "scanned": 0}
    try:
        if not enabled():
            return result
    except Exception:
        return result
    own = conn is None
    try:
        if own:
            conn = mason_db.get_connection()
        now = float(now if now is not None else time.time())
        _match(conn, now, int(cap), result, notify)
    except Exception as e:
        log.warning(f"[theses] match run failed: {e}")
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result


def _match(conn, now, cap, result, notify):
    cfg = _cfg()
    min_overlap = max(1, int(cfg.get("match_min_overlap", 2) or 2))
    result["expired"] = expire_due(now=now, conn=conn)
    theses = [t for t in list_theses(status="watching", limit=500, conn=conn)]
    try:
        wm = float(_get_state(conn, _WATERMARK_KEY) or 0)
    except Exception:
        wm = 0.0
    if wm <= 0:
        wm = now - _FIRST_RUN_LOOKBACK_S
    rows = conn.execute(
        "SELECT id, headline, source, tickers, resolved_tickers, raw, ingested_at, published_at, "
        "catalyst_id FROM news_events WHERE ingested_at > ? AND ingested_at <= ? "
        "AND COALESCE(is_deleted,0)=0 ORDER BY ingested_at ASC LIMIT ?",
        (wm, now, cap)).fetchall()
    result["scanned"] = len(rows)
    if not rows:
        return
    new_wm = max(float(r["ingested_at"]) for r in rows)
    if not theses:
        _set_state(conn, _WATERMARK_KEY, new_wm)
        return
    for t in theses:
        t["_since"] = _iso_epoch(t.get("created_date")) or 0.0
        t["_skip"] = {str(r.get("id")) for r in t.get("source_refs") or [] if isinstance(r, dict)}
        t["_skip"] |= {str(m.get("news_id")) for m in t.get("match_log") or []}
    fired = {}                                     # thesis id -> verdict already decided this run
    pending = []
    for r in rows:
        raw = _loads(r["raw"], {})
        desc = str(raw.get("desc") or "")[:1500]
        ev_tk = set(_loads(r["tickers"], [])) | set(_loads(r["resolved_tickers"], []))
        resolved_lazily = r["resolved_tickers"] is not None
        for t in theses:
            if t["id"] in fired or r["id"] in t["_skip"] or float(r["ingested_at"]) < t["_since"]:
                continue
            sc = score_event(t, r["headline"], desc, ev_tk, min_overlap=min_overlap)
            if (not sc["verdict"] and not sc["ticker_overlap"] and not resolved_lazily
                    and (sc["confirm_hits"] or sc["break_hits"]) and t.get("tickers")):
                try:                               # triage off / not yet run: resolve ourselves
                    import mason_triage
                    ev_tk |= set(mason_triage.resolve_tickers(r["headline"], desc))
                except Exception:
                    pass
                resolved_lazily = True
                sc = score_event(t, r["headline"], desc, ev_tk, min_overlap=min_overlap)
            if not sc["verdict"]:
                continue
            t["_skip"].add(r["id"])
            pending.append((t, r, sc))
            if sc["verdict"] in ("confirm", "break"):
                fired[t["id"]] = sc["verdict"]
    for t, r, sc in pending:
        try:
            entry = _record_match(conn, t, r, sc, now)
            if entry is None:
                continue
            result["matched"].append(entry)
            if (notify and sc["verdict"] in ("confirm", "break")
                    and len(result["alerts"]) < _MAX_ALERTS_PER_RUN):
                n = _notify(t, r, sc, now, conn)
                if n:
                    result["alerts"].append(n)
        except Exception as e:
            log.warning(f"[theses] match record failed ({t.get('id')}/{r['id']}): {e}")
    _set_state(conn, _WATERMARK_KEY, new_wm)


def _event_date(r) -> str:
    ts = r["published_at"] or r["ingested_at"]
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except Exception:
        return ""


def _record_match(conn, t, r, sc, now) -> dict | None:
    verdict = sc["verdict"]
    cur = _get(conn, t["id"])
    if not cur or cur["status"] != "watching":
        return None
    log_ = list(cur.get("match_log") or [])
    if any(m.get("news_id") == r["id"] for m in log_):
        return None
    entry = {"date": _now_iso(now), "news_id": r["id"], "headline": r["headline"][:300],
             "source": r["source"] or "", "event_date": _event_date(r), "verdict": verdict,
             "confirm_hits": sc["confirm_hits"], "break_hits": sc["break_hits"],
             "catalyst_id": r["catalyst_id"]}
    log_.append(entry)
    log_ = log_[-50:]
    sets = {"match_log": json.dumps(log_)}
    if verdict in ("confirm", "break"):
        sets["last_match_date"] = _now_iso(now)
        sets["status"] = "confirmed" if verdict == "confirm" else "broken"
        recs = _director_recs(conn, cur.get("tickers") or [])
        linked = list(cur.get("linked_recs") or [])
        for rec in recs:
            if rec["id"] not in linked:
                linked.append(rec["id"])
        sets["linked_recs"] = json.dumps(linked[:20])
    with conn:
        conn.execute(f"UPDATE theses SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                     (*sets.values(), t["id"]))
    return {"thesis_id": t["id"], "title": t["title"], "tickers": t.get("tickers") or [],
            "verdict": verdict, "news_id": r["id"], "headline": r["headline"],
            "catalyst_id": r["catalyst_id"], "confirm_hits": sc["confirm_hits"],
            "break_hits": sc["break_hits"]}


def _bl(label):
    """N6b: a label as a MODEL sees it - the ••NNNN mask dropped (mason_robinhood.brain_label); the stored and UI labels
    keep it. Empty/None stay as they are. If the shared function cannot be loaded the label is replaced, never leaked."""
    if label is None or label == "":
        return label
    try:
        import mason_robinhood
        return mason_robinhood.brain_label(label)
    except Exception:
        return "Account"


# ── position context (each source is a patchable seam) ──────────────────────────

def _alpaca_positions() -> list:
    import alpaca_integration
    return alpaca_integration.get_positions()


def _personal_positions() -> list:
    import mason_personal
    return mason_personal.get_open_positions()


def _live_price(ticker: str) -> float | None:
    import mason_brief
    q = mason_brief.get_quote_yf(ticker) or {}
    p = q.get("price")
    return float(p) if p else None


def _director_recs(conn, tickers) -> list:
    if not tickers:
        return []
    try:
        q = ("SELECT id, created_date, created_by, ticker, direction, entry_ref_price, extra_json "
             "FROM recommendations WHERE kind='director' AND status='open' "
             f"AND COALESCE(record_deleted,0)=0 AND ticker IN ({','.join('?' * len(tickers))}) "
             "ORDER BY created_date DESC LIMIT 10")
        return [dict(r) for r in conn.execute(q, list(tickers)).fetchall()]
    except Exception:
        return []


def _pct(a, b) -> float | None:
    try:
        return (float(a) / float(b) - 1.0) * 100.0 if a and b else None
    except Exception:
        return None


def _fmt_pct(p) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


def _fmt_sh(x) -> str:
    try:
        x = float(x)
        return f"{x:.0f}" if abs(x - round(x)) < 1e-6 else f"{x:.2f}"
    except Exception:
        return str(x)


_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


def position_context(tickers, *, conn=None, price_cache: dict | None = None) -> list:
    """Human lines of Nico's exposure on `tickers`: Alpaca paper positions (incl.
    options on the underlying), personal accounts (live % via get_quote_yf) and open
    director calls (entry vs live, sign-adjusted for shorts). Never raises."""
    tickers = [str(t).upper() for t in (tickers or []) if t]
    if not tickers:
        return []
    cache = price_cache if price_cache is not None else {}

    def price(t):
        if t not in cache:
            try:
                cache[t] = _live_price(t)
            except Exception:
                cache[t] = None
        return cache[t]

    lines = []
    try:
        for p in _alpaca_positions() or []:
            sym = str(p.get("ticker") or "").upper()
            m = _OCC_RE.match(sym)
            und = m.group(1) if m else sym
            if und not in tickers:
                continue
            plpc = p.get("unrealized_plpc")
            pl = p.get("unrealized_pl")
            what = f"{sym} option" if m else f"{sym}"
            lines.append(f"Alpaca paper: {what} {_fmt_sh(p.get('qty'))} "
                         f"{('contract' if float(p.get('qty') or 0) == 1 else 'contracts') if m else 'sh'} @ ${float(p.get('avg_entry') or 0):,.2f} → "
                         f"{_fmt_pct(plpc)}" + (f" ({'+' if pl >= 0 else '-'}${abs(pl):,.2f})"
                                                if isinstance(pl, (int, float)) else ""))
    except Exception as e:
        log.info(f"[theses] alpaca positions unavailable: {e}")
    try:
        for p in _personal_positions() or []:
            t = str(p.get("ticker") or "").upper()
            if t not in tickers or t == "CASH":
                continue
            live = price(t)
            cb = p.get("cost_basis")
            pc = _pct(live, cb)
            gain = (live - float(cb)) * float(p.get("shares") or 0) if (live and cb) else None
            lines.append(f"{_bl(p.get('account_label')) or 'Personal'}: {t} {_fmt_sh(p.get('shares'))} sh "
                         f"@ ${float(cb or 0):,.2f} → {_fmt_pct(pc)}"
                         + (f" ({'+' if gain >= 0 else '-'}${abs(gain):,.2f})" if gain is not None else ""))
    except Exception as e:
        log.info(f"[theses] personal positions unavailable: {e}")
    own = conn is None
    c = None
    try:
        c = mason_db.get_connection() if own else conn
        for rec in _director_recs(c, tickers):
            t = rec["ticker"]
            live = price(t)
            pc = _pct(live, rec.get("entry_ref_price"))
            if pc is not None and str(rec.get("direction")) in ("short", "exit", "trim"):
                pc = -pc
            who = "Opus" if rec.get("created_by") == "opus_director" else (
                "qwen" if rec.get("created_by") == "qwen_director" else rec.get("created_by") or "director")
            lines.append(f"Director call ({who}) {t} {rec.get('direction')} from "
                         f"{_short_date(rec.get('created_date'))} @ ${float(rec.get('entry_ref_price') or 0):,.2f}"
                         f" → {_fmt_pct(pc)}")
    except Exception as e:
        log.info(f"[theses] director recs unavailable: {e}")
    finally:
        if own and c is not None:
            try:
                c.close()
            except Exception:
                pass
    return lines


def render_alert(thesis: dict, event: dict, verdict: str, positions: list,
                 hits: list | None = None) -> tuple[str, str]:
    """(title, body) for a confirm/break alert. Pure — tests render it directly."""
    tk = ", ".join(thesis.get("tickers") or []) or "macro"
    opened = _short_date(thesis.get("created_date"))
    src = next((r for r in thesis.get("source_refs") or []
                if isinstance(r, dict) and r.get("headline")), None)
    word = "CONFIRMED" if verdict == "confirm" else "BROKEN"
    title = f"Thesis {word}: {thesis.get('title')} ({tk})"[:160]
    lines = [f"Remember the {thesis.get('title')} thesis from {opened}"
             + (f" (sparked by: \"{src['headline']}\"" + (f", {_short_date(src.get('date'))}" if src.get("date") else "")
                + ")" if src else "") + "?",
             f"Thesis ({thesis.get('direction') or 'neutral'}): {thesis.get('thesis')}",
             f"{'Confirmed' if verdict == 'confirm' else 'Broken'} by: \"{event.get('headline')}\""
             + (f" — {event.get('source')}" if event.get("source") else "")
             + (f", {_short_date(event.get('event_date'))}" if event.get("event_date") else "")]
    if hits:
        lines.append("Signals hit: " + ", ".join(hits))
    if positions:
        lines.append(f"Your book on {tk}:")
        lines += [f"  • {p}" for p in positions]
    else:
        lines.append(f"No open position or director call on {tk}.")
    return title, "\n".join(lines)


def _notify(t, r, sc, now, conn) -> dict | None:
    verdict = sc["verdict"]
    try:
        cur = _get(conn, t["id"]) or t
        ex = dict(cur.get("extra") or {})
        notified = dict(ex.get("notified") or {})
        if verdict in notified:
            return None
        event = {"headline": r["headline"], "source": r["source"], "event_date": _event_date(r)}
        positions = position_context(cur.get("tickers") or [], conn=conn)
        title, body = render_alert(cur, event, verdict,
                                   positions, sc["confirm_hits"] if verdict == "confirm" else sc["break_hits"])
        import mason_notifications
        tickers = cur.get("tickers") or []
        n = mason_notifications.add_notification(
            "thesis_match", title, body=body,
            severity="warn" if verdict == "break" else "info",
            ticker=tickers[0] if tickers else None, ref_table="news_events", ref_id=r["id"],
            created_by="mason", dedup_key=f"thesis:{t['id']}:{verdict}")
        notified[verdict] = {"date": _now_iso(now), "news_id": r["id"],
                             "notification_id": (n or {}).get("id")}
        ex["notified"] = notified
        with conn:
            conn.execute("UPDATE theses SET extra_json=? WHERE id=?", (json.dumps(ex), t["id"]))
        return {"thesis_id": t["id"], "verdict": verdict, "notification_id": (n or {}).get("id"),
                "title": title, "body": body}
    except Exception as e:
        log.warning(f"[theses] notify failed ({t.get('id')}): {e}")
        return None


# ── director package section ────────────────────────────────────────────────────

def _thesis_line(t: dict) -> str:
    tk = ",".join(t.get("tickers") or []) or "macro"
    tag = " [qwen proposal — unverified]" if t.get("status") == "proposed" else ""
    s = (f"- {tk} {t.get('direction') or 'neutral'} · \"{t.get('title')}\" "
         f"(opened {_short_date(t.get('created_date'))} by {t.get('origin')}, "
         f"review {_short_date(t.get('review_by'))}, {t.get('status')}){tag}: "
         f"{(t.get('thesis') or '')[:220]}")
    s += f" | confirm: {'; '.join((t.get('confirm_signals') or [])[:5])}"
    if t.get("break_signals"):
        s += f" | break: {'; '.join(t['break_signals'][:4])}"
    ml = [m for m in t.get("match_log") or [] if m.get("verdict") in ("confirm", "break")]
    if ml:
        m = ml[-1]
        s += f" | {m['verdict'].upper()} {_short_date(m.get('date'))}: \"{(m.get('headline') or '')[:140]}\""
    return s


def theses_section_text(tickers=None, max_tokens: int = 600, *, now: float | None = None) -> str:
    """Compact text of open + recently-matched (14d) theses for director packages,
    theses on `tickers` first. "" when disabled or none. Never raises."""
    try:
        if not enabled():
            return ""
        rows = list_theses(limit=200)
        cutoff = (datetime.fromtimestamp(now or time.time()) - timedelta(days=14)).isoformat()
        keep = [t for t in rows if t["status"] in OPEN_STATUSES or
                (t["status"] in ("confirmed", "broken") and (t.get("last_match_date") or "") >= cutoff)]
        if not keep:
            return ""
        want = {str(x).upper() for x in _as_list(tickers)}

        def prio(t):
            return (0 if want & set(t.get("tickers") or []) else 1,
                    0 if t["status"] in ("confirmed", "broken") else (1 if t["status"] == "watching" else 2),
                    -(_iso_epoch(t.get("last_match_date") or t.get("created_date")) or 0))
        keep.sort(key=prio)
        head = f"THESES WE'RE WATCHING ({len(keep)}) — long-lived hypotheses; say if a call bears on one:"
        out, budget = [head], max(50, int(max_tokens)) * 4 - len(head)
        shown = 0
        for t in keep:
            ln = _thesis_line(t)
            if len(ln) + 1 > budget:
                ln = ln[:max(0, budget - 2)] + "…" if budget > 120 and shown == 0 else ""
                if not ln:
                    break
            out.append(ln)
            budget -= len(ln) + 1
            shown += 1
        if shown < len(keep):
            out.append(f"  …(+{len(keep) - shown} more theses not shown)")
        return "\n".join(out)
    except Exception as e:
        log.warning(f"[theses] section failed: {e}")
        return ""


# ── chat tools (Anthropic tool schemas) ─────────────────────────────────────────

CHAT_TOOLS = [
    {
        "name": "add_watch_thesis",
        "description": (
            "Save a long-lived WATCH THESIS — a hypothesis Mason keeps watching the news for, for weeks, "
            "and alerts Nico when a headline confirms or breaks it. ONLY call this when Nico EXPLICITLY "
            "asks you to keep an eye on / watch for / track / remember to watch something (e.g. 'keep an "
            "eye on NVDA consumer GPUs', 'watch for Apple cutting iPhone orders'). NEVER call it on your "
            "own initiative, for ordinary questions, news summaries, trade ideas, or plain facts to "
            "remember (use the memory tool for facts). Turn his words into short confirm_signals "
            "(phrases a confirming headline would contain) and break_signals (phrases that would "
            "invalidate it)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short name, e.g. 'NVDA consumer-GPU acceleration'"},
                "thesis": {"type": "string", "description": "The hypothesis in one or two plain sentences"},
                "tickers": {"type": "array", "items": {"type": "string"},
                            "description": "Ticker symbols or company names it concerns; empty for pure macro"},
                "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"],
                              "description": "What confirmation would mean for the ticker(s)"},
                "confirm_signals": {"type": "array", "items": {"type": "string"},
                                    "description": "2-6 short phrases/keywords a CONFIRMING headline would contain"},
                "break_signals": {"type": "array", "items": {"type": "string"},
                                  "description": "Short phrases a headline that INVALIDATES it would contain"},
                "review_by": {"type": "string", "description": "Optional YYYY-MM-DD to stop watching (default ~45 days)"},
            },
            "required": ["title", "thesis", "confirm_signals"],
        },
    },
    {
        "name": "list_watch_theses",
        "description": (
            "List the watch theses Mason is tracking (what we're watching for, when each was opened, "
            "and any confirming/breaking headline). Call ONLY when Nico asks what we're watching, "
            "tracking or keeping an eye on, or asks about a specific saved thesis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "all", "watching", "proposed", "confirmed",
                                                      "broken", "expired", "closed"],
                           "description": "Default 'open' (watching + proposed)"},
                "ticker": {"type": "string", "description": "Optional ticker filter"},
            },
            "required": [],
        },
    },
    {
        "name": "close_watch_thesis",
        "description": (
            "Stop watching a saved watch thesis. Call ONLY when Nico explicitly says to stop watching, "
            "drop, close or forget a specific thesis. Pass its id (from list_watch_theses) or, if "
            "unknown, a ticker/phrase identifying exactly one open thesis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Thesis id from list_watch_theses"},
                "query": {"type": "string", "description": "Ticker or phrase when the id is unknown"},
                "reason": {"type": "string", "description": "Why it's being closed (optional)"},
            },
            "required": [],
        },
    },
]


def _brief_line(t: dict) -> str:
    tk = ",".join(t.get("tickers") or []) or "macro"
    s = (f"[{t['id']}] {tk} — {t['title']} ({t.get('direction')}, {t['status']}, opened "
         f"{(t.get('created_date') or '')[:10]} by {t.get('origin')}, review by {t.get('review_by')})")
    ml = [m for m in t.get("match_log") or [] if m.get("verdict") in ("confirm", "break")]
    if ml:
        s += f"\n    {ml[-1]['verdict']}ed by: \"{ml[-1].get('headline')}\" ({(ml[-1].get('date') or '')[:10]})"
    return s


def handle_chat_tool(name: str, inp: dict | None) -> str:
    """Dispatch one of CHAT_TOOLS. Returns plain text for the model. Never raises."""
    inp = inp or {}
    try:
        if name not in {t["name"] for t in CHAT_TOOLS}:
            return f"Unknown tool {name}."
        if not enabled():
            return "Watch theses are turned off (memory.theses.enabled is false)."
        if name == "add_watch_thesis":
            res = create_thesis(inp.get("title"), inp.get("thesis"), inp.get("tickers"),
                                inp.get("direction") or "neutral", inp.get("confirm_signals"),
                                inp.get("break_signals"), origin="nico", review_by=inp.get("review_by"),
                                source_refs=[{"table": "chat", "id": "", "date": _now_iso()[:10],
                                              "headline": str(inp.get("thesis") or "")[:200]}],
                                created_by="nico")
            if not res.get("ok"):
                return f"Could not save the watch thesis: {res.get('error')}"
            t = res["thesis"]
            msg = (f"Saved watch thesis [{t['id']}] \"{t['title']}\" on "
                   f"{', '.join(t['tickers']) or 'macro'} ({t['direction']}). Watching until "
                   f"{t['review_by']} for: {', '.join(t['confirm_signals'])}")
            if t.get("break_signals"):
                msg += f"; would break on: {', '.join(t['break_signals'])}"
            if res.get("rejected_tickers"):
                msg += f". Ignored unrecognized tickers: {', '.join(res['rejected_tickers'])}"
            return msg + "."
        if name == "list_watch_theses":
            st = inp.get("status") or "open"
            rows = list_theses(status=None if st == "all" else st,
                               tickers=[inp["ticker"]] if inp.get("ticker") else None, limit=20)
            if not rows:
                return "No watch theses match."
            return f"{len(rows)} watch thes{'is' if len(rows) == 1 else 'es'}:\n" + "\n".join(
                _brief_line(t) for t in rows)
        if name == "close_watch_thesis":
            tid = str(inp.get("id") or "").strip()
            if not tid:
                q = str(inp.get("query") or "").strip()
                if not q:
                    return "Tell me which thesis to close (id or ticker/phrase)."
                rows = list_theses(status="open", limit=200)
                qu = q.upper()
                hits = [t for t in rows if qu in (t.get("tickers") or []) or
                        q.lower() in (t.get("title") or "").lower()]
                if len(hits) != 1:
                    if not hits:
                        return f"No open thesis matches '{q}'."
                    return "Several open theses match — which one?\n" + "\n".join(_brief_line(t) for t in hits)
                tid = hits[0]["id"]
            res = close_thesis(tid, reason=inp.get("reason") or "closed via chat", by="nico")
            if not res.get("ok"):
                return f"Could not close it: {res.get('error')}"
            return f"Closed watch thesis [{tid}] \"{res['thesis']['title']}\"."
    except Exception as e:
        log.warning(f"[theses] chat tool {name} failed: {e}")
        return f"Watch-thesis tool error: {e}"
    return "No-op."
