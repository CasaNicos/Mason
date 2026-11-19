"""
mason_symbols.py — is this symbol worth work?  (Step 3 of the 2026-11-13 audit, R10)

Two small, shared answers, both deterministic and free:

  1. TAG SANITY. A news vendor's symbol tag is sometimes a FOREIGN listing that
     happens to spell a US ticker (L'Oréal → "OR", which in the US is OR Royalties;
     Persimmon plc → "PSN", which is Parsons; Spin Master → "TOY", not listed here).
     `sane_tags()` keeps a vendor tag only when it is a known US listing AND there
     is some evidence the headline is about that issuer. The known-listing set is
     the one Mason already keeps for fact-checking (mason_factcheck.load_ticker_master:
     the SEC company list ∪ the book ∪ the allow-list ∪ the optional Alpaca asset
     supplement) — no second cache is built here.

  2. NOT-FOUND MEMORY. When a lookup for a symbol comes back "no such thing"
     (no quote, no fundamentals, no analyst coverage ...), remember it for 24 hours
     so nobody repeats the same failing request every half hour. Used by
     mason_dossier._section_street (analyst coverage: the SKYA case — 43 dossiers in
     a week, each asking Yahoo again) and by mason_triage's ticker gate.
     `mark_not_found(sym, what)` / `is_not_found(sym, what)`; `what` names the lookup
     ("symbol" = the whole symbol is dead, "street" = analyst coverage, ...).
     Stored in the existing source_snapshots table (source='_not_found'), so it
     survives a restart and needs no schema change.

  3. RENAMES (2026-11-16, NEWS_REVIEW follow-up 6). A vendor can keep tagging a company
     with its OLD ticker after a rename (PSKY for Skydance, now SKYD); the old symbol is no
     longer a known listing, so the tag was dropped. `renamed(sym)` / `apply_renames(syms)`
     give the current ticker; mason_news_service applies them to vendor tags as they arrive.

  4. YAHOO SPELLING (2026-11-16, R2-A). `yf_symbol(sym)` / `yf_symbols(syms)`: the symbol to hand
     yfinance (BRK.B → BRK-B). Every yfinance call site goes through it; nothing else changes the
     symbol Mason stores or sends to Alpaca / Robinhood.

Config (mason.config.json, read on each call; every switch ON when absent):
  news.symbols.tag_gate_enabled        true   drop vendor tags that fail the sanity check
  news.symbols.negative_cache_enabled  true   remember not-found lookups
  news.symbols.negative_cache_hours    24
  news.symbols.renames_enabled         true   map an old ticker to its new one (see RENAMES below)
  news.symbols.renames                 {}     owner's own {"OLD": "NEW"} pairs, laid over RENAMES;
                                              "OLD": "" removes one of the built-in pairs

Never imports mason_app. Public functions never raise.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mason.symbols")
BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"
_CREATED_BY = "mason_symbols"
_SNAP_SOURCE = "_not_found"

_cfg_cache = {"mtime": None, "data": {}}
_DEFAULTS = {"tag_gate_enabled": True, "negative_cache_enabled": True, "negative_cache_hours": 24,
             "renames_enabled": True}

# Old ticker -> new ticker. Each pair is checked in Mason's own SEC company list (the
# .edgar_tickers_cache.json copies of 22 Oct .. 12 Nov): the same SEC company number (CIK)
# was listed under both tickers, then the old one was removed. Each also appears in Mason's
# news with a vendor still using the old ticker after the change. Left out on purpose: a
# status suffix (GETY -> GETYQ bankruptcy, WKEY / PERF -> foreign OTC), a split company
# (MSGS -> MSGK, its Rangers news is MSGR) and a symbol vendors use for another company (UUU).
RENAMES = {
    "PSKY": "SKYD",    # CIK 2041610 Skydance Corp (Paramount Skydance); PSKY gone by 29 Oct
    "ADIL": "AZR",     # CIK 1513525 Adial Pharmaceuticals -> Azora Therapeutics (name change 28 Oct)
    "VTAK": "VJET",    # CIK 1716621 Catheter Precision -> Flyte Aviation
    "HKIT": "BDAI",    # CIK 1742341 Hitek Global -> Biddance AI Systems
}


def _cfg() -> dict:
    """news.symbols merged over the defaults. Never raises."""
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        blk = ((_cfg_cache["data"].get("news") or {}).get("symbols") or {})
    except Exception:
        blk = {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in blk.items() if v is not None})
    return out


# ── the known-listing set (reused from mason_factcheck) ─────────────────────────

# mason_factcheck re-reads the book (positions file + three DB tables) on every call
# to decide whether its cache is still valid; on the news stream that would be once
# per headline, so the answer is held here for a minute.
_MASTER_TTL_S = 60.0
_master_mem = {"ts": 0.0, "master": None, "titles": None, "broad": None}


def _refresh_master() -> None:
    now = time.time()
    if _master_mem["master"] is not None and (now - _master_mem["ts"]) < _MASTER_TTL_S:
        return
    try:
        import mason_factcheck as FC
        m = FC.load_ticker_master() or set()
        t = FC.load_ticker_titles() or {}
    except Exception:
        m, t = set(), {}
    try:
        import mason_triage
        b = set(mason_triage.broad_universe(allow_refresh=False))
    except Exception:
        b = set()
    _master_mem.update(ts=now, master=m, titles=t, broad=b)


def reset_cache() -> None:
    """Forget the held listing set (tests; after a universe refresh)."""
    _master_mem.update(ts=0.0, master=None, titles=None, broad=None)


def _master() -> set:
    _refresh_master()
    return _master_mem["master"] or set()


def _titles() -> dict:
    _refresh_master()
    return _master_mem["titles"] or {}


def known(sym: str) -> bool:
    """True when `sym` is a known US listing (SEC company list / book / allow-list /
    Alpaca supplement). When the list itself cannot be loaded this answers True —
    a broken cache must not silence the news."""
    try:
        s = str(sym or "").strip().upper()
        if not s:
            return False
        m = _master()
        if not m:
            return True
        return s in m or s.replace(".", "-") in m or s.replace("-", ".") in m
    except Exception:
        return True


def renames() -> dict:
    """The rename map in force: RENAMES with news.symbols.renames laid over it (a pair set
    to "" or null removes a built-in one). {} when news.symbols.renames_enabled is false."""
    try:
        cfg = _cfg()
        if not cfg.get("renames_enabled", True):
            return {}
        out = dict(RENAMES)
        own = cfg.get("renames")
        if isinstance(own, dict):
            for k, v in own.items():
                k = str(k or "").strip().upper()
                v = str(v or "").strip().upper()
                if k and v:
                    out[k] = v
                elif k:
                    out.pop(k, None)
        return out
    except Exception:
        return dict(RENAMES)


def renamed(sym: str) -> str:
    """The current ticker for `sym` (upper-cased): the new ticker when `sym` is an old one
    in renames() that is no longer a known listing while the new one is; otherwise `sym`.
    A recycled ticker (the old symbol listed again, for another company) is left alone."""
    s = str(sym or "").strip().upper()
    try:
        new = renames().get(s)
        if new and new != s and not known(s) and known(new):
            return new
    except Exception:
        pass
    return s


def apply_renames(syms) -> tuple:
    """Vendor tags -> (tags with renamed() applied, order kept, repeats removed;
    [{"sym": old, "to": new}] for each tag that was renamed). Never raises."""
    out, changed = [], []
    for x in syms or []:
        s = str(x or "").strip().upper()
        if not s:
            continue
        r = renamed(s)
        if r != s:
            changed.append({"sym": s, "to": r})
        if r not in out:
            out.append(r)
    return out, changed


# words that say nothing about WHICH company
_GENERIC = set(
    "inc incorporated corp corporation co company companies ltd limited plc ag sa nv se lp llc "
    "holdings holding group the and of de class common stock shares trust fund etf adr ads "
    "technologies technology industries international systems solutions global partners capital "
    "financial bancorp bancshares bank energy resources pharmaceuticals pharmaceutical therapeutics "
    "pharma biosciences sciences medical health healthcare services enterprises worldwide american "
    "national first united general new royalties acquisition".split())


# two capitals that are usually a word or an abbreviation, not a company
_TWO_LETTER_WORDS = set("AI AM AN AS AT BE BY DO EU GO HE IF IN IP IR IS IT ME MY NO OF ON OR PM PR SO TO TV UK UN UP US WE".split())


def _name_tokens(title: str) -> list:
    toks = re.findall(r"[a-z0-9]+", (title or "").lower().replace("'", "").replace("’", ""))
    return [t for t in toks if len(t) >= 3 and t not in _GENERIC]


def issuer_named(sym: str, text: str, titles: dict = None) -> bool:
    """Is there evidence in `text` that it is about the US issuer behind `sym`?
    Evidence: an explicit ticker cue ($SYM, (SYM), 'NYSE: SYM'), the bare symbol as an
    upper-case word (3+ letters), or a distinctive word of the registered company
    name (exact, or one being the start of the other — 'Snapchat' / 'Snap')."""
    try:
        s = str(sym or "").strip().upper()
        if not s or not text:
            return False
        if re.search(rf"(?:\$|\(|:\s?){re.escape(s)}(?![A-Za-z])", text):
            return True
        if len(s) >= 3 and re.search(rf"(?<![A-Za-z$]){re.escape(s)}(?![A-Za-z])", text):
            return True
        if len(s) == 2 and s not in _TWO_LETTER_WORDS and \
                re.search(rf"(?<![A-Za-z$&.]){re.escape(s)}(?=\s+[A-Z])", text):
            return True                       # "TD Bank Group", "GE Aerospace", "HP Inc"
        titles = titles if titles is not None else _titles()
        title = titles.get(s) or titles.get(s.replace(".", "-")) or ""
        names = _name_tokens(title)
        if not names:
            # a name made only of generic words ("OR Royalties Inc."): require it verbatim
            core = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
            core = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc)\b", " ", core)
            core = " ".join(core.split())
            return bool(core) and core in " ".join(re.findall(r"[a-z0-9]+", text.lower()))
        words = set(re.findall(r"[a-z0-9]+", text.lower().replace("'", "").replace("’", "")))
        for n in names:
            if n in words:
                return True
            if len(n) >= 4:
                for w in words:
                    if len(w) >= 4 and (w.startswith(n) or (n.startswith(w) and len(w) >= 5)):
                        return True
        return False
    except Exception:
        return True


def sane_tags(headline: str, symbols, *, book=(), desc: str = "") -> tuple:
    """Vendor symbol tags → (kept, dropped). `dropped` is a list of
    {"sym", "why"} so nothing disappears silently. A tag is kept when:
      * it is in the book (holdings / watchlist), or
      * it is a known US listing AND (it belongs to the broad S&P 500 / Nasdaq-100
        universe, where brand-led headlines are normal and mis-mapping is rare, OR
        the headline names the issuer, OR Mason's own name resolver reads the
        headline as that ticker).
    Order is preserved. With the switch off, or on any error, everything is kept."""
    syms = [str(s).strip().upper() for s in (symbols or []) if s and str(s).strip()]
    try:
        if not syms or not _cfg().get("tag_gate_enabled", True):
            return syms, []
        bookset = {str(b).upper() for b in (book or ())}
        master = _master()
        if not master:
            return syms, []
        titles = _titles()
        text = f"{headline or ''}  {desc or ''}"
        broad, resolved = (_master_mem.get("broad") or set()), None
        kept, dropped = [], []
        for s in syms:
            if s in bookset:
                kept.append(s)
                continue
            if not (s in master or s.replace(".", "-") in master):
                dropped.append({"sym": s, "why": "not a known US listing"})
                continue
            if s in broad or issuer_named(s, text, titles):
                kept.append(s)
                continue
            if resolved is None:
                try:
                    import mason_triage
                    resolved = set(mason_triage.resolve_tickers(headline or "", desc or ""))
                except Exception:
                    resolved = set()
            if s in resolved:
                kept.append(s)
            else:
                dropped.append({"sym": s, "why": "headline does not name this US company"})
        return kept, dropped
    except Exception as e:
        log.debug(f"[symbols] sane_tags error, keeping all: {e}")
        return syms, []


# ── not-found memory (24 h) ─────────────────────────────────────────────────────

_nf_lock = threading.Lock()
_nf = {}                        # (what, SYM) -> (expires_epoch, reason)
_nf_loaded = 0.0
_NF_RELOAD_S = 300


def _nf_key(sym, what) -> tuple:
    return (str(what or "symbol").strip().lower(), str(sym or "").strip().upper())


def _nf_load(force: bool = False) -> None:
    """Pull unexpired rows from source_snapshots at most every few minutes."""
    global _nf_loaded
    now = time.time()
    if not force and (now - _nf_loaded) < _NF_RELOAD_S:
        return
    _nf_loaded = now
    conn = None
    try:
        import mason_db
        conn = mason_db.get_connection()
        rows = conn.execute("SELECT key, payload_json FROM source_snapshots WHERE source=? "
                            "AND record_deleted=0", (_SNAP_SOURCE,)).fetchall()
        fresh = {}
        for r in rows:
            try:
                p = json.loads(r[1]) or {}
                exp = float(p.get("expires") or 0)
                if exp > now and ":" in r[0]:
                    what, sym = r[0].split(":", 1)
                    fresh[(what, sym)] = (exp, str(p.get("reason") or ""))
            except Exception:
                continue
        with _nf_lock:
            for k, v in list(_nf.items()):          # keep unexpired in-memory marks too
                if v[0] > now and k not in fresh:
                    fresh[k] = v
            _nf.clear()
            _nf.update(fresh)
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_not_found(sym: str, what: str = "symbol") -> bool:
    """True when a `what` lookup for `sym` was marked not-found within the last
    negative_cache_hours (default 24). 'symbol' marks also answer True for any
    other `what` (a dead symbol has no analyst coverage either)."""
    try:
        if not _cfg().get("negative_cache_enabled", True):
            return False
        _nf_load()
        now = time.time()
        with _nf_lock:
            for k in (_nf_key(sym, what), _nf_key(sym, "symbol")):
                v = _nf.get(k)
                if v and v[0] > now:
                    return True
        return False
    except Exception:
        return False


def mark_not_found(sym: str, what: str = "symbol", reason: str = "") -> bool:
    """Remember that the `what` lookup for `sym` found nothing. Returns True when
    stored (or already remembered). Safe to call on every failure: a mark that is
    still running is NOT extended, so after 24 hours the lookup is tried again."""
    try:
        cfg = _cfg()
        if not cfg.get("negative_cache_enabled", True):
            return False
        k = _nf_key(sym, what)
        if not k[1]:
            return False
        now = time.time()
        _nf_load()
        with _nf_lock:
            cur = _nf.get(k)
            if cur and cur[0] > now:
                return True            # already remembered: the 24 hours run from the FIRST miss,
                                       # so the lookup is tried again when they are over
        exp = now + float(cfg.get("negative_cache_hours", 24) or 24) * 3600
        with _nf_lock:
            _nf[k] = (exp, str(reason or "")[:200])
        conn = None
        try:
            import mason_db
            conn = mason_db.get_connection()
            payload = json.dumps({"expires": exp, "reason": str(reason or "")[:200], "marked": now},
                                 separators=(",", ":"))
            with conn:
                conn.execute(
                    "INSERT INTO source_snapshots (source, key, taken_at, payload_json, created_date, "
                    "created_by, record_deleted) VALUES (?,?,?,?,?,?,0) "
                    "ON CONFLICT(source, key) DO UPDATE SET taken_at=excluded.taken_at, "
                    "payload_json=excluded.payload_json, record_deleted=0",
                    (_SNAP_SOURCE, f"{k[0]}:{k[1]}", now, payload,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), _CREATED_BY))
        except Exception as e:
            log.debug(f"[symbols] not-found mark kept in memory only: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return True
    except Exception:
        return False


def clear_not_found(sym: str, what: str = None) -> int:
    """Forget not-found marks for `sym` (one `what`, or all). Returns how many."""
    n = 0
    try:
        s = str(sym or "").strip().upper()
        with _nf_lock:
            for k in [k for k in _nf if k[1] == s and (what is None or k[0] == str(what).lower())]:
                _nf.pop(k, None)
                n += 1
        conn = None
        try:
            import mason_db
            conn = mason_db.get_connection()
            with conn:
                if what is None:
                    conn.execute("DELETE FROM source_snapshots WHERE source=? AND key LIKE ?",
                                 (_SNAP_SOURCE, f"%:{s}"))
                else:
                    conn.execute("DELETE FROM source_snapshots WHERE source=? AND key=?",
                                 (_SNAP_SOURCE, f"{str(what).lower()}:{s}"))
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception:
        pass
    return n


def not_found_list() -> list:
    """[{sym, what, reason, expires}] currently remembered (for a status page)."""
    try:
        _nf_load(force=True)
        now = time.time()
        with _nf_lock:
            return sorted(({"sym": k[1], "what": k[0], "reason": v[1], "expires": v[0]}
                           for k, v in _nf.items() if v[0] > now), key=lambda d: (d["sym"], d["what"]))
    except Exception:
        return []


def work_ok(sym: str) -> bool:
    """One answer for "should anyone build a story / dossier for this symbol?":
    a known US listing that is not remembered as dead."""
    try:
        return known(sym) and not is_not_found(sym, "symbol")
    except Exception:
        return True


# ── Yahoo spelling (R2-A 2026-11-16) ────────────────────────────────────────────
# Mason, Alpaca, Robinhood, SEC-tag vendors and the news write a US share class with a
# dot (BRK.B, BF.B, MKC.V); Yahoo / yfinance writes it with a dash (BRK-B). Asked for
# "BRK.A", yfinance answers "$BRK.A: possibly delisted; no price data found". yf_symbol()
# is the ONE place that spelling is made: call it on the symbol handed to yfinance and
# keep the caller's own symbol for everything else (DB rows, dict keys, Alpaca and
# Robinhood calls, the UI). Only a US class suffix (one letter after a dot or slash)
# changes; Yahoo's own spellings pass through untouched (^VIX, CL=F, DX-Y.NYB, BTC-USD,
# BRK-B), as do longer suffixes (SHOP.TO) and lowercase input is upper-cased like
# yfinance itself does.
_YF_CLASS_RE = re.compile(r"^([A-Z][A-Z0-9]{0,6})[./]([A-Z])$")


def yf_symbol(sym) -> str:
    """'BRK.B' → 'BRK-B', 'bf.b' → 'BF-B', 'BRK/A' → 'BRK-A'; anything else unchanged (upper-cased).
    Never raises."""
    try:
        s = str(sym or "").strip().upper()
        m = _YF_CLASS_RE.match(s)
        return f"{m.group(1)}-{m.group(2)}" if m else s
    except Exception:
        return sym


def yf_symbols(syms) -> dict:
    """{caller's symbol: Yahoo symbol} for a batch, in input order (duplicates kept once).
    For a yfinance batch download: pass list(m.values()) and read each result back with m[sym]."""
    out = {}
    for s in syms or []:
        if s not in out:
            out[s] = yf_symbol(s)
    return out


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        print(a, "known" if known(a) else "unknown", "not-found" if is_not_found(a) else "ok")
