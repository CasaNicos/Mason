// Crowd page data model (UI overhaul Task 4, 2026-11-16). Shared by every crowd* widget.
// One route, no params: every crowd widget on a page shares ONE /api/rh/crowd request through useRhApi's
// cache. The ticker profile route is fetched only by the Ticker Snapshot, per picked symbol.
//
// Payload (mason_crowd.api_payload): {status, today[{ticker,rank,list}], events[{ticker,kind,date,rank,
// rank_bucket,fwd{5|20|60:{matured,ret,xs_spy,entry_*,exit_*}}}], scorecard{enter,exit}, crowded[{ticker,
// rank,held,watched}], list{name,snap_date,n_items}, follow, history{dates[],ranks{T:[rank|null]}},
// labels{T:{name,sector,industry}}}. history + labels are newer than the rest: an older backend omits
// them, and every helper below degrades to "no history / no names" (hasHistory() tells which).
import { useEffect, useState } from 'react';
import { useRhApi } from '../../lib/api.js';
import { emitChartSymbol, normTicker, onChartSymbol } from '../../lib/bridge.js';
import { date, isNum, pct } from '../../lib/fmt.js';

export const CROWD_ROUTE = '/api/rh/crowd';
export const TICKER_ROUTE = '/api/rh/crowd/ticker';
export const FOLLOW_FLAG = 'rh_expansion.crowd.allow_follow';
// The list is snapshotted once a day; a minute is plenty to look live without wasting requests.
export const CROWD_POLL_MS = 60000;
export const MAX_LIST = 100; // the real list is "100 Most Popular"
const MAX_HISTORY_DATES = 120;
const MAX_HISTORY_TICKERS = 600;

export function useCrowd() {
  return useRhApi(CROWD_ROUTE, null, { pollMs: CROWD_POLL_MS });
}

export const isBlocked = api => !!api && api.status === 'ok' && !!api.data && api.data.status === 'blocked_needs_follow';

export const arr = v => (Array.isArray(v) ? v : []);
export const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
export const str = v => (v == null ? '' : String(v));

/** Excess return (a fraction, e.g. -0.0458) → "-4.58%"; junk → "—". */
export const xsPct = (v, digits = 2) => (isNum(v) ? pct(v * 100, { digits }) : pct(null));
/** Same, without the sign (the ▲/▼ arrow or the words carry the direction): 0.0458 → "4.58%". */
export const absPct = (v, digits = 2) => (isNum(v) ? pct(Math.abs(v) * 100, { digits, signed: false }) : pct(null));

/** "Sep 18" (calendar date, no year) for dense rows. */
export const shortDate = v => date(v).replace(/, \d{4}$/, '');

const tickerOf = v => normTicker(typeof v === 'string' ? v : '');
const rankOf = v => (isNum(v) && v >= 1 && v <= 100000 ? Math.round(v) : null);
const byRank = (a, b) => (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank);

// ── the pick bus: "show this ticker in the Ticker Snapshot" ─────────────────────────────────────────
// Rides the bundle's existing window event (mason:chart-symbol, lib/bridge.js) on its own channel, so the
// template's chart bridge (it only acts on chartId 'main') ignores it.
export const PICK_CHANNEL = 'crowd-pick';
export const emitPick = ticker => emitChartSymbol(PICK_CHANNEL, ticker);
/** The last picked ticker (null until a pick), following the pick bus. */
export function usePicked() {
  const [picked, setPicked] = useState(null);
  useEffect(() => onChartSymbol(d => setPicked(d.ticker), { chartId: PICK_CHANNEL }), []);
  return picked;
}
// ── history (rank paths) ─────────────────────────────────────────────────────────────────────────────
/** True when the payload carries the history block (newer backend), even if it is still empty. */
export const hasHistory = d => !!d && typeof d === 'object' && d.history != null && typeof d.history === 'object' && Array.isArray(d.history.dates);

const histCache = new WeakMap(); // payload object → parsed history (payloads keep identity across same polls)
/** {dates[], ranks: Map<T, (rank|null)[]>} — validated and aligned. A junk date list → no history. */
export function historyOf(d) {
  const data = obj(d);
  if (histCache.has(data)) return histCache.get(data);
  const h = obj(data.history);
  const all = arr(h.dates);
  const ranks = new Map();
  let dates = [];
  if (all.length && all.every(x => typeof x === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(x))) {
    const cut = Math.max(0, all.length - MAX_HISTORY_DATES);
    dates = all.slice(cut);
    for (const [k, v] of Object.entries(obj(h.ranks))) {
      if (ranks.size >= MAX_HISTORY_TICKERS) break;
      const t = tickerOf(k);
      if (!t || !Array.isArray(v) || v.length !== all.length) continue;
      ranks.set(t, v.slice(cut).map(rankOf));
    }
  }
  const out = { dates, ranks };
  if (d && typeof d === 'object') histCache.set(data, out);
  return out;
}

/** {name, sector, industry} for a ticker (strings or null). */
export function labelOf(d, ticker) {
  const l = obj(obj(obj(d).labels)[ticker]);
  const s = v => (typeof v === 'string' && v.trim() ? v.trim().slice(0, 120) : null);
  return { name: s(l.name), sector: s(l.sector), industry: s(l.industry) };
}

export const isFundLike = (industry, sector) =>
  /investment trusts|mutual funds|exchange traded|\betf\b/i.test(`${industry || ''} ${sector || ''}`);

/** Lookback windows (in snapshots = trading days) for "what moved". */
export const LOOKBACKS = [
  { id: 'day', back: 1, label: '1 day', since: 'since the previous snapshot', days: 4 },
  { id: 'week', back: 5, label: '1 week', since: 'over the last week', days: 9 },
  { id: 'month', back: 20, label: '1 month', since: 'over the last month', days: 35 }
];
export const lookbackOf = id => LOOKBACKS.find(l => l.id === id) || LOOKBACKS[1];

/**
 * Today's list, ranked and enriched: [{ticker, rank, held, watched, prev, prevDate, delta, isNew, name, sector}].
 * `back` = how many snapshots to look back (1 = the previous one; clamped to the history window).
 * prev = rank `back` snapshots ago (null = wasn't on it); delta = prev - rank (positive = climbed);
 * isNew = wasn't on the list then (needs history), or (no history) an "enter" event on the snapshot day.
 */
export function todayRows(d, back = 1) {
  const data = obj(d);
  const flags = new Map();
  for (const c of arr(data.crowded)) {
    const t = tickerOf(obj(c).ticker);
    if (t) flags.set(t, obj(c));
  }
  const h = historyOf(data);
  const last = h.dates.length - 1;
  const at = last >= 1 ? Math.max(0, last - Math.max(1, back)) : -1;
  const snap = str(obj(data.list).snap_date);
  const enteredToday = new Set(
    arr(data.events)
      .filter(e => obj(e).kind === 'enter' && snap && obj(e).date === snap)
      .map(e => tickerOf(obj(e).ticker))
      .filter(Boolean)
  );
  const seen = new Set();
  const rows = [];
  for (const raw of arr(data.today)) {
    const t = tickerOf(obj(raw).ticker);
    if (!t || seen.has(t) || rows.length >= MAX_LIST * 10) continue;
    seen.add(t);
    const rank = rankOf(obj(raw).rank);
    const path = h.ranks.get(t);
    const known = at >= 0;
    const prev = known && path ? path[at] : null;
    const f = flags.get(t) || {};
    const lab = labelOf(data, t);
    rows.push({
      ticker: t,
      rank,
      held: f.held === true,
      watched: f.held !== true && f.watched === true,
      prev,
      prevDate: known ? h.dates[at] : null,
      delta: known && prev != null && rank != null ? prev - rank : null,
      isNew: known ? !!path && prev == null : enteredToday.has(t),
      name: lab.name,
      sector: lab.sector,
      industry: lab.industry
    });
  }
  rows.sort(byRank);
  return rows;
}

/** Recent exits (from events): [{ticker, date, rank}] newest first. */
export function recentExits(d, maxDays = 10) {
  const snap = obj(obj(d).list).snap_date;
  const ref = typeof snap === 'string' ? Date.parse(`${snap}T00:00:00Z`) : NaN;
  const out = [];
  const seen = new Set();
  for (const e of arr(obj(d).events)) {
    const x = obj(e);
    const t = tickerOf(x.ticker);
    if (x.kind !== 'exit' || !t || seen.has(t)) continue;
    const at = typeof x.date === 'string' ? Date.parse(`${x.date}T00:00:00Z`) : NaN;
    if (Number.isFinite(ref) && Number.isFinite(at) && (ref - at) / 86400000 > maxDays) continue;
    seen.add(t);
    out.push({ ticker: t, date: x.date, rank: rankOf(x.rank) });
  }
  return out.sort((a, b) => str(b.date).localeCompare(str(a.date)));
}

/** Events of one ticker, newest first. */
export const eventsFor = (d, ticker) =>
  arr(obj(d).events)
    .filter(e => tickerOf(obj(e).ticker) === ticker)
    .map(obj)
    .sort((a, b) => str(b.date).localeCompare(str(a.date)));

/** [{date, rank}] rank path of one ticker over the history window (rank null = off the list). */
export function rankPath(d, ticker) {
  const h = historyOf(d);
  const p = h.ranks.get(ticker);
  return p ? h.dates.map((dt, i) => ({ date: dt, rank: p[i] })) : [];
}

/** Market-cap size class in words (US convention). */
export function capClass(v) {
  if (!isNum(v) || v <= 0) return null;
  if (v >= 2e11) return 'Mega-cap';
  if (v >= 1e10) return 'Large-cap';
  if (v >= 2e9) return 'Mid-cap';
  if (v >= 3e8) return 'Small-cap';
  return 'Micro-cap';
}

/** "Rank band" lanes for the tracker: 1-25, 26-50, 51-75, 76-100 (+ anything ranked past 100 at the end). */
export const RANK_BANDS = [
  { id: 'b1', lo: 1, hi: 25, label: 'Top 25', short: '1–25' },
  { id: 'b2', lo: 26, hi: 50, label: '#26–50', short: '26–50' },
  { id: 'b3', lo: 51, hi: 75, label: '#51–75', short: '51–75' },
  { id: 'b4', lo: 76, hi: Infinity, label: '#76–100', short: '76–100' }
];
