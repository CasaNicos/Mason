// MasonUI.__debug: test hook for the Debugger/Playwright (bundle design §3 + S3 §4).
// Live React roots are always counted here. Timer/rAF counters live in window.__MUI_DBG, set up
// by debug-boot.js — loaded as a classic script before the bundle by harness.html (so it also
// sees rAF captured by vendor chunks at load time), or by the import below as a fallback.
import './debug-boot.js';

const live = new Set(); // {kind, name}
const dbg = () => (typeof window !== 'undefined' && window.__MUI_DBG) || null;

export function trackRoot(info) {
  live.add(info);
  return () => live.delete(info);
}

/** Start counting from now (if the boot script didn't already). */
export function install() {
  const d = dbg();
  return d ? d.install() : false;
}

export function wantsDebug() {
  const d = dbg();
  return d ? d.wants() : false;
}

export function snapshot() {
  const d = dbg();
  const byKind = {};
  for (const r of live) byKind[r.kind] = (byKind[r.kind] || 0) + 1;
  return {
    instrumented: !!(d && d.installedAt),
    roots: live.size,
    rootsByKind: byKind,
    rootNames: [...live].map(r => `${r.kind}:${r.name}`).sort(),
    intervals: d ? d.intervals.size : 0,
    timeouts: d ? d.timeouts.size : 0,
    rafPending: d ? d.rafs.size : 0,
    rafCalls: d ? d.rafCalls : 0
  };
}
