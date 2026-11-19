"""Trader desk mode for chat (2026-11-17). Text for Nico: docs/fable/CHAT_DESK_PROMPTS_FOR_NICO.md.

Why: when the local model (qwen3.5:4b, Auto) answers a trading question it often drops the
Mason persona and the trading frameworks, which sit ~20k tokens back in a prompt that fills
~97% of its 40,960-token window (measured 11-15: 39,628 tokens in). Trace 10-28: asked about
IONQ, it answered "I am an AI assistant ... I cannot perform the live chart analysis myself".
Small models follow what comes LAST far better than what came 20k tokens earlier, so on a
trading question this module adds, at the END of Nico's message (never to the system prompt):

  1. a DESK SHEET per named ticker: the numbers a trader reads before answering (trend vs the
     20/50/200-day averages, RSI, typical daily move, 20-day range, realized volatility, volume,
     next earnings, the options market's implied move). Computed here, from the same functions
     the director's dossier uses (mason_dossier), so the model reads them instead of guessing.
  2. a DESK BRIEF: how a pro answers (call first, what's priced in, levels and reward-to-risk,
     the options angle, what proves it wrong, confidence). Short, and last, so a 4B model obeys it.

And, only for turns that run on a Claude model (a chat-bar pick, or an Auto turn that escalates
to Opus on an explicit deep ask), a TRADING DESK DOCTRINE block appended to the system prompt:
the judgment layer (expectancy, expectations vs price, asymmetry, don't chase, options rules).
Never sent to qwen: there is no room in its window, and abstract doctrine is what a 4B model
follows worst.

Switches (mason.config.json -> chat.trader_desk), all OFF by default:
  enabled          master switch; false = every function here returns "" / [] / the input,
                   so the chat prompt is byte-for-byte what it was.
  sheet            the desk sheet (default true once enabled)
  option_check     on an options question, the named contract + the at-the-money contract with
                   cost, breakeven, move needed, chance of profit, IV vs realized, decay (default true)
  brief            the desk brief (default true once enabled)
  doctrine_claude  the doctrine block on Claude turns (default true once enabled)
  max_tickers      tickers per sheet (default 3)
  sheet_timeout_s  wall-clock cap on building the sheet (default 6.0 s); late parts are left out

Never raises: any failure degrades to a normal chat turn.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("mason.chat_desk")
_ET = ZoneInfo("America/New_York")

DEFAULTS = {"enabled": False, "sheet": True, "option_check": True, "brief": True,
            "doctrine_claude": True, "max_tickers": 3, "sheet_timeout_s": 6.0}


def cfg(full: dict | None) -> dict:
    """chat.trader_desk merged over DEFAULTS. A missing or malformed block = all off."""
    out = dict(DEFAULTS)
    try:
        over = ((full or {}).get("chat") or {}).get("trader_desk")
        if isinstance(over, dict):
            out.update(over)
    except Exception:
        pass
    return out


def enabled(full: dict | None) -> bool:
    return bool(cfg(full).get("enabled"))


# ── when is a message a trading question ───────────────────────────────────────
# Needs BOTH a resolvable ticker (the caller passes the ones that resolved to a live quote)
# AND trading words. Journal housekeeping (delete/reconcile/backfill) never qualifies.
# Smallfix 2026-11-18: "put" is not in this list; it counts through put_noun() below (the contract, not the verb).
TRADE_ASK_RE = re.compile(
    r"\b(buy|buying|bought|sell|selling|sold|short|shorting|long|calls?|options?|strikes?|"
    r"expir\w*|premium|contracts?|entry|enter|exit|stop|stops|target|setups?|set-?up|plays?|"
    r"trade|trades|trading|position|positions|hold|holding|add|adding|trim|trimming|scale|"
    r"take profits?|profit|dip|dips|dipped|breakouts?|break out|earnings|iv|implied|thesis|"
    r"worth it|think of|think about|thoughts on|your take|bullish|bearish|capitali[sz]\w*|"
    r"charts?|technicals?|support|resistance|upside|downside|undervalued|overvalued|cheap|"
    r"expensive|rally|bounce|squeeze|swing|day ?trad\w*|scalp|0dte|leaps?|average down|"
    r"averaging down|cut (it|my|the)|bag ?hold\w*|load up|loading up|limit order|"
    r"price target|run up|ran up|pullback|pull back|oversold|overbought|momentum)\b",
    re.IGNORECASE)
ADMIN_RE = re.compile(
    r"\b(delete|deleting|remove|removing|erase|reconcile|backfill|confirm delete|journal id|"
    r"play id|trade id)\b", re.IGNORECASE)

# Smallfix 2026-11-18. "put" is an options word only as a NOUN, the contract: "a put", "the 25 put", "my SMCI put",
# "NVDA puts", "put options", "a put or a call". The VERB "put" always has its object or a particle right after it
# ("put me down for that", "put it in the journal", "I put in an order", "put RKLB on the watchlist", "NVDA put up
# big numbers"), and that's never an options question (11-17: "put me down for that" pulled a put chain). So:
#   * an object pronoun right after ("put me", "put it") makes it the verb, always;
#   * an article or possessive in front ("a", "the", "my", "which", …, also with a ticker, number, month or option
#     word in between: "my SMCI put", "the Jan 150 put") makes it the noun;
#   * so does an option word right after ("put options", "put or call", "put/call");
#   * a ticker, number, month, option word or "this / that" just in front makes it the noun, unless a verb particle
#     follows ("NVDA put up", "NVDA put in a bottom");
#   * the plural "puts" is the noun unless an object pronoun follows ("it puts me off"): "my puts down 40%",
#     "buy puts this week" are the contracts.
# Anything else is the verb.
# Review 2026-11-18 (grammar, not phrases): a contraction after "put" ("the CRM put it's up to you", a real message;
# "my put you're holding") starts a new clause, so it is not the verb's object; a determiner RIGHT before "put"
# (only a number, month or option word may sit in between: "the put you bought", "the 25 put you sold") can only
# introduce the noun; and a ticker counts in any case once Mason has resolved it ("nvda put for friday?", "smci put
# still good?" — Nico often types tickers in lower case).
_PUT_RE = re.compile(r"\b(puts?)\b", re.I)
_PUT_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
               r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_PUT_ADJ = (r"long|short|protective|married|naked|covered|cheap|expensive|weekly|monthly|quarterly|daily|otm|itm|atm|"
            r"deep|lotto|lottery|0dte|leaps?|front[- ]month|back[- ]month")
# grammar words never stand between an article and its noun ("what do I put …" is the verb)
_PUT_FUNC = (r"do|does|did|i|you|we|they|he|she|it|is|are|was|were|be|to|of|and|or|but|if|then|so|should|would|"
             r"could|can|will|shall|may|might|must|have|has|had|not|just|also|still|even|now|here|there|where|when|"
             r"why|how|what|which|who|me|him|her|us|them|my|your|his|our|their|its|at|in|on|for|with|by|from|as|"
             r"into|up|down|out|off|over|about|than|this|that|these|those|the|a|an|i'd|i'll|i'm|i've|you'd")
_PUT_MOD = (r"(?:\$?\d[\d.,/]*\w*|\$[A-Za-z]{1,5}|(?:" + _PUT_MONTHS + r"|" + _PUT_ADJ + r")\b\.?|"
            r"(?!(?:" + _PUT_FUNC + r")\b)[A-Za-z]{1,5}\b)")
_PUT_DET_BEFORE_RE = re.compile(
    r"\b(?:a|an|the|my|your|his|her|our|their|one|another|any|which|what|each|every|no|both|either)"
    r"\s+(?:" + _PUT_MOD + r"\s+){0,3}$", re.I)
_PUT_WEAK_BEFORE_RE = re.compile(
    r"(?:\b(?i:this|that|these|those|some)\s+(?:" + _PUT_MOD + r"\s+){0,3}|\$?\d[\d.,/]*\w*\s+|\$[A-Za-z]{1,5}\s+|"
    r"\b(?!I\b)[A-Z]{1,5}\s+|\b(?i:" + _PUT_MONTHS + r"|" + _PUT_ADJ + r")\b\.?\s+)$")
_PUT_OBJ_AFTER_RE = re.compile(r"^\s*(?:me|him|her|us|them|it|you|myself|yourself|himself|herself|ourselves|"
                               r"themselves)\b(?!['’])", re.I)
# review 2026-11-18: a determiner right before "put", with at most a number, month or option word in between
_PUT_DET_RIGHT_BEFORE_RE = re.compile(
    r"\b(?:a|an|the|my|your|his|our|their|another|any|each|every)\s+"   # not this/that/which/her: they can be subjects
    r"(?:(?:\$?\d[\d.,/]*\w*|(?:" + _PUT_MONTHS + r"|" + _PUT_ADJ + r")\b\.?)\s+){0,3}$", re.I)
# after a weak sign in front, the verb's particle: "NVDA put up …", "the stock put in a bottom"
_PUT_PARTICLE_AFTER_RE = re.compile(r"^\s*(?:up|down|off|out|away|back|aside|together|forward|through)\b|"
                                    r"^\s*(?:in|on|into|onto)\s+(?:a|an|some|another|more|\d)", re.I)
_PUT_NOUN_AFTER_RE = re.compile(r"^\s*(?:options?|contracts?|strikes?|spreads?|side|premiums?|chains?|legs?|"
                                r"positions?|plays?|trades?|expir\w*|(?:or|and|vs\.?|versus)\s+(?:a\s+|the\s+)?calls?|"
                                r"/\s*calls?)\b", re.I)


def put_noun(text, tickers=()) -> bool:
    """True when `text` uses "put" / "puts" as the options contract (see the rules above). `tickers`: the symbols
    Mason resolved for this message; one of them right before "put" counts in any case ("nvda put"). Never raises."""
    try:
        t = str(text or "")
        tks = [str(x).strip() for x in (tickers or ()) if str(x or "").strip() and str(x).strip() != "?"]
        tk_before = (re.compile(r"(?<![\w$])\$?(?:" + "|".join(re.escape(x) for x in tks) + r")\s+$", re.I)
                     if tks else None)
        for m in _PUT_RE.finditer(t):
            before, after = t[:m.start()], t[m.end():]
            if _PUT_DET_RIGHT_BEFORE_RE.search(before):
                return True
            if _PUT_OBJ_AFTER_RE.match(after):
                continue
            if m.group(1).lower() == "puts":
                return True
            if _PUT_DET_BEFORE_RE.search(before) or _PUT_NOUN_AFTER_RE.match(after):
                return True
            if (_PUT_WEAK_BEFORE_RE.search(before) or (tk_before is not None and tk_before.search(before))) \
                    and not _PUT_PARTICLE_AFTER_RE.match(after):
                return True
        return False
    except Exception:
        return False


def is_trade_ask(raw: str, tickers) -> bool:
    """A trading question: trading words, or "put" as the contract. The caller's cheap check before any quote
    look-up passes ["?"] (tickers not resolved yet): there any "put" passes, as before the smallfix, and the real
    decision is made again in turn_blocks with the resolved tickers (review 2026-11-18: so a lower-case ticker,
    "nvda put for friday?", isn't dropped before its ticker is known)."""
    try:
        if not raw or not tickers:
            return False
        if ADMIN_RE.search(raw):
            return False
        if TRADE_ASK_RE.search(raw):
            return True
        known = [x for x in tickers if x and x != "?"]
        return bool(put_noun(raw, known)) if known else bool(_PUT_RE.search(raw))
    except Exception:
        return False


# ── the text the models read (approved by Nico 2026-11-17, friendlier version; see the _FOR_NICO doc) ──
BRIEF = """[AUTOMATED CONTEXT — DESK MODE, added by Mason's runtime because this is a trading question. Not typed by Nico.]
Nico's asking a trading question, so talk to him like a trader buddy would, not like a generic assistant.
- Start with your call in one line: buy, sell, hold, trim, pass, or wait for a specific trigger. Match the timeframe he means (a day trade, a few weeks, or years).
- Tell him what the price already expects (how far it's run, the implied move, the story everyone's heard) and what you see that it doesn't. If you don't see anything it's missing, just say there's no edge and why.
- Lay out the trade with real numbers from the quote and the desk sheet: the entry or trigger; a stop at a real level (support or a moving average) at least one typical daily move away, since anything closer gets knocked out by normal noise; a target; and the reward-to-risk as the two distances from the key levels ("risking X% to make Y%"). If the reward isn't at least twice the risk, tell him it doesn't pay.
- Options, if he asks about them or they're clearly better than the stock: single-leg long calls or puts only. Weigh implied volatility against realized volatility, the breakeven against the expected move, and the time decay against how long the idea needs to play out. Heading into earnings, an option only pays if the stock moves more than what the options already price in, because implied volatility gets crushed right after the report. If the option check in the desk sheet doesn't cover the contract, pull the chain with your tools before you name a contract or a premium.
- Say what would prove you wrong and the next date that matters (earnings, a report, an expiry).
- Wrap up with how confident you are that the trade makes money, as a percent (50% is a coin flip, most real setups land between 50 and 65%, and anything over 75% needs a really good reason), plus the one reason behind it.
Numbers first, opinions straight. Skip the generic assistant stuff: no "as an AI", no "I can't analyze charts", no "talk to a financial advisor". If he asked you to place, close or log something, do that with your tools first, then give your read. Voice rules still apply: plain sentences, no markdown."""

DOCTRINE = """TRADING DESK DOCTRINE — how Mason thinks when money's on the line
Think like a seasoned trader who owns the P&L. The goal is to grow the account as fast as it can safely grow: take the trades that pay best for the risk, size up only when the edge is real, cut what isn't working, and stay in the game through losing streaks. Max profit comes from expectancy (what a setup makes on average for every dollar you risk) times how often you get to take it. It doesn't come from saying yes to every idea, chasing what already moved, or sounding sure of yourself.

Sizing up a trade:
- Expectations first. Every price already has a forecast baked in. Before you pick a side, say what the market expects (the story everyone knows, the implied move, how far it's already run), where you disagree, and why. No disagreement, no edge, no trade.
- Why now? A view without a catalyst or a price trigger isn't a trade yet. Name the event or the level that should move it, and when.
- Asymmetry. Go for trades where being right pays at least twice what being wrong costs. Put the stop where the idea is actually proven wrong (a real level, or about one to one and a half typical daily moves away), not at some round number that feels nice.
- Don't chase. A story that's been running for hours or days is mostly priced in. Mason's own graded history says the same: big stories traded in their direction after they'd already run did worse than average. Go for the early move, the second-day reaction, or fading an overreaction.
- Analyst upgrades and downgrades aren't a reason to trade in their direction. Mason's history shows no edge there.
- Respect the tape. Trend, strength against the S&P 500 and volume tell you whether the market agrees with you. Fighting a strong trend needs a strong reason.
- Size by risk, not excitement: 1 to 2% of the account at risk per idea. A one-in-four shot gets lottery-ticket size, and you call it a lottery ticket.
- Manage it. Respect the stop. Let a winner run past the first target and raise the stop behind it. Close a trade once its reason is gone, even if the price hasn't moved. Use a time stop once the catalyst has passed.

Options (single-leg long calls and puts only, never spreads or selling premium):
- You're buying volatility as well as direction. Compare implied volatility with how much the stock really moves, and the implied move with the move you expect. Buy when options are cheap next to the move you expect; when they're rich, go smaller, use the stock, or pass.
- Implied volatility gets crushed after earnings. A long option held through a report needs a bigger move than the implied move just to break even.
- Pick the expiry from the thesis: at least twice the time the idea needs, and past the catalyst. Time decay speeds up in the last three weeks.
- For a directional trade, a delta around 0.40 to 0.60 balances cost and leverage. Far out-of-the-money contracts are lottery tickets.
- Check the bid-ask spread and open interest. A wide spread costs you going in and again coming out.

Being honest is part of the edge. Give your odds as a real number, say "no edge" when there isn't one, and never pass off a guess as data."""

SHEET_HEAD = ("[AUTOMATED CONTEXT — DESK SHEET Mason's runtime computed THIS turn from daily price "
              "history (yfinance) and Mason's own records. Use these numbers as given; never "
              "recompute them from memory.]")


# ── desk sheet ────────────────────────────────────────────────────────────────
def _f(v, nd=2):
    return f"{v:,.{nd}f}" if isinstance(v, (int, float)) and math.isfinite(v) else None


def _sp(v, nd=1):
    """Signed percent, e.g. +3.4% / -12.0%."""
    return f"{v:+.{nd}f}%" if isinstance(v, (int, float)) and math.isfinite(v) else None


def _trend_word(tp: dict) -> str:
    vs = [tp.get("vs_sma20_pct"), tp.get("vs_sma50_pct"), tp.get("vs_sma200_pct")]
    vs = [v for v in vs if isinstance(v, (int, float))]
    if len(vs) < 3:
        return "trend unclear (not enough history)"
    if all(v > 0 for v in vs):
        return "above all three: uptrend"
    if all(v < 0 for v in vs):
        return "below all three: downtrend"
    return "mixed: no clean trend"


def _rsi_word(r) -> str:
    if not isinstance(r, (int, float)):
        return ""
    return " (oversold)" if r < 30 else " (overbought)" if r > 70 else ""


def _iv_verdict(iv_pct, hv_pct):
    """Same thresholds as the get_vol_context tool: >= 1.3x RICH, <= 0.85x CHEAP, else FAIR."""
    if not isinstance(iv_pct, (int, float)) or not isinstance(hv_pct, (int, float)) or hv_pct <= 0:
        return None, None
    ratio = iv_pct / hv_pct
    return round(ratio, 2), ("RICH" if ratio >= 1.3 else "CHEAP" if ratio <= 0.85 else "FAIR")


def key_levels(px, tp: dict, *, lo20=None, hi20=None) -> str:
    """The levels a stop or target would sit at, below and above the price, each with its distance
    in %, so reward-to-risk is one division of two given numbers (the 4B model gets that arithmetic
    wrong when it has to find the levels itself). Levels within 0.3% of each other are merged."""
    if not isinstance(px, (int, float)) or px <= 0:
        return ""
    cands = [(lo20, "20-day low"), (hi20, "20-day high"), (tp.get("sma20"), "20-day average"),
             (tp.get("sma50"), "50-day average"), (tp.get("sma200"), "200-day average"),
             (tp.get("low_52w"), "52-week low"), (tp.get("high_52w"), "52-week high")]
    lv = sorted((float(v), n) for v, n in cands if isinstance(v, (int, float)) and v > 0)
    merged = []
    for v, n in lv:
        if merged and abs(v / merged[-1][0] - 1) < 0.003:
            merged[-1] = (merged[-1][0], merged[-1][1] + " / " + n)
        else:
            merged.append((v, n))
    below = [f"{n} ${_f(v)} ({(v / px - 1) * 100:+.1f}%)" for v, n in reversed(merged) if v < px][:3]
    above = [f"{n} ${_f(v)} ({(v / px - 1) * 100:+.1f}%)" for v, n in merged if v >= px][:3]
    if not below and not above:
        return ""
    s = f"- Key levels from ${_f(px)}: below, " + ("; ".join(below) or "none") + \
        ". Above, " + ("; ".join(above) or "none") + "."
    return s + " Use these distances for the stop and the target."


def render_sheet(t: str, tp: dict, *, hv20=None, lo20=None, hi20=None, earnings=None,
                 implied=None, stamp: str = "") -> str:
    """Pure formatting. tp = mason_dossier._section_tape() output. Lines whose data is
    missing are dropped, never filled in."""
    L = [f"{t} desk sheet{(' (' + stamp + ' ET)') if stamp else ''}:"]
    px = tp.get("price")
    moves = [(_sp(tp.get('chg_1d'), 2), "today"), (_sp(tp.get('chg_5d')), "5 days"),
             (_sp(tp.get('chg_1m')), "1 month"), (_sp(tp.get('chg_3m')), "3 months")]
    mv = ", ".join(f"{a} {b}" for a, b in moves if a)
    if _f(px):
        L.append(f"- Price ${_f(px)}" + (f" ({mv})." if mv else "."))
    if isinstance(tp.get("vs_spy_1m"), (int, float)) and isinstance(tp.get("spy_1m"), (int, float)):
        L.append(f"- Against the S&P 500 fund (SPY) over 1 month: {tp['vs_spy_1m']:+.1f} points "
                 f"(SPY {tp['spy_1m']:+.1f}%).")
    tr = []
    for k, n in (("20", "20-day"), ("50", "50-day"), ("200", "200-day")):
        a, s = tp.get(f"vs_sma{k}_pct"), tp.get(f"sma{k}")
        if _sp(a) and _f(s):
            tr.append(f"{_sp(a)} vs its {n} average (${_f(s)})")
    if tr:
        L.append("- Trend: " + ", ".join(tr) + f". {_trend_word(tp).capitalize()}.")
    if isinstance(tp.get("rsi14"), (int, float)):
        L.append(f"- RSI(14) {tp['rsi14']:.1f}{_rsi_word(tp['rsi14'])}; under 30 is oversold, over 70 overbought.")
    atr = tp.get("atr14")
    if _f(atr) and _f(px):
        L.append(f"- Typical daily move (14-day average true range): ${_f(atr)}, {tp.get('atr14_pct'):.1f}% of the "
                 f"price. One typical day from here is ${_f(px - atr)} to ${_f(px + atr)}.")
    if _f(lo20) and _f(hi20):
        L.append(f"- 20-day range: ${_f(lo20)} to ${_f(hi20)}.")
    kl = key_levels(px, tp, lo20=lo20, hi20=hi20)
    if kl:
        L.append(kl)
    if isinstance(hv20, (int, float)):
        L.append(f"- Realized volatility, last 20 days: {hv20:.1f}% a year.")
    if isinstance(tp.get("rel_volume"), (int, float)):
        L.append(f"- Volume today: {tp['rel_volume']:.2f}x its 20-day average (part of a day if the market is open).")
    if earnings:
        L.append(f"- Next earnings: {earnings}.")
    if implied:
        iv_pct = implied.get("atm_iv_pct")
        ratio, verdict = _iv_verdict(iv_pct, hv20)
        s = (f"- Options: the market prices a move of about ±{implied['implied_move_pct']:.1f}% by "
             f"{implied.get('expiry')}")
        if implied.get("days_left") is not None:
            s += f" ({implied['days_left']} day{'' if implied['days_left'] == 1 else 's'})"
        if isinstance(iv_pct, (int, float)):
            s += f"; at-the-money implied volatility {iv_pct:.1f}%"
            if implied.get("atm_iv_expiry") and implied.get("atm_iv_expiry") != implied.get("expiry"):
                s += f" (on the {implied['atm_iv_expiry']} expiry)"
            if ratio:
                s += f", {ratio}x the realized volatility ({verdict})"
        s += f" [{implied.get('src', 'snapshot')}]."
        L.append(s)
    return "\n".join(L) if len(L) > 1 else ""


def next_earnings(t: str):
    """(date, time text) of t's next earnings from Mason's calendar (calendar_events, read-only),
    or (None, "")."""
    try:
        import mason_dossier as D
        for e in D._section_calendar(t, 60):
            if "earn" in str(e.get("type") or "").lower():
                return date.fromisoformat(str(e["date"])[:10]), str(e.get("time") or "").strip()
    except Exception:
        pass
    return None, ""


def _earnings_line(t: str) -> str | None:
    d, tm = next_earnings(t)
    if not d:
        return None
    n = (d - datetime.now(_ET).date()).days
    when = {0: "today", 1: "tomorrow"}.get(n, f"in {n} days")
    return f"{d.isoformat()}{(' ' + tm) if tm else ''} ({when})"


def _implied(t: str) -> dict | None:
    """The options market's expected move: Robinhood straddle snapshot first (Mason's option
    snapshots), else a director dossier built in the last 2 hours. None when neither."""
    try:
        import mason_option_snaps as OS
        r = OS.implied_move(t)
        if r and r.get("implied_move_pct") is not None:
            r = dict(r)
            when = datetime.fromtimestamp(r["ts"], _ET).strftime("%H:%M") if r.get("ts") else "?"
            r["src"] = f"Robinhood straddle snapshot {when} ET"
            return r
    except Exception:
        pass
    try:
        import mason_dossier as D
        d = D._cache_get(t, "ticker", 7200)
        op = (d or {}).get("options") or {}
        mv = op.get("implied_move_next_event") or {}
        if isinstance(mv.get("move_pct"), (int, float)):
            stamp = str(((d.get("as_of") or {}).get("options")) or d.get("built_at") or "")
            built = stamp[11:16] if len(stamp) >= 16 else "earlier today"
            return {"implied_move_pct": float(mv["move_pct"]), "expiry": mv.get("expiry"),
                    "days_left": mv.get("dte"),
                    "atm_iv_pct": (float(op["atm_iv"]) if isinstance(op.get("atm_iv"), (int, float)) else None),
                    "atm_iv_expiry": op.get("atm_iv_expiry"),
                    "src": f"director dossier, options pulled {built} ET"}
    except Exception:
        pass
    return None


def _sheet_for(t: str, hist: dict) -> str:
    import mason_dossier as D
    q = {}
    try:
        q = D._live_quote(t) or {}
    except Exception:
        q = {}
    tp = D._section_tape(t, hist, q, None)
    df = hist.get(t)
    closes = D._anchor_closes(df, (q or {}).get("price") if not (q or {}).get("error") else None)
    hv = D.realized_vol_series(closes, 20)
    highs, lows = D._col(df, "High")[-20:], D._col(df, "Low")[-20:]
    stamp = datetime.now(_ET).strftime("%H:%M")
    hv20 = hv[-1] * 100 if hv else None
    return render_sheet(t, tp, hv20=hv20,
                        lo20=(min(lows) if lows else None), hi20=(max(highs) if highs else None),
                        earnings=_earnings_line(t), implied=_implied(t), stamp=stamp), hv20


# ── option check (options questions only) ─────────────────────────────────────
# Smallfix 2026-11-18: "put" is not in this list; options_ask() adds it as the noun (put_noun, above).
OPTIONS_ASK_RE = re.compile(r"\b(calls?|options?|strikes?|premium|contracts?|leaps?|0dte|iv|"
                            r"implied vol\w*|expir\w*|itm|otm|atm)\b", re.IGNORECASE)


def options_ask(raw, tickers=()) -> bool:
    """An options question: an options word, or "put" / "puts" as the contract (put_noun; `tickers` = the resolved
    symbols, so "nvda put" counts)."""
    return bool(OPTIONS_ASK_RE.search(raw or "") or put_noun(raw or "", tickers))
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Real month names or their usual short forms only ("market" is not March). "may" only counts
# with a day after it ("May 15"), since "I may buy" is the verb.
_MONTH_RE = re.compile(r"\b(?:(january|february|march|april|june|july|august|september|october|november|"
                       r"december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)(?![a-z])\.?"
                       r"(?:\s+(\d{1,2})(?:st|nd|rd|th)?\b)?|(may)(?![a-z])\s+(\d{1,2})(?:st|nd|rd|th)?\b)"
                       r"(?:,?\s+(20\d\d)\b)?", re.IGNORECASE)
_ISO_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_SLASH_RE = re.compile(r"(?<![\d$.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d.])")
_STRIKE_RES = [
    re.compile(r"\$?(\d+(?:\.\d+)?)\s*strike", re.IGNORECASE),
    re.compile(r"strike\s*(?:of|at|@)?\s*\$?(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?<![\w.])\$?(\d+(?:\.\d+)?)\s*(?:c|p)\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])\$?(\d+(?:\.\d+)?)\s+(?:calls?|puts?)\b", re.IGNORECASE),
]


def target_expiry(raw: str, today: date, earnings: date | None = None, earnings_time: str = "") -> str:
    """The expiry Nico names ('January 15th', 'Jan', '1/15', '2027-01-15'), as a date the chain
    tool snaps forward to the next listed expiry. A month alone means its monthly (from the 15th).
    Nothing named: the first expiry that includes the next earnings reaction when the report is
    within three weeks (the day after it, or the day itself for a before-the-open report),
    otherwise about three weeks out."""
    def _yr(mo, day):
        y = today.year
        try:
            if date(y, mo, min(day, 28)) < today.replace(day=1) or (mo == today.month and day < today.day):
                y += 1
        except Exception:
            pass
        return y
    try:
        m = _ISO_RE.search(raw)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = _MONTH_RE.search(raw)
        if m:
            name, dd = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            mo = _MONTHS[name.lower()[:3]]
            day = int(dd) if dd else 15
            y = int(m.group(5)) if m.group(5) else _yr(mo, day)
            import calendar as _cal
            return date(y, mo, max(1, min(day, _cal.monthrange(y, mo)[1]))).isoformat()
        m = _SLASH_RE.search(raw)
        if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
            mo, day = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else _yr(mo, day)
            y = y + 2000 if y < 100 else y
            return date(y, mo, day).isoformat()
    except Exception:
        pass
    from datetime import timedelta
    if isinstance(earnings, date) and 0 <= (earnings - today).days <= 21:
        before_open = "before" in (earnings_time or "").lower()
        return (earnings if before_open else earnings + timedelta(days=1)).isoformat()
    return (today + timedelta(days=21)).isoformat()


def _strike_candidates(raw: str) -> list:
    out = []
    for rx in _STRIKE_RES:
        for m in rx.finditer(raw or ""):
            try:
                out.append(float(m.group(1)))
            except Exception:
                pass
    return out


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _row_line(kind: str, r: dict, spot: float, days: int, hv20) -> str | None:
    """One contract, all numbers computed here. kind = 'call' | 'put'."""
    try:
        k, bid, ask = float(r["strike"]), float(r.get("bid") or 0), float(r.get("ask") or 0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask >= bid else float(r.get("lastPrice") or 0)
        if mid <= 0 or spot <= 0 or days <= 0:
            return None
        be = k + mid if kind == "call" else k - mid
        need = (be / spot - 1.0) * 100.0
        iv = r.get("impliedVolatility")
        s = (f"${_f(k)} {kind}: bid ${_f(bid)} / ask ${_f(ask)}, mid ${_f(mid)} = ${_f(mid * 100, 0)} a contract")
        if bid > 0 and ask > 0:
            s += f" (spread {((ask - bid) / mid * 100):.0f}% of mid)"
        s += f". Breakeven at expiry ${_f(be)}, {need:+.1f}% from ${_f(spot)}."
        if isinstance(iv, (int, float)) and iv > 0:
            T = days / 365.0
            d2 = (math.log(spot / be) - 0.5 * iv * iv * T) / (iv * math.sqrt(T)) if be > 0 else None
            if d2 is not None:
                pop = _ncdf(d2) if kind == "call" else 1.0 - _ncdf(d2)
                s += f" Chance it finishes past breakeven: about {pop * 100:.0f}% (from its implied volatility)."
            s += f" Implied volatility {iv * 100:.1f}%"
            ratio, verdict = _iv_verdict(iv * 100.0, hv20)
            if ratio:
                s += f", {ratio}x the 20-day realized ({verdict})"
            s += "."
        if isinstance(r.get("delta"), (int, float)):
            s += f" Delta {r['delta']:.2f}."
        if isinstance(r.get("theta"), (int, float)):
            s += f" Time decay about ${abs(r['theta']) * 100:,.2f} a day per contract."
        if r.get("openInterest"):
            s += f" Open interest {int(r['openInterest']):,}."
        return s
    except Exception:
        return None


def option_check(t: str, raw: str, chain_fn, *, hv20=None, today: date | None = None) -> str:
    """For an options question: the contract Nico named (strike/expiry parsed from his words) and
    the at-the-money contract, from the live chain (chain_fn(ticker, expiry) -> the get_option_chain
    tool's JSON). Every figure computed here. "" when the chain can't be read."""
    import json
    today = today or datetime.now(_ET).date()
    try:
        data = json.loads(chain_fn(t, target_expiry(raw, today)) or "{}")
    except Exception:
        return ""
    spot = data.get("spot")
    chains = data.get("chains") or {}
    if not chains or not isinstance(spot, (int, float)) or spot <= 0:
        return ""
    exp = sorted(chains)[0]
    try:
        days = (date.fromisoformat(exp) - today).days
    except Exception:
        return ""
    if days <= 0:
        days = 1
    low = (raw or "").lower()
    put = put_noun(raw or "", [t])             # smallfix 2026-11-18: the contract, not the verb ("put me down …")
    call = bool(re.search(r"\bcalls?\b", low))
    want_put = put and not call
    sides = [("put", "puts")] if want_put else [("call", "calls")]
    # review 2026-11-18: both sides when he names both, and also when "put" is there but can't be told apart from the
    # verb and no call is named ("Put on HOOD, which strike?"): calls alone would put call prices on a put question
    if (put and call) or (not put and not call and _PUT_RE.search(raw or "")):
        sides = [("call", "calls"), ("put", "puts")]
    L = [f"- Option check, {exp} expiry ({days} day{'' if days == 1 else 's'}), from Alpaca's live option quotes:"]
    strikes = _strike_candidates(raw)
    for kind, key in sides:
        rows = [r for r in (chains[exp].get(key) or []) if isinstance(r.get("strike"), (int, float))]
        if not rows:
            continue
        named = next((r for s in strikes for r in rows if abs(r["strike"] - s) < 0.01), None)
        atm = min(rows, key=lambda r: abs(r["strike"] - spot))
        for r, tag in ((named, "the one you named"), (atm, "at the money")):
            if r is None or (tag == "at the money" and named is not None and r is named):
                continue
            line = _row_line(kind, r, spot, days, hv20)
            if line:
                L.append(f"  {tag[0].upper() + tag[1:]}: {line}")
    atm_iv = None
    try:
        rows = chains[exp].get("calls") or chains[exp].get("puts") or []
        atm_iv = min(rows, key=lambda r: abs(r["strike"] - spot)).get("impliedVolatility")
    except Exception:
        pass
    if isinstance(atm_iv, (int, float)) and atm_iv > 0:
        em = spot * atm_iv * math.sqrt(days / 365.0)
        L.append(f"  One-standard-deviation move the options price in by {exp}: ±${_f(em)} "
                 f"(±{em / spot * 100:.1f}%), a range of ${_f(spot - em)} to ${_f(spot + em)}.")
    return "\n".join(L) if len(L) > 1 else ""


def sheet_block(tickers, *, timeout_s: float = 4.0, raw: str = "", chain_fn=None) -> str:
    """The desk sheet for the given names, plus (options questions, first name only, when
    chain_fn is given) the option check. Built in worker threads and abandoned after timeout_s
    (whatever finished in time is kept). "" when nothing could be built."""
    tickers = [str(t).upper() for t in (tickers or []) if t]
    if not tickers:
        return ""
    out: dict = {}
    chain: dict = {}

    def work():
        try:
            import mason_dossier as D
            hist = D._history(list(dict.fromkeys(tickers + ["SPY"])))
        except Exception as e:
            log.info(f"[chat-desk] history failed: {e}")
            return
        for t in tickers:
            try:
                if hist.get(t) is not None and len(hist[t]) >= 30:
                    s, hv20 = _sheet_for(t, hist)
                    if s:
                        out[t] = (s, hv20)
            except Exception as e:
                log.info(f"[chat-desk] sheet {t} failed: {e}")

    def work_chain():
        try:
            ed, etm = next_earnings(tickers[0])
            chain["json"] = chain_fn(tickers[0], target_expiry(raw, datetime.now(_ET).date(), ed, etm))
        except Exception as e:
            log.info(f"[chat-desk] chain {tickers[0]} failed: {e}")

    deadline = time.time() + max(0.5, float(timeout_s))
    ths = [threading.Thread(target=work, name="chat-desk-sheet", daemon=True)]
    if chain_fn is not None and options_ask(raw, tickers):
        ths.append(threading.Thread(target=work_chain, name="chat-desk-chain", daemon=True))
    for th in ths:
        th.start()
    for th in ths:
        th.join(max(0.0, deadline - time.time()))
    got = dict(out)
    parts = []
    for i, t in enumerate(tickers):
        s, hv20 = got.get(t, ("", None))
        if i == 0 and chain.get("json"):
            oc = option_check(t, raw, lambda *_a: chain["json"], hv20=hv20)
            if oc:
                s = (s + "\n" + oc) if s else f"{t} desk sheet:\n{oc}"
        if s:
            parts.append(s)
    return (SHEET_HEAD + "\n" + "\n\n".join(parts)) if parts else ""


# ── entry points used by mason_app ────────────────────────────────────────────
def turn_blocks(raw: str, tickers, full_cfg: dict | None, chain_fn=None) -> list:
    """Extra trailing blocks for the current user message: [sheet?, brief?] in that order
    (the brief is always LAST). [] when the switch is off or this is not a trading question."""
    try:
        c = cfg(full_cfg)
        if not c.get("enabled"):
            return []
        tickers = list(dict.fromkeys(str(t).upper() for t in (tickers or []) if t))
        if not is_trade_ask(raw, tickers):
            return []
        blocks = []
        if c.get("sheet"):
            s = sheet_block(tickers[:max(1, int(c.get("max_tickers") or 3))],
                            timeout_s=float(c.get("sheet_timeout_s") or 6.0),
                            raw=raw, chain_fn=chain_fn if c.get("option_check") else None)
            if s:
                blocks.append(s)
        if c.get("brief"):
            blocks.append(BRIEF)
        return blocks
    except Exception as e:
        log.info(f"[chat-desk] turn_blocks failed: {e}")
        return []


def with_doctrine(system, full_cfg: dict | None, *, claude_turn: bool):
    """system (str or Anthropic block list) plus the DOCTRINE block, only when the switch is
    on AND this turn runs on a Claude model. Otherwise the very same object is returned."""
    try:
        c = cfg(full_cfg)
        if not (c.get("enabled") and c.get("doctrine_claude") and claude_turn):
            return system
        if isinstance(system, list):
            return list(system) + [{"type": "text", "text": DOCTRINE}]
        return ((system or "") + "\n\n" + DOCTRINE).strip()
    except Exception:
        return system
