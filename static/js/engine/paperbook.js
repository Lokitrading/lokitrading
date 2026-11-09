/* LOCAL PAPER BOOK + TRADE JOURNAL — zero-dependency paper trading on this device.
 *
 * Paper book: positions, cash and pending limit orders live in this browser.
 * Every order goes through the local fill engine (queue position, latency,
 * impact, front-running, TWAP/VWAP/dark pool), seeded by the order id, so
 * re-running a fill gives exactly the same result. Stops are checked on every
 * refresh. Each close feeds the tilt detector and the journal.
 *
 * Journal: every trade keeps the plan the jury approved (signal price, stop,
 * order type, regime, volume, news proximity) next to what actually happened.
 * The post-mortem compares the two (late entry, missed stop, manual override)
 * and tags losing trades with root causes (news window, low-volume breakout,
 * choppy market, against the quant signal), then clusters them. Performance
 * cards report Sharpe, Sortino and Calmar per strategy.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.paperbook) return;
  const Q = Loki.quant;
  const KEY = 'loki_paperbook_v1';
  const fresh = () => ({ start: 10000, cash: 10000, positions: {}, pending: [], closed: [], peak: 10000, marks: {}, seq: 1 });
  let B = Loki.store.get(KEY, null) || fresh();
  const save = () => { B.closed = B.closed.slice(-300); Loki.store.set(KEY, B); Loki.emit('paperbook:update', snapshot()); };
  const hash = (s) => { let h = 2166136261; for (const ch of String(s)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };

  /** Recent bars plus a synthetic "now" bar at the latest price, so fills start from the present. */
  async function liveBars(symbol) {
    const pair = Loki.data.binancePair && Loki.data.binancePair(symbol);
    if (pair) {
      const k = await Loki.data.binanceKlines(pair, '1m', 120);
      const avgV = Q.mean(k.slice(-60).map((b) => b.v));
      const last = k[k.length - 1];
      return { bars: [...k.slice(0, -1), { t: Date.now(), o: last.c, h: last.c, l: last.c, c: last.c, v: avgV }], spread: 2, minute: true };
    }
    const d = await Loki.data.bars(symbol, { interval: '1d', limit: 60 });
    const last = d[d.length - 1], v = (last.v || 0) / 390;
    return { bars: [...d.map((b) => ({ ...b, v: (b.v || 0) / 390 })), { t: Date.now(), o: last.c, h: last.c, l: last.c, c: last.c, v }], spread: 2, minute: false };
  }
  const markOf = (sym) => B.marks[sym] || (B.positions[sym] && B.positions[sym].avg) || 0;
  const equity = () => B.cash + Object.entries(B.positions).reduce((a, [s, p]) => a + p.qty * markOf(s), 0);

  /** Execute a jury decision (or a manual order) in the local paper book. */
  async function execute(order) {
    // order: { symbol, side, qty, type, algo, limit?, dark?, decision?, strategy? }
    const id = `p${B.seq++}`, seed = hash(id + order.symbol);
    const { bars, spread } = await liveBars(order.symbol);
    const start = bars.length - 1, d = order.decision;
    if (d && !d.approved && Loki.guard) Loki.guard.drift({ source: 'paper book', symbol: order.symbol, side: order.side, recommended: d.risk.veto ? 'blocked' : 'hold', why: d.postMortem.slice(0, 160) });
    else if (Loki.guard) Loki.guard.drift({ source: 'paper book', symbol: order.symbol, side: order.side, recommended: d ? order.side : null });
    if (order.type === 'limit') {
      B.pending.push({ id, ...order, decision: d ? slimDecision(d) : null, placedAt: bars[start].t, placedPx: bars[start].o, seed });
      save(); return { id, pending: true };
    }
    const f = Loki.fillsim.fill({ side: order.side, qty: order.qty, type: 'market', algo: order.algo || 'none', slices: 10, dark: order.dark }, bars, start, { spread_bps: spread, seed });
    apply(order, f, id, d);
    return { id, fill: f };
  }
  const slimDecision = (d) => ({ id: d.id, approved: d.approved, plan: d.plan, quantScore: d.quant.score, strategy: d.strategy, safety: d.redteam.safety, mdd: d.redteam.expectedMaxDD });

  function apply(order, f, id, d) {
    if (!f.filledQty) return;
    const dir = order.side === 'sell' ? -1 : 1, sym = order.symbol, pos = B.positions[sym];
    const dd = d && d.plan ? d : (d ? { plan: d.plan } : null);
    if (!pos || Math.sign(pos.qty) === dir) {
      const q0 = pos ? pos.qty : 0, newQ = q0 + dir * f.filledQty;
      B.positions[sym] = { qty: newQ, avg: ((pos ? pos.avg * Math.abs(q0) : 0) + f.avgPx * f.filledQty) / Math.abs(newQ), opened: pos ? pos.opened : Date.now(), id,
        strategy: order.strategy || (d && d.strategy) || 'manual', plan: dd ? (dd.plan || null) : null, decision: d ? (d.id || null) : null, quantScore: d ? (d.quant ? d.quant.score : d.quantScore) : null,
        entry: { latencyMs: f.latencyMs, slippageBps: f.slippageBps, partial: f.partial, frontRun: f.frontRun, darkShare: f.darkShare, arrival: f.arrival } };
      B.cash -= dir * f.filledQty * f.avgPx;
    } else {
      const closeQty = Math.min(Math.abs(pos.qty), f.filledQty);
      close(sym, f.avgPx, order.exit || 'manual', closeQty, f);
    }
    B.marks[sym] = f.avgPx;
    Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'local-paper', action: order.side, symbol: sym, qty: f.filledQty, price: f.avgPx, slippage_bps: f.slippageBps != null ? +f.slippageBps.toFixed(2) : null, jury: d ? d.id : null });
    Loki.emit('order', { venue: 'loki', side: order.side, symbol: sym, qty: f.filledQty, paper: true });
    save();
  }

  function close(sym, px, exit, qty, f) {
    const p = B.positions[sym]; if (!p) return null;
    const q = Math.min(Math.abs(p.qty), qty || Math.abs(p.qty)), sign = Math.sign(p.qty);
    const pnl = sign * q * (px - p.avg), ret = sign * (px / p.avg - 1);
    B.cash += sign * q * px;
    const stopPct = p.plan ? p.plan.stopPct : Loki.config().stop_loss_pct;
    const trade = { id: p.id, symbol: sym, side: sign > 0 ? 'long' : 'short', qty: q, entry: p.avg, exit: px, pnl, ret, exitReason: exit, opened: p.opened, closed: Date.now(), strategy: p.strategy, plan: p.plan, quantScore: p.quantScore, entryFill: p.entry, exitFill: f ? { slippageBps: f.slippageBps, latencyMs: f.latencyMs } : null, stopPct };
    trade.audit = postMortem(trade);
    B.closed.push(trade);
    if (Math.abs(p.qty) - q < 1e-12) delete B.positions[sym]; else p.qty -= sign * q;
    if (Loki.guard) Loki.guard.record(p.strategy, { pnl, ret, exit: exit === 'stop' ? 'stop' : exit, maxLossPct: stopPct });
    Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'local-paper', action: 'sell', symbol: sym, price: px, pnl, exit });
    return trade;
  }

  /** Refresh marks, fill resting limit orders, trigger stops. */
  async function refresh() {
    const syms = new Set([...Object.keys(B.positions), ...B.pending.map((o) => o.symbol)]);
    for (const sym of syms) {
      let lb; try { lb = await liveBars(sym); } catch { continue; }
      const last = lb.bars[lb.bars.length - 1].c; B.marks[sym] = last;
      for (const o of B.pending.filter((x) => x.symbol === sym)) {
        const startIdx = lb.bars.findIndex((b) => b.t > o.placedAt);
        if (startIdx < 0) continue;
        const f = Loki.fillsim.fill({ side: o.side, qty: o.qty, type: 'limit', limit: o.limit }, lb.bars.slice(0, -1), startIdx, { spread_bps: lb.spread, seed: o.seed, expire_bars: 240 });
        if (f.filledQty > 0) { apply({ ...o, type: 'market' }, f, o.id, o.decision ? { ...o.decision, plan: o.decision.plan } : null); B.pending = B.pending.filter((x) => x.id !== o.id); if (f.unfilled > 1e-12) B.pending.push({ ...o, qty: f.unfilled, id: o.id + 'r', placedAt: lb.bars[lb.bars.length - 2].t }); }
        else if (Date.now() - o.placedAt > 4 * 3600e3) B.pending = B.pending.filter((x) => x.id !== o.id);
      }
      const p = B.positions[sym];
      if (p) {
        const stop = (p.plan ? p.plan.stopPct : Loki.config().stop_loss_pct) / 100;
        const hit = p.qty > 0 ? last <= p.avg * (1 - stop) : last >= p.avg * (1 + stop);
        if (hit) { const f = Loki.fillsim.fill({ side: p.qty > 0 ? 'sell' : 'buy', qty: Math.abs(p.qty), type: 'market' }, lb.bars, lb.bars.length - 1, { spread_bps: lb.spread, seed: hash(p.id + 'stop') }); close(sym, f.avgPx, 'stop', Math.abs(p.qty), f); }
      }
    }
    B.peak = Math.max(B.peak, equity());
    save();
    return snapshot();
  }
  async function closePosition(sym) {
    const p = B.positions[sym]; if (!p) return null;
    const lb = await liveBars(sym);
    const f = Loki.fillsim.fill({ side: p.qty > 0 ? 'sell' : 'buy', qty: Math.abs(p.qty), type: 'market' }, lb.bars, lb.bars.length - 1, { spread_bps: lb.spread, seed: hash(p.id + 'close') });
    const t = close(sym, f.avgPx, 'manual', Math.abs(p.qty), f); save(); return t;
  }

  /* ------------------------------------------------------------- journal */
  function postMortem(t) {
    const tags = [], notes = [];
    const plan = t.plan || {};
    if (t.entryFill && plan.signalPx) {
      const late = (t.side === 'long' ? 1 : -1) * (t.entry / plan.signalPx - 1) * 1e4;
      if (late > 25) { tags.push('late entry'); notes.push(`filled ${late.toFixed(0)}bp worse than the signal price`); }
      if (t.entryFill.partial) notes.push('entry was a partial fill');
      if (t.entryFill.frontRun) notes.push('the entry size triggered simulated front-running');
    }
    if (t.exitReason === 'stop' && t.stopPct) {
      const stopPx = t.entry * (1 - (t.side === 'long' ? 1 : -1) * t.stopPct / 100), miss = (t.side === 'long' ? 1 : -1) * (stopPx / t.exit - 1) * 100;
      if (miss > 0.3) { tags.push('missed stop'); notes.push(`exited ${miss.toFixed(2)}% beyond the planned stop (gap or slippage)`); }
    }
    if (t.exitReason === 'manual' && t.ret < 0 && t.stopPct && t.ret > -t.stopPct / 100) notes.push('closed by hand before the stop: check it was the plan, not nerves');
    if (t.ret < 0) {
      if (plan.news && (plan.news.locked || (plan.news.minutesToNext != null && plan.news.minutesToNext < 60))) tags.push('entered near major economic news');
      if (plan.volumeRatio != null && plan.volumeRatio < 0.7) tags.push('low-volume breakout');
      if (plan.efficiency != null && plan.efficiency < 0.25) tags.push('choppy consolidation');
      if (t.quantScore != null && t.quantScore * (t.side === 'long' ? 1 : -1) < 0) tags.push('against the quant signal');
      if (plan.volState === 'storm') tags.push('storm volatility regime');
      if (!tags.length) tags.push('no structural cause found (normal loss)');
    }
    return { tags, notes };
  }
  function journal() {
    const losers = B.closed.filter((t) => t.ret < 0), clusters = {};
    for (const t of losers) for (const tag of (t.audit ? t.audit.tags : [])) (clusters[tag] ||= []).push(t.id);
    const byStrategy = {};
    for (const t of B.closed) (byStrategy[t.strategy] ||= []).push(t);
    const cards = Object.entries(byStrategy).map(([id, ts]) => {
      const spanDays = Math.max(1, (ts[ts.length - 1].closed - ts[0].opened) / 86400e3), perYear = Math.max(1, ts.length / spanDays * 365);
      const perf = Q.performance(ts.map((t) => t.ret), perYear);
      return { id, trades: ts.length, wins: ts.filter((t) => t.ret > 0).length, pnl: ts.reduce((a, t) => a + t.pnl, 0), ...perf, guard: Loki.guard ? Loki.guard.status(id) : null };
    });
    return { trades: B.closed.slice().reverse(), clusters: Object.entries(clusters).map(([tag, ids]) => ({ tag, count: ids.length, share: ids.length / Math.max(1, losers.length) })).sort((a, z) => z.count - a.count), cards, losers: losers.length };
  }

  function snapshot() {
    const eq = equity();
    return { equity: eq, cash: B.cash, start: B.start, drawdown: B.peak > 0 ? Math.max(0, 1 - eq / B.peak) : 0, peak: B.peak,
      positions: Object.entries(B.positions).map(([symbol, p]) => ({ symbol, ...p, mark: markOf(symbol), pnl: p.qty * (markOf(symbol) - p.avg) })), pending: B.pending.slice(), closed: B.closed.length };
  }
  function reset() { B = fresh(); save(); }

  if (Loki.mcrisk) Loki.mcrisk.register('local-paper', async () => (Object.keys(B.positions).length ? {
    equity: equity(), positions: Object.entries(B.positions).map(([symbol, p]) => ({ id: symbol, symbol, value: p.qty * markOf(symbol), kind: Loki.data.isCrypto && Loki.data.isCrypto(symbol) ? 'crypto' : 'stock' })),
  } : null));

  Loki.paperbook = { execute, refresh, closePosition, snapshot, journal, reset, postMortem, equity };
})();
