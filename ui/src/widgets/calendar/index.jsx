// Calendar: dates that matter, from GET /api/calendar (contract: docs/fable/CALENDAR_API.md), as a month.
// Top to bottom: a bar (‹ month ›, Today, count, "My stocks only", "Add event"), filter chips (also the dot
// legend), the month grid (a box per day: colored dots per kind of event and how many), and a footer with
// the "Agenda" drop-down (this month as a list) and the refresh state.
// Clicking a day opens a small popover beside the pointer (Popover.jsx) with that day's events; an event
// opens its details in the same popover, where the owner's own events can be edited, ticked off or
// removed. "Add event" opens the form there too. The popover never covers the widget like a dialog: the
// rest of the month stays visible and clickable.
//
// React Bits: BorderGlow (the day boxes; the selected day keeps its glow), AnimatedList (the popover list
// and the agenda; mod M-AnimatedList-5 adds heading rows), SquishSwitch ("My stocks only"), CountUp (the
// count). Built here because React Bits has none: the month grid layout, the filter chips, the popover,
// the form fields.
//
// Refresh: on mount, every 5 minutes, and when the tab comes back; nothing runs while hidden
// (useCalendar.js). Turning the month loads that month. Text from the API is rendered as JSX text only.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import SquishSwitch from '../../reactbits/SquishSwitch/SquishSwitch.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { Loading } from '../../lib/states.jsx';
import {
  TYPE_LABELS,
  agendaRows,
  applyBase,
  applyTypes,
  cleanFilters,
  dayNum,
  etToday,
  heldSet,
  inMonth,
  isMinorTrial,
  loadFilters,
  monthCells,
  monthDiff,
  monthGrid,
  monthGroups,
  monthName,
  monthOf,
  monthTitle,
  addMonths,
  placeDay,
  saveFilters,
  shortDay,
  toggleType,
  typeCounts
} from './calUtil.js';
import { useCalendar } from './useCalendar.js';
import { Agenda, Filters, MonthGrid, anchorOf } from './parts.jsx';
import { DayPopover, usePortalHost } from './Popover.jsx';
import './calendar.css';

export const MONTH_SPAN = 12; // how far the month can be turned either way from today's month
export const AGENDA_KEY = 'mason.calendar.agenda.v1';
const NOTICE_MS = 8000;

function store() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
function loadFlag(key) {
  try {
    return store().getItem(key) === '1';
  } catch {
    return false;
  }
}
function saveFlag(key, on) {
  try {
    store().setItem(key, on ? '1' : '0');
  } catch {
    /* private mode / storage full: the choice just is not remembered */
  }
}
const clockNow = ms => new Date(ms).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }).toLowerCase();

/** '' | 'short' | 'tiny' | 'micro' from the widget's own height, so a small widget keeps room for the grid. */
function useHeightTier(ref) {
  const [tier, setTier] = useState('');
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(entries => {
      const h = entries.length ? entries[entries.length - 1].contentRect.height : 0;
      setTier(h <= 0 ? '' : h < 200 ? 'micro' : h < 300 ? 'tiny' : h < 400 ? 'short' : '');
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return tier;
}

function Message({ kind, children }) {
  return (
    <div className={`mui-cal__msg mui-cal__msg--${kind}`} role="status">
      <span className="mui-cal__msgicon" aria-hidden="true">
        {kind === 'error' ? '!' : '◷'}
      </span>
      <p>{children}</p>
    </div>
  );
}

export function CalendarView({ cal, filters, onFilters, month, onMonth }) {
  const rootRef = useRef(null);
  const listRef = useRef(null);
  const pointerRef = useRef(null);
  const tier = useHeightTier(rootRef);
  const host = usePortalHost();
  const [pop, setPop] = useState(null); // {date | null, anchor, view: {mode: 'day' | 'event' | 'edit' | 'add', id?}}
  const [focusDate, setFocusDate] = useState(null);
  const [agenda, setAgenda] = useState(() => loadFlag(AGENDA_KEY));
  const [notice, setNotice] = useState('');
  const today = cal.today || etToday();
  const thisMonth = monthOf(today);
  const events = cal.events;
  const serverHeld = cal.held;
  const grid = useMemo(() => monthGrid(month), [month]);

  const view = useMemo(() => {
    const base = applyBase(events, filters);
    const shown = applyTypes(base, filters);
    const monthBase = inMonth(base, grid); // the chips count the month itself, like the header and the agenda
    const { rows, shown: nRows, total } = agendaRows(monthGroups(shown, grid));
    return { counts: typeCounts(monthBase), baseTotal: monthBase.length, rows, nRows, total, cells: monthCells(shown, grid), held: heldSet(events, serverHeld) };
  }, [events, filters, grid, serverHeld]);

  const labels = (cal.meta && cal.meta.labels) || TYPE_LABELS;
  const behind = (cal.meta && cal.meta.behind) || [];
  const fdaInWindow = cal.meta && !cal.busy ? cal.meta.counts.fda : null;
  // FDA items on screen; a server from before 2026-11-16 also sends the small ones, which are hidden here
  const fdaShown = events.filter(e => e.type === 'fda' && !isMinorTrial(e)).length;
  const hiddenTrials = !filters.trials && !filters.mine && fdaInWindow != null ? Math.max(0, fdaInWindow - fdaShown) : null;

  // ── the popover ──
  const cell = pop && pop.date ? view.cells.find(c => c.date === pop.date) || null : null;
  const find = useCallback(id => events.find(e => e.id === id) || null, [events]);
  const selectedGone = !!(pop && pop.view.id && !find(pop.view.id));
  useEffect(() => {
    if (!selectedGone) return;
    setPop(p => (p && p.date ? { ...p, view: { mode: 'day' } } : null)); // the event went away (removed, or dropped by a refresh)
  }, [selectedGone]);
  useEffect(() => {
    setPop(p => (p && p.date && !grid.days.includes(p.date) ? null : p)); // the month was turned past it
  }, [grid]);

  const closePop = useCallback(({ refocus } = {}) => {
    setPop(p => {
      if (refocus && p && p.date && rootRef.current) {
        const el = rootRef.current.querySelector(`.mui-cal__cell[data-date="${p.date}"]`);
        if (el) setTimeout(() => el.isConnected && el.focus({ preventScroll: true }), 0);
      }
      return null;
    });
  }, []);
  const pickDay = useCallback((date, anchor) => {
    setPop(p => (p && p.date === date && p.view.mode === 'day' ? null : { date, anchor, view: { mode: 'day' } }));
  }, []);
  const setView = useCallback(v => setPop(p => (p ? { ...p, view: v } : p)), []);
  const openAdd = useCallback(e => setPop({ date: null, anchor: anchorOf(e, e.currentTarget), view: { mode: 'add' } }), []);
  const openFromAgenda = useCallback(
    ev => {
      const wrap = listRef.current;
      const p = pointerRef.current;
      let anchor;
      if (p && Date.now() - p.at < 1500) anchor = { x: p.x, y: p.y, kbd: false };
      else {
        const sel = wrap && wrap.querySelector('.scroll-list .item.selected');
        const r = (sel || wrap).getBoundingClientRect();
        anchor = { x: r.left + r.width * 0.6, y: r.top, kbd: true };
      }
      pointerRef.current = null;
      setPop({ date: placeDay(ev, grid.from), anchor, view: { mode: 'event', id: ev.id } });
    },
    [grid]
  );

  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(''), NOTICE_MS);
    return () => clearTimeout(t);
  }, [notice]);

  const write = cal.write;
  const saveForm = useCallback(
    async (method, body) => {
      const r = await write(method, body);
      if (!r.ok) return r;
      const date = body.date;
      const shownHere = !!date && grid.days.includes(date);
      if (method === 'POST') {
        const when = `${shortDay(date, today)}${body.end_date ? ` to ${shortDay(body.end_date, today)}` : ''}`;
        setNotice(shownHere ? `Added for ${when}.` : `Added for ${when}. Turn to ${monthTitle(date.slice(0, 7))} to see it.`);
        setPop(p => (p && shownHere ? { ...p, date, view: { mode: 'day' } } : null));
      } else {
        setNotice('Saved.');
        setPop(p => (p && shownHere ? { ...p, date, view: { mode: 'event', id: body.id } } : null));
      }
      return r;
    },
    [write, grid, today]
  );

  // ── the month ──
  const offset = monthDiff(thisMonth, month);
  const turn = useCallback(
    ym => {
      if (!ym || Math.abs(monthDiff(thisMonth, ym)) > MONTH_SPAN) return;
      onMonth(ym);
    },
    [thisMonth, onMonth]
  );
  const toggleAgenda = () => {
    setAgenda(a => {
      saveFlag(AGENDA_KEY, !a);
      return !a;
    });
  };
  useEffect(() => {
    if (!agenda || month !== thisMonth) return;
    // open on today: scroll the agenda to today's heading (or the next day that has something)
    const sc = listRef.current && listRef.current.querySelector('.scroll-list');
    if (!sc || typeof sc.scrollTo !== 'function') return;
    const heads = [...sc.querySelectorAll('[data-day]')];
    const head = heads.find(h => dayNum(h.getAttribute('data-day')) >= dayNum(today));
    const sep = head && head.closest('[data-index]');
    if (sep) sc.scrollTo({ top: Math.max(0, sep.offsetTop - 2) });
  }, [agenda, month, thisMonth, today, view.rows]);

  const filtersOn = filters.types.length > 0 || filters.mine;
  const capped = view.total > view.nRows;

  return (
    <div ref={rootRef} className={`mui-cal${cal.busy ? ' is-busy' : ''}${agenda ? ' has-agenda' : ''}`} data-status={cal.status} data-tier={tier || undefined}>
      {cal.status === 'loading' ? <Loading rows={5} label="Loading the calendar…" /> : null}
      {cal.status === 'off' ? <Message kind="off">The calendar turns on after Mason’s next restart.</Message> : null}
      {cal.status === 'error' ? <Message kind="error">The calendar could not be loaded. It will try again in a minute.</Message> : null}
      {cal.status === 'ok' ? (
        <div className="mui-cal__body">
          <div className="mui-cal__bar">
            <div className="mui-cal__nav" role="group" aria-label="Month">
              <button type="button" className="mui-cal__navbtn" data-act="prev" aria-label="Previous month" title="Previous month" disabled={offset <= -MONTH_SPAN} onClick={() => turn(addMonths(month, -1))}>
                <span aria-hidden="true">‹</span>
              </button>
              <h3 className="mui-cal__mtitle" aria-live="polite">
                {monthTitle(month)}
              </h3>
              <button type="button" className="mui-cal__navbtn" data-act="next" aria-label="Next month" title="Next month" disabled={offset >= MONTH_SPAN} onClick={() => turn(addMonths(month, 1))}>
                <span aria-hidden="true">›</span>
              </button>
              {offset !== 0 ? (
                <button type="button" className="mui-cal__today" data-act="today" onClick={() => turn(thisMonth)}>
                  Today
                </button>
              ) : null}
            </div>
            <span className="mui-cal__sum">
              <b>
                <CountUp to={view.total} duration={0.6} />
              </b>{' '}
              {view.total === 1 ? 'event' : 'events'}
            </span>
            <span className="mui-cal__grow" />
            <SquishSwitch
              className="mui-cal__switch"
              checked={filters.mine}
              onChange={v => onFilters({ mine: !!v })}
              label="My stocks only"
              width={30}
              height={16}
              radius={8}
              trackColor="rgba(120, 150, 220, 0.24)"
              trackOnColor="var(--mui-accent)"
              thumbColor="var(--mui-text-2)"
              thumbOnColor="var(--mui-text)"
            />
            <button type="button" className="mui-btn mui-cal__add" data-act="add" aria-haspopup="dialog" onClick={openAdd}>
              <span aria-hidden="true">+</span> Add<span className="mui-cal__addw"> event</span>
            </button>
          </div>
          <Filters
            filters={filters}
            counts={view.counts}
            total={view.baseTotal}
            labels={labels}
            hiddenTrials={hiddenTrials}
            onAll={() => onFilters({ types: [] })}
            onType={t => onFilters({ types: toggleType(filters.types, t) })}
            onTrials={() => onFilters({ trials: !filters.trials })}
          />
          <MonthGrid
            grid={grid}
            cells={view.cells}
            today={today}
            labels={labels}
            selected={pop && pop.date}
            focusDate={focusDate}
            onFocusDate={setFocusDate}
            onPick={pickDay}
            onMonth={turn}
          />
          <AnimatePresence initial={false}>
            {agenda ? (
              <motion.div
                key="agenda"
                className="mui-cal__agenda"
                id="mui-cal-agenda"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                onPointerDownCapture={e => {
                  pointerRef.current = { x: e.clientX, y: e.clientY, at: Date.now() };
                }}
              >
                {view.rows.length ? (
                  <Agenda rows={view.rows} today={today} held={view.held} onOpen={openFromAgenda} wrapRef={listRef} />
                ) : (
                  <p className="mui-cal__agempty">Nothing in {monthName(month)}.</p>
                )}
              </motion.div>
            ) : null}
          </AnimatePresence>
          <div className="mui-cal__foot">
            <button type="button" className="mui-cal__agbtn" data-act="agenda" aria-expanded={agenda} aria-controls="mui-cal-agenda" onClick={toggleAgenda}>
              <span className="mui-cal__chev" aria-hidden="true">
                ▾
              </span>
              Agenda<span className="mui-cal__agn"> · {view.total}</span>
            </button>
            {view.total === 0 ? (
              <span className="mui-cal__none" role="status" data-foot="empty">
                Nothing scheduled in {monthName(month)}.
                {filtersOn ? (
                  <>
                    {' '}
                    <button type="button" className="mui-cal__linkbtn" data-act="clear" onClick={() => onFilters({ types: [], mine: false })}>
                      Show everything
                    </button>
                  </>
                ) : null}
              </span>
            ) : null}
            {cal.failed ? (
              <span className="mui-cal__warn" role="status" data-foot="failed">
                ⚠ Could not refresh. Showing what loaded at {clockNow(cal.at)}; trying again in a minute.
              </span>
            ) : (
              <span data-foot="updated">Updated {clockNow(cal.at)}</span>
            )}
            {capped ? (
              <span className="mui-cal__warn" data-foot="capped">
                The agenda shows the first {view.nRows} of {view.total}. Switch off some kinds above to see the rest.
              </span>
            ) : null}
            {behind.length ? (
              <span className="mui-cal__warn" data-foot="behind" title={behind.join(', ')}>
                ⚠ Not updating right now: {behind.join(', ')}
              </span>
            ) : null}
            {notice ? (
              <span className="mui-cal__notice" role="status" data-foot="notice">
                {notice}
              </span>
            ) : null}
            <span className="mui-cal__grow" />
            <span>Times are Eastern</span>
          </div>
        </div>
      ) : null}
      <DayPopover
        host={host}
        rootRef={rootRef}
        pop={cal.status === 'ok' ? pop : null}
        cell={cell}
        find={find}
        today={today}
        held={view.held}
        onView={setView}
        onClose={closePop}
        onSave={saveForm}
        onWrite={write}
      />
    </div>
  );
}

export default function Calendar() {
  const [filters, setFilters] = useState(() => loadFilters(store()));
  const [month, setMonth] = useState(() => monthOf(etToday()));
  const grid = useMemo(() => monthGrid(month), [month]);
  const cal = useCalendar({ from: grid.from, to: grid.to, mine: filters.mine, trials: filters.trials });
  useEffect(() => {
    saveFilters(store(), filters);
  }, [filters]);
  const onFilters = useCallback(patch => setFilters(f => cleanFilters({ ...f, ...patch })), []);
  return <CalendarView cal={cal} filters={filters} onFilters={onFilters} month={month} onMonth={setMonth} />;
}
