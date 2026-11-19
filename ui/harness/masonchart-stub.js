/* Harness-only stand-in for window.MasonChart (the real engine lives inline in
   templates/mason_v2.html and cannot be loaded on its own). Same public surface that
   <MasonChartHost> uses: create / setSymbol / setTf / setType / redraw / getRecord /
   destroyAllIn. Draws deterministic fake candles per symbol (seeded), clearly labelled STUB,
   no network, no animation loop. Every call is recorded in window.__chartCalls so Playwright
   can assert "row click → chart followed". Skipped with ?nochart=1 (placeholder path). */
(function () {
  'use strict';
  if (window.MasonChart) return;
  try {
    if (new URLSearchParams(location.search).get('nochart') === '1') return;
  } catch (e) { /* ignore */ }

  var registry = {};
  var calls = (window.__chartCalls = []);
  function note(m, id, arg) {
    calls.push([m, id, arg == null ? null : arg]);
    if (calls.length > 500) calls.splice(0, calls.length - 500);
  }
  function seedOf(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function rng(seed) {
    return function () {
      seed = (seed + 0x6d2b79f5) | 0;
      var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function bars(sym, tf) {
    var r = rng(seedOf(sym + '|' + tf)), out = [], px = 20 + r() * 300;
    for (var i = 0; i < 60; i++) {
      var o = px, c = o * (1 + (r() - 0.5) * 0.06);
      out.push({ o: o, c: c, h: Math.max(o, c) * (1 + r() * 0.02), l: Math.min(o, c) * (1 - r() * 0.02) });
      px = c;
    }
    return out;
  }
  function css(name, fb) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fb;
  }
  function draw(rec) {
    var cv = rec.canvas, w = cv.clientWidth, hgt = cv.clientHeight;
    if (w < 2 || hgt < 2) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(w * dpr); cv.height = Math.round(hgt * dpr);
    var ctx = cv.getContext('2d'); if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    var b = bars(rec.state.symbol || 'SPY', rec.state.tf), lo = Infinity, hi = -Infinity;
    b.forEach(function (x) { lo = Math.min(lo, x.l); hi = Math.max(hi, x.h); });
    var pad = 6, bw = (w - pad * 2) / b.length, y = function (v) { return pad + (hi - v) / (hi - lo || 1) * (hgt - pad * 2); };
    var up = css('--color-positive', '#5fd1a0'), dn = css('--color-negative', '#e0768a');
    b.forEach(function (x, i) {
      var cx = pad + i * bw + bw / 2, col = x.c >= x.o ? up : dn;
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.beginPath(); ctx.moveTo(cx, y(x.h)); ctx.lineTo(cx, y(x.l)); ctx.stroke();
      if (rec.state.type === 'trend') return;
      var t = y(Math.max(x.o, x.c)), bt = y(Math.min(x.o, x.c));
      ctx.fillRect(cx - bw * 0.35, t, bw * 0.7, Math.max(1, bt - t));
    });
  }
  function create(opts) {
    var id = opts.id;
    var rec = registry[id] = { id: id, state: { symbol: opts.symbol || '', tf: opts.tf || '1d', type: opts.type || 'candle' } };
    note('create', id, rec.state.symbol);
    var node = document.createElement('div');
    node.className = 'm-widget hx-stub-chart'; node.dataset.widgetId = id;
    var head = document.createElement('div'); head.className = 'hx-stub-head';
    var sym = document.createElement('b'); sym.textContent = rec.state.symbol;
    var tag = document.createElement('em'); tag.textContent = 'MASONCHART STUB';
    head.appendChild(sym); head.appendChild(tag);
    var cv = document.createElement('canvas');
    node.appendChild(head); node.appendChild(cv);
    rec.symbolEl = sym; rec.canvas = cv; rec.node = node;
    if (window.ResizeObserver) { rec.ro = new ResizeObserver(function () { draw(rec); }); rec.ro.observe(cv); }
    opts.grid.place(id, node);
    draw(rec);
    return node;
  }
  function setSymbol(id, s) { var r = registry[id]; if (!r) return; note('setSymbol', id, s); r.state.symbol = s; r.symbolEl.textContent = s; draw(r); }
  function setTf(id, tf) { var r = registry[id]; if (!r) return; note('setTf', id, tf); r.state.tf = tf; draw(r); }
  function setType(id, t) { var r = registry[id]; if (!r) return; note('setType', id, t); r.state.type = t; draw(r); }
  function redraw(id) { if (registry[id]) draw(registry[id]); }
  function getRecord(id) { var r = registry[id]; return r ? { id: id, symbol: r.state.symbol, tf: r.state.tf, type: r.state.type, kind: 'symbol', contract: null } : null; }
  function destroyAllIn(grid) {
    Array.prototype.forEach.call(grid.el.querySelectorAll('[data-widget-id]'), function (n) {
      var r = registry[n.dataset.widgetId];
      if (r && r.ro) r.ro.disconnect();
      note('destroy', n.dataset.widgetId);
      delete registry[n.dataset.widgetId]; n.remove();
    });
  }
  window.MasonChart = {
    __stub: true, create: create, setSymbol: setSymbol, setTf: setTf, setType: setType,
    redraw: redraw, redrawAll: function () { Object.keys(registry).forEach(redraw); },
    getRecord: getRecord, destroyAllIn: destroyAllIn,
    __ids: function () { return Object.keys(registry).sort(); }
  };
})();
