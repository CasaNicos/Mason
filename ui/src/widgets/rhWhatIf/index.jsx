// rhWhatIf — What-If (S3 §3 F5.3; contracts §6 GET /api/rh/whatif?account=&horizon=; S2-T4).
// One question: "did my exits cost me money?"
//  - Sign convention (Q-T3-2, finalization T-WI): a NEGATIVE dollar value means selling COST money
//    (the shares were worth more later); positive = the exit SAVED money — the same as Behavior
//    Coach (cost = negative everywhere). Always said in words; an exact 0 / -0 is "no difference".
//  - Horizon toggle 30 / 90 / 180 / today → ?horizon=. Unmatured exits are excluded from totals
//    and the count is shown ("5 pending").
//  - React Bits <Counter> total. K6: §6 gives ONE net total per horizon. Cost and saved are shown
//    separately only when they can be derived exactly: the lists are for this horizon, every
//    matured exit is listed (costly + best = counts.matured) and their sum equals the net total.
//    Otherwise the net is shown with honest words (open question: route-level cost/saved fields).
//  - Most costly and Best exits always side by side (stacked only below ~300 px wide).
//  - Option rows read "expiry"; crypto and other exclusions are listed with their n.
import { useState } from 'react';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { DASH, arrow, date, dirClass, isNum, money, num, pct } from '../../lib/fmt.js';
import { Pills, RollNumber, acctLabel, capList, capNote, fin, nl, useAccountScopes } from '../rhPvr/f5shared.jsx';
import '../rhPvr/f5shared.css';
import './whatif.css';

const HZ = [
  { value: '30', label: '30d', title: 'If you had held 30 days longer' },
  { value: '90', label: '90d', title: 'If you had held 90 days longer' },
  { value: '180', label: '180d', title: 'If you had held 180 days longer' },
  { value: 'today', label: 'Today', title: 'If you had held until today' }
];
const HZ_WORD = { 30: '30 days', 90: '90 days', 180: '180 days', today: 'today', expiry: 'expiry' };

const obj = v => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const rowsOf = v => (Array.isArray(v) ? v.filter(r => r && typeof r === 'object') : []);
const LIST_MAX = 50; // rows rendered per side (Debugger 10k fixtures); "50 of N shown" when capped
/** Sum rounded to cents, or null when it is not finite / implausibly large (1e308 + 1e308). */
const sum = xs => {
  const v = Math.round(xs.reduce((s, x) => s + x, 0) * 100) / 100;
  return fin(v) ? v : null;
};

/** What-If words (Q-T3-2): negative = cost, positive = saved. */
function wiWords(v) {
  if (isNum(v) && !fin(v)) return 'value out of range';
  if (!isNum(v)) return 'pending';
  if (Math.abs(v) < 0.005) return 'no difference';
  return v < 0 ? `cost you ${money(-v)}` : `saved you ${money(v)}`;
}
/** Colour class for a dollar value: cost (negative) / saved (positive) / none for 0, -0 and junk. */
const wiClass = v => (fin(v) && Math.abs(v) >= 0.005 ? (v < 0 ? 'is-cost' : 'is-saved') : '');

function Row({ r, scopes }) {
  const option = r.kind === 'option';
  const matured = r.matured !== false && fin(r.dollars);
  const ret = fin(r.ret) ? r.ret * 100 : null;
  return (
    <li className={`mui-wi__row${matured ? '' : ' is-pending'}`}>
      <div className="mui-wi__l1">
        <b className="mui-wi__tk">{option && r.contract ? String(r.contract) : String(r.ticker ?? DASH)}</b>
        <span className="mui-wi__tag">{option ? 'option · expiry' : String(r.horizon_label ?? r.horizon ?? '')}</span>
        <span className={`mui-wi__amt ${matured ? wiClass(r.dollars) : 'mui-muted'}`}>{matured ? wiWords(r.dollars) : 'pending'}</span>
      </div>
      <div className="mui-wi__l2 mui-n">
        <span>{acctLabel(r.account ?? DASH, scopes)}</span>
        <span>
          sold {date(r.sold_at)} @ {money(r.sold_px)} → {fin(r.px) ? money(r.px) : DASH}
          {r.exit_date ? ` (${date(r.exit_date)})` : ''}
        </span>
        <span className={dirClass(ret)}>
          {arrow(ret)} {pct(ret, { digits: 1 })}
        </span>
        {fin(r.dividends_missed) && r.dividends_missed > 0 ? <span>incl. {money(r.dividends_missed)} dividends</span> : null}
      </div>
    </li>
  );
}

function Side({ testid, title, rows: all, empty, scopes }) {
  const { rows, total, capped } = capList(all, LIST_MAX);
  return (
    <section className="mui-wi__side" data-testid={testid} aria-label={title}>
      <h4 className="mui-wi__sidet">
        {title} <span className="mui-n">({num(total)})</span>
      </h4>
      {rows.length ? (
        <ul className="mui-wi__list">
          {rows.map((r, i) => (
            <Row key={`${r.id ?? ''}-${i}`} r={r} scopes={scopes} />
          ))}
        </ul>
      ) : (
        <div className="mui-state mui-state--empty">{empty}</div>
      )}
      {capped ? <div className="mui-n mui-wi__cap">{capNote(rows.length, total)}</div> : null}
    </section>
  );
}

function Total({ d, h }) {
  const net = obj(d.total_dollars)[h];
  const cnt = obj(obj(d.counts)[h]);
  const matured = fin(cnt.matured) ? cnt.matured : null;
  const pending = fin(cnt.pending) ? cnt.pending : null;
  const costly = rowsOf(d.costly);
  const best = rowsOf(d.best);
  const listed = [...costly, ...best].filter(r => r.matured !== false && fin(r.dollars));
  const cost = sum(listed.filter(r => r.dollars < 0).map(r => -r.dollars)); // Q-T3-2: cost is negative
  const saved = sum(listed.filter(r => r.dollars > 0).map(r => r.dollars));
  const derivable =
    fin(net) &&
    fin(cost) &&
    fin(saved) &&
    d.horizon === h &&
    isNum(matured) &&
    matured > 0 &&
    listed.length === matured &&
    listed.length === costly.length + best.length &&
    Math.abs(saved - cost - net) < 0.015;
  const pendTxt = isNum(pending) && pending > 0 ? ` · ${num(pending)} pending (left out)` : '';

  if (isNum(net) && !fin(net)) {
    return (
      <div className="mui-wi__total is-pending" data-testid="wi-total">
        <div className="mui-wi__big mui-muted">Total out of range</div>
        <div className="mui-n">The route sent a total too large to be real; not shown. {nl(matured)} matured exits{pendTxt}</div>
      </div>
    );
  }
  if (!isNum(net)) {
    return (
      <div className="mui-wi__total is-pending" data-testid="wi-total">
        <div className="mui-wi__big mui-muted">Pending</div>
        <div className="mui-n">
          No total at {HZ_WORD[h] || h} yet: {matured == null ? DASH : num(matured)} matured, {pending == null ? DASH : num(pending)} pending. Exits count once they reach the horizon.
        </div>
      </div>
    );
  }
  if (derivable) {
    return (
      <div className="mui-wi__total" data-testid="wi-total">
        <span className="mui-sr">{`Your exits cost you ${money(cost)} / saved you ${money(saved)}`}</span>
        <div className="mui-wi__big" aria-hidden="true">
          <span className="mui-wi__w">Your exits cost you</span>
          <RollNumber className="is-cost" value={cost} before="$" text={money(cost)} fontSize={22} />
          <span className="mui-wi__w">/ saved you</span>
          <RollNumber className="is-saved" value={saved} before="$" text={money(saved)} fontSize={22} />
        </div>
        <div className="mui-n">
          net: {wiWords(net)} · {nl(matured)} exits{pendTxt}
        </div>
      </div>
    );
  }
  return (
    <div className="mui-wi__total" data-testid="wi-total">
      <span className="mui-sr">{`Net: your exits ${wiWords(net)}`}</span>
      <div className="mui-wi__big" aria-hidden="true">
        <span className="mui-wi__w">Net: your exits {Math.abs(net) < 0.005 ? 'made no difference' : net < 0 ? 'cost you' : 'saved you'}</span>
        {Math.abs(net) >= 0.005 ? (
          <RollNumber className={wiClass(net)} value={net} before="$" text={money(Math.abs(net))} fontSize={22} />
        ) : null}
      </div>
      <div className="mui-n">
        {nl(matured)} matured exits{pendTxt} · the cost / saved split isn’t in the data for this horizon
      </div>
    </div>
  );
}

function Body({ d, h, scopes }) {
  const byAcct = obj(d.by_account);
  const accts = Object.keys(byAcct).sort();
  const ex = obj(d.excluded);
  const crypto = obj(ex.crypto);
  const exAccts = Array.isArray(ex.accounts) ? ex.accounts.filter(a => typeof a === 'string') : [];
  const listH = typeof d.horizon === 'string' ? d.horizon : null;
  return (
    <div className="mui-wi__body">
      <Total d={d} h={h} />
      {accts.length ? (
        <div className="mui-wi__accts" data-testid="wi-accounts">
          {accts.map(a => {
            const v = obj(byAcct[a])[h];
            return (
              <span key={a} className={`mui-wi__acct ${fin(v) ? wiClass(v) : 'mui-muted'}`}>
                {acctLabel(a, scopes)} {wiWords(v)}
              </span>
            );
          })}
        </div>
      ) : null}
      <div className="mui-wi__lh mui-n" data-testid="wi-lists-horizon">
        Exits at horizon: {HZ_WORD[listH] || listH || DASH}
        {listH && listH !== h ? ` (the route returned lists for ${HZ_WORD[listH] || listH}, not ${HZ_WORD[h] || h})` : ''}
      </div>
      <div className="mui-wi__pair">
        <Side testid="wi-costly" title="Most costly exits" rows={rowsOf(d.costly)} empty="No costly exits at this horizon." scopes={scopes} />
        <Side testid="wi-best" title="Best exits" rows={rowsOf(d.best)} empty="No exits that saved money at this horizon." scopes={scopes} />
      </div>
      <div className="mui-wi__foot">
        {typeof d.note === 'string' ? (
          <div className="mui-n" data-testid="wi-note">
            {d.note}
          </div>
        ) : null}
        <div className="mui-n" data-testid="wi-excluded">
          Excluded: {exAccts.length ? exAccts.map(a => acctLabel(a, scopes)).join(', ') : 'no accounts'} · crypto ({nl(crypto.n)})
          {typeof crypto.note === 'string' ? ` — ${crypto.note}` : ''} · dividend reinvestment ({nl(ex.drip)}) · multi-leg ({nl(ex.multileg)})
        </div>
      </div>
    </div>
  );
}

export default function RhWhatIf({ config, saveConfig }) {
  const [h, setH] = useState(HZ.some(x => x.value === config.horizon) ? config.horizon : 'today');
  const api = useRhApi('/api/rh/whatif', { horizon: h });
  const scopes = useAccountScopes();
  const pick = v => {
    setH(v);
    if (saveConfig) saveConfig({ ...config, horizon: v });
  };
  return (
    <div className="mui-wi">
      <div className="mui-wi__top">
        <span className="mui-wi__q">If you had held…</span>
        <Pills label="Horizon" options={HZ} value={h} onChange={pick} />
      </div>
      <RhState api={api} empty={d => !d || typeof d !== 'object' || Array.isArray(d)} emptyText="No exits to replay yet.">
        {d => <Body d={d} h={h} scopes={scopes} />}
      </RhState>
    </div>
  );
}
