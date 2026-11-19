// crowdExplainer — "What is the crowd?" (page-only, #crowd). UI overhaul Task 4 (2026-11-16).
// A four-step illustrated tour (React Bits Stepper): who the crowd is, what joining ◆ / leaving ◇ the list means,
// why Mason grades the crowd against SPY, and how sure those grades are (n and the likely range). Each step is a
// small animated drawing plus one sentence; the drawings use the LIVE list when it is there (today's top 3,
// this month's joiners, the real 1-month result), and say "example" when they fall back to made-up numbers.
// Dismiss ("Got it" on the last step, or "Hide") is remembered in this widget's page config and also folds the
// widget to its header (the template's own collapse button, so the grid remembers it too). Folded or not, the
// dismissed card shows a one-glance cheat sheet with glossary tips and a "Replay the tour" button.
// React Bits: Stepper (themed through CSS + renderStepIndicator; no source change). The drawings are ours
// (inline SVG + motion, one-shot on each step: nothing loops, so the page's motion budget is untouched).
import { useCallback, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import Stepper, { Step } from '../../reactbits/Stepper/Stepper.jsx';
import { arrow, dirClass, isNum, nLabel } from '../../lib/fmt.js';
import { absPct, arr, obj, recentExits, todayRows, useCrowd, xsPct } from '../crowdShared/data.js';
import { Term } from '../crowdShared/ui.jsx';

const ease = [0.22, 1, 0.36, 1];

/** Live bits for the drawings (or example values when the list isn't there yet). */
function useLive() {
  const api = useCrowd();
  const d = api.status === 'ok' && api.data && api.data.status !== 'blocked_needs_follow' ? obj(api.data) : null;
  return useMemo(() => {
    const rows = d ? todayRows(d, 5) : [];
    const top = rows.slice(0, 3).map(r => r.ticker);
    const joined = arr(d && d.events).find(e => obj(e).kind === 'enter' && typeof obj(e).ticker === 'string');
    const left = d ? recentExits(d, 60)[0] : null;
    const w = obj(obj(obj(obj(d).scorecard).enter).windows)['20s'];
    const real = !!(w && w.min_n_met === true && isNum(w.mean_xs));
    const ci = Array.isArray(obj(w).ci95) && obj(w).ci95.length === 2 && obj(w).ci95.every(isNum) ? obj(w).ci95 : null;
    return {
      top: top.length === 3 ? top : ['AMZN', 'NVDA', 'AAPL'],
      topLive: top.length === 3,
      joined: joined ? { ticker: obj(joined).ticker, rank: obj(joined).rank } : { ticker: 'RKLB', rank: 34 },
      joinedLive: !!joined,
      left: left ? { ticker: left.ticker, rank: left.rank } : { ticker: 'GME', rank: 88 },
      leftLive: !!left,
      mean: real ? w.mean_xs : -0.019,
      ci: real && ci ? [Math.min(...ci), Math.max(...ci)] : [-0.04, 0.002],
      n: real && isNum(w.n) ? w.n : 11,
      real
    };
  }, [d]);
}

// ── drawings (viewBox 240 × 128; they scale to the box) ──────────────────────────────────────────────
const PEOPLE = Array.from({ length: 21 }, (_, i) => ({ x: 14 + (i % 7) * 13 + (Math.floor(i / 7) % 2) * 6, y: 30 + Math.floor(i / 7) * 26 }));

function Person({ x, y, i }) {
  return (
    <motion.g
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: 0.03 * i, duration: 0.35, ease }}
      className="mui-crowdx__person"
    >
      <circle cx={x} cy={y} r="3.4" />
      <path d={`M${x - 5} ${y + 11} q5 -8 10 0`} />
    </motion.g>
  );
}

function CrowdArt({ live }) {
  const bars = [
    { w: 92, t: live.top[0] },
    { w: 78, t: live.top[1] },
    { w: 66, t: live.top[2] }
  ];
  return (
    <svg viewBox="0 0 240 128" className="mui-crowdx__art" role="img" aria-label={`Many investors feed one ranked list: #1 ${bars[0].t}, #2 ${bars[1].t}, #3 ${bars[2].t}`}>
      {PEOPLE.map((p, i) => (
        <Person key={i} x={p.x} y={p.y} i={i} />
      ))}
      <motion.path
        d="M108 64 C 122 64, 124 64, 134 64"
        className="mui-crowdx__flow"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ delay: 0.55, duration: 0.4 }}
      />
      <motion.path d="M130 59 l6 5 l-6 5" className="mui-crowdx__flow" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.9 }} />
      {bars.map((b, i) => (
        <g key={b.t}>
          <text x="146" y={35 + i * 30} className="mui-crowdx__rank">
            #{i + 1}
          </text>
          <motion.rect
            x="164"
            y={24 + i * 30}
            height="16"
            rx="5"
            className={`mui-crowdx__bar is-${i}`}
            initial={{ width: 0 }}
            animate={{ width: b.w * 0.75 }}
            transition={{ delay: 0.7 + i * 0.12, duration: 0.5, ease }}
          />
          <motion.text
            x="170"
            y={36 + i * 30}
            className="mui-crowdx__tk"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 1.05 + i * 0.12 }}
          >
            {b.t}
          </motion.text>
        </g>
      ))}
      <text x="146" y="118" className="mui-crowdx__cap">
        {live.topLive ? 'today’s top 3' : 'example'} · … #100
      </text>
    </svg>
  );
}

function JoinLeaveArt({ live }) {
  const rows = [0, 1, 2, 3];
  return (
    <svg viewBox="0 0 240 128" className="mui-crowdx__art" role="img" aria-label={`${live.joined.ticker} joins the list; ${live.left.ticker} leaves it`}>
      {rows.map(i => (
        <rect key={i} x="70" y={14 + i * 24} width="100" height="18" rx="5" className="mui-crowdx__slot" />
      ))}
      <motion.g initial={{ x: 70, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.2, duration: 0.6, ease }}>
        <rect x="70" y="38" width="100" height="18" rx="5" className="mui-crowdx__chip is-in" />
        <text x="80" y="51" className="mui-crowdx__tk">
          ◆ {live.joined.ticker}
        </text>
      </motion.g>
      <motion.text x="178" y="51" className="mui-crowdx__lbl is-in" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.8 }}>
        joined{isNum(live.joined.rank) ? ` #${live.joined.rank}` : ''}
      </motion.text>
      <motion.g initial={{ x: 0, opacity: 1 }} animate={{ x: -62, opacity: 0.35 }} transition={{ delay: 1.0, duration: 0.7, ease }}>
        <rect x="70" y="86" width="100" height="18" rx="5" className="mui-crowdx__chip is-out" />
        <text x="80" y="99" className="mui-crowdx__tk">
          ◇ {live.left.ticker}
        </text>
      </motion.g>
      <motion.text x="178" y="99" className="mui-crowdx__lbl is-out" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 1.5 }}>
        left (2 days gone)
      </motion.text>
      <text x="70" y="124" className="mui-crowdx__cap">
        {live.joinedLive || live.leftLive ? 'recent real moves' : 'example'}
      </text>
    </svg>
  );
}

function VsSpyArt({ live }) {
  // zero (SPY) at x=120; ±5% spans ±96 px
  const sx = v => Math.max(-100, Math.min(100, (v / 0.05) * 96));
  const w = sx(live.mean);
  return (
    <svg viewBox="0 0 240 128" className="mui-crowdx__art" role="img" aria-label={`Crowd joiners vs SPY after 1 month: ${xsPct(live.mean)}`}>
      <line x1="120" x2="120" y1="16" y2="104" className="mui-crowdx__zero" />
      <text x="120" y="12" textAnchor="middle" className="mui-crowdx__cap">
        SPY = the market
      </text>
      <rect x="20" y="40" width="200" height="22" rx="6" className="mui-crowdx__slot" />
      <motion.rect
        y="40"
        height="22"
        rx="6"
        className={`mui-crowdx__xs ${dirClass(live.mean)}`}
        initial={{ x: 120, width: 0 }}
        animate={{ x: w < 0 ? 120 + w : 120, width: Math.abs(w) }}
        transition={{ delay: 0.35, duration: 0.8, ease }}
      />
      <text x="20" y="34" className="mui-crowdx__lbl">
        crowd’s new picks, 1 month later
      </text>
      <motion.text
        x={w < 0 ? 120 + w - 4 : 120 + w + 4}
        y="56"
        textAnchor={w < 0 ? 'end' : 'start'}
        className={`mui-crowdx__val ${dirClass(live.mean)}`}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.1 }}
      >
        {arrow(live.mean)} {absPct(live.mean)}
      </motion.text>
      <text x="20" y="86" className="mui-crowdx__cap">
        ◀ lagged the market
      </text>
      <text x="220" y="86" textAnchor="end" className="mui-crowdx__cap">
        beat the market ▶
      </text>
      <text x="120" y="122" textAnchor="middle" className="mui-crowdx__cap">
        {live.real ? 'Mason’s real result so far' : 'example numbers'}
      </text>
    </svg>
  );
}

function HowSureArt({ live }) {
  const sx = v => 120 + Math.max(-100, Math.min(100, (v / 0.06) * 96));
  const dots = Math.max(1, Math.min(30, Math.round(live.n)));
  const spread = (live.ci[1] - live.ci[0]) * 1.6 || 0.02;
  return (
    <svg viewBox="0 0 240 128" className="mui-crowdx__art" role="img" aria-label={`Average ${xsPct(live.mean)}, likely range ${xsPct(live.ci[0])} to ${xsPct(live.ci[1])}, ${nLabel(live.n)}`}>
      <line x1="16" x2="224" y1="70" y2="70" className="mui-crowdx__axis" />
      <line x1="120" x2="120" y1="40" y2="84" className="mui-crowdx__zero" />
      <text x="120" y="98" textAnchor="middle" className="mui-crowdx__cap">
        0 = same as SPY
      </text>
      {Array.from({ length: dots }, (_, i) => {
        const v = live.mean + (((i * 7919) % 97) / 96 - 0.5) * spread;
        return (
          <motion.circle
            key={i}
            cx={sx(v)}
            cy={24 + ((i * 37) % 3) * 6}
            r="2.6"
            className="mui-crowdx__sample"
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.04 * i, duration: 0.3 }}
          />
        );
      })}
      <motion.rect
        y="63"
        height="14"
        rx="7"
        className="mui-crowdx__band"
        initial={{ x: sx(live.mean), width: 0 }}
        animate={{ x: sx(live.ci[0]), width: Math.max(2, sx(live.ci[1]) - sx(live.ci[0])) }}
        transition={{ delay: 0.9, duration: 0.6, ease }}
      />
      <motion.circle cx={sx(live.mean)} cy="70" r="5" className={`mui-crowdx__mean ${dirClass(live.mean)}`} initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ delay: 1.3 }} />
      <text x="16" y="120" className="mui-crowdx__cap">
        {nLabel(live.n)} dots · band = likely range{live.real ? '' : ' (example)'}
      </text>
    </svg>
  );
}

const STEPS = [
  {
    id: 'who',
    title: 'Who is “the crowd”?',
    Art: CrowdArt,
    body: (
      <>
        Robinhood publishes the <Term k="popular">100 Most Popular</Term>: the stocks most of its customers own. Mason
        saves it every trading day. That list is the crowd.
      </>
    )
  },
  {
    id: 'moves',
    title: 'Joining ◆ & leaving ◇',
    Art: JoinLeaveArt,
    body: (
      <>
        A stock that shows up on the list has <Term k="enter">joined</Term>; one missing two days in a row has{' '}
        <Term k="exit">left</Term>.
      </>
    )
  },
  {
    id: 'spy',
    title: 'Does following the crowd pay?',
    Art: VsSpyArt,
    body: (
      <>
        After a stock joins, Mason checks how it did <Term k="spy">vs SPY</Term> (the whole market) 1 week, 1 month and 3
        months later.
      </>
    )
  },
  {
    id: 'sure',
    title: 'How sure is that?',
    Art: HowSureArt,
    body: (
      <>
        <Term k="n">n</Term> = how many examples. The band is the <Term k="ci">likely range</Term>: if it crosses 0, it
        could still be luck.
      </>
    )
  }
];

function StepBody({ s, live }) {
  const Art = s.Art;
  return (
    <div className="mui-crowdx__step" data-step={s.id}>
      <div className="mui-crowdx__art-wrap">
        <Art live={live} />
      </div>
      <div className="mui-crowdx__text">
        <h4 className="mui-crowdx__title">{s.title}</h4>
        <p className="mui-crowdx__body">{s.body}</p>
      </div>
    </div>
  );
}

function CheatSheet({ onReplay }) {
  return (
    <div className="mui-crowdx__cheat">
      <ul className="mui-crowdx__tiles">
        <li className="mui-crowdx__tile">
          <span className="mui-crowdx__ico" aria-hidden="true">
            #1
          </span>
          <span>
            The crowd = Robinhood’s <Term k="popular">100 Most Popular</Term>
          </span>
        </li>
        <li className="mui-crowdx__tile">
          <span className="mui-crowdx__ico" aria-hidden="true">
            ◆◇
          </span>
          <span>
            <Term k="enter">Joined</Term> / <Term k="exit">left</Term> the list
          </span>
        </li>
        <li className="mui-crowdx__tile">
          <span className="mui-crowdx__ico" aria-hidden="true">
            ▲▼
          </span>
          <span>
            Graded <Term k="spy">vs SPY</Term> after 1 wk / 1 mo / 3 mo
          </span>
        </li>
        <li className="mui-crowdx__tile">
          <span className="mui-crowdx__ico" aria-hidden="true">
            ±
          </span>
          <span>
            <Term k="n">n</Term> + <Term k="ci">likely range</Term> = how sure
          </span>
        </li>
      </ul>
      <button type="button" className="mui-crowd-pill mui-crowdx__replay" onClick={onReplay}>
        ↻ Replay the tour
      </button>
    </div>
  );
}

/** Fold the enclosing template widget to its header (the grid's own collapse button: the grid persists it). */
function foldWidget(el) {
  const node = el && el.closest ? el.closest('.m-widget') : null;
  if (!node || node.classList.contains('collapsed')) return;
  const head = Array.from(node.children).find(c => c.classList && c.classList.contains('m-widget-head'));
  const btn = head ? head.querySelector('.m-widget-collapse') : null;
  if (btn) btn.click();
}

export default function CrowdExplainer({ config = {}, saveConfig }) {
  const live = useLive();
  const root = useRef(null);
  const [dismissedLocal, setDismissedLocal] = useState(null);
  const dismissed = dismissedLocal == null ? config.dismissed === true : dismissedLocal;
  const [touring, setTouring] = useState(false);
  const [step, setStep] = useState(1);
  const [tourKey, setTourKey] = useState(0);

  const dismiss = useCallback(() => {
    setDismissedLocal(true);
    setTouring(false);
    if (typeof saveConfig === 'function' && config.dismissed !== true) saveConfig({ ...config, dismissed: true });
    foldWidget(root.current);
  }, [config, saveConfig]);
  const replay = () => {
    setStep(1);
    setTourKey(k => k + 1);
    setTouring(true);
  };
  const showTour = !dismissed || touring;
  const last = step === STEPS.length;

  return (
    <div className="mui-crowd mui-crowdx" ref={root} data-last={last ? '1' : '0'} data-mode={showTour ? 'tour' : 'cheat'}>
      <div className="mui-crowdx__top">
        <span className="mui-crowdx__kicker">
          {showTour ? `Step ${Math.min(step, STEPS.length)} of ${STEPS.length}` : 'Cheat sheet'}
        </span>
        {showTour ? (
          <button type="button" className="mui-crowdx__hide" onClick={dismiss} title="Hide this card (it stays hidden; replay it any time)">
            Hide ✕
          </button>
        ) : null}
      </div>
      {showTour ? (
        <Stepper
          key={tourKey}
          initialStep={1}
          onStepChange={setStep}
          onFinalStepCompleted={dismiss}
          backButtonText="Back"
          nextButtonText="Next"
          stepCircleContainerClassName="mui-crowdx__card"
          stepContainerClassName="mui-crowdx__dots"
          contentClassName="mui-crowdx__content"
          footerClassName="mui-crowdx__footer"
          backButtonProps={{ type: 'button' }}
          nextButtonProps={{ type: 'button', 'aria-label': last ? 'Got it: hide this card' : 'Next step' }}
          renderStepIndicator={({ step: s, currentStep, onStepClick }) => (
            <button
              type="button"
              className={`mui-crowdx__dot ${s === currentStep ? 'is-on' : s < currentStep ? 'is-done' : ''}`}
              aria-label={`Step ${s}: ${STEPS[s - 1].title}`}
              aria-current={s === currentStep ? 'step' : undefined}
              onClick={() => onStepClick(s)}
            >
              {s}
            </button>
          )}
        >
          {STEPS.map(s => (
            <Step key={s.id}>
              <StepBody s={s} live={live} />
            </Step>
          ))}
        </Stepper>
      ) : (
        <CheatSheet onReplay={replay} />
      )}
    </div>
  );
}
