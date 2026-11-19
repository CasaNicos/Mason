// MasonUI.effects — React Bits effects applied to VANILLA DOM nodes (WM design §7).
// Each effect mounts the real React Bits component into a fixed-position overlay sized to the
// target node (so it is never clipped by the widget's overflow:hidden) and removes itself.
// prefers-reduced-motion → a plain 150 ms opacity fade, no canvas, no rAF loop.
//
//   electrify(node, {color, ms, fadeMs, radius, chaos, speed}) → stop(immediate?)   ElectricBorder; one at a time
//     (a fading border counts: the next call removes it first). stop() fades, stop(true) removes now.
//     fadeMs: fade-in/out length, default 400 (WM §7.2 "fades out over the last 400 ms"), clamped to
//     [0, 1000], invalid → 400; 150 under
//     reduced motion. Set inline, so the CSS transition and the removal timer always agree.
//   pixelDissolve(node, {grid, ms, color, accent, accentShare, reverse}) → Promise<void>
//                                                                    PixelTransition (one-way, MODS)
//   spark(x, y, {color, count, radius, size, ms})                    ClickSpark burst at a viewport point
import { createElement as h } from 'react';
import { createRoot } from 'react-dom/client';
import { MotionConfig } from 'motion/react';
import { loadComponent } from '../registry.js';
import { trackRoot } from './debug.js';
import { prefersReducedMotion, FADE_MS } from './motion.js';
import { isHidden } from './visibility.js';

const ACCENT = '#4a9eff'; // Nocturne --color-accent
const isEl = n => typeof Element !== 'undefined' && n instanceof Element; // real Elements only

function overlay(cls) {
  const el = document.createElement('div');
  el.className = `mui-root mui-fx ${cls}`;
  el.setAttribute('aria-hidden', 'true');
  document.body.appendChild(el);
  return el;
}

function placeOver(el, node) {
  const r = node.getBoundingClientRect();
  el.style.left = `${r.left}px`;
  el.style.top = `${r.top}px`;
  el.style.width = `${r.width}px`;
  el.style.height = `${r.height}px`;
}

/** Keep an overlay glued to its node through scroll/resize; call onGone() once the node leaves
 *  the DOM (checked on every sync and every 250 ms). Returns cleanup. */
function follow(el, node, onGone) {
  let gone = false;
  const check = () => {
    if (gone || node.isConnected) return false;
    gone = true;
    if (typeof onGone === 'function') onGone();
    return true;
  };
  const sync = () => {
    if (!check()) placeOver(el, node);
  };
  const poll = setInterval(check, 250);
  sync();
  window.addEventListener('scroll', sync, true);
  window.addEventListener('resize', sync);
  let ro = null;
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(sync);
    ro.observe(node);
  }
  return () => {
    clearInterval(poll);
    window.removeEventListener('scroll', sync, true);
    window.removeEventListener('resize', sync);
    if (ro) ro.disconnect();
  };
}

function reactOverlay(el, name, element) {
  const root = createRoot(el);
  const untrack = trackRoot({ kind: 'effect', name });
  let dead = false;
  return {
    render(node) {
      if (!dead) root.render(h(MotionConfig, { reducedMotion: 'user' }, node || element));
    },
    unmount() {
      if (dead) return;
      dead = true;
      try {
        root.unmount();
      } finally {
        untrack();
      }
    }
  };
}

// ── electrify ────────────────────────────────────────────────────────────────────────────────
// Only ONE ElectricBorder in the DOM at any instant (bundle §5, WM §7.2). `currentElectric` keeps
// pointing at a border while it FADES OUT too, so the next electrify() removes it immediately.
let currentElectric = null;
export const FADE_MAX_MS = 1000; // WM needs 400; the Debugger requires removal < 1.5 s after stop()

/** fadeMs: finite numbers clamped to [0, FADE_MAX_MS]; anything else → 400 (DEBUG_F0 R3-1). */
function fadeFor(opts, reduce) {
  if (reduce) return FADE_MS;
  const v = opts.fadeMs;
  if (typeof v !== 'number' || !Number.isFinite(v)) return 400;
  return Math.min(FADE_MAX_MS, Math.max(0, v));
}

export function electrify(node, opts = {}) {
  if (!isEl(node) || !node.isConnected) return () => {};
  if (currentElectric) currentElectric(true);
  const color = typeof opts.color === 'string' ? opts.color : ACCENT;
  const reduce = prefersReducedMotion();
  const el = overlay(`mui-fx-electric${reduce ? ' mui-fx-fade' : ''}`);
  el.style.setProperty('--mui-fx-color', color);
  let ov = null;
  let stopped = false;
  let stop;
  const unfollow = follow(el, node, () => stop && stop(true)); // node removed → overlay + loop stop (WM)
  let autoTimer = null;
  let removeTimer = null;
  const fadeMs = fadeFor(opts, reduce);
  el.style.transitionProperty = 'opacity';
  el.style.transitionDuration = `${fadeMs}ms`;

  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    if (removeTimer) clearTimeout(removeTimer);
    removeTimer = null;
    if (currentElectric === stop) currentElectric = null;
    if (ov) ov.unmount();
    unfollow();
    el.remove();
  };

  // stop(): fade out, then remove. stop(true) — also from onGone and from the next electrify() —
  // removes NOW, including when a fade is already pending (it escalates; never ignored).
  // Idempotent: calls after removal are no-ops.
  stop = (immediate = false) => {
    if (removed) return;
    if (autoTimer) clearTimeout(autoTimer);
    autoTimer = null;
    if (immediate === true) {
      stopped = true;
      remove();
      return;
    }
    if (stopped) return; // a fade is already running
    stopped = true;
    el.classList.remove('is-in');
    removeTimer = setTimeout(remove, fadeMs);
  };
  currentElectric = stop;

  if (!reduce) {
    const radius = Number.isFinite(opts.radius) ? opts.radius : 18;
    ov = reactOverlay(el, 'ElectricBorder');
    loadComponent('ElectricBorder').then(
      EB => {
        if (stopped) return;
        ov.render(
          h(EB, {
            color,
            speed: Number.isFinite(opts.speed) ? opts.speed : 1,
            chaos: Number.isFinite(opts.chaos) ? opts.chaos : 0.12,
            borderRadius: radius,
            style: { width: '100%', height: '100%' }
          })
        );
      },
      () => stop(true)
    );
  }
  requestAnimationFrame(() => {
    if (!stopped) el.classList.add('is-in');
  });
  if (Number.isFinite(opts.ms) && opts.ms > 0) {
    autoTimer = setTimeout(() => stop(), Math.max(0, opts.ms - fadeMs));
  }
  return immediate => stop(immediate === true);
}

// ── pixelDissolve ────────────────────────────────────────────────────────────────────────────
export function pixelDissolve(node, opts = {}) {
  if (!isEl(node) || !node.isConnected) return Promise.resolve();
  const reverse = !!opts.reverse;
  const reduce = prefersReducedMotion();
  const ms = Number.isFinite(opts.ms) && opts.ms > 0 ? opts.ms : 380;

  // Hidden tab: gsap is asleep, so finish instantly in the final state.
  if (isHidden()) {
    node.style.visibility = reverse ? '' : 'hidden';
    return Promise.resolve();
  }

  if (reduce) {
    const el = overlay('mui-fx-pixel mui-fx-fade');
    placeOver(el, node);
    return new Promise(resolve => {
      node.style.transition = `opacity ${FADE_MS}ms ease`;
      node.style.opacity = reverse ? '0' : '1';
      void node.offsetWidth; // commit the start opacity so the transition actually runs (REVIEW_F0 nit)
      requestAnimationFrame(() => {
        node.style.opacity = reverse ? '1' : '0';
      });
      setTimeout(() => {
        el.remove();
        node.style.transition = '';
        if (reverse) node.style.opacity = '';
        resolve();
      }, FADE_MS);
    });
  }

  const grid = Math.max(2, Math.min(48, Math.round(Number.isFinite(opts.grid) ? opts.grid : 12)));
  const r = node.getBoundingClientRect();
  const rows = r.width > 0 && r.height > 0 ? Math.max(1, Math.round((grid * r.height) / r.width)) : grid;
  const el = overlay('mui-fx-pixel');
  let onGone = null;
  const unfollow = follow(el, node, () => onGone && onGone());
  const ov = reactOverlay(el, 'PixelTransition');

  return new Promise(resolve => {
    let finished = false;
    onGone = () => finish();
    const finish = () => {
      if (finished) return;
      finished = true;
      ov.unmount();
      unfollow();
      el.remove();
      resolve();
    };
    const afterCover = () => {
      if (reverse) return finish();
      node.style.visibility = 'hidden'; // content hidden under the full cover (WM §7.4 step 3)
      el.classList.add('is-out'); // 180 ms fade to 0 + scale(.96)
      setTimeout(finish, 180);
      return undefined;
    };
    // safety net: never leave a promise hanging (e.g. tab hidden mid-dissolve)
    const guard = setTimeout(finish, ms + 1500);
    loadComponent('PixelTransition').then(
      PT => {
        if (finished) return;
        ov.render(
          h(PT, {
            mode: reverse ? 'uncover' : 'cover',
            gridSize: grid,
            rows,
            pixelColor: typeof opts.color === 'string' ? opts.color : 'rgba(16,22,38,1)',
            accentColor: typeof opts.accent === 'string' ? opts.accent : ACCENT,
            accentShare: Number.isFinite(opts.accentShare) ? opts.accentShare : 0.15,
            animationStepDuration: ms / 1000,
            className: 'mui-fx-pixel__card',
            style: { width: '100%', height: '100%' },
            aspectRatio: '0',
            onDone: () => {
              clearTimeout(guard);
              afterCover();
            }
          })
        );
      },
      () => {
        clearTimeout(guard);
        finish();
      }
    );
  });
}

// ── spark ────────────────────────────────────────────────────────────────────────────────────
export function spark(x, y, opts = {}) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  const color = typeof opts.color === 'string' ? opts.color : ACCENT;
  const reduce = prefersReducedMotion();
  if (reduce) {
    const el = overlay('mui-fx-spark mui-fx-fade');
    el.style.left = `${x - 4}px`;
    el.style.top = `${y - 4}px`;
    el.style.setProperty('--mui-fx-color', color);
    requestAnimationFrame(() => el.classList.add('is-out'));
    setTimeout(() => el.remove(), FADE_MS + 50);
    return;
  }
  const radius = Number.isFinite(opts.radius) ? opts.radius : 22;
  const size = Number.isFinite(opts.size) ? opts.size : 10;
  const ms = Number.isFinite(opts.ms) ? opts.ms : 400;
  const R = Math.ceil(radius + size + 4);
  const el = overlay('mui-fx-spark');
  el.style.left = `${x - R}px`;
  el.style.top = `${y - R}px`;
  el.style.width = `${2 * R}px`;
  el.style.height = `${2 * R}px`;
  const ov = reactOverlay(el, 'ClickSpark');
  let done = false;
  const cleanup = () => {
    if (done) return;
    done = true;
    clearTimeout(guard);
    ov.unmount();
    el.remove();
  };
  const guard = setTimeout(cleanup, ms + 400);
  loadComponent('ClickSpark').then(
    CS => {
      if (done) return;
      ov.render(
        h(CS, {
          sparkColor: color,
          sparkSize: size,
          sparkRadius: radius,
          sparkCount: Number.isFinite(opts.count) ? opts.count : 8,
          duration: ms,
          burst: { x: R, y: R },
          onIdle: () => setTimeout(cleanup, 0)
        })
      );
    },
    cleanup
  );
}
