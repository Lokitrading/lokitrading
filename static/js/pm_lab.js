/* PREDICTION MARKETS v2 — a paper market-maker for Polymarket's liquidity
 * rewards, and a Kalshi ↔ Polymarket cross-venue check.
 *
 * Market-maker (paper, on this device): every 5 seconds it reads the real CLOB
 * book of a rewarded market and posts a two-sided quote inside the market's
 * reward band, skewed against its inventory (holding YES lowers both prices so
 * the bid fills less and the ask more). Fills are NEVER assumed: it reads the
 * market's real public trade prints (data-api.polymarket.com) and only fills
 * when a real taker trade crossed its price; a trade exactly at its price is
 * credited half, because a new quote waits behind the queue. The reward
 * estimate follows Polymarket's published scoring: ((max spread - distance) /
 * max spread)² × size per side, two-sided minimum, compared with the size
 * already resting in the band. Nothing is signed or sent.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  const mmRoot = document.getElementById('pm-mm');
  if (!Loki || !mmRoot) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt;
  const KEY = 'loki_pm_mm_v1';
  const TICK_MS = 5000;
  const fresh = () => ({ cash: 1000, start: 1000, inv: 0, fills: [], log: [], rewards: 0, scoreTicks: 0, inBandTicks: 0, ticks: 0 });
  let book = Loki.store.get(KEY, null) || fresh();
  let markets = [], market = null, quote = null, lastTs = 0, timer = null, busy = false, lastBook = null;
  const save = () => { book.fills = book.fills.slice(-60); book.log = book.log.slice(-60); Loki.store.set(KEY, book); };
  const say = (text, kind = '') => { book.log.push({ t: Date.now(), text, kind }); };
  const cents = (x) => `${(x * 100).toFixed(1)}¢`;

  async function loadMarkets() {
    const rows = await Loki.getJSON('https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false', { timeout: 15000 });
    markets = (rows || []).map((m) => {
      let ids = [];
      try { ids = JSON.parse(m.clobTokenIds || '[]'); } catch { /* malformed */ }
      const rate = Array.isArray(m.clobRewards) && m.clobRewards[0] ? Number(m.clobRewards[0].rewardsDailyRate) || null : null;
      return { id: String(m.id), q: String(m.question || '').slice(0, 110), condition: m.conditionId, yes: ids[0], no: ids[1], maxSpread: Number(m.rewardsMaxSpread) || 0,
        minSize: Number(m.rewardsMinSize) || 0, tick: Number(m.orderPriceMinTickSize) || 0.01, bid: Number(m.bestBid), ask: Number(m.bestAsk), daily: rate, vol: Number(m.volume24hr) || 0 };
    }).filter((m) => m.yes && m.no && m.condition && m.maxSpread > 0 && m.bid > 0.05 && m.ask < 0.95).slice(0, 40);
    $('mm-market').innerHTML = markets.map((m) => `<option value="${F.esc(m.id)}">${F.esc(m.q)} · band ±${m.maxSpread}¢ · min ${m.minSize}</option>`).join('') || '<option value="">No rewarded markets right now</option>';
    pick();
  }
  function pick() {
    const next = markets.find((m) => m.id === $('mm-market').value) || markets[0] || null;
    if (market && next && market.id !== next.id && book.inv) say(`Switched market with ${book.inv.toFixed(0)} shares still held in the old one; they stay marked at their last price.`);
    market = next; quote = null; lastTs = Math.floor(Date.now() / 1000);
    render();
  }

  const levels = (side) => (side || []).map((l) => [Number(l.price), Number(l.size)]).filter(([p, s]) => p > 0 && p < 1 && s > 0);

  /** Polymarket's per-side reward score for size resting `dist` from the mid. */
  const sideScore = (v, dist, size) => (dist <= v ? ((v - dist) / v) ** 2 * size : 0);
  // Two-sided quotes are required near the middle; one side alone scores a third.
  const combine = (one, two, mid) => (mid >= 0.1 && mid <= 0.9 ? Math.max(Math.min(one, two), Math.max(one / 3, two / 3)) : Math.max(one, two));

  async function tick() {
    if (!market || busy || document.hidden || mmRoot.closest('[hidden]')) return;
    busy = true;
    try {
      const b = await Loki.getJSON(`https://clob.polymarket.com/book?token_id=${market.yes}`, { timeout: 8000 });
      const bids = levels(b.bids).sort((x, z) => z[0] - x[0]), asks = levels(b.asks).sort((x, z) => x[0] - z[0]);
      if (!bids.length || !asks.length) throw new Error('one side of the book is empty');
      const bestBid = bids[0][0], bestAsk = asks[0][0], mid = (bestBid + bestAsk) / 2, v = market.maxSpread / 100, t = market.tick;
      lastBook = { bestBid, bestAsk, mid };

      // 1. Fills from real trade prints since the last tick, against the quote that was resting.
      if (quote) {
        const prints = await Loki.getJSON(`https://data-api.polymarket.com/v2/trades?condition=${market.condition}&limit=100`, { timeout: 8000 }).catch(() => null);
        const rows = (prints && prints.data) || [];
        for (const tr of rows.filter((x) => x.timestamp > lastTs).sort((a, z) => a.timestamp - z.timestamp)) {
          const isYes = tr.token_id === market.yes;
          const side = isYes ? tr.side : (tr.side === 'BUY' ? 'SELL' : 'BUY');
          const px = isYes ? Number(tr.price) : 1 - Number(tr.price), size = Number(tr.size) || 0;
          if (side === 'SELL' && quote.bidSize > 0 && px <= quote.bid + 1e-9) {
            const q = Math.min(quote.bidSize, px < quote.bid - 1e-9 ? size : size / 2);
            if (q > 0 && book.cash >= q * quote.bid) { book.cash -= q * quote.bid; book.inv += q; quote.bidSize -= q; book.fills.push({ t: tr.timestamp * 1000, side: 'buy', px: quote.bid, q }); say(`Bid filled: bought ${q.toFixed(0)} YES at ${cents(quote.bid)} (a real ${size.toFixed(0)}-share sell printed at ${cents(px)}).`, 'up'); }
          } else if (side === 'BUY' && quote.askSize > 0 && px >= quote.ask - 1e-9) {
            const q = Math.min(quote.askSize, px > quote.ask + 1e-9 ? size : size / 2);
            if (q > 0) { book.cash += q * quote.ask; book.inv -= q; quote.askSize -= q; book.fills.push({ t: tr.timestamp * 1000, side: 'sell', px: quote.ask, q }); say(`Ask filled: sold ${q.toFixed(0)} YES at ${cents(quote.ask)} (a real ${size.toFixed(0)}-share buy printed at ${cents(px)}).`, 'down'); }
          }
        }
        if (rows.length) lastTs = Math.max(lastTs, ...rows.map((x) => x.timestamp));
      }

      // 2. New quote: inside the reward band, skewed against inventory, post-only.
      const size = Math.max(market.minSize, Number($('mm-size').value) || 50), maxInv = size * 4;
      const width = Math.min(v * 0.9, Math.max(t, (Number($('mm-width').value) || 2) / 100));
      const skew = -(book.inv / maxInv) * width;
      let bid = Math.floor((mid - width / 2 + skew) / t) * t, ask = Math.ceil((mid + width / 2 + skew) / t) * t;
      if (bid >= bestAsk) bid = bestAsk - t;
      if (ask <= bestBid) ask = bestBid + t;
      bid = +bid.toFixed(4); ask = +ask.toFixed(4);
      const bidSize = book.inv >= maxInv || bid <= 0 ? 0 : size, askSize = book.inv <= -maxInv || ask >= 1 ? 0 : size;
      quote = { bid, ask, bidSize, askSize, at: Date.now() };

      // 3. Reward share vs the size already resting in the band.
      const ours = combine(bidSize ? sideScore(v, mid - bid, bidSize) : 0, askSize ? sideScore(v, ask - mid, askSize) : 0, mid);
      const others = combine(bids.reduce((a, [p, s]) => a + sideScore(v, mid - p, s), 0), asks.reduce((a, [p, s]) => a + sideScore(v, p - mid, s), 0), mid);
      const share = ours > 0 ? ours / (ours + others) : 0;
      book.ticks += 1;
      if (ours > 0) { book.inBandTicks += 1; book.scoreTicks += share; }
      if (market.daily) book.rewards += share * market.daily * (TICK_MS / 86400000);
      quote.share = share; quote.mid = mid;
      Loki.emit('order', { venue: 'polymarket', side: 'quote', symbol: `MM ${cents(bid)} / ${cents(ask)}`, qty: size, paper: true });
      save();
    } catch (error) {
      say(`Paused this tick: ${error.message}`, 'down');
    } finally { busy = false; render(); }
  }

  function render() {
    const mid = lastBook ? lastBook.mid : market ? (market.bid + market.ask) / 2 : 0;
    const equity = book.cash + book.inv * mid, pnl = equity - book.start;
    const fills = book.fills.length, inBand = book.ticks ? book.inBandTicks / book.ticks : 0;
    $('mm-stats').innerHTML = [
      ['Paper equity', F.usd(equity), pnl >= 0 ? 'up' : 'down', `${F.usd(pnl, true)} incl. inventory at mid`],
      ['Inventory', `${book.inv >= 0 ? '' : '−'}${Math.abs(book.inv).toFixed(0)} ${book.inv >= 0 ? 'YES' : 'NO-equiv'}`, '', `${fills} real-print fills`],
      ['Quote', quote ? `${cents(quote.bid)} / ${cents(quote.ask)}` : '—', '', quote ? `mid ${cents(quote.mid)} · band ±${market.maxSpread}¢` : 'waiting for the first tick'],
      ['Reward share', quote ? `${(quote.share * 100).toFixed(1)}%` : '—', '', `in band ${(inBand * 100).toFixed(0)}% of ticks`],
      ['Est. rewards', market && market.daily ? F.usd(book.rewards) : 'pool not published', '', market && market.daily ? `pool ${F.usd(market.daily)}/day` : 'share shown instead'],
    ].map(([k, v, cls, sub]) => `<div><span>${k}</span><b class="${cls}">${F.esc(v)}</b><em>${F.esc(sub)}</em></div>`).join('');
    $('mm-log').innerHTML = book.log.slice(-25).reverse().map((l) => `<li class="${l.kind}"><time>${new Date(l.t).toLocaleTimeString()}</time> ${F.esc(l.text)}</li>`).join('') || '<li class="muted">Start the simulator to post the first paper quote.</li>';
    $('mm-toggle').textContent = timer ? 'Pause ❚❚' : 'Start market-making ▶';
    $('mm-toggle').setAttribute('aria-pressed', String(Boolean(timer)));
  }

  function toggle() {
    if (timer) { clearInterval(timer); timer = null; say('Paused.'); render(); return; }
    if (!market) return;
    say(`Quoting ${market.q} (reward band ±${market.maxSpread}¢, minimum ${market.minSize} shares).`);
    timer = setInterval(tick, TICK_MS); tick(); render();
  }

  $('mm-market').addEventListener('change', pick);
  $('mm-toggle').addEventListener('click', () => { if (!markets.length) loadMarkets().then(toggle).catch((e) => { say(e.message, 'down'); render(); }); else toggle(); });
  $('mm-reset').addEventListener('click', () => { if (!window.confirm('Reset the market-maker to $1,000 paper and clear its fills?')) return; book = fresh(); quote = null; save(); render(); });

  /* -------------------------------------------- Kalshi ↔ Polymarket check */
  async function loadXarb() {
    const box = $('xarb-list'), btn = $('xarb-refresh');
    btn.disabled = true; box.innerHTML = '<p class="muted">Matching thousands of Kalshi markets against Polymarket…</p>';
    try {
      const d = await Loki.getJSON('/api/xarb', { timeout: 40000 });
      $('xarb-note').textContent = `${d.kalshi_markets.toLocaleString()} Kalshi and ${d.polymarket_markets.toLocaleString()} Polymarket markets compared. ${d.warning}`;
      box.innerHTML = d.pairs.length ? d.pairs.map((p) => `<article class="xarb-row ${p.arb ? 'arb' : ''}">
        <div class="xarb-head"><b class="${p.arb ? 'up' : ''}">${p.arb ? `${(p.locked_edge * 100).toFixed(1)}¢ under $1` : `${((p.cost - 1) * 100).toFixed(1)}¢ over $1`}</b><span>${F.esc(p.trade)} · cost ${cents(p.cost)} incl. Kalshi fee · title match ${Math.round(p.similarity * 100)}%</span></div>
        <p><a href="${F.esc(p.polymarket.url)}" target="_blank" rel="noopener">Polymarket</a> ${F.esc(p.polymarket.title)} · YES ${cents(p.polymarket.yes_ask)} / NO ${cents(p.polymarket.no_ask)}</p>
        <p><a href="${F.esc(p.kalshi.url)}" target="_blank" rel="noopener">Kalshi</a> ${F.esc(p.kalshi.title)} · YES ${cents(p.kalshi.yes_ask)} / NO ${cents(p.kalshi.no_ask)}</p>
        <details><summary>Compare the resolution rules</summary><p class="caption"><b>Polymarket:</b> ${F.esc(p.polymarket.rules || 'no rule text returned')}</p><p class="caption"><b>Kalshi:</b> ${F.esc(p.kalshi.rules || 'no rule text returned')}</p></details>
      </article>`).join('') : '<p class="muted">No look-alike questions right now.</p>';
    } catch (error) {
      box.innerHTML = `<p class="form-message">${F.esc(error.message)}</p>`;
    } finally { btn.disabled = false; }
  }
  $('xarb-refresh').addEventListener('click', loadXarb);

  render();
  const onView = () => { const v = document.getElementById('predictions'); if (v && !v.hidden && !markets.length) loadMarkets().catch((e) => { $('mm-market').innerHTML = `<option value="">${F.esc(e.message)}</option>`; }); };
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  onView();
})();
