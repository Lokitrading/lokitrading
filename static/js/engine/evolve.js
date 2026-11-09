/* ADAPTIVE STRATEGY MUTATION — an evolutionary runtime for the bot's own rules.
 *
 * A population of 30 strategy "strains" (entry/exit thresholds on the council
 * probability, trailing stop, stop-loss, take-profit, position size) lives in
 * memory. Each generation every strain is micro-simulated on real history,
 * the fittest breed (tournament selection + crossover), children mutate, and
 * the two best survive unchanged (elitism).
 *
 * Overfitting guard: strains are scored only on the first 70% of the data
 * (training). The champion is promoted only if it ALSO beats the currently
 * deployed settings on the last 30% it never trained on (validation). Paper
 * auto-deploy is opt-in; live trading is never changed automatically.
 * The council's probabilities are computed once per dataset (they don't
 * depend on the strain), so a generation takes milliseconds.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.evolve) return;
  const { clamp, rng, gauss } = Loki.math;
  const POP = 30, ELITE = 2, COST = 0.0006;
  const GENES = {
    entry: [0.5, 0.72], exit: [0.28, 0.5], trailing: [0.5, 15], stop: [1, 20], take: [2, 60], size: [0.1, 1],
  };
  const S = { running: false, symbol: 'BTC-USD', interval: '1h', bars: [], probs: [], pop: [], gen: 0, history: [], champion: null, deployed: null, autoDeploy: false, status: 'idle', seed: 1 };
  let rand = rng(7), timer = null;

  const randomGenome = () => Object.fromEntries(Object.entries(GENES).map(([k, [lo, hi]]) => [k, lo + rand() * (hi - lo)]));
  const fromConfig = (cfg) => ({ entry: 0.5 + cfg.min_council_confidence / 400, exit: 0.44, trailing: cfg.trailing_stop_pct, stop: cfg.stop_loss_pct, take: cfg.take_profit_pct, size: clamp(cfg.max_invested_pct / 50, 0.1, 1) });

  /** Long-only execution of a strain over bars[from..to) using precomputed council probabilities. */
  function simulate(g, from, to) {
    const b = S.bars, p = S.probs;
    let cash = 1, qty = 0, entry = 0, peak = 0, equityPeak = 1, maxDD = 0, trades = 0, wins = 0;
    for (let i = from; i < to - 1; i++) {
      const bar = b[i], next = b[i + 1];
      if (qty > 0) {
        peak = Math.max(peak, bar.h);
        const hit = bar.l <= entry * (1 - g.stop / 100) || bar.l <= peak * (1 - g.trailing / 100) || bar.h >= entry * (1 + g.take / 100) || p[i] < g.exit;
        if (hit) { const px = next.o * (1 - COST); if (px > entry) wins += 1; cash += qty * px; qty = 0; }
      } else if (p[i] >= g.entry) {
        const px = next.o * (1 + COST), spend = cash * g.size;
        qty = spend / px; cash -= spend; entry = px; peak = px; trades += 1;
      }
      const eq = cash + qty * next.c;
      equityPeak = Math.max(equityPeak, eq); maxDD = Math.max(maxDD, 1 - eq / equityPeak);
    }
    const final = cash + qty * b[to - 1].c;
    return { ret: final - 1, maxDD, trades, winRate: trades ? wins / trades : 0 };
  }
  const fitness = (r) => r.ret - 0.6 * r.maxDD - (r.trades < 3 ? 0.05 : 0);

  function breed(a, b) {
    const child = {};
    for (const [k, [lo, hi]] of Object.entries(GENES)) {
      let v = rand() < 0.5 ? a[k] : b[k];
      if (rand() < 0.35) v += gauss(rand) * (hi - lo) * 0.08;
      child[k] = clamp(v, lo, hi);
    }
    if (child.exit >= child.entry) child.exit = child.entry - 0.04;
    return child;
  }

  function generation() {
    const cut = Math.floor(S.bars.length * 0.7), start = 60;
    const scored = S.pop.map((g) => { const train = simulate(g, start, cut); return { g, train, fit: fitness(train) }; }).sort((x, z) => z.fit - x.fit);
    const pick = () => { let best = null; for (let k = 0; k < 3; k++) { const c = scored[Math.floor(rand() * scored.length)]; if (!best || c.fit > best.fit) best = c; } return best.g; };
    S.pop = scored.slice(0, ELITE).map((s) => s.g).concat(Array.from({ length: POP - ELITE }, () => breed(pick(), pick())));
    S.gen += 1;
    const top = scored[0], val = simulate(top.g, cut, S.bars.length), deployedVal = S.deployed ? simulate(S.deployed.g, cut, S.bars.length) : null;
    S.champion = { g: top.g, train: top.train, val, fit: top.fit };
    S.history.push({ gen: S.gen, best: top.fit, median: scored[Math.floor(scored.length / 2)].fit, val: fitness(val) });
    S.history = S.history.slice(-200);
    if (deployedVal && fitness(val) > fitness(deployedVal) + 0.005) {
      S.status = `Generation ${S.gen}: the champion beats the deployed rules on unseen data (${(val.ret * 100).toFixed(1)}% vs ${(deployedVal.ret * 100).toFixed(1)}%).`;
      if (S.autoDeploy) deploy();
    } else S.status = `Generation ${S.gen}: best training fitness ${top.fit.toFixed(3)}; deployed rules still hold on unseen data.`;
    Loki.emit('evolve:gen', snapshot());
  }

  function deploy() {
    if (!S.champion) return null;
    const g = S.champion.g, cfg = Loki.config();
    const next = { ...cfg, trailing_stop_pct: +g.trailing.toFixed(2), stop_loss_pct: +g.stop.toFixed(2), take_profit_pct: +g.take.toFixed(2), min_council_confidence: clamp(Math.round((g.entry - 0.5) * 400), 0, 90), max_invested_pct: clamp(Math.round(g.size * 50), 5, 100) };
    const changes = ['trailing_stop_pct', 'stop_loss_pct', 'take_profit_pct', 'min_council_confidence', 'max_invested_pct'].filter((k) => cfg[k] !== next[k]).map((k) => ({ key: k, from: cfg[k], to: next[k], why: `evolved strain, generation ${S.gen}` }));
    Loki.copilot.apply({ changes, config: next });
    S.deployed = { g: { ...g }, gen: S.gen };
    Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'evolution', action: 'deploy-strain', gen: S.gen, genome: g, validation: S.champion.val });
    Loki.emit('evolve:deploy', { gen: S.gen, changes });
    return changes;
  }

  /** Council probabilities for every bar, computed once (chunked so the page stays responsive). */
  async function prepare(symbol, interval) {
    S.status = `Loading ${symbol} ${interval} history…`; Loki.emit('evolve:gen', snapshot());
    const bars = await Loki.data.bars(symbol, { interval, limit: interval === '1d' ? 300 : 900 });
    if (bars.length < 200) throw new Error('not enough history');
    const probs = new Array(bars.length).fill(0.5);
    for (let i = 80; i < bars.length; i++) {
      probs[i] = Loki.council.voteBars(bars.slice(Math.max(0, i - 160), i + 1)).p;
      if (i % 40 === 0) { S.status = `Council reading ${i}/${bars.length} bars…`; Loki.emit('evolve:gen', snapshot()); await new Promise((r) => setTimeout(r, 0)); }
    }
    Object.assign(S, { symbol, interval, bars, probs, gen: 0, history: [] });
    rand = rng(S.seed++);
    S.pop = [fromConfig(Loki.config()), ...Array.from({ length: POP - 1 }, randomGenome)];
    S.deployed = { g: fromConfig(Loki.config()), gen: 0 };
  }

  async function start(symbol = S.symbol, interval = S.interval) {
    if (S.running) return;
    S.running = true;
    try {
      if (!S.bars.length || symbol !== S.symbol || interval !== S.interval) await prepare(symbol, interval);
      timer = setInterval(() => { if (!document.hidden) generation(); }, 1200);
      generation();
    } catch (error) { S.running = false; S.status = `Could not start: ${error.message}`; Loki.emit('evolve:gen', snapshot()); }
  }
  function stop() { S.running = false; clearInterval(timer); timer = null; S.status = `Paused at generation ${S.gen}.`; Loki.emit('evolve:gen', snapshot()); }
  function snapshot() { return { running: S.running, symbol: S.symbol, interval: S.interval, gen: S.gen, champion: S.champion, deployed: S.deployed, history: S.history, status: S.status, autoDeploy: S.autoDeploy, bars: S.bars.length }; }

  Loki.evolve = { start, stop, deploy, snapshot, simulate, fitness, GENES, setAutoDeploy(v) { S.autoDeploy = Boolean(v); Loki.emit('evolve:gen', snapshot()); }, _state: S, _generation: generation };
})();
