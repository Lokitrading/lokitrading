/* Memecoin terminal (workspace view #terminal): AUD-native, paper scalping on the live pump.fun stream.
 *
 * Every price is in Australian dollars at the live SOL/AUD rate (/api/terminal/rates). While running,
 * the tab reads the server's pump.fun relay; triggers (volume spike, whale radar) propose buys; each
 * buy must pass the anti-rug shield (/api/terminal/shield) and the circuit breakers; fills use the exact
 * bonding-curve maths after pump.fun's fee plus Solana fees in AUD; exits (stop, take-profit tiers,
 * trailing moonbag, stale, max hold) run on every price and every tick. Ctrl+Shift+X = Nuke all.
 * Paper only: nothing here signs or sends a transaction.
 */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const root = $('terminal');
  if (!root || !window.LokiScalp || !window.LokiPump) return;
  const S = window.LokiScalp; const P = window.LokiPump;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const aud = (v, d = 2) => (Number.isFinite(v) ? `${v < 0 ? '-' : ''}A$${Math.abs(v).toLocaleString('en-AU', { minimumFractionDigits: d, maximumFractionDigits: d })}` : '—');
  const KEY = 'loki_terminal_v1';

  let rates = null; let running = false; let abort = null;
  const st = new Map(); // mint -> latest reserves {vs, vt, rt, fee, symbol}
  const pending = new Set(); // mints with a buy waiting on the shield
  let book; let breaker; let spike; let radar; let holders; let lastAt = 0;
  const log = [];

  function settings() {
    const n = (id, d) => { const v = Number($(id).value); return Number.isFinite(v) ? v : d; };
    return {
      sizeAud: n('tm-size', 20), maxDailyLossAud: n('tm-daily', 100), maxTradeAud: n('tm-maxtrade', 50), maxOpen: n('tm-maxopen', 3),
      jitoTip: n('tm-jito', 0), spikeOn: $('tm-spike').checked, whaleOn: $('tm-whale').checked,
      wallets: $('tm-wallets').value.split(/[\s,]+/).filter((w) => /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(w)),
      plan: { stopLoss: -n('tm-stop', 25) / 100, tiers: [[n('tm-tp', 2), n('tm-tpshare', 50) / 100]], trail: n('tm-trail', 25) / 100,
        trailArm: 0.30, staleAfterS: n('tm-stale', 3) * 60, staleMove: 0.05, maxHoldS: n('tm-maxhold', 30) * 60 },
    };
  }
  function fresh() {
    const s = settings();
    book = new S.Book({ startAud: 1000, txLamports: 5000 + 100000, jitoTipLamports: s.jitoTip });
    breaker = new S.Breaker({ maxDailyLossAud: s.maxDailyLossAud, maxTradeAud: s.maxTradeAud, maxOpenPositions: s.maxOpen });
    spike = new S.SpikeDetector(); radar = new S.WhaleRadar(s.wallets); holders = new S.Holders();
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify({ cash: book.cashAud, closed: book.closed.slice(-200), fees: book.feesAud })); } catch { /* storage off */ }
  }
  function restore() {
    try { const d = JSON.parse(localStorage.getItem(KEY) || 'null'); if (d) { book.cashAud = d.cash; book.closed = d.closed || []; book.feesAud = d.fees || 0; } } catch { /* storage off */ }
  }
  const note = (text) => { log.unshift(`${new Date().toLocaleTimeString('en-AU')} · ${text}`); log.length = Math.min(log.length, 60); };
  const status = (text) => { $('tm-status').textContent = text; };

  async function loadRates() {
    try {
      const r = await (await fetch('/api/terminal/rates', { cache: 'no-store' })).json();
      if (!r || !r.sol_aud) throw new Error('no rate');
      rates = r;
      $('tm-solaud').textContent = aud(r.sol_aud);
      $('tm-usdcaud').textContent = r.usdc_aud.toFixed(4);
      $('tm-fees').textContent = aud(r.round_trip_fees_aud.priority, 4);
      $('tm-rate-src').textContent = `SOL/USD ${r.sources.sol_usd}, USDC/AUD ${r.sources.usdc_aud}${r.live ? '' : ' · stale'}`;
    } catch { $('tm-rate-src').textContent = 'Live AUD rate unavailable; the bot waits for it.'; }
  }

  // --- trading --------------------------------------------------------------------------------------
  async function propose(sig) {
    const s = settings();
    if (!rates || book.positions.has(sig.mint) || pending.has(sig.mint)) return;
    const gate = breaker.canEnter(s.sizeAud, lastAt);
    if (!gate.ok) { note(`skipped ${sig.mint.slice(0, 4)}…: ${gate.why}`); return; }
    pending.add(sig.mint);
    try {
      const top = holders.top10Pct(sig.mint);
      const q = `/api/terminal/shield?mint=${encodeURIComponent(sig.mint)}${top !== null ? `&stream_top10_pct=${top.toFixed(2)}` : ''}`;
      const shield = await (await fetch(q, { cache: 'no-store' })).json();
      const sym = (st.get(sig.mint) || {}).symbol || sig.mint.slice(0, 4);
      if (!shield.ok) { note(`${sym}: shield blocked the buy (${(shield.blocked_by || ['check failed']).join(', ')})`); return; }
      if (!running || !pending.has(sig.mint)) return; // stopped or nuked while the shield ran
      const now = st.get(sig.mint);
      if (!now) return;
      const again = breaker.canEnter(s.sizeAud, lastAt);
      if (!again.ok) { note(`${sym}: ${again.why}`); return; }
      const pos = book.open(sig.mint, sym, s.sizeAud, now, rates.sol_aud, lastAt, s.plan, sig.reason);
      if (pos) { breaker.recordOpen(); note(`BUY ${sym} for ${aud(s.sizeAud)} — ${sig.reason}`); }
    } catch (error) { note(`shield unavailable (${error.message}); no buy`); } finally { pending.delete(sig.mint); }
  }
  function manage(mint, at, why) {
    const pos = book.positions.get(mint); const s = st.get(mint);
    if (!pos || !s || !rates) return;
    const sells = why ? [[1, why]] : S.step(pos.plan, P.price(s.vs, s.vt), at);
    for (const [share, reason] of sells) {
      // step() gives shares of the ORIGINAL position, which is what book.sell takes; a full exit sells every token left.
      book.sell(mint, pos.plan.remaining <= 1e-12 || why ? 1 : share, s, rates.sol_aud, at, reason);
      note(`SELL ${pos.symbol} ${why ? 'all' : `${Math.round(share * 100)}%`} — ${reason}`);
      if (!book.positions.has(mint)) {
        const rec = book.closed[book.closed.length - 1];
        breaker.recordClose(rec.netAud, at);
        note(`${pos.symbol} closed: net ${aud(rec.netAud)} (${rec.netPct.toFixed(1)}%) after ${aud(rec.feesAud, 4)} fees`);
        break;
      }
    }
  }
  function handle(row) {
    if (row.at) lastAt = row.at;
    if (row.k === 'c') st.set(row.m, { ...(st.get(row.m) || {}), symbol: row.sym });
    if (row.k === 't') {
      st.set(row.m, { ...(st.get(row.m) || {}), vs: row.vs, vt: row.vt, rt: row.rt, fee: row.fee });
      holders.add(row); spike.add(row);
      if (book.positions.has(row.m)) manage(row.m, row.at);
      const s = settings();
      const w = s.whaleOn ? radar.check(row) : null;
      if (w) propose(w);
      if (s.spikeOn && row.b) { const hit = spike.check(row.m, row.at); if (hit) propose(hit); }
    }
    if (row.k === 'x' && book.positions.has(row.m)) manage(row.m, row.at, 'graduated');
    if (row.k === 'tick') for (const m of [...book.positions.keys()]) manage(m, row.at);
  }

  async function stream() {
    let failures = 0;
    while (running) {
      abort = new AbortController();
      try {
        const res = await fetch('/api/fusion/ai/pumpfun/stream?seconds=45', { headers: { Accept: 'application/x-ndjson' }, signal: abort.signal });
        if (!res.ok || !res.body) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
        const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
            if (!line) continue;
            let row; try { row = JSON.parse(line); } catch { continue; }
            if (row.k === 'error') throw new Error(row.detail || 'the relay lost Solana');
            if (row.k === 'open') { failures = 0; status('Live'); }
            handle(row);
          }
          render();
        }
      } catch (error) {
        if (!running) return;
        failures += 1; status('Reconnecting');
        if (failures >= 5) { note(`live feed failed 5 times (${error.message}); stopped`); stop(); return; }
        await new Promise((r) => setTimeout(r, 2000 * failures));
      }
    }
  }
  function start() {
    if (running) return;
    const s = settings();
    radar = new S.WhaleRadar(s.wallets);
    Object.assign(breaker.limits, { maxDailyLossAud: s.maxDailyLossAud, maxTradeAud: s.maxTradeAud, maxOpenPositions: s.maxOpen });
    book.o.jitoTipLamports = s.jitoTip;
    running = true; status('Connecting…'); note('started (paper)');
    $('tm-start').disabled = true; $('tm-stop').disabled = false;
    stream();
  }
  function stop() {
    running = false; if (abort) abort.abort();
    $('tm-start').disabled = false; $('tm-stop').disabled = true; status('Stopped'); render(); save();
  }
  function nukeAll() {
    pending.clear(); // cancel every buy still waiting on the shield
    const n = book.positions.size;
    const old = book.o.txLamports;
    book.o.txLamports = 5000 + 2000000; // maximum priority fee on every dump
    for (const m of [...book.positions.keys()]) manage(m, lastAt || Date.now() / 1000, 'NUKE ALL');
    book.o.txLamports = old;
    breaker.kill('Nuke all pressed');
    stop();
    note(`NUKE ALL: ${n} position${n === 1 ? '' : 's'} dumped to SOL at maximum priority; pending buys cancelled; kill switch on`);
    render();
  }

  // --- view -----------------------------------------------------------------------------------------
  function render() {
    const s = book.stats();
    let open = 0;
    const rows = [...book.positions.values()].map((p) => {
      const cur = st.get(p.mint); const value = cur && rates ? book.markAud(p.mint, cur, rates.sol_aud) : 0; open += value;
      const pnl = value + p.receivedAud - p.spentAud - p.feesAud - (rates ? book.legFeeAud(rates.sol_aud) : 0);
      return `<tr><td><strong>${esc(p.symbol)}</strong><small class="muted"> ${esc(p.reason)}</small></td><td>${aud(p.spentAud)}</td><td>${aud(value)}</td>` +
        `<td class="${pnl >= 0 ? 'positive' : 'negative'}">${aud(pnl)}</td><td>${Math.round((p.plan.remaining) * 100)}%</td><td>${Math.round((lastAt - p.openedAt) || 0)}s</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No open positions.</td></tr>';
    $('tm-positions').innerHTML = rows;
    $('tm-closed').innerHTML = book.closed.slice(-12).reverse().map((r) => `<tr><td>${esc(r.symbol)}</td><td>${esc(r.exit)}</td><td>${aud(r.spentAud)}</td>` +
      `<td>${aud(r.feesAud, 4)}</td><td class="${r.netAud >= 0 ? 'positive' : 'negative'}">${aud(r.netAud)} (${r.netPct.toFixed(1)}%)</td></tr>`).join('') ||
      '<tr><td colspan="5" class="muted">No closed trades yet.</td></tr>';
    $('tm-cash').textContent = aud(book.cashAud); $('tm-open').textContent = aud(open);
    $('tm-net').textContent = aud(s.netAud); $('tm-net').className = s.netAud >= 0 ? 'positive' : 'negative';
    $('tm-win').textContent = s.winRate === null ? '—' : `${s.winRate.toFixed(0)}% of ${s.trades}`;
    $('tm-breaker').textContent = breaker.tripped ? `Tripped: ${breaker.tripped}` : `OK · today ${aud(breaker.realisedAud)} of −${aud(breaker.limits.maxDailyLossAud, 0)} limit`;
    $('tm-breaker').className = breaker.tripped ? 'tag negative' : 'tag';
    $('tm-log').innerHTML = log.slice(0, 30).map((l) => `<li>${esc(l)}</li>`).join('');
  }

  fresh(); restore(); render(); loadRates();
  setInterval(() => { if (!document.hidden) loadRates(); }, 10000);
  $('tm-start').addEventListener('click', start);
  $('tm-stop').addEventListener('click', stop);
  $('tm-nuke').addEventListener('click', nukeAll);
  $('tm-reset').addEventListener('click', () => { breaker.reset(); note('breaker reset by you'); render(); });
  $('tm-clear').addEventListener('click', () => { if (running) stop(); fresh(); log.length = 0; save(); note('paper book reset to A$1,000'); render(); });
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) { e.preventDefault(); nukeAll(); }
  });
  window.addEventListener('beforeunload', save);
  // For the browser tests and the console: read-only state plus the same entry points the triggers use.
  window.LokiTerminal = { handle, propose, nukeAll, state: () => ({ open: book.positions.size, closed: book.closed.length, tripped: breaker.tripped, cash: book.cashAud, running }) };
})();
