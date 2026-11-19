// Paper vs Real cumulative-return chart: React + SVG (no chart library; S3 §2 "Charts").
// Dataviz method (~/.cache/rhx_s3/dataviz, read before writing this):
//  - ONE y-axis (cumulative %), solid hairline grid, a stronger zero line.
//  - Two series, each with its own encoding beyond color: Mason paper = dashed line + circle
//    markers; You (real) = solid line + square markers. Each is direct-labelled at its end
//    (leader line when the two end labels would collide). Legend lives in the widget header.
//  - Crosshair snaps to the nearest session; ONE tooltip lists both series. Keyboard: focus the
//    chart, ←/→/Home/End move the crosshair, Esc clears. Same readout as hover.
//  - Null values (e.g. paper_cum null) break the line; nothing is interpolated.
//  - A visually-hidden table twin carries every value for screen readers.
//  - Reflows on resize (ResizeObserver on its own box; works inside any Gridstack cell).
import { useEffect, useMemo, useRef, useState } from 'react';
import { DASH, date, isNum, pct } from '../../lib/fmt.js';
import { makeMeasure, pickTimeTicks } from '../../lib/timeTicks.js';

const M = { top: 22, left: 40, bottom: 20 };
const LABEL_H = 24;
const MARKER_MAX = 60; // per-session markers only while legible (and the DOM stays small)
const TICK_MAX = 12;
// x-axis labels: shared picker (lib/timeTicks.js, UI overhaul Task 3) — measured, never overlapping,
// coarser nice interval (day → week → month → quarter → year) as the chart narrows. Font = .mui-pvr__tick.
const tickMeasure = makeMeasure('10px Inter, sans-serif');

function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * mag;
}

function scaleY(values, h) {
  const vs = values.filter(isNum);
  let lo = Math.min(0, ...vs);
  let hi = Math.max(0, ...vs);
  if (hi - lo < 0.5) {
    hi += 0.25;
    lo -= 0.25;
  }
  const step = niceStep(hi - lo, h < 110 ? 2 : 4);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const ticks = [];
  if (![step, lo, hi].every(Number.isFinite) || step <= 0) return { lo: -1, hi: 1, ticks: [-1, 0, 1], y: v => M.top + (1 - (v + 1) / 2) * h };
  for (let t = lo, k = 0; t <= hi + step / 2 && k < TICK_MAX; t += step, k++) ticks.push(Math.round(t / step) * step);
  return { lo, hi, ticks, y: v => M.top + (1 - (v - lo) / (hi - lo || 1)) * h };
}

// Split a series into drawable runs, breaking at nulls.
function runs(points) {
  const out = [];
  let cur = [];
  for (const p of points) {
    if (p) cur.push(p);
    else if (cur.length) {
      out.push(cur);
      cur = [];
    }
  }
  if (cur.length) out.push(cur);
  return out;
}
const pathOf = run => run.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('');

function useBox(ref) {
  const [box, setBox] = useState({ w: 360, h: 150 });
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(entries => {
      const r = entries[0] && entries[0].contentRect;
      if (!r || r.width < 10 || r.height < 10) return;
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      setBox(b => (b.w === w && b.h === h ? b : { w, h }));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return box;
}

/**
 * rows: [{date, paper, real, paperDay, realDay}] (cumulative % and the day's % move)
 * labels: {paper: 'Mason paper', real: 'You · Mine'}
 */
export default function PvrChart({ rows, labels }) {
  const wrap = useRef(null);
  const svgRef = useRef(null);
  const { w, h: boxH } = useBox(wrap);
  const [idx, setIdx] = useState(null);
  const n = rows.length;
  const hasPaper = rows.some(r => isNum(r.paper));
  const hasReal = rows.some(r => isNum(r.real));

  const gutter = Math.max(76, Math.min(118, Math.round(w * 0.24)));
  const plotW = Math.max(40, w - M.left - gutter);
  const plotH = Math.max(40, boxH - M.top - M.bottom);
  const geo = useMemo(() => {
    const sy = scaleY(rows.flatMap(r => [r.paper, r.real]), plotH);
    const x = i => M.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const pts = key => rows.map((r, i) => (isNum(r[key]) ? [x(i), sy.y(r[key])] : null));
    return { sy, x, paper: pts('paper'), real: pts('real') };
  }, [rows, n, plotW, plotH]);

  // end labels: last non-null point of each series; push apart + leader line when they collide
  const ends = useMemo(() => {
    const last = key => {
      for (let i = n - 1; i >= 0; i--) if (isNum(rows[i][key])) return { i, v: rows[i][key] };
      return null;
    };
    const out = [];
    const p = last('paper');
    const r = last('real');
    if (p) out.push({ key: 'paper', name: labels.paper, v: p.v, x: geo.x(p.i), y: geo.sy.y(p.v) });
    if (r) out.push({ key: 'real', name: labels.real, v: r.v, x: geo.x(r.i), y: geo.sy.y(r.v) });
    out.forEach(o => (o.ly = o.y));
    if (out.length === 2 && Math.abs(out[0].ly - out[1].ly) < LABEL_H) {
      const [a, b] = out[0].ly <= out[1].ly ? [out[0], out[1]] : [out[1], out[0]];
      const mid = (a.ly + b.ly) / 2;
      a.ly = mid - LABEL_H / 2;
      b.ly = mid + LABEL_H / 2;
    }
    const top = M.top + 8;
    const bot = M.top + plotH - 4;
    out.forEach(o => (o.ly = Math.max(top, Math.min(bot, o.ly))));
    return out;
  }, [rows, n, labels, geo]);

  const move = i => setIdx(n ? Math.max(0, Math.min(n - 1, i)) : null);
  const onPointer = e => {
    const svg = svgRef.current;
    if (!svg || !n) return;
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / (rect.width || 1)) * w;
    move(n <= 1 ? 0 : Math.round(((px - M.left) / plotW) * (n - 1)));
  };
  const onKey = e => {
    if (!n) return;
    const cur = idx == null ? n - 1 : idx;
    const k = e.key;
    if (k === 'ArrowLeft') move(idx == null ? n - 1 : cur - 1);
    else if (k === 'ArrowRight') move(idx == null ? n - 1 : cur + 1);
    else if (k === 'Home') move(0);
    else if (k === 'End') move(n - 1);
    else if (k === 'Escape') setIdx(null);
    else return;
    e.preventDefault();
  };

  const xTicks = useMemo(
    () => (n ? pickTimeTicks({ times: rows.map(r => r.date), xOf: geo.x, x0: M.left, x1: M.left + plotW, measure: tickMeasure, utc: true }).ticks : []),
    [rows, n, geo, plotW]
  );
  const sel = idx != null && rows[idx] ? rows[idx] : null;
  const selX = sel ? geo.x(idx) : 0;
  const tipLeft = sel ? Math.min(Math.max(selX / w, 0.02), 0.98) : 0;

  // Screen-reader table twin. Memoized on the data: fmt's Intl-based date()/pct() are costly, and a
  // crosshair move must not re-format every session (10k-row profile: ~120 ms per move before).
  // div wrapper: tables ignore overflow:hidden, so .mui-sr must clip a block, not the table.
  const srTable = useMemo(
    () => (
        <div className="mui-sr">
        <table>
          <caption>Cumulative return by session</caption>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">{labels.paper}</th>
              <th scope="col">{labels.real}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{date(r.date)}</td>
                <td>{isNum(r.paper) ? pct(r.paper, { digits: 2 }) : DASH}</td>
                <td>{isNum(r.real) ? pct(r.real, { digits: 2 }) : DASH}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
    ),
    [rows, labels]
  );

  return (
    <div className="mui-pvr__chartbox" ref={wrap}>
      <svg
        ref={svgRef}
        className="mui-pvr__chart"
        width={w}
        height={boxH}
        viewBox={`0 0 ${w} ${boxH}`}
        role="group"
        tabIndex={0}
        aria-label={`Cumulative return (%) by trading session: ${labels.paper} vs ${labels.real}. Use the left and right arrow keys to read each session.`}
        onPointerMove={onPointer}
        onPointerDown={onPointer}
        onPointerLeave={() => setIdx(null)}
        onKeyDown={onKey}
        onFocus={() => idx == null && n && setIdx(n - 1)}
        onBlur={() => setIdx(null)}
      >
        <g className="mui-pvr__yaxis">
          {geo.sy.ticks.map(t => (
            <g key={t}>
              <line className={t === 0 ? 'mui-pvr__zero' : 'mui-pvr__grid'} x1={M.left} x2={M.left + plotW} y1={geo.sy.y(t)} y2={geo.sy.y(t)} />
              <text className="mui-pvr__tick" x={M.left - 6} y={geo.sy.y(t)} dy="0.32em" textAnchor="end">
                {pct(t, { digits: Math.abs(t) < 1 && t !== 0 ? 1 : 0 })}
              </text>
            </g>
          ))}
          <text className="mui-pvr__axislabel" x={4} y={9}>
            cum. %
          </text>
        </g>
        <g className="mui-pvr__xaxis">
          {xTicks.map(tk => (
            <text key={tk.i} className="mui-pvr__tick" x={tk.tx} y={M.top + plotH + 14} textAnchor={tk.anchor}>
              {tk.label}
            </text>
          ))}
        </g>
        {hasPaper ? (
          <g data-series="paper" className="mui-pvr__s mui-pvr__s--paper">
            {runs(geo.paper).map((run, k) => (
              <path key={k} className="mui-pvr__line" d={pathOf(run)} strokeDasharray="5 4" />
            ))}
            {n <= MARKER_MAX && geo.paper.map((p, i) => (p ? <circle key={i} className="mui-pvr__mk" cx={p[0]} cy={p[1]} r={idx === i ? 5 : 3.5} /> : null))}
          </g>
        ) : null}
        {hasReal ? (
          <g data-series="real" className="mui-pvr__s mui-pvr__s--real">
            {runs(geo.real).map((run, k) => (
              <path key={k} className="mui-pvr__line" d={pathOf(run)} />
            ))}
            {n <= MARKER_MAX && geo.real.map((p, i) => {
              if (!p) return null;
              const s = idx === i ? 9 : 7;
              return <rect key={i} className="mui-pvr__mk" x={p[0] - s / 2} y={p[1] - s / 2} width={s} height={s} />;
            })}
          </g>
        ) : null}
        {ends.map(o => (
          <g key={o.key} className={`mui-pvr__end mui-pvr__end--${o.key}`}>
            {Math.abs(o.ly - o.y) > 2 ? <line className="mui-pvr__leader" x1={o.x + 5} y1={o.y} x2={M.left + plotW + 6} y2={o.ly} /> : null}
            <text className="mui-pvr__endlabel" x={M.left + plotW + 8} y={o.ly}>
              <tspan className="mui-pvr__endname" x={M.left + plotW + 8} dy="-0.25em">
                {o.name}
              </tspan>
              <tspan className="mui-pvr__endval" x={M.left + plotW + 8} dy="1.15em">
                {pct(o.v, { digits: 2 })}
              </tspan>
            </text>
          </g>
        ))}
        {sel ? <line className="mui-pvr__cross" x1={selX} x2={selX} y1={M.top} y2={M.top + plotH} /> : null}
      </svg>
      {sel ? (
        <div className="mui-pvr__tip" data-testid="pvr-tip" role="status" style={{ left: `${tipLeft * 100}%` }}>
          <div className="mui-pvr__tipdate">{date(sel.date)}</div>
          <div className="mui-pvr__tiprow">
            <span className="mui-pvr__key mui-pvr__key--paper" aria-hidden="true" />
            <b>{pct(sel.paper, { digits: 2 })}</b> <span className="mui-muted">{labels.paper}</span>
            <span className="mui-n"> day {pct(sel.paperDay, { digits: 2 })}</span>
          </div>
          <div className="mui-pvr__tiprow">
            <span className="mui-pvr__key mui-pvr__key--real" aria-hidden="true" />
            <b>{pct(sel.real, { digits: 2 })}</b> <span className="mui-muted">{labels.real}</span>
            <span className="mui-n"> day {pct(sel.realDay, { digits: 2 })}</span>
          </div>
        </div>
      ) : null}
      {srTable}
    </div>
  );
}
