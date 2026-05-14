/**
 * render/stages.js — the outer "stages" card (collapsible container for all
 * stage tiles in one query) and the subgraph wrapper card (a stages card
 * inside the outer stages card for one research subgraph run).
 */
import { chatColumn, scrollToBottom, stagesAlways } from '../dom.js';
import { esc } from '../util.js';
import { finaliseLiveCard } from './stage_card.js';

export function appendStagesCard() {
  const alwaysOpen = stagesAlways();
  const wrap = document.createElement('div');
  wrap.className = 'msg-agent';
  if (!alwaysOpen) wrap.style.display = 'none';
  wrap.innerHTML =
    `<div class="ai-stages-card">` +
    `<div class="ai-stages-header">` +
      `<span>שלבי עיבוד</span>` +
      `<span class="ai-stages-toggle-arrow">▶</span>` +
    `</div>` +
    `</div>`;
  chatColumn.appendChild(wrap);
  // Don't scrollToBottom — hidden card shouldn't move the scroll.
  const card = wrap.querySelector('.ai-stages-card');
  card.querySelector('.ai-stages-header').addEventListener('click', () => {
    card.classList.toggle('collapsed');
  });
  return card;
}

export function wireStatusToggle(statusEl, stagesEl) {
  if (stagesAlways()) return;
  const wrap = stagesEl.parentElement;
  statusEl.classList.add('clickable');
  statusEl.title = 'לחץ לצפייה בשלבי עיבוד';
  statusEl.addEventListener('click', () => {
    const hidden = wrap.style.display === 'none';
    wrap.style.display = hidden ? 'block' : 'none';
    if (hidden) scrollToBottom();
  });
}

export function addSubgraphWrapperCard(stagesEl, nodeStart) {
  finaliseLiveCard(stagesEl);
  const label    = nodeStart.label || 'מחקר מעמיק';
  const loop     = nodeStart.loop  || 0;
  const loopHtml = loop > 0 ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>` : '';

  const card = document.createElement('div');
  card.className = 'stage-card subgraph-card live-stage-card';
  card.dataset.startTs = String(Date.now());
  card.innerHTML =
    `<div class="stage-header">` +
      `<span class="stage-arrow">▶</span>` +
      `<span class="stage-dot research"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-meta"><span class="live-thinking-dot"></span>${loopHtml}</span>` +
    `</div>` +
    `<div class="stage-body">` +
      `<div class="subgraph-inner-stages"></div>` +
    `</div>`;

  const header = card.querySelector('.stage-header');
  header.addEventListener('click', () => {
    header.classList.toggle('open');
    header.nextElementSibling.classList.toggle('visible');
  });

  stagesEl.appendChild(card);
  scrollToBottom();
  return card.querySelector('.subgraph-inner-stages');
}

export function finaliseSubgraphCard(subgraphContainer) {
  if (!subgraphContainer) return;
  const card = subgraphContainer.closest('.subgraph-card');
  if (!card) return;
  card.classList.remove('live-stage-card');
  const dot = card.querySelector('.live-thinking-dot');
  if (dot) dot.remove();
  const header = card.querySelector('.stage-header');
  if (header) header.classList.remove('open');

  const startTs = parseInt(card.dataset.startTs || '0', 10);
  if (startTs) {
    const elapsedMs = Date.now() - startTs;
    const metaEl = card.querySelector('.stage-meta');
    if (metaEl) {
      const timeSpan = document.createElement('span');
      timeSpan.className = 'stage-time';
      timeSpan.textContent = (elapsedMs / 1000).toFixed(1) + 's';
      metaEl.insertBefore(timeSpan, metaEl.firstChild);
    }
  }
}
