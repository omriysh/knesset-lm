/**
 * actions.js — event delegation for the page's controls. The CSP forbids inline handlers
 * (onclick=…), so markup names an action instead: data-click / data-input / data-change / data-enter
 * (Enter key) = a key of PAGE_ACTIONS, with its argument in data-arg or other data-* attributes.
 * Only the actions listed here can run; sanitized markdown never carries data-* attributes.
 *
 * Non-module script, loaded after browser.js / tabs.js / filters.js (whose functions it calls) and
 * before app.js; app.js and gemini_key.js expose their functions on window.
 */
const PAGE_ACTIONS = {
  switchTab:                    (el) => switchTab(el.dataset.arg),
  openHelp:                     () => window.openHelp(),
  closeHelp:                    () => window.closeHelp(),
  closeHelpOnBackdrop:          (el, event) => { if (event.target === el) window.closeHelp(); },
  openSettings:                 () => window.openSettings(),
  closeSettings:                () => window.closeSettings(),
  closeSettingsOnBackdrop:      (el, event) => { if (event.target === el) window.closeSettings(); },
  onStagesAlwaysToggle:         (el) => window.onStagesAlwaysToggle(el),
  replaceGeminiKeyFromSettings: () => window.replaceGeminiKeyFromSettings(),
  deleteGeminiKeyFromSettings:  () => window.deleteGeminiKeyFromSettings(),
  saveGeminiKeyFromDialog:      () => window.saveGeminiKeyFromDialog(),
  cancelGeminiKeyDialog:        () => window.cancelGeminiKeyDialog(),
  cancelGeminiKeyOnBackdrop:    (el, event) => { if (event.target === el) window.cancelGeminiKeyDialog(); },
  rfbExpand:                    () => rfbExpand(),
  rfbCollapse:                  () => rfbCollapse(),
  browseSearch:                 () => browseSearch(),
  rfToggle:                     (el) => rfToggle(el.dataset.arg),
  rfFilterList:                 (el) => rfFilterList(el, el.dataset.arg),
  rfSetGuest:                   (el) => rfSetGuest(el.value),
  rfSetDate:                    () => rfSetDate(),
  rfApplyDate:                  () => rfApplyDate(),
  rfClearAll:                   () => rfClearAll(),
  rfToggleItem:                 (el) => rfToggleItem(el.dataset.type, el.dataset.value),
  rfRemoveFilter:               (el) => rfRemoveFilter(el.dataset.type, el.dataset.value),
  closeProtocolBrowser:         () => closeProtocolBrowser(),
  browserToggleSidebar:         () => browserToggleSidebar(),
  browserToggleSummary:         () => browserToggleSummary(),
  browserSetSort:               (el) => browserSetSort(el.value),
  browserToggleGroup:           () => browserToggleGroup(),
  browserFilterParticipant:     (el) => browserFilterParticipant(el.value),
  browserLoadMore:              () => browserLoadMore(),
  browserToggleCommGroup:       (el) => browserToggleCommGroup(el.dataset.committee),
  browserSwitchMeeting:         (el) => browserSwitchMeeting(el.dataset.meetingId),
  openProtocolFromCitation:     (el) => openProtocolFromCitationButton(el),
};

function runDeclaredAction(event, actionAttribute) {
  const target = event.target instanceof Element ? event.target : null;
  const el = target && target.closest(`[${actionAttribute}]`);
  if (!el) return;
  const actionName = el.getAttribute(actionAttribute);
  if (Object.hasOwn(PAGE_ACTIONS, actionName)) PAGE_ACTIONS[actionName](el, event);
  else console.warn('[actions] unknown action:', actionName);
}

document.addEventListener('click',  (event) => runDeclaredAction(event, 'data-click'));
document.addEventListener('input',  (event) => runDeclaredAction(event, 'data-input'));
document.addEventListener('change', (event) => runDeclaredAction(event, 'data-change'));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') runDeclaredAction(event, 'data-enter');
});

document.addEventListener('error', (event) => {
  const el = event.target;
  if (el instanceof HTMLImageElement && el.dataset.hideOnError !== undefined) el.style.display = 'none';
}, true);
