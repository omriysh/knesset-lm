/**
 * reconnect.js — recovers an SSE stream that was interrupted (e.g. mobile
 * focus loss). Uses the same /api/research/{id}/stream endpoint to replay
 * already-fired events from the server's event log, plus any new ones,
 * until done / user_input_required / error / persistent failure.
 *
 * The replay path uses a reduced handler set: no live-card streaming, no
 * llm_start (so executor prompts are unavailable — passed as `{}`). It
 * shares render helpers + mapToolResults with the normal dispatch path.
 */
import { state } from './state.js';
import {
  chatColumn, queryInput, submitBtn, scrollToBottom, stagesAlways,
} from './dom.js';
import { sseLines } from './sse.js';
import {
  appendStagesCard, addSubgraphWrapperCard, finaliseSubgraphCard,
} from './render/stages.js';
import { addCompletedStageCard } from './render/stage_card.js';
import {
  appendStatus, setStatusMsg, appendAgentCard, appendErrorMsg,
} from './render/chat.js';
import { applyEvidenceCitations, buildSourcesHtml } from './render/citations.js';
import { renderUserInputPanel } from './render/user_input.js';
import { mapToolResults } from './transforms.js';
import { esc } from './util.js';

export function scheduleReconnect(delayMs = 1500) {
  setTimeout(attemptReconnect, delayMs);
}

function injectPartialNotice(containerEl) {
  if (containerEl.querySelector('.reconnect-partial-notice')) return;
  const notice = document.createElement('div');
  notice.className = 'reconnect-partial-notice';
  notice.textContent = 'הייתה בעיית חיבור רגעית, תהליך המחקר עלול להופיע באופן חלקי. התוצאה הסופית תוצג עם כל הסימוכין לאחר סיום העיבוד, גם אם חלק מהשלבים לא יופיעו כאן :)';
  containerEl.insertBefore(notice, containerEl.firstChild);
}

class ReplaySession {
  constructor(sid, statusEl) {
    this.sid               = sid;
    this.statusEl          = statusEl;
    this.stagesEl          = null;
    this.subgraphContainer = null;
    this.agentEl           = null;
    this.rawAnswer         = '';
    this.pendingFootnotes  = [];
    this.pendingCitations  = [];
  }
}

export async function attemptReconnect() {
  if (state.reconnecting || !state.reconnectSessionId) return;
  state.reconnecting = true;
  state.running      = true;
  submitBtn.disabled = true;

  // Drop disconnect error card — we're recovering.
  if (state.reconnectErrorEl) { state.reconnectErrorEl.remove(); state.reconnectErrorEl = null; }

  // Finalise any live stage cards left over from the disconnected stream.
  // Inject a partial-data notice into pre-existing subgraph cards so users know.
  chatColumn.querySelectorAll('.subgraph-card .subgraph-inner-stages').forEach(injectPartialNotice);
  chatColumn.querySelectorAll('.live-stage-card').forEach(card => {
    card.classList.remove('live-stage-card');
    const dot = card.querySelector('.live-thinking-dot');
    if (dot) dot.remove();
  });

  const sid      = state.reconnectSessionId;
  const statusEl = appendStatus('מחבר מחדש...');

  try {
    for (let attempt = 0; attempt < 30 && state.reconnectSessionId; attempt++) {
      if (attempt > 0) {
        setStatusMsg(statusEl, 'עדיין מעבד...');
        await new Promise(r => setTimeout(r, attempt < 5 ? 3000 : 8000));
        if (!state.reconnectSessionId) break;
      }
      const continueRetry = await replayOnce(sid, statusEl);
      if (!continueRetry) break;
    }
  } finally {
    setStatusMsg(statusEl, '');
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    state.reconnectErrorEl = null;
    state.running          = false;
    submitBtn.disabled     = false;
    state.reconnecting     = false;
    queryInput.focus();
    scrollToBottom();
  }
}

async function replayOnce(sid, statusEl) {
  try {
    const res = await fetch(`/api/research/${sid}/stream`);
    if (!res.ok) return false;
    const replay = new ReplaySession(sid, statusEl);
    let continueRetry = false;
    for await (const { event, data } of sseLines(res)) {
      const status = handleReplayEvent(event, data, replay);
      if (status === 'still_running') { continueRetry = true; break; }
      if (status === 'terminal')      { break; }
    }
    return continueRetry;
  } catch (exc) {
    console.error('[reconnect] network error on attempt, will retry:', exc);
    return false;
  }
}

function handleReplayEvent(event, data, replay) {
  switch (event) {
    case 'still_running':        return 'still_running';
    case 'node_start':           return replayNodeStart(data, replay);
    case 'node_result':          return replayNodeResult(data, replay);
    case 'subgraph_event':       return replaySubgraph(data, replay);
    case 'token':                return replayToken(data, replay);
    case 'done':                 return replayDone(data, replay);
    case 'user_input_required':  return replayUserInput(data, replay);
    case 'error':                return replayError(data, replay);
    default:                     return null;
  }
}

function replayNodeStart(data, replay) {
  if (!data.subgraph) return null;
  if (!replay.stagesEl) {
    // Replace the old partial stages card with a fresh replay one.
    if (state.currentStagesEl) {
      state.currentStagesEl.closest('.msg-agent')?.remove();
      state.currentStagesEl = null;
    }
    replay.stagesEl = appendStagesCard();
    state.currentStagesEl = replay.stagesEl;
  }
  replay.subgraphContainer = addSubgraphWrapperCard(replay.stagesEl, data);
  injectPartialNotice(replay.subgraphContainer);
  return null;
}

function replayNodeResult(data, replay) {
  if (!data.subgraph) return null;
  const footnotes = data.subgraph?.outputs?.footnotes;
  if (Array.isArray(footnotes) && footnotes.length) replay.pendingFootnotes.push(...footnotes);
  const citations = data.subgraph?.outputs?.citations;
  if (Array.isArray(citations)) replay.pendingCitations.push(...citations);
  finaliseSubgraphCard(replay.subgraphContainer);
  replay.subgraphContainer = null;
  return null;
}

function replaySubgraph(data, replay) {
  const kind    = data.kind    || '';
  const name    = data.name    || '';
  const payload = data.payload || {};

  if (kind === 'hook' && name === 'step_completed' && replay.subgraphContainer) {
    const stepTask = payload.step_task || 'שלב';
    const toolName = payload.tool_name || '';
    const hasError = !!(payload.error && payload.error !== 'skip');
    addCompletedStageCard(replay.subgraphContainer, {
      label:        `ביצוע: ${stepTask.slice(0, 60)}`,
      stage:        hasError ? 'reviewer' : 'tool',
      content:      payload.summary || '',
      tools:        toolName ? [toolName] : [],
      tool_results: mapToolResults(payload),
      prompt:       {},  // replay has no llm_start — prompt unavailable
    });
  } else if (kind === 'hook' && name === 'synthesizer_completed' && replay.subgraphContainer) {
    addCompletedStageCard(replay.subgraphContainer, {
      label: 'מסכם ממצאים', stage: 'research',
      content: '', thinking: '', tools: [], tool_results: [], prompt: {},
    });
  } else if (kind === 'done' && replay.subgraphContainer) {
    finaliseSubgraphCard(replay.subgraphContainer);
    replay.subgraphContainer = null;
  }
  return null;
}

function replayToken(data, replay) {
  replay.rawAnswer += data.text || '';
  if (!replay.agentEl) {
    replay.agentEl = appendAgentCard();
    setStatusMsg(replay.statusEl, '');
  }
  const body = replay.agentEl.querySelector('.prose-content');
  if (body) body.innerHTML = esc(replay.rawAnswer) + '<span class="stream-cursor"></span>';
  return null;
}

function replayDone(data, replay) {
  state.reconnectSessionId = null;
  state.sessionId          = replay.sid;
  if (replay.stagesEl && !stagesAlways()) {
    const wrap = replay.stagesEl.parentElement;
    if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
  }
  if (replay.agentEl) {
    const body = replay.agentEl.querySelector('.prose-content');
    if (body) {
      body.innerHTML = replay.rawAnswer ? marked.parse(replay.rawAnswer) : '';
      const cursor = replay.agentEl.querySelector('.stream-cursor');
      if (cursor) cursor.remove();
      if (replay.pendingFootnotes.length) {
        applyEvidenceCitations(body, replay.pendingFootnotes, replay.pendingCitations);
        replay.agentEl.insertAdjacentHTML(
          'beforeend',
          buildSourcesHtml(replay.pendingFootnotes, replay.sid),
        );
      }
    }
  }
  return 'terminal';
}

function replayUserInput(data, replay) {
  state.reconnectSessionId = null;
  state.sessionId          = replay.sid;
  const enriched = data.session_id ? data : { ...data, session_id: replay.sid };
  renderUserInputPanel(enriched);
  return 'terminal';
}

function replayError(data, replay) {
  state.reconnectSessionId = null;
  appendErrorMsg(data.error || 'שגיאה לא ידועה');
  return 'terminal';
}
