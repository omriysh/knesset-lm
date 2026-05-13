/**
 * render/stage_card.js — live and completed stage tiles inside a stages card.
 *
 * One module owns: building the card DOM, wiring the header click,
 * appending live streaming content (thinking + output), finalising a live
 * card into a completed one (visually, by removal — completed cards are
 * appended separately).
 */
import { esc } from '../util.js';
import { scrollToBottom } from '../dom.js';
import {
  renderPromptHtml, renderThinkingHtml, renderToolResultHtml,
  renderRetrievalHtml, toggleStageCard,
} from './details.js';

function buildHeaderHtml({ label, stage, loop, live, timeHtml = '', toolsHtml = '' }) {
  const loopHtml = loop > 0 ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>` : '';
  const metaHtml = live
    ? `<span class="live-thinking-dot"></span>${loopHtml}`
    : `${timeHtml}${toolsHtml}${loopHtml}`;
  return (
    `<div class="stage-header">` +
      `<span class="stage-arrow">▶</span>` +
      `<span class="stage-dot ${esc(stage)}"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-meta">${metaHtml}</span>` +
    `</div>`
  );
}

function wireHeader(card) {
  const header = card.querySelector('.stage-header');
  if (header) header.addEventListener('click', () => toggleStageCard(header));
}

export function addLiveStageCard(stagesEl, opts) {
  finaliseLiveCard(stagesEl); // clear any stale live card
  const label      = opts.label      || 'שלב';
  const stage      = opts.stage      || 'unknown';
  const loop       = opts.loop       || 0;
  const openPrompt = opts.openPrompt || false;

  const card = document.createElement('div');
  card.className = 'stage-card live-stage-card';
  card.innerHTML =
    buildHeaderHtml({ label, stage, loop, live: true }) +
    `<div class="stage-body">` +
      renderPromptHtml(opts.prompt || {}, openPrompt) +
      `<details class="sub-details open">` +
        `<summary class="sub-summary thinking-summary">תהליך עבודה…</summary>` +
        `<div class="sub-details-body"><pre class="prompt-text thinking-text"></pre></div>` +
      `</details>` +
    `</div>`;

  wireHeader(card);
  stagesEl.appendChild(card);
  scrollToBottom();
  return card;
}

export function appendLiveThinking(stagesEl, text) {
  const live = stagesEl && stagesEl.querySelector('.live-stage-card');
  if (!live) return;
  const pre = live.querySelector('.thinking-text');
  if (!pre) return;
  pre.textContent += text;
  pre.scrollTop = pre.scrollHeight;
}

export function appendLiveOutput(stagesEl, text) {
  const live = stagesEl && stagesEl.querySelector('.live-stage-card');
  if (!live) return;
  let pre = live.querySelector('.live-output-text');
  if (!pre) {
    const body = live.querySelector('.stage-body');
    if (!body) return;
    const det = document.createElement('details');
    det.className = 'sub-details open';
    det.innerHTML =
      `<summary class="sub-summary">פלט…</summary>` +
      `<div class="sub-details-body"><pre class="prompt-text live-output-text"></pre></div>`;
    body.appendChild(det);
    pre = live.querySelector('.live-output-text');
  }
  if (pre) {
    pre.textContent += text;
    pre.scrollTop = pre.scrollHeight;
  }
}

export function finaliseLiveCard(stagesEl) {
  const live = stagesEl && stagesEl.querySelector('.live-stage-card');
  if (live) live.remove();
}

export function hasLiveCard(stagesEl) {
  return !!(stagesEl && stagesEl.querySelector('.live-stage-card'));
}

export function addCompletedStageCard(stagesEl, data) {
  const label       = data.label        || 'שלב';
  const stage       = data.stage        || 'unknown';
  const content     = data.content      || '';
  const loop        = data.loop         || 0;
  const elapsedMs   = data.elapsed_ms   != null ? data.elapsed_ms : null;
  const llmMs       = data.llm_ms       || 0;
  const toolMs      = data.tool_ms      || 0;
  const thinking    = data.thinking     || '';
  const tools       = data.tools        || [];
  const retrieval   = data.retrieval    || null;
  const prompt      = data.prompt       || null;
  const toolResults = data.tool_results || [];

  let timeHtml = '';
  if (elapsedMs != null) {
    const totalStr = (elapsedMs / 1000).toFixed(1) + 's';
    if (llmMs > 0 || toolMs > 0) {
      const llmStr  = (llmMs  / 1000).toFixed(1) + 's';
      const toolStr = (toolMs / 1000).toFixed(1) + 's';
      timeHtml = `<span class="stage-time" title="LLM: ${llmStr} | כלים: ${toolStr} | סה״כ: ${totalStr}">${totalStr}</span>`;
    } else {
      timeHtml = `<span class="stage-time">${totalStr}</span>`;
    }
  }

  const uniqueTools = [...new Set(tools)];
  const toolsHtml   = uniqueTools.length > 0
    ? `<span class="stage-tools-badge">${uniqueTools.join(' · ')}</span>`
    : '';

  const promptHtml      = prompt     ? renderPromptHtml(prompt)            : '';
  const thinkingHtml    = thinking   ? renderThinkingHtml(thinking, llmMs) : '';
  const toolResultsHtml = toolResults.map(tr => renderToolResultHtml(tr)).join('');
  const retrievalHtml   = retrieval  ? renderRetrievalHtml(retrieval)      : '';

  const card = document.createElement('div');
  card.className = 'stage-card';
  card.innerHTML =
    buildHeaderHtml({ label, stage, loop, live: false, timeHtml, toolsHtml }) +
    `<div class="stage-body">` +
      promptHtml + thinkingHtml + toolResultsHtml + retrievalHtml +
      (content ? `<div class="prose-content" style="margin-top:8px">${marked.parse(content)}</div>` : '') +
    `</div>`;

  wireHeader(card);
  stagesEl.appendChild(card);
  scrollToBottom();
  return card;
}
