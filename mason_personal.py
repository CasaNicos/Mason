"""
mason_personal.py — Personal (non-Alpaca) brokerage positions Nico enters by hand.

These are real holdings in Nico's own outside accounts (a Roth IRA, Schwab,
etc.) that Alpaca has no visibility into. Each row is one lot: buying the same
ticker twice in the same account creates two rows, never auto-averaged, so cost
basis and holding period stay accurate per purchase. account_label keeps the
same ticker held in two different accounts tracking two independent P/Ls.

Two ways in:
  - UI form (add_position_direct / close_position_direct / edit_position) —
    submitting the form IS the confirming act, so these write straight to
    status='open' / 'closed' with no staging.
  - Chat (stage_position / stage_close / confirm_position) — Mason parses a
    position out of conversation, stages it (status='pending_confirm' or
    pending_action='close'), and it is NOT real until Nico explicitly confirms
    in the same conversation. A misheard "I bought" or "I sold" must never
    silently write or close a real position.
"""

import uuid
from datetime import datetime

import mason_db as DB

TABLE = "personal_positions"
VIEW = "active_" + TABLE
CASH_TICKER = "CASH"   # sentinel ticker: an uninvested cash balance, not a real symbol

# ── Robinhood-synced accounts: manual-entry guard (RH Expansion S1-T10) ──
# After the personal-positions cutover (mason_robinhood.sync_personal_positions, desk_state
# 'rh_personal_cutover'), the accounts it mapped (hand label AND Robinhood name, e.g. Roth / Roth IRA)
# are owned by the Robinhood sync. Every add / edit / close / delete aimed at one is refused with
# RH_SYNC_REFUSAL; the HTTP routes surface it unchanged through their ValueError → 400 path. Before
# the cutover (no marker) nothing is guarded, and non-Robinhood accounts are never guarded.
RH_SYNC_REFUSAL = "This account syncs from Robinhood — change it in the Robinhood app."
_RH_SYNC_BY = "robinhood_sync"
_RH_CUTOVER_KEY = "rh_personal_cutover"
_RH_DEFAULT_LABEL_MAP = {"Play": "Play Money", "Roth": "Roth IRA", "Savings": "ETF Savings"}


_RH_ALIAS_KEY = "rh_account_aliases"


def _norm_label(s) -> str:
    """The alias history's form of a label: mason_robinhood.norm_account_label (NFKC, no format characters, no
    invisible fillers, casefolded). A local NFKC/no-Cf fallback only if that module cannot be imported."""
    try:
        import mason_robinhood
        return mason_robinhood.norm_account_label(str(s))
    except Exception:
        import unicodedata
        try:
            t = unicodedata.normalize("NFKC", str(s))
            return "".join(c for c in t if unicodedata.category(c) != "Cf").strip().casefold()
        except Exception:
            return str(s).strip().lower()


def _alias_labels_of_marker(marker, alias_blob) -> set:
    """Phase 2 A2 (RULINGS #4.5): in last-4 map mode the cutover marker records accounts_last4 {name at cutover:
    role-map key}. Every label ever recorded for those keys (desk_state rh_account_aliases, read-only, no Robinhood
    call) is also a synced label, so a hand entry under an account's OLD or NEW name is still refused. Empty without
    a binding (legacy mode, or before the first map-mode reconcile)."""
    import json
    try:
        bound = marker.get("accounts_last4") if isinstance(marker, dict) else None
        if not isinstance(bound, dict) or not alias_blob:
            return set()
        keys = {str(v) for v in bound.values() if isinstance(v, str) and v}
        d = json.loads(alias_blob)
        out = set()
        for ek, e in (d.items() if isinstance(d, dict) else ()):
            if any(ek == k or k.endswith(ek) or ek.endswith(k) for k in keys):
                out |= {_norm_label(x) for x in (e.get("labels") if isinstance(e, dict) else None) or []}
        return {x for x in out if x}
    except Exception:
        return set()


def _rh_synced_labels() -> set:
    """Casefolded labels of the accounts the cutover handed to Robinhood; empty before it. A marker
    that cannot be read as expected fails CLOSED onto the default label map. In last-4 map mode it also holds
    every alias-history label of the marker's bound accounts (see _alias_labels_of_marker)."""
    import json
    conn = DB.get_connection()
    try:
        r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_RH_CUTOVER_KEY,)).fetchone()
        al = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_RH_ALIAS_KEY,)).fetchone() if r else None
    finally:
        conn.close()
    if not r:
        return set()
    m = None
    try:
        m = json.loads(r[0] or "null")
        lmap = m.get("label_map") if isinstance(m, dict) else None
        names = list(lmap.keys()) + list(lmap.values()) + list(m.get("accounts") or [])
    except Exception:
        names = []
    if not names:
        names = list(_RH_DEFAULT_LABEL_MAP) + list(_RH_DEFAULT_LABEL_MAP.values())
    out = {str(n).strip().lower() for n in names if str(n).strip()}
    return out | _alias_labels_of_marker(m, al[0] if al else None)


def _is_rh_synced(account_label: str = None, row: dict = None) -> bool:
    if row is not None and row.get("created_by") == _RH_SYNC_BY:
        return True
    labels = [account_label] + ([row.get("account_label")] if row is not None else [])
    labels = [str(x).strip().lower() for x in labels if x is not None and str(x).strip()]
    labels += [n for n in (_norm_label(x) for x in labels) if n and n not in labels]   # typed Unicode variants
    if not labels:
        return False
    synced = _rh_synced_labels()
    return any(lab in synced for lab in labels)


def _refuse_if_rh_synced(account_label: str = None, row: dict = None) -> None:
    if _is_rh_synced(account_label, row):
        raise ValueError(RH_SYNC_REFUSAL)


# ── formatting ────────────────────────────────────────────────────

def _signed_usd(v) -> str:
    """'+$60.00' / '-$60.00' — sign BEFORE the $, cents always. For P/L text Mason
    reads back and speaks. Falls back to str(v) for a non-numeric so it never
    raises inside an f-string."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{'-' if n < 0 else '+'}${abs(n):,.2f}"


# ── I/O helpers ───────────────────────────────────────────────────

def _load_all() -> list:
    """Every active (non-deleted) personal position, oldest first."""
    conn = DB.get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM {VIEW} ORDER BY created_at, id").fetchall()
        return [DB.row_to_dict(r, TABLE) for r in rows]
    finally:
        conn.close()


def _save(rows: list):
    conn = DB.get_connection()
    try:
        DB.upsert_rows(conn, TABLE, rows)
    finally:
        conn.close()


def _default_row(ticker: str, shares: float, cost_basis: float, account_label: str,
                  entry_date: str, notes: str = None, created_by: str = "nico") -> dict:
    now = datetime.now().isoformat()
    return {
        "id": str(uuid.uuid4())[:8],
        "created_at": now,
        "created_by": created_by,
        "updated_at": now,
        "ticker": ticker.strip().upper(),
        "account_label": account_label.strip(),
        "shares": float(shares),
        "cost_basis": float(cost_basis),
        "entry_date": entry_date,
        "status": "open",
        "pending_action": None,
        "pending_close_price": None,
        "pending_close_date": None,
        "close_date": None,
        "close_price": None,
        "realized_pl": None,
        "notes": notes,
    }


def _canonical_account_label(label: str) -> str:
    """Snap a typed account label onto whatever casing/spacing is already on
    file for it (case-insensitive match), so 'Roth' and 'roth' collapse into
    one account instead of silently forking into two separate groups. The
    first spelling ever used for an account wins and everything after matches
    it. Unrecognized labels pass through unchanged (trimmed only) — this is
    what lets a brand-new account get created in the first place."""
    label = (label or "").strip()
    if not label:
        return label
    key = label.lower()
    for p in _load_all():
        existing = (p.get("account_label") or "").strip()
        if existing.lower() == key:
            return existing
    return label


def get_position_by_id(position_id: str) -> dict | None:
    for p in _load_all():
        if p["id"] == position_id:
            return p
    return None


def get_open_positions() -> list:
    """status='open' — real, confirmed, currently-held positions."""
    return [p for p in _load_all() if p.get("status") == "open"]


def get_pending_positions() -> list:
    """Rows awaiting a yes/no in chat — either a staged add (status=
    'pending_confirm') or an open row with a staged close
    (pending_action='close')."""
    return [p for p in _load_all() if p.get("pending_action")]


def get_closed_positions() -> list:
    """status='closed', most recent close first — the realized P/L history."""
    rows = [p for p in _load_all() if p.get("status") == "closed"]
    return sorted(rows, key=lambda p: p.get("close_date") or "", reverse=True)


# ── UI-driven (already confirmed by the click) ─────────────────────

def add_position_direct(ticker: str, shares: float, cost_basis: float,
                         account_label: str, entry_date: str, notes: str = None) -> dict:
    """Insert a position straight to status='open'. For the dashboard "+ Add
    position" form — submitting it is itself the confirming act, no staging."""
    _refuse_if_rh_synced(account_label)
    row = _default_row(ticker, shares, cost_basis, _canonical_account_label(account_label),
                        entry_date, notes=notes, created_by="nico")
    _save([row])
    return row


def add_cash_direct(account_label: str, amount: float, entry_date: str = None, notes: str = None) -> dict:
    """Add a cash balance to an account — a lighter sibling of add_position_direct
    for money that isn't in any ticker. Modeled as a position with ticker=CASH
    and cost_basis pinned to $1, so 'shares' IS the dollar amount and there's
    never a spurious unrealized P/L on it (see is_cash handling in
    mason_app.api_personal_positions, which prices it at a fixed $1 instead of
    quoting 'CASH' as a symbol). Each add is its own row, same as an equity
    lot — topping up an account creates a second CASH row rather than merging,
    consistent with 'never auto-average, Nico manages the rows.'"""
    amount = float(amount)
    if amount <= 0:
        raise ValueError("Cash amount must be positive.")
    return add_position_direct(
        ticker=CASH_TICKER, shares=amount, cost_basis=1.0,
        account_label=account_label,
        entry_date=entry_date or datetime.now().date().isoformat(),
        notes=notes,
    )


def close_position_direct(position_id: str, close_price: float, close_date: str) -> dict:
    """Close an open position immediately, computing realized P/L. Raises
    ValueError if the position doesn't exist or isn't open — callers (the HTTP
    route) turn that into a 400, matching this codebase's tool-result
    convention of never silently no-oping a bad id."""
    pos = get_position_by_id(position_id)
    if pos is None:
        raise ValueError(f"Personal position {position_id} not found.")
    _refuse_if_rh_synced(row=pos)
    if pos.get("status") != "open":
        raise ValueError(f"Personal position {position_id} is '{pos.get('status')}', not open — nothing to close.")
    close_price = float(close_price)
    pos["close_price"] = close_price
    pos["close_date"] = close_date
    pos["realized_pl"] = round((close_price - pos["cost_basis"]) * pos["shares"], 2)
    pos["status"] = "closed"
    pos["pending_action"] = None
    pos["pending_close_price"] = None
    pos["pending_close_date"] = None
    pos["updated_at"] = datetime.now().isoformat()
    _save([pos])
    return pos


def edit_position(position_id: str, **fields) -> dict:
    """UI-driven correction of an open or pending row's editable fields
    (ticker, shares, cost_basis, account_label, entry_date, notes). No confirm
    step — an explicit edit in the UI needs no second confirmation."""
    pos = get_position_by_id(position_id)
    if pos is None:
        raise ValueError(f"Personal position {position_id} not found.")
    _refuse_if_rh_synced(fields.get("account_label"), row=pos)
    editable = {"ticker", "shares", "cost_basis", "account_label", "entry_date", "notes"}
    for k, v in fields.items():
        if k not in editable:
            continue
        if k == "ticker" and v:
            v = v.strip().upper()
        if k == "account_label" and v:
            v = _canonical_account_label(v)
        if k in ("shares", "cost_basis") and v is not None:
            v = float(v)
        pos[k] = v
    pos["updated_at"] = datetime.now().isoformat()
    _save([pos])
    return pos


def delete_position(position_id: str, deleted_by: str = "nico", reason: str = "") -> None:
    """Soft-delete — for a mis-entered row via the UI 'remove' action. Never
    physically removed; just drops out of every active_* read from here on."""
    conn = DB.get_connection()
    try:
        pos = get_position_by_id(position_id)
        if pos is None:
            raise ValueError(f"Personal position {position_id} not found.")
        _refuse_if_rh_synced(row=pos)
        DB.soft_delete(conn, TABLE, position_id, reason or "removed via UI",
                        datetime.now().isoformat(), deleted_by=deleted_by)
    finally:
        conn.close()


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


# ── Chat-driven (staged, requires explicit confirmation) ────────────

def stage_position(ticker: str, shares: float, cost_basis: float, account_label: str,
                    entry_date: str, notes: str = None, created_by: str = "mason") -> dict:
    """Stage a new position parsed out of conversation. NOT a real position
    yet — status='pending_confirm'. Returns the row plus a 'summary' string
    the model should relay verbatim so Nico sees exactly what will be saved."""
    _refuse_if_rh_synced(account_label)
    row = _default_row(ticker, shares, cost_basis, _canonical_account_label(account_label),
                        entry_date, notes=notes, created_by=created_by)
    row["status"] = "pending_confirm"
    row["pending_action"] = "add"
    _save([row])
    summary = (
        f"Staged (not yet saved): {row['shares']:g} shares of {row['ticker']} "
        f"@ ${row['cost_basis']:.2f} in \"{_bl(row['account_label'])}\", entered {row['entry_date']}"
        + (f" — notes: {notes}" if notes else "")
        + f". Position id {row['id']}. Reply to confirm or cancel before this is recorded."
    )
    row["summary"] = summary
    return row


def stage_close(position_id: str = None, close_price: float = None, close_date: str = None,
                 ticker: str = None, account_label: str = None) -> str:
    """Stage a close on an existing OPEN position, resolved either by
    position_id directly or by (ticker, account_label) uniquely matching one
    open row. Returns a plain-text confirmation summary, or an error string if
    the position can't be resolved unambiguously — this function never raises
    for a resolution failure, since its only caller is a chat tool that must
    surface the message to the model as the tool result."""
    if close_price is None or close_date is None:
        return "[NOT STAGED] close_price and close_date are both required."

    if position_id:
        pos = get_position_by_id(position_id)
        if pos is None:
            return f"[NOT STAGED] Personal position {position_id} not found."
        if pos.get("status") != "open":
            return f"[NOT STAGED] Personal position {position_id} is '{pos.get('status')}', not open."
    else:
        if not (ticker and account_label):
            return "[NOT STAGED] Provide either position_id, or both ticker and account_label."
        matches = [p for p in get_open_positions()
                   if p["ticker"] == ticker.strip().upper()
                   and p["account_label"].lower() == account_label.strip().lower()]
        if not matches:
            return f"[NOT STAGED] No open position for {ticker} in \"{_bl(account_label)}\"."
        if len(matches) > 1:
            ids = ", ".join(p["id"] for p in matches)
            return (f"[NOT STAGED] {len(matches)} open lots of {ticker} in \"{_bl(account_label)}\" "
                    f"(ids: {ids}) — specify position_id to pick one.")
        pos = matches[0]

    if _is_rh_synced(row=pos):
        return f"[NOT STAGED] {RH_SYNC_REFUSAL}"
    pos["pending_action"] = "close"
    pos["pending_close_price"] = float(close_price)
    pos["pending_close_date"] = close_date
    pos["updated_at"] = datetime.now().isoformat()
    _save([pos])
    est_pl = round((float(close_price) - pos["cost_basis"]) * pos["shares"], 2)
    return (
        f"Staged close (not yet saved): {pos['shares']:g} shares of {pos['ticker']} "
        f"in \"{_bl(pos['account_label'])}\" @ ${float(close_price):.2f} on {close_date} "
        f"(est. realized P/L {_signed_usd(est_pl)}). Position id {pos['id']}. "
        f"Reply to confirm or cancel before this is recorded."
    )


def confirm_position(position_id: str, confirm: bool) -> str:
    """Resolve a staged add or close. Returns a plain-text result describing
    exactly what happened — the tool caller must return this string verbatim
    as the tool result and never narrate its own version of it, matching this
    codebase's rule that a tool's real result is the only truth a model may
    state (see the place_paper_order / close_paper_position fabrication
    guard)."""
    pos = get_position_by_id(position_id)
    if pos is None:
        return f"[NOT CONFIRMED] Personal position {position_id} not found."
    action = pos.get("pending_action")
    if not action:
        return f"[NOT CONFIRMED] Personal position {position_id} has nothing pending to confirm."
    if confirm and _is_rh_synced(row=pos):
        return f"[NOT CONFIRMED] {RH_SYNC_REFUSAL}"

    if action == "add":
        if confirm:
            pos["status"] = "open"
            pos["pending_action"] = None
            pos["updated_at"] = datetime.now().isoformat()
            _save([pos])
            return (f"CONFIRMED and saved: {pos['shares']:g} shares of {pos['ticker']} "
                    f"@ ${pos['cost_basis']:.2f} in \"{_bl(pos['account_label'])}\" "
                    f"(entry {pos['entry_date']}), position id {pos['id']}.")
        else:
            DB.with_connection(DB.soft_delete, TABLE, position_id,
                            "cancelled by user before confirmation",
                            datetime.now().isoformat(), deleted_by="nico")
            return f"CANCELLED: staged position {position_id} was discarded, nothing was saved."

    if action == "close":
        if confirm:
            close_price = pos.pop("pending_close_price")
            close_date = pos.pop("pending_close_date")
            pos["close_price"] = close_price
            pos["close_date"] = close_date
            pos["realized_pl"] = round((close_price - pos["cost_basis"]) * pos["shares"], 2)
            pos["status"] = "closed"
            pos["pending_action"] = None
            pos["updated_at"] = datetime.now().isoformat()
            _save([pos])
            return (f"CONFIRMED and closed: {pos['shares']:g} shares of {pos['ticker']} "
                    f"in \"{_bl(pos['account_label'])}\" @ ${close_price:.2f} on {close_date}, "
                    f"realized P/L {_signed_usd(pos['realized_pl'])}.")
        else:
            pos["pending_action"] = None
            pos["pending_close_price"] = None
            pos["pending_close_date"] = None
            pos["updated_at"] = datetime.now().isoformat()
            _save([pos])
            return f"CANCELLED: staged close on {position_id} was discarded, position stays open."

    return f"[NOT CONFIRMED] Personal position {position_id} has an unrecognized pending_action '{action}'."
