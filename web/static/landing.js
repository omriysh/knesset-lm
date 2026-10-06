/**
 * landing.js — the "בצ'אט שלכם" tab (/): the live-quote demo, the review prompt builder and the connect-to-chat steps.
 *
 * Non-module script, loaded before actions.js (whose PAGE_ACTIONS call these functions) and tabs.js
 * (switchTab starts the demo).
 */

const LANDING_MCP_URL = 'https://meorav.com/mcp';
const LANDING_DEMO_STEP_MS = 4500;

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

const LANDING_CANDIDATES_URL = 'https://www.gov.il/he/pages/candidates-lists-26';

const LANDING_DEPTHS = {
  deep:  { candidates: 5 },
  quick: { candidates: 3 },
};
const LANDING_GUIDELINES_ONLY_PROMPT = `I will ask you questions to help me prepare for the 2026 Israel elections. Use the Meorav MCP for Knesset data (protocols, bills and votes of members and parties), and search the web for current news and statements. Candidates: ${LANDING_CANDIDATES_URL}
When I ask about a party, filter Meorav searches both on the party and on its leading candidates. Some parties that run to the 26th elections didn't exist in the 25th knesset, and some candidates were not members of the 25th knesset.

${LANDING_PROMPT_GUIDELINES}

Reply briefly that you are ready, and wait for my first question.`;

const _landingBuilder = { order: 'party', depth: 'deep' };

const _LANDING_PARTIES_STEP_OPENER = 'Present the user with a list of the large parties, and ask them if they wish to profile all of them or only some (recommend to go with all of them, for a balanced read).';
const _LANDING_MEORAV_SEARCH_GUIDELINES = `Candidates: ${LANDING_CANDIDATES_URL}
Guidelines for Meorav searches in this step:
When using a keyword search, if you don't get relevant results, use your knowledge and terminology you encountered online to try and expand the search.
Filter both on party and on specific members. Some parties that run to the 26th elections didn't exist in the 25th knesset, and some candidates were not members of the 25th knesset.`;

function _landingQuickResearchStep() {
  return `Step 3
${_LANDING_PARTIES_STEP_OPENER} Then build one comparison table: a row for each subject and a column for each selected party. In each cell, write the main position of the party on the subject in one line, with its best source.
To find the position of a party on a subject, take the first ${LANDING_DEPTHS.quick.candidates} contenders from the list of the party. For each of them, check the Knesset sources and search the web for them referring to the subject. Combine the approach of the candidates and present it as the approach of the party. Keep it short; the table is a map, not a profile.
${_LANDING_MEORAV_SEARCH_GUIDELINES}`;
}

function _landingResearchStep(builder) {
  if (builder.depth === 'quick') return _landingQuickResearchStep();
  const depth = LANDING_DEPTHS[builder.depth];
  const order = builder.order === 'party'
    ? 'Profile each of the selected parties based on the list of subjects. Output each profile to the user.'
    : 'Then go over the subjects one at a time. For each subject, find the approach of each selected party and present them side by side, before moving to the next subject. After each subject, ask the user if they want to dig deeper before moving on.';
  return `Step 3
${_LANDING_PARTIES_STEP_OPENER} ${order}
In order to "profile a party" on a subject, take the first ${depth.candidates} contenders from the list of each party. For each of them, check the Knesset sources and search the web for them referring to the subject. Combine the approach of the candidates and present it as the approach of the party. Keep track of your sources of information and present them to the user. Dive deep and present well-based answers.
If you can, profile each party and even each of the top candidates using a new agent.
${_LANDING_MEORAV_SEARCH_GUIDELINES}`;
}

function _landingSummaryStep(builder) {
  if (builder.depth === 'quick') return `Step 4
Ask the user if they want to dig deeper into any party, subject or cell of the table. If so, profile it fully, with quotes and sources. Use visualizations when relevant to create a clear image of the comparison.
Encourage the user to share their results and thoughts at the end of the process.`;
  if (builder.order === 'party') return `Step 4
Ask the user if they are interested in any comparisons between the different parties. If so, use the profiles you created and compare. Use visualizations when relevant to create a clear image of the comparison.
Encourage the user to share their results and thoughts at the end of the process.`;
  return `Step 4
Summarize the comparison across all the subjects. Use visualizations when relevant to create a clear image of the comparison. Ask the user if they want a full profile of any of the parties.
Encourage the user to share their results and thoughts at the end of the process.`;
}

function landingBuildReviewPrompt(builder) {
  return `Your goal is to help prepare for the 2026 Israel elections. You will be working with the user to understand what topics they find important to base their decision on, and then help them research where each party stands. Your value comes from your unique logic flow, and the ability to use the Meorav MCP. The process aims to expose the user to new sources of data, it's up to the user to judge credibility and make conclusions in the end; give them the recommendation to follow up personally on leads they find interesting in this conversation. You are here to help with that. It's fine if the user doesn't make a decision at the end of the process.

The process will be divided into the following steps:

Step 1
Explain the goal and the process to the user, as simply as possible.
Instruct the user to be critical of the data presented to them, and to ask for further digging whenever they want. Encourage further self reading and verifying of sources.

Step 2
Present the user a suggestion of a list of topics to profile candidates based on. Search the web to generate an unbiased list as possible. The goal of this step is to create an aligned list with the subjects the user finds important to them when selecting which party to vote for.
Offer the user the chance to criticise the list - they may remove or add anything they want, merge or split subjects. Interview them briefly about their changes, asking meaningful questions regarding the subjects they wish to focus on. Don't limit the user's important topic choice, but suggest that a smaller choice will help focus the rest of the process.

${_landingResearchStep(builder)}

${_landingSummaryStep(builder)}

${LANDING_PROMPT_GUIDELINES}
Before you begin a step, explain to the user what the step will be and the logic behind it.`;
}

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

function _landingCopiedThenInstall(name) {
  document.getElementById('lp-copied-name').textContent = `"${name}"`;
  document.getElementById('lp-copied-banner').hidden = false;
  setTimeout(landingGoInstall, 600);
}

function landingCopyReview(button) {
  _landingCopy(landingBuildReviewPrompt(_landingBuilder), button, '<span class="material-symbols-outlined">check</span>הועתק');
  _landingCopiedThenInstall('סקירה מקיפה');
}

function landingCopyGuidelines(button) {
  _landingCopy(LANDING_GUIDELINES_ONLY_PROMPT, button, '<span class="material-symbols-outlined">check</span>הועתק');
  _landingCopiedThenInstall('ההנחיות');
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

function _renderLandingBuilder() {
  const b = _landingBuilder;
  document.querySelectorAll('[data-lp-setting]').forEach(seg => {
    seg.querySelectorAll('button[data-arg]').forEach(btn => btn.classList.toggle('active', btn.dataset.arg === `${seg.dataset.lpSetting}:${b[seg.dataset.lpSetting]}`));
  });

  const flowmap = document.getElementById('lp-flowmap');
  flowmap.dataset.order = b.order;
  flowmap.dataset.depth = b.depth;
  flowmap.querySelectorAll('[data-click="landingBuilderSwapOrder"]').forEach(swap => { swap.disabled = b.depth === 'quick'; });
  document.getElementById('lp-review-prompt').innerHTML = _landingPromptHtml(landingBuildReviewPrompt(b));
}

function landingBuilderSet(button) {
  const [setting, value] = button.dataset.arg.split(':');
  _landingBuilder[setting] = value;
  _renderLandingBuilder();
}

function landingBuilderSwapOrder() {
  _landingBuilder.order = _landingBuilder.order === 'party' ? 'topic' : 'party';
  _renderLandingBuilder();
}

function _landingClaudeInstallLink() {
  return 'https://claude.ai/customize/connectors?modal=add-custom-connector'
    + `&connectorName=${encodeURIComponent('Meorav Yerushalmi')}&connectorUrl=${encodeURIComponent(LANDING_MCP_URL)}`;
}

document.addEventListener('DOMContentLoaded', () => {
  if (!document.getElementById('lp-review-prompt')) return;
  _renderLandingBuilder();
  document.getElementById('lp-guidelines-prompt').innerHTML = _landingPromptHtml(LANDING_GUIDELINES_ONLY_PROMPT);
  const installLink = document.getElementById('lp-claude-install');
  if (installLink) installLink.href = _landingClaudeInstallLink();
});

function landingScrollToSection(event, sectionId) {
  event.preventDefault();
  document.getElementById(sectionId)
    .scrollIntoView({ behavior: _landingReducedMotion() ? 'auto' : 'smooth', block: 'start' });
}
