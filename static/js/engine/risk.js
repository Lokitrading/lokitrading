/* SELF-HEALING RISK MATRIX — an automatic circuit breaker with dynamic limits.
 *
 * Drawdown limit = 2.5 × the account's own recent weekly volatility, bounded
 * to 3%–15%, so a quiet book trips early and a naturally swingy one is not
 * stopped by noise. Levels: GREEN (normal size), AMBER (half size, past half
 * the limit or the swarm alarmed), RED (breaker tripped: no new entries).
 *
 * When RED, exits are planned by liquidity, never by panic:
 *  - prediction-market tokens with a thin bid side are HEDGED by buying the
 *    opposite outcome token (YES + NO pays $1 for sure), instead of a market
 *    sell into an empty book;
 *  - stocks, crypto and futures with high estimated slippage are worked out
 *    with LIMIT orders at the mid in slices rather than market orders;
 *  - liquid positions are reduced by half.
 * Self-healing: after a 30-minute cooldown, once drawdown is back above the
 * amber line and the swarm is calm, size ramps back 25% → 50% → 100%.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.risk) return;
  const { clamp, std } = Loki.math;
  const KEY = 'loki_risk_matrix_v1';
  const COOLDOWN = 30 * 60000;
  const RAMP = [0.25, 0.5, 1];
  const RAMP_STEP = 60 * 60000;

  const persisted = Loki.store.get(KEY, { tripped_at: 0, healed_at: 0, history: [] });
  const freshState = () => ({ tripped_at: 0, healed_at: 0, history: [] });

  function weeklyVol(curve) {
    const values = curve.map((p) => p.v).filter((v) => v > 0);
    if (values.length < 6) return 0.03;
    const r = [];
    for (let i = 1; i < values.length; i++) r.push(Math.log(values[i] / values[i - 1]));
    const perStep = std(r.slice(-60)) || 0.004;
    const stepsPerWeek = clamp(5 * 86400000 / Math.max(60000, (curve[curve.length - 1].t - curve[0].t) / Math.max(1, curve.length - 1)), 1, 2000);
    return perStep * Math.sqrt(stepsPerWeek);
  }

  /**
   * @param {object} input
   *   curve: [{t, v}] equity over time (any spacing), oldest first
   *   positions: [{id, symbol, kind:'binary'|'stock'|'crypto'|'future', qty, value,
   *                exit_slippage_bps, opposite_ask, bid_depth_value}]
   *   swarm: Loki.swarm.snapshot() or null
   */
  function evaluate({ curve = [], positions = [], swarm = null, state = null, now = Date.now() } = {}) {
    // The replay sandbox passes its own state and clock; live use shares one saved breaker.
    const saved = state || persisted;
    const peak = curve.reduce((m, p) => Math.max(m, p.v), 0);
    const value = curve.length ? curve[curve.length - 1].v : 0;
    const drawdown = peak > 0 ? Math.max(0, 1 - value / peak) : 0;
    const limit = clamp(2.5 * weeklyVol(curve), 0.03, 0.15);
    const swarmMult = swarm ? swarm.sizeMultiplier : 1;
    const swarmEvent = Boolean(swarm && swarm.event);

    let level = drawdown >= limit ? 'red' : drawdown >= limit * 0.5 || swarmEvent ? 'amber' : 'green';
    if (level === 'red' && (!saved.tripped_at || saved.healed_at > saved.tripped_at)) {
      saved.tripped_at = now;
      saved.history.push({ t: now, event: 'tripped', drawdown, limit });
      if (!state) Loki.emit('risk:tripped', { drawdown, limit });
    }
    // Self-healing: stay tripped through the cooldown; release when conditions normalise.
    const tripped = saved.tripped_at > (saved.healed_at || 0);
    if (tripped && level !== 'red') {
      const calm = swarmMult > 0.8 && drawdown < limit * 0.5;
      if (now - saved.tripped_at >= COOLDOWN && calm) {
        saved.healed_at = now;
        saved.history.push({ t: now, event: 'healed', drawdown, limit });
        if (!state) Loki.emit('risk:healed', { drawdown });
      } else {
        level = 'red';
      }
    }
    // After healing, size climbs back in steps.
    let ramp = 1;
    if (saved.healed_at && saved.healed_at > saved.tripped_at) {
      ramp = RAMP[Math.min(RAMP.length - 1, Math.floor((now - saved.healed_at) / RAMP_STEP))];
    }
    const levelMult = { green: 1, amber: 0.5, red: 0 }[level];
    // The continuous Monte Carlo engine and the execution monitor can shrink size further.
    const mcMult = !state && Loki.mcrisk ? Loki.mcrisk.multiplier() : 1;
    const sizeMultiplier = clamp(levelMult * swarmMult * ramp * mcMult, 0, 1);

    const actions = [];
    if (level === 'red') {
      for (const p of positions) {
        const thin = (p.exit_slippage_bps || 0) > 80 || (p.bid_depth_value != null && p.bid_depth_value < Math.abs(p.value || 0) * 1.5);
        if (p.kind === 'binary' && thin && Number.isFinite(p.opposite_ask) && p.opposite_ask < 1) {
          actions.push({ type: 'hedge', id: p.id, symbol: p.symbol, qty: p.qty, price: p.opposite_ask,
            reason: `bid side too thin to sell (${Math.round(p.exit_slippage_bps || 0)}bp slippage); buying the opposite token at ${p.opposite_ask.toFixed(2)} locks the outcome instead` });
        } else if (thin) {
          actions.push({ type: 'limit-exit', id: p.id, symbol: p.symbol, qty: p.qty, slices: 4,
            reason: `estimated slippage ${Math.round(p.exit_slippage_bps || 0)}bp: work out with limit orders at the mid in 4 slices, no market orders` });
        } else {
          actions.push({ type: 'reduce', id: p.id, symbol: p.symbol, qty: Math.ceil(Math.abs(p.qty) / 2), reason: 'breaker tripped: halve the position while liquidity is fine' });
        }
      }
    }
    saved.history = saved.history.slice(-30);
    if (!state) Loki.store.set(KEY, saved);
    const out = { level, drawdown, limit, sizeMultiplier, ramp, swarmMult, tripped: level === 'red', actions, history: saved.history.slice().reverse(),
      reason: level === 'red' ? `drawdown ${(drawdown * 100).toFixed(1)}% hit the ${(limit * 100).toFixed(1)}% limit — breaker tripped, 30-minute cooldown`
        : level === 'amber' ? (swarmEvent ? 'the swarm reached a volatility quorum' : `drawdown ${(drawdown * 100).toFixed(1)}% is past half the limit`)
        : 'within limits' };
    if (!state) Loki.emit('risk:update', out);
    return out;
  }

  function reset() { persisted.tripped_at = 0; persisted.healed_at = 0; persisted.history.push({ t: Date.now(), event: 'manual reset' }); Loki.store.set(KEY, persisted); }

  Loki.risk = { evaluate, reset, freshState };
})();
