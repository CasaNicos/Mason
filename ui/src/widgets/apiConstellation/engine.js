// Network-constellation canvas engine (UI overhaul Task 6). Built from scratch: no React Bits piece draws
// a node-link graph (see the widget's index.jsx header for why OrbitImages was not used).
//
// One <canvas> draws everything that moves (orbit guides, links, data pulses, the core + its background-job
// ring, the focus reticle, the refresh ping). The nodes themselves are real <button>s rendered by React (focus
// ring, keyboard, screen readers); the engine only writes their CSS transform each frame. No React state
// changes per frame.
//   - ONE rAF loop, capped at 30 fps, DPR capped at 2 (like MasonFX / MasonChart).
//   - Runs only while the tab is visible AND the widget is on screen AND reduced motion is off. Under
//     prefers-reduced-motion it draws one static frame per change (no orbit, no pulses, no flicker).
//   - Health drives the motion: pulse speed = probe latency (fast link = fast pulses), pulse brightness =
//     freshness of the last check, LIMIT = amber packets that queue at a gate, DOWN = dark flickering link
//     with sparks that fizzle out half-way, NOT CHECKED = dim dashed link, no data, NOT CONFIGURED (R3-C) =
//     a faint static dotted link, no pulses, no ping, no flicker (optional source with no key: calm, on purpose).
//   - APIGPU (2026-11-17), the local AI model draws by its look (health.js lookOf): ASLEEP = a faint solid calm-blue
//     link, no pulses, the node breathes slowly (a soft glow, ~5.6 s); SLOW (on the processor, or since APIAMBER
//     less than half of it on the card) = amber pulses that crawl ~10× slower than an up link's; on the card = up;
//     card missing / Ollama down = down. Its node also wears a memory ring: a thin track plus an arc filled with the
//     share of the model held on the graphics card.
import { ageOf, freshness, jobBeatAge, lookOf, memShare, pulseSeconds, slowPulseSeconds } from '../apiShared/health.js';
import { CORE } from '../apiShared/focus.js';
import { coreRadius, labelOffset, linkFor, linkPoint, nodeAngle, planLayout, ringSegmentAt, safeArea, RING_GAP, RING_W, TAU } from './layout.js';

const FRAME_MS = 1000 / 30;
const DPR_CAP = 2;
const ORBIT_S = 210; // seconds per revolution
const OMEGA = TAU / ORBIT_S;
const BASE_R = 7; // node radius, px (broker / llm: BASE_R + 2)
const PING_S = 1.6;

const FALLBACK = {
  up: [95, 209, 160], // --color-positive
  warn: [224, 179, 90], // --color-warn
  down: [224, 118, 138], // --color-negative
  idle: [101, 113, 150], // --color-neutral-500
  accent: [74, 158, 255], // --color-accent
  accent3: [141, 196, 255], // --color-accent-300
  accent7: [28, 63, 110], // --color-accent-700
  text: [232, 237, 247] // --color-text
};
const VARS = { up: '--mui-up', warn: '--mui-warn', down: '--mui-down', idle: '--mui-faint', accent: '--mui-accent', accent3: '--mui-accent-300', accent7: '--mui-accent-700', text: '--mui-text' };
const TONE = { up: 'up', limit: 'warn', down: 'down', unknown: 'idle', unconfigured: 'idle', slow: 'warn', asleep: 'accent3' };
const BREATH_S = 5.6; // APIGPU: one slow "sleep light" breath of the asleep model's node
const GAUGE_GAP = 3.5; // APIGPU: px between the model node's edge and its memory ring
const JOB_TONE = { ok: 'up', off: 'idle', unknown: 'idle', stale: 'warn', restarting: 'warn', dead: 'down', gave_up: 'down' };

function parseColor(v) {
  const s = String(v || '').trim();
  let m = s.match(/^#([0-9a-f]{6})$/i);
  if (m) return [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16));
  m = s.match(/^#([0-9a-f]{3})$/i);
  if (m) return [0, 1, 2].map(i => parseInt(m[1][i] + m[1][i], 16));
  m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  return null;
}
const gradOk = g => !!g && typeof g.addColorStop === 'function'; // jsdom's canvas stub returns undefined
const raf = fn => (typeof requestAnimationFrame === 'function' ? requestAnimationFrame(fn) : setTimeout(() => fn(Date.now()), 33));
const caf = id => (typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame(id) : clearTimeout(id));
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a < 0 ? 0 : a > 1 ? 1 : +a.toFixed(3)})`;

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hash01(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i += 1) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return ((h >>> 0) % 10000) / 10000;
}
const frac = v => v - Math.floor(v);

/** LIMIT packets: travel to the gate (45 %), wait there (throttled), then continue. */
function throttled(s) {
  if (s < 0.4) return (s / 0.4) * 0.45;
  if (s < 0.65) return 0.45;
  return 0.45 + ((s - 0.65) / 0.35) * 0.55;
}

function makeCanvas(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
}

function glowSprite(rgb) {
  const c = makeCanvas(64, 64);
  const g = c.getContext('2d');
  if (!g || typeof g.createRadialGradient !== 'function') return c;
  const gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  if (!gradOk(gr)) return c;
  gr.addColorStop(0, rgba([255, 255, 255], 1));
  gr.addColorStop(0.12, rgba(rgb, 1));
  gr.addColorStop(0.38, rgba(rgb, 0.35));
  gr.addColorStop(1, rgba(rgb, 0));
  g.fillStyle = gr;
  g.fillRect(0, 0, 64, 64);
  return c;
}

export function createEngine({ stage, canvas }) {
  const ctx = canvas.getContext('2d', { alpha: true });
  const S = {
    w: 0,
    h: 0,
    dpr: 1,
    model: null,
    failed: false,
    nodes: [],
    byName: new Map(),
    els: new Map(), // name → node <button> (React refs attach before the parent's effects run)
    jobs: [],
    geo: null,
    inset: { top: 0, right: 0, bottom: 0, left: 0 },
    focus: null,
    hover: null, // {type: 'node', name} | {type: 'job', index} | {type: 'core'}
    run: false,
    reduce: false,
    visible: true,
    raf: 0,
    staticRaf: 0,
    lastTs: 0,
    lastDraw: 0,
    time: 0,
    phase: 0,
    omega: OMEGA,
    ping: -1,
    stars: null,
    colors: null,
    sprites: null,
    coreEl: null,
    coreT: '',
    tipEl: null,
    tipKey: null,
    tipSize: null,
    layoutDirty: true,
    destroyed: false,
    onLayout: null
  };
  const pt = { x: 0, y: 0 };

  function colors() {
    if (S.colors) return S.colors;
    let cs = null;
    try {
      cs = getComputedStyle(stage);
    } catch {
      cs = null;
    }
    const out = {};
    for (const k of Object.keys(FALLBACK)) out[k] = (cs && parseColor(cs.getPropertyValue(VARS[k]))) || FALLBACK[k];
    S.colors = out;
    S.sprites = {};
    for (const k of Object.keys(out)) S.sprites[k] = glowSprite(out[k]);
    return out;
  }

  function nodeFor(src) {
    let nd = S.byName.get(src.name);
    if (!nd) {
      nd = {
        name: src.name,
        seed: hash01(src.name),
        x: 0,
        y: 0,
        placed: false,
        dx: NaN,
        dy: NaN,
        el: null,
        lbl: null,
        wFull: 0,
        wShort: 0,
        lh: 0,
        lw: 0,
        r: BASE_R,
        link: {},
        nextFlicker: 0,
        flickerUntil: 0,
        fizzleAt: 0,
        fizzleT0: -9
      };
    }
    nd.src = src;
    nd.el = S.els.get(src.name) || null;
    nd.state = src.state;
    nd.look = lookOf(src); // = state, except the local model's own looks (asleep / slow)
    nd.gauge = !!src.model; // APIGPU: the local model wears a memory ring
    nd.share = src.model ? memShare(src.model) : null;
    nd.base = src.big ? BASE_R + 2 : BASE_R;
    return nd;
  }

  // ── layout ────────────────────────────────────────────────────────────────────────────────────────
  function measure() {
    for (const nd of S.nodes) {
      nd.el = S.els.get(nd.name) || null;
      nd.dx = NaN; // force the node + label transforms to be written again
      if (!nd.el) continue;
      const full = nd.el.querySelector('.apic-lbl-full');
      const short = nd.el.querySelector('.apic-lbl-short');
      nd.lbl = nd.el.querySelector('.apic-lbl');
      nd.wFull = full ? Math.ceil(full.offsetWidth) : 0;
      nd.wShort = short ? Math.ceil(short.offsetWidth) : 0;
      nd.lh = Math.ceil(Math.max(full ? full.offsetHeight : 0, short ? short.offsetHeight : 0)) || 14;
    }
  }

  function relayout() {
    S.layoutDirty = false;
    const w = Math.max(0, Math.round(stage.clientWidth));
    const h = Math.max(0, Math.round(stage.clientHeight));
    const dpr = Math.min(DPR_CAP, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
    if (w !== S.w || h !== S.h || dpr !== S.dpr) {
      S.w = w;
      S.h = h;
      S.dpr = dpr;
      canvas.width = Math.max(1, Math.round(w * dpr));
      canvas.height = Math.max(1, Math.round(h * dpr));
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      S.stars = null;
    }
    if (!w || !h) {
      S.geo = null;
      return;
    }
    measure();
    const sa = safeArea(w, h, S.inset);
    const items = S.nodes.map(nd => ({ wFull: nd.wFull, wShort: nd.wShort, h: nd.lh, r: nd.base }));
    S.geo = planLayout({ w, h, inset: S.inset, items, coreR: coreRadius(Math.max(40, sa.x1 - sa.x0), Math.max(40, sa.y1 - sa.y0)) });
    const mode = S.geo.mode;
    stage.dataset.labels = mode;
    for (const nd of S.nodes) {
      nd.r = nd.base * S.geo.scale;
      nd.lw = mode === 'full' ? nd.wFull : mode === 'short' ? nd.wShort : 0;
      if (nd.el) nd.el.style.setProperty('--apic-r', `${nd.r.toFixed(2)}px`);
    }
    if (S.coreEl) S.coreEl.style.setProperty('--apic-core', `${S.geo.coreR.toFixed(1)}px`);
    if (typeof S.onLayout === 'function') S.onLayout({ mode, cramped: S.geo.cramped, w, h });
  }

  function place(dt, snap) {
    const g = S.geo;
    if (!g) return;
    const n = S.nodes.length;
    const k = snap ? 1 : 1 - Math.exp(-dt * 9);
    for (let i = 0; i < n; i += 1) {
      const nd = S.nodes[i];
      const a = nodeAngle(i, n, S.phase);
      const tx = g.cx + g.rx * Math.cos(a);
      const ty = g.cy + g.ry * Math.sin(a);
      if (!nd.placed || snap) {
        nd.x = tx;
        nd.y = ty;
        nd.placed = true;
      } else {
        nd.x += (tx - nd.x) * k;
        nd.y += (ty - nd.y) * k;
      }
      linkFor(nd.x, nd.y, g.cx, g.cy, nd.r, g.coreR + RING_GAP + RING_W / 2 + 1, nd.link);
    }
  }

  // ── drawing ───────────────────────────────────────────────────────────────────────────────────────
  function stars() {
    if (S.stars) return S.stars;
    const { w, h, dpr } = S;
    const c = makeCanvas(w * dpr, h * dpr);
    const g = c.getContext('2d');
    if (g && typeof g.arc === 'function') {
      const col = colors();
      const rnd = mulberry32(1337);
      const n = Math.max(24, Math.min(180, Math.round((w * h) / 5200)));
      g.scale(dpr, dpr);
      for (let i = 0; i < n; i += 1) {
        const x = rnd() * w;
        const y = rnd() * h;
        const r = 0.35 + rnd() * rnd() * 0.95;
        g.fillStyle = rgba(rnd() < 0.7 ? col.text : col.accent3, 0.06 + rnd() * 0.28);
        g.beginPath();
        g.arc(x, y, r, 0, TAU);
        g.fill();
      }
    }
    S.stars = c;
    return c;
  }

  function sprite(c, key, x, y, size, alpha) {
    const sp = S.sprites && S.sprites[key];
    if (!sp || alpha <= 0.01) return;
    c.globalAlpha = Math.min(1, alpha);
    c.drawImage(sp, x - size / 2, y - size / 2, size, size);
  }

  const BASE_ALPHA = { up: 0.4, limit: 0.36, slow: 0.34, down: 0.16, unconfigured: 0.1, asleep: 0.2 };
  function linkAlpha(nd) {
    let a = BASE_ALPHA[nd.look] || 0.15; // not checked: 0.15, as before
    const hot = (S.hover && S.hover.type === 'node' && S.hover.name === nd.name) || S.focus === nd.name;
    if (hot) a = Math.min(0.95, a * 2.3 + 0.1);
    else if (S.focus) a *= 0.32;
    if (S.failed) a *= 0.6;
    return { a, hot };
  }

  function drawOrbit(c, g, col) {
    c.save();
    c.lineWidth = 1;
    c.strokeStyle = rgba(col.accent3, 0.09);
    c.beginPath();
    c.ellipse(g.cx, g.cy, g.rx, g.ry, 0, 0, TAU);
    c.stroke();
    c.setLineDash([2, 11]);
    c.lineDashOffset = S.reduce ? 0 : -S.time * 9;
    c.strokeStyle = rgba(col.accent3, 0.22);
    c.beginPath();
    c.ellipse(g.cx, g.cy, g.rx, g.ry, 0, 0, TAU);
    c.stroke();
    c.setLineDash([1, 7]);
    c.lineDashOffset = S.reduce ? 0 : S.time * 5;
    c.strokeStyle = rgba(col.accent3, 0.1);
    c.beginPath();
    c.ellipse(g.cx, g.cy, g.rx * 0.58, g.ry * 0.58, 0, 0, TAU);
    c.stroke();
    c.restore();
  }

  function drawLinks(c, col) {
    c.save();
    c.lineCap = 'round';
    for (const nd of S.nodes) {
      const l = nd.link;
      const tone = col[TONE[nd.look] || 'idle'];
      let { a, hot } = linkAlpha(nd);
      if (nd.look === 'down' && !S.reduce) {
        if (S.time > nd.nextFlicker) {
          nd.flickerUntil = S.time + 0.08 + Math.random() * 0.22;
          nd.nextFlicker = S.time + 1.4 + Math.random() * 3.2;
        }
        if (S.time < nd.flickerUntil && Math.random() > 0.35) a = Math.max(a, hot ? 0.95 : 0.55);
      }
      const grad = c.createLinearGradient(l.x0, l.y0, l.x1, l.y1);
      if (gradOk(grad)) {
        grad.addColorStop(0, rgba(tone, a));
        grad.addColorStop(1, rgba(nd.look === 'up' || nd.look === 'limit' || nd.look === 'slow' ? col.accent : tone, a * 0.55));
        c.strokeStyle = grad;
      } else c.strokeStyle = rgba(tone, a);
      c.lineWidth = hot ? 2 : nd.look === 'up' ? 1.3 : 1;
      if (nd.look === 'down') c.setLineDash([2, 5]);
      else if (nd.look === 'unconfigured') {
        c.setLineDash([1, 6]); // static dots: nothing flows, nothing is wrong
        c.lineDashOffset = 0;
      } else if (nd.look === 'unknown') {
        c.setLineDash([3, 6]);
        c.lineDashOffset = S.reduce ? 0 : -S.time * 6;
      } else c.setLineDash([]); // up / limit / slow, and asleep: a faint solid line (connected, resting)
      c.beginPath();
      c.moveTo(l.x0, l.y0);
      c.quadraticCurveTo(l.qx, l.qy, l.x1, l.y1);
      c.stroke();
      if (nd.look === 'limit') {
        // the throttle gate the amber packets queue at
        linkPoint(l, 0.45, pt);
        c.setLineDash([]);
        c.strokeStyle = rgba(col.warn, Math.min(1, a * 1.8));
        c.lineWidth = 1.5;
        c.beginPath();
        c.arc(pt.x, pt.y, 3.2, 0, TAU);
        c.stroke();
      }
    }
    c.restore();
  }

  function comet(c, key, l, t, size, alpha, trail) {
    for (let j = trail; j >= 1; j -= 1) {
      const tt = t - j * 0.028;
      if (tt < 0) continue;
      linkPoint(l, tt, pt);
      sprite(c, key, pt.x, pt.y, size * (1 - j * 0.14), alpha * (0.5 - j * 0.09));
    }
    linkPoint(l, t, pt);
    sprite(c, key, pt.x, pt.y, size, alpha);
  }

  function drawPulses(c) {
    const now = Date.now();
    c.save();
    c.globalCompositeOperation = 'lighter';
    const pingAge = S.ping >= 0 ? S.time - S.ping : 99;
    for (const nd of S.nodes) {
      const src = nd.src || {};
      const l = nd.link;
      let { a, hot } = linkAlpha(nd);
      a = Math.min(1, a * 2.3);
      const fresh = freshness(ageOf(S.model, src.lastChecked, now));
      const size = (hot ? 13 : 10) * (nd.base / BASE_R);
      if (nd.look === 'up') {
        const T = pulseSeconds(src.latencyMs);
        const count = hot ? 3 : 2;
        for (let k = 0; k < count; k += 1) comet(c, 'up', l, frac(S.time / T + k / count + nd.seed), size, a * fresh, 4);
      } else if (nd.look === 'limit') {
        const T = pulseSeconds(src.latencyMs) * 1.7;
        for (let k = 0; k < 2; k += 1) comet(c, 'warn', l, throttled(frac(S.time / T + k / 2 + nd.seed)), size * 0.85, a * fresh, 2);
      } else if (nd.look === 'slow') {
        // APIGPU: the model on the processor: amber pulses that crawl (~10× an up link's period), short tails
        const T = slowPulseSeconds(src.latencyMs);
        for (let k = 0; k < 3; k += 1) comet(c, 'warn', l, frac(S.time / T + k / 3 + nd.seed), size * 0.85, a * fresh, 2);
      } else if (nd.look === 'asleep') {
        // APIGPU: asleep: no data moves; the node breathes like a sleep light (a slow soft glow, nothing else)
        const b = 0.5 + 0.5 * Math.sin((S.time / BREATH_S) * TAU + nd.seed * TAU);
        if (nd.placed) sprite(c, 'accent3', nd.x, nd.y, nd.r * 5.2, 0.06 + 0.26 * b * (S.focus && S.focus !== nd.name ? 0.4 : 1));
      } else if (nd.look === 'down') {
        if (S.time > nd.fizzleAt) {
          nd.fizzleT0 = S.time;
          nd.fizzleAt = S.time + 2.4 + Math.random() * 3.2;
        }
        const e = (S.time - nd.fizzleT0) / 0.9;
        if (e >= 0 && e < 1) comet(c, 'down', l, e * 0.38, size * 0.8, (1 - e) * Math.max(0.5, a), 3);
      }
      if (pingAge < 1 && (nd.look === 'up' || nd.look === 'limit' || nd.look === 'slow')) {
        comet(c, nd.look === 'up' ? 'accent3' : 'warn', l, pingAge, size * 1.25, (1 - pingAge * 0.6) * 0.9, 5);
      }
    }
    c.restore();
  }

  // APIGPU: the local model's memory ring: a thin track around its node plus an arc (clockwise from 12 o'clock)
  // filled with the share of the model held in graphics-card memory. No numbers from the backend → the track only.
  function drawGauges(c, col) {
    c.save();
    c.lineCap = 'round';
    for (const nd of S.nodes) {
      if (!nd.gauge || !nd.placed) continue;
      const tone = col[TONE[nd.look] || 'idle'];
      const { a } = linkAlpha(nd);
      const R = nd.r + GAUGE_GAP;
      c.setLineDash(nd.look === 'down' ? [2, 3] : []);
      c.lineDashOffset = 0;
      c.lineWidth = 1.3;
      c.strokeStyle = rgba(tone, Math.min(0.55, 0.14 + a * 0.5));
      c.beginPath();
      c.arc(nd.x, nd.y, R, 0, TAU);
      c.stroke();
      if (nd.share != null && nd.share > 0.005) {
        c.setLineDash([]);
        c.lineWidth = 2.2;
        c.strokeStyle = rgba(tone, Math.min(1, 0.5 + a));
        c.beginPath();
        c.arc(nd.x, nd.y, R, -Math.PI / 2, -Math.PI / 2 + TAU * Math.min(1, nd.share));
        c.stroke();
      }
    }
    c.restore();
  }

  function jobAlpha(job, now) {
    if (job.status === 'ok') {
      const age = jobBeatAge(S.model, job, now);
      if (S.reduce || age == null || !(job.every > 0)) return 0.62;
      const m = age % job.every;
      return 0.5 + 0.5 * Math.exp(-m * (job.every < 3 ? 2.5 : 5));
    }
    if (job.status === 'off' || job.status === 'unknown') return 0.2;
    if (job.status === 'restarting' && !S.reduce) return 0.45 + 0.45 * (0.5 + 0.5 * Math.sin(S.time * 7));
    return 0.9;
  }

  function drawCore(c, g, col) {
    const now = Date.now();
    const R = g.coreR;
    const ringR = R + RING_GAP;
    const hotCore = S.focus === CORE || (S.hover && (S.hover.type === 'core' || S.hover.type === 'job'));
    c.save();
    c.globalCompositeOperation = 'lighter';
    sprite(c, 'accent', g.cx, g.cy, R * 7.5, 0.33);
    c.restore();
    c.save();
    const gr = typeof c.createRadialGradient === 'function' ? c.createRadialGradient(g.cx - R * 0.3, g.cy - R * 0.35, R * 0.1, g.cx, g.cy, R) : null;
    if (gradOk(gr)) {
      gr.addColorStop(0, rgba(col.accent3, 0.92));
      gr.addColorStop(0.55, rgba(col.accent, 0.55));
      gr.addColorStop(1, rgba(col.accent7, 0.9));
      c.fillStyle = gr;
    } else c.fillStyle = rgba(col.accent7, 0.9);
    c.beginPath();
    c.arc(g.cx, g.cy, R, 0, TAU);
    c.fill();
    const sw = !S.reduce && typeof c.createConicGradient === 'function' ? c.createConicGradient(S.time * 0.9, g.cx, g.cy) : null;
    if (gradOk(sw)) {
      sw.addColorStop(0, rgba(col.text, 0.28));
      sw.addColorStop(0.12, rgba(col.text, 0));
      sw.addColorStop(1, rgba(col.text, 0));
      c.fillStyle = sw;
      c.beginPath();
      c.arc(g.cx, g.cy, R - 1, 0, TAU);
      c.fill();
    }
    c.lineWidth = hotCore ? 1.6 : 1;
    c.strokeStyle = rgba(col.accent3, hotCore ? 0.9 : 0.55);
    c.beginPath();
    c.arc(g.cx, g.cy, R, 0, TAU);
    c.stroke();
    // background-job ring: one segment per supervised loop
    const n = S.jobs.length;
    c.lineCap = 'butt';
    if (!n) {
      c.setLineDash([2, 4]);
      c.lineDashOffset = S.reduce ? 0 : -S.time * 4;
      c.lineWidth = 2;
      c.strokeStyle = rgba(col.accent3, 0.25);
      c.beginPath();
      c.arc(g.cx, g.cy, ringR, 0, TAU);
      c.stroke();
      c.setLineDash([]);
    } else {
      const seg = TAU / n;
      const gap = Math.min(0.08, seg * 0.22);
      for (let i = 0; i < n; i += 1) {
        const job = S.jobs[i];
        const tone = col[JOB_TONE[job.status] || 'idle'];
        const hov = S.hover && S.hover.type === 'job' && S.hover.index === i;
        const a0 = -Math.PI / 2 + i * seg + gap / 2;
        c.lineWidth = hov ? RING_W + 2.5 : RING_W;
        c.strokeStyle = rgba(tone, hov ? 1 : jobAlpha(job, now));
        c.beginPath();
        c.arc(g.cx, g.cy, ringR, a0, a0 + seg - gap);
        c.stroke();
      }
    }
    c.restore();
  }

  function reticle(c, x, y, r, tone) {
    c.save();
    c.strokeStyle = rgba(tone, 0.95);
    c.lineWidth = 1.4;
    c.setLineDash([4, 4]);
    c.lineDashOffset = S.reduce ? 0 : -S.time * 14;
    c.beginPath();
    c.arc(x, y, r, 0, TAU);
    c.stroke();
    c.setLineDash([]);
    const rot = S.reduce ? Math.PI / 4 : -S.time * 0.8 + Math.PI / 4;
    for (let k = 0; k < 4; k += 1) {
      const a = rot + (k * Math.PI) / 2;
      c.beginPath();
      c.moveTo(x + Math.cos(a) * (r + 3), y + Math.sin(a) * (r + 3));
      c.lineTo(x + Math.cos(a) * (r + 8), y + Math.sin(a) * (r + 8));
      c.stroke();
    }
    c.restore();
  }

  function drawFocus(c, g, col) {
    if (S.focus === CORE) reticle(c, g.cx, g.cy, g.coreR + RING_GAP + RING_W + 6, col.accent3);
    else if (S.focus) {
      const nd = S.byName.get(S.focus);
      // (a node with a memory ring gets its reticle / hover ring just outside the ring)
      if (nd && nd.placed) reticle(c, nd.x, nd.y, nd.r + 6 + (nd.gauge ? GAUGE_GAP : 0), col[TONE[nd.look] || 'idle']);
    }
    if (S.hover && S.hover.type === 'node' && S.hover.name !== S.focus) {
      const nd = S.byName.get(S.hover.name);
      if (nd && nd.placed) {
        c.save();
        c.strokeStyle = rgba(col[TONE[nd.look] || 'idle'], 0.7);
        c.lineWidth = 1;
        c.beginPath();
        c.arc(nd.x, nd.y, nd.r + 4 + (nd.gauge ? GAUGE_GAP : 0), 0, TAU);
        c.stroke();
        c.restore();
      }
    }
  }

  function drawPing(c, g, col) {
    if (S.ping < 0 || S.reduce) return;
    const e = (S.time - S.ping) / PING_S;
    if (e >= 1) {
      S.ping = -1;
      return;
    }
    const r0 = g.coreR + RING_GAP;
    c.save();
    c.strokeStyle = rgba(col.accent3, (1 - e) * 0.38);
    c.lineWidth = 1.5;
    c.beginPath();
    c.ellipse(g.cx, g.cy, r0 + e * (g.rx - r0), r0 + e * (g.ry - r0), 0, 0, TAU);
    c.stroke();
    c.restore();
  }

  function render() {
    const c = ctx;
    if (!c || !S.w || !S.h) return;
    const col = colors();
    c.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    c.globalAlpha = 1;
    c.globalCompositeOperation = 'source-over';
    c.clearRect(0, 0, S.w, S.h);
    c.drawImage(stars(), 0, 0, S.w, S.h);
    const g = S.geo;
    if (!g) return;
    drawOrbit(c, g, col);
    drawLinks(c, col);
    if (!S.reduce) drawPulses(c);
    drawGauges(c, col);
    drawCore(c, g, col);
    drawFocus(c, g, col);
    drawPing(c, g, col);
    c.globalAlpha = 1;
  }

  // ── DOM (node buttons, core button, tooltip): reads first, then writes ─────────────────────────────
  function tipTarget() {
    const h = S.hover;
    const g = S.geo;
    if (!h || !g) return null;
    if (h.type === 'node') {
      const nd = S.byName.get(h.name);
      return nd && nd.placed ? { x: nd.x, y: nd.y, r: nd.r + 4 } : null;
    }
    if (h.type === 'core') return { x: g.cx, y: g.cy, r: g.coreR + RING_GAP + RING_W };
    if (h.type === 'job' && S.jobs.length) {
      const seg = TAU / S.jobs.length;
      const a = -Math.PI / 2 + (h.index + 0.5) * seg;
      const rr = g.coreR + RING_GAP;
      return { x: g.cx + Math.cos(a) * rr, y: g.cy + Math.sin(a) * rr, r: RING_W + 2 };
    }
    return null;
  }

  function writeDom() {
    const g = S.geo;
    const tip = S.tipEl;
    const key = S.hover ? JSON.stringify(S.hover) : null;
    if (tip && key && (key !== S.tipKey || !S.tipSize)) {
      S.tipKey = key;
      S.tipSize = { w: tip.offsetWidth, h: tip.offsetHeight }; // read before the writes below
    }
    if (!g) return;
    for (const nd of S.nodes) {
      if (!nd.el) continue;
      // !(… <= …) so a NaN (never written yet / forced rewrite) also writes
      if (!(Math.abs(nd.x - nd.dx) <= 0.04 && Math.abs(nd.y - nd.dy) <= 0.04)) {
        nd.el.style.transform = `translate3d(${nd.x.toFixed(2)}px,${nd.y.toFixed(2)}px,0)`;
        nd.dx = nd.x;
        nd.dy = nd.y;
        if (nd.lbl && nd.lw) {
          const o = labelOffset(nd.x - g.cx, nd.y - g.cy, nd.r, nd.lw, nd.lh);
          nd.lbl.style.transform = `translate3d(${o.x.toFixed(2)}px,${o.y.toFixed(2)}px,0)`;
        }
      }
      if (!nd.el.dataset.placed) nd.el.dataset.placed = '1';
    }
    if (S.coreEl) {
      const t = `translate3d(${g.cx.toFixed(2)}px,${g.cy.toFixed(2)}px,0)`;
      if (S.coreT !== t) {
        S.coreEl.style.transform = t;
        S.coreT = t;
      }
    }
    const tt = tip && key ? tipTarget() : null;
    if (tip && tt && S.tipSize) {
      const tw = S.tipSize.w;
      const th = S.tipSize.h;
      const W = S.w;
      const H = S.h;
      const right = tt.x >= g.cx;
      let x = right ? tt.x + tt.r + 10 : tt.x - tt.r - 10 - tw;
      let y = tt.y - th / 2;
      if (x < 6 || x + tw > W - 6) x = right ? tt.x - tt.r - 10 - tw : tt.x + tt.r + 10;
      if (x < 6 || x + tw > W - 6) {
        x = Math.min(Math.max(6, tt.x - tw / 2), Math.max(6, W - tw - 6));
        y = tt.y > g.cy ? tt.y - tt.r - 10 - th : tt.y + tt.r + 10;
      }
      y = Math.min(Math.max(6, y), Math.max(6, H - th - 6));
      tip.style.transform = `translate3d(${Math.round(x)}px,${Math.round(y)}px,0)`;
    }
  }

  // ── loop ─────────────────────────────────────────────────────────────────────────────────────────
  function step(dt) {
    S.time += dt;
    const still = !!S.focus || !!(S.hover && S.hover.type === 'node');
    const target = still ? 0 : OMEGA;
    S.omega += (target - S.omega) * Math.min(1, dt * 3.5);
    S.phase = (S.phase + S.omega * dt) % TAU;
    place(dt, false);
  }

  function frame(ts) {
    S.raf = 0;
    if (!S.run || S.destroyed) return;
    S.raf = raf(frame);
    // 30 fps cap with 4 ms of slack, so a 60 Hz display draws every 2nd frame even when rAF timestamps jitter
    // (a 1 ms slack aliased down to ~15 fps on irregular frame timing)
    if (S.lastDraw && ts - S.lastDraw < FRAME_MS - 4) return;
    const dt = S.lastTs ? Math.min(0.1, (ts - S.lastTs) / 1000) : 1 / 30;
    S.lastTs = ts;
    S.lastDraw = ts;
    if (S.layoutDirty) relayout();
    step(dt);
    render();
    writeDom();
  }

  function drawStatic() {
    if (S.destroyed || S.run || S.staticRaf || !S.visible) return;
    S.staticRaf = raf(() => {
      S.staticRaf = 0;
      if (S.destroyed || S.run) return;
      if (S.layoutDirty) relayout();
      place(0, true);
      render();
      writeDom();
    });
  }

  function start() {
    if (S.run || S.destroyed) return;
    S.run = true;
    stage.dataset.run = '1'; // test/debug hook: is the loop running?
    S.lastTs = 0;
    S.lastDraw = 0;
    S.raf = raf(frame);
  }

  function stop() {
    S.run = false;
    if (stage.dataset) stage.dataset.run = '0';
    if (S.raf) caf(S.raf);
    S.raf = 0;
  }

  const kick = () => (S.run ? undefined : drawStatic());

  return {
    /** model from useHealth (or null while loading); failed = showing the last good data. */
    setModel(model, failed = false) {
      const prev = S.model;
      const list = model && Array.isArray(model.sources) ? model.sources : [];
      const next = list.map(nodeFor);
      const changedSet = next.length !== S.nodes.length || next.some((nd, i) => S.nodes[i] !== nd);
      S.nodes = next;
      S.byName = new Map(next.map(nd => [nd.name, nd]));
      S.jobs = model && model.jobs ? model.jobs.list : [];
      if (prev && model && prev !== model && prev.fetchedAt !== model.fetchedAt) S.ping = S.time;
      S.model = model;
      S.failed = !!failed;
      S.tipSize = null; // tooltip text may have changed size
      if (changedSet || !prev) S.layoutDirty = true;
      kick();
    },
    /** React ref callbacks: the node <button> for `name` (null on unmount). */
    bindNode(name, el) {
      if (el) {
        if (S.els.get(name) === el) return;
        S.els.set(name, el);
      } else if (S.els.has(name)) S.els.delete(name);
      else return;
      const nd = S.byName.get(name);
      if (nd) {
        nd.el = el || null;
        nd.dx = NaN;
      }
      S.layoutDirty = true;
      kick();
    },
    bindCore(el) {
      S.coreEl = el;
      S.coreT = '';
      S.layoutDirty = true;
      kick();
    },
    bindTip(el) {
      S.tipEl = el;
    },
    /** Something React rendered changed size (labels, card): measure again on the next frame. */
    invalidate() {
      S.layoutDirty = true;
      kick();
    },
    /** Stage bands the orbit must keep clear of (px): the focus card, the HUD bar. */
    setInset(inset = {}) {
      const n = v => Math.max(0, Math.round(v || 0));
      const next = { top: n(inset.top), right: n(inset.right), bottom: n(inset.bottom), left: n(inset.left) };
      const cur = S.inset;
      if (next.top === cur.top && next.right === cur.right && next.bottom === cur.bottom && next.left === cur.left) return;
      S.inset = next;
      S.layoutDirty = true;
      kick();
    },
    setFocus(name) {
      S.focus = name || null;
      kick();
    },
    setHover(h) {
      S.hover = h || null;
      S.tipSize = null;
      kick();
    },
    /** run: animate (visible, on screen, motion allowed). reduce: prefers-reduced-motion. visible: on screen. */
    setMotion({ run, reduce, visible }) {
      S.reduce = !!reduce;
      S.visible = visible !== false;
      if (run && !S.reduce) start();
      else {
        stop();
        if (S.visible) drawStatic();
      }
    },
    onLayout(fn) {
      S.onLayout = fn;
    },
    /** Background-job ring segment under a stage point, or -1. */
    jobAt(x, y) {
      const g = S.geo;
      if (!g) return -1;
      return ringSegmentAt(x, y, g.cx, g.cy, g.coreR + RING_GAP, S.jobs.length);
    },
    /** Test/debug hook: {mode, cramped, nodes: {name: {x, y}}, running}. */
    debug() {
      const nodes = {};
      for (const nd of S.nodes) nodes[nd.name] = { x: Math.round(nd.x), y: Math.round(nd.y), lw: nd.lw, look: nd.look, gauge: nd.gauge, share: nd.share };
      return { mode: S.geo ? S.geo.mode : null, cramped: S.geo ? S.geo.cramped : null, geo: S.geo ? { ...S.geo } : null, nodes, running: S.run, w: S.w, h: S.h };
    },
    destroy() {
      S.destroyed = true;
      stop();
      if (S.staticRaf) caf(S.staticRaf);
      S.staticRaf = 0;
      S.nodes = [];
      S.byName = new Map();
      S.els = new Map();
      S.coreEl = null;
      S.tipEl = null;
    }
  };
}
