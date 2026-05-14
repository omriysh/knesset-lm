/**
 * dom.js — DOM refs, scroll helpers, settings flag, query-error UI.
 */
export const chatColumn   = document.getElementById('chat-column');
export const welcomeEl    = document.getElementById('welcome-state');
export const queryInput   = document.getElementById('query-input');
export const submitBtn    = document.getElementById('submit-btn');
export const queryErrorEl = document.getElementById('query-error');

export function scrollToBottom() {
  chatColumn.scrollTop = chatColumn.scrollHeight;
}

export function stagesAlways() {
  return localStorage.getItem('showStagesAlways') === 'true';
}

let _queryErrorTimer = null;
export function showQueryError(msg) {
  if (!queryErrorEl) return;
  queryErrorEl.textContent = msg;
  queryErrorEl.classList.remove('hidden');
  clearTimeout(_queryErrorTimer);
  _queryErrorTimer = setTimeout(() => {
    queryErrorEl.classList.add('hidden');
    queryErrorEl.textContent = '';
  }, 4000);
}

export function clearQueryError() {
  if (!queryErrorEl) return;
  clearTimeout(_queryErrorTimer);
  queryErrorEl.classList.add('hidden');
  queryErrorEl.textContent = '';
}
