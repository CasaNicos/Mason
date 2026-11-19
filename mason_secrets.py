"""
mason_secrets.py — the one loader for Mason's private secrets file (phase 2, task B1a).

Ruling 1 (docs/phase2/RULINGS.md §1) in short:
  * One JSON file, ``mason.secrets.json``, ``"_schema": 1``. Default location
    ``~/.config/mason/mason.secrets.json`` (dir 0700, file 0600), overridable by the env var
    ``MASON_SECRETS_FILE``. Template: ``mason.secrets.example.json`` (repo root).
  * Per-key precedence: env var (non-empty) > secrets file > legacy ``mason.config.json``
    (read-only) > ``default``. An env var set to ``""`` (or whitespace) counts as unset.
  * When the secrets file is absent or malformed, API keys FALL BACK to the legacy config, so the
    live service never loses a key during the transition. (The Robinhood role map, read via
    ``load_section("robinhood")``, is the caller's to fail closed on "malformed" — ruling 2.)

File shape::

    {"_schema": 1,
     "ANTHROPIC_API_KEY": "...", "APCA_API_KEY_ID": "...", ...,      # flat string keys
     "robinhood": {"accounts_by_last4": {...}}}                       # object sections

Guarantees:
  * Never logs, prints or raises with a value. Log lines carry key NAMES and reason classes only.
  * Values are returned verbatim from every source (no stripping), so a key resolves to the same
    bytes with or without the secrets file (RULINGS #22.3). Legacy numbers -> ``str()``; other
    non-string legacy values -> missing (one warning naming the key).
  * ``load_section``: an empty section (only ``_doc`` / empty values) is "absent" (RULINGS #24.1).
  * Never writes ``os.environ``. ``scrubbed_env()`` returns a copy.
  * No import-time I/O and no import-time crash. No function raises.
  * Thread-safe; the file and the legacy config are each re-parsed only when their stat changes.
  * Imports nothing from Mason (stdlib only), so any module may import it without recursion.
  * File hardening (rulings 1.8, 27, 31): checks run on the fully resolved real path, so a symlinked
    parent directory is fine. EVERY file must be a regular file owned by the process uid in a parent
    directory that is not group/world-writable, else it is refused ("insecure_target": keys fall
    back to legacy, the robinhood section reads "malformed"). Mode wider than 0600: inside ``$HOME``
    warns once and is still used; outside ``$HOME`` is refused. Outside-``$HOME`` acceptance logs
    one warning naming the path. Warns once on a
    mode wider than 0600; strips a UTF-8 BOM; accepts CRLF; rejects files > 1 MiB or anything that is
    not a regular file (a FIFO never blocks); JSON ``null`` = absent.

Public API: ``get``, ``get_section``, ``load_section``, ``status``, ``missing_required``, ``source``,
``file_state``, ``path``, ``scrubbed_env``, ``reset_cache`` and the per-key getters
(``anthropic_key``, ``alpaca_keys``, ``fmp_key``, ``alpha_vantage_key``, ``fred_key``, ``eia_key``,
``finviz_key``, ``data_gov_key``, ``sec_user_agent``, ``robinhood_token_file``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
import threading
from pathlib import Path

__all__ = [
    "REGISTRY", "DEFAULT_PATH", "ENV_PATH_VAR", "SCHEMA", "MAX_BYTES",
    "get", "get_section", "load_section", "status", "missing_required", "source", "file_state",
    "path", "scrubbed_env", "reset_cache",
    "anthropic_key", "alpaca_keys", "fmp_key", "alpha_vantage_key", "fred_key", "eia_key",
    "finviz_key", "data_gov_key", "sec_user_agent", "robinhood_token_file",
]

_log = logging.getLogger("mason.secrets")

SCHEMA = 1
MAX_BYTES = 1024 * 1024
ENV_PATH_VAR = "MASON_SECRETS_FILE"
# Ruling 1.2. Nico's one-word veto moves it to "~/mason/mason.secrets.json" (this line only).
DEFAULT_PATH = "~/.config/mason/mason.secrets.json"
# Legacy source (read-only). Module attribute so tests / tools can point it elsewhere.
LEGACY_CONFIG = Path(__file__).resolve().parent / "mason.config.json"

# name -> env var names (first non-empty wins), legacy config path (tuple of keys) or None,
# required flag, service. Names are the canonical keys of the secrets file.
REGISTRY: dict = {
    "ANTHROPIC_API_KEY":     {"env": ("ANTHROPIC_API_KEY",),     "legacy": ("apis", "anthropic", "key"),
                              "required": True,  "service": "Anthropic API"},
    "APCA_API_KEY_ID":       {"env": ("APCA_API_KEY_ID",),       "legacy": ("paper_trading", "api_key"),
                              "required": True,  "service": "Alpaca (paper trading, market data, news)"},
    "APCA_API_SECRET_KEY":   {"env": ("APCA_API_SECRET_KEY",),   "legacy": ("paper_trading", "api_secret"),
                              "required": True,  "service": "Alpaca (paper trading, market data, news)"},
    "FMP_API_KEY":           {"env": ("FMP_API_KEY",),           "legacy": ("apis", "fmp", "key"),
                              "required": True,  "service": "Financial Modeling Prep"},
    "ALPHA_VANTAGE_API_KEY": {"env": ("ALPHA_VANTAGE_API_KEY",), "legacy": ("apis", "alpha_vantage", "key"),
                              "required": True,  "service": "Alpha Vantage"},
    "FRED_API_KEY":          {"env": ("FRED_API_KEY",),          "legacy": ("apis", "fred", "key"),
                              "required": True,  "service": "FRED (St. Louis Fed)"},
    "EIA_API_KEY":           {"env": ("EIA_API_KEY",),           "legacy": ("apis", "eia", "key"),
                              "required": True,  "service": "U.S. EIA"},
    "FINVIZ_API_KEY":        {"env": ("FINVIZ_API_KEY",),        "legacy": ("apis", "finviz", "key"),
                              "required": False, "service": "FinViz Elite (optional)"},
    "DATA_GOV_API_KEY":      {"env": ("DATA_GOV_API_KEY",),      "legacy": None,
                              "required": False, "service": "api.data.gov (congress.gov hearings calendar)"},
    "SEC_USER_AGENT":        {"env": ("SEC_USER_AGENT",),        "legacy": ("apis", "sec", "user_agent"),
                              "required": False, "service": "SEC EDGAR fair-access contact"},
    "ROBINHOOD_TOKEN_FILE":  {"env": ("ROBINHOOD_TOKEN_FILE",),  "legacy": ("robinhood", "token_file"),
                              "required": False, "service": "Robinhood MCP OAuth token path"},
    "TWILIO_ACCOUNT_SID":    {"env": ("TWILIO_ACCOUNT_SID",),    "legacy": None,
                              "required": False, "service": "Twilio SMS (retired, no code reader)"},
    "TWILIO_AUTH_TOKEN":     {"env": ("TWILIO_AUTH_TOKEN",),     "legacy": None,
                              "required": False, "service": "Twilio SMS (retired, no code reader)"},
    "TWILIO_PHONE_NUMBER":   {"env": ("TWILIO_PHONE_NUMBER",),   "legacy": None,
                              "required": False, "service": "Twilio SMS (retired, no code reader)"},
}

_lock = threading.RLock()
_ABSENT = {"state": "absent", "reason": "no_file", "obj": None, "mode_ok": True}
_file_cache = {"key": None, "res": dict(_ABSENT)}
_legacy_cache = {"key": None, "obj": {}}
_warned: set = set()
_stats = {"file_parses": 0, "legacy_parses": 0}


# ── helpers ──────────────────────────────────────────────────────────────────

def _once(level: int, tag: tuple, msg: str, *args) -> None:
    """Log ``msg`` once per ``tag``. Callers pass names / reason classes only, never values."""
    with _lock:
        if tag in _warned:
            return
        _warned.add(tag)
    try:
        _log.log(level, msg, *args)
    except Exception:
        pass


def _clean(x):
    """``x`` verbatim if it is a usable string, else None (None / non-str / empty / whitespace =
    unset). Values are NOT stripped, so a key resolves to the same bytes from every source (N5)."""
    if isinstance(x, str) and x.strip():
        return x
    return None


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def path() -> Path:
    """The secrets file path: ``$MASON_SECRETS_FILE`` (non-empty) else ``DEFAULT_PATH``."""
    try:
        p = _clean(os.environ.get(ENV_PATH_VAR))
        return Path(os.path.expanduser(p.strip() if p else DEFAULT_PATH))
    except Exception:
        return Path(os.path.expanduser(DEFAULT_PATH))


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _bad(reason: str, mode_ok: bool = True) -> dict:
    return {"state": "malformed", "reason": reason, "obj": None, "mode_ok": mode_ok}


# ── the secrets file ─────────────────────────────────────────────────────────

def _load_file() -> dict:
    """Return the cached parse result for the current secrets file. Never raises."""
    try:
        p = path()
        try:
            lst = os.lstat(p)
        except FileNotFoundError:
            with _lock:
                _file_cache["key"] = ("absent", str(p))
                _file_cache["res"] = dict(_ABSENT)
            return _file_cache["res"]
        except OSError:
            return _remember(("lstat_err", str(p)), _bad("unreadable"))

        # RULINGS #27 + #31: every check runs on the fully resolved real path (os.path.realpath), so
        # a symlinked intermediate directory (e.g. /home -> /data/home) is no reason to refuse.
        # ALWAYS required (any file, symlink or not, anywhere): a regular file, owned by the process
        # uid, in a parent directory that is neither group- nor world-writable -> else refused.
        # Mode: inside $HOME wider than 0600 warns once and is still used (rulings 1.8, 22.3);
        # outside $HOME wider than 0600 is refused (27.1). Refused = "malformed"/"insecure_target":
        # API keys fall back to the legacy config, the robinhood section reads "malformed".
        try:
            target = Path(os.path.realpath(p, strict=True))
        except (OSError, RuntimeError, ValueError):
            return _remember(("dangling", str(p), lst.st_mtime_ns), _bad("unreadable"))
        try:
            st = os.stat(target)
            pst = os.stat(target.parent)
        except OSError:
            return _remember(("stat_err", str(p)), _bad("unreadable"))
        uid = os.getuid()
        key = (str(p), str(target), st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid,
               pst.st_mode, uid, stat.S_ISLNK(lst.st_mode))
        with _lock:
            if _file_cache["key"] == key:
                return _file_cache["res"]
        if not stat.S_ISREG(st.st_mode):
            return _remember(key, _bad("not_regular_file"))
        fmode = stat.S_IMODE(st.st_mode)
        mode_ok = (fmode & ~0o600) == 0
        if st.st_uid != uid or (stat.S_IMODE(pst.st_mode) & 0o022):
            return _remember(key, _bad("insecure_target", mode_ok))
        outside = not _within(target, Path(os.path.realpath(_home())))
        if outside:
            if not mode_ok:
                return _remember(key, _bad("insecure_target", mode_ok))
            _once(logging.WARNING, ("outside_home_ok", str(target)),
                  "mason_secrets: secrets file outside $HOME accepted (owner/mode/dir checks passed): %s",
                  str(target))
        elif not mode_ok:
            _once(logging.WARNING, ("mode", str(p)),
                  "mason_secrets: secrets file mode %s is wider than 0600; run: chmod 600 <file>",
                  oct(fmode))
        if st.st_size > MAX_BYTES:
            return _remember(key, _bad("too_large", mode_ok))
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                with os.fdopen(fd, "rb") as fh:
                    fd = None
                    blob = fh.read(MAX_BYTES + 1)
            finally:
                if fd is not None:
                    os.close(fd)
        except OSError:
            return _remember(key, _bad("unreadable", mode_ok))
        if len(blob) > MAX_BYTES:
            return _remember(key, _bad("too_large", mode_ok))
        with _lock:
            _stats["file_parses"] += 1
        try:
            txt = blob.decode("utf-8")
        except UnicodeDecodeError:
            return _remember(key, _bad("not_utf8", mode_ok))
        if txt.startswith("﻿"):
            txt = txt[1:]
        try:
            parsed = json.loads(txt)
        except ValueError:
            return _remember(key, _bad("invalid_json", mode_ok))
        if parsed is None:
            return _remember(key, {"state": "absent", "reason": "null_document", "obj": None,
                                   "mode_ok": mode_ok})
        if not isinstance(parsed, dict):
            return _remember(key, _bad("not_an_object", mode_ok))
        sv = parsed.get("_schema", SCHEMA)
        if sv != SCHEMA or isinstance(sv, bool):
            return _remember(key, _bad("unsupported_schema", mode_ok))
        return _remember(key, {"state": "ok", "reason": "", "obj": parsed, "mode_ok": mode_ok})
    except Exception:
        return _bad("internal_error")


def _remember(key, res: dict) -> dict:
    with _lock:
        _file_cache["key"] = key
        _file_cache["res"] = res
    if res["state"] == "malformed":
        _once(logging.ERROR, ("malformed", key),
              "mason_secrets: secrets file is unusable (%s); API keys fall back to the legacy config",
              res["reason"])
    return res


# ── the legacy config (read-only) ────────────────────────────────────────────

def _legacy() -> dict:
    try:
        lp = LEGACY_CONFIG
        if lp is None:
            return {}
        lp = Path(lp)
        try:
            st = os.stat(lp)
        except OSError:
            return {}
        key = (str(lp), st.st_ino, st.st_mtime_ns, st.st_size)
        with _lock:
            if _legacy_cache["key"] == key:
                return _legacy_cache["obj"]
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16 * MAX_BYTES:
            parsed = {}
        else:
            try:
                parsed = json.loads(lp.read_text(encoding="utf-8-sig"))
            except Exception:
                parsed = {}
            with _lock:
                _stats["legacy_parses"] += 1
        if not isinstance(parsed, dict):
            parsed = {}
        with _lock:
            _legacy_cache["key"] = key
            _legacy_cache["obj"] = parsed
        return parsed
    except Exception:
        return {}


def _legacy_value(name, spec_path, cfg=None):
    if not spec_path:
        return None
    # B1b: a caller may pass the config dict it already loaded (its own, possibly test-redirected,
    # mason.config.json); that dict is then the legacy source instead of LEGACY_CONFIG.
    node = _legacy() if cfg is None else (cfg if isinstance(cfg, dict) else {})
    for part in spec_path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    # Exactly today's value: legacy strings are returned verbatim (callers read them that way now).
    if isinstance(node, str):
        return node if node != "" else None
    if node is None:
        return None
    # QA N1: a number becomes its str(); anything else (bool, list, dict) is treated as missing.
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return str(node)
    where = _legacy_cache.get("key") if cfg is None else "caller_cfg"
    _once(logging.WARNING, ("legacy_type", name, where),
          "mason_secrets: legacy config value for %s is not a string or number; treated as missing", name)
    return None


# ── resolution ───────────────────────────────────────────────────────────────

def _resolve(name, cfg=None):
    """(value or None, source)."""
    if not isinstance(name, str) or not name:
        return None, "default"
    spec = REGISTRY.get(name)
    if spec:
        for e in spec["env"]:
            ev = _clean(os.environ.get(e))
            if ev is not None:
                return ev, "env"
    res = _load_file()
    if res["state"] == "ok" and not name.startswith("_"):
        fobj = res["obj"]
        if name in fobj:
            fv = fobj.get(name)
            if isinstance(fv, dict) and not spec:
                pass  # a structured section (e.g. robinhood): read via load_section, no warning
            elif fv is not None and not isinstance(fv, str):
                _once(logging.WARNING, ("type", name, _file_cache.get("key")),
                      "mason_secrets: key %s in the secrets file is not a string; ignored", name)
            else:
                cv = _clean(fv)
                if cv is not None:
                    return cv, "file"
    if spec:
        lv = _legacy_value(name, spec["legacy"], cfg)
        if lv is not None:
            return lv, "legacy"
    return None, "default"


def get(name, default: str = "", cfg=None) -> str:
    """The value of secret ``name`` (env > file > legacy config > ``default``). Never raises.

    ``cfg``: the caller's already-loaded ``mason.config.json`` dict. When given, it is the legacy
    source (byte-exact with the caller's old ``cfg[...]`` read, and honouring a test-redirected
    config path); when omitted, ``LEGACY_CONFIG`` is read."""
    try:
        v, _ = _resolve(name, cfg)
        return default if v is None else v
    except Exception:
        return default


def source(name, cfg=None) -> str:
    """Where ``name`` resolves from: ``"env" | "file" | "legacy" | "default"``. No value."""
    try:
        return _resolve(name, cfg)[1]
    except Exception:
        return "default"


def load_section(name):
    """``(dict | None, state)`` with state in ``{"absent", "ok", "malformed"}`` (ruling 1.7).

    absent    = no file, a ``null`` document, the section key missing / ``null``, or a section whose
                every non-``_`` value is empty (``{}``, ``[]``, ``""``, ``null``) — RULINGS #24.1;
    malformed = the file is present but unusable, or the section is not an object;
    ok        = a deep copy of the section object.
    """
    try:
        if not isinstance(name, str) or not name or name.startswith("_"):
            return None, "absent"
        res = _load_file()
        if res["state"] != "ok":
            return None, res["state"]
        node = res["obj"].get(name)
        if node is None:
            return None, "absent"
        if not isinstance(node, dict):
            _once(logging.ERROR, ("section", name, _file_cache.get("key")),
                  "mason_secrets: section %s in the secrets file is not an object", name)
            return None, "malformed"
        if not any(not (isinstance(k, str) and k.startswith("_")) and v not in (None, {}, [], "")
                   for k, v in node.items()):
            # RULINGS #24.1: an empty section (or one holding only _doc / empty values, i.e. the
            # template's "robinhood": {"_doc": ..., "accounts_by_last4": {}}) counts as ABSENT.
            return None, "absent"
        return copy.deepcopy(node), "ok"
    except Exception:
        return None, "malformed"


def get_section(name) -> dict:
    """Convenience form of ``load_section``: the section dict, or ``{}`` when not "ok"."""
    d, st = load_section(name)
    return d if st == "ok" and isinstance(d, dict) else {}


def status() -> dict:
    """``{name: present_bool}`` for every registered secret. Booleans only."""
    out = {}
    for n in REGISTRY:
        try:
            out[n] = _resolve(n)[0] is not None
        except Exception:
            out[n] = False
    return out


def missing_required() -> list:
    """Names of required secrets that resolve nowhere."""
    st = status()
    return [n for n, spec in REGISTRY.items() if spec["required"] and not st.get(n)]


def file_state() -> dict:
    """``{"path", "state", "reason", "mode_ok"}`` for the secrets file. No values."""
    res = _load_file()
    return {"path": str(path()), "state": res["state"], "reason": res["reason"],
            "mode_ok": bool(res.get("mode_ok", True))}


# Ruling 39 (B1d / REVIEW_B1b N3): names that are treated as secrets even when they are NOT in REGISTRY.
# Matching is on the upper-cased name (an under-scrub is the dangerous direction; over-scrubbing a third-party
# subprocess is harmless). Suffix/prefix, never "contains": MY_API_KEY_FILE and TOKENIZERS_PARALLELISM survive.
_SCRUB_PREFIXES = ("ANTHROPIC_", "APCA_")
_SCRUB_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


def _is_secret_env_name(name: str) -> bool:
    u = name.upper()
    return u == ENV_PATH_VAR or u.startswith(_SCRUB_PREFIXES) or u.endswith(_SCRUB_SUFFIXES)


def scrubbed_env(keep=()) -> dict:
    """A copy of ``os.environ`` without secrets, except ``keep``.

    Removed: every registered secret env name AND the ruling-39 patterns (``*_API_KEY``, ``*_TOKEN``,
    ``*_SECRET``, ``ANTHROPIC_*``, ``APCA_*``, ``MASON_SECRETS_FILE``; case-insensitive, suffix / prefix only).
    ``keep`` is a name or an iterable of names (canonical or env names; matched exactly) and wins over both.
    ``os.environ`` itself is never modified. For third-party subprocesses (ruling 1.6, H2).
    """
    if isinstance(keep, str):
        keep = (keep,)
    keep_env = set()
    for k in keep or ():
        spec = REGISTRY.get(k)
        keep_env.update(spec["env"] if spec else (k,))
    drop = {e for spec in REGISTRY.values() for e in spec["env"]} - keep_env
    return {k: val for k, val in os.environ.items()
            if k not in drop and (k in keep_env or not _is_secret_env_name(k))}


def reset_cache() -> None:
    """Drop the file / legacy caches and the warn-once memory (tests, tools)."""
    with _lock:
        _file_cache["key"] = None
        _file_cache["res"] = dict(_ABSENT)
        _legacy_cache["key"] = None
        _legacy_cache["obj"] = {}
        _warned.clear()


# ── per-key getters ──────────────────────────────────────────────────────────

def anthropic_key(default: str = "", cfg=None) -> str:
    return get("ANTHROPIC_API_KEY", default, cfg)


def alpaca_keys(cfg=None) -> tuple:
    """``(key_id, secret)``; each ``""`` when unset."""
    return get("APCA_API_KEY_ID", "", cfg), get("APCA_API_SECRET_KEY", "", cfg)


def fmp_key(default: str = "", cfg=None) -> str:
    return get("FMP_API_KEY", default, cfg)


def alpha_vantage_key(default: str = "", cfg=None) -> str:
    return get("ALPHA_VANTAGE_API_KEY", default, cfg)


def fred_key(default: str = "", cfg=None) -> str:
    return get("FRED_API_KEY", default, cfg)


def eia_key(default: str = "", cfg=None) -> str:
    return get("EIA_API_KEY", default, cfg)


def finviz_key(default: str = "", cfg=None) -> str:
    return get("FINVIZ_API_KEY", default, cfg)


def data_gov_key(default: str = "", cfg=None) -> str:
    return get("DATA_GOV_API_KEY", default, cfg)


def sec_user_agent(default: str = "", cfg=None) -> str:
    return get("SEC_USER_AGENT", default, cfg)


def robinhood_token_file(default: str = "", cfg=None) -> str:
    return get("ROBINHOOD_TOKEN_FILE", default, cfg)
