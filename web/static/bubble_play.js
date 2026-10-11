/**
 * bubble_play.js — a bubble playground: circles in a box that drift to its centre, push each other apart and
 * can be dragged and flung with the mouse or a finger. A tap without a drag calls the playground's onTap.
 * Used by the simple profile's interest bubbles (profile_simple.js) and the subjects tab (subjects.js).
 *
 * Non-module script, loaded before profile_simple.js and subjects.js.
 */

const PLAY_FILL = 0.48;
const PLAY_GAP_PX = 5;
const PLAY_DAMPING = 0.9;
const PLAY_PULL = 0.0016;
const PLAY_TAP_PX = 6;
const PLAY_GROW = 0.12;
const PLAY_SLEEP_SPEED = 0.03;
const PLAY_CLICK_AFTER_TAP_MS = 500;

const _playBodyByElement = new WeakMap();
let _playDrag = null;

function bubblePlayCreate(box, elements, { onTap, minRadius = 0 }) {
  const centerX = box.clientWidth / 2, centerY = box.clientHeight / 2;
  const play = { box, onTap, minRadius, frame: 0, still: 0, width: 0, height: 0 };
  play.bodies = elements.map((el, index) => {
    const angle = index * 2.4, spread = 12 + index * 6;
    const body = { play, index, el, x: centerX + Math.cos(angle) * spread, y: centerY + Math.sin(angle) * spread, vx: 0, vy: 0, r: 0, target: 0, size: 0 };
    _playBodyByElement.set(el, body);
    return body;
  });
  return play;
}

/* Sizes the bubbles by value (area ∝ value, a total of scaleTotal fills PLAY_FILL of the box) and returns their
   radii; a zero value hides its bubble. The bubbles grow or shrink to the new size. */
function bubblePlaySet(play, values, scaleTotal = values.reduce((sum, value) => sum + value, 0)) {
  play.width = play.box.clientWidth;
  play.height = play.box.clientHeight;
  const scale = Math.sqrt((PLAY_FILL * play.width * play.height) / (Math.PI * Math.max(1, scaleTotal)));
  play.bodies.forEach((body, i) => {
    body.target = values[i] ? Math.max(play.minRadius, scale * Math.sqrt(values[i])) : 0;
    if (!body.target) return;
    body.size = body.target;
    Object.assign(body.el.style, { width: `${body.size * 2}px`, height: `${body.size * 2}px` });
  });
  _bubblePlayWake(play);
  return play.bodies.map(body => body.target);
}

function _bubblePlayWake(play) {
  if (!play.frame) play.frame = requestAnimationFrame(() => _bubblePlayStep(play));
}

function _bubblePlayStep(play) {
  if (!play.box.isConnected) { play.frame = 0; return; }
  const { bodies, width, height } = play;
  const held = body => _playDrag && _playDrag.body === body;
  let growing = false;
  bodies.forEach(body => {
    body.r += (body.target - body.r) * PLAY_GROW;
    if (Math.abs(body.target - body.r) < 0.2) body.r = body.target; else growing = true;
    if (held(body)) return;
    body.vx += (width / 2 - body.x) * PLAY_PULL;
    body.vy += (height / 2 - body.y) * PLAY_PULL;
  });
  const solid = bodies.filter(body => body.r > 0.5);
  for (let pass = 0; pass < 3; pass++) {
    for (let i = 0; i < solid.length; i++) {
      for (let j = i + 1; j < solid.length; j++) {
        const a = solid[i], b = solid[j];
        const dx = b.x - a.x, dy = b.y - a.y, distance = Math.hypot(dx, dy) || 0.01, overlap = a.r + b.r + PLAY_GAP_PX - distance;
        if (overlap <= 0) continue;
        const nx = dx / distance, ny = dy / distance;
        const share = held(a) ? 0 : held(b) ? 1 : (b.r * b.r) / (a.r * a.r + b.r * b.r);
        a.x -= nx * overlap * share; a.y -= ny * overlap * share;
        b.x += nx * overlap * (1 - share); b.y += ny * overlap * (1 - share);
        const push = overlap * 0.08;
        a.vx -= nx * push * share; a.vy -= ny * push * share;
        b.vx += nx * push * (1 - share); b.vy += ny * push * (1 - share);
      }
    }
  }
  let fastest = 0;
  bodies.forEach(body => {
    if (!held(body)) {
      body.vx *= PLAY_DAMPING; body.vy *= PLAY_DAMPING;
      body.x += body.vx; body.y += body.vy;
      if (body.x < body.r) { body.x = body.r; body.vx = Math.abs(body.vx) * 0.6; }
      if (body.x > width - body.r) { body.x = width - body.r; body.vx = -Math.abs(body.vx) * 0.6; }
      if (body.y < body.r) { body.y = body.r; body.vy = Math.abs(body.vy) * 0.6; }
      if (body.y > height - body.r) { body.y = height - body.r; body.vy = -Math.abs(body.vy) * 0.6; }
      fastest = Math.max(fastest, Math.hypot(body.vx, body.vy));
    }
    const visible = body.r > 0.5;
    body.el.style.transform = `translate(${body.x - body.size}px, ${body.y - body.size}px) scale(${body.size ? body.r / body.size : 0})`;
    body.el.style.opacity = visible ? 1 : 0;
    body.el.style.pointerEvents = visible ? '' : 'none';
  });
  play.still = fastest < PLAY_SLEEP_SPEED && !(_playDrag && _playDrag.body.play === play) && !growing ? play.still + 1 : 0;
  play.frame = play.still > 30 ? 0 : requestAnimationFrame(() => _bubblePlayStep(play));
}

function _bubblePlayPoint(play, event) {
  const rect = play.box.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top, t: performance.now() };
}

document.addEventListener('pointerdown', event => {
  let el = event.target instanceof Element ? event.target : null;
  while (el && !_playBodyByElement.has(el)) el = el.parentElement;
  if (!el) return;
  const body = _playBodyByElement.get(el);
  const point = _bubblePlayPoint(body.play, event);
  _playDrag = { body, start: point, last: point, dx: point.x - body.x, dy: point.y - body.y, moved: false, pointerId: event.pointerId };
  el.setPointerCapture(event.pointerId);
  el.classList.add('held');
  _bubblePlayWake(body.play);
});

document.addEventListener('pointermove', event => {
  const drag = _playDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const point = _bubblePlayPoint(drag.body.play, event);
  if (Math.hypot(point.x - drag.start.x, point.y - drag.start.y) > PLAY_TAP_PX) drag.moved = true;
  const elapsed = Math.max(1, point.t - drag.last.t);
  drag.body.vx = ((point.x - drag.last.x) / elapsed) * 16;
  drag.body.vy = ((point.y - drag.last.y) / elapsed) * 16;
  drag.body.x = point.x - drag.dx;
  drag.body.y = point.y - drag.dy;
  drag.last = point;
  _bubblePlayWake(drag.body.play);
});

function _bubblePlayRelease(event) {
  const drag = _playDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  _playDrag = null;
  drag.body.el.classList.remove('held');
  _bubblePlayWake(drag.body.play);
  if (drag.moved || event.type === 'pointercancel') return;
  _bubblePlaySwallowNextClick();
  drag.body.play.onTap(drag.body.index);
}

/* The click that follows a tap lands on whatever onTap rendered under the finger; it must not press that too. */
function _bubblePlaySwallowNextClick() {
  const swallow = event => { event.preventDefault(); event.stopPropagation(); };
  document.addEventListener('click', swallow, { capture: true, once: true });
  setTimeout(() => document.removeEventListener('click', swallow, { capture: true }), PLAY_CLICK_AFTER_TAP_MS);
}
document.addEventListener('pointerup', _bubblePlayRelease);
document.addEventListener('pointercancel', _bubblePlayRelease);
