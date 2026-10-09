/**
 * game.js — the "היכרות" tab (/game): MK themes with the names blacked out, swiped right (agree) or left
 * (disagree), then the parties the visitor agreed and disagreed with (web/game.py). The votes live in
 * localStorage, and the seen theme ids go with every request so only new themes come back.
 *
 * Non-module script, loaded after profiles.js (pfEsc, pfFetch, pfThemeCard, pfSetThemeSource, THEME_COLORS)
 * and before tabs.js / actions.js, which call gameShow and the game* PAGE_ACTIONS.
 */

const GAME_STORAGE_KEY = 'knessetGame';
const GAME_FIRST_RESULTS = 15;
const GAME_STRONG_RESULTS = 30;
const GAME_SWIPE_PX = 100;
const GAME_FLY_OUT_MS = 320;
const GAME_REDACTED = '⟦█⟧';
const GAME_VOTE_NO = 0;
const GAME_VOTE_YES = 1;
const GAME_VOTE_UNSURE = 2;

let _gm = null;
let _gmQueue = [];
let _gmFetching = null;
let _gmDone = false;
let _gmResults = null;

/* ── state ───────────────────────────────────────────────────────── */
function _gmLoad() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(GAME_STORAGE_KEY) || 'null'); } catch (exc) { console.warn('[game] stored game unreadable:', exc); }
  const votesOk = saved && Array.isArray(saved.votes) && saved.votes.every(v => Array.isArray(v) && Number.isInteger(v[0]) && v[0] > 0 && [GAME_VOTE_NO, GAME_VOTE_YES, GAME_VOTE_UNSURE].includes(v[1]));
  _gm = saved && saved.v === 1 && votesOk ? saved : { v: 1, votes: [] };
}

const _gmDecidedCount = () => _gm.votes.filter(([, vote]) => vote !== GAME_VOTE_UNSURE).length;

function _gmSave() {
  try { localStorage.setItem(GAME_STORAGE_KEY, JSON.stringify(_gm)); } catch (exc) { console.warn('[game] could not save the game:', exc); }
}

function _gmForget(dropped) {
  if (!dropped || !dropped.length) return;
  const gone = new Set(dropped);
  _gm.votes = _gm.votes.filter(([id]) => !gone.has(id));
  _gmSave();
}

async function _gmPost(url, body) {
  const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.message || `שגיאה ${response.status}`);
  return data;
}

/* ── views ───────────────────────────────────────────────────────── */
function _gmView(name) {
  ['intro', 'play', 'results'].forEach(view => { document.getElementById(`gm-${view}`).hidden = view !== name; });
  document.getElementById('tab-game').scrollTop = 0;
}

function gameShow() {
  if (!_gm) _gmLoad();
  document.getElementById('gm-start-label').textContent = _gm.votes.length ? 'להמשיך לשחק' : 'בואו נתחיל';
  _gmView('intro');
}

function gameStart() { gameContinue(); }

function gameContinue() {
  _gmView('play');
  _gmRenderBottom();
  _gmRenderStack();
  _gmFill();
}

function gameHome() { switchTab('home'); }

/* ── cards ───────────────────────────────────────────────────────── */
const _gmRedacted = text => pfEsc(text).replaceAll(GAME_REDACTED, '<span class="gm-redact"></span>');

function _gmCardHtml(card) {
  const quotes = card.evidence.map(e => `<figure class="gm-quote">
      <blockquote>${_gmRedacted(e.quote || e.opinion)}</blockquote>
      <figcaption>${pfEsc(e.committee)} · ${pfDmy(e.date)}</figcaption>
    </figure>`).join('');
  return `<article class="gm-card" data-id="${_esc(card.id)}">
    <span class="gm-stamp gm-stamp--yes">${pfIcon('favorite')}התחברתי</span>
    <span class="gm-stamp gm-stamp--no">${pfIcon('heart_broken')}לא התחברתי</span>
    <h3>${_gmRedacted(card.title)}</h3>
    <p class="gm-summary">${_gmRedacted(card.summary)}</p>
    ${quotes ? `<div class="gm-quotes">${quotes}</div>` : ''}
  </article>`;
}

function _gmRenderStack() {
  const stack = document.getElementById('gm-stack');
  const live = [...stack.querySelectorAll('.gm-card:not(.gm-out)')];
  live.filter(el => !_gmQueue.slice(0, 2).some(card => String(card.id) === el.dataset.id)).forEach(el => el.remove());
  _gmQueue.slice(0, 2).forEach((card, i) => {
    let el = live.find(e => e.dataset.id === String(card.id));
    if (!el) {
      stack.insertAdjacentHTML('beforeend', _gmCardHtml(card));
      el = stack.lastElementChild;
      _gmBindDrag(el);
    }
    el.classList.toggle('gm-card--top', i === 0);
    el.classList.toggle('gm-card--next', i === 1);
  });
  const empty = document.getElementById('gm-stack-empty');
  empty.hidden = _gmQueue.length > 0;
  empty.innerHTML = _gmDone
    ? `${pfIcon('celebration')}<p>ראית את כל הנושאים שיש לנו.</p><button class="btn btn-primary" type="button" data-click="gameResults">${pfIcon('pie_chart')}לתוצאות</button>`
    : pfSpinner('טוען כרטיס…');
  document.querySelectorAll('.gm-vote-btn').forEach(button => { button.disabled = !_gmQueue.length; });
}

async function _gmFill() {
  if (_gmFetching || _gmDone || _gmQueue.length >= 2) return;
  const seen = [..._gm.votes.map(([id]) => id), ..._gmQueue.map(card => card.id)];
  _gmFetching = _gmPost('/api/game/next', { seen, count: 2 });
  let body;
  try { body = await _gmFetching; } catch (err) {
    document.getElementById('gm-stack-empty').innerHTML = pfErrorBox(err);
    return;
  } finally { _gmFetching = null; }
  _gmForget(body.dropped);
  const known = new Set(seen);
  _gmQueue.push(...body.cards.filter(card => !known.has(card.id)));
  if (!body.cards.length) _gmDone = true;
  _gmRenderStack();
  _gmFill();
}

function _gmBindDrag(card) {
  let startX = null, dx = 0;
  card.addEventListener('pointerdown', event => {
    if (event.button || !card.classList.contains('gm-card--top')) return;
    startX = event.clientX;
    dx = 0;
    card.setPointerCapture(event.pointerId);
    card.classList.add('gm-dragging');
  });
  card.addEventListener('pointermove', event => {
    if (startX == null) return;
    dx = event.clientX - startX;
    card.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
    card.style.setProperty('--gm-yes', Math.max(0, Math.min(1, dx / GAME_SWIPE_PX)));
    card.style.setProperty('--gm-no', Math.max(0, Math.min(1, -dx / GAME_SWIPE_PX)));
  });
  const end = () => {
    if (startX == null) return;
    startX = null;
    card.classList.remove('gm-dragging');
    if (Math.abs(dx) >= GAME_SWIPE_PX) { _gmVote(dx > 0 ? GAME_VOTE_YES : GAME_VOTE_NO); return; }
    card.style.transform = '';
    card.style.removeProperty('--gm-yes');
    card.style.removeProperty('--gm-no');
  };
  card.addEventListener('pointerup', end);
  card.addEventListener('pointercancel', end);
}

function _gmVote(vote) {
  const card = document.querySelector('#gm-stack .gm-card--top:not(.gm-out)');
  if (!card) return;
  const id = Number(card.dataset.id);
  _gm.votes.push([id, vote]);
  _gmSave();
  card.classList.remove('gm-card--top');
  card.classList.add('gm-out', { [GAME_VOTE_YES]: 'gm-out--yes', [GAME_VOTE_NO]: 'gm-out--no', [GAME_VOTE_UNSURE]: 'gm-out--unsure' }[vote]);
  card.style.transform = '';
  setTimeout(() => card.remove(), GAME_FLY_OUT_MS);
  _gmQueue = _gmQueue.filter(c => c.id !== id);
  _gmRenderBottom();
  _gmRenderStack();
  _gmFill();
}

function gameVote(button) { _gmVote(Number(button.dataset.arg)); }

function _gmRenderBottom() {
  const votes = _gmDecidedCount();
  const bar = (done, goal, label) => `<div class="gm-progress"><div class="gm-progress-track"><i style="width:${Math.min(100, done / goal * 100)}%"></i></div><span>${label}</span></div>`;
  const button = label => `<button class="btn btn-primary gm-results-btn" type="button" data-click="gameResults">${pfIcon('pie_chart')}${label}</button>`;
  let html;
  if (votes < GAME_FIRST_RESULTS) html = bar(votes, GAME_FIRST_RESULTS, `${votes}/${GAME_FIRST_RESULTS}`);
  else if (votes < GAME_STRONG_RESULTS) html = button('תוצאות ראשוניות') + bar(votes - GAME_FIRST_RESULTS, GAME_STRONG_RESULTS - GAME_FIRST_RESULTS, `${votes}/${GAME_STRONG_RESULTS}`);
  else html = button('תוצאות חזקות');
  document.getElementById('gm-bottom').innerHTML = html;
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') { gameCloseTheme(); return; }
  if (_activeTab !== 'game' || document.getElementById('gm-play').hidden || document.getElementById('gm-theme-overlay').classList.contains('open')) return;
  if (event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable]')) return;
  const vote = { ArrowRight: GAME_VOTE_YES, ArrowLeft: GAME_VOTE_NO, ArrowDown: GAME_VOTE_UNSURE }[event.key];
  if (vote === undefined) return;
  event.preventDefault();
  _gmVote(vote);
});

/* ── results ─────────────────────────────────────────────────────── */
const _gmPartyColor = partyId => THEME_COLORS[partyId % THEME_COLORS.length];

async function gameResults() {
  _gmView('results');
  const root = document.getElementById('gm-results');
  root.innerHTML = `<div class="gm-results-inner">${pfSpinner('מחשב תוצאות…')}</div>`;
  const agreed = _gm.votes.filter(([, vote]) => vote === GAME_VOTE_YES).map(([id]) => id);
  const disagreed = _gm.votes.filter(([, vote]) => vote === GAME_VOTE_NO).map(([id]) => id);
  try { _gmResults = await _gmPost('/api/game/results', { agreed, disagreed }); } catch (err) {
    root.innerHTML = `<div class="gm-results-inner">${pfErrorBox(err)}${_gmResultsActions()}</div>`;
    return;
  }
  _gmForget(_gmResults.dropped);
  _gmRenderResults();
}

function _gmArc(start, end, radius) {
  const point = angle => [radius * Math.sin(angle * Math.PI / 180), -radius * Math.cos(angle * Math.PI / 180)].map(n => n.toFixed(2)).join(' ');
  return `M0 0 L${point(start)} A${radius} ${radius} 0 ${end - start > 180 ? 1 : 0} 1 ${point(end)} Z`;
}

/* One half-pie: the right half (0°→180°, clockwise from the top) for agreed, the left for disagreed. */
function _gmHalfPie(side, parties) {
  const key = side === 'agreed' ? 'agreed' : 'disagreed';
  const from = side === 'agreed' ? 0 : 180;
  const rows = parties.filter(p => p[key].length);
  const total = rows.reduce((sum, p) => sum + p[key].length, 0);
  const shift = side === 'agreed' ? 'translate(3 0)' : 'translate(-3 0)';
  if (!total) return `<g transform="${shift}"><path class="gm-slice gm-slice--empty" d="${_gmArc(from, from + 180, 100)}"/></g>`;
  let angle = from;
  return `<g transform="${shift}">${rows.map(p => {
    const sweep = p[key].length / total * 180;
    const path = `<path class="gm-slice" d="${_gmArc(angle, angle + sweep, 100)}" style="--pc:${_gmPartyColor(p.party.id)}" data-click="gameSlice" data-arg="${_esc(`${side}:${p.party.id}`)}"><title>${pfEsc(p.party.name)} · ${p[key].length}</title></path>`;
    angle += sweep;
    return path;
  }).join('')}</g>`;
}

function _gmResultsActions() {
  return `<a class="gm-cta" href="${PROFILES_PATH}" data-click="gameOpenProfile">
      <span class="gm-cta-icon">${pfIcon('groups')}</span>
      <span><b>להכיר אותם לעומק</b><span>בפרופילים יש את כל הנושאים, ההצבעות והצעות החוק של כל מועמד/ת.</span></span>
      ${pfIcon('arrow_back')}
    </a>
    <div class="gm-results-actions">
      <button class="btn btn-primary" type="button" data-click="gameContinue">${pfIcon('style')}להמשיך לשחק</button>
      <button class="btn btn-secondary" type="button" data-click="gameHome">${pfIcon('home')}חזרה לדף הבית</button>
    </div>`;
}

function _gmRenderResults() {
  const { parties, best_party_id: bestId } = _gmResults;
  const votes = _gmDecidedCount();
  const best = parties.find(p => p.party.id === bestId);
  const headline = best
    ? `<h2>הסכמת הכי הרבה עם <a href="${_esc(`${PROFILES_PATH}/party/${best.party.id}`)}" data-click="gameOpenProfile">${pfEsc(best.party.name)}</a></h2>`
    : '<h2>עוד לא הסכמת עם אף נושא</h2>';
  const legend = side => parties.filter(p => p[side].length).map(p =>
    `<button type="button" class="gm-legend-item" style="--pc:${_gmPartyColor(p.party.id)}" data-click="gameSlice" data-arg="${_esc(`${side}:${p.party.id}`)}"><i></i>${pfEsc(p.party.name)}<b>${p[side].length}</b></button>`).join('')
    || '<span class="gm-legend-none">אין</span>';
  document.getElementById('gm-results').innerHTML = `<div class="gm-results-inner">
    <div class="gm-results-head">
      <div class="gm-eyebrow">${votes >= GAME_STRONG_RESULTS ? 'תוצאות חזקות' : 'תוצאות ראשוניות'} · מתוך ${pfNum(votes)} נושאים</div>
      ${best?.party.logo_url ? `<img class="gm-best-logo" src="${pfEsc(best.party.logo_url)}" alt="" data-hide-on-error>` : ''}
      ${headline}
    </div>
    <div class="gm-pie-wrap">
      <span class="gm-pie-label gm-pie-label--yes">${pfIcon('favorite')}הסכמת</span>
      <svg class="gm-pie" viewBox="-110 -105 220 210" aria-hidden="true">${_gmHalfPie('agreed', parties)}${_gmHalfPie('disagreed', parties)}</svg>
      <span class="gm-pie-label gm-pie-label--no">${pfIcon('close')}לא הסכמת</span>
    </div>
    <div class="gm-legends"><div class="gm-legend">${legend('agreed')}</div><div class="gm-legend">${legend('disagreed')}</div></div>
    <div class="gm-slice-panel" id="gm-slice-panel"></div>
    ${_gmResultsActions()}
  </div>`;
  const first = best ? `agreed:${best.party.id}` : (parties[0] ? `disagreed:${parties[0].party.id}` : null);
  if (first) _gmShowSlice(first);
}

function gameSlice(el) { _gmShowSlice(el.dataset.arg); }

function _gmShowSlice(arg) {
  const [side, partyId] = arg.split(':');
  const party = _gmResults.parties.find(p => String(p.party.id) === partyId);
  if (!party) return;
  document.querySelectorAll('#gm-results [data-click="gameSlice"]').forEach(el => el.classList.toggle('on', el.dataset.arg === arg));
  const refs = party[side];
  document.getElementById('gm-slice-panel').innerHTML = `
    <div class="gm-slice-head" style="--pc:${_gmPartyColor(party.party.id)}"><i></i>${side === 'agreed' ? 'הסכמת עם' : 'לא הסכמת עם'} ${pfNum(refs.length)} ${refs.length === 1 ? 'נושא' : 'נושאים'} של
      <a href="${_esc(`${PROFILES_PATH}/party/${party.party.id}`)}" data-click="gameOpenProfile">${pfEsc(party.party.name)}</a></div>
    <ul class="gm-slice-list">${refs.map(ref => `<li>
      <button class="gm-theme-link" type="button" data-click="gameTheme" data-arg="${_esc(ref.theme_id)}">${pfEsc(ref.title)}${pfIcon('chevron_left')}</button>
      <a class="gm-who" href="${pfEsc(ref.candidate.profile_url)}" data-click="gameOpenProfile">${pfAvatar(ref.candidate.photo_url, ref.candidate.name)}<span>${pfEsc(ref.candidate.name)}</span></a>
    </li>`).join('')}</ul>`;
}

function _gmRef(themeId) {
  for (const party of _gmResults?.parties || []) {
    const ref = [...party.agreed, ...party.disagreed].find(r => String(r.theme_id) === String(themeId));
    if (ref) return { ref, party: party.party };
  }
  return null;
}

/* The full theme card, as on the profile page, in a dialog (its cites and "all opinions" sheet included). */
async function gameTheme(button) {
  const overlay = document.getElementById('gm-theme-overlay');
  const body = overlay.querySelector('.gm-theme-body');
  const found = _gmRef(button.dataset.arg);
  overlay.querySelector('.gm-theme-who').innerHTML = found
    ? `<a class="gm-who" href="${pfEsc(found.ref.candidate.profile_url)}" data-click="gameOpenProfile">${pfAvatar(found.ref.candidate.photo_url, found.ref.candidate.name)}<span><b>${pfEsc(found.ref.candidate.name)}</b>${pfEsc(found.party.name)}</span></a>`
    : '';
  body.innerHTML = pfSpinner('טוען נושא…');
  overlay.classList.add('open');
  let data;
  try { data = await pfFetch(`/api/profiles/theme/${encodeURIComponent(button.dataset.arg)}`); } catch (err) { body.innerHTML = pfErrorBox(err); return; }
  const theme = { ...data.theme, color: THEME_COLORS[data.color_index % THEME_COLORS.length] };
  const evidence = {};
  theme.evidence.forEach((e, n) => { evidence[`${theme.id}-${n}`] = e; });
  pfSetThemeSource('game', { themes: [theme], quarters: data.quarters, evidence, api: `/api/profiles/party/${data.party_id}/candidate/${data.candidate_id}` });
  body.innerHTML = pfThemeCard(theme).replace('class="pf-theme"', 'class="pf-theme open"').replace('aria-expanded="false"', 'aria-expanded="true"');
}

function gameCloseTheme() {
  const overlay = document.getElementById('gm-theme-overlay');
  if (!overlay || !overlay.classList.contains('open')) return;
  overlay.classList.remove('open');
  _pfHidePopup();
}

function gameOpenProfile(link, event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  gameCloseTheme();
  profilesGo(link.getAttribute('href'));
  switchTab('profiles', { writeUrl: false });
}
