/**
 * home.js — the "ראשי" tab (/): a choice card that draws an edge to the card of the chosen way in.
 *
 * Non-module script, loaded before actions.js.
 */

function homePick(option) {
  const graph = document.getElementById('home-graph');
  graph.querySelectorAll('.home-choice').forEach(b => b.classList.toggle('active', b.dataset.arg === option));
  graph.querySelectorAll('.home-next-card').forEach(o => { o.hidden = o.dataset.option !== option; });
  graph.classList.toggle('home-graph--swapped', graph.classList.contains('home-graph--open'));
  graph.classList.add('home-graph--open');
}
