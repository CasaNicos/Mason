// newsShared/store.js — ONE store for the six News page widgets (UI overhaul Task 5, 2026-11-16).
// Every widget is its own React root (MasonUI.mountWidget), so they share state through this module
// (one instance per page: Vite puts it in a shared chunk).
//
// Data:
//   GET /api/news/flow (span, limit)  → day-wide items + aggregates (timeline buckets, bubble tickers /
//                                        sectors). Read-only route added with this task (NEEDS RESTART).
//   GET /api/news                      → fallback while that route 404s (backend not restarted yet):
//                                        the 36 newest items, accumulated across polls (≤ FEED_KEEP), with
//                                        client-side aggregates. Flow is retried every FLOW_RETRY_MS.
//   Mock mode (?rhx_mock=1)            → fixtures/news_flow.json, fixtures/news.json (no server).
// Polling: every POLL_MS (the page's existing ~2-min cadence) while ≥1 news widget is mounted, the tab
// is visible and a news widget is on screen; on return it refetches once if the data is older.
// Selection (bubble ticker / sector, timeline range): items are filtered locally at once, then (flow mode)
// replaced by a server query over the whole span (debounced, small cache).
// New arrivals: ids first seen on a later poll are "fresh" for FRESH_MS → rows type in once.
import { useContext, useEffect, useSyncExternalStore } from 'react';
import { isMockMode, fixtureUrl, unwrap } from '../../lib/api.js';
import { isHidden, onVisibility } from '../../lib/visibility.js';
import { WidgetCtx } from '../../lib/context.js';
import {
  aggregate,
  byTimeDesc,
  catCounts,
  catMatch,
  CAT_IDS,
  filterLocal,
  hasSelection,
  normItems,
  normTk,
  pickLead,
  selKey
} from './util.js';

export const POLL_MS = 120000;
export const FLOW_RETRY_MS = 10 * 60000;
export const FRESH_MS = 90000;
export const BASE_LIMIT = 150;
export const QUERY_LIMIT = 150;
export const FEED_KEEP = 600;
const TIMEOUT_MS = 12000;
const QUERY_DEBOUNCE_MS = 160;
const SPAN_KEY = 'mv_news_span_v1';

const EMPTY_Q = Object.freeze({ key: null, status: 'idle', items: null, total: 0, error: null });
const EMPTY_SEL = Object.freeze({ cat: 'all', ticker: null, sector: null, range: null, focus: null });

function readSpan() {
  try {
    return localStorage.getItem(SPAN_KEY) === '24h' ? '24h' : 'today';
  } catch {
    return 'today';
  }
}

function initial() {
  return {
    status: 'loading', // 'loading' | 'ok' | 'error'
    error: null,
    mode: null, // 'flow' | 'feed'
    span: readSpan(),
    items: [], // base items, newest first
    agg: null, // {buckets, tickers, sectors, sector_other, day, total, bucketMs, since, until}
    names: {}, // TICKER → {name, sector}
    asOf: 0, // ms of the last good load
    newCount: 0, // arrivals on the last poll
    fresh: new Map(), // key → arrival ms
    sel: EMPTY_SEL,
    q: EMPTY_Q,
    rev: 0
  };
}

let state = initial();
const subs = new Set();
function set(patch) {
  state = { ...state, ...patch, rev: state.rev + 1 };
  subs.forEach(fn => {
    try {
      fn();
    } catch {
      /* one subscriber never breaks the others */
    }
  });
}
export const getState = () => state;
export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

// ── fetch ─────────────────────────────────────────────────────────────────────
async function getJson(url) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl ? ctl.signal : undefined });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return { status: res.status || 0, body };
  } catch (e) {
    return { status: 0, body: null, error: e && e.name === 'AbortError' ? 'timed out' : String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}
const flowForcedOff = () => {
  try {
    return new URLSearchParams(window.location.search).get('news_flow') === '0'; // test hook: exercise the fallback
  } catch {
    return false;
  }
};

let seeded = false;
let seen = new Set();
let feedPool = new Map(); // fallback mode: key → item, accumulated across polls
let flowFailAt = 0;
let inflight = null;
const qCache = new Map(); // selKey → {items, total, at}

function markFresh(items) {
  const now = Date.now();
  const fresh = new Map();
  for (const [k, t] of state.fresh) if (now - t < FRESH_MS) fresh.set(k, t);
  let n = 0;
  if (!seeded) {
    seen = new Set(items.map(i => i.key));
    seeded = true;
  } else {
    for (const it of items) {
      if (!seen.has(it.key)) {
        seen.add(it.key);
        fresh.set(it.key, now);
        n += 1;
      }
    }
    if (seen.size > 5000) seen = new Set(items.map(i => i.key));
  }
  return { fresh, newCount: n };
}

function refocus(items) {
  const f = state.sel.focus;
  if (!f) return state.sel;
  const upd = items.find(i => i.key === f.key);
  return upd && upd !== f ? { ...state.sel, focus: upd } : state.sel;
}

function applyFlow(data) {
  const items = normItems(data.items).sort(byTimeDesc);
  const names = { ...state.names };
  if (data.names && typeof data.names === 'object') {
    for (const [k, v] of Object.entries(data.names)) {
      const t = normTk(k);
      if (t && v && typeof v === 'object') names[t] = { name: String(v.name || ''), sector: String(v.sector || '') };
    }
  }
  const tickers = (Array.isArray(data.tickers) ? data.tickers : [])
    .map(a => ({ ...a, ticker: normTk(a && a.ticker) }))
    .filter(a => a.ticker && Number.isFinite(a.n) && a.n > 0);
  for (const a of tickers) if (a.name || a.sector) names[a.ticker] = { name: String(a.name || ''), sector: String(a.sector || '') };
  const bucketMs = (Number(data.bucket_s) || 900) * 1000;
  const agg = {
    buckets: (Array.isArray(data.buckets) ? data.buckets : [])
      .filter(b => b && Number.isFinite(b.t))
      .map(b => ({ t: b.t * 1000, n: b.n | 0, bull: b.bull | 0, bear: b.bear | 0, mat: b.mat | 0 })),
    tickers,
    sectors: (Array.isArray(data.sectors) ? data.sectors : []).filter(s => s && typeof s.sector === 'string' && s.n > 0),
    sector_other: data.sector_other | 0,
    day: data.day && typeof data.day === 'object' ? data.day : { n: items.length, bull: 0, bear: 0, mat: 0 },
    total: Number.isFinite(data.total) ? data.total : items.length,
    bucketMs,
    since: Number(data.since) * 1000 || null,
    until: Number(data.until) * 1000 || Date.now()
  };
  const { fresh, newCount } = markFresh(items);
  set({ status: 'ok', error: null, mode: 'flow', items, agg, names, asOf: Date.now(), fresh, newCount, sel: refocus(items) });
}

function spanBounds(span, now = Date.now()) {
  if (span === '24h') return [now - 86400000, now];
  const d = new Date(now);
  d.setHours(0, 0, 0, 0);
  return [d.getTime(), now];
}

function applyFeed(rawItems) {
  for (const it of normItems(rawItems)) feedPool.set(it.key, it);
  if (feedPool.size > FEED_KEEP) {
    const keep = [...feedPool.values()].sort(byTimeDesc).slice(0, FEED_KEEP);
    feedPool = new Map(keep.map(i => [i.key, i]));
  }
  const items = [...feedPool.values()].sort(byTimeDesc);
  const [since, until] = spanBounds(state.span);
  const agg = aggregate(items, since, until, 900000);
  const { fresh, newCount } = markFresh(items);
  set({ status: 'ok', error: null, mode: 'feed', items, agg, asOf: Date.now(), fresh, newCount, sel: refocus(items) });
}

/** Load the base data (flow, else the /api/news fallback). One in flight at a time. */
export function load() {
  if (inflight) return inflight;
  inflight = (async () => {
    const mock = isMockMode();
    const now = Date.now();
    let done = false;
    if (!flowForcedOff() && (mock || now - flowFailAt > FLOW_RETRY_MS)) {
      const url = mock ? fixtureUrl('news_flow') : `/api/news/flow?span=${encodeURIComponent(state.span)}&limit=${BASE_LIMIT}`;
      const r = await getJson(url);
      const u = unwrap(r.body, r.status);
      if (u.kind === 'ok' && u.data && Array.isArray(u.data.items)) {
        applyFlow(u.data);
        done = true;
      } else if (r.status === 404 || r.status === 405 || (r.status >= 200 && r.status < 300)) {
        flowFailAt = now; // route not live yet (restart pending) or not the expected shape: fall back for a while
      }
    }
    if (!done) {
      const r = await getJson(mock ? fixtureUrl('news') : '/api/news');
      if (r.body && Array.isArray(r.body.items)) applyFeed(r.body.items);
      else if (!state.items.length) set({ status: 'error', error: r.error || `news feed unavailable (HTTP ${r.status || '—'})` });
      else set({ error: r.error || `refresh failed (HTTP ${r.status || '—'})` }); // keep the last good data
    }
    qCache.clear();
    if (hasSelection(state.sel)) scheduleQuery(0);
  })().finally(() => {
    inflight = null;
  });
  return inflight;
}

// ── polling, gated on visibility + on-screen widgets ───────────────────────────
const holders = new Map(); // token → screen store | null
let timer = null;
let offVis = null;
const screenOffs = new Map();
const anyOnScreen = () => [...holders.values()].some(s => !s || s.get());
const active = () => !isHidden() && anyOnScreen();
function tick() {
  if (active()) load();
}
function resume() {
  if (!holders.size) return;
  if (active() && Date.now() - state.asOf > POLL_MS - 1000) load();
}
/** A widget joins (its WidgetCtx.screen store, or null = always on screen). Returns release(). */
export function acquire(screen) {
  const token = {};
  holders.set(token, screen || null);
  if (screen && typeof screen.subscribe === 'function') screenOffs.set(token, screen.subscribe(resume));
  if (holders.size === 1) {
    timer = setInterval(tick, POLL_MS);
    offVis = onVisibility(resume);
    if (!state.asOf || Date.now() - state.asOf > POLL_MS - 1000) load();
  }
  return () => {
    holders.delete(token);
    const off = screenOffs.get(token);
    if (off) off();
    screenOffs.delete(token);
    if (!holders.size) {
      clearInterval(timer);
      timer = null;
      if (offVis) offVis();
      offVis = null;
    }
  };
}

/** React: the store state; mounting the hook keeps the store polling (see acquire). */
export function useNews() {
  const ctx = useContext(WidgetCtx);
  const screen = (ctx && ctx.screen) || null;
  useEffect(() => acquire(screen), [screen]);
  return useSyncExternalStore(subscribe, getState, getState);
}

// ── selection ─────────────────────────────────────────────────────────────────
function setSel(patch) {
  const sel = { ...state.sel, ...patch };
  set({ sel });
  if (!('focus' in patch) || Object.keys(patch).length > 1) scheduleQuery();
}
export function setCat(cat) {
  setSel({ cat: CAT_IDS.includes(cat) ? cat : 'all' });
}
export function toggleTicker(t) {
  const n = normTk(t);
  setSel({ ticker: n && state.sel.ticker !== n ? n : null, sector: null });
}
export function toggleSector(s) {
  const v = typeof s === 'string' && s ? s.slice(0, 48) : null;
  setSel({ sector: v && state.sel.sector !== v ? v : null, ticker: null });
}
export function setRange(r) {
  const ok = Array.isArray(r) && Number.isFinite(r[0]) && Number.isFinite(r[1]) && r[1] > r[0];
  const cur = state.sel.range;
  if (ok && cur && cur[0] === r[0] && cur[1] === r[1]) return;
  if (!ok && !cur) return;
  setSel({ range: ok ? [r[0], r[1]] : null });
}
export function clearSelection() {
  setSel({ ticker: null, sector: null, range: null });
}
export function focusItem(it) {
  if (!it || typeof it !== 'object' || !it.key) return;
  setSel({ focus: it });
  if (typeof window !== 'undefined') {
    try {
      window.dispatchEvent(new CustomEvent('mason:news-focus', { detail: { key: it.key } }));
    } catch {
      /* old browsers */
    }
  }
}
export function clearFocus() {
  setSel({ focus: null });
}
export function setSpan(span) {
  const v = span === '24h' ? '24h' : 'today';
  if (v === state.span) return;
  try {
    localStorage.setItem(SPAN_KEY, v);
  } catch {
    /* private mode */
  }
  seeded = false; // a wider window is not "new arrivals"
  feedPool = new Map();
  qCache.clear();
  set({ span: v, sel: { ...state.sel, range: null }, q: EMPTY_Q, fresh: new Map(), newCount: 0 });
  load();
}

// Server query for the active selection (flow mode only; mock + feed mode filter locally).
let qTimer = null;
function scheduleQuery(delay = QUERY_DEBOUNCE_MS) {
  clearTimeout(qTimer);
  qTimer = setTimeout(runQuery, delay);
}
function runQuery() {
  const s = state;
  if (!hasSelection(s.sel) || s.mode !== 'flow' || isMockMode()) {
    if (s.q !== EMPTY_Q) set({ q: EMPTY_Q });
    return;
  }
  const key = selKey(s.sel, s.span);
  const hit = qCache.get(key);
  if (hit && Date.now() - hit.at < POLL_MS) {
    if (s.q.key !== key || s.q.items !== hit.items) set({ q: { key, status: 'ok', items: hit.items, total: hit.total, error: null } });
    return;
  }
  const p = new URLSearchParams({ agg: '0', limit: String(QUERY_LIMIT), span: s.span });
  if (s.sel.ticker) p.set('ticker', s.sel.ticker);
  if (s.sel.sector) p.set('sector', s.sel.sector);
  if (s.sel.range) {
    p.set('from', String(s.sel.range[0] / 1000));
    p.set('to', String(s.sel.range[1] / 1000));
  }
  set({ q: { key, status: 'loading', items: null, total: 0, error: null } });
  getJson(`/api/news/flow?${p.toString()}`).then(r => {
    if (state.q.key !== key) return; // the selection moved on
    const u = unwrap(r.body, r.status);
    if (u.kind === 'ok' && u.data && Array.isArray(u.data.items)) {
      const items = normItems(u.data.items).sort(byTimeDesc);
      const total = Number.isFinite(u.data.items_total) ? u.data.items_total : items.length;
      qCache.set(key, { items, total, at: Date.now() });
      if (qCache.size > 12) qCache.delete(qCache.keys().next().value);
      set({ q: { key, status: 'ok', items, total, error: null } });
    } else {
      set({ q: { key, status: 'error', items: null, total: 0, error: u.error || 'query failed' } });
    }
  });
}

/** Items for a ticker across the span (focus card's related stories). Flow mode only; cached 2 min. */
const relCache = new Map();
export function fetchTickerItems(ticker) {
  const t = normTk(ticker);
  if (!t || state.mode !== 'flow' || isMockMode()) return Promise.resolve(null);
  const key = `${state.span}|${t}`;
  const hit = relCache.get(key);
  if (hit && Date.now() - hit.at < POLL_MS) return hit.p;
  const p = getJson(`/api/news/flow?agg=0&limit=12&span=${encodeURIComponent(state.span)}&ticker=${encodeURIComponent(t)}`).then(r => {
    const u = unwrap(r.body, r.status);
    return u.kind === 'ok' && u.data && Array.isArray(u.data.items) ? normItems(u.data.items).sort(byTimeDesc) : null;
  });
  relCache.set(key, { p, at: Date.now() });
  if (relCache.size > 24) relCache.delete(relCache.keys().next().value);
  return p;
}

// A time-only window (no ticker / sector) is counted exactly from the river's buckets, at once.
const rangeOnly = s => !!(s.sel.range && !s.sel.ticker && !s.sel.sector && s.agg && s.agg.buckets.length);
function bucketSum(s) {
  let n = 0;
  for (const b of s.agg.buckets) if (b.t >= s.sel.range[0] && b.t < s.sel.range[1]) n += b.n;
  return n;
}

// ── derived views (memoized per state object, so every widget shares one computation) ──
const memo = new WeakMap();
export function derive(s) {
  let d = memo.get(s);
  if (d) return d;
  const selOn = hasSelection(s.sel);
  const qOk = selOn && s.q.status === 'ok' && s.q.key === selKey(s.sel, s.span) && Array.isArray(s.q.items);
  const scope = !selOn ? s.items : qOk ? s.q.items : filterLocal(s.items, s.sel, s.names);
  const view = scope.filter(catMatch(s.sel.cat));
  d = {
    scope, // selection applied, every category
    view, // selection + category chip
    counts: catCounts(scope),
    book: view.filter(i => i.book),
    tape: view.filter(i => !i.book),
    lead: pickLead(view),
    scopeTotal: selOn ? (rangeOnly(s) ? bucketSum(s) : qOk ? s.q.total : scope.length) : s.agg ? s.agg.total : s.items.length,
    selOn,
    selLoading: selOn && s.q.status === 'loading'
  };
  memo.set(s, d);
  return d;
}

/** Tests only. */
export function __resetNewsStore() {
  clearTimeout(qTimer);
  clearInterval(timer);
  timer = null;
  if (offVis) offVis();
  offVis = null;
  holders.clear();
  screenOffs.clear();
  seeded = false;
  seen = new Set();
  feedPool = new Map();
  flowFailAt = 0;
  inflight = null;
  qCache.clear();
  relCache.clear();
  state = initial();
  subs.forEach(fn => fn());
}
export const __test = { applyFlow, applyFeed, markFresh, spanBounds };
