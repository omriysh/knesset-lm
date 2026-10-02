/**
 * research_intro.js — the research tab's slot above the input before the first question: the Gemini key card
 * until the visitor has a key, then a few example questions, one of them fading into a new one at a time.
 */
import { getGeminiKey, saveGeminiKey, GEMINI_KEY_CHANGED_EVENT } from './gemini_key.js';
import { queryInput } from './dom.js';
import { RESEARCH_EXAMPLES } from './research_examples.js';

const EXAMPLE_SWAP_MS = 6000;
const EXAMPLE_FADE_MS = 450;
const EXAMPLE_RESIZE_MS = 400;

const keyCard      = document.getElementById('research-key-card');
const examplesEl   = document.getElementById('research-examples');
const examplesList = document.getElementById('research-examples-list');

let shuffledExamples = [];
let nextExample      = 0;
let rotateTimer      = null;
let nextSlotToSwap   = 0;
let rotationPaused   = false;

function examplesPerRound() {
  return window.matchMedia('(max-width: 640px)').matches ? 2 : 3;
}

function takeExample() {
  const shown = new Set([...examplesList.children].map(chip => chip.textContent));
  for (let attempt = 0; attempt <= RESEARCH_EXAMPLES.length; attempt++) {
    if (nextExample >= shuffledExamples.length) {
      shuffledExamples = [...RESEARCH_EXAMPLES].sort(() => Math.random() - 0.5);
      nextExample = 0;
    }
    const example = shuffledExamples[nextExample++];
    if (!shown.has(example)) return example;
  }
  return RESEARCH_EXAMPLES[0];
}

function showExamples() {
  examplesList.innerHTML = '';
  for (let i = 0; i < examplesPerRound(); i++) {
    const chip = document.createElement('button');
    chip.className = 'example-chip';
    chip.dataset.click = 'useResearchExample';
    chip.textContent = takeExample();
    examplesList.appendChild(chip);
  }
  nextSlotToSwap = 0;
}

function swapOneExample() {
  if (rotationPaused || document.hidden || !examplesList.children.length) return;
  const chip = examplesList.children[nextSlotToSwap % examplesList.children.length];
  nextSlotToSwap++;
  chip.classList.add('fading');
  setTimeout(() => {
    const oldWidth = chip.offsetWidth;
    chip.textContent = takeExample();
    const newWidth = chip.offsetWidth;
    chip.style.width = `${oldWidth}px`;
    chip.offsetWidth;
    chip.style.width = `${newWidth}px`;
    setTimeout(() => {
      chip.style.width = '';
      chip.classList.remove('fading');
    }, EXAMPLE_RESIZE_MS);
  }, EXAMPLE_FADE_MS);
}

function renderSlot() {
  const hasKey = !!getGeminiKey();
  keyCard.hidden = hasKey;
  examplesEl.hidden = !hasKey;
  clearInterval(rotateTimer);
  rotateTimer = null;
  if (hasKey) {
    showExamples();
    rotateTimer = setInterval(swapOneExample, EXAMPLE_SWAP_MS);
  }
}

function saveInlineGeminiKey() {
  const input = document.getElementById('inline-key-input');
  const errorEl = document.getElementById('inline-key-error');
  const error = saveGeminiKey(input.value);
  errorEl.textContent = error;
  errorEl.classList.toggle('hidden', !error);
  if (!error) {
    input.value = '';
    queryInput.focus();
  }
}

function useResearchExample(button) {
  queryInput.value = button.textContent;
  queryInput.dispatchEvent(new Event('input'));
  queryInput.focus();
}

examplesEl.addEventListener('mouseenter', () => { rotationPaused = true; });
examplesEl.addEventListener('mouseleave', () => { rotationPaused = false; });
document.addEventListener(GEMINI_KEY_CHANGED_EVENT, renderSlot);

window.saveInlineGeminiKey = saveInlineGeminiKey;
window.useResearchExample  = useResearchExample;

renderSlot();
