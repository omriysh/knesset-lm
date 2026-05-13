/**
 * render/citations.js — evidence popup, sources list, evidence card rendering,
 * and the [n] → <sup> citation rewriter applied to the final answer body.
 */
import { esc, QUOTE_SKIP } from '../util.js';

let _evPopup = null;
function getEvPopup() {
  if (!_evPopup) {
    _evPopup = document.createElement('div');
    _evPopup.className = 'ev-citation-popup';
    _evPopup.hidden = true;
    document.body.appendChild(_evPopup);
    document.addEventListener('click', () => { _evPopup.hidden = true; });
  }
  return _evPopup;
}

function renderQuoteObj(obj) {
  if (Array.isArray(obj)) {
    return obj.map(renderQuoteObj).join('<hr class="ev-quote-sep">');
  }
  if (typeof obj !== 'object' || obj === null) {
    return `<div class="ev-citation-quote">${esc(String(obj))}</div>`;
  }
  // Empty result: show the query that returned nothing
  if (obj._no_results) {
    const q = obj.query || obj.topic || obj.mk_query || obj.speaker || '';
    const label = q ? ` עבור "${esc(q)}"` : '';
    return `<div class="ev-citation-empty">לא נמצאו תוצאות${label}</div>`;
  }
  // Meeting-like: structured header + text
  if (obj.meeting_id != null || obj.committee != null) {
    const parts = [];
    if (obj.committee) parts.push(esc(String(obj.committee)));
    if (obj.date)      parts.push(esc(String(obj.date)));
    if (obj.speaker)   parts.push(esc(String(obj.speaker)));
    const header = parts.length
      ? `<div class="ev-citation-meeting-header">${parts.join(' &middot; ')}</div>`
      : '';
    const text = obj.text || obj.label || obj.summary || obj.full_text || '';
    const textHtml = text ? `<div class="ev-citation-quote">${esc(String(text))}</div>` : '';
    return header + textHtml;
  }
  // Generic: visible key-value pairs
  const rows = Object.entries(obj)
    .filter(([k, v]) => !QUOTE_SKIP.has(k) && v != null && v !== '')
    .map(([k, v]) => {
      const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return `<div class="ev-citation-kv">` +
        `<span class="ev-kv-key">${esc(k)}</span>` +
        `<span class="ev-kv-val">${esc(val)}</span></div>`;
    });
  return rows.length
    ? `<div class="ev-citation-kvlist">${rows.join('')}</div>`
    : `<div class="ev-citation-quote">${esc(JSON.stringify(obj))}</div>`;
}

function showCitationPopup(supEl, quoteRaw, uiMeta) {
  const popup = getEvPopup();

  let quoteObj = null;
  if (typeof quoteRaw === 'object' && quoteRaw !== null) {
    quoteObj = quoteRaw;
  } else if (typeof quoteRaw === 'string') {
    const t = quoteRaw.trim();
    if (t.startsWith('{') || t.startsWith('[')) {
      try { quoteObj = JSON.parse(t); } catch (exc) {
        console.error('[citations] failed to parse quote JSON:', exc);
      }
    }
  }

  const contentHtml = quoteObj != null
    ? renderQuoteObj(quoteObj)
    : `<div class="ev-citation-quote">${esc(quoteRaw || '')}</div>`;

  const metaNote = (uiMeta && uiMeta.meta_note) ? uiMeta.meta_note : (uiMeta && uiMeta.tool_name) || '';
  popup.innerHTML = contentHtml +
    (metaNote ? `<div class="ev-citation-popup-source">${esc(metaNote)}</div>` : '');

  popup.hidden = false;
  const sr = supEl.getBoundingClientRect();
  const pr = popup.getBoundingClientRect();
  const GAP = 8;
  let left = sr.left + sr.width / 2 - pr.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pr.width - 8));
  const topAbove = sr.top + window.scrollY - pr.height - GAP;
  const top = (sr.top - pr.height - GAP >= 0) ? topAbove : sr.bottom + window.scrollY + GAP;
  const tailLeft = (sr.left + sr.width / 2) - left;
  popup.style.left = left + 'px';
  popup.style.top  = top + 'px';
  popup.style.setProperty('--tail-left', tailLeft + 'px');
}

export function applyEvidenceCitations(bodyEl, footnotes, citations) {
  const citMap = {};
  (citations || []).forEach(c => { if (c && c.n != null) citMap[c.n] = c; });
  const evIdToIdx = {};
  footnotes.forEach((fn, i) => { evIdToIdx[fn.id] = i + 1; });

  const hasCitations = Object.keys(citMap).length > 0;
  if (hasCitations) {
    bodyEl.innerHTML = bodyEl.innerHTML.replace(/\[(\d+)\]/g, (match, numStr) => {
      const n   = parseInt(numStr, 10);
      const cit = citMap[n];
      if (!cit) return match;
      const displayN = evIdToIdx[cit.ev_id] || n;
      const quoteStr = (typeof cit.quote === 'object' && cit.quote !== null)
        ? JSON.stringify(cit.quote)
        : (cit.quote || '');
      return (
        `<sup class="ev-cite" data-cite-n="${n}" ` +
        `data-ev-id="${esc(cit.ev_id)}" ` +
        `data-quote="${esc(quoteStr)}" ` +
        `title="${esc(cit.ev_id)}">[${displayN}]</sup>`
      );
    });
  } else {
    // Fallback: old [ev_xxx] format
    bodyEl.innerHTML = bodyEl.innerHTML.replace(/\[ev_([0-9a-f]+)\]/g, (match, hex) => {
      const evId = 'ev_' + hex;
      const n = evIdToIdx[evId];
      if (!n) return match;
      return `<sup class="ev-cite" data-ev-id="${esc(evId)}" title="${esc(evId)}">[${n}]</sup>`;
    });
  }

  bodyEl.querySelectorAll('sup.ev-cite').forEach(sup => {
    sup.addEventListener('click', e => {
      e.stopPropagation();
      const quoteRaw = sup.dataset.quote || '';
      const evId     = sup.dataset.evId  || '';
      const fn       = footnotes.find(f => f.id === evId);
      let resolvedFn = fn;
      // For expand entries, resolve to the original evidence entry for display metadata.
      if (fn && fn.tool_name === 'expand') {
        const origId = (fn.metadata && fn.metadata.evidence_id) || (fn.provenance && fn.provenance.evidence_id);
        if (origId) {
          const origFn = footnotes.find(f => f.id === origId);
          if (origFn) resolvedFn = origFn;
        }
      }
      const uiMeta = resolvedFn ? (resolvedFn.ui || { tool_name: resolvedFn.tool_name }) : {};
      if (quoteRaw) {
        showCitationPopup(sup, quoteRaw, uiMeta);
      }
    });
  });
}

export function buildSourcesHtml(footnotes, sid) {
  const entries = footnotes.map((fn, i) => {
    const n        = i + 1;
    const toolName = fn.tool_name  || '';
    const stepId   = fn.step_id    || '';
    const summary  = fn.summary    || '';
    const ref      = fn.result_ref || '';
    const header   = (
      `<span class="ev-source-num">[${n}]</span>` +
      `<span class="ev-source-tool">${esc(toolName)}</span>` +
      `<span class="ev-source-step">${esc(stepId)}</span>` +
      `<span class="ev-source-summary">${esc(summary)}</span>`
    );
    if (ref) {
      return (
        `<details class="ev-source-entry ev-source-lazy"` +
        ` data-result-ref="${esc(ref)}" data-session-id="${esc(sid || '')}"` +
        ` data-tool-name="${esc(toolName)}" data-loaded="0">` +
        `<summary class="ev-source-header">${header}</summary>` +
        `<div class="ev-source-full-slot"><div class="ev-source-placeholder">▼ לחץ להצגת מקור מלא</div></div>` +
        `</details>`
      );
    }
    return `<div class="ev-source-entry"><div class="ev-source-header">${header}</div></div>`;
  }).join('');
  return (
    `<details class="ev-sources">` +
    `<summary class="ev-sources-summary">מקורות (${footnotes.length})</summary>` +
    `<div class="ev-sources-body">${entries}</div>` +
    `</details>`
  );
}

export function renderEvidenceFull(text, toolName) {
  if (!text) return '<div class="ev-source-empty">אין תוכן</div>';
  let data;
  try { data = JSON.parse(text); } catch (exc) {
    console.error('[citations] evidence JSON parse failed, rendering as plain text:', exc);
    return `<div class="ev-card-text">${esc(text)}</div>`;
  }
  if (Array.isArray(data)) {
    if (data.length === 0) return '<div class="ev-source-empty">אין תוצאות</div>';
    const real      = data.filter(x => !(x && x._truncated));
    const truncItem = data.find(x => x && x._truncated);
    const cards     = real.map(item => renderEvidenceCard(item)).join('');
    const notice    = truncItem
      ? `<div class="ev-truncated-notice">עוד ${truncItem.items_removed} פריטים לא הוצגו</div>`
      : '';
    return `<div class="ev-full-cards">${cards}${notice}</div>`;
  }
  if (typeof data === 'object' && data !== null) return renderEvidenceCard(data);
  return `<pre class="ev-full-json">${esc(JSON.stringify(data, null, 2))}</pre>`;
}

function renderEvidenceCard(item) {
  if (typeof item !== 'object' || item === null) {
    return `<div class="ev-full-card"><pre class="ev-card-rest">${esc(String(item))}</pre></div>`;
  }
  const LABELS = ['label', 'committee_name', 'committee', 'name', 'title', 'mk_name'];
  const METAS  = ['meeting_id', 'session_id', 'date', 'knesset_num', 'score', 'relevance_score', 'bullet_idx'];
  const TEXTS  = ['text', 'text_he', 'body', 'content', 'summary'];
  const LISTS  = ['bullets', 'speeches'];
  const SKIP   = new Set(['bullet_id', 'id', '_truncated', 'items_removed', 'source_url', 'result_ref']);
  const seen   = new Set(Object.keys(item).filter(k => SKIP.has(k)));

  let labelHtml = '';
  for (const f of LABELS) {
    if (item[f]) { labelHtml = `<span class="ev-card-label">${esc(String(item[f]))}</span>`; seen.add(f); break; }
  }
  let metaBadges = '';
  for (const f of METAS) {
    if (item[f] != null) {
      const v = (f === 'score' || f === 'relevance_score') ? Number(item[f]).toFixed(3) : String(item[f]);
      metaBadges += `<span class="ev-card-meta-badge">${esc(f.replace(/_/g, ' '))}: ${esc(v)}</span>`;
      seen.add(f);
    }
  }
  let bodyHtml = '';
  for (const f of TEXTS) {
    if (item[f] && !seen.has(f)) {
      const t = String(item[f]);
      bodyHtml += `<div class="ev-card-text">${esc(t.length > 600 ? t.slice(0, 600) + '…' : t)}</div>`;
      seen.add(f); break;
    }
  }
  for (const f of LISTS) {
    if (Array.isArray(item[f]) && item[f].length > 0 && !seen.has(f)) {
      const items = item[f].slice(0, 5);
      const more  = item[f].length - items.length;
      bodyHtml += `<div class="ev-card-bullets">` +
        items.map(b => `<div class="ev-card-bullet">• ${esc(typeof b === 'string' ? b : JSON.stringify(b))}</div>`).join('') +
        (more > 0 ? `<div class="ev-card-bullet ev-card-more">+${more} נוספים…</div>` : '') +
        `</div>`;
      seen.add(f);
    }
  }
  const rest = Object.entries(item).filter(([k]) => !seen.has(k));
  if (rest.length > 0) {
    bodyHtml += `<pre class="ev-card-rest">${esc(JSON.stringify(Object.fromEntries(rest), null, 2))}</pre>`;
  }
  const headerHtml = (labelHtml || metaBadges)
    ? `<div class="ev-card-header">${labelHtml}<span class="ev-card-metas">${metaBadges}</span></div>`
    : '';
  return (
    `<div class="ev-full-card">` +
    headerHtml +
    (bodyHtml ? `<div class="ev-card-body">${bodyHtml}</div>` : '') +
    `</div>`
  );
}
