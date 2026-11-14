/* AI trading page: one practice tool at a time.
 *
 * The Practise lane holds four tools (Day Trader, Portfolio AI, the single-market paper AI and
 * the weekly self-review). They used to be stacked in one long page; the tabs show one and
 * remember the choice. A tool that is not shown keeps running: only its panels are hidden.
 * Links to #day-trader, #portfolio-ai and #ai-evolution open the right tab.
 */
'use strict';
(() => {
  const tabs = document.getElementById('tool-tabs'), view = document.getElementById('ai-trading');
  if (!tabs || !view) return;
  const KEY = 'loki_trading_tool_v1';
  const buttons = [...tabs.querySelectorAll('[data-tool-tab]')];
  const panels = [...view.querySelectorAll(':scope > section[data-tool]')];
  const names = buttons.map((b) => b.dataset.toolTab);
  const ANCHORS = { 'day-trader': 'day', 'portfolio-ai': 'portfolio', 'ai-evolution': 'review' };

  function show(tool, remember = true) {
    if (!names.includes(tool)) tool = names[0];
    buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.toolTab === tool)));
    panels.forEach((p) => p.classList.toggle('tool-off', p.dataset.tool !== tool));
    if (remember) { try { localStorage.setItem(KEY, tool); } catch { /* private mode */ } }
    window.dispatchEvent(new Event('resize')); // charts drawn while hidden re-measure themselves
  }

  /** The tabs belong to the Practise lane only. */
  function syncLane() {
    const paper = view.querySelector('[data-trading-lane="paper"]');
    tabs.hidden = !!paper && paper.getAttribute('aria-pressed') !== 'true';
  }

  buttons.forEach((b) => b.addEventListener('click', () => show(b.dataset.toolTab)));
  view.querySelectorAll('[data-trading-lane]').forEach((b) => b.addEventListener('click', () => setTimeout(syncLane, 0)));
  document.addEventListener('click', (event) => {
    const link = event.target.closest && event.target.closest('a[href^="#"]');
    const tool = link && ANCHORS[link.getAttribute('href').slice(1)];
    if (tool) show(tool);
  });

  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch { /* private mode */ }
  show(ANCHORS[location.hash.slice(1)] || saved || names[0], false);
  setTimeout(syncLane, 0);
  window.LokiTradingTools = { show };
})();
