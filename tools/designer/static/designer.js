// ═══════════════════════════════════════════════════════════════════════════
// MODAL HELPERS  (replace native prompt / confirm)
// ═══════════════════════════════════════════════════════════════════════════
const modalOverlay = document.getElementById('modal-overlay');
const modalTitle   = document.getElementById('modal-title');
const modalMsg     = document.getElementById('modal-msg');
const modalInput   = document.getElementById('modal-input');
const modalOk      = document.getElementById('modal-ok');
const modalCancel  = document.getElementById('modal-cancel');
let _modalResolve  = null;

function _openModal({ title, msg, inputDefault }) {
  modalTitle.textContent = title || '';
  if (msg) { modalMsg.textContent = msg; modalMsg.style.display = ''; }
  else      { modalMsg.style.display = 'none'; }
  if (inputDefault !== undefined) {
    modalInput.value = inputDefault;
    modalInput.style.display = '';
    setTimeout(() => { modalInput.focus(); modalInput.select(); }, 50);
  } else {
    modalInput.style.display = 'none';
  }
  modalOverlay.classList.add('open');
  return new Promise(res => { _modalResolve = res; });
}
function _closeModal(value) {
  modalOverlay.classList.remove('open');
  if (_modalResolve) { _modalResolve(value); _modalResolve = null; }
}
modalOk.addEventListener('click', () => {
  _closeModal(modalInput.style.display !== 'none' ? modalInput.value : true);
});
modalCancel.addEventListener('click', () => _closeModal(null));
modalInput.addEventListener('keydown', e => {
  if (e.key === 'Enter')  { e.preventDefault(); modalOk.click(); }
  if (e.key === 'Escape') _closeModal(null);
});

// API mirrors of native dialogs
async function appPrompt(title, defaultVal = '') {
  return _openModal({ title, inputDefault: defaultVal });
}
async function appConfirm(msg) {
  return _openModal({ title: msg });
}

// ═══════════════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════════════
let machine     = null;   // current machine JSON
let cy          = null;   // Cytoscape instance
let eh          = null;   // edgehandles instance
let edgeMode    = null;   // null | {source: nodeId}  (click-click mode)
let dirty       = false;
let snapEnabled = false;
const GRID      = 20;

const machineSelect = document.getElementById('machine-select');
const machineNameEl = document.getElementById('machine-name');
const panel         = document.getElementById('panel');
const panelTitle    = document.getElementById('panel-title');
const panelBody     = document.getElementById('panel-body');

// ═══════════════════════════════════════════════════════════════════════════
// CYTOSCAPE SETUP
// ═══════════════════════════════════════════════════════════════════════════
function initCy() {
  // Register plugins (idempotent — safe to call multiple times)
  if (typeof cytoscapeEdgehandles !== 'undefined') cytoscape.use(cytoscapeEdgehandles);
  if (typeof cytoscapeDagre !== 'undefined') cytoscape.use(cytoscapeDagre);

  cy = cytoscape({
    container: document.getElementById('cy'),
    elements: [],
    style: [
      // ── Node base ──
      {
        selector: 'node',
        style: {
          'label':              'data(label)',
          'text-valign':        'center',
          'text-halign':        'center',
          'font-size':          '13px',
          'font-weight':        '600',
          'color':              '#1e1e2e',
          'text-wrap':          'wrap',
          'text-max-width':     '110px',
          'width':              '130',
          'height':             '50',
          'shape':              'round-rectangle',
          'border-width':       2,
          'border-color':       '#45475a',
          'background-opacity': 1,
        },
      },
      // ── Begin node ──
      {
        selector: 'node[type="begin"]',
        style: {
          'background-color': '#a6e3a1',
          'border-color':     '#40a02b',
          'shape':            'ellipse',
          'width':            '90',
          'height':           '50',
        },
      },
      // ── LLM Call node ──
      {
        selector: 'node[type="llm_call"]',
        style: {
          'background-color': '#89b4fa',
          'border-color':     '#1e66f5',
        },
      },
      // ── Tool node ──
      {
        selector: 'node[type="tool"]',
        style: {
          'background-color': '#fab387',
          'border-color':     '#fe640b',
          'shape':            'round-rectangle',
          'width':            '120',
          'height':           '44',
        },
      },
      // ── User Input node — purple ──
      {
        selector: 'node[type="user_input"]',
        style: {
          'background-color': '#cba6f7',
          'border-color':     '#9a7ec8',
          'shape':            'round-rectangle',
          'width':            '130',
          'height':           '54',
        },
      },
      // ── Terminal node (can end the run) — gold double-ring ──
      {
        selector: 'node[?terminal]',
        style: {
          'border-width':     4,
          'border-color':     '#f9e2af',
          'underlay-color':   '#f9e2af',
          'underlay-padding': '6px',
          'underlay-opacity': 0.18,
          'underlay-shape':   'round-rectangle',
        },
      },
      // ── Imaginary node (not yet implemented) — dashed + faded ──
      {
        selector: 'node[?imaginary]',
        style: {
          'border-style':  'dashed',
          'opacity':       0.5,
        },
      },
      // ── Selected ──
      {
        selector: 'node:selected',
        style: {
          'border-width': 3,
          'border-color': '#f5c2e7',
          'overlay-opacity': 0.08,
          'overlay-color': '#f5c2e7',
        },
      },
      // ── Transition edge ──
      {
        selector: 'edge[edgeType="transition"]',
        style: {
          'width':                2,
          'line-color':           '#cba6f7',
          'target-arrow-color':   '#cba6f7',
          'target-arrow-shape':   'triangle',
          'curve-style':          'bezier',
          'label':                'data(label)',
          'font-size':            '11px',
          'color':                '#cdd6f4',
          'text-background-color':'#1e1e2e',
          'text-background-opacity':0.85,
          'text-background-padding':'3px',
          'text-rotation':        'autorotate',
        },
      },
      // ── Tool link edge ──
      {
        selector: 'edge[edgeType="tool_link"]',
        style: {
          'width':              1.5,
          'line-color':         '#fab387',
          'line-style':         'dashed',
          'target-arrow-color': '#fab387',
          'target-arrow-shape': 'diamond',
          'curve-style':        'bezier',
          'label':              'data(label)',
          'font-size':          '10px',
          'color':              '#a6adc8',
          'text-background-color':'#1e1e2e',
          'text-background-opacity':0.85,
          'text-background-padding':'2px',
          'text-rotation':      'autorotate',
        },
      },
      // ── Selected edge ──
      {
        selector: 'edge:selected',
        style: {
          'width':              3,
          'line-color':         '#f5c2e7',
          'target-arrow-color': '#f5c2e7',
        },
      },
    ],
    layout: { name: 'preset' },
    // Interaction
    minZoom: 0.3,
    maxZoom: 3,
    wheelSensitivity: 0.3,
  });

  // ── Events ──
  cy.on('tap', 'node', e => {
    const node = e.target;
    if (edgeMode) {
      // Complete edge creation
      completeEdge(node.id());
    } else {
      openNodePanel(node.id());
    }
  });

  cy.on('tap', 'edge', e => {
    openEdgePanel(e.target.id());
  });

  cy.on('tap', e => {
    if (e.target === cy) {
      // Background click: cancel edge mode, leave panel open
      edgeMode = null;
      document.getElementById('btn-edge').style.outline = '';
    }
  });

  // Right click context menu
  cy.on('cxttap', 'node', e => {
    e.originalEvent.preventDefault();
    showCtxMenu(e.originalEvent, e.target.id(), 'node');
  });
  cy.on('cxttap', 'edge', e => {
    e.originalEvent.preventDefault();
    showCtxMenu(e.originalEvent, e.target.id(), 'edge');
  });

  // Track position changes + optional snap to grid
  cy.on('dragfree', 'node', e => {
    const n = e.target;
    const mNode = machine?.nodes.find(x => x.id === n.id());
    if (mNode) {
      let pos = { ...n.position() };
      if (snapEnabled) {
        pos.x = Math.round(pos.x / GRID) * GRID;
        pos.y = Math.round(pos.y / GRID) * GRID;
        n.position(pos);
      }
      mNode.position = { x: Math.round(pos.x), y: Math.round(pos.y) };
      markDirty();
    }
  });

  // ── Shift+drag edge creation via edgehandles ──────────────────────
  eh = cy.edgehandles({
    canConnect: (src, tgt) => src.id() !== tgt.id(),
    edgeParams: (src, tgt) => {
      const et = (tgt.data('type') === 'tool' || src.data('type') === 'tool')
                  ? 'tool_link' : 'transition';
      return { data: { edgeType: et, label: '' } };
    },
    snap: false,
    handleSize: 12,
    handleColor: '#cba6f7',
    handleLineType: 'ghost',
    handleLineWidth: 2,
    edgeType: () => 'flat',
    loopAllowed: () => false,
  });

  cy.on('ehcomplete', (event, srcNode, tgtNode, addedEdge) => {
    if (!machine) { addedEdge.remove(); return; }
    const edgeId  = addedEdge.id();
    const srcType = machine.nodes.find(n => n.id === srcNode.id())?.type;
    const tgtType = machine.nodes.find(n => n.id === tgtNode.id())?.type;
    const edgeType = (tgtType === 'tool' || srcType === 'tool') ? 'tool_link' : 'transition';
    addedEdge.data('edgeType', edgeType);
    addedEdge.data('label', '');
    machine.edges.push({
      id: edgeId, source: srcNode.id(), target: tgtNode.id(),
      type: edgeType, label: '',
    });
    markDirty();
    openEdgePanel(edgeId);
  });
}


// ═══════════════════════════════════════════════════════════════════════════
// LOAD / RENDER MACHINE
// ═══════════════════════════════════════════════════════════════════════════
function renderMachine() {
  if (!machine || !cy) return;

  cy.elements().remove();

  for (const n of machine.nodes) {
    cy.add({
      group: 'nodes',
      data: { id: n.id, label: n.label || n.type, type: n.type,
               terminal: n.terminal || false, imaginary: n.imaginary || false },
      position: { ...n.position },
    });
  }

  for (const e of machine.edges) {
    cy.add({
      group: 'edges',
      data: {
        id:       e.id,
        source:   e.source,
        target:   e.target,
        label:    e.label || '',
        edgeType: e.type || 'transition',
      },
    });
  }

  cy.fit(undefined, 40);
  machineNameEl.value = machine.name || '';
  dirty = false;
}


// ═══════════════════════════════════════════════════════════════════════════
// NODE PANEL
// ═══════════════════════════════════════════════════════════════════════════
function openNodePanel(nodeId) {
  const mNode = machine.nodes.find(n => n.id === nodeId);
  if (!mNode) return;

  const typeLabel = {begin:'Begin',llm_call:'LLM Call',tool:'Tool',user_input:'User Input'}[mNode.type] || mNode.type;
  const badgeCls  = {begin:'badge-begin',llm_call:'badge-llm',tool:'badge-tool',user_input:'badge-userinput'}[mNode.type] || '';

  panelTitle.innerHTML = `<span class="badge ${badgeCls}">${typeLabel}</span> ${esc(mNode.label)}`;

  let html = '';

  // ── Label (dir=auto detects Hebrew/Latin automatically) ──
  html += `<div class="field">
    <label>Label</label>
    <input id="pf-label" dir="auto" value="${esc(mNode.label)}" />
  </div>`;

  // ── State flags (shown for all non-begin nodes) ──────────────────
  if (mNode.type !== 'begin') {
    html += `<div class="field">
      <label style="margin-bottom:6px">State flags</label>
      <label class="toggle-row">
        <input type="checkbox" id="pf-terminal" ${mNode.terminal ? 'checked' : ''}>
        <span>Terminal — can end the agent run</span>
      </label>
      <label class="toggle-row">
        <input type="checkbox" id="pf-imaginary" ${mNode.imaginary ? 'checked' : ''}>
        <span>Imaginary — planned but not yet implemented</span>
      </label>
    </div>`;
  }

  if (mNode.type === 'llm_call') {
    const d = mNode.data || {};
    html += `<div class="field">
      <label>System Prompt</label>
      <textarea id="pf-prompt" dir="rtl" rows="12" style="min-height:180px"
        placeholder="פרומפט מערכת...">${esc(d.system_prompt || '')}</textarea>
    </div>`;
    html += `<div class="field">
      <label>Model</label>
      <input id="pf-model" value="${esc(d.model || '')}" placeholder="e.g. qwen3.5-35b" />
    </div>`;
    html += `<div class="field-row">
      <div class="field">
        <label>Temperature</label>
        <input id="pf-temp" type="number" step="0.1" min="0" max="2" value="${d.temperature ?? 0.7}" />
      </div>
      <div class="field">
        <label>Max Tokens</label>
        <input id="pf-maxtok" type="number" value="${d.max_tokens ?? 16384}" />
      </div>
    </div>`;
    html += `<div class="field">
      <label>RAG / Query System</label>
      <select id="pf-rag">
        <option value="">None</option>
        <option value="3level" ${d.rag === '3level' ? 'selected' : ''}>3-Level RAG</option>
        <option value="custom" ${d.rag === 'custom' ? 'selected' : ''}>Custom</option>
      </select>
      <div class="hint">Optional retrieval system attached to this LLM call</div>
    </div>`;
    html += `<div class="field">
      <label>Output Format (JSON)</label>
      <textarea id="pf-output-format" rows="8" style="font-family:monospace;font-size:.75rem;direction:ltr;text-align:left;min-height:140px"
        placeholder='{\n  "type": "labeled_fields",\n  "fields": [\n    {"label": "...", "var": "...", "required": true}\n  ]\n}'>${esc(d.output_format ? JSON.stringify(d.output_format, null, 2) : '')}</textarea>
      <div class="hint">Declarative field extractor. Leave empty for raw text. See machines/knesset_agent.json for examples.</div>
    </div>`;
    html += `<div class="field">
      <label>Input Template</label>
      <textarea id="pf-template" rows="5" style="font-family:monospace;font-size:.78rem;direction:ltr;text-align:left"
        placeholder="Use {{var}} for context variables, e.g. {{question_for_rag}}, {{rag_context}}, {{sub_agent_outputs}}">${esc(d.input_template || '')}</textarea>
      <div class="hint">Leave empty to use ctx["question"] as-is.</div>
    </div>`;
    html += `<div class="field">
      <label>Stage (UI colour)</label>
      <select id="pf-stage">
        <option value="" ${!d.stage ? 'selected' : ''}>Auto-detect</option>
        <option value="router"   ${d.stage === 'router'   ? 'selected' : ''}>router (navy)</option>
        <option value="rag"      ${d.stage === 'rag'      ? 'selected' : ''}>rag (purple)</option>
        <option value="factual"  ${d.stage === 'factual'  ? 'selected' : ''}>factual (amber)</option>
        <option value="reviewer" ${d.stage === 'reviewer' ? 'selected' : ''}>reviewer (green)</option>
      </select>
    </div>`;
    html += `<div class="field">
      <label>Notes</label>
      <textarea id="pf-notes" dir="rtl" rows="3" placeholder="הערות עיצוב...">${esc(d.notes || '')}</textarea>
    </div>`;
  }

  if (mNode.type === 'tool') {
    const d = mNode.data || {};
    html += `<div class="field">
      <label>Tool Function Name</label>
      <input id="pf-fn" value="${esc(d.function_name || '')}" placeholder="e.g. get_meeting_summary" />
    </div>`;
    html += `<div class="field">
      <label>Description</label>
      <textarea id="pf-desc" dir="rtl" rows="3" placeholder="מה הכלי עושה...">${esc(d.description || '')}</textarea>
    </div>`;
    html += `<div class="field">
      <label>Parameters (JSON schema)</label>
      <textarea id="pf-params" rows="5" style="font-family:monospace;font-size:.8rem"
        placeholder='{"type":"object","properties":{...}}'>${esc(d.parameters ? JSON.stringify(d.parameters, null, 2) : '')}</textarea>
    </div>`;
    html += `<div class="field">
      <label>Notes</label>
      <textarea id="pf-notes" dir="rtl" rows="3" placeholder="הערות...">${esc(d.notes || '')}</textarea>
    </div>`;
  }

  if (mNode.type === 'begin') {
    const d = mNode.data || {};
    html += `<div class="field">
      <label>Description</label>
      <textarea id="pf-desc" dir="rtl" rows="3" placeholder="תיאור נקודת ההתחלה...">${esc(d.description || '')}</textarea>
    </div>`;
  }

  if (mNode.type === 'user_input') {
    const d = mNode.data || {};
    html += `<div class="field">
      <label>UI Type</label>
      <select id="pf-ui">
        <option value="option_select"  ${d.ui === 'option_select'  ? 'selected' : ''}>Option Select — card choices</option>
        <option value="text_input"     ${d.ui === 'text_input'     ? 'selected' : ''}>Text Input — free text</option>
        <option value="meeting_select" ${d.ui === 'meeting_select' ? 'selected' : ''}>Meeting Select — browse meetings</option>
      </select>
    </div>`;
    html += `<div class="field">
      <label>Prompt (Hebrew)</label>
      <textarea id="pf-prompt-he" dir="rtl" rows="3"
        placeholder="בחר אפשרות... (תומך ב-{{var}} מהקשר)">${esc(d.prompt_he || '')}</textarea>
      <div class="hint">Supports {{var}} template placeholders from context.</div>
    </div>`;
    html += `<div class="field">
      <label>Output Variable</label>
      <input id="pf-output-var" value="${esc(d.output_var || '')}"
        placeholder="e.g. agent, user_choice"
        style="direction:ltr;text-align:left;font-family:monospace" />
      <div class="hint">Context variable that receives the user's answer on resume.</div>
    </div>`;
    html += `<div class="field">
      <label>Pre-select Variable <span style="font-weight:400;color:#6c7086">(option_select only)</span></label>
      <input id="pf-preselect-var" value="${esc(d.preselect_var || '')}"
        placeholder="e.g. agent"
        style="direction:ltr;text-align:left;font-family:monospace" />
      <div class="hint">Context var whose current value is pre-selected in the option list.</div>
    </div>`;
    html += `<div class="field">
      <label>Options <span style="font-weight:400;color:#6c7086">(option_select only)</span></label>
      <textarea id="pf-options" rows="10"
        style="font-family:monospace;font-size:.75rem;direction:ltr;text-align:left;min-height:160px"
        placeholder='[\n  {"value":"a","label":"א","description":"..."}\n]'>${esc(d.options ? JSON.stringify(d.options, null, 2) : '')}</textarea>
      <div class="hint">JSON array of {value, label, description?} objects.</div>
    </div>`;
  }

  // Connected edges summary
  const connEdges = machine.edges.filter(e => e.source === nodeId || e.target === nodeId);
  if (connEdges.length) {
    html += `<div class="field"><label>Connected Edges (${connEdges.length})</label>`;
    for (const e of connEdges) {
      const dir = e.source === nodeId ? '&rarr;' : '&larr;';
      const other = e.source === nodeId ? e.target : e.source;
      const otherNode = machine.nodes.find(n => n.id === other);
      html += `<div style="font-size:.78rem;color:#a6adc8;padding:2px 0">
        ${dir} ${esc(otherNode?.label || other)} <span style="color:#6c7086">(${e.type}${e.label ? ': '+esc(e.label) : ''})</span>
      </div>`;
    }
    html += `</div>`;
  }

  html += `<button class="primary" style="width:100%;margin-top:8px" onclick="applyNodePanel('${nodeId}')">Apply</button>`;

  panelBody.innerHTML = html;
  openPanel();
}


function applyNodePanel(nodeId) {
  const mNode = machine.nodes.find(n => n.id === nodeId);
  if (!mNode) return;

  mNode.label = document.getElementById('pf-label')?.value || mNode.label;

  // State flags (all non-begin nodes)
  if (mNode.type !== 'begin') {
    mNode.terminal  = document.getElementById('pf-terminal')?.checked  || false;
    mNode.imaginary = document.getElementById('pf-imaginary')?.checked || false;
  }

  if (mNode.type === 'llm_call') {
    mNode.data = mNode.data || {};
    mNode.data.system_prompt  = document.getElementById('pf-prompt')?.value || '';
    mNode.data.model          = document.getElementById('pf-model')?.value || '';
    mNode.data.temperature    = parseFloat(document.getElementById('pf-temp')?.value) || 0.7;
    mNode.data.max_tokens     = parseInt(document.getElementById('pf-maxtok')?.value) || 16384;
    mNode.data.rag            = document.getElementById('pf-rag')?.value || '';
    mNode.data.input_template = document.getElementById('pf-template')?.value || '';
    mNode.data.stage          = document.getElementById('pf-stage')?.value || '';
    mNode.data.notes          = document.getElementById('pf-notes')?.value || '';

    // output_format: parse JSON or clear
    const ofRaw = document.getElementById('pf-output-format')?.value.trim() || '';
    if (ofRaw) {
      try {
        mNode.data.output_format = JSON.parse(ofRaw);
      } catch {
        toast('⚠ output_format: invalid JSON — not saved');
      }
    } else {
      delete mNode.data.output_format;
    }
  }

  if (mNode.type === 'tool') {
    mNode.data = mNode.data || {};
    mNode.data.function_name = document.getElementById('pf-fn')?.value || '';
    mNode.data.description   = document.getElementById('pf-desc')?.value || '';
    mNode.data.notes         = document.getElementById('pf-notes')?.value || '';
    try {
      const p = document.getElementById('pf-params')?.value || '';
      mNode.data.parameters = p ? JSON.parse(p) : {};
    } catch { /* keep existing */ }
  }

  if (mNode.type === 'begin') {
    mNode.data = mNode.data || {};
    mNode.data.description = document.getElementById('pf-desc')?.value || '';
  }

  if (mNode.type === 'user_input') {
    mNode.data = mNode.data || {};
    mNode.data.ui         = document.getElementById('pf-ui')?.value         || 'text_input';
    mNode.data.prompt_he  = document.getElementById('pf-prompt-he')?.value  || '';
    mNode.data.output_var = document.getElementById('pf-output-var')?.value.trim() || '';

    const preselVar = document.getElementById('pf-preselect-var')?.value.trim();
    if (preselVar) mNode.data.preselect_var = preselVar;
    else delete mNode.data.preselect_var;

    const optsRaw = document.getElementById('pf-options')?.value.trim() || '';
    if (optsRaw) {
      try { mNode.data.options = JSON.parse(optsRaw); }
      catch { toast('⚠ options: invalid JSON — not saved'); }
    } else {
      delete mNode.data.options;
    }
  }

  // Sync Cytoscape node data
  const cyNode = cy.getElementById(nodeId);
  if (cyNode.length) {
    cyNode.data('label',     mNode.label);
    cyNode.data('terminal',  mNode.terminal  || false);
    cyNode.data('imaginary', mNode.imaginary || false);
  }

  // Refresh panel title to reflect updated label
  panelTitle.innerHTML = `<span class="badge ${
    {begin:'badge-begin',llm_call:'badge-llm',tool:'badge-tool',user_input:'badge-userinput'}[mNode.type]||''
  }">${{begin:'Begin',llm_call:'LLM Call',tool:'Tool',user_input:'User Input'}[mNode.type]||mNode.type}</span> ${esc(mNode.label)}`;

  markDirty();
  toast('Applied');
}


// ═══════════════════════════════════════════════════════════════════════════
// EDGE PANEL
// ═══════════════════════════════════════════════════════════════════════════
function openEdgePanel(edgeId) {
  const mEdge = machine.edges.find(e => e.id === edgeId);
  if (!mEdge) return;

  const srcNode = machine.nodes.find(n => n.id === mEdge.source);
  const tgtNode = machine.nodes.find(n => n.id === mEdge.target);

  panelTitle.textContent = 'Edge Details';
  let html = `
    <div style="font-size:.82rem;color:#a6adc8;margin-bottom:12px">
      ${esc(srcNode?.label||mEdge.source)} &rarr; ${esc(tgtNode?.label||mEdge.target)}
    </div>
    <div class="field">
      <label>Type</label>
      <select id="pe-type">
        <option value="transition" ${mEdge.type==='transition'?'selected':''}>Transition (decision/flow)</option>
        <option value="tool_link" ${mEdge.type==='tool_link'?'selected':''}>Tool Link (tool availability)</option>
      </select>
    </div>
    <div class="field">
      <label>Label / Decision</label>
      <input id="pe-label" value="${esc(mEdge.label || '')}" placeholder="e.g. 'needs more info', 'done'" />
      <div class="hint">For transitions: the decision/question that triggers this path</div>
    </div>
    <div class="field" id="pe-cond-field" style="${mEdge.type === 'tool_link' ? 'display:none' : ''}">
      <label>Condition</label>
      <input id="pe-condition" value="${esc(mEdge.condition || '')}"
        placeholder="e.g.  agent in ['פרוטוקולים', 'שניהם']" style="direction:ltr;text-align:left;font-family:monospace;font-size:.82rem" />
      <div class="hint">Leave empty to always follow. Supported: key == 'v', key != '', key in ['a','b']</div>
    </div>
    <div class="field" id="pe-loops-field" style="${mEdge.type === 'tool_link' ? 'display:none' : ''}">
      <label>Max Loops (back-edges only)</label>
      <input id="pe-maxloops" type="number" min="1" value="${mEdge.max_loops != null ? mEdge.max_loops : ''}" placeholder="3 (default)" />
      <div class="hint">Caps the outer reviewer→router loop. Only meaningful on back-edges.</div>
    </div>
    <button class="primary" style="width:100%;margin-top:8px" onclick="applyEdgePanel('${edgeId}')">Apply</button>
    <button class="danger" style="width:100%;margin-top:6px" onclick="deleteEdge('${edgeId}')">Delete Edge</button>
  `;
  panelBody.innerHTML = html;
  openPanel();
}


function applyEdgePanel(edgeId) {
  const mEdge = machine.edges.find(e => e.id === edgeId);
  if (!mEdge) return;

  mEdge.type  = document.getElementById('pe-type')?.value || 'transition';
  mEdge.label = document.getElementById('pe-label')?.value || '';
  const cond = document.getElementById('pe-condition')?.value.trim() || '';
  if (cond) mEdge.condition = cond; else delete mEdge.condition;
  const ml = document.getElementById('pe-maxloops')?.value.trim();
  if (ml) mEdge.max_loops = parseInt(ml); else delete mEdge.max_loops;

  // Update cytoscape
  const cyEdge = cy.getElementById(edgeId);
  if (cyEdge.length) {
    cyEdge.data('label', mEdge.label);
    cyEdge.data('edgeType', mEdge.type);
  }
  markDirty();
  toast('Applied');
}


function deleteEdge(edgeId) {
  machine.edges = machine.edges.filter(e => e.id !== edgeId);
  cy.getElementById(edgeId).remove();
  closePanel();
  markDirty();
}


// ═══════════════════════════════════════════════════════════════════════════
// ADD NODES
// ═══════════════════════════════════════════════════════════════════════════
function addNode(type) {
  if (!machine) { toast('Create or load a machine first'); return; }
  const id = type + '_' + crypto.randomUUID().slice(0,8);
  const labels = { llm_call: 'LLM Call', tool: 'Tool', user_input: 'User Input' };
  const label = labels[type] || type;

  // Place near center of current view
  const ext = cy.extent();
  const pos = { x: Math.round((ext.x1+ext.x2)/2 + (Math.random()-0.5)*100),
                y: Math.round((ext.y1+ext.y2)/2 + (Math.random()-0.5)*100) };

  const node = { id, type, label, position: pos, data: {}, terminal: false, imaginary: false };
  machine.nodes.push(node);

  cy.add({
    group: 'nodes',
    data: { id, label, type, terminal: false, imaginary: false },
    position: { ...pos },
  });

  markDirty();
  openNodePanel(id);
}

document.getElementById('add-llm').addEventListener('click',       () => addNode('llm_call'));
document.getElementById('add-tool').addEventListener('click',      () => addNode('tool'));
document.getElementById('add-userinput').addEventListener('click', () => addNode('user_input'));


// ═══════════════════════════════════════════════════════════════════════════
// ADD EDGES
// ═══════════════════════════════════════════════════════════════════════════
document.getElementById('btn-edge').addEventListener('click', () => {
  if (!machine) { toast('Create or load a machine first'); return; }
  edgeMode = {};
  document.getElementById('btn-edge').style.outline = '2px solid #f5c2e7';
  toast('Click the SOURCE node');
});

function completeEdge(targetId) {
  if (!edgeMode) return;

  if (!edgeMode.source) {
    edgeMode.source = targetId;
    toast('Now click the TARGET node');
    return;
  }

  const sourceId = edgeMode.source;
  edgeMode = null;
  document.getElementById('btn-edge').style.outline = '';

  if (sourceId === targetId) { toast('Cannot connect a node to itself'); return; }

  // Determine edge type by node types
  const srcNode = machine.nodes.find(n => n.id === sourceId);
  const tgtNode = machine.nodes.find(n => n.id === targetId);
  let edgeType = 'transition';
  if (tgtNode?.type === 'tool' || srcNode?.type === 'tool') edgeType = 'tool_link';

  const id = 'e_' + crypto.randomUUID().slice(0,8);
  const edge = { id, source: sourceId, target: targetId, type: edgeType, label: '' };
  machine.edges.push(edge);

  cy.add({
    group: 'edges',
    data: { id, source: sourceId, target: targetId, label: '', edgeType },
  });

  markDirty();
  openEdgePanel(id);
}


// ═══════════════════════════════════════════════════════════════════════════
// CONTEXT MENU
// ═══════════════════════════════════════════════════════════════════════════
const ctxMenu = document.getElementById('ctx-menu');
let ctxTarget = null;
let ctxTargetType = null;  // 'node' or 'edge'

function showCtxMenu(ev, id, type) {
  ctxTarget = id;
  ctxTargetType = type;

  const items = ctxMenu.querySelectorAll('.item');
  // Hide "add-edge" for edges
  items[1].style.display = type === 'edge' ? 'none' : '';

  ctxMenu.style.left = ev.clientX + 'px';
  ctxMenu.style.top  = ev.clientY + 'px';
  ctxMenu.style.display = 'block';
}

document.addEventListener('click', () => { ctxMenu.style.display = 'none'; });

ctxMenu.querySelectorAll('.item').forEach(el => {
  el.addEventListener('click', e => {
    e.stopPropagation();
    ctxMenu.style.display = 'none';
    const action = el.dataset.action;

    if (action === 'edit') {
      if (ctxTargetType === 'node') openNodePanel(ctxTarget);
      else openEdgePanel(ctxTarget);
    }
    else if (action === 'add-edge') {
      edgeMode = { source: ctxTarget };
      document.getElementById('btn-edge').style.outline = '2px solid #f5c2e7';
      toast('Click the TARGET node');
    }
    else if (action === 'delete') {
      if (ctxTargetType === 'node') deleteNode(ctxTarget);
      else deleteEdge(ctxTarget);
    }
  });
});


function deleteNode(nodeId) {
  const mNode = machine.nodes.find(n => n.id === nodeId);
  if (mNode?.type === 'begin') { toast('Cannot delete the Begin node'); return; }

  machine.nodes = machine.nodes.filter(n => n.id !== nodeId);
  machine.edges = machine.edges.filter(e => e.source !== nodeId && e.target !== nodeId);

  cy.getElementById(nodeId).remove();
  closePanel();
  markDirty();
}


// ═══════════════════════════════════════════════════════════════════════════
// PANEL HELPERS
// ═══════════════════════════════════════════════════════════════════════════
function openPanel() {
  panel.classList.add('visible');
}

function closePanel() {
  panel.classList.remove('visible');
}
document.getElementById('panel-close').addEventListener('click', closePanel);


// ═══════════════════════════════════════════════════════════════════════════
// GLOBAL RULES PANEL
// ═══════════════════════════════════════════════════════════════════════════
document.getElementById('btn-rules').addEventListener('click', () => {
  if (!machine) { toast('Load a machine first'); return; }
  openRulesPanel();
});

function openRulesPanel() {
  panelTitle.textContent = 'Global Rules';
  panelBody.innerHTML = `
    <div class="field">
      <div class="hint" style="margin-bottom:8px">
        These rules are prepended to every node's system prompt at runtime.
        Use them for agent-wide instructions, language requirements, formatting rules, etc.
      </div>
      <textarea id="pf-global-rules" dir="rtl" rows="18" style="min-height:280px;font-size:.82rem"
        placeholder="הנחיות כלליות לכל הסוכן...">${esc(machine.global_rules || '')}</textarea>
    </div>
    <button class="primary" style="width:100%;margin-top:8px" onclick="applyRulesPanel()">Apply</button>
  `;
  openPanel();
}

function applyRulesPanel() {
  if (!machine) return;
  machine.global_rules = document.getElementById('pf-global-rules')?.value || '';
  markDirty();
  toast('Global rules updated');
}


// ═══════════════════════════════════════════════════════════════════════════
// SAVE / LOAD / NEW / DELETE / EXPORT
// ═══════════════════════════════════════════════════════════════════════════
async function loadMachineList() {
  const res = await fetch('/api/machines');
  const list = await res.json();
  machineSelect.innerHTML = '<option value="">-- Load machine --</option>';
  for (const m of list) {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.name;
    machineSelect.appendChild(opt);
  }
}

machineSelect.addEventListener('change', async () => {
  const id = machineSelect.value;
  if (!id) return;
  if (dirty) {
    const ok = await appConfirm('Unsaved changes will be lost. Continue?');
    if (!ok) { machineSelect.value = machine?.id || ''; return; }
  }
  const res = await fetch(`/api/machines/${id}`);
  machine = await res.json();
  renderMachine();
  toast(`Loaded: ${machine.name}`);
});

document.getElementById('btn-new').addEventListener('click', async () => {
  if (dirty) {
    const ok = await appConfirm('Unsaved changes will be lost. Continue?');
    if (!ok) return;
  }
  const name = await appPrompt('New machine name:', 'New Machine');
  if (!name) return;
  const res = await fetch('/api/machines', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ name }),
  });
  machine = await res.json();
  renderMachine();
  await loadMachineList();
  machineSelect.value = machine.id;
  toast(`Created: ${name}`);
});

document.getElementById('btn-save').addEventListener('click', async () => {
  if (!machine) { toast('Nothing to save'); return; }
  machine.name = machineNameEl.value || machine.name;
  await fetch(`/api/machines/${machine.id}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(machine),
  });
  dirty = false;
  await loadMachineList();
  machineSelect.value = machine.id;
  toast('Saved!');
});

document.getElementById('btn-export').addEventListener('click', () => {
  if (!machine) { toast('Nothing to export'); return; }
  const json = JSON.stringify(machine, null, 2);
  const blob = new Blob([json], { type: 'application/json' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `${machine.name.replace(/\s+/g, '_') || machine.id}.json`;
  a.click();
  URL.revokeObjectURL(url);
  toast('Exported!');
});

document.getElementById('btn-delete').addEventListener('click', async () => {
  if (!machine) return;
  const ok = await appConfirm(`Delete machine "${machine.name}"?`);
  if (!ok) return;
  await fetch(`/api/machines/${machine.id}`, { method: 'DELETE' });
  machine = null;
  cy.elements().remove();
  closePanel();
  machineNameEl.value = '';
  dirty = false;
  await loadMachineList();
  toast('Deleted');
});

document.getElementById('btn-fit').addEventListener('click', () => {
  if (cy) { cy.resize(); cy.fit(undefined, 40); }
});

document.getElementById('btn-snap').addEventListener('click', () => {
  snapEnabled = !snapEnabled;
  document.getElementById('btn-snap').classList.toggle('active', snapEnabled);
  toast(snapEnabled ? 'Snap to grid ON (20px)' : 'Snap to grid OFF');
});

document.getElementById('btn-layout').addEventListener('click', () => {
  if (!cy || !machine || cy.nodes().length === 0) return;
  cy.layout({
    name: 'dagre',
    rankDir: 'LR',
    padding: 50,
    nodeSep: 50,
    rankSep: 120,
    animate: true,
    animationDuration: 350,
    fit: true,
  }).run();
  // Sync positions back to machine data after animation
  setTimeout(() => {
    cy.nodes().forEach(n => {
      const mNode = machine?.nodes.find(x => x.id === n.id());
      if (mNode) {
        const p = n.position();
        mNode.position = { x: Math.round(p.x), y: Math.round(p.y) };
      }
    });
    markDirty();
  }, 400);
});


// ═══════════════════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════════════════
function markDirty() { dirty = true; }

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2000);
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault();
    document.getElementById('btn-save').click();
  }
  if (e.key === 'Escape') {
    edgeMode = null;
    document.getElementById('btn-edge').style.outline = '';
    closePanel();
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    // Don't delete if focus is in an input/textarea
    if (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA') return;
    const sel = cy?.$((':selected'));
    if (sel?.length === 1) {
      const el = sel[0];
      if (el.isNode()) deleteNode(el.id());
      else deleteEdge(el.id());
    }
  }
  // Shift activates edgehandles draw mode (Shift+drag to draw edge)
  if (e.key === 'Shift' && !e.repeat && machine && eh) {
    eh.enableDrawMode();
    document.getElementById('cy').style.cursor = 'crosshair';
  }
});
document.addEventListener('keyup', e => {
  if (e.key === 'Shift' && eh) {
    eh.disableDrawMode();
    document.getElementById('cy').style.cursor = '';
  }
});


// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════
initCy();
loadMachineList();
