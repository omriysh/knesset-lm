/**
 * tabs.js — Tab switching + reading tab browse search
 *
 * Depends on browser.js (openProtocolBrowser) being loaded first.
 */

/* ── Tab switching ───────────────────────────────────────────────── */
/* writeUrl: false when the URL already names this tab (opening a link, back/forward) */
function switchTab(name, { writeUrl = true, push = true } = {}) {
  // Hide all panels
  document.querySelectorAll('.app-tab-panel').forEach(p => {
    p.classList.add('hidden');
  });

  // Deactivate desktop tab buttons
  document.querySelectorAll('.app-tab-btn').forEach(b => {
    b.classList.remove('active');
  });

  // Deactivate mobile tab buttons
  document.querySelectorAll('.mobile-tab-btn').forEach(b => {
    b.classList.remove('active');
  });

  // Show the target panel
  const panel = document.getElementById(`tab-${name}`);
  if (panel) panel.classList.remove('hidden');

  // Activate desktop button
  const dtBtn = document.getElementById(`dt-tab-${name}`);
  if (dtBtn) dtBtn.classList.add('active');

  // Activate mobile button
  const mobBtn = document.getElementById(`mob-tab-${name}`);
  if (mobBtn) mobBtn.classList.add('active');

  if (name === 'reading' && writeUrl && !_readingTabHasResults()) browseSearch({ push: false });
  if (name === 'profiles') profilesShow();
  if (name === 'game') gameShow();
  if (name === 'research') setResearchTabShown(true);

  const researchSettings = document.getElementById('settings-research');
  if (researchSettings) researchSettings.disabled = name !== 'research';

  setActiveTab(name, { writeUrl, push });
}

/* ── "בצ'אט שלנו" tab: hidden until turned on in the settings or opened by a link ── */
const _RESEARCH_TAB_SHOWN_KEY = 'showResearchTab';

function researchTabShown() {
  try {
    return localStorage.getItem(_RESEARCH_TAB_SHOWN_KEY) === 'true';
  } catch (exc) {
    console.warn('[tabs] localStorage unavailable:', exc);
    return false;
  }
}

function setResearchTabShown(shown) {
  document.documentElement.classList.toggle('research-tab-shown', shown);
  try {
    localStorage.setItem(_RESEARCH_TAB_SHOWN_KEY, shown ? 'true' : 'false');
  } catch (exc) {
    console.warn('[tabs] localStorage unavailable:', exc);
  }
  if (!shown && _activeTab === 'research') switchTab('home');
}

document.documentElement.classList.toggle('research-tab-shown', researchTabShown());

/* ── URL routing: / (home), /chat, /research, /protocols?…, /profiles… (url_state.js, profiles.js);
   back/forward re-applies the URL ── */
function applyUrlRoute() {
  if (location.pathname === RESEARCH_PATH) { switchTab('research', { writeUrl: false }); return; }
  if (location.pathname === CHAT_PATH) { switchTab('chat', { writeUrl: false }); return; }
  if (location.pathname === GAME_PATH) { switchTab('game', { writeUrl: false }); return; }
  if (location.pathname === PROFILES_PATH || location.pathname.startsWith(PROFILES_PATH + '/')) {
    switchTab('profiles', { writeUrl: false });
    profilesApplyRoute();
    return;
  }
  if (location.pathname !== PROTOCOLS_PATH) { switchTab('home', { writeUrl: false }); return; }
  switchTab('reading', { writeUrl: false });
  const target = readProtocolUrl();
  setProtocolUrlState(target);
  const input = document.getElementById('reading-search-input');
  if (input) input.value = target.query;
  rfSetFilters(target.filters);
  if (!target.meeting) {
    browseSearch({ push: false });
    return;
  }
  const focus = { speech: target.speech, offset: target.offset, length: target.length };
  if (browserShowsMeeting(target.meeting)) browserFocusSpeech(target.speech, focus);
  else if (browserListsMeeting(target.meeting)) browserSwitchMeeting(target.meeting, { focus, pushUrl: false });
  else _openMeetingFromUrl(target, focus);
}

/* With a search in the link, the sidebar lists its results; the linked meeting is added on top when the
   search does not return it. */
async function _openMeetingFromUrl(target, focus) {
  _setBrowseLoading(true);
  try {
    const withSearch = hasProtocolSearch(target);
    const [linked, searched] = await Promise.all([
      _browseSearch({ query: '', filters: { ...emptyProtocolFilters(), meeting_ids: [target.meeting] } }),
      withSearch ? _browseSearch({ query: target.query, filters: target.filters }) : null,
    ]);
    const area = document.getElementById('reading-browser-area');
    area.innerHTML = '';
    if (!linked.meetings || !linked.meetings.length) {
      _showBrowsePlaceholder('הישיבה לא נמצאה', 'ייתכן שהקישור שגוי או שהישיבה אינה זמינה.', 'link_off');
      return;
    }
    const searchedMeetings = searched?.meetings || [];
    const meetings = searchedMeetings.some(m => String(m.meeting_id) === String(target.meeting))
      ? searchedMeetings : [...linked.meetings, ...searchedMeetings];
    const data = { session_id: (searched || linked).session_id, meetings };
    const label = target.query || 'ישיבה מקישור';
    openProtocolBrowser(data.session_id, target.meeting, data.meetings, {
      originalQuestion: label,
      container:        area,
      standalone:       true,
      postCompletion:   true,
      searchRequest:    withSearch ? { query: target.query, filters: target.filters } : null,
      focus,
      pushUrl:          false,
    });
    _collapseRfb();
  } catch (err) {
    console.error('[tabs] opening the linked meeting failed:', err);
    _showBrowseError('שגיאה בפתיחת הקישור: ' + err.message);
  } finally {
    _setBrowseLoading(false);
  }
}

window.addEventListener('popstate', applyUrlRoute);
document.addEventListener('DOMContentLoaded', applyUrlRoute);

/* ── Browse search (keyword; empty = newest meetings) ────────────── */
function _readingTabHasResults() {
  return !!document.querySelector('#reading-browser-area .browser-standalone-wrapper, #browse-loading-overlay');
}

/* The filters stay open over the first results, and fold away over later ones. */
let _browseSearchedBefore = false;

/* push: false when the search only loads what the current URL already says (a link, the first visit). */
async function browseSearch({ push = true } = {}) {
  const input = document.getElementById('reading-search-input');
  const btn   = document.getElementById('reading-search-btn');
  if (!input || !btn) return;

  const query   = input.value.trim();
  const filters = rfGetFilters();
  const searchRequest = { query, filters };
  updateProtocolUrl({ query, filters, meeting: null }, { push });

  _setBrowseLoading(true);
  try {
    const res = await fetch('/api/browse/search', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(searchRequest),
    });
    const data = await res.json();

    if (data.error) {
      _showBrowseError(data.error);
      return;
    }

    if (!data.meetings || !data.meetings.length) {
      _showBrowsePlaceholder(
        'לא נמצאו ישיבות',
        'אפשר לנסות מילות מפתח אחרות או לשנות את הסינון.',
        'search_off',
      );
      return;
    }

    const area = document.getElementById('reading-browser-area');
    area.innerHTML = '';

    openProtocolBrowser(
      data.session_id,
      data.meetings[0].meeting_id,
      data.meetings,
      {
        originalQuestion: query || 'ישיבות אחרונות',
        container:        area,
        standalone:       true,
        postCompletion:   true,
        searchRequest,
        pushUrl:          false,
        sortMode:         query ? 'relevance' : 'date_desc',
      }
    );

    if (_browseSearchedBefore) _collapseRfb();
    else document.querySelector('.rfb')?.classList.add('rfb-has-results');
    _browseSearchedBefore = true;

  } catch (err) {
    console.error('[tabs] browse search failed:', err);
    _showBrowseError('שגיאת רשת: ' + err.message);
  } finally {
    _setBrowseLoading(false);
  }
}

/* ── Filter bar collapse ─────────────────────────────────────────── */
/* The collapsed bar shows the search as chips; removing one searches again without it. */
function rfbExpand() {
  document.querySelector('.rfb')?.classList.remove('rfb-collapsed');
  _scrolledSinceExpand = 0;
}

function rfbCollapse() {
  _collapseRfb();
}

function _collapseRfb() {
  document.querySelector('.rfb')?.classList.add('rfb-collapsed', 'rfb-has-results');
  const chips = document.getElementById('rfb-collapsed-chips');
  if (!chips) return;
  const query = document.getElementById('reading-search-input')?.value.trim() || '';
  const queryChip = query
    ? rfChipHtml(`"${query}"`, 'rfClearQueryAndSearch', { extraClass: 'chip--query' })
    : '';
  chips.innerHTML = queryChip + rfActiveChipsHtml('rfRemoveFilterAndSearch');
}

function rfClearQueryAndSearch() {
  const input = document.getElementById('reading-search-input');
  if (input) input.value = '';
  browseSearch();
}

/* Reading the results folds an open filter bar away (after a short scroll, so a small nudge doesn't). */
const _AUTO_COLLAPSE_SCROLL_PX = 160;
let _scrolledSinceExpand = 0;
const _lastScrollTop = new WeakMap();
document.addEventListener('scroll', (event) => {
  const scroller = event.target;
  if (!(scroller instanceof Element) || !scroller.closest('#reading-browser-area')) return;
  const previous = _lastScrollTop.get(scroller) ?? scroller.scrollTop;
  _lastScrollTop.set(scroller, scroller.scrollTop);
  const rfb = document.querySelector('.rfb');
  if (!rfb?.classList.contains('rfb-has-results')) return;
  if (rfb.classList.contains('rfb-collapsed')) {
    if (_scrolledUpToTranscriptTop(scroller, previous)) rfbExpand();
    return;
  }
  if (document.querySelector('.rfb-dropdown:not(.hidden)')) return;
  _scrolledSinceExpand += Math.abs(scroller.scrollTop - previous);
  if (_scrolledSinceExpand > _AUTO_COLLAPSE_SCROLL_PX) _collapseRfb();
}, true);

/* Reaching the top of the transcript by scrolling (not by a jump, like opening another meeting) unfolds the filters. */
const _MAX_SCROLL_STEP_PX = 400;
function _scrolledUpToTranscriptTop(scroller, previousScrollTop) {
  return scroller.id === 'browser-transcript-col' && scroller.scrollTop <= 0
    && previousScrollTop > 0 && previousScrollTop < _MAX_SCROLL_STEP_PX;
}

/* ── Helpers ─────────────────────────────────────────────────────── */
function _setBrowseLoading(on) {
  const btn  = document.getElementById('reading-search-btn');
  const area = document.getElementById('reading-browser-area');
  if (btn) {
    btn.disabled = on;
    const label = btn.querySelector('span:not(.material-symbols-outlined)');
    if (label) label.textContent = on ? 'בחיפוש…' : 'חיפוש';
  }
  if (!area) return;
  const existing = document.getElementById('browse-loading-overlay');
  if (on && !existing) {
    const overlay = document.createElement('div');
    overlay.id        = 'browse-loading-overlay';
    overlay.className = 'browse-loading-overlay';
    overlay.innerHTML = `
      <div class="browse-spinner"></div>
      <div class="browse-loading-text">חיפוש פרוטוקולים…</div>`;
    area.appendChild(overlay);
  } else if (!on && existing) {
    existing.remove();
  }
}

function _showBrowseError(msg) {
  const area = document.getElementById('reading-browser-area');
  if (!area) return;
  // Keep placeholder DOM but show error banner inside
  let banner = document.getElementById('browse-error-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'browse-error-banner';
    banner.className = 'browse-error-banner';
    area.prepend(banner);
  }
  banner.textContent = msg;
  setTimeout(() => banner.remove(), 5000);
}

function _showBrowsePlaceholder(title, subtitle, icon) {
  const area = document.getElementById('reading-browser-area');
  if (!area) return;
  area.innerHTML = `
<div class="flex flex-col items-center justify-center h-full gap-4 text-center px-6">
  <div class="w-16 h-16 rounded-full bg-surface-container-high flex items-center justify-center">
    <span class="material-symbols-outlined text-on-surface-variant" style="font-size:32px">${icon}</span>
  </div>
  <div>
    <div class="text-lg font-bold text-on-surface mb-1">${title}</div>
    <div class="text-sm text-on-surface-variant max-w-xs leading-relaxed">${subtitle}</div>
  </div>
</div>`;
}
