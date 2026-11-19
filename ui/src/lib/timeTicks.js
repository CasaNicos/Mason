// timeTicks — the ONE x-axis date/time tick picker for every Mason chart (UI overhaul Task 3).
// Used by React charts (import) and by the vanilla canvas engine window.MasonChart in
// templates/mason_v2.html (via window.MasonUI.timeTicks, exposed in entry.js).
//
// Rule: labels never overlap. The picker measures the axis width and every label's real text width
// (canvas measureText or DOM), then walks a ladder of "nice" intervals from fine to coarse
//   1m 2m 5m 10m 15m 30m · 1h 2h 3h 4h 6h 12h · 1d · 1w 2w · 1M 3M 6M · 1y 2y 5y 10y
// and takes the finest interval whose labels fit with `minGap` px between them (and ticks at least
// `minPitch` px apart). A narrower chart
// therefore gets a coarser interval (wider time span between labels), never squished labels.
// Labels match the interval: "10 AM" / "10:30" (intraday), "Nov 12" (day/week), "Nov" (month),
// "2026" (year). Crossing a boundary shows the coarser unit: the first tick of a new day on an
// intraday axis reads "Nov 13", a new year reads "2026".
//
// Two axis models:
//   pickTimeTicks  — INDEX axis: one slot per data point (candles, sessions); time gaps (nights,
//                    weekends) are skipped. A tick sits on the first point inside each new bucket.
//   pickScaleTicks — TIME axis: x is proportional to time; ticks sit on exact bucket boundaries.
// Both return {step, unit, ticks:[{i?, t, x, label, level, anchor, left, right}]}, sorted by x.
//   anchor: 'start' | 'middle' | 'end' (canvas textAlign 'left'|'center'|'right'; SVG textAnchor),
//   draw the text at `tx` (x clamped so the label stays inside [x0, x1]).
//   level: 1 time · 2 day · 3 month · 4 year (the coarsest unit that changed at this tick).
// Times are epoch ms (or anything Date accepts). utc:true reads calendar parts in UTC — use it
// for bare 'YYYY-MM-DD' dates so they never shift by the local zone.
//
// fullLabel(t, {intraday, utc}) → "Fri, Nov 13, 2026" or "Fri, Nov 13, 2026 · 10:35 AM" for
// crosshair / tooltip readouts. isIntraday(times) guesses whether a series is sub-daily.
// makeMeasure(font) → cached text-width fn (canvas measureText; a char-count estimate in jsdom).

const MIN = 60000;
const DAY = 86400000;
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** The nice-interval ladder, finest first. ms = nominal length (used only for density prefilters). */
export const STEPS = [
  ['1m', 'minute', 1],
  ['2m', 'minute', 2],
  ['5m', 'minute', 5],
  ['10m', 'minute', 10],
  ['15m', 'minute', 15],
  ['30m', 'minute', 30],
  ['1h', 'minute', 60],
  ['2h', 'minute', 120],
  ['3h', 'minute', 180],
  ['4h', 'minute', 240],
  ['6h', 'minute', 360],
  ['12h', 'minute', 720],
  ['1d', 'day', 1],
  ['1w', 'week', 1],
  ['2w', 'week', 2],
  ['1M', 'month', 1],
  ['3M', 'month', 3],
  ['6M', 'month', 6],
  ['1y', 'year', 1],
  ['2y', 'year', 2],
  ['5y', 'year', 5],
  ['10y', 'year', 10]
].map(([id, unit, n]) => ({ id, unit, n, ms: unit === 'minute' ? n * MIN : unit === 'day' ? DAY : unit === 'week' ? n * 7 * DAY : unit === 'month' ? n * 30.44 * DAY : n * 365.25 * DAY }));
const STEP_BY_ID = Object.fromEntries(STEPS.map(s => [s.id, s]));

function toMs(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : NaN;
  if (v instanceof Date) return v.getTime();
  if (typeof v === 'string') {
    const m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : Date.parse(v);
  }
  return NaN;
}

/** Calendar parts of t (local wall clock, or UTC). dn = whole days since 1970-01-01 of that date. */
function partsOf(t, utc) {
  const d = new Date(t);
  const y = utc ? d.getUTCFullYear() : d.getFullYear();
  const mo = utc ? d.getUTCMonth() : d.getMonth();
  const day = utc ? d.getUTCDate() : d.getDate();
  const mod = (utc ? d.getUTCHours() : d.getHours()) * 60 + (utc ? d.getUTCMinutes() : d.getMinutes());
  return { y, mo, d: day, mod, dn: Math.round(Date.UTC(y, mo, day) / DAY), dow: utc ? d.getUTCDay() : d.getDay() };
}

function bucketKey(step, p) {
  switch (step.unit) {
    case 'minute':
      return p.dn * (1440 / step.n) + Math.floor(p.mod / step.n);
    case 'day':
      return p.dn;
    case 'week':
      return Math.floor((p.dn + 3) / (7 * step.n)); // dn 0 = Thu 1970-01-01; +3 → weeks start Monday
    case 'month':
      return Math.floor((p.y * 12 + p.mo) / step.n);
    default:
      return Math.floor(p.y / step.n);
  }
}

/** Epoch ms where bucket `key` starts (inverse of bucketKey), in local time or UTC. */
function bucketStart(step, key, utc) {
  const mk = (y, mo, d, mins = 0) => (utc ? Date.UTC(y, mo, d, 0, mins) : new Date(y, mo, d, 0, mins).getTime());
  const fromDn = (dn, mins) => {
    const c = new Date(dn * DAY);
    return mk(c.getUTCFullYear(), c.getUTCMonth(), c.getUTCDate(), mins);
  };
  switch (step.unit) {
    case 'minute':
      return fromDn(Math.floor(key / (1440 / step.n)), (key % (1440 / step.n)) * step.n);
    case 'day':
      return fromDn(key, 0);
    case 'week':
      return fromDn(key * 7 * step.n - 3, 0);
    case 'month': {
      const m = key * step.n;
      return mk(Math.floor(m / 12), ((m % 12) + 12) % 12, 1);
    }
    default:
      return mk(key * step.n, 0, 1);
  }
}

// Level of a boundary on its own (no previous point): 1 Jan → year, the 1st → month, midnight → day.
const ownLevel = p => (p.mod ? 1 : p.d !== 1 ? 2 : p.mo ? 3 : 4);
const levelOf = (prev, cur) => (!prev ? 1 : cur.y !== prev.y ? 4 : cur.mo !== prev.mo ? 3 : cur.dn !== prev.dn ? 2 : 1);

function clock(mod, withMeridiem) {
  const h = Math.floor(mod / 60) % 24;
  const m = mod % 60;
  const h12 = h % 12 || 12;
  const ap = h < 12 ? 'AM' : 'PM';
  if (m === 0) return `${h12} ${ap}`;
  return `${h12}:${String(m).padStart(2, '0')}${withMeridiem ? ` ${ap}` : ''}`;
}

/** Tick label for a point in bucket of `step`; `level` = coarsest unit that changed there. */
export function tickLabel(step, level, p) {
  if (typeof step === 'string') step = STEP_BY_ID[step];
  if (level >= 4 || step.unit === 'year') return String(p.y);
  if (step.unit === 'month') return MON[p.mo];
  if (level >= 2 || step.unit === 'day' || step.unit === 'week') return `${MON[p.mo]} ${p.d}`;
  return clock(Math.floor(p.mod / step.n) * step.n, false);
}

/** Exact readout for crosshairs/tooltips. */
export function fullLabel(t, { intraday = true, utc = false } = {}) {
  const ms = toMs(t);
  if (!Number.isFinite(ms)) return '—';
  const p = partsOf(ms, utc);
  const day = `${WD[p.dow]}, ${MON[p.mo]} ${p.d}, ${p.y}`;
  return intraday ? `${day} · ${clock(p.mod, true).replace(/^(\d+) (AM|PM)$/, '$1:00 $2')}` : day;
}

function medianGap(ms) {
  const gaps = [];
  for (let i = 1; i < ms.length; i++) if (Number.isFinite(ms[i]) && Number.isFinite(ms[i - 1]) && ms[i] !== ms[i - 1]) gaps.push(Math.abs(ms[i] - ms[i - 1]));
  if (!gaps.length) return 0;
  gaps.sort((a, b) => a - b);
  return gaps[(gaps.length - 1) >> 1];
}

/** True when the median spacing between consecutive points is under a day (or any point has a time). */
export function isIntraday(times) {
  const ms = Array.from(times || [], toMs).filter(Number.isFinite);
  const g = medianGap(ms);
  if (g) return g < DAY * 0.9;
  return ms.length > 0 && new Date(ms[0]).getHours() * 60 + new Date(ms[0]).getMinutes() !== 0;
}

/** Cached text measurer. font like '10px Inter, sans-serif'. Falls back to an estimate (no canvas). */
export function makeMeasure(font = '10px Inter, sans-serif') {
  const cache = new Map();
  const px = parseFloat(font) || 10;
  let ctx; // created on first use
  return s => {
    s = String(s);
    let w = cache.get(s);
    if (w != null) return w;
    if (ctx === undefined) {
      ctx = null;
      try {
        if (typeof document !== 'undefined') {
          ctx = document.createElement('canvas').getContext('2d') || null;
          if (ctx) ctx.font = font;
        }
      } catch {
        ctx = null;
      }
    }
    let m = null;
    try {
      m = ctx ? ctx.measureText(s) : null;
    } catch {
      m = null;
    }
    w = m && Number.isFinite(m.width) && m.width > 0 ? m.width : s.length * px * 0.62;
    cache.set(s, w);
    return w;
  };
}
const defaultMeasure = s => String(s).length * 6.2;

// Place labels (centered on their tick, clamped inside [x0, x1]) and count neighbour collisions.
// Two neighbouring labels clash when their boxes are closer than `gap` px, or their ticks are closer
// than `pitch` px (keeps wide charts airy instead of a solid wall of labels).
const clash = (a, b, sp) => {
  const [l, r] = a.x <= b.x ? [a, b] : [b, a];
  return r.left < l.right + sp.gap || r.x - l.x < sp.pitch;
};

function layout(cands, measure, x0, x1, sp) {
  for (const c of cands) {
    const w = measure(c.label);
    let left = c.x - w / 2;
    let anchor = 'middle';
    let tx = c.x;
    if (left < x0) {
      left = x0;
      anchor = 'start';
      tx = x0;
    } else if (left + w > x1) {
      left = x1 - w;
      anchor = 'end';
      tx = x1;
    }
    c.left = left;
    c.right = left + w;
    c.fit = w <= x1 - x0; // a label wider than the whole axis is never drawn
    c.anchor = anchor;
    c.tx = tx;
  }
  let hits = 0;
  for (let k = 1; k < cands.length; k++) if (clash(cands[k - 1], cands[k], sp)) hits++;
  return hits;
}

// Keep the highest-level ticks first (year > month > day > time), then left to right; drop any
// label that would overlap one already kept. Only trims the rare irregular collision (holiday
// weeks, an overnight gap) — the step choice already made the regular spacing fit.
function thin(cands, sp) {
  const order = cands.slice().sort((a, b) => b.level - a.level || a.x - b.x);
  const kept = [];
  for (const c of order) if (c.fit !== false && kept.every(k => !clash(k, c, sp))) kept.push(c);
  return kept.sort((a, b) => a.x - b.x);
}

const out = (step, ticks) => ({
  step: step ? step.id : null,
  unit: step ? step.unit : null,
  ticks: ticks.map(c => ({ i: c.i, t: c.t, x: c.x, tx: c.tx, label: c.label, level: c.level, anchor: c.anchor, left: c.left, right: c.right }))
});

function choose(build, { x0, x1, measure, sp, maxDrop, steps }) {
  const room = x1 - x0;
  let fallback = null;
  let dense = null;
  for (const step of steps) {
    const cands = build(step, room);
    if (cands === DENSE) dense = step;
    if (!Array.isArray(cands) || !cands.length) continue;
    const hits = layout(cands, measure, x0, x1, sp);
    if (hits <= Math.floor(maxDrop * (cands.length - 1))) return out(step, thin(cands, sp));
    fallback = { step, cands };
  }
  // Nothing fits cleanly (very narrow): coarsest step that had ticks, thinned to whatever fits.
  if (!fallback && dense) {
    // Every step with ticks was too dense to even try: thin the coarsest such step.
    const cands = build(dense, Infinity);
    layout(cands, measure, x0, x1, sp);
    fallback = { step: dense, cands };
  }
  if (fallback) return out(fallback.step, thin(fallback.cands, sp));
  return null;
}

const DENSE = 'dense'; // build() result: far more boundaries than the axis could ever label
const MIN_LABEL_PX = 14; // a label is never narrower than this: cheap density prefilter

/**
 * INDEX axis. times[i] is point i's time; xOf(i) its pixel x. Returns {step, unit, ticks}.
 * opts: {x0=0, x1, measure, minGap=8, minPitch=50, utc=false, maxDrop=0.1, steps=STEPS}. Steps finer than the
 * data's median spacing are skipped (5-minute bars never get a 1m/2m interval).
 */
export function pickTimeTicks({ times, xOf, x0 = 0, x1, measure = defaultMeasure, minGap = 8, minPitch = 50, utc = false, maxDrop = 0.1, steps = STEPS }) {
  const sp = { gap: minGap, pitch: minPitch };
  const n = times ? times.length : 0;
  if (!n || typeof xOf !== 'function' || !(x1 > x0)) return { step: null, unit: null, ticks: [] };
  const ms = Array.from(times, toMs);
  const parts = ms.map(t => (Number.isFinite(t) ? partsOf(t, utc) : null));
  const gap = medianGap(ms);
  const build = (step, room) => {
    if (step.ms < gap * 0.9) return null; // finer than the data itself: every point would be a tick
    const cands = [];
    let prevKey = null;
    let prev = null;
    for (let i = 0; i < n; i++) {
      const p = parts[i];
      if (!p) continue;
      const key = bucketKey(step, p);
      // the first point counts too when it sits exactly on a boundary (daily bars at midnight, a 9:30 open)
      const first = !prev && bucketStart(step, key, utc) === ms[i];
      if (first || (prev && key !== prevKey)) {
        cands.push({ i, t: ms[i], x: xOf(i), level: first ? ownLevel(p) : levelOf(prev, p), p });
        if (cands.length * Math.max(MIN_LABEL_PX + minGap, minPitch) > room * 1.5) return DENSE;
      }
      prevKey = key;
      prev = p;
    }
    for (const c of cands) c.label = tickLabel(step, c.level, c.p);
    return cands;
  };
  const r = choose(build, { x0, x1, measure, sp, maxDrop, steps });
  if (r) return r;
  // No bucket boundary inside the data at all (one point, or a very short span): label the first point.
  const i0 = parts.findIndex(Boolean);
  if (i0 < 0) return { step: null, unit: null, ticks: [] };
  const intra = isIntraday(ms);
  const one = { i: i0, t: ms[i0], x: xOf(i0), level: 1, label: intra ? clock(parts[i0].mod, true) : `${MON[parts[i0].mo]} ${parts[i0].d}` };
  layout([one], measure, x0, x1, sp);
  return out(null, one.fit ? [one] : []);
}

/**
 * TIME axis (x proportional to time). Ticks on exact bucket boundaries in (t0, t1].
 * opts: {t0, t1, x0=0, x1, xOfTime?, measure, minGap=8, minPitch=50, utc=false, steps=STEPS}
 */
export function pickScaleTicks({ t0, t1, x0 = 0, x1, xOfTime, measure = defaultMeasure, minGap = 8, minPitch = 50, utc = false, steps = STEPS }) {
  const sp = { gap: minGap, pitch: minPitch };
  const a = toMs(t0);
  const b = toMs(t1);
  if (!Number.isFinite(a) || !Number.isFinite(b) || !(b > a) || !(x1 > x0)) return { step: null, unit: null, ticks: [] };
  const xt = typeof xOfTime === 'function' ? xOfTime : t => x0 + ((t - a) / (b - a)) * (x1 - x0);
  const build = (step, room) => {
    if (((b - a) / step.ms) * Math.max(MIN_LABEL_PX + minGap, minPitch) > room * 1.5) return DENSE;
    const cands = [];
    let prev = partsOf(a, utc);
    for (let key = bucketKey(step, prev) + 1, guard = 0; guard < 4000; key++, guard++) {
      const t = bucketStart(step, key, utc);
      if (t > b) break;
      if (t <= a) continue;
      const p = partsOf(t, utc);
      cands.push({ t, x: xt(t), level: levelOf(prev, p), p, label: '' });
      prev = p;
    }
    for (const c of cands) c.label = tickLabel(step, c.level, c.p);
    return cands;
  };
  return choose(build, { x0, x1, measure, sp, maxDrop: 0, steps }) || { step: null, unit: null, ticks: [] };
}

export default { STEPS, pickTimeTicks, pickScaleTicks, tickLabel, fullLabel, isIntraday, makeMeasure };
