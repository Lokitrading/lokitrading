/* Memecoin scalper core (paper): exits, circuit breakers, triggers and an AUD book.
 *
 * The exit rules and breakers mirror loki/terminal/exits.py and breakers.py (the same test vectors
 * run against both). Fills use the exact pump.fun bonding-curve maths in pumpfun_core.js (LokiPump),
 * so a paper buy or sell gets the price the curve would really give, after the program fee, and each
 * leg also pays the Solana fees (base + priority, or a Jito tip) converted to AUD at the live rate.
 *
 * Triggers:
 *  - volume spike: a token's last-minute volume is 6x (a 500%+ surge) its average minute over the
 *    previous five, with at least twice as many unique buyers as the minute before, price rising;
 *  - whale radar: a watched wallet buys; the paper bot mirrors it at the next row's reserves.
 * Paper only: nothing here signs or sends a transaction.
 */
'use strict';
(() => {
  const P = window.LokiPump;
  const LAMPORTS = 1e9;
  const BASE_FEE = 5000;

  // --- exits (mirror of loki/terminal/exits.py) -----------------------------------------------------
  const DEFAULT_PLAN = { stopLoss: -0.25, tiers: [[2.0, 0.5]], trail: 0.25, trailArm: 0.30, staleAfterS: 180, staleMove: 0.05, maxHoldS: 1800 };
  function position(entryPrice, entryTime, plan = {}) {
    if (!(entryPrice > 0)) throw new Error('entry price must be positive');
    return { entryPrice, entryTime, plan: { ...DEFAULT_PLAN, ...plan }, remaining: 1, high: entryPrice, tiersDone: 0, moved: false, closed: null };
  }
  function step(pos, price, now) {
    if (pos.remaining <= 1e-12 || pos.closed) return [];
    const p = pos.plan; const out = [];
    const ret = price / pos.entryPrice - 1;
    pos.high = Math.max(pos.high, price);
    if (Math.abs(ret) >= p.staleMove) pos.moved = true;
    const sellAll = (why) => { out.push([pos.remaining, why]); pos.remaining = 0; pos.closed = why; return out; };
    if (ret <= p.stopLoss) return sellAll('stop loss');
    while (pos.tiersDone < p.tiers.length && price >= pos.entryPrice * p.tiers[pos.tiersDone][0]) {
      const [mult, share0] = p.tiers[pos.tiersDone]; const share = Math.min(share0, pos.remaining);
      pos.tiersDone += 1;
      if (share > 0) { pos.remaining -= share; out.push([share, `take profit ${mult}x`]); }
      if (pos.remaining <= 1e-12) { pos.closed = `take profit ${mult}x`; return out; }
    }
    const armed = pos.high >= pos.entryPrice * (1 + p.trailArm);
    if (p.trail != null && armed && price <= pos.high * (1 - p.trail)) return sellAll('trailing stop');
    const held = now - pos.entryTime;
    if (p.staleAfterS != null && held >= p.staleAfterS && !pos.moved) return sellAll('stale: no move');
    if (held >= p.maxHoldS) return sellAll('max hold time');
    return out;
  }

  // --- circuit breakers (mirror of loki/terminal/breakers.py) --------------------------------------
  const sydneyDay = (t) => new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: 'Australia/Sydney' });
  class Breaker {
    constructor(limits = {}) {
      this.limits = { maxDailyLossAud: 100, maxConsecutiveFailures: 3, maxTradeAud: 50, maxOpenPositions: 3, ...limits };
      this.day = ''; this.realisedAud = 0; this.failuresInRow = 0; this.tripped = null; this.open = 0;
    }
    roll(now) {
      const d = sydneyDay(now);
      if (d !== this.day) { this.day = d; this.realisedAud = 0; if (this.tripped && this.tripped.startsWith('daily loss')) this.tripped = null; }
    }
    canEnter(sizeAud, now) {
      this.roll(now); const L = this.limits; let why = null;
      if (this.tripped) why = `breaker tripped: ${this.tripped}`;
      else if (!(sizeAud > 0)) why = 'trade size must be positive';
      else if (sizeAud > L.maxTradeAud) why = `trade size A$${sizeAud.toFixed(2)} is over the A$${L.maxTradeAud.toFixed(2)} cap`;
      else if (this.open >= L.maxOpenPositions) why = `already ${this.open} open positions (cap ${L.maxOpenPositions})`;
      else if (this.realisedAud - sizeAud < -L.maxDailyLossAud) why = 'this trade could take the day past the daily loss limit';
      return { ok: why === null, why };
    }
    recordOpen() { this.open += 1; }
    recordClose(netAud, now) {
      this.roll(now); this.realisedAud += netAud; this.open = Math.max(0, this.open - 1);
      if (this.realisedAud <= -this.limits.maxDailyLossAud) this.tripped = `daily loss A$${(-this.realisedAud).toFixed(2)} reached the A$${this.limits.maxDailyLossAud.toFixed(2)} limit`;
    }
    recordTx(ok) {
      this.failuresInRow = ok ? 0 : this.failuresInRow + 1;
      if (this.failuresInRow >= this.limits.maxConsecutiveFailures) this.tripped = `${this.failuresInRow} failed transactions in a row`;
    }
    kill(reason = 'manual kill switch') { this.tripped = reason; }
    reset() { this.tripped = null; this.failuresInRow = 0; }
  }

  // --- triggers --------------------------------------------------------------------------------------
  class SpikeDetector {
    constructor(opts = {}) {
      this.o = { surge: 6, baseMinutes: 5, minBaseSol: 0.5, minMinuteSol: 5, minBuyers: 8, buyerGrowth: 2, ...opts };
      this.trades = new Map(); // mint -> [{t, sol, buyer, buy, price}]
    }
    add(row) {
      if (row.k !== 't') return;
      const list = this.trades.get(row.m) || [];
      list.push({ t: row.at, sol: row.sol / LAMPORTS, buyer: row.u, buy: !!row.b, price: P.price(row.vs, row.vt) });
      const keep = row.at - 60 * (this.o.baseMinutes + 1);
      while (list.length && list[0].t < keep) list.shift();
      this.trades.set(row.m, list);
    }
    check(mint, now) {
      const list = this.trades.get(mint) || [];
      if (!list.length || now - list[0].t < 60 * this.o.baseMinutes) return null; // needs a baseline
      const win = (a, b) => list.filter((x) => x.t > now - a && x.t <= now - b);
      const last = win(60, 0); const prev = win(120, 60);
      const vol = last.reduce((s, x) => s + x.sol, 0);
      let base = 0;
      for (let k = 1; k <= this.o.baseMinutes; k += 1) base += win(60 * (k + 1), 60 * k).reduce((s, x) => s + x.sol, 0);
      base = Math.max(this.o.minBaseSol, base / this.o.baseMinutes);
      const buyersNow = new Set(last.filter((x) => x.buy).map((x) => x.buyer)).size;
      const buyersPrev = new Set(prev.filter((x) => x.buy).map((x) => x.buyer)).size;
      const priceUp = last.length && prev.length ? last[last.length - 1].price > prev[prev.length - 1].price : false;
      const ok = vol >= this.o.surge * base && vol >= this.o.minMinuteSol && buyersNow >= this.o.minBuyers
        && buyersNow >= this.o.buyerGrowth * Math.max(1, buyersPrev) && priceUp;
      return ok ? { mint, reason: `volume spike: ${vol.toFixed(1)} SOL in a minute (${(vol / base).toFixed(1)}x), ${buyersNow} buyers vs ${buyersPrev}`, surge: vol / base } : null;
    }
  }

  class WhaleRadar {
    constructor(wallets = [], minSol = 0.5) { this.wallets = new Set(wallets.map((w) => w.trim()).filter(Boolean)); this.minSol = minSol; }
    check(row) {
      if (row.k !== 't' || !row.b || !this.wallets.has(row.u) || row.sol / LAMPORTS < this.minSol) return null;
      return { mint: row.m, reason: `whale ${row.u.slice(0, 4)}…${row.u.slice(-4)} bought ${(row.sol / LAMPORTS).toFixed(2)} SOL`, wallet: row.u };
    }
  }

  // Top-10 wallet share of total supply for a curve token, from every trade seen (for the rug shield).
  class Holders {
    constructor() { this.by = new Map(); }
    add(row) {
      if (row.k !== 't') return;
      const m = this.by.get(row.m) || new Map();
      m.set(row.u, (m.get(row.u) || 0) + (row.b ? 1 : -1) * row.tok);
      this.by.set(row.m, m);
    }
    top10Pct(mint) {
      const m = this.by.get(mint); if (!m) return null;
      const bal = [...m.values()].filter((v) => v > 0).sort((a, b) => b - a).slice(0, 10);
      return 100 * bal.reduce((s, v) => s + v, 0) / (1e9 * 1e6);
    }
  }

  // --- the AUD paper book ----------------------------------------------------------------------------
  class Book {
    constructor(opts = {}) {
      this.o = { startAud: 1000, txLamports: BASE_FEE + 100000, jitoTipLamports: 0, ...opts };
      this.cashAud = this.o.startAud; this.positions = new Map(); this.closed = []; this.feesAud = 0;
    }
    legFeeAud(solAud) { return (this.o.txLamports + this.o.jitoTipLamports) / LAMPORTS * solAud; }
    open(mint, symbol, sizeAud, st, solAud, now, plan, reason) {
      const lamports = Math.floor(sizeAud / solAud * LAMPORTS);
      const tokens = P.buyTokens(st.vs, st.vt, st.rt, lamports, st.fee ?? P.FEE_BPS);
      if (!(tokens > 0)) return null;
      const fee = this.legFeeAud(solAud);
      const entryPrice = (lamports / LAMPORTS) / (tokens / 1e6); // SOL per token actually paid
      const pos = { mint, symbol, reason, tokens, tokensLeft: tokens, spentAud: sizeAud, feesAud: fee, receivedAud: 0, solAudAtEntry: solAud,
        openedAt: now, plan: position(entryPrice, now, plan), last: entryPrice, sells: [] };
      this.cashAud -= sizeAud + fee; this.feesAud += fee;
      this.positions.set(mint, pos);
      return pos;
    }
    sell(mint, share, st, solAud, now, why) {
      const pos = this.positions.get(mint); if (!pos) return null;
      const tokens = Math.min(pos.tokensLeft, Math.floor(pos.tokens * share));
      if (tokens <= 0) return null;
      const sol = P.sellSol(st.vs, st.vt, tokens, st.fee ?? P.FEE_BPS) / LAMPORTS;
      const fee = this.legFeeAud(solAud); const aud = sol * solAud;
      pos.tokensLeft -= tokens; pos.receivedAud += aud; pos.feesAud += fee; this.feesAud += fee;
      this.cashAud += aud - fee;
      pos.sells.push({ at: now, why, tokens, aud });
      if (pos.tokensLeft <= 0) this.finish(mint, now, why);
      return { tokens, aud, fee };
    }
    finish(mint, now, why) {
      const pos = this.positions.get(mint); if (!pos) return null;
      const net = pos.receivedAud - pos.spentAud - pos.feesAud;
      const rec = { mint, symbol: pos.symbol, reason: pos.reason, exit: why, openedAt: pos.openedAt, closedAt: now,
        spentAud: pos.spentAud, receivedAud: pos.receivedAud, feesAud: pos.feesAud, netAud: net, netPct: 100 * net / pos.spentAud };
      this.closed.push(rec); this.positions.delete(mint);
      return rec;
    }
    markAud(mint, st, solAud) {
      const pos = this.positions.get(mint); if (!pos || pos.tokensLeft <= 0) return 0;
      return P.sellSol(st.vs, st.vt, pos.tokensLeft, st.fee ?? P.FEE_BPS) / LAMPORTS * solAud;
    }
    stats() {
      const n = this.closed.length; const net = this.closed.reduce((s, r) => s + r.netAud, 0);
      const wins = this.closed.filter((r) => r.netAud > 0).length;
      return { trades: n, netAud: net, winRate: n ? 100 * wins / n : null, feesAud: this.feesAud, cashAud: this.cashAud };
    }
  }

  window.LokiScalp = { DEFAULT_PLAN, position, step, Breaker, SpikeDetector, WhaleRadar, Holders, Book, sydneyDay };
})();
