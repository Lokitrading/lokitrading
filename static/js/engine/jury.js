/* AGENT JURY — Quant vs Risk vs Execution, with a red team and a devil's advocate.
 *
 *  [market data] ─► Quant Agent       statistical edge: momentum + mean reversion, weighted by regime
 *                ─► Risk Manager      VaR, max position size, tilt/news/quarantine — ABSOLUTE VETO
 *                ─► Execution Engine  volatility, spread, depth → order type + expected slippage
 *                ─► Red Team          six stress scenarios → safety score
 *                ─► Devil's Advocate  the opposite trade, to expose confirmation bias
 *                ─► Market Consensus  the six-agent gate (macro, liquidity, sentiment, council…)
 *                           │
 *                           ▼
 *            [Jury] 2/3 supermajority of the five voting jurors, Risk veto overrides all
 *
 * Cross-examination: before the vote the Risk Manager attacks the Quant's
 * hypothesis with the past: every earlier bar where the same signal fired in a
 * similar volatility regime, how often it failed, and the worst cases by date.
 * Every decision is written to a post-mortem log, including why the minority
 * dissented. The jurors are deterministic statistical agents running on this
 * device — not large language models — so the same data gives the same verdict.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.jury) return;
  const Q = Loki.quant;
  const LOG = 'loki_jury_log_v1';
  const tanh = Math.tanh;

  /* ------------------------------------------------------------ Quant Agent */
  function quantAgent(closes, side, council) {
    const dir = side === 'sell' ? -1 : 1, n = closes.length, last = closes[n - 1];
    const reg = Q.regime(closes);
    const r20 = last / closes[n - 21] - 1;
    const hist20 = []; for (let i = 40; i < n; i++) hist20.push(closes[i] / closes[i - 20] - 1);
    const zMom = Q.std(hist20) > 0 ? (r20 - Q.mean(hist20)) / Q.std(hist20) : 0;
    const w20 = closes.slice(-20), sma20 = Q.mean(w20), sd20 = Q.std(w20);
    const zRev = sd20 > 0 ? (last - sma20) / sd20 : 0;
    const sma50 = Q.mean(closes.slice(-50)), sma200 = n >= 200 ? Q.mean(closes.slice(-200)) : null;
    const trend = (last > sma50 ? 0.5 : -0.5) + (sma200 == null ? 0 : last > sma200 ? 0.5 : -0.5);
    const momentum = tanh(zMom) * 0.7 + trend * 0.3, reversion = -tanh(zRev / 1.5);
    const wMom = reg.character === 'trending' ? 0.75 : reg.character === 'mean-reverting' ? 0.25 : 0.5;
    let score = wMom * momentum + (1 - wMom) * reversion;
    if (council && Number.isFinite(council.p_up)) score = 0.7 * score + 0.3 * tanh((council.p_up - (Number.isFinite(council.usual) ? council.usual : 0.5)) * 6);
    const agree = score * dir;
    return { id: 'quant', name: 'Quant Agent', score, agree, vote: agree >= 0.15, regime: reg, zMom, zRev, momentum, reversion, weightMomentum: wMom,
      hypothesis: `${reg.character} regime → ${Math.round(wMom * 100)}% momentum / ${Math.round((1 - wMom) * 100)}% mean reversion. 20-bar momentum z ${zMom.toFixed(2)}, distance from 20-bar mean ${zRev.toFixed(2)}σ${council ? `, council ${Math.round(council.p_up * 100)}% up` : ''}. Net ${score >= 0 ? 'bullish' : 'bearish'} ${Math.abs(score).toFixed(2)}, which ${agree >= 0.15 ? 'supports' : 'does not support'} a ${side}.` };
  }

  /* ---------------------------------------------------- Cross-examination */
  function crossExamine(bars, side, H) {
    const closes = bars.map((b) => b.c), dir = side === 'sell' ? -1 : 1, n = closes.length;
    const g = Q.garch(Q.logReturns(closes)); if (!g) return null;
    const ratioAt = (i) => g.vol[i - 1] / g.longRunVol, nowRatio = g.ratio, sigNow = Math.sign(closes[n - 1] / closes[n - 21] - 1) || 1;
    const cases = [];
    for (let i = 60; i < n - 1 - H; i++) {
      const sig = Math.sign(closes[i] / closes[i - 20] - 1) || 1;
      if (sig !== sigNow || Math.abs(ratioAt(i) - nowRatio) > 0.25) continue;
      cases.push({ t: bars[i].t, ret: dir * (closes[i + H] / closes[i] - 1) });
    }
    const losses = cases.filter((c) => c.ret < 0).sort((a, z) => a.ret - z.ret);
    const failRate = cases.length ? losses.length / cases.length : null;
    return { n: cases.length, failRate, avg: cases.length ? Q.mean(cases.map((c) => c.ret)) : null, worst: losses.slice(0, 3), volRatio: nowRatio,
      attack: !cases.length ? 'No comparable setups in this history: the hypothesis is untested in this volatility regime.'
        : `In ${cases.length} past setups with the same signal at a similar volatility level (${nowRatio.toFixed(2)}× normal), this ${side} lost ${Math.round(failRate * 100)}% of the time over ${H} bars; worst: ${losses.slice(0, 3).map((c) => `${new Date(c.t).toISOString().slice(0, 10)} ${(c.ret * 100).toFixed(1)}%`).join(', ') || 'none'}.` };
  }

  /* ---------------------------------------------------- Execution Engine */
  async function executionAgent(p, bars, book, regime, edgeBps) {
    const g = regime.garch, volDaily = g ? g.forecast : Q.std(Q.logReturns(bars.map((b) => b.c)));
    let minuteBars = null;
    const pair = Loki.data.binancePair && Loki.data.binancePair(p.symbol);
    if (pair) { try { minuteBars = await Loki.data.binanceKlines(pair, '1m', 240); } catch { minuteBars = null; } }
    const minuteVol = minuteBars && minuteBars.length ? Q.mean(minuteBars.slice(-60).map((b) => b.v)) : (bars[bars.length - 1].v || 0) / 390;
    const participation = minuteVol > 0 ? p.qty / minuteVol : 1;
    let spreadBps = p.kind === 'stock' ? 2 : p.kind === 'future' ? 1.5 : 4;
    if (book && book.bids && book.asks && book.bids.length && book.asks.length) spreadBps = (book.asks[0][0] - book.bids[0][0]) / ((book.asks[0][0] + book.bids[0][0]) / 2) * 1e4;
    let type = 'market', algo = 'none', why;
    if (participation > 0.05) { type = 'market'; algo = participation > 0.2 ? 'vwap' : 'twap'; why = `the order is ${(participation * 100).toFixed(1)}% of a minute's volume: slice it (${algo.toUpperCase()}) to stay under the 5% impact threshold`; }
    else if (regime.volState === 'storm' || regime.volState === 'elevated' || spreadBps > 10) { type = 'limit'; why = `${regime.volState} volatility (GARCH ${(volDaily * 100).toFixed(2)}%/bar) and a ${spreadBps.toFixed(1)}bp spread: a limit order near the mid avoids paying the spread into a fast market`; }
    else why = `calm volatility, ${spreadBps.toFixed(1)}bp spread, ${(participation * 100).toFixed(2)}% of minute volume: a market order is cheapest`;
    // Expected slippage from the local fill engine on recent minute bars (crypto) or the spread/impact model.
    let slipBps = spreadBps / 2 + (participation > 0.05 ? 0.8 * volDaily / Math.sqrt(390) * Math.sqrt(participation) * 1e4 : 0), fillNote = 'spread/impact model';
    if (minuteBars && minuteBars.length > 60 && Loki.fillsim) {
      const start = minuteBars.length - 31, last = minuteBars[start].c;
      const f = Loki.fillsim.fill({ side: p.side, qty: p.qty, type, algo, limit: type === 'limit' ? +(last * (1 - (p.side === 'sell' ? -1 : 1) * 0.0005)).toFixed(2) : undefined, slices: 10, dark: algo !== 'none' }, minuteBars, start, { spread_bps: spreadBps, seed: 11 });
      if (f.slippageBps != null) { slipBps = Math.max(0, f.slippageBps); fillNote = `local fill engine on the last 30 one-minute bars: ${f.partial ? 'partial fill, ' : ''}${Math.round(f.filledQty / p.qty * 100)}% filled`; }
    }
    const venue = Loki.execq ? Loki.execq.stats(p.venue || 'alpaca-paper') : { state: 'unknown' };
    const vote = venue.state !== 'down' && (edgeBps == null ? slipBps < 15 : slipBps < Math.abs(edgeBps) / 2);
    return { id: 'execution', name: 'Execution Engine', vote, type, algo, why, slipBps, spreadBps, participation, fillNote, venue: venue.state,
      summary: `${type}${algo !== 'none' ? ` via ${algo.toUpperCase()}` : ''}: expected slippage ${slipBps.toFixed(1)}bp${edgeBps != null ? ` vs edge ${edgeBps.toFixed(0)}bp` : ''} (${fillNote})` };
  }

  /* -------------------------------------------------------- Risk Manager */
  function riskManager(p, ctx, rt, regime) {
    const cfg = Loki.config(), equity = ctx.equity || 10000, notional = p.qty * p.price * (p.multiplier || 1);
    const limit = equity * cfg.daily_loss_limit_pct / 100;
    const riskLevel = ctx.risk ? ctx.risk.level : 'green', riskMult = ctx.risk ? ctx.risk.sizeMultiplier : 1;
    const ddScale = Loki.guard ? Loki.guard.sizeScale(ctx.drawdown || 0, (ctx.ddLimit || 0.1)) : 1;
    const base = rt.scenarios[0], worstStress = Math.max(...rt.scenarios.slice(1).map((s) => s.var95));
    const varPerDollar = notional > 0 ? base.var95 / notional : 0;
    const maxByPlan = equity * cfg.max_position_pct / 100 * riskMult * ddScale * regime.sizeMult;
    const maxByVar = varPerDollar > 0 ? limit / varPerDollar : Infinity;
    const maxNotional = Math.max(0, Math.min(maxByPlan, maxByVar));
    const vetoes = []; let sizeOnly = false;
    if (!p.exit) {
      if (notional > maxNotional * 1.0001) sizeOnly = true;
      if (notional > maxNotional * 1.0001) vetoes.push(`size ${Loki.fmt.usd(notional)} exceeds the allowed ${Loki.fmt.usd(maxNotional)} (plan ${Loki.fmt.usd(maxByPlan)}, VaR ${Number.isFinite(maxByVar) ? Loki.fmt.usd(maxByVar) : '∞'})`);
      if (worstStress > limit * 1.5) vetoes.push(`stress VaR95 ${Loki.fmt.usd(worstStress)} is over 1.5× the daily loss limit ${Loki.fmt.usd(limit)}`);
      if (riskLevel === 'red') vetoes.push('the self-healing risk matrix is RED (breaker tripped)');
      if (rt.hardFail) vetoes.push('red team hard fail: tail drawdown or worst historical move is extreme for this size');
      const gate = Loki.guard ? Loki.guard.canEnter(ctx.strategy || 'manual') : { ok: true };
      if (!gate.ok) vetoes.push(gate.why);
      if (ctx.correlation && ctx.correlation.max > 0.85) vetoes.push(`concentration: ${(ctx.correlation.max * 100).toFixed(0)}% correlated with ${ctx.correlation.with} already held`);
    }
    const qtyMax = p.price > 0 ? maxNotional / (p.price * (p.multiplier || 1)) : 0;
    return { id: 'risk', name: 'Risk Manager', veto: vetoes.length > 0, vetoes, resizable: sizeOnly && vetoes.length === 1 && maxNotional > 0, maxNotional, qtyMax, limit, varPerDollar, baseVar95: base.var95, worstStress, ddScale, regimeMult: regime.sizeMult, riskMult,
      summary: vetoes.length ? `VETO: ${vetoes.join('; ')}` : `within limits: max ${Loki.fmt.usd(maxNotional)} (size ×${(riskMult * ddScale * regime.sizeMult).toFixed(2)} after risk matrix, drawdown and regime scaling)` };
  }

  /* --------------------------------------------------------------- Jury */
  async function evaluate(p, ctx = {}) {
    const bars = ctx.bars || await Loki.data.bars(p.symbol, { interval: '1d', limit: 300 });
    if (!bars || bars.length < 80) throw new Error(`Not enough history for ${p.symbol} to convene the jury.`);
    p = { ...p, price: p.price || bars[bars.length - 1].c };
    const closes = bars.map((b) => b.c), H = p.horizon || (Loki.council ? Loki.council.HORIZON : 5);
    const council = ctx.council !== undefined ? ctx.council : (bars.length > 60 && Loki.council ? Loki.council.analyze(p.symbol, bars) : null);
    let book = ctx.book || null;
    const pair = Loki.data.binancePair && Loki.data.binancePair(p.symbol);
    if (!book && pair) { try { book = await Loki.data.binanceDepth(pair, 100); } catch { book = null; } }
    const quant = quantAgent(closes, p.side, council);
    const exam = crossExamine(bars, p.side, H);
    const rt = await Loki.redteam.run({ ...p, horizon: H }, { bars, book, equity: ctx.equity, dailyLossLimit: (ctx.equity || 10000) * Loki.config().daily_loss_limit_pct / 100 });
    const edgeBps = rt.analog.mean != null ? (p.side === 'sell' ? -1 : 1) * rt.analog.mean * 1e4 : null;
    const execution = await executionAgent(p, bars, book, quant.regime, edgeBps);
    const risk = riskManager(p, ctx, rt, quant.regime);
    const consensus = Loki.consensus ? await Loki.consensus.vote({ symbol: p.symbol, side: p.side, notional: p.qty * p.price, venue: p.venue || 'alpaca-paper', exit: p.exit, kind: p.kind }, { council, risk: ctx.risk, book }) : null;
    // Cross-examination can flip the Quant: a hypothesis that failed most of the time in this regime loses its vote.
    const quantVote = quant.vote && !(exam && exam.n >= 10 && exam.failRate > 0.6);
    const jurors = [
      { id: 'quant', name: 'Quant Agent', vote: quantVote, why: quantVote ? quant.hypothesis : exam && exam.n >= 10 && exam.failRate > 0.6 && quant.vote ? `withdrawn after cross-examination: ${exam.attack}` : quant.hypothesis },
      { id: 'execution', name: 'Execution Engine', vote: execution.vote, why: execution.summary },
      { id: 'redteam', name: 'Red Team', vote: rt.approved, why: `safety ${rt.safety}% (pass ${rt.pass}%), expected max drawdown ${(rt.expectedMaxDD * 100).toFixed(1)}%` },
      { id: 'devil', name: "Devil's Advocate", vote: !rt.devil.bias, why: rt.devil.argument },
      { id: 'consensus', name: 'Market Consensus (6 agents)', vote: consensus ? consensus.approved : false, why: consensus ? consensus.summary : 'unavailable' },
    ];
    const yes = jurors.filter((j) => j.vote).length, need = Math.ceil(jurors.length * 2 / 3);
    const supermajority = yes >= need, approved = supermajority && !risk.veto;
    const dissent = jurors.filter((j) => (approved || supermajority) ? !j.vote : j.vote);
    const decision = {
      id: `j${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, t: Date.now(), symbol: p.symbol, side: p.side, qty: p.qty, price: p.price, kind: p.kind, strategy: ctx.strategy || 'manual',
      approved, supermajority, yes, need, jurors, risk, execution, quant: { score: quant.score, regime: quant.regime, hypothesis: quant.hypothesis, zMom: quant.zMom, zRev: quant.zRev },
      crossExam: exam, redteam: rt, consensus: consensus && { approved: consensus.approved, score: consensus.score, summary: consensus.summary },
      plan: { signalPx: p.price, stopPct: p.stopPct || Loki.config().stop_loss_pct, order: execution.type, algo: execution.algo, regime: quant.regime.character, volState: quant.regime.volState, efficiency: quant.regime.efficiency,
        // last COMPLETED bar vs the 20 before it (today's bar is still forming)
        volumeRatio: bars.length > 22 ? (bars[bars.length - 2].v || 0) / Math.max(1e-9, Q.mean(bars.slice(-22, -2).map((b) => b.v || 0))) : null, news: Loki.guard ? Loki.guard.newsLock() : null },
    };
    decision.postMortem = approved
      ? `APPROVED ${yes}/${jurors.length} (needs ${need}).${dissent.length ? ` Dissent: ${dissent.map((d) => `${d.name}: ${d.why}`).join(' | ')}` : ' Unanimous.'}`
      : risk.veto ? `KILLED by the Risk Manager veto (${risk.vetoes.join('; ')}). Jury stood ${yes}/${jurors.length}.`
        : `REJECTED ${yes}/${jurors.length} (needs ${need}). For: ${jurors.filter((j) => j.vote).map((j) => j.name).join(', ') || 'nobody'}. Minority reasoning: ${dissent.map((d) => `${d.name}: ${d.why}`).join(' | ') || 'none'}`;
    const log = Loki.store.get(LOG, []); log.push(slim(decision)); Loki.store.set(LOG, log.slice(-100));
    Loki.emit('jury:decision', decision);
    return decision;
  }
  const slim = (d) => ({ id: d.id, t: d.t, symbol: d.symbol, side: d.side, qty: d.qty, price: d.price, strategy: d.strategy, approved: d.approved, yes: d.yes, need: d.need, veto: d.risk.veto, vetoes: d.risk.vetoes,
    safety: d.redteam.safety, mdd: d.redteam.expectedMaxDD, jurors: d.jurors.map((j) => ({ id: j.id, vote: j.vote, why: j.why })), postMortem: d.postMortem, plan: d.plan });
  const history = () => Loki.store.get(LOG, []).slice().reverse();

  Loki.jury = { evaluate, quantAgent, crossExamine, riskManager, history };
})();
