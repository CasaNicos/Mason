"""
mason_options.py — Options play journal, accuracy ledger, and review engine

Every play Mason recommends is logged here permanently.
Outcomes are recorded at expiration, then reviewed at 30 and 60 days.
The accuracy ledger feeds back into Mason's confidence calibration over time.
"""

import json
import re
import uuid
from datetime import datetime, date
from pathlib import Path

import mason_db as DB


def _usd(v, signed=False) -> str:
    """Money for text Mason reads/speaks: sign BEFORE the $, cents always.
    signed=True forces a leading + on positives (for a P&L stat); default omits
    it (natural inside a computed sentence). Non-numeric falls through to str."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if n < 0 else ("+" if signed else "")
    return f"{sign}${abs(n):,.2f}"


ACCURACY_FILE = Path(__file__).parent / "mason_accuracy.json"
REFUSALS_FILE = Path(__file__).parent / "options_journal_refusals.json"

# Historical constants. As of the 2026-09 journal migration the play journal
# lives in mason.db (options_trades table); these two paths are no longer the
# source of truth and are kept only so any external reference still resolves.
JOURNAL_FILE  = Path(__file__).parent / "options_journal.json"
DELETED_FILE  = Path(__file__).parent / "options_journal_deleted.json"


class JournalIntegrityError(ValueError):
    """Raised when a write would put the journal into a state that contradicts
    itself — e.g. marking a row execution_verified=True while its ticker/strike/
    expiration/right disagree with its own contract_symbol. A ValueError subclass
    so an unaware caller still fails loudly instead of silently writing garbage."""


# ── I/O helpers ───────────────────────────────────────────────────

def _load_journal() -> list:
    """Every active play, oldest first. Reads the active_options_trades VIEW
    (is_deleted = 0), so a soft-deleted row is structurally incapable of coming
    back out. `strikes` and `hold_directive` are returned as parsed dicts, same
    as the JSON version always did. This is the ONE read path every other
    function in the module funnels through."""
    conn = DB.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM active_options_trades ORDER BY created_at, id"
        ).fetchall()
        return [DB.row_to_dict(r, "options_trades") for r in rows]
    finally:
        conn.close()


def _save_journal(plays: list):
    """Upsert every row in the list (insert new, update existing) in a single
    transaction — the durability guarantee _write_play_atomic() used to hand-roll
    by writing the whole file carefully. A row absent from the list is left
    untouched, which is how a soft-deleted play stays gone."""
    conn = DB.get_connection()
    try:
        DB.upsert_rows(conn, "options_trades", plays)
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


def _append_deleted_log(entry: dict):
    """Audit trail for deleted plays — nothing removed via delete_play() truly
    disappears, it just leaves the live journal and lands here instead."""
    log = []
    if DELETED_FILE.exists():
        with open(DELETED_FILE, encoding="utf-8") as f:
            log = json.load(f)
    log.append(entry)
    with open(DELETED_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, default=str)


def _append_refusal_log(entry: dict):
    """Audit trail for REFUSED writes — same shape and spirit as
    _append_deleted_log(). A refusal is a real event (Mason tried to close a
    play that is still live, or to backfill a row contradicting its own
    contract symbol) and needs to be as reviewable afterwards as a deletion is.
    Never raises: a failure to write the audit file must not turn a correct
    refusal into a crash."""
    try:
        log = []
        if REFUSALS_FILE.exists():
            with open(REFUSALS_FILE, encoding="utf-8") as f:
                log = json.load(f)
        log.append(entry)
        with open(REFUSALS_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, default=str)
    except Exception:
        pass


# ── OCC contract-symbol parsing (ground truth for a verified row) ──
# Added 2026-07-28 (journal-integrity fix 2). An OCC option symbol is fully
# self-describing: AMD260821C00500000 == AMD, expiring 2026-08-21, Call,
# $500.00 strike. When a row carries execution_verified=True its contract_symbol
# came from a real Alpaca order, which makes it the authoritative statement of
# what was actually traded — every other field on that row (ticker, expiration,
# strikes, strategy) is model-supplied prose and must agree with it.
#
# Real incident this exists to stop: row 94bbd180 carried
# contract_symbol AMD260821C00550000 with strikes {"long_call": 500} and a
# thesis naming the $500 strike, all flagged execution_verified:true. Nothing
# in the write path compared the two, so a $550 leg's closing fill was recorded
# as a $500 long call's entry.

_OCC_TAIL_RE  = re.compile(r"^(\d{6})([CP])(\d{8})$")
_OCC_ROOT_RE  = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")

# An Alpaca order id is a UUID. Finding one in a play's prose on a row that is
# not execution_verified means the caller HAS real fill data and is writing it
# where no code will read it — the 1b379e9b / 02ccfe82 / 722a6b4c shape.
# Deliberately anchored on word boundaries so a UUID inside a longer client
# order id ("mason-SPY...-3830d085b4de") does not false-positive.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE)


def _money(v) -> str:
    """Sign INSIDE the dollar sign: -$999.00, not $-999.00. Refusal messages get
    read under pressure and grepped in tests; the conventional form is the one
    people actually search for."""
    if v is None:
        return "$0.00"
    v = float(v)
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def parse_occ_symbol(symbol: str) -> dict:
    """Parse an OCC option symbol into its four authoritative facts.

    The tail of an OCC symbol is a fixed 15 characters — 6 date digits, one
    C/P, 8 strike digits — so the underlying/expiry split is unambiguous no
    matter how long the root is or whether it contains digits. Strike digits
    are thousandths of a dollar, so sub-$1 strikes (00000500 -> $0.50) parse
    correctly.

    Returns {"ok": True, ticker, expiration (YYYY-MM-DD), right ("C"|"P"),
    option_type ("call"|"put"), strike (float), symbol} or
    {"ok": False, "error": str}. Pure — never touches disk or the network.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return {"ok": False, "error": "empty contract symbol"}
    if len(s) < 16:
        return {"ok": False, "error": f"'{s}' is too short to be an OCC option symbol"}

    root, tail = s[:-15], s[-15:]
    m = _OCC_TAIL_RE.match(tail)
    if not m:
        return {"ok": False, "error": f"'{s}' does not end in a valid OCC "
                                      f"YYMMDD+C/P+8-digit-strike tail"}
    if not _OCC_ROOT_RE.match(root):
        return {"ok": False, "error": f"'{s}' has an invalid underlying root '{root}'"}

    yymmdd, right, strike_digits = m.group(1), m.group(2), m.group(3)
    try:
        exp = date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return {"ok": False, "error": f"'{s}' has an invalid expiration date '{yymmdd}'"}

    return {
        "ok": True,
        "symbol": s,
        "ticker": root,
        "expiration": exp.isoformat(),
        "right": right,
        "option_type": "call" if right == "C" else "put",
        "strike": int(strike_digits) / 1000.0,
        "error": None,
    }


def _strike_values(strikes) -> list:
    """Numeric strike values out of a play's `strikes` field, which is normally
    a dict like {"long_call": 500} but is tolerated as a bare number too."""
    if isinstance(strikes, (int, float)) and not isinstance(strikes, bool):
        return [float(strikes)]
    if isinstance(strikes, dict):
        out = []
        for v in strikes.values():
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out
    return []


def _expected_right(strategy, strikes) -> str | None:
    """Best-effort call/put from the model-supplied strategy name and strike
    keys ('long_call' -> C, 'long_put' -> P). Returns None when it genuinely
    can't tell, in which case the right is simply not checked — this guard
    catches contradictions, it does not invent them."""
    keys = " ".join(str(k) for k in strikes.keys()) if isinstance(strikes, dict) else ""
    text = f"{strategy or ''} {keys}".lower()
    has_put  = "put"  in text
    has_call = "call" in text
    if has_put and not has_call:
        return "P"
    if has_call and not has_put:
        return "C"
    return None


def check_contract_consistency(play_data: dict, contract_symbol: str) -> dict:
    """Compare model-supplied play fields against the truth encoded in the real
    contract symbol. Pure — no disk, no network, no writes.

    Returns {"ok": bool, "parsed": dict|None, "mismatches": [ {field, supplied,
    from_contract} ], "message": str}. Each mismatch type (ticker, expiration,
    strike, right) is reported independently so a refusal names every problem
    at once instead of one per round trip.
    """
    parsed = parse_occ_symbol(contract_symbol)
    if not parsed.get("ok"):
        return {
            "ok": False,
            "parsed": None,
            "mismatches": [{"field": "contract_symbol",
                            "supplied": contract_symbol,
                            "from_contract": None}],
            "message": (f"Cannot verify this play: {parsed.get('error')}. A row can only be "
                        f"marked execution_verified against a parseable OCC contract symbol."),
        }

    mismatches = []

    supplied_ticker = str(play_data.get("ticker") or "").strip().upper()
    if supplied_ticker != parsed["ticker"]:
        mismatches.append({"field": "ticker",
                           "supplied": supplied_ticker or "(missing)",
                           "from_contract": parsed["ticker"]})

    supplied_exp = str(play_data.get("expiration") or "").strip()[:10]
    if supplied_exp != parsed["expiration"]:
        mismatches.append({"field": "expiration",
                           "supplied": supplied_exp or "(missing)",
                           "from_contract": parsed["expiration"]})

    strikes = play_data.get("strikes")
    values  = _strike_values(strikes)
    if not values:
        mismatches.append({"field": "strike",
                           "supplied": "(missing)",
                           "from_contract": parsed["strike"]})
    elif len(values) > 1:
        # Standing policy is single-leg long-only (docs/active/STRATEGY_GUIDE.md).
        # A multi-strike row cannot be verified against one contract symbol —
        # that ambiguity is exactly how the two-leg AMD position was journaled
        # as unlinked rows in the first place.
        mismatches.append({"field": "strike",
                           "supplied": f"{len(values)} strikes {sorted(values)} "
                                       f"(multi-leg — one contract symbol cannot verify it)",
                           "from_contract": parsed["strike"]})
    elif abs(values[0] - parsed["strike"]) > 0.005:
        mismatches.append({"field": "strike",
                           "supplied": values[0],
                           "from_contract": parsed["strike"]})

    exp_right = _expected_right(play_data.get("strategy"), strikes)
    if exp_right and exp_right != parsed["right"]:
        mismatches.append({"field": "right",
                           "supplied": f"{play_data.get('strategy')} -> "
                                       f"{'call' if exp_right == 'C' else 'put'}",
                           "from_contract": parsed["option_type"]})

    if not mismatches:
        return {"ok": True, "parsed": parsed, "mismatches": [],
                "message": f"Consistent with {parsed['symbol']}."}

    lines = [f"  {m['field']}: you said {m['supplied']!r} | contract "
             f"{parsed['symbol']} says {m['from_contract']!r}" for m in mismatches]
    return {
        "ok": False,
        "parsed": parsed,
        "mismatches": mismatches,
        "message": (
            f"REFUSED — the fields you supplied contradict the real contract "
            f"{parsed['symbol']} that Alpaca confirmed:\n" + "\n".join(lines) +
            f"\nThe contract symbol is the ground truth; your input is wrong. "
            f"Nothing was written. Re-read the real order and call again with "
            f"fields that match the contract, or backfill the contract you "
            f"actually meant."
        ),
    }


# ── Core journal operations ───────────────────────────────────────

def _default_play_row(entry_date: str) -> dict:
    """The canonical shape of a journal row, with every key present and every
    unknown explicitly None. Pure — generates an id and a timestamp, touches
    no disk.

    Extracted from log_play() 2026-07-30 for one reason only: _write_play_atomic()
    needs the identical skeleton, and a second hand-maintained copy of a
    40-key schema is a drift bug waiting to happen. log_play()'s behaviour is
    unchanged — it overlays play_data onto this exactly as it always did, and
    its execution_verified guard below is untouched.
    """
    return {
        # Identity
        "id": str(uuid.uuid4())[:8],
        "created_at": datetime.now().isoformat(),
        # Who logged it — defaults to 'mason' (the tool). log_play() overlays
        # play_data on top of this, so a caller can hand in created_by='nico'.
        "created_by": "mason",
        "entry_date": entry_date,
        # Status
        "status": "open",      # open | closed_early | expired | reviewed
        # Cost basis — the three numbers every P&L calculation needs. Declared
        # here (2026-07-30, orphan-backfill fix C1) so the KEYS ALWAYS EXIST
        # even when a caller forgets to supply them. Before this they were
        # simply ABSENT on any row the backfill path wrote — not null, absent —
        # and calculate_pnl()'s float(play.get("premium", 0)) silently read that
        # as "the option cost nothing". SPY row 5afd580d, a long put that made
        # +$509, computed as expired-worthless at exactly $0.00 because of it.
        #
        # premium is per-share debit/credit (i.e. the fill price, NOT x100).
        # underlying_price_at_entry stays None when unknown and is NEVER
        # defaulted to the strike — strike-as-underlying is precisely what made
        # that put price out to zero intrinsic.
        "premium": None,
        "contracts": None,
        "underlying_price_at_entry": None,
        # Outcome fields — filled in at review time
        "outcome": None,       # win | loss | breakeven
        "actual_pnl": None,    # dollars (positive = profit)
        "underlying_price_at_expiry": None,
        # Review fields
        "expiry_review": None,
        "expiry_review_date": None,
        "review_30d": None,
        "review_30d_date": None,
        "review_60d": None,
        "review_60d_date": None,
        # Thesis quality (separate from P&L — good reasoning can still lose)
        "thesis_score": None,       # 0-100
        "thesis_score_notes": None,
        # Standing instruction from Nico to HOLD this play — the machine-readable
        # form of "don't close this before X". Added 2026-07-28 (journal-integrity
        # fix 3): row 83ec044f had "EXIT MORNING OF AUG 5TH BEFORE IV CRUSH" in its
        # own notes field and a thesis naming Aug 4th earnings as the whole profit
        # mechanism, and was closed July 28 anyway — because nothing in the close
        # path ever read prose. None (the default) means no directive and changes
        # nothing. Shape: {"hold_until": "YYYY-MM-DD", "reason": str,
        # "set_at": iso, "set_by": "nico"}.
        "hold_directive": None,
        # Execution linkage — set ONLY when this entry is tied to a real, confirmed
        # Alpaca fill. False/None here means this is a recommendation/idea that was
        # never (yet) verified as an actual placed-and-filled order. Never set
        # execution_verified=True by hand — only attach_execution() should do that,
        # and only after mason_execution.verify_fill() confirms a real fill.
        "execution_verified": False,
        "order_id": None,
        "client_order_id": None,
        "contract_symbol": None,
        "filled_qty": None,
        "filled_avg_price": None,
        "filled_at": None,
        "execution_note": "idea logged only — not tied to a placed Alpaca order",
        # Exit-execution linkage — the closing fill, mirroring the five entry
        # fields above. Added 2026-07-30 (orphan-backfill fix C2). Before this
        # there was nowhere to put a closing fill at all, which is why a SELL
        # order could only be recorded by inventing a whole new OPEN play for
        # it (MSFT row 276b1ea0: a $50.00 sell written as a $50.00 entry).
        # Only log_verified_closed_play() populates these, and only from a real
        # verify_and_backfill_order() lookup.
        "exit_order_id": None,
        "exit_client_order_id": None,
        "exit_filled_qty": None,
        "exit_filled_avg_price": None,
        "exit_filled_at": None,
    }


def log_play(play_data: dict, today: date = None) -> dict:
    """
    Log a new options play recommendation.
    play_data should include: ticker, strategy, direction, expiration,
    strikes (dict), premium, premium_type, contracts, underlying_price_at_entry,
    iv_at_entry, thesis, mason_confidence, max_profit, max_loss, breakeven.
    Returns the saved play dict with generated id.

    HARD INVARIANT (added 2026-07-30, log_play guard pass): this function was
    completely UNGUARDED. Its whole dispatch was two lines — `log_play(inp)` and
    a success string. That made it the unlocked back door beside every hardened
    one, and Mason walked through it the first time the front door refused him.

    Real incident: backfill_verified_option_play correctly refused the expired
    XLE260717C00055000 contract. Mason then called log_options_play by hand and
    manufactured row 722a6b4c — status "open" on a contract that had expired 13
    days earlier, contract_symbol null, order_id null, no P&L, and the real
    Alpaca order id typed into `notes` as free text. Unresolvable by design:
    close_play_early refuses it (no contract symbol to check with the broker),
    no review can compute a P&L, and it sits in the open book forever polluting
    the accuracy scorecard.

    That is Bug 8 running in reverse. Bug 8 was "Mason misuses log_options_play,
    so build a backfill tool." This is "backfill refuses, so fall back to
    log_options_play." Hardening one door and leaving the other open just moves
    the traffic.

    Two guards, both scoped as narrowly as they can be:

      1. EXPIRED + OPEN. A row entering the book as status "open" on a contract
         whose expiration has already passed. There is no position to open. This
         does NOT block a CLOSED historical row — closed history is legitimate,
         and log_verified_closed_play (which builds its row from
         _default_play_row, not from here) is deliberately untouched so the
         two-order escape hatch keeps working on expired contracts.

      2. REAL FILL DATA IN FREE TEXT on an unverified row. An Alpaca order id is
         a UUID; finding one in `notes`/`thesis` on a row that is not
         execution_verified means the caller HAS real fill data and is writing
         it somewhere no code will ever read. That is exactly the shape of the
         two rows Bug 8 was created to eliminate (XLE 1b379e9b, AMD 02ccfe82 —
         real order_id and fill data in prose, both stuck
         execution_verified:false forever).

    Raises JournalIntegrityError and writes NOTHING. There is no force= bypass:
    the correct response to "I have real fill data" is backfill_verified_option_play,
    and the correct response to "I can't verify it" is to tell Nico, not to
    manufacture a row.

    today: injectable clock for guard 1.
    """
    _today = today or date.today()

    # ── Guard 1: an expired contract cannot be OPENED ─────────────────────
    _status = str(play_data.get("status") or "open").strip().lower()
    _exp_raw = play_data.get("expiration")
    _sym = (play_data.get("contract_symbol") or "").strip().upper()
    _exp = None
    _parsed = parse_occ_symbol(_sym) if _sym else {"ok": False}
    if _parsed.get("ok"):
        _exp = _parsed["expiration"]
    elif _exp_raw:
        _exp = str(_exp_raw)[:10]
    if _status == "open" and _exp:
        try:
            _exp_date = date.fromisoformat(_exp)
        except (ValueError, TypeError):
            _exp_date = None
        if _exp_date and _exp_date < _today:
            _append_refusal_log({
                "refused_at": datetime.now().isoformat(),
                "action": "log_play",
                "reason": "expired contract cannot be logged as open",
                "expiration": _exp,
                "today": _today.isoformat(),
                "play_data": play_data,
            })
            raise JournalIntegrityError(
                f"[NOT LOGGED] {play_data.get('ticker')} "
                f"{play_data.get('strategy')} expiring {_exp} expired "
                f"{(_today - _exp_date).days} day(s) ago. It cannot be logged as an "
                f"OPEN play today ({_today.isoformat()}) — there is no position to "
                f"open.\n"
                f"Real incident (2026-07-30): row 722a6b4c, an XLE $55 call that had "
                f"expired 13 days earlier, logged this way after "
                f"backfill_verified_option_play correctly refused it. It had no "
                f"contract_symbol, no order_id and no P&L, so nothing could ever "
                f"close or resolve it. It sat in the open book as permanent filler.\n"
                f"If this is real history you want on the record, it needs its real "
                f"data: use backfill_verified_option_play's two-order form with the "
                f"buy AND sell order ids. If you don't have both, you don't have the "
                f"history — say so to Nico rather than writing a placeholder. "
                f"Nothing was written."
            )

    # ── Guard 2: real fill data smuggled into prose ───────────────────────
    if not play_data.get("execution_verified"):
        _prose = " ".join(str(play_data.get(k) or "")
                          for k in ("notes", "thesis", "execution_note"))
        _uuid = _UUID_RE.search(_prose)
        if _uuid:
            _append_refusal_log({
                "refused_at": datetime.now().isoformat(),
                "action": "log_play",
                "reason": "order id written into free text on an unverified row",
                "found": _uuid.group(0),
                "play_data": play_data,
            })
            raise JournalIntegrityError(
                f"[NOT LOGGED] The text on this play contains what looks like a real "
                f"Alpaca order id ({_uuid.group(0)}), but the row is not "
                f"execution_verified.\n"
                f"Writing a real order id into `notes` or `thesis` puts it somewhere "
                f"NO code will ever read — not reconcile, not the P&L math, not the "
                f"learning loop. It reads as a real record and is not one. That is "
                f"exactly the shape of rows XLE 1b379e9b and AMD 02ccfe82, which is "
                f"why backfill_verified_option_play was built in the first place.\n"
                f"If the order is real, attach it properly with "
                f"backfill_verified_option_play — it re-pulls the order from Alpaca "
                f"itself and will tell you precisely why it won't take it. If that "
                f"tool refuses, the answer is to tell Nico what it said, not to write "
                f"the order id down somewhere softer. Nothing was written."
            )

    plays = _load_journal()
    entry_date = play_data.get("entry_date") or date.today().isoformat()
    play = _default_play_row(entry_date)
    play.update(play_data)   # overlay caller-supplied fields
    # Guard: log_play() itself never grants verified status. Only attach_execution()
    # (called from mason_execution after a real confirmed fill) may set this True.
    # This stops a caller from claiming execution just by passing the flag in.
    play["execution_verified"] = False
    plays.append(play)
    _save_journal(plays)
    _recompute_accuracy(plays)
    return play


def attach_execution(play_id: str, order_id: str, client_order_id: str,
                      contract_symbol: str, filled_qty: float,
                      filled_avg_price: float, filled_at: str = None) -> dict | None:
    """
    Link a journal entry to a CONFIRMED Alpaca fill. This is the only function
    allowed to set execution_verified=True — call it only after
    mason_execution.verify_fill() has returned a filled/partially_filled status.
    """
    fields = {}
    try:                       # an auto-placed play is logged before its fill: take cost + size from the fill
        cur = next((x for x in _load_journal() if x.get("id") == play_id), None)
        if cur is not None and filled_avg_price is not None:
            if cur.get("premium") is None:
                fields["premium"] = float(filled_avg_price)
            if cur.get("contracts") is None and filled_qty:
                fields["contracts"] = int(float(filled_qty))
    except Exception:
        fields = {}
    return update_play(play_id, {
        **fields,
        "execution_verified": True,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "contract_symbol": contract_symbol,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "filled_at": filled_at or datetime.now().isoformat(),
        "execution_note": "verified — matches a confirmed Alpaca fill",
    })


_CLOSING_INTENTS = ("sell_to_close", "buy_to_close")


def normalize_side(side, position_intent=None) -> dict:
    """Reduce a raw broker side/intent to {"closing": bool, "opening": bool,
    "raw": str, "source": str}. Pure.

    Alpaca is not consistent about this field across endpoints and versions:
    it can be "sell", "SELL", "sell_to_close", or "sell_short". So this
    LOWERCASES and matches on the `sell` PREFIX rather than exact-comparing a
    raw broker string — an exact compare against "sell" is exactly the kind of
    brittle check that silently stops firing after a broker-side rename.

    position_intent, when Alpaca supplies it, is preferred: it is the more
    precise signal (it distinguishes sell_to_close from sell_to_open, which a
    bare side cannot). Under Nico's single-leg long-only policy the two agree,
    but the intent is still the better authority when present.

    Unknown/blank input returns closing=False, opening=False — "I don't know"
    is not "it's an entry", and callers must decide for themselves whether an
    unknown side is safe to proceed on.
    """
    intent = str(position_intent or "").strip().lower()
    if intent:
        if intent in _CLOSING_INTENTS:
            return {"closing": True, "opening": False, "raw": intent,
                    "source": "position_intent"}
        if intent.endswith("_to_open"):
            return {"closing": False, "opening": True, "raw": intent,
                    "source": "position_intent"}

    s = str(side or "").strip().lower()
    if not s:
        return {"closing": False, "opening": False, "raw": "", "source": "unknown"}
    if s.startswith("sell"):
        return {"closing": True, "opening": False, "raw": s, "source": "side"}
    if s.startswith("buy"):
        return {"closing": False, "opening": True, "raw": s, "source": "side"}
    return {"closing": False, "opening": False, "raw": s, "source": "unrecognised"}


def find_open_play_by_contract(contract_symbol: str) -> dict | None:
    """The single OPEN play on this exact contract symbol, or None.

    Keys on contract_symbol, never on ticker — matching on ticker is what
    closed the wrong AMD leg (see close_play_early's docstring)."""
    want = (contract_symbol or "").strip().upper()
    if not want:
        return None
    for p in _load_journal():
        if p.get("status") == "open" and \
           (p.get("contract_symbol") or "").strip().upper() == want:
            return p
    return None


def log_verified_play(play_data: dict, order_id: str, client_order_id: str,
                       contract_symbol: str, filled_qty: float,
                       filled_avg_price: float, side: str,
                       filled_at: str = None, position_intent: str = None,
                       position_lookup=None, today: date = None) -> dict:
    """
    Log a play that is ALREADY tied to a real, confirmed Alpaca fill — the
    backfill path for an order that was placed outside place_option_play()
    (or whose journal write failed at the time) and needs to enter the
    journal properly verified, not as a log_options_play "idea" entry.

    Added 2026-07-28 (Bug 8): reconcile()'s _sync_option_fills_to_journal()
    has always detected these as "orphan fills" but only flagged them — its
    own comment said to backfill "using log_options_play with the real fill
    data attached," which is bad advice: log_play() hard-guards
    execution_verified back to False (see the guard comment above), so no
    amount of real detail in the notes/thesis text ever made an orphan-fill
    backfill actually verified. Real incident: XLE play 1b379e9b and AMD
    play 02ccfe82 both had real order_id/fill data typed into notes/thesis,
    both still read execution_verified:false, order_id:null.

    This is exactly place_option_play()'s own log_play()+attach_execution()
    pairing (mason_execution.py:671-677), factored out so a backfill can use
    the identical, already-proven path instead of a separate one-off.

    Callers MUST source order_id/contract_symbol/filled_qty/filled_avg_price
    from a real Alpaca order lookup (mason_execution.get_order /
    verify_and_backfill_order) — never from freeform text a model wrote.
    This function itself does not call Alpaca; it trusts its caller the same
    way attach_execution() always has.

    HARD INVARIANT (added 2026-07-28, journal-integrity fix 2): the row's
    model-supplied ticker/expiration/strike/right must agree with the real
    contract_symbol before anything is written. Trusting the caller for the
    FILL is fine — it came from Alpaca. Trusting the caller for the DESCRIPTION
    is what produced row 94bbd180 (contract says $550, strikes say $500,
    execution_verified:true). The check lives here, not only in the tool
    dispatch, so the invariant holds no matter which caller reaches it.
    Raises JournalIntegrityError on contradiction and writes NOTHING — the
    check runs before log_play() so a refusal can't leave a half-written row.

    SECOND HARD INVARIANT (added 2026-07-30, orphan-backfill fix B): `side` is
    REQUIRED, and a CLOSING side is refused outright.

    Why a closing fill can never open a play: Nico's standing policy is
    single-leg LONG ONLY — see docs/active/STRATEGY_GUIDE.md and ROADMAP §0,
    enforced in code at mason_execution.build_order. Under that policy an
    ENTRY is always a buy, so a sell fill is by definition an EXIT of
    something that already exists. THIS GUARD IS DOWNSTREAM OF THAT POLICY,
    NOT A LAW OF OPTIONS: if the policy is ever widened to short premium
    (sell-to-open), this refusal must be revisited at the same time, because a
    sell would then be a legitimate entry. Do not delete it quietly — change
    it deliberately, and update this docstring with it.

    Real incident: on 2026-07-30 Mason backfilled the MSFT $50.00 SELL order
    (59d5b93b) as a brand-new OPEN play, 276b1ea0, carrying the exit price as
    the entry price. mason_execution.verify_and_backfill_order() had returned
    `side` all along (mason_execution.py:148) and nothing in the repo read it.

    The guard lives HERE, on the write path, and not in the tool dispatch.
    That is the Bug 8 lesson: guarding tool SELECTION fails. A separate
    backfill_verified_option_play tool was added so Mason would stop misusing
    log_options_play, and he then misused backfill. A write-path guard is
    inherited by every caller — the dispatch, _sync_option_fills_to_journal,
    and anything written next year. There is deliberately NO force= bypass.

    Also refuses a symbol-level duplicate: an OPEN play already existing on
    this contract_symbol. That is a second, independent net under the same
    incident, and unlike an order-id check it still catches a repeat backfill
    of the same position when the order ids differ.

    THIRD HARD INVARIANT (added 2026-07-30, guard-hardening pass): the two
    guards above were not enough, and the 16:35 session proved it by writing
    four more bad rows within twenty minutes of them shipping. What they never
    asked is the only question that actually matters on this path: *is this a
    live position right now?* They only ever asked "was this a real fill?" —
    and every dead, expired, long-closed fill in Alpaca's order history is
    still a real fill. Three guards are added here, in cheapest-first order:

      3. ORDER-ID DUPLICATE. find_play_by_order_id() has existed since Bug 8
         and was wired only into log_verified_closed_play, never into this
         function. Real incident: order a8b9e97c was already recorded as play
         5c29d5a8 (closed_early); backfilling it again wrote 9f41458a, an
         identical twin, and the book then claimed +$10,000 on a +$5,000 MSFT
         trade. Guard 2 could not catch it because it only scans OPEN rows and
         5c29d5a8 was closed. One order id, one row, in any status, forever.

      4. EXPIRED CONTRACT. The expiry is read from the OCC contract_symbol via
         parse_occ_symbol — ground truth — and NOT from play_data["expiration"],
         which is model-typed. A contract that expired before today cannot be
         entered as an OPEN position. Real incident: SPY260722C00752000 and
         XLE260717C00055000 were written status:"open" on 2026-07-30, 8 and 13
         days after they ceased to exist.

      5. BROKER POSITION. Alpaca is asked whether it actually holds this
         contract, and the row is refused unless it does. This is the guard
         that would have stopped all three orphan rows on its own, and it is
         the one that closes the whole class rather than three instances of it.
         It FAILS CLOSED on an unreachable or erroring broker, matching
         close_play_early's choice for the same reason: a broker we cannot
         reach is not evidence that a position exists. There is deliberately no
         force= bypass on any of the three.

    position_lookup: injectable broker dependency (symbol -> dict, the shape
    mason_execution.get_position returns), mirroring close_play_early. Defaults
    to the real Alpaca lookup. Pass a stub in tests; no test may hit the wire.
    today: injectable clock for the expiry guard.
    """
    consistency = check_contract_consistency(play_data, contract_symbol)
    if not consistency["ok"]:
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "log_verified_play",
            "contract_symbol": contract_symbol,
            "order_id": order_id,
            "reason": "fields contradict contract_symbol",
            "mismatches": consistency["mismatches"],
            "play_data": play_data,
        })
        raise JournalIntegrityError(consistency["message"])

    sym      = (contract_symbol or "").strip().upper()
    existing = find_open_play_by_contract(sym)

    # ── Guard 1: a CLOSING fill never opens a play ─────────────────────────
    norm = normalize_side(side, position_intent)
    if norm["closing"]:
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "log_verified_play",
            "contract_symbol": sym,
            "order_id": order_id,
            "reason": "closing side cannot open a play",
            "side": norm["raw"],
            "side_source": norm["source"],
            "existing_open_play_id": (existing or {}).get("id"),
            "play_data": play_data,
        })
        if existing:
            raise JournalIntegrityError(
                f"[NOT BACKFILLED] Order {order_id} on {sym} is a {norm['raw'].upper()} "
                f"— a CLOSING fill. It cannot open a new play.\n"
                f"Play {existing['id']} ({existing.get('ticker')} "
                f"{existing.get('strategy')}) is already OPEN on this exact contract, "
                f"entered at ${existing.get('filled_avg_price')} x "
                f"{existing.get('filled_qty')}. This order is that play's EXIT.\n"
                f"Writing it as a new entry is what produced the phantom MSFT row "
                f"276b1ea0 on 2026-07-30, double-counting a single position and "
                f"recording a $50.00 exit as a $50.00 cost basis.\n"
                f"Do this instead — record the exit against the row that already "
                f"exists, never against a new one:\n"
                f"  1. If play {existing['id']} is already journaled from its own entry "
                f"order ({existing.get('order_id')}), close THAT row: "
                f"close_options_play_early on {existing['id']}. It is broker-gated, so "
                f"it will confirm with Alpaca that the contract is actually flat first.\n"
                f"  2. If neither end of this position is journaled yet, use the "
                f"two-order form of backfill_verified_option_play: pass the BUY as "
                f"order_id and this sell as exit_order_id='{order_id}'. That writes one "
                f"closed row carrying both fills, atomically.\n"
                f"Nothing was written. Mason's standing policy is single-leg LONG ONLY "
                f"(docs/active/STRATEGY_GUIDE.md, ROADMAP §0), so an entry is always a "
                f"buy — that is why this is refused."
            )
        raise JournalIntegrityError(
            f"[NOT BACKFILLED] Order {order_id} on {sym} is a {norm['raw'].upper()} "
            f"— a CLOSING fill — and NO open play exists on that contract.\n"
            f"Refusing to write it as an entry: the fill price "
            f"${filled_avg_price} is an EXIT price, and there is no entry price "
            f"anywhere in this order. Inventing one is not an option.\n"
            f"Find the OPENING (buy) order for {sym} first — get_paper_orders or "
            f"reconcile_alpaca's orphan_fills will have it — then backfill THAT "
            f"order_id. If the position was opened and closed and you have both "
            f"order ids, use the two-order form: pass the buy as order_id and this "
            f"sell as exit_order_id, and the closed play is written atomically in "
            f"one row.\n"
            f"Nothing was written. Mason's standing policy is single-leg LONG ONLY "
            f"(docs/active/STRATEGY_GUIDE.md, ROADMAP §0), so an entry is always a "
            f"buy — that is why this is refused."
        )

    # ── Guard 2: symbol-level duplicate ────────────────────────────────────
    # Independent of guard 1 and of any order-id check. Catches a second
    # backfill of the same live position even when the order ids differ.
    if existing:
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "log_verified_play",
            "contract_symbol": sym,
            "order_id": order_id,
            "reason": "an open play already exists on this contract_symbol",
            "side": norm["raw"],
            "existing_open_play_id": existing.get("id"),
            "play_data": play_data,
        })
        raise JournalIntegrityError(
            f"[NOT BACKFILLED] Play {existing['id']} ({existing.get('ticker')} "
            f"{existing.get('strategy')}) is already OPEN on contract {sym}, entered "
            f"at ${existing.get('filled_avg_price')} x {existing.get('filled_qty')} "
            f"on order {existing.get('order_id')}.\n"
            f"Refusing to write a second open row for the same contract — that is a "
            f"double-count, and the book would then show twice the risk that is "
            f"actually on. Nothing was written.\n"
            f"If order {order_id} is a genuinely separate ADD to this position, close "
            f"or correct {existing['id']} first so one row describes one position. If "
            f"it is the same fill arriving twice, there is nothing to do."
        )

    def _refuse(reason: str, message: str, **extra):
        """Refuse without writing. Every guard below exits through here so the
        refusal log has one shape and a refusal can never leave a half-row."""
        entry = {
            "refused_at": datetime.now().isoformat(),
            "action": "log_verified_play",
            "contract_symbol": sym,
            "order_id": order_id,
            "reason": reason,
            "side": norm["raw"],
            "play_data": play_data,
        }
        entry.update(extra)
        _append_refusal_log(entry)
        raise JournalIntegrityError(message)

    # ── Guard 3: this exact order is already in the journal ────────────────
    # ANY status. A closed row holding this order id means the position has
    # already been recorded start-to-finish; writing it again does not add
    # information, it doubles the P&L. Guard 2 cannot see this because it
    # only scans OPEN rows.
    dup = find_play_by_order_id(order_id)
    if dup is None and client_order_id:
        dup = find_play_by_order_id(client_order_id)
    if dup is not None:
        _pnl = dup.get("actual_pnl")
        _pnl_txt = f", booked P&L ${float(_pnl):+.2f}" if _pnl is not None else ""
        _refuse(
            "order_id already journaled",
            f"[NOT BACKFILLED] Order {order_id} is ALREADY recorded in the journal as "
            f"play {dup['id']} ({dup.get('ticker')} {dup.get('strategy')} "
            f"{dup.get('contract_symbol')}), status '{dup.get('status')}'"
            f"{_pnl_txt}.\n"
            f"Refusing to write a second row for one order. An order id identifies "
            f"exactly one fill and one fill belongs to exactly one play — in any "
            f"status, forever.\n"
            f"Real incident (2026-07-30): order a8b9e97c was already play 5c29d5a8, "
            f"closed at +$5,000. Backfilling it a second time created twin 9f41458a "
            f"and the book reported +$10,000 on a single $5,000 trade.\n"
            f"If play {dup['id']} is wrong, correct or delete THAT row — do not write "
            f"a competing one. Nothing was written.",
            existing_play_id=dup.get("id"),
            existing_status=dup.get("status"),
        )

    # ── Guard 4: the contract has already expired ─────────────────────────
    # Expiry comes from the OCC symbol, which is ground truth. play_data
    # ["expiration"] is model-typed and is exactly the field that cannot be
    # trusted here. consistency (above) has already proven the two agree, so
    # this is belt-and-braces, not a second opinion.
    _today  = today or date.today()
    _parsed = parse_occ_symbol(sym)
    if _parsed.get("ok"):
        _exp = date.fromisoformat(_parsed["expiration"])
        if _exp < _today:
            _days = (_today - _exp).days
            _refuse(
                "contract already expired",
                f"[NOT BACKFILLED] Contract {sym} expired {_parsed['expiration']} — "
                f"{_days} day(s) ago. It cannot be entered as an OPEN position today "
                f"({_today.isoformat()}); that position does not exist any more.\n"
                f"A filled BUY order in Alpaca's history is not the same thing as a "
                f"live position. Every expired contract Mason ever bought still has a "
                f"real filled buy order behind it.\n"
                f"Real incident (2026-07-30): SPY260722C00752000 and "
                f"XLE260717C00055000 were backfilled as open 8 and 13 days after they "
                f"stopped existing, from exactly this kind of stale orphan fill.\n"
                f"If this position was opened AND closed outside the journal, use the "
                f"two-order form: pass the buy as order_id and the sell as "
                f"exit_order_id. If it expired worthless there is no closing fill to "
                f"pass, and there is nothing to open either — report it to Nico rather "
                f"than manufacturing an open row. Nothing was written.",
                contract_expiration=_parsed["expiration"],
                today=_today.isoformat(),
            )

    # ── Guard 5: Alpaca must actually hold this position ──────────────────
    # The guard that closes the class. Guards 1-4 each catch one shape of
    # stale/duplicate fill; this one asks the only question that generalises.
    # FAILS CLOSED, deliberately — same reasoning as close_play_early: a
    # broker we cannot reach is not evidence that a position exists.
    _lookup = position_lookup or _default_position_lookup
    try:
        pos = _lookup(sym)
    except Exception as e:                        # transport, auth, timeout, anything
        _refuse(
            "broker_unreachable",
            f"[NOT BACKFILLED] Could not reach Alpaca to confirm a live position on "
            f"{sym}: {e}.\n"
            f"Refusing to write an OPEN row — a broker we can't reach is not evidence "
            f"that a position exists, and this path's entire job is to record "
            f"positions that are real right now. Nothing was written. Retry when the "
            f"broker is reachable.",
        )
    if not isinstance(pos, dict) or not pos.get("ok"):
        _refuse(
            "broker_unreachable",
            f"[NOT BACKFILLED] Alpaca returned an error checking {sym}: "
            f"{pos.get('error') if isinstance(pos, dict) else pos}.\n"
            f"Refusing to write an OPEN row on an unverified position. Nothing was "
            f"written.",
        )
    _live = float(pos.get("qty") or 0)
    if not pos.get("exists") or _live <= 0:
        _refuse(
            "no live position",
            f"[NOT BACKFILLED] Order {order_id} on {sym} really did fill "
            f"({filled_qty} @ ${filled_avg_price}), but Alpaca holds NO position on "
            f"that contract right now.\n"
            f"A real fill in order history is not a live position — it is history. "
            f"This one has already been closed, expired, or assigned. Writing it as "
            f"an OPEN play would put risk on the book that does not exist and would "
            f"start position alerts on a position nobody holds.\n"
            f"Real incident (2026-07-30): three long-dead orphan fills from June 24 "
            f"and July 22 were backfilled as open plays this way, because "
            f"reconcile_alpaca's orphan list told Mason to backfill every orphan "
            f"without ever asking whether it was still held.\n"
            f"If it was opened and closed outside the journal, use the two-order form "
            f"(order_id = the buy, exit_order_id = the sell). If it expired worthless "
            f"there is nothing to open — report it to Nico. If you believe it is "
            f"genuinely still open, Alpaca disagrees, and Alpaca is the source of "
            f"truth. Nothing was written.",
            broker_qty=_live,
        )
    if _live + 1e-9 < float(filled_qty or 0):
        _refuse(
            "live position smaller than the fill",
            f"[NOT BACKFILLED] Order {order_id} filled {filled_qty} contract(s) of "
            f"{sym}, but Alpaca currently holds only {_live:g}. The position has been "
            f"partially closed since that fill.\n"
            f"Refusing to write a row claiming {filled_qty} open contracts when "
            f"{_live:g} are actually held — the book would overstate live risk. "
            f"Nothing was written. Reconcile the real remaining size first.",
            broker_qty=_live,
        )

    # Thread the injectable clock through. log_play() gained its own expiry
    # guard on 2026-07-30; without passing `_today` it would fall back to the
    # real system date and this function's `today=` argument would be only half
    # wired — the guards above would honour it and the write below would not.
    play = log_play(play_data, today=_today)
    return attach_execution(play["id"], order_id=order_id,
                            client_order_id=client_order_id,
                            contract_symbol=contract_symbol,
                            filled_qty=filled_qty,
                            filled_avg_price=filled_avg_price,
                            filled_at=filled_at)


def _write_play_atomic(row: dict) -> dict:
    """PRIVATE. Append ONE fully-formed row — entry execution fields, the five
    exit execution fields, and the closure fields all already present — in a
    SINGLE _save_journal() and a SINGLE _recompute_accuracy().

    ── Why this exists, and why it is a bypass ──────────────────────────────
    The obvious implementation of a verified-closed-play write is
    log_play() then update_play(). That is two _save_journal() calls with a
    window between them in which the row is on disk as verified-but-still-OPEN
    at its entry price. That window is not theoretical: mason.html's
    fetchOptionsPanel() polls /api/options/journal roughly every 30 seconds,
    and get_open_plays() would return the half-written row. A reader can and
    eventually will observe it, and what they would observe is a brand-new
    open position that does not exist. That is the same false state the whole
    orphan-backfill incident was about, reintroduced by the fix for it.

    ── INVARIANT CHANGE — read this before adding a second caller ───────────
    Since 2026-07-28 the rule has been:

        "only attach_execution() may set execution_verified=True"

    As of 2026-07-30 the rule is:

        "only a function whose fill data came from a real Alpaca order lookup
         may set execution_verified=True"

    That is a widening, not a weakening. log_play()'s hard
    `play["execution_verified"] = False` guard is UNCHANGED and stays exactly
    where it is — a caller still cannot claim verification just by passing the
    flag in. What has been added is a second door beside it, gated just as
    tightly: this function is module-private, is called from exactly one place
    (log_verified_closed_play), and that caller accepts fills only from
    mason_execution.verify_and_backfill_order() output.

    Do not export this. Do not call it from the tool dispatch. Do not call it
    from anywhere else. If a new caller genuinely needs it, the honest move is
    to widen log_verified_closed_play's parameters, not to add a second
    entrance to the journal.
    """
    plays = _load_journal()
    plays.append(row)
    _save_journal(plays)
    _recompute_accuracy(plays)
    return row


def log_verified_closed_play(play_data: dict,
                             order_id: str, client_order_id: str,
                             contract_symbol: str, filled_qty: float,
                             filled_avg_price: float, side: str,
                             exit_order_id: str, exit_client_order_id: str,
                             exit_contract_symbol: str, exit_filled_qty: float,
                             exit_filled_avg_price: float, exit_side: str,
                             filled_at: str = None, exit_filled_at: str = None,
                             stated_pnl: float = None, thesis_score: int = None,
                             thesis_score_notes: str = None, notes: str = "",
                             position_intent: str = None,
                             exit_position_intent: str = None) -> dict:
    """Journal a position that was opened AND closed entirely outside the
    journal, as ONE closed row carrying BOTH real fills.

    A sibling of log_verified_play(), not a replacement for it. Use
    log_verified_play when the position is still open; use this when both ends
    already happened and neither was ever recorded.

    ── INVARIANT CHANGE (2026-07-30) ────────────────────────────────────────
    This is the second function permitted to set execution_verified=True. The
    invariant moves from "only attach_execution() may set it" to "only a
    function whose fill data came from a real Alpaca order lookup may set it."
    log_play()'s hard False guard is untouched. See _write_play_atomic().

    Callers MUST source BOTH sets of fill fields from
    mason_execution.verify_and_backfill_order() output — never from freeform
    text a model wrote. This function does not call Alpaca itself; it trusts
    its caller for the FILLS exactly the way attach_execution() always has,
    and trusts it for NOTHING else.

    ── actual_pnl is COMPUTED, never accepted ───────────────────────────────
        (exit_filled_avg_price - filled_avg_price) * filled_qty * 100
    If the caller passes stated_pnl, it is cross-checked against that
    computation and a disagreement beyond $1.00 is a refusal that prints both
    numbers side by side. Neither number is silently preferred — the caller
    and the broker disagreeing about money is a fact a human needs to see.

    ── Fails CLOSED on every one of these, writing NOTHING ──────────────────
      1. entry fields contradict the entry contract symbol
      2. entry side is not an opening (buy) fill
      3. exit_order_id == order_id
      4. exit contract symbol != entry contract_symbol
      5. exit side is not the closing counterpart of the entry
      6. exit_filled_at is earlier than filled_at
      7. exit_filled_qty != filled_qty
      8. either fill price is None
      9. thesis_score outside 0-100
     10. find_play_by_order_id(order_id) already returns a row
     11. find_play_by_order_id(exit_order_id) already returns a row

    Returns the written row. Raises JournalIntegrityError on any refusal.
    """
    def _refuse(reason: str, message: str):
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "log_verified_closed_play",
            "contract_symbol": contract_symbol,
            "order_id": order_id,
            "exit_order_id": exit_order_id,
            "reason": reason,
            "play_data": play_data,
        })
        raise JournalIntegrityError(message)

    entry_sym = (contract_symbol or "").strip().upper()
    exit_sym  = (exit_contract_symbol or "").strip().upper()

    # 1. entry fields vs the real entry contract
    consistency = check_contract_consistency(play_data, entry_sym)
    if not consistency["ok"]:
        _refuse("fields contradict contract_symbol",
                f"[NOT WRITTEN] {consistency['message']}")

    # 2. the entry must be an opening fill
    entry_norm = normalize_side(side, position_intent)
    if not entry_norm["opening"]:
        _refuse("entry side is not an opening fill",
                f"[NOT WRITTEN] The ENTRY order {order_id} has side "
                f"{entry_norm['raw']!r}, which is not a buy. Under Nico's standing "
                f"single-leg LONG ONLY policy (docs/active/STRATEGY_GUIDE.md, "
                f"ROADMAP §0) an entry is always a buy, so this pair is the wrong "
                f"way round or one of the two order ids is wrong. Nothing was "
                f"written. If that policy ever widens to short premium, this check "
                f"must be revisited with it.")

    # 3. the same order cannot be both ends of a trade
    if exit_order_id and order_id and str(exit_order_id) == str(order_id):
        _refuse("exit_order_id equals order_id",
                f"[NOT WRITTEN] exit_order_id and order_id are the same order "
                f"({order_id}). One order cannot be both the entry and the exit of a "
                f"position. Nothing was written — re-read the two real orders and "
                f"pass the BUY as order_id and the SELL as exit_order_id.")

    # 4. both fills must be the same contract
    if exit_sym != entry_sym:
        _refuse("exit contract symbol differs from entry",
                f"[NOT WRITTEN] The exit order {exit_order_id} is on contract "
                f"{exit_sym or '(missing)'}, but the entry order {order_id} is on "
                f"{entry_sym}. These are two different contracts and cannot be the "
                f"two ends of one play. Matching on ticker alone is what closed the "
                f"wrong AMD leg on 2026-07-22 — the contract symbol is the key. "
                f"Nothing was written.")

    # 5. the exit must be the closing counterpart
    exit_norm = normalize_side(exit_side, exit_position_intent)
    if not exit_norm["closing"]:
        _refuse("exit side is not a closing fill",
                f"[NOT WRITTEN] The exit order {exit_order_id} has side "
                f"{exit_norm['raw'] or '(missing)'!r}, which is not a sell. The "
                f"closing counterpart of a long entry is a sell — a second buy is an "
                f"ADD to the position, not an exit, and recording it as one would "
                f"book a fictional profit and hide live risk. Nothing was written.")

    # 6. time must run forwards
    if filled_at and exit_filled_at and str(exit_filled_at) < str(filled_at):
        _refuse("exit fill is earlier than the entry fill",
                f"[NOT WRITTEN] The exit fill is timestamped {exit_filled_at}, which "
                f"is BEFORE the entry fill at {filled_at}. A position cannot be closed "
                f"before it is opened, so at least one of these two orders is not what "
                f"you think it is. Nothing was written.")

    # 7. size must match — no partial-close support, by design
    try:
        qty_mismatch = float(filled_qty) != float(exit_filled_qty)
    except (TypeError, ValueError):
        qty_mismatch = True
    if qty_mismatch:
        _refuse("exit quantity differs from entry quantity",
                f"[NOT WRITTEN] The entry filled {filled_qty} contract(s) but the exit "
                f"filled {exit_filled_qty}. THIS FUNCTION HAS NO PARTIAL-CLOSE SUPPORT "
                f"— that is a deliberate scope choice, not an oversight: one row here "
                f"describes one whole position, opened and closed, and a partial exit "
                f"needs a position model this journal does not have.\n"
                f"A partially-closed play is NOT unfixable. Journal the position with "
                f"backfill_verified_option_play (entry only), then record the partial "
                f"exit with close_options_play_early using force=true and an explicit "
                f"force_reason naming the partial quantity. That is the documented "
                f"escape hatch. Nothing was written here.")

    # 8. neither price may be missing
    if filled_avg_price is None or exit_filled_avg_price is None:
        _refuse("a fill price is missing",
                f"[NOT WRITTEN] entry fill price is {filled_avg_price!r} and exit fill "
                f"price is {exit_filled_avg_price!r}. A P&L cannot be computed from a "
                f"missing price, and substituting a zero for one is the exact defect "
                f"(C) that made SPY row 5afd580d compute as worthless. Nothing was "
                f"written.")

    # 9. thesis_score range
    if thesis_score is not None:
        try:
            ts = int(thesis_score)
        except (TypeError, ValueError):
            ts = None
        if ts is None or not (0 <= ts <= 100):
            _refuse("thesis_score out of range",
                    f"[NOT WRITTEN] thesis_score {thesis_score!r} is not an integer "
                    f"between 0 and 100. Nothing was written.")
        thesis_score = ts

    # 10/11. neither order may already be journaled
    for oid, label in ((order_id, "entry"), (exit_order_id, "exit")):
        if not oid:
            continue
        dup = find_play_by_order_id(oid)
        if dup:
            _refuse(f"{label} order already journaled",
                    f"[NOT WRITTEN] The {label} order {oid} is already recorded on play "
                    f"{dup['id']} ({dup.get('ticker')} {dup.get('strategy')}, status "
                    f"{dup.get('status')}). Writing this would double-count one real "
                    f"position — the MSFT 276b1ea0 failure mode exactly. Nothing was "
                    f"written.\n"
                    f"If play {dup['id']} is still open and this is its exit, close THAT "
                    f"row with close_options_play_early instead of creating a new one.")

    # ── P&L: computed here and nowhere else ──────────────────────────────
    actual_pnl = round(
        (float(exit_filled_avg_price) - float(filled_avg_price)) * float(filled_qty) * 100.0,
        2)

    if stated_pnl is not None:
        try:
            sp = float(stated_pnl)
        except (TypeError, ValueError):
            sp = None
        if sp is None:
            _refuse("stated P&L is not a number",
                    f"[NOT WRITTEN] stated_pnl {stated_pnl!r} is not a number. The two "
                    f"real fills compute to ${actual_pnl:+.2f}. Nothing was written.")
        elif abs(actual_pnl - sp) > 1.0:
            # Neither number silently wins. Both are printed, and a human decides.
            # Same $1.00 tolerance cross_check_play() uses — that guard worked on
            # 2026-07-30 and its threshold is deliberately mirrored, not loosened.
            _refuse("stated P&L disagrees with the computed P&L",
                    f"[NOT WRITTEN] The P&L you stated does not match what the two real "
                    f"fills compute to.\n"
                    f"    you stated : ${sp:+.2f}\n"
                    f"    fills say  : ${actual_pnl:+.2f}   "
                    f"(({float(exit_filled_avg_price)} - {float(filled_avg_price)}) "
                    f"x {float(filled_qty):g} x 100)\n"
                    f"    difference : ${actual_pnl - sp:+.2f}  (tolerance is $1.00)\n"
                    f"Nothing was written. The fills came from Alpaca and the stated "
                    f"figure came from you — but this function will not pick a winner. "
                    f"Check which order ids you passed, or drop stated_pnl and let the "
                    f"fills speak.")

    # ── build the COMPLETE row, then write it exactly once ───────────────
    entry_date = play_data.get("entry_date") or (str(filled_at)[:10] if filled_at
                                                 else date.today().isoformat())
    row = _default_play_row(entry_date)
    row.update(play_data)                    # model-supplied description first…
    row.update({                             # …then machine-sourced truth, which wins
        "entry_date": entry_date,
        "execution_verified": True,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "contract_symbol": entry_sym,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "filled_at": filled_at or datetime.now().isoformat(),
        "execution_note": "verified — entry AND exit both match confirmed Alpaca fills",
        "premium": float(filled_avg_price),
        "contracts": int(float(filled_qty)),
        "exit_order_id": exit_order_id,
        "exit_client_order_id": exit_client_order_id,
        "exit_filled_qty": exit_filled_qty,
        "exit_filled_avg_price": exit_filled_avg_price,
        "exit_filled_at": exit_filled_at,
    })
    row.update(_closure_updates(
        actual_pnl,
        notes or (f"Entry {filled_qty:g} @ ${float(filled_avg_price):.2f} "
                  f"(order {order_id}), exit {exit_filled_qty:g} @ "
                  f"${float(exit_filled_avg_price):.2f} (order {exit_order_id}). "
                  f"Both fills verified against Alpaca. P&L computed, not stated."),
        thesis_score=thesis_score,
        thesis_score_notes=thesis_score_notes,
    ))
    return _write_play_atomic(row)


def find_play_by_order_id(order_id: str) -> dict | None:
    for p in _load_journal():
        if p.get("order_id") == order_id or p.get("client_order_id") == order_id:
            return p
    return None


def find_unverified_open_plays() -> list:
    """Open/closed_early plays never tied to a confirmed Alpaca fill — i.e.
    recommendations, not confirmed executions. Not inherently a bug, but useful
    for reconciliation reporting so Nico can see which journal entries are ideas
    vs. real positions."""
    return [p for p in _load_journal()
            if p.get("status") in ("open", "closed_early") and not p.get("execution_verified")]


def get_open_plays() -> list:
    """All plays with status 'open'."""
    return [p for p in _load_journal() if p.get("status") == "open"]


def get_all_plays() -> list:
    return _load_journal()


def get_play_by_id(play_id: str) -> dict | None:
    for p in _load_journal():
        if p["id"] == play_id:
            return p
    return None


def update_play(play_id: str, updates: dict) -> dict | None:
    """
    Update a play by ID. Recomputes the accuracy ledger after every update.
    Returns updated play or None if not found.
    """
    plays = _load_journal()
    for i, p in enumerate(plays):
        if p["id"] == play_id:
            plays[i].update(updates)
            _save_journal(plays)
            _recompute_accuracy(plays)
            return plays[i]
    return None


def _default_position_lookup(symbol: str) -> dict:
    """Live Alpaca position lookup, imported lazily so this module still loads
    (and still tests) with no broker configured. Deliberately reuses
    mason_execution.get_position() rather than adding a second lookup path —
    one source of position truth, not two."""
    import mason_execution
    return mason_execution.get_position(symbol)


def _default_settlement_lookup(symbol: str) -> dict:
    """Real Alpaca disposition (sold-early vs expired-worthless), lazily imported
    so this module still loads and tests offline. Fails OPEN inside
    settlement_disposition(), so any broker error surfaces as kind='unknown' and
    never blocks a review."""
    import mason_execution
    return mason_execution.settlement_disposition(symbol)


def _yf_expiry_history(ticker: str, start: str, end: str):
    """Default fetch behind _expiry_history_fetch: yfinance daily bars with
    auto_adjust=False (no DIVIDEND adjustment) and actions=True (so the
    'Stock Splits' column is present). Note yfinance still SPLIT-adjusts Close
    even with auto_adjust=False — _expiry_close_quote undoes that."""
    import yfinance as yf
    import mason_symbols
    return yf.Ticker(mason_symbols.yf_symbol(str(ticker))).history(start=start, end=end,
                                          auto_adjust=False, actions=True)


# Module-level hook (T-D10, 2026-10-19): tests patch this so no test reaches
# the network. Signature: (ticker, start_iso, end_iso_exclusive) -> DataFrame.
_expiry_history_fetch = _yf_expiry_history

# Longest listed equity-option life (LEAPS ~2.5-3y). When a play's open date is
# unknown, a split anywhere in this window before expiry may have adjusted it.
_MAX_CONTRACT_LIFE_DAYS = 1100

# NYSE full-day closures that no fixed rule produces (days of mourning,
# weather). mason_director._NYSE_HOLIDAYS is unioned in at call time as well.
_NYSE_SPECIAL_CLOSURES = frozenset({
    date(2012, 10, 29), date(2012, 10, 30),   # Hurricane Sandy
    date(2018, 12, 5),                        # President G.H.W. Bush mourning
    date(2025, 1, 9),                         # President Carter mourning
})


def _nyse_rule_closures(year: int) -> set:
    """NYSE full-day holidays for `year` from the exchange's fixed rules
    (Rule 7.2): New Year's (Sat → not observed), MLK, Presidents', Good Friday,
    Memorial, Juneteenth (2022+), Independence, Labor, Thanksgiving, Christmas;
    Sat → Fri, Sun → Mon observance."""
    from datetime import timedelta

    def nth(month, weekday, n):
        if n > 0:
            d = date(year, month, 1)
            d += timedelta(days=(weekday - d.weekday()) % 7)
            return d + timedelta(weeks=n - 1)
        d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - weekday) % 7)

    def observed(d):
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    # Easter (anonymous Gregorian algorithm) → Good Friday
    a, b, c = year % 19, year // 100, year % 100
    d_, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d_ - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l_) // 433
    month = (h + l_ - 7 * m + 90) // 25
    day = (h + l_ - 7 * m + 33 * month + 19) % 32
    good_friday = date(year, month, day) - timedelta(days=2)

    out = set()
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        out.add(date(year, 1, 2))
    elif ny.weekday() < 5:
        out.add(ny)
    out |= {nth(1, 0, 3), nth(2, 0, 3), good_friday, nth(5, 0, -1),
            observed(date(year, 7, 4)), nth(9, 0, 1), nth(11, 3, 4),
            observed(date(year, 12, 25))}
    if year >= 2022:
        out.add(observed(date(year, 6, 19)))
    return out


def _expiry_session_expected(d: date) -> bool:
    """Was `d` a regular NYSE session? Weekday and not a full-day closure
    (rule holidays ∪ special closures ∪ mason_director._NYSE_HOLIDAYS)."""
    if d.weekday() >= 5:
        return False
    if d in _NYSE_SPECIAL_CLOSURES or d in _nyse_rule_closures(d.year):
        return False
    try:
        import mason_director
        if d.isoformat() in getattr(mason_director, "_NYSE_HOLIDAYS", ()):
            return False
    except Exception:
        pass
    return True


# Public name (Director ruling #30): T-D3b's mason_option_bars._session_expected
# reuses this calendar instead of mason_director.is_trading_day (whose holiday
# list starts 2026-11-26). Verified against XNYS 2015-2030, 0 mismatches.
nyse_session_expected = _expiry_session_expected


def _expiry_close_quote(ticker: str, expiration, today: date = None,
                        opened=None) -> dict:
    """The underlying's RAW, as-printed close ON the expiration date, with a
    status saying whether it is safe to grade against.

    Returns {"price": float|None, "status": "ok"|"unavailable"|"unsafe",
             "reason": str, "detail": str}. Never raises.

      ok          — price is the raw expiry-session close (split-safe).
      unavailable — NO USABLE DATA only (fetch error, nothing returned, no
                    Close column, no bar at/before a non-session expiry).
                    Callers keep the 2026-09-22 fail-open: skip the check.
      unsafe      — a price exists (or could) but grading against it could be
                    WRONG, or the data that decides it is SUSPICIOUS:
          not_final              expiry date not over yet (the bar may be intraday)
          bad_expiration         the play's expiration can't be parsed
          missing_split_column   Close present but no 'Stock Splits' column
          missing_session_bar    no bar for a regular session; never walk back
                                 over a real session to a neighbouring close
          conflicting_bars       two different closes for the graded bar's date
          split_in_contract_life a split ex-date in (opened, expiry]: OCC
                                 adjusted the contract, so the journal's strike
                                 is not comparable with either print
          suspicious_split_data  a corrupt / unparseable / conflicting split row
                                 inside a range that changes the result
          processing_error       an unexpected error while reading the data
        record_expiry_review REFUSES on unsafe (T-D10 round 2).
    Suspicious rows on dates that cannot change the result (conflicting bars
    on non-graded days; split rows at/before the open date) are ignored
    (T-D10 round 4), so one bad row far from expiry never disables the guard.

    Split-safe (T-D10): yfinance's Close is split-adjusted even with
    auto_adjust=False. The chosen close is un-adjusted by the product of split
    ratios dated after that bar and on/before `today` (one ratio per date) —
    the method of mason_option_bars._t3_unadjusted_close. Walk-back to an
    earlier bar only across NON-sessions (weekend / NYSE holiday), matching
    mason_option_bars._pick_close. Price number only: the R3 settle order
    (Alpaca disposition -> intrinsic off raw close -> bar close) is unchanged."""
    import math
    from datetime import timedelta

    def _q(price, status, reason, detail=""):
        return {"price": price, "status": status, "reason": reason, "detail": detail}

    try:
        exp = date.fromisoformat(str(expiration)[:10])
    except Exception:
        return _q(None, "unsafe", "bad_expiration", f"expiration {expiration!r} unparseable")
    _today = today or date.today()
    if exp >= _today:
        return _q(None, "unsafe", "not_final",
                  f"expiration {exp} is not over yet (today {_today}); the daily bar "
                  f"may still be intraday")
    _opened = None
    try:
        if opened:
            _opened = date.fromisoformat(str(opened)[:10])
    except Exception:
        _opened = None
    if _opened is not None and _opened >= exp:
        _opened = None                                   # nonsensical → unknown
    life_start = _opened or (exp - timedelta(days=_MAX_CONTRACT_LIFE_DAYS))
    start = min(life_start, exp - timedelta(days=7)).isoformat()
    end   = (_today + timedelta(days=1)).isoformat()     # later splits must be visible
    # auto_adjust=False: options settle on the ACTUAL traded close, not the
    # dividend-adjusted one. For a dividend payer (SPY) the adjusted close is
    # several dollars off the price the contract actually settled against,
    # which can flip a near-strike outcome. Grade against the real print.
    try:
        hist = _expiry_history_fetch(str(ticker), start, end)
    except Exception as e:
        return _q(None, "unavailable", "fetch_error", str(e)[:160])
    try:
        if hist is None or not hasattr(hist, "iterrows") or not hasattr(hist, "columns"):
            return _q(None, "unavailable", "no_data")
        if hist.empty:
            return _q(None, "unavailable", "no_data")
        if "Close" not in hist.columns:
            return _q(None, "unavailable", "missing_close_column")
        if "Stock Splits" not in hist.columns:
            return _q(None, "unsafe", "missing_split_column",
                      "no 'Stock Splits' column, so a split can't be ruled out")

        def _bar_date(idx):
            tz = getattr(idx, "tzinfo", None)
            if (tz is not None and str(tz) in ("UTC", "utc", "UTC+00:00")
                    and (idx.hour, idx.minute, idx.second) != (0, 0, 0)):
                idx = idx.tz_convert("America/New_York")   # exchange-local date
            return idx.date()

        def _num(v):
            try:
                return float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                return None                               # unparseable

        closes = {}        # date -> set of finite closes (identical dups collapse)
        ratios = {}        # date -> set of valid split ratios
        bad_split = {}     # date -> why the split row is corrupt
        for idx, row in hist.iterrows():
            d = _bar_date(idx)
            c = _num(row["Close"])
            if c is not None and math.isfinite(c) and c > 0:
                closes.setdefault(d, set()).add(round(c, 9))
            r = _num(row["Stock Splits"])
            if r is None:
                bad_split[d] = f"unparseable ratio {row['Stock Splits']!r}"
            elif math.isnan(r) or r == 0.0:
                pass                                      # yfinance: 0 = no split
            elif r < 0 or math.isinf(r):
                bad_split[d] = f"ratio {r}"
            else:
                ratios.setdefault(d, set()).add(round(r, 9))
        for d, rs in ratios.items():
            if len(rs) > 1:
                bad_split[d] = f"conflicting ratios {sorted(rs)}"

        # Contract life (opened, exp]: any split there means OCC adjusted it.
        life_split = sorted(d for d in ratios if life_start < d <= exp)
        if life_split:
            d0 = life_split[0]
            return _q(None, "unsafe", "split_in_contract_life",
                      f"split dated {d0} (ratio {', '.join(f'{x:g}' for x in sorted(ratios[d0]))}) "
                      f"between "
                      f"{'open ' + str(_opened) if _opened else 'the earliest possible open'} "
                      f"and expiry {exp}")
        life_bad = sorted(d for d in bad_split if life_start < d <= exp)
        if life_bad:
            return _q(None, "unsafe", "suspicious_split_data",
                      f"split row on {life_bad[0]} inside the contract life: {bad_split[life_bad[0]]}")

        if exp in closes:
            bar_day = exp
        else:
            earlier = [d for d in closes if d < exp]
            if not earlier:
                if _expiry_session_expected(exp):
                    return _q(None, "unsafe", "missing_session_bar",
                              f"no bar for {exp}, a regular NYSE session")
                return _q(None, "unavailable", "no_bar")
            bar_day = max(earlier)
            gap = bar_day + timedelta(days=1)
            while gap <= exp:                             # walk back over non-sessions only
                if _expiry_session_expected(gap):
                    return _q(None, "unsafe", "missing_session_bar",
                              f"no bar for {gap}, a regular NYSE session")
                gap += timedelta(days=1)
        if len(closes[bar_day]) > 1:
            return _q(None, "unsafe", "conflicting_bars",
                      f"{bar_day} has different closes {sorted(closes[bar_day])}")

        # Restatement range (bar_day, today]: every split here multiplies the price.
        rest_bad = sorted(d for d in bad_split if bar_day < d <= _today)
        if rest_bad:
            return _q(None, "unsafe", "suspicious_split_data",
                      f"split row on {rest_bad[0]} after expiry: {bad_split[rest_bad[0]]}")
        factor = 1.0
        for d, rs in ratios.items():
            if bar_day < d <= _today:
                factor *= next(iter(rs))
        close = next(iter(closes[bar_day]))
        return _q(round(close * factor, 6), "ok", "ok",
                  f"close {bar_day}" + (f" x {factor:g} (later splits)" if factor != 1.0 else ""))
    except Exception as e:
        return _q(None, "unsafe", "processing_error", str(e)[:160])


def _expiry_close_price(ticker: str, expiration, today: date = None, opened=None):
    """The underlying's REAL closing price ON the expiration date — the number an
    expiry review is supposed to be graded against. Returns a float only when
    _expiry_close_quote says it is safe ('ok'); None otherwise ("cannot verify"
    — not yet final, no data, a split during the contract's life, or a missing
    session bar). Never raises. This is the fix for the 2026-09-22 defect where
    the cross-check fetched period='1d' (today's spot) instead of the
    settlement-date close; T-D10 made it split-safe."""
    try:
        q = _expiry_close_quote(ticker, expiration, today=today, opened=opened)
    except Exception:
        return None
    return q["price"] if q.get("status") == "ok" else None


def set_hold_directive(play_id: str, hold_until: str | None, reason: str = "",
                        set_by: str = "nico") -> dict:
    """Record Nico's standing instruction to HOLD a play until a date — the
    machine-readable form of "don't close this before the August 4th print".

    Added 2026-07-28 (journal-integrity fix 3). Before this, no field anywhere
    in the codebase encoded a hold instruction; it lived only in conversation
    context and evaporated with it. close_play_early() reads this field and
    refuses a premature close.

    Pass hold_until=None (or an empty string) to CLEAR an existing directive —
    otherwise a directive could only ever be overridden with force=True, which
    would train Mason to force by habit and defeat the whole guard.

    Returns {"ok": bool, "play": dict|None, "error": str|None}.
    """
    play = get_play_by_id(play_id)
    if play is None:
        return {"ok": False, "play": None,
                "error": f"Play {play_id} not found in the journal."}

    if not hold_until:
        updated = update_play(play_id, {"hold_directive": None})
        return {"ok": True, "play": updated, "error": None, "cleared": True}

    hold_until = str(hold_until).strip()[:10]
    try:
        date.fromisoformat(hold_until)
    except ValueError:
        return {"ok": False, "play": None,
                "error": f"hold_until '{hold_until}' is not a valid YYYY-MM-DD date."}
    if not str(reason).strip():
        return {"ok": False, "play": None,
                "error": "A hold directive needs a reason — it is the thing that stops a "
                         "future close, so it has to say why in Nico's own terms."}

    directive = {
        "hold_until": hold_until,
        "reason": str(reason).strip(),
        "set_at": datetime.now().isoformat(),
        "set_by": set_by or "nico",
    }
    updated = update_play(play_id, {"hold_directive": directive})
    return {"ok": True, "play": updated, "error": None, "cleared": False}


def _hold_block(play: dict, today: date) -> dict | None:
    """Returns the active hold directive if `today` is before its hold_until,
    else None. A missing/None/malformed directive blocks nothing."""
    d = play.get("hold_directive")
    if not isinstance(d, dict):
        return None
    hu = d.get("hold_until")
    if not hu:
        return None
    try:
        until = date.fromisoformat(str(hu)[:10])
    except (ValueError, TypeError):
        return None
    return d if today < until else None


def _closure_updates(actual_pnl: float, notes: str = "", thesis_score: int = None,
                     thesis_score_notes: str = None, forced_note: str = "") -> dict:
    """The field-set that marks a play closed_early. Pure — no disk, no clock
    beyond date.today(), no writes. Returns the dict an update_play()/atomic
    write should apply.

    Extracted 2026-07-30 from close_play_early()'s own tail so that
    log_verified_closed_play() writes the IDENTICAL closure shape rather than
    a second, drifting copy of it. This is a refactor and nothing more:
    close_play_early()'s observable behaviour is unchanged, including the
    exact review string it produces in the forced and unforced cases.

    Note the asymmetry that is preserved deliberately: the review string is
    only .strip()ed when a forced_note is appended, matching the original
    (`review = f"{review} [FORCED OVERRIDE: ...]".strip()` ran only under
    `if force:`). An empty-notes unforced close still yields the trailing
    space it always did.
    """
    outcome = "win" if actual_pnl > 0 else ("loss" if actual_pnl < 0 else "breakeven")
    review  = f"Closed early. {notes}"
    if forced_note:
        review = f"{review} {forced_note}".strip()

    updates = {
        "status": "closed_early",
        "outcome": outcome,
        "actual_pnl": actual_pnl,
        "expiry_review": review,
        "expiry_review_date": date.today().isoformat(),
    }
    if thesis_score is not None:
        updates["thesis_score"] = thesis_score
    if thesis_score_notes:
        updates["thesis_score_notes"] = thesis_score_notes
    return updates


# ── close P&L check (knowledge base item 4, 2026-11-18) ─────────────────────────────────────────────────────────
# close_play_early used to book whatever P&L its caller typed (the IBM d3a06789 row was booked -$2,940 for a real
# -$1,050 that way). Mason's own option exits close their rows from the real fills since 11-18
# (mason_option_journal); the paths left that take a typed number are the chat tools close_options_play_early and
# review_options_play (30d/60d with a P&L), and manage_journal.py close-option. With the switch on, the typed number
# is compared with the closing fills Mason already stores for that play (no broker call): when they cover the whole
# play and disagree, the fills' result is booked, the typed number and the difference are kept on the row, and the
# caller gets a one-line note. No stored fills -> booked as typed, exactly as before (a manual close is never
# blocked). Switch: "options_journal": {"close_pnl_check": true} (read per call). Absent/false = today, unchanged.
_CPC = {"mtime": None, "on": False}
_CPC_TOL = 1.0                                   # dollars: closer than this counts as the same number


def _close_pnl_check_on() -> bool:
    try:
        p = Path(__file__).parent / "mason.config.json"
        m = p.stat().st_mtime
        if _CPC["mtime"] != m:
            c = json.loads(p.read_text(encoding="utf-8"))
            _CPC.update(mtime=m, on=((c.get("options_journal") or {}).get("close_pnl_check") is True))
        return _CPC["on"]
    except Exception:
        return False


def _fnum(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _is_multi_leg(play: dict) -> bool:
    strikes = play.get("strikes") or {}
    legs = [k for k, v in strikes.items() if v is not None] if isinstance(strikes, dict) else []
    return len(legs) > 1 or "spread" in str(play.get("strategy") or "").lower()


def stored_exit_fills(play: dict) -> dict | None:
    """The closing fills Mason already stores for this play, or None. Exact links only, never ticker or contract
    alone: (1) the play's own exit fill fields; else (2) the ONE trade record written for this play — Mason's own
    (agent_trades source 'mason', linked by journal id or entry order id) or Nico's website trade (source 'nico',
    same entry order id). Returns {qty, avg_px, partial, source, order_id}. Read-only; never raises."""
    try:
        q, px = _fnum(play.get("exit_filled_qty")), _fnum(play.get("exit_filled_avg_price"))
        if q and q > 0 and px is not None:
            return {"qty": q, "avg_px": px, "partial": False, "source": "the play's own sale record",
                    "order_id": play.get("exit_order_id")}
        occ = str(play.get("contract_symbol") or "").strip().upper()
        oid = str(play.get("order_id") or "")
        if not occ:
            return None
        conn = DB.get_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM agent_trades WHERE UPPER(COALESCE(occ,''))=? AND COALESCE(record_deleted,0)=0",
                (occ,)).fetchall()]
        except Exception:
            rows = []
        finally:
            conn.close()
        mine = []
        for r in rows:
            try:
                feats = json.loads(r.get("features_json") or "{}")
                feats = feats if isinstance(feats, dict) else {}
            except Exception:
                feats = {}
            if r.get("source") == "mason":
                linked = ((str(r.get("journal_id") or "") == str(play.get("id"))
                           and str(r.get("journal_table") or "options_trades") == "options_trades")
                          or (oid and str(feats.get("order_id") or "") == oid))
            elif r.get("source") == "nico":
                linked = bool(oid) and str(r.get("rec_id") or "") == oid
            else:
                linked = False
            if linked:
                mine.append((r, feats))
        if len(mine) != 1:
            return None
        r, feats = mine[0]
        who = "Mason's trade record" if r.get("source") == "mason" else "Nico's trade record"
        if str(r.get("status") or "") == "closed":
            qty, epx = _fnum(r.get("qty")), _fnum(r.get("exit_px"))
            reason = str(r.get("exit_reason") or "")
            # (review 2026-11-19) Nico's records are closed at $0 'expired' by mason_nico_thesis._expire whenever the
            # contract is gone after its expiry date, also when it expired IN the money and turned into shares: that
            # $0 is not a sale and must never replace a typed number.
            # (2026-11-19) the same for 'exercised': mason_nico_thesis now closes an in-the-money expiry at its value
            # from the expiry day's close, a computed number and not a sale either.
            if (not qty or epx is None or reason == "expired_with_value"
                    or (r.get("source") == "nico" and reason in ("expired", "exercised"))):
                return None
            return {"qty": qty, "avg_px": epx, "partial": False, "source": who, "order_id": r.get("exit_order_id")}
        parts = [(_fnum(x.get("qty")), _fnum(x.get("px"))) for x in (feats.get("partial_exits") or [])
                 if isinstance(x, dict)]
        parts = [(a, b) for a, b in parts if a and b is not None]
        if not parts:
            return None
        sq = sum(a for a, _ in parts)
        return {"qty": sq, "avg_px": round(sum(a * b for a, b in parts) / sq, 6), "partial": True, "source": who,
                "order_id": None}
    except Exception:
        return None


def _close_pnl_check(play: dict, stated: float, forced: bool = False) -> dict:
    """{"record": what is kept on the row, "use": the P&L to book instead (only when the stored fills cover the whole
    play and disagree), "note": one plain line for the caller}. Pure apart from the stored-fill read.
    (Review 2026-11-19) forced=True (close_play_early(force=True, force_reason=...): a person correcting a row from
    manage_journal.py or a repair script; the chat can never force) books the number given and only reports the
    difference: a forced close is how a wrong stored record gets corrected, so the stored fills must not win there."""
    rec = {"checked_at": datetime.now().isoformat(timespec="seconds"), "stated": stated, "from_fills": None,
           "difference": None}
    fills = stored_exit_fills(play)
    if fills is None:
        rec["result"] = "no stored fills; the number given was booked"
        return {"record": rec}
    rec["fills"] = fills
    entry = _fnum(play.get("filled_avg_price")) or _fnum(play.get("premium"))
    n = _fnum(play.get("filled_qty")) or _fnum(play.get("contracts"))
    if _is_multi_leg(play) or not entry or not n:
        rec["result"] = "the stored fills can't be priced for this play (spread, or no cost/contracts); booked as given"
        return {"record": rec}
    credit = (str(play.get("premium_type") or "").lower() == "credit"
              or str(play.get("strategy") or "").lower().startswith("short_"))
    per = (entry - fills["avg_px"]) if credit else (fills["avg_px"] - entry)
    if fills["partial"] or abs(fills["qty"] - n) > 1e-6:
        rec["result"] = (f"the stored sales cover {fills['qty']:g} of {n:g} contracts; the number given was booked")
        _log_pnl_check(play, rec)
        return {"record": rec}
    fp = round(per * n * 100.0, 2)
    diff = round(float(stated) - fp, 2)
    rec.update(from_fills=fp, difference=diff)
    if abs(diff) <= _CPC_TOL:
        rec["result"] = "matches the stored fills"
        return {"record": rec}
    sale = (f"{_usd(fp, signed=True)} ({n:g} contract{'s' if n != 1 else ''} at ${fills['avg_px']:.2f}, cost "
            f"${entry:.2f})")
    if forced:
        rec["result"] = "forced close: the number given was booked; the stored fills disagree"
        _log_pnl_check(play, rec)
        note = (f"Heads up: the sale Mason has on record says {sale}, but this close was forced, so the number you "
                f"gave, {_usd(stated, signed=True)}, was booked. Both are kept with the play.")
        return {"record": rec, "note": note}
    rec["result"] = "booked the stored fills' result instead of the number given"
    _log_pnl_check(play, rec)
    note = (f"Heads up: you gave {_usd(stated, signed=True)}, but the sale Mason has on record says "
            f"{sale}, so {_usd(fp, signed=True)} was booked. The number you gave is kept with the play.")
    return {"record": rec, "use": fp, "note": note}


def _log_pnl_check(play: dict, rec: dict):
    try:
        import logging
        logging.getLogger("mason.options").warning(
            "close_play_early %s %s: P&L given %s, stored fills %s (%s)", play.get("id"),
            play.get("contract_symbol"), rec.get("stated"), rec.get("from_fills"), rec.get("result"))
    except Exception:
        pass


def close_play_early(play_id: str, actual_pnl: float, notes: str = "",
                      force: bool = False, force_reason: str = "",
                      position_lookup=None, today: date = None) -> dict:
    """Mark a play as closed before expiration — but ONLY after proving to the
    live broker that the contract is actually flat.

    Added 2026-07-28 (journal-integrity fix 1). This function used to take a
    play_id and a P&L number and write status="closed_early", full stop. It
    never asked whether the position was still open. Real incident: a two-leg
    AMD position was journaled as two unlinked single-leg rows sharing only
    ticker "AMD"; the $550 leg's CLOSING fill was matched onto the $500 leg's
    row by ticker + English prose in `notes`, closing a row for a position
    that is still open and booking -$11,090 against it.

    Note the asymmetry this corrects: delete_play() has always refused to touch
    an execution_verified row without force=True. Deleting a record was
    guarded; closing a live position's record was not — and a wrongly-closed
    row is worse than a deleted one, because it silently stops the scanner and
    position alerts on a position that is still carrying real risk.

    Refuses when:
      1. a hold directive from Nico is still in force (see set_hold_directive)
      2. Alpaca reports a LIVE position on this play's exact contract_symbol
      3. Alpaca can't be reached, or the play claims execution_verified with no
         contract symbol to check

    force=True (which REQUIRES force_reason) bypasses 1 and 2 only, and records
    the override reason into expiry_review. It never bypasses 3: a broker we
    cannot reach is not evidence that a position is closed. That is the
    opposite of the debate-staleness fix's fail-open choice, and deliberately
    so — that one skipped paid work, this one destroys position truth.

    Matching keys on contract_symbol, never on ticker.

    position_lookup: injectable broker dependency (symbol -> dict, the shape
    mason_execution.get_position returns). Defaults to the real Alpaca lookup.
    today: injectable clock for the hold-directive check.

    Returns {"ok": bool, "play": dict|None, "error": str|None,
             "refused_because": str|None}.
    """
    today  = today or date.today()
    lookup = position_lookup or _default_position_lookup

    play = get_play_by_id(play_id)
    if play is None:
        return {"ok": False, "play": None, "refused_because": "not_found",
                "error": f"[NOT CLOSED] Play {play_id} not found in the journal."}

    label  = f"{play.get('ticker')} {play.get('strategy')}"
    symbol = (play.get("contract_symbol") or "").strip().upper()

    def _refuse(because: str, error: str) -> dict:
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "close_play_early",
            "play_id": play_id,
            "contract_symbol": symbol or None,
            "refused_because": because,
            "attempted_pnl": actual_pnl,
            "notes": notes,
            "forced": bool(force),
            "error": error,
        })
        return {"ok": False, "play": None, "refused_because": because, "error": error}

    if force and not str(force_reason).strip():
        return _refuse(
            "force_without_reason",
            f"[NOT CLOSED] force=true on play {play_id} ({label}) requires an explicit reason. "
            f"Overriding a live-position or hold-directive guard has to be justified "
            f"in writing — it gets recorded on the play."
        )

    # ── 0. Already closed ──────────────────────────────────────────
    # Added 2026-07-30. close_play_early never looked at the play's own status,
    # so calling it twice silently OVERWROTE the first result — status, outcome
    # and actual_pnl all replaced, no warning, and it returned ok:True both
    # times. Proven: a play closed at +$5,000 re-closed at -$999 ends up
    # -$999/loss with nothing recording that the number ever changed.
    #
    # Real incident: Mason called this on 5c29d5a8, which the guardgap repair
    # had already closed at +$5,000. His number happened to match, so nothing
    # broke — that was luck, not a guard. The broker check below cannot help
    # here: a genuinely closed position IS flat, so the flat check passes and
    # waves the second write straight through.
    #
    # force=true bypasses this (with a reason, like the others) because
    # correcting a wrong P&L on a closed row is a real need. It is bypassable,
    # but never silent.
    _closed_states = ("closed_early", "expired", "reviewed")
    if play.get("status") in _closed_states and not force:
        return _refuse(
            "already_closed",
            f"[NOT CLOSED] Play {play_id} ({label}) is ALREADY "
            f"'{play.get('status')}' — outcome '{play.get('outcome')}', P&L "
            f"{_money(play.get('actual_pnl'))}, recorded "
            f"{play.get('expiry_review_date') or 'earlier'}.\n"
            f"Refusing to close it a second time. This function overwrites "
            f"status/outcome/actual_pnl wholesale, so a second close would "
            f"silently replace:\n"
            f"    already on the row : {_money(play.get('actual_pnl'))}\n"
            f"    this call says     : {_money(actual_pnl)}\n"
            f"...and report success either way.\n"
            f"If the recorded P&L is WRONG and you are correcting it, pass "
            f"force=true with a reason saying what the old number was and why the "
            f"new one is right — the override gets written onto the play. If you "
            f"are just re-confirming a close that already happened, there is "
            f"nothing to do. Nothing was written."
        )

    # ── 1. Nico's standing hold directive ──────────────────────────
    held = _hold_block(play, today)
    if held and not force:
        return _refuse(
            "hold_directive",
            f"[NOT CLOSED] Play {play_id} ({label}{f' {symbol}' if symbol else ''}) is under a "
            f"HOLD directive from {held.get('set_by', 'nico')} until {held.get('hold_until')} "
            f"(today is {today.isoformat()}). Reason on file: \"{held.get('reason')}\". "
            f"Nothing was written — this play is still OPEN and must not be described as closed. "
            f"Either wait until {held.get('hold_until')}, clear the directive with "
            f"set_hold_directive, or pass force=true with an explicit reason if Nico has just "
            f"told you to exit early."
        )

    # ── 2/3. The broker is the only authority on whether this is flat ──
    if symbol:
        try:
            pos = lookup(symbol)
        except Exception as e:                       # transport, auth, timeout, anything
            return _refuse(
                "broker_unreachable",
                f"[NOT CLOSED] Could not reach Alpaca to check {symbol} for play {play_id} "
                f"({label}): {e}. Refusing to write a close — a broker we can't reach is not "
                f"evidence that a position is closed. Nothing was written; this play is still "
                f"OPEN. Retry once the broker is reachable. force=true does NOT bypass this."
            )
        if not isinstance(pos, dict) or not pos.get("ok"):
            err = pos.get("error") if isinstance(pos, dict) else pos
            return _refuse(
                "broker_unreachable",
                f"[NOT CLOSED] Alpaca returned an error checking {symbol} for play {play_id} "
                f"({label}): {err}. Refusing to write a close — a broker we can't reach is not "
                f"evidence that a position is closed. Nothing was written; this play is still "
                f"OPEN. force=true does NOT bypass this."
            )
        live_qty = float(pos.get("qty") or 0)
        if pos.get("exists") and live_qty > 0 and not force:
            return _refuse(
                "live_position",
                f"[NOT CLOSED] Play {play_id} ({label}) is tied to contract {symbol}, and Alpaca "
                f"still shows a LIVE position of {live_qty:g} contract(s) on that exact symbol. "
                f"Refusing to mark it closed — this play is STILL OPEN and must not be described "
                f"as closed, reviewed as closed, or counted in P&L. Close it for real first with "
                f"close_paper_position on {symbol}, then call this again; or, if the journal row "
                f"is genuinely wrong about the position, pass force=true with an explicit reason."
            )
    elif play.get("execution_verified"):
        return _refuse(
            "no_contract_symbol",
            f"[NOT CLOSED] Play {play_id} ({label}) is marked execution_verified but has no "
            f"contract_symbol, so there is no way to ask Alpaca whether it is flat. Refusing to "
            f"guess — matching on ticker alone is exactly what closed the wrong AMD leg. Nothing "
            f"was written; this play is still OPEN. Fix the row's contract_symbol first."
        )

    forced_note = ""
    if force:
        forced_note = f"[FORCED OVERRIDE: {str(force_reason).strip()}]"
        if held:
            forced_note += (f" (bypassed hold directive until {held.get('hold_until')}: "
                            f"\"{held.get('reason')}\")")

    if not _close_pnl_check_on():
        updated = update_play(play_id, _closure_updates(actual_pnl, notes,
                                                        forced_note=forced_note))
        return {"ok": True, "play": updated, "refused_because": None, "error": None}
    chk = _close_pnl_check(play, actual_pnl, forced=bool(force))   # KB item 4: switch on — compare with the fills
    updates = _closure_updates(chk.get("use", actual_pnl), notes, forced_note=forced_note)
    updates["close_pnl_check"] = chk["record"]
    updated = update_play(play_id, updates)
    res = {"ok": True, "play": updated, "refused_because": None, "error": None}
    if chk.get("note"):
        res["pnl_note"] = chk["note"]
    return res


def delete_play(play_id: str, reason: str = "", force: bool = False,
                deleted_by: str = "nico") -> dict:
    """
    Soft-delete a play: sets is_deleted=1, deleted_at=now, deleted_reason=reason,
    deleted_by=who ('nico' by default; 'system' from a maintenance script,
    'mason' if the assistant decided).
    Use for test/placeholder entries or plays logged by mistake — never to erase
    the history of a real, confirmed trade.

    The row stays in the options_trades table forever (readable via an explicit
    audit query); it just stops appearing in active_options_trades /
    get_open_plays() / get_all_plays() from this call onward — the same
    "nothing is truly unrecoverable" guarantee the old
    options_journal_deleted.json shadow file gave, enforced structurally.

    Refuses to delete a play with execution_verified=True (a confirmed Alpaca
    fill — see attach_execution()) unless force=True is explicitly passed, since
    that would erase the record of real money moving.

    Returns {"ok": bool, "deleted": play|None, "error": str|None}.
    """
    conn = DB.get_connection()
    try:
        play = get_play_by_id(play_id)
        if play is None:
            return {"ok": False, "deleted": None, "error": f"Play {play_id} not found in the journal."}

        flag = conn.execute("SELECT is_deleted FROM options_trades WHERE id = ?",
                            (play_id,)).fetchone()
        if flag and flag[0]:
            return {"ok": False, "deleted": None,
                    "error": f"Play {play_id} is already deleted."}

        if play.get("execution_verified") and not force:
            return {
                "ok": False,
                "deleted": None,
                "error": (
                    f"Play {play_id} ({play.get('ticker')} {play.get('strategy')}) is tied to a "
                    f"CONFIRMED Alpaca fill (order {play.get('order_id')}). Refusing to delete without "
                    f"force=true — this would erase the record of a real trade."
                ),
            }

        DB.soft_delete(conn, "options_trades", play_id,
                       reason or "no reason given", datetime.now().isoformat(),
                       deleted_by=deleted_by)
        _recompute_accuracy(_load_journal())
        return {"ok": True, "deleted": play, "error": None}
    finally:
        conn.close()


# ── Review scheduling ─────────────────────────────────────────────

def get_plays_needing_review() -> list:
    """
    Returns list of dicts: {play, review_type}
    review_type: 'expiry' | '30d' | '60d'
    Checks open and closed_early plays.
    """
    today = date.today()
    plays = _load_journal()
    needing = []

    for p in plays:
        if p.get("status") not in ("open", "closed_early"):
            continue
        exp_str = p.get("expiration")
        if not exp_str:
            continue
        try:
            exp_date = date.fromisoformat(str(exp_str))
        except ValueError:
            continue

        days_since_exp = (today - exp_date).days

        # Needs expiry review (play has expired, no review yet)
        if days_since_exp >= 0 and not p.get("expiry_review"):
            needing.append({"play": p, "review_type": "expiry"})
            continue   # don't double-queue for 30d/60d yet

        # Needs 30-day post-expiry review
        if days_since_exp >= 30 and p.get("expiry_review") and not p.get("review_30d"):
            needing.append({"play": p, "review_type": "30d"})
            continue

        # Needs 60-day post-expiry review
        if days_since_exp >= 60 and p.get("review_30d") and not p.get("review_60d"):
            needing.append({"play": p, "review_type": "60d"})

    return needing


def record_expiry_review(play_id: str, outcome: str, actual_pnl: float,
                          underlying_price: float, review_text: str,
                          thesis_score: int = None, thesis_notes: str = None,
                          position_lookup=None, today: date = None,
                          settlement_lookup=None,
                          settlement_price_lookup=None) -> dict | None:
    """Record the expiration outcome and review for a play — but ONLY after the
    broker confirms the position is actually gone.

    Added 2026-07-30 (guard-hardening pass). This function used to take an
    outcome and a P&L number and write them, full stop. The only thing standing
    in front of it was the dispatch's cross_check_play math guard, and that
    guard checks ARITHMETIC, not REALITY: "expired worthless, -$105" on a $1.05
    x 1 contract play is arithmetically perfect, so it passed, and the row was
    written as a settled loss while Alpaca had not settled anything.

    Real incident: play 37b6e5a0 (QQQ260730C00684000) was written
    status:"reviewed", actual_pnl:-105 on 2026-07-30 while the position was
    still sitting unresolved at the broker. Nico's rule, verbatim: "Mason should
    wait until Alpaca renders gains instead of estimating them. He should only
    estimate when told, however that should not be a basis of truth and
    therefore not logged."

    So there is deliberately NO force= bypass here. An estimate is allowed to be
    SPOKEN and is never allowed to be WRITTEN. If Mason wants to tell Nico what
    a play probably settled at, he can say so in chat; the journal takes
    settled facts only.

    Refuses when:
      1. the contract has not expired yet — that is not an expiry review
      2. Alpaca still shows a LIVE position on this play's contract_symbol
      3. Alpaca can't be reached (fails CLOSED, same as close_play_early)

    Rows with no contract_symbol are the pre-execution-era "idea" entries and
    are reviewed exactly as before — there is no position to ask about, and
    retro-blocking ten historical rows would break the learning loop for no
    integrity gain.

    Returns the updated play on success, or a dict
    {"ok": False, "refused_because": str, "error": str} on refusal. Callers
    that previously treated a falsy return as "not found" still behave
    correctly; the dispatch reads "refused_because" to surface the reason.
    """
    play = get_play_by_id(play_id)
    if play is None:
        return None

    symbol = (play.get("contract_symbol") or "").strip().upper()
    label  = f"{play.get('ticker')} {play.get('strategy')}"

    def _refuse(because: str, error: str) -> dict:
        _append_refusal_log({
            "refused_at": datetime.now().isoformat(),
            "action": "record_expiry_review",
            "play_id": play_id,
            "contract_symbol": symbol or None,
            "refused_because": because,
            "attempted_outcome": outcome,
            "attempted_pnl": actual_pnl,
            "error": error,
        })
        return {"ok": False, "refused_because": because, "error": error}

    if symbol:
        _today  = today or date.today()
        _parsed = parse_occ_symbol(symbol)

        # 1. not expired yet
        if _parsed.get("ok"):
            _exp = date.fromisoformat(_parsed["expiration"])
            if _exp > _today:
                return _refuse(
                    "not_yet_expired",
                    f"[NOT REVIEWED] Play {play_id} ({label}) is on {symbol}, which "
                    f"expires {_parsed['expiration']} — that is still "
                    f"{(_exp - _today).days} day(s) away. An expiry review records how "
                    f"a contract SETTLED; this one hasn't. Nothing was written. To exit "
                    f"early use close_options_play_early, which is broker-gated."
                )

        # 2/3. the broker is the only authority on whether this settled
        lookup = position_lookup or _default_position_lookup
        try:
            pos = lookup(symbol)
        except Exception as e:
            return _refuse(
                "broker_unreachable",
                f"[NOT REVIEWED] Could not reach Alpaca to confirm {symbol} has "
                f"settled for play {play_id} ({label}): {e}. Refusing to write an "
                f"outcome — a broker we can't reach is not evidence that a position "
                f"resolved. Nothing was written; this play is unchanged."
            )
        if not isinstance(pos, dict) or not pos.get("ok"):
            return _refuse(
                "broker_unreachable",
                f"[NOT REVIEWED] Alpaca returned an error checking {symbol} for play "
                f"{play_id} ({label}): "
                f"{pos.get('error') if isinstance(pos, dict) else pos}. Refusing to "
                f"write an outcome on an unconfirmed settlement. Nothing was written."
            )
        live_qty = float(pos.get("qty") or 0)
        if pos.get("exists") and live_qty > 0:
            return _refuse(
                "live_position",
                f"[NOT REVIEWED] Alpaca still shows a LIVE position of {live_qty:g} "
                f"contract(s) on {symbol} for play {play_id} ({label}). The broker has "
                f"NOT settled this yet.\n"
                f"Refusing to write outcome '{outcome}' / P&L "
                f"${float(actual_pnl):+.2f} — that number is an ESTIMATE until Alpaca "
                f"renders it, and an estimate must never become the journal's basis of "
                f"truth. This is the QQQ 37b6e5a0 shape from 2026-07-30.\n"
                f"Wait for Alpaca to settle it, then review. You may TELL Nico what you "
                f"expect it to settle at — just don't write it down. Nothing was "
                f"written; this play is unchanged."
            )

    # ── Defect 3 guard (2026-09-22): "flat" is not "settled to a win" ────────
    # The broker gate above proves the position is GONE; it does not prove HOW.
    # MSFT 5c29d5a8 was SOLD early (7/30 @ $50) and AMD 8cf33e40 EXPIRED WORTHLESS
    # (8/21, OPEXP net $0) — both flat, both then written as held-to-expiry wins.
    # Ask Alpaca how it actually left the book and force the review to match.
    # kind 'unknown' (Alpaca answered, shows no sell and no OPEXP) never blocks.
    # But if Alpaca did NOT answer (ok=False: 429 / 5xx / transport, after the
    # lookup's own retries) the check never ran — fail CLOSED, like the position
    # gate above. Until 2026-09-28 a 429 came back as ok=True/'unknown' and the
    # guard was silently skipped (hit twice during the review-backlog run).
    if symbol:
        disp_lookup = settlement_lookup or _default_settlement_lookup
        try:
            disp = disp_lookup(symbol)
        except Exception as e:
            disp = {"ok": False, "kind": "unavailable", "error": str(e)}
        if not isinstance(disp, dict) or not disp.get("ok"):
            err = (disp or {}).get("error") if isinstance(disp, dict) else None
            return _refuse(
                "broker_disposition_unavailable",
                f"[NOT REVIEWED] Could not confirm with Alpaca how {symbol} left the book "
                f"(sold early vs expired) — the broker did not answer"
                f"{': ' + str(err)[:160] if err else ''}. The settlement check never ran, "
                f"so nothing was written. This is usually a rate limit; try the review "
                f"again in a minute."
            )
        if disp.get("ok"):
            kind = disp.get("kind")
            if kind == "sold_early" and disp.get("sell_fills"):
                sf = disp["sell_fills"][-1]
                return _refuse(
                    "closed_early_not_expiry",
                    f"[NOT REVIEWED] Alpaca shows {symbol} was SOLD before expiry — "
                    f"a filled SELL of {sf.get('qty')} @ ${sf.get('price')} on "
                    f"{(sf.get('filled_at') or '')[:10]} (order "
                    f"{str(sf.get('order_id'))[:8]}). This is a CLOSED-EARLY exit, not "
                    f"an expiry settlement, so its P&L is (exit - entry) x size, NOT the "
                    f"value at expiration. Recording '{outcome}' / ${float(actual_pnl):+.2f} "
                    f"as an expiry review would repeat the MSFT 5c29d5a8 shape "
                    f"(sold 7/30, mis-booked as held to 8/21). Use the closed-early path "
                    f"with the real exit fill. Nothing was written."
                )
            if kind == "expired_worthless" and outcome != "loss":
                return _refuse(
                    "worthless_expiry_outcome_mismatch",
                    f"[NOT REVIEWED] Alpaca shows {symbol} EXPIRED WORTHLESS (OPEXP, "
                    f"net_amount $0) — the contract settled for nothing, which is a LOSS "
                    f"of the full premium. Stated outcome '{outcome}' contradicts the "
                    f"broker's own settlement record. This is the AMD 8cf33e40 shape "
                    f"(expired worthless 8/21, written as a +$11,490 win). Nothing was "
                    f"written; correct the outcome to a full-premium loss."
                )

    # ── Defect 2 guard (2026-09-22): grade against the REAL settlement price ─
    # record_expiry_review used to write whatever underlying_price/actual_pnl the
    # caller handed it. review_options_play can take underlying_price_at_close
    # straight from the LLM, and the cross_check ran against a price the LLM also
    # supplied — so a wrong-but-internally-consistent pair sailed through. Re-fetch
    # the actual close ON the expiration date and re-run the deterministic
    # cross-check against IT. Compares outcome/P&L (not the raw price), so a few
    # cents of price noise that changes nothing still passes. No data → skip
    # (cannot verify, do not block); a known-UNSAFE price (T-D10) → refuse.
    if symbol and actual_pnl is not None:
        real_price = None
        if settlement_price_lookup is None:
            # T-D10 round 2: the default path asks for a QUOTE, not just a price.
            # 'unavailable' keeps the fail-open above; 'unsafe' means a price
            # exists but grading against it could write a WRONG outcome, so the
            # review is refused. Nothing is written; the play stays in
            # get_plays_needing_review() and is picked up again on the next review.
            _open_dates = []
            for _f in (play.get("entry_date"), play.get("filled_at"), play.get("created_at")):
                try:
                    _open_dates.append(date.fromisoformat(str(_f)[:10]))
                except Exception:
                    pass
            _opened = min(_open_dates) if _open_dates else None
            q = _expiry_close_quote(play.get("ticker"), play.get("expiration"),
                                    today=today, opened=_opened)
            if q.get("status") == "unsafe":
                _why = {
                    "not_final": "the expiration date is not over yet, so its daily "
                                 "bar may still be intraday. Retry once the close has "
                                 "printed (the next day).",
                    "missing_session_bar": "the price source has no bar for a regular "
                                           "NYSE session on/near expiry, and grading off "
                                           "a neighbouring day's close could flip the "
                                           "outcome. Retry later, once the bar is filled.",
                    "split_in_contract_life": "the stock split while this contract was "
                                              "open, so the OCC adjusted its strike/"
                                              "deliverable and the journal's strike can't "
                                              "be safely compared with any price print. "
                                              "Mason will not auto-grade this play. It "
                                              "stays in pending reviews until a "
                                              "manual-settlement option exists (Director "
                                              "ruling #29).",
                    "conflicting_bars": "the price source reports two different closes "
                                        "for the settlement day, so the real settlement "
                                        "price is unknown. Retry later.",
                    "suspicious_split_data": "the price source's stock-split data for a "
                                             "date that changes this price is corrupt or "
                                             "contradictory. Retry later.",
                    "missing_split_column": "the price source returned no stock-split "
                                            "data, so a split can't be ruled out. Retry later.",
                    "bad_expiration": "this play's expiration date can't be read; the "
                                      "row needs fixing before it can be graded.",
                    "processing_error": "the price data could not be read safely. "
                                        "Retry later.",
                }.get(q.get("reason"), "the settlement price is not safe to grade against.")
                return _refuse(
                    "settlement_price_unverifiable",
                    f"[NOT REVIEWED] Could not verify the {play.get('expiration')} "
                    f"settlement close for {play.get('ticker')} (reason: "
                    f"{q.get('reason')}; {q.get('detail')}): {_why} Nothing was "
                    f"written; the play stays in the pending-review list."
                )
            real_price = q.get("price") if q.get("status") == "ok" else None
        else:
            price_lookup = settlement_price_lookup
            try:
                real_price = price_lookup(play.get("ticker"), play.get("expiration"),
                                          today=today)
            except TypeError:
                try:
                    real_price = price_lookup(play.get("ticker"), play.get("expiration"))
                except Exception:
                    real_price = None
            except Exception:
                real_price = None
        if real_price is not None:
            chk = cross_check_play(play, stated_pnl=float(actual_pnl),
                                   stated_outcome=outcome, expiry_price=float(real_price))
            if chk.get("status") == "mismatch":
                return _refuse(
                    "settlement_price_mismatch",
                    f"[NOT REVIEWED] Verified against the REAL close on "
                    f"{play.get('expiration')} (${real_price:.2f}): {chk['message']} "
                    f"The stated numbers price the trade off a different underlying value "
                    f"than the one the market actually printed on the settlement date — "
                    f"the 2026-09-22 defect, where today's spot got written as the expiry "
                    f"price. Nothing was written."
                )

    updates = {
        "status": "reviewed",
        "outcome": outcome,
        "actual_pnl": actual_pnl,
        "underlying_price_at_expiry": underlying_price,
        "expiry_review": review_text,
        "expiry_review_date": date.today().isoformat(),
    }
    if thesis_score is not None:
        updates["thesis_score"] = thesis_score
    if thesis_notes:
        updates["thesis_score_notes"] = thesis_notes
    return update_play(play_id, updates)


def record_periodic_review(play_id: str, review_type: str, review_text: str,
                            thesis_score: int = None) -> dict | None:
    """Record a 30d or 60d follow-up review."""
    key = f"review_{review_type}"
    updates = {
        key: review_text,
        f"{key}_date": date.today().isoformat(),
    }
    if thesis_score is not None:
        updates["thesis_score"] = thesis_score
    return update_play(play_id, updates)


# ── Accuracy ledger ───────────────────────────────────────────────

def _empty_accuracy() -> dict:
    """A fresh, fully-reset ledger. win_rate/avg_pnl are None (never 0 or 1.0)
    when nothing is graded — "no data" must never read as a score."""
    return {
        "total_plays": 0,
        "reviewed_plays": 0,
        "win_rate": None,
        "avg_pnl": None,
        "total_pnl": None,
        "by_strategy": {},
        "by_ticker": {},
        "confidence_calibration": {},
        "avg_thesis_score": None,
        "last_updated": None,
    }


def _recompute_accuracy(plays: list):
    """Recompute the accuracy ledger FROM SCRATCH out of the active journal.
    Called after every update.

    Fixed 2026-09-28 (knowledge audit): this used to start from _load_accuracy()
    — the PREVIOUS file — so with zero reviewed plays it saved the stale
    win_rate/avg_pnl untouched next to the new counts. Nothing from the old file
    is carried over now. Voided rows (outcome NULL — e.g. journal-only plays that
    never executed) are counted in total_plays but never in win_rate or P&L."""
    plays = [p for p in plays if not p.get("is_deleted")]
    acc = compute_accuracy(plays)
    _save_accuracy(acc)
    return acc


def compute_accuracy(plays: list) -> dict:
    """Pure aggregate math for the options ledger (no I/O). Only rows with a real
    outcome (win|loss|breakeven) count. actual_pnl is the TOTAL position P&L in
    dollars ((exit - entry premium) x 100 x contracts) — never per-contract."""
    reviewed = [p for p in plays if p.get("outcome") in ("win", "loss", "breakeven")]

    acc = _empty_accuracy()
    acc["total_plays"]    = len(plays)
    acc["reviewed_plays"] = len(reviewed)
    acc["last_updated"]   = date.today().isoformat()

    if not reviewed:
        return acc

    wins = [p for p in reviewed if p.get("outcome") == "win"]
    acc["win_rate"] = round(len(wins) / len(reviewed), 3)

    pnls = [p["actual_pnl"] for p in reviewed if p.get("actual_pnl") is not None]
    acc["avg_pnl"]   = round(sum(pnls) / len(pnls), 2) if pnls else None
    acc["total_pnl"] = round(sum(pnls), 2) if pnls else None

    # By strategy
    by_strat: dict = {}
    for p in reviewed:
        s = p.get("strategy", "unknown")
        if s not in by_strat:
            by_strat[s] = {"plays": 0, "wins": 0, "pnl_sum": 0, "pnl_count": 0}
        by_strat[s]["plays"] += 1
        if p.get("outcome") == "win":
            by_strat[s]["wins"] += 1
        if p.get("actual_pnl") is not None:
            by_strat[s]["pnl_sum"]   += p["actual_pnl"]
            by_strat[s]["pnl_count"] += 1
    for s, d in by_strat.items():
        d["win_rate"] = round(d["wins"] / d["plays"], 3)
        d["avg_pnl"]  = round(d["pnl_sum"] / d["pnl_count"], 2) if d["pnl_count"] else None
        del d["pnl_sum"], d["pnl_count"]
    acc["by_strategy"] = by_strat

    # By ticker
    by_ticker: dict = {}
    for p in reviewed:
        t = p.get("ticker", "unknown")
        if t not in by_ticker:
            by_ticker[t] = {"plays": 0, "wins": 0}
        by_ticker[t]["plays"] += 1
        if p.get("outcome") == "win":
            by_ticker[t]["wins"] += 1
    for t, d in by_ticker.items():
        d["win_rate"] = round(d["wins"] / d["plays"], 3)
    acc["by_ticker"] = by_ticker

    # Confidence calibration — bucket by 10s (e.g. "70-79")
    conf_buckets: dict = {}
    for p in reviewed:
        c = p.get("mason_confidence")
        if c is None:
            continue
        lo = (int(c) // 10) * 10
        bucket = f"{lo}-{lo+9}"
        if bucket not in conf_buckets:
            conf_buckets[bucket] = {"plays": 0, "wins": 0}
        conf_buckets[bucket]["plays"] += 1
        if p.get("outcome") == "win":
            conf_buckets[bucket]["wins"] += 1
    for b, d in conf_buckets.items():
        d["win_rate"] = round(d["wins"] / d["plays"], 3)
    acc["confidence_calibration"] = conf_buckets

    # Thesis scores
    scores = [p["thesis_score"] for p in reviewed if p.get("thesis_score") is not None]
    acc["avg_thesis_score"] = round(sum(scores) / len(scores), 1) if scores else None

    return acc


# ── P&L cross-check engine ────────────────────────────────────────

def calculate_pnl(play: dict, expiry_price: float) -> dict:
    """
    Deterministically calculate option P&L at expiry.
    No AI involved — pure math from strikes, premium, and contracts.

    Returns:
        calculated_pnl  : total dollars (positive = profit)
        outcome         : 'win' | 'loss' | 'breakeven'
        explanation     : plain-English breakdown of the calculation
        error           : set if strategy is unsupported
    """
    strategy  = play.get("strategy", "")
    strikes   = play.get("strikes", {})

    # ── NO DEFAULTING. Ever. ──────────────────────────────────────────────
    # This used to read float(play.get("premium", 0)) and
    # int(play.get("contracts", 1)). Those two defaults are defect C of the
    # 2026-07-30 orphan-backfill incident. A missing premium is not "the
    # option cost nothing" and a missing size is not "one contract" — they
    # mean WE DO NOT KNOW, and the only honest answer to a P&L question with
    # an unknown cost basis is to refuse it. SPY row 5afd580d had no premium
    # key at all; the substituted 0 made a deep-ITM long put that returned
    # +$509 compute as expired-worthless at exactly $0.00.
    #
    # Returning the same error shape the unsupported-strategy branch returns
    # (below) is deliberate: cross_check_play() already turns that shape into
    # status "unsupported" with the message passed through verbatim, so the
    # review is REFUSED and the missing field is named, with no new branch
    # needed there and no change to its $1 tolerance.
    missing = [k for k in ("premium", "contracts")
               if k not in play or play.get(k) is None]
    if missing:
        return {
            "error": (
                f"Cannot calculate P&L for play {play.get('id', '?')} "
                f"({play.get('ticker', '?')} {strategy}): "
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                f"missing from the journal row. Refusing to substitute a cost "
                f"basis — a missing premium is not $0.00 and a missing size is "
                f"not 1 contract. Fill in the real fill data from Alpaca "
                f"(filled_avg_price -> premium, filled_qty -> contracts) before "
                f"any review is written."
            ),
            "calculated_pnl": None,
            "outcome": None,
            "explanation": "",
        }

    try:
        premium   = float(play["premium"])
        contracts = int(float(play["contracts"]))
    except (TypeError, ValueError):
        return {
            "error": (
                f"Cannot calculate P&L for play {play.get('id', '?')}: premium "
                f"{play.get('premium')!r} / contracts {play.get('contracts')!r} "
                f"are not numbers. Refusing to guess."
            ),
            "calculated_pnl": None,
            "outcome": None,
            "explanation": "",
        }

    mult      = contracts * 100   # dollars per $1 of per-share P&L

    pnl_per_share = None
    explanation   = ""

    if strategy == "bull_call_spread":
        long_call  = float(strikes.get("long_call", 0))
        short_call = float(strikes.get("short_call", 0))
        if expiry_price <= long_call:
            pnl_per_share = -premium
            explanation = (f"Price ${expiry_price:.2f} at/below long call ${long_call:.2f}. "
                           f"Both calls expire worthless. Full debit of ${premium:.2f}/share lost.")
        elif expiry_price < short_call:
            sv = expiry_price - long_call
            pnl_per_share = sv - premium
            explanation = (f"Price ${expiry_price:.2f} between strikes. "
                           f"Spread value ${sv:.2f} minus debit ${premium:.2f} = {_usd(pnl_per_share)}/share.")
        else:
            sw = short_call - long_call
            pnl_per_share = sw - premium
            explanation = (f"Price ${expiry_price:.2f} above short call ${short_call:.2f}. "
                           f"Max profit: width ${sw:.2f} minus debit ${premium:.2f} = {_usd(pnl_per_share)}/share.")

    elif strategy == "bear_call_spread":
        short_call = float(strikes.get("short_call", 0))
        long_call  = float(strikes.get("long_call", 0))
        if expiry_price <= short_call:
            pnl_per_share = premium
            explanation = (f"Price ${expiry_price:.2f} at/below short call ${short_call:.2f}. "
                           f"Spread expires worthless. Full credit ${premium:.2f}/share kept.")
        elif expiry_price < long_call:
            sv = expiry_price - short_call
            pnl_per_share = premium - sv
            explanation = (f"Price ${expiry_price:.2f} between strikes. "
                           f"Short call worth ${sv:.2f}. Credit ${premium:.2f} minus ${sv:.2f} = {_usd(pnl_per_share)}/share.")
        else:
            sw = long_call - short_call
            pnl_per_share = premium - sw
            explanation = (f"Price ${expiry_price:.2f} above long call ${long_call:.2f}. "
                           f"Max loss: width ${sw:.2f} minus credit ${premium:.2f} = {_usd(pnl_per_share)}/share.")

    elif strategy == "cash_secured_put":
        short_put = float(strikes.get("short_put", 0))
        if expiry_price >= short_put:
            pnl_per_share = premium
            explanation = (f"Price ${expiry_price:.2f} at/above short put ${short_put:.2f}. "
                           f"Put expires worthless. Full premium ${premium:.2f}/share kept.")
        else:
            intrinsic     = short_put - expiry_price
            pnl_per_share = premium - intrinsic
            explanation = (f"Price ${expiry_price:.2f} below short put ${short_put:.2f}. "
                           f"Put worth ${intrinsic:.2f}. Premium ${premium:.2f} minus ${intrinsic:.2f} = {_usd(pnl_per_share)}/share.")

    elif strategy == "covered_call":
        short_call = float(strikes.get("short_call", 0))
        # P&L on just the option leg (not the stock position)
        if expiry_price <= short_call:
            pnl_per_share = premium
            explanation = (f"Price ${expiry_price:.2f} at/below short call ${short_call:.2f}. "
                           f"Call expires worthless. Full premium ${premium:.2f}/share kept.")
        else:
            pnl_per_share = premium - (expiry_price - short_call)
            explanation = (f"Price ${expiry_price:.2f} above short call ${short_call:.2f}. "
                           f"Call assigned. Option P&L: {_usd(pnl_per_share)}/share (stock gain capped at strike).")

    elif strategy == "long_call":
        long_call = float(strikes.get("long_call", 0))
        if expiry_price <= long_call:
            pnl_per_share = -premium
            explanation = (f"Price ${expiry_price:.2f} at/below strike ${long_call:.2f}. "
                           f"Call expires worthless. Full debit ${premium:.2f}/share lost.")
        else:
            intrinsic     = expiry_price - long_call
            pnl_per_share = intrinsic - premium
            explanation = (f"Price ${expiry_price:.2f} above strike ${long_call:.2f}. "
                           f"Intrinsic ${intrinsic:.2f} minus premium ${premium:.2f} = {_usd(pnl_per_share)}/share.")

    elif strategy == "long_put":
        long_put = float(strikes.get("long_put", 0))
        if expiry_price >= long_put:
            pnl_per_share = -premium
            explanation = (f"Price ${expiry_price:.2f} at/above strike ${long_put:.2f}. "
                           f"Put expires worthless. Full debit ${premium:.2f}/share lost.")
        else:
            intrinsic     = long_put - expiry_price
            pnl_per_share = intrinsic - premium
            explanation = (f"Price ${expiry_price:.2f} below strike ${long_put:.2f}. "
                           f"Intrinsic ${intrinsic:.2f} minus premium ${premium:.2f} = {_usd(pnl_per_share)}/share.")

    elif strategy == "bear_put_spread":
        long_put  = float(strikes.get("long_put", 0))
        short_put = float(strikes.get("short_put", 0))
        if expiry_price >= long_put:
            pnl_per_share = -premium
            explanation = (f"Price ${expiry_price:.2f} at/above long put ${long_put:.2f}. "
                           f"Both puts expire worthless. Full debit ${premium:.2f}/share lost.")
        elif expiry_price > short_put:
            sv = long_put - expiry_price
            pnl_per_share = sv - premium
            explanation = (f"Price ${expiry_price:.2f} between strikes. "
                           f"Spread value ${sv:.2f} minus debit ${premium:.2f} = {_usd(pnl_per_share)}/share.")
        else:
            sw = long_put - short_put
            pnl_per_share = sw - premium
            explanation = (f"Price ${expiry_price:.2f} at/below short put ${short_put:.2f}. "
                           f"Max profit: width ${sw:.2f} minus debit ${premium:.2f} = {_usd(pnl_per_share)}/share.")

    if pnl_per_share is None:
        return {
            "error": f"Strategy '{strategy}' is not supported by the cross-check engine.",
            "calculated_pnl": None,
            "outcome": None,
            "explanation": "",
        }

    total_pnl = round(pnl_per_share * mult, 2)
    if total_pnl > 0.50:
        outcome = "win"
    elif total_pnl < -0.50:
        outcome = "loss"
    else:
        outcome = "breakeven"

    return {
        "calculated_pnl": total_pnl,
        "outcome": outcome,
        "explanation": explanation,
        "pnl_per_share": round(pnl_per_share, 4),
        "contracts": contracts,
        "error": None,
    }


def cross_check_play(play: dict, stated_pnl: float | None,
                     stated_outcome: str | None, expiry_price: float) -> dict:
    """
    Compare a stated P&L/outcome against deterministic math.

    Returns:
        status : 'ok' | 'mismatch' | 'unsupported'
        message: human-readable summary for Mason to report
        + all calculated fields for Mason to use when re-reviewing
    """
    calc = calculate_pnl(play, expiry_price)

    if calc.get("error"):
        return {
            "status": "unsupported",
            "message": calc["error"],
            "calculated_pnl": None,
            "calculated_outcome": None,
        }

    cp  = calc["calculated_pnl"]
    co  = calc["outcome"]
    exp = calc["explanation"]

    # Mismatch if: outcome differs OR P&L differs by more than $1 (rounding tolerance)
    pnl_ok     = stated_pnl is None or abs(cp - stated_pnl) <= 1.0
    outcome_ok = stated_outcome is None or stated_outcome == co

    if pnl_ok and outcome_ok:
        return {
            "status": "ok",
            "calculated_pnl": cp,
            "calculated_outcome": co,
            "expiry_price": expiry_price,
            "explanation": exp,
            "message": f"Cross-check PASSED. Calculated: {co}, ${cp:+.2f}. {exp}",
        }

    parts = []
    if not outcome_ok:
        parts.append(f"outcome: stated '{stated_outcome}' but math says '{co}'")
    if not pnl_ok:
        parts.append(f"P&L: stated ${stated_pnl:+.2f} but math says ${cp:+.2f}")

    return {
        "status": "mismatch",
        "calculated_pnl": cp,
        "calculated_outcome": co,
        "stated_pnl": stated_pnl,
        "stated_outcome": stated_outcome,
        "expiry_price": expiry_price,
        "explanation": exp,
        "message": (
            f"CROSS-CHECK FAILED — {'; '.join(parts)}. "
            f"Math: {exp} "
            f"Do NOT write this review until Nico confirms the correct numbers."
        ),
    }


# ── Boot-time corruption detector (advisory only) ─────────────────
# Added 2026-07-30. A write-time guard cannot catch a row written BEFORE the
# guard existed — which is exactly how the four orphan-backfill rows survived.
# So the guards get a companion that reads the whole book at boot and says what
# it sees. It is ADVISORY: it prints, it does not repair, it does not close, it
# does not block boot. The correct response to detected corruption is a human
# looking at it — auto-repair is how a small data problem becomes a large one.

def detect_journal_anomalies(plays: list = None, today: date = None) -> dict:
    """Scan the journal for the four shapes of corruption seen on 2026-07-30.
    Pure apart from an optional read: pass `plays` to scan an in-memory book,
    `today` to pin the clock (rules 3 is date-relative and must be testable).

    Detects:
      1. duplicate_position — a contract_symbol present in BOTH an open row and
         a closed/reviewed row. The MSFT 276b1ea0 vs 5c29d5a8 double-count.
      2. missing_cost_basis — an execution_verified row with no premium or no
         contracts. Defects A/B: the backfill path never wrote either key, and
         calculate_pnl() then read the absence as $0.00.
      3. expired_still_open — a row still status "open" whose expiration is
         already in the past. Nothing ever resolved it, and the book is showing
         risk that is not on. (This rule shipped inverted on 2026-07-30 and
         could never fire; see the comment at its implementation.)
      4. duplicate_order_id — one order id on more than one row, in any status.
         The MSFT 9f41458a/5c29d5a8 twin, which rule 1 cannot see because both
         rows were closed.

    Returns {"ok": bool, "counts": {...}, "findings": [ {kind, severity,
    play_ids, contract_symbol, message} ], "summary": str}. Never raises —
    a detector that can crash the boot sequence is worse than no detector.
    """
    findings = []
    try:
        rows = plays if plays is not None else _load_journal()

        # 1. same contract on both sides of the book
        by_symbol = {}
        for r in rows:
            cs = (r.get("contract_symbol") or "").strip().upper()
            if cs:
                by_symbol.setdefault(cs, []).append(r)
        for cs, group in sorted(by_symbol.items()):
            opens  = [r for r in group if r.get("status") == "open"]
            closed = [r for r in group if r.get("status") in
                      ("closed_early", "expired", "reviewed")]
            if opens and closed:
                findings.append({
                    "kind": "duplicate_position",
                    "severity": "high",
                    "contract_symbol": cs,
                    "play_ids": [r.get("id") for r in opens + closed],
                    "message": (
                        f"{cs} appears in BOTH an open row and a closed row: "
                        + ", ".join(f"{r.get('id')}({r.get('status')}, "
                                    f"{r.get('filled_qty')}@${r.get('filled_avg_price')})"
                                    for r in opens + closed)
                        + ". One position is being counted twice. This is the MSFT "
                          "276b1ea0 shape — a CLOSING fill written as a new open play."
                    ),
                })

        # 2. verified with no cost basis
        for r in rows:
            if not r.get("execution_verified"):
                continue
            missing = [k for k in ("premium", "contracts")
                       if k not in r or r.get(k) is None]
            if missing:
                findings.append({
                    "kind": "missing_cost_basis",
                    "severity": "high",
                    "contract_symbol": r.get("contract_symbol"),
                    "play_ids": [r.get("id")],
                    "message": (
                        f"{r.get('id')} ({r.get('ticker')} {r.get('contract_symbol')}) is "
                        f"execution_verified but has no {' and no '.join(missing)}. Its "
                        f"P&L cannot be computed and any review of it will be refused. "
                        f"Real fill on the row: {r.get('filled_qty')} @ "
                        f"${r.get('filled_avg_price')} — run repair_orphan_backfills.py."
                    ),
                })

        # 3. open past its own expiration
        #
        # CORRECTED 2026-07-30. This rule shipped inverted and could never fire.
        # It compared filled_at > expiration — "you bought it after it expired" —
        # which is essentially impossible, so the detector reported "clean" for
        # the entire window in which three already-expired contracts sat in the
        # book marked open. (The spec it was built from was worse: "filled_at
        # older than expiration" describes every healthy play ever opened.)
        # The only question worth asking is whether it is open TODAY and its
        # expiry is in the PAST.
        #
        # Expiry is read from the OCC contract_symbol when parseable — ground
        # truth — and falls back to the model-typed "expiration" field only when
        # there is no symbol to read.
        today = today or date.today()
        for r in rows:
            if r.get("status") != "open":
                continue
            parsed = parse_occ_symbol(r.get("contract_symbol") or "")
            if parsed.get("ok"):
                exp, src = parsed["expiration"], "contract symbol"
            else:
                exp, src = (str(r.get("expiration") or "")[:10] or None), "expiration field"
            if not exp:
                continue
            try:
                exp_date = date.fromisoformat(exp)
            except (ValueError, TypeError):
                continue
            if exp_date < today:
                findings.append({
                    "kind": "expired_still_open",
                    "severity": "high",
                    "contract_symbol": r.get("contract_symbol"),
                    "play_ids": [r.get("id")],
                    "message": (
                        f"{r.get('id')} ({r.get('ticker')} {r.get('contract_symbol')}) is "
                        f"still status 'open' but expired {exp} — "
                        f"{(today - exp_date).days} day(s) ago (per its {src}). That "
                        f"position does not exist any more; the book is carrying risk "
                        f"that is not on. Either it settled and was never reviewed, or "
                        f"it is a stale orphan backfill."
                    ),
                })

        # 4. one order id on more than one row
        #
        # ADDED 2026-07-30. An order id identifies exactly one fill, and one
        # fill belongs to exactly one play — in any status. This is the rule
        # that would have caught 9f41458a, the MSFT twin of 5c29d5a8 sharing
        # order a8b9e97c, which rule 1 could not see because BOTH rows were
        # closed and rule 1 only looks for open-vs-closed collisions.
        by_order = {}
        for r in rows:
            for key in ("order_id", "exit_order_id"):
                oid = (str(r.get(key) or "")).strip()
                if oid:
                    by_order.setdefault(oid, []).append((r, key))
        for oid, group in sorted(by_order.items()):
            if len(group) < 2:
                continue
            pnls = [float(r.get("actual_pnl") or 0) for r, _ in group]
            findings.append({
                "kind": "duplicate_order_id",
                "severity": "high",
                "contract_symbol": group[0][0].get("contract_symbol"),
                "play_ids": [r.get("id") for r, _ in group],
                "message": (
                    f"Order {oid} appears on {len(group)} rows: "
                    + ", ".join(f"{r.get('id')}({r.get('status')}, as {k})"
                                for r, k in group)
                    + f". One order is one fill and belongs to exactly one play. "
                      f"Combined P&L booked against this single order: "
                      f"${sum(pnls):+.2f}. This is the MSFT 9f41458a shape — the "
                      f"same $5,000 trade counted twice."
                ),
            })
    except Exception as e:                       # never break the boot sequence
        return {"ok": False, "counts": {}, "findings": [],
                "summary": f"[journal-check] detector itself failed: {type(e).__name__}: {e}"}

    counts = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1

    if not findings:
        summary = ("[journal-check] clean — no duplicate positions, no duplicate order ids, "
                   "no missing cost basis, no expired-but-open rows.")
    else:
        bits = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()))
        summary = (f"[journal-check] {len(findings)} ANOMALY(S) FOUND ({bits}). "
                   f"Advisory only — nothing was changed. Nico should look at these.")

    return {"ok": not findings, "counts": counts,
            "findings": findings, "summary": summary}


def format_journal_anomalies(report: dict = None) -> str:
    """The detector's findings as a block of text for a console log or chat."""
    report = report or detect_journal_anomalies()
    lines = [report["summary"]]
    for f in report.get("findings", []):
        lines.append(f"  [{f['severity'].upper()}] {f['kind']}: {f['message']}")
    return "\n".join(lines)


def get_accuracy_summary() -> str:
    """
    Human-readable accuracy summary for Mason to speak/read.
    Includes calibration data so Mason knows where its edge is weak.
    """
    acc  = _load_accuracy()
    plays = _load_journal()
    open_plays   = [p for p in plays if p.get("status") == "open"]
    pending_rev  = get_plays_needing_review()

    if acc["reviewed_plays"] == 0:
        return (
            f"No reviewed plays yet. "
            f"{acc['total_plays']} total logged, {len(open_plays)} currently open."
        )

    lines = []
    wr = round(acc["win_rate"] * 100) if acc["win_rate"] is not None else "N/A"
    lines.append(f"Track record: {acc['reviewed_plays']} reviewed plays, {wr}% win rate.")

    if acc.get("avg_pnl") is not None:
        lines.append(f"Average P&L per trade: {_usd(acc['avg_pnl'], signed=True)}.")

    if acc.get("by_strategy"):
        parts = []
        for strat, d in sorted(acc["by_strategy"].items(), key=lambda x: -x[1]["plays"]):
            wr_s = round(d["win_rate"] * 100)
            parts.append(f"{strat} ({d['plays']} plays, {wr_s}% win rate)")
        lines.append("By strategy: " + "; ".join(parts) + ".")

    if acc.get("confidence_calibration"):
        lines.append("Confidence calibration (Mason said X%, actually won Y%):")
        for bucket, d in sorted(acc["confidence_calibration"].items()):
            wr_b = round(d["win_rate"] * 100)
            lines.append(f"  {bucket}% confidence → {wr_b}% win rate ({d['plays']} plays)")

    if acc.get("avg_thesis_score") is not None:
        lines.append(f"Average thesis quality score: {acc['avg_thesis_score']}/100.")

    lines.append(f"Open plays: {len(open_plays)}. Plays pending review: {len(pending_rev)}.")
    return "\n".join(lines)


def format_open_plays() -> str:
    """Format open plays for Mason to read aloud or display in chat."""
    plays = get_open_plays()
    if not plays:
        return "No open options plays in the journal."
    today = date.today()
    lines = [f"{len(plays)} open play(s):"]
    for p in plays:
        exp = p.get("expiration", "?")
        try:
            days_to_exp = (date.fromisoformat(str(exp)) - today).days
            exp_str = f"expires {exp} ({days_to_exp}d)"
        except Exception:
            exp_str = f"expires {exp}"
        conf = p.get("mason_confidence", "?")
        # premium is now a DECLARED key defaulting to None (fix C1), so a plain
        # .get("premium", "?") would print the literal "None" on a row with no
        # cost basis. An unknown cost basis must read as unknown, not as a value.
        premium = p.get("premium")
        if premium is None and p.get("execution_verified") and p.get("filled_avg_price") is not None:
            premium = p.get("filled_avg_price")      # auto-placed plays: cost = the verified fill
        premium = "?" if premium is None else premium
        ptype   = p.get("premium_type", "")
        sym     = p.get("contract_symbol")
        _rec = p.get("director_rec_id")          # extra_json keys are flattened onto the play
        _owner = f" | PLACED BY MASON (director rec {_rec}), not by Nico" if _rec else ""
        _qty = p.get("contracts") or p.get("filled_qty")
        lines.append(
            f"  [{p['id']}] {p.get('ticker','?')} {p.get('strategy','?')}"
            f"{f' {sym}' if sym else ''} | "
            f"{exp_str} | ${premium} {ptype}{f' x{_qty:g}' if isinstance(_qty, (int, float)) else ''}"
            f" | confidence {conf}%{_owner}"
        )
        # Surface any standing hold instruction every single time the book is
        # read — the whole point of fix 3 is that this can never again be
        # something Mason has to remember from conversation context.
        d = p.get("hold_directive")
        if isinstance(d, dict) and d.get("hold_until"):
            try:
                active = today < date.fromisoformat(str(d["hold_until"])[:10])
            except (ValueError, TypeError):
                active = False
            tag = "HOLD (ACTIVE)" if active else "hold directive (expired)"
            lines.append(
                f"      >> {tag} until {d['hold_until']} — set by "
                f"{d.get('set_by', 'nico')}: \"{d.get('reason', '')}\""
                + ("  DO NOT CLOSE before then." if active else "")
            )
    return "\n".join(lines)


def format_pending_reviews() -> str:
    """Format plays needing review for Mason to report."""
    pending = get_plays_needing_review()
    if not pending:
        return "No plays currently need review."
    lines = [f"{len(pending)} play(s) need review:"]
    for item in pending:
        p  = item["play"]
        rt = item["review_type"]
        lines.append(
            f"  [{p['id']}] {p.get('ticker','?')} {p.get('strategy','?')} — "
            f"{rt} review due | expired {p.get('expiration','?')}"
        )
    return "\n".join(lines)
