"""
Mason Play Review Engine
========================
Run this (or have Mason call it) when reviewing yesterday's / last session's plays.

What it does:
  1. Loads all open plays from daily_plays.csv
  2. Pulls current prices via Alpha Vantage
  3. Prints a review sheet for Mason to score each play
  4. Mason calls update_play_outcome() for each play with its score

Scoring rubric (0-10):
  10 — Perfect: hit target price, thesis played out exactly as described
  8-9 — Strong: directionally correct, didn't quite hit target but close
  6-7 — Partial: thesis was right, timing was off or move was smaller
  4-5 — Mixed: unclear, market moved but for unrelated reasons
  2-3 — Miss: directionally wrong, thesis didn't play out
  0-1 — Bad call: significant move against the thesis

Mason uses the aggregate scores to:
  - Report hit rate / avg score to Nico
  - Weight future recommendations (high-scoring play types → higher confidence)
  - Identify what types of plays it's good/bad at calling
"""

import json
import csv
import requests
from datetime import date
from pathlib import Path

# Import from mason_brief
import sys
sys.path.insert(0, str(Path(__file__).parent))
from mason_brief import (
    review_open_plays,
    update_play_outcome,
    plays_performance_summary,
    get_quote,
    BASE,
)


def print_review_sheet():
    """Print all open plays with current prices for Mason to score."""
    open_plays = review_open_plays()

    if not open_plays:
        print("No open plays to review.")
        return

    print(f"\n{'='*70}")
    print(f"MASON PLAY REVIEW — {date.today().strftime('%B %d, %Y')}")
    print(f"{'='*70}\n")

    for p in open_plays:
        entry  = float(p.get("entry_price", 0) or 0)
        curr   = float(p.get("current_price", 0) or 0)
        move   = p.get("move_since_logged", "?")
        sign   = "+" if isinstance(move, float) and move >= 0 else ""

        print(f"  Play #{p['id']} | {p['ticker']} | {p['play_type'].upper()} | "
              f"Confidence: {p['confidence'].upper()}")
        print(f"  Logged:  {p['date_logged']} @ ${entry}")
        print(f"  Target:  ${p.get('target_price') or 'not set'}")
        print(f"  Now:     ${curr}  ({sign}{move}% since logged)")

        if p.get("option_strike"):
            print(f"  Option:  {p['option_type'].upper()} ${p['option_strike']} "
                  f"exp {p['option_expiry']}  |  Premium: ${p.get('option_premium','?')}  "
                  f"|  Breakeven: ${p.get('breakeven','?')}")

        print(f"  Thesis:  {p['thesis']}")
        if p.get("notes"):
            print(f"  Notes:   {p['notes']}")
        print()

    print(f"{'='*70}")
    print(f"Total open plays: {len(open_plays)}")
    print()


def score_play_interactive():
    """Interactive CLI to score each open play."""
    open_plays = review_open_plays()
    if not open_plays:
        print("No open plays to score.")
        return

    print("\nScoring plays (0-10 scale, or 's' to skip, 'q' to quit):")
    for p in open_plays:
        print(f"\n  #{p['id']} {p['ticker']} {p['play_type'].upper()} — {p['thesis'][:80]}...")
        print(f"  Entry ${p['entry_price']} → Now ${p.get('current_price','?')} "
              f"({p.get('move_since_logged','?')}% move)")
        outcome = input("  Outcome (hit/missed/partial/expired): ").strip().lower()
        if outcome == "q":
            break
        if outcome == "s":
            continue

        score = input("  Score (0-10): ").strip()
        try:
            score = int(score)
        except:
            score = 5

        notes = input("  Notes (optional): ").strip()
        update_play_outcome(int(p["id"]), outcome, score, notes)
        print(f"  ✓ Play #{p['id']} scored.")


def performance_report() -> str:
    """
    Returns a formatted performance summary string.
    Mason includes this in weekly recaps.
    """
    stats = plays_performance_summary()

    if "error" in stats or stats.get("total_reviewed", 0) == 0:
        return "No reviewed plays yet — Mason is building its track record."

    lines = [
        f"\n📊 MASON TRACK RECORD",
        f"  Total plays logged:   {stats['total_logged']}",
        f"  Reviewed:             {stats['total_reviewed']}  ({stats['open']} still open)",
        f"  Hit rate:             {stats['hit_rate']}%",
        f"  Partial rate:         {stats['partial_rate']}%",
        f"  Miss rate:            {stats['miss_rate']}%",
        f"  Avg score:            {stats['avg_score']} / 10",
        f"\n  By play type:",
    ]

    for ptype, d in stats.get("by_play_type", {}).items():
        lines.append(f"    {ptype:<12}  {d['count']} plays  |  avg score: {d['avg_score']}/10")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--score" in sys.argv:
        score_play_interactive()
    else:
        print_review_sheet()
        print(performance_report())
