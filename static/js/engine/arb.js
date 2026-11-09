/* PREDICTIVE ARBITRAGE ENGINE (paper) — live crypto spot vs Polymarket's
 * 5-minute "Up or Down" contracts, running entirely on this device.
 *
 * Each 5-minute market settles "Up" when Chainlink's 60-second TWAP at the end
 * of the window is at or above the price at its start. Every second the engine
 * reads Binance's 1-second candles (device-direct) and prices the contract:
 *   before the TWAP window:  P(Up) = N((S - S0) / (S·σ·√(T-60-t + 60/3)))
 *   inside it:  mean = (e·A + r·S)/60, sd = (r/60)·S·σ·√(r/3)  →  N((mean - S0)/sd)
 * with σ the per-second volatility of the last ten minutes, A the average so far.
 * It compares that with the live order books for both tokens (Polymarket's
 * CLOB, device-direct) after the market's own taker fee (rate × p(1-p)).
 *
 * Paper execution is latency-honest: a signal waits the measured round-trip
 * plus signing time, then re-reads the book and fills only if the edge is
 * still there at the new price (fill-or-kill). Settlement uses Polymarket's
 * official result once the oracle posts it. Nothing is ever sent to
 * Polymarket. Real latency arbitrage competes with co-located bots, and
 * Polymarket added these fees to blunt it, so expect most edges to vanish
 * between signal and fill: that is what this engine measures.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.arb) return;
  const { clamp, std, normCdf, mean } = Loki.math;
  const KEY = 'loki_arb_paper_v1';
  const ASSETS = { btc: 'BTCUSDT', eth: 'ETHUSDT', sol: 'SOLUSDT', xrp: 'XRPUSDT' };
  const SIGN_MS = 120;          // time to sign an order locally, on top of the network round trip
  const MAX_PLAUSIBLE_EDGE = 0.25; // a bigger "edge" means the data disagree (stale feed, oracle basis), not free money
  const STALE_SECONDS = 20;     // this many 1-second bars with no trades = the spot feed has stalled
  const STAKE = 25;             // paper dollars per trade

  const book0 = () => ({ start: 1000, cash: 1000, trades: [], stats: { windows: 0, signals: 0, fills: 0, missed: 0, edge_signal: [], edge_fill: [] } });
  let paper = Loki.store.get(KEY, null) || book0();
  const save = () => { paper.trades = paper.trades.slice(-200); ['edge_signal', 'edge_fill'].forEach((k) => { paper.stats[k] = paper.stats[k].slice(-300); }); Loki.store.set(KEY, paper); };

  const S = { running: false, asset: 'btc', market: null, s0: null, secs: [], fair: null, book: null, rtt: [], series: [], minEdge: 0.03, lag: null, error: null, signalOpen: false };
  let timer = null, busy = false;

  const feeShare = (p, sched) => (sched.rate || 0) * Math.pow(Math.max(0, p * (1 - p)), sched.exponent || 1);

  async function discover() {
    const now = Math.floor(Date.now() / 1000), start = now - (now % 300);
    for (const t of [start, start + 300]) {
      const events = await Loki.getJSON(`https://gamma-api.polymarket.com/events?slug=${S.asset}-updown-5m-${t}`);
      const m = events && events[0] && events[0].markets && events[0].markets[0];
      if (!m) continue;
      const tokens = JSON.parse(m.clobTokenIds || '[]'), outcomes = JSON.parse(m.outcomes || '[]');
      if (tokens.length !== 2) continue;
      const up = outcomes[0] === 'Up' ? 0 : 1;
      return { slug: `${S.asset}-updown-5m-${t}`, id: m.id, question: m.question, start: t, end: t + 300,
        upToken: tokens[up], downToken: tokens[1 - up], fee: m.feeSchedule || { rate: 0.07, exponent: 1 },
        lookback: (m.cryptoMarketConfig && m.cryptoMarketConfig.twapLookbackSeconds) || 60, url: `https://polymarket.com/event/${S.asset}-updown-5m-${t}` };
    }
    throw new Error('no live 5-minute market found for ' + S.asset.toUpperCase());
  }

  async function readBooks() {
    const t0 = performance.now();
    const res = await fetch('https://clob.polymarket.com/books', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify([{ token_id: S.market.upToken }, { token_id: S.market.downToken }]), signal: AbortSignal.timeout(6000) });
    const rows = await res.json();
    const rtt = performance.now() - t0;
    S.rtt.push(rtt); S.rtt = S.rtt.slice(-30);
    const side = (arr, desc) => (arr || []).map((l) => [+l.price, +l.size]).sort((a, z) => desc ? z[0] - a[0] : a[0] - z[0]);
    const pick = (id) => rows.find((b) => b.asset_id === id) || {};
    const up = pick(S.market.upToken), down = pick(S.market.downToken);
    return { up: { bids: side(up.bids, true), asks: side(up.asks, false) }, down: { bids: side(down.bids, true), asks: side(down.asks, false) }, at: Date.now(), rtt };
  }

  async function readSpot() {
    const pair = ASSETS[S.asset];
    const lastT = S.secs.length ? S.secs[S.secs.length - 1].t : 0;
    // Full ten-minute history once, then only the newest seconds each tick.
    const k = await Loki.data.binanceKlines(pair, '1s', Date.now() - lastT < 20000 ? 8 : 600);
    if (!k.length) throw new Error('no spot data');
    const merged = new Map(S.secs.map((b) => [b.t, b]));
    for (const b of k) merged.set(b.t, b);
    S.secs = [...merged.values()].sort((a, z) => a.t - z.t).slice(-600);
    S.stale = feedStale(S.secs, Date.now());
    if (S.market && S.s0 == null) {
      const atStart = k.find((b) => b.t === S.market.start * 1000);
      if (atStart) S.s0 = atStart.o;
      else if (Date.now() / 1000 - S.market.start < 600) {
        const first = await Loki.data.binanceKlines(pair, '1s', 1, S.market.start * 1000);
        if (first[0]) S.s0 = first[0].o;
      }
    }
  }

  /** True when the spot feed cannot be trusted: no trades in the last STALE_SECONDS bars, or old data. */
  function feedStale(secs, now) {
    if (!secs.length) return true;
    const recent = secs.slice(-STALE_SECONDS);
    const traded = recent.reduce((a, b) => a + (b.v || 0), 0);
    return traded <= 0 || now - secs[secs.length - 1].t > 10000;
  }

  /** Fair probability that the market settles Up, from the TWAP rule. */
  function fairUp(nowSec) {
    const m = S.market, k = S.secs;
    if (!m || S.s0 == null || k.length < 60) return null;
    const closes = k.map((b) => b.c), last = closes[closes.length - 1];
    const r = []; for (let i = 1; i < closes.length; i++) r.push(Math.log(closes[i] / closes[i - 1]));
    const sigma = Math.max(std(r) || 0, 2e-6);
    const T = m.end, L = m.lookback, t = Math.min(nowSec, T);
    if (t < T - L) {
      const sd = last * sigma * Math.sqrt((T - L - t) + L / 3);
      return { p: normCdf((last - S.s0) / sd), sigma, spot: last, twap: null };
    }
    const window = k.filter((b) => b.t / 1000 >= T - L && b.t / 1000 <= t).map((b) => b.c);
    const e = Math.max(0, Math.min(L, t - (T - L))), rem = Math.max(0, T - t);
    const avg = window.length ? mean(window) : last;
    const mu = (e * avg + rem * last) / L, sd = (rem / L) * last * sigma * Math.sqrt(Math.max(rem, 1e-9) / 3);
    const p = sd < 1e-9 ? (mu >= S.s0 ? 0.999 : 0.001) : normCdf((mu - S.s0) / sd);
    return { p: clamp(p, 0.001, 0.999), sigma, spot: last, twap: avg };
  }

  function edges(book, fair) {
    const ask = (b) => (b.asks[0] ? b.asks[0][0] : null);
    const upAsk = ask(book.up), downAsk = ask(book.down);
    const fee = S.market.fee;
    return {
      upAsk, downAsk,
      up: upAsk != null ? fair - upAsk - feeShare(upAsk, fee) : null,
      down: downAsk != null ? (1 - fair) - downAsk - feeShare(downAsk, fee) : null,
      mid: book.up.bids[0] && book.up.asks[0] ? (book.up.bids[0][0] + book.up.asks[0][0]) / 2 : null,
    };
  }

  /** Lag (seconds) at which market mid best follows the model's fair value. */
  function estimateLag() {
    const s = S.series.filter((x) => x.mid != null && x.fair != null);
    if (s.length < 40) return null;
    const dF = [], dM = [];
    for (let i = 1; i < s.length; i++) { dF.push(s[i].fair - s[i - 1].fair); dM.push(s[i].mid - s[i - 1].mid); }
    let best = { lag: 0, c: -Infinity };
    for (let lag = 0; lag <= 8; lag++) {
      let c = 0; for (let i = 0; i + lag < dF.length; i++) c += dF[i] * dM[i + lag];
      if (c > best.c) best = { lag, c };
    }
    return best.c > 0 ? best.lag : null;
  }

  async function tryFill(side, signalEdge, quoted) {
    paper.stats.signals += 1; paper.stats.edge_signal.push(signalEdge);
    const wait = (mean(S.rtt) || 400) / 2 + SIGN_MS;
    await new Promise((r) => setTimeout(r, wait));
    let fresh;
    try { fresh = await readBooks(); } catch { paper.stats.missed += 1; return; }
    const fair = fairUp(Date.now() / 1000);
    if (!fair) { paper.stats.missed += 1; return; }
    const e = edges(fresh, fair.p);
    const nowEdge = side === 'up' ? e.up : e.down, price = side === 'up' ? e.upAsk : e.downAsk;
    if (S.stale || nowEdge == null || price == null || nowEdge < S.minEdge * 0.5 || nowEdge > MAX_PLAUSIBLE_EDGE || paper.cash < STAKE) {
      paper.stats.missed += 1;
      Loki.emit('arb:log', { text: `${side.toUpperCase()} signal at ${(signalEdge * 100).toFixed(1)}pt edge (ask ${quoted.toFixed(2)}) MISSED: after ${Math.round(wait)}ms the book showed ${nowEdge == null ? 'no ask' : (nowEdge * 100).toFixed(1) + 'pt'}.`, kind: 'warn' });
      return;
    }
    const shares = Math.floor((STAKE / price) * 100) / 100, fee = feeShare(price, S.market.fee) * shares;
    paper.cash -= shares * price + fee;
    paper.stats.fills += 1; paper.stats.edge_fill.push(nowEdge);
    const trade = { id: `${S.market.slug}-${side}`, slug: S.market.slug, marketId: S.market.id, side, price, shares, fee, edge: nowEdge, fair: side === 'up' ? fair.p : 1 - fair.p, at: Date.now(), end: S.market.end * 1000, status: 'open', url: S.market.url, delay: Math.round(wait) };
    paper.trades.push(trade);
    save();
    Loki.emit('arb:log', { text: `Paper BUY ${side.toUpperCase()} ${shares} @ ${price.toFixed(2)} (edge ${(nowEdge * 100).toFixed(1)}pt after ${Math.round(wait)}ms delay, fee $${fee.toFixed(2)}).`, kind: 'buy' });
    Loki.emit('order', { venue: 'polymarket', side: 'buy', symbol: `${S.asset.toUpperCase()} 5m ${side}`, qty: shares, price, paper: true });
    Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'polymarket-5m', action: 'buy', market: S.market.slug, outcome: side, shares, price, fee, edge: nowEdge });
  }

  async function settle() {
    const open = paper.trades.filter((t) => t.status === 'open' && Date.now() > t.end + 20000);
    for (const t of open.slice(0, 4)) {
      try {
        const rows = await Loki.getJSON(`https://gamma-api.polymarket.com/markets?id=${t.marketId}`);
        const m = rows && rows[0];
        if (!m || !m.closed) continue;
        const prices = JSON.parse(m.outcomePrices || '[]').map(Number), outcomes = JSON.parse(m.outcomes || '[]');
        const upIdx = outcomes[0] === 'Up' ? 0 : 1;
        const upWon = prices[upIdx] >= 0.99, downWon = prices[1 - upIdx] >= 0.99;
        if (!upWon && !downWon) continue;
        const won = (t.side === 'up' && upWon) || (t.side === 'down' && downWon);
        t.status = won ? 'won' : 'lost'; t.payout = won ? t.shares : 0; t.pnl = t.payout - t.shares * t.price - t.fee;
        paper.cash += t.payout;
        Loki.emit('arb:log', { text: `${t.slug} settled ${upWon ? 'UP' : 'DOWN'}: paper ${t.side.toUpperCase()} ${won ? 'won' : 'lost'} ${Loki.fmt.usd(t.pnl, true)}.`, kind: won ? 'up' : 'down' });
        Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'polymarket-5m', action: 'settle', market: t.slug, outcome: t.side, result: won ? 'won' : 'lost', pnl: t.pnl });
      } catch { /* the oracle has not posted yet */ }
    }
    save();
  }

  async function tick() {
    if (!S.running || busy || document.hidden) return;
    busy = true;
    try {
      const nowSec = Date.now() / 1000;
      if (!S.market || nowSec >= S.market.end) {
        if (S.market) paper.stats.windows += 1;
        S.market = await discover(); S.s0 = null; S.series = []; S.signalOpen = false;
      }
      await readSpot();
      S.book = await readBooks();
      const fair = S.stale ? null : fairUp(Date.now() / 1000);
      S.fair = fair;
      if (S.stale) S.warning = 'Binance’s 1-second feed has stalled (no trades for 20s or old data): no signals until it moves again.';
      if (fair) {
        const e = edges(S.book, fair.p);
        S.edges = e;
        S.series.push({ t: Date.now(), fair: fair.p, mid: e.mid });
        S.series = S.series.slice(-300);
        S.lag = estimateLag();
        const remaining = S.market.end - nowSec;
        const best = [['up', e.up, e.upAsk], ['down', e.down, e.downAsk]].filter((x) => x[1] != null).sort((a, z) => z[1] - a[1])[0];
        const holding = paper.trades.some((t) => t.slug === S.market.slug);
        S.warning = best && best[1] > MAX_PLAUSIBLE_EDGE ? `The model and the market disagree by ${(best[1] * 100).toFixed(0)} points: that is a data problem (stale spot feed or the oracle's price differs), not an edge, so nothing is traded.` : (S.stale ? S.warning : null);
        if (best && best[1] >= S.minEdge && best[1] <= MAX_PLAUSIBLE_EDGE && remaining > 15 && !holding && !S.signalOpen) {
          S.signalOpen = true;
          tryFill(best[0], best[1], best[2]).finally(() => { S.signalOpen = false; });
        }
      }
      S.error = null;
      if (Math.random() < 0.1) settle();
    } catch (error) {
      S.error = String(error.message || error);
    } finally {
      busy = false;
      Loki.emit('arb:update', snapshot());
    }
  }

  function snapshot() {
    const st = paper.stats, closed = paper.trades.filter((t) => t.status !== 'open');
    return { running: S.running, asset: S.asset, market: S.market, s0: S.s0, fair: S.fair, edges: S.edges, rtt: mean(S.rtt), lag: S.lag, error: S.error, warning: S.warning, stale: S.stale, series: S.series,
      paper: { cash: paper.cash, start: paper.start, trades: paper.trades.slice(-15).reverse(), open: paper.trades.filter((t) => t.status === 'open').length,
        pnl: closed.reduce((a, t) => a + (t.pnl || 0), 0), wins: closed.filter((t) => t.status === 'won').length, closed: closed.length,
        signals: st.signals, fills: st.fills, missed: st.missed, edgeSignal: mean(st.edge_signal), edgeFill: mean(st.edge_fill) },
      minEdge: S.minEdge };
  }

  Loki.arb = {
    ASSETS: Object.keys(ASSETS), fairUp, feeShare, feedStale, snapshot, MAX_PLAUSIBLE_EDGE,
    start(asset) { if (asset) S.asset = asset; if (S.running) return; S.running = true; S.market = null; tick(); timer = setInterval(tick, 1000); },
    stop() { S.running = false; clearInterval(timer); timer = null; Loki.emit('arb:update', snapshot()); },
    setAsset(asset) { if (!ASSETS[asset]) return; S.asset = asset; S.market = null; S.s0 = null; S.series = []; S.secs = []; },
    setMinEdge(x) { S.minEdge = clamp(Number(x) || 0.03, 0.005, 0.2); },
    reset() { paper = book0(); save(); Loki.emit('arb:update', snapshot()); },
    _state: S,
  };
})();
