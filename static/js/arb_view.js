/* 5-MINUTE ARBITRAGE PANEL (paper) — UI for Loki.arb in the predictions view. */
'use strict';
(() => {
  const root = document.getElementById('arb-panel');
  const Loki = window.Loki;
  if (!root || !Loki || !Loki.arb) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt;
  const log = [];

  function chart(series) {
    const s = series.filter((x) => x.fair != null);
    if (s.length < 3) return '<p class="muted">The fair-value line appears after a few seconds.</p>';
    const W = 420, H = 110, t0 = s[0].t, t1 = s[s.length - 1].t;
    const x = (t) => 4 + (t - t0) / ((t1 - t0) || 1) * (W - 8), y = (p) => 4 + (1 - p) * (H - 8);
    const path = (key) => s.filter((p) => p[key] != null).map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
    return `<svg class="arb-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Model fair value versus Polymarket mid for Up"><line x1="4" x2="${W - 4}" y1="${y(0.5)}" y2="${y(0.5)}" class="half"/><path d="${path('mid')}" class="mid"/><path d="${path('fair')}" class="fair"/></svg>
      <p class="caption"><span class="lg fair"></span> model fair value (Up) <span class="lg mid"></span> Polymarket mid</p>`;
  }

  function render(s) {
    $('arb-toggle').textContent = s.running ? 'Stop engine ❚❚' : 'Start engine ▶';
    const m = s.market;
    const left = m ? Math.max(0, Math.round(m.end - Date.now() / 1000)) : null;
    const e = s.edges || {};
    const cell = (label, value, cls = '') => `<div><span>${label}</span><b class="${cls}">${value}</b></div>`;
    $('arb-live').innerHTML = !s.running ? '<p class="muted">Engine stopped. It runs only while this page is open, on your device.</p>' : `
      <p class="caption">${m ? `<a href="${F.esc(m.url)}" target="_blank" rel="noopener noreferrer">${F.esc(m.question)} ↗</a> · ${left}s left` : 'Finding the live market…'}${s.error ? ` · <span class="bad">${F.esc(s.error)}</span>` : ''}</p>
      ${s.warning ? `<p class="form-message">${F.esc(s.warning)}</p>` : ''}
      <div class="arb-cells">
        ${cell('Start price', s.s0 ? F.num(s.s0) : '…')}${cell('Spot now', s.fair ? F.num(s.fair.spot) : '…')}${cell('TWAP so far', s.fair && s.fair.twap ? F.num(s.fair.twap) : 'not yet')}
        ${cell('Model P(Up)', s.fair ? F.pct(s.fair.p) : '…')}${cell('Up ask', e.upAsk != null ? e.upAsk.toFixed(2) : '—')}${cell('Down ask', e.downAsk != null ? e.downAsk.toFixed(2) : '—')}
        ${cell('Edge Up', s.stale ? 'paused' : e.up != null ? `${(e.up * 100).toFixed(1)}pt` : '—', e.up > 0 && !s.stale ? 'up' : 'down')}${cell('Edge Down', s.stale ? 'paused' : e.down != null ? `${(e.down * 100).toFixed(1)}pt` : '—', e.down > 0 && !s.stale ? 'up' : 'down')}${cell('Book round trip', s.rtt ? `${Math.round(s.rtt)}ms` : '…')}
        ${cell('Market lag', s.lag == null ? 'measuring' : `${s.lag}s behind the model`)}
      </div>${chart(s.series || [])}`;
    const p = s.paper;
    $('arb-stats').innerHTML = [
      ['Paper cash', F.usd(p.cash)], ['P&L (settled)', F.usd(p.pnl, true)], ['Won', `${p.wins}/${p.closed}`], ['Signals', p.signals],
      ['Filled', p.fills], ['Missed after delay', p.missed], ['Avg edge at signal', Number.isFinite(p.edgeSignal) ? `${(p.edgeSignal * 100).toFixed(1)}pt` : '—'], ['Avg edge at fill', Number.isFinite(p.edgeFill) ? `${(p.edgeFill * 100).toFixed(1)}pt` : '—'],
    ].map(([k, v]) => `<div><span>${k}</span><b>${F.esc(String(v))}</b></div>`).join('');
    $('arb-trades').innerHTML = p.trades.length ? p.trades.map((t) => `<li class="${t.status}"><b>${t.side.toUpperCase()}</b> ${t.shares} @ ${t.price.toFixed(2)} · ${F.esc(t.slug.replace(/-updown-5m-\d+/, '').toUpperCase())} ${new Date(t.end).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${t.status === 'open' ? 'awaiting result' : `${t.status} ${F.usd(t.pnl, true)}`}</li>`).join('') : '<li class="muted">No paper trades yet.</li>';
  }

  Loki.on('arb:update', render);
  Loki.on('arb:log', (entry) => { log.unshift({ ...entry, t: Date.now() }); log.splice(30); $('arb-log').innerHTML = log.map((l) => `<li class="${l.kind}"><time>${F.time(l.t)}</time> ${F.esc(l.text)}</li>`).join(''); });
  $('arb-toggle').addEventListener('click', () => { const s = Loki.arb.snapshot(); if (s.running) Loki.arb.stop(); else Loki.arb.start($('arb-asset').value); });
  $('arb-asset').addEventListener('change', () => Loki.arb.setAsset($('arb-asset').value));
  $('arb-edge').addEventListener('change', () => Loki.arb.setMinEdge(Number($('arb-edge').value) / 100));
  $('arb-reset').addEventListener('click', () => { if (window.confirm('Reset the arbitrage paper account?')) Loki.arb.reset(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && Loki.arb.snapshot().running) render(Loki.arb.snapshot()); });
  render(Loki.arb.snapshot());
})();
