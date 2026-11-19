// prefers-reduced-motion, for code that MotionConfig reducedMotion="user" does not reach:
// gsap timelines, canvas/rAF loops and the MasonUI.effects overlays (they fall back to a
// 150 ms opacity fade, bundle design §5).
import { useEffect, useState } from 'react';
import { useDocumentVisible, useOnScreen } from './visibility.js';

const QUERY = '(prefers-reduced-motion: reduce)';
export const FADE_MS = 150;

function mql() {
  try {
    return typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(QUERY) : null;
  } catch {
    return null;
  }
}

export function prefersReducedMotion() {
  const m = mql();
  return !!(m && m.matches);
}

/** React: live prefers-reduced-motion. */
export function usePrefersReducedMotion() {
  const [reduce, setReduce] = useState(prefersReducedMotion());
  useEffect(() => {
    const m = mql();
    if (!m) return undefined;
    const h = () => setReduce(!!m.matches);
    h();
    if (m.addEventListener) m.addEventListener('change', h);
    else if (m.addListener) m.addListener(h);
    return () => {
      if (m.removeEventListener) m.removeEventListener('change', h);
      else if (m.removeListener) m.removeListener(h);
    };
  }, []);
  return reduce;
}

/** React: true when a continuous loop may run (document visible AND motion allowed AND, inside a
 *  mounted widget, the widget on screen — S3-F6). */
export function useMotionAllowed() {
  const visible = useDocumentVisible();
  const reduce = usePrefersReducedMotion();
  const onScreen = useOnScreen();
  return visible && !reduce && onScreen;
}
