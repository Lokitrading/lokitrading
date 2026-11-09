// Red Team lab engines: quant statistics, the local fill engine, guardrails,
// the red team, the agent jury, journal tagging and stress maths.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ENGINE = path.join(__dirname, '../static/js/engine/');
function load(...files) {
  const mem = {};
  const storage = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); }, removeItem: (k) => { delete mem[k]; } };
  const ctx = { console, Math, Date, JSON, URL, URLSearchParams, AbortSignal, performance, setTimeout, clearTimeout, setInterval, clearInterval, Float64Array, Array, Map, Set, Promise,
    localStorage: storage, sessionStorage: storage, location: { href: 'https://x.test/', origin: 'https://x.test' }, fetch: async () => { throw new Error('offline'); } };
  ctx.window = ctx; ctx.self = ctx;
  vm.createContext(ctx);
  for (const f of files) vm.runInContext(fs.readFileSync(ENGINE + f, 'utf8'), ctx, { filename: f });
  return ctx;
}
function series(n, drift, vol = 0.01, seed = 1, start = 100) {
  let s = seed; const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  const g = () => Math.sqrt(-2 * Math.log(rnd())) * Math.cos(2 * Math.PI * rnd());
  const bars = []; let p = start;
  for (let i = 0; i < n; i++) { const o = p; p = p * Math.exp((typeof drift === 'function' ? drift(i) : drift) + vol * g()); bars.push({ t: 1.6e12 + i * 60000, o, h: Math.max(o, p) * 1.0005, l: Math.min(o, p) * 0.9995, c: p, v: 1000 }); }
  return bars;
}
const plain = (x) => JSON.parse(JSON.stringify(x));

test('quant: GARCH recovers volatility clustering, regimes are told apart, ratios are sane', () => {
  const { LokiQuant: Q } = load('quant.js');
  // Simulate a GARCH(1,1) with alpha .1 beta .85.
  const r = Q.rng(4); const rets = []; let s2 = 1e-4;
  for (let i = 0; i < 3000; i++) { const e = Math.sqrt(s2) * Q.gauss(r); rets.push(e); s2 = 1e-4 * 0.05 + 0.1 * e * e + 0.85 * s2; }
  const g = Q.garch(rets);
  assert.ok(g.alpha > 0.04 && g.alpha < 0.2, `alpha ${g.alpha}`); assert.ok(g.persistence > 0.85 && g.persistence < 0.999, `persistence ${g.persistence}`);
  const trend = Q.regime(series(400, 0.004, 0.004).map((b) => b.c));
  let p = 100; const mr = []; const rr = Q.rng(9); for (let i = 0; i < 400; i++) { p = 100 + (p - 100) * 0.5 + Q.gauss(rr); mr.push(p); }
  assert.equal(trend.character, 'trending'); assert.equal(Q.regime(mr).character, 'mean-reverting');
  const perf = Q.performance([0.01, -0.005, 0.01, -0.005, 0.01, -0.02, 0.01], 252);
  assert.ok(perf.sortino > perf.sharpe, 'Sortino only counts downside volatility'); assert.ok(perf.maxDD > 0.019 && perf.maxDD < 0.021);
  assert.ok(Math.abs(Q.corr([1, 2, 3, 4], [2, 4, 6, 8]) - 1) < 1e-12);
});

test('fill engine: limit orders wait in the queue, partial fills happen, impact and front-running apply', () => {
  const { LokiFill: F } = load('quant.js', 'fillsim.js');
  const flat = Array.from({ length: 40 }, (_, i) => ({ t: i * 60000, o: 100, h: 100.05, l: 100, c: 100.02, v: 50 }));
  // Touching 100 every bar (50 units print there per bar) with 120 units ahead: two bars clear the queue, the third fills us.
  const lim = F.fill({ side: 'buy', qty: 10, type: 'limit', limit: 100 }, flat, 1, { depth_at_touch: 120, seed: 1 });
  assert.ok(lim.events.some((e) => /still .* ahead in the queue/.test(e)));
  assert.equal(lim.fills[0].i, 3); assert.equal(lim.filledQty, 10);
  assert.ok(lim.fills.length >= 1 && lim.fills[0].i > 1, 'no fill on the first touch: we were behind the queue');
  const huge = F.fill({ side: 'buy', qty: 1000, type: 'limit', limit: 100 }, flat, 1, { depth_at_touch: 20, seed: 1, expire_bars: 5 });
  assert.equal(huge.partial, true, 'a big limit order only partially fills before it expires');
  const through = F.fill({ side: 'buy', qty: 10, type: 'limit', limit: 100.5 }, [...flat.slice(0, 2), { t: 9e5, o: 101, h: 101, l: 100.2, c: 100.3, v: 50 }], 1, { depth_at_touch: 1e9, seed: 1 });
  assert.equal(through.filledQty, 10, 'trading through the price fills regardless of the queue');
  const small = F.fill({ side: 'buy', qty: 1, type: 'market' }, flat, 5, { seed: 3 }), big = F.fill({ side: 'buy', qty: 40, type: 'market' }, flat, 5, { seed: 3 });
  assert.ok(big.slippageBps > small.slippageBps + 5, 'an order at 80% of bar volume pays impact');
  assert.equal(big.frontRun, true);
  const twap = F.fill({ side: 'buy', qty: 40, type: 'market', algo: 'twap', slices: 10, dark: true }, flat, 5, { seed: 3 });
  assert.ok(twap.slippageBps < big.slippageBps, 'slicing beats one big market order');
  assert.deepEqual(plain(F.fill({ side: 'sell', qty: 5, type: 'market' }, flat, 3, { seed: 7 })), plain(F.fill({ side: 'sell', qty: 5, type: 'market' }, flat, 3, { seed: 7 })), 'deterministic');
  const lat = F.fill({ side: 'buy', qty: 1, type: 'market' }, flat, 3, { seed: 11 }); assert.ok(lat.latencyMs >= 50 && lat.latencyMs <= 500);
});

test('fill engine replay: leveraged longs are liquidated in a crash, unlevered ones are not', () => {
  const { LokiFill: F } = load('quant.js', 'fillsim.js');
  const bars = series(300, (i) => (i > 150 && i < 170 ? -0.02 : 0.0005), 0.002);
  const longAlways = (i, b, st) => (st.qty <= 0 ? 'buy' : null);
  const lev = F.replay(longAlways, bars, { leverage: 4, maintenance: 0.25, seed: 5 }), cash = F.replay(longAlways, bars, { leverage: 1, seed: 5 });
  assert.ok(lev.liquidations >= 1 && lev.log.some((l) => /margin call/.test(l)));
  assert.equal(cash.liquidations, 0);
  assert.equal(F.replay(longAlways, bars, { leverage: 4, seed: 5 }).final, lev.final, 'replays are reproducible');
});

test('guardrails: three max-loss trades lock a strategy for 24h; drawdown shrinks size; news locks entries', () => {
  const { Loki } = load('core.js', 'quant.js', 'guard.js');
  for (let i = 0; i < 2; i++) Loki.guard.record('s1', { pnl: -50, ret: -0.05, exit: 'stop', maxLossPct: 5 });
  assert.equal(Loki.guard.status('s1').locked, false);
  Loki.guard.record('s1', { pnl: -50, ret: -0.05, exit: 'stop', maxLossPct: 5 });
  const st = Loki.guard.status('s1');
  assert.equal(st.locked, true); assert.ok(st.until - Date.now() > 23.9 * 3600e3);
  assert.equal(Loki.guard.canEnter('s1').ok, false);
  assert.equal(Loki.guard.sizeScale(0), 1); assert.equal(Loki.guard.sizeScale(0.1, 0.1), 0.25); assert.ok(Math.abs(Loki.guard.sizeScale(0.05, 0.1) - 0.625) < 1e-9);
  Loki.guard.addEvent('Test CPI', new Date(Date.now() + 10 * 60e3).toISOString());
  assert.equal(Loki.guard.newsLock().locked, true); assert.equal(Loki.guard.canEnter('fresh').ok, false);
  for (let i = 0; i < 6; i++) Loki.guard.record('loser', { pnl: -1, ret: -0.01 - i * 0.001, exit: 'rule' });
  assert.equal(Loki.guard.status('loser').quarantined, true, 'no positive risk-adjusted return → quarantine');
  assert.equal(Loki.guard.drift({ source: 't', symbol: 'X', side: 'buy', recommended: 'blocked' }), true);
});

test('red team: stress scenarios lower confidence, the devil argues the other side, safety is scored 0-100', async () => {
  const { Loki } = load('core.js', 'quant.js', 'mc_core.js', 'montecarlo.js', 'council.js', 'redteam.js');
  const bars = series(300, 0.002, 0.012, 3).map((b, i) => ({ ...b, t: 1.6e12 + i * 86400000 }));
  const p = { symbol: 'TEST', side: 'buy', qty: 10, price: bars[bars.length - 1].c, kind: 'stock' };
  const r = await Loki.redteam.run(p, { bars, dailyLossLimit: 300 });
  assert.equal(r.scenarios.length, 6);
  const base = r.scenarios[0], crash = r.scenarios.find((s) => s.id === 'flash-crash'), all = r.scenarios.find((s) => s.id === 'withdrawal');
  assert.ok(crash.var95 > base.var95 && all.var95 > base.var95, 'flash crash and liquidity withdrawal raise the tail loss');
  assert.ok(r.safety >= 0 && r.safety <= 100); assert.ok(r.expectedMaxDD > 0 && r.p95MaxDD >= r.expectedMaxDD);
  assert.equal(r.devil.side, 'sell'); assert.equal(typeof r.devil.bias, 'boolean');
  assert.equal(r.components.reduce((a, c) => a + c.weight, 0).toFixed(2), '1.00');
  // Shorting a strong uptrend: the devil's advocate (long) should look at least as good → bias flag.
  const short = await Loki.redteam.run({ ...p, side: 'sell' }, { bars, dailyLossLimit: 300 });
  assert.ok(short.devil.bias || short.safety < r.safety);
});

test('jury: the risk manager vetoes oversize trades no matter how the jury votes, and dissent is logged', async () => {
  const { Loki } = load('core.js', 'quant.js', 'mc_core.js', 'montecarlo.js', 'council.js', 'risk.js', 'copilot.js', 'guard.js', 'fillsim.js', 'redteam.js', 'jury.js');
  const bars = series(300, 0.002, 0.01, 5).map((b, i) => ({ ...b, t: 1.6e12 + i * 86400000 }));
  const price = bars[bars.length - 1].c;
  const big = await Loki.jury.evaluate({ symbol: 'TEST', side: 'buy', qty: 50000 / price, price, kind: 'stock' }, { bars, equity: 10000, council: null });
  assert.equal(big.risk.veto, true); assert.equal(big.approved, false); assert.match(big.postMortem, /KILLED by the Risk Manager veto/);
  assert.equal(big.jurors.length, 5); assert.equal(big.need, 4);
  const small = await Loki.jury.evaluate({ symbol: 'TEST', side: 'buy', qty: 100 / price, price, kind: 'stock' }, { bars, equity: 10000, council: null });
  assert.equal(small.risk.veto, false, small.risk.summary);
  assert.equal(small.approved, small.yes >= 4);
  assert.ok(small.crossExam && small.crossExam.n > 0 && /past setups/.test(small.crossExam.attack));
  assert.ok(Loki.jury.history().length >= 2, 'every decision is in the post-mortem log');
});

test('journal: losing trades are tagged by root cause; execution is audited against the plan', () => {
  const { Loki } = load('core.js', 'quant.js', 'guard.js', 'fillsim.js', 'paperbook.js');
  const t = { side: 'long', entry: 101, exit: 90, ret: -0.109, exitReason: 'stop', stopPct: 5, quantScore: -0.4, entryFill: { partial: true },
    plan: { signalPx: 100, news: { locked: false, minutesToNext: 20 }, volumeRatio: 0.5, efficiency: 0.1, volState: 'storm' } };
  const pm = Loki.paperbook.postMortem(t);
  for (const tag of ['late entry', 'missed stop', 'entered near major economic news', 'low-volume breakout', 'choppy consolidation', 'against the quant signal', 'storm volatility regime']) assert.ok(pm.tags.includes(tag), tag);
  const win = Loki.paperbook.postMortem({ side: 'long', entry: 100, exit: 105, ret: 0.05, exitReason: 'rule', plan: { signalPx: 100 } });
  assert.deepEqual(plain(win.tags), []);
});

test('stress maths: Cholesky reproduces the correlation matrix', () => {
  const { Loki } = load('core.js', 'quant.js', 'stress.js');
  const m = [[1, 0.6, -0.2], [0.6, 1, 0.1], [-0.2, 0.1, 1]], L = Loki.stress.cholesky(m);
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) { let s = 0; for (let k = 0; k < 3; k++) s += L[i][k] * L[j][k]; assert.ok(Math.abs(s - m[i][j]) < 1e-9); }
  assert.equal(Loki.stress.SHOCKS.length, 4);
});
