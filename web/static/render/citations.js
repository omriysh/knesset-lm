/**
 * render/citations.js — evidence popup, sources list, evidence card rendering,
 * and the [n] → <sup> citation rewriter applied to the final answer body.
 */
import { esc, QUOTE_SKIP } from '../util.js';

// Session id for the answer currently being rendered — used by the citation
// popup's "open in protocol viewer" action (the popup is a shared singleton).
let _citeSid = '';

/**
 * Find a protocol anchor inside a quote object/array: the first node carrying a
 * meeting_id, plus its chunk anchor (speech_idx / start_speech_idx) and quote if present.
 * Returns {meetingId, speechIdx, quote} or null.
 */
function findQuoteAnchor(obj) {
  if (obj == null || typeof obj !== 'object') return null;
  if (Array.isArray(obj)) {
    for (const it of obj) {
      const a = findQuoteAnchor(it);
      if (a) return a;
    }
    return null;
  }
  if (obj.meeting_id != null) {
    const sidx = (obj.speech_idx != null) ? obj.speech_idx
               : (obj.start_speech_idx != null ? obj.start_speech_idx : null);
    return { meetingId: String(obj.meeting_id), speechIdx: sidx, quote: (sidx != null && obj.quote) || '' };
  }
  if (Array.isArray(obj.chunks)) {
    for (const ch of obj.chunks) {
      const a = findQuoteAnchor(ch);
      if (a) return a;
    }
  }
  return null;
}

const PROTOCOL_SOURCE_NOTES = {
  opinion: 'עמדה מפרוטוקול ועדה: מתוך סיכום AI, לצד ציטוט תומך מהפרוטוקול',
  topic:   'נושא דיון מתוך סיכום AI של פרוטוקול ועדה',
  speech:  'ציטוט מפרוטוקול ועדה',
};

function protocolSourceKind(node) {
  if (node.source_kind) return node.source_kind;
  if (node.opinion) return 'opinion';
  if (node.topic) return 'topic';
  if (node.text && node.speech_idx != null) return 'speech';
  return null;
}

/** One note per distinct kind of protocol row in the quote (opinion / topic / speech), '' when none. */
function protocolSourceNote(obj) {
  const kinds = new Set();
  const visit = (node) => {
    if (node == null || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (node.meeting_id != null) {
      const kind = protocolSourceKind(node);
      if (kind) kinds.add(kind);
    }
    if (Array.isArray(node.chunks)) node.chunks.forEach(visit);
  };
  visit(obj);
  return ['opinion', 'topic', 'speech'].filter(k => kinds.has(k)).map(k => PROTOCOL_SOURCE_NOTES[k]).join(' · ');
}

const OPEN_ICON = '<span class="material-symbols-outlined ev-open-icon">library_books</span>';

/** Build the "לפרוטוקול המלא →" link markup for a resolved anchor. */
function openProtocolLinkHtml(sid, anchor) {
  if (!anchor || !anchor.meetingId) return '';
  const sidx = (anchor.speechIdx != null) ? String(anchor.speechIdx) : '';
  return (
    `<button type="button" class="ev-open-protocol" data-sid="${esc(sid)}" ` +
    `data-meeting-id="${esc(anchor.meetingId)}" data-speech-idx="${esc(sidx)}" data-quote="${esc(anchor.quote || '')}" ` +
    `data-click="openProtocolFromCitation">` +
    `${OPEN_ICON}<span>לפרוטוקול המלא ←</span></button>`
  );
}

/** Collect distinct cited meetings ({meeting_id, date, committee}) from citations. */
function collectCitedMeetings(citations) {
  const byId = new Map();
  const visit = (obj) => {
    if (obj == null || typeof obj !== 'object') return;
    if (Array.isArray(obj)) { obj.forEach(visit); return; }
    if (obj.meeting_id != null && !byId.has(String(obj.meeting_id))) {
      byId.set(String(obj.meeting_id), {
        meeting_id: String(obj.meeting_id),
        date:       obj.date || '',
        committee:  obj.committee || '',
      });
    }
    if (Array.isArray(obj.chunks)) obj.chunks.forEach(visit);
  };
  (citations || []).forEach(c => { if (c) visit(c.quote); });
  return Array.from(byId.values());
}

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
    const text = obj.text || obj.topic || obj.opinion || obj.topic_text || obj.label || obj.summary || obj.full_text || '';
    const textIsVerbatim = protocolSourceKind(obj) === 'speech';
    let textHtml = text
      ? `<div class="ev-citation-quote${textIsVerbatim ? ' ev-citation-verbatim' : ''}">${esc(String(text))}</div>` : '';
    if (obj.quote && obj.quote !== text) textHtml += `<blockquote class="ev-citation-quote ev-citation-verbatim">${esc(String(obj.quote))}</blockquote>`;
    if (Array.isArray(obj.chunks) && obj.chunks.length > 0) {
      const chunksHtml = obj.chunks.map(ch => renderQuoteObj(ch)).join('<hr class="ev-quote-sep">');
      return header + textHtml + chunksHtml;
    }
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

/** The citation's snippet as the popup shows it: {contentHtml, metaNote, anchor}. */
function citationSnippet(quoteRaw, uiMeta) {
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

  const toolNote = (uiMeta && uiMeta.meta_note) ? uiMeta.meta_note : (uiMeta && uiMeta.tool_name) || '';
  const metaNote = (quoteObj != null && protocolSourceNote(quoteObj)) || toolNote;
  const anchor   = quoteObj != null ? findQuoteAnchor(quoteObj) : null;
  return { contentHtml, metaNote, anchor };
}

function showCitationPopup(supEl, quoteRaw, uiMeta) {
  const popup = getEvPopup();
  const { contentHtml, metaNote, anchor } = citationSnippet(quoteRaw, uiMeta);
  const noteHtml = metaNote ? `<div class="ev-citation-popup-source">${esc(metaNote)}</div>` : '';
  const linkHtml = openProtocolLinkHtml(_citeSid, anchor);
  const footerHtml = (noteHtml || linkHtml)
    ? `<div class="ev-citation-popup-footer">${noteHtml}${linkHtml}</div>`
    : '';
  popup.innerHTML = contentHtml + footerHtml;

  popup.hidden = false;
  const sr = supEl.getBoundingClientRect();
  const pr = popup.getBoundingClientRect();
  const GAP = 8;
  let left = sr.left + sr.width / 2 - pr.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pr.width - 8));
  const showBelow = sr.top - pr.height - GAP < 0;
  const topAbove = sr.top + window.scrollY - pr.height - GAP;
  const top = showBelow ? sr.bottom + window.scrollY + GAP : topAbove;
  popup.classList.toggle('ev-citation-popup--below', showBelow);
  const tailLeft = (sr.left + sr.width / 2) - left;
  popup.style.left = left + 'px';
  popup.style.top  = top + 'px';
  popup.style.setProperty('--tail-left', tailLeft + 'px');
}

function citationSup(evId, displayN) {
  const sup = document.createElement('sup');
  sup.className = 'ev-cite';
  sup.dataset.evId = evId || '';
  sup.title = evId || '';
  sup.textContent = `[${displayN}]`;
  return sup;
}

/**
 * Replace every regex match inside bodyEl's text nodes with the element buildElement(match) returns
 * (null keeps the text). Works on text nodes only, so a marker can never land inside markup or an attribute.
 */
function replaceTextMarkers(bodyEl, pattern, buildElement) {
  const walker = document.createTreeWalker(bodyEl, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  for (const node of textNodes) {
    const text = node.nodeValue;
    const fragment = document.createDocumentFragment();
    let copiedUpTo = 0;
    for (const match of text.matchAll(pattern)) {
      const element = buildElement(match);
      if (!element) continue;
      fragment.append(text.slice(copiedUpTo, match.index), element);
      copiedUpTo = match.index + match[0].length;
    }
    if (copiedUpTo === 0) continue;
    fragment.append(text.slice(copiedUpTo));
    node.replaceWith(fragment);
  }
}

/** The footnote an [n] points at; an `expand` entry resolves to the evidence it expanded. */
function resolveFootnote(footnotes, evId) {
  const fn = footnotes.find(f => f.id === evId);
  if (!fn || fn.tool_name !== 'expand') return fn;
  const origId = (fn.metadata && fn.metadata.evidence_id) || (fn.provenance && fn.provenance.evidence_id);
  return (origId && footnotes.find(f => f.id === origId)) || fn;
}

function footnoteUiMeta(fn) {
  return fn ? (fn.ui || { tool_name: fn.tool_name }) : {};
}

const _footnotesByAnswerBody = new WeakMap();

export function applyEvidenceCitations(bodyEl, footnotes, citations, sid) {
  _citeSid = sid || '';
  _footnotesByAnswerBody.set(bodyEl, footnotes);
  // Stash this answer's cited meetings so the viewer sidebar can be seeded with
  // them when the user opens a protocol from a citation or a source card.
  if (_citeSid) {
    window.__citedMeetings = window.__citedMeetings || {};
    window.__citedMeetings[_citeSid] = collectCitedMeetings(citations);
  }
  const citMap = {};
  (citations || []).forEach(c => { if (c && c.n != null) citMap[c.n] = c; });
  const evIdToIdx = {};
  footnotes.forEach((fn, i) => { evIdToIdx[fn.id] = i + 1; });

  const hasCitations = Object.keys(citMap).length > 0;
  if (hasCitations) {
    replaceTextMarkers(bodyEl, /\[(\d+)\]/g, (match) => {
      const n   = parseInt(match[1], 10);
      const cit = citMap[n];
      if (!cit) return null;
      const quoteStr = (typeof cit.quote === 'object' && cit.quote !== null)
        ? JSON.stringify(cit.quote)
        : (cit.quote || '');
      const sup = citationSup(cit.ev_id, evIdToIdx[cit.ev_id] || n);
      sup.dataset.citeN = String(n);
      sup.dataset.quote = quoteStr;
      return sup;
    });
  } else {
    // Fallback: old [ev_xxx] format
    replaceTextMarkers(bodyEl, /\[ev_([0-9a-f]+)\]/g, (match) => {
      const evId = 'ev_' + match[1];
      const n = evIdToIdx[evId];
      return n ? citationSup(evId, n) : null;
    });
  }

  bodyEl.querySelectorAll('sup.ev-cite').forEach(sup => {
    sup.addEventListener('click', e => {
      e.stopPropagation();
      const quoteRaw = sup.dataset.quote || '';
      if (quoteRaw) showCitationPopup(sup, quoteRaw, footnoteUiMeta(resolveFootnote(footnotes, sup.dataset.evId || '')));
    });
  });
}

function formatToolArgValue(value) {
  if (Array.isArray(value)) return value.map(formatToolArgValue).join(', ');
  if (value !== null && typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** The tool calls behind a footnote, without their results: "tool_name — key: value · key: value". */
function footnoteToolQueryHtml(fn) {
  if (!fn) return '';
  const toolCalls = (fn.provenance && fn.provenance.tool_calls) || [{ name: fn.tool_name, args: {} }];
  return toolCalls.map(call => {
    const args = Object.entries(call.args || {})
      .filter(([, value]) => value != null && value !== '' && !(Array.isArray(value) && !value.length))
      .map(([key, value]) => `${esc(key)}: ${esc(formatToolArgValue(value))}`)
      .join(' · ');
    return `<div class="export-footnote-query" dir="ltr"><code>${esc(call.name || '')}</code>${args ? ' — ' + args : ''}</div>`;
  }).join('');
}

/**
 * A copy of an answer body for export: each distinct citation becomes a footnote numbered from
 * firstFootnoteNumber in order of appearance, holding the popup's snippet and the tool query.
 * Returns {answerHtml, footnotesHtml, footnoteCount}.
 */
export function exportAnswerWithFootnotes(bodyEl, firstFootnoteNumber) {
  const footnotes = _footnotesByAnswerBody.get(bodyEl) || [];
  const answerCopy = bodyEl.cloneNode(true);
  const footnoteNumberByCitation = new Map();
  const footnoteItems = [];
  answerCopy.querySelectorAll('sup.ev-cite').forEach(sup => {
    const citationKey = sup.dataset.citeN || sup.dataset.evId || '';
    if (!footnoteNumberByCitation.has(citationKey)) {
      const footnoteNumber = firstFootnoteNumber + footnoteNumberByCitation.size;
      footnoteNumberByCitation.set(citationKey, footnoteNumber);
      const fn = resolveFootnote(footnotes, sup.dataset.evId || '');
      const { contentHtml, metaNote } = citationSnippet(sup.dataset.quote || '', footnoteUiMeta(fn));
      footnoteItems.push(
        `<li value="${footnoteNumber}" id="export-footnote-${footnoteNumber}">` +
        contentHtml +
        (metaNote && metaNote !== fn?.tool_name ? `<div class="ev-citation-popup-source">${esc(metaNote)}</div>` : '') +
        footnoteToolQueryHtml(fn) +
        `</li>`);
    }
    const footnoteNumber = footnoteNumberByCitation.get(citationKey);
    const exportSup = document.createElement('sup');
    exportSup.className = 'export-cite';
    exportSup.textContent = `[${footnoteNumber}]`;
    sup.replaceWith(exportSup);
  });
  return {
    answerHtml:    answerCopy.innerHTML,
    footnotesHtml: footnoteItems.length ? `<ol class="export-footnotes">${footnoteItems.join('')}</ol>` : '',
    footnoteCount: footnoteItems.length,
  };
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
        `<div class="ev-source-full-slot"><div class="ev-source-placeholder">▼ להצגת המקור המלא</div></div>` +
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

export function renderEvidenceFull(text, toolName, sid) {
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
    const cards     = real.map(item => renderEvidenceCard(item, sid)).join('');
    const notice    = truncItem
      ? `<div class="ev-truncated-notice">עוד ${esc(String(truncItem.items_removed))} פריטים לא הוצגו</div>`
      : '';
    return `<div class="ev-full-cards">${cards}${notice}</div>`;
  }
  if (typeof data === 'object' && data !== null) return renderEvidenceCard(data, sid);
  return `<pre class="ev-full-json">${esc(JSON.stringify(data, null, 2))}</pre>`;
}

function renderEvidenceCard(item, sid) {
  if (typeof item !== 'object' || item === null) {
    return `<div class="ev-full-card"><pre class="ev-card-rest">${esc(String(item))}</pre></div>`;
  }
  const LABELS = ['label', 'committee_name', 'committee', 'name', 'title', 'mk_name'];
  const METAS  = ['meeting_id', 'session_id', 'date', 'knesset_num', 'score', 'relevance_score', 'bullet_idx'];
  const TEXTS  = ['text', 'text_he', 'topic', 'opinion', 'body', 'content', 'summary'];
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
  const anchor   = findQuoteAnchor(item);
  const linkHtml = (sid && anchor) ? openProtocolLinkHtml(sid, anchor) : '';
  const headerHtml = (labelHtml || metaBadges || linkHtml)
    ? `<div class="ev-card-header">${labelHtml}<span class="ev-card-metas">${metaBadges}</span>${linkHtml}</div>`
    : '';
  return (
    `<div class="ev-full-card">` +
    headerHtml +
    (bodyHtml ? `<div class="ev-card-body">${bodyHtml}</div>` : '') +
    `</div>`
  );
}
