// Display formatting shared by every widget. Every function is total: null, undefined, NaN,
// Infinity, strings and -0 never throw and never print "NaN" or "-$0.00".
// Money convention (Nico, 2026-09-21): sign ALWAYS before the $, cents ALWAYS shown.

export const DASH = '—';

/** True only for finite numbers (not numeric strings). */
export const isNum = v => typeof v === 'number' && Number.isFinite(v);

// Round first, then decide the sign, so -0.001 → "0.00" and never "-0.00".
// Values whose scaled magnitude is not finite (e.g. 1e308 * 100) are reported as {bad:true}
// and every formatter prints DASH for them: we never print ∞ or Infinity (DEBUG_F0 F3).
function rounded(v, digits) {
  const f = 10 ** digits;
  const scaled = Math.abs(v) * f;
  if (!Number.isFinite(scaled)) return { abs: 0, neg: false, zero: false, bad: true };
  const r = Math.round(scaled) / f;
  return { abs: r, neg: v < 0 && r !== 0, zero: r === 0, bad: !Number.isFinite(r) };
}

// F0b: Intl formatters are expensive to build; toLocaleString() builds one per call, which was a
// hot path at 10k rows (F4). Cache one per options key. Output is byte-identical (the spec defines
// toLocale*String(locale, opts) as new Intl.*Format(locale, opts).format) — test/fixtures/fmt_golden.json.
const nfCache = new Map();
const dfCache = new Map();
function numberFormat(digits) {
  let f = nfCache.get(digits);
  if (!f) {
    f = new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    nfCache.set(digits, f);
  }
  return f;
}
function dateFormat(locale, opts) {
  const key = `${locale}|${JSON.stringify(opts)}`;
  let f = dfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(locale, opts);
    dfCache.set(key, f);
  }
  return f;
}

function fixed(abs, digits) {
  return numberFormat(digits).format(abs);
}

/** money(-60) → "-$60.00"; {signed:true} → "+$60.00"; {compact:true} → "$1.53M". */
export function money(v, { signed: withSign = false, compact = false } = {}) {
  if (!isNum(v)) return DASH;
  let body;
  let r;
  if (compact && Math.abs(v) >= 1000) {
    const units = [
      [1e12, 'T'],
      [1e9, 'B'],
      [1e6, 'M'],
      [1e3, 'K']
    ];
    const [div, suf] = units.find(([d]) => Math.abs(v) >= d);
    r = rounded(v / div, 2);
    body = `${fixed(r.abs, 2)}${suf}`;
  } else {
    r = rounded(v, 2);
    body = fixed(r.abs, 2);
  }
  if (r.bad) return DASH;
  const sign = r.neg ? '-' : withSign && !r.zero ? '+' : '';
  return `${sign}$${body}`;
}

/** pct(1.234) → "+1.2%". Input is already in percent units. */
export function pct(v, { digits = 1, signed: withSign = true } = {}) {
  if (!isNum(v)) return DASH;
  const r = rounded(v, digits);
  if (r.bad) return DASH;
  const sign = r.neg ? '-' : withSign && !r.zero ? '+' : '';
  return `${sign}${fixed(r.abs, digits)}%`;
}

/** signed(3.14159, 2) → "+3.14". */
export function signed(v, digits = 2) {
  if (!isNum(v)) return DASH;
  const r = rounded(v, digits);
  if (r.bad) return DASH;
  const sign = r.neg ? '-' : r.zero ? '' : '+';
  return `${sign}${fixed(r.abs, digits)}`;
}

/** ▲ for up, ▼ for down, '' for flat/unknown. Pair with color; never color alone. */
export function arrow(v) {
  if (!isNum(v) || v === 0) return '';
  return v > 0 ? '▲' : '▼';
}

/** Direction class for CSS: 'mui-up' | 'mui-down' | 'mui-flat'. */
export function dirClass(v) {
  if (!isNum(v) || v === 0) return 'mui-flat';
  return v > 0 ? 'mui-up' : 'mui-down';
}

function toDate(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v;
  if (typeof v === 'number') return Number.isFinite(v) ? new Date(v) : null;
  if (typeof v !== 'string') return null;
  // A bare YYYY-MM-DD is a calendar date: render it as-is, never shifted by the local zone.
  const m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const d = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}
const isBareDate = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);

/** date('2026-09-30') → "Sep 30, 2026". {utc:true} formats timestamps in UTC (tests, server dates). */
export function date(v, { utc = false } = {}) {
  const d = toDate(v);
  if (!d) return DASH;
  return dateFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: utc || isBareDate(v) ? 'UTC' : undefined
  }).format(d);
}

/** dateTime('2026-09-30T14:05:00Z') → "Sep 30, 14:05" (24 h). */
export function dateTime(v, { utc = false } = {}) {
  const d = toDate(v);
  if (!d) return DASH;
  const tz = utc ? 'UTC' : undefined;
  const day = dateFormat('en-US', { month: 'short', day: 'numeric', timeZone: tz }).format(d);
  const time = dateFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: tz }).format(d);
  return `${day}, ${time}`;
}

/** Whole days from a → b (calendar dates), or null when either is junk. */
export function lagDays(a, b) {
  const da = toDate(a);
  const db = toDate(b);
  if (!da || !db) return null;
  return Math.round((db.getTime() - da.getTime()) / 86400000);
}

/** "n=12". Every statistic shows its n. */
export function nLabel(n) {
  return `n=${isNum(n) ? Math.round(n) : DASH}`;
}

/** Plain number with grouping, or DASH. */
export function num(v, digits = 0) {
  if (!isNum(v)) return DASH;
  const r = rounded(v, digits);
  if (r.bad) return DASH;
  return `${r.neg ? '-' : ''}${fixed(r.abs, digits)}`;
}
