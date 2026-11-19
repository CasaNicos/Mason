"""
mason_allocator.py — ETF ALLOCATOR (Agent I, 2026-09-28). Answers "I have $X — what
ETF for a relatively short-term (2-8 week) gain?" for Nico's personal accounts
(Roth / Savings / Play, from personal_positions incl. CASH rows) and for Mason's own
Alpaca PAPER account. ADVICE ONLY: this module never places an order, and Nico's
personal accounts are not tradable by Mason at all.

Contracts: docs/catalyst_desk/WEALTH_MANAGER_CONTRACTS.md (builder I).

Flow of allocate(question, *, amount=None, account=None, research_fn=None):
  1. gather — accounts (personal positions valued at the latest daily close, % vs
     cost basis, CASH per account; Alpaca paper cash/positions), the ETF screen
     (mason_etf_screen), the index outlook, a macro dossier summary (mason_dossier)
  2. ONE Opus 5.5 call (no web) via mason_llm.complete_research, effort from
     index_desk.allocator.effort; pre-call $ estimate + mason_director.budget_ok
     (skip = no call at all)
  3. raw output saved to docs/catalyst_desk/allocator_output_<ts>.md FIRST, then the
     spend ledger (mason_director.record_spend, kind='allocator'), then validation
     (ETF must be in the screen universe; per-account $ capped at available cash, or
     at the amount Nico named), then follow-ups (mason_director.add_followups,
     created_by='allocator') — the only DB write.
  Never raises; returns a plain dict ({"ok": bool, "brief", "picks", ...}).

Gate: index_desk.allocator.enabled (read per call). dry_run=True builds the prompt
and the cost estimate with NO model call and ignores the gate (free).
Tests inject research_fn(system, user, spec) -> {"text", "cost_usd", "usage", "model"}.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path

import mason_db
import mason_etf_screen as ES

log = logging.getLogger("mason.allocator")

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "mason.config.json"
OUT_DIR = BASE / "docs" / "catalyst_desk"
ALPACA_LABEL = "Alpaca paper"
_SAFE_MODEL = "claude-opus-5-5"

_DEFAULTS = {"enabled": False, "effort": "medium", "model": _SAFE_MODEL,
             "max_output_tokens": 8000, "expected_output_tokens": 6000,
             "screen_tokens": 2000, "outlook_tokens": 700, "macro_tokens": 500,
             "max_picks": 3, "default_review_days": 21}

# account label (lower) -> tax treatment line for the prompt
_ACCOUNT_TYPES = {
    "roth": "Roth IRA — tax-advantaged: gains/rotations inside it are tax-free (qualified); "
            "cash withdrawn cannot simply be re-contributed (annual limit)",
    "roth ira": None,  # alias → roth
    "savings": "TAXABLE brokerage — gains held < 1y are short-term (taxed as ordinary income); "
               "a loss sale + rebuy within 30d is a wash sale",
    "play": "TAXABLE brokerage (speculative sleeve) — short-term gains taxed as ordinary income",
    ALPACA_LABEL.lower(): "Mason's own Alpaca PAPER account — simulated money, no tax; "
                          "advice only, Mason places nothing from this call",
}
# ── RH Expansion S2-T8 ── S1 P1: RH account labels alias the existing entries (no new wording)
_ACCOUNT_TYPES.update({"play money": _ACCOUNT_TYPES["play"], "etf savings": _ACCOUNT_TYPES["savings"]})
# ── end S2-T8 ──
# N8 (QA, #58): generic words a blank-nickname account's model label can reduce to ("Account", "individual"). A candidate name that is
# exactly one of them never acts as a free-text match in Nico's question ("which account is best for $500?" has no target). Names
# that are known account sleeves (_ACCOUNT_TYPES: "roth", "savings", "play" ...) stay matchable, and the explicit account= path
# is untouched.
_GENERIC_WORDS = frozenset({"account", "accounts", "individual", "ira", "roth", "brokerage", "cash", "margin"})
_ALPACA_ALIASES = re.compile(r"\b(alpaca|paper|mason'?s own|your own|yourself|your account)\b", re.I)
# same-exposure families (concentration check)
_OVERLAP = [{"SPY", "VOO", "VTI", "RSP", "DIA"}, {"QQQ", "QQQM", "XLK"}, {"SMH", "SOXX"},
            {"MTUM", "SPMO"}, {"GLD"}, {"IWM", "VBR"}, {"VXUS", "EFA"}]


# ── config ──────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        c = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    except Exception:
        c = {}
    a = dict(_DEFAULTS)
    a.update(((c.get("index_desk") or {}).get("allocator")) or {})
    if str(a.get("model") or "").strip() in ("", "claude-opus-5"):   # never Opus 5
        a["model"] = _SAFE_MODEL
    return a


def index_desk_enabled() -> bool:
    try:
        c = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
        return bool((c.get("index_desk") or {}).get("enabled"))
    except Exception:
        return False


def allocator_enabled() -> bool:
    return bool(_cfg().get("enabled"))


# ── hooks (tests monkeypatch) ─────────────────────────────────────────────────────

def _alpaca_account() -> dict:
    import alpaca_integration as A
    return A.get_account()


def _alpaca_positions() -> list:
    import alpaca_integration as A
    return A.get_positions()


def _macro_text(max_tokens: int) -> str:
    import mason_dossier as D
    d = D.build_macro_dossier([], headline_hint="")
    txt = D.macro_dossier_text(d, max_tokens=max_tokens + 800)
    # keep only the cross-asset block: the index outlook already carries the calendar,
    # and with no event kinds the transmission map is empty
    cut = txt.find("## TRANSMISSION")
    txt = txt[:cut].rstrip() if cut > 0 else txt
    return txt if len(txt) <= max_tokens * 4 else txt[:max_tokens * 4 - 14].rstrip() + "\n…[truncated]"


def _prices(symbols: list) -> dict:
    """{sym: (last daily close, bar date)} from the shared yfinance series cache."""
    out = {}
    if not symbols:
        return out
    hist = ES._bars(list(symbols)) or {}
    for s in symbols:
        c = ES._closes(hist.get(s))
        if c:
            out[s] = (c[-1], ES._last_date(hist.get(s)))
    return out


def _today() -> date:
    return date.today()


# ── helpers ───────────────────────────────────────────────────────────────────────

def _num(v):
    try:
        f = float(str(v).replace(",", "").replace("$", "")) if isinstance(v, str) else float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _tok(s: str) -> int:
    return len(s or "") // 4


def _acct_key(label: str) -> str:
    k = (label or "").strip().lower()
    return "roth" if k in ("roth ira", "roth") else k


def _bl(label):
    """N6b: a label as a MODEL sees it - the ••NNNN mask dropped (mason_robinhood.brain_label); the stored and UI labels
    keep it. Empty/None stay as they are. If the shared function cannot be loaded the label is replaced, never leaked."""
    if label is None or label == "":
        return label
    try:
        import mason_robinhood
        return mason_robinhood.brain_label(label)
    except Exception:
        return "Account"


def _ml(a: dict) -> str:
    """N6b: the account's MODEL-facing label: ``a["mlabel"]`` (set by gather_accounts: the stored label without its
    ••NNNN mask, made unique with " (2)", " (3)"), else the stripped stored label. The stored ``label`` keeps the mask."""
    return str(a.get("mlabel") or _bl(a.get("label")) or "")


def _ro_conn():
    conn = sqlite3.connect(f"file:{Path(mason_db.DB_FILE)}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _personal_rows() -> list:
    """Open, non-deleted personal positions (READ-ONLY connection)."""
    conn = None
    try:
        conn = _ro_conn()
        rows = conn.execute(
            "SELECT ticker, account_label, shares, cost_basis, entry_date FROM personal_positions "
            "WHERE COALESCE(is_deleted,0)=0 AND status='open' ORDER BY account_label, ticker").fetchall()
        return [dict(r) for r in rows if not str(r["account_label"] or "").startswith("__")]
    except Exception as e:
        log.info(f"[allocator] personal positions unavailable: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def gather_accounts(*, include_alpaca: bool = True, screen: dict | None = None) -> dict:
    """{label: {"type", "cash", "positions": [...], "invested", "total"}} — never raises."""
    accts: dict = {}
    rows = _personal_rows()
    screen_px = {r["symbol"]: (r.get("last"), r.get("last_date"))
                 for r in (screen or {}).get("rows") or [] if r.get("last") is not None}
    need = sorted({str(r["ticker"]).upper() for r in rows
                   if str(r["ticker"]).upper() != "CASH" and str(r["ticker"]).upper() not in screen_px})
    try:
        px = dict(screen_px, **_prices(need))
    except Exception:
        px = dict(screen_px)
    today = _today()
    for r in rows:
        label = str(r["account_label"]).strip()
        key = _acct_key(label)
        a = accts.setdefault(key, {"label": label, "type": _ACCOUNT_TYPES.get(key) or "type unknown",
                                   "cash": 0.0, "positions": [], "source": "personal_positions (manual)"})
        t = str(r["ticker"]).upper()
        sh, cb = _num(r["shares"]) or 0.0, _num(r["cost_basis"]) or 0.0
        if t == "CASH":
            a["cash"] += sh * (cb or 1.0)
            continue
        p = (px.get(t) or (None, None))[0]
        held = None
        try:
            held = (today - datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()).days
        except Exception:
            pass
        a["positions"].append({"ticker": t, "shares": sh, "cost_basis": cb, "price": p,
                               "value": (sh * p) if p else sh * cb,
                               "pct_vs_cost": ((p / cb - 1) * 100) if (p and cb) else None,
                               "entry_date": r["entry_date"], "days_held": held,
                               "in_universe": t in ES.UNIVERSE})
    if include_alpaca:
        k = ALPACA_LABEL.lower()
        a = {"label": ALPACA_LABEL, "type": _ACCOUNT_TYPES[k], "cash": None, "positions": [],
             "source": "alpaca_integration (live paper)"}
        try:
            acc = _alpaca_account() or {}
            raw_cash = float(acc.get("cash") or 0.0)
            a.update(cash=max(raw_cash, 0.0), raw_cash=raw_cash, equity=acc.get("equity"),
                     buying_power=acc.get("buying_power"))
            if raw_cash < 0:
                a["note"] = (f"cash is NEGATIVE (${raw_cash:,.2f}: margin in use) — no free cash; "
                             f"equity ${float(acc.get('equity') or 0):,.2f}")
        except Exception as e:
            a["error"] = f"account: {str(e)[:100]}"
        try:
            pos = _alpaca_positions()
            if not isinstance(pos, list):
                raise ValueError(f"unexpected positions payload {type(pos).__name__}")
            a["positions"] = [{"ticker": p.get("ticker"), "shares": p.get("qty"), "cost_basis": p.get("avg_entry"),
                               "price": p.get("current_price"), "value": p.get("market_value"),
                               "pct_vs_cost": p.get("unrealized_plpc"), "entry_date": None, "days_held": None,
                               "in_universe": p.get("ticker") in ES.UNIVERSE} for p in pos if isinstance(p, dict)]
        except Exception as e:     # e.g. Alpaca 429 rate limit returns a dict → degrade, keep the cash
            a["positions_error"] = str(e)[:100]
        accts[k] = a
    # N6b (QA R1): unique, mask-free labels for the model (deterministic: dict order). An account whose label has no mask shows
    # as is; a masked one shows without it and gets " (2)", " (3)" until its _acct_key collides with NO stored key (nor alias:
    # "Roth IRA" is the key "roth") and no other model label, so a reply always names exactly the account the model was shown.
    used = set()
    for a in accts.values():
        lab = str(a["label"])
        if str(_bl(lab) or "") == lab:
            a["mlabel"] = lab
            used.add(_acct_key(lab))
    for a in accts.values():
        if "mlabel" in a:
            continue
        base = str(_bl(a["label"]) or "")
        cand, n = base, 1
        while _acct_key(cand) in accts or _acct_key(cand) in used:
            n += 1
            cand = f"{base} ({n})"
        a["mlabel"] = cand
        used.add(_acct_key(cand))
    for a in accts.values():
        inv = sum(float(p.get("value") or 0) for p in a["positions"])
        if a.get("positions_error") and a.get("equity") is not None:   # positions 429'd: equity - cash
            inv = float(a["equity"] or 0) - float(a.get("raw_cash") or 0)
        a["invested"] = inv
        a["total"] = inv + float(a.get("cash") or 0)
    return accts


_AMT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?\b|\b(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:dollars|bucks|usd)\b", re.I)


def parse_amount(q: str):
    m = _AMT_RE.search(q or "")
    if not m:
        return None
    num, k = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    v = _num(num)
    if v is None:
        return None
    return v * 1000 if k else v


def _by_ml(accts: dict) -> dict:
    """{_acct_key(model label): stored key}: the lookup of the names the model was shown (N6b)."""
    return {_acct_key(_ml(a)): k for k, a in accts.items()}


def resolve_account(q: str, accts: dict, account: str | None = None) -> str | None:
    """Explicit account arg wins, else an account label named in the question."""
    by_ml = _by_ml(accts)                                            # N6b: the name the model was shown
    if account:
        k = _acct_key(account)
        if k in by_ml:                                               # R1: the model-facing label first, stored keys second
            return by_ml[k]
        if k in accts:
            return k
        if _ALPACA_ALIASES.search(account) and ALPACA_LABEL.lower() in accts:
            return ALPACA_LABEL.lower()
        return None
    ql = (q or "").lower()
    # N6b (QA R3): the LONGEST account name found in the question wins, across the model-facing labels, the stored labels and
    # keys and the aliases ("roth" = "roth ira"); ties go to the first account. So "put $500 into Play Money (2)" reaches
    # "Play Money (2)", not "Play Money", and "ETF Savings" is not taken for "Savings". Names end in ")" or a word
    # character, so the boundary is a look-around, not \b.
    best, best_len = None, 0
    for k, a in accts.items():
        if k == ALPACA_LABEL.lower():
            continue
        names = {k, str(a.get("label") or "").lower(), _ml(a).lower()}
        if k == "roth":
            names.add("roth ira")
        for nm in names:
            nm = nm.strip()
            if nm in _GENERIC_WORDS and nm not in _ACCOUNT_TYPES:
                continue                    # N8: "account" / "individual" ... alone never targets an account (known sleeves excepted)
            if nm and len(nm) > best_len and re.search(rf"(?<!\w){re.escape(nm)}(?!\w)", ql):
                best, best_len = k, len(nm)
    if best is not None:
        return best
    if _ALPACA_ALIASES.search(q or "") and ALPACA_LABEL.lower() in accts:
        return ALPACA_LABEL.lower()
    return None


def _fmt_accounts(accts: dict, target: str | None) -> str:
    L = ["=== ACCOUNTS (personal = Nico's own outside brokerages, NOT tradable by Mason; "
         "prices = latest yfinance daily close) ==="]
    for k, a in accts.items():
        star = " ← TARGET" if k == target else ""
        if a.get("error"):
            L.append(f"[{_ml(a)}]{star} unavailable ({a['error']})")
            continue
        cash = float(a.get("cash") or 0.0)
        L.append(f"[{_ml(a)}]{star} {a['type']} · cash ${cash:,.2f} · invested ${a['invested']:,.2f} · "
                 f"total ${a['total']:,.2f} · src {a.get('source')}"
                 + (f" · NOTE {a['note']}" if a.get("note") else "")
                 + (f" · positions unavailable ({a['positions_error']})" if a.get("positions_error") else ""))
        tot = a["total"] or 1.0
        for p in sorted(a["positions"], key=lambda p: -(p.get("value") or 0)):
            pc = p.get("pct_vs_cost")
            price = p.get("price")
            L.append(f"  {p['ticker']} {p['shares']:g} sh @ cost {p['cost_basis']:.2f} → "
                     f"{'n/a' if price is None else f'{price:.2f}'} "
                     f"({'n/a' if pc is None else f'{pc:+.1f}%'}) · ${float(p.get('value') or 0):,.0f} "
                     f"({float(p.get('value') or 0) / tot * 100:.0f}% of acct)"
                     + (f" · recorded entry {p['entry_date']}" if p.get("entry_date") else ""))
    # cross-account exposure families
    held = {}
    for a in accts.values():
        for p in a.get("positions") or []:
            held[p["ticker"]] = held.get(p["ticker"], 0.0) + float(p.get("value") or 0)
    fams = []
    for fam in _OVERLAP:
        hit = {t: v for t, v in held.items() if t in fam}
        if hit:
            fams.append(f"{'/'.join(sorted(fam))}: holds " + ", ".join(f"{t} ${v:,.0f}" for t, v in hit.items()))
    if fams:
        L.append("Overlap families (same underlying exposure): " + " | ".join(fams))
    return "\n".join(L)


# ── prompt ────────────────────────────────────────────────────────────────────────

SYSTEM = """You are Mason's WEALTH MANAGER for Nico: a disciplined, plain-spoken advisor allocating cash into ETFs for a SHORT-TERM horizon (2-8 weeks). You are given his accounts (with account types), a deterministic ETF screen (momentum-with-trend score, returns, trend vs 50/200-day averages, RSI, volatility, drawdown, expense ratio, regime notes), index-level facts for the next sessions, a macro cross-asset summary and the econ calendar. Every number you cite must come from these inputs; say "n/a" rather than invent one.

Rules (hard):
- ADVICE ONLY. Nothing is executed from this answer. Nico's personal accounts are not connected to Mason; he places any trade himself.
- Recommend ETF SHARES only, and ONLY symbols that appear in the ETF SCREEN. No options, no leveraged/inverse ETFs, no margin, no multi-leg structures.
- 1-3 picks. Dollar amounts must fit the cash available in the account they go in (or the amount Nico named). Show share counts at the listed last price (fractional shares OK; say so if whole shares are needed). Leaving part of the cash uninvested is allowed — say why.
- Respect the account type: Roth vs taxable (short-term gains taxed as ordinary income, wash-sale rule) — one brief line on the tax implication.
- Check concentration against what he ALREADY holds (overlap families are listed: e.g. QQQM≈QQQ, VOO≈SPY≈VTI, SPMO≈MTUM). Prefer adding a differentiated exposure unless you explain why doubling up is right.
- Fit the regime: use the posture/top-risk zone, rates/dollar/VIX and upcoming high-impact releases (CPI/NFP/FOMC/PCE) to choose between momentum and defensive exposure and to set the entry approach (e.g. scale in around a release vs all-at-once).
- Every pick needs: entry approach, a concrete exit rule (price stop and/or close below a moving average, plus a profit-taking rule), a review date (YYYY-MM-DD, within 2-8 weeks), and the main risks.
- Be honest about uncertainty: a 2-8 week ETF move is mostly noise plus regime; don't promise returns.

Output: a brief (≤ 350 words, markdown, lead with the recommendation), then ONE fenced ```json block:
{"picks": [{"etf": "SYM", "amount_usd": 0, "shares": 0, "account": "<account label exactly as listed>", "horizon": "e.g. 3-6 weeks", "entry": "...", "stop_or_exit_rule": "...", "review_date": "YYYY-MM-DD", "rationale": "..."}],
 "follow_ups": [{"date": "YYYY-MM-DD", "ticker": "SYM or null", "action": "what to re-check", "reason": "..."}]}"""


def build_prompt(question: str, *, amount=None, account=None, screen: dict | None = None,
                 accts: dict | None = None, cfg: dict | None = None) -> dict:
    """Assemble the (system, user) pair + meta. Free — no model call. Never raises."""
    cfg = cfg or _cfg()
    t0 = time.time()
    screen = screen if screen is not None else ES.etf_screen()
    accts = accts if accts is not None else gather_accounts(screen=screen)
    amt = _num(amount) if amount is not None else parse_amount(question)
    tgt = resolve_account(question, accts, account)
    sections = {}
    sections["accounts"] = _fmt_accounts(accts, tgt)
    sections["screen"] = ES.etf_screen_text(int(cfg["screen_tokens"]), screen=screen)
    try:
        sections["outlook"] = ES.index_outlook_text(int(cfg["outlook_tokens"]))
    except Exception as e:
        sections["outlook"] = f"INDEX OUTLOOK unavailable: {e}"
    try:
        sections["macro"] = _macro_text(int(cfg["macro_tokens"]))
    except Exception as e:
        sections["macro"] = f"MACRO DOSSIER unavailable: {e}"
    today = _today()
    ask = [f"Today: {today.isoformat()} ({today.strftime('%A')}).",
           f"Nico asks: \"{(question or '').strip()[:600]}\""]
    if tgt:
        a = accts[tgt]
        ask.append(f"Target account: {_ml(a)} ({a['type']}); cash available ${float(a.get('cash') or 0):,.2f}.")
    else:
        ask.append("Target account: not specified — choose the most suitable account(s) from those listed and say why.")
    if amt is not None:
        ask.append(f"Amount to allocate: ${amt:,.2f}" + (
            f" (exceeds recorded cash ${float(accts[tgt].get('cash') or 0):,.2f} in {_ml(accts[tgt])} — Nico "
            f"named this amount, so treat it as available)" if tgt and amt > float(accts[tgt].get('cash') or 0) else "") + ".")
    else:
        ask.append("Amount: not specified — size from available cash, keeping a sensible reserve.")
    user = "\n\n".join([" ".join(ask), sections["accounts"], sections["screen"], sections["outlook"],
                        sections["macro"],
                        "Answer now: brief first, then the single ```json block. 1-3 picks from the ETF SCREEN only."])
    return {"system": SYSTEM, "user": user, "target": tgt, "amount": amt,
            "input_tokens_est": _tok(SYSTEM) + _tok(user),
            "section_tokens": {k: _tok(v) for k, v in sections.items()},
            "build_seconds": round(time.time() - t0, 2), "accounts": accts, "screen": screen}


# ── validation ────────────────────────────────────────────────────────────────────

def _price_map(screen: dict) -> dict:
    return {r["symbol"]: r.get("last") for r in (screen or {}).get("rows") or [] if r.get("last")}


def validate_picks(obj: dict, *, accts: dict, screen: dict, target: str | None, amount=None,
                   max_picks: int = 3, today: date | None = None, default_review_days: int = 21) -> dict:
    """Deterministic checks on the model's picks. Never raises.
    - ETF must be in the screen universe (else rejected)
    - account must be a known account (else → target; else rejected)
    - amount: explicit amount > 0; per account the running total is capped at
      `amount` (if Nico named one, for the target account) else at the account's cash;
      over-cap picks are trimmed and flagged, a pick trimmed below $1 is dropped
    - shares recomputed from amount / screen price when missing or inconsistent (>5%)
    - review_date must be after today and within 200 days (else default + flag)"""
    today = today or _today()
    px = _price_map(screen)
    by_ml = _by_ml(accts)
    out, rejected, flags = [], [], []
    remaining = {}
    for k, a in accts.items():
        cap = a.get("cash")
        if amount is not None and k == target:
            cap = float(amount)
        remaining[k] = float(cap) if cap is not None else 0.0
    for i, p in enumerate((obj or {}).get("picks") or []):
        try:
            if not isinstance(p, dict):
                rejected.append({"pick": p, "reason": "not an object"})
                continue
            if len(out) >= max_picks:
                rejected.append({"pick": p, "reason": f"over max {max_picks} picks"})
                continue
            etf = str(p.get("etf") or "").upper().strip()
            if etf not in ES.UNIVERSE:
                rejected.append({"pick": p, "reason": f"{etf or '?'} not in the ETF screen universe"})
                continue
            price = _num(px.get(etf))
            if not price:
                rejected.append({"pick": p, "reason": f"{etf}: no current price in the screen"})
                continue
            ak = _acct_key(str(p.get("account") or ""))
            ak = by_ml.get(ak, ak)                  # N6b (R1): the label the model was shown wins over a stored key / alias
            if ak not in accts:
                alias = resolve_account(str(p.get("account") or ""), accts, str(p.get("account") or "") or None)
                ak = alias or target
                if ak:
                    flags.append(f"{etf}: account '{p.get('account')}' mapped to {_ml(accts[ak])}")
            if not ak or ak not in accts:
                rejected.append({"pick": p, "reason": f"{etf}: unknown account '{p.get('account')}'"})
                continue
            amt = _num(p.get("amount_usd"))
            if amt is None or amt <= 0:
                sh = _num(p.get("shares"))
                amt = sh * price if sh and sh > 0 else None
            if amt is None or amt <= 0:
                rejected.append({"pick": p, "reason": f"{etf}: no positive amount"})
                continue
            item = {"etf": etf, "account": _ml(accts[ak]), "price": round(price, 2),
                    "horizon": str(p.get("horizon") or "")[:60], "entry": str(p.get("entry") or "")[:300],
                    "stop_or_exit_rule": str(p.get("stop_or_exit_rule") or "")[:300],
                    "rationale": str(p.get("rationale") or "")[:500], "flags": []}
            cap = remaining.get(ak, 0.0)
            if amt > cap + 0.005:
                if cap < 1.0:
                    rejected.append({"pick": p, "reason": f"{etf}: ${amt:,.2f} but only ${cap:,.2f} left in {_ml(accts[ak])}"})
                    continue
                item["flags"].append(f"trimmed ${amt:,.2f} → ${cap:,.2f} (available in {_ml(accts[ak])})")
                amt = cap
            remaining[ak] = cap - amt
            sh_model = _num(p.get("shares"))
            sh_calc = amt / price
            if sh_model is None or sh_model <= 0 or abs(sh_model * price - amt) > 0.05 * amt:
                if sh_model:
                    item["flags"].append(f"shares {sh_model:g} inconsistent with ${amt:,.2f} @ {price:.2f}; recomputed")
                shares = round(sh_calc, 3)
            else:
                shares = sh_model
            item.update(amount_usd=round(amt, 2), shares=shares, whole_shares=int(math.floor(sh_calc)))
            rd = None
            try:
                rd = datetime.strptime(str(p.get("review_date") or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
            if rd is None or rd <= today or rd > today + timedelta(days=200):
                new = today + timedelta(days=int(default_review_days))
                item["flags"].append(f"review_date '{p.get('review_date')}' invalid → {new.isoformat()}")
                rd = new
            item["review_date"] = rd.isoformat()
            flags.extend(f"{etf}: {f}" for f in item["flags"])
            out.append(item)
        except Exception as e:
            rejected.append({"pick": p, "reason": f"validation error: {e}"})
    return {"picks": out, "rejected": rejected, "flags": flags}


def _followup_items(picks: list, obj: dict) -> list:
    items = []
    for p in picks:
        items.append({"ticker": p["etf"], "date": p["review_date"],
                      "action": (f"Review allocator pick {p['etf']} in {p['account']} "
                                 f"(${p['amount_usd']:,.0f} ≈ {p['shares']:g} sh @ ~{p['price']:.2f}); "
                                 f"exit rule: {p['stop_or_exit_rule'] or 'n/a'}")[:300],
                      "reason": "allocator pick review date"})
    for f in (obj or {}).get("follow_ups") or []:
        if isinstance(f, dict):
            items.append({"ticker": f.get("ticker"), "date": f.get("date"),
                          "action": f"[allocator] {str(f.get('action') or '')}"[:300] if f.get("action") else "",
                          "reason": f.get("reason")})
    return items


# ── main entry ────────────────────────────────────────────────────────────────────

def _default_research(system: str, user: str, spec: dict) -> dict:
    import mason_llm
    return mason_llm.complete_research(system, user, model=spec["model"], effort=spec["effort"],
                                       max_tokens=int(spec["max_output_tokens"]), web=None)


def _budget_spec(cfg: dict) -> dict:
    import mason_director as MD
    v2 = MD.v2_cfg()
    # The allocator gets its own small DAILY reserve on top of the director's daily cap
    # (09-28: two anchors had already used $0.95 of $1.00, which would starve every ETF
    # question). The MONTHLY cap stays shared — total Opus spend never exceeds it.
    reserve = float(cfg.get("daily_reserve_usd", 0.30) or 0.0)
    return dict(v2, model=cfg["model"], effort=cfg["effort"], web={},
                daily_usd_cap=float(v2.get("daily_usd_cap", 1.0)) + reserve,
                expected_output_tokens=int(cfg["expected_output_tokens"]),
                max_output_tokens=int(cfg["max_output_tokens"]))


def allocate(question: str, *, amount=None, account=None, research_fn=None, dry_run: bool = False,
             screen: dict | None = None, accts: dict | None = None, cfg: dict | None = None) -> dict:
    """One allocation answer. Never raises; advice only (no orders)."""
    import mason_director as MD
    alloc_id = uuid.uuid4().hex[:12]
    base = {"ok": False, "alloc_id": alloc_id, "question": (question or "")[:300]}
    try:
        cfg = dict(_DEFAULTS, **(cfg or _cfg()))
        if cfg.get("model") in ("", None, "claude-opus-5"):
            cfg["model"] = _SAFE_MODEL
        if not dry_run and not cfg.get("enabled"):
            return {**base, "skipped": "index_desk.allocator.enabled is false"}
        pr = build_prompt(question, amount=amount, account=account, screen=screen, accts=accts, cfg=cfg)
        spec = _budget_spec(cfg)
        est = MD.estimate_cost(pr["system"], pr["user"], spec)
        ok, why = MD.budget_ok(est, spec)
        base.update(est_usd=round(est, 4), input_tokens_est=pr["input_tokens_est"],
                    section_tokens=pr["section_tokens"], target=(pr["accounts"].get(pr["target"]) or {}).get("label")
                    if pr["target"] else None, amount=pr["amount"], budget_note=why, model=cfg["model"],
                    effort=cfg["effort"])
        if dry_run:
            return {**base, "ok": True, "dry_run": True, "budget_ok": ok, "system": pr["system"], "user": pr["user"]}
        if not ok:
            log.warning(f"[allocator] skipped — {why}")
            return {**base, "skipped": f"budget: {why}"}
        research_fn = research_fn or _default_research
        try:
            res = research_fn(pr["system"], pr["user"], spec) or {}
        except Exception as e:
            log.warning(f"[allocator] model call failed: {e}")
            return {**base, "error": f"model call failed: {e}"}
        cost = float(res.get("cost_usd") or 0.0)
        errors = []
        # 1) paid output to disk FIRST
        raw_path = Path(OUT_DIR) / f"allocator_output_{datetime.now():%Y%m%d-%H%M%S}_{alloc_id}.md"
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(f"<!-- allocator · {res.get('model') or cfg['model']} · ${cost:.4f} · usage "
                                f"{json.dumps(res.get('usage'))} · q: {json.dumps((question or '')[:200])} · "
                                f"amount {pr['amount']} · target {base.get('target')} -->\n\n"
                                + (res.get("text") or ""), encoding="utf-8")
        except Exception as e:
            raw_path = None
            errors.append(f"raw save: {e}")
        # 2) spend ledger (file append)
        try:
            MD.record_spend(cost, kind="allocator", slate_id=alloc_id, note="index_desk.allocator")
        except Exception as e:
            errors.append(f"spend ledger: {e}")
        brief, obj, perr = MD.parse_output(res.get("text") or "")
        if perr:
            errors.append(f"parse: {perr}")
        v = validate_picks(obj or {}, accts=pr["accounts"], screen=pr["screen"], target=pr["target"],
                           amount=pr["amount"], max_picks=int(cfg["max_picks"]),
                           default_review_days=int(cfg["default_review_days"]))
        # 3) the only DB write: follow-ups (+ their reminders)
        fups = []
        try:
            allowed = set(ES.UNIVERSE) | {p["ticker"] for a in pr["accounts"].values() for p in a.get("positions") or []}
            fups = MD.add_followups(_followup_items(v["picks"], obj or {}), slate_id=alloc_id,
                                    allowed=allowed, max_items=6, created_by="allocator")
        except Exception as e:
            errors.append(f"follow-ups: {e}")
        return {**base, "ok": True, "brief": brief, "picks": v["picks"], "rejected": v["rejected"],
                "flags": v["flags"], "follow_ups": fups, "cost_usd": round(cost, 4),
                "raw_path": str(raw_path) if raw_path else None, "errors": errors,
                "note": "Advice only — no orders placed; personal accounts are not tradable by Mason."}
    except Exception as e:
        log.warning(f"[allocator] failed: {e}")
        return {**base, "error": f"allocator failed: {e}"}


def allocation_text(r: dict) -> str:
    """Compact chat reply from an allocate() result."""
    if not r.get("ok"):
        return f"Allocator did not run: {r.get('skipped') or r.get('error') or 'unknown'}"
    L = [r.get("brief") or "(no brief)"]
    if r.get("picks"):
        L.append("\nValidated picks (advice only — you place the trade):")
        for p in r["picks"]:
            L.append(f"- {p['etf']} · {p['account']} · ${p['amount_usd']:,.2f} ≈ {p['shares']:g} sh @ ~{p['price']:.2f} "
                     f"(whole: {p['whole_shares']}) · review {p['review_date']} · exit: {p['stop_or_exit_rule'] or 'n/a'}")
    else:
        L.append("\nNo valid picks.")
    if r.get("rejected"):
        L.append("Rejected: " + "; ".join(x["reason"] for x in r["rejected"])[:400])
    if r.get("flags"):
        L.append("Flags: " + "; ".join(r["flags"])[:400])
    if r.get("follow_ups"):
        L.append("Follow-ups set: " + ", ".join(f"{f.get('ticker') or 'macro'} {f['due_date']}" for f in r["follow_ups"]))
    L.append(f"(Opus 5.5 · ${r.get('cost_usd', 0):.3f})")
    return "\n".join(L)


# ── chat tools ────────────────────────────────────────────────────────────────────

CHAT_TOOLS = [
    {
        "name": "allocate_cash",
        "description": (
            "Wealth-manager ETF allocation for SHORT-TERM (2-8 week) gains. Call when Nico asks where to put "
            "cash, which ETF to buy, or how to deploy $X in an account (Roth / Savings / Play, or Mason's own "
            "Alpaca paper account). Runs ONE paid Opus 5.5 call (~$0.15-0.25 each, counted against the $20/month "
            "Opus budget) over his accounts, the ETF screen, index outlook and macro — so do NOT call it for "
            "general ETF questions (use etf_screen, free) and never call it twice for the same question. "
            "Returns a brief with 1-3 ETFs, $ amounts/share counts, entry, exit rule, review date and risks. "
            "ADVICE ONLY: nothing is ordered; Nico's personal accounts are not tradable by Mason. Relay the brief "
            "and picks as returned."),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Nico's request, verbatim"},
                "amount_usd": {"type": "number", "description": "Dollar amount to allocate, if stated"},
                "account": {"type": "string",
                            "description": "Account label if stated: Roth, Savings, Play, or 'Alpaca paper' for Mason's own"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "etf_screen",
        "description": (
            "FREE deterministic ETF screen (no LLM): ~50 curated ETFs (broad, sectors, semis, factors, themes, "
            "international, bonds, commodities, bitcoin) ranked by a short-term (2-8 week) momentum-with-trend "
            "score, with 1w/1m/3m/6m returns, vs SPY, 50/200-day trend, RSI, volatility, 3-month drawdown, "
            "expense ratio and regime notes. Use for 'which ETFs are strongest', 'how are sectors doing', or "
            "before discussing an ETF idea. Optional category filter. Quote numbers exactly as returned."),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(ES.CATEGORIES),
                             "description": "Optional filter"},
                "top": {"type": "integer", "description": "Max rows (default all that fit)"},
            },
            "required": [],
        },
    },
]


def handle_chat_tool(name: str, inp: dict) -> str:
    """Never raises; returns text for the model."""
    inp = inp or {}
    try:
        if name == "etf_screen":
            if not index_desk_enabled():
                return "etf_screen is disabled (index_desk.enabled is false)."
            return ES.etf_screen_text(900, category=inp.get("category") or None, top=inp.get("top") or None)
        if name == "allocate_cash":
            if not index_desk_enabled():
                return "allocate_cash is disabled (index_desk.enabled is false)."
            q = str(inp.get("question") or "").strip()
            if not q:
                return "allocate_cash needs the question."
            r = allocate(q, amount=inp.get("amount_usd"), account=inp.get("account") or None)
            return allocation_text(r)
        return f"unknown tool {name}"
    except Exception as e:
        return f"{name} failed: {e}"


if __name__ == "__main__":      # free dry run: python3 mason_allocator.py "I have $1,000 in Savings ..."
    import sys
    q = " ".join(sys.argv[1:]) or "I have $1,000 in Savings — best ETF for short-term gain?"
    t = time.time()
    r = allocate(q, dry_run=True)
    print(r.get("user", r))
    print(f"\n[dry run {time.time() - t:.1f}s · input ≈ {r.get('input_tokens_est')} tok · sections "
          f"{r.get('section_tokens')} · est ${r.get('est_usd')} · target {r.get('target')} · amount {r.get('amount')} "
          f"· budget {r.get('budget_note')}]")
