/* mason_engine_v2.js — VERBATIM copy of templates/mason.html lines 816-3648
   (the chat / voice / speech-synth / 3D-crystal / analysis engine).
   This file is now the source of truth (templates/mason.html, the legacy v1
   dashboard, was retired 2026-11-17; git history keeps it). Loaded by
   templates/mason_v2.html.
   No Jinja constructs exist in this block, so it is served as a plain static
   file (the {% raw %} wrapper in mason.html was purely defensive).
*/
// Restore voice preference — default ON
var voiceOn=(localStorage.getItem('mason_voice')!=='off');
var micActive=false,recog=null,thinking=null,polling=false,_voices=[],lastModelTier='';
// Tool names Mason has run for the bubble currently being generated — not
// spoken (TTS only reads streamed chunk text), just tagged onto the bubble
// like lastModelTier, and mirrored to the console via mason_app.py's runtime
// log ("tools called: ..."). Reset once tagged so it never bleeds into the
// next bubble.
var lastToolsCalled=[];
var _queryCancelled=false,_activeReader=null; // Stop Processing guard (2026-07-19 bugfix) — see stopProcessing()/sendQuery()
var _autoListening=false,_shouldRearm=false;
var _queryAbort=null;
// Pending file upload
var _pendingFile=null;
// Conversation-closing phrases — don't re-arm listening after these
var _CLOSING=[
  'all set','all good','lgtm','thanks','thank you','no thanks','bye','goodbye',
  'got it','i got it','im good',"i'm good",'nevermind','never mind','stop listening',
  'that will do','that\'s all','thats all','that\'s it','thats it','i\'m done',
  'im done','catch you','catch you later','alright thanks','alright thank you',
  'last question','last thing','last one','sounds good','perfect thanks',
  'i\'ll think about it','ill think about it','no i\'m good','no im good'
];
function _isClosing(text){
  var t=text.toLowerCase().trim().replace(/[.!?,]+$/,'');
  return _CLOSING.some(function(p){ return t===p||t===p+'.'||t.endsWith(' '+p)||t.startsWith(p+' '); });
}
// #26 — only keep the mic open if Mason's reply actually asks something.
// Strip [BRACKET GLITCHES] then check the last ~300 chars for a "?" —
// catches questions followed by 1-2 trailing sentences, not just hard "?" endings.
function _asksQuestion(text){
  var t=(text||'').replace(/\[[^\]]*\]/g,'').replace(/\s+$/,'');
  var tail=t.length>300?t.slice(-300):t;
  return /\?/.test(tail);
}
var _noSpeechTimer=null; // fires if mic is open but nobody speaks for 4s

// Boot uses CSS crystal animation — no JS progress messages needed

function sleep(ms){return new Promise(function(r){setTimeout(r,ms);})}

// ── Audio unlock system ───────────────────────────────────────
var _audioReady=false;
var _pendingSpeech=null; // {text,onDone} queued before first gesture
var _lastBriefReport=''; // full text of the most recent brief — for "read it out loud"
var _lastMasonMsg='';    // full text of Mason's most recent reply — read-aloud fallback

// "Read it out loud" — Nico explicitly asking Mason to speak the full report.
// Briefs are NOT auto-read aloud (only the spoken summary is); this is opt-in.
var _READ_ALOUD=['read it out loud','read it outloud','read that out loud',
  'read the report','read the brief','read it to me','read that to me',
  'read it aloud','read aloud','read the full report','read me the report',
  'read it back','say it out loud','read the whole thing'];
function _isReadAloud(text){
  var t=text.toLowerCase().trim().replace(/[.!?,]+$/,'');
  return _READ_ALOUD.some(function(p){ return t===p||t.endsWith(' '+p)||t.startsWith(p+' ')||t.includes(p); });
}
var _wordTimers=[]; // timer IDs for word-pulse animation (cleared on stopSpeak)
var _speechStopRequested=false; // set by stopSpeak(); speakOrdis's onend-chain checks this before continuing (2026-07-19 bugfix)
var _synthKA=null; // Chrome keepalive interval — prevents synthesis stall on long responses
var _seenNewsTitles=new Set(); // rolling dedup tracker for news feed
var _NEWS_MAX=36;             // max items kept in the feed (older ones revolve out)
var _NEWS_INITIAL=12;        // items shown on the first paint

// Chrome bug: speechSynthesis silently stalls after ~10-15s (onend stops firing, chain breaks).
// Fix: ping pause()+resume() every 9s while speaking to reset Chrome's internal watchdog.
function _startSynthKA(){
  _stopSynthKA();
  _synthKA=setInterval(function(){
    if(window.speechSynthesis.speaking||window.speechSynthesis.pending){
      window.speechSynthesis.pause();
      window.speechSynthesis.resume();
    } else {
      _stopSynthKA();
    }
  },9000);
}
function _stopSynthKA(){
  if(_synthKA){clearInterval(_synthKA);_synthKA=null;}
}

// Stuck-'speaking' watchdog. Chrome's onend sometimes silently stops firing on a
// long reply, which leaves the _ss chain dead (_SS.playing stuck true) and the
// crystal frozen on 'speaking'. While the crystal is speaking we poll: if the synth
// has actually gone silent (not speaking, not pending) for ~2s, recover — nudge the
// streaming chain forward/closed, or force idle for the batch path. Debounced so the
// brief gap before an utterance's onstart doesn't trip it.
var _spkWatch=null,_spkStall=0;
function _startSpeakWatch(){
  _stopSpeakWatch(); _spkStall=0;
  _spkWatch=setInterval(function(){
    var ss=window.speechSynthesis;
    if(ss&&(ss.speaking||ss.pending)){ _spkStall=0; return; }
    if(++_spkStall<2) return;
    _spkStall=0;
    if(_SS&&_SS.active){ _SS.playing=false; try{_ssPump();}catch(e){} }
    else { _crystalState('idle'); }
  },1000);
}
function _stopSpeakWatch(){ if(_spkWatch){clearInterval(_spkWatch);_spkWatch=null;} _spkStall=0; }

function _clearWordTimers(){
  for(var _wt=0;_wt<_wordTimers.length;_wt++)clearTimeout(_wordTimers[_wt]);
  _wordTimers=[];
}

function _unlockAudio(){
  if(_audioReady)return;
  _audioReady=true;
  var b=document.getElementById('boot');
  if(b)b.classList.add('activated');
  // Silent primer — called from gesture context, unlocks speech synthesis
  var primer=new SpeechSynthesisUtterance(' ');
  primer.volume=0;primer.rate=10;
  window.speechSynthesis.speak(primer);
  // Pre-load voices
  _voices=window.speechSynthesis.getVoices();
  if(!_voices.length){
    window.speechSynthesis.onvoiceschanged=function(){
      _voices=window.speechSynthesis.getVoices();
    };
  }
  // Drain any greeting that was queued before the first gesture
  if(_pendingSpeech){
    var pending=_pendingSpeech; _pendingSpeech=null;
    setTimeout(function(){ speakOrdis(pending.text, pending.onDone); }, 80);
  }
}
function _armUnlock(){
  function _onGesture(){
    document.removeEventListener('mousedown',_onGesture);
    document.removeEventListener('keydown',_onGesture);
    document.removeEventListener('touchstart',_onGesture);
    _unlockAudio();
  }
  document.addEventListener('mousedown',_onGesture);
  document.addEventListener('keydown',_onGesture);
  document.addEventListener('touchstart',_onGesture);
}

// ── Visibility-aware polling (P6) ───────────────────────────────────────────
// Skip NETWORK polls while the tab is hidden, and refresh them once when it
// becomes visible again. Drops idle API calls + backend subprocess spawns
// (sectors→posture, movers→yfinance) to zero when Mason is unattended.
var _pollFns=[];
function _poll(fn, ms){
  _pollFns.push(fn);
  return setInterval(function(){ if(!document.hidden) fn(); }, ms);
}
document.addEventListener('visibilitychange',function(){
  if(!document.hidden){ for(var i=0;i<_pollFns.length;i++){ try{ _pollFns[i](); }catch(e){} } }
});

// Wire boot() to DOMContentLoaded HERE, up top, so a throw in any later
// top-level IIFE (crystal, grid, etc.) can never prevent boot from running.
// `boot` is a hoisted function declaration, so this is safe before its body.
window.addEventListener('DOMContentLoaded', boot);

async function boot(){
  _armUnlock(); // arm first — gesture pre-warms audio

  // Phase 1: CSS crystal animation plays automatically (2s).
  // Kick off all background work in parallel during that time.
  initStars(); loadVoices(); updateClock();
  setInterval(updateClock,10000);
  fetchStatus(); _poll(fetchStatus,60000);
  fetchHeadroom(); _poll(fetchHeadroom,30000);
  document.getElementById('vtoggle').classList.toggle('on',voiceOn);
  var vlbl=voiceOn?'Voice: on':'Voice: off';
  document.getElementById('vlabel').textContent=vlbl;
  document.getElementById('vlabel2').textContent=vlbl;

  // Start all data fetches in parallel
  var greetP=fetch('/api/greeting').then(function(r){return r.json();}).catch(function(){return null;});
  fetchNews(); _poll(fetchNews, 120000);  // rolling news scan every 2 min
  fetchPosture();
  fetchHoldingsPanel(); fetchOptionsPanel(); fetchStockTradesWidget();
  _poll(fetchHoldingsPanel,       2*60*1000);   // refresh holdings every 2 min (includes paper)
  _poll(fetchOptionsPanel,        2*60*1000);   // refresh options every 2 min
  _poll(fetchStockTradesWidget,   60*1000);     // refresh trades every 60s (live Alpaca positions)
  fetchSectors(); fetchMovers(true);
  _poll(fetchSectors,             60*1000);     // sectors every 60s (yfinance, no quota)
  _poll(function(){ fetchMovers(false); }, 45*1000); // movers: watchlist 45s; market self-throttles to ~3min

  // Wait for crystal fade-in (min 2s) AND a user click before proceeding.
  // This guarantees Chrome has a user gesture before any speech attempt.
  var _bootClicked=new Promise(function(resolve){
    document.getElementById('boot').addEventListener('click',resolve,{once:true});
    document.addEventListener('keydown',resolve,{once:true});
  });
  await Promise.all([sleep(2000),_bootClicked]);

  // Phase 2: fade out boot screen
  var b=document.getElementById('boot');
  b.classList.add('fade-out');
  await sleep(800);
  b.style.display='none';

  // Phase 3: greeting
  var greetLine1='Good morning.',greetLine2='Mason is ready.',greetPrompt='Shall Mason fill you in?';
  var pendingNote='';
  try{
    var g=await greetP;
    if(g){
      greetLine1=g.line1||greetLine1;
      greetLine2=g.line2||greetLine2;
      greetPrompt=g.prompt||greetPrompt;
      document.getElementById('g-date').textContent=g.date||'';
      if(g.pending_note) pendingNote=g.pending_note;
    }
  }catch(e){}

  typewrite('g-line1',greetLine1,36);
  await sleep(greetLine1.length*36+150);
  typewriteOrdis('g-line2',greetLine2,20);
  await sleep(greetLine2.length*20+300);
  typewrite('g-prompt',pendingNote||greetPrompt,18);

  var fullGreeting=greetLine1+' '+greetLine2+' '+(pendingNote||greetPrompt);
  // The g-line* nodes above are hidden compat DOM in v2, so the greeting is
  // also posted as the first Mason chat bubble — otherwise with voice off it
  // was neither spoken nor visible anywhere.
  try{ addMsg('mason',fullGreeting); }catch(e){}
  if(voiceOn){
    speakOrdis(fullGreeting,function(){ setTimeout(listenForVoice,700); });
  } else {
    // Voice off: don't open the mic on a greeting nobody heard — settle on
    // TAP TO LISTEN so Nico starts the conversation when he wants to.
    _crystalState('idle');
  }
}

function initStars(){
  var c=document.getElementById('bg-canvas');
  var ctx=c.getContext('2d');
  c.width=window.innerWidth;c.height=window.innerHeight;
  var stars=[];
  for(var i=0;i<180;i++){
    stars.push({x:Math.random()*c.width,y:Math.random()*c.height,
      r:Math.random()*1.2+0.2,a:Math.random(),da:(Math.random()-.5)*.008});
  }
  function draw(){
    ctx.clearRect(0,0,c.width,c.height);
    for(var j=0;j<stars.length;j++){
      var s=stars[j];s.a+=s.da;
      if(s.a>1||s.a<0)s.da*=-1;
      ctx.globalAlpha=Math.max(0,Math.min(1,s.a));
      ctx.fillStyle='#c8d8f0';ctx.beginPath();
      ctx.arc(s.x,s.y,s.r,0,Math.PI*2);ctx.fill();
    }
    requestAnimationFrame(draw);
  }
  draw();
  window.addEventListener('resize',function(){c.width=window.innerWidth;c.height=window.innerHeight;});
}

function typewrite(id,text,delay){
  var el=document.getElementById(id);if(!el)return;
  el.textContent='';var i=0;
  var iv=setInterval(function(){el.textContent+=text[i];i++;if(i>=text.length)clearInterval(iv);},delay);
}

// UIFIX2 2026-11-18: quote-safe HTML escaper for every server / broker / news / model string this file
// writes with innerHTML (same output as esc() in templates/mason_v2.html). _escUrl: http(s) links only.
function _escH(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function _escUrl(u){var t=String(u==null?'':u).trim();return /^https?:\/\//i.test(t)?_escH(t):'#';}

function typewriteOrdis(id,text,delay){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML='';var i=0,buf='';
  var iv=setInterval(function(){
    buf+=text[i];
    el.innerHTML=_escH(buf).replace(/\[([^\]]+)\]/g,'<span class="sup">[$1]</span>');
    i++;if(i>=text.length)clearInterval(iv);
  },delay);
}

// initApp() merged into boot()

function _newsItemHtml(it){
  if(typeof it==='string') it={title:it};
  var title=it.title||it.headline||'';
  var url=it.url||'';
  var impact=it.impact||'';
  var dc= impact==='red'?'var(--red)': impact==='green'?'var(--green)': impact==='yellow'?'#ffb224':'var(--blue)';
  var tags='';
  if(it.tickers){for(var i=0;i<it.tickers.length;i++){tags+='<span class="news-tag">'+_escH(it.tickers[i])+'</span>';}}
  var moon= it.overnight?'<span class="news-moon" title="overnight / weekend">&#9789;</span>':'';
  var label=url?'<a href="'+_escUrl(url)+'" target="_blank" rel="noopener" class="news-link">'+_escH(title)+'</a>':_escH(title);
  var time= it.time_str?'<span class="news-time">'+_escH(it.time_str)+'</span>':'';
  var src= it.source?'<span class="news-src">'+_escH(it.source)+'</span>':'';
  var impl= it.implication?'<div class="news-impl">&#8627; '+_escH(it.implication)+'</div>':'';
  return '<div class="news-item" data-cat="'+_escH(it.category||'macro')+'">'
        +'<span class="news-dot" style="color:'+dc+'">&#9670;</span>'
        +time+moon+tags+'<span class="news-head">'+label+'</span>'+src+impl+'</div>';
}

async function fetchNews(){
  var el=document.getElementById('news-list-top');
  if(!el) return;
  try{
    var r=await fetch('/api/news');var d=await r.json();
    var items=d.items||d.headlines||[];
    if(!items.length){
      if(!_seenNewsTitles.size)
        el.innerHTML='<div class="news-item" style="color:var(--muted)">Feed unavailable.</div>';
      return;
    }
    var isFirst=(_seenNewsTitles.size===0);
    if(isFirst){
      // First load — show the 12 newest. Seed the dedup set with the ENTIRE
      // returned batch (not just the 12 shown) so the existing backlog never
      // prepends: the widget grows toward _NEWS_MAX only as LATER scans surface
      // genuinely new events, which arrive newest-first and prepend on top.
      var initial=items.slice(0,_NEWS_INITIAL);
      var h='';
      for(var j=0;j<initial.length;j++) h+=_newsItemHtml(initial[j]);
      el.innerHTML=h;
      for(var s=0;s<items.length;s++)
        _seenNewsTitles.add(typeof items[s]==='string'?items[s]:(items[s].title||''));
    } else {
      // Subsequent scan — prepend items we haven't shown yet so older ones
      // slide down and eventually revolve out.
      var fresh=[];
      for(var i=0;i<items.length;i++){
        var title=typeof items[i]==='string'?items[i]:(items[i].title||'');
        if(!_seenNewsTitles.has(title)){
          _seenNewsTitles.add(title);
          fresh.push(items[i]);
        }
      }
      if(fresh.length){
        var frag='';
        for(var k=0;k<fresh.length;k++) frag+=_newsItemHtml(fresh[k]);
        el.insertAdjacentHTML('afterbegin',frag);
        while(el.children.length>_NEWS_MAX) el.lastElementChild.remove();
      }
    }
  }catch(e){
    if(!_seenNewsTitles.size)
      el.innerHTML='<div class="news-item" style="color:var(--muted)">Unable to load feeds.</div>';
  }
}

async function fetchPosture(){
  try{
    var r=await fetch('/api/posture');var d=await r.json();
    var pt=document.getElementById('posture-text');
    var pb=document.getElementById('posture-badge');
    if(d.error){
      pb.textContent='UNAVAILABLE';
      pb.className='posture-badge';
      pb.style.color='var(--muted)';pb.style.borderColor='var(--muted)';
      pt.textContent='';
    } else {
      var zone=(d.top_risk&&d.top_risk.zone)||'—';
      var score=(d.top_risk&&d.top_risk.score!=null)?d.top_risk.score:'?';
      var regime=(d.sector&&d.sector.regime)||'';
      var phase=(d.sector&&d.sector.cycle_phase)||'';
      var badgeClass={Green:'badge-green',Yellow:'badge-yellow',Orange:'badge-orange',Red:'badge-red',Critical:'badge-critical'}[zone]||'badge-green';
      pb.textContent='RISK: '+zone.toUpperCase()+' ('+score+')';
      pb.className='posture-badge '+badgeClass;
      pb.style.color='';pb.style.borderColor='';
      pt.textContent=regime+(phase?' | '+phase:'');
      pt.style.color='var(--text)';
    }

    // ── Buffett Indicator ────────────────────────────────────
    var br=document.getElementById('buffett-ratio');
    var bb=document.getElementById('buffett-badge');
    var bi=document.getElementById('buffett-icon');
    var bf=d.buffett;
    if(!bf||bf.error){
      br.textContent=bf?('Error: '+bf.error):'Unavailable';
      br.style.color='var(--muted)';
    } else {
      var bzone=bf.zone||'—';
      var bcolor=bf.zone_color||'var(--muted)';
      var bcls={
        'Undervalued':'badge-green',
        'Fair Value':'badge-blue',
        'Overvalued':'badge-yellow',
        'Danger Zone':'badge-orange',
        'Extreme':'badge-red'
      }[bzone]||'badge-yellow';
      br.textContent=bf.ratio+'% — '+bf.verdict;
      br.style.color='var(--text)';
      bb.textContent=bzone.toUpperCase();
      bb.className='posture-badge '+bcls;
      bb.style.display='';
      if(bi)bi.innerHTML='&#9670;';
    }

    // save for brief use
    window._posture=d;
  }catch(e){
    var pb=document.getElementById('posture-badge');
    if(pb){pb.textContent='OFFLINE';pb.className='posture-badge';pb.style.color='var(--muted)';pb.style.borderColor='var(--muted)';}
  }
}

// ── Sectors widget (#43) ──────────────────────────────────────
async function fetchSectors(){
  var el=document.getElementById('sectors-panel-body');
  if(!el)return;
  try{
    var r=await fetch('/api/sectors');var d=await r.json();
    var secs=(d&&d.sectors)||[];
    if(!secs.length){ el.innerHTML='<div class="opts-empty">No sector data.</div>'; return; }
    function chip(s){
      var up=s.day_pct>=0,sign=up?'+':'';
      return '<div class="sec-chip '+(up?'up':'down')+'">'
        +'<span class="sc-name" title="'+_escH(s.etf)+'">'+_escH(s.name)+'</span>'
        +'<span class="sc-pct">'+sign+(s.day_pct).toFixed(2)+'%</span></div>';
    }
    var majors=secs.filter(function(s){return s.group==='major';});
    var subs=secs.filter(function(s){return s.group==='sub';});
    var html='';
    if(majors.length)html+='<div class="sec-group-label">Major sectors</div><div class="sec-grid">'+majors.map(chip).join('')+'</div>';
    if(subs.length)html+='<div class="sec-group-label">Sub-sectors</div><div class="sec-grid">'+subs.map(chip).join('')+'</div>';
    el.innerHTML=html;
  }catch(e){ el.innerHTML='<div class="opts-empty">Sectors unavailable.</div>'; }
}

// ── Movers widget (#36) — Watchlist (fast) + Market (FMP, slow) ─
var _moversMode='watchlist';
var _moversMktLast=0;
function setMoversMode(mode){
  _moversMode=mode;
  var tw=document.getElementById('mv-tab-watch'),tm=document.getElementById('mv-tab-mkt');
  if(tw)tw.classList.toggle('mv-tab-on',mode==='watchlist');
  if(tm)tm.classList.toggle('mv-tab-on',mode==='market');
  fetchMovers(true);
}
function _mvRows(rows,dir){
  if(!rows||!rows.length)return '<div class="opts-empty">—</div>';
  return rows.map(function(r){
    var sign=r.change_pct>=0?'+':'';
    return '<div class="mv-row '+dir+'"><span class="mv-tk">'+_escH(r.ticker)+'</span>'
      +'<span class="mv-pct">'+sign+(r.change_pct).toFixed(2)+'%</span></div>';
  }).join('');
}
function _renderMovers(g,l,note){
  var el=document.getElementById('movers-panel-body'); if(!el)return;
  var html='<div class="mv-cols">'
    +'<div><div class="mv-col-label">Gainers</div>'+_mvRows(g,'up')+'</div>'
    +'<div><div class="mv-col-label">Losers</div>'+_mvRows(l,'down')+'</div></div>';
  if(note)html+='<div class="mv-note">'+_escH(note)+'</div>';
  el.innerHTML=html;
}
async function fetchMovers(force){
  var el=document.getElementById('movers-panel-body'); if(!el)return;
  try{
    if(_moversMode==='market'){
      // yfinance broad-market movers: server caches 60s; client also throttles unless forced.
      var nowm=Date.now();
      if(!force && nowm-_moversMktLast<60000) return;
      _moversMktLast=nowm;
      var r=await fetch('/api/movers?src=market');var d=await r.json();
      var m=(d&&d.market)||{};
      _renderMovers(m.gainers,m.losers, m.error?('Market feed: '+m.error):'Broad market · refreshes ~1 min');
    }else{
      var r2=await fetch('/api/movers?src=watchlist');var d2=await r2.json();
      var w=(d2&&d2.watchlist)||{};
      _renderMovers(w.gainers,w.losers,'Your watchlist · refreshes ~45s');
    }
  }catch(e){ el.innerHTML='<div class="opts-empty">Movers unavailable.</div>'; }
}

async function fetchHoldingsPanel(){
  var el=document.getElementById('holdings-panel-body');
  if(!el)return;
  try{
    var r=await fetch('/api/holdings_panel');
    var d=await r.json();
    if(d.error&&!Object.keys(d.accounts||{}).length){
      el.innerHTML='<div class="opts-empty">'+_escH(d.error)+'</div>';return;
    }
    var html='';
    var accts=d.accounts||{};
    var totals=d.totals||{};
    var order=['Investing','Roth IRA'];
    // sort keys: Investing first, then Roth IRA, then anything else
    var keys=Object.keys(accts).sort(function(a,b){
      return (order.indexOf(a)<0?99:order.indexOf(a))-(order.indexOf(b)<0?99:order.indexOf(b));
    });
    for(var ai=0;ai<keys.length;ai++){
      var acct=keys[ai];
      var positions=accts[acct];
      html+='<div class="holdings-acct-label">'+_escH(acct)+'</div>';
      for(var pi=0;pi<positions.length;pi++){
        var p=positions[pi];
        if(!p.current_price&&!p.current_value)continue;
        var pct=p.gain_loss_pct;
        var pos=pct>=0;
        var sign=pos?'+':'';
        html+='<div class="holding-row">'
          +'<span class="h-ticker">'+_escH(p.ticker)+'</span>'
          +'<span class="h-price">$'+p.current_price.toFixed(2)+'</span>'
          +'<span class="h-gl '+(pos?'pos':'neg')+'">'+sign+pct.toFixed(2)+'%</span>'
          +'</div>';
      }
      var tot=totals[acct]||{};
      if(tot.value){
        var tgl=tot.gl||0;
        var tpos=tgl>=0;
        html+='<div class="holdings-total">'
          +'<span>'+fmtUsd(tot.value)+'</span>'
          +'<span style="color:'+(tpos?'var(--green)':'var(--red)')+';">'+fmtUsdSigned(tgl)+'</span>'
          +'</div>';
      }
    }
    el.innerHTML=html||'<div class="opts-empty">No positions loaded.</div>';
  }catch(e){
    el.innerHTML='<div class="opts-empty">Unable to load holdings.</div>';
  }
}

async function fetchOptionsPanel(){
  var el=document.getElementById('options-panel-body');
  var cnt=document.getElementById('opts-count');
  if(!el)return;
  try{
    var r=await fetch('/api/options/journal');
    var d=await r.json();
    var open=d.open_plays||[];
    var acc=d.accuracy||{};
    if(cnt)cnt.textContent=open.length?open.length+' OPEN':'';
    if(!open.length){
      var msg='<div class="opts-empty">No open plays. [MASON IS WATCHING]</div>';
      if(acc.total_plays){
        msg+='<div class="opts-accuracy"><span>'+_escH(acc.total_plays)+' total</span><span style="color:var(--blue)">'+
          (acc.win_rate!=null?(acc.win_rate*100).toFixed(0)+'% win':'—')+'</span></div>';
      }
      el.innerHTML=msg;return;
    }
    var html='';
    for(var i=0;i<open.length;i++){
      var play=open[i];
      var dir=(play.direction||'').toLowerCase();
      var dirCls=dir==='bullish'?'bull':dir==='bearish'?'bear':'neut';
      var dirLbl=dir==='bullish'?'BULL':dir==='bearish'?'BEAR':'NEUT';
      var strat=(play.strategy||'').replace(/_/g,' ').toUpperCase();
      var exp=play.expiration?play.expiration.slice(5):'';   // MM-DD
      var conf=play.mason_confidence!=null?play.mason_confidence+'%':'?';
      // Sub-row: contracts × premium, max profit, breakeven
      var sub='';
      var ctrs=play.contracts;
      var prem=play.premium;
      var ptype=(play.premium_type||'').toLowerCase();
      var ptypeShort=ptype.includes('debit')?'db':ptype.includes('credit')?'cr':'';
      var mp=play.max_profit; var ml=play.max_loss; var bp=play.breakeven;
      if(ctrs) sub+='<span><span class="opts-sub-num">'+_escH(ctrs)+'x</span></span>';
      if(prem!=null) sub+='<span><span class="opts-sub-num">$'+parseFloat(prem).toFixed(2)+(ptypeShort?' '+ptypeShort:'')+'</span></span>';
      if(bp!=null)   sub+='<span>BP: <span class="opts-sub-num">$'+parseFloat(bp).toFixed(2)+'</span></span>';
      if(mp!=null)   sub+='<span>max: <span class="opts-sub-num" style="color:var(--green)">'+fmtUsd(mp)+'</span></span>';
      if(ml!=null)   sub+='<span>risk: <span class="opts-sub-num" style="color:var(--red)">'+fmtUsd(Math.abs(ml))+'</span></span>';
      html+='<div class="opts-play-block">'
        +'<div class="opts-play-row">'
        +'<span class="opt-ticker">'+_escH(play.ticker)+'</span>'
        +'<span class="opt-strat">'+_escH(strat)+'</span>'
        +'<span class="opt-dir '+dirCls+'">'+dirLbl+'</span>'
        +'<span class="opt-conf">'+_escH(conf)+'</span>'
        +'<span style="font-size:8px;color:var(--muted)">'+_escH(exp)+'</span>'
        +'</div>'
        +(sub?'<div class="opts-play-sub">'+sub+'</div>':'')
        +'</div>';
    }
    if(acc.total_plays){
      html+='<div class="opts-accuracy">'
        +'<span>'+_escH(acc.reviewed_plays)+'/'+_escH(acc.total_plays)+' reviewed</span>'
        +'<span style="color:var(--blue)">'+(acc.win_rate!=null?(acc.win_rate*100).toFixed(0)+'% win':'—')+'</span>'
        +'</div>';
    }
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="opts-empty">Journal unavailable.</div>';
  }
}

async function fetchStockTradesWidget(){
  var el=document.getElementById('trades-panel-body');
  var cnt=document.getElementById('trades-count');
  if(!el)return;
  try{
    var results=await Promise.all([
      fetch('/api/trades/journal').then(function(r){return r.json();}),
      fetch('/api/paper/positions').then(function(r){return r.json();}).catch(function(){return {positions:[],account:{}};})
    ]);
    var d=results[0]; var pd=results[1];
    var open=d.open_trades||[]; var acc=d.accuracy||{};
    var allPos=pd.positions||[]; var pacct=pd.account||{};
    var equityPos=allPos.filter(function(p){return p.asset_class!=='us_option';});
    var optionPos=allPos.filter(function(p){return p.asset_class==='us_option';});
    var total=open.length+allPos.length;
    if(cnt)cnt.textContent=total?total+' OPEN':'';
    var html='';

    // ── ALPACA LIVE POSITIONS ─────────────────────────────────
    if(allPos.length){
      html+='<div class="paper-section-hdr">ALPACA LIVE</div>';
      for(var i=0;i<equityPos.length;i++){
        var p=equityPos[i];
        var pct=(p.unrealized_plpc*100);
        var plSign=p.unrealized_pl>=0?'+':'';
        var plCls=p.unrealized_pl>=0?'pos':'neg';
        html+='<div class="paper-pos-row">'
          +'<span class="opt-ticker">'+_escH(p.symbol)+'</span>'
          +'<span class="paper-qty">'+parseFloat(p.qty).toFixed(0)+'sh</span>'
          +'<span class="paper-val">'+fmtUsd(Math.abs(p.market_value))+'</span>'
          +'<span class="paper-pl '+plCls+'">'+plSign+pct.toFixed(2)+'%</span>'
          +'</div>';
      }
      if(optionPos.length){
        html+='<div class="paper-section-hdr" style="margin-top:4px;border-color:var(--bg4)">OPTIONS</div>';
        for(var i=0;i<optionPos.length;i++){
          var p=optionPos[i];
          var pct=(p.unrealized_plpc*100);
          var plSign=p.unrealized_pl>=0?'+':'';
          var plCls=p.unrealized_pl>=0?'pos':'neg';
          html+='<div class="paper-pos-row">'
            +'<span class="opt-ticker" style="font-size:7px;letter-spacing:.02em">'+_escH(p.symbol)+'</span>'
            +'<span class="paper-qty">'+parseFloat(p.qty).toFixed(0)+'ct</span>'
            +'<span class="paper-val">'+fmtUsd(Math.abs(p.market_value))+'</span>'
            +'<span class="paper-pl '+plCls+'">'+plSign+pct.toFixed(2)+'%</span>'
            +'</div>';
        }
      }
      if(pacct.equity){
        var dayPl=pacct.day_pl||0; var dayPos=dayPl>=0;
        html+='<div class="paper-acct-bar">'
          +'<span>Cash $'+(pacct.cash/1000).toFixed(1)+'k</span>'
          +'<span style="color:'+(dayPos?'var(--green)':'var(--red)')+'">Day '+fmtUsdSigned(dayPl)+'</span>'
          +'<span>Eq $'+(pacct.equity/1000).toFixed(1)+'k</span>'
          +'</div>';
      }
    }

    // ── MASON JOURNAL ─────────────────────────────────────────
    if(open.length){
      html+='<div class="paper-section-hdr" style="margin-top:'+(allPos.length?'6px':'0')+'">JOURNAL</div>';
      for(var i=0;i<open.length;i++){
        var t=open[i];
        var dir=(t.direction||'long').toUpperCase();
        var dirCls=dir==='LONG'?'bull':'bear';
        var setup=(t.setup_type||'').replace(/_/g,' ').toUpperCase();
        var conf=t.mason_confidence!=null?t.mason_confidence+'%':'?';
        var rr=t.risk_reward_ratio?t.risk_reward_ratio+'R':'?';
        html+='<div class="opts-play-row">'
          +'<span class="opt-ticker">'+_escH(t.ticker)+'</span>'
          +'<span class="opt-dir '+dirCls+'">'+_escH(dir)+'</span>'
          +'<span class="opt-strat">'+_escH(setup)+'</span>'
          +'<span class="opt-conf">'+_escH(conf)+'</span>'
          +'<span style="font-size:8px;color:var(--muted);margin-left:2px">'+_escH(rr)+'</span>'
          +'</div>';
      }
    }

    if(!allPos.length&&!open.length){
      html='<div class="opts-empty">No open positions. [MASON IS WATCHING]</div>';
    }
    if(acc.closed_trades){
      html+='<div class="opts-accuracy">'
        +'<span>'+_escH(acc.closed_trades)+' closed</span>'
        +'<span style="color:var(--blue)">'+(acc.win_rate!=null?(acc.win_rate*100).toFixed(0)+'% win':'—')+'</span>'
        +'</div>';
    }
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="opts-empty">Trades unavailable.</div>';
  }
}

var _chatEverOpened=false;

function _updateChatToggleBtn(){
  var btn=document.getElementById('chat-toggle-btn');
  if(!btn)return;
  btn.style.display='inline-flex';
  var open=document.body.classList.contains('chat-open');
  btn.innerHTML=open?'&#9664; CHAT':'&#9656; CHAT';
  btn.title=open?'Hide chat':'Show chat';
  btn.style.color=open?'var(--blue)':'var(--muted)';
  btn.style.borderColor=open?'var(--blue-dim)':'var(--bg3)';
}

function toggleChatPanel(){
  if(document.body.classList.contains('chat-open')) goHome();
  else enterChat();
}

function enterChat(){
  // Don't kill speech just because the chat panel opened. Both speech paths
  // (_ssStart / speakOrdis) cancel prior speech on their own, so the cancel
  // here is only cleanup — skip it while Mason is actively speaking.
  var _speaking = (window.speechSynthesis&&window.speechSynthesis.speaking) || (_SS&&_SS.active);
  if(!_speaking){
    _stopSynthKA();
    window.speechSynthesis.cancel();
    _pendingSpeech=null;
    _crystalState('idle');
  }
  stopListening();
  _chatEverOpened=true;
  document.body.classList.add('chat-open');
  _updateChatToggleBtn();
  if(window._gridOnChatOpen) window._gridOnChatOpen();
  setTimeout(function(){
    var inp=document.getElementById('text-input');
    if(inp)inp.focus();
    var cs=document.getElementById('chat-screen');
    if(cs&&(window.innerWidth<720||window.innerHeight>window.innerWidth)){
      setTimeout(function(){cs.scrollIntoView({behavior:'smooth'});},100);
    }
  },380);
}

function goHome(){
  // Mirror enterChat: don't kill speech just because we returned home.
  var _speaking = (window.speechSynthesis&&window.speechSynthesis.speaking) || (_SS&&_SS.active);
  if(!_speaking){
    _stopSynthKA();
    window.speechSynthesis.cancel();
    _pendingSpeech=null;
    _crystalState('idle');
  }
  stopListening();
  document.body.classList.remove('chat-open');
  if(window._gridOnChatClose) window._gridOnChatClose();
  _updateChatToggleBtn();
  if(window.innerWidth<720||window.innerHeight>window.innerWidth){
    var hs=document.getElementById('home-screen');
    if(hs)setTimeout(function(){hs.scrollIntoView({behavior:'smooth'});},100);
  }
  // Do NOT auto-start listening when returning to home — user can tap the crystal if they want.
}

async function fetchStatus(){
  try{
    var r=await fetch('/api/status');var s=await r.json();
    var status=s.status||'closed';
    var isOpen=(status==='open');
    // home header pill
    var pill=document.getElementById('market-pill');
    if(pill){
      pill.className='market-pill '+status;
      var label=status==='open'?'OPEN':status==='pre-market'?'PRE-MARKET':status==='after-hours'?'AFTER-HOURS':'CLOSED';
      pill.textContent=label;
    }
    // chat header (still uses dot + label)
    document.getElementById('chat-market-dot').className='dot '+(isOpen?'open':'closed');
    document.getElementById('chat-market-label').textContent=
      status==='open'?'Market open':status==='pre-market'?'Pre-market':status==='after-hours'?'After-hours':'Market closed';
  }catch(e){
    var pill=document.getElementById('market-pill');
    if(pill){pill.className='market-pill closed';pill.textContent='OFFLINE';}
  }
}

function _hrTok(n){n=+n||0;
  if(n>=1e9)return (n/1e9).toFixed(2)+'B';
  if(n>=1e6)return (n/1e6).toFixed(2)+'M';
  if(n>=1e3)return (n/1e3).toFixed(1)+'K';
  return ''+n;}
async function fetchHeadroom(){
  var el=document.getElementById('hr-chip'); if(!el)return;
  try{
    var d=await (await fetch('/api/headroom')).json();
    if(d&&d.available&&(d.tokens_saved>0||d.requests>0)){
      // Headline = $ actually saved THIS SESSION (native cache discount + any
      // compression), not raw compression tokens. Mason runs Headroom in cache
      // mode on purpose (byte-exact, no rewrite risk for a bot placing real
      // trades) — so raw compression tokens sit near 0 forever by design; the
      // $ figure is the number that's real, moving, and worth watching.
      var sessUsd=d.session_total_usd||0;
      el.textContent='⚡ $'+sessUsd.toFixed(2)+' saved';
      el.title='This session: $'+sessUsd.toFixed(2)+' saved across '+(d.session_requests||0)+' calls (cache mode).'
        +' Lifetime: $'+(d.lifetime_total_usd||0).toFixed(2)+' across '+(d.requests||0)+' calls, plus '
        +(d.tool_search_tokens||0).toLocaleString()+' tool-schema tokens kept out of context.'
        +' Raw compression tokens: '+(d.tokens_saved||0).toLocaleString()+' lifetime — near 0 is expected in cache mode.';
      el.style.display='inline-flex';
    } else { el.style.display='none'; }
  }catch(e){ el.style.display='none'; }
}

function updateClock(){
  var n=new Date();
  var t=String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0');
  document.getElementById('clock').textContent=t;
  document.getElementById('chat-clock').textContent=t;
}

// ── Sticky-bottom scroll (2026-07-21) ───────────────────────────────────────
// Only auto-scroll while the reader is already parked at the bottom. If Nico has
// scrolled up to reread something, Mason's streaming tokens must NOT yank the view
// back down. A scroll listener flips _stickBottom off the moment he scrolls away
// from the bottom, and back on when he returns.
var _stickBottom=true;
function _atBottom(el){ return (el.scrollHeight-el.scrollTop-el.clientHeight)<60; }
function _maybeScroll(el){ if(_stickBottom) el.scrollTop=el.scrollHeight; }
(function _initStick(){
  var m=document.getElementById('messages');
  if(m) m.addEventListener('scroll',function(){ _stickBottom=_atBottom(m); },{passive:true});
})();

// Attach a small "reread" (replay) control to a Mason bubble. Clicking it
// re-speaks that specific message via speakOrdis — works regardless of the
// voice on/off toggle, since the click is an explicit request to hear it again.
// The text is read from el._masonText, which streaming updates as tokens land.
function _attachReread(el,bub){
  var btn=document.createElement('button');
  btn.className='reread-btn';btn.type='button';
  btn.title='Reread this message aloud';
  btn.innerHTML='&#128266;'; // 🔊
  btn.onclick=function(ev){
    ev.stopPropagation();
    var t=el._masonText||bub.textContent||'';
    if(!t.trim())return;
    try{ _crystalState('speaking'); }catch(_e){}
    speakOrdis(t,null);
  };
  bub.appendChild(btn);
}

// 2026-09-04 addition: the system prompt says "zero markdown, ever" but a
// live reply from the local qwen3.5:4b model still came back full of
// **bold**, bullet, and numbered-list syntax (reported via screenshot) — a
// small local model doesn't reliably hold that rule on long, data-heavy
// replies no matter how the prompt is worded. The bubble renderer never
// interpreted markdown anyway (no bold conversion, no lists), so it was
// rendering the raw asterisks/markers as literal text. This strips the
// common offenders deterministically so the UI is clean regardless of
// whether the model complies — applied to MASON's text only, never to what
// Nico actually typed.
// Money display helpers — sign ALWAYS before the $, cents ALWAYS shown
// (Nico, 2026-09-21, reversing the earlier "round the dashboard" call).
//   fmtUsd       — a plain amount ($436.16 / -$60.00)
//   fmtUsdSigned — a delta / P&L that must carry an explicit +/- (+$60.00 / -$60.00)
function fmtUsd(v,dec){
  var n=parseFloat(v); if(!isFinite(n))n=0;
  if(dec==null)dec=2;
  return (n<0?'-':'')+'$'+Math.abs(n).toFixed(dec);
}
function fmtUsdSigned(v,dec){
  var n=parseFloat(v); if(!isFinite(n))n=0;
  if(dec==null)dec=2;
  return (n<0?'-':'+')+'$'+Math.abs(n).toFixed(dec);
}

function _cleanMarkdown(text){
  return String(text||'')
    .replace(/\$\s*-/g,'-$').replace(/\$\s*\+/g,'+$')  // sign before $: "$-60"→"-$60"
    .replace(/\*\*\*(.+?)\*\*\*/g,'$1')   // ***bold italic***
    .replace(/\*\*(.+?)\*\*/g,'$1')       // **bold**
    .replace(/__(.+?)__/g,'$1')           // __bold__
    .replace(/^ {0,3}#{1,6}\s+/gm,'')     // # headers
    .replace(/^\s*[-*+]\s+/gm,'')         // - / * / + bullet markers
    .replace(/^\s*\d+[.)]\s+/gm,'')       // 1. / 1) numbered markers
    .replace(/`([^`\n]+)`/g,'$1');        // `inline code`
}

function addMsg(role,text){
  if(thinking&&role==='mason'){thinking.remove();thinking=null;}
  var msgs=document.getElementById('messages');
  // A message Nico just sent should always pull the view to the bottom.
  if(role==='user') _stickBottom=true;
  var el=document.createElement('div');el.className='msg '+role;
  var av=document.createElement('div');av.className='msg-avatar';
  av.textContent=role==='mason'?'◆':'N';
  var bub=document.createElement('div');bub.className='msg-bubble';
  var shown=role==='mason'?_cleanMarkdown(text):text;
  bub.innerHTML=shown.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\[([^\]]+)\]/g,'<span class="sup">[$1]</span>').replace(/\n/g,'<br>');
  el.appendChild(av);el.appendChild(bub);msgs.appendChild(el);
  if(role==='mason'){ el._masonText=text; _attachReread(el,bub); }
  _maybeScroll(msgs);return el;
}

function addThinking(){
  var msgs=document.getElementById('messages');
  var el=document.createElement('div');el.className='msg mason';
  el.innerHTML='<div class="msg-avatar">◆</div><div class="msg-bubble"><div class="thinking"><div class="thinking-dots"><span></span><span></span><span></span></div><div class="thinking-model"></div></div></div>';
  msgs.appendChild(el);_maybeScroll(msgs);thinking=el;
  _flowStage='sending…';_flowModel='';_flowHR=false;_renderFlow();
}
// Live status-flow under the thinking dots: sending -> selecting model -> <model>
// -> running <tools> -> generating, plus a compression bolt when Headroom is in the
// path. Rendered into the existing .thinking-model line.
var _flowStage='',_flowModel='',_flowHR=false;
function _renderFlow(){
  if(!thinking)return;
  var el=thinking.querySelector('.thinking-model'); if(!el)return;
  var bits=[]; if(_flowStage)bits.push(_flowStage);
  if(_flowModel)bits.push('Model Selected: '+_flowModel);
  el.textContent=bits.join(' · ')+(_flowHR?' ⚡':'');
  el.classList.add('visible');
}
function _flowLabel(code){
  if(code==='hr'){ _flowHR=true; return null; }
  if(code.indexOf('tool:')===0) return 'running '+code.slice(5).split(',').join(', ')+'…';
  var m={sending:'sending…',routing:'selecting model…',generating:'generating…',factcheck:'fact checking…'};
  return m[code]||code;
}
function setThinkingStage(code){
  var lbl=_flowLabel(code);
  if(lbl!==null)_flowStage=lbl;
  _renderFlow();
}
// LLM rewire: show the CONCRETE model that generated the turn (e.g. "qwen3.5:4b",
// "phi4-mini", "claude-opus-5-5"), reported by the gateway on the tier event. The
// tier code still frames it: 'local' is the qwen brain (everyday default), 'opus'
// is deep analysis (must be clearly visible per spec §4.4), 'phi' is the degraded
// availability-fallback state. The fallback map only covers the rare case where no
// model name came through (e.g. the zero-LLM direct-answer path).
var _MODEL_FALLBACK={local:'qwen3.5:4b',opus:'claude-opus-5-5',phi:'phi4-mini'};
function _modelLabel(tier,name){
  var base=(name&&String(name).trim())?String(name).trim():(_MODEL_FALLBACK[tier]||tier||'');
  if(tier==='pick') return base+' · selected';
  if(tier==='opus') return base+' · deep analysis';
  if(tier==='phi')  return base+' · degraded';
  return base;
}
// ── Chat model picker (2026-11-12) ──────────────────────────────────────────
// A model dropdown + effort dropdown (React Bits GlideSelect) and a Tools switch
// (React Bits SquishSwitch) above the chat input. 'auto' = today's routing (qwen,
// Opus on deep escalation) and sends nothing extra. The model list and each
// model's tool permission come from /api/chat_models (mason.config.json
// llm.chat_models); the server re-checks every value, so this is only the UI.
var _chatPick={model:'auto',effort:'',tools:true};
try{
  var _cpSaved=JSON.parse(localStorage.getItem('mason.chatPick')||'null');
  if(_cpSaved&&typeof _cpSaved==='object'){
    if(typeof _cpSaved.model==='string')_chatPick.model=_cpSaved.model;
    if(typeof _cpSaved.effort==='string')_chatPick.effort=_cpSaved.effort;
    if(typeof _cpSaved.tools==='boolean')_chatPick.tools=_cpSaved.tools;
  }
}catch(e){}
var _chatModels=[],_chatEfforts=[],_pickMounts=null;
function _pickEntry(){
  for(var i=0;i<_chatModels.length;i++){ if(_chatModels[i].id===_chatPick.model) return _chatModels[i]; }
  return null;
}
function _pickBody(text){
  var b={text:text},m=_pickEntry();
  if(m){
    b.model=m.id;
    if(m.effort&&_chatPick.effort) b.effort=_chatPick.effort;
    b.tools=!!(m.tools&&_chatPick.tools);
  }
  return b;
}
function _savePick(){ try{localStorage.setItem('mason.chatPick',JSON.stringify(_chatPick));}catch(e){} }
function _renderPick(){
  if(!_pickMounts) return;
  var m=_pickEntry(),auto=!m;
  var colors={accentColor:'var(--color-accent-400)',surfaceColor:'var(--color-neutral-800)',
    highlightColor:'var(--color-neutral-700)',textColor:'var(--color-text)'};
  var modelOpts=[{value:'auto',label:'Auto',tag:'qwen → opus'}].concat(_chatModels.map(function(x){
    return {value:x.id,label:x.label,tag:(x.provider==='ollama'?'local':'api')+(x.tools?'':' · no tools')};
  }));
  _pickMounts.model.update(Object.assign({options:modelOpts,value:_chatPick.model,size:'sm',placement:'top',
    menuWidth:230,ariaLabel:'Chat model',
    onChange:function(v){ _chatPick.model=v; _savePick(); _renderPick(); }},colors));
  var effOpts=[{value:'',label:'Effort: default'}].concat(_chatEfforts.map(function(e){ return {value:e,label:'Effort: '+e}; }));
  var effOn=!!(m&&m.effort);
  _pickMounts.effort.update(Object.assign({options:effOpts,value:effOn?_chatPick.effort:'',size:'sm',placement:'top',
    menuWidth:170,disabled:!effOn,ariaLabel:'Reasoning effort',
    onChange:function(v){ _chatPick.effort=v; _savePick(); _renderPick(); }},colors));
  var canTools=!!(m&&m.tools);
  _pickMounts.tools.update({checked:auto?true:(canTools&&_chatPick.tools),disabled:!canTools,
    width:38,height:20,radius:10,ariaLabel:'Allow this model to call tools',
    trackColor:'var(--color-neutral-700)',trackOnColor:'var(--color-accent-500)',
    thumbColor:'var(--color-neutral-300)',thumbOnColor:'#ffffff',
    onChange:function(v){ _chatPick.tools=!!v; _savePick(); _renderPick(); }});
  // No visible label (it wrapped the row on narrow panels) — a hover hint only.
  var tip=document.getElementById('chat-pick-tools');
  if(tip) tip.title=auto?'Tools: Auto':(canTools?'Tools On/Off':'Tools Off');
}
function _initChatPick(){
  var bar=document.getElementById('chat-pick-bar'),W=window.MasonUI;
  if(!bar||!W||!W.ready||typeof W.ready.then!=='function') return;
  fetch('/api/chat_models').then(function(r){ return r.json(); }).then(function(d){
    _chatModels=(d&&d.models)||[]; _chatEfforts=(d&&d.efforts)||[];
    if(!_pickEntry()) _chatPick.model='auto';
    return W.ready;
  }).then(function(){
    var M=window.MasonUI;
    if(!M||typeof M.mount!=='function') return;
    _pickMounts={
      model:M.mount(document.getElementById('chat-pick-model'),'GlideSelect',{}),
      effort:M.mount(document.getElementById('chat-pick-effort'),'GlideSelect',{}),
      tools:M.mount(document.getElementById('chat-pick-tools'),'SquishSwitch',{})
    };
    bar.style.display='flex'; bar.style.flexWrap='nowrap';
    var oldLab=document.getElementById('chat-pick-tools-label');   // pre-restart template
    if(oldLab&&oldLab.parentNode) oldLab.parentNode.removeChild(oldLab);
    _renderPick();
  }).catch(function(e){
    // Picker unavailable -> chat still works, on Auto.
    _chatModels=[]; _chatPick.model='auto';
    try{ console.warn('[chat-pick] model picker unavailable, using Auto:',e); }catch(_){}
  });
}
window.addEventListener('DOMContentLoaded',_initChatPick);

function setThinkingModel(tier,name){
  lastModelTier=_modelLabel(tier,name); _flowModel=lastModelTier;
  // 'selecting model…' has now resolved — collapse it into 'Model Selected: X' so the
  // flow reads sending… → selecting model… → Model Selected: <model>, not both at once.
  if(_flowStage==='selecting model…')_flowStage='';
  _renderFlow();
}

// Post-reply fact-check badge (Path B). Attaches to the finished reply bubble and
// resolves in place: 'checking' → 'clear' (subtle ✓) or 'flagged' (⚠ N unverified,
// click to expand the note). Advisory only — it never edits the reply text, and the
// note is VISUAL-ONLY (never routed through the speaker). See mason_factcheck.audit_reply.
var _FC_REASON={
  invented_ticker:'no such ticker',
  uncited_ticker:'ticker not in the source',
  diff_contradicted:'contradicts the source',
  diff_not_in_source:'not found in the source',
  price_out_of_band:'price looks off',
  bad_number:'number looks off'
};
function _fcReason(code){var k=String(code||'').split(':')[0];return _FC_REASON[k]||(code?String(code).replace(/_/g,' '):'unverified');}
function _esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;}
function setFactcheck(bub,fc){
  if(!bub||!fc)return;
  var badge=bub.querySelector('.msg-factcheck');
  if(!badge){badge=document.createElement('div');badge.className='msg-factcheck';bub.appendChild(badge);}
  var status=fc.status||'error';
  badge.className='msg-factcheck fc-'+status;
  if(status==='checking'){
    badge.innerHTML='<span class="fc-dot"></span>Fact Checking…';
    return;
  }
  if(status==='clear'){
    badge.innerHTML='<i class="ph ph-seal-check"></i> Fact-checked'+
      (fc.checked?(' · '+_escH(fc.checked)+' claim'+(fc.checked===1?'':'s')):'');
    return;
  }
  if(status==='flagged'){
    var items=fc.items||[];
    var head='<i class="ph ph-warning"></i> '+items.length+' unverified · '+
      _escH(fc.checked||items.length)+' checked';
    var note='';
    for(var i=0;i<items.length;i++){
      var it=items[i]||{};
      note+='<div class="fc-item"><span class="fc-why">'+_esc(_fcReason(it.reason))+'</span>'+
            _esc((it.text||'').trim())+'</div>';
    }
    badge.innerHTML='<button type="button" class="fc-head">'+head+
      ' <i class="ph ph-caret-down"></i></button><div class="fc-note" hidden>'+note+'</div>';
    var hb=badge.querySelector('.fc-head'),nb=badge.querySelector('.fc-note');
    if(hb&&nb)hb.addEventListener('click',function(){nb.hidden=!nb.hidden;badge.classList.toggle('open',!nb.hidden);});
    return;
  }
  // error / unknown — stay quiet but honest.
  badge.innerHTML='<i class="ph ph-warning-circle"></i> fact-check unavailable';
}

function showProgress(){document.getElementById('progress-wrap').classList.add('visible');polling=true;}
function hideProgress(){
  document.getElementById('progress-wrap').classList.remove('visible');
  document.getElementById('progress-fill').style.width='0%';
  document.getElementById('progress-pct').textContent='0%';
  polling=false;
}

async function pollBrief(){
  if(!polling)return;
  try{
    var r=await fetch('/api/brief/status');var s=await r.json();
    document.getElementById('progress-fill').style.width=s.progress+'%';
    document.getElementById('progress-pct').textContent=s.progress+'%';
    document.getElementById('progress-step').textContent=s.step;
    if(s.progress>=100&&!s.running){
      hideProgress();
      if(thinking){thinking.remove();thinking=null;}
      var resp=s.result||'Mason encountered an issue generating the brief.';
      addMsg('mason',resp);
      _lastBriefReport=resp;   // stored so "read it out loud" can speak the full report
      _lastMasonMsg=resp;
      // Speak only the short summary — user reads the full report
      var spoken=s.spoken_summary||'Brief compiled. Full report is above. [MASON RECOMMENDS REVIEWING IT]';
      if(voiceOn) speakOrdis(spoken,function(){ if(_asksQuestion(spoken))setTimeout(listenForVoice,800); });
      else if(_asksQuestion(spoken)) setTimeout(listenForVoice,800);
    }else{setTimeout(pollBrief,500);}
  }catch(e){setTimeout(pollBrief,800);}
}

async function sendQuery(text,_forceNoRearm){
  if(!text||!text.trim())return;
  stopListening();
  // New turn — tear down any speech still running from the PREVIOUS turn (a
  // greeting, a read-aloud, a brief summary). Without this, barge-in over
  // speakOrdis()'s manually-chained utterances lets the old chain's onend
  // handlers keep firing — resuming the dead utterances and resetting the
  // crystal to 'idle' out from under the reply that's now speaking, which
  // strands the badge on TAP TO LISTEN while Mason is audibly talking.
  _haltPriorSpeech();
  // Shutdown intercept — handle before anything else
  if(_isShutdownPhrase(text)){ shutdownMason(); return; }
  // Read-aloud intercept — Nico asking Mason to speak the full report/last reply.
  // Handled client-side so it never hits the API. Reads the brief if we have one,
  // otherwise Mason's most recent message.
  if(_isReadAloud(text)){
    var _toRead=_lastBriefReport||_lastMasonMsg;
    addMsg('user',text);
    if(!_toRead){
      var _none="Nothing to read back yet — run a brief or ask me something first.";
      addMsg('mason',_none);
      if(voiceOn) speakOrdis(_none,function(){ setTimeout(listenForVoice,800); });
      return;
    }
    addMsg('mason','[MASON IS READING THE REPORT]');
    _crystalState('speaking');
    speakOrdis(_toRead,function(){ if(!_forceNoRearm&&!_isClosing(text)&&_asksQuestion(_toRead))setTimeout(listenForVoice,800); });
    return;
  }
  // Recall a finished background analysis (Section B) — client-side, no API turn.
  // Guarded so "what did you find" only intercepts when an analysis actually exists.
  if(_PULL_ANALYSIS_RE.test(text)&&_pendingAnalyses.some(function(a){return a.status==='done'||a.status==='error';})){
    _pullUpLatestAnalysis(text); return;
  }
  // Heavy analysis/research → run in the background so the conversation stays live.
  if(_ANALYSIS_INTENT_RE.test(text)){ _startAnalysisJob(text); return; }
  // If closing phrase, don't re-arm listening after Mason replies
  var _rearm = !_forceNoRearm && !_isClosing(text);
  _crystalState('thinking');
  if(!document.body.classList.contains('chat-open'))enterChat();
  addMsg('user',text);addThinking();

  try{
    // ── Streaming path ──────────────────────────────────────────
    _queryCancelled=false; // fresh turn — clear any stale cancel flag from a prior stop
    _queryAbort=new AbortController();
    var resp=await fetch('/api/query_stream',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(_pickBody(text)),
      signal:_queryAbort.signal});

    // Keep thinking dots visible until first content chunk arrives.
    // fullText = everything Mason says this turn (all bubbles) for speech/read-aloud;
    // bubbleText = just the current bubble, so a follow-through reply renders separately.
    var msgEl=null,bub=null,fullText='',bubbleText='';

    // #18 — incremental speech. Start reading the moment the FIRST full sentence
    // is ready, sentence-by-sentence, while the rest of the reply still generates.
    // Fallback: if no sentence boundary lands within ~1.5s, start speaking anyway.
    var _spokenLen=0,_fallbackTimer=null,_ssStarted=false;
    function _drainSpeak(force){
      if(!voiceOn)return;
      if(!_ssStarted){ _ssStart(); _ssStarted=true; }
      var pending=fullText.slice(_spokenLen);
      if(!pending)return;
      var b=_ssBoundary(pending);
      if(b>0){ _ssFeed(pending.slice(0,b)); _spokenLen+=b; }
      else if(force){ _ssFeed(pending); _spokenLen+=pending.length; }
    }

    // ── Batched bubble render (P5) ──────────────────────────────────────────
    // Streamed chunks arrive per-token. Rebuilding bub.innerHTML + forcing a
    // scroll reflow on EVERY token is O(n^2) DOM work and layout thrash. Instead
    // we accumulate the text (cheap string ops) and repaint at most once per
    // animation frame. _flushRender() forces a synchronous paint before we tag a
    // bubble or finish, so a pending frame can't wipe an appended tier label.
    var _msgsEl=document.getElementById('messages');
    var _renderScheduled=false,_rafId=0;
    function _renderBubble(){
      _renderScheduled=false;_rafId=0;
      if(!bub)return;
      bub.innerHTML=_cleanMarkdown(bubbleText)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\[([^\]]+)\]/g,'<span class="sup">[$1]</span>')
        .replace(/\n/g,'<br>');
      _maybeScroll(_msgsEl);
    }
    function _scheduleRender(){
      if(_renderScheduled)return;
      _renderScheduled=true;_rafId=requestAnimationFrame(_renderBubble);
    }
    function _flushRender(){
      if(_renderScheduled){ if(_rafId)cancelAnimationFrame(_rafId); _renderBubble(); }
    }

    var reader=resp.body.getReader();
    _activeReader=reader;
    var decoder=new TextDecoder();
    var buf='';

    while(true){
      var _r=await reader.read();
      // Stop Processing was clicked while this read was in flight — a proxy/buffering
      // race can let already-in-transit data resolve here even after abort(). Bail
      // out hard: don't render, don't speak, don't re-arm listening on stale data.
      // (2026-07-19 bugfix — was letting the original answer land ~seconds late.)
      if(_queryCancelled){ try{reader.cancel();}catch(_ce){} _activeReader=null; if(thinking){thinking.remove();thinking=null;} return; }
      if(_r.done)break;
      buf+=decoder.decode(_r.value,{stream:true});
      var parts=buf.split('\n\n');
      buf=parts.pop(); // incomplete last chunk
      for(var i=0;i<parts.length;i++){
        var line=parts[i].trim();
        if(!line.startsWith('data:'))continue;
        var payload=line.slice(5).trim();
        try{
          var ev=JSON.parse(payload);
          if(ev.model_tier){
            setThinkingModel(ev.model_tier, ev.model_name);
            continue;
          }
          if(ev.stage){
            setThinkingStage(ev.stage);
            continue;
          }
          if(ev.factcheck){
            // Live fact-check (Path B). Arrives AFTER the reply has streamed, so it
            // renders as a badge on the finished reply bubble, not in the flow line.
            setFactcheck(bub, ev.factcheck);
            continue;
          }
          if(ev.tools_called){
            lastToolsCalled=lastToolsCalled.concat(ev.tools_called);
            continue;
          }
          if(ev.intent==='brief_started'){
            if(thinking){thinking.remove();thinking=null;}
            if(msgEl) msgEl.remove();
            var briefMsg='Mason is compiling your brief now. [MASON IS ON IT]';
            addMsg('mason',briefMsg);
            if(voiceOn) speakOrdis(briefMsg,null);
            showProgress();setTimeout(pollBrief,600);
            reader.cancel();return;
          }
          if(ev.new_bubble){
            // Fires both when Mason follows through on an unfulfilled promise
            // (a fresh pass) AND, as of the 2026-07-19 bugfix, between tool
            // rounds within the same pass whenever a round said something out
            // loud before calling its tool. Close out the current bubble
            // (tag its model tier) and start a fresh one for whatever's next.
            _flushRender();  // paint the final text before tagging + detaching
            // Bugfix (2026-07-23): force-drain any unspoken tail NOW, same as
            // ev.done does below. Without this, a bubble that ends on a
            // bracketed aside (no terminator after the closing ']' — see
            // _ssBoundary, which never treats bracket contents as a sentence
            // end) sat unspoken until the NEXT bubble's text supplied a real
            // boundary — so the aside was heard late, stitched onto the next
            // section's opening line instead of speaking with its own bubble.
            if(voiceOn)_drainSpeak(true);
            if(msgEl)msgEl._masonText=bubbleText; // finalize reread text for this bubble
            if(bub&&lastModelTier){
              var ml2=document.createElement('div');ml2.className='msg-model';
              ml2.textContent=lastModelTier;bub.appendChild(ml2);
            }
            if(bub&&lastToolsCalled.length){
              var tl2=document.createElement('div');tl2.className='msg-tools';
              tl2.textContent='tools: '+lastToolsCalled.join(', ');bub.appendChild(tl2);
            }
            lastToolsCalled=[];
            // _renderBubble overwrote bub.innerHTML during streaming, wiping the
            // reread button addMsg attached — re-attach it now the bubble is final.
            if(msgEl&&bub)_attachReread(msgEl,bub);
            msgEl=null;bub=null;bubbleText='';
            // Bugfix (2026-07-19): show a processing indicator for the gap
            // between this bubble ending and the next one's first token —
            // without this, the badge/thinking-dots stayed silent through the
            // whole tool-execution gap with no sign of progress being made.
            if(thinking){thinking.remove();thinking=null;}
            addThinking();
            if(lastModelTier)_flowModel=lastModelTier; // addThinking() resets this — restore it, the model hasn't changed mid-turn
            continue;
          }
          if(ev.chunk){
            if(!msgEl){
              // First chunk: remove thinking dots, create real bubble
              if(thinking){thinking.remove();thinking=null;}
              msgEl=addMsg('mason','');
              bub=msgEl.querySelector('.msg-bubble');
            }
            fullText+=ev.chunk;
            bubbleText+=ev.chunk;
            // Batch the DOM repaint to one animation frame (see _scheduleRender).
            _scheduleRender();
            // Arm the ~5s speech delay on first content, then drain completed
            // sentences to the speaker as they finish generating.
            if(voiceOn){
              // Start speaking as soon as the first full sentence is ready;
              // arm a ~1.5s fallback so a long unpunctuated opener doesn't stall.
              if(!_fallbackTimer) _fallbackTimer=setTimeout(function(){ _drainSpeak(true); },1500);
              _drainSpeak();
            }
          }
          if(ev.done){
            _queryAbort=null;
            _flushRender();  // ensure the final token is painted before tagging
            if(bub&&lastModelTier){
              var ml=document.createElement('div');ml.className='msg-model';
              ml.textContent=lastModelTier;bub.appendChild(ml);
            }
            if(bub&&lastToolsCalled.length){
              var tl=document.createElement('div');tl.className='msg-tools';
              tl.textContent='tools: '+lastToolsCalled.join(', ');bub.appendChild(tl);
            }
            lastToolsCalled=[];
            _lastMasonMsg=fullText;   // for "read it out loud"
            if(msgEl)msgEl._masonText=bubbleText; // reread button reads this bubble's text
            if(msgEl&&bub)_attachReread(msgEl,bub); // re-attach after streaming overwrote innerHTML
            if(_fallbackTimer){ clearTimeout(_fallbackTimer); _fallbackTimer=null; }
            if(voiceOn){
              // Flush every remaining sentence (and any tail without a terminator).
              _drainSpeak(true);
              if(!_ssStarted){ _ssStart(); _ssStarted=true; }
              _ssEnd(function(){ _crystalState('idle'); if(_rearm&&_asksQuestion(fullText))setTimeout(listenForVoice,800); });
            }else{
              _crystalState('idle');
              if(_rearm&&_asksQuestion(fullText)) setTimeout(listenForVoice,800);
            }
          }
        }catch(ex){}
      }
    }
    // Stream ended — clean up thinking if no content was ever received
    if(thinking){thinking.remove();thinking=null;}
    _queryAbort=null;
    _activeReader=null;
  }catch(e){
    _queryAbort=null;
    _activeReader=null;
    if(thinking){thinking.remove();thinking=null;}
    // AbortError = user cancelled — don't show an error message
    if(e.name==='AbortError'){_crystalState('idle');_updateChatStatus('idle');return;}
    addMsg('mason','Connection error: '+e.message);
    if(_rearm) setTimeout(listenForVoice,2000);
  }
}

// Enter sends; Shift+Enter drops a newline (textarea handles the insert natively).
function _inputKeydown(e){
  if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendFromInput(); }
}
// Auto-grow the textarea to fit its content, capped at exactly 7 text lines. The
// cap is computed from the LIVE computed line-height (not a hardcoded px) so it
// stays correct regardless of font metrics. box-sizing:border-box → scrollHeight
// covers content+padding; we add the borders to get the visible height, and only
// switch on the (thin) scrollbar once we're actually clamped.
function _inputMaxH(t){
  var cs=getComputedStyle(t);
  var lh=parseFloat(cs.lineHeight);
  if(!lh||isNaN(lh)) lh=parseFloat(cs.fontSize)*1.5;
  return lh*7
       +parseFloat(cs.paddingTop)+parseFloat(cs.paddingBottom)
       +parseFloat(cs.borderTopWidth)+parseFloat(cs.borderBottomWidth);
}
function _autoGrowInput(t){
  var cs=getComputedStyle(t);
  var bord=parseFloat(cs.borderTopWidth)+parseFloat(cs.borderBottomWidth);
  var maxH=_inputMaxH(t);
  t.style.height='auto';
  var need=t.scrollHeight+bord;          // full height the content wants
  t.style.height=Math.min(need,maxH)+'px';
  t.style.overflowY=(need>maxH)?'auto':'hidden';
}
// Collapse the textarea back to a single row after a send/clear.
function _resetInputHeight(){
  var t=document.getElementById('text-input');
  if(t){ t.style.height='auto'; t.style.overflowY='hidden'; }
}

function sendFromInput(){
  var inp=document.getElementById('text-input');
  var val=inp.value.trim();
  // If there's a pending file upload, send that instead
  if(_pendingFile){
    var prompt=val||'Analyze this for me.';
    inp.value='';_resetInputHeight();
    sendFileQuery(_pendingFile,prompt);
    clearUpload();
    return;
  }
  if(!val)return;
  inp.value='';_resetInputHeight();
  sendQuery(val);
}

function handleFileUpload(input){
  var file=input.files[0];
  if(!file)return;
  _pendingFile=file;
  var prev=document.getElementById('upload-preview');
  var fname=document.getElementById('upload-filename');
  prev.style.display='flex';
  fname.textContent='Attached: '+file.name+' ('+Math.round(file.size/1024)+'KB) — type a question or press Send';
  document.getElementById('text-input').focus();
  // Reset input so same file can be re-selected
  input.value='';
}

function clearUpload(){
  _pendingFile=null;
  document.getElementById('upload-preview').style.display='none';
  document.getElementById('upload-filename').textContent='';
}

async function sendFileQuery(file,prompt){
  stopListening();
  _crystalState('thinking');
  if(!document.body.classList.contains('chat-open'))enterChat();
  addMsg('user','['+file.name+'] '+prompt);
  addThinking();
  try{
    var fd=new FormData();
    fd.append('file',file);
    fd.append('prompt',prompt);
    var r=await fetch('/api/upload_query',{method:'POST',body:fd});
    var d=await r.json();
    if(thinking){thinking.remove();thinking=null;}
    var resp=d.response||'(no response)';
    var uploadEl=addMsg('mason',resp);
    if(d.model_tier&&uploadEl){
      var uml=document.createElement('div');uml.className='msg-model';
      uml.textContent=_modelLabel(d.model_tier,d.model_name);uploadEl.querySelector('.msg-bubble').appendChild(uml);
    }
    if(voiceOn) speakOrdis(resp,function(){ if(_asksQuestion(resp))setTimeout(listenForVoice,800); });
    else if(_asksQuestion(resp)) setTimeout(listenForVoice,800);
  }catch(e){
    if(thinking){thinking.remove();thinking=null;}
    addMsg('mason','Upload error: '+e.message);
    setTimeout(listenForVoice,2000);
  }
}

function toggleMic(){
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){addMsg('mason','Voice input requires Chrome. [MASON APOLOGIZES]');return;}
  if(micActive){stopMic();}else{startMic(SR);}
}

function startMic(SR){
  recog=new SR();recog.lang='en-US';recog.continuous=false;recog.interimResults=false;
  recog.onstart=function(){micActive=true;
    document.getElementById('mic-btn').classList.add('active');
    _updateChatStatus('listening');};
  recog.onresult=function(e){
    document.getElementById('text-input').value=e.results[0][0].transcript;
    stopMic();setTimeout(sendFromInput,150);};
  recog.onerror=function(){stopMic();};recog.onend=function(){stopMic();};recog.start();
}

function stopMic(){
  micActive=false;
  document.getElementById('mic-btn').classList.remove('active');
  _updateChatStatus('idle');
  if(recog){try{recog.stop();}catch(ex){}recog=null;}
}

function loadVoices(){
  _voices=window.speechSynthesis.getVoices();
  if(!_voices.length)window.speechSynthesis.onvoiceschanged=function(){_voices=window.speechSynthesis.getVoices();};
}

function toggleVoice(){
  voiceOn=!voiceOn;
  localStorage.setItem('mason_voice',voiceOn?'on':'off');
  document.getElementById('vtoggle').classList.toggle('on',voiceOn);
  var lbl=voiceOn?'Voice: on':'Voice: off';
  document.getElementById('vlabel').textContent=lbl;
  document.getElementById('vlabel2').textContent=lbl;
  if(voiceOn){
    if(!_voices.length)_voices=window.speechSynthesis.getVoices();
    speakOrdis('Mason online.',function(){ setTimeout(listenForVoice,600); });
  } else {
    stopListening();
    // Bugfix (2026-07-23): a raw cancel() here doesn't actually silence Mason —
    // see stopSpeak()'s 2026-07-19 comment. cancel() fires onend on the utterance
    // being killed, and BOTH speech paths (the streaming _SS queue in _ssPump, and
    // speakOrdis's own onend-chain) auto-advance to the NEXT already-queued sentence
    // from inside that onend handler unless _SS is reset and _speechStopRequested is
    // set first — which only stopSpeak() does. That's why toggling voice off mid-
    // speech spoke the start of the next section instead of going silent (Nico's
    // report, 2026-07-23). stopSpeak() also covers _crystalState('idle') itself.
    stopSpeak();
  }
}

// ── Web Audio context for corruption effect ───────────────────
var _audioCtx=null;
var _distNode=null,_filterNode=null,_gainNode=null,_dryGain=null,_wetGain=null;

function _ensureAudio(){
  if(_audioCtx)return;
  try{
    _audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    // WaveShaper distortion
    _distNode=_audioCtx.createWaveShaper();
    var curve=new Float32Array(256);
    for(var i=0;i<256;i++){var x=i*2/256-1;curve[i]=x<0?-1+Math.abs(x)*0.35:1-x*0.35;}
    _distNode.curve=curve;_distNode.oversample='4x';
    // Low-pass filter — muffle highs
    _filterNode=_audioCtx.createBiquadFilter();
    _filterNode.type='lowpass';_filterNode.frequency.value=900;_filterNode.Q.value=2;
    // Dry/wet gains — blend clean signal with corrupted
    _dryGain=_audioCtx.createGain();_dryGain.gain.value=0.55;
    _wetGain=_audioCtx.createGain();_wetGain.gain.value=0.6;
    _gainNode=_audioCtx.createGain();_gainNode.gain.value=1.0;
    // Routing: dest <- gainNode <- dryGain + wetGain <- filter <- dist <- source
    _distNode.connect(_filterNode);
    _filterNode.connect(_wetGain);
    _wetGain.connect(_gainNode);
    _dryGain.connect(_gainNode);
    _gainNode.connect(_audioCtx.destination);
  }catch(e){_audioCtx=null;}
}

// ══════════════════════════════════════════════════════════════════════════════
// ASYNC ANALYSIS JOBS (Section B) — kickoff, status chip, completion chime, recall
// ══════════════════════════════════════════════════════════════════════════════
// Heavy analysis/research runs in the background so the conversation never blocks.
// Nico gets an immediate in-character ack, a live status chip, a soft chime when the
// job lands, and can pull the result up at any later point in the session.
var _pendingAnalyses=[];        // {id,title,status,result,spoken,announced,reviewed}
var _analysisPollTimer=null;
var _analysisCalloutQueue=[];   // job ids whose spoken callout is waiting for a gap

// Utterances that should run in the background instead of blocking the chat.
// NARROW BY DESIGN (2026-07-16): only explicit heavy-analysis phrasing routes here.
// Removed over-broad triggers ("break it down", "give me plays", "analysis on X",
// "potential plays", "screen for", "historic drop", "provide me analysis") — they were
// false-firing the 8-round / 4096-max_tokens background job (mason_app.py:869-918) and
// burning credits. Those asks now use the normal cheap chat path, which can still call
// hunt_setups / the screener tools itself. Do NOT re-widen without a cost review.
var _ANALYSIS_INTENT_RE=/\b(deep[\s-]?dive|scenario analysis|full (analysis|report|breakdown|write[\s-]?up|rundown)|comprehensive (analysis|report|breakdown)|in[\s-]?depth (analysis|look|report)|research (report|this|into)|put together .*(analysis|report|research)|run (a |the )?(full analysis|deep dive|scenario|screener)|thorough (analysis|breakdown)|do (a |some )?(deep )?(research|analysis) on|dig (deep|all the way) (in|into)|analyze .* in[\s-]?depth)\b/i;
// Recall an already-finished background analysis.
var _PULL_ANALYSIS_RE=/\b(pull up (that |the )?(analysis|research|report)|what did you find|what'?d you find|show me (that |the )?(analysis|research|report)|(that|the) analysis (you|from)|(that|the) research (you|from)|go over (that |the )?(analysis|research)|circle back)\b/i;

// Soft two-note chime (<500ms), clean tone straight to the destination (bypasses the
// Ordis distortion chain). Fires on completion no matter what TTS is doing.
function _playReadyChime(){
  if(!_audioCtx){ try{_ensureAudio();}catch(e){} }
  var ctx=_audioCtx; if(!ctx)return;
  try{
    if(ctx.state==='suspended'){ try{ctx.resume();}catch(e){} }
    var t0=ctx.currentTime, notes=[[659.25,0.0],[987.77,0.13]]; // E5 -> B5
    for(var i=0;i<notes.length;i++){
      var o=ctx.createOscillator(), g=ctx.createGain();
      o.type='sine'; o.frequency.value=notes[i][0];
      var st=t0+notes[i][1];
      g.gain.setValueAtTime(0.0001,st);
      g.gain.exponentialRampToValueAtTime(0.16,st+0.03);
      g.gain.exponentialRampToValueAtTime(0.0001,st+0.30);
      o.connect(g); g.connect(ctx.destination);
      o.start(st); o.stop(st+0.34);
    }
  }catch(e){}
}

// ── status chip ───────────────────────────────────────────────────────────────
function _showAnalysisChip(state,label,jobId){
  var chip=document.getElementById('analysis-chip'); if(!chip)return;
  chip.className=state||'';
  chip.setAttribute('data-job',jobId||'');
  var txt=chip.querySelector('.ac-text'); if(txt)txt.textContent=label||'';
  chip.style.display='flex';
}
function _hideAnalysisChip(){ var c=document.getElementById('analysis-chip'); if(c)c.style.display='none'; }
function _chipClick(){
  var chip=document.getElementById('analysis-chip'); if(!chip)return;
  var jid=chip.getAttribute('data-job');
  if(chip.classList.contains('ready')&&jid){ _hideAnalysisChip(); _reviewAnalysis(jid); }
}

// ── kickoff ─────────────────────────────────────────────────────────────────
async function _startAnalysisJob(text){
  if(!document.body.classList.contains('chat-open'))enterChat();
  addMsg('user',text);
  try{
    var r=await fetch('/api/analysis/start',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:text})});
    var d=await r.json();
    if(!d||!d.ok){ addMsg('mason','Could not start that one — try again in a sec.'); return; }
    var ack=d.ack||"On it — pulling that together in the background. Keep talking, I'll flag it when it lands.";
    addMsg('mason',ack);
    _pendingAnalyses.push({id:d.id,title:'',status:'running',result:'',spoken:'',announced:false,reviewed:false});
    _showAnalysisChip('running','Analyzing…',d.id);
    if(voiceOn){ _crystalState('speaking'); speakOrdis(ack,function(){ setTimeout(listenForVoice,600); }); }
    else setTimeout(listenForVoice,300);
    _startAnalysisPoll();
  }catch(e){ addMsg('mason','Could not start that one — network hiccup.'); }
}

function _startAnalysisPoll(){
  if(_analysisPollTimer)return;
  _analysisPollTimer=setInterval(_pollAnalyses,1500);
}
async function _pollAnalyses(){
  var running=_pendingAnalyses.filter(function(a){return a.status==='running';});
  if(!running.length){ if(_analysisPollTimer){clearInterval(_analysisPollTimer);_analysisPollTimer=null;} return; }
  for(var i=0;i<running.length;i++){
    var a=running[i];
    try{
      var r=await fetch('/api/analysis/status?id='+encodeURIComponent(a.id));
      var d=await r.json();
      if(!d||!d.ok||!d.job)continue;
      var j=d.job;
      if(j.status==='done'||j.status==='error'){
        a.status=j.status; a.title=j.title||a.title;
        a.result=j.result_text||''; a.spoken=j.spoken||a.result;
        _onAnalysisDone(a);
      } else {
        _showAnalysisChip('running','Analyzing… '+(j.progress||0)+'%',a.id);
      }
    }catch(e){}
  }
}
function _onAnalysisDone(a){
  _playReadyChime();
  _showAnalysisChip('ready',(a.title? a.title.slice(0,26) : 'Analysis')+' — ready',a.id);
  if(_analysisCalloutQueue.indexOf(a.id)<0)_analysisCalloutQueue.push(a.id);
  _maybeSpeakCallout();
}

// Turn-aware spoken callout: only speaks in a real gap — Mason not speaking, Nico not
// mid-utterance, nothing processing. Otherwise it stays queued and is retried on the
// next transition to idle (hooked in _crystalState). The chime already fired.
function _maybeSpeakCallout(){
  if(!_analysisCalloutQueue.length)return;
  var masonSpeaking=(window.speechSynthesis&&window.speechSynthesis.speaking)||(_SS&&_SS.active);
  var nicoSpeaking=_autoListening;
  var thinking=document.getElementById('thinking');
  if(masonSpeaking||nicoSpeaking||thinking)return;
  var jid=_analysisCalloutQueue.shift();
  var hit=_pendingAnalyses.filter(function(x){return x.id===jid;});
  var a=hit[0]; if(!a||a.announced||a.reviewed)return;
  var line=(a.status==='error')
    ? "That analysis hit a snag — want me to run it again?"
    : "That analysis just came back — want to go over it now, or keep going?";
  a.announced=true;
  addMsg('mason',line);
  if(voiceOn){ _crystalState('speaking'); speakOrdis(line,function(){ setTimeout(listenForVoice,700); }); }
}

// ── recall ───────────────────────────────────────────────────────────────────
async function _reviewAnalysis(jid){
  var hit=_pendingAnalyses.filter(function(x){return x.id===jid;});
  var a=hit[0];
  try{
    var r=await fetch('/api/analysis/get?id='+encodeURIComponent(jid));
    var d=await r.json();
    if(d&&d.ok&&d.job){
      if(a){ a.reviewed=true; a.announced=true; a.result=d.job.result_text||a.result; a.spoken=d.job.spoken||a.spoken; }
      addMsg('mason',d.job.result_text||'(the analysis came back empty)');
      if(voiceOn){ _crystalState('speaking'); speakOrdis(d.job.spoken||d.job.result_text||'',function(){ setTimeout(listenForVoice,700); }); }
      return true;
    }
  }catch(e){}
  if(a&&a.result){ addMsg('mason',a.result); if(voiceOn){ _crystalState('speaking'); speakOrdis(a.spoken||a.result,null); } return true; }
  return false;
}
function _pullUpLatestAnalysis(text){
  if(!document.body.classList.contains('chat-open'))enterChat();
  addMsg('user',text);
  var done=_pendingAnalyses.filter(function(a){return a.status==='done'||a.status==='error';});
  if(!done.length){
    var none="Nothing's finished cooking yet — no analysis to pull up.";
    addMsg('mason',none); if(voiceOn){ _crystalState('speaking'); speakOrdis(none,function(){setTimeout(listenForVoice,700);}); } return;
  }
  _hideAnalysisChip();
  _reviewAnalysis(done[done.length-1].id);
}

function _pickVoice(){
  if(!_voices.length)_voices=window.speechSynthesis.getVoices();
  var preferred=['Google UK English Male','Google US English','Google UK English Female'];
  for(var p=0;p<preferred.length;p++){
    for(var v=0;v<_voices.length;v++){
      if(_voices[v].name===preferred[p])return _voices[v];
    }
  }
  // fallback: first English voice
  for(var v=0;v<_voices.length;v++){
    if(_voices[v].lang&&_voices[v].lang.startsWith('en'))return _voices[v];
  }
  return _voices[0]||null;
}

function _stripMd(text){
  // Strip markdown so TTS never reads asterisks or formatting symbols.
  // Kept in parity with _cleanMarkdown() above (2026-09-04) — that fix was
  // added because qwen3.5:4b doesn't reliably avoid markdown despite the
  // system prompt forbidding it, but only the VISUAL bubble stripper got the
  // wider coverage (indented headers, "1)" / "+" list markers, ***bold
  // italic***). The speech path was left on the old, narrower set. Any
  // asterisk this still misses is caught belt-and-braces by
  // _humanizeForSpeech() in static/mason_speech.js.
  return text
    .replace(/\*\*\*(.+?)\*\*\*/g,'$1')
    .replace(/\*\*([^*]+)\*\*/g,'$1')
    .replace(/\*([^*]+)\*/g,'$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm,'')
    .replace(/^\s*\d+[.)]\s+/gm,'')
    .replace(/^\s*[-*+]\s+/gm,'')
    .replace(/`([^`]+)`/g,'$1')
    .replace(/_{1,2}([^_]+)_{1,2}/g,'$1');
}

function stopProcessing(){
  // Two independent kill switches (2026-07-19 bugfix): abort() alone was racing
  // with in-flight/buffered data (proxy in the middle) and letting the original
  // answer land seconds later anyway. _queryCancelled is checked in sendQuery's
  // read loop on every iteration; reader.cancel() severs the stream directly too.
  _queryCancelled=true;
  if(_queryAbort){_queryAbort.abort();_queryAbort=null;}
  if(_activeReader){ try{_activeReader.cancel();}catch(e){} _activeReader=null; }
  _crystalState('idle');
  _updateChatStatus('idle');
  // Remove any thinking dots left behind
  var th=document.getElementById('thinking');
  if(th)th.remove();
}

function stopSpeak(){
  // Bugfix (2026-07-19): cancel() alone doesn't stop Mason — BOTH speech paths
  // manually chain to the next queued sentence from inside each utterance's
  // onend handler, and cancel() still fires onend for the utterance being killed.
  // So without these resets, killing utterance N just auto-started utterance N+1.
  _speechStopRequested=true;                    // speakOrdis's onend-chain checks this
  _SS.active=false; _SS.playing=false; _SS.q=[]; _SS.done=false; _SS.onDone=null; // _ssPump's own guard then blocks continuation
  _stopSynthKA();
  _clearWordTimers();
  _crystalState('idle');
  try {
    // Chrome/Windows bug: cancel() sometimes silently fails on first call
    window.speechSynthesis.pause();
    window.speechSynthesis.cancel();
    setTimeout(function(){ window.speechSynthesis.cancel(); },60);
  } catch(e){}
}

// Turn-transition cleanup: fully neutralize speech left running from a PRIOR turn
// before a new one starts. Same teardown as stopSpeak() minus the user-facing
// bits (no pause(), no _crystalState('idle') — the caller sets the next state).
// _speechStopRequested short-circuits speakOrdis()'s chained AND terminal onend
// handlers (see speakOrdis), so a cancel()-fired onend from the old chain can't
// resume the dead utterances or reset the crystal; the next speakOrdis()/_ssStart()
// clears the flag again for the fresh utterance.
function _haltPriorSpeech(){
  _speechStopRequested=true;
  _SS.active=false; _SS.playing=false; _SS.q=[]; _SS.done=false; _SS.onDone=null;
  _stopSynthKA();
  _clearWordTimers();
  try{ window.speechSynthesis.cancel(); }catch(e){}
}

// ─── Speech number humanizer ────────────────────────────────────────────────
// MOVED 2026-08-03 → static/mason_speech.js (loaded by the script-src tag above).
// _dollarSpeak() and _humanizeForSpeech() now live there as pure, testable
// functions (node test_mason_speech.js). They are attached to `window` by that
// file, so every call site below keeps working unqualified — do NOT redefine them
// here, that would shadow the fixed versions and reintroduce the "down down" bug.
// Also defined there: _expandTickers, _speechSegments, _speechLatencyMark,
// _speechLatencyReport, TICKER_NAMES.
// See docs/active/SPEECH_MANNERISM_DESIGN.md for the full rationale.
// ────────────────────────────────────────────────────────────────────────────

// ── Streaming speaker (#18) ────────────────────────────────────────────
// Speaks a reply sentence-by-sentence AS it streams in, instead of waiting for
// the whole response. Append-only queue: new sentences are pushed while earlier
// ones are still playing, so we never cancel mid-sentence. Reuses the same
// dual-voice + crystal-pulse logic as speakOrdis.
function _mcSyl(word){
  word=word.toLowerCase().replace(/[^a-z]/g,'');
  if(!word)return 0;
  if(word.length<=3)return 1;
  word=word.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/,'');
  word=word.replace(/^y/,'');
  var m=word.match(/[aeiouy]{1,2}/g);
  return m?Math.max(1,m.length):1;
}
function _mcSegSyl(text){
  var w=text.trim().split(/\s+/).filter(Boolean),t=0;
  for(var i=0;i<w.length;i++)t+=_mcSyl(w[i]);
  return t||1;
}
var _SS={q:[],playing:false,done:false,onDone:null,active:false};
// BUG 3 FIX (2026-08-03): segmentation + coalescing moved to _speechSegments() in
// static/mason_speech.js. Previously this built one utterance per sentence AND per
// bracket transition; adjacent same-type segments are now merged into a single
// utterance, so each bracket/non-bracket TRANSITION produces at most one queue
// boundary instead of one per sentence. Fewer boundaries = fewer unreliable
// onstart events = less accumulated lag before the glitch aside starts.
// The syllable/word-pulse logic below is unchanged — it just operates on longer
// per-utterance text now.
function _ssBuild(text){
  var MS_N=220,MS_B=340;
  var segs=_speechSegments(text);
  var utts=[];
  for(var i=0;i<segs.length;i++){
    var isB=segs[i].isBracket, raw=segs[i].text;
    if(!raw)continue;
    var u=new SpeechSynthesisUtterance(isB?raw.toLowerCase():raw);
    var v=_pickVoice(isB); if(v)u.voice=v;
    if(isB){u.pitch=0.25;u.rate=0.82;u.volume=0.7;}else{u.pitch=1.0;u.rate=1.22;u.volume=1.0;}
    var sc=_mcSegSyl(raw),mps=isB?MS_B:MS_N;
    (function(uu,c,mp,bk){
      uu.onstart=function(){ _audioReady=true;
        _speechLatencyReport(uu);   // Item 8 — queue→onstart delta
        for(var s=0;s<c;s++){(function(idx){ _wordTimers.push(setTimeout(function(){_wordPulse(bk);},idx*mp+80)); })(s);}
      };
    })(u,sc,mps,isB);
    utts.push(u);
  }
  return utts;
}
function _ssStart(){
  _speechStopRequested=false; // fresh speech session — clear any stale stop flag
  try{window.speechSynthesis.cancel();}catch(e){}
  _clearWordTimers();
  _SS={q:[],playing:false,done:false,onDone:null,active:true};
  _crystalState('speaking');
  _startSynthKA();
}
function _ssFeed(text){
  if(!_SS.active||!text)return;
  text=_humanizeForSpeech(_stripMd(text));
  var utts=_ssBuild(text);
  for(var i=0;i<utts.length;i++)_SS.q.push(utts[i]);
  _ssPump();
}
function _ssPump(){
  if(!_SS.active||_SS.playing)return;
  if(!_SS.q.length){
    if(_SS.done){ _SS.active=false; _stopSynthKA(); _clearWordTimers(); _crystalState('idle');
      var cb=_SS.onDone; _SS.onDone=null; if(cb)cb(); }
    else {
      // Bugfix (2026-07-19): queue is empty but the turn isn't done — Mason is still
      // generating (e.g. mid tool-call round on a heavy question) and has simply gone
      // quiet for now. Without this, the badge sat frozen on SPEAKING with total
      // silence, which reads exactly like a stall. Reflect what's actually happening.
      _crystalState('thinking');
    }
    return;
  }
  _crystalState('speaking'); // about to actually vocalize again — pull the badge back from 'thinking' if it drifted there
  var u=_SS.q.shift();
  _SS.playing=true;
  u.onend=function(){ _SS.playing=false; if(window._MC)window._MC.resetBounce(); _ssPump(); };
  u.onerror=function(){ _SS.playing=false; _ssPump(); };
  _speechLatencyMark(u,'stream');   // Item 8 — stamp queue time just before speak()
  try{ window.speechSynthesis.speak(u); }catch(e){ _SS.playing=false; _ssPump(); }
}
function _ssEnd(onDone){ _SS.onDone=onDone; _SS.done=true; _ssPump(); }
// Index just past the last COMPLETE sentence in s (terminator at bracket depth 0,
// followed by whitespace/end). Returns 0 if no complete sentence yet.
function _ssBoundary(s){
  var depth=0,last=0;
  for(var i=0;i<s.length;i++){
    var c=s[i];
    if(c==='[')depth++;
    else if(c===']'){ if(depth>0)depth--; }
    else if((c==='.'||c==='!'||c==='?')&&depth===0){
      var n=s[i+1];
      if(n===undefined||/\s/.test(n)){
        var j=i+1; while(j<s.length&&/\s/.test(s[j]))j++;
        last=j;
      }
    }
  }
  return last;
}

function speakOrdis(text,onDone){
  if(!text)return;
  _speechStopRequested=false; // fresh speech session — clear any stale stop flag
  text=_stripMd(text);
  text=_humanizeForSpeech(text);
  _stopSynthKA();
  window.speechSynthesis.pause();
  window.speechSynthesis.cancel();
  _clearWordTimers();
  _crystalState('speaking');

  // Syllable counter — fires a pulse per syllable, not per word.
  // More natural: "interesting" (4 syl) pulses 4x, "a" (1 syl) pulses once.
  function _syllables(word){
    word=word.toLowerCase().replace(/[^a-z]/g,'');
    if(!word)return 0;
    if(word.length<=3)return 1;
    word=word.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/,'');
    word=word.replace(/^y/,'');
    var m=word.match(/[aeiouy]{1,2}/g);
    return m?Math.max(1,m.length):1;
  }
  function _segSyllables(text){
    var words=text.trim().split(/\s+/).filter(Boolean);
    var total=0;
    for(var i=0;i<words.length;i++) total+=_syllables(words[i]);
    return total||1;
  }

  // BUG 3 FIX (2026-08-03): sentence-splitting, [BRACKET] splitting AND the
  // protection of punctuation inside [...] (see the note this comment replaces —
  // "[MASON NOTES. ANOTHER POINT.]" must not be cut mid-bracket, and that guard is
  // load-bearing) all moved into _speechSegments() in static/mason_speech.js, which
  // additionally COALESCES adjacent same-type segments into one utterance. Each
  // bracket/non-bracket transition still yields exactly one boundary; consecutive
  // same-type sentences no longer each spawn their own queue entry.
  var MS_SYL_NORMAL=220, MS_SYL_BRACKET=340;
  var _segs=_speechSegments(text);
  var utts=[];
  var isFirstUtt=true;

  for(var si=0;si<_segs.length;si++){
    var isBracket=_segs[si].isBracket, rawSeg=_segs[si].text;
    if(!rawSeg) continue;
    var u=new SpeechSynthesisUtterance(isBracket?rawSeg.toLowerCase():rawSeg);
    var v=_pickVoice(isBracket);
    if(v)u.voice=v;
    if(isBracket){ u.pitch=0.25; u.rate=0.82; u.volume=0.7; }
    else { u.pitch=1.0; u.rate=1.22; u.volume=1.0; }
    var sylCount=_segSyllables(rawSeg);
    var msPerSyl=isBracket?MS_SYL_BRACKET:MS_SYL_NORMAL;
    var markFirst=isFirstUtt; isFirstUtt=false;
    (function(uu,sc,mps,brak,mf){
      uu.onstart=function(){
        if(mf) _audioReady=true;
        _speechLatencyReport(uu);   // Item 8 — queue→onstart delta
        for(var s=0;s<sc;s++){
          (function(idx){
            _wordTimers.push(setTimeout(function(){ _wordPulse(brak); },idx*mps+80));
          })(s);
        }
      };
    })(u,sylCount,msPerSyl,isBracket,markFirst);
    // Snap bounce to rest at the end of every utterance except the last, so the
    // pause between utterances stays visible. Previously this was attached at each
    // SENTENCE end; with coalescing an utterance IS the unit of pause, so this is
    // the same intent expressed against the new boundaries.
    if(si < _segs.length-1){
      (function(uu){
        uu.onend=function(){ if(window._MC) window._MC.resetBounce(); };
      })(u);
    }
    utts.push(u);
  }

  // Final utterance: cleanup + callback
  if(utts.length){
    (function(uu){
      uu.onend=function(){
        // cancel() from stopSpeak()/_haltPriorSpeech() also fires THIS handler. If
        // the chain was killed, the caller (or the new turn) owns the crystal state
        // and re-arm now — don't reset to 'idle' or fire onDone's listenForVoice,
        // that's what stranded the badge on TAP TO LISTEN mid-reply.
        if(_speechStopRequested)return;
        _stopSynthKA(); _clearWordTimers(); _crystalState('idle'); if(onDone)onDone();
      };
    })(utts[utts.length-1]);

    // Chrome bug: bulk-queued utterances don't reliably apply pitch/rate changes
    // between utterances (normal→bracket or bracket→normal transitions break).
    // Fix: chain utterances sequentially via onend so each one starts only after
    // the previous fully completes — Chrome resets synthesis state cleanly each time.
    for(var j=0;j<utts.length-1;j++){
      (function(idx){
        var _orig=utts[idx].onend; // preserve resetBounce if already set
        utts[idx].onend=function(){
          if(_orig)_orig();
          // Bugfix (2026-07-19): cancel() from stopSpeak() still fires this onend —
          // without this check, killing the current sentence just auto-started the next.
          if(_speechStopRequested)return;
          _speechLatencyMark(utts[idx+1],'chain');   // Item 8 — stamp queue time
          window.speechSynthesis.speak(utts[idx+1]);
        };
      })(j);
    }
    // Small delay on first speak to let cancel() from line 1047 fully settle
    setTimeout(function(){
      if(_speechStopRequested)return; // stopSpeak() fired during this 55ms window
      _speechLatencyMark(utts[0],'first');   // Item 8 — stamp queue time
      window.speechSynthesis.speak(utts[0]); _startSynthKA();
    },55);
  }

  // Detect Chrome autoplay block (cold first visit, no prior gesture)
  if(!_audioReady){
    setTimeout(function(){
      if(!window.speechSynthesis.speaking&&!window.speechSynthesis.pending){
        window.speechSynthesis.cancel();
        _clearWordTimers();
        _crystalState('idle');
        _pendingSpeech={text:text,onDone:onDone||null};
        var hc=document.getElementById('mason-crystal');
        if(hc)hc.classList.add('hint');
      }
    },400);
  }
}

// ── Voice loop (auto-listen) ─────────────────────────────────
var _silenceTimer=null;       // fires after a true silence gap
var _listenBuf='';            // accumulates final results during grace window
var _SILENCE_GRACE_MS=2800;   // endpointing gap — extended by ANY speech (incl. interim)
var _CLAUSE_EXTEND_MS=800;    // extra grace when the sentence sounds unfinished (C.3)
// Trailing tokens that mean the thought isn't done. When the most recent words end
// on one of these (a conjunction, article, preposition, dangling verb, filler), we
// stretch the silence grace so Mason won't cut in on a natural mid-sentence pause
// like "so I was thinking... about the RKLB position". Cheap suffix check, not NLP.
var _INCOMPLETE_SUFFIX_RE=/(?:^|\s)(and|but|or|so|the|a|an|to|of|for|with|about|that|this|these|those|is|are|was|were|be|been|being|i|we|my|your|our|if|when|then|than|because|while|at|on|in|into|from|as|it'?s|i'?m|i'?ll|we'?re|gonna|wanna|like|just|really|actually|basically|maybe|thinking|looking|around|near|over|under|between)\s*$/i;

// Brief red inline message in both status badges, then restore idle after ~2.2s
// (spec 2026-07-19: no-speech-timeout / mic-permission-denied feedback).
function _flashStatusError(msg){
  var txt=document.getElementById('chat-status-text');
  var el=document.getElementById('chat-status');
  var st=document.getElementById('mason-state');
  if(txt)txt.textContent=msg;
  // Bugfix (2026-07-19): use the .error CSS class, not an inline style. Inline
  // el.style.color was never cleared by _updateChatStatus() (it only manages
  // className), so it permanently pinned #chat-status red even after the state
  // machine moved back to idle/purple. className is fully reassigned here, and
  // gets fully reassigned again by _crystalState('idle') below — no residue.
  if(el)el.className='error';
  if(st){ st.innerHTML=msg; st.style.color='var(--red)'; }
  setTimeout(function(){ _crystalState('idle'); },2200);
}

function _showListeningIndicator(on){
  // Show/hide the STOP LISTENING button beneath #mason-state
  var slb=document.getElementById('stop-listen-btn');
  if(slb)slb.classList.toggle('visible',on);
}

// Bugfix (2026-07-19): the browser's continuous SpeechRecognition session can end
// itself mid-dictation (a real Chrome quirk — "continuous" is a hint, not a
// guarantee) or throw a transient error (no-speech/network/aborted) while you're
// still actively talking. Previously that either sent a half-finished sentence
// immediately or wiped what you'd said so far. Instead: restart cleanly and carry
// the in-progress buffer + on-screen text forward, so it reads as a brief hiccup
// rather than getting cut off.
function _resumeListeningWithBuffer(delay){
  var _savedBuf=_listenBuf;
  setTimeout(function(){
    listenForVoice(); // resets _listenBuf='' internally — restore right after
    if(_savedBuf){
      _listenBuf=_savedBuf;
      var _ri=document.getElementById('text-input'); if(_ri){ _ri.value=_savedBuf.trim(); _autoGrowInput(_ri); }
    }
  },delay||200);
}

function listenForVoice(){
  if(!window.SpeechRecognition&&!window.webkitSpeechRecognition)return;
  if(_autoListening)return;
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  var rec=new SR();
  rec.lang='en-US';
  rec.interimResults=true;
  rec.maxAlternatives=1;
  rec.continuous=true;
  _shouldRearm=true;
  _listenBuf='';

  rec.onstart=function(){
    _autoListening=true;
    _showListeningIndicator(true);
    _crystalState('listening');
    // 5-second no-speech timeout — if nobody talks, stop listening
    if(_noSpeechTimer)clearTimeout(_noSpeechTimer);
    _noSpeechTimer=setTimeout(function(){
      if(_autoListening&&_listenBuf.trim()===''){
        _shouldRearm=false;  // don't re-arm, just drop back to idle
        rec.stop();
        _showListeningIndicator(false);
        _crystalState('idle');
        _flashStatusError('NO SPEECH DETECTED');
      }
    },5000);
  };

  rec.onresult=function(e){
    // Got speech — cancel the no-speech timeout
    if(_noSpeechTimer){clearTimeout(_noSpeechTimer);_noSpeechTimer=null;}

    var finalChunk='',sawInterim=false,interimTail='';
    for(var i=e.resultIndex;i<e.results.length;i++){
      if(e.results[i].isFinal){
        var conf=e.results[i][0].confidence;
        var tr=e.results[i][0].transcript.trim();
        // Reject very low-confidence or very short results (background noise)
        if(conf<0.35||tr.length<2) continue;
        finalChunk+=tr+' ';
      } else if(e.results[i][0].transcript.trim().length>0){
        // Interim result — the user is still actively speaking. Keep the latest
        // interim text so we can judge whether the sentence sounds finished.
        sawInterim=true;
        interimTail=e.results[i][0].transcript.trim();
      }
    }
    if(finalChunk.trim()) _listenBuf+=finalChunk;

    // Live dictation (2026-07-19 spec) — mirror what's been heard into the chat
    // input as you talk, so you can see/edit before it sends. Guarded by
    // _autoListening so this never fires while you're typing manually.
    if(_autoListening){
      var _liveInp=document.getElementById('text-input');
      if(_liveInp){ _liveInp.value=(_listenBuf+interimTail).trim(); _autoGrowInput(_liveInp); }
    }

    // Endpointing: ANY speech activity (interim OR final) extends the grace
    // window, so Mason won't cut in during a natural mid-thought pause (#40/#42).
    // The timer only fires after a true silence gap with nothing new arriving.
    if(finalChunk.trim()||sawInterim){
      // Adaptive extension (C.3): if the freshest words end on a conjunction /
      // article / preposition / dangling verb, the thought isn't done — give it
      // an extra beat before ending the turn. Prefer the live interim tail (most
      // recent), falling back to the accumulated final buffer.
      var _tail=(interimTail||finalChunk||_listenBuf).trim();
      var _grace=_INCOMPLETE_SUFFIX_RE.test(_tail)?(_SILENCE_GRACE_MS+_CLAUSE_EXTEND_MS):_SILENCE_GRACE_MS;
      if(_silenceTimer)clearTimeout(_silenceTimer);
      _silenceTimer=setTimeout(function(){
        var txt=_listenBuf.trim();
        _listenBuf='';
        if(txt&&_shouldRearm){
          _shouldRearm=false;
          rec.stop();
          var _si=document.getElementById('text-input'); if(_si){ _si.value=''; _resetInputHeight(); }
          sendQuery(txt);
        }
      },_grace);
    }
  };

  rec.onerror=function(e){
    if(_silenceTimer){clearTimeout(_silenceTimer);_silenceTimer=null;}
    if(_noSpeechTimer){clearTimeout(_noSpeechTimer);_noSpeechTimer=null;}
    _autoListening=false;
    _showListeningIndicator(false);
    _crystalState('idle');
    if(e.error==='not-allowed'||e.error==='service-not-allowed'){
      _listenBuf='';
      _flashStatusError('MIC ACCESS DENIED');
      return; // needs the user to grant permission — don't auto-resume
    }
    // Bugfix (2026-07-19): a transient error (no-speech/network/aborted) mid-
    // dictation used to wipe _listenBuf outright, silently discarding whatever
    // you'd already said. If there's something in the buffer, resume instead of
    // discarding it.
    if(_shouldRearm){
      if(_listenBuf.trim()){ _resumeListeningWithBuffer(1200); }
      else { setTimeout(listenForVoice,1200); }
    } else {
      _listenBuf='';
    }
  };

  rec.onend=function(){
    if(_silenceTimer){clearTimeout(_silenceTimer);_silenceTimer=null;}
    if(_noSpeechTimer){clearTimeout(_noSpeechTimer);_noSpeechTimer=null;}
    _autoListening=false;
    _showListeningIndicator(false);
    // Bugfix (2026-07-19): our OWN silence-timer path (above) already clears
    // _listenBuf and sets _shouldRearm=false BEFORE calling rec.stop() — so if
    // we land here with buffered text and _shouldRearm still true, this was the
    // BROWSER ending the session on its own mid-utterance, not a deliberate
    // stop. Don't fire off a half-finished sentence — resume seamlessly instead.
    if(_listenBuf.trim() && _shouldRearm){
      _resumeListeningWithBuffer(200);
      return;
    }
    var leftover=_listenBuf.trim();
    _listenBuf='';
    if(leftover&&_shouldRearm){
      _shouldRearm=false;
      var _ei=document.getElementById('text-input'); if(_ei){ _ei.value=''; _resetInputHeight(); }
      sendQuery(leftover);
      return;
    }
    if(_shouldRearm) setTimeout(listenForVoice,1200);
  };

  try{ rec.start(); }catch(ex){ _shouldRearm=false; return; }
  window._autoRec=rec;
}

// ── Crystal click — start listening if idle ──────────────────
function _crystalClick(){
  if(_autoListening){
    // Already listening — clicking stops it
    stopListening();
    _crystalState('idle');
  } else if(!window.speechSynthesis.speaking){
    // Idle and not speaking — start listening
    listenForVoice();
  }
  // If speaking, ignore — let it finish
}
// ─────────────────────────────────────────────────────────────

// ── Shutdown ─────────────────────────────────────────────────
var _SHUTDOWN_PHRASES=['goodbye mason','bye mason','shutdown mason',
  'shut down mason','power off mason','turn off mason','goodnight mason',
  'go to sleep mason','sleep mason'];
function _isShutdownPhrase(text){
  var t=text.toLowerCase().trim().replace(/[.!?]+$/,'');
  return _SHUTDOWN_PHRASES.some(function(p){ return t===p||t.includes(p); });
}
function confirmShutdown(){ shutdownMason(); }
function shutdownMason(){
  if(window._shuttingDown) return; // prevent double-fire
  window._shuttingDown=true;
  stopListening();
  _stopSynthKA();
  _clearWordTimers();
  window.speechSynthesis.cancel();
  // Show single line overlay then close
  var ov=document.createElement('div');
  ov.id='shutdown-overlay';
  ov.style.cssText='position:fixed;inset:0;z-index:9999;background:#000;display:flex;align-items:center;justify-content:center';
  ov.innerHTML='<div style="color:rgba(255,60,60,0.7);font-family:Courier New,monospace;font-size:13px;letter-spacing:.35em">mason going dark</div>';
  document.body.appendChild(ov);
  // Kill the server after a short delay
  setTimeout(function(){
    fetch('/api/shutdown',{method:'POST'}).catch(function(){});
  },400);
  // Close the browser tab
  setTimeout(function(){
    try{ window.close(); }catch(e){}
    setTimeout(function(){
      try{
        document.open();
        document.write('<html><body style="background:#000;margin:0"></body></html>');
        document.close();
      }catch(e){}
    },600);
  },1200);
}
function _doShutdown(){ shutdownMason(); }

// ─────────────────────────────────────────────────────────────

// ── Per-card collapse toggle (legacy — kept for any remaining .card-wrap usage) ──
function toggleCard(btn){
  var wrap=btn.closest('.card-wrap');
  if(!wrap)return;
  var card=wrap.querySelector('.info-card');
  if(!card)return;
  var collapsed=card.classList.toggle('card-collapsed');
  btn.innerHTML=collapsed?'&#9654;':'&#9664;';
}
// ── Chat input status indicator ─────────────────────────────────────────────
function _updateChatStatus(state){
  var el=document.getElementById('chat-status');
  var txt=document.getElementById('chat-status-text');
  if(!el||!txt)return;
  el.className='';
  el.onclick=null;
  el.onmouseenter=null;
  el.onmouseleave=null;
  if(state==='idle'){
    el.className='idle';
    txt.textContent='TAP TO LISTEN';
    el.onclick=function(){listenForVoice();};
    el.title='Click to start listening';
  } else if(state==='speaking'){
    el.className='speaking';
    txt.textContent='SPEAKING';
    el.onmouseenter=function(){txt.textContent='STOP SPEAKING';};
    el.onmouseleave=function(){txt.textContent='SPEAKING';};
    el.onclick=function(){stopSpeak();};
    el.title='Click to stop';
  } else if(state==='listening'){
    el.className='listening';
    txt.textContent='LISTENING';
    el.onmouseenter=function(){txt.textContent='STOP LISTENING';};
    el.onmouseleave=function(){txt.textContent='LISTENING';};
    el.onclick=function(){stopListening();stopMic();_crystalState('idle');};
    el.title='Click to stop';
  } else if(state==='thinking'){
    el.className='processing';
    txt.textContent='PROCESSING';
    el.onmouseenter=function(){txt.textContent='STOP PROCESSING';};
    el.onmouseleave=function(){txt.textContent='PROCESSING';};
    el.onclick=function(){stopProcessing();};
    el.title='Click to stop';
  }
  // any other/unrecognized state: className stays '' which is display:none via CSS
}
// ────────────────────────────────────────────────────────────────────────────

function stopListening(){
  _shouldRearm=false; _autoListening=false; _showListeningIndicator(false);
  _listenBuf='';
  if(_silenceTimer){clearTimeout(_silenceTimer);_silenceTimer=null;}
  if(_noSpeechTimer){clearTimeout(_noSpeechTimer);_noSpeechTimer=null;}
  if(window._autoRec){ try{ window._autoRec.stop(); }catch(e){} window._autoRec=null; }
  if(window._rec){ try{ window._rec.stop(); }catch(e){} window._rec=null; }
}

function _crystalState(state){
  // Keep the stuck-'speaking' watchdog in sync with the crystal state.
  if(state==='speaking'){ _startSpeakWatch(); } else { _stopSpeakWatch(); }
  // Section B: a finished analysis waiting to be announced gets spoken in the gap
  // when we settle into idle — never mid-speech, never over Nico. Small beat first.
  if(state==='idle'&&_analysisCalloutQueue&&_analysisCalloutQueue.length){
    setTimeout(function(){ try{_maybeSpeakCallout();}catch(e){} },450);
  }
  var st=document.getElementById('mason-state');
  var cm=document.getElementById('chat-crystal-mini');
  // 3D main crystal
  if(window._MC) window._MC.setState(state);
  // Mini chat crystal (CSS)
  if(cm){ cm.className='crystal-mini'; if(state&&state!=='idle')cm.classList.add('state-'+state); }
  var labels={listening:'&#9679; LISTENING',speaking:'&#9679; SPEAKING',thinking:'&#9679; PROCESSING',idle:'TAP TO LISTEN'};
  var colors={listening:'var(--green)',speaking:'var(--speak-cyan)',thinking:'var(--blue)',idle:'var(--purple)'};
  var stopLabels={listening:'&#9679; STOP LISTENING',speaking:'&#9679; STOP SPEAKING',thinking:'&#9679; STOP PROCESSING'};
  if(st){
    st.innerHTML=labels[state]||'';
    st.style.color=colors[state]||'';
    if(state==='idle'){
      // Idle is clickable too (starts listening) but previews a brighten, not a red stop —
      // there's nothing running yet to cancel.
      st.style.cursor='pointer';
      st.onmouseenter=function(){st.style.color='var(--blue-glow)';};
      st.onmouseleave=function(){st.style.color=colors.idle;};
      st.onclick=function(){listenForVoice();};
    } else if(state){
      st.style.cursor='pointer';
      (function(def,stop,col){
        st.onmouseenter=function(){st.innerHTML=stop;st.style.color='var(--red)';};
        st.onmouseleave=function(){st.innerHTML=def;st.style.color=col;};
      })(labels[state],stopLabels[state],colors[state]);
      if(state==='listening') st.onclick=function(){stopListening();_crystalState('idle');};
      else if(state==='speaking') st.onclick=function(){stopSpeak();};
      else if(state==='thinking') st.onclick=function(){stopProcessing();};
    } else {
      st.style.cursor='';
      st.onmouseenter=null;st.onmouseleave=null;st.onclick=null;
    }
  }
  _updateChatStatus(state);
  // Light up stop buttons red while speaking
  var isSpeaking=(state==='speaking');
  var sb1=document.getElementById('stop-speak-input');
  var sb2=document.getElementById('stop-speak-btn');
  if(sb1){ if(isSpeaking)sb1.classList.add('speaking'); else sb1.classList.remove('speaking'); }
  if(sb2){ if(isSpeaking)sb2.classList.add('speaking'); else sb2.classList.remove('speaking'); }
}

function _wordPulse(isBracket){
  if(window._MC) window._MC.wordPulse(isBracket);
  var cm=document.getElementById('chat-crystal-mini');
  if(!cm) return;
  var cls=isBracket?'syl-pulse-bracket':'syl-pulse';
  cm.classList.remove('syl-pulse','syl-pulse-bracket');
  void cm.offsetWidth;
  cm.classList.add(cls);
}

// ── Mason 3D Crystal — Cephalon body (Three.js) ───────────────────────────
// NOTE: wrapped in try/catch. On hosts without a usable WebGL context
// (Ubuntu VMs / servers, remote-desktop / VNC sessions, GPU-blocklisted
// browsers) THREE loads fine but `new THREE.WebGLRenderer()` throws. An
// uncaught throw here aborts the rest of this inline <script> — including the
// `DOMContentLoaded -> boot` wiring below — which strands the app on the
// "[ CLICK ANYWHERE TO BEGIN ]" screen. Failing soft keeps boot alive; the
// crystal just doesn't render and every caller already guards `window._MC`.
(function(){
 try{
  var canvas=document.getElementById('mason-3d-canvas');
  if(!canvas||typeof THREE==='undefined')return;
  // Probe for a real WebGL context before handing the canvas to THREE.
  var _glProbe=document.createElement('canvas');
  var _gl=_glProbe.getContext('webgl')||_glProbe.getContext('experimental-webgl');
  if(!_gl){ console.warn('Mason crystal: WebGL unavailable — skipping 3D crystal.'); return; }
  var W=260,H=300,dpr=Math.min(window.devicePixelRatio||1,2);
  canvas.width=W*dpr; canvas.height=H*dpr;
  canvas.style.width=W+'px'; canvas.style.height=H+'px';

  var renderer=new THREE.WebGLRenderer({canvas:canvas,antialias:true,alpha:true});
  renderer.setPixelRatio(dpr); renderer.setSize(W,H); renderer.setClearColor(0,0);
  var scene=new THREE.Scene();
  var root=new THREE.Group(); scene.add(root); // single scale point for bounce
  var camera=new THREE.PerspectiveCamera(40,W/H,0.1,100);
  camera.position.set(0,1.4,4.0); camera.lookAt(0,0,0);

  function makeGridTex(lc,bg){
    var c=document.createElement('canvas'); c.width=c.height=256;
    var ctx=c.getContext('2d');
    ctx.fillStyle=bg; ctx.fillRect(0,0,256,256);
    ctx.strokeStyle=lc; ctx.lineWidth=1;
    for(var i=0;i<=256;i+=22){
      ctx.beginPath();ctx.moveTo(i,0);ctx.lineTo(i,256);ctx.stroke();
      ctx.beginPath();ctx.moveTo(0,i);ctx.lineTo(256,i);ctx.stroke();
    }
    var t=new THREE.CanvasTexture(c);
    t.wrapS=t.wrapT=THREE.RepeatWrapping; t.repeat.set(2,2); return t;
  }
  var texN=makeGridTex('rgba(0,170,255,0.45)','rgba(0,12,40,0.18)');
  var texC=makeGridTex('rgba(255,55,0,0.45)','rgba(42,3,0,0.18)');

  var geo=new THREE.CylinderGeometry(1.12,0.46,1.1,4,8,false);
  geo.applyMatrix4(new THREE.Matrix4().makeRotationX(-Math.PI/2));
  var fMat=new THREE.MeshBasicMaterial({map:texN,transparent:true,opacity:0.42,side:THREE.DoubleSide,depthWrite:false});
  var eMat=new THREE.LineBasicMaterial({color:0x00bbff,transparent:true,opacity:0.9});
  var body=new THREE.Group();
  var faceMesh=new THREE.Mesh(geo,fMat);
  var edgeMesh=new THREE.LineSegments(new THREE.EdgesGeometry(geo),eMat);
  faceMesh.renderOrder=0; edgeMesh.renderOrder=1;
  body.add(faceMesh,edgeMesh); body.rotation.order='ZXY'; root.add(body);

  function gm(color,opacity){
    return new THREE.MeshBasicMaterial({color:color,transparent:true,opacity:opacity,
      blending:THREE.AdditiveBlending,depthWrite:false,depthTest:false});
  }

  function makeCoreCanvas(c1,c2){
    var cv=document.createElement('canvas'); cv.width=cv.height=128;
    var ctx=cv.getContext('2d');
    var g=ctx.createRadialGradient(64,64,0,64,64,64);
    g.addColorStop(0,c1); g.addColorStop(0.18,'rgba(220,248,255,0.95)');
    g.addColorStop(0.40,c2); g.addColorStop(0.72,'rgba(0,80,180,0.10)');
    g.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=g; ctx.fillRect(0,0,128,128);
    return new THREE.CanvasTexture(cv);
  }
  var coreTexN=makeCoreCanvas('rgba(255,255,255,1)','rgba(0,220,255,0.55)');
  var coreTexC=makeCoreCanvas('rgba(255,220,180,1)','rgba(255,100,20,0.55)');
  var coreSpriteMat=new THREE.SpriteMaterial({map:coreTexN,blending:THREE.AdditiveBlending,depthWrite:false,depthTest:false});
  var coreSprite=new THREE.Sprite(coreSpriteMat);
  coreSprite.scale.set(0.72,0.28,1); coreSprite.renderOrder=10; root.add(coreSprite);

  var R1=0.62,R2=0.34;
  var r1line=new THREE.Mesh(new THREE.TorusGeometry(R1,0.013,8,100),gm(0x00eeff,0.98));
  var r1halo=new THREE.Mesh(new THREE.TorusGeometry(R1,0.065,8,100),gm(0x0099cc,0.20));
  var r1far =new THREE.Mesh(new THREE.TorusGeometry(R1,0.14, 8,100),gm(0x004488,0.08));
  var r2line=new THREE.Mesh(new THREE.TorusGeometry(R2,0.010,8,80), gm(0x00f8ff,0.98));
  var r2halo=new THREE.Mesh(new THREE.TorusGeometry(R2,0.052,8,80), gm(0x00bbdd,0.20));
  var r2far =new THREE.Mesh(new THREE.TorusGeometry(R2,0.11, 8,80), gm(0x004488,0.08));
  var ambGlow=new THREE.Mesh(new THREE.SphereGeometry(0.55,16,16),gm(0x001133,0.12));
  var rings=[r1line,r1halo,r1far,r2line,r2halo,r2far];
  rings.forEach(function(r,i){ r.rotation.x=Math.PI/2; r.renderOrder=5+i; });
  ambGlow.renderOrder=4;
  var floaters=rings.concat([ambGlow]);
  floaters.forEach(function(o){ root.add(o); });

  var ringMatsN=[0x00eeff,0x0099cc,0x004488,0x00f8ff,0x00bbdd,0x004488];
  var ringMatsC=[0xff4400,0xff1100,0x550000,0xff6600,0xff3300,0x550000];

  var BASE_Z=Math.PI/4,BASE_X=-0.45,BASE_Y=0.32;
  var gazeX=0,gazeY=0,targetX=0,targetY=0,_gazeSpd=0.04,holdTimer=0;
  function pickGaze(){
    var home=Math.random()<0.3;
    targetX=home?0:(Math.random()-0.5)*0.5;
    targetY=home?0:(Math.random()-0.5)*0.55;
    _gazeSpd=Math.random()<0.4?0.09:0.03;
    holdTimer=1.2+Math.random()*2.5;
  }
  pickGaze();

  // State machine
  var _state='idle',_prevState='idle',_corrupted=false,_corruptTimer=0,_glitchX=0,_gt=0,_burstTimer=0;
  var _bounce=1.0; // spring scale for Siri-like syllable bounce
  var lastT=performance.now();
  // Per-state params: ripple freq, ring brightness mult, core scale, float amplitude
  var SP={
    idle:     {rf:1.8,rb:0.5, cs:1.0, fa:0.09},
    listening:{rf:2.4,rb:1.5, cs:1.2, fa:0.06},
    thinking: {rf:1.2,rb:0.4, cs:0.8, fa:0.05},
    speaking: {rf:2.8,rb:0.9, cs:1.2, fa:0.08}
  };

  function _applyColors(corrupt){
    if(corrupt){
      eMat.color.set(0xff3300);
      rings.forEach(function(r,i){ r.material.color.set(ringMatsC[i]); });
      coreSpriteMat.map=coreTexC; coreSpriteMat.needsUpdate=true;
      fMat.map=texC; fMat.needsUpdate=true;
    } else {
      eMat.color.set(0x00bbff);
      rings.forEach(function(r,i){ r.material.color.set(ringMatsN[i]); });
      coreSpriteMat.map=coreTexN; coreSpriteMat.needsUpdate=true;
      fMat.map=texN; fMat.needsUpdate=true;
    }
  }

  window._MC={
    setState:function(s){
      _state=s||'idle'; _applyColors(false);
    },
    wordPulse:function(isBracket){
      if(isBracket){
        _prevState=_state; _corrupted=true; _corruptTimer=0.65; _applyColors(true);
        _bounce=1.22;
      } else {
        _bounce=1.16;
      }
    },
    resetBounce:function(){ _bounce=1.0; }
  };

  function animate(){
    requestAnimationFrame(animate);
    var now=performance.now();
    var dt=Math.min((now-lastT)/1000,0.05); lastT=now;
    var t=now*0.001;

    if(_corrupted){
      _corruptTimer-=dt;
      if(_corruptTimer<=0){
        _corrupted=false; _glitchX=0; _state=_prevState; _applyColors(false);
      } else {
        _gt+=dt;
        if(_gt>0.08){ _gt=0; _glitchX=(Math.random()-0.5)*0.14; }
      }
    }
    body.position.x=_corrupted?_glitchX:0;
    if(_burstTimer>0) _burstTimer-=dt;

    // Spring bounce — decay toward 1.0 at ~14% per frame (smooth ~400ms return)
    _bounce+=(1.0-_bounce)*0.14;
    root.scale.setScalar(_bounce);

    var sp=SP[_corrupted?'idle':_state]||SP.idle;
    holdTimer-=dt;
    if(holdTimer<=0) pickGaze();
    if(_state==='thinking'){
      gazeX+=(0-gazeX)*0.02; gazeY+=(0-gazeY)*0.02;
    } else {
      gazeX+=(targetX-gazeX)*_gazeSpd; gazeY+=(targetY-gazeY)*_gazeSpd;
    }

    var fy=Math.sin(t*0.75)*sp.fa;
    body.position.y=fy;
    body.rotation.z=BASE_Z; body.rotation.x=BASE_X+gazeX; body.rotation.y=BASE_Y+gazeY;

    var rip1=1+Math.sin(t*sp.rf)*0.06;
    var rip2=1+Math.sin(t*sp.rf+1.1)*0.06;
    r1line.scale.setScalar(rip1); r1halo.scale.setScalar(rip1); r1far.scale.setScalar(rip1);
    r2line.scale.setScalar(rip2); r2halo.scale.setScalar(rip2); r2far.scale.setScalar(rip2);
    r1line.material.opacity=Math.min(1,(0.85+Math.sin(t*sp.rf)*0.13)*sp.rb);
    r2line.material.opacity=Math.min(1,(0.85+Math.sin(t*sp.rf+1.1)*0.13)*sp.rb);
    floaters.forEach(function(o){ o.position.y=fy; });

    var burstBoost=_burstTimer>0?0.45:0;
    var pulse=1+Math.sin(t*2.5)*0.32;
    var csc=sp.cs*(1+burstBoost);
    coreSprite.position.y=fy;
    coreSprite.scale.set(0.72*pulse*csc,0.28*pulse*csc,1);

    renderer.render(scene,camera);
  }
  animate();

  canvas.addEventListener('mouseenter',function(){
    if(_state==='idle') canvas.style.filter='brightness(1.18)';
  });
  canvas.addEventListener('mouseleave',function(){ canvas.style.filter=''; });
 }catch(_crystalErr){
  console.warn('Mason crystal init failed — continuing without 3D crystal.',_crystalErr);
 }
})();
// ─────────────────────────────────────────────────────────────────────────────

// Stop voice loop when user is typing
document.addEventListener('DOMContentLoaded',function(){
  var inp=document.getElementById('text-input');
  if(inp)inp.addEventListener('focus',stopListening);
});
// boot() is wired to DOMContentLoaded near the top of this script (see comment
// there) so a throw in a later top-level IIFE cannot strand the boot screen.

// ── Mason Grid System ─────────────────────────────────────────────────────────
(function(){
  var COLS=24, ROWS=16;
  var MASON_ZONE={c1:9,c2:18,r1:5,r2:14}; // 1-based, exclusive end (matches CSS grid-column:9/18 grid-row:5/14)
  var MIN_COLS=2, MIN_ROWS=1;
  var GAP=4; // px gap between cards
  var LS_KEY='mason_layout';

  var DEFAULT_LAYOUT={
    'market-posture-card':{c1:1, c2:9, r1:1, r2:3,  collapsedR2:null},
    'buffett-card':        {c1:1, c2:9, r1:4, r2:8,  collapsedR2:null},
    'holdings-card':       {c1:1, c2:9, r1:8, r2:17, collapsedR2:null},
    'options-card':        {c1:18,c2:25,r1:1, r2:9,  collapsedR2:null},
    'trades-card':         {c1:18,c2:25,r1:9, r2:17, collapsedR2:null},
    'news-card':           {c1:9, c2:18,r1:1, r2:5,  collapsedR2:null},
    'sectors-card':        {c1:9, c2:18,r1:5, r2:11, collapsedR2:null},
    'movers-card':         {c1:9, c2:18,r1:11,r2:17, collapsedR2:null}
  };

  var CARD_IDS=Object.keys(DEFAULT_LAYOUT);
  var gridEl=document.getElementById('mason-grid');
  if(!gridEl)return;

  // ── Cell geometry helpers ──────────────────────────────────────────────────
  function cellW(){ return gridEl.offsetWidth/COLS; }
  function cellH(){ return gridEl.offsetHeight/ROWS; }

  function posFromGrid(lay){
    var cw=cellW(), ch=cellH();
    return {
      left:(lay.c1-1)*cw+GAP,
      top:(lay.r1-1)*ch+GAP,
      width:(lay.c2-lay.c1)*cw-GAP*2,
      height:(lay.r2-lay.r1)*ch-GAP*2
    };
  }

  function gridFromPixel(px,py){
    var cw=cellW(), ch=cellH();
    return {
      col:Math.max(1,Math.min(COLS,Math.floor(px/cw)+1)),
      row:Math.max(1,Math.min(ROWS,Math.floor(py/ch)+1))
    };
  }

  // ── Layout state ──────────────────────────────────────────────────────────
  var layout={};
  CARD_IDS.forEach(function(id){
    layout[id]=Object.assign({},DEFAULT_LAYOUT[id]);
  });

  function loadLayout(){
    try{
      var saved=JSON.parse(localStorage.getItem(LS_KEY)||'null');
      if(saved){
        CARD_IDS.forEach(function(id){
          if(saved[id]){
            layout[id]=saved[id];
            // ensure collapsedR2 field exists on old saves
            if(layout[id].collapsedR2===undefined) layout[id].collapsedR2=null;
          }
        });
      }
    }catch(e){}
  }

  function saveLayout(){
    try{ localStorage.setItem(LS_KEY,JSON.stringify(layout)); }catch(e){}
  }

  function applyLayout(){
    CARD_IDS.forEach(function(id){
      var el=document.getElementById(id);
      if(!el)return;
      var p=posFromGrid(layout[id]);
      el.style.left=p.left+'px';
      el.style.top=p.top+'px';
      el.style.width=p.width+'px';
      el.style.height=p.height+'px';
    });
  }

  // ── Overlap / Mason zone checks ───────────────────────────────────────────
  function overlapsMason(lay){
    return !(lay.c2<=MASON_ZONE.c1||lay.c1>=MASON_ZONE.c2||
             lay.r2<=MASON_ZONE.r1||lay.r1>=MASON_ZONE.r2);
  }

  function overlapsCard(lay, skipId){
    return CARD_IDS.some(function(id){
      if(id===skipId)return false;
      var o=layout[id];
      return !(lay.c2<=o.c1||lay.c1>=o.c2||lay.r2<=o.r1||lay.r1>=o.r2);
    });
  }

  function isValidPlacement(lay, skipId){
    if(lay.c1<1||lay.r1<1||lay.c2>COLS+1||lay.r2>ROWS+1)return false;
    if(lay.c2-lay.c1<MIN_COLS||lay.r2-lay.r1<MIN_ROWS)return false;
    if(overlapsMason(lay))return false;
    if(overlapsCard(lay,skipId))return false;
    return true;
  }

  // ── Drop overlay ──────────────────────────────────────────────────────────
  var overlayEl=document.getElementById('g-drop-overlay');

  function showDropHighlight(lay, valid){
    clearDropHighlight();
    var cw=cellW(), ch=cellH();
    var div=document.createElement('div');
    div.className='g-cell-highlight '+(valid?'valid':'invalid');
    div.style.left=((lay.c1-1)*cw)+'px';
    div.style.top=((lay.r1-1)*ch)+'px';
    div.style.width=((lay.c2-lay.c1)*cw)+'px';
    div.style.height=((lay.r2-lay.r1)*ch)+'px';
    overlayEl.appendChild(div);
  }

  function clearDropHighlight(){
    overlayEl.innerHTML='';
  }

  // ── Ghost element ─────────────────────────────────────────────────────────
  var ghost=document.getElementById('g-ghost');
  if(!ghost){ghost=document.createElement('div');ghost.id='g-ghost';document.body.appendChild(ghost);}

  function showGhost(x,y,w,h){
    var z=parseFloat(getComputedStyle(document.documentElement).zoom)||1;
    ghost.style.display='block';
    ghost.style.left=(x/z)+'px'; ghost.style.top=(y/z)+'px';
    ghost.style.width=w+'px'; ghost.style.height=h+'px';
  }
  function hideGhost(){ ghost.style.display='none'; }

  // ── DRAG ──────────────────────────────────────────────────────────────────
  var dragState=null;

  function onDragStart(e, cardId){
    if(e.button!==0)return;
    e.preventDefault();
    var el=document.getElementById(cardId);
    var lay=layout[cardId];
    var colSpan=lay.c2-lay.c1, rowSpan=lay.r2-lay.r1;
    var cw=cellW(), ch=cellH();

    // Record where within the card the user grabbed, so the ghost
    // stays anchored to the grab point (not the card's top-left corner).
    var gr=gridEl.getBoundingClientRect();
    var cardLeft=gr.left+(lay.c1-1)*cw, cardTop=gr.top+(lay.r1-1)*ch;
    var grabOffsetX=Math.max(0,Math.min(colSpan*cw, e.clientX-cardLeft));
    var grabOffsetY=Math.max(0,Math.min(rowSpan*ch, e.clientY-cardTop));

    el.classList.add('g-dragging');
    showGhost(e.clientX-grabOffsetX, e.clientY-grabOffsetY, colSpan*cw, rowSpan*ch);

    dragState={
      cardId:cardId,
      colSpan:colSpan, rowSpan:rowSpan,
      grabOffsetX:grabOffsetX, grabOffsetY:grabOffsetY,
      origLay:Object.assign({},lay)
    };
  }

  function onDragMove(e){
    if(!dragState)return;
    var cw=cellW(), ch=cellH();
    var ox=dragState.grabOffsetX, oy=dragState.grabOffsetY;
    // Ghost top-left stays at grab-offset distance from cursor
    showGhost(e.clientX-ox, e.clientY-oy, dragState.colSpan*cw, dragState.rowSpan*ch);

    // Cell under the ghost's top-left = where the card's top-left will land
    var gr=gridEl.getBoundingClientRect();
    var px=(e.clientX-ox)-gr.left, py=(e.clientY-oy)-gr.top;
    var cell=gridFromPixel(px, py);
    var proposed={
      c1:cell.col, c2:cell.col+dragState.colSpan,
      r1:cell.row, r2:cell.row+dragState.rowSpan,
      collapsedR2:dragState.origLay.collapsedR2!==undefined?dragState.origLay.collapsedR2:null
    };
    // clamp to grid
    if(proposed.c2>COLS+1){proposed.c1=COLS+1-dragState.colSpan;proposed.c2=COLS+1;}
    if(proposed.r2>ROWS+1){proposed.r1=ROWS+1-dragState.rowSpan;proposed.r2=ROWS+1;}
    if(proposed.c1<1){proposed.c1=1;proposed.c2=1+dragState.colSpan;}
    if(proposed.r1<1){proposed.r1=1;proposed.r2=1+dragState.rowSpan;}
    var valid=isValidPlacement(proposed,dragState.cardId);
    showDropHighlight(proposed,valid);
    dragState.proposed=proposed;
    dragState.proposedValid=valid;
  }

  function onDragEnd(e){
    if(!dragState)return;
    var el=document.getElementById(dragState.cardId);
    el.classList.remove('g-dragging');
    hideGhost();
    clearDropHighlight();

    if(dragState.proposedValid && dragState.proposed){
      // Preserve collapsedR2 so collapse state survives drag
      var prev=layout[dragState.cardId];
      dragState.proposed.collapsedR2=prev.collapsedR2!==undefined?prev.collapsedR2:null;
      layout[dragState.cardId]=dragState.proposed;
      saveLayout();
    }
    applyLayout();
    dragState=null;
  }

  // ── RESIZE ────────────────────────────────────────────────────────────────
  var resizeState=null;

  function onResizeStart(e, cardId, edge){
    if(e.button!==0)return;
    e.preventDefault();
    e.stopPropagation();
    var lay=layout[cardId];
    resizeState={
      cardId:cardId, edge:edge,
      startX:e.clientX, startY:e.clientY,
      origLay:Object.assign({},lay)
    };
  }

  function onResizeMove(e){
    if(!resizeState)return;
    var cw=cellW(), ch=cellH();
    var dx=e.clientX-resizeState.startX;
    var dy=e.clientY-resizeState.startY;
    var dcol=Math.round(dx/cw), drow=Math.round(dy/ch);
    var o=resizeState.origLay;
    // Preserve collapsedR2 through resize
    var lay={c1:o.c1,c2:o.c2,r1:o.r1,r2:o.r2,
             collapsedR2:o.collapsedR2!==undefined?o.collapsedR2:null};
    var edge=resizeState.edge;

    if(edge.indexOf('e')!==-1) lay.c2=Math.max(o.c1+MIN_COLS,Math.min(COLS+1,o.c2+dcol));
    if(edge.indexOf('w')!==-1) lay.c1=Math.min(o.c2-MIN_COLS,Math.max(1,o.c1+dcol));
    if(edge.indexOf('s')!==-1) lay.r2=Math.max(o.r1+MIN_ROWS,Math.min(ROWS+1,o.r2+drow));
    if(edge.indexOf('n')!==-1) lay.r1=Math.min(o.r2-MIN_ROWS,Math.max(1,o.r1+drow));

    var valid=!overlapsMason(lay)&&!overlapsCard(lay,resizeState.cardId)&&
      lay.c1>=1&&lay.r1>=1&&lay.c2<=COLS+1&&lay.r2<=ROWS+1;
    if(valid){
      layout[resizeState.cardId]=lay;
      var el=document.getElementById(resizeState.cardId);
      var p=posFromGrid(lay);
      el.style.left=p.left+'px'; el.style.top=p.top+'px';
      el.style.width=p.width+'px'; el.style.height=p.height+'px';
    }
  }

  function onResizeEnd(){
    if(!resizeState)return;
    saveLayout();
    resizeState=null;
  }

  // ── Global mouse events ───────────────────────────────────────────────────
  document.addEventListener('mousemove',function(e){
    if(dragState) onDragMove(e);
    if(resizeState) onResizeMove(e);
  });
  document.addEventListener('mouseup',function(e){
    if(dragState) onDragEnd(e);
    if(resizeState) onResizeEnd();
  });
  // Releasing the mouse OUTSIDE the window (e.g. dragging toward the taskbar)
  // never fires a document mouseup — that left the drag ghost + drop highlight
  // stuck on screen (the "trailing" bug). End any in-progress drag/resize when
  // the pointer leaves the document or the window loses focus.
  function _endAnyGesture(){
    if(dragState) onDragEnd();
    if(resizeState) onResizeEnd();
    // Belt-and-suspenders: clear any lingering visuals even if state was already null.
    try{ hideGhost(); clearDropHighlight(); }catch(e){}
    try{ document.querySelectorAll('.g-card.g-dragging').forEach(function(c){c.classList.remove('g-dragging');}); }catch(e){}
  }
  document.addEventListener('mouseleave',_endAnyGesture);
  window.addEventListener('blur',_endAnyGesture);

  // ── Wire up cards ─────────────────────────────────────────────────────────
  CARD_IDS.forEach(function(id){
    var el=document.getElementById(id);
    if(!el)return;

    // Drag handle
    var handle=el.querySelector('.g-drag-handle');
    if(handle){
      handle.addEventListener('mousedown',function(e){
        onDragStart(e,id);
      });
    }

    // Resize handles
    var edgeMap={
      'g-rh-n':'n','g-rh-s':'s','g-rh-e':'e','g-rh-w':'w',
      'g-rh-ne':'ne','g-rh-nw':'nw','g-rh-se':'se','g-rh-sw':'sw'
    };
    el.querySelectorAll('.g-resize-handle').forEach(function(rh){
      var edge=null;
      Object.keys(edgeMap).forEach(function(cls){
        if(rh.classList.contains(cls)) edge=edgeMap[cls];
      });
      if(edge){
        rh.addEventListener('mousedown',function(e){
          onResizeStart(e,id,edge);
        });
      }
    });
  });

  // ── Collapse toggle for grid cards ────────────────────────────────────────
  function collapseCard(id){
    var el=document.getElementById(id);
    if(!el)return;
    var lay=layout[id];
    if(lay.collapsedR2!==null)return; // already collapsed
    lay.collapsedR2=lay.r2;
    lay.r2=lay.r1+1;
    el.classList.add('g-collapsed');
    var btn=el.querySelector('.g-collapse-btn');
    if(btn)btn.innerHTML='&#9650;';
    var p=posFromGrid(lay);
    el.style.top=p.top+'px'; el.style.left=p.left+'px';
    el.style.width=p.width+'px'; el.style.height=p.height+'px';
    saveLayout();
  }

  // ── Shake card to signal conflict ────────────────────────────────────────
  function shakeCard(id){
    var el=document.getElementById(id);
    if(!el)return;
    el.classList.add('g-conflict','g-shaking');
    el.addEventListener('animationend',function h(){
      el.classList.remove('g-shaking');
      el.removeEventListener('animationend',h);
    });
    setTimeout(function(){ el.classList.remove('g-conflict'); }, 700);
  }

  // ── Expand with cascade-push (push cards below down; shake on OOB) ────────
  function expandCard(id){
    var el=document.getElementById(id);
    if(!el)return;
    var lay=layout[id];
    if(lay.collapsedR2===null||lay.collapsedR2===undefined)return;

    // Simulate the expansion + cascade push in a copy of layout
    var sim={};
    CARD_IDS.forEach(function(cid){ sim[cid]=Object.assign({},layout[cid]); });

    // Apply the expansion to sim
    sim[id].r2=sim[id].collapsedR2;
    sim[id].collapsedR2=null;

    // Cascade: push overlapping cards down, sorted top→bottom, repeat until stable
    var maxIter=CARD_IDS.length*3;
    for(var iter=0;iter<maxIter;iter++){
      var anyPush=false;
      var sorted=CARD_IDS.slice().sort(function(a,b){ return sim[a].r1-sim[b].r1; });
      for(var i=0;i<sorted.length;i++){
        for(var j=i+1;j<sorted.length;j++){
          var A=sim[sorted[i]], B=sim[sorted[j]];
          // Skip if no column overlap
          if(B.c2<=A.c1||B.c1>=A.c2) continue;
          // Skip if no row overlap
          if(B.r2<=A.r1||B.r1>=A.r2) continue;
          // A is above B (sorted by r1). Push B down until no overlap.
          var pushBy=A.r2-B.r1;
          if(pushBy>0){
            B.r1+=pushBy;
            B.r2+=pushBy;
            if(B.collapsedR2!==null&&B.collapsedR2!==undefined){
              B.collapsedR2+=pushBy;
            }
            anyPush=true;
          }
        }
      }
      if(!anyPush) break;
    }

    // Clip any cards that exceed grid bounds (allow expand even if saved size was larger)
    CARD_IDS.forEach(function(cid){
      if(sim[cid].r2>ROWS+1) sim[cid].r2=ROWS+1;
    });

    // Commit sim → layout and update DOM
    CARD_IDS.forEach(function(cid){
      layout[cid]=sim[cid];
      var cel=document.getElementById(cid);
      if(!cel)return;
      if(cid===id){
        cel.classList.remove('g-collapsed');
        var btn=cel.querySelector('.g-collapse-btn');
        if(btn)btn.innerHTML='&#9660;';
      }
      var p=posFromGrid(layout[cid]);
      cel.style.top=p.top+'px'; cel.style.left=p.left+'px';
      cel.style.width=p.width+'px'; cel.style.height=p.height+'px';
    });
    saveLayout();
  }

  window.toggleGCard=function(btn){
    // Walk up the DOM to find the .g-card (more compatible than closest())
    var card=btn;
    while(card&&!card.classList.contains('g-card')) card=card.parentElement;
    if(!card)return;
    var id=card.id
    var lay=layout[id];
    if(!lay)return;
    var isCollapsed=(lay.collapsedR2!==null&&lay.collapsedR2!==undefined);
    if(isCollapsed) expandCard(id);
    else collapseCard(id);
  };

  // ── Chat-open: push right cards left / restore on close ──────────────────
  // Snapshot saved before any repositioning so close is a clean restore.
  var _preChatLayout=null;

  window._gridOnChatOpen=function(){
    // 1. Snapshot current layout (deep copy)
    _preChatLayout={};
    CARD_IDS.forEach(function(id){ _preChatLayout[id]=Object.assign({},layout[id]); });

    // 2. Build a working copy and push right-side cards into available space
    //    "Right-side" = c1 >= MASON_ZONE.c2 (cols 18+, i.e. past Mason's right edge)
    var sim={};
    CARD_IDS.forEach(function(id){ sim[id]=Object.assign({},layout[id]); });

    // Sort right-side cards top-to-bottom so we stack them in order
    var rightIds=CARD_IDS.filter(function(id){ return sim[id].c1>=MASON_ZONE.c2; });
    rightIds.sort(function(a,b){ return sim[a].r1-sim[b].r1; });

    rightIds.forEach(function(id){
      var lay=sim[id];
      var rowSpan=(lay.collapsedR2!==null&&lay.collapsedR2!==undefined)
        ? lay.collapsedR2-lay.r1 : lay.r2-lay.r1;
      var colSpan=lay.c2-lay.c1;

      // Find lowest occupied row in the left column zone (cols 1 to colSpan)
      var bestR1=1;
      CARD_IDS.forEach(function(oid){
        if(oid===id)return;
        var o=sim[oid];
        if(o.c1>=MASON_ZONE.c2)return;
        var targetC1=1, targetC2=1+colSpan;
        var colOverlap=!(targetC2<=o.c1||targetC1>=o.c2);
        if(!colOverlap)return;
        var effR2=(o.collapsedR2!==null&&o.collapsedR2!==undefined)?o.collapsedR2:o.r2;
        if(effR2>bestR1) bestR1=effR2;
      });

      var newR1=bestR1, newR2=bestR1+rowSpan;
      if(newR2<=ROWS+1 && !_simOverlapsMason({c1:1,c2:1+colSpan,r1:newR1,r2:newR2})){
        sim[id]=Object.assign({},lay,{c1:1,c2:1+colSpan,r1:newR1,r2:newR2,
          collapsedR2:(lay.collapsedR2!==null&&lay.collapsedR2!==undefined)?newR2:null});
      }
    });

    // 3. Commit and repaint
    CARD_IDS.forEach(function(id){
      layout[id]=sim[id];
      var el=document.getElementById(id);
      if(!el)return;
      var p=posFromGrid(layout[id]);
      el.style.left=p.left+'px'; el.style.top=p.top+'px';
      el.style.width=p.width+'px'; el.style.height=p.height+'px';
    });
    setTimeout(applyLayout,420);
  };

  function _simOverlapsMason(lay){
    return !(lay.c2<=MASON_ZONE.c1||lay.c1>=MASON_ZONE.c2||
             lay.r2<=MASON_ZONE.r1||lay.r1>=MASON_ZONE.r2);
  }

  window._gridOnChatClose=function(){
    if(!_preChatLayout)return;
    CARD_IDS.forEach(function(id){
      layout[id]=Object.assign({},_preChatLayout[id]);
      var el=document.getElementById(id);
      if(!el)return;
      var lay=layout[id];
      var isCollapsed=(lay.collapsedR2!==null&&lay.collapsedR2!==undefined);
      el.classList.toggle('g-collapsed',isCollapsed);
      var btn=el.querySelector('.g-collapse-btn');
      if(btn)btn.innerHTML=isCollapsed?'&#9650;':'&#9660;';
      var p=posFromGrid(lay);
      el.style.left=p.left+'px'; el.style.top=p.top+'px';
      el.style.width=p.width+'px'; el.style.height=p.height+'px';
    });
    _preChatLayout=null;
    setTimeout(applyLayout,420);
  };

  // ── Reset layout ──────────────────────────────────────────────────────────
  window.resetGridLayout=function(){
    try{ localStorage.removeItem(LS_KEY); }catch(e){}
    location.reload();
  };

  // ── Reflow on resize ──────────────────────────────────────────────────────
  window.addEventListener('resize',function(){ applyLayout(); });

  // ── Init ──────────────────────────────────────────────────────────────────
  loadLayout();
  // Restore collapse class + button icon from saved state
  CARD_IDS.forEach(function(id){
    var el=document.getElementById(id);
    if(!el)return;
    var lay=layout[id];
    var isCollapsed=(lay.collapsedR2!==null&&lay.collapsedR2!==undefined);
    el.classList.toggle('g-collapsed',isCollapsed);
    var btn=el.querySelector('.g-collapse-btn');
    if(btn)btn.innerHTML=isCollapsed?'&#9650;':'&#9660;';
  });
  applyLayout();
  requestAnimationFrame(applyLayout);
})();
// ─────────────────────────────────────────────────────────────────────────────

