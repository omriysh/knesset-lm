const queryInput = document.getElementById('query-input');
const submitBtn  = document.getElementById('submit');
const statusEl   = document.getElementById('status');
const statusMsg  = document.getElementById('status-msg');
const timerEl    = document.getElementById('status-timer');
const errorEl    = document.getElementById('error-box');
const answerBox  = document.getElementById('answer-box');
const answerEl   = document.getElementById('answer-content');
const stagesBox  = document.getElementById('stages-box');
const devToggle  = document.getElementById('dev-toggle');

let running        = false;
let rawAnswer      = '';
let _timerInterval = null;
let _timerStart    = 0;
let devMode        = false;
let stageDataBuf   = [];   // buffer of node_result payloads for the current query

devToggle.addEventListener('click', () => {
  devMode = !devMode;
  devToggle.classList.toggle('active', devMode);
  if (devMode) {
    // Render any cards that arrived before dev mode was turned on
    stagesBox.innerHTML = '';
    for (const d of stageDataBuf) addStageCard(d);
    if (stageDataBuf.length) stagesBox.style.display = 'block';
  } else {
    stagesBox.innerHTML = '';
    stagesBox.style.display = 'none';
  }
});

marked.use({ breaks: true, gfm: true });

// Ctrl+Enter submits
queryInput.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    if (!running) startQuery();
  }
});
submitBtn.addEventListener('click', () => { if (!running) startQuery(); });

function _startTimer() {
  _timerStart = Date.now();
  timerEl.style.display = 'block';
  timerEl.textContent = '0.0s';
  _timerInterval = setInterval(() => {
    timerEl.textContent = ((Date.now() - _timerStart) / 1000).toFixed(1) + 's';
  }, 100);
}

function _stopTimer() {
  clearInterval(_timerInterval);
  _timerInterval = null;
  timerEl.textContent = ((Date.now() - _timerStart) / 1000).toFixed(1) + 's';
}

async function startQuery() {
  const question = queryInput.value.trim();
  if (!question) return;

  running = true;
  submitBtn.disabled = true;
  rawAnswer = '';
  stageDataBuf = [];

  errorEl.style.display = 'none';
  answerBox.style.display = 'none';
  answerEl.className = '';
  answerEl.textContent = '';
  stagesBox.style.display = 'none';
  stagesBox.innerHTML = '';
  timerEl.style.display = 'none';
  setStatus('מחפש...');
  _startTimer();

  let curEvent = '';
  let buf = '';

  try {
    const res = await fetch('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
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
          handleEvent(curEvent, data);
        }
      }
    }
  } catch (err) {
    showError('שגיאת חיבור: ' + err.message);
  } finally {
    if (rawAnswer) {
      answerEl.innerHTML = marked.parse(rawAnswer);
      answerEl.classList.add('rendered');
    }
    _stopTimer();
    statusMsg.innerHTML = '';
    submitBtn.disabled = false;
    running = false;
  }
}

function handleEvent(ev, data) {
  switch (ev) {
    case 'status':
      setStatus(data.msg || '');
      break;
    case 'node_result':
      stageDataBuf.push(data);
      if (devMode) addStageCard(data);
      break;
    case 'token':
      rawAnswer += data.text || '';
      if (answerBox.style.display === 'none') {
        answerBox.style.display = 'block';
        setStatus('');
      }
      answerEl.innerHTML = esc(rawAnswer) + '<span class="cursor"></span>';
      break;
    case 'done':
      break;
    case 'error':
      showError(data.error || 'שגיאה לא ידועה');
      break;
  }
}

function setStatus(msg) {
  if (msg) {
    statusMsg.innerHTML =
      '<span class="thinking-dots"><span></span><span></span><span></span></span>' +
      esc(msg);
  } else {
    statusMsg.innerHTML = '';
  }
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.style.display = 'block';
  setStatus('');
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Stage cards ───────────────────────────────────────────────────────────────

const _STAGE_TAGS = {
  router:   'ניתוב',
  rag:      'פרוטוקולים',
  factual:  'עובדתי',
  reviewer: 'עריכה',
};

function addStageCard(data) {
  const label       = data.label        || 'שלב';
  const stage       = data.stage        || 'unknown';
  const content     = data.content      || '';
  const loop        = data.loop         || 0;
  const elapsedMs   = data.elapsed_ms   != null ? data.elapsed_ms : null;
  const tools       = data.tools        || [];
  const retrieval   = data.retrieval    || null;
  const prompt      = data.prompt       || null;
  const toolResults = data.tool_results || [];

  stagesBox.style.display = 'block';

  const card = document.createElement('div');
  card.className = 'stage-card';

  const tagText  = _STAGE_TAGS[stage] || stage;
  const timeHtml = elapsedMs != null
    ? `<span class="stage-time">${(elapsedMs / 1000).toFixed(1)}s</span>`
    : '';

  const uniqueTools = [...new Set(tools)];
  const toolsHtml = uniqueTools.length > 0
    ? `<span class="stage-tools-badge">${uniqueTools.join(' · ')}</span>`
    : '';

  const loopHtml = loop > 0
    ? `<span class="stage-loop-badge">סבב ${loop + 1}</span>`
    : '';

  const promptHtml      = prompt ? renderPromptHtml(prompt, devMode) : '';
  const toolResultsHtml = toolResults.map(tr => renderToolResultHtml(tr, devMode)).join('');
  const retrievalHtml   = (stage === 'rag' && retrieval) ? renderRetrievalHtml(retrieval) : '';

  const bodyClass = devMode ? 'stage-body visible' : 'stage-body';
  const headerClass = devMode ? 'stage-header open' : 'stage-header';

  card.innerHTML =
    `<div class="${headerClass}" onclick="toggleStageCard(this)">` +
      `<span class="stage-arrow">&#9658;</span>` +
      `<span class="stage-dot ${esc(stage)}"></span>` +
      `<span class="stage-name">${esc(label)}</span>` +
      `<span class="stage-tag">${esc(tagText)}</span>` +
      `<span class="stage-meta">${timeHtml}${toolsHtml}${loopHtml}</span>` +
    `</div>` +
    `<div class="${bodyClass}">${promptHtml}${toolResultsHtml}${retrievalHtml}${marked.parse(content)}</div>`;

  stagesBox.appendChild(card);
}

function renderPromptHtml(p, expanded) {
  const sys  = p.system || '';
  const user = p.user   || '';
  const open = expanded ? ' open' : '';
  return (
    `<details class="sub-details"${open}>` +
    `<summary class="sub-summary">פרומפט</summary>` +
    `<div class="sub-details-body">` +
    `<div class="prompt-block">` +
    `<div class="prompt-role">מערכת</div>` +
    `<pre class="prompt-text">${esc(sys)}</pre>` +
    `</div>` +
    `<div class="prompt-block">` +
    `<div class="prompt-role">משתמש</div>` +
    `<pre class="prompt-text">${esc(user)}</pre>` +
    `</div>` +
    `</div>` +
    `</details>`
  );
}

function renderToolResultHtml(tr, expanded) {
  const name    = tr.name   || '';
  const args    = tr.args   || {};
  const result  = tr.result || '';
  const argsStr = JSON.stringify(args, null, 2);
  const open    = expanded ? ' open' : '';
  return (
    `<details class="sub-details"${open}>` +
    `<summary class="sub-summary"><span class="tool-summary-label">${esc(name)}</span></summary>` +
    `<div class="sub-details-body">` +
    `<div class="prompt-block">` +
    `<div class="prompt-role">ארגומנטים</div>` +
    `<pre class="prompt-text">${esc(argsStr)}</pre>` +
    `</div>` +
    `<div class="prompt-block">` +
    `<div class="prompt-role">תוצאה</div>` +
    `<pre class="prompt-text">${esc(result)}</pre>` +
    `</div>` +
    `</div>` +
    `</details>`
  );
}

function renderRetrievalHtml(r) {
  const meetings = r.meetings      || [];
  const chunks   = r.chunks        || [];
  const chars    = r.context_chars || 0;
  const tokEst   = Math.round(chars / 2);

  const summaryLabel =
    `פרטי אחזור — ${meetings.length} ישיבות · ${chunks.length} קטעים · ~${tokEst.toLocaleString()} טוקנים`;

  let body = '<div class="stats-row">';
  body += 'ישיבות: <strong>' + meetings.length + '</strong>';
  body += ' &nbsp;|&nbsp; קטעים: <strong>' + chunks.length + '</strong>';
  body += ' &nbsp;|&nbsp; הקשר: <strong>' + chars.toLocaleString() + '</strong>';
  body += ' תווים (~' + tokEst.toLocaleString() + ' טוקנים)';
  body += '</div>';

  if (meetings.length) {
    body += '<div style="color:#666;font-size:.74rem;margin-bottom:6px;direction:rtl">';
    body += meetings.map(esc).join(' &middot; ');
    body += '</div>';
  }

  if (chunks.length) {
    body += '<ul class="chunk-list">';
    for (const c of chunks) {
      body += '<li class="chunk-item">';
      body += '<span class="chunk-date">' + esc(c.date) + '</span>';
      body += '<span class="chunk-topic">' + esc(c.topic) + '</span>';
      body += '<span class="chunk-sim">sim&nbsp;' + c.p1_sim + '</span>';
      body += '</li>';
    }
    body += '</ul>';
  }

  return (
    `<details class="sub-details">` +
    `<summary class="sub-summary">${summaryLabel}</summary>` +
    `<div class="sub-details-body">${body}</div>` +
    `</details>`
  );
}

function toggleStageCard(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('visible');
}
