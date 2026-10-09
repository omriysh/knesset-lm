/**
 * app.js — entry module for the KnessetLM chat shell.
 *
 * Wires DOM events (textarea autoresize, Ctrl+Enter, submit click,
 * visibilitychange for reconnect), exposes inline-onclick handlers
 * (settings/help/stages-toggle) on `window`, configures user-input
 * panels with the submit callback, and attaches the global lazy-load
 * toggle listener.
 *
 * Loaded as `<script type="module">` from index.html. browser.js loads
 * separately as a non-module script and exposes `window.openProtocolBrowser`.
 *
 * Module layout (see Documentation/.../js-app-refactor.md):
 *
 *   state.js          — global state
 *   dom.js            — DOM refs + scroll/settings helpers
 *   util.js           — esc + question validation
 *   sse.js            — SSE async iterator
 *   transforms.js     — pure payload transforms
 *   session.js        — Session class + ExecutorState
 *   controller.js     — startQuery / submitResponse
 *   reconnect.js      — recover an interrupted stream
 *   events/dispatch.js, events/subgraph.js
 *   render/chat.js, stages.js, stage_card.js, details.js, lazy.js,
 *          user_input.js, citations.js
 */
import { state } from './state.js';
import { queryInput, submitBtn, clearQueryError } from './dom.js';
import { startQuery, submitResponse } from './controller.js';
import { configureUserInput } from './render/user_input.js';
import { attachLazyToggleListener } from './render/lazy.js';
import { attemptReconnect } from './reconnect.js';
import { refreshGeminiKeySettingsStatus } from './gemini_key.js';
import './research_intro.js';
import { refreshModelSettings } from './models.js';

// ── user_input → controller.submitResponse callback ────────────────────
configureUserInput({ onSubmit: submitResponse });

// ── tool-result + evidence-full lazy loaders ───────────────────────────
attachLazyToggleListener();

// ── Textarea auto-resize + Ctrl/⌘+Enter submit ─────────────────────────
queryInput.addEventListener('input', () => {
  queryInput.style.height = 'auto';
  queryInput.style.height = Math.max(48, Math.min(queryInput.scrollHeight, 160)) + 'px';
  clearQueryError();
});

queryInput.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    if (!state.running) startQuery();
  }
});
submitBtn.addEventListener('click', () => { if (!state.running) startQuery(); });

// ── Reconnect on tab refocus ───────────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.reconnectSessionId && !state.reconnecting) {
    attemptReconnect();
  }
});

// ── Settings overlay ───────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settings-overlay').classList.add('open');
  document.getElementById('toggle-stages-always').checked =
    localStorage.getItem('showStagesAlways') === 'true';
  document.getElementById('toggle-share-search').checked = window.shareIncludesSearch();
  document.getElementById('toggle-research-tab').checked = researchTabShown();
  refreshGeminiKeySettingsStatus();
  refreshModelSettings();
}
function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
}
function onStagesAlwaysToggle(el) {
  localStorage.setItem('showStagesAlways', el.checked ? 'true' : 'false');
}

// ── Help overlay (lazy-fetched markdown) ───────────────────────────────
let _helpLoaded = false;
async function openHelp() {
  document.getElementById('help-overlay').classList.add('open');
  if (_helpLoaded) return;
  try {
    const md = await fetch('/api/help').then(r => r.text());
    const content = document.getElementById('help-content');
    content.innerHTML = renderMarkdown(md);
    content.querySelector('h2').after(document.getElementById('help-steps-template').content.cloneNode(true));
    _helpLoaded = true;
  } catch (exc) {
    console.error('[app] help load failed:', exc);
    document.getElementById('help-content').innerHTML = '<p>שגיאה בטעינת העזרה.</p>';
  }
}
function closeHelp() {
  document.getElementById('help-overlay').classList.remove('open');
}

let _helpPromptsLoaded = false;
async function helpSetTab(tab) {
  document.querySelectorAll('#help-tabs .seg-btn').forEach(button => {
    const active = button.dataset.arg === tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.getElementById('help-content').hidden = tab !== 'about';
  const promptsPanel = document.getElementById('help-prompts');
  promptsPanel.hidden = tab !== 'prompts';
  if (tab !== 'prompts' || _helpPromptsLoaded) return;
  try {
    const prompts = await fetch('/api/help/prompts').then(r => r.json());
    promptsPanel.innerHTML = `<p class="help-prompts-lead">אלה ה-prompt-ים ששימשו את מודלי Gemini לבניית המידע שמוצג באתר, כלשונם.</p>`
      + prompts.map(prompt => `<section class="help-prompt">
          <h3>${_esc(prompt.title)}</h3>
          <p>${_esc(prompt.used_for)}</p>
          <pre dir="ltr">${_esc(prompt.text)}</pre>
        </section>`).join('');
    _helpPromptsLoaded = true;
  } catch (exc) {
    console.error('[app] help prompts load failed:', exc);
    promptsPanel.innerHTML = '<p>שגיאה בטעינת ה-prompt-ים.</p>';
  }
}

// ── Expose inline HTML handlers on window ──────────────────────────────
window.openSettings         = openSettings;
window.closeSettings        = closeSettings;
window.openHelp             = openHelp;
window.closeHelp            = closeHelp;
window.helpSetTab           = helpSetTab;
window.onStagesAlwaysToggle = onStagesAlwaysToggle;

