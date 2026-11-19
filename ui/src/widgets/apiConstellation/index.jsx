// apiConstellation: Mason's data links as a network constellation (UI overhaul Task 6, Nico 2026-11-16).
// Mason's core sits in the centre, ringed by its background loops (/api/health background_jobs). Every
// source the health monitor probes orbits it as a node. Data pulses run along the links, coloured by
// health: green = up, amber = limit hit (packets queue at a throttle gate), red = down (the link
// flickers and sparks die half-way), grey dashed = not checked yet, faint dotted with a hollow ◌ node and no
// pulses = not configured (an optional source with no key; R3-C 2026-11-17). Pulse speed follows the probe's latency
// and pulse brightness follows how fresh the last check is.
// APIGPU (2026-11-17): the local AI model (gpuhealth source "local_model_gpu") orbits next to Ollama with a memory
// ring that fills with the share of the model held on the graphics card. On the card = the normal healthy look;
// asleep = a calm blue hollow ☾ that breathes slowly, no pulses; on the processor = amber pulses that crawl ~10×
// slower; card missing / Ollama down = the normal DOWN look. Its tooltip and card show the model, memory,
// temperature and power (only the numbers the backend sent) and the backend's plain sentence.
// APIAMBER (Nico, 2026-11-18): less than half of the model on the card (◆ MOSTLY CPU) takes the same amber look as on
// the processor; the legend's hover text says which of the two it is (health.js slowWords).
// Hover (or Tab to) a node for its details; click it (Enter) to focus it and open the full detail card;
// Esc / × / clicking empty space closes it. The source list widget follows the focus (mason:api-focus).
//
// React Bits used: SpotlightCard (the detail card), ElectricBorder (around the card when the focused link
// is DOWN; at most one on screen), DecryptedText (the card title decodes on focus), CountUp (latency; the
// model's temperature and power draw).
// The constellation itself is built from scratch (engine.js, canvas + real <button> nodes) because no React
// Bits piece draws a node-link graph. The closest, Animations/OrbitImages, moves images around a fixed
// square design along a CSS offset-path. It has no links or per-link data, does not lay out to the widget's
// aspect ratio, and cannot keep labels apart. Its separate motion loop would also be a second continuous
// animation next to the pulse canvas (bundle §5 allows two per page).
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import ElectricBorder from '../../reactbits/ElectricBorder/ElectricBorder.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import { useDocumentVisible, useOnScreen } from '../../lib/visibility.js';
import { useHealth } from '../apiShared/useHealth.js';
import { CORE, useApiFocus } from '../apiShared/focus.js';
import {
  ageOf,
  ageText,
  agoText,
  etClock,
  everyText,
  inStateLabel,
  jobBeatAge,
  JOB_META,
  latencyText,
  UNDER_1_MS,
  underOneMs,
  lookOf,
  metaOf,
  modelFacts,
  modelHint,
  NOT_CONFIGURED_HINT,
  pulseSeconds,
  slowPulseSeconds,
  slowWords
} from '../apiShared/health.js';
import { JobChip, Meter, StaleNote, StateChip, useNow } from '../apiShared/parts.jsx';
import { createEngine } from './engine.js';
import '../apiShared/api.css';
import './constellation.css';

// UIFIX 2026-11-18: what the local AI model's latency reading is (mason_health.py times one Ollama /api/ps reply)
const MODEL_LATENCY_NOTE = " · Ollama's status reply, not the model's answer speed";
const DOCK_RIGHT_MIN = 600; // px of widget width from which the detail card docks on the right
const LEGEND = [
  ['up', '▲', 'up'],
  ['warn', '◆', 'limit'],
  ['down', '▼', 'down'],
  ['idle', '○', 'not checked']
];
// only listed while a source is in that state (keeps the bar short); same glyph/tone as STATE_META.unconfigured
const LEGEND_NC = ['off', '◌', 'not configured'];
// APIGPU: only while the local AI model is asleep (same glyph/tone as MODEL_META.idle); while it is on the
// processor (or mostly on it, APIAMBER) the ◆ entry reads "limit / slow"
const LEGEND_ASLEEP = ['calm', '☾', 'asleep'];

function pulseLine(src) {
  const look = lookOf(src);
  if (look === 'up') {
    const s = pulseSeconds(src.latencyMs);
    return src.latencyMs != null ? `${s.toFixed(1)} s per pulse · ${latencyText(src.latencyMs)} probe` : `${s.toFixed(1)} s per pulse · no latency (state check)`;
  }
  if (look === 'slow') return `${Math.round(slowPulseSeconds(src.latencyMs))} s per pulse · ${metaOf(src).pulse}`;
  return metaOf(src).pulse || '—';
}

/** "memory 3.4 GB, all on the card, temperature 54 °C (hot), …" for the node's accessible label (model only). */
const factsSpoken = src =>
  modelFacts(src)
    .map(f => `${f.label.toLowerCase()} ${f.text}${f.hot ? ' (hot)' : ''}`)
    .join(', ');

function nodeAria(src) {
  const m = metaOf(src);
  // (the local model's probe time is a couple of ms of bookkeeping, not a link latency: its numbers say more)
  const lat = src.latencyMs != null && !src.model ? `, ${latencyText(src.latencyMs)}` : '';
  const hint = src.notConfigured ? ` (${NOT_CONFIGURED_HINT.replace(/^Not configured: /, '')})` : '';
  const facts = factsSpoken(src);
  return `${src.label} (${src.kindLabel}): ${m.label}${hint}${lat}${facts ? `; ${facts}` : ''}. ${src.detail || ''}`.trim();
}

/** APIGPU: the model's numbers as tooltip rows (only those present). */
function ModelTipRows({ src }) {
  return modelFacts(src).map(f => (
    <div key={f.k} className="apic-tip__row">
      <span>{f.label}</span>
      <b className={f.hot ? 'api-hot' : undefined}>
        {f.text}
        {f.hot ? ' · hot' : ''}
      </b>
    </div>
  ));
}

/** APIGPU: the model's numbers as card rows (only those present): a level bar for memory and power, a live
 *  CountUp for temperature and watts. */
function ModelCardRows({ src }) {
  const m = src.model;
  const tone = metaOf(src).tone;
  return modelFacts(src).map(f => {
    let value = f.text;
    if (f.k === 'temp') {
      const t = Math.round(m.tempC);
      value = (
        <>
          <CountUp key={`${src.name}:t:${t}`} to={t} duration={0.8} /> °C{f.hot ? <span className="api-hot"> · hot</span> : null}
        </>
      );
    } else if (f.k === 'power') {
      const w = Math.round(m.powerW);
      value = (
        <>
          <CountUp key={`${src.name}:w:${w}`} to={w} duration={0.8} /> W{m.powerLimitW != null ? ` of ${Math.round(m.powerLimitW)} W` : ''}
        </>
      );
    }
    return (
      <Fragment key={f.k}>
        <dt>{f.label}</dt>
        <dd className={`apic-fact apic-fact--${f.k}`}>
          <span className="apic-fact__v">{value}</span>
          {f.share != null ? <Meter share={f.share} tone={f.k === 'memory' ? tone : 'idle'} /> : null}
        </dd>
      </Fragment>
    );
  });
}

/** Ticking relative time, isolated so the whole constellation never re-renders once a second. */
function Ago({ model, epoch, prefix = '', suffix = ' ago' }) {
  const now = useNow(1000);
  const a = ageOf(model, epoch, now);
  if (a == null) return <span>—</span>;
  const t = ageText(a);
  return (
    <span>
      {prefix}
      {t === 'now' ? 'just now' : `${t}${suffix}`}
    </span>
  );
}

function Fetched({ model }) {
  const now = useNow(1000);
  if (!model || !model.fetchedAt) return null;
  const a = Math.max(0, Math.round((now - model.fetchedAt) / 1000));
  return <span>updated {a < 5 ? 'just now' : `${ageText(a)} ago`}</span>;
}

function NodeTip({ src, model }) {
  const day = model && model.generatedAt ? src.lastCheckedIso : null;
  const isModel = !!src.model; // APIGPU: the local model shows its numbers instead of the probe latency
  return (
    <>
      <div className="apic-tip__head">
        <StateChip src={src} />
        <b>{src.label}</b>
      </div>
      <div className="api-mono">
        {src.kindLabel} · {src.name}
      </div>
      {isModel ? (
        <ModelTipRows src={src} />
      ) : (
        <div className="apic-tip__row">
          <span>Latency</span>
          <b>{latencyText(src.latencyMs)}</b>
        </div>
      )}
      <div className="apic-tip__row">
        <span>Checked</span>
        <b>
          <Ago model={model} epoch={src.lastChecked} />
        </b>
      </div>
      {!isModel || src.since != null ? (
        <div className="apic-tip__row">
          <span>{inStateLabel(src)}</span>
          <b>
            <Ago model={model} epoch={src.since} suffix="" />
            {src.sinceIso ? ` · since ${etClock(src.sinceIso, day)}` : ''}
          </b>
        </div>
      ) : null}
      {src.detail ? <div className="apic-tip__detail">{src.detail}</div> : null}
      {src.notConfigured ? <div className="apic-tip__hint apic-tip__hint--nc">◌ {NOT_CONFIGURED_HINT}</div> : null}
      <div className="apic-tip__hint">Click for the full detail</div>
    </>
  );
}

function JobTip({ job, model }) {
  const now = useNow(1000);
  const m = JOB_META[job.status] || JOB_META.unknown;
  const age = jobBeatAge(model, job, now);
  return (
    <>
      <div className="apic-tip__head">
        <JobChip status={job.status} />
        <b>{job.name}</b>
      </div>
      <div className="apic-tip__detail">{job.label}</div>
      <div className="apic-tip__row">
        <span>Heartbeat</span>
        <b>
          {everyText(job.every)} · last {age == null ? '—' : agoText(age)}
        </b>
      </div>
      <div className="apic-tip__row">
        <span>Beats</span>
        <b>{job.beats == null ? '—' : job.beats.toLocaleString('en-US')}</b>
      </div>
      {job.busyWith ? (
        <div className="apic-tip__row">
          <span>Busy</span>
          <b>{job.busyWith}</b>
        </div>
      ) : null}
      {job.restarts ? (
        <div className="apic-tip__row">
          <span>Restarts 1h</span>
          <b>{job.restarts}</b>
        </div>
      ) : null}
      {job.lastError ? <div className="apic-tip__detail apic-tip__err">{job.lastError}</div> : null}
      <div className="apic-tip__hint">{m.label} · background loop of Mason&apos;s core</div>
    </>
  );
}

function CoreTip({ model }) {
  const c = model ? model.counts : null;
  const j = model && model.jobs;
  return (
    <>
      <div className="apic-tip__head">
        <b>Mason core</b>
      </div>
      <div className="apic-tip__row">
        <span>Links up</span>
        <b>{c ? `${c.up} of ${c.active}` : '—'}</b>
      </div>
      {c && c.unconfigured ? (
        <div className="apic-tip__row">
          <span>Not configured</span>
          <b>{c.unconfigured}</b>
        </div>
      ) : null}
      {model && model.localModel ? (
        <div className="apic-tip__row">
          <span>Local AI</span>
          <b className={`api-tone--${metaOf(model.localModel).tone} apic-tip__tone`}>
            {metaOf(model.localModel).glyph} {metaOf(model.localModel).label}
          </b>
        </div>
      ) : null}
      <div className="apic-tip__row">
        <span>Loops OK</span>
        <b>{j ? `${j.counts.ok} of ${j.list.length}` : '—'}</b>
      </div>
      <div className="apic-tip__hint">Click for background loops</div>
    </>
  );
}

function SourceCard({ src, model, onClose }) {
  const now = useNow(1000);
  const day = src.lastCheckedIso;
  const sinceAge = ageOf(model, src.since, now);
  const meta = metaOf(src);
  const modelKnown = !!(src.model && src.model.known); // APIGPU: the local model in a state we have words for
  const body = (
    <SpotlightCard className={`apic-card__in api-tone--${meta.tone}`} spotlightColor="rgba(74, 158, 255, 0.16)">
      <div className="apic-card__top">
        <StateChip src={src} size="lg" long />
        <span className="apic-card__kind">{src.kindLabel}</span>
        <button type="button" className="apic-card__x" onClick={onClose} aria-label="Close detail">
          ×
        </button>
      </div>
      <h3 className="apic-card__title">
        <span className="mui-sr">{src.label}</span>
        <span aria-hidden="true">
          <DecryptedText key={src.name} text={src.label} animateOn="view" sequential speed={26} encryptedClassName="apic-enc" />
        </span>
      </h3>
      <div className="api-mono apic-card__id">{src.name}</div>
      {src.notConfigured ? (
        <p className="apic-card__hint">
          <span aria-hidden="true">◌</span>
          {NOT_CONFIGURED_HINT}. Optional source, nothing is broken.
        </p>
      ) : null}
      {modelKnown ? (
        <p className={`apic-card__hint apic-card__hint--model apic-card__hint--${src.model.look}`}>
          <span aria-hidden="true">{meta.glyph}</span>
          {modelHint(src) /* REVIEW: true for the numbers sent (partly on the card ≠ "full speed") */}
        </p>
      ) : null}
      <dl className="api-kv apic-card__kv">
        {src.model ? <ModelCardRows src={src} /> : null}
        <dt>Latency</dt>
        <dd>
          {/* UIFIX 2026-11-18: never "0 ms". A reading that rounds to zero says "under 1 ms"; the local AI model's
              row also says what was timed (Ollama's status reply), and "not measured" when there is no reading. */}
          {src.latencyMs == null ? (
            src.model ? (
              'not measured'
            ) : (
              '— (state check, no network round-trip)'
            )
          ) : underOneMs(src.latencyMs) ? (
            UNDER_1_MS
          ) : (
            <>
              <CountUp key={`${src.name}:${Math.round(src.latencyMs)}`} to={Math.round(src.latencyMs)} duration={0.8} /> ms
            </>
          )}
          {src.model && src.latencyMs != null ? MODEL_LATENCY_NOTE : ''}
        </dd>
        <dt>Last check</dt>
        <dd>
          {agoText(ageOf(model, src.lastChecked, now))}
          {src.lastCheckedIso ? ` · ${etClock(src.lastCheckedIso)}` : ''}
        </dd>
        <dt>{modelKnown || src.state === 'up' || src.notConfigured ? inStateLabel(src) : 'In state'}</dt>
        <dd>
          {sinceAge == null ? '—' : ageText(sinceAge)}
          {src.sinceIso ? ` · since ${etClock(src.sinceIso, day)}` : ''}
        </dd>
        <dt>Last OK</dt>
        <dd>
          {src.state === 'up'
            ? 'the last check'
            : src.state === 'unknown'
              ? 'not checked yet'
              : src.notConfigured
                ? 'n/a (not configured)'
                : src.sinceIso && !meta.splitFrom // APIAMBER: mostly_cpu's `since` is when on_gpu began, not when it went under half
                ? `before ${etClock(src.sinceIso, day)}`
                : 'unknown'}
        </dd>
        <dt>Detail</dt>
        <dd className="apic-card__detail">{src.detail || '—'}</dd>
        <dt>Probe</dt>
        <dd>{src.note || '—'}</dd>
        <dt>Pulse</dt>
        <dd>{pulseLine(src)}</dd>
        {src.rawState && !modelKnown && !['up', 'down', 'limit', 'not_configured'].includes(src.rawState.toLowerCase()) ? (
          <>
            <dt>State</dt>
            <dd>{src.rawState}</dd>
          </>
        ) : null}
      </dl>
    </SpotlightCard>
  );
  if (src.state !== 'down') return body;
  return (
    <ElectricBorder color="#e0768a" speed={0.6} chaos={0.08} borderRadius={14} className="apic-card__eb">
      {body}
    </ElectricBorder>
  );
}

function CoreCard({ model, onClose }) {
  const now = useNow(1000);
  const j = model && model.jobs;
  const rc = j && j.reconcile;
  return (
    <SpotlightCard className="apic-card__in api-tone--idle" spotlightColor="rgba(74, 158, 255, 0.16)">
      <div className="apic-card__top">
        <span className="api-chip api-chip--idle api-chip--lg">
          <span className="api-chip__g" aria-hidden="true">
            ◎
          </span>
          Core
        </span>
        <span className="apic-card__kind">background loops</span>
        <button type="button" className="apic-card__x" onClick={onClose} aria-label="Close detail">
          ×
        </button>
      </div>
      <h3 className="apic-card__title">
        <span className="mui-sr">Mason core</span>
        <span aria-hidden="true">
          <DecryptedText key="core" text="Mason core" animateOn="view" sequential speed={26} encryptedClassName="apic-enc" />
        </span>
      </h3>
      {!j ? (
        <p className="apic-card__note">No background-job data in this snapshot.</p>
      ) : (
        <>
          <dl className="api-kv apic-card__kv">
            <dt>Loops</dt>
            <dd>
              {j.counts.ok} of {j.list.length} OK{j.counts.off ? ` · ${j.counts.off} off` : ''}
              {j.problems.length ? ` · ${j.problems.length} need attention` : ''}
            </dd>
            <dt>Supervisor</dt>
            <dd>{j.supervisorRunning === true ? 'running' : j.supervisorRunning === false ? 'NOT running' : '—'}</dd>
            <dt>Watchdog</dt>
            <dd>{j.watchdog ? (j.watchdog.enabled ? `on · ${j.watchdog.pings.toLocaleString('en-US')} pings` : 'off (systemd did not ask for it)') : '—'}</dd>
            {rc ? (
              <>
                <dt>Reconcile</dt>
                <dd>
                  {rc.ok === true ? 'OK' : rc.ok === false ? 'FAILED' : '—'}
                  {rc.kind ? ` · ${rc.kind}` : ''}
                  {rc.at ? ` · ${etClock(rc.at)}` : ''}
                  {rc.fillsSeen != null ? ` · ${rc.fillsSeen} fills seen` : ''}
                  {rc.errors ? ` · ${rc.errors} error${rc.errors === 1 ? '' : 's'}` : ''}
                </dd>
              </>
            ) : null}
            {j.error ? (
              <>
                <dt>Error</dt>
                <dd>{j.error}</dd>
              </>
            ) : null}
          </dl>
          <ul className="apic-jobs" aria-label="Background loops">
            {j.list.map(job => {
              const age = jobBeatAge(model, job, now);
              return (
                <li key={job.name} className="apic-job">
                  <JobChip status={job.status} />
                  <span className="apic-job__name" title={job.label}>
                    {job.name}
                  </span>
                  <span className="apic-job__meta">
                    {everyText(job.every)} · {age == null ? 'no beat yet' : `beat ${agoText(age)}`}
                    {job.busyWith ? ` · busy: ${job.busyWith}` : ''}
                    {job.restarts ? ` · ${job.restarts} restart${job.restarts === 1 ? '' : 's'}/h` : ''}
                  </span>
                  {job.lastError ? <span className="apic-job__err">{job.lastError}</span> : null}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </SpotlightCard>
  );
}

export default function ApiConstellation() {
  const api = useHealth();
  const { model, status, failed, error } = api;
  const [focus, setFocus] = useApiFocus('constellation');
  const [hover, setHover] = useState(null);
  const [dock, setDock] = useState('right');
  const reduce = usePrefersReducedMotion();
  const visible = useDocumentVisible();
  const onScreen = useOnScreen();
  const tipId = useId();

  const rootRef = useRef(null);
  const stageRef = useRef(null);
  const canvasRef = useRef(null);
  const tipRef = useRef(null);
  const coreRef = useRef(null);
  const barRef = useRef(null);
  const cardRef = useRef(null);
  const engineRef = useRef(null);
  const els = useRef(new Map());
  const refFns = useRef(new Map());

  const sources = useMemo(() => (model ? model.sources : []), [model]);
  const byName = useMemo(() => new Map(sources.map(s => [s.name, s])), [sources]);
  const focusedSrc = focus && focus !== CORE ? byName.get(focus) || null : null;
  const cardOpen = focus === CORE || !!focusedSrc;

  // one engine per mount; node refs attach before this effect runs, so bind what is already there
  useLayoutEffect(() => {
    const eng = createEngine({ stage: stageRef.current, canvas: canvasRef.current });
    engineRef.current = eng;
    for (const [name, el] of els.current) eng.bindNode(name, el);
    eng.bindCore(coreRef.current);
    eng.bindTip(tipRef.current);
    return () => {
      eng.destroy();
      engineRef.current = null;
    };
  }, []);

  const nodeRef = useCallback(name => {
    let fn = refFns.current.get(name);
    if (!fn) {
      fn = el => {
        if (el) els.current.set(name, el);
        else els.current.delete(name);
        if (engineRef.current) engineRef.current.bindNode(name, el);
      };
      refFns.current.set(name, fn);
    }
    return fn;
  }, []);

  useLayoutEffect(() => {
    if (engineRef.current) engineRef.current.setModel(model, failed);
  }, [model, failed]);
  useLayoutEffect(() => {
    if (engineRef.current) engineRef.current.setFocus(cardOpen ? focus : null);
  }, [focus, cardOpen]);
  useLayoutEffect(() => {
    if (engineRef.current) engineRef.current.setHover(hover);
  }, [hover]);
  useEffect(() => {
    if (engineRef.current) engineRef.current.setMotion({ run: visible && onScreen && !reduce, reduce, visible: visible && onScreen });
  }, [visible, onScreen, reduce]);

  // a hovered node that disappeared (data refresh) must not keep a tooltip
  useEffect(() => {
    if (hover && hover.type === 'node' && !byName.has(hover.name)) setHover(null);
    if (hover && hover.type === 'job' && !(model && model.jobs && model.jobs.list[hover.index])) setHover(null);
  }, [hover, byName, model]);

  // size → dock side, safe-area insets (bar + card), relayout
  const measure = useCallback(() => {
    const root = rootRef.current;
    const eng = engineRef.current;
    if (!root || !eng) return;
    const w = root.clientWidth;
    const nextDock = w >= DOCK_RIGHT_MIN ? 'right' : 'bottom';
    setDock(d => (d === nextDock ? d : nextDock));
    const bar = barRef.current;
    const card = cardRef.current;
    const barH = bar && bar.offsetParent !== null ? bar.offsetHeight + 4 : 0;
    const inset = { bottom: barH };
    if (card) {
      if (nextDock === 'right') inset.right = card.offsetWidth + 18;
      else inset.bottom = card.offsetHeight + 14;
    }
    eng.setInset(inset);
    eng.invalidate();
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, cardOpen, focus, dock, sources]);

  useEffect(() => {
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(() => measure());
    if (rootRef.current) ro.observe(rootRef.current);
    if (barRef.current) ro.observe(barRef.current);
    return () => ro.disconnect();
  }, [measure]);
  useEffect(() => {
    const card = cardRef.current;
    if (!card || typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(() => measure());
    ro.observe(card);
    return () => ro.disconnect();
  }, [measure, cardOpen, focus]);
  useEffect(() => {
    let alive = true;
    const f = typeof document !== 'undefined' && document.fonts && document.fonts.ready;
    if (f && typeof f.then === 'function') {
      f.then(() => {
        if (alive && engineRef.current) engineRef.current.invalidate();
      });
    }
    return () => {
      alive = false;
    };
  }, []);

  const close = useCallback(() => setFocus(null), [setFocus]);
  const toggle = useCallback(name => setFocus(focus === name ? null : name), [focus, setFocus]);

  const onKeyDown = useCallback(
    e => {
      if (e.key === 'Escape' && focus) {
        e.preventDefault();
        setFocus(null);
        return;
      }
      const t = e.target;
      if (!t || !t.dataset || t.dataset.apicNode == null) return;
      const dir = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!dir || !sources.length) return;
      e.preventDefault();
      const i = sources.findIndex(s => s.name === t.dataset.apicNode);
      const next = sources[(i + dir + sources.length) % sources.length];
      const el = next && els.current.get(next.name);
      if (el) el.focus();
    },
    [focus, setFocus, sources]
  );

  const onStageMove = useCallback(
    e => {
      const eng = engineRef.current;
      const stage = stageRef.current;
      if (!eng || !stage) return;
      if (e.target && e.target.closest && e.target.closest('.apic-node,.apic-core,.apic-card')) return;
      const r = stage.getBoundingClientRect();
      const idx = eng.jobAt(e.clientX - r.left, e.clientY - r.top);
      setHover(h => {
        if (idx >= 0) return h && h.type === 'job' && h.index === idx ? h : { type: 'job', index: idx };
        return h && h.type === 'job' ? null : h;
      });
    },
    []
  );
  const onStageLeave = useCallback(() => setHover(h => (h && h.type === 'job' ? null : h)), []);
  const onStageClick = useCallback(
    e => {
      if (e.target && e.target.closest && e.target.closest('.apic-node,.apic-core,.apic-card,.apic-bar')) return;
      if (hover && hover.type === 'job') {
        setFocus(CORE);
        return;
      }
      if (focus) setFocus(null);
    },
    [focus, hover, setFocus]
  );

  const counts = model ? model.counts : null;
  // APIGPU: the legend grows only while the local model is in one of its own looks (asleep / on the processor)
  const asleep = sources.some(s => lookOf(s) === 'asleep');
  const legend = LEGEND.map(e => (e[2] === 'limit' && counts && counts.slow ? ['warn', '◆', 'limit / slow'] : e));
  if (counts && counts.unconfigured) legend.push(LEGEND_NC);
  if (asleep) legend.push(LEGEND_ASLEEP);
  const hovSrc = hover && hover.type === 'node' ? byName.get(hover.name) : null;
  const hovJob = hover && hover.type === 'job' && model && model.jobs ? model.jobs.list[hover.index] : null;

  let overlay = null;
  if (!model && status === 'error') {
    overlay = (
      <div className="apic-overlay" role="alert">
        <span>Couldn&apos;t reach the health monitor ({String(error || 'error')}).</span>
        <button type="button" className="mui-btn mui-btn--ghost" onClick={() => api.refresh()}>
          Retry
        </button>
      </div>
    );
  } else if (!model) {
    overlay = (
      <div className="apic-overlay" role="status">
        Connecting to the health monitor…
      </div>
    );
  } else if (!sources.length) {
    overlay = (
      <div className="apic-overlay" role="status">
        {model.monitorNote
          ? `Source-health monitor: ${model.monitorNote}.`
          : 'No source statuses yet — the health monitor runs its first sweep within a minute of boot.'}
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      className="apic"
      data-dock={dock}
      data-card={cardOpen ? '1' : '0'}
      data-reduce={reduce ? '1' : '0'}
      onKeyDown={onKeyDown}
    >
      <div ref={stageRef} className="apic-stage" onPointerMove={onStageMove} onPointerLeave={onStageLeave} onClick={onStageClick}>
        <canvas ref={canvasRef} className="apic-canvas" aria-hidden="true" />
        <button
          ref={coreRef}
          type="button"
          className="apic-core"
          aria-pressed={focus === CORE}
          aria-label={
            model
              ? `Mason core: ${model.counts.up} of ${model.counts.active} links up${
                  model.counts.unconfigured ? `, ${model.counts.unconfigured} not configured` : ''
                }${model.counts.slow ? `, ${model.counts.slow} slow` : ''}${
                  model.jobs ? `, ${model.jobs.counts.ok} of ${model.jobs.list.length} background loops OK` : ''
                }. Show background loops.`
              : 'Mason core'
          }
          onClick={() => toggle(CORE)}
          onPointerEnter={() => setHover({ type: 'core' })}
          onPointerLeave={() => setHover(h => (h && h.type === 'core' ? null : h))}
        >
          <b>MASON</b>
          <small>{counts ? `${counts.up}/${counts.active}` : '…'}</small>
        </button>
        {sources.map(s => {
          const m = metaOf(s);
          const isHover = hover && hover.type === 'node' && hover.name === s.name;
          return (
            <button
              key={s.name}
              ref={nodeRef(s.name)}
              type="button"
              className={`apic-node apic-node--${lookOf(s)} api-tone--${m.tone}${s.big ? ' apic-node--big' : ''}${s.model ? ' apic-node--model' : ''}${focus === s.name ? ' is-focus' : ''}`}
              data-apic-node={s.name}
              aria-label={nodeAria(s)}
              aria-pressed={focus === s.name}
              aria-describedby={isHover ? tipId : undefined}
              onClick={() => toggle(s.name)}
              onPointerEnter={() => setHover({ type: 'node', name: s.name })}
              onPointerLeave={() => setHover(h => (h && h.type === 'node' && h.name === s.name ? null : h))}
              onFocus={() => setHover({ type: 'node', name: s.name })}
              onBlur={() => setHover(h => (h && h.type === 'node' && h.name === s.name ? null : h))}
            >
              <span className="apic-dot" aria-hidden="true">
                {m.glyph}
              </span>
              <span className="apic-lbl" aria-hidden="true">
                <span className="apic-lbl-full">{s.label}</span>
                <span className="apic-lbl-short">{s.short}</span>
              </span>
            </button>
          );
        })}
        <div ref={tipRef} id={tipId} className="apic-tip" role="tooltip" hidden={!hover || (!hovSrc && !hovJob && hover.type !== 'core')}>
          {hovSrc ? <NodeTip src={hovSrc} model={model} /> : null}
          {hovJob ? <JobTip job={hovJob} model={model} /> : null}
          {hover && hover.type === 'core' ? <CoreTip model={model} /> : null}
        </div>
        {overlay}
      </div>
      <div ref={barRef} className="apic-bar">
        <span
          className="apic-legend"
          title={`▲ up · ◆ limit hit · ▼ down · ○ not checked${
            counts && counts.unconfigured ? ' · ◌ not configured (optional, add a key to enable)' : ''
          }${counts && counts.slow ? ` · ◆ slow: ${slowWords(model)}` : ''}${
            asleep ? ' · ☾ asleep: the local AI model is resting and wakes up on the next request' : ''
          } — pulse speed = probe latency, pulse brightness = how fresh the last check is`}
        >
          {legend.map(([tone, glyph, label]) => (
            <span key={label} className={`api-tone--${tone}`}>
              {glyph}
              <span className="apic-legend__t"> {label}</span>
            </span>
          ))}
          <span className="apic-legend__hint">pulse speed = latency · brightness = freshness</span>
        </span>
        <span className="apic-bar__right">
          <StaleNote failed={failed && !!model} error={error} model={model} now={Date.now()} />
          {counts ? <span>{counts.total} links</span> : null}
          <Fetched model={model} />
        </span>
      </div>
      {cardOpen ? (
        <aside ref={cardRef} className="apic-card" aria-label={focusedSrc ? `${focusedSrc.label} detail` : 'Mason core detail'}>
          {focusedSrc ? <SourceCard src={focusedSrc} model={model} onClose={close} /> : <CoreCard model={model} onClose={close} />}
        </aside>
      ) : null}
    </div>
  );
}
