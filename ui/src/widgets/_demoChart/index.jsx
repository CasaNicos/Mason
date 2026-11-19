// F0 demo: click a row → emitChartSymbol(chartId, ticker) → <MasonChartHost chartId> follows.
// The chartId is per instance, so two copies of this widget never drive each other's chart.
import { useState } from 'react';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { emitChartSymbol } from '../../lib/bridge.js';
import './demoChart.css';

export default function DemoChart({ config, instanceId }) {
  const tickers = Array.isArray(config.tickers) ? config.tickers.map(String) : [];
  const chartId = `demo-${instanceId}`;
  const [sel, setSel] = useState(tickers[0] || 'SPY');
  const pick = t => {
    setSel(t);
    emitChartSymbol(chartId, t);
  };
  return (
    <div className="mui-demochart">
      <ul className="mui-demochart__list" aria-label="Tickers">
        {tickers.map(t => (
          <li key={t}>
            <button
              type="button"
              className={`mui-demochart__row${t === sel ? ' is-sel' : ''}`}
              aria-pressed={t === sel}
              data-ticker={t}
              onClick={() => pick(t)}
            >
              {t}
            </button>
          </li>
        ))}
      </ul>
      <div className="mui-demochart__chart">
        <MasonChartHost chartId={chartId} symbol={tickers[0] || 'SPY'} tf="1d" />
      </div>
    </div>
  );
}
