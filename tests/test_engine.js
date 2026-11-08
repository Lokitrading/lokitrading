// On-device engine: council learning, co-pilot parsing, Monte Carlo, arbitrage fair value,
// risk matrix and the replay sandbox, loaded exactly as the browser loads them.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ENGINE = path.join(__dirname, '../static/js/engine/');
function load(...files) {
  const mem = {};
  const storage = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); }, removeItem: (k) => { delete mem[k]; } };
  const ctx = { console, Math, Date, JSON, URL, URLSearchParams, AbortSignal, performance, setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: storage, sessionStorage: storage, location: { href: 'https://x.test/', origin: 'https://x.test' }, fetch: async () => { throw new Error('offline'); } };
  ctx.window = ctx; ctx.self = ctx;
  vm.createContext(ctx);
  for (const f of files) vm.runInContext(fs.readFileSync(ENGINE + f, 'utf8'), ctx, { filename: f });
  return ctx;
}
function series(n, fn, seed = 1) {
  let s = seed; const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  const gauss = () => Math.sqrt(-2 * Math.log(rnd())) * Math.cos(2 * Math.PI * rnd());
  const bars = []; let p = 100;
  for (let i = 0; i < n; i++) { const o = p; p = p * Math.exp(fn(i) + 0.01 * gauss()); bars.push({ t: 1.6e12 + i * 86400000, o, h: Math.max(o, p) * 1.003, l: Math.min(o, p) * 0.997, c: p, v: 1000 + 200 * rnd() }); }
  return bars;
}

test('council replays history, scores six bots and trusts the trend bot in a steady trend', () => {
  const { Loki } = load('core.js', 'council.js');
  const r = Loki.council.analyze('TREND', series(320, () => 0.004));
  assert.equal(r.votes.length, 6);
  assert.ok(r.warmed > 150, 'walk-forward replay happened');
  assert.ok(r.p_up > 0.5 && r.verdict !== 'SELL', 'a steady uptrend is not called down');
  const trend = r.votes.find((v) => v.id === 'trend'), revert = r.votes.find((v) => v.id === 'revert');
  assert.ok(trend.weight > revert.weight, 'the trend follower out-earned the mean-reverter in a trend');
  assert.ok(Math.abs(r.votes.reduce((a, v) => a + v.weight, 0) - 1) < 1e-9, 'weights sum to 1');
  assert.ok(Loki.council.journal().length >= 1, 'it wrote a reflection');
  // A second visit with the same bars does not re-learn the same history twice.
  assert.equal(Loki.council.analyze('TREND', series(320, () => 0.004)).warmed, 0);
});

test('co-pilot turns plain language into exact, bounded config changes and can undo them', () => {
  const { Loki } = load('core.js', 'copilot.js');
  const r = Loki.copilot.parse('Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction positions if volatility spikes above 40%');
  const byKey = Object.fromEntries(r.changes.map((c) => [c.key, c]));
  assert.equal(byKey.trailing_stop_pct.to, 3.5);
  assert.deepEqual(JSON.parse(JSON.stringify(byKey.hedge_rule.to)), { enabled: true, vol_threshold_pct: 40 });
  assert.equal(Loki.copilot.parse('Set take profit to 12% and max position size 3%').changes.length, 2);
  assert.equal(Loki.copilot.parse('max position size 400%').changes[0].to, 25, 'clamped to the hard cap');
  assert.equal(Loki.copilot.parse('what is the weather').changes.length, 0, 'unknown sentences change nothing');
  Loki.copilot.apply(r);
  assert.equal(Loki.config().trailing_stop_pct, 3.5);
  Loki.copilot.undo();
  assert.equal(Loki.config().trailing_stop_pct, 5);
});

test('Monte Carlo is deterministic per seed and prices costs, thin books and binary payoffs', () => {
  const { LokiMC } = load('mc_core.js');
  const plan = { legs: [{ side: 'buy', qty: 10, price: 100, kind: 'stock', fee_bps: 1 }], returns: Array.from({ length: 300 }, (_, i) => 0.01 * Math.sin(i)), bar_seconds: 86400, horizon_bars: 5, paths: 2000, seed: 3 };
  const a = LokiMC.simulate(plan), b = LokiMC.simulate(plan);
  assert.equal(a.confidence, b.confidence);
  assert.ok(a.var95 > 0 && a.cvar95 >= a.var95);
  const fair = { legs: [{ side: 'buy', qty: 100, price: 0.4, kind: 'binary', fee_rate: 0.07, fair_p: 0.7, book: { asks: [[0.4, 1000]], bids: [[0.38, 1000]] } }], paths: 4000, seed: 5 };
  const bad = { legs: [{ ...fair.legs[0], fair_p: 0.2 }], paths: 4000, seed: 5 };
  assert.ok(LokiMC.simulate(fair).confidence > 60 && LokiMC.simulate(bad).confidence < 30, 'a mispriced winner beats a mispriced loser');
  const thin = LokiMC.simulate({ legs: [{ side: 'buy', qty: 1000, price: 0.4, kind: 'binary', fair_p: 0.5, book: { asks: [[0.4, 50]], bids: [] } }], paths: 500, seed: 1 });
  assert.ok(thin.book_exhausted_share > 0.9, 'a book that cannot fill the size is reported');
});

test('arbitrage fair value follows the TWAP settlement rule and the market fee curve', () => {
  const { Loki } = load('core.js', 'arb.js');
  const S = Loki.arb._state;
  S.market = { start: 1000, end: 1300, lookback: 60, fee: { rate: 0.07, exponent: 1 } };
  S.secs = Array.from({ length: 600 }, (_, i) => ({ t: (700 + i) * 1000, o: 100, h: 100, l: 100, c: 100 + 0.01 * Math.sin(i), v: 1 }));
  S.s0 = 99.0;
  assert.ok(Loki.arb.fairUp(1200).p > 0.95, 'far above the start price: Up is near certain');
  S.s0 = 101.0;
  assert.ok(Loki.arb.fairUp(1200).p < 0.05, 'far below: Down is near certain');
  S.s0 = 100.0;
  const mid = Loki.arb.fairUp(1100).p;
  assert.ok(mid > 0.2 && mid < 0.8, 'at the start price with time left it is uncertain');
  assert.equal(Loki.arb.feeShare(0.5, { rate: 0.07, exponent: 1 }).toFixed(4), '0.0175');
  assert.ok(Loki.arb.feeShare(0.95, { rate: 0.07, exponent: 1 }) < Loki.arb.feeShare(0.5, { rate: 0.07, exponent: 1 }));
});

test('risk matrix trips on drawdown, hedges thin binaries, and heals after the cooldown', () => {
  const { Loki } = load('core.js', 'risk.js');
  const state = Loki.risk.freshState(), t0 = 1.7e12;
  const curve = Array.from({ length: 40 }, (_, i) => ({ t: t0 + i * 3600e3, v: 1000 * (1 + 0.002 * Math.sin(i)) }));
  assert.equal(Loki.risk.evaluate({ curve, state, now: t0 + 40 * 3600e3 }).level, 'green');
  const crash = [...curve, { t: t0 + 41 * 3600e3, v: 800 }];
  const r = Loki.risk.evaluate({ curve: crash, state, now: t0 + 41 * 3600e3, positions: [
    { id: 'a', symbol: 'BTC up', kind: 'binary', qty: 100, value: 40, exit_slippage_bps: 400, opposite_ask: 0.55 },
    { id: 'b', symbol: 'SPY', kind: 'stock', qty: 10, value: 7000, exit_slippage_bps: 3 },
    { id: 'c', symbol: 'MES', kind: 'future', qty: 2, value: 3000, exit_slippage_bps: 200 } ] });
  assert.equal(r.level, 'red');
  assert.equal(r.sizeMultiplier, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(r.actions.map((a) => a.type))), ['hedge', 'reduce', 'limit-exit']);
  // recovered equity but still inside the cooldown: stays red
  const recovered = [...crash, { t: t0 + 41.1 * 3600e3, v: 1000 }];
  assert.equal(Loki.risk.evaluate({ curve: recovered, state, now: t0 + 41.1 * 3600e3, swarm: { sizeMultiplier: 1 } }).level, 'red');
  // after the cooldown and calm: healed, size ramps back from 25%
  const healed = Loki.risk.evaluate({ curve: recovered, state, now: t0 + 42 * 3600e3, swarm: { sizeMultiplier: 1 } });
  assert.notEqual(healed.level, 'red');
  assert.ok(healed.ramp === 0.25 && healed.sizeMultiplier <= 0.25);
});

test('replay sandbox steps through a crash with stops and keeps drawdown below buy-and-hold', () => {
  const ctx = load('core.js', 'council.js', 'risk.js', 'copilot.js', 'replay.js');
  const bars = series(260, (i) => (i < 140 ? 0.002 : i < 200 ? -0.012 : 0.004), 9);
  const sim = ctx.Loki.replay.simulation(bars);
  let s; while ((s = sim.step()) && !s.done) { /* play to the end */ }
  s = sim.snapshot();
  assert.ok(s.done && s.equity.length > 150);
  assert.ok(s.dd < s.holdDd, `bot drawdown ${s.dd.toFixed(3)} should be below buy-and-hold ${s.holdDd.toFixed(3)}`);
  assert.ok(s.decisions.length > 0, 'decisions are logged step by step');
});
