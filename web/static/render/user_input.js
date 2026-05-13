/**
 * render/user_input.js — option_select / text_input / deep_dive panels rendered
 * in response to a `user_input_required` event from the backend.
 *
 * The "submit" callback is injected by app.js via `configureUserInput({onSubmit})`
 * to avoid a circular import (controller → dispatch → user_input → controller).
 */
import { chatColumn, scrollToBottom } from '../dom.js';
import { esc, validateQuestion } from '../util.js';
import { state } from '../state.js';

let _onSubmit = null;

export function configureUserInput({ onSubmit }) {
  _onSubmit = onSubmit;
}

function callSubmit(outputVar, value) {
  if (_onSubmit) _onSubmit(outputVar, value);
  else console.error('[user_input] no onSubmit configured — call configureUserInput first');
}

export function renderUserInputPanel(data) {
  const ui        = data.ui         || 'text_input';
  const outputVar = data.output_var || 'user_input';

  if (ui === 'option_select') {
    renderOptionSelect(data, outputVar);
  } else if (ui === 'text_input') {
    renderTextInput(data, outputVar);
  } else if (ui === 'deep_dive') {
    const meetings = data.meetings || [];
    window.openProtocolBrowser(
      state.sessionId,
      meetings[0]?.meeting_id || null,
      meetings,
      {
        originalQuestion: data.original_question || data.query || state.lastQuestion || '',
        postCompletion:   false,
      },
    );
  }
}

function renderOptionSelect(data, outputVar) {
  const prompt  = data.prompt_he || data.prompt || 'בחר אפשרות:';
  const options = data.options || [];
  const multi   = data.multi_select || false;

  const wrap = document.createElement('div');
  wrap.className = 'msg-agent';
  const card = document.createElement('div');
  card.className = 'option-select-card';
  card.innerHTML = `<div class="option-select-prompt">${esc(prompt)}</div>`;
  const selected = new Set();

  options.forEach((opt) => {
    const label    = typeof opt === 'string' ? opt : (opt.label || opt.text || String(opt));
    const value    = typeof opt === 'string' ? opt : (opt.value ?? opt.label ?? opt.text ?? opt);
    const desc     = typeof opt === 'object' ? (opt.description || '') : '';
    const subtitle = typeof opt === 'object' ? (opt.subtitle || '') : '';
    const presel   = typeof opt === 'object' ? !!opt.selected : false;

    const btn = document.createElement('button');
    btn.className = 'option-btn';
    btn.dataset.value = JSON.stringify(value);

    let inner = `<span class="option-label">${esc(label)}</span>`;
    if (desc)     inner += `<span class="option-desc">${esc(desc)}</span>`;
    if (subtitle) inner += `<span class="option-subtitle">${esc(subtitle)}</span>`;
    btn.innerHTML = inner;

    if (presel) {
      btn.classList.add('selected');
      selected.add(value);
    }

    btn.addEventListener('click', () => {
      if (multi) {
        btn.classList.toggle('selected');
        const v = JSON.parse(btn.dataset.value);
        if (btn.classList.contains('selected')) selected.add(v);
        else selected.delete(v);
      } else {
        card.querySelectorAll('.option-btn').forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
        selected.clear();
        selected.add(JSON.parse(btn.dataset.value));
      }
    });
    card.appendChild(btn);
  });

  const submitEl = document.createElement('button');
  submitEl.className = 'option-submit';
  submitEl.textContent = 'המשך';
  submitEl.addEventListener('click', () => {
    if (selected.size === 0) return;
    const val = multi ? [...selected] : [...selected][0];
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    callSubmit(outputVar, val);
  });
  card.appendChild(submitEl);

  wrap.appendChild(card);
  chatColumn.appendChild(wrap);
  scrollToBottom();
}

function renderTextInput(data, outputVar) {
  const prompt = data.prompt_he || data.prompt || 'הכנס טקסט:';

  const wrap = document.createElement('div');
  wrap.className = 'msg-agent';
  const card = document.createElement('div');
  card.className = 'text-input-card';
  card.innerHTML = `<div class="text-input-prompt">${esc(prompt)}</div>`;

  const textarea = document.createElement('textarea');
  textarea.className = 'text-input-field';
  textarea.rows = 3;
  textarea.placeholder = 'הקלד כאן...';
  card.appendChild(textarea);

  const errHint = document.createElement('span');
  errHint.className = 'input-error hidden';
  card.appendChild(errHint);

  const submitEl = document.createElement('button');
  submitEl.className = 'text-input-submit';
  submitEl.textContent = 'שלח';
  submitEl.addEventListener('click', () => {
    const val = textarea.value.trim();
    if (!val) return;
    const err = validateQuestion(val);
    if (err) {
      errHint.textContent = err;
      errHint.classList.remove('hidden');
      return;
    }
    errHint.classList.add('hidden');
    textarea.disabled = true;
    submitEl.disabled = true;
    callSubmit(outputVar, val);
  });
  textarea.addEventListener('input', () => errHint.classList.add('hidden'));
  card.appendChild(submitEl);

  wrap.appendChild(card);
  chatColumn.appendChild(wrap);
  textarea.focus();
  scrollToBottom();
}
