/* RED TEAM LAB — the page for the adversarial pipeline, the local paper engine,
 * the behavioural guardrails, the cross-asset matrix, black-swan stress tests
 * and the trade journal. All engines run on this device; see engine/*.js. */
'use strict';
(() => {
  const root = document.getElementById('redteam');
  const Loki = window.Loki;
  if (!root || !Loki || !Loki.jury) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt, esc = F.esc, Q = Loki.quant;
  const pct = (x, d = 1) => (x == null || !Number.isFinite(x) ? '—' : `${(x * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null || !Number.isFinite(x) ? '—' : x.toFixed(d));
  const msg = (id, t, bad = false) => { const e = $(id); if (e) { e.textContent = t; e.classList.toggle('bad', bad); } };
  const spark = (vals, w = 600, h = 80, cls = '') => { const v = vals.filter(Number.isFinite); if (v.length < 2) return ''; const lo = Math.min(...v), hi = Math.max(...v), sp = hi - lo || 1; return `<svg class="cc-spark ${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(h - 2 - (x - lo) / sp * (h - 4)).toFixed(1)}`).join(' ')}"/></svg>`; };
  let last = null;

  /* ------------------------------------------------------------ gauntlet */
  async function gauntlet(e) {
    e && e.preventDefault();
    const symbol = $('rt-sym').value.trim().toUpperCase(), side = $('rt-side').value, usd = Number($('rt-usd').value) || 1000, strategy = $('rt-strategy').value.trim() || 'manual';
    if (!symbol) return;
    const btn = $('rt-go'); btn.disabled = true; $('rt-out').innerHTML = ''; $('rt-trade').disabled = true; $('rt-override').disabled = true;
    stage('all', 'pending');
    try {
      msg('rt-msg', `Loading ${symbol} history…`);
      const bars = await Loki.data.bars(symbol, { interval: '1d', limit: 300 });
      const price = bars[bars.length - 1].c, qty = usd / price;
      const book = Loki.paperbook.snapshot();
      msg('rt-msg', 'Quant, cross-examination, 6 red-team scenarios (18,000 paths), devil’s advocate, execution and risk…');
      const correlation = Loki.xasset ? await Loki.xasset.concentrationFor(symbol).catch(() => null) : null;
      const d = await Loki.jury.evaluate({ symbol, side, qty, price, kind: Loki.data.isCrypto && Loki.data.isCrypto(symbol) ? 'crypto' : 'stock', venue: 'local-paper' },
        { bars, equity: book.equity, drawdown: book.drawdown, strategy, risk: Loki.risk.evaluate({ swarm: Loki.swarm ? Loki.swarm.snapshot() : null }), correlation });
      last = d; render(d); msg('rt-msg', '');
      try { localStorage.setItem('loki_steps_council', JSON.stringify({ at: Date.now() })); } catch { /* storage blocked */ }
      $('rt-trade').disabled = !d.approved; $('rt-override').disabled = d.approved;
      $('rt-resize').hidden = !(d.risk.resizable && d.supermajority);
      if (!$('rt-resize').hidden) $('rt-resize').textContent = `Resize to the allowed ${F.usd(Math.floor(d.risk.maxNotional))} and re-run`;
    } catch (error) { msg('rt-msg', error.message, true); stage('all', ''); }
    finally { btn.disabled = false; }
  }
  function stage(id, cls) { root.querySelectorAll('.rt-stage').forEach((s) => { if (id === 'all' || s.dataset.stage === id) { s.classList.remove('ok', 'no', 'veto', 'pending'); if (cls) s.classList.add(cls); } }); }
  function render(d) {
    const rt = d.redteam;
    stage('quant', d.jurors[0].vote ? 'ok' : 'no'); stage('risk', d.risk.veto ? 'veto' : 'ok'); stage('execution', d.execution.vote ? 'ok' : 'no'); stage('redteam', rt.approved ? 'ok' : 'no'); stage('devil', rt.devil.bias ? 'no' : 'ok'); stage('consensus', d.approved ? 'ok' : 'no');
    const dial = `<div class="rt-dial ${rt.safety >= 60 ? 'good' : rt.safety >= 40 ? 'meh' : 'bad'}"><b>${rt.safety}%</b><span>safety confidence</span></div>`;
    $('rt-out').innerHTML = `
      <div class="rt-verdict ${d.approved ? 'go' : 'stop'}"><b>${d.approved ? 'APPROVED FOR PAPER' : d.risk.veto ? 'KILLED BY RISK VETO' : 'REJECTED BY THE JURY'}</b><span>${esc(d.postMortem)}</span></div>
      <div class="rt-grid">
        <section><p class="eyebrow">VERDICT MATRIX</p>${dial}
          <div class="cc-kpis"><div><span>Expected max drawdown</span><b>${pct(rt.expectedMaxDD)}</b></div><div><span>p95 drawdown</span><b>${pct(rt.p95MaxDD)}</b></div><div><span>Worst ${rt.horizon}-bar move ever</span><b class="down">${pct(rt.histWorst.move)}</b></div></div>
          <table class="cc-table rt-components"><thead><tr><th>Component</th><th>Weight</th><th>Score</th><th>Why</th></tr></thead><tbody>${rt.components.map((c) => `<tr><td>${esc(c.name)}</td><td>${Math.round(c.weight * 100)}%</td><td><span class="rt-bar"><i style="width:${Math.round(c.score * 100)}%"></i></span></td><td>${esc(c.why)}</td></tr>`).join('')}</tbody></table></section>
        <section><p class="eyebrow">STRESS SCENARIOS · 3,000 PATHS EACH</p>
          <table class="cc-table"><thead><tr><th>Scenario</th><th>Profitable</th><th>VaR95</th><th>CVaR95</th><th>Slip p99</th></tr></thead><tbody>${rt.scenarios.map((s) => `<tr class="${s.id === 'baseline' ? '' : s.confidence < rt.scenarios[0].confidence - 10 ? 'bad' : 'slow'}"><td>${esc(s.name)}</td><td>${s.confidence}%</td><td>${F.usd(s.var95)}</td><td>${F.usd(s.cvar95)}</td><td>${s.slip99.toFixed(1)}bp</td></tr>`).join('')}</tbody></table>
          <p class="caption">Historical worst ${rt.horizon}-bar move on this asset: ${pct(rt.histWorst.move)} → ${F.usd(rt.histWorst.pnl, true)} on this size.</p></section>
      </div>
      <div class="rt-grid">
        <section class="rt-devil ${rt.devil.bias ? 'bad' : ''}"><p class="eyebrow">DEVIL'S ADVOCATE · THE OPPOSITE ${rt.devil.side.toUpperCase()}</p><p>${esc(rt.devil.argument)}</p></section>
        <section><p class="eyebrow">CROSS-EXAMINATION · RISK ATTACKS THE QUANT</p><p><b>Quant:</b> ${esc(d.quant.hypothesis)}</p><p><b>Risk:</b> ${esc(d.crossExam ? d.crossExam.attack : 'not enough history')}</p></section>
      </div>
      <div class="rt-grid">
        <section><p class="eyebrow">JURY · ${d.yes}/${d.jurors.length} FOR (NEEDS ${d.need})</p><ul class="tk-checks">${d.jurors.map((j) => `<li class="${j.vote ? 'ok' : 'no'}"><b>${j.vote ? '✓' : '✗'} ${esc(j.name)}</b> ${esc(j.why)}</li>`).join('')}</ul></section>
        <section><p class="eyebrow">RISK MANAGER · ABSOLUTE VETO</p><p class="${d.risk.veto ? 'tk-stop' : 'tk-go'}">${esc(d.risk.summary)}</p>
          <div class="cc-kpis"><div><span>Max position</span><b>${F.usd(d.risk.maxNotional)}</b></div><div><span>Baseline VaR95</span><b>${F.usd(d.risk.baseVar95)}</b></div><div><span>Daily loss limit</span><b>${F.usd(d.risk.limit)}</b></div></div>
          <p class="eyebrow">EXECUTION ENGINE</p><p>${esc(d.execution.summary)}. ${esc(d.execution.why)}.</p></section>
      </div>`;
  }
  async function paperTrade(override) {
    if (!last) return;
    if (override && !window.confirm('The jury did not approve this trade. Paper-trade it anyway? It will be logged as STRATEGY DRIFT.')) return;
    const qty = override ? last.qty : Math.min(last.qty, last.risk.qtyMax || last.qty);
    msg('rt-msg', 'Routing through the local fill engine…');
    try {
      const r = await Loki.paperbook.execute({ symbol: last.symbol, side: last.side, qty, type: last.execution.type, algo: last.execution.algo, dark: last.execution.algo !== 'none', limit: last.execution.type === 'limit' ? +(last.price * (1 - (last.side === 'sell' ? -1 : 1) * 0.0005)).toFixed(2) : undefined, decision: last, strategy: $('rt-strategy').value.trim() || 'manual' });
      msg('rt-msg', r.pending ? `Limit order resting in the local book (id ${r.id}); it fills only when real volume clears the queue ahead of it.` : `Filled ${num(r.fill.filledQty, 6)} at ${F.num(r.fill.avgPx)}: ${r.fill.slippageBps.toFixed(1)}bp slippage, ${r.fill.latencyMs}ms latency${r.fill.partial ? ', partial' : ''}.`);
      renderBook(); renderJournal();
    } catch (error) { msg('rt-msg', error.message, true); }
  }

  /* ---------------------------------------------------------- paper book */
  function renderBook() {
    const b = Loki.paperbook.snapshot();
    $('rt-book').innerHTML = `<div class="cc-kpis"><div><span>Paper equity</span><b>${F.usd(b.equity)}</b></div><div><span>Since start</span><b class="${b.equity >= b.start ? 'up' : 'down'}">${F.usd(b.equity - b.start, true)}</b></div><div><span>Drawdown</span><b>${pct(b.drawdown)}</b></div><div><span>Size scale</span><b>×${Loki.guard.sizeScale(b.drawdown).toFixed(2)}</b></div></div>
      ${b.positions.length ? `<table class="cc-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>Mark</th><th>P&amp;L</th><th></th></tr></thead><tbody>${b.positions.map((p) => `<tr><td>${esc(p.symbol)}</td><td>${num(p.qty, 6)}</td><td>${F.num(p.avg)}</td><td>${F.num(p.mark)}</td><td class="${p.pnl >= 0 ? 'up' : 'down'}">${F.usd(p.pnl, true)}</td><td><button type="button" class="chip" data-close="${esc(p.symbol)}">Close</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">No open paper positions.</p>'}
      ${b.pending.length ? `<p class="eyebrow">RESTING LIMIT ORDERS</p><ul class="cc-list">${b.pending.map((o) => `<li>${esc(o.side)} ${num(o.qty, 6)} ${esc(o.symbol)} @ ${o.limit}</li>`).join('')}</ul>` : ''}`;
  }
  $('rt-book').addEventListener('click', async (e) => { const b = e.target.closest('[data-close]'); if (!b) return; b.disabled = true; try { await Loki.paperbook.closePosition(b.dataset.close); } catch (error) { msg('rt-book-msg', error.message, true); } renderBook(); renderJournal(); renderGuard(); });
  $('rt-book-refresh').addEventListener('click', async () => { msg('rt-book-msg', 'Marking to market, checking stops and resting orders…'); try { await Loki.paperbook.refresh(); msg('rt-book-msg', ''); } catch (error) { msg('rt-book-msg', error.message, true); } renderBook(); renderJournal(); renderGuard(); });
  $('rt-book-reset').addEventListener('click', () => { if (window.confirm('Reset the local paper book to $10,000 and clear its trades?')) { Loki.paperbook.reset(); renderBook(); renderJournal(); } });

  /* -------------------------------------------------------------- replay */
  let replayRun = null, anim = null;
  function fillReplayEvents() { $('rp2-event').innerHTML = Loki.replay.EVENTS.map((e) => `<option value="${e.id}">${esc(e.name)} · ${e.symbol} ${e.interval}</option>`).join(''); }
  function signalFrom(rules) {
    // Indicator values at bar i depend only on bars ≤ i, so one shared cache over the whole series has no look-ahead.
    let closes = null, cache = null, ref = null;
    return (i, bars, st) => {
      if (ref !== bars) { ref = bars; closes = bars.map((b) => b.c); cache = {}; }
      if (st.qty > 0) {
        const held = st.held = (st.held || 0) + 1;
        const exit = rules.exit.length && (rules.exitAny ? rules.exit.some((x) => Loki.strategy.check(x, i, bars, closes, cache, { held })) : rules.exit.every((x) => Loki.strategy.check(x, i, bars, closes, cache, { held })));
        const tp = rules.take != null && bars[i].h >= st.entry * (1 + rules.take / 100);
        return exit || tp ? 'sell' : null;
      }
      return rules.entry.length && rules.entry.every((x) => Loki.strategy.check(x, i, bars, closes, cache, { held: 0 })) ? 'buy' : null;
    };
  }
  async function runReplay(verify = false) {
    const ev = Loki.replay.EVENTS.find((x) => x.id === $('rp2-event').value), rules = Loki.strategy.parse($('rp2-rules').value);
    if (!rules.entry.length) { msg('rp2-msg', 'The strategy has no entry rule the grammar understood.', true); return; }
    msg('rp2-msg', `Loading ${ev.name}${Loki.localstore.isOffline() ? ' from the local cache (offline mode)' : ''}…`);
    let bars; try { bars = await Loki.replay.loadBars(ev); } catch (error) { msg('rp2-msg', error.message, true); return; }
    const algo = $('rp2-order').value, opts = { cash: 10000, sizePct: 1, leverage: Number($('rp2-lev').value) || 1, maintenance: Number($('rp2-maint').value) / 100 || 0.25, stopPct: rules.stop || undefined,
      order: { type: algo === 'limit' ? 'limit' : 'market', algo: ['twap', 'vwap'].includes(algo) ? algo : 'none', dark: $('rp2-dark').checked, offset_bps: 5 }, seed: 42 };
    // Signals read the bar's own state; keep per-run state on the closure.
    const st = { qty: 0, entry: 0, held: 0 };
    const sig = signalFrom(rules);
    const t0 = performance.now();
    const res = Loki.fillsim.replay((i, b, s) => { st.qty = s.qty; st.entry = s.entry; if (!s.qty) st.held = 0; return sig(i, b, st); }, bars, opts);
    const ms = performance.now() - t0;
    const fp = (r) => { let h = 0; for (const t of r.trades) for (const v of [t.px, t.qty]) { h = (h * 31 + Math.round(v * 1e6)) >>> 0; } return h.toString(16); };
    if (verify) {
      const st2 = { qty: 0, entry: 0, held: 0 };
      const again = Loki.fillsim.replay((i, b, s) => { st2.qty = s.qty; st2.entry = s.entry; if (!s.qty) st2.held = 0; return sig(i, b, st2); }, bars, opts);
      msg('rp2-msg', fp(res) === fp(again) && res.final === again.final ? `Deterministic: two runs produced identical fills (fingerprint ${fp(res)}, final ${F.usd(res.final)}).` : 'Runs differed: please report this.', fp(res) !== fp(again));
      return;
    }
    replayRun = { res, bars, ev }; animate(Number($('rp2-speed').value) || 10); renderLocal();
    msg('rp2-msg', `${bars.length.toLocaleString()} ${ev.interval} bars computed locally in ${ms.toFixed(0)}ms; playing back at ${$('rp2-speed').value}×.`);
  }
  function animate(speed) {
    cancelAnimationFrame(anim);
    const { res, bars, ev } = replayRun, stepMs = { '1s': 1000, '15m': 900e3, '1h': 3600e3, '1d': 86400e3 }[ev.interval] || 60000;
    // speed× real time, but never slower than ~2 s for the whole run on long intervals.
    const barsPerSec = Math.max(speed * 1000 / stepMs, bars.length / 20), start = performance.now();
    const draw = () => {
      const k = Math.min(res.equity.length, Math.floor((performance.now() - start) / 1000 * barsPerSec) + 2);
      const eq = res.equity.slice(0, k).map((e) => e.v), px = bars.slice(1, k + 1).map((b) => b.c);
      const tradesSoFar = res.trades.filter((t) => t.t <= (res.equity[k - 1] || {}).t);
      $('rp2-chart').innerHTML = `${spark(px, 600, 70, 'val')}${spark(eq, 600, 70, eq[eq.length - 1] >= 10000 ? 'up' : 'down')}`;
      $('rp2-stats').innerHTML = `<div class="cc-kpis"><div><span>Bar</span><b>${k}/${res.equity.length}</b></div><div><span>Equity</span><b>${F.usd(eq[eq.length - 1] || 10000)}</b></div><div><span>Fills</span><b>${tradesSoFar.length}</b></div><div><span>Margin calls</span><b>${res.trades.filter((t) => t.side === 'liquidation' && t.t <= (res.equity[k - 1] || {}).t).length}</b></div></div>`;
      if (k < res.equity.length) anim = requestAnimationFrame(draw);
      else $('rp2-log').innerHTML = [...res.log.map((l) => `<li class="down">${esc(l)}</li>`), ...res.trades.slice(-12).map((t) => `<li>${new Date(t.t).toISOString().replace('T', ' ').slice(0, 19)} ${esc(t.side)} ${num(t.qty, 5)} @ ${F.num(t.px)}${t.slippageBps != null ? ` · ${t.slippageBps.toFixed(1)}bp` : ''}${t.partial ? ' · partial' : ''}${t.pnl != null ? ` · ${F.usd(t.pnl, true)}` : ''}</li>`)].join('') + `<li><b>Result: ${pct(res.ret)} · max drawdown ${pct(res.maxDD)} · ${res.liquidations} liquidation(s)</b></li>`;
    };
    draw();
  }

  /* ------------------------------------------------------------ guardrails */
  function renderGuard() {
    const g = Loki.guard.snapshot(), b = Loki.paperbook.snapshot();
    const news = g.news;
    $('rt-guard').innerHTML = `
      <div class="cc-kpis"><div><span>News lockout</span><b class="${news.locked ? 'down' : 'up'}">${news.locked ? 'LOCKED' : 'open'}</b></div><div><span>Next event</span><b>${news.next ? `${esc(news.next.name)} in ${news.minutesToNext > 1440 ? `${Math.round(news.minutesToNext / 1440)}d` : `${news.minutesToNext}m`}` : '—'}</b></div><div><span>Drift rate (30d)</span><b>${pct(g.drift.rate, 0)}</b></div><div><span>Capital preservation</span><b>×${Loki.guard.sizeScale(b.drawdown).toFixed(2)}</b></div></div>
      <p class="eyebrow">STRATEGIES</p>${g.strategies.length ? `<table class="cc-table"><thead><tr><th>Strategy</th><th>Trades</th><th>Max-loss streak</th><th>State</th><th></th></tr></thead><tbody>${g.strategies.map((s) => `<tr class="${s.locked || s.quarantined ? 'bad' : 'ok'}"><td>${esc(s.id)}</td><td>${s.trades}</td><td>${s.streak}/${Loki.guard.STREAK}</td><td>${esc(s.reason || 'active')}</td><td>${s.locked || s.quarantined ? `<button class="chip" type="button" data-release="${esc(s.id)}">Release</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No closed paper trades yet.</p>'}
      <p class="eyebrow">STRATEGY DRIFT LOG</p><ul class="cc-list">${g.drift.log.slice(0, 8).map((d) => `<li class="${d.drift ? 'down' : ''}">${new Date(d.t).toLocaleString()} · ${esc(d.source)} ${esc(d.side)} ${esc(d.symbol)} ${d.drift ? `· DRIFT (recommended ${esc(d.recommended)})` : '· followed the plan'}</li>`).join('') || '<li class="muted">No manual orders yet.</li>'}</ul>
      <p class="eyebrow">HIGH-IMPACT EVENTS (LOCKOUT 15 MIN BEFORE, 5 AFTER)</p><ul class="cc-list">${g.events.map((e) => `<li>${new Date(e.ts).toLocaleString()} · <b>${esc(e.name)}</b> <span class="caption">${esc(e.source)}${e.estimated ? ' · estimated date' : ''}</span>${e.source === 'you' ? ` <button class="chip" type="button" data-unevent="${esc(e.time)}">Remove</button>` : ''}</li>`).join('') || '<li class="muted">No events loaded.</li>'}</ul>${g.calendarError ? `<p class="caption bad">${esc(g.calendarError)}</p>` : ''}`;
  }
  $('rt-guard').addEventListener('click', (e) => { const r = e.target.closest('[data-release]'); if (r) { Loki.guard.release(r.dataset.release); renderGuard(); } const u = e.target.closest('[data-unevent]'); if (u) { Loki.guard.removeEvent(u.dataset.unevent); renderGuard(); } });
  $('rt-event-form').addEventListener('submit', (e) => { e.preventDefault(); const when = Date.parse($('rt-event-time').value); const ok = Number.isFinite(when) && Loki.guard.addEvent($('rt-event-name').value.trim(), new Date(when).toISOString()); msg('rt-guard-msg', ok ? 'Event added: automated entries lock 15 minutes before it.' : 'Give the event a name and a time.', !ok); renderGuard(); });

  /* ---------------------------------------------------------- cross-asset */
  let xa = null, win = 'm60';
  async function loadXa(force) {
    msg('rt-xa-msg', 'Loading ten markets…');
    try { xa = await Loki.xasset.analyze(force); renderXa(); msg('rt-xa-msg', `${xa.days} common trading days${xa.errors.length ? ` · unavailable: ${xa.errors.join('; ')}` : ''}`); }
    catch (error) { msg('rt-xa-msg', error.message, true); }
  }
  function renderXa() {
    if (!xa) return;
    const m = xa[win], a = xa.assets;
    const cell = (v) => { if (!Number.isFinite(v)) return '<td>—</td>'; const c = v >= 0 ? `rgba(111,227,161,${Math.abs(v).toFixed(2)})` : `rgba(255,107,91,${Math.abs(v).toFixed(2)})`; return `<td style="background:${c}" title="${v.toFixed(2)}">${v.toFixed(2)}</td>`; };
    $('rt-xa').innerHTML = `<div class="table-scroll"><table class="rt-matrix"><thead><tr><th></th>${a.map((x) => `<th title="${esc(x.name)}">${esc(x.sym)}</th>`).join('')}</tr></thead><tbody>${a.map((x, i) => `<tr><th title="${esc(x.name)}">${esc(x.sym)}</th>${m[i].map(cell).join('')}</tr>`).join('')}</tbody></table></div>
      <p class="eyebrow">CROSS-ASSET SIGNALS</p><ul class="cc-list">${xa.signals.map((s) => `<li class="${s.level === 'warn' ? 'down' : s.level === 'ok' ? 'up' : ''}">${esc(s.text)}</li>`).join('')}</ul>
      <p class="eyebrow">DIVERGENCES (20-DAY VS 120-DAY)</p><ul class="cc-list">${xa.divergences.map((d) => `<li>${esc(d.text)}</li>`).join('') || '<li class="muted">No pair has decoupled from its norm.</li>'}</ul>
      <p class="eyebrow">MACRO PAIRS</p><table class="cc-table"><thead><tr><th>Pair</th><th>20d</th><th>60d</th><th>120d</th></tr></thead><tbody>${xa.pairs.map((p) => `<tr><td>${esc(p.label)}</td><td>${num(p.c20)}</td><td>${num(p.c60)}</td><td>${num(p.c120)}</td></tr>`).join('')}</tbody></table>
      <p class="eyebrow">REBALANCING (LOCAL PAPER BOOK)</p><ul class="cc-list">${xa.rebalance.suggestions.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>
      ${xa.rebalance.target.length ? `<table class="cc-table"><thead><tr><th>Asset</th><th>Now</th><th>Risk-parity target</th><th>Change</th></tr></thead><tbody>${xa.rebalance.target.map((t) => `<tr><td>${esc(t.sym)}</td><td>${pct(t.now, 0)}</td><td>${pct(t.target, 0)}</td><td class="${t.change >= 0 ? 'up' : 'down'}">${t.change >= 0 ? '+' : ''}${pct(t.change, 0)}</td></tr>`).join('')}</tbody></table>` : ''}`;
  }
  $('rt-xa-refresh').addEventListener('click', () => loadXa(true));
  root.querySelectorAll('[data-xawin]').forEach((b) => b.addEventListener('click', () => { win = b.dataset.xawin; root.querySelectorAll('[data-xawin]').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); renderXa(); }));

  /* ------------------------------------------------------------- stress */
  async function runStress() {
    const btn = $('rt-stress-go'); btn.disabled = true;
    const pf = Loki.stress.portfolioNow(), lev = Number($('rt-stress-lev').value) || 1, maint = (Number($('rt-stress-maint').value) || 25) / 100;
    const positions = pf.positions.map((p) => ({ ...p, value: p.value * lev }));
    try {
      msg('rt-stress-msg', `${pf.sample ? 'Paper book is empty: stressing a sample $10k book (40% SPY, 30% BTC, 20% gold). ' : ''}Overlaying four historical crises…`);
      const hist = await Loki.stress.historical(positions, pf.equity, { maintenance: maint });
      msg('rt-stress-msg', 'Running 10,000 jump-diffusion paths with systemic jumps…');
      const jd = await Loki.stress.jumpDiffusion(positions, pf.equity, { leverage: lev > 1 ? lev : null, maintenance: maint });
      $('rt-stress').innerHTML = `<div class="table-scroll"><table class="cc-table"><thead><tr><th>Crisis replayed on your book</th><th>Worst point</th><th>End</th><th>Max drawdown</th><th>Margin call</th></tr></thead><tbody>${hist.map((h) => h.error ? `<tr><td>${esc(h.name)}</td><td colspan="4">${esc(h.error)}</td></tr>` : `<tr class="${h.marginCall ? 'bad' : ''}"><td title="${esc(h.used.join('; '))}">${esc(h.name)}<br><span class="caption">${esc(h.note)}</span></td><td class="down">${F.usd(h.worst, true)} (${pct(h.worstPct)})</td><td>${F.usd(h.final, true)}</td><td>${pct(h.maxDD)}</td><td>${h.marginCall ? `day ${h.marginCall.step}` : '—'}</td></tr>`).join('')}</tbody></table></div>
        <div class="cc-kpis"><div><span>VaR99 (1 day)</span><b>${F.usd(jd.var99)}</b></div><div><span>CVaR99</span><b>${F.usd(jd.cvar99)}</b></div><div><span>10-day drawdown p95 / p99</span><b>${pct(jd.mddP95)} / ${pct(jd.mddP99)}</b></div><div><span>Margin-call probability</span><b class="${jd.marginCallProb > 0.01 ? 'down' : ''}">${pct(jd.marginCallProb, 2)}</b></div><div><span>Loss when called</span><b>${jd.marginCallProb ? F.usd(jd.avgLossAtCall) : '—'}</b></div></div>
        <p class="${jd.reduceBy > 0 ? 'tk-stop' : 'tk-go'}">${jd.reduceBy > 0 ? `VaR99 ${F.usd(jd.var99)} is above your daily loss limit ${F.usd(jd.limit)}: cut gross exposure by ${pct(jd.reduceBy, 0)} to bring it inside.` : `VaR99 ${F.usd(jd.var99)} is inside your daily loss limit ${F.usd(jd.limit)}.`}</p>
        <p class="caption">${jd.paths.toLocaleString()} paths × ${jd.days} days at ${jd.leverage.toFixed(1)}× gross leverage; systemic (all-assets) jumps on ${pct(jd.systemicShare, 2)} of days. Per-asset: ${jd.params.map((p) => `${esc(p.symbol)} σ ${pct(p.sigma, 2)}/day, jumps ${pct(p.lambda, 1)} of days`).join(' · ')}.</p>`;
      msg('rt-stress-msg', '');
    } catch (error) { msg('rt-stress-msg', error.message, true); }
    finally { btn.disabled = false; }
  }
  $('rt-stress-go').addEventListener('click', runStress);

  /* ------------------------------------------------------------ journal */
  function renderJournal() {
    const j = Loki.paperbook.journal(), jl = Loki.jury.history();
    $('rt-journal').innerHTML = `
      <p class="eyebrow">PERFORMANCE CARDS</p>${j.cards.length ? `<div class="rt-cards">${j.cards.map((c) => `<div class="rt-card"><b>${esc(c.id)}</b><span>${c.trades} trades · ${c.wins} wins · ${F.usd(c.pnl, true)}</span><div class="cc-kpis"><div><span>Sharpe</span><b>${num(c.sharpe)}</b></div><div><span>Sortino</span><b>${num(c.sortino)}</b></div><div><span>Calmar</span><b>${num(c.calmar)}</b></div><div><span>Max DD</span><b>${pct(c.maxDD)}</b></div></div>${c.guard && (c.guard.locked || c.guard.quarantined) ? `<p class="tk-stop">${esc(c.guard.reason)}</p>` : ''}</div>`).join('')}</div>` : '<p class="muted">Close some paper trades to get performance cards.</p>'}
      <p class="eyebrow">PATTERN-OF-FAILURE CLUSTERS (${j.losers} LOSING TRADES)</p><ul class="cc-bars">${j.clusters.map((c) => `<li><span>${esc(c.tag)}</span><b>${c.count} · ${pct(c.share, 0)}</b></li>`).join('') || '<li class="muted">No losing trades yet.</li>'}</ul>
      <p class="eyebrow">EXECUTION VS PLAN</p><ul class="cc-list">${j.trades.slice(0, 12).map((t) => `<li class="${t.ret >= 0 ? 'up' : 'down'}"><b>${esc(t.symbol)} ${esc(t.side)}</b> ${F.num(t.entry)} → ${F.num(t.exit)} (${esc(t.exitReason)}) ${F.usd(t.pnl, true)}${t.audit.tags.length ? ` · <i>${t.audit.tags.map(esc).join(', ')}</i>` : ''}${t.audit.notes.length ? `<br><span class="caption">${t.audit.notes.map(esc).join('; ')}</span>` : ''}</li>`).join('') || '<li class="muted">No closed trades yet.</li>'}</ul>
      <p class="eyebrow">JURY POST-MORTEMS</p><ul class="cc-list">${jl.slice(0, 10).map((d) => `<li class="${d.approved ? 'up' : 'down'}">${new Date(d.t).toLocaleString()} · <b>${esc(d.side)} ${esc(d.symbol)}</b> · safety ${d.safety}% · ${esc(d.postMortem)}</li>`).join('') || '<li class="muted">No decisions yet.</li>'}</ul>`;
    renderScorecard();
  }
  function renderScorecard() {
    const s = Loki.evolve ? Loki.evolve.snapshot() : null, c = s && s.champion;
    $('rt-score').innerHTML = c && c.scorecard ? `<div class="cc-kpis"><div><span>Champion (gen ${s.gen})</span><b>${esc(s.symbol)} ${esc(s.interval)}</b></div><div><span>Walk-forward consistency</span><b>${pct(c.walkForward.consistency, 0)}</b></div><div><span>Unseen Sharpe</span><b>${num(c.scorecard.sharpe)}</b></div><div><span>Sortino</span><b>${num(c.scorecard.sortino)}</b></div><div><span>Calmar</span><b>${num(c.scorecard.calmar)}</b></div><div><span>vs buy &amp; hold</span><b class="${c.scorecard.beatsBaseline ? 'up' : 'down'}">${pct(c.scorecard.ret)} / ${pct(c.scorecard.buyHold)}</b></div></div>
      <p class="caption">Out-of-sample folds: ${c.walkForward.folds.map((f, i) => `#${i + 1} ${pct(f.ret)}`).join(' · ')}${s.deployed && s.deployed.sunset ? ' · <b class="down">deployed strain SUNSET (below baseline)</b>' : ''}</p>`
      : '<p class="muted">Start evolution in the Command Center to get a walk-forward scorecard here.</p>';
  }
  Loki.on('evolve:gen', () => { if (!root.hidden) renderScorecard(); });

  /* -------------------------------------------------------- local engine */
  async function renderLocal() {
    const items = await Loki.localstore.list();
    $('rt-offline').checked = Loki.localstore.isOffline();
    $('rt-cache').innerHTML = `${items.length} cached series · ${(items.reduce((a, x) => a + x.bytes, 0) / 1e6).toFixed(1)} MB columnar · newest: ${items.slice(0, 4).map((x) => `${esc(x.key)} (${x.n.toLocaleString()} bars)`).join(', ') || 'none yet'}`;
  }
  $('rt-offline').addEventListener('change', (e) => { Loki.localstore.setOffline(e.target.checked); msg('rt-local-msg', e.target.checked ? 'Offline mode ON: engines read only from this device’s cache. Nothing is downloaded.' : 'Offline mode off: data is downloaded and cached as you go.'); });
  $('rt-bench').addEventListener('click', () => { msg('rt-local-msg', 'Running…'); setTimeout(() => { const b = Loki.localstore.benchmark(2_000_000); renderLocal(); msg('rt-local-msg', `${(b.rowsPerSec / 1e6).toFixed(1)} million rows/second for ${b.indicators} indicators on this device (${b.ms.toFixed(0)} ms for 2M rows): 10 years of minute bars in ${b.tenYearsMinuteSeconds.toFixed(2)} s.`); }, 30); });
  $('rt-cache-clear').addEventListener('click', async () => { if (!window.confirm('Clear the local bar cache?')) return; await Loki.localstore.clear(); renderLocal(); });

  /* -------------------------------------------------------------- wiring */
  $('rt-form').addEventListener('submit', gauntlet);
  $('rt-trade').addEventListener('click', () => paperTrade(false));
  $('rt-resize').addEventListener('click', () => { if (!last) return; $('rt-usd').value = String(Math.max(10, Math.floor(last.risk.maxNotional))); $('rt-resize').hidden = true; gauntlet(); });
  $('rt-override').addEventListener('click', () => paperTrade(true));
  $('rp2-run').addEventListener('click', () => runReplay(false));
  $('rp2-verify').addEventListener('click', () => runReplay(true));
  $('rp2-speed').addEventListener('input', (e) => { $('rp2-speed-out').textContent = `${e.target.value}×`; });
  Loki.on('guard:update', () => { if (!root.hidden) renderGuard(); });
  Loki.on('paperbook:update', () => { if (!root.hidden) renderBook(); });
  let started = false;
  function onView() {
    if (root.hidden) return;
    if (!started) { started = true; fillReplayEvents(); loadXa(false); Loki.guard.loadCalendar().then(renderGuard); setInterval(() => { if (!root.hidden && !document.hidden) Loki.paperbook.refresh().catch(() => {}); }, 60000); }
    renderBook(); renderGuard(); renderJournal(); renderLocal();
  }
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  onView();
})();
