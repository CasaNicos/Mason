// S3-F1 congressFeed — congressional disclosure feed (page #congress + WM catalog entry, multi).
// React Bits: AnimatedList (rows: entrance + keyboard + 200 cap), CountUp (disclosure count).
// Settings {ticker, heldOnly} persist through saveConfig (always the full config, so page keys such
// as `link` survive). On the Congress page (config.link = 'congress') it also follows the overlap
// cards' page filter, and a row click charts the ticker on the page chart (mason:chart-symbol).
import { useCallback, useMemo, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { emitChartSymbol, emitGo, normTicker } from '../../lib/bridge.js';
import { pct, arrow, dirClass, isNum, dateTime } from '../../lib/fmt.js';
import {
  ROUTE,
  POLL_MS,
  CAPTION,
  tradesOf,
  sortTrades,
  filterTrades,
  sideOf,
  partyBadge,
  amountRange,
  shortDate,
  yearOf,
  lagOf,
  fwdCells,
  fwdTitle,
  tickerText,
  chartIdFor,
  useCongressFilter,
  CONGRESS_PAGE
} from './congressLib.js';
import './congressFeed.css';

const MAX_ROWS = 200;

function FwdCell({ c }) {
  if (c.state === 'ungraded') {
    return (
      <span className="cf-fwd__cell is-ungraded" title={c.why}>
        {c.label}
      </span>
    );
  }
  if (c.state !== 'matured' || !isNum(c.value) || !Number.isFinite(c.value * 100)) {
    const word = c.state === 'delisted' ? 'delisted' : c.state === 'pending' ? 'pending' : '—';
    return (
      <span className="cf-fwd__cell is-pending" title={fwdTitle(c)}>
        <span className="cf-fwd__h">{c.h}d</span> <span className="cf-fwd__v">{word}</span>
      </span>
    );
  }
  const v = c.value * 100;
  return (
    <span className={`cf-fwd__cell ${dirClass(v)}`} title={fwdTitle(c)}>
      <span className="cf-fwd__h">{c.h}d</span>
      <span className="cf-fwd__v">
        {arrow(v)} {pct(v)}
      </span>
    </span>
  );
}

function Row({ t, refYear, charted }) {
  const { side, label } = sideOf(t);
  const cells = fwdCells(t);
  const inverted = cells.some(c => c.state === 'matured' && c.inverted);
  const lag = lagOf(t);
  const asset = typeof t.asset_type === 'string' && t.asset_type && t.asset_type !== 'equity' ? t.asset_type : null;
  return (
    <div className={`cf-row is-${side}${charted ? ' is-charted' : ''}`}>
      <div className="cf-l1">
        <span className={`cf-side is-${side}`}>{label}</span>
        <b className="cf-tk">{tickerText(t.ticker)}</b>
        {asset ? <span className="cf-tag">{asset}</span> : null}
        {t.held === true ? <span className="cf-tag cf-tag--own">held</span> : t.watched === true ? <span className="cf-tag">watched</span> : null}
        <span className="cf-amt" title="Disclosed amount range">
          {amountRange(t.amount_low, t.amount_high)}
        </span>
      </div>
      <div className="cf-l2">
        <span className="cf-who">{typeof t.politician === 'string' && t.politician ? t.politician : '—'}</span>
        <span className="mui-badge cf-party">{partyBadge(t.chamber, t.party)}</span>
        {charted ? <span className="cf-charted">◆ charted</span> : null}
      </div>
      <div className="cf-l3">
        <span className="cf-dates" title="transaction date → disclosure date">
          {shortDate(t.tx_date, refYear)} → {shortDate(t.disclosure_date, refYear)}
        </span>
        <span className="cf-lag">{lag == null ? '— lag' : `${lag}d lag`}</span>
        <span className="cf-fwd" aria-label="Forward excess vs SPY">
          {cells.map(c => (
            <FwdCell key={c.h} c={c} />
          ))}
          {inverted ? (
            <span className="cf-tag cf-tag--inv" title="Sell: graded with the sign inverted (a drop after the sale counts as right)">
              inverted
            </span>
          ) : null}
        </span>
      </div>
    </div>
  );
}

function FeedList({ data, ticker, heldOnly, link, asOf, onClearTicker }) {
  const refYear = yearOf(asOf) || new Date().getUTCFullYear();
  const all = useMemo(() => sortTrades(tradesOf(data)), [data]);
  const rows = useMemo(() => filterTrades(all, { ticker, heldOnly }), [all, ticker, heldOnly]);
  const [charted, setCharted] = useState(null);
  const keyOf = useCallback((t, i) => (typeof t.id === 'string' && t.id ? t.id : `i${i}`), []);

  const onSelect = useCallback(
    (t, i) => {
      setCharted(keyOf(t, i));
      const tk = normTicker(typeof t.ticker === 'string' ? t.ticker : '');
      if (tk) emitChartSymbol(chartIdFor(link), tk);
      if (tk && !link) emitGo(CONGRESS_PAGE, tk); // S3-F6: a dashboard row opens #congress on that ticker
    },
    [keyOf, link]
  );

  const capped = rows.length > MAX_ROWS;
  return (
    <>
      <div className="cf-meta" data-testid="cf-count">
        {capped ? (
          <b>
            {MAX_ROWS.toLocaleString('en-US')} of {rows.length.toLocaleString('en-US')}
          </b>
        ) : (
          <b>
            <CountUp to={rows.length} duration={0.8} separator="," />
          </b>
        )}{' '}
        <span>{rows.length === 1 ? 'disclosure' : 'disclosures'}</span>
        {rows.length !== all.length && !capped ? <span className="mui-muted"> of {all.length.toLocaleString('en-US')}</span> : null}
      </div>
      {rows.length === 0 ? (
        <div className="cf-empty" role="status">
          {ticker ? `No disclosures for ${ticker}${heldOnly ? ' in held tickers' : ''}.` : 'No disclosures in held tickers.'}{' '}
          {ticker ? (
            <button type="button" className="mui-btn mui-btn--ghost cf-linkbtn" onClick={onClearTicker}>
              Show all
            </button>
          ) : null}
        </div>
      ) : (
        <div className="cf-list">
          <AnimatedList
            items={rows}
            itemKey={keyOf}
            maxItems={MAX_ROWS}
            renderItem={(t, i) => <Row t={t} refYear={refYear} charted={charted === keyOf(t, i)} />}
            onItemSelect={onSelect}
            showGradients
            enableArrowNavigation
            displayScrollbar
            ariaLabel={ticker ? `Congress disclosures for ${ticker}` : 'Congress disclosures'}
          />
        </div>
      )}
    </>
  );
}

export default function CongressFeed({ config, saveConfig }) {
  const cfg = config && typeof config === 'object' ? config : {};
  const link = typeof cfg.link === 'string' ? cfg.link : '';
  const api = useRhApi(ROUTE, null, { pollMs: POLL_MS });
  const [pageTicker, setPageTicker] = useCongressFilter(link);
  const cfgTicker = normTicker(typeof cfg.ticker === 'string' ? cfg.ticker : '') || '';
  const ticker = pageTicker || cfgTicker;
  const heldOnly = cfg.heldOnly === true;

  const save = useCallback(patch => saveConfig && saveConfig({ ...cfg, ...patch }), [cfg, saveConfig]);
  const clearTicker = useCallback(() => {
    if (pageTicker) setPageTicker('');
    else save({ ticker: '' });
  }, [pageTicker, setPageTicker, save]);

  return (
    <div className="cf">
      <div className="cf-head">
        <span className="cf-title">DISCLOSURES</span>
        {ticker ? (
          <span className="cf-chip">
            <span>{ticker}</span>
            <button type="button" className="cf-chip__x" aria-label={`Clear ticker filter ${ticker}`} title="Clear ticker filter" onClick={clearTicker}>
              ✕
            </button>
          </span>
        ) : null}
        <button type="button" className={`cf-toggle${heldOnly ? ' is-on' : ''}`} aria-pressed={heldOnly} onClick={() => save({ heldOnly: !heldOnly })}>
          <span aria-hidden="true">{heldOnly ? '◉' : '○'}</span> Held only
        </button>
        {api.asOf ? <span className="cf-asof">as of {dateTime(api.asOf)}</span> : null}
      </div>
      <div className="cf-body">
        <RhState api={api} empty={d => tradesOf(d).length === 0} emptyText="No congressional disclosures yet." loadingRows={4}>
          {d => <FeedList data={d} ticker={ticker} heldOnly={heldOnly} link={link} asOf={api.asOf} onClearTicker={clearTicker} />}
        </RhState>
      </div>
      <p className="cf-caption">{CAPTION}</p>
    </div>
  );
}
