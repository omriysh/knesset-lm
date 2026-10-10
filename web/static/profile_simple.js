/**
 * profile_simple.js — the simple profile of a candidate who served with committee activity: photo and
 * details, the visitor's swipes on their themes (game.js storage), habits, and their interests as umbrella
 * bubbles over a cumulative timeline with their approach to each shared subject. The full profile
 * (profiles.js) is one click away at #full; a setting decides which of the two opens first.
 *
 * Non-module script, loaded after profiles.js (pf* helpers, the theme sheet) and before game.js / actions.js.
 */

const SIMPLE_FIRST_KEY = 'profileSimpleFirst';
const FULL_CLICKS_KEY = 'profileFullClicks';
const DEFAULT_TIP_SHOWN_KEY = 'profileDefaultTipShown';
const FULL_CLICKS_BEFORE_TIP = 3;
const INTEREST_PLAY_STEP_MS = 1300;
const BUBBLE_GAP_PX = 6;
const BUBBLE_MIN_RADIUS_SHARE = 0.3;
const BUBBLE_COLORS = [['#266829', '#ffffff'], ['#2e7d32', '#ffffff'], ['#5a9e4b', '#ffffff'], ['#b2faa9', '#185c1e'], ['#cdeec4', '#185c1e'], ['#e8f5e3', '#185c1e']];
const UMBRELLA_SHORT = { 'ביטחון וצבא': 'ביטחון וצבא', 'חינוך והשכלה': 'חינוך', 'מדיני ויחסי חוץ': 'מדיני וחוץ', 'ממשל ומשפט': 'ממשל ומשפט',
  'חברה וזכויות אזרח': 'חברה', 'נושאים נוספים': 'עוד', 'דת ומדינה': 'דת ומדינה', 'כלכלה ותעשייה': 'כלכלה' };
const HALF_LAST_MONTH = ['יוני', 'דצמבר'];
const SIMPLE_FACT_ROWS = ['residence', 'education', 'military_service', 'profession'];

let _pfs = null;

function _pfsStored(key, fallback) {
  try { const value = localStorage.getItem(key); return value === null ? fallback : value; } catch (exc) { console.warn('[profile_simple] localStorage unavailable:', exc); return fallback; }
}
function _pfsStore(key, value) {
  try { localStorage.setItem(key, value); } catch (exc) { console.warn('[profile_simple] localStorage unavailable:', exc); }
}
function profileSimpleFirst() { return _pfsStored(SIMPLE_FIRST_KEY, '1') === '1'; }
function setProfileSimpleFirst(on) { _pfsStore(SIMPLE_FIRST_KEY, on ? '1' : '0'); }

function _pfsGameVotes() {
  try {
    const saved = JSON.parse(localStorage.getItem(GAME_STORAGE_KEY) || 'null');
    return new Map((saved && Array.isArray(saved.votes) ? saved.votes : []).map(([id, vote]) => [Number(id), vote]));
  } catch (exc) { console.warn('[profile_simple] game votes unreadable:', exc); return new Map(); }
}

/* ── page ────────────────────────────────────────────────────────── */
const pfsAvailable = data => data.candidate.profile === 'full' && Boolean(data.activity && data.activity.has_subjects);

function pfsRender(root, data, partyId, candidateId, crumbs) {
  const { candidate, party, activity } = data;
  const api = `/api/profiles/party/${partyId}/candidate/${candidateId}`;
  const fullHref = `${PROFILES_PATH}/party/${partyId}/candidate/${candidateId}#full`;
  _pfs = { api, data, period: 0, umbrella: 0, playing: null, interests: null };
  const fullButton = cls => `<a class="pfs-full-btn ${cls}" href="${pfEsc(fullHref)}" data-click="profilesSimpleToFull">לפרופיל המלא${pfIcon('arrow_back')}</a>`;
  const years = _pfLatestServiceYears([activity.knesset_num]);
  root.innerHTML = `<div class="pf-page pfs">
    <div class="pfs-topline">${crumbs}${fullButton('pfs-full-btn--top')}</div>
    <div class="pfs-top">
      <div class="pfs-photo">
        ${candidate.photo_url ? `<img src="${pfEsc(candidate.photo_url)}" alt="" data-hide-on-error>` : `<div class="pfs-initials">${pfInitials(candidate.name)}</div>`}
        <div class="pfs-photo-text">
          <h1>${pfEsc(candidate.name)}</h1>
          <div>${pfEsc(party.name)} · מקום ${candidate.position}</div>
        </div>
        ${candidate.photo_url ? '<div class="pfs-photo-credit">צילום: אתר הכנסת</div>' : ''}
      </div>
      <section class="pfs-card pfs-facts">${_pfsFacts(data)}</section>
      <section class="pfs-card pfs-swipes" id="pfs-swipes">${pfSpinner('טוען…')}</section>
    </div>
    <section class="pfs-habits">
      <div class="pfs-section-head"><h2>ההרגלים ${pfInLabel(pfKnessetLabel(activity.knesset_num))} (${years})</h2></div>
      <div class="pfs-tiles">${_pfsHabitTiles(activity)}</div>
    </section>
    <section class="pfs-card pfs-interests" id="pfs-interests">
      <div class="pfs-section-head"><h2>תחומי עניין ${pfInLabel(pfKnessetLabel(activity.knesset_num))} (${years})</h2></div>
      <div class="pfs-interests-body">${pfSpinner('טוען תחומי עניין…')}</div>
    </section>
    <div class="pfs-bottom">${fullButton('')}</div>
  </div>`;
  _pfsLoadHabits(_pfs);
  _pfsLoadInterests(_pfs);
}

function _pfsFacts(data) {
  const { candidate } = data;
  const details = candidate.details || {};
  const rows = SIMPLE_FACT_ROWS.filter(key => details[key]).map(key => {
    const [, icon, label] = DETAIL_ROWS.find(([rowKey]) => rowKey === key);
    return `<div class="pfs-fact" title="${label}">${pfIcon(icon)}<span>${pfEsc(_pfDetailItems(details[key])[0] || '')}</span></div>`;
  });
  rows.unshift(`<div class="pfs-fact">${pfIcon('badge')}<span>${_pfHeroRole(data)}</span></div>`);
  return `${rows.join('')}<div class="pf-details-source">מקור הפרטים: אתר הכנסת</div>`;
}

/* ── swipes ──────────────────────────────────────────────────────── */
function _pfsRenderSwipes(themes) {
  const box = document.getElementById('pfs-swipes');
  if (!box) return;
  const votes = _pfsGameVotes();
  const swiped = themes.filter(theme => votes.has(theme.id)).map(theme => ({ theme, vote: votes.get(theme.id) }));
  const decided = swiped.filter(s => s.vote !== GAME_VOTE_UNSURE);
  const agreed = decided.filter(s => s.vote === GAME_VOTE_YES).length;
  const head = '<h2 class="pfs-card-title">מה ההחלקות שלכם אומרות</h2>';
  if (!swiped.length) {
    box.innerHTML = `${head}<p class="pfs-muted">עוד לא החלקתם על אף אחד מהנושאים של ${pfEsc(_pfs.data.candidate.name)} במשחק ההיכרות.</p>
      <button class="btn btn-secondary btn-sm" type="button" data-click="switchTab" data-arg="game">${pfIcon('favorite')}למשחק ההיכרות</button>`;
    return;
  }
  const pct = pfPct(agreed, decided.length);
  const verdict = !decided.length ? 'עוד לא החלטתם' : pct >= 60 ? 'יש ביניכם כימיה' : pct >= 40 ? 'יש על מה לדבר' : 'כנראה שלא תתחברו';
  const look = { [GAME_VOTE_YES]: ['favorite', 'pfs-swipe--yes', 'הסכמתם'], [GAME_VOTE_NO]: ['heart_broken', 'pfs-swipe--no', 'לא הסכמתם'], [GAME_VOTE_UNSURE]: ['question_mark', 'pfs-swipe--unsure', 'לא בטוחים'] };
  box.innerHTML = `${head}
    <div class="pfs-chemistry">
      <div class="pfs-ring" style="--pct:${decided.length ? pct : 0}"><b>${decided.length ? `${pct}%` : '—'}</b></div>
      <div><div class="pfs-chemistry-title">${verdict}</div><div class="pfs-muted">הסכמתם עם ${pfNum(agreed)} מתוך ${pfNum(decided.length)} העמדות שהחלקתם במשחק</div></div>
    </div>
    ${swiped.map(({ theme, vote }) => `<button class="pfs-swipe ${look[vote][1]}" type="button" data-click="profilesThemeSheet" data-arg="${_esc(theme.id)}">${pfIcon(look[vote][0])}<span><b>${pfEsc(theme.title)}</b><small>${look[vote][2]}</small></span></button>`).join('')}`;
}

/* ── habits ──────────────────────────────────────────────────────── */
const _pfsTile = (id, icon, color, value, label, source, extra = '') =>
  `<div class="pfs-tile${value === '…' ? ' pfs-tile--wait' : ''}" id="${id}">${pfIcon(icon).replace('material-symbols-outlined', `material-symbols-outlined" style="color:${color}`)}
    <div class="pfs-tile-value">${value}${extra}</div><div class="pfs-tile-label">${label}</div><div class="pfs-tile-source">${source}</div></div>`;

function _pfsHabitTiles(activity) {
  const attendance = activity.attendance;
  const committeePct = pfPct(attendance.member_meetings_attended, attendance.member_meetings);
  return [
    _pfsTile('pfs-tile-votes', 'how_to_vote', 'var(--secondary)', '…', 'נוכחות בהצבעות במליאה', 'מקור: אתר הכנסת'),
    attendance.member_meetings
      ? _pfsTile('pfs-tile-committee', 'groups', 'var(--tertiary)', `${committeePct}<small>%</small>`, `נוכחות בוועדות שהוא/היא חבר/ה בהן (${pfNum(attendance.member_meetings_attended)} מתוך ${pfNum(attendance.member_meetings)} ישיבות)`, 'מקור: פרוטוקולי הכנסת')
      : _pfsTile('pfs-tile-committee', 'groups', 'var(--tertiary)', '—', 'לא היה/תה חבר/ה בוועדות', 'מקור: אתר הכנסת'),
    _pfsTile('pfs-tile-other', 'explore', 'var(--tertiary)', pfNum(attendance.other_committee_meetings_attended), 'נוכחות בוועדות שהוא/היא לא חבר/ה בהן', 'מקור: פרוטוקולי הכנסת', '<small> ישיבות</small>'),
    _pfsTile('pfs-tile-bills', 'gavel', 'var(--primary)', '…', 'הצעות חוק שיזם/ה או הצטרף/ה אליהן', 'מקור: אתר הכנסת'),
  ].join('');
}

function _pfsSetTile(id, valueHtml, label) {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.classList.remove('pfs-tile--wait');
  tile.querySelector('.pfs-tile-value').innerHTML = valueHtml;
  if (label) tile.querySelector('.pfs-tile-label').textContent = label;
}

function _pfsLoadHabits(state) {
  pfFetch(`${state.api}/vote-summary`).then(summary => {
    if (state !== _pfs) return;
    const latest = summary.knessets.find(k => k.plenum_votes);
    if (!latest) { _pfsSetTile('pfs-tile-votes', '—', 'אין הצבעות רשומות במליאה'); return; }
    _pfsSetTile('pfs-tile-votes', `${pfPct(latest.votes_cast, latest.plenum_votes)}<small>%</small>`,
      `נוכחות בהצבעות במליאה (${pfNum(latest.votes_cast)} מתוך ${pfNum(latest.plenum_votes)})`);
  }).catch(err => _pfsSetTile('pfs-tile-votes', '—', `הצבעות: ${err.message}`));
  Promise.all([pfFetch(`${state.api}/bills`), pfFetch(`${state.api}/bills?stage=passed`)]).then(([bills, passed]) => {
    if (state !== _pfs) return;
    _pfsSetTile('pfs-tile-bills', `${pfNum(bills.total)}<small class="pfs-tile-accent"> ${pfNum(passed.total)} התקבלו כחוק</small>`);
  }).catch(err => _pfsSetTile('pfs-tile-bills', '—', `הצעות חוק: ${err.message}`));
}

/* ── interests ───────────────────────────────────────────────────── */
async function _pfsLoadInterests(state) {
  const body = document.querySelector('#pfs-interests .pfs-interests-body');
  let payload;
  try { payload = await pfFetch(`${state.api}/themes`); } catch (err) { if (state === _pfs) body.innerHTML = pfErrorBox(err); return; }
  if (state !== _pfs) return;
  const themes = payload.themes.map((theme, i) => ({ ...theme, color: THEME_COLORS[i % THEME_COLORS.length] }));
  const evidence = {};
  themes.forEach(theme => theme.evidence.forEach((e, n) => { evidence[`${theme.id}-${n}`] = e; }));
  pfSetThemeSource('profile', { themes, quarters: payload.quarters, evidence, api: state.api });
  _pfsRenderSwipes(themes);
  if (!themes.length) { body.innerHTML = '<div class="pf-empty">עוד לא נוצרו נושאים לחבר/ת הכנסת הזה/ו.</div>'; return; }
  state.interests = { ...payload, themes, themeById: Object.fromEntries(themes.map(t => [t.id, t])), periods: _pfsPeriods(payload.quarters) };
  state.period = state.interests.periods.length - 1;
  body.innerHTML = `
    <div class="pf-ai-band">${pfIcon('auto_awesome')}<span>נוצר על ידי AI מסיכומי הפרוטוקולים · גודל הבועה = כמה עמדות הביע בתחום · הגישות קובצו מעמדות כל חברי הכנסת</span></div>
    <div class="pfs-interests-grid">
      <div class="pfs-viz-col">
        <div class="pfs-timeline">
          <div class="pfs-timeline-head"><b id="pfs-period-label"></b>
            <button class="pfs-play" type="button" data-click="profilesSimplePlay" aria-label="ניגון לאורך הזמן">${pfIcon('play_arrow')}</button></div>
          <div class="pfs-timeline-bars">${state.interests.periods.map((period, i) => `<button type="button" data-click="profilesSimplePeriod" data-arg="${_esc(i)}" title="${pfEsc(period.label)}"><i></i><span>${period.short}</span></button>`).join('')}</div>
        </div>
        <div class="pfs-bubbles">${payload.umbrellas.map((u, i) => `<button type="button" data-click="profilesSimpleUmbrella" data-arg="${_esc(i)}"><b>${pfEsc(UMBRELLA_SHORT[u.name] || u.name)}</b><small></small></button>`).join('')}</div>
      </div>
      <div class="pfs-approaches"></div>
    </div>`;
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
  const box = document.querySelector('.pfs-bubbles');
  const width = box.clientWidth || 320;
  const maxRadius = Math.min(width * 0.26, 130);
  const peak = Math.max(1, ...umbrellas.map(u => u.opinions));
  const radii = values.map(v => (v ? maxRadius * Math.max(BUBBLE_MIN_RADIUS_SHARE, Math.sqrt(v / peak)) : 0));
  const { height, cells } = _pfsPack(radii, width);
  box.style.height = `${Math.ceil(height)}px`;
  box.querySelectorAll('button').forEach((button, i) => {
    const { x, y, d } = cells[i];
    const [background, color] = i === state.umbrella ? ['var(--ink)', '#ffffff'] : BUBBLE_COLORS[Math.min(i, BUBBLE_COLORS.length - 1)];
    Object.assign(button.style, { right: `${x}px`, top: `${y}px`, width: `${d}px`, height: `${d}px`, background, color, opacity: d ? 1 : 0, pointerEvents: d ? '' : 'none' });
    button.style.setProperty('--fs', `${Math.max(10, Math.min(22, d / 5.6))}px`);
    button.classList.toggle('tiny', d < 40);
    button.classList.toggle('on', i === state.umbrella);
    button.querySelector('small').textContent = pfNum(values[i]);
  });
  _pfsRenderApproaches(values[state.umbrella]);
}

function _pfsPack(radii, width) {
  const placed = [];
  const fits = (x, y, r) => placed.every(p => !p.r || Math.hypot(p.x - x, p.y - y) >= p.r + r + BUBBLE_GAP_PX - 0.01);
  const cost = (x, y) => (x * x) / 2.2 + y * y;
  radii.forEach(r => {
    const solid = placed.filter(p => p.r);
    if (!r || !solid.length) { placed.push({ x: 0, y: 0, r }); return; }
    const candidates = [];
    solid.forEach(a => {
      for (let step = 0; step < 24; step++) {
        const angle = (step / 24) * Math.PI * 2, distance = a.r + r + BUBBLE_GAP_PX;
        candidates.push([a.x + Math.cos(angle) * distance, a.y + Math.sin(angle) * distance]);
      }
      solid.forEach(b => {
        const da = a.r + r + BUBBLE_GAP_PX, db = b.r + r + BUBBLE_GAP_PX, d = Math.hypot(b.x - a.x, b.y - a.y);
        if (a === b || d > da + db || d < Math.abs(da - db) || d === 0) return;
        const along = (da * da - db * db + d * d) / (2 * d), h = Math.sqrt(Math.max(0, da * da - along * along));
        const mx = a.x + (along * (b.x - a.x)) / d, my = a.y + (along * (b.y - a.y)) / d;
        candidates.push([mx + (h * (b.y - a.y)) / d, my - (h * (b.x - a.x)) / d], [mx - (h * (b.y - a.y)) / d, my + (h * (b.x - a.x)) / d]);
      });
    });
    const best = candidates.filter(([x, y]) => fits(x, y, r)).sort((p, q) => cost(...p) - cost(...q))[0];
    placed.push({ x: best[0], y: best[1], r });
  });
  const solid = placed.filter(p => p.r);
  if (!solid.length) return { height: 0, cells: placed.map(() => ({ x: width / 2, y: 0, d: 0 })) };
  const minX = Math.min(...solid.map(p => p.x - p.r)), maxX = Math.max(...solid.map(p => p.x + p.r));
  const minY = Math.min(...solid.map(p => p.y - p.r)), maxY = Math.max(...solid.map(p => p.y + p.r));
  const scale = Math.min(1, width / (maxX - minX));
  const offsetX = (width - (maxX - minX) * scale) / 2;
  const height = (maxY - minY) * scale;
  return { height, cells: placed.map(p => (p.r
    ? { x: offsetX + (p.x - p.r - minX) * scale, y: (p.y - p.r - minY) * scale, d: p.r * 2 * scale }
    : { x: width / 2, y: height / 2, d: 0 })) };
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

function _pfsStopPlay() {
  if (!_pfs || !_pfs.playing) return;
  clearInterval(_pfs.playing);
  _pfs.playing = null;
  const play = document.querySelector('.pfs-play .material-symbols-outlined');
  if (play) play.textContent = 'play_arrow';
}

function profilesSimplePeriod(button) {
  if (!_pfs?.interests) return;
  _pfsStopPlay();
  _pfs.period = Number(button.dataset.arg);
  _pfsUpdateInterests();
}

function profilesSimpleUmbrella(button) {
  if (!_pfs?.interests) return;
  _pfs.umbrella = Number(button.dataset.arg);
  _pfsUpdateInterests();
}

function profilesSimplePlay() {
  if (!_pfs?.interests) return;
  if (_pfs.playing) { _pfsStopPlay(); return; }
  const state = _pfs;
  state.period = 0;
  _pfsUpdateInterests();
  document.querySelector('.pfs-play .material-symbols-outlined').textContent = 'pause';
  state.playing = setInterval(() => {
    if (state !== _pfs || !document.querySelector('.pfs-bubbles')) { clearInterval(state.playing); state.playing = null; return; }
    if (state.period >= state.interests.periods.length - 1) { _pfsStopPlay(); return; }
    state.period += 1;
    _pfsUpdateInterests();
  }, INTEREST_PLAY_STEP_MS);
}

const _pfsRelayout = pfDebounce(() => { if (_pfs?.interests && document.querySelector('.pfs-bubbles')) _pfsUpdateInterests(); }, 200);
window.addEventListener('resize', _pfsRelayout);

/* ── to the full profile ─────────────────────────────────────────── */
function profilesSimpleToFull(link, event) {
  event.preventDefault();
  _pfsStopPlay();
  const clicks = Number(_pfsStored(FULL_CLICKS_KEY, '0')) + 1;
  _pfsStore(FULL_CLICKS_KEY, String(clicks));
  profilesGo(link.getAttribute('href'));
  if (clicks >= FULL_CLICKS_BEFORE_TIP && _pfsStored(DEFAULT_TIP_SHOWN_KEY, '0') !== '1') {
    _pfsStore(DEFAULT_TIP_SHOWN_KEY, '1');
    document.getElementById('pfs-default-overlay').classList.add('open');
  }
}

function profilesDefaultTip(button) {
  if (button.dataset.arg === 'full') setProfileSimpleFirst(false);
  document.getElementById('pfs-default-overlay').classList.remove('open');
}
