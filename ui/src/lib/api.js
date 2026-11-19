// useRhApi: the ONE data hook for RH widgets (S3 §2 "Data", contracts §6).
//
//  - Unwraps {ok, as_of, data, stale, errors}. {enabled:false} → status 'off' + the flag to flip.
//  - status: 'loading' | 'ok' | 'off' | 'error'.  stale = envelope.stale || errors.length || last poll failed.
//  - 10 s timeout (AbortController).
//  - ONE in-flight request and a 15 s cache per URL (route + sorted params), shared by every
//    instance of every widget: N widgets on one route = 1 request.
//  - Optional polling ({pollMs}); never polls while the document is hidden or (S3-F6) while the
//    widget is off screen (WidgetCtx.screen); on return, refetches once if the cache is older than the TTL.
//  - S3-F6: an identical poll result (same data, stale, errors; as_of on the same UTC date) keeps the
//    previous state object, so it causes no re-render; `data` keeps its identity (structural sharing).
//  - Mock mode (?rhx_mock=1): GET <static/ui>/fixtures/<route_slug>.json instead of the API.
//    Slug rule: drop the query, drop a leading /api/, lowercase, runs of non [a-z0-9] → "_",
//    trim "_".  /api/rh/alerts/log → rh_alerts_log ; /api/scanners → scanners.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { isHidden, onVisibility } from './visibility.js';
import { WidgetCtx } from './context.js';

export const CACHE_TTL_MS = 15000;
export const TIMEOUT_MS = 10000;
const MAX_ENTRIES = 64;

// ── config ──────────────────────────────────────────────────────────────────
let assetBase = '/static/ui/';
let mockOverride = null;

/** Set by entry.js from import.meta.url (the directory that holds mason-ui.js). */
export function setAssetBase(base) {
  assetBase = String(base).endsWith('/') ? String(base) : `${base}/`;
}
export const getAssetBase = () => assetBase;
/** Tests/harness: force mock mode on/off (null = follow the URL). */
export function setMockMode(on) {
  mockOverride = on == null ? null : !!on;
}
export function isMockMode() {
  if (mockOverride != null) return mockOverride;
  try {
    return new URLSearchParams(window.location.search).get('rhx_mock') === '1';
  } catch {
    return false;
  }
}

// ── route helpers ───────────────────────────────────────────────────────────
export function routeSlug(route) {
  return String(route || '')
    .split('?')[0]
    .replace(/^\/?api\//i, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

// Owning config flag per route (contracts §6 module column → §7 config block).
const FLAG_RULES = [
  [/^\/api\/rh\/history\b/, 'rh_expansion.history'],
  [/^\/api\/rh\/excursions\b/, 'rh_expansion.option_bars'],
  [/^\/api\/kb\/priors\b/, 'rh_expansion.priors'],
  [/^\/api\/rh\/congress\b/, 'rh_expansion.congress'],
  [/^\/api\/rh\/crowd\b/, 'rh_expansion.crowd'],
  [/^\/api\/rh\/behavior\b/, 'rh_expansion.behavior'],
  [/^\/api\/rh\/whatif\b/, 'rh_expansion.whatif'],
  [/^\/api\/rh\/pvr\b/, 'rh_expansion.pvr'],
  [/^\/api\/rh\/(accounts|exposure|earnings|alerts)\b/, 'rh_expansion.exposure'],
  [/^\/api\/rh\/status\b/, 'rh_expansion.ui'],
  [/^\/api\/scanners\b/, 'scanners'],
  [/^\/api\/watchlist\b/, 'watchlist']
];
export function routeFlag(route) {
  const r = String(route || '').split('?')[0];
  const hit = FLAG_RULES.find(([re]) => re.test(r));
  return hit ? hit[1] : 'rh_expansion';
}

export function buildUrl(route, params) {
  const base = String(route || '');
  if (!params || typeof params !== 'object') return base;
  const keys = Object.keys(params)
    .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
    .sort();
  if (!keys.length) return base;
  const q = keys.map(k => `${encodeURIComponent(k)}=${encodeURIComponent(String(params[k]))}`).join('&');
  return `${base}${base.includes('?') ? '&' : '?'}${q}`;
}

export const fixtureUrl = slug => `${assetBase}fixtures/${slug}.json`;

// ── fetch + envelope ────────────────────────────────────────────────────────
const errText = e => (e && e.name === 'AbortError' ? 'timed out after 10 s' : String((e && e.message) || e || 'error'));
const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const asErrors = v => (Array.isArray(v) ? v.map(x => (typeof x === 'string' ? x : JSON.stringify(x))) : []);

// F0b: a non-envelope error body (e.g. a route's own 409 {"message": "Watchlist is full: 25/25"})
// carries its reason in message / error / reason (that order — finalization RULINGS 15). Surface the
// first plain string (trimmed, ≤ 300 chars) so the UI says why, not just "HTTP 409". Callers render
// it as JSX text (escaped), never as HTML.
const MAX_REASON = 300;
function plainReason(body, keys = ['message', 'error', 'reason']) {
  for (const k of keys) {
    const v = body[k];
    if (typeof v === 'string' && v.trim()) {
      const t = v.trim();
      return t.length > MAX_REASON ? `${t.slice(0, MAX_REASON)}…` : t;
    }
  }
  return null;
}

/** Normalize one response body into {kind:'ok'|'off'|'error', ...}. Never throws. */
export function unwrap(body, httpStatus = 200) {
  if (!isObj(body)) return { kind: 'error', error: `bad response (HTTP ${httpStatus})` };
  if (body.enabled === false) return { kind: 'off' };
  const errors = asErrors(body.errors);
  if (httpStatus >= 400 || body.ok === false) {
    return { kind: 'error', error: errors.length ? errors.join('; ') : plainReason(body) || `HTTP ${httpStatus}`, errors };
  }
  return {
    kind: 'ok',
    data: body.data === undefined ? null : body.data,
    message: plainReason(body, ['message']),
    asOf: typeof body.as_of === 'string' ? body.as_of : null,
    stale: body.stale === true || errors.length > 0,
    errors
  };
}

async function fetchJson(url, init = {}) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    // headers merged LAST so a caller's init.headers can add to, not replace, Accept
    const res = await fetch(url, {
      credentials: 'same-origin',
      ...init,
      headers: { Accept: 'application/json', ...(init.headers || {}) },
      signal: ctl ? ctl.signal : undefined
    });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return unwrap(body, res.status || (res.ok ? 200 : 500));
  } catch (e) {
    return { kind: 'error', error: errText(e) };
  } finally {
    clearTimeout(timer);
  }
}

// ── shared cache ────────────────────────────────────────────────────────────
// key → {result, t, promise, subs:Set<fn>}
const cache = new Map();

function entry(key) {
  let e = cache.get(key);
  if (!e) {
    e = { result: null, t: 0, promise: null, subs: new Set(), lastGood: null };
    cache.set(key, e);
    if (cache.size > MAX_ENTRIES) {
      for (const [k, v] of cache) {
        if (cache.size <= MAX_ENTRIES) break;
        if (!v.subs.size && !v.promise) cache.delete(k);
      }
    }
  } else {
    cache.delete(key); // LRU touch
    cache.set(key, e);
  }
  return e;
}

export function __resetApiCache() {
  cache.clear();
}

const now = () => Date.now();

// Cache age is measured from when the request STARTED (e.t), so a poll tick every pollMs sees an
// age of ~pollMs no matter how slow the response was. maxAge: a poll passes min(TTL, pollMs - 250)
// so pollMs <= TTL still refetches on schedule (DEBUG_F0 F2); everyone else uses the 15 s TTL.
function load(key, url, { force = false, maxAge = CACHE_TTL_MS } = {}) {
  const e = entry(key);
  if (e.promise) return e.promise;
  if (!force && e.result && now() - e.t < maxAge) return Promise.resolve(e.result);
  e.t = now();
  e.promise = fetchJson(url).then(r => {
    e.promise = null;
    if (r.kind === 'ok') {
      share(r, e.lastGood);
      e.lastGood = r;
    }
    e.result = r;
    e.subs.forEach(fn => fn(r));
    return r;
  });
  return e.promise;
}

// S3-F6 structural sharing: a poll whose data (or errors) is JSON-identical to the last good result
// reuses the previous objects, so `data` keeps its identity across identical polls (memoized children
// and the bail-out in useRhApi see "nothing changed").
function share(r, prev) {
  let json;
  try {
    json = JSON.stringify(r.data);
  } catch {
    return;
  }
  Object.defineProperty(r, 'dataJson', { value: json, enumerable: false });
  if (!prev) return;
  if (prev.dataJson === json) r.data = prev.data;
  if (JSON.stringify(prev.errors) === JSON.stringify(r.errors)) r.errors = prev.errors;
}

const day = v => (typeof v === 'string' ? v.slice(0, 10) : v);
/** Same visible state? asOf counts only by its UTC date: an identical poll whose only change is the
 *  server's response time keeps the old state object, so it causes no re-render (S3-F6 perf pass). */
function sameState(a, b) {
  return (
    a.status === b.status &&
    a.data === b.data &&
    a.stale === b.stale &&
    a.error === b.error &&
    a.flag === b.flag &&
    a.mock === b.mock &&
    day(a.asOf) === day(b.asOf) &&
    (a.errors === b.errors || JSON.stringify(a.errors) === JSON.stringify(b.errors))
  );
}

function toState(r, flag, mock) {
  const base = { flag, mock };
  if (!r) return { ...base, status: 'loading', data: null, asOf: null, stale: false, errors: [], error: null };
  return { ...base, ...stateFrom(r) };
}
function stateFrom(r) {
  if (r.kind === 'off') return { status: 'off', data: null, asOf: null, stale: false, errors: [], error: null };
  if (r.kind === 'ok') return { status: 'ok', data: r.data, asOf: r.asOf, stale: r.stale, errors: r.errors, error: null };
  return { status: 'error', data: null, asOf: null, stale: false, errors: r.errors || [], error: r.error };
}

/**
 * useRhApi(route, params?, {pollMs?, flag?})
 *   → {status, data, asOf, stale, errors, error, flag, mock, refresh()}
 * A failed poll after a good one keeps the good data: status stays 'ok', stale=true, error set.
 */
export function useRhApi(route, params = null, opts = {}) {
  const mock = isMockMode();
  const url = buildUrl(route, params);
  const key = mock ? `mock:${routeSlug(route)}` : url;
  const fetchUrl = mock ? fixtureUrl(routeSlug(route)) : url;
  const flag = opts.flag || routeFlag(route);
  const pollMs = Number(opts.pollMs) > 0 ? Math.max(1000, Number(opts.pollMs)) : 0;

  const compute = useCallback(() => {
    const e = cache.get(key);
    if (!e || !e.result) return toState(null, flag, mock);
    const r = e.result;
    if (r.kind === 'error' && e.lastGood) {
      return { ...toState(e.lastGood, flag, mock), stale: true, error: r.error };
    }
    return toState(r, flag, mock);
  }, [key, flag, mock]);

  const [state, setState] = useState(compute);
  const stateRef = useRef(state);
  const alive = useRef(true);
  const { refreshSignal, screen } = useContext(WidgetCtx);

  useEffect(() => {
    alive.current = true;
    const e = entry(key);
    // compare against the committed state (a ref), not inside a functional updater: an updater that
    // returns `prev` can still make React render this hook's component once before bailing out
    const update = () => {
      const next = compute();
      if (stateRef.current && sameState(stateRef.current, next)) return;
      stateRef.current = next;
      setState(next);
    };
    const sub = () => {
      if (alive.current) update();
    };
    e.subs.add(sub);
    update();
    // the first load always runs (an off-screen widget still has data when scrolled in)
    if (!isHidden() || !e.result) load(key, fetchUrl);

    // S3-F6: poll only while the tab is visible AND the widget is on screen
    const onScreen = () => !screen || screen.get();
    const active = () => !isHidden() && onScreen();
    const pollMaxAge = Math.min(CACHE_TTL_MS, pollMs - 250);
    let timer = null;
    const startPoll = () => {
      if (!pollMs || timer) return;
      timer = setInterval(() => {
        if (active()) load(key, fetchUrl, { maxAge: pollMaxAge });
      }, pollMs);
    };
    const stopPoll = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const resume = () => {
      if (!active()) {
        stopPoll();
        return;
      }
      startPoll();
      load(key, fetchUrl); // TTL-guarded: only refetches if the cache is old
    };
    if (active()) startPoll();
    const offVis = onVisibility(resume);
    const offScreen = screen ? screen.subscribe(resume) : () => {};
    return () => {
      alive.current = false;
      e.subs.delete(sub);
      stopPoll();
      offVis();
      offScreen();
    };
  }, [key, fetchUrl, pollMs, compute, screen]);

  const refresh = useCallback(() => {
    const e = cache.get(key);
    // Several instances refreshing together still share one request (the in-flight guard).
    if (e && e.promise) return e.promise;
    return load(key, fetchUrl, { force: true });
  }, [key, fetchUrl]);

  // MasonUI.mountWidget(...).refresh() → force-refetch (skips the first render).
  const seenSignal = useRef(refreshSignal);
  useEffect(() => {
    if (refreshSignal === seenSignal.current) return;
    seenSignal.current = refreshSignal;
    refresh();
  }, [refreshSignal, refresh]);

  return { ...state, refresh };
}

/**
 * postRh(route, body, {mockSlug?}) → {ok, data, error, message}. JSON POST, 10 s timeout, envelope
 * unwrap. `error` (non-2xx / ok:false) = envelope errors, else body.message || body.error ||
 * body.reason, else "HTTP n". `message` (2xx) = body.message of a non-envelope route (e.g. the
 * watchlist routes' one-line result), else null. Both are plain text — render as text, never HTML.
 * No optimistic writes: callers show `error` inline and only update UI on ok.
 * Mock mode GETs fixtures/<mockSlug || slug + "_post">.json and never writes.
 */
export async function postRh(route, body, { mockSlug } = {}) {
  let r;
  if (isMockMode()) {
    r = await fetchJson(fixtureUrl(mockSlug || `${routeSlug(route)}_post`), { method: 'GET' });
  } else {
    r = await fetchJson(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body == null ? {} : body)
    });
  }
  if (r.kind === 'ok') return { ok: true, data: r.data, error: null, message: r.message || null };
  if (r.kind === 'off') return { ok: false, data: null, error: `Off — enable ${routeFlag(route)}`, message: null };
  return { ok: false, data: null, error: r.error, message: null };
}
