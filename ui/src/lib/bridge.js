// React ⇄ vanilla bridge (bundle design §3). Widgets talk to the template and to each other
// ONLY through these window CustomEvents, so neither side imports the other.
//   mason:chart-symbol  {chartId, ticker}   "chart this ticker in chart <chartId>"
//   mason:go            {page, focus}       "open tab <page>, focus item <focus>"  (template router)
import { useEffect, useState } from 'react';

export const CHART_SYMBOL_EVENT = 'mason:chart-symbol';
export const GO_EVENT = 'mason:go';

const MAX_TICKER = 32; // OCC option symbols are 21 chars; anything longer is junk

/** Uppercased, trimmed ticker or null. Accepts letters, digits, space . - / ^ (BRK.B, ^VIX, OCC). */
export function normTicker(t) {
  if (typeof t !== 'string') return null;
  const s = t.trim().toUpperCase();
  if (!s || s.length > MAX_TICKER || !/^[A-Z0-9 .\-/^]+$/.test(s)) return null;
  return s;
}

const str = v => (v == null ? null : String(v));

export function emitChartSymbol(chartId, ticker) {
  const t = normTicker(ticker);
  if (!t) return false;
  window.dispatchEvent(new CustomEvent(CHART_SYMBOL_EVENT, { detail: { chartId: str(chartId), ticker: t } }));
  return true;
}

/** fn({chartId, ticker}); with {chartId} only events for that chart. Returns unsubscribe. */
export function onChartSymbol(fn, { chartId } = {}) {
  const h = e => {
    const d = e && e.detail;
    if (!d || !normTicker(d.ticker)) return;
    if (chartId != null && d.chartId !== String(chartId)) return;
    fn({ chartId: d.chartId, ticker: normTicker(d.ticker) });
  };
  window.addEventListener(CHART_SYMBOL_EVENT, h);
  return () => window.removeEventListener(CHART_SYMBOL_EVENT, h);
}

// S3-F6: mason:go replay. A go event usually *causes* its target page to mount (the template router
// opens the tab), so the page's widgets subscribe after the event fired. The bridge records the last
// go event (from emitGo or any raw window CustomEvent) and onGo(fn, {replay:true}) hands a recent one
// (≤ GO_REPLAY_MS old) to a late subscriber, once, on a microtask. This is the page-wide event bus, not
// widget state: one bundle per page, so one recorder.
export const GO_REPLAY_MS = 10000;
let last = null; // {page, focus, t}
function record(e) {
  const d = e && e.detail;
  if (!d || typeof d.page !== 'string' || !d.page) return;
  last = { page: d.page, focus: d.focus == null ? null : String(d.focus), t: Date.now() };
}
if (typeof window !== 'undefined') window.addEventListener(GO_EVENT, record);

/** The last mason:go seen on this page: {page, focus, t} or null. */
export const lastGo = () => (last ? { ...last } : null);
/** Tests only: forget the recorded go event. */
export function __resetBridge() {
  last = null;
}

export function emitGo(page, focus = null) {
  if (typeof page !== 'string' || !page) return false;
  window.dispatchEvent(new CustomEvent(GO_EVENT, { detail: { page, focus: str(focus) } }));
  return true;
}

/** fn({page, focus}). {replay:true} also delivers the last go event if it is ≤ GO_REPLAY_MS old.
 *  Returns unsubscribe. */
export function onGo(fn, { replay = false } = {}) {
  let live = true;
  const h = e => {
    const d = e && e.detail;
    if (!d || typeof d.page !== 'string') return;
    fn({ page: d.page, focus: d.focus == null ? null : String(d.focus) });
  };
  window.addEventListener(GO_EVENT, h);
  if (replay && last && Date.now() - last.t <= GO_REPLAY_MS) {
    const ev = last;
    Promise.resolve().then(() => {
      // skip when unsubscribed meanwhile, or when a newer event already reached this subscriber
      if (live && last === ev) fn({ page: ev.page, focus: ev.focus });
    });
  }
  return () => {
    live = false;
    window.removeEventListener(GO_EVENT, h);
  };
}

/** React: current ticker for a chart, following mason:chart-symbol for that chartId. */
export function useChartSymbol(chartId, initial = null) {
  const [ticker, setTicker] = useState(normTicker(initial));
  useEffect(() => onChartSymbol(d => setTicker(d.ticker), { chartId }), [chartId]);
  return ticker;
}
