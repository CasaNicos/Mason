// calendar: what the day popover shows past its list — one event's details, and the add / edit form.
// React Bits has no form field, so these are plain markup (the popover itself is Popover.jsx).
// API text is rendered as JSX text only; a link is used only when it parsed as http / https (calUtil.safeUrl).
import { useId, useState } from 'react';
import { emitChartSymbol } from '../../lib/bridge.js';
import { IMPORTANCE_TEXT, clock, longDay, moveFirstDay, rangeText, relDay, rowLabel, shortDay, validateForm } from './calUtil.js';

export function CloseButton({ onClose }) {
  return (
    <button type="button" className="mui-cal__x" aria-label="Close" title="Close" onClick={onClose}>
      <span aria-hidden="true">×</span>
    </button>
  );
}
/** "‹ Nov 17": back to the day's list. */
export function BackButton({ label, onBack }) {
  return (
    <button type="button" className="mui-cal__back" data-act="back" onClick={onBack} title={`Back to ${label}`}>
      <span aria-hidden="true">‹</span> {label}
    </button>
  );
}

// ── details ──────────────────────────────────────────────────────────────────
/** `onBack` (optional) puts "‹ day" in the header; `onRemoved` runs after a remove (default: onClose). */
export function Details({ ev, today, held, onClose, onBack, backLabel, onRemoved, onEdit, onWrite }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const range = rangeText(ev, today);
  const rel = relDay(ev.date, today);

  const send = async (method, body, closeAfter) => {
    setBusy(true);
    setError('');
    const r = await onWrite(method, body);
    setBusy(false);
    if (!r.ok) {
      setError(r.error);
      setConfirming(false);
    } else if (closeAfter) (onRemoved || onClose)();
  };

  return (
    <div className="mui-cal__det" data-type={ev.type} data-id={ev.id}>
      <div className="mui-cal__dethead">
        {onBack ? <BackButton label={backLabel} onBack={onBack} /> : null}
        <span className="mui-cal__type">
          <i className="mui-cal__dot" aria-hidden="true" />
          {rowLabel(ev)}
        </span>
        <CloseButton onClose={onClose} />
      </div>
      <div className="mui-cal__detbody">
        <h3 className={`mui-cal__dettitle${ev.done ? ' is-done' : ''}`}>{ev.full_title}</h3>
        <p className="mui-cal__detwhen">
          <b>{longDay(ev.date)}</b>
          {ev.time_et ? ` · ${clock(ev.time_et)} Eastern time` : ' · all day'}
          {rel ? <span className="mui-cal__detrel"> ({rel})</span> : null}
          {range ? <span className="mui-cal__detrange">{range}</span> : null}
        </p>
        {ev.subtitle ? <p className="mui-cal__detsub">{ev.subtitle}</p> : null}
        {ev.note ? (
          <p className="mui-cal__detnote">
            <span className="mui-cal__k">Your note</span>
            {ev.note}
          </p>
        ) : null}
        {ev.tickers.length ? (
          <div className="mui-cal__dettk">
            <span className="mui-cal__k">Stocks</span>
            {ev.tickers.map(t => (
              <button key={t} type="button" className={`mui-cal__tk mui-cal__tk--btn${held.has(t) ? ' is-held' : ''}`} title={`Show ${t} on the chart`} onClick={() => emitChartSymbol('main', t)}>
                {t}
                {held.has(t) ? <em> held</em> : null}
              </button>
            ))}
            {ev.held && !ev.tickers.some(t => held.has(t)) ? <span className="mui-cal__flag mui-cal__flag--held">◆ includes a stock you hold</span> : null}
            {ev.watched ? <span className="mui-cal__flag">on your watchlist</span> : null}
          </div>
        ) : null}
        <dl className="mui-cal__facts">
          <dt>Date</dt>
          <dd data-fact="confirmed">{ev.confirmed ? '✓ Confirmed' : '? Not confirmed. It may still change.'}</dd>
          {ev.changed ? (
            <>
              <dt>Moved</dt>
              <dd data-fact="moved">
                ↪ From {shortDay(ev.changed.previous_date, today)}
                {ev.changed.changed_on ? ` (changed on ${shortDay(ev.changed.changed_on, today)})` : ''}
              </dd>
            </>
          ) : null}
          <dt>How big</dt>
          <dd data-fact="weight">
            {ev.importance === 3 ? '★ ' : ''}
            {IMPORTANCE_TEXT[ev.importance]}
          </dd>
          {ev.source ? (
            <>
              <dt>From</dt>
              <dd data-fact="source">{ev.source}</dd>
            </>
          ) : null}
          {ev.done ? (
            <>
              <dt>Status</dt>
              <dd data-fact="done">✓ Done</dd>
            </>
          ) : null}
        </dl>
        {ev.url ? (
          <a className="mui-cal__link" href={ev.url} target="_blank" rel="noopener noreferrer">
            Open the official page <span aria-hidden="true">↗</span>
          </a>
        ) : null}
        {error ? (
          <p className="mui-cal__err" role="alert">
            {error}
          </p>
        ) : null}
      </div>
      {ev.editable ? (
        confirming ? (
          <div className="mui-cal__acts" role="group" aria-label="Remove this event?">
            <span className="mui-cal__ask">Remove this event for good?</span>
            <button type="button" className="mui-btn mui-cal__danger" data-act="remove-yes" disabled={busy} onClick={() => send('DELETE', { id: ev.id }, true)}>
              {busy ? 'Removing…' : 'Yes, remove it'}
            </button>
            <button type="button" className="mui-btn mui-btn--ghost" data-act="remove-no" data-autofocus disabled={busy} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </div>
        ) : (
          <div className="mui-cal__acts">
            <button type="button" className="mui-btn" data-act="edit" disabled={busy} onClick={onEdit}>
              Edit
            </button>
            <button type="button" className="mui-btn mui-btn--ghost" data-act="done" disabled={busy} onClick={() => send('PUT', { id: ev.id, done: !ev.done }, false)}>
              {busy ? 'Saving…' : ev.done ? 'Mark not done' : 'Mark done'}
            </button>
            <button type="button" className="mui-btn mui-btn--ghost mui-cal__danger" data-act="remove" disabled={busy} onClick={() => setConfirming(true)}>
              Remove
            </button>
          </div>
        )
      ) : null}
    </div>
  );
}

// ── add / edit ───────────────────────────────────────────────────────────────
/** `half`: never wider than half the row (the time field, which wraps onto a line of its own). */
function Field({ label, hint, error, half = false, children }) {
  return (
    <label className={`mui-cal__field${half ? ' mui-cal__field--half' : ''}${error ? ' has-error' : ''}`}>
      <span className="mui-cal__k">
        {label}
        {hint ? <i> {hint}</i> : null}
      </span>
      {children}
      {error ? (
        <span className="mui-cal__fielderr" role="alert">
          {error}
        </span>
      ) : null}
    </label>
  );
}

/**
 * `ev` = the event being edited (null = a new one; `date` presets its day). A reminder only has a name and a day.
 * "Last day" (optional) makes an event of several days: it is sent as end_date, and an edit sends '' to clear it.
 */
export function EventForm({ ev, today, date, onCancel, onWrite }) {
  const full = !(ev && ev.id.startsWith('rem:'));
  const [f, setF] = useState({
    title: ev ? ev.full_title : '',
    date: ev ? ev.date : date || today || '',
    end: ev && ev.end_date ? ev.end_date : '',
    time: ev && ev.time_et ? ev.time_et : '',
    tickers: ev ? ev.tickers.join(', ') : '',
    note: ev ? ev.note : ''
  });
  const [errors, setErrors] = useState({});
  const [failed, setFailed] = useState('');
  const [busy, setBusy] = useState(false);
  const formId = useId();
  const set = k => e => setF(s => ({ ...s, [k]: e.target.value }));
  // a new first day takes the last day with it (same length); the last day can still be changed after
  const setDate = e => {
    const v = e.target.value;
    setF(s => moveFirstDay(s, v));
  };

  const submit = async e => {
    e.preventDefault();
    if (busy) return;
    // an event that is already under way keeps its own start day
    const v = validateForm(f, { today: ev && f.date === ev.date ? null : today, full });
    setErrors(v.errors);
    setFailed('');
    if (Object.keys(v.errors).length) return;
    const b = v.body;
    const payload = ev
      ? { id: ev.id, title: b.title, date: b.date, ...(full ? { end_date: b.end_date, time: b.time, tickers: b.tickers, note: b.note } : {}) }
      : {
          title: b.title,
          date: b.date,
          ...(b.end_date ? { end_date: b.end_date } : {}),
          ...(b.time ? { time: b.time } : {}),
          ...(b.tickers.length ? { tickers: b.tickers } : {}),
          ...(b.note ? { note: b.note } : {})
        };
    setBusy(true);
    const r = await onWrite(ev ? 'PUT' : 'POST', payload);
    setBusy(false);
    if (!r.ok) setFailed(r.error);
  };

  return (
    <form className="mui-cal__det mui-cal__form" data-form={ev ? 'edit' : 'add'} noValidate onSubmit={submit} aria-labelledby={formId}>
      <div className="mui-cal__dethead">
        <span className="mui-cal__formtitle" id={formId}>
          {ev ? 'Edit event' : 'Add an event'}
        </span>
        <CloseButton onClose={onCancel} />
      </div>
      <div className="mui-cal__detbody">
        <Field label="Event name" error={errors.title}>
          <input name="title" type="text" value={f.title} onChange={set('title')} maxLength={200} autoComplete="off" data-autofocus placeholder="Dentist, tax payment, call with the bank…" aria-invalid={!!errors.title} />
        </Field>
        <div className="mui-cal__fieldrow">
          <Field label="Date" error={errors.date}>
            <input name="date" type="date" value={f.date} onChange={setDate} min={today || undefined} aria-invalid={!!errors.date} />
          </Field>
          {full ? (
            <>
              <Field label="Last day" hint="optional" error={errors.end}>
                <input name="end" type="date" value={f.end} onChange={set('end')} min={f.date || today || undefined} aria-invalid={!!errors.end} title="For an event that lasts several days" />
              </Field>
              <Field label="Time" hint="Eastern, optional" error={errors.time} half>
                <input name="time" type="time" value={f.time} onChange={set('time')} aria-invalid={!!errors.time} />
              </Field>
            </>
          ) : null}
        </div>
        {full ? (
          <>
            <Field label="Stocks" hint="optional" error={errors.tickers}>
              <input name="tickers" type="text" value={f.tickers} onChange={set('tickers')} maxLength={120} autoComplete="off" autoCapitalize="characters" spellCheck={false} placeholder="NVDA, SMCI" aria-invalid={!!errors.tickers} />
            </Field>
            <Field label="Note" hint="optional" error={errors.note}>
              <textarea name="note" rows={2} value={f.note} onChange={set('note')} maxLength={500} aria-invalid={!!errors.note} />
            </Field>
          </>
        ) : (
          <p className="mui-cal__detsub">This is a reminder, so it has a name and a day only.</p>
        )}
        {failed ? (
          <p className="mui-cal__err" role="alert">
            {failed}
          </p>
        ) : null}
      </div>
      <div className="mui-cal__acts">
        <button type="submit" className="mui-btn" data-act="save" disabled={busy}>
          {busy ? 'Saving…' : ev ? 'Save changes' : 'Add to calendar'}
        </button>
        <button type="button" className="mui-btn mui-btn--ghost" data-act="cancel" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
