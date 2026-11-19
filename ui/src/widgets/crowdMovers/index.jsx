// crowdMovers — "Crowd Movers" (page-only, #crowd). UI overhaul Task 4 (2026-11-16), the "what changed?" widget:
//  - an activity gauge for TODAY's reshuffle (names that moved 3+ places, joined or left since the previous
//    snapshot) in words: Calm / Normal / Busy / Frenzied — calibrated on the real list (most days move 0-3 names);
//  - the biggest climbers ▲ and slides ▼ over a window you pick (1 day / 1 week / 1 month), each with its rank
//    path as a sparkline, plus who joined ◆ / left ◇ in that window.
// Every row is a button that opens the ticker in the Ticker Snapshot. Needs data.history (rank paths); with an
// older backend it shows joins/exits from the events only and says why the rest is missing.
// React Bits: Counter (the rolling numbers in the gauge). The gauge and the sparklines are ours (SVG): React Bits
// has no gauge or sparkline. The rows are plain buttons in two short ranked columns (AnimatedList is a single
// scrolling listbox; five clickable rows per side read better as two side-by-side lists).
import { useMemo, useState } from 'react';
import { motion } from 'motion/react';
import Counter from '../../reactbits/Counter/Counter.jsx';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import {
  LOOKBACKS,
  emitPick,
  hasHistory,
  historyOf,
  lookbackOf,
  obj,
  rankPath,
  recentExits,
  shortDate,
  todayRows,
  useCrowd,
  usePicked
} from '../crowdShared/data.js';
import { CrowdState, RankSpark, Term } from '../crowdShared/ui.jsx';

const TOP_N = 5;
const LEVELS = [
  { max: 1, label: 'Calm', note: 'almost nothing moved' },
  { max: 4, label: 'Normal', note: 'a few names shuffled' },
  { max: 8, label: 'Busy', note: 'more reshuffling than usual' },
  { max: Infinity, label: 'Frenzied', note: 'the list is churning' }
];

function activity(data) {
  const rows = todayRows(data, 1);
  const moved = rows.filter(r => !r.isNew && r.delta != null && Math.abs(r.delta) >= 3).length;
  const joined = rows.filter(r => r.isNew).length;
  const snap = obj(data.list).snap_date;
  const left = recentExits(data, 3).filter(e => e.date === snap).length;
  const score = moved + joined + left;
  const lv = LEVELS.find(l => score <= l.max);
  return { moved, joined, left, score, lv, frac: Math.min(1, score / 12) };
}

function Gauge({ a }) {
  const reduce = usePrefersReducedMotion();
  // semicircle from 180° (left, calm) to 0° (right, frenzied)
  const ang = Math.PI * (1 - a.frac);
  const r = 40;
  const nx = 50 + r * 0.82 * Math.cos(ang);
  const ny = 50 - r * 0.82 * Math.sin(ang);
  return (
    <div className="mui-crowdmv__gauge">
      <svg viewBox="0 0 100 56" role="img" aria-label={`Crowd activity today: ${a.lv.label} (${a.score})`}>
        <path d="M10 50 A40 40 0 0 1 90 50" className="mui-crowdmv__arc" />
        <motion.path
          d="M10 50 A40 40 0 0 1 90 50"
          className="mui-crowdmv__arc-on"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: Math.max(0.02, a.frac) }}
          transition={{ duration: reduce ? 0 : 0.9, ease: [0.22, 1, 0.36, 1] }}
        />
        <motion.line
          x1="50"
          y1="50"
          initial={{ x2: 50 - r * 0.82, y2: 50 }}
          animate={{ x2: nx, y2: ny }}
          transition={{ duration: reduce ? 0 : 0.9, ease: [0.22, 1, 0.36, 1] }}
          className="mui-crowdmv__needle"
        />
        <circle cx="50" cy="50" r="3" className="mui-crowdmv__hub" />
      </svg>
      <div className="mui-crowdmv__gauge-txt">
        <b className="mui-crowdmv__level">{a.lv.label}</b>
        <span className="mui-crowdmv__gauge-sub">
          <Term k="activity">today</Term>: {a.lv.note}
        </span>
        <span className="mui-crowdmv__counts">
          <span title="moved 3+ places since the previous snapshot">
            <Counter value={a.moved} places={a.moved >= 10 ? [10, 1] : [1]} fontSize={13} padding={1} gap={0} horizontalPadding={0} gradientHeight={0} gradientFrom="transparent" /> moved 3+
          </span>
          <span>
            ◆ <Counter value={a.joined} places={a.joined >= 10 ? [10, 1] : [1]} fontSize={13} padding={1} gap={0} horizontalPadding={0} gradientHeight={0} gradientFrom="transparent" /> joined
          </span>
          <span>
            ◇ <Counter value={a.left} places={a.left >= 10 ? [10, 1] : [1]} fontSize={13} padding={1} gap={0} horizontalPadding={0} gradientHeight={0} gradientFrom="transparent" /> left
          </span>
        </span>
      </div>
    </div>
  );
}

function MoverRow({ r, data, picked, back }) {
  const path = useMemo(() => rankPath(data, r.ticker).slice(-Math.max(6, back + 1)), [data, r.ticker, back]);
  const up = r.delta > 0;
  return (
    <li>
      <button
        type="button"
        className={`mui-crowdmv__row ${picked ? 'is-picked' : ''}`}
        onClick={() => emitPick(r.ticker)}
        aria-pressed={picked}
        title={`${r.ticker}${r.name ? ` · ${r.name}` : ''}: #${r.prev} → #${r.rank} since ${shortDate(r.prevDate)} — open its snapshot`}
      >
        <b className="mui-crowdmv__tk">{r.ticker}</b>
        <span className="mui-crowdmv__path">
          #{r.prev} → #{r.rank}
        </span>
        <span className={`mui-crowdmv__d ${up ? 'mui-up' : 'mui-down'}`}>
          {up ? '▲' : '▼'}
          {Math.abs(r.delta)}
        </span>
        <span className="mui-crowdmv__spark" aria-hidden="true">
          <RankSpark path={path} width={56} height={18} label={`${r.ticker} rank`} />
        </span>
        {r.name ? <span className="mui-crowdmv__name">{r.name}</span> : null}
      </button>
    </li>
  );
}

function Column({ title, tone, rows, data, picked, back, empty }) {
  return (
    <section className={`mui-crowdmv__col is-${tone}`} aria-label={title}>
      <h4 className="mui-crowdmv__h">
        <Term k="climb">{title}</Term>
      </h4>
      {rows.length ? (
        <ol className="mui-crowdmv__list">
          {rows.map(r => (
            <MoverRow key={r.ticker} r={r} data={data} picked={picked === r.ticker} back={back} />
          ))}
        </ol>
      ) : (
        <p className="mui-crowdmv__none">{empty}</p>
      )}
    </section>
  );
}

function Movers({ data, lb, onLb }) {
  const picked = usePicked();
  const L = lookbackOf(lb);
  const histOk = hasHistory(data) && historyOf(data).dates.length >= 2;
  const rows = useMemo(() => todayRows(data, L.back), [data, L.back]);
  const a = useMemo(() => activity(data), [data]);
  const up = rows.filter(r => !r.isNew && r.delta > 0).sort((x, y) => y.delta - x.delta || x.rank - y.rank).slice(0, TOP_N);
  const down = rows.filter(r => !r.isNew && r.delta < 0).sort((x, y) => x.delta - y.delta || x.rank - y.rank).slice(0, TOP_N);
  const joined = histOk ? rows.filter(r => r.isNew) : [];
  const left = recentExits(data, L.days);
  const since = rows.find(r => r.prevDate) ? shortDate(rows.find(r => r.prevDate).prevDate) : null;
  const onKey = ev => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const i = LOOKBACKS.findIndex(x => x.id === L.id);
    const next = LOOKBACKS[(i + step + LOOKBACKS.length) % LOOKBACKS.length].id;
    onLb(next);
    const g = ev.currentTarget;
    requestAnimationFrame(() => {
      const b = g.querySelector(`[data-lb="${next}"]`);
      if (b) b.focus();
    });
  };
  return (
    <div className="mui-crowdmv">
      {histOk ? <Gauge a={a} /> : null}
      <div className="mui-crowdmv__bar">
        <span className="mui-crowdmv__since">{since ? `Since ${since}` : 'Rank moves'}</span>
        {histOk ? (
          <span className="mui-crowdmv__lbs" role="radiogroup" aria-label="Look back" onKeyDown={onKey}>
            {LOOKBACKS.map(x => (
              <button
                key={x.id}
                type="button"
                role="radio"
                aria-checked={L.id === x.id}
                tabIndex={L.id === x.id ? 0 : -1}
                data-lb={x.id}
                className={`mui-crowd-pill ${L.id === x.id ? 'is-on' : ''}`}
                onClick={() => onLb(x.id)}
              >
                {x.label}
              </button>
            ))}
          </span>
        ) : null}
      </div>
      {histOk ? (
        <div className="mui-crowdmv__cols">
          <Column title="▲ Climbing" tone="up" rows={up} data={data} picked={picked} back={L.back} empty={`Nothing climbed ${L.since}.`} />
          <Column title="▼ Slipping" tone="down" rows={down} data={data} picked={picked} back={L.back} empty={`Nothing slipped ${L.since}.`} />
        </div>
      ) : (
        <p className="mui-crowdmv__none">Climbers and slides need the rank history from the updated crowd service (Mason restart).</p>
      )}
      <div className="mui-crowdmv__foot">
        <span className="mui-crowdmv__foot-h">
          <Term k="enter">◆ Joined</Term>
        </span>
        {joined.length ? (
          joined.slice(0, 8).map(r => (
            <button key={r.ticker} type="button" className="mui-crowdmv__mini is-new" onClick={() => emitPick(r.ticker)} title={`${r.ticker} joined the list — now #${r.rank}`}>
              {r.ticker} <span>#{r.rank}</span>
            </button>
          ))
        ) : (
          <span className="mui-crowdmv__none">{histOk ? 'none' : '—'}</span>
        )}
        <span className="mui-crowdmv__foot-h">
          <Term k="exit">◇ Left</Term>
        </span>
        {left.length ? (
          left.slice(0, 8).map(e => (
            <button key={`${e.ticker}|${e.date}`} type="button" className="mui-crowdmv__mini is-left" onClick={() => emitPick(e.ticker)} title={`${e.ticker} left the list on ${shortDate(e.date)}${e.rank ? ` (was #${e.rank})` : ''}`}>
              {e.ticker} <span>{shortDate(e.date)}</span>
            </button>
          ))
        ) : (
          <span className="mui-crowdmv__none">none</span>
        )}
      </div>
    </div>
  );
}

export default function CrowdMovers({ config = {}, saveConfig }) {
  const api = useCrowd();
  const [local, setLocal] = useState(null);
  const lb = local || lookbackOf(config.lookback).id;
  const onLb = id => {
    setLocal(id);
    if (typeof saveConfig === 'function') saveConfig({ ...config, lookback: id });
  };
  return (
    <div className="mui-crowd mui-crowd--movers">
      <CrowdState api={api} compact loadingRows={4} empty={d => !d || !Array.isArray(d.today) || !d.today.length} emptyText="No list snapshot yet.">
        {d => <Movers data={obj(d)} lb={lb} onLb={onLb} />}
      </CrowdState>
    </div>
  );
}
