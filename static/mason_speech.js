/* ────────────────────────────────────────────────────────────────────────────
 * mason_speech.js — pure speech text-normalization for Mason's voice layer
 *
 * Extracted from templates/mason.html (2026-08-03) so these functions can be
 * exercised outside a browser. See docs/active/SPEECH_MANNERISM_DESIGN.md and
 * docs/prompts/SPEECH_INTEGRATION_WORKORDER.md.
 *
 * DUAL TARGET — the exact same file, unmodified, loads two ways:
 *   browser : <script src="/static/mason_speech.js"></script>  → attaches to window
 *   node    : require('./static/mason_speech.js')              → module.exports
 * Classic script only. No ES modules, no bundler, no dependencies — this matches
 * the global-function style already used throughout mason.html.
 *
 * ZERO DOM / ZERO Web Speech API dependencies live in this file. Anything that
 * touches SpeechSynthesisUtterance, voices, or the avatar stays in mason.html.
 * ──────────────────────────────────────────────────────────────────────────── */
(function (root) {
  'use strict';

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 1 — number → spoken form
   * ══════════════════════════════════════════════════════════════════════════ */

  // FIXED 2026-09-17 (reported: "$250" audibly split into two disjoint numbers
  // with no unit word, e.g. "2 50" — engines read that as two separate figures
  // with a dead pause between them instead of one phrase). Root cause: the old
  // $100-$999 branch manually chopped the number into a bare "hundreds tens"
  // pair of digit-tokens and never appended "dollars"; the >=1000 branches had
  // the same missing-unit problem. Fix: stop hand-splitting the number — every
  // speech engine already reads a plain integer like "759" correctly on its
  // own ("seven hundred fifty-nine") — and always end on the "dollars" unit
  // word so the phrase reads as one continuous thought.
  function _dollarSpeak(num) {
    num = Math.abs(num);
    if (num >= 1000000) {
      var m = (num / 1000000).toFixed(1).replace(/\.0$/, '');
      return m + " million dollars";
    }
    if (num >= 1000) {
      var k = Math.floor(num / 1000);
      var r = Math.round(num % 1000);
      return (r ? k + " thousand " + r : k + " thousand") + " dollars";
    }
    return Math.round(num) + " dollars";
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 2 — BUG 1: direction-word duplication ("down down $5.32")
   *
   * Root cause is structural, not incidental: _fmt_quote (mason_app.py) emits a
   * raw signed figure (-5.32%), Mason narrates direction in English on top of it
   * ("MSFT is down") because he must quote tool numbers verbatim, and then the
   * signed-number rewrite below prepends a SECOND "down". 100% reproducible.
   *
   * Fix: before prepending "up "/"down ", look at the text immediately preceding
   * THIS match and skip the word if the sentence already said it.
   * ══════════════════════════════════════════════════════════════════════════ */

  // Anchored to the END of the preceding text.
  //   \b(up|down)\b  — word boundaries both sides, so "downgraded", "upside" and
  //                    "sundown" do NOT count as a direction word.
  //   optional filler — catches "is down by -$5.32", "up about +3%".
  //   trailing junk   — whitespace / comma / colon / dash between word and number.
  var _DIR_TAIL_RE = /\b(up|down)\b(?:\s+(?:by|about|around|roughly|approximately|nearly|some|another|a|an))?[\s,:;—–-]*$/i;

  /**
   * @param {string} before text immediately preceding the signed-number match
   * @returns {boolean} true → the direction word is ALREADY there, do not re-add
   */
  function _stripDuplicateDirectionWord(before) {
    if (!before) return false;                 // offset 0 → nothing precedes → keep word
    return _DIR_TAIL_RE.test(before);
  }

  // One shared prefix resolver so all four signed regexes behave identically.
  // `whole.slice(0, off)` scopes the check to THIS match's position — two signed
  // numbers in one sentence are judged independently, no leaking across.
  function _dirPrefix(word, off, whole) {
    return _stripDuplicateDirectionWord(String(whole || '').slice(0, off)) ? "" : (word + " ");
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 3 — BUG 2: bare all-caps tickers spelled out letter-by-letter
   *
   * The existing parenthetical-repeat suppression in mason.html only fires for a
   * ticker that appeared as "(RKLB)" at least once. A ticker Mason mentions bare
   * and never parenthesizes reaches the TTS engine as a raw vowel-less uppercase
   * string, which most OS voices spell out. This table catches those.
   *
   * Source list = mason_scanner_universe.json "tickers" (68, read from the file)
   * ∪ tickers found in options_journal.json + trades_journal.json.
   *
   * DELIBERATELY OMITTED — see docs/active/SPEECH_MANNERISM_DESIGN.md §4:
   *  Rule A, letters are already the natural pronunciation, expanding gains
   *  nothing: AMD, IBM, TSM, RTX, GE, ASML, SPY, QQQ, XLE, XLV, CVS.
   *  Rule B, company name not confirmed — logged, not guessed: NANC, QTUM, USO.
   *  Rule C, see TICKER_BLOCKLIST below.
   * ══════════════════════════════════════════════════════════════════════════ */

  var TICKER_NAMES = {
    AAPL: "Apple",
    ABNB: "Airbnb",
    ADBE: "Adobe",
    AMAT: "Applied Materials",
    AMGN: "Amgen",
    AMZN: "Amazon",
    ANET: "Arista Networks",
    ARM:  "Arm",
    AVGO: "Broadcom",
    BA:   "Boeing",
    COIN: "Coinbase",
    CRM:  "Salesforce",
    CRWD: "CrowdStrike",
    DDOG: "Datadog",
    DE:   "Deere",
    DIS:  "Disney",
    GILD: "Gilead",
    GOOGL: "Google",
    INTU: "Intuit",
    IONQ: "Ion Q",
    ISRG: "Intuitive Surgical",
    KLAC: "KLA",
    LLY:  "Eli Lilly",
    LMT:  "Lockheed Martin",
    LRCX: "Lam Research",
    LULU: "Lululemon",
    MDB:  "MongoDB",
    MELI: "MercadoLibre",
    META: "Meta",
    MRNA: "Moderna",
    MRVL: "Marvell",
    MSFT: "Microsoft",
    MU:   "Micron",
    NFLX: "Netflix",
    NKE:  "Nike",
    NVDA: "Nvidia",
    ORCL: "Oracle",
    PANW: "Palo Alto Networks",
    PEP:  "PepsiCo",
    PLTR: "Palantir",
    PYPL: "PayPal",
    QCOM: "Qualcomm",
    REGN: "Regeneron",
    RGTI: "Rigetti",
    RKLB: "Rocket Lab",
    ROKU: "Roku",
    SBUX: "Starbucks",
    SMCI: "Supermicro",
    SNOW: "Snowflake",
    SOFI: "SoFi",
    TSLA: "Tesla",
    TXN:  "Texas Instruments",
    UBER: "Uber",
    UNH:  "UnitedHealth",
    VRT:  "Vertiv",
    VRTX: "Vertex Pharmaceuticals",
    ZS:   "Zscaler"
  };

  // Rule C — real English words / trading jargon that also happen to be tickers.
  //
  // The key insight that makes blocking these FREE: the bug being fixed is TTS
  // spelling out UNPRONOUNCEABLE strings (R-K-L-B, C-R-W-D, M-S-F-T). Every token
  // below is already a pronounceable English word, so the engine reads it as a word
  // either way — expanding it buys no speech-quality improvement, it only creates
  // false-positive risk ("net exposure" → "Cloudflare exposure"). Blocking is
  // strictly the better trade here.
  var TICKER_BLOCKLIST = {
    NOW:  1,   // ServiceNow — also an adverb; every glitch aside is ALL-CAPS
    NET:  1,   // Cloudflare — Mason says "net delta" / "net exposure" constantly
    CAT:  1,   // Caterpillar — common noun
    SPOT: 1,   // Spotify — "SPOT price" is real jargon
    COST: 1,   // Costco — common noun
    HOOD: 1,   // Robinhood — common noun
    SHOP: 1,   // Shopify — common noun/verb
    TEAM: 1    // Atlassian — common noun
  };

  // Hard never-touch set, independent of the table above. Belt-and-braces: none of
  // these are currently keys in TICKER_NAMES, and this keeps it that way if the
  // table ever grows. NOTE "0DTE" is not [A-Z]{2,5}-shaped (leading digit) so the
  // token regex never even sees it.
  var PROTECTED_ACRONYMS = {
    IV:1, AI:1, RSI:1, ATM:1, ETF:1, DTE:1, OTM:1, ITM:1, EPS:1, PE:1, ROE:1,
    SMA:1, EMA:1, MACD:1, VWAP:1, POP:1, OI:1, EOD:1, YTD:1, FOMC:1, CPI:1,
    PPI:1, GDP:1, PMI:1, SEC:1, IPO:1, ATH:1, ATR:1, MA:1, VCP:1, PEAD:1,
    SSR:1, ORL:1, USD:1, CEO:1, CFO:1, API:1
  };

  // Case-SENSITIVE by design: [A-Z] with no /i flag, so "Msft", "msft" and "Cat"
  // are never touched. Start-of-sentence and ALL-CAPS-header positions match the
  // same way as mid-sentence — \b is position-based, the class does the filtering.
  var _TICKER_TOKEN_RE = /\b[A-Z]{2,5}\b/g;

  // Splits text into bracket / non-bracket spans. The trailing "\]?" tolerates a
  // truncated aside (opening "[" with no closer, e.g. a stream cut short).
  var _BRACKET_SPAN_RE = /(\[[^\]]*\]?)/;

  /**
   * Replace bare known tickers with their company name for speech.
   * Runs ONLY on non-bracket spans: Mason's glitch asides are stylistically
   * ALL-CAPS ("[MASON IS PULLING RKLB NOW]"), which is the single biggest
   * false-positive surface in this feature. Excluding them wholesale kills the
   * "NOW" → "ServiceNow" class of collision structurally.
   */
  function _expandTickers(text) {
    if (!text) return text;
    var spans = String(text).split(_BRACKET_SPAN_RE);
    for (var i = 0; i < spans.length; i++) {
      var s = spans[i];
      if (!s) continue;
      if (s.charAt(0) === '[') continue;                 // bracket aside — leave alone
      spans[i] = s.replace(_TICKER_TOKEN_RE, function (tok) {
        if (Object.prototype.hasOwnProperty.call(PROTECTED_ACRONYMS, tok)) return tok;
        if (Object.prototype.hasOwnProperty.call(TICKER_BLOCKLIST, tok))   return tok;
        if (Object.prototype.hasOwnProperty.call(TICKER_NAMES, tok))       return TICKER_NAMES[tok];
        return tok;                                       // unknown → unchanged
      });
    }
    return spans.join('');
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 4 — the main normalizer
   * Moved from mason.html:2306-2351. Ticker/paren logic preserved verbatim;
   * the four signed regexes gained the Bug 1 preceding-context check; ticker
   * expansion (Bug 2) added after the existing paren suppression.
   * ══════════════════════════════════════════════════════════════════════════ */

  function _humanizeForSpeech(text) {
    if (text === null || text === undefined) return text;
    text = String(text);

    // ── Visual-only content the voice layer must NOT read aloud ──────────────
    // Trailing "Sources:" block (with its links) — matches at text start or after
    // a newline so it also catches a streamed tail chunk beginning with "Sources:".
    text = text.replace(/(?:^|\n)\s*sources?\s*:[\s\S]*$/i, '');
    // Any raw URL anywhere — reads aloud as gibberish.
    text = text.replace(/\bhttps?:\/\/\S+/gi, '');
    text = text.replace(/\b(?:www\.)[^\s)]+/gi, '');

    // Belt-and-braces (2026-09-17, reported: words near apostrophes/parens/
    // brackets read as "asterisk, asterisk"). qwen3.5:4b does not reliably
    // avoid markdown even though the system prompt forbids it (see
    // _cleanMarkdown's 2026-09-04 comment in mason_engine_v2.js — the visual
    // side hit the same root cause). Whatever "*" survives whatever markdown-
    // stripping the caller already ran (_stripMd), it must never reach the TTS
    // engine: most voices speak a literal "*" aloud as the word "asterisk".
    text = text.replace(/\*/g, '');

    // Ticker handling (UNCHANGED — this block is correct for what it does).
    // Note every ticker introduced as "(RKLB)" in this reply, then strip the
    // parenthetical AND any bare repeats of those SAME tickers — so TTS says the
    // company name (spoken on first mention) instead of spelling out "C-R-M"
    // later. Only tokens that actually appeared parenthesized are touched.
    var _seenTk = [], _tkRe = /\(([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\)/g, _tkM;
    while ((_tkM = _tkRe.exec(text)) !== null) { if (_seenTk.indexOf(_tkM[1]) < 0) _seenTk.push(_tkM[1]); }
    text = text.replace(/\s*\(([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\)/g, '');   // drop the annotation
    for (var _ti = 0; _ti < _seenTk.length; _ti++) {
      var _tkEsc = _seenTk[_ti].replace(/\./g, '\\.');
      text = text.replace(new RegExp('\\b' + _tkEsc + '\\b', 'g'), '');  // silence bare repeats
    }

    // Any OTHER parenthetical the ticker-only match above didn't consume — a
    // date, a clarification, a plain aside — still has its literal "(" / ")".
    // Most voices either read those aloud verbatim or garble them (reported:
    // "yesterday's (Sept 16)" style asides). Rewrite as a comma-set aside
    // instead of leaving raw punctuation for the engine to guess at.
    // Non-nested by design: Mason's asides are simple, single-level parens.
    text = text.replace(/\s*\(([^()]+)\)/g, ', $1,');
    text = text.replace(/,\s*,/g, ',');    // collapse a comma this created next to an existing one
    text = text.replace(/,(\s*[.!?])/g, '$1');  // don't leave a dangling ", ." before end punctuation

    // BUG 2 — anything the paren logic above did NOT already handle: expand bare
    // known tickers to company names. Runs after, so a ticker already silenced
    // above is simply gone and cannot be double-substituted.
    text = _expandTickers(text);

    // Collapse any double spaces left behind by the strips above.
    text = text.replace(/[ \t]{2,}/g, ' ');

    // Sign belongs BEFORE the $, always: normalize a mis-ordered "$-60" / "$+60"
    // (the model and some legacy formatters still emit these) to "-$60" / "+$60"
    // so the signed-amount handlers below actually match them instead of leaving
    // a literal "$ minus 60" for the engine to mangle.
    text = text.replace(/\$\s*-/g, '-$').replace(/\$\s*\+/g, '+$');

    // ── Signed dollar amounts ───────────────────────────────────────────────
    // +$101 → "up a hundred dollars" (a MOVEMENT).
    // -$50  → "down fifty dollars" when Mason's prose already framed it as a move
    //         ("the position is down -$50"); otherwise it's a FLAT figure Nico
    //         asked for (a balance, a P&L number) and should read "negative fifty
    //         dollars" (Nico's call, 2026-09-21). The duplicate-direction detector
    //         IS the movement/flat signal: a preceding "up/down" ⇒ movement.
    text = text.replace(/\+\$([0-9,]+(?:\.[0-9]+)?)/g, function (m, n, off, whole) {
      return _dirPrefix("up", off, whole) + _dollarSpeak(parseFloat(n.replace(/,/g, '')));
    });
    text = text.replace(/-\$([0-9,]+(?:\.[0-9]+)?)/g, function (m, n, off, whole) {
      var spoken = _dollarSpeak(parseFloat(n.replace(/,/g, '')));
      // Movement: prose already carries "up/down", so just speak the amount.
      // Flat figure: say "negative" outright.
      return _stripDuplicateDirectionWord(String(whole || '').slice(0, off))
        ? spoken
        : ("negative " + spoken);
    });
    // Bare dollar amounts: $436.60 → "four thirty-six"
    text = text.replace(/\$([0-9,]+(?:\.[0-9]+)?)/g, function (m, n) {
      return _dollarSpeak(parseFloat(n.replace(/,/g, '')));
    });
    // ── Signed percentages: +13.36% → "up thirteen percent" ─────────────────
    text = text.replace(/\+([0-9]+(?:\.[0-9]+)?)%/g, function (m, n, off, whole) {
      return _dirPrefix("up", off, whole) + Math.round(parseFloat(n)) + " percent";
    });
    text = text.replace(/-([0-9]+(?:\.[0-9]+)?)%/g, function (m, n, off, whole) {
      return _dirPrefix("down", off, whole) + Math.round(parseFloat(n)) + " percent";
    });
    // Bare percentages: 13.36% → "thirteen percent"
    text = text.replace(/([0-9]+(?:\.[0-9]+)?)%/g, function (m, n) {
      return Math.round(parseFloat(n)) + " percent";
    });
    return text;
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 5 — BUG 3: utterance segmentation + coalescing
   *
   * mason.html previously built one SpeechSynthesisUtterance per sentence AND per
   * bracket transition, in two near-duplicate code paths (_ssBuild :2374-2402 and
   * speakOrdis :2506-2543). Every extra utterance is another speechSynthesis queue
   * entry with its own unreliable onstart latency — and the avatar's glitch state
   * only starts on onstart, so each boundary is another chance to drift.
   *
   * This returns PLAIN OBJECTS (no SpeechSynthesisUtterance) so it is testable in
   * Node. mason.html turns them into utterances.
   * ══════════════════════════════════════════════════════════════════════════ */

  // Unbounded merging would build multi-thousand-character utterances, which hit
  // Chrome's long-utterance stall — the failure the _startSynthKA keepalive at
  // mason.html:863-878 already exists to paper over. 300 chars still merges 2-4
  // typical sentences, which covers the bracket-aside case this bug is about.
  var DEFAULT_MAX_MERGE_CHARS = 300;

  function _speechSegments(text, opts) {
    var out = [];
    if (text === null || text === undefined) return out;
    text = String(text);
    if (!text.trim()) return out;

    var maxMerge = (opts && opts.maxMergeChars) || DEFAULT_MAX_MERGE_CHARS;

    // Protect punctuation inside [...] from the sentence splitter, otherwise
    // "[MASON NOTES. ANOTHER POINT.]" gets cut mid-bracket and isBracket fails.
    // (Same guard as mason.html:2491-2496 — see that comment, it is load-bearing.)
    var safe = text.replace(/\[([^\]]*)\]/g, function (m, inner) {
      return '[' + inner.replace(/\./g, '·').replace(/!/g, '¡').replace(/\?/g, '¿') + ']';
    });
    // Same protection for a TRUNCATED trailing aside — an opening "[" with no
    // closer, e.g. the stream died mid-bracket. Without this its internal periods
    // survive into the sentence splitter and the aside gets chopped in half, with
    // the tail read in the normal voice instead of the glitch voice.
    safe = safe.replace(/\[([^\]]*)$/, function (m, inner) {
      return '[' + inner.replace(/\./g, '·').replace(/!/g, '¡').replace(/\?/g, '¿');
    });
    var sents = safe.split(/(?<=[.!?])\s+/).filter(function (s) { return s.trim(); });
    if (!sents.length) sents = [safe];
    sents = sents.map(function (s) {
      return s.replace(/·/g, '.').replace(/¡/g, '!').replace(/¿/g, '?');
    });

    // Pass 1 — flat segment list (one entry per sentence-part), as today.
    var flat = [];
    for (var si = 0; si < sents.length; si++) {
      // "\]?" (vs the old "\]") so a truncated aside is still recognised as a
      // bracket span instead of falling through as normal text with a literal "[".
      var parts = sents[si].split(/(\[[^\]]*\]?)/);
      for (var pi = 0; pi < parts.length; pi++) {
        var seg = parts[pi].trim();
        if (!seg) continue;
        var opens = seg.charAt(0) === '[';
        // A segment that opens a bracket and never closes it is a TRUNCATED aside
        // (stream cut short). Treat it as a bracket rather than reading a literal
        // "[" in the normal voice. Graceful degradation, never a throw.
        var isB = opens && (seg.charAt(seg.length - 1) === ']' || seg.indexOf(']') < 0);
        var raw = seg.replace(/^\[/, '').replace(/\]$/, '');
        // Strip any STRAY bracket characters left over — an unmatched "]" with no
        // opener, or the inner brackets of nested-looking input. Real bracket spans
        // were already split out above, so anything surviving here is unmatched and
        // would otherwise be handed to the TTS engine as a literal character.
        // (Found in Phase 5 adversarial testing: input "A] B" leaked its "]".)
        raw = raw.replace(/[\[\]]/g, ' ').replace(/\s{2,}/g, ' ').trim();
        if (!raw) continue;
        flat.push({ isBracket: isB, text: raw });
      }
    }

    // Pass 2 — COALESCE adjacent same-type segments. Each bracket/non-bracket
    // TRANSITION still yields exactly one boundary; the type change is never
    // merged across. This is the actual Bug 3 fix.
    for (var i = 0; i < flat.length; i++) {
      var cur = flat[i];
      var prev = out.length ? out[out.length - 1] : null;
      if (prev && prev.isBracket === cur.isBracket &&
          (prev.text.length + 1 + cur.text.length) <= maxMerge) {
        prev.text = prev.text + ' ' + cur.text;
      } else {
        out.push({ isBracket: cur.isBracket, text: cur.text });
      }
    }
    return out;
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * SECTION 6 — Item 8: speak() → onstart latency instrumentation
   * There was no measurement anywhere, so "the glitch takes ~3 seconds" was
   * anecdotal. These two helpers make it a number.
   * Both are fully defensive: instrumentation must never break the speech chain.
   * ══════════════════════════════════════════════════════════════════════════ */

  function _perfNow() {
    try {
      if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
        return performance.now();
      }
    } catch (e) {}
    return Date.now();
  }

  /** Stamp queue time. Call immediately before speechSynthesis.speak(u). */
  function _speechLatencyMark(u, label) {
    try {
      if (!u) return;
      u.__mspQueuedAt = _perfNow();
      if (label) u.__mspLabel = label;
    } catch (e) {}
  }

  /** Compute + log queue→onstart delta. Call from inside u.onstart. */
  function _speechLatencyReport(u) {
    try {
      if (!u || typeof u.__mspQueuedAt !== 'number') return null;
      var delta = _perfNow() - u.__mspQueuedAt;
      var kind = u.__mspLabel || 'utt';
      var head = String(u.text || '').slice(0, 40);
      if (typeof console !== 'undefined' && console && console.log) {
        console.log('[speech-latency] ' + kind + ' queue→onstart ' +
                    delta.toFixed(1) + 'ms  "' + head + '"');
      }
      return delta;
    } catch (e) { return null; }
  }

  /* ══════════════════════════════════════════════════════════════════════════
   * Exports — identical file works in both targets
   * ══════════════════════════════════════════════════════════════════════════ */

  var API = {
    _dollarSpeak: _dollarSpeak,
    _stripDuplicateDirectionWord: _stripDuplicateDirectionWord,
    _expandTickers: _expandTickers,
    _humanizeForSpeech: _humanizeForSpeech,
    _speechSegments: _speechSegments,
    _speechLatencyMark: _speechLatencyMark,
    _speechLatencyReport: _speechLatencyReport,
    TICKER_NAMES: TICKER_NAMES,
    TICKER_BLOCKLIST: TICKER_BLOCKLIST,
    PROTECTED_ACRONYMS: PROTECTED_ACRONYMS,
    DEFAULT_MAX_MERGE_CHARS: DEFAULT_MAX_MERGE_CHARS
  };

  // Browser: attach each symbol as a bare global, matching mason.html's existing
  // style (functions there are declared at top level and called unqualified).
  if (root) {
    for (var k in API) {
      if (Object.prototype.hasOwnProperty.call(API, k)) root[k] = API[k];
    }
    root.MasonSpeech = API;
  }
  // Node: same object, for require().
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = API;
  }

})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null));
