// Shared crowd page UI (UI overhaul Task 4, 2026-11-16): the state wrapper + blocked card (moved here from
// crowdFeed/crowdShared.jsx), the glossary <Term> tooltip, the rank-change badge, the "yours" marks and the
// rank-path sparkline. Every string renders through JSX (escaped); nothing here sets HTML.
//
// <Term k="ci">95% CI</Term>: no React Bits piece is a tooltip, so this one is ours. It is a focusable span
// (dotted underline) that shows a small card on hover, keyboard focus or tap, rendered in a portal on
// document.body (so a widget's overflow:hidden never clips it) and closed by Escape, blur or any scroll.
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import GlassSurface from '../../reactbits/GlassSurface/GlassSurface.jsx';
import { RhState } from '../../lib/states.jsx';
import { dateTime, isNum } from '../../lib/fmt.js';
import { fullLabel, makeMeasure, pickTimeTicks } from '../../lib/timeTicks.js';
import { FOLLOW_FLAG, isBlocked, obj, shortDate, str } from './data.js';
import { termOf } from './glossary.js';
import './crowd.css';

/**
 * status:"blocked_needs_follow" (S2-T2, contracts §2.1 / Q4). Text only: there is deliberately NO
 * follow control anywhere. `compact` is the catalog-size version.
 */
export function BlockedCard({ compact = false, follow = null }) {
  const f = obj(follow);
  const failed = follow && f.ok === false;
  const failLine = failed ? (
    <div className="mui-crowd-blocked__fail">
      Last follow attempt {dateTime(f.attempted_at)} failed: {str(f.error) || 'unknown error'}
    </div>
  ) : null;
  if (compact) {
    return (
      <div className="mui-crowd-blocked mui-crowd-blocked--compact" role="status">
        <span className="mui-crowd-blocked__icon" aria-hidden="true">○</span>
        <span>
          Crowd Tracker is off — &apos;100 Most Popular&apos; requires following it. Enable{' '}
          <code className="mui-code">{FOLLOW_FLAG}</code>.
        </span>
        {failLine}
      </div>
    );
  }
  return (
    <div className="mui-crowd-blocked" role="status">
      <GlassSurface width="100%" height="auto" borderRadius={14} className="mui-crowd-blocked__glass">
        <div className="mui-crowd-blocked__body">
          <span className="mui-crowd-blocked__icon" aria-hidden="true">○</span>
          <p className="mui-crowd-blocked__copy">
            Crowd Tracker is off: reading &apos;100 Most Popular&apos; requires following it. Enable{' '}
            <code className="mui-code">{FOLLOW_FLAG}</code> to turn it on.
          </p>
          {failLine}
        </div>
      </GlassSurface>
    </div>
  );
}

/**
 * Blocked wins over everything (even a stale badge or old events still in the payload): the page
 * is one card. Otherwise the standard RhState (loading / Off / error / stale badge / empty).
 */
export function CrowdState({ api, compact = false, empty, emptyText, loadingRows, children }) {
  if (isBlocked(api)) return <BlockedCard compact={compact} follow={api.data.follow} />;
  return (
    <RhState api={api} empty={empty} emptyText={emptyText} loadingRows={loadingRows}>
      {children}
    </RhState>
  );
}

// ── glossary tooltip ─────────────────────────────────────────────────────────────────────────────────
export function Term({ k, children, className = '' }) {
  const def = termOf(k);
  const anchor = useRef(null);
  const tip = useRef(null);
  const id = useId();
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return undefined;
    }
    const a = anchor.current;
    const t = tip.current;
    if (a && t) {
      const r = a.getBoundingClientRect();
      const vw = window.innerWidth || 1024;
      const vh = window.innerHeight || 768;
      const tw = t.offsetWidth || 240;
      const th = t.offsetHeight || 80;
      const left = Math.min(Math.max(8, r.left + r.width / 2 - tw / 2), Math.max(8, vw - tw - 8));
      let top = r.bottom + 8;
      let below = true;
      if (top + th > vh - 8 && r.top - th - 8 >= 8) {
        top = r.top - th - 8;
        below = false;
      }
      setPos({ left, top, below, arrow: Math.min(tw - 12, Math.max(12, r.left + r.width / 2 - left)) });
    }
    const close = () => setOpen(false);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [open]);

  if (!def) return <span className={className}>{children}</span>;
  const onKey = e => {
    if (e.key === 'Escape' && open) {
      e.stopPropagation();
      setOpen(false);
    }
  };
  return (
    <>
      <span
        ref={anchor}
        className={`mui-crowd-term ${className}`}
        tabIndex={0}
        aria-describedby={open ? id : undefined}
        data-term={k}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKey}
      >
        {children}
      </span>
      {open && typeof document !== 'undefined'
        ? createPortal(
            <div
              ref={tip}
              id={id}
              role="tooltip"
              className={`mui-root mui-crowd-tip ${pos ? (pos.below ? 'is-below' : 'is-above') : 'is-measuring'}`}
              style={pos ? { left: `${pos.left}px`, top: `${pos.top}px`, '--tip-arrow': `${pos.arrow}px` } : undefined}
            >
              <b className="mui-crowd-tip__title">{def.title}</b>
              <span className="mui-crowd-tip__body">{def.body}</span>
            </div>,
            document.body
          )
        : null}
    </>
  );
}

// ── small marks ──────────────────────────────────────────────────────────────────────────────────────
/** Rank change since the previous snapshot: "▲3" / "▼2" / "◆ new". Never color alone (arrow + number). */
export function Delta({ row, compact = false }) {
  if (!row) return null;
  if (row.isNew) {
    return (
      <span className="mui-crowd-delta is-new" title="Joined the list on the latest snapshot">
        <span aria-hidden="true">◆</span>
        {compact ? '' : ' new'}
        {compact ? <span className="mui-sr">new on the list</span> : null}
      </span>
    );
  }
  const d = row.delta;
  if (!isNum(d) || d === 0) return null;
  const up = d > 0;
  return (
    <span
      className={`mui-crowd-delta ${up ? 'mui-up' : 'mui-down'}`}
      title={`${up ? 'Up' : 'Down'} ${Math.abs(d)} since ${row.prevDate ? shortDate(row.prevDate) : 'the previous snapshot'} (was #${row.prev})`}
    >
      {up ? '▲' : '▼'}
      {Math.abs(d)}
      <span className="mui-sr">{up ? ' places up' : ' places down'}</span>
    </span>
  );
}

/** "● held" / "○ watched" (shape + word, never color alone). */
export function Yours({ row, short = false }) {
  if (!row) return null;
  if (row.held) return <span className="mui-crowd-yours is-held">● {short ? '' : 'held'}{short ? <span className="mui-sr">held</span> : null}</span>;
  if (row.watched) return <span className="mui-crowd-yours is-watched">○ {short ? '' : 'watched'}{short ? <span className="mui-sr">watched</span> : null}</span>;
  return null;
}

// ── sizing ───────────────────────────────────────────────────────────────────────────────────────────
/** [ref, {w, h}] — the element's content box, live (ResizeObserver; Gridstack resizes reflow us). */
export function useBox() {
  const ref = useRef(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => {
      const w = Math.round(el.clientWidth);
      const h = Math.round(el.clientHeight);
      setBox(b => (b.w === w && b.h === h ? b : { w, h }));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, box];
}

// ── rank path sparkline ──────────────────────────────────────────────────────────────────────────────
const AXIS_FONT = '10px Inter, system-ui, sans-serif';
let measureAxis = null;
const axisMeasure = () => (measureAxis || (measureAxis = makeMeasure(AXIS_FONT)));

/**
 * SVG rank path (rank 1 at the top). path = [{date:'YYYY-MM-DD', rank|null}]; null = off the list (the line
 * breaks there and a dashed floor marks it). axis: date labels thinned by width (lib/timeTicks, never
 * squished). hover: a crosshair with the exact date + rank.
 */
export function RankSpark({ path, width, height, axis = false, hover = false, label = 'Rank history', className = '' }) {
  const [hi, setHi] = useState(null);
  const pts = Array.isArray(path) ? path : [];
  const n = pts.length;
  const padT = 6;
  const padB = axis ? 18 : 4;
  const padX = axis ? 6 : 3;
  const W = Math.max(0, Math.round(width || 0));
  const H = Math.max(0, Math.round(height || 0));
  const geo = useMemo(() => {
    const ranks = pts.map(p => p.rank).filter(isNum);
    if (!n || !ranks.length || W < 24 || H < 16) return null;
    let lo = Math.min(...ranks);
    let hiR = Math.max(...ranks);
    if (hiR - lo < 4) {
      const mid = (lo + hiR) / 2;
      lo = Math.max(1, Math.floor(mid - 2));
      hiR = lo + 4;
    }
    const floorY = H - padB;
    const plotB = floorY - (pts.some(p => p.rank == null) ? 7 : 2);
    const x = i => (n === 1 ? W / 2 : padX + (i * (W - 2 * padX)) / (n - 1));
    const y = r => padT + ((r - lo) / (hiR - lo || 1)) * (plotB - padT);
    const segs = []; // runs of consecutive on-list points: [[x, y], ...]
    let cur = [];
    pts.forEach((p, i) => {
      if (isNum(p.rank)) cur.push([x(i), y(p.rank)]);
      else if (cur.length) {
        segs.push(cur);
        cur = [];
      }
    });
    if (cur.length) segs.push(cur);
    const gaps = [];
    let g0 = null;
    pts.forEach((p, i) => {
      if (p.rank == null && g0 == null) g0 = i;
      if (p.rank != null && g0 != null) {
        gaps.push([g0, i - 1]);
        g0 = null;
      }
    });
    if (g0 != null) gaps.push([g0, n - 1]);
    const ticks = axis
      ? pickTimeTicks({ times: pts.map(p => p.date), xOf: x, x0: 0, x1: W, measure: axisMeasure(), minGap: 10, utc: true }).ticks
      : [];
    return { x, y, segs, gaps, ticks, best: Math.min(...ranks), floorY, plotB };
  }, [pts, n, W, H, axis, padB, padX]);

  if (!geo) return <div className={`mui-crowd-spark mui-crowd-spark--empty ${className}`} style={{ height: H || undefined }} aria-hidden="true" />;
  let lastI = -1;
  for (let i = n - 1; i >= 0; i--) {
    if (isNum(pts[i].rank)) {
      lastI = i;
      break;
    }
  }
  const last = lastI >= 0 ? pts[lastI] : null;
  const aria = `${label}: ${n} snapshots, best #${geo.best}${last ? `, latest #${last.rank}` : ''}`;
  const onMove = e => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    let best = 0;
    let bd = Infinity;
    for (let i = 0; i < n; i++) {
      const dd = Math.abs(geo.x(i) - px);
      if (dd < bd) {
        bd = dd;
        best = i;
      }
    }
    setHi(best);
  };
  const hp = hi != null ? pts[hi] : null;
  return (
    <div className={`mui-crowd-spark ${className}`} style={{ width: W, height: H }}>
      <svg
        width={W}
        height={H}
        role="img"
        aria-label={aria}
        onPointerMove={hover ? onMove : undefined}
        onPointerLeave={hover ? () => setHi(null) : undefined}
      >
        <title>{aria}</title>
        {geo.gaps.map(([a, b]) => (
          <line
            key={`g${a}`}
            className="mui-crowd-spark__off"
            x1={geo.x(a) - (a === b ? 3 : 0)}
            x2={geo.x(b) + (a === b ? 3 : 0)}
            y1={geo.floorY - 2}
            y2={geo.floorY - 2}
          />
        ))}
        {geo.segs.map((s, i) =>
          s.length > 1 ? (
            <polyline key={`s${i}`} className="mui-crowd-spark__line" points={s.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(' ')} />
          ) : (
            <circle key={`s${i}`} className="mui-crowd-spark__pt" cx={s[0][0]} cy={s[0][1]} r="2" />
          )
        )}
        {last ? <circle className="mui-crowd-spark__last" cx={geo.x(lastI)} cy={geo.y(last.rank)} r="2.75" /> : null}
        {axis
          ? geo.ticks.map(tk => (
              <text key={`t${tk.i}`} className="mui-crowd-spark__tick" x={tk.tx} y={H - 4} textAnchor={tk.anchor}>
                {tk.label}
              </text>
            ))
          : null}
        {hp ? (
          <g className="mui-crowd-spark__hover">
            <line x1={geo.x(hi)} x2={geo.x(hi)} y1={padT - 2} y2={geo.floorY} />
            {isNum(hp.rank) ? <circle cx={geo.x(hi)} cy={geo.y(hp.rank)} r="3.5" /> : null}
          </g>
        ) : null}
      </svg>
      {hp ? (
        <span
          className={`mui-crowd-spark__read ${geo.x(hi) > W / 2 ? 'is-left' : ''}`}
          style={{ left: `${Math.round(geo.x(hi))}px` }}
          role="status"
        >
          {fullLabel(hp.date, { intraday: false, utc: true })} · {isNum(hp.rank) ? `#${hp.rank}` : 'not on the list'}
        </span>
      ) : null}
    </div>
  );
}
