// apiSources: every probed source as a detail card (UI overhaul Task 6; replaces the old #mv-health-grid
// cards and keeps everything they showed: status dot → status chip with text, name, kind, latency, detail,
// "since …", the amber "Limit Hit" badge with its explanation on hover). Adds: last-check age, the probe
// note, a filter (All / Issues / Up) and two-way focus with the constellation: click (or Enter on) a card
// to focus that source there; focusing a node there highlights + scrolls to its card here.
// R3-C (2026-11-17): a not-configured source (optional, no key) gets its own calm chip + hint; it shows under
// All only (never Issues, never Up), and the Issues count leaves it out.
// APIGPU (2026-11-17): the local AI model's card shows where it runs in its own words (▲ ON GPU / ☾ ASLEEP /
// ◆ ON CPU / ▼ NO GPU / ▼ DOWN), its numbers (model, memory, temperature, power, card: only those sent) and, when
// it is not simply on the card, a one-line "what this means". On the processor it is an issue (Issues, not Up).
// APIAMBER (Nico, 2026-11-18): so is less than half of it on the card (◆ MOSTLY CPU, "loaded for 25m"). The model's
// card shows no probe time (Ollama's bookkeeping reply, not a link latency), which keeps its name whole.
// React Bits used: AnimatedList (keyboard listbox: ↑/↓ + Enter; rows animate in as they scroll into view).
// Reflows with container queries (sources.css): one column when narrow, a card grid when wide.
import { useCallback, useMemo } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { useHealth } from '../apiShared/useHealth.js';
import { useApiFocus } from '../apiShared/focus.js';
import {
  ageOf,
  ageText,
  agoText,
  etClock,
  filterCounts,
  keepSource,
  latencyText,
  lookOf,
  metaOf,
  modelFacts,
  modelHint,
  NOT_CONFIGURED_HINT
} from '../apiShared/health.js';
import { StaleNote, StateChip, useNow } from '../apiShared/parts.jsx';
import { ErrorState, Loading } from '../../lib/states.jsx';
import '../apiShared/api.css';
import './sources.css';

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'issues', label: 'Issues' },
  { id: 'up', label: 'Up' }
];

function Row({ s, model, selected }) {
  const now = useNow(5000);
  const m = metaOf(s);
  const checked = ageOf(model, s.lastChecked, now);
  const inState = ageOf(model, s.since, now);
  const modelKnown = !!(s.model && s.model.known); // APIGPU
  const facts = modelFacts(s);
  return (
    <div className={`apil-row api-tone--${m.tone}${s.model ? ' apil-row--model' : ''}${selected ? ' is-sel' : ''}`}>
      <div className="apil-row__top">
        <StateChip src={s} />
        <span className="apil-row__name">{s.label}</span>
        <span className="apil-row__kind">{s.kindLabel}</span>
        {/* APIAMBER: no probe time for the local model (Ollama's 0-2 ms bookkeeping reply, not a link latency; its
            tooltip leaves it out too), so its name stays whole next to the wider ◆ MOSTLY CPU chip */}
        {s.model ? null : (
          <span className="apil-row__lat" title="probe round-trip">
            {s.latencyMs != null ? latencyText(s.latencyMs) : ''}
          </span>
        )}
      </div>
      <div className="apil-row__detail">{s.detail || (s.state === 'unknown' ? 'not yet checked' : s.notConfigured ? 'optional source' : '—')}</div>
      {s.notConfigured ? (
        <div className="apil-row__hint api-hint">
          <span aria-hidden="true">◌</span>
          {NOT_CONFIGURED_HINT}
        </div>
      ) : null}
      {facts.length ? (
        <div className="apil-row__facts">
          {facts.map(f => (
            <span key={f.k} className={`apil-row__fact apil-row__fact--${f.k}${f.hot ? ' api-hot' : ''}`}>
              <span className="apil-row__fk">{f.label}</span> {f.text}
              {f.hot ? ' · hot' : ''}
            </span>
          ))}
        </div>
      ) : null}
      {modelKnown && lookOf(s) !== 'up' ? (
        <div className="apil-row__hint api-hint apil-row__hint--model">
          <span aria-hidden="true">{m.glyph}</span>
          {modelHint(s)}
        </div>
      ) : null}
      <div className="apil-row__meta">
        <span className="api-mono">{s.name}</span>
        {checked != null ? <span>checked {agoText(checked)}</span> : null}
        {inState != null ? (
          <span>
            {modelKnown ? m.forText || m.label.toLowerCase() : s.state === 'up' ? 'up' : s.state === 'unknown' ? 'waiting' : m.label.toLowerCase()} for {ageText(inState)}
            {s.sinceIso ? ` (since ${etClock(s.sinceIso, s.lastCheckedIso)})` : ''}
          </span>
        ) : null}
      </div>
      {s.note ? <div className="apil-row__note">probe: {s.note}</div> : null}
    </div>
  );
}

export default function ApiSources({ config, saveConfig }) {
  const api = useHealth();
  const { model, status, failed, error, refresh } = api;
  const [focus, setFocus] = useApiFocus('sources');
  const filter = FILTERS.some(f => f.id === (config && config.filter)) ? config.filter : 'all';
  const setFilter = useCallback(f => saveConfig({ ...(config || {}), filter: f }), [config, saveConfig]);

  const sources = useMemo(() => (model ? model.sources : []), [model]);
  const rows = useMemo(() => sources.filter(s => keepSource(filter, s)), [sources, filter]);
  const counts = useMemo(() => filterCounts(sources), [sources]);
  const sel = focus ? rows.findIndex(s => s.name === focus) : -1;

  const renderItem = useCallback((s, _i, selected) => <Row s={s} model={model} selected={selected} />, [model]);
  const itemKey = useCallback(s => s.name, []);
  const onSelect = useCallback(s => setFocus(focus === s.name ? null : s.name), [focus, setFocus]);

  if (status === 'loading' && !model) return <Loading rows={5} label="Loading sources…" />;
  if (status === 'error' && !model) return <ErrorState error={error} onRetry={refresh} />;

  return (
    <div className="apil">
      <div className="apil-bar">
        <div className="apil-filters" role="group" aria-label="Filter sources">
          {FILTERS.map(f => (
            <button
              key={f.id}
              type="button"
              className="apil-filter"
              aria-pressed={filter === f.id}
              onClick={() => setFilter(f.id)}
              title={f.id === 'all' && counts.unconfigured ? `includes ${counts.unconfigured} not configured` : undefined}
            >
              {f.label} <b>{counts[f.id]}</b>
            </button>
          ))}
        </div>
        <StaleNote failed={failed} error={error} model={model} now={Date.now()} />
      </div>
      {!sources.length ? (
        <div className="mui-state mui-state--empty">
          {model && model.monitorNote
            ? `Source-health monitor: ${model.monitorNote}.`
            : 'No source statuses yet — the health monitor runs its first sweep within a minute of boot.'}
        </div>
      ) : !rows.length ? (
        <div className="mui-state mui-state--empty">
          {filter === 'issues'
            ? counts.unconfigured
              ? `No issues: every configured source is up (${counts.unconfigured} not configured, see All).`
              : 'No issues: every source is up.'
            : 'No sources match.'}
        </div>
      ) : (
        <AnimatedList
          className="apil-list"
          items={rows}
          renderItem={renderItem}
          itemKey={itemKey}
          onItemSelect={onSelect}
          selectedIndex={sel >= 0 ? sel : undefined}
          ariaLabel="Data sources (Enter focuses one in the constellation)"
          showGradients
          enableArrowNavigation
          displayScrollbar
        />
      )}
    </div>
  );
}
