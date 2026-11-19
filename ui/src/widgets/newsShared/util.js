// newsShared/util.js — pure helpers for the News page widgets (UI overhaul Task 5, 2026-11-16).
// No React, no DOM. Everything here is total: junk in → safe defaults out, never a throw.
//
// Item shape after normItem() (every widget reads only this):
//   {key, id, t (epoch ms|null), time (display, e.g. "3:41pm et"), headline, source, url (http(s)|null),
//    cat ('holding'|'watchlist'|'macro'), book (bool), dir ('bullish'|'bearish'|'neutral'|null),
//    sign (1|-1|0), materiality (0..1|null), material (bool), implication, desc, tickers [≤8],
//    kind (event_kind|null), mm (market_materiality|null), tier (source_tier|null)}

export const CATS = [
  { id: 'all', label: 'All' },
  { id: 'holding', label: 'Holdings' },
  { id: 'watchlist', label: 'Watchlist' },
  { id: 'macro', label: 'Macro' },
  { id: 'material', label: 'Material' }
];
export const CAT_IDS = CATS.map(c => c.id);

// ── text ──────────────────────────────────────────────────────────────────────
// Feeds ship entities ("What&#39;s", "&amp;", "&rsquo;"). The vanilla page decodes them through a
// <textarea>; the bundle keeps innerHTML out of its source, so decode with a table + numeric refs.
const NAMED = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', rsquo: '’', lsquo: '‘',
  rdquo: '”', ldquo: '“', sbquo: '‚', bdquo: '„', mdash: '—', ndash: '–',
  hellip: '…', middot: '·', bull: '•', trade: '™', reg: '®', copy: '©',
  euro: '€', pound: '£', yen: '¥', cent: '¢', deg: '°', times: '×',
  divide: '÷', plusmn: '±', frac12: '½', frac14: '¼', frac34: '¾',
  laquo: '«', raquo: '»', prime: '′', eacute: 'é', egrave: 'è', aacute: 'á',
  iacute: 'í', oacute: 'ó', uacute: 'ú', ntilde: 'ñ', ccedil: 'ç', uuml: 'ü',
  ouml: 'ö', auml: 'ä', szlig: 'ß'
};
export function decodeEntities(s) {
  const t = s == null ? '' : String(s);
  if (t.indexOf('&') < 0) return t;
  return t.replace(/&(#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[a-zA-Z][a-zA-Z0-9]{1,15});/g, (m, e) => {
    if (e[0] === '#') {
      const cp = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      const ok = Number.isFinite(cp) && cp > 0 && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff);
      return ok ? String.fromCodePoint(cp) : m;
    }
    const v = NAMED[e] ?? NAMED[e.toLowerCase()];
    return v != null ? v : m;
  });
}
/** Decoded, whitespace-collapsed, trimmed, capped text ('' for junk). */
export function cleanText(s, max = 400) {
  const t = decodeEntities(s).replace(/\s+/g, ' ').trim();
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

/** Only absolute http(s) links survive (no javascript:, data:, relative junk). */
export function safeUrl(u) {
  if (typeof u !== 'string' || !u) return null;
  try {
    const x = new URL(u);
    return x.protocol === 'http:' || x.protocol === 'https:' ? x.href : null;
  } catch {
    return null;
  }
}

const TICKER_RE = /^[A-Z0-9][A-Z0-9.\-^/]{0,15}$/;
export function normTk(t) {
  const s = typeof t === 'string' ? t.trim().toUpperCase() : '';
  return TICKER_RE.test(s) ? s : null;
}
const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);

// ── items ─────────────────────────────────────────────────────────────────────
export function normDir(d) {
  return d === 'bullish' || d === 'bearish' || d === 'neutral' ? d : null;
}
const signOf = d => (d === 'bullish' ? 1 : d === 'bearish' ? -1 : 0);

/** One /api/news or /api/news/flow item → the widget item shape (see the header). null for junk. */
export function normItem(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const headline = cleanText(raw.headline || raw.title, 400);
  if (!headline) return null;
  const tsSec = num(raw.ts) || num(raw.published_at) || num(raw.ingested_at);
  const src = Array.isArray(raw.tk_all) ? raw.tk_all : Array.isArray(raw.tickers) ? raw.tickers : [];
  const tickers = [];
  for (const t of src) {
    const n = normTk(t);
    if (n && !tickers.includes(n)) tickers.push(n);
    if (tickers.length >= 8) break;
  }
  const cat = raw.category === 'holding' || raw.category === 'watchlist' ? raw.category : 'macro';
  const dir = normDir(raw.direction);
  const mat = num(raw.materiality);
  const id = typeof raw.id === 'string' && raw.id ? raw.id : '';
  return {
    key: id || `h:${headline.slice(0, 120)}`,
    id,
    t: tsSec && tsSec > 0 ? Math.round(tsSec * 1000) : null,
    time: cleanText(raw.time_str, 24),
    headline,
    source: cleanText(raw.source, 40) || 'News',
    url: safeUrl(raw.url),
    cat,
    book: cat !== 'macro',
    dir,
    sign: signOf(dir),
    materiality: mat == null ? null : Math.max(0, Math.min(1, mat)),
    material: raw.material === true,
    implication: cleanText(raw.implication, 400),
    desc: cleanText(raw.desc, 700),
    tickers,
    kind: typeof raw.event_kind === 'string' && raw.event_kind ? raw.event_kind : null,
    mm: num(raw.market_materiality),
    tier: num(raw.source_tier)
  };
}
export function normItems(arr) {
  const out = [];
  const seen = new Set();
  for (const r of Array.isArray(arr) ? arr : []) {
    const it = normItem(r);
    if (it && !seen.has(it.key)) {
      seen.add(it.key);
      out.push(it);
    }
  }
  return out;
}
export const byTimeDesc = (a, b) => (b.t || 0) - (a.t || 0);

// ── filters ───────────────────────────────────────────────────────────────────
export function catMatch(cat) {
  if (cat === 'material') return it => it.material;
  if (cat === 'holding' || cat === 'watchlist' || cat === 'macro') return it => it.cat === cat;
  return () => true;
}
export function catCounts(items) {
  const c = { all: 0, holding: 0, watchlist: 0, macro: 0, material: 0 };
  for (const it of items) {
    c.all += 1;
    c[it.cat] += 1;
    if (it.material) c.material += 1;
  }
  return c;
}
/** Selection (bubble ticker / sector, timeline range) applied to items, client side. */
export function filterLocal(items, sel, names) {
  const { ticker, sector, range } = sel || {};
  if (!ticker && !sector && !range) return items;
  return items.filter(it => {
    if (ticker && !it.tickers.includes(ticker)) return false;
    if (sector && !it.tickers.some(t => names && names[t] && names[t].sector === sector)) return false;
    if (range && !(it.t != null && it.t >= range[0] && it.t < range[1])) return false;
    return true;
  });
}
export const hasSelection = sel => !!(sel && (sel.ticker || sel.sector || sel.range));
export function selKey(sel, span = 'today') {
  const r = sel && sel.range ? `${Math.round(sel.range[0])}-${Math.round(sel.range[1])}` : '';
  return `${span}|${(sel && sel.ticker) || ''}|${(sel && sel.sector) || ''}|${r}`;
}

// ── aggregates (fallback mode: /api/news only; flow mode gets them from the server) ──
export function aggregate(items, since, until, bucketMs = 900000) {
  const s0 = Number.isFinite(since) ? since : 0;
  const s1 = Number.isFinite(until) && until > s0 ? until : s0 + bucketMs;
  const nb = Math.max(1, Math.min(400, Math.ceil((s1 - s0) / bucketMs)));
  const buckets = Array.from({ length: nb }, (_, i) => ({ t: s0 + i * bucketMs, n: 0, bull: 0, bear: 0, mat: 0 }));
  const day = { n: 0, bull: 0, bear: 0, mat: 0 };
  const per = new Map();
  for (const it of items) {
    if (it.t == null || it.t < s0 || it.t > s1) continue;
    const b = buckets[Math.min(nb - 1, Math.max(0, Math.floor((it.t - s0) / bucketMs)))];
    const bull = it.sign > 0 ? 1 : 0;
    const bear = it.sign < 0 ? 1 : 0;
    const mat = it.material ? 1 : 0;
    for (const a of [b, day]) {
      a.n += 1;
      a.bull += bull;
      a.bear += bear;
      a.mat += mat;
    }
    for (const t of it.tickers) {
      let a = per.get(t);
      if (!a) per.set(t, (a = { ticker: t, n: 0, bull: 0, bear: 0, mat: 0, last: 0, book: '', sector: '', etf: '', name: '' }));
      a.n += 1;
      a.bull += bull;
      a.bear += bear;
      a.mat += mat;
      a.last = Math.max(a.last, it.t);
      if (it.cat === 'holding' && it.tickers.length === 1) a.book = 'holding';
      else if (it.cat === 'watchlist' && it.tickers.length === 1 && !a.book) a.book = 'watchlist';
    }
  }
  const tickers = [...per.values()].sort((a, b) => b.n - a.n || b.mat - a.mat || b.last - a.last || (a.ticker < b.ticker ? -1 : 1)).slice(0, 48);
  return { buckets, tickers, sectors: [], sector_other: 0, day, total: day.n, bucketMs, since: s0, until: s1 };
}

// ── sentiment ─────────────────────────────────────────────────────────────────
/** {score -1..1, tone 'up'|'down'|'flat', glyph ▲ ▼ •, word} — never color alone. */
export function sentiment(a) {
  const n = Math.max(1, (a && a.n) || 0);
  const s = (((a && a.bull) || 0) - ((a && a.bear) || 0)) / n;
  if (s >= 0.15) return { score: s, tone: 'up', glyph: '▲', word: 'net bullish' };
  if (s <= -0.15) return { score: s, tone: 'down', glyph: '▼', word: 'net bearish' };
  return { score: s, tone: 'flat', glyph: '•', word: 'mixed / neutral' };
}

// ── picks ─────────────────────────────────────────────────────────────────────
/** Lead story: the most material material item, else the freshest. */
export function pickLead(items) {
  let best = null;
  for (const it of items) {
    if (it.material && (!best || (it.materiality || 0) > (best.materiality || 0))) best = it;
  }
  return best || items[0] || null;
}
/** Up to `max` related stories: shared tickers, else the same event kind, else ±45 min. */
export function pickRelated(item, pool, max = 5) {
  if (!item) return [];
  const others = [];
  const seen = new Set([item.key]);
  for (const it of pool) {
    if (!seen.has(it.key)) {
      seen.add(it.key);
      others.push(it);
    }
  }
  const byTk = item.tickers.length ? others.filter(o => o.tickers.some(t => item.tickers.includes(t))) : [];
  if (byTk.length) return byTk.sort(byTimeDesc).slice(0, max);
  const byKind = item.kind && item.kind !== 'other' ? others.filter(o => o.kind === item.kind) : [];
  if (byKind.length) return byKind.sort(byTimeDesc).slice(0, max);
  if (item.t == null) return [];
  return others
    .filter(o => o.t != null && Math.abs(o.t - item.t) <= 45 * 60000)
    .sort((a, b) => Math.abs(a.t - item.t) - Math.abs(b.t - item.t))
    .slice(0, max);
}

// ── labels ────────────────────────────────────────────────────────────────────
const SECTOR_SHORT = {
  'Information Technology': 'Tech',
  'Communication Services': 'Comm.',
  'Consumer Discretionary': 'Discretionary',
  'Consumer Staples': 'Staples',
  Energy: 'Energy',
  Financials: 'Financials',
  'Health Care': 'Health',
  Industrials: 'Industrials',
  Materials: 'Materials',
  'Real Estate': 'Real Estate',
  Utilities: 'Utilities'
};
export const shortSector = s => SECTOR_SHORT[s] || (typeof s === 'string' ? s : '');

const KIND_LABEL = {
  analyst_action: 'Analyst action', earnings: 'Earnings', deal_partnership: 'Deal / partnership',
  central_bank: 'Central bank', mna: 'M&A', product_launch: 'Product launch', buyback: 'Buyback',
  geopolitical: 'Geopolitics', legal_regulatory: 'Legal / regulatory', macro_data: 'Macro data',
  mgmt_change: 'Management change', commodity: 'Commodities', fda_decision: 'FDA decision',
  trial_readout: 'Trial readout', trial_update: 'Trial update', contract_award: 'Contract award',
  guidance: 'Guidance', guidance_cut: 'Guidance cut', guidance_raise: 'Guidance raise',
  insider_buy: 'Insider buy', insider_sell: 'Insider sell', trade_policy: 'Trade policy',
  ipo_filing: 'IPO filing', ipo_pricing: 'IPO pricing', investigation: 'Investigation',
  prediction_market: 'Prediction market', app_rank: 'App ranking', activist: 'Activist',
  distress: 'Distress', data_breach: 'Data breach', labor_strike: 'Strike', accounting: 'Accounting'
};
export const kindLabel = k => (k && k !== 'other' ? KIND_LABEL[k] || String(k).replace(/_/g, ' ') : '');

const pad = n => (n < 10 ? `0${n}` : `${n}`);
/** "3:41 PM" in the browser's zone (Nico's ET). */
export function clock(ms) {
  if (!Number.isFinite(ms)) return '—';
  const d = new Date(ms);
  const h = d.getHours();
  return `${h % 12 || 12}:${pad(d.getMinutes())} ${h < 12 ? 'AM' : 'PM'}`;
}
export function clockRange(r) {
  return r && Number.isFinite(r[0]) && Number.isFinite(r[1]) ? `${clock(r[0])}–${clock(r[1])}` : '';
}
/** "just now", "4m ago", "2h ago". */
export function ago(ms, now = Date.now()) {
  if (!Number.isFinite(ms)) return '';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
export const pctText = v => (v == null ? '' : `${Math.round(v * 100)}%`);
