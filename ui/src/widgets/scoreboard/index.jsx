// Scoreboard: how Mason's own trades are doing against the S&P 500, from GET /api/learning/scoreboard
// (contract: docs/fable/SCOREBOARD_API.md). Read-only; loads on mount and every 10 minutes (useScoreboard.js).
// Top to bottom: a bar (ahead / behind, trade counts), the three headline sentences, the running-total
// chart (Mason vs the same money in the S&P 500), the periods table, each trade against the S&P 500,
// the target sentence, the notes, then "What kinds of calls have worked" (collapsed by default), and a
// footer (closing date, cost per trade, when the figures were built).
//
// The server's sentences (headline, target, notes, table titles and notes) are shown exactly as given,
// never rebuilt from the numbers. Every value can be null: it prints as a dash.
//
// React Bits: SpotlightCard (the headline and chart cards), CountUp (the trade / day counts).
// Built here because React Bits has no such piece: the chart (no chart component; MasonChart cannot
// draw two given series, see parts.jsx), the tables (no table), the collapsible section (no
// disclosure; AccordionGallery is an image gallery), the verdict labels (no badge).
import { useCallback, useState } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { DASH, isNum, pct } from '../../lib/fmt.js';
import { Loading } from '../../lib/states.jsx';
import { builtAt, longDay, standing } from './sbUtil.js';
import { useScoreboard } from './useScoreboard.js';
import { IdeasTable, PeriodsTable, RunningChart, WhatWorked } from './parts.jsx';
import './scoreboard.css';

const SPOT = 'rgba(74, 158, 255, 0.12)';
const OPEN_KEY = 'mui.scoreboard.whatWorkedOpen';
export const UNAVAILABLE = 'The scoreboard isn’t available right now.';

function store() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
function loadOpen() {
  try {
    const s = store();
    return !!s && s.getItem(OPEN_KEY) === '1';
  } catch {
    return false;
  }
}
function saveOpen(v) {
  try {
    const s = store();
    if (s) s.setItem(OPEN_KEY, v ? '1' : '0');
  } catch {
    /* storage full or blocked: the section just opens closed next time */
  }
}
const clockNow = ms => new Date(ms).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }).toLowerCase();

function Count({ value, word, plural }) {
  if (!isNum(value)) {
    return (
      <span>
        <b>{DASH}</b> {plural}
      </span>
    );
  }
  return (
    <span>
      <b>
        <CountUp to={value} duration={0.6} separator="," />
      </b>{' '}
      {value === 1 ? word : plural}
    </span>
  );
}

function Standing({ beating }) {
  const s = standing(beating);
  return (
    <span className="mui-sb__standing" data-tone={s.tone} data-part="standing">
      {s.mark ? <span aria-hidden="true">{s.mark}</span> : null}
      {s.text}
    </span>
  );
}

export function ScoreboardView({ sb, open, onToggle }) {
  const d = sb.data;
  return (
    <div className="mui-sb" data-status={sb.status}>
      {sb.status === 'loading' ? <Loading rows={5} label="Loading the scoreboard…" /> : null}
      {sb.status === 'error' ? (
        <div className="mui-sb__msg" role="status" data-part="error">
          <span className="mui-sb__msgicon" aria-hidden="true">
            !
          </span>
          <p>
            {UNAVAILABLE}
            <span className="mui-sb__sub">Mason will try again in a minute.</span>
          </p>
        </div>
      ) : null}
      {sb.status === 'ok' && d ? (
        <>
          <div className="mui-sb__bar">
            <Standing beating={d.target.beating} />
            <span className="mui-sb__counts" data-part="counts">
              <Count value={d.own.nTrades} word="trade" plural="trades" />
              <span aria-hidden="true">·</span>
              <Count value={d.own.nClosed} word="finished" plural="finished" />
              <span aria-hidden="true">·</span>
              <Count value={d.own.nOpen} word="open" plural="open" />
              <span aria-hidden="true">·</span>
              <Count value={d.own.daysRunning} word="day" plural="days" />
            </span>
          </div>

          <SpotlightCard className="mui-sb__card mui-sb__headline" spotlightColor={SPOT}>
            <p className="mui-sb__line1" data-part="line1">
              {d.headline.line1 || DASH}
            </p>
            {d.headline.line2 ? (
              <p className="mui-sb__line2" data-part="line2">
                {d.headline.line2}
              </p>
            ) : null}
            {d.headline.means ? (
              <p className="mui-sb__means" data-part="means">
                {d.headline.means}
              </p>
            ) : null}
          </SpotlightCard>

          <SpotlightCard className="mui-sb__card" spotlightColor={SPOT}>
            <h3 className="mui-sb__h">Running total since the first trade, after costs</h3>
            <RunningChart series={d.own.series} />
          </SpotlightCard>

          <section className="mui-sb__sec" aria-label="Mason against the S&P 500, by period">
            <h3 className="mui-sb__h">Mason against the S&amp;P 500</h3>
            <PeriodsTable periods={d.own.periods} asOf={d.asOf} />
          </section>

          {d.own.perTrade ? (
            <section className="mui-sb__sec" data-part="per-trade">
              <h3 className="mui-sb__h">Each trade against the S&amp;P 500 over the same days</h3>
              <IdeasTable rows={[d.own.perTrade]} label="Each trade against the S&P 500" countWord="Trades" />
            </section>
          ) : null}

          {d.target.text ? (
            <p className="mui-sb__target" data-part="target">
              {d.target.text}
            </p>
          ) : null}

          {d.own.notes.length ? (
            <ul className="mui-sb__notes" data-part="notes" aria-label="Notes">
              {d.own.notes.map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ul>
          ) : null}

          <WhatWorked worked={d.worked} open={open} onToggle={onToggle} />

          <p className="mui-sb__foot" data-part="foot">
            <span>Closing prices up to {longDay(d.asOf)}</span>
            {isNum(d.costPct) ? <span>trading cost {pct(d.costPct, { digits: 2, signed: false })} a trade</span> : null}
            {builtAt(d.generatedAt) ? <span>figures built {builtAt(d.generatedAt)}</span> : null}
            {sb.failed ? (
              <span className="mui-sb__warn" role="status" data-foot="failed">
                ⚠ Could not refresh. Showing what loaded at {clockNow(sb.at)}; trying again in a minute.
              </span>
            ) : null}
          </p>
        </>
      ) : null}
    </div>
  );
}

export default function Scoreboard() {
  const sb = useScoreboard();
  const [open, setOpen] = useState(loadOpen);
  const onToggle = useCallback(() => {
    setOpen(v => {
      saveOpen(!v);
      return !v;
    });
  }, []);
  return <ScoreboardView sb={sb} open={open} onToggle={onToggle} />;
}
