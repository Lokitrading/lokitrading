# Loki Trading Workspace

One FastAPI application plus an on-device engine: the information workspace at the gate, a Supabase-gated sign-in, a real-money-capable trade terminal (Alpaca and Tradovate), a six-bot self-learning AI council, AI paper trading, prediction-market engines, a replay sandbox, a quant lab, news desk, watchtower, and the "Bored?" game — Loki Dive, a nova-deep trivia ride with seven 25-second rounds per dive, streak combos, typo forgiveness, and example answers after every miss — plus a browser-held Alpaca paper order rail and an in-page autopilot with published, hand-railed rules.

Everything ships over Vercel: every push to `main` reaches https://lokitrading.vercel.app through the connected Vercel project. Access is code-gated; the gate code lives in the deployment env, not in this repository.

## On-device engine (2.52)

Most of the thinking now runs in the visitor's browser (`static/js/engine/`), so Vercel mostly serves files and a few relays:

- **Device-direct data.** Binance market data, Polymarket odds and order books, and Alpaca (with the visitor's own keys) are called straight from the browser; these hosts send CORS headers and are the only ones the CSP `connect-src` allows. Stock history and headlines still come through Loki's cached relays (Yahoo sends no CORS headers). Past replay windows are edge-cacheable (`/api/replay/bars`).
- **AI council** (`/#council`, `engine/council.js`). Six bots (Trend Hawk, Reversion Owl, Momentum Fox, Volatility Bear, Swarm Bee, Pattern Raven) each predict P(higher in 5 bars). Weights are earned with the Hedge rule on decayed log-loss, blending each symbol's record with the all-symbol record. A first visit replays ~250 walk-forward decisions; every live call is stored and scored when its horizon passes, with a written reflection. It is a learning statistical engine, not a conscious mind, and the page shows its Brier score next to a coin flip's 0.25.
- **Autonomous sentiment swarm** (`engine/swarm.js`). Price Pulse, Vol Burst, Book Imbalance, Odds Mover, Newswire and Social Chatter report alarm/direction/confidence; a quorum across families (market + crowd) is a volatility event that shrinks position size and biases the Swarm Bee. Lead time over the first matching headline is measured, never assumed. Social feeds are best effort and say when a browser blocks them.
- **Self-healing risk matrix** (`engine/risk.js`). Drawdown limit = 2.5 × recent weekly volatility (3–15%); GREEN / AMBER (half size) / RED (no new entries). On RED it plans exits by liquidity: thin prediction-market positions are hedged with the opposite outcome token, thin stock/crypto/futures positions get sliced limit exits, liquid ones are halved. After a 30-minute cooldown and a calm swarm, size ramps back 25 → 50 → 100%.
- **Monte Carlo** (`engine/mc_core.js`, run in a Web Worker). 5,000 paths per order: log-normal network delay, price drift during the delay, fills walking a randomly thinned book (or spread + impact), fees and on-chain gas spikes, block-bootstrapped horizon returns or a fair-probability draw for binaries. Confidence = share of profitable paths after costs, plus VaR/CVaR and p99 slippage.
- **Predictive arbitrage engine** (predictions page, `engine/arb.js`, paper only). Prices Polymarket's 5-minute "Up or Down" crypto contracts exactly as they settle (Chainlink 60-second TWAP vs start price) from Binance 1-second candles, compares both books after the market's fee (rate × p(1−p)), and paper-fills only if the edge survives the measured round trip plus signing time. It records signal-vs-fill edge decay and book lag. No real orders are ever sent.
- **Replay lab** (`/#replay`, `engine/replay.js`). FTX, LUNA, Black Thursday, the yen unwind, election night 2024, tariff shocks, COVID, DeepSeek and the 2022 bear market, played bar by bar with the council (learning inside the replay), a volatility alarm, the risk matrix and the co-pilot's stops, against buy-and-hold.
- **Holographic execution map** (`/#execution-map`, `engine/execmap.js`, Three.js r170 vendored under `static/vendor/three`, MIT). A 3D topology of every venue the device talks to; real requests (from a fetch tap that records host, status and timing only) and orders animate along the edges.
- **Strategy co-pilot** (top-bar ✦ button or Ctrl/Cmd+J, `engine/copilot.js`). Deterministic parsing of plain-language instructions into a bounded config diff with JSON preview, apply-on-confirm and undo. Example: "Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction positions if volatility spikes above 40%."
- **Proof-of-Strategy** (`engine/audit.js`). Every execution is appended automatically to a SHA-256 hash chain signed with a non-extractable ECDSA P-256 device key (IndexedDB). The badge recomputes figures from signed entries only; logs export with the public key and can be re-verified anywhere; Alpaca entries reconcile against Alpaca's own order records.

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

`loki_v8/portfolio_ai.py` replays each market's verified profile (13 stocks/ETFs incl. gold and bonds, 8 coins), gives every active market an equal share of capital, and scales the whole book toward a target yearly volatility — capped so total holdings never exceed 100% of the account. The target (10%) was chosen on 2005-2022 only. Untouched holdout 2023 → Nov 2026: **+13.7%/yr, Sharpe 1.25, worst drop −10.5%, 64% of months profitable**, versus equal-weight buy-and-hold of the same markets at +21.9%/yr, Sharpe 0.98, worst drop −28.3%. `GET /api/fusion/ai/portfolio` returns what that exact book holds at the latest completed bar; the AI trading page's Portfolio AI panel mirrors it with paper money. Historical results, not a promise.

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

- `main.py` — Vercel/uvicorn entry point; `loki/api/` — the HTTP surface (server, gate, Supabase auth, news desk, market feed, Alpaca relay, fusion research, watchtower, terminal).
- `core/` + `core_engine` history reduced to what the app imports; `loki_v8/` — the strategy engine behind the terminal; `components/` + `third_party/` — vendored originals pinned by hash-guards (`tests/test_fusion_provenance.py` fails on byte changes, on purpose).
- `templates/`, `static/` — the served pages and their assets; `tests/` — the regression suite.

Older desktop-era launchers, installers, duplicated guides and unreferenced modules were removed in 2026-10 and 2026-11 (old standalone bot, crypto trainer, Supabase vault); history retains them.
