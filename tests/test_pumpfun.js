// pump.fun bot core in the browser: it must price, decide and paper-trade exactly as the tested Python bot.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const FX = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/pumpfun_events.json'), 'utf8'));
function load() {
  const ctx = { console, Math, Date, JSON, Number, BigInt, Map, Set, Array, Object };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/engine/pumpfun_core.js'), 'utf8'), ctx, { filename: 'pumpfun_core.js' });
  return ctx.LokiPump;
}
const P = load();
const plain = (x) => JSON.parse(JSON.stringify(x));

test('curve maths matches the Python bot to the unit', () => {
  for (const c of FX.math) {
    assert.equal(P.buyTokens(c.vsol, c.vtok, c.rtok, c.lamports, c.fee), c.tokens);
    assert.equal(P.sellSol(c.vsol, c.vtok, c.tokens, c.fee), c.sell);
    assert.ok(Math.abs(P.price(c.vsol, c.vtok) - c.price) < 1e-18);
  }
});

test('a round trip including our own buy costs about the two fees', () => {
  const stake = 1e9; const fee = 125; const spend = stake * 10000 / (10000 + fee);
  const tokens = P.buyTokens(30e9, 1073e12, P.START_RTOKENS, stake, fee);
  const r = P.saleReturn({ vs: 30e9, vt: 1073e12, fee }, tokens, { stake_sol: 1, tx_cost_sol: 0 }, spend);
  assert.ok(Math.abs(r + 0.025) < 0.002, `round trip ${r}`);
});

test('the live entry test agrees with the backtest, and says why', () => {
  const { rule, features, expected } = FX.rule_cases;
  assert.deepEqual(plain(features.map((f) => P.decide(f, rule))), expected);
  assert.equal(P.why(features[1], rule), 'Mayhem mode');
  assert.equal(P.why(features[2], rule), 'too old');
  assert.equal(P.why(features[3], rule), 'creator sold');
  assert.equal(P.why(features[0], rule), 'meets the rule');
});

test('features: buyers and flow over 30 seconds, creator sales, launch-block bundle', () => {
  const t = new P.Tracker('dev', 0, 10, false);
  const up = (s, buy, sol, user, tokens) => t.update(s, s < 1 ? 10 : 20, buy, sol, user, tokens, 31e9, 1043e12, 763e12);
  up(0.5, true, 1e9, 'dev', 3e13);
  up(2, true, 2e9, 'a', 5e13);
  const f = up(3, true, 1e9, 'b', 2e13);
  assert.equal(f.buyers30, 2); assert.equal(f.flow30, 4); assert.equal(f.dev_sold, 0); assert.ok(f.bundle > 0);
  const g = up(4, false, 5e8, 'dev', 1e13);
  assert.equal(g.dev_sold, 1); assert.equal(g.flow30, 3.5);
  assert.equal(up(40, true, 1e8, 'c', 1e12).buyers30, 1);
});

test('the paper bot trades a stream exactly as LivePaper does in Python', () => {
  const pp = FX.paper_parity;
  const bot = new P.PaperBot(pp.rule, pp.costs);
  for (const row of pp.rows) bot.handleRow(row);
  assert.equal(bot.failed, pp.failed);
  assert.deepEqual(plain([...bot.positions.keys()].sort()), pp.open);
  assert.equal(bot.closed.length, pp.closed.length);
  bot.closed.forEach((t, i) => {
    assert.equal(t.symbol, pp.closed[i].symbol);
    assert.equal(t.reason, pp.closed[i].reason);
    assert.ok(Math.abs(t.ret - pp.closed[i].return) < 1e-5, `${t.symbol}: ${t.ret} vs ${pp.closed[i].return}`);
  });
  const reasons = new Set(bot.closed.map((t) => t.reason));
  for (const r of ['take profit', 'stop loss', 'creator sold', 'time stop', 'graduated', 'trailing stop']) assert.ok(reasons.has(r), r);
});

test('stopping sells what is still open at its last price', () => {
  const pp = FX.paper_parity;
  const bot = new P.PaperBot(pp.rule, pp.costs);
  for (const row of pp.rows.slice(0, 8)) bot.handleRow(row);
  assert.ok(bot.positions.size > 0);
  bot.stopAll('bot stopped');
  assert.equal(bot.positions.size, 0);
  assert.equal(bot.closed[bot.closed.length - 1].reason, 'bot stopped');
});

test('the learned filter scores exactly as the Python model, and gates entries', () => {
  const { model, features, scores } = FX.model_case;
  features.forEach((f, i) => assert.ok(Math.abs(P.score(model, f) - scores[i]) < 1e-8, `score ${i}`));
  const open = { age_max: 1e9, prog_lo: 0, prog_hi: 1, buyers_min: 0, flow_min: -1e9, bundle_max: 1, top_max: 1, dev_must_hold: false, mayhem: 'any', model };
  features.forEach((f, i) => assert.equal(P.decide(f, open), scores[i] >= model.threshold));
});
