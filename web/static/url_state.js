/**
 * url_state.js — the page URL as shareable state.
 *
 *   /research                      → research tab
 *   /protocols?q=&committee=&mk=&party=&guest=&from=&to=&meeting=&speech=&offset=&length=
 *                                  → reading tab: the search and filters, the open meeting, a speech in it,
 *                                    and a character range in that speech (src/utils/source_links.py
 *                                    builds the same links for the API)
 *   /chat                          → in-your-chat tab (connect the MCP)
 *   /                              → the home tab, URL left as is until the user moves
 *
 * The reading tab's state lives in _protocolUrlState; browser.js / tabs.js update it through
 * updateProtocolUrl, and the address bar always shows it, so copying the address shares the view.
 *
 * Non-module script, loaded before browser.js / tabs.js / filters.js.
 */

const PROTOCOLS_PATH = '/protocols';
const RESEARCH_PATH  = '/research';
const PROFILES_PATH  = '/profiles';
const CHAT_PATH      = '/chat';
const HOME_PATH      = '/';

const _LIST_FILTER_PARAMS = { committee: 'committees', mk: 'mks', party: 'parties' };
const _MEETING_ID_RE  = /^p?\d{1,12}$/;
const _SMALL_INT_RE   = /^\d{1,7}$/;
const _ISO_DATE_RE    = /^\d{4}-\d{2}-\d{2}$/;
const _SHARE_INCLUDES_SEARCH_KEY = 'shareIncludesSearch';

function emptyProtocolFilters() {
  return { committees: [], mks: [], parties: [], guest: null, date_from: null, date_to: null };
}

let _protocolUrlState = {
  query: '', filters: emptyProtocolFilters(),
  meeting: null, speech: null, offset: null, length: null,
};

/* URL query string → reading tab state; invalid values are dropped, and a range needs its speech. */
function readProtocolUrl(search = location.search) {
  const params = new URLSearchParams(search);
  const filters = emptyProtocolFilters();
  for (const [param, key] of Object.entries(_LIST_FILTER_PARAMS)) {
    filters[key] = params.getAll(param).map(v => v.trim()).filter(Boolean);
  }
  filters.guest     = (params.get('guest') || '').trim() || null;
  filters.date_from = _ISO_DATE_RE.test(params.get('from') || '') ? params.get('from') : null;
  filters.date_to   = _ISO_DATE_RE.test(params.get('to') || '')   ? params.get('to')   : null;

  const meeting = _MEETING_ID_RE.test(params.get('meeting') || '') ? params.get('meeting') : null;
  const speech  = meeting && _SMALL_INT_RE.test(params.get('speech') || '') ? Number(params.get('speech')) : null;
  const hasRange = speech !== null
    && _SMALL_INT_RE.test(params.get('offset') || '') && _SMALL_INT_RE.test(params.get('length') || '')
    && Number(params.get('length')) > 0;
  return {
    query:   (params.get('q') || '').trim(),
    filters,
    meeting,
    speech,
    offset:  hasRange ? Number(params.get('offset')) : null,
    length:  hasRange ? Number(params.get('length')) : null,
  };
}

function hasProtocolSearch(state) {
  const f = state.filters;
  return !!(state.query || f.committees.length || f.mks.length || f.parties.length
            || f.guest || f.date_from || f.date_to);
}

function protocolPath(state, includeSearch = true) {
  const params = new URLSearchParams();
  if (includeSearch) {
    if (state.query) params.set('q', state.query);
    for (const [param, key] of Object.entries(_LIST_FILTER_PARAMS)) {
      (state.filters[key] || []).forEach(v => params.append(param, v));
    }
    if (state.filters.guest)     params.set('guest', state.filters.guest);
    if (state.filters.date_from) params.set('from', state.filters.date_from);
    if (state.filters.date_to)   params.set('to', state.filters.date_to);
  }
  if (state.meeting) {
    params.set('meeting', state.meeting);
    if (state.speech !== null) {
      params.set('speech', state.speech);
      if (state.offset !== null && state.length) {
        params.set('offset', state.offset);
        params.set('length', state.length);
      }
    }
  }
  const query = params.toString();
  return PROTOCOLS_PATH + (query ? `?${query}` : '');
}

function _writeUrl(path, push) {
  if (location.pathname + location.search === path) return;
  if (push) history.pushState(null, '', path);
  else      history.replaceState(null, '', path);
}

/* Merge a change into the reading tab state and show it in the address bar (push = a new history entry).
   A new meeting clears the speech, and a new speech clears the range, unless the change sets them. */
function updateProtocolUrl(change, { push = false } = {}) {
  const next = { ..._protocolUrlState, ...change };
  if ('meeting' in change && change.meeting !== _protocolUrlState.meeting && !('speech' in change)) {
    next.speech = null;
  }
  if ('speech' in change && change.speech !== _protocolUrlState.speech && !('offset' in change)) {
    next.offset = null;
    next.length = null;
  }
  if (next.speech === null) { next.offset = null; next.length = null; }
  _protocolUrlState = next;
  if (_activeTab === 'reading') _writeUrl(protocolPath(next), push);
}

/* Replace the whole reading tab state (a URL being opened) without touching the address bar. */
function setProtocolUrlState(state) {
  _protocolUrlState = state;
}

let _activeTab = null;

function setActiveTab(name, { writeUrl, push }) {
  _activeTab = name;
  if (!writeUrl) return;
  if (name === 'home') _writeUrl(HOME_PATH, push);
  else if (name === 'chat') _writeUrl(CHAT_PATH, push);
  else if (name === 'profiles') _writeUrl(profilesCurrentPath(), push);
  else _writeUrl(name === 'research' ? RESEARCH_PATH : protocolPath(_protocolUrlState), push);
}

/* ── Sharing ─────────────────────────────────────────────────────── */

function shareIncludesSearch() {
  try {
    return localStorage.getItem(_SHARE_INCLUDES_SEARCH_KEY) !== '0';
  } catch (exc) {
    console.warn('[url_state] localStorage unavailable:', exc);
    return true;
  }
}

function setShareIncludesSearch(include) {
  try {
    localStorage.setItem(_SHARE_INCLUDES_SEARCH_KEY, include ? '1' : '0');
  } catch (exc) {
    console.warn('[url_state] localStorage unavailable:', exc);
  }
}

/* level: 'meeting' | 'speech' | 'quote' — how much of the current view the link points at */
function protocolShareUrl(level, includeSearch = shareIncludesSearch()) {
  const s = _protocolUrlState;
  const state = {
    ...s,
    speech: level === 'meeting' ? null : s.speech,
    offset: level === 'quote' ? s.offset : null,
    length: level === 'quote' ? s.length : null,
  };
  return location.origin + protocolPath(state, includeSearch);
}

const _SHARE_TITLES = { meeting: 'קישור לישיבה', speech: 'קישור לדברים', quote: 'קישור לציטוט' };

let _shareMenu = null;

function closeShareMenu() {
  _shareMenu?.remove();
  _shareMenu = null;
}

/* Small popover next to the anchor: copy / native share, with an "include search and filters" toggle. */
function openShareMenu(anchor, level) {
  closeShareMenu();
  const searchToggle = hasProtocolSearch(_protocolUrlState) ? `
    <label class="share-menu-option">
      <input type="checkbox" class="share-include-search" ${shareIncludesSearch() ? 'checked' : ''}>
      <span>כולל חיפוש וסינון</span>
    </label>` : '';
  const nativeShare = navigator.share ? `
    <button class="share-menu-btn share-native-btn">
      <span class="material-symbols-outlined">ios_share</span><span>שיתוף…</span>
    </button>` : '';
  _shareMenu = document.createElement('div');
  _shareMenu.className = 'share-menu';
  _shareMenu.innerHTML = `
    <div class="share-menu-title">${_SHARE_TITLES[level] || 'קישור'}</div>
    ${searchToggle}
    <button class="share-menu-btn share-copy-btn">
      <span class="material-symbols-outlined">link</span><span>העתקת קישור</span>
    </button>
    ${nativeShare}`;
  document.body.appendChild(_shareMenu);

  const rect = anchor.getBoundingClientRect();
  const menuWidth = _shareMenu.offsetWidth;
  _shareMenu.style.top  = `${Math.min(rect.bottom + 6, window.innerHeight - _shareMenu.offsetHeight - 8)}px`;
  _shareMenu.style.left = `${Math.max(8, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 8))}px`;

  const includeSearch = () => {
    const box = _shareMenu?.querySelector('.share-include-search');
    return box ? box.checked : false;
  };
  _shareMenu.querySelector('.share-include-search')?.addEventListener('change', e => {
    setShareIncludesSearch(e.target.checked);
  });
  _shareMenu.querySelector('.share-copy-btn').addEventListener('click', async () => {
    const url = protocolShareUrl(level, includeSearch());
    closeShareMenu();
    try {
      await navigator.clipboard.writeText(url);
      showToast('הקישור הועתק');
    } catch (exc) {
      console.error('[url_state] clipboard write failed:', exc);
      window.prompt('העתקת הקישור:', url);
    }
  });
  _shareMenu.querySelector('.share-native-btn')?.addEventListener('click', async () => {
    const url = protocolShareUrl(level, includeSearch());
    closeShareMenu();
    try {
      await navigator.share({ url, title: document.title });
    } catch (exc) {
      if (exc.name !== 'AbortError') console.error('[url_state] native share failed:', exc);
    }
  });
}

document.addEventListener('mousedown', e => {
  if (_shareMenu && !e.target.closest('.share-menu') && !e.target.closest('[data-share-level]')) closeShareMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeShareMenu(); });

let _toastTimer = null;

function showToast(message) {
  let toast = document.getElementById('app-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'app-toast';
    toast.className = 'app-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add('visible');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove('visible'), 2200);
}
