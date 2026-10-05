/**
 * landing.js — the "בצ'אט שלכם" tab (/): the live-quote demo, the prompt cards and the connect-to-chat steps.
 *
 * Non-module script, loaded before actions.js (whose PAGE_ACTIONS call these functions) and tabs.js
 * (switchTab starts the demo).
 */

const LANDING_MCP_URL = 'https://meorav.com/mcp';
const LANDING_DEMO_STEP_MS = 4500;
const LANDING_PEEK_CLOSE_MS = 300;

const LANDING_DEMO_SOURCES = [
  { who: 'דוד ביטן · הליכוד', where: 'ועדת הכלכלה · 31.05.2026',
    quote: 'אתה לא יכול לתעדף בלי שיש לך תחבורה ציבורית. עם כל הכבוד, נת"צים זה לא הדבר הכי חשוב בתחבורה ציבורית.',
    url: '/protocols?meeting=2243047&speech=94&offset=0&length=100' },
  { who: 'שלי טל מירון · יש עתיד', where: 'ועדת הכלכלה · 24.05.2026',
    quote: 'זה נחמד מאוד שדברים כתובים על הדף והם כתובים נורא יפה, אבל אם בפועל אנחנו לא מקבלים נתונים, התפקיד שלנו פה זה לעשות פיקוח פרלמנטרי על עבודת הממשלה.',
    url: '/protocols?meeting=2243051&speech=96&offset=0&length=200' },
  { who: 'אורי מקלב · יהדות התורה', where: 'ועדת הכספים · 10.06.2026',
    quote: 'אני יזמתי הצעת חוק והשקעתי רבות בנושא הזה של לא לקשור את הרכב, את הנסיעה ברכב, לדמי נסיעות שנותנים במשכורת.',
    url: '/protocols?meeting=2243927&speech=15&offset=0&length=166' },
];

const LANDING_PROMPT_GUIDELINES = `Guidelines and rules:
Give short and concise answers.
Answer only in Hebrew.
Eliminate any political bias; be as balanced as possible.
Always provide sources to the answers you write. Provide quotes when relevant, integrating the sources into the answer and not just writing a list at the end. Split sources into 3 categories: Knesset data, journalism, social media. Prefer Knesset sources over anything else, and then prefer recent information over old.
Your job is to illustrate the opinions and narratives of the candidates, present them as such and help inspire critical thinking. Make sure to frame narratives as narratives.
Don't be critical of the user's world view unless they ask you to. If you think it's important for the process, ask the user before you do so. It's better to challenge and ask questions than to provide criticism.`;

const LANDING_FULL_REVIEW_PROMPT = `Your goal is to help prepare for the 2026 Israel elections. You will be working with the user to understand what topics they find important to base their decision on, and then help them research where each party stands. Your value comes from your unique logic flow, and the ability to use the Meorav MCP. The process aims to expose the user to new sources of data, it's up to the user to judge credibility and make conclusions in the end; give them the recommendation to follow up personally on leads they find interesting in this conversation. You are here to help with that. It's fine if the user doesn't make a decision at the end of the process.

The process will be divided into the following steps:

Step 1
Explain the goal and the process to the user, as simply as possible.
Instruct the user to be critical of the data presented to them, and to ask for further digging whenever they want. Encourage further self reading and verifying of sources.

Step 2
Present the user a suggestion of a list of topics to profile candidates based on. Search the web to generate an unbiased list as possible. The goal of this step is to create an aligned list with the subjects the user finds important to them when selecting which party to vote for.
Offer the user the chance to criticise the list - they may remove or add anything they want, merge or split subjects. Interview them briefly about their changes, asking meaningful questions regarding the subjects they wish to focus on. Don't limit the user's important topic choice, but suggest that a smaller choice will help focus the rest of the process.

Step 3
Present the user with a list of the large parties, and ask them if they wish to profile all of them or only some (recommend to go with all of them, for a balanced read). Profile each of the selected parties based on the list of subjects. Output each profile to the user.
In order to "profile a party", take the first 5 contenders from the list of each party. For each of them, check the Knesset sources and search the web for them referring to the subject. Combine the approach of the candidates and present it as the approach of the party. Keep track of your sources of information and present them to the user. Dive deep and present well-based answers.
If you can, profile each party and even each of the top candidates using a new agent.
Candidates: https://www.gov.il/he/pages/candidates-lists-26
Guidelines for Meorav searches in this step:
When using a keyword search, if you don't get relevant results, use your knowledge and terminology you encountered online to try and expand the search.
Filter both on party and on specific members. Some parties that run to the 26th elections didn't exist in the 25th knesset, and some candidates were not members of the 25th knesset.

Step 4
Ask the user if they are interested in any comparisons between the different parties. If so, use the profiles you created and compare. Use visualizations when relevant to create a clear image of the comparison.
Encourage the user to share their results and thoughts at the end of the process.

${LANDING_PROMPT_GUIDELINES}
Before you begin a step, explain to the user what the step will be and the logic behind it.`;

const LANDING_CASES = [
  { name: 'סקירה מקיפה', time: '~30 דק\'', feature: true,
    pitch: 'בוחרים את הנושאים שחשובים לכם, ומקבלים את העמדות של כל המפלגות שתבחרו, עם מקורות.',
    steps: ['בונים יחד רשימת נושאים שלכם', 'פרופיל לכל מפלגה, עם ציטוטים', 'השוואות, אם תרצו'],
    prompt: LANDING_FULL_REVIEW_PROMPT },
  { name: 'מה הם עשו בפועל', time: '~10 דק\'',
    pitch: 'בוחרים מפלגה ונושא, ובודקים איך מה שהיא אומרת היום מסתדר עם מה שאמרו חבריה בכנסת האחרונה.',
    steps: ['אתם בוחרים מפלגה ונושא', 'ה-AI מחפש מה אמרו והצביעו חבריה', 'מקבלים התאמות וסתירות, עם מקור לכל אחת'],
    prompt: `Help me check how a party's current message on a topic compares with what its members said and did in the 25th Knesset. Ask me which party and topic. Use the Meorav MCP to search protocols (filter by party and by its leading members), bills and plenum votes. Search the web for the party's current message. Present where they match and where they differ, each point with a quote and a link. Encourage me to read the sources myself.

${LANDING_PROMPT_GUIDELINES}` },
  { name: 'ראיתם משהו בחדשות?', time: '~2 דק\'',
    pitch: 'ציטוט, טענה או כותרת. מדביקים, ובודקים מה באמת נאמר ומה אמרו מפלגות אחרות באותו נושא.',
    steps: ['מדביקים את מה שראיתם', 'ה-AI מחפש את המקור בכנסת', 'מקבלים את הציטוט המלא, בהקשר'],
    prompt: `I saw a claim or a quote, which I paste at the end. Use the Meorav MCP to find what was actually said in the Knesset protocols, bills or votes, and show me the original quote in context with a link. Then briefly show what members of other parties said on the same topic. Say clearly if you could not find a source.

${LANDING_PROMPT_GUIDELINES}

The claim: ` },
];



let _landingDemoTimers = [];
let _landingDemoStarted = false;

function _landingReducedMotion() {
  return matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function landingShowSource(index) {
  const card = document.getElementById('lp-source');
  if (!card) return;
  const source = LANDING_DEMO_SOURCES[index];
  document.querySelectorAll('.lp-cite').forEach(c => c.classList.toggle('on', c.dataset.arg === String(index)));
  card.classList.remove('lit');
  card.classList.add('hide');
  setTimeout(() => {
    document.getElementById('lp-src-who').textContent = source.who;
    document.getElementById('lp-src-where').textContent = source.where;
    document.getElementById('lp-src-quote').textContent = source.quote;
    document.getElementById('lp-src-link').href = source.url;
    card.classList.remove('hide');
    requestAnimationFrame(() => requestAnimationFrame(() => card.classList.add('lit')));
  }, 180);
}

function landingPickSource(el) {
  _landingDemoTimers.forEach(clearTimeout);
  landingShowSource(Number(el.dataset.arg));
}

function landingStartDemo() {
  if (_landingDemoStarted) return;
  _landingDemoStarted = true;
  if (_landingReducedMotion()) { landingShowSource(0); return; }
  LANDING_DEMO_SOURCES.forEach((_, i) => {
    _landingDemoTimers.push(setTimeout(() => landingShowSource(i), 2800 + i * LANDING_DEMO_STEP_MS));
  });
}

function _landingFallbackCopy(text) {
  const area = document.createElement('textarea');
  area.value = text;
  document.body.appendChild(area);
  area.select();
  try { document.execCommand('copy'); } catch (exc) { console.warn('[landing] copy failed:', exc); }
  area.remove();
}

function _landingCopy(text, button, doneHtml) {
  const original = button.innerHTML;
  const showDone = () => {
    button.innerHTML = doneHtml;
    button.classList.add('done');
    setTimeout(() => { button.innerHTML = original; button.classList.remove('done'); }, 1800);
  };
  if (!navigator.clipboard) { _landingFallbackCopy(text); showDone(); return; }
  navigator.clipboard.writeText(text).then(showDone, (exc) => {
    console.warn('[landing] clipboard refused, selecting instead:', exc);
    _landingFallbackCopy(text);
    showDone();
  });
}

function landingCopyMcpUrl(button) {
  _landingCopy(LANDING_MCP_URL, button, '✓');
}

function landingGoInstall() {
  const section = document.getElementById('lp-install');
  section.scrollIntoView({ behavior: _landingReducedMotion() ? 'auto' : 'smooth', block: 'start' });
  const card = document.getElementById('lp-install-card');
  card.classList.remove('flash');
  void card.offsetWidth;
  card.classList.add('flash');
}

function landingCopyPrompt(button) {
  const useCase = LANDING_CASES[Number(button.dataset.arg)];
  _landingCopy(useCase.prompt, button, '<span class="material-symbols-outlined">check</span>הועתק');
  document.getElementById('lp-copied-name').textContent = `"${useCase.name}"`;
  document.getElementById('lp-copied-banner').hidden = false;
  setTimeout(landingGoInstall, 600);
}

function landingSetInstallTab(button) {
  document.querySelectorAll('#lp-install-tabs .seg-btn').forEach(b => {
    b.classList.toggle('active', b === button);
    b.setAttribute('aria-selected', b === button);
  });
  document.querySelectorAll('[data-lp-panel]').forEach(p => { p.hidden = p.dataset.lpPanel !== button.dataset.arg; });
}

function _landingEscape(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* Prompt text as reading markup: a "Step 1" line or a short line ending in ':' heads its block; the lines
   under a ':' heading are a list, other lines are paragraphs. */
function _landingPromptHtml(prompt) {
  return prompt.trim().split(/\n\s*\n/).map(block => {
    const lines = block.split('\n').map(line => line.trim()).filter(Boolean);
    const isHeading = /^Step \d+$/.test(lines[0]) || (lines[0].endsWith(':') && lines[0].length < 40);
    const heading = isHeading ? `<h4>${_landingEscape(lines.shift())}</h4>` : '';
    const listed = isHeading && heading.includes(':');
    const body = listed
      ? (lines.length ? `<ul>${lines.map(line => `<li>${_landingEscape(line)}</li>`).join('')}</ul>` : '')
      : lines.map(line => `<p>${_landingEscape(line)}</p>`).join('');
    return heading + body;
  }).join('');
}

function _renderLandingCases() {
  const grid = document.getElementById('lp-cases');
  if (!grid) return;
  grid.innerHTML = LANDING_CASES.map((useCase, i) => `
    <article class="lp-case${useCase.feature ? ' feature' : ''}">
      <div class="lp-case-top">
        <h3>${useCase.name}</h3>
        <span class="lp-time">${useCase.time}</span>
      </div>
      <p>${useCase.pitch}</p>
      <ol class="lp-steps">${useCase.steps.map(step => `<li>${step}</li>`).join('')}</ol>
      <button class="btn btn-primary btn-block lp-copy" type="button" data-click="landingCopyPrompt" data-arg="${encodeURIComponent(i)}">
        <span class="material-symbols-outlined">content_copy</span>העתקת prompt
      </button>
      <details class="lp-peek" data-lp-peek><summary>ה-prompt המלא</summary><div class="lp-prompt">${_landingPromptHtml(useCase.prompt)}</div></details>
    </article>`).join('');
  grid.querySelectorAll('[data-lp-peek]').forEach(peek => peek.addEventListener('toggle', () => _onLandingPeekToggle(grid)));
}

/* While a prompt is open the cards keep their own heights, so only its card grows. Closing keeps that
   until the close animation ends, or the other cards would stretch to the closing card and shrink with it. */
let _landingPeekCloseTimer = null;
function _onLandingPeekToggle(grid) {
  clearTimeout(_landingPeekCloseTimer);
  if (grid.querySelector('[data-lp-peek][open]')) { grid.classList.add('peek-open'); return; }
  _landingPeekCloseTimer = setTimeout(() => grid.classList.remove('peek-open'), LANDING_PEEK_CLOSE_MS);
}

function _landingClaudeInstallLink() {
  return 'https://claude.ai/customize/connectors?modal=add-custom-connector'
    + `&connectorName=${encodeURIComponent('Meorav Yerushalmi')}&connectorUrl=${encodeURIComponent(LANDING_MCP_URL)}`;
}

document.addEventListener('DOMContentLoaded', () => {
  _renderLandingCases();
  const installLink = document.getElementById('lp-claude-install');
  if (installLink) installLink.href = _landingClaudeInstallLink();
});

function landingScrollToCases(event) {
  event.preventDefault();
  document.getElementById('lp-cases-section')
    .scrollIntoView({ behavior: _landingReducedMotion() ? 'auto' : 'smooth', block: 'start' });
}
