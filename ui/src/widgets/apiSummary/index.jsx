// apiSummary: the API page's status summary (UI overhaul Task 6; replaces the old #mv-health-meta line
// "11 up · 1 down · …"). Headline, rolling counters per state, a health bar per source kind and the
// background-loop (runtime supervisor) status from /api/health background_jobs.
// A non-zero LIMIT / DOWN / NOT CHECKED tile is a button: it focuses the next source in that state in the
// constellation (mason:api-focus), so "what is down?" is one click.
// R3-C (2026-11-17): not-configured sources (optional, no key) are counted on their own, never as an issue:
// a calm dotted "◌ N not configured" line under the tiles (click → next one in the constellation), a dotted
// segment in the kind bars, and "x/y up" counts only the configured ones.
// APIGPU (2026-11-17): when the backend reports the local AI model (gpuhealth "local_model_gpu"), one line under
// the tiles says where it runs in words (▲ ON GPU / ☾ ASLEEP / ◆ ON CPU / ▼ NO GPU / ▼ DOWN) with its model,
// memory and temperature; click → the model in the constellation. On the processor the headline says so, and a
// missing card / Ollama down adds a note under "N links down". Nothing new shows until the backend sends it.
// APIAMBER (Nico, 2026-11-18): less than half of the model on the card reads ◆ MOSTLY CPU, amber, and leads the
// headline like on the processor ("Local AI running slow" + its own note). No code here: it all comes from health.js.
// React Bits used: Counter (rolling digits), DecryptedText (the headline, and the model's status word, decode
// when they change).
// Reflows with container queries (summary.css): 2×2 tiles when narrow, one row when wide, sections side by side.
import { useCallback } from 'react';
import Counter from '../../reactbits/Counter/Counter.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import { useHealth } from '../apiShared/useHealth.js';
import { CORE, useApiFocus } from '../apiShared/focus.js';
import {
  ageText,
  agoText,
  etClock,
  headline,
  jobBeatAge,
  lookOf,
  metaOf,
  modelFacts,
  NOT_CONFIGURED_HINT,
  POLL_MS,
  STATE_META,
  STATE_ORDER,
  STATES
} from '../apiShared/health.js';
import { JobChip, StaleNote, useNow } from '../apiShared/parts.jsx';
import { Loading } from '../../lib/states.jsx';
import '../apiShared/api.css';
import './summary.css';

const TILE_LABEL = { up: 'Up', limit: 'Limit hit', down: 'Down', unknown: 'Not checked' };

function Count({ n }) {
  const v = Number.isFinite(n) ? Math.max(0, Math.min(999, Math.round(n))) : 0;
  const places = v >= 100 ? [100, 10, 1] : v >= 10 ? [10, 1] : [1];
  return (
    <span className="apis-count">
      <span className="mui-sr">{v}</span>
      <span aria-hidden="true">
        <Counter
          value={v}
          places={places}
          fontSize={26}
          padding={2}
          gap={0}
          horizontalPadding={0}
          borderRadius={0}
          gradientHeight={0}
          gradientFrom="transparent"
          gradientTo="transparent"
          fontWeight={700}
        />
      </span>
    </span>
  );
}

function Fetched({ model }) {
  const now = useNow(1000);
  if (!model || !model.fetchedAt) return null;
  const a = Math.max(0, Math.round((now - model.fetchedAt) / 1000));
  return <span>updated {a < 5 ? 'just now' : `${ageText(a)} ago`}</span>;
}

/** APIGPU: the local AI model in one line: glyph + where it runs (words) + model, memory, temperature (only the
 *  numbers the backend sent). A button: shows (or hides) the model in the constellation. */
function ModelLine({ src, focused, onPick }) {
  // (REVIEW 2026-11-17: a state without its own title, e.g. "Not checked" before the first check, keeps its full stop)
  const m = metaOf(src);
  const facts = modelFacts(src).filter(f => f.k === 'model' || f.k === 'memory' || f.k === 'temp');
  return (
    <button
      type="button"
      className={`apis-ml apis-ml--${lookOf(src)} api-tone--${m.tone}`}
      aria-pressed={focused}
      onClick={onPick}
      title={`${m.title || `${m.label}.`} Click to show it in the constellation.`}
    >
      <span className="apis-ml__g" aria-hidden="true">
        {m.glyph}
      </span>
      <span className="apis-ml__name">Local AI</span>
      <span className="apis-ml__state">
        {/* DecryptedText scrambles while it decodes: screen readers get the plain long label instead */}
        <span className="mui-sr">{m.label}</span>
        <span aria-hidden="true">
          <DecryptedText key={m.short} text={m.short} animateOn="view" sequential speed={30} encryptedClassName="apis-enc" />
        </span>
      </span>
      {facts.map(f => (
        <span key={f.k} className={`apis-ml__f apis-ml__f--${f.k}${f.hot ? ' api-hot' : ''}`}>
          {f.text}
          {f.hot ? ' · hot' : ''}
        </span>
      ))}
    </button>
  );
}

function KindBar({ k }) {
  const parts = STATES.filter(st => k[st] > 0);
  const issues = ['limit', 'slow', 'down', 'unknown'].filter(st => k[st] > 0);
  const nc = k.unconfigured || 0;
  const active = k.total - nc;
  const aria = `${k.label}: ${k.up} of ${active} up${issues.map(st => `, ${k[st]} ${STATE_META[st].label.toLowerCase()}`).join('')}${
    nc ? `, ${nc} not configured` : ''
  }`;
  return (
    <div className="apis-kind" role="img" aria-label={aria}>
      <span className="apis-kind__name">{k.label}</span>
      <span className="apis-kind__bar" aria-hidden="true">
        {parts.map(st => (
          <span key={st} className={`apis-kind__seg apis-kind__seg--${st} api-tone--${STATE_META[st].tone}`} style={{ flexGrow: k[st] }} />
        ))}
      </span>
      <span className="apis-kind__n" aria-hidden="true">
        {k.up}/{active}
        {issues.map(st => (
          <span key={st} className={`apis-kind__iss api-tone--${STATE_META[st].tone}`}>
            {' '}
            {STATE_META[st].glyph}
            {k[st]}
          </span>
        ))}
        {nc ? (
          <span className="apis-kind__nc api-tone--off" title={`${nc} not configured (optional)`}>
            {' '}
            {STATE_META.unconfigured.glyph}
            {nc}
          </span>
        ) : null}
      </span>
    </div>
  );
}

function Loops({ model }) {
  const now = useNow(5000);
  const j = model && model.jobs;
  if (!j) return <p className="apis-none">No background-loop data in this snapshot.</p>;
  if (j.error && !j.list.length) return <p className="apis-none">Runtime supervisor: {j.error}</p>;
  const rc = j.reconcile;
  return (
    <>
      <div className="apis-loops__line">
        <span className="api-tone--up apis-tone">● {j.counts.ok} OK</span>
        {j.counts.off ? <span className="api-tone--idle apis-tone">○ {j.counts.off} off</span> : null}
        {j.counts.warn ? <span className="api-tone--warn apis-tone">◆ {j.counts.warn} stale</span> : null}
        {j.counts.bad ? <span className="api-tone--down apis-tone">▼ {j.counts.bad} stopped</span> : null}
        <span className="apis-of">of {j.list.length}</span>
      </div>
      <div className="apis-loops__line apis-loops__meta">
        <span>
          supervisor{' '}
          {j.supervisorRunning === true ? (
            <b className="api-tone--up apis-tone">✓ running</b>
          ) : j.supervisorRunning === false ? (
            <b className="api-tone--down apis-tone">✕ not running</b>
          ) : (
            '—'
          )}
        </span>
        {rc ? (
          <span>
            reconcile{' '}
            <b className={`apis-tone api-tone--${rc.ok === false ? 'down' : rc.ok ? 'up' : 'idle'}`}>
              {rc.ok === false ? '✕ failed' : rc.ok ? '✓ ok' : '—'}
            </b>
            {rc.at ? ` ${etClock(rc.at)}` : ''}
            {rc.fillsSeen != null ? ` · ${rc.fillsSeen} fills` : ''}
          </span>
        ) : null}
      </div>
      {j.problems.length ? (
        <ul className="apis-problems" aria-label="Loops that need attention">
          {j.problems.map(job => {
            const age = jobBeatAge(model, job, now);
            return (
              <li key={job.name}>
                <JobChip status={job.status} />
                <span className="apis-problems__name" title={job.label}>
                  {job.name}
                </span>
                <span className="apis-problems__age">{age == null ? 'no beat' : `beat ${agoText(age)}`}</span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </>
  );
}

export default function ApiSummary() {
  const api = useHealth();
  const { model, status, failed, error, refresh } = api;
  const [focus, setFocus] = useApiFocus('summary');

  const jump = useCallback(
    st => {
      if (!model) return;
      const list = model.sources.filter(s => s.state === st);
      if (!list.length) return;
      const cur = list.findIndex(s => s.name === focus);
      setFocus(list[(cur + 1) % list.length].name);
    },
    [model, focus, setFocus]
  );

  if (status === 'loading' && !model) return <Loading rows={4} label="Loading source health…" />;
  const hl = headline(model, { failed, status });
  const c = model ? model.counts : { up: 0, limit: 0, down: 0, unknown: 0, unconfigured: 0, slow: 0, total: 0, active: 0 };
  const ncList = model ? model.sources.filter(s => s.state === 'unconfigured') : [];
  const lm = model ? model.localModel : null; // APIGPU: null until the backend reports the local model

  return (
    <div className="apis">
      <div className={`apis-head api-tone--${hl.tone}`}>
        <span className="apis-glyph" aria-hidden="true">
          {hl.glyph}
        </span>
        <div className="apis-hl">
          <h4 className="apis-headline">
            {/* DecryptedText exposes no stable accessible text (scrambled while it animates): a plain copy reads */}
            <span className="mui-sr" aria-live="polite">
              {hl.text}
            </span>
            <span aria-hidden="true">
              <DecryptedText key={hl.text} text={hl.text} animateOn="view" sequential speed={22} encryptedClassName="apis-enc" />
            </span>
          </h4>
          {(hl.notes || []).map(n => (
            <div key={n.text} className={`apis-hl__sub apis-hl__note api-tone--${n.tone}`}>
              <span aria-hidden="true">{n.glyph}</span> {n.text}
            </div>
          ))}
          {hl.sub ? <div className="apis-hl__sub api-tone--warn">◆ {hl.sub}</div> : null}
        </div>
        <button type="button" className="apis-refresh" onClick={() => refresh()} title="Refresh now" aria-label="Refresh source health now">
          ↻
        </button>
      </div>
      <div className="apis-sub">
        {model ? <span>{c.total} sources</span> : null}
        <Fetched model={model} />
        <span>every {Math.round(POLL_MS / 1000)} s</span>
        <StaleNote failed={failed} error={error} model={model} now={Date.now()} />
        {!model && status === 'error' ? <span className="api-tone--down apis-tone">{String(error || 'unreachable')}</span> : null}
        {model && model.monitorNote ? <span>{model.monitorNote}</span> : null}
      </div>
      <div className="apis-tiles">
        {STATE_ORDER.map(st => {
          const m = STATE_META[st];
          const n = c[st] || 0;
          const act = st !== 'up' && n > 0;
          const body = (
            <>
              <span className="apis-tile__g" aria-hidden="true">
                {m.glyph}
              </span>
              <Count n={n} />
              <span className="apis-tile__l">{TILE_LABEL[st]}</span>
            </>
          );
          return act ? (
            <button
              key={st}
              type="button"
              className={`apis-tile api-tone--${m.tone} is-act${n ? '' : ' is-zero'}`}
              onClick={() => jump(st)}
              title={`Show the ${n === 1 ? '' : 'next '}source that is ${m.label.toLowerCase()} in the constellation`}
            >
              {body}
            </button>
          ) : (
            <div key={st} className={`apis-tile api-tone--${m.tone}${n ? '' : ' is-zero'}`}>
              {body}
            </div>
          );
        })}
      </div>
      {lm ? <ModelLine src={lm} focused={focus === lm.name} onPick={() => setFocus(focus === lm.name ? null : lm.name)} /> : null}
      {ncList.length ? (
        <button
          type="button"
          className="apis-nc api-tone--off"
          onClick={() => jump('unconfigured')}
          title={`${NOT_CONFIGURED_HINT}. Optional, not an issue. Click to show ${ncList.length === 1 ? 'it' : 'the next one'} in the constellation.`}
        >
          <span className="apis-nc__g" aria-hidden="true">
            {STATE_META.unconfigured.glyph}
          </span>
          <b>{ncList.length}</b> <span className="apis-nc__l">not configured</span>{' '}
          <span className="apis-nc__names">{ncList.map(s => s.label).join(', ')}</span>{' '}
          <span className="apis-nc__hint">add a key to enable</span>
        </button>
      ) : null}
      <div className="apis-cols">
        <section className="apis-sec" aria-label="Health by kind">
          <h5 className="apis-h">By kind</h5>
          {model && model.byKind.length ? model.byKind.map(k => <KindBar key={k.kind} k={k} />) : <p className="apis-none">No sources yet.</p>}
        </section>
        <section className="apis-sec" aria-label="Background loops">
          <h5 className="apis-h">
            Background loops
            <button type="button" className="apis-link" onClick={() => setFocus(focus === CORE ? null : CORE)}>
              details
            </button>
          </h5>
          <Loops model={model} />
        </section>
      </div>
    </div>
  );
}
