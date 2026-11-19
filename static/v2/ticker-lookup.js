/* static/v2/ticker-lookup.js — ticker lookup (2026-11-18): the header ticker search, the Ctrl+K ticker rows and the
   ticker card popup both of them open.

   Loaded at the end of templates/mason_v2.html (after mv, MasonChart and mv.wm exist). Hooks in the template:
   the header markup (#mv-tk, left of the "Search or run a command" trigger) and one call in the palette's
   renderCmd (mv.ticker.cmdRows). The React parts live in the MasonUI bundle: 'tickerSearch' (the header list,
   React Bits AnimatedList) and 'tickerCard' (the popup's content), both mounted with MasonUI.mount.

   mv.ticker.open(sym[, {name, from}])   opens (or switches) the popup for a ticker. Any code may call it.
   mv.ticker.close() / isOpen() / symbol()
   mv.ticker.search(q)                    → Promise<[{symbol, name}]>  (GET /api/symbol_search, cached 5 min per query)
   mv.ticker.rank(q, results)             exact symbol first, then symbol-prefix matches, then the rest (API order kept)
   mv.ticker.cmdRows(q, onUpdate)         the palette's ticker rows for q (sync; fetches in the background, then onUpdate())

   Type-ahead: 170 ms debounce; every answer carries the token of the keystroke that asked, so a slow answer for "N"
   can never overwrite the rows for "NV". While an answer is on its way the previous answer stays up, kept to rows that
   still match (dimmed). The first row is highlighted; Enter opens the highlighted row, or (nothing fresh to trust yet)
   the typed text when it looks like a ticker. A typed ticker the search did not return gets its own "look up" row.
   Results keep the server's {symbol, name}; an optional "type" (EQUITY | ETF) shows as a small tag when it isn't
   EQUITY. Only symbols the card accepts are listed (≤ 5 letters + an optional .X / -X class: BRK-B, BRK.B).
   Room: the search box only takes the header's free space (it never pushes the header onto another row); with less
   than 10rem free it becomes an icon that opens the same search as a small panel (a full-width bar on phones).
   Popup: role=dialog + aria-modal, focus moves in and is trapped (Tab / Shift+Tab wrap, focus that escapes is pulled
   back), Esc and a backdrop click close it, focus returns to whatever opened it. Ctrl+K still opens the palette on
   top of it (to look up another ticker). Every API string reaches the DOM as textContent (here) or JSX text (React).
*/
(function(){
'use strict';
var mv = window.mv;
if(!mv || mv.ticker) return;

var SEARCH_URL = '/api/symbol_search?q=';
var DEBOUNCE_MS = 170, TIMEOUT_MS = 8000, CACHE_MS = 5 * 60 * 1000, CACHE_MAX = 80;
var SYM_RE = /^[A-Z]{1,5}([.\-][A-Z])?$/;                       // what /api/ticker_card accepts (else 400 bad_symbol)
var TICKERISH_RE = /^[A-Za-z]{1,5}([.\-][A-Za-z])?$/;            // typed text that reads as a ticker (NVDA, brk.b)
var TYPE_RE = /^[A-Za-z]{2,10}$/;                                 // optional result "type" (EQUITY | ETF | …)
var QUERY_RE = /^[A-Za-z0-9][A-Za-z0-9 .&'\-]{0,39}$/;           // what we send to the search at all
var RECENT_KEY = 'mv_tk_recent_v1', RECENT_MAX = 6;
var PHONE_MQ = window.matchMedia ? window.matchMedia('(max-width: 40rem)') : null;    // = ticker-lookup.css full-width bar
var ICON_BELOW_PX = 160;                                          // the box needs 10rem; under that the wrapper shows the icon

function upper(s){ return String(s == null ? '' : s).replace(/\s+/g, '').toUpperCase(); }
function looksTicker(q){ return TICKERISH_RE.test(String(q == null ? '' : q).trim()); }
function cleanName(v){
  if(typeof v !== 'string') return '';
  var t = v.replace(/\s+/g, ' ').trim();
  return t.length > 120 ? t.slice(0, 119) + '…' : t;
}
function visible(el){ return !!(el && el.isConnected && el.getClientRects().length); }
/* Overlays go inside .m-shell, not <body>: .m-shell is a stacking context (position:relative; z-index:1, so it sits
   above the CRT background), and the palette (90), sidebar drawer (85) and chat drawer (70) live in it. On <body>
   the card would cover the palette instead of sitting under it. */
function layer(){ return document.querySelector('.m-shell') || document.body; }
function reduceMotion(){ return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
function phone(){ return !!(PHONE_MQ && PHONE_MQ.matches); }

/* ── search: cache + one request per query ─────────────────────────────────────────────────────────────── */
var cache = Object.create(null), cacheKeys = [], inflight = Object.create(null);
function cacheGet(k){ var e = cache[k]; return (e && Date.now() - e.t < CACHE_MS) ? e : null; }
function cachePut(k, results){
  if(!cache[k]) cacheKeys.push(k);
  cache[k] = {t:Date.now(), results:results};
  while(cacheKeys.length > CACHE_MAX) delete cache[cacheKeys.shift()];
}
/* {results:[{symbol, name, type?}]} → at most 10 clean rows: symbol upper-cased and checked, name trimmed, type kept
   when it is a plain word, no duplicates. */
function cleanResults(d){
  var out = [], seen = Object.create(null);
  var list = (d && Array.isArray(d.results)) ? d.results : [];
  for(var i = 0; i < list.length && out.length < 10; i++){
    var r = list[i];
    if(!r || typeof r !== 'object' || typeof r.symbol !== 'string') continue;
    var s = upper(r.symbol);
    if(!SYM_RE.test(s) || seen[s]) continue;
    seen[s] = 1;
    out.push({symbol:s, name:cleanName(r.name), type:(typeof r.type === 'string' && TYPE_RE.test(r.type)) ? r.type.toUpperCase() : null});
  }
  return out;
}
function search(q){
  var t = String(q == null ? '' : q).trim(), k = t.toLowerCase();
  if(!t) return Promise.resolve([]);
  var hit = cacheGet(k);
  if(hit) return Promise.resolve(hit.results);
  if(inflight[k]) return inflight[k];
  var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
  var timer = setTimeout(function(){ if(ctl) ctl.abort(); }, TIMEOUT_MS);
  var p = fetch(SEARCH_URL + encodeURIComponent(t), {credentials:'same-origin', headers:{Accept:'application/json'},
                                                    signal:ctl ? ctl.signal : undefined})
    .then(function(r){ if(!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(function(d){ var res = cleanResults(d); cachePut(k, res); return res; });
  inflight[k] = p;
  var done = function(){ clearTimeout(timer); delete inflight[k]; };
  p.then(done, done);
  return p;
}
/* Exact symbol first, then symbols that start with what was typed, then everything else (company-name matches);
   the API's own order is kept inside each group (it ranks by relevance/popularity). */
function rank(q, results){
  var Q = upper(q);
  return (results || []).map(function(r, i){
    return {r:r, i:i, t:(r.symbol === Q ? 0 : (Q && r.symbol.indexOf(Q) === 0 ? 1 : 2))};
  }).sort(function(a, b){ return (a.t - b.t) || (a.i - b.i); }).map(function(x){ return x.r; });
}
function lookupRow(q){ var Q = upper(q); return {key:'l:' + Q, symbol:Q, name:'', type:null, kind:'lookup', exact:false}; }
/* rows to show for q: ranked matches, plus a "look up Q" row when Q reads as a ticker and the search didn't return it */
function rowsFor(q, results){
  var Q = upper(q);
  var rows = rank(q, results).map(function(r){
    return {key:'m:' + r.symbol, symbol:r.symbol, name:r.name, type:r.type || null, kind:'match', exact:r.symbol === Q};
  });
  if(looksTicker(q) && !rows.some(function(r){ return r.exact; })) rows.push(lookupRow(q));
  return rows;
}

/* ── recent lookups (this browser) ─────────────────────────────────────────────────────────────────────── */
function recentGet(){
  var a = [];
  try{ a = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); }catch(e){ a = []; }
  if(!Array.isArray(a)) return [];
  var out = [], seen = Object.create(null);
  a.forEach(function(x){
    if(!x || typeof x.s !== 'string') return;
    var s = upper(x.s);
    if(!SYM_RE.test(s) || seen[s] || out.length >= RECENT_MAX) return;
    seen[s] = 1; out.push({s:s, n:cleanName(x.n)});
  });
  return out;
}
function recentAdd(sym, name){
  var list = recentGet(), prev = null;
  list = list.filter(function(x){ if(x.s === sym){ prev = x; return false; } return true; });
  list.unshift({s:sym, n:cleanName(name) || (prev ? prev.n : '')});
  try{ localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))); }catch(e){}
}

/* ══ header ticker search (combobox) ══════════════════════════════════════════════════════════════════════ */
var H = {wrap:null, btn:null, box:null, inp:null, x:null, dd:null, host:null, scrim:null,
         q:'', rows:[], hi:-1, hiByUser:false, fresh:false, loading:false, note:null, mode:'search',
         open:false, overlay:false, token:0, timer:0, last:null, pendingEnter:false, handle:null, fallback:false};
function optId(i){ return 'mv-tk-opt-' + i; }

function hRender(){
  if(!H.inp) return;
  var show = H.open && (H.rows.length > 0 || !!H.note || H.loading);
  H.inp.setAttribute('aria-expanded', show ? 'true' : 'false');
  if(show && H.hi >= 0 && H.rows[H.hi]) H.inp.setAttribute('aria-activedescendant', optId(H.hi));
  else H.inp.removeAttribute('aria-activedescendant');
  H.dd.hidden = !show;
  if(!show) return;
  hPlace();
  var M = window.MasonUI;
  if(M && typeof M.mount === 'function' && !M.failed && !H.fallback){
    var props = hProps();
    if(H.handle){ H.handle.update(props); return; }
    H.host.textContent = '';
    try{ H.handle = M.mount(H.host, 'tickerSearch', props); return; }
    catch(err){ H.handle = null; H.fallback = true; }
  }
  hRenderFallback();
}
function hProps(){
  return {rows:H.rows, selectedIndex:H.hi, query:H.mode === 'recent' ? '' : H.q, loading:H.loading,
          heading:H.mode === 'recent' ? 'Recent' : null, note:H.note, listId:'mv-tk-list', optionId:optId,
          onPick:hPick, onHover:hHover};
}
/* Vanilla list: only while the MasonUI bundle is still loading or failed. Same rows, ids and ARIA. */
function hRenderFallback(){
  var host = H.host;
  host.textContent = '';
  var wrap = document.createElement('div'); wrap.className = 'mv-tk-fb';
  if(H.mode === 'recent' && H.rows.length){
    var hd = document.createElement('div'); hd.className = 'mv-tk-fb-head'; hd.textContent = 'Recent'; wrap.appendChild(hd);
  }
  if(H.rows.length){
    var lb = document.createElement('div'); lb.id = 'mv-tk-list'; lb.setAttribute('role', 'listbox');
    lb.setAttribute('aria-label', H.mode === 'recent' ? 'Recent tickers' : 'Ticker matches');
    H.rows.forEach(function(r, i){
      var o = document.createElement('div');
      o.id = optId(i); o.setAttribute('role', 'option'); o.setAttribute('aria-selected', i === H.hi ? 'true' : 'false');
      o.className = 'mv-tk-fb-opt' + (i === H.hi ? ' hi' : '');
      var s = document.createElement('b'); s.textContent = r.symbol;
      var n = document.createElement('span'); n.textContent = r.kind === 'lookup' ? 'Open the ticker card' : r.name;
      o.appendChild(s); o.appendChild(n);
      o.addEventListener('mouseenter', function(){ hHover(i); });
      o.addEventListener('click', function(){ hPick(i); });
      lb.appendChild(o);
    });
    wrap.appendChild(lb);
  }
  if(H.note){ var nt = document.createElement('div'); nt.className = 'mv-tk-fb-note'; nt.textContent = H.note; wrap.appendChild(nt); }
  host.appendChild(wrap);
}
/* Keep the list inside the window: wider than the box, shifted left when the box sits near the right edge. */
function hPlace(){
  var dd = H.dd;
  if(H.overlay){ dd.style.left = ''; return; }
  dd.style.left = '0px';
  var r = H.box.getBoundingClientRect(), w = dd.offsetWidth || 0, vw = document.documentElement.clientWidth;
  var over = r.left + w - (vw - 12);
  if(over > 0) dd.style.left = Math.round(-Math.min(over, Math.max(0, r.left - 12))) + 'px';
}
function hOpen(){ H.open = true; hRender(); }
function hClose(){
  H.open = false; H.pendingEnter = false;
  hRender();
}
function hHover(i){
  if(i === H.hi || !H.rows[i]) return;
  H.hi = i; H.hiByUser = true; hRender();
}
function hPick(i){
  var r = H.rows[i]; if(!r) return;
  hGo(r.symbol, r.kind === 'lookup' ? '' : r.name);
}
function hGo(sym, name){
  H.token++; clearTimeout(H.timer);
  H.inp.value = ''; H.q = ''; H.rows = []; H.hi = -1; H.note = null; H.loading = false;
  hClose();
  if(H.overlay) hCollapse(true);                 // focus the icon first, so the card hands focus back to it on close
  mv.ticker.open(sym, {name:name, from:'header'});
}
function hShowRecent(wantOpen){
  var rec = recentGet();
  H.mode = 'recent'; H.loading = false; H.fresh = true; H.note = null; H.pendingEnter = false;
  H.rows = rec.map(function(x){ return {key:'r:' + x.s, symbol:x.s, name:x.n, kind:'recent', exact:false}; });
  H.hi = -1; H.hiByUser = false;
  if(wantOpen && H.rows.length) hOpen(); else hClose();
}
function hApply(t, res){
  var prevSym = (H.hiByUser && H.rows[H.hi]) ? H.rows[H.hi].symbol : null;
  H.mode = 'search'; H.loading = false; H.fresh = true;
  H.rows = rowsFor(t, res);
  H.hi = H.rows.length ? 0 : -1;
  if(prevSym){
    var j = -1;
    H.rows.forEach(function(r, k){ if(j < 0 && r.symbol === prevSym) j = k; });
    if(j >= 0) H.hi = j; else H.hiByUser = false;
  }
  H.note = H.rows.length ? null : 'No matches for “' + t + '”. Try the ticker, like NVDA.';
  if(H.pendingEnter){
    H.pendingEnter = false;
    if(H.rows.length){ hPick(H.hi >= 0 ? H.hi : 0); return; }
  }
  hOpen();
}
function hOnInput(){
  var t = H.inp.value.trim();
  H.q = t; H.pendingEnter = false; clearTimeout(H.timer); H.token++;
  if(!t){ hShowRecent(H.open); return; }
  H.mode = 'search';
  if(!QUERY_RE.test(t)){
    H.rows = []; H.hi = -1; H.fresh = true; H.loading = false;
    H.note = 'Letters and numbers only, like NVDA or Apple.';
    hOpen(); return;
  }
  var hit = cacheGet(t.toLowerCase());
  if(hit){ H.last = {q:t, results:hit.results}; hApply(t, hit.results); return; }
  // While the answer for t is on its way: the newest answer we have, kept to rows that still match t.
  var Q = upper(t), ql = t.toLowerCase(), prev = H.last ? H.last.results : [];
  var keep = prev.filter(function(r){ return r.symbol.indexOf(Q) === 0 || (r.name && r.name.toLowerCase().indexOf(ql) !== -1); });
  H.rows = rowsFor(t, keep);
  H.hi = H.rows.length ? 0 : -1; H.hiByUser = false;
  H.fresh = false; H.loading = true; H.note = null;
  hOpen();
  var tok = H.token;
  H.timer = setTimeout(function(){
    search(t).then(function(res){
      if(tok !== H.token) return;                       // a newer keystroke owns the list now: drop this answer
      H.last = {q:t, results:res};
      hApply(t, res);
    }, function(){
      if(tok !== H.token) return;
      H.loading = false; H.fresh = true;
      H.note = looksTicker(t) ? 'Search isn’t answering right now. Press Enter to open ' + upper(t) + ' anyway.'
                              : 'Search isn’t answering right now. Try again in a moment.';
      if(H.pendingEnter){ H.pendingEnter = false; }
      hOpen();
    });
  }, DEBOUNCE_MS);
}
function hEnter(){
  var t = H.inp.value.trim();
  if(!t){ if(H.open && H.mode === 'recent' && H.hi >= 0) hPick(H.hi); return; }
  var r = H.open ? H.rows[H.hi] : null;
  // trust the highlight when it is the answer for exactly t, the user moved it there, or it IS what was typed
  if(r && (H.fresh || H.hiByUser || r.exact || r.kind === 'lookup')){ hPick(H.hi); return; }
  if(looksTicker(t)){ hGo(upper(t), ''); return; }
  if(H.loading){ H.pendingEnter = true; return; }             // a company name: open the best match when it lands
  if(H.rows.length) hPick(0);
}
function hMove(d){
  if(!H.rows.length) return;
  if(H.hi < 0) H.hi = d > 0 ? 0 : H.rows.length - 1;
  else H.hi = Math.max(0, Math.min(H.rows.length - 1, H.hi + d));
  H.hiByUser = true; hRender();
}
function hKey(e){
  if(e.isComposing || e.keyCode === 229) return;
  var k = e.key;
  if(k === 'ArrowDown'){
    e.preventDefault();
    if(!H.open){ if(H.inp.value.trim()) hOnInput(); else hShowRecent(true); if(H.open && H.rows.length && H.hi < 0){ H.hi = 0; H.hiByUser = true; hRender(); } return; }
    hMove(1);
  } else if(k === 'ArrowUp'){
    e.preventDefault();
    if(H.open) hMove(-1);
  } else if(k === 'Enter'){
    e.preventDefault();
    hEnter();
  } else if(k === 'Escape'){
    if(P.open) return;                                       // the card's own Esc handler (document) closes it
    e.preventDefault(); e.stopPropagation();
    if(H.open) hClose();
    else if(H.inp.value){ H.inp.value = ''; hOnInput(); }
    else if(H.overlay) hCollapse(true);
    else H.inp.blur();
  } else if(k === 'Tab'){
    hClose();
  }
}
/* Icon mode: the wrapper only gets the header's free space (flex-basis 0, ticker-lookup.css), so it never pushes
   the market pill / clock onto another row. Under 10rem of room it shows the icon instead of the box. */
function isIcon(){ return !!(H.wrap && H.wrap.classList.contains('is-icon')); }
function hFit(){
  if(!H.wrap) return;
  var w = H.wrap.clientWidth, icon = w < ICON_BELOW_PX;
  H.wrap.classList.toggle('is-tight', !icon && w < 210);        // a short box drops the "/" hint, not the placeholder
  if(icon === isIcon()) return;
  if(!icon && H.overlay) hCollapse(false);
  H.wrap.classList.toggle('is-icon', icon);
  if(icon && !H.overlay && H.open) hClose();
}
/* the icon opens the search: a small panel under it on a desktop, a full-width bar (+ scrim) on a phone */
function hExpand(){
  if(H.overlay) return;
  H.overlay = true;
  H.wrap.classList.add('is-open');
  H.btn.setAttribute('aria-expanded', 'true');
  if(phone()){
    if(!H.scrim){
      H.scrim = document.createElement('div'); H.scrim.className = 'mv-tk-scrim'; H.scrim.hidden = true;
      H.scrim.addEventListener('click', function(){ hCollapse(false); });
      layer().appendChild(H.scrim);
    }
    H.scrim.hidden = false;
  }
  hPlacePanel();
  H.inp.focus();
}
/* keep the desktop panel inside the window (it hangs from the icon's right edge) */
function hPlacePanel(){
  H.box.style.right = '';
  if(!H.overlay || phone()) return;
  var r = H.box.getBoundingClientRect();
  if(r.left < 12) H.box.style.right = Math.round(r.left - 12) + 'px';
}
function hCollapse(refocus){
  if(!H.overlay) return;
  H.overlay = false;
  H.wrap.classList.remove('is-open');
  H.btn.setAttribute('aria-expanded', 'false');
  H.box.style.right = '';
  if(H.scrim) H.scrim.hidden = true;
  hClose();
  if(refocus && visible(H.btn)) H.btn.focus();
}
function focusSearch(){
  if(!H.inp) return false;
  if(isIcon()) hExpand(); else H.inp.focus();
  return true;
}
function hWire(){
  H.wrap = document.getElementById('mv-tk');
  H.btn = document.getElementById('mv-tk-btn');
  H.box = document.getElementById('mv-tk-box');
  H.inp = document.getElementById('mv-tk-input');
  H.x = document.getElementById('mv-tk-x');
  H.dd = document.getElementById('mv-tk-dd');
  if(!H.wrap || !H.inp || !H.dd || !H.box || !H.btn){ H.inp = null; return; }
  H.host = document.createElement('div'); H.host.className = 'mv-tk-dd-host';
  H.dd.appendChild(H.host);
  H.dd.addEventListener('mousedown', function(e){ e.preventDefault(); });   // a click in the list keeps the input focused
  H.inp.addEventListener('input', hOnInput);
  H.inp.addEventListener('keydown', hKey);
  H.inp.addEventListener('mousedown', function(){                // a click in the box reopens the list (or the recents)
    if(H.open) return;
    if(H.inp.value.trim()) hOnInput(); else hShowRecent(true);
  });
  H.inp.addEventListener('blur', function(){
    setTimeout(function(){ if(document.activeElement !== H.inp) hClose(); }, 0);
  });
  H.btn.addEventListener('click', function(){ if(H.overlay) hCollapse(true); else hExpand(); });
  if(H.x) H.x.addEventListener('click', function(){ hCollapse(true); });
  document.addEventListener('pointerdown', function(e){
    if(!H.wrap.contains(e.target)){
      if(H.open) hClose();
      if(H.overlay && !(H.scrim && e.target === H.scrim)) hCollapse(false);
    }
  });
  if(typeof ResizeObserver === 'function') new ResizeObserver(hFit).observe(H.wrap);
  window.addEventListener('resize', function(){
    hFit();
    if(H.overlay){ if(H.scrim) H.scrim.hidden = !phone(); hPlacePanel(); }
    if(H.open) hPlace();
  });
  hFit();
  /* the bundle finished loading while the vanilla list is up: swap in the React one */
  window.addEventListener('masonui:ready', function(){
    if(H.fallback || !H.handle){ H.fallback = false; if(H.open){ H.host.textContent = ''; hRender(); } }
  });
}

/* "/" focuses the ticker search (not while typing somewhere, nor over the palette or the card) */
document.addEventListener('keydown', function(e){
  if(e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
  if(mv.cmdOpen || P.open || !H.inp) return;
  var t = e.target;
  if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''))) return;
  e.preventDefault();
  focusSearch();
});

/* ══ palette (Ctrl+K) ticker rows ═════════════════════════════════════════════════════════════════════════
   renderCmd (template) appends cmdRows(q) after the page/command rows in root mode. Synchronous: cached answers
   become rows right away; otherwise a typed ticker gets its "look up" row at once and the search runs in the
   background, then onUpdate() re-renders the palette (the template checks it is still open on the same text). */
var C = {timer:0, pending:null, token:0};
var CMD_SKIP_RE = /^(add\s+)?chart[:\s]/i;           // "Add chart: NVDA" is the palette's own command
function cmdRows(q, onUpdate){
  var t = String(q == null ? '' : q).trim();
  if(!t || !QUERY_RE.test(t) || CMD_SKIP_RE.test(t) || t.split(/\s+/).length > 4) return [];
  var hit = cacheGet(t.toLowerCase()), rows;
  if(hit) rows = rowsFor(t, hit.results);
  else {
    rows = looksTicker(t) ? [lookupRow(t)] : [];
    if(C.pending !== t){
      C.pending = t; clearTimeout(C.timer);
      var tok = ++C.token;
      C.timer = setTimeout(function(){
        var settle = function(ok){
          if(C.pending === t) C.pending = null;
          if(ok && tok === C.token && typeof onUpdate === 'function'){ try{ onUpdate(); }catch(e){} }
        };
        search(t).then(function(){ settle(true); }, function(){ settle(false); });
      }, DEBOUNCE_MS);
    }
  }
  return rows.slice(0, 8).map(function(r, i){
    var look = r.kind === 'lookup';
    return {label:r.symbol, sub:look ? 'Look up this ticker' : r.name, icon:'ph ph-chart-line-up', cls:'mv-pick-opt mv-tk-opt',
            chip:look ? 'OPEN' : (r.type && r.type !== 'EQUITY' ? r.type : (r.exact ? 'EXACT' : 'TICKER')),
            head:i === 0 ? 'Tickers' : null, always:true,
            run:function(){ mv.ticker.open(r.symbol, {from:'palette', name:look ? '' : r.name}); }};
  });
}

/* ══ the ticker card popup ════════════════════════════════════════════════════════════════════════════════ */
var P = {open:false, sym:null, el:null, box:null, host:null, handle:null, opener:null, hideTimer:0, downOnBackdrop:false};
var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function pShell(){
  if(P.el) return;
  var el = document.createElement('div');
  el.className = 'mv-tkc-modal'; el.id = 'mv-tkc-modal'; el.hidden = true;
  var box = document.createElement('div');
  box.className = 'mv-tkc-box'; box.id = 'mv-tkc-box'; box.tabIndex = -1;
  box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true'); box.setAttribute('aria-labelledby', 'mv-tkc-title');
  var host = document.createElement('div'); host.className = 'mv-tkc-host';
  box.appendChild(host); el.appendChild(box); layer().appendChild(el);
  el.addEventListener('mousedown', function(e){ P.downOnBackdrop = e.target === el; });
  el.addEventListener('click', function(e){
    if(e.target === el && P.downOnBackdrop) pClose();     // a drag that started inside the card and ended outside isn't a click-out
    P.downOnBackdrop = false;
  });
  box.addEventListener('keydown', pKey);
  P.el = el; P.box = box; P.host = host;
}
function pFocusables(){
  return Array.prototype.filter.call(P.box.querySelectorAll(FOCUSABLE), function(n){ return n.getClientRects().length > 0; });
}
function pKey(e){
  if(e.key === 'Escape'){
    if(e.defaultPrevented) return;
    e.preventDefault(); e.stopPropagation();
    pClose();
  } else if(e.key === 'Tab'){
    var f = pFocusables(), a = document.activeElement;
    if(!f.length){ e.preventDefault(); P.box.focus(); return; }
    if(e.shiftKey){ if(a === f[0] || a === P.box || !P.box.contains(a)){ e.preventDefault(); f[f.length - 1].focus(); } }
    else if(a === f[f.length - 1] || !P.box.contains(a)){ e.preventDefault(); f[0].focus(); }
  }
}
function canAddChart(){ return !!(mv.wm && typeof mv.wm.addChart === 'function'); }
function pAddChart(sym, pt){
  pClose();
  if(canAddChart()) mv.wm.addChart(sym, pt || null);
}
function pProps(){
  return {symbol:P.sym, onClose:pClose, onAddChart:canAddChart() ? pAddChart : undefined, follow:true, titleId:'mv-tkc-title'};
}
/* No bundle (still loading, or it failed): a plain card that says so, with a working close button. */
function pFallback(msg){
  var host = P.host;
  host.textContent = '';
  var w = document.createElement('div'); w.className = 'mv-tkc-fb';
  var hd = document.createElement('div'); hd.className = 'mv-tkc-fb-head';
  var h = document.createElement('h2'); h.id = 'mv-tkc-title'; h.textContent = P.sym || '';
  var x = document.createElement('button'); x.type = 'button'; x.className = 'mv-tkc-fb-x'; x.setAttribute('aria-label', 'Close the ticker card');
  var xi = document.createElement('i'); xi.className = 'ph ph-x'; xi.setAttribute('aria-hidden', 'true'); x.appendChild(xi);
  x.addEventListener('click', pClose);
  hd.appendChild(h); hd.appendChild(x);
  var p = document.createElement('p'); p.className = 'mv-tkc-fb-msg';
  p.textContent = msg || 'Loading the ticker card…';
  if(!msg) p.setAttribute('role', 'status');
  w.appendChild(hd); w.appendChild(p); host.appendChild(w);
}
function pMount(){
  var M = window.MasonUI;
  if(M && typeof M.mount === 'function' && !M.failed){
    if(P.handle){ P.handle.update(pProps()); return; }
    P.host.textContent = '';
    try{ P.handle = M.mount(P.host, 'tickerCard', pProps()); return; }
    catch(err){ P.handle = null; pFallback('The ticker card couldn’t start. A page refresh usually fixes that.'); return; }
  }
  if(M && M.failed){ pFallback('The ticker card needs Mason’s UI bundle, which didn’t load. A page refresh usually fixes that.'); return; }
  pFallback(null);
  if(M && M.ready && typeof M.ready.then === 'function') M.ready.then(function(){ if(P.open && !P.handle) pMount(); });
}
function pOpen(sym, opts){
  var s = upper(sym).slice(0, 15);
  if(!s) return false;
  opts = opts || {};
  pShell();
  recentAdd(s, opts.name || '');
  if(!P.open){
    var a = document.activeElement;
    P.opener = (a && a !== document.body && a !== document.documentElement && !P.el.contains(a)) ? a : null;
    if(P.opener && P.opener.closest && P.opener.closest('#mv-cmd-modal')){
      // opened from the palette, which closes now: hand focus back to the ticker search instead
      P.opener = visible(H.inp) ? H.inp : (visible(H.btn) ? H.btn : null);
    }
    P.open = true;
    clearTimeout(P.hideTimer);
    P.el.hidden = false;
    P.el.classList.remove('is-in');
    void P.el.offsetWidth;                                  // restart the entrance transition
    P.el.classList.add('is-in');
  }
  var changed = P.sym !== s;
  P.sym = s;
  pMount();
  if(changed) P.box.scrollTop = 0;
  // now, not on a timer: a key pressed right after Enter (Esc, Tab) must already land in the card
  if(!P.box.contains(document.activeElement)) P.box.focus({preventScroll:true});
  return true;
}
function pClose(){
  if(!P.open) return;
  P.open = false;
  var h = P.handle; P.handle = null;
  P.el.classList.remove('is-in');
  if(h) setTimeout(function(){ try{ h.unmount(); }catch(e){} }, 0);   // outside React's own click handler
  clearTimeout(P.hideTimer);
  P.hideTimer = setTimeout(function(){ if(!P.open){ P.el.hidden = true; P.host.textContent = ''; P.sym = null; } }, reduceMotion() ? 0 : 170);
  var o = P.opener; P.opener = null;
  if(o && visible(o) && typeof o.focus === 'function'){ try{ o.focus({preventScroll:true}); }catch(e){} }
}
/* focus that wanders out of the open card (e.g. after the palette closed over it) comes back to it */
document.addEventListener('focusin', function(e){
  if(!P.open || mv.cmdOpen || !P.el || P.el.contains(e.target)) return;
  if(e.target && e.target.closest && e.target.closest('.mv-dash-toast')) return;      // the dashboard's undo toast
  P.box.focus({preventScroll:true});
});
/* Esc with focus on the page itself (not in the card), the palette closed */
document.addEventListener('keydown', function(e){
  if(!P.open || e.key !== 'Escape' || e.defaultPrevented || mv.cmdOpen) return;
  e.preventDefault();
  pClose();
});

mv.ticker = {
  open:pOpen, close:pClose,
  isOpen:function(){ return P.open; },
  symbol:function(){ return P.open ? P.sym : null; },
  search:search, rank:rank, rowsFor:rowsFor, looksTicker:looksTicker,
  cmdRows:cmdRows, focusSearch:focusSearch,
  _state:function(){                                   // tests (Tests/ui/test_ticker_lookup.py)
    return {q:H.q, open:H.open, hi:H.hi, fresh:H.fresh, loading:H.loading, note:H.note, mode:H.mode, overlay:H.overlay,
            rows:H.rows.map(function(r){ return r.symbol + ':' + r.kind; }), react:!!H.handle, cardReact:!!P.handle};
  }
};
hWire();
})();
