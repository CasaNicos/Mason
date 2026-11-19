// calendar: the day popover. Clicking a day opens a small window beside the pointer (not a widget, not a
// modal): the day's events in a scrolling React Bits AnimatedList, one event's details, or the add / edit
// form, all inside the same window. The calendar stays visible and usable around it; clicking another
// day moves it there. It closes on ×, Escape, a click outside it, or when the page scrolls or resizes.
//
// It is rendered into a portal on <body> (fixed position), so a small widget never clips it. The portal
// host carries .mui-root (theme tokens) and the window itself .mui-cal (type colors), so every calendar
// style applies inside it. API text is rendered as JSX text only.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'motion/react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { EventRow } from './parts.jsx';
import { BackButton, CloseButton, Details, EventForm } from './EventPanels.jsx';
import { dayNum, relDay, shortDay, weekDay } from './calUtil.js';

const GAP = 12; // between the pointer and the window
const EDGE = 8; // kept clear at the window edges
const LIFT = 18; // the window's top sits this far above the pointer

/** A <div class="mui-root"> on <body> for the life of the widget. */
export function usePortalHost() {
  const [host] = useState(() => (typeof document !== 'undefined' ? document.createElement('div') : null));
  useLayoutEffect(() => {
    if (!host) return undefined;
    host.className = 'mui-root mui-cal-host';
    document.body.appendChild(host);
    return () => host.remove();
  }, [host]);
  return host;
}

/**
 * Where the window goes for a pointer at (x, y): to its right (to its left when that does not fit), top a
 * little above the pointer, then pushed inside the browser window. → {left, top, origin} (origin = the
 * pointer, so the window grows out of it).
 */
export function placePop(anchor, w, h, vw, vh) {
  let left = anchor.x + GAP;
  let side = 'left';
  if (left + w > vw - EDGE) {
    const alt = anchor.x - GAP - w;
    if (alt >= EDGE) {
      left = alt;
      side = 'right';
    } else left = Math.max(EDGE, vw - EDGE - w);
  }
  const top = Math.max(EDGE, Math.min(anchor.y - LIFT, vh - EDGE - h));
  return { left: Math.round(left), top: Math.round(top), origin: `${side} ${Math.round(Math.max(0, Math.min(h, anchor.y - top)))}px` };
}

const isSep = r => !!r && r.sep === true;
const keyOf = r => r.key;

function dayRows(cell) {
  if (!cell) return [];
  const rows = cell.events.map(ev => ({ sep: false, key: `e:${ev.id}`, ev }));
  if (cell.ongoing.length) {
    rows.push({ sep: true, key: 'on', label: 'Still going', count: cell.ongoing.length });
    for (const ev of cell.ongoing) rows.push({ sep: false, key: `o:${ev.id}`, ev });
  }
  return rows;
}

function DayView({ date, cell, today, held, canAdd, onOpen, onAdd, onClose }) {
  const rows = dayRows(cell);
  const n = cell ? cell.count : 0;
  const rel = relDay(date, today);
  const renderItem = useCallback(
    r =>
      r.sep ? (
        <div className="mui-cal__dayhead mui-cal__dayhead--on">
          <span className="mui-cal__dayname">{r.label}</span>
          <span className="mui-cal__dayn">{r.count}</span>
        </div>
      ) : (
        <EventRow ev={r.ev} today={today} held={held} />
      ),
    [today, held]
  );
  const onSelect = useCallback(r => r && !r.sep && onOpen(r.ev.id), [onOpen]);
  return (
    <div className="mui-cal__popday" data-day={date}>
      <div className="mui-cal__pophead">
        <span className="mui-cal__poptitle">
          <b>{weekDay(date, today)}</b>
          {rel ? <span className="mui-cal__poprel"> · {rel}</span> : null}
        </span>
        <span className="mui-cal__popn">{n === 1 ? '1 event' : `${n} events`}</span>
        <CloseButton onClose={onClose} />
      </div>
      {rows.length ? (
        <div className="mui-cal__poplist">
          <AnimatedList
            items={rows}
            isSeparator={isSep}
            itemKey={keyOf}
            renderItem={renderItem}
            onItemSelect={onSelect}
            showGradients={false}
            capLabel={false}
            ariaLabel={`Events on ${shortDay(date, today)}. Use the up and down arrow keys, then Enter to open one.`}
          />
        </div>
      ) : (
        <p className="mui-cal__popempty">Nothing scheduled.</p>
      )}
      {canAdd ? (
        <div className="mui-cal__popfoot">
          <button type="button" className="mui-btn mui-btn--ghost mui-cal__popadd" data-act="add-day" onClick={onAdd}>
            <span aria-hidden="true">+</span> Add on this day
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * pop = {date | null, anchor: {x, y, kbd}, view: {mode: 'day' | 'event' | 'edit' | 'add', id?}}.
 * `cell` = the day's box (monthCells), `find(id)` → the event or null. onView(view) switches the content,
 * onSave(method, body) is the form's write (it decides what happens next), onWrite is a plain write.
 */
export function DayPopover({ host, rootRef, pop, cell, find, today, held, onView, onClose, onSave, onWrite }) {
  if (!host) return null;
  return createPortal(
    <AnimatePresence>
      {pop ? <PopWindow key="pop" rootRef={rootRef} pop={pop} cell={cell} find={find} today={today} held={held} onView={onView} onClose={onClose} onSave={onSave} onWrite={onWrite} /> : null}
    </AnimatePresence>,
    host
  );
}

function PopWindow({ rootRef, pop, cell, find, today, held, onView, onClose, onSave, onWrite }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);
  const { date, anchor, view } = pop;
  const ev = view.id ? find(view.id) : null;
  const canAdd = !!date && dayNum(date) >= dayNum(today);

  // ── place it beside the pointer, and again whenever its size or the pointer changes ──
  const place = useCallback(() => {
    const el = ref.current;
    if (!el || typeof window === 'undefined') return;
    setPos(placePop(anchor, el.offsetWidth, el.offsetHeight, window.innerWidth, window.innerHeight));
  }, [anchor]);
  useLayoutEffect(place, [place, view.mode, view.id, date]);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(() => place());
    ro.observe(el);
    return () => ro.disconnect();
  }, [place]);

  // ── closing: Escape, a press outside (a day box just moves it), scrolling or resizing the page ──
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const inside = t => !!(t && ref.current && ref.current.contains(t));
    const onDown = e => {
      if (inside(e.target)) return;
      const root = rootRef.current;
      if (root && e.target && e.target.closest && root.contains(e.target) && e.target.closest('.mui-cal__cell, .mui-cal__add, .mui-cal__listwrap')) return;
      closeRef.current({ refocus: false });
    };
    const onKey = e => {
      if (e.key === 'Escape') closeRef.current({ refocus: true });
    };
    const onScroll = e => {
      if (!inside(e.target)) closeRef.current({ refocus: false });
    };
    const onResize = () => closeRef.current({ refocus: false });
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
    };
  }, [rootRef]);

  // ── focus: into the window when opened from the keyboard, and whenever its content changes ──
  const opened = useRef(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!opened.current) {
      opened.current = true;
      if (!anchor.kbd && view.mode === 'day') return; // a mouse user keeps focus where it was (a form still takes it)
    }
    const first = el.querySelector('[data-autofocus]') || el.querySelector('[role="listbox"]') || el.querySelector('button, input, a[href]');
    if (first) first.focus({ preventScroll: true });
  }, [view.mode, view.id, date, anchor.kbd]);

  const toDay = useCallback(() => (date ? onView({ mode: 'day' }) : onClose({ refocus: true })), [date, onView, onClose]);
  const close = useCallback(() => onClose({ refocus: true }), [onClose]);
  const back = date ? shortDay(date, today) : '';

  let body = null;
  let label = 'Day';
  if (view.mode === 'day') {
    label = date ? `Events on ${shortDay(date, today)}` : 'Events';
    body = <DayView date={date} cell={cell} today={today} held={held} canAdd={canAdd} onOpen={id => onView({ mode: 'event', id })} onAdd={() => onView({ mode: 'add' })} onClose={close} />;
  } else if (view.mode === 'event' && ev) {
    label = 'Event details';
    body = (
      <Details
        ev={ev}
        today={today}
        held={held}
        onClose={close}
        onBack={date ? toDay : null}
        backLabel={back}
        onRemoved={toDay}
        onEdit={() => onView({ mode: 'edit', id: ev.id })}
        onWrite={onWrite}
      />
    );
  } else if (view.mode === 'edit' && ev) {
    label = 'Edit event';
    body = <EventForm ev={ev} today={today} onCancel={() => onView({ mode: 'event', id: ev.id })} onWrite={onSave} />;
  } else if (view.mode === 'add') {
    label = 'Add an event';
    body = (
      <>
        {date ? (
          <div className="mui-cal__popnav">
            <BackButton label={back} onBack={toDay} />
          </div>
        ) : null}
        <EventForm ev={null} today={today} date={date && canAdd ? date : null} onCancel={toDay} onWrite={onSave} />
      </>
    );
  }

  const style = pos ? { left: pos.left, top: pos.top, transformOrigin: pos.origin } : { left: anchor.x + GAP, top: anchor.y - LIFT, visibility: 'hidden' };
  return (
    <motion.div
      ref={ref}
      className={`mui-cal mui-cal__pop${pos ? ' is-placed' : ''}`}
      role="dialog"
      aria-label={label}
      style={style}
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.94, transition: { duration: 0.12 } }}
      transition={{ type: 'spring', stiffness: 520, damping: 34, mass: 0.7 }}
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={`${view.mode}:${view.id || ''}:${date || ''}`}
          className="mui-cal__popview"
          initial={{ opacity: 0, x: 10 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -10 }}
          transition={{ duration: 0.14 }}
        >
          {body}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
