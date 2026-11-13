/* DAY TRADER AI — paper book that mirrors the tested intraday rules.
 *
 * The server (/api/fusion/ai/daytrade) replays today's 5-minute bars through
 * the same code the 10-year test used and says, per market, whether the rules
 * are long, short or flat right now. This script keeps a paper book in the
 * browser and moves it to match: equal capital per market, in and out the same
 * day, everything closed before the bell. Paper money only; no orders leave
 * the browser.
 *
 * Contents: helpers · paper book · following the rules · server plan ·
 * rendering (record, markets, book, test details, mode) · controls · timers.
 */
'use strict';
(() => {
  const root = document.getElementById('day-trader');
  if (!root) return;

  // --- Helpers ---------------------------------------------------------------
  const KEY = 'loki_day_trader_v1';
  const LEASE = 'loki_day_trader_lease'; // only one open tab trades the book
  const CHECK_MS = 60 * 1000;
  const HALF_HOUR_MS = 30 * 60 * 1000;
  const AFTER_DECISION_MS = [12000, 40000]; // the rules decide on the half hour: look again just after it
  const FALLBACK_COST_BPS = 2; // per side; used only if the server did not send a market's tested cost
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (v) => (window.LokiCurrency ? window.LokiCurrency.format(v) : (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const rounded = (v, d) => (Math.sign(v) * Math.round(Math.abs(v) * 10 ** d + 1e-9)) / 10 ** d; // halves round away from zero
  const pct = (v, d = 1) => { if (v == null || !Number.isFinite(Number(v))) return '—'; const r = rounded(Number(v), d); return `${r >= 0 ? '+' : ''}${r.toFixed(d)}%`; };
  const num = (v, d = 2) => (v != null && Number.isFinite(Number(v)) ? rounded(Number(v), d).toFixed(d) : '—');
  const today = () => new Date().toISOString().slice(0, 10);
  const fresh = (cash = 10000, mode = 'stocks') => ({ start: cash, realized: 0, running: false, positions: {}, trades: [], log: [], days: {}, mode, skips: {}, fills: { n: 0, seconds: 0 } });

  let state = (() => { try { return { ...fresh(), ...(JSON.parse(localStorage.getItem(KEY) || 'null') || {}) }; } catch { return fresh(); } })();
  let plan = null, busy = false, timer = null, aligned = null;
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* storage full */ } };
  const emit = (name, detail) => { try { if (window.Loki && window.Loki.emit) window.Loki.emit(name, detail); } catch { /* the map is optional */ } };
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

  // --- Paper book --------------------------------------------------------------
  const market = (sym) => (plan && plan.markets.find((m) => m.symbol === sym)) || null;
  const costOf = (sym) => { const m = market(sym); return (m && Number.isFinite(Number(m.cost_bps)) ? Number(m.cost_bps) : FALLBACK_COST_BPS) / 10000; };
  /** The latest trade the feed has; a paper order fills here, not at an earlier bar's price. */
  const priceOf = (m) => (m && Number(m.now_price) > 0 ? Number(m.now_price) : m && Number(m.last) > 0 ? Number(m.last) : 0);
  const unrealized = (sym, p) => { const px = priceOf(market(sym)) || p.entry; return p.side * p.qty * (px - p.entry); };
  const equity = () => state.start + state.realized + Object.entries(state.positions).reduce((a, [s, p]) => a + unrealized(s, p), 0);

  /** Seconds between the rules' signal and this paper fill (server clock), or null when unknown. */
  function fillLag(m) {
    const at = Date.parse(m && m.signal_at), served = Date.parse(plan && plan.served_at);
    return Number.isFinite(at) && Number.isFinite(served) && served >= at ? (served - at) / 1000 : null;
  }
  function lagNote(lag) {
    if (lag == null) return '';
    if (lag <= 600) { state.fills.n += 1; state.fills.seconds += lag; } // later than that is joining a trade already under way
    return lag < 90 ? `, ${Math.round(lag)} s after the signal` : lag < 3600 ? `, ${Math.round(lag / 60)} min after the signal` : '';
  }

  function close(sym, px, why, lag = null) {
    const p = state.positions[sym];
    if (!p || !(px > 0)) return;
    const pnl = p.side * p.qty * (px - p.entry) - p.qty * px * costOf(sym);
    state.realized += pnl;
    state.trades.unshift({ sym, side: p.side, qty: p.qty, entry: p.entry, exit: px, pnl, t: new Date().toISOString() });
    state.trades.length = Math.min(state.trades.length, 300);
    state.days[today()] = (state.days[today()] || 0) + pnl;
    delete state.positions[sym];
    emit('daytrader:trade', { action: 'closed', symbol: sym, pnl });
    log(`CLOSE ${p.side > 0 ? 'long' : 'short'} ${sym} @ ${px.toFixed(2)} — ${why}${lagNote(lag)} (${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)})`, pnl >= 0 ? 'good' : 'bad');
  }

  function open(sym, side, px, notional, lag = null) {
    if (!(px > 0) || !(notional > 1)) return;
    const qty = notional / px;
    state.realized -= qty * px * costOf(sym);
    state.positions[sym] = { side, qty, entry: px, t: new Date().toISOString() };
    emit('daytrader:trade', { action: side > 0 ? 'bought' : 'shorted', symbol: sym });
    log(`${side > 0 ? 'BUY' : 'SHORT'} ${qty.toFixed(4)} ${sym} @ ${px.toFixed(2)} — price broke ${side > 0 ? 'above' : 'below'} its normal range${lagNote(lag)}`, 'good');
  }

  const closeAll = (why) => { for (const sym of Object.keys(state.positions)) close(sym, priceOf(market(sym)) || state.positions[sym].entry, why); };

  // --- Following the rules -------------------------------------------------------
  /** Move the paper book to what the tested rules hold (or are about to take) right now. */
  function mirror() {
    if (!plan) return 0;
    const eq = equity();
    let n = 0;
    for (const m of plan.markets) {
      const have = state.positions[m.symbol], px = priceOf(m), live = m.status === 'open';
      if (m.skipped) {
        const key = `${today()}:${m.symbol}`;
        if (!state.skips[key]) { state.skips[key] = 1; log(`SKIP ${m.symbol} today — this setup looks like past losing days (rated ${Math.round((m.rating || 0) * 100)}% to win).`); }
      }
      // A decision on the bar that just closed is acted on now, not when the next bar has printed.
      const want = !live ? 0 : m.pending != null ? Number(m.pending) : Number(m.position) || 0;
      const lag = fillLag(m);
      if (have && have.side !== want) { close(m.symbol, px || have.entry, live ? (want ? 'signal flipped' : 'back inside the range') : 'market closed: flat overnight', live ? lag : null); n += 1; }
      if (want && live && !state.positions[m.symbol]) { open(m.symbol, want, px, eq * plan.weight_per_market, lag); n += 1; }
    }
    for (const sym of Object.keys(state.positions)) {
      if (!plan.markets.some((m) => m.symbol === sym)) { close(sym, state.positions[sym].entry, 'market left the book'); n += 1; }
    }
    return n;
  }

  // --- Server plan -----------------------------------------------------------------
  async function loadPlan() {
    const mode = state.mode === 'futures' ? 'futures' : 'stocks';
    const res = await fetch(`/api/fusion/ai/daytrade?mode=${mode}`, { cache: 'no-store', signal: AbortSignal.timeout(55000) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Day Trader plan unavailable');
    plan = data;
    const count = (test) => data.markets.filter(test).length;
    emit('daytrader:plan', { mode: data.mode || 'stocks', open: data.market_open, long: count((m) => m.position > 0), short: count((m) => m.position < 0), skipped: count((m) => m.skipped) });
    return data;
  }

  async function step() {
    if (busy) return;
    busy = true;
    $('dt-message').textContent = 'Reading today\'s 5-minute bars…';
    try {
      await loadPlan();
      if (state.running && claim()) { mirror(); save(); }
      const live = plan.market_open;
      $('dt-message').textContent = state.running
        ? (live ? 'Running. Checks every minute, and again just after each half hour, when the rules decide.' : 'Running. The US market is closed, so the book is flat until it opens (9:30am New York).')
        : (live ? 'Market open. Press Start to mirror the rules with paper money.' : 'The US market is closed. Start now and it will trade when it opens.');
    } catch (error) {
      $('dt-message').textContent = `${error.message} — will retry.`;
    } finally {
      busy = false;
      render();
    }
  }

  // --- Rendering: the tested record --------------------------------------------------
  const cell = (label, value, sub, tone) => `<div class="pf-stat"><span>${esc(label)}</span><strong style="color:${tone || 'inherit'}">${esc(value)}</strong><small>${esc(sub)}</small></div>`;
  const yearBars = (years) => {
    const max = Math.max(5, ...years.map((y) => Math.abs(y.return_pct)));
    return years.map((y) => {
      const label = `${y.year}${y.partial ? ' (so far)' : ''}: ${pct(y.return_pct)}${y.unseen ? ' (unseen)' : ''}`;
      return `<span class="pf-year${y.return_pct < 0 ? ' down' : ''}" title="${esc(label)}" aria-label="${esc(label)}"><i style="height:${Math.max(2, Math.round((Math.abs(y.return_pct) / max) * 46))}px"></i><small>${String(y.year).slice(2)}</small></span>`;
    }).join('');
  };
  const upYears = (years) => { const full = (years || []).filter((y) => !y.partial); return { full, losers: full.filter((y) => y.return_pct < 0) }; };

  function renderRecord() {
    const rec = plan && plan.record, b = rec && rec.book;
    if (!b || !b.test) { $('dt-record').innerHTML = ''; return; }
    const late = (rec.book_late && rec.book_late.test) || {}, wait = rec.late_fill_minutes || 5;
    const years = rec.calendar_years || [], y = upYears(years), yLate = upYears(rec.calendar_years_late);
    $('dt-record').innerHTML =
      cell('Simulated return / year (2022→now, unseen)', pct(b.test.cagr_pct), `filled ${wait} min late: ${pct(late.cagr_pct)} · 2016–21: ${pct(b.select.cagr_pct)}`, '#a8f0c6') +
      cell('Sharpe ratio', num(b.test.sharpe), `filled ${wait} min late: ${num(late.sharpe)} · 2016–21: ${num(b.select.sharpe)}`) +
      cell('Worst drop', pct(b.test.max_dd_pct), `filled ${wait} min late: ${pct(late.max_dd_pct)}`, '#ffb3a7') +
      cell('Winning days', `${num(b.test.win_days_pct, 0)}%`, 'most days lose a little; a few win big') +
      `<div class="pf-years"><p><strong>Year by year (simulated, instant fills).</strong> Up in ${y.full.length - y.losers.length} of ${y.full.length} full years.${y.losers.length ? ` Losing years: ${esc(y.losers.map((v) => `${v.year} ${pct(v.return_pct)}`).join(', '))}.` : ''}${yLate.full.length ? ` Filled ${wait} minutes late: up in ${yLate.full.length - yLate.losers.length} of ${yLate.full.length}.` : ''} No strategy can promise a profit every year.</p><div class="pf-year-bars" role="img" aria-label="Simulated return each calendar year">${yearBars(years)}</div></div>` +
      forwardNote(plan.forward) + speedNote(rec, b.test, late, wait) + learnNote(rec, b.test) + honestyNote(rec);
  }

  // Sessions after the tested record was frozen: the only results the rules could not have been fitted to.
  function forwardNote(f) {
    if (!f || !f.days) return '';
    const max = Math.max(0.2, ...f.daily.map((d) => Math.abs(d.return_pct)));
    const bars = f.daily.map((d) => `<span class="pf-year${d.return_pct < 0 ? ' down' : ''}" title="${esc(`${d.day}: ${pct(d.return_pct, 2)}`)}" aria-label="${esc(`${d.day}: ${pct(d.return_pct, 2)}`)}"><i style="height:${Math.max(2, Math.round((Math.abs(d.return_pct) / max) * 34))}px"></i><small>${esc(d.day.slice(8))}</small></span>`).join('');
    return `<div class="pf-years dt-forward"><p><strong>Since the record was frozen (${esc(f.since)}).</strong> ${esc(String(f.days))} completed session${f.days === 1 ? '' : 's'} the rules had never seen: ${esc(pct(f.return_pct, 2))} in total, ${esc(String(f.winning_days))} up and ${esc(String(f.losing_days))} down, worst drop ${esc(pct(f.worst_drop_pct, 2))}. ${f.days < 60 ? 'Far too few days to judge; it is shown so you can watch it build.' : ''} Simulated with instant fills, not real trades.</p><div class="pf-year-bars dt-forward-bars" role="img" aria-label="Result of each session since the record was frozen">${bars}</div></div>`;
  }

  // How much of the result depends on filling quickly.
  function speedNote(rec, test, late, wait) {
    const mine = state.fills && state.fills.n ? Math.round(state.fills.seconds / state.fills.n) : null;
    const steps = [{ minutes: 0, test }, ...(rec.fill_speed || []), { minutes: wait, test: late }].filter((s) => s.test && s.test.cagr_pct != null);
    const curve = steps.map((s) => `${s.minutes ? `${s.minutes} min late` : 'instant'} ${pct(s.test.cagr_pct)}`).join(' · ');
    return `<p class="pf-hindsight dt-speed"><strong>Speed check.</strong> This method lives on fast fills. The headline assumes every order fills the moment the rules decide. The same unseen years, by how late each order fills: ${esc(curve)} a year. The paper book here fills at the latest price as soon as it sees a signal${mine != null ? `: ${esc(String(mine))} seconds after it on average so far (${esc(String(state.fills.n))} fills)` : ', usually within a minute'}. Expect it to land between the first two figures, not on the headline.</p>`;
  }

  function learnNote(rec, test) {
    const learn = rec.learning || {}, plain = (rec.book_plain && rec.book_plain.test) || {}, sharpe = learn.select_window_sharpe || {};
    if (!learn.kept) {
      return `<p class="pf-hindsight dt-learn"><strong>Learns from its mistakes: OFF here.</strong> The same filter was tested for ${esc(plan.mode === 'futures' ? 'futures' : 'this book')} and did not help on 2018–21 (Sharpe ${esc(num(sharpe.filtered))} with it, ${esc(num(sharpe.plain))} without), so it stays off rather than being switched on for show.</p>`;
    }
    const lessons = Object.values(learn.lessons || {}).reduce((a, n) => a + n, 0), control = learn.shuffle_control;
    const luck = control ? `, and it beat ${esc(String(Math.min(control.real_beat_shuffled_select, control.real_beat_shuffled_test)))} of ${esc(String(control.runs))} filters trained on scrambled results, so it is not luck from simply trading less` : '';
    return `<p class="pf-hindsight dt-learn"><strong>Learns from its mistakes: ON.</strong> At each day's first signal it rates the setup against ${esc(lessons.toLocaleString())} past trading days and sits out the ones that look like past losers. Each year was tested using only earlier years. On unseen 2022→now data that turned ${esc(pct(plain.cagr_pct))} a year (worst drop ${esc(pct(plain.max_dd_pct))}) into ${esc(pct(test.cagr_pct))} (worst drop ${esc(pct(test.max_dd_pct))})${luck}.</p>`;
  }

  function honestyNote(rec) {
    if (plan.mode === 'futures') {
      const tested = plan.candidates || [], out = tested.filter((c) => !c.chosen);
      const dropped = out.length ? ` ${esc(String(tested.length))} micro contracts were tested; ${esc(out.map((c) => `${c.name || c.symbol} (${c.symbol})`).join(' and '))} failed the 2016–21 test and ${out.length === 1 ? 'is' : 'are'} left out.` : '';
      return `<p class="pf-hindsight"><strong>Honesty check.</strong> ${esc(rec.note || '')}${dropped} Futures are leveraged: one contract controls far more than its margin, and losses can exceed what you put in.</p>`;
    }
    const all = rec.all_candidates_book || {};
    return `<p class="pf-hindsight"><strong>Honesty check.</strong> The same rules were tested on ${esc(String(all.count))} stocks and funds. On unseen data they lost money on ${esc(String(all.losing_on_unseen))} of them after costs, and all ${esc(String(all.count))} together made ${esc(pct(all.test && all.test.cagr_pct))} a year since 2022. This book holds only the ${esc(String(rec.symbols.length))} that passed on 2016–21 with instant fills and with late ones (${esc(rec.symbols.join(', '))}).</p>`;
  }

  // --- Rendering: markets right now ----------------------------------------------------
  function renderMarkets() {
    if (!plan) return;
    const contract = (m) => (m.contract_notional ? `1 contract ≈ $${Math.round(m.contract_notional).toLocaleString('en-US')}` : '');
    $('dt-markets').innerHTML = plan.markets.map((m) => {
      const row = (cls, word, detail, band = '') => `<div class="dt-row${cls === 'idle' ? ' idle' : ''}"><span class="pf-sym">${esc(m.symbol)}</span><span class="dt-state ${cls === 'idle' ? 'flat' : cls}">${word}</span>${band}<span class="dt-detail">${detail}</span></div>`;
      if (m.status !== 'open') {
        const size = contract(m) ? ` · ${esc(contract(m))} (${esc(m.proxy)} prices)` : '';
        return row('idle', m.status === 'closed' ? 'CLOSED' : 'NO DATA', m.last ? `last ${esc(num(m.last))} · flat overnight${size}` : 'price feed unavailable');
      }
      if (m.waiting) return row('flat', 'OPEN', `${esc(num(m.last))} · waiting for the first 5-minute bar`);
      const word = m.skipped ? 'SKIP' : m.position > 0 ? 'LONG' : m.position < 0 ? 'SHORT' : 'FLAT', cls = m.skipped ? 'flat' : m.position > 0 ? 'up' : m.position < 0 ? 'down' : 'flat';
      const size = contract(m) ? ` · ${esc(contract(m))}; your share = ${esc(((equity() * plan.weight_per_market) / m.contract_notional).toFixed(2))} contracts` : '';
      const span = Math.max(1e-9, m.upper - m.lower), at = Math.min(100, Math.max(0, ((m.last - m.lower) / span) * 100));
      const skip = m.skipped ? ` · sitting today out: rated ${Math.round((m.rating || 0) * 100)}% to win` : '';
      const next = m.pending != null ? ` · ${m.pending > 0 ? 'buying' : m.pending < 0 ? 'shorting' : 'closing'} now` : '';
      return row(cls, word, `${esc(num(m.last))} · range ${esc(num(m.lower))}–${esc(num(m.upper))}${skip}${next}${size}`,
        `<span class="dt-band" title="Price against today's normal range"><i style="left:${at.toFixed(1)}%"></i></span>`);
    }).join('');
  }

  // --- Rendering: the paper book ---------------------------------------------------------
  function renderBook() {
    const eq = equity();
    const metric = (label, value) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
    $('dt-metrics').innerHTML = metric('Equity', money(eq)) + metric('Total P&L', `${money(eq - state.start)} (${pct((eq / state.start - 1) * 100, 2)})`) +
      metric('Today (closed trades)', money(state.days[today()] || 0)) + metric('Trades', String(state.trades.length));
    const rows = Object.entries(state.positions).map(([sym, p]) => {
      const u = unrealized(sym, p);
      return `<tr><td>${esc(sym)}</td><td>${p.side > 0 ? 'Long' : 'Short'}</td><td>${esc(p.qty.toFixed(4))}</td><td>${esc(p.entry.toFixed(2))}</td><td style="color:${u >= 0 ? '#a8f0c6' : '#ffb3a7'}">${esc(money(u))}</td></tr>`;
    }).join('');
    $('dt-positions').innerHTML = rows ? `<table><thead><tr><th>Market</th><th>Side</th><th>Units</th><th>Entry</th><th>P&amp;L</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="caption">No open paper positions. The Day Trader is flat between signals and overnight.</p>';
    $('dt-log').innerHTML = state.log.slice(0, 50).map((l) => `<li class="${esc(l.kind)}"><time>${esc(new Date(l.t).toLocaleString())}</time> ${esc(l.text)}</li>`).join('') || '<li>No trades yet.</li>';
  }

  // --- Rendering: how it was tested --------------------------------------------------------
  function renderTested() {
    const rec = plan && plan.record;
    if (!rec) return;
    const wait = rec.late_fill_minutes || 5, yr = (w) => esc(pct(w && w.cagr_pct));
    const rows = [...(plan.candidates || [])].sort((a, b) => Number(b.chosen) - Number(a.chosen))
      .map((c) => `<tr><td>${esc(c.symbol)}${c.chosen ? ' ✓' : ''}</td><td>${yr(c.select)}</td><td>${yr(c.select_late)}</td><td>${yr(c.test)}</td><td>${yr(c.test_late)}</td></tr>`).join('');
    $('dt-tested').innerHTML = `<p class="caption">${esc(rec.method)} Markets were chosen by this rule: ${esc(rec.selection_rule)}; then judged on 2022 onward, which the choice never saw. Data through ${esc(rec.data_through)}.</p>` +
      `<div class="table-scroll"><table><thead><tr><th>Market (✓ = in the book)</th><th>2016–21 / yr</th><th>2016–21, ${esc(String(wait))} min late</th><th>2022→now / yr (unseen)</th><th>2022→now, ${esc(String(wait))} min late</th></tr></thead><tbody>${rows}</tbody></table></div>` +
      `<p class="caption"><strong>Simpler day-trading rules that were tested first and lost money:</strong></p><ul class="caption">${(rec.rejected_rules || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` +
      `<ul class="caption">${(rec.caveats || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`;
  }

  // --- Rendering: stocks or futures ----------------------------------------------------------
  function renderMode() {
    const mode = state.mode === 'futures' ? 'futures' : 'stocks';
    const loaded = plan && plan.mode === mode ? plan.markets : null;
    $('dt-mode').querySelectorAll('[data-dt-mode]').forEach((b) => { const on = b.dataset.dtMode === mode; b.classList.toggle('primary', on); b.classList.toggle('secondary', !on); b.setAttribute('aria-pressed', String(on)); });
    $('dt-intro').textContent = mode === 'futures'
      ? `Futures mode trades micro index contracts${loaded ? ` (${loaded.map((m) => m.symbol).join(', ')})` : ''} with the same rules, during regular US hours only, and is flat by the close. Futures cost about half as much to trade, which is why this method tests better on them.`
      : `Every 30 minutes the Day Trader checks ${loaded ? loaded.length : 'a short list of'} fast-moving US markets. It goes long when price breaks above its normal range for that time of day, short when it breaks below, and closes everything before the bell.`;
    $('dt-futures-note').hidden = mode !== 'futures';
    if (mode !== 'futures') return;
    const sized = (loaded || []).filter((m) => m.contract_notional), need = sized.reduce((a, m) => a + m.contract_notional, 0);
    $('dt-futures-note').textContent = sized.length
      ? `Sizing, honestly: one contract each of ${sized.map((m) => m.symbol).join(' and ')} controls about $${Math.round(need).toLocaleString('en-US')}. The test used no leverage, so an account of about that size trades one of each; a smaller account trading whole contracts is leveraged and its swings (and losses) grow in proportion. The paper book here uses fractional contracts so it matches the tested, unleveraged record.`
      : 'Contract sizes appear once index prices load.';
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

  // --- Controls ----------------------------------------------------------------------------------
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
    closeAll('paused: closing so nothing is left unwatched');
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
    closeAll('switching mode');
    state.mode = button.dataset.dtMode;
    plan = null;
    log(`Mode set to ${state.mode === 'futures' ? 'futures' : 'stocks and funds'}.`);
    save(); render(); step();
  });
  document.addEventListener('loki:currency', render);
  document.addEventListener('loki:view', (e) => { if (e.detail && e.detail.view === 'ai-trading' && !plan) step(); });

  // --- Timers ---------------------------------------------------------------------------------------
  const visible = () => { const view = document.getElementById('ai-trading'); return view && !view.hidden && !document.hidden; };
  const wanted = () => state.running || visible();
  /** Milliseconds until the next look just after a half hour (New York half hours are UTC half hours). */
  function untilAligned(now = Date.now()) {
    const base = Math.floor(now / HALF_HOUR_MS) * HALF_HOUR_MS;
    const marks = [...AFTER_DECISION_MS.map((ms) => base + ms), base + HALF_HOUR_MS + AFTER_DECISION_MS[0]];
    return marks.find((t) => t > now + 500) - now;
  }
  function scheduleAligned() {
    aligned = setTimeout(() => { if (wanted()) step(); scheduleAligned(); }, untilAligned());
  }

  $('dt-capital').value = state.start;
  render();
  timer = setInterval(() => { if (wanted()) step(); }, CHECK_MS);
  scheduleAligned();
  if (wanted()) step();
  window.addEventListener('beforeunload', () => { clearInterval(timer); clearTimeout(aligned); });
})();
