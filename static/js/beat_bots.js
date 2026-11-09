/* BEAT THE BOTS — you vs Loki's six-bot council on real, hidden market history.
 *
 * Each round shows 90 real daily candles from a random crypto market at a
 * random date, with the name and date hidden. You call UP or DOWN for the
 * next 5 days (and how sure you are); the council calls the same chart with
 * the same information. Then the future candles are revealed one by one.
 * Sure calls pay double and cost points when wrong. Ten rounds a match.
 * Lifeline: "Ask a bot" reveals one bot's opinion for 3 points.
 * Data comes straight from Binance's public history to this device.
 */
'use strict';
(() => {
  const root = document.getElementById('bb-game');
  const Loki = window.Loki;
  if (!root || !Loki || !Loki.council) return;
  const $ = (id) => document.getElementById(id);
  const F = Loki.fmt;
  const MARKETS = [['BTCUSDT', 'Bitcoin'], ['ETHUSDT', 'Ethereum'], ['SOLUSDT', 'Solana'], ['XRPUSDT', 'XRP'], ['BNBUSDT', 'BNB'], ['DOGEUSDT', 'Dogecoin'], ['ADAUSDT', 'Cardano'], ['AVAXUSDT', 'Avalanche'], ['LINKUSDT', 'Chainlink'], ['LTCUSDT', 'Litecoin']];
  const ROUNDS = 10, SHOW = 90, AHEAD = 5, HISTORY = 160;
  const cache = new Map();
  let match = null, busy = false;

  const best = () => { try { return JSON.parse(localStorage.getItem('loki_bb_best_v1')) || { score: null, wins: 0, matches: 0 }; } catch { return { score: null, wins: 0, matches: 0 }; } };
  const saveBest = (b) => { try { localStorage.setItem('loki_bb_best_v1', JSON.stringify(b)); } catch { /* blocked */ } };
  const points = (call, sure, up) => (call === (up ? 'up' : 'down') ? (sure ? 20 : 10) : (sure ? -10 : 0));

  async function series(pair) {
    if (!cache.has(pair)) cache.set(pair, Loki.data.binanceKlines(pair, '1d', 1000));
    return cache.get(pair);
  }

  async function newRound() {
    const [pair, name] = MARKETS[Math.floor(Math.random() * MARKETS.length)];
    const bars = await series(pair);
    if (bars.length < HISTORY + AHEAD + 20) throw new Error('not enough history came back');
    const cut = HISTORY + Math.floor(Math.random() * (bars.length - HISTORY - AHEAD - 1));
    const past = bars.slice(0, cut + 1), future = bars.slice(cut + 1, cut + 1 + AHEAD);
    const { votes, p } = Loki.council.voteBars(past.slice(-HISTORY));
    return { pair, name, past, future, p, votes, at: bars[cut].t, call: null, sure: false, asked: null };
  }

  function draw(round, revealed) {
    const c = $('bb-chart'), ctx = c.getContext('2d'), dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth || 640, h = 280;
    c.width = w * dpr; c.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const shown = round.past.slice(-SHOW), fut = round.future.slice(0, revealed);
    const all = shown.concat(round.future);
    const lo = Math.min(...all.map((b) => b.l)), hi = Math.max(...all.map((b) => b.h));
    const n = SHOW + AHEAD, cw = (w - 20) / n, y = (v) => 10 + (1 - (v - lo) / ((hi - lo) || 1)) * (h - 30);
    const candle = (b, i, ghost) => {
      const x = 10 + i * cw + cw / 2, up = b.c >= b.o;
      ctx.strokeStyle = ctx.fillStyle = ghost ? (up ? '#9ec5ff' : '#c3a6ff') : up ? '#6fe3a1' : '#ff7a66';
      ctx.beginPath(); ctx.moveTo(x, y(b.h)); ctx.lineTo(x, y(b.l)); ctx.stroke();
      const top = y(Math.max(b.o, b.c)), bh = Math.max(1, Math.abs(y(b.o) - y(b.c)));
      ctx.fillRect(x - cw * 0.35, top, cw * 0.7, bh);
    };
    shown.forEach((b, i) => candle(b, i, false));
    const xCut = 10 + SHOW * cw;
    ctx.strokeStyle = 'rgba(255,210,122,.6)'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(xCut, 6); ctx.lineTo(xCut, h - 14); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,210,122,.08)'; ctx.fillRect(xCut, 6, w - xCut - 6, h - 20);
    ctx.font = '11px ui-monospace, monospace'; ctx.fillStyle = '#ffd27a'; ctx.textAlign = 'right'; ctx.fillText(revealed ? 'the future →' : 'next 5 days? →', xCut - 6, 20); ctx.textAlign = 'left';
    fut.forEach((b, i) => candle(b, SHOW + i, true));
    const entry = shown[shown.length - 1].c;
    ctx.strokeStyle = 'rgba(231,240,255,.35)'; ctx.beginPath(); ctx.moveTo(10, y(entry)); ctx.lineTo(w - 10, y(entry)); ctx.stroke();
  }

  function renderScore() {
    const m = match, b = best();
    $('bb-score').innerHTML = m ? `<div><span>You</span><b class="${m.you >= m.bots ? 'up' : ''}">${m.you}</b></div><div><span>The council</span><b class="${m.bots > m.you ? 'down' : ''}">${m.bots}</b></div><div><span>Round</span><b>${Math.min(m.i + 1, ROUNDS)}/${ROUNDS}</b></div><div><span>Your streak</span><b>${m.streak}</b></div>`
      : `<div><span>Best score</span><b>${b.score ?? '—'}</b></div><div><span>Matches won vs the bots</span><b>${b.wins}/${b.matches}</b></div>`;
  }

  async function nextRound() {
    if (busy) return; busy = true;
    $('bb-status').textContent = 'Pulling a random slice of real market history…';
    $('bb-calls').hidden = true; $('bb-reveal').innerHTML = ''; $('bb-next').hidden = true;
    try {
      match.round = await newRound();
      draw(match.round, 0);
      $('bb-status').textContent = `Mystery market, mystery date. Will it be higher or lower in ${AHEAD} days than at the yellow line?`;
      $('bb-calls').hidden = false; $('bb-ask').disabled = false; $('bb-ask-out').textContent = '';
    } catch (error) {
      $('bb-status').textContent = `Could not load history (${error.message}). Check your connection and press Next.`;
      $('bb-next').hidden = false;
    } finally { busy = false; renderScore(); }
  }

  async function call(dir) {
    const r = match.round; if (!r || r.call) return;
    r.call = dir; r.sure = $('bb-sure').checked;
    $('bb-calls').hidden = true;
    const up = r.future[r.future.length - 1].c > r.past[r.past.length - 1].c;
    const botCall = r.p >= 0.5 ? 'up' : 'down', botSure = Math.abs(r.p - 0.5) > 0.1;
    for (let k = 1; k <= AHEAD; k++) { draw(r, k); await new Promise((res) => setTimeout(res, 260)); }
    const mine = points(dir, r.sure, up) - (r.asked ? 3 : 0), theirs = points(botCall, botSure, up);
    match.you += mine; match.bots += theirs; match.streak = mine > 0 ? match.streak + 1 : 0; match.bestStreak = Math.max(match.bestStreak, match.streak);
    match.log.push({ name: r.name, at: r.at, up, you: dir, bot: botCall, mine, theirs });
    const move = (r.future[r.future.length - 1].c / r.past[r.past.length - 1].c - 1) * 100;
    $('bb-reveal').innerHTML = `<p class="bb-big ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${move >= 0 ? '+' : ''}${move.toFixed(1)}% — it was <b>${F.esc(r.name)}</b> on ${new Date(r.at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}</p>
      <p>You called <b>${dir.toUpperCase()}</b>${r.sure ? ' (sure)' : ''}: <b class="${mine > 0 ? 'up' : mine < 0 ? 'down' : ''}">${mine >= 0 ? '+' : ''}${mine}</b>. The council said <b>${botCall.toUpperCase()}</b> (${Math.round(r.p * 100)}% up${botSure ? ', sure' : ''}): <b class="${theirs > 0 ? 'up' : theirs < 0 ? 'down' : ''}">${theirs >= 0 ? '+' : ''}${theirs}</b>.</p>
      <p class="caption">${r.votes.map((v) => { const bot = Loki.council.BOTS.find((b) => b.id === v.id); return `${bot.icon} ${bot.name} ${Math.round(v.p * 100)}%`; }).join(' · ')}</p>`;
    match.i += 1;
    renderScore();
    if (match.i >= ROUNDS) return endMatch();
    $('bb-next').hidden = false; $('bb-next').focus({ preventScroll: true });
  }

  function endMatch() {
    const m = match, b = best(), won = m.you > m.bots;
    b.matches += 1; if (won) b.wins += 1; if (b.score == null || m.you > b.score) b.score = m.you; saveBest(b);
    const right = m.log.filter((x) => x.you === (x.up ? 'up' : 'down')).length, botRight = m.log.filter((x) => x.bot === (x.up ? 'up' : 'down')).length;
    $('bb-status').innerHTML = `<b>${won ? 'You beat the machines!' : m.you === m.bots ? 'A dead heat with the council.' : 'The council wins this one.'}</b> You ${m.you} – ${m.bots} bots. You called ${right}/${ROUNDS}, the bots ${botRight}/${ROUNDS}; best streak ${m.bestStreak}.`;
    $('bb-start').textContent = 'Play again ▶'; $('bb-start').hidden = false; $('bb-share').hidden = false;
    m.over = true;
    window.dispatchEvent(new CustomEvent('loki-game-played', { detail: { game: 'beat-the-bots', won } }));
    renderScore();
  }

  $('bb-start').addEventListener('click', () => { match = { i: 0, you: 0, bots: 0, streak: 0, bestStreak: 0, log: [], round: null }; $('bb-start').hidden = true; $('bb-share').hidden = true; nextRound(); });
  $('bb-next').addEventListener('click', nextRound);
  $('bb-up').addEventListener('click', () => call('up'));
  $('bb-down').addEventListener('click', () => call('down'));
  $('bb-ask').addEventListener('click', () => {
    const r = match && match.round; if (!r || r.asked || r.call) return;
    const v = r.votes[Math.floor(Math.random() * r.votes.length)], bot = Loki.council.BOTS.find((b) => b.id === v.id);
    r.asked = v.id; $('bb-ask').disabled = true;
    $('bb-ask-out').textContent = `${bot.icon} ${bot.name}: “${Math.round(v.p * 100)}% up — ${v.why}.” (−3 points)`;
  });
  $('bb-share').addEventListener('click', async () => {
    const m = match; if (!m) return;
    const text = `Loki · Beat the Bots — me ${m.you} vs the council ${m.bots}\n${m.log.map((x) => (x.you === (x.up ? 'up' : 'down') ? '🟢' : '🔴')).join('')}`;
    try { await navigator.clipboard.writeText(text); $('bb-status').textContent += ' Result copied.'; } catch { /* clipboard blocked */ }
  });
  root.addEventListener('keydown', (e) => {
    if (!match || !match.round || match.round.call || e.target.closest('input')) return;
    if (e.key === 'ArrowUp') { e.preventDefault(); call('up'); } else if (e.key === 'ArrowDown') { e.preventDefault(); call('down'); }
  });
  window.addEventListener('resize', () => { if (match && match.round) draw(match.round, match.round.call ? AHEAD : 0); });
  renderScore();
  window.LokiBeatBots = { points };
})();
