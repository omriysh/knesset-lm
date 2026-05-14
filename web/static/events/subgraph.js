/**
 * events/subgraph.js — handlers for `subgraph_event` payloads.
 *
 * Backend emits a single `subgraph_event` SSE message whose nested `kind`
 * (llm_start/_thinking/_token/_done, progress, hook, done, error) identifies
 * the inner event. This module dispatches by kind, and for llm_start/hook
 * further dispatches by phase type / hook name.
 *
 * All handlers mutate the passed-in Session instance.
 */
import {
  addLiveStageCard, addCompletedStageCard, finaliseLiveCard, hasLiveCard,
  appendLiveThinking, appendLiveOutput,
} from '../render/stage_card.js';
import { finaliseSubgraphCard } from '../render/stages.js';
import { setStatusMsg } from '../render/chat.js';
import {
  classifyPhase, extractTaskLabel, mapToolResults, subgraphPhaseLabel,
  PROGRESS_MSGS,
} from '../transforms.js';

// ── llm_start ───────────────────────────────────────────────────────────

function handleExecutorLlmStart(phase, payload, session) {
  // Parallel steps share a single live card — recreated after each step_completed.
  const isFirst = session.executor.beginStep(phase.stepKey, payload.prompt || {});
  if (!isFirst) return;

  const taskLabel = extractTaskLabel(payload.prompt) || 'מחפש מידע...';
  if (!session.subgraphPhase || !session.subgraphPhase._isExecutor) {
    session.subgraphPhase = { _isExecutor: true, thinking: '', content: '' };
  }
  if (session.subgraphContainer && !hasLiveCard(session.subgraphContainer)) {
    addLiveStageCard(session.subgraphContainer, {
      label: taskLabel, stage: 'tool', loop: 0, prompt: payload.prompt || {},
    });
  }
}

function handleSynthesizerLlmStart(phase, payload, session) {
  // All turns of the synthesizer share one phase slot.
  if (!session.subgraphPhase || !session.subgraphPhase._isSynthesizer) {
    session.subgraphPhase = {
      _isSynthesizer: true,
      label:    subgraphPhaseLabel('synthesizer'),
      stage:    'research',
      thinking: '',
      content:  '',
      prompt:   payload.prompt || {},
    };
  }
  // Live card only for the synthesis turn (not expand turns).
  if (session.subgraphContainer && !session.subgraphPhase._liveCardCreated) {
    session.subgraphPhase._liveCardCreated = true;
    addLiveStageCard(session.subgraphContainer, {
      label:      session.subgraphPhase.label,
      stage:      session.subgraphPhase.stage,
      loop:       0,
      prompt:     session.subgraphPhase.prompt,
      openPrompt: true,
    });
  }
}

function handleSynthExpandLlmStart(phase, payload, session) {
  // Expand turns just keep the synthesizer phase slot alive without a card.
  if (!session.subgraphPhase || !session.subgraphPhase._isSynthesizer) {
    session.subgraphPhase = {
      _isSynthesizer: true,
      label:    subgraphPhaseLabel('synthesizer'),
      stage:    'research',
      thinking: '',
      content:  '',
      prompt:   payload.prompt || {},
    };
  }
}

function handleOtherLlmStart(phase, payload, session) {
  session.subgraphPhase = {
    label:       subgraphPhaseLabel(phase.name || payload.phase),
    stage:       'research',
    thinking:    '',
    content:     '',
    prompt:      payload.prompt || {},
    tools:       [],
    toolResults: [],
  };
  if (session.subgraphContainer) {
    addLiveStageCard(session.subgraphContainer, {
      label:      session.subgraphPhase.label,
      stage:      session.subgraphPhase.stage,
      loop:       0,
      prompt:     session.subgraphPhase.prompt,
      openPrompt: true,
    });
  }
}

const PHASE_LLM_START = {
  executor:           handleExecutorLlmStart,
  synthesizer:        handleSynthesizerLlmStart,
  synthesizer_expand: handleSynthExpandLlmStart,
  other:              handleOtherLlmStart,
};

function handleLlmStart(data, session) {
  const phase   = classifyPhase(data.name);
  const payload = data.payload || {};
  const fn      = PHASE_LLM_START[phase.type];
  if (fn) fn(phase, payload, session);
}

// ── llm_thinking / llm_token / llm_done ─────────────────────────────────

function handleLlmThinking(data, session) {
  const phase = classifyPhase(data.name);
  const text  = (data.payload || {}).text || '';
  if (session.subgraphPhase) session.subgraphPhase.thinking += text;
  if (phase.type !== 'executor' && phase.type !== 'synthesizer_expand' && session.subgraphContainer) {
    appendLiveThinking(session.subgraphContainer, text);
  }
}

function handleLlmToken(data, session) {
  const phase = classifyPhase(data.name);
  const text  = (data.payload || {}).text || '';
  if (session.subgraphPhase) session.subgraphPhase.content += text;
  if (phase.type !== 'executor' && phase.type !== 'synthesizer_expand' && session.subgraphContainer) {
    appendLiveOutput(session.subgraphContainer, text);
  }
}

function handleLlmDone(data, session) {
  const phase   = classifyPhase(data.name);
  const payload = data.payload || {};
  // Executor + synthesizer cards are finalised by their hook handlers.
  if (phase.type === 'executor' || phase.type === 'synthesizer' || phase.type === 'synthesizer_expand') return;
  if (!session.subgraphContainer || !session.subgraphPhase) return;

  const ph = session.subgraphPhase;
  finaliseLiveCard(session.subgraphContainer);
  addCompletedStageCard(session.subgraphContainer, {
    label:        ph.label,
    stage:        ph.stage,
    loop:         0,
    content:      payload.content || ph.content || '',
    thinking:     ph.thinking,
    tools:        ph.tools,
    tool_results: ph.toolResults,
    prompt:       ph.prompt,
    elapsed_ms:   payload.elapsed_ms || 0,
    llm_ms:       payload.elapsed_ms || 0,
  });
  session.subgraphPhase = null;
}

// ── progress ────────────────────────────────────────────────────────────

function handleProgress(data, session) {
  if (data.name === 'executing') session.executor.reset();
  const msg = PROGRESS_MSGS[data.name];
  if (msg) setStatusMsg(session.statusEl, msg);
}

// ── hook (step_completed / synthesizer_completed) ───────────────────────

function handleStepCompleted(data, session) {
  const p = data.payload || {};
  const stepKey       = p.step_id || '';
  const stillActive   = session.executor.completeStep(stepKey);
  const promptForStep = session.executor.promptFor(stepKey);

  session.subgraphPhase = null;
  const task = p.step_task ? `: ${p.step_task.slice(0, 40)}` : '';
  setStatusMsg(session.statusEl, `שלב הושלם${task}`);

  if (!session.subgraphContainer) return;
  finaliseLiveCard(session.subgraphContainer);

  addCompletedStageCard(session.subgraphContainer, {
    label:        `ביצוע: ${(p.step_task || 'שלב').slice(0, 60)}`,
    stage:        p.error && p.error !== 'skip' ? 'reviewer' : 'tool',
    content:      p.summary || '',
    tools:        p.tool_name ? [p.tool_name] : [],
    tool_results: mapToolResults(p),
    prompt:       promptForStep,
  });

  if (stillActive) {
    // Other steps still executing in parallel — restore a generic live card.
    session.subgraphPhase = { _isExecutor: true, thinking: '', content: '', prompt: {} };
    addLiveStageCard(session.subgraphContainer, { label: 'מחפש מידע...', stage: 'tool', loop: 0 });
  }
}

function handleSynthesizerCompleted(data, session) {
  const ph = session.subgraphPhase;
  if (ph && ph._isSynthesizer && session.subgraphContainer) {
    finaliseLiveCard(session.subgraphContainer);
    addCompletedStageCard(session.subgraphContainer, {
      label:        ph.label || subgraphPhaseLabel('synthesizer'),
      stage:        ph.stage || 'research',
      content:      ph.content || '',
      thinking:     ph.thinking || '',
      tools:        [],
      tool_results: [],
      prompt:       ph.prompt || {},
    });
  }
  session.subgraphPhase = null;
}

const HOOK_HANDLERS = {
  step_completed:        handleStepCompleted,
  synthesizer_completed: handleSynthesizerCompleted,
};

function handleHook(data, session) {
  const fn = HOOK_HANDLERS[data.name];
  if (fn) fn(data, session);
}

// ── done / error ────────────────────────────────────────────────────────

function handleSubgraphDone(data, session) {
  if (session.subgraphContainer) finaliseSubgraphCard(session.subgraphContainer);
  session.subgraphContainer = null;
  session.subgraphPhase     = null;
}

function handleSubgraphError(data, session) {
  if (session.subgraphContainer) {
    finaliseLiveCard(session.subgraphContainer);
    finaliseSubgraphCard(session.subgraphContainer);
  }
  session.subgraphContainer = null;
  session.subgraphPhase     = null;
}

// ── dispatch table ──────────────────────────────────────────────────────

const SUBGRAPH_HANDLERS = {
  llm_start:    handleLlmStart,
  llm_thinking: handleLlmThinking,
  llm_token:    handleLlmToken,
  llm_done:     handleLlmDone,
  progress:     handleProgress,
  hook:         handleHook,
  done:         handleSubgraphDone,
  error:        handleSubgraphError,
};

export function handleSubgraphEvent(data, session) {
  const fn = SUBGRAPH_HANDLERS[data.kind];
  if (fn) fn(data, session);
}
