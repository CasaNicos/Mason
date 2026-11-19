// document.hidden handling (bundle design §5 "Animations pause when document.hidden").
// One listener for the whole bundle: the gsap ticker sleeps while hidden, and every continuous
// loop we own (LogoLoop, ShinyText, ElectricBorder, Stack autoplay, useRhApi polling) subscribes
// through onVisibility()/useDocumentVisible() and stops scheduling work.
import { useContext, useEffect, useState } from 'react';
import { WidgetCtx } from './context.js';
import { gsap } from 'gsap';

const subs = new Set();
let installed = false;
let sleptByUs = false;

export const isHidden = () => typeof document !== 'undefined' && !!document.hidden;

function onChange() {
  const hidden = isHidden();
  try {
    if (hidden) {
      gsap.ticker.sleep();
      sleptByUs = true;
    } else if (sleptByUs) {
      sleptByUs = false;
      gsap.ticker.wake();
    }
  } catch {
    /* gsap absent or torn down: nothing to pause */
  }
  subs.forEach(fn => {
    try {
      fn(hidden);
    } catch {
      /* a subscriber must never break the others */
    }
  });
}

export function installVisibility() {
  if (installed || typeof document === 'undefined') return;
  installed = true;
  // gsap wakes its ticker on import and only auto-sleeps after `autoSleep` idle frames
  // (default 120 ≈ 2 s). 60 halves that idle rAF tail.
  try {
    gsap.config({ autoSleep: 60 });
  } catch {
    /* ignore */
  }
  document.addEventListener('visibilitychange', onChange);
  if (isHidden()) onChange();
}

/** fn(hidden:boolean) on every change. Returns unsubscribe. */
export function onVisibility(fn) {
  installVisibility();
  subs.add(fn);
  return () => subs.delete(fn);
}

/** React: true while the document is visible. */
export function useDocumentVisible() {
  const [visible, setVisible] = useState(!isHidden());
  useEffect(() => {
    setVisible(!isHidden());
    return onVisibility(h => setVisible(!h));
  }, []);
  return visible;
}

// ── S3-F6: per-widget on-screen store ─────────────────────────────────────────
// document.hidden only covers a hidden tab. Inside Mason a widget is also invisible when its page
// section is hidden (another tab page is shown), when it is scrolled out of view, or when its
// Gridstack item is collapsed. MasonUI.mountWidget() gives every widget root one store, driven by an
// IntersectionObserver on the widget body, and puts it on WidgetCtx.screen. Continuous motion
// (useMotionAllowed) and useRhApi polling pause while it reads false. Without a provider (tests,
// MasonUI.mount) a widget is always "on screen".

/** {get() → bool, set(bool), subscribe(fn) → unsubscribe}. Starts on screen. */
export function createScreenStore(initial = true) {
  let on = !!initial;
  const subs = new Set();
  return {
    get: () => on,
    set(next) {
      const v = !!next;
      if (v === on) return;
      on = v;
      subs.forEach(fn => {
        try {
          fn(on);
        } catch {
          /* a subscriber must never break the others */
        }
      });
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    }
  };
}

/** React: true while this widget is on screen (always true outside MasonUI.mountWidget). */
export function useOnScreen() {
  const ctx = useContext(WidgetCtx);
  const store = (ctx && ctx.screen) || null;
  const [on, setOn] = useState(() => (store ? store.get() : true));
  useEffect(() => {
    if (!store) {
      setOn(true);
      return undefined;
    }
    setOn(store.get());
    return store.subscribe(v => setOn(!!v));
  }, [store]);
  return on;
}
