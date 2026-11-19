// crowdFeed — "Crowd Moves" (contracts §6.1; catalog multi {kind: all|enter|exit}; also on #crowd).
// Enter/exit events on Robinhood's "100 Most Popular" list in an AnimatedList (React Bits). JOINED / LEFT are
// words with a shape glyph (◆ filled / ◇ hollow); forward excess vs SPY carries a sign and ▲/▼.
// Click (or Enter) on a row opens its detail (entry → exit price per matured horizon) and, on the Crowd page,
// shows the ticker in the Ticker Snapshot (crowd pick bus). UI overhaul Task 4 (2026-11-16): plain-English
// rows ("Joined at #34"), the company name when known, horizons in weeks/months, a glossary line in the header.
import { useCallback, useMemo, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { emitGo } from '../../lib/bridge.js';
import { arrow, dirClass, isNum, money, pct } from '../../lib/fmt.js';
import { arr, emitPick, labelOf, obj, shortDate, str, useCrowd, xsPct } from '../crowdShared/data.js';
import { CrowdState, Term } from '../crowdShared/ui.jsx';

const HORIZONS = [
  ['5', '1 wk', '5 trading days'],
  ['20', '1 mo', '20 trading days'],
  ['60', '3 mo', '60 trading days']
];
const KINDS = [
  { id: 'all', label: 'All' },
  { id: 'enter', label: '◆ Joined' },
  { id: 'exit', label: '◇ Left' }
];
const normKind = k => (k === 'enter' || k === 'exit' ? k : 'all');
const keyOf = e => `${str(e.kind)}|${str(e.ticker)}|${str(e.date)}|${str(e.list)}`;

function KindBadge({ kind }) {
  const enter = kind === 'enter';
  const exit = kind === 'exit';
  return (
    <span className={`mui-crowdfeed__kind ${enter ? 'is-enter' : exit ? 'is-exit' : ''}`}>
      <span aria-hidden="true">{enter ? '◆' : exit ? '◇' : '·'}</span> {enter ? 'JOINED' : exit ? 'LEFT' : str(kind).toUpperCase() || '—'}
    </span>
  );
}

function FwdChip({ h, f }) {
  const x = obj(f);
  if (!f || x.matured !== true) {
    return (
      <span className="mui-crowdfeed__fwd is-pending" title={`${h[2]}: not graded yet`}>
        {h[1]} <span className="mui-crowdfeed__pending">pending</span>
      </span>
    );
  }
  const v = isNum(x.xs_spy) ? x.xs_spy : null;
  return (
    <span className={`mui-crowdfeed__fwd ${dirClass(v)}`} title={`${h[2]} later: ${xsPct(v)} vs SPY`}>
      {h[1]}{' '}
      <b>
        {arrow(v)} {xsPct(v)}
      </b>
    </span>
  );
}

function Detail({ e }) {
  const fwd = obj(e.fwd);
  const matured = HORIZONS.filter(([h]) => obj(fwd[h]).matured === true);
  return (
    <div className="mui-crowdfeed__detail">
      <div className="mui-crowdfeed__detail-head">
        {str(e.list) || 'list'} · {e.kind === 'exit' ? 'left' : 'joined'} on {shortDate(e.date)}
        {isNum(e.rank) ? ` at rank #${e.rank}` : ''}
      </div>
      {matured.length ? (
        matured.map(([h, , long]) => {
          const x = obj(fwd[h]);
          const ret = isNum(x.ret) ? x.ret * 100 : null;
          const xs = isNum(x.xs_spy) ? x.xs_spy : null;
          return (
            <div key={h} className="mui-crowdfeed__detail-row">
              <span className="mui-crowdfeed__h">{long}</span>
              <span>
                {shortDate(x.entry_date)} {money(isNum(x.entry_px) ? x.entry_px : null)} → {shortDate(x.exit_date)}{' '}
                {money(isNum(x.exit_px) ? x.exit_px : null)}
              </span>
              <span className={dirClass(ret)}>
                return {arrow(ret)} {pct(ret, { digits: 2 })}
              </span>
              <span className={dirClass(xs)}>
                vs SPY {arrow(xs)} {xsPct(xs)}
              </span>
            </div>
          );
        })
      ) : (
        <div className="mui-crowdfeed__detail-row is-pending">No horizon has matured yet (5, 20, 60 trading days).</div>
      )}
    </div>
  );
}

function Row({ e, open, name }) {
  const fwd = obj(e.fwd);
  const exit = e.kind === 'exit';
  return (
    <div className={`mui-crowdfeed__row ${open ? 'is-open' : ''}`} aria-expanded={open}>
      <div className="mui-crowdfeed__line">
        <KindBadge kind={e.kind} />
        <b className="mui-crowdfeed__tk">{str(e.ticker) || '—'}</b>
        {name ? <span className="mui-crowdfeed__name">{name}</span> : null}
        <span className="mui-crowdfeed__rank">
          {isNum(e.rank) ? (exit ? `was #${e.rank}` : `at #${e.rank}`) : ''}
          {isNum(e.rank) && e.rank_bucket ? ' · ' : ''}
          {str(e.rank_bucket)}
        </span>
        <span className="mui-crowdfeed__date">{shortDate(e.date)}</span>
      </div>
      <div className="mui-crowdfeed__fwds">
        <span className="mui-crowdfeed__vs">vs SPY</span>
        {HORIZONS.map(h => (
          <FwdChip key={h[0]} h={h} f={fwd[h[0]]} />
        ))}
      </div>
      {open ? <Detail e={e} /> : null}
    </div>
  );
}

function Feed({ data, kind, onKind, onPage }) {
  const [openKey, setOpenKey] = useState(null);
  const events = useMemo(() => arr(data.events).filter(e => e && typeof e === 'object'), [data]);
  const shown = useMemo(() => (kind === 'all' ? events : events.filter(e => e.kind === kind)), [events, kind]);
  const nEnter = events.filter(e => e.kind === 'enter').length;
  const nExit = events.filter(e => e.kind === 'exit').length;
  const onSelect = useCallback(e => {
    const k = keyOf(e);
    setOpenKey(prev => (prev === k ? null : k));
    if (typeof e.ticker === 'string') emitPick(e.ticker);
  }, []);
  const renderItem = useCallback(
    e => <Row e={e} open={keyOf(e) === openKey} name={typeof e.ticker === 'string' ? labelOf(data, e.ticker.toUpperCase()).name : null} />,
    [openKey, data]
  );
  // ARIA radio pattern: arrows move (and select) within the group; Tab leaves it (roving tabindex).
  const onPillKey = ev => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const i = KINDS.findIndex(k => k.id === kind);
    const next = KINDS[(i + step + KINDS.length) % KINDS.length].id;
    onKind(next);
    const group = ev.currentTarget;
    requestAnimationFrame(() => {
      const b = group.querySelector(`[data-kind="${next}"]`);
      if (b) b.focus();
    });
  };

  return (
    <div className="mui-crowdfeed">
      <div className="mui-crowdfeed__head">
        <div className="mui-crowdfeed__pills" role="radiogroup" aria-label="Show" onKeyDown={onPillKey}>
          {KINDS.map(k => (
            <button
              key={k.id}
              type="button"
              role="radio"
              aria-checked={kind === k.id}
              tabIndex={kind === k.id ? 0 : -1}
              data-kind={k.id}
              className={`mui-crowd-pill ${kind === k.id ? 'is-on' : ''}`}
              onClick={() => onKind(k.id)}
            >
              {k.label}
            </button>
          ))}
        </div>
        <span className="mui-crowdfeed__count" title="Events in the payload (joined / left)">
          {nEnter} joined · {nExit} left
        </span>
        {onPage ? null : (
          <button type="button" className="mui-crowd-pill mui-crowd-pill--go" onClick={() => emitGo('crowd')} title="Open the Crowd page">
            Crowd page ↗
          </button>
        )}
      </div>
      <p className="mui-crowdfeed__legend">
        <Term k="enter">◆ joined</Term> / <Term k="exit">◇ left</Term> the <Term k="popular">100 Most Popular</Term>; then
        how it did <Term k="spy">vs SPY</Term> 1 week, 1 month and 3 months later.
      </p>
      {shown.length ? (
        <div className="mui-crowdfeed__list">
          <AnimatedList
            items={shown}
            itemKey={keyOf}
            renderItem={renderItem}
            onItemSelect={onSelect}
            showGradients
            enableArrowNavigation
            ariaLabel={`Crowd list ${kind === 'all' ? 'joins and exits' : kind === 'enter' ? 'joins' : 'exits'}`}
          />
        </div>
      ) : (
        <div className="mui-state mui-state--empty">
          {events.length ? `No list ${kind === 'enter' ? 'joins' : 'exits'} in this window.` : 'No list joins or exits yet.'}
        </div>
      )}
    </div>
  );
}

export default function CrowdFeed({ config = {}, saveConfig }) {
  const api = useCrowd();
  const kind = normKind(config.kind);
  const onKind = useCallback(
    k => {
      if (typeof saveConfig === 'function') saveConfig({ ...config, kind: normKind(k) });
    },
    [config, saveConfig]
  );
  return (
    <div className="mui-crowd mui-crowd--feed">
      <CrowdState api={api} compact loadingRows={3}>
        {d => <Feed data={obj(d)} kind={kind} onKind={onKind} onPage={config.onPage === true} />}
      </CrowdState>
    </div>
  );
}
