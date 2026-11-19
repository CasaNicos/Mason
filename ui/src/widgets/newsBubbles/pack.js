// newsBubbles/pack.js — deterministic bubble cluster for the impact map (UI overhaul Task 5). Pure.
//
// packBubbles(nodes, W, H, opts) → [{key, x, y, r}] inside a W×H box (px).
//   nodes: [{key, n}] — bubble AREA ∝ n (radius ∝ √n).
//   Largest first, each bubble takes the first free spot on an Archimedean spiral that is stretched to
//   the box's aspect ratio, so a wide widget gets a wide cluster and a tall one a tall cluster (no
//   wasted sides). Then the cluster is scaled uniformly to fit the box minus `pad`.
// No React Bits piece does this: Ballpit (three.js) and MetaBalls (ogl) are outside the bundle's
// allowed dependencies, and BubbleMenu is a menu, not a data layout.

export function packBubbles(nodes, W, H, { pad = 8, gap = 0.14, maxIter = 6000 } = {}) {
  if (!Array.isArray(nodes) || !nodes.length || !(W > 0) || !(H > 0)) return [];
  const aspect = Math.max(0.35, Math.min(3, W / H));
  const ax = Math.sqrt(aspect);
  const ay = 1 / ax;
  const cs = nodes
    .filter(n => n && n.key != null && Number.isFinite(n.n) && n.n > 0)
    .map(n => ({ key: n.key, r: Math.sqrt(n.n), x: 0, y: 0 }))
    .sort((a, b) => b.r - a.r || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  if (!cs.length) return [];
  const placed = [];
  const b = 0.16; // spiral growth per radian (abstract units)
  let startTh = 0;
  for (const c of cs) {
    if (!placed.length) {
      placed.push(c);
      continue;
    }
    let th = Math.max(0, startTh - (2 * c.r) / b);
    let found = false;
    for (let k = 0; k < maxIter; k++) {
      const rad = b * th;
      const x = Math.cos(th) * rad * ax;
      const y = Math.sin(th) * rad * ay;
      let ok = true;
      for (let i = 0; i < placed.length; i++) {
        const p = placed[i];
        const dx = p.x - x;
        const dy = p.y - y;
        const need = p.r + c.r + gap;
        if (dx * dx + dy * dy < need * need) {
          ok = false;
          break;
        }
      }
      if (ok) {
        c.x = x;
        c.y = y;
        found = true;
        startTh = Math.max(startTh * 0.9, th * 0.75);
        break;
      }
      th += Math.min(0.6, Math.max(0.02, (c.r * 0.45) / Math.max(rad, 0.5)));
    }
    if (!found) {
      // never expected; park it beyond the cluster's right edge rather than overlap
      const maxX = placed.reduce((m, p) => Math.max(m, p.x + p.r), 0);
      c.x = maxX + c.r + gap;
      c.y = 0;
    }
    placed.push(c);
  }
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  for (const c of placed) {
    x0 = Math.min(x0, c.x - c.r);
    x1 = Math.max(x1, c.x + c.r);
    y0 = Math.min(y0, c.y - c.r);
    y1 = Math.max(y1, c.y + c.r);
  }
  const bw = Math.max(1e-6, x1 - x0);
  const bh = Math.max(1e-6, y1 - y0);
  const s = Math.max(0, Math.min((W - 2 * pad) / bw, (H - 2 * pad) / bh));
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  return placed.map(c => ({ key: c.key, x: W / 2 + (c.x - cx) * s, y: H / 2 + (c.y - cy) * s, r: c.r * s }));
}

/** How many bubbles a W×H box can label legibly. */
export function bubbleBudget(W, H) {
  if (!(W > 0) || !(H > 0)) return 0;
  return Math.max(5, Math.min(40, Math.round((W * H) / 4200)));
}
