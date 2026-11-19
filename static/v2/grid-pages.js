/* static/v2/grid-pages.js — UI overhaul Task 1 (2026-11-16): MasonGrid page helper + per-page layout reset.
 *
 * Loaded by templates/mason_v2.html right after the Phase 5 data script (it needs window.MasonGrid, window.mv
 * and mv._gridKit, the shell builder that script exports). Vanilla ES5, no dependencies, no network.
 *
 *  1. MasonGrid.of(el)            → the MasonGrid.Grid that owns a .grid-stack element, or null.
 *  2. Grid.prototype.items / layoutSnapshot / applyLayout / resetLayout / restoreLayout
 *       Layout reset + undo for the grids that keep their layout in localStorage (every page but the dashboard,
 *       whose layout lives in DashState / PUT /api/ui/layout). Edit layout → Reset uses these. R2-C: those
 *       localStorage layouts are mirrored to the server per page by static/v2/layout-sync.js (applyLayout saves →
 *       Grid._persistCollapsed → grid._sync.changed()); layout-sync also applies a newer server copy through it.
 *  3. mv.gridPage(page, opts)     → make a page an editable MasonGrid page (doc block above gridPage below).
 *
 * Every title / id reaches the DOM through textContent / setAttribute only.
 */
(function(){
'use strict';
var MG = window.MasonGrid, mv = window.mv;
if(!MG || !MG.Grid || !mv){
  try{ console.warn('[gridPage] MasonGrid / mv missing: static/v2/grid-pages.js not initialised'); }catch(e){}
  return;
}
var BASE = MG.BASE_COLS || 36;
var COLLAPSED_H = MG.COLLAPSED_H || 2;
var MIN_ROWS = MG.MIN_ROW_SPAN || 2;
var ID_RE = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;
var PAGE_RE = /^[a-z][a-z0-9_-]{0,31}$/;
var P = MG.Grid.prototype;

function own(o, k){ return o != null && Object.prototype.hasOwnProperty.call(o, k); }
function num(v, lo, hi, d){ return (typeof v === 'number' && isFinite(v)) ? Math.max(lo, Math.min(hi, Math.round(v))) : d; }
function warn(msg, err){ try{ console.warn('[gridPage] ' + msg, err === undefined ? '' : err); }catch(e){} }
function syncIcon(node, on){
  var i = node && node.querySelector('.m-widget-head .m-widget-collapse i');
  if(i) i.className = on ? 'ph ph-caret-down' : 'ph ph-caret-up';
}

/* ── 1. Grid lookup ─────────────────────────────────────────────────────────────────────────────────────── */
MG.of = function(el){ return (el && el._masonGrid) || null; };

/* ── 2. Layout snapshot / apply / reset / restore (non-store grids) ─────────────────────────────────────── */
P.items = function(){ return Array.prototype.slice.call(this.el.querySelectorAll(':scope > .grid-stack-item')); };

function liveGeom(it){
  var n = it.gridstackNode;
  if(n) return {x:n.x || 0, y:n.y || 0, w:n.w || 1, h:n.h || 1};
  function a(k, d){ var v = parseInt(it.getAttribute('gs-' + k), 10); return isFinite(v) ? v : d; }
  return {x:a('x', 0), y:a('y', 0), w:a('w', 1), h:a('h', 1)};
}

/* {geom:{id:{x,y,w,h}}, collapsed:{id:{prevH}}} in canonical 36-column units. Below 36 columns the live
   geometry is a transient re-flow, so the canonical layout (Task 8: grid.geomOf → grid.canon) is used instead. */
P.layoutSnapshot = function(){
  var self = this, geom = Object.create(null), live = this.gs.getColumn() === BASE, saved = this.saved || {};
  this.items().forEach(function(it){
    var id = it.getAttribute('gs-id'); if(!id) return;
    var g = typeof self.geomOf === 'function' ? self.geomOf(id) : (live ? liveGeom(it) : (own(saved, id) ? saved[id] : null));
    if(g) geom[id] = {x:g.x, y:g.y, w:g.w, h:g.h};
  });
  return {geom:geom, collapsed:JSON.parse(JSON.stringify(this.collapsed || {}))};
};

/* Put every item at geom[id] (36-col units) and collapse exactly the ids in `collapsed` ({id:{prevH}}), then
   save both right away. Items not in geom keep their place. Uses Gridstack's own load() (updates existing items
   by id, never adds or removes), so placements never push each other around. */
P.applyLayout = function(geom, collapsed){
  if(this.store || !this.gs) return false;          // dashboard: its layout belongs to DashState
  geom = geom || {}; collapsed = collapsed || {};
  var self = this, gs = this.gs, items = this.items();
  items.forEach(function(it){                         // 1. expand what is collapsed now
    var id = it.getAttribute('gs-id'), node = it.querySelector('.m-widget');
    if(id && node && self.isCollapsed(id)){ self._setCollapsed(id, node, it, false, false); syncIcon(node, false); }
  });
  var list = [];                                      // 2. the geometry, full heights
  items.forEach(function(it){
    var id = it.getAttribute('gs-id'); if(!id || !own(geom, id) || !geom[id]) return;
    var g = geom[id], ph = own(collapsed, id) && collapsed[id] && collapsed[id].prevH;
    var w = num(g.w, 1, BASE, 12);
    list.push({id:id, x:Math.min(BASE - w, num(g.x, 0, BASE - 1, 0)), y:num(g.y, 0, 10000, 0), w:w, h:num(ph || g.h, 1, 200, 6)});
  });
  // Task 8: this geometry becomes the grid's canonical layout. At 36 columns it is shown as is; on a narrower
  // window the grid shows the layout fitted from it (MasonGrid _fitNow) and returns to it when the window widens.
  if(self.canon) list.forEach(function(n){ self.canon[n.id] = {x:n.x, y:n.y, w:n.w, h:n.h}; });
  if(gs.getColumn() === BASE || typeof self._fitNow !== 'function'){
    if(list.length){ try{ gs.load(list, false); }catch(e){ warn('load failed', e); } }
  } else self._fitNow(true);
  var folded = Object.create(null);                   // 3. collapse what the target has collapsed
  items.forEach(function(it){
    var id = it.getAttribute('gs-id'), node = it.querySelector('.m-widget');
    if(!id || !node || !own(collapsed, id)) return;
    var ph = (collapsed[id] && collapsed[id].prevH) || (own(geom, id) && geom[id] && geom[id].h) || 6;
    self.collapsed[id] = {prevH:ph};
    self._setCollapsed(id, node, it, true, false); syncIcon(node, true);
    folded[id] = 1;
  });
  var map = Object.create(null), prev = this.saved || {};   // 4. save now (the Grid's own save waits for 36 cols)
  list.forEach(function(n){
    map[n.id] = {x:n.x, y:n.y, w:n.w, h:folded[n.id] ? COLLAPSED_H : n.h};
    if(self.canon) self.canon[n.id] = {x:n.x, y:n.y, w:n.w, h:map[n.id].h};   // same numbers a 36-col save keeps
  });
  Object.keys(prev).forEach(function(id){ if(!own(map, id)) map[id] = prev[id]; });
  this.saved = map;
  try{ localStorage.setItem(this._lsKey(), JSON.stringify(map)); }catch(e){}
  this._persistCollapsed();
  if(window.MasonChart && typeof window.MasonChart.redrawAll === 'function'){
    setTimeout(function(){ try{ window.MasonChart.redrawAll(); }catch(e){} }, 360);   // after Gridstack's .3 s slide
  }
  return true;
};

/* Reset to this.defaultLayout (everything expanded). Returns the snapshot to undo with, or null (dashboard). */
P.resetLayout = function(){
  if(this.store || !this.gs) return null;
  var snap = this.layoutSnapshot(), dl = this.defaultLayout || {}, geom = Object.create(null);
  this.items().forEach(function(it){ var id = it.getAttribute('gs-id'); if(id && own(dl, id)) geom[id] = dl[id]; });
  this.applyLayout(geom, {});
  return snap;
};
P.restoreLayout = function(snap){
  if(!snap || !snap.geom) return false;
  return this.applyLayout(snap.geom, snap.collapsed || {});
};

/* ── 3. mv.gridPage ─────────────────────────────────────────────────────────────────────────────────────────
   mv.gridPage(page, opts) → handle. Turns page `page` (its <section id="page-<page>">) into an editable
   MasonGrid page: every block becomes a movable, resizable, collapsible Gridstack widget, the layout persists per
   page and survives a refresh, and Edit layout (palette, long-press on a header) works on it automatically.

   page     the router id (lowercase): 'news', 'api', 'activities', 'notifications', ...
   opts.key optional storage key (default: page) → localStorage mv_gs3_<key> / mv_gsc3_<key>. Bump it (e.g.
            'news2') if the default coordinates change incompatibly later, so old saves are not applied.
            R2-C: the server copy uses the same key (/api/ui/layout?page=<key>); a new key or widget id must be added
            to mason_ui_layout.py UI_PAGE_WIDGETS (needs a restart), until then that part stays in localStorage.
   opts.el  the grid container: an element or its id, INSIDE #page-<page> (edit mode finds grids by section).
            It becomes the .grid-stack; leave it empty.
   opts.layout   {id: {x, y, w, h, minW?, minH?}}: the default layout in 36-column units (MasonGrid.BASE_COLS).
            Rows are square cells (row height = column width, about 37 px on a 1600 px window). When the GRID is
            960 px wide or less (R3-B; was a 1040 px window) it re-flows to 24 and then (584 px grid) 12 columns
            by itself: Task 8's MasonGrid fit, derived
            from the 36-column layout, which is the only one saved. No per-page code needed.
   opts.narrowOrder  optional [id, …]: reading order of the one-column (12-col) phone layout; ids not listed
            follow top to bottom, left to right.
   opts.narrowWrap   optional [{ids:[…], perRow?:2}, …] (R2-C): at 24 columns, when those widgets still form a strip
            (side by side, edge to edge, same top and height) it folds into rows of perRow, each as tall as the strip
            (the Positions page folds its 4 charts into 2×2 this way). 36 columns always show the canonical layout.
   opts.widgets  [{id, title, icon?, center?, block?, react?, mount?, chart?, pad?, bodyClass?}]
            id     /^[A-Za-z][A-Za-z0-9_-]{0,39}$/, unique on the page: the layout key and Gridstack id.
            title  header text; icon: a Phosphor class ('ph ph-newspaper'). Both via textContent / className.
            block  an existing element (or its id) MOVED into the widget body. It keeps its id, its listeners and
                   whatever already renders into it, so existing vanilla renderers keep working untouched.
            react  {type, config?, saveConfig?(cfg)}: a MasonUI React widget (ui/src/widgets/<type>), one root.
            mount  function(body, ctx) → {refresh?(), destroy?()} | void, for anything else.
                   ctx = {page, id, grid, node, handle}.
            chart  MasonChart.create options minus id/grid (MasonChart builds its own header). Chart ids share one
                   app-wide registry: prefix them with the page (e.g. 'news-spy').
            pad    false = no body padding, or a CSS padding string. bodyClass: extra classes for the body.
            Order of use inside one widget: block, then react, then mount.
   opts.onResize  function(id, node) after the user resizes a widget (MasonChart widgets redraw themselves).
   handle = {page, grid, el, ids(), node(id), body(id), refresh(), reset()}. A second call for the same page
            returns the first handle, so it can be called from the page's mount function on every visit.
   Body: .m-widget-body scrolls (overflow:auto) and the page's scroller is .m-content, so adopted blocks should
   drop fixed heights and let the widget size them (height:100% / flex) — content reflows on every resize.
   ─────────────────────────────────────────────────────────────────────────────────────────────────────────── */
var pages = Object.create(null);

function kit(){ return (mv && mv._gridKit) || null; }

/* Same shell as the dashboard's content widgets (Phase 5 mountContentWidget); local copy if the kit is missing. */
function shell(grid, id, w){
  var K = kit();
  if(K && typeof K.mountContentWidget === 'function') return K.mountContentWidget(grid, id, {title:w.title, icon:w.icon, center:!!w.center});
  var node = document.createElement('div');
  node.className = 'm-widget'; node.dataset.widgetId = id;
  var head = document.createElement('div'); head.className = 'm-widget-head';
  var icon = document.createElement('i'); icon.className = w.icon || 'ph ph-squares-four';
  icon.style.cssText = 'font-size:.875rem;color:var(--color-accent-400)';
  var title = document.createElement('span'); title.className = 'm-widget-title'; title.textContent = w.title || id;
  var spacer = document.createElement('div'); spacer.style.flex = '1';
  head.appendChild(icon); head.appendChild(title); head.appendChild(spacer);
  head.appendChild(MG.buildCollapseButton(grid, id));
  var body = document.createElement('div');
  body.className = 'm-widget-body m-widget-collapsible-body' + (w.center ? ' center' : '');
  node.appendChild(head); node.appendChild(body);
  grid.place(id, node);
  return body;
}

/* Min height from the layout (Gridstack enforces it), dropped while collapsed (the RH pages' rule). */
function minH(grid, id, item, mh){
  if(!item || !(mh > MIN_ROWS)) return;
  if(!grid._gpMinH){
    grid._gpMinH = Object.create(null);
    var orig = grid._setCollapsed;
    grid._setCollapsed = function(cid, node, it, on, persist){
      var m = this._gpMinH[cid];
      if(m && on){ try{ this.gs.update(it, {minH:MIN_ROWS}); }catch(e){} }
      orig.call(this, cid, node, it, on, persist);
      if(m && !on){ try{ this.gs.update(it, {minH:m}); }catch(e){} }
    };
  }
  grid._gpMinH[id] = mh;
  if(!grid.isCollapsed(id)){ try{ grid.gs.update(item, {minH:mh}); }catch(e){} }
}

/* One MasonUI React root in `host`, once the bundle is up (same contract as the dashboard's dashMountReact). */
function reactIn(host, type, inst){
  var h = null, dead = false, note = document.createElement('div');
  note.className = 'f-empty'; note.textContent = 'Loading…';
  host.appendChild(note);
  function fail(text){ if(!dead && !h) note.textContent = text || 'Widget unavailable: the UI bundle did not load'; }
  function go(){
    var M = window.MasonUI;
    if(dead || h) return;
    if(!M || typeof M.mountWidget !== 'function'){ fail(); return; }
    if(note.parentNode) note.parentNode.removeChild(note);
    var o = {id:inst.id, config:(inst.config && typeof inst.config === 'object') ? inst.config : {}};
    if(typeof inst.saveConfig === 'function') o.saveConfig = inst.saveConfig;
    try{ h = M.mountWidget(type, host, o); }
    catch(e){ host.appendChild(note); fail('Widget failed to load'); warn(type + ' mount failed', e); }
  }
  var W = window.MasonUI;
  if(W && typeof W.mountWidget === 'function') go();
  else if(W && !W.failed && W.ready && typeof W.ready.then === 'function') W.ready.then(go);
  else fail();
  var t = setTimeout(function(){ fail(); }, 20000);
  return {destroy:function(){ dead = true; clearTimeout(t); if(h){ try{ h.destroy(); }catch(e){} h = null; } },
          refresh:function(){ if(h && typeof h.refresh === 'function') h.refresh(); }};
}

function cleanLayout(layout){
  var out = Object.create(null);
  Object.keys(layout || {}).forEach(function(id){
    var g = layout[id];
    if(!ID_RE.test(id) || !g || typeof g !== 'object') return;
    var w = num(g.w, 1, BASE, 12);
    var o = {x:Math.min(BASE - w, num(g.x, 0, BASE - 1, 0)), y:num(g.y, 0, 10000, 0), w:w, h:num(g.h, 1, 200, 9)};
    if(g.minW != null) o.minW = num(g.minW, 1, BASE, 1);
    if(g.minH != null) o.minH = num(g.minH, 1, 200, MIN_ROWS);
    out[id] = o;
  });
  return out;
}

function gridPage(page, opts){
  if(typeof page !== 'string' || !PAGE_RE.test(page)){ warn('bad page id: ' + String(page)); return null; }
  if(pages[page]) return pages[page];
  opts = opts || {};
  var el = typeof opts.el === 'string' ? document.getElementById(opts.el) : opts.el;
  if(!el || el.nodeType !== 1){ warn(page + ': opts.el not found'); return null; }
  var sec = document.getElementById('page-' + page);
  if(!sec || !sec.contains(el)) warn(page + ': the grid is not inside #page-' + page + ' (Edit layout looks there)');
  var layout = cleanLayout(opts.layout);
  // storage key: the page id unless opts.key says otherwise (bump it, e.g. 'news2', when the defaults change
  // incompatibly, so old saves in mv_gs3_<key> are not applied to the new layout)
  var key = (typeof opts.key === 'string' && PAGE_RE.test(opts.key)) ? opts.key : page;
  var grid = new MG.Grid(key, el, {});
  grid.defaultLayout = layout;                        // already 36-col units: no 12-col migration
  if(Array.isArray(opts.narrowOrder)){                // Task 8: one-column reading order (MasonGrid fitLayout)
    grid.narrowOrder = opts.narrowOrder.filter(function(id){ return typeof id === 'string' && ID_RE.test(id); });
  }
  if(Array.isArray(opts.narrowWrap)){                 // R2-C: strips that fold into rows at 24 columns (MasonGrid fitRows)
    grid.narrowWrap = opts.narrowWrap.map(function(g){
      var ids = Array.isArray(g) ? g : (g && Array.isArray(g.ids) ? g.ids : []);
      ids = ids.filter(function(id){ return typeof id === 'string' && ID_RE.test(id); });
      return {ids:ids, perRow:(g && !Array.isArray(g) && g.perRow >= 1) ? Math.min(6, Math.floor(g.perRow)) : 2};
    }).filter(function(g){ return g.ids.length > 1; });
  }
  var K = kit();
  if(K && K.grids) K.grids['gp_' + page] = grid;      // joins the shared resize sync (mv._onResize)
  var seen = Object.create(null), list = [];
  (Array.isArray(opts.widgets) ? opts.widgets : []).forEach(function(w){
    if(!w || typeof w !== 'object' || !ID_RE.test(String(w.id)) || seen[w.id]){ warn(page + ': widget skipped (bad or duplicate id)', w && w.id); return; }
    seen[w.id] = 1; list.push(w);
  });
  // DOM (= Tab) order follows the on-screen order of the saved-or-default slots
  function at(w){ return grid.saved[w.id] || layout[w.id] || {x:0, y:0}; }
  list.sort(function(a, b){ var ga = at(a), gb = at(b); return ((ga.y || 0) - (gb.y || 0)) || ((ga.x || 0) - (gb.x || 0)); });

  var recs = Object.create(null);
  var handle = {
    page:page, grid:grid, el:el,
    ids:function(){ return list.map(function(w){ return w.id; }); },
    node:function(id){ return own(recs, id) ? recs[id].node : null; },
    body:function(id){ return own(recs, id) ? recs[id].body : null; },
    refresh:function(){
      Object.keys(recs).forEach(function(id){
        var hs = recs[id].hs;
        hs.forEach(function(x){ if(x && typeof x.refresh === 'function'){ try{ x.refresh(); }catch(e){ warn(page + '/' + id + ' refresh failed', e); } } });
      });
    },
    reset:function(){ return grid.resetLayout(); }
  };
  list.forEach(function(w){
    var rec = recs[w.id] = {node:null, body:null, hs:[]};
    try{
      if(w.chart && typeof w.chart === 'object'){
        var copt = {}, k;
        for(k in w.chart){ if(own(w.chart, k)) copt[k] = w.chart[k]; }
        copt.id = w.id; copt.grid = grid;
        rec.node = window.MasonChart.create(copt);
      } else {
        var body = shell(grid, w.id, w);
        rec.body = body; rec.node = body.parentNode;
        if(w.bodyClass) String(w.bodyClass).split(/\s+/).forEach(function(c){ if(c) body.classList.add(c); });
        if(w.pad === false) body.style.padding = '0';
        else if(typeof w.pad === 'string') body.style.padding = w.pad;
        var blk = typeof w.block === 'string' ? document.getElementById(w.block) : w.block;
        if(w.block && !(blk && blk.nodeType === 1)) warn(page + '/' + w.id + ': block not found');
        if(blk && blk.nodeType === 1) body.appendChild(blk);
        if(w.react && typeof w.react === 'object' && typeof w.react.type === 'string'){
          var host = body;
          if(body.firstChild){ host = document.createElement('div'); host.className = 'mv-gp-react'; host.style.cssText = 'flex:1 1 auto;min-height:0;height:100%'; body.appendChild(host); }
          rec.hs.push(reactIn(host, w.react.type, {id:page + '-' + w.id, config:w.react.config, saveConfig:w.react.saveConfig}));
        }
        if(typeof w.mount === 'function'){
          var r = w.mount(body, {page:page, id:w.id, grid:grid, node:rec.node, handle:handle});
          if(r && typeof r === 'object') rec.hs.push(r);
        }
      }
    }catch(err){ warn('mount failed: ' + page + '/' + w.id, err); }
    var item = rec.node && rec.node.closest ? rec.node.closest('.grid-stack-item') : null;
    if(layout[w.id] && layout[w.id].minH) minH(grid, w.id, item, layout[w.id].minH);
  });
  grid._persist();
  // R2-C: the layout also lives on the server (GET/PUT /api/ui/layout?page=<key>, static/v2/layout-sync.js), so it
  // follows Nico across devices; localStorage stays the cache and the fallback (and the only copy before a restart).
  if(window.MasonLayoutSync && typeof window.MasonLayoutSync.attach === 'function'){
    try{ window.MasonLayoutSync.attach(grid); }catch(e){ warn(page + ': layout sync failed to start', e); }
  }
  if(typeof opts.onResize === 'function'){
    grid.gs.on('resizestop', function(e, it){
      var id = it && it.getAttribute ? it.getAttribute('gs-id') : null;
      if(id && own(recs, id)){ try{ opts.onResize(id, recs[id].node); }catch(err){ warn('onResize failed', err); } }
    });
  }
  setTimeout(function(){
    grid.sync();
    var K2 = kit();
    if(K2 && typeof K2.armGridLoadIn === 'function'){ try{ K2.armGridLoadIn(el); }catch(e){} }
  }, 0);
  pages[page] = handle;
  return handle;
}

mv.gridPage = gridPage;
mv.gridPage.get = function(page){ return own(pages, page) ? pages[page] : null; };
})();
