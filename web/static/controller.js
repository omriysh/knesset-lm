/**
 * controller.js — startQuery and submitResponse.
 *
 * Both spin up a Session, run it against the right backend endpoint, and
 * finalise the agent card on completion. The finalize step is shared.
 * On network failure, schedules a reconnect attempt if appropriate.
 */
import { Session } from './session.js';
import { state } from './state.js';
import {
  welcomeEl, queryInput, submitBtn,
  showQueryError, clearQueryError, scrollToBottom,
} from './dom.js';
import { validateQuestion } from './util.js';
import {
  appendUserBubble, appendStatus, setStatusMsg, appendErrorMsg,
} from './render/chat.js';
import { appendStagesCard, wireStatusToggle } from './render/stages.js';
import { applyEvidenceCitations, buildSourcesHtml } from './render/citations.js';
import { scheduleReconnect } from './reconnect.js';

export async function startQuery() {
  const question = queryInput.value.trim();
  if (!question) return;
  const err = validateQuestion(question);
  if (err) { showQueryError(err); return; }
  clearQueryError();

  state.lastQuestion = question;
  state.running      = true;
  submitBtn.disabled = true;
  queryInput.value         = '';
  queryInput.style.height  = 'auto';

  if (welcomeEl) welcomeEl.style.display = 'none';
  document.body.classList.remove('show-welcome');

  appendUserBubble(question);

  const statusEl = appendStatus('');
  const stagesEl = appendStagesCard();
  state.currentStagesEl = stagesEl;
  wireStatusToggle(statusEl, stagesEl);

  await runSession('/api/research/start', { question }, statusEl, stagesEl);
}

export async function submitResponse(outputVar, value) {
  if (!state.sessionId) return;
  state.running      = true;
  submitBtn.disabled = true;

  const statusEl = appendStatus('ממשיך...');
  const stagesEl = appendStagesCard();
  state.currentStagesEl = stagesEl;
  wireStatusToggle(statusEl, stagesEl);

  await runSession(
    `/api/research/${state.sessionId}/respond`,
    { output_var: outputVar, value },
    statusEl, stagesEl,
  );
}

async function runSession(url, body, statusEl, stagesEl) {
  const session = new Session({ stagesEl, statusEl });
  try {
    await session.run(url, body);
  } catch (exc) {
    console.error('[controller] session error:', exc);
    setStatusMsg(statusEl, '');
    state.reconnectErrorEl = appendErrorMsg('שגיאת חיבור: ' + exc.message);
    if (state.reconnectSessionId && document.visibilityState === 'visible') {
      scheduleReconnect(1500);
    }
  } finally {
    finalize(session, statusEl);
  }
}

function finalize(session, statusEl) {
  const willReconnect = !!state.reconnectSessionId;

  if (session.agentEl && session.rawAnswer && !willReconnect) {
    const body = session.agentEl.querySelector('.prose-content');
    if (body) {
      body.innerHTML = marked.parse(session.rawAnswer);
      const cursor = session.agentEl.querySelector('.stream-cursor');
      if (cursor) cursor.remove();
      if (session.pendingFootnotes.length > 0) {
        applyEvidenceCitations(body, session.pendingFootnotes, session.pendingCitations);
        session.agentEl.insertAdjacentHTML(
          'beforeend',
          buildSourcesHtml(session.pendingFootnotes, state.sessionId),
        );
      }
    }
  } else if (session.agentEl && willReconnect) {
    session.agentEl.remove();
  }

  setStatusMsg(statusEl, '');
  if (statusEl && !statusEl.textContent.trim()) statusEl.remove();

  if (!willReconnect) {
    state.running      = false;
    submitBtn.disabled = false;
    queryInput.focus();
  }
  scrollToBottom();
}
