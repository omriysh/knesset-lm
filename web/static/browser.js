/**
 * browser.js — Protocol Browser panel
 *
 * openProtocolBrowser(sessionId, meetingId, meetings, opts)
 *   sessionId  — active research session
 *   meetingId  — meeting to show on open
 *   meetings   — [{meeting_id, title, date, committee, score}] from /api/browse/search
 *   opts       — { originalQuestion, postCompletion, searchRequest, focus, pushUrl, ... }
 *
 * In the reading tab (standalone) the open meeting, the clicked speech and the selected text are
 * mirrored into the page URL (url_state.js), so the address bar and the share buttons link to them.
 *
 * openProtocolBrowserWithSearch(sessionId, searchRequest, opts)
 *   runs /api/browse/search first (empty query = newest meetings), then opens.
 *
 * Renders inline in the chat column.  Two-column layout (RTL):
 *   left: transcript + AI summary + panel chat
 *   right (sidebar): meeting list, sort/filter controls
 *
 * API surface used:
 *   GET  /api/research/{id}/meeting/{mid}/summary
 *   GET  /api/research/{id}/meeting/{mid}/transcript
 *   GET  /api/research/{id}/meeting/{mid}/participants
 *   GET  /api/research/{id}/meeting/{mid}/hits?q=...    (heatmap: matching speeches)
 *   POST /api/browse/search                              (load more)
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
let _origQ     = '';     // original question (for summarize button)
let _standalone = false; // true when embedded in reading tab (no chat bar)
let _container  = null;  // DOM element the panel is appended into
let _pendingFocus = null;       // {speech, offset, length} or {speech, quote} to show once the meeting loads
let _searchRequest = null;      // /api/browse/search body behind _meetings (null = no "load more")

/* ── Heatmap state ──────────────────────────────────────────────── */
let _hmHits           = [];     // /hits rows for the active query: {speech_idx, matched_words, query_words, ranges}
let _activeHitsQuery  = null;   // topic text currently driving the heatmap (null = search query)

/* ── Sidebar sort / filter / group state ────────────────────────── */
let _sortMode        = 'relevance'; // 'relevance' | 'date_asc' | 'date_desc'
let _groupByComm     = false;
let _collapsedGroups = new Set();   // committee names that are collapsed

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
  _sortMode          = opts.sortMode || 'relevance';
  _groupByComm       = false;
  _collapsedGroups   = new Set();
  _hmHits            = [];
  _activeHitsQuery   = null;
  _searchRequest     = opts.searchRequest || null;
  _pendingFocus = opts.focus?.speech != null ? opts.focus : null;

  // Replace any existing panel
  if (_panel) _panel.remove();

  _panel = document.createElement('div');
  _panel.className = _standalone ? 'browser-standalone-wrapper' : 'msg-agent browser-wrapper';
  _panel.innerHTML = _shellHtml(opts.postCompletion);

  _container.appendChild(_panel);

  const qLabel = _panel.querySelector('#browser-question-label');
  if (qLabel) qLabel.textContent = _origQ || 'עיון בפרוטוקולים';

  _renderSidebar();
  _wireTranscriptSharing();
  if (meetingId) _loadMeeting(meetingId, { pushUrl: opts.pushUrl !== false });
  else _panel.querySelector('#browser-transcript-col').innerHTML =
    '<div class="browser-loading">לא נמצאו ישיבות</div>';
  const loadMoreBtn = _panel.querySelector('.sidebar-load-more');
  if (loadMoreBtn && !_searchRequest) loadMoreBtn.style.display = 'none';

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
    `<button id="browser-summarize-btn" class="browser-summarize-btn" title="תמצות על בסיס הפרוטוקולים שנמצאו">
      תמצות הממצאים
    </button>`;
  const closeBtn = _standalone ? '' :
    `<button class="browser-close-btn" data-click="closeProtocolBrowser" title="סגירה">✕</button>`;
  const chatBar = _standalone ? '' : `
  <div class="browser-chat-bar">
    <textarea id="browser-chat-input" placeholder="שאלה על הישיבה הזו… (Ctrl+Enter)" rows="1"></textarea>
    <button id="browser-chat-submit" class="browser-chat-submit">שליחה</button>
  </div>`;

  return `
<div class="browser-panel">
  <div class="browser-header">
    <!-- Mobile: always-visible sidebar toggle icon -->
    <button class="sidebar-mob-btn" data-click="browserToggleSidebar" title="ישיבות">
      <span class="material-symbols-outlined" style="font-size:20px">format_list_bulleted</span>
    </button>
    <!-- Desktop: appears when sidebar is collapsed -->
    <button class="sidebar-expand-btn" id="sidebar-expand-btn"
            data-click="browserToggleSidebar" title="הצגת ישיבות" style="display:none">
      <span class="material-symbols-outlined" style="font-size:15px">format_list_bulleted</span>
      <span>ישיבות</span>
    </button>
    <button class="browser-summary-btn" id="browser-summary-btn" data-click="browserToggleSummary" title="סיכום" style="display:none">
      <span class="material-symbols-outlined" style="font-size:16px;font-variation-settings:'FILL' 1">auto_awesome</span>
      <span>סיכום</span>
    </button>
    <span class="browser-breadcrumb" id="browser-question-label"></span>
    <div class="browser-header-actions">
      ${_standalone ? `<button class="browser-share-btn" data-click="browserShareMeeting" data-share-level="meeting" title="קישור לישיבה">
        <span class="material-symbols-outlined" style="font-size:16px">share</span><span>לשיתוף</span>
      </button>` : ''}
      ${summarizeBtn}
      ${closeBtn}
    </div>
  </div>
  <div class="browser-summary-bar" id="browser-summary-bar"></div>
  <div class="browser-body">
    <div class="browser-transcript-wrap">
      <div class="browser-transcript-col" id="browser-transcript-col">
        <div class="browser-loading">בטעינה…</div>
      </div>
      <div class="heatmap-strip" id="heatmap-strip">
        <div class="hm-bands" id="hm-bands"></div>
        <div class="hm-viewport" id="hm-viewport"></div>
      </div>
      <div class="hm-tip" id="hm-tip" hidden></div>
      <div class="proto-nav" id="proto-nav">
        <button class="proto-nav-btn" data-click="browserNav" data-arg="start" title="לתחילת הפרוטוקול">
          <span class="material-symbols-outlined">vertical_align_top</span>
        </button>
        <div class="proto-nav-hits" id="proto-nav-hits" hidden>
          <button class="proto-nav-btn" data-click="browserNav" data-arg="prev" title="להתאמה הקודמת">
            <span class="material-symbols-outlined">keyboard_arrow_up</span>
          </button>
          <span class="proto-nav-count" id="proto-nav-count"></span>
          <button class="proto-nav-btn" data-click="browserNav" data-arg="next" title="להתאמה הבאה">
            <span class="material-symbols-outlined">keyboard_arrow_down</span>
          </button>
        </div>
        <button class="proto-nav-btn" data-click="browserNav" data-arg="end" title="לסוף הפרוטוקול">
          <span class="material-symbols-outlined">vertical_align_bottom</span>
        </button>
      </div>
    </div>
    <div class="browser-sidebar" id="browser-sidebar">
      <!-- Desktop: collapse button above meeting list -->
      <div class="sidebar-top-header">
        <span class="sidebar-top-title">ישיבות</span>
        <button class="sidebar-top-close" data-click="browserToggleSidebar" title="הסתרה">
          <span class="material-symbols-outlined" id="sidebar-top-arrow" style="font-size:18px">chevron_right</span>
        </button>
      </div>
      <div class="sidebar-inner">
        <div class="sidebar-controls">
          <div class="seg seg--sm seg--block" id="sidebar-sort">
            <button class="seg-btn" data-click="browserSetSort" data-arg="relevance">רלוונטיות</button>
            <button class="seg-btn" data-click="browserSetSort" data-arg="date_desc">מהחדש</button>
            <button class="seg-btn" data-click="browserSetSort" data-arg="date_asc">מהישן</button>
          </div>
          <div class="seg seg--sm seg--block" id="sidebar-group">
            <button class="seg-btn" data-click="browserSetGroup" data-arg="list">רשימה</button>
            <button class="seg-btn" data-click="browserSetGroup" data-arg="committee">לפי ועדה</button>
          </div>
        </div>
        <div class="sidebar-list" id="sidebar-list"></div>
        <button class="sidebar-load-more" data-click="browserLoadMore">טעינת ישיבות נוספות</button>
      </div>
    </div>
  </div>
  <button class="sidebar-side-tab" id="sidebar-side-tab" data-click="browserToggleSidebar" title="רשימת הישיבות" style="display:none">
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

  const meetings = [..._meetings];

  // Apply sort
  if (_sortMode === 'date_asc') {
    meetings.sort((a, b) => _parseDateMs(a.date) - _parseDateMs(b.date));
  } else if (_sortMode === 'date_desc') {
    meetings.sort((a, b) => _parseDateMs(b.date) - _parseDateMs(a.date));
  }
  // 'relevance': keep server order

  list.innerHTML = _groupByComm ? _groupedHtml(meetings) : _flatHtml(meetings);

  const hasQuery = !!_searchRequest?.query;
  _panel.querySelectorAll('#sidebar-sort .seg-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.arg === _sortMode);
    if (btn.dataset.arg === 'relevance') {
      btn.disabled = !hasQuery;
      btn.title = hasQuery ? '' : 'דירוג לפי רלוונטיות זמין בחיפוש מילות מפתח';
    }
  });
  _panel.querySelectorAll('#sidebar-group .seg-btn').forEach(btn =>
    btn.classList.toggle('active', (btn.dataset.arg === 'committee') === _groupByComm));
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
      <div class="sidebar-group-header${collapsed ? ' collapsed' : ''}" data-committee="${_esc(comm)}" data-click="browserToggleCommGroup">
        <span class="group-arrow">◀</span>
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
  const dateStr   = (m.date || m.meeting_id).replace(/_/g, '/');
  const committee = (m.committee || '').replace(/_/g, ' ');
  const title     = inGroup || !committee ? dateStr : committee;
  const meta      = inGroup || !committee ? '' : `<span class="sidebar-date">${_esc(dateStr)}</span>`;
  return `<div class="sidebar-meeting ${active ? 'active' : ''} ${inGroup ? 'in-group' : ''}"
               data-meeting-id="${_esc(m.meeting_id)}" data-click="browserSwitchMeeting" title="${_esc(committee)}">
    <div class="sidebar-meeting-title">${_esc(title)}</div>
    ${meta ? `<div class="sidebar-meeting-meta">${meta}</div>` : ''}
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

function browserSetGroup(mode) {
  _groupByComm = mode === 'committee';
  _collapsedGroups.clear();
  _renderSidebar();
}

function browserToggleCommGroup(comm) {
  if (_collapsedGroups.has(comm)) _collapsedGroups.delete(comm);
  else _collapsedGroups.add(comm);
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
  if (btn) btn.textContent = open ? 'סגירה' : 'סיכום';
}

/* ── Load meeting (summary + transcript) ─────────────────────────── */
async function _loadMeeting(meetingId, { pushUrl = true } = {}) {
  _activeId          = meetingId;
  if (_standalone) updateProtocolUrl({ meeting: meetingId }, { push: pushUrl });
  _activeHitsQuery   = null;
  _hmHits            = [];
  _summary           = null;

  const m = _meetings.find(x => x.meeting_id === meetingId);

  // Update sidebar active state
  _renderSidebar();

  const col = _panel.querySelector('#browser-transcript-col');
  col.innerHTML = '<div class="browser-loading">בטעינה…</div>';

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
    _wireTopicHits(col);
    _wireTopicHits(_panel?.querySelector('#browser-summary-bar'));
    _wireSummaryJumps(col);
    _wireSummaryJumps(_panel?.querySelector('#browser-summary-bar'));

    _initHeatmap();

    _wireHeatmapStrip(col);
    col.addEventListener('scroll', () => { _updateHeatmapViewport(); _updateNavCount(); }, { passive: true });

    const focus = _pendingFocus;
    _pendingFocus = null;
    _loadHitsHeatmap(meetingId, _searchQuery(), focus ? null : 'first');
    if (focus) requestAnimationFrame(() => browserFocusSpeech(focus.speech, focus));

  } catch (err) {
    col.innerHTML = `<div class="browser-loading"><div class="browser-error">שגיאה בטעינה: ${_esc(err.message)}</div></div>`;
  }
}

/* ── Summary panel ───────────────────────────────────────────────── */
function _bulletHtml(b, isTopic) {
  // b: {text, quote?, quote_verified?, speech_idx?} or a plain string (legacy)
  const text      = typeof b === 'string' ? b : b.text;
  const speechIdx = typeof b === 'string' ? null : b.speech_idx;
  const quote     = typeof b === 'string' ? '' : (b.quote || '');
  const verified  = typeof b !== 'string' && !!b.quote_verified;
  const rangeAttrs = (typeof b !== 'string' && b.quote_offset != null && b.quote_length)
    ? ` data-quote-offset="${_esc(b.quote_offset)}" data-quote-length="${_esc(b.quote_length)}"` : '';
  const quoteAttrs = speechIdx != null
    ? `data-speech-idx="${_esc(speechIdx)}" data-quote="${_esc(quote)}"${rangeAttrs}`
    : (quote ? `data-approx-quote="${_esc(quote)}"` : '');
  const bulletAttrs = isTopic
    ? `data-hits-query="${_esc(text)}" title="הדגשת הנאומים התואמים לנושא במפת החום"`
    : (speechIdx != null ? `${quoteAttrs} title="מעבר לציטוט בפרוטוקול"`
      : (quote ? `${quoteAttrs} title="חיפוש הקטע הקרוב ביותר בפרוטוקול"` : ''));
  const jump = speechIdx != null
    ? `<button class="summary-jump-btn" ${quoteAttrs} title="מעבר לציטוט בפרוטוקול"><span class="material-symbols-outlined">arrow_outward</span></button>`
    : '';
  const approxSearch = speechIdx == null && quote
    ? `<button class="summary-approx-btn" ${quoteAttrs}><span class="material-symbols-outlined">search</span>חיפוש בפרוטוקול</button>`
    : '';
  let quoteHtml = '';
  if (quote && verified) {
    quoteHtml = `<div class="summary-quote">„${_esc(quote)}”${jump}${approxSearch}</div>`;
  } else if (quote) {
    quoteHtml = `<div class="summary-quote-unverified" title="הנוסח המדויק לא נמצא בפרוטוקול">
      <div class="summary-quote-unverified-label"><span class="material-symbols-outlined">warning</span>ציטוט לא מאומת — ייתכן שאינו מדויק</div>
      <div class="summary-quote-unverified-text">${_esc(quote)}</div>
      ${approxSearch}
    </div>`;
  }
  return `<li>
    <button class="summary-bullet-btn" ${bulletAttrs}>
      <span class="bullet-indicator"></span>
      <span>${renderMarkdownInline(text)}</span>
    </button>${quoteHtml}
  </li>`;
}

const _ATTENDANCE_SECTION_INDEX = 0;
const _TOPICS_SECTION_INDEX     = 1;

function _summarySectionsHtml(topics) {
  return topics.map(t => `<details class="summary-section"${t.index === _ATTENDANCE_SECTION_INDEX ? '' : ' open'}>
       <summary class="summary-heading">
         <span class="summary-section-arrow">▼</span>
         <span>${_esc(t.heading)}</span>
         <span class="summary-section-count">${(t.bullets || []).length}</span>
       </summary>
       <ul class="summary-bullets">${(t.bullets || []).map(b => _bulletHtml(b, t.index === _TOPICS_SECTION_INDEX)).join('')}</ul>
     </details>`).join('');
}

function _summaryHtml(data, m) {
  const topics    = data.topics || [];
  const committee = _esc(String(m?.committee || '').replace(/_/g, ' ').trim());
  const date      = _esc(String(m?.date || '').replace(/_/g, '/'));
  const titleHtml = `<span class="meeting-heading"><span class="meeting-title">${committee || date}</span>${committee && date ? `<span class="meeting-date">${date}</span>` : ''}</span>`;
  if (!topics.length) return `<div class="summary-panel"><div class="summary-toggle">${titleHtml}</div></div>`;

  return `
<details class="summary-panel">
  <summary class="summary-toggle">
    <span class="summary-toggle-label">
      <span class="summary-toggle-arrow">▼</span>
      <span class="material-symbols-outlined summary-toggle-icon">auto_awesome</span>
      <span>סיכום AI</span>
    </span>
    ${titleHtml}
  </summary>
  <div class="summary-body">${_summarySectionsHtml(topics)}</div>
</details>`;
}

function _summaryBodyHtml(data) {
  const topics = data.topics || [];
  if (!topics.length) return '';
  return _summarySectionsHtml(topics);
}

/* Opinion bullet or its jump button → scroll to the quoted speech, then highlight the quote in it */
function _wireSummaryJumps(root) {
  root?.querySelectorAll('.summary-jump-btn[data-speech-idx], .summary-bullet-btn[data-speech-idx]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const d = btn.dataset;
      browserFocusSpeech(d.speechIdx, d.quoteOffset != null
        ? { offset: Number(d.quoteOffset), length: Number(d.quoteLength) }
        : { quote: d.quote || '' });
    });
  });
  // Quote without an exact location → rank the meeting's speeches by the quote's words and go to the best one
  root?.querySelectorAll('.summary-approx-btn[data-approx-quote], .summary-bullet-btn[data-approx-quote]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      _loadHitsHeatmap(_activeId, btn.dataset.approxQuote, 'best');
    });
  });
}

/* Topic click → heatmap from the speeches matching the topic text; click again → back to the search query */
function _wireTopicHits(root) {
  root?.querySelectorAll('.summary-bullet-btn[data-hits-query]').forEach(btn => {
    btn.addEventListener('click', () => {
      const topicText = btn.dataset.hitsQuery;
      _activeHitsQuery = (_activeHitsQuery === topicText) ? null : topicText;
      _panel?.querySelectorAll('.summary-bullet-btn[data-hits-query]').forEach(b => {
        b.classList.toggle('active', b.dataset.hitsQuery === _activeHitsQuery);
      });
      _loadHitsHeatmap(_activeId, _activeHitsQuery ?? _searchQuery());
    });
  });
}

/* ── Transcript ──────────────────────────────────────────────────── */
function _meetingHeaderHtml(headerChunks) {
  if (!headerChunks.length) return '';
  const agenda = headerChunks.find(c => c.speaker.trim() === 'סדר היום') || headerChunks[0];
  const rows = headerChunks.map(c => `
    <div class="chunk-card chunk-card--header" data-chunk-id="${_esc(c.chunk_id)}">
      ${c.speaker ? `<span class="chunk-speaker">${_esc(c.speaker)}</span>` : ''}
      <div class="chunk-text">${_esc(c.text)}</div>
    </div>`).join('');
  return `
<details class="meeting-header">
  <summary class="meeting-header-summary">
    <span class="material-symbols-outlined meeting-header-arrow">chevron_left</span>
    <b>פרטי הישיבה</b>
    <span class="meeting-header-hint">${_esc(agenda.text)}</span>
  </summary>
  <div class="meeting-header-body">${rows}</div>
</details>`;
}

function _transcriptHtml(data) {
  const allChunks = data.chunks || [];
  if (!allChunks.length) return '<div class="browser-empty">אין תמלול זמין</div>';
  const headerCount = data.header_count || 0;
  const chunks = allChunks.slice(headerCount);

  const rows = chunks.map(c => {
    const color    = topicColor(c.topic_index);
    const initials = _initials(c.speaker);
    const photoName = encodeURIComponent(_speakerPhotoKey(c.speaker));
    return `
<div class="chunk-card" data-chunk-id="${_esc(c.chunk_id)}" data-topic-idx="${_esc(c.topic_index ?? '')}">
  <div class="chunk-left">
    <div class="chunk-avatar" style="background:${color}20;color:${color}">
      <span class="chunk-avatar-initials">${_esc(initials)}</span>
      <img class="chunk-avatar-img" src="/mk-photo/${photoName}" alt="" loading="lazy" data-hide-on-error>
    </div>
  </div>
  <div class="chunk-body" style="border-right-color:${color}">
    <div class="chunk-speaker-row">
      <span class="chunk-speaker">${_esc(c.speaker || '—')}</span>
      ${_standalone ? `<button class="chunk-share-btn" data-click="browserShareSpeech" data-share-level="speech" title="קישור לדברים">
        <span class="material-symbols-outlined">share</span>
      </button>` : ''}
    </div>
    <div class="chunk-text">${_esc(c.text)}</div>
  </div>
</div>`;
  }).join('');

  return `<div class="transcript-body" id="transcript-body" data-meeting-id="${_esc(data.meeting_id)}">${_meetingHeaderHtml(allChunks.slice(0, headerCount))}${rows}</div>`;
}

/* ── Heatmap: init, render, viewport indicator ───────────────────── */
/* The strip marks where the query's words sit in the transcript column (by their rendered position),
   stronger for speeches matching more of the words. */

function _initHeatmap() {
  _hmHits = [];
  _renderHeatmap();
  requestAnimationFrame(_updateHeatmapViewport);
  const inner = _panel?.querySelector('#browser-transcript-col .transcript-inner');
  if (inner) new ResizeObserver(() => { _renderHeatmap(); _updateHeatmapViewport(); }).observe(inner);
}

function _searchQuery() {
  return (_searchRequest?.query || '').trim();
}

/* /hits for `query`: highlight its words in the transcript and mark them on the heatmap;
   scrollTo 'best' goes to the best matching speech, 'first' to the first match */
async function _loadHitsHeatmap(meetingId, query, scrollTo = null) {
  const strip = _panel?.querySelector('#heatmap-strip');
  const body  = _panel?.querySelector('#transcript-body');
  if (!query) {
    _unmarkText(body, 'keyword-highlight');
    _hmHits = [];
    _renderHeatmap();
    _updateNavCount();
    return;
  }
  strip?.classList.add('loading');
  try {
    const res  = await fetch(`/api/research/${_sid}/meeting/${encodeURIComponent(meetingId)}/hits?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    if (meetingId !== _activeId) return;
    if (data.error) throw new Error(data.error);
    _hmHits = data.hits || [];
    _unmarkText(body, 'keyword-highlight');
    for (const hit of _hmHits) {
      const card = body?.querySelector(`.chunk-card[data-chunk-id="${CSS.escape(String(hit.speech_idx))}"]`);
      _markTextRanges(card?.querySelector('.chunk-text'), hit.ranges || [], 'keyword-highlight');
    }
    _renderHeatmap();
    _updateNavCount();
    if (scrollTo === 'best') {
      const best = _hmHits.reduce((a, h) => (!a || h.score > a.score) ? h : a, null);
      if (best) browserFocusSpeech(best.speech_idx);
    } else if (scrollTo === 'first') {
      const anchors = _hitAnchors();
      const first = anchors.find(a => !a.closest('.meeting-header')) || anchors[0];
      if (first) _scrollColTo(first);
    }
  } catch (err) {
    console.error('[browser] hits failed:', err);
  } finally {
    strip?.classList.remove('loading');
  }
}

const _HEATMAP_ROW_PX = 3;

function _renderHeatmap() {
  const bandsEl = _panel?.querySelector('#hm-bands');
  const col     = _panel?.querySelector('#browser-transcript-col');
  if (!bandsEl || !col) return;
  const stripHeight = bandsEl.clientHeight;
  if (!_hmHits.length || !col.scrollHeight || !stripHeight) { bandsEl.innerHTML = ''; return; }

  const scale    = stripHeight / col.scrollHeight;
  const colTop   = col.getBoundingClientRect().top - col.scrollTop;
  const strength = hit => (hit.matched_words || 1) / (hit.query_words || 1);
  const strongestByRow = new Map();
  for (const hit of _hmHits) {
    const card = col.querySelector(`.transcript-body .chunk-card[data-chunk-id="${CSS.escape(String(hit.speech_idx))}"]`);
    if (!card) continue;
    const marks = card.querySelectorAll('mark.keyword-highlight');
    const folded = card.closest('details:not([open])');
    for (const el of (folded ? [folded] : (marks.length ? marks : [card]))) {
      const row = Math.floor((el.getBoundingClientRect().top - colTop) * scale / _HEATMAP_ROW_PX);
      const best = strongestByRow.get(row);
      if (!best || strength(hit) > strength(best)) strongestByRow.set(row, hit);
    }
  }
  bandsEl.innerHTML = [...strongestByRow].map(([row, hit]) =>
    `<div class="hm-band" data-speech-idx="${_esc(hit.speech_idx)}" style="top:${row * _HEATMAP_ROW_PX}px; opacity:${0.35 + 0.65 * strength(hit)}"></div>`).join('');
}

/* Hovering the strip names the nearest match (speaker, matched words); clicking goes to it, or scrolls
   proportionally away from the matches. */
const _HEATMAP_HOVER_PX = 5;

function _bandNear(strip, clientY) {
  let nearest = null, nearestDistance = _HEATMAP_HOVER_PX;
  strip.querySelectorAll('.hm-band').forEach(band => {
    const rect = band.getBoundingClientRect();
    const distance = Math.abs(rect.top + rect.height / 2 - clientY);
    if (distance <= nearestDistance) { nearest = band; nearestDistance = distance; }
  });
  return nearest;
}

function _wireHeatmapStrip(col) {
  const strip = _panel.querySelector('#heatmap-strip');
  const tip   = _panel.querySelector('#hm-tip');
  strip.addEventListener('click', e => {
    const band = _bandNear(strip, e.clientY);
    if (band) { browserFocusSpeech(band.dataset.speechIdx); return; }
    const rect = strip.getBoundingClientRect();
    const pct  = (e.clientY - rect.top) / rect.height;
    col.scrollTo({ top: Math.max(0, pct * col.scrollHeight), behavior: 'smooth' });
  });
  strip.addEventListener('mousemove', e => {
    const band = _bandNear(strip, e.clientY);
    const hit  = band && _hmHits.find(h => String(h.speech_idx) === band.dataset.speechIdx);
    if (!hit) { tip.hidden = true; return; }
    const card    = col.querySelector(`.chunk-card[data-chunk-id="${CSS.escape(String(hit.speech_idx))}"]`);
    const speaker = card?.querySelector('.chunk-speaker')?.textContent || '';
    tip.innerHTML = `<b>${_esc(speaker)}</b><span>${_esc(hit.matched_words)} מתוך ${_esc(hit.query_words)} מילות החיפוש</span>`;
    tip.style.top = `${e.clientY - strip.parentElement.getBoundingClientRect().top}px`;
    tip.hidden = false;
  });
  strip.addEventListener('mouseleave', () => { tip.hidden = true; });
}

/* ── Transcript navigation: start / end, previous / next search match ── */
/* Matches are the speeches with highlighted words, anchored at their first highlighted word. */
function _hitAnchors() {
  const body = _panel?.querySelector('#transcript-body');
  if (!body || !_hmHits.length) return [];
  const hitIdx = new Set(_hmHits.map(h => String(h.speech_idx)));
  return [...body.querySelectorAll('.chunk-card')]
    .filter(card => hitIdx.has(card.dataset.chunkId))
    .map(card => card.querySelector('mark.keyword-highlight') || card);
}

function _shownElement(el) {
  return el.closest('details:not([open])')?.querySelector('summary') || el;
}

function _reveal(el) {
  const folded = el.closest('details:not([open])');
  if (folded) folded.open = true;
}

function _anchorCenterInCol(col, anchor) {
  const rect = _shownElement(anchor).getBoundingClientRect();
  return col.scrollTop + rect.top - col.getBoundingClientRect().top + rect.height / 2;
}

function _scrollColTo(anchor) {
  const col = _panel?.querySelector('#browser-transcript-col');
  if (!col) return;
  _reveal(anchor);
  col.scrollTo({ top: Math.max(0, _anchorCenterInCol(col, anchor) - col.clientHeight / 2), behavior: 'smooth' });
}

function browserNav(where) {
  const col = _panel?.querySelector('#browser-transcript-col');
  if (!col) return;
  if (where === 'start') { col.scrollTo({ top: 0, behavior: 'smooth' }); return; }
  if (where === 'end')   { col.scrollTo({ top: col.scrollHeight, behavior: 'smooth' }); return; }
  const anchors    = _hitAnchors();
  const viewCenter = col.scrollTop + col.clientHeight / 2;
  const centers    = anchors.map(a => _anchorCenterInCol(col, a));
  const index = where === 'next'
    ? centers.findIndex(c => c > viewCenter + 2)
    : centers.findLastIndex(c => c < viewCenter - 2);
  if (index >= 0) _scrollColTo(anchors[index]);
}

function _updateNavCount() {
  const hitsEl  = _panel?.querySelector('#proto-nav-hits');
  const countEl = _panel?.querySelector('#proto-nav-count');
  const col     = _panel?.querySelector('#browser-transcript-col');
  if (!hitsEl || !countEl || !col) return;
  const anchors = _hitAnchors();
  hitsEl.hidden = !anchors.length;
  if (!anchors.length) return;
  const viewCenter = col.scrollTop + col.clientHeight / 2;
  const passed = anchors.filter(a => _anchorCenterInCol(col, a) <= viewCenter + 2).length;
  countEl.textContent = `${Math.max(passed, 1)}/${anchors.length}`;
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

/* ── Focus a speech: scroll to it, optionally highlight a range in it ── */
/* range: {offset, length} (a link, a located quote) or {quote} (a citation's text, found the way
   quotes are verified: ignoring niqqud, whitespace and punctuation); without one, any highlight is removed. */
function browserFocusSpeech(speechIdx, range = null) {
  const col  = _panel?.querySelector('#browser-transcript-col');
  const card = col?.querySelector(`.chunk-card[data-chunk-id="${CSS.escape(String(speechIdx))}"]`);
  if (!col || !card) return;
  _reveal(card);

  const textRange = range?.quote ? _quoteRangeInCard(card, range.quote)
    : (range?.offset != null && range?.length ? range : null);
  const marks = _highlightInCard(card, textRange);
  const mark  = marks[0];
  if (_standalone) {
    updateProtocolUrl({ speech: Number(speechIdx), offset: mark ? textRange.offset : null,
                        length: mark ? textRange.length : null });
  }

  const anchor = mark || card;
  const targetTop = () => Math.max(0, _anchorCenterInCol(col, anchor) - col.clientHeight / 2);
  const target = targetTop();
  const alreadyThere = Math.abs(col.scrollTop - target) < 4;
  /* The layout can still reflow during a long scroll (the filter bar folds, the scrollbar appears), so
     land on the anchor again once it settles. */
  _afterScrollSettles(col, alreadyThere, () => {
    if (Math.abs(col.scrollTop - targetTop()) > 8) col.scrollTo({ top: targetTop(), behavior: 'instant' });
    if (mark) marks.forEach(m => m.classList.add('sweep'));
  });
  col.scrollTo({ top: target, behavior: 'smooth' });
}

/* ── Quote highlight ─────────────────────────────────────────────── */

const _QUOTE_IGNORED_CHARS = /[֑-ׇ\s"'“”„״׳.,:;!?()\[\]\-–—…]/;

function _unmarkText(root, className) {
  root?.querySelectorAll(`mark.${className}`).forEach(mark => {
    const parent = mark.parentNode;
    mark.replaceWith(...mark.childNodes);
    parent.normalize();
  });
}

/* Wrap each [offset, length] of textEl's text (sorted, not overlapping) in <mark class=className>; a range
   crossing other marks is wrapped piece by piece. Returns the new marks in text order. */
function _markTextRanges(textEl, ranges, className) {
  const marks = [];
  if (!textEl || !ranges.length) return marks;
  const walker = document.createTreeWalker(textEl, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  let nodeStart = 0;
  for (const node of textNodes) {
    const nodeEnd = nodeStart + node.length;
    const pieces = ranges.map(([offset, length]) => [Math.max(offset, nodeStart), Math.min(offset + length, nodeEnd)])
                         .filter(([start, end]) => start < end);
    const nodeMarks = [];
    for (const [start, end] of pieces.reverse()) {
      const domRange = document.createRange();
      domRange.setStart(node, start - nodeStart);
      domRange.setEnd(node, end - nodeStart);
      const mark = document.createElement('mark');
      mark.className = className;
      domRange.surroundContents(mark);
      nodeMarks.unshift(mark);
    }
    marks.push(...nodeMarks);
    nodeStart = nodeEnd;
  }
  return marks;
}

/* {offset, length} of the quote in the card's text, or null */
function _quoteRangeInCard(card, quote) {
  const raw = card.querySelector('.chunk-text')?.textContent || '';
  let normalized = '';
  const rawIndexOf = [];
  for (let i = 0; i < raw.length; i++) {
    if (_QUOTE_IGNORED_CHARS.test(raw[i])) continue;
    normalized += raw[i];
    rawIndexOf.push(i);
  }
  const normalizedQuote = [...quote].filter(ch => !_QUOTE_IGNORED_CHARS.test(ch)).join('');
  const start = normalizedQuote ? normalized.indexOf(normalizedQuote) : -1;
  if (start < 0) return null;
  const offset = rawIndexOf[start];
  return { offset, length: rawIndexOf[start + normalizedQuote.length - 1] + 1 - offset };
}

/* Highlight range ({offset, length} in the card's text) as the quote, after removing the previous quote
   highlight; [] when there is no range or it falls outside the text. */
function _highlightInCard(card, range) {
  _unmarkText(card.closest('.transcript-body'), 'quote-highlight');
  const textEl = card.querySelector('.chunk-text');
  if (!range || !textEl) return [];
  if (range.offset < 0 || range.length <= 0 || range.offset + range.length > textEl.textContent.length) return [];
  return _markTextRanges(textEl, [[range.offset, range.length]], 'quote-highlight');
}

/* ── Sharing: a speech card click or a text selection inside one speech goes into the URL ── */

function _wireTranscriptSharing() {
  if (!_standalone) return;
  const col = _panel.querySelector('#browser-transcript-col');
  col.addEventListener('click', e => {
    const card = e.target.closest('.transcript-body .chunk-card');
    if (!card || e.target.closest('button') || !document.getSelection().isCollapsed) return;
    updateProtocolUrl({ speech: Number(card.dataset.chunkId), offset: null, length: null });
  });
  const onSelectionEnd = () => setTimeout(_onTranscriptSelection, 0);
  col.addEventListener('mouseup', onSelectionEnd);
  col.addEventListener('touchend', onSelectionEnd);
  col.addEventListener('keyup', onSelectionEnd);
  document.addEventListener('selectionchange', _onSelectionHandleDrag);
  col.addEventListener('scroll', _hideSelectionShareButton, { passive: true });
}

/* A long-press selection and dragging its touch handles fire no touchend on the column:
   follow the selection once it settles */
let _selectionSettleTimer = null;
function _onSelectionHandleDrag() {
  const buttonShown = _selectionShareButton && _selectionShareButton.style.display !== 'none';
  const selection = document.getSelection();
  const inTranscript = selection?.anchorNode && _panel?.querySelector('#browser-transcript-col')?.contains(selection.anchorNode);
  if (!buttonShown && !inTranscript) return;
  clearTimeout(_selectionSettleTimer);
  _selectionSettleTimer = setTimeout(_onTranscriptSelection, 250);
}

/* Selection within one speech's text → its range in the URL and a floating share button */
function _onTranscriptSelection() {
  const selection = document.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) { _hideSelectionShareButton(); return; }
  const range = selection.getRangeAt(0);
  const common = range.commonAncestorContainer;
  const textEl = (common.nodeType === Node.ELEMENT_NODE ? common : common.parentElement)?.closest('.chunk-text');
  const card = textEl?.closest('.transcript-body .chunk-card');
  if (!card || !range.toString().trim()) { _hideSelectionShareButton(); return; }

  const beforeSelection = document.createRange();
  beforeSelection.setStart(textEl, 0);
  beforeSelection.setEnd(range.startContainer, range.startOffset);
  updateProtocolUrl({ speech: Number(card.dataset.chunkId), offset: beforeSelection.toString().length,
                      length: range.toString().length });
  _showSelectionShareButton(range.getBoundingClientRect());
}

let _selectionShareButton = null;

function _showSelectionShareButton(selectionRect) {
  if (!_selectionShareButton) {
    _selectionShareButton = document.createElement('button');
    _selectionShareButton.className = 'selection-share-btn';
    _selectionShareButton.dataset.click = 'browserShareQuote';
    _selectionShareButton.dataset.shareLevel = 'quote';
    _selectionShareButton.innerHTML = '<span class="material-symbols-outlined">share</span><span>לשיתוף</span>';
    _selectionShareButton.addEventListener('mousedown', e => e.preventDefault());
    document.body.appendChild(_selectionShareButton);
  }
  const button = _selectionShareButton;
  button.style.display = 'flex';
  const gapClearingSelectionHandles = matchMedia('(pointer: coarse)').matches ? 32 : 8;
  const below = selectionRect.bottom + gapClearingSelectionHandles;
  const fitsBelow = below + button.offsetHeight + 8 <= window.innerHeight;
  button.style.top  = `${fitsBelow ? below : Math.max(8, selectionRect.top - button.offsetHeight - 8)}px`;
  button.style.left = `${Math.max(8, Math.min(selectionRect.left + selectionRect.width / 2 - button.offsetWidth / 2,
                                              window.innerWidth - button.offsetWidth - 8))}px`;
}

function _hideSelectionShareButton() {
  if (_selectionShareButton) _selectionShareButton.style.display = 'none';
}

function browserShareMeeting(button) {
  openShareMenu(button, 'meeting');
}

function browserShareSpeech(button) {
  const card = button.closest('.chunk-card');
  if (!card) return;
  updateProtocolUrl({ speech: Number(card.dataset.chunkId) });
  openShareMenu(button, 'speech');
}

function browserShareQuote(button) {
  openShareMenu(button, 'quote');
  _hideSelectionShareButton();
}

function _afterScrollSettles(col, alreadyThere, callback) {
  if (alreadyThere) { requestAnimationFrame(callback); return; }
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    col.removeEventListener('scrollend', finish);
    callback();
  };
  col.addEventListener('scrollend', finish, { once: true });
  setTimeout(finish, 900);
}

/* ── Deep-link from the agent answer → open protocol in reading tab ── */
function openProtocolFromCitationButton(btn) {
  const d = btn.dataset;
  openProtocolFromCitation(d.sid, d.meetingId, d.speechIdx || null, d.quote || '');
}

function openProtocolFromCitation(sid, meetingId, speechIdx, quote = '') {
  if (!meetingId) return;
  // Seed the viewer sidebar with the answer's own cited meetings.
  const seed = (window.__citedMeetings && window.__citedMeetings[sid]) || [];
  const meetings = (seed.length && seed.some(m => String(m.meeting_id) === String(meetingId)))
    ? seed
    : [{ meeting_id: String(meetingId) }];

  switchTab('reading', { writeUrl: false });
  setProtocolUrlState({ ...readProtocolUrl(''), meeting: null });

  const area = document.getElementById('reading-browser-area');
  if (area) area.innerHTML = '';

  openProtocolBrowser(sid, String(meetingId), meetings, {
    container:      area || undefined,
    standalone:     true,
    postCompletion: true,
    focus:          (speechIdx != null && speechIdx !== '') ? { speech: String(speechIdx), quote } : null,
  });
}

/* ── Sidebar switch meeting ──────────────────────────────────────── */
function browserSwitchMeeting(meetingId, { focus = null, pushUrl = true } = {}) {
  if (meetingId === _activeId) return;
  _pendingFocus = focus?.speech != null ? focus : null;
  _loadMeeting(meetingId, { pushUrl });
}

function browserShowsMeeting(meetingId) {
  return !!_panel && _standalone && _activeId === meetingId;
}

function browserListsMeeting(meetingId) {
  return !!_panel && _standalone && _meetings.some(m => m.meeting_id === meetingId);
}

/* ── Load more meetings (same search, larger top_k) ─────────────── */
async function browserLoadMore() {
  const btn = _panel.querySelector('.sidebar-load-more');
  if (!_searchRequest || !btn) return;
  btn.textContent = 'בטעינה…';
  try {
    const requested = _meetings.length + 40;
    const data = await _browseSearch({ ..._searchRequest, top_k: requested });
    const existingIds = new Set(_meetings.map(m => m.meeting_id));
    const newOnes = (data.meetings || []).filter(m => !existingIds.has(m.meeting_id));
    _meetings = [..._meetings, ...newOnes];
    _renderSidebar();
    if ((data.meetings || []).length < requested) btn.style.display = 'none';
    else btn.textContent = 'טעינת ישיבות נוספות';
  } catch (err) {
    console.error('[browser] load more failed:', err);
    btn.textContent = 'שגיאה — ניסיון נוסף';
  }
}

async function _browseSearch(searchRequest) {
  const res  = await fetch('/api/browse/search', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(searchRequest),
  });
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}

/* Run the search, then open the browser on its results (used by the deep-dive panel) */
async function openProtocolBrowserWithSearch(sessionId, searchRequest, opts = {}) {
  try {
    const data = await _browseSearch(searchRequest);
    const meetings = data.meetings || [];
    openProtocolBrowser(sessionId, meetings[0]?.meeting_id || null, meetings, { ...opts, searchRequest });
  } catch (err) {
    console.error('[browser] search failed:', err);
    openProtocolBrowser(sessionId, null, [], { ...opts, searchRequest });
  }
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
  const q = _origQ || 'תמצות הממצאים העיקריים מהפרוטוקולים שנמצאו';
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
    if (!(await window.requireGeminiKey())) throw new Error('נדרש מפתח Gemini כדי לשאול על הפרוטוקול');
    const res = await fetch(`/api/research/${_sid}/workspace/ask`, {
      method:  'POST',
      headers: {'Content-Type': 'application/json', ...window.geminiKeyHeaders()},
      body:    JSON.stringify({ question, meeting_id: meetingId, model: window.meetingChatModel() }),
    });
    const rejection = await window.agentResponseError(res);
    if (rejection) throw rejection;

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
            prose.innerHTML = renderMarkdown(raw);
          }
        }
      }
    }
  } catch (err) {
    prose.innerHTML = `<span style="color:#b02500">שגיאה: ${_esc(err.message)}</span>`;
  }
  if (raw) prose.innerHTML = renderMarkdown(raw);
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

// Strip honorific prefixes so "ח\"כ נעמה לזימי" → "נעמה לזימי" for photo lookup
function _speakerPhotoKey(name) {
  if (!name) return '';
  return name.trim()
    .replace(/^(ח"כ|ח'כ|השר|השרה|שר|שרה|יו"ר|מנכ"ל|ד"ר|פרופ'?)\s+/u, '')
    .trim();
}

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
