// rhPvr — Paper vs Real (S3 §3 F5.1; contracts §6 GET /api/rh/pvr?range=; S2-T5).
// One question at a glance: "is Mason's paper book beating my real trades?"
//  - Heroes: React Bits <Counter> for both cumulative returns (sign + ▲/▼ beside it).
//  - React + SVG line chart (PvrChart.jsx), one y-axis, crosshair + tooltip.
//  - Account selector defaults to "Mine" = the route's real_ret["all"] key, which S2-T5 computes
//    over summary.accounts_in_scope (family accounts excluded). K4: the route has no account
//    param; the per-account lines come from the real_cum keys the route returns. Open question.
//  - 1M / 3M / YTD / All → ?range= (backend upper-cases; ALL = since the first order).
//  - Head-to-head (W/L/tie with n), overlap list (winner as a word), caveats always visible,
//    stale badge (envelope) + drift badge (any reconcile drift > 1% or not reconcilable).
import { useMemo, useState } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { arrow, date, dirClass, isNum, money, num, pct } from '../../lib/fmt.js';
import PvrChart from './PvrChart.jsx';
import { Pills, RollNumber, acctLabel, capList, capNote, fin, nl, useAccountScopes } from './f5shared.jsx';
import './f5shared.css';
import './pvr.css';

const RANGES = [
  { value: '1M', label: '1M' },
  { value: '3M', label: '3M' },
  { value: 'YTD', label: 'YTD' },
  { value: 'ALL', label: 'All' }
];
const MINE = 'all';
const DRIFT_LIMIT = 1; // percent (S2-T5: drift > 1% → stale)
// Render caps (Debugger 10k-row fixtures): the chart draws at most CHART_MAX sessions (evenly
// decimated, first + last kept); lists end with "N of M shown".
const CHART_MAX = 600;
const OVERLAP_MAX = 50;
const CAVEAT_MAX = 20;
const LIST_MAX = 20;

/** Evenly spaced subset of at most `max` rows, always keeping the first and the last. */
function decimate(rows, max) {
  if (rows.length <= max) return rows;
  const out = [];
  for (let k = 0; k < max; k++) out.push(rows[Math.round((k * (rows.length - 1)) / (max - 1))]);
  return out;
}

const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const arr = v => (Array.isArray(v) ? v : []);
const strs = v => arr(v).filter(s => typeof s === 'string');

function Hero({ label, value, cls, unavailable }) {
  const text = fin(value) ? pct(value, { digits: 2 }) : unavailable;
  return (
    <div className={`mui-pvr__hero mui-pvr__hero--${cls}`}>
      <span className="mui-pvr__herolabel">
        <span className={`mui-pvr__key mui-pvr__key--${cls}`} aria-hidden="true" />
        {label}
      </span>
      <span className={`mui-pvr__heroval ${fin(value) ? dirClass(value) : 'mui-muted'}`}>
        <RollNumber
          value={value}
          digits={2}
          before={fin(value) ? `${arrow(value)} ${pct(value, { digits: 2 })[0] === '-' ? '-' : pct(value, { digits: 2 })[0] === '+' ? '+' : ''}` : ''}
          after="%"
          text={text}
        />
      </span>
    </div>
  );
}

const WINNER = { mason: 'Mason won', nico: 'Nico won', tie: 'Tie' };

function OverlapRow({ o, scopes }) {
  const [open, setOpen] = useState(false);
  const p = obj(o.paper);
  const r = obj(o.real);
  const word = WINNER[o.winner] || 'Pending';
  const pending = !WINNER[o.winner];
  return (
    <li className={`mui-pvr__ov${pending ? ' is-pending' : ''}`}>
      <button type="button" className="mui-pvr__ovbtn" aria-expanded={open ? 'true' : 'false'} onClick={() => setOpen(v => !v)}>
        <b className="mui-pvr__tk">{String(o.ticker ?? '—')}</b>
        <span className="mui-pvr__dir">{o.direction === 'short' ? 'short' : o.direction === 'long' ? 'long' : String(o.direction ?? '')}</span>
        <span className={dirClass(p.ret_pct)} title="Mason paper return">
          M {arrow(p.ret_pct)} {pct(p.ret_pct, { digits: 1 })}
        </span>
        <span className={dirClass(r.ret_pct)} title="Nico real return">
          N {arrow(r.ret_pct)} {pct(r.ret_pct, { digits: 1 })}
        </span>
        <span className={`mui-pvr__win mui-pvr__win--${pending ? 'pending' : o.winner}`}>{pending ? 'Pending (window open)' : word}</span>
      </button>
      {open ? (
        <div className="mui-pvr__ovmore">
          <div>
            Mason paper: {date(p.entry_date)} @ {money(p.entry_px)} → {p.exit_date ? `${date(p.exit_date)} @ ${money(p.exit_px)}` : 'open'}
          </div>
          <div>
            Nico real ({acctLabel(r.account ?? '—', scopes)}): {date(r.entry_date)} @ {money(r.entry_px)} →{' '}
            {r.exit_date ? `${date(r.exit_date)} @ ${money(r.exit_px)}` : 'open'}
          </div>
          <div className="mui-n">
            {fin(o.sessions_apart) ? `${num(o.sessions_apart)} sessions apart` : ''}
            {fin(o.window_sessions) ? ` · ${num(o.window_sessions)}-session window` : ''}
            {isNum(o.diff_pct) ? ` · Mason − Nico ${pct(o.diff_pct, { digits: 2 })}` : ''}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function Body({ d, api, account, setAccount, scopes, range, setRange }) {
  const series = arr(d.series).filter(r => r && typeof r === 'object');
  const summary = obj(d.summary);
  const realKeys = useMemo(() => {
    const keys = new Set();
    for (const r of series) for (const k of Object.keys(obj(r.real_cum))) keys.add(k);
    const per = [...keys].filter(k => k !== MINE).sort();
    return keys.has(MINE) || !per.length ? [MINE, ...per] : per;
  }, [series]);
  const acct = realKeys.includes(account) ? account : realKeys[0];
  const realName = acct === MINE ? 'Mine' : acct;
  const labels = useMemo(() => ({ paper: 'Mason paper', real: `You · ${realName}` }), [realName]);

  const rows = useMemo(
    () =>
      series.map(r => ({
        date: r.date,
        paper: fin(r.paper_cum) ? r.paper_cum : null,
        paperDay: fin(r.paper_ret) ? r.paper_ret : null,
        real: fin(obj(r.real_cum)[acct]) ? obj(r.real_cum)[acct] : null,
        realDay: fin(obj(r.real_ret)[acct]) ? obj(r.real_ret)[acct] : null
      })),
    [series, acct]
  );
  const lastNum = key => {
    for (let i = rows.length - 1; i >= 0; i--) if (fin(rows[i][key])) return rows[i][key];
    return null;
  };
  const chartRows = useMemo(() => decimate(rows, CHART_MAX), [rows]);
  const paperCum = fin(summary.paper_cum_pct) ? summary.paper_cum_pct : lastNum('paper');
  const realCum = fin(obj(summary.real_cum_pct)[acct]) ? obj(summary.real_cum_pct)[acct] : lastNum('real');
  const gap = fin(paperCum) && fin(realCum) ? paperCum - realCum : null;

  const inScope = strs(summary.accounts_in_scope);
  const excluded = strs(summary.excluded_accounts);
  const recon = arr(summary.reconcile).filter(x => x && typeof x === 'object');
  const drifting = recon.filter(x => (fin(x.drift_pct) && Math.abs(x.drift_pct) > DRIFT_LIMIT) || x.ok === false);
  const worst = drifting.filter(x => fin(x.drift_pct)).sort((a, b) => Math.abs(b.drift_pct) - Math.abs(a.drift_pct))[0];
  const h2h = obj(summary.head_to_head);
  const hit = obj(summary.hit_rate);
  const decided = ['mason', 'nico', 'tie'].reduce((s, k) => s + (fin(h2h[k]) ? h2h[k] : 0), 0);
  const overlap = capList(arr(d.overlap).filter(x => x && typeof x === 'object'), OVERLAP_MAX);
  const caveats = capList(strs(d.caveats), CAVEAT_MAX);
  const reconShown = capList(recon, LIST_MAX);
  const errs = capList(arr(api.errors), LIST_MAX);
  const h2hNum = v => (fin(v) ? num(v) : '—');
  const paperInfo = obj(d.paper);

  return (
    <div className="mui-pvr__body">
      <div className="mui-pvr__bar">
        <label className="mui-pvr__acct">
          <span className="mui-sr">Real account</span>
          <select aria-label="Real account" value={acct} onChange={e => setAccount(e.target.value)}>
            {realKeys.map(k => (
              <option key={k} value={k}>
                {k === MINE ? 'Mine' : acctLabel(k, scopes)}
              </option>
            ))}
          </select>
        </label>
        {drifting.length ? (
          <span className="mui-badge mui-badge--warn" data-testid="pvr-drift" title="Reconstructed value vs Robinhood positions value">
            <span aria-hidden="true">⚠</span>
            {worst ? ` drift ${pct(Math.abs(worst.drift_pct), { digits: 2, signed: false })} (${String(worst.account)})` : ' reconcile failed'}
            {drifting.length > 1 ? ` +${drifting.length - 1}` : ''}
          </span>
        ) : null}
        <span className="mui-pvr__spacer" />
        <Pills label="Range" options={RANGES} value={range} onChange={setRange} />
      </div>

      <div className="mui-pvr__heroes" data-testid="pvr-hero">
        <Hero label="Mason paper" value={paperCum} cls="paper" unavailable="paper unavailable" />
        <span className="mui-pvr__vs" aria-hidden="true">
          vs
        </span>
        <Hero label={labels.real} value={realCum} cls="real" unavailable="no real data" />
        <span className="mui-pvr__gap">
          {fin(gap)
            ? Math.abs(gap) < 0.005
              ? 'level'
              : `${gap > 0 ? 'Mason' : 'You'} ahead by ${num(Math.abs(gap), 2)} pts`
            : ''}
        </span>
      </div>

      <div className="mui-pvr__main">
        <div className="mui-pvr__chartcol">
          {rows.length ? (
            <PvrChart rows={chartRows} labels={labels} />
          ) : (
            <div className="mui-state mui-state--empty">No sessions in this range yet.</div>
          )}
          <div className="mui-pvr__scope" data-testid="pvr-scope">
            <span>
              {acct === MINE ? 'Mine' : realName} = {acct === MINE ? (inScope.length ? inScope.join(', ') : 'in-scope accounts') : acctLabel(acct, scopes)}
            </span>
            {excluded.length ? <span> · excluded from Mine: {excluded.map(a => acctLabel(a, scopes)).join(', ')}</span> : null}
            <span className="mui-n">
              {' '}
              · {String(d.range ?? '')} {date(d.start)} – {date(d.end)} · {num(rows.length)} sessions
              {chartRows.length < rows.length ? ` (chart shows ${num(chartRows.length)}, evenly spaced)` : ''}
              {fin(paperInfo.n_days) ? ` · paper ${nl(paperInfo.n_days)} days` : ''}
            </span>
          </div>
        </div>

        <div className="mui-pvr__side">
          <SpotlightCard className="mui-pvr__h2h" spotlightColor="rgba(74, 158, 255, 0.16)">
            <div data-testid="pvr-h2h">
              <div className="mui-pvr__h2htitle">
                Head-to-head <span className="mui-n">matched windows · {nl(decided)} decided</span>
              </div>
              <div className="mui-pvr__h2hrow">
                <span className="mui-pvr__h2hcell">
                  <b>Mason</b> <span className="mui-pvr__h2hnum">{h2hNum(h2h.mason)}</span>
                </span>
                <span className="mui-pvr__h2hcell">
                  <b>Nico</b> <span className="mui-pvr__h2hnum">{h2hNum(h2h.nico)}</span>
                </span>
                <span className="mui-pvr__h2hcell">
                  <b>Tie</b> <span className="mui-pvr__h2hnum">{h2hNum(h2h.tie)}</span>
                </span>
                <span className="mui-n">{fin(h2h.pending) ? `${num(h2h.pending)} pending` : ''}</span>
              </div>
              <div className="mui-pvr__hit">
                Hit rate: Mason {fin(hit.paper) ? pct(hit.paper * 100, { digits: 0, signed: false }) : '—'} · Nico{' '}
                {fin(hit.real) ? pct(hit.real * 100, { digits: 0, signed: false }) : '—'} <span className="mui-n">{nl(hit.n)}</span>
              </div>
            </div>
          </SpotlightCard>
          <div className="mui-pvr__ovtitle">Overlap · same ticker, both sides</div>
          {overlap.total ? (
            <ul className="mui-pvr__ovlist" data-testid="pvr-overlap">
              {overlap.rows.map((o, i) => (
                <OverlapRow key={`${o.rec_id ?? ''}-${o.ticker ?? ''}-${i}`} o={o} scopes={scopes} />
              ))}
            </ul>
          ) : (
            <div className="mui-state mui-state--empty" data-testid="pvr-overlap">
              No ticker traded by both sides in this range.
            </div>
          )}
          {overlap.capped ? <div className="mui-n">{capNote(overlap.rows.length, overlap.total)}</div> : null}
          {recon.length ? (
            <div className="mui-pvr__recon" data-testid="pvr-recon">
              {reconShown.capped ? <div className="mui-n">{capNote(reconShown.rows.length, reconShown.total)}</div> : null}
              {reconShown.rows.map((x, i) => (
                <div key={i} className={x.ok === false ? 'is-bad' : ''}>
                  {x.ok === false ? '⚠ ' : '✓ '}
                  {String(x.account ?? '—')}:{' '}
                  {fin(x.drift_pct) ? `drift ${pct(x.drift_pct, { digits: 2, signed: false })}` : 'reconcile not possible'}
                  {x.ok === false ? ' (out of tolerance)' : ''}
                </div>
              ))}
            </div>
          ) : null}
          {errs.total ? (
            <ul className="mui-pvr__errors" data-testid="pvr-errors">
              {errs.rows.map((e, i) => (
                <li key={i}>{String(e)}</li>
              ))}
              {errs.capped ? <li>{capNote(errs.rows.length, errs.total)}</li> : null}
            </ul>
          ) : null}
        </div>
      </div>

      <div className="mui-pvr__caveats" data-testid="pvr-caveats" tabIndex={0} aria-label="Caveats">
        <b>Caveats ({num(caveats.total)})</b>
        {caveats.total ? (
          <ul>
            {caveats.rows.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        ) : null}
        {caveats.capped ? <span className="mui-n"> · {capNote(caveats.rows.length, caveats.total)}</span> : null}
        {caveats.total ? null : <span className="mui-muted"> none returned by the route</span>}
      </div>
    </div>
  );
}

export default function RhPvr({ config, saveConfig }) {
  // Per-instance state, seeded from the saved config; every change is persisted via saveConfig.
  const [range, setRangeState] = useState(RANGES.some(r => r.value === config.range) ? config.range : '1M');
  const [account, setAccountState] = useState(typeof config.account === 'string' && config.account ? config.account : MINE);
  const api = useRhApi('/api/rh/pvr', { range });
  const scopes = useAccountScopes();
  const save = next => saveConfig && saveConfig({ ...config, range, account, ...next });
  const setAccount = a => {
    setAccountState(a);
    save({ account: a });
  };
  const setRange = r => {
    setRangeState(r);
    save({ range: r });
  };
  return (
    <div className="mui-pvr">
      <RhState api={api} empty={d => !d || typeof d !== 'object'} emptyText="No Paper-vs-Real data yet.">
        {d => <Body d={d} api={api} account={account} setAccount={setAccount} scopes={scopes} range={range} setRange={setRange} />}
      </RhState>
    </div>
  );
}
