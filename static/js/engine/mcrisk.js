/* REAL-TIME MONTE CARLO RISK ENGINE — the open book, stress-tested continuously.
 *
 * Any part of the app that holds positions registers a provider (the trade
 * terminal for broker positions, the Polymarket paper bot for its tokens).
 * Every 5 seconds while a Loki tab is visible, 10,000 one-day paths are run in
 * the background worker: whole historical days are replayed jointly for all
 * symbols (so they move together as they really did) and binary tokens settle
 * at their fair probability. Out come VaR95/99, CVaR95 and each position's
 * share of the worst 5% of days. When VaR95 exceeds the co-pilot's daily loss
 * limit, a size multiplier (limit ÷ VaR) flows into the risk matrix, so every
 * new order shrinks automatically, and the trailing stop suggestion tightens.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.mcrisk) return;
  const PATHS = 10000, EVERY = 5000;
  const providers = new Map();
  const returnsCache = new Map();
  const S = { last: null, multiplier: 1, running: false, error: null };

  async function returnsFor(symbol) {
    const hit = returnsCache.get(symbol);
    if (hit && Date.now() - hit.at < 3600e3) return hit.r;
    const bars = await Loki.data.bars(symbol, { interval: '1d', limit: 300 });
    const r = Loki.math.logReturns(bars.map((b) => b.c)).slice(-250);
    returnsCache.set(symbol, { at: Date.now(), r });
    return r;
  }

  async function tick() {
    if (document.hidden || S.running || !providers.size) return;
    S.running = true;
    try {
      let equity = 0;
      const positions = [];
      for (const [name, fn] of providers) {
        const got = await fn();
        if (!got) continue;
        equity += got.equity || 0;
        for (const p of got.positions || []) positions.push({ ...p, id: `${name}:${p.id || p.symbol}` });
      }
      if (!positions.length) { S.last = { positions: 0, equity }; S.multiplier = 1; Loki.emit('mcrisk:update', snapshot()); return; }
      const linear = [];
      for (const p of positions.filter((x) => x.kind !== 'binary')) {
        try { linear.push({ id: p.id, value: p.value, returns: await returnsFor(p.symbol) }); } catch { /* symbol without history: excluded and reported */ }
      }
      const binary = positions.filter((x) => x.kind === 'binary');
      const res = await Loki.mc.run({ positions: [...linear, ...binary], horizon_days: 1, paths: PATHS, seed: Date.now() % 1e6 }, 'portfolio');
      const cfg = Loki.config(), limit = equity > 0 ? equity * cfg.daily_loss_limit_pct / 100 : null;
      S.multiplier = limit && res.var95 > limit ? Math.max(0.1, limit / res.var95) : 1;
      S.last = { ...res, equity, limit, positions: positions.length, excluded: positions.length - linear.length - binary.length, at: Date.now(),
        suggestion: S.multiplier < 1 ? `VaR95 ${Loki.fmt.usd(res.var95)} is above your ${cfg.daily_loss_limit_pct}% daily loss limit (${Loki.fmt.usd(limit)}): new orders are sized ×${S.multiplier.toFixed(2)} and a trailing stop near ${(cfg.trailing_stop_pct * S.multiplier).toFixed(1)}% would bring it inside.` : 'Within your daily loss limit.' };
      S.error = null;
    } catch (error) { S.error = String(error.message || error); }
    finally { S.running = false; Loki.emit('mcrisk:update', snapshot()); }
  }

  function snapshot() { return { ...S.last, multiplier: S.multiplier, error: S.error, providers: [...providers.keys()] }; }
  setInterval(tick, EVERY);

  Loki.mcrisk = {
    register(name, fn) { providers.set(name, fn); },
    unregister(name) { providers.delete(name); },
    multiplier: () => S.multiplier,
    snapshot, tick,
  };
})();
