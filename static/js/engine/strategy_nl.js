/* NATURAL-LANGUAGE STRATEGY BUILDER — a sentence becomes rules, code and a backtest.
 *
 * "Buy BTC when RSI 14 drops below 30 and price is above the 200 EMA, sell when
 *  RSI goes above 70 or after 48 bars, stop loss 3%, take profit 8%"
 * is parsed by a fixed grammar (no model guesses: anything not understood is
 * listed, never invented) into a typed rule set, printed as readable code, and
 * backtested bar by bar on real history with fees: entries fill at the NEXT
 * bar's open, so a signal never trades on the bar that produced it.
 * Grammar: RSI(n) above/below x · price above/below SMA/EMA(n) · SMA/EMA(a)
 * crosses above/below SMA/EMA(b) · breaks the n-bar high/low · price up/down
 * x% in n bars · volume above k× average · council above/below x% · after N
 * bars · stop loss / take profit / trailing stop x% · size x%.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.strategy) return;
  const FEE = 0.001; // 10 bp per side: taker fee + slippage

  const SYMBOLS = { bitcoin: 'BTC-USD', btc: 'BTC-USD', ethereum: 'ETH-USD', eth: 'ETH-USD', solana: 'SOL-USD', sol: 'SOL-USD', xrp: 'XRP-USD', doge: 'DOGE-USD' };
  const num = (s) => Number(String(s).replace(',', '.'));

  function parseConditions(text) {
    const conds = [], seen = [];
    const take = (re, fn) => { let m; const r = new RegExp(re.source, 'gi'); while ((m = r.exec(text))) { const c = fn(m); if (c) { conds.push(c); seen.push(m[0].trim()); } } };
    take(/rsi\s*\(?\s*(\d{1,3})?\s*\)?\s*(?:is\s+|goes\s+|drops\s+|falls\s+|rises\s+|crosses\s+)?(above|below|over|under|>|<)\s*(\d+(?:\.\d+)?)/, (m) => ({ type: 'rsi', n: +(m[1] || 14), op: /above|over|>/.test(m[2]) ? '>' : '<', value: num(m[3]) }));
    take(/(sma|ema|moving average|ma)\s*\(?\s*(\d{1,3})\s*\)?\s*(?:-?\s*(?:day|bar|period))?\s*(?:is\s+)?crosses\s+(above|below|over|under)\s+(?:the\s+)?(sma|ema|moving average|ma)?\s*\(?\s*(\d{1,3})/, (m) => ({ type: 'cross', fast: { kind: /ema/.test(m[1]) ? 'ema' : 'sma', n: +m[2] }, dir: /above|over/.test(m[3]) ? 'up' : 'down', slow: { kind: /ema/.test(m[4] || m[1]) ? 'ema' : 'sma', n: +m[5] } }));
    take(/(\d{1,3})\s*-?\s*(?:day\s+|bar\s+|period\s+)?(sma|ema|moving average|ma)\s+crosses\s+(above|below|over|under)\s+(?:the\s+)?(\d{1,3})\s*-?\s*(?:day\s+|bar\s+|period\s+)?(sma|ema|moving average|ma)?/, (m) => ({ type: 'cross', fast: { kind: /ema/.test(m[2]) ? 'ema' : 'sma', n: +m[1] }, dir: /above|over/.test(m[3]) ? 'up' : 'down', slow: { kind: /ema/.test(m[5] || m[2]) ? 'ema' : 'sma', n: +m[4] } }));
    take(/(?:price|close|it)\s+(?:is\s+|closes\s+|trades\s+)?(above|below|over|under)\s+(?:the\s+)?(\d{1,3})\s*(?:-?\s*(?:day|bar|period))?\s*(sma|ema|moving average|ma)/, (m) => ({ type: 'vs_ma', op: /above|over/.test(m[1]) ? '>' : '<', ma: { kind: /ema/.test(m[3]) ? 'ema' : 'sma', n: +m[2] } }));
    take(/(?:price|close|it)\s+(?:is\s+|closes\s+|trades\s+)?(above|below|over|under)\s+(?:the\s+)?(sma|ema|moving average|ma)\s*\(?\s*(\d{1,3})/, (m) => ({ type: 'vs_ma', op: /above|over/.test(m[1]) ? '>' : '<', ma: { kind: /ema/.test(m[2]) ? 'ema' : 'sma', n: +m[3] } }));
    take(/breaks?\s+(?:above\s+|below\s+)?(?:the\s+)?(\d{1,3})\s*-?\s*(?:day|bar|period)?\s*(high|low)/, (m) => ({ type: 'breakout', n: +m[1], side: m[2].toLowerCase() }));
    take(/(?:price\s+)?(?:is\s+)?(up|down|rises|falls|drops|gains|loses)\s+(?:more than\s+|over\s+|by\s+)?(\d+(?:\.\d+)?)\s*%\s+(?:in|over|within)\s+(\d{1,3})\s*(?:bars?|candles?|days?|hours?|minutes?)/, (m) => ({ type: 'change', dir: /up|rises|gains/.test(m[1]) ? 'up' : 'down', pct: num(m[2]), n: +m[3] }));
    take(/volume\s+(?:is\s+)?(?:above|over|more than|>)\s*(\d+(?:\.\d+)?)\s*(?:x|×|times)\s*(?:the\s+)?(?:(\d{1,3})\s*-?\s*(?:day|bar)?\s*)?(?:average|avg)/, (m) => ({ type: 'volume', mult: num(m[1]), n: +(m[2] || 20) }));
    take(/council\s+(?:confidence\s+|probability\s+|says\s+up\s+)?(?:is\s+)?(above|below|over|under)\s*(\d+(?:\.\d+)?)\s*%?/, (m) => ({ type: 'council', op: /above|over/.test(m[1]) ? '>' : '<', value: num(m[2]) / 100 }));
    take(/after\s+(\d{1,4})\s*(?:bars?|candles?)/, (m) => ({ type: 'bars_held', n: +m[1] }));
    return { conds, seen };
  }

  function parse(sentence) {
    const raw = String(sentence || '').slice(0, 600);
    const t = ' ' + raw.toLowerCase().replace(/\s+/g, ' ') + ' ';
    const out = { symbol: null, interval: '1h', entry: [], exit: [], stop: null, take: null, trailing: null, size: 1, notes: [] };
    const NOT = new Set(['when', 'the', 'if', 'at', 'a', 'on', 'and', 'it', 'only', 'every', 'each', 'after', 'once']);
    const sym = [...t.matchAll(/\b(?:buy|trade|long|on)\s+([a-z]{1,10}(?:-usd)?)\b/g)].map((x) => x[1]).find((w) => !NOT.has(w) && !/^\d/.test(w));
    if (sym) out.symbol = SYMBOLS[sym] || sym.toUpperCase();
    const iv = t.match(/\b(1m|5m|15m|1h|4h|1d|daily|hourly|minute|15[\s-]minute|4[\s-]hour)\b/);
    if (iv) out.interval = { daily: '1d', hourly: '1h', minute: '1m', '15 minute': '15m', '15-minute': '15m', '4 hour': '4h', '4-hour': '4h' }[iv[1]] || iv[1];
    // Split into the entry clause and the exit clause at the first "sell/exit/close".
    const split = t.search(/\b(sell|exit|close|get out)\b/);
    const entryText = split > 0 ? t.slice(0, split) : t, exitText = split > 0 ? t.slice(split) : '';
    const pe = parseConditions(entryText.replace(/stop[\s-]*loss[^,;.]*|take[\s-]*profit[^,;.]*|trailing[^,;.]*/g, ' '));
    const px = parseConditions(exitText.replace(/stop[\s-]*loss[^,;.]*|take[\s-]*profit[^,;.]*|trailing[^,;.]*/g, ' '));
    out.entry = pe.conds.filter((c) => c.type !== 'bars_held');
    out.exit = px.conds;
    out.exitAny = !/\band\b/.test(exitText.replace(/\b(stop|take|trailing)[^,;.]*/g, ''));
    let m = t.match(/stop[\s-]*loss\s*(?:of\s+|at\s+)?(\d+(?:\.\d+)?)\s*%/); if (m) out.stop = num(m[1]);
    m = t.match(/take[\s-]*profit\s*(?:of\s+|at\s+)?(\d+(?:\.\d+)?)\s*%/); if (m) out.take = num(m[1]);
    m = t.match(/trailing\s*(?:stop)?\s*(?:of\s+|at\s+)?(\d+(?:\.\d+)?)\s*%/); if (m) out.trailing = num(m[1]);
    m = t.match(/(?:size|risk|use|invest)\s*(?:of\s+)?(\d+(?:\.\d+)?)\s*%/); if (m) out.size = Math.min(1, Math.max(0.01, num(m[1]) / 100));
    if (!out.symbol) { out.symbol = 'BTC-USD'; out.notes.push('No symbol named: using BTC-USD.'); }
    if (!out.entry.length) out.notes.push('No entry condition understood. Try “buy when RSI 14 is below 30”.');
    if (!out.exit.length && out.stop == null && out.take == null && out.trailing == null) out.notes.push('No exit understood: positions would only close at the end of the test. Add “sell when …”, a stop loss or a take profit.');
    out.understood = [...pe.seen, ...px.seen];
    return out;
  }

  /* ----------------------------------------------------------- indicators */
  function sma(c, n) { const o = new Array(c.length).fill(null); let s = 0; for (let i = 0; i < c.length; i++) { s += c[i]; if (i >= n) s -= c[i - n]; if (i >= n - 1) o[i] = s / n; } return o; }
  function ema(c, n) { const o = new Array(c.length).fill(null), k = 2 / (n + 1); let e = null; for (let i = 0; i < c.length; i++) { e = e == null ? c[i] : c[i] * k + e * (1 - k); if (i >= n - 1) o[i] = e; } return o; }
  function rsi(c, n) {
    const o = new Array(c.length).fill(null); let g = 0, l = 0;
    for (let i = 1; i < c.length; i++) {
      const d = c[i] - c[i - 1], up = Math.max(0, d), dn = Math.max(0, -d);
      if (i <= n) { g += up / n; l += dn / n; if (i === n) o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l); continue; }
      g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; o[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l);
    }
    return o;
  }
  const ma = (c, spec, cache) => { const k = spec.kind + spec.n; return (cache[k] ||= (spec.kind === 'ema' ? ema : sma)(c, spec.n)); };

  function check(cond, i, bars, c, cache, ctx) {
    switch (cond.type) {
      case 'rsi': { const r = (cache['rsi' + cond.n] ||= rsi(c, cond.n))[i]; return r != null && (cond.op === '>' ? r > cond.value : r < cond.value); }
      case 'vs_ma': { const m = ma(c, cond.ma, cache)[i]; return m != null && (cond.op === '>' ? c[i] > m : c[i] < m); }
      case 'cross': { const f = ma(c, cond.fast, cache), s = ma(c, cond.slow, cache); if (i < 1 || f[i - 1] == null || s[i - 1] == null) return false; return cond.dir === 'up' ? f[i - 1] <= s[i - 1] && f[i] > s[i] : f[i - 1] >= s[i - 1] && f[i] < s[i]; }
      case 'breakout': { if (i < cond.n) return false; let hi = -Infinity, lo = Infinity; for (let k = i - cond.n; k < i; k++) { hi = Math.max(hi, bars[k].h); lo = Math.min(lo, bars[k].l); } return cond.side === 'high' ? c[i] > hi : c[i] < lo; }
      case 'change': { if (i < cond.n) return false; const ch = (c[i] / c[i - cond.n] - 1) * 100; return cond.dir === 'up' ? ch >= cond.pct : ch <= -cond.pct; }
      case 'volume': { if (i < cond.n) return false; let s = 0; for (let k = i - cond.n; k < i; k++) s += bars[k].v || 0; return (bars[i].v || 0) > cond.mult * (s / cond.n); }
      case 'council': { const p = ctx.councilP ? ctx.councilP[i] : null; return p != null && (cond.op === '>' ? p > cond.value : p < cond.value); }
      case 'bars_held': return ctx.held >= cond.n;
      default: return false;
    }
  }

  /** Bar-by-bar backtest: signal on bar i, fill at bar i+1's open, 10bp per side. */
  function backtest(rules, bars, opts = {}) {
    const c = bars.map((b) => b.c), cache = {}, trades = [], equity = [];
    let cash = 1, qty = 0, entry = 0, peak = 0, held = 0, eqPeak = 1, maxDD = 0;
    const start = Math.min(bars.length - 2, Math.max(1, opts.warmup ?? 50));
    for (let i = start; i < bars.length - 1; i++) {
      const next = bars[i + 1];
      if (qty > 0) {
        held += 1; peak = Math.max(peak, bars[i].h);
        const ctx = { held, councilP: opts.councilP };
        let why = null;
        if (rules.stop != null && bars[i].l <= entry * (1 - rules.stop / 100)) why = 'stop loss';
        else if (rules.take != null && bars[i].h >= entry * (1 + rules.take / 100)) why = 'take profit';
        else if (rules.trailing != null && bars[i].l <= peak * (1 - rules.trailing / 100)) why = 'trailing stop';
        else if (rules.exit.length && (rules.exitAny ? rules.exit.some((x) => check(x, i, bars, c, cache, ctx)) : rules.exit.every((x) => check(x, i, bars, c, cache, ctx)))) why = 'exit rule';
        if (why) { const px = next.o * (1 - FEE); cash += qty * px; trades.push({ in: entry, out: px, ret: px / entry - 1, bars: held, why, t: next.t }); qty = 0; held = 0; }
      } else if (rules.entry.length && rules.entry.every((x) => check(x, i, bars, c, cache, { held: 0, councilP: opts.councilP }))) {
        const px = next.o * (1 + FEE), spend = cash * rules.size; qty = spend / px; cash -= spend; entry = px; peak = px; held = 0;
      }
      const eq = cash + qty * next.c; equity.push({ t: next.t, v: eq });
      eqPeak = Math.max(eqPeak, eq); maxDD = Math.max(maxDD, 1 - eq / eqPeak);
    }
    if (qty > 0) { const px = bars[bars.length - 1].c * (1 - FEE); cash += qty * px; trades.push({ in: entry, out: px, ret: px / entry - 1, bars: held, why: 'end of test', t: bars[bars.length - 1].t }); qty = 0; }
    const wins = trades.filter((x) => x.ret > 0).length;
    return { ret: cash - 1, maxDD, trades: trades.length, winRate: trades.length ? wins / trades.length : 0, avgTrade: trades.length ? trades.reduce((a, x) => a + x.ret, 0) / trades.length : 0,
      buyHold: bars[bars.length - 1].c / bars[start].o - 1, list: trades.slice(-30), equity, bars: bars.length - start };
  }

  const fmtCond = (x) => ({
    rsi: () => `rsi(close, ${x.n})[i] ${x.op} ${x.value}`,
    vs_ma: () => `close[i] ${x.op} ${x.ma.kind}(close, ${x.ma.n})[i]`,
    cross: () => `crosses${x.dir === 'up' ? 'Above' : 'Below'}(${x.fast.kind}(close, ${x.fast.n}), ${x.slow.kind}(close, ${x.slow.n}), i)`,
    breakout: () => `close[i] ${x.side === 'high' ? '>' : '<'} ${x.side === 'high' ? 'highest(high' : 'lowest(low'}, ${x.n}, i - 1)`,
    change: () => `pctChange(close, ${x.n}, i) ${x.dir === 'up' ? '>=' : '<='} ${x.dir === 'up' ? '' : '-'}${x.pct}`,
    volume: () => `volume[i] > ${x.mult} * avg(volume, ${x.n}, i - 1)`,
    council: () => `council.p_up[i] ${x.op} ${x.value}`,
    bars_held: () => `barsHeld >= ${x.n}`,
  }[x.type] || (() => '/* ? */'))();

  /** Readable code for the rule set (documentation of exactly what the backtest runs). */
  function toCode(r) {
    const entry = r.entry.map(fmtCond).join(' &&\n      ') || 'false';
    const lines = [r.stop != null && [`low[i] <= entryPrice * ${(1 - r.stop / 100).toFixed(4)}`, `stop loss ${r.stop}%`],
      r.take != null && [`high[i] >= entryPrice * ${(1 + r.take / 100).toFixed(4)}`, `take profit ${r.take}%`],
      r.trailing != null && [`low[i] <= peak * ${(1 - r.trailing / 100).toFixed(4)}`, `trailing stop ${r.trailing}%`],
      r.exit.length && [`(${r.exit.map(fmtCond).join(r.exitAny ? ' || ' : ' && ')})`, 'exit rule']].filter(Boolean);
    const exits = lines.map(([e, c], k) => `${e}${k < lines.length - 1 ? ' ||' : ''}  // ${c}`).join('\n      ') || 'false';
    return `// ${r.symbol} · ${r.interval} bars · long only · fills at next bar open · 10bp per side\nfunction onBar(i) {\n  if (!inPosition) {\n    if (${entry})\n      buyNextOpen(${(r.size * 100).toFixed(0)}% of equity);\n  } else if (\n      ${exits}\n  ) sellNextOpen();\n}`;
  }

  async function loadBars(rules, limit = 1000) {
    const pair = Loki.data.binancePair ? Loki.data.binancePair(rules.symbol) : null;
    if (pair) return Loki.data.binanceKlines(pair, rules.interval, limit);
    return Loki.data.bars(rules.symbol, { interval: rules.interval === '1d' ? '1d' : '1h', limit: Math.min(300, limit) });
  }

  Loki.strategy = { parse, backtest, toCode, loadBars, indicators: { sma, ema, rsi }, check,
    EXAMPLES: ['Buy BTC on 1h when RSI 14 is below 30 and price is above the 200 EMA, sell when RSI goes above 65 or after 48 bars, stop loss 3%, take profit 8%',
      'Buy ETH 15m when the 20 EMA crosses above the 50 EMA and volume is above 1.5x average, sell when the 20 EMA crosses below the 50 EMA, trailing stop 2%',
      'Buy SOL 1h when price breaks the 24 bar high, stop loss 4%, take profit 10%, size 50%'] };
})();
