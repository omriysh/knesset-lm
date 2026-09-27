/**
 * gemini_key.js — the visitor's Gemini API key for the auto-research tab.
 *
 * Kept only in this browser (localStorage) and sent as the X-Gemini-Api-Key
 * header with agent requests; the server passes it to Gemini and never stores it.
 */
export const GEMINI_KEY_HEADER = 'X-Gemini-Api-Key';

const STORAGE_KEY = 'geminiApiKey';
const KEY_PATTERN = /^[A-Za-z0-9_\-]{30,100}$/;

let keyWhenStorageUnavailable = '';
let pendingDialogResolvers   = [];
let promptedOnThisPage       = false;

export function getGeminiKey() {
  try {
    return localStorage.getItem(STORAGE_KEY) || keyWhenStorageUnavailable;
  } catch (exc) {
    console.error('[gemini_key] localStorage read failed:', exc);
    return keyWhenStorageUnavailable;
  }
}

function storeGeminiKey(key) {
  keyWhenStorageUnavailable = key;
  try {
    localStorage.setItem(STORAGE_KEY, key);
  } catch (exc) {
    console.error('[gemini_key] localStorage write failed, key kept for this page only:', exc);
  }
}

export function clearGeminiKey() {
  keyWhenStorageUnavailable = '';
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (exc) {
    console.error('[gemini_key] localStorage remove failed:', exc);
  }
  refreshGeminiKeySettingsStatus();
}

function dialogElement(id) {
  return document.getElementById(id);
}

function showDialogError(message) {
  const errorEl = dialogElement('gemini-key-error');
  errorEl.textContent = message || '';
  errorEl.classList.toggle('hidden', !message);
}

export function openGeminiKeyDialog(errorMessage = '') {
  dialogElement('gemini-key-input').value = '';
  showDialogError(errorMessage);
  dialogElement('gemini-key-overlay').classList.add('open');
  dialogElement('gemini-key-input').focus();
}

function closeGeminiKeyDialog(savedKey) {
  dialogElement('gemini-key-overlay').classList.remove('open');
  const resolvers = pendingDialogResolvers;
  pendingDialogResolvers = [];
  resolvers.forEach(resolve => resolve(savedKey));
}

function saveGeminiKeyFromDialog() {
  const key = dialogElement('gemini-key-input').value.trim();
  if (!KEY_PATTERN.test(key)) {
    showDialogError('המפתח לא נראה תקין — העתיקו אותו במלואו מ-Google AI Studio.');
    return;
  }
  storeGeminiKey(key);
  refreshGeminiKeySettingsStatus();
  closeGeminiKeyDialog(key);
}

function cancelGeminiKeyDialog() {
  closeGeminiKeyDialog(null);
}

/** Resolves to the stored key, or asks for one; null when the visitor dismisses the dialog. */
export function requireGeminiKey() {
  const key = getGeminiKey();
  if (key) return Promise.resolve(key);
  return new Promise(resolve => {
    pendingDialogResolvers.push(resolve);
    openGeminiKeyDialog();
  });
}

export function promptGeminiKeyIfMissing() {
  if (promptedOnThisPage || getGeminiKey()) return;
  promptedOnThisPage = true;
  openGeminiKeyDialog();
}

export function refreshGeminiKeySettingsStatus() {
  const statusEl = dialogElement('gemini-key-status');
  if (statusEl) statusEl.textContent = getGeminiKey() ? 'שמור בדפדפן הזה בלבד' : 'לא הוגדר';
}

export function geminiKeyHeaders() {
  return { [GEMINI_KEY_HEADER]: getGeminiKey() };
}

/**
 * Error to show for a rejected agent request (null when res is OK). A 401 means the key is
 * missing or rejected: the stored key is dropped and the key dialog reopens.
 */
export async function agentResponseError(res) {
  if (res.ok) return null;
  const body = await res.json().catch(exc => {
    console.error('[gemini_key] unreadable error body:', exc);
    return {};
  });
  if (res.status === 401) {
    clearGeminiKey();
    openGeminiKeyDialog(body.error === 'gemini_key_invalid'
      ? 'המפתח נדחה על ידי Google.'
      : 'נדרש מפתח Gemini תקין כדי להריץ מחקר אוטומטי.');
    return new Error(body.message || 'נדרש מפתח Gemini');
  }
  if (res.status === 503) {
    return new Error(body.error === 'gemini_key_unverified'
      ? 'לא ניתן לאמת את מפתח ה-Gemini כרגע — נסו שוב בעוד רגע'
      : (body.message || 'השרת עמוס כרגע — נסו שוב בעוד כמה דקות'));
  }
  if (res.status === 429) return new Error('יותר מדי בקשות — נסו שוב בעוד דקה');
  return new Error(body.error || body.message || ('HTTP ' + res.status));
}

function replaceGeminiKeyFromSettings() {
  openGeminiKeyDialog();
}

function deleteGeminiKeyFromSettings() {
  clearGeminiKey();
}

window.saveGeminiKeyFromDialog     = saveGeminiKeyFromDialog;
window.cancelGeminiKeyDialog       = cancelGeminiKeyDialog;
window.replaceGeminiKeyFromSettings = replaceGeminiKeyFromSettings;
window.deleteGeminiKeyFromSettings = deleteGeminiKeyFromSettings;
window.promptGeminiKeyIfMissing    = promptGeminiKeyIfMissing;
window.requireGeminiKey            = requireGeminiKey;
window.geminiKeyHeaders            = geminiKeyHeaders;
window.agentResponseError          = agentResponseError;
