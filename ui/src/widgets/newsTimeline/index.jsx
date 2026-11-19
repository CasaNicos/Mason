// newsTimeline — the Sentiment river (News page, UI overhaul Task 5, 2026-11-16).
// Today's news flow as a river: the faint envelope is the story count per 15-minute slice, the green
// bank above the line is bullish stories, the red bank below is bearish, ◆ marks slices with material
// stories. Scrub: press and drag across the river to select a time window (a click selects one slice;
// clicking the selected slice again clears it). Every News widget follows the window. Keyboard: the
// river is a slider — ←/→ move a slice, Shift+←/→ extend, Home/End, Enter toggles, Esc clears.
// With a ticker / sector picked in the Impact map, a bright line overlays that pick's own flow.
// X axis: Task 3's timeTicks util (pickScaleTicks: labels thin out by measured pixel width, never
// squish); the crosshair readout uses its fullLabel().
// Built here (no React Bits fit: there is no chart/river component in the catalog; the bundle forbids
// a second chart library). The one-time draw-in reveal is a CSS keyframe, off under reduced motion.
import { useMemo, useRef, useState } from 'react';
import { num } from '../../lib/fmt.js';
import { fullLabel, makeMeasure, pickScaleTicks } from '../../lib/timeTicks.js';
import { derive, setRange, useNews } from '../newsShared/store.js';
import { clock, clockRange } from '../newsShared/util.js';
import { NewsState } from '../newsShared/parts.jsx';
import { useBox } from '../newsShared/useBox.js';
import { overlayPath, river } from './river.js';
import '../newsShared/news.css';
import './newsTimeline.css';

const AXIS_H = 20;
const PAD_X = 12;
const PAD_TOP = 10;
const measure = makeMeasure('10px Inter, sans-serif');

function sliceText(b, bucketMs) {
  if (!b) return '';
  return `${clock(b.t)}–${clock(b.t + bucketMs)}: ${num(b.n)} stories, ▲ ${num(b.bull)} bullish, ▼ ${num(b.bear)} bearish${b.mat ? `, ◆ ${num(b.mat)} material` : ''}`;
}

export default function NewsTimeline() {
  const s = useNews();
  const d = derive(s);
  const { ref: stageCb, el: stageRef, box } = useBox();
  const [hoverX, setHoverX] = useState(null);
  const [cursor, setCursor] = useState(null);
  const anchor = useRef(null);
  const drag = useRef(null);

  const agg = s.agg;
  const buckets = agg ? agg.buckets : [];
  const bucketMs = agg ? agg.bucketMs : 900000;
  const t0 = agg ? agg.since : 0;
  const t1 = agg ? agg.until : 0;
  const W = box.w;
  const H = box.h;
  const x0 = PAD_X;
  const x1 = Math.max(x0 + 1, W - PAD_X);
  const y0 = PAD_TOP;
  const y1 = Math.max(y0 + 1, H - AXIS_H);
  const yMid = (y0 + y1) / 2;
  const half = (y1 - y0) / 2;
  const ready = !!(agg && buckets.length && W > 40 && H > 40 && t1 > t0);

  const g = useMemo(
    () => (ready ? river(buckets, { t0, t1, bucketMs, x0, x1, yMid, half }) : null),
    [ready, buckets, t0, t1, bucketMs, x0, x1, yMid, half]
  );
  const ticks = useMemo(() => (g ? pickScaleTicks({ t0, t1, x0, x1, xOfTime: g.xOf, measure, minGap: 12 }).ticks : []), [g, t0, t1, x0, x1]);
  const pickOn = !!(s.sel.ticker || s.sel.sector);
  const overlay = useMemo(() => {
    if (!g || !pickOn) return '';
    const counts = new Array(buckets.length).fill(0);
    for (const it of d.scope) {
      if (it.t == null) continue;
      const i = Math.floor((it.t - t0) / bucketMs);
      if (i >= 0 && i < counts.length) counts[i] += 1;
    }
    return overlayPath(counts, g.xs, yMid, half);
  }, [g, pickOn, d.scope, buckets.length, t0, bucketMs, yMid, half]);

  const range = s.sel.range;
  const rangeFor = (ia, ib) => {
    const a = Math.min(ia, ib);
    const b = Math.max(ia, ib);
    return [t0 + a * bucketMs, Math.min(t1, t0 + (b + 1) * bucketMs)];
  };
  const idxOfT = t => Math.max(0, Math.min(buckets.length - 1, Math.floor((t - t0) / bucketMs)));
  const xFrom = e => {
    const r = stageRef.current ? stageRef.current.getBoundingClientRect() : { left: 0 };
    return Math.max(x0, Math.min(x1, e.clientX - r.left));
  };
  const singleSel = i => !!range && idxOfT(range[0]) === i && range[1] - range[0] <= bucketMs + 1;

  const onPointerDown = e => {
    if (!g || (e.button != null && e.button !== 0)) return;
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events */
    }
    const i = g.indexAt(xFrom(e));
    drag.current = { i0: i, moved: false, wasSel: singleSel(i) };
    anchor.current = i;
    setCursor(i);
    setRange(rangeFor(i, i));
  };
  const onPointerMove = e => {
    if (!g) return;
    const x = xFrom(e);
    setHoverX(x);
    if (drag.current) {
      const i = g.indexAt(x);
      if (i !== drag.current.i0) drag.current.moved = true;
      setCursor(i);
      setRange(rangeFor(drag.current.i0, i));
    }
  };
  const onPointerUp = () => {
    if (drag.current && !drag.current.moved && drag.current.wasSel) {
      setRange(null);
      setCursor(null);
    }
    drag.current = null;
  };
  const onKeyDown = e => {
    if (!g) return;
    const nb = buckets.length;
    const cur = cursor != null ? cursor : range ? idxOfT(range[0]) : nb - 1;
    let i = cur;
    if (e.key === 'ArrowLeft') i = Math.max(0, cur - 1);
    else if (e.key === 'ArrowRight') i = Math.min(nb - 1, cur + 1);
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = nb - 1;
    else if (e.key === 'Escape') {
      if (range) {
        e.preventDefault();
        setRange(null);
      }
      setCursor(null);
      return;
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (singleSel(cur)) setRange(null);
      else {
        anchor.current = cur;
        setRange(rangeFor(cur, cur));
      }
      setCursor(cur);
      return;
    } else return;
    e.preventDefault();
    if (e.shiftKey) {
      if (anchor.current == null) anchor.current = cur;
      setRange(rangeFor(anchor.current, i));
    } else {
      anchor.current = i;
      setRange(rangeFor(i, i));
    }
    setCursor(i);
  };

  const hi = g && hoverX != null ? g.indexAt(hoverX) : null;
  const ri = hi != null ? hi : cursor;
  const rb = ri != null && ri >= 0 ? buckets[ri] : null;
  const rangeStats = useMemo(() => {
    if (!range || !buckets.length) return null;
    const a = { n: 0, bull: 0, bear: 0, mat: 0 };
    for (const b of buckets) {
      if (b.t >= range[0] && b.t < range[1]) {
        a.n += b.n;
        a.bull += b.bull;
        a.bear += b.bear;
        a.mat += b.mat;
      }
    }
    return a;
  }, [range, buckets]);
  const rx0 = g && range ? Math.max(x0, g.xOf(range[0])) : null;
  const rx1 = g && range ? Math.min(x1, g.xOf(range[1])) : null;
  const hoverT = g && hoverX != null ? g.tOf(hoverX) : null;
  const valueText = rb ? sliceText(rb, bucketMs) : 'No slice selected';
  const spanWord = s.span === '24h' ? 'last 24 hours' : 'today';

  return (
    <div className="nw nw-tl">
      <div className="nw-tl__bar">
        <span className="nw-tl__legend">
          <span className="nw-tl__key nw-tl__key--up">▲ bullish</span>
          <span className="nw-tl__key nw-tl__key--down">▼ bearish</span>
          <span className="nw-tl__key nw-tl__key--vol">volume</span>
          <span className="nw-tl__key nw-tl__key--mat">◆ material</span>
          {pickOn ? <span className="nw-tl__key nw-tl__key--pick">— {s.sel.ticker || s.sel.sector}</span> : null}
        </span>
        {range && rangeStats ? (
          <button type="button" className="nw-pill" onClick={() => setRange(null)} aria-label={`Clear the time window ${clockRange(range)}`}>
            ◷ {clockRange(range)} · {num(rangeStats.n)} stories · ▲{num(rangeStats.bull)} ▼{num(rangeStats.bear)} <span aria-hidden="true">✕</span>
          </button>
        ) : (
          <span className="nw-tl__hint">Drag across the river to scrub {spanWord}</span>
        )}
      </div>
      <NewsState s={s} empty={agg && !buckets.length ? 'No stories in this window yet.' : null}>
        <div
          className={`nw-tl__stage${drag.current ? ' is-drag' : ''}`}
          ref={stageCb}
          role="slider"
          tabIndex={0}
          aria-label={`Sentiment river, ${spanWord}, 15-minute slices. Arrow keys move, Shift extends, Enter selects, Escape clears.`}
          aria-valuemin={0}
          aria-valuemax={Math.max(0, buckets.length - 1)}
          aria-valuenow={ri != null && ri >= 0 ? ri : undefined}
          aria-valuetext={valueText}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onPointerLeave={() => setHoverX(null)}
          onKeyDown={onKeyDown}
          onBlur={() => setCursor(null)}
        >
          {g ? (
            <svg className="nw-tl__svg" width={W} height={H} viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
              {ticks.map(tk => (
                <line key={`g${tk.t}`} className={`nw-tl__grid${tk.level > 1 ? ' is-major' : ''}`} x1={tk.x} x2={tk.x} y1={y0} y2={y1} />
              ))}
              <line className="nw-tl__mid" x1={x0} x2={x1} y1={yMid} y2={yMid} />
              {/* .nw-tl__flow reveals left→right once on mount (CSS clip-path keyframe; none under reduced motion) */}
              <g className="nw-tl__flow">
                <path className="nw-tl__env" d={g.env} />
                <path className="nw-tl__bull" d={g.bull} />
                <path className="nw-tl__bear" d={g.bear} />
                {overlay ? <path className="nw-tl__pick" d={overlay} /> : null}
                {g.mats.map(m => (
                  <path key={`m${m.i}`} className="nw-tl__mat" d={`M${m.x},${y0 + 2}l4,4l-4,4l-4,-4z`} />
                ))}
              </g>
              <line className="nw-tl__now" x1={g.xEnd} x2={g.xEnd} y1={y0} y2={y1} />
              {range && rx0 != null ? (
                <>
                  <rect className="nw-tl__dim" x={x0} y={y0} width={Math.max(0, rx0 - x0)} height={y1 - y0} />
                  <rect className="nw-tl__dim" x={rx1} y={y0} width={Math.max(0, x1 - rx1)} height={y1 - y0} />
                  <rect className="nw-tl__sel" x={rx0} y={y0} width={Math.max(2, rx1 - rx0)} height={y1 - y0} />
                </>
              ) : null}
              {hoverX != null ? <line className="nw-tl__cross" x1={hoverX} x2={hoverX} y1={y0} y2={y1} /> : null}
              {ticks.map(tk => (
                <text key={`t${tk.t}`} className={`nw-tl__tick${tk.level > 1 ? ' is-major' : ''}`} x={tk.tx} y={H - 6} textAnchor={tk.anchor}>
                  {tk.label}
                </text>
              ))}
            </svg>
          ) : null}
          {g && rb ? (
            <div className="nw-tl__read" style={{ left: `${Math.max(90, Math.min(W - 90, hoverX != null ? hoverX : g.xs[ri] || 0))}px` }} aria-hidden="true">
              <b>{hoverT != null ? fullLabel(hoverT, { intraday: true }) : `${clock(rb.t)}–${clock(rb.t + bucketMs)}`}</b>
              <span>
                {num(rb.n)} stories · <span className="mui-up">▲ {num(rb.bull)}</span> · <span className="mui-down">▼ {num(rb.bear)}</span>
                {rb.mat ? <> · ◆ {num(rb.mat)}</> : null}
              </span>
            </div>
          ) : null}
        </div>
      </NewsState>
    </div>
  );
}
