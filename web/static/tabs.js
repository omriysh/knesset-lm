/**
 * tabs.js — Tab switching + reading tab browse search
 *
 * Depends on browser.js (openProtocolBrowser) being loaded first.
 */

/* ── Tab switching ───────────────────────────────────────────────── */
function switchTab(name) {
  // Hide all panels
  document.querySelectorAll('.app-tab-panel').forEach(p => {
    p.classList.add('hidden');
  });

  // Deactivate desktop tab buttons
  document.querySelectorAll('.app-tab-btn').forEach(b => {
    b.classList.remove('active');
  });

  // Deactivate mobile tab buttons
  document.querySelectorAll('.mobile-tab-btn').forEach(b => {
    b.classList.remove('active');
  });

  // Show the target panel
  const panel = document.getElementById(`tab-${name}`);
  if (panel) panel.classList.remove('hidden');

  // Activate desktop button
  const dtBtn = document.getElementById(`dt-tab-${name}`);
  if (dtBtn) dtBtn.classList.add('active');

  // Activate mobile button
  const mobBtn = document.getElementById(`mob-tab-${name}`);
  if (mobBtn) mobBtn.classList.add('active');
}

/* ── Browse search ───────────────────────────────────────────────── */
async function browseSearch() {
  const input = document.getElementById('reading-search-input');
  const btn   = document.getElementById('reading-search-btn');
  if (!input || !btn) return;

  const query = input.value.trim();
  if (!query) {
    input.focus();
    return;
  }

  // Validate (mirrors the server-side check)
  if (typeof _validateQuestion === 'function') {
    const err = _validateQuestion(query);
    if (err) {
      _showBrowseError(err);
      return;
    }
  }

  _setBrowseLoading(true);

  try {
    const res = await fetch('/api/browse/rag', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ query }),
    });
    const data = await res.json();

    if (data.error) {
      _showBrowseError(data.error);
      return;
    }

    if (!data.meetings || !data.meetings.length) {
      _showBrowsePlaceholder(
        'לא נמצאו ישיבות',
        'נסה מילות חיפוש אחרות או שינוי ניסוח השאילתה.',
        'search_off',
      );
      return;
    }

    // Clear placeholder / previous browser panel
    const area = document.getElementById('reading-browser-area');
    area.innerHTML = '';

    // Open browser panel inside reading area
    openProtocolBrowser(
      data.session_id,
      data.meetings[0].meeting_id,
      data.meetings,
      {
        originalQuestion: query,
        container:        area,
        standalone:       true,
        postCompletion:   true,
      }
    );

  } catch (err) {
    _showBrowseError('שגיאת רשת: ' + err.message);
  } finally {
    _setBrowseLoading(false);
  }
}

/* ── Helpers ─────────────────────────────────────────────────────── */
function _setBrowseLoading(on) {
  const btn = document.getElementById('reading-search-btn');
  if (!btn) return;
  btn.disabled = on;
  btn.textContent = on ? 'מחפש…' : 'חפש';
}

function _showBrowseError(msg) {
  const area = document.getElementById('reading-browser-area');
  if (!area) return;
  // Keep placeholder DOM but show error banner inside
  let banner = document.getElementById('browse-error-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'browse-error-banner';
    banner.className = 'browse-error-banner';
    area.prepend(banner);
  }
  banner.textContent = msg;
  setTimeout(() => banner.remove(), 5000);
}

function _showBrowsePlaceholder(title, subtitle, icon) {
  const area = document.getElementById('reading-browser-area');
  if (!area) return;
  area.innerHTML = `
<div class="flex flex-col items-center justify-center h-full gap-4 text-center px-6">
  <div class="w-16 h-16 rounded-full bg-surface-container-high flex items-center justify-center">
    <span class="material-symbols-outlined text-on-surface-variant" style="font-size:32px">${icon}</span>
  </div>
  <div>
    <div class="text-lg font-bold text-on-surface mb-1">${title}</div>
    <div class="text-sm text-on-surface-variant max-w-xs leading-relaxed">${subtitle}</div>
  </div>
</div>`;
}
