/* Memecoin paper bot (AI trading → Memecoins tab).
 *
 * Shows what the tested memecoin rule holds now, its tested record against simply holding the
 * coins, every rule that was tried, and its record since it was frozen; a live scan of each coin's
 * market (Binance order books, every 20 s); and, with the visitor's saved Alpaca paper keys, the
 * same rule trading their PAPER account (every 5 minutes while the tab is open). Nothing here
 * touches real money.
 */
'use strict';
(() => {
  const root = document.getElementById('memecoin');
  if (!root) return;
  const { $, element, request, table, message } = window.LokiWorkspace;
  const pct = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%`);
  const num = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));
  const price = (v) => (v == null ? '—' : v >= 1 ? v.toFixed(4) : v.toPrecision(4));
  let loaded = false, timer = null, scanTimer = null;

  async function load() {
    try {
      const data = await request('/api/fusion/ai/memecoin');
      render(data);
    } catch (error) {
      $('#mc-status').textContent = 'Unavailable';
      $('#mc-verdict').replaceChildren(element('p', error.message || 'Prices could not be read; it tries again shortly.', 'form-message error'));
    }
  }

  function render(d) {
    const rec = d.record || {}, test = rec.test || {}, hold = (rec.hold || {}).test || {};
    const held = d.holdings.filter((h) => h.holding);
    $('#mc-status').textContent = held.length ? `Holding ${held.length}` : 'In cash';
    $('#mc-status').classList.toggle('good', held.length > 0);

    const verdict = element('p', null, 'pf-hindsight');
    verdict.append(element('strong', rec.passed ? 'Passed its test. ' : 'Did not pass its own test, so it stays on paper. '),
      `On ${test.from} to ${test.to}, which the choice never saw, it made ${pct(test.total_pct)} in total (${pct(test.cagr_pct)} a year, worst drop ${pct(test.max_dd_pct)}, Sharpe ${num(test.sharpe)}). ` +
      `Holding the same coins ${hold.total_pct < 0 ? `lost ${Math.abs(hold.total_pct).toFixed(1)}%` : `made ${pct(hold.total_pct)}`} (worst drop ${pct(hold.max_dd_pct)}). It mostly protected money by stepping aside in the 2025 crash; it did not make much. ` +
      `${rec.rules_losing_in_test} of the ${rec.rules_tested} rules tried lost money on those dates. The bar it had to clear: ${rec.pass_rule}.`);
    const wf = ((rec.walk_forward || {}).periods || {});
    const recent = wf['mid-2024 on'], boom = wf['2021 to mid-2024'];
    const parts = [verdict];
    if (recent && boom) {
      parts.push(element('p', `Fairer test (re-picking the best rule every month from the 6 months before it): during the 2021–mid-2024 boom it made ${pct(boom.rule.total_pct)} against ${pct(boom.hold.total_pct)} for holding; since mid-2024 it lost ${Math.abs(recent.rule.total_pct).toFixed(1)}% while holding lost ${Math.abs(recent.hold.total_pct).toFixed(1)}%. It is not profitable in the current market; it only loses less.`, 'pf-hindsight'));
    }
    $('#mc-verdict').replaceChildren(...parts);

    const per = rec.per_coin_test || {};
    const fwd = d.forward || {};
    $('#mc-record').replaceChildren(
      element('p', `Each coin on ${test.from} → ${test.to} (unseen), after 0.35% per trade`, 'eyebrow'),
      table(['Coin', 'Rule', 'Rule worst drop', 'Holding it', 'Holding worst drop'], Object.entries(per).map(([c, v]) => [
        c, pct((v.rule || {}).total_pct), pct((v.rule || {}).max_dd_pct), pct((v.hold || {}).total_pct), pct((v.hold || {}).max_dd_pct)])),
      element('p', fwd.bars >= 2
        ? `Since the record was frozen (${fwd.since}): ${pct(fwd.total_pct)} over ${fwd.bars} bars, worst drop ${pct(fwd.max_dd_pct)}. Far too short to judge; it is shown so you can watch it build.`
        : `Since the record was frozen (${fwd.since}): no completed bars yet.`, 'caption'));

    $('#mc-rules').replaceChildren(table(['Rule', 'Sharpe to mid-2024 (choosing)', 'Return a year, unseen', 'Sharpe, unseen', 'Worst drop, unseen'],
      (rec.rules || []).map((r) => [r.label === d.label ? `${r.label} (chosen)` : r.label, num((r.choose || {}).sharpe),
        pct((r.test || {}).cagr_pct), num((r.test || {}).sharpe), pct((r.test || {}).max_dd_pct)])));
    $('#mc-caveats').textContent = `${(rec.caveats || []).join(' ')} No real money: the site only ever trades this bot on an Alpaca paper account you connect. Not financial advice.`;
  }

  // --- Live scan: 24-hour figures, spread and order-book depth, refreshed every 20 s while visible ---
  const usd = (v) => (v == null ? '—' : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(0)}k` : `$${Math.round(v)}`);
  async function scan() {
    try {
      const d = await request('/api/fusion/ai/memecoin/scan');
      $('#mc-scan').replaceChildren(table(['Coin', 'Price', '24h', '24h volume', 'Spread', 'Depth ±1% (bid / ask)', 'Rule', 'Next trigger', 'Read'],
        d.coins.map((c) => c.status === 'no_data' ? [c.coin, 'no data', '', '', '', '', '', '', ''] : [
          c.coin, price(c.price), pct(c.change_24h_pct), usd(c.volume_24h_usd), c.spread_bps == null ? '—' : `${c.spread_bps.toFixed(1)} bp`,
          c.bid_depth_usd == null ? '—' : `${usd(c.bid_depth_usd)} / ${usd(c.ask_depth_usd)}`, c.rule === 'hold' ? 'Hold' : 'Cash',
          c.to_buy_pct != null ? `buys ${c.to_buy_pct > 0 ? `${c.to_buy_pct.toFixed(1)}% higher` : 'on the next close'}` : c.to_sell_pct != null ? `sells ${Math.abs(c.to_sell_pct).toFixed(1)}% lower` : '—',
          (c.read || []).join(' · ') || '—'])));
      $('#mc-scan-time').textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()} · ${d.note}`;
    } catch (error) {
      $('#mc-scan').replaceChildren(element('p', error.message || 'The order books could not be read; it tries again shortly.', 'form-message error'));
    }
  }

  // --- Launch radar: the newest Solana launches with their safety verdicts (every 60 s) ---
  async function radar() {
    try {
      const d = await request('/api/fusion/ai/memecoin/launches');
      const label = (v) => (v === 'rejected' ? 'Rejected' : v === 'watching' ? 'Watching' : v.startsWith('would') ? 'Would enter' : v);
      $('#mc-radar').replaceChildren(table(['Token', 'Launchpad', 'Liquidity', '5-min volume', 'Verdict', 'Why'],
        d.tokens.map((t) => [t.symbol || `${t.mint.slice(0, 6)}…`, t.launchpad || '—', t.liquidity_usd == null ? '—' : usd(t.liquidity_usd),
          t.volume_m5_usd == null ? '—' : usd(t.volume_m5_usd), label(t.verdict), (t.reasons || []).slice(0, 2).join(' · ') || '—'])));
      const rejected = d.tokens.filter((t) => t.verdict === 'rejected').length;
      $('#mc-radar-time').textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()} · ${rejected} of ${d.tokens.length} rejected on safety`;
    } catch (error) {
      $('#mc-radar').replaceChildren(element('p', error.message || 'The launch feed could not be read; it tries again shortly.', 'form-message error'));
    }
  }

  // --- Paper trading on the visitor's Alpaca account (server: /api/broker/alpaca/memecoin) ---
  const KEYS = 'loki_alpaca_keys_v1', STATE = 'loki_alpaca_memecoin_v1', CONFIRM = 'trade memecoins with paper money';
  const keys = () => { try { const v = JSON.parse(localStorage.getItem(KEYS) || 'null'); return v && v.key_id && v.secret_key ? { key_id: v.key_id, secret_key: v.secret_key } : null; } catch { return null; } };
  let state = (() => { try { return { running: false, log: [], ...(JSON.parse(localStorage.getItem(STATE) || 'null') || {}) }; } catch { return { running: false, log: [] }; } })();
  let busy = false, last = null, checkTimer = null, radarTimer = null;
  const save = () => { try { localStorage.setItem(STATE, JSON.stringify(state)); } catch { /* storage full */ } };
  const log = (text, kind = 'info') => { state.log.unshift({ t: new Date().toISOString(), text, kind }); state.log.length = Math.min(state.log.length, 150); save(); };
  const allocation = () => Math.min(25, Math.max(1, Number($('#mca-allocation').value) || 10));

  async function check({ start = false, stop = false } = {}) {
    const saved = keys();
    if (!saved || busy) return null;
    busy = true; renderPaper();
    try {
      const d = await request('/api/broker/alpaca/memecoin', { ...saved, confirm: CONFIRM, allocation_pct: allocation(), start, stop });
      last = d;
      state.failures = 0;
      if (d.stopped_for_loss && state.running) {
        state.running = false;
        log(`Daily loss stop: the paper account is ${d.day_change_pct.toFixed(2)}% down today. The bot's coins were sold and it is off until you start it again.`, 'bad');
      }
      for (const a of d.actions || []) {
        const what = a.did === 'sell' ? `SELL all ${a.coin}` : `BUY ${a.coin}${a.usd ? ` for US$${a.usd.toFixed(2)}` : ''}`;
        const key = `${new Date().toISOString().slice(0, 13)}:${a.coin}:${a.status}:${a.detail || ''}`;
        if (a.status !== 'sent') { if ((state.seen || {})[key]) continue; state.seen = { ...(state.seen || {}), [key]: 1 }; }
        log(`${what} · ${a.status}${a.detail ? `: ${a.detail}` : ''}${a.receipt ? ` · receipt ${a.receipt.hash.slice(0, 12)}` : ''}`, a.status === 'sent' ? 'good' : a.status === 'refused' ? 'bad' : 'info');
        try { if (a.receipt && window.Loki && window.Loki.audit) window.Loki.audit.append({ kind: a.receipt.action === 'blocked' ? 'guard' : 'paper', venue: 'alpaca-crypto', source: 'memecoin-bot', action: a.receipt.action, symbol: a.coin, usd: a.usd, receipt: a.receipt.hash, policy: d.policy_hash, trace_id: d.trace_id }); } catch { /* the log is a record only */ }
      }
      message('#mca-message', state.running ? `Running. Last check ${new Date().toLocaleTimeString()}.` : '', false);
      return d;
    } catch (error) {
      message('#mca-message', error.message || 'Alpaca could not be reached; it tries again.', true);
      state.failures = (state.failures || 0) + 1;
      if (state.running && state.failures === 5) log(`Five checks in a row failed (${error.message || 'no answer'}). It keeps trying; check the Alpaca dashboard if this goes on.`, 'bad');
      if (/rejected those keys|blocked trading|already holds/.test(error.message || '')) { state.running = false; log(error.message, 'bad'); }
      return null;
    } finally { busy = false; save(); renderPaper(); }
  }

  function renderPaper() {
    const saved = keys();
    $('#mca-status').textContent = state.running ? 'On' : 'Off';
    $('#mca-status').classList.toggle('good', state.running);
    $('#mca-start').disabled = busy || !saved || state.running;
    $('#mca-stop').disabled = busy || !saved;
    $('#mca-check').disabled = busy || !saved;
    if (!saved) $('#mca-intro').innerHTML = 'First save your Alpaca <strong>paper</strong> keys on the <a href="#real-market">Real account</a> page, then come back here.';
    else $('#mca-intro').textContent = `It may use up to ${allocation()}% of the paper account, split by the rule's own weights, and only cash (crypto cannot use margin).`;
    const out = $('#mca-out'); out.replaceChildren();
    if (last) {
      out.append(element('p', `Paper equity US$${last.equity.toLocaleString('en-US')} · cash for crypto US$${Math.round(last.crypto_buying_power).toLocaleString('en-US')} · rule: ${last.rule}`, 'integration-note'));
      out.append(table(['Coin', 'Rule', 'Held', 'This check'], last.coins.map((c) => [c.coin, c.rule === 'hold' ? 'Hold' : 'Cash', c.held_qty ? String(c.held_qty) : '0', c.action])));
    }
    $('#mca-log').replaceChildren(...(state.log.length ? state.log.slice(0, 50).map((l) => element('li', `${new Date(l.t).toLocaleString()} ${l.text}`, l.kind)) : [element('li', 'No memecoin orders yet.')]));
  }

  $('#mca-start').addEventListener('click', async () => {
    if (!window.confirm(`Start the memecoin bot on your Alpaca PAPER account?\n\nIt may use up to ${allocation()}% of the account in DOGE, SHIB, PEPE, BONK, WIF and TRUMP while this tab is open. Paper money only. This rule did not pass its own test; results are not guaranteed.`)) return;
    const d = await check({ start: true });
    if (!d) return;
    state.running = true; log(`Started on the Alpaca paper account with up to ${allocation()}% of equity.`, 'good'); save(); renderPaper();
  });
  $('#mca-stop').addEventListener('click', async () => {
    state.running = false; save();
    const d = await check({ stop: true });
    log(d ? `Stopped. ${(d.actions || []).filter((a) => a.did === 'sell').length} coin(s) sold.` : 'Stopped. Selling failed; check the Alpaca dashboard.', d ? 'info' : 'bad');
    renderPaper();
  });
  $('#mca-check').addEventListener('click', () => check());
  $('#mca-allocation').addEventListener('change', renderPaper);

  function onShow() {
    const visible = !root.classList.contains('tool-off') && !document.getElementById('ai-trading').hidden;
    if (visible && !loaded) { loaded = true; load(); scan(); radar(); renderPaper(); }
    if (visible && !timer) {
      timer = setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
      scanTimer = setInterval(() => { if (!document.hidden) scan(); }, 20 * 1000);
      radarTimer = setInterval(() => { if (!document.hidden) radar(); }, 60 * 1000);
      checkTimer = setInterval(() => { if (state.running) check(); }, 5 * 60 * 1000);
      if (state.running) check();
    }
    if (!visible && timer) { [timer, scanTimer, checkTimer, radarTimer].forEach(clearInterval); timer = scanTimer = checkTimer = radarTimer = null; }
  }
  new MutationObserver(onShow).observe(root, { attributes: true, attributeFilter: ['class'] });
  window.addEventListener('hashchange', () => setTimeout(onShow, 50));
  setTimeout(onShow, 0);
})();
