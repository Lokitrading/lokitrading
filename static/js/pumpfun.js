/* PUMP.FUN BOT — live paper trading of new pump.fun launches, read from pump.fun's own Solana program.
 *
 * Off until asked. Once started, this tab reads /api/fusion/ai/pumpfun/stream, where the server
 * subscribes to the pump.fun program's logs and sends one decoded row per launch, trade and
 * graduation (about 10 KB a second). engine/pumpfun_core.js runs the trained rule on every new
 * standard launch and paper-trades it on the real bonding curve, in the stream's own time: orders land
 * after the tested delay at whatever reserves the curve has by then, with the program's fees and a
 * per-transaction cost. Nothing is signed or sent; there is no wallet and no key.
 */
'use strict';
(() => {
  const root = document.getElementById('pump-bot');
  if (!root || !window.LokiPump) return;
  const P = window.LokiPump;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const STORE = 'loki_pumpfun_paper_v1';
  const START_BALANCE = 10;
  const RUN_LIMIT_MS = 60 * 60 * 1000;
  const HIDDEN_LIMIT_MS = 2 * 60 * 1000;

  let cfg = null; let rule = null; let costs = null;
  let bot = null; let running = false; let abort = null; let startedAt = 0; let bytes = 0; let lastAt = 0;
  let hiddenTimer = null; let tick = null; let dirty = true; let synced = 0; let failedSynced = 0; let selected = null;
  const LAG_OK_S = 5; // Jupiter checks are only fair while the feed is close to real time
  let book = load();

  function load() {
    try { const b = JSON.parse(localStorage.getItem(STORE) || 'null'); if (b && Array.isArray(b.closed)) return { fees: 0, ...b }; } catch { /* fresh book */ }
    return { closed: [], fees: 0, started: Date.now() };
  }
  function save() { try { localStorage.setItem(STORE, JSON.stringify({ ...book, closed: book.closed.slice(-300) })); } catch { /* private mode */ } }
  const sol = (x, d = 3) => `${x >= 0 ? '' : '−'}${Math.abs(x).toFixed(d)} SOL`;
  const pct = (x, d = 1) => `${x >= 0 ? '+' : '−'}${Math.abs(x * 100).toFixed(d)}%`;
  const msg = (text, error = false) => { const el = $('pump-message'); el.textContent = text; el.classList.toggle('error', error); };
  const setStatus = (text) => { $('pump-status').textContent = text; };
  const stake = () => { const v = Number($('pump-stake').value); return Number.isFinite(v) && v >= 0.01 && v <= 5 ? v : costs.stake_sol; };

  // --- the tested record --------------------------------------------------------------------------
  function ruleText(r) {
    if (r.swarm && r.swarm.length) {
      const ages = r.swarm.map((a) => a.age_max); const bands = r.swarm.map((a) => `${Math.round(a.prog_lo * 100)}–${Math.round(a.prog_hi * 100)}%`);
      return `A swarm of ${r.swarm.length} rule agents watches every new pump.fun launch priced in SOL. Each agent has its own idea of a good entry (launches up to ${Math.round(Math.max(...ages))} seconds old, `
        + `curve bands from ${[...new Set(bands)].slice(0, 3).join(', ')}, different buyer, flow and holder limits). The bot buys only when at least ${r.quorum} of them agree. `
        + `It sells ${r.take_profit >= 1e8 ? 'with no profit cap,' : `at ${pct(r.take_profit, 0)},`} at ${pct(r.stop_loss, 0)}${r.trail != null ? `, on a ${Math.round(r.trail * 100)}-point drop from its best once up ${Math.round((r.trail_arm ?? 0.2) * 100)}%` : ''}, `
        + `when the creator sells, or after ${r.max_hold_s >= 60 ? `${Math.round(r.max_hold_s / 60)} minute${r.max_hold_s >= 120 ? 's' : ''}` : `${Math.round(r.max_hold_s)} seconds`}.`;
    }
    const m = r.mayhem === 'exclude' ? ' It skips Mayhem-mode launches (an automated agent trades those).' : r.mayhem === 'only' ? ' It only trades Mayhem-mode launches.' : '';
    return `It watches every new pump.fun launch priced in SOL and buys when the launch is at most ${Math.round(r.age_max)} seconds old, `
      + `${Math.round(r.prog_lo * 100)}–${Math.round(r.prog_hi * 100)}% of the way up its bonding curve, with at least ${r.buyers_min} different buyers in the last 30 seconds`
      + `${r.flow_min > 0 ? ` and ${r.flow_min} SOL or more of net buying` : ''}${r.dev_must_hold ? ', and the creator has not sold' : ''}`
      + `${r.bundle_max < 1 ? `, and at most ${Math.round(r.bundle_max * 100)}% of the supply was bought in the launch block` : ''}`
      + `${r.top_max < 1 ? `, and no single wallet holds more than ${Math.round(r.top_max * 100)}% of what buyers hold` : ''}`
      + `${r.model ? ', and only when a model trained on past launches expects the trade to make money after costs (it skips the setups that lost before)' : ''}.${m} `
      + `It sells ${r.take_profit >= 1e8 ? 'with no profit cap,' : `at ${pct(r.take_profit, 0)},`} at ${pct(r.stop_loss, 0)}${r.trail != null ? `, on a ${Math.round(r.trail * 100)}-point drop from its best once up ${Math.round((r.trail_arm ?? 0.2) * 100)}%` : ''}, `
      + `when the creator sells, or after ${r.max_hold_s >= 60 ? `${Math.round(r.max_hold_s / 60)} minute${r.max_hold_s >= 120 ? 's' : ''}` : `${Math.round(r.max_hold_s)} seconds`}.`;
  }
  function renderRecord() {
    const rec = cfg; const r = rec.recorded || {}; const t = rec.test || {};
    const passed = rec.verdict === 'passed';
    $('pump-intro').textContent = rule ? ruleText(rule) : 'No rule passed its checks, so the bot only watches.';
    const row = (label, s) => (s && s.trades ? `<tr><td>${esc(label)}</td><td>${s.trades}</td><td>${pct(s.mean_return_pct / 100)}</td><td>${s.win_rate_pct}%</td><td>${sol(s.total_sol, 2)}</td><td>${s.mean_ci90_pct ? `${pct(s.mean_ci90_pct[0] / 100)} to ${pct(s.mean_ci90_pct[1] / 100)}` : ''}</td></tr>` : '');
    const stressLabel = { latency_3s: 'Orders land in 3 s, not 1.5 s', 'tx_cost_0.002': 'Paying 0.002 SOL a transaction', stake_1_sol: 'Staking 1 SOL a trade' };
    const base = rec.baselines_test || {};
    $('pump-record').innerHTML = `<p class="caption">Trained on <strong>${(r.used || 0).toLocaleString()}</strong> pump.fun launches recorded straight from the program over ${r.hours} hours (${esc((r.from || '').replace('T', ' ').replace('Z', ''))} to ${esc((r.to || '').replace('T', ' ').replace('Z', ''))} UTC), every launch kept, including the many that died. `
      + 'Rules were fitted on the first half, compared on the next quarter and reported once on the last quarter, which played no part in choosing them.</p>'
      + `<p class="pf-hindsight${passed ? ' dt-learn' : ''}"><strong>${passed ? 'Passed on launches it never saw.' : 'Did not pass on launches it never saw.'}</strong> ${passed ? 'Its average trade made money on the held-out launches, and the 90% range for that average stays above zero.' : 'The held-out launches did not confirm it, so the paper bot is a live experiment, not an edge.'}</p>`
      + `<table><thead><tr><th>Launches it never saw</th><th>Trades</th><th>Avg trade</th><th>Won</th><th>Total at ${costs.stake_sol} SOL</th><th>90% range of avg</th></tr></thead><tbody>`
      + row('The bot', t) + row('At most 5 open at once', rec.test_max_5_open)
      + Object.entries(rec.test_stress || {}).map(([k, s]) => row(stressLabel[k] || k, s)).join('')
      + (rec.forward && rec.forward.bot ? row(`Forward: ${rec.forward.launches} later launches, rule frozen`, rec.forward.bot) : '')
      + row('Buy every launch at 5 s, sell at 60 s', base.buy_every_launch_at_5s_hold_60s)
      + row('Buy every launch at 30 s, sell at 5 min', base.buy_every_launch_at_30s_hold_300s)
      + `</tbody></table><p class="caption">Costs in every figure: pump.fun's ${(costs.curve_fee_bps / 100).toFixed(2)}% fee each way, ${costs.tx_cost_sol} SOL a transaction, orders landing ${costs.latency_s} s after the decision, and buys that would fill more than ${Math.round(costs.max_slippage * 100)}% above the decision price fail. One recording is one market mood; a quieter or wilder day can differ.</p>`;
  }

  // --- stream --------------------------------------------------------------------------------------
  async function pump() {
    let failures = 0;
    while (running) {
      abort = new AbortController();
      try {
        const res = await fetch('/api/fusion/ai/pumpfun/stream?seconds=45', { headers: { Accept: 'application/x-ndjson' }, signal: abort.signal });
        if (!res.ok || !res.body) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
        const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          bytes += value.byteLength; buf += dec.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
            if (!line) continue;
            let row; try { row = JSON.parse(line); } catch { continue; }
            if (row.k === 'error') throw new Error(row.detail || 'the relay lost Solana');
            if (row.k === 'open') { failures = 0; setStatus('Live'); msg('Live: reading pump.fun\'s program. New launches appear below as they are created.'); }
            if (row.at) lastAt = row.at;
            bot.handleRow(row);
            dirty = true;
          }
          sync();
          checkEntries();
        }
      } catch (error) {
        if (!running) return;
        failures += 1;
        setStatus('Reconnecting');
        if (failures >= 5) { msg(`The live feed failed five times in a row (${error.message}); stopped. Try again in a few minutes.`, true); stop('feed lost'); return; }
        await new Promise((r) => setTimeout(r, 2000 * failures));
      }
    }
  }
  /** Jupiter, the second connector: the real executable quote for the same trade, right now. */
  async function jupiterQuote(mint, side, amount) {
    const res = await fetch(`/api/fusion/ai/pumpfun/quote?mint=${encodeURIComponent(mint)}&side=${side}&amount=${Math.round(amount)}`, { headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    const q = await res.json();
    return q.available === false ? null : q;
  }
  const feedIsCurrent = () => Date.now() / 1000 - lastAt < LAG_OK_S;
  const gapText = (g) => (g == null ? '–' : Math.abs(g) < 0.0005 ? 'same price' : `${Math.abs(g * 100).toFixed(1)}% ${g > 0 ? 'better' : 'worse'}`);
  /** Each paper buy, once landed, is priced again by Jupiter for the same SOL: the gap shows how real the fill was. */
  function checkEntries() {
    if (!feedIsCurrent()) return;
    for (const pos of bot.positions.values()) {
      if (pos.status !== 'open' || pos.jupAsked) continue;
      pos.jupAsked = true;
      jupiterQuote(pos.mint, 'buy', bot.costs.stake_sol * P.LAMPORTS).then((q) => {
        if (!q || !pos.tokens) return;
        pos.jupEntry = q.out_amount / pos.tokens - 1;
        bot.note(`${pos.symbol}: Jupiter's quote for the same buy: ${gapText(pos.jupEntry)} (via ${q.route.join(' → ') || '?'})`); dirty = true;
      }).catch(() => {});
    }
  }
  /** Copy newly closed paper trades into the saved book; ask Jupiter what the same sale would have paid. */
  function sync() {
    const fresh = bot.closed.slice(synced); synced = bot.closed.length;
    if (fresh.length) {
      const rows = fresh.map((t) => ({ symbol: t.symbol, mint: t.mint, reason: t.reason, ret: t.ret, pnl: t.pnl, at: Date.now(), jupEntry: t.jupEntry, jupExit: null }));
      book.closed.push(...rows);
      rows.forEach((row, i) => {
        const t = fresh[i];
        if (!t.tokens || !t.saleLamports || !feedIsCurrent()) return;
        jupiterQuote(t.mint, 'sell', t.tokens).then((q) => { if (q) { row.jupExit = q.out_amount / t.saleLamports - 1; save(); dirty = true; } }).catch(() => {});
      });
    }
    if (bot.failed > failedSynced) { book.fees += (bot.failed - failedSynced) * bot.costs.tx_cost_sol; failedSynced = bot.failed; }
    if (fresh.length) save();
  }

  // --- run control ---------------------------------------------------------------------------------
  function start() {
    if (running) return;
    if (!rule) { msg('No rule passed its checks, so there is nothing to run.', true); return; }
    bot = new P.PaperBot(rule, { ...costs, stake_sol: stake() });
    running = true; startedAt = Date.now(); bytes = 0; synced = 0; failedSynced = 0;
    $('pump-start').disabled = true; $('pump-stop').disabled = false; $('pump-stake').disabled = true;
    setStatus('Connecting'); msg('Connecting to the live pump.fun feed…');
    pump();
    tick = setInterval(onTick, 1000);
  }
  function stop(why = 'stopped') {
    if (!running) return;
    running = false;
    if (abort) abort.abort();
    clearInterval(tick); tick = null; clearTimeout(hiddenTimer); hiddenTimer = null;
    bot.stopAll(`bot ${why}`); sync(); save();
    $('pump-start').disabled = !rule; $('pump-stop').disabled = true; $('pump-stake').disabled = false; setStatus('Off'); dirty = true; render();
  }
  function onTick() {
    if (Date.now() - startedAt > RUN_LIMIT_MS) { msg('Stopped after an hour. Start it again any time.'); stop('hour limit'); return; }
    dirty = true; render();
  }
  document.addEventListener('visibilitychange', () => {
    if (!running) return;
    if (document.hidden) hiddenTimer = setTimeout(() => { msg('Stopped because the tab was hidden for two minutes.'); stop('tab hidden'); }, HIDDEN_LIMIT_MS);
    else { clearTimeout(hiddenTimer); hiddenTimer = null; }
  });

  // --- view ----------------------------------------------------------------------------------------
  function render() {
    if (!dirty) return; dirty = false;
    const now = bot ? bot.now : 0;
    const lag = running && lastAt ? Date.now() / 1000 - lastAt : 0;
    $('pump-rate').textContent = running && bot ? `${bot.seen} launches seen · ${(bytes / 1048576).toFixed(1)} MB used${lag > 5 ? ` · feed ${Math.round(lag)} s behind` : ''}` : '';
    const live = bot ? [...bot.launches.values()].sort((a, b) => b.created - a.created).slice(0, 12) : [];
    $('pump-launches').innerHTML = live.length ? `<table><thead><tr><th>Launch</th><th>Age</th><th>Curve sold</th><th>Mkt cap</th><th>Buyers 30 s</th><th>Net buying 30 s</th><th>Bot</th></tr></thead><tbody>${live.map((la) => {
      const f = la.f;
      return `<tr data-mint="${esc(la.mint)}" class="${la.mint === selected ? 'is-selected' : ''}" tabindex="0" title="Show this launch's X-ray"><td><a href="https://pump.fun/coin/${esc(la.mint)}" target="_blank" rel="noopener noreferrer">${esc(la.symbol)}</a>${la.mayhem ? ' <span class="tag">Mayhem</span>' : ''}</td><td>${Math.max(0, Math.round(now - la.created))} s</td><td>${f ? Math.round(f.prog * 100) + '%' : '–'}</td><td>${f ? f.mcap.toFixed(1) + ' SOL' : '–'}</td><td>${f ? f.buyers30 : '–'}</td><td>${f ? f.flow30.toFixed(2) + ' SOL' : '–'}</td><td>${esc(la.verdict)}</td></tr>`;
    }).join('')}</tbody></table>` : `<p class="muted">${running ? 'Waiting for the next launch; pump.fun sees one every few seconds…' : 'Start the bot to watch new launches as they are created.'}</p>`;
    renderXray(now);
    const open = bot ? [...bot.positions.values()] : [];
    $('pump-positions').innerHTML = open.length ? `<table><thead><tr><th>Holding</th><th>Status</th><th>Now</th><th>Best</th><th>Held</th></tr></thead><tbody>${open.map((p) => `<tr><td>${esc(p.symbol)}</td><td>${p.status === 'open' ? (p.exitAt != null ? 'selling: ' + esc(p.reason) : 'open') : 'order landing'}</td><td>${p.status === 'open' ? pct(p.r) : '–'}</td><td>${p.status === 'open' && p.peak > -1 ? pct(p.peak) : '–'}</td><td>${p.landed ? Math.round(now - p.landed) + ' s' : '–'}</td></tr>`).join('')}</tbody></table>` : '';
    const c = book.closed; const n = c.length; const total = c.reduce((s, t) => s + t.pnl, 0) - book.fees; const wins = c.filter((t) => t.ret > 0).length;
    const gaps = c.flatMap((t) => [t.jupEntry, t.jupExit]).filter((g) => g != null).map((g) => Math.abs(g)).sort((x, y) => x - y);
    const medianGap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : null;
    $('pump-book').innerHTML = `<div class="pf-metrics">${[['Paper balance', sol(START_BALANCE + total)], ['Closed trades', String(n)], ['Won', n ? Math.round(wins / n * 100) + '%' : '–'], ['Total P&amp;L', n ? sol(total) : '–'], ['Typical gap vs Jupiter', medianGap == null ? '–' : `${(medianGap * 100).toFixed(1)}%`]].map(([k, v]) => `<div><span>${k}</span><strong>${v}</strong></div>`).join('')}</div>`
      + (n ? `<details class="pf-log-box"><summary>Closed paper trades</summary><div class="table-scroll"><table><thead><tr><th>Launch</th><th>Why it sold</th><th>Return</th><th>P&amp;L</th><th>Jupiter, buy</th><th>Jupiter, sell</th></tr></thead><tbody>${c.slice(-30).reverse().map((t) => `<tr><td>${esc(t.symbol)}</td><td>${esc(t.reason)}</td><td>${pct(t.ret)}</td><td>${sol(t.pnl)}</td><td>${gapText(t.jupEntry)}</td><td>${gapText(t.jupExit)}</td></tr>`).join('')}</tbody></table></div><p class="caption">Jupiter columns: what Jupiter, Solana's main trade router, quoted for the same trade when it landed, against the bot's own fill on the curve. "Worse" means a real order would have done worse than the paper fill.</p></details>` : '');
    const events = bot ? bot.events.slice(-40).reverse() : [];
    $('pump-log').innerHTML = events.map((e) => `<li><time>${esc(new Date(e.at * 1000).toLocaleTimeString())}</time> ${esc(e.text)}</li>`).join('') || '<li>No paper trades yet.</li>';
  }

  /** Launch X-ray: every signal the bot reads for the selected launch, and each rule check it passes or fails. */
  function renderXray(now) {
    const el = $('pump-xray');
    const la = bot && selected ? bot.launches.get(selected) : null;
    if (!la || !la.f) { el.innerHTML = running && bot && bot.launches.size ? '<p class="caption">Tap a launch to see its X-ray: every signal the bot reads and each check of the rule.</p>' : ''; return; }
    const f = la.f; const r = rule;
    const checks = r.swarm && r.swarm.length ? r.swarm.map((agent, i) => [`Agent ${i + 1}: ${P.why(f, agent)}`, P.decide(f, agent), '']).concat([[`Swarm: at least ${r.quorum} of ${r.swarm.length} agree`, P.decide(f, r), `${P.votes(f, r)} agree`]]) : [
      [`At most ${Math.round(r.age_max)} s old`, f.age <= r.age_max, `${Math.round(f.age)} s`],
      [`${Math.round(r.prog_lo * 100)}–${Math.round(r.prog_hi * 100)}% of the curve sold`, f.prog >= r.prog_lo && f.prog <= r.prog_hi, `${(f.prog * 100).toFixed(1)}%`],
      [`At least ${r.buyers_min} buyers in 30 s`, f.buyers30 >= r.buyers_min, String(f.buyers30)],
      [`At least ${r.flow_min} SOL net buying in 30 s`, f.flow30 >= r.flow_min, `${f.flow30.toFixed(2)} SOL`],
      [`At most ${Math.round(r.bundle_max * 100)}% bought in the launch block`, f.bundle <= r.bundle_max, `${(f.bundle * 100).toFixed(1)}%`],
      [`No wallet over ${Math.round(r.top_max * 100)}% of buyers' holdings`, f.top_share <= r.top_max, `${(f.top_share * 100).toFixed(0)}%`],
    ].filter(([label]) => !/^(At least 0 SOL|At most 100%|No wallet over 100%)/.test(label));  // limits this rule does not use
    if (!r.swarm && r.dev_must_hold) checks.push(['Creator has not sold', f.dev_sold < 0.5, f.dev_sold > 0.5 ? 'sold' : 'holding']);
    if (!r.swarm && (r.mayhem || 'any') !== 'any') checks.push([r.mayhem === 'exclude' ? 'Not a Mayhem-mode launch' : 'A Mayhem-mode launch', (f.mayhem > 0.5) === (r.mayhem === 'only'), f.mayhem > 0.5 ? 'Mayhem' : 'standard']);
    if (r.model) { const sc = P.score(r.model, f); checks.push([`Model expects at least ${pct(r.model.threshold)}`, sc >= r.model.threshold, pct(sc)]); }
    const signals = [['Market cap', `${f.mcap.toFixed(1)} SOL`], ['Distinct buyers so far', String(f.buyers_total)], ['Sells in 30 s', String(f.sells30)],
      ['Trades in 10 s', String(f.trades10)], ['Price, 30 s change', pct(f.chg30)], ['Below its peak', pct(f.peak_dd)], ['Biggest buy, share of 30 s buying', `${Math.round(f.whale30 * 100)}%`]];
    el.innerHTML = `<div class="xray-head"><strong>${esc(la.symbol)}</strong><span class="caption">${Math.round(now - la.created)} s old · ${esc(la.verdict)}</span><button type="button" class="button secondary" data-xray-close>Close</button></div>`
      + `<ul class="xray-checks">${checks.map(([label, ok, val]) => `<li class="${ok ? 'ok' : 'no'}"><span aria-hidden="true">${ok ? '✓' : '✕'}</span> ${esc(label)} <em>${esc(val)}</em></li>`).join('')}</ul>`
      + `<div class="pf-metrics">${signals.map(([k, v]) => `<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div>`;
  }
  $('pump-launches').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-mint]'); if (!tr || e.target.closest('a')) return;
    selected = tr.dataset.mint === selected ? null : tr.dataset.mint; dirty = true; render();
  });
  $('pump-launches').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr[data-mint]'); if (!tr) return;
    e.preventDefault(); selected = tr.dataset.mint === selected ? null : tr.dataset.mint; dirty = true; render();
  });
  $('pump-xray').addEventListener('click', (e) => { if (e.target.closest('[data-xray-close]')) { selected = null; dirty = true; render(); } });

  // --- check one token -----------------------------------------------------------------------------
  $('pump-check-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const mint = $('pump-check-mint').value.trim(); const out = $('pump-check-out');
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint)) { out.innerHTML = '<p class="form-message error">Paste a Solana token address (32–44 letters and digits).</p>'; return; }
    out.innerHTML = '<p class="muted">Reading its bonding curve from Solana…</p>';
    try {
      const res = await fetch(`/api/fusion/ai/pumpfun/curve?mint=${encodeURIComponent(mint)}`, { headers: { Accept: 'application/json' } });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
      const cv = d.curve; const jm = d.jupiter;
      const tiles = [];
      if (cv) tiles.push(['pump.fun curve', cv.graduated ? 'Graduated' : `${cv.progress_pct}% sold`], ['Market cap', `${cv.market_cap_sol.toFixed(1)} SOL`], ['Type', cv.mayhem ? 'Mayhem mode' : cv.standard_curve ? 'Standard' : 'Non-standard']);
      else tiles.push(['pump.fun curve', 'None']);
      if (jm) {
        const usd = (x) => (x == null ? '–' : x >= 1 ? `$${Number(x).toLocaleString(undefined, { maximumFractionDigits: 2 })}` : `$${Number(x).toPrecision(3)}`);
        tiles.push(['Price (Jupiter)', usd(jm.price_usd)], ['Liquidity', jm.liquidity_usd == null ? '–' : `$${Math.round(jm.liquidity_usd).toLocaleString()}`], ['24 h change', jm.change_24h_pct == null ? '–' : pct(jm.change_24h_pct / 100)]);
        if (jm.buy_0_1_sol) tiles.push(['Buying 0.1 SOL now', `${jm.buy_0_1_sol.price_impact_pct.toFixed(1)}% impact`]);
      }
      out.innerHTML = `<div class="pf-metrics">${tiles.map(([k, v]) => `<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div>`
        + `<p class="caption">${jm && jm.buy_0_1_sol ? `Jupiter routes it through ${esc(jm.buy_0_1_sol.route.join(' → '))}. ` : 'Jupiter has no market for it right now. '}${esc(d.note)} <a href="${esc(d.links.pump_fun)}" target="_blank" rel="noopener noreferrer">pump.fun ↗</a> · <a href="${esc(d.links.solscan)}" target="_blank" rel="noopener noreferrer">Solscan ↗</a></p>`;
    } catch (error) { out.innerHTML = `<p class="form-message error">${esc(error.message)}</p>`; }
  });

  // --- boot ----------------------------------------------------------------------------------------
  $('pump-start').addEventListener('click', start);
  $('pump-stop').addEventListener('click', () => { msg('Stopped. Open paper positions were sold at the curve\'s last price.'); stop(); });
  $('pump-reset').addEventListener('click', () => {
    if (running) { msg('Stop the bot before resetting its paper book.', true); return; }
    book = { closed: [], fees: 0, started: Date.now() }; save(); dirty = true; render(); msg('Paper book reset to 10 SOL.');
  });
  fetch('/api/fusion/ai/pumpfun', { headers: { Accept: 'application/json' } }).then(async (res) => {
    const d = await res.json(); if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    cfg = d; costs = d.costs; rule = d.verdict === 'no rule passed' ? null : d.rule || null;
    $('pump-stake').value = costs.stake_sol;
    renderRecord(); setStatus(rule ? 'Off' : 'Watching only'); $('pump-start').disabled = !rule;
  }).catch((error) => { setStatus('Unavailable'); msg(`The bot's record could not be loaded: ${error.message}`, true); });
  render();
})();
