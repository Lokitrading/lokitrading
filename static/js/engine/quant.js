/* QUANT LIBRARY — pure statistics shared by the red team, the agent jury,
 * regime detection, the cross-asset matrix, stress tests and the journal.
 * Everything works on plain arrays / typed arrays, so it runs the same in the
 * page, in the worker and in node tests. No randomness without a seed.
 */
'use strict';
(function (root) {
  const Q = {};
  Q.rng = function (seed) { let a = seed >>> 0 || 1; return () => { a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; };
  Q.gauss = (r) => { let u = 0, v = 0; while (u === 0) u = r(); while (v === 0) v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  Q.mean = (a) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return a.length ? s / a.length : 0; };
  Q.std = (a) => { if (a.length < 2) return 0; const m = Q.mean(a); let s = 0; for (let i = 0; i < a.length; i++) s += (a[i] - m) ** 2; return Math.sqrt(s / (a.length - 1)); };
  Q.logReturns = (c) => { const out = new Float64Array(Math.max(0, c.length - 1)); for (let i = 1; i < c.length; i++) out[i - 1] = Math.log(c[i] / c[i - 1]); return out; };
  Q.quantile = (sorted, p) => { if (!sorted.length) return NaN; const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i); return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo); };

  /** Pearson correlation of two equal-length arrays. */
  Q.corr = (a, b) => {
    const n = Math.min(a.length, b.length); if (n < 3) return NaN;
    let ma = 0, mb = 0; for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; } ma /= n; mb /= n;
    let sab = 0, saa = 0, sbb = 0; for (let i = 0; i < n; i++) { const x = a[i] - ma, y = b[i] - mb; sab += x * y; saa += x * x; sbb += y * y; }
    return saa > 0 && sbb > 0 ? sab / Math.sqrt(saa * sbb) : NaN;
  };

  /** Peak-to-trough drawdown of an equity (or price) series, as a positive fraction. */
  Q.maxDrawdown = (eq) => { let peak = -Infinity, dd = 0; for (let i = 0; i < eq.length; i++) { if (eq[i] > peak) peak = eq[i]; if (peak > 0) dd = Math.max(dd, 1 - eq[i] / peak); } return dd; };

  /** Risk-adjusted performance from per-period returns. periodsPerYear: 252 daily, 8760 hourly… */
  Q.performance = (rets, periodsPerYear = 252) => {
    const r = Array.from(rets).filter(Number.isFinite);
    if (r.length < 2) return { n: r.length, sharpe: null, sortino: null, calmar: null, cagr: null, maxDD: 0, total: 0 };
    const m = Q.mean(r), s = Q.std(r);
    const downside = Math.sqrt(r.reduce((a, x) => a + Math.min(0, x) ** 2, 0) / r.length);
    const eq = [1]; for (const x of r) eq.push(eq[eq.length - 1] * (1 + x));
    const total = eq[eq.length - 1] - 1, maxDD = Q.maxDrawdown(eq);
    const years = r.length / periodsPerYear, cagr = years > 0 && eq[eq.length - 1] > 0 ? Math.pow(eq[eq.length - 1], 1 / years) - 1 : null;
    return { n: r.length, total, maxDD, cagr,
      sharpe: s > 0 ? (m / s) * Math.sqrt(periodsPerYear) : null,
      sortino: downside > 0 ? (m / downside) * Math.sqrt(periodsPerYear) : null,
      calmar: maxDD > 0 && cagr != null ? cagr / maxDD : null };
  };

  /**
   * GARCH(1,1) by maximum likelihood (coarse grid, then coordinate refinement).
   * σ²_t = ω + α ε²_{t-1} + β σ²_{t-1}, with ω set by variance targeting.
   * Returns parameters, the conditional volatility path and a one-step forecast.
   */
  Q.garch = (retsIn) => {
    const r = Array.from(retsIn).filter(Number.isFinite);
    const n = r.length; if (n < 50) return null;
    const m = Q.mean(r), e = r.map((x) => x - m), v0 = e.reduce((a, x) => a + x * x, 0) / n;
    const ll = (alpha, beta) => {
      if (alpha < 0 || beta < 0 || alpha + beta >= 0.999) return -Infinity;
      const omega = v0 * (1 - alpha - beta); let s2 = v0, sum = 0;
      for (let t = 1; t < n; t++) { s2 = omega + alpha * e[t - 1] ** 2 + beta * s2; if (!(s2 > 0)) return -Infinity; sum += -0.5 * (Math.log(s2) + e[t] ** 2 / s2); }
      return sum;
    };
    let best = { a: 0.05, b: 0.9, ll: ll(0.05, 0.9) };
    for (let a = 0.01; a <= 0.3; a += 0.03) for (let b = 0.5; b <= 0.98; b += 0.04) { const v = ll(a, b); if (v > best.ll) best = { a, b, ll: v }; }
    for (let step = 0.02; step > 0.0005; step /= 2) {
      let moved = true;
      while (moved) { moved = false; for (const [da, db] of [[step, 0], [-step, 0], [0, step], [0, -step]]) { const v = ll(best.a + da, best.b + db); if (v > best.ll) { best = { a: best.a + da, b: best.b + db, ll: v }; moved = true; } } }
    }
    const omega = v0 * (1 - best.a - best.b); const vol = new Float64Array(n); let s2 = v0;
    for (let t = 0; t < n; t++) { if (t) s2 = omega + best.a * e[t - 1] ** 2 + best.b * s2; vol[t] = Math.sqrt(s2); }
    const next = Math.sqrt(omega + best.a * e[n - 1] ** 2 + best.b * s2);
    return { alpha: best.a, beta: best.b, omega, persistence: best.a + best.b, longRunVol: Math.sqrt(v0), vol, forecast: next, ratio: next / Math.sqrt(v0), loglik: best.ll };
  };

  /** Lo–MacKinlay variance ratio at lag q: >1 trending, <1 mean-reverting. */
  Q.varianceRatio = (r, q = 5) => {
    const a = Array.from(r).filter(Number.isFinite); if (a.length < q * 6) return null;
    const v1 = Q.std(a) ** 2; const sums = []; for (let i = q; i <= a.length; i++) { let s = 0; for (let k = i - q; k < i; k++) s += a[k]; sums.push(s); }
    const vq = Q.std(sums) ** 2; return v1 > 0 ? vq / (q * v1) : null;
  };
  /** Kaufman efficiency ratio of a price series: |net move| / path length, 0 choppy … 1 straight line. */
  Q.efficiency = (c, n = 20) => { if (c.length <= n) return null; const s = c.length - 1 - n; let path = 0; for (let i = s + 1; i < c.length; i++) path += Math.abs(c[i] - c[i - 1]); return path > 0 ? Math.abs(c[c.length - 1] - c[s]) / path : 0; };

  /** Market regime from closes: trend/mean-reversion character plus GARCH volatility state. */
  Q.regime = (closes) => {
    const r = Q.logReturns(closes), g = Q.garch(r), vr = Q.varianceRatio(r, 5), er = Q.efficiency(closes, 20);
    // Trending: returns persist (variance ratio > 1.1) or price moves in a near-straight line (efficiency > 0.5).
    const character = vr == null ? 'unknown' : (vr > 1.1 && (er ?? 0) > 0.3) || (er ?? 0) > 0.5 ? 'trending' : vr < 0.9 && (er ?? 1) < 0.35 ? 'mean-reverting' : 'random-walk';
    const volState = !g ? 'unknown' : g.ratio > 1.4 ? 'storm' : g.ratio > 1.1 ? 'elevated' : g.ratio < 0.8 ? 'calm' : 'normal';
    // Safeguards: shrink size as forecast vol rises above its long-run level, widen stops so noise does not stop out.
    const sizeMult = g ? Math.max(0.25, Math.min(1, 1 / Math.max(1, g.ratio) ** 1.5)) : 1;
    const stopMult = g ? Math.max(1, Math.min(2, g.ratio)) : 1;
    return { character, volState, varianceRatio: vr, efficiency: er, garch: g && { alpha: g.alpha, beta: g.beta, persistence: g.persistence, forecast: g.forecast, longRunVol: g.longRunVol, ratio: g.ratio }, sizeMult, stopMult };
  };

  /** Merton jump-diffusion path (log returns), for synthetic crash scenarios. */
  Q.jumpDiffusionPath = (r, steps, { mu = 0, sigma = 0.01, lambda = 0.02, jumpMean = -0.05, jumpSd = 0.04 } = {}) => {
    const out = new Float64Array(steps);
    for (let i = 0; i < steps; i++) {
      let x = mu - 0.5 * sigma * sigma + sigma * Q.gauss(r);
      // Poisson number of jumps this step (Knuth; lambda is small)
      let k = 0, p = 1; const L = Math.exp(-lambda); do { k++; p *= r(); } while (p > L); k -= 1;
      for (let j = 0; j < k; j++) x += jumpMean + jumpSd * Q.gauss(r);
      out[i] = x;
    }
    return out;
  };

  root.LokiQuant = Q;
  if (root.Loki && !root.Loki.quant) root.Loki.quant = Q;
})(typeof self !== 'undefined' ? self : this);
