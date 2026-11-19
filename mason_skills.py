"""
mason_skills.py — Phase 3: runs the vendored Claude trading skills as Mason's
server-side data + idea backends.

Each skill is a self-contained Python package under Mason/skills/<name>/scripts/
with a CLI entry point that writes a timestamped JSON report to --output-dir.
This module runs them as subprocesses (FMP key injected via env), reads the
newest JSON each produced, and caches results — screeners are slow and
quota-heavy, so we don't re-run them on every brief.

position-sizer is pure Python, so it's imported and called directly.

Everything degrades gracefully: a missing key, a missing dep, a timeout, or a
non-zero exit returns {"ok": False, "error": ...} and never raises into Mason.

Public API:
    run_skill(name, extra_args=None, ttl=..., timeout=...) -> dict
    run_breadth() / run_vcp() / run_canslim() / run_market_top()
    run_regime() / run_theme() / run_earnings()            -> dict
    size_position(account, entry, stop, risk_pct, ...)      -> dict
    last_cached(name) -> dict | None
"""

import json
import os
import sys
import glob
import time
import subprocess
from pathlib import Path

BASE       = Path(__file__).parent
SKILLS_DIR = BASE / "skills"
REPORTS    = BASE / "skill_reports"
CACHE_FILE = BASE / "skill_cache.json"
REPORTS.mkdir(exist_ok=True)

# name -> (skill_folder, entry_script, default_args, default_ttl_seconds, needs_fmp)
REGISTRY = {
    "breadth":    ("market-breadth-analyzer",       "market_breadth_analyzer.py", [],                 900,  False),
    "vcp":        ("vcp-screener",                   "screen_vcp.py",              ["--strict"],       21600, True),
    "canslim":    ("canslim-screener",               "screen_canslim.py",          ["--top", "10"],    21600, True),
    "market_top": ("market-top-detector",            "market_top_detector.py",     [],                 3600, True),
    "regime":     ("macro-regime-detector",          "macro_regime_detector.py",   [],                 3600, True),
    "theme":      ("theme-detector",                 "theme_detector.py",          [],                 7200, True),
    "earnings":   ("earnings-trade-analyzer",        "analyze_earnings_trades.py", [],                 21600, True),
    "druckenmiller": ("stanley-druckenmiller-investment", "strategy_synthesizer.py", [],               3600, False),
}

_mem_cache = {}   # name -> {"ts": epoch, "data": {...}}


# ── config / env ────────────────────────────────────────────────────────────────

def _cfg():
    try:
        return json.loads((BASE / "mason.config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmp_key():
    import mason_secrets as _S    # B1b: env > secrets file > config (byte-exact legacy)
    return _S.get("FMP_API_KEY", "", cfg=_cfg())


def _skill_env():
    # RULINGS 1.6 (H2): third-party skill scripts get a copy of the environment with every known
    # secret env var removed; only the two keys the skills use are put back explicitly below.
    import mason_secrets as _S
    env = {**_S.scrubbed_env(keep=()), "PYTHONIOENCODING": "utf-8"}
    k = _fmp_key()
    if k:
        env["FMP_API_KEY"] = k
    fv = _S.get("FINVIZ_API_KEY", "", cfg=_cfg())
    if fv:
        env["FINVIZ_API_KEY"] = fv
    return env


# ── disk cache (survives restarts so the brief has data immediately) ─────────────

def _load_disk_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_disk_cache(d):
    try:
        CACHE_FILE.write_text(json.dumps(d, default=str), encoding="utf-8")
    except Exception:
        pass


def last_cached(name):
    if name in _mem_cache:
        return _mem_cache[name].get("data")
    disk = _load_disk_cache().get(name)
    return disk.get("data") if disk else None


def _cache_get(name, ttl):
    ent = _mem_cache.get(name)
    if not ent:
        ent = _load_disk_cache().get(name)
    if ent and (time.time() - ent.get("ts", 0)) < ttl:
        return ent.get("data")
    return None


def _cache_put(name, data):
    _mem_cache[name] = {"ts": time.time(), "data": data}
    disk = _load_disk_cache()
    disk[name] = _mem_cache[name]
    _save_disk_cache(disk)


# ── subprocess runner ────────────────────────────────────────────────────────────

def _latest_json(outdir: Path):
    files = sorted(glob.glob(str(outdir / "*.json")), key=os.path.getmtime)
    # skip the skill's own rolling history files if any
    files = [f for f in files if "history" not in os.path.basename(f).lower()]
    if not files:
        return None
    try:
        return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    except Exception:
        return None


def run_skill(name, extra_args=None, ttl=None, timeout=300, force=False):
    """Run a registered skill; return {"ok":bool, "data":..., "cached":bool, "error":...}."""
    if name not in REGISTRY:
        return {"ok": False, "error": f"unknown skill '{name}'"}
    folder, entry, defaults, default_ttl, needs_fmp = REGISTRY[name]
    ttl = default_ttl if ttl is None else ttl

    if not force:
        cached = _cache_get(name, ttl)
        if cached is not None:
            return {"ok": True, "data": cached, "cached": True}

    if needs_fmp and not _fmp_key():
        # fall back to any stale cached copy rather than nothing
        stale = last_cached(name)
        return {"ok": bool(stale), "data": stale, "cached": True,
                "error": "no FMP key — returning last cached" if stale else "no FMP key"}

    script = SKILLS_DIR / folder / "scripts" / entry
    if not script.exists():
        return {"ok": False, "error": f"skill script missing: {script}"}

    outdir = REPORTS / name
    outdir.mkdir(parents=True, exist_ok=True)
    args = list(defaults if extra_args is None else extra_args) + ["--output-dir", str(outdir)]
    try:
        r = subprocess.run(
            [sys.executable, str(script)] + args,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(script.parent), env=_skill_env(),
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        stale = last_cached(name)
        return {"ok": bool(stale), "data": stale, "cached": True,
                "error": f"{name} timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{name} run error: {e}"}

    data = _latest_json(outdir)
    if data is None:
        return {"ok": False, "error": f"{name} produced no JSON (rc={r.returncode}): "
                                      f"{(r.stderr or '')[-300:]}"}
    _cache_put(name, data)
    return {"ok": True, "data": data, "cached": False}


# ── convenience wrappers ─────────────────────────────────────────────────────────

def run_breadth(**kw):    return run_skill("breadth", **kw)
def run_vcp(**kw):        return run_skill("vcp", **kw)
def run_canslim(**kw):    return run_skill("canslim", **kw)
def run_market_top(**kw): return run_skill("market_top", **kw)
def run_regime(**kw):     return run_skill("regime", **kw)
def run_theme(**kw):      return run_skill("theme", **kw)
def run_earnings(**kw):   return run_skill("earnings", **kw)


# ── position sizing (pure Python — imported directly) ────────────────────────────

def size_position(account, entry, stop, risk_pct=1.0, max_pos_pct=None,
                  sector=None, current_sector_exposure=0.0):
    try:
        scripts = str(SKILLS_DIR / "position-sizer" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import position_sizer as ps
        params = ps.SizingParameters(
            account_size=float(account), entry_price=float(entry), stop_price=float(stop),
            risk_pct=float(risk_pct), max_position_pct=max_pos_pct,
            sector=sector, current_sector_exposure=current_sector_exposure,
        )
        res = ps.calculate_position(params)
        return {"ok": True, "data": res}
    except Exception as e:
        return {"ok": False, "error": f"position-sizer error: {e}"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("skill", nargs="?", default="breadth")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.skill == "size":
        print(json.dumps(size_position(600, 58, 52, 2.0, max_pos_pct=25), indent=2, default=str))
    else:
        out = run_skill(a.skill, force=a.force)
        print(json.dumps({k: (v if k != "data" else "…json…") for k, v in out.items()}, indent=2, default=str))
