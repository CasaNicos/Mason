// scoreboard parts: the running-total chart, the periods table, the idea tables and the
// "What kinds of calls have worked" section. All server text is JSX text (React escapes it).
//
// The chart is a small inline SVG, not window.MasonChart: MasonChart draws candles / a line for a
// ticker or the account, and loads its own data; it cannot draw two given series. React Bits has no
// chart piece. No chart library is added (bundle design §4).
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { DASH, arrow, dirClass, isNum, money } from '../../lib/fmt.js';
import { countText, linePath, longDay, pct2, rangeText, rowDetail, shortDay, usd, usdPlain, yDomain, PERIOD_HINT } from './sbUtil.js';
import { makeMeasure, pickTimeTicks } from '../../lib/timeTicks.js';

// ── the running-total chart ──────────────────────────────────────────────────
const PAD = { l: 4, r: 58, t: 8, b: 18 };
const FALLBACK = { w: 320, h: 150 }; // before the first measure (and in tests)

function useBoxSize(ref) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const set = (w, h) => setSize(s => (Math.abs(s.w - w) < 1 && Math.abs(s.h - h) < 1 ? s : { w: Math.round(w), h: Math.round(h) }));
    const r = el.getBoundingClientRect();
    if (r.width > 0) set(r.width, r.height);
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(entries => {
      const box = entries.length ? entries[entries.length - 1].contentRect : null;
      if (box && box.width > 0) set(box.width, box.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

// x-axis dates: shared picker (lib/timeTicks.js, UI overhaul Task 3) — measured labels, never overlapping;
// a narrower chart gets a coarser nice interval (day → week → month → quarter → year). Font = .mui-sb__tick.
const tickMeasure = makeMeasure("10px ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace");

const axisMoney = v => (Math.abs(v) >= 10000 ? money(v, { compact: true }) : money(v));

export const RunningChart = memo(function RunningChart({ series }) {
  const boxRef = useRef(null);
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, '');
  const size = useBoxSize(boxRef);
  const w = size.w || FALLBACK.w;
  const h = Math.max(90, size.h || FALLBACK.h);
  const [sel, setSel] = useState(null); // index into series; null = the last day
  const last = series.length - 1;
  const at = sel == null || sel > last ? last : sel;

  const geo = useMemo(() => {
    const [lo, hi] = yDomain(series);
    const x0 = PAD.l;
    const x1 = Math.max(x0 + 10, w - PAD.r);
    const y0 = PAD.t;
    const y1 = Math.max(y0 + 10, h - PAD.b);
    const xAt = i => (series.length < 2 ? (x0 + x1) / 2 : x0 + (i * (x1 - x0)) / (series.length - 1));
    const yAt = v => y0 + ((hi - v) / (hi - lo)) * (y1 - y0);
    const pts = key => series.map((s, i) => ({ x: xAt(i), y: s[key] == null ? null : yAt(s[key]) }));
    const mason = pts('mason');
    const spy = pts('spy');
    const zeroY = yAt(0);
    const solid = mason.every(p => p.y != null) && mason.length > 1;
    const area = solid ? `${linePath(mason)}L${mason[mason.length - 1].x.toFixed(1)},${zeroY.toFixed(1)}L${mason[0].x.toFixed(1)},${zeroY.toFixed(1)}Z` : '';
    // right-hand scale: $0, and the highest / lowest real value (not the padded edge, which is a meaningless
    // number); a label too close to $0 is dropped so labels never overlap
    const vals = series.flatMap(s => [s.mason, s.spy]).filter(v => v != null);
    const ticks = [{ v: 0, y: zeroY, zero: true }];
    for (const v of [Math.max(0, ...vals), Math.min(0, ...vals)]) if (v !== 0 && Math.abs(yAt(v) - zeroY) >= 14) ticks.push({ v, y: yAt(v) });
    const xTicks = pickTimeTicks({ times: series.map(s => s.day), xOf: xAt, x0, x1, measure: tickMeasure, utc: true }).ticks;
    return { x0, x1, y0, y1, xAt, mason, spy, zeroY, area, ticks, xTicks, masonD: linePath(mason), spyD: linePath(spy) };
  }, [series, w, h]);

  const pick = useCallback(
    e => {
      const svg = e.currentTarget;
      const r = svg.getBoundingClientRect();
      if (!r.width || series.length < 2) return;
      const x = ((e.clientX - r.left) / r.width) * w;
      const i = Math.round(((x - geo.x0) / (geo.x1 - geo.x0)) * (series.length - 1));
      setSel(Math.max(0, Math.min(last, i)));
    },
    [geo, series.length, last, w]
  );
  const onKey = e => {
    const moves = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: last };
    if (!(e.key in moves)) return;
    e.preventDefault();
    setSel(Math.max(0, Math.min(last, moves[e.key])));
  };

  if (!series.length) return <p className="mui-sb__none">No trading days to chart yet.</p>;
  const day = series[at];
  const sx = geo.xAt(at);
  return (
    <div className="mui-sb__chartwrap">
      <ul className="mui-sb__legend" aria-label="Chart key">
        <li data-series="mason">
          <svg viewBox="0 0 22 8" width="22" height="8" aria-hidden="true" focusable="false">
            <line x1="1" y1="4" x2="21" y2="4" />
          </svg>
          Mason
        </li>
        <li data-series="spy">
          <svg viewBox="0 0 22 8" width="22" height="8" aria-hidden="true" focusable="false">
            <line x1="1" y1="4" x2="21" y2="4" />
          </svg>
          S&amp;P 500, same money
        </li>
      </ul>
      <div
        ref={boxRef}
        className="mui-sb__chart"
        tabIndex={0}
        role="group"
        aria-label="Running total chart. Use the left and right arrow keys to read each day."
        aria-describedby={`${uid}-read`}
        onKeyDown={onKey}
      >
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} onPointerMove={pick} onPointerLeave={() => setSel(null)} aria-hidden="true" focusable="false">
          <defs>
            <linearGradient id={`${uid}-fill`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" className="mui-sb__stop1" />
              <stop offset="100%" className="mui-sb__stop2" />
            </linearGradient>
            <clipPath id={`${uid}-clip`}>
              <rect className="mui-sb__reveal" x="0" y="0" width={w} height={h} />
            </clipPath>
          </defs>
          <line className="mui-sb__grid" x1={geo.x0} x2={geo.x1} y1={geo.y0} y2={geo.y0} />
          <line className="mui-sb__grid" x1={geo.x0} x2={geo.x1} y1={geo.y1} y2={geo.y1} />
          <line className="mui-sb__zero" x1={geo.x0} x2={geo.x1} y1={geo.zeroY} y2={geo.zeroY} />
          {geo.ticks.map(t => (
            <text key={t.zero ? 'z' : t.v} className={`mui-sb__tick${t.zero ? ' is-zero' : ''}`} x={geo.x1 + 6} y={t.y} dominantBaseline="middle">
              {t.zero ? '$0' : axisMoney(t.v)}
            </text>
          ))}
          {geo.xTicks.map(tk => (
            <text key={tk.i} className="mui-sb__tick" data-xtick="" x={tk.tx} y={h - 4} textAnchor={tk.anchor}>
              {tk.label}
            </text>
          ))}
          <g clipPath={`url(#${uid}-clip)`}>
            {geo.area ? <path className="mui-sb__area" d={geo.area} fill={`url(#${uid}-fill)`} /> : null}
            <path className="mui-sb__line" data-series="spy" d={geo.spyD} />
            <path className="mui-sb__line" data-series="mason" d={geo.masonD} />
            {series.length === 1 || geo.mason.some(p => p.y == null) || geo.spy.some(p => p.y == null)
              ? series.map((s, i) => (
                  <g key={s.day}>
                    {geo.spy[i].y != null ? <circle className="mui-sb__pt" data-series="spy" cx={geo.spy[i].x} cy={geo.spy[i].y} r="2.5" /> : null}
                    {geo.mason[i].y != null ? <circle className="mui-sb__pt" data-series="mason" cx={geo.mason[i].x} cy={geo.mason[i].y} r="2.5" /> : null}
                  </g>
                ))
              : null}
          </g>
          <line className="mui-sb__cursor" x1={sx} x2={sx} y1={geo.y0} y2={geo.y1} />
          {geo.spy[at].y != null ? <circle className="mui-sb__dot" data-series="spy" cx={sx} cy={geo.spy[at].y} r="3.5" /> : null}
          {geo.mason[at].y != null ? <circle className="mui-sb__dot" data-series="mason" cx={sx} cy={geo.mason[at].y} r="3.5" /> : null}
        </svg>
      </div>
      <p className="mui-sb__read" id={`${uid}-read`} data-day={day.day}>
        <span className="mui-sb__readday">{longDay(day.day)}</span>
        <span>
          Mason <b className={dirClass(day.mason)}>{usd(day.mason)}</b>
        </span>
        <span>
          S&amp;P 500 <b>{usd(day.spy)}</b>
        </span>
        <span>
          <b>{usdPlain(day.atWork)}</b> at work
        </span>
      </p>
    </div>
  );
});

// ── Mason against the S&P 500, by period ─────────────────────────────────────
function Money({ v, p, beat = false }) {
  const mark = beat ? arrow(v) : '';
  return (
    <>
      <span className={beat ? dirClass(v) : undefined}>
        {mark ? <span aria-hidden="true">{mark} </span> : null}
        {usd(v)}
      </span>
      <span className="mui-sb__sub">{pct2(p)}</span>
    </>
  );
}

export const PeriodsTable = memo(function PeriodsTable({ periods, asOf }) {
  const since = (periods.find(p => p.key === 'since_start') || {}).data;
  return (
    <div className="mui-sb__scroll" role="region" aria-label="Mason against the S&P 500, by period" tabIndex={0}>
      <table className="mui-sb__table mui-sb__periods">
        <thead>
          <tr>
            <th scope="col">Period</th>
            <th scope="col" className="is-num">
              Mason
            </th>
            <th scope="col" className="is-num">
              S&amp;P 500, same money
            </th>
            <th scope="col" className="is-num">
              Ahead or behind
            </th>
          </tr>
        </thead>
        <tbody>
          {periods.map(({ key, label, data: d }) => {
            const same = key !== 'since_start' && d && d.whole;
            const span = d && d.from && d.to ? (d.from === d.to ? shortDay(d.to, asOf) : `${shortDay(d.from, asOf)} – ${shortDay(d.to, asOf)}`) : PERIOD_HINT[key];
            return (
              <tr key={key} data-period={key} data-same={same ? '1' : undefined}>
                <th scope="row">
                  {label}
                  <span className="mui-sb__sub">{span}</span>
                </th>
                {same ? (
                  <td colSpan={3} className="mui-sb__same">
                    same as since it began
                  </td>
                ) : (
                  <>
                    <td className="is-num">{d ? <Money v={d.mason} p={d.masonPct} /> : DASH}</td>
                    <td className="is-num">{d ? <Money v={d.spy} p={d.spyPct} /> : DASH}</td>
                    <td className="is-num" data-col="beat">
                      {d ? <Money v={d.beat} p={d.beatPct} beat /> : DASH}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mui-sb__cap">
        Percents are of the most money Mason had in trades at one time{since && isNum(since.atWork) ? ` (${usdPlain(since.atWork)} since it began)` : ''}.
      </p>
    </div>
  );
});

// ── idea tables ──────────────────────────────────────────────────────────────
// "What it says" sits right after the row label, so on a phone it is seen without scrolling the table.
export function Verdict({ verdict, plain = false }) {
  if (plain) {
    return (
      <span className="mui-sb__verdict" data-tone="yardstick">
        {verdict.text}
      </span>
    );
  }
  return (
    <span className="mui-sb__verdict" data-tone={verdict.tone}>
      {verdict.mark ? <span aria-hidden="true">{verdict.mark} </span> : null}
      {verdict.text}
    </span>
  );
}

/** One table of rows. `yardstick` = the comparison table: its verdict is never coloured. */
export function IdeasTable({ rows, label, countWord = 'Ideas', yardstick = false }) {
  return (
    <div className="mui-sb__scroll" role="region" aria-label={label} tabIndex={0}>
      <table className="mui-sb__table mui-sb__ideas" data-yardstick={yardstick ? '1' : undefined}>
        <thead>
          <tr>
            <th scope="col">
              <span className="mui-sr">Group</span>
            </th>
            <th scope="col">What it says</th>
            <th scope="col" className="is-num">
              {countWord}
            </th>
            <th scope="col" className="is-num">
              Average
            </th>
            <th scope="col" className="is-num">
              Middle
            </th>
            <th scope="col" className="is-num">
              Likely range
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${i}:${r.group}`} data-verdict={yardstick ? undefined : r.verdict.tone} title={rowDetail(r) || undefined}>
              <th scope="row">
                {r.group}
                {yardstick ? <span className="mui-sb__tag">yardstick</span> : null}
              </th>
              <td>
                <Verdict verdict={r.verdict} plain={yardstick} />
              </td>
              <td className="is-num">{countText(r.n)}</td>
              <td className="is-num">{pct2(r.avg)}</td>
              <td className="is-num">{pct2(r.typical)}</td>
              <td className="is-num mui-sb__range">{rangeText(r.low, r.high)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Coverage({ c }) {
  const bits = [];
  if (c.inTables != null) bits.push(`These tables rest on ${countText(c.inTables)} ideas.`);
  const out = [];
  if (c.lowPrice) out.push(`${countText(c.lowPrice)} on stocks under ${isNum(c.minPrice) ? money(c.minPrice) : 'the minimum price'}`);
  if (c.noMinute) out.push(`${countText(c.noMinute)} with no price at the minute the idea appeared`);
  if (out.length) bits.push(`Left out: ${out.join(', ')}.`);
  if (c.waiting) bits.push(`${countText(c.waiting)} more are still being followed.`);
  return bits.length ? <p className="mui-sb__cap" data-part="coverage">{bits.join(' ')}</p> : null;
}

export const WhatWorked = memo(function WhatWorked({ worked, open, onToggle }) {
  const bodyId = `${useId().replace(/[^A-Za-z0-9_-]/g, '')}-ww`;
  const yard = worked.tables.find(t => t.key === 'comparison');
  const yardAvg = yard && yard.rows[0] ? yard.rows[0].avg : null;
  const days = worked.horizon != null ? `${worked.horizon} trading ${worked.horizon === 1 ? 'day' : 'days'}` : 'a few trading days';
  return (
    <section className="mui-sb__ww" data-open={open ? '1' : '0'}>
      <button type="button" className="mui-sb__toggle" aria-expanded={open} aria-controls={bodyId} data-act="toggle-ww" onClick={onToggle}>
        <span className="mui-sb__chev" aria-hidden="true">
          ›
        </span>
        What kinds of calls have worked
        {!open && worked.tables.length ? <span className="mui-sb__tag">{worked.tables.length} tables</span> : null}
      </button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            key="ww"
            id={bodyId}
            className="mui-sb__wwbody"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: 'easeOut' }}
          >
            <p className="mui-sb__lead">
              Every idea Mason saw is followed for {days} and compared with the S&amp;P 500 over the same days. These are results on paper,
              not money made.
            </p>
            <p className="mui-sb__calm" data-part="calm">
              Nothing in this section is a reason to change a setting.
            </p>
            {worked.leans ? (
              <p className="mui-sb__leans" data-part="leans">
                {worked.leans.leaning} of the {worked.leans.groups} groups with enough data lean one way or the other
                {worked.leans.luck != null ? `; about ${worked.leans.luck} would lean by luck alone` : ''}.
              </p>
            ) : null}
            <p className="mui-sb__cap">
              Average and Middle are each idea’s result minus the S&amp;P 500’s over the same days (Middle: half did better, half worse).
              Likely range: where the real average most likely sits.
            </p>
            {worked.tables.map(t => (
              <div className="mui-sb__tbl" key={t.key} data-table={t.key}>
                <h4 className="mui-sb__h">{t.title || DASH}</h4>
                {t.note ? <p className="mui-sb__note">{t.note}</p> : null}
                {t.key !== 'comparison' && isNum(yardAvg) ? <p className="mui-sb__yard">Compare with the yardstick above: {pct2(yardAvg)}</p> : null}
                <IdeasTable rows={t.rows} label={t.title || t.key} yardstick={t.key === 'comparison'} />
              </div>
            ))}
            {!worked.tables.length ? <p className="mui-sb__none">No ideas have finished their {days} yet.</p> : null}
            <Coverage c={worked.coverage} />
          </motion.div>
        ) : null}
      </AnimatePresence>
    </section>
  );
});
