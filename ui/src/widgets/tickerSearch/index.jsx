// tickerSearch — the header ticker search's results list, on React Bits AnimatedList (rows scale in as answers
// arrive; a row that stays between keystrokes keeps its key and does not replay).
//
// Mounted by static/v2/ticker-lookup.js: MasonUI.mount(el, 'tickerSearch', props) → {update, unmount}. The template
// owns the text input, the request (debounce, stale answers dropped) and every key: ArrowUp/ArrowDown/Enter/Escape
// never reach this list (enableArrowNavigation off, so the listbox is not in the Tab order either). It only draws.
// Props:
//   rows           [{key, symbol, name, kind: 'match'|'lookup'|'recent', exact, type?}]  (type: the server's optional
//                  "EQUITY" | "ETF" | …; anything but EQUITY shows as a small tag)
//   selectedIndex  the highlighted row (-1: none)
//   query          what was typed (its match is highlighted in the symbol / name)
//   loading        a request for `query` is in flight (rows, if any, are the previous answer filtered locally)
//   heading        optional label above the rows (e.g. "Recent")
//   note           optional one-line status under the rows ("No matches…", "Search isn't answering…")
//   listId         id of the listbox (the input's aria-controls)
//   optionId(i)    id of row i (the input's aria-activedescendant)
//   onPick(i)      a row was clicked;  onHover(i) the pointer moved onto row i
// Mouse down anywhere in the list is prevented, so the input keeps focus (and the list stays open) on click.
// Every string renders as JSX text.
import { useCallback } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import './tickerSearch.css';

const str = v => (v == null ? '' : String(v));

/** text with the first case-insensitive occurrence of q wrapped in <mark> (prefixOnly: only when text starts with q). */
export function Hl({ text, q, prefixOnly = false }) {
  const t = str(text);
  const needle = str(q).trim().toLowerCase();
  const at = needle ? t.toLowerCase().indexOf(needle) : -1;
  if (at < 0 || (prefixOnly && at !== 0)) return t;
  return (
    <>
      {t.slice(0, at)}
      <mark className="mui-tks__hl">{t.slice(at, at + needle.length)}</mark>
      {t.slice(at + needle.length)}
    </>
  );
}

export default function TickerSearch({
  rows = [],
  selectedIndex = -1,
  query = '',
  loading = false,
  heading = null,
  note = null,
  listId,
  optionId,
  onPick,
  onHover
}) {
  const list = Array.isArray(rows) ? rows.filter(r => r && typeof r === 'object') : [];
  const sel = Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < list.length ? selectedIndex : -1;
  const renderItem = useCallback(
    (r, i, on) => (
      <div
        className={`mui-tks__row${on ? ' is-sel' : ''}${r.kind === 'lookup' ? ' is-lookup' : ''}`}
        onMouseEnter={() => typeof onHover === 'function' && onHover(i)}
      >
        <span className="mui-tks__sym">
          <Hl text={r.symbol} q={query} prefixOnly />
        </span>
        <span className="mui-tks__name">
          {r.kind === 'lookup' ? 'Open the ticker card' : str(query).trim().length > 1 ? <Hl text={r.name} q={query} /> : str(r.name)}
        </span>
        <span className="mui-tks__tags">
          {typeof r.type === 'string' && r.type && r.type !== 'EQUITY' ? <span className="mui-tks__tag is-type">{r.type}</span> : null}
          {r.kind === 'lookup' ? (
            <span className="mui-tks__tag">look up</span>
          ) : r.exact ? (
            <span className="mui-tks__tag is-exact">exact</span>
          ) : r.kind === 'recent' ? (
            <span className="mui-tks__tag is-recent" aria-hidden="true">
              <i className="ph ph-clock-counter-clockwise" />
            </span>
          ) : null}
        </span>
      </div>
    ),
    [query, onHover]
  );
  return (
    <div className={`mui-tks${loading ? ' is-loading' : ''}`} onMouseDown={e => e.preventDefault()}>
      {heading ? (
        <div className="mui-tks__head" aria-hidden="true">
          {heading}
        </div>
      ) : null}
      {list.length ? (
        <AnimatedList
          items={list}
          itemKey={r => str(r.key)}
          renderItem={renderItem}
          selectedIndex={sel}
          onItemSelect={(r, i) => typeof onPick === 'function' && onPick(i)}
          enableArrowNavigation={false}
          showGradients={list.length > 7}
          displayScrollbar={false}
          maxItems={20}
          capLabel={false}
          ariaLabel={heading ? `${heading} tickers` : 'Ticker matches'}
          listId={listId}
          optionId={typeof optionId === 'function' ? (r, i) => optionId(i) : undefined}
        />
      ) : loading ? (
        <div className="mui-tks__skel" aria-hidden="true">
          {[0, 1, 2].map(i => (
            <div key={i} className="mui-tks__skelrow">
              <span className="mui-skel" />
              <span className="mui-skel" />
            </div>
          ))}
        </div>
      ) : null}
      {note ? <div className="mui-tks__note">{note}</div> : null}
      <div className="mui-tks__foot" aria-hidden="true">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> move
        </span>
        <span>
          <kbd>↵</kbd> open
        </span>
        <span>
          <kbd>esc</kbd> close
        </span>
        {loading ? <span className="mui-tks__live">searching…</span> : null}
      </div>
    </div>
  );
}
