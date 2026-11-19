// newsShared/useBox.js — the content box of a News widget element, kept current by a ResizeObserver
// (one read per animation frame), so SVG widgets re-layout on every Gridstack resize / reflow.
// Returns {ref, el, box}: put `ref` (a callback ref) on the element. A callback ref, not a ref object:
// the element usually mounts AFTER the first render (the loading skeleton comes first), and the observer
// must attach whenever the element appears or changes.
import { useCallback, useEffect, useRef, useState } from 'react';

export function useBox() {
  const el = useRef(null);
  const [node, setNode] = useState(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const ref = useCallback(n => {
    el.current = n;
    setNode(n);
  }, []);
  useEffect(() => {
    if (!node) return undefined;
    let raf = 0;
    const read = () => {
      raf = 0;
      const r = node.getBoundingClientRect();
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      setBox(b => (b.w === w && b.h === h ? b : { w, h }));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => {
      if (!raf) raf = requestAnimationFrame(read);
    });
    ro.observe(node);
    return () => {
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [node]);
  return { ref, el, box };
}
