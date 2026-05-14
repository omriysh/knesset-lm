/**
 * filters.js — Reading tab filter bar state + interactions
 *
 * Globals used by inline HTML handlers:
 *   rfToggle, rfFilterList, rfToggleItem, rfSetGuest,
 *   rfSetDate, rfApplyDate, rfClearAll, rfRemoveFilter, rfGetFilters
 */

/* ── Dropdown data (loaded from /api/meta on init) ──────────────── */
let _RF_COMMITTEES = [];
let _RF_MKS        = [];
let _RF_PARTIES    = [];

/* ── Filter state ───────────────────────────────────────────────── */
const _rfState = {
  committees: new Set(),
  mks:        new Set(),
  parties:    new Set(),
  guest:      '',
  dateFrom:   '',
  dateTo:     '',
};

let _rfOpen = null;

/* ── Init ───────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  _rfSetListLoading('rf-committee-list');
  _rfSetListLoading('rf-mk-list');
  _rfSetListLoading('rf-party-list');

  _rfLoadMeta();

  // Portal: move all dropdowns to <body> so overflow-x:auto on rfb-filter-row
  // can't clip them (fixed children position relative to viewport, not parent).
  const portal = document.createElement('div');
  portal.id = 'rfb-portal';
  document.body.appendChild(portal);
  document.querySelectorAll('.rfb-dropdown').forEach(d => portal.appendChild(d));

  document.addEventListener('click', e => {
    if (_rfOpen &&
        !e.target.closest('.rfb-filter-item') &&
        !e.target.closest('.rfb-dropdown')) {
      _rfClose(_rfOpen);
    }
  });

  // Close on scroll outside the dropdown (capture catches rfb-filter-row too)
  document.addEventListener('scroll', e => {
    if (_rfOpen && !e.target.closest?.('.rfb-dropdown')) _rfClose(_rfOpen);
  }, { passive: true, capture: true });
});

async function _rfLoadMeta() {
  try {
    const res  = await fetch('/api/meta');
    const data = await res.json();
    _RF_COMMITTEES = data.committees || [];
    _RF_MKS        = data.mks        || [];
    _RF_PARTIES    = data.parties    || [];
    _rfRenderList('rf-committee-list', _RF_COMMITTEES, 'committee');
    _rfRenderList('rf-mk-list',        _RF_MKS,        'mk');
    _rfRenderList('rf-party-list',     _RF_PARTIES,    'party');
  } catch (exc) {
    console.error('[filters] meta fetch failed:', exc);
    _rfSetListError('rf-committee-list');
    _rfSetListError('rf-mk-list');
    _rfSetListError('rf-party-list');
  }
}

function _rfSetListLoading(listId) {
  const el = document.getElementById(listId);
  if (el) el.innerHTML = '<div class="rfb-list-status">טוען…</div>';
}

function _rfSetListError(listId) {
  const el = document.getElementById(listId);
  if (el) el.innerHTML = '<div class="rfb-list-status rfb-list-status--error">שגיאה בטעינה</div>';
}

/* ── Dropdown open / close ──────────────────────────────────────── */
function rfToggle(type) {
  if (_rfOpen === type) { _rfClose(type); return; }
  if (_rfOpen) _rfClose(_rfOpen);
  _rfOpenDrop(type);
}

function _rfOpenDrop(type) {
  const drop = document.getElementById(`rfdrop-${type}`);
  if (!drop) return;
  drop.classList.remove('hidden');

  // Position below the trigger button via viewport coordinates
  const btn = document.getElementById(`fitem-${type}`)?.querySelector('.rfb-filter-btn');
  if (btn) {
    const r        = btn.getBoundingClientRect();
    const dropMinW = 230;
    const margin   = 6;
    drop.style.top = (r.bottom + margin) + 'px';
    if (r.right >= dropMinW + margin) {
      // Enough room to the left: right-anchor with button's right edge
      drop.style.right = Math.max(margin, window.innerWidth - r.right) + 'px';
      drop.style.left  = 'auto';
    } else {
      // Near left edge: left-anchor, clamped so it doesn't fall off right
      drop.style.left  = Math.max(margin, Math.min(r.left, window.innerWidth - dropMinW - margin)) + 'px';
      drop.style.right = 'auto';
    }
  }

  const chev = document.getElementById(`rfchev-${type}`);
  if (chev) chev.textContent = 'expand_less';
  document.getElementById(`fitem-${type}`)?.classList.add('rfb-filter-item--open');
  _rfOpen = type;
}

function _rfClose(type) {
  document.getElementById(`rfdrop-${type}`)?.classList.add('hidden');
  const chev = document.getElementById(`rfchev-${type}`);
  if (chev) chev.textContent = 'expand_more';
  document.getElementById(`fitem-${type}`)?.classList.remove('rfb-filter-item--open');
  _rfOpen = null;
}

/* ── Option list rendering ──────────────────────────────────────── */
function _rfRenderList(listId, items, type) {
  const el = document.getElementById(listId);
  if (!el) return;
  const set = _rfSetFor(type);
  el.innerHTML = items.map(item => {
    const sel = set.has(item) ? 'rfb-option--selected' : '';
    const safe = item.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return `<button class="rfb-option ${sel}" onclick="rfToggleItem('${type}','${safe}')" data-value="${item}">
  <span class="rfb-option-check material-symbols-outlined">check</span>${item}</button>`;
  }).join('');
}

function _rfSetFor(type) {
  if (type === 'committee') return _rfState.committees;
  if (type === 'mk')        return _rfState.mks;
  if (type === 'party')     return _rfState.parties;
  return new Set();
}

/* ── Multi-select toggles ───────────────────────────────────────── */
function rfToggleItem(type, value) {
  const set = _rfSetFor(type);
  if (set.has(value)) set.delete(value); else set.add(value);

  document.getElementById(`rf-${type}-list`)?.querySelectorAll('.rfb-option').forEach(btn => {
    btn.classList.toggle('rfb-option--selected', set.has(btn.dataset.value));
  });

  if (type === 'committee') {
    _rfBadge('committee', set.size);
  } else {
    _rfBadge('participants', _rfParticipantCount());
  }
  _rfRenderChips();
}

function rfFilterList(input, listId) {
  const q = input.value.toLowerCase();
  document.getElementById(listId)?.querySelectorAll('.rfb-option').forEach(btn => {
    btn.style.display = btn.dataset.value.toLowerCase().includes(q) ? '' : 'none';
  });
}

/* ── Guest (free text) ──────────────────────────────────────────── */
function rfSetGuest(value) {
  _rfState.guest = value.trim();
  _rfBadge('participants', _rfParticipantCount());
  _rfRenderChips();
}

function _rfParticipantCount() {
  return _rfState.mks.size + _rfState.parties.size + (_rfState.guest ? 1 : 0);
}

/* ── Date range ─────────────────────────────────────────────────── */
function rfSetDate() {
  _rfState.dateFrom = document.getElementById('rf-date-from')?.value || '';
  _rfState.dateTo   = document.getElementById('rf-date-to')?.value   || '';
  _rfBadge('date', (_rfState.dateFrom || _rfState.dateTo) ? 1 : 0);
  _rfRenderChips();
}

function rfApplyDate() {
  rfSetDate();
  _rfClose('date');
}

/* ── Clear all ──────────────────────────────────────────────────── */
function rfClearAll() {
  _rfState.committees.clear();
  _rfState.mks.clear();
  _rfState.parties.clear();
  _rfState.guest    = '';
  _rfState.dateFrom = '';
  _rfState.dateTo   = '';

  ['committee', 'participants', 'date'].forEach(t => _rfBadge(t, 0));

  ['rf-committee-list', 'rf-mk-list', 'rf-party-list'].forEach(id =>
    document.getElementById(id)?.querySelectorAll('.rfb-option').forEach(b =>
      b.classList.remove('rfb-option--selected')
    )
  );
  ['rf-guest-input', 'rf-date-from', 'rf-date-to'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });

  _rfRenderChips();
}

/* ── Remove one chip ────────────────────────────────────────────── */
function rfRemoveFilter(type, value) {
  if (type === 'committee') {
    _rfState.committees.delete(value);
    _rfBadge('committee', _rfState.committees.size);
    _rfDeselectOption('rf-committee-list', value);
  } else if (type === 'mk') {
    _rfState.mks.delete(value);
    _rfBadge('participants', _rfParticipantCount());
    _rfDeselectOption('rf-mk-list', value);
  } else if (type === 'party') {
    _rfState.parties.delete(value);
    _rfBadge('participants', _rfParticipantCount());
    _rfDeselectOption('rf-party-list', value);
  } else if (type === 'guest') {
    _rfState.guest = '';
    _rfBadge('participants', _rfParticipantCount());
    const el = document.getElementById('rf-guest-input'); if (el) el.value = '';
  } else if (type === 'date') {
    _rfState.dateFrom = '';
    _rfState.dateTo   = '';
    _rfBadge('date', 0);
    ['rf-date-from', 'rf-date-to'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  }
  _rfRenderChips();
}

function _rfDeselectOption(listId, value) {
  document.getElementById(listId)?.querySelectorAll('.rfb-option').forEach(btn => {
    if (btn.dataset.value === value) btn.classList.remove('rfb-option--selected');
  });
}

/* ── Badge on filter button ─────────────────────────────────────── */
function _rfBadge(type, count) {
  const btn = document.querySelector(`#fitem-${type} .rfb-filter-btn`);
  if (!btn) return;
  btn.classList.toggle('rfb-filter-btn--active', count > 0);
  let badge = btn.querySelector('.rfb-badge');
  if (count > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'rfb-badge';
      btn.querySelector('.rfb-chevron').before(badge);
    }
    badge.textContent = count;
  } else {
    badge?.remove();
  }
}

/* ── Active filter chips row ────────────────────────────────────── */
function _rfRenderChips() {
  const row      = document.getElementById('rf-active-chips');
  const clearBtn = document.getElementById('rf-clear-btn');
  if (!row) return;

  const chips = [];
  _rfState.committees.forEach(v => chips.push({ label: v,                           type: 'committee', value: v }));
  _rfState.mks.forEach(v        => chips.push({ label: `ח"כ ${v}`,                 type: 'mk',        value: v }));
  _rfState.parties.forEach(v    => chips.push({ label: v,                           type: 'party',     value: v }));
  if (_rfState.guest)              chips.push({ label: `אורח: ${_rfState.guest}`,   type: 'guest',     value: 'guest' });
  if (_rfState.dateFrom || _rfState.dateTo) {
    const parts = [];
    if (_rfState.dateFrom) parts.push(_fmtDate(_rfState.dateFrom));
    if (_rfState.dateTo)   parts.push(_fmtDate(_rfState.dateTo));
    chips.push({ label: parts.join(' — '), type: 'date', value: 'date' });
  }

  const has = chips.length > 0;
  row.classList.toggle('hidden', !has);
  clearBtn?.classList.toggle('hidden', !has);

  if (has) {
    row.innerHTML = chips.map(c => {
      const sv = c.value.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
      return `<span class="rfb-chip">${c.label}<button class="rfb-chip-remove" onclick="rfRemoveFilter('${c.type}','${sv}')" title="הסר"><span class="material-symbols-outlined" style="font-size:13px">close</span></button></span>`;
    }).join('');
  }
}

function _fmtDate(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y}`;
}

/* ── Return current filter state for search API ─────────────────── */
function rfGetFilters() {
  return {
    committees: [..._rfState.committees],
    mks:        [..._rfState.mks],
    parties:    [..._rfState.parties],
    guest:      _rfState.guest  || null,
    date_from:  _rfState.dateFrom || null,
    date_to:    _rfState.dateTo   || null,
  };
}
