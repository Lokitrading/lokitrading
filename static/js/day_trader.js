/* DAY TRADER AI — paper book that mirrors the tested intraday rules.
 *
 * The server (/api/fusion/ai/daytrade) replays today's 5-minute bars through
 * the same code the 10-year test used and says, per market, whether the rules
 * are long, short or flat right now. This script keeps a paper book in the
 * browser and moves it to match: equal capital per market, in and out the same
 * day, everything closed before the bell. Paper money only; no orders leave
 * the browser.
 */
'use strict';
(() => {
  const root = document.getElementById('day-trader');
  if (!root) return;
  const KEY = 'loki_day_trader_v1';
  const LEASE = 'loki_day_trader_lease';
  const CHECK_MS = 60 * 1000;
  const FUTURES = new Set(['MNQ', 'MES', 'M2K', 'MYM']);
  const COST = (sym) => (FUTURES.has(sym) ? 0.5 : sym === 'QQQ' || sym === 'SPY' ? 1 : 2) / 10000; // per side, as in the test
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (v) => (window.LokiCurrency ? window.LokiCurrency.format(v) : (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v, d = 1) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(d)}%` : '—');
  const fresh = (cash = 10000, mode = 'stocks') => ({ start: cash, realized: 0, running: false, positions: {}, trades: [], log: [], days: {}, mode, skips: {} });

  let state = (() => { try { return { ...fresh(), ...(JSON.parse(localStorage.getItem(KEY) || 'null') || {}) }; } catch { return fresh(); } })();
  let plan = null, busy = false, timer = null;
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* storage full */ } };
  const log = (text, kind = 'info') => { state.log.unshift({ t: new Date().toISOString(), text, kind }); state.log.length = Math.min(state.log.length, 120); };

  function claim() {
    try {
      const held = Number(localStorage.getItem(LEASE) || 0);
      if (held && Date.now() - held < 90000 && !root.dataset.owner) return false;
      localStorage.setItem(LEASE, String(Date.now()));
      root.dataset.owner = '1';
      return true;
    } catch { return true; }
  }

  const market = (sym) => (plan && plan.markets.find((m) => m.symbol === sym)) || null;
  const unrealized = (sym, p) => { const m = market(sym); const px = m && Number(m.last) > 0 ? Number(m.last) : p.entry; return p.side * p.qty * (px - p.entry); };
  const equity = () => state.start + state.realized + Object.entries(state.positions).reduce((a, [s, p]) => a + unrealized(s, p), 0);

  function close(sym, px, why) {
    const p = state.positions[sym];
    if (!p || !(px > 0)) return;
    const pnl = p.side * p.qty * (px - p.entry) - p.qty * px * COST(sym);
    state.realized += pnl;
    state.trades.unshift({ sym, side: p.side, qty: p.qty, entry: p.entry, exit: px, pnl, t: new Date().toISOString() });
    state.trades.length = Math.min(state.trades.length, 300);
    const day = new Date().toISOString().slice(0, 10);
    state.days[day] = (state.days[day] || 0) + pnl;
    delete state.positions[sym];
    log(`CLOSE ${p.side > 0 ? 'long' : 'short'} ${sym} @ ${px.toFixed(2)} — ${why} (${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)})`, pnl >= 0 ? 'good' : 'bad');
  }

  function open(sym, side, px, notional) {
    if (!(px > 0) || !(notional > 1)) return;
    const qty = notional / px;
    state.realized -= qty * px * COST(sym);
    state.positions[sym] = { side, qty, entry: px, t: new Date().toISOString() };
    log(`${side > 0 ? 'BUY' : 'SHORT'} ${qty.toFixed(4)} ${sym} @ ${px.toFixed(2)} — price broke ${side > 0 ? 'above' : 'below'} its normal range`, 'good');
  }

  /** Move the paper book to what the tested rules hold right now. */
  function mirror() {
    if (!plan) return 0;
    const eq = equity();
    let n = 0;
    for (const m of plan.markets) {
      const have = state.positions[m.symbol], px = Number(m.last);
      if (m.skipped) {
        const key = `${new Date().toISOString().slice(0, 10)}:${m.symbol}`;
        if (!state.skips[key]) { state.skips[key] = 1; log(`SKIP ${m.symbol} today — this setup looks like past losing days (rated ${Math.round((m.rating || 0) * 100)}% to win).`); }
      }
      const want = m.status === 'open' ? Number(m.position) || 0 : 0;
      if (have && have.side !== want) { close(m.symbol, px > 0 ? px : have.entry, m.status === 'open' ? (want ? 'signal flipped' : 'back inside the range') : 'market closed: flat overnight'); n += 1; }
      if (want && !state.positions[m.symbol] && m.status === 'open') { open(m.symbol, want, px, eq * plan.weight_per_market); n += 1; }
    }
    for (const sym of Object.keys(state.positions)) {
      if (!plan.markets.some((m) => m.symbol === sym)) { close(sym, state.positions[sym].entry, 'market left the book'); n += 1; }
    }
    return n;
  }

  async function loadPlan() {
    const mode = state.mode === 'futures' ? 'futures' : 'stocks';
    const res = await fetch(`/api/fusion/ai/daytrade?mode=${mode}`, { cache: 'no-store', signal: AbortSignal.timeout(55000) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Day Trader plan unavailable');
    plan = data;
    return data;
  }

  async function step() {
    if (busy) return;
    busy = true;
    $('dt-message').textContent = 'Reading today\'s 5-minute bars…';
    try {
      await loadPlan();
      if (state.running && claim()) { mirror(); save(); }
      const open = plan.market_open;
      $('dt-message').textContent = state.running
        ? (open ? 'Running. Checks every minute; the rules decide on the half hour.' : 'Running. The US market is closed, so the book is flat until it opens (9:30am New York).')
        : (open ? 'Market open. Press Start to mirror the rules with paper money.' : 'The US market is closed. Start now and it will trade when it opens.');
    } catch (error) {
      $('dt-message').textContent = `${error.message} — will retry.`;
    } finally {
      busy = false;
      render();
    }
  }

  function renderRecord() {
    const rec = plan && plan.record, b = rec && rec.book;
    if (!b || !b.test) { $('dt-record').innerHTML = ''; return; }
    const cell = (label, value, sub, tone) => `<div class="pf-stat"><span>${esc(label)}</span><strong style="color:${tone || 'inherit'}">${esc(value)}</strong><small>${esc(sub)}</small></div>`;
    const years = rec.calendar_years || [], full = years.filter((y) => !y.partial), losers = full.filter((y) => y.return_pct < 0);
    const max = Math.max(5, ...years.map((y) => Math.abs(y.return_pct)));
    const bars = years.map((y) => {
      const label = `${y.year}${y.partial ? ' (so far)' : ''}: ${pct(y.return_pct)}${y.unseen ? ' (unseen)' : ''}`;
      return `<span class="pf-year${y.return_pct < 0 ? ' down' : ''}" title="${esc(label)}" aria-label="${esc(label)}"><i style="height:${Math.max(2, Math.round((Math.abs(y.return_pct) / max) * 46))}px"></i><small>${String(y.year).slice(2)}</small></span>`;
    }).join('');
    const plainTest = rec.book_plain && rec.book_plain.test, learn = rec.learning || {};
    const lessons = Object.values(learn.lessons || {}).reduce((a, n) => a + n, 0);
    const control = learn.shuffle_control;
    const learnNote = learn.kept
      ? `<p class="pf-hindsight dt-learn"><strong>Learns from its mistakes: ON.</strong> At each day's first signal it rates the setup against ${esc(lessons.toLocaleString())} past trading days and sits out the ones that look like past losers. Each year was tested using only earlier years. On unseen 2022→now data that turned ${esc(pct(plainTest && plainTest.cagr_pct))} a year (worst drop ${esc(pct(plainTest && plainTest.max_dd_pct))}) into ${esc(pct(b.test.cagr_pct))} (worst drop ${esc(pct(b.test.max_dd_pct))})${control ? `, and it beat ${esc(String(Math.min(control.real_beat_shuffled_select, control.real_beat_shuffled_test)))} of ${esc(String(control.runs))} filters trained on scrambled results, so it is not luck from simply trading less` : ''}.</p>`
      : `<p class="pf-hindsight dt-learn"><strong>Learns from its mistakes: OFF here.</strong> The same filter was tested for ${esc(plan.mode === 'futures' ? 'futures' : 'this book')} and did not help on 2018–21 (Sharpe ${esc(String(learn.select_window_sharpe ? learn.select_window_sharpe.filtered : '—'))} with it, ${esc(String(learn.select_window_sharpe ? learn.select_window_sharpe.plain : '—'))} without), so it stays off rather than being switched on for show.</p>`;
    const honesty = plan.mode === 'futures'
      ? `<p class="pf-hindsight"><strong>Honesty check.</strong> ${esc(rec.note || '')} Four micro contracts were tested; Russell (M2K) and Dow (MYM) lost money and are left out. Checked against the real futures over the last 35 sessions, the fund's daily results matched 0.98–0.99. Futures are leveraged: one contract controls far more than its margin, and losses can exceed what you put in.</p>`
      : `<p class="pf-hindsight"><strong>Honesty check.</strong> The same rules were tested on 12 stocks and funds. They lost money on ${esc(String(plan.stock_candidates_losing))} of them after costs, and all 12 together made ${esc(pct(rec.all_candidates_book && rec.all_candidates_book.test && rec.all_candidates_book.test.cagr_pct))} a year since 2022. This book holds only the ${esc(String(rec.symbols.length))} that worked in 2016–21 (${esc(rec.symbols.join(', '))}).</p>`;
    $('dt-record').innerHTML =
      cell('Simulated return / year (2022→now, unseen)', pct(b.test.cagr_pct), `2016–21: ${pct(b.select.cagr_pct)} (rules chosen here)`, '#a8f0c6') +
      cell('Sharpe ratio', Number(b.test.sharpe).toFixed(2), `2016–21: ${Number(b.select.sharpe).toFixed(2)}`) +
      cell('Worst drop', pct(b.test.max_dd_pct), 'on unseen data', '#ffb3a7') +
      cell('Winning days', `${Number(b.test.win_days_pct).toFixed(0)}%`, 'most days lose a little; a few win big') +
      `<div class="pf-years"><p><strong>Year by year (simulated).</strong> Up in ${full.length - losers.length} of ${full.length} full years.${losers.length ? ` Losing years: ${esc(losers.map((y) => `${y.year} ${pct(y.return_pct)}`).join(', '))}.` : ''} No strategy can promise a profit every year.</p><div class="pf-year-bars" role="img" aria-label="Simulated return each calendar year">${bars}</div></div>` +
      learnNote + honesty;
  }

  function renderMarkets() {
    if (!plan) return;
    $('dt-markets').innerHTML = plan.markets.map((m) => {
      if (m.status !== 'open') {
        const note = m.contract_notional ? ` · 1 contract ≈ $${Math.round(m.contract_notional).toLocaleString('en-US')} (${m.proxy} prices)` : '';
        return `<div class="dt-row idle"><span class="pf-sym">${esc(m.symbol)}</span><span class="dt-state flat">${m.status === 'closed' ? 'CLOSED' : 'NO DATA'}</span><span class="dt-detail">${m.last ? `last ${esc(Number(m.last).toFixed(2))} · flat overnight${esc(note)}` : 'price feed unavailable'}</span></div>`;
      }
      const word = m.skipped ? 'SKIP' : m.position > 0 ? 'LONG' : m.position < 0 ? 'SHORT' : 'FLAT', cls = m.skipped ? 'flat' : m.position > 0 ? 'up' : m.position < 0 ? 'down' : 'flat';
      const size = m.contract_notional ? ` · 1 contract ≈ $${Math.round(m.contract_notional).toLocaleString('en-US')}; your share = ${((equity() * plan.weight_per_market) / m.contract_notional).toFixed(2)} contracts` : '';
      const span = Math.max(1e-9, m.upper - m.lower), at = Math.min(100, Math.max(0, ((m.last - m.lower) / span) * 100));
      return `<div class="dt-row"><span class="pf-sym">${esc(m.symbol)}</span><span class="dt-state ${cls}">${word}</span>` +
        `<span class="dt-band" title="Price against today's normal range"><i style="left:${at.toFixed(1)}%"></i></span>` +
        `<span class="dt-detail">${esc(Number(m.last).toFixed(2))} · range ${esc(Number(m.lower).toFixed(2))}–${esc(Number(m.upper).toFixed(2))}${m.skipped ? ` · sitting today out: rated ${Math.round((m.rating || 0) * 100)}% to win` : ''}${m.pending != null ? ` · ${m.pending > 0 ? 'buying' : m.pending < 0 ? 'shorting' : 'closing'} at the next bar` : ''}${esc(size)}</span></div>`;
    }).join('');
  }

  function renderBook() {
    const eq = equity(), today = new Date().toISOString().slice(0, 10);
    const metric = (label, value) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
    $('dt-metrics').innerHTML = metric('Equity', money(eq)) + metric('Total P&L', `${money(eq - state.start)} (${pct((eq / state.start - 1) * 100, 2)})`) +
      metric('Today (closed trades)', money(state.days[today] || 0)) + metric('Trades', String(state.trades.length));
    const rows = Object.entries(state.positions).map(([sym, p]) => {
      const u = unrealized(sym, p);
      return `<tr><td>${esc(sym)}</td><td>${p.side > 0 ? 'Long' : 'Short'}</td><td>${esc(p.qty.toFixed(4))}</td><td>${esc(p.entry.toFixed(2))}</td><td style="color:${u >= 0 ? '#a8f0c6' : '#ffb3a7'}">${esc(money(u))}</td></tr>`;
    }).join('');
    $('dt-positions').innerHTML = rows ? `<table><thead><tr><th>Market</th><th>Side</th><th>Units</th><th>Entry</th><th>P&amp;L</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="caption">No open paper positions. The Day Trader is flat between signals and overnight.</p>';
    $('dt-log').innerHTML = state.log.slice(0, 50).map((l) => `<li class="${esc(l.kind)}"><time>${esc(new Date(l.t).toLocaleString())}</time> ${esc(l.text)}</li>`).join('') || '<li>No trades yet.</li>';
  }

  function renderTested() {
    const rec = plan && plan.record;
    if (!rec) return;
    const rows = (plan.candidates || []).map((c) => `<tr><td>${esc(c.symbol)}${c.chosen ? ' ✓' : ''}</td><td>${esc(pct(c.select && c.select.cagr_pct))}</td><td>${esc(pct(c.test && c.test.cagr_pct))}</td></tr>`).join('');
    $('dt-tested').innerHTML = `<p class="caption">${esc(rec.method)} Markets were chosen by this rule: ${esc(rec.selection_rule)}; then judged on 2022 onward, which the choice never saw. Data through ${esc(rec.data_through)}.</p>` +
      `<div class="table-scroll"><table><thead><tr><th>Market (✓ = in the book)</th><th>2016–21 / yr</th><th>2022→now / yr (unseen)</th></tr></thead><tbody>${rows}</tbody></table></div>` +
      `<p class="caption"><strong>Simpler day-trading rules that were tested first and lost money:</strong></p><ul class="caption">${(rec.rejected_rules || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` +
      `<ul class="caption">${(rec.caveats || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`;
  }

  const INTRO = {
    stocks: 'Every 30 minutes the Day Trader checks five fast-moving US markets. It goes long when price breaks above its normal range for that time of day, short when it breaks below, and closes everything before the bell.',
    futures: 'Futures mode trades micro Nasdaq (MNQ) and micro S&P 500 (MES) contracts with the same rules, during regular US hours only, and is flat by the close. Futures cost about half as much to trade, which is why this method tests better on them.',
  };
  function renderMode() {
    const mode = state.mode === 'futures' ? 'futures' : 'stocks';
    $('dt-mode').querySelectorAll('[data-dt-mode]').forEach((b) => { const on = b.dataset.dtMode === mode; b.classList.toggle('primary', on); b.classList.toggle('secondary', !on); b.setAttribute('aria-pressed', String(on)); });
    $('dt-intro').textContent = INTRO[mode];
    const lev = plan && plan.mode === 'futures' ? plan.markets.filter((m) => m.contract_notional) : [];
    $('dt-futures-note').hidden = mode !== 'futures';
    if (mode === 'futures') {
      const need = lev.reduce((a, m) => a + m.contract_notional, 0);
      $('dt-futures-note').textContent = lev.length
        ? `Sizing, honestly: one contract each of ${lev.map((m) => m.symbol).join(' and ')} controls about $${Math.round(need).toLocaleString('en-US')}. The test used no leverage, so an account of about that size trades one of each; a smaller account trading whole contracts is leveraged and its swings (and losses) grow in proportion. The paper book here uses fractional contracts so it matches the tested, unleveraged record.`
        : 'Contract sizes appear once index prices load.';
    }
  }

  function render() {
    renderMode();
    $('dt-status').textContent = state.running ? (plan && plan.market_open ? 'Trading' : 'Waiting for the open') : 'Idle';
    $('dt-status').classList.toggle('good', state.running);
    $('dt-start').disabled = state.running;
    $('dt-pause').disabled = !state.running;
    $('dt-capital').disabled = state.running || state.trades.length > 0 || Object.keys(state.positions).length > 0;
    renderRecord(); renderMarkets(); renderBook(); renderTested();
  }

  $('dt-start').addEventListener('click', () => {
    if (!state.trades.length && !Object.keys(state.positions).length) {
      const cap = Math.max(100, Number($('dt-capital').value) || 10000);
      state = { ...fresh(cap, state.mode), log: state.log };
    }
    state.running = true;
    log('Day Trader started with paper money.', 'good');
    save();
    step();
  });
  $('dt-pause').addEventListener('click', () => {
    state.running = false;
    for (const sym of Object.keys(state.positions)) { const m = market(sym); close(sym, m && Number(m.last) > 0 ? Number(m.last) : state.positions[sym].entry, 'paused: closing so nothing is left unwatched'); }
    log('Paused — all paper positions closed.');
    save(); render();
  });
  $('dt-refresh').addEventListener('click', () => step());
  $('dt-reset').addEventListener('click', () => {
    state = fresh(Math.max(100, Number($('dt-capital').value) || 10000), state.mode);
    save(); render();
    $('dt-message').textContent = 'Paper book reset.';
  });
  $('dt-mode').addEventListener('click', (event) => {
    const button = event.target.closest('[data-dt-mode]');
    if (!button || button.dataset.dtMode === state.mode || busy) return;
    for (const sym of Object.keys(state.positions)) { const m = market(sym); close(sym, m && Number(m.last) > 0 ? Number(m.last) : state.positions[sym].entry, 'switching mode'); }
    state.mode = button.dataset.dtMode;
    plan = null;
    log(`Mode set to ${state.mode === 'futures' ? 'futures (MNQ, MES)' : 'stocks'}.`);
    save(); render(); step();
  });
  document.addEventListener('loki:currency', render);
  document.addEventListener('loki:view', (e) => { if (e.detail && e.detail.view === 'ai-trading' && !plan) step(); });

  $('dt-capital').value = state.start;
  render();
  const visible = () => { const view = document.getElementById('ai-trading'); return view && !view.hidden && !document.hidden; };
  timer = setInterval(() => { if (state.running || visible()) step(); }, CHECK_MS);
  if (visible() || state.running) step();
  window.addEventListener('beforeunload', () => clearInterval(timer));
})();
