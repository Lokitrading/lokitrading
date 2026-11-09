/* MULTI-AGENT CONSENSUS GATE — no live order leaves without a weighted vote.
 *
 * Six specialist agents look at the same proposed order:
 *   Macro       central-bank tone (Fed/ECB/BoE press feeds) + the S&P 500 trend
 *   Liquidity   order-book depth and spread vs the order's size     (can veto)
 *   Sentiment   the swarm's crowd bias, when it is running
 *   Council     the six-bot council's learned probability
 *   Risk        risk-matrix level and the continuous Monte Carlo VaR (can veto)
 *   Execution   venue health from the execution-quality monitor     (can veto)
 * Directional votes (-1..1, positive = agrees with the side) are weighted
 * Macro 20% · Sentiment 20% · Council 60% and renormalised over the agents
 * that reported. Approval needs: at least three agents reporting, a weighted
 * score of at least +0.10 for an entry (exits always pass the directional
 * test, since reducing risk is never blocked), and no veto. Every vote and
 * its reason is returned, so the decision can be read and audited.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.consensus) return;
  const { clamp, mean } = Loki.math;
  const WEIGHTS = { macro: 0.2, sentiment: 0.2, council: 0.6 };
  const PASS = 0.1;
  let macroCache = null;

  async function macroView() {
    if (macroCache && Date.now() - macroCache.at < 5 * 60000) return macroCache;
    const [news, spy] = await Promise.all([
      Loki.getJSON('/api/newswire', { timeout: 15000 }).catch(() => null),
      Loki.data.bars('SPY', { interval: '1d', limit: 60 }).catch(() => []),
    ]);
    const tone = news && Number.isFinite(news.tone) ? news.tone : null; // -1 dovish .. +1 hawkish
    let trend = null;
    if (spy.length > 25) { const c = spy.map((b) => b.c); trend = clamp((c[c.length - 1] / mean(c.slice(-20)) - 1) * 20, -1, 1); }
    macroCache = { at: Date.now(), tone, trend, latest: news && news.items && news.items[0] };
    return macroCache;
  }

  /**
   * order: { symbol, side: 'buy'|'sell', notional, venue, exit?: boolean, kind?: 'stock'|'crypto'|'future'|'binary' }
   * ctx:   { council?: council.analyze result, risk?: risk.evaluate result, mc?: Monte Carlo result,
   *          book?: {bids, asks}, dailyLossLimit?: number (dollars) }
   */
  async function vote(order, ctx = {}) {
    const dir = order.side === 'sell' ? -1 : 1;
    const votes = [];
    const add = (id, name, v) => votes.push({ id, name, ...v });

    try {
      const m = await macroView();
      if (m.tone == null && m.trend == null) throw new Error('no macro data');
      const riskOn = (m.trend ?? 0) * 0.7 - (m.tone ?? 0) * 0.5; // hawkish central banks weigh on risk assets
      add('macro', 'Macro-Sensing', { score: clamp(riskOn * dir, -1, 1), why: `${m.tone == null ? 'no central-bank read' : `central banks ${m.tone > 0.15 ? 'hawkish' : m.tone < -0.15 ? 'dovish' : 'neutral'} (${m.tone.toFixed(2)})`}; S&P 500 ${m.trend == null ? 'trend unknown' : m.trend >= 0 ? 'above' : 'below'} its 20-day mean` });
    } catch { add('macro', 'Macro-Sensing', { score: null, why: 'macro feeds unavailable — abstains' }); }

    const book = ctx.book;
    if (book && book.bids && book.asks && book.bids.length && book.asks.length) {
      const mid = (book.bids[0][0] + book.asks[0][0]) / 2, spreadBps = (book.asks[0][0] - book.bids[0][0]) / mid * 1e4;
      const side = dir > 0 ? book.asks : book.bids, band = mid * 0.005;
      const depth = side.filter(([p]) => Math.abs(p - mid) <= band).reduce((a, [p, q]) => a + p * q, 0);
      const veto = depth < (order.notional || 0) * 3 || spreadBps > 25;
      add('liquidity', 'Order-Book Liquidity', { score: null, veto, why: `spread ${spreadBps.toFixed(1)}bp, $${Math.round(depth).toLocaleString()} within ±0.5% vs order $${Math.round(order.notional || 0).toLocaleString()}${veto ? ' — too thin for this size' : ''}` });
    } else {
      add('liquidity', 'Order-Book Liquidity', { score: null, veto: false, why: order.kind === 'stock' ? 'listed equity: exchange liquidity assumed for a retail-size order' : 'no book available — not vetoing, sized by the risk matrix instead' });
    }

    const sw = Loki.swarm ? Loki.swarm.snapshot() : null;
    if (sw && sw.running) add('sentiment', 'Sentiment-Scraper', { score: clamp(sw.sentiment * 2 * dir, -1, 1), why: `swarm crowd bias ${sw.sentiment >= 0 ? '+' : ''}${sw.sentiment.toFixed(2)}${sw.event ? ', volatility event in progress' : ''}` });
    else add('sentiment', 'Sentiment-Scraper', { score: null, why: 'swarm not deployed — abstains' });

    if (ctx.council) add('council', 'Six-Bot Council', { score: clamp((ctx.council.p_up - (Number.isFinite(ctx.council.usual) ? ctx.council.usual : 0.5)) * 6 * dir, -1, 1), why: `${Math.round(ctx.council.p_up * 100)}% up over ${ctx.council.horizon} bars (confidence ${Math.round(ctx.council.confidence * 100)}%)` });
    else add('council', 'Six-Bot Council', { score: null, why: 'no council read for this instrument — abstains' });

    const risk = ctx.risk || Loki.risk.evaluate({ swarm: sw });
    const mc = ctx.mc, limit = ctx.dailyLossLimit;
    const varBreach = mc && limit && mc.var95 > limit;
    add('risk', 'Risk-Manager', { score: null, veto: !order.exit && (risk.level === 'red' || Boolean(varBreach)),
      why: `risk ${risk.level.toUpperCase()} (${risk.reason})${mc ? `; VaR95 ${Loki.fmt.usd(mc.var95)}${limit ? ` vs daily limit ${Loki.fmt.usd(limit)}` : ''}` : ''}` });

    const ex = Loki.execq ? Loki.execq.stats(order.venue || 'alpaca-paper') : { state: 'unknown' };
    add('execution', 'Execution-Quality', { score: null, veto: ex.state === 'down', why: ex.state === 'unknown' ? 'no recent calls to this venue yet' : `${ex.state}: ${Math.round(ex.errorRate * 100)}% errors, p95 ${ex.p95}ms over ${ex.n} calls` });

    const directional = votes.filter((v) => v.score != null && WEIGHTS[v.id]);
    const wsum = directional.reduce((a, v) => a + WEIGHTS[v.id], 0);
    const score = wsum ? directional.reduce((a, v) => a + WEIGHTS[v.id] * v.score, 0) / wsum : 0;
    const reporting = votes.filter((v) => v.score != null || v.veto != null).length;
    const vetoes = votes.filter((v) => v.veto);
    const approved = reporting >= 3 && !vetoes.length && (order.exit || score >= PASS);
    const result = { approved, score, vetoes: vetoes.map((v) => v.name), votes, reporting,
      summary: approved ? `Approved by consensus (score ${score >= 0 ? '+' : ''}${score.toFixed(2)})` : vetoes.length ? `Vetoed by ${vetoes.map((v) => v.name).join(', ')}` : `Not enough agreement (score ${score >= 0 ? '+' : ''}${score.toFixed(2)} < +${PASS})` };
    Loki.emit('consensus:vote', result);
    return result;
  }

  Loki.consensus = { vote, WEIGHTS, PASS };
})();
