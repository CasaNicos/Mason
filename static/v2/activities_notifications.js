/* ═══════════════════════════════════════════════════════════════════════════
   Task 7 (UI overhaul 2026-11-16) — Activities + Notifications → widget grids.
   Paired with static/v2/activities_notifications.css.

   Each page's existing blocks (markup in templates/mason_v2.html, tagged
   .an-block with an id) are MOVED — never cloned or rebuilt — into Gridstack
   widgets inside the same <section>. Moving a node keeps its id, its own
   listeners, and its place under the section, so everything the template
   wires still works unchanged:
     - getElementById lookups (pd-canvas, pd-tip, pd-speed, mv-act-*, vp-*)
     - delegated handlers on #page-notifications (data-nselbar, data-nselect,
       data-vpclearfeed, data-ndismiss, data-nreview, data-rv*) — the grid
       lives inside the section, so clicks still bubble to it
     - direct listeners (data-vplimit dropdowns, data-acttier, Load more, …)
     - #page-notifications.vp-selecting styling (select mode)
   The floating #vp-selbar stays page-level (it is not a block).

   Glue added here (all in-place, no re-render):
     - the widget body hosting a block gets .an-host (the CSS size container)
       and the block gets .is-widget (turns on the widget layout rules)
     - the now-empty old wrappers (.vp-wrap/.vp-cols) are hidden
     - the three notification lists' edge fades re-measure when their widget
       resizes (ResizeObserver → the list's own 'scroll' listener, which runs
       the template's vpColFades)
     - Pulse Deck: the template's pdDraw() calls pdResize() every frame, which
       re-reads the canvas's client box, so the orb follows the widget size
       on its own; a resize just wakes the tooltip off (stale position).
   Layouts persist per page through Task 1's mv.gridPage helper (localStorage
   mv_gs3_activities / mv_gsc3_activities, mv_gs3_notifications / mv_gsc3_notifications).
   ═══════════════════════════════════════════════════════════════════════════ */
(function(){
'use strict';

/* Default geometry, in the grid's native units (36 columns, square cells,
   ≈ 38px per cell on a 1440px screen). Matches today's fixed pages:
   Activities = orb (26rem stage) on top, ledger below, both full width;
   Notifications = console strip on top, three equal columns below. */
var PAGES = {
  activities: {
    section: 'page-activities', gridId: 'mv-act-grid',
    widgets: [
      {id:'act_pulse',  block:'mv-pulse-deck', title:'PULSE DECK',  icon:'ph ph-planet',
       x:0, y:0,  w:36, h:16, minW:6, minH:5},
      {id:'act_ledger', block:'mv-act-ledger', title:'WORK LEDGER', icon:'ph ph-list-checks',
       x:0, y:16, w:36, h:20, minW:8, minH:5}
    ],
    emptied: []
  },
  notifications: {
    section: 'page-notifications', gridId: 'vp-grid',
    widgets: [
      {id:'nt_centre', block:'vp-centre',     title:'NOTIFICATION CENTRE', icon:'ph ph-bell',
       x:0,  y:0, w:36, h:9,  minW:6, minH:3},
      {id:'nt_alerts', block:'vp-col-alerts', title:'ALERTS',              icon:'ph ph-warning-octagon',
       x:0,  y:9, w:12, h:20, minW:6, minH:5},
      {id:'nt_review', block:'vp-col-review', title:'NEEDS REVIEW',        icon:'ph ph-gavel',
       x:12, y:9, w:12, h:20, minW:6, minH:5},
      {id:'nt_feed',   block:'vp-col-feed',   title:'THE FEED',            icon:'ph ph-broadcast',
       x:24, y:9, w:12, h:20, minW:6, minH:5}
    ],
    emptied: ['.vp-wrap']   // console+cols wrapper; .vp-selbar is outside it and stays put
  }
};

var mounted = {};

function blockEl(w){ return document.getElementById(w.block); }

/* ── Task 1's page-grid helper (static/v2/grid-pages.js → mv.gridPage) ──────
   Moves each block into a standard .m-widget shell (header = drag handle with
   icon + title + collapse), persists per page in mv_gs3_<page>/mv_gsc3_<page>,
   and joins the page to Edit layout. Idempotent: a 2nd call returns the same
   handle. Returns null if the helper isn't loaded (page keeps its classic layout). */
function helperMount(pageKey, spec){
  if(!window.mv || typeof window.mv.gridPage !== 'function') return null;
  var layout = {};
  spec.widgets.forEach(function(w){ layout[w.id] = {x:w.x, y:w.y, w:w.w, h:w.h, minW:w.minW, minH:w.minH}; });
  return window.mv.gridPage(pageKey, {
    el: spec.gridId,
    layout: layout,
    widgets: spec.widgets.map(function(w){
      return {id:w.id, title:w.title, icon:w.icon, block:w.block, bodyClass:'an-host'};
    })
  });
}

/* ── per-block glue after the helper has placed the nodes ─────────────────── */
function tagBlocks(spec){
  spec.widgets.forEach(function(w){
    var b = blockEl(w); if(!b) return;
    b.classList.add('is-widget');
    if(b.parentNode && b.parentNode.classList) b.parentNode.classList.add('an-host');
  });
  var sec = document.getElementById(spec.section);
  spec.emptied.forEach(function(sel){
    var el = sec && sec.querySelector(sel);
    if(el && !el.querySelector('.an-block')) el.classList.add('an-emptied');
  });
  if(sec) sec.classList.add('an-grid-page');
}

/* Notifications: a widget resize changes each list's clientHeight, so its
   top/bottom fade masks must be recomputed. The template wires a 'scroll'
   listener on each list (vpAnimateCol → vpColFades); firing that event is the
   in-place refresh, no re-render. rAF-coalesced so a drag-resize costs one
   measure per frame at most. */
function wireListFades(){
  if(!('ResizeObserver' in window)) return;
  ['alerts','review','feed'].forEach(function(col){
    var list = document.getElementById('vp-'+col);
    if(!list || list._anRO) return;
    var pending = false;
    list._anRO = new ResizeObserver(function(){
      if(pending) return; pending = true;
      requestAnimationFrame(function(){ pending = false; list.dispatchEvent(new Event('scroll')); });
    });
    list._anRO.observe(list);
  });
}

/* Pulse Deck: the orb re-fits itself every frame (pdResize in the template).
   Hide the hover tooltip on resize so it doesn't float at a stale spot. */
function wirePulseDeck(){
  var wrap = document.querySelector('#mv-pulse-deck .pd-canvas-wrap');
  if(!wrap || wrap._anRO || !('ResizeObserver' in window)) return;
  wrap._anRO = new ResizeObserver(function(){
    var t = document.getElementById('pd-tip'); if(t) t.style.display = 'none';
  });
  wrap._anRO.observe(wrap);
}

function mountPage(pageKey){
  if(mounted[pageKey]) return true;
  var spec = PAGES[pageKey]; if(!spec) return false;
  var sec = document.getElementById(spec.section);
  if(!sec || sec.hidden) return false;               // Gridstack needs a laid-out (visible) container
  for(var i=0;i<spec.widgets.length;i++){ if(!blockEl(spec.widgets[i])) return false; }
  var res = helperMount(pageKey, spec);
  if(!res) return false;                             // helper missing → page stays in its classic layout
  mounted[pageKey] = res;
  tagBlocks(spec);
  if(pageKey === 'notifications') wireListFades();
  if(pageKey === 'activities') wirePulseDeck();
  return true;
}

/* Called from the template's own mount functions on EVERY visit
   (mountActivities / mountNotifications in mason_v2.html), after the section is
   un-hidden by mv.go — the helper's documented pattern. */
window.MasonAN = {pages:PAGES, mount:mountPage, mounted:function(k){ return !!mounted[k]; }};
})();
