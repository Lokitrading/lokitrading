/* MONTE CARLO CORE — shared by the page and the background worker.
 *
 * simulate(plan) runs N independent paths of what could happen to a trade
 * between "send" and the horizon:
 *   1. a network/exchange delay (log-normal from the measured median and p95),
 *   2. the price drifting during that delay (scaled from the asset's own
 *      recent returns),
 *   3. the fill: walking a real order book whose depth is randomly thinned
 *      (liquidity can vanish exactly when you need it), or a spread + impact
 *      model when no book is available,
 *   4. fees, plus a gas-cost spike model for on-chain venues,
 *   5. the outcome at the horizon: block-bootstrapped from the asset's own
 *      return history (keeps fat tails and volatility clustering), or a
 *      win/lose draw at the fair probability for binary outcome tokens.
 * The confidence score is the share of paths that end with a profit after
 * every cost. Pure function, seeded RNG, no network.
 */
(function (root) {
  'use strict';
  function rng(seed) { let a = seed >>> 0 || 1; return function () { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function gauss(r) { let u = 0, v = 0; while (u === 0) u = r(); while (v === 0) v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
  function q(sorted, p) { if (!sorted.length) return NaN; const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i); return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo); }

  function walk(levels, qty, thin) {
    let left = qty, cost = 0;
    for (let i = 0; i < levels.length && left > 1e-12; i++) {
      const take = Math.min(left, levels[i][1] * thin);
      cost += take * levels[i][0]; left -= take;
    }
    if (left > 1e-12) { // book exhausted: the rest fills 2% worse than the last level
      const lastPrice = levels.length ? levels[levels.length - 1][0] : 0;
      cost += left * lastPrice * 1.02; return { avg: cost / qty, exhausted: true };
    }
    return { avg: cost / qty, exhausted: false };
  }

  /**
   * plan: {
   *   legs: [{ side: 'buy'|'sell', qty, price, kind: 'stock'|'crypto'|'future'|'binary',
   *            book?: {asks:[[p,q]], bids:[[p,q]]}, fee_bps?, fee_rate?, fair_p?, multiplier? }],
   *   returns: [log returns per bar], bar_seconds, horizon_bars,
   *   latency: { median_ms, p95_ms }, gas: { median_usd, spike_prob, spike_mult },
   *   paths, seed
   * }
   */
  function simulate(plan) {
    const N = Math.max(200, Math.min(20000, plan.paths || 5000));
    const r = rng(plan.seed || 7);
    const rets = (plan.returns || []).filter(Number.isFinite);
    const barSec = plan.bar_seconds || 86400;
    const H = Math.max(1, plan.horizon_bars || 1);
    const mu = rets.length ? rets.reduce((a, x) => a + x, 0) / rets.length : 0;
    const sd = rets.length > 1 ? Math.sqrt(rets.reduce((a, x) => a + (x - mu) ** 2, 0) / (rets.length - 1)) : 0.02;
    const perSec = sd / Math.sqrt(barSec);
    const lat = plan.latency || { median_ms: 250, p95_ms: 1200 };
    const lnMed = Math.log(Math.max(1, lat.median_ms)), lnSd = Math.max(0.05, Math.log(Math.max(lat.p95_ms, lat.median_ms + 1) / Math.max(1, lat.median_ms)) / 1.645);
    const gas = plan.gas || { median_usd: 0, spike_prob: 0, spike_mult: 1 };
    const BLOCK = 5;

    const pnl = new Float64Array(N), slip = new Float64Array(N), delays = new Float64Array(N);
    let exhausted = 0;
    for (let n = 0; n < N; n++) {
      const delayMs = Math.exp(lnMed + lnSd * gauss(r));
      delays[n] = delayMs;
      const drift = Math.exp(perSec * Math.sqrt(delayMs / 1000) * gauss(r));
      // horizon return by block bootstrap (or Gaussian when there is no history)
      let horizonLog = 0;
      if (rets.length > BLOCK * 2) {
        let left = H;
        while (left > 0) { const start = Math.floor(r() * (rets.length - BLOCK)); for (let k = 0; k < BLOCK && left > 0; k++, left--) horizonLog += rets[start + k]; }
      } else {
        horizonLog = mu * H + sd * Math.sqrt(H) * gauss(r);
      }
      // Flash-crash injection: with probability p the horizon also takes a jump (e.g. -8% ± 4%).
      if (plan.shock && r() < plan.shock.prob) horizonLog += plan.shock.mean + plan.shock.sd * gauss(r);
      let total = 0, slipBps = 0;
      for (const leg of plan.legs || []) {
        const dir = leg.side === 'sell' ? -1 : 1, qty = Math.abs(leg.qty), mult = leg.multiplier || 1;
        const thin = (plan.thin_lo ?? 0.35) + (plan.thin_span ?? 0.8) * r(); // stress tests pass a much thinner book
        let fill;
        if (leg.book && (dir > 0 ? leg.book.asks : leg.book.bids) && (dir > 0 ? leg.book.asks : leg.book.bids).length) {
          const side = dir > 0 ? leg.book.asks : leg.book.bids;
          const bb = leg.book.bids && leg.book.bids[0], ba = leg.book.asks && leg.book.asks[0];
          const mid0 = bb && ba ? (ba[0] + bb[0]) / 2 : side[0][0], widen = plan.spread_mult || 1; // news: quotes pull away from the mid
          const w = walk(side.map(([p, s]) => [(mid0 + (p - mid0) * widen) * (leg.kind === 'binary' ? 1 : drift), s]), qty, thin);
          fill = w.avg; if (w.exhausted) exhausted++;
        } else {
          const spreadBps = (leg.kind === 'stock' ? 2 : leg.kind === 'future' ? 1.5 : leg.kind === 'binary' ? 150 : 4) * (plan.spread_mult || 1);
          const impactBps = 8 * Math.sqrt(Math.max(0, qty * leg.price * mult) / 1e6) / thin;
          fill = leg.price * drift * (1 + dir * (spreadBps / 2 + impactBps) / 1e4);
        }
        if (leg.kind === 'binary') fill = Math.min(0.999, Math.max(0.001, fill));
        slipBps += Math.abs(fill / leg.price - 1) * 1e4 / (plan.legs.length || 1);
        let fee = 0;
        if (leg.fee_rate) fee = leg.fee_rate * fill * (1 - fill) * qty;            // Polymarket crypto fee curve
        else fee = (leg.fee_bps || 0) / 1e4 * fill * qty * mult;
        let exit;
        if (leg.kind === 'binary') exit = r() < (leg.fair_p ?? 0.5) ? 1 : 0;
        else exit = leg.price * Math.exp(horizonLog);
        total += dir * (exit - fill) * qty * mult - fee;
      }
      if (gas.median_usd) total -= gas.median_usd * (r() < (gas.spike_prob || 0) ? (gas.spike_mult || 5) * (0.5 + r()) : 0.6 + 0.8 * r());
      pnl[n] = total; slip[n] = slipBps;
    }
    const sorted = Array.from(pnl).sort((a, b) => a - b);
    const sSlip = Array.from(slip).sort((a, b) => a - b), sDelay = Array.from(delays).sort((a, b) => a - b);
    const wins = sorted.filter((x) => x > 0).length;
    const k = Math.max(1, Math.floor(N * 0.05));
    const tailMean = sorted.slice(0, k).reduce((a, x) => a + x, 0) / k;
    const lo = sorted[0], hi = sorted[N - 1], bins = 30, hist = new Array(bins).fill(0);
    for (const x of sorted) hist[Math.min(bins - 1, Math.floor((x - lo) / ((hi - lo) || 1) * bins))]++;
    return {
      paths: N, confidence: Math.round(100 * wins / N), prob_loss: 1 - wins / N,
      pnl: { mean: sorted.reduce((a, x) => a + x, 0) / N, p5: q(sorted, 0.05), p50: q(sorted, 0.5), p95: q(sorted, 0.95), worst: lo, best: hi },
      var95: -q(sorted, 0.05), cvar95: -tailMean,
      slippage_bps: { p50: q(sSlip, 0.5), p99: q(sSlip, 0.99) },
      delay_ms: { p50: q(sDelay, 0.5), p99: q(sDelay, 0.99) },
      book_exhausted_share: exhausted / N,
      histogram: { lo, hi, counts: hist },
    };
  }

  /**
   * Portfolio stress test: one horizon, N paths, joint bootstrap of whole
   * historical days across every symbol (keeps their real correlation).
   * plan: { positions: [{ id, value, returns: [aligned daily log returns] } | { id, kind: 'binary', qty, price, fair_p }],
   *         horizon_days, paths, seed }
   */
  function simulatePortfolio(plan) {
    const N = Math.max(500, Math.min(20000, plan.paths || 10000));
    const r = rng(plan.seed || 11), H = Math.max(1, plan.horizon_days || 1);
    const linear = (plan.positions || []).filter((p) => p.returns && p.returns.length > 20);
    const binary = (plan.positions || []).filter((p) => p.kind === 'binary');
    const days = linear.length ? Math.min(...linear.map((p) => p.returns.length)) : 0;
    const pnl = new Float64Array(N), contrib = linear.map(() => new Float64Array(N));
    for (let n = 0; n < N; n++) {
      let total = 0;
      for (let h = 0; h < H; h++) {
        const d = days ? Math.floor(r() * days) : 0;
        linear.forEach((p, k) => { const ret = p.returns[p.returns.length - days + d]; const x = p.value * (Math.exp(ret) - 1); contrib[k][n] += x; total += x; });
      }
      for (const b of binary) { const win = r() < (b.fair_p ?? b.price); total += b.qty * ((win ? 1 : 0) - b.price); }
      pnl[n] = total;
    }
    const sorted = Array.from(pnl).sort((a, b) => a - b);
    const k5 = Math.max(1, Math.floor(N * 0.05)), k1 = Math.max(1, Math.floor(N * 0.01));
    const tail = sorted.slice(0, k5).reduce((a, x) => a + x, 0) / k5;
    // each position's average P&L inside the worst 5% of paths = its share of the tail
    const order = Array.from(pnl.keys()).sort((a, b) => pnl[a] - pnl[b]).slice(0, k5);
    const shares = linear.map((p, k) => ({ id: p.id, tail: order.reduce((a, i) => a + contrib[k][i], 0) / k5 }));
    return { paths: N, var95: -q(sorted, 0.05), var99: -q(sorted, 0.01), cvar95: -tail, worst: sorted[0], median: q(sorted, 0.5), contributions: shares, k1 };
  }

  root.LokiMC = { simulate, simulatePortfolio };
})(typeof self !== 'undefined' ? self : this);
