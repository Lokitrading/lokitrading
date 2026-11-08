/* FIRST-VISIT NICKNAME — "What should Loki call you?"
 *
 * Asked once, the first time someone opens the workspace. The name is kept in
 * this browser only (localStorage); it is never sent to an account. Talk to
 * Loki receives it with each question so replies can greet the visitor by
 * name. A signed-in member's account nickname still takes precedence in the
 * hero greeting (loki_auth.js).
 */
'use strict';
(() => {
  const KEY = 'loki_visitor_name_v1';
  const VALID = /^[\p{L}\p{N}][\p{L}\p{N} .'_-]{0,23}$/u;

  const read = () => {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (raw && typeof raw.name === 'string' && VALID.test(raw.name.trim())) return { name: raw.name.trim(), skipped: false };
      if (raw && raw.skipped === true) return { name: '', skipped: true };
    } catch { /* storage blocked or corrupt: treat as first visit */ }
    return null;
  };
  const write = (value) => { try { localStorage.setItem(KEY, JSON.stringify({ ...value, at: new Date().toISOString() })); } catch { /* private mode */ } };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 24);

  window.LokiVisitor = {
    name: () => (read() || {}).name || '',
    set(name) {
      const next = clean(name);
      if (!VALID.test(next)) return false;
      write({ name: next });
      greet();
      document.dispatchEvent(new CustomEvent('loki:name', { detail: { name: next } }));
      return true;
    },
  };

  function partOfDay() {
    const h = new Date().getHours();
    return h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  }

  function greet() {
    const el = document.getElementById('hero-greeting');
    if (!el || el.dataset.member === '1') return; // a signed-in member's greeting wins
    const name = window.LokiVisitor.name();
    if (!name) { el.hidden = true; return; }
    el.textContent = '';
    const text = document.createElement('span');
    text.textContent = `${partOfDay()}, ${name}.`;
    const change = document.createElement('button');
    change.type = 'button';
    change.className = 'hero-name-change';
    change.textContent = 'Not you?';
    change.setAttribute('aria-label', `Change the name Loki calls you (currently ${name})`);
    change.addEventListener('click', () => ask(true));
    el.append(text, ' ', change);
    el.hidden = false;
  }

  function ask(changing) {
    if (document.getElementById('welcome-name')) return;
    const previouslyFocused = document.activeElement;
    const wrap = document.createElement('div');
    wrap.id = 'welcome-name';
    wrap.className = 'welcome-name';
    wrap.innerHTML =
      '<div class="welcome-name-card" role="dialog" aria-modal="true" aria-labelledby="welcome-name-title" aria-describedby="welcome-name-desc">' +
      '<p class="eyebrow">WELCOME TO LOKI</p>' +
      '<h2 id="welcome-name-title">What should Loki call you?</h2>' +
      '<p class="muted" id="welcome-name-desc">A first name or nickname is perfect. It stays in this browser only — it isn\'t sent to any account — and you can change it any time.</p>' +
      '<form id="welcome-name-form" novalidate>' +
      '<label class="sr-only" for="welcome-name-input">Your nickname</label>' +
      '<input id="welcome-name-input" maxlength="24" autocomplete="nickname" placeholder="e.g. Josh" spellcheck="false">' +
      '<p class="form-message" id="welcome-name-error" role="alert"></p>' +
      '<div class="button-row"><button class="button primary" type="submit">Let\'s go →</button>' +
      '<button class="button secondary" type="button" id="welcome-name-skip">' + (changing ? 'Cancel' : 'Skip for now') + '</button></div>' +
      '</form></div>';
    document.body.appendChild(wrap);
    const input = wrap.querySelector('#welcome-name-input');
    input.value = window.LokiVisitor.name();
    const close = () => { wrap.remove(); if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus(); };
    const skip = () => { if (!changing && !window.LokiVisitor.name()) write({ skipped: true }); close(); };
    wrap.querySelector('#welcome-name-form').addEventListener('submit', (event) => {
      event.preventDefault();
      if (window.LokiVisitor.set(input.value)) close();
      else wrap.querySelector('#welcome-name-error').textContent = 'Use 1–24 letters or numbers (spaces, . \' _ - are fine).';
    });
    wrap.querySelector('#welcome-name-skip').addEventListener('click', skip);
    wrap.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.preventDefault(); skip(); }
      if (event.key === 'Tab') { // keep focus inside the dialog
        const items = [...wrap.querySelectorAll('input, button')];
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    setTimeout(() => input.focus(), 30);
  }

  greet();
  if (!read() && document.getElementById('hero-greeting')) {
    // Let the arrival animation breathe before asking.
    setTimeout(() => { if (!read()) ask(false); }, 900);
  }
})();
