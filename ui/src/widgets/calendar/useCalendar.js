// Data hook for GET /api/calendar (+ /api/calendar/meta) and the three write calls.
// Contract: docs/fable/CALENDAR_API.md. Not useRhApi: these routes return their fields at the top
// level, and a 404 here has its own meaning (the service has not been restarted since the calendar
// was installed).
//  - Loads on mount, then every 5 minutes; after a failure it tries again after 1 minute. No timer runs
//    while the tab is hidden or the widget is off screen. When the tab comes back it reloads if the data
//    is older than 30 seconds. There is never more than one timer, and never a per-second one.
//  - The hundreds of small drug-trial dates are not downloaded unless asked for: with `trials` off the
//    window is one request with trials=0, and the server leaves them out (2026-11-16; a server from before
//    sends them anyway and calUtil.applyBase hides them, so the screen is the same).
//  - The answer's `held_tickers` (every stock Nico or Mason holds) marks the held stock chips; without it the
//    widget falls back to guessing from the events (calUtil.heldSet).
//  - An identical refresh keeps the same `events` array, so nothing below re-renders.
//  - Mock mode (?rhx_mock=1) reads <static/ui>/fixtures/calendar.json and calendar_meta.json; writes
//    are refused there.
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { fixtureUrl, isMockMode } from '../../lib/api.js';
import { WidgetCtx } from '../../lib/context.js';
import { isHidden, onVisibility } from '../../lib/visibility.js';
import { TYPE_ORDER, TYPE_LABELS, cleanHeld, etToday, isGone, isObj, isYmd, mergeEvents, writeErrorText } from './calUtil.js';

export const ROUTE = '/api/calendar';
export const META_ROUTE = '/api/calendar/meta';
export const POLL_MS = 300000;
export const RETRY_MS = 60000;
export const RESUME_MS = 30000;
const TIMEOUT_MS = 15000;

function qs(route, params) {
  const q = new URLSearchParams(params).toString();
  return q ? `${route}?${q}` : route;
}

/** The request for one window (a list, so a window could again be split into several). */
export function eventUrls({ from, to, mine, trials }) {
  return [qs(ROUTE, { from, to, ...(mine ? { held: '1' } : {}), ...(trials ? {} : { trials: '0' }) })];
}

/** → {status, body}. status 0 = no answer (offline, timeout); body null = not JSON. Never throws. */
async function callJson(url, method = 'GET', payload) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
  try {
    const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl ? ctl.signal : undefined };
    if (payload !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(payload);
    }
    const res = await fetch(url, init);
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return { status: typeof res.status === 'number' ? res.status : res.ok ? 200 : 500, body };
  } catch {
    return { status: 0, body: null };
  } finally {
    clearTimeout(timer);
  }
}
const good = r => r.status >= 200 && r.status < 300 && isObj(r.body);

/** /api/calendar/meta → what the widget uses: chip labels, counts for the window, sources that failed. */
export function cleanMeta(body) {
  if (!isObj(body) || !Array.isArray(body.types)) return null;
  const labels = {};
  const counts = {};
  for (const t of body.types) {
    if (!isObj(t) || !TYPE_ORDER.includes(t.type)) continue;
    labels[t.type] = typeof t.label === 'string' && t.label.trim() ? t.label.trim().slice(0, 40) : TYPE_LABELS[t.type];
    counts[t.type] = typeof t.count === 'number' && Number.isFinite(t.count) && t.count >= 0 ? Math.round(t.count) : null;
  }
  const behind = [];
  if (isObj(body.feeds)) {
    for (const f of Object.values(body.feeds)) {
      if (isObj(f) && f.on !== false && f.ok === false && typeof f.label === 'string' && f.label.trim()) behind.push(f.label.trim().slice(0, 60));
    }
  }
  return { labels, counts, behind };
}

const EMPTY = [];
const INITIAL = { status: 'loading', events: EMPTY, meta: null, held: null, today: null, key: '', at: 0, failed: false };

/**
 * useCalendar({from, to, mine, trials}) → (from / to: the first and last box of the month on screen)
 *   {status: 'loading' | 'ok' | 'off' | 'error', events, meta, held, today, at, failed, busy, reload(), write(method, body)}
 *   'off' = the routes are not there yet (HTTP 404). `failed` = the last refresh failed but older data is shown.
 *   `held` = the server's list of held stocks (null from a server that does not send one).
 *   `busy` = the data on screen belongs to an earlier choice of dates / filters and the new one is loading.
 *   write() → {ok: true, event} | {ok: false, error: 'plain sentence'}; a good write reloads before it resolves.
 */
export function useCalendar({ from, to, mine, trials }) {
  const [state, setState] = useState(INITIAL);
  const { refreshSignal, screen } = useContext(WidgetCtx);
  const loadRef = useRef(null);
  const key = `${from}|${to}|${mine ? 1 : 0}|${trials ? 1 : 0}`;

  useEffect(() => {
    let alive = true;
    let inflight = null;
    let again = false;
    let timer = null;
    let lastAt = 0;
    let lastBad = false;
    let lastJson = null;
    const mock = isMockMode();
    const active = () => !isHidden() && (!screen || screen.get());
    const clear = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const plan = () => {
      clear();
      if (!alive || !active()) return;
      const due = lastAt + (lastBad ? RETRY_MS : POLL_MS) - Date.now();
      timer = setTimeout(() => load(), Math.max(1000, due));
    };

    async function once() {
      clear();
      lastAt = Date.now();
      const urls = mock ? [fixtureUrl('calendar')] : eventUrls({ from, to, mine, trials });
      const metaUrl = mock ? fixtureUrl('calendar_meta') : qs(META_ROUTE, { from, to });
      const [metaRes, ...evRes] = await Promise.all([callJson(metaUrl), ...urls.map(u => callJson(u))]);
      if (!alive) return;
      const off = evRes.some(r => r.status === 404);
      const ok = !off && evRes.every(r => good(r) && Array.isArray(r.body.events));
      lastBad = !ok && !off;
      if (ok) {
        const events = mergeEvents(...evRes.map(r => r.body.events));
        const serverToday = evRes.map(r => r.body.today).find(isYmd) || etToday();
        const meta = good(metaRes) ? cleanMeta(metaRes.body) : null;
        const held = evRes.map(r => cleanHeld(r.body.held_tickers)).find(Array.isArray) || null;
        const json = JSON.stringify([events, meta, serverToday, held]);
        const same = json === lastJson;
        lastJson = json;
        setState(s => ({
          status: 'ok',
          events: same && s.status === 'ok' ? s.events : events,
          meta: same && s.status === 'ok' ? s.meta : meta || (s.key === key ? s.meta : null),
          held: same && s.status === 'ok' ? s.held : held,
          today: serverToday,
          key,
          at: Date.now(),
          failed: false
        }));
      } else if (off) {
        lastJson = null;
        setState(s => (s.status === 'off' ? s : { ...INITIAL, status: 'off' }));
      } else {
        // keep whatever is on screen; only an empty widget shows the error message
        setState(s => (s.status === 'ok' ? (s.failed ? s : { ...s, failed: true }) : s.status === 'error' ? s : { ...INITIAL, status: 'error' }));
      }
    }
    // One request round at a time. A call made while one is running asks for one more round after it,
    // and gets the same promise, so a caller that awaits it sees data newer than its own write.
    function load() {
      if (inflight) {
        again = true;
        return inflight;
      }
      inflight = (async () => {
        do {
          again = false;
          await once();
        } while (again && alive);
      })().finally(() => {
        inflight = null;
        plan();
      });
      return inflight;
    }
    loadRef.current = load;

    load(); // the first load always runs, so a widget scrolled into view already has data
    const offVis = onVisibility(() => {
      if (!active()) return clear();
      if (Date.now() - lastAt >= RESUME_MS) load();
      else plan();
      return undefined;
    });
    const offScreen = screen
      ? screen.subscribe(() => {
          if (!active()) return clear();
          if (Date.now() - lastAt >= (lastBad ? RETRY_MS : POLL_MS)) load();
          else plan();
          return undefined;
        })
      : () => {};
    return () => {
      alive = false;
      clear();
      offVis();
      offScreen();
      if (loadRef.current === load) loadRef.current = null;
    };
  }, [screen, from, to, mine, trials, key]);

  // MasonUI.mountWidget(...).refresh() bumps refreshSignal
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    if (loadRef.current) loadRef.current();
  }, [refreshSignal]);

  const reload = useCallback(() => (loadRef.current ? loadRef.current() : Promise.resolve()), []);
  const write = useCallback(async (method, body) => {
    if (isMockMode()) return { ok: false, error: 'This is a preview with sample data, so changes are not saved.' };
    const r = await callJson(ROUTE, method, body);
    if (good(r) && typeof r.body.error !== 'string') {
      if (loadRef.current) await loadRef.current();
      return { ok: true, event: r.body.event || null };
    }
    const gone = isGone(r.status, r.body);
    if (gone && loadRef.current) await loadRef.current();
    return { ok: false, error: writeErrorText(r.status, r.body), gone };
  }, []);

  return { ...state, busy: state.status === 'ok' && state.key !== key, reload, write };
}
