// rhEarnings — "Which of my holdings report next, and how do they usually do?" (S3 §3 F4.3,
// contracts §6 /api/rh/earnings). One row per held ticker, sorted by next report date:
// 8 EPS-surprise mini-bars (SVG, signed, zero baseline; built from scratch — React Bits has no
// charts), next date + timing, implied move, and the "You hold this through earnings" chip
// (React Bits StarBorder + ShinyText).
//
// Motion (K7 interim ruling): only ONE chip animates — the soonest held-through report — and only
// while useMotionAllowed() (visible tab, no reduced motion). Every other chip renders the same
// components static: ShinyText `disabled`, StarBorder's CSS loop switched off from rhEarnings.css.
import { memo, useMemo } from 'react';
import StarBorder from '../../reactbits/StarBorder/StarBorder.jsx';
import ShinyText from '../../reactbits/ShinyText/ShinyText.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { useMotionAllowed } from '../../lib/motion.js';
import { pct, arrow, date, num, isNum, nLabel, lagDays, DASH } from '../../lib/fmt.js';
import { useAccountScopes, accountLabel } from '../rhAccounts/scope.js';
import { fin, objs, moreText, PCT_MAX } from '../rhAccounts/guard.js';
import './rhEarnings.css';

export const SLOTS = 8;
const BW = 7; // bar width
const BG = 3; // gap
const H = 26;
const MID = H / 2;

const TIMING = { am: 'before open', bmo: 'before open', pm: 'after close', amc: 'after close' };
const timingText = t => (t ? TIMING[String(t).toLowerCase()] || String(t) : '');
// Plain code-unit comparison (ISO dates, tickers): localeCompare is ~100× slower and a 10k
// payload sorted with it cost a > 100 ms main-thread task.
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

export const ROW_CAP = 40; // rows drawn; the rest are counted (keeps a 10k payload under one long task)
const ACCT_CAP = 6;

/** Boundary clean: the latest SLOTS quarters only, finite plausible percents, string accounts. */
export function cleanRow(r, i = 0) {
  const quarters = objs(r.quarters)
    .sort((a, b) => cmp(String(b.report_date || ''), String(a.report_date || '')))
    .slice(0, SLOTS)
    .map(q => ({ ...q, period: q.period == null ? '—' : String(q.period), surprise_pct: fin(q.surprise_pct, PCT_MAX) }));
  return {
    ...r,
    _rk: `${String(r.ticker)}#${i}`, // unique React key even when tickers repeat (F4-1)
    ticker: String(r.ticker),
    quarters,
    implied_move_pct: fin(r.implied_move_pct, PCT_MAX),
    held_accounts: Array.isArray(r.held_accounts) ? r.held_accounts.map(String) : []
  };
}

const nextDate = r => (r && r.next && typeof r.next.date === 'string' && r.next.date ? r.next.date : null);

/** Sort by next report date ascending; rows without a date last; ticker breaks ties. */
export function sortRows(rows) {
  return [...rows].sort((a, b) => {
    const da = nextDate(a);
    const db = nextDate(b);
    if (da !== db) {
      if (!da) return 1;
      if (!db) return -1;
      return da < db ? -1 : 1;
    }
    return cmp(String(a.ticker), String(b.ticker));
  });
}

/** The soonest held-through ROW (identity, not ticker: duplicate tickers must not all go live,
 *  DEBUG_F4 F4-1). Ties keep the earlier row in `rows` order. */
export function soonestHeld(rows) {
  let best = null; // one linear pass (rows can be 10k)
  for (const r of rows) {
    if (r.held_through !== true || !nextDate(r)) continue;
    if (!best || cmp(nextDate(r), nextDate(best)) < 0) best = r;
  }
  return best;
}

const surpriseText = q =>
  isNum(q.surprise_pct) ? `${arrow(q.surprise_pct)} ${pct(q.surprise_pct)}`.trim() : `${DASH} (no surprise data)`;

const Bars = memo(function Bars({ quarters, scale }) {
  const qs = (Array.isArray(quarters) ? quarters : [])
    .filter(q => q && typeof q === 'object')
    .sort((a, b) => cmp(String(a.report_date || ''), String(b.report_date || '')))
    .slice(-SLOTS);
  const pad = SLOTS - qs.length;
  const W = SLOTS * BW + (SLOTS - 1) * BG;
  const k = (MID - 1) / scale;
  const summary = qs.length
    ? `EPS surprise, last ${qs.length} quarters, oldest first: ${qs.map(q => `${q.period} ${isNum(q.surprise_pct) ? pct(q.surprise_pct) : DASH}`).join(', ')}`
    : 'No reported quarters';
  return (
    <svg className="rhe-bars" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
      <line className="rhe-zero" x1="0" x2={W} y1={MID} y2={MID} />
      {Array.from({ length: pad }, (_, i) => (
        <rect key={`e${i}`} className="rhe-slot-empty" x={i * (BW + BG)} y={MID - 1} width={BW} height="2" rx="1">
          <title>no data</title>
        </rect>
      ))}
      {qs.map((q, j) => {
        const x = (pad + j) * (BW + BG);
        const v = q.surprise_pct;
        const label = `${q.period} (reported ${date(q.report_date)}): ${surpriseText(q)} EPS surprise`;
        if (!isNum(v)) {
          return (
            <rect key={`${q.period}-${j}`} className="rhe-slot-empty" x={x} y={MID - 1} width={BW} height="2" rx="1">
              <title>{label}</title>
            </rect>
          );
        }
        const hgt = Math.max(1.5, Math.min(MID - 1, Math.abs(v) * k));
        const up = v > 0;
        return (
          <rect
            key={`${q.period}-${j}`}
            className={`rhe-bar ${up ? 'mui-up' : v < 0 ? 'mui-down' : 'mui-flat'}`}
            x={x}
            y={up ? MID - hgt : MID}
            width={BW}
            height={hgt}
            rx="1.5"
          >
            <title>{label}</title>
          </rect>
        );
      })}
    </svg>
  );
});

function Chip({ live }) {
  return (
    <StarBorder
      as="span"
      className={`rhe-chip ${live ? 'is-live' : 'is-static'}`}
      color="var(--mui-accent-300)"
      speed="5s"
      thickness={1}
      backgroundColor="var(--mui-accent-900)"
      textColor="var(--mui-text)"
      borderColor="var(--mui-line-strong)"
    >
      <ShinyText
        text="You hold this through earnings"
        disabled={!live}
        speed={3}
        color="var(--mui-accent-300)"
        shineColor="var(--mui-text)"
      />
    </StarBorder>
  );
}

// memo: a later /api/rh/accounts answer or a poll with the same rows must not re-format every bar
const Row = memo(function Row({ r, scale, live, asOf, family }) {
  const qs = Array.isArray(r.quarters) ? r.quarters.filter(q => q && typeof q === 'object') : [];
  const nNum = qs.filter(q => isNum(q.surprise_pct)).length;
  const latest = [...qs]
    .filter(q => isNum(q.surprise_pct))
    .sort((a, b) => cmp(String(b.report_date || ''), String(a.report_date || '')))[0];
  const nd = nextDate(r);
  const days = nd && asOf ? lagDays(String(asOf).slice(0, 10), nd) : null;
  const held = Array.isArray(r.held_accounts) ? r.held_accounts : [];
  return (
    <li className={`rhe-row${r.held_through ? ' is-held' : ''}`} data-testid={`earn-${r.ticker}`}>
      <div className="rhe-row__top">
        <b className="rhe-tk">{r.ticker}</b>
        <Bars quarters={qs} scale={scale} />
        <span className="rhe-last">
          {latest ? <>last {surpriseText(latest)}</> : <>last {DASH}</>} <span className="mui-n">{nLabel(nNum)}</span>
        </span>
        <span className="rhe-next">
          {nd ? (
            <>
              next <b>{date(nd)}</b>
              {timingText(r.next.timing) ? ` · ${timingText(r.next.timing)}` : ''}
              {isNum(days) && days >= 0 ? <span className="mui-n"> · in {days}d</span> : null}
            </>
          ) : (
            <>next {DASH}</>
          )}
        </span>
        <span className="rhe-imp">
          {isNum(r.implied_move_pct) ? `±${num(r.implied_move_pct, 1)}% implied` : `implied ${DASH}`}
        </span>
      </div>
      <div className="rhe-row__sub">
        {r.held_through ? <Chip live={live} /> : null}
        <span className="rhe-accts">
          {held.slice(0, ACCT_CAP).map((a, i) => (
            <span key={`${a}-${i}`} className="rhe-acct">
              {accountLabel(a, family)}
            </span>
          ))}
          {held.length > ACCT_CAP ? <span className="rhe-more">{moreText(held.length - ACCT_CAP, 'accounts')}</span> : null}
        </span>
      </div>
    </li>
  );
});

function Strip({ data, asOf, family }) {
  const motionOk = useMotionAllowed();
  const all = useMemo(() => sortRows(objs(data).filter(r => r.ticker != null).map((r, i) => cleanRow(r, i))), [data]);
  const rows = all.slice(0, ROW_CAP);
  const liveRow = motionOk ? soonestHeld(all) : null; // `all` is sorted, so ties go to the first drawn row
  const scale = useMemo(() => {
    let m = 5;
    for (const r of rows) for (const q of Array.isArray(r.quarters) ? r.quarters : []) if (q && isNum(q.surprise_pct)) m = Math.max(m, Math.abs(q.surprise_pct));
    return m;
  }, [rows]);
  return (
    <>
      <div className="rhe-head">
        <span>EPS surprise, last {SLOTS} quarters (oldest → newest)</span>
        <span className="mui-n">bar scale ±{num(scale, 0)}%</span>
      </div>
      <ul className="rhe-list" aria-label="Earnings for held tickers">
        {rows.map(r => (
          <Row key={r._rk} r={r} scale={scale} live={r === liveRow} asOf={asOf} family={family} />
        ))}
      </ul>
      {all.length > ROW_CAP ? <div className="mui-list-cap">{moreText(all.length - ROW_CAP, 'held tickers (latest report dates last)')}</div> : null}
    </>
  );
}

export default function RhEarnings() {
  const api = useRhApi('/api/rh/earnings', null, { pollMs: 120000 });
  const scopes = useAccountScopes();
  return (
    <div className="rhe">
      <RhState api={api} empty={d => !Array.isArray(d) || d.length === 0} emptyText="No held tickers to show.">
        {d => <Strip data={d} asOf={api.asOf} family={scopes.family} />}
      </RhState>
    </div>
  );
}
