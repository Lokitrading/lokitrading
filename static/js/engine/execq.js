/* EXECUTION QUALITY MONITOR — self-healing routing for the on-device engine.
 *
 * Every real request the page makes is timed by the network tap (core.js).
 * This module keeps a rolling window per venue (last 40 calls or 5 minutes):
 * error rate, median and p95 latency. A venue is HEALTHY, DEGRADED (slow or
 * a few errors) or DOWN (mostly failing). The risk matrix and the consensus
 * gate read this before any order: orders to a DOWN venue are paused (state
 * is kept, nothing is dropped), DEGRADED venues halve size, and price feeds
 * re-route automatically: Binance ↔ Coinbase for crypto spot.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.execq) return;
  const WINDOW = 40, MAX_AGE = 5 * 60000;
  const VENUE_OF = (host, path) => {
    if (host === 'loki') return path.startsWith('/api/broker/tradovate') ? 'tradovate' : 'loki';
    if (host.includes('binance')) return 'binance';
    if (host.includes('coinbase')) return 'coinbase';
    if (host.startsWith('gamma-api')) return 'polymarket-odds';
    if (host.startsWith('clob') || host.startsWith('ws-subscriptions')) return 'polymarket-clob';
    if (host.startsWith('data-api.polymarket')) return 'polymarket-data';
    if (host.startsWith('paper-api.alpaca')) return 'alpaca-paper';
    if (host.includes('alpaca')) return 'alpaca-live';
    return null;
  };
  const samples = {};
  Loki.on('net', (e) => {
    if (e.phase !== 'end') return;
    const v = VENUE_OF(e.host, e.path || '');
    if (!v) return;
    const list = (samples[v] ||= []);
    list.push({ t: Date.now(), ms: e.ms, ok: e.status > 0 && e.status < 500 && e.status !== 429 });
    if (list.length > WINDOW) list.splice(0, list.length - WINDOW);
  });

  function stats(v) {
    const now = Date.now();
    const list = (samples[v] || []).filter((s) => now - s.t < MAX_AGE);
    if (!list.length) return { venue: v, n: 0, state: 'unknown', errorRate: 0, p50: null, p95: null };
    const ms = list.map((s) => s.ms).sort((a, z) => a - z);
    const errorRate = list.filter((s) => !s.ok).length / list.length;
    const p50 = ms[Math.floor(ms.length * 0.5)], p95 = ms[Math.min(ms.length - 1, Math.floor(ms.length * 0.95))];
    const state = list.length >= 3 && errorRate >= 0.5 ? 'down' : errorRate >= 0.15 || p95 > 3000 ? 'degraded' : 'healthy';
    return { venue: v, n: list.length, state, errorRate, p50, p95 };
  }
  const all = () => Object.keys(samples).map(stats);

  /** Multiplier for order size on a venue: 1 healthy/unknown, 0.5 degraded, 0 down. */
  const sizeFactor = (v) => ({ healthy: 1, unknown: 1, degraded: 0.5, down: 0 }[stats(v).state]);

  /** Crypto spot with automatic failover: Binance first, Coinbase when Binance is down or stalled. */
  async function spot(pair = 'BTCUSDT') {
    const cbId = pair.replace(/USDT$/, '-USD');
    const tryBinance = async () => {
      const k = await Loki.data.binanceKlines(pair, '1s', 5);
      const last = k[k.length - 1];
      if (!last || Date.now() - last.t > 15000 || k.every((b) => !(b.v > 0))) throw new Error('binance stalled');
      return { price: last.c, source: 'binance', at: last.t };
    };
    const tryCoinbase = async () => {
      const t = await Loki.getJSON(`https://api.exchange.coinbase.com/products/${cbId}/ticker`, { timeout: 6000 });
      return { price: Number(t.price), source: 'coinbase', at: Date.parse(t.time) || Date.now() };
    };
    const order = stats('binance').state === 'down' ? [tryCoinbase, tryBinance] : [tryBinance, tryCoinbase];
    let lastError = null;
    for (const fn of order) {
      try { const r = await fn(); Loki.emit('execq:route', r); return r; } catch (error) { lastError = error; }
    }
    throw lastError || new Error('no spot source answered');
  }

  Loki.execq = { stats, all, sizeFactor, spot, VENUE_OF };
})();
