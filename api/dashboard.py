"""
FastAPI dashboard — port 8080.
Endpoints:
  GET  /              → HTML approval dashboard (auto-refreshes every 3s)
  GET  /api/pending   → list pending approvals from dashboard:pending
  POST /api/approve/{id} → approve an action (re-queues to Executor)
  POST /api/reject/{id}  → reject an action
  GET  /api/feed      → recent activity log (last 50 entries)
  GET  /api/health    → live health status of all services
"""
import asyncio
import time
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="PersonalOS Dashboard", version="1.0.0")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>PersonalOS Agent Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #0f0f14; color: #e0e0e0; padding: 24px; }
    h1 { color: #7c6af7; font-size: 1.6rem; margin-bottom: 4px; }
    .subtitle { color: #888; font-size: 0.85rem; margin-bottom: 20px; }
    .section-title { font-size: 1rem; font-weight: 600; color: #aaa; margin: 20px 0 10px; border-bottom: 1px solid #222; padding-bottom: 6px; display:flex; align-items:center; gap:8px; }
    .count-badge { background:#2a2a3a; color:#888; border-radius:10px; padding:1px 8px; font-size:0.75rem; font-weight:normal; }

    /* Email cards */
    .email-card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 10px; padding: 0; margin-bottom: 12px; overflow:hidden; }
    .email-card.priority-high { border-left: 4px solid #f87171; }
    .email-card.priority-medium { border-left: 4px solid #fb923c; }
    .email-card.priority-low { border-left: 4px solid #4a4a5a; }
    .email-header { display:flex; align-items:center; gap:10px; padding:12px 16px 8px; flex-wrap:wrap; }
    .email-subject { font-size:0.95rem; font-weight:600; color:#e0e0e0; flex:1; min-width:0; }
    .email-from { font-size:0.78rem; color:#666; padding: 0 16px 4px; }
    .email-snippet { font-size:0.8rem; color:#888; padding: 0 16px 10px; line-height:1.5; }

    .badge { display:inline-flex; align-items:center; gap:4px; padding:3px 9px; border-radius:12px; font-size:0.72rem; font-weight:700; white-space:nowrap; flex-shrink:0; }
    .badge-high { background:#3b0a0a; color:#f87171; }
    .badge-medium { background:#3b1f00; color:#fb923c; }
    .badge-low { background:#1e1e2e; color:#6b7280; }
    .badge-conf { background:#1e1e3a; color:#a78bfa; }
    .badge-response-auto { background:#1a2e1a; color:#4ade80; }
    .badge-response-approved { background:#1a3a2a; color:#34d399; }
    .badge-response-rejected { background:#3b0a0a; color:#f87171; }
    .badge-response-pending { background:#2a1a00; color:#fb923c; }
    .badge-response-silent { background:#1e1e1e; color:#555; }

    .email-body { padding: 4px 16px 12px; }
    .detail-row { display:flex; gap:8px; align-items:baseline; margin-bottom:6px; flex-wrap:wrap; }
    .detail-label { font-size:0.72rem; color:#555; text-transform:uppercase; letter-spacing:.04em; width:90px; flex-shrink:0; }
    .detail-value { font-size:0.82rem; color:#bbb; flex:1; }
    .detail-value.action-name { color:#c4b5fd; font-weight:600; }
    .call-box { background:#13131c; border:1px solid #2a2a3a; border-radius:6px; padding:10px 12px; font-size:0.8rem; color:#9ca3af; line-height:1.6; margin-top:6px; font-style:italic; }
    .call-box::before { content:"📞 "; font-style:normal; }
    .alts-list { font-size:0.78rem; color:#666; margin-top:4px; }
    .alts-list div { margin:2px 0 2px 4px; }

    .actions-row { padding: 8px 16px 12px; display:flex; gap:8px; }
    .btn { padding:7px 16px; border:none; border-radius:6px; cursor:pointer; font-size:0.82rem; font-weight:600; }
    .btn-approve { background:#1a472a; color:#4ade80; border:1px solid #166534; }
    .btn-approve:hover { background:#166534; }
    .btn-reject { background:#3b0a0a; color:#f87171; border:1px solid #7f1d1d; }
    .btn-reject:hover { background:#7f1d1d; }

    /* Activity feed */
    .feed-item { padding:8px 12px; border-left:3px solid #3a3a4a; margin-bottom:6px; background:#13131c; border-radius:4px; font-size:0.8rem; }
    .feed-time { color:#555; font-size:0.73rem; }
    .feed-action { color:#c4b5fd; }

    .empty { color:#555; font-style:italic; padding:12px; }
    .status-bar { position:fixed; bottom:12px; right:16px; font-size:0.73rem; color:#444; }
    .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:#4ade80; margin-right:6px; animation:pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

    .health-grid { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
    .health-chip { display:flex; align-items:center; gap:6px; background:#1a1a24; border:1px solid #2a2a3a; border-radius:8px; padding:5px 12px; font-size:0.76rem; }
    .health-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
    .dot-ok { background:#4ade80; }
    .dot-error { background:#f87171; }
    .dot-warn { background:#fb923c; }
    .dot-sim { background:#888; }
    .health-name { color:#aaa; font-weight:600; text-transform:uppercase; font-size:0.7rem; }
    .health-detail { color:#666; font-size:0.7rem; }

    .tabs { display:flex; gap:4px; margin-bottom:14px; flex-wrap:wrap; }
    .tab { padding:6px 14px; border-radius:6px; cursor:pointer; font-size:0.82rem; color:#666; background:#1a1a24; border:1px solid #2a2a3a; }
    .tab.active { background:#2a1a4a; color:#c4b5fd; border-color:#7c6af7; }

    .stats-bar { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:20px; }
    .stat-card { background:#1a1a24; border:1px solid #2a2a3a; border-radius:10px; padding:12px 18px; flex:1; min-width:100px; text-align:center; }
    .stat-num { font-size:1.5rem; font-weight:700; }
    .stat-label { font-size:0.7rem; color:#666; text-transform:uppercase; letter-spacing:.05em; }
    .stat-high { color:#f87171; } .stat-med { color:#fb923c; } .stat-low { color:#6b7280; }
    .stat-auto { color:#4ade80; } .stat-pending { color:#a78bfa; } .stat-total { color:#e0e0e0; }

    .search-row { display:flex; gap:8px; margin-bottom:12px; align-items:center; flex-wrap:wrap; }
    .search-input { flex:1; min-width:180px; background:#1a1a24; border:1px solid #2a2a3a; border-radius:6px; padding:8px 12px; color:#e0e0e0; font-size:0.85rem; outline:none; }
    .search-input:focus { border-color:#7c6af7; }
    .search-input::placeholder { color:#555; }
    .btn-bulk { padding:7px 14px; border:none; border-radius:6px; cursor:pointer; font-size:0.8rem; font-weight:600; background:#1a3a2a; color:#34d399; border:1px solid #166534; }
    .btn-bulk:hover { background:#166534; }
    .btn-export { padding:7px 14px; border:none; border-radius:6px; cursor:pointer; font-size:0.8rem; font-weight:600; background:#1a1a2a; color:#a78bfa; border:1px solid #3a2a5a; }
    .btn-export:hover { background:#2a1a4a; }

    .kw-chip { display:inline-block; background:#3b0a0a; color:#f87171; border-radius:10px; padding:1px 7px; font-size:0.7rem; margin:2px 2px 0 0; }
    .args-box { background:#0d0d18; border:1px solid #2a2a3a; border-radius:6px; padding:8px 12px; font-size:0.78rem; color:#9ca3af; line-height:1.6; margin-top:4px; font-family:monospace; white-space:pre-wrap; }

    #toast-container { position:fixed; top:16px; right:16px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
    .toast { background:#2a1a4a; color:#c4b5fd; border:1px solid #7c6af7; border-radius:8px; padding:10px 16px; font-size:0.82rem; opacity:0; transform:translateY(-10px); transition:all .3s; max-width:300px; }
    .toast.show { opacity:1; transform:translateY(0); }
    .kbd { display:inline-block; background:#1a1a2a; border:1px solid #3a3a5a; border-radius:4px; padding:1px 5px; font-size:0.68rem; color:#888; font-family:monospace; }
  </style>
</head>
<body>
  <div id="toast-container"></div>
  <h1>PersonalOS Agent</h1>
  <p class="subtitle">Autonomous multi-agent AI system — SOLARIS X 2026 &nbsp;|&nbsp; <span class="kbd">A</span> approve &nbsp;<span class="kbd">R</span> reject &nbsp;<span class="kbd">↑↓</span> navigate</p>

  <div class="section-title">System Health</div>
  <div id="health-grid" class="health-grid"><span style="color:#555;font-size:.8rem">Loading...</span></div>

  <div class="section-title">Overview</div>
  <div class="stats-bar" id="stats-bar">
    <div class="stat-card"><div class="stat-num stat-total" id="stat-total">—</div><div class="stat-label">Total Emails</div></div>
    <div class="stat-card"><div class="stat-num stat-high" id="stat-high">—</div><div class="stat-label">High Priority</div></div>
    <div class="stat-card"><div class="stat-num stat-med" id="stat-med">—</div><div class="stat-label">Medium Priority</div></div>
    <div class="stat-card"><div class="stat-num stat-low" id="stat-low">—</div><div class="stat-label">Low Priority</div></div>
    <div class="stat-card"><div class="stat-num stat-auto" id="stat-auto">—</div><div class="stat-label">Auto-Executed</div></div>
    <div class="stat-card"><div class="stat-num stat-pending" id="stat-pending">—</div><div class="stat-label">Pending Approval</div></div>
  </div>

  <div class="section-title">
    Email Intelligence
    <span class="count-badge" id="email-count">—</span>
  </div>

  <div class="search-row">
    <input class="search-input" id="search-box" placeholder="Search by subject, sender, action..." oninput="applyFilter()" />
    <button class="btn-bulk" onclick="approveAllPending()" title="Approve all pending items">Approve All Pending</button>
    <button class="btn-export" onclick="exportCSV()" title="Export emails to CSV">Export CSV</button>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="filterEmails('all',this)">All</div>
    <div class="tab" onclick="filterEmails('high',this)">High Priority</div>
    <div class="tab" onclick="filterEmails('medium',this)">Medium</div>
    <div class="tab" onclick="filterEmails('low',this)">Low</div>
    <div class="tab" onclick="filterEmails('pending',this)">Pending Approval</div>
    <div class="tab" onclick="filterEmails('auto_executed',this)">Auto-Executed</div>
    <div class="tab" onclick="filterEmails('rejected',this)">Rejected</div>
  </div>
  <div id="email-list"><p class="empty">Loading...</p></div>

  <div class="section-title">Activity Feed</div>
  <div id="activity-feed"><p class="empty">Loading...</p></div>

  <div class="status-bar"><span class="pulse"></span>Live — auto-refresh every 3s</div>

  <script>
    let allEmails = [];
    let activeFilter = 'all';
    let focusedIdx = -1;
    let prevPendingCount = -1;

    const PRIORITY_LABEL = { high:'HIGH', medium:'MED', low:'LOW' };
    const PRIORITY_BADGE = { high:'badge-high', medium:'badge-medium', low:'badge-low' };
    const RESPONSE_BADGE = {
      auto_executed:'badge-response-auto', approved:'badge-response-approved',
      rejected:'badge-response-rejected', pending:'badge-response-pending',
      silent_discarded:'badge-response-silent',
    };
    const RESPONSE_LABEL = {
      auto_executed:'⚡ Auto-Executed', approved:'✓ Approved',
      rejected:'✕ Rejected', pending:'⏳ Awaiting Approval',
      silent_discarded:'— Silently Discarded',
    };

    function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    function timeAgo(ts) {
      if (!ts) return '';
      const diff = Math.floor((Date.now() - new Date(ts)) / 1000);
      if (diff < 60) return diff + 's ago';
      if (diff < 3600) return Math.floor(diff/60) + 'm ago';
      if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
      return Math.floor(diff/86400) + 'd ago';
    }

    function confBar(c) {
      const col = c > 90 ? '#f87171' : c >= 70 ? '#fb923c' : '#4a4a6a';
      return `<div style="display:inline-block;width:${Math.min(c,100)}px;height:6px;background:${col};border-radius:3px;vertical-align:middle;margin-right:6px"></div>`;
    }

    function argsPreview(item) {
      const args = item.action_args || {};
      if (!item.action || item.action === 'no_action' || !Object.keys(args).length) return '';
      let lines = [];
      if (item.action === 'send_email') {
        if (args.to) lines.push('To:      ' + esc(args.to));
        if (args.subject) lines.push('Subject: ' + esc(args.subject));
        if (args.body) lines.push('Body:\n' + esc(args.body));
      } else if (item.action === 'create_event') {
        if (args.summary) lines.push('Event:   ' + esc(args.summary));
        if (args.start_datetime) lines.push('Start:   ' + esc(args.start_datetime));
        if (args.end_datetime) lines.push('End:     ' + esc(args.end_datetime));
      } else if (item.action === 'move_file') {
        if (args.source) lines.push('From: ' + esc(args.source));
        if (args.destination) lines.push('To:   ' + esc(args.destination));
      } else {
        lines.push(esc(JSON.stringify(args, null, 2)));
      }
      if (!lines.length) return '';
      return `<div class="detail-row" style="align-items:flex-start">
        <span class="detail-label">Action Args</span>
        <div class="args-box">${lines.join('\n')}</div>
      </div>`;
    }

    function renderEmails(emails) {
      const el = document.getElementById('email-list');
      if (!emails.length) { el.innerHTML = '<p class="empty">No emails in this category</p>'; return; }
      el.innerHTML = emails.map((item, idx) => {
        const pr = item.priority || 'low';
        const resp = item.user_response || 'pending';
        const isPending = resp === 'pending';
        const kws = (item.urgency_keywords || []);
        const kwHtml = kws.length ? `<div style="padding:0 16px 6px">${kws.map(k=>`<span class="kw-chip">${esc(k)}</span>`).join('')}</div>` : '';
        const altsHtml = (item.alternatives || []).length ? `
          <div class="detail-row">
            <span class="detail-label">Alternatives</span>
            <div class="alts-list">
              ${item.alternatives.map(a=>`<div>• <b>${esc(a.action).replace(/_/g,' ')}</b> (${a.confidence}%) — ${esc(a.reason)}</div>`).join('')}
            </div>
          </div>` : '';
        return `
        <div class="email-card priority-${pr}" id="card-${item.id}" data-idx="${idx}" tabindex="0">
          <div class="email-header">
            <span class="badge ${PRIORITY_BADGE[pr]}">${PRIORITY_LABEL[pr]}</span>
            <span class="email-subject">${esc(item.subject||'(no subject)')}</span>
            <span class="badge badge-conf">${confBar(item.confidence||0)}${item.confidence||0}%</span>
            <span class="badge ${RESPONSE_BADGE[resp]||'badge-response-pending'}">${RESPONSE_LABEL[resp]||resp}</span>
            <span style="font-size:.7rem;color:#555;margin-left:4px">${timeAgo(item.created_at)}</span>
          </div>
          <div class="email-from">From: ${esc(item.from_addr)}</div>
          ${item.snippet ? `<div class="email-snippet">${esc(item.snippet).substring(0,200)}…</div>` : ''}
          ${kwHtml}
          <div class="email-body">
            <div class="detail-row">
              <span class="detail-label">Action</span>
              <span class="detail-value action-name">${esc(item.action||'no_action').replace(/_/g,' ')}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Reason</span>
              <span class="detail-value">${esc(item.reason)}</span>
            </div>
            ${item.explanation ? `<div class="detail-row">
              <span class="detail-label">Explanation</span>
              <span class="detail-value" style="color:#666;font-style:italic">${esc(item.explanation)}</span>
            </div>` : ''}
            ${altsHtml}
            ${argsPreview(item)}
            <div class="detail-row">
              <span class="detail-label">Scoring</span>
              <span class="detail-value" style="color:#555">base=${item.scoring?.base||'—'} × urgency=${item.scoring?.urgency_mult||'—'} × history=${item.scoring?.history_mult||'—'} = ${item.confidence}%</span>
            </div>
            ${item.call_text ? `<div class="detail-row" style="align-items:flex-start">
              <span class="detail-label">Call Script</span>
              <div class="call-box">${esc(item.call_text)}</div>
            </div>` : ''}
          </div>
          ${isPending ? `<div class="actions-row">
            <button class="btn btn-approve" onclick="approve('${item.id}')">Approve &amp; Execute</button>
            <button class="btn btn-reject" onclick="reject('${item.id}')">Reject</button>
          </div>` : ''}
        </div>`;
      }).join('');
    }

    function updateStats() {
      const total = allEmails.length;
      const high = allEmails.filter(e=>e.priority==='high').length;
      const med  = allEmails.filter(e=>e.priority==='medium').length;
      const low  = allEmails.filter(e=>e.priority==='low').length;
      const auto = allEmails.filter(e=>e.user_response==='auto_executed').length;
      const pend = allEmails.filter(e=>e.user_response==='pending').length;
      document.getElementById('stat-total').textContent = total;
      document.getElementById('stat-high').textContent  = high;
      document.getElementById('stat-med').textContent   = med;
      document.getElementById('stat-low').textContent   = low;
      document.getElementById('stat-auto').textContent  = auto;
      document.getElementById('stat-pending').textContent = pend;
      document.getElementById('email-count').textContent = total;
      // Toast on new pending arrivals
      if (prevPendingCount >= 0 && pend > prevPendingCount) {
        showToast(`${pend - prevPendingCount} new email(s) need your approval`);
      }
      prevPendingCount = pend;
    }

    async function loadEmails() {
      try {
        const res = await fetch('/api/emails');
        allEmails = await res.json();
        updateStats();
        applyFilter();
      } catch(e) {}
    }

    function applyFilter() {
      const q = (document.getElementById('search-box')?.value || '').toLowerCase();
      let filtered = allEmails;
      if (activeFilter === 'pending') filtered = allEmails.filter(e=>e.user_response==='pending');
      else if (activeFilter === 'auto_executed') filtered = allEmails.filter(e=>e.user_response==='auto_executed');
      else if (activeFilter === 'rejected') filtered = allEmails.filter(e=>e.user_response==='rejected');
      else if (activeFilter !== 'all') filtered = allEmails.filter(e=>e.priority===activeFilter);
      if (q) filtered = filtered.filter(e =>
        (e.subject||'').toLowerCase().includes(q) ||
        (e.from_addr||'').toLowerCase().includes(q) ||
        (e.action||'').toLowerCase().includes(q) ||
        (e.reason||'').toLowerCase().includes(q)
      );
      renderEmails(filtered);
    }

    function filterEmails(f, el) {
      activeFilter = f;
      document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
      el.classList.add('active');
      applyFilter();
    }

    async function approveAllPending() {
      const pending = allEmails.filter(e=>e.user_response==='pending');
      if (!pending.length) { showToast('No pending items to approve'); return; }
      for (const item of pending) {
        await fetch('/api/approve/'+item.id, {method:'POST'});
      }
      showToast(`Approved ${pending.length} item(s)`);
      loadEmails(); loadFeed();
    }

    function exportCSV() {
      const cols = ['subject','from_addr','priority','confidence','action','user_response','reason','created_at'];
      const rows = [cols.join(',')];
      allEmails.forEach(e => {
        rows.push(cols.map(c => '"' + String(e[c]||'').replace(/"/g,'""') + '"').join(','));
      });
      const blob = new Blob([rows.join('\n')], {type:'text/csv'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'personalos_emails.csv';
      a.click();
    }

    async function loadFeed() {
      const res = await fetch('/api/feed');
      const entries = await res.json();
      const el = document.getElementById('activity-feed');
      if (!entries.length) { el.innerHTML = '<p class="empty">No activity yet</p>'; return; }
      el.innerHTML = entries.map(e => `
        <div class="feed-item">
          <span class="feed-time">${(e.timestamp||'').replace('T',' ').substring(0,19)} UTC</span>
          <span style="color:#555"> | </span>
          <span style="color:#888">${esc(e.agent)}</span>
          <span style="color:#555"> → </span>
          <span class="feed-action">${esc(e.action)}</span>
        </div>
      `).join('');
    }

    async function loadHealth() {
      try {
        const res = await fetch('/api/health');
        const data = await res.json();
        const el = document.getElementById('health-grid');
        const svcMap = { redis:'Redis', openrouter:'LLM', google:'Google', twilio:'Twilio', chromadb:'ChromaDB', mcp_server:'MCP' };
        const queues = data.services?.redis?.queues || {};
        let chips = Object.entries(data.services).map(([key, svc]) => {
          const dotClass = svc.status==='ok'?'dot-ok':svc.status==='error'?'dot-error':svc.status==='simulation_mode'?'dot-sim':'dot-warn';
          const detail = svc.latency_ms ? svc.latency_ms+'ms' : svc.account || svc.note || svc.error || svc.status;
          return `<div class="health-chip"><span class="health-dot ${dotClass}"></span><span class="health-name">${svcMap[key]||key}</span><span class="health-detail">${esc(detail)}</span></div>`;
        });
        if (queues.events != null) chips.push(`<div class="health-chip"><span class="health-dot dot-ok"></span><span class="health-name">Queues</span><span class="health-detail">events=${queues.events} approvals=${queues.approvals} dashboard=${queues.dashboard_pending}</span></div>`);
        el.innerHTML = chips.join('');
      } catch(e) {}
    }

    function showToast(msg) {
      const c = document.getElementById('toast-container');
      const t = document.createElement('div');
      t.className = 'toast';
      t.textContent = msg;
      c.appendChild(t);
      requestAnimationFrame(() => { requestAnimationFrame(() => t.classList.add('show')); });
      setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3500);
    }

    async function approve(id) {
      await fetch('/api/approve/'+id, {method:'POST'});
      showToast('Action approved and queued for execution');
      loadEmails(); loadFeed();
    }

    async function reject(id) {
      await fetch('/api/reject/'+id, {method:'POST'});
      showToast('Action rejected');
      loadEmails(); loadFeed();
    }

    // Keyboard shortcuts: A=approve focused, R=reject focused, arrows=navigate
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT') return;
      const cards = [...document.querySelectorAll('.email-card')];
      if (!cards.length) return;
      if (e.key === 'ArrowDown') { focusedIdx = Math.min(focusedIdx+1, cards.length-1); cards[focusedIdx].focus(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { focusedIdx = Math.max(focusedIdx-1, 0); cards[focusedIdx].focus(); e.preventDefault(); }
      else if (e.key === 'a' || e.key === 'A') {
        const focused = document.activeElement?.closest('.email-card');
        const btn = focused?.querySelector('.btn-approve');
        if (btn) btn.click();
      } else if (e.key === 'r' || e.key === 'R') {
        const focused = document.activeElement?.closest('.email-card');
        const btn = focused?.querySelector('.btn-reject');
        if (btn) btn.click();
      }
    });

    function refresh() { loadEmails(); loadFeed(); }
    refresh();
    loadHealth();
    setInterval(refresh, 3000);
    setInterval(loadHealth, 15000);
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """Serve the approval dashboard HTML."""
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/pending")
async def get_pending() -> list[dict]:
    """Return all items currently waiting for human approval."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    return await redis.get_dashboard_items()


@app.post("/api/approve/{item_id}")
async def approve_action(item_id: str) -> dict:
    """
    Approve a pending action:
    1. Fetch from dashboard:pending
    2. Set approved_override=True
    3. Re-queue to approvals:pending (Executor will auto-execute and record outcome)
    4. Delete from dashboard:pending

    Note: ChromaDB outcome recording is intentionally left to the ExecutorAgent.
    Recording here AND in the Executor would double-count this approval and
    artificially inflate future confidence scores for this action type.
    """
    from memory.redis_client import RedisClient

    redis = RedisClient.get_instance()
    item = await redis.get_dashboard_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    item["approved_override"] = True
    await redis.push_approval(item)
    await redis.remove_dashboard_item(item_id)
    await redis.update_email_response(item_id, "approved")

    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"APPROVED: {item.get('action')} (user decision)",
        "plan_id": item_id,
    })

    return {"status": "approved", "plan_id": item_id}


@app.post("/api/reject/{item_id}")
async def reject_action(item_id: str) -> dict:
    """
    Reject a pending action:
    1. Delete from dashboard:pending
    2. Record rejection in ChromaDB
    """
    from memory.redis_client import RedisClient
    from memory.chroma_memory import ChromaMemory

    redis = RedisClient.get_instance()
    item = await redis.get_dashboard_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    await redis.remove_dashboard_item(item_id)
    await redis.update_email_response(item_id, "rejected")

    # Record rejection → lowers future confidence for this action type
    try:
        memory = ChromaMemory.from_settings()
        await memory.record_outcome(item, {}, approved=False, executor="dashboard")
    except Exception:
        pass

    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"REJECTED: {item.get('action')} (user decision)",
        "plan_id": item_id,
    })

    return {"status": "rejected", "plan_id": item_id}


@app.get("/api/emails")
async def get_all_emails() -> list[dict]:
    """Return all email plans (every confidence level) sorted newest first."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    return await redis.get_all_emails()


@app.get("/api/feed")
async def activity_feed(limit: int = 50) -> list[dict]:
    """Return recent activity log entries (newest first)."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    return await redis.get_activity_log(limit=limit)


@app.get("/api/health")
async def health_check() -> dict:
    """
    Live health status of all services.
    Returns a dict with status per service and overall system health.
    """
    from config.settings import get_settings
    cfg = get_settings()
    checks: dict[str, dict] = {}
    overall = True

    # ── Redis ──────────────────────────────────────────────────────────────────
    try:
        from memory.redis_client import RedisClient
        redis = RedisClient.get_instance()
        t0 = time.perf_counter()
        ok = await redis.ping()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        eq = await redis._redis.llen("events:queue")
        aq = await redis._redis.llen("approvals:pending")
        dp = await redis._redis.hlen("dashboard:pending")
        checks["redis"] = {
            "status": "ok" if ok else "error",
            "latency_ms": ms,
            "queues": {"events": eq, "approvals": aq, "dashboard_pending": dp},
        }
    except Exception as e:
        checks["redis"] = {"status": "error", "error": str(e)}
        overall = False

    # ── OpenRouter LLM ────────────────────────────────────────────────────────
    # Use /models endpoint (no token cost, no rate limit) instead of a completion call.
    try:
        if not cfg.openrouter_api_key:
            checks["openrouter"] = {"status": "error", "error": "OPENROUTER_API_KEY not set"}
            overall = False
        else:
            import httpx
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=5.0) as hclient:
                r = await hclient.get(
                    f"{cfg.openrouter_base_url}/models",
                    headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
                )
            ms = round((time.perf_counter() - t0) * 1000, 1)
            if r.status_code == 200:
                checks["openrouter"] = {
                    "status": "ok",
                    "model": cfg.openrouter_model,
                    "latency_ms": ms,
                }
            else:
                checks["openrouter"] = {
                    "status": "error",
                    "error": f"HTTP {r.status_code} — check OPENROUTER_API_KEY",
                }
                overall = False
    except Exception as e:
        checks["openrouter"] = {"status": "error", "error": str(e)[:100]}
        overall = False

    # ── Google APIs ───────────────────────────────────────────────────────────
    try:
        from pathlib import Path
        token_ok = Path(cfg.google_token_path).exists()
        creds_ok = Path(cfg.google_credentials_path).exists()
        if token_ok and creds_ok:
            from mcp_server.google_auth import get_credentials
            from googleapiclient.discovery import build
            creds = await asyncio.to_thread(get_credentials)
            gmail = await asyncio.to_thread(build, "gmail", "v1", credentials=creds)
            t0 = time.perf_counter()
            profile = await asyncio.to_thread(
                lambda: gmail.users().getProfile(userId="me").execute()
            )
            ms = round((time.perf_counter() - t0) * 1000, 1)
            checks["google"] = {
                "status": "ok",
                "account": profile.get("emailAddress", ""),
                "latency_ms": ms,
            }
        else:
            checks["google"] = {
                "status": "warning",
                "error": "credentials.json or token.json missing — OAuth not completed",
            }
    except Exception as e:
        checks["google"] = {"status": "error", "error": str(e)[:100]}

    # ── Twilio ────────────────────────────────────────────────────────────────
    if cfg.twilio_enabled:
        try:
            from twilio.rest import Client
            t0 = time.perf_counter()
            tw = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
            acct = await asyncio.to_thread(
                lambda: tw.api.accounts(cfg.twilio_account_sid).fetch()
            )
            ms = round((time.perf_counter() - t0) * 1000, 1)
            checks["twilio"] = {
                "status": "ok",
                "account": acct.friendly_name,
                "from": cfg.twilio_from_number,
                "to": cfg.twilio_to_number,
                "latency_ms": ms,
            }
        except Exception as e:
            checks["twilio"] = {"status": "error", "error": str(e)[:100]}
            overall = False
    else:
        checks["twilio"] = {"status": "simulation_mode", "note": "TWILIO_* vars not set"}

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    try:
        import chromadb
        t0 = time.perf_counter()
        client = await asyncio.to_thread(chromadb.PersistentClient, cfg.chroma_persist_path)
        cols = await asyncio.to_thread(client.list_collections)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        checks["chromadb"] = {
            "status": "ok",
            "collections": [c.name for c in cols],
            "latency_ms": ms,
        }
    except Exception as e:
        checks["chromadb"] = {"status": "error", "error": str(e)[:100]}
        overall = False

    # ── MCP Server ────────────────────────────────────────────────────────────
    # Probe the root URL, NOT the /sse endpoint.  The SSE endpoint streams
    # indefinitely — an httpx GET against it would hang (or ReadTimeout) even
    # when the server is perfectly healthy.  Any HTTP response from "/" confirms
    # the ASGI app is routing, which is all we need to know.
    mcp_root = f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port}/"
    try:
        import httpx
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=2.0) as hclient:
            await hclient.get(mcp_root)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        checks["mcp_server"] = {
            "status": "ok",
            "url": cfg.mcp_sse_url,
            "latency_ms": ms,
        }
    except Exception:
        checks["mcp_server"] = {
            "status": "error",
            "url": cfg.mcp_sse_url,
            "error": "Not reachable (starts with main.py)",
        }

    return {
        "overall": "healthy" if overall else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    }
