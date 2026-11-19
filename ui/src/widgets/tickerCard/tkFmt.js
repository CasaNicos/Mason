// Ticker card formatting + payload guards (contract: GET /api/ticker_card, see index.jsx).
// Every function is total: null, undefined, NaN, Infinity, strings-for-numbers and objects-for-strings print
// "—" (or are dropped), never "NaN", "$NaN", "-$0.00" or "[object Object]". Money: sign before the "$", like
// lib/fmt.js (Nico, 2026-09-21). Percent fields arrive in percent units (1.25 = 1.25%).
import { DASH, isNum, money, num, pct, signed, date as fmtDate, lagDays } from '../../lib/fmt.js';

export { DASH };

const SAFE = 1e16; // anything bigger is junk (market caps top out around 5e12)
const fin = v => (isNum(v) && Math.abs(v) <= SAFE ? (v === 0 ? 0 : v) : null);

/** A finite number field of obj, else null. */
export function N(obj, key) {
  return obj && typeof obj === 'object' ? fin(obj[key]) : null;
}

/** A trimmed one-line string field of obj (numbers are accepted and stringified), capped at max chars, else null. */
export function S(obj, key, max = 160) {
  if (!obj || typeof obj !== 'object') return null;
  return text(obj[key], max);
}

export function text(v, max = 160) {
  if (typeof v === 'number' && Number.isFinite(v)) v = String(v);
  if (typeof v !== 'string') return null;
  const t = v.replace(/\s+/g, ' ').trim();
  if (!t) return null;
  return t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t;
}

/** A plain object section of the payload, or null (arrays, strings and null are "missing"). */
export function section(v) {
  return v && typeof v === 'object' && !Array.isArray(v) ? v : null;
}

/** Last price: cents; four decimals under $1 (so penny stocks keep their digits). */
export function price(v) {
  const x = fin(v);
  if (x == null) return DASH;
  if (x !== 0 && Math.abs(x) < 1) {
    const r = Math.abs(x).toFixed(4);
    if (Number(r) === 0) return '$0.0000';
    return `${x < 0 ? '-' : ''}$${r}`;
  }
  return money(x);
}

const UNITS = [
  [1e12, 'T'],
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'K']
];

/** 182340000 → "182.3M", 24.3e9 → "24.30B", 812 → "812". Two decimals under 10 of a unit, else one. */
export function compact(v) {
  const x = fin(v);
  if (x == null) return DASH;
  const a = Math.abs(x);
  for (let i = 0; i < UNITS.length; i++) {
    const [d, s] = UNITS[i];
    if (a >= d) {
      let y = a / d;
      let digits = y < 10 ? 2 : 1;
      let r = Number(y.toFixed(digits));
      // 999.96M rounds to "1000.0M": step up to the next unit instead
      if (r >= 1000 && i > 0) {
        y = a / UNITS[i - 1][0];
        digits = 2;
        r = Number(y.toFixed(digits));
        return `${x < 0 ? '-' : ''}${r.toFixed(digits)}${UNITS[i - 1][1]}`;
      }
      return `${x < 0 ? '-' : ''}${r.toFixed(digits)}${s}`;
    }
  }
  return num(x, Number.isInteger(x) ? 0 : 2);
}

/** 4.44e12 → "$4.44T", -4.2e6 → "-$4.20M", 412.5 → "$412.50". */
export function compactMoney(v) {
  const x = fin(v);
  if (x == null) return DASH;
  if (Math.abs(x) < 1000) return money(x);
  const body = compact(Math.abs(x));
  return `${x < 0 ? '-' : ''}$${body}`;
}

/** EPS and other per-share money: "$3.36", "-$0.21". */
export function perShare(v) {
  const x = fin(v);
  return x == null ? DASH : money(x);
}

/** P/E-style ratio: "54.23", "1,204.5". */
export function ratio(v) {
  const x = fin(v);
  if (x == null) return DASH;
  return num(x, Math.abs(x) >= 100 ? 1 : 2);
}

/** Percent-unit value without a sign: 1.12 → "1.12%". */
export function pctPlain(v, digits = 2) {
  const x = fin(v);
  return x == null ? DASH : pct(x, { digits, signed: false });
}

/** Percent-unit value with a sign: 1.28 → "+1.28%". */
export function pctSigned(v, digits = 2) {
  const x = fin(v);
  return x == null ? DASH : pct(x, { digits, signed: true });
}

/** Signed price change: "+2.31", "-0.0042" under a cent. */
export function change(v) {
  const x = fin(v);
  if (x == null) return DASH;
  return signed(x, x !== 0 && Math.abs(x) < 0.01 ? 4 : 2);
}

/** Whole number with grouping: 36000 → "36,000". */
export function count(v) {
  const x = fin(v);
  return x == null ? DASH : num(Math.round(x), 0);
}

/** "2026-11-19" → "Nov 19, 2026"; anything else that parses → its local date; junk → "—". */
export function day(v) {
  if (typeof v !== 'string' && !isNum(v)) return DASH;
  return fmtDate(v);
}

function todayIso(now = new Date()) {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Days from today (local calendar) to a "YYYY-MM-DD": "today", "tomorrow", "in 12 days", "3 days ago"; else null. */
export function relDay(v, now = new Date()) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}/.test(v)) return null;
  const n = lagDays(todayIso(now), v.slice(0, 10));
  if (n == null) return null;
  if (n === 0) return 'today';
  if (n === 1) return 'tomorrow';
  if (n === -1) return 'yesterday';
  return n > 0 ? `in ${n} days` : `${-n} days ago`;
}

/** Earnings timing code → words. */
export function timing(v) {
  if (v === 'bmo') return 'before the open';
  if (v === 'amc') return 'after the close';
  return null;
}

const asOfFmt =
  typeof Intl !== 'undefined'
    ? new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : null;
const timeFmt = typeof Intl !== 'undefined' ? new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }) : null;

/** ISO timestamp → "12:42 PM" when it is today (local), else "Nov 17, 12:42 PM"; junk → null. */
export function asOf(v, now = new Date()) {
  if (typeof v !== 'string' || !v) return null;
  const d = new Date(v);
  if (Number.isNaN(d.getTime()) || !asOfFmt) return null;
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? timeFmt.format(d) : asOfFmt.format(d);
}

/**
 * The company website as a safe link: http(s) only (a bare "www.x.com" gets https://), no credentials.
 * → {href, label} or null. javascript:, data:, mailto: and anything unparsable → null.
 */
export function safeUrl(v) {
  const t = text(v, 300);
  if (!t || /\s/.test(t)) return null;
  let raw = t;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    if (!/^[a-z0-9.-]+\.[a-z]{2,}(?:[/?#:].*)?$/i.test(raw)) return null;
    raw = `https://${raw}`;
  }
  let u;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  if (u.username || u.password || !u.hostname) return null;
  return { href: u.href, label: u.hostname.replace(/^www\./i, '') };
}

/**
 * Where the price sits between low and high, 0..1 (clamped), or null when either end is missing or low >= high.
 */
export function rangePos(lo, hi, v) {
  const a = fin(lo);
  const b = fin(hi);
  const x = fin(v);
  if (a == null || b == null || x == null || !(b > a)) return null;
  return Math.max(0, Math.min(1, (x - a) / (b - a)));
}

/** "data: yfinance (profile, metrics) · alpaca (quote)" groups — [{source, parts:[...]}], stable order. A combined
 *  source such as "robinhood+yfinance" is shown as "robinhood + yfinance". */
export function sourceGroups(sources) {
  const s = section(sources);
  if (!s) return [];
  const order = ['quote', 'profile', 'metrics', 'financials', 'dates', 'analyst'];
  const keys = [...order.filter(k => k in s), ...Object.keys(s).filter(k => !order.includes(k))].slice(0, 12);
  const groups = [];
  for (const k of keys) {
    const raw = text(s[k], 60);
    if (!raw) continue;
    const src = raw.split('+').map(x => x.trim()).filter(Boolean).join(' + ');
    if (!src) continue;
    const part = text(k, 24);
    if (!part) continue;
    let g = groups.find(x => x.source.toLowerCase() === src.toLowerCase());
    if (!g) {
      g = { source: src, parts: [] };
      groups.push(g);
    }
    g.parts.push(part);
  }
  return groups;
}

/** The payload, shaped: every section an object or null, periods a bounded array, missing a bounded string list. */
export function cleanCard(body) {
  const b = section(body) || {};
  const fin0 = section(b.financials);
  const periods = fin0 && Array.isArray(fin0.periods) ? fin0.periods.filter(p => section(p)).slice(0, 8) : [];
  const dates = section(b.dates);
  return {
    symbol: text(b.symbol, 16),
    asOf: typeof b.as_of === 'string' ? b.as_of : null,
    profile: section(b.profile),
    quote: section(b.quote),
    metrics: section(b.metrics),
    financials: fin0 ? { periodType: text(fin0.period_type, 20), periods } : null,
    dates: dates
      ? {
          next: section(dates.next_earnings),
          last: section(dates.last_earnings),
          exDividend: typeof dates.ex_dividend === 'string' ? dates.ex_dividend : null,
          dividendPay: typeof dates.dividend_pay === 'string' ? dates.dividend_pay : null
        }
      : null,
    analyst: section(b.analyst),
    sources: section(b.sources),
    missing: Array.isArray(b.missing) ? b.missing.map(m => text(m, 60)).filter(Boolean).slice(0, 60) : []
  };
}

/** True when every listed number field of obj is missing. */
export function allNull(obj, keys) {
  if (!obj) return true;
  return keys.every(k => N(obj, k) == null);
}
