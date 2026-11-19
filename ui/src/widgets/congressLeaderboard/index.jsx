// S3-F1 congressLeaderboard — politicians ranked by SHRUNK excess vs SPY at 90 days (page #congress
// only; not a catalog type). Every row shows its n. "Show below min-n" is off by default (K3: the API
// already drops politicians below congress.leaderboard_min_n and the §6 row has no min_n_met; a row
// that ever carries min_n_met false/0 is hidden by default and shown muted, "too few trades to use").
// React Bits: SpotlightCard (one per row), CountUp (the shrunk excess), ShinyText (the #1 name; the
// page's only continuous effect, gated by useMotionAllowed inside ShinyText).
import { useMemo, useState } from 'react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import ShinyText from '../../reactbits/ShinyText/ShinyText.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { pct, arrow, dirClass, isNum, nLabel } from '../../lib/fmt.js';
import { ROUTE, POLL_MS, boardOf } from '../congressFeed/congressLib.js';
import './congressLeaderboard.css';

const belowMinN = r => r.min_n_met === false || r.min_n_met === 0;
const SHRUNK_HELP =
  'Shrunk excess: the mean 90-day excess return vs SPY pulled toward zero in proportion to how few graded trades a politician has, so a lucky small sample cannot top the board.';

const CAP = 50; // F1-1: ~500 members of Congress → at most 50 published + 50 below-min-n cards

/** A fraction as percent, or null unless the SCALED value is finite (F1-4: 1e308 * 100 = Infinity). */
const pctOf = x => (isNum(x) && Number.isFinite(x * 100) ? x * 100 : null);

/** Published rows first (by shrunk excess, high → low), below-min-n rows after. */
function rankRows(list) {
  const key = r => {
    const v = pctOf(r.shrunk_xs);
    return v == null ? -Infinity : v;
  };
  const by = (a, b) => key(b) - key(a);
  const all = list.slice();
  return [...all.filter(r => !belowMinN(r)).sort(by), ...all.filter(belowMinN).sort(by)];
}

function Bar({ v, scale }) {
  if (!isNum(v) || !(scale > 0)) return <div className="lb-bar" aria-hidden="true" />;
  const w = Math.min(50, (Math.abs(v) / scale) * 50);
  const style = v >= 0 ? { left: '50%', width: `${w}%` } : { left: `${50 - w}%`, width: `${w}%` };
  return (
    <div className="lb-bar" aria-hidden="true">
      <span className="lb-bar__zero" />
      <span className={`lb-bar__fill ${dirClass(v)}`} style={style} />
    </div>
  );
}

function Row({ r, pos, scale, motionName }) {
  const muted = belowMinN(r);
  const v = pctOf(r.shrunk_xs);
  const abs0 = v == null ? null : Math.round(Math.abs(v) * 100) / 100;
  const abs = abs0 != null && Number.isFinite(abs0) ? abs0 : null;
  const name = typeof r.politician === 'string' && r.politician ? r.politician : '—';
  const rank = isNum(r.rank) ? Math.round(r.rank) : pos + 1;
  return (
    <div className={`lb-row${muted ? ' is-muted' : ''}`} data-testid="lb-row">
      <SpotlightCard className="lb-card" spotlightColor="color-mix(in srgb, var(--mui-accent) 20%, transparent)">
        <div className="lb-grid">
          <span className="lb-rank">{muted ? '—' : `#${rank}`}</span>
          <span className="lb-name" title={name}>
            {motionName ? <ShinyText text={name} speed={3.5} delay={2.5} color="var(--mui-text)" shineColor="var(--mui-accent-300)" /> : name}
          </span>
          <span className={`lb-big ${dirClass(v)}`} title={SHRUNK_HELP}>
            {abs == null ? (
              '—'
            ) : (
              <>
                {arrow(v)} {v < 0 && abs !== 0 ? '-' : abs !== 0 ? '+' : ''}
                <CountUp from={0.01} to={abs} duration={0.4} />%
              </>
            )}
          </span>
          <span className="lb-sub">
            <span className="lb-n">{nLabel(r.n)}</span>
            <span>hit {pct(pctOf(r.hit_rate_90), { signed: false })}</span>
            <span>raw {pct(pctOf(r.xs_spy_90), { digits: 2 })}</span>
            {muted ? <span className="lb-few">too few trades to use</span> : null}
          </span>
          <Bar v={v} scale={scale} />
        </div>
      </SpotlightCard>
    </div>
  );
}

function Board({ data, showBelow }) {
  const all = useMemo(() => rankRows(boardOf(data)), [data]);
  const published = useMemo(() => all.filter(r => !belowMinN(r)), [all]);
  const below = useMemo(() => all.filter(belowMinN), [all]);
  // F1-1: hard cap per group, so 10k rows never become 10k SpotlightCards; the cut is always stated.
  const rows = showBelow ? [...published.slice(0, CAP), ...below.slice(0, CAP)] : published.slice(0, CAP);
  const scale = rows.reduce((m, r) => {
    const v = pctOf(r.shrunk_xs);
    return v == null ? m : Math.max(m, Math.abs(v));
  }, 0);
  const capParts = [];
  if (published.length > CAP) capParts.push(`${CAP} of ${published.length.toLocaleString('en-US')} shown`);
  if (showBelow && below.length > CAP) capParts.push(`${CAP} of ${below.length.toLocaleString('en-US')} below min-n shown`);
  return (
    <>
      {showBelow && below.length === 0 ? (
        <p className="lb-note" role="note">
          Only politicians with n ≥ min-n are published by the API, so there is no one below min-n to show.
        </p>
      ) : null}
      {capParts.length ? (
        <p className="lb-cap" role="note" data-testid="lb-cap">
          {capParts.join(' · ')}
        </p>
      ) : null}
      <div className="lb-list" role="list" aria-label="Politicians ranked by shrunk excess vs SPY at 90 days">
        {rows.map((r, i) => (
          <div role="listitem" key={`${typeof r.politician === 'string' ? r.politician : 'x'}-${i}`}>
            <Row r={r} pos={i} scale={scale} motionName={i === 0 && !belowMinN(r)} />
          </div>
        ))}
      </div>
    </>
  );
}

export default function CongressLeaderboard() {
  const api = useRhApi(ROUTE, null, { pollMs: POLL_MS });
  const [showBelow, setShowBelow] = useState(false);
  return (
    <div className="lb">
      <div className="lb-head">
        <span className="lb-title">LEADERBOARD</span>
        <span className="lb-how" title={SHRUNK_HELP}>
          ranked by shrunk excess vs SPY · 90d
        </span>
        <button type="button" className={`lb-toggle${showBelow ? ' is-on' : ''}`} aria-pressed={showBelow} onClick={() => setShowBelow(s => !s)}>
          <span aria-hidden="true">{showBelow ? '◉' : '○'}</span> Show below min-n
        </button>
      </div>
      <div className="lb-body">
        <RhState api={api} empty={d => boardOf(d).length === 0} emptyText="No politician has enough graded trades yet." loadingRows={3}>
          {d => <Board data={d} showBelow={showBelow} />}
        </RhState>
      </div>
    </div>
  );
}
