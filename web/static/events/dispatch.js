/**
 * events/dispatch.js — top-level SSE event dispatcher.
 *
 * Routes incoming `{event, data}` pairs to per-event handlers. Each handler
 * mutates the passed-in Session instance. Subgraph events are delegated to
 * the more specialized dispatcher in ./subgraph.js.
 */
import { state } from '../state.js';
import { stagesAlways, scrollToBottom } from '../dom.js';
import { esc } from '../util.js';

import {
  addLiveStageCard, addCompletedStageCard, finaliseLiveCard, appendLiveThinking,
} from '../render/stage_card.js';
import { addSubgraphWrapperCard, finaliseSubgraphCard } from '../render/stages.js';
import {
  appendAgentCard, appendErrorMsg, appendExploreSourcesButton, setStatusMsg,
} from '../render/chat.js';
import { renderUserInputPanel } from '../render/user_input.js';

import { handleSubgraphEvent } from './subgraph.js';

function onSessionId(data, session) {
  state.sessionId          = data.session_id;
  state.reconnectSessionId = data.session_id;
}

function onStatus(data, session) {
  setStatusMsg(session.statusEl, data.msg || '');
}

function onNodeStart(data, session) {
  if (data.subgraph) {
    session.subgraphContainer = addSubgraphWrapperCard(session.stagesEl, data);
  } else {
    addLiveStageCard(session.stagesEl, data);
  }
}

function onThinkingToken(data, session) {
  appendLiveThinking(session.stagesEl, data.text || '');
}

function onNodeResult(data, session) {
  if (data.subgraph) {
    const footnotes = data.subgraph?.outputs?.footnotes;
    if (Array.isArray(footnotes) && footnotes.length > 0) {
      session.pendingFootnotes = footnotes;
    }
    const citations = data.subgraph?.outputs?.citations;
    if (Array.isArray(citations)) {
      session.pendingCitations = citations;
    }
    finaliseSubgraphCard(session.subgraphContainer);
    session.subgraphContainer = null;
    session.subgraphPhase     = null;
  } else {
    finaliseLiveCard(session.stagesEl);
    addCompletedStageCard(session.stagesEl, data);
  }
}

function onToken(data, session) {
  session.rawAnswer += data.text || '';
  if (!session.agentEl) {
    session.agentEl = appendAgentCard();
    setStatusMsg(session.statusEl, '');
  }
  const body = session.agentEl.querySelector('.prose-content');
  if (body) {
    body.innerHTML = esc(session.rawAnswer) + '<span class="stream-cursor"></span>';
  }
  scrollToBottom();
}

function onDone(data, session) {
  state.reconnectSessionId = null;
  finaliseLiveCard(session.stagesEl);
  // Reveal stages wrap (collapsed) even if user never clicked — allows post-hoc inspection.
  if (!stagesAlways()) {
    const wrap = session.stagesEl?.parentElement;
    if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
  }
  appendExploreSourcesButton(state.sessionId, state.lastQuestion);
}

function onError(data, session) {
  state.reconnectSessionId = null;
  finaliseLiveCard(session.stagesEl);
  setStatusMsg(session.statusEl, '');
  appendErrorMsg(data.error || 'שגיאה לא ידועה');
}

function onUserInputRequired(data, session) {
  finaliseLiveCard(session.stagesEl);
  setStatusMsg(session.statusEl, '');
  renderUserInputPanel(data);
}

function noop() {}

const EVENT_HANDLERS = {
  session_id:          onSessionId,
  status:              onStatus,
  node_start:          onNodeStart,
  thinking_token:      onThinkingToken,
  node_result:         onNodeResult,
  subgraph_event:      handleSubgraphEvent,
  token:               onToken,
  done:                onDone,
  error:               onError,
  user_input_required: onUserInputRequired,
  user_paused:         noop,
};

export function handleEvent(eventName, data, session) {
  const fn = EVENT_HANDLERS[eventName];
  if (fn) fn(data, session);
}
