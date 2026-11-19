// Squarified treemap layout (Bruls, Huizing, van Wijk 2000). Pure function, no DOM.
// Built from scratch: React Bits has no chart components (S3 §2 "Charts").
//
// squarify(items, {x, y, w, h}, valueOf) → [{item, value, x, y, w, h}]
//  - tile area ∝ value; tiles exactly fill the rect, never overlap
//  - non-finite, zero and negative values are dropped
//  - items are laid out largest first (stable for ties)

/** Worst aspect ratio of a strip of areas laid along a side of length `side`. */
export function worstRatio(areas, side) {
  if (!areas.length || !(side > 0)) return Infinity;
  let s = 0;
  let max = -Infinity;
  let min = Infinity;
  for (const a of areas) {
    s += a;
    if (a > max) max = a;
    if (a < min) min = a;
  }
  if (!(s > 0) || !(min > 0)) return Infinity;
  const s2 = s * s;
  const w2 = side * side;
  return Math.max((w2 * max) / s2, s2 / (w2 * min));
}

export function squarify(items, rect, valueOf) {
  const R = rect || {};
  let x = Number(R.x) || 0;
  let y = Number(R.y) || 0;
  let w = Number(R.w) || 0;
  let h = Number(R.h) || 0;
  if (!(w > 0) || !(h > 0) || !Array.isArray(items)) return [];
  const data = items
    .map((item, i) => {
      let v;
      try {
        v = valueOf(item);
      } catch {
        v = NaN;
      }
      return { item, i, value: typeof v === 'number' ? v : NaN };
    })
    .filter(d => Number.isFinite(d.value) && d.value > 0)
    .sort((a, b) => b.value - a.value || a.i - b.i);
  if (!data.length) return [];
  const total = data.reduce((a, d) => a + d.value, 0);
  const scale = (w * h) / total;
  const areas = data.map(d => d.value * scale);
  const out = [];

  const layout = idx => {
    const s = idx.reduce((a, k) => a + areas[k], 0);
    if (w >= h) {
      // strip is a column along the left edge
      const cw = Math.min(w, s / h);
      let yy = y;
      idx.forEach((k, j) => {
        const th = j === idx.length - 1 ? y + h - yy : areas[k] / cw;
        out.push({ item: data[k].item, value: data[k].value, x, y: yy, w: cw, h: th });
        yy += th;
      });
      x += cw;
      w -= cw;
    } else {
      // strip is a row along the top edge
      const rh = Math.min(h, s / w);
      let xx = x;
      idx.forEach((k, j) => {
        const tw = j === idx.length - 1 ? x + w - xx : areas[k] / rh;
        out.push({ item: data[k].item, value: data[k].value, x: xx, y, w: tw, h: rh });
        xx += tw;
      });
      y += rh;
      h -= rh;
    }
  };

  let row = [];
  let i = 0;
  while (i < areas.length) {
    const side = Math.min(w, h);
    const cur = row.map(k => areas[k]);
    if (!row.length || worstRatio([...cur, areas[i]], side) <= worstRatio(cur, side)) {
      row.push(i);
      i += 1;
    } else {
      layout(row);
      row = [];
    }
  }
  if (row.length) layout(row);
  return out;
}
