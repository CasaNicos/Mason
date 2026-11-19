// Data hook for GET /api/health, shared by every API-page widget (UI overhaul Task 6).
// Not useRhApi: that hook unwraps the RH envelope ({ok, data, ...}); this route returns its fields at the
// top level (same reason as learning_loop/useLoopStatus.js).
//  - ONE store per URL for the whole page: the constellation, the summary and the source list make one
//    request per tick between them (in-flight dedupe + a TTL just under the poll period).
//  - Polls every 45 s, the template's API-page cadence. Never while the tab is hidden or the widget is off
//    screen (its page is not shown); on return it refetches once if the data is older than 45 s.
//  - HTTP error / non-JSON / timeout / {error, no sources} → 'error'. After a good answer the last good
//    model stays on screen with `failed` set instead (the UI shows a "stale" badge, text not color).
//  - An unchanged source keeps its object identity across polls (health.js normalizeHealth).
//  - Mock mode (?rhx_mock=1) reads <static/ui>/fixtures/health.json.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { fixtureUrl, isMockMode, routeSlug } from '../../lib/api.js';
import { WidgetCtx } from '../../lib/context.js';
import { isHidden, onVisibility } from '../../lib/visibility.js';
import { normalizeHealth, POLL_MS, ROUTE } from './health.js';

const TIMEOUT_MS = 10000;
const stores = new Map(); // url → {url, result, lastGood, t, promise, subs}

function storeFor(url) {
  let s = stores.get(url);
  if (!s) {
    s = { url, result: null, lastGood: null, t: 0, promise: null, subs: new Set() };
    stores.set(url, s);
  }
  return s;
}

/** Tests only. */
export function __resetHealthStore() {
  stores.clear();
}

async function fetchBody(url) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: ctl ? ctl.signal : undefined
    });
    if (res.ok === false) return { ok: false, error: `HTTP ${res.status}` };
    const body = await res.json();
    return { ok: true, body, at: Date.now() };
  } catch (e) {
    return { ok: false, error: e && e.name === 'AbortError' ? 'timed out after 10 s' : 'network error' };
  } finally {
    clearTimeout(timer);
  }
}

function load(s, { force = false, maxAge = POLL_MS - 1500 } = {}) {
  if (s.promise) return s.promise;
  if (!force && s.result && Date.now() - s.t < maxAge) return Promise.resolve(s.result);
  s.t = Date.now();
  s.promise = fetchBody(s.url).then(r => {
    s.promise = null;
    let res;
    if (!r.ok) res = { kind: 'error', error: r.error };
    else {
      const model = normalizeHealth(r.body, r.at, s.lastGood ? s.lastGood.model : null);
      if (!model) res = { kind: 'error', error: 'bad response' };
      else if (model.error && !model.sources.length) res = { kind: 'error', error: 'health snapshot failed', detail: model.error };
      else res = { kind: 'ok', model };
    }
    if (res.kind === 'ok') s.lastGood = res;
    s.result = res;
    s.subs.forEach(fn => {
      try {
        fn();
      } catch {
        /* one subscriber never breaks the others */
      }
    });
    return res;
  });
  return s.promise;
}

const LOADING = { status: 'loading', model: null, failed: false, error: null };

function view(s) {
  const r = s.result;
  if (!r) return LOADING;
  if (r.kind === 'ok') return { status: 'ok', model: r.model, failed: false, error: null };
  if (s.lastGood) return { status: 'ok', model: s.lastGood.model, failed: true, error: r.error };
  return { status: 'error', model: null, failed: true, error: r.error };
}

const same = (a, b) => a.status === b.status && a.model === b.model && a.failed === b.failed && a.error === b.error;

/** → {status: 'loading'|'ok'|'error', model, failed, error, refresh()} */
export function useHealth() {
  const url = isMockMode() ? fixtureUrl(routeSlug(ROUTE)) : ROUTE;
  const store = storeFor(url);
  const [state, setState] = useState(() => view(store));
  const stateRef = useRef(state);
  const { refreshSignal, screen } = useContext(WidgetCtx);

  useEffect(() => {
    let alive = true;
    const update = () => {
      if (!alive) return;
      const next = view(store);
      if (same(stateRef.current, next)) return;
      stateRef.current = next;
      setState(next);
    };
    store.subs.add(update);
    update();
    load(store, { maxAge: POLL_MS }); // the first load always runs (TTL-guarded across widgets)

    const active = () => !isHidden() && (!screen || screen.get());
    let timer = null;
    const start = () => {
      if (!timer) timer = setInterval(() => active() && load(store), POLL_MS);
    };
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const resume = () => {
      if (!active()) return stop();
      start();
      load(store, { maxAge: POLL_MS });
      return undefined;
    };
    if (active()) start();
    const offVis = onVisibility(resume);
    const offScreen = screen ? screen.subscribe(resume) : () => {};
    return () => {
      alive = false;
      store.subs.delete(update);
      stop();
      offVis();
      offScreen();
    };
  }, [store, screen]);

  const refresh = useCallback(() => load(store, { force: true }), [store]);

  // MasonUI.mountWidget(...).refresh() bumps refreshSignal → force a refetch (not on the first render)
  const seen = useRef(refreshSignal);
  useEffect(() => {
    if (seen.current === refreshSignal) return;
    seen.current = refreshSignal;
    refresh();
  }, [refreshSignal, refresh]);

  return { ...state, refresh };
}
