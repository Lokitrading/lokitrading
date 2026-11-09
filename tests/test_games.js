// Market Word and Beat the Bots: scoring rules, the daily word, and the word bank.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function el() {
  const node = { children: [], dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {} }, hidden: false, disabled: false,
    innerHTML: '', textContent: '', addEventListener() {}, setAttribute() {}, focus() {}, querySelectorAll: () => [], appendChild() {}, getContext: () => null };
  return node;
}
function load(file, extra = {}) {
  const nodes = {};
  const document = { getElementById: (id) => (nodes[id] ||= el()), createElement: () => el() };
  const store = {};
  const ctx = { console, Math, Date, JSON, document, CustomEvent: class { constructor(n, d) { this.detail = d && d.detail; } },
    localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } }, navigator: {}, setTimeout, ...extra };
  ctx.window = ctx; ctx.window.addEventListener = () => {}; ctx.window.dispatchEvent = () => {};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js', file), 'utf8'), ctx, { filename: file });
  return ctx;
}

test('market word scores greens before yellows and never double-counts a letter', () => {
  const { LokiMarketWord: MW } = load('market_word.js');
  const s = (g, w) => JSON.parse(JSON.stringify(MW.score(g, w)));
  assert.deepEqual(s('TREND', 'TREND'), ['correct', 'correct', 'correct', 'correct', 'correct']);
  assert.deepEqual(s('ALPHA', 'CHART'), ['present', 'absent', 'absent', 'present', 'absent']);
  // one A in the answer: only the first unmatched A is marked present
  assert.deepEqual(s('AABBB', 'XAXXX'), ['absent', 'correct', 'absent', 'absent', 'absent']);
  assert.deepEqual(s('SALSA', 'ASSET'), ['present', 'present', 'absent', 'present', 'absent']); // ASSET has two S's
});

test('market word bank: five capital letters, every answer has a meaning, daily word is stable', () => {
  const { LokiMarketWord: MW } = load('market_word.js');
  assert.ok(MW.WORDS.length >= 60);
  for (const w of MW.WORDS) { assert.match(w, /^[A-Z]{5}$/); assert.ok(MW.VALID.has(w)); }
  assert.equal(MW.dailyWord(42), MW.dailyWord(42));
  const week = new Set(Array.from({ length: 7 }, (_, i) => MW.dailyWord(100 + i)));
  assert.ok(week.size >= 5, 'consecutive days rarely repeat');
});

test('beat the bots pays double for sure calls and costs points when sure and wrong', () => {
  const { LokiBeatBots: BB } = load('beat_bots.js', { Loki: { council: { BOTS: [] }, fmt: { esc: (x) => x }, data: {} } });
  assert.equal(BB.points('up', false, true), 10);
  assert.equal(BB.points('up', true, true), 20);
  assert.equal(BB.points('down', false, true), 0);
  assert.equal(BB.points('down', true, true), -10);
  assert.equal(BB.points('down', true, false), 20);
});
