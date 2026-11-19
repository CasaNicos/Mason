"""
mason_logsetup.py — service log level + the yfinance "no data" filter (audit 2026-11-13, O8).

1. LOG LEVEL. The service used to log at DEBUG by default: ~500 MB a week in the system
   journal, with whole prompts, memory and RH payloads (the MCP client's DEBUG lines) in
   clear text. The default is now INFO. One switch turns the old behaviour back on:

       mason.config.json  →  "logging": {"level": "DEBUG"}      (restart Mason)
       or env MASON_LOG_LEVEL=DEBUG                              (wins over the config)

   With DEBUG on, the trace mirror in the journal goes back to full detail too
   (mason_trace._log_cap); at INFO it is capped at 280 characters per field. The
   mason_trace.jsonl file itself is unchanged (always full, local only).

2. YFINANCE "possibly delisted; no price data found". yfinance logs this at ERROR for any
   symbol that returns no rows. ~9,079 a week, and almost none were dead symbols:
     * 97% came from the watch-tape pre-market poll (04:00-08:00 ET, 1-minute bars, period=1d)
       asking for symbols that simply had not traded yet that morning (FICO, REGN, XLB ...).
       That poll wraps its download in `quiet_yf_no_data()`; those records are counted, never
       logged (count in mason_watch_tape.status()["extended"]["no_data_today"] and `counts()`
       here). Only the empty-bars answer ("no price data found") and yfinance's "N Failed
       downloads:" header are quieted there; any other error (no timezone found = Yahoo not
       reachable, connection errors ...) still goes to the log.
     * the rest were foreign listings picked up from news tags (ISP, BAYN, 6701 ...), retried
       every cycle.
   Every other "possibly delisted" line is logged ONCE per symbol per day as a WARNING; repeats
   are counted and summarised in one INFO line when the day rolls over.
   OFF by default (review 2026-11-15): config logging.yf_mark_not_found = true also remembers a
   symbol as not-found for 24 h (mason_symbols.mark_not_found(sym, "symbol")) when Yahoo says
   explicitly "symbol may be delisted" on a multi-day request. Off because that mark makes
   mason_symbols.work_ok False, and mason_news_quality.tradable (the triage alert gate, no book
   exemption) uses work_ok: a Yahoo-side spelling miss (Mason asks "BRK.A", Yahoo wants "BRK-A")
   or a transient Yahoo 404 would silence a live US name's news for a day. Even when on, only
   plain symbols (letters/digits, no "." or "-") are marked.

2b. YAHOO REFUSALS (2026-11-16). yfinance shares ONE Yahoo cookie + "crumb" (sign-in token) per
   process. When Yahoo rotates it, the next requests come back 401 "Invalid Crumb"; yfinance swaps
   its sign-in method and tries once more, and when several threads do that at the same moment
   some get a second 401 ("Invalid Crumb", or "User is unable to access this feature" for a token
   minted the other way). yfinance then logs the 401 at ERROR and hands back an empty answer.
   ~5-27 a day, all from dossier builds. Every such record bumps a per-thread counter
   (`yf_refusals()`), so a caller can tell "Yahoo refused" from "Yahoo has no data" and ask again
   (mason_dossier does, once). Logging: the 1st, 10th, 100th ... refusal of each kind in a day is
   logged as ONE WARNING with the running count; the rest are counted (`refusal_counts()`), and a
   dossier part that is still refused on the second try is logged by `note_yf_loss` once per item
   per day, so no dossier part goes missing without a line saying so (other Yahoo callers do not
   retry: their refusals after the 1st/10th/100th are only counted). The day's totals are logged
   at the first refusal of a later day (like the no-data summary above; a restart starts at 0).
   Config logging.yf_quiet_refusals=false keeps every refusal as the original ERROR line (the
   counter still works).

3. POLLING ACCESS LINES. A dashboard tab polls a few endpoints every second (~370k request
   lines, ~62 MB a week). Below DEBUG, SUCCESSFUL (2xx/304) GETs of the paths in
   config logging.quiet_poll_paths (default below) are counted, not logged; every error status
   and every other request is still logged.

Never imports mason_app. Public functions never raise.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
DEFAULT_LEVEL = "INFO"
_VALID = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

log = logging.getLogger("mason.logsetup")


def _config_level():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        v = ((cfg.get("logging") or {}).get("level") or "")
        return str(v).strip().upper() or None
    except Exception:
        return None


def level() -> str:
    """Env MASON_LOG_LEVEL, else config logging.level, else INFO. Unknown names fall back to INFO."""
    for v in (os.environ.get("MASON_LOG_LEVEL"), _config_level()):
        v = (v or "").strip().upper()
        if v == "WARN":
            v = "WARNING"
        if v in _VALID:
            return v
    return DEFAULT_LEVEL


# ── yfinance no-data filter ─────────────────────────────────────────────────────
_tls = threading.local()
_lock = threading.Lock()
_SYM_RE = re.compile(r"\$([A-Za-z0-9.\-^=]{1,15}):|'([A-Za-z0-9.\-^=]{1,15})'")
_YAHOO_DEAD = "symbol may be delisted"
_counts = {"day": None, "expected_empty": 0, "logged": 0, "repeats": 0, "marked_not_found": 0}
_seen_today: set = set()
_marked_today: set = set()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def counts() -> dict:
    with _lock:
        return dict(_counts)


@contextmanager
def quiet_yf_no_data():
    """Inside this block, yfinance "possibly delisted / no price data" records are EXPECTED
    (e.g. pre-market minute bars before a symbol trades): counted, never logged."""
    prev = getattr(_tls, "quiet", False)
    _tls.quiet = True
    try:
        yield
    finally:
        _tls.quiet = prev


def _roll_day_locked(today: str):
    """Reset the per-day sets; returns yesterday's summary (logged by the caller, outside the lock)."""
    if _counts["day"] == today:
        return None
    prev = None
    if _counts["day"] is not None and (_counts["repeats"] or _counts["expected_empty"]):
        prev = (_counts["day"], _counts["logged"], _counts["repeats"], _counts["expected_empty"])
    _counts.update(day=today, expected_empty=0, logged=0, repeats=0, marked_not_found=0)
    _seen_today.clear()
    _marked_today.clear()
    return prev


def _symbols(msg: str) -> list:
    out = []
    for a, b in _SYM_RE.findall(msg or ""):
        s = (a or b).upper()
        if s and s not in out:
            out.append(s)
    return out


def _mark_enabled() -> bool:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return (cfg.get("logging") or {}).get("yf_mark_not_found") is True
    except Exception:
        return False


_PLAIN_SYM = re.compile(r"[A-Z][A-Z0-9]{0,5}")


def _mark_dead(syms) -> int:
    n = 0
    try:
        import mason_symbols
        for s in syms:
            if mason_symbols.mark_not_found(s, "symbol", "Yahoo: no data found, symbol may be delisted"):
                n += 1
    except Exception:
        pass
    return n


class _YfNoDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        header = "Failed download" in msg and "possibly delisted" not in msg
        if "possibly delisted" not in msg and "no price data found" not in msg and not header:
            return True
        if header and not getattr(_tls, "quiet", False):
            return True                       # outside the quiet block: unchanged
        try:
            summary = None
            to_mark = []
            with _lock:
                summary = _roll_day_locked(_today())
                if getattr(_tls, "quiet", False) and (header or "no price data found" in msg):
                    if not header:
                        _counts["expected_empty"] += 1
                    keep = False
                else:
                    syms = _symbols(msg) or ["?"]
                    new = [s for s in syms if s not in _seen_today]
                    _seen_today.update(syms)
                    if _YAHOO_DEAD in msg and "period=1d" not in msg:
                        to_mark = [s for s in syms if _PLAIN_SYM.fullmatch(s) and s not in _marked_today]
                        _marked_today.update(to_mark)
                    if new:
                        _counts["logged"] += 1
                        keep = True
                    else:
                        _counts["repeats"] += 1
                        keep = False
            if summary:
                d, lg, rep, exp = summary
                log.info("yfinance no-data summary for %s: %d logged, %d repeats not logged, "
                         "%d expected-empty (pre-market) not logged", d, lg, rep, exp)
            if to_mark and not _mark_enabled():
                to_mark = []
            if to_mark:
                n = _mark_dead(to_mark)
                with _lock:
                    _counts["marked_not_found"] += n
            if keep:
                record.levelno, record.levelname = logging.WARNING, "WARNING"
                record.msg, record.args = (msg + "  [logged once per symbol per day; repeats counted"
                                           + ("; remembered as not-found 24h" if to_mark else "") + "]"), ()
            return keep
        except Exception:
            return True


_FILTER = _YfNoDataFilter()


# ── Yahoo refusals (401 Invalid Crumb / unable to access this feature) ──────────
_REF_KINDS = (("Invalid Crumb", "crumb"), ("unable to access this feature", "feature"))
_ref = {"day": None, "crumb": 0, "feature": 0, "other401": 0, "lost": 0}
_lost_today: set = set()
_REF_WHY = {"crumb": "Invalid Crumb: its sign-in token had just expired",
            "feature": "\"unable to access this feature\": a sign-in token from the other method",
            "other401": "HTTP 401"}


def yf_refusals() -> int:
    """How many Yahoo refusals (HTTP 401) yfinance has logged IN THIS THREAD so far. Compare before
    and after a call to know whether that call was refused. Never raises."""
    return int(getattr(_tls, "refused", 0) or 0)


def refusal_counts() -> dict:
    with _lock:
        return dict(_ref)


def _quiet_refusals() -> bool:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return (cfg.get("logging") or {}).get("yf_quiet_refusals") is not False
    except Exception:
        return True


def _roll_ref_locked(today: str):
    if _ref["day"] == today:
        return None
    prev = None
    if _ref["day"] is not None and (_ref["crumb"] or _ref["feature"] or _ref["other401"] or _ref["lost"]):
        prev = dict(_ref)
    _ref.update(day=today, crumb=0, feature=0, other401=0, lost=0)
    _lost_today.clear()
    return prev


def _log_ref_summary(prev) -> None:
    if prev:
        log.info("Yahoo refusal summary for %s: %d expired-token, %d 'unable to access', %d other 401; "
                 "%d parts still missing after a retry", prev["day"], prev["crumb"], prev["feature"],
                 prev["other401"], prev["lost"])


def note_yf_loss(feature: str, symbol: str, what: str) -> bool:
    """A part Yahoo refused twice (the retry too): one WARNING per feature/symbol/part per day, so
    the gap is never silent. Returns True when logged now. Never raises."""
    try:
        key = (str(feature), str(symbol).upper(), str(what))
        with _lock:
            prev = _roll_ref_locked(_today())
            _ref["lost"] += 1
            new = key not in _lost_today
            _lost_today.add(key)
        _log_ref_summary(prev)
        if new:
            log.warning("%s %s: Yahoo refused %s twice (sign-in token trouble on Yahoo's side); that part "
                        "is missing this time and is fetched again on the next build "
                        "[logged once per item per day]", key[0], key[1], key[2])
        return new
    except Exception:
        return False


class _YfRefusalFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "HTTP Error 401" not in msg and "401 Client Error" not in msg:
            return True
        try:
            kind = next((k for frag, k in _REF_KINDS if frag in msg), "other401")
            _tls.refused = int(getattr(_tls, "refused", 0) or 0) + 1
            with _lock:
                prev = _roll_ref_locked(_today())
                _ref[kind] += 1
                n = _ref[kind]
            _log_ref_summary(prev)
            if not _quiet_refusals():
                return True                         # switch off: the original ERROR line, unchanged
            if n in (1, 10, 100, 1000, 10000):
                record.levelno, record.levelname = logging.WARNING, "WARNING"
                record.msg, record.args = (f"Yahoo refused a request ({_REF_WHY[kind]}); {n} today. yfinance "
                                           f"fetches a new token and Mason asks again once "
                                           f"[logged at the 1st, 10th, 100th ... of the day; "
                                           f"the rest counted]"), ()
                return True
            return False
        except Exception:
            return True


_REF_FILTER = _YfRefusalFilter()

# ── werkzeug polling access lines ───────────────────────────────────────────────
DEFAULT_QUIET_POLL_PATHS = ("/api/paper/positions", "/api/sectors/live", "/api/following/quotes",
                            "/api/paper/portfolio_history")
_ACCESS_RE = re.compile(r'"GET ([^ ?"]+)(?:\?[^ "]*)? HTTP/[0-9.]+" (\d{3})')
_quiet_paths: set = set(DEFAULT_QUIET_POLL_PATHS)
_poll_counts = {"suppressed": 0}


def _config_quiet_paths():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        v = (cfg.get("logging") or {}).get("quiet_poll_paths")
        if isinstance(v, list):
            return {str(x) for x in v if isinstance(x, str) and x.startswith("/")}
    except Exception:
        pass
    return None


class _PollAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if logging.getLogger("mason").isEnabledFor(logging.DEBUG):
                return True
            m = _ACCESS_RE.search(record.getMessage())
            if not m or m.group(1) not in _quiet_paths:
                return True
            code = int(m.group(2))
            if 200 <= code < 300 or code == 304:
                with _lock:
                    _poll_counts["suppressed"] += 1
                return False
            return True
        except Exception:
            return True


_POLL_FILTER = _PollAccessFilter()


def poll_counts() -> dict:
    with _lock:
        return dict(_poll_counts, paths=sorted(_quiet_paths))


def install_filters() -> None:
    """Attach the filters (idempotent). basicConfig(force=True) does not remove them: they sit
    on the 'yfinance' and 'werkzeug' loggers, not on a root handler."""
    try:
        lg = logging.getLogger("yfinance")
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        if _REF_FILTER not in lg.filters:
            lg.addFilter(_REF_FILTER)
    except Exception:
        pass
    try:
        qp = _config_quiet_paths()
        if qp is not None:
            _quiet_paths.clear()
            _quiet_paths.update(qp)
        wl = logging.getLogger("werkzeug")
        if _POLL_FILTER not in wl.filters:
            wl.addFilter(_POLL_FILTER)
    except Exception:
        pass


def setup() -> str:
    """Install the filters and return the level name to configure. Never raises."""
    install_filters()
    return level()
