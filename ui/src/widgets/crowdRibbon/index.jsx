// crowdRibbon — "Crowd Tracker" (page-only, #crowd). UI overhaul Task 4 (2026-11-16): the single ribbon became
// FOUR lanes that scroll at different speeds in alternating directions, grouped two ways (pill toggle):
//   By rank  — #1–25 · #26–50 · #51–75 · #76–100
//   Movers   — joined ◆ · climbing ▲ · slipping ▼ · left ◇, over the last week (needs data.history)
// Every chip is a button: click / Enter picks the ticker for the Ticker Snapshot (crowd pick bus). Holdings stay
// marked "● held" / "○ watched" (shape + word, never color alone); ▲3 / ▼2 / ◆ is the rank change.
// React Bits: LogoLoop (one per lane; its rAF loop is gated by useMotionAllowed() — MODS M-LogoLoop-1 — so lanes
// stop while hidden, off screen or under reduced motion). (The page title keeps the template's one-shot
// DecryptedText; the widget header already names the tracker, so there is no second title inside.) A lane whose chips
// fit its width is a static row (no loop, no duplicate copies). Hover or keyboard focus pauses a lane.
// Under reduced motion every lane is a static row you can scroll sideways. Container queries (crowd.css) drop
// lanes 4 → 3 → 2 as the widget gets shorter and compact the labels as it narrows.
import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import LogoLoop from '../../reactbits/LogoLoop/LogoLoop.jsx';
import { date, num } from '../../lib/fmt.js';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import {
  MAX_LIST,
  RANK_BANDS,
  arr,
  emitPick,
  hasHistory,
  historyOf,
  lookbackOf,
  obj,
  recentExits,
  str,
  todayRows,
  useCrowd,
  usePicked
} from '../crowdShared/data.js';
import { CrowdState, Delta, Term, useBox } from '../crowdShared/ui.jsx';

const MAX_NAMED = 8;
const MODES = [
  { id: 'rank', label: 'By rank' },
  { id: 'move', label: 'Movers' }
];
// px/s per lane; directions alternate so neighbouring lanes never move together
const SPEEDS = { rank: [20, 28, 36, 44], move: [24, 32, 24, 32] };
const DIRS = ['left', 'right', 'left', 'right'];

const fmtList = (xs, max = MAX_NAMED) => {
  const head = xs.slice(0, max).map(c => `${str(c.ticker)} ${c.rank != null ? `#${c.rank}` : ''}`.trim());
  return head.join(', ') + (xs.length > max ? `, +${num(xs.length - max)} more` : '');
};

const chipTitle = r =>
  [
    r.ticker,
    r.name,
    r.left ? `left the list ${r.date ? `on ${date(r.date)}` : ''}${r.lastRank ? ` (was #${r.lastRank})` : ''}` : r.rank != null ? `#${r.rank}` : null,
    !r.left && r.prev != null && r.delta ? `was #${r.prev}${r.prevDate ? ` on ${date(r.prevDate)}` : ''}` : null,
    !r.left && r.isNew ? 'new on the list' : null,
    r.sector,
    r.held ? 'held' : r.watched ? 'watched' : null,
    'click for its snapshot'
  ]
    .filter(Boolean)
    .join(' · ');

const Chip = memo(function Chip({ r, picked, focusable }) {
  const cls = [
    'mui-crowd-tk',
    r.held ? 'mui-crowd-tk--held' : '',
    !r.held && r.watched ? 'mui-crowd-tk--watched' : '',
    r.left ? 'mui-crowd-tk--left' : '',
    picked ? 'is-picked' : ''
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <button type="button" className={cls} title={chipTitle(r)} tabIndex={focusable ? 0 : -1} aria-pressed={picked} onClick={() => emitPick(r.ticker)}>
      <span className="mui-crowd-tk__rank">{r.left ? '◇' : r.rank != null ? `#${r.rank}` : '#—'}</span>
      <b className="mui-crowd-tk__sym">{r.ticker}</b>
      {r.left ? <span className="mui-crowd-tk__was">was #{r.lastRank ?? '—'}</span> : <Delta row={r} compact />}
      {r.held ? <span className="mui-crowd-tk__mark">● held</span> : null}
      {!r.held && r.watched ? <span className="mui-crowd-tk__mark">○ watched</span> : null}
    </button>
  );
});

function Lane({ lane, idx, speed, direction, picked, reduce }) {
  const [stripRef, box] = useBox();
  const [loop, setLoop] = useState(false);
  const [focusIn, setFocusIn] = useState(false);
  const items = lane.items;
  // A lane hidden by a container query (short widget) has no box: it renders the cheap static row, no loop.
  const shown = box.w > 0;
  // Loop only when the chips don't fit: render the static row, measure it, switch to LogoLoop if it overflows.
  // Back to static only once the strip is wider than that measured row (+24 px), never by measuring LogoLoop's
  // own copies (they can read 0 mid-layout, which would flip-flop). New items → measure again.
  const need = useRef(0);
  const seenItems = useRef(items);
  useLayoutEffect(() => {
    const strip = stripRef.current;
    if (seenItems.current !== items) {
      seenItems.current = items;
      need.current = 0;
      if (loop) {
        setLoop(false);
        return;
      }
    }
    if (!strip || reduce || !shown) return;
    if (!loop) {
      const ul = strip.querySelector('.mui-crowdribbon__static');
      if (ul && ul.scrollWidth > strip.clientWidth + 2) {
        need.current = ul.scrollWidth;
        setLoop(true);
      }
    } else if (need.current > 0 && strip.clientWidth > need.current + 24) {
      setLoop(false);
    }
  }, [box.w, items, loop, reduce, shown, stripRef]);
  // copy 0 is the accessible one; LogoLoop marks the other copies aria-hidden, so their chips leave the Tab order
  const renderItem = useCallback(
    (r, key) => <Chip r={r} picked={picked === r.ticker} focusable={String(key).startsWith('0-')} />,
    [picked]
  );
  const onBlur = e => {
    if (!e.currentTarget.contains(e.relatedTarget)) setFocusIn(false);
  };
  return (
    <div className={`mui-crowdribbon__lane is-${lane.tone}`} data-lane={lane.id} data-lane-i={idx}>
      <span className="mui-crowdribbon__lane-label">
        <Term k={lane.term}>
          <span className="mui-crowdribbon__lane-long">{lane.label}</span>
          <span className="mui-crowdribbon__lane-short" aria-hidden="true">
            {lane.short}
          </span>
        </Term>
        <span className="mui-crowdribbon__lane-n" title={`${items.length} tickers in this row`}>
          {items.length}
        </span>
      </span>
      <div
        className={`mui-crowdribbon__strip ${reduce ? 'is-static' : ''}`}
        ref={stripRef}
        onFocus={() => setFocusIn(true)}
        onBlur={onBlur}
      >
        {!items.length ? (
          <span className="mui-crowdribbon__lane-empty">{lane.empty}</span>
        ) : loop && shown && !reduce ? (
          <LogoLoop
            logos={items}
            renderItem={renderItem}
            speed={focusIn ? 0 : speed}
            direction={direction}
            gap={8}
            logoHeight={14}
            pauseOnHover
            fadeOut
            ariaLabel={`${lane.label}: ${items.length} tickers`}
          />
        ) : (
          <ul className="mui-crowdribbon__static" role="list" aria-label={`${lane.label}: ${items.length} tickers`}>
            {items.map(r => (
              <li key={r.ticker} className="logoloop__item">
                <Chip r={r} picked={picked === r.ticker} focusable />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function lanesOf(data, mode) {
  if (mode === 'move') {
    const lb = lookbackOf('week');
    const rows = todayRows(data, lb.back);
    const up = rows.filter(r => !r.isNew && r.delta > 0).sort((a, b) => b.delta - a.delta || a.rank - b.rank);
    const down = rows.filter(r => !r.isNew && r.delta < 0).sort((a, b) => a.delta - b.delta || a.rank - b.rank);
    const fresh = rows.filter(r => r.isNew);
    const left = recentExits(data, lb.days).map(e => ({ ticker: e.ticker, rank: null, lastRank: e.rank, date: e.date, left: true }));
    return [
      { id: 'new', label: 'Joined ◆', short: '◆', term: 'enter', tone: 'new', items: fresh, empty: 'No new names this week.' },
      { id: 'up', label: 'Climbing ▲', short: '▲', term: 'climb', tone: 'up', items: up, empty: 'Nothing climbed this week.' },
      { id: 'down', label: 'Slipping ▼', short: '▼', term: 'climb', tone: 'down', items: down, empty: 'Nothing slipped this week.' },
      { id: 'left', label: 'Left ◇', short: '◇', term: 'exit', tone: 'left', items: left, empty: 'Nobody left the list this week.' }
    ];
  }
  const rows = todayRows(data, 1).slice(0, MAX_LIST);
  return RANK_BANDS.map((b, i) => ({
    id: b.id,
    label: b.label,
    short: b.short,
    term: i === 0 ? 'popular' : 'rank',
    tone: `band${i + 1}`,
    items: rows.filter(r => r.rank != null && r.rank >= b.lo && r.rank <= b.hi),
    empty: 'No tickers in this band yet.'
  }));
}

function Tracker({ data, mode, onMode }) {
  const reduce = usePrefersReducedMotion();
  const picked = usePicked();
  const canMove = hasHistory(data) && historyOf(data).dates.length >= 2;
  const m = canMove ? mode : 'rank';
  const lanes = useMemo(() => lanesOf(data, m), [data, m]);
  const all = useMemo(() => todayRows(data, 1), [data]);
  const crowded = arr(data.crowded).filter(c => c && typeof c === 'object');
  const held = crowded.filter(c => c.held === true).sort((a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9));
  const watched = crowded.filter(c => c.held !== true && c.watched === true).sort((a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9));
  const list = obj(data.list);
  const total = Math.max(Number.isFinite(list.n_items) ? list.n_items : 0, arr(data.today).length);
  const shown = Math.min(all.length, MAX_LIST);
  const heldTitle = `Held (your accounts): ${fmtList(held) || 'none'}${watched.length ? ` · watching: ${fmtList(watched)}` : ''}`;
  // ARIA radio pattern for the mode pills (arrows move + select; Tab leaves the group)
  const onPillKey = ev => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const i = MODES.findIndex(x => x.id === m);
    const next = MODES[(i + step + MODES.length) % MODES.length].id;
    onMode(next);
    const g = ev.currentTarget;
    requestAnimationFrame(() => {
      const b = g.querySelector(`[data-mode="${next}"]`);
      if (b) b.focus();
    });
  };

  return (
    <div className="mui-crowdribbon" data-mode={m}>
      <div className="mui-crowdribbon__head">
        <span className="mui-crowdribbon__meta">
          <Term k="popular">{str(list.name) || 'Popular list'}</Term> · snapshot {date(list.snap_date)} ·{' '}
          {total > shown ? `${num(shown)} of ${num(total)} shown` : `${num(shown)} tickers`}
        </span>
        <span className="mui-crowdribbon__held" title={heldTitle}>
          {held.length ? (
            <>
              <span className="mui-crowdribbon__held-n">
                <span aria-hidden="true">● </span>
                {num(held.length)} held on the list (your accounts):
              </span>{' '}
              <b>{fmtList(held)}</b>
            </>
          ) : (
            <>None of your holdings (your accounts) are on the list today.</>
          )}
          {watched.length ? (
            <>
              {' · '}
              <span aria-hidden="true">○ </span>watching {num(watched.length)}: <b>{fmtList(watched)}</b>
            </>
          ) : null}
        </span>
        {canMove ? (
          <span className="mui-crowdribbon__modes" role="radiogroup" aria-label="Group the rows" onKeyDown={onPillKey}>
            {MODES.map(x => (
              <button
                key={x.id}
                type="button"
                role="radio"
                aria-checked={m === x.id}
                tabIndex={m === x.id ? 0 : -1}
                data-mode={x.id}
                className={`mui-crowd-pill ${m === x.id ? 'is-on' : ''}`}
                onClick={() => onMode(x.id)}
              >
                {x.label}
              </button>
            ))}
          </span>
        ) : null}
      </div>
      <div className="mui-crowdribbon__lanes">
        {lanes.map((lane, i) => (
          <Lane key={`${m}-${lane.id}`} lane={lane} idx={i} speed={SPEEDS[m][i]} direction={DIRS[i]} picked={picked} reduce={reduce} />
        ))}
      </div>
    </div>
  );
}

export default function CrowdRibbon({ config = {}, saveConfig }) {
  const api = useCrowd();
  const [localMode, setLocalMode] = useState(null);
  const mode = localMode || (config.mode === 'move' ? 'move' : 'rank');
  const onMode = useCallback(
    m => {
      const next = m === 'move' ? 'move' : 'rank';
      setLocalMode(next);
      if (typeof saveConfig === 'function') saveConfig({ ...config, mode: next });
    },
    [config, saveConfig]
  );
  return (
    <div className="mui-crowd mui-crowd--ribbon">
      <CrowdState
        api={api}
        loadingRows={2}
        empty={d => !d || !arr(d.today).length}
        emptyText="No list snapshot yet — the first daily snapshot fills this tracker."
      >
        {d => <Tracker data={obj(d)} mode={mode} onMode={onMode} />}
      </CrowdState>
    </div>
  );
}
