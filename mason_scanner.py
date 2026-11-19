"""
mason_scanner.py — Scanner Agent (MULTI_AGENT_ROADMAP.md Section 4.0), v1.

Discovery, not judgment: sweeps a static ticker universe (4.0a) plus every
ticker held in the Alpaca paper account (4.0b) through deterministic skill
adapters, and writes one schema-conformant scanner_output JSON per run to
OvernightScans/ (Roadmap Section 5.1 shape + documented metadata extensions).

HARD RULES (build contract):
  * ZERO LLM calls at runtime. Pure deterministic Python.
  * Read-only reuse of mason_execution / mason_options / mason_regime_gate /
    mason_intelligence. Never places, sizes, or modifies anything.
  * Regime/intelligence context is attached as METADATA on every candidate and
    alert — it never filters or suppresses anything.
  * Additive-only: writes exactly one new file per run, never overwrites.

Entry points:
    run_scan(skill_timeout=240, universe_path=None) -> dict   (importable)
    python mason_scanner.py [--skill-timeout N] [--no-write]  (CLI)
"""

import argparse
import json
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import mason_scanner_skills as SS

UNIVERSE_FILE = BASE / "mason_scanner_universe.json"
OUTPUT_DIR = BASE / "OvernightScans"
SCHEMA_VERSION = "scanner_output/1.0"

# Roadmap 5.1 enum + documented v1 extensions (see SCANNER_README.md):
#   option_expiry — held option play near expiration (options are first-class here)
#   adverse_move  — underlying moving hard against a held option's direction
ALLOWED_TRIGGERS = {"earnings_surprise", "volume_spike", "gap", "news",
                    "stop_approaching", "target_approaching", "correlation_drift",
                    "option_expiry", "adverse_move"}
ALLOWED_CAND_URGENCY = {"routine", "elevated"}
ALLOWED_ALERT_URGENCY = {"routine", "needs_attention_now"}

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ── inputs: universe + held book (read-only) ─────────────────────────────────────

def load_universe(path=None):
    p = Path(path) if path else UNIVERSE_FILE
    d = json.loads(p.read_text(encoding="utf-8"))
    return [t.upper() for t in d.get("tickers", [])]


def _parse_occ(symbol):
    """RKLB260724C00050000 -> (RKLB, date(2026,7,24), 'C', 50.0) or None."""
    m = _OCC_RE.match((symbol or "").strip().upper())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return root, exp, cp, int(strike) / 1000.0


def _contract_label(contract):
    """AMD260821C00500000 -> '$500 exp 2026-08-21'. Bug 6 fix (2026-07-28):
    used so two DIFFERENT contracts on the same ticker never produce
    indistinguishable alert text (see comment at the adverse_move loop)."""
    occ = _parse_occ(contract)
    if not occ:
        return None
    _root, exp_d, _cp, strike = occ
    return f"${strike:g} exp {exp_d.isoformat()}"


def fetch_held_book():
    """Read-only pull of the paper book: equity positions, option positions,
    and Mason's open option-play journal entries. Never raises."""
    book = {"equities": [], "options": [], "journal_plays": [],
            "errors": [], "attempted_calls": 0}
    try:
        import mason_execution as EX
        book["attempted_calls"] += 1
        pos = EX.positions()
        if pos.get("ok"):
            for p in pos["positions"]:
                sym = (p.get("symbol") or "").upper()
                if _parse_occ(sym):
                    book["options"].append(p)
                else:
                    book["equities"].append(p)
        else:
            book["errors"].append(f"alpaca positions: {pos.get('error')} "
                                  f"({SS.LIVE_ENV_MSG})")
    except Exception as e:
        book["errors"].append(f"mason_execution unavailable: {e}")
    try:
        import mason_options as MO
        for play in (MO.get_open_plays() or []):
            book["journal_plays"].append(play)
    except Exception as e:
        book["errors"].append(f"mason_options journal unavailable: {e}")
    return book


def held_tickers(book):
    s = set()
    for p in book["equities"]:
        t = (p.get("symbol") or "").upper()
        if t:
            s.add(t)
    for p in book["options"]:
        occ = _parse_occ(p.get("symbol"))
        if occ:
            s.add(occ[0])
    for j in book["journal_plays"]:
        t = (j.get("ticker") or "").upper()
        if t:
            s.add(t)
    return s


# ── context: regime gate + intelligence (file-based reads, metadata only) ────────

def gather_context():
    """Regime + self-graded-confidence context. METADATA ONLY — callers must not
    use this to filter candidates (Nico mandate: full visibility, context attached)."""
    ctx = {"regime_gate": None, "intelligence": None, "errors": []}
    try:
        import mason_regime_gate as G
        g = G.gate(force_breadth=False)   # reads posture cache; no forced refresh
        ctx["regime_gate"] = {k: g.get(k) for k in
                              ("stance", "risk_zone", "composite",
                               "exposure_ceiling_pct", "risk_multiplier",
                               "new_entries", "fighting_tape", "line", "as_of")}
    except Exception as e:
        ctx["errors"].append(f"regime gate: {e}")
    try:
        import mason_intelligence as MI
        tl = MI.regime_timeline()
        sc = MI.scorecard()
        cur = tl.get("current") or {}
        ctx["intelligence"] = {
            "regime": cur.get("regime"),
            "regime_since": cur.get("start"),
            "regime_streak_snapshots": cur.get("snapshots"),
            "lead_sector": tl.get("lead_sector"),
            "lead_streak": tl.get("lead_streak"),
            "n_snapshots": tl.get("n"),
            "read_confidence_factor": sc.get("confidence_factor"),
            "n_graded_reads": sc.get("n_graded"),
            "calib_accuracy": sc.get("calib_accuracy"),
            "accuracy_by_regime": sc.get("by_regime") or {},
        }
    except Exception as e:
        ctx["errors"].append(f"intelligence: {e}")
    return ctx


def _candidate_context(ctx):
    """Compact per-candidate metadata slice of the full context block."""
    g = ctx.get("regime_gate") or {}
    i = ctx.get("intelligence") or {}
    regime = i.get("regime")
    reg_acc = (i.get("accuracy_by_regime") or {}).get(regime or "", None)
    return {"risk_zone": g.get("risk_zone"), "stance": g.get("stance"),
            "regime": regime, "regime_streak_snapshots": i.get("regime_streak_snapshots"),
            "read_confidence_factor": i.get("read_confidence_factor"),
            "regime_read_accuracy": reg_acc}


# ── 4.0a: new-opportunity scan ───────────────────────────────────────────────────

def _merge_candidates(adapter_results):
    """Union all adapter candidates; a ticker flagged by multiple skills gets one
    entry with merged flagged_by / reasons and the max urgency."""
    merged = {}
    for res in adapter_results:
        for c in res.get("candidates", []):
            t = c["ticker"]
            if t not in merged:
                merged[t] = {"ticker": t, "flagged_by": list(c["flagged_by"]),
                             "reason": c["reason"], "urgency": c["urgency"],
                             "signals": {c["flagged_by"][0]: c.get("signal") or {}}}
            else:
                m = merged[t]
                for src in c["flagged_by"]:
                    if src not in m["flagged_by"]:
                        m["flagged_by"].append(src)
                m["reason"] = m["reason"] + " | " + c["reason"]
                m["signals"][c["flagged_by"][0]] = c.get("signal") or {}
                if c["urgency"] == "elevated":
                    m["urgency"] = "elevated"
    return sorted(merged.values(),
                  key=lambda m: (-len(m["flagged_by"]), m["urgency"] != "elevated",
                                 m["ticker"]))


def run_new_opportunity_scan(universe, skill_timeout):
    results = [
        SS.vcp_candidates(timeout=skill_timeout, universe=universe),
        SS.canslim_candidates(timeout=skill_timeout),
        SS.earnings_candidates(timeout=skill_timeout),
        SS.theme_candidates(universe, timeout=skill_timeout),
        SS.neglected_value_candidates(),
    ]
    tech_snaps = SS.technical_snapshots(universe)
    results.append(SS.technical_candidates(tech_snaps.get("snapshots") or {}))
    earnings_raw = []
    for r in results:
        if r["source"] == "earnings-trade-analyzer":
            earnings_raw = (r.get("extra") or {}).get("raw_results") or []
    return _merge_candidates(results), results, tech_snaps, earnings_raw


# ── 4.0b: position-aware scan (pure logic — unit-testable) ───────────────────────

def build_position_alerts(book, snapshots, earnings_raw, today=None,
                          gap_pct=4.0, big_gap_pct=7.0, vol_spike=2.5,
                          stop_loss_pct=-7.0, target_gain_pct=20.0,
                          expiry_soon_dte=5, expiry_urgent_dte=2):
    """Material-change sweep of every held ticker. PURE function: all inputs are
    plain data, no I/O — the live wrapper is run_scan()."""
    today = today or date.today()
    alerts = []
    earn_by_sym = {}
    for row in (earnings_raw or []):
        sym = (row.get("symbol") or "").upper()
        if sym:
            earn_by_sym[sym] = row

    def add(ticker, trigger, detail, urgency, held_since=None):
        alerts.append({"ticker": ticker, "held_since": held_since,
                       "trigger": trigger, "detail": detail, "urgency": urgency})

    # Equity positions.
    for p in book.get("equities", []):
        t = (p.get("symbol") or "").upper()
        if not t:
            continue
        snap = (snapshots or {}).get(t) or {}
        plpc = None
        try:
            plpc = float(p.get("unrealized_plpc")) * 100.0
        except (TypeError, ValueError):
            pass
        gap = snap.get("gap_pct")
        vr = snap.get("vol_ratio_20d")
        chg = snap.get("day_change_pct")
        if chg is None:
            try:
                chg = float(p.get("change_today")) * 100.0
            except (TypeError, ValueError):
                pass
        if gap is not None and abs(gap) >= gap_pct:
            add(t, "gap", f"gapped {gap:+.1f}% vs prior close"
                + (f" on {vr:.1f}x volume" if vr else ""),
                "needs_attention_now" if abs(gap) >= big_gap_pct else "routine")
        elif chg is not None and abs(chg) >= big_gap_pct:
            add(t, "gap", f"moved {chg:+.1f}% today",
                "needs_attention_now")
        if vr is not None and vr >= vol_spike:
            add(t, "volume_spike", f"volume {vr:.1f}x 20-day average"
                + (f", day move {chg:+.1f}%" if chg is not None else ""),
                "needs_attention_now" if (chg is not None and abs(chg) >= 5) else "routine")
        er = earn_by_sym.get(t)
        if er:
            eg = er.get("gap_pct")
            add(t, "earnings_surprise",
                f"post-earnings grade {er.get('grade')} "
                f"(score {er.get('composite_score')}"
                + (f", gap {eg:+.1f}%" if isinstance(eg, (int, float)) else "") + ")",
                "needs_attention_now" if (isinstance(eg, (int, float)) and abs(eg) >= 5)
                else "routine")
        if plpc is not None and plpc <= stop_loss_pct:
            add(t, "stop_approaching",
                f"open loss {plpc:.1f}% — breaching default 8% stop discipline "
                f"(no explicit stop on file)", "needs_attention_now")
        elif plpc is not None and plpc >= target_gain_pct:
            add(t, "target_approaching",
                f"open gain {plpc:+.1f}% — review profit-taking rules", "routine")

    # Option positions/plays: journal plays first (richer: direction + entry date),
    # then raw Alpaca option positions not covered by a journal entry.
    seen_occ = set()
    # Bug 6 fix (2026-07-28, revised): docs/prompts/DEBUG_SCANNER_DUPLICATE_ALERTS_PROMPT.md
    # scoped this as "2 journal entries share one contract_symbol." Checking the
    # REAL AMD data (options_journal.json) after shipping the first version of
    # this fix showed that claim was wrong — the actual 3 AMD alerts came from
    # 3 DIFFERENT contract_symbols (1 unrelated long_call + 2 legs of a
    # bull_call_spread), so a contract_symbol-only dedup collapses nothing.
    # Confirmed by running the fixed code against the real journal file before
    # shipping this revision — see [[project-mason-scanner-duplicate-alerts]].
    #
    # Two-part fix, per Nico's explicit choice (2026-07-28) rather than a guess:
    #   1. Still collapse TRUE duplicates — a second entry on the exact same
    #      contract_symbol merges into one alert with a running count.
    #   2. Every adverse_move alert now names its own strike/expiry (via
    #      _contract_label) so DIFFERENT contracts on the same ticker are never
    #      indistinguishable text — without merging or guessing whether they're
    #      related. Whether same-day multi-contract plays should be linked into
    #      one logical position is still Nico's call alone, not decided here
    #      (same open question as the Arbiter's correlation_check — see
    #      ARBITER_README.md Section 11 / mason_arbiter.py:236-275).
    adverse_by_contract = {}   # contract_symbol -> {"idx": alerts-list index, "count": int}
    for j in book.get("journal_plays", []):
        t = (j.get("ticker") or "").upper()
        exp = j.get("expiration")
        held_since = j.get("entry_date") or j.get("created_at")
        direction = (j.get("direction") or "").lower()
        contract = (j.get("contract_symbol") or "").upper()
        try:
            exp_d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            exp_d = None
        if contract:
            seen_occ.add(contract)
        if exp_d is not None:
            dte = (exp_d - today).days
            if dte <= expiry_soon_dte:
                add(t, "option_expiry",
                    f"open {j.get('strategy') or 'option'} play expires {exp} "
                    f"({dte} DTE)",
                    "needs_attention_now" if dte <= expiry_urgent_dte else "routine",
                    held_since=held_since)
        snap = (snapshots or {}).get(t) or {}
        chg = snap.get("day_change_pct")
        if chg is not None and direction in ("bullish", "bearish", "call", "put"):
            against = (chg <= -4.0 if direction in ("bullish", "call")
                       else chg >= 4.0)
            if against:
                label = _contract_label(contract)
                tag = f" ({label})" if label else ""
                if contract and contract in adverse_by_contract:
                    rec = adverse_by_contract[contract]
                    rec["count"] += 1
                    alerts[rec["idx"]]["detail"] = (
                        f"underlying moved {chg:+.1f}% against open {direction} "
                        f"option play{tag} ({rec['count']} plays on this contract)"
                    )
                else:
                    add(t, "adverse_move",
                        f"underlying moved {chg:+.1f}% against open "
                        f"{direction} option play{tag}", "needs_attention_now",
                        held_since=held_since)
                    if contract:
                        adverse_by_contract[contract] = {"idx": len(alerts) - 1, "count": 1}
    for p in book.get("options", []):
        occ = _parse_occ(p.get("symbol"))
        if not occ or (p.get("symbol") or "").upper() in seen_occ:
            continue
        root, exp_d, cp, strike = occ
        dte = (exp_d - today).days
        if dte <= expiry_soon_dte:
            add(root, "option_expiry",
                f"Alpaca option position {p.get('symbol')} expires "
                f"{exp_d.isoformat()} ({dte} DTE, no journal entry)",
                "needs_attention_now" if dte <= expiry_urgent_dte else "routine")
    return alerts


# ── schema validation ────────────────────────────────────────────────────────────

def validate_scanner_output(d):
    """Validate against Roadmap 5.1 shape (+ documented extensions).
    Returns (ok: bool, problems: list[str])."""
    probs = []
    for k in ("cycle_id", "timestamp", "new_candidates", "position_alerts"):
        if k not in d:
            probs.append(f"missing top-level key: {k}")
    if not isinstance(d.get("cycle_id"), str) or not d.get("cycle_id"):
        probs.append("cycle_id must be non-empty string")
    try:
        datetime.fromisoformat(str(d.get("timestamp")))
    except ValueError:
        probs.append(f"timestamp not ISO8601: {d.get('timestamp')!r}")
    for i, c in enumerate(d.get("new_candidates") or []):
        w = f"new_candidates[{i}]"
        if not c.get("ticker"):
            probs.append(f"{w}: missing ticker")
        if not isinstance(c.get("flagged_by"), list) or not c.get("flagged_by"):
            probs.append(f"{w}: flagged_by must be non-empty list")
        if not c.get("reason"):
            probs.append(f"{w}: missing reason")
        if c.get("urgency") not in ALLOWED_CAND_URGENCY:
            probs.append(f"{w}: bad urgency {c.get('urgency')!r}")
    for i, a in enumerate(d.get("position_alerts") or []):
        w = f"position_alerts[{i}]"
        if not a.get("ticker"):
            probs.append(f"{w}: missing ticker")
        if "held_since" not in a:
            probs.append(f"{w}: missing held_since key")
        if a.get("trigger") not in ALLOWED_TRIGGERS:
            probs.append(f"{w}: bad trigger {a.get('trigger')!r}")
        if not a.get("detail"):
            probs.append(f"{w}: missing detail")
        if a.get("urgency") not in ALLOWED_ALERT_URGENCY:
            probs.append(f"{w}: bad urgency {a.get('urgency')!r}")
    return (not probs), probs


# ── orchestration ────────────────────────────────────────────────────────────────

def run_scan(skill_timeout=SS.DEFAULT_SKILL_TIMEOUT, universe_path=None):
    """Full Scanner cycle (4.0a + 4.0b + context attachment). Returns the
    scanner_output dict; never raises on data-source failure — every failed
    source is recorded in data_sources with its error."""
    started = datetime.now()
    universe = load_universe(universe_path)
    book = fetch_held_book()
    held = held_tickers(book)
    scan_list = sorted(set(universe) | held)

    ctx = gather_context()
    cand_ctx = _candidate_context(ctx)

    new_cands, adapter_results, tech_snaps, earnings_raw = \
        run_new_opportunity_scan(scan_list, skill_timeout)
    for c in new_cands:
        c["context"] = dict(cand_ctx)

    alerts = build_position_alerts(book, tech_snaps.get("snapshots") or {},
                                   earnings_raw)
    for a in alerts:
        a["context"] = dict(cand_ctx)

    sources = {}
    for r in adapter_results:
        sources[r["source"]] = {
            "ok": r["ok"], "cached": r.get("cached", False),
            "candidates": len(r["candidates"]), "error": r.get("error"),
            "attempted_calls": r.get("attempted_calls", 0)}
    sources["technical-snapshots"] = {
        "ok": tech_snaps["ok"], "cached": False,
        "tickers_covered": len(tech_snaps.get("snapshots") or {}),
        "error": tech_snaps.get("error"),
        "attempted_calls": tech_snaps.get("attempted_calls", 0)}
    sources["alpaca-paper-book"] = {
        "ok": not book["errors"], "error": "; ".join(book["errors"]) or None,
        "equities": len(book["equities"]), "options": len(book["options"]),
        "journal_plays": len(book["journal_plays"]),
        "attempted_calls": book["attempted_calls"]}

    now = datetime.now()
    out = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": f"scan_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "timestamp": now.isoformat(timespec="seconds"),
        "new_candidates": new_cands,
        "position_alerts": alerts,
        # ── metadata extensions (documented in SCANNER_README.md) ──
        "context": ctx,
        "universe": {"base_count": len(universe), "held_added": sorted(held),
                     "scanned_count": len(scan_list)},
        "data_sources": sources,
        "runtime_seconds": round((now - started).total_seconds(), 1),
        "llm_calls": 0,   # by construction — this tool never calls a model
    }
    return out


def write_output(out, output_dir=None):
    """Write one new file per run; never overwrite an existing one."""
    outdir = Path(output_dir) if output_dir else OUTPUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = outdir / f"scanner_output_{stamp}.json"
    while path.exists():
        path = outdir / f"scanner_output_{stamp}_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return path


def get_latest_scan(output_dir=None):
    """Find and load the newest OvernightScans/scanner_output_*.json for chat/greeting
    consumption. Never raises — mirrors the rest of this module's fail-soft contract.

    Returns either:
      {"ok": True, "path": str, "data": {...full scanner_output...},
       "stale": bool, "age_note": str}
      {"ok": False, "error": str}                      (no files / read/parse failure)

    `stale` is True when the newest file's `timestamp` is not from today (local date) —
    e.g. Mason wasn't restarted since yesterday, so the boot-time scan never re-ran.
    Filenames are timestamp-sortable (scanner_output_YYYYMMDD_HHMMSS[_hex].json), so a
    plain lexicographic sort finds the newest without stat()-ing every file.
    """
    outdir = Path(output_dir) if output_dir else OUTPUT_DIR
    try:
        files = sorted(outdir.glob("scanner_output_*.json"))
        if not files:
            return {"ok": False, "error": "no scanner output files found in OvernightScans/"}
        latest = files[-1]
        data = json.loads(latest.read_text(encoding="utf-8"))
        try:
            ts = datetime.fromisoformat(str(data.get("timestamp")))
        except (ValueError, TypeError):
            ts = datetime.fromtimestamp(latest.stat().st_mtime)
        stale = ts.date() != date.today()
        age_note = ("same-day scan" if not stale else
                    f"scan is from {ts.date().isoformat()}, not today ({date.today().isoformat()})")
        return {"ok": True, "path": str(latest), "data": data,
                "stale": stale, "age_note": age_note}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mason Scanner Agent (Roadmap 4.0)")
    ap.add_argument("--skill-timeout", type=int, default=SS.DEFAULT_SKILL_TIMEOUT,
                    help="per-skill subprocess timeout, seconds (default 240)")
    ap.add_argument("--no-write", action="store_true",
                    help="print JSON to stdout instead of writing OvernightScans/")
    a = ap.parse_args(argv)

    out = run_scan(skill_timeout=a.skill_timeout)
    ok, probs = validate_scanner_output(out)
    if a.no_write:
        print(json.dumps(out, indent=2, default=str))
    else:
        path = write_output(out)
        print(f"wrote {path}")
    print(f"cycle {out['cycle_id']}: {len(out['new_candidates'])} candidates, "
          f"{len(out['position_alerts'])} position alerts, "
          f"{out['runtime_seconds']}s, schema_valid={ok}")
    live = [k for k, v in out["data_sources"].items() if v.get("ok")]
    dead = [k for k, v in out["data_sources"].items() if not v.get("ok")]
    print(f"sources ok: {', '.join(live) or 'none'}")
    if dead:
        print(f"sources failed (expected offline): {', '.join(dead)}")
    if not ok:
        print("SCHEMA PROBLEMS:")
        for p in probs:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
