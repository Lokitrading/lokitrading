/* PREDICTION MARKETS — live Polymarket odds, Crowd vs Machine, paper predictions.
 *
 * Every number comes from Loki's read-only relay (/api/polymarket/*), which
 * reads Polymarket's public data. Nothing here can place a real bet: paper
 * predictions use play credits kept in this browser only.
 */
'use strict';
(() => {
  const root = document.getElementById('predictions');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (p, d = 0) => (Number.isFinite(p) ? `${(p * 100).toFixed(d)}%` : '—');
  const money = (v) => (Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: v >= 100 ? 0 : 2 }) : '—');
  const compact = (v) => (!Number.isFinite(v) ? '—' : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(0)}K` : `$${v.toFixed(0)}`);
  const when = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: d.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined }); };
  const safeUrl = (u) => (/^https:\/\/polymarket\.com\/event\/[a-z0-9-]+$/.test(String(u || '')) ? u : null);
  const CATS = [['finance', 'Finance'], ['economy', 'Economy & Fed'], ['crypto', 'Crypto'], ['stocks', 'Stocks'], ['politics', 'Politics'], ['trending', 'Trending']];
  const NAMES = { 'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum', 'SOL-USD': 'Solana', 'XRP-USD': 'XRP', SPY: 'S&P 500', NVDA: 'Nvidia', AAPL: 'Apple', MSFT: 'Microsoft', AMZN: 'Amazon', GOOGL: 'Alphabet', TSLA: 'Tesla', NFLX: 'Netflix', PLTR: 'Palantir', META: 'Meta' };
  const state = { cat: 'finance', query: '', events: [], loadedOnce: false };

  /* ------------------------------------------------------- paper book */
  const PAPER_KEY = 'loki_poly_paper_v1';
  const START_CREDITS = 1000;
  function loadBook() {
    try {
      const raw = JSON.parse(localStorage.getItem(PAPER_KEY) || 'null');
      if (raw && Number.isFinite(raw.cash) && Array.isArray(raw.positions)) return raw;
    } catch { /* corrupt or blocked */ }
    return { cash: START_CREDITS, positions: [], settled: [] };
  }
  let book = loadBook();
  const saveBook = () => { try { localStorage.setItem(PAPER_KEY, JSON.stringify(book)); } catch { /* storage full or blocked */ } };

  /* ------------------------------------------------ crowd vs machine */
  function curveSvg(curve, price) {
    if (!Array.isArray(curve) || curve.length < 2 || !Number.isFinite(price)) return '';
    const W = 220, H = 70, pad = 4;
    const lo = Math.min(curve[0].level, price), hi = Math.max(curve[curve.length - 1].level, price);
    const x = (v) => pad + (v - lo) / ((hi - lo) || 1) * (W - pad * 2);
    const y = (p) => pad + (1 - p) * (H - pad * 2);
    const d = curve.map((c, i) => `${i ? 'L' : 'M'}${x(c.level).toFixed(1)},${y(c.p_above).toFixed(1)}`).join(' ');
    const area = `${d} L${x(curve[curve.length - 1].level).toFixed(1)},${H - pad} L${x(curve[0].level).toFixed(1)},${H - pad} Z`;
    return `<svg class="cvm-curve" viewBox="0 0 ${W} ${H}" role="img" aria-label="Crowd chance of finishing above each price level">` +
      `<path class="cvm-area" d="${area}"/><path class="cvm-line" d="${d}"/>` +
      `<line class="cvm-half" x1="${pad}" x2="${W - pad}" y1="${y(0.5)}" y2="${y(0.5)}"/>` +
      `<line class="cvm-now" x1="${x(price).toFixed(1)}" x2="${x(price).toFixed(1)}" y1="${pad}" y2="${H - pad}"/></svg>`;
  }
  const LEAN = { bullish: ['Bullish', 'up'], bearish: ['Bearish', 'down'], neutral: ['Coin flip', 'flat'], unknown: ['No read', 'flat'] };
  const VERDICT = { agree: ['They agree', 'agree'], disagree: ['They disagree', 'disagree'], mixed: ['Mixed signals', 'mixed'] };
  function renderCvm(items) {
    const grid = $('cvm-grid');
    if (!items.length) {
      grid.innerHTML = '<p class="muted">No Polymarket price ladders are open right now — check back soon.</p>';
      $('cvm-status').textContent = 'No ladders open';
      return;
    }
    const disagree = items.filter((i) => i.verdict === 'disagree').length;
    $('cvm-status').textContent = disagree ? `${disagree} disagreement${disagree > 1 ? 's' : ''} right now` : `${items.length} markets compared`;
    grid.innerHTML = items.map((i) => {
      const name = NAMES[i.symbol] || i.symbol;
      const [crowdLabel, crowdCls] = LEAN[i.crowd_lean] || LEAN.unknown;
      const [aiLabel, aiCls] = LEAN[i.machine_lean] || LEAN.unknown;
      const [verdict, vCls] = VERDICT[i.verdict] || VERDICT.mixed;
      const days = Number(i.horizon_days);
      const horizon = days < 1 ? 'within a day' : `in ${Math.round(days)} day${Math.round(days) === 1 ? '' : 's'}`;
      const median = Number.isFinite(i.crowd_median) ? `${money(i.crowd_median)} (${i.crowd_median_change_pct >= 0 ? '+' : ''}${i.crowd_median_change_pct}%)` : 'outside the ladder';
      const link = safeUrl(i.url);
      return `<article class="cvm-card ${vCls}">` +
        `<header><div><strong>${esc(name)}</strong><span class="muted"> · ${esc(money(i.price))}</span></div><span class="cvm-verdict ${vCls}">${esc(verdict)}</span></header>` +
        `<div class="cvm-sides">` +
        `<div class="cvm-side"><span class="eyebrow">CROWD (POLYMARKET)</span><b class="${crowdCls}">${esc(crowdLabel)}</b>` +
        `<span>${esc(pct(i.crowd_prob_up))} chance it finishes above today's price ${esc(horizon)}</span><span class="muted">Expected: ${esc(median)}</span></div>` +
        `<div class="cvm-side"><span class="eyebrow">MACHINE (LOKI AI)</span><b class="${aiCls}">${esc(aiLabel)}</b>` +
        `<span>${esc(i.machine_text)}</span><span class="muted">Trend score ${esc(Number.isFinite(i.machine_score) ? Math.round(i.machine_score) : '—')}</span></div>` +
        `</div>${curveSvg(i.curve, i.price)}` +
        `<p class="caption">${esc(i.question)}${link ? ` · <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">See on Polymarket ↗</a>` : ''}</p>` +
        `</article>`;
    }).join('');
  }
  async function loadCvm() {
    try {
      const res = await fetch('/api/polymarket/crowd-vs-machine', { cache: 'no-store', signal: AbortSignal.timeout(45000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Crowd vs Machine is unavailable right now.');
      renderCvm(data.items || []);
    } catch (error) {
      $('cvm-status').textContent = 'Unavailable';
      $('cvm-grid').innerHTML = `<p class="form-message">${esc(error.message)}</p>`;
    }
  }

  /* -------------------------------------------------------- explorer */
  function renderTabs() {
    $('pm-tabs').innerHTML = CATS.map(([key, label]) =>
      `<button type="button" role="tab" class="lm-tab${!state.query && state.cat === key ? ' active' : ''}" aria-selected="${!state.query && state.cat === key}" data-pm-cat="${key}">${esc(label)}</button>`).join('');
  }
  function renderEvents() {
    const grid = $('pm-grid');
    if (!state.events.length) {
      grid.innerHTML = `<p class="muted">${state.query ? `No open markets match "${esc(state.query)}".` : 'No open markets in this category right now.'}</p>`;
      return;
    }
    grid.innerHTML = state.events.map((e) => {
      const link = safeUrl(e.url);
      const rows = e.markets.map((m) => {
        const change = Number.isFinite(m.change_1d) && Math.abs(m.change_1d) >= 0.005
          ? `<em class="${m.change_1d >= 0 ? 'up' : 'down'}">${m.change_1d >= 0 ? '▲' : '▼'}${Math.abs(m.change_1d * 100).toFixed(1)}</em>` : '';
        return `<div class="pm-outcome">` +
          `<span class="pm-label" title="${esc(m.question)}">${esc(m.label || 'Yes')}</span>` +
          `<span class="pm-bar"><i style="width:${Math.max(1, Math.min(100, m.yes * 100)).toFixed(1)}%"></i></span>` +
          `<span class="pm-prob">${esc(pct(m.yes, m.yes < 0.1 || m.yes > 0.9 ? 1 : 0))}${change}</span>` +
          `<span class="pm-bet"><button type="button" class="pm-yes" data-bet="yes" data-market="${esc(m.id)}">Yes</button><button type="button" class="pm-no" data-bet="no" data-market="${esc(m.id)}">No</button></span>` +
          `</div>`;
      }).join('');
      return `<article class="pm-card"><header><h3>${esc(e.title)}</h3>` +
        `<span class="muted">${e.end ? `Resolves ${esc(when(e.end))} · ` : ''}${esc(compact(e.volume_24h))} traded today</span></header>` +
        `${rows}${e.more ? `<p class="caption">+${e.more} less likely outcomes</p>` : ''}` +
        `${link ? `<a class="text-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open on Polymarket ↗</a>` : ''}</article>`;
    }).join('');
  }
  async function loadEvents() {
    const msg = $('pm-message');
    msg.textContent = 'Loading live odds…';
    renderTabs();
    try {
      const qs = state.query ? `q=${encodeURIComponent(state.query)}` : `category=${state.cat}`;
      const res = await fetch(`/api/polymarket/events?${qs}&limit=18`, { cache: 'no-store', signal: AbortSignal.timeout(25000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Polymarket odds are unavailable right now.');
      state.events = data.events || [];
      msg.textContent = state.query ? `Results for "${state.query}" — clear the search to browse categories.` : '';
      renderEvents();
    } catch (error) {
      msg.textContent = error.message;
      $('pm-grid').innerHTML = '';
    }
  }

  /* ---------------------------------------------------- paper betting */
  function findMarket(id) {
    for (const e of state.events) for (const m of e.markets) if (m.id === id) return { e, m };
    return null;
  }
  function openTicket(button) {
    const found = findMarket(button.dataset.market);
    if (!found) return;
    root.querySelectorAll('.pm-ticket').forEach((t) => t.remove());
    const side = button.dataset.bet;
    const price = side === 'yes' ? found.m.yes : 1 - found.m.yes;
    if (!(price > 0.005 && price < 0.995)) { $('pm-message').textContent = 'That outcome is already decided in the market — pick a live one.'; return; }
    const ticket = document.createElement('form');
    ticket.className = 'pm-ticket';
    ticket.innerHTML = `<span>Paper <b>${side.toUpperCase()}</b> on “${esc(found.m.label)}” at ${esc(pct(price, 1))} — pays 1 credit per share if right.</span>` +
      `<label>Stake<input type="number" min="1" max="${Math.floor(book.cash)}" step="1" value="${Math.min(50, Math.floor(book.cash))}" aria-label="Paper stake in credits"></label>` +
      `<button class="button primary" type="submit">Place paper prediction</button><button class="button secondary" type="button" data-ticket-cancel>Cancel</button>`;
    button.closest('.pm-outcome').after(ticket);
    ticket.querySelector('input').focus();
    ticket.addEventListener('submit', (event) => {
      event.preventDefault();
      const stake = Math.floor(Number(ticket.querySelector('input').value));
      if (!(stake >= 1) || stake > book.cash) { $('pm-message').textContent = `Stake between 1 and ${Math.floor(book.cash)} credits.`; return; }
      book.cash -= stake;
      book.positions.push({ market_id: found.m.id, label: found.m.label, event: found.e.title, side, stake, entry: price, shares: stake / price, current: price, at: new Date().toISOString() });
      saveBook();
      ticket.remove();
      $('pm-message').textContent = `Paper prediction placed: ${side.toUpperCase()} on “${found.m.label}” for ${stake} credits.`;
      renderBook();
    });
    ticket.querySelector('[data-ticket-cancel]').addEventListener('click', () => ticket.remove());
  }
  function renderBook() {
    const value = book.positions.reduce((s, p) => s + p.shares * p.current, 0);
    const staked = book.positions.reduce((s, p) => s + p.stake, 0);
    const total = book.cash + value;
    const pnl = total - START_CREDITS;
    $('pm-paper-summary').innerHTML =
      `<span><b>${esc(money(total))}</b> credits total</span><span>Cash ${esc(money(book.cash))}</span><span>Open ${esc(money(value))} (staked ${esc(money(staked))})</span>` +
      `<span class="${pnl >= 0 ? 'up' : 'down'}">${pnl >= 0 ? '+' : ''}${esc(money(pnl))} since start</span>`;
    const list = $('pm-paper-list');
    if (!book.positions.length && !(book.settled || []).length) {
      list.innerHTML = '<p class="muted">Pick Yes or No on any outcome above to place a paper prediction with play money (you start with 1,000 credits).</p>';
      return;
    }
    const open = book.positions.map((p, i) => {
      const gain = p.shares * p.current - p.stake;
      return `<div class="pm-pos"><span><b>${esc(p.side.toUpperCase())}</b> · ${esc(p.label)} <span class="muted">— ${esc(p.event)}</span></span>` +
        `<span>${esc(pct(p.entry, 1))} → ${esc(pct(p.current, 1))}</span><span class="${gain >= 0 ? 'up' : 'down'}">${gain >= 0 ? '+' : ''}${esc(money(gain))}</span>` +
        `<button type="button" class="button secondary" data-sell="${i}">Sell</button></div>`;
    }).join('');
    const done = (book.settled || []).slice(-8).reverse().map((p) =>
      `<div class="pm-pos settled"><span><b>${esc(p.side.toUpperCase())}</b> · ${esc(p.label)}</span><span>${esc(p.result)}</span>` +
      `<span class="${p.payout - p.stake >= 0 ? 'up' : 'down'}">${p.payout - p.stake >= 0 ? '+' : ''}${esc(money(p.payout - p.stake))}</span><span></span></div>`).join('');
    list.innerHTML = (open || '<p class="muted">No open paper predictions.</p>') + (done ? `<p class="eyebrow">SETTLED</p>${done}` : '');
  }
  async function refreshBook() {
    const ids = [...new Set(book.positions.map((p) => p.market_id))].filter((id) => /^\d+$/.test(id)).slice(0, 25);
    if (!ids.length) { renderBook(); return; }
    try {
      const res = await fetch(`/api/polymarket/markets?ids=${ids.join(',')}`, { cache: 'no-store', signal: AbortSignal.timeout(20000) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not refresh paper prices.');
      const byId = new Map((data.markets || []).map((m) => [m.id, m]));
      const still = [];
      for (const p of book.positions) {
        const m = byId.get(p.market_id);
        if (m && Number.isFinite(m.yes)) p.current = p.side === 'yes' ? m.yes : 1 - m.yes;
        if (m && m.closed && (p.current >= 0.99 || p.current <= 0.01)) {
          const payout = p.current >= 0.99 ? p.shares : 0;
          book.cash += payout;
          (book.settled = book.settled || []).push({ ...p, payout, result: payout ? 'Won' : 'Lost' });
        } else {
          still.push(p);
        }
      }
      book.positions = still;
      saveBook();
    } catch (error) {
      $('pm-message').textContent = error.message;
    }
    renderBook();
  }

  /* ------------------------------------------------------------ wiring */
  root.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-pm-cat]');
    if (tab) { state.cat = tab.dataset.pmCat; state.query = ''; $('pm-search-input').value = ''; loadEvents(); return; }
    const bet = event.target.closest('[data-bet]');
    if (bet) { openTicket(bet); return; }
    const sell = event.target.closest('[data-sell]');
    if (sell) {
      const p = book.positions[Number(sell.dataset.sell)];
      if (!p) return;
      book.cash += p.shares * p.current;
      (book.settled = book.settled || []).push({ ...p, payout: p.shares * p.current, result: `Sold at ${pct(p.current, 1)}` });
      book.positions.splice(Number(sell.dataset.sell), 1);
      saveBook();
      renderBook();
    }
  });
  $('pm-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.query = $('pm-search-input').value.trim().slice(0, 80);
    loadEvents();
  });
  $('pm-paper-refresh').addEventListener('click', refreshBook);
  $('pm-paper-reset').addEventListener('click', () => {
    if (!window.confirm('Reset your paper predictions to 1,000 credits?')) return;
    book = { cash: START_CREDITS, positions: [], settled: [] };
    saveBook();
    renderBook();
  });

  const visible = () => !root.hidden;
  function onView() {
    if (!visible()) return;
    if (!state.loadedOnce) {
      state.loadedOnce = true;
      loadCvm();
      loadEvents();
      refreshBook();
    }
  }
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  renderBook();
  onView();
})();
