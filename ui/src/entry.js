// window.MasonUI — the only entry of the Mason React bundle (docs/ui/REACT_BITS_BUNDLE_DESIGN.md §3).
// Loaded by the template as <script type="module" src="/static/ui/mason-ui.js"> plus
// <link rel="stylesheet" href="/static/ui/mason-ui.css">.
//
//   MasonUI.ready                          Promise, resolves once this entry has run
//   MasonUI.mountWidget(type, body, inst)  → {refresh(), destroy()}   one React root per instance
//   MasonUI.mount(el, name, props)         → {update(props), unmount()}  any widget or React Bits component
//   MasonUI.catalog({internal?})           → {type: {title, icon, group, multi, size, defaults, desc, settings, pages, ...}}
//   MasonUI.effects.{electrify, pixelDissolve, spark}
//   MasonUI.bridge.{emitChartSymbol, onChartSymbol, emitGo, onGo}
//   MasonUI.timeTicks.{pickTimeTicks, pickScaleTicks, fullLabel, isIntraday, makeMeasure}  x-axis ticks (lib/timeTicks.js)
//   MasonUI.livePrice.{start, attach, classify, fitRange, ...}  live price line on MasonChart (lib/livePrice.js)
//   MasonUI.__debug                        test hook: roots / intervals / rAF counters (see lib/debug.js)
import './lib/debug-boot.js'; // fallback install; the harness/template load it as a classic script first
import './theme.css';
import { createElement as h, Component } from 'react';
import { createRoot } from 'react-dom/client';
import { MotionConfig } from 'motion/react';
import { catalog, hasWidget, widgetMeta, loadComponent, mountables } from './registry.js';
import { setAssetBase, isMockMode, __resetApiCache } from './lib/api.js';
import { WidgetCtx } from './lib/context.js';
import { Loading } from './lib/states.jsx';
import { installVisibility, createScreenStore } from './lib/visibility.js';
import { trackRoot, install as installDebug, snapshot } from './lib/debug.js';
import { electrify, pixelDissolve, spark } from './lib/effects.js';
import { emitChartSymbol, onChartSymbol, emitGo, onGo } from './lib/bridge.js';
import timeTicks from './lib/timeTicks.js'; // Task 3: shared x-axis tick picker (also used by MasonChart)
import livePrice from './lib/livePrice.js'; // live last-price line on MasonChart (off unless the template starts it)
import './lib/livePrice.css';

const VERSION = '0.1.0';

installVisibility();
try {
  setAssetBase(new URL(/* @vite-ignore */ './', import.meta.url).href);
} catch {
  /* keep the /static/ui/ default */
}

// ── error boundary: one crashing widget never takes the page (or its siblings) down ──────────
class Boundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return h(
        'div',
        { className: 'mui-state mui-state--error', role: 'alert' },
        h('span', { className: 'mui-state__icon', 'aria-hidden': 'true' }, '!'),
        h('span', { className: 'mui-state__text' }, `This widget crashed: ${String(this.state.error && this.state.error.message)}`),
        h('button', { type: 'button', className: 'mui-btn mui-btn--ghost', onClick: () => this.setState({ error: null }) }, 'Retry')
      );
    }
    return this.props.children;
  }
}

function Shell({ kind, name, ready, children }) {
  return h(
    MotionConfig,
    { reducedMotion: 'user' },
    h(
      'div',
      { className: `mui-root mui-${kind}`, 'data-mui-type': name, 'data-mui-ready': ready ? '1' : '0' },
      h(Boundary, null, children)
    )
  );
}

function unknownType(name) {
  return h(
    'div',
    { className: 'mui-state mui-state--error', role: 'alert' },
    h('span', { className: 'mui-state__icon', 'aria-hidden': 'true' }, '!'),
    h('span', { className: 'mui-state__text' }, `Unknown widget type: ${String(name)}`)
  );
}

const isEl = el => typeof Element !== 'undefined' && el instanceof Element;
let seq = 0;

// One live React root per element (DEBUG_F0 F4). Mounting again on the same element first
// destroys the previous root, so WM remount/settings paths can't leave two roots fighting over
// one DOM node (which threw removeChild errors on destroy).
const owners = new WeakMap(); // element → {dispose}
function claim(el, dispose) {
  const prev = owners.get(el);
  if (prev) prev.dispose();
  const token = { dispose };
  owners.set(el, token);
  return () => {
    if (owners.get(el) === token) owners.delete(el);
  };
}

/**
 * mountWidget(type, body, inst) → {refresh(), destroy()}  (the WM catalog adapter)
 *   inst = {id?, config?, saveConfig?(cfg)}. The widget component receives props
 *   {config, saveConfig, instanceId, type}. config = {...meta.defaults, ...inst.config}.
 *   saveConfig(next) replaces the instance config, re-renders, and calls inst.saveConfig(next).
 */
function mountWidget(type, body, inst = {}) {
  if (!isEl(body)) throw new TypeError('MasonUI.mountWidget: body must be an Element');
  inst = inst && typeof inst === 'object' ? inst : {};
  const name = String(type);
  const instanceId = inst.id != null ? String(inst.id) : `${name}-${++seq}`;
  const meta = widgetMeta(name);
  const defaults = (meta && meta.defaults) || {};
  let config = { ...defaults, ...(inst.config && typeof inst.config === 'object' ? inst.config : {}) };
  let Comp = null;
  let failed = null;
  let signal = 0;
  let destroyed = false;

  const handle = {};
  const release = claim(body, () => handle.destroy && handle.destroy());
  const root = createRoot(body);
  const untrack = trackRoot({ kind: 'widget', name: `${name}#${instanceId}` });

  // S3-F6: on-screen store (lib/visibility.js). The body is "on screen" while any part of it is within
  // 200 px of the viewport; a hidden page section, a collapsed Gridstack item or a scrolled-away widget
  // reads off screen. Continuous motion and useRhApi polling follow it. body[data-mui-onscreen] mirrors it.
  const screen = createScreenStore(true);
  body.dataset.muiOnscreen = '1';
  let io = null;
  if (typeof IntersectionObserver === 'function') {
    try {
      io = new IntersectionObserver(
        entries => {
          const e = entries && entries[entries.length - 1];
          if (!e || destroyed) return;
          const on = !!e.isIntersecting;
          body.dataset.muiOnscreen = on ? '1' : '0';
          screen.set(on);
        },
        { rootMargin: '200px' }
      );
      io.observe(body);
    } catch {
      io = null; // no observer: always on screen (the pre-F6 behavior)
    }
  }
  let ctxValue = null;
  const ctxFor = () => {
    if (!ctxValue || ctxValue.refreshSignal !== signal) ctxValue = { refreshSignal: signal, type: name, instanceId, screen };
    return ctxValue;
  };

  const saveConfig = next => {
    if (destroyed) return;
    const cfg = next && typeof next === 'object' ? next : {};
    config = { ...defaults, ...cfg };
    try {
      if (typeof inst.saveConfig === 'function') inst.saveConfig(cfg);
    } finally {
      render();
    }
  };

  function render() {
    if (destroyed) return;
    let content;
    if (!hasWidget(name)) content = unknownType(name);
    else if (failed) content = h('div', { className: 'mui-state mui-state--error', role: 'alert' }, `Failed to load widget: ${String(failed.message || failed)}`);
    else if (!Comp) content = h(Loading, { label: 'Loading widget…' });
    else
      content = h(
        WidgetCtx.Provider,
        { value: ctxFor() },
        h(Comp, { config, saveConfig, instanceId, type: name })
      );
    root.render(h(Shell, { kind: 'widget', name, ready: !!Comp }, content));
  }

  render();
  if (hasWidget(name)) {
    loadComponent(name).then(
      C => {
        Comp = C;
        render();
      },
      err => {
        failed = err;
        render();
      }
    );
  }

  handle.refresh = () => {
    if (destroyed) return;
    signal += 1;
    render();
  };
  handle.destroy = () => {
    if (destroyed) return;
    destroyed = true;
    if (io) {
      try {
        io.disconnect();
      } catch {
        /* ignore */
      }
      io = null;
    }
    try {
      root.unmount();
    } finally {
      untrack();
      release();
      delete body.dataset.muiOnscreen;
    }
  };
  return { refresh: handle.refresh, destroy: handle.destroy };
}

/** mount(el, name, props) → {update(props), unmount()} for any widget type or React Bits component. */
function mount(el, name, props = {}) {
  if (!isEl(el)) throw new TypeError('MasonUI.mount: el must be an Element');
  const nm = String(name);
  let current = props && typeof props === 'object' ? props : {};
  let Comp = null;
  let failed = null;
  let dead = false;
  const api = {};
  const release = claim(el, () => api.unmount && api.unmount());
  const root = createRoot(el);
  const untrack = trackRoot({ kind: 'mount', name: nm });
  const render = () => {
    if (dead) return;
    let content = null;
    if (failed) content = unknownType(nm);
    else if (Comp) content = h(Comp, current);
    root.render(h(Shell, { kind: 'mount', name: nm, ready: !!Comp }, content));
  };
  render();
  loadComponent(nm).then(
    C => {
      Comp = C;
      render();
    },
    err => {
      failed = err;
      render();
    }
  );
  api.update = next => {
    current = next && typeof next === 'object' ? next : {};
    render();
  };
  api.unmount = () => {
    if (dead) return;
    dead = true;
    try {
      root.unmount();
    } finally {
      untrack();
      release();
    }
  };
  return { update: api.update, unmount: api.unmount };
}

const MasonUI = {
  version: VERSION,
  ready: Promise.resolve(),
  mountWidget,
  mount,
  catalog,
  effects: { electrify, pixelDissolve, spark },
  bridge: { emitChartSymbol, onChartSymbol, emitGo, onGo },
  timeTicks,
  livePrice,
  isMock: isMockMode,
  __debug: {
    snapshot,
    roots: () => snapshot().roots,
    install: installDebug,
    mountables,
    resetApi: __resetApiCache
  }
};

if (typeof window !== 'undefined') {
  // A pre-shim may already exist (see ui/README.md "Template wiring"): keep its `ready` promise,
  // which resolves on the 'masonui:ready' event dispatched below.
  const pre = window.MasonUI;
  if (pre && pre.ready && typeof pre.ready.then === 'function') MasonUI.ready = pre.ready;
  window.MasonUI = MasonUI;
  try {
    window.dispatchEvent(new CustomEvent('masonui:ready', { detail: { version: VERSION } }));
  } catch {
    /* old browsers */
  }
}

export default MasonUI;
