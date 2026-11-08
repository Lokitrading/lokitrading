/* AI STRATEGY CO-PILOT OVERLAY — a command bar on every workspace page.
 * Open with the top-bar button or Ctrl/Cmd+J. Type an instruction; Loki shows
 * the exact config diff and JSON before anything changes. */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || !Loki.copilot || document.getElementById('copilot-overlay')) return;
  const F = Loki.fmt;
  const host = document.querySelector('.topbar-status');
  const overlay = document.createElement('div');
  overlay.id = 'copilot-overlay';
  overlay.className = 'cp-overlay';
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-label', 'Strategy co-pilot');
  overlay.innerHTML = `<div class="cp-card">
    <form class="cp-form" autocomplete="off"><span class="cp-mark" aria-hidden="true">✦</span><input id="cp-input" maxlength="300" placeholder="Tell the strategy what to do… e.g. shift trailing stop down by 1.5% and hedge predictions if volatility spikes above 40%" aria-label="Instruction for the strategy co-pilot"><button class="button primary" type="submit">Translate</button></form>
    <div id="cp-out" class="cp-out"></div>
    <div class="cp-foot"><div class="cp-examples">${Loki.copilot.EXAMPLES.map((x) => `<button type="button" class="chip" data-example="${F.esc(x)}">${F.esc(x.length > 46 ? x.slice(0, 44) + '…' : x)}</button>`).join('')}</div>
    <div class="button-row"><button class="button secondary" id="cp-undo" type="button">Undo last change</button><button class="button secondary" id="cp-close" type="button">Close (Esc)</button></div></div>
    <details><summary>Current config</summary><pre id="cp-config"></pre></details></div>`;
  document.body.appendChild(overlay);
  const $ = (id) => overlay.querySelector('#' + id);
  let pending = null;

  if (host) {
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'cp-open'; btn.id = 'copilot-open';
    btn.innerHTML = '<span aria-hidden="true">✦</span> Co-pilot';
    btn.setAttribute('aria-label', 'Open the strategy co-pilot (Ctrl or Cmd + J)');
    btn.addEventListener('click', () => open());
    host.insertBefore(btn, host.firstChild);
  }

  const show = () => { $('cp-config').textContent = JSON.stringify(Loki.config(), null, 2); };
  function open(text) { overlay.hidden = false; show(); $('cp-input').value = text || ''; $('cp-input').focus(); }
  function close() { overlay.hidden = true; pending = null; $('cp-out').innerHTML = ''; }
  function translate(text) {
    const parsed = Loki.copilot.parse(text);
    if (!parsed.changes.length) {
      pending = null;
      $('cp-out').innerHTML = `<p class="form-message">${parsed.understood ? 'That is already how the strategy is set: nothing to change.' : 'I did not understand that as a strategy change, so nothing will change. Try one of the examples below.'}</p>`;
      return;
    }
    pending = parsed;
    const diff = {}; parsed.changes.forEach((c) => { diff[c.key] = c.to; });
    $('cp-out').innerHTML = `<table class="cp-diff"><thead><tr><th>Setting</th><th>Now</th><th>Becomes</th><th>Why</th></tr></thead><tbody>${parsed.changes.map((c) => `<tr><td>${F.esc(c.label)}</td><td><code>${F.esc(JSON.stringify(c.from))}</code></td><td><code>${F.esc(JSON.stringify(c.to))}</code></td><td>${F.esc(c.why)}</td></tr>`).join('')}</tbody></table>
      <p class="caption">As code (merged into your strategy config):</p><pre class="cp-code">${F.esc(JSON.stringify(diff, null, 2))}</pre>
      <div class="button-row"><button class="button primary" id="cp-apply" type="button">Apply ${parsed.changes.length} change${parsed.changes.length > 1 ? 's' : ''}</button><button class="button secondary" id="cp-discard" type="button">Discard</button></div>`;
    $('cp-apply').addEventListener('click', () => { Loki.copilot.apply(pending); $('cp-out').innerHTML = `<p class="tk-go">Applied. The trade terminal, replay sandbox and bots use it immediately.</p>`; pending = null; show(); });
    $('cp-discard').addEventListener('click', () => { pending = null; $('cp-out').innerHTML = ''; });
  }
  overlay.querySelector('.cp-form').addEventListener('submit', (e) => { e.preventDefault(); translate($('cp-input').value); });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close();
    const ex = e.target.closest('[data-example]');
    if (ex) { $('cp-input').value = ex.dataset.example; translate(ex.dataset.example); }
  });
  $('cp-close').addEventListener('click', close);
  $('cp-undo').addEventListener('click', () => { const last = Loki.copilot.undo(); $('cp-out').innerHTML = `<p class="caption">${last ? 'Undid the last change.' : 'Nothing to undo.'}</p>`; show(); });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); overlay.hidden ? open() : close(); }
    if (e.key === 'Escape' && !overlay.hidden) close();
  });
  document.addEventListener('click', (e) => { if (e.target.closest('[data-open-copilot]')) open(); });
  Loki.copilotOpen = open;
})();
