/* CROSS-ASSET CORRELATION MATRIX — markets are not evaluated in isolation.
 *
 * Ten markets (stocks, tech, crypto, gold, the dollar, long bonds, the 10-year
 * yield, oil, transports) are aligned on the days they all traded. Rolling
 * 20/60/120-day correlations reveal:
 *  - Cross-asset signals on classic macro pairs (gold vs dollar, BTC vs tech,
 *    yields vs stocks, oil vs transports, gold vs yields);
 *  - Divergences: a pair whose 20-day correlation has moved more than 0.5 away
 *    from its 120-day norm has decoupled — a possible regime change;
 *  - Rebalancing: positions in the local paper book that move together are
 *    flagged, with a risk-parity suggestion (inverse volatility, correlation
 *    adjusted) for how exposure could be spread instead.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.xasset) return;
  const Q = Loki.quant;
  const ASSETS = [
    { sym: 'SPY', name: 'S&P 500' }, { sym: 'QQQ', name: 'Nasdaq 100' }, { sym: 'BTC-USD', name: 'Bitcoin' }, { sym: 'ETH-USD', name: 'Ether' },
    { sym: 'GLD', name: 'Gold' }, { sym: 'UUP', name: 'US dollar' }, { sym: 'TLT', name: '20y Treasuries' }, { sym: '^TNX', name: '10y yield' },
    { sym: 'USO', name: 'Crude oil' }, { sym: 'IYT', name: 'Transports' },
  ];
  const PAIRS = [['GLD', 'UUP', 'Gold vs US dollar'], ['BTC-USD', 'QQQ', 'Bitcoin vs tech'], ['^TNX', 'SPY', '10y yield vs stocks'], ['USO', 'IYT', 'Oil vs transports'], ['GLD', '^TNX', 'Gold vs yields'], ['TLT', 'SPY', 'Bonds vs stocks']];
  let cache = null;
  const day = (t) => new Date(t).toISOString().slice(0, 10);

  async function load(force = false) {
    if (cache && !force && Date.now() - cache.at < 30 * 60e3) return cache;
    const series = {}, errors = [];
    await Promise.all(ASSETS.map(async (a) => {
      try { const bars = await Loki.data.bars(a.sym, { interval: '1d', limit: 300 }); series[a.sym] = new Map(bars.map((b) => [day(b.t), b.c])); }
      catch (e) { errors.push(`${a.name}: ${e.message}`); }
    }));
    const syms = ASSETS.map((a) => a.sym).filter((s) => series[s] && series[s].size > 60);
    const dates = [...series[syms[0]].keys()].filter((d) => syms.every((s) => series[s].has(d))).sort();
    const rets = {};
    for (const s of syms) { const c = dates.map((d) => series[s].get(d)); rets[s] = s === '^TNX' ? c.slice(1).map((x, i) => (x - c[i]) / 10) : Array.from(Q.logReturns(c)); } // yield: change in points/10
    cache = { at: Date.now(), syms, dates: dates.slice(1), rets, closes: Object.fromEntries(syms.map((s) => [s, dates.map((d) => series[s].get(d))])), errors };
    return cache;
  }

  const window_ = (a, n) => a.slice(-n);
  function matrix(c, n) { return c.syms.map((a) => c.syms.map((b) => (a === b ? 1 : Q.corr(window_(c.rets[a], n), window_(c.rets[b], n))))); }

  async function analyze(force = false) {
    const c = await load(force), n = c.syms.length;
    const m20 = matrix(c, 20), m60 = matrix(c, 60), m120 = matrix(c, 120);
    const idx = (s) => c.syms.indexOf(s), name = (s) => (ASSETS.find((a) => a.sym === s) || {}).name || s;
    const divergences = [];
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const d = m20[i][j] - m120[i][j];
      if (Number.isFinite(d) && Math.abs(d) > 0.5 && Math.abs(m120[i][j]) > 0.25) divergences.push({ a: c.syms[i], b: c.syms[j], c20: m20[i][j], c120: m120[i][j],
        text: `${name(c.syms[i])} and ${name(c.syms[j])} have decoupled: 20-day correlation ${m20[i][j].toFixed(2)} vs ${m120[i][j].toFixed(2)} over 120 days${Math.sign(m20[i][j]) !== Math.sign(m120[i][j]) ? ' (sign flipped)' : ''}.` });
    }
    divergences.sort((x, y) => Math.abs(y.c20 - y.c120) - Math.abs(x.c20 - x.c120));
    // Five-day moves in standard deviations, for the macro pair signals.
    const z5 = (s) => { const r = c.rets[s]; if (!r) return null; const sd = Q.std(r.slice(-120)) * Math.sqrt(5); const m = r.slice(-5).reduce((a, x) => a + x, 0); return sd > 0 ? m / sd : null; };
    const signals = [];
    const zs = Object.fromEntries(c.syms.map((s) => [s, z5(s)]));
    const say = (cond, text, level = 'warn') => { if (cond) signals.push({ text, level }); };
    if (zs.GLD != null && zs.UUP != null) say(zs.GLD > 1 && zs.UUP > 1, `Gold (+${zs.GLD.toFixed(1)}σ) and the dollar (+${zs.UUP.toFixed(1)}σ) are rising together this week: a flight to safety, not ordinary dollar weakness.`);
    if (zs['^TNX'] != null && zs.SPY != null) say(zs['^TNX'] > 1.5 && zs.SPY < 0, `The 10-year yield jumped ${zs['^TNX'].toFixed(1)}σ in five days while stocks fell: rate pressure on equities, consider lower equity leverage.`);
    if (zs.USO != null && zs.IYT != null) say(zs.USO > 1.5 && zs.IYT < -0.5, `Oil spiked (+${zs.USO.toFixed(1)}σ) while transports fell (${zs.IYT.toFixed(1)}σ): fuel costs biting, a classic early warning.`);
    if (zs.GLD != null && zs['^TNX'] != null) say(zs.GLD > 1 && zs['^TNX'] > 1, `Gold is rising even as yields spike: an unusual divergence that often marks a regime change (inflation or credit fear).`);
    if (zs['BTC-USD'] != null && zs.QQQ != null) { const gap = zs['BTC-USD'] - zs.QQQ; say(Math.abs(gap) > 2, `Bitcoin is moving ${gap > 0 ? 'well ahead of' : 'well behind'} tech (${zs['BTC-USD'].toFixed(1)}σ vs ${zs.QQQ.toFixed(1)}σ): crypto has ${gap > 0 ? 'decoupled to the upside' : 'lost its risk-on link'}.`, 'info'); }
    if (!signals.length) signals.push({ text: 'No macro pair is flashing: cross-asset moves are within normal ranges this week.', level: 'ok' });
    const pairs = PAIRS.filter(([a, b]) => idx(a) >= 0 && idx(b) >= 0).map(([a, b, label]) => ({ a, b, label, c20: m20[idx(a)][idx(b)], c60: m60[idx(a)][idx(b)], c120: m120[idx(a)][idx(b)] }));
    return { assets: c.syms.map((s) => ({ sym: s, name: name(s), z5: zs[s], vol: Q.std(c.rets[s].slice(-60)) * Math.sqrt(252) })), m20, m60, m120, divergences: divergences.slice(0, 8), signals, pairs, days: c.dates.length, errors: c.errors, rebalance: rebalance(c, m60) };
  }

  /** Correlation-aware look at the local paper book: clusters to trim and a risk-parity target. */
  function rebalance(c, m60) {
    const book = Loki.paperbook ? Loki.paperbook.snapshot() : null;
    const held = book ? book.positions.filter((p) => c.syms.includes(normalize(p.symbol))) : [];
    if (held.length < 2) return { held: held.length, suggestions: [held.length ? 'Only one tracked position: correlation risk is the single asset itself.' : 'No paper positions in tracked markets yet; open some in the paper book to get rebalancing advice.'], target: [] };
    const syms = held.map((p) => normalize(p.symbol)), gross = held.reduce((a, p) => a + Math.abs(p.qty * p.mark), 0);
    const w = held.map((p) => Math.abs(p.qty * p.mark) / gross), vol = syms.map((s) => Q.std(c.rets[s].slice(-60)) || 1e-6);
    const corrOf = (a, b) => m60[c.syms.indexOf(a)][c.syms.indexOf(b)];
    const suggestions = [];
    for (let i = 0; i < syms.length; i++) for (let j = i + 1; j < syms.length; j++) { const r = corrOf(syms[i], syms[j]); if (r > 0.7) suggestions.push(`${syms[i]} and ${syms[j]} move together (ρ=${r.toFixed(2)}): together they are one bet of ${((w[i] + w[j]) * 100).toFixed(0)}% of the book; trim the larger.`); }
    // Inverse-volatility weights, shrunk for each asset's average correlation with the rest.
    const raw = syms.map((s, i) => { const avgR = Q.mean(syms.filter((_, j) => j !== i).map((t) => corrOf(s, t))); return (1 / vol[i]) * (1 - Math.max(0, avgR) * 0.5); });
    const tot = raw.reduce((a, x) => a + x, 0), target = syms.map((s, i) => ({ sym: s, now: w[i], target: raw[i] / tot, change: raw[i] / tot - w[i] }));
    let div = 0; for (let i = 0; i < syms.length; i++) for (let j = 0; j < syms.length; j++) div += w[i] * w[j] * (i === j ? 1 : corrOf(syms[i], syms[j]));
    suggestions.push(`Effective number of independent bets: ${(1 / div).toFixed(2)} of ${syms.length} positions.`);
    return { held: held.length, suggestions, target, effectiveBets: 1 / div };
  }
  const normalize = (s) => (/^(BTC|ETH)(USDT|\/USD|USD)$/.test(s) ? s.replace(/(USDT|\/USD|USD)$/, '') + '-USD' : s);

  /** Highest 60-day correlation of `symbol` with anything held in the paper book. */
  async function concentrationFor(symbol) {
    const book = Loki.paperbook ? Loki.paperbook.snapshot() : null;
    if (!book || !book.positions.length) return null;
    const s0 = normalize(symbol); let best = null;
    const mine = await Loki.data.bars(s0, { interval: '1d', limit: 120 }).catch(() => null);
    if (!mine) return null;
    const m = new Map(mine.map((b) => [day(b.t), b.c]));
    for (const p of book.positions) {
      const s = normalize(p.symbol); if (s === s0) { best = { max: 1, with: s }; break; }
      const other = await Loki.data.bars(s, { interval: '1d', limit: 120 }).catch(() => null); if (!other) continue;
      const common = other.filter((b) => m.has(day(b.t)));
      const a = Q.logReturns(common.map((b) => m.get(day(b.t)))), b2 = Q.logReturns(common.map((b) => b.c));
      const r = Q.corr(a.slice(-60), b2.slice(-60)); if (Number.isFinite(r) && (!best || r > best.max)) best = { max: r, with: s };
    }
    return best;
  }

  Loki.xasset = { ASSETS, PAIRS, load, analyze, concentrationFor };
})();
