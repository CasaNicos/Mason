// newsTimeline/river.js — geometry for the sentiment river (UI overhaul Task 5). Pure, no DOM.
//
// The river: x = time (proportional), center line = calm. The faint envelope's width is the story
// count per bucket; bullish stories swell the green bank above the line, bearish the red bank below.
// Curves are monotone cubics (Fritsch–Carlson), so a bank never overshoots across the center line.

const f = v => (Math.round(v * 10) / 10).toString();

/** SVG path through pts (strictly monotone x, either direction), monotone cubic. */
export function monotonePath(pts, move = true) {
  const n = pts.length;
  if (!n) return '';
  let d = move ? `M${f(pts[0].x)},${f(pts[0].y)}` : `L${f(pts[0].x)},${f(pts[0].y)}`;
  if (n === 1) return d;
  const dx = [];
  const m = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1].x - pts[i].x;
    m[i] = dx[i] ? (pts[i + 1].y - pts[i].y) / dx[i] : 0;
  }
  const t = new Array(n);
  t[0] = m[0];
  t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) {
      t[i] = 0;
      t[i + 1] = 0;
      continue;
    }
    const a = t[i] / m[i];
    const b = t[i + 1] / m[i];
    const h = a * a + b * b;
    if (h > 9) {
      const s = 3 / Math.sqrt(h);
      t[i] = s * a * m[i];
      t[i + 1] = s * b * m[i];
    }
  }
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += `C${f(pts[i].x + h)},${f(pts[i].y + h * t[i])},${f(pts[i + 1].x - h)},${f(pts[i + 1].y - h * t[i + 1])},${f(pts[i + 1].x)},${f(pts[i + 1].y)}`;
  }
  return d;
}

/**
 * river(buckets, opts) → geometry.
 *   buckets [{t (ms), n, bull, bear, mat}] ascending; opts {t0, t1, bucketMs, x0, x1, yMid, half}
 *   → {xOf(t), tOf(x), indexAt(x), env, bull, bear, mats:[{x, n, i}], maxN, maxDir, xEnd, xs (bucket centers)}
 * The envelope uses the busiest bucket as full height; the banks use the busiest DIRECTIONAL bucket,
 * so a mostly-neutral day still shows its bull/bear shape (exact counts are in the readout).
 */
export function river(buckets, { t0, t1, bucketMs, x0, x1, yMid, half }) {
  const span = Math.max(1, t1 - t0);
  const W = Math.max(1, x1 - x0);
  const xOf = t => x0 + ((t - t0) / span) * W;
  const tOf = x => t0 + ((x - x0) / W) * span;
  const nb = buckets.length;
  const indexAt = x => (nb ? Math.max(0, Math.min(nb - 1, Math.floor((tOf(x) - t0) / bucketMs))) : -1);
  if (!nb) return { xOf, tOf, indexAt, env: '', bull: '', bear: '', mats: [], maxN: 0, maxDir: 0, xEnd: x0, xs: [] };
  let maxN = 0;
  let maxDir = 0;
  for (const b of buckets) {
    maxN = Math.max(maxN, b.n);
    maxDir = Math.max(maxDir, b.bull, b.bear);
  }
  const sEnv = maxN ? (half * 0.94) / maxN : 0;
  const sDir = maxDir ? (half * 0.9) / maxDir : 0;
  const xs = buckets.map(b => Math.min(x1, xOf(Math.min(t1, b.t + bucketMs / 2))));
  const xEnd = Math.min(x1, xOf(Math.min(t1, buckets[nb - 1].t + bucketMs)));
  const series = key => {
    const pts = [{ x: x0, v: buckets[0][key] }];
    for (let i = 0; i < nb; i++) if (xs[i] > pts[pts.length - 1].x + 0.01) pts.push({ x: xs[i], v: buckets[i][key] });
    if (xEnd > pts[pts.length - 1].x + 0.01) pts.push({ x: xEnd, v: buckets[nb - 1][key] });
    return pts;
  };
  const bank = (key, sign, s) => {
    const pts = series(key).map(p => ({ x: p.x, y: yMid + sign * p.v * s }));
    return `${monotonePath(pts)}L${f(pts[pts.length - 1].x)},${f(yMid)}L${f(pts[0].x)},${f(yMid)}Z`;
  };
  const nPts = series('n');
  const up = nPts.map(p => ({ x: p.x, y: yMid - p.v * sEnv }));
  const dn = nPts
    .slice()
    .reverse()
    .map(p => ({ x: p.x, y: yMid + p.v * sEnv }));
  const env = `${monotonePath(up)}${monotonePath(dn, false)}Z`;
  const mats = [];
  buckets.forEach((b, i) => {
    if (b.mat > 0) mats.push({ x: xs[i], n: b.mat, i });
  });
  return { xOf, tOf, indexAt, env, bull: bank('bull', -1, sDir), bear: bank('bear', 1, sDir), mats, maxN, maxDir, xEnd, xs };
}

/** Line through a selection's own per-bucket counts (overlay) at the river's bucket x positions. */
export function overlayPath(counts, xs, yMid, half) {
  if (!counts || !xs || !xs.length) return '';
  let max = 0;
  for (const c of counts) max = Math.max(max, c || 0);
  if (!max) return '';
  const s = (half * 0.9) / max;
  const pts = [];
  xs.forEach((x, i) => {
    if (!pts.length || x > pts[pts.length - 1].x + 0.01) pts.push({ x, y: yMid - (counts[i] || 0) * s });
  });
  return monotonePath(pts);
}
