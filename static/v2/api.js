/* ═══════════════════════════════════════════════════════════════════════════
   Task 6 (UI overhaul 2026-11-16): the API page (#page-api) as a MasonGrid widget page.
   Paired with static/v2/api.css. Uses Task 1's mv.gridPage helper (static/v2/grid-pages.js).

   Widgets (every one movable / resizable / collapsible, arranged in Edit layout):
     constellation  React apiConstellation (ui/src/widgets/apiConstellation): the network constellation
     opus           the existing vanilla Opus Director panel: #mv-opus-ctl is MOVED in (same id, so the
                    template's renderOpus / toggleOpus / runOpusNow keep working untouched)
     summary        React apiSummary: headline, counts, health by kind, background loops
     sources        React apiSources: one card per source (replaces the #mv-health-grid cards)
   The three React widgets share ONE /api/health poll (45 s, the page's old cadence) in the bundle, so the
   template's own refreshHealth() is skipped while they run (MasonAPI.react()).

   Graceful degradation:
     - grid-pages.js missing        → mount() returns false; the old page stays as it was.
     - the UI bundle failed to load → the old vanilla meta line + cards (#mv-health-fallback) are moved
                                      into the Source detail widget and the template's refreshHealth
                                      renders them again (MasonAPI.react() turns false).
   Layout: localStorage mv_gs3_api / mv_gsc3_api (helper). Source-list filter: mv_api_cfg_v1.
   Every string reaches the DOM through the helper (textContent) or React (JSX). No HTML is built here.
   ═══════════════════════════════════════════════════════════════════════════ */
(function(){
'use strict';

var PAGE = 'api';
var CFG_KEY = 'mv_api_cfg_v1';
/* 36-column units, square cells (≈37 px on a 1600 px window). Same numbers as the widgets' meta.pages. */
var LAYOUT = {
  constellation: {x:0,  y:0,  w:24, h:16, minW:9, minH:7},
  opus:          {x:24, y:0,  w:12, h:8,  minW:6, minH:4},
  summary:       {x:24, y:8,  w:12, h:8,  minW:6, minH:5},
  sources:       {x:0,  y:16, w:36, h:11, minW:8, minH:5}
};
var st = {handle:null, react:true, fellBack:false, refreshHealth:null};

function warn(msg, err){ try{ console.warn('[api page] ' + msg, err === undefined ? '' : err); }catch(e){} }
function own(o, k){ return o != null && Object.prototype.hasOwnProperty.call(o, k); }

/* per-widget config (flat strings / numbers / bools only, WM §5.2) */
function cfgAll(){
  try{ var v = JSON.parse(localStorage.getItem(CFG_KEY) || 'null'); return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {}; }
  catch(e){ return {}; }
}
function cfgClean(c){
  var out = {};
  if(!c || typeof c !== 'object') return out;
  Object.keys(c).forEach(function(k){
    var v = c[k];
    if(/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(k) && (typeof v === 'boolean' || (typeof v === 'number' && isFinite(v)) || (typeof v === 'string' && v.length <= 64))) out[k] = v;
  });
  return out;
}
function cfgGet(id){ var a = cfgAll(); return own(a, id) ? cfgClean(a[id]) : {}; }
function cfgSave(id, c){
  var a = cfgAll(); a[id] = cfgClean(c);
  try{ localStorage.setItem(CFG_KEY, JSON.stringify(a)); }catch(e){}
}

/* Narrow windows: MasonGrid lays every grid page out from its 36-column layout (Task 8, generic; this page used to
   carry its own fitNarrow + resize handler for that). The phone (12-column) layout reads in this order. */
var NARROW_ORDER = ['constellation', 'summary', 'opus', 'sources'];

/* The bundle did not come up: show the vanilla health cards inside the Source detail widget. */
function fallback(){
  if(st.fellBack) return;
  st.fellBack = true;
  st.react = false;
  var fb = document.getElementById('mv-health-fallback');
  var body = st.handle && st.handle.body('sources');
  if(fb && body){ body.insertBefore(fb, body.firstChild); fb.hidden = false; }
  warn('the UI bundle did not load: showing the plain source-health cards');
  if(typeof st.refreshHealth === 'function'){ try{ st.refreshHealth(); }catch(e){ warn('refreshHealth failed', e); } }
}
/* Same contract as the helper's reactIn: wait for MasonUI.ready, give up after 20 s or on script error. */
function watchBundle(){
  var M = window.MasonUI;
  if(M && typeof M.mountWidget === 'function') return;
  if(!M || M.failed){ fallback(); return; }
  var t0 = Date.now();
  (function tick(){
    var W = window.MasonUI;
    if(W && typeof W.mountWidget === 'function') return;
    if(!W || W.failed || Date.now() - t0 > 20000){ fallback(); return; }
    setTimeout(tick, 250);
  })();
}

/* Build the page once (idempotent; called from the template's mountApi on every visit).
   opts.refreshHealth: the template's own renderer, used only by the fallback.
   → true while the React widgets own /api/health (the template then skips refreshHealth). */
function mount(opts){
  if(opts && typeof opts.refreshHealth === 'function') st.refreshHealth = opts.refreshHealth;
  if(st.handle) return st.react;
  var mv = window.mv;
  var sec = document.getElementById('page-api');
  var el = document.getElementById('mv-api-grid');
  if(!mv || typeof mv.gridPage !== 'function' || !sec || !el){ st.react = false; return false; }
  var handle = null;
  try{
    handle = mv.gridPage(PAGE, {
      el: el,
      key: PAGE,
      layout: LAYOUT,
      narrowOrder: NARROW_ORDER,
      widgets: [
        {id:'constellation', title:'NETWORK CONSTELLATION', icon:'ph ph-graph', pad:false,
         bodyClass:'mv-api-body mv-api-cons', react:{type:'apiConstellation'}},
        {id:'opus', title:'OPUS DIRECTOR', icon:'ph ph-crown-simple', block:'mv-opus-ctl',
         bodyClass:'mv-api-body mv-api-opus'},
        {id:'summary', title:'LINK STATUS', icon:'ph ph-pulse', pad:'.75rem 1rem',
         bodyClass:'mv-api-body', react:{type:'apiSummary'}},
        {id:'sources', title:'SOURCE DETAIL', icon:'ph ph-plugs-connected', pad:'.75rem 1rem',
         bodyClass:'mv-api-body mv-api-sources',
         react:{type:'apiSources', config:cfgGet('sources'), saveConfig:function(c){ cfgSave('sources', c); }}}
      ]
    });
  }catch(e){ warn('gridPage failed', e); handle = null; }
  if(!handle){ st.react = false; return false; }
  st.handle = handle;
  sec.classList.add('mv-api-grid-on');
  var fb = document.getElementById('mv-health-fallback');
  if(fb) fb.hidden = true;                       // the React widgets render health now
  watchBundle();
  return st.react;
}

window.MasonAPI = {
  mount: mount,
  react: function(){ return !!st.handle && st.react; },
  handle: function(){ return st.handle; },
  layout: function(){ return JSON.parse(JSON.stringify(LAYOUT)); }
};
})();
