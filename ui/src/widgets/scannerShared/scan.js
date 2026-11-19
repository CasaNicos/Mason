// Pure helpers for the /api/scanners payload (mason_scanners.api_scanners, real fixture
// Tests/fixtures/api/scanners.json). Total functions: junk in → '—' or null out, never a throw.
import { DASH } from '../../lib/fmt.js';

export const SCANNERS_ROUTE = '/api/scanners';
export const POLL_MS = 15000; // SC §5: the page refreshes every 15 s (DB read, no Robinhood call)

const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const str = v => (typeof v === 'string' ? v : v == null ? '' : String(v));

/** Scans with a string scan_id and an array of hits with an id (defensive against a bad payload). */
export function scansOf(data) {
  const raw = isObj(data) && Array.isArray(data.scans) ? data.scans : [];
  return raw
    .filter(s => isObj(s) && typeof s.scan_id === 'string' && s.scan_id)
    .map(s => ({
      ...s,
      columns: Array.isArray(s.columns) ? s.columns.filter(c => typeof c === 'string') : [],
      hits: Array.isArray(s.hits) ? s.hits.filter(h => isObj(h) && h.id != null && h.id !== '') : []
    }));
}

/**
 * The scan a list shows: config.scan_id when set (stored by id, never by title); otherwise the
 * slot-th scan (1-based). An id that is not in the data is reported as missing — never silently
 * replaced by another scan.
 */
export function resolveScan(scans, scanId, slot) {
  if (scanId) {
    const s = scans.find(x => x.scan_id === scanId);
    return s ? { scan: s, missing: null } : { scan: null, missing: String(scanId) };
  }
  const i = Number.isFinite(Number(slot)) ? Math.max(1, Math.round(Number(slot))) - 1 : 0;
  return { scan: scans[i] || null, missing: null };
}

/** "13:58" (24 h, local time) or '—'. */
export function hhmm(iso) {
  if (typeof iso !== 'string' || !iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
}

const NUM_RE = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/;
const EXP_RE = /^\s*[-+]?(?:\d+\.?\d*|\.\d+)[eE][-+]?\d+\s*$/;
const YMD_RE = /^\s*\d(?:\.\d+)?[eE]\+?0?7\s*$|^\s*\d{8}(?:\.0+)?\s*$/;

/**
 * The ONE numeric parse shared with the server (mason_scanners._ratio_num; parity table
 * test/widgets/scannerCellParity.json): trim, U+2212 minus → "-", one leading "+" dropped, ","
 * thousands separators dropped, then a plain decimal/exponent number. No K/M/B/T suffix, no "$",
 * no NaN/Infinity → null.
 */
export function parseCellNum(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v !== 'string') return null;
  let t = v.trim().replace(/\u2212/g, '-');
  if (t.startsWith('+')) t = t.slice(1);
  t = t.replace(/,/g, '');
  if (!NUM_RE.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** toFixed without a negative zero ("-0.00" → "0.00"); same text as the server's _fixed. */
export function fixedNoNegZero(n, dp) {
  const t = n.toFixed(dp);
  return /^-0(?:\.0+)?$/.test(t) ? t.slice(1) : t;
}

/**
 * RULINGS 27 G-S1 (same rule as the server, mason_scanners._ratio_pct): a cell is a RATIO when its
 * column name (after leading spaces) starts with "%" and its value carries no "%" sign → percent
 * points, else null.
 */
export function ratioPct(column, v) {
  if (!/^\s*%/.test(str(column))) return null;
  if (typeof v === 'string' && v.includes('%')) return null;
  const n = parseCellNum(v);
  return n == null ? null : n * 100;
}

function ymdDate(column, v) {
  if (!/date/i.test(str(column))) return null;
  const n = typeof v === 'number' ? v : typeof v === 'string' && YMD_RE.test(v) ? Number(v) : NaN;
  if (!Number.isInteger(n) || n < 10000000 || n > 99999999) return null;
  const y = Math.floor(n / 10000), m = Math.floor(n / 100) % 100, d = n % 100;
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
  return `${String(y).padStart(4, '0')}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function compact(n) {
  const a = Math.abs(n);
  for (const [lim, suf] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (a >= lim) return `${fixedNoNegZero(n / lim, 2)}${suf}`;
  }
  return String(Number(n.toPrecision(6)));
}

/**
 * A Robinhood cell as display text (rendered through JSX, so escaped). Without a column: strings
 * verbatim (unchanged behaviour). With the column (G-S3): "%" ratio cells as "-5.08%", yyyymmdd
 * float dates as "2026-10-19", exponent floats compacted ("1.53M"). Never rewrites stored data.
 */
export function cellText(v, column) {
  if (v == null || v === '') return DASH;
  if (column != null && (typeof v === 'string' || typeof v === 'number')) {
    const r = ratioPct(column, v);
    if (r != null) return `${fixedNoNegZero(r, 2)}%`;
    const d = ymdDate(column, v);
    if (d) return d;
    // exponent floats compact (UI only, G-S3) — never in a date column: an invalid yyyymmdd stays raw
    if (typeof v === 'string' && !/date/i.test(str(column)) && EXP_RE.test(v) && Number.isFinite(Number(v))) {
      return compact(Number(v));
    }
  }
  if (typeof v === 'string') return v;
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : DASH;
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return DASH; // objects/arrays: never dump structure into a cell
}

/** Signed number from a "% Change"-like column ("-5.03%", "+1.2", 8.42) or null. */
export function changeValue(column, v) {
  if (!/change/i.test(str(column))) return null;
  const n = typeof v === 'number' ? v : parseFloat(str(v).replace(/[%,+\s]/g, ''));
  return Number.isFinite(n) && n !== 0 ? n : null;
}

/** qwen triage → {score, dir, word, title}. Direction is a word plus ▲/▼, never color alone. */
export function qwenView(t) {
  if (!isObj(t)) return { score: 'pending', dir: 0, word: '', title: 'Not triaged by qwen yet', state: 'pending' };
  if (t.failed) return { score: 'failed', dir: 0, word: '', title: 'qwen triage failed', state: 'failed' };
  const s = typeof t.score === 'number' && t.score >= 0 && t.score <= 1 ? t.score.toFixed(2) : DASH; // 0–1 only
  const d = t.direction === 'long' ? 1 : t.direction === 'short' ? -1 : 0;
  const parts = [str(t.note), t.why_now ? `Why now: ${str(t.why_now)}` : ''].filter(Boolean);
  return {
    score: s,
    dir: d,
    word: d > 0 ? 'long' : d < 0 ? 'short' : 'none',
    title: parts.join(' — ') || 'qwen score (0–1)',
    state: 'ok'
  };
}

/** Watchlist status words (watch_status from mason_watchlist). */
export function watchWord(s) {
  if (s === 'proposed') return 'proposed';
  if (s === 'active') return 'watching';
  if (typeof s === 'string' && s) return s;
  return DASH;
}

export function isDisconnected(status) {
  return isObj(status) && (status.connected === false || status.state === 'disconnected');
}

export function statusOf(data) {
  return isObj(data) && isObj(data.status) ? data.status : null;
}

export function errText(e) {
  if (e == null || e === '') return '';
  if (typeof e === 'string') return e;
  if (isObj(e)) return str(e.message || e.error || e.where || 'error');
  return str(e);
}
