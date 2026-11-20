/* Your money · Sure (workspace view: #my-money).
 *
 * Connects to the visitor's own Sure personal-finance server (github.com/we-promise/sure) through
 * Loki's read-only relay (/api/sure/overview). The address and API key stay in this browser —
 * sessionStorage by default, localStorage only when "Remember on this device" is ticked — and are
 * sent with each read; the server never stores them. Shows net worth, accounts, holdings and the
 * Portfolio AI's reading beside each holding it follows. Nothing here is advice.
 */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $('sure-form');
  if (!form) return;
  const KEY = 'loki_sure_v1';
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    read() {
      for (const s of [sessionStorage, localStorage]) {
        try { const v = JSON.parse(s.getItem(KEY) || 'null'); if (v && v.url && v.key) return { ...v, remembered: s === localStorage }; } catch { /* storage blocked */ }
      }
      return null;
    },
    write(v, remember) {
      this.clear();
      try { (remember ? localStorage : sessionStorage).setItem(KEY, JSON.stringify({ url: v.url, key: v.key })); } catch { /* storage blocked */ }
    },
    clear() { for (const s of [sessionStorage, localStorage]) { try { s.removeItem(KEY); } catch { /* storage blocked */ } } },
  };
  let creds = store.read();
  let busy = false;
  let currency = 'USD';

  const money = (v, cur = currency) => {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD', maximumFractionDigits: 0 }).format(Number(v)); } catch { return Number(v).toFixed(0); }
  };
  const msg = (text, ok) => { const m = $('sure-message'); m.textContent = text; m.classList.toggle('success', Boolean(ok)); };
  const setConnected = (on, host) => {
    $('sure-status').textContent = on ? `Connected · ${host || ''}` : 'Not connected';
    $('sure-refresh').disabled = !on; $('sure-forget').disabled = !on;
  };

  function render(d) {
    currency = d.currency || 'USD';
    $('sure-networth').textContent = money(d.net_worth);
    $('sure-assets').textContent = money(d.assets);
    $('sure-liabilities').textContent = money(d.liabilities);
    $('sure-invested').textContent = money(d.invested);
    $('sure-facts').innerHTML = (d.facts || []).map((f) => `<li>${esc(f)}</li>`).join('') || '<li>Nothing to note yet.</li>';
    $('sure-note').textContent = d.note || '';
    const read = (l) => {
      if (!l) return '<span class="muted">not followed by Loki</span>';
      const s = Number(l.trend_score);
      const tone = s > 0 ? 'positive' : s < 0 ? 'negative' : '';
      return `<span class="${tone}">${esc(l.trend || '—')} · score ${Number.isFinite(s) ? (s > 0 ? '+' : '') + s.toFixed(0) : '—'}</span>` +
        `<small class="muted"> ${esc(String(l.regime || '').replace(/_/g, ' ').toLowerCase())}${l.ai_holds ? ` · held by the AI (${esc(l.ai_weight_pct)}%)` : ''}</small>`;
    };
    $('sure-holdings').innerHTML = (d.holdings || []).map((h) =>
      `<tr><td><strong>${esc(h.ticker || h.name || '—')}</strong>${h.ticker && h.name ? `<small class="muted"> ${esc(h.name)}</small>` : ''}</td>` +
      `<td>${money(h.value, h.currency)}</td><td>${esc(h.account || '—')}</td><td>${read(h.loki)}</td></tr>`).join('') ||
      '<tr><td colspan="4" class="muted">No investment holdings in Sure.</td></tr>';
    $('sure-accounts').innerHTML = (d.accounts || []).map((a) =>
      `<tr><td>${esc(a.name || '—')}${a.institution ? `<small class="muted"> ${esc(a.institution)}</small>` : ''}</td>` +
      `<td>${esc(String(a.type || '').replace(/_/g, ' '))}</td><td class="${a.classification === 'liability' ? 'negative' : ''}">${money(a.balance, a.currency)}</td></tr>`).join('') ||
      '<tr><td colspan="3" class="muted">No accounts in Sure.</td></tr>';
    $('sure-result').hidden = false;
  }

  async function load() {
    if (!creds || busy) return;
    busy = true;
    msg('Reading your Sure server…');
    try {
      const res = await fetch('/api/sure/overview', { cache: 'no-store', headers: { 'X-Sure-Url': creds.url, 'X-Sure-Key': creds.key }, signal: AbortSignal.timeout(60000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not read your Sure server.');
      render(data);
      setConnected(true, data.server);
      msg(`Read just now: ${(data.accounts || []).length} accounts, ${(data.holdings || []).length} holdings.`, true);
    } catch (error) {
      setConnected(false);
      msg(error.name === 'TimeoutError' ? 'Your Sure server took too long to answer.' : error.message);
    } finally { busy = false; }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const url = $('sure-url').value.trim(), key = $('sure-key').value.trim();
    if (!url || !key) { msg('Enter your Sure address and API key.'); return; }
    creds = { url, key };
    store.write(creds, $('sure-remember').checked);
    $('sure-key').value = '';
    load();
  });
  $('sure-refresh').addEventListener('click', load);
  $('sure-forget').addEventListener('click', () => {
    store.clear(); creds = null; setConnected(false);
    $('sure-result').hidden = true; $('sure-url').value = ''; msg('Forgotten: the address and key are gone from this browser.', true);
  });

  if (creds) { $('sure-url').value = creds.url; $('sure-remember').checked = creds.remembered; setConnected(false); }
  // Desktop app: Sure runs on this computer beside Loki (the app starts it with Docker).
  let localTimer = 0;
  async function checkLocal() {
    let info;
    try { info = await (await fetch('/api/sure/local', { cache: 'no-store' })).json(); } catch { return; }
    const box = $('sure-local');
    if (!info || !info.desktop) { box.hidden = true; return; }
    const open = info.url ? `<a class="button secondary" href="${esc(info.url)}" target="_blank" rel="noopener noreferrer">Open Sure ↗</a>` : '';
    const text = {
      running: `<strong>Sure is running on this computer.</strong> First time? Open Sure, create your account and add your accounts, then go to Settings → API key, create a read key and paste it below.`,
      starting: `<strong>Sure is starting on this computer…</strong> The first start downloads about 2 GB, so it can take several minutes. This note updates by itself.`,
      checking: `<strong>Looking for Sure on this computer…</strong>`,
      'no-docker': `<strong>Sure can run inside Loki on this computer.</strong> Install <a class="text-link" href="https://www.docker.com/products/docker-desktop/" target="_blank" rel="noopener noreferrer">Docker Desktop</a>, start it, then reopen Loki; it sets Sure up for you. Or connect a Sure server you already run.`,
      failed: `<strong>Sure could not start on this computer.</strong> Check that Docker Desktop is running, then reopen Loki. Details are in loki.log in Loki's data folder.`,
      off: `Sure on this computer is turned off (LOKI_SURE=0). You can still connect any Sure server.`,
    }[info.state] || '';
    box.innerHTML = `<p>${text}</p>${info.state === 'running' ? open : ''}`;
    box.hidden = !text;
    if (info.url && !$('sure-url').value) $('sure-url').value = info.url;
    clearTimeout(localTimer);
    if (['starting', 'checking'].includes(info.state)) localTimer = setTimeout(checkLocal, 10000);
  }

  let loaded = false;
  const onView = () => {
    if (location.hash !== '#my-money') return;
    checkLocal();
    if (creds && !loaded) { loaded = true; load(); }
  };
  window.addEventListener('hashchange', onView);
  onView();
})();
