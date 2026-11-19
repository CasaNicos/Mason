// Learning Loop: Mason's own trading loop at a glance, from GET /api/agent_loop/status (60 s poll).
// Top to bottom: Opus budget / trades / sleeve, open positions, calibration, closed P&L, horizon mix,
// then one line each for Nico's theses and the lean prompt format.
// Every field can be null or missing: numbers go through lib/fmt (bad value → "—"), lists through
// arr(), objects through obj(). A failed fetch or {ok:false} shows "Loop status unavailable".
// React Bits: SpotlightCard (section cards), CountUp (whole-number counts). The table, the bars and
// the stat tiles are plain markup: React Bits has no table / progress-bar component.
// A position row is a button-like row: click / Enter charts its ticker on the main chart.
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { emitChartSymbol } from '../../lib/bridge.js';
import { DASH, arrow, date, dateTime, dirClass, isNum, money, num, pct } from '../../lib/fmt.js';
import { Loading } from '../../lib/states.jsx';
import { useLoopStatus } from './useLoopStatus.js';
import './learningLoop.css';

const SPOT = 'rgba(74, 158, 255, 0.14)';
const WARN_AT = 0.8;
const MAX_CLOSED = 5;
const MAX_COUNT = 1e7;
const HORIZONS = ['swing', 'intraday', 'long'];

export const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
export const arr = v => (Array.isArray(v) ? v : []);
export const str = v => (typeof v === 'string' ? v : isNum(v) ? String(v) : '');
const txt = v => str(v) || DASH;
const rows = v => arr(v).filter(r => r && typeof r === 'object' && !Array.isArray(r));
const clamp01 = v => (isNum(v) ? Math.max(0, Math.min(1, v)) : 0);
const plainPct = (v, digits = 0) => pct(v, { digits, signed: false });
const px = v => (isNum(v) ? num(v, 2) : DASH);
const count = v => (isNum(v) && v >= 0 && v <= MAX_COUNT ? Math.round(v) : null);

/** A rate that may arrive as a share (0.58) or as percent (58) → percent, or null. */
export function ratePct(v) {
  if (!isNum(v) || v < 0) return null;
  return v <= 1 ? v * 100 : v <= 100 ? v : null;
}
/** wins / n in percent when both are usable; else the given rate; else null. */
export function hitPct(wins, n, rate) {
  if (isNum(wins) && isNum(n) && n > 0 && wins >= 0 && wins <= n) return (wins / n) * 100;
  return ratePct(rate);
}
/** "anchor_1300" → "1pm call"; "anchor_0930" → "9:30am call"; anything else → words. */
export function leanKind(k) {
  const s = str(k);
  const m = s.match(/^anchor_(\d{2})(\d{2})$/);
  if (!m || +m[1] > 23 || +m[2] > 59) return s.replace(/_/g, ' ').trim();
  const h = +m[1];
  return `${h % 12 || 12}${m[2] === '00' ? '' : `:${m[2]}`}${h < 12 ? 'am' : 'pm'} call`;
}
/** Options show the contract symbol; stocks the ticker. */
export const posName = p => (str(p.vehicle).toLowerCase() === 'stock' ? str(p.ticker) || str(p.symbol) : str(p.symbol) || str(p.ticker)) || DASH;

function Count({ value }) {
  const n = count(value);
  return n == null ? DASH : <CountUp to={n} duration={0.8} />;
}

function Bar({ value, cap, label, warn = false }) {
  const share = isNum(value) && isNum(cap) && cap > 0 ? clamp01(value / cap) : 0;
  return (
    <span
      className={`mui-ll__bar${warn ? ' is-warn' : ''}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(share * 100)}
    >
      <i style={{ width: `${(share * 100).toFixed(1)}%` }} />
    </span>
  );
}

function Pnl({ usd, pctV }) {
  const dir = isNum(usd) ? usd : pctV;
  return (
    <span className={dirClass(dir)}>
      {arrow(dir)} {money(usd, { signed: true })}
      {isNum(pctV) ? <span className="mui-ll__sub"> {pct(pctV, { digits: 1 })}</span> : null}
    </span>
  );
}

function Header({ d }) {
  const b = obj(d.budget);
  const a = obj(d.account);
  const used = isNum(b.day_usd) && isNum(b.day_cap_usd) && b.day_cap_usd > 0 ? b.day_usd / b.day_cap_usd : null;
  const warn = used != null && used >= WARN_AT;
  const queued = arr(d.queued).length;
  const errors = arr(d.errors).map(e => (typeof e === 'string' ? e : JSON.stringify(e)));
  return (
    <div className="mui-ll__head">
      <div className={`mui-ll__stat${warn ? ' is-warn' : ''}`} data-stat="spend">
        <span className="mui-ll__k">Opus spend today</span>
        <span className="mui-ll__v">
          {money(b.day_usd)} <span className="mui-ll__of">/ {money(b.day_cap_usd)}</span>
          {warn ? <span className="mui-ll__flag"> ⚠ {used >= 1 ? 'at cap' : 'near cap'}</span> : null}
        </span>
        <Bar value={b.day_usd} cap={b.day_cap_usd} warn={warn} label="Opus spend today, share of the daily cap" />
        <span className="mui-ll__sub">
          month {money(b.month_usd)} / {money(b.month_cap_usd)}
        </span>
      </div>
      <div className="mui-ll__stat" data-stat="trades">
        <span className="mui-ll__k">Trades today</span>
        <span className="mui-ll__v">
          <Count value={a.trades_today} /> <span className="mui-ll__of">/ {isNum(a.max_trades_per_day) ? num(a.max_trades_per_day) : DASH}</span>
        </span>
        <Bar value={a.trades_today} cap={a.max_trades_per_day} label="Trades today, share of the daily limit" />
        {queued ? <span className="mui-ll__sub">{queued} queued</span> : null}
      </div>
      <div className="mui-ll__stat" data-stat="sleeve">
        <span className="mui-ll__k">Sleeve in use</span>
        <span className="mui-ll__v">
          {plainPct(a.sleeve_pct, 1)} <span className="mui-ll__of">of {plainPct(a.sleeve_cap_pct)}</span>
        </span>
        <Bar value={a.sleeve_pct} cap={a.sleeve_cap_pct} label="Sleeve in use, share of the sleeve cap" />
        <span className="mui-ll__sub">{money(a.sleeve_usd)} deployed</span>
      </div>
      <div className="mui-ll__stat" data-stat="equity">
        <span className="mui-ll__k">Paper equity</span>
        <span className="mui-ll__v">{money(a.equity)}</span>
        <span className={`mui-ll__sub ${dirClass(a.day_pnl_pct)}`}>
          today {arrow(a.day_pnl_pct)} {pct(a.day_pnl_pct, { digits: 2 })}
        </span>
        <span className="mui-ll__sub">cash {money(a.cash)}</span>
      </div>
      {errors.length ? (
        <span className="mui-badge mui-badge--warn mui-ll__errs" title={errors.join(' · ')} role="status">
          <span aria-hidden="true">⚠</span> {errors.length} data {errors.length === 1 ? 'issue' : 'issues'}
        </span>
      ) : null}
    </div>
  );
}

function OpenRow({ p }) {
  const tk = str(p.ticker) || str(p.symbol);
  const name = posName(p);
  const vehicle = str(p.vehicle).toLowerCase();
  const chart = () => tk && emitChartSymbol('main', tk);
  return (
    <tr
      className="mui-ll__row"
      tabIndex={0}
      data-rec={str(p.rec_id)}
      title={tk ? `Chart ${tk}` : undefined}
      onClick={chart}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          chart();
        }
      }}
    >
      <th scope="row" data-cell="name">
        {name}
        {vehicle && vehicle !== 'stock' ? <span className="mui-ll__tag">{vehicle}</span> : null}
      </th>
      <td data-cell="tier">{txt(p.tier)}</td>
      <td data-cell="horizon">{txt(p.horizon)}</td>
      <td data-cell="qty" className="is-num">{isNum(p.qty) ? num(p.qty, Number.isInteger(p.qty) ? 0 : 2) : DASH}</td>
      <td data-cell="px" className="is-num">
        {px(p.entry_px)} <span aria-hidden="true">→</span><span className="mui-sr"> to </span> {px(p.last)}
      </td>
      <td data-cell="pnl" className="is-num">
        <Pnl usd={p.pnl_usd} pctV={p.pnl_pct} />
      </td>
      <td data-cell="exits" className="is-num">
        {px(p.stop)} / {px(p.target)}
      </td>
      <td data-cell="tstop">{p.time_stop_date ? date(p.time_stop_date) : DASH}</td>
    </tr>
  );
}

function OpenPositions({ list }) {
  return (
    <SpotlightCard className="mui-ll__card mui-ll__card--open" spotlightColor={SPOT}>
      <h3 className="mui-ll__h">
        Open positions <span className="mui-n">{list.length}</span>
      </h3>
      {list.length ? (
        <div className="mui-ll__scroll">
          <table className="mui-ll__table">
            <thead>
              <tr>
                <th scope="col">Ticker</th>
                <th scope="col">Tier</th>
                <th scope="col">Horizon</th>
                <th scope="col" className="is-num">Qty</th>
                <th scope="col" className="is-num">Entry → last</th>
                <th scope="col" className="is-num">P&amp;L</th>
                <th scope="col" className="is-num">Stop / target</th>
                <th scope="col">Time stop</th>
              </tr>
            </thead>
            <tbody>
              {list.map((p, i) => (
                <OpenRow key={str(p.rec_id) || `${posName(p)}|${i}`} p={p} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mui-ll__empty">No open Mason positions</p>
      )}
    </SpotlightCard>
  );
}

function Calibration({ c }) {
  const base = hitPct(c.wins, c.n, c.p_base);
  const shown = ratePct(c.p_base) ?? base;
  const own = count(c.own_graded_n);
  const need = count(c.normal_requires_own_n);
  const segs = rows(c.segments);
  return (
    <SpotlightCard className="mui-ll__card" spotlightColor={SPOT}>
      <h3 className="mui-ll__h">Calibration</h3>
      <p className="mui-ll__line" data-line="base">
        Base hit rate <b>{shown == null ? DASH : plainPct(shown)}</b>{' '}
        <span className="mui-n">
          ({isNum(c.wins) ? num(c.wins) : DASH}/{isNum(c.n) ? num(c.n) : DASH})
        </span>
      </p>
      <p className="mui-ll__line" data-line="own">
        Own graded trades: <b>{own == null ? DASH : own}</b> of {need == null ? DASH : need} needed for the normal tier
      </p>
      <Bar value={own} cap={need} label="Own graded trades, share of what the normal tier needs" />
      {segs.length ? (
        <table className="mui-ll__table mui-ll__table--mini">
          <thead>
            <tr>
              <th scope="col">Segment</th>
              <th scope="col" className="is-num">Trades</th>
              <th scope="col" className="is-num">Hit rate</th>
            </tr>
          </thead>
          <tbody>
            {segs.map((s, i) => {
              const r = ratePct(s.p_cal) ?? hitPct(s.wins, s.n);
              return (
                <tr key={`${str(s.segment)}|${i}`} data-seg={str(s.segment)}>
                  <th scope="row">{str(s.segment).split('|').join(' · ') || DASH}</th>
                  <td className="is-num">{isNum(s.n) ? num(s.n) : DASH}</td>
                  <td className="is-num">
                    {r == null ? DASH : plainPct(r)}
                    {isNum(s.wins) && isNum(s.n) ? <span className="mui-n"> ({num(s.wins)}/{num(s.n)})</span> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
    </SpotlightCard>
  );
}

/** One breakdown bucket: a bare number is its P&L; an object may carry n / wins / a P&L field. */
function bucket(v) {
  if (isNum(v)) return { usd: v, n: null, wins: null, rate: null };
  const o = obj(v);
  const usd = [o.total_usd, o.pnl_usd, o.usd].find(isNum);
  const n = [o.n_closed, o.n].find(isNum);
  return { usd: usd ?? null, n: n ?? null, wins: isNum(o.wins) ? o.wins : null, rate: o.win_rate };
}
function Breakdown({ label, by }) {
  const keys = Object.keys(obj(by)).sort();
  if (!keys.length) return null;
  return (
    <p className="mui-ll__line mui-ll__brk" data-brk={label}>
      <span className="mui-ll__k">By {label}</span>
      {keys.map(k => {
        const b = bucket(by[k]);
        const r = hitPct(b.wins, b.n, b.rate);
        return (
          <span key={k} className="mui-ll__chip">
            <b>{k}</b> <Pnl usd={b.usd} />
            {b.n != null ? <span className="mui-n"> n={num(b.n)}</span> : null}
            {r != null ? <span className="mui-n"> · {plainPct(r)} wins</span> : null}
          </span>
        );
      })}
    </p>
  );
}

function ClosedPnl({ p, closed }) {
  const n = count(p.n_closed);
  const win = hitPct(p.wins, p.n_closed, p.win_rate);
  const last = closed.slice(0, MAX_CLOSED);
  return (
    <SpotlightCard className="mui-ll__card" spotlightColor={SPOT}>
      <h3 className="mui-ll__h">Closed P&amp;L</h3>
      <p className="mui-ll__line" data-line="closed">
        Total <b><Pnl usd={p.total_usd} /></b> · win rate <b>{win == null ? DASH : plainPct(win)}</b>{' '}
        <span className="mui-n">
          ({isNum(p.wins) ? num(p.wins) : DASH}/{n == null ? DASH : n} closed)
        </span>
      </p>
      <Breakdown label="tier" by={p.by_tier} />
      <Breakdown label="horizon" by={p.by_horizon} />
      <Breakdown label="vehicle" by={p.by_vehicle} />
      {last.length ? (
        <ul className="mui-ll__closed" aria-label="Last closed trades">
          {last.map((t, i) => (
            <li key={str(t.rec_id) || `${posName(t)}|${i}`} data-rec={str(t.rec_id)}>
              <b className="mui-ll__tk">{posName(t)}</b>
              <Pnl usd={t.pnl_usd} pctV={t.pnl_pct} />
              {isNum(t.r_multiple) ? <span className="mui-n">{num(t.r_multiple, 1)}R</span> : null}
              <span className="mui-ll__why">{str(t.exit_reason).replace(/_/g, ' ') || DASH}</span>
              {t.exit_ts && date(t.exit_ts) !== DASH ? <span className="mui-n">{date(t.exit_ts)}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mui-ll__empty">No closed trades yet</p>
      )}
    </SpotlightCard>
  );
}

function HorizonMix({ h }) {
  const target = obj(h.target);
  const actual = obj(h.actual_pct);
  const keys = [...HORIZONS, ...Object.keys({ ...target, ...actual }).filter(k => !HORIZONS.includes(k)).sort()];
  return (
    <SpotlightCard className="mui-ll__card" spotlightColor={SPOT}>
      <h3 className="mui-ll__h">
        Horizon mix <span className="mui-n">actual vs target</span>
      </h3>
      <div className="mui-ll__mix">
        {keys.map(k => (
          <div key={k} className="mui-ll__mixrow" data-horizon={k}>
            <span className="mui-ll__k">{k}</span>
            <span
              className="mui-ll__bar mui-ll__bar--mix"
              role="img"
              aria-label={`${k}: actual ${plainPct(actual[k])}, target ${plainPct(target[k])}`}
            >
              <i style={{ width: `${(clamp01(actual[k] / 100) * 100).toFixed(1)}%` }} />
              {isNum(target[k]) ? <em style={{ left: `${(clamp01(target[k] / 100) * 100).toFixed(1)}%` }} /> : null}
            </span>
            <span className="mui-ll__mixtxt">
              <b>{plainPct(actual[k])}</b> <span className="mui-n">target {plainPct(target[k])}</span>
            </span>
          </div>
        ))}
      </div>
    </SpotlightCard>
  );
}

function FootLines({ nico, lean }) {
  const win = hitPct(nico.wins, nico.n_closed, nico.win_rate);
  const kinds = arr(lean.kinds).map(leanKind).filter(Boolean);
  const cnt = v => (count(v) == null ? DASH : count(v));
  return (
    <div className="mui-ll__foot">
      <p className="mui-ll__line" data-line="nico">
        <span className="mui-ll__k">Nico’s theses</span> {cnt(nico.n_open)} open · {cnt(nico.n_closed)} closed · win rate{' '}
        {win == null ? DASH : plainPct(win)} · P&amp;L <Pnl usd={nico.pnl_usd} />
      </p>
      <p className="mui-ll__line" data-line="lean">
        <span className="mui-ll__k">Lean format</span> {kinds.length ? `Lean on: ${kinds.join(', ')}` : 'Lean off'} · lean avg{' '}
        {money(lean.avg_cost_lean)}
        {isNum(lean.n_lean) ? <span className="mui-n"> (n={num(lean.n_lean)})</span> : null} vs full avg {money(lean.avg_cost_full)}
        {isNum(lean.n_full) ? <span className="mui-n"> (n={num(lean.n_full)})</span> : null}
      </p>
    </div>
  );
}

export function LoopView({ data, failed = false }) {
  const d = obj(data);
  return (
    <div className="mui-ll">
      <Header d={d} />
      <div className="mui-ll__body">
        <OpenPositions list={rows(d.open)} />
        <div className="mui-ll__grid">
          <Calibration c={obj(d.calibration)} />
          <ClosedPnl p={obj(d.pnl)} closed={rows(d.closed_recent)} />
          <HorizonMix h={obj(d.horizon_mix)} />
        </div>
        <FootLines nico={obj(d.nico)} lean={obj(d.lean)} />
        <p className="mui-ll__asof">
          {failed ? <span className="mui-ll__flag">⚠ Loop status unavailable, showing the last update · </span> : null}
          as of {d.as_of && dateTime(d.as_of) !== DASH ? dateTime(d.as_of) : DASH}
        </p>
      </div>
    </div>
  );
}

export default function LearningLoop() {
  const s = useLoopStatus();
  if (s.status === 'loading') return <Loading rows={5} />;
  if (s.status === 'error') {
    return (
      <div className="mui-state mui-state--empty mui-ll__off" role="status">
        Loop status unavailable
      </div>
    );
  }
  return <LoopView data={s.data} failed={s.failed} />;
}
