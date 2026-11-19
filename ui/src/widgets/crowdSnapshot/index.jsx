// crowdSnapshot — "Ticker Snapshot" (page-only, #crowd). UI overhaul Task 4 (2026-11-16).
// Click any ticker anywhere on the Crowd page (tracker chip, Crowd Moves row, scorecard example, mover row) and
// this panel opens it: what the company does, sector / industry / size, a price chart, and its crowd history
// (rank path over the last snapshots, when it joined or left, and how it did vs SPY afterwards).
// - Picks arrive on the crowd pick bus (crowdShared/data.js, a mason:chart-symbol channel). The last pick is
//   saved in this widget's page config, so a reload shows the same ticker. Before any pick: your highest
//   ranked holding on the list, else #1.
// - A pick from another widget flashes this panel (MasonUI.effects.electrify = React Bits ElectricBorder,
//   one at a time, ~1 s) and scrolls it into view when it is off screen.
// - Company profile: GET /api/rh/crowd/ticker?symbol= (cached Robinhood fundamentals). Until Mason runs the
//   backend that has it (NEEDS RESTART), the panel says details aren't available and shows the rest.
// - Chart: <MasonChartHost> = window.MasonChart, the template's canvas engine (line, daily bars by default;
//   its own type/timeframe menus, crosshair and date axis). Rank path: RankSpark (SVG, lib/timeTicks axis).
// React Bits: Counter (rolling rank digits), ElectricBorder (via effects.electrify). Built from scratch: the
// profile/facts layout and the crowd-history list (no React Bits piece is a data card of this shape).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import Counter from '../../reactbits/Counter/Counter.jsx';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { useRhApi } from '../../lib/api.js';
import { normTicker, onChartSymbol } from '../../lib/bridge.js';
import { electrify } from '../../lib/effects.js';
import { arrow, date, dirClass, isNum, money, num, pct } from '../../lib/fmt.js';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import {
  PICK_CHANNEL,
  TICKER_ROUTE,
  arr,
  capClass,
  eventsFor,
  hasHistory,
  isFundLike,
  labelOf,
  obj,
  rankPath,
  shortDate,
  str,
  todayRows,
  useCrowd,
  xsPct
} from '../crowdShared/data.js';
import { CrowdState, Delta, Term, Yours, RankSpark, useBox } from '../crowdShared/ui.jsx';

const HORIZONS = [
  ['5', '1 week', '5 trading days'],
  ['20', '1 month', '20 trading days'],
  ['60', '3 months', '60 trading days']
];

function Facts({ p, fund }) {
  const items = [];
  if (isNum(p.market_cap)) {
    items.push([
      'cap',
      'Market cap',
      `${money(p.market_cap, { compact: true })}${capClass(p.market_cap) ? ` · ${capClass(p.market_cap)}` : ''}`
    ]);
  }
  if (!fund && isNum(p.pe_ratio)) items.push(['pe', 'P/E', num(p.pe_ratio, 1)]);
  if (isNum(p.dividend_yield) && p.dividend_yield > 0) items.push(['yield', 'Dividend yield', pct(p.dividend_yield, { digits: 2, signed: false })]);
  if (isNum(p.low_52_weeks) && isNum(p.high_52_weeks)) items.push(['range52', '52-week range', `${money(p.low_52_weeks)} – ${money(p.high_52_weeks)}`]);
  if (isNum(p.year_founded) && p.year_founded > 1600) items.push([null, 'Founded', String(p.year_founded)]);
  if (isNum(p.num_employees) && p.num_employees > 0) items.push([null, 'Employees', num(p.num_employees)]);
  if (str(p.headquarters)) items.push([null, 'Headquarters', str(p.headquarters)]);
  if (!items.length) return null;
  return (
    <dl className="mui-crowdsnap__facts">
      {items.map(([k, label, value]) => (
        <div key={label} className="mui-crowdsnap__fact">
          <dt>{k ? <Term k={k}>{label}</Term> : label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function About({ ticker, label, mock }) {
  const api = useRhApi(TICKER_ROUTE, { symbol: ticker });
  const [more, setMore] = useState(false);
  useEffect(() => setMore(false), [ticker]);
  const d = api.status === 'ok' ? obj(api.data) : null;
  // mock mode serves one fixture for every symbol: show it anyway (harness); live, it must match the pick
  const sameSym = d && (mock || str(d.symbol).toUpperCase() === ticker);
  const p = sameSym ? obj(d.profile) : {};
  const sector = str(p.sector) || label.sector || '';
  const industry = str(p.industry) || label.industry || '';
  const fund = isFundLike(industry, sector);
  const cap = capClass(p.market_cap);
  const desc = str(p.description);
  let note = null;
  if (api.status === 'loading') note = null;
  else if (api.status === 'off') note = 'Crowd data is off.';
  else if (api.status === 'error' || !sameSym) note = 'Company details aren’t available yet.';
  else if (d.status === 'pending') note = 'Company details are on the way — Mason fetches them about once an hour.';
  else if (d.status === 'unknown') note = 'No company details: Mason only fetches them for tickers that have been on the list.';
  else if (d.status === 'not_found') note = 'Robinhood has no company details for this ticker.';
  return (
    <section className="mui-crowdsnap__about" aria-label={`About ${ticker}`}>
      <div className="mui-crowdsnap__chips">
        {fund ? (
          <span className="mui-crowdsnap__chip is-fund">
            <Term k="etf">Fund (ETF)</Term>
          </span>
        ) : sector && sector !== 'Miscellaneous' ? (
          <span className="mui-crowdsnap__chip">{sector}</span>
        ) : null}
        {industry && !fund ? <span className="mui-crowdsnap__chip is-soft">{industry}</span> : null}
        {cap ? (
          <span className="mui-crowdsnap__chip is-cap">
            <Term k="cap">{cap}</Term>
          </span>
        ) : null}
      </div>
      {api.status === 'loading' ? (
        <div className="mui-crowdsnap__skel" role="status" aria-live="polite">
          <span className="mui-sr">Loading company details…</span>
          <i className="mui-skel" />
          <i className="mui-skel" />
          <i className="mui-skel" />
        </div>
      ) : desc ? (
        <p className={`mui-crowdsnap__desc ${more ? 'is-open' : ''}`}>
          {desc}{' '}
          {desc.length > 160 ? (
            <button type="button" className="mui-crowdsnap__more" onClick={() => setMore(v => !v)} aria-expanded={more}>
              {more ? 'less' : 'more'}
            </button>
          ) : null}
        </p>
      ) : note ? (
        <p className="mui-crowdsnap__note" title={api.error || undefined}>
          {note}
        </p>
      ) : null}
      {sameSym && d.status === 'ok' ? <Facts p={p} fund={fund} /> : null}
      {sameSym && d.as_of ? <span className="mui-crowdsnap__src">Robinhood fundamentals · {date(d.as_of)}</span> : null}
    </section>
  );
}

function Horizon({ h, f }) {
  const x = obj(f);
  if (!f || x.matured !== true) {
    return (
      <span className="mui-crowdsnap__hz is-pending" title={`${h[2]}: not graded yet`}>
        {h[1]} <i>pending</i>
      </span>
    );
  }
  const xs = isNum(x.xs_spy) ? x.xs_spy : null;
  const ret = isNum(x.ret) ? x.ret : null;
  return (
    <span className={`mui-crowdsnap__hz ${dirClass(xs)}`} title={`${h[2]}: stock ${xsPct(ret)}, ${xsPct(xs)} vs SPY (${shortDate(x.entry_date)} → ${shortDate(x.exit_date)})`}>
      {h[1]}{' '}
      <b>
        {arrow(xs)} {xsPct(xs)}
      </b>
    </span>
  );
}

function History({ data, ticker, row }) {
  const [boxRef, box] = useBox();
  const path = useMemo(() => rankPath(data, ticker), [data, ticker]);
  const evs = useMemo(() => eventsFor(data, ticker).slice(0, 4), [data, ticker]);
  const histOk = hasHistory(data);
  const onDays = path.filter(p => p.rank != null).length;
  const firstOn = path.length && path[0].rank != null;
  const lastSeen = [...path].reverse().find(p => p.rank != null);
  let status;
  if (row) {
    status = (
      <>
        On the list at <b>#{row.rank}</b>
        {row.prev != null && row.delta ? ` (was #${row.prev} on ${shortDate(row.prevDate)})` : row.isNew ? ' — new on the latest snapshot' : ''}
        {path.length ? `. Listed on ${onDays} of the last ${path.length} snapshots${firstOn ? ` (since at least ${shortDate(path[0].date)})` : ''}.` : '.'}
      </>
    );
  } else {
    status = (
      <>
        Not on the list now
        {lastSeen ? (
          <>
            {' '}
            — last seen {shortDate(lastSeen.date)} at <b>#{lastSeen.rank}</b>.
          </>
        ) : (
          '.'
        )}
      </>
    );
  }
  return (
    <section className="mui-crowdsnap__hist" aria-label={`${ticker} crowd history`}>
      <h4 className="mui-crowdsnap__h">
        Crowd history <Term k="rank">rank</Term>
      </h4>
      <p className="mui-crowdsnap__status">{status}</p>
      <div className="mui-crowdsnap__spark" ref={boxRef}>
        {path.length >= 2 ? (
          <RankSpark path={path} width={box.w} height={Math.max(44, Math.min(96, box.h))} axis hover label={`${ticker} rank`} />
        ) : (
          <span className="mui-crowdsnap__note">
            {histOk ? 'The rank path builds up one snapshot per trading day.' : 'Rank history needs the updated crowd service (Mason restart).'}
          </span>
        )}
      </div>
      {evs.length ? (
        <ul className="mui-crowdsnap__events">
          {evs.map(e => (
            <li key={`${e.kind}|${e.date}`} className={`mui-crowdsnap__ev is-${e.kind === 'exit' ? 'exit' : 'enter'}`}>
              <span className="mui-crowdsnap__ev-what">
                <span aria-hidden="true">{e.kind === 'exit' ? '◇' : '◆'}</span>{' '}
                <Term k={e.kind === 'exit' ? 'exit' : 'enter'}>{e.kind === 'exit' ? 'Left' : 'Joined'}</Term> {shortDate(e.date)}
                {isNum(e.rank) ? ` ${e.kind === 'exit' ? '(was' : 'at'} #${e.rank}${e.kind === 'exit' ? ')' : ''}` : ''}
              </span>
              <span className="mui-crowdsnap__ev-after">
                <Term k="spy">vs SPY</Term> after:{' '}
                {HORIZONS.map(h => (
                  <Horizon key={h[0]} h={h} f={obj(e.fwd)[h[0]]} />
                ))}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mui-crowdsnap__note">No joins or exits for {ticker} in the last 30 days.</p>
      )}
    </section>
  );
}

function RankBadge({ row }) {
  const reduce = usePrefersReducedMotion();
  if (!row || row.rank == null) return <span className="mui-crowdsnap__rank is-off">off the list</span>;
  const places = row.rank >= 100 ? [100, 10, 1] : row.rank >= 10 ? [10, 1] : [1];
  return (
    <span className="mui-crowdsnap__rank" title={`#${row.rank} on the list`}>
      <span className="mui-crowdsnap__hash">#</span>
      {reduce ? (
        <b>{row.rank}</b>
      ) : (
        <Counter value={row.rank} places={places} fontSize={22} padding={2} gap={0} horizontalPadding={0} gradientHeight={0} gradientFrom="transparent" fontWeight={700} />
      )}
      <span className="mui-sr"> {row.rank} on the list</span>
      <Delta row={row} />
    </span>
  );
}

function Snapshot({ data, ticker, mock, chartId }) {
  const rows = useMemo(() => todayRows(data, 1), [data]);
  const row = rows.find(r => r.ticker === ticker) || null;
  const label = labelOf(data, ticker);
  return (
    <div className="mui-crowdsnap" data-ticker={ticker}>
      <AnimatePresence mode="wait" initial={false}>
        <motion.header
          key={ticker}
          className="mui-crowdsnap__id"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}
        >
          <span className="mui-crowdsnap__sym">{ticker}</span>
          <span className="mui-crowdsnap__name">{label.name || ''}</span>
          <RankBadge row={row} />
          <Yours row={row} />
        </motion.header>
      </AnimatePresence>
      <div className="mui-crowdsnap__grid">
        <About ticker={ticker} label={label} mock={mock} />
        <section className="mui-crowdsnap__chart" aria-label={`${ticker} price chart`}>
          <MasonChartHost chartId={chartId} symbol={ticker} tf="1d" type="trend" follow={false} />
        </section>
        <History data={data} ticker={ticker} row={row} />
      </div>
    </div>
  );
}

export default function CrowdSnapshot({ config = {}, saveConfig, instanceId }) {
  const api = useCrowd();
  const reduce = usePrefersReducedMotion();
  const host = useRef(null);
  const [pick, setPick] = useState(() => normTicker(typeof config.ticker === 'string' ? config.ticker : ''));
  const cfg = useRef(config);
  cfg.current = config;
  const save = useRef(saveConfig);
  save.current = saveConfig;

  const onPick = useCallback(
    t => {
      setPick(t);
      if (typeof save.current === 'function' && cfg.current.ticker !== t) save.current({ ...cfg.current, ticker: t });
      const el = host.current;
      const node = el && el.closest ? el.closest('.m-widget') || el : el;
      if (!node || !node.getBoundingClientRect) return;
      const r = node.getBoundingClientRect();
      const vh = window.innerHeight || 800;
      if (r.bottom < 60 || r.top > vh - 60) {
        try {
          node.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
        } catch {
          /* old engines */
        }
      }
      electrify(node, { ms: 1100, fadeMs: 300, radius: 18 });
    },
    [reduce]
  );
  useEffect(() => onChartSymbol(d => onPick(d.ticker), { chartId: PICK_CHANNEL }), [onPick]);

  const data = api.status === 'ok' ? obj(api.data) : null;
  const fallback = useMemo(() => {
    if (!data) return null;
    const rows = todayRows(data, 1);
    return (rows.find(r => r.held) || rows[0] || {}).ticker || null;
  }, [data]);
  const ticker = pick || fallback;
  // one MasonChart record per instance (MasonChart persists per id): 'mui-chart-crowd-snap-<instance>'
  const chartId = `crowd-snap-${String(instanceId || 'page').replace(/[^A-Za-z0-9_-]+/g, '-')}`;
  return (
    <div className="mui-crowd mui-crowd--snapshot" ref={host}>
      <CrowdState api={api} loadingRows={5} empty={d => !d || (!arr(d.today).length && !pick)} emptyText="No list snapshot yet — pick a ticker once the first daily snapshot lands.">
        {d =>
          ticker ? (
            <Snapshot data={obj(d)} ticker={ticker} mock={api.mock} chartId={chartId} />
          ) : (
            <div className="mui-state mui-state--empty">Click any ticker on this page to see its snapshot.</div>
          )
        }
      </CrowdState>
    </div>
  );
}
