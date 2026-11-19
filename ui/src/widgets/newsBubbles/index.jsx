// newsBubbles — the Impact map (News page, UI overhaul Task 5, 2026-11-16).
// One bubble per ticker (or per GICS sector), AREA = stories in the window, color = net sentiment
// (bullish − bearish) / stories, ALWAYS paired with ▲ / ▼ / • in the label and the accessible name.
// A dashed ring marks material stories. Click (or Enter / Space) filters every News widget to that
// ticker / sector; clicking it again clears the filter (Esc too).
// React Bits: ClickSpark (through MasonUI.effects.spark, lib/effects.js) bursts on a pick.
// Built here: the cluster layout (pack.js — no React Bits fit, see there) drawn as SVG; bubbles
// spring into place with `motion` (MotionConfig reducedMotion="user" makes that instant).
// Reflow: a ResizeObserver re-packs the cluster to the widget's shape on every Gridstack resize.
import { useCallback, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { spark } from '../../lib/effects.js';
import { num } from '../../lib/fmt.js';
import { toggleSector, toggleTicker, useNews } from '../newsShared/store.js';
import { sentiment, shortSector } from '../newsShared/util.js';
import { NewsState } from '../newsShared/parts.jsx';
import { useBox } from '../newsShared/useBox.js';
import { bubbleBudget, packBubbles } from './pack.js';
import '../newsShared/news.css';
import './newsBubbles.css';

const TONE_TOKEN = { up: ['--color-positive', '#5fd1a0'], down: ['--color-negative', '#e0768a'], flat: ['--color-accent-300', '#8dc4ff'] };
function tokenColor(tone) {
  const [name, dflt] = TONE_TOKEN[tone] || TONE_TOKEN.flat;
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || dflt;
  } catch {
    return dflt;
  }
}

function labelOf(node) {
  const a = node.a;
  const sent = sentiment(a);
  const book = a.book === 'holding' ? ', you hold it' : a.book === 'watchlist' ? ', on your watchlist' : '';
  return `${node.title}: ${num(a.n)} stories, ${num(a.bull || 0)} bullish, ${num(a.bear || 0)} bearish${a.mat ? `, ${num(a.mat)} material` : ''}, ${sent.word}${book}`;
}

function Bubble({ p, node, selected, dimmed, onPick, onHover }) {
  const sent = sentiment(node.a);
  const big = p.r >= 25;
  const mid = p.r >= 13;
  const intensity = Math.min(1, 0.25 + Math.abs(sent.score) * 1.5);
  const held = node.a.book === 'holding' || node.a.book === 'watchlist';
  return (
    <motion.g
      className={`nw-bubble nw-bubble--${sent.tone}${selected ? ' is-sel' : ''}${node.a.mat ? ' has-mat' : ''}`}
      initial={{ x: p.x, y: p.y, scale: 0.2, opacity: 0 }}
      animate={{ x: p.x, y: p.y, scale: 1, opacity: dimmed ? 0.3 : 1 }}
      transition={{ type: 'spring', stiffness: 170, damping: 21, mass: 0.7 }}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={labelOf(node)}
      data-key={node.key}
      onClick={e => onPick(node, e)}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onPick(node, e);
        }
      }}
      onMouseEnter={() => onHover(node.key)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(node.key)}
      onBlur={() => onHover(null)}
    >
      <circle className="nw-bubble__ring" r={p.r + 3.5} />
      <circle className="nw-bubble__body" r={p.r} style={{ '--nw-int': intensity }} />
      <circle className="nw-bubble__gloss" r={p.r * 0.92} cy={-p.r * 0.06} />
      {mid ? (
        <text className="nw-bubble__tk" y={big ? -3 : 4} style={{ fontSize: `${Math.max(9, Math.min(15, p.r * 0.42))}px` }}>
          {node.label}
        </text>
      ) : null}
      {big ? (
        <text className="nw-bubble__n" y={Math.min(16, p.r * 0.5)} style={{ fontSize: `${Math.max(8.5, Math.min(12, p.r * 0.3))}px` }}>
          {sent.glyph} {num(node.a.n)}
          {held ? ' ●' : ''}
        </text>
      ) : null}
    </motion.g>
  );
}

export default function NewsBubbles({ config, saveConfig }) {
  const s = useNews();
  const { ref: stageRef, box } = useBox();
  const [hover, setHover] = useState(null);
  const sectorsOk = !!(s.agg && s.agg.sectors && s.agg.sectors.length);
  const want = config && config.group === 'sectors' ? 'sectors' : 'tickers';
  const g = want === 'sectors' && sectorsOk ? 'sectors' : 'tickers';

  const nodes = useMemo(() => {
    if (!s.agg) return [];
    if (g === 'sectors') {
      return s.agg.sectors.map(a => ({ key: a.sector, n: a.n, a, kind: 'sector', label: a.etf || shortSector(a.sector), title: a.sector, sub: (a.top || []).slice(0, 3).join(' · ') }));
    }
    return s.agg.tickers.map(a => ({ key: a.ticker, n: a.n, a, kind: 'ticker', label: a.ticker, title: a.ticker, sub: a.name || '' }));
  }, [s.agg, g]);
  const budget = bubbleBudget(box.w, box.h);
  const shown = useMemo(() => nodes.slice(0, budget), [nodes, budget]);
  const layout = useMemo(() => packBubbles(shown.map(x => ({ key: x.key, n: x.n })), box.w, box.h), [shown, box.w, box.h]);
  const byKey = useMemo(() => new Map(shown.map(x => [x.key, x])), [shown]);
  const selKey = g === 'sectors' ? s.sel.sector : s.sel.ticker;

  const onPick = useCallback(
    (node, e) => {
      if (node.kind === 'sector') toggleSector(node.key);
      else toggleTicker(node.key);
      let x = e && e.clientX;
      let y = e && e.clientY;
      if (!Number.isFinite(x) || !Number.isFinite(y) || (x === 0 && y === 0)) {
        const r = e && e.currentTarget && e.currentTarget.getBoundingClientRect ? e.currentTarget.getBoundingClientRect() : null;
        x = r ? r.left + r.width / 2 : NaN;
        y = r ? r.top + r.height / 2 : NaN;
      }
      if (Number.isFinite(x) && Number.isFinite(y)) spark(x, y, { color: tokenColor(sentiment(node.a).tone) });
    },
    []
  );
  const setGroup = v => {
    if (typeof saveConfig === 'function') saveConfig({ ...(config || {}), group: v });
  };
  const onKeyDown = e => {
    if (e.key === 'Escape' && selKey) {
      e.preventDefault();
      if (g === 'sectors') toggleSector(selKey);
      else toggleTicker(selKey);
    }
  };

  const hp = hover != null ? layout.find(p => p.key === hover) : null;
  const hn = hp ? byKey.get(hp.key) : null;
  const selNode = selKey ? nodes.find(n => n.key === selKey) : null;
  const hiddenN = Math.max(0, nodes.length - shown.length);

  return (
    <div className="nw nw-bub" onKeyDown={onKeyDown}>
      <div className="nw-bub__bar">
        <div className="nw-seg" role="group" aria-label="Group bubbles by">
          <button type="button" className={g === 'tickers' ? 'is-on' : ''} aria-pressed={g === 'tickers'} onClick={() => setGroup('tickers')}>
            Tickers
          </button>
          <button
            type="button"
            className={g === 'sectors' ? 'is-on' : ''}
            aria-pressed={g === 'sectors'}
            disabled={!sectorsOk}
            title={sectorsOk ? 'Group by GICS sector' : 'Sectors need the full-day flow (after a Mason restart)'}
            onClick={() => setGroup('sectors')}
          >
            Sectors
          </button>
        </div>
        <span className="nw-bub__hint" aria-live="polite">
          {selNode ? (
            <>
              Filtering <b>{selNode.title}</b> — click it again (or Esc) to clear
            </>
          ) : (
            'Click a bubble to filter the feeds'
          )}
        </span>
      </div>
      <NewsState s={s} empty={s.agg && !nodes.length ? (g === 'sectors' ? 'No sector-mapped stories in this window yet.' : 'No ticker-tagged stories in this window yet.') : null}>
        <div className="nw-bub__stage" ref={stageRef}>
          {box.w > 0 && box.h > 0 ? (
            <svg className="nw-bub__svg" width={box.w} height={box.h} viewBox={`0 0 ${box.w} ${box.h}`} role="group" aria-label={`Impact map by ${g}: bubble size is the story count`}>
              {layout.map(p => {
                const node = byKey.get(p.key);
                if (!node) return null;
                return <Bubble key={p.key} p={p} node={node} selected={selKey === p.key} dimmed={!!selKey && selKey !== p.key} onPick={onPick} onHover={setHover} />;
              })}
            </svg>
          ) : null}
          {hp && hn ? (
            <div
              className={`nw-bub__tip${hp.y - hp.r - 6 < 72 ? ' is-below' : ''}`}
              role="tooltip"
              style={{
                left: `${Math.max(70, Math.min(box.w - 70, hp.x))}px`,
                top: `${hp.y - hp.r - 6 < 72 ? Math.min(box.h - 4, hp.y + hp.r + 6) : hp.y - hp.r - 6}px`
              }}
            >
              <b>{hn.title}</b>
              {hn.sub ? <span className="nw-bub__tip-sub">{hn.sub}</span> : null}
              <span>
                {num(hn.a.n)} stories · <span className="mui-up">▲ {num(hn.a.bull || 0)}</span> · <span className="mui-down">▼ {num(hn.a.bear || 0)}</span>
                {hn.a.mat ? <> · ◆ {num(hn.a.mat)} material</> : null}
              </span>
              <span className="nw-bub__tip-sub">
                {sentiment(hn.a).word}
                {hn.a.book === 'holding' ? ' · ● you hold it' : hn.a.book === 'watchlist' ? ' · ● watchlist' : ''}
              </span>
            </div>
          ) : null}
        </div>
      </NewsState>
      <div className="nw-bub__legend">
        <span>size = stories</span>
        <span className="mui-up">▲ net bullish</span>
        <span className="mui-down">▼ net bearish</span>
        <span>• mixed</span>
        <span className="nw-bub__legend-mat">◆ ring = material</span>
        {g === 'tickers' ? <span>● held / watched</span> : null}
        {hiddenN ? <span className="nw-bub__legend-more">+{num(hiddenN)} smaller (resize to see more)</span> : null}
        {s.mode === 'feed' ? <span className="nw-bub__legend-more">latest stories only until restart</span> : null}
      </div>
    </div>
  );
}
