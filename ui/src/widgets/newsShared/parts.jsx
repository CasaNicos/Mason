// newsShared/parts.jsx — shared pieces of the News page widgets (UI overhaul Task 5).
// React Bits: DecryptedText (headline type-in for NEW arrivals only; one-shot, settles at once under
// prefers-reduced-motion via MODS M-DecryptedText-1). Every server string renders as JSX text.
import { useEffect, useMemo, useRef, useState } from 'react';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import { isMockMode, fixtureUrl } from '../../lib/api.js';
import { kindLabel, pctText, normTk } from './util.js';

export const TYPE_MS = 16; // per revealed character
const TYPE_CHARS = '0123456789ABCDEF<>/\\|=+*#:';

/** Headline that "types in" (decrypts left→right) once when `play`, then plain text. */
export function TypeIn({ text, play, onDone, className = '' }) {
  const [done, setDone] = useState(!play);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  useEffect(() => {
    if (!play) return undefined;
    const ms = Math.min(5000, text.length * TYPE_MS + 400);
    const id = setTimeout(() => {
      setDone(true);
      if (doneRef.current) doneRef.current();
    }, ms);
    return () => clearTimeout(id);
  }, [play, text]);
  if (done || !play) return <span className={className}>{text}</span>;
  // DecryptedText's own screen-reader copy is visibility:hidden (upstream), so read ours instead.
  return (
    <span className={`${className} nw-typing`}>
      <span className="mui-sr">{text}</span>
      <span aria-hidden="true">
        <DecryptedText text={text} animateOn="view" sequential revealDirection="start" speed={TYPE_MS} characters={TYPE_CHARS} className="nw-typed" encryptedClassName="nw-scramble" />
      </span>
    </span>
  );
}

/** Per-widget "played" memory so a fresh row types in once, even across re-mounts. */
export function usePlayed() {
  const ref = useRef(null);
  if (!ref.current) ref.current = new Set();
  return ref.current;
}

export function DirTag({ dir, compact = false }) {
  if (dir === 'bullish') return <span className="nw-dir nw-dir--up">{compact ? '▲' : '▲ BULL'}</span>;
  if (dir === 'bearish') return <span className="nw-dir nw-dir--down">{compact ? '▼' : '▼ BEAR'}</span>;
  return null;
}

export function MatTag({ it }) {
  if (!it || !it.material) return null;
  return (
    <span className="nw-mat" title="Material: scored by Mason, touches your book, materiality ≥ 40%">
      ◆ {it.materiality != null ? pctText(it.materiality) : 'MAT'}
    </span>
  );
}

export function KindTag({ kind }) {
  const k = kindLabel(kind);
  return k ? <span className="nw-kind">{k}</span> : null;
}

/** Ticker chips; the direction mark rides along in text (never color alone). */
export function TkChips({ tickers, dir, max = 4 }) {
  if (!tickers || !tickers.length) return null;
  const cls = dir === 'bullish' ? ' nw-tk--up' : dir === 'bearish' ? ' nw-tk--down' : '';
  const more = tickers.length - max;
  return (
    <span className="nw-tks">
      {tickers.slice(0, max).map(t => (
        <span key={t} className={`nw-tk${cls}`}>
          {t}
        </span>
      ))}
      {more > 0 ? <span className="nw-tk nw-tk--more">+{more}</span> : null}
    </span>
  );
}

/** One story row (feeds + related list). Click / Enter on the row is handled by the list. */
export function StoryRow({ it, fresh = false, played, showImpl = true, compact = false }) {
  const play = fresh && !!played && !played.has(it.key);
  const tone = it.dir === 'bullish' ? 'up' : it.dir === 'bearish' ? 'down' : it.material ? 'warn' : 'flat';
  return (
    <div className={`nw-row nw-row--${tone}${play ? ' is-fresh' : ''}${compact ? ' nw-row--compact' : ''}`} data-key={it.key}>
      <span className="nw-row__stripe" aria-hidden="true" />
      <div className="nw-row__body">
        <div className="nw-row__hl">
          {it.dir === 'bullish' || it.dir === 'bearish' ? <DirTag dir={it.dir} compact={compact} /> : null}
          <MatTag it={it} />
          <TypeIn text={it.headline} play={play} onDone={() => played && played.add(it.key)} className="nw-row__txt" />
        </div>
        <div className="nw-row__meta">
          {play ? <span className="nw-new">NEW</span> : null}
          <span className="nw-src">{it.source}</span>
          {it.time ? (
            <>
              <span className="nw-sep" aria-hidden="true">·</span>
              <span className="nw-time">{it.time}</span>
            </>
          ) : null}
          {it.tickers.length ? <TkChips tickers={it.tickers} dir={it.dir} max={compact ? 2 : 4} /> : null}
        </div>
        {showImpl && it.implication ? (
          <div className="nw-row__impl">
            <span className="nw-arr" aria-hidden="true">↳</span> {it.implication}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Loading / error / empty states shared by the news widgets (text always present). */
export function NewsState({ s, empty, children }) {
  if (s.status === 'loading' && !s.items.length) {
    return (
      <div className="mui-state mui-state--loading" role="status" aria-live="polite">
        <span className="mui-sr">Loading news…</span>
        {[92, 78, 64].map(w => (
          <div key={w} className="mui-skel" style={{ width: `${w}%` }} aria-hidden="true" />
        ))}
      </div>
    );
  }
  if (s.status === 'error' && !s.items.length) {
    return (
      <div className="mui-state mui-state--error" role="alert">
        <span className="mui-state__icon" aria-hidden="true">!</span>
        <span className="mui-state__text">Couldn’t load news: {String(s.error || 'unknown error')}</span>
      </div>
    );
  }
  if (empty) return <div className="mui-state mui-state--empty">{empty}</div>;
  return children;
}

// ── mini price charts: /api/bars 5-minute session (shared cache, 60 s) ──
const barsCache = new Map(); // ticker → {at, p}
const BARS_TTL = 60000;
function loadBars(ticker) {
  const hit = barsCache.get(ticker);
  if (hit && Date.now() - hit.at < BARS_TTL) return hit.p;
  const mock = isMockMode();
  const url = mock ? fixtureUrl('bars') : `/api/bars?ticker=${encodeURIComponent(ticker)}&tf=5m&limit=78`;
  const p = fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      const src = d && mock && d.by_ticker && Array.isArray(d.by_ticker[ticker]) ? d.by_ticker[ticker] : d && d.bars;
      const pts = (Array.isArray(src) ? src : [])
        .map(b => ({ t: Number(b && b.t), c: Number(b && b.c), o: Number(b && b.o) }))
        .filter(b => Number.isFinite(b.t) && Number.isFinite(b.c) && b.c > 0);
      return pts.length >= 2 ? pts : null;
    })
    .catch(() => null);
  barsCache.set(ticker, { at: Date.now(), p });
  if (barsCache.size > 40) barsCache.delete(barsCache.keys().next().value);
  return p;
}
export function useBars(ticker) {
  const t = normTk(ticker);
  const [st, setSt] = useState({ t, pts: null, status: t ? 'loading' : 'none' });
  useEffect(() => {
    if (!t) return undefined;
    let live = true;
    setSt({ t, pts: null, status: 'loading' });
    loadBars(t).then(pts => {
      if (live) setSt({ t, pts, status: pts ? 'ok' : 'none' });
    });
    return () => {
      live = false;
    };
  }, [t]);
  return st.t === t ? st : { t, pts: null, status: 'loading' };
}

/** Sparkline geometry for a session's closes. chg = last close vs the first bar's open, in %. */
export function sparkGeo(pts, w = 120, h = 32) {
  if (!pts || pts.length < 2) return null;
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of pts) {
    lo = Math.min(lo, p.c);
    hi = Math.max(hi, p.c);
  }
  const base = Number.isFinite(pts[0].o) && pts[0].o > 0 ? pts[0].o : pts[0].c;
  lo = Math.min(lo, base);
  hi = Math.max(hi, base);
  const span = hi - lo || 1;
  const x = i => (i / (pts.length - 1)) * w;
  const y = v => h - 2 - ((v - lo) / span) * (h - 4);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join('');
  const last = pts[pts.length - 1].c;
  const chg = ((last - base) / base) * 100;
  const tone = chg > 0.005 ? 'up' : chg < -0.005 ? 'down' : 'flat';
  return { d, area: `${d}L${w},${h}L0,${h}Z`, baseY: y(base), last, chg, tone, w, h };
}

/** The sparkline itself (decorative: the price and move are printed next to it as text). */
export function SparkSvg({ geo }) {
  if (!geo) return <svg className="nw-spark nw-spark--empty" viewBox="0 0 120 32" aria-hidden="true" />;
  return (
    <svg className={`nw-spark nw-spark--${geo.tone}`} viewBox={`0 0 ${geo.w} ${geo.h}`} preserveAspectRatio="none" aria-hidden="true">
      <path className="nw-spark__area" d={geo.area} />
      <line className="nw-spark__base" x1="0" x2={geo.w} y1={geo.baseY} y2={geo.baseY} />
      <path className="nw-spark__line" d={geo.d} />
    </svg>
  );
}
export function useSparkGeo(pts) {
  return useMemo(() => sparkGeo(pts), [pts]);
}
