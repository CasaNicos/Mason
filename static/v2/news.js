/* ═══════════════════════════════════════════════════════════════════════════
   Task 5 (UI overhaul 2026-11-16): the News page (#page-news) as a MasonGrid widget page.
   Paired with static/v2/news.css (the page's HUD / holo-terminal skin). Uses Task 1's mv.gridPage helper
   (static/v2/grid-pages.js). Called from the template's mountNews() on every visit (idempotent).

   Widgets (each movable / resizable from 4 sides + 4 corners / collapsible; Edit layout + Reset work):
     hud       React newsHud      — wire tape (React Bits LogoLoop), counts, bull/bear gauge, filter chips
     timeline  React newsTimeline — sentiment river, drag to scrub a time window
     bubbles   React newsBubbles  — impact map, one bubble per ticker / sector, click to filter
     focus     React newsFocus    — top story, or the clicked story: summary, tickers + mini charts, related
     book      React newsBook     — "For your book" (holdings + watchlist)
     tape      React newsTape     — "The tape" (macro + market)
   The six React roots share ONE store in the bundle (ui/src/widgets/newsShared/store.js): one poll of
   /api/news/flow (falls back to /api/news until Mason restarts with that route), ~2-min cadence, paused while
   the page is hidden.

   Graceful degradation:
     - grid-pages.js missing        → mount() returns false; the old page (#mv-nw-legacy) shows as before.
     - the UI bundle failed to load → the grid hides and the old page (filters / lead / book / tape, still
                                      rendered by the template's renderNewsPage) shows instead.
   Layout: localStorage mv_gs3_news / mv_gsc3_news (helper). Widget config (Impact map Tickers/Sectors):
   mv_news_cfg_v1. Every string reaches the DOM through the helper (textContent) or React (JSX).
   ═══════════════════════════════════════════════════════════════════════════ */
(function(){
'use strict';

var PAGE = 'news';
var CFG_KEY = 'mv_news_cfg_v1';
/* 36-column units, square cells (≈37 px on a 1600 px window). Same numbers as each widget's meta.js pages. */
var LAYOUT = {
  hud:      {x:0,  y:0,  w:36, h:6,  minW:10, minH:3},
  timeline: {x:0,  y:6,  w:24, h:7,  minW:8,  minH:4},
  bubbles:  {x:24, y:6,  w:12, h:7,  minW:6,  minH:4},
  focus:    {x:0,  y:13, w:14, h:14, minW:7,  minH:5},
  book:     {x:14, y:13, w:11, h:14, minW:6,  minH:5},
  tape:     {x:25, y:13, w:11, h:14, minW:6,  minH:5}
};
/* one column (12-col grid, phones): reading order */
var NARROW_ORDER = ['hud', 'focus', 'book', 'tape', 'timeline', 'bubbles'];
var st = {handle:null, react:true, fellBack:false};

function warn(msg, err){ try{ console.warn('[news page] ' + msg, err === undefined ? '' : err); }catch(e){} }
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
function react(id, type){
  return {type:type, config:cfgGet(id), saveConfig:function(c){ cfgSave(id, c); }};
}

/* Narrow windows: MasonGrid lays every grid page out from its 36-column layout (Task 8, generic; this page used to
   carry its own fitNarrow + resize handler for that). NARROW_ORDER above is the phone layout's reading order. */

/* The bundle did not come up: hide the grid, show the old page (the template's renderNewsPage fills it). */
function fallback(){
  if(st.fellBack) return;
  st.fellBack = true;
  st.react = false;
  var sec = document.getElementById('page-news');
  var legacy = document.getElementById('mv-nw-legacy');
  var el = document.getElementById('mv-nw-grid');
  if(legacy) legacy.hidden = false;
  if(el) el.hidden = true;
  if(sec) sec.classList.remove('mv-nw-grid-on');
  warn('the UI bundle did not load: showing the plain news page');
  if(typeof window.renderNewsPage === 'function'){ try{ window.renderNewsPage(); }catch(e){ warn('renderNewsPage failed', e); } }
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

/* Build the page once (idempotent; the template's mountNews calls it on every visit).
   → true while the React widgets own the page (false = the old page is showing). */
function mount(){
  if(st.handle) return st.react;
  var mv = window.mv;
  var sec = document.getElementById('page-news');
  var el = document.getElementById('mv-nw-grid');
  if(!mv || typeof mv.gridPage !== 'function' || !sec || !el){ st.react = false; return false; }
  var handle = null;
  try{
    handle = mv.gridPage(PAGE, {
      el: el,
      key: PAGE,
      layout: LAYOUT,
      narrowOrder: NARROW_ORDER,
      widgets: [
        {id:'hud',      title:'WIRE HUD',        icon:'ph ph-broadcast',     pad:'.5rem .75rem .4375rem', bodyClass:'mv-nw-body mv-nw-hud',      react:react('hud', 'newsHud')},
        {id:'timeline', title:'SENTIMENT RIVER', icon:'ph ph-wave-sine',     pad:'.5rem .75rem',          bodyClass:'mv-nw-body mv-nw-timeline', react:react('timeline', 'newsTimeline')},
        {id:'bubbles',  title:'IMPACT MAP',      icon:'ph ph-circles-three', pad:'.5rem .75rem',          bodyClass:'mv-nw-body mv-nw-bubbles',  react:react('bubbles', 'newsBubbles')},
        {id:'focus',    title:'STORY FOCUS',     icon:'ph ph-crosshair',     pad:'.625rem .75rem',        bodyClass:'mv-nw-body mv-nw-focus',    react:react('focus', 'newsFocus')},
        {id:'book',     title:'FOR YOUR BOOK',   icon:'ph ph-target',        pad:'.5rem .625rem .5rem .75rem', bodyClass:'mv-nw-body mv-nw-book', react:react('book', 'newsBook')},
        {id:'tape',     title:'THE TAPE',        icon:'ph ph-newspaper',     pad:'.5rem .625rem .5rem .75rem', bodyClass:'mv-nw-body mv-nw-tape', react:react('tape', 'newsTape')}
      ]
    });
  }catch(e){ warn('gridPage failed', e); handle = null; }
  if(!handle){ st.react = false; return false; }
  st.handle = handle;
  sec.classList.add('mv-nw-grid-on');
  var legacy = document.getElementById('mv-nw-legacy');
  if(legacy) legacy.hidden = true;
  watchBundle();
  return st.react;
}

window.MasonNews = {
  mount: mount,
  react: function(){ return !!st.handle && st.react; },
  handle: function(){ return st.handle; },
  layout: function(){ return JSON.parse(JSON.stringify(LAYOUT)); }
};
})();
