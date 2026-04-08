'use strict';
// ─────────────────────── State ────────────────────────────────────────────────
let allEmails = [];
let activeFilter = 'all';
let focusedIdx = -1;
let prevPendingCount = -1;
let wsConnected = false;

// ─────────────────────── Utilities ───────────────────────────────────────────
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function timeAgo(ts){
  if(!ts) return '';
  const d=Math.floor((Date.now()-new Date(ts))/1000);
  if(d<60) return d+'s ago';
  if(d<3600) return Math.floor(d/60)+'m ago';
  if(d<86400) return Math.floor(d/3600)+'h ago';
  return Math.floor(d/86400)+'d ago';
}
function confBar(c){
  const col=c>90?'var(--red)':c>=70?'var(--orange)':'var(--text-4)';
  return `<span style="display:inline-block;width:${Math.round(Math.min(c,100)*.65)}px;height:4px;background:${col};border-radius:2px;vertical-align:middle;margin-right:5px"></span>`;
}
function showToast(msg,type=''){
  const c=document.getElementById('toast-container');
  const t=document.createElement('div');
  t.className='toast'+(type?' toast-'+type:'');
  t.textContent=msg;
  c.appendChild(t);
  requestAnimationFrame(()=>requestAnimationFrame(()=>t.classList.add('show')));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),260);},3500);
}

// ─────────────────────── Tab navigation ──────────────────────────────────────
function switchTab(name, el){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  el.classList.add('active');
  if(name==='analytics') loadAnalytics();
  if(name==='prefs') loadPreferences();
}

// ─────────────────────── WebSocket ───────────────────────────────────────────
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onopen=()=>{
    wsConnected=true;
    document.getElementById('ws-label').textContent='Live — Connected';
    document.getElementById('status-bar').innerHTML='<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);margin-right:5px;animation:pulse 2.5s infinite"></span>WebSocket connected';
  };
  ws.onmessage=(e)=>{
    const msg=JSON.parse(e.data||'{}');
    if(msg.type==='refresh') refresh();
    if(msg.type==='new_plan') showToast(msg.message||'New plan arrived','');
  };
  ws.onclose=()=>{
    wsConnected=false;
    document.getElementById('ws-label').textContent='Reconnecting…';
    document.getElementById('status-bar').textContent='WebSocket disconnected — reconnecting…';
    setTimeout(connectWS,3000);
  };
  ws.onerror=()=>ws.close();
}
connectWS();
setInterval(()=>{if(!wsConnected) refresh();}, 5000);
setInterval(refresh, 8000);
setInterval(loadHealth, 20000);

// ─────────────────────── Email rendering ─────────────────────────────────────
const PRIORITY_BADGE={'high':'badge-high','medium':'badge-medium','low':'badge-low'};
const PRIORITY_LABEL={'high':'HIGH','medium':'MED','low':'LOW'};
const RESPONSE_BADGE={
  auto_executed:'badge-response-auto', approved:'badge-response-approved',
  rejected:'badge-response-rejected',  pending:'badge-response-pending',
  silent_discarded:'badge-response-silent',
};
const RESPONSE_LABEL={
  auto_executed:'⚡ Auto-Executed', approved:'✓ Approved',
  rejected:'✕ Rejected',           pending:'⏳ Awaiting Approval',
  silent_discarded:'— Discarded',
};

function argsPreview(item){
  const args=item.action_args||{};
  if(!item.action||item.action==='no_action'||!Object.keys(args).length) return '';
  let lines=[];
  if(item.action==='send_email'){
    if(args.to) lines.push('To:      '+esc(args.to));
    if(args.subject) lines.push('Subject: '+esc(args.subject));
    if(args.body) lines.push('Body:\n'+esc(args.body));
  } else if(item.action==='create_event'){
    if(args.summary) lines.push('Event:   '+esc(args.summary));
    if(args.start_datetime) lines.push('Start:   '+esc(args.start_datetime));
    if(args.end_datetime)   lines.push('End:     '+esc(args.end_datetime));
  } else if(item.action==='move_file'){
    if(args.source)      lines.push('From: '+esc(args.source));
    if(args.destination) lines.push('To:   '+esc(args.destination));
  } else lines.push(esc(JSON.stringify(args,null,2)));
  if(!lines.length) return '';
  return `<div class="detail-row" style="align-items:flex-start"><span class="detail-label">Action Args</span><div class="args-box">${lines.join('\n')}</div></div>`;
}

function renderEmails(emails){
  const el=document.getElementById('email-list');
  if(!emails.length){el.innerHTML='<p class="empty">No emails in this category</p>';return;}
  el.innerHTML=emails.map((item,idx)=>{
    const pr=item.priority||'low';
    const resp=item.user_response||'pending';
    const isPending=resp==='pending';
    const kws=item.urgency_keywords||[];
    const kwHtml=kws.length?`<div style="padding:0 16px 8px">${kws.map(k=>`<span class="kw-chip">${esc(k)}</span>`).join('')}</div>`:'';
    const altsHtml=(item.alternatives||[]).length?`
      <div class="detail-row"><span class="detail-label">Alternatives</span>
      <div style="font-size:.74rem;color:var(--text-3);flex:1">${item.alternatives.map(a=>`<div style="margin:3px 0">• <b style="color:var(--text-2)">${esc(a.action).replace(/_/g,' ')}</b> (${a.confidence}%) — ${esc(a.reason)}</div>`).join('')}</div></div>`:'';
    const sc=item.scoring||{};
    const scoreRow=sc.base!=null?`<div class="detail-row"><span class="detail-label">Scoring</span>
      <span class="detail-value" style="color:var(--text-3);font-family:'JetBrains Mono',monospace;font-size:.7rem">base=${sc.base} × urgency=${sc.urgency_mult} × history=${sc.history_mult} = ${item.confidence}%</span></div>`:'';
    return `<div class="email-card priority-${pr}" id="card-${item.id}" data-idx="${idx}" tabindex="0">
      <div class="email-header">
        <span class="badge ${PRIORITY_BADGE[pr]||'badge-low'}">${PRIORITY_LABEL[pr]||'?'}</span>
        <span class="email-subject">${esc(item.subject||'(no subject)')}</span>
        <span class="badge badge-conf">${confBar(item.confidence||0)}${item.confidence||0}%</span>
        <span class="badge ${RESPONSE_BADGE[resp]||'badge-response-pending'}">${RESPONSE_LABEL[resp]||resp}</span>
        <span class="email-meta">${timeAgo(item.created_at)}</span>
      </div>
      <div class="email-from">From: ${esc(item.from_addr||'—')}</div>
      ${item.snippet?`<div class="email-snippet">${esc(item.snippet).substring(0,220)}</div>`:''}
      ${kwHtml}
      <div class="email-body">
        <div class="detail-row"><span class="detail-label">Action</span><span class="detail-value action-name">${esc(item.action||'no_action').replace(/_/g,' ')}</span></div>
        <div class="detail-row"><span class="detail-label">Reason</span><span class="detail-value">${esc(item.reason)}</span></div>
        ${item.explanation?`<div class="detail-row"><span class="detail-label">Explanation</span><span class="detail-value" style="color:var(--text-3);font-style:italic">${esc(item.explanation)}</span></div>`:''}
        ${altsHtml}
        ${argsPreview(item)}
        ${scoreRow}
        ${item.call_text?`<div class="detail-row" style="align-items:flex-start"><span class="detail-label">Call Script</span><div class="call-box">${esc(item.call_text)}</div></div>`:''}
      </div>
      ${isPending?`<div class="actions-row">
        <button class="btn btn-approve" onclick="approve('${item.id}')">✓ Approve &amp; Execute</button>
        <button class="btn btn-reject"  onclick="reject('${item.id}')">✕ Reject</button>
      </div>`:''}
    </div>`;
  }).join('');
}

function updateStats(){
  const total=allEmails.length;
  const high =allEmails.filter(e=>e.priority==='high').length;
  const med  =allEmails.filter(e=>e.priority==='medium').length;
  const low  =allEmails.filter(e=>e.priority==='low').length;
  const auto =allEmails.filter(e=>e.user_response==='auto_executed').length;
  const pend =allEmails.filter(e=>e.user_response==='pending').length;
  document.getElementById('stat-total').textContent=total;
  document.getElementById('stat-high').textContent=high;
  document.getElementById('stat-med').textContent=med;
  document.getElementById('stat-low').textContent=low;
  document.getElementById('stat-auto').textContent=auto;
  document.getElementById('stat-pending').textContent=pend;
  document.getElementById('email-count').textContent=total;
  const badge=document.getElementById('pending-badge');
  if(pend>0){badge.textContent=pend;badge.style.display='';}
  else badge.style.display='none';
  if(prevPendingCount>=0&&pend>prevPendingCount)
    showToast(`${pend-prevPendingCount} new item(s) need your approval`);
  prevPendingCount=pend;
}

async function loadEmails(){
  try{
    const res=await fetch('/api/emails');
    allEmails=await res.json();
    updateStats();
    applyFilter();
  }catch(e){}
}
function applyFilter(){
  const q=(document.getElementById('search-box')?.value||'').toLowerCase();
  let f=allEmails;
  if(activeFilter==='pending')            f=allEmails.filter(e=>e.user_response==='pending');
  else if(activeFilter==='auto_executed') f=allEmails.filter(e=>e.user_response==='auto_executed');
  else if(activeFilter==='rejected')      f=allEmails.filter(e=>e.user_response==='rejected');
  else if(activeFilter!=='all')           f=allEmails.filter(e=>e.priority===activeFilter);
  if(q) f=f.filter(e=>(e.subject||'').toLowerCase().includes(q)||(e.from_addr||'').toLowerCase().includes(q)||(e.action||'').toLowerCase().includes(q)||(e.reason||'').toLowerCase().includes(q));
  renderEmails(f);
}
function filterEmails(f,el){
  activeFilter=f;
  document.querySelectorAll('.filter-chip').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  applyFilter();
}

async function approveAllPending(){
  const pending=allEmails.filter(e=>e.user_response==='pending');
  if(!pending.length){showToast('No pending items');return;}
  for(const item of pending) await fetch('/api/approve/'+item.id,{method:'POST'});
  showToast('Approved '+pending.length+' item(s)','ok');
  loadEmails(); loadFeed();
}
function exportCSV(){
  const cols=['subject','from_addr','priority','confidence','action','user_response','reason','created_at'];
  const rows=[cols.join(',')];
  allEmails.forEach(e=>rows.push(cols.map(c=>'"'+String(e[c]||'').replace(/"/g,'""')+'"').join(',')));
  const blob=new Blob([rows.join('\n')],{type:'text/csv'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='personalos_emails.csv'; a.click();
}

// ─────────────────────── Feed ─────────────────────────────────────────────────
async function loadFeed(){
  try{
    const res=await fetch('/api/feed');
    const entries=await res.json();
    const el=document.getElementById('activity-feed');
    if(!entries.length){el.innerHTML='<p class="empty">No activity yet</p>';return;}
    el.innerHTML=entries.map(e=>`
      <div class="feed-item">
        <span class="feed-dot"></span>
        <span class="feed-time">${(e.timestamp||'').replace('T',' ').substring(0,19)}</span>
        <span class="feed-agent">${esc(e.agent)}</span>
        <span class="feed-arrow">→</span>
        <span class="feed-action">${esc(e.action)}</span>
      </div>`).join('');
  }catch(e){}
}

// ─────────────────────── Health ───────────────────────────────────────────────
async function loadHealth(){
  try{
    const res=await fetch('/api/health');
    const data=await res.json();
    const el=document.getElementById('health-grid');
    const svcMap={redis:'Redis',openrouter:'LLM',google:'Google',twilio:'Twilio',chromadb:'ChromaDB',mcp_server:'MCP'};
    const q=data.services?.redis?.queues||{};
    let chips=Object.entries(data.services).map(([key,svc])=>{
      const dc=svc.status==='ok'?'dot-ok':svc.status==='error'?'dot-error':svc.status==='simulation_mode'?'dot-sim':'dot-warn';
      const detail=svc.latency_ms?svc.latency_ms+'ms':svc.account||svc.note||svc.error||svc.status;
      return `<div class="health-chip"><span class="health-dot ${dc}"></span><span class="health-name">${svcMap[key]||key}</span><span class="health-detail">&nbsp;${esc(String(detail).substring(0,30))}</span></div>`;
    });
    if(q.events!=null) chips.push(`<div class="health-chip"><span class="health-dot dot-ok"></span><span class="health-name">Queues</span><span class="health-detail">&nbsp;ev=${q.events} ap=${q.approvals} db=${q.dashboard_pending}</span></div>`);
    el.innerHTML=chips.join('');
  }catch(e){}
}

// ─────────────────────── Analytics ───────────────────────────────────────────
async function loadAnalytics(){
  try{
    const [mRes,fRes]=await Promise.all([fetch('/api/metrics'),fetch('/api/feed?limit=10')]);
    const m=await mRes.json();
    const feed=await fRes.json();
    renderAnalytics(m,feed);
  }catch(e){document.getElementById('analytics-grid').innerHTML='<p class="empty">Error loading metrics</p>';}
}
function barRow(label,val,max,fillClass){
  const pct=max>0?Math.round(val/max*100):0;
  return `<div class="bar-row"><span class="bar-label">${esc(label)}</span><div class="bar-track"><div class="bar-fill ${fillClass}" style="width:${pct}%"></div></div><span class="bar-val">${val}</span></div>`;
}
function renderAnalytics(m,feed){
  const autoRate=m.total>0?Math.round(m.by_response.auto_executed/m.total*100):0;
  const avgConf=m.avg_confidence?m.avg_confidence.toFixed(1):'—';
  const maxPri=Math.max(...Object.values(m.by_priority||{}),1);
  const maxRes=Math.max(...Object.values(m.by_response||{}),1);
  const maxConf=Math.max(...Object.values(m.confidence_distribution||{}),1);
  document.getElementById('analytics-grid').innerHTML=`
    <div class="analytics-card">
      <h3>Auto-Execute Rate</h3>
      <div class="big-num">${autoRate}%</div>
      <div class="big-sub">${m.by_response.auto_executed||0} of ${m.total} plans auto-executed</div>
      <div style="margin-top:14px;font-size:.74rem;color:var(--text-3)">Avg Confidence: <span style="color:var(--purple-hi);font-weight:700">${avgConf}%</span></div>
    </div>
    <div class="analytics-card">
      <h3>Priority Breakdown</h3>
      <div class="bar-chart">
        ${barRow('High',m.by_priority.high||0,maxPri,'bar-fill-high')}
        ${barRow('Medium',m.by_priority.medium||0,maxPri,'bar-fill-med')}
        ${barRow('Low',m.by_priority.low||0,maxPri,'bar-fill-low')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Routing Outcomes</h3>
      <div class="bar-chart">
        ${barRow('Auto-Exec',m.by_response.auto_executed||0,maxRes,'bar-fill-auto')}
        ${barRow('Approved',m.by_response.approved||0,maxRes,'bar-fill-auto')}
        ${barRow('Pending',m.by_response.pending||0,maxRes,'bar-fill-pend')}
        ${barRow('Rejected',m.by_response.rejected||0,maxRes,'bar-fill-rej')}
        ${barRow('Silent',m.by_response.silent_discarded||0,maxRes,'bar-fill-sil')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Confidence Distribution</h3>
      <div class="bar-chart">
        ${barRow('0–30%',(m.confidence_distribution||{})['0-30']||0,maxConf,'bar-fill-conf0')}
        ${barRow('31–60%',(m.confidence_distribution||{})['31-60']||0,maxConf,'bar-fill-conf1')}
        ${barRow('61–80%',(m.confidence_distribution||{})['61-80']||0,maxConf,'bar-fill-conf2')}
        ${barRow('81–100%',(m.confidence_distribution||{})['81-100']||0,maxConf,'bar-fill-conf3')}
      </div>
    </div>
    <div class="analytics-card">
      <h3>Queue Depths</h3>
      <div class="bar-chart">
        ${barRow('Events',(m.queue_depths||{}).events||0,Math.max((m.queue_depths||{}).events||0,1),'bar-fill-high')}
        ${barRow('Approvals',(m.queue_depths||{}).approvals||0,Math.max((m.queue_depths||{}).approvals||0,1),'bar-fill-med')}
        ${barRow('Dashboard',(m.queue_depths||{}).dashboard_pending||0,Math.max((m.queue_depths||{}).dashboard_pending||0,1),'bar-fill-pend')}
      </div>
      <div style="margin-top:14px;font-size:.72rem;color:var(--text-3)">Total processed: <b style="color:var(--text)">${m.total}</b></div>
    </div>
  `;
  const fEl=document.getElementById('analytics-feed');
  if(!feed.length){fEl.innerHTML='<p class="empty">No activity yet</p>';return;}
  fEl.innerHTML=feed.map(e=>`
    <div class="feed-item">
      <span class="feed-dot"></span>
      <span class="feed-time">${(e.timestamp||'').replace('T',' ').substring(0,19)}</span>
      <span class="feed-agent">${esc(e.agent)}</span>
      <span class="feed-arrow">→</span>
      <span class="feed-action">${esc(e.action)}</span>
    </div>`).join('');
}

// ─────────────────────── Preferences ─────────────────────────────────────────
async function loadPreferences(){
  try{
    const res=await fetch('/api/preferences');
    const prefs=await res.json();
    document.getElementById('pref-count').textContent=prefs.length;
    const el=document.getElementById('pref-list');
    if(!prefs.length){el.innerHTML='<p class="empty">No preferences stored yet</p>';return;}
    el.innerHTML=prefs.map(p=>`
      <div class="pref-item">
        <span class="pref-icon">🧠</span>
        <div style="flex:1">
          <div class="pref-text">${esc(p.document)}</div>
          <div class="pref-meta"><span class="cat-badge">${esc(p.metadata?.category||'general')}</span>source: ${esc(p.metadata?.source||'—')} &bull; added ${timeAgo(p.metadata?.created_at)}</div>
        </div>
      </div>`).join('');
  }catch(e){document.getElementById('pref-list').innerHTML='<p class="empty">Error loading preferences</p>';}
}
async function addPreference(){
  const text=document.getElementById('pref-text').value.trim();
  const cat=document.getElementById('pref-cat').value;
  const res=document.getElementById('pref-result');
  if(!text){showToast('Enter a preference statement','err');return;}
  try{
    const r=await fetch('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,category:cat})});
    const d=await r.json();
    if(r.ok){
      res.className='inject-result inject-ok'; res.textContent='Preference saved!'; res.style.display='block';
      document.getElementById('pref-text').value='';
      loadPreferences();
    } else {
      res.className='inject-result inject-err'; res.textContent=d.error||'Error'; res.style.display='block';
    }
    setTimeout(()=>res.style.display='none',3000);
  }catch(e){showToast('Error saving preference','err');}
}

// ─────────────────────── Event injection ─────────────────────────────────────
function onInjTypeChange(){
  const t=document.getElementById('inj-type').value;
  document.getElementById('inj-email-fields').style.display=t==='email'?'':'none';
  document.getElementById('inj-cal-fields').style.display=t==='calendar'?'':'none';
}
async function injectEvent(){
  const btn=document.getElementById('inj-btn');
  const res=document.getElementById('inj-result');
  btn.disabled=true; btn.textContent='Injecting…';
  const type=document.getElementById('inj-type').value;
  let body={event_type:type};
  if(type==='email'){
    body.from=document.getElementById('inj-from').value||'demo@example.com';
    body.subject=document.getElementById('inj-subject').value||'Test event';
    body.snippet=document.getElementById('inj-snippet').value||'';
    body.urgent=document.getElementById('inj-urgent').checked;
  } else if(type==='calendar'){
    body.summary=document.getElementById('inj-cal-title').value||'Test Meeting';
    body.start=document.getElementById('inj-cal-start').value||new Date().toISOString();
  }
  try{
    const r=await fetch('/api/events/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(r.ok){
      res.className='inject-result inject-ok';
      res.textContent='Event injected! ID: '+d.event_id+'. The Planner will process it in seconds.';
      res.style.display='block';
      showToast('Event injected into pipeline','ok');
    } else {
      res.className='inject-result inject-err';
      res.textContent=d.error||'Error';
      res.style.display='block';
    }
  }catch(e){
    res.className='inject-result inject-err'; res.textContent=String(e); res.style.display='block';
  }
  btn.disabled=false; btn.textContent='Inject Event →';
  setTimeout(()=>res.style.display='none',6000);
}

// ─────────────────────── Test Call ───────────────────────────────────────────
async function triggerTestCall(){
  showToast('Placing test call…');
  try{
    const r=await fetch('/api/twilio/test',{method:'POST'});
    const d=await r.json();
    if(r.ok&&d.call_sid) showToast('Call placed! SID: '+d.call_sid.substring(0,20)+'…','ok');
    else if(r.ok) showToast('Simulated call — set TWILIO_* env vars for real call');
    else showToast(d.error||'Call failed','err');
  }catch(e){showToast('Error: '+e,'err');}
}

// ─────────────────────── Approve / Reject ────────────────────────────────────
async function approve(id){
  await fetch('/api/approve/'+id,{method:'POST'});
  showToast('Approved and queued for execution','ok');
  loadEmails(); loadFeed();
}
async function reject(id){
  await fetch('/api/reject/'+id,{method:'POST'});
  showToast('Rejected');
  loadEmails(); loadFeed();
}

// ─────────────────────── Keyboard shortcuts ──────────────────────────────────
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA'||e.target.tagName==='SELECT') return;
  const cards=[...document.querySelectorAll('.email-card')];
  if(!cards.length) return;
  if(e.key==='ArrowDown'){focusedIdx=Math.min(focusedIdx+1,cards.length-1);cards[focusedIdx].focus();e.preventDefault();}
  else if(e.key==='ArrowUp'){focusedIdx=Math.max(focusedIdx-1,0);cards[focusedIdx].focus();e.preventDefault();}
  else if(e.key==='a'||e.key==='A'){const f=document.activeElement?.closest('.email-card');f?.querySelector('.btn-approve')?.click();}
  else if(e.key==='r'||e.key==='R'){const f=document.activeElement?.closest('.email-card');f?.querySelector('.btn-reject')?.click();}
});

// ─────────────────────── Poll Now ────────────────────────────────────────────
async function pollNow(silent){
  try{
    const r=await fetch('/api/poll/now',{method:'POST'});
    const d=await r.json();
    if(!silent) showToast('Checking for new emails…');
    setTimeout(()=>{loadEmails();loadFeed();},4000);
    setTimeout(()=>{loadEmails();loadFeed();},10000);
  }catch(e){}
}

// ─────────────────────── Init ─────────────────────────────────────────────────
function refresh(){loadEmails();loadFeed();}
refresh();
loadHealth();
pollNow(true);
setInterval(()=>{loadEmails();loadFeed();},30000);
