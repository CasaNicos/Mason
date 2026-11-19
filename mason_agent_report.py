"""
mason_agent_report.py — read-mostly reporting around the agent loop (PAPER ONLY, NO LLM).

  status_payload()     dashboard widget JSON (cached 30 s in-process)
  weekly_report()      Fridays after 16:30 ET, once per ISO week: docs/fable/weekly/WEEK_<yyyy>-W<ww>.md
                       + ONE inbox notification; proposals are deterministic and never applied
  maybe_switch_lean()  owner's ruling: after two trading days of the 13:00 call running lean and
                       passing every check, set v2.lean_kinds to ["all"] (the only config write here)
  tick()               scheduler entry: cheap no-op most of the time

Every public function catches exceptions and returns a dict; nothing raises.
"""

import json
import os
import tempfile
import time
from datetime import datetime, date, timedelta
from pathlib import Path

WEEKLY_DIR = Path(__file__).parent / "docs" / "fable" / "weekly"     # tests redirect this
CACHE_S = 30
_CACHE = {}                    # {"at": epoch, "payload": dict}
LEAN_CHECK_EVERY_S = 1800
_LAST_LEAN_CHECK = 0.0
_STATE_SQL = "CREATE TABLE IF NOT EXISTS agent_report_state(key TEXT PRIMARY KEY, value TEXT, at TEXT)"
_DEFAULT_MIX = {"swing": 40, "intraday": 30, "long": 30}
_HZ_BUCKET = {"0-5DTE": "intraday", "swing": "swing", "30d": "long", "90d+": "long"}
PROPOSAL_HEADER = "PROPOSALS FOR NICO (nothing here is applied until you say so)"


# ── lazy module access ───────────────────────────────────────────────────────────

def _AT():
    import mason_autotrade
    return mason_autotrade


def _AL():
    import mason_agent_loop
    return mason_agent_loop


def _D():
    import mason_director
    return mason_director


def _NT():
    import mason_nico_thesis
    return mason_nico_thesis


def _f(v):
    return _AT()._f(v)


def _q(sql, args=()):
    return _AL()._q(sql, args)


def _exec(sql, args=()):
    conn = _AL()._conn()
    try:
        with conn:
            conn.execute(sql, args)
    finally:
        conn.close()


def _state_get(key):
    _exec(_STATE_SQL)
    r = _q("SELECT value FROM agent_report_state WHERE key=?", (key,))
    return r[0]["value"] if r else None


def _state_set(key, value):
    _exec(_STATE_SQL)
    _exec("INSERT OR REPLACE INTO agent_report_state(key, value, at) VALUES (?,?,?)",
          (key, str(value), datetime.now().isoformat(timespec="seconds")))


def _notify(type_, title, body, dedup):
    import mason_notifications as MN
    MN.add_notification(type_, title[:200], body=body, severity="info", dedup_key=dedup)


def _rate(w, n):
    return (w / n) if n else None


# ── shared pieces ────────────────────────────────────────────────────────────────

def _director_runs() -> list:
    """Opus director v2 activity rows → [{day, created, kind, mode, lean, cost, parse_error, n_pred}]."""
    out = []
    for r in _q("SELECT created_date, extra_json FROM activity WHERE type='opus_director_v2' "
                "ORDER BY created_date"):
        try:
            ex = json.loads(r["extra_json"] or "{}")
        except Exception:
            ex = {}
        out.append({"created": str(r["created_date"] or ""), "day": str(r["created_date"] or "")[:10],
                    "kind": str(ex.get("kind") or ""), "mode": str(ex.get("mode") or ""),
                    "lean": bool(ex.get("lean")), "cost": _f(ex.get("cost_usd")),
                    "parse_error": ex.get("parse_error"), "n_pred": ex.get("n_predictions")})
    return out


def lean_stats() -> dict:
    """Average cost of lean vs full director runs since the day of the first lean run."""
    D = _D()
    runs = _director_runs()
    first = next((r["day"] for r in runs if r["lean"]), None)        # window starts on the first lean DAY
    win = [r for r in runs if first and r["day"] >= first and r["cost"] is not None]
    lean = [r["cost"] for r in win if r["lean"]]
    full = [r["cost"] for r in win if not r["lean"]]
    return {"kinds": list(D.v2_cfg().get("lean_kinds") or []), "since": first,
            "avg_cost_lean": (sum(lean) / len(lean)) if lean else None, "n_lean": len(lean),
            "avg_cost_full": (sum(full) / len(full)) if full else None, "n_full": len(full)}


def _mason_rows(status=None):
    sql = "SELECT * FROM agent_trades WHERE source='mason' AND COALESCE(record_deleted,0)=0"
    if status:
        sql += " AND status=?"
        return _q(sql, (status,))
    return _q(sql)


def _agg(rows, key) -> dict:
    out = {}
    for r in rows:
        k = str(r.get(key) or "?")
        d = out.setdefault(k, {"n": 0, "wins": 0, "pnl_usd": 0.0})
        d["n"] += 1
        d["wins"] += 1 if r.get("win") == 1 else 0
        d["pnl_usd"] = round(d["pnl_usd"] + (r.get("pnl_usd") or 0.0), 2)
    for d in out.values():
        d["win_rate"] = _rate(d["wins"], d["n"])
    return out


# ── status payload ───────────────────────────────────────────────────────────────

def _pos_map(pos) -> dict:
    return {str(p.get("symbol") or "").upper(): p for p in (pos.get("positions") or [])} if pos.get("ok") else {}


def status_payload() -> dict:
    now = time.time()
    if _CACHE.get("payload") is not None and now - _CACHE.get("at", 0) < CACHE_S:
        return _CACHE["payload"]
    errors = []
    AT, AL = _AT(), _AL()
    out = {"ok": True, "as_of": datetime.now().isoformat(timespec="seconds")}
    try:
        AL.ensure_schema()
    except Exception as e:
        errors.append(f"schema: {e}")
    try:
        cfg = AL._cfg()
    except Exception as e:
        cfg = {}
        errors.append(f"cfg: {e}")
    pos = {"ok": False}
    try:
        pos = AT._positions()
        if not pos.get("ok"):
            errors.append(f"positions: {pos.get('error')}")
    except Exception as e:
        errors.append(f"positions: {e}")
    pmap = _pos_map(pos)

    try:
        D = _D()
        sp, v2 = D.spend(), D.v2_cfg()
        out["budget"] = {"day_usd": sp.get("day_usd"), "day_cap_usd": v2.get("daily_usd_cap"),
                         "month_usd": sp.get("month_usd"), "month_cap_usd": v2.get("monthly_usd_cap")}
    except Exception as e:
        out["budget"] = None
        errors.append(f"budget: {e}")

    try:
        acct = AT._account()
        if not acct.get("ok"):
            raise RuntimeError(acct.get("error"))
        eq = acct.get("equity") or 0.0
        sleeve = AT._sleeve_exposure(pos) if pos.get("ok") else None
        today = AL._now_et().date().isoformat()
        out["account"] = {"equity": eq, "cash": acct.get("cash"), "day_pnl_pct": acct.get("day_pnl_pct"),
                          "sleeve_usd": sleeve,
                          "sleeve_pct": (sleeve / eq * 100.0) if (sleeve is not None and eq) else None,
                          "sleeve_cap_pct": cfg.get("max_open_exposure_pct"),
                          "trades_today": len(AT._trades_today(today)),
                          "max_trades_per_day": cfg.get("max_trades_per_day")}
    except Exception as e:
        out["account"] = None
        errors.append(f"account: {e}")

    try:
        b = AL.base_rate()
        segs = []
        for (h, v), st in sorted(AL.segment_rates().items()):
            segs.append({"segment": f"{h}|{v}", "n": st["n"], "wins": st["wins"],
                         "p_cal": AL.calibrate(b["mean_raw"], h, v).get("p_cal")})
        out["calibration"] = {"p_base": b.get("p_base"), "n": b.get("n"), "wins": b.get("wins"),
                              "mean_raw": b.get("mean_raw"), "own_graded_n": AL.own_graded_n(),
                              "normal_requires_own_n": cfg.get("normal_requires_own_n"), "segments": segs}
    except Exception as e:
        out["calibration"] = None
        errors.append(f"calibration: {e}")

    open_rows = []
    try:
        open_rows = _mason_rows("open")
        lst = []
        for r in sorted(open_rows, key=lambda r: str(r.get("entry_ts") or "")):
            sym = (r.get("occ") or r.get("ticker") or "").upper()
            mult = 100 if r.get("occ") else 1
            last = _f((pmap.get(sym) or {}).get("current_price"))
            entry = r.get("entry_px")
            rem = AL._remaining(r)
            pnl = (last - entry) * rem * mult if (last is not None and entry is not None) else None
            lst.append({"rec_id": r["rec_id"], "ticker": r["ticker"], "symbol": sym, "vehicle": r["vehicle"],
                        "tier": r["tier"], "horizon": r["horizon"], "qty": r["qty"], "entry_px": entry,
                        "last": last, "pnl_usd": round(pnl, 2) if pnl is not None else None,
                        "pnl_pct": ((last - entry) / entry * 100.0) if (last is not None and entry) else None,
                        "stop": r["stop"], "target": r["target"], "time_stop_date": r["time_stop_date"],
                        "entry_ts": r["entry_ts"]})
        out["open"] = lst
    except Exception as e:
        out["open"] = []
        errors.append(f"open: {e}")

    try:
        closed = _mason_rows("closed")
        recent = sorted(closed, key=lambda r: str(r.get("exit_ts") or ""), reverse=True)[:10]
        out["closed_recent"] = [{k: r.get(k) for k in ("rec_id", "ticker", "vehicle", "tier", "horizon", "qty",
                                                        "entry_px", "exit_px", "pnl_usd", "pnl_pct", "r_multiple",
                                                        "exit_reason", "exit_ts")}
                                | {"symbol": r.get("occ") or r.get("ticker")} for r in recent]
        wins = sum(1 for r in closed if r.get("win") == 1)
        out["pnl"] = {"n_closed": len(closed), "wins": wins, "win_rate": _rate(wins, len(closed)),
                      "total_usd": round(sum(r.get("pnl_usd") or 0.0 for r in closed), 2),
                      "by_tier": _agg(closed, "tier"), "by_horizon": _agg(closed, "horizon"),
                      "by_vehicle": _agg(closed, "vehicle")}
    except Exception as e:
        out["closed_recent"], out["pnl"] = [], None
        errors.append(f"closed: {e}")

    try:
        tot = {"swing": 0.0, "intraday": 0.0, "long": 0.0}
        for r in open_rows:
            bk = _HZ_BUCKET.get(str(r.get("horizon") or ""))
            if bk:
                tot[bk] += float(r.get("cost_usd") or 0.0)
        s = sum(tot.values())
        out["horizon_mix"] = {"target": cfg.get("horizon_mix") or dict(_DEFAULT_MIX),
                              "actual_pct": {k: (round(v / s * 100.0, 1) if s else 0.0) for k, v in tot.items()}}
    except Exception as e:
        out["horizon_mix"] = None
        errors.append(f"horizon_mix: {e}")

    try:
        out["queued"] = AL._queued_ids()
    except Exception as e:
        out["queued"] = []
        errors.append(f"queued: {e}")
    try:
        out["nico"] = _NT().stats()
    except Exception as e:
        out["nico"] = None
        errors.append(f"nico: {e}")
    try:
        ls = lean_stats()
        ls.pop("since", None)
        out["lean"] = ls
    except Exception as e:
        out["lean"] = None
        errors.append(f"lean: {e}")
    if errors:
        out["errors"] = errors
    _CACHE.update(at=now, payload=out)
    return out


# ── weekly report ────────────────────────────────────────────────────────────────

def _seg_table(title, rows_week, rows_all, key) -> list:
    wk, al = _agg(rows_week, key), _agg(rows_all, key)
    L = [f"### {title}", "", "| segment | week n | week win% | week P&L | all n | all win% | all P&L |",
         "|---|---|---|---|---|---|---|"]
    for k in sorted(set(wk) | set(al)):
        w, a = wk.get(k) or {}, al.get(k) or {}
        fmt = lambda d: (f"{d['n']}", f"{d['win_rate'] * 100:.0f}%" if d.get("win_rate") is not None else "-",
                         f"${d['pnl_usd']:+,.2f}") if d else ("0", "-", "-")
        L.append("| " + " | ".join((k,) + fmt(w) + fmt(a)) + " |")
    if len(L) == 4:
        L.append("| (none) | | | | | | |")
    return L + [""]


def proposals(closed_all, lean) -> list:
    """Deterministic rules over Mason's own closed trades → [text]. Nothing is applied."""
    out = []
    seg = {}
    for r in closed_all:
        k = f"{r.get('horizon') or '?'}|{r.get('vehicle') or '?'}"
        d = seg.setdefault(k, {"n": 0, "w": 0, "pnl": 0.0})
        d["n"] += 1
        d["w"] += 1 if r.get("win") == 1 else 0
        d["pnl"] += r.get("pnl_usd") or 0.0
    for k, d in sorted(seg.items()):
        wr = d["w"] / d["n"]
        if d["n"] >= 10 and wr < 0.40:
            out.append(f"stop trading {k} — {d['w']}/{d['n']} wins ({wr * 100:.0f}%), P&L ${d['pnl']:+,.2f}")
        elif d["n"] >= 10 and wr > 0.60 and d["pnl"] > 0:
            out.append(f"raise size for {k} — {d['w']}/{d['n']} wins ({wr * 100:.0f}%), P&L ${d['pnl']:+,.2f}")
    n = len(closed_all)
    ts = sum(1 for r in closed_all if r.get("exit_reason") == "time_stop")
    if n and ts / n > 0.5:
        out.append(f"targets/stops too wide or horizon too short — time_stop closed {ts}/{n} "
                   f"({ts / n * 100:.0f}%) of trades")
    al, af = lean.get("avg_cost_lean"), lean.get("avg_cost_full")
    if al is not None and af is not None and al >= af:
        out.append(f"lean format is not saving money — lean avg ${al:.3f} (n={lean.get('n_lean')}) vs "
                   f"full avg ${af:.3f} (n={lean.get('n_full')})")
    return out


def _calib_batch(before_iso=None) -> list:
    if before_iso:
        r = _q("SELECT MAX(computed_at) m FROM agent_calibration WHERE computed_at<=?", (before_iso,))
    else:
        r = _q("SELECT MAX(computed_at) m FROM agent_calibration")
    m = r[0]["m"] if r else None
    if not m:
        return []
    return _q("SELECT segment, source, n, wins, p_cal FROM agent_calibration WHERE computed_at=?", (m,))


def weekly_report(now_et=None, force=False) -> dict:
    try:
        now = now_et or _AL()._now_et()
        if not force and (now.weekday() != 4 or (now.hour, now.minute) < (16, 30)):
            return {"skipped": "not Friday after 16:30 ET"}
        iy, iw, _ = now.isocalendar()
        week = f"{iy}-W{iw:02d}"
        path = Path(WEEKLY_DIR) / f"WEEK_{week}.md"
        _AL().ensure_schema()
        if _state_get(f"weekly:{week}") or path.exists():
            return {"skipped": "done this week", "week": week}
        monday = (now.date() - timedelta(days=now.weekday())).isoformat()
        rows = _mason_rows()
        placed = [r for r in rows if str(r.get("entry_ts") or "")[:10] >= monday]
        closed_all = [r for r in rows if r.get("status") == "closed"]
        closed_wk = [r for r in closed_all if str(r.get("exit_ts") or "")[:10] >= monday]
        wins_wk = sum(1 for r in closed_wk if r.get("win") == 1)
        pnl_wk = sum(r.get("pnl_usd") or 0.0 for r in closed_wk)
        L = [f"# Agent loop — week {week}", "", f"generated {now.isoformat(timespec='minutes')}", "",
             "## Trades", "", "| placed | closed | wins | win% | P&L |", "|---|---|---|---|---|",
             f"| {len(placed)} | {len(closed_wk)} | {wins_wk} | "
             f"{(wins_wk / len(closed_wk) * 100):.0f}% | ${pnl_wk:+,.2f} |" if closed_wk else
             f"| {len(placed)} | 0 | 0 | - | $0.00 |", ""]
        for title, key in (("By tier", "tier"), ("By horizon", "horizon"), ("By vehicle", "vehicle"),
                           ("By exit reason", "exit_reason")):
            L += _seg_table(title, closed_wk, closed_all, key)
        # calibration now vs a week ago
        nowb = _calib_batch()
        agob = {(r["segment"], r["source"]): r for r in
                _calib_batch((now - timedelta(days=7)).replace(tzinfo=None).isoformat(timespec="seconds"))}
        L += ["## Calibration (now vs a week ago)", "", "| segment | source | n | wins | p_cal | p_cal week ago |",
              "|---|---|---|---|---|---|"]
        for r in nowb:
            a = agob.get((r["segment"], r["source"]))
            pc = f"{r['p_cal']:.3f}" if r["p_cal"] is not None else "-"
            pa = f"{a['p_cal']:.3f}" if (a and a["p_cal"] is not None) else "-"
            L.append(f"| {r['segment']} | {r['source']} | {r['n']} | {r['wins']} | {pc} | {pa} |")
        if not nowb:
            L.append("| (no snapshot yet) | | | | | |")
        # spend
        D = _D()
        v2 = D.v2_cfg()
        led = D._ledger_rows()
        wk_usd = sum(float(r.get("cost_usd") or 0) for r in led if str(r.get("day") or "") >= monday
                     and str(r.get("day") or "") <= now.date().isoformat())
        sp = D.spend(now)
        lean = lean_stats()
        fmt = lambda x: f"${x:.3f}" if x is not None else "-"
        L += ["", "## Opus spend", "", "| week | month | daily cap | monthly cap | lean avg (n) | full avg (n) |",
              "|---|---|---|---|---|---|",
              f"| ${wk_usd:.2f} | ${sp.get('month_usd', 0):.2f} | ${float(v2.get('daily_usd_cap') or 0):.2f} | "
              f"${float(v2.get('monthly_usd_cap') or 0):.2f} | {fmt(lean['avg_cost_lean'])} ({lean['n_lean']}) | "
              f"{fmt(lean['avg_cost_full'])} ({lean['n_full']}) |", ""]
        # Nico's thesis
        ns = _NT().stats()
        nwr = f"{ns['win_rate'] * 100:.0f}%" if ns.get("win_rate") is not None else "-"
        L += ["## Nico's thesis (separate source)", "", "| open | closed | wins | win% | P&L |", "|---|---|---|---|---|",
              f"| {ns.get('n_open', 0)} | {ns.get('n_closed', 0)} | {ns.get('wins', 0)} | {nwr} | "
              f"${float(ns.get('pnl_usd') or 0):+,.2f} |", ""]
        props = proposals(closed_all, lean)
        L += [f"## {PROPOSAL_HEADER}", ""]
        L += [f"- {p}" for p in props] if props else \
            [f"No change proposed (not enough closed trades: n={len(closed_all)})."]
        Path(WEEKLY_DIR).mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(L) + "\n", encoding="utf-8")
        _state_set(f"weekly:{week}", str(path))
        summary = [f"Week {week}: placed {len(placed)}, closed {len(closed_wk)}, wins {wins_wk}, "
                   f"P&L ${pnl_wk:+,.2f}",
                   f"All-time own: closed {len(closed_all)}, "
                   f"wins {sum(1 for r in closed_all if r.get('win') == 1)}",
                   f"Opus spend: week ${wk_usd:.2f}, month ${sp.get('month_usd', 0):.2f} "
                   f"of ${float(v2.get('monthly_usd_cap') or 0):.2f}",
                   f"Lean avg {fmt(lean['avg_cost_lean'])} vs full avg {fmt(lean['avg_cost_full'])}",
                   f"Nico's thesis: closed {ns.get('n_closed', 0)}, wins {ns.get('wins', 0)}, "
                   f"P&L ${float(ns.get('pnl_usd') or 0):+,.2f}",
                   f"Proposals: {len(props)} (none applied)" if props else "Proposals: none",
                   f"Report: docs/fable/weekly/{path.name}"]
        try:
            _notify("agent_weekly", f"Agent loop weekly report {week}", "\n".join(summary), f"agent_weekly:{week}")
        except Exception:
            pass
        return {"ok": True, "week": week, "path": str(path), "proposals": props}
    except Exception as e:
        return {"error": f"weekly_report: {type(e).__name__}: {e}"}


# ── lean auto-switch ─────────────────────────────────────────────────────────────

def _trading_day(d: str, holidays) -> bool:
    try:
        x = date.fromisoformat(d)
        return x.weekday() < 5 and x not in holidays and d not in holidays
    except Exception:
        return False


def _v2_node(cfg: dict):
    return (((cfg.get("analyst_desk") or {}).get("opus_director") or {}).get("v2"))


def maybe_switch_lean(now_et=None) -> dict:
    try:
        cfg_path = Path(_AT().CONFIG_PATH)
        raw = cfg_path.read_text(encoding="utf-8")
        cfg = json.loads(raw)
        kinds = list(_D().v2_cfg(cfg).get("lean_kinds") or [])
        if kinds != ["anchor_1300"]:
            return {"skipped": "lean_kinds is not ['anchor_1300']", "lean_kinds": kinds}
        hol = _AL()._holidays()
        runs = _director_runs()
        lean = [r for r in runs if r["lean"] and r["kind"] == "anchor_1300"]
        days = sorted({r["day"] for r in lean if _trading_day(r["day"], hol)})
        if len(days) < 2:
            return {"skipped": "fewer than 2 trading days of lean anchor_1300 runs", "days": days}
        first = lean[0]["day"]
        full = [r for r in runs if not r["lean"] and r["kind"].startswith("anchor") and r["day"] >= first
                and r["cost"] is not None]
        reasons = []
        if any(str(r.get("parse_error") or "").strip() for r in lean):
            reasons.append(("parse_error", "a lean run had a parse error"))
        if any(not isinstance(r["n_pred"], (int, float)) or r["n_pred"] < 1 for r in lean):
            reasons.append(("no_predictions", "a lean run produced no predictions"))
        lc = [r["cost"] for r in lean if r["cost"] is not None]
        avg_l = sum(lc) / len(lc) if lc else None
        avg_f = sum(r["cost"] for r in full) / len(full) if full else None
        if avg_l is None or avg_f is None:
            reasons.append(("no_cost", "no cost to compare (lean or full runs missing)"))
        elif not avg_l < avg_f:
            reasons.append(("cost", f"lean avg ${avg_l:.3f} is not below full avg ${avg_f:.3f}"))
        if reasons:
            key = "lean_switch_fail:" + ",".join(k for k, _ in reasons)
            if not _state_get(key):
                _notify("system", "Lean format NOT switched on for the morning call",
                        "Kept lean_kinds ['anchor_1300']: " + "; ".join(t for _, t in reasons),
                        "agent_lean_switch_fail")
                _state_set(key, "notified")
            return {"switched": False, "reasons": [t for _, t in reasons]}
        # round-trip check: refuse unless the file is exactly what the app would write
        if json.dumps(cfg, indent=2, ensure_ascii=False) + "\n" != raw:
            return {"switched": False, "refused": "config file does not round-trip; not writing"}
        node = _v2_node(cfg)
        if not isinstance(node, dict) or list(node.get("lean_kinds") or []) != ["anchor_1300"]:
            return {"switched": False, "refused": "lean_kinds not found at analyst_desk.opus_director.v2"}
        node["lean_kinds"] = ["all"]
        new = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".mason.config.", suffix=".tmp", dir=str(cfg_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new)
            try:
                os.chmod(tmp, os.stat(cfg_path).st_mode & 0o7777)
            except Exception:
                pass
            os.replace(tmp, cfg_path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        n = len(lc) + len(full)
        msg = (f"Lean format now on for both daily calls: lean avg ${avg_l:.3f} vs full avg "
               f"${avg_f:.3f} over {n} runs")
        if not _state_get("lean_switch_done"):
            _notify("system", msg, msg + " (lean_kinds ['anchor_1300'] -> ['all'])", "agent_lean_switch")
            _state_set("lean_switch_done", "notified")
        return {"switched": True, "lean_kinds": ["all"], "avg_lean": avg_l, "avg_full": avg_f, "n": n}
    except Exception as e:
        return {"error": f"maybe_switch_lean: {type(e).__name__}"}


# ── tick ─────────────────────────────────────────────────────────────────────────

def tick(now_et=None) -> dict:
    global _LAST_LEAN_CHECK
    out = {}
    try:
        out["nico_sync"] = _NT().sync(now_et)          # throttled to once / 15 min inside
    except Exception as e:
        out["nico_sync"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["weekly"] = weekly_report(now_et)
    except Exception as e:
        out["weekly"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        if time.time() - _LAST_LEAN_CHECK >= LEAN_CHECK_EVERY_S:
            _LAST_LEAN_CHECK = time.time()
            out["lean"] = maybe_switch_lean(now_et)
        else:
            out["lean"] = {"skipped": "throttled"}
    except Exception as e:
        out["lean"] = {"error": f"{type(e).__name__}: {e}"}
    return out
