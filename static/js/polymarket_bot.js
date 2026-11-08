/* LOKI POLYMARKET PAPER BOT — exits first, then entries, every minute.
 *
 * The server (/api/polymarket/bot/*) does the pricing: model fair value vs the
 * live CLOB book. This file keeps the paper account in this browser only,
 * applies the fills the server priced, and shows each order in Polymarket's
 * CLOB shape. Nothing here can sign or send a real order.
 */
'use strict';
(() => {
  const root = document.getElementById('pm-bot');
  const view = document.getElementById('predictions');
  if (!root || !view) return;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const usd = (v, signed = false) => (Number.isFinite(v) ? `${signed && v > 0 ? '+' : ''}${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—');
  const cents = (p) => (Number.isFinite(p) ? `${(p * 100).toFixed(1)}¢` : '—');
  const pct = (p) => (Number.isFinite(p) ? `${(p * 100).toFixed(1)}%` : '—');
  const day = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); };
  const safeUrl = (u) => (/^https:\/\/polymarket\.com\/event\/[a-z0-9-]+$/.test(String(u || '')) ? u : null);
  const NAMES = { 'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum', 'SOL-USD': 'Solana', 'XRP-USD': 'XRP', SPY: 'S&P 500', NVDA: 'Nvidia', AAPL: 'Apple', MSFT: 'Microsoft', AMZN: 'Amazon', GOOGL: 'Alphabet', TSLA: 'Tesla', NFLX: 'Netflix', PLTR: 'Palantir', META: 'Meta' };
  const KEY = 'loki_poly_bot_v1';
  const START = 1000;
  const MAX_OPEN = 20;
  const TICK_MS = 60000;
  const COOLDOWN_MS = 6 * 3600e3; // no re-entering a market the bot just left: that is churn, not edge

  const fresh = () => ({ start: START, cash: START, positions: [], closed: [], log: [], auto: false, rules: null, scan: null, lastRun: null });
  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (raw && Number.isFinite(raw.cash) && Array.isArray(raw.positions) && Array.isArray(raw.closed)) return { ...fresh(), ...raw };
    } catch { /* corrupt or blocked storage */ }
    return fresh();
  }
  let bot = load();
  const save = () => {
    bot.log = bot.log.slice(-60);
    bot.closed = bot.closed.slice(-100);
    try { localStorage.setItem(KEY, JSON.stringify(bot)); } catch { /* storage full or blocked */ }
  };
  const log = (text, kind = '') => bot.log.push({ t: Date.now(), text, kind });
  const newId = () => `b${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
  const mark = (p) => (Number.isFinite(p.mark) ? p.mark : p.entry);
  const openValue = () => bot.positions.reduce((s, p) => s + (p.hedged ? p.shares : p.shares * mark(p)), 0);
  const exposure = () => bot.positions.reduce((s, p) => s + p.cost + (p.hedged ? p.hedged.cost : 0), 0);
  const equity = () => bot.cash + openValue();
  const describe = (p) => `${p.token} · ${NAMES[p.symbol] || p.symbol} ${p.token === 'YES' ? 'above' : 'not above'} ${Number(p.strike).toLocaleString('en-US')}`;

  async function api(path, options = {}) {
    const res = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(50000), ...options });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(res.status === 429 ? 'Too many bot runs in a minute — waiting for the next one.' : (typeof data.detail === 'string' ? data.detail : 'The bot service is unavailable right now.'));
    return data;
  }

  /* ---------------------------------------------------------- one cycle */
  function close(p, price, reason) {
    const proceeds = p.shares * price;
    bot.cash += proceeds;
    const pnl = proceeds - p.cost;
    bot.closed.push({ ...p, exit: price, pnl, reason, closedAt: new Date().toISOString() });
    log(`Sold ${describe(p)} at ${cents(price)} → ${usd(pnl, true)} (${reason})`, pnl >= 0 ? 'up' : 'down');
    window.Loki?.emit('order', { venue: 'polymarket', side: 'sell', symbol: describe(p), qty: p.shares, paper: true });
    window.Loki?.audit?.append({ kind: 'paper', venue: 'polymarket-bot', action: 'sell', market: p.market_id, token: p.token, shares: p.shares, price, pnl, reason });
  }

  async function settleExpired() {
    const now = Date.now();
    bot.positions = bot.positions.filter((p) => {
      if (!p.hedged || new Date(p.resolves).getTime() + 5 * 60000 > now) return true;
      const payout = p.shares, cost = p.cost + p.hedged.cost, pnl = payout - cost;
      bot.cash += payout;
      bot.closed.push({ ...p, exit: 1, pnl, reason: 'hedged pair settled at $1 a pair', closedAt: new Date().toISOString() });
      log(`Settled hedged ${describe(p)}: pair paid ${usd(payout)} → ${usd(pnl, true)}`, pnl >= 0 ? 'up' : 'down');
      window.Loki?.audit?.append({ kind: 'paper', venue: 'polymarket-bot', action: 'settle', market: p.market_id, pnl, hedged: true });
      return false;
    });
    const expired = bot.positions.filter((p) => new Date(p.resolves).getTime() + 5 * 60000 < now && /^\d+$/.test(p.market_id || ''));
    if (!expired.length) return;
    const ids = [...new Set(expired.map((p) => p.market_id))].slice(0, 25);
    const data = await api(`/api/polymarket/markets?ids=${ids.join(',')}`);
    const byId = new Map((data.markets || []).map((m) => [m.id, m]));
    bot.positions = bot.positions.filter((p) => {
      const m = byId.get(p.market_id);
      if (!expired.includes(p) || !m || !m.closed || !Number.isFinite(m.yes)) return true;
      const yes = m.yes >= 0.99 ? 1 : m.yes <= 0.01 ? 0 : null;
      if (yes === null) return true; // not settled yet
      close(p, p.token === 'YES' ? yes : 1 - yes, 'market resolved');
      return false;
    });
  }

  async function reviewExits() {
    const open = bot.positions.filter((p) => !p.hedged).slice(0, 20);
    if (!open.length) return;
    const rule = window.Loki && window.Loki.config ? window.Loki.config().hedge_rule : null;
    const body = { positions: open.map((p) => ({ id: p.id, token_id: p.token_id, token: p.token, symbol: p.symbol, strike: p.strike, entry: p.entry, shares: p.shares, resolves: p.resolves, ...(p.hedge_token_id ? { hedge_token_id: p.hedge_token_id } : {}) })) };
    if (rule && rule.enabled) body.hedge_above_vol = rule.vol_threshold_pct;
    const data = await api('/api/polymarket/bot/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (data.rules) bot.rules = data.rules;
    const byId = new Map((data.decisions || []).map((d) => [d.id, d]));
    bot.positions = bot.positions.filter((p) => {
      const d = byId.get(p.id);
      if (!d) return true;
      if (Number.isFinite(d.bid)) p.mark = d.bid;
      p.model = Number.isFinite(d.model_prob) ? d.model_prob : p.model;
      p.status = d.reason;
      if (d.action === 'hedge' && d.hedge && d.order) {
        const cost = d.hedge.avg * p.shares;
        if (cost <= bot.cash) {
          bot.cash -= cost;
          p.hedged = { price: d.hedge.avg, cost, at: new Date().toISOString() };
          p.mark = 1 - d.hedge.avg; // the pair is now worth $1 at resolution: value the YES leg at what is locked
          p.status = `${d.reason}. Locked until resolution.`;
          log(`Hedged ${describe(p)}: bought the opposite token at ${cents(d.hedge.avg)} instead of selling into a thin book`, 'buy');
          window.Loki?.emit('order', { venue: 'polymarket', side: 'buy', symbol: `hedge ${describe(p)}`, qty: p.shares, paper: true });
          window.Loki?.audit?.append({ kind: 'paper', venue: 'polymarket-bot', action: 'hedge', market: p.market_id, shares: p.shares, price: d.hedge.avg, reason: d.reason });
          return true;
        }
      }
      if (d.action === 'sell' && Number.isFinite(d.bid) && d.order) { close(p, d.bid, d.reason); return false; }
      if (d.action === 'sell') p.status = `${d.reason} — but the book has no bids for the full size, so it is held`;
      return true;
    });
  }

  async function enter() {
    const qs = new URLSearchParams({ bankroll: Math.max(1, equity()).toFixed(2), exposure: exposure().toFixed(2) });
    const data = await api(`/api/polymarket/bot/signals?${qs}`);
    bot.rules = data.rules || bot.rules;
    bot.scan = { at: Date.now(), ladders: data.ladders_scanned, books: data.books_checked, candidates: data.candidates, signals: data.signals || [] };
    let bought = 0;
    for (const s of data.signals || []) {
      if (bot.positions.length >= MAX_OPEN) break;
      if (bot.positions.some((p) => p.token_id === s.token_id || p.market_id === s.market_id)) continue;
      if (bot.closed.some((c) => c.market_id === s.market_id && Date.now() - new Date(c.closedAt).getTime() < COOLDOWN_MS)) continue;
      if (!(s.cost > 0) || s.cost > bot.cash) continue;
      bot.cash -= s.cost;
      window.Loki?.emit('order', { venue: 'polymarket', side: 'buy', symbol: describe(s), qty: s.shares, paper: true });
      window.Loki?.audit?.append({ kind: 'paper', venue: 'polymarket-bot', action: 'buy', market: s.market_id, token: s.token, shares: s.shares, price: s.avg_fill, edge: s.edge });
      bot.positions.push({ id: newId(), token_id: s.token_id, hedge_token_id: s.hedge_token_id, token: s.token, symbol: s.symbol, strike: s.strike, market_id: s.market_id, question: s.question, url: s.url,
        resolves: s.resolves, entry: s.avg_fill, shares: s.shares, cost: s.cost, mark: s.avg_fill, model: s.model_prob, edge: s.edge, opened: new Date().toISOString(), status: 'just opened' });
      log(`Bought ${describe(s)} — ${s.shares} shares at ${cents(s.avg_fill)} (model ${pct(s.model_prob)}, edge ${(s.edge * 100).toFixed(1)} pts)`, 'buy');
      bought += 1;
    }
    if (!bought) log(data.signals?.length ? 'Signals found, but already held, just exited (6-hour cooldown) or not enough paper cash.' : `No trade: nothing beat the book by enough after spread and fees (${data.ladders_scanned || 0} ladders, ${data.books_checked || 0} books checked).`);
  }

  let busy = false;
  async function cycle(manual = false) {
    if (busy) return;
    busy = true;
    $('pmb-scan').disabled = true;
    setStatus('Checking exits, then scanning…');
    try {
      await settleExpired();
      await reviewExits(); // exits are always handled before any new entry
      await enter();
      bot.lastRun = Date.now();
    } catch (error) {
      log(`Paused this cycle: ${error.message}`, 'warn');
      if (manual) setStatus(error.message);
    } finally {
      busy = false;
      $('pmb-scan').disabled = false;
      save();
      render();
    }
  }

  /* -------------------------------------------------------------- render */
  function setStatus(text) {
    const el = root.querySelector('.pmb-status');
    if (el) el.textContent = text;
  }
  function render() {
    const value = openValue();
    const eq = bot.cash + value;
    const unreal = value - exposure();
    const realised = bot.closed.reduce((s, c) => s + c.pnl, 0);
    const wins = bot.closed.filter((c) => c.pnl > 0).length;
    const total = eq - bot.start;
    const auto = bot.auto ? `Autopilot on · every minute while this page is open` : 'Autopilot off';
    $('pmb-summary').innerHTML =
      `<div><span>Paper equity</span><b>${esc(usd(eq))}</b><em class="${total >= 0 ? 'up' : 'down'}">${esc(usd(total, true))} since start</em></div>` +
      `<div><span>Cash</span><b>${esc(usd(bot.cash))}</b><em>${bot.positions.length} open</em></div>` +
      `<div><span>Unrealised (at the bid)</span><b class="${unreal >= 0 ? 'up' : 'down'}">${esc(usd(unreal, true))}</b><em>${esc(usd(exposure()))} at risk</em></div>` +
      `<div><span>Realised</span><b class="${realised >= 0 ? 'up' : 'down'}">${esc(usd(realised, true))}</b><em>${bot.closed.length} closed · ${bot.closed.length ? Math.round(100 * wins / bot.closed.length) : 0}% won</em></div>` +
      `<p class="pmb-status" role="status">${esc(auto)}${bot.lastRun ? ` · last run ${esc(new Date(bot.lastRun).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}` : ''}</p>`;
    const r = bot.rules;
    $('pmb-rules').innerHTML = r ? [
      `Edge ≥ ${(r.min_edge * 100).toFixed(0)} pts after fees`, `${r.kelly_fraction * 4 === 1 ? 'Quarter' : r.kelly_fraction}-Kelly`, `≤ ${(r.max_position_fraction * 100).toFixed(0)}% per trade`,
      `≤ ${(r.max_total_fraction * 100).toFixed(0)}% invested`, `Exit ${r.lock_buffer_minutes} min before lock`, `Stop −${(r.stop_loss * 100).toFixed(0)}%`,
      `Take ${(r.take_profit_share * 100).toFixed(0)}% of upside`, `Crowd weight ${((1 - r.model_weight) * 100).toFixed(0)}%`,
    ].map((t) => `<span>${esc(t)}</span>`).join('') : '';
    $('pmb-auto').textContent = bot.auto ? 'Pause autopilot ❚❚' : 'Start autopilot ▶';
    $('pmb-auto').setAttribute('aria-pressed', String(bot.auto));
    root.classList.toggle('is-running', bot.auto);

    $('pmb-positions').innerHTML = bot.positions.length ? bot.positions.map((p) => {
      const pnl = p.hedged ? p.shares - p.cost - p.hedged.cost : p.shares * mark(p) - p.cost;
      const link = safeUrl(p.url);
      return `<div class="pmb-pos"><div><b>${esc(describe(p))}</b><span class="muted"> · resolves ${esc(day(p.resolves))}</span>` +
        `<p class="caption">${esc(p.status || '')}${link ? ` · <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Market ↗</a>` : ''}</p></div>` +
        `<span>${esc(cents(p.entry))} → ${esc(cents(mark(p)))}<br><span class="muted">${esc(p.shares)} sh · model ${esc(pct(p.model))}</span></span>` +
        `<span class="${pnl >= 0 ? 'up' : 'down'}">${esc(usd(pnl, true))}</span></div>`;
    }).join('') : '<p class="muted">No open positions. The bot only buys when the order book is clearly mispriced — most minutes it sits out.</p>';

    const scan = bot.scan;
    if (!scan) $('pmb-signals').innerHTML = '<p class="muted">Nothing scanned yet.</p>';
    else if (!scan.signals.length) $('pmb-signals').innerHTML = `<p class="muted">No trade at ${esc(new Date(scan.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}: ${esc(scan.ladders || 0)} ladders and ${esc(scan.books || 0)} order books checked, nothing cleared the edge bar after spread and fees. Sitting out is a position.</p>`;
    else $('pmb-signals').innerHTML = scan.signals.map((s) => `<div class="pmb-signal"><div><b>${esc(describe(s))}</b><span class="muted"> · ${esc(s.question)}</span></div>` +
      `<p class="caption">Spot ${esc(Number(s.spot).toLocaleString('en-US'))} · vol ${esc(pct(s.sigma))} · fair ${esc(pct(s.model_prob))} vs crowd ${esc(pct(s.market_prob))} · fill ${esc(cents(s.avg_fill))} · fee ${esc(cents(s.fee_per_share))} · <b>edge ${esc((s.edge * 100).toFixed(1))} pts</b> · cost ${esc(usd(s.cost))}</p>` +
      `<details><summary>CLOB order (never sent)</summary><pre>${esc(JSON.stringify(s.order, null, 2))}</pre></details></div>`).join('');

    $('pmb-log').innerHTML = bot.log.length ? bot.log.slice(-40).reverse().map((l) =>
      `<li class="${esc(l.kind)}"><time>${esc(new Date(l.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}</time> ${esc(l.text)}</li>`).join('') : '<li>No activity yet.</li>';
  }

  /* -------------------------------------------------------------- wiring */
  let timer = null;
  const showing = () => !view.hidden && !document.hidden;
  function schedule() {
    clearInterval(timer);
    timer = null;
    if (bot.auto && showing()) timer = setInterval(() => { if (showing()) cycle(); }, TICK_MS);
  }
  $('pmb-auto').addEventListener('click', () => {
    bot.auto = !bot.auto;
    log(bot.auto ? 'Autopilot started.' : 'Autopilot paused.');
    save();
    render();
    schedule();
    if (bot.auto) cycle(true);
  });
  $('pmb-scan').addEventListener('click', () => cycle(true));
  $('pmb-reset').addEventListener('click', () => {
    if (!window.confirm('Reset the paper bot to $1,000 and clear its history?')) return;
    bot = fresh();
    save();
    render();
    schedule();
  });
  const onView = () => {
    schedule();
    if (bot.auto && showing() && (!bot.lastRun || Date.now() - bot.lastRun > TICK_MS)) cycle();
  };
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  document.addEventListener('visibilitychange', onView);
  render();
  onView();
})();
