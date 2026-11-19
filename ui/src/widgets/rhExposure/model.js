// rhExposure data helpers (pure). Number text goes through lib/fmt.js.
import { num, pct, arrow, isNum, DASH } from '../../lib/fmt.js';
import { fin, objs, moreText, count, PCT_MAX } from '../rhAccounts/guard.js';

export const LEG_CAP = 8; // ETF legs named in a breakdown; the rest are counted
export const TILE_CAP = 60; // tiles drawn; the rest fold into one "Other" tile

/** "NVDA 11.2% = direct 4.0 + SMH 4.1 + QQQ 3.1" (a zero direct leg is omitted). */
export function breakdownText(n) {
  if (!n || typeof n !== 'object') return '';
  if (n.other) return otherText(n);
  const legs = [];
  const direct = fin(n.direct_pct, PCT_MAX);
  if (direct != null && direct > 0) legs.push(`direct ${num(direct, 1)}`);
  const via = objs(n.via).filter(v => fin(v.pct, PCT_MAX) != null);
  for (const v of via.slice(0, LEG_CAP)) legs.push(`${String(v.etf == null ? '?' : v.etf)} ${num(v.pct, 1)}`);
  if (via.length > LEG_CAP) legs.push(moreText(via.length - LEG_CAP, 'ETFs'));
  const total = fin(n.total_pct, PCT_MAX);
  const head = `${String(n.ticker == null ? '?' : n.ticker)} ${total == null ? DASH : num(total, 1)}%`;
  return legs.length ? `${head} = ${legs.join(' + ')}` : head;
}

/** "day ▲ +1.84%" / "day —". Sign AND arrow, never color alone. */
export function dayText(v) {
  if (!isNum(v)) return `day ${DASH}`;
  const a = arrow(v);
  return `day ${a ? `${a} ` : ''}${pct(v, { digits: 2 })}`;
}

// Diverging color scale for day change (dataviz: two opposite hues, neutral midpoint, equal steps
// per arm). Nocturne's --mui-up / --mui-down are the poles; the midpoint is the neutral panel gray.
// Clipped at ±CLIP %; STEPS steps per arm.
export const CLIP = 3;
export const STEPS = 4;

/** -STEPS..+STEPS; 0 for flat / unknown. */
export function divergingStep(v) {
  if (!isNum(v) || v === 0) return 0;
  const s = Math.max(1, Math.ceil((Math.min(Math.abs(v), CLIP) / CLIP) * STEPS));
  return v > 0 ? s : -s;
}

const MIX = [0, 28, 46, 64, 82]; // % of the pole hue mixed into the neutral, per step

/** CSS color for a step (Nocturne tokens via color-mix; no new palette). */
export function stepFill(step) {
  if (!step) return 'var(--rhx-neutral)';
  const pole = step > 0 ? 'var(--mui-up)' : 'var(--mui-down)';
  return `color-mix(in oklab, ${pole} ${MIX[Math.abs(step)]}%, var(--rhx-neutral))`;
}

/** Label ink that clears contrast on the fill (dark ink on the two strongest steps). */
export const inkFor = step => (Math.abs(step) >= 3 ? 'is-ink-dark' : 'is-ink-light');

/** "+9,940 more names = 99.4% of exposure" (the folded "Other" tile). */
export function otherText(o) {
  return `${moreText(o.count, 'names')} = ${num(o.total_pct, 1)}% of exposure`;
}

/**
 * Boundary clean + cap: valid nodes (finite 0 < total_pct ≤ PCT_MAX) sorted largest first; the
 * first TILE_CAP are drawn, the rest fold into one Other node so the treemap stays O(TILE_CAP).
 * → {tiles, all: n valid, unsized: n dropped for a missing/junk size, shownPct}
 */
export function capNodes(raw) {
  const all = objs(raw).map(n => ({
    ...n,
    ticker: n.ticker == null ? '?' : String(n.ticker),
    total_pct: fin(n.total_pct, PCT_MAX),
    direct_pct: fin(n.direct_pct, PCT_MAX),
    day_chg_pct: fin(n.day_chg_pct, PCT_MAX)
  }));
  const sized = all.filter(n => n.total_pct != null && n.total_pct > 0).sort((a, b) => b.total_pct - a.total_pct);
  const tiles = sized.slice(0, TILE_CAP);
  const rest = sized.slice(TILE_CAP);
  if (rest.length) {
    tiles.push({ ticker: '__other__', other: true, count: rest.length, total_pct: rest.reduce((a, n) => a + n.total_pct, 0), day_chg_pct: null });
  }
  return { tiles, all: sized.length, unsized: all.length - sized.length, shownPct: sized.reduce((a, n) => a + n.total_pct, 0) };
}

export { count };
