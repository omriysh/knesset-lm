/**
 * render/lazy.js — lazy fetch + unload of tool results and evidence full text.
 * Registers a single global `toggle` listener that dispatches to the right loader
 * and re-evaluates the unload timer for all loaded tool-result panels.
 */
import { esc } from '../util.js';
import { renderEvidenceFull } from './citations.js';

const TOOL_RESULT_UNLOAD_MS = 30_000;

function isToolPanelVisible(el) {
  if (!el.open) return false;
  let node = el.parentElement;
  while (node) {
    if (node.tagName === 'DETAILS' && !node.open) return false;
    node = node.parentElement;
  }
  return true;
}

async function loadToolResult(el) {
  if (el.dataset.loaded === '1' || el.dataset.loading === '1') return;
  el.dataset.loading = '1';
  const slot = el.querySelector('.tool-result-slot');
  if (slot) slot.innerHTML = '<div class="tool-result-loading">טוען...</div>';
  const ref = el.dataset.resultRef;
  const sid = el.dataset.sessionId;
  try {
    const resp = await fetch(`/api/research/${sid}/tool_result/${ref}`);
    const json = await resp.json();
    const text = json.full || '';
    if (slot) slot.innerHTML =
      `<div class="prompt-block"><div class="prompt-role">תוצאה</div><pre class="prompt-text">${esc(text)}</pre></div>`;
    el.dataset.loaded  = '1';
    el.dataset.loading = '0';
    updateToolResultTimer(el);
  } catch (exc) {
    console.error('[lazy] tool result load failed:', exc);
    if (slot) slot.innerHTML = `<div class="tool-result-error">שגיאה בטעינה: ${esc(String(exc))}</div>`;
    el.dataset.loading = '0';
  }
}

function unloadToolResult(el) {
  clearTimeout(el._unloadTimer);
  el._unloadTimer = null;
  el.dataset.loaded = '0';
  const slot = el.querySelector('.tool-result-slot');
  if (slot) slot.innerHTML = '<div class="tool-result-placeholder">▼ לחץ להצגת תוצאה</div>';
}

function updateToolResultTimer(el) {
  if (el.dataset.loaded !== '1') return;
  if (isToolPanelVisible(el)) {
    clearTimeout(el._unloadTimer);
    el._unloadTimer = null;
  } else if (!el._unloadTimer) {
    el._unloadTimer = setTimeout(() => unloadToolResult(el), TOOL_RESULT_UNLOAD_MS);
  }
}

async function loadEvidenceFull(el) {
  if (el.dataset.loaded === '1' || el.dataset.loading === '1') return;
  el.dataset.loading = '1';
  const slot = el.querySelector('.ev-source-full-slot');
  if (slot) slot.innerHTML = '<div class="ev-source-loading">טוען…</div>';
  const ref  = el.dataset.resultRef;
  const sid  = el.dataset.sessionId;
  const tool = el.dataset.toolName || '';
  try {
    const resp = await fetch(`/api/research/${sid}/tool_result/${ref}`);
    const json = await resp.json();
    if (slot) slot.innerHTML = renderEvidenceFull(json.full || '', tool);
    el.dataset.loaded  = '1';
    el.dataset.loading = '0';
  } catch (exc) {
    console.error('[lazy] evidence full load failed:', exc);
    if (slot) slot.innerHTML = `<div class="ev-source-error">שגיאה: ${esc(String(exc))}</div>`;
    el.dataset.loading = '0';
  }
}

export function attachLazyToggleListener() {
  // Toggle event does NOT bubble — must use capture-phase listener.
  document.addEventListener('toggle', (e) => {
    const toggled = e.target;
    if (toggled.classList && toggled.classList.contains('tool-result-lazy') && toggled.open) {
      loadToolResult(toggled);
    }
    if (toggled.classList && toggled.classList.contains('ev-source-lazy') && toggled.open) {
      loadEvidenceFull(toggled);
    }
    // Re-evaluate unload timer for every loaded tool-result panel.
    document.querySelectorAll('.tool-result-lazy[data-loaded="1"]').forEach(updateToolResultTimer);
  }, true);
}
