/**
 * profiles.js — the profiles tab (/profiles…): the 26th Knesset party lists, a party's candidates and a
 * candidate's profile (web/profiles.py). The route is the path, plus the profile's own tab as a bare #token.
 *
 * Non-module script, loaded after url_state.js and browser.js (_esc) and before tabs.js / actions.js, which call
 * profilesApplyRoute / profilesShow / profilesCurrentPath and the profiles* PAGE_ACTIONS.
 */

const PROFILE_TABS = {
  roles:  ['תפקידים', 'badge'],
  themes: ['נושאים ועמדות', 'insights'],
  votes:  ['הצבעות', 'how_to_vote'],
  bills:  ['הצעות חוק', 'gavel'],
};
const PROFILE_TABS_BY_DEPTH = { full: ['themes', 'votes', 'bills', 'roles'], bills: ['votes', 'bills', 'roles'] };
const THEME_COLORS = ['#7c3aed', '#1d4ed8', '#c2410c', '#0f766e', '#a16207', '#be185d', '#005f99', '#4d7c0f',
                      '#9333ea', '#b45309', '#0e7490', '#991b1b', '#475569', '#db2777', '#15803d', '#6d28d9'];
const THEMES_SHOWN = 6;
const THEME_PILLS_SHOWN = 8;
const OTHER_THEME = 'other';
const OTHER_THEME_LABEL = 'נושאים נוספים';
const BILL_STAGES = ['הונחה', 'טרומית', 'ראשונה', 'ועדה', 'שלישית'];
const BILL_STAGES_DONE = { tabled: 1, preliminary: 2, first: 4, passed: 5 };
const BILL_STAGE_FILTERS = [['', 'הכל'], ['tabled', 'הונחה'], ['preliminary', 'עברה טרומית'], ['first', 'עברה ראשונה'], ['passed', 'התקבלה כחוק'], ['stopped', 'נעצרה']];
const VOTE_LOOK = {
  'בעד': ['thumb_up', 'v-for'], 'נגד': ['thumb_down', 'v-against'], 'נמנע': ['do_not_disturb_on', 'v-abstain'],
  'נוכח': ['person_check', 'v-present'],
};
const KNESSET_SITE_MK_URL = 'https://main.knesset.gov.il/mk/apps/mk/mk-personal-details/';
const KNESSET_SITE_BILL_URL = 'https://main.knesset.gov.il/activity/legislation/laws/pages/LawBill.aspx?t=lawsuggestionssearch&lawitemid=';

let _pfRoute = { party: null, candidate: null, tab: null };
let _pfRenderToken = 0;
let _pf = null;
const _pfCache = new Map();

/* ── helpers ─────────────────────────────────────────────────────── */
const pfEsc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pfNum = n => Number(n || 0).toLocaleString('en-US');
const pfDmy = iso => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : '';
const pfPct = (part, whole) => whole ? Math.round(part / whole * 100) : 0;
const pfIcon = name => `<span class="material-symbols-outlined">${name}</span>`;
const pfDateCell = iso => iso ? `<div class="pf-date"><b>${iso.slice(8, 10)}.${iso.slice(5, 7)}</b>${iso.slice(0, 4)}</div>` : '<div class="pf-date"></div>';
const pfInitials = name => pfEsc(String(name || '').split(' ').filter(Boolean).slice(0, 2).map(w => w[0]).join(''));
const pfAvatar = (url, name) => `<div class="pf-avatar">${url ? `<img src="${pfEsc(url)}" alt="" loading="lazy" data-hide-on-error>` : pfInitials(name)}</div>`;
const pfSpinner = text => `<div class="pf-loading"><span class="pf-spin"></span> ${pfEsc(text)}</div>`;
const pfErrorBox = err => `<div class="pf-error">${pfEsc(err.message || err)}</div>`;

function pfHighlight(text, query) {
  const safe = pfEsc(text);
  const words = String(query || '').split(/\s+/).filter(w => w.length > 1).map(w => pfEsc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  return words.length ? safe.replace(new RegExp(words.join('|'), 'g'), m => `<mark>${m}</mark>`) : safe;
}

function pfFetch(url, { cache = true } = {}) {
  if (cache && _pfCache.has(url)) return _pfCache.get(url);
  const request = fetch(url).then(async response => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || body.message || `שגיאה ${response.status}`);
    return body;
  });
  if (cache) {
    _pfCache.set(url, request);
    request.catch(() => _pfCache.delete(url));
  }
  return request;
}

function pfDebounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* ── routing ─────────────────────────────────────────────────────── */
function _pfParseRoute(pathname = location.pathname, hash = location.hash) {
  const match = pathname.match(/^\/profiles(?:\/party\/(\d{1,4})(?:\/candidate\/(\d{1,4}))?)?\/?$/);
  const tab = hash.replace('#', '');
  return { party: match && match[1] ? Number(match[1]) : null,
           candidate: match && match[2] ? Number(match[2]) : null,
           tab: Object.hasOwn(PROFILE_TABS, tab) ? tab : null };
}

function _pfPath(route) {
  if (route.party == null) return PROFILES_PATH;
  const party = `${PROFILES_PATH}/party/${route.party}`;
  if (route.candidate == null) return party;
  return `${party}/candidate/${route.candidate}${route.tab ? '#' + route.tab : ''}`;
}

function profilesCurrentPath() { return _pfPath(_pfRoute); }

function profilesApplyRoute() {
  _pfRoute = _pfParseRoute();
  _pfRender();
}

/* Switching to the tab without a link: keep the profile that was open. */
function profilesShow() {
  if (!document.querySelector('#profiles-root > *')) _pfRender();
}

function profilesGo(href) {
  const url = new URL(href, location.origin);
  history.pushState(null, '', url.pathname + url.hash);
  _pfRoute = _pfParseRoute(url.pathname, url.hash);
  _pfRender();
  document.getElementById('tab-profiles').scrollTop = 0;
}

function _pfRender() {
  const root = document.getElementById('profiles-root');
  if (!root) return;
  _pfHidePopup();
  const token = ++_pfRenderToken;
  const current = () => token === _pfRenderToken;
  if (_pfRoute.party == null) _pfRenderParties(root, current);
  else if (_pfRoute.candidate == null) _pfRenderParty(root, _pfRoute.party, current);
  else _pfRenderCandidate(root, _pfRoute.party, _pfRoute.candidate, current);
}

const _pfLink = (href, inner, cls = '') => `<a href="${pfEsc(href)}" class="${cls}" data-click="profilesNav">${inner}</a>`;
const _pfCrumbs = items => `<nav class="pf-crumbs">${items.map(([label, href]) => href ? _pfLink(href, pfEsc(label)) : `<span>${pfEsc(label)}</span>`).join(pfIcon('chevron_left'))}</nav>`;

/* ── party grid ──────────────────────────────────────────────────── */
function _pfPartyTags(party) {
  return [
    party.full_profiles ? `<span class="pf-tag pf-tag--mk">${pfIcon('verified')}${party.full_profiles} ח"כ בכנסת ה-25</span>` : '',
    party.former_mks ? `<span class="pf-tag pf-tag--former">${pfIcon('history')}${party.former_mks} כיהנו בעבר</span>` : '',
    `<span class="pf-tag">${party.candidate_count} מועמדים</span>`,
  ].join('');
}

const _pfBallot = party => party.logo_url
  ? `<div class="pf-ballot pf-ballot--logo"><img src="${pfEsc(party.logo_url)}" alt="${pfEsc(party.name)}" loading="lazy"><span class="pf-ballot-chip">${pfEsc(party.letters)}</span></div>`
  : `<div class="pf-ballot">${party.ballot_url
    ? `<img src="${pfEsc(party.ballot_url)}" alt="פתק ${pfEsc(party.letters)}" loading="lazy">`
    : `<span class="pf-ballot-letters">${pfEsc(party.letters)}</span>`}</div>`;

async function _pfRenderParties(root, current) {
  root.innerHTML = `<div class="pf-page">${pfSpinner('טוען את הרשימות…')}</div>`;
  let data;
  try { data = await pfFetch('/api/profiles/parties'); } catch (err) { if (current()) root.innerHTML = `<div class="pf-page">${pfErrorBox(err)}</div>`; return; }
  if (!current()) return;
  root.innerHTML = `<div class="pf-page">
    <div class="pf-head">
      <div><h1>הרשימות לכנסת ה-26</h1>
      <p>${data.parties.length} רשימות, כפי שהוגשו <a href="${pfEsc(data.source)}" target="_blank" rel="noopener">לוועדת הבחירות המרכזית</a> (טרם אושרו סופית).</p></div>
      <input type="text" class="field field--sm pf-search" placeholder="חיפוש רשימה או מועמד בראשה" data-input="profilesFilterParties">
    </div>
    <div class="pf-party-grid" id="pf-party-grid">${data.parties.map(party => _pfLink(`${PROFILES_PATH}/party/${party.id}`, `
      ${_pfBallot(party)}
      <div class="pf-party-body">
        <div class="pf-party-name">${pfEsc(party.name)}</div>
        <div class="pf-party-leader">בראשות ${pfEsc(party.leader)}</div>
        <div class="pf-party-stats">${_pfPartyTags(party)}</div>
      </div>`, 'pf-party').replace('<a ', `<a data-search="${pfEsc(`${party.name} ${party.leader} ${party.letters}`)}" `)).join('')}</div>
  </div>`;
}

function profilesFilterParties(input) {
  const query = input.value.trim();
  document.querySelectorAll('#pf-party-grid .pf-party').forEach(card => {
    card.hidden = !!query && !card.dataset.search.includes(query);
  });
}

/* ── a party's candidates ────────────────────────────────────────── */

const JOINT_LIST_MAIN_PARTY_MAX_SHARE = 0.75;
async function _pfRenderParty(root, partyId, current) {
  root.innerHTML = `<div class="pf-page">${pfSpinner('טוען את הרשימה…')}</div>`;
  let party;
  try { party = await pfFetch(`/api/profiles/party/${partyId}`); } catch (err) { if (current()) root.innerHTML = `<div class="pf-page">${_pfCrumbs([['הרשימות', PROFILES_PATH]])}${pfErrorBox(err)}</div>`; return; }
  if (!current()) return;
  const fromPartyCounts = {};
  party.candidates.forEach(c => { fromPartyCounts[c.from_party] = (fromPartyCounts[c.from_party] || 0) + 1; });
  const [mainFromParty, mainFromPartyCount] = Object.entries(fromPartyCounts).sort((a, b) => b[1] - a[1])[0] || [];
  const isJointList = mainFromPartyCount < party.candidates.length * JOINT_LIST_MAIN_PARTY_MAX_SHARE;
  const card = c => {
    const inner = `<span class="pf-cand-pos">${c.position}</span>${pfAvatar(c.photo_url, c.name)}
      <div class="pf-cand-name">${pfEsc(c.name)}</div>
      ${c.profile === 'full' ? `<span class="pf-tag pf-tag--mk">${pfIcon('verified')}ח"כ בכנסת ה-25</span>`
        : c.profile === 'bills' ? `<span class="pf-tag pf-tag--former">${pfIcon('history')}כנסת ${c.knessets[c.knessets.length - 1]}</span>` : ''}
      ${c.from_party && (isJointList || c.from_party !== mainFromParty) ? `<div class="pf-cand-from">מטעם ${pfEsc(c.from_party)}</div>` : ''}`;
    return c.profile === 'none'
      ? `<div class="pf-cand pf-cand--none" data-profile="none">${inner}</div>`
      : _pfLink(`${PROFILES_PATH}/party/${partyId}/candidate/${c.position}`, inner, `pf-cand pf-cand--${c.profile}`).replace('<a ', `<a data-profile="${_esc(c.profile)}" `);
  };
  root.innerHTML = `<div class="pf-page">
    ${_pfCrumbs([['הרשימות', PROFILES_PATH], [party.name]])}
    <section class="pf-party-hero">${_pfBallot(party)}
      <div><h1>${pfEsc(party.name)}</h1>
      <p>${pfEsc(party.submitted_by)}</p>
      <div class="pf-party-stats">${_pfPartyTags(party)}</div>
      ${party.website ? `<div class="pf-links"><a class="btn btn-secondary btn-sm" href="${pfEsc(party.website)}" target="_blank" rel="noopener">${pfIcon('language')}לאתר המפלגה</a></div>` : ''}</div>
    </section>
    <div class="pf-legend">
      <div class="pf-legend-keys"><span class="pf-tag pf-tag--mk">${pfIcon('verified')}פרופיל מלא</span><span class="pf-tag pf-tag--former">${pfIcon('history')}כיהן בכנסת קודמת</span></div>
      <div class="seg seg--sm"><button class="seg-btn active" type="button" data-click="profilesFilterCandidates" data-arg="all">כל המועמדים</button><button class="seg-btn" type="button" data-click="profilesFilterCandidates" data-arg="mks">רק חברי כנסת</button></div>
    </div>
    <div class="pf-cand-grid" id="pf-cand-grid">${party.candidates.map(card).join('')}</div>
    <p class="pf-note">תמונות מאתר הכנסת, ולמועמדים שלא כיהנו בכנסת מוויקיפדיה כשיש. המספר הוא המקום ברשימה.</p>
  </div>`;
}

function profilesFilterCandidates(button) {
  button.parentElement.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b === button));
  const onlyMks = button.dataset.arg === 'mks';
  document.querySelectorAll('#pf-cand-grid > [data-profile]').forEach(card => { card.hidden = onlyMks && card.dataset.profile === 'none'; });
}

/* ── a candidate's profile ───────────────────────────────────────── */
function _pfHeroRole(data) {
  const roles = data.roles;
  const candidate = data.candidate;
  const parts = [];
  if (roles) {
    roles.government.forEach(r => parts.push(`<b>${pfEsc(r.position_name || r.govministry_name)}</b>`));
    roles.knesset_roles.forEach(r => parts.push(`<b>${pfEsc(r.position)}</b>`));
  }
  const knessets = candidate.knessets;
  const served = knessets.length === 1 ? `הכנסת ה-${knessets[0]}` : `הכנסות ה-${knessets.slice(0, -1).join(', ה-')} וה-${knessets[knessets.length - 1]}`;
  const faction = roles && roles.factions.length ? roles.factions[roles.factions.length - 1].faction_name : '';
  parts.push(`${candidate.profile === 'full' ? 'חבר/ת' : 'כיהן/ה ב'}${candidate.profile === 'full' ? ` ${served}` : served}${faction ? ` · סיעת ${pfEsc(faction)}` : ''}`);
  return parts.join(' · ');
}

const DETAIL_ROWS = [['birth_date', 'cake', 'נולד/ה'], ['place_of_birth', 'location_on', 'מקום לידה'], ['residence', 'home', 'מגורים'],
                     ['military_service', 'military_tech', 'שירות צבאי'], ['education', 'school', 'השכלה'], ['profession', 'work', 'מקצוע']];

const DETAIL_SHORT_MAX_CHARS = 40;

function _pfDetailItems(text) {
  return text.replace(/&#x0?D;/gi, '').split(/[\r\n]+/).map(line => line.trim().replace(/^[-–•]\s*/, '')).filter(Boolean);
}

function _pfDetails(details) {
  const rows = DETAIL_ROWS.filter(([key]) => details[key]).map(([key, icon, label]) => ({ icon, label, items: _pfDetailItems(details[key]) }));
  const isShort = row => row.items.length === 1 && row.items[0].length <= DETAIL_SHORT_MAX_CHARS;
  const facts = rows.filter(isShort).map(row => `<span class="pf-fact" title="${row.label}">${pfIcon(row.icon)}${pfEsc(row.items[0])}</span>`).join('');
  const blocks = rows.filter(row => !isShort(row)).map(row => `<div class="pf-detail-block"><div class="pf-detail-head">${pfIcon(row.icon)}${row.label}</div>${row.items.length > 1
    ? `<ul class="pf-detail-list">${row.items.map(item => `<li>${pfEsc(item)}</li>`).join('')}</ul>` : `<p>${pfEsc(row.items[0])}</p>`}</div>`).join('');
  if (!facts && !blocks) return '';
  return `<div class="pf-details">${facts ? `<div class="pf-facts">${facts}</div>` : ''}${blocks ? `<div class="pf-detail-blocks">${blocks}</div>` : ''}</div>`;
}

function _pfStat(id, num, label, bar = null, wait = false) {
  return `<div class="pf-stat${wait ? ' pf-stat--wait' : ''}" id="${id}"><span class="pf-stat-num">${num}</span><span class="pf-stat-label">${label}</span>${bar === null ? '' : `<div class="pf-stat-bar"><i style="width:${bar}%"></i></div>`}</div>`;
}

function _pfHeroStats(data) {
  const activity = data.activity;
  if (data.candidate.profile !== 'full') {
    return _pfStat('pf-stat-knessets', data.candidate.knessets.length, 'כהונות בכנסת')
      + _pfStat('pf-stat-votes', '…', 'נוכחות בהצבעות במליאה', 0, true) + _pfStat('pf-stat-bills', '…', 'הצעות חוק', null, true);
  }
  const attendance = activity.attendance;
  const committeePct = pfPct(attendance.member_meetings_attended, attendance.member_meetings);
  return [
    _pfStat('pf-stat-opinions', pfNum(activity.opinions), `עמדות בפרוטוקולים, ב-${pfNum(activity.meetings_spoke)} ישיבות`),
    attendance.member_meetings
      ? _pfStat('pf-stat-committee', `${committeePct}<small>%</small>`, `נוכחות בוועדות: ${pfNum(attendance.member_meetings_attended)} מתוך ${pfNum(attendance.member_meetings)} ישיבות של ועדות שהיה/תה חבר/ה בהן`, committeePct)
      : _pfStat('pf-stat-committee', pfNum(attendance.meetings_attended), 'ישיבות ועדה שנכח/ה בהן'),
    _pfStat('pf-stat-votes', '…', 'נוכחות בהצבעות במליאה', 0, true),
    _pfStat('pf-stat-bills', '…', 'הצעות חוק', null, true),
  ].join('');
}

async function _pfRenderCandidate(root, partyId, candidateId, current) {
  const base = `${PROFILES_PATH}/party/${partyId}/candidate/${candidateId}`;
  const api = `/api/profiles/party/${partyId}/candidate/${candidateId}`;
  if (_pf && _pf.api === api && root.querySelector('.pf-hero')) { _pfSelectTab(_pfRoute.tab || _pf.tabs[0], { writeUrl: false }); return; }
  root.innerHTML = `<div class="pf-page">${pfSpinner('טוען את הפרופיל…')}</div>`;
  let data;
  try { data = await pfFetch(api); } catch (err) { if (current()) root.innerHTML = `<div class="pf-page">${_pfCrumbs([['הרשימות', PROFILES_PATH], ['הרשימה', `${PROFILES_PATH}/party/${partyId}`]])}${pfErrorBox(err)}</div>`; return; }
  if (!current()) return;
  const { candidate, party } = data;
  const crumbs = _pfCrumbs([['הרשימות', PROFILES_PATH], [party.name, `${PROFILES_PATH}/party/${partyId}`], [candidate.name]]);
  if (candidate.profile === 'none') {
    root.innerHTML = `<div class="pf-page">${crumbs}<div class="pf-empty">${pfEsc(candidate.name)}, מקום ${candidate.position} ברשימה, לא כיהן/ה בכנסת, ולכן אין עדיין פרופיל.</div></div>`;
    return;
  }
  const tabs = PROFILE_TABS_BY_DEPTH[candidate.profile];
  _pf = { api, base, data, tabs, loaded: {}, themes: null, evidence: {},
          opinions: { q: '', theme: '', from: '', to: '', group: 'date', rows: [], total: 0, facets: null, loading: false },
          votes: { q: '', filter: 'all', rows: [], total: 0, loading: false },
          bills: { q: '', role: '', stage: '', rows: [], total: 0, loading: false } };
  root.innerHTML = `<div class="pf-page">
    ${crumbs}
    <section class="pf-hero">
      <div class="pf-portrait">${pfAvatar(candidate.photo_url, candidate.name)}</div>
      <div class="pf-hero-main">
        <div class="pf-eyebrow">
          ${_pfLink(`${PROFILES_PATH}/party/${partyId}`, `${pfIcon('how_to_vote')}${pfEsc(party.name)}`, 'pf-chip pf-chip--party')}
          <span class="pf-chip">${pfIcon('format_list_numbered')}מקום ${candidate.position} ברשימה</span>
        </div>
        <h1>${pfEsc(candidate.name)}</h1>
        <div class="pf-role-line">${_pfHeroRole(data)}</div>
        ${_pfDetails(candidate.details)}
        ${candidate.site_id ? `<div class="pf-links"><a class="btn btn-secondary btn-sm" href="${KNESSET_SITE_MK_URL}${Number(candidate.site_id)}" target="_blank" rel="noopener">${pfIcon('open_in_new')}אתר הכנסת</a></div>` : ''}
      </div>
      <div class="pf-stats">${_pfHeroStats(data)}</div>
    </section>
    <div class="pf-tabs"><nav class="seg">${tabs.map(tab => `<button class="seg-btn" type="button" data-click="profilesTab" data-arg="${_esc(tab)}">${pfIcon(PROFILE_TABS[tab][1])}${PROFILE_TABS[tab][0]}</button>`).join('')}</nav></div>
    ${tabs.map(tab => `<div class="pf-panel" id="pf-panel-${tab}" hidden></div>`).join('')}
  </div>`;
  _pfSelectTab(_pfRoute.tab && tabs.includes(_pfRoute.tab) ? _pfRoute.tab : tabs[0], { writeUrl: false });
  _pfLoadHeaderNumbers();
}

async function _pfLoadHeaderNumbers() {
  const profile = _pf;
  pfFetch(`${profile.api}/bills`).then(bills => {
    if (profile !== _pf) return;
    _pfSetStat('pf-stat-bills', pfNum(bills.total), 'הצעות חוק שיזם/ה או הצטרף/ה אליהן');
  }).catch(err => _pfSetStat('pf-stat-bills', '—', `הצעות חוק: ${err.message}`));
  pfFetch(`${profile.api}/vote-summary`).then(summary => {
    if (profile !== _pf) return;
    const latest = summary.knessets.find(k => k.plenum_votes);
    if (!latest) { _pfSetStat('pf-stat-votes', '—', 'אין הצבעות רשומות במליאה'); return; }
    const pct = pfPct(latest.votes_cast, latest.plenum_votes);
    _pfSetStat('pf-stat-votes', `${pct}<small>%</small>`, `נוכחות בהצבעות: ${pfNum(latest.votes_cast)} מתוך ${pfNum(latest.plenum_votes)} הצבעות במליאת הכנסת ה-${latest.knesset_num}`, pct);
  }).catch(err => _pfSetStat('pf-stat-votes', '—', `הצבעות: ${err.message}`));
}

function _pfSetStat(id, numHtml, label, bar = null) {
  const stat = document.getElementById(id);
  if (!stat) return;
  stat.classList.remove('pf-stat--wait');
  stat.querySelector('.pf-stat-num').innerHTML = numHtml;
  stat.querySelector('.pf-stat-label').textContent = label;
  const fill = stat.querySelector('.pf-stat-bar i');
  if (fill && bar !== null) requestAnimationFrame(() => { fill.style.width = `${bar}%`; });
}

function _pfSelectTab(tab, { writeUrl = true } = {}) {
  if (!_pf || !_pf.tabs.includes(tab)) return;
  document.querySelectorAll('.pf-tabs .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.arg === tab));
  _pf.tabs.forEach(t => { document.getElementById(`pf-panel-${t}`).hidden = t !== tab; });
  _pfRoute.tab = tab === _pf.tabs[0] ? null : tab;
  if (writeUrl) {
    history.replaceState(null, '', profilesCurrentPath());
    const scroller = document.getElementById('tab-profiles');
    const bar = document.querySelector('.pf-tabs');
    if (bar && scroller.scrollTop > bar.offsetTop) scroller.scrollTop = bar.offsetTop;
  }
  if (!_pf.loaded[tab]) {
    _pf.loaded[tab] = true;
    ({ themes: _pfLoadThemesTab, votes: _pfLoadVotesTab, bills: _pfLoadBillsTab, roles: _pfLoadRolesTab })[tab]();
  }
}

function profilesTab(button) { _pfSelectTab(button.dataset.arg); }

const _pfCardHead = (icon, title, sub, extra = '') => `<div class="pf-card-head"><div class="pf-card-icon">${pfIcon(icon)}</div><div class="pf-card-head-text"><span class="pf-card-title">${title}</span>${sub ? `<span class="pf-card-sub">${sub}</span>` : ''}</div>${extra}</div>`;

/* ── themes + opinions ───────────────────────────────────────────── */
async function _pfLoadThemesTab() {
  const profile = _pf;
  const panel = document.getElementById('pf-panel-themes');
  panel.innerHTML = `<section class="pf-card" id="pf-themes-card">${_pfCardHead('insights', 'הנושאים המרכזיים בכנסת ה-25', '')}<div class="pf-card-body">${pfSpinner('טוען נושאים…')}</div></section>
    <section class="pf-card" id="pf-opinions-card">${_pfCardHead('format_quote', 'כל העמדות <span class="pf-count" id="pf-op-total"></span>', 'מתוך סיכומי הפרוטוקולים, מהחדש לישן',
      `<div class="seg seg--sm"><button class="seg-btn active" type="button" data-click="profilesOpinionGroup" data-arg="date">לפי תאריך</button><button class="seg-btn" type="button" data-click="profilesOpinionGroup" data-arg="committee">לפי ועדה</button></div>`)}
      <div class="pf-card-body">
        <div class="pf-tools"><input type="text" class="field field--sm pf-search" placeholder="חיפוש בעמדות ובציטוטים" data-input="profilesOpinionSearch">
          <label class="pf-date-field"><span>מתאריך</span><input type="date" class="field field--sm" id="pf-op-from" data-change="profilesOpinionDates"></label>
          <label class="pf-date-field"><span>עד</span><input type="date" class="field field--sm" id="pf-op-to" data-change="profilesOpinionDates"></label></div>
        <div class="pf-pills" id="pf-op-pills"></div>
        <div class="pf-result-count" id="pf-op-count"></div>
        <div id="pf-op-list">${pfSpinner('טוען עמדות…')}</div>
        <div class="pf-center"><button class="btn btn-secondary btn-sm" type="button" id="pf-op-more" data-click="profilesOpinionMore" hidden>${pfIcon('expand_more')}הצגת עוד</button></div>
      </div></section>`;
  try {
    profile.themes = await pfFetch(`${profile.api}/themes`);
  } catch (err) {
    if (profile === _pf) panel.querySelector('#pf-themes-card .pf-card-body').innerHTML = pfErrorBox(err);
    profile.themes = { themes: [], quarters: [] };
  }
  if (profile !== _pf) return;
  profile.themes.themes.forEach((theme, i) => {
    theme.color = THEME_COLORS[i % THEME_COLORS.length];
    theme.evidence.forEach((e, n) => { profile.evidence[`${theme.id}-${n}`] = e; });
  });
  _pfRenderThemes(false);
  _pfLoadOpinions(true);
}

function _pfThemeById(id) { return (_pf.themes?.themes || []).find(t => String(t.id) === String(id)); }

function _pfRenderThemes(showAll) {
  const data = _pf.themes;
  const body = document.querySelector('#pf-themes-card .pf-card-body');
  const sub = document.querySelector('#pf-themes-card .pf-card-sub');
  if (!data.themes.length) { body.innerHTML = '<div class="pf-empty">עוד לא נוצרו נושאים לחבר/ת הכנסת הזה/ו.</div>'; return; }
  if (sub) sub.textContent = `${data.themes.length} נושאים שעולים מכל העמדות שהביע/ה, עם מקור לכל טענה`;
  const quarters = data.quarters;
  const firstYear = quarters.length ? quarters[0].slice(0, 4) : '', lastYear = quarters.length ? quarters[quarters.length - 1].slice(0, 4) : '';
  const shown = showAll ? data.themes : data.themes.slice(0, THEMES_SHOWN);
  const card = theme => {
    const top = Math.max(1, ...theme.quarter_counts);
    const cites = theme.evidence.map((_, n) => `<sup class="pf-cite" data-click="profilesCite" data-cite="${theme.id}-${n}">${n + 1}</sup>`).join('');
    const committee = theme.top_committees[0] ? theme.top_committees[0][0] : '';
    return `<article class="pf-theme" id="pf-theme-${theme.id}" style="--tc:${theme.color}">
      <button class="pf-theme-toggle" type="button" data-click="profilesThemeToggle" aria-expanded="false">
        <span class="pf-theme-top"><h3>${pfEsc(theme.title)}</h3>${pfIcon('expand_more').replace('material-symbols-outlined', 'material-symbols-outlined pf-theme-chev')}</span>
        <span class="pf-theme-stats"><span><b>${pfNum(theme.opinion_count)}</b> עמדות</span><span><b>${pfNum(theme.meeting_count)}</b> ישיבות</span>${committee ? `<span>${pfEsc(committee)}</span>` : ''}</span>
        <span class="pf-theme-spark">${_pfSpark(theme, quarters, top)}<span class="pf-spark-axis"><span>${firstYear}</span><span>${lastYear}</span></span></span>
      </button>
      <div class="pf-theme-more"><div>
        <p>${pfEsc(theme.summary)} ${cites}</p>
        <div class="pf-theme-foot"><button class="pf-link" type="button" data-click="profilesThemeSheet" data-arg="${_esc(theme.id)}">לכל ${pfNum(theme.opinion_count)} העמדות בנושא${pfIcon('chevron_left')}</button></div>
      </div></div>
    </article>`;
  };
  body.innerHTML = `
    <div class="pf-ai-band">${pfIcon('auto_awesome')}<span>${pfNum(data.opinions_in_a_theme)} מתוך ${pfNum(data.total_opinions)} העמדות שייכות לאחד הנושאים · נוצר אוטומטית (Gemini) מסיכומי הפרוטוקולים · לחצו על עמודה בגרף כדי לראות את העמדות מאותו רבעון</span></div>
    <div class="pf-theme-map">${data.themes.map(t => `<button type="button" style="flex:${t.opinion_count};--tc:${t.color}" title="${pfEsc(t.title)} · ${pfNum(t.opinion_count)} עמדות" data-click="profilesThemeJump" data-arg="${_esc(t.id)}"></button>`).join('')}</div>
    <div class="pf-themes">${shown.map(card).join('')}</div>
    ${data.themes.length > THEMES_SHOWN ? `<div class="pf-center"><button class="btn btn-secondary btn-sm" type="button" data-click="profilesThemesAll" data-arg="${_esc(showAll ? '' : '1')}">${showAll ? `${pfIcon('expand_less')}הצגת ${THEMES_SHOWN} הנושאים הראשונים` : `${pfIcon('expand_more')}הצגת כל ${data.themes.length} הנושאים`}</button></div>` : ''}`;
}

const QUARTER_MONTHS = ['ינואר–מרץ', 'אפריל–יוני', 'יולי–ספטמבר', 'אוקטובר–דצמבר'];
const pfQuarterLabel = quarter => `${QUARTER_MONTHS[Number(quarter[5]) - 1]} ${quarter.slice(0, 4)}`;
const pfQuarterOf = iso => `${iso.slice(0, 4)}Q${Math.floor((Number(iso.slice(5, 7)) - 1) / 3) + 1}`;

function pfQuarterRange(quarter) {
  const year = Number(quarter.slice(0, 4)), first = (Number(quarter[5]) - 1) * 3 + 1;
  const lastDay = new Date(Date.UTC(year, first + 2, 0)).getUTCDate();
  const month = n => String(n).padStart(2, '0');
  return [`${year}-${month(first)}-01`, `${year}-${month(first + 2)}-${lastDay}`];
}

function _pfSpark(theme, quarters, top, height = 22, action = 'profilesThemeQuarter') {
  return `<span class="pf-spark" style="height:${height}px">${theme.quarter_counts.map((v, i) => v
    ? `<i class="${v === top ? 'hot' : ''}" style="height:${Math.max(2, v / top * height)}px" title="${pfQuarterLabel(quarters[i])} · ${v} עמדות" data-click="${_esc(action)}" data-theme="${_esc(theme.id)}" data-quarter="${_esc(quarters[i])}"></i>`
    : '<i class="empty"></i>').join('')}</span>`;
}

const _pfOpinionsUrl = params => `${_pf.api}/opinions?${new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null))}`;

/* A spark bar: the theme's opinions of that quarter, in a citation-like popup next to the bar. */
async function profilesThemeQuarter(bar, event) {
  event.stopPropagation();
  event.preventDefault();
  const popup = document.getElementById('pf-popup');
  if (_pfPopupAnchor === bar && !popup.hidden) { _pfHidePopup(); return; }
  _pfHidePopup();
  _pfPopupAnchor = bar;
  bar.classList.add('on');
  const theme = _pfThemeById(bar.dataset.theme), quarter = bar.dataset.quarter;
  const [from, to] = pfQuarterRange(quarter);
  const head = total => `<div class="pf-popup-head"><b>${pfEsc(theme.title)}</b><span>${pfQuarterLabel(quarter)}${total == null ? '' : ` · ${pfNum(total)} עמדות`}</span></div>`;
  popup.classList.add('pf-popup--list');
  popup.style.setProperty('--tc', theme.color);
  popup.innerHTML = head(null) + pfSpinner('טוען עמדות…');
  _pfPlacePopup(popup, bar);
  let page;
  try { page = await pfFetch(_pfOpinionsUrl({ theme: theme.id, date_from: from, date_to: to })); } catch (err) { if (_pfPopupAnchor === bar) popup.innerHTML = head(null) + pfErrorBox(err); return; }
  if (_pfPopupAnchor !== bar) return;
  popup.innerHTML = `${head(page.total)}
    <div class="pf-popup-list">${page.opinions.map(o => `<div class="pf-popup-item">
      <div class="pf-popup-date">${pfDmy(o.date)} · ${pfEsc(o.committee)}</div>
      <div class="pf-popup-opinion">${pfEsc(o.opinion)}</div>
      ${o.quote ? `${o.quote_verified ? '' : `<span class="pf-verified pf-approx">${pfIcon('error')}ציטוט משוער</span>`}<q>${pfEsc(o.quote)}</q>` : ''}
      <a class="pf-link-btn" href="${pfEsc(_pfProtocolUrl(o))}" target="_blank" rel="noopener">${pfIcon('description')}לפרוטוקול</a>
    </div>`).join('')}</div>
    <div class="pf-popup-foot"><span></span><button class="btn btn-secondary btn-sm" type="button" data-click="profilesThemeSheet" data-arg="${_esc(theme.id)}" data-quarter="${_esc(quarter)}">${page.total > page.opinions.length ? `כל ${pfNum(page.total)} העמדות ברבעון` : 'כל העמדות בנושא'}${pfIcon('chevron_left')}</button></div>`;
  _pfPlacePopup(popup, bar);
}

/* All of a theme's opinions: a side sheet with the theme's timeline, filterable by quarter. */
let _pfSheet = null;
function profilesThemeSheet(button) {
  const theme = _pfThemeById(button.dataset.arg);
  if (!theme) return;
  _pfHidePopup();
  _pfSheet = { theme, quarter: button.dataset.quarter || '', rows: [], total: 0 };
  const sheet = document.getElementById('pf-theme-sheet');
  sheet.style.setProperty('--tc', theme.color);
  sheet.querySelector('.pf-sheet-head').innerHTML = `<div class="pf-sheet-title"><h2>${pfEsc(theme.title)}</h2>
      <div class="pf-theme-stats"><span><b>${pfNum(theme.opinion_count)}</b> עמדות</span><span><b>${pfNum(theme.meeting_count)}</b> ישיבות</span></div></div>
    <button class="btn btn-ghost btn-icon" type="button" title="סגירה" data-click="profilesCloseSheet">${pfIcon('close')}</button>`;
  sheet.querySelector('.pf-sheet-summary').textContent = theme.summary;
  _pfRenderSheetTimeline();
  sheet.classList.add('open');
  _pfLoadSheet(true);
}

function _pfRenderSheetTimeline() {
  const { theme, quarter } = _pfSheet;
  const quarters = _pf.themes.quarters, top = Math.max(1, ...theme.quarter_counts);
  const timeline = document.querySelector('#pf-theme-sheet .pf-sheet-timeline');
  timeline.innerHTML = `${_pfSpark(theme, quarters, top, 56, 'profilesSheetQuarter')}
    <div class="pf-spark-axis"><span>${quarters[0]?.slice(0, 4) || ''}</span><span>${quarters[quarters.length - 1]?.slice(0, 4) || ''}</span></div>
    <div class="pf-sheet-filter">${quarter
      ? `<span class="pf-pill on"><span class="dot"></span>${pfQuarterLabel(quarter)}</span><button class="pf-link" type="button" data-click="profilesSheetQuarter" data-quarter="">כל התקופה</button>`
      : '<span>לחצו על עמודה כדי לסנן לרבעון</span>'}</div>`;
  timeline.querySelectorAll('.pf-spark i').forEach(bar => bar.classList.toggle('on', !!quarter && bar.dataset.quarter === quarter));
  timeline.classList.toggle('filtered', !!quarter);
}

function profilesSheetQuarter(element) {
  _pfSheet.quarter = _pfSheet.quarter === element.dataset.quarter ? '' : element.dataset.quarter;
  _pfRenderSheetTimeline();
  _pfLoadSheet(true);
}

async function _pfLoadSheet(reset) {
  const sheetState = _pfSheet;
  const sheet = document.getElementById('pf-theme-sheet');
  const list = sheet.querySelector('.pf-sheet-list'), more = sheet.querySelector('[data-pf-sheet-more]');
  if (reset) { sheetState.rows = []; list.innerHTML = pfSpinner('טוען עמדות…'); more.hidden = true; sheet.querySelector('.pf-sheet-body').scrollTop = 0; }
  const [from, to] = sheetState.quarter ? pfQuarterRange(sheetState.quarter) : ['', ''];
  more.disabled = true;
  let page;
  try { page = await pfFetch(_pfOpinionsUrl({ theme: sheetState.theme.id, date_from: from, date_to: to, offset: sheetState.rows.length })); } catch (err) { if (sheetState === _pfSheet) list.innerHTML = pfErrorBox(err); return; } finally { more.disabled = false; }
  if (sheetState !== _pfSheet) return;
  sheetState.rows = sheetState.rows.concat(page.opinions);
  sheetState.total = page.total;
  let lastQuarter = '';
  list.innerHTML = sheetState.rows.map(o => {
    const quarter = pfQuarterOf(o.date);
    const head = quarter !== lastQuarter ? `<div class="pf-op-group-head">${pfIcon('date_range')}${pfQuarterLabel(quarter)}</div>` : '';
    lastQuarter = quarter;
    return head + _pfOpinionRow(o, { showTheme: false });
  }).join('') || '<div class="pf-empty">אין עמדות ברבעון הזה</div>';
  more.hidden = sheetState.rows.length >= sheetState.total;
}

function profilesSheetMore() { _pfLoadSheet(false); }
function profilesCloseSheet() {
  const sheet = document.getElementById('pf-theme-sheet');
  if (sheet) sheet.classList.remove('open');
  _pfSheet = null;
}

function profilesThemeToggle(button) {
  const theme = button.closest('.pf-theme');
  const open = theme.classList.toggle('open');
  button.setAttribute('aria-expanded', open);
  if (!open) _pfHidePopup();
}

function profilesThemeJump(button) {
  const id = button.dataset.arg;
  if (!document.getElementById(`pf-theme-${id}`)) _pfRenderThemes(true);
  const theme = document.getElementById(`pf-theme-${id}`);
  theme.classList.add('open');
  theme.scrollIntoView({ behavior: 'smooth', block: 'center' });
  theme.classList.add('flash');
  setTimeout(() => theme.classList.remove('flash'), 1200);
}

function profilesThemesAll(button) { _pfRenderThemes(button.dataset.arg === '1'); }

function _pfProtocolUrl(o) {
  const params = new URLSearchParams({ meeting: o.meeting_id });
  if (o.speech_idx != null) {
    params.set('speech', o.speech_idx);
    if (o.quote_offset != null && o.quote_length) { params.set('offset', o.quote_offset); params.set('length', o.quote_length); }
  }
  return `${PROTOCOLS_PATH}?${params}`;
}

let _pfPopupAnchor = null;
function profilesCite(sup, event) {
  event.stopPropagation();
  const e = _pf.evidence[sup.dataset.cite];
  if (!e) return;
  const popup = document.getElementById('pf-popup');
  if (_pfPopupAnchor === sup && !popup.hidden) { _pfHidePopup(); return; }
  _pfHidePopup();
  _pfPopupAnchor = sup;
  sup.classList.add('on');
  popup.innerHTML = `<div class="pf-popup-meeting">${pfEsc(e.committee)}</div><div class="pf-popup-date">${pfDmy(e.date)}</div>
    <div class="pf-popup-opinion">${pfEsc(e.opinion)}</div>${e.quote ? `<q>${pfEsc(e.quote)}</q>` : ''}
    <div class="pf-popup-foot">${e.quote_verified ? `<span class="pf-verified">${pfIcon('verified')}הציטוט אומת מול הפרוטוקול</span>` : `<span class="pf-verified pf-approx">${pfIcon('error')}ציטוט משוער</span>`}
    <a class="btn btn-secondary btn-sm" href="${pfEsc(_pfProtocolUrl(e))}" target="_blank" rel="noopener">${pfIcon('description')}לפרוטוקול</a></div>`;
  _pfPlacePopup(popup, sup);
}

function _pfPlacePopup(popup, anchor) {
  popup.hidden = false;
  const rect = anchor.getBoundingClientRect(), width = popup.offsetWidth, height = popup.offsetHeight;
  popup.style.left = `${Math.min(Math.max(16, rect.left + rect.width / 2 - width / 2), innerWidth - width - 16)}px`;
  const tabBar = document.getElementById('mobile-tab-bar');
  const visibleBottom = Math.min(innerHeight, tabBar && tabBar.offsetHeight ? tabBar.getBoundingClientRect().top : innerHeight) - 8;
  const top = rect.top - height - 10 > 70 ? rect.top - height - 10 : Math.min(rect.bottom + 10, visibleBottom - height);
  popup.style.top = `${Math.max(8, top)}px`;
}

function _pfHidePopup() {
  const popup = document.getElementById('pf-popup');
  if (popup) { popup.hidden = true; popup.classList.remove('pf-popup--list'); }
  document.querySelectorAll('sup.pf-cite.on, #pf-themes-card .pf-spark i.on').forEach(el => el.classList.remove('on'));
  _pfPopupAnchor = null;
}

async function _pfLoadOpinions(reset) {
  const profile = _pf, state = profile.opinions;
  if (reset) state.rows = [];
  const params = new URLSearchParams({ offset: state.rows.length });
  if (state.q) params.set('q', state.q);
  if (state.theme) params.set('theme', state.theme);
  if (state.from) params.set('date_from', state.from);
  if (state.to) params.set('date_to', state.to);
  const key = `${state.q}|${state.theme}|${state.from}|${state.to}`;
  state.key = key;
  const list = document.getElementById('pf-op-list');
  if (reset) list.innerHTML = pfSpinner('טוען עמדות…');
  const more = document.getElementById('pf-op-more');
  more.disabled = true;
  let page;
  try { page = await pfFetch(`${profile.api}/opinions?${params}`); } catch (err) { if (profile === _pf) list.innerHTML = pfErrorBox(err); return; } finally { more.disabled = false; }
  if (profile !== _pf || state.key !== key) return;
  state.rows = reset ? page.opinions : state.rows.concat(page.opinions);
  state.total = page.total;
  if (!state.facets) state.facets = page.facets;
  _pfRenderOpinions();
}

function _pfRenderOpinions() {
  const state = _pf.opinions;
  const themes = _pf.themes?.themes || [];
  const counts = state.facets?.themes || {};
  document.getElementById('pf-op-total').textContent = pfNum(_pf.data.activity?.opinions);
  const pill = (id, label, color, count) => `<button class="pf-pill${String(state.theme) === String(id) ? ' on' : ''}" type="button" data-click="profilesOpinionTheme" data-arg="${_esc(id)}" style="--tc:${color}" title="${pfEsc(label)}"><span class="dot"></span><span>${pfEsc(label)}</span> · ${pfNum(count)}</button>`;
  const themed = themes.filter(t => counts[t.id]);
  const shown = state.pillsOpen ? themed : themed.filter((t, i) => i < THEME_PILLS_SHOWN || String(t.id) === String(state.theme));
  const hiddenCount = themed.length - shown.length;
  document.getElementById('pf-op-pills').innerHTML = (counts[OTHER_THEME] ? pill(OTHER_THEME, OTHER_THEME_LABEL, 'var(--outline-variant)', counts[OTHER_THEME]) : '')
    + shown.map(t => pill(t.id, t.title, t.color, counts[t.id])).join('')
    + (hiddenCount > 0 ? `<button class="pf-pill" type="button" data-click="profilesOpinionPills">${pfIcon('add')}<span>עוד ${hiddenCount} נושאים</span></button>`
      : state.pillsOpen && themed.length > THEME_PILLS_SHOWN ? `<button class="pf-pill" type="button" data-click="profilesOpinionPills">${pfIcon('remove')}<span>פחות</span></button>` : '');
  const filtered = state.q || state.theme || state.from || state.to;
  document.getElementById('pf-op-count').textContent = `${pfNum(state.total)} עמדות${filtered ? ' מתאימות' : ''}`;
  document.getElementById('pf-op-more').hidden = state.rows.length >= state.total;
  const list = document.getElementById('pf-op-list');
  if (!state.rows.length) { list.innerHTML = '<div class="pf-empty">אין עמדות שמתאימות לחיפוש</div>'; return; }
  const row = o => _pfOpinionRow(o, { query: state.q, showCommittee: state.group !== 'committee' });
  if (state.group === 'date') { list.innerHTML = state.rows.map(row).join(''); return; }
  const groups = new Map();
  state.rows.forEach(o => { if (!groups.has(o.committee)) groups.set(o.committee, []); groups.get(o.committee).push(o); });
  list.innerHTML = [...groups.entries()].sort((a, b) => b[1].length - a[1].length).map(([committee, rows]) =>
    `<div><div class="pf-op-group-head">${pfIcon('meeting_room')}${pfEsc(committee)} <span class="pf-count">${rows.length}</span></div>${rows.map(row).join('')}</div>`).join('');
}

function _pfOpinionRow(o, { query = '', showCommittee = true, showTheme = true } = {}) {
  const theme = o.theme_ids.length ? _pfThemeById(o.theme_ids[0]) : null;
  const meta = [showTheme ? (theme ? pfEsc(theme.title) : OTHER_THEME_LABEL) : '', showCommittee ? pfEsc(o.committee) : ''].filter(Boolean).join(' · ');
  return `<article class="pf-op" style="--tc:${theme ? theme.color : 'var(--outline-variant)'}">
    ${pfDateCell(o.date)}
    <div>
      ${meta ? `<div class="pf-op-meta"><span class="tdot"></span>${meta}</div>` : ''}
      ${o.meeting_topic ? `<div class="pf-op-topic" title="${pfEsc(o.meeting_topic)}">${pfEsc(o.meeting_topic)}</div>` : ''}
      <div class="pf-op-text"><span class="pf-bullet"></span><span>${pfHighlight(o.opinion, query)}</span></div>
      ${o.quote ? `<div class="pf-op-quote${o.quote_verified ? '' : ' approx'}">${o.quote_verified ? '' : `<span class="pf-verified pf-approx">${pfIcon('error')}ציטוט משוער</span> `}"${pfHighlight(o.quote, query)}"</div>` : ''}
      <div class="pf-op-actions"><a class="pf-link-btn" href="${pfEsc(_pfProtocolUrl(o))}" target="_blank" rel="noopener">${pfIcon('description')}לפרוטוקול</a></div>
    </div>
  </article>`;
}

const _pfOpinionSearch = pfDebounce(value => { _pf.opinions.q = value; _pfLoadOpinions(true); }, 350);
function profilesOpinionSearch(input) { _pfOpinionSearch(input.value.trim()); }
function profilesOpinionTheme(button) {
  _pf.opinions.theme = String(_pf.opinions.theme) === button.dataset.arg ? '' : button.dataset.arg;
  _pfLoadOpinions(true);
}
function profilesOpinionGroup(button) {
  button.parentElement.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b === button));
  _pf.opinions.group = button.dataset.arg;
  _pfRenderOpinions();
}
function profilesOpinionDates() {
  _pf.opinions.from = document.getElementById('pf-op-from').value;
  _pf.opinions.to = document.getElementById('pf-op-to').value;
  _pfLoadOpinions(true);
}
function profilesOpinionMore() { _pfLoadOpinions(false); }
function profilesOpinionPills() { _pf.opinions.pillsOpen = !_pf.opinions.pillsOpen; _pfRenderOpinions(); }

/* ── votes ───────────────────────────────────────────────────────── */
function _pfVotePill(result) {
  const [icon, cls] = VOTE_LOOK[result] || ['remove', 'v-absent'];
  return `<span class="pf-vpill ${cls}">${pfIcon(icon)}${pfEsc(result || '—')}</span>`;
}

function _pfVoteBar(byResult, total) {
  const pct = n => total ? n / total * 100 : 0;
  return `<div class="pf-vs-bar"><i style="flex:${pct(byResult['בעד'])};background:var(--primary)"></i><i style="flex:${pct(byResult['נגד'])};background:var(--error)"></i><i style="flex:${pct(byResult['נמנע'] + byResult.other)};background:var(--warn-strong)"></i><i style="flex:${100 - pct(byResult['בעד'] + byResult['נגד'] + byResult['נמנע'] + byResult.other)}"></i></div>`;
}

async function _pfLoadVotesTab() {
  const profile = _pf;
  const panel = document.getElementById('pf-panel-votes');
  panel.innerHTML = `<section class="pf-card" id="pf-votes-card">${_pfCardHead('how_to_vote', 'הצבעות במליאה', 'ההצבעות שלו/ה, מהחדשה לישנה',
      `<div class="seg seg--sm">${[['all', 'הכל'], ['בעד', 'בעד'], ['נגד', 'נגד'], ['other', 'אחר']].map(([value, label], i) => `<button class="seg-btn${i ? '' : ' active'}" type="button" data-click="profilesVoteFilter" data-arg="${_esc(value)}">${label}</button>`).join('')}</div>`)}
    <div class="pf-card-body">
      <div class="pf-vote-summary" id="pf-vote-summary">${pfSpinner('מחשב סיכום הצבעות…')}</div>
      <div class="pf-tools"><input type="text" class="field field--sm pf-search" placeholder="חיפוש הצעת חוק או הצבעה" data-input="profilesVoteSearch"></div>
      <div class="pf-result-count" id="pf-vote-count"></div>
      <div id="pf-vote-list">${pfSpinner('טוען הצבעות…')}</div>
      <div class="pf-center"><button class="btn btn-secondary btn-sm" type="button" id="pf-vote-more" data-click="profilesVoteMore" hidden>${pfIcon('expand_more')}הצגת עוד</button></div>
    </div></section>`;
  pfFetch(`${profile.api}/vote-summary`).then(summary => {
    if (profile !== _pf) return;
    document.getElementById('pf-vote-summary').innerHTML = summary.knessets.filter(k => k.votes_cast).map(k => {
      const total = k.votes_cast;
      return `<div class="pf-vote-split">
        <div class="pf-vs-label"><span>הכנסת ה-${k.knesset_num}</span><small>${pfNum(total)} הצבעות מתוך ${pfNum(k.plenum_votes)} · נוכחות ${pfPct(total, k.plenum_votes)}%</small></div>
        ${_pfVoteBar(k.by_result, total)}
        <div class="pf-vs-legend"><span><i style="background:var(--primary)"></i>בעד ${pfPct(k.by_result['בעד'], total)}%</span><span><i style="background:var(--error)"></i>נגד ${pfPct(k.by_result['נגד'], total)}%</span><span><i style="background:var(--warn-strong)"></i>נמנע או נוכח ${pfPct(k.by_result['נמנע'] + k.by_result.other, total)}%</span></div>
      </div>`;
    }).join('') || '<div class="pf-empty">אין הצבעות רשומות</div>';
  }).catch(err => { if (profile === _pf) document.getElementById('pf-vote-summary').innerHTML = pfErrorBox(err); });
  _pfLoadVotes(true);
}

async function _pfLoadVotes(reset) {
  const profile = _pf, state = profile.votes;
  if (state.loading) return;
  if (reset) state.rows = [];
  const params = new URLSearchParams({ offset: state.rows.length });
  if (state.q) params.set('q', state.q);
  const query = state.q;
  if (reset) document.getElementById('pf-vote-list').innerHTML = pfSpinner('טוען הצבעות…');
  state.loading = true;
  let page;
  try { page = await pfFetch(`${profile.api}/votes?${params}`); } catch (err) { if (profile === _pf) document.getElementById('pf-vote-list').innerHTML = pfErrorBox(err); return; } finally { state.loading = false; }
  if (profile !== _pf || query !== state.q) return;
  const seen = new Set(state.rows.map(v => v.vote_id));
  state.rows = state.rows.concat(page.votes.filter(v => !seen.has(v.vote_id)));
  state.total = page.total;
  state.exhausted = page.votes.length < page.page_size;
  _pfRenderVotes();
}

function _pfRenderVotes() {
  const state = _pf.votes;
  const matches = v => state.filter === 'all' || (state.filter === 'other' ? !['בעד', 'נגד'].includes(v.result) : v.result === state.filter);
  const groups = [];
  state.rows.forEach(v => {
    const title = (v.vote_title || '').trim(), day = (v.vote_datetime || '').slice(0, 10);
    const last = groups[groups.length - 1];
    if (last && last.title === title && last.day === day) last.items.push(v);
    else groups.push({ title, day, items: [v] });
  });
  groups.forEach(g => { g.items.reverse(); g.main = g.items.find(v => !v.vote_subject) || g.items[g.items.length - 1]; });
  const shown = groups.filter(g => g.items.some(matches));
  document.getElementById('pf-vote-count').textContent = `${pfNum(state.total)} הצבעות${state.q ? ' מתאימות' : ''} · נטענו ${pfNum(state.rows.length)}, מקובצות ל-${pfNum(groups.length)} נושאים`;
  document.getElementById('pf-vote-more').hidden = state.exhausted || state.rows.length >= (state.total || 0);
  document.getElementById('pf-vote-list').innerHTML = shown.length ? shown.map(g => {
    const counts = {};
    g.items.forEach(v => { counts[v.result] = (counts[v.result] || 0) + 1; });
    const aggregated = g.items.length > 1;
    const dots = aggregated ? `<span class="pf-vdots" title="${g.items.length} הצבעות בקבוצה">${g.items.filter(v => v !== g.main).slice(0, 3)
      .map(v => `<i class="${(VOTE_LOOK[v.result] || ['', 'v-absent'])[1]}"></i>`).join('')}</span>` : '';
    const head = `<div class="pf-vote">${pfDateCell(g.day)}
      <span class="pf-vchev">${aggregated ? pfIcon('unfold_more') : ''}</span>
      <div><div class="pf-vote-title">${pfHighlight(g.title, state.q)}</div>
      <div class="pf-vote-sub"><span>${g.main.vote_subject ? pfEsc(g.main.vote_subject) : 'הצבעה על ההצעה'}</span>${aggregated ? `<span class="pf-vcount">${g.items.length} הצבעות: ${Object.entries(counts).map(([r, n]) => `${n} ${pfEsc(r)}`).join(' · ')}</span>` : ''}${g.items.some(v => v.is_no_confidence) ? '<span class="pf-nc-tag">הצעת אי-אמון</span>' : ''}</div></div>
      <div class="pf-vresult">${_pfVotePill(g.main.result)}${dots}</div></div>`;
    if (g.items.length === 1) return `<div class="pf-vgroup">${head}</div>`;
    return `<details class="pf-vgroup"><summary>${head}</summary><div class="pf-vsubs">${g.items.map(v => `<div class="pf-vsub"><span>${pfEsc(v.vote_subject || 'הצבעה על ההצעה')} · ${(v.vote_datetime || '').slice(11, 16)}</span>${_pfVotePill(v.result)}</div>`).join('')}</div></details>`;
  }).join('') : '<div class="pf-empty">אין הצבעות שמתאימות</div>';
}

const _pfVoteSearch = pfDebounce(value => { _pf.votes.q = value; _pf.votes.loading = false; _pfLoadVotes(true); }, 450);
function profilesVoteSearch(input) { _pfVoteSearch(input.value.trim()); }
function profilesVoteFilter(button) {
  button.parentElement.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b === button));
  _pf.votes.filter = button.dataset.arg;
  _pfRenderVotes();
}
function profilesVoteMore() { _pfLoadVotes(false); }

/* ── bills ───────────────────────────────────────────────────────── */
async function _pfLoadBillsTab() {
  const profile = _pf;
  const panel = document.getElementById('pf-panel-bills');
  panel.innerHTML = `<section class="pf-card" id="pf-allies-card">${_pfCardHead('handshake', 'שותפים לחקיקה', 'מי חתמו איתו/ה הכי הרבה על הצעות חוק')}<div class="pf-card-body" id="pf-allies">${pfSpinner('סופר חותמים על כל ההצעות…')}</div></section>
    <section class="pf-card" id="pf-bills-card">${_pfCardHead('gavel', 'הצעות חוק <span class="pf-count" id="pf-bill-total"></span>', 'יזם/ה או הצטרף/ה, בכל הכנסות, מהעדכנית לישנה',
      `<div class="seg seg--sm">${[['', 'הכל'], ['initiator', 'יזם/ה'], ['joined', 'הצטרף/ה']].map(([value, label], i) => `<button class="seg-btn${i ? '' : ' active'}" type="button" data-click="profilesBillRole" data-arg="${_esc(value)}">${label}</button>`).join('')}</div>`)}
    <div class="pf-card-body">
      <div class="pf-tools"><input type="text" class="field field--sm pf-search" placeholder="חיפוש בשם ההצעה" data-input="profilesBillSearch">
        <div class="pf-pills">${BILL_STAGE_FILTERS.map(([value, label], i) => `<button class="pf-pill${i ? '' : ' on'}" type="button" data-click="profilesBillStage" data-arg="${_esc(value)}">${label}</button>`).join('')}</div></div>
      <div class="pf-result-count" id="pf-bill-count"></div>
      <div id="pf-bill-list">${pfSpinner('טוען הצעות חוק…')}</div>
      <div class="pf-center"><button class="btn btn-secondary btn-sm" type="button" id="pf-bill-more" data-click="profilesBillMore" hidden>${pfIcon('expand_more')}הצגת עוד</button></div>
    </div></section>`;
  pfFetch(`${profile.api}/cosponsors`).then(data => {
    if (profile !== _pf) return;
    document.getElementById('pf-allies').innerHTML = `<div class="pf-bill-stats"><span class="pf-tag">${pfNum(data.bills)} הצעות</span><span class="pf-tag pf-tag--mk">${pfNum(data.initiated)} כיוזם/ת</span><span class="pf-tag pf-tag--former">${pfNum(data.passed)} התקבלו כחוק</span></div>`
      + (data.cosponsors.length ? `<div class="pf-allies">${data.cosponsors.map(a => {
        const inner = `<div class="pf-avatar">${pfInitials(a.name)}</div><div class="pf-ally-info"><div class="pf-ally-name">${pfEsc(a.name)}</div><div class="pf-ally-sub">${a.party_name ? pfEsc(a.party_name) : 'לא מתמודד/ת'}</div></div><span class="pf-ally-num">${pfNum(a.shared_bills)} הצעות</span>`;
        return a.profile_url ? _pfLink(a.profile_url, inner, 'pf-ally') : `<div class="pf-ally">${inner}</div>`;
      }).join('')}</div>` : '<div class="pf-empty">אין חותמים משותפים</div>');
  }).catch(err => { if (profile === _pf) document.getElementById('pf-allies').innerHTML = pfErrorBox(err); });
  _pfLoadBills(true);
}

async function _pfLoadBills(reset) {
  const profile = _pf, state = profile.bills;
  if (reset) state.rows = [];
  const params = new URLSearchParams({ offset: state.rows.length });
  if (state.q) params.set('q', state.q);
  if (state.role) params.set('role', state.role);
  if (state.stage) params.set('stage', state.stage);
  const key = params.toString().replace(/offset=\d+&?/, '');
  state.key = key;
  if (reset) document.getElementById('pf-bill-list').innerHTML = pfSpinner('טוען הצעות חוק…');
  let page;
  try { page = await pfFetch(`${profile.api}/bills?${params}`); } catch (err) { if (profile === _pf) document.getElementById('pf-bill-list').innerHTML = pfErrorBox(err); return; }
  if (profile !== _pf || state.key !== key) return;
  state.rows = state.rows.concat(page.bills);
  state.total = page.total || 0;
  if (!state.q && !state.role && !state.stage) document.getElementById('pf-bill-total').textContent = pfNum(state.total);
  _pfRenderBills();
}

function _pfRenderBills() {
  const state = _pf.bills;
  document.getElementById('pf-bill-count').textContent = `${pfNum(state.total)} הצעות${state.q || state.role || state.stage ? ' מתאימות' : ''}`;
  document.getElementById('pf-bill-more').hidden = state.rows.length >= state.total;
  document.getElementById('pf-bill-list').innerHTML = state.rows.length ? state.rows.map(b => {
    const status = b.status || '';
    const passed = b.stage === 'passed', merged = status.startsWith('מוזגה'), dead = b.stage === 'stopped';
    const stage = BILL_STAGES_DONE[b.stage] || 1;
    const date = b.first_document_date || (b.last_updated || '').slice(0, 10);
    const government = (b.sub_type || '').includes('ממשלתית');
    const roleTag = `<span class="pf-role-tag${b.mk_is_initiator ? ' lead' : ''}">${b.mk_is_initiator ? 'יזם/ה' : 'הצטרף/ה'}</span>`
      + (government ? '<span class="pf-role-tag gov">ממשלתית</span>' : '');
    const initiators = b.initiators.filter(i => i.is_initiator).length;
    return `<div class="pf-bill"><div class="pf-date">${date ? `<b>${date.slice(5, 7)}.${date.slice(0, 4)}</b>${b.first_document_date ? 'הגשה' : 'עדכון'}` : ''}</div><div>
      <div class="pf-bill-title">${pfHighlight(b.name, state.q)}</div>
      <div class="pf-bill-meta"><span class="pf-k-tag">כנסת ${b.knesset_num}</span>${roleTag}${initiators ? `<span>${initiators} יוזמים</span>` : ''}${b.private_number ? `<span>פ/${b.private_number}/${b.knesset_num}</span>` : ''}</div>
      <div class="pf-stages" aria-hidden="true">${BILL_STAGES.map((_, i) => `<i class="${i < stage ? (dead ? 'dead' : 'done') : ''}"></i>`).join('')}</div>
      <div class="pf-stage-label${passed ? '' : merged ? ' merged' : dead ? ' dead' : ''}">${pfEsc(status)}</div>
      <div class="pf-bill-actions">
        <button class="pf-link-btn" type="button" data-click="profilesBillPeek" data-bill="${_esc(Number(b.bill_id))}" data-name="${_esc(b.name)}">${pfIcon('visibility')}הצצה בטקסט</button>
        <a class="pf-link-btn" href="${KNESSET_SITE_BILL_URL}${Number(b.bill_id)}" target="_blank" rel="noopener">${pfIcon('open_in_new')}באתר הכנסת</a>
      </div>
    </div></div>`;
  }).join('') : '<div class="pf-empty">אין הצעות שמתאימות</div>';
}

const _pfBillSearch = pfDebounce(value => { _pf.bills.q = value; _pfLoadBills(true); }, 450);
function profilesBillSearch(input) { _pfBillSearch(input.value.trim()); }
function profilesBillRole(button) {
  button.parentElement.querySelectorAll('.seg-btn').forEach(b => b.classList.toggle('active', b === button));
  _pf.bills.role = button.dataset.arg;
  _pfLoadBills(true);
}
function profilesBillStage(button) {
  button.parentElement.querySelectorAll('.pf-pill').forEach(b => b.classList.toggle('on', b === button));
  _pf.bills.stage = button.dataset.arg;
  _pfLoadBills(true);
}
function profilesBillMore() { _pfLoadBills(false); }

/* Bill text preview: a dialog over the profile, nothing else moves. */
let _pfBillDialog = null;
async function profilesBillPeek(button) {
  _pfBillDialog = { id: button.dataset.bill, name: button.dataset.name, offset: 0 };
  const overlay = document.getElementById('pf-bill-overlay');
  overlay.querySelector('h2').textContent = _pfBillDialog.name;
  overlay.querySelector('.pf-bill-text').innerHTML = pfSpinner('מחלץ את טקסט ההצעה מהמסמך…');
  overlay.querySelector('[data-pf-bill-more]').hidden = true;
  overlay.querySelector('[data-pf-bill-source]').hidden = true;
  overlay.classList.add('open');
  _pfLoadBillText();
}

async function _pfLoadBillText() {
  const dialog = _pfBillDialog;
  const overlay = document.getElementById('pf-bill-overlay');
  const textBox = overlay.querySelector('.pf-bill-text');
  const more = overlay.querySelector('[data-pf-bill-more]');
  more.disabled = true;
  let record;
  try { record = await pfFetch(`/api/profiles/bill/${Number(dialog.id)}/text?offset=${dialog.offset}`); } catch (err) { if (dialog === _pfBillDialog) textBox.innerHTML = pfErrorBox(err); return; } finally { more.disabled = false; }
  if (dialog !== _pfBillDialog) return;
  if (dialog.offset === 0) textBox.textContent = '';
  textBox.append(document.createTextNode(record.text));
  dialog.offset = record.text_chars[1];
  more.hidden = !record.truncated;
  const source = overlay.querySelector('[data-pf-bill-source]');
  source.href = record.url;
  source.hidden = false;
}

function profilesBillMoreText() { _pfLoadBillText(); }
function profilesCloseBill() { document.getElementById('pf-bill-overlay').classList.remove('open'); _pfBillDialog = null; }

/* ── roles ───────────────────────────────────────────────────────── */
const ROLE_RANK = { 'יו"ר ועדה': 3, 'חבר ועדה': 2, 'מ"מ חבר ועדה': 1 };
const JOINT_COMMITTEE_RE = /^(ה?וועדה המשותפת|ועדה משותפת|ה?וועדה המיוחדת|ועדת הכנסת המשותפת)/;
const pfMonthYear = iso => iso ? `${iso.slice(5, 7)}.${iso.slice(0, 4)}` : '';

function _pfRoleRow(icon, title, role, start, finish) {
  const period = start ? (finish ? `מ-${pfDmy(start)} עד ${pfDmy(finish)}` : `מ-${pfDmy(start)}`) : '';
  return `<div class="pf-role">${pfIcon(icon)}<div><b>${pfEsc(title)}</b>${role ? ` · ${pfEsc(role)}` : ''}${period ? `<small>${period}</small>` : ''}</div></div>`;
}

function _pfKnessetBlock(k, isCurrent) {
  if (k.error) return `<div class="pf-k-block"><div class="pf-k-head">הכנסת ה-${k.knesset_num}</div><div class="pf-error">${pfEsc(k.error)}</div></div>`;
  const merged = new Map();
  k.committee_positions.forEach(p => {
    const rank = ROLE_RANK[p.position] || 0;
    const known = merged.get(p.committee_name);
    if (!known || rank > known.rank) merged.set(p.committee_name, { ...p, rank });
  });
  const committees = [...merged.values()].sort((a, b) => b.rank - a.rank);
  const active = committees.filter(p => !p.finish_date || !isCurrent);
  const main = active.filter(p => !JOINT_COMMITTEE_RE.test(p.committee_name));
  const joint = active.filter(p => JOINT_COMMITTEE_RE.test(p.committee_name));
  const ended = isCurrent ? committees.filter(p => p.finish_date) : [];
  const dates = [...k.factions, ...k.committee_positions].map(p => p.start_date).filter(Boolean).sort();
  const row = p => _pfRoleRow(p.rank === 3 ? 'star' : 'groups', p.committee_name, p.position.replace(' ועדה', ''), p.start_date, p.finish_date);
  return `<div class="pf-k-block${isCurrent ? ' current' : ''}">
    <div class="pf-k-head">הכנסת ה-${k.knesset_num}${dates.length ? ` <small>מ-${pfMonthYear(dates[0])}</small>` : ''}</div>
    ${k.govministries.map(g => _pfRoleRow('account_balance', g.position_name || 'שר/ה', g.govministry_name, g.start_date, g.finish_date)).join('')}
    ${k.knesset_roles.map(r => _pfRoleRow('workspace_premium', r.position, '', r.start_date, r.finish_date)).join('')}
    ${k.faction_chairpersons.map(r => _pfRoleRow('flag', `יו"ר סיעת ${r.faction_name}`, '', r.start_date, r.finish_date)).join('')}
    ${k.factions.map(f => _pfRoleRow('how_to_vote', `סיעת ${f.faction_name}`, '', f.start_date, f.finish_date)).join('')}
    ${main.map(row).join('')}
    ${joint.length ? `<details class="pf-role-more"><summary>ועוד ${joint.length} ועדות משותפות ומיוחדות${pfIcon('expand_more')}</summary>${joint.map(row).join('')}</details>` : ''}
    ${ended.length ? `<details class="pf-role-more"><summary>${ended.length} תפקידים בוועדות שהסתיימו${pfIcon('expand_more')}</summary>${ended.map(row).join('')}</details>` : ''}
  </div>`;
}

async function _pfLoadRolesTab() {
  const profile = _pf;
  const panel = document.getElementById('pf-panel-roles');
  const attendance = profile.data.activity?.attendance;
  panel.innerHTML = `<section class="pf-card">${_pfCardHead('badge', 'תפקידים', 'סיעות, ועדות ותפקידים בכל הכנסות שכיהן/ה בהן')}<div class="pf-card-body" id="pf-roles">${pfSpinner('טוען תפקידים…')}</div></section>
    ${attendance && attendance.per_committee.length ? `<section class="pf-card">${_pfCardHead('event_available', 'נוכחות לפי ועדה', 'ישיבות בכנסת ה-25 שנכח/ה בהן, מתוך הפרוטוקולים')}<div class="pf-card-body">
      ${attendance.per_committee.slice(0, 10).map(([committee, count]) => `<div class="pf-att-row"><span title="${pfEsc(committee)}">${pfEsc(committee)}</span><div class="pf-att-bar"><i style="width:${count / attendance.per_committee[0][1] * 100}%"></i></div><b>${pfNum(count)}</b></div>`).join('')}
      <p class="pf-note">מספר הישיבות לפי רשימת הנוכחים בפרוטוקול. אחוז הנוכחות בראש העמוד מחושב רק על ועדות שהיה/תה חבר/ה בהן, בתקופת החברות.</p>
    </div></section>` : ''}`;
  try {
    const data = await pfFetch(`${profile.api}/roles`);
    if (profile !== _pf) return;
    document.getElementById('pf-roles').innerHTML = `<div class="pf-roles">${data.knessets.map((k, i) => _pfKnessetBlock(k, i === 0 && profile.data.candidate.profile === 'full')).join('')}</div>`;
  } catch (err) {
    if (profile === _pf) document.getElementById('pf-roles').innerHTML = pfErrorBox(err);
  }
}

/* ── outside clicks ──────────────────────────────────────────────── */
document.addEventListener('click', event => {
  if (!(event.target instanceof Element)) return;
  if (!event.target.closest('#pf-popup') && !event.target.closest('sup.pf-cite, .pf-spark i')) _pfHidePopup();
  if (event.target.id === 'pf-bill-overlay') profilesCloseBill();
  if (event.target.id === 'pf-theme-sheet') profilesCloseSheet();
});
document.addEventListener('keydown', event => { if (event.key === 'Escape') { _pfHidePopup(); profilesCloseBill(); profilesCloseSheet(); } });
document.addEventListener('scroll', event => { if (!(event.target instanceof Element && event.target.closest('#pf-popup'))) _pfHidePopup(); }, { capture: true, passive: true });

function profilesNav(link, event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  profilesGo(link.getAttribute('href'));
}
