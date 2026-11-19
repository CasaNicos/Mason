// API-page focus bridge (UI overhaul Task 6). The constellation and the source list are separate React
// roots (one per Gridstack widget), so "which source is focused" travels as a window CustomEvent, the
// same pattern as lib/bridge.js (mason:chart-symbol / mason:go):
//   mason:api-focus  {name: <source name> | CORE | null, origin}
// Each widget instance keeps its own state and follows the events; `last` is only the bus's memory, so a
// widget mounted later starts on the current focus.
import { useCallback, useEffect, useState } from 'react';

export const API_FOCUS_EVENT = 'mason:api-focus';
export const CORE = '__core__';
const NAME_RE = /^[A-Za-z0-9_.:\-]{1,64}$/;

let last = null; // {name, origin, t}

const clean = name => (typeof name === 'string' && NAME_RE.test(name) ? name : null);

export function emitApiFocus(name, origin = '') {
  const n = clean(name);
  last = { name: n, origin: String(origin || ''), t: Date.now() };
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(API_FOCUS_EVENT, { detail: { name: n, origin: last.origin } }));
  }
}

export function onApiFocus(fn) {
  const h = e => {
    const d = e && e.detail;
    if (!d) return;
    fn({ name: clean(d.name), origin: String(d.origin || '') });
  };
  window.addEventListener(API_FOCUS_EVENT, h);
  return () => window.removeEventListener(API_FOCUS_EVENT, h);
}

export const lastApiFocus = () => (last ? { ...last } : null);

/** Tests only. */
export function __resetApiFocus() {
  last = null;
}

/** [focusedName | null, setFocus(name | null)] synced across the page's API widgets. */
export function useApiFocus(origin) {
  const [focus, setLocal] = useState(() => (last ? last.name : null));
  useEffect(() => onApiFocus(({ name }) => setLocal(name)), []);
  const setFocus = useCallback(name => emitApiFocus(name, origin), [origin]);
  return [focus, setFocus];
}
