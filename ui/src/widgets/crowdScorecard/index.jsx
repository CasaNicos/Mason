// crowdScorecard — "Crowd Scorecard" (page-only, #crowd). UI overhaul Task 4 (2026-11-16): plain English first.
// It leads with ONE sentence ("Stocks the crowd piles into have lagged SPY by 1.87% over 20 trading days (n=11)")
// and an honest second line from the 95% CI (consistent so far / not proven yet). Below: one bar per horizon
// (1 week / 1 month / 3 months ≈ 5 / 20 / 60 trading sessions) on ONE shared scale where 0 = SPY: the bar is the
// average excess return, the soft band its likely range (95% CI), and every row keeps its n and beat-SPY share,
// so the rigor stays visible, just quieter. Tabs flip between stocks that JOINED the list and stocks that LEFT.
// Rows below min-n read "not enough data yet (n=…)" and show no number. The backend's own hypothesis text is
// kept, verbatim, as the footnote. Examples (best / worst graded joiners) open in the Ticker Snapshot.
// React Bits: SpotlightCard (the lead card), CountUp (event counts). The bars are ours: no React Bits piece
// draws a range/interval bar.
import { useMemo, useState } from 'react';
import { motion } from 'motion/react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { arrow, dirClass, isNum, nLabel, pct } from '../../lib/fmt.js';
import { absPct, arr, emitPick, obj, str, useCrowd, xsPct } from '../crowdShared/data.js';
import { CrowdState, Term } from '../crowdShared/ui.jsx';

const WINDOWS = [
  ['5s', 5, '1 week', '5 trading days'],
  ['20s', 20, '1 month', '20 trading days'],
  ['60s', 60, '3 months', '60 trading days']
];
const MAX_EVENTS = 1e7;
const VERDICT = {
  supported: 'Supported',
  contradicted: 'Contradicted',
  inconclusive: 'Inconclusive',
  insufficient_data: 'Not enough data'
};
const SIDES = [
  { id: 'enter', label: 'After joining', glyph: '◆' },
  { id: 'exit', label: 'After leaving', glyph: '◇' }
];

const ciOf = w => {
  const c = arr(w.ci95);
  return c.length === 2 && isNum(c[0]) && isNum(c[1]) ? [Math.min(c[0], c[1]), Math.max(c[0], c[1])] : null;
};
const meanOf = w => (isNum(w.mean_xs) ? w.mean_xs : null);
const usable = w => w.min_n_met === true && meanOf(w) != null;
const ciText = ci => (ci ? `95% CI ${xsPct(ci[0])} to ${xsPct(ci[1])}` : '95% CI —');

/** Shared x-domain over every plotted row of both sides (always includes 0 = SPY), padded 10%. */
function domainOf(sides) {
  let lo = 0;
  let hi = 0;
  for (const s of sides) {
    for (const [win] of WINDOWS) {
      const w = obj(obj(s.windows)[win]);
      if (!usable(w)) continue;
      const ci = ciOf(w) || [meanOf(w), meanOf(w)];
      lo = Math.min(lo, ci[0], meanOf(w));
      hi = Math.max(hi, ci[1], meanOf(w));
    }
  }
  if (hi - lo < 1e-9) return [-0.01, 0.01];
  const pad = (hi - lo) * 0.1;
  return [lo - pad, hi + pad];
}

/** The headline: which window, what it says, how sure. Prefers the 20-session window (the hypothesis). */
function leadOf(sc) {
  const enter = obj(sc.enter);
  const hyp = enter.hypothesis && typeof enter.hypothesis === 'object' ? enter.hypothesis : null;
  const win = ['20s', '5s', '60s'].find(k => usable(obj(obj(enter.windows)[k])));
  if (!win) {
    // the window with the most graded joiners so far (named in the sentence, so its n is not mistaken for another's)
    let best = null;
    for (const spec of WINDOWS) {
      const n = obj(obj(enter.windows)[spec[0]]).n;
      if (isNum(n) && n > 0 && (!best || n > best.n)) best = { n, spec };
    }
    return { win: null, n: best ? best.n : 0, spec: best ? best.spec : null, hyp };
  }
  const w = obj(obj(enter.windows)[win]);
  const spec = WINDOWS.find(x => x[0] === win);
  return { win, spec, w, m: meanOf(w), ci: ciOf(w), n: isNum(w.n) ? w.n : 0, hyp };
}

function Lead({ sc }) {
  const L = leadOf(sc);
  const hyp = L.hyp;
  const verdictKey = hyp ? str(hyp.verdict) : 'none';
  const verdict = hyp ? VERDICT[hyp.verdict] || str(hyp.verdict) || 'Not tested yet' : 'Not tested yet';
  let line;
  let sub;
  if (!L.win) {
    line = (
      <>
        Not enough graded joiners yet to tell whether stocks the crowd piles into beat <Term k="spy">SPY</Term> (
        <Term k="n">{nLabel(L.n)}</Term>
        {L.spec ? ` graded after ${L.spec[3]}` : ''} so far).
      </>
    );
    sub = 'Mason grades every stock that joins the list after 5, 20 and 60 trading days; this fills in as they mature.';
  } else {
    const m = L.m;
    const dir = m < 0 ? 'lagged' : m > 0 ? 'beaten' : 'matched';
    line = (
      <>
        Stocks the <Term k="crowd">crowd</Term> piles into have{' '}
        <b className={`mui-crowdsc__big ${dirClass(m)}`}>
          {dir}
          {m !== 0 ? (
            <>
              {' '}
              <Term k="spy">SPY</Term> by {arrow(m)} {absPct(m)}
            </>
          ) : (
            <>
              {' '}
              <Term k="spy">SPY</Term>
            </>
          )}
        </b>{' '}
        over {L.spec[1]} <Term k="sessions">trading days</Term> (<Term k="n">{nLabel(L.n)}</Term>).
      </>
    );
    const ci = L.ci;
    if (!ci) sub = 'The likely range is not available for this window.';
    else if (ci[1] < 0) sub = <>Consistent so far: the <Term k="ci">likely range</Term> ({xsPct(ci[0])} to {xsPct(ci[1])}) stays below zero.</>;
    else if (ci[0] > 0) sub = <>Consistent so far: the <Term k="ci">likely range</Term> ({xsPct(ci[0])} to {xsPct(ci[1])}) stays above zero.</>;
    else sub = <>Not proven yet: the <Term k="ci">likely range</Term> ({xsPct(ci[0])} to {xsPct(ci[1])}) still includes zero, so it could be luck.</>;
  }
  return (
    <SpotlightCard className="mui-crowdsc__lead" spotlightColor="rgba(74, 158, 255, 0.14)">
      <div className="mui-crowdsc__q">
        <span className="mui-crowdsc__question">Do stocks entering the list underperform SPY?</span>
        <span className={`mui-crowdsc__verdict is-${verdictKey}`} title="The backend's verdict on its 20-session test">
          <Term k="verdict">{verdict}</Term>
          {hyp && hyp.window ? ` · ${str(hyp.window).replace(/s$/, '')} sessions` : ''}
          {hyp ? ` · ${nLabel(hyp.n)}` : ''}
        </span>
      </div>
      <p className="mui-crowdsc__line">{line}</p>
      <p className="mui-crowdsc__sub">{sub}</p>
    </SpotlightCard>
  );
}

function Bar({ w, dom }) {
  const m = meanOf(w);
  const ci = ciOf(w);
  const x = v => Math.max(0, Math.min(100, ((v - dom[0]) / (dom[1] - dom[0])) * 100));
  const z = x(0);
  const xm = x(m);
  const left = Math.min(z, xm);
  const width = Math.abs(xm - z);
  const label = `average ${xsPct(m)} vs SPY, ${ciText(ci)}, ${nLabel(w.n)}`;
  return (
    <span className={`mui-crowdsc__track ${dirClass(m)}`} role="img" aria-label={label} title={label}>
      <span className="mui-crowdsc__zero" style={{ left: `${z}%` }} />
      {ci ? <span className="mui-crowdsc__band" style={{ left: `${x(ci[0])}%`, width: `${Math.max(0.6, x(ci[1]) - x(ci[0]))}%` }} /> : null}
      <motion.span
        className="mui-crowdsc__fill"
        style={{ left: `${left}%` }}
        initial={{ width: '0%' }}
        animate={{ width: `${width}%` }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
      />
      <span className="mui-crowdsc__dot" style={{ left: `${xm}%` }} />
    </span>
  );
}

function WindowRow({ spec, w, dom }) {
  const [win, , label, long] = spec;
  const n = isNum(w.n) ? w.n : 0;
  if (!usable(w)) {
    return (
      <div className="mui-crowdsc__row is-thin" data-window={win}>
        <span className="mui-crowdsc__h" data-cell="h" title={long}>
          {label}
        </span>
        <span className="mui-crowdsc__thin" data-cell="thin">
          not enough data yet ({nLabel(n)})
        </span>
      </div>
    );
  }
  const m = meanOf(w);
  const ci = ciOf(w);
  return (
    <div className="mui-crowdsc__row" data-window={win}>
      <span className="mui-crowdsc__h" data-cell="h" title={long}>
        {label}
      </span>
      <span className={`mui-crowdsc__mean ${dirClass(m)}`} data-cell="mean">
        {arrow(m)} {xsPct(m)}
      </span>
      <span className="mui-crowdsc__plot" data-cell="plot">
        <Bar w={w} dom={dom} />
      </span>
      <span className="mui-crowdsc__stats" data-cell="stats">
        <span className="mui-n mui-crowdsc__n">{nLabel(n)}</span>
        {isNum(w.hit_rate) ? <span className="mui-crowdsc__hit">beat SPY {pct(w.hit_rate * 100, { digits: 0, signed: false })}</span> : null}
        <span className="mui-crowdsc__ci-txt">{ciText(ci)}</span>
      </span>
    </div>
  );
}

function Axis({ dom }) {
  const z = ((0 - dom[0]) / (dom[1] - dom[0])) * 100;
  return (
    <div className="mui-crowdsc__row mui-crowdsc__axis" aria-hidden="true">
      <span className="mui-crowdsc__plot mui-crowdsc__axis-plot" data-cell="plot">
        <span className="mui-crowdsc__axis-lo">{xsPct(dom[0], 1)}</span>
        <span className="mui-crowdsc__axis-zero" style={{ left: `${z}%` }}>
          SPY
        </span>
        <span className="mui-crowdsc__axis-hi">{xsPct(dom[1], 1)}</span>
      </span>
    </div>
  );
}

function Buckets({ rows }) {
  const list = arr(rows).filter(b => b && typeof b === 'object');
  if (!list.length) return null;
  const nice = { top10: 'Top 10', '11-50': '#11–50', '51-100': '#51–100' };
  return (
    <div className="mui-crowdsc__buckets">
      <span className="mui-crowdsc__sub2">
        By <Term k="rank">rank</Term> when it joined{list[0].window ? ` (${str(list[0].window).replace(/s$/, '')} sessions)` : ''}:
      </span>
      {list.map((b, i) => {
        const m = meanOf(b);
        return (
          <span key={`${str(b.rank_bucket)}|${str(b.window)}|${i}`} className="mui-crowdsc__bucket">
            <b>{nice[str(b.rank_bucket)] || str(b.rank_bucket) || '—'}</b>{' '}
            {b.min_n_met === true ? (
              <span className={dirClass(m)}>
                {arrow(m)} {xsPct(m)}
              </span>
            ) : (
              <span className="mui-crowdsc__thin">not enough data yet</span>
            )}{' '}
            <span className="mui-n">{nLabel(b.n)}</span>
          </span>
        );
      })}
    </div>
  );
}

/** Best and worst graded examples on this side (the longest matured horizon each): clickable tickers. */
function Examples({ events, side }) {
  const picks = useMemo(() => {
    const out = [];
    for (const e of arr(events)) {
      const x = obj(e);
      if (x.kind !== side || typeof x.ticker !== 'string') continue;
      const f = obj(x.fwd);
      const h = ['60', '20', '5'].find(k => obj(f[k]).matured === true && isNum(obj(f[k]).xs_spy));
      if (h) out.push({ ticker: x.ticker, h, xs: obj(f[h]).xs_spy, date: x.date });
    }
    out.sort((a, b) => a.xs - b.xs);
    if (out.length < 2) return out;
    return [out[0], out[out.length - 1]];
  }, [events, side]);
  if (!picks.length) return null;
  const hl = { 5: '1 wk', 20: '1 mo', 60: '3 mo' };
  return (
    <div className="mui-crowdsc__examples">
      <span className="mui-crowdsc__sub2">Examples:</span>
      {picks.map((p, i) => (
        <button
          key={`${p.ticker}|${p.date}|${i}`}
          type="button"
          className={`mui-crowdsc__ex ${dirClass(p.xs)}`}
          onClick={() => emitPick(p.ticker)}
          title={`${p.ticker}: ${xsPct(p.xs)} vs SPY after ${hl[p.h]} — open its snapshot`}
        >
          <b>{p.ticker}</b> {arrow(p.xs)} {xsPct(p.xs)} <span className="mui-crowdsc__ex-h">{hl[p.h]}</span>
        </button>
      ))}
    </div>
  );
}

function Side({ side, s, dom, events }) {
  const enter = side === 'enter';
  // DEBUG_F2 F2-4: CountUp renders "-∞" for absurd values; only a sane, finite count reaches it.
  const nEv = isNum(s.n_events) && s.n_events >= 0 && s.n_events <= MAX_EVENTS ? Math.round(s.n_events) : null;
  const any = WINDOWS.some(([win]) => usable(obj(obj(s.windows)[win])));
  return (
    <div className="mui-crowdsc__side" data-side={side}>
      <div className="mui-crowdsc__side-head">
        <span className="mui-crowdsc__sub2">
          Return <Term k="spy">vs SPY</Term> after a stock{' '}
          <Term k={enter ? 'enter' : 'exit'}>{enter ? 'joined' : 'left'}</Term> the list
        </span>
        <span className="mui-crowdsc__events">
          {nEv == null ? '—' : <CountUp to={nEv} duration={1} />} {enter ? 'entry' : 'exit'} events
        </span>
      </div>
      <div className="mui-crowdsc__grid">
        {WINDOWS.map(spec => (
          <WindowRow key={spec[0]} spec={spec} w={obj(obj(s.windows)[spec[0]])} dom={dom} />
        ))}
        {any ? <Axis dom={dom} /> : null}
      </div>
      {enter ? <Buckets rows={s.by_bucket} /> : null}
      <Examples events={events} side={side} />
    </div>
  );
}

function Scorecard({ data, side, onSide }) {
  const sc = obj(data.scorecard);
  const enter = obj(sc.enter);
  const exit = obj(sc.exit);
  const dom = useMemo(() => domainOf([enter, exit]), [enter, exit]);
  const hyp = enter.hypothesis && typeof enter.hypothesis === 'object' ? enter.hypothesis : null;
  const onKey = ev => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const i = SIDES.findIndex(x => x.id === side);
    const next = SIDES[(i + step + SIDES.length) % SIDES.length].id;
    onSide(next);
    const g = ev.currentTarget;
    requestAnimationFrame(() => {
      const b = g.querySelector(`[data-tab="${next}"]`);
      if (b) b.focus();
    });
  };
  return (
    <div className="mui-crowdsc">
      <Lead sc={sc} />
      <div className="mui-crowdsc__tabs" role="radiogroup" aria-label="Which events" onKeyDown={onKey}>
        {SIDES.map(x => (
          <button
            key={x.id}
            type="button"
            role="radio"
            aria-checked={side === x.id}
            tabIndex={side === x.id ? 0 : -1}
            data-tab={x.id}
            className={`mui-crowd-pill ${side === x.id ? 'is-on' : ''}`}
            onClick={() => onSide(x.id)}
          >
            <span aria-hidden="true">{x.glyph}</span> {x.label}
          </button>
        ))}
      </div>
      <div className="mui-crowdsc__scroll">
        <Side side={side} s={side === 'exit' ? exit : enter} dom={dom} events={data.events} />
        {hyp && str(hyp.text) ? <p className="mui-crowdsc__hyp">{str(hyp.text)}</p> : null}
      </div>
    </div>
  );
}

export default function CrowdScorecard({ config = {}, saveConfig }) {
  const api = useCrowd();
  const [localSide, setLocalSide] = useState(null);
  const side = localSide || (config.side === 'exit' ? 'exit' : 'enter');
  const onSide = s => {
    const next = s === 'exit' ? 'exit' : 'enter';
    setLocalSide(next);
    if (typeof saveConfig === 'function') saveConfig({ ...config, side: next });
  };
  return (
    <div className="mui-crowd mui-crowd--scorecard">
      <CrowdState api={api} compact loadingRows={4}>
        {d => <Scorecard data={obj(d)} side={side} onSide={onSide} />}
      </CrowdState>
    </div>
  );
}
