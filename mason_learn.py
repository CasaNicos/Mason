"""
mason_learn.py — nightly grader of everything Mason looked at but did not trade
(audit L5, docs/fable/AUDIT_2026-11-13.md; Step 5 "learn fast"). NO model calls.

For every item Mason produced (a scored catalyst, an inbox alert, a director "watch" or long/short call,
a local-model watch / call / news read, a dossier, a scanner hit, a quick-check note) it
records, in ONE table (`learn_outcomes`, one row per item and horizon), what a simple,
fixed trade on that item would have returned:

  entry    the first price Mason could have traded at AFTER the item existed. The rule
           is the director's honest grade (mason_desk_pipeline._tradable_grade_row,
           "grade_v2"): next regular-session open + 5 minutes for anything that appeared
           outside trading hours, the price at the time for anything that appeared during
           the session. Never a price from before the item's timestamp.
  exit     the close of the Nth trading day that OPENED after the item appeared
           (N = 1, 3, 5, 10 by default) — the same session counting grade_v2 uses.
  result   in the item's own direction after the same 0.10% cost; for an item with no
           direction only the raw move is recorded (plus, when the item states a
           sentiment, the move in the direction of that sentiment).
  S&P 500  SPY over the very same window, so every graded row has "result minus S&P 500".

This module does NO price arithmetic of its own: every row is produced by calling
_tradable_grade_row with a call that has no limit, stop or target. Splits, half-days,
holidays, missing bars and "never guess" behave exactly as they do for the director.

Dividends (2026-11-16, docs/fable/DIVIDENDS_FOR_NICO.md). Whoever holds a stock or the S&P 500
fund when an ex-dividend date opens is paid that dividend in cash. After grading, add_dividends()
gives each graded row div_pct (the stock's dividends with entry day < ex-date <= exit day, as a
percent of the entry price), spy_div_pct (the S&P 500 fund's, over the same days) and div_status
(counted | none | unknown). The price columns are NOT changed; a reader adds the dividends (a long
earns them, a short owes them: mason_scoreboard does). Records come from Mason's own price cache
(price_bars_daily carries the free source's close and dividend for each day) and, for the S&P 500
fund and Mason's own stocks only, from ONE free request per ticker per night (yfinance, kept in this
module's cache file). No record for either side = 'unknown': the price result stands as before, and
the row is looked at again each night until div_recheck_days after its exit. Switch:
learning.grader.dividends (off = nothing is looked up and the scoreboard ignores the columns).

Prices (free only): what Mason already holds first (price_bars_daily, watch_bars, the
regrade bar cache — all read-only), then Alpaca's free historical bars through the
existing mason_watch_tape._alpaca_data_get hook, 100 symbols a call. Everything fetched
is kept in a separate small SQLite file outside the repo (learning.grader.cache_path,
default ~/.local/share/mason/learn/learn_prices.db). One item's window is always served
from ONE source so two split bases can never be mixed.

When an item "existed" (the entry is never earlier than this; review 2026-11-15):
  catalyst       the latest of: newest headline stored, last alert, last price reading, the
                 row's own creation, real arrival of a read-back headline; plus catalyst_lag_s
                 (the scoring pass runs up to ~3 minutes after the headline is stored). A
                 catalyst row is the story as it stood at its LAST update, taken in once quiet
                 for a day: it describes the end of a story, not the moment it first scored.
  news_alert     the alert's own time; a direction only if the story did not change afterwards
  scanner_hit    the latest of first poll / confirmation / local-model triage
  quick_check    start of the check plus the model's answer time
  everything else  the row's own creation time
What is and is not "known at the time" among the feature columns:
  point-in-time   direction, score, confidence, label, book (set when the news was stored),
                  regime (built only from closes up to the day before, although Mason itself
                  stores that label about a day later), days_to_earnings (only report dates
                  Mason had already stored), features_json keys without the prefix below
  does not change sector
  NOT known then  every features_json key starting with `later_` (what became of the item:
                  status, whether it was traded, order state). Never use these as inputs.

Writes: ONLY the `learn_outcomes` table (and its own cache file). Never recommendations,
agent_trades or any other existing table. Short transactions; nothing is held open while
a price is being fetched.

Public API (none of these raise):
    tick(now=None)                 called once an hour by mason_maintenance after the
                                   backup step; runs at most once a night, 03:15-06:15 ET.
                                   It only takes in the last `lookback_days` (21): the whole
                                   history is taken in by hand with --backfill, never here.
    run(...)                       one pass: take in new items, fetch, grade what is due
    status()                       row counts + what the last pass did
Manual:
    python3 mason_learn.py --backfill [--db COPY.db] [--cache FILE] [--no-network]
    python3 mason_learn.py --once     [--db COPY.db]      one ordinary pass, as the nightly job does
    python3 mason_learn.py --status   [--db COPY.db]

Switch: learning.grader.enabled (on when absent). See _DEFAULTS for every setting.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import mason_db

log = logging.getLogger("mason.learn")

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "mason.config.json"
GRADER_VERSION = 1
_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# What the free price source accepts as a US stock symbol (letters, optional ".B" class). Anything
# else (digits, hyphens: foreign listings) is never sent: one such symbol makes it reject the whole
# request. Measured 2026-11-15: BAJAJ-AUTO, IR5B, HEN3 -> "invalid symbol"; WPTG.B, SRV.UN -> accepted.
_FETCH_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,3})?$")
_BAD_SYM_RE = re.compile(r"invalid symbol:\s*([A-Za-z0-9.\-]+)")
_REGRADE_CACHE = Path.home() / ".local" / "share" / "mason" / "regrade" / "regrade_bar_cache.json"

_DEFAULTS = {
    "enabled": True,                 # the nightly grader (reads prices, writes its own table)
    "time_et": "03:15",              # earliest start, after the 02:30 database backup
    "window_hours": 3.0,             # no nightly work after time_et + this: done by 06:15, before the 07:00 director run
    "tick_budget_s": 240,            # most seconds one hourly pass may spend
    "max_fetch_calls_per_night": 150,  # outside price requests per night (the backfill ignores it)
    "fetch_pause_s": 0.35,           # pause between two outside requests
    "horizons": [1, 3, 5, 10],       # trading days
    "lookback_days": 21,             # the nightly pass only takes in items newer than this. Older history is
                                     # taken in ONLY by the by-hand `--backfill`, never inside the service.
    "catalyst_settle_hours": 24,     # a catalyst is taken in once it has been quiet this long
    "catalyst_lag_s": 180,           # a catalyst row is written by the triage pass up to ~3 minutes after its
                                     # newest headline was stored (measured: 97% within 180 s); the entry waits this long
    "max_attempts": 3,               # nights a row with a missing price is retried before it is final
    "retry_hours": 20,               # least time between two attempts on the same row
    "refetch_days": 7,               # a symbol with no data is not asked for again for this long
    "cache_path": "",                # "" = ~/.local/share/mason/learn/learn_prices.db
    "sources": {"catalyst": True, "alert": True, "director": True, "local": True, "dossier": True,
                "scanner": True, "local_read": True, "quick_check": True},
    "dividends": True,               # count cash dividends paid while held, on both sides (the scoreboard reads it too)
    "div_max_fetch": 40,             # most dividend-history requests to the free source in one pass (only the S&P 500
                                     # fund and Mason's own stocks are ever asked for; each at most once a night)
    "div_recheck_days": 30,          # a row whose dividends were unknown is looked at again until this long after its exit
    "option_facts": False,           # Job C (2026-11-17): implied move / volatility on record AT the item's time, from
                                     # the stored option snapshots, into features_json (add_option_facts; no network).
                                     # Off by default; once on, rows from the first snapshot onward are filled in.
    "option_facts_max_age_hours": 96,  # a snapshot older than this at the item's time is not used (covers a long weekend)
}

_SECTOR_OF_ETF = {"XLK": "Technology", "XLF": "Financial Services", "XLV": "Healthcare",
                  "XLE": "Energy", "XLI": "Industrials", "XLY": "Consumer Cyclical",
                  "XLP": "Consumer Defensive", "XLU": "Utilities", "XLB": "Basic Materials",
                  "XLRE": "Real Estate", "XLC": "Communication Services"}
_EXACT_RULES = ("decision_quote", "decision_minute")

_state = {"last_error": None, "last_run": None}


# ── settings ─────────────────────────────────────────────────────────────────────

def _config_block(name: str) -> dict:
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8")).get("learning") or {}
        blk = raw.get(name) if isinstance(raw, dict) else None
        return blk if isinstance(blk, dict) else {}
    except Exception:
        return {}


def cfg() -> dict:
    """learning.grader over the defaults, read on each call (no restart needed)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    for k, v in _config_block("grader").items():
        if str(k).startswith("_"):
            continue
        if k == "sources" and isinstance(v, dict):
            out["sources"].update(v)
        else:
            out[k] = v
    try:
        hz = sorted({int(h) for h in out.get("horizons") or [] if 1 <= int(h) <= 60})
    except (TypeError, ValueError):
        hz = []
    out["horizons"] = hz or list(_DEFAULTS["horizons"])
    return out


def cache_path(c: dict | None = None) -> Path:
    p = str((c or cfg()).get("cache_path") or "").strip()
    return Path(p).expanduser() if p else Path.home() / ".local" / "share" / "mason" / "learn" / "learn_prices.db"


def _PP():
    import mason_desk_pipeline
    return mason_desk_pipeline


def _grade_opts(c: dict) -> dict:
    """The director's own tradable-grade settings (cost, entry delay, half-days, benchmark),
    with the session map replaced by this module's fixed horizons."""
    PP = _PP()
    try:
        desk = PP._desk(None)
    except Exception:
        desk = {}
    o = dict(PP._tradable_opts(desk))
    o["smap"] = {int(h): int(h) for h in c["horizons"]}
    return o


def _log_once(msg: str) -> None:
    """A failure is logged once; the same message again stays quiet."""
    if _state.get("last_error") != msg:
        _state["last_error"] = msg
        log.warning("[learn] %s", msg)


# ── time helpers (all session math comes from mason_desk_pipeline) ───────────────

def _aware(v):
    """Epoch seconds, or an ISO string (naive = this machine's local time) → aware datetime."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), timezone.utc)
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


def _time_of_day(et: datetime, kind: str) -> str:
    if kind == "pre_open":
        return "before the open"
    if kind == "after_close":
        return "after the close"
    if kind == "non_session":
        return "weekend or holiday"
    m = et.hour * 60 + et.minute
    if m < 10 * 60 + 30:
        return "first hour"
    return "midday" if m < 14 * 60 else "last two hours"


def _open_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=_PP()._et_tz()).timestamp())


def _plan(created: datetime, horizon: int, hol, half, delay_min: float):
    """(entry session, kind, horizon session, t0) exactly as _tradable_grade_row derives them."""
    PP = _PP()
    es, kind = PP._entry_session(created, hol, half)
    xd = PP._nth_session_after(PP._session0(created, hol), int(horizon), hol)
    if xd < es:
        xd = es
    et = PP._to_et(created)
    t0 = int(et.timestamp()) if kind == "intraday" else _open_ts(es) + int(delay_min * 60)
    return es, kind, xd, t0


def _last_closed_session(now_et: datetime, hol) -> date:
    PP = _PP()
    d = now_et.date()
    while not (PP._is_session_day(d, hol) and PP._session_closed(d, now_et)):
        d -= timedelta(days=1)
    return d


# ── prices ───────────────────────────────────────────────────────────────────────

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (ticker TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL,
    fetched_at REAL, PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS daily_tried (ticker TEXT PRIMARY KEY, lo TEXT, hi TEXT, tried_at REAL, n INTEGER);
CREATE TABLE IF NOT EXISTS intraday (ticker TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL,
    sec INTEGER, fetched_at REAL, PRIMARY KEY (ticker, ts));
CREATE TABLE IF NOT EXISTS minute_tried (ticker TEXT, t0 INTEGER, tried_at REAL, bar_ts INTEGER,
    PRIMARY KEY (ticker, t0));
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bad_symbol (ticker TEXT PRIMARY KEY, noted_at REAL);
CREATE TABLE IF NOT EXISTS div_events (ticker TEXT, ex_date TEXT, amount REAL, prev_close REAL, fetched_at REAL,
    PRIMARY KEY (ticker, ex_date));
CREATE TABLE IF NOT EXISTS div_known (ticker TEXT PRIMARY KEY, lo TEXT, hi TEXT, tried_at REAL, n INTEGER);
"""


class Prices:
    """Daily bars and entry-time prices for the grader. Reads Mason's own tables through a
    read-only handle; fetches what is missing (when `network`) and keeps it in the cache
    file. `calls` counts every outside request. Never raises out of its public methods."""

    def __init__(self, path, *, db_path=None, network=True, deadline=None, max_calls=None,
                 pause_s=0.35, refetch_days=7.0, now=None, fetch=None, benchmark="SPY", readonly=False,
                 div_fetch=None):
        self.path = Path(path)
        self.readonly = bool(readonly)
        if self.readonly:                        # a reader never creates or changes the cache file
            network = False
            if self.path.exists():
                self.c = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=10)
            else:
                self.c = sqlite3.connect(":memory:")
                self.c.executescript(_CACHE_SCHEMA)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.c = sqlite3.connect(str(self.path), timeout=10)
            self.c.executescript(_CACHE_SCHEMA)
            self.c.commit()
        self.c.row_factory = sqlite3.Row
        self.db_path = str(db_path or mason_db.DB_FILE)
        self.network = bool(network)
        self.deadline = deadline                 # time.monotonic() value, or None
        self.max_calls = max_calls               # None = no cap
        self.pause_s = float(pause_s or 0)
        self.refetch_s = float(refetch_days) * 86400
        self.now = float(now if now is not None else time.time())
        self.fetch = fetch                       # tests inject fetch(path, params) -> dict | None
        self.div_fetch = div_fetch               # tests inject div_fetch(ticker, start) -> [{date, close, div}]
        # dividend records are fetched only with network on, and never by a test that injected a fake
        # price source without a fake dividend source
        self.div_network = bool(network) and (div_fetch is not None or fetch is None)
        self.div_calls = 0
        self._pdiv = {}                          # ticker -> {day: (close, dividend)} from Mason's price cache
        self.benchmark = benchmark
        self.calls = 0
        self.bars_in = 0
        self.stopped = None                      # why fetching stopped early, if it did
        self._fails = 0
        self._mem = {}                           # ticker -> {"pbd": {...}, "own": {...}, "rg": {...}}
        self._rg = None
        self._hol = _PP()._nyse_holidays()
        self._ro = None
        try:                                     # symbols the price source has refused before
            self._bad = {r[0] for r in self.c.execute("SELECT ticker FROM bad_symbol").fetchall()}
        except sqlite3.Error:
            self._bad = set()
        self._sess = {}                          # (first day, last day) -> [session days]
        self._lm = {}                            # (ticker, day) -> Mason's own minute bars

    # -- handles -------------------------------------------------------------
    def _main(self):
        if self._ro is None:
            self._ro = sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True, timeout=10)
            self._ro.row_factory = sqlite3.Row
        return self._ro

    def close(self):
        for h in (self._ro, self.c):
            try:
                if h is not None:
                    h.close()
            except Exception:
                pass
        self._ro = None

    def state_get(self, key, default=None):
        try:
            r = self.c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(r["value"]) if r else default
        except Exception:
            return default

    def state_set(self, key, value) -> None:
        if self.readonly:
            return
        try:
            with self.c:
                self.c.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?,?)",
                               (key, json.dumps(value, default=str)))
        except Exception:
            pass

    # -- daily ---------------------------------------------------------------
    def _regrade_cache(self) -> dict:
        if self._rg is None:
            try:
                self._rg = dict((json.loads(_REGRADE_CACHE.read_text(encoding="utf-8")) or {}).get("daily") or {})
            except Exception:
                self._rg = {}
        return self._rg

    def _load(self, t: str) -> dict:
        m = self._mem.get(t)
        if m is not None:
            return m
        PP = _PP()
        m = {"pbd": {}, "own": {}, "rg": {}}
        lo = (datetime.fromtimestamp(self.now) - timedelta(days=400)).date().isoformat()
        try:
            for r in self._main().execute(
                    "SELECT date, open, high, low, close FROM price_bars_daily WHERE ticker=? AND date>=? "
                    "AND COALESCE(xcheck_status,'')<>'not_found'", (t, lo)).fetchall():
                b = PP._ohlc(r["open"], r["high"], r["low"], r["close"])
                if b:
                    m["pbd"][str(r["date"])[:10]] = b
        except sqlite3.Error:
            pass
        try:
            for r in self.c.execute("SELECT date, o, h, l, c FROM daily WHERE ticker=?", (t,)).fetchall():
                b = PP._ohlc(r["o"], r["h"], r["l"], r["c"])
                if b:
                    m["own"][r["date"]] = b
        except sqlite3.Error:
            pass
        for k, b in (self._regrade_cache().get(t) or {}).items():
            if isinstance(b, dict):
                bb = PP._ohlc(b.get("o"), b.get("h"), b.get("l"), b.get("c"))
                if bb:
                    m["rg"][str(k)[:10]] = bb
        self._mem[t] = m
        return m

    def _sessions(self, s: date, e: date) -> list:
        out = self._sess.get((s, e))
        if out is None:
            PP = _PP()
            out, d = [], s
            while d <= e:
                if PP._is_session_day(d, self._hol):
                    out.append(d.isoformat())
                d += timedelta(days=1)
            self._sess[(s, e)] = out
        return out

    def _source_for(self, t: str, s: date, e: date):
        """Name of the ONE source that holds every session of [s, e], else None."""
        m = self._load(t)
        days = self._sessions(s, e)
        for src in ("pbd", "own", "rg"):
            if days and all(d in m[src] for d in days):
                return src
        return None

    def daily(self, t, s: date, e: date) -> dict:
        """{YYYY-MM-DD: bar} for [s, e] from one source only (the grader's daily_fn)."""
        t = str(t).upper()
        m = self._load(t)
        src = self._source_for(t, s, e)
        if src is None:                          # incomplete: hand back one source so the grader names the gap
            src = "own" if any(s.isoformat() <= k <= e.isoformat() for k in m["own"]) else "pbd"
        return {k: b for k, b in m[src].items() if s.isoformat() <= k <= e.isoformat()}

    def prev_close(self, t, s: date, e: date):
        """Close of the last session before `s`, from the same source that serves [s, e]."""
        t = str(t).upper()
        src = self._source_for(t, s, e)
        if src is None:
            return None
        lo = (s - timedelta(days=10)).isoformat()
        prior = [k for k in self._load(t)[src] if lo <= k < s.isoformat()]
        return self._load(t)[src][max(prior)]["c"] if prior else None

    def daily_ready(self, t, s: date, e: date) -> bool:
        """True when [s, e] can be answered now: covered, or already asked for and not there."""
        t = str(t).upper()
        if self._source_for(t, s, e) is not None or not self.network:
            return True
        r = self.c.execute("SELECT lo, hi, tried_at FROM daily_tried WHERE ticker=?", (t,)).fetchone()
        return bool(r and r["lo"] <= s.isoformat() and r["hi"] >= e.isoformat()
                    and self.now - float(r["tried_at"] or 0) < self.refetch_s)

    # -- dividends (2026-11-16) ------------------------------------------------
    def _pc_divs(self, t: str) -> dict:
        """{day: (close, dividend)} from the rows of Mason's price cache that carry the free source's close:
        only such a row says whether there was a dividend that day (0.0 = none). Read-only, once per ticker."""
        got = self._pdiv.get(t)
        if got is None:
            got = {}
            lo = (datetime.fromtimestamp(self.now) - timedelta(days=400)).date().isoformat()
            try:
                for r in self._main().execute(
                        "SELECT date, json_extract(xcheck_json, '$.yf_close') yc, json_extract(xcheck_json, '$.div') dv "
                        "FROM price_bars_daily WHERE ticker=? AND date>=? AND xcheck_json LIKE '%yf_close%'",
                        (t, lo)).fetchall():
                    yc = _num(r["yc"])
                    if yc and yc > 0:
                        got[str(r["date"])[:10]] = (yc, _num(r["dv"]) or 0.0)
            except sqlite3.Error:
                pass
            self._pdiv[t] = got
        return got

    def dividends(self, t, after: date, through: date):
        """Cash dividends of `t` with after < ex-date <= through: [(ex-date, amount per share, share of the
        previous close)], [] when there were none, None when NOT KNOWN (no record covers every day). The share
        of the previous close comes from one source, so it holds whatever split basis a price is on.
        Bought on the ex-date = not paid (the stock already trades without it); sold on it = paid."""
        t = str(t).upper()
        PP = _PP()
        d0 = after
        while not PP._is_session_day(d0, self._hol):
            d0 -= timedelta(days=1)
        a, z = after.isoformat(), through.isoformat()
        days = self._sessions(d0, through)
        if not any(d > a for d in days):
            return []                             # no trading day in the window: nothing can have been paid
        try:                                      # 1) the free source's history, fetched at night
            k = self.c.execute("SELECT lo, hi FROM div_known WHERE ticker=?", (t,)).fetchone()
            if k is not None and k["lo"] and k["hi"] and k["lo"] <= a and k["hi"] >= z:
                return [(r["ex_date"], float(r["amount"]), float(r["amount"]) / float(r["prev_close"]))
                        for r in self.c.execute(
                            "SELECT ex_date, amount, prev_close FROM div_events WHERE ticker=? AND ex_date>? "
                            "AND ex_date<=? AND amount>0 AND prev_close>amount ORDER BY ex_date", (t, a, z)).fetchall()]
        except sqlite3.Error:
            pass                                  # an older cache file without these tables: Mason's own record only
        pd = self._pc_divs(t)                     # 2) Mason's own price cache, every day of the window
        if not all(d in pd for d in days):
            return None
        out = []
        for i, d in enumerate(days):
            if d > a and pd[d][1] > 0:
                prev = pd[days[i - 1]][0]
                if not prev > pd[d][1]:
                    return None
                out.append((d, pd[d][1], pd[d][1] / prev))
        return out

    def fetch_dividends(self, needs: dict, max_calls: int = 40) -> int:
        """needs = {ticker: (after, through)}, most important first. A ticker whose dividends over its window
        are not known yet gets ONE request to the free source (_div_history): its daily history from 10 days
        before `after` to today. Never twice in 20 hours for one ticker, at most `max_calls` a pass, nothing
        past the deadline. A failed request is remembered as tried and changes nothing else. Returns the
        number of requests. Never raises."""
        if not self.div_network:
            return 0
        n = 0
        try:
            last = _last_closed_session(datetime.fromtimestamp(self.now, _PP()._et_tz()), self._hol).isoformat()
            for t, (a, z) in needs.items():
                t = str(t).upper()
                if n >= int(max_calls or 0) or (self.deadline is not None and time.monotonic() >= self.deadline):
                    break
                if not self.fetchable(t) or self.dividends(t, a, z) is not None:
                    continue
                r = self.c.execute("SELECT tried_at FROM div_known WHERE ticker=?", (t,)).fetchone()
                if r is not None and self.now - float(r["tried_at"] or 0) < 20 * 3600:
                    continue                      # asked already tonight
                if n and self.pause_s > 0 and self.div_fetch is None:
                    time.sleep(self.pause_s)      # the same pause as between price requests (review 2026-11-16)
                n += 1
                self.div_calls += 1
                try:
                    rows = (self.div_fetch or _div_history)(t, a - timedelta(days=10))
                except Exception as e:
                    rows = None
                    log.debug("[learn] dividend request for %s failed: %s", t, e)
                self._keep_dividends(t, rows, last)
        except Exception as e:
            _log_once(f"dividend records not fetched: {type(e).__name__}: {e}")
        return n

    def _keep_dividends(self, t: str, rows, last: str) -> None:
        """Store one answer: every dividend in it with the close of the day before, and the span it is
        complete for (its first day to its last closed day). Nothing usable: only the time of the try."""
        rows = sorted((x for x in (rows or []) if isinstance(x, dict) and str(x.get("date") or "")[:10]
                       and str(x["date"])[:10] <= last and (_num(x.get("close")) or 0) > 0),
                      key=lambda x: str(x["date"])[:10])
        with self.c:
            if len(rows) < 2:
                self.c.execute("INSERT INTO div_known(ticker, tried_at, n) VALUES (?,?,0) "
                               "ON CONFLICT(ticker) DO UPDATE SET tried_at=excluded.tried_at", (t, self.now))
                return
            day = [str(x["date"])[:10] for x in rows]
            ev = []
            for i in range(1, len(rows)):
                dv, prev = _num(rows[i].get("div")) or 0.0, _num(rows[i - 1]["close"])
                if dv > 0 and prev and prev > dv:
                    ev.append((t, day[i], dv, prev, self.now))
            lo = day[0] if not (_num(rows[0].get("div")) or 0) > 0 else day[1]
            old = self.c.execute("SELECT lo, hi FROM div_known WHERE ticker=?", (t,)).fetchone()
            if old is not None and old["lo"] and old["hi"] and old["lo"] < lo <= old["hi"]:
                lo = old["lo"]                    # overlaps what was known before: keep the older part
            # an answer cannot measure a dividend on its own first day (no close before it): keep what an
            # earlier, longer answer stored for that day (review 2026-11-16: >= here lost it while `lo` kept
            # the older span, so the dividend silently read as "none paid")
            self.c.execute("DELETE FROM div_events WHERE ticker=? AND ex_date>? AND ex_date<=?", (t, day[0], day[-1]))
            self.c.executemany("INSERT OR REPLACE INTO div_events(ticker, ex_date, amount, prev_close, fetched_at) "
                               "VALUES (?,?,?,?,?)", ev)
            self.c.execute("INSERT OR REPLACE INTO div_known(ticker, lo, hi, tried_at, n) VALUES (?,?,?,?,?)",
                           (t, lo, day[-1], self.now, len(ev)))

    # -- entry-time prices -----------------------------------------------------
    def _local_minutes(self, t, d: date) -> list:
        got = self._lm.get((t, d))
        if got is None:
            try:
                got = _PP()._default_minute_bars(t, d, conn=self._main())
            except Exception:
                got = []
            self._lm[(t, d)] = got
        return got

    def minutes(self, t, d: date) -> list:
        """Minute bars for one session (the grader's minute_fn): Mason's own tape, with any
        hole in it filled from the entry-time bars kept in the cache (the tape wins where
        both hold the same minute)."""
        t = str(t).upper()
        local = self._local_minutes(t, d)
        o = _open_ts(d)
        try:
            own = [dict(ts=int(r["ts"]), o=r["o"], h=r["h"], l=r["l"], c=r["c"], sec=int(r["sec"] or 300))
                   for r in self.c.execute("SELECT ts, o, h, l, c, sec FROM intraday WHERE ticker=? AND ts>=? "
                                           "AND ts<? ORDER BY ts", (t, o, o + 390 * 60)).fetchall()]
        except sqlite3.Error:
            own = []
        if not own or not local:
            return own or local
        both = {b["ts"]: b for b in own}
        both.update({b["ts"]: b for b in local})
        return [both[k] for k in sorted(both)]

    def minute_ready(self, t, d: date, t0: int) -> bool:
        t = str(t).upper()
        if not self.network:
            return True
        if any(t0 <= b["ts"] < t0 + 600 for b in self._local_minutes(t, d)):
            return True                           # Mason's own tape has the minute that is needed
        key = _open_ts(d) if t == self.benchmark else int(t0)
        return self.c.execute("SELECT 1 FROM minute_tried WHERE ticker=? AND t0=?", (t, key)).fetchone() is not None

    # -- outside requests --------------------------------------------------------
    def can_call(self) -> bool:
        if not self.network or self.stopped:
            return False
        if self.max_calls is not None and self.calls >= int(self.max_calls):
            self.stopped = "nightly request limit reached"
            return False
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.stopped = "time budget used up"
            return False
        return True

    def fetchable(self, t: str) -> bool:
        return bool(_FETCH_RE.match(t)) and t not in self._bad

    def _note_bad(self, t: str) -> None:
        self._bad.add(t)
        try:
            with self.c:
                self.c.execute("INSERT OR REPLACE INTO bad_symbol(ticker, noted_at) VALUES (?,?)", (t, self.now))
        except sqlite3.Error:
            pass

    def _get(self, params: dict):
        """One request through Mason's existing Alpaca data hook (10 s timeout there)."""
        self.calls += 1
        try:
            if self.fetch is not None:
                j = self.fetch("/v2/stocks/bars", dict(params))
            else:
                import mason_watch_tape as WT
                j = WT._alpaca_data_get("/v2/stocks/bars", dict(params))
        except Exception as e:
            j = None
            log.debug("[learn] price request failed: %s", e)
        if self.pause_s > 0 and self.fetch is None:
            time.sleep(self.pause_s)
        return j

    def _bars(self, symbols: list, start_ts: int, end_ts: int, timeframe: str):
        """({symbol: [raw bars]}, ok) for one batch. A symbol the source names as invalid is
        dropped, remembered (never sent again) and the batch is asked for once more; only an
        unexplained rejection splits the batch in two."""
        out = {}
        symbols = list(symbols)
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "feed": "sip", "adjustment": "split",
                  "start": _iso(start_ts), "end": _iso(end_ts), "limit": 10000}
        for _ in range(60):
            if not self.can_call():
                return out, False
            j = self._get(params)
            if not isinstance(j, dict):
                self._fails += 1
                if self._fails >= 3:
                    self.stopped = "price source not answering"
                return out, False
            code = j.get("_http_status")
            if code:
                named = _BAD_SYM_RE.search(str(j.get("message") or "")) if code in (400, 422) else None
                bad = named.group(1).upper() if named else None
                if bad and bad in symbols:                         # the source says which one it will not take
                    self._note_bad(bad)
                    symbols = [x for x in symbols if x != bad]
                    if not symbols:
                        return {}, True
                    params["symbols"] = ",".join(symbols)
                    params.pop("page_token", None)
                    out = {}
                    continue
                if code in (400, 422) and len(symbols) > 1:        # rejected without saying why: split
                    half = len(symbols) // 2
                    a, ok_a = self._bars(symbols[:half], start_ts, end_ts, timeframe)
                    b, ok_b = self._bars(symbols[half:], start_ts, end_ts, timeframe)
                    a.update(b)
                    return a, ok_a and ok_b
                if code in (400, 422):
                    return {}, True                                # this one symbol is not served
                self.stopped = ("price source refused the request" if code in (401, 403)
                                else "price source is rate-limiting" if code == 429
                                else f"price source answered {code}")
                return out, False
            self._fails = 0
            for sym, lst in (j.get("bars") or {}).items():
                out.setdefault(str(sym).upper(), []).extend(lst or [])
                self.bars_in += len(lst or [])
            tok = j.get("next_page_token")
            if not tok:
                return out, True
            params["page_token"] = tok
        return out, False

    def fetch_daily(self, needs: dict) -> int:
        """needs = {ticker: (first day, last day)}. Fetches, 100 symbols a request, every
        ticker whose window is neither held nor already asked for. Returns tickers fetched."""
        PP = _PP()
        todo = sorted((t for t, (s, e) in needs.items() if _SYM_RE.match(t) and not self.daily_ready(t, s, e)),
                      key=lambda t: (needs[t][0], t))
        skip = [t for t in todo if not self.fetchable(t)]
        if skip and self.network:                 # not a symbol the source serves: settled without a request
            with self.c:
                self.c.executemany("INSERT OR REPLACE INTO daily_tried(ticker, lo, hi, tried_at, n) VALUES (?,?,?,?,0)",
                                   [(t, (needs[t][0] - timedelta(days=7)).isoformat(), needs[t][1].isoformat(),
                                     self.now) for t in skip])
        todo = [t for t in todo if self.fetchable(t)]
        done = 0
        for i in range(0, len(todo), 100):
            if not self.can_call():
                break
            chunk = todo[i:i + 100]
            lo = min(needs[t][0] for t in chunk) - timedelta(days=7)
            hi = max(needs[t][1] for t in chunk)
            tz = PP._et_tz()
            start = int(datetime(lo.year, lo.month, lo.day, tzinfo=tz).timestamp())
            end = min(int(datetime(hi.year, hi.month, hi.day, tzinfo=tz).timestamp()) + 86400,
                      int(self.now) - 16 * 60)
            got, ok = self._bars(chunk, start, end, "1Day")
            if not ok:
                break
            rows, tried = [], []
            for t in chunk:
                n = 0
                for b in got.get(t) or []:
                    ts = _parse_ts(b.get("t"))
                    bar = PP._ohlc(b.get("o"), b.get("h"), b.get("l"), b.get("c"))
                    if ts is None or not bar:
                        continue
                    d = datetime.fromtimestamp(ts, tz).date().isoformat()
                    if lo.isoformat() <= d <= hi.isoformat():
                        rows.append((t, d, bar["o"], bar["h"], bar["l"], bar["c"], self.now))
                        n += 1
                tried.append((t, lo.isoformat(), hi.isoformat(), self.now, n))
                self._mem.pop(t, None)
            with self.c:
                self.c.executemany("INSERT OR REPLACE INTO daily(ticker, date, o, h, l, c, fetched_at) "
                                   "VALUES (?,?,?,?,?,?,?)", rows)
                self.c.executemany("INSERT OR REPLACE INTO daily_tried(ticker, lo, hi, tried_at, n) "
                                   "VALUES (?,?,?,?,?)", tried)
            done += len(chunk)
        return done

    def fetch_minutes(self, needs: dict) -> int:
        """needs = {session date: {ticker: {t0, ...}}}. Per day, 100 symbols a request
        (5-minute bars; the source answers a long request in pages of about 2,500 bars and
        every page counts as one request). Only the bar each t0 needs is kept — the first
        one starting at or after t0, within ten minutes; the benchmark keeps the whole
        session. Returns ticker-days fetched."""
        PP = _PP()
        half = PP._NYSE_HALF_DAYS
        done = 0
        for d in sorted(needs):
            per = {t: set(v) for t, v in needs[d].items()
                   if _SYM_RE.match(t) and any(not self.minute_ready(t, d, x) for x in v)}
            o = _open_ts(d)
            close = o + (PP._close_minutes(d, half) - 570) * 60
            if close > self.now - 16 * 60:
                continue
            skip = [(t, int(x), self.now, None) for t in per if not self.fetchable(t) for x in per[t]]
            if skip and self.network:
                with self.c:
                    self.c.executemany("INSERT OR REPLACE INTO minute_tried(ticker, t0, tried_at, bar_ts) "
                                       "VALUES (?,?,?,?)", skip)
            todo = sorted(t for t in per if self.fetchable(t))
            # Symbols that only need the price just after the open are asked for the first half
            # hour only (one short answer per 100 symbols); the rest get the whole session.
            early = [t for t in todo if t != self.benchmark and max(per[t]) < o + 900]
            rest = [t for t in todo if t not in set(early)]
            batches = [(early[i:i + 100], min(o + 1800, close)) for i in range(0, len(early), 100)]
            batches += [(rest[i:i + 100], close) for i in range(0, len(rest), 100)]
            for chunk, until in batches:
                if not self.can_call():
                    return done
                got, ok = self._bars(chunk, o, until, "5Min")
                if not ok:
                    return done
                rows, tried = [], []
                for t in chunk:
                    bars = []
                    for b in got.get(t) or []:
                        ts = _parse_ts(b.get("t"))
                        bar = PP._ohlc(b.get("o"), b.get("h"), b.get("l"), b.get("c"))
                        if ts is not None and bar and o <= ts < close:
                            bars.append(dict(bar, ts=ts))
                    bars.sort(key=lambda b: b["ts"])
                    if t == self.benchmark:
                        keep = bars
                        tried.append((t, o, self.now, bars[0]["ts"] if bars else None))
                    else:
                        keep = []
                        for t0 in sorted(per[t]):
                            hit = next((b for b in bars if b["ts"] >= t0), None)
                            hit = hit if (hit and hit["ts"] < t0 + 600) else None
                            if hit and hit not in keep:
                                keep.append(hit)
                            tried.append((t, int(t0), self.now, hit["ts"] if hit else None))
                    rows += [(t, b["ts"], b["o"], b["h"], b["l"], b["c"], 300, self.now) for b in keep]
                with self.c:
                    self.c.executemany("INSERT OR REPLACE INTO intraday(ticker, ts, o, h, l, c, sec, fetched_at) "
                                       "VALUES (?,?,?,?,?,?,?,?)", rows)
                    self.c.executemany("INSERT OR REPLACE INTO minute_tried(ticker, t0, tried_at, bar_ts) "
                                       "VALUES (?,?,?,?)", tried)
                done += len(chunk)
        return done


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s):
    try:
        s = str(s).strip().replace("Z", "+00:00")
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
        if m:
            s = m.group(1) + m.group(3)
        dt = datetime.fromisoformat(s)
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        return None


def _div_history(ticker: str, start: date) -> list:
    """Network hook (tests pass Prices(div_fetch=...) instead): the free source's daily rows from `start`
    to today, [{"date", "close", "div"}], through mason_price_cache's own request and reader (no new price
    library here): `close` is the plain close (split-adjusted only), `div` the cash dividend per share on
    its ex-date, on the same basis. Raises on an outage, so a failure is never read as 'no dividends'."""
    import mason_price_cache as PC
    end = datetime.now(_PP()._et_tz()).date() + timedelta(days=1)
    df = PC._yf_history(PC._yf_symbol(str(ticker).upper()), start.isoformat(), end.isoformat())
    return [{"date": r["date"], "close": r["close"], "div": r["div"]} for r in PC._yf_rows(df)]


# ── the items ────────────────────────────────────────────────────────────────────

def _j(raw) -> dict:
    try:
        v = json.loads(raw) if raw else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _dir_of(sentiment):
    s = str(sentiment or "").strip().lower()
    return "long" if s in ("bullish", "long") else "short" if s in ("bearish", "short") else None


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        log.debug("[learn] source query skipped: %s", e)
        return []


def _news_book(conn, news_id):
    if not news_id:
        return None
    r = _rows(conn, "SELECT category FROM news_events WHERE id=?", (news_id,))
    cat = r[0]["category"] if r else None
    return "held" if cat == "holding" else "watched" if cat == "watchlist" else None


def _epoch(v):
    dt = _aware(v)
    return dt.timestamp() if dt is not None else None


def _backfill_arrivals(conn) -> dict:
    """{catalyst id: latest REAL arrival time of a headline that was read back after a stream
    disconnect}. Such a headline is stored under its publication time (news_events.ingested_at),
    which can be hours before Mason had it; raw.backfill.at is when it actually arrived."""
    out = {}
    for r in _rows(conn, "SELECT catalyst_id, MAX(CAST(json_extract(raw,'$.backfill.at') AS REAL)) at FROM news_events "
                         "WHERE catalyst_id IS NOT NULL AND raw LIKE '%\"backfill\"%' GROUP BY catalyst_id"):
        if _num(r["at"]):
            out[r["catalyst_id"]] = float(r["at"])
    return out


def _items_catalysts(conn, since_ts, now, c, have=()):
    settle = now - float(c.get("catalyst_settle_hours") or 0) * 3600
    lag = max(0.0, float(c.get("catalyst_lag_s") or 0))
    arrived = _backfill_arrivals(conn)
    for r in _rows(conn, "SELECT id, primary_ticker, event_kind, first_seen, last_seen, alerted_at, n_events, "
                         "n_sources, velocity, market_materiality, direction, tape_json, score, status, extra_json, "
                         "created_date FROM catalysts WHERE COALESCE(record_deleted,0)=0 AND primary_ticker IS NOT NULL "
                         "AND COALESCE(last_seen, first_seen) >= ?", (since_ts,)):
        if r["id"] in have:
            continue
        ex, tape = _j(r["extra_json"]), _j(r["tape_json"])
        # The row as stored was last changed no earlier than the latest of these: the time its newest
        # headline was stored, its last alert, its last price reading, the moment the row itself was
        # first written (a story rebuilt later from old headlines did not exist before that) and the
        # real arrival of a headline read back after a disconnect. first_seen/last_seen are the times
        # the headlines were STORED; the triage pass that scores the story runs up to a few minutes
        # later and leaves no timestamp, so `catalyst_lag_s` is added. The entry comes after all of it.
        seen = max([x for x in (_num(r["first_seen"]), _num(r["last_seen"]), _num(r["alerted_at"]),
                                _num(ex.get("_tape_fetch_ts")), _epoch(r["created_date"]),
                                arrived.get(r["id"])) if x] or [0])
        if not seen or seen > settle:
            continue
        ts = seen + lag
        d = _dir_of(r["direction"])
        parts = ex.get("score_parts") if isinstance(ex.get("score_parts"), dict) else {}
        yield {"kind": "catalyst", "id": r["id"], "ticker": r["primary_ticker"], "ts": ts,
               "direction": d, "basis": "sentiment" if d else "none", "label": r["event_kind"],
               "score": _num(r["score"]), "sector": _SECTOR_OF_ETF.get(str(tape.get("sector_etf") or "")),
               "book": _news_book(conn, ex.get("top_event_id")),
               "features": {"stated": r["direction"], "n_events": r["n_events"], "n_sources": r["n_sources"],
                            "velocity": _num(r["velocity"]), "materiality": _num(r["market_materiality"]),
                            "alerted": 1 if r["alerted_at"] else 0, "later_status": r["status"],
                            "story_hours": round((seen - float(r["first_seen"] or seen)) / 3600.0, 1),
                            "tape_move_pct": _num(tape.get("move_pct")), "vs_sector_pct": _num(tape.get("vs_sector_pct")),
                            "rel_volume": _num(tape.get("rel_volume")), "tape_confirmed": tape.get("confirmed"),
                            "in_broad_universe": None if parts.get("universe_factor") is None
                            else int(float(parts.get("universe_factor") or 0) >= 1.0)}}


def _items_alerts(conn, since_ts, now, have=()):
    """One item per inbox alert raised on a catalyst, timed at the alert itself. Newer
    catalysts keep each alert with the score it had then (extra_json.alerts); older ones
    only kept the time of the last alert (alerted_at), so their score is left empty.
    The alert does not store the direction the story had when it was sent. The story's
    direction is used ONLY when no headline joined the story after the alert (then it is
    what the alert showed); otherwise the alert is recorded with no direction and the
    later direction is kept aside as `later_stated`, never traded on."""
    for r in _rows(conn, "SELECT id, primary_ticker, event_kind, direction, alerted_at, last_seen, tape_json, extra_json "
                         "FROM catalysts WHERE COALESCE(record_deleted,0)=0 AND primary_ticker IS NOT NULL "
                         "AND alerted_at IS NOT NULL AND alerted_at >= ?", (since_ts,)):
        ex = _j(r["extra_json"])
        al = [a for a in (ex.get("alerts") or []) if isinstance(a, dict) and _num(a.get("at"))]
        al = al or [{"at": r["alerted_at"]}]
        for a in al:
            ts = _num(a.get("at"))
            iid = f"{r['id']}@{int(ts)}"
            if iid in have or ts > now:
                continue
            unchanged = (_num(r["last_seen"]) or 0) <= ts + 1.0
            d = _dir_of(r["direction"]) if unchanged else None
            yield {"kind": "news_alert", "id": iid, "ticker": r["primary_ticker"], "ts": ts,
                   "direction": d, "basis": "sentiment" if d else "none", "label": r["event_kind"],
                   "score": _num(a.get("score")),
                   "sector": _SECTOR_OF_ETF.get(str(_j(r["tape_json"]).get("sector_etf") or "")),
                   "book": _news_book(conn, ex.get("top_event_id")),
                   "features": {"stated": r["direction"] if unchanged else None,
                                "later_stated": None if unchanged else r["direction"],
                                "why": a.get("why"), "catalyst_id": r["id"]}}


def _items_recs(conn, since_iso, c, have=()):
    src = c["sources"]
    sent = {}
    if src.get("local"):
        for r in _rows(conn, "SELECT news_id, ticker, sentiment FROM qwen_news_analysis WHERE analysis_type='ticker_deep' "
                             "AND COALESCE(record_deleted,0)=0 AND created_date>=? ORDER BY created_date", (since_iso,)):
            sent[(r["news_id"], str(r["ticker"] or "").upper())] = r["sentiment"]
    traded = {r["rec_id"] for r in _rows(conn, "SELECT rec_id FROM agent_trades WHERE rec_id IS NOT NULL")}
    cat_kind = {}
    for r in _rows(conn, "SELECT id, created_date, created_by, ticker, kind, direction, materiality, source_news_id, "
                         "status, entry_ref_price, extra_json FROM recommendations "
                         "WHERE COALESCE(record_deleted,0)=0 AND created_date>=?", (since_iso,)):
        if r["id"] in have:
            continue
        ex = _j(r["extra_json"])
        kind, direction = r["kind"], str(r["direction"] or "").lower()
        base = {"id": r["id"], "ticker": r["ticker"], "ts": r["created_date"], "model": r["created_by"],
                "ref_px": _num(r["entry_ref_price"])}
        if kind == "director" and src.get("director"):
            tr = ex.get("trade") if isinstance(ex.get("trade"), dict) else {}
            exe = ex.get("execution") if isinstance(ex.get("execution"), dict) else {}
            cid = ex.get("catalyst_id")
            if cid and cid not in cat_kind:
                k = _rows(conn, "SELECT event_kind, score FROM catalysts WHERE id=?", (cid,))
                cat_kind[cid] = (k[0]["event_kind"], _num(k[0]["score"])) if k else (None, None)
            feats = {"anchor": ex.get("anchor_kind"), "declared_horizon": ex.get("horizon"),
                     "declared_days": ex.get("horizon_days"), "vehicle": str(tr.get("vehicle") or "stock").lower(),
                     "expected_move_pct": _num(ex.get("exp_move_pct")), "later_rec_status": r["status"],
                     "later_traded": int(r["id"] in traded), "later_order_state": exe.get("state"),
                     "later_blocked_reason": (exe.get("blocked_reason") or exe.get("queue_block_reason") or None)}
            if direction in ("long", "short"):
                yield dict(base, kind="director_call", direction=direction, basis="own",
                           label=cat_kind.get(cid, (None, None))[0], confidence=_num(ex.get("confidence")),
                           features=dict(feats, stated=ex.get("sentiment")))
            elif direction == "watch":
                d = _dir_of(ex.get("sentiment"))
                yield dict(base, kind="director_watch", direction=d, basis="sentiment" if d else "none",
                           label=cat_kind.get(cid, (None, None))[0], confidence=_num(ex.get("confidence")),
                           ref_px=None, features=dict(feats, stated=ex.get("sentiment")))
        elif kind == "watch" and src.get("local"):
            s = sent.get((r["source_news_id"], str(r["ticker"] or "").upper()))
            d = _dir_of(s)
            yield dict(base, kind="local_watch", direction=d, basis="sentiment" if d else "none", label="watch",
                       score=_num(r["materiality"]), book=_news_book(conn, r["source_news_id"]), ref_px=None,
                       features={"stated": s, "later_rec_status": r["status"]})
        elif kind in ("manage_position", "new_idea") and direction in ("long", "short") and src.get("local"):
            yield dict(base, kind="local_call", direction=direction, basis="own", label=kind,
                       score=_num(r["materiality"]),
                       book="held" if kind == "manage_position" else _news_book(conn, r["source_news_id"]),
                       features={"later_rec_status": r["status"], "later_traded": int(r["id"] in traded)})


def _items_dossiers(conn, since_ts):
    for r in _rows(conn, "SELECT id, subject, built_at, json_extract(payload_json,'$.profile.sector') sector, "
                         "json_extract(payload_json,'$.profile.market_cap') cap, "
                         "json_extract(payload_json,'$.profile.short_pct') short_pct FROM dossiers "
                         "WHERE kind='ticker' AND COALESCE(record_deleted,0)=0 AND built_at>=?", (since_ts,)):
        yield {"kind": "dossier", "id": r["id"], "ticker": r["subject"], "ts": _num(r["built_at"]),
               "direction": None, "basis": "none", "label": "dossier", "sector": r["sector"],
               "features": {"market_cap": _num(r["cap"]), "short_pct": _num(r["short_pct"])}}


def _items_scanner(conn, since_iso):
    titles = {r["scan_id"]: r["title"] for r in _rows(conn, "SELECT scan_id, title FROM scanner_scans")}
    for r in _rows(conn, "SELECT id, scan_id, ticker, first_seen, created_date, triaged_at, first_price, triage_score, "
                         "watch_status, grades_json, "
                         "json_extract(first_row_json,'$.columns.\"% Change\"') chg FROM scanner_hits "
                         "WHERE COALESCE(record_deleted,0)=0 AND first_seen>=?", (since_iso,)):
        tri = _j(r["grades_json"]).get("triage")
        tri = tri if isinstance(tri, dict) else {}
        d = _dir_of(tri.get("direction"))
        chg = _num(r["chg"])
        # first_seen is the FIRST poll that showed the ticker; the hit only exists once later polls
        # confirm it (created_date, 1-10 minutes on), and its score and direction only once the
        # local model has looked at it (triaged_at). The item is timed at the latest of the three,
        # and the price stamped on the hit (taken before that) is kept aside, not used as the entry.
        ts = max([x for x in (_epoch(r["first_seen"]), _epoch(r["created_date"]), _epoch(r["triaged_at"])) if x] or [0])
        if not ts:
            continue
        yield {"kind": "scanner_hit", "id": r["id"], "ticker": r["ticker"], "ts": ts,
               "direction": d, "basis": "sentiment" if d else "none",
               "label": titles.get(r["scan_id"]) or r["scan_id"], "score": _num(r["triage_score"]),
               "ref_px": None, "model": "qwen triage",
               "features": {"stated": tri.get("direction"), "later_watch_status": r["watch_status"],
                            "first_seen": r["first_seen"], "price_on_hit": _num(r["first_price"]),
                            "day_change_pct_at_hit": None if chg is None else round(chg * 100.0, 2)}}


def _items_local_reads(conn, since_iso):
    for r in _rows(conn, "SELECT id, created_date, news_id, ticker, sentiment, materiality, model FROM qwen_news_analysis "
                         "WHERE analysis_type='ticker_deep' AND COALESCE(record_deleted,0)=0 AND ticker IS NOT NULL "
                         "AND created_date>=?", (since_iso,)):
        d = _dir_of(r["sentiment"])
        yield {"kind": "local_read", "id": r["id"], "ticker": r["ticker"], "ts": r["created_date"],
               "direction": d, "basis": "sentiment" if d else "none", "label": "news read",
               "score": _num(r["materiality"]), "model": r["model"], "book": _news_book(conn, r["news_id"]),
               "features": {"stated": r["sentiment"]}}


def _items_quick_checks(conn, since_ts):
    for r in _rows(conn, "SELECT id, created_epoch, seconds, trigger, ticker, holder, action, ref_px, model, output_json "
                         "FROM desk_checks WHERE COALESCE(record_deleted,0)=0 AND status IN ('done','decided') "
                         "AND ticker IS NOT NULL AND created_epoch>=?", (since_ts,)):
        d = None
        try:
            out = json.loads(r["output_json"] or "{}")
            for dec in (out.get("decisions") if isinstance(out, dict) else out) or []:
                if str(dec.get("ticker") or "").upper() == str(r["ticker"]).upper():
                    d = _dir_of(dec.get("direction"))
                    break
        except Exception:
            pass
        # created_epoch is when the check STARTED; its answer exists only after the model call
        # (`seconds` long). The price stamped at the start is kept aside, not used as the entry.
        start = _num(r["created_epoch"])
        if not start:
            continue
        yield {"kind": "quick_check", "id": r["id"], "ticker": r["ticker"],
               "ts": start + max(0.0, _num(r["seconds"]) or 0.0),
               "direction": d, "basis": "sentiment" if d else "none", "label": r["trigger"],
               "ref_px": None, "model": r["model"],
               "book": "held" if r["holder"] else None,
               "features": {"action": r["action"], "price_at_start": _num(r["ref_px"])}}


class _Context:
    """Look-ups shared by every item of one pass (regime, earnings dates, sector)."""

    def __init__(self, conn):
        self.reg = [(r["date"], r["label"]) for r in _rows(
            conn, "SELECT date, label FROM market_regimes WHERE date>=? ORDER BY date",
            ((date.today() - timedelta(days=500)).isoformat(),))]
        self.earn = {}                           # ticker -> [(report date, when Mason stored that date)]
        for r in _rows(conn, "SELECT ticker, report_date, created_date FROM earnings_results WHERE report_date IS NOT NULL"):
            self.earn.setdefault(str(r["ticker"]).upper(), []).append(
                (str(r["report_date"])[:10], _epoch(r["created_date"]) or 0.0))
        for v in self.earn.values():
            v.sort()
        self.sector = {}
        for r in _rows(conn, "SELECT subject, json_extract(payload_json,'$.profile.sector') s FROM dossiers "
                             "WHERE kind='ticker' AND COALESCE(record_deleted,0)=0 ORDER BY built_at"):
            if r["s"]:
                self.sector[str(r["subject"]).upper()] = r["s"]

    def regime(self, day: str):
        """The stored market regime of the last day BEFORE the item's day (nothing from the day itself)."""
        lab = None
        for d, l in self.reg:
            if d >= day:
                break
            lab = l
        return lab

    def earnings_days(self, t: str, day: str, known_by: float | None = None):
        """Calendar days to the next earnings report (0 = today); negative = days since
        the last one when it was within the past 5 days. None when unknown / far away.
        Only report dates Mason had already stored by `known_by` (the item's time) count,
        so an old item never gets a date that was learned later."""
        ds = [d for d, at in (self.earn.get(t) or []) if known_by is None or at <= known_by]
        if not ds:
            return None
        d0 = date.fromisoformat(day)
        nxt = next((x for x in ds if x >= day), None)
        if nxt and (date.fromisoformat(nxt) - d0).days <= 45:
            return (date.fromisoformat(nxt) - d0).days
        prev = [x for x in ds if x < day]
        if prev and (d0 - date.fromisoformat(prev[-1])).days <= 5:
            return -(d0 - date.fromisoformat(prev[-1])).days
        return None


_INSERT_COLS = ("source_kind", "item_id", "horizon_days", "ticker", "item_ts", "item_day", "direction",
                "direction_basis", "label", "score", "confidence", "model", "time_of_day", "entry_kind",
                "regime", "sector", "book", "days_to_earnings", "ref_px", "features_json", "status", "reason",
                "entry_session", "horizon_session", "attempts", "created_date", "grader_version")


def take_in(*, now: float, c: dict, backfill: bool = False, deadline=None) -> dict:
    """Add the rows (status 'pending', features frozen as they were when the item existed)
    for every item not in the table yet. Idempotent: an item already present is skipped."""
    PP = _PP()
    hol, o = PP._nyse_holidays(), _grade_opts(c)
    since_ts = 0.0 if backfill else now - float(c["lookback_days"]) * 86400
    since_iso = "" if backfill else datetime.fromtimestamp(since_ts).isoformat()
    since_utc = "" if backfill else datetime.fromtimestamp(since_ts, timezone.utc).isoformat()
    src = c["sources"]
    out = {"new_items": 0, "new_rows": 0, "by_kind": {}}
    conn = mason_db.get_connection()
    try:
        ctx = _Context(conn)
        have = {}
        for r in _rows(conn, "SELECT DISTINCT source_kind, item_id FROM learn_outcomes"):
            have.setdefault(r["source_kind"], set()).add(r["item_id"])
        gens = []
        if src.get("catalyst"):
            gens.append(_items_catalysts(conn, since_ts, now, c, have.get("catalyst", ())))
        if src.get("alert"):
            gens.append(_items_alerts(conn, since_ts, now, have.get("news_alert", ())))
        if src.get("director") or src.get("local"):
            gens.append(_items_recs(conn, since_iso, c, set().union(*(have.get(k, set()) for k in (
                "director_call", "director_watch", "local_watch", "local_call")))))
        if src.get("dossier"):
            gens.append(_items_dossiers(conn, since_ts))
        if src.get("scanner"):
            gens.append(_items_scanner(conn, since_utc))
        if src.get("local_read"):
            gens.append(_items_local_reads(conn, since_iso))
        if src.get("quick_check"):
            gens.append(_items_quick_checks(conn, since_ts))
        stamp = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        batch = []

        def flush():
            if batch:
                with conn:                        # one short write transaction per few hundred rows
                    conn.executemany(f"INSERT OR IGNORE INTO learn_outcomes({', '.join(_INSERT_COLS)}) "
                                     f"VALUES ({', '.join('?' * len(_INSERT_COLS))})", batch)
                batch.clear()

        for gen in gens:
            for it in gen:
                if it["id"] in have.get(it["kind"], ()):
                    continue
                created = _aware(it.get("ts"))
                if created is None or created.timestamp() > now:
                    continue
                t = str(it.get("ticker") or "").upper().strip()
                et = PP._to_et(created)
                day = et.date().isoformat()
                bad = None if _SYM_RE.match(t) else "not a tradable US ticker symbol"
                feats = {k: v for k, v in (it.get("features") or {}).items() if v is not None}
                for h in c["horizons"]:
                    es, kind, xd, _t0 = _plan(created, h, hol, o["half_days"], o["entry_delay_min"])
                    batch.append((
                        it["kind"], it["id"], int(h), t, et.isoformat(timespec="seconds"), day,
                        it.get("direction"), it.get("basis") or "none", it.get("label"), it.get("score"),
                        it.get("confidence"), it.get("model"), _time_of_day(et, kind), kind, ctx.regime(day),
                        it.get("sector") or ctx.sector.get(t), it.get("book"),
                        ctx.earnings_days(t, day, created.timestamp()),
                        it.get("ref_px"), json.dumps(feats, default=str) if feats else None,
                        "not_gradable" if bad else "pending", bad, es.isoformat(), xd.isoformat(),
                        int(c["max_attempts"]) if bad else 0, stamp, GRADER_VERSION))
                have.setdefault(it["kind"], set()).add(it["id"])
                out["new_items"] += 1
                out["new_rows"] += len(c["horizons"])
                out["by_kind"][it["kind"]] = out["by_kind"].get(it["kind"], 0) + 1
                if len(batch) >= 400:
                    flush()
                    if deadline is not None and time.monotonic() >= deadline:
                        out["partial"] = True
                        return out
        flush()
    finally:
        conn.close()
    return out


# ── grading ──────────────────────────────────────────────────────────────────────

def grade_one(row: dict, prices: Prices, *, now_et: datetime, opts: dict, hol) -> dict:
    """The outcome columns for one row. Every number comes out of the director's
    _tradable_grade_row; this function only chooses the call it is given and copies
    the answer. Returns {"status": graded | not_gradable | pending, ...}."""
    PP = _PP()
    directed = row.get("direction") in ("long", "short")
    sign = -1.0 if row.get("direction") == "short" else 1.0
    rec = {"direction": row["direction"] if directed else "long", "ticker": row["ticker"],
           "created_date": row["item_ts"], "entry_ref_price": row.get("ref_px")}
    extra = {"horizon_days": int(row["horizon_days"])}
    g = PP._tradable_grade_row(rec, extra, now_et=now_et, daily_fn=prices.daily,
                               minute_fn=prices.minutes, opts=opts, holidays=hol)
    note = None
    if g.get("status") == "ungraded" and g.get("reason") == "no usable price at the time of the call":
        # No print within ten minutes of an item that appeared during the session: enter at the
        # NEXT session's open instead (a later price, never an earlier one) and say so.
        es = date.fromisoformat(g["entry_session"])
        late = datetime(es.year, es.month, es.day, tzinfo=PP._et_tz()) + timedelta(
            minutes=PP._close_minutes(es, opts["half_days"]) + 1)
        g = PP._tradable_grade_row(dict(rec, created_date=late.isoformat(), entry_ref_price=None), extra,
                                   now_et=now_et, daily_fn=prices.daily, minute_fn=prices.minutes,
                                   opts=opts, holidays=hol)
        note = "next_session"
    st = g.get("status")
    if st == "pending":
        return {"status": "pending"}
    if st != "graded":
        return {"status": "not_gradable", "reason": g.get("reason") or f"grader said {st}"}
    if g.get("spy_return_pct") is None:
        return {"status": "not_gradable", "reason": g.get("benchmark_note") or "no S&P 500 price for the same days"}
    rule = g.get("tradable_entry_rule") or ""
    exact = int(note is None and (rule in _EXACT_RULES or rule.startswith("minute_")))
    es, xs = date.fromisoformat(g["fill_session"]), date.fromisoformat(g["exit_session"])
    hold, d = 0, es
    while d < xs:
        d = PP._nth_session_after(d, 1, hol)
        hold += 1
    raw = round(sign * float(g["tradable_return_pct"]), 3)
    spy = float(g["spy_return_pct"])
    pc = prices.prev_close(row["ticker"], es, xs)
    return {"status": "graded", "reason": None,
            "entry_session": g["fill_session"], "entry_px": g["tradable_entry_px"],
            "entry_rule": (rule + "+next_session") if note else rule, "entry_exact": exact,
            "gap_pct": None if not pc else round((float(g["tradable_entry_px"]) / pc - 1.0) * 100.0, 3),
            "exit_session": g["exit_session"], "exit_px": g["exit_px"], "hold_sessions": hold,
            "raw_move_pct": raw, "spy_move_pct": round(spy, 3), "raw_minus_spy_pct": round(raw - spy, 3),
            "cost_pct": g["cost_pct"],
            "result_pct": g["tradable_return_net_pct"] if directed else None,
            "result_minus_spy_pct": g["excess_return_net_pct"] if directed else None}


_OUT_COLS = ("status", "reason", "entry_session", "entry_px", "entry_rule", "entry_exact", "gap_pct",
             "exit_session", "exit_px", "hold_sessions", "raw_move_pct", "spy_move_pct", "raw_minus_spy_pct",
             "cost_pct", "result_pct", "result_minus_spy_pct")


def grade_due(*, now: float, c: dict, prices: Prices, deadline=None, limit=None) -> dict:
    """Grade every row whose horizon has passed and whose prices are in hand. Rows whose
    prices could not be fetched in this pass stay 'pending' and are picked up next time."""
    PP = _PP()
    hol, o = PP._nyse_holidays(), _grade_opts(c)
    now_et = datetime.fromtimestamp(now, PP._et_tz())
    last = _last_closed_session(now_et, hol).isoformat()
    retry_before = datetime.fromtimestamp(now - float(c["retry_hours"]) * 3600).isoformat(timespec="seconds")
    conn = mason_db.get_connection()
    try:
        due = [dict(r) for r in conn.execute(
            "SELECT source_kind, item_id, horizon_days, ticker, item_ts, direction, ref_px, entry_session, "
            "horizon_session, status, attempts FROM learn_outcomes WHERE horizon_session<=? AND "
            "(status='pending' OR (status='not_gradable' AND attempts<? AND COALESCE(checked_date,'')<?)) "
            "ORDER BY ticker, item_id, horizon_days" + (f" LIMIT {int(limit)}" if limit else ""),
            (last, int(c["max_attempts"]), retry_before)).fetchall()]
    finally:
        conn.close()
    out = {"due": len(due), "graded": 0, "not_gradable": 0, "left_pending": 0, "calls": 0}
    if not due:
        return out
    bench = o["benchmark"]
    prices.benchmark = bench
    need_d, need_m, plan = {}, {}, {}
    for r in due:
        created = _aware(r["item_ts"])
        es, kind, xd, t0 = _plan(created, r["horizon_days"], hol, o["half_days"], o["entry_delay_min"])
        plan[(r["source_kind"], r["item_id"], r["horizon_days"])] = (es, xd, t0)
        for t in (r["ticker"], bench):
            lo, hi = need_d.get(t, (es, xd))
            need_d[t] = (min(lo, es), max(hi, xd))
            need_m.setdefault(es, {}).setdefault(t, set()).add(t0)
    c0 = prices.calls
    try:
        prices.fetch_daily({bench: need_d[bench]})      # the benchmark first; its minutes ride with each day
        prices.fetch_daily(need_d)
        prices.fetch_minutes(need_m)
    except Exception as e:
        _log_once(f"price fetch stopped: {type(e).__name__}: {e}")
    out["calls"] = prices.calls - c0
    if prices.stopped:
        out["fetch_stopped"] = prices.stopped
    stamp = datetime.fromtimestamp(now).isoformat(timespec="seconds")
    bench_ok = prices.daily_ready(bench, *need_d[bench])
    updates = []

    def flush():
        if updates:
            conn2 = mason_db.get_connection()
            try:
                with conn2:
                    conn2.executemany(
                        "UPDATE learn_outcomes SET " + ", ".join(f"{k}=?" for k in _OUT_COLS)
                        + ", attempts=?, checked_date=?, graded_date=?, grader_version=? "
                          "WHERE source_kind=? AND item_id=? AND horizon_days=?", updates)
            finally:
                conn2.close()
            updates.clear()

    for r in due:
        key = (r["source_kind"], r["item_id"], r["horizon_days"])
        es, xd, t0 = plan[key]
        if deadline is not None and time.monotonic() >= deadline:
            out["partial"] = True
            break
        if not (bench_ok and prices.daily_ready(r["ticker"], es, xd) and prices.minute_ready(r["ticker"], es, t0)
                and prices.minute_ready(bench, es, t0)):
            out["left_pending"] += 1
            continue
        try:
            res = grade_one(r, prices, now_et=now_et, opts=o, hol=hol)
        except Exception as e:
            res = {"status": "not_gradable", "reason": f"grader error: {type(e).__name__}: {e}"[:200]}
        if res["status"] == "pending":
            out["left_pending"] += 1
            continue
        if res["status"] == "not_gradable":
            res = dict({k: None for k in _OUT_COLS}, entry_session=r["entry_session"], **res)
        out[res["status"]] += 1
        updates.append(tuple(res.get(k) for k in _OUT_COLS)
                       + (int(r["attempts"] or 0) + 1, stamp, stamp if res["status"] == "graded" else None,
                          GRADER_VERSION) + key)
        if len(updates) >= 400:
            flush()
    flush()
    return out


# ── dividends (2026-11-16) ───────────────────────────────────────────────────────

def _div_due(now: float, c: dict):
    """WHERE clause + args for the graded rows add_dividends looks at: never looked at, or unknown and
    still inside the re-check window after their exit."""
    since = (datetime.fromtimestamp(now).date() - timedelta(days=int(c.get("div_recheck_days") or 0))).isoformat()
    return ("status='graded' AND (div_status IS NULL OR (div_status='unknown' AND exit_session>=?))", (since,))


def _cash_pct(prices: "Prices", t: str, events: list, es: date, xs: date, base: float):
    """Dividends per share, on the price basis of the bars the grade used, as a percent of `base`.
    None when that basis does not match `base` (a split since the row was graded): never guessed."""
    if not events:
        return 0.0
    bars = prices.daily(t, es, xs)
    tot = 0.0
    for ex_day, amount, share in events:
        prev = [k for k in bars if k < ex_day]
        pc = bars[max(prev)]["c"] if prev else None
        if not (pc and 0.5 < pc / base < 2.0):
            return None
        tot += share * pc
    return tot / base * 100.0


def add_dividends(*, now: float, c: dict, prices: "Prices", deadline=None, limit=None) -> dict:
    """Fill div_pct / spy_div_pct / div_status on graded rows (see the module docstring). Only these three
    columns are written; the price columns are never touched, so running it again never counts twice.
    A row with no record for the stock OR the S&P 500 fund is 'unknown' (both left empty: one side is
    never given dividends the other side lacks)."""
    bench = _grade_opts(c)["benchmark"]
    where, args = _div_due(now, c)
    conn = mason_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT source_kind, item_id, horizon_days, ticker, entry_session, exit_session, entry_px, spy_move_pct "
            f"FROM learn_outcomes WHERE {where} ORDER BY ticker, entry_session"
            + (f" LIMIT {int(limit)}" if limit else ""), args).fetchall()]
    finally:
        conn.close()
    out = {"looked_at": 0, "counted": 0, "none": 0, "unknown": 0}
    memo, updates = {}, []

    def divs(t, es, xs):
        k = (t, es, xs)
        if k not in memo:
            memo[k] = prices.dividends(t, es, xs)
        return memo[k]

    def flush():
        if updates:
            conn2 = mason_db.get_connection()
            try:
                with conn2:
                    conn2.executemany("UPDATE learn_outcomes SET div_pct=?, spy_div_pct=?, div_status=? "
                                      "WHERE source_kind=? AND item_id=? AND horizon_days=?", updates)
            finally:
                conn2.close()
            updates.clear()

    for i, r in enumerate(rows):
        if deadline is not None and i % 100 == 0 and time.monotonic() >= deadline:
            out["partial"] = True
            break
        st, dp, sp = "unknown", None, None
        try:
            es, xs = date.fromisoformat(str(r["entry_session"])), date.fromisoformat(str(r["exit_session"]))
            px, sm = _num(r["entry_px"]), _num(r["spy_move_pct"])
            sd, bd = divs(str(r["ticker"] or "").upper(), es, xs), divs(bench, es, xs)
            if sd is not None and bd is not None and px and px > 0:
                s0 = None                         # the S&P 500 fund's entry price, from its exit close and its move
                if bd:
                    sx = (prices.daily(bench, xs, xs).get(xs.isoformat()) or {}).get("c")
                    s0 = sx / (1.0 + sm / 100.0) if (sx and sm is not None and sm > -100.0) else None
                if s0 or not bd:
                    dp = _cash_pct(prices, str(r["ticker"]).upper(), sd, es, xs, px)
                    sp = _cash_pct(prices, bench, bd, es, xs, s0) if bd else 0.0
                    if dp is None or sp is None:
                        dp = sp = None
                    else:
                        dp, sp = round(dp, 4), round(sp, 4)
                        st = "counted" if (sd or bd) else "none"
        except Exception as e:
            st, dp, sp = "unknown", None, None
            log.debug("[learn] dividends for %s %s: %s", r.get("source_kind"), r.get("item_id"), e)
        out["looked_at"] += 1
        out[st] += 1
        updates.append((dp, sp, st, r["source_kind"], r["item_id"], r["horizon_days"]))
        if len(updates) >= 400:
            flush()
    flush()
    return out


def _dividend_step(*, now: float, c: dict, prices: "Prices", deadline=None) -> dict:
    """Ask the free source for the S&P 500 fund's and Mason's own stocks' dividend records when they are not
    known yet (once a night each; a closed trade's stock only until its record covers the trade), then fill
    the dividend columns. Everything else comes from Mason's own price cache: no request is ever made for an
    idea's stock."""
    PP = _PP()
    bench = prices.benchmark
    where, args = _div_due(now, c)
    conn = mason_db.get_connection()
    try:
        lo = conn.execute(f"SELECT MIN(entry_session) FROM learn_outcomes WHERE {where}", args).fetchone()[0]
    finally:
        conn.close()
    last = _last_closed_session(datetime.fromtimestamp(now, PP._et_tz()), PP._nyse_holidays())
    own = {}                                      # Mason's own stock trades: entry day to exit day (last close if open)
    conn = mason_db.get_connection()
    try:
        trades = _rows(conn, "SELECT ticker, vehicle, entry_ts, exit_ts, status FROM agent_trades WHERE source='mason' "
                             "AND COALESCE(record_deleted,0)=0 AND entry_ts IS NOT NULL")
    finally:
        conn.close()
    for r in trades:
        t, ent = str(r["ticker"] or "").upper(), _aware(r["entry_ts"])
        if not (_SYM_RE.match(t) and str(r["vehicle"] or "stock").lower() == "stock") or ent is None:
            continue
        a = PP._to_et(ent).date()
        ex = _aware(r["exit_ts"]) if r["status"] == "closed" else None
        z = min(PP._to_et(ex).date(), last) if ex is not None else last
        if a <= last:
            w = own.get(t)
            own[t] = (min(a, w[0]), max(z, w[1])) if w else (a, z)
    starts = ([date.fromisoformat(str(lo))] if lo else []) + [w[0] for w in own.values()]
    needs = {bench: (min(starts), last)} if starts else {}
    needs.update({t: w for t, w in own.items() if t != bench})
    asked = prices.fetch_dividends(needs, int(c.get("div_max_fetch") or 0))
    return dict(add_dividends(now=now, c=c, prices=prices, deadline=deadline), requests=asked)


def _mark_needs(now: float, bench: str = "SPY") -> dict:
    """Daily bars the scoreboard needs to value Mason's own stock trades and the same money
    in the S&P 500: {ticker: (first trade day, last closed session)}. Read-only."""
    PP = _PP()
    hol = PP._nyse_holidays()
    last = _last_closed_session(datetime.fromtimestamp(now, PP._et_tz()), hol)
    conn = mason_db.get_connection()
    try:
        rows = _rows(conn, "SELECT ticker, vehicle, entry_ts FROM agent_trades WHERE source='mason' "
                           "AND COALESCE(record_deleted,0)=0 AND entry_ts IS NOT NULL")
    finally:
        conn.close()
    days, syms = [], set()
    for r in rows:
        ent = _aware(r["entry_ts"])
        if ent is None or PP._to_et(ent).date() > last:
            continue
        days.append(PP._to_et(ent).date())
        t = str(r["ticker"] or "").upper()
        if _SYM_RE.match(t) and str(r["vehicle"] or "stock").lower() == "stock":
            syms.add(t)
    if not days:
        return {}
    return {t: (min(days), last) for t in syms | {bench}}


def add_option_facts(*, now: float, c: dict, deadline=None) -> dict:
    """Job C (2026-11-17): for every row not looked at yet, what the options market was pricing for its stock AT THE
    ITEM'S OWN TIME, from Mason's stored option snapshots (mason_option_snaps.implied_move with as_of = item_ts: the
    newest snapshot whose quotes were read at or before the item, never a later one). Added to features_json (no new
    column: the director's description of the database stays as it is):
      opt_snap              'ok' | 'none' (no usable snapshot for that stock then: none taken, older than
                            option_facts_max_age_hours at the item's time, or quotes too wide)
      opt_implied_move_pct  the move, up or down, priced in up to that snapshot's expiry (nearest expiry >= 5 days out)
      opt_implied_days      calendar days from the item's day to that expiry
      opt_atm_iv_pct        at-the-money implied volatility, percent a year
      opt_daily_move_pct    the same as one day's move (IV / sqrt(252))
      opt_snap_age_min      minutes between the snapshot and the item
    Rows from before the first stored snapshot are never looked at (no key, i.e. NULL); rows less than 30 minutes old
    wait for the next pass (a snapshot pass may still be storing). Each item is looked at once. Reads the snapshot
    tables through a read-only connection; writes only learn_outcomes.features_json; no network. Never raises."""
    out = {"looked_at": 0, "ok": 0, "none": 0}
    conn = ro = None
    try:
        import sqlite3 as _sq
        import mason_option_snaps as OS
        conn = mason_db.get_connection()
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opt_snap_straddles'").fetchone():
            return dict(out, skipped="no option snapshots stored")
        firsts = {str(r[0]).upper(): int(r[1]) for r in conn.execute(
            "SELECT ticker, MIN(ts) FROM opt_snap_straddles GROUP BY ticker").fetchall() if r[1] is not None}
        if not firsts:
            return dict(out, skipped="no option snapshots stored")
        first = min(firsts.values())
        lo = datetime.fromtimestamp(first - 2 * 86400, _PP()._et_tz()).isoformat(timespec="seconds")
        rows = conn.execute(
            "SELECT rowid, ticker, item_ts, features_json FROM learn_outcomes WHERE item_ts >= ? AND "
            "(features_json IS NULL OR (json_valid(features_json) AND json_extract(features_json, '$.opt_snap') IS NULL))",
            (lo,)).fetchall()
        try:
            age = float(c.get("option_facts_max_age_hours", 96)) * 60.0
        except (TypeError, ValueError):
            age = 96 * 60.0
        ro = _sq.connect(f"{Path(mason_db.DB_FILE).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        memo, upd = {}, []
        for r in rows:
            at = _aware(r[2])
            if at is None:
                continue
            at = at.timestamp()
            if at < first or at > now - 1800:
                continue                                  # before snapshots began / too fresh: not looked at (yet)
            t = str(r[1] or "").upper().strip()
            key = (t, int(at))
            if key not in memo:
                s = None
                if t in firsts and at >= firsts[t]:
                    s = OS.implied_move(t, int(at), max_age_minutes=age, conn=ro)
                    if s and float(s.get("ts") or 0) > at:
                        s = None                          # (cannot happen: implied_move reads ts <= as_of) never later
                memo[key] = ({"opt_snap": "ok", "opt_implied_move_pct": s.get("implied_move_pct"),
                              "opt_implied_days": s.get("days_left"), "opt_atm_iv_pct": s.get("atm_iv_pct"),
                              "opt_daily_move_pct": s.get("daily_move_pct"), "opt_snap_age_min": s.get("age_minutes")}
                             if s else {"opt_snap": "none"})
            try:
                f = json.loads(r[3]) if r[3] else {}
                if not isinstance(f, dict):
                    continue
            except ValueError:
                continue
            f.update({k: v for k, v in memo[key].items() if v is not None})
            upd.append((json.dumps(f, default=str), r[0]))
            out["looked_at"] += 1
            out["ok" if memo[key]["opt_snap"] == "ok" else "none"] += 1
        for i in range(0, len(upd), 500):
            if deadline is not None and time.monotonic() >= deadline:
                out["partial"] = True                     # the rest is looked at on the next pass
                break
            with conn:
                conn.executemany("UPDATE learn_outcomes SET features_json=? WHERE rowid=?", upd[i:i + 500])
            out["written"] = out.get("written", 0) + len(upd[i:i + 500])
        out["items"] = len(memo)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        _log_once(f"option facts skipped this pass: {out['error']}")
    finally:
        for x in (ro, conn):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
    return out


def run(*, now: float | None = None, backfill: bool = False, network: bool = True, budget_s=None,
        max_calls=None, cache=None, fetch=None, c: dict | None = None, div_fetch=None) -> dict:
    """One pass: take in new items, fetch the prices that are due, grade, add dividends. Safe to
    call again at any time — it only does what is still missing. Never raises."""
    t_start = time.monotonic()
    now = float(now if now is not None else time.time())
    c = c or cfg()
    out = {"ok": False, "backfill": bool(backfill)}
    prices = None
    try:
        deadline = None if budget_s is None else t_start + float(budget_s)
        out["take_in"] = take_in(now=now, c=c, backfill=backfill, deadline=deadline)
        if c.get("option_facts") is True:          # Job C: stored option snapshots at each item's time
            out["option_facts"] = add_option_facts(now=now, c=c, deadline=deadline)
        prices = Prices(cache or cache_path(c), network=network, deadline=deadline, max_calls=max_calls,
                        pause_s=c.get("fetch_pause_s", 0.35), refetch_days=c.get("refetch_days", 7),
                        now=now, fetch=fetch, benchmark=_grade_opts(c)["benchmark"], div_fetch=div_fetch)
        out["grade"] = grade_due(now=now, c=c, prices=prices, deadline=deadline)
        try:                                      # closing prices for the scoreboard (usually already held)
            prices.fetch_daily(_mark_needs(now, prices.benchmark))
        except Exception as e:
            log.debug("[learn] scoreboard prices skipped: %s", e)
        if c.get("dividends", True) is not False:
            try:                                  # a failure here leaves the dividend columns as they were
                out["dividends"] = _dividend_step(now=now, c=c, prices=prices, deadline=deadline)
            except Exception as e:
                out["dividends"] = {"error": f"{type(e).__name__}: {e}"}
                _log_once(f"dividends skipped this pass: {out['dividends']['error']}")
        out.update(ok=True, calls=prices.calls, bars_fetched=prices.bars_in,
                   partial=bool(out["take_in"].get("partial") or out["grade"].get("partial")
                                or out["grade"].get("left_pending") or (out.get("dividends") or {}).get("partial")))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        _log_once(f"pass failed: {out['error']}")
    finally:
        if prices is not None:
            out.setdefault("calls", prices.calls)
            prices.close()
    out["seconds"] = round(time.monotonic() - t_start, 1)
    _state["last_run"] = dict(out, at=datetime.fromtimestamp(now).isoformat(timespec="seconds"))
    return out


# ── the nightly schedule ─────────────────────────────────────────────────────────

def _hhmm(s, default=(3, 15)):
    try:
        hh, mm = (int(x) for x in str(s).split(":")[:2])
        return hh, mm
    except Exception:
        return default


def _in_window(now_et: datetime, c: dict) -> bool:
    hh, mm = _hhmm(c.get("time_et"))
    start = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now_et < start + timedelta(hours=float(c.get("window_hours") or 3.0))


def tick(now: float | None = None, *, force: bool = False) -> dict:
    """Called once an hour from the maintenance thread, after the backup step. Does
    nothing outside the nightly window or once tonight's work is finished. Each call is
    held to `tick_budget_s`; unfinished work continues on the next hourly call and, past
    the nightly request limit, the next night. Never raises."""
    try:
        c = cfg()
        if c.get("enabled") is False:
            return {}
        now = float(now if now is not None else time.time())
        now_et = datetime.fromtimestamp(now, _PP()._et_tz())
        if not force and not _in_window(now_et, c):
            return {}
        day = now_et.date().isoformat()
        st_db = Prices(cache_path(c), network=False, now=now, readonly=True)
        try:
            st = st_db.state_get("night") or {}
        finally:
            st_db.close()
        if st.get("day") != day:
            st = {"day": day, "calls": 0, "done": False}
        if st.get("done") and not force:
            return {}
        left = max(0, int(c.get("max_fetch_calls_per_night") or 0) - int(st.get("calls") or 0))
        res = run(now=now, budget_s=float(c.get("tick_budget_s") or 240), max_calls=left, c=c)
        st["calls"] = int(st.get("calls") or 0) + int(res.get("calls") or 0)
        # finished for tonight when nothing is left that tonight could still do
        st["done"] = bool(res.get("ok")) and (not res.get("partial") or st["calls"] >= int(
            c.get("max_fetch_calls_per_night") or 0))
        st_db = Prices(cache_path(c), network=False, now=now)
        try:
            st_db.state_set("night", st)
            st_db.state_set("last_pass", {k: res.get(k) for k in ("ok", "error", "calls", "seconds", "partial",
                                                                   "take_in", "grade", "dividends")} | {"at": now})
        finally:
            st_db.close()
        if res.get("ok"):
            _state["last_error"] = None
        return {"grader": {k: res.get(k) for k in ("ok", "error", "calls", "seconds", "partial")}}
    except Exception as e:
        _log_once(f"nightly step failed: {type(e).__name__}: {e}")
        return {"grader": {"ok": False, "error": f"{type(e).__name__}: {e}"}}


def status() -> dict:
    """Row counts by kind and state, and what the last pass did. Read-only. Never raises."""
    out = {"enabled": cfg().get("enabled") is not False, "rows": {}, "last_pass": None}
    try:
        conn = mason_db.get_connection()
        try:
            for r in conn.execute("SELECT source_kind, status, COUNT(*) n FROM learn_outcomes GROUP BY 1, 2"):
                out["rows"].setdefault(r["source_kind"], {})[r["status"]] = r["n"]
            try:                                  # graded rows by what is known about their dividends
                out["dividends"] = {r["s"]: r["n"] for r in conn.execute(
                    "SELECT COALESCE(div_status, 'not looked at yet') s, COUNT(*) n FROM learn_outcomes "
                    "WHERE status='graded' GROUP BY 1")}
            except sqlite3.Error:
                pass
        finally:
            conn.close()
        p = cache_path()
        if p.exists():
            st_db = Prices(p, network=False, readonly=True)
            try:
                out["last_pass"] = st_db.state_get("last_pass")
                out["night"] = st_db.state_get("night")
            finally:
                st_db.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mason learning grader (see the module docstring).")
    ap.add_argument("--backfill", action="store_true", help="take in ALL history and grade it (no nightly limits)")
    ap.add_argument("--db", help="database file to work on (default: the live mason.db)")
    ap.add_argument("--cache", help="price cache file (default: learning.grader.cache_path)")
    ap.add_argument("--no-network", action="store_true", help="never fetch a price")
    ap.add_argument("--max-calls", type=int, default=None, help="most outside price requests in this run")
    ap.add_argument("--once", action="store_true", help="one ordinary pass now (what the nightly job does, no limits)")
    ap.add_argument("--as-of", help="pretend it is this Eastern time, 'YYYY-MM-DD HH:MM' (rehearsals on a copy only)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true", help="allow a run during the nightly backup window")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if a.db:
        mason_db.DB_FILE = Path(a.db)
        mason_db.reset_schema_cache()
    if a.status:
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if not (a.backfill or a.once):
        ap.error("choose --backfill, --once or --status (the nightly pass runs by itself)")
    et = datetime.now(_PP()._et_tz())
    if not a.db and not a.force and (2, 15) <= (et.hour, et.minute) < (3, 15):
        print("Not started: the nightly database backup runs around now. Try again after 03:15 ET.")
        return 2
    now = None
    if a.as_of:
        if not a.db:
            ap.error("--as-of is for rehearsals: it needs --db COPY")
        now = datetime.strptime(a.as_of, "%Y-%m-%d %H:%M").replace(tzinfo=_PP()._et_tz()).timestamp()
    res = run(now=now, backfill=bool(a.backfill), network=not a.no_network, max_calls=a.max_calls, cache=a.cache)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
