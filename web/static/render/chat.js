/**
 * render/chat.js — bubbles, status row, agent answer card, error row,
 * post-completion "explore sources" button.
 */
import { chatColumn, scrollToBottom } from '../dom.js';
import { esc } from '../util.js';

export function appendUserBubble(text) {
  const row = document.createElement('div');
  row.className = 'msg-user';
  row.innerHTML = `<div class="msg-user-bubble">${esc(text)}</div>`;
  chatColumn.appendChild(row);
  scrollToBottom();
}

export function appendStatus(msg) {
  const el = document.createElement('div');
  el.className = 'msg-status';
  if (msg) setStatusMsg(el, msg);
  chatColumn.appendChild(el);
  return el;
}

export function setStatusMsg(el, msg) {
  if (!el) return;
  if (msg) {
    el.innerHTML =
      `<span class="thinking-dots"><span></span><span></span><span></span></span>` +
      `<span>${esc(msg)}</span>`;
    el.style.display = 'flex';
  } else {
    el.innerHTML = '';
    el.style.display = 'none';
  }
}

export function appendAgentCard() {
  const wrap = document.createElement('div');
  wrap.className = 'msg-agent';
  wrap.innerHTML =
    `<div class="msg-agent-card">` +
    `<div class="prose-content"></div>` +
    `</div>`;
  chatColumn.appendChild(wrap);
  scrollToBottom();
  return wrap;
}

export function appendErrorMsg(msg) {
  const el = document.createElement('div');
  el.className = 'msg-error';
  el.textContent = msg;
  chatColumn.appendChild(el);
  scrollToBottom();
  return el;
}

