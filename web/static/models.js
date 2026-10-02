/**
 * models.js — the visitor's Gemini/Gemma model for each part of the research agent.
 *
 * The server has no defaults: the choice is kept in this browser (localStorage) and sent with each
 * new research (/api/research/start) and meeting-chat question. The settings fill each part's choices
 * from the models the visitor's key can call (/api/gemini/models).
 */
import { geminiKeyHeaders, getGeminiKey } from './gemini_key.js';
import { esc } from './util.js';

export const DEFAULT_RESEARCH_MODELS = {
  intent:        'gemma-4-31b-it',
  planner:       'gemini-3.5-flash',
  critic:        'gemini-3.5-flash-lite',
  executor:      'gemini-3.1-flash-lite',
  synthesizer:   'gemini-3.8-flash',
  answer_editor: 'gemini-3.1-flash-lite',
};

const MODEL_ROLES = [
  { role: 'intent',        label: 'זיהוי סוג השאלה' },
  { role: 'planner',       label: 'תכנון המחקר',   warnsOnLightModel: true },
  { role: 'critic',        label: 'ביקורת התוכנית והממצאים' },
  { role: 'executor',      label: 'הרצת שלבי המחקר' },
  { role: 'synthesizer',   label: 'כתיבת התשובה',  warnsOnLightModel: true },
  { role: 'answer_editor', label: 'עריכת התשובה ושיחה על פרוטוקול' },
];

const STORAGE_KEY = 'researchModels';
const MODEL_ID_PATTERN = /^(gemini|gemma)-[a-z0-9.\-]{1,60}$/;

let modelsWhenStorageUnavailable = null;
let keyModelsCache = { key: null, models: null };

function storedModels() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') || modelsWhenStorageUnavailable || {};
  } catch (exc) {
    console.error('[models] reading the stored models failed:', exc);
    return modelsWhenStorageUnavailable || {};
  }
}

function storeModels(models) {
  modelsWhenStorageUnavailable = models;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(models));
  } catch (exc) {
    console.error('[models] localStorage write failed, choice kept for this page only:', exc);
  }
}

/** The model of each part: the visitor's choice where it is a valid id, else the default. */
export function getResearchModels() {
  const stored = storedModels();
  return Object.fromEntries(Object.entries(DEFAULT_RESEARCH_MODELS).map(([role, defaultModel]) =>
    [role, MODEL_ID_PATTERN.test(stored[role] || '') ? stored[role] : defaultModel]));
}

export function meetingChatModel() {
  return getResearchModels().answer_editor;
}

function isLightModel(modelId) {
  return modelId.includes('lite') || modelId.startsWith('gemma-');
}

async function keyModels() {
  const key = getGeminiKey();
  if (!key) return null;
  if (keyModelsCache.key === key && keyModelsCache.models) return keyModelsCache.models;
  const res = await fetch('/api/gemini/models', { headers: geminiKeyHeaders() });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const { models } = await res.json();
  keyModelsCache = { key, models };
  return models;
}

function modelOptionsHtml(availableModels, chosenModel) {
  const options = availableModels.map(model =>
    `<option value="${esc(model.id)}"${model.id === chosenModel ? ' selected' : ''}>${esc(model.id)}</option>`);
  if (!availableModels.some(model => model.id === chosenModel)) {
    const note = availableModels.length ? ' (לא זמין במפתח)' : '';
    options.unshift(`<option value="${esc(chosenModel)}" selected>${esc(chosenModel)}${note}</option>`);
  }
  return options.join('');
}

function renderModelRows(availableModels) {
  const container = document.getElementById('model-settings');
  if (!container) return;
  const chosen = getResearchModels();
  container.innerHTML = MODEL_ROLES.map(({ role, label, warnsOnLightModel }) => `
    <div class="model-setting-row">
      <label class="model-setting-label" for="model-${esc(role)}">${esc(label)}</label>
      <select id="model-${esc(role)}" class="model-setting-select" dir="ltr" data-role="${esc(role)}"
              data-change="setResearchModel"${availableModels.length ? '' : ' disabled'}>
        ${modelOptionsHtml(availableModels, chosen[role])}
      </select>
      ${warnsOnLightModel && isLightModel(chosen[role])
        ? '<div class="model-setting-warning">מודל קל עלול להפיק כאן תוצאות חלשות או שבורות</div>' : ''}
    </div>`).join('');
}

function setModelSettingsStatus(message) {
  const statusEl = document.getElementById('model-settings-status');
  if (statusEl) statusEl.textContent = message;
}

/** Fill the settings' model choices from the key's model list (fetched once per key per page). */
export async function refreshModelSettings() {
  renderModelRows([]);
  if (!getGeminiKey()) {
    setModelSettingsStatus('אחרי הזנת מפתח Gemini אפשר לבחור מבין המודלים הזמינים בו');
    return;
  }
  setModelSettingsStatus('טוען את רשימת המודלים…');
  try {
    renderModelRows(await keyModels());
    setModelSettingsStatus('המודל של כל חלק במחקר, מבין המודלים הזמינים במפתח');
  } catch (exc) {
    console.error('[models] loading the key\'s models failed:', exc);
    setModelSettingsStatus('לא ניתן לטעון כרגע את רשימת המודלים מ-Google');
  }
}

function setResearchModel(role, modelId) {
  if (!(role in DEFAULT_RESEARCH_MODELS) || !MODEL_ID_PATTERN.test(modelId)) return;
  storeModels({ ...getResearchModels(), [role]: modelId });
  renderModelRows(keyModelsCache.models || []);
}

function resetResearchModels() {
  storeModels({ ...DEFAULT_RESEARCH_MODELS });
  renderModelRows(keyModelsCache.models || []);
}

window.setResearchModel     = setResearchModel;
window.resetResearchModels  = resetResearchModels;
window.meetingChatModel     = meetingChatModel;
