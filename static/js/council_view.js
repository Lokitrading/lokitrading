/* COUNCIL VIEW — six bots, their learned trust, the swarm, and the journal. */
'use strict';
(() => {
  const root = document.getElementById('council');
  const Loki = window.Loki;
  if (!root || !Loki || !Loki.council) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt;
  let busy = false;

  function fillSymbols() {
    const list = Loki.config().watchlist;
    $('cn-symbols').innerHTML = list.map((s) => `<button type="button" class="chip" data-sym="${F.esc(s)}">${F.esc(s)}</button>`).join('');
  }

  async function ask(symbol) {
    if (busy) return;
    busy = true; $('cn-go').disabled = true;
    $('cn-status').textContent = `Gathering ${symbol} history and letting the bots argue…`;
    try {
      const bars = await Loki.data.bars(symbol, { interval: '1d', limit: 300 });
      const sw = Loki.swarm.snapshot();
      const r = Loki.council.analyze(symbol, bars, { sentiment: sw.running ? sw.sentiment : undefined });
      render(r);
      $('cn-status').textContent = r.warmed ? `Learned from ${r.warmed} past decisions on ${symbol} before answering.` : `Answered from ${r.record.calls} scored calls on ${symbol}.`;
    } catch (error) {
      $('cn-status').textContent = error.message;
    } finally { busy = false; $('cn-go').disabled = false; renderJournal(); }
  }

  // The honest yardstick is not a coin flip but always guessing this market's usual up-rate.
  function skillLine(rec, r) {
    if (rec.skill == null) return `Always guessing this market's usual rate (${Number.isFinite(r.usual) ? Math.round(r.usual * 100) + '% up' : '—'}) is the bar to beat; there are not enough comparable calls yet to say whether the council beats it.`;
    const pts = rec.skill * 100;
    const word = pts > 1 ? 'better than' : pts < -1 ? 'worse than' : 'about the same as';
    return `That is ${word} always guessing this market's usual rate (Brier ${rec.usual_brier.toFixed(3)}; skill ${pts >= 0 ? '+' : ''}${pts.toFixed(1)}%). ` +
      'Over 10 years of 21 markets the council\'s skill was about zero: five-day direction is close to unpredictable, so treat this as a second opinion and use the Portfolio AI for decisions.';
  }

  function render(r) {
    const cls = r.verdict === 'BUY' ? 'up' : r.verdict === 'SELL' ? 'down' : 'flat';
    const rec = r.record;
    $('cn-verdict').innerHTML = `
      <div class="cn-head"><div><span class="eyebrow">${F.esc(r.symbol)} · NEXT ${r.horizon} BARS</span><b class="${cls}">${r.verdict}</b><span>${Math.round(r.p_up * 100)}% chance of finishing higher${Number.isFinite(r.usual) ? ` (usually ${Math.round(r.usual * 100)}%)` : ''} · confidence ${Math.round(r.confidence * 100)}% · agreement ${Math.round(r.agreement * 100)}%</span></div>
      <div class="cn-mood"><span class="eyebrow">STATE OF MIND · ${F.esc(r.mood.word.toUpperCase())}</span><p>${F.esc(r.mood.text)}</p></div></div>
      <p class="caption">Track record on ${F.esc(r.symbol)}: ${rec.calls} scored calls, ${rec.hit_rate == null ? '—' : Math.round(rec.hit_rate * 100) + '% right'}, Brier ${rec.brier == null ? '—' : rec.brier.toFixed(3)} (lower is better). ${skillLine(rec, r)} These are honest walk-forward numbers, not a promise.</p>`;
    $('cn-bots').innerHTML = r.votes.map((v) => `
      <article class="cn-bot">
        <header><span class="cn-icon" aria-hidden="true">${v.icon}</span><div><strong>${F.esc(v.name)}</strong><span class="muted">${F.esc(v.style)}</span></div></header>
        <div class="cn-meter" role="img" aria-label="${Math.round(v.p * 100)}% up"><i style="width:${Math.round(v.p * 100)}%"></i><span>${Math.round(v.p * 100)}% up</span></div>
        <p>${F.esc(v.why)}</p>
        <p class="caption">Trust ${Math.round(v.weight * 100)}% · ${v.hit_rate == null ? 'no record yet' : `${Math.round(v.hit_rate * 100)}% right over ${v.calls} calls`}</p>
      </article>`).join('');
    renderLeaderboard();
  }

  function renderLeaderboard() {
    $('cn-league').innerHTML = Loki.council.globalRecord().sort((a, z) => (z.hit_rate || 0) - (a.hit_rate || 0))
      .map((b) => `<li><span>${b.icon} ${F.esc(b.name)}</span><b>${b.hit_rate == null ? '—' : Math.round(b.hit_rate * 100) + '%'}</b><span class="muted">${b.calls} calls</span></li>`).join('');
  }
  function renderJournal() {
    const items = Loki.council.journal().slice(0, 12);
    $('cn-journal').innerHTML = items.length ? items.map((j) => `<li><time>${F.esc(new Date(j.t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</time> ${F.esc(j.text)}</li>`).join('') : '<li class="muted">Nothing learned yet: ask the council about a symbol.</li>';
  }

  function renderSwarm(s) {
    $('sw-toggle').textContent = s.running ? 'Stop the swarm ❚❚' : 'Deploy the swarm ▶';
    $('sw-agents').innerHTML = s.agents.map((a) => `<li class="sw-${a.status}"><span class="sw-dot" style="--alarm:${a.alarm.toFixed(2)}"></span><b>${F.esc(a.name)}</b><span class="muted">${F.esc(a.family)}</span><span class="sw-alarm">${a.status === 'ok' ? `alarm ${Math.round(a.alarm * 100)}% · ${a.direction >= 0 ? '▲' : '▼'} ${Math.abs(a.direction).toFixed(2)}` : F.esc(a.status)}</span><span class="caption">${F.esc(a.note)}</span></li>`).join('');
    $('sw-consensus').innerHTML = `<b class="${s.event ? 'down' : 'up'}">${s.event ? 'VOLATILITY EVENT' : 'CALM'}</b> · ${s.quorum} of 6 agents alarmed · position size ×${s.sizeMultiplier.toFixed(2)} · crowd bias ${s.sentiment >= 0 ? '+' : ''}${s.sentiment.toFixed(2)} (fed to Swarm Bee)`;
    $('sw-rounds').innerHTML = s.rounds.map((r) => `<li><time>${F.time(r.t)}</time> ${r.quorum ? '<b>quorum</b> ' : ''}${F.esc(r.alarmed.join(', ') || 'no alarms')} → size ×${r.size.toFixed(2)}, bias ${r.bias >= 0 ? '+' : ''}${r.bias.toFixed(2)}</li>`).join('') || '<li class="muted">No rounds yet.</li>';
    const ev = s.events.filter((e) => e.lead_min != null);
    $('sw-lead').textContent = ev.length ? `Measured lead over the news: ${ev.map((e) => `${e.lead_min >= 0 ? '+' : ''}${e.lead_min} min`).join(', ')} (positive = the swarm reacted before the first matching headline).` : 'No event has been confirmed by a headline yet, so no lead time is claimed.';
  }

  $('cn-symbols').addEventListener('click', (e) => { const b = e.target.closest('[data-sym]'); if (b) { $('cn-symbol').value = b.dataset.sym; ask(b.dataset.sym); } });
  $('cn-form').addEventListener('submit', (e) => { e.preventDefault(); const s = $('cn-symbol').value.trim().toUpperCase(); if (s) ask(s); });
  $('cn-reset').addEventListener('click', () => { if (window.confirm('Forget everything the council has learned on this device?')) { Loki.council.reset(); renderJournal(); renderLeaderboard(); $('cn-bots').innerHTML = ''; $('cn-verdict').innerHTML = ''; } });
  $('sw-toggle').addEventListener('click', () => { const s = Loki.swarm.snapshot(); if (s.running) Loki.swarm.stop(); else Loki.swarm.start($('sw-pair').value); });
  $('sw-pair').addEventListener('change', () => Loki.swarm.setPair($('sw-pair').value));
  Loki.on('swarm:update', renderSwarm);
  Loki.on('config:change', fillSymbols);
  Loki.on('council:journal', renderJournal);
  fillSymbols(); renderJournal(); renderLeaderboard(); renderSwarm(Loki.swarm.snapshot());
  const onView = () => { if (!root.hidden && !root.dataset.asked) { root.dataset.asked = '1'; ask(Loki.config().watchlist[0] || 'SPY'); } };
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  onView();
})();
