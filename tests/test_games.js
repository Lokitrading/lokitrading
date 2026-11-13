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

// --- Loki Dive: the on-device snapshot ----------------------------------------------
function loadDive(saved = {}) {
  const node = () => ({ children: [], dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    hidden: false, disabled: false, innerHTML: '', textContent: '', value: '', addEventListener() {}, removeEventListener() {},
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null, focus() {}, blur() {}, querySelectorAll: () => [], querySelector: () => null,
    appendChild() {}, append() {}, replaceChildren() {}, remove() {}, closest: () => null, getContext: () => null, scrollIntoView() {},
    getBoundingClientRect: () => ({ width: 300, height: 200, left: 0, top: 0 }) });
  const nodes = {}, store = { ...saved };
  const document = { getElementById: (id) => (nodes[id] ||= node()), createElement: node, querySelector: node, querySelectorAll: () => [],
    addEventListener() {}, body: node(), documentElement: node(), hidden: false, visibilityState: 'visible' };
  const ctx = { console, Math, Date, JSON, document, URL, URLSearchParams, Intl, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, navigator: {}, performance: { now: () => 0 }, location: { hash: '', pathname: '/', search: '' },
    matchMedia: () => ({ matches: false, addEventListener() {} }), fetch: async () => ({ ok: false, json: async () => ({}) }),
    CustomEvent: class { constructor(n, d) { this.detail = d && d.detail; } },
    localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    sessionStorage: { getItem: () => null, setItem() {} } };
  ctx.window = ctx; ctx.addEventListener = () => {}; ctx.dispatchEvent = () => {};
  vm.createContext(ctx);
  for (const file of ['game_data.js', 'game.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js', file), 'utf8'), ctx, { filename: file });
  return { dive: ctx.LokiDive, store };
}

test('loki dive saves its on-device snapshot, journal included, and reads it back', () => {
  const journal = { 'name a stock exchange': { best: 3, times: 2 }, 'name a candlestick pattern': { best: 1, times: 5 } };
  const { dive, store } = loadDive({ loki_dive_journal: JSON.stringify(journal) });
  assert.equal(dive.autosave(), true); // it used to throw inside and report false every time
  const snapshot = JSON.parse(store.loki_dive_last_open);
  assert.deepEqual(snapshot.journal, journal);
  assert.ok(snapshot.when && /saved automatically on this device/.test(snapshot.note));
  assert.equal(dive.lastSaved().when, snapshot.when);
  assert.equal(JSON.parse(JSON.stringify(dive.exportData())).saved_locally_on_this_device, true);
});

test('loki dive keeps only the newest journal entries in the snapshot', () => {
  const big = Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`prompt ${String(i).padStart(3, '0')}`, { best: 1, times: 1 }]));
  const { dive, store } = loadDive({ loki_dive_journal: JSON.stringify(big) });
  assert.equal(dive.autosave(), true);
  const kept = Object.keys(JSON.parse(store.loki_dive_last_open).journal);
  assert.equal(kept.length, 120);
  assert.equal(kept[0], 'prompt 030'); // the oldest thirty are dropped
});
