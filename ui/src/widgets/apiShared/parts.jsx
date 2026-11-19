// Small shared pieces for the API-page widgets. All server text renders as JSX (escaped).
import { useEffect, useState } from 'react';
import { useDocumentVisible, useOnScreen } from '../../lib/visibility.js';
import { JOB_META, metaOf } from './health.js';

/** Date.now() that ticks every `ms` while the tab is visible and the widget is on screen (relative times). */
export function useNow(ms = 1000, on = true) {
  const visible = useDocumentVisible();
  const onScreen = useOnScreen();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on || !visible || !onScreen) return undefined;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms, on, visible, onScreen]);
  return now;
}

/** "▲ UP" chip: glyph + text + tone color. Pass `src` (a source) instead of `state` to get the source's own
 *  words: the local AI model says "▲ ON GPU" / "☾ ASLEEP" / "◆ ON CPU" / "▼ NO GPU" (APIGPU). */
export function StateChip({ state, src, size = 'md', long = false }) {
  const m = metaOf(src || { state });
  return (
    <span className={`api-chip api-chip--${m.tone} api-chip--${size}`} title={m.title || undefined}>
      <span className="api-chip__g" aria-hidden="true">
        {m.glyph}
      </span>
      {long ? m.label : m.short}
    </span>
  );
}

/** APIGPU: a thin level bar (graphics-card memory, power draw). Decoration only: the numbers next to it carry the
 *  meaning in words, so it is hidden from screen readers. Built from scratch: React Bits has no meter / gauge
 *  (ElasticSlider is an input, Stepper a wizard). share = 0-1. */
export function Meter({ share, tone = 'up' }) {
  if (share == null || !Number.isFinite(share)) return null;
  const pct = Math.round(Math.max(0, Math.min(1, share)) * 100);
  return (
    <span className={`api-meter api-tone--${tone}`} aria-hidden="true">
      <span className="api-meter__fill" style={{ width: `${pct}%` }} />
    </span>
  );
}

export function JobChip({ status }) {
  const m = JOB_META[status] || JOB_META.unknown;
  return (
    <span className={`api-chip api-chip--${m.tone} api-chip--sm`}>
      <span className="api-chip__g" aria-hidden="true">
        {m.glyph}
      </span>
      {m.label}
    </span>
  );
}

export function StaleNote({ failed, error, model, now }) {
  if (!failed) return null;
  const age = model && model.fetchedAt ? Math.max(0, Math.round((now - model.fetchedAt) / 1000)) : null;
  return (
    <span className="api-stale" role="status" title={error ? String(error) : 'the last refresh failed'}>
      <span aria-hidden="true">⚠</span> stale{age != null ? ` · last good ${age < 60 ? `${age}s` : `${Math.round(age / 60)}m`} ago` : ''}
    </span>
  );
}
