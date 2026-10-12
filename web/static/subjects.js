/**
 * subjects.js — the "נושאים" tab: the umbrella areas as a bubble playground → an area's subjects as a list
 * → a subject's background and approaches, with the MK themes behind the open approach at the bottom
 * (pfThemeCard, the profile theme sheet and citations).
 *
 *   /subjects                                  → the areas
 *   /subjects/area/{name}                      → that area's subjects
 *   /subjects/{subject_id}                     → a subject's background and approaches
 *   /subjects/{subject_id}/approach/{id}       → the same, with the themes behind one approach open
 *
 * Non-module script, loaded after profiles.js (pfEsc, pfFetch, pfThemeCard, pfSetThemeSource) and
 * profile_simple.js (BUBBLE_COLORS, UMBRELLA_SHORT).
 */

const SUBJECTS_PATH = '/subjects';
const SUBJECTS_API = '/api/subjects';
const UNKNOWN_PARTY = '?';
const PARTIES_SHOWN = 4;
const APPROACH_THEME_COLOR = 'var(--primary)';

let _sjRoute = { area: null, subject: null, approach: null };
let _sjRenderToken = 0;
let _sjAreas = null;
let _sjSubject = null;

/* ── routing ─────────────────────────────────────────────────────── */
function _sjParseRoute(pathname = location.pathname) {
  const area = pathname.match(/^\/subjects\/area\/([^/]+)\/?$/);
  if (area) return { area: decodeURIComponent(area[1]), subject: null, approach: null };
  const subject = pathname.match(/^\/subjects\/(\d{1,6})(?:\/approach\/(\d{1,7}))?\/?$/);
  if (subject) return { area: null, subject: Number(subject[1]), approach: subject[2] ? Number(subject[2]) : null };
  return { area: null, subject: null, approach: null };
}

const _sjAreaPath = name => `${SUBJECTS_PATH}/area/${encodeURIComponent(name)}`;
const _sjSubjectPath = id => `${SUBJECTS_PATH}/${id}`;
const _sjApproachPath = (subjectId, approachId) => `${SUBJECTS_PATH}/${subjectId}/approach/${approachId}`;

function subjectsCurrentPath() {
  if (_sjRoute.approach != null) return _sjApproachPath(_sjRoute.subject, _sjRoute.approach);
  if (_sjRoute.subject != null) return _sjSubjectPath(_sjRoute.subject);
  if (_sjRoute.area != null) return _sjAreaPath(_sjRoute.area);
  return SUBJECTS_PATH;
}

function subjectsApplyRoute() {
  _sjRoute = _sjParseRoute();
  _sjRender();
}

function subjectsShow() {
  if (!document.querySelector('#subjects-root > *')) _sjRender();
}

function subjectsGo(href) {
  const url = new URL(href, location.origin);
  history.pushState(null, '', url.pathname);
  _sjRoute = _sjParseRoute(url.pathname);
  _sjRender();
  document.getElementById('tab-subjects').scrollTop = 0;
}

function subjectsNav(link, event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  subjectsGo(link.getAttribute('href'));
}

const _sjLink = (href, inner, cls = '') => `<a href="${pfEsc(href)}" class="${cls}" data-click="subjectsNav">${inner}</a>`;
const _sjCrumbs = items => `<nav class="pf-crumbs">${items.map(([label, href]) => href ? _sjLink(href, pfEsc(label)) : `<span>${pfEsc(label)}</span>`).join(pfIcon('chevron_left'))}</nav>`;
const _sjAllAi = `<div class="pf-ai-band">${pfIcon('auto_awesome')}<span>כל התוכן בעמוד זה נוצר על ידי AI ועלול להכיל טעויות</span></div>`;

function _sjStagger(root) {
  root.querySelectorAll('.pf-stagger').forEach(list => [...list.children].forEach((child, i) => child.style.setProperty('--pf-i', i)));
}

async function _sjLoadAreas() {
  if (!_sjAreas) {
    _sjAreas = await pfFetch(SUBJECTS_API);
    _sjAreas.areas.sort((a, b) => b.subjects.length - a.subjects.length);
  }
  return _sjAreas;
}

async function _sjRender() {
  const root = document.getElementById('subjects-root');
  if (!root) return;
  const token = ++_sjRenderToken;
  if (!root.firstElementChild) root.innerHTML = `<div class="pf-page">${pfSpinner('טוען נושאים…')}</div>`;
  let html;
  try {
    html = _sjRoute.subject != null ? await _sjSubjectPage(_sjRoute.subject) : await _sjAreasPage();
  } catch (err) {
    html = `<div class="pf-page sj-page">${_sjTop([['כל התחומים', SUBJECTS_PATH], ['שגיאה']])}${pfErrorBox(err)}</div>`;
  }
  if (token !== _sjRenderToken) return;
  root.innerHTML = html;
  _sjStagger(root);
  if (document.querySelector('.sj-play')) _sjPlayStart();
  if (_sjRoute.subject != null && _sjRoute.approach != null && _sjSubject) _sjLoadApproach(_sjRoute.approach, false);
}

/* ── the areas: a bubble playground (bubble_play.js); tap an area for its subjects ── */
const PLAY_MIN_RADIUS_PX = 32;

let _sjPlay = null;

const _sjTop = crumbs => `<div class="sj-top">${_sjCrumbs(crumbs)}
  <button class="btn btn-ghost sj-how" type="button" data-click="subjectsProcess">${pfIcon('account_tree')}איך זה נוצר?</button></div>`;

async function _sjAreasPage() {
  await _sjLoadAreas();
  const area = _sjAreas.areas.find(a => a.name === _sjRoute.area);
  if (!area) return `<div class="pf-page sj-page sj-page--play">${_sjTop([['כל התחומים']])}
    <div class="sj-play"></div>
    <p class="sj-play-hint">גררו את הבועות · לחצו על בועה כדי להיכנס</p>
  </div>`;
  const most = Math.max(1, ...area.subjects.map(s => s.mk_count));
  return `<div class="pf-page sj-page">${_sjTop([['כל התחומים', SUBJECTS_PATH], [area.name]])}${_sjAllAi}
    <div class="sj-subject-list pf-stagger">${area.subjects.map(s => _sjLink(_sjSubjectPath(s.id), `
      <b>${pfEsc(s.name)}</b>
      <span class="sj-subject-desc">${pfEsc(s.description)}</span>
      <span class="pfs-approach-bar"><i style="width:${(s.mk_count / most) * 100}%"></i></span>
      <small>${pfNum(s.mk_count)} ח״כים · ${pfNum(s.approach_count)} גישות</small>`, 'sj-subject-tile')).join('')}</div>
  </div>`;
}

function _sjPlayStart() {
  const box = document.querySelector('.sj-play');
  const areas = _sjAreas.areas;
  box.innerHTML = areas.map(a => `<div class="sj-ball" title="${pfEsc(a.name)}"><b>${pfEsc(UMBRELLA_SHORT[a.name] || a.name)}</b><small>${pfNum(a.subjects.length)} נושאים</small></div>`).join('');
  _sjPlay = bubblePlayCreate(box, [...box.children], { onTap: i => subjectsGo(_sjAreaPath(areas[i].name)), minRadius: PLAY_MIN_RADIUS_PX });
  _sjPlaySize();
}

function _sjPlaySize() {
  const radii = bubblePlaySet(_sjPlay, _sjAreas.areas.map(a => a.subjects.length));
  _sjPlay.bodies.forEach((body, i) => {
    const [background, color] = BUBBLE_COLORS[Math.min(i, BUBBLE_COLORS.length - 1)];
    Object.assign(body.el.style, { background, color });
    body.el.style.setProperty('--fs', `${Math.max(9, Math.min(22, radii[i] / 3.6))}px`);
  });
}

window.addEventListener('resize', pfDebounce(() => { if (_sjPlay && _sjPlay.box.isConnected) _sjPlaySize(); }, 150));

/* ── a subject: background, approaches, and the open approach's themes ─ */
function _sjParties(partyCounts) {
  const parties = partyCounts.filter(([party]) => party !== UNKNOWN_PARTY);
  const shown = parties.slice(0, PARTIES_SHOWN);
  const rest = parties.length - shown.length;
  return `<span class="sj-parties">${shown.map(([party, count]) => `<span class="sj-party"><span>${pfEsc(party)}</span><b>${pfNum(count)}</b></span>`).join('')}${rest > 0 ? `<span class="sj-party">+${pfNum(rest)}</span>` : ''}</span>`;
}

const _sjMajority = (approach, approaches) => approach.mk_count === Math.max(...approaches.map(a => a.mk_count));
const _sjTag = majority => `<span class="pfs-tag ${majority ? 'pfs-tag--majority' : 'pfs-tag--minority'}">${majority ? 'דעת רוב' : 'דעת מיעוט'}</span>`;

async function _sjSubjectPage(subjectId) {
  const subject = _sjSubject && _sjSubject.id === subjectId ? _sjSubject : await pfFetch(`${SUBJECTS_API}/${subjectId}`);
  _sjSubject = subject;
  const total = Math.max(1, subject.mk_count);
  const overlap = subject.approaches.reduce((sum, a) => sum + a.mk_count, 0) > subject.mk_count;
  return `<div class="pf-page sj-page">
    ${_sjTop([['כל התחומים', SUBJECTS_PATH], [subject.umbrella, _sjAreaPath(subject.umbrella)]])}${_sjAllAi}
    <div class="pf-head"><div><h1>${pfEsc(subject.name)}</h1>
      <p>${pfNum(subject.mk_count)} חברי כנסת דיברו על הנושא · ${pfNum(subject.approaches.length)} גישות</p></div></div>
    <div class="sj-about">
      <section class="pfs-card"><h2 class="pfs-card-title">${pfIcon('info')}על הנושא</h2><p>${pfEsc(subject.description)}</p></section>
      <section class="pfs-card"><h2 class="pfs-card-title">${pfIcon('menu_book')}רקע</h2><p>${pfEsc(subject.background)}</p></section>
    </div>
    <section class="pfs-card sj-approaches-card">
      <h2 class="pfs-card-title">הגישות לנושא</h2>
      <p class="sj-approaches-hint">גישה = עמדה משותפת שעלתה מדברי כמה חברי כנסת · בחרו גישה כדי לראות מי החזיק/ה בה</p>
      <div class="sj-approach-list pf-stagger">${subject.approaches.map(a => `
        <button class="pfs-approach sj-approach${a.id === _sjRoute.approach ? ' on' : ''}" type="button" data-click="subjectsApproach" data-arg="${_esc(a.id)}">
          <span class="pfs-approach-main">
            <span class="pfs-approach-top"><b>${pfEsc(a.name)}</b>${_sjTag(_sjMajority(a, subject.approaches))}</span>
            <span class="sj-subject-desc">${pfEsc(a.summary)}</span>
            <span class="pfs-approach-bar"><i style="width:${(a.mk_count / total) * 100}%"></i></span>
            <span class="sj-approach-foot">${_sjParties(a.party_counts)}<small>${pfNum(a.mk_count)} מתוך ${pfNum(subject.mk_count)} ח״כים</small></span>
          </span>${pfIcon('expand_more')}</button>`).join('')}</div>
      ${overlap ? '<p class="sj-overlap-note">חבר/ת כנסת יכול/ה להופיע ביותר מגישה אחת, כשנושאים שונים מהפרופיל שלו/ה קובצו לגישות שונות, ולכן סכום הגישות גדול ממספר חברי הכנסת.</p>' : ''}
    </section>
    <section class="sj-approach-themes" id="sj-approach-themes" hidden></section>
  </div>`;
}

async function _sjLoadApproach(approachId, scroll) {
  const subject = _sjSubject;
  const section = document.getElementById('sj-approach-themes');
  const approach = subject.approaches.find(a => a.id === approachId);
  document.querySelectorAll('.sj-approach').forEach(button => button.classList.toggle('on', Number(button.dataset.arg) === approachId));
  if (!approach) { section.hidden = false; section.innerHTML = pfErrorBox(new Error('הגישה לא נמצאה')); return; }
  section.hidden = false;
  section.innerHTML = pfSpinner('טוען את חברי הכנסת…');
  let data;
  try { data = await pfFetch(`${SUBJECTS_API}/${subject.id}/approach/${approachId}`); } catch (err) { section.innerHTML = pfErrorBox(err); return; }
  if (_sjSubject !== subject || _sjRoute.approach !== approachId) return;
  const themes = data.themes.map(theme => ({ ...theme, color: APPROACH_THEME_COLOR, api: `${SUBJECTS_API}/theme/${theme.id}` }));
  const evidence = {};
  themes.forEach(theme => theme.evidence.forEach((e, n) => { evidence[`${theme.id}-${n}`] = e; }));
  pfSetThemeSource('subjects', { themes, quarters: data.quarters, evidence, api: null });
  section.innerHTML = `
    <div class="sj-approach-themes-head"><h2>${pfEsc(approach.name)}</h2>${_sjTag(_sjMajority(approach, subject.approaches))}</div>
    <p class="sj-approach-themes-sub">${pfNum(themes.length)} נושאים מפרופילי חברי הכנסת שקובצו לגישה הזו · לחצו לסיכום ולציטוטים</p>
    <div class="pf-themes sj-themes pf-stagger" data-pf-source="subjects">${themes.map(theme => pfThemeCard(theme, _sjThemeOwner(theme.mk))).join('')}</div>`;
  _sjStagger(section);
  if (scroll) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function subjectsApproach(button) {
  const approachId = Number(button.dataset.arg);
  if (_sjRoute.approach === approachId) {
    _sjRoute.approach = null;
    history.replaceState(null, '', _sjSubjectPath(_sjSubject.id));
    button.classList.remove('on');
    document.getElementById('sj-approach-themes').hidden = true;
    return;
  }
  _sjRoute.approach = approachId;
  history.replaceState(null, '', _sjApproachPath(_sjSubject.id, approachId));
  _sjLoadApproach(approachId, true);
}

function _sjThemeOwner(mk) {
  const inner = `${pfAvatar(mk.photo_url, mk.name)}<span class="sj-owner-text"><b>${pfEsc(mk.name)}</b><small>${pfEsc(mk.party)}</small></span>`;
  return mk.profile_url
    ? `<a class="sj-owner" href="${pfEsc(mk.profile_url)}" data-click="subjectsOpenProfile" title="לפרופיל">${inner}${pfIcon('arrow_back')}</a>`
    : `<button class="sj-owner" type="button" data-click="subjectsNotCandidate" data-name="${_esc(mk.name)}" data-party="${_esc(mk.party)}" data-photo="${_esc(mk.photo_url || '')}" title="אין פרופיל מלא">${inner}${pfIcon('info')}</button>`;
}

function subjectsNotCandidate(button) {
  const { name, party, photo } = button.dataset;
  const overlay = document.getElementById('sj-mk-overlay');
  overlay.querySelector('.dialog-body').innerHTML = `<div class="sj-mk-note">
    ${pfAvatar(photo || null, name)}<b>${pfEsc(name)}</b><small>${pfEsc(party)}</small>
    <p>חבר/ת הכנסת אינו/ה מועמד/ת לכנסת ה-26, ולכן אין לו/ה פרופיל מלא באתר.</p></div>`;
  overlay.classList.add('open');
}

function subjectsOpenFromSheet(link, event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  profilesCloseSheet();
  switchTab('subjects', { writeUrl: false });
  subjectsGo(link.getAttribute('href'));
}

function subjectsOpenProfile(link, event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
  event.preventDefault();
  switchTab('profiles', { writeUrl: false });
  profilesGo(link.getAttribute('href'));
}

/* ── "how was this made?" ─────────────────────────────────────────── */
let _sjPrompts = {};

const _sjProcessStep = (key, icon, title, counts, text) => `
  <div class="sj-step">
    <span class="sj-step-icon">${pfIcon(icon)}</span>
    <div class="sj-step-body">
      <div class="sj-step-top"><b>${title}</b>${counts ? `<span class="sj-step-count">${counts}</span>` : ''}</div>
      <p>${text}</p>
      <button class="sj-prompt-btn" type="button" data-click="subjectsPrompt" data-arg="${_esc(key)}">${pfIcon('code')}${_sjPrompts[key].length > 1 ? 'הצגת הפרומפטים' : 'הצגת הפרומפט'}</button>
      <div class="sj-prompt" hidden>${_sjPrompts[key].map(prompt => `<pre dir="ltr">${pfEsc(prompt)}</pre>`).join('')}</div>
    </div>
  </div>`;
const _sjArrow = `<span class="sj-flow-arrow">${pfIcon('arrow_downward')}</span>`;

async function subjectsProcess(scope) {
  const overlay = document.getElementById('sj-process-overlay');
  const body = overlay.querySelector('.dialog-body');
  const forProfiles = scope === 'profiles';
  overlay.querySelector('.sj-process-head b').textContent = forProfiles ? 'איך נוצרו הפרופילים?' : 'איך נוצרו הנושאים?';
  body.innerHTML = pfSpinner('טוען…');
  overlay.classList.add('open');
  let counts;
  try { counts = await pfFetch(`${SUBJECTS_API}/process`); } catch (err) { body.innerHTML = pfErrorBox(err); return; }
  _sjPrompts = counts.prompts;
  const opinionsAndThemes = `    ${_sjProcessStep('opinions', 'record_voice_over', 'חילוץ עמדות מכל דיון', `${pfNum(counts.opinions)} עמדות`,
      `המודל קרא כל דיון (${pfNum(counts.protocols)} דיונים) ורשם כל עמדה שדובר/ת הביע/ה, עם ציטוט מהדיון.`)}
    ${_sjArrow}
    ${_sjProcessStep('themes', 'person', 'קיבוץ העמדות של כל ח״כ לנושאים', `${pfNum(counts.themes)} נושאים`,
      `המודל קיבל את כל העמדות של חבר/ת כנסת אחד/ת וקיבץ אותן לנושאים שחוזרים בדבריו/ה. אלה הנושאים שבעמוד הפרופילים.`)}`;
  if (forProfiles) {
    body.innerHTML = `<p class="sj-flow-intro">רשימות המועמדים נלקחו מאתר ועדת הבחירות המרכזית. ההצבעות, הצעות החוק והכהונות מגיעים ישירות מאתר הכנסת, והנוכחות בוועדות מפרוטוקולי הדיונים.</p>
    <p class="sj-flow-intro">את הנושאים והעמדות יצר מודל השפה gemini-3.8-flash מתוך הדיונים בוועדות ${pfInLabel(pfKnessetLabel(counts.knesset_num))}.</p>
    <div class="sj-flow">${opinionsAndThemes}</div>
    <p class="sj-flow-intro sj-flow-outro">הפרופילים המקוצרים מציגים גם נתונים שנוצרו עבור לשונית הנושאים (תחומים, נושאים משותפים וגישות), בתהליך שמוסבר שם.</p>
    <button class="btn btn-ghost sj-how" type="button" data-click="subjectsProcess">${pfIcon('account_tree')}איך נוצרו הנושאים?</button>`;
    return;
  }
  body.innerHTML = `<p class="sj-flow-intro">כל שלב הוא משימה שמודל השפה gemini-3.8-flash ביצע על התוצרים של השלב הקודם, החל מהדיונים בוועדות ${pfInLabel(pfKnessetLabel(counts.knesset_num))}.</p>
  <div class="sj-flow">
    ${opinionsAndThemes}
    ${_sjArrow}
    ${_sjProcessStep('subjects', 'hub', 'הצעת נושאים משותפים ותחומים', `${pfNum(counts.subjects)} נושאים · ${pfNum(counts.areas)} תחומים`,
      'המודל קיבל את הנושאים של חברי הכנסת בקבוצות והציע נושאים משותפים, כל אחד עם תחום-על, תיאור ורקע. אחר כך ההצעות של כל הקבוצות אוחדו לרשימה אחת.')}
    ${_sjArrow}
    ${_sjProcessStep('assign', 'move_down', 'שיוך הנושאים של כל ח״כ לנושאים המשותפים', '',
      'המודל עבר על כל נושא מהפרופילים ובחר לאילו נושאים משותפים הוא שייך. נושא אחד יכול להשתייך לכמה נושאים משותפים.')}
    ${_sjArrow}
    ${_sjProcessStep('background', 'fact_check', 'בדיקת הרקע', '',
      'מודל נפרד בדק שכל עובדה ברקע של נושא מופיעה בסיכומי הנושאים שהרקע נכתב מהם. רקע שנפסל תוקן ונבדק שוב.')}
    ${_sjArrow}
    ${_sjProcessStep('approaches', 'call_split', 'חלוקה לגישות', `${pfNum(counts.approaches)} גישות`,
      'בכל נושא משותף, המודל חילק את הנושאים של חברי הכנסת לפי העמדה שהם נוקטים, וכתב לכל גישה שם וסיכום.')}
  </div>`;
}

function subjectsPrompt(button) {
  const prompt = button.nextElementSibling;
  prompt.hidden = !prompt.hidden;
  button.classList.toggle('on', !prompt.hidden);
}

function subjectsCloseDialogs() {
  document.querySelectorAll('.sj-overlay.open').forEach(overlay => overlay.classList.remove('open'));
}

document.addEventListener('keydown', event => { if (event.key === 'Escape') subjectsCloseDialogs(); });
