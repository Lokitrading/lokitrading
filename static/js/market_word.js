/* MARKET WORD — Loki's five-letter market-vocabulary puzzle.
 *
 * Six guesses to find a trading word; every answer comes with a plain
 * definition, so a round teaches one term. Daily (the same word for every
 * player, by date) or Practice (unlimited). Hint reveals the definition,
 * Hard mode makes you reuse what you have found, and a colour-blind palette
 * swaps green/yellow for blue/orange. Stats and streaks stay in this browser.
 */
'use strict';
(() => {
  const root = document.getElementById('mw-game');
  if (!root) return;
  const $ = (id) => document.getElementById(id);

  const ANSWERS = {
    ALPHA: 'Return above a benchmark — the part of a gain that skill, not the market, explains.',
    ASSET: 'Anything with value you can own: shares, bonds, coins, property, cash.',
    AUDIT: 'An independent check that a company’s accounts are true.',
    BASIS: 'The gap between a futures price and the spot price — or what you paid for a holding.',
    BEARS: 'Traders who expect prices to fall.',
    BIDDY: null,
    BLOCK: 'A large single trade, or one batch of transactions on a blockchain.',
    BONDS: 'Loans to a government or company that pay interest until they mature.',
    BOOST: 'A sudden lift in price or volume.',
    BULLS: 'Traders who expect prices to rise.',
    BURNS: 'Tokens destroyed on purpose to shrink a coin’s supply.',
    BUYER: 'The side that pays the ask — demand in an order book.',
    CAPEX: 'Capital expenditure: money a company spends on long-lived assets.',
    CARRY: 'Earning the yield difference between what you borrow and what you hold.',
    CHAIN: 'A blockchain — or the list of option strikes for one expiry.',
    CHART: 'A picture of price over time: lines, bars or candlesticks.',
    CLOSE: 'The last price of a trading session — the one most indicators use.',
    COINS: 'Crypto assets that run on their own blockchain.',
    COVER: 'Buying back a short position to close it.',
    CRASH: 'A sudden, deep fall across a market.',
    CYCLE: 'The repeating swing of expansion and contraction in an economy or market.',
    DEBIT: 'Money taken out of an account — or the net cost of an options spread.',
    DELTA: 'How much an option’s price moves when the underlying moves by one.',
    DEPTH: 'How much volume waits at each price in the order book.',
    DRIFT: 'A slow, steady move in one direction with no obvious catalyst.',
    ENTRY: 'The price where you open a trade.',
    FLOAT: 'Shares actually available to trade, after insiders’ locked holdings.',
    FUNDS: 'Pooled money managed together — or simply the cash in an account.',
    GAINS: 'Profits on a position.',
    GAMMA: 'How fast an option’s delta changes as price moves.',
    GREED: 'The emotion that keeps people in a winning trade too long.',
    HEDGE: 'A position taken to offset the risk of another one.',
    INDEX: 'A basket that tracks a market, like the S&P 500.',
    ISSUE: 'Shares or bonds sold by a company, or the act of selling them.',
    LEVER: 'To borrow so a position is bigger than your cash — leverage.',
    LIMIT: 'An order that only fills at your price or better.',
    LOANS: 'Money lent at interest — the business of banks and bond markets.',
    LONGS: 'Positions that profit when price rises.',
    MACRO: 'The big picture: rates, inflation, growth, currencies.',
    MERGE: 'When two companies combine into one.',
    MINER: 'A computer that secures a proof-of-work blockchain for rewards.',
    MONEY: 'What every market prices everything else in.',
    OFFER: 'The ask: the lowest price a seller will take right now.',
    ORDER: 'An instruction to buy or sell.',
    PANIC: 'Selling driven by fear rather than information.',
    PEAKS: 'Local highs on a chart.',
    PRICE: 'What the last buyer and seller agreed on.',
    PRIME: 'The best lending rate a bank offers its most trusted borrowers.',
    PROXY: 'A stand-in: an asset traded because it moves like another one.',
    QUOTE: 'The current bid and ask for an instrument.',
    RALLY: 'A sustained rise in prices.',
    RANGE: 'The band between a period’s high and low.',
    RATES: 'Interest rates — the price of money set by central banks and markets.',
    RATIO: 'One number divided by another, like price-to-earnings.',
    RISKS: 'The ways a trade can lose money.',
    SCALP: 'Trading for tiny, quick profits many times a day.',
    SHARE: 'A slice of ownership in a company.',
    SHORT: 'A position that profits when price falls.',
    SPEND: 'Consumer spending — a big driver of company earnings.',
    STAKE: 'Locking coins to help run a proof-of-stake network for rewards.',
    STOCK: 'Ownership in a company, traded on an exchange.',
    SWING: 'A trade held for days to catch one leg of a move.',
    THETA: 'The value an option loses each day as expiry gets closer.',
    TICKS: 'The smallest price steps a market can move in.',
    TOKEN: 'A crypto asset issued on someone else’s blockchain.',
    TRADE: 'Buying or selling something — the whole point of a market.',
    TREND: 'The general direction prices are moving.',
    VALUE: 'What something is worth, which is not always its price.',
    WAGER: 'A bet — what prediction markets let people place on outcomes.',
    WHALE: 'A holder big enough to move a market alone.',
    YIELD: 'The income an investment pays, as a percentage of its price.',
  };
  delete ANSWERS.BIDDY;
  const WORDS = Object.keys(ANSWERS);
  const EXTRA = ('ABOUT ABOVE ACTOR ADMIT ADOPT ADULT AFTER AGAIN AGENT AGREE AHEAD ALARM ALERT ALIKE ALIVE ALLOW ALONE ALONG ALTER AMBER AMONG ANGER ANGLE APART APPLE APPLY ARENA ARGUE ARISE ARRAY ASIDE AVOID AWAKE BADGE BAKER BEACH BEGAN BEGIN BEING BELOW BENCH BIRTH BLACK BLAME BLANK BLAST BLEND BLIND BLOOM BOARD BOUND BRAIN BRAKE BRAND BRAVE BREAD BREAK BRIEF BRING BROAD BROKE BROWN BUILD CABLE CATCH CAUSE CHAIR CHASE CHEAP CHECK CHIEF CHILD CIVIL CLAIM CLASS CLEAN CLEAR CLIMB CLOCK CLOUD COACH COAST COUNT COURT CRAFT CRANE CREAM CRIME CROSS CROWD CROWN CRUDE CURVE DAILY DANCE DEALT DEBUT DELAY DOUBT DOZEN DRAFT DRAMA DREAM DRESS DRILL DRINK DRIVE EAGER EARLY EARTH EIGHT ELITE EMPTY ENJOY ENTER EQUAL ERROR EVENT EVERY EXACT EXIST EXTRA FAITH FALSE FAULT FIELD FIGHT FINAL FIRST FIXED FLAME FLASH FLEET FLOOR FOCUS FORCE FORUM FOUND FRAME FRAUD FRESH FRONT FRUIT FULLY GIANT GIVEN GLASS GLOBE GRACE GRADE GRAND GRANT GRASS GREAT GREEN GROSS GROUP GUARD GUESS GUEST GUIDE HAPPY HEART HEAVY HORSE HOTEL HOUSE HUMAN IDEAL IMAGE INNER INPUT JOINT JUDGE LABEL LARGE LASER LATER LAUGH LAYER LEARN LEASE LEAST LEAVE LEGAL LEVEL LIGHT LOCAL LOGIC LOOSE LOWER LUCKY MAGIC MAJOR MAKER MARCH MATCH MAYBE METAL MIGHT MINOR MINUS MIXED MODEL MONTH MORAL MOTOR MOUNT MOUSE MOUTH MUSIC NEVER NIGHT NOISE NORTH NOVEL OCEAN OFTEN OTHER PAINT PANEL PAPER PARTY PEACE PHASE PHONE PHOTO PIECE PILOT PITCH PLACE PLAIN PLANE PLANT PLATE POINT POUND POWER PRESS PRIDE PRINT PRIOR PRIZE PROOF PROUD PROVE QUEEN QUICK QUIET QUITE RADIO RAISE RAPID REACH READY REALM REBEL REFER RELAX REPLY RIGHT RIGID RIVAL RIVER ROUGH ROUND ROUTE ROYAL SCALE SCENE SCOPE SCORE SENSE SERVE SEVEN SHAPE SHARP SHEET SHELF SHELL SHIFT SHIRT SHOCK SIGHT SKILL SLATE SLEEP SLIDE SMALL SMART SMILE SMOKE SOLID SOLVE SOUND SOUTH SPACE SPARE SPEAK SPEED SPENT SPLIT SPORT STAFF STAGE STAND START STATE STEAM STEEL STEEP STICK STILL STONE STORE STORM STORY STUDY STUFF STYLE SUGAR SUPER SWEET TABLE TASTE TEACH THANK THEFT THEME THERE THICK THING THINK THIRD THREE THROW TIGHT TITLE TODAY TOPIC TOTAL TOUCH TOUGH TOWER TRACK TRAIN TREAT TRIAL TRUST TRUTH TWICE UNDER UNION UNTIL UPPER UPSET URBAN USUAL VALID VIDEO VISIT VITAL WASTE WATCH WATER WHEEL WHITE WHOLE WORLD WORRY WORSE WORST WORTH WRITE WRONG YOUNG').split(' ');
  const VALID = new Set([...WORDS, ...EXTRA]);
  const EPOCH = Date.UTC(2026, 0, 1);
  const dayNumber = () => Math.floor((Date.now() - EPOCH) / 86400000) + 1;
  const dailyWord = (n) => WORDS[((n * 2654435761) >>> 0) % WORDS.length];

  const load = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? d : v; } catch { return d; } };
  const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage blocked */ } };
  const blankStats = () => ({ played: 0, wins: 0, streak: 0, best: 0, dist: [0, 0, 0, 0, 0, 0], last: 0 });
  const prefs = load('loki_mw_prefs_v1', { hard: false, cb: false });

  let mode = load('loki_mw_mode_v1', 'daily');
  let game = null;

  function fresh(m) {
    const n = dayNumber();
    if (m === 'daily') {
      const saved = load('loki_mw_daily_v1', null);
      if (saved && saved.n === n) return saved;
      return { mode: 'daily', n, word: dailyWord(n), rows: [], hint: false, done: false, won: false };
    }
    let word = WORDS[Math.floor(Math.random() * WORDS.length)];
    if (game && game.word === word) word = WORDS[(WORDS.indexOf(word) + 7) % WORDS.length];
    return { mode: 'practice', n: 0, word, rows: [], hint: false, done: false, won: false };
  }
  const persist = () => { if (game.mode === 'daily') save('loki_mw_daily_v1', game); };
  let typing = '';

  function score(guess, word) {
    const res = Array(5).fill('absent'), pool = word.split('');
    for (let i = 0; i < 5; i++) if (guess[i] === word[i]) { res[i] = 'correct'; pool[i] = null; }
    for (let i = 0; i < 5; i++) if (res[i] !== 'correct') { const j = pool.indexOf(guess[i]); if (j >= 0) { res[i] = 'present'; pool[j] = null; } }
    return res;
  }

  function hardViolation(guess) {
    for (const row of game.rows) {
      for (let i = 0; i < 5; i++) {
        if (row.res[i] === 'correct' && guess[i] !== row.guess[i]) return `Hard mode: letter ${i + 1} must be ${row.guess[i]}`;
        if (row.res[i] === 'present' && !guess.includes(row.guess[i])) return `Hard mode: the guess must contain ${row.guess[i]}`;
      }
    }
    return null;
  }

  function say(text) { $('mw-message').textContent = text; }

  function render(revealRow = -1) {
    root.classList.toggle('mw-cb', Boolean(prefs.cb));
    const board = $('mw-board');
    board.innerHTML = '';
    for (let r = 0; r < 6; r++) {
      const row = document.createElement('div');
      row.className = 'mw-row';
      const done = game.rows[r];
      const letters = done ? done.guess : r === game.rows.length ? typing.padEnd(5) : '     ';
      for (let c = 0; c < 5; c++) {
        const t = document.createElement('div');
        const ch = letters[c].trim();
        t.className = 'mw-tile' + (ch ? ' filled' : '') + (done ? ` ${done.res[c]}` : '') + (r === revealRow ? ' flip' : '');
        if (r === revealRow) t.style.animationDelay = `${c * 0.18}s`;
        t.textContent = ch;
        t.setAttribute('aria-label', ch ? `${ch}${done ? ', ' + done.res[c] : ''}` : 'empty');
        row.appendChild(t);
      }
      board.appendChild(row);
    }
    const keyState = {};
    const rank = { absent: 1, present: 2, correct: 3 };
    for (const row of game.rows) for (let i = 0; i < 5; i++) { const k = row.guess[i]; if ((rank[row.res[i]] || 0) > (rank[keyState[k]] || 0)) keyState[k] = row.res[i]; }
    $('mw-keys').querySelectorAll('[data-key]').forEach((b) => { b.className = 'mw-key' + (b.dataset.key.length > 1 ? ' wide' : '') + (keyState[b.dataset.key] ? ' ' + keyState[b.dataset.key] : ''); });
    $('mw-mode-daily').setAttribute('aria-pressed', String(mode === 'daily'));
    $('mw-mode-practice').setAttribute('aria-pressed', String(mode === 'practice'));
    $('mw-hard').setAttribute('aria-pressed', String(Boolean(prefs.hard)));
    $('mw-cb').setAttribute('aria-pressed', String(Boolean(prefs.cb)));
    $('mw-title').textContent = mode === 'daily' ? `Daily word #${game.n}` : 'Practice word';
    $('mw-hint').disabled = game.done || game.hint;
    $('mw-hint-text').hidden = !game.hint && !game.done;
    $('mw-hint-text').innerHTML = game.done ? `<b>${game.word}</b> — ${ANSWERS[game.word]}` : `Hint: ${ANSWERS[game.word]}`;
    $('mw-new').hidden = mode === 'daily';
    $('mw-share').hidden = !game.done;
    renderStats();
  }

  function renderStats() {
    const s = load(`loki_mw_stats_${mode}_v1`, blankStats());
    const max = Math.max(1, ...s.dist);
    $('mw-stats').innerHTML = `<div><b>${s.played}</b><span>played</span></div><div><b>${s.played ? Math.round(s.wins / s.played * 100) : 0}%</b><span>won</span></div><div><b>${s.streak}</b><span>streak</span></div><div><b>${s.best}</b><span>best streak</span></div>` +
      `<ol class="mw-dist">${s.dist.map((n, i) => `<li><span>${i + 1}</span><i style="width:${Math.max(6, n / max * 100)}%" class="${game.done && game.won && game.rows.length === i + 1 ? 'now' : ''}">${n}</i></li>`).join('')}</ol>`;
  }

  function finish(won) {
    game.done = true; game.won = won;
    const key = `loki_mw_stats_${game.mode}_v1`, s = load(key, blankStats());
    if (game.mode !== 'daily' || s.last !== game.n) {
      s.played += 1;
      if (won) { s.wins += 1; s.dist[game.rows.length - 1] += 1; }
      const consecutive = game.mode !== 'daily' || s.last === game.n - 1;
      s.streak = won ? (consecutive ? s.streak + 1 : 1) : 0;
      s.best = Math.max(s.best, s.streak);
      if (game.mode === 'daily') s.last = game.n;
      save(key, s);
    }
    persist();
    render();
    say(won ? ['Genius trade.', 'Sharp.', 'Well read.', 'Solid.', 'Close call.', 'Phew — just in time.'][game.rows.length - 1] : `Out of guesses. The word was ${game.word}.`);
    if (won) $('mw-board').children[game.rows.length - 1].classList.add('win');
    window.dispatchEvent(new CustomEvent('loki-game-played', { detail: { game: 'market-word', won } }));
  }

  function submit() {
    if (game.done) return;
    if (typing.length < 5) { shake('Not enough letters'); return; }
    if (!VALID.has(typing)) { shake('Not in the word list'); return; }
    if (prefs.hard) { const v = hardViolation(typing); if (v) { shake(v); return; } }
    const res = score(typing, game.word);
    game.rows.push({ guess: typing, res });
    typing = '';
    const won = res.every((r) => r === 'correct');
    render(game.rows.length - 1);
    persist();
    if (won || game.rows.length === 6) setTimeout(() => finish(won), 1100);
    else say(`${6 - game.rows.length} guess${6 - game.rows.length === 1 ? '' : 'es'} left.`);
  }
  function shake(msg) {
    say(msg);
    const row = $('mw-board').children[game.rows.length];
    if (row) { row.classList.remove('shake'); void row.offsetWidth; row.classList.add('shake'); }
  }
  function press(key) {
    if (game.done) return;
    if (key === 'ENTER') return submit();
    if (key === 'BACK') typing = typing.slice(0, -1);
    else if (/^[A-Z]$/.test(key) && typing.length < 5) typing += key;
    say('');
    render();
  }

  function buildKeys() {
    const rows = ['QWERTYUIOP', 'ASDFGHJKL', ['ENTER', ...'ZXCVBNM', 'BACK']];
    $('mw-keys').innerHTML = rows.map((r) => `<div class="mw-keyrow">${[...r].map((k) => `<button type="button" class="mw-key" data-key="${k}" aria-label="${k === 'BACK' ? 'Delete letter' : k === 'ENTER' ? 'Submit guess' : k}">${k === 'BACK' ? '⌫' : k === 'ENTER' ? 'Enter' : k}</button>`).join('')}</div>`).join('');
  }

  function start(m) {
    mode = m; save('loki_mw_mode_v1', mode);
    typing = '';
    game = fresh(mode);
    say(game.done ? (game.won ? 'Solved today. A new daily word arrives tomorrow — or switch to Practice.' : `Today's word was ${game.word}. Practice is unlimited.`) : 'Type or tap letters; Enter to guess.');
    render();
  }

  $('mw-keys').addEventListener('click', (e) => { const k = e.target.closest('[data-key]'); if (k) { press(k.dataset.key); root.focus({ preventScroll: true }); } });
  root.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.target.closest('input, textarea, select')) return;
    if (e.key === 'Enter' && e.target.closest('button')) return;
    if (e.key === 'Enter') { e.preventDefault(); press('ENTER'); }
    else if (e.key === 'Backspace') { e.preventDefault(); press('BACK'); }
    else if (/^[a-z]$/i.test(e.key)) { e.preventDefault(); press(e.key.toUpperCase()); }
  });
  $('mw-board').addEventListener('click', () => root.focus({ preventScroll: true }));
  $('mw-mode-daily').addEventListener('click', () => start('daily'));
  $('mw-mode-practice').addEventListener('click', () => start('practice'));
  $('mw-new').addEventListener('click', () => start('practice'));
  $('mw-hint').addEventListener('click', () => { game.hint = true; persist(); render(); });
  $('mw-hard').addEventListener('click', () => {
    if (game.rows.length && !game.done) { say('Hard mode can only change before your first guess.'); return; }
    prefs.hard = !prefs.hard; save('loki_mw_prefs_v1', prefs); render();
  });
  $('mw-cb').addEventListener('click', () => { prefs.cb = !prefs.cb; save('loki_mw_prefs_v1', prefs); render(); });
  $('mw-share').addEventListener('click', async () => {
    const icon = { correct: prefs.cb ? '🟧' : '🟩', present: prefs.cb ? '🟦' : '🟨', absent: '⬛' };
    const head = `Loki Market Word ${game.mode === 'daily' ? '#' + game.n : '(practice)'} ${game.won ? game.rows.length : 'X'}/6${prefs.hard ? '*' : ''}${game.hint ? ' (hint)' : ''}`;
    const text = `${head}\n${game.rows.map((r) => r.res.map((x) => icon[x]).join('')).join('\n')}`;
    try { await navigator.clipboard.writeText(text); say('Result copied — paste it anywhere.'); } catch { say(text); }
  });

  buildKeys();
  start(mode);
  window.LokiMarketWord = { score, dailyWord, WORDS, VALID };
})();
