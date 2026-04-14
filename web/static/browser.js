/**
 * browser.js — Protocol Browser panel
 *
 * openProtocolBrowser(sessionId, meetingId, meetings, opts)
 *   sessionId  — active research session
 *   meetingId  — meeting to show on open
 *   meetings   — [{meeting_id, title, date, committee, score}] from deep_dive payload
 *   opts       — { originalQuestion, postCompletion }
 *
 * Renders inline in the chat column.  Two-column layout (RTL):
 *   left: transcript + AI summary + panel chat
 *   right (sidebar): meeting list, load-more button
 *
 * API surface used:
 *   GET  /api/research/{id}/meeting/{mid}/summary
 *   GET  /api/research/{id}/meeting/{mid}/transcript
 *   GET  /api/research/{id}/rag?query=...&top_k=40      (load more)
 *   POST /api/research/{id}/workspace/select             (pin chunk)
 *   POST /api/research/{id}/workspace/ask               (panel chat + summarize)
 */

/* ── Topic color palette (index → CSS color) ────────────────────── */
const TOPIC_COLORS = ['#266829','#005f99','#765600','#b02500','#5b5c5a'];
function topicColor(idx) {
  if (idx == null || idx < 0) return '#adadab';
  return TOPIC_COLORS[idx % TOPIC_COLORS.length];
}

/* ── State ──────────────────────────────────────────────────────── */
let _sid       = null;   // session id
let _meetings  = [];     // full meeting list (grows on "load more")
let _activeId  = null;   // currently shown meeting_id
let _panel     = null;   // root DOM element (.msg-agent wrapper)
let _summary   = null;   // last fetched summary {topics:[]}
let _activeTopicFilter = null;  // null = show all; number = show that topic index
let _origQ     = '';     // original question (for summarize button)

/* ── Main entry point ───────────────────────────────────────────── */
function openProtocolBrowser(sessionId, meetingId, meetings, opts = {}) {
  _sid      = sessionId;
  _meetings = meetings || [];
  _activeId = meetingId;
  _origQ    = opts.originalQuestion || '';

  // Replace any existing panel
  if (_panel) _panel.remove();

  _panel = document.createElement('div');
  _panel.className = 'msg-agent browser-wrapper';
  _panel.innerHTML = _shellHtml(opts.postCompletion);

  const chatColumn = document.getElementById('chat-column');
  chatColumn.appendChild(_panel);
  _renderSidebar();
  _loadMeeting(meetingId);

  // Panel chat submit
  _panel.querySelector('#browser-chat-submit').addEventListener('click', _browserAsk);
  _panel.querySelector('#browser-chat-input').addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _browserAsk(); }
  });

  // Summarize button (if shown)
  const sumBtn = _panel.querySelector('#browser-summarize-btn');
  if (sumBtn) sumBtn.addEventListener('click', _browserSummarize);

  chatColumn.scrollTop = chatColumn.scrollHeight;
}

/* ── Shell HTML ─────────────────────────────────────────────────── */
function _shellHtml(postCompletion) {
  const summarizeBtn = postCompletion ? '' :
    `<button id="browser-summarize-btn" class="browser-summarize-btn" title="תמצת על בסיס הפרוטוקולים שנמצאו">
      תמצת עבורי
    </button>`;
  return `
<div class="browser-panel">
  <div class="browser-header">
    <span class="browser-breadcrumb">כנסת ישראל / פרוטוקולים / <span id="browser-meeting-title">…</span></span>
    <div class="browser-header-actions">
      ${summarizeBtn}
      <button class="browser-close-btn" onclick="closeProtocolBrowser()" title="סגור">✕</button>
    </div>
  </div>
  <div class="browser-body">
    <div class="browser-transcript-col" id="browser-transcript-col">
      <div class="browser-loading">טוען…</div>
    </div>
    <div class="browser-sidebar" id="browser-sidebar"></div>
  </div>
  <div class="browser-chat-bar">
    <textarea id="browser-chat-input" placeholder="שאל שאלה על הישיבה הזו… (Ctrl+Enter)" rows="1"></textarea>
    <button id="browser-chat-submit" class="browser-chat-submit">שלח</button>
  </div>
</div>`;
}

function closeProtocolBrowser() {
  if (_panel) { _panel.remove(); _panel = null; }
}

/* ── Sidebar ─────────────────────────────────────────────────────── */
function _renderSidebar() {
  const sb = _panel.querySelector('#browser-sidebar');
  if (!sb) return;
  let html = '<div class="sidebar-list">';
  for (const m of _meetings) {
    const active  = m.meeting_id === _activeId;
    const pct     = Math.round(m.score * 100);
    const badgeCls = pct >= 85 ? 'rel-green' : pct >= 70 ? 'rel-blue' : 'rel-grey';
    html += `<div class="sidebar-meeting ${active ? 'active' : ''}"
                  onclick="browserSwitchMeeting('${_esc(m.meeting_id)}')">
      <div class="sidebar-meeting-title">${_esc(m.title || m.meeting_id)}</div>
      <div class="sidebar-meeting-meta">
        <span class="sidebar-date">${_esc(m.date || '')}</span>
        <span class="rel-badge ${badgeCls}">${pct}%</span>
      </div>
    </div>`;
  }
  html += '</div>';
  html += `<button class="sidebar-load-more" onclick="browserLoadMore()">טען עוד ישיבות</button>`;
  sb.innerHTML = html;
}

/* ── Load meeting (summary + transcript) ─────────────────────────── */
async function _loadMeeting(meetingId) {
  _activeId = meetingId;
  _activeTopicFilter = null;
  _summary = null;

  // Update breadcrumb
  const m = _meetings.find(x => x.meeting_id === meetingId);
  const titleEl = _panel.querySelector('#browser-meeting-title');
  if (titleEl) titleEl.textContent = m ? (m.title || meetingId) : meetingId;

  // Update sidebar active state
  _renderSidebar();

  const col = _panel.querySelector('#browser-transcript-col');
  col.innerHTML = '<div class="browser-loading">טוען…</div>';

  try {
    const [summaryData, transcriptData] = await Promise.all([
      fetch(`/api/research/${_sid}/meeting/${encodeURIComponent(meetingId)}/summary`).then(r => r.json()),
      fetch(`/api/research/${_sid}/meeting/${encodeURIComponent(meetingId)}/transcript`).then(r => r.json()),
    ]);

    if (summaryData.error) throw new Error(summaryData.error);
    if (transcriptData.error) throw new Error(transcriptData.error);

    _summary = summaryData;
    col.innerHTML = _summaryHtml(summaryData) + _transcriptHtml(transcriptData);

    // Wire topic pill clicks
    col.querySelectorAll('.topic-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        const idx = parseInt(pill.dataset.topicIdx, 10);
        _filterByTopic(idx);
      });
    });

  } catch (err) {
    col.innerHTML = `<div class="browser-error">שגיאה בטעינה: ${_esc(err.message)}</div>`;
  }
}

/* ── Summary panel ───────────────────────────────────────────────── */
function _summaryHtml(data) {
  const topics = data.topics || [];
  if (!topics.length) return '';

  const pills = topics.map((t, i) =>
    `<button class="topic-pill" data-topic-idx="${i}"
             style="border-color:${topicColor(i)};color:${topicColor(i)};--topic-color:${topicColor(i)}">
       <span class="pill-dot" style="background:${topicColor(i)}"></span>
       ${_esc(t.heading)}
     </button>`
  ).join('');

  const sections = topics.map((t, i) =>
    `<div class="summary-section">
       <div class="summary-heading" style="color:${topicColor(i)}">
         <span class="pill-dot" style="background:${topicColor(i)}"></span>
         ${_esc(t.heading)}
       </div>
       <ul class="summary-bullets">
         ${t.bullets.map(b => `<li>${_esc(b)}</li>`).join('')}
       </ul>
     </div>`
  ).join('');

  return `
<details class="summary-panel" open>
  <summary class="summary-toggle">
    <span>סיכום AI</span>
    <span class="summary-pills-row">${pills}</span>
  </summary>
  <div class="summary-body">${sections}</div>
</details>`;
}

/* ── Transcript ──────────────────────────────────────────────────── */
function _transcriptHtml(data) {
  const chunks = data.chunks || [];
  if (!chunks.length) return '<div class="browser-empty">אין תמלול זמין</div>';

  const rows = chunks.map(c => {
    const color   = topicColor(c.topic_index);
    const initials = _initials(c.speaker);
    const pinBtn  = `<button class="chunk-pin" onclick="browserPinChunk('${_esc(c.chunk_id)}','${_esc(data.meeting_id)}')" title="הוסף לסל">📌</button>`;
    return `
<div class="chunk-card" data-chunk-id="${_esc(c.chunk_id)}" data-topic-idx="${c.topic_index ?? ''}">
  <div class="chunk-left">
    <div class="chunk-avatar" style="background:${color}20;color:${color}">${_esc(initials)}</div>
  </div>
  <div class="chunk-body" style="border-right-color:${color}">
    <div class="chunk-speaker-row">
      <span class="chunk-speaker">${_esc(c.speaker || '—')}</span>
      ${pinBtn}
    </div>
    <div class="chunk-text">${_esc(c.text)}</div>
  </div>
</div>`;
  }).join('');

  return `<div class="transcript-body" id="transcript-body">${rows}</div>`;
}

/* ── Topic filter ────────────────────────────────────────────────── */
function _filterByTopic(topicIdx) {
  const col = _panel.querySelector('#browser-transcript-col');
  if (!col) return;

  // Toggle off if same topic clicked again
  if (_activeTopicFilter === topicIdx) {
    _activeTopicFilter = null;
    col.querySelectorAll('.chunk-card').forEach(c => c.classList.remove('dimmed'));
    col.querySelectorAll('.topic-pill').forEach(p => p.classList.remove('active-pill'));
    return;
  }
  _activeTopicFilter = topicIdx;

  col.querySelectorAll('.topic-pill').forEach(p => {
    p.classList.toggle('active-pill', parseInt(p.dataset.topicIdx, 10) === topicIdx);
  });
  col.querySelectorAll('.chunk-card').forEach(c => {
    const cIdx = c.dataset.topicIdx === '' ? null : parseInt(c.dataset.topicIdx, 10);
    c.classList.toggle('dimmed', cIdx !== topicIdx);
  });

  // Scroll to first matching chunk
  const first = col.querySelector(`.chunk-card:not(.dimmed)`);
  if (first) first.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── Sidebar switch meeting ──────────────────────────────────────── */
function browserSwitchMeeting(meetingId) {
  if (meetingId === _activeId) return;
  _loadMeeting(meetingId);
}

/* ── Load more meetings ──────────────────────────────────────────── */
async function browserLoadMore() {
  const btn = _panel.querySelector('.sidebar-load-more');
  if (btn) btn.textContent = 'טוען…';
  try {
    const query = encodeURIComponent(_origQ || '');
    const res   = await fetch(`/api/research/${_sid}/rag?query=${query}&top_k=40`);
    const data  = await res.json();
    if (data.meetings) {
      const existingIds = new Set(_meetings.map(m => m.meeting_id));
      const newOnes = (data.meetings).filter(m => !existingIds.has(m.meeting_id));
      _meetings = [..._meetings, ...newOnes];
      _renderSidebar();
    }
  } catch (err) {
    if (btn) btn.textContent = 'שגיאה — נסה שוב';
  }
}

/* ── Pin chunk ───────────────────────────────────────────────────── */
async function browserPinChunk(chunkId, meetingId) {
  // Find the chunk text from DOM
  const col   = _panel.querySelector('#browser-transcript-col');
  const card  = col?.querySelector(`.chunk-card[data-chunk-id="${chunkId}"]`);
  const text  = card?.querySelector('.chunk-text')?.textContent?.trim() || '';

  try {
    await fetch(`/api/research/${_sid}/workspace/select`, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ chunk_id: chunkId, text, source_meeting_id: meetingId }),
    });
    // Visual feedback
    const btn = card?.querySelector('.chunk-pin');
    if (btn) { btn.textContent = '✅'; btn.disabled = true; }
  } catch { /* silent */ }
}

/* ── Panel chat (ask about current meeting) ──────────────────────── */
async function _browserAsk() {
  const input = _panel.querySelector('#browser-chat-input');
  const q = input?.value?.trim();
  if (!q) return;
  input.value = '';

  _streamWorkspaceAsk(q, _activeId);
}

/* ── Summarize button ────────────────────────────────────────────── */
async function _browserSummarize() {
  const q = _origQ || 'תמצת את הממצאים העיקריים מהפרוטוקולים שנמצאו';
  _streamWorkspaceAsk(q, _activeId);
}

/* ── Stream /workspace/ask → new agent message in main chat ─────── */
async function _streamWorkspaceAsk(question, meetingId) {
  const chatColumn = document.getElementById('chat-column');

  // User bubble
  const userRow = document.createElement('div');
  userRow.className = 'msg-user';
  userRow.innerHTML = `<div class="msg-user-bubble">${_esc(question)}</div>`;
  chatColumn.appendChild(userRow);

  // Agent card (streaming)
  const agentWrap = document.createElement('div');
  agentWrap.className = 'msg-agent';
  agentWrap.innerHTML = '<div class="msg-agent-card"><div class="prose-content"></div></div>';
  chatColumn.appendChild(agentWrap);
  chatColumn.scrollTop = chatColumn.scrollHeight;

  const prose = agentWrap.querySelector('.prose-content');
  let raw = '';
  let buf = '';
  let curEvent = '';

  try {
    const res = await fetch(`/api/research/${_sid}/workspace/ask`, {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ question, meeting_id: meetingId }),
    });

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (line.startsWith('event: ')) { curEvent = line.slice(7).trim(); }
        else if (line.startsWith('data: ')) {
          let data;
          try { data = JSON.parse(line.slice(6)); } catch { continue; }
          if (curEvent === 'token') {
            raw += data.text || '';
            prose.innerHTML = _esc(raw) + '<span class="stream-cursor"></span>';
            chatColumn.scrollTop = chatColumn.scrollHeight;
          } else if (curEvent === 'done') {
            prose.innerHTML = marked.parse(raw);
          }
        }
      }
    }
  } catch (err) {
    prose.innerHTML = `<span style="color:#b02500">שגיאה: ${_esc(err.message)}</span>`;
  }
  if (raw) prose.innerHTML = marked.parse(raw);
  chatColumn.scrollTop = chatColumn.scrollHeight;
}

/* ── Helpers ─────────────────────────────────────────────────────── */
function _initials(name) {
  if (!name) return '?';
  return name.trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('');
}

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
