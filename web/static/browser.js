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
 *   right (sidebar): meeting list, sort/filter controls
 *
 * API surface used:
 *   GET  /api/research/{id}/meeting/{mid}/summary
 *   GET  /api/research/{id}/meeting/{mid}/transcript
 *   GET  /api/research/{id}/meeting/{mid}/participants
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
let _standalone = false; // true when embedded in reading tab (no chat bar)
let _container  = null;  // DOM element the panel is appended into

/* ── Heatmap state ──────────────────────────────────────────────── */
let _hmChunks        = [];   // [{chunk_id, chars, simScore, topicScores}]
let _activeBulletIdx = null; // null = query mode; number = specific bullet

/* ── Sidebar sort / filter / group state ────────────────────────── */
let _sortMode        = 'relevance'; // 'relevance' | 'date_asc' | 'date_desc'
let _groupByComm     = false;
let _collapsedGroups = new Set();   // committee names that are collapsed
let _filterPart      = '';          // participant name filter string
let _partCache       = {};          // meeting_id → string[] (lowercase speaker names)
let _partLoadedCount = 0;           // how many participant fetches completed

/* ── Main entry point ───────────────────────────────────────────── */
/**
 * openProtocolBrowser(sessionId, meetingId, meetings, opts)
 *
 * opts.container    — DOM element to append into (default: #chat-column)
 * opts.standalone   — true: fills the container, hides chat bar
 * opts.postCompletion / opts.originalQuestion — as before
 */
function openProtocolBrowser(sessionId, meetingId, meetings, opts = {}) {
  _sid        = sessionId;
  _standalone = !!opts.standalone;
  _container  = opts.container || document.getElementById('chat-column');

  _meetings = (meetings || []).map(m => {
    const clean = s => String(s || '').replace(/_/g, ' ').trim();
    let title;
    if (m.committee && m.date) {
      title = `${clean(m.committee)} — ${clean(m.date)}`;
    } else if (m.title && m.title !== m.meeting_id) {
      title = clean(m.title);
    } else {
      title = clean(m.committee || m.date || m.meeting_id);
    }
    return { ...m, title };
  });
  _activeId          = meetingId;
  _origQ             = opts.originalQuestion || '';
  _sortMode          = 'relevance';
  _groupByComm       = false;
  _collapsedGroups   = new Set();
  _filterPart        = '';
  _partCache         = {};
  _partLoadedCount   = 0;
  _hmChunks          = [];
  _activeBulletIdx   = null;

  // Replace any existing panel
  if (_panel) _panel.remove();

  _panel = document.createElement('div');
  _panel.className = _standalone ? 'browser-standalone-wrapper' : 'msg-agent browser-wrapper';
  _panel.innerHTML = _shellHtml(opts.postCompletion);

  _container.appendChild(_panel);

  const qLabel = _panel.querySelector('#browser-question-label');
  if (qLabel) qLabel.textContent = _origQ || 'עיון בפרוטוקולים';

  _renderSidebar();
  _loadMeeting(meetingId);
  _loadAllParticipants();

  // On mobile, auto-collapse the sidebar in standalone mode
  if (_standalone && window.innerWidth < 768) {
    const sb = _panel.querySelector('#browser-sidebar');
    if (sb) sb.classList.add('sidebar-collapsed');
    const sideTab = _panel.querySelector('#sidebar-side-tab');
    if (sideTab) sideTab.style.display = 'flex';
  }

  // Panel chat wiring (only when not standalone)
  if (!_standalone) {
    const chatSubmit = _panel.querySelector('#browser-chat-submit');
    const chatInput  = _panel.querySelector('#browser-chat-input');
    if (chatSubmit) chatSubmit.addEventListener('click', _browserAsk);
    if (chatInput)  chatInput.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _browserAsk(); }
    });
  }

  // Summarize button (if shown)
  const sumBtn = _panel.querySelector('#browser-summarize-btn');
  if (sumBtn) sumBtn.addEventListener('click', _browserSummarize);

  _container.scrollTop = _container.scrollHeight;
}

/* ── Shell HTML ─────────────────────────────────────────────────── */
function _shellHtml(postCompletion) {
  // In standalone mode: no "summarize for me", no chat bar, no close button
  const summarizeBtn = (postCompletion || _standalone) ? '' :
    `<button id="browser-summarize-btn" class="browser-summarize-btn" title="תמצת על בסיס הפרוטוקולים שנמצאו">
      תמצת עבורי
    </button>`;
  const closeBtn = _standalone ? '' :
    `<button class="browser-close-btn" onclick="closeProtocolBrowser()" title="סגור">✕</button>`;
  const chatBar = _standalone ? '' : `
  <div class="browser-chat-bar">
    <textarea id="browser-chat-input" placeholder="שאל שאלה על הישיבה הזו… (Ctrl+Enter)" rows="1"></textarea>
    <button id="browser-chat-submit" class="browser-chat-submit">שלח</button>
  </div>`;

  return `
<div class="browser-panel">
  <div class="browser-header">
    <!-- Mobile: always-visible sidebar toggle icon -->
    <button class="sidebar-mob-btn" onclick="browserToggleSidebar()" title="ישיבות">
      <span class="material-symbols-outlined" style="font-size:20px">format_list_bulleted</span>
    </button>
    <!-- Desktop: appears when sidebar is collapsed -->
    <button class="sidebar-expand-btn" id="sidebar-expand-btn"
            onclick="browserToggleSidebar()" title="הצג ישיבות" style="display:none">
      <span class="material-symbols-outlined" style="font-size:15px">format_list_bulleted</span>
      <span>ישיבות</span>
    </button>
    <button class="browser-summary-btn" id="browser-summary-btn" onclick="browserToggleSummary()" title="סיכום AI" style="display:none">
      <span class="material-symbols-outlined" style="font-size:16px;font-variation-settings:'FILL' 1">auto_awesome</span>
      <span>סיכום</span>
    </button>
    <span class="browser-breadcrumb" id="browser-question-label"></span>
    <div class="browser-header-actions">
      ${summarizeBtn}
      ${closeBtn}
    </div>
  </div>
  <div class="browser-summary-bar" id="browser-summary-bar"></div>
  <div class="browser-body">
    <div class="browser-transcript-wrap">
      <div class="browser-transcript-col" id="browser-transcript-col">
        <div class="browser-loading">טוען…</div>
      </div>
      <div class="heatmap-strip" id="heatmap-strip">
        <div class="hm-bands" id="hm-bands"></div>
        <div class="hm-viewport" id="hm-viewport"></div>
      </div>
    </div>
    <div class="browser-sidebar" id="browser-sidebar">
      <!-- Desktop: collapse button above meeting list -->
      <div class="sidebar-top-header">
        <span class="sidebar-top-title">ישיבות</span>
        <button class="sidebar-top-close" onclick="browserToggleSidebar()" title="הסתר">
          <span class="material-symbols-outlined" id="sidebar-top-arrow" style="font-size:18px">chevron_right</span>
        </button>
      </div>
      <div class="sidebar-inner">
        <div class="sidebar-controls">
          <div class="sidebar-sort-row">
            <label class="sort-label">מיין:</label>
            <select id="sort-select" class="sort-select" onchange="browserSetSort(this.value)">
              <option value="relevance">רלוונטיות</option>
              <option value="date_desc">תאריך ↓</option>
              <option value="date_asc">תאריך ↑</option>
            </select>
            <button id="sort-grp" class="sort-btn" onclick="browserToggleGroup()">קבץ לפי ועדה</button>
          </div>
          <div class="sidebar-filter-row">
            <input id="sidebar-part-input" class="sidebar-part-input"
                   placeholder="טוען משתתפים…"
                   oninput="browserFilterParticipant(this.value)"
                   disabled />
          </div>
        </div>
        <div class="sidebar-list" id="sidebar-list"></div>
        <button class="sidebar-load-more" onclick="browserLoadMore()">טען עוד ישיבות</button>
      </div>
    </div>
  </div>
  <button class="sidebar-side-tab" id="sidebar-side-tab" onclick="browserToggleSidebar()" title="פתח רשימת ישיבות" style="display:none">
    <span class="material-symbols-outlined" id="sidebar-side-tab-icon" style="font-size:18px">format_list_bulleted</span>
  </button>
  ${chatBar}
</div>`;
}

function closeProtocolBrowser() {
  if (_panel) { _panel.remove(); _panel = null; }
}

/* ── Sidebar ─────────────────────────────────────────────────────── */
function _renderSidebar() {
  const list = _panel.querySelector('#sidebar-list');
  if (!list) return;

  // Apply participant filter
  let meetings = [..._meetings];
  const q = _filterPart.toLowerCase().trim();
  if (q) {
    meetings = meetings.filter(m => {
      const parts = _partCache[m.meeting_id];
      if (parts === undefined) return true; // not yet loaded — keep visible
      return parts.some(p => p.includes(q));
    });
  }

  // Apply sort
  if (_sortMode === 'date_asc') {
    meetings.sort((a, b) => _parseDateMs(a.date) - _parseDateMs(b.date));
  } else if (_sortMode === 'date_desc') {
    meetings.sort((a, b) => _parseDateMs(b.date) - _parseDateMs(a.date));
  }
  // 'relevance': keep original RAG order

  list.innerHTML = _groupByComm ? _groupedHtml(meetings) : _flatHtml(meetings);

  // Sync sort controls
  const sel = _panel.querySelector('#sort-select');
  if (sel) sel.value = _sortMode;
  const grpBtn = _panel.querySelector('#sort-grp');
  if (grpBtn) grpBtn.classList.toggle('sort-active', _groupByComm);
}

/* ── Flat meeting list HTML ─────────────────────────────────────── */
function _flatHtml(meetings) {
  if (!meetings.length) return '<div class="sidebar-empty">אין תוצאות</div>';
  return meetings.map(m => _meetingCardHtml(m, false)).join('');
}

/* ── Grouped (by committee) HTML ────────────────────────────────── */
function _groupedHtml(meetings) {
  if (!meetings.length) return '<div class="sidebar-empty">אין תוצאות</div>';

  // Build groups
  const groups = {};
  for (const m of meetings) {
    const key = (m.committee || '').replace(/_/g, ' ').trim() || 'אחר';
    if (!groups[key]) groups[key] = [];
    groups[key].push(m);
  }

  // Sort groups: by best score (relevance) or alphabetically (date sort)
  const comms = Object.keys(groups).sort((a, b) => {
    if (_sortMode === 'relevance') {
      const best = g => Math.max(...groups[g].map(m => m.score || 0));
      return best(b) - best(a);
    }
    return a.localeCompare(b, 'he');
  });

  return comms.map(comm => {
    const collapsed = _collapsedGroups.has(comm);
    const cards     = collapsed ? '' : groups[comm].map(m => _meetingCardHtml(m, true)).join('');
    return `<div class="sidebar-group">
      <div class="sidebar-group-header" onclick="browserToggleCommGroup('${_esc(comm)}')">
        <span class="group-arrow">${collapsed ? '▶' : '▼'}</span>
        <span class="group-name">${_esc(comm)}</span>
        <span class="group-count">${groups[comm].length}</span>
      </div>
      ${cards}
    </div>`;
  }).join('');
}

/* ── Single meeting card HTML ───────────────────────────────────── */
function _meetingCardHtml(m, inGroup) {
  const active    = m.meeting_id === _activeId;
  const pct       = Math.round((m.score || 0) * 100);
  const badgeCls  = pct >= 65 ? 'rel-green' : pct >= 50 ? 'rel-blue' : 'rel-grey';
  const dateStr   = (m.date || m.meeting_id).replace(/_/g, '/');
  const commHtml  = inGroup ? '' :
    `<span class="sidebar-committee">${_esc((m.committee || '').replace(/_/g, ' '))}</span>`;
  return `<div class="sidebar-meeting ${active ? 'active' : ''} ${inGroup ? 'in-group' : ''}"
               onclick="browserSwitchMeeting('${_esc(m.meeting_id)}')">
    <div class="sidebar-meeting-title">${_esc(dateStr)}</div>
    <div class="sidebar-meeting-meta">
      ${commHtml}
      <span class="rel-badge ${badgeCls}">${pct}%</span>
    </div>
  </div>`;
}

/* ── Date → ms for sorting (DD_MM_YYYY or DD/MM/YYYY) ──────────── */
function _parseDateMs(dateStr) {
  if (!dateStr) return 0;
  const p = String(dateStr).replace(/_/g, '/').split('/');
  if (p.length < 3) return 0;
  return new Date(+p[2], +p[1] - 1, +p[0]).getTime() || 0;
}

/* ── Sort / group / filter handlers ────────────────────────────── */
function browserSetSort(mode) {
  _sortMode = mode;
  _renderSidebar();
}

function browserToggleGroup() {
  _groupByComm = !_groupByComm;
  _collapsedGroups.clear();
  _renderSidebar();
}

function browserToggleCommGroup(comm) {
  if (_collapsedGroups.has(comm)) _collapsedGroups.delete(comm);
  else _collapsedGroups.add(comm);
  _renderSidebar();
}

function browserFilterParticipant(value) {
  _filterPart = value;
  _renderSidebar();
}

function browserToggleSidebar() {
  const sb = _panel?.querySelector('#browser-sidebar');
  if (!sb) return;
  const collapsed = sb.classList.toggle('sidebar-collapsed');
  // Desktop: show expand-btn in header when collapsed
  const expandBtn = _panel?.querySelector('#sidebar-expand-btn');
  if (expandBtn) expandBtn.style.display = collapsed ? 'flex' : 'none';
  // Desktop: flip the arrow in the sidebar top header
  const arrow = _panel?.querySelector('#sidebar-top-arrow');
  if (arrow) arrow.textContent = collapsed ? 'chevron_left' : 'chevron_right';
  // Mobile: flip the icon in the header button
  const mobIcon = _panel?.querySelector('.sidebar-mob-btn .material-symbols-outlined');
  if (mobIcon) mobIcon.textContent = collapsed ? 'format_list_bulleted' : 'close';
  // Mobile: side-tab visible only when sidebar is collapsed
  const sideTab = _panel?.querySelector('#sidebar-side-tab');
  if (sideTab) sideTab.style.display = collapsed ? 'flex' : 'none';
  const sideTabIcon = _panel?.querySelector('#sidebar-side-tab-icon');
  if (sideTabIcon) sideTabIcon.textContent = 'format_list_bulleted';
}

function browserToggleSummary() {
  const bar = _panel?.querySelector('#browser-summary-bar');
  if (!bar) return;
  const open = bar.classList.toggle('open');
  const btn = _panel?.querySelector('#browser-summary-btn span:not(.material-symbols-outlined)');
  if (btn) btn.textContent = open ? 'סגור' : 'סיכום';
}

/* ── Participant loading ─────────────────────────────────────────── */
async function _loadAllParticipants() {
  const toLoad = [..._meetings];
  _partLoadedCount = 0;
  await Promise.all(toLoad.map(m => _loadParticipantsFor(m.meeting_id, toLoad.length)));
}

async function _loadParticipantsFor(meetingId, total) {
  if (_partCache[meetingId] !== undefined) { // already cached
    _onParticipantLoaded(total);
    return;
  }
  try {
    const res  = await fetch(`/api/research/${_sid}/meeting/${encodeURIComponent(meetingId)}/participants`);
    const data = await res.json();
    _partCache[meetingId] = (data.participants || []).map(p => p.toLowerCase());
  } catch {
    _partCache[meetingId] = [];
  }
  _onParticipantLoaded(total);
}

function _onParticipantLoaded(total) {
  _partLoadedCount++;
  if (_partLoadedCount >= total) {
    const inp = _panel?.querySelector('#sidebar-part-input');
    if (inp) { inp.disabled = false; inp.placeholder = 'סנן לפי משתתף…'; }
    if (_filterPart) _renderSidebar(); // re-render now all data is available
  }
}

/* ── Load meeting (summary + transcript) ─────────────────────────── */
async function _loadMeeting(meetingId) {
  _activeId          = meetingId;
  _activeTopicFilter = null;
  _activeBulletIdx   = null;
  _hmChunks          = [];
  _summary           = null;

  const m = _meetings.find(x => x.meeting_id === meetingId);

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
    col.innerHTML =
      `<div class="transcript-inner">` +
        _summaryHtml(summaryData, m) +
        _transcriptHtml(transcriptData) +
      `</div>`;

    // Populate header summary bar (desktop)
    const summaryBar = _panel?.querySelector('#browser-summary-bar');
    if (summaryBar) {
      summaryBar.innerHTML = '<div class="summary-bar-inner">' + _summaryBodyHtml(summaryData) + '</div>';
    }
    const summaryBtn = _panel?.querySelector('#browser-summary-btn');
    if (summaryBtn) summaryBtn.style.display = 'flex';
    // Wire bullet clicks in header bar
    _panel?.querySelectorAll('#browser-summary-bar .summary-bullet-btn[data-bullet-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.bulletIdx, 10);
        _activeBulletIdx = (_activeBulletIdx === idx) ? null : idx;
        _renderHeatmap();
        _highlightBulletBtn(idx);
      });
    });

    // Wire summary bullet clicks for heatmap reranking
    col.querySelectorAll('.summary-bullet-btn[data-bullet-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.bulletIdx, 10);
        _filterByBullet(idx);
      });
    });

    // Init heatmap with grey bands (no scores yet)
    _initHeatmap(transcriptData.chunks || []);

    // Heatmap strip click → proportional scroll.
    // Uses col.scrollHeight coordinates to match hm-viewport tracking.
    const strip = _panel.querySelector('#heatmap-strip');
    strip.addEventListener('click', e => {
      const rect = strip.getBoundingClientRect();
      const pct  = (e.clientY - rect.top) / rect.height;
      col.scrollTo({ top: Math.max(0, pct * col.scrollHeight), behavior: 'smooth' });
    });

    // Scroll listener → update viewport indicator
    col.addEventListener('scroll', _updateHeatmapViewport, { passive: true });

    // Async: score pass-2 chunks and fill heatmap colors
    _scoreAndRenderHeatmap(meetingId);

  } catch (err) {
    col.innerHTML = `<div class="browser-loading"><div class="browser-error">שגיאה בטעינה: ${_esc(err.message)}</div></div>`;
  }
}

/* ── Summary panel ───────────────────────────────────────────────── */
function _summaryHtml(data, m) {
  const topics = data.topics || [];
  if (!topics.length) return '';

  const metaParts = [];
  if (m?.committee) metaParts.push(_esc((m.committee + '').replace(/_/g, ' ').trim()));
  if (m?.date)      metaParts.push(_esc((m.date + '').replace(/_/g, '/')));
  const metaHtml = metaParts.length
    ? `<span class="summary-toggle-sep">|</span><span class="summary-toggle-meta">${metaParts.join(' | ')}</span>`
    : '';

  const sections = topics.map((t, i) => {
    const bullets = (t.bullets || []).map(b => {
      // b is {text, bullet_idx} (new API) or a plain string (legacy)
      const text      = typeof b === 'string' ? b : b.text;
      const bulletIdx = typeof b === 'string' ? null : b.bullet_idx;
      const idxAttr   = bulletIdx != null ? `data-bullet-idx="${bulletIdx}"` : '';
      return `<li>
        <button class="summary-bullet-btn" ${idxAttr}>
          <span class="bullet-indicator"></span>
          <span>${marked.parseInline(text)}</span>
        </button>
      </li>`;
    }).join('');
    return `<div class="summary-section">
       <div class="summary-heading">
         ${_esc(t.heading)}
       </div>
       <ul class="summary-bullets">${bullets}</ul>
     </div>`;
  }).join('');

  return `
<details class="summary-panel">
  <summary class="summary-toggle">
    <span class="summary-toggle-arrow">▼</span>
    <span>סיכום AI</span>
    ${metaHtml}
  </summary>
  <div class="summary-body">${sections}</div>
</details>`;
}

function _summaryBodyHtml(data) {
  const topics = data.topics || [];
  if (!topics.length) return '';
  return topics.map((t, i) => {
    const bullets = (t.bullets || []).map(b => {
      const text      = typeof b === 'string' ? b : b.text;
      const bulletIdx = typeof b === 'string' ? null : b.bullet_idx;
      const idxAttr   = bulletIdx != null ? `data-bullet-idx="${bulletIdx}"` : '';
      return `<li>
        <button class="summary-bullet-btn" ${idxAttr}>
          <span class="bullet-indicator"></span>
          <span>${marked.parseInline(text)}</span>
        </button>
      </li>`;
    }).join('');
    return `<div class="summary-section">
       <div class="summary-heading">${_esc(t.heading)}</div>
       <ul class="summary-bullets">${bullets}</ul>
     </div>`;
  }).join('');
}

function _highlightBulletBtn(idx) {
  _panel?.querySelectorAll('.summary-bullet-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.bulletIdx, 10) === idx);
  });
}

/* ── Transcript ──────────────────────────────────────────────────── */
function _transcriptHtml(data) {
  const chunks = data.chunks || [];
  if (!chunks.length) return '<div class="browser-empty">אין תמלול זמין</div>';

  const rows = chunks.map(c => {
    const color   = topicColor(c.topic_index);
    const initials = _initials(c.speaker);
    return `
<div class="chunk-card" data-chunk-id="${_esc(c.chunk_id)}" data-topic-idx="${c.topic_index ?? ''}">
  <div class="chunk-left">
    <div class="chunk-avatar" style="background:${color}20;color:${color}">${_esc(initials)}</div>
  </div>
  <div class="chunk-body" style="border-right-color:${color}">
    <div class="chunk-speaker-row">
      <span class="chunk-speaker">${_esc(c.speaker || '—')}</span>
    </div>
    <div class="chunk-text">${_esc(c.text)}</div>
  </div>
</div>`;
  }).join('');

  return `<div class="transcript-body" id="transcript-body" data-meeting-id="${_esc(data.meeting_id)}">${rows}</div>`;
}

/* ── Heatmap: init, render, viewport indicator ───────────────────── */

function _initHeatmap(chunks) {
  _activeBulletIdx = null;
  _hmChunks = chunks.map(c => ({
    chunk_id:    c.chunk_id,
    chars:       (c.text || '').length || 1,
    simScore:    null,
    topicScores: [],
  }));
  _renderHeatmap(_hmChunks.map(c => c.simScore));
  // Show wave animation while scores load
  _panel?.querySelector('#heatmap-strip')?.classList.add('loading');
  requestAnimationFrame(_updateHeatmapViewport);
}

/* Score all pass-2 chunks for meetingId against the current question, then update heatmap */
async function _scoreAndRenderHeatmap(meetingId) {
  const query = _origQ;
  if (!query) {
    _panel?.querySelector('#heatmap-strip')?.classList.remove('loading');
    return;
  }

  try {
    const res = await fetch(
      `/api/research/${_sid}/meeting/${encodeURIComponent(meetingId)}/score_pass2`,
      {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ query }),
      }
    );
    const data = await res.json();
    if (meetingId !== _activeId) return;

    if (!data.error && data.chunks?.length) {
      for (let i = 0; i < _hmChunks.length; i++) {
        const speechIdx = parseInt(_hmChunks[i].chunk_id, 10);
        for (const rc of data.chunks) {
          if (rc.start <= speechIdx && speechIdx <= rc.end) {
            _hmChunks[i].simScore    = rc.score;
            _hmChunks[i].topicScores = rc.tvec || [];
            break;
          }
        }
      }
      _renderHeatmap(_hmChunks.map(c => c.simScore));
    }
  } catch (_) { /* scoring is best-effort */ } finally {
    _panel?.querySelector('#heatmap-strip')?.classList.remove('loading');
  }
}

/* scores: array of float|null, same order as _hmChunks */
function _renderHeatmap(scores) {
  const bandsEl = _panel?.querySelector('#hm-bands');
  if (!bandsEl) return;

  const total = _hmChunks.reduce((s, c) => s + c.chars, 0) || 1;

  bandsEl.innerHTML = _hmChunks.map((c, i) => {
    const flex = c.chars / total;
    return `<div class="hm-band" style="flex:${flex}; background:${_heatColor(scores[i])}"
                 title="${_heatLabel(scores[i])}"></div>`;
  }).join('');
}

/* Salmon→yellow-green→green gradient: null→grey, 0→salmon, 1→interface green */
function _heatColor(s) {
  if (s == null) return '#e2e3df';
  const t = Math.max(0, Math.min(1, s));
  // hue: 12 (salmon-red) → 122 (green)
  // sat: 80 → 45%
  // lightness: 80 → 38%
  const h = Math.round(12  + t * 110);
  const sv = Math.round(80 - t * 35);
  const l  = Math.round(80 - t * 42);
  return `hsl(${h},${sv}%,${l}%)`;
}

function _heatLabel(s) {
  return s == null ? '—' : Math.round(s * 100) + '%';
}

function _updateHeatmapViewport() {
  const col   = _panel?.querySelector('#browser-transcript-col');
  const strip = _panel?.querySelector('#heatmap-strip');
  const vp    = _panel?.querySelector('#hm-viewport');
  if (!col || !strip || !vp) return;

  const sh = col.scrollHeight;
  const ch = strip.clientHeight;
  if (!sh || !ch) return;

  const ratio = ch / sh;
  const vpH   = Math.max(16, col.clientHeight * ratio);
  vp.style.height = vpH + 'px';
  vp.style.top    = (col.scrollTop * ratio) + 'px';
}

/* ── Bullet filter → heatmap rerank ─────────────────────────────── */
function _filterByBullet(bulletIdx) {
  const col = _panel?.querySelector('#browser-transcript-col');
  if (!col) return;

  // Toggle off if same bullet clicked
  if (_activeBulletIdx === bulletIdx) {
    _activeBulletIdx  = null;
    _activeTopicFilter = null;
    _renderHeatmap(_hmChunks.map(c => c.simScore));
    col.querySelectorAll('.chunk-card').forEach(c => c.classList.remove('dimmed'));
    col.querySelectorAll('.summary-bullet-btn').forEach(b => b.classList.remove('active'));
    return;
  }

  _activeBulletIdx  = bulletIdx;
  _activeTopicFilter = bulletIdx;

  // Raw tvec scores are L1-normalised (tiny, ~1/N each).
  // Normalise by max so heatmap + dimming use relative ranking.
  const rawScores = _hmChunks.map(c => c.topicScores[bulletIdx] ?? null);
  const maxS = Math.max(...rawScores.filter(s => s != null), 0);
  const normScores = maxS > 0
    ? rawScores.map(s => s != null ? s / maxS : null)
    : rawScores;

  _renderHeatmap(normScores);

  // Dim chunks in lower half of relevance for this bullet
  col.querySelectorAll('.chunk-card').forEach((card, i) => {
    const s = normScores[i] ?? 0;
    card.classList.toggle('dimmed', s < 0.35);
  });

  // Mark active bullet
  col.querySelectorAll('.summary-bullet-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.bulletIdx, 10) === bulletIdx);
  });

  // Scroll to first relevant chunk
  const first = col.querySelector('.chunk-card:not(.dimmed)');
  if (first) {
    const target = col.scrollTop
      + first.getBoundingClientRect().top
      - col.getBoundingClientRect().top
      - 32;
    col.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }
}

/* ── Scroll transcript to chunk ──────────────────────────────────── */
function browserScrollToChunk(chunkId) {
  const col  = _panel?.querySelector('#browser-transcript-col');
  const card = col?.querySelector(`.chunk-card[data-chunk-id="${chunkId}"]`);
  if (!col || !card) return;

  const target = col.scrollTop
    + card.getBoundingClientRect().top
    - col.getBoundingClientRect().top
    - col.clientHeight / 2
    + card.clientHeight / 2;
  col.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
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
      const newOnes = data.meetings.filter(m => !existingIds.has(m.meeting_id));
      _meetings = [..._meetings, ...newOnes];   // _meetingLabel normalises at render time
      _renderSidebar();
      if (newOnes.length) _loadNewParticipants(newOnes);
    }
  } catch (err) {
    if (btn) btn.textContent = 'שגיאה — נסה שוב';
  }
}

/* called after browserLoadMore adds new meetings to _meetings */
async function _loadNewParticipants(newMeetings) {
  await Promise.all(newMeetings.map(m => _loadParticipantsFor(m.meeting_id, newMeetings.length)));
  if (_filterPart) _renderSidebar();
}


/* ── Panel chat (ask about current meeting) ──────────────────────── */
async function _browserAsk() {
  const input = _panel.querySelector('#browser-chat-input');
  const q = input?.value?.trim();
  if (!q) return;

  // _validateQuestion defined in app.js (loaded before browser.js)
  const _err = typeof _validateQuestion === 'function' ? _validateQuestion(q) : null;
  if (_err) {
    let hint = _panel.querySelector('#browser-chat-error');
    if (!hint) {
      hint = document.createElement('span');
      hint.id = 'browser-chat-error';
      hint.className = 'input-error';
      input.parentElement?.appendChild(hint);
    }
    hint.textContent = _err;
    setTimeout(() => { hint.textContent = ''; }, 4000);
    return;
  }

  const hint = _panel.querySelector('#browser-chat-error');
  if (hint) hint.textContent = '';

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
  const chatColumn = document.getElementById('chat-column') || _container;

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
function _meetingLabel(m) {
  const clean = s => String(s || '').replace(/_/g, ' ').trim();
  const comm  = clean(m.committee);
  const date  = clean(m.date);
  if (comm && date) return `${comm} — ${date}`;
  if (comm)         return comm;
  if (date)         return date;
  const t = clean(m.title);
  const id = String(m.meeting_id || '');
  return (t && t !== id) ? t : `ישיבה ${id}`;
}

function _initials(name) {
  if (!name) return '?';
  return name.trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('');
}

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
