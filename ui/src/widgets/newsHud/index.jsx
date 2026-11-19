// newsHud — the News page's HUD header + live wire tape (UI overhaul Task 5, 2026-11-16).
// React Bits: LogoLoop (the wire crawl — the page's ONE continuous effect; its rAF is gated by
//   useMotionAllowed(), MODS M-LogoLoop-1: static when hidden / off screen / reduced motion),
//   GlassSurface (the readout pills), CountUp (story count), DecryptedText (via TypeIn: the
//   "latest" line types in when a new story lands; one-shot).
// Built here, no React Bits fit: the category chips, the bull/bear gauge and the filter pills
//   (plain buttons; they must be keyboard buttons with visible text, not effects).
// Chips + filter pills drive the shared store (newsShared/store.js), so every News widget follows.
import { useCallback, useEffect, useMemo, useState } from 'react';
import LogoLoop from '../../reactbits/LogoLoop/LogoLoop.jsx';
import GlassSurface from '../../reactbits/GlassSurface/GlassSurface.jsx';
import CountUp from '../../reactbits/CountUp/CountUp.jsx';
import { num } from '../../lib/fmt.js';
import { clearSelection, derive, focusItem, setCat, setRange, setSpan, toggleSector, toggleTicker, useNews } from '../newsShared/store.js';
import { ago, CATS, clock, clockRange, shortSector } from '../newsShared/util.js';
import { TypeIn, usePlayed } from '../newsShared/parts.jsx';
import '../newsShared/news.css';
import './newsHud.css';

const WIRE_MAX = 28;
const trunc = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const toneOf = it => (it.dir === 'bullish' ? 'up' : it.dir === 'bearish' ? 'down' : 'flat');

function Wire({ it }) {
  const glyph = it.dir === 'bullish' ? '▲' : it.dir === 'bearish' ? '▼' : '•';
  return (
    // tabIndex -1: the crawl moves, so keyboard users get the same stories in the feeds instead
    <button type="button" tabIndex={-1} className={`nw-wire nw-wire--${toneOf(it)}`} onClick={() => focusItem(it)} title={`${it.headline} — ${it.source}`}>
      <span className="nw-wire__time">{it.time || clock(it.t)}</span>
      <span className="nw-wire__dir" aria-hidden="true">
        {glyph}
      </span>
      <span className="nw-wire__src">{it.source}</span>
      <span className="nw-wire__hl">{trunc(it.headline, 110)}</span>
      {it.tickers.length ? <span className="nw-wire__tk">{it.tickers.slice(0, 3).join(' ')}</span> : null}
    </button>
  );
}

function Stat({ k, label, value, sub, tone }) {
  return (
    <div className={`nw-stat${tone ? ` nw-stat--${tone}` : ''}`} data-stat={k}>
      <GlassSurface width="100%" height="100%" borderRadius={10} brightness={20} opacity={0.6} blur={8} backgroundOpacity={0.08}>
        <span className="nw-stat__in">
          <span className="nw-stat__label">{label}</span>
          <span className="nw-stat__value">{value}</span>
          {sub ? <span className="nw-stat__sub">{sub}</span> : null}
        </span>
      </GlassSurface>
    </div>
  );
}

function Gauge({ bull, bear }) {
  const dirN = bull + bear;
  const pb = dirN ? Math.round((bull / dirN) * 100) : 50;
  return (
    <div className="nw-gauge" role="img" aria-label={dirN ? `${pb}% of directional stories bullish, ${100 - pb}% bearish` : 'No directional stories yet'}>
      <span className="nw-gauge__lab nw-gauge__lab--up">▲ {dirN ? `${pb}%` : '—'}</span>
      <span className="nw-gauge__bar" aria-hidden="true">
        <i className="nw-gauge__up" style={{ width: `${pb}%` }} />
        <i className="nw-gauge__down" style={{ width: `${100 - pb}%` }} />
      </span>
      <span className="nw-gauge__lab nw-gauge__lab--down">{dirN ? `${100 - pb}%` : '—'} ▼</span>
    </div>
  );
}

export default function NewsHud() {
  const s = useNews();
  const d = derive(s);
  const played = usePlayed();
  const [ping, setPing] = useState(false);
  const [, setNow] = useState(0);

  // one-shot scan sweep when a poll brings new stories (no loop; CSS animation runs once)
  useEffect(() => {
    if (!s.newCount) return undefined;
    setPing(true);
    const id = setTimeout(() => setPing(false), 1400);
    return () => clearTimeout(id);
  }, [s.asOf, s.newCount]);
  // "updated 2m ago" stays honest between polls (30 s tick, text only)
  useEffect(() => {
    const id = setInterval(() => setNow(n => n + 1), 30000);
    return () => clearInterval(id);
  }, []);

  const wire = useMemo(() => s.items.slice(0, WIRE_MAX).map(it => ({ ...it, id: it.key })), [s.items]);
  const renderWire = useCallback(it => <Wire it={it} />, []);
  const latest = s.items[0] || null;
  const day = (s.agg && s.agg.day) || { n: s.items.length, bull: 0, bear: 0, mat: 0 };
  const total = s.agg ? s.agg.total : s.items.length;
  const spanWord = s.span === '24h' ? 'last 24h' : 'today';
  const sel = s.sel;
  const anySel = !!(sel.ticker || sel.sector || sel.range);

  return (
    <div className={`nw nw-hud${ping ? ' is-ping' : ''}`}>
      <div className="nw-hud__top">
        <div className="nw-hud__brand">
          <span className={`nw-live${s.status === 'ok' && !s.error ? '' : ' is-stale'}`} title={s.error ? `Last refresh failed: ${s.error}` : 'Live — refreshes about every 2 minutes'}>
            <i aria-hidden="true" />
            {s.status === 'ok' && !s.error ? 'LIVE' : s.status === 'loading' ? 'SYNC' : 'STALE'}
          </span>
          <span className="nw-hud__title">NEWS//WIRE</span>
        </div>
        <div className="nw-hud__stats">
          <Stat k="stories" label={`Stories ${spanWord}`} value={<CountUp to={total} duration={0.8} separator="," />} />
          <Stat k="bull" tone="up" label="Bullish" value={`▲ ${num(day.bull)}`} />
          <Stat k="bear" tone="down" label="Bearish" value={`▼ ${num(day.bear)}`} />
          <Stat k="mat" tone="warn" label="Material" value={`◆ ${num(day.mat)}`} />
        </div>
        <Gauge bull={day.bull || 0} bear={day.bear || 0} />
        <div className="nw-hud__right">
          <div className="nw-seg" role="group" aria-label="Time window">
            <button type="button" className={s.span === 'today' ? 'is-on' : ''} aria-pressed={s.span === 'today'} onClick={() => setSpan('today')}>
              Today
            </button>
            <button type="button" className={s.span === '24h' ? 'is-on' : ''} aria-pressed={s.span === '24h'} onClick={() => setSpan('24h')}>
              24h
            </button>
          </div>
          <span className="nw-hud__asof" title={s.asOf ? new Date(s.asOf).toString() : ''}>
            {s.asOf ? `updated ${clock(s.asOf)} · ${ago(s.asOf)}` : 'connecting…'}
            {s.newCount ? <b className="nw-new nw-new--pill">+{num(s.newCount)} new</b> : null}
          </span>
        </div>
      </div>

      <div className="nw-hud__mid">
        <div className="nw-chips" role="group" aria-label="Filter stories">
          {CATS.map(c => (
            <button key={c.id} type="button" className={`nw-chip${sel.cat === c.id ? ' is-on' : ''}`} aria-pressed={sel.cat === c.id} onClick={() => setCat(c.id)}>
              {c.label} <span className="nw-chip__n">{num(d.counts[c.id] || 0)}</span>
            </button>
          ))}
        </div>
        {anySel ? (
          <div className="nw-filters" role="group" aria-label="Active filters">
            {sel.ticker ? (
              <button type="button" className="nw-pill" onClick={() => toggleTicker(sel.ticker)} aria-label={`Remove the ${sel.ticker} filter`}>
                {sel.ticker} <span aria-hidden="true">✕</span>
              </button>
            ) : null}
            {sel.sector ? (
              <button type="button" className="nw-pill" onClick={() => toggleSector(sel.sector)} aria-label={`Remove the ${sel.sector} filter`}>
                {shortSector(sel.sector)} <span aria-hidden="true">✕</span>
              </button>
            ) : null}
            {sel.range ? (
              <button type="button" className="nw-pill" onClick={() => setRange(null)} aria-label="Remove the time filter">
                ◷ {clockRange(sel.range)} <span aria-hidden="true">✕</span>
              </button>
            ) : null}
            <button type="button" className="nw-pill nw-pill--clear" onClick={clearSelection}>
              Clear all
            </button>
            <span className="nw-filters__n">{d.selLoading ? 'scanning the day…' : `${num(d.scopeTotal)} match`}</span>
          </div>
        ) : null}
        {s.mode === 'feed' ? (
          <span className="nw-mode" title="The full-day flow comes from /api/news/flow, which goes live after Mason restarts">
            Live feed only (newest 36, growing this session) — full-day flow after a Mason restart
          </span>
        ) : null}
      </div>

      {latest ? (
        <button type="button" className={`nw-latest nw-latest--${toneOf(latest)}`} onClick={() => focusItem(latest)}>
          <span className="nw-latest__k">LATEST ›</span>
          <span className="nw-latest__meta">
            {latest.time || clock(latest.t)} · {latest.source}
          </span>
          <TypeIn
            key={latest.key}
            text={latest.headline}
            play={s.fresh.has(latest.key) && !played.has(latest.key)}
            onDone={() => played.add(latest.key)}
            className="nw-latest__hl"
          />
        </button>
      ) : null}

      <div className="nw-hud__tape">
        {wire.length ? (
          <LogoLoop
            logos={wire}
            renderItem={renderWire}
            speed={42}
            gap={28}
            logoHeight={12}
            pauseOnHover
            fadeOut
            ariaLabel="Latest headlines crawl (the same stories are in the feeds below)"
          />
        ) : (
          <span className="nw-hud__quiet">{s.status === 'loading' ? 'Connecting to the wire…' : 'The wire is quiet.'}</span>
        )}
      </div>
    </div>
  );
}
