// Constellation geometry (pure, unit-tested). Nodes sit on an ellipse around the core at evenly spaced
// PARAMETRIC angles and the whole ring turns (the orbit). Parametric spacing puts nodes closest together
// at the left/right ends, where labels extend sideways, and furthest apart at the top/bottom, where labels
// are centred above/below them, so labels get room exactly where they need it.
//
// "Labels never overlap": planLayout() simulates the orbit over a full turn (PHASES samples) and picks the
// richest label mode that is collision-free at EVERY sampled phase: 'full' names → 'short' names → 'none'
// (names then show on hover/focus only). Neighbouring nodes move almost in parallel, so what changes between
// two 3° samples is a few px of relative position; every rect is padded (PAD) to cover that. Re-run on
// every resize / data / card change (cheap: < 2 ms for a dozen nodes).
export const TAU = Math.PI * 2;
export const PHASES = 120; // one sample per 3° of orbit
export const LABEL_GAP = 6; // px between a node's edge and its label
export const PAD = 4; // px of air around every label rect in the collision test
export const RING_GAP = 6; // core disk → background-job ring radius
export const RING_W = 4; // job ring stroke width
export const MARGIN = 8; // px between the outermost label and the stage edge
const MIN_LINK = 16; // px of visible link between the core ring and a node

/** Angle of node i of n at orbit phase `phase` (node 0 starts at 12 o'clock). */
export const nodeAngle = (i, n, phase) => phase + (TAU * i) / Math.max(1, n) - Math.PI / 2;

/** Outward unit vector (core → node) and the label's anchor fractions. Smooth in the angle, so a label
 *  glides around its node as it orbits: right of it on the right side, above it at the top, etc. */
export function anchor(dx, dy) {
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  return { ux, uy, ax: 0.5 - 0.5 * ux, ay: 0.5 - 0.5 * uy };
}

/** Label rect (stage coords) for a node of radius r at (px, py) with the core at (cx, cy). */
export function labelBox(px, py, cx, cy, r, lw, lh) {
  const a = anchor(px - cx, py - cy);
  const ox = px + a.ux * (r + LABEL_GAP);
  const oy = py + a.uy * (r + LABEL_GAP);
  return { x: ox - a.ax * lw, y: oy - a.ay * lh, w: lw, h: lh };
}

/** Label offset relative to the node centre (what the engine writes as the label's transform). */
export function labelOffset(dx, dy, r, lw, lh) {
  const a = anchor(dx, dy);
  return { x: a.ux * (r + LABEL_GAP) - a.ax * lw, y: a.uy * (r + LABEL_GAP) - a.ay * lh };
}

const hit = (a, b, pad) =>
  a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;

/** The orbit that keeps every label inside the safe area at every phase, or null when it cannot fit. */
/** Safe area {x0, y0, x1, y1} of a w×h stage minus the insets (the focus card, the HUD bar). */
export function safeArea(w, h, inset = {}) {
  const x0 = Math.max(0, inset.left || 0);
  const y0 = Math.max(0, inset.top || 0);
  return { x0, y0, x1: Math.max(x0, w - Math.max(0, inset.right || 0)), y1: Math.max(y0, h - Math.max(0, inset.bottom || 0)) };
}

export function fitOrbit({ w, h, inset = {}, labelW, labelH, nodeR, coreR }) {
  const { x0, y0, x1, y1 } = safeArea(w, h, inset);
  if (x1 - x0 < 40 || y1 - y0 < 40) return null;
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  const lw = labelW > 0 ? labelW + LABEL_GAP : 0;
  const lh = labelH > 0 ? labelH + LABEL_GAP : 0;
  const rx = (x1 - x0) / 2 - MARGIN - nodeR - lw;
  const ry = (y1 - y0) / 2 - MARGIN - nodeR - lh;
  const min = coreR + nodeR + MIN_LINK;
  if (rx < min || ry < min) return null;
  return { cx, cy, rx, ry, x0, y0, x1, y1 };
}

/** True when anything overlaps at any sampled phase (labels, other nodes, the core, the safe edges). */
export function collides(geo, items, mode, coreR, phases = PHASES) {
  const n = items.length;
  if (!n) return false;
  const { cx, cy, rx, ry, x0 = 0, y0 = 0, x1, y1 } = geo;
  const core = { x: cx - coreR, y: cy - coreR, w: coreR * 2, h: coreR * 2 };
  const pos = new Array(n);
  const boxes = new Array(n);
  for (let k = 0; k < phases; k += 1) {
    const phase = (TAU * k) / phases;
    for (let i = 0; i < n; i += 1) {
      const a = nodeAngle(i, n, phase);
      const px = cx + rx * Math.cos(a);
      const py = cy + ry * Math.sin(a);
      pos[i] = { x: px, y: py, r: items[i].r };
      if (mode === 'none') boxes[i] = null;
      else {
        const lw = mode === 'full' ? items[i].wFull : items[i].wShort;
        boxes[i] = labelBox(px, py, cx, cy, items[i].r, lw, items[i].h);
      }
    }
    for (let i = 0; i < n; i += 1) {
      const p = pos[i];
      for (let j = i + 1; j < n; j += 1) {
        const q = pos[j];
        if (Math.hypot(p.x - q.x, p.y - q.y) < p.r + q.r + 3) return true;
      }
      const b = boxes[i];
      if (!b) continue;
      if (b.x < x0 - 1 || b.y < y0 - 1 || b.x + b.w > x1 + 1 || b.y + b.h > y1 + 1) return true;
      if (hit(b, core, PAD)) return true;
      for (let j = 0; j < n; j += 1) {
        if (j === i) continue;
        const q = pos[j];
        if (hit(b, { x: q.x - q.r, y: q.y - q.r, w: q.r * 2, h: q.r * 2 }, PAD)) return true;
        const c = boxes[j];
        if (c && j > i && hit(b, c, PAD)) return true;
      }
    }
  }
  return false;
}

/**
 * items: [{wFull, wShort, h, r}] (measured label sizes + node radius). → {cx, cy, rx, ry, mode, scale,
 * cramped}. scale (≤ 1) shrinks nodes when even unlabeled nodes do not fit; cramped = nothing fit, the
 * best-effort orbit is returned anyway (a tiny widget still draws).
 */
export function planLayout({ w, h, inset = {}, items = [], coreR }) {
  const modes = ['full', 'short', 'none'];
  for (const scale of [1, 0.8, 0.62]) {
    const its = items.map(it => ({ ...it, r: it.r * scale }));
    const nodeR = its.reduce((m, it) => Math.max(m, it.r), 6 * scale);
    const cR = coreR * (scale === 1 ? 1 : 0.85);
    const ringOuter = cR + RING_GAP + RING_W / 2 + 1; // the job ring is part of the core's footprint
    for (const mode of modes) {
      if (mode !== 'none' && scale < 1) continue; // shrinking nodes only helps the label-less ring
      const labelW = mode === 'none' ? 0 : its.reduce((m, it) => Math.max(m, mode === 'full' ? it.wFull : it.wShort), 0);
      const labelH = mode === 'none' ? 0 : its.reduce((m, it) => Math.max(m, it.h), 0);
      const geo = fitOrbit({ w, h, inset, labelW, labelH, nodeR, coreR: ringOuter });
      if (!geo) continue;
      if (!collides(geo, its, mode, ringOuter)) return { ...geo, mode, scale, coreR: cR, cramped: false };
    }
  }
  const a = safeArea(w, h, inset);
  const x0 = a.x0;
  const y0 = a.y0;
  const x1 = Math.max(x0 + 40, a.x1);
  const y1 = Math.max(y0 + 40, a.y1);
  const cR = Math.max(12, Math.min(coreR, Math.min(x1 - x0, y1 - y0) * 0.12));
  return {
    cx: (x0 + x1) / 2,
    cy: (y0 + y1) / 2,
    rx: Math.max(cR + 12, (x1 - x0) / 2 - MARGIN - 6),
    ry: Math.max(cR + 12, (y1 - y0) / 2 - MARGIN - 6),
    x0,
    y0,
    x1,
    y1,
    mode: 'none',
    scale: 0.62,
    coreR: cR,
    cramped: true
  };
}

/** Core radius for a stage: grows with the stage, 16-40 px. */
export const coreRadius = (w, h) => Math.max(16, Math.min(40, Math.min(w, h) * 0.085));

/** Point on the quadratic link from node (t = 0) to core edge (t = 1). Writes into `out`. */
export function linkPoint(l, t, out) {
  const u = 1 - t;
  out.x = u * u * l.x0 + 2 * u * t * l.qx + t * t * l.x1;
  out.y = u * u * l.y0 + 2 * u * t * l.qy + t * t * l.y1;
  return out;
}

/** Link geometry node → core: ends at the core's ring, bowed slightly (a galaxy swirl), same side for all. */
export function linkFor(px, py, cx, cy, r, ringR, out = {}) {
  const dx = px - cx;
  const dy = py - cy;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  out.x0 = px - ux * r;
  out.y0 = py - uy * r;
  out.x1 = cx + ux * ringR;
  out.y1 = cy + uy * ringR;
  const bow = Math.min(36, len * 0.14);
  out.qx = (out.x0 + out.x1) / 2 - uy * bow;
  out.qy = (out.y0 + out.y1) / 2 + ux * bow;
  return out;
}

/** Index of the background-job ring segment under (x, y), or -1. */
export function ringSegmentAt(x, y, cx, cy, ringR, n, band = 7) {
  if (!n) return -1;
  const d = Math.hypot(x - cx, y - cy);
  if (Math.abs(d - ringR) > band) return -1;
  let a = Math.atan2(y - cy, x - cx) + Math.PI / 2; // 0 at 12 o'clock, clockwise
  a = ((a % TAU) + TAU) % TAU;
  return Math.min(n - 1, Math.floor(a / (TAU / n)));
}
