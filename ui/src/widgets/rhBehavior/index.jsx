// rhBehavior — Behavior Coach (S3 §3 F5.2; contracts §6 GET /api/rh/behavior; S2-T3).
// React Bits <Stack> deck, one card per finding: the behavior, what it cost in words, n + 95% CI,
// and ONE suggested rule.
//  - Sign convention (S2 Q-T3-2): in Behavior a NEGATIVE `dollars` is a cost → "cost you $X";
//    positive → "gained you $X"; null → "no dollar estimate". Stated in words on the widget.
//  - K6: §6 has no `rule` field. The S2-T3 finding text carries exactly one
//    "Suggestion for Nico: …" sentence, so the rule is split out of the route's own text. When a
//    finding has no such sentence we say so and invent nothing (open question: a `rule` field).
//  - Significant cards are solid; the rest are muted AND read "Early signal (n=…)" (text, not
//    opacity alone). Order: significant first, then by dollar size; the top card is the costliest.
//  - Stack autoplay is OFF (no continuous loop). Drag a card, or use Prev/Next (keyboard).
//  - UIFIX 2026-11-18: every card carries a key that belongs to its finding (its place in the sorted deck), and
//    Stack keeps that key as the card's id (M-Stack-2). ‹ › therefore MOVE the same cards to new slots and
//    Stack's own spring plays; before, the cards were re-numbered by slot on every click, so only their text
//    was swapped and nothing animated. A dragged-away top card advances the same counter (onSendToBack).
import { useCallback, useMemo, useRef, useState } from 'react';
import Stack from '../../reactbits/Stack/Stack.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { DASH, arrow, dirClass, money, num, pct } from '../../lib/fmt.js';
import { fin, nl } from '../rhPvr/f5shared.jsx';
import './behavior.css';

const SUGGEST = 'Suggestion for Nico:';
// The Stack fan shows ~4 cards; render a window of at most DECK_MAX from the current position so a
// 10k-finding route never mounts 10k cards. Prev/Next still cycle through all of them.
const DECK_MAX = 12;
// Display names + value units for the S2-T3 metrics. Unknown metrics show their raw name.
const METRICS = {
  disposition: ['Sell winners fast, hold losers', 'x'],
  weekday: ['Weekday effect', '$'],
  chase_5: ['Chasing a +5% day · 5 sessions', '%'],
  chase_20: ['Chasing a +5% day · 20 sessions', '%'],
  dip_5: ['Buying a −5% day · 5 sessions', '%'],
  dip_20: ['Buying a −5% day · 20 sessions', '%'],
  earnings_hold: ['Holding through earnings', '$'],
  size_drift: ['Sizing up after a loss', 'x'],
  round_trip: ['Quick round trips', '$'],
  regime_fit: ['Regime fit', '%'],
  exit_quality: ['Exit quality', '%']
};

function fmtUnit(v, unit) {
  if (!fin(v)) return DASH;
  if (unit === '%') return pct(v * 100, { digits: 1 });
  if (unit === '$') return money(v, { signed: true });
  if (unit === 'x') return `${num(v, 1)}×`;
  return num(v, 2);
}

function splitFinding(text) {
  const s = typeof text === 'string' ? text : '';
  const i = s.indexOf(SUGGEST);
  if (i < 0) return { finding: s.trim(), rule: null };
  const rule = s.slice(i + SUGGEST.length).trim();
  return { finding: s.slice(0, i).trim(), rule: rule || null };
}

function costWords(d) {
  if (!fin(d)) return 'no dollar estimate';
  if (Math.abs(d) < 0.005) return 'no cost either way';
  return d < 0 ? `cost you ${money(-d)}` : `gained you ${money(d)}`;
}

function Card({ f }) {
  const sig = f.significant === true;
  const [name, unit] = METRICS[f.metric] || [String(f.metric ?? 'finding'), ''];
  const { finding, rule } = splitFinding(f.finding);
  const ci = Array.isArray(f.ci95) && f.ci95.length === 2 && f.ci95.every(fin) ? f.ci95 : null;
  return (
    <article className={`mui-beh ${sig ? 'mui-beh--sig' : 'mui-beh--early'}`} aria-label={`${name}: ${costWords(f.dollars)}`}>
      <header className="mui-beh__head">
        <span className={`mui-beh__badge${sig ? ' is-sig' : ''}`}>
          {sig ? `Significant (${nl(f.n)})` : `Early signal (${nl(f.n)})`}
        </span>
        <span className="mui-beh__metric">{name}</span>
      </header>
      <div className={`mui-beh__cost ${dirClass(f.dollars)}`}>
        {fin(f.dollars) && Math.abs(f.dollars) >= 0.005 ? <span aria-hidden="true">{arrow(f.dollars)} </span> : null}
        {costWords(f.dollars)}
      </div>
      <p className="mui-beh__finding">{finding}</p>
      <div className="mui-beh__stat mui-n">
        you {fmtUnit(f.value, unit)} vs baseline {fmtUnit(f.baseline, unit)}
        {ci ? ` · 95% CI ${fmtUnit(ci[0], unit)} to ${fmtUnit(ci[1], unit)}` : ' · 95% CI —'} · {nl(f.n)}
      </div>
      {rule ? (
        <div className="mui-beh__rule">
          <b>Suggested rule</b> {rule}
        </div>
      ) : (
        <div className="mui-beh__norule mui-muted">No suggested rule in the data for this finding.</div>
      )}
    </article>
  );
}

const rank = f => [f.significant === true ? 0 : 1, fin(f.dollars) ? -Math.abs(f.dollars) : 1];

function Deck({ findings }) {
  const sorted = useMemo(
    () =>
      findings
        .map((f, i) => ({ f, i }))
        .sort((a, b) => {
          const [a0, a1] = rank(a.f);
          const [b0, b1] = rank(b.f);
          return a0 - b0 || a1 - b1 || a.i - b.i;
        })
        .map(x => x.f),
    [findings]
  );
  const [k, setK] = useState(0);
  const N = sorted.length;
  const off = N ? ((k % N) + N) % N : 0;
  // Stack draws the LAST card on top: rotate so sorted[off] is on top, then reverse.
  // Each card's key is its finding's place in the sorted deck, so it stays the same card while it moves.
  const cards = useMemo(() => {
    const win = Array.from({ length: Math.min(N, DECK_MAX) }, (_, j) => (off + j) % N);
    return win.reverse().map(i => <Card key={i} f={sorted[i]} />);
  }, [sorted, off, N]);
  // Dragging the top card away is the same step as ›: keep the counter in step with what Stack shows.
  const topId = useRef(null);
  topId.current = N ? `k:${off}` : null;
  const onSendToBack = useCallback(id => {
    if (id === topId.current) setK(v => v + 1);
  }, []);
  const sig = sorted.filter(f => f.significant === true).length;
  return (
    <div className="mui-beh-wrap">
      <div className="mui-beh-bar">
        <span data-testid="beh-summary">
          {num(N)} findings · {num(sig)} significant · {num(N - sig)} early signal
        </span>
        <span className="mui-beh-nav">
          <button type="button" className="mui-btn mui-btn--ghost" aria-label="Previous card" onClick={() => setK(v => v - 1)}>
            ‹
          </button>
          <button type="button" className="mui-btn mui-btn--ghost" aria-label="Next card" onClick={() => setK(v => v + 1)}>
            ›
          </button>
        </span>
      </div>
      <div className="mui-beh-deck">
        <Stack cards={cards} onSendToBack={onSendToBack} sensitivity={90} randomRotation={false} autoplay={false} sendToBackOnClick={false} animationConfig={{ stiffness: 260, damping: 24 }} />
      </div>
      <div className="mui-beh-foot mui-n" data-testid="beh-convention">
        Sign: a negative dollar figure is a cost (“cost you”), a positive one a gain. Drag the top card or use ‹ › to browse.
      </div>
    </div>
  );
}

export default function RhBehavior() {
  const api = useRhApi('/api/rh/behavior');
  return (
    <div className="mui-behw">
      <RhState
        api={api}
        empty={d => !Array.isArray(d) || !d.some(f => f && typeof f === 'object')}
        emptyText="No behavior findings yet. They appear once enough of your closed trades are graded."
      >
        {d => <Deck findings={d.filter(f => f && typeof f === 'object')} />}
      </RhState>
    </div>
  );
}
