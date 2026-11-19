// scannerScorecard — "which scanner actually has edge" (SC §4.6, on the page; K8).
// One React Bits SpotlightCard per scan; the hit rate at each graded horizon counts up with React
// Bits CountUp. Every rate shows n and its Wilson CI95; it reads "early signal" unless the backend
// says evidence == "significant"; min_n_met false → "too few graded hits to use (n=…)".
// The connection pill is a React Bits GlassSurface fed by /api/scanners (shared cache with the lists).
import { useMemo } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import GlassSurface from '../../reactbits/GlassSurface/GlassSurface.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { arrow, dirClass, pct, nLabel, date, isNum, num, DASH } from '../../lib/fmt.js';
import { SCANNERS_ROUTE, POLL_MS, hhmm, isDisconnected, statusOf, cellText } from '../scannerShared/scan.js';
import '../scannerShared/scanner.css';
import './scannerScorecard.css';

const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const WINDOW = { rth: 'regular hours', extended: 'extended hours' };
const MAX_CARDS = 12; // a 13th+ scan folds into "+N more" (the page itself lays out 4)
// Counts and n: finite, non-negative, sane (a 1e308 or NaN from a bad payload never reaches CountUp).
const okCount = v => isNum(v) && v >= 0 && v < 1e9;
const cnt = v => (okCount(v) ? num(v) : DASH);
const unit = v => isNum(v) && v >= 0 && v <= 1;

function Horizon({ k, h, minMet }) {
  const n = isObj(h) && okCount(h.n) ? Math.round(h.n) : 0;
  const rate = isObj(h) && unit(h.hit_rate) ? Math.round(h.hit_rate * 1000) / 10 : null;
  const ci = isObj(h) && Array.isArray(h.hit_rate_ci95) && h.hit_rate_ci95.length === 2 && h.hit_rate_ci95.every(unit) ? h.hit_rate_ci95 : null;
  const sig = isObj(h) && h.evidence === 'significant';
  const signedV = isObj(h) && isNum(h.avg_signed_pct) ? h.avg_signed_pct : null;
  const xs = isObj(h) && isNum(h.avg_signed_xs_pct) ? h.avg_signed_xs_pct : null;
  const move = isObj(h) && isNum(h.avg_move_pct) ? h.avg_move_pct : null;
  const few = minMet === false;
  if (!n || rate == null) {
    return (
      <div className="ssc-hz is-empty" data-horizon={k}>
        <span className="ssc-hz-l">+{k}d</span>
        <span className="mui-muted">{n ? `rate unavailable (${nLabel(n)})` : `no graded hits yet (${nLabel(n)})`}</span>
      </div>
    );
  }
  return (
    <div className={`ssc-hz${few ? ' is-few' : ''}${sig ? ' is-sig' : ''}`} data-horizon={k}>
      <span className="ssc-hz-l" title={`Graded ${k} trading session${k === '1' ? '' : 's'} after the hit`}>
        +{k}d
      </span>
      <span className="ssc-rate-wrap" title="Share of graded hits that moved in the called direction">
        <CountUp className="sc-rate" to={rate} duration={0.6} />
        <span className="ssc-pct">%</span>
      </span>
      <span className="ssc-meta">
        <span className="mui-n">{nLabel(n)}</span>
        {ci ? (
          <span>
            CI95 {Math.round(ci[0] * 100)}–{Math.round(ci[1] * 100)}%
          </span>
        ) : null}
        {signedV != null ? (
          <span title="Average move in the called direction">
            <span className={dirClass(signedV)}>{`${arrow(signedV)} ${pct(signedV, { digits: 2 })}`}</span> signed
          </span>
        ) : null}
        {move != null ? <span title="Average absolute move">avg move {pct(move, { digits: 2, signed: false })}</span> : null}
        {xs != null ? (
          <span title="Average signed move minus SPY over the same sessions">
            vs SPY <span className={dirClass(xs)}>{`${arrow(xs)} ${pct(xs, { digits: 2 })}`}</span>
          </span>
        ) : null}
      </span>
      <span className="ssc-tags">
        {sig ? (
          <span className="mui-badge ssc-sig">✓ significant</span>
        ) : (
          <span className="mui-badge ssc-early" title="Not significant yet: the CI95 still includes 50% or n is below the minimum">
            early signal
          </span>
        )}
        {few ? <span className="ssc-few">too few graded hits to use ({nLabel(n)})</span> : null}
      </span>
    </div>
  );
}

function StatusPill({ live }) {
  const st = live.status === 'ok' ? statusOf(live.data) : null;
  const disc = isDisconnected(st);
  let text = 'Robinhood: …';
  if (live.status === 'off') text = 'Scanners: off';
  else if (live.status === 'error') text = 'Robinhood: status unavailable';
  else if (disc) text = `Robinhood: reconnect · last poll ${hhmm(st.last_poll)}`;
  else if (st) text = `Robinhood: connected · ${WINDOW[st.window] || 'market closed'} · polled ${hhmm(st.last_poll)}`;
  return (
    <GlassSurface width="auto" height="auto" borderRadius={999} className={`ssc-pill${disc ? ' is-disc' : ''}`}>
      <span className="ssc-pill-txt" role="status">
        <span aria-hidden="true">{disc ? '⚠' : '●'}</span> {text}
      </span>
    </GlassSurface>
  );
}

export default function ScannerScorecard() {
  const api = useRhApi('/api/scanners/scorecard');
  const live = useRhApi(SCANNERS_ROUTE, null, { pollMs: POLL_MS });
  const flags = live.status === 'ok' && isObj(live.data) && isObj(live.data.flags) ? live.data.flags : null;
  return (
    <div className="ssc">
      <div className="sc-bar ssc-bar">
        <StatusPill live={live} />
        {flags && flags.trade_path === false ? (
          <span className="mui-badge" title="SC-D1: hits are logged, triaged and graded but cannot lead to a trade">
            observe only
          </span>
        ) : null}
        {api.status === 'ok' && isObj(api.data) && api.data.since ? <span className="sc-run ssc-since">graded since {date(api.data.since)}</span> : null}
        <span className="sc-run ssc-rule">rates read “early signal” until the CI95 excludes 50% with enough graded hits</span>
      </div>
      <RhState api={api} empty={d => !isObj(d) || !Array.isArray(d.scans) || !d.scans.length} emptyText="No scans graded yet.">
        {d => <Cards scans={d.scans} />}
      </RhState>
    </div>
  );
}

function Cards({ scans }) {
  const all = useMemo(() => scans.filter(s => isObj(s) && s.scan_id), [scans]);
  const list = all.slice(0, MAX_CARDS);
  return (
    <div className="ssc-grid">
      {all.length > MAX_CARDS ? (
        <div className="ssc-more" title={all.slice(MAX_CARDS).map(s => cellText(s.title)).join(' · ')}>
          +{all.length - MAX_CARDS} more scans
        </div>
      ) : null}
      {list.map(s => {
        const hz = isObj(s.horizons) ? s.horizons : {};
        const keys = Object.keys(hz).sort((a, b) => Number(a) - Number(b));
        const met = isObj(s.min_n_met) ? s.min_n_met : {};
        return (
          <SpotlightCard key={s.scan_id} className="sc-card" spotlightColor="rgba(74, 158, 255, 0.16)">
            <div className="ssc-title" title={cellText(s.title)}>
              {cellText(s.title)}
            </div>
            <div className="ssc-counts">
              {cnt(s.n_hits)} hits · {cnt(s.n_triaged)} triaged · {cnt(s.n_proposed)} proposed · {cnt(s.n_held_advice)} advice
            </div>
            {keys.map(k => (
              <Horizon key={k} k={k} h={hz[k]} minMet={met[k]} />
            ))}
          </SpotlightCard>
        );
      })}
    </div>
  );
}
