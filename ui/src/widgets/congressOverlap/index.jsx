// S3-F1 congressOverlap — "Your overlap" (page #congress only; not a catalog type).
// One card per held or watched ticker that Congress traded in the last 45 days (held_overlap,
// S2-T1): "RKLB — 3 buys / 0 sells in 45d — A, B, C". Clicking a card filters the page's feed
// (page-scoped FILTER_EVENT; the feed is a separate React root) and charts the ticker
// (mason:chart-symbol). Clicking the active card again clears the filter.
// React Bits: SpotlightCard (cards), DecryptedText (page header, plays once on first view).
import { useCallback } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { emitChartSymbol, normTicker } from '../../lib/bridge.js';
import { ROUTE, POLL_MS, overlapOf, namesLine, plural, count, tickerText, chartIdFor, emitCongressFilter, useCongressFilter, moreLabel } from '../congressFeed/congressLib.js';
import './congressOverlap.css';

const CAP = 24; // held + watched tickers rarely exceed this; the rest is stated, never silently dropped

function SplitBar({ buys, sells }) {
  const b = buys || 0;
  const s = sells || 0;
  const total = b + s;
  if (!total) return <div className="co-bar co-bar--empty" aria-hidden="true" />;
  return (
    <div className="co-bar" aria-hidden="true">
      {b ? <span className="co-bar__buy" style={{ flexGrow: b }} /> : null}
      {s ? <span className="co-bar__sell" style={{ flexGrow: s }} /> : null}
    </div>
  );
}

function Card({ o, active, onPick }) {
  const tk = tickerText(o.ticker);
  const buys = count(o.n_buys);
  const sells = count(o.n_sells);
  return (
    <SpotlightCard className={`co-card${active ? ' is-active' : ''}`} spotlightColor="color-mix(in srgb, var(--mui-accent) 22%, transparent)">
      <button type="button" className="co-btn" data-testid="co-card" aria-pressed={active} onClick={() => onPick(o)} title={active ? 'Showing only this ticker — click to show all' : 'Filter the feed to this ticker and chart it'}>
        <span className="co-line">
          <b className="co-tk">{tk}</b>
          {` — ${plural(buys, 'buy', 'buys')} / ${plural(sells, 'sell', 'sells')} in 45d — `}
          <span className="co-names">{namesLine(o.politicians)}</span>
        </span>
        <SplitBar buys={buys} sells={sells} />
        <span className="co-legend" aria-hidden="true">
          <span className="co-legend__buy">■ {buys ?? "—"} buy</span>
          <span className="co-legend__sell">■ {sells ?? "—"} sell</span>
          {active ? <span className="co-legend__on">◆ filtering feed</span> : null}
        </span>
      </button>
    </SpotlightCard>
  );
}

export default function CongressOverlap({ config }) {
  const cfg = config && typeof config === 'object' ? config : {};
  const link = typeof cfg.link === 'string' ? cfg.link : '';
  const api = useRhApi(ROUTE, null, { pollMs: POLL_MS });
  const [active, setActive] = useCongressFilter(link);

  const pick = useCallback(
    o => {
      const tk = normTicker(typeof o.ticker === 'string' ? o.ticker : '') || '';
      const next = tk && tk !== active ? tk : '';
      if (link) emitCongressFilter(link, next); // our own listener updates `active`
      else setActive(next);
      if (next) emitChartSymbol(chartIdFor(link), next);
    },
    [active, link, setActive]
  );

  return (
    <div className="co">
      <div className="co-head" data-testid="co-head">
        <h3 className="co-title">
          <DecryptedText text="CONGRESS · YOUR OVERLAP" animateOn="view" sequential speed={28} revealDirection="start" encryptedClassName="co-enc" />
        </h3>
        <span className="co-sub">held or watched tickers traded by Congress · last 45d</span>
      </div>
      <div className="co-body">
        <RhState api={api} empty={d => overlapOf(d).length === 0} emptyText="None of your held or watched tickers were traded by Congress in the last 45 days." loadingRows={2}>
          {d => {
            const all = overlapOf(d);
            const shown = all.slice(0, CAP); // F1-2: never one card per row of an unbounded list
            return (
              <>
                <div className="co-grid" role="group" aria-label="Your overlap with Congress trades">
                  {shown.map((o, i) => {
                    const tk = normTicker(typeof o.ticker === 'string' ? o.ticker : '') || '';
                    return <Card key={`${tk || 'x'}-${i}`} o={o} active={!!tk && tk === active} onPick={pick} />;
                  })}
                </div>
                {all.length > CAP ? (
                  <p className="co-cap" role="note" data-testid="co-cap">
                    {moreLabel(CAP, all.length)}
                  </p>
                ) : null}
              </>
            );
          }}
        </RhState>
      </div>
    </div>
  );
}
