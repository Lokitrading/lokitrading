/* COMMAND CENTER — every engine on one glass dashboard.
 *
 * Draggable, collapsible widgets (order and state remembered on this device):
 * telemetry, venue health, the continuous Monte Carlo risk engine, the
 * consensus gate, evolution, a live order-book heatmap / depth surface with
 * iceberg detection, the natural-language strategy builder, the central-bank
 * newswire, verified scorecards and strategy commitments, trade-signal relays
 * and voice control. Hotkeys work everywhere (not while typing):
 *   Shift+P pause every bot · Shift+F flatten the paper account · Shift+K kill
 *   switch · Shift+V voice · Shift+C co-pilot · Shift+M this page (? opens the cheat sheet)
 * Destructive actions always ask for confirmation, by key or by voice.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  const root = document.getElementById('command');
  if (!Loki || !root) return;
  const F = Loki.fmt;
  const $ = (id) => document.getElementById(id);
  const esc = F.esc;
  const grid = $('cc-grid');
  const ORDER_KEY = 'loki_cc_order_v1', FOLD_KEY = 'loki_cc_folded_v1', HOOK_KEY = 'loki_cc_signal_hook_v1';
  const visible = () => !root.hidden && !document.hidden;
  const pct = (x, d = 1) => `${(x * 100).toFixed(d)}%`;
  const msg = (id, text, bad = false) => { const el = $(id); if (el) { el.textContent = text; el.classList.toggle('bad', bad); } };
  const spark = (vals, w = 220, h = 40, cls = '') => {
    const v = vals.filter(Number.isFinite); if (v.length < 2) return '';
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    return `<svg class="cc-spark ${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(h - 2 - (x - lo) / span * (h - 4)).toFixed(1)}`).join(' ')}"/></svg>`;
  };

  /* ------------------------------------------------ drag, fold, persist */
  function restoreLayout() {
    const order = Loki.store.get(ORDER_KEY, []), folded = Loki.store.get(FOLD_KEY, []);
    for (const id of order) { const w = grid.querySelector(`[data-w="${id}"]`); if (w) grid.appendChild(w); }
    for (const id of folded) { const w = grid.querySelector(`[data-w="${id}"]`); if (w) w.classList.add('folded'); }
    grid.querySelectorAll('.cc-widget').forEach((w) => { const b = w.querySelector('.cc-fold'); if (b) b.setAttribute('aria-expanded', String(!w.classList.contains('folded'))); });
    syncArrows();
  }
  const syncArrows = () => {
    const all = [...grid.querySelectorAll('.cc-widget')];
    all.forEach((w, k) => { w.querySelector('.cc-up').disabled = k === 0; w.querySelector('.cc-down').disabled = k === all.length - 1; });
  };
  const saveLayout = () => {
    syncArrows();
    Loki.store.set(ORDER_KEY, [...grid.querySelectorAll('.cc-widget')].map((w) => w.dataset.w));
    Loki.store.set(FOLD_KEY, [...grid.querySelectorAll('.cc-widget.folded')].map((w) => w.dataset.w));
  };
  let dragging = null;
  grid.addEventListener('dragstart', (e) => { const w = e.target.closest('.cc-widget'); if (!w || !e.target.closest('.cc-handle')) { if (w && !e.target.closest('.cc-handle')) e.preventDefault(); return; } dragging = w; w.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', w.dataset.w); });
  grid.addEventListener('dragover', (e) => {
    if (!dragging) return; e.preventDefault();
    const over = e.target.closest('.cc-widget'); if (!over || over === dragging) return;
    const r = over.getBoundingClientRect(), after = (e.clientY - r.top) / r.height > 0.5 || (e.clientX - r.left) / r.width > 0.6;
    grid.insertBefore(dragging, after ? over.nextSibling : over);
  });
  grid.addEventListener('dragend', () => { if (dragging) dragging.classList.remove('dragging'); dragging = null; saveLayout(); });
  grid.addEventListener('click', (e) => {
    const w = e.target.closest('.cc-widget'); if (!w) return;
    if (e.target.closest('.cc-fold')) { w.classList.toggle('folded'); e.target.closest('.cc-fold').setAttribute('aria-expanded', String(!w.classList.contains('folded'))); saveLayout(); }
    if (e.target.closest('.cc-up') && w.previousElementSibling) { grid.insertBefore(w, w.previousElementSibling); saveLayout(); (w.querySelector('.cc-up').disabled ? w.querySelector('.cc-down') : w.querySelector('.cc-up')).focus(); }
    if (e.target.closest('.cc-down') && w.nextElementSibling) { grid.insertBefore(w.nextElementSibling, w); saveLayout(); (w.querySelector('.cc-down').disabled ? w.querySelector('.cc-up') : w.querySelector('.cc-down')).focus(); }
  });
  $('cc-layout-reset').addEventListener('click', () => { Loki.store.set(ORDER_KEY, []); Loki.store.set(FOLD_KEY, []); location.reload(); });

  /* ---------------------------------------------------------- telemetry */
  const TARGETS = [
    { id: 'loki', label: 'Loki server', url: '/api/status/telemetry', parse: (d) => d },
    { id: 'binance', label: 'Binance', url: 'https://data-api.binance.vision/api/v3/time', parse: (d) => ({ server_time: d.serverTime / 1000 }) },
    { id: 'coinbase', label: 'Coinbase', url: 'https://api.exchange.coinbase.com/time', parse: (d) => ({ server_time: d.epoch }) },
    { id: 'polymarket', label: 'Polymarket CLOB', url: 'https://clob.polymarket.com/time', parse: (d) => ({ server_time: Number(d) + 0.5, coarse: true }) }, // whole seconds only
  ];
  const tele = Object.fromEntries(TARGETS.map((t) => [t.id, { ms: [], ok: [], skew: null, info: null }]));
  async function ping(t) {
    const s = tele[t.id], t0 = performance.now(), wall0 = Date.now();
    try {
      const res = await fetch(t.url, { cache: 'no-store', signal: AbortSignal.timeout(6000) });
      const ms = performance.now() - t0;
      if (!res.ok) throw new Error(String(res.status));
      const d = t.parse(await res.json());
      s.ms.push(ms); s.ok.push(1);
      if (d && Number.isFinite(d.server_time)) { s.skew = d.server_time * 1000 - (wall0 + ms / 2); s.coarse = Boolean(d.coarse); }
      if (t.id === 'loki') s.info = d;
    } catch { s.ms.push(NaN); s.ok.push(0); }
    s.ms = s.ms.slice(-30); s.ok = s.ok.slice(-30);
  }
  function renderTelemetry() {
    $('cc-tele').innerHTML = `<table class="cc-table"><thead><tr><th>Endpoint</th><th>p50</th><th>Loss</th><th>Clock</th><th>Trend</th></tr></thead><tbody>${TARGETS.map((t) => {
      const s = tele[t.id], good = s.ms.filter(Number.isFinite).sort((a, z) => a - z), loss = s.ok.length ? 1 - s.ok.reduce((a, x) => a + x, 0) / s.ok.length : null;
      const p50 = good.length ? good[Math.floor(good.length / 2)] : null;
      return `<tr class="${loss == null ? '' : loss > 0.2 ? 'bad' : p50 > 1500 ? 'slow' : 'ok'}"><td>${esc(t.label)}</td><td>${p50 == null ? '—' : Math.round(p50) + 'ms'}</td><td>${loss == null ? '—' : pct(loss, 0)}</td><td>${s.skew == null ? '—' : s.coarse ? `~${s.skew >= 0 ? '+' : ''}${(s.skew / 1000).toFixed(1)}s ±0.5s` : `${s.skew >= 0 ? '+' : ''}${Math.round(s.skew)}ms`}</td><td>${spark(s.ms, 90, 22)}</td></tr>`;
    }).join('')}</tbody></table>`;
    const i = tele.loki.info;
    $('cc-server').innerHTML = i ? [['Region', i.region], ['Uptime', i.uptime_s < 120 ? `${Math.round(i.uptime_s)}s${i.cold_start ? ' (cold start)' : ''}` : `${(i.uptime_s / 60).toFixed(0)} min`], ['Memory', i.rss_mb ? `${i.rss_mb} MB` : i.peak_rss_mb ? `${i.peak_rss_mb} MB peak` : '—'], ['CPU', i.cpu_share == null ? '—' : pct(i.cpu_share)], ['Load', i.load_1m == null ? '—' : i.load_1m.toFixed(2)], ['Python', i.python]]
      .map(([k, v]) => `<div><span>${k}</span><b>${esc(String(v))}</b></div>`).join('') : '<p class="muted">Waiting for the server…</p>';
  }
  async function telemetryLoop() { if (visible()) { await Promise.all(TARGETS.map(ping)); renderTelemetry(); renderVenues(); } }

  /* ------------------------------------------------------- venue health */
  function renderVenues() {
    const rows = Loki.execq ? Loki.execq.all().filter((v) => v.n) : [];
    $('cc-venues').innerHTML = rows.length ? `<table class="cc-table"><thead><tr><th>Venue</th><th>State</th><th>Errors</th><th>p50 / p95</th><th>Size</th></tr></thead><tbody>${rows.map((v) => `<tr class="${v.state === 'healthy' ? 'ok' : v.state === 'degraded' ? 'slow' : 'bad'}"><td>${esc(v.venue)}</td><td>${v.state}</td><td>${pct(v.errorRate, 0)}</td><td>${v.p50 == null ? '—' : `${Math.round(v.p50)} / ${Math.round(v.p95)}ms`}</td><td>×${Loki.execq.sizeFactor(v.venue)}</td></tr>`).join('')}</tbody></table>`
      : '<p class="muted">No venue traffic in the last 5 minutes yet.</p>';
  }

  /* ---------------------------------------------------- Monte Carlo risk */
  function renderMc(s) {
    s = s || (Loki.mcrisk && Loki.mcrisk.snapshot());
    if (!s) return;
    const box = $('cc-mc');
    if (s.error) { box.innerHTML = `<p class="form-message">${esc(s.error)}</p>`; return; }
    if (!s.positions) { box.innerHTML = `<p class="muted">No open positions to stress-test. Connect a broker on Trade or let the Polymarket paper bot open a position — both feed this engine automatically (providers: ${esc((s.providers || []).join(', ') || 'none')}).</p>`; return; }
    box.innerHTML = `<div class="cc-kpis">${[['VaR 95%', F.usd(s.var95)], ['VaR 99%', F.usd(s.var99)], ['CVaR 95%', F.usd(s.cvar95)], ['Worst path', F.usd(s.worst, true)], ['Size multiplier', `×${(s.multiplier || 1).toFixed(2)}`]].map(([k, v]) => `<div><span>${k}</span><b>${esc(v)}</b></div>`).join('')}</div>
      <p class="caption">${(s.paths || 0).toLocaleString()} one-day paths in ${s.ms || '—'}ms · ${s.positions} positions${s.excluded ? ` (${s.excluded} without price history left out)` : ''} · equity ${F.usd(s.equity || 0)}</p>
      ${(s.contributions || []).length ? `<p class="eyebrow">SHARE OF THE WORST 5% OF DAYS</p><ul class="cc-bars">${s.contributions.slice().sort((a, z) => a.tail - z.tail).slice(0, 6).map((c) => `<li><span>${esc(c.id)}</span><b class="${c.tail < 0 ? 'down' : 'up'}">${F.usd(c.tail, true)}</b></li>`).join('')}</ul>` : ''}
      <p class="${s.multiplier < 1 ? 'tk-stop' : 'tk-go'}">${esc(s.suggestion || '')}</p>`;
  }
  Loki.on('mcrisk:update', renderMc);
  $('cc-mc-run').addEventListener('click', () => { msg('cc-mc-msg', 'Running 10,000 paths…'); Loki.mcrisk.tick().then(() => msg('cc-mc-msg', '')); });

  /* -------------------------------------------------------- consensus */
  function renderVote(r) {
    $('cc-vote').innerHTML = `<p class="${r.approved ? 'tk-go' : 'tk-stop'}">${esc(r.summary)} · ${r.reporting} of ${r.votes.length} agents reporting</p>
      <ul class="tk-checks">${r.votes.map((v) => `<li class="${v.veto ? 'no' : v.score == null ? '' : v.score >= 0 ? 'ok' : 'no'}"><b>${esc(v.name)}</b> ${v.veto ? 'VETO · ' : v.score == null ? '' : `${v.score >= 0 ? '+' : ''}${v.score.toFixed(2)} · `}${esc(v.why)}</li>`).join('')}</ul>`;
  }
  Loki.on('consensus:vote', renderVote);
  $('cc-vote-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const symbol = $('cc-vote-sym').value.trim().toUpperCase() || 'BTC-USD', side = $('cc-vote-side').value, notional = Number($('cc-vote-usd').value) || 1000;
    msg('cc-vote-msg', `Polling six agents on ${symbol}…`);
    $('cc-vote').innerHTML = '';
    try {
      const bars = await Loki.data.bars(symbol, { interval: '1d', limit: 300 });
      const council = bars.length > 60 ? Loki.council.analyze(symbol, bars) : null;
      let book = null;
      const pair = Loki.data.binancePair && Loki.data.binancePair(symbol);
      if (pair) { try { book = await Loki.data.binanceDepth(pair, 100); } catch { /* no book */ } }
      await Loki.consensus.vote({ symbol, side, notional, venue: pair ? 'binance' : 'alpaca-paper', kind: pair ? 'crypto' : 'stock' }, { council, book });
      msg('cc-vote-msg', 'A dry run: nothing is sent. Real orders on Trade run this same vote.');
    } catch (error) { msg('cc-vote-msg', error.message, true); $('cc-vote').innerHTML = '<p class="muted">No vote: the agents need price history for this symbol.</p>'; }
  });

  /* -------------------------------------------------------- evolution */
  function renderEvolve(s) {
    s = s || Loki.evolve.snapshot();
    $('cc-evo-toggle').textContent = s.running ? 'Stop evolving ■' : 'Start evolving ▶';
    $('cc-evo-auto').checked = Boolean(s.autoDeploy);
    $('cc-evo-status').textContent = s.status || '';
    const h = s.history || [];
    const g = s.champion && s.champion.g;
    $('cc-evo-out').innerHTML = `${h.length > 1 ? `<div class="cc-evo-chart">${spark(h.map((x) => x.best), 300, 60, 'train')}${spark(h.map((x) => x.val), 300, 60, 'val')}</div><p class="caption"><span class="legend train">training fitness</span> <span class="legend val">unseen-data fitness</span> · generation ${s.gen} · ${s.bars} bars of ${esc(s.symbol)} ${esc(s.interval)}</p>` : ''}
      ${g ? `<table class="cc-table"><tbody>${[['Enter above', pct(g.entry, 0)], ['Exit below', pct(g.exit, 0)], ['Trailing stop', `${g.trailing.toFixed(1)}%`], ['Stop-loss', `${g.stop.toFixed(1)}%`], ['Take-profit', `${g.take.toFixed(1)}%`], ['Size', pct(g.size, 0)],
        ['Training return', pct(s.champion.train.ret)], ['Unseen-data return', pct(s.champion.val.ret)], ['Unseen max drawdown', pct(s.champion.val.maxDD)], ['Deployed', s.deployed ? `generation ${s.deployed.gen}` : 'not yet']].map(([k, v]) => `<tr><td>${k}</td><td><b>${esc(v)}</b></td></tr>`).join('')}</tbody></table>` : ''}`;
    $('cc-evo-deploy').disabled = !g;
  }
  Loki.on('evolve:gen', renderEvolve);
  Loki.on('evolve:deploy', (d) => msg('cc-evo-msg', `Deployed generation ${d.gen}: ${d.changes.length} setting${d.changes.length === 1 ? '' : 's'} changed (undo with the co-pilot).`));
  $('cc-evo-toggle').addEventListener('click', () => { const s = Loki.evolve.snapshot(); if (s.running) Loki.evolve.stop(); else Loki.evolve.start($('cc-evo-sym').value, $('cc-evo-iv').value); });
  $('cc-evo-auto').addEventListener('change', (e) => Loki.evolve.setAutoDeploy(e.target.checked));
  $('cc-evo-deploy').addEventListener('click', () => { if (window.confirm('Apply the champion strain to your strategy settings (paper autopilot, replay and checks use them)? You can undo it with the co-pilot.')) Loki.evolve.deploy(); });

  /* ------------------------------------- order-book heatmap / surface */
  const HM = { pair: 'BTCUSDT', cols: [], lastAgg: null, icebergs: [], timer: null, mode: 'heat', prevBook: null, tick: 0 };
  const ROWS = 60, COLS = 120; // the price band adapts to how deep the returned book reaches
  async function heatTick() {
    if (!visible() || root.querySelector('[data-w="heatmap"]').classList.contains('folded')) return;
    try {
      const book = await Loki.data.binanceDepth(HM.pair, 1000);
      const mid = (book.bids[0][0] + book.asks[0][0]) / 2;
      if (!HM.band) HM.band = Math.max(0.0002, 0.95 * Math.min(mid - book.bids[book.bids.length - 1][0], book.asks[book.asks.length - 1][0] - mid) / mid);
      const lo = mid * (1 - HM.band), step = (mid * 2 * HM.band) / ROWS;
      const col = new Float64Array(ROWS);
      for (const [p, q] of [...book.bids, ...book.asks]) { const r = Math.floor((p - lo) / step); if (r >= 0 && r < ROWS) col[r] += p * q; }
      HM.cols.push({ mid, lo, step, col, t: Date.now() }); if (HM.cols.length > COLS) HM.cols.shift();
      // Iceberg heuristic: a price level that traded at least its full displayed size since the last
      // snapshot, yet is still showing most of that size, was probably refilled from hidden quantity.
      const q = new URLSearchParams({ symbol: HM.pair, limit: '1000' });
      if (HM.lastAgg) q.set('fromId', String(HM.lastAgg + 1));
      const trades = await Loki.getJSON(`https://data-api.binance.vision/api/v3/aggTrades?${q}`, { timeout: 6000 });
      if (trades.length) HM.lastAgg = trades[trades.length - 1].a;
      if (HM.prevBook && trades.length) {
        const traded = new Map();
        for (const t of trades) { const k = (+t.p).toFixed(2); traded.set(k, (traded.get(k) || 0) + +t.q); }
        const levelsNow = new Map([...book.bids, ...book.asks].map(([p, s]) => [p.toFixed(2), s]));
        for (const [p, s] of [...HM.prevBook.bids.slice(0, 20), ...HM.prevBook.asks.slice(0, 20)]) {
          const k = p.toFixed(2), hit = traded.get(k) || 0, now = levelsNow.get(k) || 0;
          if (s * p > 20000 && hit >= s * 0.9 && now >= s * 0.6) {
            const side = p <= HM.prevBook.bids[0][0] ? 'bid' : 'ask';
            HM.icebergs.unshift({ t: Date.now(), p, side, shown: s, traded: hit, now });
            HM.icebergs = HM.icebergs.slice(0, 12);
          }
        }
      }
      HM.prevBook = { bids: book.bids, asks: book.asks };
      drawHeat();
      $('cc-ice').innerHTML = HM.icebergs.length ? HM.icebergs.map((x) => `<li><time>${new Date(x.t).toLocaleTimeString()}</time> <b class="${x.side === 'bid' ? 'up' : 'down'}">${x.side} ${F.num(x.p)}</b> showed ${F.num(x.shown, 4)}, traded ${F.num(x.traded, 4)}, still ${F.num(x.now, 4)}</li>`).join('') : '<li class="muted">No refills spotted yet. Icebergs show as a level that keeps trading its full size without shrinking.</li>';
      msg('cc-heat-msg', `${HM.pair} · mid ${F.num(mid)} · ±${(HM.band * 100).toFixed(2)}% · ${HM.cols.length} snapshots`);
    } catch (error) { msg('cc-heat-msg', `Book paused: ${error.message}`, true); }
  }
  function drawHeat() {
    const cv = $('cc-heat'), ctx = cv.getContext('2d'), dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = cv.clientWidth || 600, h = cv.clientHeight || 260;
    if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    if (!HM.cols.length) return;
    const max = Math.max(...HM.cols.flatMap((c) => Array.from(c.col))) || 1;
    const last = HM.cols[HM.cols.length - 1];
    if (HM.mode === 'heat') {
      const cw = w / COLS, rh = h / ROWS;
      HM.cols.forEach((c, x) => {
        const x0 = (COLS - HM.cols.length + x) * cw;
        for (let r = 0; r < ROWS; r++) {
          const v = Math.pow(c.col[r] / max, 0.45); if (v < 0.04) continue;
          const price = c.lo + (r + 0.5) * c.step;
          ctx.fillStyle = price < c.mid ? `rgba(111,227,161,${v.toFixed(3)})` : `rgba(255,107,91,${v.toFixed(3)})`;
          ctx.fillRect(x0, h - (r + 1) * rh, cw + 0.5, rh + 0.5);
        }
      });
      // mid-price line, mapped into the latest snapshot's price scale
      ctx.strokeStyle = 'rgba(231,240,255,.85)'; ctx.lineWidth = 1.5; ctx.beginPath();
      HM.cols.forEach((c, x) => { const y = h - ((c.mid - last.lo) / last.step) * rh, px = (COLS - HM.cols.length + x + 0.5) * cw; x ? ctx.lineTo(px, y) : ctx.moveTo(px, y); });
      ctx.stroke();
    } else {
      // Depth surface: each snapshot is a ridge of cumulative depth, stacked back in time (isometric).
      const n = HM.cols.length, depthX = 0.45, depthY = 0.5;
      for (let k = 0; k < n; k += Math.max(1, Math.floor(n / 40))) {
        const c = HM.cols[k], age = (n - 1 - k) / Math.max(1, n - 1);
        const ox = age * w * depthX * 0.5, oy = -age * h * depthY;
        ctx.beginPath();
        for (let r = 0; r < ROWS; r++) {
          const v = Math.pow(c.col[r] / max, 0.45);
          const x = ox + (r / (ROWS - 1)) * w * (1 - depthX * 0.5), y = h - 6 + oy - v * h * 0.45;
          r ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.strokeStyle = `rgba(139,218,212,${(0.15 + 0.85 * (1 - age)).toFixed(2)})`; ctx.lineWidth = age < 0.02 ? 2 : 1; ctx.stroke();
      }
      ctx.fillStyle = 'rgba(231,240,255,.7)'; ctx.font = '11px ui-monospace, monospace';
      ctx.fillText('price →   (newest snapshot in front, older ones behind ↗)', 8, 14);
    }
  }
  $('cc-heat-pair').addEventListener('change', (e) => { HM.pair = e.target.value; HM.band = 0; HM.cols = []; HM.lastAgg = null; HM.prevBook = null; HM.icebergs = []; heatTick(); });
  $('cc-heat-mode').addEventListener('click', (e) => { HM.mode = HM.mode === 'heat' ? 'surface' : 'heat'; e.target.textContent = HM.mode === 'heat' ? 'Depth surface view' : 'Heatmap view'; drawHeat(); });

  /* ----------------------------------------- natural-language strategy */
  let built = null, runner = Loki.store.get('loki_nl_runner_v1', null);
  $('cc-nl-examples').innerHTML = Loki.strategy.EXAMPLES.map((x) => `<button type="button" class="chip" data-nl="${esc(x)}">${esc(x.length > 60 ? x.slice(0, 58) + '…' : x)}</button>`).join('');
  $('cc-nl-examples').addEventListener('click', (e) => { const b = e.target.closest('[data-nl]'); if (b) { $('cc-nl-text').value = b.dataset.nl; buildStrategy(); } });
  async function buildStrategy() {
    const text = $('cc-nl-text').value.trim();
    if (!text) { msg('cc-nl-msg', 'Describe a strategy first.', true); return; }
    const rules = Loki.strategy.parse(text);
    $('cc-nl-btn').disabled = true; msg('cc-nl-msg', `Loading ${rules.symbol} ${rules.interval} history and backtesting…`);
    try {
      const bars = await Loki.strategy.loadBars(rules, 1000);
      if (bars.length < 120) throw new Error('not enough history for this symbol and interval');
      const bt = Loki.strategy.backtest(rules, bars);
      built = { rules, bt, text };
      $('cc-nl-out').innerHTML = `
        ${rules.notes.length ? `<ul class="tk-checks">${rules.notes.map((n) => `<li class="no">${esc(n)}</li>`).join('')}</ul>` : ''}
        <p class="caption">Understood: ${rules.understood.map((u) => `<code>${esc(u)}</code>`).join(' · ') || 'nothing'}</p>
        <div class="cc-kpis">${[['Return', pct(bt.ret), bt.ret >= 0 ? 'up' : 'down'], ['Buy & hold', pct(bt.buyHold), bt.buyHold >= 0 ? 'up' : 'down'], ['Max drawdown', pct(bt.maxDD), ''], ['Trades', String(bt.trades), ''], ['Win rate', bt.trades ? pct(bt.winRate, 0) : '—', ''], ['Avg trade', bt.trades ? pct(bt.avgTrade, 2) : '—', '']].map(([k, v, c]) => `<div><span>${k}</span><b class="${c}">${v}</b></div>`).join('')}</div>
        ${spark(bt.equity.map((p) => p.v), 600, 70, bt.ret >= 0 ? 'up' : 'down')}
        <p class="caption">${bt.bars} ${esc(rules.interval)} bars of real ${esc(rules.symbol)} history, fills at the next bar's open, 10bp per side. A backtest is not a forecast.</p>
        <details><summary>Rules as JSON</summary><pre class="cc-code">${esc(JSON.stringify({ symbol: rules.symbol, interval: rules.interval, entry: rules.entry, exit: rules.exit, exit_any: rules.exitAny, stop_pct: rules.stop, take_pct: rules.take, trailing_pct: rules.trailing, size: rules.size }, null, 2))}</pre></details>
        <details open><summary>Generated code</summary><pre class="cc-code">${esc(Loki.strategy.toCode(rules))}</pre></details>`;
      $('cc-nl-arm').disabled = !rules.entry.length;
      msg('cc-nl-msg', '');
    } catch (error) { msg('cc-nl-msg', error.message, true); }
    finally { $('cc-nl-btn').disabled = false; }
  }
  $('cc-nl-form').addEventListener('submit', (e) => { e.preventDefault(); buildStrategy(); });
  $('cc-nl-arm').addEventListener('click', async () => {
    if (!built) return;
    const c = await Loki.audit.commit({ rules: built.rules, text: built.text }, `NL strategy: ${built.rules.symbol}`);
    runner = { rules: built.rules, text: built.text, commitment: c.hash, pos: null, cash: 10000, start: 10000, trades: [], armed: Date.now(), lastBar: 0 };
    Loki.store.set('loki_nl_runner_v1', runner);
    msg('cc-nl-msg', `Armed on paper. Commitment ${c.hash.slice(0, 12)}… was logged before the first trade, so the rules can be proven later.`);
    renderRunner(); runnerTick();
  });
  $('cc-nl-disarm').addEventListener('click', () => { runner = null; Loki.store.set('loki_nl_runner_v1', null); renderRunner(); });
  async function runnerTick() {
    if (!runner || document.hidden) return;
    try {
      const bars = await Loki.strategy.loadBars(runner.rules, 400);
      const closed = bars.slice(0, -1); // only completed bars decide
      const i = closed.length - 1, bar = closed[i];
      if (!bar || bar.t <= runner.lastBar) return;
      runner.lastBar = bar.t;
      const px = bars[bars.length - 1].o, rules = runner.rules;
      const c = closed.map((b) => b.c), cache = {};
      if (runner.pos) {
        runner.pos.held += 1; runner.pos.peak = Math.max(runner.pos.peak, bar.h);
        const ctx = { held: runner.pos.held };
        const hit = (rules.stop != null && bar.l <= runner.pos.entry * (1 - rules.stop / 100)) || (rules.take != null && bar.h >= runner.pos.entry * (1 + rules.take / 100))
          || (rules.trailing != null && bar.l <= runner.pos.peak * (1 - rules.trailing / 100))
          || (rules.exit.length && (rules.exitAny ? rules.exit.some((x) => Loki.strategy.check(x, i, closed, c, cache, ctx)) : rules.exit.every((x) => Loki.strategy.check(x, i, closed, c, cache, ctx))));
        if (hit) {
          const out = px * 0.999, pnl = runner.pos.qty * out - runner.pos.cost;
          runner.cash += runner.pos.qty * out; runner.trades.push({ t: Date.now(), side: 'sell', px: out, pnl }); runner.pos = null;
          Loki.audit.append({ kind: 'paper', venue: 'nl-strategy', action: 'sell', symbol: rules.symbol, price: out, pnl });
          signal(`SELL ${rules.symbol} @ ${F.num(out)} (paper, NL strategy) · P&L ${F.usd(pnl, true)}`, 'strategy');
        }
      } else if (rules.entry.every((x) => Loki.strategy.check(x, i, closed, c, cache, { held: 0 }))) {
        const inPx = px * 1.001, spend = runner.cash * rules.size, qty = spend / inPx;
        runner.cash -= spend; runner.pos = { qty, entry: inPx, cost: spend, peak: inPx, held: 0 };
        runner.trades.push({ t: Date.now(), side: 'buy', px: inPx });
        Loki.audit.append({ kind: 'paper', venue: 'nl-strategy', action: 'buy', symbol: rules.symbol, price: inPx, qty });
        Loki.emit('order', { venue: 'loki', side: 'buy', symbol: rules.symbol, qty, paper: true });
        signal(`BUY ${rules.symbol} @ ${F.num(inPx)} (paper, NL strategy)`, 'strategy');
      }
      runner.mark = bars[bars.length - 1].c;
      Loki.store.set('loki_nl_runner_v1', runner);
    } catch (error) { msg('cc-run-msg', `Runner paused this tick: ${error.message}`, true); }
    renderRunner();
  }
  function renderRunner() {
    const box = $('cc-runner');
    $('cc-nl-disarm').hidden = !runner;
    if (!runner) { box.innerHTML = '<p class="muted">No strategy armed. Build one, then “Commit &amp; arm on paper”.</p>'; return; }
    const eq = runner.cash + (runner.pos ? runner.pos.qty * (runner.mark || runner.pos.entry) : 0);
    box.innerHTML = `<p><b>${esc(runner.rules.symbol)} ${esc(runner.rules.interval)}</b> · paper equity ${F.usd(eq)} (${F.usd(eq - runner.start, true)}) · ${runner.pos ? `holding ${F.num(runner.pos.qty, 6)} from ${F.num(runner.pos.entry)}` : 'flat'} · ${runner.trades.length} fills · commitment <code>${esc(runner.commitment.slice(0, 12))}…</code></p>`;
  }

  /* -------------------------------------------------------- newswire */
  async function loadNews() {
    const box = $('cc-news');
    try {
      const d = await Loki.getJSON('/api/newswire', { timeout: 20000 });
      const tone = d.tone, label = tone == null ? 'no policy signal' : tone > 0.15 ? 'hawkish' : tone < -0.15 ? 'dovish' : 'neutral';
      $('cc-tone').innerHTML = `<div class="cc-gauge"><i style="left:${tone == null ? 50 : (tone + 1) * 50}%"></i></div><p><b>${label}</b>${tone == null ? '' : ` (${tone >= 0 ? '+' : ''}${tone.toFixed(2)}, ${d.scored} scored items)`} · <span class="caption">dovish ← → hawkish</span></p>`;
      box.innerHTML = d.items.slice(0, 14).map((it) => `<li class="${it.tone > 0 ? 'down' : it.tone < 0 ? 'up' : ''}"><span class="tag">${esc(it.source)}</span> ${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>` : esc(it.title)}${it.matched.length ? ` <code>${esc(it.matched.join(' '))}</code>` : ''}<br><time>${it.published ? new Date(it.published).toLocaleString() : ''}</time></li>`).join('');
      msg('cc-news-msg', `${d.method}.${Object.keys(d.errors || {}).length ? ` Unreachable: ${Object.keys(d.errors).join(', ')}.` : ''}`);
    } catch (error) { box.innerHTML = ''; msg('cc-news-msg', error.message, true); }
  }
  $('cc-news-refresh').addEventListener('click', loadNews);

  /* --------------------------------------- scorecards and commitments */
  function renderCommit() {
    const c = Loki.audit.currentCommit();
    $('cc-commit-now').innerHTML = c ? `Current commitment <code>${esc(c.hash.slice(0, 16))}…</code> · ${esc(c.label)} · ${new Date(c.at).toLocaleString()}` : 'No strategy committed yet.';
  }
  $('cc-commit').addEventListener('click', async () => { const c = await Loki.audit.commit(Loki.config(), 'co-pilot settings'); renderCommit(); msg('cc-proof-msg', `Committed ${c.hash.slice(0, 16)}…: every trade from now carries this hash.`); });
  $('cc-reveal').addEventListener('click', () => {
    const r = Loki.audit.reveal(); if (!r) { msg('cc-proof-msg', 'Nothing committed yet.', true); return; }
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: 'application/json' })); a.download = `loki-reveal-${r.hash.slice(0, 10)}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });
  $('cc-reveal-file').addEventListener('change', async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try { const v = await Loki.audit.checkReveal(JSON.parse(await file.text())); msg('cc-proof-msg', v.ok ? `Reveal checks out: ${v.why}${v.logged_at ? `; committed in this log at ${new Date(v.logged_at).toLocaleString()}` : ''}; ${v.trades} trades carry it.` : `Reveal FAILED: ${v.why}.`, !v.ok); }
    catch (error) { msg('cc-proof-msg', `Could not read that file: ${error.message}`, true); }
    e.target.value = '';
  });
  $('cc-score-make').addEventListener('click', async () => {
    try {
      const token = await Loki.audit.scorecard();
      const url = `${location.origin}/?scorecard=${token}#command`;
      $('cc-score-link').value = url;
      try { await navigator.clipboard.writeText(url); msg('cc-proof-msg', 'Signed scorecard link copied. Anyone with the site password can open it and their browser re-checks the signature.'); }
      catch { msg('cc-proof-msg', 'Signed scorecard link ready below; copy it to share.'); }
    } catch (error) { msg('cc-proof-msg', error.message, true); }
  });
  async function verifyScore(token) {
    try {
      const v = await Loki.audit.verifyScorecard(token), m = v.card.metrics;
      $('cc-score-view').innerHTML = `<div class="audit-badge ${v.ok ? 'ok' : 'broken'}"><b>${v.ok ? '✓ Signature valid' : '✗ Signature does NOT match'}</b> · device key ${esc(v.fingerprint.slice(0, 8))} · issued ${new Date(v.card.issued).toLocaleString()}<br>
        ${m.executions} executions (${m.paper} paper, ${m.live} live) · realised ${F.usd(m.realised_pnl, true)} · ${m.closed ? `${m.wins}/${m.closed} winners` : 'no closed trades'} · chain of ${v.card.count} entries since ${v.card.genesis ? new Date(v.card.genesis).toLocaleDateString() : '—'} · head <code>${esc(String(v.card.head).slice(0, 12))}…</code>${v.card.commitment ? ` · strategy commitment <code>${esc(v.card.commitment.slice(0, 12))}…</code>` : ''}</div>
        <p class="caption">This proves the figures were signed by that device's key and not edited since. It cannot prove the device recorded every trade it made; reconcile with the broker for that.</p>`;
    } catch (error) { $('cc-score-view').innerHTML = `<p class="form-message">Not a valid scorecard: ${esc(error.message)}</p>`; }
  }
  $('cc-score-check').addEventListener('click', () => { const v = $('cc-score-link').value.trim(); const m = v.match(/scorecard=([A-Za-z0-9_-]+)/); verifyScore(m ? m[1] : v); });

  /* -------------------------------------------------- signal relay */
  const hook = () => Loki.store.get(HOOK_KEY, { url: '', chat: '', strategy: true, orders: false });
  function loadHook() { const h = hook(); $('cc-hook-url').value = h.url; $('cc-hook-chat').value = h.chat; $('cc-hook-strategy').checked = h.strategy; $('cc-hook-orders').checked = h.orders; }
  $('cc-hook-form').addEventListener('submit', (e) => { e.preventDefault(); Loki.store.set(HOOK_KEY, { url: $('cc-hook-url').value.trim(), chat: $('cc-hook-chat').value.trim(), strategy: $('cc-hook-strategy').checked, orders: $('cc-hook-orders').checked }); msg('cc-hook-msg', 'Saved on this device.'); });
  async function signal(text, kind) {
    const h = hook();
    if (!h.url || (kind === 'strategy' && !h.strategy) || (kind === 'order' && !h.orders)) return false;
    await Loki.postJSON('/api/signals/relay', { url: h.url, text: `Loki · ${text}`, chat_id: h.chat || null });
    return true;
  }
  $('cc-hook-test').addEventListener('click', async () => {
    const h = hook(); if (!h.url) { msg('cc-hook-msg', 'Save a webhook first.', true); return; }
    try { await Loki.postJSON('/api/signals/relay', { url: h.url, text: 'Loki · test signal from the command center', chat_id: h.chat || null }); msg('cc-hook-msg', 'Test signal delivered.'); }
    catch (error) { msg('cc-hook-msg', error.message, true); }
  });
  Loki.on('audit:append', (e) => { if (e && e.data && ['buy', 'sell', 'order'].includes(e.data.action) && e.data.venue !== 'nl-strategy') signal(`${String(e.data.side || e.data.action).toUpperCase()} ${e.data.symbol || e.data.market || ''} ${e.data.qty || e.data.shares || ''} (${e.data.kind}, ${e.data.venue})`, 'order').catch(() => {}); });
  $('cc-card-export').addEventListener('click', () => {
    const card = { format: 'loki-strategy-card/1', exported_at: new Date().toISOString(), config: Loki.config(), nl_strategy: built ? { text: built.text, rules: built.rules, backtest: { ret: built.bt.ret, maxDD: built.bt.maxDD, trades: built.bt.trades, winRate: built.bt.winRate } } : null, commitment: (Loki.audit.currentCommit() || {}).hash || null };
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(card, null, 2)], { type: 'application/json' })); a.download = 'loki-strategy-card.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });
  $('cc-card-file').addEventListener('change', async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try {
      const card = JSON.parse(await file.text());
      if (card.format !== 'loki-strategy-card/1') throw new Error('not a Loki strategy card');
      if (card.nl_strategy && card.nl_strategy.text) { $('cc-nl-text').value = String(card.nl_strategy.text).slice(0, 600); buildStrategy(); }
      if (card.config) {
        const parsed = Loki.copilot.sanitize(card.config);
        if (parsed.changes.length && window.confirm(`Also apply this card’s settings? ${parsed.changes.map((c) => `${c.label}: ${JSON.stringify(c.from)} → ${JSON.stringify(c.to)}`).join('; ')} (undo with the co-pilot)`)) Loki.copilot.apply(parsed);
      }
      msg('cc-hook-msg', 'Strategy card loaded.');
    } catch (error) { msg('cc-hook-msg', error.message, true); }
    e.target.value = '';
  });

  /* ------------------------------------------- pause, flatten, kill */
  function pauseAll() {
    const stopped = [];
    if (Loki.arb && Loki.arb.snapshot().running) { Loki.arb.stop(); stopped.push('arbitrage engine'); }
    if (Loki.evolve && Loki.evolve.snapshot().running) { Loki.evolve.stop(); stopped.push('evolution'); }
    if (Loki.swarm && Loki.swarm.snapshot().running) { Loki.swarm.stop(); stopped.push('swarm'); }
    const mm = document.getElementById('mm-toggle'); if (mm && mm.getAttribute('aria-pressed') === 'true') { mm.click(); stopped.push('market-maker'); }
    const pmb = document.getElementById('pmb-auto'); if (pmb && pmb.getAttribute('aria-pressed') === 'true') { pmb.click(); stopped.push('Polymarket bot'); }
    if (Loki.config().autopilot) { Loki.copilot.apply({ changes: [{ key: 'autopilot', from: true, to: false, why: 'paused by hotkey/voice' }], config: { ...Loki.config(), autopilot: false } }); stopped.push('paper autopilot'); }
    if (runner) { runner = null; Loki.store.set('loki_nl_runner_v1', null); renderRunner(); stopped.push('armed NL strategy (disarmed; re-arm from the builder)'); }
    toast(stopped.length ? `Paused: ${stopped.join(', ')}. Open positions are kept.` : 'Nothing was running.');
  }
  function killSwitch(paperOnly) {
    const venue = Loki.store.get('loki_trade_venue_v1', 'alpaca-paper');
    if (paperOnly && /live/.test(venue)) { toast('Flatten-paper only works while Trade is on a paper or demo account.'); return; }
    location.hash = 'trade';
    setTimeout(() => { const k = document.getElementById('kill-switch'); if (k) k.click(); }, 300);
  }
  let toastTimer = null;
  function toast(text) { const t = $('cc-toast'); t.textContent = text; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 5000); }

  /* -------------------------------------------------------- voice */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, listening = false;
  function voice() {
    if (!SR) { toast('Voice commands need a browser with speech recognition (Chrome, Edge or Safari).'); return; }
    if (listening) { rec.stop(); return; }
    rec = new SR(); rec.lang = 'en-US'; rec.interimResults = false; rec.maxAlternatives = 1;
    rec.onresult = (e) => runVoice(e.results[0][0].transcript);
    rec.onerror = (e) => toast(`Voice: ${e.error}`);
    rec.onend = () => { listening = false; $('cc-voice').classList.remove('on'); $('cc-voice').setAttribute('aria-pressed', 'false'); };
    rec.start(); listening = true; $('cc-voice').classList.add('on'); $('cc-voice').setAttribute('aria-pressed', 'true'); toast('Listening…');
  }
  const VIEWS = { overview: 'overview', trade: 'trade', council: 'council', predictions: 'predictions', prediction: 'predictions', replay: 'replay', news: 'news', command: 'command', 'command center': 'command', 'execution map': 'execution-map', game: 'game' };
  function runVoice(said) {
    const t = said.toLowerCase().trim();
    $('cc-voice-log').insertAdjacentHTML('afterbegin', `<li><time>${new Date().toLocaleTimeString()}</time> “${esc(said)}”</li>`);
    if (/\b(pause|stop) (everything|all|the bots?|trading)\b|^pause$/.test(t)) return pauseAll();
    if (/kill switch|flatten everything|close everything/.test(t)) return window.confirm(`Voice heard “${said}”. Open the kill switch?`) && killSwitch(false);
    if (/flatten paper/.test(t)) return window.confirm(`Voice heard “${said}”. Flatten the paper account?`) && killSwitch(true);
    const go = t.match(/^(?:go to|open|show)(?: the)? (.+)$/);
    if (go && VIEWS[go[1].replace(/ page$/, '')]) { location.hash = VIEWS[go[1].replace(/ page$/, '')]; return; }
    if (/co-?pilot/.test(t) && !/set|shift|move|stop loss/.test(t)) return Loki.copilotOpen && Loki.copilotOpen('');
    // Anything else goes to the co-pilot, which shows the exact diff before anything changes.
    if (Loki.copilotOpen) Loki.copilotOpen(said);
  }
  $('cc-voice').addEventListener('click', voice);

  /* ------------------------------------------------------- hotkeys */
  document.addEventListener('keydown', (e) => {
    const typing = e.target.closest && e.target.closest('input, textarea, select, [contenteditable="true"]');
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!e.shiftKey) return;
    const k = e.key.toUpperCase();
    if (k === 'P') { e.preventDefault(); pauseAll(); }
    else if (k === 'F') { e.preventDefault(); killSwitch(true); }
    else if (k === 'K') { e.preventDefault(); killSwitch(false); }
    else if (k === 'V') { e.preventDefault(); voice(); }
    else if (k === 'C') { e.preventDefault(); if (Loki.copilotOpen) Loki.copilotOpen(''); }
    else if (k === 'M') { e.preventDefault(); location.hash = 'command'; }
  });
  $('cc-pause').addEventListener('click', pauseAll);

  /* ---------------------------------------------------------- wiring */
  restoreLayout(); loadHook(); renderCommit(); renderRunner(); renderEvolve(); renderMc();
  setInterval(runnerTick, 60000); setTimeout(runnerTick, 3000); // an armed paper strategy keeps running on any page
  let started = false;
  function onView() {
    if (root.hidden) return;
    if (!started) {
      started = true;
      telemetryLoop(); setInterval(telemetryLoop, 10000);
      heatTick(); HM.timer = setInterval(heatTick, 2000);
      loadNews();
      if (Loki.mcrisk) Loki.mcrisk.tick();
    } else { telemetryLoop(); drawHeat(); }
  }
  const sc = new URLSearchParams(location.search).get('scorecard');
  if (sc) { if (!location.hash || location.hash === '#overview') location.hash = 'command'; verifyScore(sc); }
  document.addEventListener('loki:view', onView);
  window.addEventListener('hashchange', () => setTimeout(onView, 0));
  window.addEventListener('resize', () => { if (!root.hidden) drawHeat(); });
  onView();
  Loki.command = { pauseAll, signal, runVoice };
})();
