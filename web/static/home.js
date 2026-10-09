/**
 * home.js — the "ראשי" tab (/): a choice card that draws an edge to the card of the chosen way in.
 *
 * Non-module script, loaded before actions.js.
 */

const HOME_FOLD_MS = 280;
let _homeFoldTimer = null;

function homePick(option) {
  const graph = document.getElementById('home-graph');
  if (graph.dataset.option === option) return;
  graph.dataset.option = option;
  graph.querySelectorAll('.home-choice').forEach(b => b.classList.toggle('active', b.dataset.arg === option));
  clearTimeout(_homeFoldTimer);
  if (!graph.classList.contains('home-graph--open')) {
    _homeShowOption(graph, option);
    graph.classList.add('home-graph--open');
    return;
  }
  graph.classList.add('home-graph--folding');
  _homeFoldTimer = setTimeout(() => {
    _homeShowOption(graph, option);
    graph.classList.remove('home-graph--folding');
  }, HOME_FOLD_MS);
}

/* The cards are laid out in their hidden pose first, so they transition in instead of popping in. */
function _homeShowOption(graph, option) {
  graph.querySelectorAll('.home-next-card').forEach(card => { card.hidden = card.dataset.option !== option; });
  void graph.offsetWidth;
}
