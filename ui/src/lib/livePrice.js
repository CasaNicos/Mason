// MasonUI.livePrice — the live last-price line on every MasonChart (bars stay as they are, ~15 min behind).
//
// Built from scratch: React Bits has no chart overlay / price-line piece (its Counter and text animations are
// React components for headline numbers, and this sits inside the vanilla canvas engine in templates/mason_v2.html).
// The overlay is four small DOM nodes above the canvas (line, dot, price tag, "as of" label), moved with one CSS
// transform; the pulse is a CSS animation. A price tick therefore costs no canvas redraw unless the y-range has to grow.
//
//   start({pollS, url?})                 once, by the template, only when ui.live_price_overlay.enabled is on
//   attach({id, wrap, canvas, getSymbol(), redraw()}) → handle | null (not started)
//     handle.sync()                      the chart's symbol / kind changed
//     handle.range(mn, mx, atEdge)       → {mn, mx}: the y-range to draw with (grown only when the price left it)
//     handle.place({yFor, plotW, plotH, top, mn, mx, cw})   end of every draw(): put the overlay where the price is
//     handle.hide()                      nothing to show (no bars)
//     handle.detach()
//
// One GET /api/live_prices?symbols=… per tick for all charts that are on screen (IntersectionObserver per chart,
// which also covers hidden pages and collapsed widgets). No visible chart or a hidden tab = no timer, no request.
// Contract of the route: mason_live_price.py docstring.

export const QUIET_S = 900; // market open but no trade for this long: shown grey with "as of"
// Review 2026-11-19: the backup source is ONE exchange (IEX, a small share of the volume): no IEX trade for 2 minutes
// says nothing about the real market, so that price turns grey much sooner, and it always carries a visible "IEX".
export const IEX_QUIET_S = 120;
export const CLOSED_POLL_S = 60; // while the market is closed the price does not move; look once a minute for the open
export const CLOSED_TRUST_S = 300;
export const MIN_REQUEST_GAP_MS = 1000;
const UP = '▲';
const DOWN = '▼';
const FLAT = '–';

export const staleAfterS = pollS => Math.max(20, 4 * pollS);

/** Up / down against the previous close. Never colour alone: every direction has its own glyph. */
export function direction(price, prevClose) {
  if (!Number.isFinite(price) || !Number.isFinite(prevClose) || prevClose <= 0) return 'flat';
  // the same to the cent (as shown) is unchanged: never an arrow next to "0.00%"
  if (fmtPrice(price) === fmtPrice(prevClose)) return 'flat';
  return price > prevClose ? 'up' : 'down';
}

/**
 * What to show for one quote. q = a quote of /api/live_prices (or null), market = its "market" object,
 * elapsedS = seconds since that answer arrived in the browser.
 * → {mode: 'live' | 'stale' | 'closed' | 'none', dir, why: null | 'source' | 'quiet', priceAgeS, answerAgeS}
 */
export function classify(q, market, elapsedS, pollS) {
  if (!q || !Number.isFinite(q.price)) return { mode: 'none', dir: 'flat', why: null, priceAgeS: null, answerAgeS: null };
  const el = Math.max(0, elapsedS || 0);
  const answerAgeS = (Number(q.answer_age_s) || 0) + el;
  const priceAgeS = (Number(q.price_age_s) || 0) + el;
  const dir = direction(q.price, q.prev_close);
  const base = { dir, priceAgeS, answerAgeS };
  // Closed: the price does not move and Mason looks only once a minute, so an older answer is normal. One that is
  // older than a few of those looks is not trusted for "open / closed" either.
  if (market && market.open === false) return answerAgeS > CLOSED_TRUST_S ? { ...base, mode: 'stale', why: 'source' } : { ...base, mode: 'closed', why: null };
  if (answerAgeS > staleAfterS(pollS)) return { ...base, mode: 'stale', why: 'source' };
  if (!market || market.open !== true) return { ...base, mode: 'stale', why: 'source' };
  if (priceAgeS > (q.source === 'alpaca_iex' ? IEX_QUIET_S : QUIET_S)) return { ...base, mode: 'stale', why: 'quiet' };
  return { ...base, mode: 'live', why: null };
}

/**
 * The y-range to draw with. Bars decide it; it grows only when the live price is outside, and a grown edge is then
 * kept (no jitter) until the bars cover the price again.
 * ext = the previous result's ext (or null). atEdge = the newest bar is in view.
 * → {mn, mx, ext: {lo, hi} | null, pin: 0 | 1 | -1}   pin: the price is above (1) / below (-1) and the range was NOT grown
 */
export function fitRange(mn, mx, price, ext, atEdge) {
  const span = mx - mn;
  if (!Number.isFinite(price) || !(span > 0) || (price >= mn && price <= mx)) return { mn, mx, ext: null, pin: 0 };
  const above = price > mx;
  const dist = above ? price - mx : mn - price;
  // scrolled back in history, or a price absurdly far away (bad tick, split): never squash the bars for it
  if (!atEdge || dist > 4 * span) return { mn, mx, ext: null, pin: above ? 1 : -1 };
  const pad = span * 0.08; // room above a new edge
  const slack = span * 0.4; // a kept edge may be this far from the price before it is pulled back in
  const next = { lo: null, hi: null };
  if (above) {
    const keep = ext && Number.isFinite(ext.hi) && ext.hi >= price && ext.hi <= price + slack && ext.hi > mx;
    next.hi = keep ? ext.hi : price + pad;
    return { mn, mx: next.hi, ext: next, pin: 0 };
  }
  const keep = ext && Number.isFinite(ext.lo) && ext.lo <= price && ext.lo >= price - slack && ext.lo < mn;
  next.lo = keep ? ext.lo : price - pad;
  return { mn: next.lo, mx, ext: next, pin: 0 };
}

export function fmtPrice(p) {
  if (!Number.isFinite(p)) return '';
  return p.toFixed(p < 10 ? 3 : 2);
}
const SOURCE_NAME = { robinhood: 'Robinhood (real-time)', alpaca_iex: 'Alpaca (IEX exchange only)' };
function clock(ms, withSec) {
  const o = { hour: 'numeric', minute: '2-digit' };
  if (withSec) o.second = '2-digit';
  return new Date(ms).toLocaleTimeString('en-US', o);
}
/** "14:32" style label for the grey state: the time of the price, with the day when it is not today's. */
export function asOfLabel(tsMs, nowMs) {
  if (!Number.isFinite(tsMs)) return '';
  const d = new Date(tsMs);
  const n = new Date(nowMs);
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return sameDay ? `as of ${hm}` : `as of ${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} ${hm}`;
}
function ago(s) {
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}
export function tagText(q, st) {
  const g = st.dir === 'up' ? UP : st.dir === 'down' ? DOWN : FLAT;
  return `${g}${fmtPrice(q.price)}`;
}
/** Plain sentence for the hover text / screen readers: the price, where it came from and when. */
export function hoverText(symbol, q, st, nowMs) {
  const ts = Date.parse(q.ts);
  const parts = [`${symbol} ${fmtPrice(q.price)}`];
  if (Number.isFinite(q.prev_close) && q.prev_close > 0) {
    const pct = ((q.price - q.prev_close) / q.prev_close) * 100;
    const word = st.dir === 'up' ? 'up' : st.dir === 'down' ? 'down' : 'unchanged';
    parts[0] += st.dir === 'flat' ? `, unchanged from the previous close (${fmtPrice(q.prev_close)})` : `, ${word} ${Math.abs(pct).toFixed(2)}% from the previous close (${fmtPrice(q.prev_close)})`;
  } else parts[0] += ' (previous close not known)';
  const src = SOURCE_NAME[q.source] || String(q.source || 'unknown source');
  let when = 'time not known';
  if (Number.isFinite(ts)) {
    const day = asOfLabel(ts, nowMs).replace(/^as of /, '').replace(/\s?\d\d:\d\d$/, '');
    when = `${day ? `${day}, ` : ''}${clock(ts, true)}`;
  }
  parts[0] += '.';
  parts.push(`Source: ${src}. Last trade ${when}${q.session === 'extended' ? ', outside regular hours' : ''}.`);
  if (st.mode === 'live') parts.push(`Live: checked ${ago(st.answerAgeS)} ago.`);
  else if (st.mode === 'closed') parts.push('Market closed: this is the last price.');
  else if (st.why === 'source') parts.push(`Not live: no fresh answer for ${ago(st.answerAgeS)}.`);
  else parts.push(`Not live: no ${q.source === 'alpaca_iex' ? 'IEX ' : ''}trade for ${ago(st.priceAgeS)}.`);
  return parts.join(' ');
}

// ── the manager (one per page) ───────────────────────────────────────────────────────────────────
const defaults = () => ({
  fetchJson: url => fetch(url, { headers: { Accept: 'application/json' } }).then(r => r.json()),
  mono: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
  wall: () => Date.now(),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: t => clearTimeout(t),
  isHidden: () => typeof document !== 'undefined' && !!document.hidden,
  onVisibility: fn => {
    if (typeof document === 'undefined') return () => {};
    document.addEventListener('visibilitychange', fn);
    return () => document.removeEventListener('visibilitychange', fn);
  },
  observe: (el, cb) => {
    if (typeof IntersectionObserver === 'undefined') {
      cb(true);
      return () => {};
    }
    const io = new IntersectionObserver(es => es.forEach(e => cb(e.isIntersecting)));
    io.observe(el);
    return () => io.disconnect();
  }
});

const SYM_OK = /^[A-Z]{1,5}(?:\.[A-Z])?$/;

export function createManager(options = {}) {
  const env = { ...defaults(), ...options };
  const pollS = Math.max(2, Math.min(60, Number(options.pollS) || 5));
  const url = options.url || '/api/live_prices';
  const charts = new Map(); // id → chart
  const quotes = new Map(); // symbol → {q, at (mono ms)}
  let market = null;
  let timer = null;
  let timerAt = 0;
  let inflight = false;
  let lastReq = -Infinity;
  let fails = 0;
  let off = false; // the server said the switch is off: stop for good
  const stats = { requests: 0, lastUrl: null };

  const visibleSymbols = () => {
    const s = new Set();
    charts.forEach(c => {
      if (c.visible && c.symbol) s.add(c.symbol);
    });
    return [...s].sort();
  };
  const wanted = () => !off && !env.isHidden() && visibleSymbols().length > 0;

  function schedule(ms) {
    if (timer != null) env.clearTimer(timer);
    timer = null;
    if (!wanted()) return;
    const wait = Math.max(ms, MIN_REQUEST_GAP_MS - (env.mono() - lastReq), 0);
    timerAt = env.mono() + wait;
    timer = env.setTimer(tick, wait);
  }
  /** Something became visible / changed symbol: ask soon instead of waiting out the interval. */
  function kick() {
    if (!wanted()) {
      if (timer != null) env.clearTimer(timer);
      timer = null;
      return;
    }
    if (inflight) return;
    if (timer == null || timerAt - env.mono() > 200) schedule(150);
  }
  function nextDelay() {
    if (fails) return Math.min(60, pollS * 2 ** Math.min(fails, 4)) * 1000;
    return (market && market.open === false ? Math.max(pollS, CLOSED_POLL_S) : pollS) * 1000;
  }
  function tick() {
    timer = null;
    if (!wanted() || inflight) return;
    const syms = visibleSymbols();
    inflight = true;
    lastReq = env.mono();
    stats.requests += 1;
    stats.lastUrl = `${url}?symbols=${syms.map(encodeURIComponent).join(',')}`;
    let p;
    try {
      p = Promise.resolve(env.fetchJson(stats.lastUrl));
    } catch (e) {
      p = Promise.reject(e);
    }
    p.then(
      j => {
        inflight = false;
        if (j && j.enabled === false) {
          off = true;
          quotes.clear();
          charts.forEach(c => c.refresh(true));
          return;
        }
        if (!j || j.ok !== true || !j.quotes) {
          fails += 1;
        } else {
          fails = 0;
          market = j.market || null;
          const at = env.mono();
          Object.keys(j.quotes).forEach(s => {
            const q = j.quotes[s];
            if (q && Number.isFinite(q.price)) quotes.set(s, { q, at });
            else quotes.delete(s); // the server gave up on it (no answer for 15 minutes): show nothing, not an old price
          });
        }
        charts.forEach(c => c.refresh(true));
        schedule(nextDelay());
      },
      () => {
        inflight = false;
        fails += 1;
        charts.forEach(c => c.refresh(true)); // ages keep growing: a live line turns grey by itself
        schedule(nextDelay());
      }
    );
  }
  const offVis = env.onVisibility(() => {
    if (env.isHidden()) {
      if (timer != null) env.clearTimer(timer);
      timer = null;
    } else {
      charts.forEach(c => c.refresh(false)); // ages moved on while hidden: grey first, then ask
      kick();
    }
  });

  function stateFor(symbol) {
    const e = symbol ? quotes.get(symbol) : null;
    if (!e) return null;
    const st = classify(e.q, market, (env.mono() - e.at) / 1000, pollS);
    return st.mode === 'none' ? null : { q: e.q, st };
  }

  function attach(spec) {
    if (!spec || !spec.wrap || !spec.id) return null;
    if (charts.has(spec.id)) charts.get(spec.id).detach();
    const doc = spec.wrap.ownerDocument;
    const mk = (tag, cls) => {
      const n = doc.createElement(tag);
      n.className = cls;
      return n;
    };
    const root = mk('div', 'mlp');
    root.hidden = true;
    const line = mk('i', 'mlp-line');
    const dot = mk('i', 'mlp-dot');
    dot.appendChild(mk('i', 'mlp-ping'));
    const age = mk('span', 'mlp-age');
    const tag = mk('span', 'mlp-tag');
    tag.tabIndex = 0;
    tag.setAttribute('role', 'img');
    [line, dot, age].forEach(n => n.setAttribute('aria-hidden', 'true'));
    root.append(line, dot, age, tag);
    spec.wrap.appendChild(root);

    const c = { id: spec.id, symbol: null, visible: false, ext: null, geo: null, shown: null };
    const readSymbol = () => {
      let s = null;
      try {
        s = spec.getSymbol();
      } catch {
        s = null;
      }
      s = typeof s === 'string' ? s.trim().toUpperCase() : '';
      return SYM_OK.test(s) ? s : null;
    };
    const set = (node, key, val) => {
      if (node[key] !== val) node[key] = val;
    };
    function hide() {
      set(root, 'hidden', true);
      c.shown = null;
    }
    /** Write the overlay for the current quote at the geometry of the last draw. glide = moved by a price tick. */
    function paint(glide) {
      const cur = stateFor(c.symbol);
      const g = c.geo;
      if (!cur || !g) return hide();
      const { q, st } = cur;
      const fit = fitRange(g.mn, g.mx, q.price, null, false); // only asks: inside the drawn range?
      const pin = fit.pin;
      let y = pin > 0 ? g.top : pin < 0 ? g.plotH : g.yFor(q.price);
      y = Math.max(g.top, Math.min(g.plotH, y));
      root.dataset.mode = st.mode;
      root.dataset.dir = st.dir;
      root.dataset.pin = pin ? (pin > 0 ? 'above' : 'below') : '';
      root.classList.toggle('mlp--glide', !!glide && st.mode === 'live' && c.shown != null);
      // the wrap may have padding around the canvas: all positions are the canvas's own (g.ox / g.oy from place())
      root.style.left = `${g.ox}px`;
      root.style.width = `${Math.round(g.cw)}px`;
      root.style.transform = `translate3d(0,${Math.round((g.oy + y) * 2) / 2}px,0)`;
      line.style.width = `${Math.round(g.plotW)}px`;
      dot.style.left = `${Math.round(g.plotW)}px`;
      const gutter = Math.max(0, Math.round(g.cw - g.plotW));
      age.style.right = `${gutter + 6}px`;
      tag.style.minWidth = `${Math.max(0, gutter - 4)}px`; // covers the axis label it sits on
      set(tag, 'textContent', tagText(q, st));
      const ts = Date.parse(q.ts);
      set(age, 'textContent', st.mode === 'stale' ? asOfLabel(ts, env.wall()) : q.source === 'alpaca_iex' ? 'IEX' : '');
      const tip = hoverText(c.symbol, q, st, env.wall());
      if (tag.title !== tip) {
        tag.title = tip;
        tag.setAttribute('aria-label', tip);
      }
      set(root, 'hidden', false);
      c.shown = q.price;
    }
    c.refresh = fromTick => {
      const cur = stateFor(c.symbol);
      if (!cur || !c.geo) return hide();
      if (!c.visible) return undefined;
      const g = c.geo;
      // Would a draw now use another y-range than the canvas shows (the price left it, or a grown edge can go back)?
      // Only then redraw the canvas (which calls place() again); otherwise just move the DOM.
      const f = fitRange(g.bmn, g.bmx, cur.q.price, c.ext, g.atEdge);
      const needsRedraw = f.mn !== g.mn || f.mx !== g.mx;
      if (needsRedraw && fromTick) {
        try {
          spec.redraw();
        } catch {
          paint(false);
        }
        return undefined;
      }
      return paint(!!fromTick);
    };
    c.detach = () => {
      if (c.unobserve) c.unobserve();
      c.unobserve = null;
      if (root.parentNode) root.parentNode.removeChild(root);
      if (charts.get(c.id) === c) charts.delete(c.id);
      kick();
    };
    c.unobserve = env.observe(spec.canvas || spec.wrap, on => {
      if (c.visible === !!on) return;
      c.visible = !!on;
      kick();
    });
    c.symbol = readSymbol();
    charts.set(c.id, c);
    kick();

    return {
      sync() {
        const s = readSymbol();
        if (s === c.symbol) return;
        c.symbol = s;
        c.ext = null;
        // review 2026-11-19: the last draw's geometry belongs to the old symbol's bars; nothing is painted on it
        // (a poll answer arriving before the new bars would otherwise put the new price over the old chart)
        c.geo = null;
        c.bars = null;
        hide();
        kick();
      },
      range(mn, mx, atEdge) {
        const cur = stateFor(c.symbol);
        c.bars = { mn, mx, atEdge: !!atEdge };
        if (!cur) {
          c.ext = null;
          return { mn, mx };
        }
        const f = fitRange(mn, mx, cur.q.price, c.ext, !!atEdge);
        c.ext = f.ext;
        return { mn: f.mn, mx: f.mx };
      },
      place(g) {
        const b = c.bars || { mn: g.mn, mx: g.mx, atEdge: true };
        const cv = spec.canvas;
        c.geo = { ...g, bmn: b.mn, bmx: b.mx, atEdge: b.atEdge, ox: (cv && cv.offsetLeft) || 0, oy: (cv && cv.offsetTop) || 0 };
        paint(false);
      },
      hide() {
        c.geo = null;
        hide();
      },
      detach: c.detach,
      _chart: c
    };
  }

  return {
    attach,
    pollS,
    stats,
    get: stateFor,
    market: () => market,
    visibleSymbols,
    isPolling: () => timer != null || inflight,
    stop() {
      off = true;
      if (timer != null) env.clearTimer(timer);
      timer = null;
      offVis();
      [...charts.values()].forEach(c => c.detach());
    }
  };
}

let mgr = null;
/** Template entry: start({pollS}) once. Returns the manager (the same one on a second call). */
export function start(options = {}) {
  if (!mgr) mgr = createManager(options);
  return mgr;
}
export const attach = spec => (mgr ? mgr.attach(spec) : null);
export const active = () => !!mgr;
export function __reset() {
  if (mgr) mgr.stop();
  mgr = null;
}

export default { start, attach, active, classify, fitRange, direction, hoverText, tagText, asOfLabel, staleAfterS, createManager, __reset };
