/* static/v2/layout-sync.js — UI overhaul R2-C (2026-11-16): the server copy of every grid page's layout, and the
 * one-time cleanup of superseded layout keys.
 *
 * Every MasonGrid page except the dashboard keeps its canonical 36-column layout in this browser's localStorage
 * (mv_gs3_<key> geometry, mv_gsc3_<key> collapsed; MasonGrid in templates/mason_v2.html). This file mirrors it to
 * the server so a layout arranged on one device follows Nico to the others:
 *     GET/PUT /api/ui/layout?page=<key>          (mason_ui_layout.py; the contract is in its docstring)
 * localStorage stays the cache and the fallback: a page is always built from it first, instantly.
 *
 *   attach(grid)  called right after a page's grid is built (mountPositions, rhxBuildPage, mv.gridPage).
 *                 GET the server copy, then: newer than this browser's → applied to the grid; this browser has
 *                 changes the server lacks → PUT; nothing stored yet and this layout differs from the page's
 *                 default → PUT (the first device with an arranged page seeds the server). A browser that never
 *                 synced this page takes the server copy (the layout last saved on any device).
 *   changed       MasonGrid calls grid._sync.changed() whenever it saves the canonical layout or the collapse map
 *                 (Grid._writeCanon / _persistCollapsed). 600 ms debounce, then PUT {v:1, rev, geom, collapsed} —
 *                 only when the layout differs from what the server already has.
 *   409           another device saved first: its copy wins and is applied here (the dashboard doc's rule).
 *   re-check      each visit of the page and when the tab comes back, at most every 30 s: GET, apply if newer.
 *
 * Until Mason restarts with the new route, GET ?page= answers with the dashboard doc and no `page` field: that
 * marks the page "no server copy" for the session and nothing is ever PUT (localStorage only, the behaviour before
 * this file). A 404 (a page key the server does not list) does the same.
 *
 * This browser's sync state: localStorage mv_pgsync_v1 = {key: {rev, sig}} — the server rev the local layout is
 * based on and a hash of the layout at that rev, so changes made offline are sent on the next visit.
 * Vanilla ES5 (+ Math.imul), no dependencies. Nothing from the server reaches the DOM except through MasonGrid's
 * applyLayout (numbers only, ids filtered to the widgets on the page).
 */
(function(){
'use strict';

/* ── 1. one-time cleanup of superseded layout keys ───────────────────────────────────────────────────────────
   Exact keys only, none of them read by any code any more. Live keys (mv_gs3_positions_m, mv_gs3_crowd_v2, the
   dashboard's one-time migration sources mv_gs3_dashboard / mv_gsc3_dashboard, …) are not in the list.
   Runs once per GC_VERSION per browser (marker mv_ls_gc). */
var GC_KEY = 'mv_ls_gc', GC_VERSION = 1;
var STALE_KEYS = [
  // the Positions + Options merge (Task 2): one grid saved under mv_gs3_positions_m / mv_gsc3_positions_m now
  'mv_gs3_positions', 'mv_gsc3_positions', 'mv_gs3_options', 'mv_gsc3_options',
  // the Crowd redesign (Task 4): mv_gs3_crowd_v2 / mv_gsc3_crowd_v2 now
  'mv_gs3_crowd', 'mv_gsc3_crowd'
];
// older grid generations (pages that existed then): the hand-rolled grid (mv_layout_*), Gridstack's first keys
// (mv_gs_* / mv_gsc_*) and the 24-column era (mv_gs2_* / mv_gsc2_*)
['dashboard', 'positions', 'options'].forEach(function(p){
  STALE_KEYS.push('mv_layout_' + p, 'mv_gs_' + p, 'mv_gsc_' + p, 'mv_gs2_' + p, 'mv_gsc2_' + p);
});
function cleanupKeys(){
  var removed = [];
  try{
    if((parseInt(localStorage.getItem(GC_KEY), 10) || 0) >= GC_VERSION) return removed;
    STALE_KEYS.forEach(function(k){ if(localStorage.getItem(k) !== null){ localStorage.removeItem(k); removed.push(k); } });
    localStorage.setItem(GC_KEY, String(GC_VERSION));
  }catch(e){}
  return removed;
}
cleanupKeys();

/* ── 2. server copy of the page layouts ────────────────────────────────────────────────────────────────────── */
var MG = window.MasonGrid;
if(!MG || !MG.Grid){
  try{ console.warn('[layout-sync] MasonGrid missing: page layouts stay in this browser only'); }catch(e){}
  window.MasonLayoutSync = {attach:function(){ return null; }, cleanup:cleanupKeys, staleKeys:STALE_KEYS.slice()};
  return;
}
var BASE = MG.BASE_COLS || 36, COLLAPSED_H = MG.COLLAPSED_H || 2;
var URL = '/api/ui/layout?page=';
var META_KEY = 'mv_pgsync_v1';
var KEY_RE = /^[a-z][a-z0-9_]{0,31}$/, ID_RE = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;
var DEBOUNCE = 600, BACKOFF_MAX = 60000, RECHECK_MS = 30000, MAX_REV = 9007199254740991;
var S = Object.create(null);                 // storage key → sync state

function own(o, k){ return o != null && Object.prototype.hasOwnProperty.call(o, k); }
function warn(m, e){ try{ console.warn('[layout-sync] ' + m, e === undefined ? '' : e); }catch(_){} }
function int(v, lo, hi){ return (typeof v === 'number' && isFinite(v) && Math.round(v) === v && v >= lo && v <= hi) ? v : null; }
function num(v, lo, hi){ return (typeof v === 'number' && isFinite(v)) ? Math.max(lo, Math.min(hi, Math.round(v))) : null; }
function hash(s){                            // FNV-1a 32-bit → 8 hex chars (change detection only)
  var h = 0x811c9dc5;
  for(var i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return ('0000000' + h.toString(16)).slice(-8);
}

function metaAll(){
  try{ var v = JSON.parse(localStorage.getItem(META_KEY) || 'null'); return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {}; }
  catch(e){ return {}; }
}
function metaGet(key){
  var m = metaAll()[key];
  return (m && typeof m === 'object' && int(m.rev, 0, MAX_REV) !== null && typeof m.sig === 'string') ? {rev:m.rev, sig:m.sig} : null;
}
function metaSet(key, rev, sig){
  var a = metaAll(); a[key] = {rev:rev, sig:sig};
  try{ localStorage.setItem(META_KEY, JSON.stringify(a)); }catch(e){}
}

function gridIds(grid){
  var out = Object.create(null);
  Array.prototype.forEach.call(grid.el.querySelectorAll(':scope > .grid-stack-item'), function(it){
    var id = it.getAttribute('gs-id'); if(id && ID_RE.test(id)) out[id] = 1;
  });
  return out;
}
/* {geom, collapsed} → the server's form: ids allowed by the server AND on this page, ints in its ranges, sorted. */
function norm(geom, collapsed, allow, onGrid){
  var g = {}, c = {};
  Object.keys(geom || {}).sort().forEach(function(id){
    if(!ID_RE.test(id) || (allow && !allow[id]) || (onGrid && !onGrid[id])) return;
    var v = geom[id]; if(!v || typeof v !== 'object') return;
    var w = num(v.w, 1, BASE), x = num(v.x, 0, BASE - 1), y = num(v.y, 0, 10000), h = num(v.h, 1, 200);
    if(w === null || x === null || y === null || h === null) return;
    g[id] = {x:Math.min(x, BASE - w), y:y, w:w, h:h};
  });
  Object.keys(collapsed || {}).sort().forEach(function(id){
    if(!own(g, id)) return;
    var v = collapsed[id], ph = num(v && v.prevH, 1, 200);
    c[id] = {prevH:ph !== null ? ph : (g[id].h > COLLAPSED_H ? g[id].h : 6)};
  });
  return {geom:g, collapsed:c};
}
function sigOf(l){
  return hash(Object.keys(l.geom).map(function(id){
    var v = l.geom[id];
    return id + ':' + v.x + ',' + v.y + ',' + v.w + ',' + v.h + (own(l.collapsed, id) ? '/' + l.collapsed[id].prevH : '');
  }).join(';'));
}
/* The page's current canonical layout (36-col numbers at any window width: Task 8 geomOf). */
function local(st){
  var grid = st.grid, ids = gridIds(grid), geom = {};
  Object.keys(ids).forEach(function(id){ var g = grid.geomOf ? grid.geomOf(id) : null; if(g) geom[id] = g; });
  return norm(geom, grid.collapsed, st.allow, ids);
}
function defaults(st){
  var dl = st.grid.defaultLayout || {}, geom = {};
  Object.keys(dl).forEach(function(id){ geom[id] = dl[id]; });
  return norm(geom, {}, st.allow, gridIds(st.grid));
}
/* A server doc → {rev, geom, collapsed} restricted to this page, or null when unusable. */
function serverDoc(st, d){
  if(!d || typeof d !== 'object' || d.v !== 1 || int(d.rev, 1, MAX_REV) === null) return null;
  if(!d.geom || typeof d.geom !== 'object' || Array.isArray(d.geom)) return null;
  var col = (d.collapsed && typeof d.collapsed === 'object' && !Array.isArray(d.collapsed)) ? d.collapsed : {};
  var n = norm(d.geom, col, st.allow, gridIds(st.grid));
  n.rev = d.rev;
  return n;
}
function busy(grid){ return !!grid.el.querySelector('.ui-draggable-dragging, .ui-resizable-resizing'); }

/* Put the server's layout on the page (MasonGrid applyLayout: numbers only; widgets it does not mention keep their
   place and their collapse state). No PUT comes back out of it unless Gridstack resolves the layout differently. */
function adopt(st, sd){
  if(busy(st.grid)){ setTimeout(function(){ adopt(st, sd); }, 500); return; }   // never move widgets under a drag
  var grid = st.grid, col = {};
  Object.keys(sd.collapsed).forEach(function(id){ col[id] = sd.collapsed[id]; });
  Object.keys(grid.collapsed || {}).forEach(function(id){ if(!own(sd.geom, id) && gridIds(grid)[id]) col[id] = grid.collapsed[id]; });
  if(Object.keys(sd.geom).length){
    st.applying = true;
    try{ grid.applyLayout(sd.geom, col); }catch(e){ warn(st.key + ': applying the server layout failed', e); }
    st.applying = false;
  }
  st.rev = sd.rev; st.sig = sigOf(sd); st.reput0 = false;
  metaSet(st.key, st.rev, st.sig);
}

function offline(st){
  st.backoff = Math.min(BACKOFF_MAX, st.backoff ? st.backoff * 2 : 2000);
  clearTimeout(st.retry);
  st.retry = setTimeout(function(){ st.retry = 0; if(st.supported === true) flush(st); else check(st); }, st.backoff);
}

function schedule(st){
  clearTimeout(st.timer);
  st.timer = setTimeout(function(){ st.timer = 0; flush(st); }, DEBOUNCE);
}
function changed(st){
  if(st.applying || st.supported !== true || st.rejected) return;   // before the first GET: it compares anyway
  schedule(st);
}
/* PUT the page's layout when it differs from the server's. */
function flush(st, keepalive){
  if(st.supported !== true || st.rejected) return;
  if(st.inflight){ st.again = true; return; }
  var l = local(st), sig = sigOf(l);
  if(sig === st.sig) return;
  st.inflight = true; st.again = false;
  var body = JSON.stringify({v:1, rev:st.rev, geom:l.geom, collapsed:l.collapsed});
  var o = {method:'PUT', headers:{'Content-Type':'application/json'}, body:body, cache:'no-store'};
  if(keepalive) o.keepalive = true;
  fetch(URL + encodeURIComponent(st.key), o)
    .then(function(r){ return r.json().catch(function(){ return {}; }).then(function(j){ return {status:r.status, j:j || {}}; }); })
    .then(function(res){
      st.inflight = false;
      var j = res.j;
      if(res.status === 200 && j.ok === true && j.page === st.key && int(j.rev, 1, MAX_REV) !== null){
        st.rev = j.rev; st.sig = sig; st.backoff = 0; st.reput0 = false;
        metaSet(st.key, st.rev, st.sig);
        if(st.again) schedule(st);
        return;
      }
      if(res.status === 409 && j.page === st.key){
        var sd = serverDoc(st, j.doc);
        if(sd){ adopt(st, sd); return; }                                  // another device saved first: its copy wins
        if(!st.reput0){ st.reput0 = true; st.rev = 0; flush(st); return; }   // nothing usable stored: ours at rev 0, once
        offline(st); return;
      }
      if(res.status === 400){
        st.rejected = true;
        warn('the server refused the ' + st.key + ' layout (' + String(j.error || '').slice(0, 120) + '): it stays in this browser');
        return;
      }
      if(res.status === 404 || (res.status === 200 && j.page !== st.key)){ st.supported = false; return; }
      offline(st);
    })
    .catch(function(){ st.inflight = false; offline(st); });
}

/* GET the server copy and reconcile. The first time (boot) the browser's mv_pgsync_v1 entry is the reference;
   after that, this tab's own rev/sig (another tab's saves arrive as a newer server rev). */
function check(st){
  if(st.supported === false || st.getting || st.inflight) return;
  st.getting = true; st.lastGet = Date.now();
  fetch(URL + encodeURIComponent(st.key), {cache:'no-store'})
    .then(function(r){ return r.json().catch(function(){ return null; }).then(function(j){ return {status:r.status, j:j}; }); })
    .then(function(res){
      st.getting = false;
      var j = res.j;
      // no `page` echo = a server without per-page layouts (before the restart); 404 = a key it does not list
      if(res.status === 404 || (res.status === 200 && !(j && j.page === st.key))){ st.supported = false; return; }
      if(res.status !== 200 || !j || !Array.isArray(j.ids)){ offline(st); return; }
      st.supported = true; st.backoff = 0;
      st.allow = Object.create(null);
      j.ids.forEach(function(id){ if(typeof id === 'string' && ID_RE.test(id)) st.allow[id] = 1; });
      reconcile(st, j.doc ? serverDoc(st, j.doc) : null, j.doc ? int(j.doc.rev, 1, MAX_REV) : null);
    })
    .catch(function(){ st.getting = false; offline(st); });
}
function reconcile(st, sd, srvRev){
  var ref = st.booted ? {rev:st.rev, sig:st.sig} : st.meta;
  st.booted = true;
  var lsig = sigOf(local(st));
  if(!sd){
    st.rev = srvRev || 0;                    // a stored doc this page cannot use (corrupt / foreign) is replaced
    st.sig = null;
    if(srvRev || lsig !== sigOf(defaults(st))) flush(st);   // nothing stored: only an arranged page seeds the server
    else st.sig = lsig;                       // the default layout needs no server copy; the first change PUTs
    return;
  }
  var ssig = sigOf(sd);
  if(!ref || sd.rev > ref.rev){ if(ssig !== lsig) adopt(st, sd); else { st.rev = sd.rev; st.sig = ssig; metaSet(st.key, st.rev, st.sig); } return; }
  st.rev = sd.rev; st.sig = ssig;           // same rev (or the server is older, e.g. a restored DB): ours stands
  if(lsig !== ssig) flush(st);
  else metaSet(st.key, st.rev, st.sig);
}

function attach(grid){
  if(!grid || grid.store || typeof grid.pageKey !== 'string' || !KEY_RE.test(grid.pageKey) ||
     typeof grid.applyLayout !== 'function' || typeof window.fetch !== 'function') return null;
  var key = grid.pageKey;
  if(S[key]) return S[key];
  var st = S[key] = {key:key, grid:grid, supported:null, allow:null, rev:0, sig:null, meta:metaGet(key), booted:false,
                     timer:0, retry:0, backoff:0, inflight:false, again:false, getting:false, lastGet:0,
                     applying:false, reput0:false, rejected:false};
  grid._sync = {changed:function(){ changed(st); }};
  check(st);
  return st;
}
/* Re-check the grids of page `pageId` (a visit, or the tab coming back): at most every RECHECK_MS per grid. */
function recheck(pageId){
  var sec = typeof pageId === 'string' ? document.getElementById('page-' + pageId) : null;
  if(!sec) return;
  Object.keys(S).forEach(function(k){
    var st = S[k];
    if(st.supported !== true || !sec.contains(st.grid.el)) return;
    if(Date.now() - st.lastGet < RECHECK_MS || st.timer || st.inflight || st.getting) return;
    check(st);
  });
}

var mv = window.mv;
if(mv){
  var prevOnPage = mv._onPage;
  mv._onPage = function(id){
    if(typeof prevOnPage === 'function') prevOnPage(id);
    try{ recheck(id); }catch(e){ warn('recheck failed', e); }
  };
}
document.addEventListener('visibilitychange', function(){
  if(document.visibilityState === 'visible' && window.mv) recheck(window.mv.page);
});
window.addEventListener('online', function(){
  Object.keys(S).forEach(function(k){
    var st = S[k]; st.backoff = 0;
    if(st.retry){ clearTimeout(st.retry); st.retry = 0; if(st.supported === true) flush(st); else check(st); }
  });
});
// closing the tab inside the debounce: send it now (keepalive); anything missed is sent on the next visit
window.addEventListener('pagehide', function(){
  Object.keys(S).forEach(function(k){ var st = S[k]; if(st.timer){ clearTimeout(st.timer); st.timer = 0; flush(st, true); } });
});

window.MasonLayoutSync = {
  attach:attach,
  recheck:recheck,
  cleanup:cleanupKeys,
  staleKeys:STALE_KEYS.slice(),
  /* read-only state for checks / debugging */
  state:function(key){
    var st = own(S, key) ? S[key] : null;
    return st ? {key:st.key, supported:st.supported, rev:st.rev, sig:st.sig, pending:!!(st.timer || st.inflight),
                 rejected:st.rejected, allow:st.allow ? Object.keys(st.allow) : null} : null;
  }
};
})();
