// S3-F1 congressChart — candles for the selected Congress ticker (page #congress only; not a catalog
// type). Follows mason:chart-symbol addressed to '<link>-main' (overlap cards and feed rows emit it).
// MasonChart has no marker API (F0, MasonChartHost.jsx header), so the disclosure dates for the ticker
// are listed under the chart instead of drawn on it — we never patch the engine.
// The MasonChart id is per instance ('congress-<instanceId>'), so two copies never share a chart record.
// React Bits: none fit a candle chart (the engine is MasonChart, per S3 §2); the dates list reuses the
// feed's BUY/SELL labels. See the hand-off packet.
import { useEffect, useMemo, useState } from 'react';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { onChartSymbol, normTicker } from '../../lib/bridge.js';
import { ROUTE, POLL_MS, tradesOf, overlapOf, sortTrades, filterTrades, sideOf, shortDate, yearOf, lagOf, chartIdFor, moreLabel, ungradedReason, onCongressGo } from '../congressFeed/congressLib.js';
import './congressChart.css';

const CAP = 50; // F1-3: newest 50 disclosure dates; the rest is stated

function firstTicker(d) {
  for (const o of overlapOf(d)) {
    const t = normTicker(typeof o.ticker === 'string' ? o.ticker : '');
    if (t) return t;
  }
  for (const t of sortTrades(tradesOf(d))) {
    const k = normTicker(typeof t.ticker === 'string' ? t.ticker : '');
    if (k) return k;
  }
  return null;
}

function Dates({ data, symbol, refYear }) {
  const rows = useMemo(() => filterTrades(sortTrades(tradesOf(data)), { ticker: symbol }), [data, symbol]);
  if (!symbol) return <p className="cc-none">Pick an overlap card or a feed row to chart it.</p>;
  if (!rows.length) return <p className="cc-none">No disclosures for {symbol} in this window.</p>;
  return (
    <>
      <ol className="cc-dates" aria-label={`Disclosure dates for ${symbol}`}>
        {rows.slice(0, CAP).map((t, i) => {
          const { side, label } = sideOf(t);
          const lag = lagOf(t);
          const ug = ungradedReason(t); // OQ-3: never imply a grade the backend will not compute
          return (
            <li key={typeof t.id === 'string' && t.id ? t.id : `i${i}`} className="cc-date" data-testid="cc-date">
              <span className="cc-date__d">{shortDate(t.disclosure_date, refYear)}</span>
              <span className={`cf-side is-${side}`}>{label}</span>
              <span className="cc-date__who">{typeof t.politician === 'string' && t.politician ? t.politician : '—'}</span>
              <span className="cc-date__tx">
                tx {shortDate(t.tx_date, refYear)} · {lag == null ? '—' : `${lag}d`} lag
              </span>
              {ug ? (
                <span className="cc-date__ug" title={ug.why}>
                  {ug.label}
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      {rows.length > CAP ? (
        <p className="cc-cap" role="note" data-testid="cc-cap">
          {moreLabel(CAP, rows.length)}
        </p>
      ) : null}
    </>
  );
}

export default function CongressChart({ config, instanceId }) {
  const cfg = config && typeof config === 'object' ? config : {};
  const link = typeof cfg.link === 'string' ? cfg.link : '';
  const groupChart = chartIdFor(link);
  const api = useRhApi(ROUTE, null, { pollMs: POLL_MS });
  const [picked, setPicked] = useState(null);
  useEffect(() => (groupChart ? onChartSymbol(d => setPicked(d.ticker), { chartId: groupChart }) : undefined), [groupChart]);
  useEffect(() => (groupChart ? onCongressGo(setPicked) : undefined), [groupChart]); // S3-F6 go focus

  const data = api.status === 'ok' ? api.data : null;
  const symbol = picked || firstTicker(data) || '';
  const refYear = yearOf(api.asOf) || new Date().getUTCFullYear();
  const n = data && symbol ? filterTrades(tradesOf(data), { ticker: symbol }).length : null;

  return (
    <div className="cc">
      <div className="cc-head">
        <b className="cc-sym" data-testid="cc-sym">
          {symbol || '—'}
        </b>
        <span className="cc-sub">daily candles</span>
        {n != null ? (
          <span className="cc-count">
            {n} {n === 1 ? 'disclosure' : 'disclosures'}
          </span>
        ) : null}
      </div>
      <div className="cc-chart">
        <MasonChartHost chartId={`congress-${instanceId}`} symbol={symbol} tf="1d" follow={false} />
      </div>
      <div className="cc-foot">
        <div className="cc-foot__title">
          Disclosure dates <span className="cc-foot__note">(markers are not supported by the chart engine yet, so they are listed here)</span>
        </div>
        <RhState api={api} empty={d => tradesOf(d).length === 0} emptyText="No congressional disclosures yet." loadingRows={2}>
          {d => <Dates data={d} symbol={symbol} refYear={refYear} />}
        </RhState>
      </div>
    </div>
  );
}
