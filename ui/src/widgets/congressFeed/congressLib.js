// Shared helpers for the four Congress widgets (congressFeed, congressOverlap, congressLeaderboard,
// congressChart). Pure functions + one page-scoped event; no module-level state.
//
// Data: GET /api/rh/congress (contracts §6, S2-T1). Field meaning per S2-T1:
//   trades[].fwd["30"|"90"|"180"] = {ret, xs_spy, matured, inverted, signal_ret, signal_xs, delisted}
//   (only matured horizons are stored; sells are graded with the sign inverted → signal_* fields).
//
// Linking (S3 F1): every Congress widget on the page carries config.link = 'congress'.
//   - Overlap card → feed filter: FILTER_EVENT {group, ticker} (page-scoped; '' clears).
//   - Feed row / overlap card → chart: mason:chart-symbol {chartId: '<link>-main', ticker}.
//   Unlinked instances (e.g. a congressFeed on the home dashboard) ignore page filters and emit
//   mason:chart-symbol with chartId null ("chart this" for whoever listens) plus, since S3-F6,
//   mason:go {page:'congress', focus:ticker}: the template opens #congress, whose linked widgets take
//   the focus as their filter / chart pick (onCongressGo, replayed).
import { useEffect, useState } from 'react';
import { isNum, DASH, pct } from '../../lib/fmt.js';
import { normTicker, onGo } from '../../lib/bridge.js';

export const ROUTE = '/api/rh/congress';
export const POLL_MS = 60000; // the backend polls every congress.poll_hours (6 h); 60 s is plenty
export const CAPTION = 'Disclosures lag trades by up to 45 days; graded from the first session after disclosure.';
export const HORIZONS = ['30', '90', '180'];
export const FILTER_EVENT = 'mason:congress-filter';

const str = v => (typeof v === 'string' ? v : v == null ? '' : String(v));
const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** Data accessors that never throw on junk. */
export const listOf = v => (Array.isArray(v) ? v.filter(x => x && typeof x === 'object') : []);
export const tradesOf = d => listOf(d && d.trades);
export const overlapOf = d => listOf(d && d.held_overlap);
export const boardOf = d => listOf(d && d.leaderboard);

/** Ticker for display: string as-is (JSX escapes it) or DASH. */
export const tickerText = t => (str(t).trim() ? str(t).trim() : DASH);

/** chartId for a linked widget group, or null when unlinked. */
export const chartIdFor = link => (link ? `${link}-main` : null);

// ── page-scoped filter event ────────────────────────────────────────────────
export function emitCongressFilter(group, ticker) {
  if (!group) return false;
  const t = ticker ? normTicker(str(ticker)) || '' : '';
  window.dispatchEvent(new CustomEvent(FILTER_EVENT, { detail: { group: String(group), ticker: t } }));
  return true;
}

/** fn(ticker) for filter events addressed to `group`. Returns unsubscribe. */
export function onCongressFilter(group, fn) {
  if (!group) return () => {};
  const h = e => {
    const d = e && e.detail;
    if (!d || d.group !== String(group)) return;
    fn(typeof d.ticker === 'string' ? d.ticker : '');
  };
  window.addEventListener(FILTER_EVENT, h);
  return () => window.removeEventListener(FILTER_EVENT, h);
}

export const CONGRESS_PAGE = 'congress';
/** fn(ticker) for mason:go {page:'congress', focus} with a valid ticker focus (replayed). Returns unsubscribe. */
export function onCongressGo(fn) {
  return onGo(
    ({ page, focus }) => {
      if (page !== CONGRESS_PAGE) return;
      const t = normTicker(focus == null ? '' : focus);
      if (t) fn(t);
    },
    { replay: true }
  );
}

/** React: the current page filter for `group` ('' = none). Always '' when unlinked. */
export function useCongressFilter(group) {
  const [t, setT] = useState('');
  useEffect(() => onCongressFilter(group, setT), [group]);
  // S3-F6: #congress opened from a dashboard feed row (mason:go {page:'congress', focus:ticker}) acts
  // like an overlap-card click: the linked page widgets filter to that ticker. Replayed, because the
  // go event is what mounts the page.
  useEffect(() => (group ? onCongressGo(setT) : undefined), [group]);
  return [group ? t : '', setT];
}

// ── formatting ──────────────────────────────────────────────────────────────
function shortMoney(v) {
  const a = Math.abs(v);
  const units = [
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K']
  ];
  for (const [d, s] of units) {
    if (a >= d * 0.9995) {
      const q = Math.round((a / d) * 10) / 10;
      return `$${Number.isInteger(q) ? q : q.toFixed(1)}${s}`;
    }
  }
  return `$${Math.round(a)}`;
}

/** Disclosed amount range: 500001, 1000000 → "$500K–$1M". */
export function amountRange(low, high) {
  const lo = isNum(low) && low >= 0 ? low : null;
  const hi = isNum(high) && high >= 0 ? high : null;
  if (lo == null && hi == null) return DASH;
  if (hi == null) return `over ${shortMoney(lo)}`;
  if (lo == null) return `up to ${shortMoney(hi)}`;
  return `${shortMoney(lo)}–${shortMoney(hi)}`;
}

const CHAMBER = { house: 'House', senate: 'Senate' };
const PARTY = { democrat: 'D', democratic: 'D', republican: 'R', independent: 'I' };

/** "House · D" — chamber + party as text (never color alone). */
export function partyBadge(chamber, party) {
  const c = str(chamber).trim();
  const p = str(party).trim();
  const cc = CHAMBER[c.toLowerCase()] || clip(c, 12);
  const pp = PARTY[p.toLowerCase()] || clip(p, 12);
  const out = [cc, pp].filter(Boolean).join(' · ');
  return out || DASH;
}

function toUtcDate(v) {
  if (typeof v !== 'string') return null;
  const m = v.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Sep 9" (or "Sep 9 '25" when the year differs from refYear). */
export function shortDate(v, refYear = null) {
  const d = toUtcDate(v);
  if (!d) return DASH;
  const s = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const y = d.getUTCFullYear();
  return refYear != null && y !== refYear ? `${s} '${String(y).slice(2)}` : s;
}

export const yearOf = iso => {
  const d = toUtcDate(typeof iso === 'string' ? iso.slice(0, 10) : null);
  return d ? d.getUTCFullYear() : null;
};

/** Lag in days: the API's lag_days, else disclosure − transaction. null when unknown. */
export function lagOf(t) {
  if (isNum(t.lag_days)) return Math.round(t.lag_days);
  const a = toUtcDate(t.tx_date);
  const b = toUtcDate(t.disclosure_date);
  return a && b ? Math.round((b - a) / 86400000) : null;
}

/** 'buy' | 'sell' | 'other' and its label. */
export function sideOf(t) {
  const s = str(t && t.tx_type).toLowerCase();
  if (s === 'buy' || s === 'purchase') return { side: 'buy', label: 'BUY' };
  if (s === 'sell' || s === 'sale' || s.startsWith('sale')) return { side: 'sell', label: 'SELL' };
  return { side: 'other', label: s ? clip(s.toUpperCase(), 10) : DASH };
}

const GRADED_ONLY = 'The backend only grades equity buys/sells';

/**
 * Rows mason_congress never grades (S2-T1; mason_congress.py:597-608 grades only tx_type buy/sell with
 * asset_type equity): a non-equity asset_type ('option', or any other cleaned string) or tx_type
 * 'exchange'. Returns {label, why} for those, null for gradeable rows. A missing asset_type is equity
 * (the backend's default).
 */
export function ungradedReason(t) {
  if (!t || typeof t !== 'object') return null;
  const asset = typeof t.asset_type === 'string' ? t.asset_type.trim().toLowerCase() : '';
  if (asset && asset !== 'equity') {
    if (asset === 'option') return { label: 'not graded — options', why: `Option legs are not graded: call/put direction unknown. ${GRADED_ONLY}.` };
    return { label: `not graded — ${clip(asset, 16)}`, why: `${GRADED_ONLY}; this row is a ${clip(asset, 16)} disclosure.` };
  }
  if (typeof t.tx_type === 'string' && t.tx_type.trim().toLowerCase() === 'exchange') {
    return { label: 'not graded — exchange', why: `Exchanges are not graded. ${GRADED_ONLY}.` };
  }
  return null;
}

/**
 * Forward results per horizon. Matured → the signal-oriented excess vs SPY (signal_xs; for sells
 * the sign is inverted by the backend, flagged `inverted`). Unmatured or missing → 'pending'.
 * Rows the backend never grades → ONE cell {h:'all', state:'ungraded', label, why} (Director OQ-3:
 * "pending" would promise a grade that never comes).
 */
export function fwdCells(t) {
  const ug = ungradedReason(t);
  if (ug) return [{ h: 'all', state: 'ungraded', label: ug.label, why: ug.why }];
  const fwd = t && t.fwd && typeof t.fwd === 'object' ? t.fwd : {};
  return HORIZONS.map(h => {
    const f = fwd[h];
    if (!f || typeof f !== 'object' || f.matured !== true) return { h, state: 'pending' };
    if (f.delisted === true) return { h, state: 'delisted' };
    const value = isNum(f.signal_xs) ? f.signal_xs : isNum(f.xs_spy) && f.inverted !== true ? f.xs_spy : null;
    return {
      h,
      state: 'matured',
      value,
      inverted: f.inverted === true,
      raw: isNum(f.xs_spy) ? f.xs_spy : null,
      ret: isNum(f.signal_ret) ? f.signal_ret : null
    };
  });
}

/** Title text for a forward cell (the detail behind the number). */
export function fwdTitle(c) {
  if (c.state === 'ungraded') return c.why;
  if (c.state === 'pending') return `${c.h} sessions after entry: not matured yet`;
  if (c.state === 'delisted') return `${c.h}d: delisted before maturing (excluded from averages)`;
  const base = `${c.h}d excess vs SPY ${pct(isNum(c.value) ? c.value * 100 : null)}`;
  return c.inverted ? `${base} — sell, graded inverted (raw ${pct(isNum(c.raw) ? c.raw * 100 : null)}: a drop counts as right)` : base;
}

const dkey = v => (typeof v === 'string' ? v.slice(0, 10) : '');

/** Newest disclosure first, then newest transaction. Never mutates. */
export function sortTrades(list) {
  return listOf(list)
    .slice()
    .sort((a, b) => {
      const d = dkey(b.disclosure_date).localeCompare(dkey(a.disclosure_date));
      return d || dkey(b.tx_date).localeCompare(dkey(a.tx_date));
    });
}

/** {ticker?, heldOnly?} filter (ticker compared case-insensitively). */
export function filterTrades(list, { ticker = '', heldOnly = false } = {}) {
  const want = str(ticker).trim().toUpperCase();
  return listOf(list).filter(t => (!want || str(t.ticker).trim().toUpperCase() === want) && (!heldOnly || t.held === true));
}

/** "A, B, C +2" */
export function namesLine(names, max = 3) {
  const list = (Array.isArray(names) ? names : []).map(str).map(s => s.trim()).filter(Boolean);
  if (!list.length) return DASH;
  return list.length > max ? `${list.slice(0, max).join(', ')} +${list.length - max}` : list.join(', ');
}

export const plural = (n, one, many) => `${isNum(n) ? Math.round(n) : DASH} ${isNum(n) && Math.round(n) === 1 ? one : many}`;
export const count = v => (isNum(v) && v >= 0 ? Math.round(v) : null);

/** Cap note for lists cut to `shown` of `total`: "+76 more (24 of 100 shown)". */
export const moreLabel = (shown, total) =>
  `+${(total - shown).toLocaleString('en-US')} more (${shown.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} shown)`;
