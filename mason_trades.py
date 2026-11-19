"""
mason_trades.py — Stock trade journal, accuracy ledger, and review engine

Tracks Mason's long and short stock trade recommendations.
Covers swing trades (days-weeks), position trades (weeks-months),
and short selling. Mirrors the options journal structure.

Accuracy ledger feeds back into confidence calibration over time.

Persistence
-----------
Backed by SQLite (mason.db, see mason_db.py) as of the 2026-09 journal migration.
The public API — every function name, argument list, and return shape — is
UNCHANGED from the JSON version, so mason_app.py / mason_learning.py /
mason_execution.py needed no edits.

Deletes are SOFT: delete_trade() flips is_deleted=1 instead of removing the row.
_load_journal() reads through the active_trades VIEW (is_deleted = 0), so a
deleted trade is structurally incapable of reaching get_open_trades(),
get_all_trades(), the accuracy ledger, or any model-facing tool response.

trades_accuracy.json is still written as a convenience cache (unchanged format);
it is recomputed from the live table after every write, so it cannot drift.
"""

import json
import re
import unicodedata
import uuid
from datetime import datetime, date
from pathlib import Path

import mason_db as DB

# Kept for the accuracy cache only — the journal itself lives in mason.db now.
ACCURACY_FILE = Path(__file__).parent / "trades_accuracy.json"

# Historical constants. The journal is SQLite now; these paths are no longer the
# source of truth and are left only so any external reference resolves.
JOURNAL_FILE  = Path(__file__).parent / "trades_journal.json"
DELETED_FILE  = Path(__file__).parent / "trades_journal_deleted.json"


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load_journal() -> list:
    """Every active trade, oldest first. Reads the active_trades view, so a
    soft-deleted row can never appear in the result. This is the ONE read path
    every other function in the module funnels through."""
    conn = DB.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM active_trades ORDER BY created_at, id"
        ).fetchall()
        return [DB.row_to_dict(r, "trades") for r in rows]
    finally:
        conn.close()


def _save_journal(trades: list):
    """Upsert every row in the list (insert new, update existing). One
    transaction. A row that is not in the list is left untouched — which is how
    a soft-deleted trade stays gone: it is simply never in the active list that
    gets handed back here."""
    conn = DB.get_connection()
    try:
        DB.upsert_rows(conn, "trades", trades)
    finally:
        conn.close()


def _load_accuracy() -> dict:
    if ACCURACY_FILE.exists():
        with open(ACCURACY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return _empty_accuracy()


def _save_accuracy(data: dict):
    with open(ACCURACY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ── Core journal operations ───────────────────────────────────────────────────

def log_trade(trade_data: dict) -> dict:
    """
    Log a new stock trade recommendation.
    Required fields: ticker, direction (long|short), entry_price,
    stop_loss, target_price, setup_type, time_horizon, thesis, mason_confidence.
    Optional: shares, notes, entry_order_id.
    Returns the saved trade dict with generated id and computed R:R.
    """
    entry_price = float(trade_data.get("entry_price", 0))
    stop_loss   = float(trade_data.get("stop_loss", 0))
    target      = float(trade_data.get("target_price", 0))
    direction   = trade_data.get("direction", "long").lower()

    # Risk and reward per share (direction-aware)
    if direction == "long":
        risk_per_share   = round(entry_price - stop_loss, 4)
        reward_per_share = round(target - entry_price, 4)
    else:  # short
        risk_per_share   = round(stop_loss - entry_price, 4)
        reward_per_share = round(entry_price - target, 4)

    risk_reward = (
        round(reward_per_share / risk_per_share, 2)
        if risk_per_share > 0 else 0
    )

    entry_date = trade_data.get("entry_date") or date.today().isoformat()

    trade = {
        # Identity
        "id":         str(uuid.uuid4())[:8],
        "created_at": datetime.now().isoformat(),
        # Who logged it — 'mason' (the tool wrote it, the default), 'nico', or
        # 'system'. Pairs with created_at for the creation half of the audit trail.
        "created_by": trade_data.get("created_by", "mason"),
        "entry_date": entry_date,
        # Core fields
        "status":        "open",   # open | closed | reviewed
        "ticker":        trade_data.get("ticker", "").upper(),
        "direction":     direction,
        "setup_type":    trade_data.get("setup_type", ""),
        "time_horizon":  trade_data.get("time_horizon", "swing"),
        "shares":        trade_data.get("shares"),
        "entry_price":   entry_price,
        "stop_loss":     stop_loss,
        "target_price":  target,
        # Computed
        "risk_per_share":    risk_per_share,
        "reward_per_share":  reward_per_share,
        "risk_reward_ratio": risk_reward,
        # Thesis
        "thesis":           trade_data.get("thesis", ""),
        "mason_confidence": trade_data.get("mason_confidence", 50),
        "notes":            trade_data.get("notes"),
        # Execution linkage — set when reconciled from a real Alpaca fill by
        # mason_execution._sync_fills_to_journal(). Was silently dropped by the
        # JSON version (the dict below never copied the key through); the column
        # exists now so it is captured.
        "entry_order_id":   trade_data.get("entry_order_id"),
        # Outcome — filled at close time
        "outcome":          None,   # win | loss | breakeven
        "exit_price":       None,
        "exit_date":        None,
        "actual_pnl":       None,
        "exit_reason":      None,   # target_hit | stop_hit | thesis_changed | manual
        "exit_order_id":    None,
        "review_text":      None,
        "thesis_score":     None,   # 0-100, separate from P&L
        "thesis_score_notes": None,
    }

    _save_journal([trade])
    _recompute_accuracy(_load_journal())
    return trade


# Books follow-up 5 (2026-11-16): the fields a person's / the chat's log of a trade adds to the bare row the
# fill reconcile writes ('alpaca-sync': no stop, target, thesis or setup). The fill price, size, entry date and
# order id of that row are the broker's own and are kept.
_ADOPT_FIELDS = ("setup_type", "time_horizon", "stop_loss", "target_price", "thesis", "mason_confidence", "created_by")


def adopt_or_log_trade(trade_data: dict) -> tuple[dict, bool]:
    """log_trade(), unless the journal already has a row for the same Alpaca order (entry_order_id): then no second
    row is written. Returns (row, adopted).

    The fill reconcile (mason_execution._sync_fills_to_journal) writes an 'alpaca-sync' row for a buy it sees before
    the writer that journals the buy with its details (the chat order confirm, the log_stock_trade tool). That row is
    ADOPTED: it takes the caller's descriptive fields (_ADOPT_FIELDS; notes are appended) and keeps the broker's
    price, size, date and order id; risk/reward are recomputed from its real entry price. Every open or closed active
    row of that order is updated the same way (a sell may have split it). A row of that order that is NOT an
    'alpaca-sync' row was journaled with its details already: it is returned unchanged.
    Without an entry_order_id, or with no row for it, this is exactly log_trade()."""
    oid = str(trade_data.get("entry_order_id") or "").strip()
    hits = [t for t in _load_journal() if oid and str(t.get("entry_order_id") or "").strip() == oid]
    if not hits:
        return log_trade(trade_data), False
    synced = [t for t in hits if (t.get("setup_type") or "") == "alpaca-sync"]
    if not synced:
        return hits[0], True
    out = []
    for t in synced:
        t = dict(t)
        for k in _ADOPT_FIELDS:
            if trade_data.get(k) not in (None, ""):
                t[k] = trade_data[k]
        if trade_data.get("notes"):
            t["notes"] = ((t.get("notes") or "") + " " + str(trade_data["notes"])).strip()
        entry = float(t.get("entry_price") or 0)
        stop, target = float(t.get("stop_loss") or 0), float(t.get("target_price") or 0)
        if (t.get("direction") or "long").lower() == "long":
            risk, reward = round(entry - stop, 4), round(target - entry, 4)
        else:
            risk, reward = round(stop - entry, 4), round(entry - target, 4)
        t["risk_per_share"], t["reward_per_share"] = risk, reward
        t["risk_reward_ratio"] = round(reward / risk, 2) if risk > 0 else 0
        out.append(t)
    _save_journal(out)
    _recompute_accuracy(_load_journal())
    return out[0], True


def get_open_trades() -> list:
    return [t for t in _load_journal() if t.get("status") == "open"]


def get_all_trades() -> list:
    return _load_journal()


def get_trade_by_id(trade_id: str) -> dict | None:
    """Direct single-row lookup by exact id. Deliberately NOT filtered by
    is_deleted — callers (e.g. close_trade()) already know the specific id they
    want, deleted or not. Never used to build a list handed to the model."""
    conn = DB.get_connection()
    try:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return DB.row_to_dict(row, "trades")
    finally:
        conn.close()


# Phase-2 A9 round 2 (DEBUG_A9 §2): the guard fails CLOSED on every shape it cannot vouch for.
_LINK_DEPTH_CAP = 64          # nesting levels searched for a _link; deeper -> refuse
_EXTRA_DECODE_LAYERS = 4      # string layers peeled off a double-encoded extra_json
_PERSONAL_EXIT_REASON = "personal_account_trade"


def _norm_token(v, cf_as_separator: bool = False) -> str:
    """Canonical form for comparing a token (round 3, DEBUG_A9 R2-N2): NFKC (full-width ->
    ASCII), drop format/zero-width characters (Unicode Cf), lower-case, collapse runs of
    whitespace / '_' / '-' to one '_', strip leading/trailing punctuation and underscores.
    'personal__account__trade.', 'Personal Account-Trade', 'personal\u200baccount_trade'
    all become 'personal_account_trade'."""
    t = unicodedata.normalize("NFKC", str(v if v is not None else ""))
    sep = " " if cf_as_separator else ""
    t = "".join((sep if unicodedata.category(ch) == "Cf" else ch) for ch in t)
    t = re.sub(r"[\s_\-]+", "_", t.strip().lower())
    return re.sub(r"^[\W_]+|[\W_]+$", "", t)


def _is_personal_reason(v) -> bool:
    """exit_reason names a personal-account trade, under either reading of a format
    character (dropped: 'pers\u200bonal'; or a word break: 'personal\u200baccount')."""
    return _PERSONAL_EXIT_REASON in (_norm_token(v), _norm_token(v, cf_as_separator=True))


def _decode_extra(raw):
    """Parse a RAW extra_json value. Returns (obj, problem): problem is None only for an
    empty value or a plain JSON object. A double-encoded value is peeled so its _link can
    still be named, but it is reported as a problem (row_to_dict cannot read it either)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return {}, None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        obj = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None, "has unreadable extra_json"
    layers = 0
    while isinstance(obj, str) and layers < _EXTRA_DECODE_LAYERS:
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError, RecursionError):
            break
        layers += 1
    if layers == 0 and isinstance(obj, dict):
        return obj, None
    return obj, "has extra_json that is not a plain JSON object (double-encoded, list or scalar)"


def _link_target(lv):
    """What one `_link` value points at: None = not a personal link (a dict naming only
    other tables, or null); otherwise a short description. A list, string or any other
    non-dict _link counts as linked (fail closed)."""
    if lv is None:
        return None
    if isinstance(lv, dict):
        named = _personal_key_id(lv)
        if named is not None:
            return f"linked to personal_positions {named}"
        # Round 3 (DEBUG_A9 R2-N1): any mention of personal_positions anywhere in the
        # _link (a key, a value, nested) counts as linked.
        blob = _norm_blob(lv)
        if "personal_positions" in blob:
            return f"linked to personal_positions (per _link {blob[:80]})"
        return None
    if isinstance(lv, list):
        for el in lv:
            if isinstance(el, dict):
                named = _personal_key_id(el)
                if named is not None:
                    return f"linked to personal_positions {named}"
    return f"has a _link of unrecognised shape ({json.dumps(lv, default=str)[:80]})"


def _personal_key_id(d: dict):
    """The id under a top-level key of d that normalises to 'personal_positions'
    (case / space / '-' variants included), as text; '(blank id)' if empty; None if no such key."""
    for k, v in d.items():
        if _norm_token(k) == "personal_positions":
            return str(v) if v not in (None, "") else "(blank id)"
    return None


def _norm_blob(o) -> str:
    """The whole value as lower-cased NFKC JSON text, format characters dropped, so a
    'personal_positions' mention is found in any key or value at any depth."""
    t = unicodedata.normalize("NFKC", json.dumps(o, default=str, ensure_ascii=False)).lower()
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def _scan_extra(obj):
    """Search every dict and list in extra_json, at any depth, for a personal `_link`.
    Iterative, so no recursion limit applies. Returns a description, or None if clean.
    Hitting the depth cap refuses (fail closed)."""
    stack = [(obj, 0)]
    while stack:
        o, d = stack.pop()
        if not isinstance(o, (dict, list)):
            continue
        if d > _LINK_DEPTH_CAP:
            return f"has extra_json nested deeper than {_LINK_DEPTH_CAP} levels (not inspected)"
        if isinstance(o, dict):
            if "_link" in o:
                hit = _link_target(o["_link"])
                if hit:
                    return hit
            stack.extend((v, d + 1) for v in o.values())
        else:
            stack.extend((v, d + 1) for v in o)
    return None


def close_refusal(trade_id: str) -> tuple[bool, str | None]:
    """Phase-2 A9 (RULINGS #30): may close_trade() write this row?

    Returns (found, reason). found is False when no row with this exact id exists
    at all (close_trade then returns None, as it always has). reason is None when
    the close is allowed, else a plain refusal message. Reads the RAW row (not
    row_to_dict, which drops is_deleted and flattens extra_json) so every
    condition is checked against what is actually stored.

    Round 2: refuses on a _link at any depth (dicts and lists), a list/string
    _link, a double-encoded / non-object / unparseable extra_json, the depth cap,
    and any case/whitespace variant of exit_reason 'personal_account_trade'.

    Known limits (RULINGS #38, accepted by the Director): homoglyph spellings (e.g. a
    Cyrillic 'о' inside 'personal') and combining-accent variants (a combining acute
    turning 'trade' into 'tradé') are NOT folded to the ASCII token, so such an
    exit_reason is not recognised as personal. The guard's threat model is accidental
    or model-driven drift in stored / tool-supplied values, not forgery by a writer
    with direct DB access (who could simply change `status`). Folding confusables
    would add a dependency and false positives out of proportion to that threat."""
    conn = DB.get_connection()
    try:
        row = conn.execute(
            "SELECT status, is_deleted, exit_reason, extra_json FROM trades WHERE id = ?",
            (trade_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return False, None
    status, is_deleted, exit_reason, extra_raw = row[0], row[1], row[2], row[3]
    extra, problem = _decode_extra(extra_raw)
    link = _scan_extra(extra) if extra is not None else None
    personal = _is_personal_reason(exit_reason)
    why = []
    if is_deleted:
        why.append("is deleted")
    if status != "open":
        why.append(f"is already {status or 'not open'}")
    if personal and link and link.startswith("linked to personal_positions"):
        why.append("is a personal-account trade " + link)
    else:
        if personal:
            why.append("is a personal-account trade")
        if link:
            why.append(("is a personal-account trade " + link)
                        if link.startswith("linked to personal_positions") else link)
    if problem:
        why.append(problem)
    if not why:
        return True, None
    return True, f"trade {trade_id} " + " / ".join(why) + "; not changed"


def close_trade(trade_id: str, exit_price: float, exit_reason: str,
                review_text: str, thesis_score: int = None,
                thesis_notes: str = "", shares_override: int = None) -> dict | None:
    """
    Close a trade and record the outcome.
    exit_reason: target_hit | stop_hit | thesis_changed | manual
    thesis_score: 0-100, separate from P&L (good thesis that lost = 70+, lucky win = 20)

    Phase-2 A9 guard (RULINGS #30): raises ValueError, and writes nothing (no
    accuracy recompute), when the row is not open, is soft-deleted, has an
    extra_json _link to a personal_positions row, or has exit_reason
    'personal_account_trade'. There is no override. A missing id returns None.
    """
    found, refusal = close_refusal(trade_id)
    if not found:
        return None
    if refusal:
        raise ValueError(refusal)
    trades = _load_journal()
    for i, t in enumerate(trades):
        if t["id"] != trade_id:
            continue

        ep        = float(t.get("entry_price", 0))
        direction = t.get("direction", "long")
        shares    = shares_override or t.get("shares") or 0

        if direction == "long":
            pnl_per_share = exit_price - ep
        else:
            pnl_per_share = ep - exit_price

        actual_pnl = round(pnl_per_share * shares, 2) if shares else round(pnl_per_share, 4)
        outcome    = "win" if pnl_per_share > 0 else ("loss" if pnl_per_share < 0 else "breakeven")

        trades[i].update({
            "status":       "closed",
            "outcome":      outcome,
            "exit_price":   exit_price,
            "exit_date":    date.today().isoformat(),
            "exit_reason":  exit_reason,
            "actual_pnl":   actual_pnl,
            "review_text":  review_text,
            "thesis_score": thesis_score,
            "thesis_score_notes": thesis_notes,
        })

        _save_journal(trades)
        _recompute_accuracy(trades)
        return trades[i]

    return None


def delete_trade(trade_id: str, reason: str = "", force: bool = False,
                 deleted_by: str = "nico") -> dict:
    """
    Soft-delete a trade: sets is_deleted=1, deleted_at=now, deleted_reason=reason,
    deleted_by=who ('nico' by default — a delete is usually Nico asking; pass
    'system' from a maintenance script, 'mason' if the assistant decided).
    Use for test/placeholder entries or trades logged by mistake — never to erase
    the history of a real trade.

    The row stays in the `trades` table forever (readable via a direct id lookup
    or an explicit audit query); it just stops appearing in active_trades /
    get_open_trades() / get_all_trades() from this call onward — which is the
    same audit-trail guarantee the old trades_journal_deleted.json shadow file
    gave, enforced structurally instead of by a second file.

    Refuses to delete a trade carrying an entry_order_id (reconciled from a real
    Alpaca fill) unless force=True is explicitly passed.

    Returns {"ok": bool, "deleted": trade|None, "error": str|None}.
    """
    conn = DB.get_connection()
    try:
        trade = get_trade_by_id(trade_id)
        if trade is None:
            return {"ok": False, "deleted": None, "error": f"Trade {trade_id} not found in the journal."}

        flag = conn.execute("SELECT is_deleted FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if flag and flag[0]:
            return {"ok": False, "deleted": None,
                    "error": f"Trade {trade_id} is already deleted."}

        if trade.get("entry_order_id") and not force:
            return {
                "ok": False,
                "deleted": None,
                "error": (
                    f"Trade {trade_id} ({trade.get('ticker')} {trade.get('direction')}) is tied to a "
                    f"real Alpaca fill (order {trade.get('entry_order_id')}). Refusing to delete without "
                    f"force=true — this would erase the record of a real trade."
                ),
            }

        DB.soft_delete(conn, "trades", trade_id,
                       reason or "no reason given", datetime.now().isoformat(),
                       deleted_by=deleted_by)
        _recompute_accuracy(_load_journal())
        return {"ok": True, "deleted": get_trade_by_id(trade_id), "error": None}
    finally:
        conn.close()


# ── Accuracy ledger ───────────────────────────────────────────────────────────

def _empty_accuracy() -> dict:
    """A fresh, fully-reset ledger. win_rate/avg_pnl/total_pnl are None (not 0,
    not 1.0) when there is nothing graded — "no data" must never read as a score."""
    return {
        "total_trades":     0,
        "closed_trades":    0,
        "win_rate":         None,
        "avg_pnl":          None,
        "total_pnl":        None,
        "by_direction":     {},
        "by_setup_type":    {},
        "by_ticker":        {},
        "by_time_horizon":  {},
        "confidence_calibration": {},
        "avg_thesis_score": None,
        "last_updated":     None,
    }


def _recompute_accuracy(trades: list):
    """Rebuild trades_accuracy.json FROM SCRATCH out of the active journal.

    Fixed 2026-09-28 (knowledge audit): this used to start from _load_accuracy()
    — the PREVIOUS file — and only overwrite some keys. When the closed set went
    empty it saved early, so the old win_rate/avg_pnl/total_pnl survived next to
    total_trades=0 (git 61eb32e: total_trades 0, win_rate 1.0); and by_direction /
    by_time_horizon were merged into the old dicts, so a direction or horizon
    with no remaining closed trades kept its stale numbers forever. Nothing from
    the old file is carried over now."""
    trades = [t for t in trades if not t.get("is_deleted")]
    acc = compute_accuracy(trades)
    _save_accuracy(acc)
    return acc


def compute_accuracy(trades: list) -> dict:
    """Pure aggregate math for the stock ledger (no I/O). Only rows with a real
    outcome (win|loss|breakeven) count; P&L sums skip rows with no actual_pnl."""
    closed = [t for t in trades if t.get("outcome") in ("win", "loss", "breakeven")]

    acc = _empty_accuracy()
    acc["total_trades"]  = len(trades)
    acc["closed_trades"] = len(closed)
    acc["last_updated"]  = date.today().isoformat()

    if not closed:
        return acc

    wins = [t for t in closed if t.get("outcome") == "win"]
    acc["win_rate"] = round(len(wins) / len(closed), 3)

    pnls = [t["actual_pnl"] for t in closed if t.get("actual_pnl") is not None]
    acc["total_pnl"] = round(sum(pnls), 2) if pnls else None
    acc["avg_pnl"]   = round(sum(pnls) / len(pnls), 2) if pnls else None

    # By direction
    for direction in ("long", "short"):
        subset = [t for t in closed if t.get("direction") == direction]
        if subset:
            dw = len([t for t in subset if t.get("outcome") == "win"])
            dpnls = [t["actual_pnl"] for t in subset if t.get("actual_pnl") is not None]
            acc["by_direction"][direction] = {
                "trades":   len(subset),
                "wins":     dw,
                "win_rate": round(dw / len(subset), 3),
                "total_pnl": round(sum(dpnls), 2) if dpnls else None,
            }

    # By setup type
    by_setup: dict = {}
    for t in closed:
        s = t.get("setup_type", "unknown")
        if s not in by_setup:
            by_setup[s] = {"trades": 0, "wins": 0, "pnl_sum": 0, "pnl_count": 0}
        by_setup[s]["trades"] += 1
        if t.get("outcome") == "win":
            by_setup[s]["wins"] += 1
        if t.get("actual_pnl") is not None:
            by_setup[s]["pnl_sum"]   += t["actual_pnl"]
            by_setup[s]["pnl_count"] += 1
    for s, d in by_setup.items():
        d["win_rate"] = round(d["wins"] / d["trades"], 3)
        d["avg_pnl"]  = round(d["pnl_sum"] / d["pnl_count"], 2) if d["pnl_count"] else None
        del d["pnl_sum"], d["pnl_count"]
    acc["by_setup_type"] = by_setup

    # By ticker
    by_ticker: dict = {}
    for t in closed:
        tk = t.get("ticker", "unknown")
        if tk not in by_ticker:
            by_ticker[tk] = {"trades": 0, "wins": 0}
        by_ticker[tk]["trades"] += 1
        if t.get("outcome") == "win":
            by_ticker[tk]["wins"] += 1
    for tk, d in by_ticker.items():
        d["win_rate"] = round(d["wins"] / d["trades"], 3)
    acc["by_ticker"] = by_ticker

    # By time horizon
    for horizon in ("intraday", "swing", "position"):
        subset = [t for t in closed if t.get("time_horizon") == horizon]
        if subset:
            hw = len([t for t in subset if t.get("outcome") == "win"])
            acc["by_time_horizon"][horizon] = {
                "trades":   len(subset),
                "wins":     hw,
                "win_rate": round(hw / len(subset), 3),
            }

    # Confidence calibration
    conf_buckets: dict = {}
    for t in closed:
        c = t.get("mason_confidence")
        if c is None:
            continue
        lo     = (int(c) // 10) * 10
        bucket = f"{lo}-{lo+9}"
        if bucket not in conf_buckets:
            conf_buckets[bucket] = {"trades": 0, "wins": 0}
        conf_buckets[bucket]["trades"] += 1
        if t.get("outcome") == "win":
            conf_buckets[bucket]["wins"] += 1
    for b, d in conf_buckets.items():
        d["win_rate"] = round(d["wins"] / d["trades"], 3)
    acc["confidence_calibration"] = conf_buckets

    # Thesis scores
    scores = [t["thesis_score"] for t in closed if t.get("thesis_score") is not None]
    acc["avg_thesis_score"] = round(sum(scores) / len(scores), 1) if scores else None

    return acc


# ── Display formatters ────────────────────────────────────────────────────────

def format_open_trades() -> str:
    trades = get_open_trades()
    if not trades:
        return "No open stock trades."
    lines = [f"{len(trades)} open stock trade(s):"]
    for t in trades:
        direction = t.get("direction", "long").upper()
        rr        = t.get("risk_reward_ratio", "?")
        lines.append(
            f"  [{t['id']}] {t['ticker']} {direction} | {t.get('setup_type','')} | "
            f"entry ${t['entry_price']} | stop ${t['stop_loss']} | "
            f"target ${t['target_price']} | {rr}:1 R:R | "
            f"conf {t.get('mason_confidence')}% | {t.get('time_horizon','')}"
        )
    return "\n".join(lines)


def get_accuracy_summary() -> str:
    acc    = _load_accuracy()
    trades = _load_journal()
    open_t = [t for t in trades if t.get("status") == "open"]

    if acc["closed_trades"] == 0:
        return (
            f"No closed stock trades yet. "
            f"{acc['total_trades']} logged, {len(open_t)} currently open."
        )

    lines = []
    wr = round(acc["win_rate"] * 100) if acc["win_rate"] is not None else "N/A"
    lines.append(f"Stock trade record: {acc['closed_trades']} closed, {wr}% win rate.")

    if acc.get("total_pnl") is not None:
        v = acc["total_pnl"]
        lines.append(f"Total realized P&L: {'-' if v < 0 else '+'}${abs(v):,.2f}")

    if acc.get("by_direction"):
        parts = []
        for d, rec in acc["by_direction"].items():
            wr_d = round(rec["win_rate"] * 100)
            parts.append(f"{d} {rec['wins']}/{rec['trades']} ({wr_d}%)")
        lines.append("By direction: " + " | ".join(parts))

    if acc.get("by_setup_type"):
        parts = []
        for s, d in sorted(acc["by_setup_type"].items(), key=lambda x: -x[1]["trades"]):
            wr_s = round(d["win_rate"] * 100)
            parts.append(f"{s} ({d['trades']} trades, {wr_s}% win rate)")
        lines.append("By setup: " + "; ".join(parts))

    if acc.get("confidence_calibration"):
        lines.append("Confidence calibration:")
        for bucket, d in sorted(acc["confidence_calibration"].items()):
            wr_b = round(d["win_rate"] * 100)
            lines.append(f"  {bucket}% conf → {wr_b}% win rate ({d['trades']} trades)")

    if acc.get("avg_thesis_score") is not None:
        lines.append(f"Avg thesis score: {acc['avg_thesis_score']}/100")

    lines.append(f"Open: {len(open_t)} trades.")
    return "\n".join(lines)
