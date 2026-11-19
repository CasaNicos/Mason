// S3-F6 realmoneyChart — #realmoney page chart. Candles on Mason's own engine via <MasonChartHost>
// (window.MasonChart); no chart library. Symbol: the last ticker sent to chartId 'main'
// (rhExposure tile / rhAlertsLog fire), else the largest look-through exposure (shared
// /api/rh/exposure request with rhExposure's default "Mine" scope, so no extra request).
import { useEffect, useMemo, useState } from 'react';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { useRhApi } from '../../lib/api.js';
import { normTicker, onChartSymbol } from '../../lib/bridge.js';
import { isNum } from '../../lib/fmt.js';
import './realmoneyChart.css';

export const MAIN_CHART = 'main';

/** Largest node by total_pct (ties: input order); junk rows skipped. */
export function topTicker(d) {
  const nodes = d && Array.isArray(d.nodes) ? d.nodes : [];
  let best = null;
  for (const n of nodes) {
    if (!n || typeof n !== 'object' || n.other) continue;
    const t = normTicker(typeof n.ticker === 'string' ? n.ticker : '');
    if (!t || !isNum(n.total_pct)) continue;
    if (!best || n.total_pct > best.p) best = { t, p: n.total_pct };
  }
  return best ? best.t : null;
}

export default function RealmoneyChart({ instanceId }) {
  const api = useRhApi('/api/rh/exposure', null, { pollMs: 60000 });
  const [picked, setPicked] = useState(null);
  useEffect(() => onChartSymbol(d => setPicked(d.ticker), { chartId: MAIN_CHART }), []);
  const fallback = useMemo(() => (api.status === 'ok' ? topTicker(api.data) : null), [api.status, api.data]);
  const symbol = picked || fallback || '';

  return (
    <div className="rmc">
      <div className="rmc-head">
        <b className="rmc-sym" data-testid="rmc-sym">
          {symbol || '—'}
        </b>
        <span className="rmc-sub">daily candles</span>
        <span className="rmc-hint">{picked ? 'last clicked holding' : symbol ? 'largest exposure · click a tile or an alert to chart it' : ''}</span>
      </div>
      <div className="rmc-chart">
        {symbol ? (
          <MasonChartHost chartId={`realmoney-${instanceId}`} symbol={symbol} tf="1d" follow={false} />
        ) : (
          <p className="rmc-empty" role="note">
            Click a tile in True Exposure or an alert in the RH Alerts Log to chart it here.
          </p>
        )}
      </div>
    </div>
  );
}
