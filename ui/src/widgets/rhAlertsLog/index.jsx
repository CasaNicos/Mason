// rhAlertsLog — "Which of my Robinhood price alerts fired, and what are my alert habits?"
// (S3 §3 F4.4, contracts §6 /api/rh/alerts/log). Fires in the React Bits AnimatedList (row select
// charts the symbol); one habits line from `patterns`. Informational only: suggested levels are
// text marked "not set" — there is no control that sets anything at Robinhood (contracts §2.1).
import { useCallback } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { emitChartSymbol } from '../../lib/bridge.js';
import { money, pct, num, dateTime, nLabel } from '../../lib/fmt.js';
import { fin, objs, count, MONEY_MAX, PCT_MAX, COUNT_MAX } from '../rhAccounts/guard.js';
import './rhAlertsLog.css';

const DAYS = 30;
const FIRE_CAP = 50; // AnimatedList rows (one motion + IntersectionObserver each); the list states "50 of N shown"
const CHART_ID = 'main';
const DIR = { up: '▲', down: '▼' };
const VERB = { price_crosses: 'crossed', price_above: 'went above', price_below: 'went below' };

export function fireText(f) {
  const dir = typeof f.direction === 'string' ? f.direction.toLowerCase() : '';
  const verb = VERB[f.condition] || String(f.condition || 'fired').replace(/_/g, ' ');
  const a = DIR[dir] ? `${DIR[dir]} ` : '';
  return `${a}${verb}${dir ? ` ${dir}` : ''}`;
}

export function habitText(p) {
  const parts = [];
  const nfv = fin(p.n_fires, COUNT_MAX);
  const nlv = fin(p.n_levels, COUNT_MAX);
  const nf = nfv == null || nfv < 0 ? null : Math.round(nfv);
  const nl = nlv == null || nlv < 0 ? null : Math.round(nlv);
  parts.push(
    `${p.ticker}: ${count(nf)} fire${nf === 1 ? '' : 's'} on ${count(nl)} level${nl === 1 ? '' : 's'} (${nLabel(nf)})`
  );
  const bias = fin(p.round_number_bias, 1);
  if (bias != null && bias >= 0) parts.push(`${pct(bias * 100, { signed: false, digits: 0 })} round-number levels`);
  const off = fin(p.typical_offset_pct, PCT_MAX);
  if (off != null) parts.push(`typically ${num(off, 1)}% from price`);
  return parts.join(' · ');
}

function Habits({ patterns }) {
  const ps = objs(patterns).filter(p => p.ticker != null);
  if (!ps.length) {
    return (
      <div className="rhl-habits" data-testid="alerts-habits">
        <span className="mui-muted">No alert habits yet (no fires in the window).</span>
      </div>
    );
  }
  const top = ps[0];
  const idea = objs(top.proposed_levels).find(l => fin(l.level, MONEY_MAX) != null);
  return (
    <div className="rhl-habits" data-testid="alerts-habits">
      <span className="rhl-habits__line">
        <span className="rhl-habits__k">Your habits</span> {habitText(top)}
        {ps.length > 1 ? <span className="mui-n"> · +{count(ps.length - 1)} more ticker{ps.length > 2 ? 's' : ''}</span> : null}
      </span>
      {top.note || idea ? (
        <span className="rhl-habits__note">
          {top.note ? <span>{String(top.note)}</span> : null}
          {idea ? (
            <span className="rhl-idea">
              {' '}Idea (not set): {money(idea.level)}
              {idea.basis ? ` — ${String(idea.basis)}` : ''}
            </span>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

function FireRow({ f }) {
  const dir = typeof f.direction === 'string' ? f.direction.toLowerCase() : '';
  return (
    <div className="rhl-row" data-testid={`fire-${f.id}`}>
      <b className="rhl-tk">{f.symbol}</b>
      <span className={`rhl-what ${dir === 'up' ? 'mui-up' : dir === 'down' ? 'mui-down' : 'mui-flat'}`}>{fireText(f)}</span>
      <span className="rhl-lvl">{money(fin(f.level, MONEY_MAX))}</span>
      <span className="rhl-at">at {money(fin(f.price_at_fire, MONEY_MAX))}</span>
      <span className="mui-n rhl-time">{dateTime(f.fired_at)}</span>
    </div>
  );
}

function Log({ data }) {
  const fires = objs(data.fires); // AnimatedList renders at most 200 and states "200 of N shown"

  const onSelect = useCallback(f => emitChartSymbol(CHART_ID, f && f.symbol), []);
  return (
    <>
      <Habits patterns={data.patterns} />
      <div className="rhl-sub">
        <span>
          {count(fires.length)} fire{fires.length === 1 ? '' : 's'} · last {DAYS} days
        </span>
        <span className="mui-badge">info only</span>
      </div>
      {fires.length ? (
        <div className="rhl-list">
          <AnimatedList
            items={fires}
            itemKey={(f, i) => (f.id != null ? String(f.id) : `i${i}`)}
            renderItem={f => <FireRow f={f} />}
            onItemSelect={onSelect}
            maxItems={FIRE_CAP}
            showGradients
            enableArrowNavigation
            ariaLabel="Robinhood alert fires"
          />
        </div>
      ) : (
        <div className="mui-state mui-state--empty">No alerts fired in the last {DAYS} days.</div>
      )}
    </>
  );
}

export default function RhAlertsLog() {
  const api = useRhApi('/api/rh/alerts/log', { days: DAYS }, { pollMs: 60000 });
  return (
    <div className="rhl">
      <RhState api={api} empty={d => !d || typeof d !== 'object'} emptyText="No alert data.">
        {d => <Log data={d} />}
      </RhState>
    </div>
  );
}
