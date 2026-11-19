"""
mason_sizing.py — STEP 4: size by risk, limits, value gate (audit X11 + X12, 2026-11-15).

What it does
------------
One call, plan(...), made by mason_autotrade._build for every order it builds. It answers:

  1. SIZE BY RISK      quantity = risk budget for the trade / distance from entry to stop, never more
                       dollars than today's tier budget. For a long option the risk per contract is the
                       premium paid x the stop fraction the exit code really uses (the call's own stop
                       premium; else autotrade.exits.default_opt_stop_pct; else the whole premium).
  2. LIMITS            per-symbol cap (Nico's holding counts as well as Mason's), per-sector cap,
                       earnings rule (shrink or block a new entry shortly before the company reports).
  3. VALUE GATE        a call with no stop or no target is refused; minimum reward-to-risk; minimum
                       expected dollar value; "already moved" check (the move since the prior close or
                       since the event against the stock's normal day and the option's implied move).
  4. FEATURES          the numbers above, recorded on the trade at decision time.
  5. BOOK TEXT         what is held (per symbol and per sector, with distance to stop) and the limits,
                       in plain words, for the director and the quick checks (book_block()).

Safety
------
  * Every rule has its own switch and every switch is OFF by default. autotrade.sizing.shadow is ON by
    default: with a rule off, Mason sizes and places exactly as before and the plan only RECORDS what the
    rule would have done. With every switch off AND shadow off, active() is False and nothing here runs.
  * Deterministic. No model call. Free data only: the broker position list Mason already reads, the
    sector cache (desk_state 'pead:sector', filled from Robinhood fundamentals; misses are looked up
    once and cached under 'sizing:sector'), earnings_results / calendar_events, price_bars_daily, and
    the quote the order is priced from.
  * Never raises. A rule that cannot be judged (no sector, no earnings date, no bars, an exception) is
    skipped, the trade is sized the old way where sizing itself failed, and the plan says so.
  * Read-only: nothing here places, cancels, sells or resizes anything. Robinhood is called only through
    mason_robinhood._call with the read tool get_equity_fundamentals.

Config (mason.config.json → autotrade)
--------------------------------------
  sizing.enabled                         risk-based quantity is USED                       (false)
  sizing.shadow                          record what the rules would do while they are off (true)
  sizing.show_book                       add the book + limits text to the model prompts   (false)
  limits.symbol_cap.enabled              (false)     limits.sector_cap.enabled             (false)
  limits.earnings.enabled                (false)
  value_gate.require_levels.enabled      (false)     value_gate.min_reward_risk.enabled    (false)
  value_gate.min_expected_value.enabled  (false)     value_gate.priced_in.enabled          (false)
See DEFAULTS for every number.

Step 4b (2026-11-15, Nico's spend table)
----------------------------------------
  * An OPTION is sized by its spend limit (autotrade.option_spend: dollars by days to expiry and tier),
    not by the amount at risk: the premium paid is what can be lost. sizing.options_by_spend (true);
    false = the earlier rule (risk amount with the one-contract allowance). Stock is sized by risk as
    before. The spend limit, its band and tier, and the contracts it gives are recorded on every option.
  * HARD CEILING: no trade may lose more than limits.max_loss.max_pct percent of the account at its
    stop (2.0; null = no ceiling). limits.max_loss.enabled (false): off = recorded only, like the others.
  * sizing.show_spend_table (false): the model text describes the table. Off = every word the models
    read is exactly what it was before Step 4b.
"""

import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta

MODULE = "mason_sizing"
VERSION = 1

BROAD = "Broad market fund"
_EXEMPT_DEFAULT = [BROAD, "Bond fund", "International fund", "Commodity fund", "Crypto fund", "Other fund"]

DEFAULTS = {
    "sizing": {
        "enabled": False,                 # risk-based quantity is used for real orders
        "shadow": True,                   # rules that are off are still worked out and recorded
        "show_book": False,               # model-visible text (needs Nico's approval of the wording)
        # dollars Mason may lose if the stop is hit, per trade, by tier
        "risk_usd": {"learning": 65.0, "normal": 100.0, "high": 200.0},
        "risk_usd_default": 65.0,         # a call with no tier (placed by hand)
        "usd_cap_mult": 1.0,              # dollar cap per trade = today's tier budget x this (never above 1:
                                          # to allow more dollars raise autotrade.tiers.<tier>.usd_max instead)
        "one_unit_max_risk_mult": 2.0,    # one SHARE is still bought if its risk <= the amount x this
        "one_contract_max_risk_mult": 5.0,  # one option CONTRACT is still bought if its risk <= the amount x this
        "min_stop_pct_stock": 0.5,        # a stock stop closer than this % of price is sized as if this far
        "min_stop_frac_option": 0.25,     # an option stop tighter than this fraction of premium is sized as this
        "opt_no_stop_frac": 1.0,          # an option with no stop at all: the whole premium is at risk
        "apply_to_manual": False,         # limits + value gate also block an order Nico approves by hand
        "lookup_budget_s": 4.0,           # (review) most seconds ONE order may wait for all outside lookups together
                                          # (sector, daily prices, the stock price behind an option); 0 = never wait
        # Step 4b
        "options_by_spend": True,         # with sizing.enabled: an option is sized by its spend limit
                                          # (autotrade.option_spend), not by risk_usd; false = the earlier rule
        "show_spend_table": False,        # model-visible wording for the spend table (needs Nico's approval)
        "show_larger_sizes": False,       # (Nico 2026-11-15: keep off) also tell the models that higher-rated calls
                                          # get larger amounts and that those are locked; off = only the amounts
                                          # of the smallest size are ever mentioned
    },
    "limits": {
        "symbol_cap": {"enabled": False,
                       "max_pct": 8.0,            # Nico's + Mason's holding of one symbol, % of account value
                       "index_max_pct": 30.0},    # ... for a broad market fund (SPY, QQQ, IWM, ...)
        "sector_cap": {"enabled": False,
                       "max_pct": 50.0,           # everything in the account in one sector, % of account value
                       "mason_max_pct": 8.0,      # Mason's own trades in one sector, % of account value
                       "exempt": list(_EXEMPT_DEFAULT),
                       "sector_map": {},          # {"TICKER": "Sector"} set by hand, wins over everything
                       "fetch_missing": True,     # look a missing sector up at Robinhood (read-only), then cache
                       "ttl_days": 90, "miss_ttl_hours": 24, "fetch_timeout_s": 6.0},
        "earnings": {"enabled": False,
                     "sessions_before": 3,        # stock: a report this many trading sessions ahead or fewer
                     "stock_action": "shrink",    # "shrink" | "block"
                     "option_sessions_before": 10,  # option: a report before expiry and this close (null = any)
                     "option_action": "block",    # "shrink" | "block"
                     "shrink_mult": 0.5,
                     "keep_one": True,            # shrinking never goes below one share / contract
                     "earnings_trade_re": r"(?<![-\w])earnings[- ]trade\b(?!-)"},
        # Step 4b: hard ceiling on what ONE trade may lose at its stop
        "max_loss": {"enabled": False,
                     "max_pct": 2.0},             # % of account value; null = no ceiling at all
    },
    "value_gate": {
        "require_levels": {"enabled": False},
        "min_reward_risk": {"enabled": False, "value": 1.0},
        "min_expected_value": {"enabled": False, "usd": 2.0},
        "priced_in": {"enabled": False,
                      "stock_max_mult": 6.0,      # move in the trade's direction / the stock's normal day
                      "option_max_mult": 2.0,
                      "lookback_sessions": 20, "min_sessions": 10, "max_bar_age_days": 6,
                      "fetch_missing_bars": True, "fetch_timeout_s": 8.0,
                      # Job C (2026-11-17): a normal day as the OPTIONS MARKET priced it at the close the move is
                      # measured from (stored option snapshots), instead of the stock's own past days; the past days
                      # stay the yardstick whenever no snapshot is stored. Changes only what the rule records while
                      # the rule itself is off. (false)
                      "use_snapshots": False,
                      "snapshot_max_age_minutes": 390},   # a snapshot older than this at that close is not used
    },
}

# gate name -> the words used when it blocks (mason_autotrade._GATE_WORDS gets the same lines)
GATE_WORDS = {
    "risk_size": "the amount Mason may lose on one trade does not cover one share or one contract",
    "symbol_cap": "the account already holds as much of this stock as the limit allows",
    "sector_cap": "the account already holds as much of this sector as the limit allows",
    "earnings_window": "the company reports earnings too soon",
    "exit_plan": "the call has no stop or no target",
    "reward_risk": "the possible gain is too small next to the possible loss",
    "expected_value": "the trade is worth too little on average to be worth placing",
    "priced_in": "the price has already made an unusually large move in this direction",
    # Step 4b ({pct} = limits.max_loss.max_pct, filled in by gate_words / mason_autotrade.plain_block_reasons)
    "spend_limit": "one contract costs more than Mason may spend on one option trade",
    "max_loss": "this trade could lose more than {pct}% of the account",
}

_FUND_SECTORS = {
    **{s: BROAD for s in ("SPY", "VOO", "IVV", "VTI", "QQQ", "QQQM", "IWM", "DIA", "RSP", "MTUM", "SPMO",
                          "QUAL", "USMV", "VLUE", "VBR", "SPLG", "VT")},
    **{s: "International fund" for s in ("VXUS", "EFA", "EEM", "FXI", "KWEB", "EWJ")},
    **{s: "Bond fund" for s in ("SHY", "IEF", "TLT", "HYG", "TIP", "LQD", "BND", "AGG")},
    **{s: "Commodity fund" for s in ("GLD", "SLV", "USO", "DBC", "IAU")},
    "IBIT": "Crypto fund",
    # sector funds count with the nearest Robinhood sector (approximate on purpose)
    "XLK": "Electronic Technology", "SMH": "Electronic Technology", "SOXX": "Electronic Technology",
    "ITA": "Electronic Technology", "XLC": "Technology Services", "IGV": "Technology Services",
    "CIBR": "Technology Services", "ARKK": "Technology Services", "XLF": "Finance", "XLRE": "Finance",
    "XLE": "Energy Minerals", "XLV": "Health Technology", "XBI": "Health Technology",
    "XLI": "Producer Manufacturing", "XLY": "Retail Trade", "XLP": "Consumer Non-Durables",
    "XLU": "Utilities", "XLB": "Process Industries", "URA": "Non-Energy Minerals", "TAN": "Utilities",
}

# Robinhood's sector names in plain words (model- and owner-facing text)
SECTOR_WORDS = {
    "Electronic Technology": "chips, hardware and aerospace", "Technology Services": "software and internet",
    "Health Technology": "drugs and medical devices", "Health Services": "health insurers and providers",
    "Finance": "banks, insurers and property", "Retail Trade": "retailers",
    "Consumer Non-Durables": "food, drink and household goods", "Consumer Durables": "cars and durable goods",
    "Consumer Services": "restaurants, travel and media", "Commercial Services": "business services",
    "Producer Manufacturing": "industrial machinery", "Process Industries": "chemicals and materials",
    "Energy Minerals": "oil and gas", "Non-Energy Minerals": "metals and mining",
    "Industrial Services": "energy services and construction", "Transportation": "airlines, rail and shipping",
    "Utilities": "utilities", "Communications": "telecom", "Distribution Services": "distributors",
    "Miscellaneous": "other", BROAD: "whole-market funds", "Bond fund": "bond funds",
    "International fund": "funds of foreign shares", "Commodity fund": "gold, oil and other commodity funds",
    "Crypto fund": "crypto funds", "Other fund": "other funds",
}

_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_DS_PEAD, _DS_OWN = "pead:sector", "sizing:sector"
_memo = {"sectors": None, "sectors_at": 0.0, "tries": {}}
_MEMO_TTL_S = 300.0
_lock = threading.Lock()
_tl = threading.local()                  # .deadline: when the order being planned stops waiting for outside lookups


def _wait_s(own_timeout) -> float:
    """Seconds a lookup may be waited for: its own limit, cut to what is left of the order's time
    budget (sizing.lookup_budget_s). 0.0 = do not start it. Outside plan() only the own limit applies."""
    try:
        own = max(0.5, float(own_timeout or 6.0))
    except (TypeError, ValueError):
        own = 6.0
    dl = getattr(_tl, "deadline", None)
    if dl is None:
        return own
    left = dl - time.monotonic()
    return 0.0 if left < 0.05 else min(own, left)


def _timed(fn, wait_s, name="sizing-lookup"):
    """fn() in a helper thread, waited for at most `wait_s` seconds; None when it is not back by then
    (the thread is left to finish on its own) or when it raised."""
    if wait_s <= 0:
        return None
    box = {}

    def work():
        try:
            box["v"] = fn()
        except Exception:
            pass
    t = threading.Thread(target=work, name=name, daemon=True)
    t.start()
    t.join(wait_s)
    return box.get("v")


# ── small helpers ────────────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _merge(base: dict, over) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def scfg(cfg=None) -> dict:
    """{"sizing", "limits", "value_gate"} over DEFAULTS. `cfg` = the autotrade config dict (None = read it)."""
    try:
        if cfg is None:
            import mason_autotrade as AT
            cfg = AT._at_cfg()
    except Exception:
        cfg = {}
    out = {}
    for k in ("sizing", "limits", "value_gate"):
        try:
            v = (cfg or {}).get(k)
            out[k] = _merge(DEFAULTS[k], v if isinstance(v, dict) else {})
        except Exception:
            out[k] = _merge(DEFAULTS[k], {})
    return out


def _on(block) -> bool:
    return isinstance(block, dict) and block.get("enabled") is True


def switches(sc: dict) -> dict:
    return {"sizing": _on(sc["sizing"]),
            "symbol_cap": _on(sc["limits"].get("symbol_cap")),
            "sector_cap": _on(sc["limits"].get("sector_cap")),
            "earnings": _on(sc["limits"].get("earnings")),
            "max_loss": _on(sc["limits"].get("max_loss")),
            "require_levels": _on(sc["value_gate"].get("require_levels")),
            "min_reward_risk": _on(sc["value_gate"].get("min_reward_risk")),
            "min_expected_value": _on(sc["value_gate"].get("min_expected_value")),
            "priced_in": _on(sc["value_gate"].get("priced_in"))}


def active(cfg=None) -> bool:
    """False = nothing in this module is to run for an order (every switch off and shadow off)."""
    try:
        sc = scfg(cfg)
        return bool(sc["sizing"].get("shadow") is True or any(switches(sc).values()))
    except Exception:
        return False


def gate_words(gate, sc=None) -> str:
    """GATE_WORDS[gate] with the loss ceiling's percent filled in."""
    w = GATE_WORDS.get(gate, gate)
    if "{pct}" in w:
        try:
            w = w.replace("{pct}", f"{float((sc or scfg())['limits']['max_loss']['max_pct']):g}")
        except Exception:
            w = w.replace("{pct}% of the account", "the most allowed on one trade")
    return w


def show_book(cfg=None) -> bool:
    try:
        return scfg(cfg)["sizing"].get("show_book") is True
    except Exception:
        return False


def _money(v) -> str:
    v = _f(v)
    if v is None:
        return "n/a"
    if abs(v) >= 100 or abs(v - round(v)) < 0.005:
        return f"${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _holidays() -> set:
    try:
        import mason_director
        return set(mason_director._NYSE_HOLIDAYS)
    except Exception:
        return set()


def _is_session(d: date, hol) -> bool:
    return d.weekday() < 5 and d.isoformat() not in hol


def _prev_session(d: date, hol) -> date:
    d -= timedelta(days=1)
    while not _is_session(d, hol):
        d -= timedelta(days=1)
    return d


def sessions_to_report(today: date, report_date, timing, holidays=None):
    """Trading sessions between `today` and the last close BEFORE the market can react to the report.
    A report after the close (or with no known time) on day D is carried through D's close; a report
    before the open on D is carried through the close of the session before D. 0 = a position opened
    today is still open when the report lands. None = the report can no longer hit a position opened
    today (it is in the past, or it came out before today's open)."""
    try:
        rd = report_date if isinstance(report_date, date) else date.fromisoformat(str(report_date)[:10])
    except Exception:
        return None
    hol = _holidays() if holidays is None else holidays
    tm = str(timing or "").lower()
    before_open = tm == "am" or "before" in tm
    carry = _prev_session(rd, hol) if before_open else rd
    while not _is_session(carry, hol):               # a report dated on a closed day: the session before it
        carry = _prev_session(carry, hol)
    if carry < today:
        return None
    n, d = 0, today
    while d < carry:
        d += timedelta(days=1)
        if _is_session(d, hol):
            n += 1
    return n


def norm_iv(iv):
    """Implied volatility as a percent per year. Robinhood gives a fraction (0.897), the stored
    contract quotes a percent (89.7)."""
    v = _f(iv)
    if v is None or v <= 0:
        return None
    return v * 100.0 if v <= 5.0 else v


def underlying_of(sym) -> str:
    s = str(sym or "").upper().strip()
    m = _OCC.match(s)
    return m.group(1) if m else s


def _side_of(sym) -> str:
    """'long' for stock and calls, 'short' for puts — what a held symbol adds to."""
    m = _OCC.match(str(sym or "").upper().strip())
    return "short" if (m and m.group(5) == "P") else "long"


# ── data: DB reads (all free, all local) ─────────────────────────────────────────

def _db_rows(sql, args=()):
    import mason_db
    conn = mason_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _ds_get(key) -> dict:
    try:
        r = _db_rows("SELECT value FROM desk_state WHERE key=?", (key,))
        v = json.loads(r[0]["value"]) if r and r[0].get("value") else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _ds_set(key, value) -> bool:
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            with conn:
                conn.execute("INSERT INTO desk_state (key, value, updated_date) VALUES (?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_date=excluded.updated_date",
                             (key, json.dumps(value, sort_keys=True), datetime.now().isoformat(timespec="seconds")))
        finally:
            conn.close()
        return True
    except Exception:
        return False


def _sector_caches() -> tuple:
    now = time.monotonic()
    with _lock:
        if _memo["sectors"] is not None and now - _memo["sectors_at"] < _MEMO_TTL_S:
            return _memo["sectors"]
    pair = (_ds_get(_DS_PEAD), _ds_get(_DS_OWN))
    with _lock:
        _memo["sectors"], _memo["sectors_at"] = pair, now
    return pair


def reset():
    with _lock:
        _memo.update(sectors=None, sectors_at=0.0, tries={})


def _lookups_allowed() -> bool:
    """Outside lookups (Robinhood fundamentals, a refill of the daily price cache) happen only when the
    SAME config file autotrade reads has Robinhood switched on — a test config never reaches them."""
    try:
        import mason_quotes
        return bool(mason_quotes._rh_enabled())
    except Exception:
        return False


def _fund_label(sector, industry):
    if re.search(r"investment trusts|mutual funds|exchange traded", str(industry or ""), re.I):
        return "Other fund"
    return sector


def _rh_fundamentals(symbols, timeout_s):
    """{TICKER: {"sector", "industry"}} from Robinhood's read-only fundamentals tool; {} on any failure.
    Runs in a helper thread so a slow answer can never hold an order up for longer than `timeout_s`."""
    box = {}

    def work():
        try:
            import mason_robinhood
            data = mason_robinhood._call("get_equity_fundamentals", {"symbols": list(symbols)}, lane="live")
            for r in (data.get("results") if isinstance(data, dict) else None) or []:
                if isinstance(r, dict) and r.get("symbol"):
                    box[str(r["symbol"]).upper()] = {"sector": r.get("sector"), "industry": r.get("industry")}
        except Exception:
            pass
    wait = _wait_s(timeout_s)
    if wait <= 0:                                             # this order has used up its waiting time
        return {}
    t = threading.Thread(target=work, name="sizing-sector", daemon=True)
    t.start()
    t.join(wait)
    return dict(box)


def sector_of(ticker, sc=None, *, fetch=True, fetch_fn=None, today=None) -> str | None:
    """Sector of one ticker, or None when it is not known. Order: the hand-set map, the fund table,
    the earnings module's cache, this module's own cache, then (once, cached) Robinhood fundamentals."""
    t = str(ticker or "").upper().strip()
    if not t:
        return None
    try:
        sc = sc or scfg()
        cap = sc["limits"].get("sector_cap") or {}
        hand = {str(k).upper(): v for k, v in (cap.get("sector_map") or {}).items()}
        if hand.get(t):
            return str(hand[t])
        if t in _FUND_SECTORS:
            return _FUND_SECTORS[t]
        pead, own = _sector_caches()
        today = today or date.today()
        e = pead.get(t)
        if isinstance(e, dict) and e.get("sector"):
            return _fund_label(str(e["sector"]).strip(), e.get("industry"))
        e = own.get(t)
        if isinstance(e, dict):
            try:
                age_h = (datetime.now() - datetime.fromisoformat(str(e.get("at")))).total_seconds() / 3600.0
            except Exception:
                age_h = 1e9
            if e.get("sector") and age_h <= float(cap.get("ttl_days") or 90) * 24.0:
                return _fund_label(str(e["sector"]).strip(), e.get("industry"))
            if not e.get("sector") and age_h <= float(cap.get("miss_ttl_hours") or 24):
                return None                                   # looked up recently, nothing there
        if not (fetch and cap.get("fetch_missing", True)):
            return None
        if fetch_fn is None and not _lookups_allowed():
            return None
        if fetch_fn is None and _wait_s(cap.get("fetch_timeout_s")) <= 0:
            return None                                       # no waiting time left for this order: next time
        with _lock:                                           # at most one try per ticker per 15 minutes
            last = _memo["tries"].get(t, 0.0)
            if time.monotonic() - last < 900.0 and last:
                return None
            _memo["tries"][t] = time.monotonic()
        got = (fetch_fn or _rh_fundamentals)([t], cap.get("fetch_timeout_s") or 6.0) or {}
        if t not in got:
            return None                                       # no answer (Robinhood off / slow): try again later
        g = got[t] or {}
        sec = g.get("sector") if isinstance(g.get("sector"), str) and g.get("sector").strip() else None
        own = dict(_ds_get(_DS_OWN))
        own[t] = {"sector": sec.strip() if sec else None, "industry": g.get("industry"),
                  "at": datetime.now().isoformat(timespec="seconds")}
        _ds_set(_DS_OWN, own)
        with _lock:
            _memo["sectors"] = None
        return _fund_label(sec.strip(), g.get("industry")) if sec else None
    except Exception:
        return None


def warm_sectors(tickers, sc=None, *, fetch_fn=None, max_n=20) -> int:
    """Look up, in ONE Robinhood call, the sectors of held names that no cache knows yet, so the next
    order sees them. Names tried in the last 15 minutes, or cached as "nothing there" in the last day,
    are skipped. Returns how many names were asked for. Never raises."""
    try:
        sc = sc or scfg()
        cap = sc["limits"].get("sector_cap") or {}
        if not cap.get("fetch_missing", True) or (fetch_fn is None and not _lookups_allowed()):
            return 0
        pead, own = _sector_caches()
        need = []
        for t in dict.fromkeys(str(x or "").upper().strip() for x in (tickers or [])):
            if not t or t in _FUND_SECTORS or (isinstance(pead.get(t), dict) and pead[t].get("sector")):
                continue
            e = own.get(t)
            if isinstance(e, dict):
                try:
                    age_h = (datetime.now() - datetime.fromisoformat(str(e.get("at")))).total_seconds() / 3600.0
                except Exception:
                    age_h = 1e9
                if age_h <= (float(cap.get("ttl_days") or 90) * 24.0 if e.get("sector")
                             else float(cap.get("miss_ttl_hours") or 24)):
                    continue
            with _lock:
                last = _memo["tries"].get(t, 0.0)
                if last and time.monotonic() - last < 900.0:
                    continue
                _memo["tries"][t] = time.monotonic()
            need.append(t)
            if len(need) >= int(max_n):
                break
        if not need:
            return 0
        got = (fetch_fn or _rh_fundamentals)(need, cap.get("fetch_timeout_s") or 6.0) or {}
        if got:
            own = dict(_ds_get(_DS_OWN))
            stamp = datetime.now().isoformat(timespec="seconds")
            for t in need:
                if t in got:
                    g = got[t] or {}
                    sec = g.get("sector") if isinstance(g.get("sector"), str) and g.get("sector").strip() else None
                    own[t] = {"sector": sec.strip() if sec else None, "industry": g.get("industry"), "at": stamp}
            _ds_set(_DS_OWN, own)
            with _lock:
                _memo["sectors"] = None
        return len(need)
    except Exception:
        return 0


def next_earnings(ticker, today: date, holidays=None) -> dict | None:
    """{"date", "timing", "sessions", "source"} of the next report that can still hit a position opened
    today, or None when no date is stored for this ticker."""
    t = str(ticker or "").upper().strip()
    ti = today.isoformat()
    cands = []
    try:
        for r in _db_rows("SELECT report_date, timing, eps_act FROM earnings_results WHERE ticker=? AND report_date >= ? "
                          "ORDER BY report_date LIMIT 4", (t, ti)):
            if r["report_date"] == ti and r.get("eps_act") is not None:
                continue                                      # already out
            cands.append((r["report_date"], r.get("timing"), "earnings_results"))
    except Exception:
        pass
    if not cands:
        try:
            for r in _db_rows("SELECT event_date, event_time, status FROM calendar_events WHERE ticker=? AND "
                              "event_type='earnings' AND event_date >= ? AND record_deleted=0 ORDER BY event_date LIMIT 4",
                              (t, ti)):
                if str(r.get("status") or "") == "reported":
                    continue
                cands.append((r["event_date"], r.get("event_time"), "calendar_events"))
        except Exception:
            pass
    if not cands:
        try:
            import mason_calendar
            for it in mason_calendar.list_events(ti, (today + timedelta(days=120)).isoformat(), types=["earnings"],
                                                 today=today) or []:
                if t in (it.get("tickers") or []) and it.get("status") != "done":
                    cands.append((it.get("date"), it.get("subtitle"), "mason_calendar"))
        except Exception:
            pass
    for d, tm, src in sorted(cands, key=lambda c: str(c[0])):
        n = sessions_to_report(today, d, tm, holidays)
        if n is not None:
            return {"date": str(d)[:10], "timing": tm, "sessions": n, "source": src}
    return None


def daily_closes(ticker, before_iso, n=21) -> list:
    """[(date, close)] oldest first: the last `n` daily closes strictly before `before_iso`."""
    try:
        rows = _db_rows("SELECT date, close FROM price_bars_daily WHERE ticker=? AND date < ? AND close IS NOT NULL "
                        "ORDER BY date DESC LIMIT ?", (str(ticker).upper(), before_iso, int(n)))
        return [(r["date"], float(r["close"])) for r in rows][::-1]
    except Exception:
        return []


def _fresh(closes, today: date, max_age_days=None, holidays=None) -> bool:
    """True when the newest stored close is the close of the last session before `today`. (review) An
    older close is not "yesterday's close": measuring today's move from it would count two or more
    days as one, so the already-moved check is skipped instead. `max_age_days` is no longer used."""
    try:
        if not closes:
            return False
        hol = _holidays() if holidays is None else holidays
        return date.fromisoformat(str(closes[-1][0])[:10]) >= _prev_session(today, hol)
    except Exception:
        return False


def recent_closes(ticker, today: date, pc: dict) -> list:
    """The last daily closes before `today` from the local price cache. When they are missing or old,
    Mason's own price cache is asked once to fill them (free: Robinhood day bars / yfinance), in a
    helper thread with a time limit; [] when there is still nothing recent."""
    n = int(pc.get("lookback_sessions") or 20) + 1
    ti = today.isoformat()
    closes = daily_closes(ticker, ti, n)
    if _fresh(closes, today) and len(closes) > int(pc.get("min_sessions") or 10):
        return closes
    wait = _wait_s(pc.get("fetch_timeout_s") or 8.0)
    if pc.get("fetch_missing_bars", True) and wait > 0 and _lookups_allowed():
        key = f"bars:{str(ticker).upper()}"
        with _lock:
            last = _memo["tries"].get(key, 0.0)
            due = not last or time.monotonic() - last >= 900.0
            if due:
                _memo["tries"][key] = time.monotonic()
        if due:
            def work():
                try:
                    import mason_price_cache
                    mason_price_cache.get_daily(str(ticker).upper(), (today - timedelta(days=45)).isoformat(),
                                                (today - timedelta(days=1)).isoformat())
                except Exception:
                    pass
            th = threading.Thread(target=work, name="sizing-bars", daemon=True)
            th.start()
            th.join(wait)
            closes = daily_closes(ticker, ti, n)
    return closes if _fresh(closes, today) else []


def normal_move(closes, min_sessions=10):
    """Mean absolute daily change in percent over the given closes (None with too few)."""
    ch = [abs(closes[i][1] / closes[i - 1][1] - 1.0) * 100.0 for i in range(1, len(closes)) if closes[i - 1][1]]
    if len(ch) < int(min_sessions):
        return None
    return sum(ch) / len(ch)


def event_ref(rec, today: date):
    """(close before the call's catalyst first appeared, that date, sessions since) or (None, None, None)."""
    try:
        cid = ((rec or {}).get("extra") or {}).get("catalyst_id")
        t = str((rec or {}).get("ticker") or "").upper()
        if not cid or not t:
            return None, None, None
        r = _db_rows("SELECT first_seen FROM catalysts WHERE id=?", (cid,))
        fs = _f(r[0]["first_seen"]) if r else None
        if not fs:
            return None, None, None
        try:
            from zoneinfo import ZoneInfo
            ev_day = datetime.fromtimestamp(fs, ZoneInfo("America/New_York")).date()
        except Exception:
            ev_day = datetime.utcfromtimestamp(fs).date()
        if ev_day > today:
            return None, None, None
        b = daily_closes(t, ev_day.isoformat(), 1)
        if not b:
            return None, None, None
        hol = _holidays()
        n, d = 0, date.fromisoformat(b[0][0])
        while d < today:
            d += timedelta(days=1)
            if _is_session(d, hol):
                n += 1
        return b[0][1], b[0][0], max(1, n)
    except Exception:
        return None, None, None


def catalyst_kind(rec):
    try:
        cid = ((rec or {}).get("extra") or {}).get("catalyst_id")
        if not cid:
            return None
        r = _db_rows("SELECT event_kind FROM catalysts WHERE id=?", (cid,))
        return (r[0].get("event_kind") if r else None) or None
    except Exception:
        return None


_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|without|avoid\w*|skip\w*|isn'?t|aren'?t|wasn'?t|don'?t|doesn'?t|won'?t)\b"
                       r"|n't\b", re.I)


def is_earnings_trade(rec, sc, kind_fn=None) -> bool:
    """The call itself says it is an earnings trade: its story is an earnings story, or its own text
    uses the words "earnings trade"."""
    try:
        ex = (rec or {}).get("extra") or {}
        if ex.get("earnings_trade") is True or ((ex.get("trade") or {}).get("earnings_trade") is True):
            return True
        if (kind_fn or catalyst_kind)(rec) == "earnings":
            return True
        pat = (sc["limits"].get("earnings") or {}).get("earnings_trade_re")
        if not pat:
            return False
        for k in ("prediction", "thesis", "invalidation"):
            txt = str(ex.get(k) or "")
            for m in re.finditer(pat, txt, re.I):            # (review) "not an earnings trade", "avoid the earnings trade"
                before = re.split(r"[.;:!?]", txt[:m.start()])[-1]   # do not switch the rule off: look back in the sentence
                if not _NEGATION.search(before):
                    return True
        return False
    except Exception:
        return False


# ── the book: who holds what ─────────────────────────────────────────────────────

def mason_open_qty() -> dict:
    """{SYMBOL or OCC: quantity Mason still holds} from agent_trades. {} when it cannot be read."""
    out = {}
    try:
        for r in _db_rows("SELECT ticker, occ, qty, features_json FROM agent_trades WHERE source='mason' AND "
                          "status='open' AND COALESCE(record_deleted,0)=0"):
            sym = str(r.get("occ") or r.get("ticker") or "").upper()
            q = _f(r.get("qty")) or 0.0
            try:
                for x in (json.loads(r.get("features_json") or "{}").get("partial_exits") or []):
                    q -= _f(x.get("qty")) or 0.0
            except Exception:
                pass
            if sym and q > 1e-9:
                out[sym] = out.get(sym, 0.0) + q
    except Exception:
        return {}
    return out


def _parked() -> dict:
    """{FUND: shares the idle-cash rule has parked} (mason_idle_cash). {} when nothing is parked, the module is
    absent or its ledger cannot be read. Parked shares are spare cash: they are not a holding for the limits.
    (review fix) A purchase of the rule's that is still working counts in full."""
    try:
        import mason_idle_cash as IC
        return dict(IC.owned_map() or {})
    except Exception:
        return {}


def book_snapshot(pos, equity, *, mason_qty=None, sc=None, sector_fn=None) -> dict:
    """The account as the limits see it. `pos` = mason_autotrade._positions().

    {"ok", "equity", "symbols": {UNDERLYING: {"long_usd", "short_usd", "mason_long_usd", "mason_short_usd",
    "nico_long_usd", "nico_short_usd", "sector", "lines": [...]}}, "sectors": {SECTOR: {"long_usd",
    "short_usd", "mason_long_usd", "mason_short_usd", "symbols": [...]}}}
    A put is 'short' exposure, stock and calls are 'long'. An option counts with the money in it (its
    market value), not with the shares it controls."""
    out = {"ok": False, "equity": _f(equity), "symbols": {}, "sectors": {}, "unknown_sector": []}
    try:
        if not (pos or {}).get("ok"):
            out["error"] = "no position list"
            return out
        mq = dict(mason_open_qty() if mason_qty is None else mason_qty)
        sc = sc or scfg()
        sfn = sector_fn or (lambda t: sector_of(t, sc, fetch=False))
        pk = _parked()                                    # idle-cash rule: {} unless something is parked
        for p in pos.get("positions") or []:
            sym = str(p.get("symbol") or "").upper()
            qty = abs(_f(p.get("qty")) or 0.0)
            if not sym or qty <= 0:
                continue
            is_opt = bool(_OCC.match(sym))
            mult = 100.0 if is_opt else 1.0
            px = _f(p.get("current_price"))
            mv = abs(_f(p.get("market_value")) or 0.0) or ((px or 0.0) * qty * mult)
            if not px and qty:
                px = mv / qty / mult
            if pk and not is_opt and str(p.get("side") or "long").lower() == "long":
                # Parked cash (the idle-cash rule's shares of the index fund) is not a holding: it is left out of
                # the one-stock and sector limits and out of the holdings text, and kept as its own figure.
                pq = min(qty, float(pk.get(sym, 0.0)))
                if pq > 1e-9:
                    out["parked_usd"] = out.get("parked_usd", 0.0) + mv * (pq / qty)
                    out.setdefault("parked", {})[sym] = pq
                    mv *= (qty - pq) / qty
                    qty -= pq
                    if qty <= 1e-9:
                        continue
            und, side = underlying_of(sym), _side_of(sym)
            if str(p.get("side") or "long").lower() != "long":
                side = "short" if side == "long" else "long"  # a short stock position is the owner's
            mine = min(qty, mq.get(sym, 0.0))
            m_usd = mv * (mine / qty) if qty else 0.0
            s = out["symbols"].setdefault(und, {"long_usd": 0.0, "short_usd": 0.0, "mason_long_usd": 0.0,
                                                "mason_short_usd": 0.0, "nico_long_usd": 0.0, "nico_short_usd": 0.0,
                                                "sector": None, "lines": []})
            s[f"{side}_usd"] += mv
            s[f"mason_{side}_usd"] += m_usd
            s[f"nico_{side}_usd"] += mv - m_usd
            s["lines"].append({"symbol": sym, "is_option": is_opt, "qty": qty, "mason_qty": mine,
                               "nico_qty": qty - mine, "px": px, "entry": _f(p.get("avg_entry_price")),
                               "value": mv, "side": side,
                               "unrealized_pct": (_f(p.get("unrealized_plpc")) or 0.0) * 100.0
                               if p.get("unrealized_plpc") is not None else None})
        for und, s in out["symbols"].items():
            try:
                sec = sfn(und)
            except Exception:
                sec = None
            s["sector"] = sec
            if not sec:
                out["unknown_sector"].append(und)
                continue
            g = out["sectors"].setdefault(sec, {"long_usd": 0.0, "short_usd": 0.0, "mason_long_usd": 0.0,
                                                "mason_short_usd": 0.0, "symbols": []})
            for k in ("long_usd", "short_usd", "mason_long_usd", "mason_short_usd"):
                g[k] += s[k]
            g["symbols"].append(und)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ── the maths ────────────────────────────────────────────────────────────────────

def risk_per_unit(vehicle, limit, stop, sc, exits_cfg=None) -> dict:
    """What one share / one contract loses if the stop is hit.
    {"ok", "usd", "frac" (of the price paid), "basis"}; ok False = it cannot be worked out (no stop on a
    stock and no default stop configured, or no price)."""
    limit, stop = _f(limit), _f(stop)
    s, ex = sc["sizing"], (exits_cfg or {})
    if not limit or limit <= 0:
        return {"ok": False, "why": "no entry price"}
    if str(vehicle or "").lower() in ("call", "put"):
        if stop is not None and 0 <= stop < limit:
            frac, basis = (limit - stop) / limit, "the call's stop premium"
        elif stop is not None:
            return {"ok": False, "why": f"stop premium {stop:g} is not below the entry premium {limit:g}"}
        elif _f(ex.get("default_opt_stop_pct")) is not None:
            frac, basis = float(ex["default_opt_stop_pct"]) / 100.0, "the default option stop"
        else:
            frac, basis = float(s.get("opt_no_stop_frac") or 1.0), "no stop: the whole premium"
        floor = float(s.get("min_stop_frac_option") or 0.0)
        if frac < floor:
            frac, basis = floor, basis + f" (sized as {floor * 100:.0f}% of the premium at least)"
        frac = min(1.0, max(0.0, frac))
        return {"ok": frac > 0, "usd": limit * 100.0 * frac, "frac": frac, "basis": basis}
    if stop is not None and 0 < stop < limit:
        dist, basis = limit - stop, "the call's stop"
    elif stop is not None:
        return {"ok": False, "why": f"stop {stop:g} is not below the entry {limit:g}"}
    elif _f(ex.get("default_stock_stop_pct")) is not None:
        dist, basis = limit * float(ex["default_stock_stop_pct"]) / 100.0, "the default stock stop"
    else:
        return {"ok": False, "why": "the call has no stop"}
    floor = limit * float(s.get("min_stop_pct_stock") or 0.0) / 100.0
    if dist < floor:
        dist, basis = floor, basis + f" (sized as {float(s.get('min_stop_pct_stock')):g}% of the price at least)"
    return {"ok": dist > 0, "usd": dist, "frac": dist / limit, "basis": basis}


def loss_per_unit(vehicle, limit, stop, exits_cfg=None) -> dict:
    """Step 4b, for the loss ceiling. What one share / one contract really loses when its stop is hit,
    with the stop the exit code uses (mason_agent_loop._levels) and no rounding up: stock (entry - stop);
    option (entry premium - stop premium) x 100, the default option stop when the call gives none and
    one is configured, else the whole premium. {"ok", "usd", "basis"}; ok False = it cannot be worked
    out (a stock call with no stop and no default stop, or no price)."""
    limit, stop = _f(limit), _f(stop)
    ex = exits_cfg or {}
    if not limit or limit <= 0:
        return {"ok": False, "why": "no entry price"}
    if str(vehicle or "").lower() in ("call", "put"):
        if stop is not None and 0 <= stop < limit:
            return {"ok": True, "usd": (limit - stop) * 100.0, "basis": "the call's stop premium"}
        if stop is None and _f(ex.get("default_opt_stop_pct")) is not None:
            frac = min(1.0, max(0.0, float(ex["default_opt_stop_pct"]) / 100.0))
            if frac > 0:
                return {"ok": True, "usd": limit * 100.0 * frac, "basis": "the default option stop"}
        return {"ok": True, "usd": limit * 100.0,
                "basis": "no stop: the whole premium" if stop is None else "the stop premium is not below the entry: the whole premium"}
    if stop is not None and 0 < stop < limit:
        return {"ok": True, "usd": limit - stop, "basis": "the call's stop"}
    if stop is not None:
        return {"ok": False, "why": f"stop {stop:g} is not below the entry {limit:g}"}
    if _f(ex.get("default_stock_stop_pct")) is not None and float(ex["default_stock_stop_pct"]) > 0:
        return {"ok": True, "usd": limit * float(ex["default_stock_stop_pct"]) / 100.0, "basis": "the default stock stop"}
    return {"ok": False, "why": "the call has no stop"}


def spend_limit(cfg, tier, dte):
    """Step 4b. {"usd", "band", "tier", "days"} from autotrade.option_spend for one option trade, or None
    when no table is set (mason_agent_loop.option_spend). A call with no tier uses the learning column."""
    try:
        import mason_agent_loop as AL
        return AL.option_spend(tier or "learning", dte, cfg if cfg is not None else None)
    except Exception:
        return None


def risk_budget(tier, sc, mult=1.0) -> float:
    s = sc["sizing"]
    b = _f((s.get("risk_usd") or {}).get(str(tier))) if tier else None
    if b is None:
        b = _f(s.get("risk_usd_default")) or 0.0
    try:
        m = min(1.0, max(0.0, float(mult)))                   # the learned multiplier can only lower it
    except (TypeError, ValueError):
        m = 1.0
    return max(0.0, b * m)


def size_by_risk(unit_cost, unit_risk, budget_risk, usd_cap, sc, is_option=False) -> dict:
    """Quantity = risk budget / risk per unit, never more dollars than the cap.
    {"qty", "bind": "risk"|"cap"|"one_unit"|"none", "why" (when qty is 0)}"""
    unit_cost, unit_risk = _f(unit_cost), _f(unit_risk)
    budget_risk, usd_cap = _f(budget_risk) or 0.0, _f(usd_cap) or 0.0
    if not unit_cost or unit_cost <= 0 or not unit_risk or unit_risk <= 0:
        return {"qty": 0, "bind": "none", "why": "no price or no risk per unit"}
    q_risk = int(math.floor(budget_risk / unit_risk + 1e-9))
    q_cap = int(math.floor(usd_cap / unit_cost + 1e-9))
    if q_cap < 1:
        return {"qty": 0, "bind": "cap", "q_risk": q_risk, "q_cap": q_cap,
                "why": f"one costs {_money(unit_cost)}, more than the {_money(usd_cap)} allowed per trade"}
    if q_risk < 1:
        allow = float(sc["sizing"].get("one_contract_max_risk_mult" if is_option else "one_unit_max_risk_mult") or 1.0)
        if unit_risk <= budget_risk * allow + 1e-9:
            return {"qty": 1, "bind": "one_unit", "q_risk": q_risk, "q_cap": q_cap}
        return {"qty": 0, "bind": "risk", "q_risk": q_risk, "q_cap": q_cap,
                "why": (f"one would lose {_money(unit_risk)} at its stop; the amount Mason may lose on this "
                        f"trade is {_money(budget_risk)} (up to {_money(budget_risk * allow)} for a single one)")}
    return {"qty": min(q_risk, q_cap), "bind": "risk" if q_risk <= q_cap else "cap", "q_risk": q_risk, "q_cap": q_cap}


def _room_qty(q, room_usd, unit_cost):
    if room_usd is None:
        return q
    return max(0, min(q, int(math.floor(max(0.0, room_usd) / unit_cost + 1e-9))))


# ── the plan ─────────────────────────────────────────────────────────────────────

def plan(*, cfg, rec, vehicle, ticker, limit, stop, target, qty_today, usd_budget, tier=None,
         equity=None, pos=None, quote=None, approver="auto", now=None, expiry=None, p=None,
         risk_mult=1.0, order_symbol=None, providers=None) -> dict:
    """Everything Step 4 has to say about one order. Never raises.

    Returns {"v", "switches", "shadow", "qty_today", "qty" (what to place: equals qty_today unless a
    live rule changed it), "qty_all_rules" (what all rules together would place), "rules": [...],
    "live_blocks": [{"gate", "detail"}], "warnings": [...], "would_block": [...], "fallback": [...],
    "features": {...}}.
    `providers` (tests / replay) may replace: sector(ticker), earnings(ticker, today), closes(ticker,
    before_iso, n), underlying_px(ticker), event_ref(rec, today), book (a ready book_snapshot),
    catalyst_kind(rec), holidays (set), spend(tier, days_to_expiry) (the option spend limit)."""
    pv = providers or {}
    out = {"v": VERSION, "qty_today": qty_today, "qty": qty_today, "qty_all_rules": qty_today, "rules": [],
           "live_blocks": [], "warnings": [], "would_block": [], "fallback": [], "features": {}}
    _tl.deadline = None
    try:
        sc = scfg(cfg)
        sw = switches(sc)
        try:                                                 # (review) one time budget for every outside lookup below
            _tl.deadline = time.monotonic() + max(0.0, float(sc["sizing"].get("lookup_budget_s", 4.0)))
        except (TypeError, ValueError):
            _tl.deadline = time.monotonic() + 4.0
        manual = (approver == "nico") and not sc["sizing"].get("apply_to_manual")
        out["switches"], out["shadow"] = sw, bool(sc["sizing"].get("shadow") is True)
        if manual:
            out["manual"] = True
        vehicle = str(vehicle or "").lower()
        is_opt = vehicle in ("call", "put")
        side = "short" if vehicle == "put" else "long"
        ticker = str(ticker or "").upper()
        limit_px, stop, target = _f(limit), _f(stop), _f(target)       # limit_px: the price the order will pay
        mult = 100.0 if is_opt else 1.0
        unit_cost = (limit_px or 0.0) * mult
        equity = _f(equity)
        now = now or datetime.now()
        today = now.date() if isinstance(now, datetime) else now
        q0 = int(qty_today or 0)
        F = out["features"]
        exits = (cfg or {}).get("exits") or {}
        q_live, q_all = q0, q0
        sized_live, sized_all = q0, q0                       # after sizing, before the limits

        def rule(name, on, detail, *, live=False, would=False, effect="none", qty_after=None, gate=None, error=None):
            """Record one rule. `would` = it refuses the trade that all rules together would place;
            `live` = it refuses the trade as it is being placed now (only acted on when the rule is ON:
            then it is a block, or a warning for a hand approval). "ok" in the record is the verdict
            on the path that governs this rule: the live one when it is on, the all-rules one when off."""
            refused = bool(live) if on else bool(would)
            r = {"rule": name, "on": bool(on), "ok": not refused, "effect": effect, "detail": detail}
            if qty_after is not None:
                r["qty_after"] = qty_after
            if error:
                r["error"] = error
                out["fallback"].append(f"{name}: {error}")
            out["rules"].append(r)
            if would:
                out["would_block"].append(name)
            if on and live and gate:
                if manual and name != "risk_size":
                    out["warnings"].append(f"{gate_words(gate, sc)} — {detail}")
                else:
                    out["live_blocks"].append({"gate": gate, "detail": detail})
            return r

        def cap_rule(name, on, fn, text, same=""):
            """A rule that can make the order smaller: fn(quantity) -> the quantity it allows.
            Judged twice, on the order as it is being placed now and on the order all rules together
            would place. A path an earlier rule already refused is judged on its size before the
            limits, so this rule's own verdict is still recorded (and still blocks when it is ON)."""
            nonlocal q_live, q_all
            base_a = q_all if q_all >= 1 else max(1, int(sized_all or 0))
            base_l = q_live if q_live >= 1 else max(1, int(sized_live or 0))
            aft_a, aft_l = min(base_a, max(0, int(fn(base_a)))), min(base_l, max(0, int(fn(base_l))))
            if q_all >= 1:
                q_all = aft_a
            if on and not manual and q_live >= 1:
                q_live = aft_l
            b, a = (base_l, aft_l) if on else (base_a, aft_a)
            rule(name, on, text + (f" — {b} → {a}" if a < b else same), live=aft_l < 1, would=aft_a < 1,
                 effect=("block" if a < 1 else "shrink") if a < b else "none", qty_after=a if a < b else None, gate=name)

        # ── 1. size: stock by risk; an option by its spend limit (Step 4b) or, with options_by_spend off, by risk ──
        ru = {"ok": False, "why": "not worked out"}
        lu = {"ok": False, "why": "not worked out"}
        by_spend = bool(is_opt and sc["sizing"].get("options_by_spend", True) is True)
        try:
            ru = risk_per_unit(vehicle, limit_px, stop, sc, exits)
            budget = risk_budget(tier, sc, risk_mult)
            cap = (_f(usd_budget) or 0.0) * min(1.0, max(0.0, float(sc["sizing"].get("usd_cap_mult") or 1.0)))
            F.update({"risk_budget_usd": _r(budget), "usd_cap": _r(cap), "tier": tier})
            sp, q_sp = None, None
            if is_opt:                                       # Step 4b: the spend limit that applies, always recorded
                try:
                    dte = (date.fromisoformat(str(expiry)[:10]) - today).days
                except Exception:
                    dte = None
                sp = (pv["spend"](tier, dte) if pv.get("spend") else spend_limit(cfg, tier, dte))
                lim = min(cap, float(sp["usd"])) if sp else cap
                q_sp = int(math.floor(lim / unit_cost + 1e-9)) if unit_cost > 0 else 0
                F.update({"days_to_expiry": dte, "spend_limit_usd": _r(lim), "spend_table_usd": _r((sp or {}).get("usd")),
                          "spend_band": (sp or {}).get("band") or "no table: the dollar budget of the trade",
                          "spend_tier": ((sp or {}).get("tier") if tier else "learning (no level on the call)") if sp else tier,
                          "qty_by_spend": q_sp})
            if by_spend:
                out["size_basis"] = "spend"
                if ru.get("ok"):                             # the risk at the stop is still a fact worth keeping
                    F.update({"risk_per_unit_usd": _r(ru["usd"]), "stop_dist_pct": _r(ru["frac"] * 100.0, 3),
                              "risk_basis": ru["basis"],
                              "qty_by_risk": size_by_risk(unit_cost, ru["usd"], budget, cap, sc, True)["qty"]})
                F["size_bind"] = "spend"
                q_all = sized_all = q_sp
                hand_one = bool(sw["sizing"] and manual and q_sp < 1 and q0 >= 1)
                why = (f"one contract costs {_money(unit_cost)}, more than the {_money(lim)} Mason may spend on this "
                       f"option trade")
                if hand_one:                                 # Nico approves it himself: one, never refused
                    q_live = sized_live = 1
                    out["warnings"].append(f"{gate_words('spend_limit', sc)} — {why}. Placed by hand it is one contract only.")
                elif sw["sizing"]:
                    q_live = sized_live = q_sp
                where = (f"the limit for an option expiring in {sp['band']}" + (f" ({sp['days']} days away)" if sp.get("days") is not None else "")
                         + f" at the {F['spend_tier']} level" if sp else "the dollar budget of this trade (no spend table is set)")
                rule("spend_limit", sw["sizing"],
                     (f"{q_sp} = the most whole contracts of {_money(unit_cost)} that fit {_money(lim)}, {where}"
                      if q_sp >= 1 else f"{why} ({where})") + (" — approved by hand: one only" if hand_one else ""),
                     live=q_sp < 1 and not hand_one, would=q_sp < 1, effect="size",
                     qty_after=1 if hand_one else q_sp, gate="spend_limit")
                rule("risk_size", False, "not used for an option: it is sized by its spend limit, the premium paid is "
                                         "what can be lost (sizing.options_by_spend)")
            elif ru.get("ok"):
                sz = size_by_risk(unit_cost, ru["usd"], budget, cap, sc, is_opt)
                F.update({"risk_per_unit_usd": _r(ru["usd"]), "stop_dist_pct": _r(ru["frac"] * 100.0, 3),
                          "risk_basis": ru["basis"], "qty_by_risk": sz["qty"], "size_bind": sz["bind"]})
                q_all = sized_all = sz["qty"]
                hand_one = bool(sw["sizing"] and manual and sz["qty"] < 1 and q0 >= 1 and sz.get("bind") == "risk")
                if hand_one:                                 # (review) Nico approves it himself: one, never refused
                    q_live = sized_live = 1
                    out["warnings"].append(f"{GATE_WORDS['risk_size']} — {sz.get('why')}. Placed by hand it is "
                                           f"one {'contract' if is_opt else 'share'} only.")
                elif sw["sizing"]:
                    q_live = sized_live = sz["qty"]
                words = (f"{sz['qty']} = {_money(budget)} at risk / {_money(ru['usd'])} lost per "
                         f"{'contract' if is_opt else 'share'} at the stop ({ru['basis']}), at most {_money(cap)} in all"
                         + ({"cap": "; the dollar limit is what holds it", "one_unit": "; one is allowed although it "
                             "risks more than the amount"}.get(sz["bind"], "")))
                rule("risk_size", sw["sizing"],
                     words if sz["qty"] >= 1 else (sz.get("why") or words) + (" — approved by hand: one only" if hand_one else ""),
                     live=sz["qty"] < 1 and not hand_one, would=sz["qty"] < 1, effect="size",
                     qty_after=1 if hand_one else sz["qty"], gate="risk_size")
            else:
                rule("risk_size", sw["sizing"], f"sized the old way: {ru.get('why')}",
                     error=f"cannot size by risk ({ru.get('why')})")
        except Exception as e:
            q_live = q_all = sized_live = sized_all = q0
            rule("risk_size", sw["sizing"], "sized the old way", error=f"{type(e).__name__}: {e}")

        # ── 1b. hard ceiling on the loss at the stop (Step 4b) ──
        try:
            mlc = sc["limits"].get("max_loss") or {}
            pct = _f(mlc.get("max_pct"))
            lu = loss_per_unit(vehicle, limit_px, stop, exits)
            if lu.get("ok"):
                F["loss_per_unit_usd"] = _r(lu["usd"])
            unit_w = "contract" if is_opt else "share"
            if pct is None or pct <= 0:
                rule("max_loss", sw["max_loss"], "no ceiling is set (limits.max_loss.max_pct is null)")
            elif not equity or equity <= 0:
                rule("max_loss", sw["max_loss"], "not checked", error="the account value could not be read")
            elif not lu.get("ok") or not (lu["usd"] > 0):
                rule("max_loss", sw["max_loss"], "not checked",
                     error=f"the loss at the stop cannot be worked out ({lu.get('why') or 'no loss'})")
            else:
                most = equity * pct / 100.0
                fit = int(math.floor(most / lu["usd"] + 1e-9))
                F.update({"max_loss_usd": _r(most), "max_loss_pct": pct, "qty_by_max_loss": fit})
                base_a = q_all if q_all >= 1 else max(1, int(sized_all or 0))
                base_l = q_live if q_live >= 1 else max(1, int(sized_live or 0))
                aft_a, aft_l = min(base_a, fit), min(base_l, fit)
                on_ml = sw["max_loss"]
                hand_one = bool(on_ml and manual and aft_l < 1 and q_live >= 1)   # by hand: one, with a warning
                if q_all >= 1:
                    q_all = aft_a
                    if aft_a >= 1:
                        sized_all = aft_a
                if on_ml and q_live >= 1:
                    q_live = 1 if hand_one else aft_l
                    if q_live >= 1:
                        sized_live = q_live
                b, a = (base_l, aft_l) if on_ml else (base_a, aft_a)
                text = (f"{b} x {_money(lu['usd'])} lost per {unit_w} at the stop ({lu['basis']}) = {_money(b * lu['usd'])}; "
                        f"the most one trade may lose is {pct:g}% of the account ({_money(most)})")
                if a < b:
                    text += f" — {b} → {a}" if a >= 1 else f" — even one {unit_w} is over it"
                if hand_one:
                    out["warnings"].append(f"{gate_words('max_loss', sc)} — {text}. Placed by hand it is one {unit_w} only.")
                    text += " — approved by hand: one only"
                rule("max_loss", on_ml, text, live=aft_l < 1 and not hand_one, would=aft_a < 1,
                     effect=("block" if a < 1 else "shrink") if a < b else "none",
                     qty_after=((1 if hand_one else a) if a < b else None), gate="max_loss")
        except Exception as e:
            rule("max_loss", sw.get("max_loss"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── facts the limits need ──
        book = None
        try:
            book = pv.get("book")
            if book is None and (pos or {}).get("ok") and equity:
                book = book_snapshot(pos, equity, sc=sc, sector_fn=pv.get("sector"))
        except Exception as e:
            out["fallback"].append(f"book: {type(e).__name__}: {e}")
        sector = None
        try:
            sector = (pv["sector"](ticker) if pv.get("sector") else sector_of(ticker, sc, today=today))
        except Exception as e:
            out["fallback"].append(f"sector: {type(e).__name__}: {e}")
        F["sector"] = sector
        try:                                                 # held names with no known sector: asked once, for next time,
            if not pv and (book or {}).get("unknown_sector"):  # in the background (the order never waits for it)
                threading.Thread(target=warm_sectors, args=(list(book["unknown_sector"]), sc),
                                 name="sizing-warm", daemon=True).start()
        except Exception:
            pass
        sym = ((book or {}).get("symbols") or {}).get(ticker) or {}
        if book and book.get("ok") and equity:
            F.update({"symbol_held_usd": _r(sym.get(f"{side}_usd") or 0.0),
                      "symbol_held_pct": _r((sym.get(f"{side}_usd") or 0.0) / equity * 100.0),
                      "symbol_held_nico_usd": _r(sym.get(f"nico_{side}_usd") or 0.0),
                      "symbol_held_mason_usd": _r(sym.get(f"mason_{side}_usd") or 0.0)})
            ups = [ln.get("unrealized_pct") for ln in (sym.get("lines") or []) if ln.get("unrealized_pct") is not None]
            if ups:
                F["symbol_held_unrealized_pct"] = _r(min(ups))
            sec_row = (book.get("sectors") or {}).get(sector) if sector else None
            if sector:
                F.update({"sector_held_usd": _r((sec_row or {}).get(f"{side}_usd") or 0.0),
                          "sector_held_pct": _r(((sec_row or {}).get(f"{side}_usd") or 0.0) / equity * 100.0),
                          "sector_held_mason_usd": _r((sec_row or {}).get(f"mason_{side}_usd") or 0.0)})

        # ── 2a. earnings ──
        try:
            ec = sc["limits"]["earnings"]
            ne = (pv["earnings"](ticker, today) if pv.get("earnings")
                  else next_earnings(ticker, today, pv.get("holidays")))
            et = is_earnings_trade(rec, sc, pv.get("catalyst_kind"))
            F.update({"earnings_date": (ne or {}).get("date"), "earnings_timing": (ne or {}).get("timing"),
                      "sessions_to_earnings": (ne or {}).get("sessions"), "earnings_trade": bool(et)})
            hit, action = False, None
            if ne and ne.get("sessions") is not None:
                if is_opt:
                    win = ec.get("option_sessions_before")
                    try:                                     # (review) a report AFTER the close of the expiry day comes
                        tm_ = str(ne.get("timing") or "").lower()   # too late to touch the option; no known time = it may
                        after_close = tm_ == "pm" or "after" in tm_
                        rd_, xd_ = str(ne["date"])[:10], str(expiry)[:10]
                        spans = bool(expiry) and (rd_ < xd_ or (rd_ == xd_ and not after_close))
                    except Exception:
                        spans = False
                    F["expiry_spans_earnings"] = spans
                    hit = spans and (win is None or ne["sessions"] <= int(win))
                    action = str(ec.get("option_action") or "block")
                else:
                    hit = ne["sessions"] <= int(ec.get("sessions_before") or 0)
                    action = str(ec.get("stock_action") or "shrink")
            when = (f"{ticker} reports earnings on {ne['date']}"
                    + ({"am": " before the open", "pm": " after the close"}.get(str(ne.get('timing') or '').lower(), "")
                       if ne else "") + f", {ne['sessions']} trading session(s) away") if ne else \
                f"no earnings date is stored for {ticker}"
            if not hit:
                rule("earnings_window", sw["earnings"], when + (" — outside the window" if ne else ""))
            elif et:
                rule("earnings_window", sw["earnings"], when + " — the call says it is an earnings trade, so it is left alone")
            elif action == "block":
                cap_rule("earnings_window", sw["earnings"], lambda q: 0,
                      when + (f" and the option runs past it (expiry {str(expiry)[:10]})" if is_opt else "")
                      + " — not opened this close to the report")
            else:
                m = float(ec.get("shrink_mult") or 0.5)

                def shrink(q):
                    s_ = int(math.floor(q * m + 1e-9))
                    return max(1, s_) if (ec.get("keep_one", True) and q >= 1) else s_
                cap_rule("earnings_window", sw["earnings"], shrink, when + f" — size cut to {m * 100:.0f}%",
                      same=" (already the smallest size)")
        except Exception as e:
            rule("earnings_window", sw.get("earnings"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── 2b. symbol cap ──
        try:
            cc = sc["limits"]["symbol_cap"]
            if not (book and book.get("ok") and equity and unit_cost > 0):
                rule("symbol_cap", sw["symbol_cap"], "not checked", error="the account's positions could not be read")
            else:
                is_index = sector == BROAD
                pct = float(cc.get("index_max_pct") if is_index else cc.get("max_pct"))
                held = sym.get(f"{side}_usd") or 0.0
                room = pct / 100.0 * equity - held
                who = (f"{_money(sym.get(f'nico_{side}_usd') or 0)} is Nico's, "
                       f"{_money(sym.get(f'mason_{side}_usd') or 0)} is Mason's")
                cap_rule("symbol_cap", sw["symbol_cap"], lambda q: _room_qty(q, room, unit_cost),
                      f"the account holds {_money(held)} of {ticker} ({held / equity * 100:.1f}% of its value; {who}); "
                      f"the limit for one {'whole-market fund' if is_index else 'stock'} is {pct:g}% "
                      f"({_money(pct / 100.0 * equity)}), so {_money(max(0.0, room))} of room is left")
        except Exception as e:
            rule("symbol_cap", sw.get("symbol_cap"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── 2c. sector cap ──
        try:
            cc = sc["limits"]["sector_cap"]
            if not (book and book.get("ok") and equity and unit_cost > 0):
                rule("sector_cap", sw["sector_cap"], "not checked", error="the account's positions could not be read")
            elif not sector:
                rule("sector_cap", sw["sector_cap"], "not checked", error=f"no sector is known for {ticker}")
            elif sector in (cc.get("exempt") or []):
                rule("sector_cap", sw["sector_cap"], f"{ticker} is a {SECTOR_WORDS.get(sector, sector)} holding — no sector limit")
            else:
                g = (book.get("sectors") or {}).get(sector) or {}
                held, mine = g.get(f"{side}_usd") or 0.0, g.get(f"mason_{side}_usd") or 0.0
                rooms = []
                if _f(cc.get("max_pct")) is not None:
                    rooms.append((float(cc["max_pct"]) / 100.0 * equity - held,
                                  f"everything in the account in this sector is {_money(held)} "
                                  f"({held / equity * 100:.1f}%; limit {float(cc['max_pct']):g}%)"))
                if _f(cc.get("mason_max_pct")) is not None:
                    rooms.append((float(cc["mason_max_pct"]) / 100.0 * equity - mine,
                                  f"Mason's own trades in this sector are {_money(mine)} "
                                  f"({mine / equity * 100:.1f}%; limit {float(cc['mason_max_pct']):g}%)"))
                room = min([r_ for r_, _ in rooms]) if rooms else None
                cap_rule("sector_cap", sw["sector_cap"], lambda q: _room_qty(q, room, unit_cost),
                      f"{SECTOR_WORDS.get(sector, sector)} ({sector}): " + "; ".join(t_ for _, t_ in rooms))
        except Exception as e:
            rule("sector_cap", sw.get("sector_cap"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── 3a. exit plan ──
        try:
            missing = [w for w, v in (("stop", stop), ("target", target)) if v is None]
            rule("exit_plan", sw["require_levels"],
                 "stop and target are both given" if not missing else f"the call gives no {' and no '.join(missing)}",
                 live=bool(missing), would=bool(missing), effect="block" if missing else "none", gate="exit_plan")
        except Exception as e:
            rule("exit_plan", sw.get("require_levels"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── 3b. reward to risk, expected value ──
        rr = None
        try:
            if limit_px and stop is not None and target is not None and limit_px > stop and target > limit_px:
                rr = (target - limit_px) / (limit_px - stop)
            F["reward_risk"] = _r(rr, 3)
            mc = sc["value_gate"]["min_reward_risk"]
            if rr is None:
                rule("reward_risk", sw["min_reward_risk"], "not checked: it needs a stop below and a target above the entry")
            else:
                need = float(mc.get("value") or 0.0)
                low = rr < need - 1e-9
                rule("reward_risk", sw["min_reward_risk"],
                     f"possible gain {_money((target - limit_px) * mult)} against possible loss "
                     f"{_money((limit_px - stop) * mult)} per {'contract' if is_opt else 'share'} = {rr:.2f} to 1 "
                     f"(at least {need:g} to 1 is asked)",
                     live=low, would=low, effect="block" if low else "none", gate="reward_risk")
        except Exception as e:
            rule("reward_risk", sw.get("min_reward_risk"), "not checked", error=f"{type(e).__name__}: {e}")
        spread_pct = None
        try:
            q = quote or {}
            spread_pct = _f(q.get("spread_pct"))
            bid, ask = _f(q.get("bid")), _f(q.get("ask"))
            if spread_pct is None and bid and ask and ask >= bid:
                spread_pct = (ask - bid) / ((ask + bid) / 2.0) * 100.0
            F.update({"spread_pct": _r(spread_pct, 3), "quote_source": q.get("source")})
            if is_opt:
                iv = norm_iv(q.get("iv"))
                if iv is None:
                    iv = norm_iv((((rec or {}).get("extra") or {}).get("trade") or {}).get("contract_quote", {}).get("iv"))
                    if iv is not None:
                        F["iv_source"] = "the call's stored contract quote"
                F.update({"iv_pct": _r(iv), "delta": _r(_f(q.get("delta")), 3), "open_interest": _f(q.get("oi"))})
        except Exception as e:
            out["fallback"].append(f"quote facts: {type(e).__name__}: {e}")
        try:
            mc = sc["value_gate"]["min_expected_value"]
            pp = _f(p)
            if rr is None or pp is None or not (0.0 < pp < 1.0):
                rule("expected_value", sw["min_expected_value"], "not checked: it needs a stop, a target and a probability")
            else:
                need = float(mc.get("usd") or 0.0)

                def worth(cands):
                    """(expected dollars, gain, loss, cost, quantity) on the first real quantity of `cands`: a
                    size another rule cut to nothing falls back to the size before the limits, so this rule
                    is judged on a real position and not on a single share."""
                    n = next((int(x) for x in cands if x and x >= 1), 1)
                    gain, loss = (target - limit_px) * mult * n, (limit_px - stop) * mult * n
                    cost = (spread_pct or 0.0) / 100.0 * limit_px * mult * n      # half the spread in, half out
                    return pp * gain - (1.0 - pp) * loss - cost, gain, loss, cost, n
                wl, wa = worth((q_live, sized_live, q0)), worth((q_all, sized_all, q0))
                F.update({"p_used": _r(pp, 4), "expected_value_usd": _r(wl[0]), "est_cost_usd": _r(wl[3]),
                          "expected_value_all_rules_usd": _r(wa[0])})
                ev, gain, loss, cost, n = wl if sw["min_expected_value"] else wa
                rule("expected_value", sw["min_expected_value"],
                     f"{pp * 100:.0f}% chance of +{_money(gain)}, {100 - pp * 100:.0f}% chance of -{_money(loss)}, "
                     f"about {_money(cost)} of trading cost = {'+' if ev >= 0 else '-'}{_money(abs(ev))} on average "
                     f"for {n} (at least {_money(need)} is asked)",
                     live=wl[0] < need - 1e-9, would=wa[0] < need - 1e-9,
                     effect="block" if ev < need - 1e-9 else "none", gate="expected_value")
        except Exception as e:
            rule("expected_value", sw.get("min_expected_value"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── 3c. already moved ──
        try:
            pc = sc["value_gate"]["priced_in"]
            ti = today.isoformat()
            if pv.get("closes"):
                closes = pv["closes"](ticker, ti, int(pc.get("lookback_sessions") or 20) + 1)
                closes = closes if _fresh(closes, today, None, pv.get("holidays")) else []
            else:
                closes = recent_closes(ticker, today, pc)
            nm = normal_move(closes, pc.get("min_sessions") or 10) if closes else None
            if is_opt:
                upx = pv["underlying_px"](ticker) if pv.get("underlying_px") else _underlying_px(ticker, cfg)
            else:
                upx = _f((quote or {}).get("px")) or _f((quote or {}).get("last")) or limit_px
            prev = closes[-1][1] if closes else None
            sign = -1.0 if side == "short" else 1.0
            mv_today = (upx / prev - 1.0) * 100.0 if (upx and prev) else None
            ev_px, ev_date, ev_n = (pv["event_ref"](rec, today) if pv.get("event_ref") else event_ref(rec, today))
            mv_event = (upx / ev_px - 1.0) * 100.0 if (upx and ev_px) else None
            F.update({"underlying_px": _r(upx, 4), "prev_close": _r(prev, 4), "move_since_prev_close_pct": _r(mv_today, 3),
                      "move_since_event_pct": _r(mv_event, 3), "event_ref_date": ev_date,
                      "normal_daily_move_pct": _r(nm, 3)})
            ratios = []
            if mv_today is not None and nm:
                ratios.append((sign * mv_today / nm, f"{mv_today:+.1f}% since yesterday's close"))
            if mv_event is not None and nm and ev_n:
                ratios.append((sign * mv_event / (nm * math.sqrt(ev_n)),
                               f"{mv_event:+.1f}% in the {ev_n} session(s) since the news first appeared"))
            iv = F.get("iv_pct")
            if is_opt and iv:
                imp_day = iv / math.sqrt(252.0)
                F["implied_daily_move_pct"] = _r(imp_day, 3)
                try:
                    dte = (date.fromisoformat(str(expiry)[:10]) - today).days
                    F["implied_move_to_expiry_pct"] = _r(iv * math.sqrt(max(dte, 1) / 365.0), 3)
                except Exception:
                    pass
                if mv_today is not None:
                    F["move_vs_implied_day"] = _r(sign * mv_today / imp_day, 3)
            snap_r = (_snap_ratios(F, ticker, side, now, pc, pv, closes, mv_today, mv_event, ev_date, ev_n, nm, sign)
                      if pc.get("use_snapshots") is True else [])          # Job C: [] = no snapshot, judged as before
            if snap_r:
                ratio, words_, yard = max(snap_r, key=lambda x: x[0])
                F["move_vs_normal"] = _r(max(ratios, key=lambda x: x[0])[0], 3) if ratios else None
                F["priced_in_yardstick"] = yard
                lim = float(pc.get("option_max_mult") if is_opt else pc.get("stock_max_mult"))
                far = ratio >= lim
                # review 2026-11-17: the option's own price in the SAME unit as the yardstick (an average day,
                # up or down = implied volatility / sqrt(252) x sqrt(2/pi)); the old wording's "% a day" is one
                # standard deviation (no sqrt(2/pi)), which set next to the yardstick made the jump look 25% bigger
                rule("priced_in", sw["priced_in"],
                     words_ + (f"; the option itself is priced at implied volatility {iv:.0f}%, an average day of "
                               f"about {iv / math.sqrt(252.0) * _AVG_DAY:.1f}%" if is_opt and iv else "")
                     + f" (limit {lim:g} times)",
                     live=far, would=far, effect="block" if far else "none", gate="priced_in")
            elif not ratios:
                F["move_vs_normal"] = None
                rule("priced_in", sw["priced_in"], "not checked",
                     error=("no recent daily prices for this stock (the stored prices do not reach the last session)"
                            if not nm else "no live price to compare"))
            else:
                ratio, what = max(ratios, key=lambda x: x[0])
                F["move_vs_normal"] = _r(ratio, 3)
                lim = float(pc.get("option_max_mult") if is_opt else pc.get("stock_max_mult"))
                dirw = "down" if side == "short" else "up"
                far = ratio >= lim
                rule("priced_in", sw["priced_in"],
                     f"{ticker} is {what}; a normal day for it is {nm:.1f}%. That is {max(ratio, 0):.1f} times a "
                     f"normal move {dirw}, the way this trade points"
                     + (f"; the option prices in about {F['implied_daily_move_pct']:.1f}% a day "
                        f"(implied volatility {iv:.0f}%)" if is_opt and iv else "")
                     + f" (limit {lim:g} times)",
                     live=far, would=far, effect="block" if far else "none", gate="priced_in")
        except Exception as e:
            rule("priced_in", sw.get("priced_in"), "not checked", error=f"{type(e).__name__}: {e}")

        # ── result ──
        out["qty"] = int(q_live)
        out["qty_all_rules"] = 0 if out["would_block"] else int(q_all)
        if out["live_blocks"]:
            out["qty"] = 0                                            # refused: the reasons are in live_blocks
        elif out["qty"] < 1 and q0 >= 1:
            out["qty"] = q0                                           # never to zero without a recorded reason
            out["fallback"].append("a rule cut the size to zero without a block reason; today's size kept")
        qn = out["qty"] if out["qty"] >= 1 else 0
        if ru.get("ok"):
            F.update({"risk_usd": _r(ru["usd"] * qn), "risk_today_usd": _r(ru["usd"] * q0),
                      "risk_all_rules_usd": _r(ru["usd"] * out["qty_all_rules"])})
        if lu.get("ok"):                                              # Step 4b: what the ceiling is measured on
            F.update({"loss_at_stop_usd": _r(lu["usd"] * qn), "loss_at_stop_today_usd": _r(lu["usd"] * q0),
                      "loss_at_stop_all_rules_usd": _r(lu["usd"] * out["qty_all_rules"])})
            if equity:
                F["loss_at_stop_pct_of_account"] = _r(lu["usd"] * qn / equity * 100.0, 3)
        F.update({"qty": qn, "qty_today": q0, "qty_all_rules": out["qty_all_rules"],
                  "cost_usd": _r(unit_cost * qn), "vehicle": vehicle, "at": now.isoformat(timespec="seconds")
                  if isinstance(now, datetime) else str(now)})
        out["changed"] = out["qty"] != q0
    except Exception as e:                                             # the whole plan failed: today's sizing
        out.update(qty=qty_today, qty_all_rules=qty_today, live_blocks=[], changed=False)
        out["fallback"].append(f"plan: {type(e).__name__}: {e}")
    finally:
        _tl.deadline = None
    return out


def _underlying_px(ticker, cfg):
    """The stock price behind an option order (one extra quote), waited for inside the order's time budget."""
    try:
        import mason_autotrade as AT
        q = _timed(lambda: AT._trusted_quote(ticker, False, cfg, any_age=True), _wait_s(8.0), "sizing-underlying") or {}
        return _f(q.get("px")) or _f(q.get("last")) or _f(q.get("ask"))
    except Exception:
        return None


# ── Job C (2026-11-17): the options market's yardstick for the already-moved check ──────────────────────────────────
# Today's yardstick is the stock's own normal day: the average size (up or down) of its last 20 daily moves. With
# value_gate.priced_in.use_snapshots on, the yardstick is what the options market was pricing BEFORE the move: the
# at-the-money implied volatility of the newest stored option snapshot read at or before the close the move is
# measured from (and never after the decision), turned into the same kind of number — an average day's size. A day
# whose ups and downs follow the bell curve with spread s averages s x sqrt(2/pi), about 0.8 s, and implied volatility
# gives s for one day as IV / sqrt(252). So the ratio keeps its meaning ("so many normal days") and the limits (6 for
# a stock, 2 for an option) keep theirs. A snapshot read after the move began is not used: a crash lifts implied
# volatility at once (the SMCI put was bought at about 90%), and measuring the drop against it would excuse the very
# chase the check is there to catch.

_AVG_DAY = math.sqrt(2.0 / math.pi)


def _close_ts(day_iso):
    """16:00 New York time on `day_iso`, as epoch seconds (None when unreadable)."""
    try:
        from zoneinfo import ZoneInfo
        d = date.fromisoformat(str(day_iso)[:10])
        return datetime(d.year, d.month, d.day, 16, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    except Exception:
        return None


def _decision_ts(now):
    """The decision time as epoch seconds: an aware time as it is; a naive one read as New York time (Mason's
    convention, the same as mason_option_snaps); a bare date as 00:00 New York time that day. None otherwise."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        if isinstance(now, datetime):
            return (now if now.tzinfo else now.replace(tzinfo=et)).timestamp()
        if isinstance(now, date):
            return datetime(now.year, now.month, now.day, tzinfo=et).timestamp()
    except Exception:
        pass
    return None


def snap_yardstick(ticker, ref_day, now, pc, pv=None):
    """An average day's move (percent, up or down) as the options market priced it for `ticker` at the close of
    `ref_day`: from the newest stored option snapshot read at or before that close, and never after the decision
    time `now`. None when there is none to use (no snapshot, older than snapshot_max_age_minutes at that close,
    quotes too wide): the caller keeps the stock's past days. Stored rows only, no network, at most a 1-second wait
    for a database lock. `pv["implied_move"](ticker, as_of_epoch, max_age_minutes)` replaces the look-up in tests."""
    try:
        at, dec = _close_ts(ref_day), _decision_ts(now)
        if at is None or dec is None:
            return None
        at = min(at, dec)
        age = float(pc.get("snapshot_max_age_minutes") or 390)
        if (pv or {}).get("implied_move"):
            s = pv["implied_move"](ticker, at, age)
        else:
            import mason_option_snaps as OS
            s = OS.implied_move_stored(ticker, at, max_age_minutes=age)
        if not s or not _f(s.get("atm_iv_pct")) or _f(s.get("ts")) is None or float(s["ts"]) > at:
            return None
        ivp = float(s["atm_iv_pct"])
        return {"avg_day_pct": ivp / math.sqrt(252.0) * _AVG_DAY, "atm_iv_pct": ivp, "ts": int(s["ts"]),
                "age_minutes": s.get("age_minutes"), "as_of": int(at)}
    except Exception:
        return None


def _snap_ratios(F, ticker, side, now, pc, pv, closes, mv_today, mv_event, ev_date, ev_n, nm, sign) -> list:
    """The already-moved check's parts, (ratio, words, yardstick), each measured against the options market's
    average day where a snapshot is stored and against the stock's own normal day (nm) where not. [] when no part
    has a snapshot: the check is then judged exactly as before. Writes the snapshot facts into F. Never raises."""
    out, used = [], False
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        dirw = "down" if side == "short" else "up"

        def words(what, y, r):
            if y:
                t_ = datetime.fromtimestamp(y["ts"], et)
                return (f"{ticker} is {what}; before that move (option prices read {t_:%a %d %b} at {t_:%H:%M} New "
                        f"York time) the options market priced an average day for it at {y['avg_day_pct']:.1f}% "
                        f"(implied volatility {y['atm_iv_pct']:.0f}%)"
                        + (f"; its own past days give {nm:.1f}%" if nm else "")
                        + f". That is {max(r, 0):.1f} times that average day {dirw}, the way this trade points")
            return (f"{ticker} is {what}; a normal day for it is {nm:.1f}%. That is {max(r, 0):.1f} times a "
                    f"normal move {dirw}, the way this trade points")

        y = snap_yardstick(ticker, closes[-1][0], now, pc, pv) if (mv_today is not None and closes) else None
        F.update({"snap_prev_close_iv_pct": _r((y or {}).get("atm_iv_pct")),
                  "snap_prev_close_avg_day_pct": _r((y or {}).get("avg_day_pct"), 3),
                  "snap_prev_close_read_at": datetime.fromtimestamp(y["ts"], et).isoformat() if y else None,
                  "snap_prev_close_age_minutes": (y or {}).get("age_minutes"),
                  "move_vs_options": _r(sign * mv_today / y["avg_day_pct"], 3) if y else None,
                  "priced_in_yardstick": None})
        if mv_today is not None and (y or nm):
            r = sign * mv_today / (y["avg_day_pct"] if y else nm)
            out.append((r, words(f"{mv_today:+.1f}% since yesterday's close", y, r), "options" if y else "past days"))
            used = used or bool(y)
        if mv_event is not None and ev_n:
            ye = snap_yardstick(ticker, ev_date, now, pc, pv) if ev_date else None
            F.update({"snap_event_iv_pct": _r((ye or {}).get("atm_iv_pct")),
                      "move_vs_options_event": _r(sign * mv_event / (ye["avg_day_pct"] * math.sqrt(ev_n)), 3) if ye else None})
            if ye or nm:
                r = sign * mv_event / ((ye["avg_day_pct"] if ye else nm) * math.sqrt(ev_n))
                out.append((r, words(f"{mv_event:+.1f}% in the {ev_n} session(s) since the news first appeared", ye, r),
                            "options" if ye else "past days"))
                used = used or bool(ye)
        if not used:
            F["priced_in_yardstick"] = "past days (no option snapshot)"
    except Exception as e:
        F["snap_error"] = f"{type(e).__name__}: {e}"
        return []
    return out if used else []


def record(pl: dict) -> dict:
    """The compact form kept on the call and the trade (execution.features → agent_trades.features_json)."""
    try:
        return {"entry_features": dict(pl.get("features") or {}),
                "sizing_plan": {"v": pl.get("v"), "shadow": pl.get("shadow"),
                                "switches_on": sorted(k for k, v in (pl.get("switches") or {}).items() if v),
                                "qty_today": pl.get("qty_today"), "qty_placed": pl.get("qty"),
                                "qty_all_rules": pl.get("qty_all_rules"),
                                "would_block": list(pl.get("would_block") or []),
                                "blocked_by": [b["gate"] for b in (pl.get("live_blocks") or [])],
                                "rules": [{k: r.get(k) for k in ("rule", "on", "ok", "effect", "qty_after", "detail", "error")
                                           if r.get(k) is not None} for r in (pl.get("rules") or [])],
                                "fallback": list(pl.get("fallback") or []),
                                **({"size_basis": pl["size_basis"]} if pl.get("size_basis") else {}),
                                **({"manual": True} if pl.get("manual") else {})}}
    except Exception as e:
        return {"sizing_plan": {"fallback": [f"record: {type(e).__name__}: {e}"]}}


# ── model-visible text: the book and the limits ──────────────────────────────────

_NON_ALPACA = ("roth", "personal account", "brokerage csv", "other account", "another account", "outside account",
               "brokerage account", "investing account")   # fallback only; mason_execution.is_alpaca_row rules


def _journal_stops() -> dict:
    """{TICKER: (stop, target)} from Nico's open journal rows held in the paper account: the first row
    with a stop. Mason's own rows (linked to a director call) and rows whose text names another
    account are left out — the same reading mason_protect uses."""
    out = {}
    try:
        try:
            import mason_execution
            _alpaca = mason_execution.is_alpaca_row          # the one shared rule (books fix 2026-11-15)
        except Exception:
            def _alpaca(row):
                txt = " ".join(str(row.get(k) or "") for k in ("notes", "thesis", "setup_type")).lower()
                return not any(m in txt for m in _NON_ALPACA)
        try:
            mids = {str(r["journal_id"]) for r in _db_rows(
                "SELECT journal_id FROM agent_trades WHERE source='mason' AND journal_id IS NOT NULL")}
        except Exception:
            mids = set()
        for r in _db_rows("SELECT * FROM trades WHERE status='open' AND COALESCE(is_deleted,0)=0 ORDER BY entry_date"):
            t = str(r.get("ticker") or "").upper()
            s = _f(r.get("stop_loss"))
            if not t or not s or t in out or str(r.get("id")) in mids:
                continue
            if r.get("director_rec_id") or str(r.get("setup_type") or "").lower() == "director":
                continue
            if not _alpaca(r):
                continue
            out[t] = (s, _f(r.get("target_price")))
    except Exception:
        pass
    return out


def _mason_rows() -> dict:
    out = {}
    try:
        for r in _db_rows("SELECT ticker, occ, qty, entry_px, stop, target, time_stop_date FROM agent_trades WHERE "
                          "source='mason' AND status='open' AND COALESCE(record_deleted,0)=0"):
            out.setdefault(str(r.get("occ") or r.get("ticker") or "").upper(), r)
    except Exception:
        pass
    return out


def _g(v) -> str:
    v = _f(v)
    return "n/a" if v is None else (f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}")


def _qty(v) -> str:
    v = _f(v) or 0.0
    return f"{v:g}"


def _opt_words(occ) -> str:
    m = _OCC.match(occ)
    if not m:
        return occ
    return (f"{m.group(1)} {'call' if m.group(5) == 'C' else 'put'}, strike {int(m.group(6)) / 1000.0:g}, "
            f"expires 20{m.group(2)}-{m.group(3)}-{m.group(4)}")


def limits_lines(sc=None, cfg=None) -> list:
    """The rules that are switched ON, one plain sentence each. [] when none is on."""
    sc = sc or scfg(cfg)
    sw = switches(sc)
    L = []
    s, lim, vg = sc["sizing"], sc["limits"], sc["value_gate"]
    table = s.get("show_spend_table") is True                # Step 4b wording; off = the lines below, unchanged
    if table:
        L += spend_lines(sc, cfg)
    elif sw["sizing"] and s.get("options_by_spend", True) is not True:
        # (review) The 11-15 "Size" line says an option is sized by the amount at risk, one contract up to
        # $325. That is true only under the earlier option rule (options_by_spend false). With options
        # sized by their spend limit it would tell the models something false, so it is left out until
        # the spend-table wording is switched on (sizing.show_spend_table), which says how both are sized.
        ru = s.get("risk_usd") or {}
        L.append(f"- Size: Mason buys as many shares or contracts as would lose about {_money(ru.get('learning'))} if the "
                 f"stop is hit (the amount for the smallest size of trade), and never spends more than the dollar limit per "
                 f"trade. A wide stop therefore means a smaller position. For an option the loss is counted from the "
                 f"premium paid down to your stop premium. A single share is still bought when it would lose up to "
                 f"{_money((_f(ru.get('learning')) or 0) * float(s.get('one_unit_max_risk_mult') or 1))}, a single option "
                 f"contract when it would lose up to "
                 f"{_money((_f(ru.get('learning')) or 0) * float(s.get('one_contract_max_risk_mult') or 1))}; above that the "
                 f"trade is not placed.")
    if sw["symbol_cap"]:
        c = lim["symbol_cap"]
        L.append(f"- One stock: Nico's and Mason's holdings of the same stock together may not exceed {float(c['max_pct']):g}% "
                 f"of the account ({float(c['index_max_pct']):g}% for a whole-market fund such as SPY or QQQ). A new trade is "
                 f"cut to the room that is left, or not placed when there is none.")
    if sw["sector_cap"]:
        c = lim["sector_cap"]
        L.append(f"- One sector: everything in the account in one sector may not exceed {float(c['max_pct']):g}% of the "
                 f"account, and Mason's own trades in one sector may not exceed {float(c['mason_max_pct']):g}%. "
                 f"Whole-market, bond and commodity funds are not counted.")
    if sw["earnings"]:
        c = lim["earnings"]
        st = ("is cut to " + f"{float(c['shrink_mult']) * 100:.0f}% of its size" if c.get("stock_action") != "block"
              else "is not placed")
        ow = c.get("option_sessions_before")
        ot = ("is cut to " + f"{float(c['shrink_mult']) * 100:.0f}% of its size" if c.get("option_action") != "block"
              else "is not placed")
        L.append(f"- Earnings: a new stock position opened {int(c['sessions_before'])} trading sessions or fewer before the "
                 f"company reports {st}. An option that expires after a report "
                 + (f"due within {int(ow)} trading sessions " if ow is not None else "") + f"{ot}. "
                 f"If you mean to hold through the report on purpose, write the words \"earnings trade\" in the thesis "
                 f"and the rule is not applied.")
    if sw["require_levels"]:
        L.append("- Exit plan: a long or short call with no stop or no target is not placed.")
    if sw["min_reward_risk"]:
        v = float(vg["min_reward_risk"]["value"])
        L.append("- Gain against loss: the distance from entry to target must be at least "
                 + ("as large as" if abs(v - 1.0) < 1e-9 else f"{v:g} times") + " the distance from entry to stop.")
    if sw["min_expected_value"]:
        L.append(f"- Worth placing: chance of winning x gain, minus chance of losing x loss, minus the trading cost, must "
                 f"come to at least {_money(vg['min_expected_value']['usd'])}.")
    if sw["priced_in"]:
        c = vg["priced_in"]
        L.append(f"- Already moved: a stock trade is not placed when the price has already gone {float(c['stock_max_mult']):g} "
                 f"times its normal daily move in the trade's direction (since yesterday's close, or since the news first "
                 f"appeared); for an option the limit is {float(c['option_max_mult']):g} times.")
    return L


def _spend_facts(sc, cfg) -> dict:
    """What the Step 4b wording needs: the dollar limits in force for the smallest size of trade, the
    larger ones, and whether those are still locked. Never raises (missing pieces come back as None)."""
    out = {"stock_usd": None, "stock_pct": None, "rows": None, "locked": True, "own_n": None, "need": None,
           "tiers": {}, "min_p": {}, "call_cap": None, "put_cap": None}
    try:
        if cfg is None:
            import mason_autotrade as AT
            cfg = AT._at_cfg()
        tiers = (cfg or {}).get("tiers") or {}
        out["tiers"] = {k: _f((tiers.get(k) or {}).get("usd_max")) for k in ("learning", "normal", "high")}
        out["min_p"] = {k: _f((tiers.get(k) or {}).get("min_p")) for k in ("learning", "normal", "high")}
        out["stock_usd"] = out["tiers"].get("learning")
        out["stock_pct"] = _f((tiers.get("learning") or {}).get("max_pct"))
        out["call_cap"], out["put_cap"] = _f((cfg or {}).get("call_premium_cap_usd")), _f((cfg or {}).get("put_premium_cap_usd"))
        import mason_agent_loop as AL
        out["rows"] = AL.option_spend_rows(cfg)
        out["need"] = int((cfg or {}).get("normal_requires_own_n", 10))
        out["own_n"] = int(AL.own_graded_n())
        out["locked"] = out["own_n"] < out["need"]
    except Exception:
        pass
    return out


def _bands(rows, col, cap=None) -> str:
    """"$1,000 when it expires in 0-5 days and $1,250 in 6 days or more": the amounts of one column,
    neighbouring bands with the same amount written as one."""
    runs = []
    for r in rows:
        usd = min(r[col], cap) if cap is not None else r[col]
        if runs and abs(runs[-1][0] - usd) < 0.005:
            runs[-1][2] = r["max_days"]
        else:
            runs.append([usd, r["min_days"], r["max_days"]])
    bits = []
    for i, (usd, lo, hi) in enumerate(runs):
        band = f"{lo} days or more" if hi is None else f"{lo}-{hi} days"
        bits.append(f"{_money(usd)} " + ("when it expires in " if i == 0 else "in ") + band)
    return bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]


def spend_lines(sc, cfg=None) -> list:
    """Step 4b wording (sizing.show_spend_table): the dollars per trade, how a stock and an option are
    sized, and the loss ceiling when it is on. One plain sentence group per line, each starting "- "."""
    f = _spend_facts(sc, cfg)
    s, sw = sc["sizing"], switches(sc)
    ru = s.get("risk_usd") or {}
    by_spend = s.get("options_by_spend", True) is True
    L = []
    stock = f"a stock trade gets up to {_money(f['stock_usd'])}"
    pct_w = (f" No trade of this size gets more than {f['stock_pct']:g}% of the account." if f["stock_pct"] is not None else "")
    if f["rows"]:
        opt = (f"An option trade gets as many contracts as fit its spend limit, which depends on how soon the option "
               f"expires: {_bands(f['rows'], 'learning', f['stock_usd'])}. One contract that costs more than its limit "
               f"is not bought.")
    else:
        opt = (f"An option trade gets up to {_money(f['call_cap'])} for calls and {_money(f['put_cap'])} for puts, so "
               f"one contract must cost less than that.")
    more = ""
    larger = s.get("show_larger_sizes") is True              # off: the models are never told larger sizes exist
    if not larger:
        pass
    elif f["locked"] and f["need"] is not None:
        more = (f" These are the amounts for now. Larger ones for the calls Mason rates highest stay locked until "
                f"Mason has closed {f['need']} trades of its own (so far {f['own_n']}).")
    elif (f["need"] is not None and f["rows"] and f["tiers"].get("normal") and f["tiers"].get("high")
          and f["min_p"].get("normal") and f["min_p"].get("high")):
        more = (f" Those are the amounts for the smallest size of trade. A call Mason rates "
                f"{f['min_p']['normal'] * 100:.0f}% or better may get up to "
                f"{_money(f['tiers']['normal'])} of stock, or for an option {_bands(f['rows'], 'normal', f['tiers']['normal'])}; "
                f"one it rates {f['min_p']['high'] * 100:.0f}% or better up to {_money(f['tiers']['high'])} of stock, or for an "
                f"option {_bands(f['rows'], 'high', f['tiers']['high'])}.")
    L.append(f"- Dollars per trade: {stock}. {opt}{pct_w}{more}")
    if sw["sizing"]:
        amt = _f(ru.get("learning")) or 0.0
        txt = (f"- Size of a stock trade: inside that dollar limit Mason buys as many shares as would lose about "
               f"{_money(amt)} if the stop is hit, so a wide stop means a smaller position. A single share is still "
               f"bought when it would lose up to {_money(amt * float(s.get('one_unit_max_risk_mult') or 1))}; above "
               f"that the trade is not placed.")
        if larger and not f["locked"] and _f(ru.get("normal")) and _f(ru.get("high")):
            txt += f" (For the two larger sizes of trade the amount is {_money(ru.get('normal'))} and {_money(ru.get('high'))}.)"
        if by_spend:
            txt += (" An option is not sized from its stop: the premium paid is what an option trade can lose, so the "
                    "spend limit above is its size.")
        else:
            txt += (f" An option is sized the same way, from the premium paid down to your stop premium; a single "
                    f"contract is still bought when it would lose up to "
                    f"{_money(amt * float(s.get('one_contract_max_risk_mult') or 1))}.")
        L.append(txt)
    if sw["max_loss"]:
        pct = _f((sc["limits"].get("max_loss") or {}).get("max_pct"))
        if pct:
            L.append(f"- Most a trade may lose: no trade may lose more than {pct:g}% of the account at its stop. For a "
                     f"stock that is the fall from the entry to your stop; for an option, the fall from the premium paid "
                     f"to your stop premium, or the whole premium when you give no stop. Mason makes the trade smaller "
                     f"to fit, or does not place it when even one share or one contract would lose more.")
    return L


def size_words(cfg, old: str) -> str:
    """The sentence about position size in the director's "what works" record. `old` (today's sentence)
    comes back unchanged unless size-by-risk is ON and the model text switch is ON — or, Step 4b, the
    spend-table wording is switched on (sizing.show_spend_table), which replaces both."""
    try:
        sc = scfg(cfg)
        if sc["sizing"].get("show_spend_table") is True:
            f = _spend_facts(sc, cfg)
            if f["stock_usd"] is None:
                return old
            if f["rows"]:
                opt = f"an option trade up to {_bands(f['rows'], 'learning', f['stock_usd'])}"
            else:
                opt = f"an option trade up to {_money(f['call_cap'])} for calls and {_money(f['put_cap'])} for puts"
            txt = (f"Size is set by Mason: a stock trade gets up to {_money(f['stock_usd'])}, {opt}"
                   + (f" (never more than {f['stock_pct']:g}% of the account)" if f["stock_pct"] is not None else "")
                   + ", so one option contract must cost less than that.")
            if _on(sc["sizing"]):
                amt = _f((sc["sizing"].get("risk_usd") or {}).get("learning")) or 0.0
                txt += (f" Shares are bought so that about {_money(amt)} is lost if your stop is hit"
                        + ("; an option is not sized from its stop." if sc["sizing"].get("options_by_spend", True) is True
                           else "; an option the same way, from the premium to your stop premium."))
            if sc["sizing"].get("show_larger_sizes") is True and f["locked"] and f["need"] is not None:
                txt += f" Larger amounts stay locked until Mason has closed {f['need']} trades of its own (so far {f['own_n']})."
            return txt
        if not (_on(sc["sizing"]) and sc["sizing"].get("show_book") is True):
            return old
        if sc["sizing"].get("options_by_spend", True) is True:
            return old            # (review) the 11-15 sentence describes the earlier option rule ($65, one contract up
                                  # to $325); with options sized by their spend limit it would be false, so today's
                                  # sentence stays until sizing.show_spend_table is switched on
        s = sc["sizing"]
        t = ((cfg or {}).get("tiers") or {}).get("learning") or {}
        amt = _f((s.get("risk_usd") or {}).get("learning")) or 0.0
        return (f"Size is set by Mason from your stop: it buys as many shares or contracts as would lose about "
                f"{_money(amt)} if the stop is hit, and never spends more than ${t.get('usd_max')} on one trade "
                f"(calls <= ${(cfg or {}).get('call_premium_cap_usd')}, puts <= ${(cfg or {}).get('put_premium_cap_usd')}). "
                f"One option contract must cost less than that and lose no more than "
                f"{_money(amt * float(s.get('one_contract_max_risk_mult') or 1))} at your stop premium.")
    except Exception:
        return old


_BOOK_TOKENS, _ONE_STOCK_TOKENS = 1300, 900    # the room mason_director gives this section (whole book / one stock)


def book_block(*, cfg=None, pos=None, acct=None, tickers=None, max_symbols=24, small_pct=1.0, max_tokens=None,
               _fold=True) -> dict:
    """{"title", "body", "symbols"} — the limits and what is held, in plain words. body "" when the
    switch is off or the account cannot be read. `tickers` (quick checks): only these symbols are
    listed one by one; the sector totals always cover the whole account. Holdings under `small_pct`
    percent of the account share one line.
    (review) The body stays inside `max_tokens` (default: the room the director gives the section, 1,300
    tokens, 900 for one stock; 0 = no limit) instead of being cut in the middle of a sentence by the
    caller. The whole book: more of the small holdings share the one "smaller holdings" line (under 2%,
    3%, 5%, 10% each); if that is not enough, and always for one stock, the smallest sectors are left
    out, whole lines, and the text says so. The rules are never shortened. A text that fits is returned
    exactly as it always was."""
    out = {"title": "WHAT THE ACCOUNT HOLDS NOW, AND THE LIMITS ON NEW TRADES", "body": "", "symbols": []}
    try:
        if cfg is None:
            import mason_autotrade as AT
            cfg = AT._at_cfg()
        sc = scfg(cfg)
        if sc["sizing"].get("show_book") is not True:
            return out
        if pos is None or acct is None:
            import mason_autotrade as AT
            acct = acct if acct is not None else AT._account()
            pos = pos if pos is not None else AT._positions()
        if not ((acct or {}).get("ok") and (pos or {}).get("ok")):
            return out
        eq = _f(acct.get("equity")) or 0.0
        if eq <= 0:
            return out
        bk = book_snapshot(pos, eq, sc=sc)
        if not bk.get("ok"):
            return out
        sw = switches(sc)
        jstops, mrows = _journal_stops(), _mason_rows()
        want = {str(t).upper() for t in tickers} if tickers else None
        L = [f"The account is worth {_money(eq)}. Percentages below are of that amount."]
        rules = limits_lines(sc, cfg)
        if rules:
            L += ["", "Rules Mason applies to every new long or short call. A call that breaks one is not placed "
                      "(or is made smaller), so check a new idea against them and against the holdings below:"] + rules

        def stop_words(ln, und):
            bits = []
            mr = mrows.get(ln["symbol"])
            ms = _f((mr or {}).get("stop")) if mr else None
            if ms is not None and ln["px"]:
                d = (ln["px"] - ms) / ln["px"] * 100.0
                bits.append(f"Mason's stop {_g(ms)}, " + (f"{d:.1f}% below the price" if d >= 0
                                                         else f"the price is already {abs(d):.1f}% below it"))
            elif ln["mason_qty"] > 1e-9:
                bits.append("no stop on Mason's part")
            if ln["nico_qty"] > 1e-9:
                js = jstops.get(und) if not ln["is_option"] else None
                if js and ln["px"]:
                    d = (ln["px"] - js[0]) / ln["px"] * 100.0
                    bits.append(f"Nico's recorded stop {_g(js[0])}, " + (f"{d:.1f}% below the price" if d >= 0
                                                                        else f"the price is already {abs(d):.1f}% below it"))
                else:
                    bits.append("no stop recorded for Nico's" if ln["mason_qty"] > 1e-9 else "no stop recorded")
            return "; ".join(bits)

        def line_words(ln, und):
            what = (f"{_qty(ln['qty'])} x {_opt_words(ln['symbol'])}" if ln["is_option"] else f"{_qty(ln['qty'])} shares")
            if ln["mason_qty"] > 1e-9 and ln["nico_qty"] > 1e-9:
                who = f"{_qty(ln['nico_qty'])} are Nico's, {_qty(ln['mason_qty'])} are Mason's"
            else:
                who = "Mason's" if ln["mason_qty"] > 1e-9 else "Nico's"
            return f"{what} at {_g(ln['px'])} ({who}); {stop_words(ln, und)}"

        syms = sorted(bk["symbols"].items(), key=lambda kv: -(kv[1]["long_usd"] + kv[1]["short_usd"]))
        shown = [(u, s) for u, s in syms if want is None or u in want][:max_symbols]
        L.append("")
        L.append("Held now, largest first:" if want is None else "Held now in the stock(s) this check is about:")
        small = []
        cap_c = sc["limits"]["symbol_cap"]
        for und, s in shown:
            tot = s["long_usd"] + s["short_usd"]
            pct = tot / eq * 100.0
            out["symbols"].append(und)
            if want is None and pct < float(small_pct):
                who = "Mason's" if (s["mason_long_usd"] + s["mason_short_usd"]) >= tot - 0.005 else \
                    ("Nico's" if (s["mason_long_usd"] + s["mason_short_usd"]) <= 0.005 else "Nico's and Mason's")
                opt = any(ln["is_option"] for ln in s["lines"])
                small.append(f"{und} {_money(tot)} ({who}{', options' if opt else ''})")
                continue
            flag = ""
            if sw["symbol_cap"]:
                lim = float(cap_c["index_max_pct"] if s.get("sector") == BROAD else cap_c["max_pct"])
                room = lim / 100.0 * eq - s["long_usd"]
                flag = (f" At or over the {lim:g}% limit for one stock: a new buy will not be placed." if room <= 0
                        else f" Room under the {lim:g}% limit: {_money(room)}.")
            L.append(f"- {und}: {_money(tot)}, {pct:.1f}% of the account. "
                     + " | ".join(line_words(ln, und) for ln in s["lines"]) + "." + flag)
        if small:
            L.append(f"- Smaller holdings, under {float(small_pct):g}% each: " + "; ".join(small) + ".")
        if not shown:
            L.append("- nothing" if want is None else "- nothing in this stock")
        if bk.get("parked_usd"):                           # idle-cash rule; a sentence only with its wording switch on
            try:
                import mason_idle_cash as IC
                _ic = IC.cfg()
                if _ic.get("show_in_book") is True and IC.live(_ic):     # (review fix) "Mason sells it by itself"
                    L.append("- " + IC.book_words(bk["parked_usd"], list(bk.get("parked") or {})))   # is true only then
            except Exception:
                pass
        if want is not None and len(syms) > len(shown):
            L.append(f"({len(syms) - len(shown)} other holding(s) are not listed here; they are counted in the sector totals.)")
        L.append("")
        L.append("By sector (shares and call options; put options are named separately):")
        sec_start, sec_keep = len(L), set()
        sec_c = sc["limits"]["sector_cap"]
        secs = sorted(bk["sectors"].items(), key=lambda kv: -(kv[1]["long_usd"] + kv[1]["short_usd"]))
        for sec, g in secs:
            longs = sorted(u for u in g["symbols"] if bk["symbols"][u]["long_usd"] > 0)
            puts = sorted(u for u in g["symbols"] if bk["symbols"][u]["short_usd"] > 0)
            ln = f"- {SECTOR_WORDS.get(sec, sec).capitalize()}: "
            if longs:
                ln += f"{_money(g['long_usd'])}, {g['long_usd'] / eq * 100:.1f}% of the account ({', '.join(longs)})"
                if g["mason_long_usd"] > 0:
                    ln += f"; {_money(g['mason_long_usd'])} of it is Mason's"
            if puts:
                ln += ("; " if longs else "") + f"puts worth {_money(g['short_usd'])} ({', '.join(puts)})"
            ln += "."
            if sw["sector_cap"] and longs and sec not in (sec_c.get("exempt") or []):
                rooms = []
                if _f(sec_c.get("max_pct")) is not None:
                    rooms.append(float(sec_c["max_pct"]) / 100.0 * eq - g["long_usd"])
                if _f(sec_c.get("mason_max_pct")) is not None:
                    rooms.append(float(sec_c["mason_max_pct"]) / 100.0 * eq - g["mason_long_usd"])
                if rooms:
                    room = min(rooms)
                    ln += (" No room left for a new buy in this sector." if room <= 0
                           else f" Room left for new buys by Mason: {_money(room)}.")
            if want is not None and (set(g["symbols"]) & want):
                sec_keep.add(len(L))                       # the sector of the stock a quick check is about always stays
            L.append(ln)
        if bk.get("unknown_sector"):
            L.append(f"- Sector not known: {', '.join(sorted(bk['unknown_sector']))}.")
        try:                                               # (review) never hand the caller a text it has to cut mid-sentence
            room = (_ONE_STOCK_TOKENS if want is not None else _BOOK_TOKENS) if max_tokens is None else int(max_tokens)
            over = room > 0 and len("\n".join(L)) // 4 > room            # only a text that really does not fit is touched
            if over and _fold and want is None:
                # 1. the whole book: more of the small holdings share the one "smaller holdings" line
                kw = dict(cfg=cfg, pos=pos, acct=acct, tickers=tickers, max_symbols=max_symbols, _fold=False)
                for sp in (2.0, 3.0, 5.0, 10.0):
                    if sp > float(small_pct):
                        alt = book_block(small_pct=sp, max_tokens=0, **kw)
                        if alt.get("body") and len(alt["body"]) // 4 <= room:
                            return alt
                alt = book_block(small_pct=max(10.0, float(small_pct)), max_tokens=room, **kw)   # 2. and then the sectors
                if alt.get("body"):
                    return alt
            left, i = 0, len(L) - 1
            while over and (len("\n".join(L)) + 72) // 4 > room and i >= sec_start:   # 72: room for the note below
                if i not in sec_keep:                      # the smallest sectors are left out, whole lines
                    del L[i]
                    left += 1
                i -= 1
            if left:
                L.append(f"({left} smaller sector line{'s are' if left > 1 else ' is'} left out to keep this short.)")
        except Exception:
            pass
        out["body"] = "\n".join(L)
    except Exception:
        out["body"] = ""
    return out
