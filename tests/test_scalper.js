// Memecoin scalper core: the same exits and breakers as loki/terminal (Python), triggers, and the AUD book.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load() {
  const ctx = { console, Math, Date, JSON, Number, BigInt, Map, Set, Array, Object, Error };
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const f of ['pumpfun_core.js', 'scalper_core.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/engine', f), 'utf8'), ctx, { filename: f });
  }
  return { S: ctx.LokiScalp, P: ctx.LokiPump };
}
const { S, P } = load();
const T0 = 1763600000;
const plain = (x) => JSON.parse(JSON.stringify(x));

test('exits match the Python plan: stop, half at 2x, moonbag trailing stop, stale, max hold', () => {
  let p = S.position(1, T0);
  assert.deepEqual(plain(S.step(p, 0.74, T0 + 5)), [[1, 'stop loss']]);
  p = S.position(1, T0, { staleAfterS: null });
  assert.deepEqual(plain(S.step(p, 2.0, T0 + 30)), [[0.5, 'take profit 2x']]);
  assert.deepEqual(plain(S.step(p, 3.0, T0 + 60)), []);
  assert.deepEqual(plain(S.step(p, 2.30, T0 + 80)), []);
  assert.deepEqual(plain(S.step(p, 2.20, T0 + 90)), [[0.5, 'trailing stop']]);
  p = S.position(1, T0, { staleAfterS: 120, staleMove: 0.05 });
  assert.deepEqual(plain(S.step(p, 1.02, T0 + 60)), []);
  assert.deepEqual(plain(S.step(p, 1.03, T0 + 121)), [[1, 'stale: no move']]);
  p = S.position(1, T0, { staleAfterS: 120, maxHoldS: 600, trail: null });
  S.step(p, 1.10, T0 + 30);
  assert.deepEqual(plain(S.step(p, 1.08, T0 + 601)), [[1, 'max hold time']]);
  p = S.position(1, T0, { tiers: [[2.0, 0.5], [4.0, 0.25]], staleAfterS: null });
  assert.deepEqual(plain(S.step(p, 4.5, T0 + 10)), [[0.5, 'take profit 2x'], [0.25, 'take profit 4x']]);
});

test('breakers: daily loss, failed transactions, caps; exits never blocked', () => {
  const b = new S.Breaker({ maxDailyLossAud: 50, maxTradeAud: 40 });
  assert.equal(b.canEnter(20, T0).ok, true);
  b.recordOpen(); b.recordClose(-30, T0);
  assert.match(b.canEnter(25, T0).why, /daily loss/);
  b.recordOpen(); b.recordClose(-25, T0);
  assert.match(b.tripped, /daily loss/);
  assert.equal(b.canEnter(5, T0 + 86400).ok, true);
  const f = new S.Breaker({ maxConsecutiveFailures: 3 });
  f.recordTx(false); f.recordTx(false); f.recordTx(true); f.recordTx(false); f.recordTx(false);
  assert.equal(f.tripped, null);
  f.recordTx(false);
  assert.match(f.tripped, /3 failed transactions/);
  assert.equal(f.canEnter(5, T0 + 86400).ok, false);
  f.reset();
  assert.equal(f.canEnter(5, T0).ok, true);
});

const VS0 = 30e9; const VT0 = 1073e12; const RT0 = 793e12;
function trade(m, at, sol, buyer, buy = true, vs = VS0, vt = VT0) {
  return { k: 't', m, at, sol: sol * 1e9, u: buyer, b: buy ? 1 : 0, tok: Math.round(sol * 3e13), vs, vt, rt: RT0, fee: 125 };
}

test('volume spike: 6x the usual minute with buyer growth and rising price fires; a quiet token does not', () => {
  const d = new S.SpikeDetector();
  let vs = VS0;
  for (let i = 0; i < 300; i += 30) d.add(trade('M', T0 + i, 0.2, `old${i % 3}`, true, vs += 1e8, VT0)); // ~0.4 SOL a minute, 3 wallets
  assert.equal(d.check('M', T0 + 299), null);
  for (let i = 0; i < 20; i += 1) d.add(trade('M', T0 + 300 + i * 2, 0.5, `new${i}`, true, vs += 5e8, VT0 - i * 1e11));
  const hit = d.check('M', T0 + 340);
  assert.ok(hit && /volume spike/.test(hit.reason) && hit.surge >= 6);
  const q = new S.SpikeDetector();
  for (let i = 0; i < 400; i += 10) q.add(trade('Q', T0 + i, 0.3, `w${i % 4}`));
  assert.equal(q.check('Q', T0 + 399), null);
});

test('whale radar mirrors only watched wallets buying at least the minimum', () => {
  const r = new S.WhaleRadar(['Whale1111111111111111111111111111111111111'], 0.5);
  assert.equal(r.check(trade('M', T0, 2, 'Someone')), null);
  assert.equal(r.check(trade('M', T0, 0.1, 'Whale1111111111111111111111111111111111111')), null);
  assert.equal(r.check(trade('M', T0, 2, 'Whale1111111111111111111111111111111111111', false)), null);
  assert.match(r.check(trade('M', T0, 2, 'Whale1111111111111111111111111111111111111')).reason, /whale Whal…1111 bought 2.00 SOL/);
});

test('holder concentration from the stream counts net balances', () => {
  const h = new S.Holders();
  h.add({ k: 't', m: 'M', u: 'a', b: 1, tok: 100e12 });
  h.add({ k: 't', m: 'M', u: 'a', b: 0, tok: 40e12 });
  h.add({ k: 't', m: 'M', u: 'b', b: 1, tok: 20e12 });
  assert.equal(Math.round(h.top10Pct('M')), 8); // (60e12 + 20e12) / 1e15 = 8%
});

test('the AUD book fills on the curve, charges every leg and reports true net', () => {
  const book = new S.Book({ startAud: 1000, txLamports: 105000, jitoTipLamports: 0 });
  const st = { vs: VS0, vt: VT0, rt: RT0, fee: 125 };
  const solAud = 100;
  const pos = book.open('M', 'MEME', 50, st, solAud, T0, {}, 'test');
  assert.ok(pos.tokens > 0 && Math.abs(book.cashAud - (1000 - 50 - 0.000105 * 100)) < 1e-9);
  // the same reserves straight back: the round trip loses the program fees both ways plus two tx fees
  const sold = book.sell('M', 1, { vs: VS0 + 0.5e9, vt: VS0 * VT0 / (VS0 + 0.5e9), rt: RT0, fee: 125 }, solAud, T0 + 10, 'test exit');
  assert.ok(sold.aud < 50 && sold.aud > 48);
  const rec = book.closed[0];
  assert.equal(book.positions.size, 0);
  assert.ok(Math.abs(rec.netAud - (rec.receivedAud - 50 - 2 * 0.0105)) < 1e-9 && rec.netAud < 0);
  assert.equal(book.stats().trades, 1);
});
