/* LOKI ENGINE CORE — shared by every on-device engine (council, swarm, risk,
 * Monte Carlo, arbitrage, replay, audit, co-pilot, 3D map, trade terminal).
 *
 * Most of Loki's thinking now happens on the visitor's device. The server is
 * used only where a browser cannot go itself (Yahoo stock data, Tradovate,
 * Loki's own chat brain). This file holds the maths, safe storage, an event
 * bus, market data adapters and a network tap that lets the execution map
 * show every real request as it happens.
 */
'use strict';
(() => {
  if (window.Loki && window.Loki.ready) return;
  const Loki = (window.Loki = window.Loki || {});

  /* ------------------------------------------------------------ events */
  const listeners = new Map();
  Loki.on = (name, fn) => { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); return () => listeners.get(name).delete(fn); };
  Loki.emit = (name, detail) => {
    (listeners.get(name) || []).forEach((fn) => { try { fn(detail); } catch (error) { console.error('[loki]', name, error); } });
  };

  /* ----------------------------------------------------------- storage */
  Loki.store = {
    get(key, fallback) {
      try { const raw = localStorage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
    },
    session: {
      get(key, fallback) { try { const raw = sessionStorage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; } },
      set(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* blocked */ } },
      del(key) { try { sessionStorage.removeItem(key); } catch { /* blocked */ } },
    },
  };

  /* -------------------------------------------------------------- maths */
  const M = (Loki.math = {});
  M.clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  M.mean = (a) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0);
  M.std = (a) => { if (a.length < 2) return 0; const m = M.mean(a); return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1)); };
  M.quantile = (sorted, q) => { if (!sorted.length) return NaN; const i = (sorted.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i); return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo); };
  M.erf = (x) => { // Abramowitz-Stegun 7.1.26
    const s = Math.sign(x); x = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return s * y;
  };
  M.normCdf = (x) => 0.5 * (1 + M.erf(x / Math.SQRT2));
  M.rng = (seed) => { let a = seed >>> 0 || 1; return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; };
  M.gauss = (rand) => { let u = 0, v = 0; while (u === 0) u = rand(); while (v === 0) v = rand(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  M.logReturns = (closes) => { const out = []; for (let i = 1; i < closes.length; i++) if (closes[i] > 0 && closes[i - 1] > 0) out.push(Math.log(closes[i] / closes[i - 1])); return out; };
  M.ema = (values, n) => { const k = 2 / (n + 1); const out = []; let e = values[0]; for (const v of values) { e = v * k + e * (1 - k); out.push(e); } return out; };
  M.sma = (values, n) => values.length < n ? NaN : M.mean(values.slice(-n));
  M.rsi = (closes, n = 14) => {
    if (closes.length <= n) return 50;
    let gain = 0, loss = 0;
    for (let i = closes.length - n; i < closes.length; i++) { const d = closes[i] - closes[i - 1]; if (d > 0) gain += d; else loss -= d; }
    if (loss === 0) return 100;
    return 100 - 100 / (1 + gain / loss);
  };
  M.logLoss = (p, y) => { const q = M.clamp(p, 1e-4, 1 - 1e-4); return -(y ? Math.log(q) : Math.log(1 - q)); };

  /* ----------------------------------------------------------- formats */
  Loki.fmt = {
    esc: (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    pct: (p, d = 1) => (Number.isFinite(p) ? `${(p * 100).toFixed(d)}%` : '—'),
    num: (v, d = 2) => (Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: Math.min(d, 2) }) : '—'),
    usd: (v, signed = false) => (Number.isFinite(v) ? `${signed && v > 0 ? '+' : ''}${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}` : '—'),
    time: (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    ago: (t) => { const s = Math.round((Date.now() - t) / 1000); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`; },
  };

  /* -------------------------------------------------------- network tap
   * Every request the page makes is reported (host, method, status, ms) so
   * the execution map can light up real traffic. Request bodies, headers and
   * keys are never recorded. */
  const hostLabel = (url) => {
    try {
      const u = new URL(url, location.href);
      if (u.origin === location.origin) return 'loki';
      return u.hostname;
    } catch { return 'unknown'; }
  };
  Loki.hostLabel = hostLabel;
  if (!window.__lokiFetchTapped && window.fetch) {
    window.__lokiFetchTapped = true;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input && input.url;
      const method = (init && init.method) || (input && input.method) || 'GET';
      const started = performance.now();
      const host = hostLabel(url || '');
      Loki.emit('net', { phase: 'start', host, method, path: safePath(url) });
      try {
        const res = await nativeFetch(input, init);
        Loki.emit('net', { phase: 'end', host, method, path: safePath(url), status: res.status, ms: Math.round(performance.now() - started) });
        return res;
      } catch (error) {
        Loki.emit('net', { phase: 'end', host, method, path: safePath(url), status: 0, ms: Math.round(performance.now() - started), error: true });
        throw error;
      }
    };
  }
  function safePath(url) { try { return new URL(url, location.href).pathname.slice(0, 60); } catch { return ''; } }

  /* ------------------------------------------------------------- JSON IO */
  Loki.getJSON = async (url, { timeout = 15000, headers } = {}) => {
    const res = await fetch(url, { cache: 'no-store', headers, signal: AbortSignal.timeout(timeout) });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && (typeof data.detail === 'string' ? data.detail : data.message)) || `HTTP ${res.status}`);
    return data;
  };
  Loki.postJSON = async (url, body, { timeout = 20000, headers = {} } = {}) => {
    const res = await fetch(url, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body), signal: AbortSignal.timeout(timeout) });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && (typeof data.detail === 'string' ? data.detail : data.message)) || `HTTP ${res.status}`);
    return data;
  };

  /* -------------------------------------------------------- market data
   * Crypto comes straight from Binance's public market-data host (CORS-open)
   * on the device; stocks and indices go through Loki's cached relay. */
  const CRYPTO = { 'BTC-USD': 'BTCUSDT', 'ETH-USD': 'ETHUSDT', 'SOL-USD': 'SOLUSDT', 'XRP-USD': 'XRPUSDT', 'BNB-USD': 'BNBUSDT', 'DOGE-USD': 'DOGEUSDT', 'ADA-USD': 'ADAUSDT', 'AVAX-USD': 'AVAXUSDT', 'LTC-USD': 'LTCUSDT' };
  Loki.data = {
    CRYPTO,
    isCrypto: (symbol) => Boolean(CRYPTO[symbol] || /USDT$/.test(symbol)),
    binancePair: (symbol) => CRYPTO[symbol] || (/USDT$/.test(symbol) ? symbol : null),
    parseKlines: (rows) => (Array.isArray(rows) ? rows : []).map((k) => ({ t: k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5] })).filter((b) => b.c > 0),
    async binanceKlines(pair, interval = '1h', limit = 500, startTime, endTime) {
      const qs = new URLSearchParams({ symbol: pair, interval, limit: String(Math.min(1000, limit)) });
      if (startTime) qs.set('startTime', String(startTime));
      if (endTime) qs.set('endTime', String(endTime));
      return Loki.data.parseKlines(await Loki.getJSON(`https://data-api.binance.vision/api/v3/klines?${qs}`));
    },
    async binanceDepth(pair, limit = 50) {
      const d = await Loki.getJSON(`https://data-api.binance.vision/api/v3/depth?symbol=${pair}&limit=${limit}`);
      return { bids: (d.bids || []).map(([p, q]) => [+p, +q]), asks: (d.asks || []).map(([p, q]) => [+p, +q]) };
    },
    /** Daily (or intraday) bars for any symbol, oldest first: [{t,o,h,l,c,v}] */
    async bars(symbol, { interval = '1d', limit = 300 } = {}) {
      const pair = Loki.data.binancePair(symbol);
      if (pair) return Loki.data.binanceKlines(pair, interval, limit);
      const tf = { '1d': '1d', '1h': '1h', '15m': '15m', '5m': '5m', '1m': '1m' }[interval] || '1d';
      const data = await Loki.getJSON(`/api/fusion/ai/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}&limit=${Math.min(300, limit)}`);
      return (data.candles || []).map((b) => ({ t: Date.parse(b.t) || 0, o: +b.o, h: +b.h, l: +b.l, c: +b.c, v: +(b.v || 0) })).filter((b) => b.c > 0 && b.t > 0);
    },
  };

  Loki.ready = true;
  Loki.emit('ready', {});
})();
