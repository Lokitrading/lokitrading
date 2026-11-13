/* BLACK SWAN STRESS LAB — how the whole portfolio survives the worst days.
 *
 *  1. Historical shock injection: the real daily path of past crises
 *     (2010 Flash Crash, COVID crash 2020, FTX collapse 2022, yen unwind 2024)
 *     is overlaid on today's positions. Assets with their own history in the
 *     window use it; others are mapped through today's beta to the crisis's
 *     anchor market. The Flash Crash's intraday trough (about −9% on the S&P 500,
 *     far worse than its −3.2% close) is applied on top, because daily data hides it.
 *  2. 10,000 jump-diffusion scenarios over 10 days: correlated diffusion from
 *     today's GARCH volatilities and 120-day correlations, each asset's own jump
 *     frequency and size, plus a systemic jump that hits everything at once —
 *     the "correlation breakdown" where crypto and gold dump together.
 *  3. Margin escalation: with leverage, a path whose equity falls below the
 *     maintenance requirement is force-liquidated at that point with extra
 *     slippage, the way a broker would, and the realised loss is recorded.
 * Outputs: VaR99 (1 day), max-drawdown distribution, probability of a margin
 * call, and how much to cut gross exposure to bring VaR99 inside your limit.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.stress) return;
  const Q = Loki.quant;
  const SHOCKS = [
    { id: 'flash2010', name: '2010 Flash Crash', start: '2010-05-03', end: '2010-05-10', anchor: 'SPY', intraday: -0.09, note: 'S&P 500 fell ~9% in minutes on 6 May 2010 and closed −3.2%.' },
    { id: 'covid2020', name: 'COVID liquidity crunch', start: '2020-02-19', end: '2020-03-23', anchor: 'SPY', cryptoAnchor: 'BTC-USD', note: 'Stocks −34% in 23 trading days; Bitcoin halved on 12 March.' },
    { id: 'ftx2022', name: 'FTX collapse', start: '2022-11-04', end: '2022-11-21', anchor: 'BTC-USD', note: 'An exchange failed; crypto fell ~25% in days.' },
    { id: 'yen2024', name: 'Yen carry-trade unwind', start: '2024-07-31', end: '2024-08-07', anchor: 'SPY', cryptoAnchor: 'BTC-USD', note: 'A global de-leveraging day on 5 August 2024.' },
  ];
  const isCrypto = (s) => /(-USD|USDT)$/.test(s) && !/^(EUR|GBP|JPY|AUD)/.test(s);

  async function history(sym, start, end) {
    const pair = Loki.data.binancePair && Loki.data.binancePair(sym);
    if (pair) return Loki.data.binanceKlines(pair, '1d', 60, Date.parse(start), Date.parse(end) + 86400e3);
    const d = await Loki.getJSON(`/api/replay/bars?symbol=${encodeURIComponent(sym)}&start=${start}&end=${end}&interval=1d`, { timeout: 20000 });
    return d.bars;
  }

  /** Portfolio = [{ symbol, value }] (signed dollar exposure); equity in dollars. */
  async function historical(portfolio, equity, opts = {}) {
    const recent = {}; // today's daily returns, for betas
    for (const p of portfolio) recent[p.symbol] = Array.from(Q.logReturns((await Loki.data.bars(p.symbol, { interval: '1d', limit: 150 })).map((b) => b.c)));
    const out = [];
    for (const s of SHOCKS) {
      const anchors = {};
      for (const a of new Set([s.anchor, s.cryptoAnchor].filter(Boolean))) { try { anchors[a] = await history(a, s.start, s.end); } catch { anchors[a] = null; } }
      const anchorRecent = {};
      for (const a of Object.keys(anchors)) anchorRecent[a] = Array.from(Q.logReturns((await Loki.data.bars(a, { interval: '1d', limit: 150 })).map((b) => b.c)));
      let worst = 0, pathPnl = null, used = [];
      for (const p of portfolio) {
        let path = null, how = '';
        try { const own = await history(p.symbol, s.start, s.end); if (own && own.length > 2) { path = own.map((b) => b.c / own[0].c - 1); how = 'own history'; } } catch { /* not listed then */ }
        if (!path) {
          const a = isCrypto(p.symbol) && s.cryptoAnchor ? s.cryptoAnchor : s.anchor, ab = anchors[a];
          if (!ab || ab.length < 2) continue;
          const n = Math.min(recent[p.symbol].length, anchorRecent[a].length), x = anchorRecent[a].slice(-n), y = recent[p.symbol].slice(-n);
          const beta = Q.std(x) > 0 ? Q.corr(x, y) * Q.std(y) / Q.std(x) : 1;
          path = ab.map((b) => beta * (b.c / ab[0].c - 1)); how = `β ${beta.toFixed(2)} to ${a}`;
        }
        if (s.intraday && !isCrypto(p.symbol)) path = path.concat([Math.min(...path) + s.intraday * (how.startsWith('β') ? Number(how.split(' ')[1]) || 1 : 1)]);
        used.push(`${p.symbol}: ${how}`);
        const pnl = path.map((r) => p.value * r);
        pathPnl = pathPnl ? pathPnl.map((v, i) => v + (pnl[Math.min(i, pnl.length - 1)] || 0)) : pnl;
      }
      if (!pathPnl) { out.push({ ...s, error: 'no data for this window' }); continue; }
      worst = Math.min(...pathPnl);
      const eqPath = pathPnl.map((v) => equity + v);
      out.push({ ...s, worst, worstPct: worst / equity, final: pathPnl[pathPnl.length - 1], maxDD: Q.maxDrawdown([equity, ...eqPath]), used, marginCall: marginCheck(eqPath, portfolio, opts) });
    }
    return out;
  }
  function marginCheck(eqPath, portfolio, { maintenance = 0.25 } = {}) {
    const gross = portfolio.reduce((a, p) => a + Math.abs(p.value), 0);
    const i = eqPath.findIndex((e) => e < maintenance * gross);
    return i < 0 ? null : { step: i, equity: eqPath[i] };
  }

  function cholesky(m) {
    const n = m.length, L = m.map(() => new Array(n).fill(0));
    for (let i = 0; i < n; i++) for (let j = 0; j <= i; j++) {
      let s = m[i][j]; for (let k = 0; k < j; k++) s -= L[i][k] * L[j][k];
      L[i][j] = i === j ? Math.sqrt(Math.max(s, 1e-10)) : s / L[j][j];
    }
    return L;
  }

  /** 10,000 jump-diffusion paths with a systemic jump that sends correlations to one. */
  async function jumpDiffusion(portfolio, equity, { paths = 10000, days = 10, leverage = null, maintenance = 0.25, seed = 17 } = {}) {
    const syms = portfolio.map((p) => p.symbol), n = syms.length, params = [], rets = {};
    for (const s of syms) {
      const r = Array.from(Q.logReturns((await Loki.data.bars(s, { interval: '1d', limit: 300 })).map((b) => b.c)));
      rets[s] = r; const g = Q.garch(r), sd = Q.std(r), big = r.filter((x) => Math.abs(x) > 4 * sd);
      params.push({ sigma: g ? g.forecast : sd, lambda: Math.max(1 / 250, big.length / r.length), jumpMean: big.length ? Q.mean(big) : -3 * sd, jumpSd: big.length > 1 ? Q.std(big) : sd * 2, sd });
    }
    const len = Math.min(...syms.map((s) => rets[s].length), 120);
    const corr = syms.map((a) => syms.map((b) => (a === b ? 1 : (Q.corr(rets[a].slice(-len), rets[b].slice(-len)) || 0) * 0.98)));
    const L = cholesky(corr), r = Q.rng(seed);
    const values = portfolio.map((p) => p.value), gross = values.reduce((a, v) => a + Math.abs(v), 0);
    const lev = leverage || gross / equity, sysLambda = 1 / 250;
    const day1 = new Float64Array(paths), mdd = new Float64Array(paths); let calls = 0, callLoss = 0, breakdowns = 0;
    for (let p = 0; p < paths; p++) {
      if (p % 2000 === 0) await new Promise((res) => setTimeout(res, 0));
      const v = values.slice(); let eq = equity, peak = equity, dd = 0, liquidated = false;
      for (let d = 0; d < days && !liquidated; d++) {
        const z = new Array(n).fill(0).map(() => Q.gauss(r)), x = new Array(n).fill(0);
        for (let i = 0; i < n; i++) { let s = 0; for (let k = 0; k <= i; k++) s += L[i][k] * z[k]; x[i] = params[i].sigma * s; }
        const systemic = r() < sysLambda;
        if (systemic) breakdowns++;
        let pnl = 0;
        for (let i = 0; i < n; i++) {
          let ret = x[i];
          if (r() < params[i].lambda) ret += params[i].jumpMean + params[i].jumpSd * Q.gauss(r);
          if (systemic) ret += -4 * params[i].sd * (0.75 + 0.5 * r()); // everything falls together: correlation → 1
          const dv = v[i] * (Math.exp(ret) - 1); v[i] += dv; pnl += dv;
        }
        eq += pnl; if (d === 0) day1[p] = pnl;
        const grossNow = v.reduce((a, y) => a + Math.abs(y), 0);
        if (lev > 1 && eq < maintenance * grossNow) { const slip = grossNow * 0.004; eq -= slip; liquidated = true; calls++; callLoss += equity - eq; }
        peak = Math.max(peak, eq); dd = Math.max(dd, 1 - eq / peak);
      }
      mdd[p] = dd;
    }
    const s1 = Array.from(day1).sort((a, z) => a - z), sd = Array.from(mdd).sort((a, z) => a - z);
    const var99 = -Q.quantile(s1, 0.01), limit = equity * Loki.config().daily_loss_limit_pct / 100;
    return { paths, days, var99, var95: -Q.quantile(s1, 0.05), cvar99: -Q.mean(s1.slice(0, Math.max(1, Math.floor(paths * 0.01)))),
      mddMean: Q.mean(sd), mddP95: Q.quantile(sd, 0.95), mddP99: Q.quantile(sd, 0.99), marginCallProb: calls / paths, avgLossAtCall: calls ? callLoss / calls : 0,
      systemicShare: breakdowns / (paths * days), leverage: lev, limit, params: syms.map((s, i) => ({ symbol: s, sigma: params[i].sigma, lambda: params[i].lambda, jumpMean: params[i].jumpMean })),
      reduceBy: var99 > limit ? 1 - limit / var99 : 0 };
  }

  /** The positions to stress: the local paper book, or a sample book when it is empty. */
  function portfolioNow() {
    const b = Loki.paperbook ? Loki.paperbook.snapshot() : null;
    if (b && b.positions.length) return { equity: b.equity, positions: b.positions.map((p) => ({ symbol: p.symbol, value: p.qty * p.mark })), sample: false };
    return { equity: 10000, positions: [{ symbol: 'SPY', value: 4000 }, { symbol: 'BTC-USD', value: 3000 }, { symbol: 'GLD', value: 2000 }], sample: true };
  }

  Loki.stress = { SHOCKS, historical, jumpDiffusion, portfolioNow, cholesky };
})();
