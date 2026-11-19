"""
mason_institutional_flow.py — EDGAR-native institutional 13F flow tracker.

Why this exists: the `institutional-flow-tracker` skill is FMP-based
(institutional-ownership/symbol-positions-summary + extract-analytics/holder).
Inside a Cowork chat session, Claude's sandbox proxy is default-deny and
financialmodelingprep.com (along with almost every other data vendor) is
unreachable from bash — only SEC EDGAR is reachable, and only through the
web_fetch tool, not through requests.get() in-sandbox. See memory
`project_mason_sandbox_proxy_edgar_swap` for the full diagnosis.

This module sources the same underlying signal (13F institutional holdings)
directly from SEC EDGAR instead of FMP. It is a NEW capability file, not an
edit to the read-only `institutional-flow-tracker` skill cache (which cannot
be modified in a Cowork session — see skill system rules).

Reuses mason_edgar.py's proven CIK-resolution / SEC-compliant HTTP helpers
rather than duplicating them: _get, _ua, resolve_cik, _fetch_submissions,
_select_filings, _doc_url. Do not copy that logic here — import it.

Two lookups:

  get_institution_holdings(name, quarters=2) -> dict
      Resolve an institutional MANAGER (e.g. "Berkshire Hathaway", "ARK
      Investment") to its CIK, pull its N most recent 13F-HR filings, parse
      each INFOTABLE, and diff quarter-over-quarter (new / sold-out /
      increased / decreased, top holdings by value). This is the feature the
      original FMP skill's track_institution_portfolio.py explicitly stubbed
      out as "not yet implemented" — FMP organizes 13F data by stock, not by
      institution, so it couldn't do this either. EDGAR can, natively.

  get_stock_institutional_flow(ticker, quarters=2, max_filers=25) -> dict
      Reverse lookup: which managers hold TICKER and how has that changed.
      Uses EDGAR full-text search (forms=13F-HR) for the issuer name, then
      fetches each candidate filer's INFOTABLE to confirm/extract the exact
      row. KNOWN LIMITATION (always surfaced in the output as coverage_note):
      EDGAR full-text search is not a guaranteed-exhaustive census of every
      13F filer the way FMP's server-side reconciled aggregate is. Treat
      institution counts here as a floor from the sampled filers, not an
      exact count. Good for "is anyone notable buying/selling this," not for
      precise % institutional ownership.

format_report(data) -> str
      Markdown report mirroring institutional-flow-tracker's own output shape
      (exec summary / ownership trend / top holders / new-sold-out /
      concentration / data sources + timestamp + coverage caveats).

Public API is designed to NEVER raise — every function returns a dict with
an "error" key (or a diagnostic string from format_report) on failure, same
contract as mason_edgar.py.
"""

import re
import time
import urllib.parse as _u
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mason_edgar import _get, resolve_cik, _fetch_submissions, _select_filings, _doc_url

BASE = Path(__file__).parent

_FTS_URL = "https://efts.sec.gov/LATEST/search-index"

# common issuer-name suffixes to strip when fuzzy-matching a resolved company
# title (e.g. "Apple Inc.") against 13F INFOTABLE issuer names (e.g. "APPLE INC")
_SUFFIXES = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|LLC|LP|PLC|"
    r"HOLDINGS?|GROUP|SA|NV|AG)\b\.?",
    re.IGNORECASE,
)


def _norm_name(name: str) -> str:
    """Normalize a company name for fuzzy matching (uppercase, no suffix/punct)."""
    s = _SUFFIXES.sub(" ", name or "")
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


# ── INFOTABLE XML parsing ───────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip XML namespace, return local tag name. '{ns}infoTable' -> 'infoTable'."""
    return tag.split("}")[-1] if "}" in tag else tag


def _text(elem, name, default=""):
    if elem is None:
        return default
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return default


def parse_infotable_xml(xml_text: str) -> list:
    """Parse a 13F INFOTABLE XML document into a list of holding rows.

    Pure + unit-testable (no network). Returns [] on malformed input rather
    than raising. Each row:
        {issuer, class, cusip, value_thousands, shares, share_type,
         voting_sole, voting_shared, voting_none}

    Note on units: the nominal SEC 13F instruction says `value` is reported in
    thousands of dollars. Empirically that is NOT what current filers submit —
    verified against Berkshire's 2026-05-15 13F-HR: its Coca-Cola rows sum to
    exactly 400,000,000 shares (a famous, unchanged-for-decades position) at a
    raw `value` total of 30,420,000,000, i.e. $76.05/share at whole-dollar
    scale. Treating that as thousands would price KO at $76,050/share, which
    is wrong. So `value_thousands` here is a field-name legacy of the spec;
    treat it as whole dollars (see `_fmt_usd` below, which does NOT ×1000).
    If a future filer's numbers look 1000x off, re-check this assumption.
    """
    if not xml_text or "<" not in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    rows = []
    for elem in root.iter():
        if _local(elem.tag) != "infoTable":
            continue
        shrs_elem = None
        voting_elem = None
        for child in elem:
            if _local(child.tag) == "shrsOrPrnAmt":
                shrs_elem = child
            elif _local(child.tag) == "votingAuthority":
                voting_elem = child

        try:
            value = int(_text(elem, "value", "0") or "0")
        except ValueError:
            value = 0
        try:
            shares = int(_text(shrs_elem, "sshPrnamt", "0") or "0")
        except ValueError:
            shares = 0

        rows.append({
            "issuer": _text(elem, "nameOfIssuer"),
            "class": _text(elem, "titleOfClass"),
            "cusip": _text(elem, "cusip"),
            "value_thousands": value,
            "shares": shares,
            "share_type": _text(shrs_elem, "sshPrnamtType", "SH"),
            "voting_sole": _text(voting_elem, "Sole", "0"),
            "voting_shared": _text(voting_elem, "Shared", "0"),
            "voting_none": _text(voting_elem, "None", "0"),
        })
    return rows


def aggregate_by_cusip(rows: list) -> dict:
    """Collapse multiple INFOTABLE rows (sub-account splits) into one position
    per CUSIP: {cusip: {issuer, class, value_thousands, shares, row_count}}.
    """
    out = {}
    for r in rows:
        cusip = r["cusip"] or f"NOCUSIP:{_norm_name(r['issuer'])}"
        agg = out.setdefault(cusip, {
            "issuer": r["issuer"], "class": r["class"], "cusip": r["cusip"],
            "value_thousands": 0, "shares": 0, "row_count": 0,
        })
        agg["value_thousands"] += r["value_thousands"]
        agg["shares"] += r["shares"]
        agg["row_count"] += 1
    return out


# ── filing directory helpers ─────────────────────────────────────────────────

def _filing_dir_url(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"


def _find_infotable_filename(cik: int, accession: str):
    """Fetch the filing's directory listing (index.json) and return the
    INFOTABLE xml filename (the .xml file that isn't primary_doc.xml).
    None on failure or if no INFOTABLE is present (e.g. malformed filer).
    """
    url = f"{_filing_dir_url(cik, accession)}/index.json"
    r = _get(url)
    if r is None:
        return None
    try:
        items = r.json().get("directory", {}).get("item", [])
    except Exception:
        return None
    candidates = [
        it["name"] for it in items
        if it.get("name", "").lower().endswith(".xml") and it["name"].lower() != "primary_doc.xml"
    ]
    return candidates[0] if candidates else None


def _fetch_infotable(cik: int, accession: str):
    """Fetch + parse a filing's INFOTABLE. Returns (rows, source_url) or ([], None)."""
    fname = _find_infotable_filename(cik, accession)
    if not fname:
        return [], None
    url = f"{_filing_dir_url(cik, accession)}/{fname}"
    r = _get(url, timeout=25)
    if r is None:
        return [], url
    return parse_infotable_xml(r.text), url


# ── quarter-over-quarter diff ────────────────────────────────────────────────

def _diff_positions(current: dict, prior: dict) -> dict:
    """Diff two {cusip: {..., shares, value_thousands}} maps."""
    cur_keys, prior_keys = set(current), set(prior)
    new_positions = sorted(
        (current[k] for k in cur_keys - prior_keys),
        key=lambda x: -x["value_thousands"],
    )
    sold_out = sorted(
        (prior[k] for k in prior_keys - cur_keys),
        key=lambda x: -x["value_thousands"],
    )
    increased, decreased, unchanged = [], [], []
    for k in cur_keys & prior_keys:
        c, p = current[k], prior[k]
        if p["shares"] == 0:
            continue
        pct = (c["shares"] - p["shares"]) / p["shares"] * 100.0
        row = {**c, "prior_shares": p["shares"], "change_pct": round(pct, 1)}
        if pct > 1.0:
            increased.append(row)
        elif pct < -1.0:
            decreased.append(row)
        else:
            unchanged.append(row)
    increased.sort(key=lambda x: -x["change_pct"])
    decreased.sort(key=lambda x: x["change_pct"])
    return {
        "new_positions": new_positions, "sold_out": sold_out,
        "increased": increased, "decreased": decreased, "unchanged": unchanged,
    }


# ── public: single-institution portfolio (the FMP skill couldn't do this) ──

def get_institution_holdings(name: str, quarters: int = 2) -> dict:
    """Pull an institutional manager's last N 13F-HR filings from EDGAR and
    diff them quarter-over-quarter. NEVER raises; returns {"error": ...} on
    failure.
    """
    cik, title = resolve_cik(name)
    if cik is None:
        return {"error": f"could not resolve '{name}' to a CIK: {title}"}

    subs = _fetch_submissions(cik)
    if not subs:
        return {"error": f"resolved '{name}' to CIK {cik} ({title}) but "
                          f"couldn't fetch its filing history (network/rate-limit)."}

    filings = _select_filings(subs, forms=["13F-HR"], limit=quarters)
    if not filings:
        return {"error": f"{title} (CIK {cik}) has no 13F-HR filings on record."}

    quarters_data = []
    for f in filings:
        rows = _fetch_infotable(cik, f["accession"])[0]
        if not rows:
            continue
        quarters_data.append({
            "date": f["date"], "accession": f["accession"],
            "positions": aggregate_by_cusip(rows),
        })
        time.sleep(0.1)

    if not quarters_data:
        return {"error": f"{title} (CIK {cik}): found {len(filings)} 13F-HR filing(s) "
                          f"but none had a parseable INFOTABLE."}

    latest = quarters_data[0]
    top_holdings = sorted(latest["positions"].values(), key=lambda x: -x["value_thousands"])[:20]
    total_value = sum(p["value_thousands"] for p in latest["positions"].values())

    diff = None
    if len(quarters_data) >= 2:
        diff = _diff_positions(latest["positions"], quarters_data[1]["positions"])

    return {
        "kind": "institution",
        "institution": title, "cik": cik,
        "as_of": latest["date"], "prior_as_of": quarters_data[1]["date"] if len(quarters_data) >= 2 else None,
        "total_positions": len(latest["positions"]),
        "total_value_thousands": total_value,
        "top_holdings": top_holdings,
        "diff": diff,
        "quarters_fetched": len(quarters_data),
        "source": "SEC EDGAR 13F-HR (direct)",
    }


# ── public: reverse lookup — who holds this ticker ──────────────────────────

def _fts_search(query: str, start_dt: str, end_dt: str, frm: int = 0) -> dict:
    url = (f"{_FTS_URL}?q=%22{_u.quote(query)}%22&forms=13F-HR"
           f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}&from={frm}")
    r = _get(url, timeout=20)
    if r is None:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def _quarter_bounds(quarters_back: int):
    """Approximate [start,end) filing-date windows for the last N 13F quarters
    (13Fs are due 45 days after quarter end: mid-Feb/May/Aug/Nov)."""
    today = datetime.now(timezone.utc).date()
    # walk back in ~91-day steps from today, wide enough to catch each deadline
    bounds = []
    end = today
    for _ in range(quarters_back):
        start = end - timedelta(days=100)
        bounds.append((start.isoformat(), end.isoformat()))
        end = start
    return bounds


def get_stock_institutional_flow(ticker: str, quarters: int = 2, max_filers: int = 25) -> dict:
    """Reverse lookup: which 13F filers hold `ticker`, and how has that
    changed quarter-over-quarter. Sourced from EDGAR full-text search + direct
    INFOTABLE fetches — NOT a guaranteed-exhaustive census (see coverage_note
    in the returned dict). NEVER raises.
    """
    cik, title = resolve_cik(ticker)
    if cik is None:
        return {"error": f"could not resolve '{ticker}': {title}"}
    issuer_guess = _norm_name(title)

    windows = _quarter_bounds(quarters)
    quarters_data = []
    for start_dt, end_dt in windows:
        hits_seen = 0
        holders = {}
        frm = 0
        while hits_seen < max_filers:
            page = _fts_search(title, start_dt, end_dt, frm=frm)
            hits = page.get("hits", {}).get("hits", [])
            if not hits:
                break
            for h in hits:
                src = h.get("_source", {})
                filer_cik = src.get("cik") or ""
                if isinstance(filer_cik, list):
                    filer_cik = filer_cik[0] if filer_cik else ""
                acc = (h.get("_id", "") or "").split(":")[0]
                if not filer_cik or not acc:
                    continue
                filer_cik_int = int(str(filer_cik).lstrip("0") or "0")
                rows, _src_url = _fetch_infotable(filer_cik_int, acc)
                match = next(
                    (r for r in rows if _norm_name(r["issuer"]) == issuer_guess
                     or issuer_guess in _norm_name(r["issuer"])
                     or _norm_name(r["issuer"]) in issuer_guess),
                    None,
                )
                hits_seen += 1
                if match:
                    filer_name = ", ".join(src.get("display_names", [])) or f"CIK {filer_cik_int}"
                    agg = aggregate_by_cusip([r for r in rows if r is match or
                                              (_norm_name(r["issuer"]) == _norm_name(match["issuer"]))])
                    pos = next(iter(agg.values()))
                    holders[filer_name] = {
                        "filer_cik": filer_cik_int, "value_thousands": pos["value_thousands"],
                        "shares": pos["shares"], "issuer_as_filed": match["issuer"],
                    }
                if hits_seen >= max_filers:
                    break
            frm += len(hits)
            if len(hits) < 10:
                break
        quarters_data.append({"window": (start_dt, end_dt), "holders": holders})

    latest = quarters_data[0]["holders"] if quarters_data else {}
    prior = quarters_data[1]["holders"] if len(quarters_data) >= 2 else {}

    diff = None
    if len(quarters_data) >= 2:
        # diff keyed by filer name since we're comparing holder rosters, not cusips
        cur_keys, prior_keys = set(latest), set(prior)
        new_holders = sorted(
            ({**latest[k], "name": k} for k in cur_keys - prior_keys), key=lambda x: -x["value_thousands"]
        )
        exited = sorted(
            ({**prior[k], "name": k} for k in prior_keys - cur_keys), key=lambda x: -x["value_thousands"]
        )
        changed = []
        for k in cur_keys & prior_keys:
            c, p = latest[k], prior[k]
            if p["shares"] == 0:
                continue
            pct = round((c["shares"] - p["shares"]) / p["shares"] * 100.0, 1)
            changed.append({"name": k, **c, "prior_shares": p["shares"], "change_pct": pct})
        changed.sort(key=lambda x: -x["change_pct"])
        diff = {"new_holders": new_holders, "exited": exited, "changed": changed}

    top_holders = sorted(
        ({"name": n, **v} for n, v in latest.items()), key=lambda x: -x["value_thousands"]
    )[:20]

    return {
        "kind": "stock",
        "ticker": ticker.upper(), "resolved_title": title, "issuer_cik": cik,
        "quarters_sampled": [w for w, _ in [(q["window"], None) for q in quarters_data]],
        "institutions_found": len(latest),
        "top_holders": top_holders,
        "diff": diff,
        "coverage_note": (
            "EDGAR full-text search sampled up to {} filers per quarter and confirmed each "
            "via its own INFOTABLE. This is NOT a reconciled, exhaustive census the way FMP's "
            "aggregate endpoint was -- treat institution counts as a floor, and prioritize the "
            "named top holders (which are individually verified) over the raw count."
        ).format(max_filers),
        "source": "SEC EDGAR full-text search + 13F-HR INFOTABLE (direct)",
    }


# ── markdown report ──────────────────────────────────────────────────────────

def _fmt_usd(v: int) -> str:
    """Format a raw INFOTABLE `value` as USD. NOT multiplied by 1000 — see the
    units note in parse_infotable_xml(); current filers report whole dollars."""
    dollars = v
    if abs(dollars) >= 1_000_000_000:
        return f"${dollars/1_000_000_000:.2f}B"
    if abs(dollars) >= 1_000_000:
        return f"${dollars/1_000_000:.1f}M"
    return f"${dollars:,.0f}"


def format_report(data: dict) -> str:
    """Render get_institution_holdings() or get_stock_institutional_flow()
    output as a markdown report. Returns a diagnostic string if data has an
    "error" key.
    """
    if "error" in data:
        return f"Institutional flow (EDGAR): {data['error']}"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    if data["kind"] == "institution":
        lines.append(f"# Institutional Holdings — {data['institution']} (CIK {data['cik']})")
        lines.append(f"\nAs of {data['as_of']}"
                      + (f" (prior: {data['prior_as_of']})" if data['prior_as_of'] else "")
                      + f" — {data['total_positions']} positions, "
                      + f"{_fmt_usd(data['total_value_thousands'])} total 13F value.\n")
        lines.append("## Top Holdings\n")
        lines.append("| Issuer | Class | Value | Shares |")
        lines.append("|---|---|---|---|")
        for h in data["top_holdings"][:15]:
            lines.append(f"| {h['issuer']} | {h['class']} | {_fmt_usd(h['value_thousands'])} | {h['shares']:,} |")

        if data["diff"]:
            d = data["diff"]
            if d["new_positions"]:
                lines.append("\n## New Positions This Quarter\n")
                for h in d["new_positions"][:10]:
                    lines.append(f"- {h['issuer']} — {_fmt_usd(h['value_thousands'])}")
            if d["sold_out"]:
                lines.append("\n## Sold Out This Quarter\n")
                for h in d["sold_out"][:10]:
                    lines.append(f"- {h['issuer']} — was {_fmt_usd(h['value_thousands'])}")
            if d["increased"]:
                lines.append("\n## Increased Positions\n")
                for h in d["increased"][:10]:
                    lines.append(f"- {h['issuer']} — +{h['change_pct']}% ({h['shares']:,} sh)")
            if d["decreased"]:
                lines.append("\n## Decreased Positions\n")
                for h in d["decreased"][:10]:
                    lines.append(f"- {h['issuer']} — {h['change_pct']}% ({h['shares']:,} sh)")

    else:  # kind == "stock"
        lines.append(f"# Institutional Flow — {data['ticker']} ({data['resolved_title']})")
        lines.append(f"\n~{data['institutions_found']} institution(s) found holding "
                      f"(sampled, see coverage note below).\n")
        lines.append("## Top Holders\n")
        lines.append("| Institution | Value | Shares |")
        lines.append("|---|---|---|")
        for h in data["top_holders"][:15]:
            lines.append(f"| {h['name']} | {_fmt_usd(h['value_thousands'])} | {h['shares']:,} |")

        if data["diff"]:
            d = data["diff"]
            if d["new_holders"]:
                lines.append("\n## New Holders\n")
                for h in d["new_holders"][:10]:
                    lines.append(f"- {h.get('name', h.get('filer_cik'))} — {_fmt_usd(h['value_thousands'])}")
            if d["exited"]:
                lines.append("\n## Exited Holders\n")
                for h in d["exited"][:10]:
                    lines.append(f"- {h.get('name', h.get('filer_cik'))} — was {_fmt_usd(h['value_thousands'])}")
            if d["changed"]:
                lines.append("\n## Changed Positions\n")
                for h in d["changed"][:10]:
                    lines.append(f"- {h['name']} — {h['change_pct']:+.1f}% ({h['shares']:,} sh)")

        lines.append(f"\n> **Coverage note:** {data['coverage_note']}")

    lines.append(f"\n---\n*Source: {data['source']} · Generated {ts}*")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="EDGAR-native institutional 13F flow tracker")
    p.add_argument("query", help="Institution name (--mode institution) or ticker (--mode stock)")
    p.add_argument("--mode", choices=["institution", "stock"], default="stock")
    p.add_argument("--quarters", type=int, default=2)
    p.add_argument("--max-filers", type=int, default=25)
    args = p.parse_args()

    if args.mode == "institution":
        result = get_institution_holdings(args.query, quarters=args.quarters)
    else:
        result = get_stock_institutional_flow(args.query, quarters=args.quarters, max_filers=args.max_filers)

    print(format_report(result))
