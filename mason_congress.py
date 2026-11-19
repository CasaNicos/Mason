"""
mason_congress.py — Congressional Trades (RH Expansion S2-T1).

Spec: docs/robinhood/RH_EXPANSION_S2_SIGNALS_TOOLS.md §2 T1; binding contracts in
docs/robinhood/RH_EXPANSION_CONTRACTS.md (§5 politician_trades, §6 /api/rh/congress, §9 tests).

What it does
  * Ingest STOCK-Act disclosures from RH `get_politician_trades` (Tip Ranks data). The tool needs
    a symbol or a politician name (S1 PROBE bullet 7), so there are two sweeps, both on the RH
    "bulk" rate lane:
      - tickers: (holdings ∪ watchlist ∪ S&P 500 ∪ tickers already seen), rotating about
        1/`rotation_parts` (6) of the universe per run; the run is scheduled every `poll_hours`;
      - politicians already seen, rotating the same way (catches tickers outside the universe).
        A per-politician answer is capped at 25 rows with no cursor; that cap is counted.
    Rows land in politician_trades keyed sha1(politician_norm|ticker|tx_date|tx_type|amount_low),
    so the same disclosure seen from both sweeps is one row.
  * Grade with the ONE grading function, mason_price_cache.fwd_return(ticker, disclosure_date,
    days=30|90|180, entry_rule="after"): entry = first session close strictly AFTER the
    disclosure date ("graded from when we could have acted"). Raw return + excess vs SPY are
    stored; sells carry an inverted signal (inverted:true). Exchanges and option rows are not
    graded. Only matured horizons are stored; a horizon the price cache can no longer reach
    (delisted) is stored as delisted:true and excluded from every average.
  * Leaderboard (90-day excess, n ≥ leaderboard_min_n) + priors through mason_kb_priors
    (family "politician": per politician, per chamber/party, per disclosure-lag bucket).
  * held_overlap: last 45 days of buys/sells in tickers Nico holds (his in-scope RH accounts; Household and other
    personal_scope.exclude_accounts are left out, T-D6) or watches.
  * Optional daily digest through mason_notifications (congress.notify, default off).

Context only: nothing here places, routes or recommends a trade.
Flags: robinhood.enabled (master) AND rh_expansion.congress.enabled. Off -> every public function
is a cheap neutral no-op. Public functions never raise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

import mason_db
import mason_robinhood as RH

log = logging.getLogger("mason.congress")

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
MODULE = "mason_congress"
TOOL = "get_politician_trades"
HORIZONS = (30, 90, 180)
RH_ROW_CAP = 25                    # PROBE bullet 7: per-query answers cap at 25, no cursor

_DEFAULTS = {
    "enabled": False,
    "notify": False,
    "poll_hours": 6,
    "leaderboard_min_n": 8,
    "amount_weighted": False,      # option: weight the 90d mean by the disclosed-range midpoint
    "max_pages": 5,                # paged_all bound per query
    "rotation_parts": 6,           # sweep ~1/6 of the universe per run
    "max_calls_per_run": 300,
    "grade_batch": 500,
    "grade_give_up_days": 30,      # horizon still unresolved this long after it elapsed -> terminal
    "delisted_grace_days": 14,
    "overlap_days": 45,
    "digest_lookback_days": 7,
    "daily_at_et": "17:30",
    "skip_after_fails": 3,         # O8 (2026-11-15): an item whose query fails this many sweeps IN A ROW
    "skip_days": 7,                # is skipped for skip_days (counted, shown in status) so one bad
}                                  # symbol (RH could not parse CHTR all week) cannot stall the rotation


# ── config (read per call, mtime-cached, merged over _DEFAULTS) ─────────────────
_cfg_cache = {"mtime": None, "path": None, "data": {}}


def _cfg() -> dict:
    """A torn/invalid read (mid-write file, "", "null", "[]") keeps the LAST GOOD config and is
    retried on the next call; defaults apply only if no good read has ever happened."""
    out = dict(_DEFAULTS)
    try:
        p = Path(CONFIG)
        if _cfg_cache["path"] != str(p):
            _cfg_cache.update(data={}, mtime=None, path=str(p))
        mt = p.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                raise ValueError("config is not a non-empty JSON object")
            _cfg_cache.update(data=data, mtime=mt)
    except Exception:
        pass                                   # keep _cfg_cache["data"] (last good)
    try:
        node = ((_cfg_cache["data"] or {}).get("rh_expansion") or {}).get("congress") or {}
        if isinstance(node, dict):
            out.update({k: v for k, v in node.items() if v is not None and not str(k).startswith("_")})
    except Exception:
        pass
    return out


def _num(v, default, lo=None, hi=None):
    try:
        x = float(v)
        if math.isnan(x):
            return default
    except (TypeError, ValueError):
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def enabled() -> bool:
    try:
        return bool(RH.enabled()) and _cfg().get("enabled") is True
    except Exception:
        return False


# ── time hooks (tests replace _today / _now) ─────────────────────────────────────
def _today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── run state (status) ───────────────────────────────────────────────────────────
_lock = threading.Lock()
_state: dict = {}


def _reset_state() -> None:
    _state.clear()
    _state.update(last_run=None, errors=[], truncated=0, stale=False,
                  counts={"rejected_disclosure_before_tx": 0, "rejected_malformed": 0,
                          "politician_capped": 0, "inserted": 0, "graded_horizons": 0,
                          "delisted": 0, "unresolved": 0, "nonfinite": 0})


_reset_state()


def _err(where: str, e) -> str:
    msg = f"{_now()} {where}: {type(e).__name__ if isinstance(e, BaseException) else 'error'}: {e}"[:300]
    _state["errors"] = (_state.get("errors", []) + [msg])[-20:]
    log.warning(f"[congress] {msg}")
    return msg


def _count(key: str, n: int = 1) -> None:
    _state["counts"][key] = _state["counts"].get(key, 0) + n


# ── desk_state key/value (durable cursors + digest bookkeeping) ──────────────────
def _state_get(key: str, default=None):
    try:
        conn = mason_db.get_connection()
        try:
            row = conn.execute("SELECT value FROM desk_state WHERE key=?", (f"congress.{key}",)).fetchone()
        finally:
            conn.close()
        return json.loads(row["value"]) if row and row["value"] is not None else default
    except Exception as e:
        _err("state_get", e)
        return default


def _state_set(key: str, value) -> bool:
    try:
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                             "updated_date=excluded.updated_date",
                             (f"congress.{key}", json.dumps(value), _now()))
        finally:
            conn.close()
        return True
    except Exception as e:
        _err("state_set", e)
        return False


# ── normalization ────────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f<>]")
_TITLE_RE = re.compile(r"^(?:the\s+)?(?:rep|sen|hon|honorable|representative|senator|congressman|"
                       r"congresswoman|delegate|del|dr|mr|mrs|ms)\.?\s+", re.I)
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq"}
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _clean_text(s, max_len: int = 120) -> str:
    """Plain text: tags and angle brackets removed, control chars dropped, spaces collapsed."""
    if s is None:
        return ""
    s = _CTRL_RE.sub(" ", _TAG_RE.sub(" ", str(s)))
    return " ".join(s.split())[:max_len]


def display_name(name) -> str:
    """'Rep. Pelosi, Nancy' -> 'Nancy Pelosi'; titles and Jr./Sr./II suffixes dropped."""
    s = _clean_text(name)
    prev = None
    while s and s != prev:
        prev = s
        s = _TITLE_RE.sub("", s).strip()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    parts = [p for p in parts if p.lower().strip(". ") not in _SUFFIXES]
    if len(parts) >= 2:
        s = f"{parts[1]} {parts[0]}"
    elif parts:
        s = parts[0]
    else:
        s = ""
    s = _TITLE_RE.sub("", s).strip()
    toks = s.split()
    while len(toks) > 1 and toks[-1].lower().strip(".") in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


def normalize_name(name) -> str:
    """Identity key for a politician: lowercase first + last name, titles, initials and
    suffixes removed. 'Nancy Pelosi' == 'Pelosi, Nancy' == 'Rep. Nancy Pelosi'."""
    d = display_name(name).lower()
    d = re.sub(r"[^\w\s-]", " ", d)
    toks = [t for t in d.split() if t.strip("-_")]
    if len(toks) > 2:                 # drop middle/leading initials only when a full name remains
        full = [t for t in toks if len(t.strip("-_")) > 1]
        toks = full if len(full) >= 2 else toks
    if not toks:
        return ""
    return toks[0] if len(toks) == 1 else f"{toks[0]} {toks[-1]}"


def _norm_ticker(t) -> str | None:
    s = str(t or "").strip().upper()
    return s if _TICKER_RE.match(s) else None


def _amt(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def _fin(v) -> float | None:
    """A finite float or None (NaN/inf/bool/garbage never reach the DB or the API)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _strict(obj):
    """Deep copy with every non-finite float replaced by None (strict-JSON output)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strict(v) for v in obj]
    return obj


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _tx_type(v) -> str | None:
    s = str(v or "").strip().upper()
    if s.startswith(("BUY", "PURCHASE")):
        return "buy"
    if s.startswith(("SELL", "SALE")):
        return "sell"
    if s.startswith("EXCHANGE"):
        return "exchange"
    return None


def _chamber(position, name) -> str | None:
    p = str(position or "").strip().lower()
    n = str(name or "").strip().lower()
    if p.startswith("sen") or n.startswith("sen."):
        return "senate"
    if p.startswith(("rep", "congress", "deleg")) or n.startswith("rep."):
        return "house"
    return None


def _row_id(norm, ticker, tx_date, tx_type, amount_low, asset_type) -> str:
    key = "|".join([norm, ticker, tx_date, tx_type, "" if amount_low is None else str(amount_low)])
    if asset_type != "equity":
        key += f"|{asset_type}"      # an option leg must not collide with the equity disclosure
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def parse_trade(raw) -> dict:
    """One RH row -> {"ok": True, "row": {...}} or {"ok": False, "reason": ...}. Never raises."""
    try:
        if not isinstance(raw, dict):
            return {"ok": False, "reason": "malformed"}
        ticker = _norm_ticker(raw.get("symbol"))
        name = raw.get("politician_name")
        disp, norm = display_name(name), normalize_name(name)
        tx = _tx_type(raw.get("transaction_type"))
        txd, dsd = _date(raw.get("transaction_date")), _date(raw.get("disclosure_date"))
        if not ticker or not norm or not tx or not txd or not dsd:
            return {"ok": False, "reason": "malformed"}
        lag = (dsd - txd).days
        if lag < 0:
            return {"ok": False, "reason": "disclosure_before_tx"}
        rng = raw.get("amount_range") if isinstance(raw.get("amount_range"), dict) else {}
        lo, hi = _amt(rng.get("min")), _amt(rng.get("max"))
        asset = str(raw.get("asset_type") or "equity").strip().lower() or "equity"
        asset = asset if asset in ("equity", "option") else re.sub(r"[^a-z_]", "", asset)[:20] or "other"
        party = re.sub(r"[^a-z ]", "", str(raw.get("party") or "").lower()).strip() or None
        clean_raw = {   # whitelist only — nothing account-like is ever persisted
            "politician_name": _clean_text(name), "party": _clean_text(raw.get("party"), 40),
            "position": _clean_text(raw.get("position"), 40), "asset_type": asset,
            "symbol": ticker, "transaction_type": _clean_text(raw.get("transaction_type"), 40),
            "amount_range": {"min": _clean_text(rng.get("min"), 20) or None,
                             "max": _clean_text(rng.get("max"), 20) or None},
            "transaction_date": txd.isoformat(), "disclosure_date": dsd.isoformat(),
            "source": _clean_text(raw.get("source"), 40),
        }
        row = {"id": _row_id(norm, ticker, txd.isoformat(), tx, lo, asset),
               "politician": disp, "politician_norm": norm,
               "chamber": _chamber(raw.get("position"), _clean_text(name)), "party": party,
               "ticker": ticker, "tx_type": tx, "amount_low": lo, "amount_high": hi,
               "tx_date": txd.isoformat(), "disclosure_date": dsd.isoformat(), "lag_days": lag,
               "asset_type": asset, "raw_json": json.dumps(clean_raw, sort_keys=True)}
        return {"ok": True, "row": row}
    except Exception as e:
        return {"ok": False, "reason": f"malformed:{type(e).__name__}"}


# ── ingest ───────────────────────────────────────────────────────────────────────
def ingest(raws) -> dict:
    """Parse + INSERT OR IGNORE. Idempotent: a re-seen disclosure is a dupe, never an update."""
    out = {"inserted": 0, "dupes": 0, "rejected": 0, "rejected_reasons": {}}
    if not enabled():
        return out
    try:
        parsed = []
        for raw in list(raws or [])[:5000]:
            p = parse_trade(raw)
            if p["ok"]:
                parsed.append(p["row"])
            else:
                reason = p["reason"].split(":")[0]
                out["rejected"] += 1
                out["rejected_reasons"][reason] = out["rejected_reasons"].get(reason, 0) + 1
                _count("rejected_disclosure_before_tx" if reason == "disclosure_before_tx"
                       else "rejected_malformed")
        if not parsed:
            return out
        now = _now()
        conn = mason_db.get_connection()
        try:
            with conn:
                for r in parsed:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO politician_trades (id, politician, politician_norm, "
                        "chamber, party, ticker, tx_type, amount_low, amount_high, tx_date, "
                        "disclosure_date, lag_days, entry_date, entry_px, fwd_json, raw_json, "
                        "created_date, created_by, record_deleted) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,0)",
                        (r["id"], r["politician"], r["politician_norm"], r["chamber"], r["party"],
                         r["ticker"], r["tx_type"], r["amount_low"], r["amount_high"], r["tx_date"],
                         r["disclosure_date"], r["lag_days"], r["raw_json"], now, MODULE))
                    if cur.rowcount:
                        out["inserted"] += 1
                    else:
                        out["dupes"] += 1
        finally:
            conn.close()
        _count("inserted", out["inserted"])
    except Exception as e:
        out["inserted"] = 0
        out["error"] = _err("ingest", e)
    return out


# ── universe ─────────────────────────────────────────────────────────────────────
_book = {"held": None, "ts": None, "scope": None}


def _held(refresh: bool) -> set:
    """Held tickers. refresh=True (job paths) asks RH and caches; refresh=False (HTTP request
    path) reads the cache only, so an API request never triggers an RH call. The cache carries the
    exclude_accounts scope stamp; a cache from another scope is dropped (T-D6, RULINGS #13)."""
    if refresh:
        stamp = RH.scope_stamp()
        got = _safe_set(_holdings, "holdings")
        _book.update(held=set(got), ts=_now(), scope=stamp)
        return got
    if _book.get("scope") != RH.scope_stamp():
        _book.update(held=None, ts=None, scope=None)
        return set()
    return set(_book["held"] or ())


def _holdings() -> set:
    """Tickers held in Nico's in-scope RH accounts (Household / exclude_accounts and blank labels left out, T-D6)."""
    return set(RH.in_scope_position_symbols())


def _watched() -> set:
    import mason_watchlist
    return {it.get("ticker") for it in mason_watchlist.list_items("active") if it.get("ticker")}


def _sp500() -> set:
    """Existing Mason constituents source (mason_triage broad universe, Wikipedia S&P 500 + NDX,
    cached weekly by triage). Cache only — this module never triggers the network refresh."""
    import mason_triage
    bu = mason_triage.broad_universe(allow_refresh=False) or {}
    return {k for k, v in bu.items() if "SP500" in ((v or {}).get("index") or [])}


def _seen_tickers() -> set:
    conn = mason_db.get_connection()
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM politician_trades WHERE record_deleted=0")}
    finally:
        conn.close()


def _safe_set(fn, where) -> set:
    try:
        return {t for t in (_norm_ticker(x) for x in (fn() or [])) if t}
    except Exception as e:
        _err(f"universe {where}", e)
        return set()


def universe() -> list:
    """Sorted (holdings ∪ watchlist ∪ S&P 500 ∪ tickers already seen)."""
    if not enabled():
        return []
    u = set()
    u |= _held(True)
    for fn, where in ((_watched, "watchlist"), (_sp500, "sp500"), (_seen_tickers, "seen")):
        u |= _safe_set(fn, where)
    return sorted(u)


def _seen_politicians() -> list:
    """[(norm, display)] for every politician already stored, most recent display name."""
    conn = mason_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT politician_norm, politician FROM politician_trades WHERE record_deleted=0 "
            "AND politician_norm != '' ORDER BY politician_norm, disclosure_date DESC").fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for r in rows:
        if r[0] not in seen and r[1]:
            seen.add(r[0])
            out.append((r[0], r[1]))
    return out


# ── sweep ────────────────────────────────────────────────────────────────────────
def _fetch(args: dict) -> tuple:
    """(rows, meta) via mason_robinhood.paged_all on the bulk lane. Never raises."""
    try:
        return RH.paged_all(TOOL, args, lane="bulk", rows_key="trades",
                            max_pages=int(_num(_cfg().get("max_pages"), 5, 1, 50)))
    except Exception as e:
        return [], {"pages": 0, "truncated": False, "errors": [f"{type(e).__name__}: {e}"[:300]]}


def _skip_get() -> dict:
    """{cursor_key: {item: {"fails": n, "last": iso, "until": iso|None, "error": str}}} (desk_state)."""
    v = _state_get("skip", {})
    return v if isinstance(v, dict) else {}


def _skipped_now(skip: dict, cursor_key: str, item) -> bool:
    if int(_num(_cfg().get("skip_after_fails"), 3, 0, 1000)) <= 0:
        return False                  # skip_after_fails 0 = old behaviour: no item is ever skipped
    ent = (skip.get(cursor_key) or {}).get(str(item)) or {}
    until = ent.get("until")
    return bool(until) and str(until) > _now()


def _note_fail(skip: dict, cursor_key: str, item, err: str) -> bool:
    """Count one more failing sweep for `item`. True when it has now failed skip_after_fails sweeps in a
    row: it is then skipped for skip_days (the S1 checkpoint rule still holds for one-off failures)."""
    cfg = _cfg()
    after = int(_num(cfg.get("skip_after_fails"), 3, 0, 1000))
    days = _num(cfg.get("skip_days"), 7, 0, 365)
    ent = skip.setdefault(cursor_key, {}).setdefault(str(item), {"fails": 0})
    ent.update(fails=int(ent.get("fails") or 0) + 1, last=_now(), error=str(err)[:200], until=None)
    if after <= 0 or ent["fails"] < after:
        return False
    try:
        base = datetime.fromisoformat(_now())
    except Exception:
        base = datetime.now(timezone.utc)
    ent["until"] = (base + timedelta(days=days)).isoformat(timespec="seconds")
    return True


def _rotate(items: list, cursor_key: str, args_of, out: dict, budget: list, cap_check=False) -> None:
    """Sweep the next ~1/rotation_parts of `items`, starting at the stored cursor. The cursor
    never advances past a query that errored (S1 rule: an incomplete page is not a checkpoint) —
    except an item that has failed skip_after_fails sweeps in a row: that one is skipped for
    skip_days with ONE counted warning, so a single bad item cannot stall the whole rotation."""
    n = len(items)
    if not n:
        return
    parts = int(_num(_cfg().get("rotation_parts"), 6, 1, 1000))
    k = min(n, max(1, math.ceil(n / parts)))
    start = int(_num(_state_get(cursor_key, 0), 0, 0)) % n
    skip = _skip_get()
    skip_before = json.dumps(skip, sort_keys=True)
    done = 0
    for i in range(k):
        if budget[0] <= 0:
            break
        item = items[(start + i) % n]
        if _skipped_now(skip, cursor_key, item):
            out["skipped"] = out.get("skipped", 0) + 1
            done += 1
            continue
        budget[0] -= 1
        rows, meta = _fetch(args_of(item))
        if meta.get("truncated"):
            out["truncated"] += 1
            _state["truncated"] += 1
        if cap_check and len(rows) >= RH_ROW_CAP:
            _count("politician_capped")
            out["capped"] += 1
        res = ingest(rows)
        for key in ("inserted", "dupes", "rejected"):
            out[key] += res.get(key, 0)
        if res.get("error"):                  # failed write: not a checkpoint, retry next run
            out["errors"].append(res["error"])
            break
        if meta.get("errors"):
            if _note_fail(skip, cursor_key, item, meta["errors"][0]):
                _count("skipped_bad_items")
                out["skipped"] = out.get("skipped", 0) + 1
                ent = skip[cursor_key][str(item)]
                log.warning(f"[congress] skipping {item!r} until {ent['until']}: its query failed "
                            f"{ent['fails']} sweeps in a row ({ent['error']})")
                done += 1
                continue
            for m in meta["errors"][:3]:
                out["errors"].append(_err(f"sweep {cursor_key}", m))
            break
        ent = (skip.get(cursor_key) or {}).pop(str(item), None)
        if ent is not None and not skip.get(cursor_key):
            skip.pop(cursor_key, None)
        done += 1
    if json.dumps(skip, sort_keys=True) != skip_before:
        _state_set("skip", skip)
    _state_set(cursor_key, (start + done) % n)
    out[cursor_key.replace("_cursor", "s")] += done


def sweep() -> dict:
    """One rotation step: ~1/6 of the ticker universe, then ~1/6 of the politicians seen."""
    if not enabled():
        return {"ok": False, "enabled": False}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "busy": True}
    out = {"ok": True, "tickers": 0, "politicians": 0, "inserted": 0, "dupes": 0, "rejected": 0,
           "truncated": 0, "capped": 0, "skipped": 0, "errors": []}
    try:
        budget = [int(_num(_cfg().get("max_calls_per_run"), 300, 1, 5000))]
        _rotate(universe(), "ticker_cursor", lambda t: {"equity_symbol": t}, out, budget)
        try:
            pols = _seen_politicians()
        except Exception as e:
            pols = []
            out["errors"].append(_err("sweep politicians", e))
        _rotate([d for _, d in pols], "politician_cursor", lambda d: {"politician_name": d}, out,
                budget, cap_check=True)
    except Exception as e:
        out["errors"].append(_err("sweep", e))
    finally:
        _state["last_run"] = _now()
        _state["stale"] = bool(out["errors"])
        _lock.release()
    out["ok"] = not out["errors"]
    return out


# ── grading (mason_price_cache.fwd_return only) ──────────────────────────────────
def _delisted(res: dict, elapsed: int, h: int) -> bool:
    if elapsed <= h:
        return False
    flag = res.get("delisted") is True or any(
        s in str(res.get("error") or res.get("status") or "").lower()
        for s in ("not_found", "delist"))
    if flag:
        return True
    grace = int(_num(_cfg().get("delisted_grace_days"), 14, 0, 365))
    return (res.get("matured") is not True and res.get("ret") is None and not res.get("exit_date")
            and bool(res.get("entry_date")) and elapsed > h + grace)


def grade_pending(limit: int | None = None) -> dict:
    out = {"graded": 0, "horizons": 0, "delisted": 0, "errors": 0}
    if not enabled():
        return out
    try:
        import mason_price_cache as PC
    except Exception as e:
        _err("grade import mason_price_cache", e)
        out["errors"] += 1
        return out
    try:
        batch = int(limit or _num(_cfg().get("grade_batch"), 500, 1, 20000))
        give_up = int(_num(_cfg().get("grade_give_up_days"), 30, 1, 3650))
        today = _today()
        # Only rows with something DUE: a horizon that has elapsed and isn't stored yet, or a
        # missing entry. Options/exchanges never qualify (never gradeable), and every due
        # horizon ends stored, delisted or unresolved (terminal) -> nothing blocks the batch.
        fj = "COALESCE(fwd_json, '{}')"
        due = [f"(json_type({fj}, '$.\"{h}\"') IS NULL AND disclosure_date < ?)" for h in HORIZONS]
        args = [(today - timedelta(days=h)).isoformat() for h in HORIZONS]
        due.append(f"(entry_date IS NULL AND json_type({fj}, '$.\"{HORIZONS[0]}\"') IS NULL "
                   "AND disclosure_date < ?)")
        args.append(today.isoformat())
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, ticker, tx_type, disclosure_date, entry_date, entry_px, fwd_json, raw_json "
                "FROM politician_trades WHERE record_deleted=0 AND tx_type IN ('buy','sell') "
                "AND COALESCE(json_extract(raw_json, '$.asset_type'), 'equity') = 'equity' "
                f"AND ({' OR '.join(due)}) "
                "ORDER BY disclosure_date DESC, id LIMIT ?", (*args, batch))]
        finally:
            conn.close()
        updates = []
        for r in rows:
            try:
                if (json.loads(r["raw_json"] or "{}").get("asset_type") or "equity") != "equity":
                    continue          # option legs: call/put direction unknown -> not graded
                disc = _date(r["disclosure_date"])
                if not disc:
                    continue
                elapsed = (today - disc).days
                fwd = json.loads(r["fwd_json"] or "{}")
                entry_date, entry_px = r["entry_date"], r["entry_px"]
                changed = False
                for h in HORIZONS:
                    if str(h) in fwd:
                        continue
                    want_entry = h == HORIZONS[0] and entry_date is None and elapsed >= 1
                    if elapsed <= h and not want_entry:
                        continue
                    try:
                        res = PC.fwd_return(r["ticker"], disc.isoformat(), days=h, entry_rule="after")
                    except Exception as e:
                        _err(f"fwd_return {r['ticker']} {h}d", e)
                        out["errors"] += 1
                        continue
                    if not isinstance(res, dict):
                        out["errors"] += 1
                        continue
                    ret, xs = _fin(res.get("ret")), _fin(res.get("xs"))
                    nonfinite = (res.get("ret") is not None and ret is None) or \
                        (res.get("xs") is not None and xs is None)
                    if nonfinite:
                        _err(f"fwd_return {r['ticker']} {h}d", "non-finite/invalid ret or xs ignored")
                        _count("nonfinite")
                        out["errors"] += 1
                    if entry_date is None and res.get("entry_date") and not nonfinite:
                        epx = _fin(res.get("entry_px"))
                        entry_date = res.get("entry_date") if epx is not None else None
                        if entry_date:
                            entry_px = epx
                            changed = True
                    if _delisted(res, elapsed, h):
                        fwd[str(h)] = {"ret": None, "xs_spy": None, "matured": False, "delisted": True}
                        out["delisted"] += 1
                        changed = True
                    elif res.get("matured") is True and ret is not None and not nonfinite:
                        inv = r["tx_type"] == "sell"     # a sell is right when the stock lags
                        sign = -1.0 if inv else 1.0
                        # ret / xs are finite floats here (checked above)
                        fwd[str(h)] = {"ret": ret, "xs_spy": xs, "matured": True, "inverted": inv,
                                       "signal_ret": -ret if inv else ret,
                                       "signal_xs": (sign * xs) if xs is not None else None,
                                       "delisted": False}
                        out["horizons"] += 1
                        changed = True
                    elif elapsed > h + give_up:
                        # still nothing usable long after the horizon elapsed (unknown ticker, no
                        # entry, bad data): terminal + counted so it can never block the batch
                        fwd[str(h)] = {"ret": None, "xs_spy": None, "matured": False,
                                       "unresolved": True}
                        out["unresolved"] = out.get("unresolved", 0) + 1
                        _count("unresolved")
                        changed = True
                if changed:
                    updates.append((json.dumps(fwd, sort_keys=True, allow_nan=False) if fwd else None,
                                    entry_date, entry_px, r["id"]))
            except Exception as e:
                _err(f"grade row {r.get('id')}", e)
                out["errors"] += 1
        if updates:
            conn = mason_db.get_connection()
            try:
                with conn:
                    conn.executemany("UPDATE politician_trades SET fwd_json=?, entry_date=COALESCE("
                                     "entry_date, ?), entry_px=COALESCE(entry_px, ?) WHERE id=?",
                                     updates)
            finally:
                conn.close()
        out["graded"] = len(updates)
        _count("graded_horizons", out["horizons"])
    except Exception as e:
        _err("grade_pending", e)
        out["errors"] += 1
    return out


# ── leaderboard + priors (mason_kb_priors.upsert_prior only) ─────────────────────
def _lag_bucket(lag) -> str:
    lag = int(lag or 0)
    return "0-15" if lag < 15 else "15-30" if lag < 30 else "30-45" if lag <= 45 else "45+"


def _weight(lo, hi) -> float:
    if lo is not None and hi is not None:
        return max((lo + hi) / 2.0, 1.0)
    return max(lo if lo is not None else (hi if hi is not None else 1.0), 1.0)


FAMILY = "politician"
WINDOW = "90d"
# str.format templates rendered by mason_kb_priors._render (stat + scalar key placeholders).
# Only stats that are non-None whenever n >= 1 are used under numeric specs (mean, hit_rate),
# so a template never falls back to the default summary.
# The politician template carries the DISPLAY name as literal text (built per row by _fmt_pol):
# a {politician} placeholder would render the key value, i.e. the lowercase normalized name.
_FMT_POL_TAIL = (": mean 90-day excess vs SPY after disclosure {mean:+.2%}, "
                 "hit rate {hit_rate:.0%} (sells inverted), n={n}")


def _fmt_pol(display: str) -> str:
    return _clean_text(display).replace("{", "{{").replace("}", "}}") + _FMT_POL_TAIL
_FMT_GROUP = ("{chamber} {party}: mean 90-day excess vs SPY after disclosure {mean:+.2%}, "
              "hit rate {hit_rate:.0%} (sells inverted), n={n}")
_FMT_LAG = ("Disclosure lag {lag_bucket} days: mean 90-day excess vs SPY {mean:+.2%}, "
            "hit rate {hit_rate:.0%} (sells inverted), n={n}")


def _prior(KB, key: dict, samples: list, refs: list, fmt: str, withheld: int, errs: list):
    """One upsert through mason_kb_priors. Records a failure in `errs` (so the pass skips the
    retire step and failed keys are not retired)."""
    if KB is None or not samples:
        return None
    try:
        kw = {"summary_fmt": fmt, "refs": refs[:50]}
        if withheld:
            kw["withheld"] = int(withheld)       # real S1 API: delisted samples, shown in the summary
        try:
            row = KB.upsert_prior(FAMILY, key, WINDOW, samples, **kw)
        except TypeError:
            if "withheld" not in kw:
                raise
            kw.pop("withheld")                   # an older upsert_prior without `withheld`
            row = KB.upsert_prior(FAMILY, key, WINDOW, samples, **kw)
        if row is None and getattr(KB, "enabled", lambda: True)():
            errs.append(key)
        return row
    except Exception as e:
        errs.append(key)
        _err(f"upsert_prior {key}", e)
        return None


def _shrunk(row):
    try:
        st = row.get("stat_json") if isinstance(row, dict) else None
        st = json.loads(st) if isinstance(st, str) else (st or {})
        return _fin(st.get("shrunk_mean"))
    except Exception:
        return None


def _final_shrunk(KB) -> dict:
    """{prior id: shrunk_mean} read back AFTER the whole pass. mason_kb_priors re-shrinks the pool
    after every upsert, so the value returned by an early upsert_prior is order-dependent."""
    out = {}
    try:
        q = getattr(KB, "query", None)
        for r in (q(FAMILY, limit=2000) if q else []) or []:
            if isinstance(r, dict) and r.get("id"):
                out[r["id"]] = _fin((r.get("stat") or {}).get("shrunk_mean"))
    except Exception as e:
        _err("kb_priors query", e)
    return out


def refresh_leaderboard() -> list:
    """Recompute per-politician 90d stats, write priors (bracketed by begin_run/retire_unrefreshed
    so keys that stop being produced are retired), store + return the leaderboard."""
    if not enabled():
        return []
    try:
        cfg = _cfg()
        min_n = int(_num(cfg.get("leaderboard_min_n"), 8, 1))
        weighted = cfg.get("amount_weighted") is True
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, politician, politician_norm, chamber, party, lag_days, amount_low, "
                "amount_high, disclosure_date, fwd_json FROM politician_trades WHERE record_deleted=0 "
                "AND tx_type IN ('buy','sell') AND fwd_json IS NOT NULL ORDER BY disclosure_date, id")]
        finally:
            conn.close()
        try:
            import mason_kb_priors as KB
        except Exception as e:
            _err("import mason_kb_priors", e)
            KB = None
        per, groups, lags = {}, {}, {}
        for r in rows:
            h = (json.loads(r["fwd_json"] or "{}") or {}).get("90") or {}
            x = _fin(h.get("signal_xs"))
            gk, lk = (r["chamber"] or "unknown", r["party"] or "unknown"), _lag_bucket(r["lag_days"])
            p = per.setdefault(r["politician_norm"], {"name": r["politician"], "xs": [], "w": [],
                                                      "ids": [], "withheld": 0})
            g = groups.setdefault(gk, {"xs": [], "ids": [], "withheld": 0})
            lg = lags.setdefault(lk, {"xs": [], "ids": [], "withheld": 0})
            if h.get("delisted"):              # counted as withheld, never averaged
                for b in (p, g, lg):
                    b["withheld"] += 1
                continue
            if h.get("matured") is not True or x is None:
                continue
            p["name"] = r["politician"] or p["name"]          # rows are date-ordered: latest wins
            p["w"].append(_weight(r["amount_low"], r["amount_high"]))
            for b in (p, g, lg):
                b["xs"].append(x)
                b["ids"].append(r["id"])
        errs = []
        begin = getattr(KB, "begin_run", None) if KB is not None else None
        run = None
        if begin:
            try:
                run = begin(FAMILY)
            except Exception as e:
                errs.append("begin_run")
                _err("begin_run", e)
        board, pid = [], {}
        for norm, p in per.items():
            prow = _prior(KB, {"politician": norm}, p["xs"], p["ids"], _fmt_pol(p["name"]),
                          p["withheld"], errs)
            n = len(p["xs"])
            if n < min_n:
                continue
            mean = (sum(x * w for x, w in zip(p["xs"], p["w"])) / sum(p["w"])) if weighted \
                else sum(p["xs"]) / n
            if prow and prow.get("id"):
                pid[norm] = prow["id"]
            board.append({"politician": p["name"], "n": n,
                          "hit_rate_90": round(sum(1 for x in p["xs"] if x > 0) / n, 4),
                          "xs_spy_90": round(mean, 6), "shrunk_xs": _shrunk(prow), "_norm": norm})
        for (ch, pa), g in sorted(groups.items()):
            _prior(KB, {"chamber": ch, "party": pa}, g["xs"], g["ids"], _FMT_GROUP, g["withheld"], errs)
        for b, g in sorted(lags.items()):
            _prior(KB, {"lag_bucket": b}, g["xs"], g["ids"], _FMT_LAG, g["withheld"], errs)
        retire = getattr(KB, "retire_unrefreshed", None) if KB is not None else None
        if run and retire:
            if errs:
                _err("refresh_leaderboard", f"{len(errs)} prior write(s) failed; stale-key retire skipped")
            else:
                try:
                    retire(FAMILY, run)
                except Exception as e:
                    _err("retire_unrefreshed", e)
        final = _final_shrunk(KB) if KB is not None else {}
        for r in board:
            v = final.get(pid.get(r["_norm"]), r["shrunk_xs"]) if pid.get(r["_norm"]) in final \
                else r["shrunk_xs"]
            r["shrunk_xs"] = None if v is None else round(v, 6)
        board.sort(key=lambda r: (-(r["shrunk_xs"] if r["shrunk_xs"] is not None else r["xs_spy_90"]),
                                  r["politician"]))
        for i, r in enumerate(board, 1):
            r["rank"] = i
        norms = [r.pop("_norm") for r in board]
        _state_set("leaderboard", {"computed": _now(), "rows": board, "norms": norms})
        return [dict(r) for r in board]
    except Exception as e:
        _err("refresh_leaderboard", e)
        return []


def _stored_board() -> tuple:
    d = _state_get("leaderboard", {}) or {}
    rows = [r for r in d.get("rows") or [] if isinstance(r, dict)]
    norms = list(d.get("norms") or [])
    return rows, norms if len(norms) == len(rows) else [normalize_name(r.get("politician")) for r in rows]


def leaderboard() -> list:
    if not enabled():
        return []
    try:
        return _stored_board()[0]
    except Exception as e:
        _err("leaderboard", e)
        return []


# ── cross-reference ──────────────────────────────────────────────────────────────
def held_overlap(days: int | None = None, *, refresh: bool = True) -> list:
    """[{ticker, n_buys, n_sells, politicians}] for held/watched tickers, last `days` (45) of
    disclosures. Holdings = Nico's in-scope RH accounts only (T-D6: Household excluded)."""
    if not enabled():
        return []
    try:
        days = int(_num(days if days is not None else _cfg().get("overlap_days"), 45, 1, 3650))
        tickers = _held(refresh) | _safe_set(_watched, "watchlist")
        if not tickers:
            return []
        cutoff = (_today() - timedelta(days=days)).isoformat()
        conn = mason_db.get_connection()
        try:
            q = ",".join("?" * len(tickers))
            rows = conn.execute(
                "SELECT ticker, tx_type, politician FROM politician_trades WHERE record_deleted=0 "
                f"AND tx_type IN ('buy','sell') AND disclosure_date >= ? AND ticker IN ({q})",
                [cutoff, *sorted(tickers)]).fetchall()
        finally:
            conn.close()
        agg = {}
        for t, tx, pol in rows:
            a = agg.setdefault(t, {"ticker": t, "n_buys": 0, "n_sells": 0, "politicians": set()})
            a["n_buys" if tx == "buy" else "n_sells"] += 1
            if pol:
                a["politicians"].add(pol)
        out = [dict(a, politicians=sorted(a["politicians"])) for a in agg.values()]
        out.sort(key=lambda a: (-(a["n_buys"] + a["n_sells"]), a["ticker"]))
        return out
    except Exception as e:
        _err("held_overlap", e)
        return []


# ── digest notification (default off; mason_notifications only) ─────────────────
def notify_digest() -> dict:
    """At most one digest per ET day, only for top-quartile, min-n politicians trading a ticker
    Nico holds or watches."""
    out = {"sent": False, "n": 0}
    if not enabled() or _cfg().get("notify") is not True:
        return out
    try:
        today = _today().isoformat()
        if _state_get("notify_last_date") == today:
            return dict(out, reason="already_sent_today")
        board, norms = _stored_board()
        if not board:
            return dict(out, reason="no_leaderboard")
        q = max(1, math.ceil(len(board) / 4))
        top = {norms[i] for i, r in enumerate(board) if int(r.get("rank") or 10 ** 9) <= q}
        tickers = _held(True) | _safe_set(_watched, "watchlist")
        since = _state_get("notify_since") or \
            (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        recent = (_today() - timedelta(days=int(_num(_cfg().get("digest_lookback_days"), 7, 1, 90)))).isoformat()
        now = _now()
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, politician, politician_norm, ticker, tx_type, amount_low, amount_high, "
                "disclosure_date FROM politician_trades WHERE record_deleted=0 AND tx_type IN "
                "('buy','sell') AND created_date > ? AND disclosure_date >= ? "
                "ORDER BY disclosure_date DESC, id", (since, recent))]
        finally:
            conn.close()
        hits = [r for r in rows if r["ticker"] in tickers and r["politician_norm"] in top]
        if not hits:
            _state_set("notify_since", now)
            return dict(out, reason="no_matches")
        ranks = {norms[i]: r.get("rank") for i, r in enumerate(board)}
        lines = []
        for r in hits[:20]:
            rng = "" if r["amount_low"] is None else \
                f" (${r['amount_low']:,.0f}-{'?' if r['amount_high'] is None else format(r['amount_high'], ',.0f')})"
            lines.append(f"{r['politician']} (rank {ranks.get(r['politician_norm'])}) "
                         f"{r['tx_type']} {r['ticker']}{rng}, disclosed {r['disclosure_date']}")
        body = ("Disclosed congressional trades in names you hold or watch. Context only, not a "
                "trade signal.\n" + "\n".join(lines))
        import mason_notifications as N
        tks = sorted({r["ticker"] for r in hits})
        N.add_notification("congress_digest",
                           f"Congress: {len(hits)} trade(s) by top-ranked politicians in "
                           f"{', '.join(tks[:5])}",
                           body=body, severity="info", ticker=tks[0] if len(tks) == 1 else None,
                           ref_table="politician_trades", ref_id=hits[0]["id"],
                           created_by=MODULE, dedup_key=f"congress_digest:{today}")
        _state_set("notify_last_date", today)
        _state_set("notify_since", now)          # advanced only after a successful send
        return {"sent": True, "n": len(hits)}
    except Exception as e:
        return dict(out, error=_err("notify_digest", e))


# ── jobs ─────────────────────────────────────────────────────────────────────────
def run_cycle() -> dict:
    """Scheduled every poll_hours: one sweep step, then the (optional) digest."""
    if not enabled():
        return {"enabled": False}
    try:
        return {"enabled": True, "sweep": sweep(), "digest": notify_digest()}
    except Exception as e:
        return {"enabled": True, "error": _err("run_cycle", e)}


def run_daily() -> dict:
    """Scheduled after the close: grade matured horizons, then the leaderboard + priors."""
    if not enabled():
        return {"enabled": False}
    try:
        g = grade_pending()
        lb = refresh_leaderboard()
        return {"enabled": True, "grade": g, "leaderboard": len(lb)}
    except Exception as e:
        return {"enabled": True, "error": _err("run_daily", e)}


def jobs() -> list:
    """For the S2-T8 integrator. Empty when the module is off."""
    if not enabled():
        return []
    cfg = _cfg()
    at = cfg.get("daily_at_et") if isinstance(cfg.get("daily_at_et"), str) else "17:30"
    return [{"name": "rhx_congress_sweep",
             "every_s": int(_num(cfg.get("poll_hours"), 6, 0.25, 168) * 3600), "fn": run_cycle},
            {"name": "rhx_congress_grade", "at_et": at, "fn": run_daily}]


def status() -> dict:
    st = {"name": "congress", "enabled": enabled(), "last_run": _state.get("last_run"), "rows": 0,
          "errors": list(_state.get("errors") or []), "truncated": _state.get("truncated", 0),
          "rate_hits": 0, "xcheck_mismatch": 0, "counts": dict(_state.get("counts") or {}),
          "cursors": {}}
    if not st["enabled"]:
        return st
    try:
        st["rate_hits"] = int(((RH.status() or {}).get("rate") or {}).get("rate_hits") or 0)
    except Exception:
        pass
    try:
        conn = mason_db.get_connection()
        try:
            st["rows"] = conn.execute(
                "SELECT COUNT(*) FROM politician_trades WHERE record_deleted=0").fetchone()[0]
            st["counts"]["delisted"] = conn.execute(
                "SELECT COUNT(*) FROM politician_trades WHERE record_deleted=0 AND fwd_json LIKE "
                "'%\"delisted\": true%'").fetchone()[0]
            st["counts"]["unresolved"] = conn.execute(
                "SELECT COUNT(*) FROM politician_trades WHERE record_deleted=0 AND fwd_json LIKE "
                "'%\"unresolved\": true%'").fetchone()[0]
        finally:
            conn.close()
        st["cursors"] = {"ticker": _state_get("ticker_cursor", 0),
                         "politician": _state_get("politician_cursor", 0)}
        st["skipped_items"] = {ck: {it: {"fails": e.get("fails"), "until": e.get("until")}
                                    for it, e in (v or {}).items() if e.get("until")}
                               for ck, v in _skip_get().items()}
    except Exception as e:
        _err("status", e)
        st["errors"] = list(_state.get("errors") or [])
    return st


# ── API ──────────────────────────────────────────────────────────────────────────
def _trade_out(r: dict, held: set, watched: set) -> dict:
    try:
        fwd = json.loads(r.get("fwd_json") or "{}") or {}
    except Exception:
        fwd = {}
    try:
        asset = (json.loads(r.get("raw_json") or "{}") or {}).get("asset_type") or "equity"
    except Exception:
        asset = "equity"
    return {"id": r["id"], "politician": _clean_text(r["politician"]),
            "politician_norm": r["politician_norm"], "chamber": r["chamber"], "party": r["party"],
            "ticker": r["ticker"], "tx_type": r["tx_type"], "asset_type": asset,
            "amount_low": r["amount_low"], "amount_high": r["amount_high"], "tx_date": r["tx_date"],
            "disclosure_date": r["disclosure_date"], "lag_days": r["lag_days"],
            "entry_date": r["entry_date"], "entry_px": r["entry_px"], "fwd": fwd,
            "graded": any(isinstance(v, dict) and v.get("matured") is True for v in fwd.values()),
            "held": r["ticker"] in held, "watched": r["ticker"] in watched}


def api_payload(ticker=None, politician=None, days=None) -> dict:
    """Contracts §6 /api/rh/congress envelope. {"enabled": false} when off. Never raises."""
    if not enabled():
        return {"enabled": False}
    errors = []
    data = {"trades": [], "held_overlap": [], "leaderboard": []}
    try:
        d = int(_num(days, 90, 1, 3650)) if days not in (None, "") else 90
        held = _held(False)                     # cache only: no RH call on the request path
        watched = _safe_set(_watched, "watchlist")
        sql = ("SELECT * FROM politician_trades WHERE record_deleted=0 AND disclosure_date >= ?")
        args = [(_today() - timedelta(days=d)).isoformat()]
        tk = _norm_ticker(ticker) if ticker else None
        if ticker and not tk:
            errors.append("invalid ticker")
        if tk:
            sql += " AND ticker=?"
            args.append(tk)
        if politician:
            pn = normalize_name(politician)
            sql += " AND politician_norm=?"
            args.append(pn)
        sql += " ORDER BY disclosure_date DESC, id LIMIT 500"
        conn = mason_db.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(sql, args)] if not (ticker and not tk) else []
        finally:
            conn.close()
        data["trades"] = [_trade_out(r, held, watched) for r in rows]
        data["held_overlap"] = held_overlap(refresh=False)
        data["leaderboard"] = leaderboard()
    except Exception as e:
        errors.append(_err("api", e))
        return {"ok": False, "as_of": _now(), "data": data, "stale": True, "errors": errors}
    return _strict({"ok": True, "as_of": _now(), "data": data, "stale": bool(_state.get("stale")),
                    "errors": errors})


bp = Blueprint("rhx_congress", __name__)


@bp.route("/api/rh/congress", methods=["GET"])
def api_congress():
    try:
        body = json.dumps(_strict(api_payload(request.args.get("ticker"), request.args.get("politician"),
                                              request.args.get("days"))), allow_nan=False)
        return Response(body, mimetype="application/json")
    except Exception as e:
        return jsonify({"ok": False, "as_of": _now(), "data": None, "stale": True,
                        "errors": [_err("api route", e)]})
