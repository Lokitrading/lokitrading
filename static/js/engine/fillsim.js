/* LOCAL PAPER FILL ENGINE — deterministic, institutional-style fills on this device.
 *
 * Naive paper engines fill a limit order the moment price touches it. This one:
 *  - Queue position: a limit order joins the BACK of the queue at its price. It
 *    fills only after the volume ahead of it trades at that price; a bar that
 *    trades THROUGH the price fills it fully, a bar that only touches it fills
 *    what is left after the queue ahead (partial fills are normal).
 *  - Latency: 50–500 ms between signal and arrival (seeded), during which the
 *    price drifts by the bar's own volatility.
 *  - Market impact: square-root law. Above 5% of the bar's volume the average
 *    fill walks away from the arrival price by Y·σ·√(participation).
 *  - Adversarial market makers: a large or fast-participating order makes the
 *    simulated makers widen the spread and fade quotes against it.
 *  - Algorithms: TWAP (equal slices), VWAP (a fixed share of each bar's volume)
 *    and an optional dark-pool node that tries a midpoint fill for each slice
 *    with a limited, seeded amount of hidden liquidity.
 *  - Margin: leveraged replays liquidate positions when equity falls below the
 *    maintenance requirement, the way a broker would.
 * Same inputs + same seed = same fills, so a backtest and a paper replay agree.
 * Nothing here touches the network: bars are passed in (and cached by the page).
 */
'use strict';
(function (root) {
  const Q = root.LokiQuant;
  const DEF = { spread_bps: 2, depth_at_touch: 0, latency_ms: [50, 500], impact_y: 0.8, front_run_participation: 0.1, tick: 0.01, expire_bars: 30, dark_hit: 0.35, dark_size: 0.4 };

  function barSigma(bars, i) { // per-bar volatility from the previous 30 bars (no look-ahead)
    const s = Math.max(1, i - 30), r = []; for (let k = s; k <= i && k < bars.length; k++) if (bars[k - 1]) r.push(Math.log(bars[k].c / bars[k - 1].c));
    return Q.std(r) || 0.001;
  }

  /**
   * Fill one order against bars starting at index `start` (the bar AFTER the signal).
   * order: { side: 'buy'|'sell', qty, type: 'market'|'limit', limit?, algo?: 'none'|'twap'|'vwap', slices?, pov?, dark?: bool }
   * Returns { fills, filledQty, avgPx, arrival, slippageBps, latencyMs, partial, unfilled, events, frontRun, darkShare }
   */
  function fill(order, bars, start, opts = {}) {
    const o = { ...DEF, ...opts }, r = Q.rng(o.seed || 1), dir = order.side === 'sell' ? -1 : 1;
    const qty = Math.abs(order.qty), events = [], fills = [];
    if (!bars[start]) return { fills, filledQty: 0, avgPx: null, arrival: null, slippageBps: null, latencyMs: 0, partial: false, unfilled: qty, events: ['no bar to trade on'], frontRun: false, darkShare: 0 };
    const [lmin, lmax] = o.latency_ms, latencyMs = Math.round(lmin + r() * (lmax - lmin));
    const barMs = bars[start + 1] ? Math.max(1000, bars[start + 1].t - bars[start].t) : 60000;
    const sig0 = barSigma(bars, start);
    const arrival = bars[start].o;
    const drift = Math.exp(sig0 * Math.sqrt(latencyMs / barMs) * Q.gauss(r)); // price moves while the order is in flight
    events.push(`latency ${latencyMs}ms: price moved ${((drift - 1) * 1e4).toFixed(1)}bp before arrival`);
    let left = qty, frontRun = false, darkQty = 0;

    const take = (i, want, px, venue) => { const q = Math.min(left, want); if (q <= 1e-12) return 0; fills.push({ i, t: bars[i].t, qty: q, px, venue }); left -= q; return q; };
    const marketFill = (i, want, firstBar) => {
      const b = bars[i], sig = barSigma(bars, i), part = b.v > 0 ? want / b.v : 1;
      let spread = o.spread_bps / 1e4;
      if (part > o.front_run_participation) { spread *= 1 + part * 10; if (!frontRun) events.push(`front-running: ${(part * 100).toFixed(1)}% of bar volume, makers widen the spread ×${(1 + part * 10).toFixed(1)} and fade quotes`); frontRun = true; }
      const impact = part > 0.05 ? o.impact_y * sig * Math.sqrt(part) : 0;
      const ref = (firstBar ? arrival * drift : b.o);
      const px = ref * (1 + dir * (spread / 2 + impact));
      if (impact > 0 && firstBar) events.push(`impact: ${(part * 100).toFixed(1)}% of bar volume moves the average fill ${(impact * 1e4).toFixed(1)}bp`);
      return take(i, want, px, 'lit');
    };
    const darkTry = (i, want) => {
      if (!order.dark || r() > o.dark_hit) return 0;
      const mid = bars[i].o, q = take(i, want * o.dark_size * (0.5 + r()), mid, 'dark'); darkQty += q; return q;
    };

    const algo = order.algo || 'none';
    if (order.type === 'limit' && algo === 'none') {
      const L = order.limit; let queue = o.depth_at_touch || (bars[start].v || 0) * 0.02; // volume resting ahead of us at our price
      events.push(`limit ${L}: joined the queue behind ${queue.toFixed(2)} units`);
      for (let i = start; i < Math.min(bars.length, start + o.expire_bars) && left > 1e-12; i++) {
        const b = bars[i], through = dir > 0 ? b.l < L - o.tick / 2 : b.h > L + o.tick / 2, touch = dir > 0 ? b.l <= L + o.tick / 2 : b.h >= L - o.tick / 2;
        if (through) { take(i, left, L, 'lit'); events.push(`bar ${i - start}: traded through ${L}, filled the rest`); break; }
        if (touch) {
          const range = Math.max(o.tick, b.h - b.l), atPrice = (b.v || 0) * Math.min(1, o.tick * 5 / range); // share of the bar's volume printed at our tick
          const eaten = Math.min(queue, atPrice); queue -= eaten;
          const got = take(i, atPrice - eaten, L, 'lit');
          if (got > 0) events.push(`bar ${i - start}: touched ${L}, queue cleared, partial fill ${got.toFixed(4)}`);
          else events.push(`bar ${i - start}: touched ${L}, still ${queue.toFixed(2)} ahead in the queue`);
        }
      }
      if (left > 1e-12) events.push(`expired after ${o.expire_bars} bars with ${left.toFixed(4)} unfilled`);
    } else if (algo === 'twap' || algo === 'vwap') {
      const n = Math.max(1, order.slices || 10), pov = order.pov || 0.05;
      for (let k = 0, i = start; i < bars.length && left > 1e-12 && k < n * 4; k++, i++) {
        const want = algo === 'twap' ? qty / n : Math.max(qty / (n * 4), (bars[i].v || 0) * pov);
        const d = darkTry(i, want);
        marketFill(i, Math.max(0, want - d), k === 0);
        if (algo === 'twap' && k + 1 >= n) break;
      }
      events.push(`${algo.toUpperCase()}: ${fills.length} child fills${order.dark ? `, ${fills.filter((f) => f.venue === 'dark').length} in the dark pool` : ''}`);
      if (left > 1e-12) events.push(`${left.toFixed(4)} not executed inside the window`);
    } else {
      darkTry(start, qty);
      marketFill(start, left, true);
    }
    const filledQty = fills.reduce((a, f) => a + f.qty, 0), notional = fills.reduce((a, f) => a + f.qty * f.px, 0);
    const avgPx = filledQty > 0 ? notional / filledQty : null;
    return { fills, filledQty, avgPx, arrival, latencyMs, partial: filledQty > 0 && left > 1e-12, unfilled: left, frontRun, darkShare: filledQty ? darkQty / filledQty : 0, events,
      slippageBps: avgPx ? dir * (avgPx / arrival - 1) * 1e4 : null };
  }

  /**
   * Deterministic local replay of a signal function over bars, with leverage and margin calls.
   * signal(i, bars) → 'buy' | 'sell' | null, evaluated on CLOSED bar i; orders fill from bar i+1.
   * opts: { cash, sizePct, leverage, maintenance, order: {type, algo, ...}, seed, stopPct }
   */
  function replay(signal, bars, opts = {}) {
    const o = { cash: 10000, sizePct: 1, leverage: 1, maintenance: 0.25, order: { type: 'market' }, seed: 7, ...opts };
    let cash = o.cash, qty = 0, entry = 0, liquidations = 0, seed = o.seed;
    const equity = [], trades = [], log = [];
    for (let i = 1; i < bars.length - 1; i++) {
      const px = bars[i].c, exposure = Math.abs(qty) * px, eq = cash + qty * px;
      // Broker margin check on the bar's worst price.
      if (qty !== 0 && o.leverage > 1) {
        const worst = qty > 0 ? bars[i].l : bars[i].h, eqWorst = cash + qty * worst;
        if (eqWorst < o.maintenance * Math.abs(qty) * worst) {
          const f = fill({ side: qty > 0 ? 'sell' : 'buy', qty: Math.abs(qty), type: 'market' }, bars, i + 1, { ...o.fill, seed: seed++ });
          if (f.avgPx) { cash += qty * f.avgPx; trades.push({ t: bars[i + 1].t, side: 'liquidation', qty, px: f.avgPx, pnl: qty * (f.avgPx - entry) }); log.push(`margin call at bar ${i}: equity below ${o.maintenance * 100}% maintenance, position force-closed at ${f.avgPx.toFixed(2)}`); qty = 0; liquidations++; }
          equity.push({ t: bars[i].t, v: cash }); continue;
        }
      }
      const s = signal(i, bars, { qty, entry, cash, equity: eq });
      if (s === 'buy' && qty <= 0 || s === 'sell' && qty > 0) {
        if (qty !== 0) {
          const f = fill({ ...o.order, side: qty > 0 ? 'sell' : 'buy', qty: Math.abs(qty), type: 'market' }, bars, i + 1, { ...o.fill, seed: seed++ });
          if (f.avgPx) { cash += qty * f.avgPx; trades.push({ t: bars[i + 1].t, side: 'close', qty, px: f.avgPx, pnl: qty * (f.avgPx - entry), slippageBps: f.slippageBps }); qty = 0; }
        }
        if (s === 'buy') {
          const notional = Math.max(0, eq) * o.sizePct * o.leverage, want = notional / bars[i + 1].o;
          const f = fill({ ...o.order, side: 'buy', qty: want, limit: o.order.type === 'limit' ? +(px * (1 - (o.order.offset_bps || 5) / 1e4)).toFixed(2) : undefined }, bars, i + 1, { ...o.fill, seed: seed++ });
          if (f.filledQty > 0) { cash -= f.filledQty * f.avgPx; qty = f.filledQty; entry = f.avgPx; trades.push({ t: bars[i + 1].t, side: 'buy', qty, px: f.avgPx, slippageBps: f.slippageBps, partial: f.partial }); }
        }
      } else if (qty > 0 && o.stopPct && bars[i].l <= entry * (1 - o.stopPct / 100)) {
        const f = fill({ side: 'sell', qty, type: 'market' }, bars, i + 1, { ...o.fill, seed: seed++ });
        if (f.avgPx) { cash += qty * f.avgPx; trades.push({ t: bars[i + 1].t, side: 'stop', qty, px: f.avgPx, pnl: qty * (f.avgPx - entry) }); qty = 0; }
      }
      equity.push({ t: bars[i].t, v: cash + qty * bars[i].c });
    }
    const final = cash + qty * bars[bars.length - 1].c;
    return { final, ret: final / o.cash - 1, maxDD: Q.maxDrawdown(equity.map((e) => e.v)), trades, equity, liquidations, log };
  }

  root.LokiFill = { fill, replay, DEF };
  if (root.Loki && !root.Loki.fillsim) root.Loki.fillsim = root.LokiFill;
})(typeof self !== 'undefined' ? self : this);
