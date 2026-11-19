"""
mason_robinhood.py — Robinhood, read-only (docs/robinhood/ROBINHOOD_DESIGN.md).

Mason is its OWN OAuth client of Robinhood's official MCP server
(https://agent.robinhood.com/mcp/trading). No password, no robin_stocks, no borrowed token.

G4 — NEVER TRADES. Every call goes through _call(), which refuses:
  - anything matching place_/cancel_/exercise_/review_/preview_/…order, update/unfollow_watchlist,
    or a scan write (create/update/delete_scan…)
  - anything not in READ_TOOLS / WRITE_TOOLS (new server tools are refused until listed here)
  - WRITE_TOOLS (watchlist items, price alerts — RH-D1) unless their own config flag is on
  - follow_watchlist unless rh_expansion.crowd.allow_follow is on AND the target list's name is in
    rh_expansion.crowd.lists (RH Expansion contracts §2.1)
_call() also runs every request through a process-wide two-lane token bucket (live / bulk) with
429/timeout backoff — bulk: 2→64 s, 6 retries; live: 1 retry after 2 s (≤10 s total) so UI-driven
calls stay snappy (RH Expansion S1-T0); paged_all() is the bounded, cycle-safe paginator.

Login is a one-time, human step:  python3 mason_robinhood.py login
Tokens live in ~/.config/mason/robinhood_oauth.json (0600, outside the repo — mason.config.json is
git-tracked). After that, refresh keeps the connection alive; if refresh fails the module goes
'disconnected' and every public function returns an empty/neutral value.

Config: mason.config.json → "robinhood" (all flags false). Public functions never raise.
The MCP client is async; one background asyncio thread owns one long-lived session and the sync
public API submits to it. Tests replace the module hook _invoke(tool, args) -> data.
"""

import os
import re
import sys
import json
import time
import queue
import asyncio
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CONFIG = Path(__file__).resolve().parent / "mason.config.json"
SERVER_URL = "https://agent.robinhood.com/mcp/trading"
REDIRECT_URI = "http://localhost:8765/callback"
CALL_TIMEOUT_S = 10
CONNECT_TIMEOUT_S = 20

# ── safety boundary (design §3) ───────────────────────────────────────────────
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions", "get_option_positions",
    "get_equity_orders", "get_option_orders", "get_crypto_positions", "get_crypto_orders",
    "get_equity_quotes", "get_option_quotes", "get_option_instruments", "get_option_chains",
    "get_equity_historicals", "get_option_historicals", "get_index_quotes", "get_crypto_quotes",
    "get_watchlists", "get_watchlist_items", "get_option_watchlist", "get_alerts", "get_alert_log",
    "get_realized_pnl", "get_pnl_trade_history", "get_earnings_calendar",
    "get_equity_fundamentals", "get_equity_analyst_ratings",
    # RH Expansion S1-T0 (contracts §2; one account-history tool removed per §2.6)
    "get_politician_trades", "get_earnings_results", "get_index_historicals", "get_indexes",
    "get_popular_watchlists",
    # scanners (SCANNER_INTAKE_DESIGN §7) — reads only; scan writes stay forbidden below
    "get_scans", "run_scan", "get_scanner_filter_specs", "get_scanner_datapoints",
})
# tool -> config flag path that must be true (RH-D1)
WRITE_TOOLS = {
    "create_watchlist": ("watchlist_sync", "enabled"),
    "add_to_watchlist": ("watchlist_sync", "enabled"),
    "remove_from_watchlist": ("watchlist_sync", "enabled"),
    "create_alert": ("alerts_mirror", "enabled"),
    "update_alert": ("alerts_mirror", "enabled"),
    "delete_alert": ("alerts_mirror", "enabled"),
    # RH Expansion §2.1: the one new write. Path is under the top-level "rh_expansion" block (not
    # "robinhood"), and _call() additionally requires the list's NAME to be in crowd.lists.
    "follow_watchlist": ("rh_expansion", "crowd", "allow_follow"),
}
_FORBIDDEN_RE = re.compile(
    r"^(place_|cancel_|exercise_|review_|preview_)|"
    r"^(update_watchlist|unfollow_watchlist)$|"
    r"^(create|update|delete)_scan|"         # scan writes; run_scan / get_scans don't match
    r"(^|_)order$")


class RHForbidden(Exception):
    """A call Mason is never (or not currently) allowed to make."""


class RHError(Exception):
    """Robinhood/MCP returned an error."""


class RHNeedsLogin(Exception):
    """No usable token — Nico must run `python3 mason_robinhood.py login`."""


def _is_forbidden(tool: str) -> bool:
    return bool(_FORBIDDEN_RE.search(tool or ""))


# ── config (read per call, mtime-cached) ──────────────────────────────────────
_cfg_cache = {"mtime": None, "data": {}}
_DEFAULTS = {
    "enabled": False,
    "token_file": "~/.config/mason/robinhood_oauth.json",
    "accounts": ["Play Money", "Agentic", "ETF Savings", "Roth IRA", "Household"],
    "poll_seconds": {"positions": 300, "quotes": 60},
    "personal_positions_sync": {"enabled": False,
                                "label_map": {"Play": "Play Money", "Roth": "Roth IRA",
                                              "Savings": "ETF Savings"}},
    "watchlist_sync": {"enabled": False, "push_list": "Mason"},
    "alerts_mirror": {"enabled": False},
    "tape": {"enabled": False},
    "brain_context": {"enabled": False},
}


def _config() -> dict:
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        return _cfg_cache["data"] or {}
    except Exception:
        return {}


def _cfg() -> dict:
    out = json.loads(json.dumps(_DEFAULTS))
    try:
        rh = _config().get("robinhood") or {}
        for k, v in rh.items():
            if v is None or str(k).startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            else:
                out[k] = v
    except Exception:
        pass
    return out


def enabled() -> bool:
    try:
        return bool(_cfg().get("enabled", False))
    except Exception:
        return False


def _flag(*path) -> bool:
    node = _cfg()
    for p in path:
        node = node.get(p) if isinstance(node, dict) else None
    return bool(node)


# ── personal scope: rh_expansion.personal_scope.exclude_accounts (Finalization E1, RULINGS #1) ──
# ONE parser for every module (S1, S2, D6). Ratified R8: a clean list → as given (normalized); [] → nobody;
# key missing / null → Household; a non-list value (a bare string included) or ANY unusable entry → the usable
# entries PLUS "household" and an error. Pure, never raises, never splits a bare string into letters.
# N7 (QA, N6b rework 2): the ONE glyph pair that prefixes every account mask Mason writes ("••1234"). brain_label / strip_masks
# are built from it, so a model-bound text never keeps a mask written with it; Tests/test_p2_n6b.py pins the constant and
# fails if any module writes a different bullet-style mask.
MASK_PREFIX = "\u2022\u2022"
_MASK_RE = re.compile(r"\s*(?:%s|%s)\s*\d*" % (re.escape(MASK_PREFIX), re.escape(MASK_PREFIX.encode("ascii", "backslashreplace").decode())))

_DEFAULT_EXCLUDE = frozenset({"household"})
_bad_scope_seen = set()


# Invisible "blank-looking" chars that are NOT Cf (Hangul fillers, braille blank, CGJ, Khmer inherent vowels,
# Mongolian FVS, variation selectors) — dropped like Cf chars (QA DEBUG_T-E1 S1).
_INVISIBLE_FILLERS = frozenset([0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F,
                                0x2800, 0x3164, 0xFFA0, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)])


def norm_account_label(s) -> str:
    """NFKC, drop Unicode format (Cf) chars (zero-width space/joiner, BOM) and invisible fillers, strip,
    casefold. Non-str → "". A result with no letter or digit (e.g. only fillers, controls, punctuation)
    → "" so it counts as blank / unusable everywhere (fail closed)."""
    if not isinstance(s, str):
        return ""
    try:
        s = unicodedata.normalize("NFKC", s)
        s = "".join(ch for ch in s if ord(ch) not in _INVISIBLE_FILLERS and unicodedata.category(ch) != "Cf")
        s = s.strip().casefold()
        return s if any(ch.isalnum() for ch in s) else ""
    except Exception:
        return ""


def parse_exclude_accounts(value, *, present=True) -> tuple:
    """(frozenset of normalized labels, error str | None). Never raises. The config-label rule is pure (see
    _parse_exclude_legacy); in last-4 map mode (phase 2 A2, RULINGS #2) the labels of every non-personal account
    (and every unresolvable stored label) are unioned in, so callers that parse their own config inherit the
    role map. With no role map the result is exactly the legacy one."""
    excl, err = _parse_exclude_legacy(value, present=present)
    try:
        extra = _map_excluded_labels()
        if extra:
            excl = frozenset(excl) | extra
    except Exception:
        excl = frozenset(excl) | _closed_labels_best_effort()          # QA R3: never fewer labels
    return excl, err


def _parse_exclude_legacy(value, present=True) -> tuple:
    """(frozenset of normalized labels, error str | None). Pure, no I/O, never raises."""
    try:
        if not present or value is None:
            return _DEFAULT_EXCLUDE, None
        if isinstance(value, str):
            lab = norm_account_label(value)
            return frozenset({lab} if lab else ()) | _DEFAULT_EXCLUDE, \
                "personal_scope.exclude_accounts is a bare string, not a list"
        if not isinstance(value, list):
            return _DEFAULT_EXCLUDE, \
                f"personal_scope.exclude_accounts is not a list ({type(value).__name__})"
        out, bad = set(), False
        for x in value:
            lab = norm_account_label(x)
            if lab:
                out.add(lab)
            else:
                bad = True
        if bad:
            return frozenset(out) | _DEFAULT_EXCLUDE, "personal_scope.exclude_accounts has unusable entries"
        return frozenset(out), None
    except Exception as e:
        return _DEFAULT_EXCLUDE, f"personal_scope.exclude_accounts unreadable ({type(e).__name__})"


def resolve_exclude_accounts(rhx, *, log=None, source="robinhood") -> frozenset:
    """Shared reader for a module's raw `rh_expansion` node (each module keeps its own config cache).
    A non-dict rh_expansion / personal_scope fails closed. Each distinct bad value is logged once per
    `source` through `log(msg)` (default: this module's _err). Never raises.
    Phase 2 A2 (RULINGS #2): in last-4 map mode the result is ALSO unioned with `_map_excluded_labels()` (every
    label recorded for a non-personal account, plus unresolvable stored labels), so all label-keyed consumers
    inherit the role map without being edited. No role map = exactly the legacy result."""
    excl = _resolve_exclude_legacy(rhx, log=log, source=source)
    try:
        extra = _map_excluded_labels()
        if extra:
            excl = frozenset(excl) | extra
    except Exception:
        excl = frozenset(excl) | _closed_labels_best_effort()          # QA R3: never fewer labels
    return excl


def _resolve_exclude_legacy(rhx, *, log=None, source="robinhood") -> frozenset:
    try:
        err = None
        if rhx is None:
            rhx = {}
        if not isinstance(rhx, dict):
            excl = _DEFAULT_EXCLUDE
            err = f"personal_scope.exclude_accounts unreadable: rh_expansion is not an object " \
                  f"({type(rhx).__name__})"
            key = repr(rhx)[:200]
        else:
            ps = rhx.get("personal_scope")
            key = repr(ps)[:200]
            if ps is None:
                excl = _DEFAULT_EXCLUDE
            elif not isinstance(ps, dict):
                excl = _DEFAULT_EXCLUDE
                err = f"personal_scope.exclude_accounts unreadable: personal_scope is not an object " \
                      f"({type(ps).__name__})"
            else:
                excl, err = _parse_exclude_legacy(ps.get("exclude_accounts"),
                                                  present="exclude_accounts" in ps)
        if err:
            k = (source, key)
            if k not in _bad_scope_seen and len(_bad_scope_seen) < 200:
                _bad_scope_seen.add(k)
                msg = f"{err}; excluding Household (fail closed)"
                try:
                    (log or (lambda m: _err("config", ValueError(m))))(msg)
                except Exception:
                    pass
        return excl
    except Exception:
        return _DEFAULT_EXCLUDE


def excluded_accounts() -> frozenset:
    """Normalized labels excluded from Mason's personal analytics / tools (this module's config)."""
    try:
        return resolve_exclude_accounts(_config().get("rh_expansion"))
    except Exception as e:
        try:
            _err("config", e)
        except Exception:
            pass
        try:
            return _DEFAULT_EXCLUDE | _closed_labels_best_effort()      # QA R3: never fewer labels than closed mode
        except Exception:
            return _DEFAULT_EXCLUDE


def in_scope_positions(**kw) -> list:
    """positions(**kw) minus excluded accounts; a blank/missing label is also removed (RULINGS #12).
    Phase 2 A2 (RULINGS #2): in last-4 map mode a row must ALSO resolve, through its account mask, to a `personal`
    account (a row without a usable mask is out), and in closed mode nothing is in scope."""
    try:
        excl = excluded_accounts()
        mode = _scope_mode()
        out = []
        for p in positions(**kw) or []:
            if not isinstance(p, dict):
                continue
            lab = norm_account_label(p.get("account"))
            if not lab or lab in excl:
                continue
            if mode["mode"] != "legacy" and not _row_is_personal(p, mode):
                continue
            out.append(p)
        return out
    except Exception as e:
        try:
            _err("in_scope_positions", e)
        except Exception:
            pass
        return []


def in_scope_position_symbols() -> set:
    """Same rule as position_symbols(), over in_scope_positions()."""
    try:
        return {p["underlying"] for p in in_scope_positions()
                if p.get("kind") in ("equity", "option") and p.get("underlying")}
    except Exception:
        return set()


def scope_stamp(excl=None) -> str:
    """12-hex stamp of a normalized exclude set (default: excluded_accounts()); the same value as
    mason_kb_priors._scope_hash. Caches of in-scope holdings carry it and are ignored on a mismatch
    (T-D6, RULINGS #13)."""
    import hashlib
    try:
        excl = excluded_accounts() if excl is None else excl
        return hashlib.sha1(json.dumps(sorted(excl)).encode()).hexdigest()[:12]
    except Exception:
        return hashlib.sha1(json.dumps(sorted(_DEFAULT_EXCLUDE)).encode()).hexdigest()[:12]


# ══ account identity by last-4 (phase 2 A2; RULINGS #2, #3, #4, #24.1) ═════════════════════════════════════════
# An account's identity is the SUFFIX of its account number, never its nickname (a nickname is whatever the owner
# types in the Robinhood app, so a renamed family account must not be able to leak). The role map is PRIVATE: it
# lives in the secrets file (mason_secrets section "robinhood"):
#     "accounts_by_last4": {"<4 digits>": "personal" | "family" | "hidden"}       (5-8 digits only to break a clash)
# Three modes, decided per call by _scope_mode():
#   legacy  no map, or an EMPTY map ({} counts as absent, #24.1): exactly the label-based scope of before this change
#           (robinhood.accounts allow-filter, rh_expansion.personal_scope.exclude_accounts, _DEFAULT_EXCLUDE).
#   map     a valid map: accounts() returns an active account iff its number matches a personal / family key; the
#           display label is the LIVE nickname. Only `personal` is in analytics scope; `family` is display-only;
#           `hidden` is ignored; an unmapped account is dropped (allowlist) with one warning.
#   closed  anything that is present but unusable (malformed file or section, a key not 4-8 digits, an unknown role,
#           two keys that normalise equal, two keys of one account with different roles): accounts() == [] and every
#           label ever recorded for an account is excluded. One status error naming the reason class, never a number.
# Label-keyed consumers (the 5 resolve_exclude_accounts + 2 parse_exclude_accounts callers) inherit the map because
# those two functions union _map_excluded_labels(): every label in the ALIAS HISTORY (desk_state key
# rh_account_aliases, written only by accounts(); seeded read-only from the stored tables while absent) that is not
# recorded exclusively for personal accounts. Analytics reads never call Robinhood and never write.
_ROLES = frozenset({"personal", "family", "hidden"})
_KEY_RE = re.compile(r"[0-9]{4,8}")
_ALIAS_KEY = "rh_account_aliases"
_ALIAS_TTL_S = 60.0
_MARKER_LAST4 = "accounts_last4"
_alias_lock = threading.RLock()
_alias_mem = {}          # key -> {"labels": set[str], "current": str, "role": str}  (this process, always unioned in)
_alias_cache = {"ts": 0.0, "entries": None, "unresolved": frozenset()}
_scope_seen = set()      # warn-once tags (cleared by _reset_caches, like _acct_missing_seen)
_LEGACY_MODE = {"mode": "legacy", "map": {}, "reason": "absent", "sig": "legacy"}


def _last4_of(x):
    """The 4 digits at the end of a mask ('••1234') or an account number, else None."""
    m = re.search(r"([0-9]{4})\D*$", x) if isinstance(x, str) else None
    return m.group(1) if m else None


def _scope_note(tag, msg) -> None:
    """ONE status error per distinct `tag` (visible in status()). `msg` names reason classes and ••NNNN masks only."""
    try:
        with _alias_lock:
            if tag in _scope_seen or len(_scope_seen) >= 200:
                return
            _scope_seen.add(tag)
        _err("accounts", ValueError(msg))
    except Exception:
        pass


def _closed_mode(reason: str) -> dict:
    return {"mode": "closed", "map": {}, "reason": reason, "sig": "closed:" + reason}


class _DupDict(dict):
    """A JSON object that remembers the keys it repeated (json.loads silently keeps the LAST value)."""
    dups = ()


def _dup_pairs(pairs):
    d, seen, dups = _DupDict(), set(), []
    for k, v in pairs:
        if k in seen:
            dups.append(k)
        seen.add(k)
        d[k] = v
    d.dups = tuple(dups)
    return d


def _has_dups(node) -> bool:
    if isinstance(node, _DupDict) and node.dups:
        return True
    if isinstance(node, dict):
        return any(_has_dups(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_dups(v) for v in node)
    return False


_dup_cache = {"key": None, "res": "ok"}


def _secrets_dup_state(S) -> str:
    """"ok" | "dup" | "unreadable". RULINGS #2 (QA R1): a key repeated in the private file is malformed, because
    json.loads keeps the last value and a pasted line could flip a family account to `personal`. Checked for a repeated
    top-level "robinhood" key and for any repeated key inside that section. The file is re-read through the same
    resolved path mason_secrets already validated (cached by inode / mtime / size). No file = "ok"."""
    try:
        p = os.path.realpath(S.path())
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return "ok"
        key = (p, st.st_ino, st.st_mtime_ns, st.st_size)
        with _alias_lock:
            if _dup_cache["key"] == key:
                return _dup_cache["res"]
        res = "ok"
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                blob = fh.read(int(getattr(S, "MAX_BYTES", 1 << 20)) + 1)
            txt = blob.decode("utf-8")
            doc = json.loads(txt[1:] if txt.startswith("\ufeff") else txt, object_pairs_hook=_dup_pairs)
            if isinstance(doc, dict) and (("robinhood" in getattr(doc, "dups", ())) or _has_dups(doc.get("robinhood"))):
                res = "dup"
        except Exception:
            res = "unreadable"
        if res != "unreadable":                  # a failed read is never cached (QA R6): the next call re-reads
            with _alias_lock:
                _dup_cache.update(key=key, res=res)
        return res
    except Exception:
        return "unreadable"


def _scope_mode() -> dict:
    """{"mode": "legacy"|"map"|"closed", "map": {key: role}, "reason", "sig"}. Never raises. The role map comes from
    mason_secrets.load_section("robinhood") (RULINGS #1.7): absent -> legacy; anything present but unusable -> closed."""
    try:
        try:
            import mason_secrets as _S
        except Exception:
            return _closed_mode("secrets_module_unavailable")
        sec, state = _S.load_section("robinhood")
        if state != "malformed":
            dup = _secrets_dup_state(_S)
            if dup == "dup":
                return _closed_mode("duplicate_key")
            if dup != "ok":
                return _closed_mode("secrets_file_unreadable")
        if state == "absent":
            return _LEGACY_MODE
        if state != "ok" or not isinstance(sec, dict):
            return _closed_mode("secrets_file_or_section_malformed")
        raw = sec.get("accounts_by_last4")
        if raw is None or (isinstance(raw, dict) and not raw):
            return _LEGACY_MODE                      # missing key, null or {} = absent (RULINGS #24.1)
        if not isinstance(raw, dict):
            return _closed_mode("role_map_not_an_object")
        kmap = {}
        for k, v in raw.items():
            kk = k.strip() if isinstance(k, str) else ""
            if not _KEY_RE.fullmatch(kk):
                return _closed_mode("key_not_4_to_8_digits")
            role = v.strip().lower() if isinstance(v, str) else ""
            if role not in _ROLES:
                return _closed_mode("unknown_role")
            if kk in kmap:
                return _closed_mode("duplicate_key")
            kmap[kk] = role
        import hashlib
        sig = "map:" + hashlib.sha1(json.dumps(sorted(kmap.items())).encode()).hexdigest()[:16]
        return {"mode": "map", "map": kmap, "reason": "", "sig": sig}
    except Exception:
        return _closed_mode("internal_error")


def _role_of_key(k: str, kmap: dict) -> str:
    """Effective role of an alias-history key under the CURRENT map (the stored role is never trusted). 'personal'
    only if every map key that corresponds to `k` is personal; no corresponding key -> 'unmapped'; otherwise the
    single non-personal role, or 'mixed'. A 4-digit seed key corresponds to every longer map key ending in it, and a
    stored 5-8 digit key to a shorter map key it ends with (fail closed)."""
    if k in kmap:
        return kmap[k]
    roles = {r for mk, r in kmap.items() if mk.endswith(k) or k.endswith(mk)}
    if not roles:
        return "unmapped"
    return next(iter(roles)) if len(roles) == 1 else "mixed"


# ── alias history (RULINGS #3) ───────────────────────────────────────────────
def _missing_table(e) -> bool:
    """True only for SQLite's own "no such table: <name>" (a prefix match, QA N9: a message that merely CONTAINS the
    words, or a "no such column", is a failed read)."""
    import sqlite3
    return isinstance(e, sqlite3.OperationalError) and str(e).lower().startswith("no such table")


def _tolerate_missing_table(e) -> None:
    """Only a MISSING table counts as "nothing stored". Any other sqlite error (a lock, WAL recovery, I/O, a malformed
    page, schema drift) is a failed read and is re-raised, so it can never be mistaken for an empty history (QA R6)."""
    if not _missing_table(e):
        raise e


class _PartialRead(Exception):
    """Some sources of the alias history could not be read (QA R8). `.result` holds EVERYTHING that was readable, in the
    shape of the function that raised it; `.failed` names the failing sources (never values). Callers keep the partial
    result (exclude more, never less), report the failure, and do not cache."""

    def __init__(self, result, failed):
        super().__init__("alias history sources unreadable: " + ", ".join(failed))
        self.result, self.failed = result, list(failed)


def _alias_seed(conn, failed=None) -> tuple:
    """READ-ONLY seed of the alias history from the stored tables: ({last4: {labels}}, {labels that must resolve}).
    rh_orders / rh_trades carry account_label + account_mask; rh_account_snaps carries the mask in raw_json; a label in
    exit_replays or on a robinhood_sync personal_positions row must resolve through the seed or it is excluded.
    Every source is read on its own. With `failed=None` any error other than a missing table raises (the persist path);
    with a list, the failing source's name is appended and the other sources still contribute (QA R8).
    A-C1 (RULINGS #49.2): a source that fails (typically one drifted column, e.g. account_mask) gets a second,
    LABELS-ONLY attempt (`retry`, SELECT DISTINCT account_label); what it returns goes into the unresolved set, so a
    label that only that table holds is still excluded unless the history records it only for personal keys."""
    import sqlite3
    seed, extra = {}, set()

    def q(sql, args=(), name="", retry=None):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            if _missing_table(e):
                return []
            if failed is None:
                raise
            failed.append(name)
            if retry:
                try:
                    for rr in conn.execute(retry).fetchall():
                        lab = norm_account_label(rr[0])
                        if lab:
                            extra.add(lab)
                except sqlite3.Error:
                    pass                      # a table unreadable by label too is unreadable for every consumer
            return []

    def add(label, mask):
        lab = norm_account_label(label)
        k = _last4_of(mask)
        if lab and k:
            seed.setdefault(k, set()).add(lab)
        elif lab:
            extra.add(lab)
    for tbl in ("rh_orders", "rh_trades"):
        for r in q(f"SELECT DISTINCT account_label, account_mask FROM {tbl}", name=tbl,
                   retry=f"SELECT DISTINCT account_label FROM {tbl}"):
            add(r[0], r[1])
    for r in q("SELECT DISTINCT account_label, raw_json FROM rh_account_snaps", name="rh_account_snaps",
               retry="SELECT DISTINCT account_label FROM rh_account_snaps"):
        try:
            m = (json.loads(r[1] or "{}") or {}).get("masked")
        except (TypeError, ValueError, AttributeError):
            m = None
        add(r[0], m)
    for r in q("SELECT DISTINCT account_label FROM exit_replays", name="exit_replays"):
        lab = norm_account_label(r[0])
        if lab:
            extra.add(lab)
    for r in q("SELECT DISTINCT account_label FROM personal_positions WHERE created_by = ?", ("robinhood_sync",),
               name="personal_positions"):
        lab = norm_account_label(r[0])
        if lab:
            extra.add(lab)
    return seed, extra


def _alias_decode(txt):
    """desk_state value -> {key: {"labels": set, ...meta}} or None when unusable."""
    try:
        d = json.loads(txt or "null")
        if not isinstance(d, dict):
            return None
        out = {}
        for k, v in d.items():
            if not (isinstance(k, str) and _KEY_RE.fullmatch(k)) or not isinstance(v, dict):
                return None
            labs = v.get("labels")
            if not isinstance(labs, list):
                return None
            out[k] = dict(v, labels={norm_account_label(x) for x in labs if norm_account_label(x)})
        return out
    except (TypeError, ValueError):
        return None


def _alias_db_read_full() -> tuple:
    """(persisted {key: {"labels": set, "role": str, ...}} | None, seed {key: set}, extra labels, corrupt). `corrupt`
    is True when a history row exists but cannot be decoded (persisted is then None too). A read-only (mode=ro)
    connection: an analytics read never writes and never creates the DB.
    QA R8: the history and each seed table are read INDEPENDENTLY. If any source fails with something other than "no
    such table" (a lock, schema drift, a malformed page) the sources that succeeded are still returned, inside a
    _PartialRead (so the caller cannot cache or trust it as complete). A failure to open the DB raises as before."""
    import sqlite3
    from urllib.parse import quote
    import mason_db
    dbp = Path(mason_db.DB_FILE)
    if not dbp.exists():
        return None, {}, set(), False
    conn = sqlite3.connect("file:" + quote(str(dbp.resolve())) + "?mode=ro", uri=True, timeout=2.0)
    try:
        failed, persisted, corrupt = [], None, False
        try:
            r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_ALIAS_KEY,)).fetchone()
        except sqlite3.Error as e:
            if not _missing_table(e):
                failed.append("history")
            r = None
        if r:
            persisted = _alias_decode(r[0])
            corrupt = persisted is None
            if persisted is None:
                _scope_note(("alias_corrupt",), "the stored account alias history is unreadable; rebuilding it from "
                            "the stored rows (labels stay excluded)")
        seed, extra = _alias_seed(conn, failed)
        res = (persisted, seed, extra, corrupt)
        if failed:
            raise _PartialRead(res, failed)
        return res
    finally:
        conn.close()


def _alias_db_read() -> tuple:
    """(persisted {key: set} | None, seed {key: set}, extra labels); see _alias_db_read_full. A _PartialRead carries the
    same shape."""
    def conv(res):
        persisted, seed, extra = res[0], res[1], res[2]
        return (None if persisted is None else {k: set(v["labels"]) for k, v in persisted.items()}), seed, extra
    try:
        return conv(_alias_db_read_full())
    except _PartialRead as e:
        raise _PartialRead(conv(e.result), e.failed) from None


def _alias_db_roles() -> dict:
    """{key: last stored role} from the persisted alias history (read-only; {} when absent). Raises on an unreadable DB."""
    import sqlite3
    from urllib.parse import quote
    import mason_db
    dbp = Path(mason_db.DB_FILE)
    if not dbp.exists():
        return {}
    conn = sqlite3.connect("file:" + quote(str(dbp.resolve())) + "?mode=ro", uri=True, timeout=2.0)
    try:
        try:
            r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_ALIAS_KEY,)).fetchone()
        except sqlite3.OperationalError as e:
            _tolerate_missing_table(e)
            return {}
        dec = _alias_decode(r[0]) if r else None
        return {k: str(v.get("role") or "") for k, v in (dec or {}).items()}
    finally:
        conn.close()


def _alias_view() -> tuple:
    """({key: set(norm labels)}, frozenset(unresolved labels)): persisted history (or, while absent, the read-only
    seed) UNION this process's in-memory record. TTL 60 s; accounts() invalidates it. No Robinhood call, no write."""
    with _alias_lock:
        c = _alias_cache
        if c["entries"] is not None and time.time() - c["ts"] < _ALIAS_TTL_S:
            return c["entries"], c["unresolved"]
    persisted, seed, extra, failed = None, {}, set(), False
    try:
        persisted, seed, extra = _alias_db_read()
    except _PartialRead as e:
        # QA R8: keep EVERY source that was readable (history and the other seed tables), never cache, report once.
        failed = True
        persisted, seed, extra = e.result
        _scope_note(("alias_partial", tuple(e.failed)), "account alias history: source(s) unreadable ("
                    + ", ".join(e.failed) + "); using everything that could be read plus this process's record")
    except Exception as e:
        failed = True                       # QA R6: a failed (or partial) read is NEVER cached; the next call re-reads
        _scope_note(("alias_unreadable", type(e).__name__),
                    f"account alias history unreadable ({type(e).__name__}); using this process's record only")
    # QA R2: the read-only seed is ALWAYS unioned in, per key (exclude more, never less), so a label that reached a
    # stored row but not the persisted history (a failed save, another process) is still excluded.
    entries = {}
    for src in (persisted or {}, seed):
        for k, v in src.items():
            entries.setdefault(k, set()).update(v)
    with _alias_lock:
        for k, m in _alias_mem.items():
            entries.setdefault(k, set()).update(m["labels"])
        known = set().union(*entries.values()) if entries else set()
        unresolved = frozenset(x for x in extra if x not in known)
        if not failed:
            _alias_cache.update(ts=time.time(), entries=entries, unresolved=unresolved)
    if unresolved:
        _scope_note(("unresolved", len(unresolved)),
                    f"{len(unresolved)} stored account label(s) resolve to no known Robinhood account; "
                    "excluded (not provably personal)")
    return entries, unresolved


def _alias_merge(cur, items, conn, today):
    """(history dict, changed): `cur` (None = no history yet -> seeded read-only from the stored rows through `conn`)
    with `items` merged in. Pure apart from the seed read."""
    changed = False
    if cur is None:
        cur, changed = {}, True
        for k, labs in _alias_seed(conn)[0].items():
            cur[k] = {"labels": set(labs), "current": "", "role": "unknown", "first_seen": today, "last_seen": today}
    for it in items:
        e = cur.get(it["key"])
        if e is None:
            cur[it["key"]] = {"labels": set(it["labels"]), "current": it["current"], "role": it["role"],
                              "first_seen": today, "last_seen": today}
            changed = True
        elif (not set(it["labels"]) <= e["labels"] or e.get("current") != it["current"]
              or e.get("role") != it["role"]):
            e["labels"] |= set(it["labels"])
            e.update(current=it["current"], role=it["role"], last_seen=today)
            changed = True
    return cur, changed


def _alias_record(items) -> bool:
    """Record the labels of the accounts accounts() is about to return: in memory first (always), then in
    desk_state[rh_account_aliases] (seeding it from the stored rows while absent). Invariant (RULINGS #3.2): every
    label that can reach a stored row is in the history before accounts() returns it. A refresh that changes nothing
    takes no write lock. items: [{"key", "labels": set(norm), "current": str, "role": str}]. True when the history is
    persisted; never raises."""
    with _alias_lock:
        for it in items:
            m = _alias_mem.setdefault(it["key"], {"labels": set(), "current": "", "role": ""})
            m["labels"] |= set(it["labels"])
            m["current"], m["role"] = it["current"], it["role"]
        _alias_cache.update(ts=0.0, entries=None, unresolved=frozenset())
    try:
        import mason_db
        today = datetime.now().date().isoformat()
        conn = mason_db.get_connection()
        try:
            r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_ALIAS_KEY,)).fetchone()
            cur = _alias_decode(r[0]) if r else None
            if cur is not None and not _alias_merge(cur, items, conn, today)[1]:
                return True                                   # nothing new: no write lock
            conn.isolation_level = None
            conn.execute("PRAGMA busy_timeout = 3000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_ALIAS_KEY,)).fetchone()
                cur, changed = _alias_merge(_alias_decode(r[0]) if r else None, items, conn, today)
                if changed:
                    blob = json.dumps({k: dict(v, labels=sorted(v["labels"])) for k, v in sorted(cur.items())})
                    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                                 "updated_date = excluded.updated_date", (_ALIAS_KEY, blob, today))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
        return True
    except Exception as e:
        _scope_note(("alias_persist", type(e).__name__),
                    f"could not persist the account alias history ({type(e).__name__}); kept in memory only")
        return False


def _closed_labels_best_effort() -> frozenset:
    """Every label this process or the DB has ever recorded for an account, whatever its role (the closed-mode set),
    gathered best effort and never raising. The fallback of every fail-closed path (QA R3)."""
    out = set()
    try:
        with _alias_lock:
            for m in _alias_mem.values():
                out |= set(m["labels"])
    except Exception:
        pass
    try:
        try:
            persisted, seed, extra = _alias_db_read()
        except _PartialRead as e:              # QA R8: every readable source still counts
            persisted, seed, extra = e.result
        for d in (persisted or {}, seed):
            for labs in d.values():
                out |= set(labs)
        out |= set(extra)
    except Exception:
        pass
    return frozenset(out)


_sticky_cache = {"ts": 0.0, "labels": frozenset()}


def _sticky_labels() -> frozenset:
    """LEGACY mode only (A2_sticky, amends RP-2 "absent = legacy"): if the persisted alias history exists, a role map
    was active once and is missing now (deleted, moved, wrong MASON_SECRETS_FILE). Then stay excluded: every label of a
    history key whose LAST stored role was not `personal` (family, hidden, unknown), every read-only SEED label of a key
    that is absent from the history or whose stored role is not personal (a label stored while a save failed, QA R7),
    and every stored label that resolves to no account. One status error. Empty when no history exists (today's live
    state), so legacy parity is untouched. TTL 60 s, read-only DB, no RH call. A failed read (a lock included) is NOT
    "no history": it returns every recorded label plus one error, and is never cached; a corrupt history likewise.
    The history and each seed table are read independently (QA R8): a source that fails contributes nothing but is
    reported and prevents caching; every source that succeeded still counts. A failed HISTORY source means "present":
    the closed set is built from the sources that could be read. A seed-table failure while the history reads fine (or
    is simply absent, as today) never turns the whole thing closed."""
    now = time.time()
    with _alias_lock:
        if _sticky_cache["ts"] and now - _sticky_cache["ts"] < _ALIAS_TTL_S:
            return _sticky_cache["labels"]
    partial = []
    try:
        persisted, seed, extra, corrupt = _alias_db_read_full()
    except _PartialRead as e:
        persisted, seed, extra, corrupt = e.result
        partial = list(e.failed)
        _scope_note(("sticky_partial", tuple(partial)), "the role-map history: source(s) unreadable ("
                    + ", ".join(partial) + "); using everything that could be read (fail closed where unsure)")
    except Exception as e:
        _scope_note(("sticky_unreadable", type(e).__name__),
                    f"the role-map history could not be read ({type(e).__name__}); every recorded label is excluded "
                    "(fail closed)")
        return _closed_labels_best_effort()
    labels, present = set(), False
    if corrupt or "history" in partial:
        present = True
        for d in ({k: v["labels"] for k, v in (persisted or {}).items()}, seed):
            for labs in d.values():
                labels |= set(labs)
        labels |= set(extra)
        with _alias_lock:
            for m in _alias_mem.values():
                labels |= set(m["labels"])
    elif persisted is not None:
        present = True
        known = set()
        for k, e in persisted.items():
            known |= e["labels"]
            if str(e.get("role") or "") != "personal":
                labels |= e["labels"]
        for sk, slabs in seed.items():
            known |= slabs
            rel = [e for pk, e in persisted.items() if pk == sk or pk.endswith(sk) or sk.endswith(pk)]
            if not rel or any(str(e.get("role") or "") != "personal" for e in rel):
                labels |= slabs
        labels |= {x for x in extra if x not in known}
    if present:
        _scope_note(("sticky",), "the role map was active earlier and is now missing; history labels of non-personal "
                    "accounts stay excluded (restore the map, or delete desk_state rh_account_aliases to leave map mode)")
    if not partial:                                  # a partial read is never cached (QA R6/R8)
        with _alias_lock:
            _sticky_cache.update(ts=now, labels=frozenset(labels))
    return frozenset(labels)


def _map_excluded_labels(mode=None) -> frozenset:
    """Normalized labels that label-keyed consumers must exclude because of the role map (empty in legacy mode).
    map mode: every alias-history label NOT recorded exclusively for personal accounts, plus every stored label that
    resolves to no account. closed mode: every label ever recorded, plus the unresolved ones. Never raises; an
    internal failure outside legacy mode returns the closed set (QA R3: fail closed, never fewer labels)."""
    try:
        mode = mode or _scope_mode()
        if mode["mode"] == "legacy":
            return _sticky_labels()
        entries, unresolved = _alias_view()
        out = set(unresolved)
        if mode["mode"] == "closed":
            for labs in entries.values():
                out |= labs
            return frozenset(out)
        # A stored 4-digit key that SEVERAL longer map keys of different roles now refine ("mixed", after a shared
        # last-4 was disambiguated) cannot say whose label it was; the live display label of a PERSONAL account
        # (this process's accounts() result, unique by construction) is not blocked by it. Every other label stays
        # excluded (fail closed).
        live_personal = {norm_account_label(r.get("label")) for r in _accounts_view_rows(mode)
                         if r.get("role") == "personal"}
        flags = {}
        for k, labs in entries.items():
            role = _role_of_key(k, mode["map"])
            for lab in labs:
                flags.setdefault(lab, set()).add(role == "personal" or (role == "mixed" and lab in live_personal))
        out |= {lab for lab, f in flags.items() if f != {True}}
        shared = frozenset(lab for lab, f in flags.items() if len(f) == 2)
        if shared:
            _scope_note(("shared_label", shared), "a personal account shares a label with a non-personal account; "
                        "the label stays excluded - rename it in the Robinhood app")
        return frozenset(out)
    except Exception:
        _scope_note(("labels_failed",), "the role-map label exclusion failed internally; every recorded label is "
                    "excluded (fail closed)")
        return _closed_labels_best_effort()


def role_excluded_labels() -> frozenset:
    """Public form of _map_excluded_labels() for a consumer that keeps its OWN reader of the config exclude list:
    normalized labels the last-4 role map excludes (empty with no role map). A consumer unions it into its set."""
    return _map_excluded_labels()


def _accounts_view_rows(mode) -> list:
    """The cached accounts() rows when they were produced under this map (else [])."""
    with _alias_lock:
        if mode.get("sig") == _acct_cache.get("sig") and _acct_cache.get("rows"):
            return list(_acct_cache["rows"])
    return []


def family_labels() -> frozenset:
    """Normalized labels of the accounts the role map marks 'family' (map mode only; empty otherwise). The role
    source for the family display (A1); makes no Robinhood call."""
    try:
        mode = _scope_mode()
        if mode["mode"] != "map":
            return frozenset()
        entries, _ = _alias_view()
        out = {lab for k, labs in entries.items() if _role_of_key(k, mode["map"]) == "family" for lab in labs}
        out |= {norm_account_label(r.get("label")) for r in _accounts_view_rows(mode) if r.get("role") == "family"}
        return frozenset(x for x in out if x)
    except Exception:
        return frozenset()


def _ro_marker():
    """The personal-positions cutover marker (desk_state) read through a read-only connection, or None."""
    import sqlite3
    from urllib.parse import quote
    import mason_db
    dbp = Path(mason_db.DB_FILE)
    if not dbp.exists():
        return None
    conn = sqlite3.connect("file:" + quote(str(dbp.resolve())) + "?mode=ro", uri=True, timeout=2.0)
    try:
        try:
            r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_PP_CUTOVER_KEY,)).fetchone()
        except sqlite3.OperationalError as e:
            _tolerate_missing_table(e)
            return None
        v = json.loads(r[0] or "null") if r else None
        return v if isinstance(v, dict) else None
    finally:
        conn.close()


def synced_alias_labels() -> frozenset:
    """Map mode: normalized labels ever recorded for the accounts the personal-positions cutover handed to the
    Robinhood sync (marker accounts_last4), so a hand row typed under an old OR new name is still seen as synced
    (RULINGS #4.5, A7 carry). Read-only DB reads, no Robinhood call. Empty in legacy mode / before the binding."""
    try:
        mode = _scope_mode()
        if mode["mode"] != "map":
            return frozenset()
        m = _ro_marker() or {}
        bound = m.get(_MARKER_LAST4)
        if not isinstance(bound, dict):
            return frozenset()
        keys = {str(v) for v in bound.values() if isinstance(v, str) and _KEY_RE.fullmatch(v)}
        entries, _ = _alias_view()
        out = {lab for k, labs in entries.items()
               if any(k == mk or mk.endswith(k) or k.endswith(mk) for mk in keys) for lab in labs}
        out |= {norm_account_label(r.get("label")) for r in _accounts_view_rows(mode) if r.get("key") in keys}
        return frozenset(x for x in out if x)
    except Exception:
        return frozenset()


def _row_is_personal(p: dict, mode: dict) -> bool:
    """A position row is analytics-eligible in map mode iff its account mask resolves to a PERSONAL account. The
    authoritative source is the last accounts() result (label + last-4 -> role); without it, every map key ending in
    the mask must be personal. A row without a usable mask is out."""
    if mode["mode"] != "map":
        return False
    last4 = _last4_of(p.get("account_masked"))
    if not last4:
        return False
    lab = norm_account_label(p.get("account"))
    hits = [r for r in _accounts_view_rows(mode)
            if norm_account_label(r.get("label")) == lab and r.get("last4") == last4]
    if hits:
        return all(h.get("role") == "personal" for h in hits)
    cands = [r for k, r in mode["map"].items() if k[-4:] == last4]
    return bool(cands) and all(r == "personal" for r in cands)


def _accounts_closed(mode) -> list:
    _scope_note(("closed", mode["reason"]), f"last-4 role map unusable ({mode['reason']}); every Robinhood "
                "account is excluded (fail closed)")
    _acct_cache.update(ts=0.0, rows=None, sig=mode["sig"])
    return []


def _accounts_map(mode) -> list:
    """Map mode (RULINGS #2, #3). Display label = the live nickname; identity and role = the number's suffix."""
    sig, kmap = mode["sig"], mode["map"]
    if (_acct_cache["rows"] is not None and time.time() - _acct_cache["ts"] < _ACCT_TTL_S
            and _acct_cache.get("sig") == sig):
        return list(_acct_cache["rows"])
    raw = (_call("get_accounts") or {}).get("accounts") or []
    active = [a for a in raw if isinstance(a, dict) and not (a.get("deactivated") or a.get("permanently_deactivated"))
              and a.get("state") == "active"]
    nums = [str(a.get("account_number") or "") for a in active]
    matches = [[k for k in kmap if n and n.endswith(k)] for n in nums]
    if any(len({kmap[k] for k in ks}) > 1 for ks in matches):
        _scope_note(("conflict", sig), "an account matches two role-map keys with different roles; every Robinhood "
                    "account is excluded (fail closed) - fix accounts_by_last4")
        _acct_cache.update(ts=time.time(), rows=[], sig=sig)
        return []
    best = {i: max(ks, key=len) for i, ks in enumerate(matches) if ks}
    by_key = {}
    for i, k in best.items():
        by_key.setdefault(k, []).append(i)
    clash = {i for k, idxs in by_key.items() if len(idxs) > 1 and kmap[k] != "hidden" for i in idxs}
    for k, idxs in sorted(by_key.items()):
        if len(idxs) > 1 and kmap[k] != "hidden":
            _scope_note(("clash", k), f"two active Robinhood accounts share the suffix {MASK_PREFIX}{k[-4:]}; use a longer "
                        "suffix key (both are dropped until then)")
    unmapped = sorted({MASK_PREFIX + nums[i][-4:] for i in range(len(active)) if i not in best})
    if unmapped:
        _scope_note(("unmapped", tuple(unmapped)),
                    f"new Robinhood account {', '.join(unmapped[:10])} is not in accounts_by_last4 (dropped)")
    kept = [i for i in sorted(best) if i not in clash and kmap[best[i]] in ("personal", "family")]
    labels = []
    for i in kept:
        lab = str(_label(active[i]) or "").strip()
        if not norm_account_label(lab) or re.fullmatch(r"[0-9]{5,}", lab):
            lab = f"Account {MASK_PREFIX}{nums[i][-4:]}"                      # never use a bare account number as a label
        labels.append(lab)
    seen = {}
    for lab in labels:
        seen[norm_account_label(lab)] = seen.get(norm_account_label(lab), 0) + 1
    final = []
    for i, lab in zip(kept, labels):
        if seen[norm_account_label(lab)] > 1:                       # RULINGS #3.5: labels are keys downstream
            lab = f"{lab} {MASK_PREFIX}{nums[i][-4:]}"
        final.append(lab)
    n_seen = {}
    for j, lab in enumerate(final):
        c = n_seen[norm_account_label(lab)] = n_seen.get(norm_account_label(lab), 0) + 1
        if c > 1:
            final[j] = f"{lab} #{c}"
    rows, items = [], []
    for i, lab in zip(kept, final):
        a, num, k = active[i], nums[i], best[i]
        rows.append({"label": lab, "number": num, "rhs_number": str(a.get("rhs_account_number") or num),
                     "masked": MASK_PREFIX + num[-4:], "type": a.get("type"),
                     "account_type": a.get("brokerage_account_type"),
                     "is_default": bool(a.get("is_default")), "option_level": a.get("option_level") or "",
                     "last4": num[-4:], "key": k, "role": kmap[k]})
        nick = norm_account_label(_label(a))
        items.append({"key": k, "labels": {x for x in (norm_account_label(lab), nick) if x}, "current": lab,
                      "role": kmap[k]})
    _alias_record(items)                                           # before the rows can reach any stored row
    try:                                                           # QA R5: a personal key whose history is excluded
        legacy_excl = _resolve_exclude_legacy(_config().get("rh_expansion"), log=lambda m: None)
        entries, _u = _alias_view()
        odd = sorted({MASK_PREFIX + it["key"][-4:] for it in items if it["role"] == "personal"
                      and (set(it["labels"]) | {lab for ek, labs in entries.items()
                                                if ek == it["key"] or ek == it["key"][-4:] for lab in labs}) & legacy_excl})
        if odd:
            _scope_note(("personal_excluded", tuple(odd)), f"account {', '.join(odd)} is mapped personal but was "
                        "recorded under an excluded label; check accounts_by_last4")
    except Exception:
        pass
    order = [str(s).lower() for s in (_cfg().get("accounts") or [])]    # display order only, never a filter
    rows.sort(key=lambda r: order.index(r["label"].lower()) if r["label"].lower() in order else len(order))
    _acct_cache.update(ts=time.time(), rows=rows, sig=sig)
    miss = sorted({MASK_PREFIX + k[-4:] for k in kmap if not any(n.endswith(k) for n in nums if n)})
    if miss:
        _scope_note(("nomatch", tuple(miss)), f"role-map entr{'ies' if len(miss) > 1 else 'y'} "
                    f"{', '.join(miss[:10])} match no active Robinhood account")
    return list(rows)


# RH Expansion block (contracts §7) — only the keys this module needs; merged over the file.
_RHX_DEFAULTS = {
    "rate": {"bulk_per_s": 2, "live_per_s": 4},
    "crowd": {"allow_follow": False, "lists": ["100 Most Popular"]},
}


def _rhx_cfg() -> dict:
    out = json.loads(json.dumps(_RHX_DEFAULTS))
    try:
        for k, v in (_config().get("rh_expansion") or {}).items():
            if v is None or str(k).startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            else:
                out[k] = v
    except Exception:
        pass
    return out


def _write_allowed(path) -> bool:
    if path and path[0] == "rh_expansion":
        node = _rhx_cfg()
        for p in path[1:]:
            node = node.get(p) if isinstance(node, dict) else None
        return node is True
    return _flag(*path)


def _token_path() -> Path:
    default = str(_cfg().get("token_file") or _DEFAULTS["token_file"])
    try:                              # phase 2 B1/A2: env ROBINHOOD_TOKEN_FILE > secrets file > config > default
        import mason_secrets as _S
        default = _S.robinhood_token_file(default, cfg=_config()) or default
    except Exception:
        pass
    return Path(os.path.expanduser(str(default)))


# ── state ─────────────────────────────────────────────────────────────────────
_state = {"connected": None, "last_ok": None, "errors": [], "calls": 0, "last_call_ts": [],
          "rate_hits": 0, "retries": 0, "paged_truncated": 0}
_acct_cache = {"ts": 0.0, "rows": None}
_pos_cache = {}          # {"ts": float, "rows": list}
_quote_cache = {}        # symbol -> (ts, dict)
_instr_cache = {}        # option instrument id -> dict
_ACCT_TTL_S = 600
_QUOTE_TTL_S = 5


def _reset_caches():
    _acct_cache.update(ts=0.0, rows=None)
    _pos_cache.clear()
    _quote_cache.clear()
    _instr_cache.clear()
    _state["errors"] = []
    _acct_missing_seen.clear()
    with _alias_lock:                        # phase 2 A2: alias record, view cache, warn-once memory
        _alias_mem.clear()
        _alias_cache.update(ts=0.0, entries=None, unresolved=frozenset())
        _dup_cache.update(key=None, res="ok")
        _sticky_cache.update(ts=0.0, labels=frozenset())
        _scope_seen.clear()


def _err(where: str, e) -> None:
    msg = f"{datetime.now().isoformat(timespec='seconds')} {where}: {type(e).__name__}: {e}"
    _state["errors"] = (_state["errors"] + [msg])[-5:]
    if isinstance(e, RHNeedsLogin):
        _state["connected"] = False


# ── rate limiter (RH Expansion S1-T0) ────────────────────────────────────────
# Process-wide, two lanes so a backfill can never starve the scanner poller:
#   live — scanner poller, tape, quotes, UI-driven calls (rh_expansion.rate.live_per_s)
#   bulk — backfills and sweeps                           (rh_expansion.rate.bulk_per_s)
# Each lane is a token bucket holding up to one second of tokens. A 429 on either lane halves
# BOTH lanes for 10 minutes (every 429, including the last one before giving up).
# Retry budget per lane (429s and timeouts; writes retry only on 429):
#   bulk — back off 2, 4, 8 … s (cap 120 s), max 6 retries
#   live — at most 1 retry after 2 s, total backoff ≤ 10 s, then re-raise (UI calls must not hang)
# _clock/_sleep are module hooks so tests drive a fake clock.
_clock = time.monotonic
_sleep = time.sleep
LANES = ("live", "bulk")
_HALVE_FOR_S = 600
_BACKOFF_CAP_S = 120
_MAX_RETRIES = 6                 # bulk lane
_LIVE_MAX_RETRIES = 1            # live lane
_LIVE_MAX_BACKOFF_S = 10         # live lane: total backoff sleep budget
_RATE_LIMIT_RE = re.compile(r"\b(?:http|status|code|error)[\s:=#-]*429\b|too many requests|rate[\s_-]?limit",
                            re.IGNORECASE)
_STATUS_LINE_429_RE = re.compile(r"\s*429(?![\d.,])")
_lim_lock = threading.Lock()
_lim = {"lanes": {}, "halved_until": 0.0}


def _limiter_reset():
    with _lim_lock:
        _lim["lanes"] = {}
        _lim["halved_until"] = 0.0


def _lane_rate(lane: str) -> float:
    """Current tokens/s for a lane (config rate, halved inside a 429 window). Never below 0.1/s."""
    try:
        r = float((_rhx_cfg().get("rate") or {}).get(f"{lane}_per_s") or _RHX_DEFAULTS["rate"][f"{lane}_per_s"])
    except (TypeError, ValueError):
        r = float(_RHX_DEFAULTS["rate"][f"{lane}_per_s"])
    if _clock() < _lim["halved_until"]:
        r /= 2.0
    return max(0.1, r)


def _acquire(lane: str) -> None:
    """Take one token from the lane, sleeping (outside the lock) if the bucket is empty."""
    rate = _lane_rate(lane)
    with _lim_lock:
        now = _clock()
        b = _lim["lanes"].get(lane)
        if b is None:
            b = _lim["lanes"][lane] = {"tokens": rate, "ts": now}
        b["tokens"] = min(rate, b["tokens"] + (now - b["ts"]) * rate)
        b["ts"] = now
        b["tokens"] -= 1.0                   # reserve; a negative balance is this caller's wait
        wait = -b["tokens"] / rate if b["tokens"] < 0 else 0.0
    if wait > 0:
        _sleep(wait)


def _backoff_s(attempt: int) -> float:
    return float(min(_BACKOFF_CAP_S, 2 ** (attempt + 1)))


def _retry_kind(e) -> str | None:
    """'429' for a rate-limit answer, 'timeout' for a timeout, None for anything else."""
    if isinstance(e, (RHForbidden, RHNeedsLogin)):
        return None
    resp = getattr(e, "response", None)
    if getattr(resp, "status_code", None) == 429 or getattr(e, "status_code", None) == 429:
        return "429"
    msg = str(e)
    # a status code or rate-limit wording only — not any "429" in the text (e.g. "strike 429.00");
    # a message that *starts* with the code ("429", "429 Too Many Requests") is a status line
    if _RATE_LIMIT_RE.search(f"{type(e).__name__} {msg}") or _STATUS_LINE_429_RE.match(msg):
        return "429"
    if isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower():
        return "timeout"
    return None


def _on_429() -> None:
    with _lim_lock:
        _lim["halved_until"] = _clock() + _HALVE_FOR_S
        for b in _lim["lanes"].values():
            b["tokens"] = min(b["tokens"], 0.0)
    _state["rate_hits"] += 1


def _call(tool: str, args: dict | None = None, *, lane: str = "live"):
    """The ONLY path to Robinhood. Raises RHForbidden for anything outside the boundary."""
    args = dict(args or {})
    if lane not in LANES:
        raise ValueError(f"unknown Robinhood rate lane {lane!r} (use one of {LANES})")
    if _is_forbidden(tool):
        raise RHForbidden(f"{tool}: trading/order tools are never callable from Mason")
    if tool in WRITE_TOOLS:
        path = WRITE_TOOLS[tool]
        if not _write_allowed(path):
            where = ".".join(path) if path[0] == "rh_expansion" else "robinhood." + ".".join(path)
            raise RHForbidden(f"{tool}: write disabled ({where}=false)")
        if tool == "follow_watchlist":
            _check_follow_target(args)
            args = {"list_id": args["list_id"]}      # send nothing else to RH
    elif tool not in READ_TOOLS:
        raise RHForbidden(f"{tool}: not on Mason's Robinhood allowlist")
    max_retries = _MAX_RETRIES if lane == "bulk" else _LIVE_MAX_RETRIES
    attempt, slept = 0, 0.0
    while True:
        _acquire(lane)
        _state["calls"] += 1
        _state["last_call_ts"] = (_state["last_call_ts"] + [time.time()])[-120:]
        try:
            data = _invoke(tool, args)
            break
        except Exception as e:
            kind = _retry_kind(e)
            if kind == "429":
                _on_429()                    # every 429 halves both lanes, even the last one
            if kind is None or attempt >= max_retries:
                raise
            if tool in WRITE_TOOLS and kind != "429":
                raise                        # a timed-out write may already have happened
            wait = _backoff_s(attempt)
            if lane == "live" and slept + wait > _LIVE_MAX_BACKOFF_S:
                raise
            _state["retries"] += 1
            _sleep(wait)
            slept += wait
            attempt += 1
    _state["connected"] = True
    _state["last_ok"] = datetime.now().isoformat(timespec="seconds")
    return data


def _check_follow_target(args: dict) -> None:
    """follow_watchlist(list_id) is allowed only for a Robinhood-curated list whose NAME is in
    rh_expansion.crowd.lists, and only with the master switch on. Fail closed on any doubt."""
    if not enabled():
        raise RHForbidden("follow_watchlist: robinhood.enabled=false")
    allowed = {str(n).strip().lower() for n in (_rhx_cfg().get("crowd") or {}).get("lists") or []
               if isinstance(n, str) and n.strip()}
    list_id = args.get("list_id")
    if not allowed or not isinstance(list_id, str) or not list_id:
        raise RHForbidden("follow_watchlist: no list_id or no rh_expansion.crowd.lists configured")
    try:
        data = _call("get_popular_watchlists")
    except Exception as e:
        raise RHForbidden(f"follow_watchlist: could not resolve list name ({type(e).__name__})")
    rows = data if isinstance(data, list) else next(
        (v for v in (data or {}).values() if isinstance(v, list)), []) if isinstance(data, dict) else []
    name = next((r.get("display_name") or r.get("name") for r in rows
                 if isinstance(r, dict) and r.get("id") == list_id), None)
    if not isinstance(name, str) or name.strip().lower() not in allowed:
        raise RHForbidden(f"follow_watchlist: list {name or list_id!r} is not in rh_expansion.crowd.lists")


def _paged(tool: str, args: dict, key: str, max_pages: int = 20) -> list:
    out, cursor = [], None
    for _ in range(max_pages):
        a = dict(args)
        if cursor:
            a["cursor"] = cursor
        data = _call(tool, a) or {}
        out.extend(data.get(key) or [])
        cursor = data.get("next") or data.get("next_cursor")
        if not cursor:
            break
    return out


def _page_rows(data, rows_key, strict=False) -> tuple:
    """(dict rows, error|None) from one page. Only list values count; only their dict items are
    kept. A malformed page yields no rows and an error, never garbage rows. strict (opt-in): a page
    that is not an object carrying `rows_key` is an error too, never "no rows"."""
    if data is None:
        return [], ("malformed page: empty answer" if strict else None)
    if isinstance(data, dict):
        if rows_key:
            val = data.get(rows_key)
            if val is None:
                return [], (f"malformed page: no {rows_key!r} list" if strict else None)
        else:
            val = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        val = data
    if not isinstance(val, list):
        return [], f"malformed page: rows are {type(val).__name__}, not a list"
    rows = [r for r in val if isinstance(r, dict)]
    dropped = len(val) - len(rows)
    return rows, (f"dropped {dropped} non-dict row(s)" if dropped else None)


def _next_cursor(data, cursor_key, next_key):
    if not isinstance(data, dict):
        return None
    keys = (next_key,) if isinstance(next_key, str) else tuple(next_key or ())
    nxt = next((data.get(k) for k in keys if data.get(k)), None)
    if isinstance(nxt, str) and nxt.startswith("http"):
        q = parse_qs(urlparse(nxt).query).get(cursor_key)
        if q:
            return q[0]
    return nxt if isinstance(nxt, (str, int)) and nxt != "" else None


def paged_all(tool: str, args: dict | None = None, *, cursor_key: str = "cursor",
              next_key=("next_cursor", "next"), max_pages: int = 200, lane: str = "bulk",
              rows_key: str | None = None, strict_rows: bool = False) -> tuple:
    """Every page of a cursor-paginated read tool → (rows, {"pages", "truncated", "errors"}).

    Bounded by max_pages; stops if the server hands back a cursor it already gave (a cycle) — both
    set truncated=True. Never raises: a failing page ends the walk with the rows so far plus an
    error. Rows come from `rows_key` or, by default, the first list-valued key of each page.
    Default lane is 'bulk' (backfills/sweeps). strict_rows (opt-in, the transfer check): an empty
    answer, a page without its `rows_key` list, or the master switch off is an error, never "no rows"."""
    rows, meta = [], {"pages": 0, "truncated": False, "errors": []}
    if not enabled():                        # master switch: no hook call at all
        if strict_rows:
            meta["errors"].append("robinhood.enabled is off")
        return rows, meta
    seen, cursor = set(), None
    try:
        max_pages = max(1, int(max_pages))
    except (TypeError, ValueError):
        max_pages = 200
    while True:
        a = dict(args or {})
        if cursor is not None:
            a[cursor_key] = cursor
        try:
            data = _call(tool, a, lane=lane)
        except Exception as e:
            meta["errors"].append(f"{tool} page {meta['pages'] + 1}: {type(e).__name__}: {e}"[:300])
            _err(f"paged_all {tool}", e)
            break
        meta["pages"] += 1
        page_rows, perr = _page_rows(data, rows_key, strict_rows)
        rows.extend(page_rows)
        if perr:
            meta["errors"].append(f"{tool} page {meta['pages']}: {perr}")
        nxt = _next_cursor(data, cursor_key, next_key)
        if nxt is None:
            break
        if nxt in seen or nxt == cursor:
            meta["truncated"] = True
            meta["errors"].append(f"{tool}: cursor cycle at page {meta['pages']} — stopped")
            break
        if meta["pages"] >= max_pages:
            meta["truncated"] = True
            break
        if cursor is not None:
            seen.add(cursor)
        seen.add(nxt)
        cursor = nxt
    if meta["truncated"]:
        _state["paged_truncated"] += 1
    return rows, meta


def _f(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ── accounts ──────────────────────────────────────────────────────────────────
_TYPE_LABELS = {"ira_roth": "Roth IRA", "ira_traditional": "Traditional IRA"}


def _label(a: dict) -> str:
    return (a.get("nickname") or "").strip() or _TYPE_LABELS.get(
        a.get("brokerage_account_type"), a.get("brokerage_account_type") or a.get("account_number"))


def accounts() -> list:
    """Tracked accounts (RH-D2): [{label, number, rhs_number, masked, type, account_type, is_default, option_level}]."""
    if not enabled():
        return []
    try:
        mode = _scope_mode()                 # phase 2 A2: last-4 identity (RULINGS #2)
        if mode["mode"] == "closed":
            return _accounts_closed(mode)
        if mode["mode"] == "map":
            return _accounts_map(mode)
        _scope_note(("legacy",), "last-4 role map not configured; using legacy label scope")
        if (_acct_cache["rows"] is not None and time.time() - _acct_cache["ts"] < _ACCT_TTL_S
                and _acct_cache.get("sig") in (None, "legacy")):
            return list(_acct_cache["rows"])
        want = {s.lower() for s in (_cfg().get("accounts") or [])}
        rows, live = [], set()
        for a in (_call("get_accounts") or {}).get("accounts") or []:
            if a.get("deactivated") or a.get("permanently_deactivated") or a.get("state") != "active":
                continue
            lab = _label(a)
            live.add(str(lab).lower())
            if want and lab.lower() not in want:
                continue
            num = str(a.get("account_number") or "")
            rows.append({"label": lab, "number": num,
                         "rhs_number": str(a.get("rhs_account_number") or num),
                         "masked": MASK_PREFIX + num[-4:], "type": a.get("type"),
                         "account_type": a.get("brokerage_account_type"),
                         "is_default": bool(a.get("is_default")),
                         "option_level": a.get("option_level") or ""})
        _acct_cache.update(ts=time.time(), rows=rows, sig="legacy")
        _warn_missing_accounts(live)
        return list(rows)
    except Exception as e:
        _err("accounts", e)
        return []


_acct_missing_seen = set()


def _warn_missing_accounts(live) -> None:
    """RULINGS #28 (F-ACCT): a configured robinhood.accounts label with no active RH account is dropped from
    every surface (fail safe). Record ONE status error per distinct missing-label set (visible in status()),
    naming only the configured labels (digit runs masked). Never raises; never changes the returned rows."""
    try:
        missing = [str(s) for s in (_cfg().get("accounts") or []) if str(s).lower() not in live]
        key = frozenset(m.lower() for m in missing)
        if not missing or key in _acct_missing_seen or len(_acct_missing_seen) >= 50:
            return
        _acct_missing_seen.add(key)
        names = ", ".join(repr(re.sub(r"\d{5,}", lambda d: MASK_PREFIX + d.group()[-4:], "".join(
            ch for ch in lab if ch.isprintable()))[:60]) for lab in missing[:10])
        _err("accounts", ValueError(f"configured account{'s' if len(missing) > 1 else ''} {names} not found "
                                    "among active Robinhood accounts (dropped from every surface; "
                                    "rename it back in the Robinhood app or update robinhood.accounts)"))
    except Exception:
        pass


# ── positions ─────────────────────────────────────────────────────────────────
def _instrument(option_id: str) -> dict:
    if option_id in _instr_cache:
        return _instr_cache[option_id]
    data = _call("get_option_instruments", {"ids": option_id}) or {}
    rows = data.get("results") or data.get("instruments") or []
    row = next((r for r in rows if r.get("id") == option_id), rows[0] if rows else {})
    _instr_cache[option_id] = row
    return row


def _positions_for(acct: dict) -> list:
    out = []
    base = {"account": acct["label"], "account_masked": acct["masked"], "source": "robinhood"}
    for p in _paged("get_equity_positions", {"account_number": acct["number"]}, "positions"):
        qty = _f(p.get("quantity"), 0.0)
        if not qty:
            continue
        out.append(dict(base, kind="equity", ticker=(p.get("symbol") or "").upper(),
                        underlying=(p.get("symbol") or "").upper(), qty=qty,
                        avg_cost=_f(p.get("average_buy_price")), side=p.get("type") or "long"))
    for p in _paged("get_option_positions", {"account_number": acct["number"], "nonzero": True},
                    "positions"):
        qty = _f(p.get("quantity"), 0.0)
        if not qty:
            continue
        oid = p.get("option_id") or p.get("option") or ""
        ins = {}
        try:
            ins = _instrument(oid) if oid else {}
        except Exception as e:
            _err("option_instrument", e)
        und = (p.get("chain_symbol") or ins.get("chain_symbol") or "").upper()
        out.append(dict(base, kind="option", ticker=und, underlying=und, option_id=oid, qty=qty,
                        avg_cost=_f(p.get("average_price")), side=p.get("type") or "long",
                        strike=_f(ins.get("strike_price")), option_type=ins.get("type"),
                        expiry=p.get("expiration_date") or ins.get("expiration_date")))
    try:
        for p in _paged("get_crypto_positions", {"rhs_account_number": acct["rhs_number"]}, "positions"):
            qty = _f(p.get("quantity"), 0.0)
            if not qty:
                continue
            code = (p.get("currency_code") or p.get("asset") or p.get("symbol") or "").upper()
            out.append(dict(base, kind="crypto", ticker=code, underlying=code, qty=qty,
                            avg_cost=_f(p.get("average_cost") or p.get("cost_basis_per_unit")),
                            side="long"))
    except Exception as e:
        _err(f"crypto_positions {acct['masked']}", e)
    return out


def positions(*, max_age: float | None = None) -> list:
    """Flat list across tracked accounts: equity, option (enriched: strike/type/expiry), crypto."""
    if not enabled():
        return []
    try:
        ttl = max_age if max_age is not None else float(_cfg()["poll_seconds"].get("positions") or 300)
        sig = _scope_mode()["sig"]           # phase 2 A2: a role-map change must not be served from the old cache
        if (_pos_cache.get("rows") is not None and time.time() - _pos_cache.get("ts", 0) < ttl
                and _pos_cache.get("sig", "legacy") == sig):
            return list(_pos_cache["rows"])
        rows = []
        for a in accounts():
            try:
                rows.extend(_positions_for(a))
            except Exception as e:
                _err(f"positions {a['masked']}", e)
        _pos_cache.update(ts=time.time(), rows=rows, sig=sig)
        return list(rows)
    except Exception as e:
        _err("positions", e)
        return []


def position_symbols() -> set:
    """Equity tickers + option underlyings (crypto excluded — not on the equity tape)."""
    return {p["underlying"] for p in positions() if p["kind"] in ("equity", "option") and p["underlying"]}


# ── quotes ────────────────────────────────────────────────────────────────────
def _norm_quote(r: dict) -> dict:
    q = r.get("quote") or {}
    reg, ext = _f(q.get("last_trade_price")), _f(q.get("last_non_reg_trade_price"))
    reg_t, ext_t = q.get("venue_last_trade_time") or "", q.get("venue_last_non_reg_trade_time") or ""
    use_ext = ext is not None and (reg is None or ext_t > reg_t)
    last = ext if use_ext else reg
    prev = _f(q.get("adjusted_previous_close")) or _f(q.get("previous_close"))
    close = r.get("close") or {}
    return {"symbol": q.get("symbol"), "last": last, "as_of": ext_t if use_ext else reg_t,
            "session": "ext" if use_ext else "reg", "bid": _f(q.get("bid_price")) or None,
            "ask": _f(q.get("ask_price")) or None, "prev_close": prev,
            "official_close": _f(close.get("price")), "official_close_date": close.get("date"),
            "chg_pct": ((last - prev) / prev * 100.0) if (last is not None and prev) else None,
            "state": q.get("state"), "has_traded": q.get("has_traded"), "source": "rh"}


def quotes(symbols) -> dict:
    """{SYMBOL: {last, as_of, session, bid, ask, prev_close, chg_pct, ...}}; batches of 20, 5s cache."""
    if not enabled():
        return {}
    out = {}
    try:
        now = time.time()
        want = []
        for s in dict.fromkeys(str(x).upper().strip() for x in (symbols or []) if x):
            hit = _quote_cache.get(s)
            if hit and now - hit[0] < _QUOTE_TTL_S:
                out[s] = hit[1]
            else:
                want.append(s)
        for i in range(0, len(want), 20):
            try:
                data = _call("get_equity_quotes", {"symbols": want[i:i + 20]}) or {}
                for r in data.get("results") or []:
                    nq = _norm_quote(r)
                    if nq["symbol"]:
                        s = nq["symbol"].upper()
                        out[s] = nq
                        _quote_cache[s] = (time.time(), nq)
            except Exception as e:
                _err("quotes", e)
        return out
    except Exception as e:
        _err("quotes", e)
        return out


def option_quotes(option_ids) -> dict:
    if not enabled():
        return {}
    out = {}
    try:
        ids = [i for i in dict.fromkeys(option_ids or []) if i]
        for i in range(0, len(ids), 20):
            data = _call("get_option_quotes", {"instrument_ids": ids[i:i + 20]}) or {}
            for r in data.get("results") or []:
                q = r.get("quote") or r
                key = q.get("instrument_id") or q.get("id") or ""
                if key:
                    out[key] = q
    except Exception as e:
        _err("option_quotes", e)
    return out


# ── portfolio / watchlists / alerts / orders ──────────────────────────────────
def snapshot() -> dict:
    """{label: {"masked", "portfolio": {...}, "positions": [...]}} for every tracked account."""
    if not enabled():
        return {}
    try:
        pos = positions()
        out = {}
        for a in accounts():
            try:
                port = _call("get_portfolio", {"account_number": a["number"]}) or {}
            except Exception as e:
                _err(f"portfolio {a['masked']}", e)
                port = {}
            out[a["label"]] = {"masked": a["masked"],
                               "total_value": _f(port.get("total_value")),
                               "cash": _f(port.get("cash")),
                               "buying_power": _f((port.get("buying_power") or {}).get("buying_power")),
                               "positions": [p for p in pos if p["account"] == a["label"]]}
        return out
    except Exception as e:
        _err("snapshot", e)
        return {}


def watchlists() -> list:
    """Custom + followed lists with their symbols: [{id, name, owner_type, types, symbols, items}]."""
    if not enabled():
        return []
    try:
        out = []
        for w in (_call("get_watchlists") or {}).get("watchlists") or []:
            types = w.get("allowed_object_types") or []
            row = {"id": w.get("id"), "name": w.get("display_name"), "owner_type": w.get("owner_type"),
                   "types": types, "symbols": [], "items": []}
            if types == ["option_strategy"]:
                out.append(row)              # options list: see option_watchlist()
                continue
            try:
                items = (_call("get_watchlist_items", {"list_id": w["id"]}) or {}).get("items") or []
                row["items"] = items
                row["symbols"] = [str(i.get("symbol")).upper() for i in items if i.get("symbol")]
            except Exception as e:
                _err(f"watchlist_items {w.get('display_name')}", e)
            out.append(row)
        return out
    except Exception as e:
        _err("watchlists", e)
        return []


def option_watchlist() -> list:
    if not enabled():
        return []
    try:
        d = _call("get_option_watchlist") or {}
        return d.get("items") or d.get("options") or d.get("results") or []
    except Exception as e:
        _err("option_watchlist", e)
        return []


def alerts() -> list:
    if not enabled():
        return []
    try:
        return _paged("get_alerts", {}, "alerts")
    except Exception as e:
        _err("alerts", e)
        return []


def orders(since_iso: str) -> list:
    """Equity + option orders since a date (informational, e.g. for the activity log)."""
    if not enabled():
        return []
    out = []
    for a in accounts():
        for tool, kind in (("get_equity_orders", "equity"), ("get_option_orders", "option")):
            try:
                for o in _paged(tool, {"account_number": a["number"], "created_at_gte": since_iso},
                                "orders", max_pages=5):
                    out.append(dict(o, kind=kind, account=a["label"]))
            except Exception as e:
                _err(f"{tool} {a['masked']}", e)
    return out


# ── personal_positions reconcile (RH-A1) ──────────────────────────────────────
# RH Expansion S1-T10 (Nico, 2026-09-30): Robinhood becomes the source of truth for the accounts in
# robinhood.personal_positions_sync.label_map (Play → Play Money, Roth → Roth IRA, Savings → ETF
# Savings). Every other account (Schwab, …) keeps hand entry, untouched.
#   sync_personal_positions(dry_run=True)                  diff only — the v1 shape, unchanged
#   sync_personal_positions(dry_run=False, cutover=True)   one-time cutover, drift-gated against
#                                                          the approved baseline, one transaction
#   sync_personal_positions(dry_run=False)                 ongoing reconcile (after the cutover)
# Synced rows: created_by='robinhood_sync', account_label = the RH account name, same columns as a
# hand row (+ extra_json.rh_sync). Writes take _pp_lock (in-process) and BEGIN IMMEDIATE (any
# process), so a cutover and a reconcile tick never interleave. Fail closed: an RH outage, an
# empty/malformed answer, or a bad quantity never closes a position.
_PP_TABLE = "personal_positions"
_PP_SYNC_BY = "robinhood_sync"
_PP_CUTOVER_KEY = "rh_personal_cutover"
_PP_CUTOVER_REASON = "rh cutover 2026-09-30 baseline"
_PP_BASELINE = (Path(__file__).resolve().parent / "docs" / "robinhood" / "expansion" / "S1"
                / "step2_approved_diff.json")
_PP_DRIFT_QTY = 0.01             # max |hand − RH| shares per changed ticker (DRIP-sized)
_PP_DRIFT_CASH = 25.0            # max |hand − RH| cash per account, $
_PP_QTY_EPS = 1e-9
_PP_CASH_EPS = 0.005
_PP_MAX_PAGES = 20
_PP_LOCK_TIMEOUT_S = 10.0
_pp_lock = threading.Lock()
_activity_log = None             # hook: fn(type, title, **kw); None → mason_activity.log_activity
_pp_clock = time.time            # hook: wall clock for the two-strike mass-close rule (tests advance it)
_PP_STRIKE_PREFIX = "rh_personal_pending_close:"


def _personal_open_rows() -> list:
    import mason_personal
    return mason_personal.get_open_positions()


def _pp_lmap() -> dict:
    lm = _cfg()["personal_positions_sync"].get("label_map")
    if not isinstance(lm, dict):
        return {}
    return {k.strip(): v.strip() for k, v in lm.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}


def _pp_resolver(lmap: dict):
    """account_label → RH account name, or None for a hand-entry account. Case/space-insensitive on
    the hand label (label_map key) AND on the RH name itself (synced rows, or a hand row typed with
    the RH name) — so no row for a synced account can slip past the diff."""
    keys = {str(k).strip().lower(): v for k, v in lmap.items()}
    names = {str(v).strip().lower(): v for v in lmap.values()}

    def resolve(label):
        k = str(label or "").strip().lower()
        return keys.get(k) or names.get(k)
    return resolve


def _pp_diff(rows, rh_rows, lmap: dict, canon=None) -> dict:
    """The dry-run diff (v1 shape): hand/synced rows of mapped accounts vs RH equity positions.
    `canon` (last-4 map mode, RULINGS #4): RH account label -> the label_map / baseline NAME of the same account
    (None = not a synced account), so a renamed account still diffs under its baseline name."""
    res = {"match": [], "changed": [], "hand_only": [], "rh_only": [], "cash": [], "written": 0}
    resolve = _pp_resolver(lmap)
    hand = {}
    for r in rows or []:
        rh_label = resolve(r.get("account_label"))
        if not rh_label:
            continue                      # not a Robinhood account — hand entry stays
        t = (r.get("ticker") or "").upper()
        if t == "CASH":
            res["cash"].append({"account_label": r.get("account_label"), "rh_account": rh_label,
                                "hand": _f(r.get("shares"))})
            continue
        k = (rh_label, t)
        h = hand.setdefault(k, {"account_label": r.get("account_label"), "rh_account": rh_label,
                                "ticker": t, "hand_qty": 0.0, "ids": []})
        h["hand_qty"] += _f(r.get("shares"), 0.0)
        h["ids"].append(r.get("id"))
    rh = {}
    for p in rh_rows or []:
        acct = canon(p["account"]) if canon else p["account"]
        if p["kind"] != "equity" or acct not in lmap.values():
            continue
        k = (acct, p["ticker"])
        rh.setdefault(k, {"rh_account": acct, "ticker": p["ticker"], "rh_qty": 0.0,
                          "avg_cost": p["avg_cost"]})["rh_qty"] += p["qty"]
    for k, h in hand.items():
        if k not in rh:
            res["hand_only"].append(h)
        elif abs(h["hand_qty"] - rh[k]["rh_qty"]) < 1e-4:
            res["match"].append(dict(h, **rh[k]))
        else:
            res["changed"].append(dict(h, **rh[k]))
    res["rh_only"] = [v for k, v in rh.items() if k not in hand]
    return res


def sync_personal_positions(*, dry_run: bool = True, cutover: bool = False) -> dict:
    """Diff (dry_run), first-time cutover (dry_run=False, cutover=True) or ongoing reconcile
    (dry_run=False) of personal_positions for the label_map accounts against Robinhood. Never raises.
    Writes need robinhood.enabled AND robinhood.personal_positions_sync.enabled."""
    res = {"match": [], "changed": [], "hand_only": [], "rh_only": [], "cash": [], "written": 0}
    if not enabled():
        return res
    if not dry_run:
        if not _flag("personal_positions_sync", "enabled"):
            return {"written": 0, "skipped": "personal_positions_sync disabled"}
        try:
            return _pp_cutover(_pp_lmap()) if cutover else _pp_reconcile(_pp_lmap())
        except Exception as e:
            _err("sync_personal_positions", e)
            return {"written": 0, "error": _pp_redact(f"{type(e).__name__}: {e}")}
    try:
        lmap = _pp_lmap()
        canon, labels, berrs = None, {}, []
        mode = _scope_mode()
        if mode["mode"] == "closed":
            res["error"] = "last-4 role map unusable; no account is available"
            return res
        if mode["mode"] == "map":                  # RULINGS #4: a renamed account still diffs under its baseline name
            try:
                marker = _pp_marker_read()
            except Exception:
                marker = None
            bound, berrs = _pp_bind(sorted(set(lmap.values())), marker, accounts())
            labels = {n: r["label"] for n, r in bound.items()}
            by_label = {lab: n for n, lab in labels.items()}
            canon = by_label.get
        res = _pp_diff(_personal_open_rows(), positions(), lmap, canon)
        if berrs:
            res["bind_errors"] = [_pp_redact(x) for x in berrs]
        if res["cash"]:
            snap = snapshot()
            for c in res["cash"]:
                c["rh"] = (snap.get(labels.get(c["rh_account"], c["rh_account"])) or {}).get("cash")
        return res
    except Exception as e:
        _err("sync_personal_positions", e)
        res["error"] = str(e)
        return res


def _pp_redact(s) -> str:
    """Mask anything that looks like a full account number (keep the last 4)."""
    return re.sub(r"\d{6,}", lambda m: MASK_PREFIX + m.group(0)[-4:], str(s))


def _pp_today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _pp_now() -> str:
    return datetime.now().isoformat()


def _pp_log(title: str, *, ticker=None, extra=None, tier: str = "work") -> None:
    """One Activity v2 row (default 'work' tier). Never raises."""
    try:
        fn = _activity_log
        if fn is None:
            import mason_activity
            fn = mason_activity.log_activity
        fn("rh_personal_sync", _pp_redact(title), actor="system", category="analyst", tier=tier,
           ticker=ticker, extra=extra or {})
    except Exception as e:
        _err("personal sync activity", e)


def _pp_bind(names, marker, rows) -> tuple:
    """Last-4 map mode (RULINGS #4): bind each synced-account NAME (the label at cutover / a label_map value) to the
    CURRENT personal account row, by identity. Order: the marker's accounts_last4 binding; else the alias history
    (never the live label first: a live label can belong to a different, renamed account); else, only when the name
    has no history at all, an exact live label among PERSONAL rows. A name that is ambiguous, recorded for a
    non-personal account too, or matches no personal account is NOT bound (never guessed). -> ({name: row}, errors)"""
    bound_keys = marker.get(_MARKER_LAST4) if isinstance(marker, dict) else None
    bound_keys = bound_keys if isinstance(bound_keys, dict) else {}
    personal = [r for r in rows or [] if isinstance(r, dict) and r.get("role") == "personal" and r.get("key")]
    by_key = {r["key"]: r for r in personal}
    kmap = _scope_mode()["map"]
    entries, _ = _alias_view()
    out, errs = {}, []
    for name in names:
        k = bound_keys.get(name)
        if isinstance(k, str) and k:
            if k in by_key:
                out[name] = by_key[k]
            else:
                errs.append(f"{name}: its recorded account is not an active personal account")
            continue
        nn = norm_account_label(name)
        ekeys = [ek for ek, labs in entries.items() if nn and nn in labs]
        if ekeys:
            if {_role_of_key(ek, kmap) for ek in ekeys} != {"personal"}:
                errs.append(f"{name}: label also recorded for a non-personal account; not bound")
                continue
            cands = {r["key"]: r for r in personal
                     if any(r["key"] == ek or r["key"].endswith(ek) or ek.endswith(r["key"]) for ek in ekeys)}
            if len(cands) == 1:
                out[name] = next(iter(cands.values()))
            else:
                errs.append(f"{name}: cannot resolve to exactly one personal account by last-4; not bound")
            continue
        live = [r for r in personal if nn and norm_account_label(r.get("label")) == nn]
        if len(live) == 1:
            out[name] = live[0]
        else:
            errs.append(f"{name}: not found among the personal accounts")
    return out, errs


def _pp_book(rh_names, acct_for=None) -> tuple:
    """Strict read of the synced accounts → ({name: book}, errors). book = {"pos": {T: {"qty",
    "avg_cost"}}, "zero": {T} (RH reports qty 0), "bad": {T} (NaN/negative/unparseable qty),
    "raw_n": rows RH returned, "cash", "equity_value"}. An account whose positions cannot be read
    cleanly (outage, malformed page, endless cursor) is left OUT, so its rows are never touched."""
    import math
    errors = []
    try:
        accts = acct_for if acct_for is not None else {a["label"]: a for a in accounts()}
    except Exception as e:
        accts = {}
        _err("personal sync accounts", e)
    out = {}
    for name in rh_names:
        a = accts.get(name)
        if not a:
            errors.append(f"{name}: account unavailable from Robinhood")
            continue
        try:
            raw, cursor = [], None
            for _ in range(_PP_MAX_PAGES):
                args = {"account_number": a["number"]}
                if cursor:
                    args["cursor"] = cursor
                data = _call("get_equity_positions", args)
                if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
                    raise RHError("malformed get_equity_positions page")
                raw.extend(data["positions"])
                cursor = data.get("next") or data.get("next_cursor")
                if not cursor:
                    break
            else:
                raise RHError(f"positions cursor did not end within {_PP_MAX_PAGES} pages")
            pos, zero, bad = {}, set(), set()
            for p in raw:
                if not isinstance(p, dict) or not str(p.get("symbol") or "").strip():
                    raise RHError("malformed position row")
                t = str(p["symbol"]).strip().upper()
                qty = _f(p.get("quantity"))
                if qty is None or not math.isfinite(qty) or qty < 0:
                    bad.add(t)
                    errors.append(f"{name} {t}: invalid quantity {p.get('quantity')!r} — left unchanged")
                    continue
                if qty == 0:
                    zero.add(t)
                    continue
                cost = _f(p.get("average_buy_price"))
                cost = cost if (cost is not None and math.isfinite(cost) and cost >= 0) else None
                cur = pos.get(t)
                if cur:                       # same symbol twice: merge, weighted cost
                    if cur["avg_cost"] is not None and cost is not None:
                        cur["avg_cost"] = (cur["avg_cost"] * cur["qty"] + cost * qty) / (cur["qty"] + qty)
                    cur["qty"] += qty
                else:
                    pos[t] = {"qty": qty, "avg_cost": cost}
            book = {"pos": pos, "zero": zero - set(pos), "bad": bad - set(pos), "raw_n": len(raw),
                    "cash": None, "equity_value": None, "_number": a["number"]}
        except Exception as e:
            _err(f"personal sync positions {a['masked']}", e)
            errors.append(f"{name} ({a['masked']}): positions unavailable: {type(e).__name__}: {e}")
            continue
        try:
            port = _call("get_portfolio", {"account_number": a["number"]})
            port = port if isinstance(port, dict) else {}
            for k in ("cash", "equity_value"):
                v = _f(port.get(k))
                book[k] = v if (v is not None and math.isfinite(v) and v >= 0) else None
            if book["cash"] is None:
                errors.append(f"{name} ({a['masked']}): cash unavailable")
        except Exception as e:
            _err(f"personal sync portfolio {a['masked']}", e)
            errors.append(f"{name} ({a['masked']}): portfolio unavailable: {type(e).__name__}: {e}")
        out[name] = book
    return out, [_pp_redact(x) for x in errors]


def _pp_marker(conn):
    r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_PP_CUTOVER_KEY,)).fetchone()
    if not r:
        return None
    try:
        v = json.loads(r[0] or "null")
    except (TypeError, ValueError):
        v = None
    return v if isinstance(v, dict) else {"malformed": True}


def _pp_marker_read():
    import mason_db
    conn = mason_db.get_connection()
    try:
        return _pp_marker(conn)
    finally:
        conn.close()


def _pp_begin(conn) -> None:
    """Exclusive write lock across processes; fails (no partial writes) after _PP_LOCK_TIMEOUT_S."""
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {int(_PP_LOCK_TIMEOUT_S * 1000)}")
    conn.execute("BEGIN IMMEDIATE")


def _pp_insert(conn, row: dict) -> None:
    import mason_db
    cfg = mason_db.TABLES[_PP_TABLE]
    cols = cfg["columns"]
    conn.execute(f"INSERT INTO {_PP_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 mason_db._row_to_values(row, cfg))


def _pp_row(ticker, account, shares, cost, entry_date, now, replaced, last4=None) -> dict:
    import uuid
    row = {"id": str(uuid.uuid4())[:8], "created_at": now, "created_by": _PP_SYNC_BY, "updated_at": now,
            "ticker": ticker, "account_label": account, "shares": float(shares), "cost_basis": float(cost),
            "entry_date": entry_date, "status": "open", "pending_action": None, "pending_close_price": None,
            "pending_close_date": None, "close_date": None, "close_price": None, "realized_pl": None,
            "notes": None, "rh_sync": {"source": "robinhood", "synced_at": now, "replaced_ids": list(replaced)}}
    if last4:                                    # RULINGS #3.6: map mode tags the row with its role-map key (private DB)
        row["rh_sync"]["last4"] = last4
    return row


def _pp_first_buy(conn, account, ticker):
    """ET date of the first filled RH buy of ticker in account (rh_orders), or None."""
    try:
        r = conn.execute("SELECT MIN(filled_at) FROM rh_orders WHERE account_label = ? AND kind = 'equity' "
                         "AND upper(symbol) = ? AND side = 'buy' AND filled_at IS NOT NULL "
                         "AND coalesce(record_deleted, 0) = 0", (account, ticker)).fetchone()
        if not r or not r[0]:
            return None
        from zoneinfo import ZoneInfo
        from datetime import timezone
        ts = datetime.fromisoformat(str(r[0]).replace("Z", "+00:00"))
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return None


def _pp_open_rows(conn) -> list:
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {_PP_TABLE} WHERE is_deleted = 0 AND status = 'open' ORDER BY created_at, id")]


def _pp_drift(diff: dict, book: dict, lmap: dict) -> list:
    """Reasons the fresh diff is NOT the approved baseline (empty list = gate passes)."""
    reasons = []
    try:
        base = json.loads(_PP_BASELINE.read_text(encoding="utf-8"))
        if (base.get("_approved") or {}).get("by") != "nico":
            raise ValueError("baseline not approved")
    except Exception as e:
        return [f"approved baseline unavailable: {type(e).__name__}"]
    base_accts = {r.get("rh_account") for s in ("match", "changed", "cash") for r in base.get(s) or []}
    if set(lmap.values()) != base_accts:
        reasons.append(f"label_map accounts {sorted(lmap.values())} differ from the baseline's {sorted(base_accts)}")
    for name in sorted(set(lmap.values()) - set(book)):
        reasons.append(f"{name}: Robinhood positions unavailable")
    for name, b in sorted(book.items()):
        for t in sorted(b["bad"]):
            reasons.append(f"{name} {t}: invalid Robinhood quantity")
    for r in diff["hand_only"]:
        reasons.append(f"hand_only {r['rh_account']} {r['ticker']} ({r['hand_qty']:g} sh)")
    for r in diff["rh_only"]:
        reasons.append(f"rh_only {r['rh_account']} {r['ticker']} ({r['rh_qty']:g} sh)")
    for r in diff["changed"]:
        d = abs(r["rh_qty"] - r["hand_qty"])
        if d > _PP_DRIFT_QTY + _PP_QTY_EPS:
            reasons.append(f"changed {r['rh_account']} {r['ticker']} by {d:g} sh (> {_PP_DRIFT_QTY})")
    have = {(r["rh_account"], r["ticker"]) for r in diff["match"] + diff["changed"]}
    for r in (base.get("match") or []) + (base.get("changed") or []):
        if (r.get("rh_account"), r.get("ticker")) not in have:
            reasons.append(f"baseline position {r.get('rh_account')} {r.get('ticker')} no longer on both sides")
    hand_cash = {}
    for c in diff["cash"]:
        hand_cash[c["rh_account"]] = hand_cash.get(c["rh_account"], 0.0) + (c.get("hand") or 0.0)
    for name in sorted(set(lmap.values())):
        rh_cash = (book.get(name) or {}).get("cash")
        if rh_cash is None:
            reasons.append(f"cash {name}: Robinhood cash unavailable")
        elif abs(rh_cash - hand_cash.get(name, 0.0)) > _PP_DRIFT_CASH + _PP_CASH_EPS:
            reasons.append(f"cash {name}: delta ${abs(rh_cash - hand_cash.get(name, 0.0)):.2f} > ${_PP_DRIFT_CASH:g}")
    return reasons


def _pp_cutover(lmap: dict) -> dict:
    """First-time cutover: drift gate → one transaction → desk_state marker. Second run = no-op."""
    import mason_db
    m = _pp_marker_read()
    if m:
        return {"already_cut_over": True, "cutover": m, "written": 0}
    mode = _scope_mode()
    if mode["mode"] == "closed":
        return {"written": 0, "error": "last-4 role map unusable; no account is available"}
    bound = {}
    if mode["mode"] == "map":                   # RULINGS #4: identity by last-4; the marker records it
        bound, berrs = _pp_bind(sorted(set(lmap.values())), None, accounts())
        book, errors = _pp_book(sorted(set(lmap.values())), acct_for=bound)
        errors = errors + [_pp_redact(x) for x in berrs]
    else:
        book, errors = _pp_book(sorted(set(lmap.values())))
    if not _pp_lock.acquire(timeout=_PP_LOCK_TIMEOUT_S):
        return {"written": 0, "error": "another personal sync is running"}
    try:
        conn = mason_db.get_connection()
        try:
            _pp_begin(conn)
            try:
                m = _pp_marker(conn)
                if m:
                    conn.execute("ROLLBACK")
                    return {"already_cut_over": True, "cutover": m, "written": 0}
                rows = _pp_open_rows(conn)
                rh_rows = [{"kind": "equity", "account": n, "ticker": t, "qty": v["qty"], "avg_cost": v["avg_cost"]}
                           for n, b in book.items() for t, v in b["pos"].items()]
                diff = _pp_diff(rows, rh_rows, lmap)
                for c in diff["cash"]:
                    c["rh"] = (book.get(c["rh_account"]) or {}).get("cash")
                reasons = _pp_drift(diff, book, lmap)
                if reasons:
                    conn.execute("ROLLBACK")
                    return {"aborted": "drift", "diff": diff, "reasons": reasons, "errors": errors, "written": 0}
                resolve, now, today = _pp_resolver(lmap), _pp_now(), _pp_today()
                mine = [r for r in rows if resolve(r.get("account_label"))]
                groups = {}
                for r in mine:
                    groups.setdefault((resolve(r["account_label"]), (r.get("ticker") or "").upper()), []).append(r)
                rows_in = 0
                for name in sorted(book):
                    for t, v in sorted(book[name]["pos"].items()):
                        hand = groups.get((name, t), [])
                        dates = sorted(str(h["entry_date"]) for h in hand if h.get("entry_date"))
                        entry = (dates[0] if dates else None) or _pp_first_buy(conn, name, t) or today
                        cost = v["avg_cost"]
                        if cost is None:
                            q = sum(_f(h.get("shares"), 0.0) for h in hand)
                            cost = (sum(_f(h.get("shares"), 0.0) * _f(h.get("cost_basis"), 0.0) for h in hand) / q
                                    if q else 0.0)
                        _pp_insert(conn, _pp_row(t, name, v["qty"], cost, entry, now, [h["id"] for h in hand],
                                                 (bound.get(name) or {}).get("key")))
                        rows_in += 1
                    hand = groups.get((name, "CASH"), [])
                    dates = sorted(str(h["entry_date"]) for h in hand if h.get("entry_date"))
                    _pp_insert(conn, _pp_row("CASH", name, book[name]["cash"], 1.0, dates[0] if dates else today,
                                             now, [h["id"] for h in hand], (bound.get(name) or {}).get("key")))
                    rows_in += 1
                for r in mine:
                    try:
                        extra = json.loads(r.get("extra_json") or "{}")
                    except (TypeError, ValueError):
                        extra = {}
                    extra = extra if isinstance(extra, dict) else {"_prior_extra": extra}
                    extra["pre_cutover"] = dict(r)
                    conn.execute(f"UPDATE {_PP_TABLE} SET is_deleted = 1, deleted_at = ?, deleted_by = ?, "
                                 f"deleted_reason = ?, extra_json = ? WHERE id = ? AND is_deleted = 0",
                                 (now, _PP_SYNC_BY, _PP_CUTOVER_REASON, json.dumps(extra, default=str), r["id"]))
                marker = {"done_at": now, "rows_in": rows_in, "rows_out": len(mine),
                          "accounts": sorted(book), "label_map": dict(lmap),
                          "baseline": "docs/robinhood/expansion/S1/step2_approved_diff.json"}
                if bound:
                    marker[_MARKER_LAST4] = {n: bound[n]["key"] for n in sorted(book) if n in bound}
                conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                             "updated_date = excluded.updated_date",
                             (_PP_CUTOVER_KEY, json.dumps(marker), now))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    finally:
        _pp_lock.release()
    _pp_log(f"Robinhood cutover: {rows_in} synced rows in, {len(mine)} hand rows retired "
            f"({', '.join(sorted(book))})", extra={"rows_in": rows_in, "rows_out": len(mine)})
    return {"cutover": "done", "rows_in": rows_in, "rows_out": len(mine), "written": rows_in + len(mine),
            "diff": diff, "errors": errors}


def _pp_close_px(qt):
    for k in ("last", "official_close", "prev_close"):
        v = _f((qt or {}).get(k))
        if v is not None and v > 0:
            return v
    return None


def _pp_reconcile(lmap: dict) -> dict:
    """After the cutover: update synced rows in place, add new RH positions, close gone ones (last RH
    quote), keep cash current. Only the accounts recorded at cutover are synced; a later label_map
    change is reported, never acted on. Idempotent; each change → one Activity row (work tier)."""
    import mason_db
    m = _pp_marker_read()
    if not m:
        return {"skipped": "not_cut_over", "written": 0}
    rec_map = m.get("label_map") if isinstance(m.get("label_map"), dict) else {}
    accts = [a for a in (m.get("accounts") or sorted(set(rec_map.values())) or sorted(set(lmap.values())))
             if isinstance(a, str) and a]
    out = {"written": 0, "changes": [], "errors": [], "suspect_empty": []}
    if rec_map and rec_map != lmap:
        out["label_map_changed"] = {"at_cutover": rec_map, "config": lmap,
                                    "note": "reconcile keeps the accounts recorded at cutover"}
    mode = _scope_mode()
    if mode["mode"] == "closed":
        return {"written": 0, "skipped": "role_map_unusable"}
    key_cur, name_key, bind_new, moved, label_key = {}, {}, {}, {}, {}
    if mode["mode"] == "map":
        # RULINGS #4: each synced account is resolved by last-4 -> its CURRENT label. A synced row belongs to the
        # account named by its rh_sync.last4 tag (written when a row is created or relabelled in map mode); an
        # untagged row (a legacy row) belongs to the account bound to the marker NAME it carries. Rows of an
        # account whose label changed are relabelled in the transaction below; an unresolvable name is skipped.
        bound, berrs = _pp_bind(accts, m, accounts())
        out["errors"] += [_pp_redact(x) for x in berrs]
        have_bind = m.get(_MARKER_LAST4) if isinstance(m.get(_MARKER_LAST4), dict) else {}
        bind_new = {n: bound[n]["key"] for n in bound if n not in have_bind}
        key_cur = {r["key"]: r["label"] for r in bound.values()}
        name_key = {norm_account_label(n): r["key"] for n, r in bound.items()}
        label_key = {r["label"]: r["key"] for r in bound.values()}
        accts = [bound[n]["label"] for n in accts if n in bound]

    def _row_key(r):
        """Map mode: the bound role-map key a synced row belongs to (None = leave the row alone)."""
        try:
            ex = json.loads(r.get("extra_json") or "{}")
        except (TypeError, ValueError):
            ex = {}
        tag = ((ex.get("rh_sync") or {}).get("last4") if isinstance(ex, dict)
               and isinstance(ex.get("rh_sync"), dict) else None)
        if tag:
            return tag if tag in key_cur else None
        return name_key.get(norm_account_label(r.get("account_label")))

    def _cur(r):
        k = _row_key(r) if key_cur else None
        return key_cur[k] if k else r["account_label"]
    book, errors = _pp_book(accts)
    out["errors"] += errors
    conn = mason_db.get_connection()
    try:
        pre = [r for r in _pp_open_rows(conn) if r.get("created_by") == _PP_SYNC_BY]
    finally:
        conn.close()
    gone = sorted({(r["ticker"] or "").upper() for r in pre if _cur(r) in book
                   and (r["ticker"] or "").upper() != "CASH"
                   and (r["ticker"] or "").upper() not in book[_cur(r)]["pos"]})
    qts = quotes(gone) if gone else {}
    sells = _pp_prefetch_sells(book)
    xc, xplan, xres = _pp_xfer_cfg(), None, None      # moves between synced accounts (switch off: none of it runs)
    if xc["enabled"]:
        xnow, xwait, xexcl = _pp_clock(), _pp_strike_s(), excluded_accounts()
        xfills = _pp_xfer_prefetch(book, xc, xnow, xwait, xexcl)
    changes, strikes = [], []
    out["pending_close"] = []
    if not _pp_lock.acquire(timeout=_PP_LOCK_TIMEOUT_S):
        out["errors"].append("another personal sync is running")
        return out
    try:
        conn = mason_db.get_connection()
        try:
            _pp_begin(conn)
            try:
                mk = _pp_marker(conn)
                if not mk:
                    conn.execute("ROLLBACK")
                    return {"skipped": "not_cut_over", "written": 0}
                now, today = _pp_now(), _pp_today()
                if key_cur:                         # relabel by ROW ID from each row's own attribution (swap-safe)
                    for r in [dict(x) for x in conn.execute(
                            f"SELECT id, account_label, extra_json FROM {_PP_TABLE} WHERE created_by = ?",
                            (_PP_SYNC_BY,)).fetchall()]:
                        k = _row_key(r)
                        if k and key_cur[k] != r["account_label"]:
                            try:
                                ex = json.loads(r.get("extra_json") or "{}")
                            except (TypeError, ValueError):
                                ex = {}
                            ex = ex if isinstance(ex, dict) else {"_prior_extra": ex}
                            ex.setdefault("rh_sync", {})
                            if isinstance(ex["rh_sync"], dict):
                                ex["rh_sync"]["last4"] = k
                            conn.execute(f"UPDATE {_PP_TABLE} SET account_label = ?, extra_json = ?, "
                                         f"updated_at = ? WHERE id = ?",
                                         (key_cur[k], json.dumps(ex, default=str), now, r["id"]))
                            moved[(r["account_label"], key_cur[k])] = \
                                moved.get((r["account_label"], key_cur[k]), 0) + 1
                if bind_new and isinstance(mk, dict) and not mk.get("malformed"):
                    nb = dict(mk.get(_MARKER_LAST4) or {}) if isinstance(mk.get(_MARKER_LAST4), dict) else {}
                    add = {n: k for n, k in bind_new.items() if n not in nb}
                    if add:
                        nb.update(add)
                        conn.execute("UPDATE desk_state SET value = ?, updated_date = ? WHERE key = ?",
                                     (json.dumps(dict(mk, **{_MARKER_LAST4: nb})), now, _PP_CUTOVER_KEY))
                have = {}
                for r in _pp_open_rows(conn):
                    if r.get("created_by") == _PP_SYNC_BY and r.get("account_label") in accts:
                        have.setdefault((r["account_label"], (r["ticker"] or "").upper()), []).append(r)
                if xc["enabled"]:
                    try:
                        xplan = _pp_xfer_start(conn, have, book, accts, xexcl, xc, xnow, xwait, xfills)
                        out["errors"] += xplan["errors"]
                    except Exception as e:
                        xplan = None
                        _err("personal sync move check", e)
                        out["errors"].append(f"move check skipped on this look: {type(e).__name__}")
                xhold = xplan["hold"] if xplan else {}
                for name in accts:
                    b = book.get(name)
                    if b is None:
                        continue                          # unreadable → untouched (fail closed)
                    mine = {t: rs for (a, t), rs in have.items() if a == name}
                    for t, rs in mine.items():
                        if len(rs) > 1:
                            out["errors"].append(f"{name} {t}: {len(rs)} synced rows — only the first is kept current")
                    suspect = (b["raw_n"] == 0 and any(t != "CASH" for t in mine) and b["equity_value"] != 0.0)
                    if suspect:
                        out["suspect_empty"].append(name)
                    for t, v in sorted(b["pos"].items()):
                        rs = mine.get(t)
                        if rs:
                            r = rs[0]
                            cost = v["avg_cost"] if v["avg_cost"] is not None else _f(r["cost_basis"], 0.0)
                            if (xplan is not None and not cost and _f(r["cost_basis"], 0.0) > 0
                                    and _pp_xfer_moved_in(r)):
                                cost = _f(r["cost_basis"], 0.0)   # RH shows a 0 cost for shares moved in: keep theirs
                            if (abs(_f(r["shares"], 0.0) - v["qty"]) > _PP_QTY_EPS
                                    or abs(_f(r["cost_basis"], 0.0) - cost) > _PP_QTY_EPS):
                                conn.execute(f"UPDATE {_PP_TABLE} SET shares = ?, cost_basis = ?, updated_at = ? "
                                             f"WHERE id = ?", (v["qty"], cost, now, r["id"]))
                                changes.append(("updated", name, t, f"{r['shares']:g} → {v['qty']:g} sh, "
                                                f"cost {r['cost_basis']} → {cost}", r["id"]))
                        else:
                            row = _pp_row(t, name, v["qty"], v["avg_cost"] if v["avg_cost"] is not None else 0.0,
                                          _pp_first_buy(conn, name, t) or today, now, [], label_key.get(name))
                            _pp_insert(conn, row)
                            changes.append(("opened", name, t, f"{v['qty']:g} sh @ {v['avg_cost']}", row["id"]))
                    if not suspect:
                        xh = xhold.get(name) or set()
                        held = [t for t in mine if t != "CASH" and t not in xh]
                        cand = sorted(t for t in held if t not in b["pos"] and t not in b["bad"])
                        fmass = False
                        if xh and cand:                   # stocks held for a pending move vanished as well: they still
                            allh = [t for t in mine if t != "CASH"]        # count for the mass-close rule, so the
                            allc = [t for t in allh if t not in b["pos"] and t not in b["bad"]]  # rest keep 2 looks
                            fmass = len(allc) >= 2 and len(allc) * 2 >= len(allh)
                        n_strikes = len(strikes)
                        to_close = _pp_strike(conn, name, b, mine, held, cand, out, strikes, sells, force_mass=fmass)
                        if xh:                            # a strike whose stocks are all pending moves: no 'reset' note
                            strikes[n_strikes:] = [s for s in strikes[n_strikes:]
                                                   if not (s[0] == "reset" and set(s[2]) <= xh)]
                        for t, fill in sorted(to_close.items()):
                            rs = mine[t]
                            px = fill["price"] if fill else _pp_close_px(qts.get(t))
                            if px is None:
                                out["errors"].append(f"{name} {t}: gone from Robinhood but no quote — close deferred")
                                continue
                            for r in rs:
                                pl = round((px - _f(r["cost_basis"], 0.0)) * _f(r["shares"], 0.0), 2)
                                extra_sql, args = "", [today, px, pl, now]
                                if fill:
                                    try:
                                        ex = json.loads(r.get("extra_json") or "{}")
                                    except (TypeError, ValueError):
                                        ex = {}
                                    ex = ex if isinstance(ex, dict) else {}
                                    ex["rh_close"] = {"via": "sell_fill", "order_ids": list(fill["order_ids"])}
                                    extra_sql = ", extra_json = ?"
                                    args.append(json.dumps(ex, default=str))
                                conn.execute(f"UPDATE {_PP_TABLE} SET status = 'closed', close_date = ?, "
                                             f"close_price = ?, realized_pl = ?, pending_action = NULL, "
                                             f"pending_close_price = NULL, pending_close_date = NULL, updated_at = ?"
                                             f"{extra_sql} WHERE id = ?", (*args, r["id"]))
                                why = ("sold at RH (fill)" if fill else
                                       "qty 0 at RH" if t in b["zero"] else "gone from RH")
                                changes.append(("closed", name, t, f"{r['shares']:g} sh @ {px} ({why})", r["id"]))
                            if xplan is not None:         # remembered so a later arrival can re-book it as a move
                                if fill:
                                    xplan["sold_now"].add((name, t))
                                else:
                                    xplan["quote_closed"][(name, t)] = [str(r["id"]) for r in rs]
                    if b["cash"] is not None:
                        rs = mine.get("CASH")
                        if rs:
                            if abs(_f(rs[0]["shares"], 0.0) - b["cash"]) > _PP_CASH_EPS:
                                conn.execute(f"UPDATE {_PP_TABLE} SET shares = ?, updated_at = ? WHERE id = ?",
                                             (b["cash"], now, rs[0]["id"]))
                                changes.append(("cash", name, "CASH", f"${rs[0]['shares']:,.2f} → ${b['cash']:,.2f}",
                                                rs[0]["id"]))
                        else:
                            row = _pp_row("CASH", name, b["cash"], 1.0, today, now, [], label_key.get(name))
                            _pp_insert(conn, row)
                            changes.append(("cash", name, "CASH", f"new ${b['cash']:,.2f}", row["id"]))
                if xplan is not None:
                    conn.execute("SAVEPOINT pp_moves")
                    try:
                        xres = _pp_xfer_finish(conn, xplan, book, today, now)
                        conn.execute("RELEASE pp_moves")
                    except Exception as e:
                        conn.execute("ROLLBACK TO pp_moves")
                        conn.execute("RELEASE pp_moves")
                        xplan = xres = None
                        _err("personal sync move booking", e)
                        out["errors"].append(f"move booking skipped on this look: {type(e).__name__}")
                    if xres:
                        changes += xres["changes"]
                        if xres.get("error"):
                            out["errors"].append(xres["error"])
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    finally:
        _pp_lock.release()
    for (old, new), n in sorted(moved.items()):
        _pp_log(f"Robinhood sync: relabelled {old} → {new} ({n} rows)",
                extra={"kind": "relabel", "from": old, "to": new, "rows": n})
    for kind, name, tickers in strikes:
        if kind == "no_sell_fill":
            _pp_log(f"Robinhood no longer shows {', '.join(tickers)} in {name} but no sale was found — "
                    f"left open; check the app.", tier="decision",
                    extra={"kind": "close_strike_no_sell_fill", "account": name, "tickers": tickers})
            continue
        _pp_log(f"Robinhood sync: mass close {kind} in {name}: {', '.join(tickers)}",
                extra={"kind": f"close_strike_{kind}", "account": name, "tickers": tickers})
    for kind, name, t, what, rid in changes:
        _pp_log(f"Robinhood sync: {kind} {t} in {name}: {what}", ticker=None if t == "CASH" else t,
                extra={"kind": kind, "account": name, "row_id": rid})
    if xplan is not None:
        for name in xplan.get("elig") or ():          # committed: these accounts were read cleanly on this look
            _pp_xfer_looks[name] = xplan["now"]
        out["transfers"] = _pp_xfer_log(xplan, xres or {}, xwait)
    out["changes"] = [{"kind": k, "account": n, "ticker": t, "what": w, "id": i} for k, n, t, w, i in changes]
    out["written"] = len(changes) + ((xres or {}).get("written") or 0)
    return out


def _pp_strike_s() -> float:
    """Second-strike wait: poll_seconds.positions, floored at 60 s (a 0/negative/garbage value can
    never shrink the window to nothing)."""
    import math
    try:
        v = _f(_cfg()["poll_seconds"].get("positions"))
    except Exception:
        v = None
    return max(60.0, v if (v is not None and math.isfinite(v)) else 300.0)


def _pp_strike_get(conn, name):
    r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_PP_STRIKE_PREFIX + name,)).fetchone()
    try:
        v = json.loads(r[0]) if r else None
    except (TypeError, ValueError):
        v = None
    return r is not None, (v if isinstance(v, dict) else None)


def _pp_sells(number, first_seen):
    """Filled RH SELL orders created since first_seen − 1 day → {T: {"qty", "price" (qty-weighted),
    "order_ids"}}; None when the read failed, errored or was truncated (then nothing may close)."""
    from datetime import timezone
    try:
        gte = datetime.fromtimestamp(float(first_seen) - 86400, timezone.utc).strftime("%Y-%m-%d")
        rows, meta = paged_all("get_equity_orders", {"account_number": number, "state": "filled",
                                                     "created_at_gte": gte},
                               max_pages=_PP_MAX_PAGES, lane="bulk", rows_key="orders")
        if meta.get("errors") or meta.get("truncated"):
            return None
        out = {}
        for o in rows:
            if str(o.get("side") or "").lower() != "sell" or str(o.get("state") or "").lower() != "filled":
                continue
            if str(o.get("created_at") or "")[:10] < gte:
                continue
            t = str(o.get("symbol") or "").strip().upper()
            qty = _f(o.get("cumulative_quantity"))
            qty = qty if qty is not None else _f(o.get("quantity"))
            px = _f(o.get("average_price"))
            if px is None:
                ex = [(_f(x.get("quantity")), _f(x.get("price"))) for x in o.get("executions") or []
                      if isinstance(x, dict)]
                ex = [(q, p) for q, p in ex if q and p]
                px = sum(q * p for q, p in ex) / sum(q for q, _ in ex) if ex else None
            if not t or not qty or qty <= 0 or px is None or px <= 0 or qty != qty or px != px:
                continue
            cur = out.setdefault(t, {"qty": 0.0, "notional": 0.0, "order_ids": []})
            cur["qty"] += qty
            cur["notional"] += qty * px
            cur["order_ids"].append(str(o.get("id") or ""))
        return {t: {"qty": v["qty"], "price": v["notional"] / v["qty"], "order_ids": v["order_ids"]}
                for t, v in out.items()}
    except Exception as e:
        _err("personal sync sell orders", e)
        return None


def _pp_prefetch_sells(book) -> dict:
    """Before the transaction: for each account whose mass-close strike is due, read its filled sells
    (one bulk-lane call per account). {name: sells | None}. Accounts not due are absent."""
    import mason_db
    out = {}
    try:
        now, wait = _pp_clock(), _pp_strike_s()
        conn = mason_db.get_connection()
        try:
            due = {}
            for name in book:
                _, st = _pp_strike_get(conn, name)
                first = _f((st or {}).get("first_seen"))
                if first is not None and now - first >= wait:
                    due[name] = first
        finally:
            conn.close()
        for name, first in due.items():
            out[name] = _pp_sells(book[name]["_number"], first)
    except Exception as e:
        _err("personal sync prefetch", e)
    return out


def _pp_strike(conn, name, b, mine, held, cand, out, strikes, sells, force_mass=False) -> dict:
    """Which of `cand` (open synced positions RH no longer reports) close now → {T: fill | None}.

    Mass close (D1 + Director DENY-1): RH answered an empty list, OR ≥2 would close and they are at
    least half of the account's open synced positions, OR the set is the one an existing strike is
    already holding. A mass close never happens on the first observation: a strike {tickers,
    first_seen} goes to desk_state. On a later tick ≥ _pp_strike_s() later that sees the SAME set gone,
    the account's FILLED SELL orders since first_seen − 1 day (prefetched, bulk lane) must cover each
    ticker's held qty; only those close, at the qty-weighted fill price. The rest stay open with
    reason 'no_sell_fill' and ONE decision-tier Activity entry per strike. An orders read that failed
    closes nothing. Anything else (a single position / a minority) clears a stale strike and closes
    immediately at the quote, as before. Writes only on a change (idempotent).
    force_mass (only with transfer detection on): `cand` vanished together with stocks held back for a pending move
    between Nico's accounts and the whole set is a mass close, so `cand` stays one; a strike already holding a larger
    set keeps its first look and its note."""
    key = _PP_STRIKE_PREFIX + name
    exists, prev = _pp_strike_get(conn, name)
    prev_t = sorted((prev or {}).get("tickers") or [])

    def put(val):
        if val != prev:
            conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) ON CONFLICT(key) "
                         "DO UPDATE SET value = excluded.value, updated_date = excluded.updated_date",
                         (key, json.dumps(val), _pp_now()))

    def drop():
        if exists:
            conn.execute("DELETE FROM desk_state WHERE key = ?", (key,))

    mass = bool(cand) and (force_mass or b["raw_n"] == 0 or (len(cand) >= 2 and len(cand) * 2 >= len(held))
                           or (prev is not None and prev_t == cand))
    if not mass:
        if exists:
            drop()
            strikes.append(("reset", name, prev_t))
        return {t: None for t in cand}
    now, wait = _pp_clock(), _pp_strike_s()
    same = prev is not None and (prev_t == cand or (force_mass and set(cand) <= set(prev_t)))
    first = _f((prev or {}).get("first_seen")) if same else None
    cur = dict(prev) if same and first is not None else None
    if cur is None:
        first = now
        cur = {"tickers": cand, "first_seen": now}
        put(cur)
        strikes.append(("recorded", name, cand))
    elif prev_t != cand:                      # force_mass: the strike narrows to the stocks still at stake
        cur["tickers"] = cand
        put(cur)

    def pending(tickers, reason=None):
        p = {"account": name, "tickers": list(tickers), "first_seen": first,
             "closes_after_s": round(max(0.0, first + wait - now), 1)}
        if reason:
            p["reason"] = reason
        out["pending_close"].append(p)

    if now - first < wait:
        pending(cand, cur.get("reason"))
        return {}
    if name not in sells:
        pending(cand, "awaiting_orders")
        return {}
    s = sells[name]
    if s is None:
        out["errors"].append(f"{name}: sell orders unavailable — mass close held")
        pending(cand, "orders_unavailable")
        return {}
    need = {t: sum(_f(r.get("shares"), 0.0) for r in mine.get(t) or []) for t in cand}
    ok = {t: s[t] for t in cand if t in s and s[t]["qty"] >= need[t] - _PP_QTY_EPS}
    left = [t for t in cand if t not in ok]
    if ok:
        strikes.append(("confirmed", name, sorted(ok)))
    if not left:
        drop()
        return ok
    val = {"tickers": left, "first_seen": first, "reason": "no_sell_fill", "notified": True}
    if not cur.get("notified"):
        strikes.append(("no_sell_fill", name, left))
    put(val)
    pending(left, "no_sell_fill")
    return ok


# ── moves between Nico's own synced accounts (transfer detection, 2026-11-18) ────────────────────────────────────────
# On 2026-11-03 two stocks were moved inside Robinhood from one synced account to another. The sync saw them vanish
# with no sale (mass-close rule -> no_sell_fill) and left the rows open, so they showed under both accounts (fixed by
# hand on 11-11). With robinhood.personal_positions_sync.transfers.enabled (default OFF = exactly the old behaviour):
#   * every look remembers, for window_hours, each DEPARTURE (open synced rows minus Robinhood's share count) and each
#     ARRIVAL (Robinhood's count minus the open synced rows) in desk_state[_PP_XFER_KEY] (labels, tickers, share
#     counts, times, row ids; never an account number);
#   * a departure and an arrival of the same stock in two different personal synced accounts, first seen within
#     window_hours of each other, with share counts within max(0.01, qty_tolerance_pct %), each the other's closest
#     match in time (a tie = ambiguous = nothing), become a PENDING move. While pending, the sending rows stay out of
#     the mass-close / market-price close;
#   * a pending move is BOOKED on a later look at least _pp_strike_s() after it was formed (two looks, like the
#     mass-close rule), only if both accounts were read cleanly (the sender's answer not a suspect empty one), the
#     shares are still gone from the sender and still in the receiver, and both order histories were read cleanly
#     (by when each order last CHANGED, in any state, so an order placed long before it filled — good-till-cancelled,
#     a stop — and a partly filled one count): no sale of that stock in the sender back to window_hours before the
#     departure, and no buy of it in the receiver since its previous remembered arrival of that stock (at most
#     window_hours back). A sale on record means it is NOT a move, and the old rules handle the rows exactly as
#     without this switch (the move logic never books a sale itself: a sale days old may have sold earlier shares);
#     a buy on record means the arrival was a purchase and the old rules take over. A failed / rate-limited /
#     truncated read waits; a pending move older than window_hours is dropped and the old rules take over;
#   * an event first seen on an account's first clean look after a blind spell longer than window_hours (Mason down,
#     logged out, the account unreadable), or on its first look since this process started / the switch was turned
#     on, never pairs: the order look-back could miss a sale or a buy made during the blind spell (_pp_xfer_looks);
#   * booking: the sending rows close at cost with no profit or loss as of the day the departure was first seen
#     (rh_close.via = "transfer"; a row the old rule had already closed at the market price is re-booked at cost and
#     keeps the old figures under rh_close.was); a partial move is only noted on the sender (rh_sync.transfers_out).
#     The receiving row takes the moved shares' first-buy date (the earlier one when it already held the stock) and,
#     while Robinhood reports no cost (blank or 0), the sender's cost (blended into an existing position);
#     rh_sync.transfers_in records where the shares came from and the row's previous date and cost;
#   * stocks held back for a pending move still count for the mass-close rule (force_mass), so the other stocks that
#     vanished with them keep its two looks and its sale check.
# The family account never takes part: an account on personal_scope.exclude_accounts (in last-4 map mode also every
# non-personal account, which is never synced anyway) is neither a sender nor a receiver, and anything remembered
# about it from before it was excluded is forgotten (its order history is never read).
_PP_XFER_KEY = "rh_personal_transfer_watch"
_PP_XFER_MIN_SH = 0.01           # share tolerance floor (a fractional crumb on a small position)
_PP_XFER_EPS = 1e-6              # the smallest share change that counts as a departure or an arrival
_PP_XFER_MAX = 200               # cap per remembered list (oldest dropped first)
_PP_XFER_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_pp_xfer_looks = {}              # account label -> _pp_clock() of its last committed clean look (this process only)


def _pp_xfer_isnum(x) -> bool:
    import math
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pp_xfer_cfg() -> dict:
    """robinhood.personal_positions_sync.transfers -> {"enabled", "window_s", "tol_pct"}. OFF unless it is an object
    whose "enabled" is exactly true. window_hours (1-720, default 96) and qty_tolerance_pct (0-10, default 2) fall back
    to their defaults when missing or unusable. Never raises."""
    out = {"enabled": False, "window_s": 96 * 3600.0, "tol_pct": 2.0}
    try:
        node = (_cfg().get("personal_positions_sync") or {}).get("transfers")
        if not isinstance(node, dict):
            return out
        w, t = node.get("window_hours"), node.get("qty_tolerance_pct")
        if _pp_xfer_isnum(w) and 1.0 <= w <= 720.0:
            out["window_s"] = float(w) * 3600.0
        if _pp_xfer_isnum(t) and 0.0 <= t <= 10.0:
            out["tol_pct"] = float(t)
        out["enabled"] = node.get("enabled") is True
    except Exception:
        out["enabled"] = False
    return out


def _pp_xfer_day(ts) -> str:
    """ET calendar date of a _pp_clock() time."""
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York")).date().isoformat()


def _pp_xfer_when(ts) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York")).isoformat(timespec="seconds")


def _pp_xfer_entry(rs):
    """Earliest entry_date (YYYY-MM-DD) among rows, or None."""
    ds = sorted(str(r.get("entry_date") or "")[:10] for r in rs or []
                if _PP_XFER_DATE_RE.fullmatch(str(r.get("entry_date") or "")[:10]))
    return ds[0] if ds else None


def _pp_xfer_cost(rs):
    """Share-weighted cost_basis of rows, or None when they hold no shares."""
    q = sum(max(0.0, _f(r.get("shares"), 0.0)) for r in rs or [])
    if q <= 0:
        return None
    return sum(max(0.0, _f(r.get("shares"), 0.0)) * _f(r.get("cost_basis"), 0.0) for r in rs) / q


def _pp_xfer_valid(e, kind) -> bool:
    """Shape check of one remembered entry; anything else in the stored state is dropped, never trusted."""
    try:
        if kind == "pend":
            return (isinstance(e, dict) and _pp_xfer_isnum(e.get("since")) and _pp_xfer_valid(e.get("dep"), "dep")
                    and _pp_xfer_valid(e.get("arr"), "arr"))
        if not (isinstance(e, dict) and isinstance(e.get("account"), str) and e["account"].strip()
                and isinstance(e.get("ticker"), str) and e["ticker"].strip()
                and all(_pp_xfer_isnum(e.get(k)) for k in ("qty", "before", "after", "seen")) and e["qty"] > 0
                and isinstance(e.get("ok", False), bool)):
            return False
        if kind == "dep":
            return (isinstance(e.get("full"), bool) and isinstance(e.get("rows"), list)
                    and all(isinstance(x, str) for x in e["rows"]) and isinstance(e.get("quote_closed", []), list)
                    and all(isinstance(x, str) for x in e.get("quote_closed", [])))
        return isinstance(e.get("new"), bool)
    except Exception:
        return False


def _pp_xfer_read(conn) -> tuple:
    """(state {"deps", "arrs", "pend"} holding only well-formed entries, the stored text or None)."""
    r = conn.execute("SELECT value FROM desk_state WHERE key = ?", (_PP_XFER_KEY,)).fetchone()
    raw = r[0] if r else None
    try:
        v = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        v = None
    w = {"deps": [], "arrs": [], "pend": []}
    if isinstance(v, dict):
        for k, kind in (("deps", "dep"), ("arrs", "arr"), ("pend", "pend")):
            if isinstance(v.get(k), list):
                w[k] = [e for e in v[k] if _pp_xfer_valid(e, kind)]
    return w, (raw if r else None)


def _pp_xfer_write(conn, w, raw) -> bool:
    """Store the state when it changed; the key is removed when nothing is remembered. -> written?"""
    for k in ("deps", "arrs", "pend"):
        w[k] = sorted(w[k], key=lambda e: float(e.get("seen", e.get("since", 0.0))))[-_PP_XFER_MAX:]
    if not any(w.values()):
        if raw is None:
            return False
        conn.execute("DELETE FROM desk_state WHERE key = ?", (_PP_XFER_KEY,))
        return True
    txt = json.dumps({"v": 1, "deps": w["deps"], "arrs": w["arrs"], "pend": w["pend"]}, sort_keys=True, default=str)
    if txt == raw:
        return False
    conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value, updated_date = excluded.updated_date", (_PP_XFER_KEY, txt, _pp_now()))
    return True


def _pp_xfer_note(ex, key, item) -> dict:
    """Append `item` to extra_json["rh_sync"][key] (a list), keeping whatever else the row carries."""
    rs = ex.get("rh_sync")
    rs = rs if isinstance(rs, dict) else ({} if rs is None else {"_prior": rs})
    rs[key] = (rs.get(key) if isinstance(rs.get(key), list) else []) + [item]
    ex["rh_sync"] = rs
    return ex


def _pp_xfer_extra(r) -> dict:
    try:
        ex = json.loads(r.get("extra_json") or "{}")
    except (TypeError, ValueError):
        ex = {}
    return ex if isinstance(ex, dict) else {"_prior_extra": ex}


def _pp_xfer_moved_in(r) -> bool:
    """The row received shares moved in from another of Nico's accounts (rh_sync.transfers_in)."""
    try:
        rs = _pp_xfer_extra(r).get("rh_sync")
        return isinstance(rs, dict) and isinstance(rs.get("transfers_in"), list) and bool(rs["transfers_in"])
    except Exception:
        return False


def _pp_xfer_events(have, book, names, now) -> tuple:
    """This look's departures and arrivals in the accounts `names`: the open synced rows (the last synced state)
    against Robinhood's fresh answer. CASH, tickers with an unusable quantity and tickers with more than one open
    synced row (only the first is kept current, so the sum would show a change on every look) are skipped.
    -> ({(account, T): departure}, {(account, T): arrival})"""
    deps, arrs = {}, {}
    for name in names:
        b = book[name]
        mine = {t: rs for (a, t), rs in have.items() if a == name and t != "CASH"}
        for t, rs in sorted(mine.items()):
            if t in b["bad"] or len(rs) > 1:
                continue
            before = sum(_f(r.get("shares"), 0.0) for r in rs)
            after = b["pos"][t]["qty"] if t in b["pos"] else 0.0
            if before - after > _PP_XFER_EPS:
                deps[(name, t)] = {"account": name, "ticker": t, "qty": before - after, "before": before,
                                   "after": after, "seen": now, "full": t not in b["pos"],
                                   "rows": [str(r["id"]) for r in rs], "entry": _pp_xfer_entry(rs),
                                   "cost": _pp_xfer_cost(rs), "quote_closed": []}
        for t, v in sorted(b["pos"].items()):
            rs = mine.get(t) or []
            if t == "CASH" or len(rs) > 1:
                continue
            before = sum(_f(r.get("shares"), 0.0) for r in rs)
            if v["qty"] - before > _PP_XFER_EPS:
                arrs[(name, t)] = {"account": name, "ticker": t, "qty": v["qty"] - before, "before": before,
                                   "after": v["qty"], "seen": now, "new": not rs, "prev_cost": _pp_xfer_cost(rs),
                                   "prev_entry": _pp_xfer_entry(rs)}
    return deps, arrs


def _pp_xfer_plan(w, deps_now, arrs_now, book, elig, xc, now, wait, fills) -> dict:
    """One look of the move rule over the remembered state `w` (updated in place; no I/O). `fills` = the prefetched
    {"orders": {account: _pp_xfer_orders() | None}}.
    -> {"hold": {account: {T}}, "book": [pending], "notes": [(kind, pending)], "errors": [str]}. hold = the sending
    tickers kept out of the mass-close / market-price close on this look."""
    W, tp = xc["window_s"], xc["tol_pct"]
    orders = fills.get("orders") or {}
    plan = {"hold": {}, "book": [], "notes": [], "errors": []}

    def tol(a, b):
        return max(_PP_XFER_MIN_SH, tp / 100.0 * max(abs(a), abs(b)))

    def cur(acct, t):                    # Robinhood's count on this look; None = the account was not read cleanly
        if acct not in elig:
            return None
        p = book[acct]["pos"].get(t)
        return p["qty"] if p else 0.0

    def gone(d):                         # the shares are still out of the sender (None = cannot tell on this look)
        c = cur(d["account"], d["ticker"])
        return None if c is None else c <= d["after"] + tol(d["qty"], d["qty"])

    def here(a):                         # the shares are still in the receiver
        c = cur(a["account"], a["ticker"])
        return None if c is None else c >= a["after"] - tol(a["qty"], a["qty"])

    def hold(d):
        if d["full"]:
            plan["hold"].setdefault(d["account"], set()).add(d["ticker"])

    keep = []                            # 1. nothing older than the window is trusted
    for p in w["pend"]:
        if now - p["since"] > W:
            plan["notes"].append(("expired", p))
        else:
            keep.append(p)
    w["pend"] = keep
    w["deps"] = [d for d in w["deps"] if now - d["seen"] <= W]
    w["arrs"] = [a for a in w["arrs"] if now - a["seen"] <= W]
    keep = []                            # 2. pending moves: still true? due? a sale on record?
    for p in w["pend"]:
        d, a = p["dep"], p["arr"]
        g, h = gone(d), here(a)
        if g is False:
            plan["notes"].append(("came_back", p))
            w["arrs"].append(a)
            continue
        if h is False:
            plan["notes"].append(("left", p))
            w["deps"].append(d)
            continue
        src, dst, t = d["account"], a["account"], d["ticker"]
        if g is None or h is None or now - p["since"] < wait or src not in orders or dst not in orders:
            keep.append(p)               # not readable on this look, too early, or the orders were not read: wait
            hold(d)
            continue
        if orders[src] is None or orders[dst] is None:
            plan["errors"].append(f"{src if orders[src] is None else dst}: orders unavailable — move of {t} held")
            keep.append(p)
            hold(d)
            continue
        sold = (orders[src]["sell"].get(t) or []) + (orders[src]["sell"].get("?") or [])
        if any(ts is None or ts >= d["seen"] - W for ts in sold):
            plan["notes"].append(("sale", p))
            continue                     # a sale on record in the sender: not a move; the old rules handle its rows
        low = max([a["seen"] - W] + [e["seen"] for e in w["arrs"]
                                     if e["account"] == dst and e["ticker"] == t and e["seen"] < a["seen"]])
        bought = (orders[dst]["buy"].get(t) or []) + (orders[dst]["buy"].get("?") or [])
        if any(ts is None or ts > low for ts in bought):
            plan["notes"].append(("bought", p))
            continue                     # a buy on record in the receiver explains the arrival: not a move
        plan["book"].append(p)
        hold(d)
    w["pend"] = keep
    owned = {(p["dep"]["account"], p["dep"]["ticker"]) for p in w["pend"] + plan["book"]}
    rec = {}                             # 3. departures: keep each one's first look
    for d in w["deps"]:
        k = (d["account"], d["ticker"])
        if k not in owned and k not in rec:
            rec[k] = d
    for k, d in deps_now.items():
        if k in owned:
            continue
        old = rec.get(k)
        if old is None or abs(old["qty"] - d["qty"]) > tol(old["qty"], d["qty"]):
            rec[k] = d                   # new, or a different amount: start over from this look
    w["deps"] = [d for d in rec.values() if gone(d) is not False]
    w["arrs"] = [a for a in w["arrs"] + list(arrs_now.values()) if here(a) is not False]
    claims = {}                          # 4. new pairs: each departure's closest-in-time matching arrival, and each
    for i, d in enumerate(w["deps"]):    #    arrival's closest departure; a tie at the closest = ambiguous = no pair.
        if gone(d) is not True or d.get("ok") is not True:     # (an event seen after a blind spell never pairs)
            continue
        js = [(abs(a["seen"] - d["seen"]), j) for j, a in enumerate(w["arrs"])
              if a["ticker"] == d["ticker"] and a["account"] != d["account"] and here(a) is True
              and a.get("ok") is True
              and abs(a["seen"] - d["seen"]) <= W and abs(a["qty"] - d["qty"]) <= tol(a["qty"], d["qty"])]
        js.sort()
        if js and (len(js) == 1 or js[0][0] < js[1][0]):
            claims.setdefault(js[0][1], []).append((js[0][0], i))
    used_d, used_a = set(), set()
    for j, cl in sorted(claims.items()):
        cl.sort()
        if len(cl) > 1 and cl[0][0] == cl[1][0]:
            continue
        i = cl[0][1]
        p = {"since": now, "dep": w["deps"][i], "arr": w["arrs"][j]}
        w["pend"].append(p)
        used_d.add(i)
        used_a.add(j)
        hold(p["dep"])
        plan["notes"].append(("pending", p))
    w["deps"] = [d for i, d in enumerate(w["deps"]) if i not in used_d]
    w["arrs"] = [a for j, a in enumerate(w["arrs"]) if j not in used_a]
    return plan


def _pp_xfer_orders(number, since_ts):
    """The account's orders that CHANGED since the UTC day of `since_ts` (a _pp_clock time), read by their last change
    (updated_at_gte) in any state, so an order placed long before it filled (good-till-cancelled, a stop) and a partly
    filled one are seen. -> {"sell": {T: [time]}, "buy": {T: [time]}} for every order with shares executed (time = the
    latest of its timestamps; None = unreadable, so it counts as recent; T "?" = symbol unreadable, so it counts for
    every stock); None when the read failed, errored, was truncated, or a page carried no "orders" list (strict:
    an empty or reshaped answer is never read as "nothing on record")."""
    from datetime import timezone
    try:
        gte = datetime.fromtimestamp(float(since_ts), timezone.utc).strftime("%Y-%m-%d")
        rows, meta = paged_all("get_equity_orders", {"account_number": number, "updated_at_gte": gte},
                               max_pages=_PP_MAX_PAGES, lane="bulk", rows_key="orders", strict_rows=True)
        if meta.get("errors") or meta.get("truncated"):
            return None
        out = {"sell": {}, "buy": {}}
        for o in rows:
            side = str(o.get("side") or "").strip().lower()
            if side not in out:
                continue
            ex = [x for x in o.get("executions") or [] if isinstance(x, dict)]
            q = _f(o.get("cumulative_quantity"))
            if (q is None or q != q) and ex:
                q = sum(_f(x.get("quantity"), 0.0) for x in ex)
            if q is not None and q != q:
                q = None
            if not ((q is not None and q > 0) or (q is None and str(o.get("state") or "").lower() == "filled")):
                continue                 # nothing executed: cancelled / rejected / still open with no fill
            t = str(o.get("symbol") or "").strip().upper() or "?"
            ts = []
            for s in [o.get("last_transaction_at"), o.get("updated_at"), o.get("created_at")] + \
                     [x.get("timestamp") for x in ex]:
                try:
                    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                    ts.append((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
                except (TypeError, ValueError):
                    pass
            out[side].setdefault(t, []).append(max(ts) if ts else None)
        return out
    except Exception as e:
        _err("personal sync move orders", e)
        return None


def _pp_xfer_prefetch(book, xc, now, wait, excl=frozenset()) -> dict:
    """Before the transaction: for each move due for its second look, read the orders of both accounts (once per
    account, bulk lane, _pp_xfer_orders) back to window_hours before the earlier of its departure / arrival. An
    excluded (family) account is never read. -> {"orders": {account: orders | None}}"""
    import mason_db
    out = {"orders": {}}
    try:
        conn = mason_db.get_connection()
        try:
            w, _raw = _pp_xfer_read(conn)
        finally:
            conn.close()
        due = {}
        for p in w["pend"]:
            a, b = p["dep"]["account"], p["arr"]["account"]
            if (a in book and b in book and wait <= now - p["since"] <= xc["window_s"]
                    and norm_account_label(a) not in excl and norm_account_label(b) not in excl):
                for acct, seen in ((a, p["dep"]["seen"]), (b, p["arr"]["seen"])):
                    due[acct] = min(due.get(acct, seen), seen)
        for acct, first in sorted(due.items()):
            out["orders"][acct] = _pp_xfer_orders(book[acct]["_number"], first - xc["window_s"])
    except Exception as e:
        _err("personal sync move check", e)
    return out


def _pp_xfer_start(conn, have, book, accts, excl, xc, now, wait, fills) -> dict:
    """Inside the reconcile transaction, before the per-account loop: read the state (forgetting anything about an
    account that is excluded now), take this look's departures and arrivals in the eligible accounts (read cleanly,
    not a suspect empty answer, not excluded; "ok" = its previous clean look in this process was at most window_hours
    ago, else the event never pairs) and decide."""
    w, raw = _pp_xfer_read(conn)

    def off(lab):                        # excluded now (the family account): whatever was remembered is forgotten
        return norm_account_label(lab) in excl
    w["deps"] = [d for d in w["deps"] if not off(d["account"])]
    w["arrs"] = [a for a in w["arrs"] if not off(a["account"])]
    w["pend"] = [p for p in w["pend"] if not (off(p["dep"]["account"]) or off(p["arr"]["account"]))]
    elig = set()
    for name in accts:
        b, lab = book.get(name), norm_account_label(name)
        if b is None or not lab or lab in excl:
            continue
        if b["raw_n"] == 0 and any(t != "CASH" for (a, t) in have if a == name) and b["equity_value"] != 0.0:
            continue                     # a suspect empty answer is never evidence of a move
        elig.add(name)
    deps, arrs = _pp_xfer_events(have, book, sorted(elig), now)
    for e in list(deps.values()) + list(arrs.values()):
        prev = _pp_xfer_looks.get(e["account"])          # its account's previous clean look (this process)
        e["ok"] = prev is not None and 0.0 <= now - prev <= xc["window_s"]
    plan = _pp_xfer_plan(w, deps, arrs, book, elig, xc, now, wait, fills)
    plan.update(w=w, raw=raw, quote_closed={}, sold_now=set(), now=now, elig=sorted(elig))
    return plan


def _pp_xfer_book(conn, plan, book, now, res) -> None:
    """Write the confirmed moves (inside the caller's savepoint)."""
    def rows(ids, status):
        ids = [str(i) for i in ids or []][:200]
        if not ids:
            return []
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM {_PP_TABLE} WHERE id IN ({', '.join('?' * len(ids))}) AND is_deleted = 0 AND "
            f"created_by = ? AND status = ?", (*ids, _PP_SYNC_BY, status))]

    def open_rows(acct, t):
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM {_PP_TABLE} WHERE is_deleted = 0 AND status = 'open' AND created_by = ? AND "
            f"account_label = ? AND upper(ticker) = ? ORDER BY created_at, id", (_PP_SYNC_BY, acct, t))]
    for p in plan["book"]:
        d, a = p["dep"], p["arr"]
        src, dst, t = d["account"], a["account"], d["ticker"]
        info = {"to_account": dst, "first_seen": _pp_xfer_when(d["seen"]), "booked_at": now}
        touched, kind = [], "partial"
        if d["full"]:
            day = _pp_xfer_day(d["seen"])
            mine = [r for r in rows(d["rows"], "open") if r["account_label"] == src
                    and (r["ticker"] or "").upper() == t]
            earlier = [r for r in rows(d.get("quote_closed"), "closed") if r["account_label"] == src
                       and (r["ticker"] or "").upper() == t and not _pp_xfer_extra(r).get("rh_close")]
            for r in mine:
                ex = _pp_xfer_extra(r)
                ex["rh_close"] = dict(info, via="transfer", shares=_f(r["shares"], 0.0))
                conn.execute(f"UPDATE {_PP_TABLE} SET status = 'closed', close_date = ?, close_price = cost_basis, "
                             f"realized_pl = 0.0, pending_action = NULL, pending_close_price = NULL, "
                             f"pending_close_date = NULL, updated_at = ?, extra_json = ? WHERE id = ? AND "
                             f"status = 'open'", (day, now, json.dumps(ex, default=str), r["id"]))
                touched.append(r["id"])
            for r in earlier:
                ex = _pp_xfer_extra(r)
                ex["rh_close"] = dict(info, via="transfer", shares=_f(r["shares"], 0.0),
                                      was={"close_price": r["close_price"], "realized_pl": r["realized_pl"]})
                conn.execute(f"UPDATE {_PP_TABLE} SET close_price = cost_basis, realized_pl = 0.0, updated_at = ?, "
                             f"extra_json = ? WHERE id = ? AND status = 'closed'",
                             (now, json.dumps(ex, default=str), r["id"]))
                touched.append(r["id"])
            if not touched:
                res["dropped"].append(("rows_changed", p))
                continue
            kind = "full" if mine else "rebooked"
        else:
            rs = open_rows(src, t)
            if rs:
                ex = _pp_xfer_note(_pp_xfer_extra(rs[0]), "transfers_out", dict(info, shares=round(d["qty"], 6)))
                conn.execute(f"UPDATE {_PP_TABLE} SET extra_json = ?, updated_at = ? WHERE id = ?",
                             (json.dumps(ex, default=str), now, rs[0]["id"]))
                touched.append(rs[0]["id"])
        to_row, entry, rs = None, None, open_rows(dst, t)
        if rs:
            r = rs[0]
            old_entry, old_cost = str(r.get("entry_date") or "")[:10], _f(r["cost_basis"], 0.0)
            first = d.get("entry") if isinstance(d.get("entry"), str) and _PP_XFER_DATE_RE.fullmatch(d["entry"]) \
                else None
            entry = old_entry
            if first and (a["new"] or not _PP_XFER_DATE_RE.fullmatch(old_entry) or first < old_entry):
                entry = first
            cost, dc = old_cost, (d.get("cost") if _pp_xfer_isnum(d.get("cost")) else None)
            rh_avg = ((book.get(dst) or {}).get("pos", {}).get(t) or {}).get("avg_cost")
            if (rh_avg is None or rh_avg <= 0) and dc is not None and dc > 0:
                if a["new"]:
                    cost = dc
                elif _pp_xfer_isnum(a.get("prev_cost")) and a["before"] + a["qty"] > 0:
                    cost = (a["before"] * a["prev_cost"] + a["qty"] * dc) / (a["before"] + a["qty"])
            ex = _pp_xfer_note(_pp_xfer_extra(r), "transfers_in", {
                "from_account": src, "shares": round(a["qty"], 6), "entry_date": first, "cost_basis": dc,
                "from_rows": list(d["rows"]), "prior_entry_date": old_entry or None, "prior_cost_basis": old_cost,
                "first_seen": info["first_seen"], "booked_at": now})
            conn.execute(f"UPDATE {_PP_TABLE} SET entry_date = ?, cost_basis = ?, extra_json = ?, updated_at = ? "
                         f"WHERE id = ?", (entry or r["entry_date"], cost, json.dumps(ex, default=str), now, r["id"]))
            touched.append(r["id"])
            to_row = r["id"]
        res["written"] += len(touched)
        res["booked"].append({"from": src, "to": dst, "ticker": t, "shares": round(d["qty"], 6), "kind": kind,
                              "from_rows": list(d["rows"]), "to_row": to_row, "entry_date": entry,
                              "first_seen": info["first_seen"]})


def _pp_xfer_finish(conn, plan, book, today, now) -> dict:
    """Inside the reconcile transaction, after the per-account loop (the receiving rows are current): note the rows
    the old rule closed on this look, book (own savepoint: a failure books nothing and the moves stay pending), store
    the state. -> {"booked", "changes", "written", "dropped", "error"}"""
    w = plan["w"]
    for (a, t), ids in plan["quote_closed"].items():
        for d in w["deps"]:
            if d["account"] == a and d["ticker"] == t:
                d["quote_closed"] = sorted(set(d.get("quote_closed") or []) | set(ids))
    if plan["sold_now"]:
        w["deps"] = [d for d in w["deps"] if (d["account"], d["ticker"]) not in plan["sold_now"]]
    res = {"booked": [], "changes": [], "written": 0, "dropped": [], "error": None}
    conn.execute("SAVEPOINT pp_move_book")
    try:
        _pp_xfer_book(conn, plan, book, now, res)
        conn.execute("RELEASE pp_move_book")
    except Exception as e:
        conn.execute("ROLLBACK TO pp_move_book")
        conn.execute("RELEASE pp_move_book")
        _err("personal sync move booking", e)
        res = {"booked": [], "changes": [], "written": 0, "dropped": [],
               "error": f"move booking held on this look: {type(e).__name__}"}
        w["pend"] += list(plan["book"])
        plan["book"] = []
    for kind, p in res["dropped"]:
        plan["notes"].append((kind, p))
    _pp_xfer_write(conn, w, plan["raw"])
    return res


def _pp_xfer_log(plan, res, wait) -> dict:
    """Plain Activity notes (work tier) for this look's moves, and the result block of the reconcile. Never raises."""
    for kind, p in plan["notes"]:
        try:
            t, src, dst, q = p["dep"]["ticker"], p["dep"]["account"], p["arr"]["account"], p["dep"]["qty"]
            title = {
                "pending": f"Robinhood sync: {t} looks like a move from {src} to {dst} ({q:g} sh), not a sale. "
                           f"Mason checks once more before booking it.",
                "sale": f"Robinhood sync: {t} left {src} with a sale on record, so it is not a move to {dst}.",
                "bought": f"Robinhood sync: {t} showed up in {dst} through a buy on record, so it is not a move "
                          f"from {src}.",
                "expired": f"Robinhood sync: Mason could not confirm in time that {t} moved from {src} to {dst}; "
                           f"it is handled the usual way."}.get(kind) or \
                f"Robinhood sync: {t} no longer looks like a move from {src} to {dst}; nothing was booked."
            _pp_log(title, ticker=t, extra={"kind": f"transfer_{kind}", "from": src, "to": dst, "ticker": t,
                                            "shares": round(q, 6)})
        except Exception as e:
            _err("personal sync move note", e)
    for b in res.get("booked") or []:
        try:
            t, src, dst, q, e = b["ticker"], b["from"], b["to"], b["shares"], b.get("entry_date")
            keeps = f", and the {dst} row keeps the first buy date {e}" if e else ""
            if b["kind"] == "partial":
                title = (f"Robinhood sync: {q:g} sh of {t} moved from {src} to {dst}. Booked as a move, not a sale"
                         + (f"; the {dst} row keeps the first buy date {e}." if e else "."))
            elif b["kind"] == "rebooked":
                title = (f"Robinhood sync: {t} moved from {src} to {dst} ({q:g} sh). Booked as a move, not a sale: "
                         f"the {src} row, closed earlier at the market price, is now closed at cost with no profit "
                         f"or loss{keeps}.")
            else:
                title = (f"Robinhood sync: {t} moved from {src} to {dst} ({q:g} sh). Booked as a move, not a sale: "
                         f"the {src} row is closed at cost with no profit or loss{keeps}.")
            _pp_log(title, ticker=t, extra={"kind": "transfer_booked", "from": src, "to": dst, "ticker": t,
                                            "shares": q, "how": b["kind"], "rows": b["from_rows"],
                                            "row_id": b["to_row"]})
        except Exception as e:
            _err("personal sync move note", e)
    now = plan["now"]
    return {"pending": [{"from": p["dep"]["account"], "to": p["arr"]["account"], "ticker": p["dep"]["ticker"],
                         "shares": round(p["dep"]["qty"], 6), "since": _pp_xfer_when(p["since"]),
                         "second_look_in_s": round(max(0.0, p["since"] + wait - now), 1)} for p in plan["w"]["pend"]],
            "booked": list(res.get("booked") or []),
            "dropped": [{"from": p["dep"]["account"], "to": p["arr"]["account"], "ticker": p["dep"]["ticker"],
                         "why": kind} for kind, p in plan["notes"] if kind != "pending"]}


def _pp_xfer_history(conn) -> list:
    """Moves on record (synced rows closed with rh_close.via = transfer, by hand or by the sync) and their receiving
    rows. state "fix" = the receiving row is dated later than the moved shares' first buy."""
    out = []
    for r in conn.execute(f"SELECT * FROM {_PP_TABLE} WHERE is_deleted = 0 AND created_by = ? AND status = 'closed' "
                          f"ORDER BY close_date, id", (_PP_SYNC_BY,)):
        r = dict(r)
        rc = _pp_xfer_extra(r).get("rh_close")
        if not isinstance(rc, dict) or rc.get("via") != "transfer":
            continue
        t, to = (r.get("ticker") or "").upper(), rc.get("to_account")
        it = {"ticker": t, "from": r["account_label"], "to": to if isinstance(to, str) and to else None,
              "row": r["id"], "closed": r.get("close_date"), "first_buy": _pp_xfer_entry([r]), "recv": None,
              "recv_entry": None, "state": "no receiving account recorded"}
        if it["to"]:
            recv = [dict(x) for x in conn.execute(
                f"SELECT * FROM {_PP_TABLE} WHERE is_deleted = 0 AND status = 'open' AND created_by = ? AND "
                f"account_label = ? AND upper(ticker) = ? ORDER BY created_at, id", (_PP_SYNC_BY, it["to"], t))]
            if not recv:
                it["state"] = "the receiving account holds no open row of it now"
            elif len(recv) > 1:
                it["state"] = "several open rows in the receiving account; left alone"
            else:
                xe = str(recv[0].get("entry_date") or "")[:10]
                it.update(recv=recv[0]["id"], recv_entry=xe)
                it["state"] = "fix" if (it["first_buy"] and _PP_XFER_DATE_RE.fullmatch(xe)
                                        and xe > it["first_buy"]) else "ok"
        out.append(it)
    return out


def _pp_xfer_fix_history(db_path, only=None) -> list:
    """`transfers --fix-history --confirm`: in ONE transaction (the personal-sync lock + BEGIN IMMEDIATE) re-read the
    moves on record and give each receiving row that is dated later than the moved shares' first buy that date (only
    the receiving row ids in `only`, when given); the old date goes into rh_sync.transfers_in. Nothing else is
    touched. -> the fixed history items."""
    import sqlite3
    import mason_db
    if not _pp_lock.acquire(timeout=_PP_LOCK_TIMEOUT_S):
        raise RuntimeError("another personal sync is running")
    try:
        if db_path is None:
            conn = mason_db.get_connection()
        else:
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            _pp_begin(conn)
            try:
                now, done = _pp_now(), []
                for h in _pp_xfer_history(conn):
                    if h["state"] != "fix" or (only and h["recv"] not in only):
                        continue
                    r = dict(conn.execute(f"SELECT * FROM {_PP_TABLE} WHERE id = ?", (h["recv"],)).fetchone())
                    ex = _pp_xfer_note(_pp_xfer_extra(r), "transfers_in", {
                        "from_account": h["from"], "from_rows": [h["row"]], "entry_date": h["first_buy"],
                        "prior_entry_date": h["recv_entry"], "booked_at": now, "via": "history_fix"})
                    conn.execute(f"UPDATE {_PP_TABLE} SET entry_date = ?, extra_json = ?, updated_at = ? WHERE id = ? "
                                 f"AND status = 'open' AND is_deleted = 0",
                                 (h["first_buy"], json.dumps(ex, default=str), now, h["recv"]))
                    done.append(h)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    finally:
        _pp_lock.release()
    return done


def _pp_xfer_report(args) -> int:
    """`python3 mason_robinhood.py transfers [--db FILE] [--fix-history [--row ID]... [--confirm]]`: a READ-ONLY report
    of the move rule on the stored tables (no Robinhood call): the switch, the moves waiting for their second look,
    what is remembered, the accounts held by the mass-close rule, and the moves on record whose receiving row still
    carries the move day as its first-buy date. --fix-history lists those date fixes (only the receiving rows named
    with --row, when given); with --confirm it writes them. Prints labels (account masks removed), tickers, share
    counts, dates and row ids only."""
    import sqlite3
    import mason_db
    db, fix, confirm, only, bad, it = None, False, False, [], [], iter(args)
    for x in it:
        if x == "--db":
            db = next(it, None)
            if not db or db.startswith("--"):
                bad.append("--db needs a file path")
        elif x == "--fix-history":
            fix = True
        elif x == "--confirm":
            confirm = True
        elif x == "--row":
            v = next(it, None)
            if not v or v.startswith("--"):
                bad.append("--row needs a receiving row id")
            else:
                only.append(v.strip())
        else:
            bad.append(f"unknown option {x[:40]!r}")
    if bad or ((confirm or only) and not fix):
        print("usage: python3 mason_robinhood.py transfers [--db FILE] [--fix-history [--row ID]... [--confirm]]")
        for b in bad or ["--confirm and --row only go with --fix-history"]:
            print("  " + strip_masks(b))
        return 2
    path = Path(db).expanduser() if db else Path(mason_db.DB_FILE)
    if not path.is_file():
        print(f"no database file at {path}")
        return 2
    xc = _pp_xfer_cfg()
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            w, _raw = _pp_xfer_read(conn)
            strikes = []
            for r in conn.execute("SELECT key, value FROM desk_state WHERE key LIKE ? ORDER BY key",
                                  (_PP_STRIKE_PREFIX + "%",)):
                try:
                    v = json.loads(r[1] or "null")
                except (TypeError, ValueError):
                    v = None
                v = v if isinstance(v, dict) else {}
                tk = ", ".join(str(x) for x in (v.get("tickers") or []) if isinstance(x, str)) or "?"
                strikes.append(f"{r[0][len(_PP_STRIKE_PREFIX):]} ({tk}"
                               + ("; no sale was found" if v.get("reason") == "no_sell_fill" else "") + ")")
            hist = _pp_xfer_history(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"could not read the database ({type(e).__name__})")
        return 1
    fixes = [h for h in hist if h["state"] == "fix"]
    if only:
        unknown = sorted(set(only) - {h["recv"] for h in fixes})
        if unknown:
            print("refused: no date fix is available for row(s) " + ", ".join(x[:40] for x in unknown)
                  + " (see: transfers --fix-history)")
            return 2
        fixes = [h for h in fixes if h["recv"] in only]
    lines = ["Robinhood moves between your own accounts: read-only report (nothing is changed, no Robinhood call).",
             "Switch robinhood.personal_positions_sync.transfers.enabled is " + (
                 "ON: moves are detected and booked." if xc["enabled"] else
                 "off: moves are not detected; the old rules apply."),
             f"Settings: window {xc['window_s'] / 3600:g} hours; share counts may differ by {xc['tol_pct']:g}% "
             f"(at least {_PP_XFER_MIN_SH:g} shares).",
             f"Moves waiting for their second look: {len(w['pend'])}"]
    for p in w["pend"]:
        d = p["dep"]
        lines.append(f"  - {d['ticker']} {d['qty']:g} sh: {d['account']} -> {p['arr']['account']}, first seen "
                     f"{_pp_xfer_when(d['seen'])}, paired {_pp_xfer_when(p['since'])}")
    lines.append(f"Remembered, not matched yet: {len(w['deps'])} departure(s), {len(w['arrs'])} arrival(s)")
    lines.append("Accounts whose missing stocks wait for a sale on record (mass-close rule): "
                 + ("; ".join(strikes) if strikes else "none"))
    lines.append(f"Moves on record: {len(hist)} (the sending row was closed as a move)")
    for h in hist:
        s = f"  - {h['ticker']}: {h['from']} -> {h['to'] or '?'}, closed {h['closed']} (row {h['row']})"
        if h["state"] == "fix":
            s += (f"; receiving row {h['recv']} is dated {h['recv_entry']}, which looks like a fresh buy; the moved "
                  f"shares were first bought {h['first_buy']}")
        elif h["state"] == "ok":
            s += f"; receiving row {h['recv']} already carries the first buy date"
        else:
            s += f"; {h['state']}"
        lines.append(s)
    if not fix:
        lines.append(f"First-buy-date fixes available: {len(fixes)}" + (
            " (list them: transfers --fix-history; write them: transfers --fix-history --confirm; one row only: "
            "add --row ID)" if fixes else ""))
        print(strip_masks("\n".join(lines)))
        return 0
    if not confirm:
        lines.append(f"History fix, dry run (nothing written): {len(fixes)} receiving row(s) would get the first buy "
                     f"date of the moved shares:")
        lines += [f"  - row {h['recv']} ({h['ticker']} in {h['to']}): {h['recv_entry']} -> {h['first_buy']}"
                  for h in fixes]
        print(strip_masks("\n".join(lines)))
        return 0
    try:
        done = _pp_xfer_fix_history(path if db else None, only=set(only) or None)
    except Exception as e:
        print(strip_masks("\n".join(lines)))
        print(f"History fix failed, nothing was written ({type(e).__name__}).")
        return 1
    lines.append(f"History fix written: {len(done)} receiving row(s) now carry the first buy date of the moved shares.")
    for h in done:
        lines.append(f"  - row {h['recv']} ({h['ticker']} in {h['to']}): {h['recv_entry']} -> {h['first_buy']}")
        if not db:
            _pp_log(f"Robinhood sync: {h['ticker']} in {h['to']} now shows the first buy date {h['first_buy']} of the "
                    f"shares moved from {h['from']} (it showed {h['recv_entry']}, the day of the move).",
                    ticker=h["ticker"], extra={"kind": "transfer_history_fix", "row_id": h["recv"],
                                               "from": h["from"], "to": h["to"]})
    print(strip_masks("\n".join(lines)))
    return 0


def _pp_tick() -> dict:
    """One scheduled reconcile tick (no-op unless both flags are on and the cutover is done)."""
    return sync_personal_positions(dry_run=False)


def _personal_sync_jobs() -> list:
    """Reconcile schedule for S2's integrator (S1 touches no scheduler): every poll_seconds.positions
    during market hours on trading days, plus once after the close."""
    try:
        every = int(_cfg()["poll_seconds"].get("positions") or 300)
    except Exception:
        every = 300
    return [{"name": "rh_personal_reconcile", "every_s": every, "window_et": ["09:30", "16:00"],
             "trading_days_only": True, "fn": _pp_tick,
             "note": "mason_robinhood.sync_personal_positions(dry_run=False); no-op until the cutover"},
            {"name": "rh_personal_reconcile_close", "at_et": "16:10", "trading_days_only": True, "fn": _pp_tick}]


def _cli(argv) -> int:
    """`diff` prints the dry-run; `cutover --confirm` runs the first-time cutover (Nico's go-live step);
    `transfers` prints the read-only report of moves between accounts (see _pp_xfer_report)."""
    cmd = argv[0] if argv else ""
    if cmd == "transfers":
        return _pp_xfer_report(argv[1:])
    if cmd == "diff":
        if not enabled():
            print("refused: robinhood.enabled is false in mason.config.json")
            return 2
        print(json.dumps(sync_personal_positions(dry_run=True), indent=1, default=str))
        return 0
    if cmd == "cutover":
        if "--confirm" not in argv[1:]:
            print("refused: the cutover replaces the hand rows for the Robinhood accounts — "
                  "review `python3 mason_robinhood.py diff`, then re-run with --confirm")
            return 2
        if not enabled() or not _flag("personal_positions_sync", "enabled"):
            print("refused: robinhood.enabled and robinhood.personal_positions_sync.enabled must both be true")
            return 2
        res = sync_personal_positions(dry_run=False, cutover=True)
        print(json.dumps(res, indent=1, default=str))
        if res.get("cutover") == "done" or res.get("already_cut_over"):
            return 0
        print("refused: drift vs the approved baseline — review a fresh diff with Nico"
              if res.get("aborted") == "drift" else "cutover failed — nothing was written")
        return 3
    print("usage: python3 mason_robinhood.py diff | cutover --confirm | transfers [--fix-history [--row ID] [--confirm]]")
    return 2


def _last4_template() -> int:
    """Nico-run, READ-ONLY (one get_accounts call, nothing is written): prints a PROPOSAL for the private
    robinhood.accounts_by_last4 map (RULINGS #2). A legacy-excluded label (e.g. exclude_accounts) is proposed as
    'family', everything else as 'personal'; two accounts sharing a last-4 get the shortest unique longer suffix.
    The output holds real account suffixes: it goes to the terminal only. Agents never run this."""
    if not enabled():
        print("refused: robinhood.enabled is false in mason.config.json")
        return 2
    try:
        raw = (_call("get_accounts") or {}).get("accounts") or []
    except Exception as e:
        print(f"could not read the Robinhood accounts: {type(e).__name__}")
        return 1
    active = [a for a in raw if isinstance(a, dict) and not (a.get("deactivated") or a.get("permanently_deactivated"))
              and a.get("state") == "active" and str(a.get("account_number") or "")]
    legacy_excl = _resolve_exclude_legacy(_config().get("rh_expansion"), log=lambda m: None)
    # QA R4: the stored history decides too. An account whose last-4 was EVER recorded under an excluded label, or
    # under a last stored role other than personal, or that cannot be told apart from another account, is proposed
    # as `family`, never `personal` (read-only: no new Robinhood call).
    hist, roles, hist_note = {}, {}, ""
    try:
        persisted, seed, _extra = _alias_db_read()
        for d in (persisted or {}, seed):
            for hk, labs in d.items():
                hist.setdefault(hk, set()).update(labs)
        roles = _alias_db_roles()
    except Exception as e:
        hist_note = f"the stored account history could not be read ({type(e).__name__}); EVERY role needs a manual check"
    nums = [str(a["account_number"]) for a in active]
    out, lines = {}, []
    for a, n in zip(active, nums):
        k = n[-4:]
        for width in range(4, 9):
            k = n[-width:]
            if sum(1 for o in nums if o.endswith(k)) == 1:
                break
        rel = lambda ek: ek == k or ek == n[-4:] or k.endswith(ek) or ek.endswith(k)      # noqa: E731
        why = []
        if norm_account_label(_label(a)) in legacy_excl:
            why.append("its current label is on the exclude list")
        if any(labs & legacy_excl for ek, labs in hist.items() if rel(ek)):
            why.append("previously recorded under an excluded label")
        if any(r and r != "personal" and r != "unknown" for ek, r in roles.items() if rel(ek)):
            why.append("previously recorded with a role other than personal")
        if sum(1 for o in nums if o.endswith(n[-4:])) > 1:
            why.append("shares its last 4 digits with another account")
        if hist_note:
            why.append("history unreadable")
        role = "family" if why else "personal"
        out[k] = role
        lines.append(f"  {MASK_PREFIX}{n[-4:]}  {str(_label(a))[:40]!r:<44} -> {role}" + (f"   CHECK: {'; '.join(why)}" if why else ""))
    print("Proposal for robinhood.accounts_by_last4 (review every role; an account you leave out is DROPPED):")
    if hist_note:
        print("WARNING: " + hist_note)
    print("\n".join(lines))
    print("\nPaste this object into the \"robinhood\" section of ~/.config/mason/mason.secrets.json:")
    print(json.dumps({"accounts_by_last4": out}, indent=2))
    return 0 if out else 1


# ── brain context (RH-D3) ─────────────────────────────────────────────────────
def brain_label(acct) -> str:
    """N6b (RULINGS #49.5; was A2_n6's _brain_label): the account label as a MODEL (Opus, qwen, the chat model) sees it.
    A display label can carry a ••NNNN mask (a label shared by two accounts, or a blank nickname); no last-4 goes to a
    model, so the mask is dropped. Stored labels and the UI keep the mask (stable per account). A label that is only a
    mask (or empty) becomes "Account". Pure; never raises."""
    if acct is None:
        return "Account"
    try:
        return strip_masks(acct).strip() or "Account"
    except Exception:
        return "Account"


def strip_masks(text) -> str:
    """N6b rework (QA R2/N6c): FREE TEXT as a model may see it: every ••NNNN mask (and a truncated one) removed, the rest of
    the text untouched. brain_label() is this plus "a bare label that is only a mask becomes 'Account'". Pure; never raises
    (on a failure it removes every bullet and every 4+ digit run: fail closed)."""
    try:
        return _MASK_RE.sub("", str(text))                                   # the literal mask or its JSON-escaped form
    except Exception:
        return re.sub(r"•+\s*\d*|\\u2022(?:\\u2022)*\s*\d*|\d{4,}", "", "" if text is None else str(text))


_brain_label = brain_label        # A2_n6's private name (its test and context_text keep working)


def context_text(max_lines: int = 40) -> str:
    """Nico's real holdings for Opus/analysts. Context only — never a trade trigger. Only in-scope accounts:
    rh_expansion.personal_scope.exclude_accounts (Household) and blank labels are left out (T-D6, RULINGS #10/#12/#13)."""
    if not enabled() or not _flag("brain_context", "enabled"):
        return ""
    try:
        pos = in_scope_positions()
        if not pos:
            return ""
        q = quotes(sorted({p["ticker"] for p in pos if p["kind"] == "equity"} |
                          {p["underlying"] for p in pos if p["kind"] == "option"}))
        lines = ["Nico's REAL holdings at Robinhood (read-only context — never trade these, "
                 "never propose orders for these accounts):"]
        by_acct = {}
        for p in pos:
            by_acct.setdefault(p["account"], []).append(p)
        for acct, rows in by_acct.items():
            vals = []
            for p in rows:
                last = (q.get(p["ticker"]) or {}).get("last")
                if p["kind"] == "equity" and last is not None:
                    vals.append((p, p["qty"] * last, last))
                else:
                    vals.append((p, None, last))
            tot = sum(v for _, v, _ in vals if v) or 0.0
            lines.append(f"- {brain_label(acct)}:")
            for p, v, last in sorted(vals, key=lambda x: -(x[1] or 0)):
                if p["kind"] == "option":
                    lines.append(f"    {p['underlying']} {p.get('expiry')} {p.get('strike')} "
                                 f"{p.get('option_type')} x{p['qty']:g} ({p['side']}) avg {p['avg_cost']}")
                    continue
                pl = (f" P/L {((last - p['avg_cost']) / p['avg_cost'] * 100):+.1f}%"
                      if last and p.get("avg_cost") else "")
                w = f" {v / tot * 100:.0f}% of acct" if (v and tot) else ""
                flag = " [CONCENTRATED]" if (v and tot and v / tot > 0.25 and len(vals) > 1) else ""
                lines.append(f"    {p['ticker']} {p['qty']:g} sh @ {p['avg_cost']}{pl}{w}{flag}")
        return "\n".join(lines[:max_lines])
    except Exception as e:
        _err("context_text", e)
        return ""


def status() -> dict:
    now = time.time()
    return {"enabled": enabled(), "scope_mode": _scope_mode()["mode"], "connected": _state["connected"],
            "last_ok": _state["last_ok"],
            "token_file_exists": _token_path().exists(), "accounts": len(_acct_cache["rows"] or []),
            "calls_total": _state["calls"],
            "calls_last_60s": sum(1 for t in _state["last_call_ts"] if now - t < 60),
            "rate": {"live_per_s": _lane_rate("live"), "bulk_per_s": _lane_rate("bulk"),
                     "halved": _clock() < _lim["halved_until"], "rate_hits": _state["rate_hits"],
                     "retries": _state["retries"]},
            "paged_truncated": _state["paged_truncated"],
            "errors": list(_state["errors"])}


# ══ MCP / OAuth machinery ═════════════════════════════════════════════════════
class FileTokenStorage:
    """mcp TokenStorage backed by one 0600 JSON file. Also persists the token expiry and the
    OAuth server metadata — without them the SDK forgets both on restart (expired token looks
    valid → 401 → full browser login; refresh posts to the wrong token URL)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        new = tokens.model_dump(mode="json", exclude_none=True)
        old = data.get("tokens") or {}
        if not new.get("refresh_token") and old.get("refresh_token"):
            new["refresh_token"] = old["refresh_token"]          # non-rotating refresh
        data["tokens"] = new
        data["expires_at"] = time.time() + float(new["expires_in"]) if new.get("expires_in") else None
        data["saved_at"] = datetime.now().isoformat(timespec="seconds")
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        c = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def get_metadata(self) -> dict:
        return self._read().get("oauth_metadata") or {}

    def set_metadata(self, md: dict) -> None:
        data = self._read()
        data["oauth_metadata"] = md
        self._write(data)

    def get_expires_at(self):
        return self._read().get("expires_at")


def _make_provider(storage: FileTokenStorage, redirect_handler=None, callback_handler=None):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata

    class _MasonOAuth(OAuthClientProvider):
        async def _initialize(self):
            await super()._initialize()
            md = storage.get_metadata()
            if md:
                try:
                    self.context.oauth_metadata = OAuthMetadata.model_validate(md)
                except Exception:
                    pass
            exp = storage.get_expires_at()
            if exp:
                self.context.token_expiry_time = float(exp) - 60   # refresh a minute early

        async def _handle_token_response(self, response):
            await super()._handle_token_response(response)
            if self.context.oauth_metadata:
                storage.set_metadata(self.context.oauth_metadata.model_dump(mode="json", exclude_none=True))

    meta = OAuthClientMetadata(client_name="Mason (read-only)", redirect_uris=[REDIRECT_URI],
                               grant_types=["authorization_code", "refresh_token"],
                               response_types=["code"], token_endpoint_auth_method="none")
    return _MasonOAuth(server_url=SERVER_URL, client_metadata=meta, storage=storage,
                       redirect_handler=redirect_handler, callback_handler=callback_handler)


async def _no_login_redirect(url: str) -> None:
    raise RHNeedsLogin("Robinhood token missing/expired — run: python3 mason_robinhood.py login")


def _parse_result(res):
    if getattr(res, "isError", False):
        txt = " ".join(getattr(c, "text", "") for c in (res.content or []))
        raise RHError(txt[:300] or "tool error")
    body = getattr(res, "structuredContent", None)
    if not body:
        txt = "".join(getattr(c, "text", "") for c in (res.content or []))
        body = json.loads(txt) if txt.strip() else {}
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


class _Runner:
    """One background asyncio loop + one long-lived MCP session (lazy, reopened on failure)."""

    def __init__(self):
        self.loop = None
        self.session = None
        self.stop = None
        self.ready = None
        self.lock = threading.Lock()

    def _ensure_loop(self):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, name="mason-robinhood", daemon=True).start()

    async def _owner(self, ready: asyncio.Future):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        try:
            auth = _make_provider(FileTokenStorage(_token_path()), redirect_handler=_no_login_redirect)
            async with streamablehttp_client(SERVER_URL, auth=auth, timeout=CALL_TIMEOUT_S,
                                            terminate_on_close=False) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    self.session, self.stop = s, asyncio.Event()
                    ready.set_result(True)
                    await self.stop.wait()
        except BaseException as e:            # noqa: BLE001 — surfaced via the ready future
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RHError(repr(e)))
        finally:
            self.session = None

    async def _connect(self):
        if self.session is not None:
            return self.session
        ready = self.loop.create_future()
        self.loop.create_task(self._owner(ready))
        await asyncio.wait_for(ready, CONNECT_TIMEOUT_S)
        return self.session

    async def _acall(self, tool, args):
        s = await self._connect()
        try:
            res = await asyncio.wait_for(s.call_tool(tool, args), CALL_TIMEOUT_S)
        except Exception:
            if self.stop:
                self.stop.set()              # drop the session; the next call reconnects
            raise
        return _parse_result(res)

    def call(self, tool, args):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._acall(tool, args), self.loop)
        return fut.result(CONNECT_TIMEOUT_S + CALL_TIMEOUT_S + 2)

    async def _alist(self):
        s = await self._connect()
        return [t.name for t in (await s.list_tools()).tools]

    def list_tools(self):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(self._alist(), self.loop).result(CONNECT_TIMEOUT_S + 5)


_runner = _Runner()


def _mcp_invoke(tool: str, args: dict):
    return _runner.call(tool, args)


_invoke = _mcp_invoke          # module hook — tests replace this


# ══ CLI: one-time login + smoke ═══════════════════════════════════════════════
def _login() -> int:
    """Interactive, human-run. Registers Mason as an OAuth client and stores the tokens."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from http.server import BaseHTTPRequestHandler, HTTPServer

    got = queue.Queue()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                got.put((qs["code"][0], (qs.get("state") or [None])[0]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Mason: Robinhood login received. You can close this tab.")

        def log_message(self, *a):
            pass

    def _serve():
        try:
            srv = HTTPServer(("127.0.0.1", 8765), _H)
            srv.timeout = 1
            while got.empty():
                srv.handle_request()
        except OSError as e:
            print(f"(local callback server unavailable: {e} — use paste-back)")

    def _stdin():
        try:
            line = sys.stdin.readline().strip()
            qs = parse_qs(urlparse(line).query)
            if "code" in qs:
                got.put((qs["code"][0], (qs.get("state") or [None])[0]))
        except Exception:
            pass

    async def redirect(url: str):
        print("\n1) Open this URL in a browser and approve Mason (read-only):\n")
        print("   " + url + "\n")
        print("2) The browser is sent to http://localhost:8765/callback?code=…")
        print("   If that page doesn't load (browser on another machine), copy the FULL address-bar")
        print("   URL and paste it here, then press Enter:\n")

    async def callback():
        threading.Thread(target=_serve, daemon=True).start()
        threading.Thread(target=_stdin, daemon=True).start()
        while got.empty():
            await asyncio.sleep(0.5)
        return got.get()

    async def run():
        storage = FileTokenStorage(_token_path())
        auth = _make_provider(storage, redirect_handler=redirect, callback_handler=callback)
        async with streamablehttp_client(SERVER_URL, auth=auth, timeout=30, terminate_on_close=False) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                print(f"\nConnected. Server exposes {len(names)} tools.")
                unlisted = [n for n in names if not _is_forbidden(n) and n not in READ_TOOLS
                            and n not in WRITE_TOOLS]
                blocked = [n for n in names if _is_forbidden(n)]
                print(f"Hard-blocked (trade/order): {len(blocked)}  ·  refused as unlisted: {len(unlisted)}")
                accts = _parse_result(await s.call_tool("get_accounts", {}))
                for a in accts.get("accounts") or []:
                    if a.get("state") == "active" and not a.get("deactivated"):
                        print(f"  {_label(a):<14} {MASK_PREFIX}{str(a.get('account_number'))[-4:]}")
        print(f"\nToken saved to {_token_path()} (0600). Mason can now read Robinhood.")

    try:
        asyncio.run(run())
        return 0
    except Exception as e:
        print(f"Login failed: {type(e).__name__}: {e}")
        return 1


def _smoke() -> int:
    """Read-only live check using the saved token (no browser). Measures call latency."""
    _cfg_cache["data"] = dict(_config(), robinhood=dict(_cfg(), enabled=True))
    _cfg_cache["mtime"] = CONFIG.stat().st_mtime
    t0 = time.time()
    accs = accounts()
    print(f"accounts: {[a['label'] for a in accs]}  ({time.time() - t0:.2f}s)")
    t0 = time.time()
    pos = positions(max_age=0)
    print(f"positions: {len(pos)}  ({time.time() - t0:.2f}s)")
    for p in pos:
        print(f"  {p['account']:<12} {p['kind']:<6} {p['ticker']:<6} {p['qty']:g} @ {p['avg_cost']}")
    t0 = time.time()
    q = quotes(sorted(position_symbols()) or ["SPY"])
    print(f"quotes: {len(q)}  ({time.time() - t0:.2f}s)")
    print(json.dumps(status(), indent=1))
    return 0 if accs else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        sys.exit(_login())
    if cmd == "smoke":
        sys.exit(_smoke())
    if cmd == "last4-template":
        sys.exit(_last4_template())
    if cmd in ("diff", "cutover", "transfers"):
        sys.exit(_cli(sys.argv[1:]))
    print("usage: python3 mason_robinhood.py login | smoke | diff | cutover --confirm | last4-template | transfers")
    sys.exit(2)
