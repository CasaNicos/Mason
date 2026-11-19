"""
Mason — Alpaca Paper Trading Integration
=========================================
Connects to your Alpaca paper trading account to:
  - Submit Mason's play recommendations as paper orders
  - Track fills and P&L automatically (no manual CSV needed)
  - Pull account balance and positions
  - Close positions when Mason says exit

SETUP (one-time):
  1. Create free account at https://alpaca.markets
  2. Go to Paper Trading → API Keys → Generate
  3. Add your keys to mason.config.json:
       "paper_trading": {
         "enabled": true,
         "api_key": "YOUR_KEY_HERE",
         "api_secret": "YOUR_SECRET_HERE"
       }

IMPORTANT: This is PAPER trading only — no real money.
Mason uses this to build a verifiable track record before
connecting to a real brokerage. Mason cannot execute real trades.

Install: pip install alpaca-py --break-system-packages
"""

import json
import requests
from datetime import datetime, date
from pathlib import Path

BASE   = Path(__file__).parent
CONFIG = json.loads((BASE / "mason.config.json").read_text())
PT     = CONFIG.get("paper_trading", {}) or {}

# B1b: env APCA_* > private secrets file > paper_trading (byte-exact legacy); never raises.
import mason_secrets as _S
ALPACA_KEY, ALPACA_SECRET = _S.alpaca_keys(cfg=CONFIG)
# Strip trailing /v2 if present — we append it per-request
_raw_url      = PT.get("base_url", "https://paper-api.alpaca.markets/v2")
ALPACA_URL    = _raw_url.rstrip("/").removesuffix("/v2")
ENABLED       = PT.get("enabled", False)


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }


def _check_enabled():
    if not ENABLED:
        raise RuntimeError(
            "Alpaca paper trading is disabled. "
            "Add your API keys to mason.config.json and set enabled=true."
        )
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError(
            "Alpaca API keys not set. Get them at https://alpaca.markets "
            "(Paper Trading → API Keys) and add to mason.config.json."
        )


# ═══════════════════════════════════════════════════════════════════
# ACCOUNT
# ═══════════════════════════════════════════════════════════════════

def get_account() -> dict:
    """Returns paper account balance, buying power, equity."""
    _check_enabled()
    r = requests.get(f"{ALPACA_URL}/v2/account", headers=_headers(), timeout=10)
    a = r.json()
    return {
        "equity":        float(a.get("equity", 0)),
        "cash":          float(a.get("cash", 0)),
        "buying_power":  float(a.get("buying_power", 0)),
        "portfolio_value": float(a.get("portfolio_value", 0)),
        "pl_today":      float(a.get("equity", 0)) - float(a.get("last_equity", 0)),
    }


def get_positions() -> list[dict]:
    """Current open paper positions."""
    _check_enabled()
    r = requests.get(f"{ALPACA_URL}/v2/positions", headers=_headers(), timeout=10)
    positions = []
    for p in r.json():
        positions.append({
            "ticker":      p["symbol"],
            "qty":         float(p["qty"]),
            "side":        p["side"],
            "avg_entry":   float(p["avg_entry_price"]),
            "market_value": float(p["market_value"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": float(p["unrealized_plpc"]) * 100,
            "current_price": float(p["current_price"]),
        })
    return positions


# ═══════════════════════════════════════════════════════════════════
# ORDERS — for paper executing Mason's plays
# ═══════════════════════════════════════════════════════════════════

def submit_paper_order(
    ticker: str,
    qty: float,
    side: str,          # "buy" | "sell"
    order_type: str     = "market",  # "market" | "limit" | "stop" | "stop_limit"
    limit_price: float  = None,
    stop_price: float   = None,
    time_in_force: str  = "day",     # "day" | "gtc" | "ioc" | "fok"
    mason_play_id: int  = None,      # links to daily_plays.csv entry
    notes: str          = "",
) -> dict:
    """
    Submit a paper order for a Mason play.
    This is how Mason 'executes' its recommendations in paper trading.

    IMPORTANT: Mason cannot submit real orders. This is paper only.
    Nico must manually replicate real trades in his actual brokerage.
    """
    _check_enabled()

    payload = {
        "symbol":        ticker,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": time_in_force,
    }
    if limit_price:
        payload["limit_price"] = str(limit_price)
    if stop_price:
        payload["stop_price"] = str(stop_price)

    r    = requests.post(f"{ALPACA_URL}/v2/orders", headers=_headers(),
                         json=payload, timeout=10)
    data = r.json()

    result = {
        "order_id":      data.get("id"),
        "status":        data.get("status"),
        "ticker":        ticker,
        "qty":           qty,
        "side":          side,
        "type":          order_type,
        "submitted_at":  data.get("submitted_at"),
        "mason_play_id": mason_play_id,
        "notes":         notes,
        "error":         data.get("message") if r.status_code != 200 else None,
    }

    # Log to alpaca_orders.csv
    _log_order(result)
    return result


def close_paper_position(ticker: str, notes: str = "") -> dict:
    """Close an entire paper position — for when Mason says 'exit this'."""
    _check_enabled()
    r    = requests.delete(f"{ALPACA_URL}/v2/positions/{ticker}",
                           headers=_headers(), timeout=10)
    data = r.json()
    return {
        "ticker": ticker,
        "status": data.get("status", "closed"),
        "closed_at": datetime.now().isoformat(),
        "notes": notes,
    }


def get_orders(status: str = "all", limit: int = 20) -> list[dict]:
    """Recent paper orders — for reviewing fills against Mason's plays."""
    _check_enabled()
    r = requests.get(f"{ALPACA_URL}/v2/orders",
                     headers=_headers(),
                     params={"status": status, "limit": limit},
                     timeout=10)
    return r.json()


# ═══════════════════════════════════════════════════════════════════
# ORDER LOG
# ═══════════════════════════════════════════════════════════════════

ORDER_LOG = BASE / "alpaca_orders.csv"
ORDER_FIELDS = [
    "order_id", "submitted_at", "ticker", "qty", "side", "type",
    "status", "mason_play_id", "notes", "error"
]

def _log_order(order: dict):
    """Append order to alpaca_orders.csv."""
    import csv
    file_exists = ORDER_LOG.exists() and ORDER_LOG.stat().st_size > 0
    with open(ORDER_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ORDER_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(order)


# ═══════════════════════════════════════════════════════════════════
# PAPER PORTFOLIO SUMMARY — for Mason's brief
# ═══════════════════════════════════════════════════════════════════

def paper_portfolio_summary() -> str:
    """
    Formatted paper trading account summary for the daily brief.
    Mason includes this if Alpaca is enabled.
    """
    try:
        acct      = get_account()
        positions = get_positions()

        lines = [
            f"\n  📄 PAPER ACCOUNT (Alpaca)",
            f"  Portfolio value: ${acct['portfolio_value']:,.2f}  |  "
            f"Cash: ${acct['cash']:,.2f}  |  "
            f"Today's P&L: {'-' if acct['pl_today']<0 else '+'}${abs(acct['pl_today']):,.2f}",
        ]

        if positions:
            lines.append("")
            for p in positions:
                sign = "+" if p["unrealized_pl"] >= 0 else ""
                lines.append(
                    f"  {p['ticker']:<6} {p['qty']} shares @ ${p['avg_entry']:.2f}  |  "
                    f"Now: ${p['current_price']:.2f}  |  "
                    f"P&L: {'-' if p['unrealized_pl']<0 else '+'}${abs(p['unrealized_pl']):.2f} ({sign}{p['unrealized_plpc']:.1f}%)"
                )
        else:
            lines.append("  No open paper positions.")

        return "\n".join(lines)

    except RuntimeError as e:
        return f"\n  📄 PAPER ACCOUNT: {e}"
    except Exception as e:
        return f"\n  📄 PAPER ACCOUNT: Error — {e}"


# ═══════════════════════════════════════════════════════════════════
# SETUP HELPER
# ═══════════════════════════════════════════════════════════════════

def setup_instructions():
    print("""
╔══════════════════════════════════════════════════════════════╗
║          MASON — ALPACA PAPER TRADING SETUP                  ║
╚══════════════════════════════════════════════════════════════╝

1. Go to https://alpaca.markets and create a free account

2. In the dashboard, click "Paper Trading" in the left nav

3. Go to Overview → API Keys → Generate New Key
   Copy the API Key and Secret Key

4. Open: <your Mason folder>/mason.config.json
   Update the paper_trading section:

   "paper_trading": {
     "provider": "alpaca",
     "enabled": true,
     "api_key": "YOUR_API_KEY_HERE",
     "api_secret": "YOUR_SECRET_HERE",
     "base_url": "https://paper-api.alpaca.markets"
   }

5. Install alpaca-py (optional — Mason uses REST directly):
   pip install alpaca-py --break-system-packages

6. Test: python alpaca_integration.py --test

Once connected, Mason will:
  • Paper-execute its play recommendations automatically
  • Track fills and P&L in real-time
  • Build a verifiable 30-90 day track record
  • Use that record to earn trust for eventual real trading
""")


if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        setup_instructions()
    elif "--test" in sys.argv:
        try:
            acct = get_account()
            print(f"✅ Connected to Alpaca paper account")
            print(f"   Portfolio value: ${acct['portfolio_value']:,.2f}")
            print(f"   Buying power:    ${acct['buying_power']:,.2f}")
        except RuntimeError as e:
            print(f"❌ {e}")
    elif "--positions" in sys.argv:
        print(paper_portfolio_summary())
    else:
        setup_instructions()
