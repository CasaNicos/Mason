// newsFocus — the lead story / Story focus card (News page, UI overhaul Task 5, 2026-11-16).
// Shows the top story (most material in view, else the freshest) until a headline is clicked anywhere on
// the page (feeds, wire tape, related list); then that story "expands" here: summary (the feed's blurb),
// Mason's read (qwen implication), direction / materiality / kind, the affected tickers with session
// sparklines (/api/bars 5m — click one to open a real MasonChart below), related stories (same tickers
// across the day, else same event kind, else the same half hour) and the source link.
// React Bits: SpotlightCard (the card), DecryptedText (headline decrypts once per story, ~0.5 s; static
// under reduced motion). Charts: lib/MasonChartHost.jsx → window.MasonChart (no second chart library).
// Every string is JSX text; the only link is an http(s) URL (util.safeUrl) with rel="noopener noreferrer".
import { useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { signed } from '../../lib/fmt.js';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import { clearFocus, derive, fetchTickerItems, focusItem, toggleTicker, useNews } from '../newsShared/store.js';
import { ago, clock, pickRelated, shortSector } from '../newsShared/util.js';
import { DirTag, KindTag, MatTag, NewsState, SparkSvg, useBars, useSparkGeo } from '../newsShared/parts.jsx';
import '../newsShared/news.css';
import './newsFocus.css';

const toneOf = it => (it.dir === 'bullish' ? 'up' : it.dir === 'bearish' ? 'down' : it.material ? 'warn' : 'flat');

function TickerCard({ t, info, charted, filtered, onChart }) {
  const bars = useBars(t);
  const geo = useSparkGeo(bars.pts);
  const chg = geo ? geo.chg : null;
  const sub = (info && (info.name || shortSector(info.sector))) || '';
  const moveText = geo ? `${geo.last.toFixed(2)}, ${chg >= 0 ? 'up' : 'down'} ${Math.abs(chg).toFixed(2)}% over the last session bars` : 'no recent bars';
  return (
    <div className={`nw-tcard nw-tcard--${geo ? geo.tone : 'flat'}${charted ? ' is-on' : ''}`}>
      <button type="button" className="nw-tcard__main" aria-pressed={charted} aria-label={`${t}${sub ? ` (${sub})` : ''}: ${moveText}. ${charted ? 'Hide' : 'Show'} the chart.`} onClick={() => onChart(t)}>
        <span className="nw-tcard__head">
          <b>{t}</b>
          {sub ? <span className="nw-tcard__name">{sub}</span> : null}
        </span>
        <SparkSvg geo={geo} />
        <span className="nw-tcard__px">
          {geo ? (
            <>
              {geo.last.toFixed(2)}{' '}
              <span className={chg > 0.005 ? 'mui-up' : chg < -0.005 ? 'mui-down' : 'mui-flat'}>
                {chg > 0.005 ? '▲' : chg < -0.005 ? '▼' : '•'} {signed(chg, 2)}%
              </span>
            </>
          ) : bars.status === 'loading' ? (
            'loading…'
          ) : (
            'no bars'
          )}
        </span>
      </button>
      <button
        type="button"
        className={`nw-tcard__filter${filtered ? ' is-on' : ''}`}
        aria-pressed={filtered}
        aria-label={filtered ? `Stop filtering the page to ${t}` : `Filter the page to ${t}`}
        title={filtered ? `Stop filtering to ${t}` : `Filter the page to ${t}`}
        onClick={() => toggleTicker(t)}
      >
        <i className={filtered ? 'ph ph-x' : 'ph ph-funnel-simple'} aria-hidden="true" />
      </button>
    </div>
  );
}

export default function NewsFocus() {
  const s = useNews();
  const d = derive(s);
  const reduce = usePrefersReducedMotion();
  const focused = s.sel.focus;
  const item = focused || d.lead;
  const mode = focused ? 'focus' : 'lead';
  const key = item ? item.key : '';
  const [chart, setChart] = useState({ key: '', t: null });
  const [remote, setRemote] = useState({ key: '', items: null });
  const [pulse, setPulse] = useState(0);
  const rootRef = useRef(null);
  const firstTk = item && item.tickers.length ? item.tickers[0] : null;

  // related stories across the whole span for the story's first ticker (flow mode; cached in the store)
  useEffect(() => {
    let live = true;
    if (firstTk) {
      fetchTickerItems(firstTk).then(r => {
        if (live) setRemote({ key, items: r });
      });
    }
    return () => {
      live = false;
    };
  }, [key, firstTk, s.mode]);

  // a story focused from another widget: pulse the card, and bring it into view if it is off screen
  useEffect(() => {
    const h = () => {
      setPulse(p => p + 1);
      const el = rootRef.current && rootRef.current.closest ? rootRef.current.closest('.grid-stack-item') || rootRef.current : null;
      if (!el || typeof el.getBoundingClientRect !== 'function') return;
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight || 800;
      if ((r.bottom < 80 || r.top > vh - 80) && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
    };
    window.addEventListener('mason:news-focus', h);
    return () => window.removeEventListener('mason:news-focus', h);
  }, [reduce]);

  const remoteItems = remote.key === key ? remote.items : null;
  const pool = useMemo(() => [...(remoteItems || []), ...s.items, ...(s.q.items || [])], [remoteItems, s.items, s.q.items]);
  const related = useMemo(() => pickRelated(item, pool, 5), [item, pool]);
  const chartTk = chart.key === key ? chart.t : null;
  const onChart = t => setChart(c => ({ key, t: c.key === key && c.t === t ? null : t }));

  return (
    <div className={`nw nw-focus nw-focus--${mode}`} ref={rootRef}>
      <NewsState s={s} empty={!item ? (d.selLoading ? 'Scanning the day…' : d.selOn || s.sel.cat !== 'all' ? 'No stories match the current filter.' : 'No stories yet.') : null}>
        {item ? (
          <SpotlightCard className={`nw-card nw-card--${toneOf(item)}`} spotlightColor="rgba(74, 158, 255, 0.2)">
            {pulse ? <span key={pulse} className="nw-card__pulse" aria-hidden="true" /> : null}
            <motion.div key={key} className="nw-card__in" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22 }}>
              <div className="nw-card__eyebrow">
                <span className="nw-card__mode">{mode === 'focus' ? '◉ Story focus' : '◆ Top story'}</span>
                <DirTag dir={item.dir} />
                <MatTag it={item} />
                <KindTag kind={item.kind} />
                {mode === 'focus' ? (
                  <button type="button" className="nw-card__back" onClick={clearFocus}>
                    ✕ <span className="nw-card__back-txt">back to top story</span>
                  </button>
                ) : null}
              </div>
              <h2 className="nw-card__hl">
                <span className="mui-sr">{item.headline}</span>
                <span aria-hidden="true">
                  {/* one quick decrypt per story (~0.5 s whatever the length); new arrivals elsewhere type in left→right */}
                  <DecryptedText key={key} text={item.headline} animateOn="view" maxIterations={12} speed={40} characters="0123456789ABCDEF<>/\|=+*#" className="nw-typed" encryptedClassName="nw-scramble" />
                </span>
              </h2>
              <div className="nw-card__meta">
                <span className="nw-src">{item.source}</span>
                <span className="nw-sep" aria-hidden="true">·</span>
                <span>{item.time || clock(item.t)}</span>
                {item.t ? (
                  <>
                    <span className="nw-sep" aria-hidden="true">·</span>
                    <span>{ago(item.t)}</span>
                  </>
                ) : null}
                {item.tier === 1 ? <span className="nw-card__tier">tier-1 wire</span> : null}
              </div>
              <div className="nw-card__grid">
                <div className="nw-card__text">
                  {item.desc ? (
                    <p className="nw-card__sum">
                      <span className="nw-card__label">Summary</span>
                      {item.desc}
                    </p>
                  ) : null}
                  {item.implication ? (
                    <p className="nw-card__impl">
                      <span className="nw-card__label">
                        <span className="nw-arr" aria-hidden="true">↳</span> Mason’s read
                      </span>
                      {item.implication}
                    </p>
                  ) : null}
                  {!item.desc && !item.implication ? <p className="nw-card__none">No summary from this source yet.</p> : null}
                </div>
                {item.tickers.length ? (
                  <div className="nw-card__tickers" role="group" aria-label="Affected tickers">
                    {item.tickers.slice(0, 4).map(t => (
                      <TickerCard key={t} t={t} info={s.names[t]} charted={chartTk === t} filtered={s.sel.ticker === t} onChart={onChart} />
                    ))}
                  </div>
                ) : null}
              </div>
              {chartTk ? (
                <div className="nw-card__chart">
                  <div className="nw-card__charthead">
                    <b>{chartTk}</b> <span className="mui-muted">· Mason chart</span>
                    <button type="button" className="nw-card__back" onClick={() => setChart({ key, t: null })}>
                      ✕ close chart
                    </button>
                  </div>
                  <div className="nw-card__chartbox">
                    <MasonChartHost chartId="news-focus" symbol={chartTk} tf="5m" type="candle" follow={false} />
                  </div>
                </div>
              ) : null}
              {related.length ? (
                <div className="nw-card__related">
                  <div className="nw-card__label">Related stories</div>
                  <ul>
                    {related.map(r => (
                      <li key={r.key}>
                        <button type="button" className={`nw-rel nw-rel--${toneOf(r)}`} onClick={() => focusItem(r)}>
                          <span className="nw-rel__dir" aria-hidden="true">
                            {r.dir === 'bullish' ? '▲' : r.dir === 'bearish' ? '▼' : '•'}
                          </span>
                          <span className="nw-rel__hl">{r.headline}</span>
                          <span className="nw-rel__meta">
                            {r.source} · {r.time || clock(r.t)}
                            {r.dir === 'bullish' ? ' · bullish' : r.dir === 'bearish' ? ' · bearish' : ''}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <div className="nw-card__foot">
                {item.url ? (
                  <a className="nw-card__link" href={item.url} target="_blank" rel="noopener noreferrer">
                    Read on {item.source} <span aria-hidden="true">↗</span>
                  </a>
                ) : (
                  <span className="mui-muted">No source link for this story</span>
                )}
              </div>
            </motion.div>
          </SpotlightCard>
        ) : null}
      </NewsState>
    </div>
  );
}
