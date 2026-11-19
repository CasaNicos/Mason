// <MasonChartHost chartId symbol tf type follow/>: candles on Mason's own canvas engine
// (window.MasonChart in templates/mason_v2.html). We never add a second chart library.
//
// MasonChart.create(opts) expects a MasonGrid `grid` and calls grid.place(id, node). Inside a
// React widget there is no grid (Gridstack owns the OUTER widget), so we pass a tiny shim whose
// place() appends the chart node into our host div, and whose drag/resize/collapse hooks are
// no-ops. Unmount → MasonChart.destroyAllIn(shim) drops the registry record and the node.
//
// - symbol / tf / type props are followed with setSymbol / setTf / setType.
// - follow (default true): also follows `mason:chart-symbol` events addressed to this chartId.
// - A ResizeObserver on the host calls MasonChart.redraw(id) (Gridstack resizes reflow us).
// - No window.MasonChart (e.g. harness without its stub, or the engine failed) → placeholder.
//   The harness ships a small stub (ui/harness/masonchart-stub.js) with the same API.
// - MasonChart persists per-id state in localStorage (mv_chart_state_v1), keyed by
//   "mui-chart-<chartId>", so give each chart a stable, unique chartId.
// - Markers (e.g. disclosure dates) are NOT supported by MasonChart today.
// - Ranges (ticker card, 2026-11-18), all optional: `range` (any string key), `limit` (bars to fetch, 1-1000) and
//   `view` (how many of the newest bars to show: a number, or view(bars) → number). With `range` set, a change of
//   range calls MasonChart.setWindow(id, {tf, limit, view}) once (one fetch) instead of setTf; an engine without
//   setWindow falls back to setTf. `persist={false}` keeps this chart out of mv_chart_state_v1 (create opts.persist).
import { useEffect, useRef, useState } from 'react';
import { onChartSymbol, normTicker } from './bridge.js';

const chartApi = () => (typeof window !== 'undefined' && window.MasonChart && typeof window.MasonChart.create === 'function' ? window.MasonChart : null);

function makeShim(el) {
  return {
    el,
    place(_id, node) {
      el.appendChild(node);
      return node;
    },
    isCollapsed: () => false,
    toggleCollapse() {},
    startDrag() {},
    startResize() {}
  };
}

export default function MasonChartHost({
  chartId,
  symbol,
  tf = '1d',
  type = 'candle',
  follow = true,
  className = '',
  style,
  range = null,
  limit,
  view,
  persist = true
}) {
  const hostRef = useRef(null);
  const shimRef = useRef(null);
  const [available] = useState(() => !!chartApi());
  const id = `mui-chart-${chartId}`;
  const sym = normTicker(symbol) || '';
  // latest window props, read by the create / range effects (a view function may change identity every render)
  const win = useRef({});
  win.current = { tf, limit, view };

  // create / destroy
  useEffect(() => {
    const api = chartApi();
    const host = hostRef.current;
    if (!api || !host) return undefined;
    const shim = makeShim(host);
    shimRef.current = shim;
    try {
      const opts = { id, grid: shim, headerMode: 'slot', kind: 'symbol', symbol: sym, tf, type };
      if (persist === false) opts.persist = false;
      if (Number.isFinite(win.current.limit)) opts.limit = win.current.limit;
      if (win.current.view != null) opts.view = win.current.view;
      api.create(opts);
      // create() restores the last persisted state for this id; the prop wins.
      const rec = typeof api.getRecord === 'function' ? api.getRecord(id) : null;
      if (sym && rec && rec.symbol !== sym) api.setSymbol(id, sym);
    } catch {
      /* engine error: the host stays empty rather than breaking the widget */
    }
    let ro = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        try {
          if (typeof api.redraw === 'function') api.redraw(id);
        } catch {
          /* ignore */
        }
      });
      ro.observe(host);
    }
    return () => {
      if (ro) ro.disconnect();
      try {
        if (typeof api.destroyAllIn === 'function') api.destroyAllIn(shim);
      } catch {
        /* ignore */
      }
      shimRef.current = null;
      while (host.firstChild) host.removeChild(host.firstChild);
    };
    // Re-create only when the chart identity changes; symbol/tf/type are followed below.
  }, [id]);

  // follow props
  const first = useRef(true);
  useEffect(() => {
    if (first.current) return;
    const api = chartApi();
    if (api && sym) api.setSymbol(id, sym);
  }, [id, sym]);
  useEffect(() => {
    if (first.current || range != null) return; // with a range, the range effect below owns tf
    const api = chartApi();
    if (api && typeof api.setTf === 'function') api.setTf(id, tf);
  }, [id, tf, range]);
  useEffect(() => {
    if (first.current || range == null) return;
    const api = chartApi();
    if (!api) return;
    const w = win.current;
    if (typeof api.setWindow === 'function') {
      const next = { tf: w.tf };
      if (Number.isFinite(w.limit)) next.limit = w.limit;
      if (w.view != null) next.view = w.view;
      api.setWindow(id, next);
    } else if (typeof api.setTf === 'function') api.setTf(id, w.tf);
  }, [id, range]);
  useEffect(() => {
    if (first.current) return;
    const api = chartApi();
    if (api && typeof api.setType === 'function') api.setType(id, type);
  }, [id, type]);
  useEffect(() => {
    first.current = false;
  }, []);

  // follow bridge events for this chart
  useEffect(() => {
    if (!follow) return undefined;
    return onChartSymbol(
      ({ ticker }) => {
        const api = chartApi();
        if (api) api.setSymbol(id, ticker);
      },
      { chartId }
    );
  }, [follow, chartId, id]);

  if (!available) {
    return (
      <div className={`mui-chart-host mui-chart-host--placeholder ${className}`} style={style} role="img" aria-label={`Chart unavailable for ${sym || 'no symbol'}`}>
        <span className="mui-chart-host__msg">Chart unavailable — MasonChart not loaded</span>
        {sym ? <b className="mui-chart-host__sym">{sym}</b> : null}
      </div>
    );
  }
  return <div ref={hostRef} className={`mui-chart-host ${className}`} style={style} data-chart-id={chartId} />;
}
