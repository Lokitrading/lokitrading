/* AUTONOMOUS SENTIMENT SWARM — six micro-agents watch different streams at once
 * and vote on how big positions should be when the market suddenly moves.
 *
 *   Price Pulse      1-minute returns vs the last two hours (Binance, device-direct)
 *   Vol Burst        15-minute vs 4-hour realised volatility
 *   Book Imbalance   bid vs ask depth near the mid (Binance order book)
 *   Odds Mover       biggest 1-hour moves on Polymarket (device-direct)
 *   Newswire         headline tone and bursts (Loki's cached news relay)
 *   Social Chatter   Reddit / Hacker News posting tone and velocity (best effort:
 *                    some browsers or networks block these; the agent says so)
 *
 * Consensus protocol, every round: each agent reports alarm (0..1), direction
 * (-1..1) and confidence. A volatility event needs a quorum of at least two
 * alarmed agents from different families (market microstructure vs crowd).
 * The swarm then publishes a position-size multiplier and a sentiment bias
 * that the council and the risk matrix use. When a later headline confirms
 * an event, the measured lead time is recorded, so "ahead of the news" is a
 * number the swarm earns, not a claim.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.swarm) return;
  const { clamp, mean, std, logReturns } = Loki.math;

  const POS = ['surge', 'soar', 'rally', 'beat', 'beats', 'record', 'bull', 'bullish', 'gain', 'gains', 'up', 'upgrade', 'approve', 'approved', 'approval', 'inflow', 'inflows', 'buy', 'breakout', 'rebound', 'jump', 'jumps', 'strong', 'optimism', 'cut', 'cuts', 'easing', 'moon', 'pump', 'adoption', 'etf'];
  const NEG = ['crash', 'plunge', 'plunges', 'slump', 'miss', 'misses', 'bear', 'bearish', 'loss', 'losses', 'down', 'downgrade', 'ban', 'hack', 'hacked', 'exploit', 'lawsuit', 'sec', 'probe', 'fraud', 'outflow', 'outflows', 'sell', 'selloff', 'sell-off', 'liquidation', 'liquidations', 'fear', 'recession', 'hike', 'hikes', 'tariff', 'tariffs', 'dump', 'collapse', 'default', 'war', 'sanctions', 'bankrupt', 'bankruptcy'];
  const tone = (text) => {
    const words = String(text || '').toLowerCase().match(/[a-z-]+/g) || [];
    let s = 0;
    for (const w of words) { if (POS.includes(w)) s += 1; else if (NEG.includes(w)) s -= 1; }
    return clamp(s / 3, -1, 1);
  };

  /** Binance's public mirror occasionally freezes; frozen bars carry zero volume. */
  const assertLive = (bars) => {
    const recent = bars.slice(-3);
    if (!recent.length || recent.every((b) => !(b.v > 0)) || Date.now() - bars[bars.length - 1].t > 3 * 60000) throw new Error('Binance feed stalled (no trades in the latest minutes)');
  };

  const agents = [
    { id: 'pulse', name: 'Price Pulse', family: 'market', every: 15000 },
    { id: 'volburst', name: 'Vol Burst', family: 'market', every: 30000 },
    { id: 'book', name: 'Book Imbalance', family: 'market', every: 10000 },
    { id: 'odds', name: 'Odds Mover', family: 'crowd', every: 60000 },
    { id: 'news', name: 'Newswire', family: 'crowd', every: 120000 },
    { id: 'social', name: 'Social Chatter', family: 'crowd', every: 120000 },
  ].map((a) => ({ ...a, status: 'idle', alarm: 0, direction: 0, confidence: 0, note: 'waiting for first read', last: 0 }));

  const state = { running: false, pair: 'BTCUSDT', rounds: [], event: null, events: Loki.store.get('loki_swarm_events_v1', []), sizeMultiplier: 1, sentiment: 0, quorum: 0, newsSeen: new Set() };
  let timers = [];

  const reads = {
    async pulse() {
      const k = await Loki.data.binanceKlines(state.pair, '1m', 121);
      assertLive(k);
      const r = logReturns(k.map((b) => b.c));
      const last = r[r.length - 1], base = r.slice(0, -1), sd = std(base) || 1e-9;
      const z = (last - mean(base)) / sd;
      return { alarm: clamp((Math.abs(z) - 2) / 3, 0, 1), direction: clamp(z / 4, -1, 1), confidence: 0.8, note: `last minute ${z >= 0 ? '+' : ''}${z.toFixed(1)}σ` };
    },
    async volburst() {
      const k = await Loki.data.binanceKlines(state.pair, '1m', 240);
      assertLive(k);
      const r = logReturns(k.map((b) => b.c));
      const ratio = (std(r.slice(-15)) || 1e-9) / (std(r) || 1e-9);
      const drift = r.slice(-15).reduce((a, x) => a + x, 0);
      return { alarm: clamp((ratio - 1.4) / 1.6, 0, 1), direction: clamp(drift * 200, -1, 1), confidence: 0.7, note: `15m/4h vol ratio ${ratio.toFixed(2)}` };
    },
    async book() {
      const d = await Loki.data.binanceDepth(state.pair, 100);
      if (!d.bids.length || !d.asks.length) throw new Error('empty book');
      const mid = (d.bids[0][0] + d.asks[0][0]) / 2, band = mid * 0.005;
      const bid = d.bids.filter(([p]) => p >= mid - band).reduce((a, [p, q]) => a + p * q, 0);
      const ask = d.asks.filter(([p]) => p <= mid + band).reduce((a, [p, q]) => a + p * q, 0);
      const imb = (bid - ask) / ((bid + ask) || 1), spreadBps = (d.asks[0][0] - d.bids[0][0]) / mid * 1e4;
      const thin = Math.min(bid, ask) < 250000;
      return { alarm: clamp((Math.abs(imb) - 0.35) / 0.5 + (thin ? 0.3 : 0) + (spreadBps > 5 ? 0.3 : 0), 0, 1), direction: clamp(imb * 1.5, -1, 1), confidence: 0.6, note: `imbalance ${(imb * 100).toFixed(0)}%, spread ${spreadBps.toFixed(1)}bp, depth ±0.5% $${Math.round(Math.min(bid, ask) / 1000)}k` };
    },
    async odds() {
      // Only markets that move money: crypto, finance and the economy (not sports or culture).
      const tags = ['crypto', 'finance', 'economy'];
      const sets = await Promise.all(tags.map((tag) => Loki.getJSON(`https://gamma-api.polymarket.com/events?active=true&closed=false&limit=25&order=volume24hr&ascending=false&tag_slug=${tag}`).catch(() => [])));
      const rows = sets.flat().flatMap((e) => (e && Array.isArray(e.markets) ? e.markets : [])).filter((m) => !m.closed);
      const moves = rows.map((m) => ({ q: String(m.question || ''), move: Number(m.oneHourPriceChange) || 0 })).filter((m) => Number.isFinite(m.move));
      const big = moves.filter((m) => Math.abs(m.move) >= 0.08).sort((a, z) => Math.abs(z.move) - Math.abs(a.move));
      const crypto = moves.filter((m) => /bitcoin|btc|ethereum|crypto/i.test(m.q) && /above|up|reach|hit/i.test(m.q));
      const dir = crypto.length ? clamp(crypto.reduce((a, m) => a + m.move, 0) * 5, -1, 1) : 0;
      return { alarm: clamp(big.length / 4, 0, 1), direction: dir, confidence: 0.5, note: big.length ? `${big.length} markets moved 8+ pts in an hour, e.g. “${big[0].q.slice(0, 60)}” ${big[0].move > 0 ? '+' : ''}${Math.round(big[0].move * 100)}` : 'no big odds moves in the last hour' };
    },
    async news() {
      const data = await Loki.getJSON('/api/news?limit=24');
      const items = (data.items || []).map((i) => ({ title: i.title || '', at: (Number(i.published_at) || 0) * 1000 }));
      const recent = items.filter((i) => Date.now() - i.at < 30 * 60000);
      const t = mean(items.slice(0, 15).map((i) => tone(i.title)));
      for (const i of recent) if (!state.newsSeen.has(i.title)) { state.newsSeen.add(i.title); confirmLead(i); }
      return { alarm: clamp((recent.length - 3) / 6 + Math.abs(t) * 0.5, 0, 1), direction: t, confidence: items.length ? 0.55 : 0, note: `${recent.length} headlines in 30 min, tone ${t >= 0 ? '+' : ''}${t.toFixed(2)}` };
    },
    async social() {
      let posts = [];
      try {
        const r = await Loki.getJSON('https://www.reddit.com/r/CryptoCurrency+wallstreetbets+stocks/new.json?limit=50', { timeout: 8000 });
        posts = (r.data?.children || []).map((c) => ({ title: c.data?.title || '', at: (c.data?.created_utc || 0) * 1000 }));
      } catch {
        const r = await Loki.getJSON('https://hn.algolia.com/api/v1/search_by_date?query=bitcoin%20OR%20stocks%20OR%20fed&tags=story&hitsPerPage=40', { timeout: 8000 });
        posts = (r.hits || []).map((h) => ({ title: h.title || '', at: (h.created_at_i || 0) * 1000 }));
      }
      if (!posts.length) throw new Error('no posts');
      const span = Math.max(1, (Math.max(...posts.map((p) => p.at)) - Math.min(...posts.map((p) => p.at))) / 60000);
      const perMin = posts.length / span, t = mean(posts.map((p) => tone(p.title)));
      return { alarm: clamp((perMin - 3) / 6 + Math.abs(t) * 0.6, 0, 1), direction: t, confidence: 0.35, note: `${perMin.toFixed(1)} posts/min, tone ${t >= 0 ? '+' : ''}${t.toFixed(2)}` };
    },
  };

  function confirmLead(item) {
    const ev = state.events.find((e) => !e.news_at && item.at >= e.at - 5 * 60000 && item.at - e.at < 3 * 3600000);
    if (!ev) return;
    ev.news_at = item.at; ev.lead_min = Math.round((item.at - ev.at) / 60000); ev.headline = item.title.slice(0, 120);
    Loki.store.set('loki_swarm_events_v1', state.events.slice(-40));
  }

  async function poll(agent) {
    if (!state.running || document.hidden) return;
    try {
      Object.assign(agent, await reads[agent.id](), { status: 'ok', last: Date.now() });
    } catch (error) {
      Object.assign(agent, { status: agent.id === 'social' ? 'unavailable' : 'error', alarm: 0, confidence: 0, note: agent.id === 'social' ? 'social feeds are blocked on this device or network' : `no read: ${String(error.message || error).slice(0, 80)}`, last: Date.now() });
    }
    round();
  }

  function round() {
    const live = agents.filter((a) => a.status === 'ok' && a.confidence > 0);
    const conf = live.reduce((s, a) => s + a.confidence, 0) || 1;
    const alarm = live.reduce((s, a) => s + a.confidence * a.alarm, 0) / conf;
    const alarmed = live.filter((a) => a.alarm >= 0.5);
    const families = new Set(alarmed.map((a) => a.family));
    const quorum = alarmed.length >= 3 || (alarmed.length >= 2 && families.size === 2);
    state.sentiment = clamp(live.reduce((s, a) => s + a.confidence * a.direction, 0) / conf, -1, 1);
    state.sizeMultiplier = clamp(1 - 0.6 * alarm - (quorum ? 0.25 : 0), 0.25, 1);
    state.quorum = alarmed.length;
    const entry = { t: Date.now(), alarmed: alarmed.map((a) => a.name), quorum, size: state.sizeMultiplier, bias: state.sentiment };
    const prev = state.rounds[state.rounds.length - 1];
    if (!prev || prev.quorum !== quorum || prev.alarmed.join() !== entry.alarmed.join()) state.rounds.push(entry);
    state.rounds = state.rounds.slice(-50);
    if (quorum && !state.event) {
      state.event = { at: Date.now(), pair: state.pair, agents: entry.alarmed, bias: state.sentiment };
      state.events.push({ ...state.event });
      Loki.store.set('loki_swarm_events_v1', state.events.slice(-40));
      Loki.emit('swarm:event', state.event);
    } else if (!quorum && state.event && Date.now() - state.event.at > 5 * 60000) {
      state.event = null;
    }
    Loki.emit('swarm:update', snapshot());
  }

  function snapshot() {
    return { running: state.running, pair: state.pair, agents: agents.map((a) => ({ ...a })), sizeMultiplier: state.sizeMultiplier, sentiment: state.sentiment, quorum: state.quorum, event: state.event, rounds: state.rounds.slice(-12).reverse(), events: state.events.slice(-10).reverse() };
  }

  function start(pair) {
    if (pair) state.pair = pair;
    if (state.running) return;
    state.running = true;
    for (const agent of agents) { poll(agent); timers.push(setInterval(() => poll(agent), agent.every)); }
  }
  function stop() { state.running = false; timers.forEach(clearInterval); timers = []; Loki.emit('swarm:update', snapshot()); }

  Loki.swarm = { start, stop, snapshot, tone, setPair(pair) { state.pair = pair; agents.forEach((a) => poll(a)); } };
})();
