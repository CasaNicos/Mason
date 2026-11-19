"""
mason_brief_triage.py — Phase 1 of the brief redesign: headline-first triage.

Instead of dumping every section every time, this scores everything that COULD
be said, ranks it, and emits only what clears the bar — leading with what needs
a decision. Quiet domains collapse to a one-line "receipt" so Nico knows they
were checked, not skipped.

Phase 1 scope: options needing action, stock trades near stop/target,
posture, calendar, the agenda/reminders feed, a ranked "what to do", and
flag-avoidance (decisions pending across multiple briefs). News Flow (Phase 2),
skill-powered new plays (Phase 3), and charts (Phase 4) bolt on after.
(The "book movers" domain read the retired positions.csv snapshot; removed 2026-11-19, Nico's 11-11 ruling.)

Degrades gracefully: anything needing live market data is wrapped; offline it
falls back to the journal values and the relevant line goes quiet.

Public API:
    build()          -> dict   (structured brief + rendered screen/spoken text)
    render_screen()  -> str
    render_spoken()  -> str
"""

import json
import random
from datetime import datetime, date
from pathlib import Path

BASE          = Path(__file__).parent
HIST_FILE     = BASE / "brief_history.json"
POSTURE_CACHE = BASE / ".posture_cache.json"

# Reuse the existing data layer where possible (guarded — yfinance/FMP may be absent).
try:
    import mason_brief as MB
except Exception:
    MB = None
import mason_options
import mason_trades
import mason_agenda
try:
    import mason_news
except Exception:
    mason_news = None
try:
    import mason_ideas
except Exception:
    mason_ideas = None

# ── Tunable thresholds (see docs/prompts/BRIEF_DESIGN.md §3) ───────────────────
NEAR_STOP_PCT         = 3.0    # within this % of a stop = red
NEAR_TARGET_PCT       = 3.0    # within this % of a target = green
FLAG_AVOID_AFTER      = 2      # an actionable item pending this many briefs gets flagged

DOT = {"red": "🔴", "yellow": "🟡", "green": "🟢"}


class Item:
    """One thing Mason could say. Lower `pri` = more urgent (sorts first)."""
    __slots__ = ("pri", "status", "domain", "headline", "detail", "action", "key")

    def __init__(self, pri, status, domain, headline, detail="", action=None, key=None):
        self.pri = pri
        self.status = status
        self.domain = domain
        self.headline = headline
        self.detail = detail
        self.action = action     # short imperative for the "what to do" list
        self.key = key           # stable id for flag-avoidance tracking

    def line(self):
        return f"{DOT.get(self.status,'')} {self.headline}".strip()


# ── history (deltas + flag-avoidance) ──────────────────────────────────────────

def _load_hist() -> dict:
    try:
        if HIST_FILE.exists():
            return json.loads(HIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"last_brief": None, "posture": None, "pending": {}}


def _save_hist(d: dict):
    try:
        HIST_FILE.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


# ── data helpers ───────────────────────────────────────────────────────────────

def _quote(ticker):
    if MB is None:
        return {}
    try:
        return MB.get_quote_yf(ticker) or {}
    except Exception:
        return {}


def _posture():
    try:
        if POSTURE_CACHE.exists():
            txt = POSTURE_CACHE.read_text(encoding="utf-8").lstrip()
            # Cache may hold trailing data / multiple objects — read just the first.
            d, _ = json.JSONDecoder().raw_decode(txt)
            tr  = d.get("top_risk", {}) or {}
            sec = d.get("sector", {}) or {}
            return {
                "zone":   tr.get("zone")  or sec.get("regime", "unknown"),
                "score":  tr.get("score") if tr.get("score") is not None else sec.get("regime_score", "?"),
                "phase":  sec.get("cycle_phase", ""),
                "regime": sec.get("regime", ""),
            }
    except Exception:
        pass
    return None


# ── domain scorers ─────────────────────────────────────────────────────────────

def _option_items():
    """Expiry timing comes from the agenda; breach checks use a live underlying quote."""
    items = []
    try:
        plays = mason_options.get_open_plays()
    except Exception:
        plays = []

    # Expiry proximity, via the agenda engine (already T-1/day-of aware).
    # Only the URGENT ones (day-of / tomorrow) become "what matters" items here;
    # the multi-day heads-ups flow through THINGS TO DO instead, so nothing triples.
    try:
        for a in mason_agenda.build_agenda():
            if a.get("kind") != "expiry" or a.get("urgency", 3) > 1:
                continue
            urg = a.get("urgency", 1)
            st  = "red" if urg == 0 else "yellow"
            tk  = a.get("ticker", "?")
            items.append(Item(
                pri=urg, status=st, domain="options",
                headline=a["line"],
                action=(f"Close/settle {tk}" if urg == 0 else f"Plan {tk} exit"),
                key=f"opt-expiry:{tk}:{a.get('when')}",
            ))
    except Exception:
        pass

    # Short-strike / breakeven breach (needs a live underlying quote).
    for p in plays:
        q = _quote(p.get("ticker", ""))
        px = q.get("price")
        if not px:
            continue
        be = p.get("breakeven")
        short = (p.get("strikes") or {}).get("short_call") or (p.get("strikes") or {}).get("short_put")
        tk = p.get("ticker", "?")
        if short and px >= short and "call" in str(p.get("strategy", "")):
            items.append(Item(
                pri=0, status="red", domain="options",
                headline=f"{tk} ${px:.2f} breached short call ${short}",
                action=f"Manage {tk} — short strike tested",
                key=f"opt-breach:{tk}",
            ))
        elif be and abs(px - be) / be <= 0.01:
            items.append(Item(
                pri=1, status="yellow", domain="options",
                headline=f"{tk} ${px:.2f} sitting on breakeven ${be}",
                key=f"opt-be:{tk}",
            ))
    return items


def _other_account(t) -> bool:
    """True for a journal row held in ANOTHER account (mason_execution.non_alpaca_marker, the one shared rule:
    today only the 1-share NVDA row of the old brokerage account). Its stop is not Mason's to watch, so the brief
    never puts a 'near stop / near target' item up for it (review fix 2026-11-19). False when the rule can't load."""
    try:
        import mason_execution
        return mason_execution.non_alpaca_marker(t) is not None
    except Exception:
        return False


def _stock_trade_items():
    items = []
    try:
        trades = mason_trades.get_open_trades()
    except Exception:
        trades = []
    for t in trades:
        if _other_account(t):
            continue
        q = _quote(t.get("ticker", ""))
        px = q.get("price")
        if not px:
            continue
        tk, stop, tgt = t.get("ticker", "?"), t.get("stop_loss"), t.get("target_price")
        if stop and px > stop and (px - stop) / px * 100 <= NEAR_STOP_PCT:
            items.append(Item(
                pri=0, status="red", domain="trades",
                headline=f"{tk} ${px:.2f} — {((px-stop)/px*100):.1f}% from stop ${stop}",
                action=f"Decide on {tk} — near stop",
                key=f"trade-stop:{tk}",
            ))
        elif tgt and px < tgt and (tgt - px) / px * 100 <= NEAR_TARGET_PCT:
            items.append(Item(
                pri=1, status="green", domain="trades",
                headline=f"{tk} ${px:.2f} — {((tgt-px)/px*100):.1f}% from target ${tgt}",
                action=f"Plan {tk} exit — near target",
                key=f"trade-tgt:{tk}",
            ))
    return items


def _posture_item(hist):
    p = _posture()
    if not p:
        return None, "posture unavailable"
    zone = str(p["zone"]).lower()
    prev = (hist.get("posture") or {})
    changed = prev and prev.get("zone") and str(prev["zone"]).lower() != zone
    elevated = zone in ("high", "severe", "red", "caution", "defensive")
    label = f"posture {p['zone']} {p['score']}/100" + (f" · {p['phase']}" if p["phase"] else "")
    if changed:
        return Item(pri=1, status="yellow", domain="posture",
                    headline=f"Posture flipped to {p['zone']} ({p['score']}/100) since last brief",
                    detail=p["phase"], key="posture-change"), None
    if elevated:
        return Item(pri=2, status="yellow", domain="posture",
                    headline=label.capitalize(), key="posture"), None
    return None, label  # quiet receipt


def _calendar_items(watch):
    items = []
    if MB is None:
        return items
    # Earnings within the week for names we care about.
    try:
        for e in (MB.get_earnings_this_week() or []):
            sym = e.get("symbol", "")
            if sym and sym in watch:
                items.append(Item(pri=2, status="yellow", domain="calendar",
                                  headline=f"{sym} earnings {e.get('date','')}",
                                  key=f"earn:{sym}"))
    except Exception:
        pass
    return items


def _watch_universe():
    seed = {"SPY", "QQQ", "VIX", "TSM", "NVDA", "IONQ", "RKLB",
            "XLE", "XLV", "OXY", "USO", "LMT", "MU"}
    try:
        seed |= {p["ticker"] for p in mason_options.get_open_plays()}
    except Exception:
        pass
    try:
        seed |= {t["ticker"] for t in mason_trades.get_open_trades()}
    except Exception:
        pass
    return seed


# ── flag-avoidance ─────────────────────────────────────────────────────────────

def _flag_avoidance(actionable_keys, hist):
    """Increment a pending-count for each actionable item; flag the stale ones."""
    pending = dict(hist.get("pending", {}))
    flags = []
    now_keys = set(actionable_keys)
    for k in list(pending.keys()):
        if k not in now_keys:
            del pending[k]                       # resolved — stop tracking
    for k in now_keys:
        pending[k] = pending.get(k, 0) + 1
        if pending[k] >= FLAG_AVOID_AFTER:
            flags.append((k, pending[k]))
    hist["pending"] = pending
    return flags


# ── assembly ───────────────────────────────────────────────────────────────────

def _news_items():
    """Returns (matter_items, pulse): material news as ranked Items + a top-of-tape pulse."""
    if mason_news is None:
        return [], []
    try:
        flow = mason_news.fetch_flow(limit=15)
    except Exception:
        return [], []
    prio = [it for it in flow if it.get("category") in ("holding", "watchlist")]
    pulse = (prio or flow)[:3]
    rankmap = {"red": 1, "yellow": 2}
    matter = []
    for it in flow:
        if it.get("category") == "holding" and it.get("impact") in ("red", "yellow"):
            head = it.get("implication") or it.get("headline", "")
            tk = ",".join(it.get("tickers", []))
            matter.append(Item(
                pri=rankmap.get(it["impact"], 2), status=it["impact"], domain="news",
                headline=(f"{tk}: " if tk else "") + head,
                detail=it.get("source", ""), key=f"news:{it.get('id')}"))
    return matter[:3], pulse


def build() -> dict:
    hist = _load_hist()
    watch = _watch_universe()

    opt_items   = _option_items()
    trade_items = _stock_trade_items()
    post_item, post_quiet = _posture_item(hist)
    cal_items   = _calendar_items(watch)

    news_matter, news_pulse = _news_items()
    idea_plays = []
    if mason_ideas is not None:
        try:
            idea_plays = mason_ideas.last_ideas()
        except Exception:
            idea_plays = []
    all_items = list(opt_items) + list(trade_items) + list(cal_items) + list(news_matter)
    if post_item:
        all_items.append(post_item)
    all_items.sort(key=lambda i: (i.pri, i.domain))

    # "What matters" is reserved for genuinely notable items (red/yellow, pri<=2).
    # Multi-day heads-ups (green, pri 3) live in THINGS TO DO, not the headline.
    what_matters = [i for i in all_items if i.pri <= 2][:3]

    # Things to do — straight from the agenda engine (expiries, reviews, reminders, FOMC).
    todo = []
    try:
        for a in mason_agenda.build_agenda():
            todo.append({"line": a["line"], "kind": a.get("kind"), "urgency": a.get("urgency", 3)})
    except Exception:
        pass

    # Actionable keys (for flag-avoidance): anything carrying an action.
    actionable = [i.key for i in all_items if i.action and i.key]
    flags = _flag_avoidance(actionable, hist)
    flag_lines = []
    by_key = {i.key: i for i in all_items}
    for k, n in flags:
        it = by_key.get(k)
        what = it.action if it else k
        flag_lines.append(f"You've had \"{what}\" open across {n} briefs — time to decide.")

    # What to do now — ranked actions from the matter-items + agenda urgents.
    what_to_do = []
    for i in sorted(all_items, key=lambda x: x.pri):
        if i.action:
            what_to_do.append(i.action)
    for a in todo:
        # expiry urgents are already represented by item.action above — skip the verbose twin
        if a["urgency"] == 0 and a.get("kind") not in ("expiry",):
            what_to_do.append(a["line"])
    # de-dup, keep order
    seen, wtd = set(), []
    for a in what_to_do:
        if a not in seen:
            seen.add(a); wtd.append(a)
    wtd += flag_lines

    # The rest — quiet receipts for domains that produced nothing notable.
    rest = []
    if not opt_items:   rest.append("options all behaving")
    if not trade_items: rest.append("no stops in range")
    if not cal_items:   rest.append("nothing on the calendar for your names")
    if not post_item and post_quiet: rest.append(post_quiet)
    if not news_matter: rest.append("quiet tape on your names")

    # State line.
    n = len(what_matters)
    if n == 0 and not todo:
        mood = random.choice([
            "Quiet tape.", "Slow one.", "Not much doing.", "Calm out there.",
        ])
        state = f"{mood} Nothing needs you."
    elif n == 0:
        state = f"Nothing urgent — but {len(todo)} thing{'s' if len(todo)!=1 else ''} on the list."
    else:
        state = f"{n} thing{'s' if n!=1 else ''} matter{'s' if n==1 else ''} today."

    # Persist history for next time's deltas + flag tracking.
    hist["last_brief"] = datetime.now().isoformat()
    hist["posture"]    = _posture()
    hist.pop("book_value", None)          # the retired positions.csv total (book domain removed 2026-11-19)
    _save_hist(hist)

    return {
        "state_line":   state,
        "what_matters": what_matters,
        "plays":        [i for i in (opt_items + trade_items)],
        "things_to_do": todo,
        "what_to_do":   wtd,
        "the_rest":     rest,
        "news_pulse":   news_pulse,
        "idea_plays":   idea_plays,
        "ts":           datetime.now().strftime("%a %b %-d, %-I:%M%p").lower(),
        "posture":      _posture(),
    }


# ── rendering ──────────────────────────────────────────────────────────────────

_OPENERS = [
    "Mason has been watching. [MASON IS ALWAYS WATCHING]",
    "Tape read complete. [MASON READS FAST]",
    "Mason ran the scan.",
    "Here is where things stand. [MASON KEEPS STANDING WATCH]",
]


def render_screen(b=None) -> str:
    b = b or build()
    L = []
    p = b.get("posture") or {}
    post = f"  [posture: {p.get('zone','?')} {p.get('score','?')}]" if p else ""
    L.append(f"{random.choice(_OPENERS)}")
    L.append(f"{b['state_line']}  ·  {b['ts']}{post}")
    L.append("")

    # Boot-refresh receipt — what Mason refreshed when it loaded (screen only).
    try:
        import mason_startup as _MS
        _boot = _MS.last_boot_status()
        if _boot:
            L.append("  ⟳ " + _MS.summary_line(_boot))
            L.append("")
    except Exception:
        pass

    if b["what_matters"]:
        L.append("WHAT MATTERS")
        for n, i in enumerate(b["what_matters"], 1):
            extra = f"  — {i.detail}" if i.detail else ""
            L.append(f"  {n}. {i.line()}{extra}")
        L.append("")

    if b.get("news_pulse"):
        shown = {i.key for i in b["what_matters"]}
        pulse = [it for it in b["news_pulse"] if f"news:{it.get('id')}" not in shown]
        if pulse:
            L.append("NEWS PULSE")
            for it in pulse:
                dot = DOT.get(it.get("impact"), "")
                tk = ("[" + ",".join(it.get("tickers", [])) + "] ") if it.get("tickers") else ""
                line = it.get("implication") or it.get("headline", "")
                L.append(f"  {dot} {tk}{line}")
            L.append("")

    if b.get("idea_plays"):
        L.append("NEW PLAYS  [MASON IS HUNTING]")
        for p in b["idea_plays"][:3]:
            tgt = f" → ${p.get('target')}" if p.get("target") else ""
            L.append(f"  ▸ {p.get('ticker')} {p.get('setup','')} ({p.get('score','?')}/100) "
                     f"entry ${p.get('entry')} stop ${p.get('stop')}{tgt}")
        L.append("  (say \"hunt setups\" for the full pitch + sizing)")
        L.append("")

    if b["things_to_do"]:
        L.append("THINGS TO DO")
        for a in b["things_to_do"]:
            L.append(f"  · {a['line']}")
        L.append("")

    if b["what_to_do"]:
        L.append("WHAT TO DO NOW")
        for n, a in enumerate(b["what_to_do"], 1):
            L.append(f"  {n}. {a}")
        L.append("")

    if b["the_rest"]:
        L.append("THE REST (checked, quiet)")
        L.append("  " + " · ".join(b["the_rest"]))
        L.append("")

    L.append("  [MASON IS DONE HERE] — that's the read.")
    return "\n".join(L)


def render_spoken(b=None) -> str:
    b = b or build()
    parts = [b["state_line"]]
    for i in b["what_matters"]:
        parts.append(i.headline + (f" — {i.action}" if i.action else ""))
    if b["what_matters"]:
        parts.append("Want the detail on any of these?")
    elif b["what_to_do"]:
        parts.append("A couple of housekeeping items, but nothing urgent.")
    return " ".join(parts)


if __name__ == "__main__":
    data = build()
    print(render_screen(data))
    print("\n--- SPOKEN ---")
    print(render_spoken(data))
