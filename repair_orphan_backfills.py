#!/usr/bin/env python3
"""
repair_orphan_backfills.py — one-off, idempotent repair of the four journal rows
corrupted by the orphan-fill backfill path on 2026-07-28 / 2026-07-30.

Run this from the machine that owns options_journal.json.

WHAT WENT WRONG
---------------
Three separate defects, all in the backfill_verified_option_play path:

  A. The dispatch never read verified["side"]. mason_execution.verify_and_backfill_order()
     has always returned it (mason_execution.py:148) and NOTHING in the repo ever
     consumed it. So a CLOSING (sell) fill got written as a brand-new OPEN play,
     carrying the EXIT price as the ENTRY price.

  B. The dispatch's play_data never carried `premium` or `contracts`, and
     log_play()'s defaults dict never declared either key. The rows do not have a
     null cost basis — the keys are ABSENT.

  C. calculate_pnl() silently substituted zero: float(play.get("premium", 0)).
     A missing premium therefore read as "the option cost nothing". A long put
     with no cost basis computed as expired-worthless with P&L exactly $0.00.

Defect C is why review_options_play blocked on the SPY row: calculate_pnl returned
0, cross_check_play compared it against the stated +$509, blew the $1.00 tolerance,
and refused the write. THAT REFUSAL WAS CORRECT. The guard is healthy; the data
feeding it is not. This script fixes the data. It does not touch the guard.

TARGET END STATE
----------------
  9fdc2269  XLE260807P00058000   -> cost basis only ($0.85 x 1). Status untouched.
  8cf33e40  AMD260821C00500000   -> cost basis only ($61.55 x 2). Status untouched.
  5afd580d  SPY260729P00737000   -> cost basis $0.68 x 1 (the BUY, never the exit),
                                    then closed_early / win / +$509 with exit fields.
  276b1ea0  MSFT260821C00400000  -> DELETED via delete_play(). Phantom duplicate:
                                    it is the SELL side of 5c29d5a8, which is
                                    already correctly closed_early at +$5,000.

POST-REPAIR ASSERTIONS (all enforced, --apply aborts the run if any fails)
  1. zero rows missing premium or contracts
  2. no contract_symbol appears in both an open row and a closed/reviewed row

BROKER VERIFICATION — READ THIS
-------------------------------
This script CANNOT reach Alpaca from the environment it was authored in (the
sandbox is default-deny; paper-api.alpaca.markets returns 403 at the tunnel).
Every number below therefore comes from one of two sources, and the script says
which, out loud, on every run:

  BROKER-VERIFIED  — already present in the row's own execution_verified fields,
                     written there by a real Alpaca order lookup at the time.
                     filled_avg_price and filled_qty for all four rows are this.

  OPERATOR-ASSERTED — Nico's confirmed statement, recorded in the row's own notes
                     field. The SPY exit ($5.77, order a0e7c7bc) is this and ONLY
                     this. It has NOT been re-pulled from Alpaca by this script.

The P&L is never taken as a given. $509 is RE-DERIVED from the two prices:
(5.77 - 0.68) * 1 * 100 = 509.00. If Nico's asserted exit price is wrong, the
derived P&L is wrong with it, visibly, rather than a hardcoded 509 papering over
the disagreement. Pass --verify-exit to re-pull order a0e7c7bc from a live Alpaca
and have the script check the assertion for you before it writes anything.

USAGE
-----
    python repair_orphan_backfills.py                 # DRY RUN (default): diff only
    python repair_orphan_backfills.py --verify-exit   # dry run + live Alpaca check
    python repair_orphan_backfills.py --apply         # snapshot, then write
    python repair_orphan_backfills.py --apply --verify-exit

Idempotent: run it twice and the second run reports "already repaired" and writes
nothing. Safe to dry-run as often as you like.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mason_options as MO   # noqa: E402


# ── the facts this repair depends on ─────────────────────────────────────────

# Rows whose cost basis was never written. Every one of these is
# status=open + execution_verified=true, and every one is missing BOTH keys.
COST_BASIS_ROWS = ["9fdc2269", "8cf33e40", "5afd580d", "276b1ea0"]

# The SPY 0DTE. Entry is broker-verified on the row already ($0.68, 1 contract).
SPY_ROW_ID        = "5afd580d"
SPY_EXIT_ORDER_ID = "a0e7c7bc-a523-471d-b0fb-73963e87981d"   # OPERATOR-ASSERTED
SPY_EXIT_PRICE    = 5.77                                      # OPERATOR-ASSERTED
SPY_EXIT_QTY      = 1.0                                       # OPERATOR-ASSERTED

# The MSFT phantom. 276b1ea0's "entry" of $50.00 is in fact the CLOSING fill of
# the position recorded by 5c29d5a8, which is already closed_early at +$5,000.
MSFT_PHANTOM_ID  = "276b1ea0"
MSFT_CORRECT_ID  = "5c29d5a8"
MSFT_SYMBOL      = "MSFT260821C00400000"

SNAPSHOT_DIR = Path(__file__).parent / "_backups"


class Abort(RuntimeError):
    """Reality disagrees with the repair's assumptions. Nothing is written."""


# ── broker access (injectable — mocked in tests, never called at import) ─────

def _live_get_order(order_id: str) -> dict:
    """Real Alpaca lookup, reusing the one ground-truth path the rest of the
    codebase already uses. Only ever called under --verify-exit."""
    import mason_execution
    return mason_execution.verify_and_backfill_order(order_id)


# ── pure helpers ─────────────────────────────────────────────────────────────

def derive_pnl(entry_price: float, exit_price: float, qty: float) -> float:
    """The ONLY place a P&L number is produced. Never trust a stated figure —
    an option's P&L is fully determined by two fills and a contract count."""
    return round((float(exit_price) - float(entry_price)) * float(qty) * 100.0, 2)


def _missing(row: dict, key: str) -> bool:
    """True when a key is absent OR null. Both count as 'no value' — and only
    these two states are ever overwritten. A real number is never clobbered."""
    return key not in row or row.get(key) is None


def verify_exit_against_alpaca(get_order=None) -> dict:
    """Optional: re-pull the SPY exit order from a live Alpaca and check it
    against the operator-asserted numbers. Returns a report dict; raises Abort
    only on a genuine contradiction, not on unreachability (unreachable just
    downgrades the claim back to operator-asserted, which is where it started)."""
    get_order = get_order or _live_get_order
    print(f"\n  Re-pulling exit order {SPY_EXIT_ORDER_ID} from Alpaca...")
    try:
        o = get_order(SPY_EXIT_ORDER_ID)
    except Exception as e:
        print(f"    UNREACHABLE ({e}). Exit numbers remain OPERATOR-ASSERTED.")
        return {"verified": False, "reason": f"unreachable: {e}"}
    if not isinstance(o, dict) or not o.get("ok"):
        err = o.get("error") if isinstance(o, dict) else o
        print(f"    Alpaca could not confirm the order: {err}")
        print(f"    Exit numbers remain OPERATOR-ASSERTED.")
        return {"verified": False, "reason": str(err)}

    problems = []
    if (o.get("contract_symbol") or "").upper() != "SPY260729P00737000":
        problems.append(f"contract {o.get('contract_symbol')} is not SPY260729P00737000")
    if not str(o.get("side") or "").lower().startswith("sell"):
        problems.append(f"side is {o.get('side')!r}, not a sell — this is not an exit")
    if o.get("filled_avg_price") is not None and abs(float(o["filled_avg_price"]) - SPY_EXIT_PRICE) > 0.005:
        problems.append(f"fill price ${o['filled_avg_price']} != asserted ${SPY_EXIT_PRICE}")
    if o.get("filled_qty") is not None and abs(float(o["filled_qty"]) - SPY_EXIT_QTY) > 1e-9:
        problems.append(f"qty {o['filled_qty']} != asserted {SPY_EXIT_QTY}")

    if problems:
        raise Abort("the live exit order CONTRADICTS the operator-asserted numbers:\n  - "
                    + "\n  - ".join(problems)
                    + "\nNothing was written. Reconcile this by hand before re-running.")

    print(f"    CONFIRMED by Alpaca: {o.get('contract_symbol')} "
          f"{o.get('filled_qty')} @ ${o.get('filled_avg_price')} side={o.get('side')}")
    return {"verified": True, "order": o}


# ── planning ─────────────────────────────────────────────────────────────────

def plan_repair(rows: list) -> dict:
    """Compute every change without touching disk. Returns
    {"updates": {row_id: {field: (before, after)}}, "deletes": [...],
     "already_clean": bool, "notes": [...]}"""
    by_id = {r.get("id"): r for r in rows}
    updates: dict = {}
    deletes: list = []
    notes:   list = []

    for rid in COST_BASIS_ROWS:
        row = by_id.get(rid)
        if row is None:
            notes.append(f"{rid}: NOT PRESENT in the journal — nothing to repair "
                         f"(already deleted, or this repair already ran).")
            continue

        change: dict = {}

        # ── cost basis, and ONLY where the value is genuinely absent/null ──
        fap  = row.get("filled_avg_price")
        fqty = row.get("filled_qty")
        if _missing(row, "premium"):
            if fap is None:
                raise Abort(f"{rid} has no filled_avg_price to source a premium from. "
                            f"Refusing to invent a cost basis.")
            change["premium"] = (row.get("premium", "<ABSENT>"), float(fap))
        if _missing(row, "contracts"):
            if fqty is None:
                raise Abort(f"{rid} has no filled_qty to source a contract count from. "
                            f"Refusing to invent a size.")
            change["contracts"] = (row.get("contracts", "<ABSENT>"), int(float(fqty)))
        if "underlying_price_at_entry" not in row:
            # Declared so the key exists, left NULL on purpose. Never defaulted to
            # the strike — strike-as-underlying is exactly what made the SPY put
            # compute as precisely worthless.
            #
            # Keyed on ABSENCE only, not on _missing(): the target value here IS
            # None, so testing "absent or null" would make this a permanent no-op
            # change that never settles and breaks the "already repaired" report.
            change["underlying_price_at_entry"] = ("<ABSENT>", None)

        # ── the SPY close ─────────────────────────────────────────────────
        if rid == SPY_ROW_ID:
            entry = float(fap)
            if abs(entry - 0.68) > 0.005:
                raise Abort(f"{SPY_ROW_ID}'s broker-verified entry is ${entry}, not $0.68. "
                            f"The repair's assumptions no longer match the row. Nothing written.")
            pnl = derive_pnl(entry, SPY_EXIT_PRICE, SPY_EXIT_QTY)
            if row.get("status") != "closed_early":
                change["status"]  = (row.get("status"), "closed_early")
                change["outcome"] = (row.get("outcome"), "win" if pnl > 0 else
                                                          ("loss" if pnl < 0 else "breakeven"))
                change["actual_pnl"] = (row.get("actual_pnl"), pnl)
                change["exit_order_id"]        = (row.get("exit_order_id", "<ABSENT>"),
                                                  SPY_EXIT_ORDER_ID)
                # Not known and NOT invented. Alpaca was never re-pulled for it.
                change["exit_client_order_id"] = (row.get("exit_client_order_id", "<ABSENT>"), None)
                change["exit_filled_qty"]      = (row.get("exit_filled_qty", "<ABSENT>"),
                                                  SPY_EXIT_QTY)
                change["exit_filled_avg_price"] = (row.get("exit_filled_avg_price", "<ABSENT>"),
                                                   SPY_EXIT_PRICE)
                change["exit_filled_at"]       = (row.get("exit_filled_at", "<ABSENT>"), None)
                # Same shape close_play_early() writes, so the auto-review daemon
                # does not immediately re-queue an expired-but-unreviewed row.
                change["expiry_review"] = (
                    row.get("expiry_review"),
                    f"Closed early. Repaired by repair_orphan_backfills.py on "
                    f"{date_stamp()}. Entry ${entry:.2f} (broker-verified on this row) "
                    f"-> exit ${SPY_EXIT_PRICE:.2f} on order {SPY_EXIT_ORDER_ID} "
                    f"(OPERATOR-ASSERTED from Nico, not re-pulled from Alpaca). "
                    f"P&L re-derived, not copied: "
                    f"({SPY_EXIT_PRICE} - {entry}) x {SPY_EXIT_QTY:g} x 100 = ${pnl:+.2f}. "
                    f"exit_filled_at is null because the exact fill timestamp was never "
                    f"captured — it is not guessed here."
                )
                change["expiry_review_date"] = (row.get("expiry_review_date"), date_stamp())
                notes.append(f"{SPY_ROW_ID}: P&L ${pnl:+.2f} is RE-DERIVED from "
                             f"({SPY_EXIT_PRICE} - {entry}) x {SPY_EXIT_QTY:g} x 100, "
                             f"not copied from the stated figure.")
            else:
                notes.append(f"{SPY_ROW_ID}: already closed_early — close step skipped.")

        # ── the MSFT phantom ──────────────────────────────────────────────
        if rid == MSFT_PHANTOM_ID:
            correct = by_id.get(MSFT_CORRECT_ID)
            if correct is None:
                raise Abort(f"{MSFT_CORRECT_ID} (the CORRECT MSFT record) is not in the "
                            f"journal. Refusing to delete {MSFT_PHANTOM_ID} — that would "
                            f"erase the only remaining trace of this position.")
            if correct.get("status") != "closed_early":
                raise Abort(f"{MSFT_CORRECT_ID} is status={correct.get('status')!r}, not "
                            f"'closed_early'. The repair assumed it was already correctly "
                            f"closed. Nothing written.")
            # Recorded on the row before it is archived, so the copy that lands in
            # options_journal_deleted.json is not silently misleading about what
            # its $50.00 actually was.
            change["repair_note"] = (
                row.get("repair_note", "<ABSENT>"),
                f"PHANTOM ROW. filled_avg_price ${row.get('filled_avg_price')} is a "
                f"SELL (closing) fill, NOT an entry price — it is the exit of the "
                f"position recorded by {MSFT_CORRECT_ID}. The `premium` written here "
                f"by the repair is therefore that same exit price and must NOT be read "
                f"as a cost basis. Row deleted {date_stamp()}."
            )
            deletes.append({
                "id": rid,
                "reason": (
                    f"Phantom duplicate created 2026-07-30 by backfill_verified_option_play "
                    f"on the SELL order {row.get('order_id')}. The dispatch never read "
                    f"verified['side'], so a closing fill was written as a new OPEN play "
                    f"with the exit price ($" + f"{row.get('filled_avg_price')}) as the entry. "
                    f"The correct record for {MSFT_SYMBOL} is {MSFT_CORRECT_ID}, already "
                    f"closed_early with actual_pnl "
                    f"{correct.get('actual_pnl')}. Deleted by repair_orphan_backfills.py."
                ),
            })

        if change:
            updates[rid] = change

    already_clean = not updates and not deletes
    return {"updates": updates, "deletes": deletes,
            "already_clean": already_clean, "notes": notes}


def date_stamp() -> str:
    return datetime.now().date().isoformat()


# ── reporting ────────────────────────────────────────────────────────────────

def print_plan(plan: dict, rows: list):
    by_id = {r.get("id"): r for r in rows}
    print("\n" + "=" * 76)
    print("PLANNED CHANGES — full before/after, per row")
    print("=" * 76)

    if plan["already_clean"]:
        print("\n  Nothing to do. The journal is already repaired.")

    for rid, change in plan["updates"].items():
        row = by_id.get(rid, {})
        print(f"\n  {rid}  {row.get('ticker')} {row.get('strategy')} "
              f"{row.get('contract_symbol')}")
        print(f"    (broker-verified on this row: filled_qty={row.get('filled_qty')} "
              f"@ ${row.get('filled_avg_price')})")
        width = max(len(f) for f in change) if change else 0
        for field, (before, after) in change.items():
            b = "<ABSENT>" if before == "<ABSENT>" else repr(before)
            a = repr(after)
            if len(str(a)) > 90:
                print(f"      {field:<{width}}  {b}")
                print(f"      {'':<{width}}    ->  {str(a)[:300]}...")
            else:
                print(f"      {field:<{width}}  {b}  ->  {a}")

    for d in plan["deletes"]:
        row = by_id.get(d["id"], {})
        print(f"\n  {d['id']}  {row.get('ticker')} {row.get('strategy')} "
              f"{row.get('contract_symbol')}")
        print(f"      DELETE  (execution_verified={row.get('execution_verified')} "
              f"-> force=True required, preserved in options_journal_deleted.json)")
        print(f"      reason: {d['reason']}")

    if plan["notes"]:
        print("\n  Notes:")
        for n in plan["notes"]:
            print(f"    - {n}")

    print("\n  Rows explicitly left ALONE (status not inferred — they may genuinely "
          "still be open):")
    for rid in ("9fdc2269", "8cf33e40"):
        row = by_id.get(rid)
        if row:
            print(f"    {rid}  {row.get('ticker')} {row.get('contract_symbol')}  "
                  f"status={row.get('status')}  expiration={row.get('expiration')}  "
                  f"-> cost basis only, status untouched")


# ── assertions ───────────────────────────────────────────────────────────────

def assert_end_state(rows: list) -> list:
    """The two post-repair invariants. Returns a list of failure strings."""
    fails = []

    for r in rows:
        for key in ("premium", "contracts"):
            if _missing(r, key):
                fails.append(f"row {r.get('id')} ({r.get('contract_symbol')}) "
                             f"still missing {key}")

    from collections import defaultdict
    by_symbol = defaultdict(list)
    for r in rows:
        cs = (r.get("contract_symbol") or "").strip().upper()
        if cs:
            by_symbol[cs].append((r.get("id"), r.get("status")))
    for cs, entries in by_symbol.items():
        statuses = {s for _, s in entries}
        if "open" in statuses and (statuses - {"open"}):
            fails.append(f"contract {cs} appears in BOTH an open and a closed row: {entries}")

    return fails


# ── application ──────────────────────────────────────────────────────────────

def snapshot_journal() -> Path:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest  = SNAPSHOT_DIR / f"options_journal.pre-orphanfix-{stamp}.json"
    shutil.copy2(MO.JOURNAL_FILE, dest)
    return dest


def apply_repair(plan: dict) -> dict:
    """Write the plan. Updates first, deletion last, so the archived copy of the
    phantom carries its repair_note."""
    applied = {"updated": [], "deleted": []}

    for rid, change in plan["updates"].items():
        updates = {field: after for field, (_before, after) in change.items()}
        result = MO.update_play(rid, updates)
        if result is None:
            raise Abort(f"update_play({rid}) returned None — row vanished mid-repair. "
                        f"STOP and inspect the journal.")
        applied["updated"].append(rid)

    for d in plan["deletes"]:
        # force=True is required: these rows carry execution_verified=True and
        # delete_play() correctly refuses them otherwise. The deletion is
        # preserved verbatim in options_journal_deleted.json with the reason.
        res = MO.delete_play(d["id"], reason=d["reason"], force=True)
        if not res.get("ok"):
            raise Abort(f"delete_play({d['id']}) refused: {res.get('error')}")
        applied["deleted"].append(d["id"])

    return applied


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair the four orphan-backfill-corrupted options journal rows.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. WITHOUT this flag the script is a DRY RUN "
                         "and touches nothing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op form of the default behaviour")
    ap.add_argument("--verify-exit", action="store_true",
                    help="re-pull the SPY exit order from a live Alpaca and check it "
                         "against the operator-asserted numbers before writing")
    args = ap.parse_args(argv)

    write = bool(args.apply) and not args.dry_run

    print("=" * 76)
    print("repair_orphan_backfills.py — " + ("APPLY (will write)" if write
                                             else "DRY RUN (writes nothing)"))
    print("=" * 76)
    print(f"journal: {MO.JOURNAL_FILE}")

    rows = MO._load_journal()
    print(f"{len(rows)} rows loaded.")

    # ── provenance, stated every run ──────────────────────────────────────
    print("\nNUMBER PROVENANCE")
    print("  BROKER-VERIFIED   entry fills (filled_avg_price / filled_qty) — already on")
    print("                    each row, written by a real Alpaca order lookup.")
    print("  OPERATOR-ASSERTED SPY exit $5.77 x 1 on order a0e7c7bc — from Nico's")
    print("                    confirmed statement in the row's own notes. This script")
    print("                    has NOT re-pulled it from Alpaca. Use --verify-exit to.")
    print("  DERIVED           every P&L. (5.77 - 0.68) x 1 x 100 = "
          f"${derive_pnl(0.68, 5.77, 1.0):+.2f}. Never a copied figure.")

    if args.verify_exit:
        try:
            v = verify_exit_against_alpaca()
        except Abort as e:
            print(f"\nABORTED: {e}")
            return 2
        if v.get("verified"):
            print("  -> SPY exit is now BROKER-VERIFIED for this run.")

    try:
        plan = plan_repair(rows)
    except Abort as e:
        print(f"\nABORTED: {e}")
        return 2

    print_plan(plan, rows)

    if plan["already_clean"]:
        print("\nAlready repaired. Nothing written.")
        fails = assert_end_state(rows)
        _print_assertions(fails)
        return 0 if not fails else 1

    if not write:
        print("\n" + "-" * 76)
        print("DRY RUN — nothing was written.")
        print("Review the diff above. If it is right, re-run with --apply.")
        print("-" * 76)
        return 0

    snap = snapshot_journal()
    print(f"\nSnapshot written: {snap}")

    try:
        applied = apply_repair(plan)
    except Abort as e:
        print(f"\nABORTED MID-WRITE: {e}")
        print(f"Restore from {snap} before doing anything else.")
        return 2

    print(f"\nApplied. updated={applied['updated']}  deleted={applied['deleted']}")

    after = MO._load_journal()
    print(f"{len(after)} rows now in the journal.")
    fails = assert_end_state(after)
    _print_assertions(fails)
    if fails:
        print(f"\nThe journal is NOT clean. Snapshot is at {snap}.")
        return 1
    return 0


def _print_assertions(fails: list):
    print("\n" + "=" * 76)
    print("POST-REPAIR ASSERTIONS")
    print("=" * 76)
    if not fails:
        print("  PASS  no row is missing premium or contracts")
        print("  PASS  no contract_symbol appears in both an open and a closed row")
    else:
        for f in fails:
            print(f"  FAIL  {f}")


if __name__ == "__main__":
    sys.exit(main())
