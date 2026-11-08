/* STRATEGY CO-PILOT — plain-language commands become exact config changes.
 *
 * "Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction
 * positions if volatility spikes above 40%" becomes two typed changes:
 *   trailing_stop_pct 5 → 3.5   and   hedge_rule {enabled: true, vol_threshold_pct: 40}
 * shown as a diff plus the resulting JSON before anything is applied. The
 * parser is deterministic (no model guesses): if a sentence is not understood
 * it says so instead of doing something else. Applied configs are versioned,
 * so every change can be undone.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.copilot) return;
  const KEY = 'loki_strategy_config_v1', HISTORY = 'loki_strategy_config_history_v1';
  const DEFAULTS = Object.freeze({
    trailing_stop_pct: 5, stop_loss_pct: 8, take_profit_pct: 15, max_position_pct: 5, max_invested_pct: 30,
    daily_loss_limit_pct: 3, min_council_confidence: 15, min_edge_pts: 3, autopilot: false, risk_mode: 'normal',
    hedge_rule: { enabled: false, vol_threshold_pct: 40 }, swarm_sensitivity: 1, watchlist: ['SPY', 'QQQ', 'AAPL', 'NVDA', 'BTC-USD', 'ETH-USD'],
  });
  const LIMITS = { trailing_stop_pct: [0.5, 30], stop_loss_pct: [0.5, 50], take_profit_pct: [1, 200], max_position_pct: [0.5, 25], max_invested_pct: [5, 100], daily_loss_limit_pct: [0.5, 20], min_council_confidence: [0, 90], min_edge_pts: [0.5, 20], swarm_sensitivity: [0.25, 3] };
  const LABEL = { trailing_stop_pct: 'Trailing stop distance', stop_loss_pct: 'Stop-loss', take_profit_pct: 'Take-profit', max_position_pct: 'Max size per trade', max_invested_pct: 'Max invested', daily_loss_limit_pct: 'Daily loss limit', min_council_confidence: 'Min council confidence', min_edge_pts: 'Min edge (prediction bots)', autopilot: 'Paper autopilot', risk_mode: 'Risk mode', hedge_rule: 'Hedge rule', swarm_sensitivity: 'Swarm sensitivity', watchlist: 'Watchlist' };
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const config = () => ({ ...clone(DEFAULTS), ...Loki.store.get(KEY, {}) });
  const num = (s) => Number(String(s).replace(',', '.'));
  const bound = (key, v) => { const [lo, hi] = LIMITS[key] || [-Infinity, Infinity]; return Math.round(Math.min(hi, Math.max(lo, v)) * 100) / 100; };
  const NUMBER = '(-?\\d+(?:[.,]\\d+)?)';

  function adjust(text, phrase, key, out, cfg) {
    // "<verb> <phrase> (down|up|to|by) N%" in several word orders
    const re = new RegExp(`(shift|move|lower|raise|reduce|increase|tighten|loosen|widen|set|change|make|put|bump|drop|cut)?\\s*(?:the\\s+|my\\s+)?(?:dynamic\\s+)?(?:${phrase})\\s*(?:distance\\s*)?(?:to\\s+be\\s+)?(down|up|lower|higher|tighter|wider|to|at|by|=|of)?\\s*(?:by\\s+)?${NUMBER}\\s*(%|percent|pts?|points?)?`, 'i');
    const m = text.match(re);
    if (!m) return;
    const verb = (m[1] || '').toLowerCase(), dir = (m[2] || '').toLowerCase(), value = num(m[3]);
    if (!Number.isFinite(value)) return;
    const down = ['down', 'lower', 'tighter', 'reduce', 'lower', 'tighten', 'drop', 'cut'].includes(dir) || ['lower', 'reduce', 'tighten', 'drop', 'cut'].includes(verb);
    const up = ['up', 'higher', 'wider'].includes(dir) || ['raise', 'increase', 'loosen', 'widen', 'bump'].includes(verb);
    const relative = (dir === 'by' || down || up) && dir !== 'to' && dir !== 'at' && dir !== '=';
    const from = cfg[key];
    const to = bound(key, relative ? from + (down ? -1 : 1) * Math.abs(value) : Math.abs(value));
    out.push({ key, from, to, why: relative ? `${down ? 'down' : 'up'} by ${Math.abs(value)}` : `set to ${Math.abs(value)}` });
  }

  /** Parse a sentence into changes. Never applies anything. */
  function parse(text) {
    const t = ` ${String(text || '').toLowerCase().replace(/\s+/g, ' ')} `;
    const cfg = config(), changes = [];
    adjust(t, 'trailing[\\s-]*stop(?:[\\s-]*loss)?', 'trailing_stop_pct', changes, cfg);
    if (!/trailing/.test(t)) adjust(t, 'stop[\\s-]*loss|stop', 'stop_loss_pct', changes, cfg);
    adjust(t, 'take[\\s-]*profit|profit target|target', 'take_profit_pct', changes, cfg);
    adjust(t, '(?:max(?:imum)?\\s+)?(?:position|trade)\\s*size|risk per trade|size per trade', 'max_position_pct', changes, cfg);
    adjust(t, '(?:max(?:imum)?\\s+)?(?:invested|exposure|total exposure)', 'max_invested_pct', changes, cfg);
    adjust(t, 'daily loss(?:\\s*limit)?|max daily loss', 'daily_loss_limit_pct', changes, cfg);
    adjust(t, '(?:min(?:imum)?\\s+)?edge', 'min_edge_pts', changes, cfg);
    let m = t.match(/(?:council\s+)?confidence\s*(?:is\s+)?(?:above|over|>=?|at least|of)\s*(\d+(?:\.\d+)?)\s*%?/);
    if (m) changes.push({ key: 'min_council_confidence', from: cfg.min_council_confidence, to: bound('min_council_confidence', num(m[1])), why: 'trade only above this council confidence' });
    m = t.match(/hedge[^.]*?(?:volatility|vol|vix)[^.]*?(?:above|over|exceeds?|>)\s*(\d+(?:\.\d+)?)\s*%?/);
    if (m) changes.push({ key: 'hedge_rule', from: cfg.hedge_rule, to: { enabled: true, vol_threshold_pct: Math.min(300, Math.max(5, num(m[1]))) }, why: 'buy the opposite outcome token on prediction positions when annualised volatility crosses this line' });
    else if (/(stop|disable|turn off|cancel)\s+(the\s+)?hedg/.test(t)) changes.push({ key: 'hedge_rule', from: cfg.hedge_rule, to: { ...cfg.hedge_rule, enabled: false }, why: 'hedge rule off' });
    if (/\b(pause|stop|halt|disable)\b[^.]*\b(bot|autopilot|trading|auto)/.test(t) && !/stop[\s-]*loss/.test(t)) changes.push({ key: 'autopilot', from: cfg.autopilot, to: false, why: 'paper autopilot off' });
    else if (/\b(resume|start|enable|turn on)\b[^.]*\b(bot|autopilot|trading|auto)/.test(t)) changes.push({ key: 'autopilot', from: cfg.autopilot, to: true, why: 'paper autopilot on (live orders always need your confirmation)' });
    if (/risk[\s-]*off|defensive|play it safe|be careful/.test(t)) changes.push({ key: 'risk_mode', from: cfg.risk_mode, to: 'defensive', why: 'half size, tighter stops' });
    else if (/risk[\s-]*on|aggressive/.test(t)) changes.push({ key: 'risk_mode', from: cfg.risk_mode, to: 'aggressive', why: 'full size within the hard caps' });
    else if (/normal risk|risk normal|back to normal/.test(t)) changes.push({ key: 'risk_mode', from: cfg.risk_mode, to: 'normal', why: 'standard sizing' });
    m = t.match(/swarm\s+(more|less)\s+sensitive|make the swarm (more|less) sensitive/);
    if (m) { const more = (m[1] || m[2]) === 'more'; changes.push({ key: 'swarm_sensitivity', from: cfg.swarm_sensitivity, to: bound('swarm_sensitivity', cfg.swarm_sensitivity * (more ? 1.5 : 0.67)), why: more ? 'react to smaller shocks' : 'ignore smaller shocks' }); }
    m = t.match(/\badd\s+([a-z0-9.^=\-,\s]+?)\s+to\s+(?:my\s+|the\s+)?watch\s*list/);
    if (m) { const add = m[1].split(/,|\band\b|\s+/).map((s) => s.trim().toUpperCase()).filter((s) => /^[A-Z0-9.^=\-]{1,12}$/.test(s)); if (add.length) changes.push({ key: 'watchlist', from: cfg.watchlist, to: [...new Set([...cfg.watchlist, ...add])].slice(0, 20), why: `add ${add.join(', ')}` }); }
    m = t.match(/\bremove\s+([a-z0-9.^=\-,\s]+?)\s+from\s+(?:my\s+|the\s+)?watch\s*list/);
    if (m) { const drop = m[1].split(/,|\band\b|\s+/).map((s) => s.trim().toUpperCase()).filter(Boolean); changes.push({ key: 'watchlist', from: cfg.watchlist, to: cfg.watchlist.filter((s) => !drop.includes(s)), why: `remove ${drop.join(', ')}` }); }
    if (/reset (everything|all|the config|settings|to defaults?)/.test(t)) return { changes: Object.keys(DEFAULTS).filter((k) => JSON.stringify(cfg[k]) !== JSON.stringify(DEFAULTS[k])).map((k) => ({ key: k, from: cfg[k], to: clone(DEFAULTS[k]), why: 'default' })), config: clone(DEFAULTS) };
    const after = clone(cfg);
    for (const c of changes) after[c.key] = clone(c.to);
    const real = changes.filter((c) => JSON.stringify(c.from) !== JSON.stringify(c.to));
    return { changes: real.map((c) => ({ ...c, label: LABEL[c.key] || c.key })), config: after, understood: changes.length > 0 };
  }

  function apply(parsed) {
    if (!parsed || !parsed.changes || !parsed.changes.length) return config();
    const history = Loki.store.get(HISTORY, []);
    history.push({ t: Date.now(), before: config(), changes: parsed.changes });
    Loki.store.set(HISTORY, history.slice(-30));
    Loki.store.set(KEY, parsed.config);
    Loki.emit('config:change', parsed.config);
    return parsed.config;
  }
  function undo() {
    const history = Loki.store.get(HISTORY, []);
    const last = history.pop();
    if (!last) return null;
    Loki.store.set(HISTORY, history);
    Loki.store.set(KEY, last.before);
    Loki.emit('config:change', last.before);
    return last;
  }
  /** Effective sizing multiplier from the risk mode (the risk matrix multiplies on top). */
  const modeMultiplier = () => ({ defensive: 0.5, normal: 1, aggressive: 1 }[config().risk_mode] || 1);

  Loki.config = config;
  Loki.copilot = { parse, apply, undo, DEFAULTS, LABEL, modeMultiplier, history: () => Loki.store.get(HISTORY, []).slice().reverse(),
    EXAMPLES: ['Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction positions if volatility spikes above 40%', 'Set take profit to 12% and max position size 3%', 'Only trade when council confidence is above 30%', 'Go defensive and pause the autopilot', 'Add TSLA and AMD to my watchlist', 'Daily loss limit 2%'] };
})();
