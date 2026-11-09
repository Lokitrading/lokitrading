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

test('arbitrage engine refuses to trade on a stalled feed or an implausible edge', () => {
  const { Loki } = load('core.js', 'arb.js');
  const now = 2e12;
  const live = Array.from({ length: 30 }, (_, i) => ({ t: now - (30 - i) * 1000, c: 100, v: i % 3 }));
  const frozen = live.map((b) => ({ ...b, v: 0 }));
  assert.equal(Loki.arb.feedStale(live, now), false);
  assert.equal(Loki.arb.feedStale(frozen, now), true, 'zero-volume seconds = stalled feed');
  assert.equal(Loki.arb.feedStale(live, now + 60000), true, 'old data = stalled feed');
  assert.ok(Loki.arb.MAX_PLAUSIBLE_EDGE <= 0.25, 'a 48-point "edge" is a data error, never a trade');
});

test('natural-language builder parses a sentence into typed rules, code and a next-bar-fill backtest', () => {
  const { Loki } = load('core.js', 'strategy_nl.js');
  const r = Loki.strategy.parse('Buy ETH 15m when the 20 EMA crosses above the 50 EMA and RSI 14 is below 70, sell when RSI goes above 75 or after 30 bars, stop loss 2%, take profit 6%, size 50%');
  assert.equal(r.symbol, 'ETH-USD'); assert.equal(r.interval, '15m');
  assert.deepEqual(JSON.parse(JSON.stringify(r.entry.map((c) => c.type))), ['rsi', 'cross']);
  assert.equal(r.stop, 2); assert.equal(r.take, 6); assert.equal(r.size, 0.5); assert.equal(r.exitAny, true);
  assert.ok(r.exit.some((c) => c.type === 'bars_held' && c.n === 30));
  assert.match(Loki.strategy.toCode(r), /crossesAbove\(ema\(close, 20\), ema\(close, 50\), i\)/);
  // Unknown words are reported, never invented into rules.
  const vague = Loki.strategy.parse('buy when the vibes are good');
  assert.equal(vague.entry.length, 0); assert.ok(vague.notes.some((n) => /No entry condition/.test(n)));
  // A breakout rule on a rising series trades, fills at the next open, and pays costs.
  const bars = series(400, () => 0.003);
  const bt = Loki.strategy.backtest(Loki.strategy.parse('buy when price breaks the 20 bar high, stop loss 5%, take profit 4%'), bars);
  assert.ok(bt.trades > 3, 'it traded'); assert.ok(bt.list.every((t) => t.bars >= 0 && Number.isFinite(t.ret)));
  const flat = Loki.strategy.backtest({ ...Loki.strategy.parse('buy when rsi is below 101, sell after 1 bars'), size: 1 }, series(200, () => 0, 3));
  assert.ok(flat.avgTrade < 0, 'round trips on a driftless series lose the 10bp-per-side cost');
});

test('portfolio Monte Carlo bootstraps whole days jointly and prices binaries at fair odds', () => {
  const { LokiMC } = load('mc_core.js');
  const days = Array.from({ length: 250 }, (_, i) => Math.sin(i * 1.7) * 0.02);
  const same = LokiMC.simulatePortfolio({ positions: [{ id: 'a', value: 1000, returns: days }, { id: 'b', value: 1000, returns: days }], paths: 4000, seed: 5 });
  const hedged = LokiMC.simulatePortfolio({ positions: [{ id: 'a', value: 1000, returns: days }, { id: 'b', value: -1000, returns: days }], paths: 4000, seed: 5 });
  assert.ok(same.var95 > 25, 'two identical longs add up');
  assert.ok(Math.abs(hedged.var95) < 1e-9, 'a long and an equal short on the same days cancel exactly');
  assert.ok(same.var99 >= same.var95 && same.cvar95 >= same.var95);
  const bin = LokiMC.simulatePortfolio({ positions: [{ id: 'x', kind: 'binary', qty: 100, price: 0.5, fair_p: 0.5 }], paths: 20000, seed: 9 });
  assert.ok(Math.abs(bin.var95 - 50) < 1e-9, 'a 50¢ token loses its 50¢ stake in the bad half');
});

test('execution monitor marks a failing venue down and routes size to zero', () => {
  const { Loki } = load('core.js', 'execq.js');
  for (let i = 0; i < 6; i++) Loki.emit('net', { phase: 'end', host: 'paper-api.alpaca.markets', path: '/v2/orders', ms: 120, status: i < 4 ? 503 : 200 });
  for (let i = 0; i < 6; i++) Loki.emit('net', { phase: 'end', host: 'data-api.binance.vision', path: '/api/v3/klines', ms: 90, status: 200 });
  assert.equal(Loki.execq.stats('alpaca-paper').state, 'down'); assert.equal(Loki.execq.sizeFactor('alpaca-paper'), 0);
  assert.equal(Loki.execq.stats('binance').state, 'healthy'); assert.equal(Loki.execq.sizeFactor('binance'), 1);
  assert.equal(Loki.execq.stats('tradovate').state, 'unknown');
});

test('consensus gate vetoes on a down venue or a red breaker, and lets exits through', async () => {
  const { Loki } = load('core.js', 'council.js', 'risk.js', 'execq.js', 'consensus.js');
  Loki.getJSON = async (url) => { if (url === '/api/newswire') return { tone: 0, items: [] }; throw new Error('offline'); };
  Loki.data.bars = async () => series(80, () => 0.004);
  const council = { p_up: 0.8, horizon: 5, confidence: 0.6 };
  const green = { level: 'green', reason: 'within limits' }, red = { level: 'red', reason: 'drawdown' };
  const ok = await Loki.consensus.vote({ symbol: 'SPY', side: 'buy', notional: 500, venue: 'alpaca-paper', kind: 'stock' }, { council, risk: green });
  assert.equal(ok.approved, true, ok.summary);
  const blocked = await Loki.consensus.vote({ symbol: 'SPY', side: 'buy', notional: 500, venue: 'alpaca-paper', kind: 'stock' }, { council, risk: red });
  assert.equal(blocked.approved, false); assert.ok(blocked.vetoes.includes('Risk-Manager'));
  const exit = await Loki.consensus.vote({ symbol: 'SPY', side: 'sell', notional: 500, venue: 'alpaca-paper', kind: 'stock', exit: true }, { council, risk: red });
  assert.equal(exit.approved, true, 'closing risk is never blocked by the breaker or a bearish vote');
  for (let i = 0; i < 5; i++) Loki.emit('net', { phase: 'end', host: 'paper-api.alpaca.markets', path: '/v2/orders', ms: 100, status: 502 });
  const down = await Loki.consensus.vote({ symbol: 'SPY', side: 'buy', notional: 500, venue: 'alpaca-paper', kind: 'stock' }, { council, risk: green });
  assert.ok(down.vetoes.includes('Execution-Quality'));
  const thin = await Loki.consensus.vote({ symbol: 'BTC-USD', side: 'buy', notional: 50000, venue: 'binance', kind: 'crypto' }, { council, risk: green, book: { bids: [[99.9, 10]], asks: [[100.1, 10]] } });
  assert.ok(thin.vetoes.includes('Order-Book Liquidity'));
});

test('replay sandbox carries second-by-second events with exact UTC windows', () => {
  const { Loki } = load('core.js', 'council.js', 'risk.js', 'replay.js');
  const hf = Loki.replay.EVENTS.filter((e) => e.interval === '1s');
  assert.ok(hf.length >= 4);
  for (const e of hf) { const span = Date.parse(e.end) - Date.parse(e.start); assert.ok(span > 0 && span <= 3000 * 1000, `${e.id} fits in 3,000 one-second bars`); assert.ok(Date.parse(e.start) >= Date.parse('2024-08-05T00:00:00Z'), 'Binance keeps 1s bars from Aug 2024'); }
});
