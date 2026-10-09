/**
 * landing.js — the "בצ'אט שלכם" tab (/chat): the connect-to-chat steps and the review prompt builder.
 *
 * Non-module script, loaded before actions.js (whose PAGE_ACTIONS call these functions).
 */

const LANDING_MCP_URL = 'https://meorav.com/mcp';

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

const _landingBuilder = { depth: 'deep' };

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
  return `Step 3
${_LANDING_PARTIES_STEP_OPENER}
Then ask the user how they want to go over the results: party by party, or subject by subject.
Party by party: profile each of the selected parties based on the list of subjects. Output each profile to the user.
Subject by subject: go over the subjects one at a time. For each subject, find the approach of each selected party and present them side by side, before moving to the next subject. After each subject, ask the user if they want to dig deeper before moving on.
In order to "profile a party" on a subject, take the first ${LANDING_DEPTHS.deep.candidates} contenders from the list of each party. For each of them, check the Knesset sources and search the web for them referring to the subject. Combine the approach of the candidates and present it as the approach of the party. Keep track of your sources of information and present them to the user. Dive deep and present well-based answers.
If you can, profile each party and even each of the top candidates using a new agent.
${_LANDING_MEORAV_SEARCH_GUIDELINES}`;
}

function _landingSummaryStep(builder) {
  if (builder.depth === 'quick') return `Step 4
Ask the user if they want to dig deeper into any party, subject or cell of the table. If so, profile it fully, with quotes and sources. Use visualizations when relevant to create a clear image of the comparison.
Encourage the user to share their results and thoughts at the end of the process.`;
  return `Step 4
If you went party by party, ask the user if they are interested in any comparisons between the different parties. If so, use the profiles you created and compare.
If you went subject by subject, summarize the comparison across all the subjects, and ask the user if they want a full profile of any of the parties.
Use visualizations when relevant to create a clear image of the comparison.
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

function _landingReducedMotion() {
  return matchMedia('(prefers-reduced-motion: reduce)').matches;
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

const LANDING_STEP_COPY_TEXTS = { name: 'Meorav Yerushalmi', url: LANDING_MCP_URL };
const LANDING_STEP_OPEN_MS = 500;
const LANDING_STEP_EDGE_GAP_FRACTION = 0.2;

function _landingEaseInOut(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

/* Where the next step (its card and arrowhead) lands once its open transition ends: open it with
   transitions off, measure, then close it again so the real open still animates. */
function _landingOpenedStepRect(track, next) {
  track.classList.add('lp-measure');
  next.classList.add('home-graph--open');
  const rect = next.querySelector('.lp-step-body').getBoundingClientRect();
  next.classList.remove('home-graph--open');
  void track.offsetWidth;
  track.classList.remove('lp-measure');
  void track.offsetWidth;
  return rect;
}

/* How far the track must scroll so the step keeps a gap of 20% of the track's width from its left edge
   (a row of steps), or of its height from its bottom (a column on mobile). */
function _landingStepScrollNeeded(track, stepRect, isRow) {
  const trackRect = track.getBoundingClientRect();
  if (isRow) return Math.min(0, stepRect.left - (trackRect.left + trackRect.width * LANDING_STEP_EDGE_GAP_FRACTION));
  return Math.max(0, stepRect.bottom - (trackRect.bottom - trackRect.height * LANDING_STEP_EDGE_GAP_FRACTION));
}

function _landingScrollTrack(track, isRow, distance, behavior) {
  track.scrollBy(isRow ? { left: distance, behavior } : { top: distance, behavior });
}

/* Scrolls the track along with the open transition, then corrects whatever the still-growing track
   could not scroll yet. */
function _landingKeepStepInView(track, next, isRow, stepRect) {
  const distance = _landingStepScrollNeeded(track, stepRect, isRow);
  if (!distance) return;
  if (_landingReducedMotion()) { _landingScrollTrack(track, isRow, distance, 'auto'); return; }
  const axis = isRow ? 'scrollLeft' : 'scrollTop';
  const startScroll = track[axis];
  const startTime = performance.now();
  const frame = (now) => {
    const progress = Math.min(1, (now - startTime) / LANDING_STEP_OPEN_MS);
    track[axis] = startScroll + distance * _landingEaseInOut(progress);
    if (progress < 1) { requestAnimationFrame(frame); return; }
    const remaining = _landingStepScrollNeeded(track, next.querySelector('.lp-step-body').getBoundingClientRect(), isRow);
    if (Math.abs(remaining) > 1) _landingScrollTrack(track, isRow, remaining, 'smooth');
  };
  requestAnimationFrame(frame);
}

function landingStepNext(el) {
  const step = el.closest('.lp-step');
  const next = step.nextElementSibling;
  if (!next || next.classList.contains('home-graph--open')) return;
  const track = step.closest('.lp-steps');
  const isRow = getComputedStyle(track).flexDirection === 'row';
  const stepRect = _landingOpenedStepRect(track, next);
  step.classList.add('lp-step--done');
  next.classList.add('home-graph--open');
  _landingKeepStepInView(track, next, isRow, stepRect);
}

function landingScrollToSection(event, sectionId) {
  event.preventDefault();
  document.getElementById(sectionId).scrollIntoView({ behavior: _landingReducedMotion() ? 'auto' : 'smooth', block: 'start' });
}

function landingCopyField(button) {
  _landingCopy(LANDING_STEP_COPY_TEXTS[button.dataset.arg], button.querySelector('.material-symbols-outlined'), 'check');
}

function _landingGoInstall() {
  const section = document.getElementById('lp-install');
  section.scrollIntoView({ behavior: _landingReducedMotion() ? 'auto' : 'smooth', block: 'start' });
  document.querySelectorAll('.lp-step:first-child .home-next-card').forEach(step => {
    step.classList.remove('flash');
    void step.offsetWidth;
    step.classList.add('flash');
  });
}

function _landingCopiedThenInstall(name) {
  document.getElementById('lp-copied-name').textContent = `"${name}"`;
  document.getElementById('lp-copied-banner').hidden = false;
  setTimeout(_landingGoInstall, 600);
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
  document.querySelectorAll('[data-lp-panel]').forEach(p => { p.classList.toggle('lp-panel-off', p.dataset.lpPanel !== button.dataset.arg); });
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
  flowmap.dataset.depth = b.depth;
  document.getElementById('lp-review-prompt').innerHTML = _landingPromptHtml(landingBuildReviewPrompt(b));
}

function landingBuilderSet(button) {
  const [setting, value] = button.dataset.arg.split(':');
  if (_landingBuilder[setting] === value) return;
  _landingBuilder[setting] = value;
  _renderLandingBuilder();
  _landingBumpCopyButton();
}

function _landingBumpCopyButton() {
  const copy = document.querySelector('.lp-copy');
  copy.classList.remove('lp-copy--bump');
  void copy.offsetWidth;
  copy.classList.add('lp-copy--bump');
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
  _landingObserveReveals();
});

function _landingObserveReveals() {
  const sections = document.querySelectorAll('.lp-reveal');
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => entry.target.classList.toggle('lp-in', entry.isIntersecting));
  }, { root: sections[0].closest('.landing'), rootMargin: '-20% 0px' });
  sections.forEach(section => observer.observe(section));
}
