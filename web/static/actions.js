/**
 * actions.js — event delegation for the page's controls. The CSP forbids inline handlers
 * (onclick=…), so markup names an action instead: data-click / data-input / data-change / data-enter
 * (Enter key) = a key of PAGE_ACTIONS, with its argument in data-arg or other data-* attributes.
 * Only the actions listed here can run; sanitized markdown never carries data-* attributes.
 *
 * Non-module script, loaded after url_state.js / landing.js / home.js / browser.js / profiles.js / game.js / tabs.js / filters.js (whose functions it calls) and
 * before app.js; app.js and gemini_key.js expose their functions on window.
 */
const PAGE_ACTIONS = {
  switchTab:                    (el) => { if (el.dataset.arg === 'profiles' && _activeTab === 'profiles') profilesGo(PROFILES_PATH); else switchTab(el.dataset.arg); },
  landingOpenTab:               (el, event) => { event.preventDefault(); switchTab(el.dataset.arg); },
  homePick:                     (el) => homePick(el.dataset.arg),
  gameStart:                    () => gameStart(),
  gameVote:                     (el) => gameVote(el),
  gameResults:                  () => gameResults(),
  gameContinue:                 () => gameContinue(),
  gameHome:                     () => gameHome(),
  gameSlice:                    (el) => gameSlice(el),
  gameTheme:                    (el) => gameTheme(el),
  gameCloseTheme:               () => gameCloseTheme(),
  gameThemeBackdrop:            (el, event) => { if (event.target === el) gameCloseTheme(); },
  gameOpenProfile:              (el, event) => gameOpenProfile(el, event),
  landingScrollToSection:       (el, event) => landingScrollToSection(event, el.getAttribute('href').slice(1)),
  landingStepNext:              (el) => landingStepNext(el),
  landingCopyField:             (el) => landingCopyField(el),
  landingCopyReview:            (el) => landingCopyReview(el),
  landingCopyGuidelines:        (el) => landingCopyGuidelines(el),
  landingBuilderSet:            (el) => landingBuilderSet(el),
  landingSetInstallTab:         (el) => landingSetInstallTab(el),
  openHelp:                     () => window.openHelp(),
  closeHelp:                    () => window.closeHelp(),
  helpSetTab:                   (el) => window.helpSetTab(el.dataset.arg),
  closeHelpOnBackdrop:          (el, event) => { if (event.target === el) window.closeHelp(); },
  openSettings:                 () => window.openSettings(),
  closeSettings:                () => window.closeSettings(),
  closeSettingsOnBackdrop:      (el, event) => { if (event.target === el) window.closeSettings(); },
  onStagesAlwaysToggle:         (el) => window.onStagesAlwaysToggle(el),
  onShareSearchToggle:          (el) => setShareIncludesSearch(el.checked),
  onResearchTabToggle:          (el) => setResearchTabShown(el.checked),
  replaceGeminiKeyFromSettings: () => window.replaceGeminiKeyFromSettings(),
  deleteGeminiKeyFromSettings:  () => window.deleteGeminiKeyFromSettings(),
  setResearchModel:             (el) => window.setResearchModel(el.dataset.role, el.value),
  resetResearchModels:          () => window.resetResearchModels(),
  saveGeminiKeyFromDialog:      () => window.saveGeminiKeyFromDialog(),
  cancelGeminiKeyDialog:        () => window.cancelGeminiKeyDialog(),
  cancelGeminiKeyOnBackdrop:    (el, event) => { if (event.target === el) window.cancelGeminiKeyDialog(); },
  saveInlineGeminiKey:          () => window.saveInlineGeminiKey(),
  useResearchExample:           (el) => window.useResearchExample(el),
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
  rfRemoveFilterAndSearch:      (el) => { rfRemoveFilter(el.dataset.type, el.dataset.value); browseSearch(); },
  rfClearQueryAndSearch:        () => rfClearQueryAndSearch(),
  closeProtocolBrowser:         () => closeProtocolBrowser(),
  browserToggleSidebar:         () => browserToggleSidebar(),
  browserToggleSummary:         () => browserToggleSummary(),
  browserSetSort:               (el) => browserSetSort(el.dataset.arg),
  browserSetGroup:              (el) => browserSetGroup(el.dataset.arg),
  browserLoadMore:              () => browserLoadMore(),
  browserNav:                   (el) => browserNav(el.dataset.arg),
  browserToggleCommGroup:       (el) => browserToggleCommGroup(el.dataset.committee),
  browserSwitchMeeting:         (el) => browserSwitchMeeting(el.dataset.meetingId),
  openProtocolFromCitation:     (el) => openProtocolFromCitationButton(el),
  browserShareMeeting:          (el) => browserShareMeeting(el),
  browserShareSpeech:           (el) => browserShareSpeech(el),
  browserShareQuote:            (el) => browserShareQuote(el),
  profilesNav:                  (el, event) => profilesNav(el, event),
  profilesFilterParties:        (el) => profilesFilterParties(el),
  profilesFilterCandidates:     (el) => profilesFilterCandidates(el),
  profilesTab:                  (el) => profilesTab(el),
  profilesThemeToggle:          (el) => profilesThemeToggle(el),
  profilesThemeJump:            (el) => profilesThemeJump(el),
  profilesThemeSheet:           (el) => profilesThemeSheet(el),
  profilesSheetQuarter:         (el) => profilesSheetQuarter(el),
  profilesSheetMore:            () => profilesSheetMore(),
  profilesCloseSheet:           () => profilesCloseSheet(),
  profilesOpinionDates:         () => profilesOpinionDates(),
  profilesThemesAll:            (el) => profilesThemesAll(el),
  profilesCite:                 (el, event) => profilesCite(el, event),
  profilesOpinionSearch:        (el) => profilesOpinionSearch(el),
  profilesOpinionTheme:         (el) => profilesOpinionTheme(el),
  profilesOpinionGroup:         (el) => profilesOpinionGroup(el),
  profilesOpinionMore:          () => profilesOpinionMore(),
  profilesOpinionPills:         () => profilesOpinionPills(),
  profilesVoteSearch:           (el) => profilesVoteSearch(el),
  profilesVoteFilter:           (el) => profilesVoteFilter(el),
  profilesVoteMore:             () => profilesVoteMore(),
  profilesBillSearch:           (el) => profilesBillSearch(el),
  profilesBillRole:             (el) => profilesBillRole(el),
  profilesBillStage:            (el) => profilesBillStage(el),
  profilesBillMore:             () => profilesBillMore(),
  profilesBillPeek:             (el) => profilesBillPeek(el),
  profilesBillMoreText:         () => profilesBillMoreText(),
  profilesCloseBill:            () => profilesCloseBill(),
  profilesNewcomer:             (el) => profilesNewcomer(el),
  profilesCopyNewcomersPrompt:  (el) => profilesCopyNewcomersPrompt(el),
  profilesCloseNewcomer:        () => profilesCloseNewcomer(),
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
