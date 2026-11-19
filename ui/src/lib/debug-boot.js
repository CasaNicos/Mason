/* Timer / rAF counters for MasonUI.__debug (Debugger + Playwright), DEBUG_F0 F1.
 *
 * This ONE file is used two ways:
 *  1. As a CLASSIC script, emitted verbatim to static/ui/debug-boot.js and loaded by harness.html
 *     (and optionally by the template) BEFORE the module bundle. Module chunks (vendor-motion,
 *     gsap…) capture requestAnimationFrame when they evaluate, and in the built bundle every
 *     static chunk evaluates before mason-ui.js's own code, so only a classic script that runs
 *     first can wrap the timers in time.
 *  2. Imported (side effect) by lib/debug.js, for vitest and pages without the classic script.
 * Either way it defines window.__MUI_DBG once (second load is a no-op) and installs the wrappers
 * when asked: ?rhx_debug=1 / ?rhx_mock=1 in the URL, or window.MASONUI_DEBUG = true beforehand.
 * Wrappers are transparent: same arguments, same return values. No imports/exports on purpose.
 */
(function () {
  var w = typeof window !== 'undefined' ? window : null;
  if (!w || w.__MUI_DBG) return;
  var D = (w.__MUI_DBG = {
    installedAt: null,
    rafCalls: 0,
    intervals: new Set(),
    timeouts: new Set(),
    rafs: new Set(),
    install: install,
    wants: wants
  });
  var inNativeRaf = false; // jsdom implements rAF with setInterval: don't count that as ours

  function wants() {
    try {
      if (w.MASONUI_DEBUG === true) return true;
      var q = new URLSearchParams(w.location.search);
      return q.get('rhx_debug') === '1' || q.get('rhx_mock') === '1';
    } catch (e) {
      return false;
    }
  }

  function install() {
    if (D.installedAt) return false;
    D.installedAt = Date.now();
    var sI = w.setInterval.bind(w), cI = w.clearInterval.bind(w);
    var sT = w.setTimeout.bind(w), cT = w.clearTimeout.bind(w);
    var rAF = w.requestAnimationFrame ? w.requestAnimationFrame.bind(w) : null;
    var cAF = w.cancelAnimationFrame ? w.cancelAnimationFrame.bind(w) : null;
    w.setInterval = function (fn, ms) {
      var id = sI.apply(null, arguments);
      if (!inNativeRaf) D.intervals.add(id);
      return id;
    };
    w.clearInterval = function (id) {
      D.intervals.delete(id);
      return cI(id);
    };
    w.setTimeout = function (fn, ms) {
      var id;
      var args = Array.prototype.slice.call(arguments);
      if (typeof fn === 'function') {
        args[0] = function () {
          D.timeouts.delete(id);
          return fn.apply(this, arguments);
        };
      }
      id = sT.apply(null, args);
      D.timeouts.add(id);
      return id;
    };
    w.clearTimeout = function (id) {
      D.timeouts.delete(id);
      return cT(id);
    };
    if (rAF) {
      w.requestAnimationFrame = function (fn) {
        var id;
        inNativeRaf = true;
        try {
          id = rAF(function (t) {
            D.rafs.delete(id);
            D.rafCalls += 1;
            fn(t);
          });
        } finally {
          inNativeRaf = false;
        }
        D.rafs.add(id);
        return id;
      };
      w.cancelAnimationFrame = function (id) {
        D.rafs.delete(id);
        return cAF(id);
      };
    }
    if (typeof globalThis !== 'undefined' && globalThis !== w) {
      ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'].forEach(function (k) {
        if (w[k]) globalThis[k] = w[k];
      });
    }
    return true;
  }

  if (wants()) install();
})();
