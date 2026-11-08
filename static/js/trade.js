/* TRADE TERMINAL — real orders on Alpaca (paper or live) and Tradovate (demo or
 * live), with the on-device council, Monte Carlo, swarm and risk matrix in the
 * loop, and every execution written to the signed Proof-of-Strategy log.
 *
 * Keys: Alpaca keys go from this browser straight to Alpaca (Alpaca allows
 * it), so Loki's server never sees them. Paper keys stay in this browser;
 * LIVE keys are kept only for this tab (sessionStorage) and vanish when it
 * closes. Tradovate needs Loki's relay, which stores nothing.
 * Guardrails: live trading must be armed by typing the phrase each session,
 * every live order is confirmed with its full details, the notional cap and
 * the daily live-order cap apply, the risk breaker blocks new live entries,
 * and the autopilot only ever trades paper/demo accounts.
 */
'use strict';
(() => {
  const root = document.getElementById('trade');
  const Loki = window.Loki;
  if (!root || !Loki) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt;
  const PHRASE = 'trade real money';
  const HARD_NOTIONAL_CAP = 25000;
  const LIVE_DAILY_CAP = 20;
  const FUTURES = { MES: 5, ES: 50, MNQ: 2, NQ: 20, M2K: 5, RTY: 50, MYM: 0.5, YM: 5, MCL: 100, CL: 1000, MGC: 10, GC: 100 };
  const VENUES = {
    'alpaca-paper': { broker: 'alpaca', live: false, base: 'https://paper-api.alpaca.markets', label: 'Alpaca paper' },
    'alpaca-live': { broker: 'alpaca', live: true, base: 'https://api.alpaca.markets', label: 'Alpaca LIVE' },
    'tradovate-demo': { broker: 'tradovate', live: false, env: 'demo', label: 'Tradovate demo' },
    'tradovate-live': { broker: 'tradovate', live: true, env: 'live', label: 'Tradovate LIVE' },
  };
  const state = { venue: Loki.store.get('loki_trade_venue_v1', 'alpaca-paper'), account: null, positions: [], orders: [], equityCurve: Loki.store.get('loki_trade_equity_v1', {}), check: null, autopilot: null };

  /* ------------------------------------------------------------ credentials */
  const credKey = (venue) => `loki_trade_cred_${venue}`;
  function getCred(venue = state.venue) {
    const v = VENUES[venue];
    if (v.broker === 'alpaca' && !v.live) return Loki.store.get('loki_alpaca_keys_v1', null) || Loki.store.get(credKey(venue), null);
    return Loki.store.session.get(credKey(venue), null);
  }
  function setCred(venue, cred) {
    const v = VENUES[venue];
    if (v.broker === 'alpaca' && !v.live) Loki.store.set('loki_alpaca_keys_v1', cred); else Loki.store.session.set(credKey(venue), cred);
  }
  function clearCred(venue) {
    const v = VENUES[venue];
    try { if (v.broker === 'alpaca' && !v.live) localStorage.removeItem('loki_alpaca_keys_v1'); } catch { /* blocked */ }
    Loki.store.session.del(credKey(venue));
  }
  const armed = () => { const a = Loki.store.session.get('loki_live_armed_v1', 0); return a && Date.now() - a < 2 * 3600e3; };

  /* ------------------------------------------------------------- broker API */
  async function alpaca(path, { method = 'GET', body, venue = state.venue } = {}) {
    const v = VENUES[venue], cred = getCred(venue);
    if (!cred) throw new Error(`Connect ${v.label} first.`);
    const res = await fetch(v.base + path, { method, cache: 'no-store', signal: AbortSignal.timeout(15000),
      headers: { 'APCA-API-KEY-ID': cred.key_id, 'APCA-API-SECRET-KEY': cred.secret_key, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(res.status === 401 || res.status === 403 ? `${v.label} rejected the keys (check you pasted ${v.live ? 'LIVE' : 'PAPER'} keys).` : (data && data.message) || `${v.label} answered HTTP ${res.status}`);
    return data;
  }
  async function tradovate(path, { method = 'GET', body, venue = state.venue, query } = {}) {
    const v = VENUES[venue], cred = getCred(venue);
    if (!cred || !cred.token) throw new Error(`Connect ${v.label} first.`);
    const qs = query ? `?${new URLSearchParams(query)}` : '';
    const res = await fetch(`/api/broker/tradovate/${v.env}/${path}${qs}`, { method, cache: 'no-store', signal: AbortSignal.timeout(20000),
      headers: { Authorization: `Bearer ${cred.token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && data.detail) || `Tradovate relay answered HTTP ${res.status}`);
    return data;
  }

  async function refresh(quiet = false) {
    const v = VENUES[state.venue];
    if (!getCred()) { state.account = null; state.positions = []; state.orders = []; render(); return; }
    if (!quiet) setMsg('trade-msg', 'Refreshing…');
    try {
      if (v.broker === 'alpaca') {
        const [acct, pos, ord] = await Promise.all([alpaca('/v2/account'), alpaca('/v2/positions'), alpaca('/v2/orders?status=all&limit=25&direction=desc')]);
        state.account = { equity: +acct.equity, cash: +acct.cash, buying_power: +acct.buying_power, day_pl: +acct.equity - +acct.last_equity, id: acct.account_number, blocked: acct.trading_blocked || acct.account_blocked, status: acct.status };
        state.positions = (pos || []).map((p) => ({ symbol: p.symbol, qty: +p.qty, avg: +p.avg_entry_price, price: +p.current_price, value: +p.market_value, pl: +p.unrealized_pl, side: p.side, asset_class: p.asset_class }));
        state.orders = (ord || []).map((o) => ({ id: o.id, symbol: o.symbol, side: o.side, qty: +o.qty, type: o.type, status: o.status, filled: +o.filled_qty, price: +(o.filled_avg_price || o.limit_price || 0), at: o.submitted_at || o.created_at }));
      } else {
        const snap = await tradovate('snapshot');
        const cash = (snap.balances || []).reduce((a, b) => a + (b.cash || 0), 0);
        state.account = { equity: cash, cash, buying_power: null, day_pl: (snap.balances || []).reduce((a, b) => a + (b.realized_pnl || 0), 0), id: (snap.accounts || []).map((a) => a.name).join(', '), accounts: snap.accounts || [] };
        state.positions = (snap.positions || []).map((p) => ({ symbol: p.contract || String(p.contract_id), contract_id: p.contract_id, account_id: p.account_id, qty: p.quantity, avg: p.net_price, price: null, value: null, pl: null }));
        state.orders = (snap.orders || []).map((o) => ({ id: o.id, symbol: String(o.contractId), side: String(o.action || '').toLowerCase(), qty: null, type: o.orderType, status: o.ordStatus, at: o.timestamp }));
      }
      const curve = (state.equityCurve[state.venue] ||= []);
      if (Number.isFinite(state.account.equity) && state.account.equity > 0) { curve.push({ t: Date.now(), v: state.account.equity }); state.equityCurve[state.venue] = curve.slice(-500); Loki.store.set('loki_trade_equity_v1', state.equityCurve); }
      if (!quiet) setMsg('trade-msg', `Updated ${F.time(Date.now())}.`);
    } catch (error) { setMsg('trade-msg', error.message, true); }
    render();
  }

  /* ------------------------------------------------------- pre-trade check */
  function riskNow() {
    return Loki.risk.evaluate({ curve: state.equityCurve[state.venue] || [], swarm: Loki.swarm ? Loki.swarm.snapshot() : null,
      positions: state.positions.map((p) => ({ id: p.symbol, symbol: p.symbol, kind: FUTURES[root_(p.symbol)] ? 'future' : /\/USD$|USD$/.test(p.symbol) ? 'crypto' : 'stock', qty: p.qty, value: p.value || 0, exit_slippage_bps: 5 })) });
  }
  const root_ = (sym) => String(sym || '').replace(/[FGHJKMNQUVXZ]\d{1,2}$/, '');
  function ticket() {
    const symbol = $('tk-symbol').value.trim().toUpperCase(), side = $('tk-side').value, qty = Number($('tk-qty').value), type = $('tk-type').value, limit = Number($('tk-limit').value) || null;
    return { symbol, side, qty, type, limit };
  }
  function marketSymbol(sym) {
    const v = VENUES[state.venue];
    if (v.broker === 'tradovate') return null;
    if (/^(BTC|ETH|SOL|XRP|DOGE|LTC|AVAX|LINK)(\/?USD)?$/.test(sym)) return sym.replace('/USD', '').replace(/USD$/, '') + '-USD';
    return sym;
  }

  async function preTradeCheck() {
    const t = ticket(), v = VENUES[state.venue], cfg = Loki.config();
    if (!t.symbol || !(t.qty > 0)) throw new Error('Enter a symbol and a quantity.');
    $('tk-check').disabled = true; $('tk-result').innerHTML = '<p class="muted">Asking the council, simulating 5,000 paths…</p>';
    try {
      const msym = marketSymbol(t.symbol);
      let council = null, bars = [], price = t.limit;
      if (msym) {
        bars = await Loki.data.bars(msym, { interval: '1d', limit: 300 });
        price = price || bars[bars.length - 1].c;
        const swarm = Loki.swarm ? Loki.swarm.snapshot() : null;
        council = Loki.council.analyze(msym, bars, { sentiment: swarm && swarm.running ? swarm.sentiment : undefined });
      }
      const mult = FUTURES[root_(t.symbol)] || 1;
      if (!price) throw new Error('Give a limit price for futures (Loki has no futures quote feed).');
      const notional = price * t.qty * mult;
      const kind = v.broker === 'tradovate' ? 'future' : msym && Loki.data.isCrypto(msym) ? 'crypto' : 'stock';
      let book = null;
      if (kind === 'crypto' && Loki.data.binancePair(msym)) { try { const d = await Loki.data.binanceDepth(Loki.data.binancePair(msym), 100); book = { asks: d.asks, bids: d.bids }; } catch { /* spread model instead */ } }
      const net = Loki.execmap ? Loki.execmap.stats()[v.broker === 'alpaca' ? (v.live ? 'alpaca-live' : 'alpaca-paper') : 'tradovate'] : null;
      const lat = net && net.n ? { median_ms: net.ms, p95_ms: net.ms * 3 } : { median_ms: 250, p95_ms: 1200 };
      const mc = await Loki.mc.run({ legs: [{ side: t.side, qty: t.qty, price, kind, book, fee_bps: kind === 'crypto' ? 25 : 0.5, multiplier: mult }],
        returns: bars.length ? Loki.math.logReturns(bars.map((b) => b.c)).slice(-500) : [], bar_seconds: 86400, horizon_bars: Loki.council.HORIZON,
        latency: lat, paths: 5000, seed: Date.now() % 100000 });
      const risk = riskNow();
      const equity = state.account ? state.account.equity : null;
      const maxNotional = Math.min(HARD_NOTIONAL_CAP, Number($('tk-cap').value) || 1000);
      const sizePct = equity ? notional / equity * 100 : null;
      const councilAgrees = !council || (t.side === 'buy' ? council.p_up >= 0.5 : council.p_up <= 0.5);
      const checks = [
        ['Connected', Boolean(state.account), state.account ? `${v.label} account ${state.account.id || ''}` : 'connect the account first'],
        ['Notional cap', notional <= maxNotional, `${F.usd(notional)} vs your cap ${F.usd(maxNotional)}`],
        ['Size vs plan', sizePct == null || sizePct <= cfg.max_position_pct * (risk.sizeMultiplier || 0.0001) * 1.0001 || t.side === 'sell', sizePct == null ? 'equity unknown' : `${sizePct.toFixed(1)}% of equity vs ${(cfg.max_position_pct * risk.sizeMultiplier).toFixed(1)}% allowed now`],
        ['Risk matrix', risk.level !== 'red' || t.side === 'sell', `${risk.level.toUpperCase()}: ${risk.reason}`],
        ['Council', councilAgrees, council ? `${Math.round(council.p_up * 100)}% up over ${council.horizon} bars: ${council.verdict}` : 'no council read for futures'],
        ['Simulated confidence', mc.confidence >= 50, `${mc.confidence}% of 5,000 paths profitable after costs`],
      ];
      if (v.live) checks.push(['Live trading armed', armed(), armed() ? 'armed for this session' : `type “${PHRASE}” in the arming box first`]);
      state.check = { t, council, mc, risk, checks, notional, price, kind, at: Date.now() };
      renderCheck();
      return state.check;
    } finally { $('tk-check').disabled = false; }
  }

  function renderCheck() {
    const c = state.check; if (!c) return;
    const pass = c.checks.every(([, ok]) => ok);
    const council = c.council;
    $('tk-result').innerHTML = `
      <div class="tk-score ${c.mc.confidence >= 55 ? 'good' : c.mc.confidence >= 45 ? 'meh' : 'bad'}"><b>${c.mc.confidence}</b><span>confidence<br>(${c.mc.paths.toLocaleString()} paths, ${c.mc.ms}ms on this device)</span></div>
      ${Loki.mc.histogramSvg(c.mc)}
      <p class="caption">Expected ${F.usd(c.mc.pnl.mean, true)} · 5% worst case ${F.usd(c.mc.pnl.p5, true)} · VaR95 ${F.usd(c.mc.var95)} · CVaR95 ${F.usd(c.mc.cvar95)} · slippage p50 ${c.mc.slippage_bps.p50.toFixed(1)}bp / p99 ${c.mc.slippage_bps.p99.toFixed(1)}bp · delay p99 ${Math.round(c.mc.delay_ms.p99)}ms${c.mc.book_exhausted_share > 0.01 ? ` · book ran out in ${(c.mc.book_exhausted_share * 100).toFixed(0)}% of paths` : ''}</p>
      ${council ? `<p class="caption">Council: ${council.votes.map((v) => `${v.icon} ${Math.round(v.p * 100)}%`).join(' · ')} → <b>${Math.round(council.p_up * 100)}% up</b>. ${F.esc(council.mood.text)}</p>` : ''}
      <ul class="tk-checks">${c.checks.map(([name, ok, why]) => `<li class="${ok ? 'ok' : 'no'}"><b>${ok ? '✓' : '✗'} ${F.esc(name)}</b> ${F.esc(why)}</li>`).join('')}</ul>
      <p class="${pass ? 'tk-go' : 'tk-stop'}">${pass ? 'All checks pass. You can place this order.' : 'Some checks fail. You can still place it on paper/demo; live orders need every check to pass.'}</p>`;
    $('tk-send').disabled = VENUES[state.venue].live && !pass;
  }

  /* ----------------------------------------------------------------- orders */
  async function place() {
    const v = VENUES[state.venue], t = ticket();
    if (!state.check || Date.now() - state.check.at > 120000 || state.check.t.symbol !== t.symbol || state.check.t.qty !== t.qty || state.check.t.side !== t.side) await preTradeCheck();
    const c = state.check;
    if (v.live) {
      if (!c.checks.every(([, ok]) => ok)) throw new Error('Live orders need every pre-trade check to pass.');
      const today = new Date().toDateString(), count = Loki.store.get('loki_live_orders_today_v1', { day: today, n: 0 });
      if (count.day === today && count.n >= LIVE_DAILY_CAP) throw new Error(`Daily cap of ${LIVE_DAILY_CAP} live orders reached.`);
      if (!window.confirm(`REAL MONEY ORDER on ${v.label}\n\n${t.side.toUpperCase()} ${t.qty} ${t.symbol} ${t.type}${t.limit ? ' @ ' + t.limit : ''}\nEstimated notional ${F.usd(c.notional)}\nSimulated confidence ${c.mc.confidence}%\n\nThis is not advice and can lose money. Send it?`)) return;
      Loki.store.set('loki_live_orders_today_v1', { day: today, n: (count.day === today ? count.n : 0) + 1 });
    }
    let result;
    if (v.broker === 'alpaca') {
      const crypto = c.kind === 'crypto';
      const symbol = crypto ? t.symbol.replace(/-?USDT?$/, '').replace('/USD', '') + '/USD' : t.symbol;
      const body = { symbol, qty: String(t.qty), side: t.side, type: t.type, time_in_force: crypto ? 'gtc' : 'day' };
      if (t.type === 'limit') body.limit_price = String(t.limit);
      Loki.emit('order', { venue: 'alpaca', live: v.live, side: t.side, symbol, qty: t.qty, pending: true });
      result = await alpaca('/v2/orders', { method: 'POST', body });
      result = { id: result.id, status: result.status };
    } else {
      const acct = (state.account && state.account.accounts || [])[0];
      if (!acct) throw new Error('No Tradovate account found on this connection.');
      Loki.emit('order', { venue: 'tradovate', live: v.live, side: t.side, symbol: t.symbol, qty: t.qty, pending: true });
      const r = await tradovate('order', { method: 'POST', body: { account_id: acct.id, account_spec: acct.name, symbol: t.symbol, action: t.side === 'buy' ? 'Buy' : 'Sell', quantity: Math.round(t.qty), order_type: t.type === 'limit' ? 'Limit' : 'Market', price: t.type === 'limit' ? t.limit : null, confirm: v.live ? PHRASE : '' } });
      result = { id: r.order_id, status: 'sent' };
    }
    await Loki.audit.append({ kind: v.live ? 'live' : 'paper', venue: state.venue, action: 'order', symbol: t.symbol, side: t.side, qty: t.qty, type: t.type, limit: t.limit, broker_order_id: result.id, confidence: c.mc.confidence, council_p: c.council ? c.council.p_up : null, risk: c.risk.level });
    setMsg('trade-msg', `${v.label}: ${t.side.toUpperCase()} ${t.qty} ${t.symbol} sent (order ${String(result.id).slice(0, 8)}…, ${result.status}).`);
    setTimeout(() => refresh(true), 1200);
  }

  async function killSwitch() {
    const v = VENUES[state.venue];
    if (!window.confirm(`KILL SWITCH on ${v.label}: cancel every open order and close every position${v.live ? ' WITH REAL MONEY' : ''}?`)) return;
    if (v.broker === 'alpaca') {
      await alpaca('/v2/orders', { method: 'DELETE' }).catch(() => null);
      await alpaca('/v2/positions?cancel_orders=true', { method: 'DELETE' });
    } else {
      const byAccount = {};
      for (const p of state.positions) (byAccount[p.account_id] ||= []).push(p.contract_id);
      for (const [account, ids] of Object.entries(byAccount)) await tradovate('flatten', { method: 'POST', body: { account_id: Number(account), contract_ids: ids } });
    }
    await Loki.audit.append({ kind: v.live ? 'live' : 'paper', venue: state.venue, action: 'kill-switch', positions: state.positions.length });
    setMsg('trade-msg', 'Kill switch sent: orders cancelled and positions closing.');
    setTimeout(() => refresh(true), 1500);
  }

  /* -------------------------------------------------------------- autopilot */
  async function autopilotTick() {
    const v = VENUES[state.venue], cfg = Loki.config();
    if (v.live || !cfg.autopilot || !state.account || v.broker !== 'alpaca') return;
    const risk = riskNow(), log = [];
    for (const sym of cfg.watchlist.slice(0, 8)) {
      try {
        const bars = await Loki.data.bars(sym, { interval: '1d', limit: 300 });
        const council = Loki.council.analyze(sym, bars, { sentiment: Loki.swarm && Loki.swarm.snapshot().running ? Loki.swarm.snapshot().sentiment : undefined });
        const alpacaSym = Loki.data.isCrypto(sym) ? sym.replace('-USD', '/USD') : sym;
        const held = state.positions.find((p) => p.symbol === alpacaSym.replace('/', '') || p.symbol === alpacaSym);
        const threshold = 0.5 + cfg.min_council_confidence / 400;
        if (!held && council.p_up >= threshold && risk.level !== 'red') {
          const budget = state.account.equity * (cfg.max_position_pct / 100) * risk.sizeMultiplier * Loki.copilot.modeMultiplier();
          const price = bars[bars.length - 1].c, qty = Loki.data.isCrypto(sym) ? Math.floor(budget / price * 1e4) / 1e4 : Math.floor(budget / price);
          if (qty > 0) {
            const order = await alpaca('/v2/orders', { method: 'POST', body: { symbol: alpacaSym, qty: String(qty), side: 'buy', type: 'market', time_in_force: Loki.data.isCrypto(sym) ? 'gtc' : 'day' } });
            Loki.emit('order', { venue: 'alpaca', side: 'buy', symbol: alpacaSym, qty, pending: true });
            await Loki.audit.append({ kind: 'paper', venue: state.venue, action: 'order', symbol: alpacaSym, side: 'buy', qty, type: 'market', broker_order_id: order.id, council_p: council.p_up, risk: risk.level, autopilot: true });
            log.push(`BUY ${qty} ${alpacaSym} (council ${Math.round(council.p_up * 100)}% up)`);
          }
        } else if (held && (council.p_up <= 0.44 || risk.level === 'red')) {
          await alpaca(`/v2/positions/${encodeURIComponent(held.symbol)}`, { method: 'DELETE' });
          Loki.emit('order', { venue: 'alpaca', side: 'sell', symbol: held.symbol, qty: held.qty, pending: true });
          await Loki.audit.append({ kind: 'paper', venue: state.venue, action: 'order', symbol: held.symbol, side: 'sell', qty: held.qty, type: 'close', council_p: council.p_up, risk: risk.level, autopilot: true, pnl: held.pl });
          log.push(`CLOSE ${held.symbol} (${risk.level === 'red' ? 'risk breaker' : `council ${Math.round(council.p_up * 100)}% up`})`);
        }
      } catch (error) { log.push(`${sym}: ${error.message}`); }
    }
    setMsg('ap-msg', `Autopilot ran ${F.time(Date.now())}: ${log.join(' · ') || 'no trades (no edge or already positioned)'}.`);
    refresh();
  }

  /* -------------------------------------------------------------------- UI */
  function setMsg(id, text, bad = false) { const el = $(id); if (el) { el.textContent = text; el.classList.toggle('bad', bad); } }
  function render() {
    const v = VENUES[state.venue];
    root.classList.toggle('trade-live', v.live);
    document.body.classList.toggle('trade-live-active', v.live && !root.hidden);
    root.querySelectorAll('[data-venue]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.venue === state.venue)));
    $('trade-banner').innerHTML = v.live
      ? `<b>LIVE · REAL MONEY</b> ${F.esc(v.label)}. Orders here move real funds. ${armed() ? 'Armed for this session.' : 'Not armed.'}`
      : `<b>SIMULATED</b> ${F.esc(v.label)}: practice money; fills and P&amp;L are not real.`;
    $('alpaca-cred').hidden = v.broker !== 'alpaca'; $('tradovate-cred').hidden = v.broker !== 'tradovate';
    if (render.venue !== state.venue) { render.venue = state.venue; const c = getCred(); $('ak-id').value = v.broker === 'alpaca' && c ? c.key_id : ''; $('ak-secret').value = ''; }
    $('arm-box').hidden = !v.live;
    $('tk-send').disabled = v.live && !armed();
    const cred = getCred();
    $('cred-status').textContent = cred ? `Connected key ${String(cred.key_id || cred.name || '').slice(0, 6)}… (${v.live ? 'this tab only' : 'saved in this browser'})` : 'Not connected';
    const a = state.account;
    $('trade-account').innerHTML = a ? [['Equity', F.usd(a.equity)], ['Cash', F.usd(a.cash)], ['Buying power', a.buying_power == null ? '—' : F.usd(a.buying_power)], ['Today', F.usd(a.day_pl, true)]]
      .map(([k, val]) => `<div><span>${k}</span><b>${val}</b></div>`).join('') + (a.blocked ? '<p class="form-message">The broker reports trading is blocked on this account.</p>' : '') : '<p class="muted">Connect an account to see balances.</p>';
    $('trade-positions').innerHTML = state.positions.length ? `<table class="tk-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>Now</th><th>P&amp;L</th></tr></thead><tbody>${state.positions.map((p) => `<tr><td>${F.esc(p.symbol)}</td><td>${F.num(p.qty, 4)}</td><td>${F.num(p.avg)}</td><td>${p.price == null ? '—' : F.num(p.price)}</td><td class="${(p.pl || 0) >= 0 ? 'up' : 'down'}">${p.pl == null ? '—' : F.usd(p.pl, true)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No open positions.</p>';
    $('trade-orders').innerHTML = state.orders.length ? `<table class="tk-table"><thead><tr><th>When</th><th>Order</th><th>Status</th><th></th></tr></thead><tbody>${state.orders.slice(0, 12).map((o) => `<tr><td>${o.at ? F.esc(new Date(o.at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })) : ''}</td><td>${F.esc(`${o.side} ${o.qty ?? ''} ${o.symbol} ${o.type || ''}`)}</td><td>${F.esc(o.status || '')}</td><td>${/new|accepted|pending|working/i.test(o.status || '') ? `<button class="button secondary tk-cancel" data-cancel="${F.esc(o.id)}" type="button">Cancel</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No recent orders.</p>';
    const r = riskNow();
    $('trade-risk').innerHTML = `<b class="risk-${r.level}">${r.level.toUpperCase()}</b> ${F.esc(r.reason)} · drawdown ${F.pct(r.drawdown)} of a ${F.pct(r.limit)} dynamic limit · size ×${r.sizeMultiplier.toFixed(2)}${r.actions.length ? `<ul>${r.actions.map((x) => `<li>${F.esc(x.type)} ${F.esc(x.symbol)}: ${F.esc(x.reason)}</li>`).join('')}</ul>` : ''}`;
    const cfg = Loki.config();
    $('ap-toggle').textContent = cfg.autopilot ? 'Autopilot ON (paper/demo) · pause' : 'Start paper autopilot';
    $('ap-toggle').disabled = v.live;
    $('ap-note').textContent = v.live ? 'The autopilot never trades live accounts: live orders are always yours to confirm.' : `Every 15 minutes while this page is open: council over ${cfg.watchlist.slice(0, 8).join(', ')}; size = ${cfg.max_position_pct}% × risk × swarm × ${cfg.risk_mode} mode.`;
  }

  async function renderAudit() {
    const box = $('audit-badge'); if (!box) return;
    try {
      const v = await Loki.audit.verify();
      const m = v.metrics || Loki.audit.metrics();
      box.className = `audit-badge ${v.ok ? 'ok' : 'broken'}`;
      box.innerHTML = v.count
        ? `<b>${v.ok ? '✓ Verified log' : '✗ Log broken'}</b> ${v.count} signed entries · device key ${F.esc((v.fingerprint || '').slice(0, 8))} · since ${F.esc(new Date(v.genesis || Date.now()).toLocaleDateString())}${v.ok ? '' : ` · entry ${v.broken_at}: ${F.esc(v.why)}`}<br><span class="caption">From the signed entries only: ${m.executions} executions (${m.live} live, ${m.paper} paper), realised ${F.usd(m.realised_pnl, true)} over ${m.closed} closed.</span>`
        : '<b>Proof-of-Strategy</b> No executions yet. Every order you or the autopilot place here is signed and chained automatically.';
    } catch (error) { box.className = 'audit-badge'; box.textContent = `Audit log unavailable: ${error.message}`; }
  }

  async function reconcile() {
    const entries = Loki.audit.entries().filter((e) => e.data && e.data.broker_order_id && String(e.data.venue).startsWith('alpaca')).slice(0, 15);
    if (!entries.length) { setMsg('audit-msg', 'No Alpaca orders in the log yet.'); return; }
    let ok = 0, missing = 0;
    for (const e of entries) {
      try { const o = await alpaca(`/v2/orders/${e.data.broker_order_id}`, { venue: e.data.venue }); if (o && o.symbol) ok += 1; else missing += 1; } catch { missing += 1; }
    }
    setMsg('audit-msg', `Reconciled with Alpaca: ${ok} of ${entries.length} logged orders confirmed by the broker${missing ? `, ${missing} not found (different keys or venue)` : ''}.`, missing > 0);
  }

  /* --------------------------------------------------------------- wiring */
  root.addEventListener('click', async (e) => {
    const venue = e.target.closest('[data-venue]');
    if (venue) { state.venue = venue.dataset.venue; Loki.store.set('loki_trade_venue_v1', state.venue); state.check = null; $('tk-result').innerHTML = ''; render(); pill(); refresh(); return; }
    const cancel = e.target.closest('[data-cancel]');
    if (cancel) { try { if (VENUES[state.venue].broker === 'alpaca') await alpaca(`/v2/orders/${cancel.dataset.cancel}`, { method: 'DELETE' }); else await tradovate('cancel', { method: 'POST', body: { order_id: Number(cancel.dataset.cancel) } }); setMsg('trade-msg', 'Cancel sent.'); setTimeout(() => refresh(true), 800); } catch (error) { setMsg('trade-msg', error.message, true); } }
  });
  $('alpaca-cred').addEventListener('submit', async (e) => {
    e.preventDefault();
    const key_id = $('ak-id').value.trim(), secret_key = $('ak-secret').value.trim();
    if (!/^[A-Za-z0-9]{10,32}$/.test(key_id) || secret_key.length < 20) { setMsg('trade-msg', 'Those do not look like Alpaca keys.', true); return; }
    if (VENUES[state.venue].live !== key_id.startsWith('AK') && /^(AK|PK)/.test(key_id)) { setMsg('trade-msg', VENUES[state.venue].live ? 'That is a PAPER key (PK…). Live keys start with AK.' : 'That is a LIVE key (AK…). Paper keys start with PK.', true); return; }
    setCred(state.venue, { key_id, secret_key }); $('ak-secret').value = '';
    await refresh();
  });
  $('tradovate-cred').addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = VENUES[state.venue];
    try {
      setMsg('trade-msg', 'Asking Tradovate for a token…');
      const r = await Loki.postJSON('/api/broker/tradovate/token', { environment: v.env, name: $('tv-name').value.trim(), password: $('tv-pass').value, cid: Number($('tv-cid').value), sec: $('tv-sec').value.trim() });
      setCred(state.venue, { token: r.access_token, name: $('tv-name').value.trim(), expires: r.expires });
      $('tv-pass').value = ''; $('tv-sec').value = '';
      await refresh();
    } catch (error) { setMsg('trade-msg', error.message, true); }
  });
  $('cred-forget').addEventListener('click', () => { clearCred(state.venue); state.account = null; state.positions = []; state.orders = []; render(); setMsg('trade-msg', 'Forgotten on this device.'); });
  $('arm-box').addEventListener('submit', (e) => {
    e.preventDefault();
    if ($('arm-phrase').value.trim().toLowerCase() === PHRASE) { Loki.store.session.set('loki_live_armed_v1', Date.now()); $('arm-phrase').value = ''; setMsg('trade-msg', 'Live trading armed for 2 hours in this tab.'); render(); }
    else setMsg('trade-msg', `Type exactly: ${PHRASE}`, true);
  });
  $('tk-type').addEventListener('change', () => { $('tk-limit-wrap').hidden = $('tk-type').value !== 'limit'; });
  $('tk-check').addEventListener('click', () => preTradeCheck().catch((error) => { $('tk-result').innerHTML = `<p class="form-message">${F.esc(error.message)}</p>`; }));
  $('tk-send').addEventListener('click', () => place().catch((error) => setMsg('trade-msg', error.message, true)));
  $('trade-refresh').addEventListener('click', () => refresh());
  $('kill-switch').addEventListener('click', () => killSwitch().catch((error) => setMsg('trade-msg', error.message, true)));
  $('ap-toggle').addEventListener('click', () => { const cfg = Loki.config(); Loki.copilot.apply({ changes: [{ key: 'autopilot', from: cfg.autopilot, to: !cfg.autopilot }], config: { ...cfg, autopilot: !cfg.autopilot } }); render(); if (!cfg.autopilot) autopilotTick(); });
  $('audit-verify').addEventListener('click', renderAudit);
  $('audit-reconcile').addEventListener('click', () => reconcile().catch((error) => setMsg('audit-msg', error.message, true)));
  $('audit-export').addEventListener('click', async () => {
    const data = await Loki.audit.export();
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: `loki-proof-of-strategy-${data.fingerprint}.json` }); a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('audit-file').addEventListener('change', async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try { const v = await Loki.audit.verify(JSON.parse(await file.text())); setMsg('audit-msg', v.ok ? `File verified: ${v.count} entries, chain and signatures intact (key ${String(v.fingerprint).slice(0, 8)}).` : `File FAILED at entry ${v.broken_at}: ${v.why}.`, !v.ok); }
    catch (error) { setMsg('audit-msg', `Could not verify: ${error.message}`, true); }
  });
  Loki.on('config:change', render);
  Loki.on('audit:append', renderAudit);
  Loki.on('risk:update', () => {});
  setInterval(() => { if (!root.hidden && !document.hidden) autopilotTick(); }, 15 * 60000);
  setInterval(() => { if (!root.hidden && !document.hidden && getCred()) refresh(true); }, 60000);
  function pill() {
    const el = document.getElementById('mode-pill'), live = VENUES[state.venue].live;
    if (!el || root.hidden) return;
    el.textContent = live ? 'LIVE · REAL MONEY' : `PAPER TRADING · ${VENUES[state.venue].label.toUpperCase()}`;
    el.classList.toggle('mode-pill-real', live); el.classList.toggle('mode-pill-sim', !live);
  }
  const onView = () => { document.body.classList.toggle('trade-live-active', VENUES[state.venue].live && !root.hidden); if (!root.hidden) { render(); pill(); refresh(); renderAudit(); } };
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  render();
  onView();
})();
