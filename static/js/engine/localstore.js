/* LOCAL-FIRST DATA LAYER — columnar bar cache, offline mode and a speed benchmark.
 *
 * Every bar series the engine downloads is also stored in IndexedDB as columns
 * of typed arrays (t, o, h, l, c, v as Float64Array), the same layout columnar
 * databases use. With Offline mode on, the engine reads only from this cache:
 * no network, no API cost, no cloud latency; missing data fails clearly
 * instead of silently fetching.
 * The benchmark runs vectorised indicator passes (SMA, EMA, RSI, rolling σ)
 * over millions of rows on this CPU and reports rows per second.
 * Honest limits: this is plain JavaScript typed arrays, not DuckDB or Parquet
 * (DuckDB-WASM is a ~30 MB download) and not explicit SIMD; and no language
 * model runs locally (a quantised model is a multi-gigabyte download).
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.localstore) return;
  const DB = 'loki-bars', STORE = 'series', MAX = 400;
  let offline = Loki.store.get('loki_offline_mode_v1', false);
  const open = () => new Promise((res, rej) => { const q = indexedDB.open(DB, 1); q.onupgradeneeded = () => q.result.createObjectStore(STORE); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
  const tx = async (mode, fn) => { const db = await open(); return new Promise((res, rej) => { const t = db.transaction(STORE, mode), st = t.objectStore(STORE); const out = fn(st); t.oncomplete = () => res(out && out.result !== undefined ? out.result : out); t.onerror = () => rej(t.error); }); };

  const toCols = (bars) => { const n = bars.length, c = { t: new Float64Array(n), o: new Float64Array(n), h: new Float64Array(n), l: new Float64Array(n), c: new Float64Array(n), v: new Float64Array(n) }; bars.forEach((b, i) => { c.t[i] = b.t; c.o[i] = b.o; c.h[i] = b.h; c.l[i] = b.l; c.c[i] = b.c; c.v[i] = b.v || 0; }); return c; };
  const fromCols = (c) => Array.from(c.t, (t, i) => ({ t, o: c.o[i], h: c.h[i], l: c.l[i], c: c.c[i], v: c.v[i] }));

  async function put(key, bars) {
    if (!bars || bars.length < 50 || typeof indexedDB === 'undefined') return; // live polls of a few bars are not worth a disk write
    try { await tx('readwrite', (st) => st.put({ key, at: Date.now(), n: bars.length, cols: toCols(bars) }, key)); prune(); } catch { /* storage full or blocked: caching is best-effort */ }
  }
  async function get(key) { try { const r = await new Promise((res, rej) => { open().then((db) => { const q = db.transaction(STORE).objectStore(STORE).get(key); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); }).catch(rej); }); return r ? fromCols(r.cols) : null; } catch { return null; } }
  async function list() {
    try { const db = await open(); return await new Promise((res) => { const out = []; const q = db.transaction(STORE).objectStore(STORE).openCursor(); q.onsuccess = () => { const c = q.result; if (!c) return res(out.sort((a, z) => z.at - a.at)); out.push({ key: c.key, n: c.value.n, at: c.value.at, bytes: c.value.n * 48 }); c.continue(); }; q.onerror = () => res(out); }); } catch { return []; }
  }
  async function prune() { const all = await list(); if (all.length <= MAX) return; const old = all.slice(MAX); await tx('readwrite', (st) => old.forEach((x) => st.delete(x.key))); }
  async function clear() { await tx('readwrite', (st) => st.clear()); }

  // Wrap the engine's data functions: cache every download; in offline mode, serve only from cache.
  const wrap = (name, keyOf) => {
    const original = Loki.data[name];
    if (!original || original.__wrapped) return;
    const f = async (...args) => {
      const key = keyOf(...args);
      if (offline) { const hit = await get(key); if (hit) return hit; throw new Error(`Offline mode: ${key} is not in the local cache yet. Turn offline mode off once to download it.`); }
      const bars = await original.apply(Loki.data, args);
      put(key, bars);
      return bars;
    };
    f.__wrapped = true; Loki.data[name] = f;
  };
  wrap('binanceKlines', (pair, interval = '1h', limit = 500, startTime, endTime) => `binance:${pair}:${interval}:${limit}:${startTime || ''}:${endTime || ''}`);
  wrap('bars', (symbol, { interval = '1d', limit = 300 } = {}) => `bars:${symbol}:${interval}:${limit}`);
  if (Loki.replay && Loki.replay.loadBars && !Loki.replay.loadBars.__wrapped) {
    const orig = Loki.replay.loadBars;
    const f = async (ev) => { const key = `replay:${ev.id}`; const hit = await get(key); if (hit) return hit; if (offline) throw new Error(`Offline mode: replay "${ev.name}" is not cached yet.`); const bars = await orig(ev); put(key, bars); return bars; };
    f.__wrapped = true; Loki.replay.loadBars = f;
  }

  function setOffline(on) { offline = Boolean(on); Loki.store.set('loki_offline_mode_v1', offline); Loki.emit('localstore:mode', { offline }); }

  /** Vectorised indicator passes over `rows` synthetic minute bars; returns rows per second. */
  function benchmark(rows = 2_000_000) {
    const c = new Float64Array(rows); let p = 100, s = 12345;
    for (let i = 0; i < rows; i++) { s = (s * 16807) % 2147483647; p *= 1 + (s / 2147483647 - 0.5) * 0.002; c[i] = p; }
    const t0 = performance.now();
    const sma = new Float64Array(rows), ema = new Float64Array(rows), sd = new Float64Array(rows), rsi = new Float64Array(rows);
    let sum = 0, sum2 = 0, e = c[0], g = 0, l = 0; const k = 2 / 21, n = 20;
    for (let i = 0; i < rows; i++) {
      const x = c[i]; sum += x; sum2 += x * x;
      if (i >= n) { const y = c[i - n]; sum -= y; sum2 -= y * y; }
      const m = sum / Math.min(n, i + 1); sma[i] = m; sd[i] = Math.sqrt(Math.max(0, sum2 / Math.min(n, i + 1) - m * m));
      e = x * k + e * (1 - k); ema[i] = e;
      if (i) { const d = x - c[i - 1]; g = (g * 13 + Math.max(0, d)) / 14; l = (l * 13 + Math.max(0, -d)) / 14; rsi[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); }
    }
    const ms = performance.now() - t0;
    return { rows, ms, rowsPerSec: rows / (ms / 1000), indicators: 4, tenYearsMinuteSeconds: (10 * 252 * 390) / (rows / (ms / 1000)), check: sma[rows - 1] + ema[rows - 1] + sd[rows - 1] + rsi[rows - 1] };
  }

  Loki.localstore = { put, get, list, clear, setOffline, isOffline: () => offline, benchmark, toCols, fromCols };
})();
