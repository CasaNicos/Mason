// Data hook for GET /api/learning/scoreboard (contract: docs/fable/SCOREBOARD_API.md). Read-only route.
// Not useRhApi: this route returns its fields at the top level, not in the RH envelope.
//  - Loads once on mount, then every 10 minutes (the numbers only change overnight; the server caches
//    5 minutes). After a failure it tries again after 1 minute. Never more often than once a minute,
//    whatever asks (refresh(), the tab coming back, the widget scrolling into view).
//  - No timer runs while the tab is hidden or the widget is off screen; when it comes back it reloads
//    only if the data is older than the 10-minute interval. There is never more than one timer.
//  - {ok:false}, HTTP errors, a body that is not JSON, a timeout or no network → 'error' (one plain
//    message, never the server's error text). After a good load the data stays on screen and `failed`
//    is set instead.
//  - An identical answer keeps the same `data` object, so nothing below re-renders.
//  - Mock mode (?rhx_mock=1) reads <static/ui>/fixtures/learning_scoreboard.json.
import { useContext, useEffect, useRef, useState } from 'react';
import { fixtureUrl, isMockMode, routeSlug } from '../../lib/api.js';
import { WidgetCtx } from '../../lib/context.js';
import { isHidden, onVisibility } from '../../lib/visibility.js';
import { cleanScoreboard } from './sbUtil.js';

export const ROUTE = '/api/learning/scoreboard';
export const POLL_MS = 600000; // 10 minutes
export const RETRY_MS = 60000; // after a failure
export const MIN_GAP_MS = 60000; // the contract's floor: never poll faster than once a minute
const TIMEOUT_MS = 15000;

/** → the raw JSON body when the answer is usable, else null. Never throws. */
async function fetchBoard(url) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl ? ctl.signal : undefined });
    if (res.ok === false) return null;
    const body = await res.json();
    return body && typeof body === 'object' && !Array.isArray(body) && body.ok !== false ? body : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const INITIAL = { status: 'loading', data: null, failed: false, at: 0 };

/** → {status: 'loading' | 'ok' | 'error', data (cleanScoreboard model), failed, at} */
export function useScoreboard() {
  const [state, setState] = useState(INITIAL);
  const { refreshSignal, screen } = useContext(WidgetCtx);
  const forceRef = useRef(null);

  useEffect(() => {
    let alive = true;
    let busy = false;
    let lastAt = 0;
    let lastBad = false;
    let lastJson = null;
    let timer = null;
    const url = isMockMode() ? fixtureUrl(routeSlug(ROUTE)) : ROUTE;
    const active = () => !isHidden() && (!screen || screen.get());
    const clear = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const plan = (wait = lastBad ? RETRY_MS : POLL_MS) => {
      clear();
      if (!alive || !active()) return;
      const due = Math.max(lastAt + wait, lastAt + MIN_GAP_MS) - Date.now();
      timer = setTimeout(load, Math.max(1000, due));
    };

    async function load() {
      if (busy) return;
      clear();
      busy = true;
      lastAt = Date.now();
      const body = await fetchBoard(url);
      busy = false;
      if (!alive) return;
      const data = body ? cleanScoreboard(body) : null;
      lastBad = !data;
      if (!data) {
        setState(s => (s.data ? (s.failed ? s : { ...s, failed: true }) : s.status === 'error' ? s : { ...INITIAL, status: 'error' }));
      } else {
        const json = JSON.stringify(body);
        const same = json === lastJson;
        lastJson = json;
        const at = Date.now();
        setState(s => {
          if (same && s.status === 'ok') return s.failed ? { ...s, failed: false, at } : s; // nothing new: no re-render
          return { status: 'ok', data, failed: false, at };
        });
      }
      plan();
    }
    // refresh() / coming back on screen: load now if the floor allows, else at the floor
    const nudge = () => {
      if (!active()) return clear();
      if (Date.now() - lastAt >= MIN_GAP_MS) load();
      else plan(MIN_GAP_MS);
      return undefined;
    };
    forceRef.current = nudge;

    load(); // the first load always runs, so a widget scrolled into view already has data
    const resume = () => {
      if (!active()) return clear();
      if (Date.now() - lastAt >= (lastBad ? RETRY_MS : POLL_MS)) load();
      else plan();
      return undefined;
    };
    const offVis = onVisibility(resume);
    const offScreen = screen ? screen.subscribe(resume) : () => {};
    return () => {
      alive = false;
      clear();
      offVis();
      offScreen();
      if (forceRef.current === nudge) forceRef.current = null;
    };
  }, [screen]);

  // MasonUI.mountWidget(...).refresh() bumps refreshSignal (still held to once a minute)
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
