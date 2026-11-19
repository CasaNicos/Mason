// rhStories — Trade Stories (S3 §3 F5.4; contracts §6 GET /api/rh/history/stories +
// POST /api/rh/history/stories/<trade_id> {thesis, verdict, note} | {skip:true}).
// Built on React Bits <Stepper>: one closed trade per step, at most 5 a day.
//  - The step advances ONLY after a 2xx (no optimistic write): the Stepper's own Next button is
//    hidden and clicked programmatically once postRh() returns ok. Back / indicator jumps are off,
//    so an answered story can't be re-posted from this widget.
//  - Thesis ≤ 280 characters with a live counter; 281+ shows an inline error and blocks submit
//    (the backend requires a non-empty thesis, mason_rh_history._validate). Exactly 4 verdicts.
//  - API errors (e.g. the _post_invalid fixture) show inline; the form keeps what was typed.
//  - Family accounts (scope from /api/rh/accounts) are never offered; the backend excludes them too.
//  - K6: the GET has no pool count, so the done line says "Done for today" without an N.
//  - The queue is frozen for the session (no polling): answering never reshuffles the steps.
import { useId, useMemo, useRef, useState } from 'react';
import Stepper, { Step } from '../../reactbits/Stepper/Stepper.jsx';
import { postRh, useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { DASH, arrow, date, dirClass, money, num } from '../../lib/fmt.js';
import { fin, isFamily, useAccountScopes } from '../rhPvr/f5shared.jsx';
import './stories.css';

const PER_DAY = 5;
const MAX = 280;
const NOTE_MAX = 1000;
const ROUTE = '/api/rh/history/stories';
const VERDICTS = [
  ['right', 'Right', 'The thesis was right and the trade worked for the reason you expected.'],
  ['wrong', 'Wrong', 'The thesis was wrong: the result came from something you missed.'],
  ['luck', 'Luck', 'The outcome came from luck, not from your thesis.'],
  ['timing', 'Timing', 'The idea was right, but the entry or exit timing was off.']
];

function TradeCard({ r }) {
  const pl = r.realized_pl;
  return (
    <div className="mui-story__trade" data-testid="story-step">
      <div className="mui-story__tl1">
        <b className="mui-story__tk">{String(r.symbol ?? DASH)}</b>
        <span className="mui-story__tag">{String(r.kind ?? DASH)}</span>
        {r.slot ? <span className="mui-story__tag">{String(r.slot)}</span> : null}
        {fin(pl) ? (
          <span className={`mui-story__pl ${dirClass(pl)}`}>
            {arrow(pl)} {money(pl, { signed: true })} {Math.abs(pl) >= 0.005 ? (pl > 0 ? 'won' : 'lost') : ''}
          </span>
        ) : (
          <span className="mui-story__pl mui-muted">P&amp;L —</span>
        )}
      </div>
      <div className="mui-story__tl2 mui-n">
        <span>{String(r.account ?? DASH)}</span>
        <span>
          {String(r.side ?? '')} {fin(r.qty) ? num(r.qty, r.qty % 1 ? 4 : 0) : DASH} @ {money(r.price)}
        </span>
        <span>closed {date(r.closed_at)}</span>
        <span>held {fin(r.holding_days) ? `${num(r.holding_days, 1)} days` : DASH}</span>
      </div>
    </div>
  );
}

function Indicator({ step, currentStep, total }) {
  const st = currentStep === step ? 'active' : currentStep > step ? 'complete' : 'inactive';
  const word = st === 'complete' ? 'done' : st === 'active' ? 'current' : 'to do';
  return (
    <span className={`mui-story__ind is-${st}`} aria-label={`Story ${step} of ${total}, ${word}`}>
      {st === 'complete' ? '✓' : step}
    </span>
  );
}

function Queue({ rows }) {
  const uid = useId();
  const nextRef = useRef(null);
  const [cur, setCur] = useState(0);
  const [done, setDone] = useState(false);
  const [thesis, setThesis] = useState('');
  const [verdict, setVerdict] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState(null);
  const [tally, setTally] = useState({ saved: 0, skipped: 0 });

  const total = rows.length;
  const r = rows[Math.min(cur, total - 1)];
  const len = thesis.length;
  const over = len > MAX;
  const canSave = !busy && !done && !over && thesis.trim().length > 0 && VERDICTS.some(v => v[0] === verdict);

  // F5-1 (DEBUG_F5): `busy` is React state, so several clicks / submits fired in one task all saw
  // busy=false and POSTed the same trade, then each success advanced a step (later stories were
  // "answered" without a POST). Guards, all synchronous refs:
  //  - inflight: one request at a time, checked before anything else, cleared in finally;
  //  - answered: each trade id is answered at most once;
  //  - curId: the step advances only if the step on screen is still the trade that was posted.
  const inflight = useRef(false);
  const answered = useRef(new Set());
  const curId = useRef(null);
  curId.current = !done && r ? String(r.id) : null;

  const send = async body => {
    if (inflight.current || done || !r) return;
    const id = String(r.id);
    if (answered.current.has(id)) return;
    inflight.current = true;
    setBusy(true);
    setError(null);
    let res;
    try {
      res = await postRh(`${ROUTE}/${encodeURIComponent(id)}`, body, {
        mockSlug: body.skip ? 'rh_history_stories_post_skip' : 'rh_history_stories_post'
      });
    } finally {
      inflight.current = false;
      setBusy(false);
    }
    if (!res || !res.ok) {
      setError((res && res.error) || 'The story could not be saved.');
      return;
    }
    if (answered.current.has(id)) return;
    answered.current.add(id);
    const lesson = res.data && typeof res.data === 'object' ? res.data.lesson_id : null;
    setStatus(body.skip ? `Skipped ${String(r.symbol ?? '')}` : `Saved → lesson ${lesson ? String(lesson) : DASH}`);
    setTally(t => (body.skip ? { ...t, skipped: t.skipped + 1 } : { ...t, saved: t.saved + 1 }));
    setThesis('');
    setVerdict('');
    setNote('');
    // advance only after the 2xx, exactly once, and only if that trade is still the step on screen
    if (curId.current === id && nextRef.current) nextRef.current.click();
  };

  return (
    <div className="mui-story">
      <div className="mui-story__bar">
        <span data-testid="story-progress">{done ? `${total} of ${total} answered` : `Story ${cur + 1} of ${total}`}</span>
        <span className="mui-n">up to {PER_DAY} a day</span>
        {status ? (
          <span className="mui-story__status" role="status" data-testid="story-status">
            {status}
          </span>
        ) : null}
      </div>
      <Stepper
        initialStep={1}
        onStepChange={s => setCur(s - 1)}
        onFinalStepCompleted={() => setDone(true)}
        disableStepIndicators
        renderStepIndicator={p => <Indicator {...p} total={total} />}
        nextButtonProps={{ ref: nextRef, tabIndex: -1, 'aria-hidden': 'true', className: 'mui-story__hidden-next' }}
        backButtonProps={{ hidden: true, tabIndex: -1, 'aria-hidden': 'true' }}
        stepCircleContainerClassName="mui-story__card"
        footerClassName="mui-story__stepfoot"
      >
        {rows.map(row => (
          <Step key={String(row.id)}>
            <TradeCard r={row} />
          </Step>
        ))}
      </Stepper>
      {done ? (
        <div className="mui-story__done" data-testid="story-done" role="status">
          <b>Done for today.</b> {tally.saved} saved · {tally.skipped} skipped.
        </div>
      ) : (
        <form
          className="mui-story__form"
          onSubmit={e => {
            e.preventDefault();
            if (canSave) send({ thesis: thesis.trim(), verdict, note: note.trim() });
          }}
        >
          <div className="mui-story__field">
            <label htmlFor={`${uid}-thesis`} className="mui-story__lab">
              Thesis <span className="mui-n">why did you take this trade?</span>
            </label>
            <textarea
              id={`${uid}-thesis`}
              rows={2}
              value={thesis}
              aria-invalid={over ? 'true' : 'false'}
              aria-describedby={`${uid}-count`}
              onChange={e => setThesis(e.target.value)}
              disabled={busy}
            />
            <div className="mui-story__countrow">
              {over ? (
                <span className="mui-story__err" role="alert">
                  {len - MAX} character{len - MAX === 1 ? '' : 's'} over the {MAX} limit — shorten it to save.
                </span>
              ) : (
                <span />
              )}
              <span id={`${uid}-count`} className={`mui-story__count${over ? ' is-over' : ''}`} data-testid="story-count">
                {len}/{MAX}
              </span>
            </div>
          </div>
          <fieldset className="mui-story__verdicts" disabled={busy}>
            <legend className="mui-story__lab">Verdict</legend>
            {VERDICTS.map(([v, name, def]) => (
              <label key={v} className={`mui-story__v${verdict === v ? ' is-on' : ''}`}>
                <input type="radio" name={`${uid}-verdict`} value={v} checked={verdict === v} onChange={() => setVerdict(v)} />
                <span className="mui-story__vname">{name}</span> <span className="mui-story__vdef">{def}</span>
              </label>
            ))}
          </fieldset>
          <div className="mui-story__field">
            <label htmlFor={`${uid}-note`} className="mui-story__lab">
              Note <span className="mui-n">optional</span>
            </label>
            <input id={`${uid}-note`} type="text" value={note} maxLength={NOTE_MAX} onChange={e => setNote(e.target.value)} disabled={busy} />
          </div>
          {error ? (
            <div className="mui-story__apierr" role="alert">
              {String(error)}
            </div>
          ) : null}
          <div className="mui-story__actions">
            <button type="submit" className="mui-btn" disabled={!canSave}>
              Save story
            </button>
            <button type="button" className="mui-btn mui-btn--ghost" disabled={busy} onClick={() => send({ skip: true })}>
              Skip
            </button>
            {busy ? <span className="mui-n">saving…</span> : null}
          </div>
        </form>
      )}
    </div>
  );
}

function Stories({ data, scopes }) {
  // Freeze the day's queue the first time it arrives (answers never reshuffle the steps).
  const [frozen] = useState(() => (Array.isArray(data) ? data.filter(x => x && typeof x === 'object' && x.id != null) : []));
  const rows = useMemo(() => frozen.filter(x => !isFamily(x.account, scopes)).slice(0, PER_DAY), [frozen, scopes]);
  if (!rows.length) {
    return (
      <div className="mui-story__empty" data-testid="story-empty">
        <b>No stories today.</b>
        <span>No closed trade qualifies for a story right now. When one does, up to {PER_DAY} a day will appear here.</span>
      </div>
    );
  }
  return <Queue rows={rows} />;
}

export default function RhStories() {
  const api = useRhApi(ROUTE);
  const scopes = useAccountScopes();
  return (
    <div className="mui-storiesw">
      <RhState api={api} empty={() => false}>
        {d => <Stories data={d} scopes={scopes} />}
      </RhState>
    </div>
  );
}

