// calendar: the month grid, the filter chips and the agenda list.
// Every string from the API (titles, subtitles, notes, sources, tickers) is rendered as JSX text only.
import { memo, useCallback, useEffect, useRef, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import BorderGlow from '../../reactbits/BorderGlow/BorderGlow.jsx';
import {
  IMPORTANCE_TEXT,
  MAX_ROWS,
  TYPE_LABELS,
  VIEW_TYPES,
  WEEKDAYS,
  addDays,
  addMonths,
  clock,
  dayHeader,
  dayNum,
  dayOfMonth,
  longDay,
  monthTitle,
  rangeText,
  rowLabel,
  shortDay
} from './calUtil.js';

const MAX_DOTS = 5;
const MAX_CHIPS = 4;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// ── the month grid ───────────────────────────────────────────────────────────
// One box per day, Sunday first. A box shows its day number, a colored dot per kind of event, and how many
// events start that day; ◆ = touches a stock you or Mason hold, a dashed line = a multi-day event carries on.
// Every box is a React Bits BorderGlow: the edge nearest the pointer lights up in that day's event colors,
// and the selected day keeps a slowly turning glow (calendar.css) while its popover is open.
// Keyboard: one box is in the Tab order; the arrows move by day / week (and into the next month),
// Page Up / Page Down by month, Enter opens the day.
const GLOW_DEFAULT = ['var(--mui-accent)', 'var(--mui-accent-300)', 'var(--cal-conference)'];
const glowColors = types => (types.length ? types.slice(0, 3).map(t => `var(--cal-${t})`) : GLOW_DEFAULT);

/** Where a day was opened from: the pointer, or (keyboard) the box's top-right corner. */
export function anchorOf(e, el) {
  if (e && e.detail !== 0 && (e.clientX || e.clientY)) return { x: e.clientX, y: e.clientY, kbd: false };
  const r = el.getBoundingClientRect();
  return { x: r.right - 6, y: r.top + 6, kbd: true };
}

export function MonthGrid({ grid, cells, today, labels, selected, focusDate, onFocusDate, onPick, onMonth }) {
  const ref = useRef(null);
  const wantFocus = useRef(false);
  const tdy = dayNum(today);
  const rovingDate = grid.days.includes(focusDate) ? focusDate : grid.days.includes(today) ? today : grid.first;

  useEffect(() => {
    if (!wantFocus.current) return;
    wantFocus.current = false;
    const el = ref.current && ref.current.querySelector(`[data-date="${rovingDate}"]`);
    if (el) el.focus({ preventScroll: true });
  }, [rovingDate, grid.month]);

  const move = useCallback(
    target => {
      if (!target) return;
      wantFocus.current = true;
      onFocusDate(target);
      if (!grid.days.includes(target)) onMonth(target.slice(0, 7)); // past the dimmed boxes: turn the page
    },
    [grid, onFocusDate, onMonth]
  );

  const onKey = (e, date) => {
    const dow = grid.days.indexOf(date) % 7;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -dow, End: 6 - dow }[e.key];
    if (step != null) {
      e.preventDefault();
      move(addDays(date, step));
    } else if (e.key === 'PageUp' || e.key === 'PageDown') {
      e.preventDefault();
      const ym = addMonths(date.slice(0, 7), e.key === 'PageUp' ? -1 : 1);
      const lastDom = +addDays(`${addMonths(ym, 1)}-01`, -1).slice(8);
      const target = `${ym}-${String(Math.min(+date.slice(8), lastDom)).padStart(2, '0')}`;
      wantFocus.current = true;
      onFocusDate(target);
      onMonth(ym);
    }
  };

  return (
    <div className="mui-cal__month" ref={ref}>
      <div className="mui-cal__wkdays" aria-hidden="true">
        {WEEKDAYS.map(d => (
          <span key={d}>{d}</span>
        ))}
      </div>
      <div className="mui-cal__grid" role="group" aria-label={`${monthTitle(grid.month)}. Arrow keys move between days, Enter opens a day.`} style={{ '--weeks': grid.days.length / 7 }}>
        {cells.map(c => {
          const dn = dayNum(c.date);
          const isToday = dn === tdy;
          const kinds = c.types.map(t => labels[t] || TYPE_LABELS[t]);
          const say =
            `${isToday ? 'Today, ' : ''}${longDay(c.date)}: ` +
            (c.count ? `${plural(c.count, 'event', 'events')} (${kinds.join(', ')})` : 'nothing scheduled') +
            (c.ongoing.length ? `. Still on: ${c.ongoing.map(e => e.title).join('; ')}` : '') +
            (c.held ? '. Touches a stock you or Mason hold' : '');
          const cls =
            'mui-cal__box' +
            (c.inMonth ? '' : ' is-out') +
            (isToday ? ' is-today' : '') +
            (dn < tdy ? ' is-past' : '') +
            (c.count ? '' : ' is-empty') +
            (c.big ? ' is-big' : '') +
            (selected === c.date ? ' is-selected' : '');
          return (
            <BorderGlow
              key={c.date}
              className={cls}
              backgroundColor="var(--mui-panel-3)"
              borderRadius={6}
              glowRadius={14}
              edgeSensitivity={25}
              coneSpread={22}
              glowColor="212 100 72"
              glowIntensity={0.9}
              fillOpacity={0.3}
              colors={glowColors(c.types)}
            >
              <button
                type="button"
                className="mui-cal__cell"
                data-date={c.date}
                tabIndex={c.date === rovingDate ? 0 : -1}
                aria-label={say}
                aria-haspopup="dialog"
                aria-expanded={selected === c.date}
                aria-current={isToday ? 'date' : undefined}
                onClick={e => {
                  onFocusDate(c.date);
                  onPick(c.date, anchorOf(e, e.currentTarget));
                }}
                onKeyDown={e => onKey(e, c.date)}
              >
                <span className="mui-cal__num">{dayOfMonth(c.date)}</span>
                {c.held ? (
                  <span className="mui-cal__mine" aria-hidden="true">
                    ◆
                  </span>
                ) : null}
                <span className="mui-cal__dots" aria-hidden="true">
                  {c.types.slice(0, MAX_DOTS).map(t => (
                    <i key={t} className="mui-cal__dot" data-type={t} />
                  ))}
                  {c.types.length > MAX_DOTS ? <em>+</em> : null}
                </span>
                {c.count ? (
                  <span className="mui-cal__badge" aria-hidden="true">
                    {c.count}
                  </span>
                ) : null}
                {c.ongoing.length ? <span className="mui-cal__on" aria-hidden="true" /> : null}
              </button>
            </BorderGlow>
          );
        })}
      </div>
    </div>
  );
}

// ── filter chips ─────────────────────────────────────────────────────────────
// One line of chips by default; "More kinds" opens the rest. It opens by itself when a remembered
// filter is on, or when keyboard focus lands on a chip that is out of sight.
export function Filters({ filters, counts, total, labels, hiddenTrials, onAll, onType, onTrials }) {
  const types = VIEW_TYPES.filter(t => counts[t] > 0 || filters.types.includes(t));
  const showTrials = filters.trials || hiddenTrials == null || hiddenTrials > 0;
  const ref = useRef(null);
  const [open, setOpen] = useState(() => filters.types.length > 0);
  const [many, setMany] = useState(false); // more than one line of chips
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    el.scrollTop = 0; // a focused chip may have scrolled the clipped row
    const measure = () => {
      const one = el.firstElementChild ? el.firstElementChild.offsetHeight : 0;
      setMany(open ? one > 0 && el.clientHeight > one * 1.6 : el.scrollHeight > el.clientHeight + 2);
    };
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [open, types.length, showTrials]);
  return (
    <div className={`mui-cal__chipbar${open ? ' is-open' : ''}`}>
      <div
        ref={ref}
        className="mui-cal__chips"
        role="group"
        aria-label="Kinds of events to show"
        onFocus={e => {
          if (!open && e.target !== e.currentTarget && e.target.offsetTop > e.currentTarget.offsetTop + 4) setOpen(true);
        }}
      >
        <button type="button" className="mui-cal__chip" data-chip="all" aria-pressed={!filters.types.length} onClick={onAll}>
          All <span className="mui-cal__chipn">{total}</span>
        </button>
        {types.map(t => (
          <button key={t} type="button" className="mui-cal__chip" data-chip={t} data-type={t} aria-pressed={filters.types.includes(t)} onClick={() => onType(t)}>
            <i className="mui-cal__dot" aria-hidden="true" />
            {labels[t] || TYPE_LABELS[t]} <span className="mui-cal__chipn">{counts[t]}</span>
          </button>
        ))}
        {showTrials ? (
          <button
            type="button"
            className="mui-cal__chip mui-cal__chip--more"
            data-chip="trials"
            aria-pressed={filters.trials}
            title="There are hundreds of small drug-trial dates. They stay hidden unless you ask for them; the ones that touch your stocks always show."
            onClick={onTrials}
          >
            {filters.trials ? 'Hide small drug-trial dates' : `Show small drug-trial dates${hiddenTrials ? ` (${hiddenTrials} more)` : ''}`}
          </button>
        ) : null}
      </div>
      {many ? (
        <button type="button" className="mui-cal__chiptoggle" data-act="chips" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          {open ? 'Fewer' : 'More kinds'} <span aria-hidden="true">{open ? '▴' : '▾'}</span>
        </button>
      ) : null}
    </div>
  );
}

// ── one row of the agenda ────────────────────────────────────────────────────
function Tickers({ ev, held }) {
  if (!ev.tickers.length) return null;
  const shown = ev.tickers.slice(0, MAX_CHIPS);
  const more = ev.tickers.length - shown.length;
  const anyKnown = ev.tickers.some(t => held.has(t));
  return (
    <>
      {shown.map(t => (
        <span key={t} className={`mui-cal__tk${held.has(t) ? ' is-held' : ''}`}>
          {t}
          {held.has(t) ? <em> held</em> : null}
        </span>
      ))}
      {more > 0 ? <span className="mui-cal__tk mui-cal__tk--more">+{more} more</span> : null}
      {ev.held && !anyKnown ? <span className="mui-cal__flag mui-cal__flag--held">◆ includes a stock you hold</span> : null}
      {ev.watched ? <span className="mui-cal__flag">on your watchlist</span> : null}
    </>
  );
}

export const EventRow = memo(function EventRow({ ev, today, held }) {
  const range = rangeText(ev, today);
  const moved = !!ev.changed && !ev.editable; // a date he moved himself is not news to him (the details still say it)
  return (
    <div className={`mui-cal__row imp-${ev.importance}${ev.done ? ' is-done' : ''}`} data-type={ev.type} data-id={ev.id}>
      <span className="mui-cal__when">
        {ev.time_et ? (
          <>
            {clock(ev.time_et)}
            <i> ET</i>
          </>
        ) : (
          'all day'
        )}
      </span>
      <div className="mui-cal__main">
        <div className="mui-cal__title">
          <span className="mui-cal__type">
            <i className="mui-cal__dot" aria-hidden="true" />
            {rowLabel(ev)}
          </span>
          {ev.importance === 3 ? (
            <span className="mui-cal__star" title={IMPORTANCE_TEXT[3]}>
              <span aria-hidden="true">★</span>
              <span className="mui-sr">Important: </span>
            </span>
          ) : null}
          <span className="mui-cal__name">{ev.title}</span>
        </div>
        {ev.subtitle ? <div className="mui-cal__sub">{ev.subtitle}</div> : null}
        {ev.tickers.length || range || !ev.confirmed || moved || ev.done ? (
          <div className="mui-cal__tags">
            <Tickers ev={ev} held={held} />
            {range ? <span className="mui-cal__flag mui-cal__flag--range">{range}</span> : null}
            {!ev.confirmed ? <span className="mui-cal__flag mui-cal__flag--maybe">? date not confirmed</span> : null}
            {moved ? <span className="mui-cal__flag mui-cal__flag--moved">↪ moved from {shortDay(ev.changed.previous_date, today)}</span> : null}
            {ev.done ? <span className="mui-cal__flag mui-cal__flag--done">✓ done</span> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
});

function DayHead({ row, today }) {
  return (
    <div className="mui-cal__dayhead" data-day={row.date}>
      <span className="mui-cal__dayname">{dayHeader(row.date, today)}</span>
      <span className="mui-cal__dayn">{row.count}</span>
    </div>
  );
}

const isSep = r => !!r && r.sep === true;
const keyOf = r => r.key;

/** The scrolling list: one sticky header row per day, then that day's events (React Bits AnimatedList). */
export function Agenda({ rows, today, held, onOpen, wrapRef }) {
  const renderItem = useCallback(r => (r.sep ? <DayHead row={r} today={today} /> : <EventRow ev={r.ev} today={today} held={held} />), [today, held]);
  const onSelect = useCallback(
    r => {
      if (r && !r.sep) onOpen(r.ev);
    },
    [onOpen]
  );
  return (
    <div className="mui-cal__listwrap" ref={wrapRef}>
      <AnimatedList
        items={rows}
        isSeparator={isSep}
        itemKey={keyOf}
        renderItem={renderItem}
        onItemSelect={onSelect}
        maxItems={MAX_ROWS}
        capLabel={false}
        ariaLabel="This month’s events. Use the up and down arrow keys, then Enter to open one."
      />
    </div>
  );
}
