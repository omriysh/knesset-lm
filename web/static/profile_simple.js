/**
 * profile_simple.js — the top of a candidate's profile: photo and details, the visitor's swipes on their themes
 * (game.js storage) and habits; and the profile's subjects tab: their interests as umbrella bubbles over a
 * cumulative timeline with their approach to each shared subject.
 *
 * Non-module script, loaded after profiles.js (pf* helpers, the theme sheet) and before game.js / actions.js.
 */

const INTEREST_PLAY_STEP_MS = 1300;
const BUBBLE_MIN_RADIUS_PX = 36;
const BUBBLE_COLORS = [['#266829', '#ffffff'], ['#2e7d32', '#ffffff'], ['#5a9e4b', '#ffffff'], ['#b2faa9', '#185c1e'], ['#cdeec4', '#185c1e'], ['#e8f5e3', '#185c1e']];
const UMBRELLA_SHORT = { 'ביטחון וצבא': 'ביטחון וצבא', 'חינוך והשכלה': 'חינוך', 'מדיני ויחסי חוץ': 'מדיני וחוץ', 'ממשל ומשפט': 'ממשל ומשפט',
  'חברה וזכויות אזרח': 'חברה', 'נושאים נוספים': 'עוד', 'דת ומדינה': 'דת ומדינה', 'כלכלה ותעשייה': 'כלכלה' };
const HALF_LAST_MONTH = ['יוני', 'דצמבר'];
const SIMPLE_FACT_ROWS = ['residence', 'education', 'military_service', 'profession'];

let _pfs = null;
let _pfsViaMatches = [];

function _pfsGameVotes() {
  try {
    const saved = JSON.parse(localStorage.getItem(GAME_STORAGE_KEY) || 'null');
    return new Map((saved && Array.isArray(saved.votes) ? saved.votes : []).map(([id, vote]) => [Number(id), vote]));
  } catch (exc) { console.warn('[profile_simple] game votes unreadable:', exc); return new Map(); }
}

/* ── page ────────────────────────────────────────────────────────── */
const pfsAvailable = data => data.candidate.profile === 'full' && Boolean(data.activity && data.activity.has_subjects);

function pfsLoadSubjectsTab(profile) {
  const { data, api } = profile;
  _pfs = { api, data, period: 0, umbrella: 0, playing: null, interests: null };
  const years = _pfLatestServiceYears([data.activity.knesset_num]);
  document.getElementById('pf-panel-subjects').innerHTML = `<section class="pfs-card pfs-interests" id="pfs-interests" data-pf-source="interests">
      <div class="pfs-section-head"><h2>תחומי עניין ${pfInLabel(pfKnessetLabel(data.activity.knesset_num))} (${years})</h2></div>
      <div class="pfs-interests-body">${pfSpinner('טוען תחומי עניין…')}</div>
    </section>`;
  _pfsLoadInterests(_pfs);
}

/* The photo, details, swipes and habits that open the profile. */
function pfsHeader(data, withSwipes) {
  const { candidate, party, activity } = data;
  const habitsWhen = activity ? `${pfInLabel(pfKnessetLabel(activity.knesset_num))} (${_pfLatestServiceYears([activity.knesset_num])})` : 'בכנסת';
  return `<div class="pfs-top${withSwipes ? '' : ' pfs-top--two'}">
      <div class="pfs-photo">
        ${candidate.photo_url ? `<img src="${pfEsc(candidate.photo_url)}" alt="" data-hide-on-error>` : `<div class="pfs-initials">${pfInitials(candidate.name)}</div>`}
        <div class="pfs-photo-text">
          <h1>${pfEsc(candidate.name)}</h1>
          <div>${pfEsc(party.name)} · מקום ${candidate.position}</div>
        </div>
        ${candidate.photo_url ? '<div class="pfs-photo-credit">צילום: אתר הכנסת</div>' : ''}
      </div>
      <section class="pfs-card pfs-facts">${_pfsFacts(data)}</section>
      ${withSwipes ? `<section class="pfs-card pfs-swipes" id="pfs-swipes" data-pf-source="swipes">${pfSpinner('טוען…')}</section>` : ''}
    </div>
    <section class="pfs-habits">
      <div class="pfs-section-head"><h2>ההרגלים ${habitsWhen}</h2></div>
      <div class="pfs-tiles">${_pfsHabitTiles(data)}</div>
    </section>`;
}

function _pfsFacts(data) {
  const { candidate } = data;
  const details = candidate.details || {};
  const rows = SIMPLE_FACT_ROWS.filter(key => details[key]).map(key => {
    const [, icon, label] = DETAIL_ROWS.find(([rowKey]) => rowKey === key);
    return `<div class="pfs-fact" title="${label}">${pfIcon(icon)}<span>${pfEsc(_pfDetailItems(details[key])[0] || '')}</span></div>`;
  });
  rows.unshift(`<div class="pfs-fact">${pfIcon('badge')}<span>${_pfHeroRole(data)}</span></div>`);
  const site = candidate.site_id ? `<a href="${KNESSET_SITE_MK_URL}${Number(candidate.site_id)}" target="_blank" rel="noopener">אתר הכנסת</a>` : 'אתר הכנסת';
  return `${rows.join('')}<div class="pf-details-source">מקור הפרטים: ${site}</div>`;
}

/* ── swipes ──────────────────────────────────────────────────────── */
function _pfsEvidence(themes) {
  const evidence = {};
  themes.forEach(theme => theme.evidence.forEach((e, n) => { evidence[`${theme.id}-${n}`] = e; }));
  return evidence;
}

/* Colours the /themes payload's themes, shows the visitor's swipes on them and returns them. */
function pfsShowSwipes(payload, api, name) {
  const themes = payload.themes.map((theme, i) => ({ ...theme, color: THEME_COLORS[i % THEME_COLORS.length] }));
  pfSetThemeSource('swipes', { themes, quarters: payload.quarters, evidence: _pfsEvidence(themes), api });
  _pfsLoadSwipes(themes, api, name);
  return themes;
}

/* The candidate's themes in the same approach as a swiped theme count too: they come from the server. */
async function _pfsLoadSwipes(themes, api, name) {
  const votes = _pfsGameVotes();
  const [, partyId, position] = api.match(/party\/(\d+)\/candidate\/(\d+)/).map(Number);
  const ids = vote => [...votes].filter(([, v]) => v === vote).map(([id]) => id);
  let via = [];
  if (votes.size) {
    try {
      const response = await fetch('/api/game/candidate-matches', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agreed: ids(GAME_VOTE_YES), disagreed: ids(GAME_VOTE_NO), party_id: partyId, position }) });
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      via = (await response.json()).matches.filter(match => match.swiped);
    } catch (exc) { console.error('[profile_simple] approach matches failed:', exc); }
  }
  _pfsRenderSwipes(themes, votes, via, name);
}

function _pfsRenderSwipes(themes, votes, via, name) {
  const box = document.getElementById('pfs-swipes');
  if (!box) return;
  _pfsViaMatches = via;
  const swiped = themes.filter(theme => votes.has(theme.id)).map(theme => ({ theme, vote: votes.get(theme.id) }));
  const decided = swiped.filter(s => s.vote !== GAME_VOTE_UNSURE).length + via.length;
  const agreed = swiped.filter(s => s.vote === GAME_VOTE_YES).length + via.filter(match => match.agreed).length;
  const head = '<h2 class="pfs-card-title">מה ההחלקות שלכם אומרות</h2>';
  if (!swiped.length && !via.length) {
    box.innerHTML = `${head}<p class="pfs-muted">עוד לא החלקתם על אף אחד מהנושאים של ${pfEsc(name)} במשחק ההיכרות.</p>
      <button class="btn btn-secondary btn-sm" type="button" data-click="switchTab" data-arg="game">${pfIcon('favorite')}למשחק ההיכרות</button>`;
    return;
  }
  const pct = pfPct(agreed, decided);
  const verdict = !decided ? 'עוד לא החלטתם' : pct >= 60 ? 'יש ביניכם כימיה' : pct >= 40 ? 'יש על מה לדבר' : 'כנראה שלא תתחברו';
  const look = { [GAME_VOTE_YES]: ['favorite', 'pfs-swipe--yes', 'הסכמתם'], [GAME_VOTE_NO]: ['heart_broken', 'pfs-swipe--no', 'לא הסכמתם'], [GAME_VOTE_UNSURE]: ['close', 'pfs-swipe--unsure', 'לא בטוחים'] };
  const viaRow = (match, i) => `<button class="pfs-swipe pfs-swipe--via pfs-swipe--via-${match.agreed ? 'yes' : 'no'}" type="button" data-click="profilesViaMatch" data-arg="${i}">${pfIcon(match.agreed ? 'favorite' : 'heart_broken')}<span><b>${pfEsc(match.title)}</b><small>${match.agreed ? 'הסכמתם' : 'לא הסכמתם'} עם הגישה${match.via_approach ? `: ${pfEsc(match.via_approach)}` : ''}</small></span></button>`;
  box.innerHTML = `${head}
    <div class="pfs-chemistry">
      <div class="pfs-ring" style="--pct:${decided ? pct : 0}"><b>${decided ? `${pct}%` : '—'}</b></div>
      <div><div class="pfs-chemistry-title">${verdict}</div><div class="pfs-muted">הסכמתם עם ${pfNum(agreed)} מתוך ${pfNum(decided)} העמדות שנספרו במשחק</div></div>
    </div>
    ${swiped.map(({ theme, vote }) => `<button class="pfs-swipe ${look[vote][1]}" type="button" data-click="profilesThemeSheet" data-arg="${_esc(theme.id)}">${pfIcon(look[vote][0])}<span><b>${pfEsc(theme.title)}</b><small>${look[vote][2]}</small></span></button>`).join('')}
    ${via.length ? `<div class="pfs-swipes-sub">באותה גישה כמו עמדה שהחלקתם</div>${via.map(viaRow).join('')}` : ''}`;
}

function profilesViaMatch(button) {
  const match = _pfsViaMatches[Number(button.dataset.arg)];
  if (match) gmOpenRelated(match, match.agreed);
}

/* ── habits ──────────────────────────────────────────────────────── */
const _pfsTile = (id, icon, color, value, label, source, extra = '') =>
  `<div class="pfs-tile${value === '…' ? ' pfs-tile--wait' : ''}" id="${id}">${pfIcon(icon).replace('material-symbols-outlined', `material-symbols-outlined" style="color:${color}`)}
    <div class="pfs-tile-value">${value}${extra}</div><div class="pfs-tile-label">${label}</div><div class="pfs-tile-source">${source}</div></div>`;

function _pfsHabitTiles(data) {
  const activity = data.activity;
  const votesTile = _pfsTile('pfs-tile-votes', 'how_to_vote', 'var(--secondary)', '…', 'נוכחות בהצבעות במליאה', 'מקור: אתר הכנסת');
  const billsTile = _pfsTile('pfs-tile-bills', 'gavel', 'var(--primary)', '…', 'הצעות חוק שיזם/ה או הצטרף/ה אליהן', 'מקור: אתר הכנסת');
  if (!activity) {
    return [_pfsTile('pfs-tile-knessets', 'account_balance', 'var(--tertiary)', pfNum(data.candidate.knessets.length), `כהונות בכנסת (${pfKnessetsLabel(data.candidate.knessets)})`, 'מקור: אתר הכנסת'),
      votesTile, billsTile].join('');
  }
  const attendance = activity.attendance;
  const committeePct = pfPct(attendance.member_meetings_attended, attendance.member_meetings);
  return [
    votesTile,
    attendance.member_meetings
      ? _pfsTile('pfs-tile-committee', 'groups', 'var(--tertiary)', `${committeePct}<small>%</small>`, `נוכחות בוועדות שהוא/היא חבר/ה בהן (${pfNum(attendance.member_meetings_attended)} מתוך ${pfNum(attendance.member_meetings)} ישיבות)`, 'מקור: פרוטוקולי הכנסת')
      : _pfsTile('pfs-tile-committee', 'groups', 'var(--tertiary)', '—', 'לא היה/תה חבר/ה בוועדות', 'מקור: אתר הכנסת'),
    _pfsTile('pfs-tile-other', 'explore', 'var(--tertiary)', pfNum(attendance.other_committee_meetings_attended), 'נוכחות בוועדות שהוא/היא לא חבר/ה בהן', 'מקור: פרוטוקולי הכנסת', '<small> ישיבות</small>'),
    billsTile,
  ].join('');
}

function _pfsSetTile(id, valueHtml, label, afterLabelHtml = '') {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.classList.remove('pfs-tile--wait');
  tile.querySelector('.pfs-tile-value').innerHTML = valueHtml;
  if (label) tile.querySelector('.pfs-tile-label').textContent = label;
  if (afterLabelHtml) tile.querySelector('.pfs-tile-label').insertAdjacentHTML('afterend', afterLabelHtml);
}

function pfsLoadHabits(api, isCurrent) {
  pfFetch(`${api}/vote-summary`).then(summary => {
    if (!isCurrent()) return;
    const latest = summary.knessets.find(k => k.plenum_votes);
    if (!latest) { _pfsSetTile('pfs-tile-votes', '—', 'אין הצבעות רשומות במליאה'); return; }
    _pfsSetTile('pfs-tile-votes', `${pfPct(latest.votes_cast, latest.plenum_votes)}<small>%</small>`,
      `נוכחות בהצבעות במליאה (${pfNum(latest.votes_cast)} מתוך ${pfNum(latest.plenum_votes)})`);
  }).catch(err => _pfsSetTile('pfs-tile-votes', '—', `הצבעות: ${err.message}`));
  Promise.all([pfFetch(`${api}/bills`), pfFetch(`${api}/bills?stage=passed`)]).then(([bills, passed]) => {
    if (!isCurrent()) return;
    const passedPct = pfPct(passed.total, bills.total);
    _pfsSetTile('pfs-tile-bills', pfNum(bills.total), null, bills.total ? `<div class="pfs-tile-bar" style="--pct:${passedPct}%"><i></i></div>
      <div class="pfs-tile-bar-label"><b>${pfNum(passed.total)}</b> התקבלו כחוק · ${passedPct}%</div>` : '');
  }).catch(err => _pfsSetTile('pfs-tile-bills', '—', `הצעות חוק: ${err.message}`));
}

/* ── interests ───────────────────────────────────────────────────── */
async function _pfsLoadInterests(state) {
  const body = document.querySelector('#pfs-interests .pfs-interests-body');
  let payload;
  try { payload = await pfFetch(`${state.api}/themes`); } catch (err) { if (state === _pfs) body.innerHTML = pfErrorBox(err); return; }
  if (state !== _pfs) return;
  const themes = pfsShowSwipes(payload, state.api, state.data.candidate.name);
  pfSetThemeSource('interests', { themes, quarters: payload.quarters, evidence: _pfsEvidence(themes), api: state.api });
  if (!themes.length) { body.innerHTML = '<div class="pf-empty">עוד לא נוצרו נושאים לחבר/ת הכנסת הזה/ו.</div>'; return; }
  state.interests = { ...payload, themes, themeById: Object.fromEntries(themes.map(t => [t.id, t])), periods: _pfsPeriods(payload.quarters) };
  state.period = state.interests.periods.length - 1;
  body.innerHTML = `
    <div class="pf-ai-band">${pfIcon('auto_awesome')}<span>נוצר על ידי AI מסיכומי הפרוטוקולים ועלול להכיל טעויות · גודל הבועה = כמה עמדות הביע בתחום · הגישות קובצו מעמדות כל חברי הכנסת</span></div>
    <div class="pfs-interests-grid">
      <div class="pfs-viz-col">
        <div class="pfs-timeline">
          <div class="pfs-timeline-head"><b id="pfs-period-label"></b>
            <button class="pfs-play" type="button" data-click="profilesSimplePlay" aria-label="ניגון לאורך הזמן">${pfIcon('play_arrow')}</button></div>
          <div class="pfs-timeline-bars">${state.interests.periods.map((period, i) => `<button type="button" data-click="profilesSimplePeriod" data-arg="${_esc(i)}" title="${pfEsc(period.label)}"><i></i><span>${period.short}</span></button>`).join('')}</div>
        </div>
        <div class="pfs-bubbles">${payload.umbrellas.map((u, i) => `<button type="button"><b>${pfEsc(UMBRELLA_SHORT[u.name] || u.name)}</b><small></small></button>`).join('')}</div>
      </div>
      <div class="pfs-approaches"></div>
    </div>`;
  const box = body.querySelector('.pfs-bubbles');
  state.bubbles = bubblePlayCreate(box, [...box.children], { onTap: _pfsSelectUmbrella, minRadius: BUBBLE_MIN_RADIUS_PX });
  _pfsUpdateInterests();
}

function _pfsPeriods(quarters) {
  const periods = [];
  quarters.forEach((quarter, index) => {
    const year = quarter.slice(0, 4), half = Number(quarter.slice(5)) <= 2 ? 0 : 1;
    let period = periods[periods.length - 1];
    if (!period || period.key !== `${year}-${half}`) {
      period = { key: `${year}-${half}`, label: `${HALF_LAST_MONTH[half]} ${year}`, short: half === 0 || !periods.length ? year : '', quarters: [] };
      periods.push(period);
    }
    period.quarters.push(index);
  });
  return periods;
}

const _pfsUpTo = (counts, periods, periodIndex) => periods.slice(0, periodIndex + 1).reduce((acc, p) => acc + p.quarters.reduce((sum, q) => sum + counts[q], 0), 0);

function _pfsUpdateInterests() {
  const state = _pfs;
  const { umbrellas, periods } = state.interests;
  const last = periods.length - 1;
  document.getElementById('pfs-period-label').textContent = state.period === last ? 'עמדות לאורך תקופת הכנסת האחרונה' : `עמדות עד ${periods[state.period].label}`;
  const perPeriod = periods.map(p => umbrellas.reduce((acc, u) => acc + p.quarters.reduce((sum, q) => sum + u.quarter_counts[q], 0), 0));
  const peakPeriod = Math.max(1, ...perPeriod);
  document.querySelectorAll('.pfs-timeline-bars button').forEach((button, i) => {
    button.querySelector('i').style.height = `${6 + (perPeriod[i] / peakPeriod) * 30}px`;
    button.classList.toggle('on', i <= state.period);
    button.classList.toggle('current', i === state.period);
  });
  const values = umbrellas.map(u => _pfsUpTo(u.quarter_counts, periods, state.period));
  const radii = bubblePlaySet(state.bubbles, values, umbrellas.reduce((sum, u) => sum + u.opinions, 0));
  state.bubbles.bodies.forEach(({ el: button }, i) => {
    const [background, color] = i === state.umbrella ? ['var(--ink)', '#ffffff'] : BUBBLE_COLORS[Math.min(i, BUBBLE_COLORS.length - 1)];
    Object.assign(button.style, { background, color });
    button.style.setProperty('--fs', `${Math.max(10, Math.min(22, radii[i] / 2.8))}px`);
    button.classList.toggle('on', i === state.umbrella);
    button.querySelector('small').textContent = pfNum(values[i]);
  });
  _pfsRenderApproaches(values[state.umbrella]);
}

function _pfsApproachRows(umbrella) {
  const rows = [];
  umbrella.theme_ids.forEach(themeId => {
    const theme = _pfs.interests.themeById[themeId];
    const subjects = theme.subjects.filter(s => s.umbrella === umbrella.name);
    if (!subjects.length) rows.push({ subject: null, themes: [theme] });
    subjects.forEach(subject => {
      const row = rows.find(r => r.subject && r.subject.id === subject.id);
      if (row) row.themes.push(theme); else rows.push({ subject, themes: [theme] });
    });
  });
  return rows;
}

function _pfsRenderApproaches(umbrellaOpinions) {
  const { umbrellas, periods } = _pfs.interests;
  const umbrella = umbrellas[_pfs.umbrella];
  const rows = _pfsApproachRows(umbrella).map(row => ({ ...row, count: row.themes.reduce((acc, t) => acc + _pfsUpTo(t.quarter_counts, periods, _pfs.period), 0) }));
  const peak = Math.max(1, ...rows.map(r => r.count));
  document.querySelector('.pfs-approaches').innerHTML = `
    <div class="pfs-approaches-head"><b>${pfEsc(umbrella.name)}</b><span>${pfNum(umbrellaOpinions)} עמדות</span></div>
    ${rows.map(({ subject, themes, count }) => {
      const approach = subject && subject.approach;
      const text = approach ? approach.name : subject ? subject.name : themes[0].title;
      const tag = approach ? `<span class="pfs-tag ${approach.majority ? 'pfs-tag--majority' : 'pfs-tag--minority'}">${approach.majority ? 'דעת רוב' : 'דעת מיעוט'}</span>` : '';
      const context = approach ? ` · ${pfNum(approach.mk_count)} מתוך ${pfNum(subject.mk_count)} ח״כים שדיברו על הנושא` : '';
      return `<button class="pfs-approach${count ? '' : ' pfs-approach--empty'}" type="button" data-click="profilesThemeSheet" data-arg="${_esc(themes[0].id)}">
        <span class="pfs-approach-main"><span class="pfs-approach-top"><b>${pfEsc(text)}</b>${tag}</span>
          <span class="pfs-approach-bar"><i style="width:${(count / peak) * 100}%"></i></span>
          <small>${pfNum(count)} עמדות${context}</small></span>${pfIcon('chevron_left')}</button>`;
    }).join('')}`;
}

function pfsStopPlay() {
  if (!_pfs || !_pfs.playing) return;
  clearInterval(_pfs.playing);
  _pfs.playing = null;
  const play = document.querySelector('.pfs-play .material-symbols-outlined');
  if (play) play.textContent = 'play_arrow';
}

function profilesSimplePeriod(button) {
  if (!_pfs?.interests) return;
  pfsStopPlay();
  _pfs.period = Number(button.dataset.arg);
  _pfsUpdateInterests();
}

function _pfsSelectUmbrella(index) {
  if (!_pfs?.interests) return;
  _pfs.umbrella = index;
  _pfsUpdateInterests();
}

function profilesSimplePlay() {
  if (!_pfs?.interests) return;
  if (_pfs.playing) { pfsStopPlay(); return; }
  const state = _pfs;
  state.period = 0;
  _pfsUpdateInterests();
  document.querySelector('.pfs-play .material-symbols-outlined').textContent = 'pause';
  state.playing = setInterval(() => {
    if (state !== _pfs || !document.querySelector('.pfs-bubbles')) { clearInterval(state.playing); state.playing = null; return; }
    if (state.period >= state.interests.periods.length - 1) { pfsStopPlay(); return; }
    state.period += 1;
    _pfsUpdateInterests();
  }, INTEREST_PLAY_STEP_MS);
}

const _pfsRelayout = pfDebounce(() => { if (_pfs?.interests && document.querySelector('.pfs-bubbles')?.offsetParent) _pfsUpdateInterests(); }, 200);
window.addEventListener('resize', _pfsRelayout);
