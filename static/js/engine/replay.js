/* LIVE SIMULATION & REPLAY SANDBOX — play back real market shocks bar by bar
 * and watch the bot's risk models react, step by step, on this device.
 *
 * Each step uses only bars up to "now": the six-bot council votes (and keeps
 * learning inside the replay from its own resolved calls), a volatility alarm
 * stands in for the swarm, the self-healing risk matrix sets the size, and the
 * strategy config (trailing stop, stop-loss, take-profit, sizing) from the
 * co-pilot is applied. Fills happen at the next bar's open with costs.
 * Crypto events use Binance's public history (device-direct); stock events use
 * Loki's cached history relay.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.replay) return;
  const { clamp, std, logReturns, logLoss } = Loki.math;
  const F = Loki.fmt;

  const EVENTS = [
    { id: 'ftx', name: 'FTX collapse', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '1h', start: '2022-11-03', end: '2022-11-15', note: 'Exchange insolvency: BTC fell ~25% in four days.' },
    { id: 'luna', name: 'LUNA / UST death spiral', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '1h', start: '2022-05-03', end: '2022-05-16', note: 'A stablecoin de-pegged and dragged crypto down.' },
    { id: 'covid-btc', name: 'Black Thursday (crypto)', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '1h', start: '2020-03-06', end: '2020-03-20', note: 'BTC halved in a day as COVID panic hit every market.' },
    { id: 'yen', name: 'Yen carry-trade unwind', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '15m', start: '2024-08-01', end: '2024-08-08', note: 'A flash crash across assets on 5 August 2024.' },
    { id: 'election', name: 'US election night 2024', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '15m', start: '2024-11-03', end: '2024-11-09', note: 'Prediction markets resolved and BTC repriced overnight.' },
    { id: 'tariff-btc', name: 'Tariff shock (crypto)', symbol: 'BTC-USD', pair: 'BTCUSDT', interval: '1h', start: '2025-03-31', end: '2025-04-14', note: '“Liberation Day” tariffs: risk assets sold off, then snapped back.' },
    { id: 'covid-spy', name: 'COVID crash (S&P 500)', symbol: 'SPY', interval: '1d', start: '2019-10-01', end: '2020-06-30', note: 'The fastest 30% bear market on record, then a V.' },
    { id: 'deepseek', name: 'DeepSeek AI shock (Nvidia)', symbol: 'NVDA', interval: '1d', start: '2024-10-01', end: '2025-03-15', note: 'A cheap Chinese AI model knocked ~17% off Nvidia in a day.' },
    { id: 'bear2022', name: '2022 bear market (Nasdaq)', symbol: 'QQQ', interval: '1d', start: '2021-09-01', end: '2022-12-31', note: 'A slow, grinding year-long decline as rates rose.' },
    { id: 'tariff-spy', name: 'Tariff shock (S&P 500)', symbol: 'SPY', interval: '1d', start: '2024-12-01', end: '2025-05-30', note: 'A 19% drawdown in weeks, then a full recovery.' },
  ];
  const WARMUP = 60, H = 3, COST_BPS = 5;

  async function loadBars(ev) {
    if (ev.pair) {
      const start = Date.parse(ev.start + 'T00:00:00Z'), end = Date.parse(ev.end + 'T00:00:00Z');
      const out = [];
      let cursor = start;
      while (cursor < end && out.length < 3000) {
        const chunk = await Loki.data.binanceKlines(ev.pair, ev.interval, 1000, cursor, end);
        if (!chunk.length) break;
        out.push(...chunk);
        cursor = chunk[chunk.length - 1].t + 1;
        if (chunk.length < 1000) break;
      }
      return out;
    }
    const data = await Loki.getJSON(`/api/replay/bars?symbol=${encodeURIComponent(ev.symbol)}&start=${ev.start}&end=${ev.end}&interval=${ev.interval}`, { timeout: 20000 });
    return data.bars;
  }

  /** A fresh simulation over the bars. step() advances one bar and returns what happened. */
  function simulation(bars, cfgIn) {
    const cfg = { ...Loki.config(), ...(cfgIn || {}) };
    const sim = {
      i: WARMUP, cash: 10000, qty: 0, entry: 0, peak: 0, equity: [], hold: [], decisions: [], trades: 0, trips: 0,
      riskState: Loki.risk.freshState(), weights: Object.fromEntries(Loki.council.BOTS.map((b) => [b.id, 1 / Loki.council.BOTS.length])),
      loss: Object.fromEntries(Loki.council.BOTS.map((b) => [b.id, 0])), calls: [], done: false, last: null,
    };
    const startPrice = bars[WARMUP].c, startCash = sim.cash;
    const say = (text, kind = '') => sim.decisions.push({ t: bars[sim.i].t, text, kind });

    function learn() {
      // Resolve the replay's own past calls whose horizon has passed and reweight the bots.
      const ready = sim.calls.filter((c) => c.i + H <= sim.i);
      sim.calls = sim.calls.filter((c) => c.i + H > sim.i);
      for (const c of ready) {
        const y = bars[c.i + H].c > bars[c.i].c ? 1 : 0;
        for (const v of c.votes) sim.loss[v.id] = sim.loss[v.id] * 0.97 + logLoss(v.p, y);
      }
      if (ready.length) {
        const raw = Loki.council.BOTS.map((b) => Math.exp(-0.9 * sim.loss[b.id]));
        const total = raw.reduce((a, x) => a + x, 0);
        Loki.council.BOTS.forEach((b, k) => { sim.weights[b.id] = raw[k] / total; });
      }
    }

    function step() {
      if (sim.done) return null;
      const i = sim.i, bar = bars[i], view = bars.slice(0, i + 1);
      learn();
      const { votes, p, confidence } = Loki.council.voteBars(view, sim.weights);
      sim.calls.push({ i, votes: votes.map((v) => ({ id: v.id, p: v.p })) });
      const r = logReturns(view.slice(-120).map((b) => b.c));
      const volRatio = (std(r.slice(-12)) || 1e-9) / (std(r) || 1e-9);
      const swarmMult = clamp(1 - 0.5 * clamp((volRatio - 1.3) / 1.5, 0, 1), 0.4, 1);
      const value = sim.cash + sim.qty * bar.c;
      sim.equity.push({ t: bar.t, v: value });
      sim.hold.push({ t: bar.t, v: startCash * bar.c / startPrice });
      const risk = Loki.risk.evaluate({ curve: sim.equity.slice(-400), swarm: { sizeMultiplier: swarmMult, event: volRatio > 2.2 ? { at: bar.t } : null }, state: sim.riskState, now: bar.t });
      if (risk.level === 'red' && (!sim.last || sim.last.risk !== 'red')) { sim.trips += 1; say(`Risk matrix tripped: ${risk.reason}.`, 'down'); }
      const next = bars[i + 1];
      if (!next) { sim.done = true; return snapshot(); }
      const fill = (dir) => next.o * (1 + dir * (COST_BPS + 0.05 * (bar.h - bar.l) / bar.c * 1e4) / 1e4);
      // exits first: stops, then signal flips, then the breaker
      if (sim.qty > 0) {
        sim.peak = Math.max(sim.peak, bar.h);
        const trail = sim.peak * (1 - cfg.trailing_stop_pct / 100), stop = sim.entry * (1 - cfg.stop_loss_pct / 100), take = sim.entry * (1 + cfg.take_profit_pct / 100);
        let why = null;
        if (bar.l <= stop) why = `stop-loss ${cfg.stop_loss_pct}% below entry`;
        else if (bar.l <= trail) why = `trailing stop ${cfg.trailing_stop_pct}% under the peak`;
        else if (bar.h >= take) why = `take-profit +${cfg.take_profit_pct}%`;
        else if (p < 0.45) why = `council turned bearish (${Math.round(p * 100)}% up)`;
        else if (risk.level === 'red') why = 'risk matrix breaker';
        if (why) {
          const px = fill(-1), pnl = (px - sim.entry) * sim.qty;
          sim.cash += sim.qty * px; say(`SELL ${F.num(sim.qty, 4)} at ${F.num(px)}: ${why}. Trade ${F.usd(pnl, true)}.`, pnl >= 0 ? 'up' : 'down');
          sim.qty = 0;
        }
      }
      const threshold = 0.5 + (cfg.min_council_confidence / 100) * 0.25;
      if (sim.qty === 0 && p >= threshold && risk.level !== 'red') {
        const size = clamp(risk.sizeMultiplier * Loki.copilot.modeMultiplier() * (cfg.max_invested_pct / 100) * 2, 0, 1);
        if (size > 0.05) {
          const px = fill(1), qty = (sim.cash * size) / px;
          sim.cash -= qty * px; sim.qty = qty; sim.entry = px; sim.peak = px; sim.trades += 1;
          say(`BUY ${F.num(qty, 4)} at ${F.num(px)}: council ${Math.round(p * 100)}% up (confidence ${Math.round(confidence * 100)}%), size ${Math.round(size * 100)}% of cash (risk ${risk.level}, vol alarm ×${swarmMult.toFixed(2)}).`, 'buy');
        }
      }
      sim.last = { t: bar.t, price: bar.c, p, confidence, risk: risk.level, riskReason: risk.reason, size: risk.sizeMultiplier, swarmMult, weights: { ...sim.weights }, votes };
      sim.i += 1;
      return snapshot();
    }

    function snapshot() {
      const eq = sim.equity, hold = sim.hold;
      const dd = (curve) => { let peak = 0, worst = 0; for (const p of curve) { peak = Math.max(peak, p.v); worst = Math.max(worst, 1 - p.v / peak); } return worst; };
      return { i: sim.i, total: bars.length, done: sim.done, last: sim.last, decisions: sim.decisions.slice(-14).reverse(), trades: sim.trades, trips: sim.trips,
        equity: eq, hold, ret: eq.length ? eq[eq.length - 1].v / startCash - 1 : 0, holdRet: hold.length ? hold[hold.length - 1].v / startCash - 1 : 0,
        dd: dd(eq), holdDd: dd(hold), inMarket: sim.qty > 0 };
    }
    return { step, snapshot, bars };
  }

  /* ------------------------------------------------------------------ UI */
  function mount(root) {
    if (!root || root.dataset.ready) return;
    root.dataset.ready = '1';
    root.innerHTML = `
      <div class="rp-controls">
        <label>Market event<select id="rp-event">${EVENTS.map((e) => `<option value="${e.id}">${F.esc(e.name)} · ${e.symbol} ${e.interval}</option>`).join('')}</select></label>
        <label>Speed<select id="rp-speed"><option value="300">1×</option><option value="80" selected>4×</option><option value="20">16×</option><option value="0">Max</option></select></label>
        <div class="button-row"><button class="button primary" id="rp-play" type="button">Load &amp; play ▶</button><button class="button secondary" id="rp-step" type="button">Step ⏭</button><button class="button secondary" id="rp-reset" type="button">Reset</button></div>
      </div>
      <p class="caption" id="rp-note"></p>
      <canvas id="rp-chart" class="rp-chart" height="260" aria-label="Replay chart: price, bot equity and buy-and-hold"></canvas>
      <div class="rp-stats" id="rp-stats"></div>
      <div class="rp-grid"><div><p class="eyebrow">WHAT THE BOT SEES NOW</p><div id="rp-now" class="rp-now"><p class="muted">Pick an event and press play.</p></div></div>
      <div><p class="eyebrow">DECISIONS, STEP BY STEP</p><ol id="rp-log" class="rp-log"></ol></div></div>`;
    const $ = (id) => root.querySelector('#' + id);
    let sim = null, timer = null, current = null;
    const describe = () => { const ev = EVENTS.find((e) => e.id === $('rp-event').value); $('rp-note').textContent = `${ev.note} ${ev.start} → ${ev.end}. Uses your co-pilot settings (trailing stop ${Loki.config().trailing_stop_pct}%, stop ${Loki.config().stop_loss_pct}%).`; return ev; };
    describe();
    $('rp-event').addEventListener('change', () => { stop(); sim = null; describe(); });
    const stop = () => { clearTimeout(timer); timer = null; $('rp-play').textContent = sim && !sim.snapshot().done ? 'Play ▶' : 'Load & play ▶'; };
    async function ensure() {
      const ev = describe();
      if (sim && current === ev.id) return true;
      $('rp-play').disabled = true; $('rp-now').innerHTML = '<p class="muted">Loading history…</p>';
      try {
        const bars = await loadBars(ev);
        if (bars.length < WARMUP + 10) throw new Error('not enough history came back for this window');
        sim = simulation(bars); current = ev.id;
        return true;
      } catch (error) {
        $('rp-now').innerHTML = `<p class="form-message">${F.esc(error.message)}</p>`; return false;
      } finally { $('rp-play').disabled = false; }
    }
    function loop() {
      const delay = Number($('rp-speed').value);
      const steps = delay === 0 ? 25 : 1;
      let snap = null;
      for (let k = 0; k < steps; k++) { snap = sim.step(); if (!snap || snap.done) break; }
      render(snap || sim.snapshot());
      if (snap && !snap.done && timer !== null) timer = setTimeout(loop, delay || 16);
      else stop();
    }
    $('rp-play').addEventListener('click', async () => {
      if (timer !== null) { stop(); return; }
      if (!(await ensure())) return;
      $('rp-play').textContent = 'Pause ❚❚'; timer = 0; loop();
    });
    $('rp-step').addEventListener('click', async () => { stop(); if (await ensure()) render(sim.step() || sim.snapshot()); });
    $('rp-reset').addEventListener('click', () => { stop(); sim = null; current = null; $('rp-log').innerHTML = ''; $('rp-stats').innerHTML = ''; $('rp-now').innerHTML = '<p class="muted">Reset. Press play.</p>'; draw(null); });

    function render(s) {
      if (!s) return;
      const L = s.last;
      $('rp-stats').innerHTML = [
        ['Bot', F.pct(s.ret), s.ret >= 0 ? 'up' : 'down'], ['Buy & hold', F.pct(s.holdRet), s.holdRet >= 0 ? 'up' : 'down'],
        ['Bot worst drawdown', F.pct(-s.dd), 'down'], ['Buy & hold worst drawdown', F.pct(-s.holdDd), 'down'],
        ['Trades', String(s.trades), ''], ['Breaker trips', String(s.trips), ''], ['Progress', `${Math.min(100, Math.round(s.i / s.total * 100))}%`, ''],
      ].map(([k, v, c]) => `<div><span>${F.esc(k)}</span><b class="${c}">${F.esc(v)}</b></div>`).join('');
      if (L) {
        const top = Object.entries(L.weights).sort((a, z) => z[1] - a[1])[0];
        const bot = Loki.council.BOTS.find((b) => b.id === top[0]);
        $('rp-now').innerHTML = `<p><b>${F.esc(new Date(L.t).toLocaleString())}</b> · price ${F.num(L.price)}</p>
          <p>Council: <b>${Math.round(L.p * 100)}% up</b> (confidence ${Math.round(L.confidence * 100)}%), trusting ${F.esc(bot.icon + ' ' + bot.name)} most (${Math.round(top[1] * 100)}%).</p>
          <p>Risk matrix: <b class="risk-${L.risk}">${L.risk.toUpperCase()}</b>, ${F.esc(L.riskReason)}; size ×${L.size.toFixed(2)}; volatility alarm ×${L.swarmMult.toFixed(2)}.</p>
          <p>${s.inMarket ? 'In the market.' : 'In cash.'}${s.done ? ' <b>Replay finished.</b>' : ''}</p>`;
      }
      $('rp-log').innerHTML = s.decisions.map((d) => `<li class="${d.kind}"><time>${F.esc(new Date(d.t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</time> ${F.esc(d.text)}</li>`).join('') || '<li class="muted">No trades yet: the bot is waiting for an edge.</li>';
      draw(s);
    }
    function draw(s) {
      const c = $('rp-chart'), ctx = c.getContext('2d'), w = c.clientWidth || 600, h = c.height;
      c.width = w * (window.devicePixelRatio || 1); c.height = h * (window.devicePixelRatio || 1); ctx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (!s || !s.equity.length) return;
      const n = s.total - WARMUP, x = (k) => 8 + (k / Math.max(1, n - 1)) * (w - 16);
      const all = s.equity.map((p) => p.v).concat(s.hold.map((p) => p.v));
      const lo = Math.min(...all), hi = Math.max(...all), y = (v) => h - 16 - (v - lo) / ((hi - lo) || 1) * (h - 32);
      const line = (pts, color, width) => { ctx.beginPath(); pts.forEach((p, k) => (k ? ctx.lineTo(x(k), y(p.v)) : ctx.moveTo(x(k), y(p.v)))); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke(); };
      line(s.hold, 'rgba(162,181,196,.7)', 1.4);
      line(s.equity, '#a8f0c6', 2);
      ctx.font = '11px ui-monospace, monospace'; ctx.fillStyle = '#a8f0c6'; ctx.fillText('bot', 10, 14); ctx.fillStyle = 'rgba(162,181,196,.9)'; ctx.fillText('buy & hold', 44, 14);
    }
  }

  Loki.replay = { EVENTS, simulation, loadBars, mount };
})();
