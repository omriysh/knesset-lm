/**
 * game.js — the "היכרות" tab (/game): MK themes with the names blacked out, swiped right (agree) or left
 * (disagree), then the parties the visitor agreed and disagreed with (web/game.py). The votes live in
 * localStorage, and the seen theme ids go with every request so only new themes come back.
 *
 * Non-module script, loaded after profiles.js (pfEsc, pfFetch, pfThemeCard, pfSetThemeSource, THEME_COLORS)
 * and before tabs.js / actions.js, which call gameShow and the game* PAGE_ACTIONS.
 */

const GAME_STORAGE_KEY = 'knessetGame';
const GAME_PREFS_KEY = 'knessetGamePrefs';
const GAME_PRIOR_MATCHES = 3;
const GAME_FIRST_RESULTS = 15;
const GAME_STRONG_RESULTS = 30;
const GAME_SWIPE_PX = 100;
const GAME_TAP_PX = 6;
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
let _gmPrefs = null;
let _gmPartyList = null;
let _gmQueueGeneration = 0;

/* ── state ───────────────────────────────────────────────────────── */
function _gmLoad() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(GAME_STORAGE_KEY) || 'null'); } catch (exc) { console.warn('[game] stored game unreadable:', exc); }
  const votesOk = saved && Array.isArray(saved.votes) && saved.votes.every(v => Array.isArray(v) && Number.isInteger(v[0]) && v[0] > 0 && [GAME_VOTE_NO, GAME_VOTE_YES, GAME_VOTE_UNSURE].includes(v[1]));
  _gm = saved && saved.v === 1 && votesOk ? saved : { v: 1, votes: [] };
}

const _gmDecidedCount = () => _gm.votes.filter(([, vote]) => vote !== GAME_VOTE_UNSURE).length;

/* Kept apart from the votes: the parties dealt in the game and the parties shown in the results (null = every
   party), and whether results are normalized. */
function _gmGetPrefs() {
  if (_gmPrefs) return _gmPrefs;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(GAME_PREFS_KEY) || 'null'); } catch (exc) { console.warn('[game] stored preferences unreadable:', exc); }
  const ids = list => Array.isArray(list) && list.length && list.every(Number.isInteger) ? list : null;
  _gmPrefs = { normalized: Boolean(saved && saved.normalized), play: ids(saved?.play), show: ids(saved?.show) };
  return _gmPrefs;
}

function _gmSavePrefs() {
  try { localStorage.setItem(GAME_PREFS_KEY, JSON.stringify(_gmPrefs)); } catch (exc) { console.warn('[game] could not save the preferences:', exc); }
}

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
  const quotes = card.evidence.map((e, i) => `<figure class="gm-quote${i ? '' : ' on'}">
      <blockquote>${_gmRedacted(e.quote || e.opinion)}</blockquote>
      <figcaption>${pfEsc(e.committee)} · ${pfDmy(e.date)}</figcaption>
    </figure>`).join('');
  const quoteCount = card.evidence.length > 1 ? `<span class="gm-quote-count">${pfIcon('format_quote')}<span>ציטוט 1 מתוך ${card.evidence.length}</span> · לחצו לציטוט הבא</span>` : '';
  return `<article class="gm-card" data-id="${_esc(card.id)}">
    <span class="gm-stamp gm-stamp--yes">${pfIcon('favorite')}התחברתי</span>
    <span class="gm-stamp gm-stamp--no">${pfIcon('heart_broken')}לא התחברתי</span>
    <h3>${_gmRedacted(card.title)}</h3>
    <p class="gm-summary">${_gmRedacted(card.summary)}</p>
    ${quotes ? `<div class="gm-quotes${quoteCount ? ' gm-quotes--more' : ''}">${quotes}${quoteCount}</div>` : ''}
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
  document.getElementById('gm-info-btn').disabled = !_gmQueue.length;
}

async function _gmFill() {
  if (_gmFetching || _gmDone || _gmQueue.length >= 2) return;
  const seen = [..._gm.votes.map(([id]) => id), ..._gmQueue.map(card => card.id)];
  const generation = _gmQueueGeneration;
  _gmFetching = _gmPost('/api/game/next', { seen, count: 2, parties: _gmGetPrefs().play });
  let body;
  try { body = await _gmFetching; } catch (err) {
    document.getElementById('gm-stack-empty').innerHTML = pfErrorBox(err);
    return;
  } finally { _gmFetching = null; }
  if (generation !== _gmQueueGeneration) { _gmFill(); return; }
  _gmForget(body.dropped);
  const known = new Set(seen);
  _gmQueue.push(...body.cards.filter(card => !known.has(card.id)));
  if (!body.cards.length) _gmDone = true;
  _gmRenderStack();
  _gmFill();
}

/* The card's quotes show one at a time; a tap on them brings the next one. */
function _gmNextQuote(card) {
  const quotes = [...card.querySelectorAll('.gm-quote')];
  if (quotes.length < 2) return;
  const next = (quotes.findIndex(quote => quote.classList.contains('on')) + 1) % quotes.length;
  quotes.forEach((quote, i) => quote.classList.toggle('on', i === next));
  card.querySelector('.gm-quote-count span:last-child').textContent = `ציטוט ${next + 1} מתוך ${quotes.length}`;
}

function _gmBindDrag(card) {
  let startX = null, dx = 0, onQuotes = false;
  card.addEventListener('pointerdown', event => {
    if (event.button || !card.classList.contains('gm-card--top')) return;
    startX = event.clientX;
    dx = 0;
    onQuotes = event.target instanceof Element && Boolean(event.target.closest('.gm-quotes'));
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
  const end = event => {
    if (startX == null) return;
    startX = null;
    card.classList.remove('gm-dragging');
    if (Math.abs(dx) >= GAME_SWIPE_PX) { _gmVote(dx > 0 ? GAME_VOTE_YES : GAME_VOTE_NO); return; }
    if (event.type === 'pointerup' && onQuotes && Math.abs(dx) < GAME_TAP_PX) _gmNextQuote(card);
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

/* The top card's subject: its background and approaches, the card's own approach marked. */
async function gameInfo() {
  const card = _gmQueue[0];
  if (!card) return;
  const overlay = document.getElementById('gm-info-overlay');
  const body = overlay.querySelector('.gm-info-body');
  overlay.querySelector('.gm-info-title').innerHTML = `<small>${pfEsc(card.subject.umbrella)}</small><b>${pfEsc(card.subject.name)}</b>`;
  body.innerHTML = pfSpinner('טוען…');
  overlay.classList.add('open');
  let subject;
  try { subject = await pfFetch(`/api/subjects/${encodeURIComponent(card.subject.id)}`); } catch (err) { body.innerHTML = pfErrorBox(err); return; }
  if (_gmQueue[0] !== card) return;
  body.innerHTML = `
    <div class="pf-ai-band">${pfIcon('auto_awesome')}<span>נוצר על ידי AI מעמדות חברי הכנסת בדיונים</span></div>
    <h3 class="gm-info-head">${pfIcon('history_edu')}רקע</h3>
    <p class="gm-info-text">${pfEsc(subject.background)}</p>
    <h3 class="gm-info-head">${pfIcon('alt_route')}הגישות השונות</h3>
    <ol class="gm-approaches">${subject.approaches.map(approach => `<li class="${approach.id === card.approach_id ? 'on' : ''}">
      <b>${pfEsc(approach.name)}</b>${approach.id === card.approach_id ? '<span class="gm-approach-tag">הגישה בכרטיס</span>' : ''}
      <p>${pfEsc(approach.summary)}</p></li>`).join('')}</ol>`;
}

function gameCloseInfo() {
  const overlay = document.getElementById('gm-info-overlay');
  if (overlay) overlay.classList.remove('open');
}

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
  if (event.key === 'Escape') { gameCloseTheme(); gameCloseInfo(); gameCloseParties(); return; }
  if (_activeTab !== 'game' || document.getElementById('gm-play').hidden || document.querySelector('#gm-theme-overlay.open, #gm-info-overlay.open, #gm-parties-overlay.open')) return;
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

const _gmShownParties = () => {
  const chosen = _gmGetPrefs().show;
  return chosen ? _gmResults.parties.filter(p => chosen.includes(p.party.id)) : _gmResults.parties;
};

/* Normalized: the share of a party's matches that went that way, pulled toward the visitor's overall rate by
   GAME_PRIOR_MATCHES imaginary matches, so a party met once can't top the chart with 100%. */
function _gmScorer(parties) {
  if (!_gmGetPrefs().normalized) return (party, side) => party[side].length;
  const agreed = parties.reduce((sum, p) => sum + p.agreed.length, 0);
  const matches = parties.reduce((sum, p) => sum + p.agreed.length + p.disagreed.length, 0);
  const agreedRate = matches ? agreed / matches : 0.5;
  return (party, side) => (party[side].length + GAME_PRIOR_MATCHES * (side === 'agreed' ? agreedRate : 1 - agreedRate))
    / (party.agreed.length + party.disagreed.length + GAME_PRIOR_MATCHES);
}

const _gmRanked = (parties, side, score) => parties.filter(p => p[side].length).sort((a, b) => score(b, side) - score(a, side));
const _gmScoreLabel = (party, side, score) => _gmGetPrefs().normalized ? `${Math.round(100 * score(party, side))}%` : pfNum(party[side].length);

function _gmBest() {
  const parties = _gmShownParties();
  return _gmRanked(parties, 'agreed', _gmScorer(parties))[0];
}

/* One half-pie: the right half (0°→180°, clockwise from the top) for agreed, the left for disagreed. */
function _gmHalfPie(side, parties, score) {
  const key = side === 'agreed' ? 'agreed' : 'disagreed';
  const from = side === 'agreed' ? 0 : 180;
  const rows = _gmRanked(parties, key, score);
  const total = rows.reduce((sum, p) => sum + score(p, key), 0);
  const shift = side === 'agreed' ? 'translate(3 0)' : 'translate(-3 0)';
  if (!total) return `<g transform="${shift}"><path class="gm-slice gm-slice--empty" d="${_gmArc(from, from + 180, 100)}"/></g>`;
  let angle = from;
  return `<g transform="${shift}">${rows.map(p => {
    const sweep = score(p, key) / total * 180;
    const path = `<path class="gm-slice" d="${_gmArc(angle, angle + sweep, 100)}" style="--pc:${_gmPartyColor(p.party.id)}" data-click="gameSlice" data-arg="${_esc(`${side}:${p.party.id}`)}"><title>${pfEsc(p.party.name)} · ${_gmScoreLabel(p, key, score)}</title></path>`;
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
  const parties = _gmShownParties();
  const score = _gmScorer(parties);
  const { normalized, show: chosen } = _gmGetPrefs();
  const votes = _gmDecidedCount();
  const best = _gmBest();
  const headline = best
    ? `<h2>הסכמת הכי הרבה עם <a href="${_esc(`${PROFILES_PATH}/party/${best.party.id}`)}" data-click="gameOpenProfile">${pfEsc(best.party.name)}</a></h2>`
    : '<h2>עוד לא הסכמת עם אף נושא</h2>';
  const legend = side => _gmRanked(parties, side, score).map(p =>
    `<button type="button" class="gm-legend-item" style="--pc:${_gmPartyColor(p.party.id)}" data-click="gameSlice" data-arg="${_esc(`${side}:${p.party.id}`)}"><i></i>${pfEsc(p.party.name)}<b>${_gmScoreLabel(p, side, score)}</b></button>`).join('')
    || '<span class="gm-legend-none">אין</span>';
  document.getElementById('gm-results').innerHTML = `<div class="gm-results-inner">
    <div class="gm-results-head">
      <div class="gm-eyebrow">${votes >= GAME_STRONG_RESULTS ? 'תוצאות חזקות' : 'תוצאות ראשוניות'} · מתוך ${pfNum(votes)} נושאים</div>
      ${best?.party.logo_url ? `<img class="gm-best-logo" src="${pfEsc(best.party.logo_url)}" alt="" data-hide-on-error>` : ''}
      ${headline}
      <p class="gm-disclaimer">${pfIcon('info')}<span>זו לא המלצה להצבעה. התוצאות מבוססות רק על מי שכיהנו כחברי וחברות הכנסת ה-25, לחצו על מפלגה כדי לקרוא עוד.</span></p>
      <div class="gm-results-tools">
        <button type="button" class="gm-tool${normalized ? ' on' : ''}" data-click="gameNormalize" aria-pressed="${normalized}">${pfIcon('balance')}לפי אחוז הסכמה</button>
        <button type="button" class="gm-tool${chosen ? ' on' : ''}" data-click="gameChooseParties" data-arg="show">${pfIcon('filter_list')}${_gmPartiesLabel('show')}</button>
        ${best ? `<button type="button" class="gm-tool" data-click="gameShare">${pfIcon('share')}שיתוף התוצאה</button>` : ''}
      </div>
    </div>
    <div class="gm-pie-wrap">
      <span class="gm-pie-label gm-pie-label--yes">${pfIcon('favorite')}הסכמת</span>
      <svg class="gm-pie" viewBox="-110 -105 220 210" aria-hidden="true">${_gmHalfPie('agreed', parties, score)}${_gmHalfPie('disagreed', parties, score)}</svg>
      <span class="gm-pie-label gm-pie-label--no">${pfIcon('heart_broken')}לא הסכמת</span>
    </div>
    <div class="gm-legends"><div class="gm-legend">${legend('agreed')}</div><div class="gm-legend">${legend('disagreed')}</div></div>
    <div class="gm-slice-panel" id="gm-slice-panel"></div>
    ${_gmResultsActions()}
  </div>`;
  const first = best ? `agreed:${best.party.id}` : (parties[0] ? `disagreed:${parties[0].party.id}` : null);
  if (first) _gmShowSlice(first);
}

function gameSlice(el) { _gmShowSlice(el.dataset.arg); }

function gameNormalize() { _gmOpenParties('results'); }

function gameSetNormalized(on) {
  _gmGetPrefs().normalized = on;
  _gmSavePrefs();
  _gmPrefsChanged(false);
}

async function gameShare() {
  const best = _gmBest();
  if (!best) return;
  const url = `${location.origin}${GAME_PATH}`;
  const text = `במשחק ההיכרות של מעורב ירושלמי הסכמתי הכי הרבה עם ${best.party.name}. ועם מי ההתאמה שלך? בואו לשחק:`;
  if (navigator.share) {
    try { await navigator.share({ title: document.title, text, url }); } catch (exc) {
      if (exc.name !== 'AbortError') console.error('[game] native share failed:', exc);
    }
    return;
  }
  try {
    await navigator.clipboard.writeText(`${text} ${url}`);
    showToast('הקישור הועתק');
  } catch (exc) {
    console.error('[game] clipboard write failed:', exc);
    window.prompt('העתקת הקישור:', `${text} ${url}`);
  }
}

function _gmShowSlice(arg) {
  const [side, partyId] = arg.split(':');
  const party = _gmShownParties().find(p => String(p.party.id) === partyId);
  if (!party) return;
  document.querySelectorAll('#gm-results [data-click="gameSlice"]').forEach(el => el.classList.toggle('on', el.dataset.arg === arg));
  const refs = party[side];
  document.getElementById('gm-slice-panel').innerHTML = `
    <div class="gm-slice-head" style="--pc:${_gmPartyColor(party.party.id)}"><i></i>${side === 'agreed' ? 'הסכמת עם' : 'לא הסכמת עם'} ${pfNum(refs.length)} ${refs.length === 1 ? 'נושא' : 'נושאים'} של
      <a href="${_esc(`${PROFILES_PATH}/party/${party.party.id}`)}" data-click="gameOpenProfile">${pfEsc(party.party.name)}</a></div>
    <ul class="gm-slice-list">${refs.map(ref => `<li>
      <div><button class="gm-theme-link" type="button" data-click="gameTheme" data-arg="${_esc(ref.theme_id)}">${pfEsc(ref.title)}${pfIcon('chevron_left')}</button>
        ${ref.via_approach ? `<small class="gm-via">${pfIcon('alt_route')}באותה גישה: ${pfEsc(ref.via_approach)}</small>` : ''}</div>
      <a class="gm-who" href="${pfEsc(ref.candidate.profile_url)}" data-click="gameOpenProfile">${pfAvatar(ref.candidate.photo_url, ref.candidate.name)}<span>${pfEsc(ref.candidate.name)}</span></a>
    </li>`).join('')}</ul>`;
}

/* ── which parties: dealt in the game (play) and shown in the results (show) ── */
function _gmPartiesLabel(key) {
  const chosen = _gmGetPrefs()[key];
  return chosen ? `${pfNum(chosen.length)} ${chosen.length === 1 ? 'מפלגה' : 'מפלגות'}` : 'כל המפלגות';
}

function gameSettingsRefresh() {
  document.getElementById('toggle-game-normalize').checked = _gmGetPrefs().normalized;
  document.getElementById('game-play-status').textContent = _gmGetPrefs().play ? `נושאים רק של ${_gmPartiesLabel('play')}` : 'נושאים של כל המפלגות';
  document.getElementById('game-show-status').textContent = _gmGetPrefs().show ? `רק ${_gmPartiesLabel('show')}` : 'כל המפלגות';
}

/* After a change: results redraw, and when the dealt parties changed, cards of the old choice leave the queue. */
function _gmPrefsChanged(playChanged) {
  if (playChanged) {
    _gmQueue = [];
    _gmDone = false;
    _gmQueueGeneration++;
    if (_gm && !document.getElementById('gm-play').hidden) { _gmRenderStack(); _gmFill(); }
  }
  if (_gmResults && !document.getElementById('gm-results').hidden) _gmRenderResults();
  if (document.getElementById('settings-overlay').classList.contains('open')) gameSettingsRefresh();
}

function gameChooseParties(button) { _gmOpenParties(button.dataset.arg); }

const GAME_HOW_SCORED = `<ol class="gm-how-steps">
    <li>${pfIcon('description')}<span>AI חילץ מהפרוטוקולים של דיוני הכנסת את העמדות שכל חבר/ת כנסת הביעו.</span></li>
    <li>${pfIcon('alt_route')}<span>AI נוסף קיבץ את העמדות לגישות שונות בכל נושא.</span></li>
    <li>${pfIcon('favorite')}<span>החלקה ימינה היא הסכמה עם הגישה כולה: כל מפלגה שחבר או חברת כנסת שלה דיברו בעד הגישה מקבלת נקודה. החלקה שמאלה נותנת לאותן מפלגות נקודת אי־הסכמה.</span></li>
    <li>${pfIcon('balance')}<span>בלי אחוז הסכמה, המפלגה עם הכי הרבה נקודות הסכמה מובילה. עם אחוז הסכמה, כל מפלגה נמדדת לפי החלק מהנקודות שלה שהיו הסכמה, כך שמפלגות גדולות לא מובילות רק כי דיברו על יותר נושאים.</span></li>
  </ol>`;

/* results: the normalization toggle (with how scores are counted) over the shown parties; show / play: parties only. */
async function _gmOpenParties(mode) {
  const overlay = document.getElementById('gm-parties-overlay');
  const body = overlay.querySelector('.gm-parties-body');
  const apply = overlay.querySelector('[data-click="gameApplyParties"]');
  overlay.dataset.mode = mode;
  overlay.querySelector('.gm-parties-title').textContent = { results: 'איך להציג את התוצאות?', show: 'אילו מפלגות להציג בתוצאות?', play: 'נושאים של אילו מפלגות לשחק?' }[mode];
  apply.disabled = true;
  body.innerHTML = pfSpinner('טוען מפלגות…');
  overlay.classList.add('open');
  if (!_gmPartyList) {
    try { _gmPartyList = (await pfFetch('/api/game/parties')).parties; } catch (err) { body.innerHTML = pfErrorBox(err); return; }
  }
  const key = mode === 'play' ? 'play' : 'show';
  const chosen = _gmGetPrefs()[key];
  const intro = {
    results: `<div class="gm-norm-row">
        <label class="gm-norm-toggle"><span class="toggle-switch"><input type="checkbox" class="gm-norm-input" ${_gmGetPrefs().normalized ? 'checked' : ''}><span class="toggle-track"></span></span><b>לפי אחוז הסכמה</b></label>
        <button type="button" class="gm-tool gm-how-btn" data-click="gameHowScored">${pfIcon('help')}איך זה מחושב?</button>
      </div>
      <div class="gm-how" hidden>${GAME_HOW_SCORED}</div>
      <p class="gm-parties-intro gm-parties-warn">${pfIcon('warning')}<span>באחוז הסכמה מפלגות קטנות עלולות לבלוט אחרי מעט נושאים. הציון של מפלגה שפגשת מעט מתקרב לממוצע שלך עד שמצטברים נושאים, ואפשר לבחור כאן אילו מפלגות להציג.</span></p>
      <h3 class="gm-parties-sub">מפלגות בתוצאות</h3>`,
    show: '<p class="gm-parties-intro">רק המפלגות שנבחרו יופיעו בתוצאות. המשחק עצמו לא משתנה.</p>',
    play: '<p class="gm-parties-intro">במשחק יופיעו רק נושאים של המפלגות שנבחרו. הנושא נבחר קודם, מבין הנושאים שלפחות לאחת מהן יש בהם עמדה.</p>',
  }[mode];
  body.innerHTML = `${intro}
    <div class="gm-parties-bar"><button type="button" class="gm-tool" data-click="gamePartiesAll" data-arg="1">${pfIcon('done_all')}בחירת הכל</button>
      <button type="button" class="gm-tool" data-click="gamePartiesAll" data-arg="0">${pfIcon('remove_done')}ניקוי</button></div>
    <div class="gm-parties-list">${_gmPartyList.map(({ party, cards }) => `<label class="gm-party-pick" style="--pc:${_gmPartyColor(party.id)}">
      <input type="checkbox" value="${_esc(party.id)}" ${!chosen || chosen.includes(party.id) ? 'checked' : ''}>
      ${party.logo_url ? `<img src="${pfEsc(party.logo_url)}" alt="" data-hide-on-error>` : '<i></i>'}
      <span><b>${pfEsc(party.name)}</b><small>${pfNum(cards)} נושאים במשחק</small></span>
      ${pfIcon('check_circle')}
    </label>`).join('')}</div>`;
  apply.disabled = false;
}

function gameHowScored(button) {
  const how = button.closest('.gm-parties-body').querySelector('.gm-how');
  how.hidden = !how.hidden;
  button.classList.toggle('on', !how.hidden);
}

function gamePartiesAll(button) {
  document.querySelectorAll('#gm-parties-overlay .gm-party-pick input').forEach(box => { box.checked = button.dataset.arg === '1'; });
}

function gameApplyParties() {
  const overlay = document.getElementById('gm-parties-overlay');
  const boxes = [...overlay.querySelectorAll('.gm-party-pick input')];
  const picked = boxes.filter(box => box.checked).map(box => Number(box.value));
  if (!picked.length) { showToast('צריך לבחור לפחות מפלגה אחת'); return; }
  const prefs = _gmGetPrefs();
  const key = overlay.dataset.mode === 'play' ? 'play' : 'show';
  const parties = picked.length === boxes.length ? null : picked;
  const changed = JSON.stringify(parties) !== JSON.stringify(prefs[key]);
  prefs[key] = parties;
  const normalized = overlay.querySelector('.gm-norm-input');
  if (normalized) prefs.normalized = normalized.checked;
  _gmSavePrefs();
  gameCloseParties();
  _gmPrefsChanged(key === 'play' && changed);
}

function gameCloseParties() {
  document.getElementById('gm-parties-overlay').classList.remove('open');
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
