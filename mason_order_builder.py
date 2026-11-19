"""mason_order_builder.py — deterministic, model-free order entry for Mason.

WHY THIS EXISTS
---------------
2026-09-10: online testing showed the local model won't reliably *place* an order
either — asked to "log/place a trade" it describes it and defers ("would you like
me to log it now?"), or worse fabricates a "done" with a hallucinated id. The delete
path was made reliable by taking the model out of the loop (see mason_app
_DIRECT_ANSWER). Placing an order is the same class of integrity-critical mutation,
so it gets the same treatment: a deterministic two-step handler.

FLOW (two-step, like delete)
----------------------------
  PREVIEW:  "buy AAPL"  /  "place an order on Apple"  /  "short RKLB 50 @ limit 78"
            -> resolve the ticker, pull a live quote for the default limit, size the
               default qty from a fixed notional, show the full proposed order and a
               self-contained `confirm order …` command. NOTHING is placed.
  CONFIRM:  "confirm order AAPL buy 3 limit 312.50 [stop 300 target 340]"
            -> place the REAL Alpaca paper order deterministically, then log it.

RESOLUTION (Nico, 2026-09-10: "figure out the company, ask if unsure")
    A clean ticker that a live quote validates is used as-is. Otherwise a symbol
    search (FMP) resolves a name/ticker to candidates: exactly one US-equity match
    proceeds; several -> a clarifying question listing them; none -> ask for the
    symbol. The search is a plain data lookup — the model is never in this path.

POLICY
    Limit orders only (market is rejected upstream by mason_execution). A stop AND
    target together make a broker-side bracket. Every path is two-step: the first
    message only ever previews; nothing places without an explicit `confirm order`.

This module is PURE: all I/O (quote, account, symbol search, place, log) is injected,
so the whole thing is unit-tested with no network and no orders placed. mason_app
wires the real functions in.
"""

import re

# Default per-order notional when Nico names a ticker without a share count
# (Nico 2026-09-10: "fixed notional"). Overridable from mason.config.json
# order_builder.default_notional; mason_app passes the resolved value in.
DEFAULT_NOTIONAL = 1000.0

# Verbs -> side. "long"/"buy" open long; "short"/"sell" open short/close.
# Kept deliberately tight (no "grab"/"dump" slang) to limit false positives on the
# bare-verb branch; a false preview is harmless (nothing places without an explicit
# `confirm order`) but still worth avoiding.
_BUY_WORDS  = r"buy|long|go\s+long|purchase"
_SELL_WORDS = r"sell|short|go\s+short"

# Idioms that start like an order but aren't one ("sell me on NVDA" = convince me).
_IDIOM_RE = re.compile(r"^(?:me|us|him|her|them|yourself|myself)\s+(?:on|about)\b", re.IGNORECASE)

# Leading trigger: an order verb, tolerating "place an order on/for", "put in an
# order for", etc. Everything AFTER the verb is parsed field-by-field below, so the
# trigger only has to recognise intent + capture the remainder.
_ORDER_TRIGGER_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)*"
    r"(?:(?:place|put\s+in|open|enter|set\s+up|submit)\s+"
    r"(?:a|an)?\s*(?P<v1>" + _BUY_WORDS + r"|" + _SELL_WORDS + r")?\s*"
    r"(?:order|position|trade)\s+(?:on|for|in)?\s+"
    r"|(?P<v2>" + _BUY_WORDS + r"|" + _SELL_WORDS + r")\s+)"
    r"(?P<rest>.+)$",
    re.IGNORECASE,
)

# Field extractors applied to the remainder after the verb.
_QTY_RE    = re.compile(r"\b(?P<qty>\d+(?:\.\d+)?)\s*(?:shares?|shrs?|sh)\b", re.IGNORECASE)
_QTY_BARE  = re.compile(r"(?<![\w.])(?P<qty>\d{1,7})(?![\w.%$])")
_LIMIT_RE  = re.compile(r"(?:@|\bat\b|\blimit\b|\blmt\b)\s*\$?(?P<limit>\d+(?:\.\d+)?)", re.IGNORECASE)
_STOP_RE   = re.compile(r"\bstop(?:\s*loss)?\b\s*\$?(?P<stop>\d+(?:\.\d+)?)", re.IGNORECASE)
_TARGET_RE = re.compile(r"\b(?:target|tgt|tp|take\s*profit)\b\s*\$?(?P<target>\d+(?:\.\d+)?)", re.IGNORECASE)

# Self-contained confirm command. Strict + fully explicit so it can never be
# satisfied by a fuzzy phrase — the id/side/qty/limit are all present literally.
_CONFIRM_RE = re.compile(
    r"^(?:yes[,\s]+|yep[,\s]+|ok(?:ay)?[,\s]+)?confirm\s+order\s+"
    r"(?P<sym>[A-Za-z][A-Za-z.\-]{0,9})\s+"
    r"(?P<side>buy|sell|long|short)\s+"
    r"(?P<qty>\d+(?:\.\d+)?)\s+"
    r"(?:@|limit|at)\s*\$?(?P<limit>\d+(?:\.\d+)?)"
    r"(?:\s+stop\s*\$?(?P<stop>\d+(?:\.\d+)?))?"
    r"(?:\s+(?:target|tp)\s*\$?(?P<target>\d+(?:\.\d+)?))?"
    r"(?:\s+(?P<tif>day|gtc))?[\s.!]*$",
    re.IGNORECASE,
)

_STOPWORDS = {"the", "a", "an", "some", "stock", "shares", "share", "of", "on",
              "for", "in", "please", "me", "my", "order", "position", "trade",
              "at", "limit", "stop", "target", "tp", "day", "gtc", "and"}


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|company|co|ltd|plc|holdings?|group|"
    r"the|technologies|technology|international|intl)\b", re.IGNORECASE)


def _norm_company(s):
    """Company name normalised for matching: lowercased, punctuation and common
    corporate suffixes (Inc/Corp/Holdings/…) stripped. 'Apple Inc.' -> 'apple'."""
    s = _COMPANY_SUFFIX_RE.sub(" ", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _side_from(word):
    if not word:
        return None
    return "sell" if re.match(_SELL_WORDS, word.strip(), re.IGNORECASE) else "buy"


def parse_order_request(raw):
    """Parse a natural order request into a dict, or None if it isn't one.

    Returns {side, name, qty|None, limit|None, stop|None, target|None} where `name`
    is the raw ticker-or-company string still to be resolved. Deliberately returns
    None (fall through to the model) rather than guessing when there's no verb."""
    m = _ORDER_TRIGGER_RE.match((raw or "").strip())
    if not m:
        return None
    side = _side_from(m.group("v1") or m.group("v2"))
    if side is None:
        # "place an order on AAPL" with no explicit side — default to buy (opening).
        side = "buy"
    rest = m.group("rest").strip()
    # "sell me on X" / "buy me on X" is persuasion, not an order.
    if _IDIOM_RE.match(rest):
        return None

    fields = {"side": side, "qty": None, "limit": None, "stop": None, "target": None}
    # Pull labelled fields first, removing each match from `rest` so the leftover is
    # the name/ticker only.
    def _take(rx, key, cast=float):
        nonlocal rest
        mm = rx.search(rest)
        if mm:
            fields[key] = cast(mm.group(key))
            rest = (rest[:mm.start()] + " " + rest[mm.end():]).strip()

    _take(_LIMIT_RE, "limit")
    _take(_STOP_RE, "stop")
    _take(_TARGET_RE, "target")
    _take(_QTY_RE, "qty")            # "50 shares"
    if fields["qty"] is None:        # a bare integer left over is the qty
        mm = _QTY_BARE.search(rest)
        if mm:
            fields["qty"] = float(mm.group("qty"))
            rest = (rest[:mm.start()] + " " + rest[mm.end():]).strip()

    # Whatever survives, minus stopwords and stray punctuation (e.g. a leftover
    # "@" from "@ limit 78"), is the ticker/company name.
    name = " ".join(w.strip(".,@$") for w in re.split(r"\s+", rest)
                    if w.lower() not in _STOPWORDS and re.search(r"[A-Za-z0-9]", w)).strip()
    if not name:
        return None
    fields["name"] = name
    if fields["qty"] is not None:
        fields["qty"] = int(fields["qty"]) if fields["qty"] == int(fields["qty"]) else fields["qty"]
    return fields


def resolve_symbol(name, *, quote_fn, search_fn):
    """Resolve a ticker-or-company string to a decision dict:
        {"status": "ok", "symbol": "AAPL", "company": "Apple Inc"}
        {"status": "ambiguous", "candidates": [(sym, company), ...]}
        {"status": "none"}

    A token that already looks like a ticker AND returns a live quote is accepted
    immediately (fast path, no search). Otherwise search_fn(name) supplies US-equity
    candidates. Both I/O calls are injected; this function has no network of its own."""
    tok = (name or "").strip()
    looks_ticker = bool(re.fullmatch(r"[A-Za-z]{1,5}", tok))
    if looks_ticker:
        sym = tok.upper()
        q = quote_fn(sym)
        if q is not None:
            return {"status": "ok", "symbol": sym, "company": (q.get("name") if isinstance(q, dict) else None) or sym}
        # looked like a ticker but no quote — fall through to search (e.g. "APPL" typo)

    candidates = search_fn(tok) or []
    # normalise to (symbol, company); keep order (search_fn already ranks US equities)
    norm = []
    seen = set()
    for c in candidates:
        sym = (c.get("symbol") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            norm.append((sym, c.get("name") or sym))
    if not norm:
        return {"status": "none"}
    # exact ticker hit inside the results -> take it
    if looks_ticker:
        for sym, comp in norm:
            if sym == tok.upper():
                return {"status": "ok", "symbol": sym, "company": comp}
    if len(norm) == 1:
        return {"status": "ok", "symbol": norm[0][0], "company": norm[0][1]}
    # Prefer a single company whose NAME matches the query once suffixes like
    # "Inc/Corp/Holdings" are stripped — so "Apple" -> AAPL even though "Appleseed
    # Fund" and "Apple Hospitality REIT" also match the substring. Only when EXACTLY
    # one candidate matches this way; otherwise it stays ambiguous and we ask.
    nq = _norm_company(tok)
    if nq:
        exact = [(s, c) for s, c in norm if _norm_company(c) == nq]
        if len(exact) == 1:
            return {"status": "ok", "symbol": exact[0][0], "company": exact[0][1]}
    return {"status": "ambiguous", "candidates": norm[:5]}


def default_qty(limit_price, notional):
    """Shares for a fixed notional, floored, min 1."""
    if not limit_price or limit_price <= 0:
        return None
    return max(1, int(notional // limit_price))


def build_preview(parsed, *, quote_fn, search_fn, notional=DEFAULT_NOTIONAL):
    """Turn a parsed request into the preview string (never places). Returns a str,
    or None to fall through to the model. Handles resolution, the clarifying-question
    branches, the live-quote default limit, and the fixed-notional default qty."""
    if not parsed:
        return None
    r = resolve_symbol(parsed["name"], quote_fn=quote_fn, search_fn=search_fn)
    if r["status"] == "none":
        return (f"I couldn't find a ticker for “{parsed['name']}”. "
                f"Give me the symbol, e.g. `{parsed['side']} AAPL`.")
    if r["status"] == "ambiguous":
        opts = ", ".join(f"{comp} ({sym})" for sym, comp in r["candidates"])
        first = r["candidates"][0][0]
        return (f"Which one did you mean — {opts}? "
                f"Reply with the ticker, e.g. `{parsed['side']} {first}`.")

    sym, company = r["symbol"], r["company"]
    limit = parsed["limit"]
    if limit is None:
        q = quote_fn(sym)
        price = (q.get("price") if isinstance(q, dict) else None)
        if not price:
            return (f"I found {company} ({sym}) but couldn't get a live price to set a "
                    f"limit. Tell me the limit, e.g. `{parsed['side']} {sym} @ <price>`.")
        limit = round(float(price), 2)
    qty = parsed["qty"] or default_qty(limit, notional)
    if not qty:
        return f"Tell me how many shares of {sym}, e.g. `{parsed['side']} {sym} 10 @ {limit}`."

    side = parsed["side"]
    stop, target = parsed["stop"], parsed["target"]
    bracket = (stop is not None and target is not None)
    est = qty * limit
    lines = [
        f"Proposed paper order — {company} ({sym}):",
        f"  {side.upper()} {qty} @ limit ${limit:g}  (day)  ≈ ${est:,.0f}",
    ]
    if bracket:
        lines.append(f"  bracket: stop ${stop:g} / target ${target:g}")
    elif stop is not None or target is not None:
        lines.append(f"  (stop AND target are both needed for a bracket — "
                     f"{'target' if stop is not None else 'stop'} missing, so this rests as a plain limit)")
    confirm = f"confirm order {sym} {side} {qty} limit {limit:g}"
    if bracket:
        confirm += f" stop {stop:g} target {target:g}"
    lines += [
        "Nothing placed yet. To place it, send:",
        f"  {confirm}",
        "Edit any number in that line before sending, or add `stop <s> target <t>` to bracket it.",
    ]
    return "\n".join(lines)


def parse_confirm(raw):
    """Parse a `confirm order …` line into the exact order kwargs, or None."""
    m = _CONFIRM_RE.match((raw or "").strip())
    if not m:
        return None
    side = "sell" if m.group("side").lower() in ("sell", "short") else "buy"
    out = {
        "symbol": m.group("sym").upper(),
        "side": side,
        "qty": float(m.group("qty")),
        "limit": float(m.group("limit")),
        "stop": float(m.group("stop")) if m.group("stop") else None,
        "target": float(m.group("target")) if m.group("target") else None,
        "tif": (m.group("tif") or "day").lower(),
    }
    if out["qty"] == int(out["qty"]):
        out["qty"] = int(out["qty"])
    return out
