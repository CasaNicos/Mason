// Shared small UI states every RH widget reuses (S3 §2 "Data"). Text is always present, so no
// state is signalled by color alone. All strings render through JSX (escaped).
import { dateTime } from './fmt.js';

export function Loading({ label = 'Loading…', rows = 3 }) {
  return (
    <div className="mui-state mui-state--loading" role="status" aria-live="polite">
      <span className="mui-sr">{label}</span>
      {Array.from({ length: Math.max(1, Math.min(8, rows)) }, (_, i) => (
        <div key={i} className="mui-skel" style={{ width: `${92 - i * 14}%` }} aria-hidden="true" />
      ))}
    </div>
  );
}

/**
 * "Off — enable `rh_expansion.<x>`" (contracts §6: route returned {enabled:false}). Every RH-backed
 * module is also off while the master switch `robinhood.enabled` is false (contracts §7), so the
 * hint names both.
 */
export function Off({ flag = 'rh_expansion' }) {
  return (
    <div className="mui-state mui-state--off" role="status">
      <span className="mui-state__icon mui-state__icon--svg" aria-hidden="true">
        {/* power symbol as inline SVG: the U+23FB glyph is tofu without a symbol font (F0b) */}
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">
          <path d="M8 1.75v5.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <path d="M4.6 3.9a5.1 5.1 0 1 0 6.8 0" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </span>
      <span>
        Off — enable <code className="mui-code">{String(flag)}</code>
        <span className="mui-state__sub">
          {' '}(and the master switch <code className="mui-code">robinhood.enabled</code>)
        </span>
      </span>
    </div>
  );
}

export function ErrorState({ error, onRetry }) {
  return (
    <div className="mui-state mui-state--error" role="alert">
      <span className="mui-state__icon" aria-hidden="true">!</span>
      <span className="mui-state__text">Couldn’t load: {String(error || 'unknown error')}</span>
      {onRetry ? (
        <button type="button" className="mui-btn mui-btn--ghost" onClick={() => onRetry()}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Empty({ children = 'Nothing yet.' }) {
  return <div className="mui-state mui-state--empty">{children}</div>;
}

/** Amber "stale" pill. Renders nothing when fresh. The reason is in the title + text. */
export function StaleBadge({ stale, errors = [], error = null, asOf = null }) {
  if (!stale) return null;
  const reasons = [...(Array.isArray(errors) ? errors : []), ...(error ? [error] : [])].map(String);
  const title = [asOf ? `as of ${dateTime(asOf)}` : null, ...reasons].filter(Boolean).join(' · ') || 'stale data';
  return (
    <span className="mui-badge mui-badge--warn" title={title} role="status">
      <span aria-hidden="true">⚠</span> stale{reasons.length ? ` · ${reasons[0]}` : ''}
    </span>
  );
}

/**
 * <RhState api={useRhApi(...)}>{data => ...}</RhState>
 * loading → skeleton, off → Off, error → ErrorState(retry), ok → children(data) with the stale
 * badge on top when needed. `empty(data)` (optional) decides when to show <Empty>.
 */
export function RhState({ api, children, empty, emptyText, loadingRows }) {
  if (!api || api.status === 'loading') return <Loading rows={loadingRows} />;
  if (api.status === 'off') return <Off flag={api.flag} />;
  if (api.status === 'error') return <ErrorState error={api.error} onRetry={api.refresh} />;
  const isEmpty = typeof empty === 'function' ? empty(api.data) : api.data == null;
  return (
    <>
      {api.stale ? (
        <div className="mui-state__badges">
          <StaleBadge stale errors={api.errors} error={api.error} asOf={api.asOf} />
        </div>
      ) : null}
      {isEmpty ? <Empty>{emptyText}</Empty> : children(api.data)}
    </>
  );
}
