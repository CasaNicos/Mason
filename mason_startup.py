"""
mason_startup.py — Mason's boot refresh.

Mason runs on startup (not always-on, no schedules), so everything that keeps him
current happens once when he loads. refresh_all() runs three steps, in order,
each isolated so one failure never aborts the others:

  1. POSTURE   — re-run mason_market_posture.py and refresh .posture_cache.json so
                 the regime gate reads today's market the moment Mason opens.
  2. RECONCILE — pull Alpaca's real fills/positions (Alpaca = source of truth) and
                 sync the trade journal to them (mason_execution.reconcile()).
  3. LEARN     — resolve any open trades against real price action and recompute the
                 calibrated track record (mason_learning.resolve_open()).

Result: insight, truth, and confidence are all fresh at load. Designed to run in a
daemon thread so it never blocks the server; writes .mason_boot.json so the UI/brief
can show "as of boot" and what was refreshed. Network-dependent steps degrade
gracefully (offline → that step reports an error, the rest still run).

Public API:
    refresh_all(timeout_posture=70) -> dict
    last_boot_status()              -> dict | None
    run_in_background()             -> threading.Thread
"""

import json
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

BASE          = Path(__file__).parent
POSTURE_CACHE = BASE / ".posture_cache.json"
BOOT_STATUS   = BASE / ".mason_boot.json"


# ── step 1: posture ──────────────────────────────────────────────────────────────

def _refresh_posture(timeout=70):
    script = BASE / "mason_market_posture.py"
    if not script.exists():
        return {"ok": False, "error": "mason_market_posture.py missing"}
    try:
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(BASE),
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if not out:
            return {"ok": False, "error": f"no output (rc={r.returncode}): {(r.stderr or '')[-200:]}"}
        data = json.loads(out)
        if data.get("error"):
            return {"ok": False, "error": data["error"]}
        # Persist the fresh posture so the regime gate / brief read it immediately.
        POSTURE_CACHE.write_text(json.dumps(data), encoding="utf-8")
        zone = (data.get("top_risk", {}) or {}).get("zone")
        score = (data.get("top_risk", {}) or {}).get("score")
        regime = (data.get("sector", {}) or {}).get("regime")
        return {"ok": True, "zone": zone, "score": score, "regime": regime,
                "as_of": data.get("as_of")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"posture refresh timed out ({timeout}s) — kept last cache"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── step 2: reconcile with Alpaca (source of truth) ──────────────────────────────

def _reconcile():
    try:
        import mason_execution as EX
        return EX.reconcile()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── step 3: resolve + recompute the learning ledger ──────────────────────────────

def _resolve_learning():
    try:
        import mason_learning as LE
        res = LE.resolve_open()
        if isinstance(res, dict):
            res.setdefault("ok", True)   # step succeeded if resolve_open() didn't raise
        # Attach a one-line snapshot of the calibrated record for the boot summary.
        try:
            tr = LE.track_record()
            res["overall"] = tr.get("overall")
        except Exception:
            pass
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── step 4: capture today's market read into the intelligence layer ──────────────

def _capture_intelligence():
    """Snapshot today's market read into the growing intelligence layer (deduped to one
    per day), then grade any past reads now old enough. Runs AFTER posture so the fresh
    .posture_cache.json is what gets captured. Isolated — never aborts the other steps."""
    try:
        import mason_intelligence as MI
        snap = MI.capture_snapshot(source="boot")
        graded = MI.grade_snapshots()
        return {"ok": True, "date": snap.get("date"), "regime": snap.get("regime"),
                "zone": snap.get("top_risk_zone"), "merged": bool(snap.get("merged")),
                "graded": graded.get("graded", 0)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── orchestrator ─────────────────────────────────────────────────────────────────

def refresh_all(timeout_posture=70, _steps=None):
    """Run the boot refresh. Each step isolated; always returns a summary."""
    steps = _steps or {
        "posture":      lambda: _refresh_posture(timeout_posture),
        "reconcile":    _reconcile,
        "learning":     _resolve_learning,
        "intelligence": _capture_intelligence,   # after posture: cache is fresh
    }
    summary = {"started": datetime.now().isoformat(), "steps": {}}
    for name, fn in steps.items():
        try:
            summary["steps"][name] = fn()
        except Exception as e:
            summary["steps"][name] = {"ok": False, "error": str(e)}
    summary["finished"] = datetime.now().isoformat()
    summary["ok"] = all((summary["steps"][s] or {}).get("ok") for s in summary["steps"])
    _write_boot_status(summary)
    return summary


def _write_boot_status(summary):
    try:
        BOOT_STATUS.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def last_boot_status():
    try:
        return json.loads(BOOT_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_in_background():
    """Spawn refresh_all on a daemon thread (never blocks the server). Returns the thread."""
    import threading
    t = threading.Thread(target=refresh_all, daemon=True, name="startup-refresh")
    t.start()
    return t


def summary_line(summary=None):
    s = summary or last_boot_status() or {}
    st = s.get("steps", {})
    p = st.get("posture", {}); rc = st.get("reconcile", {}); ln = st.get("learning", {})
    bits = []
    if p.get("ok"):
        bits.append(f"posture {p.get('zone')} {p.get('score')}/100")
    else:
        bits.append("posture stale")
    if rc.get("ok"):
        bits.append(f"reconciled {rc.get('fills_seen',0)} fills/{rc.get('positions',0)} pos")
    else:
        bits.append("reconcile offline")
    ov = (ln or {}).get("overall") or {}
    if ov:
        bits.append(f"record {ov.get('n',0)} trades · calib {ov.get('calib_win_rate')}")
    intel = st.get("intelligence", {})
    if intel.get("ok"):
        bits.append(f"intel {intel.get('regime') or '?'} snapshot")
    return "Boot refresh: " + " · ".join(bits)


# ── self-test (offline; injects mock steps, verifies isolation + boot status) ────

def _self_test():
    global BOOT_STATUS
    real = BOOT_STATUS
    BOOT_STATUS = BASE / ".mason_boot_selftest.json"
    try:
        # one step deliberately raises — the others must still run and be recorded
        def good_posture(): return {"ok": True, "zone": "Yellow", "score": 32, "regime": "Cyclical"}
        def boom():         raise RuntimeError("simulated offline")
        def good_learn():   return {"ok": True, "note": "resolved 0", "overall": {"n": 5, "calib_win_rate": 0.58}}
        summ = refresh_all(_steps={"posture": good_posture, "reconcile": boom, "learning": good_learn})
        assert summ["steps"]["posture"]["ok"] is True
        assert summ["steps"]["reconcile"]["ok"] is False and "simulated" in summ["steps"]["reconcile"]["error"]
        assert summ["steps"]["learning"]["ok"] is True
        assert summ["ok"] is False                      # overall fails if any step failed
        assert last_boot_status()["steps"]["posture"]["zone"] == "Yellow"
        print("SELF-TEST OK")
        print(" ", summary_line(summ))
    finally:
        try:
            (BASE / ".mason_boot_selftest.json").unlink()
        except Exception:
            pass
        BOOT_STATUS = real


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "status", "selftest"])
    a = ap.parse_args()
    if a.cmd == "selftest":
        _self_test()
    elif a.cmd == "status":
        print(json.dumps(last_boot_status(), indent=2, default=str))
    else:
        s = refresh_all()
        print(summary_line(s))
        print(json.dumps(s, indent=2, default=str))
