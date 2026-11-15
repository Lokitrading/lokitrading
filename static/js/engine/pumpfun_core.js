/* pump.fun bot core: the same curve maths, features, rule and paper engine as loki_v8/pumpfun.py and
 * loki_v8/pumpfun_bot.py (LivePaper), so the bot in the browser is the bot that was tested.
 *
 * It is fed rows from /api/fusion/ai/pumpfun/stream (the server decodes pump.fun's program logs) and
 * runs on the rows' own timestamps: an order decided at one row lands at the first row latency_s
 * later, at that moment's reserves. Paper only; nothing here signs or sends a transaction.
 */
'use strict';
(() => {
  const LAMPORTS = 1e9;
  const UNIT = 1e6;
  const START_RTOKENS = 793100000 * UNIT;
  const SUPPLY = 1e9 * UNIT;
  const FEE_BPS = 125;
  const WINDOW_S = 30;
  const TRAIL_ARM = 0.2;
  const GRAD_HAIRCUT = 0.01;

  // --- curve maths: exact integers, as the program works them (the reserve product is ~3e25, far past
  // what a JavaScript number holds exactly) --------------------------------------------------------------
  const big = (x) => BigInt(Math.max(0, Math.round(Number(x) || 0)));
  const price = (vsol, vtok) => (vtok > 0 ? (vsol / LAMPORTS) / (vtok / UNIT) : 0);
  const progress = (rtok) => Math.max(0, Math.min(1, 1 - rtok / START_RTOKENS));
  const marketCap = (vsol, vtok) => price(vsol, vtok) * SUPPLY / UNIT;
  function buyTokens(vsol, vtok, rtok, lamportsIn, feeBps = FEE_BPS) {
    const S = big(vsol); const T = big(vtok);
    const spend = big(lamportsIn) * 10000n / (10000n + big(feeBps));
    if (S + spend === 0n) return 0;
    const out = T - (S * T) / (S + spend) - 1n;
    const cap = big(rtok);
    return Number(out < 0n ? 0n : out > cap ? cap : out);
  }
  function sellSol(vsol, vtok, tokensIn, feeBps = FEE_BPS) {
    const S = big(vsol); const T = big(vtok); const k = big(tokensIn);
    if (T + k === 0n) return 0;
    const gross = S - (S * T) / (T + k);
    const net = gross - gross * big(feeBps) / 10000n;
    return Number(net < 0n ? 0n : net);
  }

  // --- features (mirror of Tracker in loki_v8/pumpfun_bot.py) --------------------------------------------
  class Tracker {
    constructor(dev, createdAt, createdSlot, mayhem) {
      Object.assign(this, { dev, createdAt, createdSlot, mayhem: !!mayhem });
      this.recent = []; this.holdings = new Map(); this.bundleTokens = 0; this.devSold = false;
    }
    update(at, slot, buy, sol, user, tokens, vsol, vtok, rtok) {
      const px = price(vsol, vtok);
      if (buy) {
        this.holdings.set(user, (this.holdings.get(user) || 0) + tokens);
        if (slot <= this.createdSlot + 1) this.bundleTokens += tokens;
      } else {
        this.holdings.set(user, Math.max(0, (this.holdings.get(user) || 0) - tokens));
        if (user === this.dev) this.devSold = true;
      }
      this.recent.push([at, buy, sol, user, px]);
      while (this.recent.length && this.recent[0][0] < at - WINDOW_S) this.recent.shift();
      const buyers = new Set(); let flow = 0; let sells = 0;
      for (const [, b, s, u] of this.recent) { if (b && u !== this.dev) buyers.add(u); flow += b ? s : -s; if (!b) sells++; }
      let held = 0; let top = 0;
      for (const [u, v] of this.holdings) { if (u === this.dev) continue; held += v; if (v > top) top = v; }
      const oldPx = this.recent[0][4];
      return {
        age: at - this.createdAt, prog: progress(rtok), buyers30: buyers.size, flow30: flow / LAMPORTS,
        dev_sold: this.devSold ? 1 : 0, bundle: this.bundleTokens / SUPPLY, top_share: held ? top / held : 1,
        chg30: oldPx ? px / oldPx - 1 : 0, sells30: sells, mcap: marketCap(vsol, vtok), mayhem: this.mayhem ? 1 : 0,
      };
    }
  }

  /** The learned filter's inputs and predicted return (model_inputs / score in loki_v8/pumpfun_bot.py). */
  function modelInputs(f) {
    return [Math.min(f.age, 600) / 600, f.prog, Math.log1p(f.buyers30), Math.sign(f.flow30) * Math.log1p(Math.abs(f.flow30)),
      f.dev_sold, f.bundle, f.top_share, Math.min(5, Math.max(-1, f.chg30)), Math.log1p(f.sells30), Math.log(Math.max(f.mcap, 1e-3)), f.mayhem];
  }
  function score(model, f) {
    const x = modelInputs(f); let s = model.bias;
    for (let i = 0; i < x.length; i++) s += ((x[i] - model.mean[i]) / model.std[i]) * model.weights[i];
    return s;
  }
  /** The live entry test, the same as decide() in loki_v8/pumpfun_bot.py (with the learned score, if any). */
  function decide(f, r) {
    const ok = f.age <= r.age_max && f.prog >= r.prog_lo && f.prog <= r.prog_hi && f.buyers30 >= r.buyers_min
      && f.flow30 >= r.flow_min && f.bundle <= r.bundle_max && f.top_share <= r.top_max
      && (!r.dev_must_hold || f.dev_sold < 0.5)
      && ((r.mayhem || 'any') === 'any' || (f.mayhem > 0.5) === (r.mayhem === 'only'));
    return ok && (!r.model || score(r.model, f) >= r.model.threshold);
  }
  /** The first condition a launch fails right now, or that it meets the rule (for the live table). */
  function why(f, r) {
    if (f.age > r.age_max) return 'too old';
    if ((r.mayhem || 'any') === 'exclude' && f.mayhem > 0.5) return 'Mayhem mode';
    if (r.mayhem === 'only' && f.mayhem < 0.5) return 'not Mayhem mode';
    if (r.dev_must_hold && f.dev_sold > 0.5) return 'creator sold';
    if (f.bundle > r.bundle_max) return 'bundled launch';
    if (f.top_share > r.top_max) return 'one wallet holds too much';
    if (f.prog < r.prog_lo) return 'too early on the curve';
    if (f.prog > r.prog_hi) return 'too far up the curve';
    if (f.buyers30 < r.buyers_min) return 'too few buyers';
    if (f.flow30 < r.flow_min) return 'not enough buying';
    if (r.model && score(r.model, f) < r.model.threshold) return 'model expects a loss';
    return 'meets the rule';
  }

  /** Return on the stake if ``tokens`` were sold at this row's reserves (fees and transaction cost paid).
   * The stream leaves out our own paper buy, so its ``spend`` is added back, as in LivePaper._sale(). */
  function saleReturn(row, tokens, costs, spend) {
    const vs = row.vs + spend; const vt = row.vs * row.vt / vs;
    const net = sellSol(vs, vt, tokens, row.fee ?? FEE_BPS) / LAMPORTS - costs.tx_cost_sol;
    return net / costs.stake_sol - 1;
  }

  /** Paper trading in stream time: the same state machine as LivePaper in loki_v8/pumpfun_bot.py. */
  class PaperBot {
    constructor(rule, costs, maxOpen = 5) {
      Object.assign(this, { rule, costs, maxOpen });
      this.launches = new Map(); this.positions = new Map(); this.closed = []; this.events = [];
      this.failed = 0; this.seen = 0; this.now = 0;
    }
    note(text) { this.events.push({ at: this.now, text }); if (this.events.length > 200) this.events.shift(); }
    sale(row, pos) { return saleReturn(row, pos.tokens, this.costs, pos.spend); }
    close(mint, reason, ret) {
      const pos = this.positions.get(mint); this.positions.delete(mint);
      const net = ret - this.costs.tx_cost_sol / this.costs.stake_sol;
      const t = { mint, symbol: pos.symbol, reason, ret: net, pnl: net * this.costs.stake_sol, held: this.now - (pos.landed ?? this.now), at: this.now };
      this.closed.push(t);
      const la = this.launches.get(mint); if (la) la.verdict = `sold (${reason})`;
      this.note(`${pos.symbol}: sold, ${reason}, ${(net * 100).toFixed(1)}%`);
      return t;
    }
    due() {
      for (const [mint, pos] of [...this.positions]) {
        const la = this.launches.get(mint); if (!la || !la.state) continue;
        if (pos.status === 'pending' && this.now >= pos.landAt) {
          const st = la.state;
          if (la.done || price(st.vs, st.vt) > pos.decided * (1 + this.costs.max_slippage)) {
            this.positions.delete(mint); this.failed++;
            la.verdict = la.done ? 'graduated before the order landed' : 'buy failed: price ran past the slippage cap';
            this.note(`${pos.symbol}: buy failed (${la.done ? 'graduated first' : 'price moved too far'}), fee paid`);
            continue;
          }
          const lamports = Math.round(this.costs.stake_sol * LAMPORTS);
          Object.assign(pos, { status: 'open', landed: this.now, peak: -1, r: 0, spend: lamports * 10000 / (10000 + st.fee), tokens: buyTokens(st.vs, st.vt, st.rt, lamports, st.fee), entryMcap: marketCap(st.vs, st.vt) });
          la.verdict = 'holding';
          this.note(`${pos.symbol}: bought at ${pos.entryMcap.toFixed(1)} SOL market cap`);
        } else if (pos.status === 'open') {
          if (pos.exitAt == null && this.now >= pos.landed + this.rule.max_hold_s) Object.assign(pos, { exitAt: this.now + this.costs.latency_s, reason: 'time stop' });
          if (pos.exitAt != null && this.now >= pos.exitAt) this.close(mint, pos.reason, this.sale(la.state, pos));
        }
      }
    }
    handleRow(row) {
      this.now = Math.max(this.now, Number(row.at) || this.now);
      const { k, m } = row;
      if (k === 'c') {
        this.seen++;
        this.launches.set(m, { mint: m, symbol: row.sym || '?', created: this.now, dev: row.dev, mayhem: !!row.may, state: null, f: null, done: false, entered: false, verdict: 'waiting for its first trade', tracker: new Tracker(row.dev, this.now, row.slot, !!row.may) });
      } else if (k === 't' && this.launches.has(m)) {
        const la = this.launches.get(m); la.state = row;
        const f = la.f = la.tracker.update(this.now, row.slot, !!row.b, row.sol, row.u, row.tok, row.vs, row.vt, row.rt);
        const pos = this.positions.get(m); const r = this.rule;
        if (pos && pos.status === 'open' && pos.exitAt == null) {
          const ret = this.sale(row, pos); pos.r = ret; pos.peak = Math.max(pos.peak, ret);
          const reason = ret >= r.take_profit ? 'take profit' : ret <= r.stop_loss ? 'stop loss'
            : (!row.b && row.u === la.dev) ? 'creator sold'
              : (r.trail != null && pos.peak >= (r.trail_arm ?? TRAIL_ARM) && ret <= pos.peak - r.trail) ? 'trailing stop' : null;
          if (reason) Object.assign(pos, { exitAt: this.now + this.costs.latency_s, reason });
        } else if (!pos && !la.entered) {
          la.verdict = why(f, r);
          if (decide(f, r)) {
            la.entered = true;
            if (this.positions.size < this.maxOpen) {
              this.positions.set(m, { mint: m, symbol: la.symbol, status: 'pending', landAt: this.now + this.costs.latency_s, decided: price(row.vs, row.vt) });
              la.verdict = 'buying (order landing)';
            } else la.verdict = 'met the rule; 5 already open';
          }
        }
      } else if (k === 'x' && this.launches.has(m)) {
        const la = this.launches.get(m); la.done = true; la.verdict = 'graduated';
        const pos = this.positions.get(m);
        if (pos && pos.status === 'open' && la.state) this.close(m, 'graduated', (this.sale(la.state, pos) + 1) * (1 - GRAD_HAIRCUT) - 1);
      }
      this.due();
      if (this.launches.size > 2000) for (const [mint, la] of [...this.launches]) if (this.now - la.created > 900 && !this.positions.has(mint)) this.launches.delete(mint);
    }
    /** Sell everything still open at its last price (when the bot is stopped). */
    stopAll(reason) {
      for (const [mint, pos] of [...this.positions]) {
        const la = this.launches.get(mint);
        if (pos.status === 'open' && la && la.state) this.close(mint, reason, this.sale(la.state, pos)); else this.positions.delete(mint);
      }
    }
  }

  window.LokiPump = { LAMPORTS, START_RTOKENS, FEE_BPS, GRAD_HAIRCUT, price, progress, marketCap, buyTokens, sellSol, Tracker, decide, why, score, saleReturn, PaperBot };
})();
