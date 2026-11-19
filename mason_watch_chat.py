"""
mason_watch_chat.py — the `watch_tape` chat tool (docs/watchlist/WATCHLIST_DESIGN.md R15).

"Mason, what's HOOD's tape been doing?" → one compact, deterministic answer built from
the watchlist store, mason_watch_signals.tape_stats, today's watch_events, the latest
qwen digest and the latest options snapshot. Read-only. Offered to the chat brain by
mason_app._EXT_TOOL_MODULES only while watchlist.enabled. Never raises.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import mason_db

CHAT_TOOLS = [{
    "name": "watch_tape",
    "description": ("What a watched ticker's live tape has been doing today: last price, % change, "
                    "VWAP, relative volume, the events Mason's watcher recorded (level crosses, "
                    "breakouts, big moves, position P&L steps), the latest qwen tape digest and "
                    "options IV / implied move. Use when Nico asks how a watched name is trading."),
    "input_schema": {"type": "object",
                     "properties": {"ticker": {"type": "string", "description": "e.g. HOOD"}},
                     "required": ["ticker"]},
}]

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,3})?$")
_BADGE = {"nico": "Your watch", "opus": "Mason-watched", "qwen": "Suggested"}


def _fmt(v, spec="{:.2f}"):
    try:
        return spec.format(float(v))
    except (TypeError, ValueError):
        return "n/a"


def _today_rows(ticker: str) -> tuple[list, dict | None]:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn = mason_db.get_connection()
    try:
        ev = [dict(r) for r in conn.execute(
            "SELECT ts, kind, severity, message FROM watch_events WHERE ticker=? AND ts>=? "
            "ORDER BY ts ASC LIMIT 12", (ticker, start)).fetchall()]
        dg = conn.execute("SELECT slot, text, created_date FROM watch_digests WHERE ticker=? "
                          "ORDER BY created_date DESC LIMIT 1", (ticker,)).fetchone()
    finally:
        conn.close()
    return ev, (dict(dg) if dg else None)


def watch_tape_text(ticker: str) -> str:
    try:
        import mason_watchlist as WL
        if not WL.enabled():
            return "The watchlist is turned off (watchlist.enabled=false)."
    except Exception:
        return "The watchlist isn't available."
    t = str(ticker or "").upper().strip().lstrip("$")
    if not _TICKER_RE.match(t):
        return "Please give a valid ticker (e.g. HOOD)."
    item = None
    try:
        item = WL.get_item(t)
    except Exception:
        pass
    role = None
    try:
        role = ((WL.universe() or {}).get(t) or {}).get("role")
    except Exception:
        pass
    if not item and role not in ("position", "infra"):
        return f"{t} is not on the watchlist. Say 'watch {t}' to add it."
    if item:
        head = f"{t} [{_BADGE.get(item.get('origin'), item.get('origin'))} · {item.get('horizon') or '?'}]"
        if item.get("reason"):
            head += f" — why: {item['reason']}"
        lv = item.get("levels") or []
        if lv:
            head += " · levels: " + ", ".join(f"{l.get('side')} {_fmt(l.get('price'))} ({l.get('set_by')})"
                                             for l in lv if isinstance(l, dict))
    else:
        head = f"{t} [{'Position' if role == 'position' else 'Index/sector ETF'}]"
    lines = [head]
    try:
        import mason_watch_signals as SG
        s = SG.tape_stats(t) or {}
        if s.get("no_trades_today"):
            lines.append(f"No trades yet today (prior close {_fmt(s.get('prior_close'))}).")
        else:
            lines.append(f"Last {_fmt(s.get('last'))} ({_fmt(s.get('chg_pct_prev_close'), '{:+.1f}%')} vs prior close)"
                         f" · VWAP {_fmt(s.get('vwap'))} ({'above' if s.get('above_vwap') else 'below'})"
                         f" · rvol {_fmt(s.get('rvol'), '{:.1f}')} · ATR {_fmt(s.get('atr_pct'), '{:.1f}%')}"
                         + (f" · vs SPY {_fmt(s.get('vs_spy_pct'), '{:+.1f}%')}" if s.get("vs_spy_pct") is not None else ""))
    except Exception:
        lines.append("Tape stats unavailable right now.")
    try:
        import mason_watch_tape as TP
        o = TP.latest_option_snap(t)
        if o and o.get("atm_iv") is not None:
            iv = float(o["atm_iv"])
            iv = iv * 100 if iv < 5 else iv
            lines.append(f"Options ({o.get('expiry')}): IV {iv:.0f}, implied move ±{_fmt(o.get('implied_move_pct'), '{:.1f}')}%.")
    except Exception:
        pass
    try:
        ev, dg = _today_rows(t)
        if ev:
            lines.append("Today's events: " + "; ".join(
                f"{datetime.fromtimestamp(e['ts']).strftime('%H:%M')} {e.get('message') or e.get('kind')}" for e in ev))
        else:
            lines.append("No watcher events today.")
        if dg and dg.get("text"):
            lines.append(f"Latest digest ({dg.get('slot')}, {str(dg.get('created_date'))[:16]}): {dg['text']}")
    except Exception:
        pass
    return "\n".join(lines)


def handle_chat_tool(name: str, inp: dict | None) -> str:
    if name != "watch_tape":
        return f"unknown tool {name}"
    try:
        return watch_tape_text((inp or {}).get("ticker"))
    except Exception as e:
        return f"watch_tape failed: {e}"
