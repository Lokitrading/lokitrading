/* Memecoin paper bot (AI trading → Memecoins tab).
 *
 * Shows what the tested memecoin rule holds now, its tested record against simply holding the
 * coins, every rule that was tried, and its record since it was frozen. Paper only: it never
 * sends an order. It loads when its tab is first opened and refreshes every 5 minutes while open.
 */
'use strict';
(() => {
  const root = document.getElementById('memecoin');
  if (!root) return;
  const { $, element, request, table } = window.LokiWorkspace;
  const pct = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%`);
  const num = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));
  const price = (v) => (v == null ? '—' : v >= 1 ? v.toFixed(4) : v.toPrecision(4));
  let loaded = false, timer = null;

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
    $('#mc-verdict').replaceChildren(verdict);

    $('#mc-holdings').replaceChildren(
      element('p', `Right now · latest completed ${d.bars} bar · ${d.in_market_pct}% of the paper book in coins, the rest in cash`, 'eyebrow'),
      table(['Coin', 'Price (USDT)', 'Rule says', 'Share of book'], d.holdings.map((h) => [
        h.coin, price(h.price), h.status === 'no_data' ? 'no price' : h.holding ? 'Hold' : 'Cash', h.weight ? `${(h.weight * 100).toFixed(1)}%` : '0%'])));

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
    $('#mc-caveats').textContent = `${(rec.caveats || []).join(' ')} Paper only: this bot never sends an order. Not financial advice.`;
  }

  function onShow() {
    const visible = !root.classList.contains('tool-off') && !document.getElementById('ai-trading').hidden;
    if (visible && !loaded) { loaded = true; load(); }
    if (visible && !timer) timer = setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
    if (!visible && timer) { clearInterval(timer); timer = null; }
  }
  new MutationObserver(onShow).observe(root, { attributes: true, attributeFilter: ['class'] });
  window.addEventListener('hashchange', () => setTimeout(onShow, 50));
  setTimeout(onShow, 0);
})();
