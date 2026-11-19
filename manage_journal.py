#!/usr/bin/env python3
"""
manage_journal.py — deterministic, no-LLM control of the trade / options journal
in mason.db.

WHY THIS EXISTS
---------------
2026-09-05: qwen3.5:4b narrated a trade deletion it never executed (the 27k tool
prefix had been truncated out of its context), and routing the turn to Opus was
both against policy and useless. An integrity-critical mutation cannot depend on
a model emitting a tool call. This is the path that doesn't: pure Python, zero
API, zero tokens, exit codes, fully tested (test_manage_journal.py).

It calls the same mason_trades / mason_options public functions the chat tools
do, so every guard (soft-delete, real-fill refusal, broker flat-check, accuracy
recompute) still applies.

COMMANDS
--------
  list [--all] [--options]        live trades (--all: + closed; --options: plays)
  show <id>                       one row, every field
  deleted [--options]             soft-deleted rows + why
  delete <id> --reason R [--force] [--confirm]
  undelete <id>
  close-stock  <id> --price P --reason R [--exit-reason E] [--shares N] [--confirm]
  close-option <id> --pnl N --reason R [--force] [--confirm]

Mutations are two-step: run once to preview the exact row, re-run with --confirm
to write. --force is required to delete a row tied to a real Alpaca fill, or to
close an option without the live broker flat-check.

EXIT CODES:  0 ok · 1 not found / refused / no-op · 2 usage error
"""

import argparse
import json
import sys
from datetime import date

import mason_db as DB
import mason_trades as MT
import mason_options as MO


# ── helpers ─────────────────────────────────────────────────────────────────

def _p(*a):
    print(*a)


def _fmt_trade(t: dict) -> str:
    return (f"{t['id']}  {t.get('ticker'):6} {t.get('direction','?'):5} "
            f"{t.get('status'):8} entry {t.get('entry_date')}  "
            f"@ {t.get('entry_price')}  shares={t.get('shares')}  "
            f"setup={t.get('setup_type')}"
            + (f"  order_id={t['entry_order_id']}" if t.get('entry_order_id') else ""))


def _fmt_play(p: dict) -> str:
    return (f"{p['id']}  {p.get('ticker'):6} {p.get('strategy','?'):16} "
            f"{p.get('status'):12} exp {p.get('expiration')}  "
            f"{p.get('contract_symbol') or ''}"
            + ("  verified" if p.get('execution_verified') else ""))


# ── read commands ──────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    if args.options:
        plays = MO.get_all_plays() if args.all else MO.get_open_plays()
        _p(f"{len(plays)} option play(s){' (all)' if args.all else ' open'}:")
        for p in plays:
            _p("  " + _fmt_play(p))
    else:
        trades = MT.get_all_trades() if args.all else MT.get_open_trades()
        _p(f"{len(trades)} stock trade(s){' (all)' if args.all else ' open'}:")
        for t in trades:
            _p("  " + _fmt_trade(t))
    return 0


def cmd_show(args) -> int:
    row = MT.get_trade_by_id(args.id) or MO.get_play_by_id(args.id)
    if row is None:
        _p(f"No row with id {args.id} in trades or options_trades.")
        return 1
    _p(json.dumps(row, indent=2, default=str, sort_keys=True))
    return 0


def cmd_deleted(args) -> int:
    table = "options_trades" if args.options else "trades"
    rows = DB.get_connection().execute(
        f"SELECT id, ticker, status, deleted_at, deleted_by, deleted_reason FROM {table} "
        f"WHERE is_deleted = 1 ORDER BY deleted_at DESC").fetchall()
    _p(f"{len(rows)} soft-deleted row(s) in {table}:")
    for r in rows:
        _p(f"  {r['id']}  {r['ticker']:6} {r['status']:10} "
           f"deleted {r['deleted_at']} by {r['deleted_by'] or '?'}  — {r['deleted_reason']}")
    return 0


# ── mutations ──────────────────────────────────────────────────────────────

def cmd_delete(args) -> int:
    if not (args.reason or "").strip():
        _p("delete requires --reason (an audit trail is not optional).")
        return 2
    t = MT.get_trade_by_id(args.id)
    kind, row = ("stock trade", t)
    if row is None:
        row = MO.get_play_by_id(args.id)
        kind = "options play"
    if row is None:
        _p(f"id {args.id} not found in trades or options_trades. Nothing deleted.")
        return 1

    if DB.get_connection().execute(
            f"SELECT is_deleted FROM {'trades' if kind.startswith('stock') else 'options_trades'} "
            f"WHERE id = ?", (args.id,)).fetchone()["is_deleted"]:
        _p(f"{args.id} is already soft-deleted. Use `undelete {args.id}` to restore it.")
        return 1

    _p(f"Target: {kind}")
    _p("  " + (_fmt_trade(row) if kind.startswith("stock") else _fmt_play(row)))
    if not args.confirm:
        _p(f"\nPreview only — nothing written. To soft-delete (recoverable), run:\n"
           f"  python3 manage_journal.py delete {args.id} --reason {args.reason!r} --confirm"
           + ("  --force" if (row.get('entry_order_id') or row.get('execution_verified')) else ""))
        return 0

    if kind.startswith("stock"):
        res = MT.delete_trade(args.id, reason=args.reason, force=args.force)
    else:
        res = MO.delete_play(args.id, reason=args.reason, force=args.force)
    if res.get("ok"):
        _p(f"\nSoft-deleted {args.id} (is_deleted=1). Recoverable with `undelete {args.id}`.")
        return 0
    _p(f"\n[NOT DELETED] {res.get('error')}")
    return 1


def cmd_undelete(args) -> int:
    conn = DB.get_connection()
    for table in ("trades", "options_trades"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (args.id,)).fetchone():
            if DB.restore(conn, table, args.id):
                _p(f"Restored {args.id} in {table} — it is live again.")
                # keep the recompute caches honest
                (MT if table == "trades" else MO)._recompute_accuracy(
                    (MT if table == "trades" else MO)._load_journal())
                return 0
            _p(f"{args.id} in {table} is not soft-deleted — nothing to undo.")
            return 1
    _p(f"No row with id {args.id}.")
    return 1


def cmd_close_stock(args) -> int:
    if not (args.reason or "").strip():
        _p("close-stock requires --reason.")
        return 2
    t = MT.get_trade_by_id(args.id)
    if t is None:
        _p(f"No stock trade with id {args.id}.")
        return 1
    if t.get("status") != "open":
        _p(f"{args.id} is already '{t.get('status')}' (outcome {t.get('outcome')}, "
           f"pnl {t.get('actual_pnl')}). Not re-closing.")
        return 1
    _p("Target: " + _fmt_trade(t))
    # Phase-2 A9 (RULINGS #30): the close_trade guard refuses deleted / _link-ed /
    # personal-account rows even when status is 'open'. Say so at preview time too,
    # instead of offering a --confirm command that would only be refused.
    _found, _refusal = MT.close_refusal(args.id)
    if _refusal:
        _p(f"Refused: {_refusal}.")
        return 2
    _p(f"  -> close @ {args.price}, exit_reason={args.exit_reason}, "
       f"shares={args.shares or t.get('shares')}")
    if not args.confirm:
        _p(f"\nPreview only. To write:\n  python3 manage_journal.py close-stock {args.id} "
           f"--price {args.price} --reason {args.reason!r} "
           f"--exit-reason {args.exit_reason} --confirm")
        return 0
    price = float(args.price)
    shares = float(args.shares) if args.shares else None
    try:
        row = MT.close_trade(args.id, exit_price=price,
                             exit_reason=args.exit_reason,
                             review_text=args.reason,
                             shares_override=shares)
    except ValueError as e:                    # A9 guard: nothing was written
        _p(f"Refused: {e}.")
        return 2
    if row is None:
        _p("close_trade returned nothing — id not found at write time.")
        return 1
    _p(f"\nClosed {args.id}: status={row['status']} outcome={row['outcome']} "
       f"pnl={row['actual_pnl']}")
    return 0


def _flat_lookup(_symbol):
    """A position_lookup that reports 'flat'. Used only with --force, where the
    operator is explicitly asserting the contract is closed."""
    return {"ok": True, "exists": False, "qty": 0, "qty_available": 0}


def cmd_close_option(args) -> int:
    if not (args.reason or "").strip():
        _p("close-option requires --reason.")
        return 2
    p = MO.get_play_by_id(args.id)
    if p is None:
        _p(f"No options play with id {args.id}.")
        return 1
    _p("Target: " + _fmt_play(p))
    _p(f"  -> close, actual_pnl={args.pnl}"
       + ("   [--force: skipping broker flat-check, asserting closed]" if args.force else ""))
    if not args.confirm and MO._close_pnl_check_on():   # (KB review 2026-11-19) show the P&L check before --confirm
        try:
            chk = MO._close_pnl_check(p, float(args.pnl), forced=bool(args.force))
            _p(f"  -> P&L check: {chk['record'].get('result')}"
               + (f"; {chk['use']:+.2f} would be booked instead" if chk.get("use") is not None else ""))
        except Exception as e:
            _p(f"  -> P&L check unavailable ({type(e).__name__})")
    if not args.confirm:
        _p(f"\nPreview only. To write:\n  python3 manage_journal.py close-option {args.id} "
           f"--pnl {args.pnl} --reason {args.reason!r} --confirm"
           + ("  --force" if args.force else ""))
        return 0
    res = MO.close_play_early(
        args.id, actual_pnl=float(args.pnl), notes=args.reason,
        force=args.force, force_reason=(args.reason if args.force else ""),
        position_lookup=_flat_lookup if args.force else None)
    if res.get("ok"):
        row = res["play"]
        _p(f"\nClosed {args.id}: status={row['status']} outcome={row.get('outcome')} "
           f"pnl={row.get('actual_pnl')}")
        if res.get("pnl_note"):                      # KB item 4: only with options_journal.close_pnl_check on
            _p(res["pnl_note"])
        return 0
    _p(f"\n[NOT CLOSED] {res.get('error') or res.get('refused_because')}")
    return 1


# ── arg parsing ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="manage_journal.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--all", action="store_true")
    p.add_argument("--options", action="store_true"); p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show"); p.add_argument("id"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser("deleted"); p.add_argument("--options", action="store_true")
    p.set_defaults(fn=cmd_deleted)

    p = sub.add_parser("delete")
    p.add_argument("id"); p.add_argument("--reason", default="")
    p.add_argument("--force", action="store_true"); p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_delete)

    p = sub.add_parser("undelete"); p.add_argument("id"); p.set_defaults(fn=cmd_undelete)

    p = sub.add_parser("close-stock")
    p.add_argument("id"); p.add_argument("--price", required=True)
    p.add_argument("--reason", default=""); p.add_argument("--exit-reason", default="manual",
                   dest="exit_reason")
    p.add_argument("--shares", default=None); p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_close_stock)

    p = sub.add_parser("close-option")
    p.add_argument("id"); p.add_argument("--pnl", required=True)
    p.add_argument("--reason", default=""); p.add_argument("--force", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(fn=cmd_close_option)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except SystemExit:
        raise
    except Exception as e:
        _p(f"error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
