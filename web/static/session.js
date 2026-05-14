/**
 * session.js — Session class and ExecutorState helper.
 *
 * A `Session` owns the state for one query: per-event handlers mutate its
 * fields (agentEl, subgraphContainer, subgraphPhase, executor, pending
 * footnotes/citations). `run()` POSTs to the given URL and dispatches each
 * SSE message through the event handler table.
 *
 * `ExecutorState` tracks which executor steps are still running and stores
 * each step's prompt so the completed card can show it on `step_completed`,
 * regardless of which step finished first (matters for parallel execution).
 */
import { sseLines } from './sse.js';
import { handleEvent } from './events/dispatch.js';

export class ExecutorState {
  constructor() {
    this.activeSteps = new Set();
    this.stepPrompts = {};
  }

  /** Begin tracking a step. Returns true the first time only. */
  beginStep(stepKey, prompt) {
    if (this.activeSteps.has(stepKey)) return false;
    this.activeSteps.add(stepKey);
    this.stepPrompts[stepKey] = prompt;
    return true;
  }

  /** Mark step complete. Returns true if any other step is still active. */
  completeStep(stepKey) {
    this.activeSteps.delete(stepKey);
    return this.activeSteps.size > 0;
  }

  promptFor(stepKey) {
    return this.stepPrompts[stepKey] || {};
  }

  reset() {
    this.activeSteps = new Set();
    this.stepPrompts = {};
  }
}

export class Session {
  constructor({ stagesEl, statusEl }) {
    this.stagesEl          = stagesEl;
    this.statusEl          = statusEl;
    this.agentEl           = null;
    this.rawAnswer         = '';
    this.subgraphContainer = null;
    this.subgraphPhase     = null;
    this.executor          = new ExecutorState();
    this.pendingFootnotes  = [];
    this.pendingCitations  = [];
  }

  async run(url, body) {
    const res = await fetch(url, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    for await (const { event, data } of sseLines(res)) {
      handleEvent(event, data, this);
    }
  }
}
