/* Auto day trading on the visitor's Alpaca PAPER account (Day Trader panel).
 *
 * Uses the paper keys saved on the Real account page (alpaca_keys.js). While it
 * runs, this tab asks the server (/api/broker/alpaca/daytrade) for one check
 * every 30 seconds and again just after each half hour; the server reads the
 * Day Trader's plan and the account, and sends the orders that make the account
 * hold what the tested rules hold. Only one open tab runs it at a time. All the
 * guards (paper host, margin minimum, buying power, 2% daily loss stop, closing
 * time, spread, drift alarm, size caps) live on the server, so a changed page
 * cannot skip them. Every receipt, including a blocked order's, goes into the
 * device's signed trade log; a repeat of the same message is logged only once.
 * After the close it adds one signed summary of the day to that log. Desktop alerts
 * (if allowed) fire only for what needs a person: auto trading switched off, the drift
 * alarm, the loss stop, a refused close, or positions still open after the bell.
 * "Download diagnostic bundle" saves the policy, drift state and the last 50 receipts.
 */
'use strict';
(() => {
  const root = document.getElementById('dt-alpaca');
  if (!root) return;
  const { $, element, request, table, number, message } = window.LokiWorkspace; // shared helpers (workspace.js)

  // --- State -----------------------------------------------------------------------
  const KEYS = 'loki_alpaca_keys_v1'; // written by alpaca_keys.js
  const STATE = 'loki_alpaca_daytrader_v1';
  const LEASE = 'loki_alpaca_daytrader_lease';
  const CHECK_MS = 30 * 1000;
  const HALF_HOUR_MS = 30 * 60 * 1000;
  const AFTER_DECISION_MS = [8000, 25000];
  const CONFIRM = 'day trade paper money';

  const keys = () => {
    try {
      const value = JSON.parse(localStorage.getItem(KEYS) || 'null');
      return value && value.key_id && value.secret_key ? { key_id: value.key_id, secret_key: value.secret_key } : null;
    } catch { return null; }
  };
  let state = (() => { try { return { running: false, log: [], ...(JSON.parse(localStorage.getItem(STATE) || 'null') || {}) }; } catch { return { running: false, log: [] }; } })();
  let busy = false, last = null, timer = null, aligned = null;
  const save = () => { try { localStorage.setItem(STATE, JSON.stringify(state)); } catch { /* storage full */ } };
  const nyDay = () => new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  const nyClock = () => new Date().toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour12: false });
  const once = (key) => { const k = `${nyDay()}:${key}`; if ((state.seen || {})[k]) return false; state.seen = { ...(state.seen || {}), [k]: 1 }; return true; };
  /** A desktop alert, only if the visitor allowed them; never a reason to stop trading. */
  const notify = (title, body) => {
    try { if (window.Notification && window.Notification.permission === 'granted') new window.Notification(title, { body, tag: title }); } catch { /* alerts are optional */ }
  };
  const log = (text, kind = 'info') => { state.log.unshift({ t: new Date().toISOString(), text, kind }); state.log.length = Math.min(state.log.length, 200); save(); };

  function claim() { // one tab trades; another open tab only watches
    try {
      const held = Number(localStorage.getItem(LEASE) || 0);
      if (held && Date.now() - held < 90000 && !root.dataset.owner) return false;
      localStorage.setItem(LEASE, String(Date.now()));
      root.dataset.owner = '1';
      return true;
    } catch { return true; }
  }

  // --- One check ---------------------------------------------------------------------
  async function check({ start = false, stop = false } = {}) {
    const saved = keys();
    if (!saved || busy) return null;
    if (!start && !stop && !claim()) { $('#dta-message').textContent = 'Running in another tab.'; return null; }
    busy = true; render();
    try {
      const data = await request('/api/broker/alpaca/daytrade', { ...saved, confirm: CONFIRM, start, stop });
      last = data;
      tally(data);
      for (const a of data.actions || []) {
        if (a.did === 'close' && a.status === 'refused' && once(`close-refused:${a.symbol}`)) notify('Loki: a close was refused', `${a.symbol}: ${a.detail || 'Alpaca refused it'}. It tries again on the next check; check the Alpaca dashboard.`);
        const seen = `${new Date().toISOString().slice(0, 10)}:${a.symbol}:${a.status}:${a.detail || ''}`;
        if (a.status !== 'sent') { if (state.seen && state.seen[seen]) continue; state.seen = { ...(state.seen || {}), [seen]: 1 }; }
        const what = a.did === 'close' ? `CLOSE ${a.symbol} (${a.shares > 0 ? 'bought back' : 'sold'} ${Math.abs(a.shares)} shares)`
          : `${a.shares > 0 ? 'BUY' : 'SHORT'} ${Math.abs(a.shares)} ${a.symbol}`;
        const lag = lagText(data.markets.find((m) => m.symbol === a.symbol), data.checked_at);
        const sealed = a.receipt ? ` · receipt ${a.receipt.hash.slice(0, 12)}` : '';
        log(`${what} — ${a.status}${a.detail ? `: ${a.detail}` : ''}${a.status === 'sent' ? lag + sealed : ''}`, a.status === 'refused' ? 'bad' : a.status === 'sent' ? 'good' : 'info');
        if (a.receipt) record(a, data);
      }
      const drift = data.drift || {};
      const driftKey = `${new Date().toISOString().slice(0, 10)}:drift:${drift.tier}`;
      if (drift.tier && drift.tier !== 'green' && !(state.seen || {})[driftKey]) {
        state.seen = { ...(state.seen || {}), [driftKey]: 1 };
        log(`Drift alarm ${drift.tier.toUpperCase()}: new positions sized x${drift.multiplier}. ${(drift.reasons || []).join('; ')}.`, 'bad');
        notify(`Loki drift alarm: ${drift.tier.toUpperCase()}`, `New positions are sized x${drift.multiplier}. ${(drift.reasons || []).join('; ')}.`);
      }
      const leftOpen = (data.markets || []).filter((m) => m.held);
      if (state.running && !data.market_open && leftOpen.length && nyClock() >= '16:00' && once('left-open')) {
        const which = leftOpen.map((m) => `${m.symbol} ${m.held > 0 ? '+' : ''}${m.held}`).join(', ');
        log(`After the close the account still holds ${which}. Close them in the Alpaca dashboard, or press Stop when the market opens.`, 'bad');
        notify('Loki: positions open after the close', which);
      }
      if (state.running && !data.market_open && nyClock() >= '16:00') await digest(data);
      if (data.closing_time && state.running) {
        const key = `${new Date().toISOString().slice(0, 10)}:bell`;
        if (!(state.seen || {})[key]) { state.seen = { ...(state.seen || {}), [key]: 1 }; log(`Closing time: ${data.minutes_to_close} minutes to Alpaca's close, so every Day Trader position is being closed.`); }
      }
      if (data.stopped_for_loss && state.running) {
        state.running = false;
        log(`Daily loss stop: the account is ${data.day_change_pct.toFixed(2)}% down today. Every Day Trader position was closed and auto trading is off until you start it again.`, 'bad');
        notify('Loki: daily loss stop', `The paper account is ${data.day_change_pct.toFixed(2)}% down today. Positions closed; auto trading is off.`);
      }
      message('#dta-message', state.running
        ? (data.market_open ? `Running. Last check ${new Date().toLocaleTimeString()}.` : 'Running. The US market is closed; it trades when it opens (9:30am New York).')
        : '', false);
      return data;
    } catch (error) {
      message('#dta-message', error.message || 'Alpaca could not be reached; it will try again.', true);
      if (/rejected those keys|blocked trading|already holds|at least US\$/.test(error.message || '')) {
        if (state.running) notify('Loki auto trading switched off', error.message);
        state.running = false; log(error.message, 'bad');
      }
      return null;
    } finally { busy = false; save(); render(); }
  }

  /** Add an order's receipt to this device's signed, hash-chained trade log (engine/audit.js). */
  function record(action, data) {
    try {
      if (!window.Loki || !window.Loki.audit) return;
      const blocked = action.receipt.action === 'blocked';
      window.Loki.audit.append({ kind: blocked ? 'guard' : 'paper', venue: 'alpaca', source: 'day-trader', action: blocked ? 'blocked' : action.did, symbol: action.symbol,
        qty: action.shares, broker_order_id: (action.order && action.order.id) || null,
        client_order_id: (action.order && action.order.client_order_id) || null,
        receipt: action.receipt.hash, policy: data.policy_hash, trace_id: data.trace_id,
        why: blocked ? action.detail : undefined, drift: data.drift ? `${data.drift.tier} x${data.drift.multiplier}` : undefined });
    } catch { /* the log is a record, never a reason to stop trading */ }
  }

  /** Today's figures for the end-of-day summary (New York day; each blocked reason counted once a day). */
  function tally(data) {
    const day = nyDay();
    if (!state.tally || state.tally.day !== day) state.tally = { day, checks: 0, signals: {}, opened: 0, closed: 0, refused: 0, blocked: {}, digested: false };
    const t = state.tally;
    if (!data.market_open) return;
    t.checks += 1;
    for (const m of data.markets || []) if (m.rules && m.signal_at) t.signals[`${m.symbol}@${m.signal_at}`] = 1;
    for (const a of data.actions || []) {
      if (a.status === 'sent') { if (a.did === 'close') t.closed += 1; else t.opened += 1; }
      else if (a.status === 'refused') t.refused += 1;
      else if (a.status === 'held back') {
        const reason = String(a.detail || '').replace(/; it tries again on the next check$/, '').replace(/\d+\.\d+/g, '#').replace(/\b\d+ s old/g, '# s old').replace(/\(\d+ of \d+ left\)/g, '(# of # left)');
        const key = `${a.symbol}:${reason}`;
        if (!(t.blockedSeen || {})[key]) { t.blockedSeen = { ...(t.blockedSeen || {}), [key]: 1 }; t.blocked[reason] = (t.blocked[reason] || 0) + 1; }
      }
    }
  }

  /** After the close: one signed summary of the day in the trade log. */
  async function digest(data) {
    const t = state.tally;
    if (!t || t.day !== nyDay() || !t.checks || t.digested) return;
    t.digested = true; save();
    const d = data.drift || {};
    const summary = { kind: 'digest', venue: 'alpaca', source: 'day-trader', day: t.day, checks: t.checks,
      signals: Object.keys(t.signals).length, opened: t.opened, closed: t.closed, refused: t.refused, blocked: t.blocked,
      account_day_change_pct: data.day_change_pct, equity: data.equity, still_open: (data.markets || []).filter((m) => m.held).length,
      drift: `${d.tier || '—'} x${d.multiplier ?? '—'}${d.z != null ? ` z ${Number(d.z).toFixed(2)}` : ''}`, policy: data.policy_hash };
    let hash = '';
    try { const entry = window.Loki && window.Loki.audit ? await window.Loki.audit.append(summary) : null; hash = entry ? entry.hash : ''; } catch { /* the summary is a record only */ }
    const blocked = Object.values(t.blocked).reduce((a, b) => a + b, 0);
    log(`Day summary ${t.day}: ${summary.signals} signals, ${t.opened} opened, ${t.closed} closed, ${blocked} held back, ${t.refused} refused; whole account ${data.day_change_pct >= 0 ? '+' : ''}${Number(data.day_change_pct).toFixed(2)}% today; drift ${summary.drift}${hash ? `; signed log head ${hash.slice(0, 12)}` : ''}.`);
  }

  /** Policy, drift state and the last 50 Day Trader receipts as one JSON file. The Alpaca keys are never in it. */
  async function exportBundle() {
    const audit = window.Loki && window.Loki.audit;
    let full = null, check = null;
    try { full = audit ? await audit.export() : null; check = audit ? await audit.verify() : null; } catch { /* export what there is */ }
    const receipts = full ? full.entries.filter((e) => e.data && e.data.source === 'day-trader').slice(-50) : [];
    const pick = (o, keys) => (o ? Object.fromEntries(keys.filter((k) => k in o).map((k) => [k, o[k]])) : null);
    const bundle = {
      format: 'loki-daytrader-diagnostics/1', exported_at: new Date().toISOString(),
      policy: last ? last.policy : null, policy_hash: last ? last.policy_hash : null, drift: last ? last.drift : null,
      last_check: pick(last, ['checked_at', 'trace_id', 'market_open', 'equity', 'day_change_pct', 'buying_power', 'minutes_to_close', 'closing_time', 'stopped_for_loss', 'rate_budget', 'clock_skew_s', 'markets', 'blocked']),
      today: state.tally ? { ...state.tally, blockedSeen: undefined, signals: Object.keys(state.tally.signals || {}).length } : null,
      receipts, public_key: full ? full.public_key : null, key_fingerprint: full ? full.fingerprint : null,
      whole_log_verification: check,
      how_to_verify: 'Each receipt entry carries hash = SHA-256 of canonical {seq, ts, data, prev, key} and an ECDSA P-256 signature of that hash by public_key. The chain links (prev) can be followed in the full export from the Proof-of-strategy panel.',
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' }));
    const link = Object.assign(document.createElement('a'), { href: url, download: `loki-daytrader-diagnostics-${nyDay()}.json` });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message('#dta-message', `Saved: policy, drift state and ${receipts.length} receipt(s). Your Alpaca keys are not in the file.`, false);
  }

  function lagText(market, checkedAt) {
    const at = Date.parse(market && market.signal_at), now = Date.parse(checkedAt);
    return Number.isFinite(at) && Number.isFinite(now) && now >= at && now - at < 600000 ? `, ${Math.round((now - at) / 1000)} s after the signal` : '';
  }

  // --- Rendering -----------------------------------------------------------------------
  function render() {
    const saved = keys();
    $('#dta-status').textContent = state.running ? (last && !last.market_open ? 'On · market closed' : 'On') : 'Off';
    $('#dta-status').classList.toggle('good', state.running);
    $('#dta-start').disabled = busy || !saved || state.running;
    $('#dta-stop').disabled = busy || !saved;
    $('#dta-check').disabled = busy || !saved;
    $('#dta-intro').replaceChildren(saved
      ? `Press Start and the Day Trader trades your Alpaca paper account by itself with the same rules as above: equal money in each of its markets, long or short, everything closed 10 minutes before the bell. It needs at least US$2,000 in the account, stays within your buying power, stops for the day if the account falls 2%, and sizes down by itself if its live results drift from the test.`
      : Object.assign(element('span'), { innerHTML: 'First save your Alpaca <strong>paper</strong> keys on the <a href="#real-market">Real account</a> page, then come back here.' }));
    const out = $('#dta-out');
    out.replaceChildren();
    if (last) {
      out.append(element('p', `Paper account equity US$${number(last.equity)} · today ${last.day_change_pct >= 0 ? '+' : ''}${last.day_change_pct.toFixed(2)}% (whole account) · buying power US$${number(last.buying_power)} · US market ${last.market_open ? 'open' : 'closed'}.`, 'integration-note'));
      const budget = last.rate_budget, skew = last.clock_skew_s;
      if (budget || skew != null) out.append(element('p', [budget ? `Alpaca request budget: ${budget.remaining} of ${budget.limit} left this minute.` : '', skew != null ? `This server's clock is ${Math.abs(skew).toFixed(1)} s ${skew >= 0 ? 'ahead of' : 'behind'} Alpaca's (Alpaca's is used).` : ''].join(' ').trim(), 'caption'));
      if (last.drift) out.append(driftLine(last.drift));
      const word = (n) => (n > 0 ? 'Long' : n < 0 ? 'Short' : 'Flat');
      out.append(table(['Market', 'Rules', 'Holding (shares)', 'Price', 'Note'],
        last.markets.map((m) => [m.symbol, word(m.rules), m.held ? `${m.held > 0 ? '+' : ''}${m.held}` : '0', number(m.price), m.note || (m.action === 'wait' ? 'order filling' : '')])));
      if (last.policy) out.append(policyBox(last.policy, last.policy_hash));
    }
    $('#dta-log').innerHTML = '';
    $('#dta-log').append(...(state.log.length ? state.log.slice(0, 60).map((l) => element('li', `${new Date(l.t).toLocaleString()} ${l.text}`, l.kind)) : [element('li', 'No Alpaca orders yet.')]));
  }

  function driftLine(d) {
    const fmt = (v, digits = 2) => (v == null ? '—' : Number(v).toFixed(digits));
    const pctv = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
    const text = d.trades < d.min_trades
      ? `Drift alarm: warming up (${d.trades} of ${d.min_trades} finished round trips). Size x1.0 until there is enough to judge.`
      : `Drift alarm: ${String(d.tier).toUpperCase()} · size x${d.multiplier} · last ${d.trades} round trips: won ${pctv(d.win_rate)} (tested ${pctv(d.expected_win_rate)}, z ${fmt(d.z)}), payoff ${fmt(d.payoff)} (tested ${fmt(d.expected_payoff)}), slippage ${fmt(d.slippage_bps, 1)} bp (assumed ${fmt(d.assumed_cost_bps, 1)} bp).`;
    const line = element('p', text, 'caption');
    line.style.color = d.tier === 'red' ? '#ffb3a7' : d.tier === 'amber' ? '#ffd27a' : '';
    return line;
  }

  function policyBox(p, hash) {
    const box = element('details', null, 'pf-log-box');
    const usd = (n) => `US$${Number(n).toLocaleString('en-US')}`;
    box.append(element('summary', `Risk policy v${p.version} · ${String(hash).slice(0, 12)}`));
    box.append(element('p', [
      'Checked on the server for every order, before anything reaches Alpaca:',
      `paper account only; at least ${usd(p.min_equity_usd)} of equity; new positions within ${Math.round(Number(p.max_buying_power_used) * 100)}% of buying power;`,
      `stop for the day at a ${p.daily_loss_stop_pct}% loss; everything closed ${p.flatten_minutes_before_close} minutes before Alpaca's close;`,
      `quotes from the SIP feed when your Alpaca plan has it, otherwise IEX, and never older than ${p.quote_max_age_seconds} seconds;`,
      `a drift alarm that sizes new positions x0.5 or x0.1 when live results fall short of the test;`,
      `at most ${usd(p.max_per_market_usd)} and ${Number(p.max_shares).toLocaleString('en-US')} shares a market;`,
      `a new position only within ${Math.round(p.signal_ttl_seconds / 60)} minutes of the signal;`,
      `no new position while the spread is wider than ${p.max_spread_pct}%; shorts ${p.shorts};`,
      `no new position before ${p.opening_lockout_until} New York time, in a stock that may be halted (a one-sided, crossed or stale quote, or not listed as active and tradable), or on the ex-date of a split, spin-off or special dividend;`,
      `entries are capped limit orders (${String(p.entry_order).replace(/^limit, /, '')}), at most ${p.limit_through_quote_pct}% through the quote, ${p.max_entry_tries} tries a signal; closing orders are ${p.exit_order};`,
      `below ${p.rate_limit_reserve_pct}% of Alpaca's request budget it opens nothing; every time is taken from Alpaca's clock.`,
      'Each order, and each blocked order, gets a receipt (a SHA-256 hash of the market snapshot, this policy, the drift state and the checks),',
      "added to this device's signed trade log, and a fixed order id, so a retried request can never place it twice.",
    ].join(' '), 'caption'));
    return box;
  }

  // --- Controls -------------------------------------------------------------------------
  $('#dta-start').addEventListener('click', async () => {
    try { if (window.Notification && window.Notification.permission === 'default') window.Notification.requestPermission().catch(() => {}); } catch { /* alerts are optional */ }
    if (!window.confirm('Start auto day trading on your Alpaca PAPER account?\n\nIt will buy and short the Day Trader\'s markets by itself while this tab is open, and close everything before 4pm New York time. Paper money only. Results are not guaranteed.')) return;
    const data = await check({ start: true });
    if (!data) return;
    state.running = true; state.day = new Date().toISOString().slice(0, 10);
    log('Auto day trading started on the Alpaca paper account.', 'good');
    message('#dta-message', data.market_open ? 'Running. It checks every 30 seconds and right after each half hour.' : 'Running. The US market is closed; it trades when it opens (9:30am New York).', false);
    save(); render();
  });
  $('#dta-stop').addEventListener('click', async () => {
    state.running = false; save();
    const data = await check({ stop: true });
    log(data ? `Stopped. ${(data.actions || []).length} position(s) closed.` : 'Stopped. Closing positions failed; check the Alpaca dashboard.', data ? 'info' : 'bad');
    if (data) message('#dta-message', 'Stopped. Day Trader positions are closed; nothing else in the account was touched.', false);
    render();
  });
  $('#dta-check').addEventListener('click', () => check());
  $('#dta-export').addEventListener('click', () => exportBundle());
  window.addEventListener('beforeunload', (event) => {
    if (state.running && last && last.market_open && last.markets.some((m) => m.held)) { event.preventDefault(); event.returnValue = ''; }
  });

  // --- Timers ---------------------------------------------------------------------------
  function untilAligned(now = Date.now()) {
    const base = Math.floor(now / HALF_HOUR_MS) * HALF_HOUR_MS;
    return [...AFTER_DECISION_MS.map((ms) => base + ms), base + HALF_HOUR_MS + AFTER_DECISION_MS[0]].find((t) => t > now + 500) - now;
  }
  (function scheduleAligned() { aligned = setTimeout(() => { if (state.running) check(); scheduleAligned(); }, untilAligned()); })();
  timer = setInterval(() => { if (state.running) check(); else render(); }, CHECK_MS);
  window.addEventListener('beforeunload', () => { clearInterval(timer); clearTimeout(aligned); });
  render();
  if (state.running) check();
})();
