/* TOP-BAR FEED LIGHTS — live health of every upstream the workspace depends on.
 *
 * Reads /api/status/feeds (each check is a tiny real request on the server,
 * cached ~45 s) every 60 s while the tab is visible. One dot in the top bar
 * shows the worst state; the popover lists every feed with its latency.
 */
'use strict';
(() => {
  const wrap = document.getElementById('feed-lights');
  if (!wrap) return;
  const button = document.getElementById('feed-lights-button');
  const panel = document.getElementById('feed-lights-panel');
  const dot = button.querySelector('.feed-dot');
  const label = button.querySelector('.feed-lights-label');
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const WORDS = { ok: 'Live', slow: 'Slow', setup: 'Not set up', down: 'Down', checking: 'Checking' };
  const SUMMARY = { ok: 'All feeds live', slow: 'Some feeds slow', setup: 'Feeds live · some optional feeds not set up', down: 'A feed is down', checking: 'Checking feeds' };
  let timer = null;
  let last = null;

  function render(data) {
    last = data;
    const overall = WORDS[data.overall] ? data.overall : 'down';
    const down = (data.feeds || []).filter((f) => f.state === 'down').length;
    // "setup" only means an optional feed has no keys yet; every feed that is set up is live, so the
    // top bar reads as live and the popover still lists what is not set up.
    dot.dataset.state = overall === 'setup' ? 'ok' : overall;
    label.textContent = overall === 'ok' || overall === 'setup' ? 'Feeds live' : overall === 'slow' ? 'Feeds slow' : `${down} feed${down === 1 ? '' : 's'} down`;
    button.setAttribute('aria-label', `${SUMMARY[overall]}. Show data feed status.`);
    const when = data.checked_at ? new Date(data.checked_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
    panel.innerHTML = `<p class="eyebrow">DATA FEEDS · ${esc(SUMMARY[overall].toUpperCase())}</p><ul>` +
      (data.feeds || []).map((f) => {
        const state = WORDS[f.state] ? f.state : 'down';
        return `<li><span class="feed-dot" data-state="${state}" aria-hidden="true"></span><span class="feed-name">${esc(f.label)}</span>` +
          `<span class="feed-state">${esc(WORDS[state])}${f.latency_ms >= 1 ? ` · ${esc(f.latency_ms)} ms` : ''}</span>` +
          `<span class="feed-detail">${esc(f.detail)}</span></li>`;
      }).join('') +
      `</ul><p class="caption">Checked ${esc(when)} with a small live request to each source. When a feed is down Loki shows "unavailable" instead of guessing.</p>`;
  }

  async function check() {
    if (document.hidden) return;
    try {
      const res = await fetch('/api/status/feeds', { cache: 'no-store', signal: AbortSignal.timeout(20000) });
      if (res.status === 401 || res.status === 403) return; // signed out: the gate handles it
      const data = await res.json().catch(() => null);
      if (!res.ok || !data) throw new Error('status unavailable');
      render(data);
    } catch {
      render({ overall: 'down', checked_at: Math.floor(Date.now() / 1000), feeds: [{ key: 'loki', label: 'Loki server', state: 'down', detail: 'Could not reach the status check — your connection or the server may be down.' }] });
    }
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(check, 60000);
  }

  button.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open && !last) check();
  });
  document.addEventListener('click', (event) => {
    if (!panel.hidden && !wrap.contains(event.target)) { panel.hidden = true; button.setAttribute('aria-expanded', 'false'); }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !panel.hidden) { panel.hidden = true; button.setAttribute('aria-expanded', 'false'); button.focus(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { check(); schedule(); } });
  check();
  schedule();
})();
