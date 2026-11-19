// rhExposure — "What do I really own once ETFs are looked through?" (S3 §3 F4.2, contracts §6
// /api/rh/exposure). React + SVG squarified treemap (built from scratch: React Bits has no chart
// components, S3 §2): tile area = total_pct, fill = day change on a diverging scale with a neutral
// midpoint, and every tile carries sign + ▲/▼ in its label / aria-label / tooltip. Click or Enter
// shows the breakdown and emits mason:chart-symbol. Account pills are filters only.
// A1-UI (RULINGS #53.5): in the family view (data.view === 'family', display only) tiles never
// emit mason:chart-symbol (no price fetch driven by family holdings) and the foot shows data.note.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRhApi } from '../../lib/api.js';
import { RhState, Loading } from '../../lib/states.jsx';
import { emitChartSymbol } from '../../lib/bridge.js';
import { num, pct, date, isNum, DASH } from '../../lib/fmt.js';
import { squarify } from './squarify.js';
import { breakdownText, dayText, divergingStep, stepFill, inkFor, STEPS, CLIP, capNodes, otherText, TILE_CAP, count } from './model.js';
import { fin, moreText, PCT_MAX } from '../rhAccounts/guard.js';
import { useAccountScopes, accountLabel } from '../rhAccounts/scope.js';
import './rhExposure.css';

const PILL_CAP = 20; // account pills; the rest are counted
const ACCT_CAP = 6; // account names in a tooltip / detail line
const names = (accts, family) =>
  accts.length > ACCT_CAP
    ? `${accts.slice(0, ACCT_CAP).map(a => accountLabel(a, family)).join(', ')} ${moreText(accts.length - ACCT_CAP, 'accounts')}`
    : accts.map(a => accountLabel(a, family)).join(', ');

export const FAMILY_VIEW = 'family';
export const isFamilyView = d => !!d && typeof d === 'object' && d.view === FAMILY_VIEW;
const NOTE_MAX = 160;
const FAMILY_NOTE = 'family account — display only';
/** Foot text of the family view: the server's plain-text data.note (React-escaped, capped), else a fixed line. */
export const familyNote = d => {
  const n = d && typeof d.note === 'string' ? d.note.trim() : '';
  return n ? n.slice(0, NOTE_MAX) : FAMILY_NOTE;
};

const CHART_ID = 'main'; // the page/dashboard chart (F6 routes it; see packet OPEN QUESTIONS)
const FALLBACK = { w: 420, h: 220 }; // before the first ResizeObserver tick (and in jsdom)
const GAP = 1; // half of the 2 px surface gap between tiles

function useSize(ref) {
  const [size, setSize] = useState(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const read = () => {
      const r = el.getBoundingClientRect();
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      setSize(s => (w > 0 && h > 0 && (!s || s.w !== w || s.h !== h) ? { w, h } : s));
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, [ref]);
  return size || FALLBACK;
}

function Pills({ account, setAccount, scopes }) {
  const pill = (key, label, value, title, extra = {}) => (
    <button
      key={key}
      type="button"
      className={`rhx-pill${account === value ? ' is-on' : ''}`}
      aria-pressed={account === value}
      title={title}
      onClick={() => setAccount(value)}
      {...extra}
    >
      {label}
    </button>
  );
  return (
    <div className="rhx-pills" role="group" aria-label="Account filter">
      {pill('mine', 'Mine', '', 'Mine: personal accounts only (family accounts excluded) — the server default scope')}
      {pill('all', 'All', 'all', 'All your accounts (accounts excluded from Mason, e.g. family, are never included)')}
      {scopes.list.slice(0, PILL_CAP).map(a =>
        pill(`a-${a.account}`, accountLabel(a.account, scopes.family), a.account, `Only ${a.account}`)
      )}
      {scopes.list.length > PILL_CAP ? (
        <span className="rhx-pills__more">{moreText(scopes.list.length - PILL_CAP, 'accounts')}</span>
      ) : null}
      {pill(
        'manual',
        'Manual',
        '__manual__',
        'Manual-only exposure has no API parameter yet: /api/rh/exposure?account= takes "all" or one account label (hand-entered rows are already included in Mine).',
        { disabled: true }
      )}
    </div>
  );
}

// memo: a tooltip move or a selection change re-renders only the tiles whose props changed
const Tile = memo(function Tile({ t, sel, onPick, onTip, display }) {
  const n = t.item;
  const step = divergingStep(n.day_chg_pct);
  const x = t.x + GAP;
  const y = t.y + GAP;
  const w = Math.max(0, t.w - 2 * GAP);
  const h = Math.max(0, t.h - 2 * GAP);
  const label = n.other ? otherText(n) : `${breakdownText(n)}; ${dayText(n.day_chg_pct)}`;
  // dataviz: a label is drawn inside a tile only when it fits with padding (≈6.6 px/char at
  // 11-12 px); otherwise it lives in the tooltip / aria-label / detail panel, never clipped.
  const vText = n.other ? `${num(n.total_pct, 1)}%` : `${num(n.total_pct, 1)}% · ${dayText(n.day_chg_pct).replace('day ', '')}`;
  const tk = n.other ? `+${count(n.count)} more` : String(n.ticker);
  const fits = s => s.length * 6.6 + 12 <= w;
  const big = h >= 40 && fits(vText) && fits(tk);
  const mid = !big && h >= 18 && fits(tk);
  const show = () => onTip(t);
  return (
    <g
      className={`rhx-tile ${inkFor(step)}${sel ? ' is-sel' : ''}${n.other ? ' is-other' : ''}${display ? ' is-display' : ''}`}
      data-testid={`tile-${n.ticker}`}
      role={display ? 'img' : 'button'}
      tabIndex={0}
      aria-label={label}
      aria-pressed={display ? undefined : sel}
      onClick={display ? undefined : () => onPick(n)}
      onKeyDown={
        display
          ? undefined
          : e => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onPick(n);
              }
            }
      }
      onPointerEnter={show}
      onPointerLeave={() => onTip(null)}
      onFocus={show}
      onBlur={() => onTip(null)}
    >
      <rect x={x} y={y} width={w} height={h} rx={Math.min(4, w / 4, h / 4)} style={{ fill: stepFill(step) }} />
      {big || mid ? (
        <text x={x + 6} y={y + (big ? 16 : h / 2 + 4)} className="rhx-tile__tk" aria-hidden="true">
          {tk}
        </text>
      ) : null}
      {big ? (
        <text x={x + 6} y={y + 31} className="rhx-tile__v" aria-hidden="true">
          {vText}
        </text>
      ) : null}
    </g>
  );
});

function Tip({ t, size, family }) {
  if (!t) return null;
  const n = t.item;
  const W = 230;
  const left = Math.max(4, Math.min(size.w - W - 4, t.x + t.w / 2 - W / 2));
  const below = t.y + t.h / 2 < size.h / 2;
  const style = below ? { left, top: Math.min(size.h - 8, t.y + t.h / 2 + 10) } : { left, bottom: Math.max(4, size.h - (t.y + t.h / 2) + 10) };
  const accts = Array.isArray(n.accounts) ? n.accounts.map(String) : [];
  if (n.other) {
    return (
      <div className="rhx-tip" role="tooltip" style={{ ...style, width: W }}>
        <b className="rhx-tip__v">{otherText(n)}</b>
        <span className="mui-muted">smaller names folded into one tile</span>
      </div>
    );
  }
  return (
    <div className="rhx-tip" role="tooltip" style={{ ...style, width: W }}>
      <b className="rhx-tip__v">{breakdownText(n)}</b>
      <span>{dayText(n.day_chg_pct)}</span>
      <span className="mui-muted">
        {accts.length ? `held directly in ${names(accts, family)}` : 'via ETFs only'}
      </span>
    </div>
  );
}

function Detail({ n, family, display }) {
  if (!n) {
    return (
      <div className="rhx-detail is-hint" data-testid="exp-detail">
        {display
          ? 'Hover or focus a tile for its breakdown (family view: display only, not charted).'
          : 'Select a tile for its breakdown (it also charts the ticker).'}
      </div>
    );
  }
  if (n.other) {
    return (
      <div className="rhx-detail" data-testid="exp-detail" aria-live="polite">
        <b>{otherText(n)}</b>
        <span className="mui-muted">folded into one tile; each is smaller than the {TILE_CAP} drawn</span>
      </div>
    );
  }
  const accts = Array.isArray(n.accounts) ? n.accounts.map(String) : [];
  const legs = [
    ...(isNum(n.direct_pct) && n.direct_pct > 0 ? [{ k: 'direct', v: n.direct_pct }] : []),
    ...(Array.isArray(n.via) ? n.via.filter(v => v && fin(v.pct, PCT_MAX) != null).slice(0, 8).map(v => ({ k: String(v.etf), v: v.pct })) : [])
  ];
  const tot = legs.reduce((a, l) => a + l.v, 0) || 1;
  return (
    <div className="rhx-detail" data-testid="exp-detail" aria-live="polite">
      <b>{breakdownText(n)}</b>
      <span className="rhx-legs" aria-hidden="true">
        {legs.map((l, i) => (
          <span key={`${l.k}-${i}`} className={`rhx-leg rhx-leg--${i % 3}`} style={{ flexGrow: l.v / tot }} title={`${l.k} ${num(l.v, 1)}%`} />
        ))}
      </span>
      <span>{dayText(n.day_chg_pct)}</span>
      <span className="mui-muted">
        {accts.length ? `direct in ${names(accts, family)}` : 'no direct holding'}
      </span>
    </div>
  );
}

function Legend() {
  const steps = [];
  for (let s = -STEPS; s <= STEPS; s += 1) steps.push(s);
  return (
    <span className="rhx-legend" aria-label={`Color = day change, ▼ -${CLIP}% to ▲ +${CLIP}%, gray = flat`}>
      <span aria-hidden="true">▼ -{CLIP}%</span>
      <span className="rhx-legend__ramp" aria-hidden="true">
        {steps.map(s => (
          <span key={s} style={{ background: stepFill(s) }} />
        ))}
      </span>
      <span aria-hidden="true">+{CLIP}% ▲</span>
    </span>
  );
}

function Treemap({ data, family, refetching }) {
  const wrap = useRef(null);
  const size = useSize(wrap);
  const [sel, setSel] = useState(null);
  const [tip, setTip] = useState(null);
  const capped = useMemo(() => capNodes(data.nodes), [data]);
  const nodes = capped.tiles;
  const tiles = useMemo(() => squarify(nodes, { x: 0, y: 0, w: size.w, h: size.h }, n => n.total_pct), [nodes, size.w, size.h]);
  const isCapped = nodes.some(n => n.other);
  const coverage = fin(data.coverage_pct, PCT_MAX);
  const display = isFamilyView(data); // family view: nothing a click can do reaches the chart or the network
  const selNode = !display && sel ? nodes.find(n => n.ticker === sel) || null : null;
  const pick = useCallback(
    n => {
      if (display) return;
      setSel(n.ticker);
      if (!n.other) emitChartSymbol(CHART_ID, n.ticker);
    },
    [display]
  );
  return (
    <>
      <div className={`rhx-map${refetching ? ' is-refetching' : ''}`} ref={wrap}>
        {tiles.length ? (
          <svg width={size.w} height={size.h} viewBox={`0 0 ${size.w} ${size.h}`} role="group" aria-label="True exposure treemap">
            {tiles.map(t => (
              <Tile key={t.item.ticker} t={t} sel={!display && t.item.ticker === sel} onPick={pick} onTip={setTip} display={display} />
            ))}
          </svg>
        ) : (
          <div className="mui-state mui-state--empty">No holdings in this scope.</div>
        )}
        <Tip t={tip} size={size} family={family} />
      </div>
      <Detail n={selNode} family={family} display={display} />
      <div className="rhx-foot" data-testid="exp-coverage">
        {display ? (
          <span className="rhx-cov" data-testid="exp-family-note" title="Family view: weights at cost, display only">
            {familyNote(data)}
          </span>
        ) : (
          <span
            className="rhx-cov"
            title="coverage_pct from /api/rh/exposure: share of in-scope equity dollars whose underlying names are known (direct stocks + the known part of each ETF)"
          >
            ETF look-through covers {coverage == null ? DASH : pct(coverage, { signed: false })} of holding dollars
          </span>
        )}
        {capped.all ? (
          <span className="mui-n">
            {count(capped.all)} names{isCapped ? '' : ' shown'} = {pct(capped.shownPct, { signed: false })} of exposure
            {isCapped ? ` · top ${TILE_CAP} drawn, the rest in one tile` : ''}
            {capped.unsized ? ` · ${count(capped.unsized)} without a size not drawn` : ''}
          </span>
        ) : null}
        {!display && data.as_of_holdings ? <span className="mui-n">ETF holdings as of {date(data.as_of_holdings)}</span> : null}
        <Legend />
      </div>
    </>
  );
}

export default function RhExposure({ config, saveConfig }) {
  const [account, setAccountState] = useState(typeof config.account === 'string' ? config.account : '');
  const scopes = useAccountScopes();
  const api = useRhApi('/api/rh/exposure', account ? { account } : null, { pollMs: 60000 });
  const last = useRef(null);
  if (api.status === 'ok' && api.data) last.current = api.data;
  const setAccount = a => {
    if (a === '__manual__') return;
    setAccountState(a);
    if (typeof saveConfig === 'function') saveConfig({ ...config, account: a });
  };
  const holding = api.status === 'loading' && last.current;
  return (
    <div className="rhx">
      <Pills account={account} setAccount={setAccount} scopes={scopes} />
      {holding ? (
        // dataviz: refetch keeps the frame (previous render at reduced opacity, no skeleton flash)
        <Treemap data={last.current} family={scopes.family} refetching />
      ) : api.status === 'loading' ? (
        <Loading rows={4} />
      ) : (
        <RhState api={api} empty={d => !d || typeof d !== 'object'} emptyText="No exposure data.">
          {d => <Treemap data={d} family={scopes.family} />}
        </RhState>
      )}
    </div>
  );
}
