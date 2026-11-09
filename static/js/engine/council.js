/* THE COUNCIL — six bots predict where a trade goes next, and learn whom to trust.
 *
 * Each bot is a different trading philosophy computed only from data it could
 * have seen at the time. Their votes are combined with weights earned from
 * each bot's own track record (the Hedge / multiplicative-weights rule on
 * log-loss): a bot that keeps calling moves right gains influence, one that
 * keeps missing loses it. When a symbol is first opened, the council replays
 * its last ~250 decisions on history (walk-forward, no peeking) so the
 * weights start from evidence; afterwards every live call is stored and
 * scored when its horizon passes. Everything runs on this device.
 *
 * Calibration (2.57): markets rise more often than they fall, so a bot with no
 * view should say "the usual", not 50%. Each bot now states how far it
 * expects this market to depart from its own usual up-rate (the share of
 * past 5-bar windows that rose), in half-size steps. The record is scored
 * against that usual-rate guess, not a coin flip: on 10 years of 21 markets
 * the council's skill over it is about zero, and the page says so.
 *
 * It is a statistical engine with memory, not a conscious mind, and its
 * probabilities are estimates, not promises.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.council) return;
  const { clamp, mean, std, ema, rsi, logReturns, logLoss } = Loki.math;
  const STATE_KEY = 'loki_council_v2';
  const HORIZON = 5;          // bars ahead (5 trading days on daily bars)
  const ETA = 1.6;            // learning rate of the weight update
  const DECAY = 0.985;        // older mistakes fade, so the council keeps adapting
  const MIN_HISTORY = 70;

  const STEP = 0.5;           // bots move half as far from the usual rate as before: their edges are small
  let usual = 0.5;            // this market's usual up-rate, set by vote() before the bots run
  const squash = (score, span = 0.3) => clamp(usual + STEP * span * Math.tanh(score), 0.08, 0.92);
  const tail = (a, n) => a.slice(Math.max(0, a.length - n));

  /* ---------------------------------------------------------------- bots */
  const BOTS = [
    {
      id: 'trend', name: 'Trend Hawk', icon: '🦅', style: 'Rides established trends (EMA 20 vs 50, slope).',
      predict(b) {
        const c = b.map((x) => x.c), e20 = ema(c, 20), e50 = ema(c, 50);
        const vol = std(tail(logReturns(c), 20)) || 0.01;
        const gap = (e20[e20.length - 1] / e50[e50.length - 1] - 1) / (vol * Math.sqrt(10));
        const slope = (e20[e20.length - 1] / e20[e20.length - 6] - 1) / (vol * Math.sqrt(5));
        const s = 0.7 * gap + 0.5 * slope;
        return { p: squash(s), why: `EMA20 is ${gap >= 0 ? 'above' : 'below'} EMA50 and sloping ${slope >= 0 ? 'up' : 'down'}` };
      },
    },
    {
      id: 'revert', name: 'Reversion Owl', icon: '🦉', style: 'Fades stretched moves back toward the average (z-score, RSI).',
      predict(b) {
        const c = b.map((x) => x.c), w = tail(c, 20), m = mean(w), sd = std(w) || 1e-9;
        const z = (c[c.length - 1] - m) / sd, r = rsi(c, 14);
        const s = -0.55 * z - 0.03 * (r - 50);
        return { p: squash(s, 0.25), why: `price is ${z.toFixed(1)}σ from its 20-bar mean, RSI ${r.toFixed(0)}` };
      },
    },
    {
      id: 'momentum', name: 'Momentum Fox', icon: '🦊', style: 'Chases breakouts and 20-bar momentum.',
      predict(b) {
        const c = b.map((x) => x.c), last = b[b.length - 1], prior = b.slice(-21, -1);
        const vol = std(tail(logReturns(c), 30)) || 0.01;
        const roc = Math.log(c[c.length - 1] / c[c.length - 21]) / (vol * Math.sqrt(20));
        const hi = Math.max(...prior.map((x) => x.h)), lo = Math.min(...prior.map((x) => x.l));
        const brk = last.c > hi ? 1 : last.c < lo ? -1 : 0;
        return { p: squash(0.8 * roc + 0.9 * brk), why: brk ? `broke the 20-bar ${brk > 0 ? 'high' : 'low'}` : `20-bar momentum ${roc >= 0 ? '+' : ''}${roc.toFixed(2)}σ` };
      },
    },
    {
      id: 'vol', name: 'Volatility Bear', icon: '🐻', style: 'Reads fear: rising volatility below trend is a warning.',
      predict(b) {
        const c = b.map((x) => x.c), r = logReturns(c);
        const ratio = (std(tail(r, 10)) || 1e-9) / (std(tail(r, 60)) || 1e-9);
        const e50 = ema(c, 50), above = c[c.length - 1] > e50[e50.length - 1];
        const s = (above ? 0.35 : -0.45) - (ratio - 1) * (above ? 0.6 : 1.2);
        return { p: squash(s, 0.22), why: `short/long volatility ratio ${ratio.toFixed(2)} while ${above ? 'above' : 'below'} EMA50` };
      },
    },
    {
      id: 'swarm', name: 'Swarm Bee', icon: '🐝', style: 'Listens to the crowd: news, prediction markets, order books, volume flow.',
      predict(b, ctx) {
        const c = b.map((x) => x.c), v = b.map((x) => x.v || 0);
        let obv = 0; const flow = [];
        for (let i = Math.max(1, b.length - 30); i < b.length; i++) { obv += Math.sign(c[i] - c[i - 1]) * v[i]; flow.push(obv); }
        const avgVol = mean(tail(v, 30)) || 1;
        const flowScore = flow.length > 5 ? (flow[flow.length - 1] - flow[0]) / (avgVol * Math.sqrt(flow.length)) : 0;
        const live = ctx && Number.isFinite(ctx.sentiment) ? ctx.sentiment : null;
        const s = live == null ? 0.6 * flowScore : 0.35 * flowScore + 1.4 * live;
        return { p: squash(s, 0.25), why: live == null ? `volume flow ${flowScore >= 0 ? 'buying' : 'selling'} (${flowScore.toFixed(2)})` : `crowd sentiment ${live >= 0 ? '+' : ''}${live.toFixed(2)} with ${flowScore >= 0 ? 'buying' : 'selling'} flow` };
      },
    },
    {
      id: 'pattern', name: 'Pattern Raven', icon: '🐦‍⬛', style: 'Finds the 15 most similar past setups and counts what happened next.',
      predict(b) {
        const c = b.map((x) => x.c), r = logReturns(c), W = 10, K = 15;
        if (r.length < W + HORIZON + 30) return { p: 0.5, why: 'not enough history yet' };
        const sd = std(r) || 1e-9, now = tail(r, W).map((x) => x / sd);
        const cands = [];
        for (let i = W; i <= r.length - HORIZON - W; i++) {
          let d = 0;
          for (let j = 0; j < W; j++) { const diff = r[i - W + j] / sd - now[j]; d += diff * diff; }
          const fwd = Math.log(c[i + HORIZON] / c[i]);
          cands.push([d, fwd]);
        }
        cands.sort((a, z) => a[0] - z[0]);
        const best = cands.slice(0, K), ups = best.filter((x) => x[1] > 0).length;
        return { p: clamp((ups + 1) / (best.length + 2), 0.08, 0.92), why: `${ups} of the ${best.length} closest past setups rose over ${HORIZON} bars` };
      },
    },
  ];

  /** How often this market rose over HORIZON bars in its recent past (Laplace-smoothed). */
  function baseRate(b, lookback = 500) {
    let ups = 0, n = 0;
    for (let i = Math.max(0, b.length - 1 - lookback); i + HORIZON < b.length; i++) { ups += b[i + HORIZON].c > b[i].c ? 1 : 0; n++; }
    return clamp((ups + 1) / (n + 2), 0.3, 0.7);
  }

  /* --------------------------------------------------------------- state */
  const blank = () => ({ symbols: {}, global: Object.fromEntries(BOTS.map((x) => [x.id, { loss: 0, n: 0, hits: 0 }])), journal: [], version: 2 });
  let state = Loki.store.get(STATE_KEY, null);
  if (!state || state.version !== 2) state = blank();
  const save = () => {
    state.journal = state.journal.slice(-80);
    for (const sym of Object.keys(state.symbols)) state.symbols[sym].pending = (state.symbols[sym].pending || []).slice(-30);
    Loki.store.set(STATE_KEY, state);
  };
  const symbolState = (symbol) => (state.symbols[symbol] ||= { bots: Object.fromEntries(BOTS.map((x) => [x.id, { loss: 0, n: 0, hits: 0 }])), ens: { n: 0, hits: 0, brier: 0 }, pending: [], warmed: 0 });
  const reflect = (text, symbol) => { state.journal.push({ t: Date.now(), symbol, text }); Loki.emit('council:journal', { text, symbol }); };

  function weights(sym) {
    const s = symbolState(sym);
    const raw = BOTS.map((bot) => {
      const local = s.bots[bot.id], g = state.global[bot.id];
      const avgLocal = local.n ? local.loss / local.n : Math.LN2, avgGlobal = g.n ? g.loss / g.n : Math.LN2;
      const shrink = local.n / (local.n + 25); // little local evidence -> lean on the global record
      const avg = shrink * avgLocal + (1 - shrink) * avgGlobal;
      return Math.exp(-ETA * 12 * (avg - Math.LN2));
    });
    const total = raw.reduce((a, x) => a + x, 0) || 1;
    return Object.fromEntries(BOTS.map((bot, i) => [bot.id, raw[i] / total]));
  }

  function vote(bars, ctx, w) {
    usual = baseRate(bars);
    const votes = BOTS.map((bot) => { try { return { ...bot.predict(bars, ctx), id: bot.id }; } catch { return { id: bot.id, p: 0.5, why: 'abstained' }; } });
    const p = votes.reduce((a, v) => a + v.p * w[v.id], 0);
    const spread = Math.sqrt(votes.reduce((a, v) => a + w[v.id] * (v.p - p) ** 2, 0));
    const agreement = clamp(1 - spread / 0.2, 0, 1);
    // How far the council departs from the usual rate (bots step at most ~0.15 away), damped by disagreement.
    const confidence = clamp(Math.abs(p - usual) * 4 * (0.4 + 0.6 * agreement), 0, 1);
    return { votes, p, agreement, confidence, usual };
  }

  function score(sym, votes, pEns, y, base) {
    const s = symbolState(sym);
    for (const v of votes) {
      for (const rec of [s.bots[v.id], state.global[v.id]]) {
        rec.loss = rec.loss * DECAY + logLoss(v.p, y);
        rec.n = rec.n * DECAY + 1;
        rec.hits = rec.hits * DECAY + ((v.p >= 0.5) === Boolean(y) ? 1 : 0);
      }
    }
    s.ens.n += 1; s.ens.hits += (pEns >= 0.5) === Boolean(y) ? 1 : 0; s.ens.brier += (pEns - y) ** 2;
    // The honest yardstick: what always guessing this market's usual up-rate would have scored.
    if (Number.isFinite(base)) { s.ens.bn = (s.ens.bn || 0) + 1; s.ens.bb = (s.ens.bb || 0) + (base - y) ** 2; s.ens.mb = (s.ens.mb || 0) + (pEns - y) ** 2; }
  }

  /** Walk-forward replay of past decisions so the weights start from evidence. */
  function warmStart(symbol, bars) {
    const s = symbolState(symbol);
    const last = bars.length - HORIZON - 1;
    if (last < MIN_HISTORY) return 0;
    // First visit: replay the last ~250 decisions. Later visits: only the bars
    // added since, skipping any that a stored live call will score itself.
    const pendingT = new Set((s.pending || []).map((c) => c.t));
    const first = s.warmed ? bars.findIndex((b) => b.t > s.warmed) : Math.max(MIN_HISTORY, last - 250);
    if (first < 0 || first > last) return 0;
    let count = 0;
    for (let i = first; i <= last; i++) {
      if (pendingT.has(bars[i].t)) continue;
      const view = bars.slice(0, i + 1);
      const w = weights(symbol);
      const { votes, p, usual: base } = vote(view, null, w);
      const y = bars[i + HORIZON].c > bars[i].c ? 1 : 0;
      score(symbol, votes, p, y, base);
      count += 1;
    }
    s.warmed = bars[last].t;
    if (count >= 20) {
      const ranked = BOTS.map((b) => ({ b, hit: s.bots[b.id].n ? s.bots[b.id].hits / s.bots[b.id].n : 0.5 })).sort((a, z) => z.hit - a.hit);
      reflect(`Replayed ${count} past ${HORIZON}-bar calls on ${symbol}. I trust ${ranked[0].b.name} most (${Math.round(ranked[0].hit * 100)}% right) and ${ranked[ranked.length - 1].b.name} least (${Math.round(ranked[ranked.length - 1].hit * 100)}%).`, symbol);
    }
    return count;
  }

  /** Score live calls whose horizon has passed, and say what was learned. */
  function resolvePending(symbol, bars) {
    const s = symbolState(symbol), still = [];
    for (const call of s.pending) {
      const idx = bars.findIndex((b) => b.t >= call.t);
      if (idx < 0 || idx + HORIZON >= bars.length) { still.push(call); continue; }
      const entry = bars[idx].c, exit = bars[idx + HORIZON].c, y = exit > entry ? 1 : 0;
      const before = weights(symbol);
      score(symbol, call.votes, call.p, y, call.base);
      const after = weights(symbol);
      const right = call.votes.filter((v) => (v.p >= 0.5) === Boolean(y)).map((v) => BOTS.find((b) => b.id === v.id).name);
      const mover = BOTS.map((b) => [b, after[b.id] - before[b.id]]).sort((a, z) => Math.abs(z[1]) - Math.abs(a[1]))[0];
      reflect(`My ${new Date(call.t).toLocaleDateString()} call on ${symbol} (${Math.round(call.p * 100)}% up) was ${(call.p >= 0.5) === Boolean(y) ? 'right' : 'wrong'}: ${((exit / entry - 1) * 100).toFixed(1)}%. Right: ${right.join(', ') || 'nobody'}. ${mover[0].name}'s weight ${mover[1] >= 0 ? 'rose' : 'fell'} to ${Math.round(after[mover[0].id] * 100)}%.`, symbol);
    }
    s.pending = still;
  }

  /**
   * Full council read for a symbol. bars: oldest-first [{t,o,h,l,c,v}] (daily).
   * ctx.sentiment (-1..1) comes from the sentiment swarm when it is running.
   */
  function analyze(symbol, bars, ctx = {}) {
    if (!Array.isArray(bars) || bars.length < MIN_HISTORY) throw new Error(`The council needs at least ${MIN_HISTORY} bars of history for ${symbol}.`);
    const warmed = warmStart(symbol, bars);
    resolvePending(symbol, bars);
    const w = weights(symbol);
    const { votes, p, agreement, confidence, usual: base } = vote(bars, ctx, w);
    const s = symbolState(symbol), lastT = bars[bars.length - 1].t;
    if (!s.pending.some((c) => c.t === lastT)) s.pending.push({ t: lastT, p, base, votes: votes.map((v) => ({ id: v.id, p: v.p })) });
    save();
    // A call only counts as a view when it departs clearly from the usual rate.
    const verdict = p >= base + 0.06 ? 'BUY' : p <= base - 0.06 ? 'SELL' : 'HOLD';
    const ens = s.ens;
    return {
      symbol, p_up: p, confidence, agreement, verdict, horizon: HORIZON, warmed,
      price: bars[bars.length - 1].c, as_of: lastT,
      votes: votes.map((v) => {
        const bot = BOTS.find((b) => b.id === v.id), rec = s.bots[v.id];
        return { ...v, name: bot.name, icon: bot.icon, style: bot.style, weight: w[v.id], hit_rate: rec.n ? rec.hits / rec.n : null, calls: Math.round(rec.n) };
      }),
      usual: base,
      record: { calls: ens.n, hit_rate: ens.n ? ens.hits / ens.n : null, brier: ens.n ? ens.brier / ens.n : null,
        // Compared on the same calls: positive skill = better than always guessing the usual rate.
        usual_brier: ens.bn ? ens.bb / ens.bn : null, skill: ens.bn >= 30 ? 1 - ens.mb / ens.bb : null, compared: ens.bn || 0 },
      mood: mood(ens, confidence, bars, agreement),
    };
  }

  /** A one-line, data-derived self-description. Not feelings: a status report. */
  function mood(ens, confidence, bars, agreement = 0) {
    const hit = ens.n ? ens.hits / ens.n : 0.5;
    const vol = std(tail(logReturns(bars.map((b) => b.c)), 20)) * Math.sqrt(252);
    if (ens.n >= 30 && hit < 0.48) return { word: 'humbled', text: `I've been wrong more than right lately (${Math.round(hit * 100)}%), so I'm sizing smaller until I earn it back.` };
    if (vol > 0.6) return { word: 'cautious', text: `Volatility is high (${Math.round(vol * 100)}% annualised); my edges shrink in storms, so caution beats conviction.` };
    if (confidence > 0.35) return { word: 'confident', text: 'The bots mostly agree and the evidence is strong. Still an estimate, never a promise.' };
    if (agreement >= 0.5) return { word: 'uncertain', text: 'The bots mostly agree there is no clear edge: this looks close to the usual odds right now.' };
    return { word: 'uncertain', text: 'The bots disagree; the honest answer is that this is close to the usual odds right now.' };
  }

  /** Pure vote with caller-held weights (the replay sandbox learns its own). */
  function voteBars(bars, w, ctx) {
    const weightsIn = w || Object.fromEntries(BOTS.map((b) => [b.id, 1 / BOTS.length]));
    return vote(bars, ctx || null, weightsIn);
  }

  Loki.council = {
    BOTS, HORIZON, analyze, weights, voteBars,
    journal: () => state.journal.slice().reverse(),
    globalRecord: () => BOTS.map((b) => ({ id: b.id, name: b.name, icon: b.icon, calls: Math.round(state.global[b.id].n), hit_rate: state.global[b.id].n ? state.global[b.id].hits / state.global[b.id].n : null })),
    reset() { state = blank(); save(); },
    _state: () => state,
  };
})();
