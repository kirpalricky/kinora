// Event type -> label/colour/emoji. 2/6/7 are inferred from the cameras' data; adjust if wrong.
const TYPES={2:{n:'Person',c:'#5B8DEF'},7:{n:'Baby crying',c:'#EE88AA'},6:{n:'Motion / sound',c:'#EBBF62'}};
const T=t=>TYPES[t]||{n:'Event '+t,c:'#8b5cf6'};
const $=id=>document.getElementById(id), v=$('v'), cv=$('cv'), ctx=cv.getContext('2d');
const pad=n=>String(n).padStart(2,'0');
const esc=s=>String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch]);   // camera names are user text
const hm=ts=>{const d=new Date(ts*1000);let h=d.getHours();const ap=h<12?'am':'pm';h=h%12||12;return `${h}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${ap}`};
const hh=ts=>{const d=new Date(ts*1000);const h=d.getHours();return `${h%12||12}:00${h<12?'am':'pm'}`};
const mmss=s=>`${pad(Math.floor(s/60))}:${pad(Math.round(s%60))}`;
/* player-logic:begin */
// Pure functions for the app bar, the Live/Recorded control and the status line. They touch no DOM, network or storage:
// tests/test_player_logic.js reads this block between the markers.
const pad2=n=>String(n).padStart(2,'0');
function clockShort(ts){ const d=new Date(ts*1000), h=d.getHours(); return `${h%12||12}:${pad2(d.getMinutes())} ${h<12?'am':'pm'}`; }
function clockFull(ts){ const d=new Date(ts*1000), h=d.getHours(); return `${h%12||12}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())} ${h<12?'am':'pm'}`; }
// Status line (#livestat): only problems and actionable states have text. Healthy states (connecting, live, recorded, idle)
// return no text, so the line stays hidden. kind: connecting | live | recorded | recording | note | busy | paused | offline | disabled | notset | idle
function statusLine(st){
  switch(st.kind){
    case 'recording': return {text:'Still recording. Try again in a minute.', tone:'accent'};
    case 'note': return {text:st.text||'', tone:st.tone||'warn'};
    case 'busy': return {text:'Camera busy: another app is watching.'+(st.secs?` Retrying in ${st.secs} s.`:''), tone:'warn'};
    case 'paused': return {text:`Sign-ins paused ${st.mins} min to protect the camera.`, tone:'warn'};
    case 'offline': return {text:`${st.name} isn't answering on the network.`, tone:'bad'};
    case 'disabled': return {text:`${st.name} is disabled: ${st.reason}`, tone:'bad'};
    case 'notset': return {text:'Not set up yet.', tone:'mut'};
    case 'connecting': case 'live': case 'recorded': return {text:'', tone:'ok'};
    default: return {text:'', tone:'mut'};
  }
}
// What a click on a mode button does: 'none' | 'start' (Live today) | 'jump' (Live on a past day: go to Today) | 'stop' (Recorded while live).
// Explanation for the Live/Recordings controls (title-like tooltip, data-tip and aria-description; no visible caption).
function modeCaption(live){ return live?'Watching live. This is what the camera sees right now.':'Playing back what the camera saved to its SD card. Pick a time on the timeline or an event.'; }
function modeAction(liveOn,target,today){
  if(target==='live') return liveOn?'none':(today?'start':'jump');
  return liveOn?'stop':'none';
}
// Camera switcher (role=tablist): index to move focus to for an arrow/Home/End key, or null for any other key.
function tabTarget(key,i,n){
  if(!(n>0)) return null;
  if(key==='ArrowRight') return i<0||i>=n-1?0:i+1;
  if(key==='ArrowLeft') return i<=0?n-1:i-1;
  if(key==='Home') return 0;
  if(key==='End') return n-1;
  return null;
}
// Roving tabindex: the selected tab is the one tab stop (the first tab when no camera is selected).
function rovingTabIndex(selected,i){ return selected>=0?(i===selected?0:-1):(i===0?0:-1); }
// Events list: subhead text ("2 pm"), counts, the export button's label, which body state to show, grouping by clock hour.
function countText(n){ return `${n} event${n===1?'':'s'}`; }
function countChip(total,shown,filtered){ return filtered?`${shown} of ${total}`:String(total); }
function exportLabel(ts){ return `Export event at ${clockShort(ts)}`; }
// Body of the list: 'loading' (skeletons), 'empty' (no events that day), 'filtered' (none match the chips), 'list'.
// Events panel state (one per render; every finished load ends in one of these). loading = skeletons and spinner (a refresh
// that keeps the rows is not loading); error = alert with Retry (rows stay if there are any); empty = no events that day;
// filtered-empty = events exist but the chosen types match none; ready = the list.
function eventsPanelState(s){
  if(s.loading&&!s.refreshing) return 'loading';
  if(s.error) return 'error';
  if(!(s.total>0)) return 'empty';
  if(!(s.shown>0)) return 'filtered-empty';
  return 'ready';
}
// The type filter row is shown only when there are at least two event types to choose from.
function filtersVisible(types){ return types>=2; }
// Events list order: a new array, newest start first (flat list, no hour subheads).
function newestFirst(list){ return [...list].sort((a,b)=>b.start-a.start); }
// Up/Down/Home/End in the event rows (no wrap): index to focus, or null for any other key. i < 0 means focus is outside the list.
function rovingEventIndex(key,i,n){
  if(!(n>0)) return null;
  if(key==='ArrowDown') return i<0?0:Math.min(n-1,i+1);
  if(key==='ArrowUp') return i<0?0:Math.max(0,i-1);
  if(key==='Home') return 0;
  if(key==='End') return n-1;
  return null;
}
// Timeline keys: seconds to move the playhead (Left/Right 10 s, with Shift 1 min), or null for other keys.
function timelineKeyStep(key,shift){ const s=shift?60:10; if(key==='ArrowLeft') return -s; if(key==='ArrowRight') return s; return null; }
// Event thumbnails (/api/thumb): what to do after one answer. st = {tries, hard}: attempts and hard failures so far, both
// counting this one. 202 = the camera's video slot is busy: retry after the backoff (or Retry-After if longer), at most
// THUMB_MAX_TRIES attempts. Any other failure is retried once. 404 = thumbnails are off for the camera: no retry.
const THUMB_BACKOFF_MS=[5000,10000,20000,40000], THUMB_MAX_TRIES=5, THUMB_HARD_RETRY_MS=5000, THUMB_REC_TRIES=12, THUMB_REC_MIN_MS=10000;
function thumbOutcome(status,st,retryAfterS){
  if(status===200) return {do:'show'};
  if(status===404) return {do:'none',why:'off'};
  if(status===202){
    if(!(st.tries<THUMB_MAX_TRIES)) return {do:'none',why:'busy'};
    return {do:'wait',ms:Math.max(THUMB_BACKOFF_MS[st.tries-1],(retryAfterS||0)*1000)};
  }
  // 409 = the event is still being recorded (a refresh brings in new ones): wait for it, not a failure. Up to 12 attempts.
  if(status===409) return st.tries<THUMB_REC_TRIES?{do:'wait',ms:Math.max(THUMB_REC_MIN_MS,(retryAfterS||0)*1000)}:{do:'none',why:'failed'};
  return st.hard<2?{do:'wait',ms:THUMB_HARD_RETRY_MS}:{do:'none',why:'failed'};
}
// Stretches of [lo,hi] with no recording, from clips [{start,end}]. Gaps shorter than minS seconds are ignored.
function gapsOf(clips,lo,hi,minS){
  const out=[]; let cur=lo;
  for(const c of [...clips].sort((a,b)=>a.start-b.start)){
    if(c.end<=cur) continue;
    if(c.start-cur>=minS) out.push({start:cur,end:Math.min(c.start,hi)});
    cur=Math.max(cur,c.end); if(cur>=hi) break;
  }
  if(hi-cur>=minS) out.push({start:cur,end:hi});
  return out.filter(g=>g.end>g.start);
}
// Events of one type whose gap is at most gapS seconds become one range, so a busy hour reads as a few bars, not a barcode.
function mergeEvents(list,gapS){
  const out=[], last={};
  for(const e of [...list].sort((x,y)=>x.start-y.start)){
    const m=last[e.type];
    if(m&&e.start-m.end<=gapS){ m.end=Math.max(m.end,e.end); }
    else { const n={type:e.type,start:e.start,end:e.end}; out.push(n); last[e.type]=n; }
  }
  return out;
}
// Index of the first queued thumbnail whose retry time has come, or -1. items: [{due}] in list order.
function thumbPick(items,now){ return items.findIndex(i=>i.due<=now); }
// Milliseconds until the first queued thumbnail is due (0 = now), or null when nothing is queued.
function thumbWakeMs(items,now){ return items.length?Math.max(0,Math.min(...items.map(i=>i.due))-now):null; }
// The queue runs whenever something waits (thumbnails load beside Live; the server refuses with 202 if the camera cannot).
function thumbMayRun(liveOn,queued){ return queued>0; }   // Live no longer pauses thumbnails; a busy camera answers 202 and the loader backs off
// CSS state class of an event's thumbnail slot. The same size in every state; no text, so the list never jumps.
function thumbSlotClass(state){ return state==='show'?'th-ok':state==='off'?'th-off':state==='failed'?'th-fail':state==='flight'?'th-load':'th-wait'; }
/* player-logic:end */
const WIN=120;
let msgTimer=null;
function setMsg(t,err){ clearTimeout(msgTimer); const m=$('msg'); m.textContent=t||''; m.classList.toggle('err',!!err); if(err) unfreeze(); }
// Automatic continuation: show "Buffering…" only if the next piece is NOT ready within ms (a ready piece swaps in without any message).
function setMsgLater(t,ms){ clearTimeout(msgTimer); msgTimer=setTimeout(()=>setMsg(t),ms); }
// Hold the last frame of a piece on a canvas over the video while the next piece loads (no black flash between pieces).
let freezeTimer=null;
function freezeFrame(){ try{ if(!v.videoWidth) return; const c=$('freeze'); c.width=v.videoWidth; c.height=v.videoHeight; c.getContext('2d').drawImage(v,0,0); c.style.display='block';
  clearTimeout(freezeTimer); freezeTimer=setTimeout(unfreeze,4000); }catch(e){} }
function unfreeze(){ clearTimeout(freezeTimer); const c=$('freeze'); if(c) c.style.display='none'; }
// Ask the server first so its real error text can be shown (a <video> element cannot read it).
async function preflight(u,opts){
  try{ const r=await fetch(u,opts); if(r.ok) return {ok:true};
       return {ok:false,text:((await r.text()).trim()||`HTTP ${r.status}`)}; }
  catch(e){ return {ok:false,text:'Lost contact with the local server'}; }
}
let listErr='', recNote='', recTimer=null, retryTimer=null, retryN=0, retryKey='';
let evLoading=false, loadSeq=0;
// A list request that gets no answer within 20 s leaves loading and shows the error state (listErr = EV_TIMEOUT).
const EV_TIMEOUT='timeout'; let evGuard=null;
function armEventsGuard(){ clearTimeout(evGuard); evGuard=setTimeout(()=>{ if(!evLoading) return; evLoading=false; evRefreshing=false; listErr=EV_TIMEOUT; renderEvents(); draw(); },20000); }
let cam=null, day=new Date(), dayStart=0, clips=[], events=[], cur=null, camMeta={}, hls=null, liveOn=false;
const hasThumbs=()=>camMeta[cam]?.thumbs!==false||camMeta[cam]?.native_thumbs===true;
const isStream=()=>!!camMeta[cam]?.stream;   // P2P-only camera: playback is one HLS stream, started at a time
const ffCam=()=>!!camMeta[cam]?.ff;           // camera that can stream the faster speeds (see ffScaleFor)
function streamScale(){ if(isStream()) return ffCam()?ffScaleFor(true,rate):'1/1'; return ffCam()?ffScaleFor(false,rate):null; }   // null = full pieces
let streamOn=false, streamFast=false, stream=null, streamTok=0, streamTick=null;
let viewT=0, pps=300/3600, dragging=false, seekTimer=null, W=0, H=96;
const TT=40;   // timeline: hour labels above, track from TT down (96px total: 40px labels, 56px track)
// Status line state: stBase = what the player is doing (set on state change); stLoad = a failed list load; stPoll = the camera's health from /api/status.
let stBase={kind:'idle'}, stLoad=null, stPoll=null, lastStatus='';

const ymd=d=>`${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
const dayLabel=()=>day.toLocaleDateString('en-CA');  // 2026-11-21

async function init(){
  const cams=await (await fetch('/api/cameras')).json();
  // /api/cameras lists the cameras in the order the owner chose (Settings, Order); that order is used as is.
  // Only a verified and enabled camera may be contacted (listing, live, clips). Others are shown disabled; with none,
  // the setup wizard (static/setup.js) owns the screen and this page does not start playback.
  const okCam=c=>!!(c.enabled&&c.verified);
  // Cameras that are not set up stay in the switcher (aria-disabled, with a "Set up" chip); clicking one opens Settings.
  $('chips').innerHTML=cams.map(c=>okCam(c)?`<button class="pb-tab" role="tab" type="button" data-id="${esc(c.id)}">${esc(c.name)}</button>`:
    `<button class="pb-tab" role="tab" type="button" data-id="${esc(c.id)}" aria-disabled="true" title="${c.enabled?'Not set up yet. Click to finish setup.':'Switched off. Click to open Settings.'}">${esc(c.name)}<span class="pb-setup">Set up</span></button>`).join('');
  markCam();
  cams.forEach(c=>camMeta[c.id]=c);
  const pick=cams.filter(okCam);
  if(!pick.length) return;
  let saved=null; try{saved=localStorage.getItem('cam')}catch(e){}
  cam=(pick.find(c=>c.id===saved)||pick[0]).id;
  $('chips').onclick=e=>{const b=e.target.closest('[data-id]'); if(!b) return;
    if(b.getAttribute('aria-disabled')==='true'){ if(typeof openSetup==='function') openSetup('settings'); return; }
    if(b.dataset.id!==cam){cam=b.dataset.id; try{localStorage.setItem('cam',cam)}catch(e){} day=new Date(); load({live:true})}};
  $('chips').onkeydown=e=>{ const k=[...$('chips').children], j=tabTarget(e.key,k.indexOf(document.activeElement),k.length);
    if(j===null) return; e.preventDefault(); k[j].focus(); };
  $('live').onclick=()=>{ const a=modeAction(liveOn,'live',onToday()); if(a==='start') startLive(); else if(a==='jump'){ day=new Date(); load({live:true}); } };
  $('recbtn').onclick=()=>{ if(modeAction(liveOn,'recorded',true)==='stop'){ stopLive(); setMsg(clips.length?'Pick a time on the timeline or an event':''); } };
  $('sound').onclick=()=>{ v.muted=!v.muted; paintSound(); };   // Live only; starts muted (startLive)
  $('modeseg').onkeydown=e=>{ if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return; e.preventDefault();
    const b=$(e.key==='ArrowLeft'||e.key==='Home'?'live':'recbtn'); b.focus(); b.click(); };
  resize(); load({live:true});
}
const markCam=()=>{ const k=[...$('chips').children], si=k.findIndex(b=>b.dataset.id===cam);
  k.forEach((b,i)=>{ const on=b.dataset.id===cam; b.classList.toggle('on',on); b.setAttribute('aria-selected',String(on)); b.tabIndex=rovingTabIndex(si,i); });
  $('camname').textContent=si>=0?k[si].firstChild.textContent:'Cameras'; };   // narrow screens: the one switcher button names the camera
const onToday=()=>ymd(day)===ymd(new Date());
const dayText=()=>onToday()?'Today':dayLabel();

// Retry only the recordings list (every minute, up to 6 times) without touching the live view.
async function retryRecs(key,n,recUrl,first){
  if(first){ recTimer=setTimeout(()=>retryRecs(key,1,recUrl,false),60000); return; }
  if(cam+ymd(day)!==key) return;
  try{
    const r=await fetch(recUrl);
    if(r.ok){ clips=await r.json(); recNote=''; renderEvents(); draw(); if(!liveOn) setMsg('Pick a time on the timeline or an event'); return; }
    recNote='Recordings list unavailable: '+(await r.text()).trim()+(n<6?' (retrying)':'');
  }catch(e){}
  renderEvents();
  if(n<6) recTimer=setTimeout(()=>retryRecs(key,n+1,recUrl,false),60000);
}
async function load(opts={}){
  thumbReset(); const seq=++loadSeq; events=[]; clips=[]; listErr=''; recNote=''; evLoading=true; evRefreshing=false; armEventsGuard(); renderEvents(); draw();
  markCam(); $('dlabel').textContent=dayText(); $('date').value=dayLabel();
  setTip($('live'),modeCaption(true)+(onToday()?'':' Live jumps to today.'));
  stLoad=null; setBase({kind:'idle'});
  updatePtz();
  dayStart=new Date(day.getFullYear(),day.getMonth(),day.getDate()).getTime()/1000;
  unfreeze(); const wasLive=liveOn; await stopStream(); stopLive(); v.pause(); v.removeAttribute('src'); cur=null; lastSel=null; setMsg('Loading…');
  const isToday=ymd(day)===ymd(new Date());
  const wantLive=(opts.live||wasLive)&&isToday;   // Live is the default view for a camera
  try{
    const recUrl=`/api/recordings?cam=${cam}&date=${ymd(day)}`;
    // Baby Room's SD video can't be played, so its recordings list is not needed (it also needs a camera session slot).
    const [r,e]=await Promise.all([fetch(recUrl),fetch(`/api/events?cam=${cam}&date=${ymd(day)}`)]);
    if(!e.ok) throw new Error(await e.text());
    if(seq!==loadSeq) return;
    events=await e.json(); listErr=''; recNote=''; clearTimeout(recTimer);
    if(r&&r.ok) clips=await r.json();
    else if(r){ clips=[]; recNote='Recordings list unavailable: '+(await r.text()).trim(); retryRecs(cam+ymd(day),0,recUrl,true); }
    else clips=[];
  }catch(err){
    if(seq!==loadSeq) return;
    clips=[];events=[];evLoading=false;
    const key=cam+ymd(day); if(key!==retryKey){ retryKey=key; retryN=0; }
    const w=/waiting (\d+) s/.exec(err.message);                 // the server is pacing logins
    const slots=/session slots/.test(err.message);               // the camera has no free session slot
    let delay=0;
    if(w) delay=(+w[1]+2)*1000;
    else if(slots&&retryN<6){ retryN++; delay=60000; }            // slots free up as old sessions time out
    listErr=err.message+(delay?` (retrying automatically in ${Math.round(delay/1000)} s)`:slots?' (gave up retrying: close the Tapo app / other viewers and reload)':'');
    stLoad=slots?{kind:'busy',secs:delay?60:0}:null; paintStatus();
    setMsg('Error: '+err.message,true); renderEvents(); draw();
    clearTimeout(retryTimer);
    if(delay) retryTimer=setTimeout(()=>{ if(cam+ymd(day)===key) load({live:wantLive}); },delay);
    if(wantLive) startLive();
    return}
  evLoading=false; setMsg('');
  viewT=events.length?events[events.length-1].start:(clips.length?clips[clips.length-1].end-5:dayStart+43200);
  renderEvents(); draw(); setMsg(clips.length?'Pick a time on the timeline or an event':'');
  if(wantLive) startLive();
}

/* ---------- timeline (canvas) ---------- */
function resize(){
  const dpr=devicePixelRatio||1; W=cv.clientWidth; cv.width=W*dpr; cv.height=H*dpr; ctx.setTransform(dpr,0,0,dpr,0,0); draw();
}
addEventListener('resize',resize);
// Playhead x: centred, except on today where the future is empty - then it slides right so "now" sits near the right edge.
let ax=0;
function anchor(){
  const mid=W/2;
  if(ymd(day)!==ymd(new Date())) return mid;
  const right=Math.min(W*0.92,W-70);
  return Math.min(right,Math.max(mid,right-(Date.now()/1000-viewT)*pps));
}
const x2t=x=>viewT+(x-ax)/pps, t2x=t=>ax+(t-viewT)*pps;
let hatchKey='', hatchPat=null;
// Diagonal hatching in the given colour (half transparent), cached per colour: the "no footage" fill.
function hatchFill(color){
  if(hatchKey===color&&hatchPat) return hatchPat;
  const k=document.createElement('canvas'); k.width=k.height=8; const g=k.getContext('2d');
  g.strokeStyle=color; g.globalAlpha=.5; g.lineWidth=1.5; g.beginPath();
  g.moveTo(-2,10); g.lineTo(10,-2); g.moveTo(-2,2); g.lineTo(2,-2); g.moveTo(6,10); g.lineTo(10,6); g.stroke();
  hatchKey=color; hatchPat=ctx.createPattern(k,'repeat'); return hatchPat;
}
function draw(){
  ax=anchor();
  const cs=getComputedStyle(document.documentElement);
  ctx.clearRect(0,0,W,H);
  ctx.save(); ctx.beginPath(); if(ctx.roundRect) ctx.roundRect(0,TT,W,H-TT,8); else ctx.rect(0,TT,W,H-TT); ctx.clip();   // a rounded strip
  ctx.fillStyle=cs.getPropertyValue('--track'); ctx.fillRect(0,TT,W,H-TT);
  const t0=x2t(0), t1=x2t(W);   // marks: hatching where there is NO footage, then the events as tall rounded bars
  if(clips.length){
    const isToday=ymd(day)===ymd(new Date()), hi=isToday?Math.min(dayStart+86400,Date.now()/1000):dayStart+86400;
    ctx.fillStyle=hatchFill(cs.getPropertyValue('--rec').trim());
    for(const g of gapsOf(clips,dayStart,hi,Math.max(10,4/pps))){ if(g.end<t0||g.start>t1) continue; const gx=t2x(g.start); ctx.fillRect(gx,TT,Math.max(1,(g.end-g.start)*pps),H-TT); }
  }
  for(const m of mergeEvents(events.filter(e=>e.end>=t0&&e.start<=t1&&shown(e)),6/pps)){
    ctx.fillStyle=T(m.type).c; const ex=t2x(m.start), ew=Math.max(3,(m.end-m.start)*pps);
    if(ctx.roundRect){ ctx.beginPath(); ctx.roundRect(ex,TT+8,ew,H-TT-16,Math.min(3,ew/2)); ctx.fill(); } else ctx.fillRect(ex,TT+8,ew,H-TT-16); }
  ctx.restore();
  const now=Date.now()/1000;   // "now" marker (today only), 2px in the alert colour
  if(ymd(day)===ymd(new Date())&&now>=t0&&now<=t1){ ctx.fillStyle=cs.getPropertyValue('--c-bad'); ctx.fillRect(t2x(now)-1,TT,2,H-TT); }
  ctx.fillStyle=cs.getPropertyValue('--mut'); ctx.font='500 12px system-ui'; ctx.textAlign='center';
  const step=pps>0.03?3600:pps>0.012?3*3600:6*3600;
  for(let t=Math.ceil(t0/step)*step;t<=t1;t+=step){ const x=t2x(t);
    if(Math.abs(x-ax)>46) ctx.fillText(hh(t),x,16);   // skip a label that would sit under the time bubble
    ctx.fillRect(x-1,24,2,6); }
  $('bubble').textContent=hm(viewT); $('bubble').style.left=ax+'px'; $('head').style.left=ax+'px';
  paintSlider();
}
// The timeline is a slider for the keyboard: its value is the time of day, read out in full.
function paintSlider(){ const n=String(Math.min(86399,Math.max(0,Math.round(viewT-dayStart)))), tx=clockFull(viewT);
  if(cv.getAttribute('aria-valuenow')!==n) cv.setAttribute('aria-valuenow',n);
  if(cv.getAttribute('aria-valuetext')!==tx) cv.setAttribute('aria-valuetext',tx);
  const vc=!!(v.currentSrc||v.srcObject); if(v.controls!==vc) v.controls=vc; }   // native controls only once a source is loaded (no bar over the loading state)
const clampT=t=>Math.min(dayStart+86399,Math.max(dayStart,t));
cv.addEventListener('pointerdown',e=>{dragging=true; cv.setPointerCapture(e.pointerId); cv._x=e.clientX; v.pause()});
cv.addEventListener('pointermove',e=>{ if(!dragging) return; viewT=clampT(viewT-(e.clientX-cv._x)/pps); cv._x=e.clientX; draw(); });
cv.addEventListener('pointerup',()=>{ if(!dragging) return; dragging=false; play(viewT); });
cv.addEventListener('keydown',e=>{ const s=timelineKeyStep(e.key,e.shiftKey); if(s===null) return; e.preventDefault();
  viewT=clampT(viewT+s); draw(); clearTimeout(seekTimer); seekTimer=setTimeout(()=>play(viewT),350); });   // same seek as the wheel
cv.addEventListener('wheel',e=>{ e.preventDefault();
  if(Math.abs(e.deltaX)>Math.abs(e.deltaY)) viewT=clampT(viewT+e.deltaX/pps);
  else pps=Math.min(1.5,Math.max(0.004,pps*Math.exp(-e.deltaY*0.0015)));
  draw(); clearTimeout(seekTimer); seekTimer=setTimeout(()=>play(viewT),350); },{passive:false});

/* ---------- playback (2-minute windows fetched from the camera) ---------- */
const url=w=>`/api/clip?cam=${cam}&start=${w.start}&end=${w.end}`;
let rate=1; try{ rate=+localStorage.getItem('rate')||1; }catch(e){}
if(![1,2,4,8,16].includes(rate)) rate=1;
function applyRate(){
  const lock=liveOn||(streamOn&&!streamFast&&!ffCam());
  const shown=lock?1:rate;   // live and streamed (normal) playback are always real-time
  $('speed').classList.toggle('live',lock); $('speed').setAttribute('aria-disabled',String(lock));
  updatePtz();
  [...$('speed').children].forEach(b=>{ const on=+b.dataset.r===shown; b.classList.toggle('on',on); b.setAttribute('aria-pressed',String(on)); });
  if(streamOn&&streamFast){ v.defaultPlaybackRate=ffPlayRate(); v.playbackRate=ffPlayRate(); v.muted=true; }   // key frames only: no sound
  else if(!lock){ v.defaultPlaybackRate=rate; v.playbackRate=rate; v.muted=rate>=4; }   // sound is useless at 4x and up
  paintStatus();
}
// The speed the picture plays at: live and streamed normal playback are always real time.
function shownRate(){ return (liveOn||(streamOn&&!streamFast&&!ffCam()))?1:rate; }
$('speed').onclick=e=>{ const b=e.target.closest('button'); if(!b||liveOn||(streamOn&&!streamFast&&!ffCam())) return; const before=streamScale(); rate=+b.dataset.r;
  try{ localStorage.setItem('rate',rate); }catch(e){} applyRate();
  if((cur||streamOn)&&before!==streamScale()) play(viewT); };   // a different scale (or pieces <-> stream) means a different source: restart there
const QUICK=20;   // first piece after a click/seek: short, so playback starts within a couple of seconds
// A piece starts exactly at ts (an event's start, or where you scrubbed) and is len seconds long.
function pieceAt(ts,len){ ts=Math.floor(ts); const c=clips.find(c=>ts>=c.start&&ts<c.end); if(!c) return null;
  return {start:ts,end:Math.min(c.end,ts+len)}; }
/* ff-policy:begin */
// Camera scales: a stream asked for `scale` (n/1) is re-stamped so that played at normal rate it runs at n x real time: 2/1 = full frames,
// 4/1, 8/1 and 16/1 = key frames only (about 2 pictures per second, no sound). Cameras that can only stream playback (P2P) stream every speed;
// the others stream only 8x and 16x (slower speeds use full pieces).
const ffScaleFor=(p2p,rate)=>(p2p||rate>=8)?rate+'/1':null;
const ffFactor=scale=>+String(scale).split('/')[0]||1;     // 1 s of stream = factor s of recording
const ffPlayRate=()=>1;
const ffViewT=(start,base,t,factor)=>start+Math.max(0,t-base)*factor;      // stream time -> recording time
const ffSeekTo=(start,base,ts,factor)=>base+(ts-start)/factor;             // recording time -> stream time
/* ff-policy:end */
/* prefetch-policy:begin */
const PF_DEFAULT_SPV=0.07;   // wall seconds needed to fetch one second of video (about 14x); learned per camera below
function pfDepth(rate,spv){ if(rate<=1) return 1; return Math.min(3,Math.ceil((spv||PF_DEFAULT_SPV)*rate)+1); }   // pieces to keep queued ahead
function pfLen(rate,k,base){ return (rate<4||k<1)?base:Math.min(base,[40,60,90][k-1]||base); }   // fast playback: pieces grow 40, 60, 90 s up to the base, so a freeze is never one long wait
function pfLearn(old,sec,len){ if(!(sec>=0.6)||!(len>0)) return old; const x=sec/len; return old?0.6*old+0.4*x:x; }   // ignore cache hits
/* prefetch-policy:end */
const spvByCam={};   // learned seconds-per-video-second, per camera
const contLen=(k=0)=>camMeta[cam]?.win||pfLen(rate,k,WIN);   // P2P cameras serve ~1x realtime: small windows (win); others: ramp up to WIN seconds
function nextWindow(w){ const c=clips.find(c=>w.end>=c.start&&w.end<c.end)||clips.find(c=>c.start>=w.end); if(!c) return null;
  const k=(w.k||0)+1, n=pieceAt(Math.max(w.end,c.start),contLen(k)); if(n) n.k=k; return n; }
// Queue the next piece(s) on the server while the current one plays. Range 0-0 makes the server download the
// piece into its cache without sending it to the browser.
let pfToken=0;
const pfQueued=new Set(), pfDone=new Set();   // pieces requested from the server / known to be in its cache already
async function prefetchAhead(from){
  const tok=++pfToken; let w=from; const depth=pfDepth(rate,spvByCam[cam]), c=cam;
  for(let i=0;i<depth;i++){
    w=nextWindow(w); if(!w||w.end>=Date.now()/1000-90||tok!==pfToken) return;
    const key=c+'_'+w.start+'_'+w.end; if(pfQueued.has(key)) continue;
    pfQueued.add(key); const t0=performance.now();
    try{ const r=await fetch(url(w),{headers:{Range:'bytes=0-0'}}); if(!r.ok){ pfQueued.delete(key); return; }
      spvByCam[c]=pfLearn(spvByCam[c],(performance.now()-t0)/1000,w.end-w.start); pfDone.add(key); }
    catch(e){ pfQueued.delete(key); return; }
  }
}
async function releaseLive(c){   // ask the server to close the live relay now (frees the camera's video slot)
  try{ const r=await fetch('/api/live/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cam:c})}); return r.ok; }
  catch(e){ return false; }
}
async function play(ts,auto=true,len=QUICK,loadingMsg='Loading…',k=0){
  if(!clips.length){ setMsg('The recordings list is not loaded yet'+(recNote?' ('+recNote+')':''),true); return; }
  if(streamScale()) return startStream(ts,loadingMsg==='Loading…'?'Starting playback…':loadingMsg);
  if(streamOn) await stopStream();   // back to full pieces from a key-frame stream
  if(k===0) pfToken++;   // a fresh seek/click: stop queueing pieces for the old position
  ts=Math.floor(ts);   // scrubbing gives fractional seconds; the clip API takes whole seconds
  const wasLive=liveOn||!!hls; stopLive();
  let w=pieceAt(ts,len);
  if(!w){ const c=clips.find(c=>c.start>=ts)||clips[clips.length-1]; ts=c.start; w=pieceAt(ts,len); }
  if(!w) return;
  w.k=k;
  w.end=Math.min(w.end,Math.floor(Date.now()/1000)-90);   // camera cannot serve the last ~90 s
  if(w.end-w.start<2){ setBase({kind:'recording'}); return; }
  cur=w; const my=w; if(k>0) setMsgLater(loadingMsg,700); else setMsg(loadingMsg); syncList();
  if(wasLive) await releaseLive(cam);                       // instant on a current server; older ones ignore it
  if(cur!==my) return;
  const tf0=performance.now();
  const wkey=cam+'_'+w.start+'_'+w.end;
  let pf=pfDone.has(wkey)?{ok:true}:await preflight(url(w),{headers:{Range:'bytes=0-0'}});   // triggers the download once, surfaces errors (skipped when already prefetched)
  if(pf.ok&&!pfDone.has(wkey)) spvByCam[cam]=pfLearn(spvByCam[cam],(performance.now()-tf0)/1000,w.end-w.start);
  if(!pf.ok&&pf.text.includes('live view is open')){   // older server: the relay releases the camera ~8 s after we stop
    setMsg('Switching from live view to the recording…');
    await new Promise(r=>setTimeout(r,9000));
    if(cur!==my) return;
    pf=await preflight(url(w),{headers:{Range:'bytes=0-0'}});
  }
  if(cur!==my) return;
  if(!pf.ok){
    const g=/last login was \d+ s ago, waiting (\d+) s/.exec(pf.text);   // the login gate's short, temporary refusal: wait and retry by itself
    if(g){ setMsg(pf.text+' (retrying automatically)',true);
      setTimeout(()=>{ if(cur===my) play(my.start,auto,len,loadingMsg,my.k||0); },(+g[1]+3)*1000); return; }
    setMsg(pf.text,true); return; }
  if(rate>1&&!w.pf){ w.pf=1; prefetchAhead(w); }   // fast playback: queue the next pieces now, before this one even starts playing
  v.src=url(w);
  v.onloadedmetadata=()=>{ if(cur!==my) return; v.currentTime=0; setMsg(''); applyRate();
    if(auto) v.play(); };
  v.onerror=()=>{ if(cur===my) setMsg('Could not play the video file',true); };
}
v.addEventListener('timeupdate',()=>{ if(cur&&!dragging){ viewT=cur.start+v.currentTime; draw(); syncList();
  // above 1x a piece ends quickly, so queue the next ones as soon as this one starts; at 1x wait until it is part-way
  const early=rate>1?0.5:(v.duration&&v.duration<=40?1:v.duration/2);
  if(!cur.pf&&v.duration&&v.currentTime>early){ cur.pf=1; prefetchAhead(cur); } }});
v.addEventListener('ended',()=>{ const n=cur&&nextWindow(cur); if(n){ freezeFrame(); play(n.start,true,n.end-n.start,'Buffering the next piece…',n.k); } });
// the first frame of the next piece is on screen: release the held frame
v.addEventListener('playing',()=>{ if(v.requestVideoFrameCallback) v.requestVideoFrameCallback(unfreeze); else setTimeout(unfreeze,80);
  if(!liveOn&&(cur||streamOn)) setBase({kind:'recorded',at:cur?cur.start:viewT}); });
v.addEventListener('volumechange',()=>paintSound());   // the speaker button shows whether the sound is off

/* ---------- streamed playback (P2P-only cameras: one session feeds an HLS stream on the server) ---------- */
const postJSON=(u,b,extra={})=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),...extra});
function snapToClip(ts){ ts=Math.floor(ts); if(clips.some(c=>ts>=c.start&&ts<c.end)) return ts;
  const c=clips.find(c=>c.start>=ts)||clips[clips.length-1]; return c?c.start:ts; }
async function stopStream(server=true){
  const c=stream?.cam, was=streamOn||!!stream;
  ++streamTok; streamOn=false; streamFast=false; clearInterval(streamTick); stream=null;
  if(was){ if(hls){ hls.destroy(); hls=null; } v.onplaying=null; v.onerror=null; v.pause(); v.removeAttribute('src'); v.load(); }
  applyRate();
  if(server&&c){ try{ await postJSON('/api/play/stop',{cam:c}); }catch(e){} }
}
async function startStream(ts,msg='Starting playback…'){
  if(!clips.length){ setMsg('The recordings list is not loaded yet'+(recNote?' ('+recNote+')':''),true); return; }
  ts=Math.min(snapToClip(ts),Math.floor(Date.now()/1000)-90);   // the camera has nothing for the last ~90 s
  // already inside the running stream (and buffered/produced up to there): just seek, no new session
  const scale=streamScale(), fast=scale!=='1/1', factor=ffFactor(scale);
  if(streamOn&&stream&&stream.cam===cam&&stream.fast===fast&&(stream.factor||1)===factor&&hls&&v.seekable&&v.seekable.length){
    const off=ts-stream.start, t=ffSeekTo(stream.start,stream.base,ts,factor), end=v.seekable.end(v.seekable.length-1);
    if(off>=0&&t<=end-1){ v.currentTime=t; viewT=ts; setMsg(''); return; } }
  const tok=++streamTok, my=cam, wasLive=liveOn||!!hls;
  clearInterval(streamTick); stopLive(); stream=null; streamOn=true; streamFast=fast; cur=null; applyRate(); setMsg(fast?'Starting '+rate+'x…':msg);
  if(wasLive) await releaseLive(my);
  if(tok!==streamTok) return;
  const fail=(text,retry)=>{ if(tok!==streamTok) return; streamOn=false; applyRate();
    const g=/last login was \d+ s ago, waiting (\d+) s/.exec(text);
    if(g&&retry){ setMsg(text+' (retrying automatically)',true); setTimeout(()=>{ if(tok===streamTok&&cam===my) startStream(ts,msg); },(+g[1]+3)*1000); return; }
    setMsg(text,true); };
  let r; try{ r=await postJSON('/api/play/start',{cam:my,ts,scale}); }catch(e){ fail('Could not reach the server',false); return; }
  if(tok!==streamTok) return;
  if(r.status===409) return;                       // replaced by a newer click: that request reports for itself
  if(!r.ok){ fail(await r.text(),true); return; }
  const st=await r.json();
  stream={cam:my,sid:st.sid,start:st.start,base:0,fast,factor,scale};
  let pl=st.playlist;
  for(let i=0;i<60&&!pl;i++){                       // the first segment takes a couple of seconds
    await new Promise(res=>setTimeout(res,500)); if(tok!==streamTok) return;
    let s; try{ s=await (await fetch(`/api/play/status?cam=${my}`)).json(); }catch(e){ continue; }
    if(s.state==='error'||s.state==='ended'){ fail(s.message||'Playback ended',false); return; }
    pl=s.playlist; }
  if(!pl){ fail('The camera did not deliver video in time',false); await stopStream(); return; }
  const pr=fast?ffPlayRate():1; v.defaultPlaybackRate=pr; v.playbackRate=pr; v.muted=true;   // muted start: autoplay is only allowed muted after the async wait
  const startPlay=()=>v.play().then(()=>{ v.playbackRate=pr; },e=>setMsg('Press ▶ to start ('+(e&&e.name||'autoplay blocked')+')'));
  v.onplaying=()=>{ if(streamOn&&tok===streamTok) setMsg(''); };
  const ended=m=>{ if(tok===streamTok) setMsg(m,true); };
  if(window.Hls&&Hls.isSupported()){
    // an EVENT playlist (nothing deleted): start at its beginning, not at the "live edge"
    hls=new Hls({startPosition:0,manifestLoadingMaxRetry:2,levelLoadingMaxRetry:3,fragLoadingMaxRetry:3,backBufferLength:900});
    hls.loadSource(pl); hls.attachMedia(v);
    hls.on(Hls.Events.LEVEL_LOADED,(_,d)=>{ if(stream&&stream.sid===st.sid&&d.details.fragments.length) stream.base=d.details.fragments[0].start; });
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{ setMsg('Starting playback…'); startPlay(); });
    hls.on(Hls.Events.ERROR,(_,d)=>{ if(d.fatal) ended(`Playback error: ${d.details||d.type}. The stream may have ended; pick a time to restart.`); });
  } else if(v.canPlayType('application/vnd.apple.mpegurl')){ v.src=pl; v.onerror=()=>ended('Playback error: the browser could not play the stream'); startPlay(); }
  else { fail('This browser cannot play HLS',false); return; }
  let n=0;
  streamTick=setInterval(async()=>{
    if(tok!==streamTok||!stream) return;
    if(!dragging){ viewT=ffViewT(stream.start,stream.base,v.currentTime,stream.factor||1); draw(); }
    if(++n%5===0){ let s; try{ s=await (await fetch(`/api/play/status?cam=${my}`)).json(); }catch(e){ return; }
      if(tok===streamTok&&s.sid===stream.sid&&(s.state==='ended'||s.state==='error')&&s.message) setBase({kind:'note',text:s.message+' (what is already loaded keeps playing)',tone:'warn'}); }
  },1000);
}
window.addEventListener('pagehide',()=>{ if(stream) postJSON('/api/play/stop',{cam:stream.cam},{keepalive:true}); });

/* ---------- live (RTSP relayed as HLS by the server) ---------- */
let liveTick=null;
async function startLive(){
  await stopStream(); stopLive(); liveOn=true; cur=null; markMode(); setBase({kind:'connecting'}); applyRate(); setMsg('Connecting to live stream…');
  const src=`/live/${cam}/index.m3u8`;
  const pf=await preflight(src);          // starts the relay and reports why it could not start
  if(!liveOn) return;
  if(!pf.ok){ setMsg(pf.text,true); liveOn=false; markMode(); setBase({kind:'idle'}); applyRate(); return; }
  const fail=m=>{ setMsg(m,true); };
  // The wait above can outlast the browser's "recent click" window, and browsers refuse
  // unmuted autoplay without one. Start muted (always allowed); unmute with the player controls.
  v.defaultPlaybackRate=1; v.playbackRate=1; v.muted=true;
  const startPlay=()=>v.play().then(()=>{},e=>setMsg('Press ▶ to start the stream ('+(e&&e.name||'autoplay blocked')+')'));
  v.onplaying=()=>{ if(liveOn){ setMsg(''); setBase({kind:'live'}); console.debug('live picture', v.videoWidth+'x'+v.videoHeight); } };
  if(window.Hls&&Hls.isSupported()){
    hls=new Hls({liveSyncDurationCount:2,manifestLoadingMaxRetry:1,levelLoadingMaxRetry:2});
    hls.loadSource(src); hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{ setMsg('Starting playback…'); startPlay(); });
    hls.on(Hls.Events.ERROR,(_,d)=>{
      if(d.fatal) fail(`Live stream error: ${d.details||d.type} (${d.type}). Camera busy or unreachable?`);
    });
  } else if(v.canPlayType('application/vnd.apple.mpegurl')){   // Safari
    v.src=src; v.onerror=()=>{ if(liveOn) fail('Live stream error: the browser could not play the HLS stream'); }; startPlay();
  } else { fail('This browser cannot play HLS'); return; }
  liveTick=setInterval(()=>{
    if(liveOn&&!dragging&&ymd(day)===ymd(new Date())){ viewT=Date.now()/1000; draw(); }
  },1000);
}
function stopLive(){
  if(!liveOn&&!hls) return;
  liveOn=false; markMode(); clearInterval(liveTick); setBase({kind:'idle'}); applyRate();
  if(hls){ hls.destroy(); hls=null; }
  v.onplaying=null; v.onerror=null; v.muted=false; v.pause(); v.removeAttribute('src'); v.load();
  thumbPump();   // thumbnails were waiting for Live to close
}

// Live/Recorded control: the two segments mirror liveOn (aria-checked, one tab stop, arrows move between them).
function markMode(){ const l=$('live'), r=$('recbtn');
  l.classList.toggle('on',liveOn); r.classList.toggle('on',!liveOn);
  l.setAttribute('aria-checked',String(liveOn)); r.setAttribute('aria-checked',String(!liveOn));
  l.tabIndex=liveOn?0:-1; r.tabIndex=liveOn?-1:0; paintSound(); }
// Tooltip text for a control: data-tip (CSS ::after on hover and focus) and aria-description (screen readers).
function setTip(el,t){ el.dataset.tip=t; el.setAttribute('aria-description',t); }
// Speaker button (Live only): aria-pressed = sound on. Live starts muted; the button toggles v.muted.
function paintSound(){ const b=$('sound'); b.hidden=!liveOn; b.setAttribute('aria-pressed',String(!v.muted)); b.dataset.tip=v.muted?'Sound off':'Sound on'; }
// Status line: paints #livestat only when its copy or tone changes (never once a second).
function setBase(st){ stBase=st; paintStatus(); }
function paintStatus(){
  const st=stLoad||stPoll||stBase;
  const line=statusLine({...st, name:camMeta[cam]?.name||cam||'', rate:shownRate()});
  const key=line.tone+'|'+line.text;
  if(key===lastStatus) return; lastStatus=key;
  const el=$('livestat'); el.textContent=line.text; el.className='pb-status t-'+line.tone; el.hidden=!line.text;   // no text: no line, no dot
}

/* ---------- Events list (grouped by hour, one tab stop, roving Up/Down) ---------- */
const sel=new Set();  // selected event types; empty = show all
const shown=e=>!sel.size||sel.has(e.type);
let filterSig='', evRefreshing=false;
// Filter chips are rebuilt only when the types or counts change (keyboard focus survives a press); pressed state is set in place.
function renderFilters(loading){
  const box=$('filters');
  if(loading){ box.innerHTML='<span class="chsk w1"></span><span class="chsk w2"></span><span class="chsk w3"></span>'; filterSig=''; return; }
  const counts={}; events.forEach(e=>counts[e.type]=(counts[e.type]||0)+1);
  [...sel].forEach(t=>{ if(!counts[t]) sel.delete(t); });
  const keys=Object.keys(counts).sort((a,b)=>counts[b]-counts[a]);
  if(!filtersVisible(keys.length)){ sel.clear(); box.innerHTML=''; filterSig=''; return; }   // zero or one type: no filter row
  const sig=keys.map(t=>t+':'+counts[t]).join(',');
  if(sig!==filterSig){ filterSig=sig;
    box.innerHTML=keys.map(t=>`<button class="chip" type="button" data-t="${t}"><i class="dot" data-type="${t}"></i>${esc(T(+t).n)} <span>${counts[t]}</span></button>`).join('');
    box.querySelectorAll('i[data-type]').forEach(i=>i.style.setProperty('background',T(+i.dataset.type).c)); }
  box.querySelectorAll('button[data-t]').forEach(b=>{ const on=sel.has(+b.dataset.t); b.classList.toggle('on',on); b.setAttribute('aria-pressed',String(on)); });
}
$('filters').onclick=e=>{ const b=e.target.closest('button[data-t]'); if(!b) return; const t=+b.dataset.t;
  sel.has(t)?sel.delete(t):sel.add(t); lastSel=null; renderEvents(); draw(); };
// A state block: an alert (role=alert), a note (role=status) or an empty-state line with one ghost button.
function evState(cls,role,text,btn){
  const d=document.createElement('div'); d.className='ev-state'+(cls?' '+cls:''); if(role) d.setAttribute('role',role);
  const p=document.createElement('p'); p.textContent=text; d.append(p);
  if(btn){ const b=document.createElement('button'); b.type='button'; b.className='pb-btn'; b.id=btn.id; b.textContent=btn.label; b.onclick=btn.go; d.append(b); }
  return d;
}
function setEvTabStop(i){ [...$('evs').querySelectorAll('.evmain')].forEach((b,k)=>{ b.tabIndex=k===i?0:-1; }); }
/* ---------- event thumbnails: visible rows only, one request at a time, retried while the camera is busy ---------- */
// TH.items: ts -> {ts, el, vis, state, tries, hard, due, url}. state: idle | queued | flight | show | off | failed | gaveup.
// 'gaveup' = busy for THUMB_MAX_TRIES attempts (it is tried again when its row scrolls into view). Reset on every camera or day change.
const TH={gen:0,items:new Map(),timer:null,inflight:false,io:null,cache:new Map()};   // cache: 'cam:ts' -> blob URL, kept across camera/day changes
const TH_CACHE_MAX=400;
function thumbRemember(key,url){ TH.cache.delete(key); TH.cache.set(key,url); while(TH.cache.size>TH_CACHE_MAX){ const k=TH.cache.keys().next().value; URL.revokeObjectURL(TH.cache.get(k)); TH.cache.delete(k); } }
function thumbReset(){
  TH.gen++; clearTimeout(TH.timer); TH.timer=null; TH.inflight=false;
  TH.items=new Map();
}
function thumbObserver(){
  if(!TH.io) TH.io=new IntersectionObserver(entries=>{
    for(const en of entries){
      const it=TH.items.get(+en.target.dataset.ts);
      if(!it||it.el!==en.target) continue;   // a row that was rendered away
      it.vis=en.isIntersecting;
      if(it.vis&&(it.state==='idle'||it.state==='gaveup')){ if(it.state==='gaveup') it.tries=0; it.state='queued'; }
    }
    thumbPump();
  });
  return TH.io;
}
// Called on every render of the list: the rows are rebuilt, so nothing is visible until the new rows report in.
function thumbDetach(){
  if(TH.io) TH.io.disconnect();
  for(const it of TH.items.values()) it.vis=false;
}
function thumbAttach(ts,el){
  if(!TH.items.has(ts)){ const u=TH.cache.get(`${cam}:${ts}`); TH.items.set(ts,{ts,el,vis:false,state:u?'show':'idle',tries:0,hard:0,due:0,url:u||null}); }
  const it=TH.items.get(ts); it.el=el;
  if(it.state==='show'&&it.url) thumbPaint(it);   // remembered from an earlier visit: paint it now (the row was just built without it)
  thumbObserver().observe(el);
}
function thumbPaint(it){
  const el=it.el; if(!el) return;
  el.className=`th ${thumbSlotClass(it.state)}`;
  const img=el.querySelector('.th-img');
  if(it.state==='show'&&!img){
    const i=document.createElement('img'); i.className='th-img fade'; i.alt=''; i.src=it.url;
    i.addEventListener('load',()=>i.classList.add('in'));
    i.addEventListener('error',()=>{ it.state='failed'; thumbPaint(it); });
    el.prepend(i);
  } else if(it.state!=='show'&&img) img.remove();
}
// Starts the next fetch when Live is closed and a visible thumbnail is due; otherwise arms a timer for the next retry.
function thumbPump(){
  clearTimeout(TH.timer); TH.timer=null;
  if(TH.inflight) return;
  const list=[...TH.items.values()].filter(it=>it.vis&&it.state==='queued');
  if(!thumbMayRun(liveOn,list.length)) return;
  const now=Date.now(), i=thumbPick(list,now);
  if(i<0){ TH.timer=setTimeout(thumbPump,thumbWakeMs(list,now)); return; }
  thumbFetch(list[i]);
}
async function thumbFetch(it){
  const gen=TH.gen, c=cam;
  TH.inflight=true; it.state='flight'; it.tries++;
  let status=0, blob=null, retryS=0;
  try{
    const r=await fetch(`/api/thumb?cam=${encodeURIComponent(c)}&ts=${it.ts}`);
    status=r.status; retryS=+r.headers.get('Retry-After')||0;
    if(status===200) blob=await r.blob();
  }catch(e){ status=0; blob=null; }
  if(status!==200&&status!==202&&status!==404&&status!==409) it.hard++;
  TH.inflight=false;
  if(gen!==TH.gen) return;   // the camera or day changed while this was in flight
  const o=thumbOutcome(status,{tries:it.tries,hard:it.hard},retryS);
  if(o.do==='show'){ it.url=URL.createObjectURL(blob); it.state='show'; thumbRemember(`${c}:${it.ts}`,it.url); }
  else if(o.do==='wait'){ it.state='queued'; it.due=Date.now()+o.ms; }
  else it.state=o.why==='busy'?'gaveup':o.why;
  thumbPaint(it);
  thumbPump();
}
function evRow(e){
  const ty=T(e.type), d=document.createElement('div'); d.className='ev'; d.dataset.s=e.start;
  if(String(e.start)===lastSel) d.classList.add('on');
  const it=TH.items.get(e.start), shown=it&&it.state==='show'&&it.url;
  const slot=hasThumbs()?thumbSlotClass(it?it.state:'idle'):'th-off';
  d.innerHTML=`<button class="evmain" type="button" tabindex="-1" aria-label="${esc(clockFull(e.start))}, ${esc(ty.n)}">
      <span class="th ${slot}" data-ts="${e.start}">${shown?`<img class="th-img" alt="" src="${it.url}">`:''}<svg class="th-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="3" y="6" width="12" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3z"/></svg><b>${mmss(e.end-e.start)}</b></span>
      <span class="evtxt"><span class="t">${clockFull(e.start)}</span><span class="k"><i class="dot" data-type="${e.type}"></i>${esc(ty.n)}</span></span></button>
    <button class="exbtn" type="button" tabindex="-1" aria-label="${esc(exportLabel(e.start))}" title="Export this event"><svg class="pb-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>`;
  d.querySelector('.dot').style.setProperty('background',ty.c);
  d.querySelector('.exbtn').onclick=ev=>{ ev.stopPropagation(); openExport(e.start-5,e.end+5); };
  d.onclick=()=>{ setEvSheet(false); viewT=e.start; draw(); play(e.start); };
  if(hasThumbs()) thumbAttach(e.start,d.querySelector('.th'));
  return d;
}
function renderEvents(){
  thumbDetach();
  const list=events.filter(shown);
  const st=eventsPanelState({loading:evLoading,refreshing:evRefreshing,error:listErr,total:events.length,shown:list.length});
  const skeleton=st==='loading';
  renderFilters(skeleton);
  const chip=skeleton?'':countChip(events.length,list.length,sel.size>0);
  $('evcount').textContent=chip; $('evpeekn').textContent=chip;
  const rb=$('evrefresh'); rb.setAttribute('aria-disabled',String(evLoading)); rb.classList.toggle('spin',evLoading);
  const el=$('evs'); el.setAttribute('aria-busy',String(skeleton));
  if(skeleton){ el.innerHTML='<span class="pb-sr" role="status">Loading events…</span>'+'<div class="evsk"></div>'.repeat(5); return; }
  el.replaceChildren();
  if(st==='error') el.append(evState('ev-alert','alert',listErr===EV_TIMEOUT?"Couldn't load events. Retry.":"Couldn't load events: "+listErr,{id:'evretry',label:'Retry',go:()=>$('evrefresh').click()}));
  if(recNote&&!listErr) el.append(evState('ev-warn','status','Recordings list unavailable. Events still show; retrying.'));
  if(st==='empty') el.append(evState('','','No events on this day.',{id:'evprev',label:'Show previous day',go:()=>shift(-1)}));
  else if(st==='filtered-empty') el.append(evState('','','No events match these filters.',{id:'evclear',label:'Clear filters',go:()=>{ sel.clear(); lastSel=null; renderEvents(); draw(); }}));
  else if(st==='ready'||events.length) newestFirst(list).forEach(e=>el.append(evRow(e)));   // error with rows from before: keep them
  const mains=[...el.querySelectorAll('.evmain')], on=mains.findIndex(b=>b.closest('.ev').classList.contains('on'));
  setEvTabStop(on<0?0:on);
}
// One tab stop for the list; Up/Down/Home/End move between rows, Right goes to the row's export button, Left back, Enter plays (native click).
$('evs').addEventListener('keydown',e=>{
  const btn=e.target.closest('.evmain,.exbtn'); if(!btn) return;
  if(btn.classList.contains('exbtn')){ if(e.key==='ArrowLeft'){ e.preventDefault(); btn.closest('.ev').querySelector('.evmain').focus(); } return; }
  if(e.key==='ArrowRight'){ e.preventDefault(); btn.closest('.ev').querySelector('.exbtn').focus(); return; }
  const mains=[...$('evs').querySelectorAll('.evmain')], j=rovingEventIndex(e.key,mains.indexOf(btn),mains.length);
  if(j===null) return; e.preventDefault(); setEvTabStop(j); mains[j].focus();
});
// Events as a bottom sheet below 600px: the peek handle toggles it; Escape closes it.
function setEvSheet(open){ $('evpanel').classList.toggle('open',open); $('evpeek').setAttribute('aria-expanded',String(open)); }
$('evpeek').onclick=()=>setEvSheet(!$('evpanel').classList.contains('open'));
$('evpanel').addEventListener('keydown',e=>{ if(e.key==='Escape'&&$('evpanel').classList.contains('open')){ e.preventDefault(); setEvSheet(false); $('evpeek').focus(); } });
// Re-fetch only the events list (server-side refresh=1 bypasses the 60 s cache); the player is untouched.
$('evrefresh').onclick=async()=>{
  if(evLoading) return;
  const key=cam+ymd(day), url=`/api/events?cam=${cam}&date=${ymd(day)}&refresh=1`;
  evLoading=true; evRefreshing=true; armEventsGuard(); renderEvents();
  try{
    const r=await fetch(url);
    if(key!==cam+ymd(day)) return;
    if(!r.ok) throw new Error((await r.text()).trim());
    events=await r.json(); listErr=''; lastSel=null;
  }catch(err){ if(key!==cam+ymd(day)) return; listErr=err.message; }
  evLoading=false; evRefreshing=false; renderEvents(); draw();
};
let lastSel=null;
function syncList(){
  const e=events.find(e=>viewT>=e.start&&viewT<=e.end); const k=e?String(e.start):null;
  if(k===lastSel) return; lastSel=k;
  document.querySelectorAll('.ev').forEach(d=>d.classList.toggle('on',d.dataset.s===k));
  const mains=[...$('evs').querySelectorAll('.evmain')], on=mains.findIndex(b=>b.closest('.ev').classList.contains('on'));
  if(on>=0) setEvTabStop(on);
}

/* ---------- date controls ---------- */
const shift=n=>{day=new Date(day.getFullYear(),day.getMonth(),day.getDate()+n); load()};
$('prev').onclick=()=>shift(-1); $('next').onclick=()=>shift(1);
$('dlabel').onclick=()=>$('date').showPicker?.();
$('date').onchange=()=>{const [y,m,d]=$('date').value.split('-').map(Number); if(y){day=new Date(y,m-1,d); load();}};
init();


/* ---------- export (time range -> video+audio or audio only) ---------- */
const toLocalInput=ts=>{const d=new Date(ts*1000);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`};
const fromLocalInput=v=>new Date(v).getTime()/1000;
let expJob=null, expTimer=null;
function openExport(a,b){
  $('exptitle').textContent=`Export: ${camMeta[cam]?.name||cam}`;
  $('expstart').value=toLocalInput(a); $('expend').value=toLocalInput(b);
  $('expmsg').textContent=''; $('explinks').innerHTML=''; $('expbar').style.display='none';
  $('expgo').textContent='Export';
  $('exp').showModal();
}
$('exportbtn').onclick=()=>{
  const e=events.find(e=>viewT>=e.start&&viewT<=e.end);
  e?openExport(e.start-5,e.end+5):openExport(Math.floor(viewT),Math.floor(viewT)+300);
};
function stopPoll(){ clearInterval(expTimer); expTimer=null; }
$('expcancel').onclick=()=>{
  if(expJob&&expTimer) fetch(`/api/export/${expJob}/cancel`,{method:'POST'}).catch(()=>{});
  stopPoll(); $('exp').close();
};
$('expgo').onclick=async()=>{
  const start=Math.floor(fromLocalInput($('expstart').value)), end=Math.floor(fromLocalInput($('expend').value));
  if(!(end>start)){ $('expmsg').textContent='"To" must be after "From".'; return; }
  $('expgo').disabled=true; $('explinks').innerHTML=''; $('expmsg').textContent='Starting…';
  const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cam,start,end,mode:$('expmode').value})});
  if(!r.ok){ $('expmsg').textContent=await r.text(); $('expgo').disabled=false; return; }
  expJob=(await r.json()).id; $('expbar').style.display='block';
  expTimer=setInterval(async()=>{
    try{
      const j=await (await fetch(`/api/export/${expJob}`)).json();
      $('expmsg').textContent=j.message; $('expbar').max=j.total||1; $('expbar').value=j.done||0;
      if(['done','error','cancelled'].includes(j.state)){
        stopPoll(); $('expgo').disabled=false; $('expbar').style.display='none';
        if(j.state==='done') $('explinks').innerHTML=
          `<a href="/exports/${j.file}" download>Download ${j.file} (${(j.size/1048576).toFixed(1)} MB)</a>`+
          `<a href="/exports/${j.file}.json" download>Download timing map (${j.file}.json)</a>`+
          `<div class="exp-note">Also saved in the project's exports/ folder.</div>`;
      }
    }catch(e){ stopPoll(); $('expmsg').textContent='Lost contact with the server'; $('expgo').disabled=false; }
  },1000);
};


/* ---------- camera status (from /api/status: local state, never contacts a camera) ---------- */
const lastHealth={};
async function pollStatus(){
  try{
    const st=await (await fetch('/api/status')).json();
    let line='';   // #camstat: transient camera states only (starting up, video downloads paused)
    const cameBack=[];
    [...$('chips').children].forEach(b=>{
      if(b.getAttribute('aria-disabled')==='true') return;   // not set up: no status dot or tooltip
      const x=st[b.dataset.id]; if(!x) return;
      let level='ok', text=`${x.name}: ${x.connected?'connected':'not connected yet'}, ${x.logins_last_hour} login(s) in the last hour`, transient='';
      const hs=x.health&&x.health.state;
      if(hs==='down'){ level='bad'; text=`${x.name} is not answering on the network (off, unplugged or out of Wi-Fi range)`; }
      else if(hs==='booting'){ level='wait'; text=`${x.name} is starting up (answers ping, services not ready yet)`; transient=text; }
      else if(x.disabled){ level='bad'; text=`${x.name} is disabled: ${x.disabled}`; }
      else if(x.blocked_for_s>0){ level='wait'; text=`${x.name}: login paused for ${Math.ceil(x.blocked_for_s/60)} min to protect the camera from lockout`; }
      else if(x.media_blocked_for_s>0){ level='wait'; text=`${x.name}: video downloads paused for ${Math.ceil(x.media_blocked_for_s/60)} min (camera returned no video)`; transient=text; }
      const prev=lastHealth[b.dataset.id]; lastHealth[b.dataset.id]=hs;
      if(prev&&prev!=='ready'&&hs==='ready') cameBack.push(b.dataset.id);
      b.dataset.s=level; b.title=text;
      if(b.dataset.id===cam) line=transient;
    });
    const el=$('camstat'); if(el.textContent!==line) el.textContent=line; el.style.display=line?'block':'none';   // role=alert: announce only real changes
    const cx=st[cam];
    if(cx){ const chs=cx.health&&cx.health.state;   // stateful problems go to the status line only (not repeated in #camstat)
      stPoll=chs==='down'?{kind:'offline',name:cx.name}:cx.disabled?{kind:'disabled',name:cx.name,reason:cx.disabled}:cx.blocked_for_s>0?{kind:'paused',mins:Math.ceil(cx.blocked_for_s/60)}:null;
      paintStatus(); }
    if(cameBack.includes(cam)){ clearTimeout(retryTimer); retryN=0; load({live:true}); }   // it just came back: reload now
  }catch(e){}
}
setInterval(pollStatus,10000); setTimeout(pollStatus,500);


/* ---------- pan / tilt (cameras marked ptz in cameras.json) ---------- */
let ptzTimer=null;
function updatePtz(){ const on=!!camMeta[cam]?.ptz&&liveOn;   // the pad is only for the live view
  $('ptz').style.display=on?'grid':'none'; if(!on){ $('ptzmsg').textContent=''; ptzStop(); } }
async function ptzStep(d){
  try{
    const r=await fetch('/api/ptz',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cam,dir:d})});
    if(r.ok){ $('ptzmsg').textContent=''; $('ptzmsg').className=''; return true; }
    if(r.status===429) return true;                        // held button outpaced the rate limit: not an error
    const text=(await r.text()).trim();
    if(text.includes('MOTOR_LOCKED_ROTOR')||text.includes('-64304')){   // end of the camera's travel: not a failure
      const m=$('ptzmsg'); m.textContent='Reached the end of the camera\'s '+(d==='up'||d==='down'?'tilt':'pan')+' range'; m.className='info';
      return false;
    }
    $('ptzmsg').className=''; $('ptzmsg').textContent=text; return false;
  }catch(e){ $('ptzmsg').textContent='Lost contact with the local server'; return false; }
}
let ptzHeld=false, ptzBusy=false;
function ptzStop(){ ptzHeld=false; clearInterval(ptzTimer); ptzTimer=null; }
$('ptz').addEventListener('pointerdown',e=>{
  const b=e.target.closest('button'); if(!b) return;
  e.preventDefault(); ptzStop();
  const d=b.dataset.d;
  ptzHeld=true;                                    // the button state decides, not the (later) server reply:
  const tick=async()=>{                            // a quick click then sends exactly one step
    if(!ptzHeld||ptzBusy) return;
    ptzBusy=true; const ok=await ptzStep(d); ptzBusy=false;
    if(!ok) ptzStop();
  };
  tick();
  ptzTimer=setInterval(tick,600);                  // only while the button stays down
});
['pointerup','pointerleave','pointercancel'].forEach(ev=>$('ptz').addEventListener(ev,ptzStop));
addEventListener('pointerup',ptzStop);
addEventListener('blur',ptzStop);


/* ---------- theme: System (default) / Light / Dark ---------- */
const THEMES=['system','light','dark'];
// The Theme group in the overflow menu: its three menuitemradio rows show the choice (aria-checked).
function applyTheme(t){
  const root=document.documentElement;
  if(t==='light'||t==='dark') root.setAttribute('data-theme',t); else root.removeAttribute('data-theme');
  const eff=t==='light'||t==='dark'?t:'system';
  document.querySelectorAll('#theme [data-t]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.t===eff)));
  draw();                                   // the timeline canvas reads the colours when it draws
}
let theme='system'; try{ theme=localStorage.getItem('theme')||'system'; }catch(e){}
if(!THEMES.includes(theme)) theme='system';
applyTheme(theme);
$('theme').addEventListener('click',e=>{ const b=e.target.closest('[data-t]'); if(!b) return;
  theme=b.dataset.t;
  try{ localStorage.setItem('theme',theme); }catch(err){}
  applyTheme(theme);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>draw());   // system theme changed

/* ---------- app bar overflow menu (Add camera, Cameras and passwords, Theme, About) ---------- */
const menuItems=()=>[...$('pbmenu').querySelectorAll('[role="menuitem"],[role="menuitemradio"]')].filter(b=>!b.hidden);
function openMenu(end){ const it=menuItems(); $('pbmenu').hidden=false; $('menubtn').setAttribute('aria-expanded','true');
  (end?it[it.length-1]:it[0])?.focus(); }
function closeMenu(refocus){ if($('pbmenu').hidden) return; $('pbmenu').hidden=true; $('menubtn').setAttribute('aria-expanded','false');
  if(refocus) $('menubtn').focus(); }
$('menubtn').onclick=()=>{ if($('pbmenu').hidden) openMenu(false); else closeMenu(true); };
$('menubtn').addEventListener('keydown',e=>{ if(e.key==='ArrowDown'||e.key==='ArrowUp'){ e.preventDefault(); openMenu(e.key==='ArrowUp'); } });
$('pbmenu').addEventListener('keydown',e=>{ const it=menuItems(), i=it.indexOf(document.activeElement), n=it.length;
  if(e.key==='Escape'){ e.preventDefault(); closeMenu(true); }
  else if(e.key==='Tab'){ closeMenu(false); $('menubtn').focus(); }   // Tab closes the menu and moves on from the button
  else if(e.key==='ArrowDown'||e.key==='ArrowUp'){ e.preventDefault(); it[e.key==='ArrowDown'?(i+1)%n:(i-1+n)%n]?.focus(); }
  else if(e.key==='Home'||e.key==='End'){ e.preventDefault(); it[e.key==='Home'?0:n-1]?.focus(); } });
$('pbmenu').addEventListener('click',e=>{ if(e.target.closest('[role="menuitem"]')) closeMenu(false); });
document.addEventListener('pointerdown',e=>{ if(!$('pbmenu').hidden && !$('pbmenu').contains(e.target) && e.target!==$('menubtn')) closeMenu(false); });
$('addcam').onclick=()=>{ if(typeof openSetupAdd==='function') openSetupAdd(); };
$('aboutbtn').onclick=()=>$('about').showModal();
/* ---------- narrow screens: the camera switcher becomes one button and a sheet (the real #chips stays the source of truth) ---------- */
$('camsel').onclick=()=>{ buildCamSheet(); $('camsheet').showModal(); };
function buildCamSheet(){ const box=$('camlist'); box.replaceChildren();
  [...$('chips').children].forEach(b=>{
    const m=document.createElement('button'); m.type='button'; m.className='pb-mi';
    const name=[...b.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('');
    m.textContent=name+(b.getAttribute('aria-disabled')==='true'?' · Set up':'');
    m.dataset.id=b.dataset.id; if(b.dataset.s) m.dataset.s=b.dataset.s;
    m.setAttribute('aria-current',String(b.dataset.id===cam));
    m.onclick=()=>{ $('camsheet').close(); const t=[...$('chips').children].find(c=>c.dataset.id===m.dataset.id); if(t) t.click(); };
    box.append(m); }); }
$('sheetadd').onclick=()=>{ $('camsheet').close(); if(typeof openSetupAdd==='function') openSetupAdd(); };
$('aboutclose').onclick=()=>$('about').close();
$('about').addEventListener('close',()=>$('menubtn').focus());

markMode();
applyRate();
