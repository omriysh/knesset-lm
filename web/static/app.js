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
let running             = false;
let sessionId           = null;   // current or last session id
let _lastQuestion       = '';     // most recent user question (for explore-sources)
let _reconnectSessionId = null;   // set on stream start, cleared on clean done/error
let _reconnecting       = false;  // true while _attemptReconnect is looping
let _reconnectErrorEl   = null;   // the red error card shown on disconnect (removed on reconnect)
let _currentStagesEl    = null;   // .ai-stages-card of the active session (for reconnect cleanup)

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
  _currentStagesEl = stagesEl;
  _wireStatusToggle(statusEl, stagesEl);
  let   agentEl           = null;
  let   rawAnswer         = '';
  let   subgraphContainer = null;
  let   subgraphPhase     = null;
  let   pendingFootnotes  = [];
  let   pendingCitations  = [];
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
            get agentEl()            { return agentEl; },            set agentEl(v)            { agentEl = v; },
            get rawAnswer()          { return rawAnswer; },          set rawAnswer(v)          { rawAnswer = v; },
            get subgraphContainer()  { return subgraphContainer; },  set subgraphContainer(v)  { subgraphContainer = v; },
            get subgraphPhase()      { return subgraphPhase; },      set subgraphPhase(v)      { subgraphPhase = v; },
            get pendingFootnotes()   { return pendingFootnotes; },   set pendingFootnotes(v)   { pendingFootnotes = v; },
            get pendingCitations()   { return pendingCitations; },   set pendingCitations(v)   { pendingCitations = v; },
          });
        }
      }
    }
  } catch (err) {
    setStatusMsg(statusEl, '');
    _reconnectErrorEl = appendErrorMsg('שגיאת חיבור: ' + err.message);
    if (_reconnectSessionId && document.visibilityState === 'visible') {
      setTimeout(_attemptReconnect, 1500);
    }
  } finally {
    const _willReconnect = !!_reconnectSessionId;
    if (agentEl && rawAnswer && !_willReconnect) {
      const body = agentEl.querySelector('.prose-content');
      if (body) {
        body.innerHTML = marked.parse(rawAnswer);
        const cursor = agentEl.querySelector('.stream-cursor');
        if (cursor) cursor.remove();
        if (pendingFootnotes.length > 0) {
          _applyEvidenceCitations(body, pendingFootnotes, pendingCitations);
          agentEl.insertAdjacentHTML('beforeend', _buildSourcesHtml(pendingFootnotes, sessionId));
        }
      }
    } else if (agentEl && _willReconnect) {
      agentEl.remove();
    }
    setStatusMsg(statusEl, '');
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    if (!_willReconnect) {
      running = false;
      submitBtn.disabled = false;
      queryInput.focus();
    }
    scrollToBottom();
  }
}

function handleEvent(ev, data, refs) {
  switch (ev) {
    case 'session_id':
      sessionId = data.session_id;
      _reconnectSessionId = data.session_id;
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
        const footnotes = data.subgraph?.outputs?.footnotes;
        if (Array.isArray(footnotes) && footnotes.length > 0) {
          refs.pendingFootnotes = footnotes;
        }
        const citations = data.subgraph?.outputs?.citations;
        if (Array.isArray(citations)) {
          refs.pendingCitations = citations;
        }
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
      const isSynthesizerPhase = sg_name.startsWith('synthesizer');
      const isSynthesizerExpandPhase = sg_name === 'synthesizer:expand';

      if (sg_kind === 'llm_start') {
        if (isExecutorPhase) {
          // All turns of the same step share one phase slot; don't create a card.
          if (!refs.subgraphPhase || !refs.subgraphPhase._isExecutor) {
            refs.subgraphPhase = { _isExecutor: true, thinking: '', content: '', prompt: sg_payload.prompt || {} };
          }
        } else if (isSynthesizerPhase) {
          // All turns of the synthesizer share one phase slot.
          if (!refs.subgraphPhase || !refs.subgraphPhase._isSynthesizer) {
            refs.subgraphPhase = {
              _isSynthesizer: true,
              label:       _subgraphPhaseLabel('synthesizer'),
              stage:       'research',
              thinking:    '',
              content:     '',
              prompt:      sg_payload.prompt || {},
            };
          }
          // Create live card only for the synthesis turn (not expand turns).
          if (!isSynthesizerExpandPhase && refs.subgraphContainer && !refs.subgraphPhase._liveCardCreated) {
            refs.subgraphPhase._liveCardCreated = true;
            addLiveStageCard(refs.subgraphContainer, {
              label:      refs.subgraphPhase.label,
              stage:      refs.subgraphPhase.stage,
              loop:       0,
              prompt:     refs.subgraphPhase.prompt,
              openPrompt: true,
            });
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
        // Show thinking in the live card only for non-executor, non-expand phases
        if (!isExecutorPhase && !isSynthesizerExpandPhase && refs.subgraphContainer) {
          appendLiveThinking(refs.subgraphContainer, sg_payload.text || '');
        }

      } else if (sg_kind === 'llm_token') {
        if (refs.subgraphPhase) refs.subgraphPhase.content += sg_payload.text || '';
        if (!isExecutorPhase && !isSynthesizerExpandPhase && refs.subgraphContainer) {
          appendLiveOutput(refs.subgraphContainer, sg_payload.text || '');
        }

      } else if (sg_kind === 'llm_done') {
        if (isExecutorPhase || isSynthesizerPhase) {
          // Accumulate; card is finalized by step_completed / synthesizer_completed
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

      } else if (sg_kind === 'hook' && sg_name === 'synthesizer_completed') {
        const ph = refs.subgraphPhase;
        if (ph && ph._isSynthesizer && refs.subgraphContainer) {
          finaliseLiveCard(refs.subgraphContainer);
          addCompletedStageCard(refs.subgraphContainer, {
            label:        ph.label || _subgraphPhaseLabel('synthesizer'),
            stage:        ph.stage || 'research',
            content:      ph.content || '',
            thinking:     ph.thinking || '',
            tools:        [],
            tool_results: [],
            prompt:       ph.prompt || {},
          });
        }
        refs.subgraphPhase = null;

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
      _reconnectSessionId = null;
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
      _reconnectSessionId = null;
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
  _currentStagesEl = stagesEl;
  _wireStatusToggle(statusEl, stagesEl);
  let   agentEl           = null;
  let   rawAnswer         = '';
  let   subgraphContainer = null;
  let   subgraphPhase     = null;
  let   pendingFootnotes  = [];
  let   pendingCitations  = [];
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
            get agentEl()            { return agentEl; },            set agentEl(v)            { agentEl = v; },
            get rawAnswer()          { return rawAnswer; },          set rawAnswer(v)          { rawAnswer = v; },
            get subgraphContainer()  { return subgraphContainer; },  set subgraphContainer(v)  { subgraphContainer = v; },
            get subgraphPhase()      { return subgraphPhase; },      set subgraphPhase(v)      { subgraphPhase = v; },
            get pendingFootnotes()   { return pendingFootnotes; },   set pendingFootnotes(v)   { pendingFootnotes = v; },
            get pendingCitations()   { return pendingCitations; },   set pendingCitations(v)   { pendingCitations = v; },
          });
        }
      }
    }
  } catch (err) {
    setStatusMsg(statusEl, '');
    _reconnectErrorEl = appendErrorMsg('שגיאת חיבור: ' + err.message);
    if (_reconnectSessionId && document.visibilityState === 'visible') {
      setTimeout(_attemptReconnect, 1500);
    }
  } finally {
    const _willReconnect = !!_reconnectSessionId;
    if (agentEl && rawAnswer && !_willReconnect) {
      const body = agentEl.querySelector('.prose-content');
      if (body) {
        body.innerHTML = marked.parse(rawAnswer);
        const cursor = agentEl.querySelector('.stream-cursor');
        if (cursor) cursor.remove();
        if (pendingFootnotes.length > 0) {
          _applyEvidenceCitations(body, pendingFootnotes, pendingCitations);
          agentEl.insertAdjacentHTML('beforeend', _buildSourcesHtml(pendingFootnotes, sessionId));
        }
      }
    } else if (agentEl && _willReconnect) {
      agentEl.remove();
    }
    setStatusMsg(statusEl, '');
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    if (!_willReconnect) {
      running = false;
      submitBtn.disabled = false;
      queryInput.focus();
    }
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
  return el;
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

  if (toggled.classList && toggled.classList.contains('tool-result-lazy') && toggled.open) {
    _loadToolResult(toggled);
  }
  if (toggled.classList && toggled.classList.contains('ev-source-lazy') && toggled.open) {
    _loadEvidenceFull(toggled);
  }

  // Re-evaluate unload timer for every loaded tool-result panel.
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

/* ── Evidence citations ─────────────────────────────────────── */

// Shared popup element — created once, reused.
let _evPopup = null;
function _getEvPopup() {
  if (!_evPopup) {
    _evPopup = document.createElement('div');
    _evPopup.className = 'ev-citation-popup';
    _evPopup.hidden = true;
    document.body.appendChild(_evPopup);
    document.addEventListener('click', () => { _evPopup.hidden = true; });
  }
  return _evPopup;
}

// Fields to skip when rendering a quote object generically.
const _QUOTE_SKIP = new Set([
  'bullet_id', 'bullet_idx', 'id', 'knesset', 'knesset_num',
  'mk_individual_id', 'committee_id', 'faction_id', 'position_id',
]);

function _renderQuoteObj(obj) {
  if (Array.isArray(obj)) {
    return obj.map(_renderQuoteObj).join('<hr class="ev-quote-sep">');
  }
  if (typeof obj !== 'object' || obj === null) {
    return `<div class="ev-citation-quote">${esc(String(obj))}</div>`;
  }
  // Empty result: show the query that returned nothing
  if (obj._no_results) {
    const q = obj.query || obj.topic || obj.mk_query || obj.speaker || '';
    const label = q ? ` עבור "${esc(q)}"` : '';
    return `<div class="ev-citation-empty">לא נמצאו תוצאות${label}</div>`;
  }
  // Meeting-like: has meeting_id or committee → structured header + text
  if (obj.meeting_id != null || obj.committee != null) {
    const parts = [];
    if (obj.committee) parts.push(esc(String(obj.committee)));
    if (obj.date)      parts.push(esc(String(obj.date)));
    if (obj.speaker)   parts.push(esc(String(obj.speaker)));
    const header = parts.length
      ? `<div class="ev-citation-meeting-header">${parts.join(' &middot; ')}</div>`
      : '';
    const text = obj.text || obj.label || obj.summary || obj.full_text || '';
    const textHtml = text ? `<div class="ev-citation-quote">${esc(String(text))}</div>` : '';
    return header + textHtml;
  }
  // Generic: render visible key-value pairs
  const rows = Object.entries(obj)
    .filter(([k, v]) => !_QUOTE_SKIP.has(k) && v != null && v !== '')
    .map(([k, v]) => {
      const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return `<div class="ev-citation-kv">` +
        `<span class="ev-kv-key">${esc(k)}</span>` +
        `<span class="ev-kv-val">${esc(val)}</span></div>`;
    });
  return rows.length
    ? `<div class="ev-citation-kvlist">${rows.join('')}</div>`
    : `<div class="ev-citation-quote">${esc(JSON.stringify(obj))}</div>`;
}

function _showCitationPopup(supEl, quoteRaw, uiMeta) {
  const popup = _getEvPopup();

  // Parse quote — may be a JSON object/array or a plain string.
  let quoteObj = null;
  if (typeof quoteRaw === 'object' && quoteRaw !== null) {
    quoteObj = quoteRaw;
  } else if (typeof quoteRaw === 'string') {
    const t = quoteRaw.trim();
    if (t.startsWith('{') || t.startsWith('[')) {
      try { quoteObj = JSON.parse(t); } catch (_) {}
    }
  }

  const contentHtml = quoteObj != null
    ? _renderQuoteObj(quoteObj)
    : `<div class="ev-citation-quote">${esc(quoteRaw || '')}</div>`;

  const metaNote = (uiMeta && uiMeta.meta_note) ? uiMeta.meta_note : (uiMeta && uiMeta.tool_name) || '';
  popup.innerHTML = contentHtml +
    (metaNote ? `<div class="ev-citation-popup-source">${esc(metaNote)}</div>` : '');

  popup.hidden = false;
  const sr = supEl.getBoundingClientRect();
  const pr = popup.getBoundingClientRect();
  const GAP = 8;
  let left = sr.left + sr.width / 2 - pr.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pr.width - 8));
  // Show above if room, otherwise show below.
  const topAbove = sr.top + window.scrollY - pr.height - GAP;
  const top = (sr.top - pr.height - GAP >= 0) ? topAbove : sr.bottom + window.scrollY + GAP;
  const tailLeft = (sr.left + sr.width / 2) - left;
  popup.style.left = left + 'px';
  popup.style.top  = top + 'px';
  popup.style.setProperty('--tail-left', tailLeft + 'px');
}

function _applyEvidenceCitations(bodyEl, footnotes, citations) {
  // Build lookup: citation n → {ev_id, quote}
  const citMap = {};
  (citations || []).forEach(c => { if (c && c.n != null) citMap[c.n] = c; });

  // Build lookup: ev_id → footnote index (1-based) for display number
  const evIdToIdx = {};
  footnotes.forEach((fn, i) => { evIdToIdx[fn.id] = i + 1; });

  const hasCitations = Object.keys(citMap).length > 0;

  if (hasCitations) {
    bodyEl.innerHTML = bodyEl.innerHTML.replace(/\[(\d+)\]/g, (match, numStr) => {
      const n   = parseInt(numStr, 10);
      const cit = citMap[n];
      if (!cit) return match;
      const displayN = evIdToIdx[cit.ev_id] || n;
      // Serialize quote to string for data attribute (may be object or string).
      const quoteStr = (typeof cit.quote === 'object' && cit.quote !== null)
        ? JSON.stringify(cit.quote)
        : (cit.quote || '');
      return (
        `<sup class="ev-cite" data-cite-n="${n}" ` +
        `data-ev-id="${esc(cit.ev_id)}" ` +
        `data-quote="${esc(quoteStr)}" ` +
        `title="${esc(cit.ev_id)}">[${displayN}]</sup>`
      );
    });
  } else {
    // Fallback: old [ev_xxx] format
    bodyEl.innerHTML = bodyEl.innerHTML.replace(/\[ev_([0-9a-f]+)\]/g, (match, hex) => {
      const evId = 'ev_' + hex;
      const n = evIdToIdx[evId];
      if (!n) return match;
      return `<sup class="ev-cite" data-ev-id="${esc(evId)}" title="${esc(evId)}">[${n}]</sup>`;
    });
  }

  // Attach click handlers
  bodyEl.querySelectorAll('sup.ev-cite').forEach(sup => {
    sup.addEventListener('click', e => {
      e.stopPropagation();
      const quoteRaw = sup.dataset.quote || '';
      const evId     = sup.dataset.evId  || '';
      const fn       = footnotes.find(f => f.id === evId);
      // For expand entries, resolve to the original evidence entry for display metadata.
      let resolvedFn = fn;
      if (fn && fn.tool_name === 'expand') {
        const origId = (fn.metadata && fn.metadata.evidence_id) || (fn.provenance && fn.provenance.evidence_id);
        if (origId) {
          const origFn = footnotes.find(f => f.id === origId);
          if (origFn) resolvedFn = origFn;
        }
      }
      const uiMeta   = resolvedFn ? (resolvedFn.ui || {tool_name: resolvedFn.tool_name}) : {};
      if (quoteRaw) {
        _showCitationPopup(sup, quoteRaw, uiMeta);
      }
    });
  });
}

function _buildSourcesHtml(footnotes, sid) {
  const entries = footnotes.map((fn, i) => {
    const n        = i + 1;
    const toolName = fn.tool_name  || '';
    const stepId   = fn.step_id    || '';
    const summary  = fn.summary    || '';
    const ref      = fn.result_ref || '';
    const header   = (
      `<span class="ev-source-num">[${n}]</span>` +
      `<span class="ev-source-tool">${esc(toolName)}</span>` +
      `<span class="ev-source-step">${esc(stepId)}</span>` +
      `<span class="ev-source-summary">${esc(summary)}</span>`
    );
    if (ref) {
      return (
        `<details class="ev-source-entry ev-source-lazy"` +
        ` data-result-ref="${esc(ref)}" data-session-id="${esc(sid || '')}"` +
        ` data-tool-name="${esc(toolName)}" data-loaded="0">` +
        `<summary class="ev-source-header">${header}</summary>` +
        `<div class="ev-source-full-slot"><div class="ev-source-placeholder">▼ לחץ להצגת מקור מלא</div></div>` +
        `</details>`
      );
    }
    return `<div class="ev-source-entry"><div class="ev-source-header">${header}</div></div>`;
  }).join('');
  return (
    `<details class="ev-sources">` +
    `<summary class="ev-sources-summary">מקורות (${footnotes.length})</summary>` +
    `<div class="ev-sources-body">${entries}</div>` +
    `</details>`
  );
}

async function _loadEvidenceFull(el) {
  if (el.dataset.loaded === '1' || el.dataset.loading === '1') return;
  el.dataset.loading = '1';
  const slot = el.querySelector('.ev-source-full-slot');
  if (slot) slot.innerHTML = '<div class="ev-source-loading">טוען…</div>';
  const ref  = el.dataset.resultRef;
  const sid  = el.dataset.sessionId;
  const tool = el.dataset.toolName || '';
  try {
    const resp = await fetch(`/api/research/${sid}/tool_result/${ref}`);
    const json = await resp.json();
    if (slot) slot.innerHTML = _renderEvidenceFull(json.full || '', tool);
    el.dataset.loaded  = '1';
    el.dataset.loading = '0';
  } catch (err) {
    if (slot) slot.innerHTML = `<div class="ev-source-error">שגיאה: ${esc(String(err))}</div>`;
    el.dataset.loading = '0';
  }
}

function _renderEvidenceFull(text, toolName) {
  if (!text) return '<div class="ev-source-empty">אין תוכן</div>';
  let data;
  try { data = JSON.parse(text); } catch { return `<div class="ev-card-text">${esc(text)}</div>`; }
  if (Array.isArray(data)) {
    if (data.length === 0) return '<div class="ev-source-empty">אין תוצאות</div>';
    const real      = data.filter(x => !(x && x._truncated));
    const truncItem = data.find(x => x && x._truncated);
    const cards     = real.map(item => _renderEvidenceCard(item)).join('');
    const notice    = truncItem
      ? `<div class="ev-truncated-notice">עוד ${truncItem.items_removed} פריטים לא הוצגו</div>`
      : '';
    return `<div class="ev-full-cards">${cards}${notice}</div>`;
  }
  if (typeof data === 'object' && data !== null) return _renderEvidenceCard(data);
  return `<pre class="ev-full-json">${esc(JSON.stringify(data, null, 2))}</pre>`;
}

function _renderEvidenceCard(item) {
  if (typeof item !== 'object' || item === null) {
    return `<div class="ev-full-card"><pre class="ev-card-rest">${esc(String(item))}</pre></div>`;
  }
  const LABELS  = ['label', 'committee_name', 'committee', 'name', 'title', 'mk_name'];
  const METAS   = ['meeting_id', 'session_id', 'date', 'knesset_num', 'score', 'relevance_score', 'bullet_idx'];
  const TEXTS   = ['text', 'text_he', 'body', 'content', 'summary'];
  const LISTS   = ['bullets', 'speeches'];
  const SKIP    = new Set(['bullet_id', 'id', '_truncated', 'items_removed', 'source_url', 'result_ref']);
  const seen    = new Set(Object.keys(item).filter(k => SKIP.has(k)));

  let labelHtml = '';
  for (const f of LABELS) {
    if (item[f]) { labelHtml = `<span class="ev-card-label">${esc(String(item[f]))}</span>`; seen.add(f); break; }
  }
  let metaBadges = '';
  for (const f of METAS) {
    if (item[f] != null) {
      const v = (f === 'score' || f === 'relevance_score') ? Number(item[f]).toFixed(3) : String(item[f]);
      metaBadges += `<span class="ev-card-meta-badge">${esc(f.replace(/_/g, ' '))}: ${esc(v)}</span>`;
      seen.add(f);
    }
  }
  let bodyHtml = '';
  for (const f of TEXTS) {
    if (item[f] && !seen.has(f)) {
      const t = String(item[f]);
      bodyHtml += `<div class="ev-card-text">${esc(t.length > 600 ? t.slice(0, 600) + '…' : t)}</div>`;
      seen.add(f); break;
    }
  }
  for (const f of LISTS) {
    if (Array.isArray(item[f]) && item[f].length > 0 && !seen.has(f)) {
      const items = item[f].slice(0, 5);
      const more  = item[f].length - items.length;
      bodyHtml += `<div class="ev-card-bullets">` +
        items.map(b => `<div class="ev-card-bullet">• ${esc(typeof b === 'string' ? b : JSON.stringify(b))}</div>`).join('') +
        (more > 0 ? `<div class="ev-card-bullet ev-card-more">+${more} נוספים…</div>` : '') +
        `</div>`;
      seen.add(f);
    }
  }
  const rest = Object.entries(item).filter(([k]) => !seen.has(k));
  if (rest.length > 0) {
    bodyHtml += `<pre class="ev-card-rest">${esc(JSON.stringify(Object.fromEntries(rest), null, 2))}</pre>`;
  }
  const headerHtml = (labelHtml || metaBadges)
    ? `<div class="ev-card-header">${labelHtml}<span class="ev-card-metas">${metaBadges}</span></div>`
    : '';
  return (
    `<div class="ev-full-card">` +
    headerHtml +
    (bodyHtml ? `<div class="ev-card-body">${bodyHtml}</div>` : '') +
    `</div>`
  );
}

/* ═══════════════════════════════════════════════════════════════════
   RECONNECT — recover SSE stream after mobile focus loss
═══════════════════════════════════════════════════════════════════ */

function _injectPartialNotice(containerEl) {
  if (containerEl.querySelector('.reconnect-partial-notice')) return;
  const notice = document.createElement('div');
  notice.className = 'reconnect-partial-notice';
  notice.textContent = 'תוכן חלקי בשל ניתוק בזמן תהליך חשיבה. כדי לראות את התהליך המלא החיבור לאתר צריך להישאר רציף. התוצאה הסופית תוצג לאחר סיום העיבוד, גם אם חלק מהשלבים לא יופיעו כאן.';
  containerEl.insertBefore(notice, containerEl.firstChild);
}

async function _attemptReconnect() {
  if (_reconnecting || !_reconnectSessionId) return;
  _reconnecting = true;
  running = true;
  submitBtn.disabled = true;
  // Remove the disconnect error card — we're recovering
  if (_reconnectErrorEl) { _reconnectErrorEl.remove(); _reconnectErrorEl = null; }

  // Finalize any live stage cards left open from the disconnected stream.
  // Inject partial notice into subgraph wrapper cards so expanding them explains the gap.
  chatColumn.querySelectorAll('.subgraph-card .subgraph-inner-stages').forEach(inner => {
    _injectPartialNotice(inner);
  });
  chatColumn.querySelectorAll('.live-stage-card').forEach(card => {
    card.classList.remove('live-stage-card');
    const dot = card.querySelector('.live-thinking-dot');
    if (dot) dot.remove();
  });

  const sid = _reconnectSessionId;
  const statusEl = appendStatus('מחבר מחדש...');

  try {
    for (let attempt = 0; attempt < 30 && _reconnectSessionId; attempt++) {
      if (attempt > 0) {
        setStatusMsg(statusEl, 'עדיין מעבד — בודק שוב...');
        await new Promise(r => setTimeout(r, attempt < 5 ? 3000 : 8000));
        if (!_reconnectSessionId) break;
      }

      let continueRetry = false;
      try {
        const res = await fetch(`/api/research/${sid}/stream`);
        if (!res.ok) break;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '', curEvent = '';
        let agentEl = null, rawAnswer = '';
        const pendingFootnotes = [], pendingCitations = [];
        // replayRefs tracks state while processing the replayed event_log
        const replayRefs = { stagesEl: null, subgraphContainer: null };

        outer: while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n'); buf = lines.pop();
          for (const line of lines) {
            if (line.startsWith('event: ')) { curEvent = line.slice(7).trim(); continue; }
            if (!line.startsWith('data: ')) continue;
            let data; try { data = JSON.parse(line.slice(6)); } catch { continue; }

            if (curEvent === 'still_running') {
              continueRetry = true;
              break outer;

            } else if (curEvent === 'node_start') {
              if (data.subgraph) {
                if (!replayRefs.stagesEl) {
                  // Replace the old partial stages card with a fresh replay one
                  if (_currentStagesEl) {
                    _currentStagesEl.closest('.msg-agent')?.remove();
                    _currentStagesEl = null;
                  }
                  replayRefs.stagesEl = appendStagesCard();
                  _currentStagesEl = replayRefs.stagesEl;
                }
                replayRefs.subgraphContainer = addSubgraphWrapperCard(replayRefs.stagesEl, data);
                _injectPartialNotice(replayRefs.subgraphContainer);
              }

            } else if (curEvent === 'node_result') {
              if (data.subgraph) {
                const footnotes = data.subgraph?.outputs?.footnotes;
                if (Array.isArray(footnotes) && footnotes.length) pendingFootnotes.push(...footnotes);
                const citations = data.subgraph?.outputs?.citations;
                if (Array.isArray(citations)) pendingCitations.push(...citations);
                finaliseSubgraphCard(replayRefs.subgraphContainer);
                replayRefs.subgraphContainer = null;
              }

            } else if (curEvent === 'subgraph_event') {
              const sg_kind    = data.kind    || '';
              const sg_name    = data.name    || '';
              const sg_payload = data.payload || {};
              if (sg_kind === 'hook' && sg_name === 'step_completed' && replayRefs.subgraphContainer) {
                const stepTask        = sg_payload.step_task || 'שלב';
                const toolName        = sg_payload.tool_name || '';
                const hasError        = !!(sg_payload.error && sg_payload.error !== 'skip');
                const fullResult      = sg_payload.full || '';
                const toolCalls       = sg_payload.tool_calls || [];
                const toolCallResults = sg_payload.tool_call_results || [];
                let toolResults;
                if (toolCallResults.length > 0) {
                  toolResults = toolCallResults.map(tc => ({
                    name: tc.name, args: tc.args || {}, result: tc.full || tc.summary || '', result_ref: tc.result_ref || null,
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
                addCompletedStageCard(replayRefs.subgraphContainer, {
                  label:        `ביצוע: ${stepTask.slice(0, 60)}`,
                  stage:        hasError ? 'reviewer' : 'tool',
                  content:      sg_payload.summary || '',
                  tools:        toolName ? [toolName] : [],
                  tool_results: toolResults,
                  prompt:       {},
                });
              } else if (sg_kind === 'hook' && sg_name === 'synthesizer_completed' && replayRefs.subgraphContainer) {
                addCompletedStageCard(replayRefs.subgraphContainer, {
                  label: 'מסכם ממצאים', stage: 'research',
                  content: '', thinking: '', tools: [], tool_results: [], prompt: {},
                });
              } else if (sg_kind === 'done' && replayRefs.subgraphContainer) {
                finaliseSubgraphCard(replayRefs.subgraphContainer);
                replayRefs.subgraphContainer = null;
              }

            } else if (curEvent === 'token') {
              rawAnswer += data.text || '';
              if (!agentEl) { agentEl = appendAgentCard(); setStatusMsg(statusEl, ''); }
              const body = agentEl.querySelector('.prose-content');
              if (body) body.innerHTML = esc(rawAnswer) + '<span class="stream-cursor"></span>';

            } else if (curEvent === 'done') {
              _reconnectSessionId = null;
              sessionId = sid;
              // Reveal replay stages card if one was created
              if (replayRefs.stagesEl && !_stagesAlways()) {
                const wrap = replayRefs.stagesEl.parentElement;
                if (wrap && wrap.style.display === 'none') wrap.style.display = 'block';
              }
              if (agentEl) {
                const body = agentEl.querySelector('.prose-content');
                if (body) {
                  body.innerHTML = rawAnswer ? marked.parse(rawAnswer) : '';
                  const c = agentEl.querySelector('.stream-cursor');
                  if (c) c.remove();
                  if (pendingFootnotes.length) {
                    _applyEvidenceCitations(body, pendingFootnotes, pendingCitations);
                    agentEl.insertAdjacentHTML('beforeend', _buildSourcesHtml(pendingFootnotes, sid));
                  }
                }
              }
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
                  const rd  = await fetch(`/api/research/${sid}/rag?query=${q}&top_k=20`).then(r => r.json());
                  const mts = rd.meetings || [];
                  openProtocolBrowser(sid, mts[0]?.meeting_id || null, mts, {
                    originalQuestion: _lastQuestion, postCompletion: true,
                  });
                  exploreBtn.innerHTML = 'חקור בפרוטוקולים';
                  exploreBtn.disabled = false;
                } catch (err) { exploreBtn.disabled = false; exploreBtn.innerHTML = 'שגיאה — נסה שוב'; console.warn('[reconnect] explore button error', err); }
              });
              exploreWrap.appendChild(exploreBtn);
              chatColumn.appendChild(exploreWrap);
              break outer;

            } else if (curEvent === 'user_input_required') {
              _reconnectSessionId = null;
              sessionId = sid;
              if (!data.session_id) data = { ...data, session_id: sid };
              renderUserInputPanel(data);
              break outer;

            } else if (curEvent === 'error') {
              _reconnectSessionId = null;
              appendErrorMsg(data.error || 'שגיאה לא ידועה');
              break outer;
            }
          }
        }
      } catch (err) {
        console.warn('[reconnect] network error on attempt, will retry:', err);
      }
      if (!continueRetry) break;
    }
  } finally {
    setStatusMsg(statusEl, '');
    if (statusEl && !statusEl.textContent.trim()) statusEl.remove();
    _reconnectErrorEl = null;
    running = false;
    submitBtn.disabled = false;
    _reconnecting = false;
    queryInput.focus();
    scrollToBottom();
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && _reconnectSessionId && !_reconnecting) {
    _attemptReconnect();
  }
});

/* ── Utility ───────────────────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
