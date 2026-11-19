"""
mason_news_quality.py — one inbox item per story, and only real, tradable news in it.
(Audit 2026-11-13, "news quality" piece, built 2026-11-15.)

Three streams write news into the inbox (table `notifications`):
  * news      — mason_notifications.sync_material_news (created_by='news_service')
  * breaking  — mason_analyst_desk "Breaking: TICKER — headline" (created_by='mason')
  * triage    — mason_triage "Catalyst: TICKER — event (score)" (created_by='mason_triage')
Each one decided on its own, so the same story arrived two or three times, routine
items (rating / price-target notes, conference presentations, market wraps,
look-backs) were posted as "major moves", and a few foreign listings slipped in.

This module is the shared rulebook all three use. It is deterministic and free:

  gate_event(ev, stream=...)      should this news event become an inbox item at all,
                                  and under which ticker?  (symbol sanity, subject,
                                  routine filter, age)
  routine_reason(headline)        is this a routine item rather than an event?
  subject_ticker(headline, tks)   which listed company is the headline about?
  match_story(new, items)         does an inbox item for the same story already exist?
  place(...)                      write the item: new row, fold into the existing one,
                                  or upgrade the existing unread one in place.

Config (mason.config.json -> "news" -> "quality"; read on each call; every switch ON
when the key is absent; "enabled": false turns the whole module into a no-op):
  enabled                       true   master switch
  symbol_gate                   true   only US-listed tickers; subject must be the company named
  routine_filter                true   routine items do not alert unless held + material
  routine_held_min_materiality  0.7    the "clearly material" bar for a held name
  story_fold                    true   one inbox item per story (cross-stream + repeat batches)
  same_item_hours               24     same article / same triage story / near-same headline
  story_fold_hours              12     same ticker + same kind of event, or a follow-up piece
  breaking_merge                true   a Breaking item for news already in the inbox upgrades it
  triage_merge                  true   a Catalyst alert for a story already in the inbox upgrades it
  relabel_news                  true   a plain news headline is type 'news', not 'major_move'
  max_age_hours                 24     news / Breaking items older than this are not posted
  max_age_hours_held            72     the same limit for a name Nico or Mason HOLDS (covers a weekend
                                       scoring backlog; review fix 2026-11-15)
  label_fixes                   true   triage / PR-wire event-label fixes (see mason_triage)
  second_strong_same_kind       true   a second strong event of a kind already alerted may alert once
  ui_filter                     true   /api/news/notifications hides what the inbox refused or folded
  retitle_stronger              true   a headline STRONGER than its story item's title (a stronger kind of
                                       event, or the same rank and materiality up by retitle_min_mat_step)
                                       takes over the item: an unread item is re-titled (the old title goes
                                       under "More on this story"); on a READ item, or one it cannot take
                                       over cleanly (a Breaking / Catalyst row), it gets its own row
                                       (follow-up 1, 2026-11-16)
  retitle_min_mat_step          0.2    the materiality step that counts as "stronger" at the same rank
  skip_repeat_lead              true   a row never lists its own lead article again under "More on this
                                       story" (follow-up 3)
  fold_held_names               false  false = a HELD name (held_set: Nico's holdings + Mason's open trades /
                                       positions) keeps every distinct headline as its own row; only the
                                       same article or a near-same rewrite of the row's headline folds /
                                       upgrades (match_story pass 1 'same_item' / 'same_headline').
                                       true = held names are grouped like any other (Nico, 2026-11-16)
  triage_bucket_check           true   a triage alert that matched an unread row ONLY through the shared
                                       per-ticker bucket upgrades it only when it is about the same thing;
                                       otherwise it is its own row (follow-up 4)
  shared_form_rule              true   one held-name form rule (held_event_form) for the inbox gate, the
                                       triage alert check and the analyst desk's Breaking skip; it does not
                                       excuse a round-up ("AM Need to Know: ...") or a law firm's advert, and
                                       a preview / rating note behind the form still counts. false = the
                                       2026-11-15 rule in the inbox gate only (NEWS_REVIEW follow-up 5,
                                       2026-11-16)
  held_live_blog                true   with shared_form_rule: a held company's own live blog ("Nvidia earnings
                                       live updates: ...", a label naming only that company, an event word and
                                       live-blog words) is not a round-up. false = every recap-form label is a
                                       round-up, as before (leftovers 2, 2026-11-16)

Public functions never raise (they degrade to the old behaviour).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("mason.news_quality")
BASE = Path(__file__).parent
CONFIG = BASE / "mason.config.json"

_DEFAULTS = {
    "enabled": True,
    "symbol_gate": True,
    "routine_filter": True,
    "routine_held_min_materiality": 0.7,
    "story_fold": True,
    "same_item_hours": 24,
    "story_fold_hours": 12,
    "breaking_merge": True,
    "triage_merge": True,
    "relabel_news": True,
    "max_age_hours": 24,
    "max_age_hours_held": 72,
    "label_fixes": True,
    "second_strong_same_kind": True,
    "ui_filter": True,
    "retitle_stronger": True,
    "retitle_min_mat_step": 0.2,
    "skip_repeat_lead": True,
    "triage_bucket_check": True,
    "fold_held_names": False,
    "shared_form_rule": True,
    "held_live_blog": True,
}
_cfg_cache = {"mtime": None, "data": {}}
OVERRIDES: dict = {}            # tests / replays only: laid over the config file


def cfg() -> dict:
    """news.quality merged over the defaults (+ OVERRIDES). Never raises."""
    try:
        mt = CONFIG.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(CONFIG.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
        blk = ((_cfg_cache["data"].get("news") or {}).get("quality") or {})
        if not isinstance(blk, dict):
            blk = {}
    except Exception:
        blk = {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in blk.items() if v is not None})
    out.update(OVERRIDES)
    return out


def on(key: str) -> bool:
    """True when the module is enabled AND switch `key` is on."""
    try:
        c = cfg()
        return bool(c.get("enabled", True)) and bool(c.get(key, True))
    except Exception:
        return False


# ── the book ─────────────────────────────────────────────────────────────────────

_BOOK_TTL = 60.0
_book_mem = {"ts": 0.0, "held": None, "book": None}


def _refresh_book(force: bool = False) -> None:
    now = time.time()
    if not force and _book_mem["held"] is not None and now - _book_mem["ts"] < _BOOK_TTL:
        return
    held, watch = set(), set()
    try:
        import mason_news
        held |= {str(t).upper() for t in mason_news.holdings_set()}
        watch |= {str(t).upper() for t in mason_news.watchlist_set()}
    except Exception:
        pass
    try:
        import mason_db
        conn = mason_db.get_connection()
        try:
            for sql in ("SELECT DISTINCT ticker FROM agent_trades WHERE status='open' "
                        "AND COALESCE(record_deleted,0)=0 AND ticker IS NOT NULL",
                        "SELECT DISTINCT ticker FROM personal_positions WHERE status='open' "
                        "AND COALESCE(is_deleted,0)=0 AND ticker IS NOT NULL"):
                try:
                    held |= {str(r[0]).upper() for r in conn.execute(sql).fetchall() if r[0]}
                except Exception:
                    pass
        finally:
            conn.close()
    except Exception:
        pass
    _book_mem.update(ts=now, held=held, book=held | watch)


def held_set() -> set:
    """Names Nico (or Mason's own trades) actually hold — not the watchlist."""
    _refresh_book()
    return set(_book_mem["held"] or ())


def book_set() -> set:
    """Holdings + watchlist (the 'book' the news bridge is scoped to)."""
    _refresh_book()
    return set(_book_mem["book"] or ())


def reset_cache() -> None:
    _book_mem.update(ts=0.0, held=None, book=None)
    _decision_cache.clear()


# ── symbol sanity + subject ─────────────────────────────────────────────────────

def tradable(sym) -> bool:
    """A US listing that is worth an alert: known to Mason's listing set (SEC list ∪
    book ∪ allow-list ∪ Alpaca supplement) and not remembered as dead. Numbers-only
    symbols (Hong Kong '700', Tokyo '7203') are never US tickers, even when the
    listing set cannot be loaded."""
    try:
        s = str(sym or "").strip().upper()
        if not s or s.isdigit() or not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,2})?", s):
            return False
        import mason_symbols
        return bool(mason_symbols.work_ok(s))
    except Exception:
        return True


_GENERIC_NAME = set(
    "inc incorporated corp corporation co company companies ltd limited plc ag sa nv se lp llc "
    "holdings holding group the and of de class common stock shares trust fund etf adr ads "
    "technologies technology industries international systems solutions global partners capital "
    "financial bancorp bancshares bank energy resources pharmaceuticals pharmaceutical therapeutics "
    "pharma biosciences sciences medical health healthcare services enterprises worldwide american "
    "national first united general new royalties acquisition semiconductor semiconductors".split())


def _norm(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'")


def _titles() -> dict:
    try:
        import mason_symbols
        return mason_symbols._titles() or {}
    except Exception:
        return {}


def mention_spans(sym: str, headline: str, titles: dict = None) -> list:
    """[(start, end)] where `headline` names the issuer behind `sym`: a ticker cue
    ($SYM, (SYM), 'NYSE: SYM'), the bare symbol as an upper-case word (2+ letters,
    not an everyday abbreviation), or a distinctive word of its registered name
    (case-insensitive word start: 'Macy's' for "Macy's, Inc.", 'Dominion' for
    'DOMINION ENERGY, INC'). Empty when the headline does not name it."""
    out = []
    try:
        s = str(sym or "").strip().upper()
        text = _norm(headline)
        if not s or not text:
            return out
        for m in re.finditer(rf"(?:\$|\(|:\s?){re.escape(s)}(?![A-Za-z])", text):
            out.append((m.start(), m.end()))
        if len(s) >= 2:
            try:
                import mason_symbols
                two = mason_symbols._TWO_LETTER_WORDS
            except Exception:
                two = set()
            if len(s) >= 3 or s not in two:
                for m in re.finditer(rf"(?<![A-Za-z$&.']){re.escape(s)}(?![A-Za-z'])", text):
                    out.append((m.start(), m.end()))
        titles = titles if titles is not None else _titles()
        title = titles.get(s) or titles.get(s.replace(".", "-")) or ""
        toks = [t for t in re.findall(r"[a-z0-9]+", title.lower().replace("'", ""))
                if len(t) >= 3 and t not in _GENERIC_NAME]
        low = text.lower()
        for t in toks[:3]:
            if len(t) < 4:
                continue          # 'nov' (NOV Inc.) would match the month in "Nov. 6"
            for m in re.finditer(rf"\b{re.escape(t)}", low.replace("'", " ") if "'" in t else low):
                # a word that STARTS with the name token ("Xencor's", "Snapchat" ~ "Snap")
                out.append((m.start(), m.end()))
        return sorted(set(out))
    except Exception:
        return out


# "<A> Upgrades/Maintains/... <B>", "<A> Liquidates Stake in <B>": A is the broker or the
# investor; the headline is about B.
_ACTOR_VERB_RE = re.compile(
    r"\b(?:upgrad\w+|downgrad\w+)\b|"
    r"\b(?:maintain\w*|reiterat\w+|reaffirm\w+|initiat\w+|reinstat\w+|assum\w+|"
    r"starts?|adjust\w*|rais\w+|lower\w*|cuts?|boost\w*|lift\w*|trims?|trimm\w+|hik\w+|slash\w*|"
    r"issu\w+|assign\w*|liquidat\w+|increas\w+|reduc\w+|exit\w*|dissolv\w+|sells?|sold|buys?|bought|"
    r"takes?|took|adds?|added|discloses?|reports?)\b"
    r"(?=[^.;|]{0,60}\b(?:rating|price target|target price|PT|coverage|stake|position|holding|shares? in)\b)", re.I)
# "... Camera Featuring onsemi Sensor", "... Utilizing Xencor's Xtend Technology": the company
# named here supplies a part or a technology; the headline is about someone else.
_SUPPLIER_RE = re.compile(r"\b(?:featuring|utili[sz]ing|using|powered by|built (?:on|with)|based on|equipped with|"
                          r"incorporating|leveraging|with (?:the )?help of|running on|selects?|chooses?|picks?)"
                          r"\s+(?:the\s+|its\s+|a\s+)?$", re.I)


def _supplier_only(spans: list, headline: str) -> bool:
    """Every mention sits right after a 'featuring / utilizing / powered by ...' cue."""
    if not spans:
        return False
    text = _norm(headline)
    return all(_SUPPLIER_RE.search(text[max(0, a - 40):a]) for a, _b in spans)


_LIST_JOIN_RE = re.compile(r"^(?:'s|’s)?\s*(?:,\s*(?:and\s+|&\s+)?|\s+(?:and|&)\s+)$", re.I)


def subject_group(headline: str, named: list, spans: dict) -> list:
    """The first-named company plus any named right beside it in a list ('AMD, Broadcom
    rally', 'SK Telecom, Nvidia, and Rebellions collaborate'): co-subjects."""
    if not named:
        return []
    text = _norm(headline)
    group = [named[0]]
    end = spans[named[0]][0][1]
    for t in named[1:]:
        a, b = spans[t][0]
        gap = text[end:a]
        # skip the rest of a multi-word name ("Lattice Semiconductor, Entegris")
        gap = re.sub(r"^(?:\s+[A-Z][\w.&'-]*){0,3}", "", gap)
        if _LIST_JOIN_RE.match(gap):
            group.append(t)
            end = b
        else:
            break
    return group


def subject_ticker(headline: str, tickers, *, resolved=None, desc: str = "", prefer=None) -> tuple:
    """(ticker | None, why). The listed company the headline is ABOUT, among the
    vendor tags + Mason's own name resolution. Rules, in order:
      * only tradable US listings count (foreign '700', 'BEIA.B', 'NAPA' never);
      * a broker or investor in front of a rating / stake verb is not the subject
        ('Morgan Stanley Downgrades Coinbase' -> COIN; 'Jana ... Liquidates Stake in Everpure' -> P);
      * a company named only as a part supplier is not the subject
        ('... Camera Featuring onsemi Sensor' -> nobody listed);
      * otherwise the company named first; if none is named, the first tradable tag.
    why: 'ok' | 'no_us_listing' | 'supplier_only'."""
    try:
        cands = []
        for t in list(tickers or []) + list(resolved or []):
            u = str(t or "").strip().upper()
            if u and u not in cands:
                cands.append(u)
        if not cands:
            return None, "no_tickers"
        trad = [t for t in cands if tradable(t)]
        if not trad:
            return None, "no_us_listing"
        titles = _titles()
        spans = {t: mention_spans(t, headline, titles) for t in trad}
        named = [t for t in trad if spans[t]]
        supplier = [t for t in named if _supplier_only(spans[t], headline)]
        named = [t for t in named if t not in supplier]
        if supplier and not named:
            return None, "supplier_only"
        named.sort(key=lambda t: spans[t][0][0])
        m = _ACTOR_VERB_RE.search(_norm(headline))
        if m and len(named) >= 2:
            after = [t for t in named if spans[t][0][0] > m.start()]
            if after:
                return after[0], "ok"
        if named:
            if prefer:
                pref = {str(p).upper() for p in prefer}
                for t in subject_group(headline, named, spans):
                    if t in pref:
                        return t, "ok"
            return named[0], "ok"
        return trad[0], "ok"
    except Exception:
        tl = [str(t).upper() for t in (tickers or []) if t]
        return (tl[0] if tl else None), "ok"


# ── routine items ────────────────────────────────────────────────────────────────

def _R(p):
    return re.compile(p, re.I)


# A brokerage's view of the stock: rating, price target, coverage. "change" = the rating
# itself moved (up/downgrade, new coverage); everything else ("maintains", "adjusts price
# target", "analysts update ratings") is a note. Credit ratings are not stock opinions.
_RATING_CHANGE_RE = _R(r"\b(?:upgrad\w*|downgrad\w*)\b|\b(?:initiat\w*|starts?|launch\w*|assum\w*|resum\w*|reinstat\w*)"
                       r"\b(?:[^.;|]){0,25}\bcoverage\b|\binitiat\w* (?:\w+ ){0,6}(?:at|with) (?:an? )?"
                       r"(?:buy|sell|hold|neutral|outperform|underperform|overweight|underweight|equal[- ]weight|"
                       r"market perform|sector perform|peer perform|in[- ]line|strong buy|accumulate|reduce)\b")
_RATING_NOTE_RE = _R(r"\bprice[- ]targets?\b|\btarget price\b|\bPT\b|\bratings?\b|\bcoverage\b|"
                     r"\banalysts?\b(?:[^.;|]){0,30}\b(?:outlook|ratings?|targets?|estimates|views?|says|sees)\b|"
                     r"\b(?:buy|sell|hold|neutral|outperform|underperform|overweight|underweight|equal[- ]weight|"
                     r"market perform|sector perform|in[- ]line) rating\b|"
                     r"\b(?:maintains|reiterates|reaffirms|keeps)\b(?:[^.;|]){0,40}\b(?:buy|sell|hold|neutral|outperform|"
                     r"underperform|overweight|underweight|equal[- ]weight|market perform|sector perform)\b")
_RATING_ACTOR_RE = _R(r"\b(?:upgrad\w*|downgrad\w*|maintain\w*|reiterat\w*|reaffirm\w*|initiat\w*|adjust\w*|rais\w+|"
                      r"lower\w*|cuts?|boost\w*|lift\w*|trims?|issu\w+|assign\w*|receiv\w+|updat\w+|hik\w+|slash\w*|"
                      r"reduc\w+|increas\w+|sets?|keeps?)\b")
_CREDIT_RE = _R(r"\b(?:credit|debt|bonds?|notes|junk|investment[- ]grade|sovereign|Moody's|Moody’s|Fitch|"
                r"S&P Global Ratings|DBRS|outlook to (?:negative|positive|stable))\b")

# Presenting data / speaking at a meeting. The first report of pivotal results is an event
# even when it is made at a congress, so those are left to the event rules.
_CONF_RE = _R(r"\b(?:present\w*|showcas\w*|highlight\w*|shar(?:e|es|ed|ing)|unveil\w*|report\w*|announc\w*)\b"
              r"(?:[^.;|]){0,90}\bat (?:the )?(?:[\w&’'.\-]+ ){0,6}(?:\d{4}|annual|meeting|congress|conference|"
              r"symposium|summit|convention|forum|week|convergence|sessions?)\b|"
              r"\b(?:to )?(?:present|participat\w*|speak|host)\w* (?:at|in) (?:the |an |upcoming |two |three |several )?"
              r"(?:[\w&’'.\-]+ ){0,6}(?:conferences?|summits?|symposi\w+|fireside chats?|meetings?|forums?)\b|"
              r"\b(?:data|poster|oral|late[- ]breaking) presentations?\b")
_PIVOTAL_RE = _R(r"\b(?:top[- ]?line|pivotal|phase\s?(?:3|III)|primary endpoint|met (?:its |the )?(?:primary|main))\b")

# Looking back over a long span rather than reporting something that just happened.
_RETRO_RE = _R(r"\b(?:during|over|across|in|after) (?:\w+(?:'s|’s) )?(?:the )?(?:past |last |first |his |her |its )?"
               r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|several) (?:years|decades)\b|"
               r"\b(?:since|after) (?:its |the )?(?:IPO|founding|inception|listing)\b|"
               r"\bfrom \$[\d.,]+ ?(?:[mb]illion|trillion|[mbt]n?)? to \$[\d.,]+ ?(?:[mb]illion|trillion|[mbt]n?)\b|"
               r"\b(?:anniversary|a decade of|decades of|history of|years as (?:CEO|chief|chair\w*)|the \w+ era)\b")

# A scheduled event that has not happened yet (earnings previews, "what to watch").
_PREVIEW_RE = _R(r"\b(?:ahead of|before|into|heading into|going into) (?:\w+(?:'s|’s) )?(?:the |its |next |this )?"
                 r"(?:week(?:'s|’s)? |quarter(?:'s|’s)? |upcoming |key |big )?(?:earnings|results|report|print|"
                 r"Q[1-4]|quarterly|call|investor day|fed|FOMC|meeting|decision)\b|"
                 r"\b(?:earnings|results)\b(?:[^.;|]){0,50}\b(?:next week|this week|week ahead|on deck|on tap|loom\w*)\b|"
                 r"\bgearing up for\b|\bmost anticipated\b|\b(?:what|things) to (?:watch|expect)\b|"
                 r"\bset to report\b|\bto report (?:[\w-]+ ){0,3}(?:earnings|results)\b|\bweek ahead\b|\bearnings preview\b|"
                 r"\b(?:ahead of|before) (?:the )?(?:CPI|PPI|PCE|jobs report|payrolls|GDP|inflation data)\b|"
                 r"\breports? (?:after the bell|before the (?:bell|open)|after the close|tonight|tomorrow)\b")

# A commentator's view, a feature or an explainer — the form says "someone talking about
# news", not "something happened".
_COMMENT_RE = _R(r"\b(?:warning|message|advice|verdict|prediction|take|plea)s? (?:for|to) (?:\w+ ){0,3}"
                 r"(?:investors|shareholders|holders|traders|bulls|bears)\b|"
                 r"[:—–]\s*(?:how|why|what|inside|the (?:inside |real )?story|a look|lessons)\b|"
                 r"(?:^|[.;:]\s)I(?:'d|’d| would|'m|’m| am| will|'ll|’ll)? (?:\w+ ){0,3}(?:buy|sell|own|avoid|buying|selling)\b|"
                 r"\bth(?:is|ese) (?:[\w\"“”'’-]+ ){0,4}stocks?\b(?! (?:split|offering|sale|buyback|repurchase))|"
                 r"^\s*better (?:[\w-]+ ){0,3}stock\b")


def routine_reason(headline: str, *, company: bool = True, skip=()) -> tuple:
    """(reason | None, cls). cls 'not_event' = never alert (market wrap, look-back,
    preview, commentary, question, listicle, plaintiff-firm advertising); cls
    'routine_event' = alert only on a held name and when clearly material (a rating
    change, a conference presentation). reason for ratings is 'rating_change' or
    'rating_note'. `skip`: 'not_event' forms to pass over, so the headline is judged by
    the rules after them (routine_blocks, held names). Never raises."""
    try:
        h = (headline or "").strip()
        if not h:
            return None, None
        ne = None
        try:
            import mason_triage
            ne = mason_triage.non_event_reason(h, company=company)
            if not ne and company and mason_triage.is_market_wrap(h):
                ne = "market_wrap"
        except Exception:
            ne = None
        if ne == "rating_unchanged":
            return "rating_note", "routine_event"
        if ne and ne not in skip:
            return ne, "not_event"
        if _RETRO_RE.search(h) and "look_back" not in skip:
            return "look_back", "not_event"
        if _PREVIEW_RE.search(h):
            return "preview", "not_event"
        if _COMMENT_RE.search(h) and "commentary" not in skip:
            return "commentary", "not_event"
        if not _CREDIT_RE.search(h):
            if _RATING_CHANGE_RE.search(h):
                return "rating_change", "routine_event"
            if _RATING_NOTE_RE.search(h) and _RATING_ACTOR_RE.search(h):
                return "rating_note", "routine_event"
        if _CONF_RE.search(h) and not _PIVOTAL_RE.search(h):
            return "conference", "routine_event"
        return None, None
    except Exception:
        return None, None


# Forms that say "someone talking about news" but often carry a real event in the same
# headline ("Nvidia's board will probe CEO ... Here's what to know", "... as major US law
# firm hired to investigate claims – business live"). On a name Nico or Mason HOLDS, when
# the headline also names a company event (its kind is not 'other'), the form alone does
# not keep the item out: it goes on to the story fold, where a repeat of a story already in
# the inbox is folded anyway. Market wraps, lists, questions and previews stay out
# everywhere. (Review fix 2026-11-15: losing a held-name event is worse than noise.)
_HELD_FORM_ONLY = {"recap", "explainer", "solicitation", "commentary", "look_back"}

# A company that hires / retains a law firm reports its own action ("... as major US law firm
# hired to investigate claims"); a law firm announcing an investigation is advertising.
_HIRED_RE = re.compile(r"\b(?:hires?|hired|hiring|retains?|retained|retaining|engag(?:es|ed|ing)|"
                       r"taps|tapped|appoints?|appointed)\b", re.I)


# (2026-11-16, leftovers 2; switch held_live_blog) A held company's own live blog is that company's
# event, not a round-up: "Nvidia earnings live updates: ..." is a recap-form label that names only the
# company, an event word and the live-blog words. Any other word keeps the label a round-up: a second
# company, "stock market", "movers", "preview", "transcript", "week ahead", a show name ("GTC"); so does
# a label that does not name the company ("Earnings live updates: JPMorgan, Goldman ...").
_OWN_EVENT_RE = re.compile(r"\b(?:earnings|results|keynote|event|(?:investor|analyst) day|"
                           r"(?:annual|special|shareholders?|stockholders?) meeting)\b", re.I)
_OWN_LABEL_WORDS = set("""earnings results keynote event investor investors analyst analysts day annual special
shareholder shareholders stockholder stockholders meeting quarter quarterly first second third fourth fiscal full
year call conference report live updates update blog coverage follow the a an and of on its s at for""".split())
# (leftovers 2 review) words of a round-up label that are also words of some company names ("Robinhood MARKETS",
# "LIVE Nation", "Wall Street ..."): a mention that is only such a word does not name the company, and as a name word
# it does not excuse itself in the rest of the label ("Markets and earnings live updates" is not Robinhood's blog).
_ROUNDUP_NAME_WORDS = set("""market markets stock stocks share shares wall street trading trade traders economy
economic futures movers mover news today tech big world global week weekly daily midday morning evening afternoon
premarket""".split()) | _OWN_LABEL_WORDS


def _own_live_label(label: str, ticker) -> bool:
    """True when `label` names `ticker`'s company and otherwise only event and live-blog words."""
    s = str(ticker or "").strip().upper()
    spans = mention_spans(s, label) if s else []
    spans = [(a, b) for a, b in spans                      # (review) the word a name match starts must be a name
             if (re.match(r"[a-z0-9]*", label[a:].lower()).group(0) or "$") not in _ROUNDUP_NAME_WORDS]
    if not spans or not _OWN_EVENT_RE.search(label):
        return False
    rest, last = "", 0
    for a, b in spans:
        rest, last = rest + label[last:a] + " ", max(last, b)
    rest += label[last:]
    titles = _titles()
    own = set(re.findall(r"[a-z0-9]+", str(titles.get(s) or titles.get(s.replace(".", "-")) or "").lower()))
    own -= _ROUNDUP_NAME_WORDS
    return all(w in _OWN_LABEL_WORDS or w in own or re.fullmatch(r"q[1-4]|fy\d{0,4}|[1-4]q\d{0,2}|\d{1,4}", w)
               for w in re.findall(r"[a-z0-9]+", rest.lower()))


def is_digest(headline: str, ticker=None) -> bool:
    """A round-up of several stories or a scheduled column, not one company's event: the
    headline's own label, the words before its first colon, is itself a recap form ("AM Need
    to Know: ...", "Stock Market Midday, Nov. 12: ...", "Premarket movers: ...", "Earnings Week
    Ahead: ...", "Meta Q3 Earnings Preview: ..."). With `ticker` (held_event_form passes the held
    name) and held_live_blog on, that company's own live blog is not a round-up ("Nvidia earnings
    live updates: ..."; see _own_live_label). Never raises."""
    try:
        t = _norm(headline).strip()
        i = t.find(":")
        if i <= 0:
            return False
        import mason_triage
        if mason_triage.non_event_reason(t[:i], company=False) != "recap":
            return False
        try:
            own = bool(ticker) and on("held_live_blog") and _own_live_label(t[:i], ticker)
        except Exception:
            own = False                 # the new check failing never lets a round-up through
        return not own
    except Exception:
        return False


def _also_question_or_list(headline: str) -> bool:
    """The headline is ALSO a question or a numbered list: forms that stay out on every
    name. mason_triage.non_event_reason stops at its first match, so an earlier 'explainer'
    or 'recap' hit hides them ("SA Asks: How much could Tesla be worth in a merger deal?").
    Same tests as non_event_reason. Never raises. (Review fix 2026-11-16.)"""
    try:
        import mason_triage as T
        t = T._clean(headline).strip()
        return bool(t.rstrip("\"'”’) ").endswith("?") or re.search(r"\?\s+[A-Z(\"“]", t)
                    or T._NE_LISTICLE_RE.search(t))
    except Exception:
        return False


def held_event_form(headline: str, ticker, reason, *, held=None) -> bool:
    """True when the commentary FORM `reason` must not keep `headline` out: it is one of
    _HELD_FORM_ONLY, `ticker` is a HELD name and the headline names a company event (its kind
    is not 'other'). With shared_form_rule on (the default) also: the headline is not a
    round-up (is_digest), not also a question or a list, and a 'solicitation' counts only when the company hired / retained
    the firm, not when a law firm advertises. The one held-name rule behind the inbox gate
    (routine_blocks), the triage alert check and the analyst desk's Breaking skip
    (non_event_blocks). Never raises."""
    try:
        if reason not in _HELD_FORM_ONLY or not ticker:
            return False
        held = held if held is not None else held_set()
        if str(ticker).upper() not in held or headline_kind(headline, has_ticker=True)[0] == "other":
            return False
        if on("shared_form_rule"):
            if is_digest(headline, ticker) or _also_question_or_list(headline):
                return False
            if reason == "solicitation" and not _HIRED_RE.search(_norm(headline)):
                return False
        return True
    except Exception:
        return False


def non_event_blocks(headline: str, ticker, *, company: bool = True, held=None):
    """mason_triage.non_event_reason(headline) for a headline about `ticker`, unless
    held_event_form excuses it: the triage alert check (mason_triage fresh()) and the analyst
    desk's Breaking skip use this, so a held name's event headline that ends "Here's what to
    know" or "- follow live" is treated the same on every news path. The rest of the routine
    rules (previews, rating notes, talks) are applied after it by routine_blocks. With
    shared_form_rule or routine_filter off: the plain non_event_reason. Never raises."""
    try:
        import mason_triage
        ne = mason_triage.non_event_reason(headline, company=company)
    except Exception:
        return None
    try:
        if ne and on("routine_filter") and on("shared_form_rule") and \
                held_event_form(headline, ticker, ne, held=held):
            return None
    except Exception:
        pass
    return ne


def routine_blocks(headline: str, ticker, materiality, *, held=None, company: bool = True):
    """The routine reason that keeps this headline out of the inbox, or None.
    A 'routine_event' passes only on a HELD name with materiality >= the bar, and a
    rating only when the rating itself changed (not a note or a target tweak). On a
    HELD name, the 'not_event' forms in _HELD_FORM_ONLY do not block when
    held_event_form() says so; with shared_form_rule on, the headline is then judged by
    the rules after the form (a preview, a rating / price-target note or a conference
    talk still counts)."""
    if not on("routine_filter"):
        return None
    reason, cls = routine_reason(headline, company=company)
    if not reason:
        return None
    if cls == "not_event" and held_event_form(headline, ticker, reason, held=held):
        if not on("shared_form_rule"):
            return None
        reason, cls = routine_reason(headline, company=company, skip=_HELD_FORM_ONLY)
        if not reason:
            return None
    if cls == "routine_event":
        held = held if held is not None else held_set()
        try:
            mat = float(materiality) if materiality is not None else 0.0
        except (TypeError, ValueError):
            mat = 0.0
        bar = float(cfg().get("routine_held_min_materiality", 0.7) or 0.7)
        if ticker and str(ticker).upper() in held and mat >= bar and reason != "rating_note":
            return None
    return reason


# ── event kind of a headline (for "same story") ─────────────────────────────────

def headline_kind(headline: str, *, has_ticker: bool = True) -> tuple:
    """(kind, family, strong) from mason_triage's deterministic rules."""
    try:
        import mason_triage as T
        k = T.classify_kind(headline, None, has_ticker=has_ticker)
        b = T.event_bounds(headline, kind=k, has_ticker=has_ticker)
        return k, T.kind_family(k), bool(b.get("strong"))
    except Exception:
        return "other", "news", False


# ── the news-event gate (news + breaking streams) ────────────────────────────────

_decision_cache: dict = {}
_DECISION_TTL = 600.0


def _parse_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except Exception:
            return []
    return []


def gate_event(ev: dict, *, stream: str = "news", now: float = None, held=None, book=None,
               check_age: bool = None) -> dict:
    """Should news event `ev` become an inbox item, and under which ticker?
    ev keys used: id, headline|title, tickers, resolved_tickers (optional), materiality,
    impact, published_at|ts, ingested_at, desc.
    Returns {"ok", "why", "ticker", "routine", "kind", "family", "strong"}. With the
    module off: {"ok": True, "ticker": first tag} (old behaviour)."""
    tks = [str(t).upper() for t in _parse_list(ev.get("tickers")) if t]
    first = tks[0] if tks else None
    res = {"ok": True, "why": None, "ticker": first, "routine": None,
           "kind": "other", "family": "news", "strong": False}
    try:
        c = cfg()
        if not c.get("enabled", True):
            return res
        head = (ev.get("headline") or ev.get("title") or "").strip()
        if not head:
            return dict(res, ok=False, why="no_headline")
        now = float(now if now is not None else time.time())
        check_age = (stream == "news") if check_age is None else check_age
        if check_age:
            mx = float(c.get("max_age_hours", 24) or 0)
            if mx > 0:
                try:
                    hs = held if held is not None else held_set()
                    if any(t in hs for t in tks):
                        mx = max(mx, float(c.get("max_age_hours_held", 72) or 0))
                except Exception:
                    pass
            ts = ev.get("published_at") or ev.get("ts") or ev.get("ingested_at")
            ing = ev.get("ingested_at")
            # the later of publish and arrival: a headline read late from a slow feed is
            # still news on arrival; one scored two days after it arrived is not
            when = max(float(ts or 0), float(ing or 0)) if (ts or ing) else None
            if mx > 0 and when and (now - when) > mx * 3600:
                return dict(res, ok=False, why="stale")
        key = (ev.get("id"), head, tuple(tks))
        hit = _decision_cache.get(key)
        if hit and (time.time() - hit[0]) < _DECISION_TTL and held is None and book is None:
            base = hit[1]
        else:
            base = _gate_static(ev, head, tks, c, held=held, book=book)
            if held is None and book is None:
                if len(_decision_cache) > 4000:
                    _decision_cache.clear()
                _decision_cache[key] = (time.time(), base)
        return dict(res, **base)
    except Exception as e:
        log.debug(f"[news-quality] gate error, letting it through: {e}")
        return res


def _gate_static(ev, head, tks, c, *, held=None, book=None) -> dict:
    out = {}
    ticker = tks[0] if tks else None
    if c.get("symbol_gate", True) and tks:
        resolved = [str(t).upper() for t in _parse_list(ev.get("resolved_tickers")) if t]
        book = book if book is not None else book_set()
        bookset = {str(b).upper() for b in book}
        held_now = held if held is not None else held_set()
        prefer = [t for t in tks if t in held_now] + [t for t in tks if t in bookset]
        subj, why = subject_ticker(head, tks, resolved=resolved, desc=ev.get("desc") or "", prefer=prefer)
        if subj is None:
            return {"ok": False, "why": why, "ticker": None}
        if subj not in tks[:1] and bookset and subj not in bookset:
            # the tag that put this in the book is not who the headline is about
            # (a broker, an investor, a partner): it is not news about the book
            return {"ok": False, "why": "subject_off_book", "ticker": subj}
        if subj not in bookset and bookset:
            in_book = [t for t in tks if t in bookset and tradable(t)]
            if in_book and subj not in in_book and any(mention_spans(t, head) for t in in_book):
                subj = in_book[0]
        ticker = subj
    out["ticker"] = ticker
    reason = routine_blocks(head, ticker, ev.get("materiality"), held=held)
    if reason:
        return {"ok": False, "why": "routine:" + reason, "ticker": ticker, "routine": reason}
    k, fam, strong = headline_kind(head, has_ticker=bool(ticker))
    out.update(kind=k, family=fam, strong=strong, ok=True, why=None)
    return out


# ── one inbox item per story ─────────────────────────────────────────────────────

_STREAM_RANK = {"news": 1, "breaking": 2, "triage": 3}
_LEAD_PREFIX_RE = re.compile(r"^(?:Now confirmed: [^.]*\.\s*|New development:\s*|This story has grown[^.]*\.\s*Latest:\s*)")


def _hl_tokens(h: str) -> set:
    stop = {"the", "and", "for", "with", "from", "that", "this", "its", "says", "said", "after", "amid",
            "over", "into", "update", "breaking", "just", "report", "inc", "corp", "shares", "stock"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", _norm(h).lower()) if t not in stop}


def near_same(a: str, b: str, thr: float = 0.5) -> bool:
    """Same headline, or one outlet's rewrite of it."""
    try:
        na, nb = " ".join(_norm(a).lower().split()), " ".join(_norm(b).lower().split())
        if not na or not nb:
            return False
        if na == nb or (len(na) > 25 and na in nb) or (len(nb) > 25 and nb in na):
            return True
        ta, tb = _hl_tokens(a), _hl_tokens(b)
        if len(ta) < 3 or len(tb) < 3:
            return False
        return len(ta & tb) / len(ta | tb) >= thr
    except Exception:
        return False


def _stems(h: str) -> set:
    return {t[:5] for t in _hl_tokens(h)}


def _shares_words(h: str, others: list, thr: float = 0.15) -> bool:
    """A follow-up piece talks about the same thing: it shares a fair part of its words
    (5-letter stems) with the story's headline or a headline already folded into it."""
    a = _stems(h)
    if not a:
        return False
    for o in others:
        b = _stems(o)
        if b and len(a & b) / len(a | b) >= thr:
            return True
    return False


def new_item(*, stream: str, ticker, headline: str, news_ids=(), catalyst_ids=(), kind="other",
             family=None, strong=False, ts: float = None, mat=None) -> dict:
    fam = family
    if fam is None:
        try:
            import mason_triage as T
            fam = T.kind_family(kind)
        except Exception:
            fam = "news"
    return {"stream": stream, "ticker": (str(ticker).upper() if ticker else None), "h": headline or "",
            "news_ids": {str(x) for x in news_ids if x}, "catalyst_ids": {str(x) for x in catalyst_ids if x},
            "kind": kind or "other", "family": fam or "news", "kinds": {kind or "other"},
            "strong": bool(strong), "ts": float(ts if ts is not None else time.time()), "mat": _mat(mat)}


def _mat(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _held_separate(ticker, held=None) -> bool:
    """A name Nico or Mason HOLDS keeps every distinct headline as its own row (Nico,
    2026-11-16: "keep held-name headlines separate, don't group them"); only true duplicates
    fold. news.quality.fold_held_names true groups them like any other name. Never raises."""
    try:
        if cfg().get("fold_held_names", False):
            return False
        hs = held if held is not None else held_set()
        return bool(ticker) and str(ticker).upper() in {str(t).upper() for t in hs}
    except Exception:
        return False


def match_story(new: dict, items: list, *, now: float = None, held=None):
    """(item, how) — the inbox item `new` belongs to, or (None, None).
    Pass 1 (same_item_hours): the same article, the same triage story, or a near-same
    headline. Pass 2 (story_fold_hours): same ticker and the same kind of event, or a
    follow-up piece with no event of its own. A STRONG event of a kind the item does
    not hold yet is news in its own right and never folds by kind.
    A HELD name (_held_separate) matches only a true duplicate: the same article
    ('same_item') or a near-same rewrite of the item's headline ('same_headline') — no
    triage-bucket match and no pass 2. `held`: the held set (replays); default held_set()."""
    try:
        c = cfg()
        if not (c.get("enabled", True) and c.get("story_fold", True)) or not new.get("ticker"):
            return None, None
        now = float(now if now is not None else time.time())
        dup_only = _held_separate(new["ticker"], held)
        same_h = float(c.get("same_item_hours", 24) or 24) * 3600
        fold_h = float(c.get("story_fold_hours", 12) or 12) * 3600
        mine = [i for i in items if i.get("ticker") == new["ticker"] and now - float(i.get("ts") or 0) <= same_h]
        mine.sort(key=lambda i: -float(i.get("ts") or 0))
        for i in mine:
            if new["news_ids"] & set(i.get("news_ids") or ()):
                return i, "same_item"
            # a triage "story" is a per-ticker, per-family bucket that can run for weeks
            # (t:NVDA:product held 850 headlines): a STRONG event of a kind the item does
            # not hold yet is not folded on the bucket alone (review fix 2026-11-15)
            kinds_i = set(i.get("kinds") or ()) | {i.get("kind") or "other"}
            if not dup_only and new["catalyst_ids"] & set(i.get("catalyst_ids") or ()) and \
                    not (new.get("strong") and new.get("kind") not in kinds_i):
                return i, "same_story"
            if near_same(new["h"], i.get("h") or ""):
                return i, "same_headline"
        if dup_only:
            return None, None
        for i in mine:
            if now - float(i.get("ts") or 0) > fold_h:
                continue
            kinds = set(i.get("kinds") or ()) | {i.get("kind") or "other"}
            if new.get("strong") and new.get("kind") not in kinds:
                continue
            if new.get("family") == "news":
                # no event of its own: a follow-up only when it talks about the same thing
                if _shares_words(new["h"], [i.get("h") or ""] + list(i.get("also_h") or [])):
                    return i, "follow_up"
                continue
            fams = (set(i.get("families") or ()) | {i.get("family") or "news"}) - {"news"}
            if new.get("family") in fams:
                return i, "same_kind"
        return None, None
    except Exception:
        return None, None


# ── inbox rows <-> story items ───────────────────────────────────────────────────

_BREAKING_RE = re.compile(r"^Breaking:\s*[^—]+—\s*")


def _stream_of(row) -> str | None:
    by = row.get("created_by")
    if by == "news_service":
        return "news"
    if by == "mason_triage":
        return "triage"
    if by == "mason" and str(row.get("title") or "").startswith("Breaking:"):
        return "breaking"
    return None


def _iso_epoch(s) -> float:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


def row_item(row: dict, conn=None) -> dict | None:
    """An inbox row as a story item (uses extra.story when the row carries it)."""
    stream = _stream_of(row)
    if not stream:
        return None
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    if not extra and row.get("extra_json"):
        try:
            extra = json.loads(row["extra_json"]) or {}
        except Exception:
            extra = {}
    st = extra.get("story") if isinstance(extra.get("story"), dict) else None
    ts = _iso_epoch(row.get("created_date"))
    if st:
        if "strong" in st:
            strong_lead = bool(st.get("strong"))
        else:                       # a row written before 2026-11-16: read its lead headline
            strong_lead = headline_kind(st.get("h") or "", has_ticker=bool(row.get("ticker")))[2]
        it = new_item(stream=stream, ticker=row.get("ticker") or st.get("ticker"), headline=st.get("h") or "",
                      news_ids=st.get("news_ids") or [], catalyst_ids=st.get("catalyst_ids") or [],
                      kind=st.get("kind") or "other", family=st.get("family"), ts=ts,
                      strong=strong_lead, mat=st.get("mat"))
        it["kinds"] |= set(st.get("kinds") or [])
        it["families"] = set(st.get("families") or []) | {it["family"]}
        for a in st.get("also") or []:
            if a.get("h"):
                it.setdefault("also_h", []).append(a["h"])
    else:
        title = str(row.get("title") or "")
        if stream == "news":
            h = title
        elif stream == "breaking":
            h = _BREAKING_RE.sub("", title)
        else:
            h = _LEAD_PREFIX_RE.sub("", (str(row.get("body") or "").split("\n")[0]))
        news_ids, cat_ids = [], []
        if row.get("ref_table") == "news_events" and row.get("ref_id"):
            news_ids.append(row["ref_id"])
        if row.get("ref_table") == "catalysts" and row.get("ref_id"):
            cat_ids.append(row["ref_id"])
        k, fam, _s = headline_kind(h, has_ticker=bool(row.get("ticker")))
        it = new_item(stream=stream, ticker=row.get("ticker"), headline=h, news_ids=news_ids,
                      catalyst_ids=cat_ids, kind=k, family=fam, ts=ts, strong=_s)
        it["families"] = {fam}
    # the item's triage stories come from its OWN lead article(s), not from headlines folded
    # under it — otherwise every fold adds a bucket and the item becomes a sink that
    # swallows the ticker's whole day (review fix 2026-11-15)
    folded_ids = set()
    if st:
        for a in st.get("also") or []:
            if not a.get("repeat_of_lead"):   # a repeat of the lead article keeps it the row's own
                folded_ids |= {str(x) for x in (a.get("news_ids") or [])}
    if conn is not None and (it["news_ids"] - folded_ids):
        try:
            ids = sorted(it["news_ids"] - folded_ids)
            q = conn.execute(f"SELECT catalyst_id, event_kind FROM news_events WHERE id IN ({','.join('?' * len(ids))})",
                             ids).fetchall()
            for r in q:
                if r[0]:
                    it["catalyst_ids"].add(r[0])
        except Exception:
            pass
    if conn is not None and stream == "triage" and it["catalyst_ids"]:
        try:
            for cid in list(it["catalyst_ids"]):
                r = conn.execute("SELECT event_kind FROM catalysts WHERE id=?", (cid,)).fetchone()
                if r and r[0]:
                    it["kinds"].add(r[0])
                    import mason_triage as T
                    it.setdefault("families", set()).add(T.kind_family(r[0]))
        except Exception:
            pass
    it["id"] = row.get("id")
    it["is_read"] = bool(row.get("is_read"))
    it["row_stream"] = stream
    return it


def inbox_items(ticker: str, *, now: float = None) -> list:
    """Live inbox items (the three news streams) for `ticker` inside same_item_hours."""
    if not ticker:
        return []
    try:
        import mason_db
        now = float(now if now is not None else time.time())
        hours = float(cfg().get("same_item_hours", 24) or 24)
        cut = datetime.fromtimestamp(now - hours * 3600).isoformat()
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE ticker = ? AND record_deleted = 0 AND created_date >= ? "
                "AND created_by IN ('news_service','mason','mason_triage') "
                "AND type IN ('huge_event','major_move','news') ORDER BY created_date DESC LIMIT 40",
                (str(ticker).upper(), cut)).fetchall()
            out = []
            for r in rows:
                it = row_item(dict(r), conn)
                if it:
                    out.append(it)
            return out
        finally:
            conn.close()
    except Exception as e:
        log.debug(f"[news-quality] inbox read failed: {e}")
        return []


def story_extra(item: dict) -> dict:
    """The extra_json 'story' block a NEW inbox row carries."""
    return {"ticker": item.get("ticker"), "h": (item.get("h") or "")[:300], "kind": item.get("kind"),
            "family": item.get("family"), "kinds": sorted(item.get("kinds") or []),
            "families": sorted(set(item.get("families") or ()) | {item.get("family") or "news"}),
            "news_ids": sorted(item.get("news_ids") or []), "catalyst_ids": sorted(item.get("catalyst_ids") or []),
            "stream": item.get("stream"), "strong": bool(item.get("strong")), "mat": item.get("mat"), "also": []}


def decide(new: dict, hit: dict, how: str) -> str:
    """What to do with `new` given the inbox item it matched ('new' when none):
      'upgrade' — replace that UNREAD item with this stronger one (a triage alert over a
                  news / Breaking item; a Breaking read of the same article over its news item;
                  a stronger headline of the story over an item of the same or a lower stream)
      'fold'    — a news / Breaking headline joins the existing item (no new row)
      'attach'  — a triage alert whose article Nico already READ: recorded on it, not re-posted
      'new'     — a row of its own (a triage alert on a different headline than the read item)"""
    if hit is None:
        return "new"
    stream = new.get("stream")
    hit_stream = hit.get("row_stream") or hit.get("stream")
    if stream == "triage":
        if not hit.get("is_read"):
            if how == "same_story" and _bucket_only_other_story(new, hit):
                return "new"           # follow-up 4: not this row's story, only its bucket
            return "upgrade"
        if how == "same_headline" and stronger(new, hit):
            return "new"               # follow-up 1: a stronger rewrite of a READ item is shown
        return "attach" if how in ("same_item", "same_headline") else "new"
    if hit.get("is_read") and how not in ("same_item", "same_headline"):
        # a READ item never swallows a different headline: Nico would never see it (the
        # fold keeps the row read and its date). It is posted, and becomes the story's
        # item for what follows (review fix 2026-11-15)
        return "new"
    if hit.get("is_read"):
        # the same article or a rewrite of a READ item is attached quietly — unless the
        # rewrite is stronger than what Nico read: then it is its own (unread) row
        return "new" if how == "same_headline" and stronger(new, hit) else "fold"
    if stream == "breaking" and how == "same_item" and \
            _STREAM_RANK.get(hit_stream, 0) < _STREAM_RANK["breaking"]:
        return "upgrade"
    if stronger(new, hit):
        # follow-up 1: the stronger headline becomes the item's title. Only a row of the same or
        # a lower stream can be taken over (a plain headline must not turn a Breaking read or a
        # Catalyst alert back into plain news): otherwise it gets its own row, unless it is the
        # same article / a rewrite, which folds into the (unread) row as before
        if _STREAM_RANK.get(stream, 0) >= _STREAM_RANK.get(hit_stream, 0):
            return "upgrade"
        return "fold" if how in ("same_item", "same_headline") else "new"
    return "fold"


def _lead_rank(kind, strong) -> int:
    """How strong a headline is as a story's title: 2 a strong event (mason_triage STRONG_KINDS
    with its floor reached), 1 any other company event, 0 no event / a rating note."""
    if strong:
        return 2
    return 0 if (kind or "other") in ("other", "analyst_action") else 1


def stronger(new: dict, hit: dict) -> bool:
    """Is `new` a stronger title for the story than the item's current lead `hit`? A higher
    rank (_lead_rank), or the same rank with a materiality at least retitle_min_mat_step
    higher (only when both are known). Never raises; False when retitle_stronger is off."""
    try:
        if not on("retitle_stronger"):
            return False
        rn, rh = _lead_rank(new.get("kind"), new.get("strong")), _lead_rank(hit.get("kind"), hit.get("strong"))
        if rn != rh:
            return rn > rh
        mn, mh = _mat(new.get("mat")), _mat(hit.get("mat"))
        if mn is None or mh is None:
            return False
        step = float(cfg().get("retitle_min_mat_step", 0.2) or 0.2)
        return mn >= mh + step - 1e-9
    except Exception:
        return False


def _bucket_only_other_story(new: dict, hit: dict) -> bool:
    """A triage alert matched `hit` only through the shared per-ticker bucket (match_story
    'same_story'). It is a DIFFERENT story when its headline shares almost none of its word
    stems with the item's lead or with any headline folded under it (below the 0.15 overlap
    a follow-up piece needs, _shares_words: "DZ Bank Downgrades Apple to Sell" vs "Investors
    Are Fleeing to Apple…"; "TSMC October revenue jumps 60%" vs "TSMC's October sales jump"
    is the same story). Never raises; False (= upgrade as before) when triage_bucket_check
    is off or on doubt."""
    try:
        if not on("triage_bucket_check"):
            return False
        if new.get("news_ids") and new["news_ids"] & set(hit.get("news_ids") or ()):
            return False
        told = [h for h in [hit.get("h")] + list(hit.get("also_h") or []) if h]
        if not told or len(_stems(new.get("h") or "")) < 3:
            return False
        return not _shares_words(new["h"], told)
    except Exception:
        return False


def _log_upgrade(notif_id, payload: dict) -> None:
    """An upgrade rewrites a row instead of calling add_notification, so the activity
    write-hook on add_notification never sees it: log the alert the same way, so a
    Breaking / Catalyst alert keeps its (decision-tier) audit row. Never raises."""
    try:
        import mason_activity as A
        ntype = payload.get("type")
        tier = getattr(A, "_NOTIF_TIER", {}).get(ntype, "work")
        if tier is None:
            return
        A.log_activity("alert_sent", f"Inbox ({ntype}): {str(payload.get('title') or '')[:160]}", actor="mason",
                       tier=tier, ticker=payload.get("ticker"), ref_table="notifications", ref_id=notif_id,
                       extra={"notification_type": ntype, "severity": payload.get("severity"),
                              "upgraded_in_place": True})
    except Exception:
        pass


def place(*, new: dict, payload: dict, notifier=None, now: float = None, url: str = "", source: str = "",
          note: str = "", merge_key: str = None) -> dict:
    """Write one inbox item for `new` (see new_item) with `payload` (the keyword
    arguments for mason_notifications.add_notification). Returns
    {"action": "new"|"folded"|"upgraded"|"attached", "id", "how"} — see decide().
    `merge_key` names the stream's switch ('breaking_merge' / 'triage_merge'); with it
    off (or story_fold off) the item is posted as before."""
    import mason_notifications as MN
    notifier = notifier or MN.add_notification
    now = float(now if now is not None else time.time())
    hit, how = None, None
    if on("story_fold") and new.get("ticker") and (merge_key is None or on(merge_key)):
        try:
            items = inbox_items(new.get("ticker"), now=now)
            if new.get("stream") == "triage":
                items = [i for i in items if i.get("row_stream") != "triage"]   # triage's own budget decides re-alerts
            hit, how = match_story(new, items, now=now)
        except Exception:
            hit, how = None, None
    action = decide(new, hit, how)
    if action != "new":
        try:
            entry = {"h": (new.get("h") or "")[:300], "src": source or "", "url": url or "",
                     "news_ids": sorted(new.get("news_ids") or []), "catalyst_ids": sorted(new.get("catalyst_ids") or []),
                     "stream": new.get("stream"), "kind": new.get("kind"), "family": new.get("family"),
                     "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "how": how}
            if note:
                entry["note"] = note[:600]
            if action == "upgrade":
                if MN.upgrade_story_item(hit["id"], payload=payload, entry=entry, story=story_extra(new)):
                    _log_upgrade(hit["id"], payload)
                    return {"action": "upgraded", "id": hit["id"], "how": how}
                # the row was read in the meantime (or is a Catalyst alert): do what a READ row
                # gets — another headline, or a stronger rewrite, stands alone; the same
                # article is attached
                if decide(new, dict(hit, is_read=True), how) == "new":
                    action = "new"
            if action != "new" and MN.attach_story_item(hit["id"], entry=entry):
                return {"action": "attached" if new.get("stream") == "triage" else "folded",
                        "id": hit["id"], "how": how}
        except Exception as e:
            log.debug(f"[news-quality] fold failed, posting normally: {e}")
    try:
        n = notifier(**payload, extra={"story": story_extra(new)})
    except TypeError:
        n = notifier(**payload)                # a notifier without the extra= keyword (tests)
    return {"action": "new", "id": (n or {}).get("id") if isinstance(n, dict) else None, "how": None}


# ── what the inbox already holds (for the UI's material-news list) ───────────────

def folded_news_ids(hours: float = 72) -> set:
    """news_ids that an inbox row covers as its story (its own ref or a folded
    'also' entry) — the Notifications page must not show them again as loose pings."""
    try:
        import mason_db
        cut = datetime.fromtimestamp(time.time() - hours * 3600).isoformat()
        conn = mason_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT j.value FROM notifications n, json_each(n.extra_json, '$.story.news_ids') j "
                "WHERE n.created_date >= ? AND n.extra_json IS NOT NULL", (cut,)).fetchall()
            out = {r[0] for r in rows if r[0]}
            rows = conn.execute(
                "SELECT a.value FROM notifications n, json_each(n.extra_json, '$.story.also') a "
                "WHERE n.created_date >= ? AND n.extra_json IS NOT NULL", (cut,)).fetchall()
            for r in rows:
                try:
                    for x in (json.loads(r[0]) or {}).get("news_ids") or []:
                        out.add(x)
                except Exception:
                    pass
            return out
        finally:
            conn.close()
    except Exception:
        return set()
