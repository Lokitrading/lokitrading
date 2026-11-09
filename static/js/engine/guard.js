/* BEHAVIOURAL GUARDRAILS — the "tilt detector" and circuit breakers.
 *
 *  - Tilt lockout: three consecutive max-loss trades (stopped out, or a loss at
 *    least as large as the strategy's planned maximum) lock that strategy out of
 *    simulated trading for 24 hours.
 *  - Strategy drift: every manual paper order is compared with what the rules
 *    and the agent jury said; overrides are logged as drift, with a drift rate.
 *  - Capital preservation: position size scales down with drawdown, from 100%
 *    at the equity peak to 25% at the drawdown limit.
 *  - Quarantine: a strategy with ≥5 trades in the last 30 days and a
 *    non-positive risk-adjusted return (Sharpe of its trade returns) is moved to
 *    quarantine until it is reviewed and released.
 *  - News lockout: automated entries are blocked 15 minutes before (and 5 after)
 *    high-impact releases (FOMC decisions from the Federal Reserve's calendar,
 *    US jobs reports on their usual first-Friday schedule, plus any event you add).
 * All state stays on this device.
 */
'use strict';
(() => {
  const Loki = window.Loki;
  if (!Loki || Loki.guard) return;
  const Q = Loki.quant;
  const KEY = 'loki_guard_v1';
  const LOCK_MS = 24 * 3600e3, STREAK = 3, WINDOW = 30 * 86400e3;
  const fresh = () => ({ strategies: {}, drift: [], custom: [], released: {} });
  let S = Loki.store.get(KEY, null) || fresh();
  const save = () => { S.drift = S.drift.slice(-200); Loki.store.set(KEY, S); Loki.emit('guard:update', snapshot()); };
  const strat = (id) => (S.strategies[id] ||= { trades: [], lockedUntil: 0, quarantined: false, quarantinedAt: 0, peak: null, equity: null });

  /** Record a closed trade. trade: { pnl, ret, maxLossPct?, exit?: 'stop'|..., t? } */
  function record(id, trade) {
    const s = strat(id), t = trade.t || Date.now();
    const maxLoss = trade.exit === 'stop' || (trade.maxLossPct != null && trade.ret <= -Math.abs(trade.maxLossPct) / 100 * 0.95);
    s.trades.push({ t, pnl: trade.pnl, ret: trade.ret, maxLoss, exit: trade.exit || null });
    s.trades = s.trades.slice(-300);
    const tail = s.trades.slice(-STREAK);
    if (tail.length === STREAK && tail.every((x) => x.maxLoss) && s.lockedUntil < t) {
      s.lockedUntil = t + LOCK_MS;
      Loki.emit('guard:tilt', { id, until: s.lockedUntil });
      Loki.audit && Loki.audit.append({ kind: 'paper', venue: 'guard', action: 'tilt-lockout', strategy: id, until: new Date(s.lockedUntil).toISOString() });
    }
    const q = quarantineCheck(id, t);
    if (q && !s.quarantined && !(S.released[id] > t - WINDOW)) { s.quarantined = true; s.quarantinedAt = t; Loki.emit('guard:quarantine', { id, ...q }); }
    save();
    return status(id, t);
  }

  function quarantineCheck(id, now = Date.now()) {
    const recent = strat(id).trades.filter((x) => now - x.t < WINDOW && Number.isFinite(x.ret));
    if (recent.length < 5) return null;
    const perf = Q.performance(recent.map((x) => x.ret), Math.max(12, recent.length * 12));
    return perf.sharpe == null || perf.sharpe <= 0 ? { trades: recent.length, sharpe: perf.sharpe, total: perf.total } : null;
  }

  function status(id, now = Date.now()) {
    const s = strat(id), last = s.trades.slice(-STREAK);
    let streak = 0; for (let i = s.trades.length - 1; i >= 0 && s.trades[i].maxLoss; i--) streak++;
    const locked = s.lockedUntil > now;
    return { id, locked, until: locked ? s.lockedUntil : null, streak, quarantined: s.quarantined, trades: s.trades.length, last,
      reason: locked ? `tilt lockout: ${STREAK} max-loss trades in a row; paused until ${new Date(s.lockedUntil).toLocaleString()}` : s.quarantined ? 'quarantined: no positive risk-adjusted return over the last 30 days' : null };
  }
  function release(id) { const s = strat(id); s.quarantined = false; s.lockedUntil = 0; S.released[id] = Date.now(); save(); }

  /** Capital preservation: 1 at the peak → 0.25 at the drawdown limit (and below). */
  function sizeScale(drawdown, limit = 0.1) { return Math.max(0.25, Math.min(1, 1 - 0.75 * Math.max(0, drawdown) / Math.max(1e-9, limit))); }

  /** Compare a manual paper order with what the rules / jury recommended. */
  function drift(entry) {
    // entry: { source, symbol, side, recommended: 'buy'|'sell'|'hold'|'blocked', why }
    const isDrift = entry.recommended && entry.recommended !== entry.side;
    S.drift.push({ t: Date.now(), ...entry, drift: Boolean(isDrift) });
    save();
    if (isDrift) Loki.emit('guard:drift', entry);
    return isDrift;
  }
  function driftStats() {
    const recent = S.drift.filter((d) => Date.now() - d.t < WINDOW);
    return { orders: recent.length, drifts: recent.filter((d) => d.drift).length, rate: recent.length ? recent.filter((d) => d.drift).length / recent.length : 0, log: S.drift.slice(-20).reverse() };
  }

  /* ----------------------------------------------------------- news lockout */
  let calendar = { events: [], at: 0, error: null };
  async function loadCalendar() {
    if (Date.now() - calendar.at < 3600e3 && calendar.events.length) return calendar;
    try { const d = await Loki.getJSON('/api/calendar/macro', { timeout: 15000 }); calendar = { events: d.events || [], at: Date.now(), error: null, note: d.note }; }
    catch (error) { calendar = { ...calendar, error: error.message, at: Date.now() }; }
    return calendar;
  }
  const allEvents = () => [...calendar.events, ...S.custom].map((e) => ({ ...e, ts: Date.parse(e.time) })).filter((e) => Number.isFinite(e.ts)).sort((a, z) => a.ts - z.ts);
  /** Locked when a high-impact event is within 15 minutes ahead or 5 minutes behind. */
  function newsLock(now = Date.now()) {
    const hit = allEvents().find((e) => e.ts - now <= 15 * 60e3 && now - e.ts <= 5 * 60e3);
    const next = allEvents().find((e) => e.ts > now);
    return { locked: Boolean(hit), event: hit || null, next: next || null, minutesToNext: next ? Math.round((next.ts - now) / 60e3) : null };
  }
  function addEvent(name, time) { if (!name || !Number.isFinite(Date.parse(time))) return false; S.custom.push({ name: String(name).slice(0, 60), time, impact: 'high', source: 'you', estimated: false }); S.custom = S.custom.filter((e) => Date.parse(e.time) > Date.now() - 86400e3).slice(-30); save(); return true; }
  function removeEvent(time) { S.custom = S.custom.filter((e) => e.time !== time); save(); }

  /** One gate for automated entries: tilt, quarantine and news. */
  function canEnter(id, now = Date.now()) {
    const st = status(id, now), news = newsLock(now);
    if (st.locked || st.quarantined) return { ok: false, why: st.reason };
    if (news.locked) return { ok: false, why: `news lockout: ${news.event.name} at ${new Date(news.event.ts).toLocaleTimeString()}` };
    return { ok: true, why: null };
  }

  function snapshot() {
    return { strategies: Object.keys(S.strategies).map((id) => ({ ...status(id), quarantine: quarantineCheck(id) })), drift: driftStats(), news: newsLock(), events: allEvents().filter((e) => e.ts > Date.now() - 3600e3).slice(0, 12), calendarError: calendar.error };
  }
  function reset() { S = fresh(); save(); }

  loadCalendar().then(() => Loki.emit('guard:update', snapshot()));
  Loki.guard = { record, status, release, sizeScale, drift, driftStats, newsLock, loadCalendar, addEvent, removeEvent, canEnter, snapshot, reset, STREAK, LOCK_MS };
})();
