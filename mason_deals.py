"""
mason_deals.py — the deal detector for press-wire releases (news phase 6b, 2026-11-18).

Why: a partnership, a big contract, a licence or a takeover is born on the press wire, with the
company's own wording in the headline. Mason already reads three wires (mason_sources._WIRES) and
files each release under a kind (mason_sources.wire_headline_kind), but that kind misses more than
half of the real deals (measured on 160 hand-labelled releases: recall 0.43) — it has no share-sale
or licensing kind, and "Secures Six-Figure Agreement", "Receives ... Order" or "Proposed Acquisition"
fall through to "other". It also never says WHO the other company is.

What it does (deterministic: no model, no network, no database write):
  detect(title, desc="", subject=None) -> dict | None
    kind          mna | offering | licensing | contract_award | collaboration | partnership
    label         the kind in plain words ("contract or order won")
    status        proposed | agreed | priced | closed | completed | approved | terminated | None
    side          mna only: "acquirer" | "target" | None
    subtype       offering only: equity | convertible | debt | placement | atm | units
    counterparties  tickers of the OTHER listed companies named as parties ([] when none is named or
                    they are private / not US-listed). Derived positively from the text only:
                    exchange tags "(Nasdaq: X)" in the release's first paragraph, and names after
                    the deal words ("partners with NVIDIA", "license agreement with AbbVie"),
                    resolved by mason_triage.resolve_tickers (curated names + the SEC list, with
                    its everyday-word and private-company guards) and kept only when
                    mason_symbols.known() says the symbol is a US listing. (Review 2026-11-19:
                    a company the curated lists mark as a different firm with a famous name --
                    Merck KGaA, Roche, Bayer, UCB ... -- or an organisation of another kind that
                    only carries a listed name -- "Oracle Red Bull Racing", "Novo Nordisk
                    Foundation" -- gets no ticker, and the release's own company under another
                    share class is never "the other party". Known limit: a bare one-word name
                    equal to a listed company's name, e.g. a private Greek "CRONOS", is still
                    taken for that company (Cronos Group); 3 wrong on 5,742 stored releases.)
    names         the counterparty names as written (also the private ones)
    big_partner   a counterparty in the S&P 500 / Nasdaq-100 when the release's own company is not
                  (the "small company partners with NVIDIA" case), else None
    value_usd     the first dollar amount in the headline, or None
    triage_kind   the kind mason_triage files it under (licensing / collaboration / partnership →
                  deal_partnership; offering → offering for shares, other for plain bonds)
    evidence      the words that decided the kind
  The kind comes from the HEADLINE only (an earnings release that mentions a deal is not a deal);
  the blurb is read only for exchange tags and when the headline names no counterparty.
  An offering that is being ENDED (an at-the-market program terminated, an offering withdrawn) keeps
  kind "offering" with status "terminated" but is filed with today's triage kind ("other"): it is not
  a share sale. Values are read in US dollars only.

Switches (mason.config.json → "news" → "deals"; read on every call; everything OFF when absent):
  enabled            false   run the detector on every press-wire release (mason_sources._src_wire):
                             the result is stored in the release's raw record and the kind is used
                             as the triage hint. Off: mason_sources behaves exactly as before.
  big_partner_floor  0       when > 0 (suggested 0.7): a deal with a big listed partner starts at
                             this materiality in triage (mason_triage.market_materiality), so it can
                             reach the inbox through the normal Catalyst alert, with its budget and
                             duplicate rules. 0 = no change in scoring.
Nothing here places an order or calls a model. Public functions never raise.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("mason.deals")
BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"          # tests repoint this

KINDS = ("mna", "offering", "licensing", "contract_award", "collaboration", "partnership")
LABEL = {"mna": "merger or acquisition", "offering": "share or bond sale", "licensing": "licensing deal",
         "contract_award": "contract or order won", "collaboration": "research collaboration",
         "partnership": "partnership"}
_TRIAGE = {"mna": "mna", "offering": "offering", "licensing": "deal_partnership",
           "contract_award": "contract_award", "collaboration": "deal_partnership",
           "partnership": "deal_partnership"}
DEFAULTS = {"enabled": False, "big_partner_floor": 0.0}

# ── config (mtime-cached, read per call) ───────────────────────────────────────

_cfg_cache = {"mtime": None, "path": None, "cfg": {}}


def cfg() -> dict:
    """news.deals over DEFAULTS. {} problems → DEFAULTS (everything off)."""
    out = dict(DEFAULTS)
    try:
        p = Path(CONFIG)
        m = p.stat().st_mtime
        if _cfg_cache["mtime"] != m or _cfg_cache["path"] != str(p):
            _cfg_cache.update(mtime=m, path=str(p), cfg=json.loads(p.read_text(encoding="utf-8")) or {})
        blk = ((_cfg_cache["cfg"].get("news") or {}).get("deals") or {})
        if isinstance(blk, dict):
            out.update({k: v for k, v in blk.items() if not str(k).startswith("_")})
    except Exception:
        pass
    return out


def enabled() -> bool:
    return cfg().get("enabled") is True


def big_partner_floor() -> float:
    try:
        v = float(cfg().get("big_partner_floor") or 0)
    except Exception:
        return 0.0
    return v if 0.0 < v <= 0.95 else 0.0


# ── text helpers ───────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    s = _html.unescape(str(s or ""))
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return " ".join(s.split())


def _R(p: str):
    return re.compile(p, re.I)


# A SPAC's own name ("Bluerock Acquisition Corp. II", "FACT II ACQUISITION CORP.") is not an acquisition.
_SPAC_NAME_RE = _R(r"\b(?:[\w&'.-]+\s+){0,4}acquisition\s+corp(?:oration)?\.?(?:\s+(?:I{1,3}|IV|V|VI{0,3}|IX|X))?\b")

# ── headlines that are never a deal, whatever else they say ────────────────────
_EXCLUDE = [
    ("results", _R(r"\b(?:reports?|announces?|posts?|delivers?|releases?)\b[^|]{0,90}\b(?:results|earnings)\b|"
                   r"\b(?:first|second|third|fourth|q[1-4]|fiscal|full[- ]year|quarter(?:ly)?|annual|interim|"
                   r"half[- ]year|nine[- ]month)\b[^|]{0,40}\b(?:results|earnings)\b|\bresults (?:on|date)\b")),
    ("law_firm", _R(r"\b(?:class action|securities fraud|investor alert|shareholder alert|investors? (?:who|with|have)|"
                    r"law firm|rosen law|pomerantz|levi & korsinsky|kessler topaz|bronstein|faruqi|glancy|"
                    r"deadline alert|investigation (?:alert|notice)|lawsuit|encourages investors)\b")),
    ("payout", _R(r"\bdividends?\b|\bdistributions? (?:of|per)\b|\bdeclares?\b[^|]{0,40}\bdistribution\b")),
    ("people", _R(r"\b(?:appoints?|elects?|hires?|promotes?|retire\w*|resign\w*|steps? down|leadership transition|"
                  r"board of directors|chief [a-z]+ officer)\b")),
    ("event", _R(r"\b(?:to (?:present|participate|host|ring|showcase|feature|exhibit)|will (?:present|participate|host)|"
                 r"conference call|webcast|fireside|investor day|annual (?:general )?meeting|"
                 r"at (?:the )?[^|]{0,40}\b(?:conference|summit|congress|symposium))\b")),
    ("ipo", _R(r"\b(?:initial public offering|IPO|go public|going public|direct listing|uplist\w*|S-1 filing|"
               r"spin[- ]?off)\b")),
    ("spac_housekeeping", _R(r"\b(?:extension of (?:the )?(?:deadline|date|time)|extends? (?:the )?(?:deadline|date|"
                             r"termination date)|separate trading|trust account|redemption|postpone\w*|adjourn\w*)\b")),
    ("own_securities", _R(r"\b(?:tender offers?|exchange offers?|consent solicitations?)\b[^|]{0,90}\b(?:notes|"
                          r"debentures|bonds|securities|preferred)\b|\bmini-tender\b|\bmake-whole\b|"
                          r"\bfundamental change\b|\b(?:share |stock )?(?:repurchase|buyback|buy back)\b|"
                          r"\bnormal course issuer bid\b|\bagreements? to exchange\b|\bexchange agreements?\b|"
                          r"\bdepositary share ratio\b|\bADS ratio\b")),
    # (review 2026-11-19) an INDUCEMENT GRANT to a new employee is not a deal; a WARRANT inducement is a share sale (below)
    ("grant_prize", _R(r"\binducement (?:grants?|awards?|equity|options?|stock options?|RSUs?|restricted|plan)\b|"
                       r"\b(?:employment|employee) inducement\b|\bgrants?\b(?![^|]{0,25}\blicen)|\bscholarship\w*|\bdonat\w+|\bcharit\w+|"
                       r"\bwinners?\b|\bof the year\b|\b(?:wins?|won|receives?|earns?|named|honou?red|recogni[sz]ed)\b[^|]{0,70}"
                       r"\b(?:awards?|prize|medal|recognition|accolade|honou?rs?|leader)\b"
                       r"(?![^|]{0,25}\b(?:contract|agreement|order|task))")),
    ("letters_actions", _R(r"\bletter to (?:shareholders|stockholders)\b|\breverse (?:stock )?split\b|"
                           r"\bstrategic (?:review|alternatives)\b|\bexplor\w+ (?:a )?(?:sale|strategic)\b|"
                           r"\bincreas\w+ (?:its )?(?:ownership )?stake\b|\bintends? to exit\b|\bintention to exit\b|"
                           r"\bexit its (?:shareholding|stake)\b|\breject\w* [^|]{0,30}offer\b")),
    ("insider_trade", _R(r"\b(?:CEO|CFO|COO|chair\w*|directors?|insiders?|officers?|founders?|executives?|president)\b"
                         r"[^|]{0,30}\b(?:buys?|bought|purchases?|purchased|sells?|sold)\b[^|]{0,30}\b(?:shares|stock)\b|"
                         r"\bbuys? (?:more )?shares\b")),
    ("backlog", _R(r"\border backlog\b|\bbacklog of\b")),
    ("sponsorship", _R(r"\bofficial [\w ]{0,30}partner\b|\bfounding partner\w*|\bsponsor\w*|\bnaming rights\b|"
                       r"\bjersey\b|\bstadium\b")),
    ("marketing_collab", _R(r"\blimited[- ]edition\b|\bcandles?\b|\bcapsule\b|\bmerch\w*\b|\bcollab\b|"
                            r"\bcollection\b[^|]{0,40}\bcollaboration\b")),
    # (review 2026-11-19) what a court or a tribunal "awards" is a ruling, not business won
    ("legal", _R(r"\b(?:injunction|verdict|jury|damages|arbitration|judgment|judgement|infringement)\b")),
]

# ── kinds, in the order they are tried ─────────────────────────────────────────
# share / bond sales. Unambiguous phrases decide alone; ambiguous ones ("stock offering",
# "offering of ...") need a capital-raise verb next to them ("Expands Onchain U.S. Stock Offering
# to 25 Equities" is a product, "Pricing of Secondary Public Offering" is a sale).
_OFF_SURE = _R(r"\b(?:public offering|underwritten offering|registered direct offering|private placements?|"
               r"PIPE(?: commitments?| financing| investment)?|at[- ]the[- ]market(?: offering| equity| program)?|"
               r"ATM (?:offering|program|facility)|equity units|depositary shares? offering|"
               r"follow-on offering|secondary (?:public )?offering|bought deal|equity financing|"
               r"(?:senior |convertible |exchangeable |subordinated )*(?:secured |unsecured )?notes offering|"
               r"debentures? offering|bond offering|preferred stock offering|rights offering|equity raise|capital raise|"
               r"fully backstopped|(?:balance sheet|comprehensive|strategic|capital) recapitali[sz]ation|"
               r"recapitali[sz]ation (?:agreement|plan|transaction|support agreement)|"
               # (review 2026-11-19) holders paid to exercise warrants early = new shares sold for cash
               r"warrant (?:exercise )?inducement|inducement (?:letter|agreement|offer|transaction)s?|"
               r"warrant exercise (?:agreement|transaction)s?)\b")
# need a sale word in the headline: "Prices $2.0 billion of investment grade notes" is a sale, "No Convertible
# Notes Outstanding" is not; a bare "offering" must be the object of the sale word itself ("Announces Pricing of
# Offering", "Announces $50 Million Offering"), so "Suite Launches, Offering Systematic..." stays a product
_OFF_MAYBE = _R(r"\b(?:stock offering|share offering|equity offering|unit offering|offering of [^|]{0,60}\b(?:notes|"
                r"shares|units|stock|warrants|debentures|bonds|securities)|convertible (?:senior )?(?:secured )?notes|"
                r"(?:investment grade|green|senior) (?:secured |unsecured )?(?:floating rate )?(?:green )?notes|"
                r"(?:pric\w+|propos\w+|launch\w*|clos\w+|upsiz\w+|commenc\w+|complet\w+|withdr\w+|suspen\w+|"
                r"terminat\w+|announces?)(?: of)?(?: (?:its|an?|the|previously announced))*"
                r"(?: (?:(?:US)?\$?[\d.,]+ ?(?:million|billion|m|bn)|upsized|proposed|underwritten|registered|direct|"
                r"public|secondary|follow-on|overnight|concurrent|marketed|best[- ]efforts))* offering)\b")
_OFF_VERB = _R(r"\b(?:pric\w+|propos\w+|launch\w*|clos\w+|upsiz\w+|commenc\w+|complet\w+|withdr\w+|suspen\w+|"
               r"terminat\w+|files? for|announces? (?:a |an )?(?:\$|US\$)?[\d.,]+ ?(?:million|billion|m|bn)|"
               # (review 2026-11-19) "Announces Offering of Convertible Senior Notes" / "Announces Private Offering of
               # $600 Million of Senior Notes": the plain announcement of the sale itself
               r"announces?(?= (?:an? |its |the )?(?:(?:proposed|private|public|underwritten|registered|concurrent|"
               r"upsized) )*offering\b))\b")
# (review 2026-11-19) an offering that is being ENDED (an at-the-market program terminated, an offering withdrawn) is
# not a share sale: it is filed with today's kind ('other'), with status 'terminated'
_OFF_ENDED = _R(r"\b(?:terminat\w*|withdr\w*|suspen\w*|cancel\w*|abandon\w*|discontinu\w*)\b[^|]{0,60}"
                r"\b(?:offering|program(?:me)?|placement|facility|ATM)\b|\b(?:offering|program(?:me)?|placement)\b"
                r"[^|]{0,30}\b(?:terminated|withdrawn|suspended|cancell?ed|abandoned|discontinued)\b")
_OFF_PRODUCT = _R(r"\bofferings\b|\b(?:onchain|on-chain|tokeni[sz]ed|tokens?)\b|\b(?:product|service|retail|"
                  r"home|menu|solution|platform|patient access) offering\b")
_OFF_SUBTYPE = [("atm", _R(r"\bat[- ]the[- ]market\b|\bATM\b")), ("convertible", _R(r"\bconvertible|exchangeable\b")),
                ("placement", _R(r"\bprivate placement|PIPE|registered direct\b")),
                ("units", _R(r"\bunits?\b")),
                ("debt", _R(r"\bnotes\b|\bbonds?\b|\bdebentures?\b|\bsenior secured\b|\bterm loan\b")),
                ("equity", _R(r"."))]

_MNA_TARGET = _R(r"\b(?:to be acquired|acquired by|acquisition (?:of [^|]{0,60} )?by|agrees? to be (?:acquired|bought)|"
                 r"to be bought|merger agreement|to merge|merger (?:with|of|between)|plans? to merge|planned merger|"
                 r"proposed merger|complete[sd]? (?:the |its |their )?merger|business combination|(?<!in )combination with|"
                 r"takeover|tender offer (?:for|to (?:acquire|purchase) (?:all|the outstanding))|go(?:ing)?[- ]private|"
                 r"buyout|reverse merger|merger-related)\b")
_MNA_ACQ = _R(r"\b(?:to acquire|acquires?|acquired|acquisition of|acquiring|agrees? to (?:acquire|buy|purchase)|"
              r"agreement to (?:acquire|buy|purchase)|(?:offer|bid|proposal) to (?:acquire|buy|purchase)|"
              r"LOI to acquire|letter of intent to acquire|(?:proposed|planned|strategic) acquisition|"
              r"announces? (?:the )?acquisition|complet\w+ (?:the |its )?acquisition|clos\w+ (?:the |its )?acquisition|"
              r"finali[sz]\w+ (?:the |its )?acquisition|to buy|buys|purchases? (?:of )?(?:the )?(?:assets|business|"
              r"operations|facilit\w+|portfolio|stake))\b")
_MNA_SELL = _R(r"\b(?:to sell|sells?|sale of|divest\w*|carve-out)\b[^|]{0,60}\b(?:business|unit|division|segment|"
               r"subsidiary|assets?|stake|operations|program|portfolio|brand)s?\b")
_MNA_STAKE = _R(r"\b(?:majority|controlling|minority|\d{1,2}(?:\.\d+)?%) (?:stake|interest|ownership|equity)\b")
_MNA_STEP = _R(r"\b(?:shareholders?|stockholders?|unitholders?) (?:approve|vote)\w*\b[^|]{0,60}\b(?:merger|acquisition|"
               r"combination|transaction|arrangement)\b|\bproxy statement\b[^|]{0,60}\b(?:merger|combination|"
               r"acquisition)\b|\b(?:ISS|Glass Lewis)\b[^|]{0,60}\b(?:merger|acquisition|combination|transaction)\b|"
               r"\b(?:regulatory|antitrust|HSR|court|final) approval\b[^|]{0,40}\b(?:merger|acquisition|"
               r"combination)\b")
_MNA_END = _R(r"\bterminat\w+[^|]{0,40}\b(?:merger|acquisition|combination|deal)\b|\bsuspends?[^|]{0,60}"
              r"\bacquisition\b|\bwithdr\w+[^|]{0,30}\b(?:offer|proposal|bid)\b|\b(?:holds?|maintains?|raises?|"
              r"increases?|sweetens?|reiterates?|stands by)\b[^|]{0,40}\b(?:offer|proposal|bid) for\b|"
              r"\bunsolicited (?:offer|proposal|bid)\b|\b(?:offer|proposal|bid) for [A-Z]")

_LIC = _R(r"\b(?:licen[cs]e (?:agreement|deal|renewal|and (?:collaboration|option) agreement)|licensing (?:agreement|"
          r"deal|transaction|partnership)|(?:exclusive|global|worldwide|multi-year|non-exclusive|IP|patent|technology|"
          r"commercial) licen[cs]e(?! application)|in-licens\w+|out-licens\w+|option to licen[cs]e|royalty "
          r"(?:agreement|monetization|purchase))\b")

_LIC_RIGHTS = _R(r"\bacqui\w+ (?:the )?(?:exclusive |global |worldwide |commercial |north american |u\.s\. )?"
                 r"(?:rights|licen[cs]e)\b|\b(?:agreement|deal|partnership)\b[^|]{0,60}\bto (?:co-)?develop and "
                 r"commerciali[sz]e\b|\bcommerciali[sz]ation (?:agreement|rights|deal)\b|\brights to (?:develop|"
                 r"commerciali[sz]e|market)\b")

_COLLAB = _R(r"\bclinical (?:trial )?(?:collaboration and )?supply (?:agreement|collaboration)\b|"
             r"\b(?:research|clinical|discovery|scientific|co-development|drug discovery|translational|development) "
             r"(?:collaboration|partnership|agreement)\b|\bco-develop\w*\b|\bcollaborat\w+ (?:to (?:develop|evaluate|"
             r"discover|advance|research|study|test)|on (?:the )?(?:development|research|discovery|clinical))\b")

_CONTRACT = _R(r"\b(?:is awarded|has been awarded|awarded|wins?|won|secures?|secured|receives?|received|lands?|books?|"
               r"signs?|inks?|obtains?)\b[^|]{0,70}\b(?:contracts?|(?:purchase |task |production |follow-on |add-on |"
               r"repeat )?orders?|framework agreements?|supply agreements?|service agreements?|maintenance (?:agreements?|"
               r"contracts?)|repair agreements?|customers?|renewals?|tenders?|awards?(?= (?:for|from|under|to)\b)|"
               r"(?:six|seven|eight|multi)[- ]figure (?:agreements?|deals?))\b|"
               r"\b(?:secures?|secured|wins?|won|lands?)\b[^|]{0,60}\bagreements?\b|\btake-or-pay\b|"
               r"\bagreements?\b[^|]{0,60}\b(?:valued at|worth)\b|"
               r"\b(?:follow-on|add-on|repeat|production|initial|first|new|additional|record) (?:purchase )?orders?\b|"
               r"\b(?:supply|purchase|service|services|maintenance|repair|offtake|charter|manufacturing|engineering) "
               r"(?:agreement|contract)s?\b|\bagreements? to (?:supply|support|provide|deliver|build|install|deploy|operate|"
               r"manage|power|equip|service)\b|\bcharters?\b|\btask orders?\b|\bIDIQ\b|\bawarded\b")
_SELECT = _R(r"\b(?:selected (?:by|as|to)|selects|chosen (?:by|as)|taps|awards)\b")

_PARTNER = _R(r"\b(?:strategic partnership|partnership (?:agreement|with|between|to|for)|partners with|"
              r"announces? (?:a |an )?(?:new |strategic |expanded |multi-year )?partnership|"
              r"(?:expand|extend|deepen|renew|broaden|strengthen)\w* (?:its |their |our )?(?:strategic |global |long-term )?"
              r"(?:partnership|collaboration|alliance|relationship)|teams? up with|team up|strategic alliance|"
              r"alliance with|joint venture|JV|memorandum of understanding|MOU|cooperation agreement|co-sell\w*|"
              r"reseller agreement|distribution (?:agreement|deal|partnership)|in collaboration with|strategic collaboration|"
              r"collaboration (?:with|between|agreement)|collaborates? with|integration partnership)\b|"
              r"\band\b[^|]{1,80}\bpartner (?:to|on|with|in|for)\b")
_AGREEMENT = _R(r"\b(?:signs?|signed|enters? into|entered into|inks?|announces?|reach(?:es)?|forms?)\b[^|]{0,50}"
                r"\b(?:agreement|alliance|venture)\b|\bagreement with\b")

# (review 2026-11-19) only US dollars: "C$20 million", "A$3.8 million", "HK$" are not read as dollars
_VALUE_RE = re.compile(r"(?<![A-Za-z])(?:US)?\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|million|bn|mn|B|M)\b", re.I)
# (review 2026-11-19) whole words only ("Centers" is not "enters", "Platform" is not "plan")
_STATUS = [("terminated", _R(r"\b(?:terminat\w+|withdr\w+|suspen\w+|cancel\w+)")),
           ("completed", _R(r"\b(?:complet\w+|clos(?:es|ed|ing) (?:of |the |its )?(?!offering)|finali[sz]\w+)")),
           ("closed", _R(r"\bclos(?:es|ed|ing)\b")),
           ("priced", _R(r"\bpric(?:es|ed|ing)\b")),
           ("approved", _R(r"\bapprov\w+")),
           ("proposed", _R(r"\b(?:propos\w+|launch\w*|plans?|intends?|LOI|letter of intent|non-binding|MOU|memorandum|"
                           r"exclusivity|offer to|bid|commenc\w+)\b")),
           ("agreed", _R(r"\b(?:agree\w*|definitive|signs?|signed|enters?|entered|to acquire|to merge|announces?)\b"))]


def _first(rx, text):
    m = rx.search(text)
    return m.group(0) if m else None


def _excluded(title: str):
    for why, rx in _EXCLUDE:
        if rx.search(title):
            return why
    return None


def _value(text: str):
    m = _VALUE_RE.search(text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2).lower()
    return v * (1e9 if unit in ("billion", "bn", "b") else 1e6)


def _status(text: str):
    for st, rx in _STATUS:
        if rx.search(text):
            return st
    return None


def _offering(t: str):
    sure = _first(_OFF_SURE, t)
    if not sure:
        maybe = _first(_OFF_MAYBE, t)
        if not maybe or _OFF_PRODUCT.search(t) or not _OFF_VERB.search(t):
            return None
        sure = maybe
    elif _OFF_PRODUCT.search(t) and not re.search(r"(?i)public|registered|placement|notes|convertible|secondary|"
                                                     r"depositary|units|ATM|at-the-market", sure):
        return None
    sub = next(k for k, rx in _OFF_SUBTYPE if rx.search(t))
    if sub == "units" and re.search(r"(?i)notes", t):
        sub = "debt"
    return sure, sub


def _kind(t: str):
    """(kind, evidence, extra) from a cleaned headline, or None."""
    o = _offering(t)
    if o:
        return "offering", o[0], {"subtype": o[1]}
    t2 = _SPAC_NAME_RE.sub(" ", t)
    for rx, side in ((_MNA_TARGET, "target"), (_MNA_STEP, "target"), (_MNA_END, None)):
        ev = _first(rx, t2)
        if ev:
            return "mna", ev, {"side": side}
    ev = _first(_LIC_RIGHTS, t2)
    if ev:
        return "licensing", ev, {}
    ev = _first(_MNA_SELL, t2)
    if ev:
        return "mna", ev, {"side": "seller"}
    ev = _first(_MNA_ACQ, t2)
    if ev:
        return "mna", ev, {"side": "acquirer"}
    if _MNA_STAKE.search(t2) and re.search(r"(?i)acqui|purchas|buy|stake sale|sell", t2):
        return "mna", _first(_MNA_STAKE, t2), {"side": None}
    ev = _first(_LIC, t2)
    if ev:
        return "licensing", ev, {}
    ev = _first(_COLLAB, t2)
    if ev:
        return "collaboration", ev, {}
    ev = _first(_PARTNER, t2)
    if ev:
        return "partnership", ev, {}
    ev = _first(_CONTRACT, t2)
    if ev:
        return "contract_award", ev, {}
    ev = _first(_SELECT, t2)
    if ev:
        return "contract_award", ev, {"select": True}
    ev = _first(_AGREEMENT, t2)
    if ev:
        return ("contract_award" if re.search(r"(?i)supply|order|service|maintenance|repair|purchase", t2)
                else "partnership"), ev, {}
    return None


# ── who is on the other side ───────────────────────────────────────────────────

_STOP = (r"(?=\s*(?:[,;:|(\[]|\s-\s|\bto\b|\bfor\b|\bin\b|\bon\b|\bat\b|\bas\b|\bthat\b|\bwhich\b|\bwho\b|\bfrom\b|\bvia\b|"
         r"\bacross\b|\bthrough\b|\binto\b|\bover\b|\bunder\b|\bafter\b|\bamid\b|\bfollowing\b|\bwith\b|\band\b|\bby\b|$))")
_NAME = r"((?:the\s+)?[A-Z0-9][\w&.'\-/]*(?:\s+(?:of|de|&|[A-Z0-9][\w&.'\-/]*)){0,6}?)"
_CP_CUES = [
    _R(r"\b(?:with|between)\s+" + _NAME + _STOP),
    re.compile(r"^" + _NAME + r"\s+and\s+" + _NAME + r"\s+(?:announce|partner|expand|extend|sign|enter|form|launch|deepen|"
               r"team|collaborate|complete|agree|join|ink|unveil|deliver|bring|introduce|establish)", re.I),
    _R(r"\b(?:acquire|acquires|acquired|acquisition of|to buy|buys|offer for|merger with|combination with|acquired by|"
       r"selected by|chosen by|awarded by|order from|contract from)\s+" + _NAME + _STOP),
    re.compile(r"^" + _NAME + r"\s+(?:selects|taps|awards|chooses|picks)\b", re.I),
]


def _triage():
    try:
        import mason_triage as T
        return T
    except Exception:
        return None


def _known(sym: str) -> bool:
    try:
        import mason_symbols
        return bool(mason_symbols.known(sym))
    except Exception:
        return False


_DIVISION = {"business", "enterprise", "consumer", "commercial", "federal", "government", "division", "unit"}


def _span_is(span: str, tk: str) -> bool:
    """True when the name span really IS the company tk: one of mason_triage's curated names
    matches inside it, or the span is the company's own (normalised) name, optionally followed by
    generic words ("AT&T Business", "Regeneron Pharmaceuticals"). A different company that shares a
    word with it ("EVEREST CLOUD" is not Everest Group) does not count."""
    T = _triage()
    try:
        for rx, atk, _ctx in T._ALIASES_C:
            if atk == tk and rx.search(span):
                return True
        names = T._resolver_index()["names"]
        toks = T._norm_name(span).split()
        while toks:
            if names.get(" ".join(toks)) == tk:
                return True
            if toks[-1] in T._GENERIC_TAIL or toks[-1] in T._NAME_SUFFIX or toks[-1] in _DIVISION:
                toks.pop()
                continue
            break
    except Exception:
        return False
    return False


# (review 2026-11-19) a name that belongs to a different organisation than the listed company it contains: a sports team,
# a foundation, a university or hospital named after (or sponsored by) the company ("Oracle Red Bull Racing", "Novo
# Nordisk Foundation"), and a German KGaA ("Merck KGaA, Darmstadt" is not Merck & Co.)
_OTHER_ORG_RE = re.compile(r"(?i)\b(?:foundation|racing|team|university|college|school|hospitals?|clinic|institute|"
                           r"museum|festival|league|stadium|arena|academy|charity|kgaa)\b")


def _blocked(span: str) -> bool:
    """True for a span that names a company the curated lists say is NOT the US listing that shares its name
    (mason_sources._ALIASES entries mapped to "": Merck KGaA / EMD Serono, Roche / Genentech, Bayer, UCB, ...),
    or an organisation of another kind that only carries a listed company's name (_OTHER_ORG_RE)."""
    if _OTHER_ORG_RE.search(span or ""):
        return True
    try:
        import mason_sources as S
        low = " ".join(str(span or "").lower().split())
        return any(tk == "" and re.search(pat, low) for pat, tk in S._ALIASES)
    except Exception:
        return False


_titles_mem = {"key": None, "map": {}}


def _sec_title(tk: str) -> str:
    """The SEC company name of a ticker ("" when unknown). Two tickers with the same name are one company
    (DeFi Development Corp. trades as DFDV and its preferred stock as CHAD)."""
    T = _triage()
    try:
        k = T.EDGAR_CACHE.stat().st_mtime
        if _titles_mem["key"] != k:
            _titles_mem.update(key=k, map={str(t).upper(): str(n or "").strip().lower() for t, n in T._edgar_rows()})
        return _titles_mem["map"].get(str(tk or "").upper(), "")
    except Exception:
        return ""


def _same_company(a: str, b: str) -> bool:
    ta = _sec_title(a)
    return bool(ta) and ta == _sec_title(b)


def _resolve(text: str) -> list:
    """Listed companies named by one counterparty span (see _span_is). [] when none."""
    T = _triage()
    if T is None or not text or _blocked(text):
        return []
    try:
        return [t for t in T.resolve_tickers(text) if _known(t) and _span_is(text, t)]
    except Exception:
        return []


# a US exchange tag inside parentheses, also when the parentheses list several markets:
# "(NYSE: BAM, TSX: BAM)", "(TSX: TRP; NYSE: TRP)", "(Nasdaq: CRWV)"
_US_TAG_RE = re.compile(r"\([^()]{0,40}?\b(?:NASDAQ|Nasdaq|NYSE(?: American| Arca)?|NYSEAMERICAN|AMEX|Cboe(?: BZX)?)"
                        r"\s*[:：]\s*([A-Z]{1,5}(?:\.[A-Z])?)\s*(?=[,;)])")


def _exchange_tags(text: str) -> list:
    out = []
    try:
        for m in _US_TAG_RE.finditer(text or ""):
            tk = m.group(1).upper()
            if tk not in out and _known(tk):
                out.append(tk)
    except Exception:
        pass
    return out


def _names(t: str) -> list:
    out = []
    for rx in _CP_CUES:
        for m in rx.finditer(t):
            for g in m.groups():
                n = (g or "").strip(" .,'")
                n = re.sub(r"(?i)^the\s+", "", n)
                n = re.sub(r"'s$", "", n)
                if n and len(n) >= 2 and n not in out:
                    out.append(n)
    return out


def _broad() -> set:
    """S&P 500 and Nasdaq-100 members only. mason_triage.broad_universe also carries the book (index "book"),
    and a held or watched small company is not a "big partner"."""
    T = _triage()
    if T is None:
        return set()
    try:
        return {t for t, m in T.broad_universe(allow_refresh=False).items()
                if set((m or {}).get("index") or []) & {"SP500", "NDX"}}
    except Exception:
        return set()


def detect(title: str, desc: str = "", subject=None) -> dict | None:
    """See the module doc. None when the headline is not a deal. Never raises."""
    try:
        t = _clean(title)
        if not t or _excluded(t):
            return None
        k = _kind(t)
        if not k:
            return None
        kind, evidence, extra = k
        subj = str(subject or "").upper() or None
        d = _clean(desc)[:400]
        names = _names(t)
        cps = []
        for n in names:
            for tk in _resolve(n):
                if tk != subj and tk not in cps:
                    cps.append(tk)
        for tk in _exchange_tags(d) + _exchange_tags(t):
            if tk != subj and tk not in cps:
                cps.append(tk)
        if subj:                                 # (review) never the release's own company under another share class
            cps = [c for c in cps if not _same_company(c, subj)]
        if kind == "contract_award" and extra.get("select") and subj:
            # "<Customer> selects <Vendor>": the release's own company buying is not a win for it
            head = re.split(r"(?i)\b(?:selects|taps|chooses|picks)\b", t, maxsplit=1)[0]
            if subj in _resolve(head):
                return None
        broad = _broad() if cps else set()
        big = None
        if kind in ("partnership", "collaboration", "licensing", "contract_award") and subj and subj not in broad:
            big = next((c for c in cps if c in broad), None)
        tri = _TRIAGE[kind]
        status = _status(t)
        if kind == "offering" and extra.get("subtype") == "debt":
            tri = "other"                       # a plain bond sale: no dilution, keep today's kind
        if kind == "offering" and _OFF_ENDED.search(t):
            tri, status = "other", "terminated"  # (review) an ended offering is not a share sale: today's kind
        return {"kind": kind, "label": LABEL[kind], "status": status,
                "side": extra.get("side") if kind == "mna" else None,
                "subtype": extra.get("subtype") if kind == "offering" else None,
                "counterparties": cps[:4], "names": names[:4], "big_partner": big,
                "value_usd": _value(t), "triage_kind": tri, "evidence": evidence[:80]}
    except Exception as e:
        log.debug(f"[deals] detect failed: {e}")
        return None


def for_wire(title: str, desc: str, tickers) -> dict | None:
    """What mason_sources stores for one wire release when news.deals is on: the detect()
    result plus the triage floor for a big-partner deal (only when big_partner_floor > 0).
    None when the switch is off or the release is not a deal. Never raises."""
    try:
        if not enabled():
            return None
        subj = (list(tickers or []) or [None])[0]
        d = detect(title, desc, subj)
        if not d:
            return None
        fl = big_partner_floor()
        if fl and d.get("big_partner"):
            d["floor"] = round(fl, 3)
        return d
    except Exception:
        return None
