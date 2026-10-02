/**
 * render/export.js — export research answers as a PDF through the browser's print dialog ("Save as PDF"):
 * the questions and final answers, with each citation as a footnote holding the popup's snippet and the
 * tool query behind it. Each answer card gets a button offering this question and answer or the whole
 * conversation.
 */
import { chatColumn } from '../dom.js';
import { esc } from '../util.js';
import { exportAnswerWithFootnotes } from './citations.js';

function answerBodyOf(row) {
  return row.querySelector(':scope > .msg-agent-card > .prose-content');
}

function isFinishedAnswer(row) {
  const answerBody = row.classList.contains('msg-agent') && answerBodyOf(row);
  return !!answerBody && !answerBody.querySelector('.stream-cursor');
}

function questionBefore(answerRow) {
  for (let row = answerRow.previousElementSibling; row; row = row.previousElementSibling) {
    if (row.classList.contains('msg-user')) return row;
  }
  return null;
}

/** Questions and finished answers of the conversation, in order. */
function conversationRows() {
  return [...chatColumn.children].filter(row => row.classList.contains('msg-user') || isFinishedAnswer(row));
}

function exportHtml(rows) {
  const parts = [];
  let nextFootnoteNumber = 1;
  for (const row of rows) {
    if (row.classList.contains('msg-user')) {
      parts.push(`<div class="export-question">${esc(row.textContent.trim())}</div>`);
      continue;
    }
    const { answerHtml, footnotesHtml, footnoteCount } = exportAnswerWithFootnotes(answerBodyOf(row), nextFootnoteNumber);
    nextFootnoteNumber += footnoteCount;
    parts.push(`<div class="export-answer prose-content">${answerHtml}</div>${footnotesHtml}`);
  }
  return parts.join('');
}

/* The page title is the PDF's default file name (and the browser may print it in the page header) */
function printRows(rows) {
  const exportEl = document.createElement('div');
  exportEl.id = 'conversation-export';
  exportEl.dir = 'rtl';
  exportEl.innerHTML = exportHtml(rows);
  document.body.appendChild(exportEl);
  document.body.classList.add('exporting-conversation');
  const pageTitle = document.title;
  const firstQuestion = rows.find(row => row.classList.contains('msg-user'));
  document.title = firstQuestion ? firstQuestion.textContent.trim().slice(0, 80) : pageTitle;
  window.addEventListener('afterprint', () => {
    exportEl.remove();
    document.body.classList.remove('exporting-conversation');
    document.title = pageTitle;
  }, { once: true });
  window.print();
}

let _exportMenu = null;

function closeExportMenu() {
  _exportMenu?.remove();
  _exportMenu = null;
}

function openExportMenu(button, answerRow) {
  closeExportMenu();
  _exportMenu = document.createElement('div');
  _exportMenu.className = 'share-menu answer-export-menu';
  _exportMenu.innerHTML = `
    <div class="share-menu-title">ייצוא ל-PDF</div>
    <button class="share-menu-btn" data-export-scope="answer">
      <span class="material-symbols-outlined">chat_bubble</span><span>השאלה והתשובה הזו</span>
    </button>
    <button class="share-menu-btn" data-export-scope="conversation">
      <span class="material-symbols-outlined">forum</span><span>כל השיחה</span>
    </button>`;
  document.body.appendChild(_exportMenu);
  const rect = button.getBoundingClientRect();
  const menuHeight = _exportMenu.offsetHeight;
  _exportMenu.style.top  = `${rect.top - menuHeight - 6 > 8 ? rect.top - menuHeight - 6 : rect.bottom + 6}px`;
  _exportMenu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - _exportMenu.offsetWidth - 8))}px`;
  _exportMenu.addEventListener('click', e => {
    const scope = e.target.closest('[data-export-scope]')?.dataset.exportScope;
    if (!scope) return;
    closeExportMenu();
    const question = questionBefore(answerRow);
    printRows(scope === 'answer' ? [question, answerRow].filter(Boolean) : conversationRows());
  });
}

document.addEventListener('mousedown', e => {
  if (_exportMenu && !e.target.closest('.answer-export-menu') && !e.target.closest('.answer-export-btn')) closeExportMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeExportMenu(); });

export function addAnswerExportButton(answerRow) {
  const card = answerRow?.querySelector(':scope > .msg-agent-card');
  if (!card || card.querySelector('.answer-export-row')) return;
  const buttonRow = document.createElement('div');
  buttonRow.className = 'answer-export-row';
  buttonRow.innerHTML = `
    <button type="button" class="answer-export-btn" title="ייצוא ל-PDF">
      <span class="material-symbols-outlined">picture_as_pdf</span><span>ייצוא</span>
    </button>`;
  const button = buttonRow.querySelector('button');
  button.addEventListener('click', () => {
    if (_exportMenu) closeExportMenu();
    else openExportMenu(button, answerRow);
  });
  card.appendChild(buttonRow);
}
