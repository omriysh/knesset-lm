/**
 * app.js — KnessetLM chat shell
 *
 * Manages the single-column chat interface.  Sends questions to
 * /api/research/start, streams SSE events, renders chat messages
 * (user bubbles, AI stages card, agent response, status, errors).
 *
 * Interactivity: handles user_input_required events for option_select
 * and text_input node types.  Session id is stored for resume via
 * /api/research/{id}/respond.
 */

/* ── DOM refs ──────────────────────────────────────────────────── */
const chatColumn  = document.getElementById('chat-column');
const welcomeEl   = document.getElementById('welcome-state');
const queryInput  = document.getElementById('query-input');
const submitBtn   = document.getElementById('submit-btn');

marked.use({ breaks: true, gfm: true });

/* ── Tool result lazy-load config ──────────────────────────── */
const TOOL_RESULT_UNLOAD_MS = 30_000;

/* ── Settings ───────────────────────────────────────────────── */
function _stagesAlways() {
  return localStorage.getItem('showStagesAlways') === 'true';
}
function openSettings() {
  document.getElementById('settings-overlay').classList.add('open');
  document.getElementById('toggle-stages-always').checked = _stagesAlways();
}
function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
}

let _helpLoaded = false;
async function openHelp() {
  document.getElementById('help-overlay').classList.add('open');
  if (_helpLoaded) return;
  try {
    const md = await fetch('/api/help').then(r => r.text());
    document.getElementById('help-content').innerHTML = marked.parse(md);
    _helpLoaded = true;
  } catch {
    document.getElementById('help-content').textContent = 'שגיאה בטעינת העזרה.';
  }
}
function closeHelp() {
  document.getElementById('help-overlay').classList.remove('open');
}
function onStagesAlwaysToggle(el) {
  localStorage.setItem('showStagesAlways', el.checked ? 'true' : 'false');
}

/* ── State ─────────────────────────────────────────────────────── */
let running       = false;
let sessionId     = null;   // current or last session id
let _lastQuestion = '';     // most recent user question (for explore-sources)

/* ── Textarea auto-resize ───────────────────────────────────────── */
queryInput.addEventListener('input', () => {
  queryInput.style.height = 'auto';
  queryInput.style.height = Math.min(queryInput.scrollHeight, 160) + 'px';
});

/* ── Submit on Ctrl+Enter ───────────────────────────────────────── */
queryInput.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    if (!running) startQuery();
  }
});
submitBtn.addEventListener('click', () => { if (!running) startQuery(); });

/* ═══════════════════════════════════════════════════════════════════
   MAIN QUERY FLOW
═══════════════════════════════════════════════════════════════════ */

async function startQuery() {
  const question = queryInput.value.trim();
  if (!question) return;

  _lastQuestion = question;
  running = true;
  submitBtn.disabled = true;
  queryInput.value = '';
  queryInput.style.height = 'auto';

  // Hide welcome state on first query; drop input bar to bottom
  if (welcomeEl) welcomeEl.style.display = 'none';
  document.body.classList.remove('show-welcome');

  // Render user bubble
  appendUserBubble(question);

  // Status above stages; stages collapsed until clicked
  const statusEl   = appendStatus('');      // appears first (above)
  const stagesEl   = appendStagesCard();    // appears below, collapsed
  _wireStatusToggle(statusEl, stagesEl);
  let   agentEl           = null;
  let   rawAnswer         = '';
  let   subgraphContainer = null;
  let   subgraphPhase     = null;
  let   curEvent          = '';
  let   buf               = '';

  try {
    const res = await fetch('/api/research/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ question }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      const lines = buf.split('\n');
      buf = lines.pop();

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          curEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          let data;
          try { data = JSON.parse(line.slice(6)); } catch { continue; }
          handleEvent(curEvent, data, {
            stagesEl, statusEl,
            get agentEl()           { return agentEl; },           set agentEl(v)           { agentEl = v; },
            get rawAnswer()         { return rawAnswer; },         set rawAnswer(v)         { rawAnswer = v; },
            get subgraphContainer() { return subgraphContainer; }, set subgraphContainer(v) { subgraphContainer = v; },
            get subgraphPhase()     { return subgraphPhase; },     set subgraphPhase(v)     { subgraphPhase = v; },
          });
        }
      }
    }
  } catch (err) {
    setStatusMsg(statusEl, '');
    appendErrorMsg('שגיאת חיבור: ' + err.message);
  } finally {
    // Finalize streamed answer
    if (agentEl && rawAnswer) {
      const body = agentEl.querySelector('.prose-content');
      if (body) {
        body.innerHTML = marked.parse(rawAnswer);
        // Remove streaming cursor if present
        const cursor = agentEl.querySelector('.stream-cursor');
        if (cursor) cursor.remove();
      }
    }
    setStatusMsg(statusEl, '');
    // Remove empty status row
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    running = false;
    submitBtn.disabled = false;
    queryInput.focus();
    scrollToBottom();
  }
}

function handleEvent(ev, data, refs) {
  switch (ev) {
    case 'session_id':
      sessionId = data.session_id;
      break;

    case 'status':
      setStatusMsg(refs.statusEl, data.msg || '');
      break;

    case 'node_start':
      if (data.subgraph) {
        refs.subgraphContainer = addSubgraphWrapperCard(refs.stagesEl, data);
      } else {
        addLiveStageCard(refs.stagesEl, data);
      }
      break;

    case 'thinking_token':
      appendLiveThinking(refs.stagesEl, data.text || '');
      break;

    case 'node_result':
      if (data.subgraph) {
        finaliseSubgraphCard(refs.subgraphContainer);
        refs.subgraphContainer = null;
        refs.subgraphPhase     = null;
      } else {
        finaliseLiveCard(refs.stagesEl);
        addCompletedStageCard(refs.stagesEl, data);
      }
      break;

    case 'subgraph_event': {
      const sg_kind    = data.kind    || '';
      const sg_name    = data.name    || '';
      const sg_payload = data.payload || {};

      const isExecutorPhase = sg_name.startsWith('executor:');

      if (sg_kind === 'llm_start') {
        if (isExecutorPhase) {
          // All turns of the same step share one phase slot; don't create a card.
          if (!refs.subgraphPhase || !refs.subgraphPhase._isExecutor) {
            refs.subgraphPhase = { _isExecutor: true, thinking: '', content: '', prompt: sg_payload.prompt || {} };
          }
        } else {
          refs.subgraphPhase = {
            label:       _subgraphPhaseLabel(sg_name || sg_payload.phase),
            stage:       'research',
            thinking:    '',
            content:     '',
            prompt:      sg_payload.prompt || {},
            tools:       [],
            toolResults: [],
          };
          if (refs.subgraphContainer) {
            addLiveStageCard(refs.subgraphContainer, {
              label:      refs.subgraphPhase.label,
              stage:      refs.subgraphPhase.stage,
              loop:       0,
              prompt:     refs.subgraphPhase.prompt,
              openPrompt: true,
            });
          }
        }

      } else if (sg_kind === 'llm_thinking') {
        if (refs.subgraphPhase) refs.subgraphPhase.thinking += sg_payload.text || '';
        // Show thinking in the live card only for non-executor phases
        if (!isExecutorPhase && refs.subgraphContainer) {
          appendLiveThinking(refs.subgraphContainer, sg_payload.text || '');
        }

      } else if (sg_kind === 'llm_token') {
        if (refs.subgraphPhase) refs.subgraphPhase.content += sg_payload.text || '';
        if (!isExecutorPhase && refs.subgraphContainer) {
          appendLiveOutput(refs.subgraphContainer, sg_payload.text || '');
        }

      } else if (sg_kind === 'llm_done') {
        if (isExecutorPhase) {
          // Accumulate; card is finalized by step_completed
        } else if (refs.subgraphContainer && refs.subgraphPhase) {
          const ph = refs.subgraphPhase;
          finaliseLiveCard(refs.subgraphContainer);
          addCompletedStageCard(refs.subgraphContainer, {
            label:        ph.label,
            stage:        ph.stage,
            loop:         0,
            content:      sg_payload.content || ph.content || '',
            thinking:     ph.thinking,
            tools:        ph.tools,
            tool_results: ph.toolResults,
            prompt:       ph.prompt,
            elapsed_ms:   sg_payload.elapsed_ms || 0,
            llm_ms:       sg_payload.elapsed_ms || 0,
          });
          refs.subgraphPhase = null;
        }

      } else if (sg_kind === 'progress') {
        const _PROGRESS_MSGS = {
          planning_started:         'מתכנן שלבי חקר...',
          executing:                'מבצע שלבי חקר...',
          synthesizing:             'מסכם ממצאים...',
          replanning:               'מתכנן מחדש...',
          critic_pre_revise:        'מתקן תוכנית...',
          validator_revise:         'מאמת תוכנית...',
          critic_post_started:      'בודק תוצאות...',
          critic_post_replan_capped:'מסכם למרות תוצאות חלקיות...',
        };
        const msg = _PROGRESS_MSGS[sg_name];
        if (msg) setStatusMsg(refs.statusEl, msg);

      } else if (sg_kind === 'hook' && sg_name === 'step_completed') {
        // Save prompt before clearing phase
        const executorPrompt = refs.subgraphPhase ? (refs.subgraphPhase.prompt || {}) : {};
        refs.subgraphPhase = null;
        const task = sg_payload.step_task ? `: ${sg_payload.step_task.slice(0, 40)}` : '';
        setStatusMsg(refs.statusEl, `שלב הושלם${task}`);
        if (refs.subgraphContainer) {
          const stepTask        = sg_payload.step_task || 'שלב';
          const toolName        = sg_payload.tool_name || '';
          const hasError        = !!(sg_payload.error && sg_payload.error !== 'skip');
          const fullResult      = sg_payload.full || '';
          const toolCalls       = sg_payload.tool_calls || [];
          const toolCallResults = sg_payload.tool_call_results || [];

          let toolResults;
          if (toolCallResults.length > 0) {
            toolResults = toolCallResults.map(tc => ({
              name:       tc.name,
              args:       tc.args || {},
              result:     tc.full || tc.summary || '',
              result_ref: tc.result_ref || null,
            }));
          } else if (toolCalls.length === 1) {
            toolResults = [{ name: toolCalls[0].name || toolName || 'כלי', args: toolCalls[0].args || {}, result: fullResult }];
          } else if (toolCalls.length > 1) {
            toolResults = toolCalls.map(tc => ({ name: tc.name, args: tc.args || {}, result: '' }));
            if (fullResult) toolResults.push({ name: 'תוצאה מלאה', args: {}, result: fullResult });
          } else if (fullResult) {
            toolResults = [{ name: toolName || 'תוצאה מלאה', args: {}, result: fullResult }];
          } else {
            toolResults = [];
          }

          addCompletedStageCard(refs.subgraphContainer, {
            label:        `ביצוע: ${stepTask.slice(0, 60)}`,
            stage:        hasError ? 'reviewer' : 'tool',
            content:      sg_payload.summary || '',
            tools:        toolName ? [toolName] : [],
            tool_results: toolResults,
            prompt:       executorPrompt,
          });
        }

      } else if (sg_kind === 'done') {
        if (refs.subgraphContainer) finaliseSubgraphCard(refs.subgraphContainer);
        refs.subgraphContainer = null;
        refs.subgraphPhase     = null;

      } else if (sg_kind === 'error') {
        if (refs.subgraphContainer) {
          finaliseLiveCard(refs.subgraphContainer);
          finaliseSubgraphCard(refs.subgraphContainer);
        }
        refs.subgraphContainer = null;
        refs.subgraphPhase     = null;
      }
      break;
    }

    case 'token': {
      refs.rawAnswer += data.text || '';
      if (!refs.agentEl) {
        refs.agentEl = appendAgentCard();
        setStatusMsg(refs.statusEl, '');
      }
      const body = refs.agentEl.querySelector('.prose-content');
      if (body) {
        body.innerHTML = esc(refs.rawAnswer) + '<span class="stream-cursor"></span>';
      }
      scrollToBottom();
      break;
    }

    case 'done': {
      finaliseLiveCard(refs.stagesEl);
      // Reveal stages wrap (collapsed) even if user never clicked — allows post-hoc inspection
      if (!_stagesAlways()) {
        const wrap = refs.stagesEl?.parentElement;
        if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
      }
      // "Explore sources" button — opens protocol browser post-completion
      const exploreWrap = document.createElement('div');
      exploreWrap.className = 'explore-sources-row';
      const exploreBtn = document.createElement('button');
      exploreBtn.className = 'explore-sources-btn';
      exploreBtn.textContent = 'חקור בפרוטוקולים';
      exploreBtn.addEventListener('click', async () => {
        exploreBtn.disabled = true;
        exploreBtn.innerHTML = '<span class="btn-spinner"></span> טוען…';
        try {
          const q   = encodeURIComponent(_lastQuestion);
          const res = await fetch(`/api/research/${sessionId}/rag?query=${q}&top_k=20`);
          const rd  = await res.json();
          const mts = rd.meetings || [];
          openProtocolBrowser(sessionId, mts[0]?.meeting_id || null, mts, {
            originalQuestion: _lastQuestion,
            postCompletion: true,
          });
          exploreBtn.innerHTML = 'חקור בפרוטוקולים';
          exploreBtn.disabled = false;
        } catch {
          exploreBtn.disabled = false;
          exploreBtn.innerHTML = 'שגיאה — נסה שוב';
        }
      });
      exploreWrap.appendChild(exploreBtn);
      chatColumn.appendChild(exploreWrap);
      scrollToBottom();
      break;
    }

    case 'error':
      finaliseLiveCard(refs.stagesEl);
      setStatusMsg(refs.statusEl, '');
      appendErrorMsg(data.error || 'שגיאה לא ידועה');
      break;

    case 'user_input_required':
      finaliseLiveCard(refs.stagesEl);
      setStatusMsg(refs.statusEl, '');
      renderUserInputPanel(data);
      break;

    case 'user_paused':
      // Stream closed — input panel already rendered above
      break;
  }
}

/* ═══════════════════════════════════════════════════════════════════
   RESUME FLOW  (after user_input_required)
═══════════════════════════════════════════════════════════════════ */

async function submitResponse(outputVar, value) {
  if (!sessionId) return;

  running = true;
  submitBtn.disabled = true;

  const statusEl          = appendStatus('ממשיך...');
  const stagesEl          = appendStagesCard();
  _wireStatusToggle(statusEl, stagesEl);
  let   agentEl           = null;
  let   rawAnswer         = '';
  let   subgraphContainer = null;
  let   subgraphPhase     = null;
  let   curEvent          = '';
  let   buf               = '';

  try {
    const res = await fetch(`/api/research/${sessionId}/respond`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ output_var: outputVar, value }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value: chunk } = await reader.read();
      if (done) break;
      buf += decoder.decode(chunk, { stream: true });

      const lines = buf.split('\n');
      buf = lines.pop();

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          curEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          let data;
          try { data = JSON.parse(line.slice(6)); } catch { continue; }
          handleEvent(curEvent, data, {
            stagesEl, statusEl,
            get agentEl()           { return agentEl; },           set agentEl(v)           { agentEl = v; },
            get rawAnswer()         { return rawAnswer; },         set rawAnswer(v)         { rawAnswer = v; },
            get subgraphContainer() { return subgraphContainer; }, set subgraphContainer(v) { subgraphContainer = v; },
            get subgraphPhase()     { return subgraphPhase; },     set subgraphPhase(v)     { subgraphPhase = v; },
          });
        }
      }
    }
  } catch (err) {
    setStatusMsg(statusEl, '');
    appendErrorMsg('שגיאת חיבור: ' + err.message);
  } finally {
    if (agentEl && rawAnswer) {
      const body = agentEl.querySelector('.prose-content');
      if (body) {
        body.innerHTML = marked.parse(rawAnswer);
        const cursor = agentEl.querySelector('.stream-cursor');
        if (cursor) cursor.remove();
      }
    }
    setStatusMsg(statusEl, '');
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    running = false;
    submitBtn.disabled = false;
    queryInput.focus();
    scrollToBottom();
  }
}

/* ═══════════════════════════════════════════════════════════════════
   RENDER HELPERS — chat messages
═══════════════════════════════════════════════════════════════════ */

function appendUserBubble(text) {
  const row = document.createElement('div');
  row.className = 'msg-user';
  row.innerHTML = `<div class="msg-user-bubble">${esc(text)}</div>`;
  chatColumn.appendChild(row);
  scrollToBottom();
}

function appendStagesCard() {
  const alwaysOpen = _stagesAlways();
  const wrap = document.createElement('div');
  wrap.className = 'msg-agent';
  if (!alwaysOpen) wrap.style.display = 'none';
  wrap.innerHTML =
    `<div class="ai-stages-card">` +
    `<div class="ai-stages-header">` +
      `<span>שלבי עיבוד</span>` +
      `<span class="ai-stages-toggle-arrow">▶</span>` +
    `</div>` +
    `</div>`;
  chatColumn.appendChild(wrap);
  // Don't scrollToBottom — hidden card shouldn't affect scroll
  const card = wrap.querySelector('.ai-stages-card');
  card.querySelector('.ai-stages-header').addEventListener('click', () => {
    card.classList.toggle('collapsed');
  });
  return card;
}

function _wireStatusToggle(statusEl, stagesEl) {
  if (_stagesAlways()) return;
  const wrap = stagesEl.parentElement;
  statusEl.classList.add('clickable');
  statusEl.title = 'לחץ לצפייה בשלבי עיבוד';
  statusEl.addEventListener('click', () => {
    const hidden = wrap.style.display === 'none';
    wrap.style.display = hidden ? 'block' : 'none';
    if (hidden) scrollToBottom();
  });
}

function appendStatus(msg) {
  const el = document.createElement('div');
  el.className = 'msg-status';
  if (msg) setStatusMsg(el, msg);
  chatColumn.appendChild(el);
  return el;
}

function setStatusMsg(el, msg) {
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

function appendAgentCard() {
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

function appendErrorMsg(msg) {
  const el = document.createElement('div');
  el.className = 'msg-error';
  el.textContent = msg;
  chatColumn.appendChild(el);
  scrollToBottom();
}

function scrollToBottom() {
  chatColumn.scrollTop = chatColumn.scrollHeight;
}

/* ═══════════════════════════════════════════════════════════════════
   AI STAGES CARD — live and completed tiles
═══════════════════════════════════════════════════════════════════ */

// The live card is the last child of the stages card (while streaming thinking)
function addLiveStageCard(stagesEl, nodeStart) {
  finaliseLiveCard(stagesEl); // clear any stale live card

  const label      = nodeStart.label      || 'שלב';
  const stage      = nodeStart.stage      || 'unknown';
  const loop       = nodeStart.loop       || 0;
  const openPrompt = nodeStart.openPrompt || false;
  const loopHtml = loop > 0 ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>` : '';

  const card = document.createElement('div');
  card.className = 'stage-card live-stage-card';
  card.innerHTML =
    `<div class="stage-header open" onclick="toggleStageCard(this)">` +
      `<span class="stage-arrow">▶</span>` +
      `<span class="stage-dot ${esc(stage)}"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-meta"><span class="live-thinking-dot"></span>${loopHtml}</span>` +
    `</div>` +
    `<div class="stage-body visible">` +
      renderPromptHtml(nodeStart.prompt || {}, openPrompt) +
      `<details class="sub-details open">` +
        `<summary class="sub-summary thinking-summary">תהליך עבודה…</summary>` +
        `<div class="sub-details-body"><pre class="prompt-text thinking-text"></pre></div>` +
      `</details>` +
    `</div>`;

  stagesEl.appendChild(card);
  scrollToBottom();
}

function appendLiveThinking(stagesEl, text) {
  const live = stagesEl.querySelector('.live-stage-card');
  if (!live) return;
  const pre = live.querySelector('.thinking-text');
  if (!pre) return;
  pre.textContent += text;
  pre.scrollTop = pre.scrollHeight;
}

function finaliseLiveCard(stagesEl) {
  const live = stagesEl && stagesEl.querySelector('.live-stage-card');
  if (live) live.remove();
}

function addCompletedStageCard(stagesEl, data) {
  const label       = data.label        || 'שלב';
  const stage       = data.stage        || 'unknown';
  const content     = data.content      || '';
  const loop        = data.loop         || 0;
  const elapsedMs   = data.elapsed_ms   != null ? data.elapsed_ms : null;
  const llmMs       = data.llm_ms       || 0;
  const toolMs      = data.tool_ms      || 0;
  const thinking    = data.thinking     || '';
  const tools       = data.tools        || [];
  const retrieval   = data.retrieval    || null;
  const prompt      = data.prompt       || null;
  const toolResults = data.tool_results || [];

  const loopHtml = loop > 0 ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>` : '';

  let timeHtml = '';
  if (elapsedMs != null) {
    const totalStr = (elapsedMs / 1000).toFixed(1) + 's';
    if (llmMs > 0 || toolMs > 0) {
      const llmStr  = (llmMs  / 1000).toFixed(1) + 's';
      const toolStr = (toolMs / 1000).toFixed(1) + 's';
      timeHtml = `<span class="stage-time" title="LLM: ${llmStr} | כלים: ${toolStr} | סה״כ: ${totalStr}">${totalStr}</span>`;
    } else {
      timeHtml = `<span class="stage-time">${totalStr}</span>`;
    }
  }

  const uniqueTools = [...new Set(tools)];
  const toolsHtml   = uniqueTools.length > 0
    ? `<span class="stage-tools-badge">${uniqueTools.join(' · ')}</span>`
    : '';

  const promptHtml      = prompt     ? renderPromptHtml(prompt)       : '';
  const thinkingHtml    = thinking   ? renderThinkingHtml(thinking, llmMs) : '';
  const toolResultsHtml = toolResults.map(tr => renderToolResultHtml(tr)).join('');
  const retrievalHtml   = retrieval  ? renderRetrievalHtml(retrieval) : '';

  const card = document.createElement('div');
  card.className = 'stage-card';
  card.innerHTML =
    `<div class="stage-header" onclick="toggleStageCard(this)">` +
      `<span class="stage-arrow">▶</span>` +
      `<span class="stage-dot ${esc(stage)}"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-meta">${timeHtml}${toolsHtml}${loopHtml}</span>` +
    `</div>` +
    `<div class="stage-body">` +
      promptHtml + thinkingHtml + toolResultsHtml + retrievalHtml +
      (content ? `<div class="prose-content" style="margin-top:8px">${marked.parse(content)}</div>` : '') +
    `</div>`;

  stagesEl.appendChild(card);
  scrollToBottom();
}

/* ═══════════════════════════════════════════════════════════════════
   SUBGRAPH (RESEARCH AGENT) RENDERING HELPERS
═══════════════════════════════════════════════════════════════════ */

function _subgraphPhaseLabel(phase) {
  const labels = {
    'planner':          'מתכנן שלבי חקר',
    'planner_replan':   'מתכנן מחדש',
    'critic_pre':       'ביקורת תוכנית',
    'validator':        'אימות תוכנית',
    'critic_post':      'ביקורת תוצאות',
    'synthesizer':      'מסכם ממצאים',
  };
  if (phase && phase.startsWith('executor:')) {
    const parts = phase.split(':');
    const stepId = parts[1] || '';
    return `ביצוע ${stepId}`;
  }
  return labels[phase] || phase || 'שלב';
}

function addSubgraphWrapperCard(stagesEl, nodeStart) {
  finaliseLiveCard(stagesEl);
  const label    = nodeStart.label || 'מחקר מעמיק';
  const loop     = nodeStart.loop  || 0;
  const loopHtml = loop > 0 ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>` : '';

  const card = document.createElement('div');
  card.className = 'stage-card subgraph-card live-stage-card';
  card.dataset.startTs = String(Date.now());
  card.innerHTML =
    `<div class="stage-header open" onclick="toggleStageCard(this)">` +
      `<span class="stage-arrow">▶</span>` +
      `<span class="stage-dot research"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-meta"><span class="live-thinking-dot"></span>${loopHtml}</span>` +
    `</div>` +
    `<div class="stage-body visible">` +
      `<div class="subgraph-inner-stages"></div>` +
    `</div>`;

  stagesEl.appendChild(card);
  scrollToBottom();
  return card.querySelector('.subgraph-inner-stages');
}

function finaliseSubgraphCard(subgraphContainer) {
  if (!subgraphContainer) return;
  const card = subgraphContainer.closest('.subgraph-card');
  if (!card) return;
  card.classList.remove('live-stage-card');
  const dot = card.querySelector('.live-thinking-dot');
  if (dot) dot.remove();
  const header = card.querySelector('.stage-header');
  if (header) header.classList.remove('open');

  const startTs = parseInt(card.dataset.startTs || '0', 10);
  if (startTs) {
    const elapsedMs = Date.now() - startTs;
    const metaEl = card.querySelector('.stage-meta');
    if (metaEl) {
      const timeSpan = document.createElement('span');
      timeSpan.className = 'stage-time';
      timeSpan.textContent = (elapsedMs / 1000).toFixed(1) + 's';
      metaEl.insertBefore(timeSpan, metaEl.firstChild);
    }
  }
}

function appendLiveOutput(stagesEl, text) {
  const live = stagesEl && stagesEl.querySelector('.live-stage-card');
  if (!live) return;
  let pre = live.querySelector('.live-output-text');
  if (!pre) {
    const body = live.querySelector('.stage-body');
    if (!body) return;
    const det = document.createElement('details');
    det.className = 'sub-details open';
    det.innerHTML =
      `<summary class="sub-summary">פלט…</summary>` +
      `<div class="sub-details-body"><pre class="prompt-text live-output-text"></pre></div>`;
    body.appendChild(det);
    pre = live.querySelector('.live-output-text');
  }
  if (pre) {
    pre.textContent += text;
    pre.scrollTop = pre.scrollHeight;
  }
}

/* ═══════════════════════════════════════════════════════════════════
   USER INPUT PANELS
═══════════════════════════════════════════════════════════════════ */

function renderUserInputPanel(data) {
  const ui        = data.ui        || 'text_input';
  const outputVar = data.output_var || 'user_input';

  if (ui === 'option_select') {
    renderOptionSelect(data, outputVar);
  } else if (ui === 'text_input') {
    renderTextInput(data, outputVar);
  } else if (ui === 'deep_dive') {
    const meetings = data.meetings || [];
    openProtocolBrowser(
      sessionId,
      meetings[0]?.meeting_id || null,
      meetings,
      {
        originalQuestion: data.original_question || data.query || _lastQuestion || '',
        postCompletion: false,
      }
    );
  }
}

function renderOptionSelect(data, outputVar) {
  const prompt  = data.prompt_he || data.prompt || 'בחר אפשרות:';
  const options = data.options   || [];
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

    // Build button content: label + optional description + optional subtitle
    let inner = `<span class="option-label">${esc(label)}</span>`;
    if (desc) inner += `<span class="option-desc">${esc(desc)}</span>`;
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
    // Disable the whole panel
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    submitResponse(outputVar, val);
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

  const submitEl = document.createElement('button');
  submitEl.className = 'text-input-submit';
  submitEl.textContent = 'שלח';
  submitEl.addEventListener('click', () => {
    const val = textarea.value.trim();
    if (!val) return;
    textarea.disabled = true;
    submitEl.disabled = true;
    submitResponse(outputVar, val);
  });
  card.appendChild(submitEl);

  wrap.appendChild(card);
  chatColumn.appendChild(wrap);
  textarea.focus();
  scrollToBottom();
}

/* ═══════════════════════════════════════════════════════════════════
   STAGE CARD DETAIL RENDERERS
═══════════════════════════════════════════════════════════════════ */

function toggleStageCard(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('visible');
}

function renderPromptHtml(p, open = false) {
  const sys  = p.system || '';
  const user = p.user   || '';
  const openAttr = open ? ' open' : '';
  return (
    `<details class="sub-details"${openAttr}>` +
    `<summary class="sub-summary">פרומפט</summary>` +
    `<div class="sub-details-body">` +
    (sys ? `<div class="prompt-block"><div class="prompt-role">מערכת</div><pre class="prompt-text">${esc(sys)}</pre></div>` : '') +
    `<div class="prompt-block"><div class="prompt-role">משתמש</div><pre class="prompt-text">${esc(user)}</pre></div>` +
    `</div></details>`
  );
}

function renderThinkingHtml(thinking, llmMs) {
  const timeStr = llmMs > 0 ? ` (${(llmMs / 1000).toFixed(1)}s)` : '';
  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary thinking-summary">מחשבות${esc(timeStr)}</summary>` +
    `<div class="sub-details-body"><pre class="prompt-text thinking-text">${esc(thinking)}</pre></div>` +
    `</details>`
  );
}

function renderToolResultHtml(tr) {
  const name      = tr.name       || '';
  const args      = tr.args       || {};
  const elapsedMs = tr.elapsed_ms != null ? tr.elapsed_ms : null;
  const argsStr   = JSON.stringify(args, null, 2);
  const timeStr   = elapsedMs != null
    ? ` <span class="tool-time">${(elapsedMs / 1000).toFixed(1)}s</span>`
    : '';
  const hasArgs = Object.keys(args).length > 0;
  const argsHtml = hasArgs
    ? `<div class="prompt-block"><div class="prompt-role">ארגומנטים</div><pre class="prompt-text">${esc(argsStr)}</pre></div>`
    : '';

  if (tr.result_ref) {
    // Lazy variant — full text fetched on expand
    return (
      `<details class="sub-details tool-result-lazy"` +
      ` data-result-ref="${esc(tr.result_ref)}" data-session-id="${esc(sessionId || '')}" data-loaded="0">` +
      `<summary class="sub-summary"><span class="tool-summary-label">${esc(name)}</span>${timeStr}</summary>` +
      `<div class="sub-details-body">` +
      argsHtml +
      `<div class="tool-result-slot"><div class="tool-result-placeholder">▼ לחץ להצגת תוצאה</div></div>` +
      `</div></details>`
    );
  }

  // Inline variant (backward compat)
  const result = tr.result || '';
  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary"><span class="tool-summary-label">${esc(name)}</span>${timeStr}</summary>` +
    `<div class="sub-details-body">` +
    argsHtml +
    `<div class="prompt-block"><div class="prompt-role">תוצאה</div><pre class="prompt-text">${esc(result)}</pre></div>` +
    `</div></details>`
  );
}

/* ── Lazy tool result loading ───────────────────────────────── */

function _isToolPanelVisible(el) {
  if (!el.open) return false;
  let node = el.parentElement;
  while (node) {
    if (node.tagName === 'DETAILS' && !node.open) return false;
    node = node.parentElement;
  }
  return true;
}

async function _loadToolResult(el) {
  if (el.dataset.loaded === '1' || el.dataset.loading === '1') return;
  el.dataset.loading = '1';
  const slot = el.querySelector('.tool-result-slot');
  if (slot) slot.innerHTML = '<div class="tool-result-loading">טוען...</div>';
  const ref = el.dataset.resultRef;
  const sid = el.dataset.sessionId;
  try {
    const resp = await fetch(`/api/research/${sid}/tool_result/${ref}`);
    const json = await resp.json();
    const text = json.full || '';
    if (slot) slot.innerHTML =
      `<div class="prompt-block"><div class="prompt-role">תוצאה</div><pre class="prompt-text">${esc(text)}</pre></div>`;
    el.dataset.loaded  = '1';
    el.dataset.loading = '0';
    _updateToolResultTimer(el);
  } catch (err) {
    if (slot) slot.innerHTML = `<div class="tool-result-error">שגיאה בטעינה: ${esc(String(err))}</div>`;
    el.dataset.loading = '0';
  }
}

function _unloadToolResult(el) {
  clearTimeout(el._unloadTimer);
  el._unloadTimer = null;
  el.dataset.loaded = '0';
  const slot = el.querySelector('.tool-result-slot');
  if (slot) slot.innerHTML = '<div class="tool-result-placeholder">▼ לחץ להצגת תוצאה</div>';
}

function _updateToolResultTimer(el) {
  if (el.dataset.loaded !== '1') return;
  if (_isToolPanelVisible(el)) {
    clearTimeout(el._unloadTimer);
    el._unloadTimer = null;
  } else {
    if (!el._unloadTimer) {
      el._unloadTimer = setTimeout(() => _unloadToolResult(el), TOOL_RESULT_UNLOAD_MS);
    }
  }
}

// Global toggle handler — handles both self-open (trigger load) and
// any ancestor toggle (re-evaluate timer for all loaded lazy panels).
document.addEventListener('toggle', function(e) {
  const toggled = e.target;

  // If a lazy panel opened and not yet loaded, fetch the result.
  if (toggled.classList && toggled.classList.contains('tool-result-lazy') && toggled.open) {
    _loadToolResult(toggled);
  }

  // Re-evaluate timer for every loaded lazy panel in the document.
  document.querySelectorAll('.tool-result-lazy[data-loaded="1"]').forEach(_updateToolResultTimer);
}, true); // capture phase — toggle doesn't bubble

function renderRetrievalHtml(r) {
  const meetings = r.meetings      || [];
  const chunks   = r.chunks        || [];
  const chars    = r.context_chars || 0;
  const tokEst   = Math.round(chars / 2);
  const ragMs    = r.rag_ms        || 0;
  const ragTime  = ragMs > 0 ? ` · ${(ragMs / 1000).toFixed(1)}s` : '';

  const label = `פרטי אחזור — ${meetings.length} ישיבות · ${chunks.length} קטעים · ~${tokEst.toLocaleString()} טוקנים${ragTime}`;

  let body = '<div class="stats-row">';
  body += `ישיבות: <strong>${meetings.length}</strong>`;
  body += ` &nbsp;|&nbsp; קטעים: <strong>${chunks.length}</strong>`;
  body += ` &nbsp;|&nbsp; הקשר: <strong>${chars.toLocaleString()}</strong> תווים (~${tokEst.toLocaleString()} טוקנים)`;
  body += '</div>';

  if (meetings.length) {
    body += `<div style="color:#666;font-size:.74rem;margin-bottom:6px;direction:rtl">${meetings.map(esc).join(' · ')}</div>`;
  }

  if (chunks.length) {
    body += '<ul class="chunk-list">';
    for (const c of chunks) {
      body += `<li class="chunk-item">` +
        `<span class="chunk-date">${esc(c.date)}</span>` +
        `<span class="chunk-topic">${esc(c.topic)}</span>` +
        `<span class="chunk-sim">sim&nbsp;${c.p1_sim}</span>` +
        `</li>`;
    }
    body += '</ul>';
  }

  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary">${label}</summary>` +
    `<div class="sub-details-body">${body}</div>` +
    `</details>`
  );
}

/* ── Utility ───────────────────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
