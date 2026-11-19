// wm-picker — the palette's "Add widget…" list (WM design §6.2, §7.1), built on React Bits AnimatedList.
//
// Mounted by the template palette (T4):  MasonUI.mount(el, 'wm-picker', props) → {update, unmount}
// Props (ENGINEER_BRIEF §5 C3):
//   rows            [{key, type, group, title, desc, icon, state:'on'|'add', ticker?:bool}]
//   selectedIndex   the palette's mv.cmdHi. It counts SELECTABLE ROWS ONLY, top to bottom as shown;
//                   group headers are never selectable and never count (orchestrator ruling W13).
//                   Rows are shown grouped (GROUP_ORDER, then unknown groups first-seen; a stable sort),
//                   so pass rows already in that order: then selectedIndex i is exactly rows[i].
//                   -1 / out of range / non-integer = no highlight.
//   onPick(key)     a row was clicked (ON BOARD rows too: the palette scrolls to + flashes them).
//   onTicker(key, TICKER)   Enter / "Add" in a row's inline ticker input; TICKER is trimmed + upper-case
//                           and matches ^[A-Za-z.]{1,6}$ (WM §6.1).
//   onCancelTicker()        Esc / "Cancel" in that input.
// Keyboard: the palette owns ArrowUp/ArrowDown/Enter (the list never handles them). Inside the ticker
// input, Enter / Escape / ArrowUp / ArrowDown / Backspace / Delete / Home / End / PageUp / PageDown stay
// in the input and do NOT bubble to the palette's document keydown listener (so Esc cancels the ticker
// instead of closing the palette, and Backspace edits the ticker instead of leaving picker mode).
// Every string renders through JSX (escaped); the icon class is checked against a safe pattern.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import './wm-picker.css';

export const GROUP_ORDER = ['Account', 'Market', 'Lists', 'Feeds', 'System', 'Robinhood'];
export const TICKER_RE = /^[A-Za-z.]{1,6}$/;
const ICON_RE = /^[A-Za-z0-9 _-]{1,80}$/;
const DEFAULT_ICON = 'ph ph-squares-four';
const MAX_ROWS = 200; // AnimatedList cap; past it the list ends with "200 of N shown"

/** Trimmed, upper-cased ticker when it matches TICKER_RE and has at least one letter (brief §10.6), else null. */
export function validTicker(v) {
  if (typeof v !== 'string') return null;
  const t = v.trim();
  return TICKER_RE.test(t) && /[A-Za-z]/.test(t) ? t.toUpperCase() : null;
}

const str = v => (v == null ? '' : String(v));

/** rows → display items, grouped (GROUP_ORDER, then unknown groups in first-seen order), stable. */
export function groupRows(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const norm = [];
  list.forEach((r, src) => {
    if (!r || typeof r !== 'object') return;
    norm.push({
      src,
      key: str(r.key),
      group: str(r.group) || 'Other',
      title: str(r.title) || str(r.key),
      desc: str(r.desc),
      icon: typeof r.icon === 'string' && ICON_RE.test(r.icon) ? r.icon : DEFAULT_ICON,
      on: r.state === 'on',
      ticker: r.ticker === true
    });
  });
  const extra = [];
  for (const r of norm) if (!GROUP_ORDER.includes(r.group) && !extra.includes(r.group)) extra.push(r.group);
  const order = [...GROUP_ORDER, ...extra];
  const rank = g => order.indexOf(g);
  norm.sort((a, b) => rank(a.group) - rank(b.group) || a.src - b.src);
  const seenKeys = new Set();
  let prev = null;
  return norm.map((r, i) => {
    let id = `k:${r.key}`;
    if (seenKeys.has(id)) id = `k:${r.key}#${r.src}`;
    seenKeys.add(id);
    const head = r.group !== prev;
    prev = r.group;
    return { ...r, id, head, pos: i };
  });
}

const stop = e => e.stopPropagation();
const KEEP_KEYS = new Set(['ArrowUp', 'ArrowDown', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown']);

function TickerInput({ item, onTicker, onCancel }) {
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const ref = useRef(null);
  const errRef = useRef(null);
  const errId = useId();
  useEffect(() => {
    const el = ref.current;
    if (el) el.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    // the error line grows the row: keep it visible inside the list (no-op where unsupported)
    const el = errRef.current;
    if (error && el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' });
  }, [error]);
  const submit = () => {
    const t = validTicker(value);
    if (!t) {
      setError('Ticker must be 1–6 letters or dots (e.g. TSLA, BRK.B)');
      return;
    }
    if (typeof onTicker === 'function') onTicker(item.key, t);
  };
  const cancel = () => {
    if (typeof onCancel === 'function') onCancel();
  };
  const onKeyDown = e => {
    if (e.key === 'Enter') {
      // T3b: Enter that confirms an IME composition is not a submit
      if ((e.nativeEvent && e.nativeEvent.isComposing) || e.keyCode === 229) {
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      submit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      cancel();
    } else if (KEEP_KEYS.has(e.key)) {
      // the palette must not move its highlight (arrows) or go back to its root (Backspace/Delete)
      // while a ticker is being typed; Ctrl/⌘+K and Tab still reach it
      e.stopPropagation();
    }
  };
  return (
    <div className="wm-pick-tk" onClick={stop} onMouseDown={stop}>
      <div className="wm-pick-tk__row">
        <i className="ph ph-magnifying-glass" aria-hidden="true" />
        <input
          ref={ref}
          className="wm-pick-ticker"
          type="text"
          inputMode="text"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          maxLength={16}
          placeholder="Ticker, e.g. TSLA"
          aria-label={`Ticker for ${item.title}`}
          aria-invalid={error ? 'true' : 'false'}
          aria-describedby={error ? errId : undefined}
          value={value}
          onChange={e => {
            setValue(e.target.value);
            if (error) setError('');
          }}
          onKeyDown={onKeyDown}
        />
        <button type="button" className="wm-pick-tk__btn wm-pick-tk__btn--add" aria-label={`Add ${item.title}`} onClick={submit}>
          Add <kbd>↵</kbd>
        </button>
        <button type="button" className="wm-pick-tk__btn" aria-label="Cancel" onClick={cancel}>
          <kbd>Esc</kbd>
        </button>
      </div>
      {error ? (
        <div id={errId} ref={errRef} className="wm-pick-err" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}

export default function WmPicker({ rows, selectedIndex, onPick, onTicker, onCancelTicker }) {
  const items = useMemo(() => groupRows(rows), [rows]);
  const reduce = usePrefersReducedMotion();

  // callbacks through refs: AnimatedList's handlers stay stable across palette updates
  const cb = useRef({});
  cb.current = { onPick, onTicker, onCancelTicker };
  const pick = useCallback(item => {
    const f = cb.current.onPick;
    if (item && typeof f === 'function') f(item.key);
  }, []);
  const ticker = useCallback((k, t) => {
    const f = cb.current.onTicker;
    if (typeof f === 'function') f(k, t);
  }, []);
  const cancel = useCallback(() => {
    const f = cb.current.onCancelTicker;
    if (typeof f === 'function') f();
  }, []);

  // W13: index over displayed selectable rows (headers live inside their first row's option, so the
  // AnimatedList index IS the selectable-row index).
  const sel = Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < items.length ? selectedIndex : -1;

  const renderItem = useCallback(
    (it, _i, selected) => (
      <>
        {it.head ? (
          <div className="wm-pick-head" onClick={stop} aria-disabled="true">
            {it.group}
          </div>
        ) : null}
        <div
          className={`wm-pick-row${selected ? ' is-sel' : ''}${it.on ? ' is-on' : ''}`}
          data-wm-key={it.key}
          data-wm-index={it.pos}
        >
          <span className="wm-pick-ico">
            <i className={it.icon} aria-hidden="true" />
          </span>
          <span className="wm-pick-main">
            <span className="wm-pick-title">{it.title}</span>
            <span className="wm-pick-desc">{it.desc}</span>
          </span>
          <span className={`wm-pick-state ${it.on ? 'is-on' : 'is-add'}`}>{it.on ? 'ON BOARD' : '+ ADD'}</span>
        </div>
        {it.ticker ? <TickerInput item={it} onTicker={ticker} onCancel={cancel} /> : null}
      </>
    ),
    [ticker, cancel]
  );

  const cls = `wm-picker${reduce ? ' is-reduced' : ''}`;
  if (!items.length) {
    return (
      <div className={cls}>
        <div className="wm-pick-empty" role="status">
          No widgets to add.
        </div>
      </div>
    );
  }
  return (
    <div className={cls}>
      <AnimatedList
        items={items}
        itemKey={it => it.id}
        renderItem={renderItem}
        onItemSelect={pick}
        selectedIndex={sel}
        enableArrowNavigation={false}
        showGradients
        displayScrollbar
        maxItems={MAX_ROWS}
        ariaLabel="Widgets"
        capLabel={(shown, total) => `${shown.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} widgets shown — type to narrow`}
        className="wm-pick-list"
      />
    </div>
  );
}
