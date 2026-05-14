/**
 * render/details.js — HTML builders for the expandable sub-sections inside
 * a stage card: prompt, thinking, tool result, retrieval.
 */
import { esc } from '../util.js';
import { state } from '../state.js';

export function toggleStageCard(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('visible');
}

export function renderPromptHtml(p, open = false) {
  const sys  = p.system || '';
  const user = p.user   || '';
  const openAttr = open ? ' open' : '';
  return (
    `<details class="sub-details"${openAttr}>` +
    `<summary class="sub-summary">פרומפט</summary>` +
    `<div class="sub-details-body">` +
    (sys ? `<div class="prompt-block"><div class="prompt-role">מערכת</div><pre class="prompt-text">${esc(sys)}</pre></div>` : '') +
    `<div class="prompt-block"><div class="prompt-role">משתמש</div><pre class="prompt-text">${esc(user)}</pre></div>` +
    `</div></details>`
  );
}

export function renderThinkingHtml(thinking, llmMs) {
  const timeStr = llmMs > 0 ? ` (${(llmMs / 1000).toFixed(1)}s)` : '';
  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary thinking-summary">מחשבות${esc(timeStr)}</summary>` +
    `<div class="sub-details-body"><pre class="prompt-text thinking-text">${esc(thinking)}</pre></div>` +
    `</details>`
  );
}

export function renderToolResultHtml(tr) {
  const name      = tr.name       || '';
  const args      = tr.args       || {};
  const elapsedMs = tr.elapsed_ms != null ? tr.elapsed_ms : null;
  const argsStr   = JSON.stringify(args, null, 2);
  const timeStr   = elapsedMs != null
    ? ` <span class="tool-time">${(elapsedMs / 1000).toFixed(1)}s</span>`
    : '';
  const hasArgs  = Object.keys(args).length > 0;
  const argsHtml = hasArgs
    ? `<div class="prompt-block"><div class="prompt-role">ארגומנטים</div><pre class="prompt-text">${esc(argsStr)}</pre></div>`
    : '';

  if (tr.result_ref) {
    // Lazy variant — full text fetched on expand.
    return (
      `<details class="sub-details tool-result-lazy"` +
      ` data-result-ref="${esc(tr.result_ref)}" data-session-id="${esc(state.sessionId || '')}" data-loaded="0">` +
      `<summary class="sub-summary"><span class="tool-summary-label">${esc(name)}</span>${timeStr}</summary>` +
      `<div class="sub-details-body">` +
      argsHtml +
      `<div class="tool-result-slot"><div class="tool-result-placeholder">▼ לחץ להצגת תוצאה</div></div>` +
      `</div></details>`
    );
  }

  const result = tr.result || '';
  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary"><span class="tool-summary-label">${esc(name)}</span>${timeStr}</summary>` +
    `<div class="sub-details-body">` +
    argsHtml +
    `<div class="prompt-block"><div class="prompt-role">תוצאה</div><pre class="prompt-text">${esc(result)}</pre></div>` +
    `</div></details>`
  );
}

export function renderRetrievalHtml(r) {
  const meetings = r.meetings      || [];
  const chunks   = r.chunks        || [];
  const chars    = r.context_chars || 0;
  const tokEst   = Math.round(chars / 2);
  const ragMs    = r.rag_ms        || 0;
  const ragTime  = ragMs > 0 ? ` · ${(ragMs / 1000).toFixed(1)}s` : '';

  const label = `פרטי אחזור — ${meetings.length} ישיבות · ${chunks.length} קטעים · ~${tokEst.toLocaleString()} טוקנים${ragTime}`;

  let body = '<div class="stats-row">';
  body += `ישיבות: <strong>${meetings.length}</strong>`;
  body += ` &nbsp;|&nbsp; קטעים: <strong>${chunks.length}</strong>`;
  body += ` &nbsp;|&nbsp; הקשר: <strong>${chars.toLocaleString()}</strong> תווים (~${tokEst.toLocaleString()} טוקנים)`;
  body += '</div>';

  if (meetings.length) {
    body += `<div style="color:#666;font-size:.74rem;margin-bottom:6px;direction:rtl">${meetings.map(esc).join(' · ')}</div>`;
  }

  if (chunks.length) {
    body += '<ul class="chunk-list">';
    for (const c of chunks) {
      body += `<li class="chunk-item">` +
        `<span class="chunk-date">${esc(c.date)}</span>` +
        `<span class="chunk-topic">${esc(c.topic)}</span>` +
        `<span class="chunk-sim">sim&nbsp;${c.p1_sim}</span>` +
        `</li>`;
    }
    body += '</ul>';
  }

  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary">${label}</summary>` +
    `<div class="sub-details-body">${body}</div>` +
    `</details>`
  );
}
