# Loki Trading Workspace

Loki is a trading research site: one FastAPI application plus an engine that runs in the visitor's browser. It has a Day Trader AI and a Portfolio AI that trade paper money by tested rules, a trade terminal for Alpaca and Tradovate with guardrails, a six-bot council, a Red Team lab, prediction-market tools, live markets, news, a 3D engine map, a replay lab and games.

- **Live site:** https://lokitrading.vercel.app. Every push to `main` deploys there through Vercel. Access is code-gated; the gate code lives in the deployment's settings.
- **Results are simulated.** Every performance figure below comes from a historical test with assumed costs, or from paper money. None of it is a promise of profit or financial advice.

## Contents

1. [What is in the app](#1-what-is-in-the-app)
2. [The trading AIs and how they tested](#2-the-trading-ais-and-how-they-tested)
3. [Safety and honesty rules](#3-safety-and-honesty-rules)
4. [Run, test and deploy](#4-run-test-and-deploy)
5. [Project layout](#5-project-layout)
6. [Feature reference](#6-feature-reference)
7. [Release notes](#7-release-notes)

## 1. What is in the app

Each row is a page in the sidebar. "On device" means the work runs in the visitor's browser.

| Page | What it does | Main code |
|---|---|---|
| Overview | Where you left off, the daily AI briefing with its self-check, a market snapshot and getting-started guides | `static/js/ai_briefing.js`, `loki_radar.js`, `visit_note.js`, `next_steps.js` |
| Command center | Every engine on one screen: telemetry, execution quality, live Monte Carlo risk, consensus gate, strategy evolution, order-book heatmap, plain-English strategy builder, central-bank newswire | `static/js/command_center.js`, `static/js/engine/` |
| Red Team lab | Attacks a proposed trade before it is simulated: jury and red team, local paper book, tilt detector, replay engine, cross-asset matrix, stress lab, post-mortems (on device) | `static/js/redteam_view.js`, `engine/jury.js`, `engine/redteam.js`, `engine/paperbook.js`, `engine/guard.js`, `engine/stress.js` |
| Trade | Real orders to Alpaca (paper or live) and Tradovate (demo or live) with guardrails, an autopilot on practice money, and a signed log | `static/js/trade.js`, `engine/audit.js`, `loki/api/broker_tradovate.py` |
| AI council | Six statistical bots scored honestly, a league table, the sentiment swarm (on device) | `static/js/council_view.js`, `engine/council.js`, `engine/swarm.js` |
| Quant lab | Import one asset's history: walk-forward research, factor diagnostics, pullback test, machine-learning study, proof engine, watchtower and headlines | `static/js/workspace.js`, `proof_engine.js`, `watchtower.js`, `core/strategy_lab.py`, `core/fusion/quant.py` |
| Paper portfolio | A paper journal, a position-size preview, backup and restore | `static/js/workspace.js`, `backup.js`, `core/fusion/portfolio.py`, `core/risk_preview.py` |
| AI trading | **Day Trader** (main mode), **Portfolio AI**, the weekly self-review, the per-symbol paper simulator with the AI's reasoning, live-account controls, funded-account panel | `static/js/day_trader.js`, `portfolio_ai.js`, `ai_evolution.js`, `ai_trading.js`, `live_trading.js`, `funded_accounts.js`, `loki/api/ai_paper.py`, `loki_v8/` |
| Live markets | Self-refreshing board, candlestick chart, the AI's read on the chosen symbol, price alerts | `static/js/live_markets.js`, `loki/api/live_markets.py` |
| Talk to Loki | Plain-English answers about markets, the AIs and your own research (no language model) | `loki/api/loki_brain.py`, `core/copilot.py` |
| Prediction markets | Polymarket odds, Crowd vs Machine, the paper bot, the 5-minute arbitrage engine, a market-making simulator, a Kalshi ↔ Polymarket check, paper predictions (all paper) | `static/js/predictions.js`, `polymarket_bot.js`, `arb_view.js`, `pm_lab.js`, `loki/api/polymarket.py`, `loki/api/polymarket_bot.py` |
| 3D engine map | A live 3D map of every venue the device talks to | `static/js/engine/execmap.js` |
| Replay lab | Past crises replayed bar by bar against the council and the risk matrix (on device) | `static/js/engine/replay.js`, `static/js/engine_views.js` |
| News | Public headlines with topic and keyword filters | `static/js/news_view.js`, `loki/api/news_desk.py` |
| Simulated market | A generated practice exchange with a paper order ticket, guided drills, analogue research, and AI paper trading on Alpaca through the local launcher | `static/js/simulated_market.js`, `practice.js`, `analogue_workspace.js`, `broker_workspace.js`, `core/fusion/simulated_market.py` |
| Real account | Alpaca paper keys, the Portfolio AI on that paper account (preview first), exchange balances, and manual Kraken spot orders through the local launcher | `static/js/alpaca_keys.js`, `real_account.js`, `core/fusion/real_account.py`, `core/fusion/real_execution.py` |
| Bored? | Games: Market Word, Beat the Bots, Loki Dive with its leaderboard | `static/js/market_word.js`, `beat_bots.js`, `game.js`, `dive_board.js` |

Other pages: the access gate (`/gate`), login (`/login`), account (`/account`), and the legal pages (`/terms`, `/privacy`, `/cookies`, `/transparency`).

## 2. The trading AIs and how they tested

All figures are simulated on historical data after assumed costs. Rules are chosen on earlier years only and judged on later years they never saw. Ideas that were tested and failed are kept on record.

| AI | What it does | Unseen-data result | Honest limits |
|---|---|---|---|
| **Portfolio AI** | Holds 21 markets while they trend; idle cash in T-bills | +14.2% a year, Sharpe 1.29, worst drop −9.2% (2023 → Nov 2026) | Trails buy-and-hold in a strong bull market; its edge is smaller drops |
| **Day Trader** (stocks and funds) | In and out the same day on six markets, flat every night | +7.4% a year, Sharpe 1.00, worst drop −9.0% (2022 → Nov 2026) with instant fills; +5.9% one minute late; +4.2% five minutes late | Thin edge; lost money on 20 of the 28 markets tested; depends on fast fills |
| **Day Trader** (futures: MES, MNQ) | The same rules on micro index futures | +7.9% a year, Sharpe 1.06, worst drop −9.2% with instant fills; +6.2% one minute late | Tested on index funds at futures costs; futures are leveraged |
| **pump.fun bot** (Memecoins tab) | Paper-trades new pump.fun launches on the real bonding curve, read from pump.fun's own Solana program | Did not pass: −12% a trade on the 527 test launches (36 trades); +34% on the next 542 with the rule frozen (29 trades, range −6% to +75%); buying every launch lost 2–8% a trade | Most launches go to zero; one 1.8-hour recording is one market mood |
| **Six-bot council** | Predicts the next five bars | About zero skill against always guessing the usual up-rate | Five-day direction is close to unpredictable, and the page says so |

### Day Trader (main mode on the AI trading page)

- **Method** (`loki_v8/daytrade.py`): published intraday momentum with noise bands (Zarattini, Aziz and Barbon, 2024), used as written with no tuning and one stated change: the average-price line is time-weighted, so every price feed gives the same signals. For every 5-minute slot it measures how far price normally sits from the day's open at that time (14-session average). Every 30 minutes it goes long above that band and the session's average price, short below, flat in between. Everything closes before the bell.
- **One engine.** The function `replay_day` runs the 10-year test and the live plan (`GET /api/fusion/ai/daytrade?mode=stocks|futures`), so the page trades what was tested.
- **Test** (`train_daytrade.py` → `loki_v8/daytrade_record.json`): 10 years of 5-minute bars from Alpaca on 28 markets. Costs per side: 1 bp on the big index funds, 2 bp on stocks, 0.5 bp on futures. A market is traded only if it passed on 2016–2021 with instant fills and with fills 5 minutes late. Six passed: QQQ, NVDA, TSLA, AMZN, AMD, ARKK.
- **Learning from mistakes.** At each day's first signal it rates the setup against about 9,300 past trading days and sits out days that look like past losers. Each year is traded with a model fitted only on earlier years. The filter is kept only where it beat the plain rules on 2018–2021 and beat filters trained on scrambled outcomes (28 of 30 and 30 of 30). It is on for stocks and off for futures, where it did not help.
- **Speed.** Orders are re-priced on 1-minute bars to show what a late fill costs (see the table above and the 2.65 notes). The paper book on the page acts on a new signal within seconds and logs each fill's delay.
- **Forward record.** The record ends on a fixed date. Every session after it is replayed with the frozen rules and shown on the page, with a note that a few days prove nothing.
- **Paper money only.** No orders leave the browser.

### pump.fun bot (Memecoins tab)

- **Connector** (`loki_v8/pumpfun.py`): pump.fun has no public trading API, and its website API blocks automated clients. Every launch and trade is an event its program (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) writes to the Solana log, so the bot reads those: launches, trades with the curve's reserves after each one, and graduations. Only events written while pump.fun itself was executing are read. The curve maths matches pump.fun's own fills to within a billionth.
- **Live relay** (`/api/fusion/ai/pumpfun/stream`): Solana's public node refuses web pages, and the raw stream is about 0.6 MB a second. The server subscribes to the program's logs for 45 seconds at a time and passes the browser one decoded row per launch, trade and graduation, about 10 KB a second. The browser bot (`static/js/engine/pumpfun_core.js`) runs on the rows' own timestamps, so a late batch never changes a paper fill, and it makes exactly the trades the Python bot makes (tested row for row).
- **What it sees** (decision-time only): launch age, share of the curve sold, buyers and net SOL in the last 30 seconds, buyers since launch, trades in 10 seconds, drop from the peak, the biggest buy's share of recent buying, supply bought in the launch block, the largest holder's share, whether the creator sold, market cap, and Mayhem mode (about half of all launches, where an automated agent trades without fees). Tap a live launch for its X-ray.
- **Fills**: orders land 1.5 seconds after the decision at the reserves the curve has then; a buy that would fill more than 15% above the decision price fails and costs the transaction fee; pump.fun's 1.25% fee each way; 0.0005 SOL a transaction; our own price impact is given back on the sale. A token that graduates is sold at its last curve price less 1%.
- **Training** (`train_pumpfun.py` → `loki_v8/pumpfun_record.json`): every standard launch recorded from the program for 1.8 hours (3,028 launches; 2,107 with their whole tradable life recorded), the many that died included. Three kinds of bot were tried: 6,144 entry rules × 144 exits; a learned filter (a ridge model of each trade's return from its entry-time signals); and a swarm of the 15 best rules that buys on a quorum. Each was fitted on the first half of the launches and compared on the next quarter; only the winner was run once on the last quarter.
- **Result**: the single rule won the comparison (+39% a trade on the fit launches, +56% on the check launches, from 38 and 18 trades). On the 527 launches it never saw it lost 12% a trade (36 trades, 11% won, −0.86 SOL at 0.2 SOL a trade). The swarm made +23% on the check launches; the learned filter found nothing. Buying every launch lost about 8% a trade. The record says "did not pass", and the bot runs on paper as a live experiment, labelled as such.
- **Forward check** (`train_pumpfun.py forward`, rule frozen): on the next 542 launches it made +34% a trade (29 trades, 28% won, best +364%, median −40%), while buying every launch lost 2%. Its 90% range (−6% to +75%) still includes losing, and the test window before it lost 12%: two short windows pointing opposite ways is noise, not a proven edge, so the verdict stands.
- **What was ruled out on the way**: copying wallets whose buys were followed by gains. Some wallets are better than others (their results carry over weakly), but copying the best 56 of them 1.5 seconds late lost 7% a trade on later launches, worse than copying anyone.
- **Second connector: Jupiter** (`/api/fusion/ai/pumpfun/quote`). Jupiter, Solana's main trade router, routes pump.fun's curve before graduation and its AMM after. Every paper buy and sell is priced again by Jupiter for the same amount when it lands, and the gap is shown beside the trade. "Read its curve" shows Jupiter's price, liquidity, 24-hour change and the impact of a 0.1 SOL buy for any Solana token, pump.fun or not.
- **Paper only.** There is no wallet; nothing is signed or sent.

### Portfolio AI (21 markets at once)

`loki_v8/portfolio_ai.py` replays each market's verified profile (13 stocks/ETFs incl. gold and bonds, 8 coins), gives every active market an equal share of capital, and scales the whole book toward a target yearly volatility — capped so total holdings never exceed 100% of the account. The target (10%) was chosen on 2005-2022 only. Untouched holdout 2023 → Nov 2026 (idle cash in T-bills since 2.58): **+14.2%/yr, Sharpe 1.29, worst drop −9.2%, 66% of months profitable**, versus equal-weight buy-and-hold of the same markets at +21.9%/yr, Sharpe 0.98, worst drop −28.3%. `GET /api/fusion/ai/portfolio` returns what that exact book holds at the latest completed bar; the AI trading page's Portfolio AI panel mirrors it with paper money. Historical results, not a promise.

### Live AI (per symbol) follows the verified rules

The AI paper trader (`/api/fusion/ai/step`) decides through `loki_v8/live_engine.py`, which mirrors `run_backtest` bar for bar: it scores only the last *completed* daily bar (today's forming candle is excluded), applies the same trend / regime / volatility gates and conviction / vol-target sizing, and manages open positions with the profile's time stop, breakeven lock, trailing stop and partial target. `tests/test_live_engine.py` replays backtests and checks the live planner agrees on every entry decision.

### 20-year universal profiles (current live AI)

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

### Live AI: learning from mistakes

`loki_v8/mistake_model.py` replays the AI's own trades, records 15 market features at every entry's signal bar and whether the trade made money after costs, and fits a small regularised logistic model. Live and in backtests the same gate then skips setups whose estimated win chance is below a threshold. It is trained only on trades that had already closed before the window it is judged on, the threshold is chosen on 2019-2022, and it ships only if the 2023+ holdout is not worse. Current result: on for stocks (validation score 0.44 → 0.51; holdout same +12.3%/yr with 108 instead of 127 trades), off for crypto (no validation gain). Rerun with `python train_long_history.py --from-report state/long_history_report.json --write-snapshot`.

### Self-evolving AI (weekly)

Every Sunday `.github/workflows/ai-evolution.yml` runs `evolve.py`, one generation of champion vs challengers (`loki_v8/evolution.py`):

1. The live profile is the champion; its one-step neighbours on the training grid plus a few random grid points are the challengers (~15 per asset class).
2. All are backtested over the full 20 years with the same fees, slippage, windows and score the champion was chosen with.
3. A challenger is promoted only if its rank score (worse of 2005-2018 and 2019-2022) beats the champion's by at least 0.05 **and** it is no worse on 2023-onward and the last 12 months. Recent data can veto a promotion but never choose one.
4. The learn-from-mistakes filter is re-validated walk-forward; the live setting is re-checked before any new one can replace it, and when it is kept it is refitted on every closed trade, including the newest.
5. The generation is appended to `loki_v8/evolution_log.json`, the tests run, and the update is opened as a pull request. Merging deploys it; closing it keeps the current AI.

The record is served at `GET /api/fusion/ai/evolution`, shown on the AI trading page, and explained by Talk to Loki ("is the AI improving itself?"). Generation 1 (2026-11-08): both champions kept (best challengers 0.441 vs 0.444 for stocks, 0.844 vs 0.852 for crypto); the stocks mistake filter was refitted on 686 closed trades. Run by hand with `python evolve.py --write-snapshot`. For the workflow to open PRs, enable *Allow GitHub Actions to create and approve pull requests* in Settings → Actions → General.

### Forward test on Alpaca paper

`alpaca_forward.py` mirrors the Portfolio AI's current plan onto an Alpaca **paper** account (the host is hard-wired to `paper-api.alpaca.markets`): it reads the same plan the website serves, trades only the AI's markets, skips moves under 2% of equity, sells before it buys, defers stock trades while the US market is closed (crypto trades any day), and reports the account's return and worst drop since it started. BNB is not offered by Alpaca, so its share stays in cash. It is a dry run unless `--execute` is passed.

`.github/workflows/alpaca-paper-forward.yml` runs it daily at 14:45 UTC once the repository has `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` secrets (paper keys); without them it skips. Judge the results over months — a few days of paper P&L say nothing about a daily trend strategy, and tuning the AI on them would only fit noise.

### Older per-symbol book

`loki_v8/train.py` still provides the earlier 10-year per-symbol walk-forward book (`python -m loki_v8.train`). The live AI uses it only as a fallback when no verified universal profile exists for an asset class.

## 3. Safety and honesty rules

- **Simulated means simulated.** Every paper P&L panel carries a SIMULATED tag, and performance figures are labelled as hypothetical with their limits.
- **The Day Trader, the Portfolio AI and Polymarket are paper only.** Loki never holds a wallet private key and never signs or sends a Polymarket order.
- **Real orders exist only in the Trade terminal** and only behind its guardrails: live trading is armed per session by typing "trade real money", every live order is confirmed, there is a notional cap and a 20-live-orders-a-day cap, a kill switch, and jury and red-team approval. The autopilot only ever trades paper or demo accounts.
- **No language models.** The council, jurors, chat brain and bots are deterministic statistical code. Nothing here is conscious or sentient.
- **Failed ideas stay on record.** Rejected rules and experiments are listed in the tested records and on the pages.
- **No decoration posing as data.** On the 3D map, comets are real requests and cubes are orders; only venues and agents that have actually done something are lit and labelled.

### Guarantees

- The honest-error contract: degraded states are visible, empty logs say empty, headline cards say "information, never advice", the autopilot's published rule is its contract, and the relay caps order size server-side regardless of what the panel says.
- Keys and secrets never enter the repository; the backup export excludes credentials.
- The "Bored?" game is entirely local: scores and dive history never leave the browser, and its rarity tiers are hand-written bank data, not measurements from other players.
- `pytest` (all Python tests), plus `node --test` for the five JavaScript test files. The CI workflows in `.github/` run both on every push.

### Security

- **Isolation between users:** account data is keyed only by the uid inside the signed session cookie (validated as a UUID); a request naming another owner is refused before the database; credentials can never be saved to the cloud; chat history is per browser session; Tradovate tokens are encrypted and owner-bound.
- **Cookies:** HMAC-signed, HttpOnly, SameSite, Secure on HTTPS. Public deployments without `LOKI_GATE_SECRET` (or a Supabase key) now sign with a random per-process key instead of a constant from the source, so cookies cannot be forged.
- **Headers:** strict Content-Security-Policy (scripts only from this site — no inline scripts exist), `frame-ancestors 'self'`, nosniff, Permissions-Policy, COOP, HSTS on HTTPS. Cross-site writes to account/chat endpoints are rejected.
- **Rate limits** (`loki/api/rate_limit.py`): sign-in, sign-up, broker sign-in, revenue operator key, and the heavy AI/market endpoints.
- Real orders, broker and native-engine routes only work from the local launcher with its session token and are off on Vercel. `main.py` binds to 127.0.0.1 unless `LOKI_HOST` says otherwise.
- **Operators should set:** `LOKI_GATE_SECRET` (long random), `LOKI_SITE_CODE`, `LOKI_BROKER_ENCRYPTION_KEY`, and `LOKI_SECURE_COOKIES=1` behind any non-Vercel HTTPS proxy.

### Privacy, terms and age

- `/privacy`, `/cookies` and `/terms` (v6) are public and describe exactly what the code does: functional first-party cookies only (no analytics, advertising or third-party cookies — so no consent banner), browser-local storage for journals/watchlists/chat, optional account saves, chat turns kept at most 30 days, broker credentials never stored.
- **Age:** entry and sign-up require an "I am 18 or older" confirmation, enforced server-side; no birth date is asked for or stored.
- **Data rights:** `GET /api/account/export` (download my data), `POST /api/account/delete` (type DELETE; removes saves, published score and the login), and a "clear Loki data from this browser" control on `/privacy`.
- **Minimisation:** no third-party requests from the browser (Google Fonts removed; CSP allows no external hosts).
- **Claims:** performance figures are labelled as hypothetical/simulated with a limitations disclaimer; marketing lines that the evidence did not support were rewritten.
- Set `LOKI_OPERATOR_NAME` and `LOKI_OPERATOR_EMAIL` so the policies name the data controller and a contact address. Have the policies reviewed for your jurisdiction.

## 4. Run, test and deploy

### Desktop app (Windows and macOS)

Loki also runs as an app on your own computer: the same workspace in its own window, with the server and every trading AI running on the laptop.

- **Download:** the [desktop-latest release](../../releases/tag/desktop-latest) has `Loki-Setup-Windows.exe` (installer, no administrator rights needed), `Loki-Windows-portable.zip` (runs without installing) and `Loki-macOS.dmg` (Apple silicon).
- **First launch:** the app is not code-signed. On Windows choose *More info → Run anyway*; on macOS open *System Settings → Privacy & Security → Open Anyway*.
- **Your data** (journals, models you train, settings) is saved in `%LOCALAPPDATA%\Loki` on Windows and `~/Library/Application Support/Loki` on macOS, and is kept when the app updates or is uninstalled. The app listens on `127.0.0.1` only, so nothing is reachable from your network, and it has no access gate.
- **How it works:** `desktop/loki_desktop.py` starts the FastAPI server in the background and opens it in a native window (Edge WebView2 on Windows, WebKit on macOS); closing the window stops the server. If no native window is available it opens Chrome or Edge as an app window. `--browser` forces that; `--smoke` and `--window-check` are the self-tests the build runs on each packaged app.
- **Build it yourself** on the OS you want it for: `python -m pip install -r requirements.txt -r desktop/requirements-desktop.txt`, then `python desktop/build.py` (output in `dist/`). The [Desktop app workflow](.github/workflows/desktop-app.yml) does this on Windows and macOS for every change on `main`, tests both apps (the server and the real window), builds the installer and the disk image, and replaces the `desktop-latest` release.
- **Run from source** without packaging: `python desktop/loki_desktop.py` (add `pywebview` for the native window).

### Run locally

Requires Python 3.12. From the repository root, on Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8124
```

On macOS or Linux:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

```bash
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8124
```

Optional environment variables: `LOKI_SITE_GATE=1` enables the gate (default code `flathead`; set `LOKI_SITE_CODE` to change it), `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` enable the login flow, Alpaca keys are browser-held and verify through the relay (`/api/broker/alpaca/*`) whenever the user enters them.

### Checks

```bash
python -m pytest -q -o addopts=""
```

```bash
node --test tests/test_ai_trading.js tests/test_engine.js tests/test_games.js tests/test_redteam.js tests/test_pumpfun.js
```

```bash
flake8 loki loki_v8 core --select=E9,F63,F7,F82,F401
```

```bash
mypy loki
```

`tests/test_code_structure.py` keeps the house rules: every Python module, script and stylesheet starts with a description, and every browser script is self-contained (the shared workspace helpers are taken by name from `window.LokiWorkspace`).

### Deploy and automation

| Workflow (`.github/workflows/`) | When | What it does |
|---|---|---|
| `ci-cd.yml` | Pushes to `main` and every pull request | Lint, type check, all tests on Linux and Windows, a security scan, container build and smoke test |
| `research-workspace.yml` | Pushes to `main` and every pull request | A focused offline regression suite on Linux and Windows |
| `ai-evolution.yml` | Sundays 05:23 UTC | One generation of the AI's self-review, opened as a pull request |
| `alpaca-paper-forward.yml` | Daily 14:45 UTC | Mirrors the Portfolio AI on an Alpaca paper account (skips without paper keys) |
| `daytrade-evolution.yml` | Sundays 06:41 UTC | The Day Trader's self-review: walk-forward search, shadow test, a pull request only if both pass |

Vercel deploys `main` to the live site. `GET /health` reports the running version.

### Rebuilding the tested records

| Script | Rebuilds | Notes |
|---|---|---|
| `train_long_history.py --write-snapshot` | `loki_v8/training_report_snapshot.json` (live AI and Portfolio AI) | About 30 minutes on 4 cores |
| `train_daytrade.py --cache DIR` | `loki_v8/daytrade_record.json` (Day Trader) | Needs Alpaca market-data keys in `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` |
| `evolve.py --write-snapshot` | `loki_v8/evolution_log.json` | Run weekly by `ai-evolution.yml` |
| `evolve_daytrade.py --cache DIR --write` | `loki_v8/daytrade_evolution.json` (and the record, on promotion) | Run weekly by `daytrade-evolution.yml` |
| `crosscheck_alpaca.py` | Nothing; prints an independent-data check | Uses Alpaca's own daily bars |
| `alpaca_forward.py` | Nothing; trades an Alpaca paper account | A dry run unless `--execute` is passed |
| `train_memecoin.py --cache DIR --write` | `loki_v8/memecoin_record.json` (big-memecoin bot) | Binance's public 4-hour and daily bars |
| `train_pumpfun.py record --minutes N --out DIR` | Nothing; records every pump.fun launch and trade from the program | About 30 launches a minute; needs a Solana node (`SOLANA_WS_URL`, public by default) |
| `train_pumpfun.py train --data DIR` | `loki_v8/pumpfun_record.json` (pump.fun bot) | Fit, check and test on launches in time order |
| `train_pumpfun.py paper --minutes N` | Nothing; runs the pump.fun bot live on paper | The same machine as the browser bot |
| `memecoin_shadow.py` | A local SQLite ledger | Shadow mode for new Solana launches (2.77) |
| `train_analogue.py`, `train_public_derivatives.py` | Local research models under `state/` | See `docs/` |

## 5. Project layout

```text
main.py                  entry point for uvicorn and Vercel
loki/                    the web application
  version.py             the release number (shown by /health, used to refresh cached assets)
  api/server.py          the FastAPI app: pages, security headers, access gate, accounts, core APIs
  api/*.py               one module per feature (table below)
loki_v8/                 the trading engine: indicators, strategy, backtest, live engine,
                         Portfolio AI, Day Trader, training
  *.json                 the tested records the site serves
core/                    research engines: strategy lab, Talk to Loki's copilot, core/fusion/*
third_party/             small vendored MIT sources with licences and hashes
templates/               the pages (index.html is the workspace)
static/css/              stylesheets (style.css is the base)
static/js/               one script per feature
static/js/engine/        the on-device engine (window.Loki)
static/vendor/three/     Three.js r170 for the 3D map (MIT)
tests/                   the regression suite (pytest, plus five Node test files)
docs/                    setup and research notes
supabase/                SQL to run once in the Supabase project
.github/workflows/       CI and the scheduled jobs
*.py (root)              scripts that rebuild the tested records (section 4)
tools/                   developer tools (code_health.py: the weekly code-health sweep)
desktop/                 the desktop app: launcher (loki_desktop.py), PyInstaller build, Windows installer
```

**`loki/api`, by feature**

| Area | Modules |
|---|---|
| App, access and accounts | `server.py`, `site_access.py`, `member_data.py`, `rate_limit.py`, `revenue.py` |
| Trading AIs | `ai_paper.py` (live AI, Portfolio AI, Day Trader, evolution), `alpaca_ai.py` |
| Brokers | `broker_alpaca.py`, `broker_tradovate.py`, `funded.py`, `broker_workspace.py` |
| Markets and news | `live_markets.py`, `market_feed.py`, `binance_market_data.py`, `watchtower.py`, `news_desk.py`, `replay_data.py`, `feed_status.py`, `engine_feeds.py` |
| Prediction markets | `polymarket.py`, `polymarket_bot.py` |
| Memecoins | `memecoin_api.py` (big-memecoin plan, scan, launch radar), `alpaca_memecoin.py`, `pumpfun_api.py` (pump.fun record, live relay, curve reads), `order_safety.py` |
| Talk to Loki | `loki_brain.py`, `loki_wording.py` |
| Research | `fusion.py`, `analogue.py`, `proof_engine.py`, `proof_rate_store.py`, `strategy_backtest.py`, `indicator_page.py` |
| Games | `dive_board.py`, `dive_sessions.py` |

**How the browser scripts fit together**

- Every script is self-contained. Scripts talk to each other only through named objects on `window` (`Loki` for the engine, `LokiWorkspace` for the shared workspace helpers, `LokiCurrency`, and a few more) and through page events such as `loki:view`.
- `static/js/workspace.js` loads before the scripts that use it and offers the shared helpers. Each of those scripts names what it takes on its first line.
- The vendored sources in `third_party/` are pinned by hash: `tests/test_fusion_provenance.py` fails if a byte changes.

## 6. Feature reference

### On-device engine (2.52)

Most of the thinking now runs in the visitor's browser (`static/js/engine/`), so Vercel mostly serves files and a few relays:

- **Device-direct data.** Binance market data, Polymarket odds and order books, and Alpaca (with the visitor's own keys) are called straight from the browser; these hosts send CORS headers and are the only ones the CSP `connect-src` allows. Stock history and headlines still come through Loki's cached relays (Yahoo sends no CORS headers). Past replay windows are edge-cacheable (`/api/replay/bars`).
- **AI council** (`/#council`, `engine/council.js`). Six bots (Trend Hawk, Reversion Owl, Momentum Fox, Volatility Bear, Swarm Bee, Pattern Raven) each predict P(higher in 5 bars). Weights are earned with the Hedge rule on decayed log-loss, blending each symbol's record with the all-symbol record. A first visit replays ~250 walk-forward decisions; every live call is stored and scored when its horizon passes, with a written reflection. It is a learning statistical engine, not a conscious mind. Since 2.57 each bot states how far it expects the market to depart from its *usual* up-rate (markets rise more often than they fall), and the page scores the council against always guessing that usual rate rather than a coin flip. Measured over 10 years of 21 markets (22,000+ walk-forward calls per set) its skill over that guess is about zero: five-day direction is close to unpredictable, and the page says so.
- **Autonomous sentiment swarm** (`engine/swarm.js`). Price Pulse, Vol Burst, Book Imbalance, Odds Mover, Newswire and Social Chatter report alarm/direction/confidence; a quorum across families (market + crowd) is a volatility event that shrinks position size and biases the Swarm Bee. Lead time over the first matching headline is measured, never assumed. Social feeds are best effort and say when a browser blocks them.
- **Self-healing risk matrix** (`engine/risk.js`). Drawdown limit = 2.5 × recent weekly volatility (3–15%); GREEN / AMBER (half size) / RED (no new entries). On RED it plans exits by liquidity: thin prediction-market positions are hedged with the opposite outcome token, thin stock/crypto/futures positions get sliced limit exits, liquid ones are halved. After a 30-minute cooldown and a calm swarm, size ramps back 25 → 50 → 100%.
- **Monte Carlo** (`engine/mc_core.js`, run in a Web Worker). 5,000 paths per order: log-normal network delay, price drift during the delay, fills walking a randomly thinned book (or spread + impact), fees and on-chain gas spikes, block-bootstrapped horizon returns or a fair-probability draw for binaries. Confidence = share of profitable paths after costs, plus VaR/CVaR and p99 slippage.
- **Predictive arbitrage engine** (predictions page, `engine/arb.js`, paper only). Prices Polymarket's 5-minute "Up or Down" crypto contracts exactly as they settle (Chainlink 60-second TWAP vs start price) from Binance 1-second candles, compares both books after the market's fee (rate × p(1−p)), and paper-fills only if the edge survives the measured round trip plus signing time. It records signal-vs-fill edge decay and book lag. No real orders are ever sent.
- **Replay lab** (`/#replay`, `engine/replay.js`). FTX, LUNA, Black Thursday, the yen unwind, election night 2024, tariff shocks, COVID, DeepSeek and the 2022 bear market, played bar by bar with the council (learning inside the replay), a volatility alarm, the risk matrix and the co-pilot's stops, against buy-and-hold.
- **3D engine map** (`/#execution-map`, `engine/execmap.js`, Three.js r170 vendored under `static/vendor/three`, MIT). A calm 3D map: your device in the middle, one ring of venues it talks to and an inner ring of the engine's agents. Real requests (from a fetch tap that records host, status and timing only) travel as comets and orders orbit as cubes. Only venues and agents that have done something are lit and labelled; idle ones are grey dots. One line of live figures sits under the scene, and the tables list active rows (with "Show all").
- **Strategy co-pilot** (top-bar ✦ button or Ctrl/Cmd+J, `engine/copilot.js`). Deterministic parsing of plain-language instructions into a bounded config diff with JSON preview, apply-on-confirm and undo. Example: "Shift dynamic trailing stop-loss down by 1.5% and hedge current prediction positions if volatility spikes above 40%."
- **Proof-of-Strategy** (`engine/audit.js`). Every execution is appended automatically to a SHA-256 hash chain signed with a non-extractable ECDSA P-256 device key (IndexedDB). The badge recomputes figures from signed entries only; logs export with the public key and can be re-verified anywhere; Alpaca entries reconcile against Alpaca's own order records.

### Red Team lab (2.55)

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

### Engine v3 and the Command Center (2.54)

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

### Trade terminal

`/#trade` (`static/js/trade.js`) sends real orders to **Alpaca paper or live** (browser → Alpaca directly; paper keys saved in the browser, live keys kept only for the tab) and **Tradovate demo or live** (`loki/api/broker_tradovate.py`, a stateless relay that stores nothing: credentials → token once, then token per request; contracts capped at 10 per ticket). Guardrails:

- Live trading must be armed per session by typing "trade real money".
- Every live order shows its full details for confirmation and must pass every pre-trade check (council, Monte Carlo, risk matrix, notional cap). There is a 20-live-orders-a-day cap.
- The risk breaker blocks new live entries, and a kill switch cancels everything and flattens.
- The autopilot only ever trades paper/demo accounts.
- Every order lands in the signed log.

Polymarket stays paper-only: real orders there would need a wallet private key.

### Live markets

`/#live-markets` is a self-refreshing board (every ~15 s while the view is open and the tab is visible) covering crypto majors, US mega-caps, indices, commodities and FX, with session status, top movers, flash-on-change tiles and a personal watchlist saved in the browser. A candlestick chart sits at the top of the view (the S&P 500 until you pick another tile): 1m, 5m, 15m, 1h, daily and weekly candles (weekly built from daily), candles or line, 20/50-period moving averages, a volume pane, and a crosshair readout of open/high/low/close/volume by mouse, touch or arrow keys. The newest candle follows the live board price between candle refreshes. Below it, the AI engine's review-only read for that symbol. Served by `loki/api/live_markets.py` (`/api/markets/live`, `/api/markets/quotes`); crypto comes from Binance Spot (live), everything else from Yahoo Finance (near-live). Failed feeds stay visible per tile.

### Prediction markets (Polymarket) and Crowd vs Machine

`/#predictions` shows live Polymarket odds — Finance, Economy & Fed, Crypto, Stocks, Politics, Trending, plus search — through a read-only relay (`loki/api/polymarket.py`: fixed host `gamma-api.polymarket.com`, 90 s cache, 8 MB cap, whitelisted fields, Polymarket links validated). **Loki never places Polymarket bets**: real-money orders need a crypto wallet's private key and are geo-restricted in several countries. Visitors practise with **paper predictions** (1,000 play credits in the browser; positions revalue from live odds and settle when a market resolves).

**Crowd vs Machine** (only on Loki): for every Polymarket price ladder ("Bitcoin above ___ on November 11?", "NVIDIA closes above ___ …") the ladder is smoothed into a monotone curve, giving the crowd's chance of finishing above today's price and its median expected price; Loki's trading AI reads the same market, and each card says whether they agree, disagree or are mixed. Ladders at least 3 days out are preferred (a ladder settling within hours is a coin flip). Talk to Loki answers odds questions from the same data ("will the Fed cut rates in December?").

The top price ticker now updates prices in place, so its loop never restarts and every symbol on the board comes round.

### Polymarket paper bot

The **Loki Polymarket bot** (`/#predictions`, `loki/api/polymarket_bot.py`) trades Polymarket price ladders with paper money:

- **Fair value:** live spot and 30-day realised volatility give a lognormal chance of finishing above each strike, blended 50/50 with the crowd's mid-price (the crowd often knows about earnings or quiet weekends the model does not).
- **Entries:** it buys the YES token (view: above) or the NO token (view: below) only when fair value beats the price actually payable on the live CLOB book (`clob.polymarket.com/book`, asks walked level by level) by at least 3 points after Polymarket's taker fee. Rungs with a YES spread over 10c, long shots under 10c and near-certainties over 90c are skipped.
- **Sizing:** quarter-Kelly, at most 5% per trade and 30% invested, one rung per ladder and two per underlying.
- **Exits are checked before any entry, every minute:** out 30 minutes before the market locks, a 40% stop-loss, take-profit once 60% of the remaining upside is in hand, or when the edge is gone. Sells walk the bids, so exit slippage counts. A market the bot just left is not re-entered for 6 hours.
- **Orders** are shown in Polymarket's CLOB shape (`token_id`, `side`, `price`, `size`, `FOK`) for review. **Loki never holds a wallet private key and never signs or sends an order**; the paper account lives in the browser (`loki_poly_bot_v1`).
- **Not built, on purpose:** latency arbitrage on 5-minute markets (a website server cannot win races against co-located bots) and market making (paper fills for resting orders would be invented).

API: `GET /api/polymarket/bot/signals?bankroll=&exposure=` and `POST /api/polymarket/bot/review` (max 20 positions), rate-limited.

### Alpaca and Tradovate connectors

**Alpaca (paper).** Keys stay in the visitor's browser and travel per request to a relay that only talks to `paper-api.alpaca.markets` (and `data.alpaca.markets` for bars). The panel verifies keys, shows account facts and holdings, places capped manual orders, and now also: **cancels** open orders from the log, **closes** a whole position, and **lets the Portfolio AI manage the paper account** — *Preview the AI's trades* shows exactly what it would do; *Place these paper orders* (typed confirmation server-side, plan recomputed at that moment, sells before buys, at most 25 orders) places them. The same logic (`loki/api/alpaca_ai.py`) drives `alpaca_forward.py`. Minute bars now show the last session in evenings and weekends instead of an error. Validation errors never echo submitted values, so keys can't bounce back into logs.

**Tradovate (read-only).** Sign in with Tradovate API credentials (the password is used once; only the issued token is kept, encrypted, owner-bound, ≤1 hour) or paste a token. Positions now show contract names (e.g. `MESZ6`) and balances their currency, when Tradovate allows those reads. Needs `LOKI_BROKER_ENCRYPTION_KEY` on the server.

**Independent-data check** (`crosscheck_alpaca.py`, run 2026-11-08 with Alpaca's own adjusted daily bars): the live stock profile gives +9.22%/yr (Sharpe 0.61, 12/13 profitable) on Alpaca data vs +9.30%/yr on Yahoo over 2017→now — correlation 0.999 across symbols. Crypto (2022→now) +9.64%/yr vs +10.55%, correlation 0.988, but Bitcoin differs (+5.2% vs +11.5%) because Alpaca's crypto volume is its own venue's; the crypto profile's volume factor makes it somewhat data-source sensitive. Nothing was tuned on this.

### Talk to Loki

The workspace "Talk to Loki" panel runs on `loki/api/loki_brain.py`: name any ticker or coin and it runs the live AI engine and explains every check in plain English; it also answers market checks, the AI's track record, how the AI works, and ~30 trading terms. No external language model is used; questions about your own portfolio/research go to the context-aware copilot.

### AI self-check, daily briefing, price alerts

- **Honest uncertainty:** the Portfolio AI's held-back result comes with a 90% block-bootstrap range (`holdout_ci`, currently about +5% to +23% a year).
- **Self-health check:** every live plan compares the strategy's last 12 months with all rolling 12-month windows from 2005-2022 (`expected_range`) and reports `normal`, `below_range` or `above_range`.
- **Daily AI briefing** on the overview: health meter, this week's entries/exits, and the uncertainty range; Talk to Loki answers "how is the AI doing?" and "what changed this week?".
- **Price alerts** on Live markets: above/below alerts stored in this browser, checked on every board refresh, with an in-page toast and an optional browser notification.
- The operator named in the policies defaults to Joshua Mathew (`LOKI_OPERATOR_NAME` / `LOKI_OPERATOR_EMAIL` override it).

### Status lights and paper badges

The top bar shows a **feed light** fed by `GET /api/status/feeds` (`loki/api/feed_status.py`): Yahoo stock quotes, Binance crypto quotes, Polymarket odds, Polymarket order books, the trading-AI profiles, accounts and login security, each checked with a tiny live request (parallel, 45 s cache, refreshed every minute while the tab is visible). States are Live, Slow, Not set up and Down; details never include hosts or secrets. A pulsing **PAPER TRADING · SIMULATED** badge sits beside it and turns red (**REAL ACCOUNT**) only in the real-account view; every paper P&L panel carries a **SIMULATED** tag.

### Opening experience

`static/js/loki_scene.js` + `static/css/loki_scene.css` add a once-per-visit intro, animated market canvases, a live price tape and motion to the gate page and overview, plus an interface motion system: view transitions, 3D card tilt, a cursor aura, ambient aurora, numbers that count to new values, self-drawing charts and an animated allocation ring. `static/js/loki_radar.js` draws the **AI Market Radar** in the home banner — the Portfolio AI's 21 markets placed by live trend score, held positions glowing and pinging under a rotating sweep (data from `/api/fusion/ai/portfolio`). All presentation only; honours reduced-motion and pauses in hidden tabs.

### Games (Bored?)

- **Market Word** (`static/js/market_word.js`): a five-letter trading-vocabulary puzzle with a definition for every answer. Daily (same word for everyone, by date) and unlimited Practice; hint, hard mode, colour-blind palette, streaks, guess distribution and a copyable result grid.
- **Beat the Bots** (`static/js/beat_bots.js`): ten rounds of real daily crypto history (Binance, device-direct) at a random date with the name hidden. You and the six-bot council each call the next five days; sure calls pay double and cost points when wrong; "Ask a bot" reveals one bot's view. Name and date are revealed after each call.
- **Loki Dive** (`static/js/game.js`): rare-answer trivia, now 69 hand-picked prompts including markets topics (crypto, exchanges, S&P 500 members, investors, candlesticks, indicators, central banks, crashes). Daily runs stay server-verified because the server reads the same bank.

## 7. Release notes

Newest first. Figures in older notes are as they stood at that release; current figures are in section 2 and on the site.

### 2.87: Loki's memory, self-study and learning from mistakes

The **Loki's memory** panel (AI trading → Portfolio) shows a memory that studies itself (`loki_v8/mind.py`, `GET /api/fusion/ai/memory`). Four steps:
1. **Remember.** Every 5 trading days over the last ~2 years, each of the 21 markets' decisions (held it or stayed out) is recorded, with the trend score, regime and price the AI saw that day, and nothing it could not have known then.
2. **Grade.** Five trading days later, every decision is marked against the real price: *right*, *mistake* (held and it fell) or *missed move* (stayed out of a big rise).
3. **Learn lessons.** Mistakes are grouped by the conditions the AI saw: regime, trend-score band, kind of market, and regime × kind. A group with 25+ graded decisions that is right at least 8 points less often than average and loses money becomes a *weak spot*. One that clearly beats the average becomes a *strong spot*. Most-missed markets and recent form (getting better or worse) are tracked too.
4. **Prove before acting.** Weak spots are learned from the earlier 60% of decisions only and judged on the later 40% they never saw.
   - **Adopted** only if holding half of them would have made more money on those later decisions and beaten 90% of 60 randomly relabelled groups. An adopted lesson halves that setup's weight in the live plan, and the allocation is marked "half size".
   - **Otherwise** a lesson is shown and never used. A filter that chases its own past mistakes without this check overfits and loses money.

**Where else it shows up:**
- **Talk to Loki** answers from its memory ("what have you learned?", "what mistakes did you make?").
- **The desktop app** also keeps a permanent journal of the live decisions on your computer (`mind_journal.json` in the app-data folder). It is graded as the days pass and kept across rule changes.

**Alongside the existing self-evolution:** every week the AI still competes against challenger versions of itself, and the learn-from-mistakes filter is retrained on every closed trade.

**Honest current reading** (cached data to mid-November 2026):
- 2,184 decisions remembered; when holding, right 53% of the time, with an average week of +0.21%.
- The latest 60 held decisions are flagged as **slipping** (30% right).
- Weak spot: crypto in a bull regime was right 33–36% of the time. It did **not** hold up on later decisions, so it is shown and not used.

### 2.86: Loki and Sure as one app on your computer

**The desktop app now runs Sure alongside Loki.** If Docker Desktop is installed and running, opening Loki starts [Sure](https://github.com/we-promise/sure) (personal finance) in the background, and "Your money" connects to it.
- **What the app does:**
  - copies `desktop/sure/compose.yml` (Sure's web app, worker, Postgres and Redis) into Loki's app-data folder;
  - writes a `.env` next to it with secrets created once on your computer, readable only by you: Sure's `SECRET_KEY_BASE`, the database password and its three encryption keys;
  - runs `docker compose -p loki-sure up -d`.
- **Sure stays private and separate:** Sure listens on `127.0.0.1:3000` only and runs from its official, unmodified image, so it keeps its own AGPL-3.0 licence. Loki talks to it only over its API.
- **Your Sure data** lives in Docker volumes and survives restarts and Loki updates. Closing Loki leaves Sure running, so it is ready next time. To stop it, run `docker compose -p loki-sure down` in that `sure` folder. Set `LOKI_SURE=0` to never start it.
- **The "Your money" page shows Sure's state:** running (with an **Open Sure** button for first-time setup: create your account, then Settings → API key), starting (the first start downloads about 2 GB), Docker not installed (with a link to Docker Desktop), or failed. In the app it fills in Sure's address for you.
- **Real amounts now parse in any currency:** Sure sends formatted amounts such as "$60,000.00", and they are now read correctly in every currency format ("€1.234,56", "A$1,234.50", "-$12.00").
- **Fixed:** while Loki's AI was warming up, the page wrongly said none of your holdings were followed.
- **Tested against real Sure:** the launcher set Sure up from an empty data folder, then Loki read a seeded account through Sure's API: net worth $177,200, three accounts, three holdings, with the AI's trend reading beside SPY and NVDA.

### 2.85: your money from Sure, read by Loki's AI

**Your money · Sure** is a new workspace view. It connects to your own [Sure](https://github.com/we-promise/sure) personal-finance server (Sure is the community fork of Maybe Finance) and shows your net worth, assets, liabilities, accounts and investment holdings.
- **Loki's reading:** beside every holding Loki follows, the Portfolio AI shows its trend score, its regime, and whether its own simulated book holds it.
- **Plain facts about your mix:** your largest holding's share, how much is in crypto, how much is in cash, and how many of your holdings are in a downtrend. Readings, never advice.
- **Read-only:** Loki only sends fixed `GET` requests to Sure's `/api/v1` (balance sheet, accounts, holdings) and never writes to Sure.
- **Your key stays with you:** the Sure address and API key stay in your browser (in this tab only, unless you tick *Remember on this device*) and go with each read. The server never stores, logs or echoes them.
- **No reaching inside private networks:** on the website, a Sure server must be on the public internet over https. Private, loopback and reserved addresses are refused and redirects are not followed. The desktop app can also reach a Sure server on your own computer or home network. Cloud-metadata (link-local) addresses are always refused.
- **Never kept waiting:** if Loki's AI is still warming up, your money shows straight away and the AI reading follows on the next refresh.
- **Licences:** Loki talks to Sure only over Sure's published API. No Sure code is copied into Loki, so Sure keeps its AGPL-3.0 licence and Loki keeps its own.

**Fixed:** number tiles across the site (`.metric`) could freeze on a wrong number when they changed. Two scripts (aurora.js and loki_scene.js) were both counting the same tile up to its new value, and aurora also mistook its own animation frames for new values. Each tile now has one animator, and aurora ignores the text it wrote itself.

### 2.84: Loki as a desktop app, and a lighter, faster workspace

**Desktop app.** Loki now installs as an app on Windows and macOS. It shows the same workspace in its own window, and the server and every trading AI run on your computer. See [Desktop app](#desktop-app-windows-and-macos) for download and first-launch steps.
- Everything you save goes to your own app-data folder, and local secrets are created once on first launch.
- The app listens on `127.0.0.1` only.
- Every build is tested on Windows and macOS before it is published: the packaged server answers, and the real app window loads the workspace.

**Faster and lighter** (measured in a headless browser on the overview and 3D map):
- **Overview radar:** the dish, rings and labels are drawn once and reused. The sweep runs at 30 frames a second instead of 60, and the radar stops drawing entirely while paused.
- **Background scene:** runs at 30 frames a second with exactly the same speed of motion, and the cursor glow stops working once it has caught up with the pointer.
- **3D engine map:**
  - It renders at 30 frames a second while only the slow spin is moving, and at full rate while you drag, fly to a node or traffic is in flight.
  - Nothing runs while the map is off screen.
  - It skips three.js's per-shader error checks.
- **Shared requests:** when several panels need the same read-only data on load (the radar, the daily briefing and the Portfolio AI all read the portfolio), one request now answers all of them. A shared answer is reused for at most 15 seconds, so periodic refreshes stay fresh.
- **Compression:** pages, scripts, styles and JSON are gzip-compressed. Answers under 1 KB and the live pump.fun stream are left as they are, so each stream line still arrives at once.

### 2.83: unused code removed

A sweep for code nothing uses: every Python module was traced from the real entry points (the server, the CI and training scripts), every API route was checked against the pages and scripts that call it, and every script, stylesheet and element id was checked against the pages.
- Python: a legacy question helper in Talk to Loki, a placebo function in the pump.fun bot that no report called, a leftover Binance address, a config helper and a rate-limit reset that only tests used, and seven wording constants for the revenue page that no page rendered (the page carries its own text, which the tests check).
- CSS: about 80 selectors for elements that no longer exist (the old overview header, launcher icons, workflow cards and goal note, sidebar labels, "new" badges).
- Kept on purpose: the revenue ledger API (the operator records profits through it), and the unchanged upstream copies under `third_party/*/upstream/`, which the licences and provenance files point to.

### 2.82: the private chat room is gone

Loki's private chat room (`/ai-chat`) is removed, with all of its code: the server module (`loki/api/ai_chat.py`, its sign-in, passcode, cookies and 30-day message store), its page and script, the hosting guide, and its tests. **Talk to Loki** in the workspace stays and is unchanged; the home page's "Talk to Loki" link now opens it.
- The sidebar link, the access gate's "next page" option, the privacy and cookie notices about chat messages and the `loki_chat` cookie, and the operator settings `LOKI_CHAT_*` went with it.
- `/ai-chat` now answers 404; the server no longer stores any chat messages.

**A shorter overview** (about a quarter shorter):
- The research tiles and the performance chart stay hidden until you have actually run research in the Quant lab; before that they only showed dashes and "Awaiting evidence".
- The four "From idea to evidence" cards (which repeated the sidebar) and the closing win-rate note are gone.
- The "Build your process" checklist spans the page with its four steps side by side.

### 2.81.1: 3D map table toggle

- "Show all" on the 3D engine map now redraws the tables at once instead of on the next one-second refresh. The live button sweep after 2.81 found the delay: the button changed its label, but the table still showed only active rows.

### 2.81: Jupiter as a second memecoin connector, a calm 3D map, clearer messages

**Jupiter connector** (`loki/api/pumpfun_api.py`). Jupiter routes pump.fun tokens on the bonding curve ("Pump.fun") and after graduation ("Pump.fun Amm"), so it prices any pump.fun token at any stage, plus every other Solana token.
- `GET /api/fusion/ai/pumpfun/quote?mint=…&side=buy|sell&amount=…`: Jupiter's executable quote. No route is an ordinary answer (`available: false`), not an error.
- The pump.fun paper bot checks every fill against it: Jupiter's quote for the same buy when it lands, and for the same sale when it closes, shown beside each trade with the typical gap. In a first live run the typical gap was about 7%. Sales into a falling price were the worst (up to 35% below the paper fill), because the price keeps falling between the curve reading and the quote. That is a real limit of paper fills in a crash, and the bot now shows it instead of hiding it.
- "Read its curve" now works for any token: the pump.fun curve when there is one, and Jupiter's price, liquidity, 24-hour change and route always.

**3D engine map: rebuilt to be calm.**
- **Kept:** one core, one ring of venues, one ring of agents.
- **Gone:** the starfield, nebula, aurora, shooting stars, scan waves, dashed rings, energy shield, particle disk, wireframe shells and lens streak.
- **Labels and colour:** only venues and agents that have done something are labelled and in colour; idle ones are grey dots, and their arcs are nearly invisible.
- **Overlays:** the four stat boxes no longer cover the scene; one line of live figures and a short legend sit under it.
- **Tables:** they list active rows, with "Show all".

**Clearer messages.**
- A paper buy the bot skips now says why ("price jumped 24% before the order landed, over the 15% cap") instead of "buy failed".
- The Real account panel explains that exchange balances need the Loki launcher on your own computer, instead of showing an error on every visit.

### 2.80: a pump.fun bot read straight from pump.fun's program, and every bot retrained

**pump.fun connector and paper bot** (Memecoins tab; details in section 2):
- reads launches, trades and graduations from pump.fun's own Solana program, through a server relay that sends the browser about 10 KB a second;
- paper-trades new launches on the real bonding curve with the program's fees, a landing delay, a slippage cap and a transaction cost;
- a launch X-ray: every signal the bot reads and each rule check, for any live launch;
- "Read its curve": the live bonding-curve state of any pump.fun token, read from its account;
- `train_pumpfun.py record | train | forward | paper`.

**Trained honestly, and it did not pass.** Three kinds of bot (a rule grid, a learned filter and a swarm of 15 rule agents that buys on a quorum) were fitted on the first half of 2,107 recorded launches and compared on the next quarter. The winner lost 12% a trade on the last 527, which it never saw; buying every launch lost about 8%. On the next 542 launches, with the rule frozen, it made +34% a trade from 29 trades, with a 90% range of −6% to +75%: too noisy to call an edge, so the verdict stands. Copying "smart" wallets lost 7% a trade on later launches. The bot is a live paper experiment, labelled as one.

**Day Trader: self-review re-run.** The adaptive setting beat the current rules over all 119 unseen months since 2017 (Sharpe 1.03 against 0.90) but was slightly worse over the last 24 months (−0.61 against −0.60), so the current rules stay. Both versions of the plain rules lost money over those 24 months; the "learns from its mistakes" filter is what keeps the 2022 → Nov 2026 result at +7.4% a year (simulated). The scheduled Sunday job records the review.

**Big-memecoin bot: retrained on fresh bars.** The same rule was picked again: +13.5% from July 2024 to now while holding the coins lost 83%. Its Sharpe of 0.33 is still under its 0.5 bar, so it still does not pass.

### 2.79: checked end to end, tidier on phones

**Everything checked live.** Every view and page on the live site was loaded at desktop and phone sizes, and 257 buttons were clicked one by one: no page errors, no failed requests and no server errors.

**Faster repeat visits, never a stale file** (`loki/version.py`, `loki/api/server.py`). Every `/static` link now carries the release plus the deployed commit (`?v=2.79.0-abc1234` on Vercel). A file asked for with the current tag is cached for a year; any other request is revalidated each time. A browser can no longer mix a new page with an old script after a deploy.

**Phones:**
- The top bar wraps onto a second row instead of hiding the feed light and Sign in past the edge of the screen.
- Money figures in the portfolio and Red Team tiles no longer split mid-number ("A$14,760." on one line, "86" on the next); the type now shrinks to fit the tile.
- Tables that scroll sideways no longer split "+12.0%" or "GOOG" across two lines.
- The Alpaca, Tradovate and broker key fields stack in one column, so a pasted key can be read end to end.

**Feed light:** reads "Feeds live" when every feed that is set up is live. Optional feeds without keys are still listed in the popover as "Not set up".

### 2.78: hardening the bots against a misbehaving world

**Orders that are never placed twice** (`loki/api/order_safety.py`, used by the Day Trader and the memecoin bot). Every order already had a fixed client id built from its signal. Now, when Alpaca's answer to an order is lost (a timeout or a 5xx), the bot asks Alpaca for that order by its id before reporting anything:
- **found:** the order landed;
- **no record:** nothing was placed, and the next check retries with the same id;
- **lookup fails too:** the result is reported as unknown, and the next check, which re-reads the account first, settles it.

Every check was already a full reconciliation against Alpaca's positions and orders.

**Memecoin bot on Alpaca paper:** a daily loss stop. At 3% down on the day it sells its coins and stops buying until the next day.

**Shadow runner** (`memecoin_shadow.py`):
- **Strict trade states:** PENDING_ENTRY → OPEN → PARTIAL_EXIT → CLOSED, or FAILED_SAFE. An illegal move raises an error, and an entry stuck pending for a minute is rolled back.
- **"Simulate before signing":** before entering, it quotes a buy and then the sale of what that buy returns. Losing more than 10% on the round trip points to a hidden tax or a honeypot, and the token is rejected. Tokens with a transfer fee are rejected outright.
- **Latency:** a second quote 2 seconds after the decision stands in for the moment a real order lands. If the price moved more than 3% against it, the entry fails safe and only the fee is charged.
- **Cabal check:** the biggest launch buyers are traced one hop back to whoever funded their wallets. Three or more funded by the same wallet rejects the token. (One hop, not 3–5: deeper tracing on the public Solana endpoint would be rate-limited.)
- **Failover and breakers:**
  - Solana calls fail over across `SOLANA_RPC_URLS` plus the public endpoint, and a failing endpoint sits out for a minute;
  - new entries pause while more than half of recent outside calls fail;
  - a daily kill switch stops entries after US$100 of losses (`--kill-usd`).
- **Hash-chained ledger:** every trade event goes into a ledger that `--verify` re-checks.
- **Bug fixed:** `--report` would have crashed on the first closed trade (an SQL `is not None` where SQL needs `is not null`). The new chaos tests found it.

**Chaos and property-based tests** (`tests/test_chaos.py`, Hypothesis):
- **Random inputs:** 300 random RugCheck reports, 300 random DexScreener answers, 200 junk transactions and 400 random price paths (including NaN and infinity).
- **Exit-engine invariants:** nothing is sold twice or lost, a closed position never sells again, and each take-profit fires at most once.
- **Injected failures:**
  - 429 and 503 answers, malformed JSON and HTML;
  - a 90% flash crash, a price run before landing, a honeypot, no route to sell;
  - a stuck pending entry, the kill switch, the error breaker, RPC failover;
  - an illegal state change and a tampered ledger.

  Filters that crashed on non-dict fields were hardened.
- **Lost order answers** are tested for both bots.

**Interface:**
- **Memecoins tab:** live scan first, then the honest verdict, the paper-trading panel, how it was tested, and the launch radar at the bottom.
- **Tables:** figures no longer wrap mid-word.
- **Failing checks:** five failed checks in a row raise one alert; the bots keep trying, since positions may need closing.

**Already in place, so not rebuilt:**
- signed, hash-chained receipts for every order;
- a weekly offline self-review with a shadow test before any new setting goes live;
- the Red Team jury for adversarial review;
- volatility-scaled sizing in the memecoin rule;
- the Day Trader's learn-from-mistakes filter.

**Not built:**
- **A regime classifier or order-flow signals added to the live rules:** they are untested, and adding them would trade new rules without a test.
- **Maker or post-only orders:** they would trade fill certainty for fees, untested.
- **Polymarket-to-memecoin "front-running":** no evidence of an edge.
- **Language-model debate agents:** no language models in trading decisions.
- **3–5 hop funding graphs:** these need a paid RPC.

### 2.77: new-launch memecoins, Phase 1 shadow mode (paper only)

**Screening** (`loki_v8/launch_filters.py`, pure and tested). A new Solana token is rejected unless:
- its mint authority and freeze authority are revoked;
- its liquidity is at least 90% locked or burned, or it trades on a launchpad bonding curve;
- the top 10 wallets hold at most 25% (pools, bonding curves and lockers left out);
- RugCheck finds no linked insider wallets and no danger-level risk;
- wallets other than pools bought less than 40% of the supply in the launch slot and the next one, checked from Solana's own transaction history.

A token that passes is only entered on organic velocity: 5-minute volume at least 2x the pool's liquidity, buys at least 1.5x sells, and at least US$5,000 of liquidity.

**Exits:**
- sell 50% at 2x and 25% at 4.5x, then trail the last 25% 30% under its peak;
- exit everything if it has not reached +20% within 12 minutes;
- exit everything if it falls 30% below entry (a hard stop added to the plan as given, because rugs fall faster than a time stop);
- exit everything if the creator or a tracked top-5 holder sells more than half their tokens.

**Shadow runner** (`memecoin_shadow.py`). It reads RugCheck's new-token feed and DexScreener's latest Solana tokens every 20 seconds. It screens them, simulates US$20 entries and every exit with real Jupiter quotes (real price impact and pool fees), charges 0.0003 SOL per transaction for priority fees and tips, and logs every token, verdict and trade to SQLite. `--report` prints:
- the rejection reasons;
- the closed trades, expectancy and win rate;
- the Phase 2 gate: at least 200 closed trades with a positive net result.

A position it cannot sell for three checks in a row is counted as lost. It cannot measure failed or late transactions during a spike, MEV against a real order, or wash trading by one wallet (DexScreener counts transactions, not wallets), so real results would be worse. It holds no keys and signs nothing.

Because the site only runs while a page is open, the runner is a script for any always-on computer:

```bash
python memecoin_shadow.py --minutes 0 --db shadow.db
```

**Launch radar** (Memecoins tab, `/api/fusion/ai/memecoin/launches`). The 12 newest launches, each with its verdict and the reasons, refreshed every minute. It takes about 1.5 s and is cached for 60 s.

**Not built, by decision:**
- **Multi-wallet orchestration to hide a footprint from other traders:** it misleads the market and overlaps with manipulation.
- **A real-money engine holding keys:** the site never holds wallet keys.
- **X and Telegram sentiment agents:** X's API is paid and Telegram scraping is against its terms.
- **Language-model agents:** the deterministic filters come first.

Pump.fun's own API blocks automated requests, so new launches come from RugCheck's feed instead.

### 2.76: memecoin scanner and an Alpaca paper connector

**Connectors.**
- **Analysis:** Binance's public market data, already reachable from the hosted site.
- **Trading:** Alpaca, which trades all six coins as crypto pairs (DOGE/USD and so on), 24/7, on paper.
- **Not used:** DexScreener, CoinGecko and GeckoTerminal were considered, but they are not reachable from the build environment, so nothing built on them could be tested.

**Live scan** (`/api/fusion/ai/memecoin/scan`, in the Memecoins tab). For each coin:
- price, 24-hour change, volume and trade count;
- spread, and order-book depth within 1% on each side;
- whether the book leans to bids or asks;
- what the rule holds, and how far price must move to trigger the next buy or sell;
- a short read such as "deep book", "thin book" or "wide spread".

One 24-hour ticker call covers all six coins, and their order books are read at the same time. A scan takes under a second, and the server reuses it for 15 seconds. The page refreshes it every 20 seconds while the tab is open. It describes the market right now; it does not forecast.

**Paper trading on Alpaca** (`/api/broker/alpaca/memecoin`). With the paper keys saved on the Real account page, the memecoin rule trades the visitor's Alpaca **paper** account by itself, checking every 5 minutes while the tab is open.
- **Allocation:** 1–25% of equity (10% by default), split by the rule's weights. It uses cash only (Alpaca's non-marginable buying power), because crypto cannot be bought on margin.
- **When it trades:** it buys when the rule turns a coin on and sells it in full when the rule turns it off. There is no resizing in between.
- **Guards:**
  - no buy when Alpaca's spread is over 0.60% or its quote is over 5 minutes old;
  - US$10 minimum order;
  - fixed order ids, so a retried check cannot buy twice;
  - it refuses to start on an account already holding these coins;
  - Stop sells everything.
- **Records:** every order and every blocked order gets a receipt in the device's signed log.
- **No real money:** it never trades real money.

**Fixes found by testing the connector against a real paper account** (read-only calls; no order was placed):
- Crypto prices were being rounded to cents, which turned PEPE at US$0.0000043 into 0.
- Margin buying power (US$84,540) was used where crypto may only use cash (US$18,411).

**Is it profitable? No, and the record says so.**
- **Walk-forward:** a fairer test re-picks the best rule each month from the 6 months before it. It made +568% during the 2021–mid-2024 boom (holding made +8,116%), but **lost 46% since mid-2024** (holding lost 80%).
- **Fees are higher than tested:** Alpaca's memecoin spreads (0.24–0.47%) are wider than the 0.35% total cost per side assumed in the test.
- **Purpose:** the paper connector exists to build an honest record with real fills, not because the rule is expected to make money.

### 2.75: a memecoin bot, tested honestly (paper only)

**What it does.** The new **Memecoins** tab under AI trading follows DOGE, SHIB, PEPE, BONK, WIF and TRUMP (the memecoins Alpaca trades) on Binance's public 4-hour prices. It buys a coin after it breaks above its 10-day high and sells after it falls below its 5-day low. Wilder coins get smaller stakes, so each stays near an 80%-a-year volatility, and the rest of the money stays in cash. It shows what it holds now, its tested record, every rule tried, and its record since it was frozen.

**How it was tested** (`train_memecoin.py`, `loki_v8/memecoin.py`, `loki_v8/memecoin_record.json`, all fixed before any result was seen):
- **Coins:** the six above plus FLOKI, from each coin's listing (DOGE since 2019) to 14 November 2026.
- **Rules:** 15 plain rules from three families (breakout, moving-average trend, momentum) on 4-hour and daily bars.
- **Trading:** long or cash only, since Alpaca does not short crypto, and 0.35% cost per trade side (Alpaca's 0.25% fee plus spread).
- **Choice and judgement:** the rule with the best Sharpe up to 30 June 2024 was chosen, then judged once on July 2024 to now.
- **Bar to pass:** Sharpe above 0.5, a positive return and a smaller worst drop than holding.

**Result: it did not pass.**

| July 2024 → now (unseen) | Chosen rule | Holding the coins |
|---|---|---|
| Total | +13.5% | -81.6% |
| Return a year | +5.5% | -51% |
| Worst drop | -43.5% | -92.7% |
| Sharpe | 0.33 | -0.30 |

- **Protection, not profit:** it mostly protected money by stepping out of the 2025 memecoin crash; it did not make much. 11 of the 15 rules lost money over the same dates.
- **By coin:** DOGE, PEPE and BONK made +40% to +54% under the rule; SHIB, WIF and TRUMP lost 38% to 51%, though far less than holding them.
- **Hindsight:** the coins are famous today, and memecoins that collapsed to nothing are not in the test, which flatters every result.

**So it is paper only.** It never sends an order and is not connected to Alpaca auto trading. If its record since 14 November holds up over months, it can be tested again. It was not retuned after the result: picking a better-looking rule now would only fit the past.

### 2.74: Day Trader retrained on fresh data; fixes from the first real Alpaca run

**Retrained.** The Day Trader's record was rebuilt from 10 years of Alpaca 5-minute bars through 13 November 2026, for all 28 candidate markets, using a paper account's market-data access.
- **Result:** the same six markets were chosen (QQQ, NVDA, TSLA, AMZN, AMD, ARKK), with the same settings, and the learn-from-mistakes filter was kept. It beat the plain rules on 2018–21 and beat 28 of 30 filters trained on scrambled outcomes.
- **Unseen 2022–now:** +7.37% a year, Sharpe 0.99, worst drop -9.0% with instant fills; +4.13% a year (Sharpe 0.61) filled 5 minutes late.
- **Without the filter:** +3.24% a year with a -16.2% worst drop, so the filter is doing real work.
- **Futures book (MES, MNQ):** +8.06% a year, Sharpe 1.09.

**Self-review, generation 2:** current rules kept. Picking each month's best setting beat them on unseen months overall (Sharpe 1.03 against 0.90) but was slightly worse over the last 24 months (-0.613 against -0.603), so it did not pass. Over the last 24 months every setting of the plain rules lost money, which is why the filter stays on. The full table is in `loki_v8/daytrade_evolution.json`.

**Recent check on another feed.** On the latest 46 trading days of Yahoo 5-minute prices (11 Sep – 13 Nov), the stock book made +2.55% (worst drop -1.2%) while buying and holding the same six stocks lost 3.1%. Most of those days are inside the training data, so this checks that the live engine matches the record; it is not new proof of an edge.

**Fixes found by running against a real Alpaca account:**
- **Bar downloads now retry Alpaca's temporary server errors (502, 503, 504)**, not only rate limits. One 504 used to abort a whole training run.
- **A blocked entry on a free data plan now says why.** Alpaca refuses SIP quotes (403) on the free plan, and the IEX quote's own reason (for example a one-sided quote after hours) was being replaced by a generic "no quote".
- **Confirmed against real answers:**
  - Alpaca's nanosecond clock is parsed correctly.
  - The asset flags and the snapshot format match the code.
  - Real past splits (NVDA 10-for-1 on 10 June 2024, AVGO on 15 July 2024) are recognised by the corporate-actions guard.

### 2.73: a cleaner look, second pass

- **Flatter surfaces.** Panels keep one soft outline; the cards inside them are lightly tinted instead of outlined again, and the gradients and shadows on panels are gone.
- **Shorter labels.** 43 of the small capital-letter labels above headings were cut to a few words (for example "DAY TRADER AI · MAIN MODE · IN AND OUT THE SAME DAY · PAPER MONEY" is now "DAY TRADER AI" beside its SIMULATED tag). Any part saying paper, real or read-only was kept where no tag already says it. The labels now use the normal typeface, and page titles no longer carry one.
- **Top bar.** Search, feed status and sign-in lost their frames; a guest sees just "Sign in".
- **Overview.** The slogan line, the "01 Observe · 02 Test · 03 Decide" row and the three shortcut cards that repeated the sidebar were removed, and the hero is shorter.
- **Sidebar.** The active page is a tint, not an outlined box.

### 2.72: a calmer interface

- **Top bar on one row.** The controls no longer wrap onto two or three rows. The page name replaces the "Workspace / …" trail, the search button is an icon with its shortcut, and the exchange-rate text moved out of the bar. On phones the bar scrolls sideways instead of stacking.
- **Grouped sidebar.** Pages are grouped under Trade, Research and Markets with tighter rows, so the whole menu fits a laptop screen without scrolling. The "NEW" badges are gone.
- **AI trading shows one tool at a time.** Day Trader, Portfolio AI, Single market and Weekly self-review are now tabs (`static/js/trading_tools.js`) instead of one page about 6,500 pixels long. A hidden tool keeps running, the choice is remembered, and the account switch (Practise, Alpaca, Funded) is a small control above the tabs.
- **Overview.** One heading instead of two, and each market tile shows the day's change as a short figure (the "vs previous close" wording is in its tooltip).
- **Styles.** The new rules are in `static/css/declutter.css`. Old rules for the AI trading header and tabs, spread over three stylesheets, were removed.
- No text about risk, testing or simulated results was removed or hidden.

### 2.71: execution guards, capped entries and day-end records for auto trading

**New guards on each new position** (`loki/api/alpaca_daytrade.py`, risk policy v3). Closing is never held back by them.
- **Opening lockout.** No new position before 09:45 New York time, while the opening auctions settle. The rules' first decision is at 10:00, so this changes nothing today; it is in the policy so a future change cannot trade the auction.
- **Halt check.** The quote must have a bid and an ask above 0, the ask above the bid, and be at most 2 minutes old. Alpaca must list the stock as active and tradable. A volatility halt shows up as a one-sided, crossed or frozen quote, so no order is queued into a halted stock.
- **Corporate actions.** No new position on the ex-date of a split, reverse split, stock dividend, spin-off or special dividend (Alpaca's corporate-actions data), when yesterday's close and today's prices are not comparable. If that data cannot be read, nothing opens. Regular dividends and earnings days are not blocked: the 10-year test traded through them, and blocking them would change the tested rules without testing the change.
- **Request budget.** Every Alpaca answer's `X-RateLimit-Remaining` is read. Below 15% left, the check skips the drift reading and opens nothing, keeping the rest for closing orders.
- **Alpaca's clock.** The 5-minute signal limit and the 2-minute quote limit are measured against Alpaca's clock, not the server's. Alpaca's nanosecond timestamps are parsed correctly. The server's offset from Alpaca is shown in the panel.

**Entries are capped limit orders.** A new position is a limit order at most 0.05% past the ask (buy) or bid (sell), rounded to the cent inside the cap, immediate-or-cancel. It fills at once like a market order, but a sudden gap in liquidity cannot fill it far from the quote the guards just checked. An unfilled order is cancelled by Alpaca and retried on later checks, at most 3 times per signal, each try with its own numbered order id. Two checks racing still send the same id, so Alpaca keeps one. Buying power is checked at the cap price. Closing stays a market order.
- **Not yet tested:** the 10-year test assumed market fills. Capped entries may miss some fast moves. The receipts record each try, so a few weeks of paper trading will show whether the cap costs more than it saves.

**Fewer calls.** One snapshot call per feed covers every market's quote, so all quotes come from the same moment. Asset flags and corporate actions are read at most every half hour.

**Already in place:** a reversal waits until Alpaca shows the old position closed, and a market with an order still working is left alone. Opposite orders therefore never cross, which avoids wash-trade refusals.

**In the panel:**
- **Desktop alerts** (if you allow them when you press Start), only for what needs you:
  - auto trading switched off (rejected keys, a blocked account);
  - the drift alarm going amber or red;
  - the daily loss stop;
  - a refused close;
  - positions still open after the bell.
- **Day summary.** After the close, one signed entry goes into the trade log: signals, orders opened and closed, held back by reason, refusals, the account's day change, the drift state and the log's head hash.
- **Reload fix.** Reloading the trading tab used to make it stand aside for up to 90 seconds, because it took its own lease for another tab's. Each page load now has its own id and hands the lease back when it closes; a second open tab still only watches.
- **Download diagnostic bundle.** One JSON file with the risk policy, the drift state, the last check (request budget and clock offset included) and the last 50 signed receipts with the public key to verify them. Your Alpaca keys are never in it.

**Release checks:**
- **Exact pins.** Runtime packages in `requirements.txt` are pinned to the versions the suite passed on, and a test keeps them exact.
- **Secret scan.** All 143 commits were checked for key and token patterns and for the paper keys used in testing. Only test fixtures matched; no real key or webhook is in the history or the page. ("flathead" is the public demo access code, not a secret.)
- **Configuration.** The site has no Alpaca keys or URLs in its settings to fail on. Keys arrive with each request, and the Alpaca hosts are fixed in code.
- **Duplicate checks.** Two tabs or two server instances checking at once are covered by the shared order ids, not a lock.

**Not built:**
- **A live vote of momentum and reversion "specialist" agents.** Its voters have never been tested, and adding them to live entries would trade untested rules. The tested rules plus these guards are what trades. A specialist would first need its own 10-year test, through the same walk-forward and shadow gates as the weekly self-review.
- **An after-hours language-model swarm that writes patches.** Declined in 2.69 for the same reasons.
- **External chat-app alerts and a scheduled 8:30 am asset sync.** The trader runs from your open tab, and the site has no background worker or webhook account to send from. Desktop alerts and the half-hour cache cover both.

### 2.70: tidy-up and type checks across the whole codebase

- **Removed unused code:** an unused response model in the Loki Dive API, an unused slippage helper in the browser risk engine, an unused `reset()` in the browser guard, and an outdated list of modules in `loki/api/__init__.py`.
- **Fixed every type error outside `loki/`.** The training scripts, `loki_v8/`, `core/` and `tools/` now pass `mypy`, as `loki/` already did. The fixes tightened a few loose spots: the Alpaca forward test now checks that Alpaca answered with an object before reading it, and the Day Trader's fill-delay study no longer passes a possibly missing price frame along.
- **CI now type-checks all of it**, so new type errors in these files fail the build.
- No change to trading rules, records or results.

### 2.69: the Day Trader reviews itself every week; a code-health sweep

**Weekly self-review** (`evolve_daytrade.py`, `.github/workflows/daytrade-evolution.yml`, Sundays). A new setting reaches the live rules only through three gates:

1. **Walk-forward search.** Nine bounded settings are tried: the noise lookback (10, 14 or 20 sessions) times the band width (0.8, 1.0 or 1.2 times the noise). The published rules are 14 and 1.0. From 2017 on, each month is traded with whichever setting had the best Sharpe over the previous 6 months, then judged on that unseen month. The process passes only if it beats the current rules' Sharpe on those months by at least 0.10, and is no worse over the last 24 months.
2. **Shadow twin.** A passing pick is replayed on live 5-minute prices beside the current rules, with no money. After at least 7 sessions it must have a positive average day, at least as good as the current rules'.
3. **A person.** Only then is it proposed as a pull request, with the record rebuilt using the new setting. Merging it changes the live rules; closing it keeps the current ones.

Settings are compared on the plain rules on both sides, so the mistake filter cannot favour either. The settings are bounded (`daytrade.PARAM_GRID`), and a setting outside the grid is never traded.

**Generation 1 (run on 10 years of bars):** the current rules were kept.
- Picking each month's best setting beat the fixed rules on unseen months overall: Sharpe 1.03 against 0.90.
- It was slightly worse over the last 24 months (−0.63 against −0.59), so it did not pass.
- The run also shows that without the mistake filter, every setting lost money over the last two years. That is why the filter is on.
- The result is logged in `loki_v8/daytrade_evolution.json` and shown in the Day Trader's "How it was tested".

The weekly search downloads its history from Alpaca and needs the repository secrets `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. Without them only the shadow test runs. Cached history is topped up with recent weeks before each run.

**Code-health sweep** (`tools/code_health.py`). It reads the source with Python's `ast` module. These rules fail CI and the tests:
- a bare `except:`;
- order sizing done without `Decimal`, or with `float()`, `round()` or `math.floor`;
- two functions with the same body.

Functions over 150 lines are listed but don't fail. There are 7 today, including the backtest loop that the live AI must match bar for bar, so they are left as they are. The sweep's report appears on every CI run, and CI now checks unused imports across the whole codebase (it used to check only `loki/`).

**Not built: an AI that patches the code by itself.** Code changes already go through the full test suite, type checks and a reviewed pull request. That is the gate you described, with a person in it. Letting an unattended language model push patches would need an API key and would hand write access to something the tests cannot fully judge. The sweep finds the problems; fixing them stays with a person or a supervised coding session.

### 2.68: a risk firewall for auto trading

Every order the Alpaca auto day trader sends now passes these server-side checks first (`loki/api/alpaca_daytrade.py`):

- **One written risk policy.** Every limit sits in one versioned `POLICY`, and its SHA-256 hash goes into each order's receipt. The page shows the policy and its hash.
- **No duplicate orders.** Each order carries a fixed `client_order_id` built from the market, the signal time and the side. If a request is retried after a dropped connection, Alpaca refuses the copy, and the check reports "already sent".
- **Malformed answers stop everything.** Alpaca's account, clock, position and quote answers are validated with Pydantic before use. A `NaN`, a missing field or a maintenance page stops the check before any order.
- **Exact sizes.** Position sizes are worked out in `Decimal`, with the budget rounded to the cent first. A float version made one-sixth of $120,000 come out at $19,999.99, one share short. A zero, negative or infinite price never sizes an order.
- **Stale signals expire.** A new position opens only within 5 minutes of the rules' signal, the delay the test measured. A missed signal waits for the next decision. Closing never expires.
- **Spread guard.** No new position while Alpaca's latest quote spread is wider than 0.10% of the price, or when there is no clean quote.
- **Receipts.** Each order gets a receipt: a SHA-256 hash of the market snapshot (bid, ask, spread, equity), the policy hash and the list of checks it passed. The page adds it to this device's signed, hash-chained trade log (`engine/audit.js`) with the broker's order id.
- **Trace ids.** Each check gets a trace id that appears in the server's JSON log lines and in every receipt from that check.
- **A broker refusal is not bad keys.** Alpaca answers HTTP 403 when it refuses an order for buying power, an intraday margin deficit or an old day-trading block. The relay used to treat every 403 as rejected keys, which would have switched auto trading off. Now only a 401 means bad keys. A refusal is logged once and tried again on the next check.
- **The US$25,000 minimum is gone.** FINRA's pattern-day-trader rule ended on 4 June 2026, after the SEC approved the change to Rule 4210 in April. Brokers phase in the new intraday margin checks until October 2027. The minimum is now the US$2,000 a margin account (and so a short) needs. New positions never use more than 95% of the buying power Alpaca reports.
- **Closing time.** Every Day Trader position is closed from 10 minutes before Alpaca's own closing time, which also covers early-close days, so a hiccup at the bell cannot leave a position overnight.
- **Quote feed.** The spread guard reads the consolidated SIP feed when the Alpaca plan includes it, otherwise the free IEX feed, and never a quote older than 2 minutes. The feed used is written into every receipt, so a missed entry can be traced to a thin IEX quote.
- **Blocked orders get receipts.** An entry held back (spread, no clean quote, no buying power) gets a receipt saying which check stopped it. It goes into the signed log as a `guard` entry, which is not counted as a trade.
- **Drift alarm** (`loki_v8/drift.py`). From the account's own fills over the last 45 days, it compares the last 30 finished round trips with the tested record of the same markets:
  - **win rate:** a z-score against the tested rate;
  - **payoff:** the average win ÷ the average loss;
  - **slippage:** each fill against the price the rules decided on. The order id carries that price, so Alpaca's own records hold everything needed.

  After 20 round trips: amber (new positions x0.5) when z falls below −1.5, payoff below 60% of the test's, or slippage above twice the assumed cost; red (x0.1) when z falls below −2.5 or payoff below 35%. It only ever shrinks new positions, and its state is in every receipt. The tested record now holds per-market baselines (`market_baselines`: win rate and payoff on the unseen years, as the live book trades them); the headline figures are unchanged.
- **Already the case, now stated:** orders are whole shares with no fractional shares or brackets, so fractional-order rejections cannot happen. (Since 2.71, entries are capped limit orders priced in whole cents; closes stay market orders.) Every check re-reads positions and open orders from Alpaca before deciding, so a restart or a closed tab never orphans a position.
- 15 new tests cover these guards: duplicate retries, malformed answers, stale signals, wide, crossed and stale quotes, SIP-to-IEX fallback, buying power, closing time, 403 refusals, blocked receipts, Decimal sizing, receipt hashes, and the drift maths worked by hand.

### 2.67: auto day trading on your Alpaca paper account

- The Day Trader panel has a **Start auto day trading** button. With your Alpaca **paper** keys saved on the Real account page, it trades that account by itself with the Day Trader's tested rules. While the tab is open it checks every 30 seconds and right after each half hour, and it closes everything before the bell.
- The server does the work (`loki/api/alpaca_daytrade.py`, `POST /api/broker/alpaca/daytrade`). Each check reads the plan and the account, then sends the orders that make the account hold what the rules hold. It only acts when the rules change direction, so one check never fights another.
- **Guards, checked on every call:**
  - Paper host only.
  - It trades only the Day Trader's six markets. It refuses to start if the account already holds any of them.
  - At least US$25,000 of equity, because US pattern-day-trader rules block repeated same-day trades below that.
  - A 2% daily loss stop. In the 10-year test the book fell that far on one day out of about 2,700.
  - At most US$50,000 and 5,000 shares per market, in whole shares.
  - Shorts only in stocks Alpaca lists as easy to borrow.
- **Stop and close positions** closes the Day Trader's positions and nothing else. If the tab was closed mid-day, the first check next time flattens the leftovers.
- Real money is not wired up: anything that trades by itself stays on paper (section 3).

### 2.66: a tidy-up — dead code out, scripts untangled, labels fixed

**Removed** (nothing in the app used any of it)
- `static/js/strategy-lab.js` (352 lines) and its tests: an old in-browser strategy engine that no page loaded.
- Chart.js (205 KB): no page used it.
- `run_strategy_lab.py`: an undocumented command-line wrapper around the strategy lab.
- `loki_v8/broker.py` and the engine's `ExecutionAgent`: a dormant paper broker and order-execution path. The live AI only ever returned decisions, and the browser keeps the paper ledger.
- Three functions that only their own tests called (`portfolio_backtest`, `analyze_trend`, and `evaluate_factor_panel` with its vendored factor-analysis maths).
- Unused settings, including placeholder broker-credential fields in the engine's config.
- 123 style rules left behind by the pages removed in 2.56.

**Untangled**
- `workspace.js` used to share about twenty helper names with nine other scripts invisibly, as browser globals. It now hands them out through one object, `window.LokiWorkspace`, and each script names what it takes on its first line.
- Four more scripts that declared global names are now wrapped. Every script on the site is self-contained.
- A linter run over all 79 scripts reports no undefined names and no unused variables.

**Fixed**
- **Loki Dive never saved its on-device snapshot.** A constant was reassigned; the error was silently swallowed, so the save always failed. It now saves.
- An assignment to a variable that was never declared.
- A stray byte-order mark at the top of `watchtower.js`.
- **Three Day Trader figures were a tenth of a point out.** The record kept two decimals and the pages showed one, and a number rounded twice can land on the wrong side. The record now keeps three decimals. One minute late is +5.9% a year for stocks (was shown as +6.0%) and +6.2% for futures (was +6.3%); the futures headline is +7.9% (was shown as +7.8%). The chat and the page now round the same way.
- `docs/ACCOUNT_STORAGE.md` pointed at a schema file that was deleted long ago.

**Labelled**
- Every Python module, script and stylesheet starts with a description of what it is for, and `server.py` has section headings.
- Package descriptions that still described long-deleted code (`loki`, `core`, `loki_v8`) are rewritten. The chat room is no longer called "sentient" in the code.
- `pyproject.toml`: placeholder links are replaced with the real site and repository, and six stale type-check exemptions are gone.
- This README is reorganised: numbered sections, a contents list, a map of the app, a project layout, and release notes in order.
- `tests/test_code_structure.py` keeps these rules from slipping.

**Checked**
- 1,102 Python tests and 35 script tests pass, including new ones for the Loki Dive save and the house rules.
- flake8, mypy (Linux and Windows targets) and the script linter are clean.
- Every page was scanned on desktop and mobile, and 262 buttons were clicked: no errors.

### 2.65: Day Trader v3 — feed-proof signals, a wider test, and what slow fills cost

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
| Stocks and funds (6 markets, mistake filter on) | +7.4%/yr, Sharpe 1.00, worst drop −9.0% | +5.9% | +4.8% | +4.4% | +4.2%, Sharpe 0.61 |
| Futures (MES and MNQ, filter off) | +7.9%/yr, Sharpe 1.06, worst drop −9.2% | +6.2% | +5.1% | +4.0% | +5.1%, Sharpe 0.74 |

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

### 2.64: 3D engine map, glitch fixes and a lighter, sharper look

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

### 2.63: forward record for the Day Trader

- The tested record ends on a fixed date (`data_through`). Every completed session after it is one the rules could not have been fitted to.
- `GET /api/fusion/ai/daytrade` now returns `forward`: those sessions replayed with the frozen rules and the frozen mistake model, as an equal-capital book.
- The page shows the total, up and down days, worst drop and a bar per day, with a plain note that a handful of days proves nothing.
- Limits: the price feed keeps about 60 days of 5-minute bars, so the strip shows at most the last 60 days. It is simulated on delayed prices, not real trades.

### 2.62: 3D engine map, second upgrade

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

### 2.61: Day Trader learns from its mistakes; futures mode

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

### 2.60: Day Trader AI (main mode)

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

### 2.59: risk levels; another round of honest research

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

### 2.58: idle cash earns Treasury-bill interest; year-by-year record

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

### 2.57: is the trading AI actually good? An honest audit

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

### 2.56: leaner workspace, cooler 3D map

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

### 2.55 and earlier

- **2.55: Red Team lab.** See [Red Team lab](#red-team-lab-255) in the feature reference.
- **2.54: Engine v3 and the Command Center.** See [Engine v3 and the Command Center](#engine-v3-and-the-command-center-254).
- **2.52: the on-device engine.** See [On-device engine](#on-device-engine-252).
- Older desktop-era launchers, installers, duplicated guides and unreferenced modules were removed in 2026-10 and 2026-11 (the old standalone bot, the crypto trainer, the Supabase vault). The repository's history retains them.
