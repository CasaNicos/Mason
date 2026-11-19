"""mason_kb_priors.py — shared priors library (RH Expansion S1-T5, X-G3).

Spec: docs/robinhood/RH_EXPANSION_S1_DATA_BACKFILL.md §2 T5; contracts §5 kb_priors, §6 /api/kb/priors,
§7 rh_expansion.priors. Every kb_priors row in Mason is written through upsert_prior() here (S1: personal,
excursion, pead, regime; S2: politician, crowd, behavior, scanner). Nothing else writes the table.

Public API
  upsert_prior(family, key, window, samples, *, summary_fmt=None, refs=None, withheld=0,
               excess_vs_spy=None) -> row dict | None
      id = sha1(f"{family}|{json.dumps(key, sort_keys=True)}|{window}")[:16]; a recompute is an upsert
      (and revives a retired row). Computes over the FINITE samples (None/NaN/inf/non-numbers are dropped
      and counted in stat.dropped_nonfinite):
        n, mean, median, hit_rate (share of samples > 0), sd, ci95 (bootstrap of the mean, numpy,
        seeded, 2,000 resamples), shrunk_mean (empirical Bayes, see below), min_n_met (n >= priors.min_n).
      Returns the stored row as a dict; row["stat_json"] is a dict (callers accept dict or str).
      Edge cases (ruling 6): n=0 → mean/median/hit_rate/sd/ci95/shrunk_mean are None, min_n_met=0, the row is
      still stored so the family's coverage is visible. n=1 → ci95 is the degenerate [x, x], sd=0.
  context_block(families, tickers=None, max_lines=12) -> str
      Prompt text for S2's integrator. Only live rows (record_deleted=0) with min_n_met=1 AND n >= the
      CURRENT priors.min_n; rows keyed to an excluded account (personal_scope.exclude_accounts, on key field
      "account"/"account_label"/"account_mask", str or list) are skipped. Strongest effect first: |shrunk_mean| / standard error.
      Every line carries "n=<n>". Plain text: no HTML, no control characters, JSON-safe. "" when off/empty.
      `tickers`: rows whose key has a ticker/symbol/underlying field must match; rows without one are kept.
  query(family=None, limit=500) -> [api rows]       (what /api/kb/priors returns as `data`)
  begin_run(family) -> run_id ; retire_unrefreshed(family, run_id) -> int ; retire_except(family, keep_ids)
  prior_id(family, key, window) -> str ; status() -> dict ; bp (Flask Blueprint, GET /api/kb/priors)

Retiring stale keys (Debugger Q2 on T2). A recompute only touches the keys it produces, so a key that
stops being produced (e.g. per-account "Household" rows after Household is excluded again) would linger. Each family
writer brackets its recompute:
      run = mason_kb_priors.begin_run("personal")       # before the first upsert_prior of the pass
      ... upsert_prior("personal", ...) for every bucket ...
      if pass_had_no_errors:
          mason_kb_priors.retire_unrefreshed("personal", run)
  retire_unrefreshed soft-deletes (record_deleted=1) every live row of that family NOT upserted since
  begin_run. It retires nothing when run_id is not the family's open run (stale/unknown/None), and it
  consumes the run. A pass with zero buckets legitimately retires the whole family. Skip the retire call
  when the pass hit errors, or the failed buckets would be retired too. retire_except(family, keep_ids) is
  the stateless alternative for writers that already know their full id set (prior_id() builds ids).
  Retired rows are invisible to context_block, query and /api/kb/priors and leave the shrinkage pool.
  Defence in depth for §2.8: context_block/query also hide rows whose key "account" is excluded, so even a
  writer that never calls retire cannot leak Household's stats into Nico's prompts.

Shrinkage (ruling 5). shrunk_mean = w*mean + (1-w)*pool_mean, w = n/(n+k), k = priors.shrink_k (default 10).
  The pool is the family's live rows with the same window and the same key["metric"] (when the key has a
  "metric"; the excursion family mixes % and session metrics, which must not be averaged together).
  pool_mean = n-weighted mean of the pool's row means, over pool MEMBERS only: rows whose key account
  (account/account_label/account_mask, str or list) is "all" (an aggregate of other rows — T2 writes both
  per-account and account:"all" rows from the same trades, which would double count) or an excluded account
  (personal_scope.exclude_accounts) do not feed the mean, but are still shrunk toward it. Fallback when no
  member has data: in-scope rows incl. aggregates, then every row. The whole pool is re-shrunk after every
  upsert and every retire, so the stored value does not depend on upsert order (a later exclude_accounts
  flip is applied lazily: each row's stat._scope stamps the exclude_accounts set it was shrunk under, and
  query/context_block/the API/begin_run re-shrink (and re-render) any family whose stamp differs, once,
  before serving — T5-a1).

Personal-data aggregates after a scope flip (A1-D1). Rows built from Nico's RH samples — is_personal_data():
  family personal/behavior, regime key metric=personal_hit_rate, excursion key src=personal (extend
  PERSONAL_DATA_FAMILIES / PERSONAL_DATA_KEY_RULES for future S2 personal writers) — carry stat._sample_scope,
  the exclude_accounts stamp their SAMPLES were selected under (set at upsert, never touched by _reshrink).
  An aggregate of them (no single concrete account key: account "all", no account field, or a list) whose
  _sample_scope differs from the current scope is hidden from query/context_block/the API and kept out of
  the shrinkage pool until its writer re-upserts it — re-shrinking cannot remove Household's samples from it.
  Rows keyed to one concrete account (e.g. "Play Money") stay visible: their samples cannot change with scope.
  Market/paper rows (regime spx_fwd_20s, excursion src paper/rec, pead, crowd, politician, scanner) are never
  hidden. DEPLOY NOTE: rows written before T5-a1 have no _sample_scope, so personal-data aggregates are
  hidden after the first deploy until each writer (T2 personal_priors, T4 excursion_priors, T7 regimes,
  S2 behavior) has run once. That is the safe direction (no Household data reaches prompts).

Keys (DEBUG_T5 D1). key_json is hashed and stored raw (json.dumps(key, sort_keys=True)): keys are writer
  labels such as scan ids, CIKs or dates and are never redacted. A key whose account/account_label/
  account_mask value contains a full 8-12 digit account number is refused (None + logged error); use the
  account label. Redaction applies to summaries, source_refs, the stored template and errors.
  /api/kb/priors escapes <, > and & as \\u003c/\\u003e/\\u0026 in the JSON body (D2), so key values come
  back exact but no response carries raw HTML.

Summaries (ruling 1). summary_fmt is a str.format template. Placeholders: every stat key (n, mean, median,
  hit_rate, ci95_lo, ci95_hi, sd, shrunk_mean, family_mean, excess_vs_spy, withheld), plus family, window and
  each scalar key field (stats win a name clash). An unknown placeholder, positional/attribute/index fields,
  a bad format spec, a None value under a numeric spec, or a non-str template never raises: the row gets a
  default summary and stat.fallback=true. Every stored summary contains n: when the template does not use
  {n} and the text has no "n=<n>", " (n=<n>)" is appended. withheld > 0 (ruling 3, e.g. delisted
  mid-horizon) adds "; <k> withheld" unless the template uses {withheld}. HTML angle brackets, braces and
  control characters are stripped; account-number-length digit runs are masked to the last 4.

Config (ruling 7): rh_expansion.priors.{enabled (default false), min_n (10), shrink_k (10)}, read per call
  through mason_robinhood._config() (mtime-cached). The robinhood.enabled master switch does NOT apply:
  priors are local math over rows already in mason.db and never touch Robinhood. With priors.enabled false
  every public function is a cheap no-op (upsert_prior → None, context_block → "", query → [],
  retire/begin → 0/None, route → {"enabled": false}) and the DB is not opened.

Never raises from a public function; every connection is closed; dependencies: numpy (already installed).
"""

import hashlib
import json
import logging
import math
import re
import string
import threading
import uuid
from datetime import datetime, timezone

import numpy as np

import mason_db
import mason_robinhood as _RH

log = logging.getLogger("mason.kb_priors")

CREATED_BY = "mason_kb_priors"
NAME = "kb_priors"
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260930
_BOOT_CELLS = 2_000_000           # resample-matrix cells per chunk (bounds memory for huge n)
MAX_REFS = 500                    # source_refs kept per row (total in stat._refs_total)
MAX_ROWS = 20000                  # read cap for query/context/pool scans
MAX_API_ROWS = 2000
MAX_CONTEXT_LINES = 50
MAX_SUMMARY_CHARS = 400
_TICKER_FIELDS = ("ticker", "symbol", "underlying")
_ACCOUNT_FIELDS = ("account", "account_label", "account_mask")
_STAT_PLACEHOLDERS = ("n", "mean", "median", "hit_rate", "ci95_lo", "ci95_hi", "sd", "shrunk_mean",
                      "family_mean", "excess_vs_spy", "withheld")

_DEFAULTS = {"enabled": False, "min_n": 10, "shrink_k": 10}

_ACCT_RE = re.compile(r"(?<![\w.])\d{8,12}(?![\w.])")     # standalone account-number-length runs
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_N_EQ_RE = re.compile(r"\bn\s*=\s*(\d+)")

_lock = threading.RLock()
_runs = {}                         # family -> {"run_id", "ids": set()}
_state = {"errors": [], "last_run": None}


def _reset_state_for_tests():
    with _lock:
        _runs.clear()
        _state.update(errors=[], last_run=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact(s) -> str:
    return _ACCT_RE.sub(lambda m: "••" + m.group(0)[-4:], str(s))


def _err(where, e=None) -> str:
    msg = _redact(f"{where}: {type(e).__name__}: {e}" if e is not None else str(where))[:300]
    with _lock:
        _state["errors"] = (_state["errors"] + [msg])[-50:]
    log.warning(f"[kb_priors] {msg}")
    return msg


# ── config ───────────────────────────────────────────────────────────────────
def _rhx() -> dict:
    try:
        return _RH._config().get("rh_expansion") or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        for k, v in (_rhx().get("priors") or {}).items():
            if v is not None and not str(k).startswith("_"):
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    """rh_expansion.priors.enabled only — the robinhood.enabled master switch does not apply (local math)."""
    try:
        return _cfg().get("enabled") is True
    except Exception:
        return False


def _min_n() -> int:
    try:
        return max(1, int(_cfg().get("min_n")))
    except (TypeError, ValueError):
        return 10


def _shrink_k() -> float:
    try:
        k = float(_cfg().get("shrink_k"))
        return k if math.isfinite(k) and k >= 0 else 10.0
    except (TypeError, ValueError):
        return 10.0


def _scope_hash(excl=None) -> str:
    """Stamp of the normalized exclude_accounts set that a row's shrunk_mean/family_mean was computed under."""
    excl = _exclude_accounts() if excl is None else excl
    return hashlib.sha1(json.dumps(sorted(excl)).encode()).hexdigest()[:12]


def _exclude_accounts() -> set:
    """Normalized excluded labels via the shared parser (mason_robinhood, RULINGS #1). Fails closed."""
    try:
        return set(_RH.resolve_exclude_accounts(_RH._config().get("rh_expansion"),
                                                log=lambda m: _err(m), source=CREATED_BY))
    except Exception:
        return {"household"}


# ── math ─────────────────────────────────────────────────────────────────────
def _finite(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _bootstrap_ci(arr):
    """Percentile CI95 of the mean, seeded (same samples → same CI). n=0 → None, n=1 → [x, x]."""
    n = len(arr)
    if n == 0:
        return None
    if n == 1:
        return [float(arr[0]), float(arr[0])]
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    chunk = max(1, min(BOOTSTRAP_RESAMPLES, _BOOT_CELLS // n))
    means, done = [], 0
    while done < BOOTSTRAP_RESAMPLES:
        m = min(chunk, BOOTSTRAP_RESAMPLES - done)
        means.append(arr[rng.integers(0, n, size=(m, n))].mean(axis=1))
        done += m
    allm = np.concatenate(means)
    lo, hi = np.percentile(allm, [2.5, 97.5])
    return [float(lo), float(hi)]


def _stats(samples) -> dict:
    """Pure stats over the finite samples (see module docstring for n=0 / n=1)."""
    raw = list(samples or [])
    xs = [f for f in (_finite(v) for v in raw) if f is not None]
    n = len(xs)
    out = {"n": n, "mean": None, "median": None, "hit_rate": None, "sd": None, "ci95": None,
           "dropped_nonfinite": len(raw) - n}
    if n:
        arr = np.asarray(xs, dtype=float)
        out.update(mean=float(arr.mean()), median=float(np.median(arr)),
                   hit_rate=float((arr > 0).sum()) / n,
                   sd=float(arr.std(ddof=1)) if n > 1 else 0.0, ci95=_bootstrap_ci(arr))
    return out


# ── summary rendering ────────────────────────────────────────────────────────
def _clean_text(s) -> str:
    s = _CTRL_RE.sub(" ", str(s))
    s = re.sub(r"[<>{}]", "", s)
    return re.sub(r"\s+", " ", _redact(s)).strip()[:MAX_SUMMARY_CHARS]


def _fmt_vals(family, window, key, stat) -> dict:
    vals = {}
    for k, v in (key or {}).items():
        if isinstance(k, str) and (v is None or isinstance(v, (str, int, float))) and not isinstance(v, bool):
            vals[k] = v
    ci = stat.get("ci95") or [None, None]
    vals.update(family=family, window=window)
    for k in _STAT_PLACEHOLDERS:
        vals[k] = stat.get(k)
    vals["ci95_lo"], vals["ci95_hi"] = ci[0], ci[1]
    return vals


def _num(v, spec="{:.4g}"):
    return "n/a" if v is None else spec.format(v)


def _default_summary(family, window, key, stat) -> str:
    kd = ", ".join(f"{k}={v}" for k, v in sorted((key or {}).items())
                   if isinstance(v, (str, int, float)) and not isinstance(v, bool))
    hr = stat.get("hit_rate")
    return (f"{family} [{kd}] ({window}): mean {_num(stat.get('mean'))}, median {_num(stat.get('median'))}, "
            f"hit rate {'n/a' if hr is None else f'{hr:.0%}'} (n={stat.get('n', 0)})")


def _render(fmt, family, window, key, stat):
    """(summary, fallback) — never raises."""
    n = int(stat.get("n") or 0)
    fallback, fields = None, set()
    text = None
    if isinstance(fmt, str):
        try:
            for _, field, spec, conv in string.Formatter().parse(fmt):
                if field is None:
                    continue
                if not field or field.isdigit() or any(c in field for c in ".[]") or "{" in (spec or ""):
                    raise ValueError(f"unsupported field {field!r}")
                fields.add(field)
            text = fmt.format_map(_fmt_vals(family, window, key, stat))
        except Exception:
            text, fields = None, set()
    if text is None:
        fallback = True if fmt is not None else None
        text = _default_summary(family, window, key, stat)
        fields = {"n"}
    text = _clean_text(text)
    if "n" not in fields and not any(int(m) == n for m in _N_EQ_RE.findall(text)):
        text = f"{text} (n={n})"
    w = int(stat.get("withheld") or 0)
    if w > 0 and "withheld" not in fields:
        text = text[:-1] + f"; {w} withheld)" if text.endswith(f"(n={n})") else f"{text} ({w} withheld)"
    return text[:MAX_SUMMARY_CHARS + 40], fallback


# ── keys / ids ───────────────────────────────────────────────────────────────
def _key_json(key) -> str:
    """Raw, canonical key JSON: keys are writer labels (scan ids, CIKs, dates), so they are hashed and stored
    exactly as given (DEBUG_T5 D1). Full account numbers are refused upstream by _key_account_problem."""
    return json.dumps(key, sort_keys=True, default=str)


def _account_values(key) -> list:
    """Normalized account labels in a key (str or list values of account/account_label/account_mask), via
    mason_robinhood.norm_account_label — the same rule as the config side (T-D6; NFKC, Cf/fillers dropped,
    casefold). A value with no letter/digit normalizes to ''."""
    out = []
    for f in _ACCOUNT_FIELDS:
        v = key.get(f) if isinstance(key, dict) else None
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                out.append(_RH.norm_account_label(str(x)))
    return out


def _key_account_problem(key) -> bool:
    """True when an account field carries a full (8-12 digit) account number — such a key is refused."""
    return any(_ACCT_RE.search(v) for v in _account_values(key))


def _pool_member(key, excl) -> bool:
    """Rows that feed the family mean: in scope and not an aggregate ("all") of other rows (no double count)."""
    acc = _account_values(key)
    return not any(a == "all" or a in excl for a in acc)


# Personal-data rows (A1-D1): rows whose samples come from Nico's RH accounts and so depend on
# personal_scope.exclude_accounts at WRITE time. Extend here when an S2 writer adds personal samples.
PERSONAL_DATA_FAMILIES = ("personal", "behavior")
PERSONAL_DATA_KEY_RULES = {"regime": ("metric", "personal_hit_rate"), "excursion": ("src", "personal")}


def is_personal_data(family, key) -> bool:
    """True for rows built from Nico's personal (RH) samples: family personal/behavior, regime rows with
    key metric=personal_hit_rate, excursion rows with key src=personal. Market/paper rows → False."""
    if family in PERSONAL_DATA_FAMILIES:
        return True
    rule = PERSONAL_DATA_KEY_RULES.get(family)
    return bool(rule) and isinstance(key, dict) and key.get(rule[0]) == rule[1]


def _single_account(key) -> bool:
    """Keyed to exactly one concrete account (not "all"): its samples are that account's only, so the
    exclude_accounts set in force at write time cannot have changed them."""
    acc = _account_values(key)
    return len(acc) == 1 and acc[0] != "all"


def _stale_personal(family, key, stat, scope) -> bool:
    """A personal-data aggregate whose samples were selected under a different exclude_accounts set than
    `scope` (or before T5-a1 stamped _sample_scope at all). Hidden from reads and kept out of the
    shrinkage pool until its writer re-upserts it."""
    return (is_personal_data(family, key) and not _single_account(key)
            and (stat or {}).get("_sample_scope") != scope)


def prior_id(family, key, window) -> str:
    return hashlib.sha1(f"{family}|{_key_json(key)}|{window}".encode()).hexdigest()[:16]


def _loads(s, default):
    if isinstance(s, (dict, list)):
        return s
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except (TypeError, ValueError):
        return default


def _metric_of(key_json):
    k = _loads(key_json, {})
    return k.get("metric") if isinstance(k, dict) else None


# ── shrinkage pool ───────────────────────────────────────────────────────────
def _shrink_fields(stat, fm, k) -> dict:
    n, mean = int(stat.get("n") or 0), stat.get("mean")
    if not n or mean is None:
        return {"shrunk_mean": None, "family_mean": fm, "shrink_k": k}
    w = n / (n + k) if (n + k) > 0 else 1.0
    return {"shrunk_mean": float(w * mean + (1 - w) * (fm if fm is not None else mean)),
            "family_mean": fm, "shrink_k": k}


def _pool_mean(rows, excl=None, family=None, scope=None):
    """n-weighted mean over the pool's members (see _pool_member); falls back to the in-scope rows, then to
    every row, when no member has data (e.g. a pool made only of aggregate rows). Stale personal-data
    aggregates (_stale_personal) never feed the mean, in any fallback."""
    excl = set() if excl is None else excl
    scope = _scope_hash(excl) if scope is None else scope
    rows = [r for r in rows if not _stale_personal(family, r.get("_key") or {}, r.get("_stat"), scope)]
    for pick in (lambda r: _pool_member(r.get("_key") or {}, excl),
                 lambda r: not any(a in excl for a in _account_values(r.get("_key") or {})),
                 lambda r: True):
        m = _pool_mean_raw([r for r in rows if pick(r)])
        if m is not None:
            return m
    return None


def _pool_mean_raw(rows):
    num = den = 0.0
    for r in rows:
        st = r["_stat"]
        n, mean = int(st.get("n") or 0), st.get("mean")
        if n > 0 and mean is not None:
            num += n * mean
            den += n
    return (num / den) if den else None


def _reshrink(conn, family, window=None, metric=None, *, all_pools=False, k=None):
    """Recompute shrunk_mean for one pool (family, window, key.metric) or every pool in the family."""
    k = _shrink_k() if k is None else k
    excl = _exclude_accounts()
    scope = _scope_hash(excl)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, key_json, window, family, stat_json, summary FROM kb_priors"
        " WHERE family=? AND coalesce(record_deleted,0)=0 LIMIT ?", (family, MAX_ROWS))]
    pools = {}
    for r in rows:
        r["_stat"] = _loads(r["stat_json"], {})
        r["_key"] = _loads(r["key_json"], {})
        pk = (r["window"], _metric_of(r["key_json"]))
        if all_pools or pk == (window, metric):
            pools.setdefault(pk, []).append(r)
    for pool in pools.values():
        fm = _pool_mean(pool, excl, family, scope)
        for r in pool:
            st = r["_stat"]
            new = dict(_shrink_fields(st, fm, k), _scope=scope)
            if all(st.get(f) == v for f, v in new.items()):
                continue
            st.update(new)
            summary = r["summary"]
            fmt = st.get("_fmt")
            if isinstance(fmt, str) and ("shrunk_mean" in fmt or "family_mean" in fmt):
                summary, _ = _render(fmt, family, r["window"], _loads(r["key_json"], {}), st)
            conn.execute("UPDATE kb_priors SET stat_json=?, summary=? WHERE id=?",
                         (json.dumps(st, sort_keys=True), summary, r["id"]))


# ── upsert ───────────────────────────────────────────────────────────────────
_BASE_FIELDS = ("n", "mean", "median", "hit_rate", "sd", "ci95", "excess_vs_spy", "withheld",
                "dropped_nonfinite")


def upsert_prior(family, key, window, samples, *, summary_fmt=None, refs=None, withheld=0,
                 excess_vs_spy=None):
    """Compute + store one prior (see module docstring). Flag off → None. Never raises (None + logged error)."""
    if not enabled():
        return None
    try:
        if not isinstance(family, str) or not family.strip():
            _err(f"upsert_prior: bad family {family!r}")
            return None
        if not isinstance(key, dict):
            _err(f"upsert_prior {family}: key must be a dict, got {type(key).__name__}")
            return None
        if samples is None or isinstance(samples, (str, bytes, dict)):
            _err(f"upsert_prior {family}: samples must be a list")
            return None
        if _key_account_problem(key):
            _err(f"upsert_prior {family}: key carries a full account number, refused (use the account label)")
            return None
        family, window = family.strip(), str(window)
        stat = _stats(samples)
        try:
            w = int(withheld or 0)
        except (TypeError, ValueError):
            w = 0
        stat["withheld"] = max(0, w)
        stat["excess_vs_spy"] = _finite(excess_vs_spy)
        refs = list(refs or []) if not isinstance(refs, (str, bytes, dict)) else [refs]
        stat["_refs_total"] = len(refs)
        if isinstance(summary_fmt, str):
            stat["_fmt"] = _redact(summary_fmt[:1000])
        excl = _exclude_accounts()
        stat["_scope"] = _scope_hash(excl)
        stat["_sample_scope"] = stat["_scope"]      # write-time scope of the SAMPLES; _reshrink never touches it
        kj = _key_json(key)
        rid = prior_id(family, key, window)
        min_n_met = int(stat["n"] >= _min_n())
        refs_json = _redact(json.dumps(refs[:MAX_REFS], default=str))
        k = _shrink_k()
        with _lock:
            conn = mason_db.get_connection()
            try:
                old = conn.execute("SELECT version, stat_json, summary, record_deleted FROM kb_priors WHERE id=?",
                                   (rid,)).fetchone()
                pool = [dict(r) for r in conn.execute(
                    "SELECT id, key_json, stat_json FROM kb_priors WHERE family=? AND window=?"
                    " AND coalesce(record_deleted,0)=0 AND id<>? LIMIT ?", (family, window, rid, MAX_ROWS))]
                metric = key.get("metric")
                pool = [dict(r, _stat=_loads(r["stat_json"], {}), _key=_loads(r["key_json"], {})) for r in pool
                        if _metric_of(r["key_json"]) == metric] + [{"_stat": stat, "_key": key}]
                stat.update(_shrink_fields(stat, _pool_mean(pool, excl, family, stat["_scope"]), k))
                summary, fb = _render(summary_fmt, family, window, key, stat)
                if fb:
                    stat["fallback"] = True
                version = 1
                if old is not None:
                    ost = _loads(old["stat_json"], {})
                    same = (all(ost.get(f) == stat.get(f) for f in _BASE_FIELDS) and old["summary"] == summary
                            and not old["record_deleted"])
                    version = int(old["version"] or 1) + (0 if same else 1)
                now = _now_iso()
                conn.execute(
                    "INSERT INTO kb_priors (id, family, key_json, window, n, stat_json, summary, min_n_met,"
                    " computed_date, source_refs, version, created_by, record_deleted)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)"
                    " ON CONFLICT(id) DO UPDATE SET family=excluded.family, key_json=excluded.key_json,"
                    " window=excluded.window, n=excluded.n, stat_json=excluded.stat_json,"
                    " summary=excluded.summary, min_n_met=excluded.min_n_met,"
                    " computed_date=excluded.computed_date, source_refs=excluded.source_refs,"
                    " version=excluded.version, created_by=excluded.created_by, record_deleted=0",
                    (rid, family, kj, window, stat["n"], json.dumps(stat, sort_keys=True), summary, min_n_met,
                     now, refs_json, version, CREATED_BY))
                _reshrink(conn, family, window, metric, k=k)
                conn.commit()
                row = dict(conn.execute("SELECT * FROM kb_priors WHERE id=?", (rid,)).fetchone())
            finally:
                conn.close()
            run = _runs.get(family)
            if run is not None:
                run["ids"].add(rid)
            _state["last_run"] = now
        row["stat_json"] = _loads(row["stat_json"], {})
        return row
    except Exception as e:
        _err(f"upsert_prior {family!r}", e)
        return None


# ── scope refresh (T5-a1) ────────────────────────────────────────────────────
MAX_REFRESH_FAMILIES = 20


def _refresh_scope(families=None) -> int:
    """Re-shrink (and re-render) every family whose live rows were shrunk under a different exclude_accounts
    set than the current one. One cheap indexed check when nothing changed; at most MAX_REFRESH_FAMILIES
    families per call. Returns the number of families re-shrunk. Never raises (errors logged; the caller then
    serves the stored values)."""
    try:
        scope = _scope_hash()
        sql = ("SELECT DISTINCT family FROM kb_priors WHERE coalesce(record_deleted,0)=0"
               " AND coalesce(json_extract(stat_json, '$._scope'), '') <> ?")
        args = [scope]
        if families:
            sql += f" AND family IN ({','.join('?' * len(families))})"
            args += list(families)
        stale = mason_db.with_connection(lambda c: [r[0] for r in c.execute(sql + " LIMIT ?",
                                                                               args + [MAX_REFRESH_FAMILIES])])
        if not stale:
            return 0
        with _lock:
            conn = mason_db.get_connection()
            try:
                for fam in stale:
                    _reshrink(conn, fam, all_pools=True)
                conn.commit()
            finally:
                conn.close()
        log.info(f"[kb_priors] exclude_accounts changed: re-shrunk {len(stale)} famil(ies): {stale}")
        return len(stale)
    except Exception as e:
        _err("refresh_scope", e)
        return 0


# ── retire ───────────────────────────────────────────────────────────────────
def begin_run(family):
    """Open a refresh pass for `family`; returns run_id (None when off/bad). Replaces any open run.
    Also re-shrinks the family first when exclude_accounts changed since its last shrink (T5-a1)."""
    if not enabled() or not isinstance(family, str) or not family.strip():
        return None
    _refresh_scope([family.strip()])
    run_id = uuid.uuid4().hex[:12]
    with _lock:
        _runs[family.strip()] = {"run_id": run_id, "ids": set()}
    return run_id


def _retire(family, keep: set) -> int:
    with _lock:
        conn = mason_db.get_connection()
        try:
            live = [r[0] for r in conn.execute(
                "SELECT id FROM kb_priors WHERE family=? AND coalesce(record_deleted,0)=0 LIMIT ?",
                (family, MAX_ROWS))]
            dead = [i for i in live if i not in keep]
            for i in range(0, len(dead), 500):
                part = dead[i:i + 500]
                conn.execute(f"UPDATE kb_priors SET record_deleted=1 WHERE id IN ({','.join('?' * len(part))})",
                             part)
            if dead:
                _reshrink(conn, family, all_pools=True)
            conn.commit()
            return len(dead)
        finally:
            conn.close()


def retire_unrefreshed(family, run_id) -> int:
    """Soft-delete the family's live rows not upserted since begin_run(family) returned run_id. Never raises."""
    if not enabled() or not isinstance(family, str):
        return 0
    try:
        family = family.strip()
        with _lock:
            run = _runs.get(family)
            if run is None or not run_id or run["run_id"] != run_id:
                return 0
            _runs.pop(family, None)
            keep = set(run["ids"])
        n = _retire(family, keep)
        if n:
            log.info(f"[kb_priors] retired {n} stale {family} prior(s)")
        return n
    except Exception as e:
        _err(f"retire_unrefreshed {family!r}", e)
        return 0


def retire_except(family, keep_ids) -> int:
    """Soft-delete every live row of `family` whose id is not in keep_ids. Never raises."""
    if not enabled() or not isinstance(family, str):
        return 0
    try:
        return _retire(family.strip(), {str(i) for i in (keep_ids or [])})
    except Exception as e:
        _err(f"retire_except {family!r}", e)
        return 0


# ── reads ────────────────────────────────────────────────────────────────────
def _strength(st) -> float:
    n, sd = int(st.get("n") or 0), st.get("sd")
    m = st.get("shrunk_mean") if st.get("shrunk_mean") is not None else st.get("mean")
    if not n or m is None:
        return 0.0
    se = (sd or 0.0) / math.sqrt(n)
    return min(abs(m) / se, 1e9) if se > 0 else (1e9 if m else 0.0)


def _live_rows(families=None):
    """Live, in-scope rows as dicts with parsed key/stat. Raises (callers wrap). Re-shrinks first when
    exclude_accounts changed since the rows were shrunk (T5-a1), so every read serves current-scope values.
    Hides stale personal-data aggregates (A1-D1, _stale_personal) until their writer re-upserts them."""
    _refresh_scope(families)
    excl = _exclude_accounts()
    scope = _scope_hash(excl)
    sql = "SELECT * FROM kb_priors WHERE coalesce(record_deleted,0)=0"
    args = []
    if families:
        sql += f" AND family IN ({','.join('?' * len(families))})"
        args += list(families)
    sql += " ORDER BY family, id LIMIT ?"
    args.append(MAX_ROWS)
    rows = mason_db.with_connection(lambda c: [dict(r) for r in c.execute(sql, args)])
    out = []
    for r in rows:
        key, st = _loads(r["key_json"], {}), _loads(r["stat_json"], {})
        if not isinstance(key, dict) or not isinstance(st, dict):
            continue
        if any(a in excl for a in _account_values(key)):
            continue
        if _stale_personal(r["family"], key, st, scope):
            continue
        r["_key"], r["_stat"], r["_score"] = key, st, _strength(st)
        out.append(r)
    return out


def _api_row(r) -> dict:
    return {"id": r["id"], "family": r["family"], "key": r["_key"], "window": r["window"],
            "n": int(r["n"] or 0), "stat": {k: v for k, v in r["_stat"].items() if not str(k).startswith("_")},
            "summary": _clean_text(r["summary"] or ""), "min_n_met": bool(r["min_n_met"]),
            "computed_date": r["computed_date"]}


def _query(family=None, limit=500) -> list:
    try:
        limit = max(1, min(int(limit), MAX_API_ROWS))
    except (TypeError, ValueError):
        limit = 500
    rows = _live_rows([family] if family else None)
    rows.sort(key=lambda r: (r["family"], -r["_score"], -int(r["n"] or 0), r["id"]))
    return [_api_row(r) for r in rows[:limit]]


def query(family=None, limit=500) -> list:
    """Live, in-scope priors as /api/kb/priors rows. Flag off → []. Never raises."""
    if not enabled():
        return []
    try:
        return _query(family, limit)
    except Exception as e:
        _err("query", e)
        return []


def context_block(families, tickers=None, max_lines=12) -> str:
    """Prompt-safe priors text (see module docstring). Flag off / nothing qualifying → ""."""
    if not enabled():
        return ""
    try:
        if isinstance(families, str):
            families = [families]
        fams = [f for f in (families or []) if isinstance(f, str) and f] or None
        try:
            max_lines = max(1, min(int(max_lines), MAX_CONTEXT_LINES))
        except (TypeError, ValueError):
            max_lines = 12
        tick = None
        if tickers:
            tick = {str(t).strip().upper() for t in ([tickers] if isinstance(tickers, str) else tickers) if t}
        min_n = _min_n()
        picked = []
        for r in _live_rows(fams):
            n = int(r["n"] or 0)
            if not r["min_n_met"] or n < min_n:
                continue
            if tick is not None:
                tv = [str(r["_key"][f]).upper() for f in _TICKER_FIELDS if r["_key"].get(f) is not None]
                if tv and not (set(tv) & tick):
                    continue
            picked.append(r)
        picked.sort(key=lambda r: (-r["_score"], -int(r["n"] or 0), r["id"]))
        lines = []
        for r in picked[:max_lines]:
            lines.append(_clean_text(f"- [{r['family']}, n={int(r['n'] or 0)}] {r['summary'] or ''}"))
        if not lines:
            return ""
        return "\n".join(["KNOWLEDGE PRIORS (graded history; n = sample size; means shrunk toward family "
                          "average; strongest first):"] + lines)
    except Exception as e:
        _err("context_block", e)
        return ""


def status() -> dict:
    on = enabled()
    with _lock:
        out = {"name": NAME, "enabled": on, "last_run": _state["last_run"], "rows": 0, "retired": 0,
               "families": {}, "errors": list(_state["errors"]), "truncated": False, "rate_hits": 0,
               "xcheck_mismatch": 0, "open_runs": sorted(_runs)}
    if not on:
        return out
    try:
        res = mason_db.with_connection(lambda c: [tuple(r) for r in c.execute(
            "SELECT family, coalesce(record_deleted,0), count(*) FROM kb_priors GROUP BY 1, 2")])
        for fam, dead, cnt in res:
            if dead:
                out["retired"] += cnt
            else:
                out["rows"] += cnt
                out["families"][fam] = cnt
    except Exception as e:
        out["errors"].append(_err("status", e))
    return out


def jobs() -> list:
    """S1 job contract (mason_rh_history, above jobs()): nothing to schedule — the store is writer-driven.
    Every family is (re)computed by its writer's own job (personal 16:40, excursion 16:50, regime 17:05,
    pead 17:25; S2 writers likewise), each bracketing begin_run/retire_unrefreshed, and an exclude_accounts
    change re-shrinks lazily on the next read. Nothing here is time-driven and it makes no RH call."""
    return []


# ── HTTP (contracts §6) ──────────────────────────────────────────────────────
try:
    from flask import Blueprint, Response, request

    bp = Blueprint("rhx_kb_priors", __name__)

    def _json(obj) -> "Response":
        """JSON with <, > and & escaped as \\u003c/\\u003e/\\u0026 (same as mason_rh_history._json): no response
        carries raw HTML (contracts §6, DEBUG_T5 D2) while parsed values stay exact."""
        body = json.dumps(obj, ensure_ascii=True, default=str)
        body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return Response(body, status=200, mimetype="application/json")

    @bp.route("/api/kb/priors", methods=["GET"])
    def _api_priors():
        if not enabled():
            return _json({"enabled": False})
        as_of = _now_iso()
        try:
            data = _query(request.args.get("family") or None, request.args.get("limit", 500, type=int) or 500)
            return _json({"ok": True, "as_of": as_of, "data": data, "stale": False, "errors": []})
        except Exception as e:
            msg = _err("api priors", e)
            return _json({"ok": False, "as_of": as_of, "data": [], "stale": True, "errors": [msg]})
except Exception:                       # flask missing: module still works, route is skipped
    bp = None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(status(), indent=2, default=str))
    sys.exit(0)
