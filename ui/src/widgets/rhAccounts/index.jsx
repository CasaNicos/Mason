// rhAccounts — "Where is my money, and is any of it sitting idle?" (S3 §3 F4.1, contracts §6
// /api/rh/accounts). One row per account: equity on the React Bits Counter, day P&L with sign +
// ▲/▼, a cash-% ring (SVG, number as text), idle-cash line, dividends received vs 12-month
// projection; the row expands to the top 3 positions. Scope ("family") comes from the data.
import { memo, useMemo, useState, useId } from 'react';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { money, pct, arrow, dirClass, date, isNum, DASH } from '../../lib/fmt.js';
import MoneyCounter from './MoneyCounter.jsx';
import { isFamily, isMine } from './scope.js';
import { fin, finSum, objs, moreText, count, MONEY_MAX, PCT_MAX, DAYS_MAX, COUNT_MAX } from './guard.js';
import './rhAccounts.css';

export const ROW_CAP = 12; // Robinhood allows a handful of accounts; past 12 the rest are counted (each row rolls Counters)
const sum = (rows, k) => finSum(rows.map(r => r[k]));

/** Boundary clean: finite, plausible numbers only; top positions cut to 3 here. */
export function cleanAccount(r) {
  const d = r.dividends && typeof r.dividends === 'object' ? r.dividends : null;
  return {
    ...r,
    account: r.account == null ? '—' : String(r.account),
    equity: fin(r.equity, MONEY_MAX),
    day_pl: fin(r.day_pl, MONEY_MAX),
    cash: fin(r.cash, MONEY_MAX),
    cash_pct: fin(r.cash_pct, PCT_MAX),
    idle_days: fin(r.idle_days, DAYS_MAX),
    n_positions: fin(r.n_positions, COUNT_MAX),
    // null stays null (a family row has no top list, A1-UI); anything else is cut to 3 as before
    top: r.top == null ? null : objs(r.top).slice(0, 3).map(p => ({ ...p, pct: fin(p.pct, PCT_MAX) })),
    dividends: d ? { ...d, received: fin(d.received), projected_12m: fin(d.projected_12m) } : null
  };
}

function Ring({ value }) {
  const R = 11;
  const C = 2 * Math.PI * R;
  const v = isNum(value) ? Math.max(0, Math.min(100, value)) : 0;
  return (
    <svg className="rha-ring" viewBox="0 0 30 30" width="30" height="30" aria-hidden="true">
      <circle className="rha-ring__track" cx="15" cy="15" r={R} />
      {isNum(value) && v > 0 ? (
        <circle
          className="rha-ring__arc"
          cx="15"
          cy="15"
          r={R}
          strokeDasharray={`${(v / 100) * C} ${C}`}
          transform="rotate(-90 15 15)"
        />
      ) : null}
    </svg>
  );
}

function DayPl({ v, equity }) {
  // day % only against a real, positive starting value (DEBUG_F4 F4-2: never against a 0 base)
  const base = isNum(equity) && equity > 0 && isNum(v) ? equity - v : null;
  const p = isNum(base) && base > 0 ? (v / base) * 100 : null;
  return (
    <span className={`rha-day ${dirClass(v)}`}>
      {arrow(v) ? `${arrow(v)} ` : ''}
      {money(v, { signed: true })}
      {isNum(p) ? <span className="rha-day__pct"> ({pct(p, { digits: 2 })})</span> : null}
      <span className="mui-sr"> today</span>
    </span>
  );
}

export function idleText(r) {
  const days = r.idle_days;
  const since = r.idle_since;
  if (!(isNum(days) && days > 0) && !since) return null;
  const head = `${r.account} has ${money(r.cash)} uninvested`;
  // mason_exposure._idle: idle_since is set only while snapshot history is shorter than
  // idle_history_days, i.e. the streak may be longer than we can see → "tracking since".
  if (since) return `${head}, tracking since ${date(since)}`;
  return `${head} for ${count(days)} day${Math.round(days) === 1 ? '' : 's'}`;
}

function divText(d) {
  if (!d || typeof d !== 'object') return null;
  const rec = `${money(d.received)}${d.received_is_est ? ' est.' : ''} received`;
  return `Dividends ${rec} · ${money(d.projected_12m)} next 12m`;
}

const AccountRow = memo(function AccountRow({ r }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const fam = isFamily(r);
  const idle = idleText(r);
  const top = Array.isArray(r.top) ? r.top.slice(0, 3) : [];
  const nPos = isNum(r.n_positions) ? count(r.n_positions) : DASH;
  return (
    <li className={`rha-acct${fam ? ' is-family' : ''}${open ? ' is-open' : ''}`} data-testid={`acct-${r.account}`}>
      <button
        type="button"
        className="rha-acct__main"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen(o => !o)}
      >
        <span className="rha-acct__cash">
          <Ring value={r.cash_pct} />
          <span className="rha-acct__cashpct">
            {isNum(r.cash_pct) ? pct(r.cash_pct, { signed: false }) : DASH} cash
          </span>
        </span>
        <span className="rha-acct__name">
          <b>{r.account}</b>
          <span className="rha-acct__mask">{r.account_masked}</span>
          {fam ? <span className="mui-badge rha-fam">family</span> : null}
        </span>
        <span className="rha-acct__eq">
          <MoneyCounter value={r.equity} fontSize={15} />
          <DayPl v={r.day_pl} equity={r.equity} />
        </span>
        <span className="rha-acct__chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {idle || r.dividends ? (
        <div className="rha-acct__sub">
          {idle ? <span className="rha-idle">{idle}</span> : null}
          {r.dividends ? <span className="rha-div">{divText(r.dividends)}</span> : null}
        </div>
      ) : null}
      <div id={detailId} className="rha-acct__detail" hidden={!open}>
        {open ? (
          <>
            {fam ? (
              <span className="mui-n" data-testid="acct-family-detail">
                {nPos} positions · family account — display only
              </span>
            ) : (
              <span className="mui-n">
                Top {top.length} of {nPos} positions
              </span>
            )}
            {fam ? null : top.length ? (
              <ul className="rha-top">
                {top.map(p => (
                  <li key={p.ticker}>
                    <b>{p.ticker}</b>
                    <span className="rha-top__bar" aria-hidden="true">
                      <span style={{ width: `${isNum(p.pct) ? Math.max(0, Math.min(100, p.pct)) : 0}%` }} />
                    </span>
                    <span>{pct(p.pct, { signed: false })}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="mui-muted"> · no positions</span>
            )}
          </>
        ) : null}
      </div>
    </li>
  );
});

function Head({ rows }) {
  const mine = rows.filter(isMine);
  const mineEq = sum(mine, 'equity');
  const minePl = sum(mine, 'day_pl');
  return (
    <div className="rha-head" data-testid="acct-head">
      <span className="rha-head__label" title="Mine = accounts with scope “personal” (family accounts excluded)">
        Mine
      </span>
      <MoneyCounter value={mineEq} fontSize={20} className="rha-head__eq" />
      {isNum(mineEq) ? <DayPl v={minePl} equity={mineEq} /> : null}
      <span className="rha-head__all">
        all {count(rows.length)} accounts{rows.some(isFamily) ? ' (incl. family)' : ''} {money(sum(rows, 'equity'))}
      </span>
    </div>
  );
}

export default function RhAccounts() {
  const api = useRhApi('/api/rh/accounts', null, { pollMs: 60000 });
  return (
    <div className="rha">
      <RhState
        api={api}
        empty={d => !Array.isArray(d) || d.length === 0}
        emptyText="No Robinhood accounts yet."
        loadingRows={4}
      >
        {d => <Accounts d={d} />}
      </RhState>
    </div>
  );
}

function Accounts({ d }) {
  const rows = useMemo(() => objs(d).map(cleanAccount), [d]);
  return (
    <>
      <Head rows={rows} />
      <ul className="rha-list" aria-label="Robinhood accounts">
        {rows.slice(0, ROW_CAP).map((r, i) => (
          <AccountRow key={`${r.account}-${r.account_masked}-${i}`} r={r} />
        ))}
      </ul>
      {rows.length > ROW_CAP ? <div className="mui-list-cap">{moreText(rows.length - ROW_CAP, 'accounts')}</div> : null}
    </>
  );
}
