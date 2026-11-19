// scannerChart — 1-minute candles (window.MasonChart via <MasonChartHost>) of whatever ticker the
// scanner lists on the same link channel announce (SC §5: {kind:'symbol', type:'candle', tf:'1m'}).
// Re-link with the chip; any number of lists can share a channel, so one chart can follow two lists.
// On mount / re-link it says hello on its channel, so it shows its list's current (top) row at once.
import { useEffect, useState } from 'react';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { onChartSymbol } from '../../lib/bridge.js';
import LinkPicker from '../scannerShared/LinkPicker.jsx';
import { channel, linkChartId, sayHello } from '../scannerShared/link.js';
import '../scannerShared/scanner.css';
import './scannerChart.css';

const safeId = v => String(v == null ? 'x' : v).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);

export default function ScannerChart({ config, saveConfig, instanceId }) {
  const ch = channel(config.link);
  const link = ch.id;
  const [sym, setSym] = useState(null);
  const [heard, setHeard] = useState(false);

  useEffect(() => {
    setHeard(false);
    const off = onChartSymbol(
      ({ ticker }) => {
        setSym(ticker);
        setHeard(true);
      },
      { chartId: linkChartId(link) }
    );
    sayHello(link);
    return off;
  }, [link]);

  return (
    <div className={`scc sc-link--${link}`}>
      <div className="sc-bar">
        <LinkPicker value={link} what="chart" onChange={id => saveConfig({ ...config, link: id })} />
        <b className="scc-sym" aria-live="polite">
          {sym || '—'}
        </b>
        <span className="sc-sort">1m candles</span>
        {sym && !heard ? (
          <span className="sc-run">
            no list on {ch.glyph} Link {ch.label} yet
          </span>
        ) : null}
      </div>
      <div className="scc-body">
        {sym ? (
          <MasonChartHost chartId={`scanner-${safeId(instanceId)}`} symbol={sym} tf="1m" type="candle" follow={false} />
        ) : (
          <div className="mui-state mui-state--empty scc-wait">
            Waiting for a list on {ch.glyph} Link {ch.label}… set any scanner list to {ch.glyph} {ch.label} to drive this chart.
          </div>
        )}
      </div>
    </div>
  );
}
