// Data hook for GET /api/agent_loop/status. Not useRhApi: that hook unwraps the RH envelope
// ({ok, data, ...}) and this route returns its fields at the top level.
//  - Polls every 60 s (the backend caches 30 s); never while the tab is hidden or the widget is off
//    screen; on return it refetches once if the last fetch is older than the poll interval.
//  - {ok:false}, a non-object body, an HTTP error, a timeout or a network failure → 'error'. After a
//    good response the last good data stays on screen and `failed` is set instead.
//  - An identical poll (same JSON) keeps the same state object: no re-render.
//  - Mock mode (?rhx_mock=1) reads <static/ui>/fixtures/agent_loop_status.json.
import { useContext, useEffect, useRef, useState } from 'react';
import { fixtureUrl, isMockMode, routeSlug } from '../../lib/api.js';
import { WidgetCtx } from '../../lib/context.js';
import { isHidden, onVisibility } from '../../lib/visibility.js';

export const ROUTE = '/api/agent_loop/status';
export const POLL_MS = 60000;
const TIMEOUT_MS = 10000;

async function fetchStatus(url) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: ctl ? ctl.signal : undefined
    });
    const body = await res.json();
    const good = res.ok !== false && body && typeof body === 'object' && !Array.isArray(body) && body.ok !== false;
    return good ? body : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** → {status: 'loading' | 'ok' | 'error', data, failed} */
export function useLoopStatus() {
  const [state, setState] = useState({ status: 'loading', data: null, failed: false });
  const { refreshSignal, screen } = useContext(WidgetCtx);
  const forceRef = useRef(null);

  useEffect(() => {
    let alive = true;
    let busy = false;
    let lastAt = 0;
    let lastJson = null;
    let timer = null;
    const url = isMockMode() ? fixtureUrl(routeSlug(ROUTE)) : ROUTE;

    const load = async () => {
      if (busy) return;
      busy = true;
      lastAt = Date.now();
      const body = await fetchStatus(url);
      busy = false;
      if (!alive) return;
      if (!body) {
        setState(s => (s.data ? (s.failed ? s : { ...s, failed: true }) : s.status === 'error' ? s : { status: 'error', data: null, failed: true }));
        return;
      }
      const json = JSON.stringify(body);
      setState(s => {
        if (s.status === 'ok' && !s.failed && json === lastJson) return s;
        lastJson = json;
        return { status: 'ok', data: body, failed: false };
      });
    };
    forceRef.current = load;

    const active = () => !isHidden() && (!screen || screen.get());
    const start = () => {
      if (!timer) timer = setInterval(() => active() && load(), POLL_MS);
    };
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const resume = () => {
      if (!active()) return stop();
      start();
      if (Date.now() - lastAt >= POLL_MS) load();
      return undefined;
    };
    load(); // the first load always runs, so a widget scrolled into view already has data
    if (active()) start();
    const offVis = onVisibility(resume);
    const offScreen = screen ? screen.subscribe(resume) : () => {};
    return () => {
      alive = false;
      stop();
      offVis();
      offScreen();
    };
  }, [screen]);

  // MasonUI.mountWidget(...).refresh() bumps refreshSignal
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    if (forceRef.current) forceRef.current();
  }, [refreshSignal]);

  return state;
}
