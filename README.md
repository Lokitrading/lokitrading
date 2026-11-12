# Loki Trading Workspace

One FastAPI application plus an on-device engine: the information workspace at the gate, a Supabase-gated sign-in, a real-money-capable trade terminal (Alpaca and Tradovate), a six-bot self-learning AI council, AI paper trading, prediction-market engines, a Red Team lab, a 3D engine map, a replay sandbox, a quant lab, news, watchtower, and the "Bored?" game — Loki Dive, a nova-deep trivia ride with seven 25-second rounds per dive, streak combos, typo forgiveness, and example answers after every miss — plus a browser-held Alpaca paper order rail and an in-page autopilot with published, hand-railed rules.

Everything ships over Vercel: every push to `main` reaches https://lokitrading.vercel.app through the connected Vercel project. Access is code-gated; the gate code lives in the deployment env, not in this repository.

## On-device engine (2.52)

Most of the thinking now runs in the visitor's browser (`static/js/engine/`), so Vercel mostly serves files and a few relays:

- **Device-direct data.** Binance market data, Polymarket odds and order books, and Alpaca (with the visitor's own keys) are called straight from the browser; these hosts send CORS headers and are the only ones the CSP `connect-src` allows. Stock history and headlines still come through Loki's cached relays (Yahoo sends no CORS headers). Past replay windows are edge-cacheable (`/api/replay/bars`).
- **AI council** (`/#council`, `engine/council.js`). Six bots (Trend Hawk, Reversion Owl, Momentum Fox, Volatility Bear, Swarm Bee, Pattern Raven) each predict P(higher in 5 bars). Weights are earned with the Hedge rule on decayed log-loss, blending each symbol's record with the all-symbol record. A first visit replays ~250 walk-forward decisions; every live call is stored and scored when its horizon passes, with a written reflection. It is a learning statistical engine, not a conscious mind. Since 2.57 each bot states how far it expects the market to depart from its *usual* up-rate (markets rise more often than they fall), and the page scores the council against always guessing that usual rate rather than a coin flip. Measured over 10 years of 21 markets (22,000+ walk-forward calls per set) its skill over that guess is about zero: five-day direction is close to unpredictable, and the page says so.
- **Autonomous sentiment swarm** (`engine/swarm.js`). Price Pulse, Vol Burst, Book Imbalance, Odds Mover, Newswire and Social Chatter report alarm/direction/confidence; a quorum across families (market + crowd) is a volatility event that shrinks position size and biases the Swarm Bee. Lead time over the first matching headline is measured, never assumed. Social feeds are best effort and say when a browser blocks them.
- **Self-healing risk matrix** (`engine/risk.js`). Drawdown limit = 2.5 × recent weekly volatility (3–15%); GREEN / AMBER (half size) / RED (no new entries). On RED it plans exits by liquidity: thin prediction-market positions are hedged with the opposite outcome token, thin stock/crypto/futures positions get sliced limit exits, liquid ones are halved. After a 30-minute cooldown and a calm swarm, size ramps back 25 → 50 → 100%.
- **Monte Carlo** (`engine/mc_core.js`, run in a Web Worker). 5,000 paths per order: log-normal network delay, price drift during the delay, fills walking a randomly thinned book (or spread + impact), fees and on-chain gas spikes, block-bootstrapped horizon returns or a fair-probability draw for binaries. Confidence = share of profitable paths after costs, plus VaR/CVaR and p99 slippage.
- **Predictive arbitrage engine** (predictions page, `engine/arb.js`, paper only). Prices Polymarket's 5-minute "Up or Down" crypto contracts exactly as they settle (Chainlink 60-second TWAP vs start price) from Binance 1-second candles, compares both books after the market's fee (rate × p(1−p)), and paper-fills only if the edge survives the measured round trip plus signing time. It records signal-vs-fill edge decay and book lag. No real orders are ever sent.
- **Replay lab** (`/#replay`, `engine/replay.js`). FTX, LUNA, Black Thursday, the yen unwind, election night 2024, tariff shocks, COVID, DeepSeek and the 2022 bear market, played bar by bar with the council (learning inside the replay), a volatility alarm, the risk matrix and the co-pilot's stops, against buy-and-hold.
- **3D engine map** (`/#execution-map`, `engine/execmap.js`, Three.js r170 vendored under `static/vendor/three`, MIT). A 3D topology of every venue the device talks to; real requests (from a fetch tap that records host, status and timing only) and orders animate along the edges. See the 2.56 notes below.
- **Strategy co-pilot** (top-bar ✦ button or Ctrl/Cmd+J, `engine/copilot.js`). Deterministic parsing of plain-language instructions into a bounded config diff with JSON preview, apply-on-confirm and undo. Example: "Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction positions if volatility spikes above 40%."
- **Proof-of-Strategy** (`engine/audit.js`). Every execution is appended automatically to a SHA-256 hash chain signed with a non-extractable ECDSA P-256 device key (IndexedDB). The badge recomputes figures from signed entries only; logs export with the public key and can be re-verified anywhere; Alpaca entries reconcile against Alpaca's own order records.

## 2.65: Day Trader v3 — feed-proof signals, a wider test, and what slow fills cost

**The rules**
- **Same signals from every price feed.** The rules compare price with the session's average price. The published method weights that average by volume, but free feeds disagree about intraday volume (two servers of the same provider differed on about 20% of bars), so a signal could depend on which server answered. The average is now time-weighted and uses no volume. In the 10-year test the two versions give near-identical results (daily correlation 0.97–0.99).
- **Exact accounting.** A position is tracked from its fill price to its exit price. A short is a fixed number of shares; nothing is compounded bar by bar. The paper book on the page has always worked this way, and the test now matches it.

**A wider, stricter test** (`train_daytrade.py`)
- 28 markets instead of 12: index funds, large stocks, sector funds, gold, silver, bonds, oil, gas, emerging markets and China.
- A market is traded only if it passed on 2016–2021 with instant fills *and* with fills 5 minutes late (Sharpe above 0.3 both ways). Six passed: QQQ, NVDA, TSLA, AMZN, AMD, ARKK.
- On unseen data the rules lost money on 20 of the 28 markets. All 28 together made −3.6% a year.

**Results on unseen data (2022 → November 2026, simulated, after costs)**

| Book | Instant fills | 1 min late | 2 min late | 3 min late | 5 min late |
|---|---|---|---|---|---|
| Stocks and funds (6 markets, mistake filter on) | +7.4%/yr, Sharpe 1.00, worst drop −9.0% | +6.0% | +4.8% | +4.4% | +4.2%, Sharpe 0.61 |
| Futures (MES and MNQ, filter off) | +7.9%/yr, Sharpe 1.06, worst drop −9.2% | +6.3% | +5.1% | +4.0% | +5.1%, Sharpe 0.74 |

- Without the mistake filter the stock book made +3.2% a year with instant fills and −0.9% with fills 5 minutes late.
- The 1–3 minute columns use 1-minute bars: each order is re-priced at the open of the minute N minutes after its signal. Two cross-checks hold exactly: at 0 minutes this method reproduces the main test, and at 5 minutes it reproduces the 5-minute-bar late test.
- In 2016–21 a one-minute delay cost almost nothing. Since 2022 it costs about a fifth of the result: the edge goes to whoever acts first.
- Futures are tested on each contract's index fund at futures costs. Over the last 35 sessions the funds' daily results matched the real contracts' (NQ=F, ES=F) with a correlation of 0.99.

**So the live trader acts faster**
- A decision on the bar that just closed is reported at once (`pending`), and the paper book acts on it straight away at the latest price in the feed (`now_price`). Before, it waited five minutes for the next bar to print.
- The page checks again 12 and 40 seconds after every half hour, when the rules decide. For two minutes after each half hour the server re-reads prices every 15 seconds, and a plan built before the latest 5-minute bar closed is never reused.
- Between full history reads (every ten minutes) only today's bars are fetched, which is a small request.
- The mistake filter rates a pending first signal too, so the book never opens a trade the tested rules would have skipped.
- Every paper fill logs how many seconds after the signal it happened. The page shows the running average beside the fill-speed figures.
- If the feed stops printing bars for 30 minutes during a session (a halt or an early close), the market is treated as closed and the book goes flat.

**Honest summary.** The day-trading edge is real in the test, thin, and dependent on speed. A browser on a free price feed cannot be instant, so expect the paper book between the "instant" and "1 min late" columns at best. The Portfolio AI (unseen +14.2% a year, Sharpe 1.29) remains the stronger tested strategy.

## 2.64: 3D engine map, glitch fixes and a lighter, sharper look

**Fixed**
- **Flickering static.** A per-frame film-grain effect read as a glitch; it is removed.
- **Rainbow edges.** Colour fringing is cut to a trace.
- **Stretched stage.** The stage used to stretch to the height of the tables beside it, becoming a tall narrow strip with the camera far too close. It now has its own height, and the camera pulls back on narrow stages so the whole map stays in frame.
- **Stutter.** Rendering is much lighter: a lower pixel ratio, the glow pass at one-third resolution with two blur passes, and less anti-aliasing.
- **Quality governor.** If frames stay slow, it lowers the resolution first and then switches the glow pass off.

**Added** (all cheap to draw)
- Aurora ribbons.
- Counter-rotating dashed HUD rings on the floor.
- A lens streak across the core.
- A shockwave from the core when an agent fires.
- Energy running up each venue's pillar, faster when that venue is in use.

The legend still says which parts are decoration.

## 2.63: forward record for the Day Trader

- The tested record ends on a fixed date (`data_through`). Every completed session after it is one the rules could not have been fitted to.
- `GET /api/fusion/ai/daytrade` now returns `forward`: those sessions replayed with the frozen rules and the frozen mistake model, as an equal-capital book.
- The page shows the total, up and down days, worst drop and a bar per day, with a plain note that a handful of days proves nothing.
- Limits: the price feed keeps about 60 days of 5-minute bars, so the strip shows at most the last 60 days. It is simulated on delayed prices, not real trades.

## 2.62: 3D engine map, second upgrade

**New look** (`engine/execmap.js`)
- **Post-processing glow.** A bloom pipeline written for this page (bright pass, separable blur, filmic tone mapping, vignette, light film grain). No extra libraries; it falls back to plain rendering if the device cannot do it.
- **Plasma core.** The heart is an animated noise shader that runs hotter with real request traffic.
- **Energy shield and particle disk.** A fresnel shell and thousands of orbiting points around the core.
- **Holo floor.** A shader-drawn polar grid with an outward sweep.
- **Light pillars.** Each venue stands on a pillar with a landing ring that brightens when it is used.
- **Shooting stars** and a fly-in when the map opens.

**New data on the map**
- The Day Trader is now a live node. It pulses when the paper book buys, shorts or closes, and its tooltip shows the current plan.
- A failed request flashes the shield and the screen edge red.

**Still honest**
- Bright comets are real requests and agent events only.
- The disk, shield and shooting stars are decoration, and the legend says so.
- Reduced-motion settings skip the fly-in and shooting stars and slow everything down. Small screens use fewer particles and a lighter glow.

## 2.61: Day Trader learns from its mistakes; futures mode

**Learning from mistakes** (`daytrade.learn_from_mistakes`)
- At each day's first signal the Day Trader rates the setup from what is known at that moment: the opening gap, the day's normal range, yesterday's move, the weekday, and the signal's side and time.
- It sits out days that look like past losing days (estimated win chance under 40%).
- Each year is traded with a model fitted only on earlier years' days. The live site uses the model fitted on all ~8,000 past trading days.
- It is kept only where it earned it. It had to beat the plain rules on 2018–2021 **and** beat filters trained on scrambled outcomes; it won 29 of 30 and 30 of 30. That rules out "it only helped because it traded less".

| Stocks book (QQQ, NVDA, TSLA, AMZN, AMD), 2022 → now, unseen | Return / yr | Sharpe | Worst drop |
|---|---|---|---|
| Plain rules | +5.8% | 0.55 | −17.0% |
| **With learning** | **+8.0%** | **1.10** | **−9.8%** |

**Futures mode** (`?mode=futures`)
- It trades micro Nasdaq-100 (MNQ) and micro S&P 500 (MES) with the same rules, tested at futures costs (0.5 bp per side) on each contract's index fund during regular US hours.
- Unseen 2022 → now: **+8.4%/yr, Sharpe 1.13, worst drop −9.0%**. On 2016–21 it made +6.2%/yr.
- The fund is a stand-in for the contract. Checked against the real futures over the last 35 sessions, daily results correlate 0.98–0.99.
- Micro Russell (M2K) and micro Dow (MYM) lost money and are left out.
- The mistake filter is **off** for futures: it did not help there on 2018–21.
- Sizing is shown in contracts, with a plain warning. One MNQ plus one MES controls about $95,000; the test used no leverage; real futures can lose more than the deposit.

**Tested and rejected**
- An adaptive market switch (trade a market only while its recent results are positive): worse than the fixed list.
- Volatility-scaled position size: too small a gain to matter.

## 2.60: Day Trader AI (main mode)

The AI trading page now leads with a **Day Trader**: in and out the same day, flat every night, paper money.

**How it trades** (`loki_v8/daytrade.py`)
- The method is published intraday momentum with noise bands (Zarattini, Aziz and Barbon, 2024), used as written with no tuning.
- For every 5-minute slot it measures how far price normally sits from the day's open at that time (14-session average).
- Every 30 minutes it goes long above that band and the session VWAP, short below, flat in between.
- Orders fill at the next bar's open, and everything closes at the last bar.
- One function, `replay_day`, runs both the 10-year test and the live plan (`GET /api/fusion/ai/daytrade`), so the page trades what was tested.

**How it tested** (`train_daytrade.py` → `loki_v8/daytrade_record.json`; 10 years of 5-minute bars from Alpaca; 1 bp per side on index funds, 2 bp on stocks)
- Twelve liquid stocks and funds were tested. Markets were chosen on 2016–2021 only (Sharpe above 0.3) and judged on 2022 onward.
- The book holds QQQ, NVDA, TSLA, AMZN and AMD, with equal capital each.

| Window | Return / yr | Sharpe | Worst drop |
|---|---|---|---|
| 2016–2021 (markets chosen here) | +13.0% | 1.34 | −8.9% |
| **2022 → now (unseen)** | **+5.8%** | **0.55** | **−17.0%** |

**What the record also says**
- The rules lost money after costs on 7 of the 12 markets on unseen data. All 12 together made −0.8% a year since 2022.
- 2025 was a losing year (−10.7%).
- It is very sensitive to costs: at double the assumed cost most of the edge is gone.
- It tested weaker than the Portfolio AI (+14.2% a year, worst drop −9.2% on its own unseen window).
- Simpler rules were tested first on hourly bars and all lost after costs: opening-range breakout, first-hour and last-half-hour momentum, gap fades, and crypto breakout, dip-buying and trend rules. They are listed in the record and on the page.

**On the site**
- The browser keeps a paper book that mirrors the plan every minute: equal capital per market, long and short, closed before the bell.
- Prices are delayed 5-minute bars. No orders leave the browser.
- Talk to Loki answers day-trading questions from the same record.

## 2.59: risk levels; another round of honest research

**Risk levels (AI trading page, `GET /api/fusion/ai/portfolio?risk=steady|balanced|growth`).**
- The same tested rules can now run at three risk levels. Each level publishes its whole simulated record (2005 → now, same fees, slippage and T-bill cash):

| Level | Risk target | Return / yr | Worst drop | Losing years (of 21) | Sharpe |
|---|---|---|---|---|---|
| **Steady** (tested default) | 10% | +13.3% | −15.9% | 2 | 1.26 |
| Balanced | 15% | +16.0% | −18.2% | 2 | 1.24 |
| Growth | 20% | +17.1% | −20.3% | 4 | 1.21 |

- Steady stays the default because it earned the most for the risk it took on 2005–2022. Higher levels hold more of each market, so both returns and drops grow.
- Nothing borrows: total holdings stay capped at 100%, which is why returns flatten above Growth.
- The Alpaca forward test and the website's Alpaca panel stay on Steady.
- Switching level reuses the downloaded market data for a minute, so it never refetches 21 markets.

**Research that did not earn a place** (chosen on 2005–2022 only; baseline score 1.19, logged in `rejected_experiments`):
- capital tilted to the strongest markets (6- and 12-month momentum): 0.99–1.17;
- a drawdown brake (halve or stop after a 5% or 8% drop): smaller drops but 0.25–1.13;
- dip-buying sleeves on index funds (2-day RSI): no edge after costs, 1.04;
- crisis diversifiers (dollar, commodities, mid-term Treasuries): 1.20, inside the noise.

Twelve alternatives across three rounds have now failed to beat the current rules by the 0.05 promotion margin. That is evidence the rules sit on a robust plateau, and it is the reason no "better" signal was shipped: one chosen from this many tries would mostly be luck.

**Error sweep.** Every page on the live site (desktop and mobile), every data endpoint and every button was re-checked; no new errors were found.

## 2.58: idle cash earns Treasury-bill interest; year-by-year record

**Idle cash in Treasury bills.**
- The Portfolio AI holds markets only while they trend, so on average about 37% of the account sat in cash earning nothing.
- That cash is now parked in a 0–3 month Treasury-bill fund (SGOV) in four places:
  - the tested record;
  - the live plan (`cash_parking`);
  - the website's paper book, which accrues the current yield;
  - the Alpaca paper forward test and the website's Alpaca panel, which buy and sell SGOV.
- How the replay credits the interest:
  - it uses yesterday's published 13-week bill yield (`^IRX`);
  - minus a conservative 0.14% a year fee;
  - with 2 bps charged on every move in or out.
- The risk level was re-chosen on 2005–2022 with the cash included; it stayed at 10%.

| Window | Before | With T-bills |
|---|---|---|
| 2005–2018 | +11.5%/yr, Sharpe 1.15, worst −15.1% | +12.0%/yr, Sharpe 1.19, worst −14.9% |
| 2019–2022 | +15.6%/yr, Sharpe 1.39 | +15.9%/yr, Sharpe 1.42 |
| 2023 → now (unseen) | +13.3%/yr, Sharpe 1.21, worst −9.8% | **+14.2%/yr, Sharpe 1.29, worst −9.2%** |

**Year-by-year record (simulated).**
- The AI trading page, Talk to Loki and the record (`calendar_years`) now show every calendar year since 2005.
- It was up in 19 of 21 full years. The losing years were 2008 (−6.1%) and 2022 (−4.1%).
- No strategy can promise a profit every year, or every month; the page says so.

## 2.57: is the trading AI actually good? An honest audit

**Does it work?**
- On the live site, the Portfolio AI, the per-symbol AI (10 tickers including BRK.B and EURUSD=X), and Talk to Loki all answered correctly.
- The live plan matches the tested 20-year replay. The live site replays only 5 years per market, yet in/out decisions disagree on at most 0.8% of days in any market.
- The backtest is causal: it decides on the previous completed bar and fills on the next one, with stop gaps handled.

**How good is it?**
- **Portfolio AI, data it never saw (2023 on):** +13.3%/yr, Sharpe 1.21, worst drop −9.8%. Buy-and-hold of the same markets: +21.9%/yr, Sharpe 0.98, worst drop −28.3%.
- **Hindsight check (new):** the 21 markets include individual stocks that are known winners today, which flatters any backtest.
  - The identical method was re-run on index funds, gold, bonds, BTC and ETH.
  - Result: +10.2%/yr, Sharpe 1.03, worst drop −8.6%, against buy-and-hold's +16.2%/yr, Sharpe 1.03, −22.0%.
  - It **matches** buy-and-hold on risk-adjusted return; its real edge is far smaller drops.
  - This check is now shipped in the record (`hindsight_check`), shown on the AI trading page, and given by Talk to Loki.
- **Six-bot council:** it was scored against always guessing each market's usual up-rate, not a coin flip.
  - Data: 10 years of 10 markets, then confirmed on 11 others.
  - Its skill was about zero (−0.3%). Its ~55% hit rate came from markets rising more often than they fall.

**What changed**
- **Council calibration.**
  - Bots now predict departures from the usual rate, in half-size steps.
  - Skill vs the usual rate went from −0.31% to +0.17% on the set the change was picked on, and from −0.56% to −0.15% on the 11 markets it was not.
  - That is less wrong, not a new edge, and the page now says so.
  - Autopilot, consensus and the jury read the council relative to the usual rate too.
- **Alpaca forward-test report:** today's equity is now included. Alpaca's daily history ends yesterday, so the "since" return used to lag the balance shown next to it.

**Tried and rejected** (chosen on 2005–2022 only, as before; logged in `rejected_experiments`):
- risk-balanced (inverse-volatility) weights: Sharpe 0.95–0.99 vs 1.15;
- fixed stock/crypto budgets: 1.152 vs 1.151;
- a multi-horizon trend blend: 1.19 vs 1.15, below the 0.05 promotion margin.

The current rules sit on a robust plateau; none of these is a real improvement.

## 2.56: leaner workspace, cooler 3D map

- **Retired:** the Trading desk, the Scenarios page and the Research review (the AI-council review form, the narrative/MiroFish scenario engine and the Fincept bridge). Their pages, sidebar links, server endpoints (`/api/desk/*`, `/api/fusion/council`, `/api/fusion/scenario`, `/api/fusion/narrative`, `/api/fusion/*mirofish*`, `/api/fusion/fincept/*`), modules and tests are gone. Use the **Red Team lab** to review a trade before you place it; stress tests live there too.
- **Sidebar:** the "Your research" card is gone, NEW badges stay only on the newest page (Red Team lab), and the 3D engine map moved into the main navigation.
- **3D engine map, rebuilt:**
  - starfield, nebula and polar-grid backdrop with tone mapping and fog;
  - a reactor core whose heartbeat follows the real request rate;
  - glowing curved arcs to each venue;
  - an agent ring wired to the core;
  - comets coloured by outcome (sent / ok / slow / failed), plus scan waves that turn red on a failure.
- **Map controls and HUD:**
  - drag to orbit, wheel or pinch to zoom, hover for a tooltip, click to fly to a node, double-click to reset;
  - auto-orbit toggle and fullscreen (left automatically when you switch pages);
  - a live HUD with requests/min, average latency, errors/min and active agents.
- **Honest by design:** the bright comets are real requests only; the faint particles on the arcs are decorative. Reduced-motion settings slow everything down and turn off auto-orbit.

## Red Team lab (2.55)

`#redteam`. Every proposed trade is attacked before it is simulated. All of it runs on the visitor's device unless marked "server".

- **Agent jury** (`engine/jury.js`):
  - **Quant Agent:** momentum and mean reversion, weighted by the regime that GARCH, variance-ratio and efficiency tests detect.
  - **Risk Manager:** VaR, maximum position size, and the tilt, quarantine, news and concentration checks. It has an **absolute veto**.
  - **Execution Engine:** chooses market, limit, TWAP or VWAP from volatility, spread and participation, and estimates slippage with the local fill engine.
  - **Red Team, Devil's Advocate and the six-agent consensus** also vote.
  - A trade needs a 2/3 supermajority of the five jurors.
  - The Risk Manager cross-examines the Quant with past failures in the same volatility regime.
  - Every decision is written to a post-mortem log with the minority's reasoning.
  - The jurors are deterministic statistical agents. No large language models are involved: none are configured on the server.
- **Red Team** (`engine/redteam.js`):
  - Six scenarios of 3,000 paths each: baseline, depth collapse, latency spike, spread widening ×8, flash-crash jump, and full liquidity withdrawal.
  - Plus the asset's worst historical move, and path-wise maximum drawdown.
  - The Devil's Advocate runs the opposite trade through the same tests.
  - The verdict matrix gives a 0–100% safety score; paper trades need at least 60%.
- **Local fill engine** (`engine/fillsim.js`):
  - Queue position for limit orders, and partial fills.
  - 50–500 ms latency.
  - Square-root impact above 5% of bar volume.
  - Front-running market makers.
  - TWAP/VWAP with a dark-pool node.
  - Margin calls.
  - Seeded and deterministic: the "Verify determinism" button proves identical fills.
- **Local paper book and journal** (`engine/paperbook.js`):
  - Zero-dependency paper trading.
  - Execution-vs-plan audits and pattern-of-failure clusters.
  - Sharpe, Sortino and Calmar cards.
- **Guardrails** (`engine/guard.js`):
  - 3 max-loss trades in a row → 24-hour lockout.
  - Strategy drift log.
  - Drawdown-scaled sizing.
  - 30-day quarantine.
  - News lockout 15 minutes before FOMC decisions (server: `/api/calendar/macro`, from the Fed's calendar) and estimated jobs reports. Add CPI or anything else yourself.
- **Cross-asset matrix** (`engine/xasset.js`), across 10 markets:
  - Macro pair signals.
  - 20- vs 120-day divergence alerts.
  - Risk-parity rebalancing for the paper book.
- **Black swan stress** (`engine/stress.js`):
  - 2010, 2020, 2022 and 2024 crises replayed on the book.
  - 10,000 jump-diffusion paths with systemic correlation-to-one jumps.
  - Margin-call escalation.
  - The cut in gross exposure needed to bring VaR99 inside the limit.
- **Local-first data** (`engine/localstore.js`):
  - Columnar typed-array cache in IndexedDB.
  - Offline mode.
  - A CPU benchmark.
- **Walk-forward evolution:** strains are scored on rolling out-of-sample windows. They get Sharpe/Sortino/Calmar scorecards and are sunset when they fall below baseline.
- **Wired into the Trade terminal:** live orders also need jury approval and red-team safety.

## Engine v3 and the Command Center (2.54)

Everything below runs on the visitor's device unless marked "server".

- **Consensus gate** (`engine/consensus.js`): six agents (Macro, Liquidity, Sentiment, Council, Risk, Execution) vote on every order from the Trade terminal. Live orders need approval; Liquidity, Risk and Execution can veto; exits are never blocked by direction.
- **Execution quality** (`engine/execq.js`): rolling error rate and p50/p95 latency per venue from the page's own network tap. Degraded venues halve size, down venues pause orders, crypto spot fails over from Binance to Coinbase.
- **Live Monte Carlo risk** (`engine/mcrisk.js`): 10,000 one-day paths every 5 s over every open broker and Polymarket-paper position (joint bootstrap of real days). VaR95 above the daily loss limit shrinks every new order.
- **Evolution** (`engine/evolve.js`): 30 strategy strains breed on 70% of history; a champion is promoted only if it beats the current rules on the unseen 30%. Paper auto-deploy is opt-in; live is never changed automatically.
- **Natural-language strategies** (`engine/strategy_nl.js`): a fixed grammar turns a sentence into rules, readable code and a next-bar-fill backtest. A strategy can be armed on paper under a commitment hash.
- **Commit-reveal and signed scorecards** (`engine/audit.js`): the rules are hashed with a salt before trading, and every later log entry carries that hash. Scorecard links are signed with the device key and verified in any browser. This is a hash commitment, not a zero-knowledge proof.
- **Prediction markets v2** (`pm_lab.js`):
  - a paper market-maker for Polymarket liquidity rewards, filled only by real trade prints;
  - a read-only Kalshi ↔ Polymarket cross-venue check (server: `/api/xarb`), with both rule texts shown under every pair.
- **Command Center** (`#command`, `command_center.js`): glass widgets you can drag and collapse, with these panels:
  - telemetry (server: `/api/status/telemetry`);
  - order-book heatmap and depth surface with an iceberg heuristic;
  - central-bank newswire (server: `/api/newswire`);
  - signal relay to Telegram/Discord/Slack (server: `/api/signals/relay`, fixed hosts only, nothing stored);
  - strategy cards;
  - voice commands and hotkeys (Shift+P/F/K/V/C/M; destructive actions always confirm).
- **Also new:**
  - second-by-second replays (1 s Binance bars);
  - the sub-agent "neural flow" ring in the 3D execution map.

## Games (Bored?)

- **Market Word** (`static/js/market_word.js`): a five-letter trading-vocabulary puzzle with a definition for every answer. Daily (same word for everyone, by date) and unlimited Practice; hint, hard mode, colour-blind palette, streaks, guess distribution and a copyable result grid.
- **Beat the Bots** (`static/js/beat_bots.js`): ten rounds of real daily crypto history (Binance, device-direct) at a random date with the name hidden. You and the six-bot council each call the next five days; sure calls pay double and cost points when wrong; "Ask a bot" reveals one bot's view. Name and date are revealed after each call.
- **Loki Dive** (`static/js/game.js`): rare-answer trivia, now 69 hand-picked prompts including markets topics (crypto, exchanges, S&P 500 members, investors, candlesticks, indicators, central banks, crashes). Daily runs stay server-verified because the server reads the same bank.

## Trade terminal

`/#trade` (`static/js/trade.js`) sends real orders to **Alpaca paper or live** (browser → Alpaca directly; paper keys saved in the browser, live keys kept only for the tab) and **Tradovate demo or live** (`loki/api/broker_tradovate.py`, a stateless relay that stores nothing: credentials → token once, then token per request; contracts capped at 10 per ticket). Guardrails:

- Live trading must be armed per session by typing "trade real money".
- Every live order shows its full details for confirmation and must pass every pre-trade check (council, Monte Carlo, risk matrix, notional cap). There is a 20-live-orders-a-day cap.
- The risk breaker blocks new live entries, and a kill switch cancels everything and flattens.
- The autopilot only ever trades paper/demo accounts.
- Every order lands in the signed log.

Polymarket stays paper-only: real orders there would need a wallet private key.

## Run locally

Requires Python 3.12. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8124
```

Optional environment variables: `LOKI_SITE_GATE=1` enables the gate (default code `flathead`; set `LOKI_SITE_CODE` to change it), `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` enable the login flow, Alpaca keys are browser-held and verify through the relay (`/api/broker/alpaca/*`) whenever the user enters them.

## Guarantees

- The honest-error contract: degraded states are visible, empty logs say empty, headline cards say "information, never advice", the autopilot's published rule is its contract, and the relay caps order size server-side regardless of what the panel says.
- Keys and secrets never enter the repository; the backup export excludes credentials.
- The "Bored?" game is entirely local: scores and dive history never leave the browser, and its rarity tiers are hand-written bank data, not measurements from other players.
- `pytest` (all Python tests), plus `node --test` for the two JavaScript test modules. The CI workflows in `.github/` run both on every push.

## Live markets

`/#live-markets` is a self-refreshing board (every ~15 s while the view is open and the tab is visible) covering crypto majors, US mega-caps, indices, commodities and FX, with session status, top movers, flash-on-change tiles and a personal watchlist saved in the browser. A candlestick chart sits at the top of the view (the S&P 500 until you pick another tile): 1m, 5m, 15m, 1h, daily and weekly candles (weekly built from daily), candles or line, 20/50-period moving averages, a volume pane, and a crosshair readout of open/high/low/close/volume by mouse, touch or arrow keys. The newest candle follows the live board price between candle refreshes. Below it, the AI engine's review-only read for that symbol. Served by `loki/api/live_markets.py` (`/api/markets/live`, `/api/markets/quotes`); crypto comes from Binance Spot (live), everything else from Yahoo Finance (near-live). Failed feeds stay visible per tile.

## Live AI follows the verified rules

The AI paper trader (`/api/fusion/ai/step`) decides through `loki_v8/live_engine.py`, which mirrors `run_backtest` bar for bar: it scores only the last *completed* daily bar (today's forming candle is excluded), applies the same trend / regime / volatility gates and conviction / vol-target sizing, and manages open positions with the profile's time stop, breakeven lock, trailing stop and partial target. `tests/test_live_engine.py` replays backtests and checks the live planner agrees on every entry decision.

## 20-year universal profiles (current live AI)

`train_long_history.py` fits ONE rulebook for stocks/ETFs and one for crypto on daily data from 2004 (crypto from 2014), using the same `run_backtest` the live engine mirrors, with 10 bps fees + 5 bps slippage per side:

- **select** on the worse of development (2005-2018; crypto 2015-2020) and validation (2019-2022; crypto 2021-2022) scores;
- **report once** on the untouched holdout (2023 onward); the live AI only uses a block marked `verified` (made money on the holdout).

Results shipped in `loki_v8/training_report_snapshot.json` (`universal`), data through 2026-11-06:

| Holdout 2023→ | AI avg / yr | Profitable | AI avg worst DD | Buy & hold avg / yr | B&H avg worst DD |
|---|---|---|---|---|---|
| Stocks/ETFs (13) | +12.3% | 12/13 | −20.9% | +27.8% | −26.7% |
| Crypto (8) | +9.2% | 7/8 | −29.4% | +17.3% | −71.3% |

Stocks use trend-only scoring, entry at score ≥ 30, vol-target sizing (20%), and hold until price breaks its 200-day average (10 ATR protective stop). Crypto uses trend-leader scoring, entry ≥ 8, SELL-signal exit. The AI makes money out of sample but trails buy-and-hold in the strong 2023-26 bull market; its edge is far smaller drawdowns. Historical results, not a promise.

```powershell
python train_long_history.py --write-snapshot        # ~30 min on 4 cores
```

## Portfolio AI (21 markets at once)

`loki_v8/portfolio_ai.py` replays each market's verified profile (13 stocks/ETFs incl. gold and bonds, 8 coins), gives every active market an equal share of capital, and scales the whole book toward a target yearly volatility — capped so total holdings never exceed 100% of the account. The target (10%) was chosen on 2005-2022 only. Untouched holdout 2023 → Nov 2026 (idle cash in T-bills since 2.58): **+14.2%/yr, Sharpe 1.29, worst drop −9.2%, 66% of months profitable**, versus equal-weight buy-and-hold of the same markets at +21.9%/yr, Sharpe 0.98, worst drop −28.3%. `GET /api/fusion/ai/portfolio` returns what that exact book holds at the latest completed bar; the AI trading page's Portfolio AI panel mirrors it with paper money. Historical results, not a promise.

## Security

- **Isolation between users:** account data is keyed only by the uid inside the signed session cookie (validated as a UUID); a request naming another owner is refused before the database; credentials can never be saved to the cloud; chat history is per browser session; Tradovate tokens are encrypted and owner-bound.
- **Cookies:** HMAC-signed, HttpOnly, SameSite, Secure on HTTPS. Public deployments without `LOKI_GATE_SECRET` (or a Supabase key) now sign with a random per-process key instead of a constant from the source, so cookies cannot be forged; chat admin claims additionally require `LOKI_CHAT_ADMIN_CODE`.
- **Headers:** strict Content-Security-Policy (scripts only from this site — no inline scripts exist), `frame-ancestors 'self'`, nosniff, Permissions-Policy, COOP, HSTS on HTTPS. Cross-site writes to account/chat endpoints are rejected.
- **Rate limits** (`loki/api/rate_limit.py`): sign-in, sign-up, broker sign-in, revenue operator key, and the heavy AI/market endpoints.
- Real orders, broker and native-engine routes only work from the local launcher with its session token and are off on Vercel. `main.py` binds to 127.0.0.1 unless `LOKI_HOST` says otherwise.
- **Operators should set:** `LOKI_GATE_SECRET` (long random), `LOKI_CHAT_SESSION_SECRET`, `LOKI_SITE_CODE`, `LOKI_BROKER_ENCRYPTION_KEY`, and `LOKI_SECURE_COOKIES=1` behind any non-Vercel HTTPS proxy.

## Privacy, terms and age

- `/privacy`, `/cookies` and `/terms` (v6) are public and describe exactly what the code does: functional first-party cookies only (no analytics, advertising or third-party cookies — so no consent banner), browser-local storage for journals/watchlists/chat, optional account saves, chat turns kept at most 30 days, broker credentials never stored.
- **Age:** entry and sign-up require an "I am 18 or older" confirmation, enforced server-side; no birth date is asked for or stored.
- **Data rights:** `GET /api/account/export` (download my data), `POST /api/account/delete` (type DELETE; removes saves, published score and the login), and a "clear Loki data from this browser" control on `/privacy`.
- **Minimisation:** no third-party requests from the browser (Google Fonts removed; CSP allows no external hosts).
- **Claims:** performance figures are labelled as hypothetical/simulated with a limitations disclaimer; marketing lines that the evidence did not support were rewritten.
- Set `LOKI_OPERATOR_NAME` and `LOKI_OPERATOR_EMAIL` so the policies name the data controller and a contact address. Have the policies reviewed for your jurisdiction.

## Opening experience

`static/js/loki_scene.js` + `static/css/loki_scene.css` add a once-per-visit intro, animated market canvases, a live price tape and motion to the gate page and overview, plus an interface motion system: view transitions, 3D card tilt, a cursor aura, ambient aurora, numbers that count to new values, self-drawing charts and an animated allocation ring. `static/js/loki_radar.js` draws the **AI Market Radar** in the home banner — the Portfolio AI's 21 markets placed by live trend score, held positions glowing and pinging under a rotating sweep (data from `/api/fusion/ai/portfolio`). All presentation only; honours reduced-motion and pauses in hidden tabs.

## AI self-check, daily briefing, price alerts

- **Honest uncertainty:** the Portfolio AI's held-back result comes with a 90% block-bootstrap range (`holdout_ci`, currently about +5% to +23% a year).
- **Self-health check:** every live plan compares the strategy's last 12 months with all rolling 12-month windows from 2005-2022 (`expected_range`) and reports `normal`, `below_range` or `above_range`.
- **Daily AI briefing** on the overview: health meter, this week's entries/exits, and the uncertainty range; Talk to Loki answers "how is the AI doing?" and "what changed this week?".
- **Price alerts** on Live markets: above/below alerts stored in this browser, checked on every board refresh, with an in-page toast and an optional browser notification.
- The operator named in the policies defaults to Joshua Mathew (`LOKI_OPERATOR_NAME` / `LOKI_OPERATOR_EMAIL` override it).

## Self-evolving AI (weekly)

Every Sunday `.github/workflows/ai-evolution.yml` runs `evolve.py`, one generation of champion vs challengers (`loki_v8/evolution.py`):

1. The live profile is the champion; its one-step neighbours on the training grid plus a few random grid points are the challengers (~15 per asset class).
2. All are backtested over the full 20 years with the same fees, slippage, windows and score the champion was chosen with.
3. A challenger is promoted only if its rank score (worse of 2005-2018 and 2019-2022) beats the champion's by at least 0.05 **and** it is no worse on 2023-onward and the last 12 months. Recent data can veto a promotion but never choose one.
4. The learn-from-mistakes filter is re-validated walk-forward; the live setting is re-checked before any new one can replace it, and when it is kept it is refitted on every closed trade, including the newest.
5. The generation is appended to `loki_v8/evolution_log.json`, the tests run, and the update is opened as a pull request. Merging deploys it; closing it keeps the current AI.

The record is served at `GET /api/fusion/ai/evolution`, shown on the AI trading page, and explained by Talk to Loki ("is the AI improving itself?"). Generation 1 (2026-11-08): both champions kept (best challengers 0.441 vs 0.444 for stocks, 0.844 vs 0.852 for crypto); the stocks mistake filter was refitted on 686 closed trades. Run by hand with `python evolve.py --write-snapshot`. For the workflow to open PRs, enable *Allow GitHub Actions to create and approve pull requests* in Settings → Actions → General.

## Forward test on Alpaca paper

`alpaca_forward.py` mirrors the Portfolio AI's current plan onto an Alpaca **paper** account (the host is hard-wired to `paper-api.alpaca.markets`): it reads the same plan the website serves, trades only the AI's markets, skips moves under 2% of equity, sells before it buys, defers stock trades while the US market is closed (crypto trades any day), and reports the account's return and worst drop since it started. BNB is not offered by Alpaca, so its share stays in cash. It is a dry run unless `--execute` is passed.

`.github/workflows/alpaca-paper-forward.yml` runs it daily at 14:45 UTC once the repository has `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` secrets (paper keys); without them it skips. Judge the results over months — a few days of paper P&L say nothing about a daily trend strategy, and tuning the AI on them would only fit noise.

## Prediction markets (Polymarket) and Crowd vs Machine

`/#predictions` shows live Polymarket odds — Finance, Economy & Fed, Crypto, Stocks, Politics, Trending, plus search — through a read-only relay (`loki/api/polymarket.py`: fixed host `gamma-api.polymarket.com`, 90 s cache, 8 MB cap, whitelisted fields, Polymarket links validated). **Loki never places Polymarket bets**: real-money orders need a crypto wallet's private key and are geo-restricted in several countries. Visitors practise with **paper predictions** (1,000 play credits in the browser; positions revalue from live odds and settle when a market resolves).

**Crowd vs Machine** (only on Loki): for every Polymarket price ladder ("Bitcoin above ___ on November 11?", "NVIDIA closes above ___ …") the ladder is smoothed into a monotone curve, giving the crowd's chance of finishing above today's price and its median expected price; Loki's trading AI reads the same market, and each card says whether they agree, disagree or are mixed. Ladders at least 3 days out are preferred (a ladder settling within hours is a coin flip). Talk to Loki answers odds questions from the same data ("will the Fed cut rates in December?").

The top price ticker now updates prices in place, so its loop never restarts and every symbol on the board comes round.

## Polymarket paper bot

The **Loki Polymarket bot** (`/#predictions`, `loki/api/polymarket_bot.py`) trades Polymarket price ladders with paper money:

- **Fair value:** live spot and 30-day realised volatility give a lognormal chance of finishing above each strike, blended 50/50 with the crowd's mid-price (the crowd often knows about earnings or quiet weekends the model does not).
- **Entries:** it buys the YES token (view: above) or the NO token (view: below) only when fair value beats the price actually payable on the live CLOB book (`clob.polymarket.com/book`, asks walked level by level) by at least 3 points after Polymarket's taker fee. Rungs with a YES spread over 10c, long shots under 10c and near-certainties over 90c are skipped.
- **Sizing:** quarter-Kelly, at most 5% per trade and 30% invested, one rung per ladder and two per underlying.
- **Exits are checked before any entry, every minute:** out 30 minutes before the market locks, a 40% stop-loss, take-profit once 60% of the remaining upside is in hand, or when the edge is gone. Sells walk the bids, so exit slippage counts. A market the bot just left is not re-entered for 6 hours.
- **Orders** are shown in Polymarket's CLOB shape (`token_id`, `side`, `price`, `size`, `FOK`) for review. **Loki never holds a wallet private key and never signs or sends an order**; the paper account lives in the browser (`loki_poly_bot_v1`).
- **Not built, on purpose:** latency arbitrage on 5-minute markets (a website server cannot win races against co-located bots) and market making (paper fills for resting orders would be invented).

API: `GET /api/polymarket/bot/signals?bankroll=&exposure=` and `POST /api/polymarket/bot/review` (max 20 positions), rate-limited.

## Status lights and paper badges

The top bar shows a **feed light** fed by `GET /api/status/feeds` (`loki/api/feed_status.py`): Yahoo stock quotes, Binance crypto quotes, Polymarket odds, Polymarket order books, the trading-AI profiles, accounts and login security, each checked with a tiny live request (parallel, 45 s cache, refreshed every minute while the tab is visible). States are Live, Slow, Not set up and Down; details never include hosts or secrets. A pulsing **PAPER TRADING · SIMULATED** badge sits beside it and turns red (**REAL ACCOUNT**) only in the real-account view; every paper P&L panel carries a **SIMULATED** tag.

## Alpaca and Tradovate connectors

**Alpaca (paper).** Keys stay in the visitor's browser and travel per request to a relay that only talks to `paper-api.alpaca.markets` (and `data.alpaca.markets` for bars). The panel verifies keys, shows account facts and holdings, places capped manual orders, and now also: **cancels** open orders from the log, **closes** a whole position, and **lets the Portfolio AI manage the paper account** — *Preview the AI's trades* shows exactly what it would do; *Place these paper orders* (typed confirmation server-side, plan recomputed at that moment, sells before buys, at most 25 orders) places them. The same logic (`loki/api/alpaca_ai.py`) drives `alpaca_forward.py`. Minute bars now show the last session in evenings and weekends instead of an error. Validation errors never echo submitted values, so keys can't bounce back into logs.

**Tradovate (read-only).** Sign in with Tradovate API credentials (the password is used once; only the issued token is kept, encrypted, owner-bound, ≤1 hour) or paste a token. Positions now show contract names (e.g. `MESZ6`) and balances their currency, when Tradovate allows those reads. Needs `LOKI_BROKER_ENCRYPTION_KEY` on the server.

**Independent-data check** (`crosscheck_alpaca.py`, run 2026-11-08 with Alpaca's own adjusted daily bars): the live stock profile gives +9.22%/yr (Sharpe 0.61, 12/13 profitable) on Alpaca data vs +9.30%/yr on Yahoo over 2017→now — correlation 0.999 across symbols. Crypto (2022→now) +9.64%/yr vs +10.55%, correlation 0.988, but Bitcoin differs (+5.2% vs +11.5%) because Alpaca's crypto volume is its own venue's; the crypto profile's volume factor makes it somewhat data-source sensitive. Nothing was tuned on this.

## Learning from mistakes

`loki_v8/mistake_model.py` replays the AI's own trades, records 15 market features at every entry's signal bar and whether the trade made money after costs, and fits a small regularised logistic model. Live and in backtests the same gate then skips setups whose estimated win chance is below a threshold. It is trained only on trades that had already closed before the window it is judged on, the threshold is chosen on 2019-2022, and it ships only if the 2023+ holdout is not worse. Current result: on for stocks (validation score 0.44 → 0.51; holdout same +12.3%/yr with 108 instead of 127 trades), off for crypto (no validation gain). Rerun with `python train_long_history.py --from-report state/long_history_report.json --write-snapshot`.

## Talk to Loki

Both the workspace "Talk to Loki" panel and the `/ai-chat` room share `loki/api/loki_brain.py`: name any ticker or coin and it runs the live AI engine and explains every check in plain English; it also answers market checks, the AI's track record, how the AI works, and ~30 trading terms. No external language model is used; questions about your own portfolio/research go to the context-aware copilot.

## Older per-symbol book

`loki_v8/train.py` still provides the earlier 10-year per-symbol walk-forward book (`python -m loki_v8.train`). The live AI uses it only as a fallback when no verified universal profile exists for an asset class.

## Layout

- `main.py` — Vercel/uvicorn entry point; `loki/api/` — the HTTP surface (server, gate, Supabase auth, news desk, market feed, Alpaca relay, fusion (quant lab, portfolio, real account), watchtower, terminal).
- `core/` + `core_engine` history reduced to what the app imports; `loki_v8/` — the strategy engine behind the terminal; `components/` + `third_party/` — vendored originals pinned by hash-guards (`tests/test_fusion_provenance.py` fails on byte changes, on purpose).
- `templates/`, `static/` — the served pages and their assets; `tests/` — the regression suite.

Older desktop-era launchers, installers, duplicated guides and unreferenced modules were removed in 2026-10 and 2026-11 (old standalone bot, crypto trainer, Supabase vault); history retains them.
