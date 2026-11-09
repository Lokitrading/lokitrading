/* ADVERSARIAL RED TEAM ENGINE — every proposed trade is attacked before it is simulated.
 *
 * Scenarios (each 3,000 Monte Carlo paths on the asset's own history, seeded):
 *   baseline        the market as it is
 *   thin-book       displayed depth collapses to 8–28% of what is showing
 *   latency         exchange/network delay spikes (1.5 s median, 8 s p95)
 *   news-spread     quotes pull away from the mid ×8 (a news print)
 *   flash-crash     20% chance of an adverse jump the size of the asset's worst move
 *   withdrawal      liquidity withdrawal: all three at once
 * plus a deterministic replay of this asset's single worst historical move over
 * the holding period, and 2,000 path-wise simulations for the expected maximum
 * drawdown (block bootstrap), repeated with flash-crash jumps for the stressed p95.
 * A hard fail (which the Risk Manager turns into a veto) is judged in dollars
 * against the account's loss limit, not as a percentage of the position.
 *
 * Devil's Advocate: the exact opposite position is run through the same
 * machinery and through a historical analog count; if the other side looks as
 * good, the setup is flagged for confirmation bias.
 *
 * Verdict matrix: a 0–100% safety score from five explicit components
 * (edge 30%, stress resilience 25%, drawdown 20%, liquidity 15%, bias 10%) and
 * the expected maximum drawdown. Paper trades need ≥60%.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.redteam) return;
  const Q = Loki.quant;
  const PASS = 60;

  /** Historical analogs: bars whose 20-bar momentum had the same sign and similar size. */
  function analogs(closes, H) {
    const n = closes.length, mom = (i) => closes[i] / closes[i - 20] - 1, now = mom(n - 1), out = [];
    for (let i = 20; i < n - 1 - H; i++) { const m = mom(i); if (Math.sign(m) === Math.sign(now) && Math.abs(m - now) < Math.max(0.02, Math.abs(now) * 0.5)) out.push(closes[i + H] / closes[i] - 1); }
    return { now, n: out.length, pUp: out.length ? out.filter((x) => x > 0).length / out.length : null, mean: out.length ? Q.mean(out) : null, worst: out.length ? Math.min(...out) : null, best: out.length ? Math.max(...out) : null };
  }

  /** Expected and p95 max drawdown of the position over H bars (as a fraction of its value). */
  function pathDrawdown(rets, H, dir, { paths = 2000, seed = 3, jump = null } = {}) {
    const r = Q.rng(seed), B = 3, dds = new Float64Array(paths), n = rets.length;
    for (let p = 0; p < paths; p++) {
      let v = 1, peak = 1, dd = 0, left = H;
      while (left > 0) {
        const s = Math.floor(r() * Math.max(1, n - B));
        for (let k = 0; k < B && left > 0; k++, left--) {
          let x = rets[s + k] || 0;
          if (jump && r() < jump.prob / H) x += jump.mean + jump.sd * Q.gauss(r);
          v *= Math.exp(dir * x); peak = Math.max(peak, v); dd = Math.max(dd, 1 - v / peak);
        }
      }
      dds[p] = dd;
    }
    const sorted = Array.from(dds).sort((a, z) => a - z);
    return { expected: Q.mean(sorted), p95: Q.quantile(sorted, 0.95), worst: sorted[sorted.length - 1] };
  }

  /**
   * proposal: { symbol, side, qty, price, kind, multiplier?, horizon?, stopPct?, fee_bps? }
   * ctx: { bars (oldest first), book?, equity?, dailyLossLimit?, latency? }
   */
  async function run(proposal, ctx) {
    const bars = ctx.bars || [], closes = bars.map((b) => b.c);
    if (closes.length < 80) throw new Error('The red team needs at least 80 bars of history for this symbol.');
    const dir = proposal.side === 'sell' ? -1 : 1, H = proposal.horizon || (Loki.council ? Loki.council.HORIZON : 5);
    const rets = Array.from(Q.logReturns(closes)).slice(-750);
    const sd = Q.std(rets), mult = proposal.multiplier || 1, notional = proposal.qty * proposal.price * mult;
    // Worst adverse H-bar move in this asset's own history.
    let worstMove = 0; for (let i = H; i < closes.length; i++) { const m = dir * Math.log(closes[i] / closes[i - H]); if (m < worstMove) worstMove = m; }
    const crash = Math.max(Math.abs(worstMove), 4 * sd * Math.sqrt(H));
    const leg = { side: proposal.side, qty: proposal.qty, price: proposal.price, kind: proposal.kind, book: ctx.book || null, fee_bps: proposal.fee_bps ?? (proposal.kind === 'crypto' ? 25 : 0.5), multiplier: mult };
    const base = { legs: [leg], returns: rets, bar_seconds: 86400, horizon_bars: H, latency: ctx.latency || { median_ms: 250, p95_ms: 1200 }, paths: 3000 };
    const SCEN = [
      ['baseline', 'Baseline', {}],
      ['thin-book', 'Depth collapse (book 8–28% of displayed)', { thin_lo: 0.08, thin_span: 0.2 }],
      ['latency', 'Latency spike (1.5 s median, 8 s p95)', { latency: { median_ms: 1500, p95_ms: 8000 } }],
      ['news-spread', 'News print: spread ×8', { spread_mult: 8 }],
      ['flash-crash', `Flash crash: 20% chance of a ${(crash * 100).toFixed(1)}% adverse jump`, { shock: { prob: 0.2, mean: -dir * crash, sd: crash / 3 } }],
      ['withdrawal', 'Liquidity withdrawal: all of the above', { thin_lo: 0.08, thin_span: 0.2, spread_mult: 5, latency: { median_ms: 1500, p95_ms: 8000 }, shock: { prob: 0.2, mean: -dir * crash, sd: crash / 3 } }],
    ];
    const scenarios = [];
    for (const [id, name, patch] of SCEN) {
      const res = await Loki.mc.run({ ...base, ...patch, seed: 1000 + scenarios.length });
      scenarios.push({ id, name, confidence: res.confidence, mean: res.pnl.mean, var95: res.var95, cvar95: res.cvar95, slip99: res.slippage_bps.p99, exhausted: res.book_exhausted_share, ms: res.ms });
    }
    const histWorst = { name: `Worst ${H}-bar move in this asset's history`, move: worstMove, pnl: notional * (Math.exp(worstMove) - 1) };
    const dd = pathDrawdown(rets, H, dir);                                              // the market as it is
    const ddStress = pathDrawdown(rets, H, dir, { seed: 4, jump: { prob: 0.2, mean: -crash, sd: crash / 3 } }); // with flash-crash jumps

    // Devil's Advocate: the opposite trade through the same baseline and the same analogs.
    const opp = await Loki.mc.run({ ...base, legs: [{ ...leg, side: dir > 0 ? 'sell' : 'buy' }], seed: 1000 });
    const an = analogs(closes, H), mine = scenarios[0].confidence;
    const oppAnalog = an.pUp == null ? null : dir > 0 ? 1 - an.pUp : an.pUp;
    const bias = opp.confidence >= mine - 5 || (oppAnalog != null && an.n >= 15 && oppAnalog > 0.5);
    const devil = { side: dir > 0 ? 'sell' : 'buy', confidence: opp.confidence, analogN: an.n, analogOpposite: oppAnalog, bias,
      argument: bias ? `The opposite ${dir > 0 ? 'short' : 'long'} scores ${opp.confidence}% vs your ${mine}%${oppAnalog != null ? `, and in ${an.n} similar past setups the other side won ${Math.round(oppAnalog * 100)}% of the time` : ''}. The setup is not one-sided: this looks like confirmation bias.`
        : `The opposite trade scores ${opp.confidence}% vs your ${mine}%${oppAnalog != null ? ` and won ${Math.round(oppAnalog * 100)}% of ${an.n} similar past setups` : ''}: the case against you is weaker.` };

    // Verdict matrix.
    const stressOnly = scenarios.slice(1), limit = ctx.dailyLossLimit || notional * ((proposal.stopPct || 8) / 100);
    const edge = Math.max(0, Math.min(1, (mine - 35) / 40));
    const resilience = Math.max(0, Math.min(1, Q.mean(stressOnly.map((s) => s.confidence)) / Math.max(1, mine))) * Math.max(0, Math.min(1, 1 - (Math.max(...stressOnly.map((s) => s.var95)) - limit) / Math.max(limit, 1e-9)));
    const ddCap = (proposal.stopPct || 10) / 100, drawdown = Math.max(0, Math.min(1, 1 - dd.expected / ddCap));
    const thin = scenarios.find((s) => s.id === 'thin-book');
    const liquidity = Math.max(0, Math.min(1, 1 - (thin.exhausted || 0) * 2 - Math.max(0, thin.slip99 - 20) / 200));
    const components = [
      ['Edge', 0.30, edge, `${mine}% of baseline paths profit after costs`],
      ['Stress resilience', 0.25, resilience, `stress paths keep ${Math.round(Q.mean(stressOnly.map((s) => s.confidence)))}% profitable; worst VaR95 ${Loki.fmt.usd(Math.max(...stressOnly.map((s) => s.var95)))} vs limit ${Loki.fmt.usd(limit)}`],
      ['Drawdown', 0.20, drawdown, `expected max drawdown ${(dd.expected * 100).toFixed(1)}% (p95 ${(dd.p95 * 100).toFixed(1)}%, ${(ddStress.p95 * 100).toFixed(1)}% with flash crashes) vs ${(ddCap * 100).toFixed(0)}% cap`],
      ['Liquidity', 0.15, liquidity, `thin book: ran out in ${Math.round((thin.exhausted || 0) * 100)}% of paths, p99 slippage ${thin.slip99.toFixed(1)}bp`],
      ['Bias check', 0.10, bias ? 0 : 1, bias ? "Devil's Advocate: the other side is as plausible" : 'the opposite case is weaker'],
    ];
    const safety = Math.round(100 * components.reduce((a, [, w, v]) => a + w * v, 0));
    // Hard fail is about the ACCOUNT: the stressed p95 drawdown or the worst move ever, in dollars, against the loss limit.
    const hardFail = ddStress.p95 * notional > limit * 3 || histWorst.pnl < -limit * 4;
    const result = { safety, approved: safety >= PASS && !hardFail, pass: PASS, hardFail, expectedMaxDD: dd.expected, p95MaxDD: dd.p95, worstPathDD: dd.worst, stressedP95DD: ddStress.p95,
      scenarios, histWorst, devil, components: components.map(([name, weight, score, why]) => ({ name, weight, score, why })), horizon: H, notional, analog: an };
    Loki.emit('redteam:verdict', { symbol: proposal.symbol, side: proposal.side, safety, approved: result.approved });
    return result;
  }

  Loki.redteam = { run, analogs, pathDrawdown, PASS };
})();
