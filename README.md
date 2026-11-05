# Duo

Two Claude agents run a small spot crypto portfolio on Kraken. A Strategist proposes trades and a Risk Manager challenges them. They decide together when to talk next, and you watch everything on a local dashboard.

It starts in **paper mode**: live Kraken prices, simulated fills, no real money. As shipped, `config.toml` runs the **systematic runner** below, a research strategy traded by code, and the agents can't trade. Set `[systematic] enabled = false` for the agents. This is an experiment, not a way to make money. It may well lose money, and nothing here is financial advice.

## Start it

1. Get an API key from [console.anthropic.com](https://console.anthropic.com).
2. Run `./start.sh`. The first run installs everything and creates `.env`.
3. Put your key in `.env` as `ANTHROPIC_API_KEY=...`.
4. Run `./start.sh` again and open http://127.0.0.1:8787.

To see the dashboard without a key, run `.venv/bin/python -m tests.demo_server` and open http://127.0.0.1:8799. It uses live prices with a scripted conversation and invented chart history, and it never touches your real data. The demo server is in the private working copy only; the public repository leaves it out.

`start.sh` keeps the Mac awake while it runs, but closing the lid can still put it to sleep. **While the bot is stopped or asleep, no stop-losses run**, which is why it should move to a server before using real money. To run the paper test with the laptop shut down, see "Running the paper test on GitHub Actions" below.

## The systematic runner

`config.toml` now starts the **systematic runner** (`[systematic] enabled = true`): a strategy from the research, decided once a day by code and traded on paper. While it's on, the agents can't trade at all, and everything goes to its own database, `data/paper_systematic.db`. `data/paper.db` isn't touched. Set `enabled = false` to go back to the agents.

**The research's verdict.** The one-time holdout gate has decided: no spot strategy passed it. The one pass is funding-rate carry on perpetual futures, which UK retail clients can't trade, and this runner refuses it. The AI agents added nothing in any role tested. The full verdict is at the top of `RESULTS.md`, one of the research write-ups, which are kept privately and aren't in the public repository. `RUNBOOK.md` covers running the paper bot.

**Which strategy.** `strategy` is a research variant id. As shipped it is `F1__sma_50__BTC__vol20`, trend on Bitcoin alone at a 20% volatility target: the researcher's suggested fallback, **for paper trading only**, and it **did not pass the gate** (it failed G2 on both cost tracks: its holdout Sharpe was below half its development Sharpe). Not trading at all is an equally reasonable choice. It replaced the placeholder `F1__ENS__BTC_ETH__vol40` (the preregistered trend fallback, which failed several gates). This isn't financial advice, and nothing goes live without your explicit decision. Any F1 variant (BTC, BTC+ETH or TOP10, neighbours like `__L=72` included), any F2 variant, and the DCA fallback `dca_BTC_ETH` work. F3 (perpetual futures, which UK retail clients can't trade) and F4 (intraday) are refused at start with the reason, and so is F5, which mixes its members' returns rather than holding target weights. One paper run holds one strategy: to change it, move `data/paper_systematic.db` aside first. A strategy that can put more than `max_asset_pct` in one coin, such as an F1 BTC-only variant (100%) or F2 with inverse-volatility weights, is refused at start, because paper would hold less than the research's strategy: raise `max_asset_pct`, or set `allow_capped_targets = true` to run it capped, and each decision then logs the research's target beside the capped one, which `compare` replays. The code's default is 50%; the shipped `config.toml` sets `max_asset_pct = 100`, because `F1__sma_50__BTC__vol20` holds Bitcoin alone and would be refused at 50. The strategy's own volatility sizing, not that cap, sets its Bitcoin weight each day. For the DCA fallback, a weekly tranche whose day passes with no decision (the Mac asleep, a feed down) is bought late at the next decision, so the run still buys all 52.

**Same targets as the research.** Each day the runner fetches Binance's public daily klines (the research's source), builds the same panel the research uses with the research's own cleaning, and calls the strategy's own `targets()` function. `tests/test_sys_parity.py` shows the runner's targets equal the research's on every one of the last 120 development days, for F1 on BTC+ETH, an F1 TOP10 variant and an F2 variant. Columns get the research's names, X~2 after a break included, even when the break is older than the 600-day window (`duo/research_breaks.json`, checked against the research by the parity test), so each sleeve goes to the Kraken book the research's cost model gives that name. In a TOP-n universe, a coin Kraken can't sell to a UK client (no Kraken book in the research cost model, a Kraken book of that name that is another coin, as with Terra 2.0's LUNA~2, or XMR) keeps its sleeve in cash, and the decision log says so. So do two columns that would trade on one Kraken book.

**When it trades.** The daily bar closes at 00:00 UTC. A loop runs every 5 minutes for health checks, data freshness and orders. The day's decision is made once, at the first loop from 00:02 UTC at which the just-closed bar is in Binance's data. It never uses the bar still forming. An asset trades only if its change passes the research's band, and a target of 0 always trades.

**How it trades, on paper.** Each change starts as a post-only limit at Kraken's best bid (buys) or ask (sells), is moved to the new best price every 15 minutes, and after 120 minutes crosses the spread at no worse than 0.5% from the quote. Paper fills follow the same rules as before, at Kraken Pro Tier 3 fees (0.22% maker, 0.38% taker) on GBP 20,000, converted once to the USD book the research's cost model chose, less 20 bps FX. These are the research's own assumptions.

**Hard limits, in code** (`duo/sys_risk.py`). Each has its own setting in `[systematic]`, is logged pass or fail on every decision, and fails closed: an unknown or broken input blocks trading. Every order goes through one function that checks them all.

| Limit | Default | What happens |
| --- | --- | --- |
| Per asset | The strategy's largest sleeve + 2 points, never above 50% | A buy that would pass it is cut or refused |
| Gross exposure | 100% | No shorts and no leverage, ever |
| Per order | A buy: its target change + 2% of equity. A sell: the quantity its order set out to sell | Bigger orders are refused. A sale can't be blocked by its coin rising while it works |
| Daily loss | 8% below the UTC day's start (the last loop before midnight, or, after downtime, the holdings at Binance's midnight closes; unknown blocks buys) | Resting buys cancelled, no buys until 00:00 UTC. Sells still run |
| Kill switch | 45% below peak equity, marked only at sane, fresh, moving quotes | Everything cancelled and sold at market (capped), then halted until you resume. It fires even while trading is paused from the dashboard |
| Stale data or errors | Daily bar missing 30 minutes after 00:00, a quote over 90 seconds old or frozen for 10 minutes, or 5 API errors in a row | No new orders until it clears, which it does by itself |

**Resuming after the kill switch** (or after Stop trading on the dashboard): press **Resume** in the dashboard's Systematic runner panel, or run `.venv/bin/python -m duo resume`. The drawdown peak restarts from equity at that moment. On GitHub Actions, run the Paper run workflow with action `resume` instead (RUNBOOK 12.3): the dashboard and `duo resume` only reach the Mac's own paper run. Nothing else needs you: every other halt clears on its own.

**What's recorded.** Every decision is a row with everything needed to audit it: the time, the strategy, the research code hash, the signal source and a hash of the bars, the last 300 closes per asset, signals, targets, bands, holdings, the orders it meant to place, and each limit's value, threshold and outcome. Orders, child orders and fills link back to it. Every loop writes a heartbeat.

| Command | What it does |
| --- | --- |
| `.venv/bin/python -m duo status` | Strategy, last heartbeat, last decision, halts. No network |
| `.venv/bin/python -m duo decisions` | Every decision, with its orders and fills, as JSONL in `data/decisions_systematic.jsonl` |
| `.venv/bin/python -m duo compare` | Paper against the research backtest of the same decisions, in `data/compare_systematic.md` |
| `.venv/bin/python -m duo watchdog` | Is the bot alive, and is the Mac on mains power? |
| `.venv/bin/python -m duo resume` | Lifts the kill switch |
| `.venv/bin/python -m duo tick` | One poll, then exits: what each GitHub Actions run does. It never starts a new paper run unless given `--first-run`; `--flatten` sells everything and halts |
| `.venv/bin/python -m duo probe` | Can this machine reach Kraken's and Binance's public prices? Changes nothing |
| `.venv/bin/python -m duo site --out DIR` | Writes the static status page into DIR |

**Paper against backtest.** `compare` re-runs the research engine (`backtest_weights`, or `dca` for the DCA fallback) on the same signal data and targets, with the research cost model at Tier 3 taker. It shows, per order, the backtest's assumed fill (the next day's open plus its cost at that size) against the paper fill and fee; per day, paper's return against the backtest's, the difference, its mean and tracking error; and how many fills were maker. The dashboard shows the same. After at least 4 weeks, this is what says whether paper behaved as the research expected. It can't say whether the strategy has an edge: 4 weeks is far too short for that.

## Keeping it running

A Mac on battery can sleep or run flat, and the bot then stops with no alert (audit finding 77). Three things help, and installing them is up to you:

1. **Plug the Mac in.** `start.sh` warns if it starts on battery, and runs the bot under `caffeinate -i -s`, which stops sleep while on mains power. Closing the lid can still sleep it.
2. **The watchdog**, `.venv/bin/python -m duo watchdog`, reads the heartbeat the bot writes every loop (`data/heartbeat_systematic.json`) and `pmset -g batt`, and posts a macOS notification if the heartbeat is more than 15 minutes old, the runner is halted, it has placed no new orders for 30 minutes (`degraded_alert_minutes`: an exchange outage keeps the heartbeat fresh), today's decision is overdue, or the Mac is on battery.
3. **launchd templates** in `ops/` (in the private working copy; the public repository leaves them out): `com.duo.bot.plist` runs the bot and restarts it whenever it exits, and `com.duo.watchdog.plist` runs the watchdog every 5 minutes. They aren't installed. To install them yourself, replace `__PROJECT__` in each with this folder's full path, copy them to `~/Library/LaunchAgents/`, and load each with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.duo.bot.plist` (and the same for the watchdog). Don't also run `start.sh` while the bot's agent is loaded, or two copies will run. `launchctl bootout gui/$(id -u)/com.duo.bot` stops it.

While the bot is stopped or the Mac is asleep, nothing is checked or traded, and in paper nothing fills.

## Running the paper test on GitHub Actions

The paper test can run on GitHub's machines instead, so the laptop can be shut down. GitHub Pages can only host static files, so it shows the status page but can't run the bot. GitHub Actions runs the bot: on a schedule it starts a fresh machine, restores the paper run's state, runs one poll (`python -m duo tick`), saves the state, and publishes the status page to Pages. It's all in `.github/workflows/paper.yml`, a workflow called **Paper run**, and RUNBOOK section 12 is the step-by-step guide.

- **Where:** the public repository `mattthewhiggins/duo-paper`. The status page is **https://mattthewhiggins.github.io/duo-paper/**, and like the repository it's public: anyone can see the paper trades.
- **When, in UTC:** every 15 minutes from 00:05 to 03:50, while the day's decision is made and its orders are worked; hourly at :35 for the rest of the day; and at 23:47, so the next day's loss limit starts from a reading just before midnight. GitHub starts scheduled runs late when it's busy, often by 5 to 60 minutes, and sometimes skips one.
- **The state** is `data/` (the database and the heartbeat file), kept on its own branch, `paper-state`. Each run restores it and saves it back. The branch keeps one commit per UTC day, so it's a daily snapshot, and `main` only ever holds code.
- **Using it:** in the repository's Actions tab, open Paper run, press Run workflow and pick an action. `start` begins the paper run: run it once. `tick` runs an extra poll now. `resume` lifts the kill switch, then polls. `flatten` sells everything and halts. `probe` checks that Kraken and Binance answer from GitHub's machines, and changes nothing. Until `start` has run, scheduled runs do nothing and say so, without failing. If `start` fails, or its state can't be saved, no paper run has begun: the run says so, and you run `start` again. Runs never overlap: a run that starts while another is going waits its turn, in order.
- **Checking on it:** the status page shows the last run and what it found: "Running normally", "Needs attention" (with the reason, such as a tick that didn't finish or today's decision overdue) or "Halted". When no run has updated it for about 3¼ hours (195 minutes), it says "Stale", or "Finished" once the 28-day test is complete. The Actions tab lists every run, and each run's summary says what its tick did.
- **Failure emails:** GitHub emails you when a run fails, if your notification settings let it (RUNBOOK 12.2). A run fails when its tick fails or is stopped after 270 seconds, or the state can't be restored or saved. It fails when the Install step, the page's build or upload, or the `deploy` job that publishes the page fails. And it fails when the watchdog finds the runner halted, degraded for too long, its decision overdue, or that the run wrote no heartbeat. While the cause lasts, every run fails. **Missed or stopped runs send no email:** a run that never starts can't fail, so if GitHub stops starting the scheduled runs, only the page's "Stale" notice and the Actions list show it. Look at one of them every day.
- **Stopping it:** the runs don't stop by themselves. When the page says the 28-day test is complete (28 finished days compared), disable the workflow (Actions tab, Paper run, the `...` menu, Disable workflow), or run `gh workflow disable "Paper run" -R mattthewhiggins/duo-paper`. That's the end of the test, and the same stops it at any other time. Nothing more runs or trades, and the state stays on `paper-state` if you enable it again later.

How it differs from the laptop's loop:

- **The decision** is made at the first run after 00:02 UTC that sees the just-closed bar: normally the 00:05 run, later when GitHub starts it late. The overdue alert waits until 02:00 UTC.
- **Resting orders** are judged on Kraken's 1-minute candles between runs, by the same paper rule, since nothing watches the book then. The laptop checks a fresh quote every 10 seconds. Only a gap of more than 3 hours since the last run counts as an outage: then each resting order is still judged on the candles over its life, and closes unfilled if nothing traded through it.
- **Risk checks** (the kill switch, the daily loss limit, stale data) run once per run, every 15 to 60 minutes, rather than every 5 minutes, and "5 API errors in a row" comes after 3 to 5 failed runs: one failed run can count twice (Kraken on the morning of a decision, Binance before the day's bar is seen).
- **Daily bars** come from Binance's market-data host, data-api.binance.vision, which serves the same klines as api.binance.com. api.binance.com refuses US addresses (HTTP 451), and GitHub's machines are mostly in the US, so it's only the fallback. Each decision records which host it used.

**Costs:** none. Actions on GitHub's standard machines and Pages are free for public repositories.

**GitHub's terms** allow Actions for developing and testing the software project in the repository. This paper test is a time-boxed test of this project, stopped at its end by disabling the workflow ("Stopping it" above), so it fits. A live trading bot isn't a test, and would need its own server (see "Moving to a server").

**Nothing here places real orders or needs keys.** The workflow has no secrets and reads only public Kraken and Binance prices.

**What's published.** `.venv/bin/python ops/publish_repo.py TARGET_DIR` copies only the files the run and its tests need (the code, the tests, `config.toml`, the research cost model, this README and the RUNBOOK) into a clone of the public repository. It deletes anything there that's no longer on its list. It stops without changing anything if a file is called `.env` or isn't UTF-8 text, if a file on its list is a link, or if a file holds a path under /Users (also written with dashes for slashes, as in a Claude session folder's name), an email address other than a GitHub no-reply or example one, or something shaped like a key (an Anthropic, GitHub or AWS key, a private key, or one of this project's key settings with a value). It checks for nothing else, so look over the copy before you push it. It doesn't commit or push. `RESULTS.md`, `AUDIT.md` and the research write-ups and results are kept privately.

## Going live with the systematic runner

The runner is paper only, and refuses to start with `mode = "live"`. Before it could trade real money, at least these would need to exist, and each needs your explicit approval (preregistration section 14):

1. **Protection that doesn't depend on the bot running** (audit finding 34): the kill switch and daily limit run only inside the process, so a crash, sleep or outage leaves positions unwatched. Live needs exchange-side protection, or a server that can't sleep.
2. **A restart path that holds up** (finding 48): live reconciliation that copes with an account holding less spare cash than the bot has spent, without leaving positions unprotected.
3. **The live order findings** listed as "left for live" in `duo/AUDIT_STATUS.md` (kept privately, with the audit): unknown order states (35, 36, 38, 51), failed bookings (60), key problems (70), reconcile races (53), and a spread limit on the taker leg (85).
4. **At least 4 weeks of paper** with `compare` showing paper close to the backtest, and the holdout gate's verdict on the strategy: **fail**. `F1__sma_50__BTC__vol20` failed G2 on both tracks (the research's holdout results, kept privately), so this condition isn't met, and no paper result can change that.

## The desk discussion

Every 5 minutes the two agents talk on Claude Haiku 5.5, a much cheaper model. They review what has changed, develop strategy ideas and test them on years of real prices with the `test_rule` tool. What they learn goes into the Research notes, which every later conversation can read.

**Nothing said on the desk can change the portfolio.** Those agents can't place orders, make plans or edit the strategy. If the Risk Manager thinks something should change right now, it can ask for a decision session on Claude Opus 5.5, which reviews the case and decides. Those requests are limited to 4 Opus sessions a day and at least 30 minutes apart, and agreed changes go through the usual hard limits.

Measured cost so far: about 0.3 cents a discussion, or about $23 a month, plus about 11 cents per Opus session. Set `discussion_minutes = 0` in `config.toml` to turn it off. The desk stops whenever trading is stopped or an API budget runs out.

More talk costs more and isn't evidence of more profit. On $1,000, about $35 a month in API costs means making about 3.5% a month just to break even.

## How a session works

1. **Something wakes the agents:** the check-in time the Strategist chose, a 3% price move, a stop-loss or profit target firing, or the Wake button.
2. **The Strategist reads a briefing** (portfolio, limits, market numbers, waiting orders, its strategy and journal), can pull candles from Kraken, and proposes orders, plans, stop changes or a new strategy.
3. **The Risk Manager reviews it** and either approves, asks for changes (up to 3 rounds), or rejects. If nothing is proposed and nothing is open, it isn't consulted, to save money.
4. **Code checks every approved order** against the hard limits at the latest prices, then places it. A plan is checked again when its price arrives.

If a call fails, gets declined, or hits a budget limit, the session ends and nothing trades.

## What the agents can't do

These limits are in code and in `config.toml`. The agents can see them but can't change them.

| Limit | Default |
| --- | --- |
| Largest single buy | 10% of equity |
| Largest holding in one asset | 35% of equity |
| Cash kept after any buy | 20% of equity |
| Buys per day (sells aren't capped) | 4 |
| Stop-loss on every buy | Required, 1.5% to 25% below the price |
| Profit target on every buy | Required, at least 2 round trips of fees above entry: 1.6% for a resting buy, 2.4% for an immediate one |
| Plans | Up to 5 waiting, for up to 72 hours each, with a trigger within 25% of the price |
| Trading fees | No new buys once 30-day fees would pass 3% of equity |
| Daily loss | At 5%, buys pause until 00:00 UTC |
| Drawdown from peak | At 20%, agent trading stops until you resume it |
| Instruments | Spot only: no leverage or shorting. BTC, ETH and SOL |
| Stale or bad prices | No trading on prices over 90 seconds old |
| API spend | $1 a session, $2 a day, $50 a month |

Stops, targets, plans and resting orders are checked in code every 10 seconds, whether or not the agents are awake or the budget has run out.

**Stop trading** blocks the agents and new trades, cancels waiting buys and pauses plans, but leaves stops and targets armed. **Sell everything** cancels all waiting orders and plans, sells what the bot holds and stops agent trading. To stop all activity, quit the program. Any orders left in Kraken's book then expire by themselves within 10 minutes.

## Order types

**Resting or immediate.** The agents choose for each order.

| | Resting | Immediate |
| --- | --- | --- |
| How it works | Waits in the order book at the best price for up to 10 minutes | Fills now, or not at all |
| Fee at Kraken's entry tier | 0.40% | 0.80% |
| The catch | May not fill | Costs twice as much |

Stops and Sell everything are always immediate, because getting out matters more than the fee. Profit targets rest, and try again while the price holds. If a stop fires while an order is waiting on that asset, the order is cancelled first.

A resting order can't be filled at the higher fee, because it's post-only, and on Kraken it expires by itself even if the bot dies. Anything that filled while the bot was down gets booked when it restarts.

**Plans.** A plan is a buy the agents approve in advance, like "buy SOL if it falls to this price". Code watches and acts when the price arrives, with no paid session and no further review, but it checks every limit again at that moment. If a limit blocks it, the plan stays waiting until it clears or the plan expires. A resting order that misses puts its plan back to waiting. If an asset's stop-loss fires, its plans are cancelled and the agents are woken to reconsider.

**How often resting orders fill.** On two years of 5-minute prices, the market traded past a resting buy placed at the current price within 10 minutes 82% of the time for BTC, 87% for ETH and 90% for SOL. Treat those as upper limits, because they ignore your place in the queue. Four hours later, orders that filled were flat on average (about -0.02%), while the ones that missed were up 0.13% to 0.24%. So you tend to miss the ones that rise, though that gap is smaller than the 0.40% saved.

Paper mode is stricter than that history: a resting order only fills if the market is clearly past its price at one of the 10-second checks. It still can't model your place in the queue, so live fills may differ. The dashboard shows the real fill rate and the fees saved as they build up.

## The numbers

- **Net P&L** is equity minus starting capital minus API costs, with trading fees already included.
- **Holding BTC** is what the same starting capital would be worth if it had bought BTC at the first price the bot saw. A rising market can make the bot look clever, so judge it against this line.
- **Trades** download as CSV from the dashboard. Trades may be taxable, so keep the file.

## Costs to keep in mind

- **Trading fees:** at Kraken Pro's entry tier, a round trip costs about 0.8% with resting orders and 1.6% with immediate ones, before slippage. Check [Kraken's fee schedule](https://www.kraken.com/features/fee-schedule) and set `maker_pct` and `taker_pct` to your rates.
- **API costs:** decisions use Claude Opus 5.5 at medium effort, and discussions use Claude Haiku 5.5. Spending the full $50 a month on a $1,000 portfolio needs a 5% monthly gain just to break even, so size the budget to the portfolio. To cut costs, make `discussion_minutes` longer or set `model` to `claude-sonnet-5-5`, though the agents' judgment may change.

## How often should it trade?

Run the test yourself with `.venv/bin/python -m duo backtest`. It takes a few minutes and uses public price data.

The October 2026 run covered 2,466 rule runs on two years of 5-minute prices for BTC, ETH and SOL, spanning a rising year and a falling one. These are the rule runs that made money after fees at Kraken's entry tier:

| How often the rule trades | Profitable | Median per trade |
| --- | --- | --- |
| Over 3 times a day | 0 of 638 | -1.06% |
| 1 to 3 times a day | 0 of 592 | -1.04% |
| 1 a week to 1 a day | 0 of 784 | -1.12% |
| Under once a week | 68 of 452 | -0.87% |

- **It isn't only fees.** With fees set to zero, the rules trading at least daily still made nothing: 0 of 1,230 were profitable, at a median of -0.08% a trade. Price moves far enough to cover fees several times a day, but these rules couldn't predict the direction.
- **Lower tiers don't rescue it.** At Tier 6, which needs $100K a month in volume, 0 of those 1,230 were profitable.
- **Winning days aren't profit.** At Tier 3, 23 rule runs won on at least 60% of their active days, and none of them made money overall. Small frequent wins were outweighed by occasional larger losses.
- **Picking winners from the past failed.** Rules chosen on the previous year and scored on the next quarter lost money at every fee level, including no fees.

The limits: these are simple long-only price rules, tested on 5-minute candles from Coinbase. They use no order book data, no shorting and no Claude judgment, so this doesn't prove every fast strategy fails.

## How would it have done each year?

Run this with `.venv/bin/python -m duo backtest-years`. It takes about 30 minutes the first time.

This run tested 424 rules per asset that fit the bot's limits, on nearly 8 years of 5-minute prices for BTC, ETH and SOL (SOL only from its 2021 listing). Each quarter it used only the rule that did best over the previous year, or stayed in cash if none made money. Results are at Kraken's entry fees, before API costs, without compounding, and with up to 26.7% of capital per asset:

| Year | Picked rules | Same positions held | BTC held |
| --- | --- | --- | --- |
| 2020 | +18.7% | +207.6% | +305% |
| 2021 | +30.1% | +210.8% | +59% |
| 2022 | -46.1% | -60.2% | -64% |
| 2023 | +36.7% | +311.1% | +156% |
| 2024 | -8.2% | +67.3% | +121% |
| 2025 | -30.5% | -13.7% | -6% |
| 2026 to date | -12.1% | -15.4% | -11% |
| **Average a year** | **-2.3%** | **+102.2%** | **+81%** |

The averages also count a 19-day stretch at the end of 2019, shown in the command's full output. The rules lost money in 5 of the 8 calendar periods and paid about 24% of capital a year in fees. With no fees at all the same process averaged +14.6% a year, and at the $10K-a-month fee tier +6.9%. Their worst fall was 94% of starting capital, measured without the bot's 20% drawdown stop.

This doesn't test the agents. Claude already knows how past markets turned out, so replaying history with it wouldn't be a fair test. Paper trading from now on is the only honest measure of whether they add anything.

## Going live

The bot can trade real money only after you take all of these steps yourself. Run paper mode first, for long enough to see it through a real market move.

1. In `config.toml`, set `starting_capital` to an amount you can afford to lose, and `quote` to the cash currency on your Kraken account. The bot only trades that amount and tracks only its own coins, so anything else in the account is left alone.
2. Create a Kraken API key with only these permissions: **Funds: Query**, plus **Orders and trades: Query open orders & trades, Query closed orders & trades, Create & modify orders**. Don't enable any Withdraw permission.
3. Add `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` and `DUO_LIVE_CONFIRM=trade real money` to `.env`.
4. Run `.venv/bin/python -m duo preflight`. It places no trades. It confirms the key can't withdraw, the balance covers `starting_capital`, and Kraken would accept an order.
5. Set `mode = "live"` and restart. The preflight runs again on every start, and the bot refuses to start if any check fails.

Paper and live keep separate histories (`data/paper.db` and `data/live.db`).

The withdrawal check uses Kraken's `WithdrawInfo` endpoint, which Kraken has marked as deprecated. If Kraken retires it, the check fails and live mode won't start.

If Kraken doesn't answer when an order is sent, the bot stops agent trading rather than risk doubling up, and asks you to check Kraken by hand. If the account holds less than the bot's books say, it stops too.

## Moving to a server

Copy the folder to an always-on Linux machine, create the virtualenv, and run `.venv/bin/python -m duo run` under a service manager such as systemd (`start.sh` is Mac-only because of `caffeinate`).

Leave the dashboard on `127.0.0.1` and reach it through an SSH tunnel: `ssh -L 8787:127.0.0.1:8787 your-server`. If you do expose it, set `DUO_DASHBOARD_TOKEN` and put it behind HTTPS. Restrict the Kraken key to the server's IP address.

## Tests

```bash
.venv/bin/python -W ignore::ResourceWarning -m unittest discover -s tests -t .
```

The tests never spend money or reach private Kraken endpoints, and never touch the network at all for the systematic runner: Binance and Kraken are stand-ins. `tests/test_sys_parity.py` takes about 1½ minutes, because it rebuilds the runner's panel for each of 120 development days; where the research's development panel isn't there, as in the public repository, its 2 tests are skipped. They cover the ledger, every limit, the conversation loop, failure handling, the dashboard and the status page. The status page's tests that run its script in Node.js are skipped where Node.js isn't installed. The Anthropic SDK and the Kraken order code run against local stand-ins, which can't prove the real services accept the requests.

**The tests don't depend on the shipped config.** They set the limits they test themselves (`TEST_LIMITS` in `tests/sys_helpers.py`), so changing the strategy or `max_asset_pct` in `config.toml` doesn't change what they check. Run on 5 November 2026 with the shipped config, the suite ran 418 tests: OK.

## Files

| Path | What it does |
| --- | --- |
| `config.toml` | All the settings |
| `duo/agents.py` | The two agents, their prompts and the conversation |
| `duo/engine.py` | Main loop, stop-losses, waking the agents, the briefing |
| `duo/risk.py` | The hard limits |
| `duo/ledger.py` | Balances and P&L, rebuilt from the bot's fills |
| `duo/brokers.py` | Paper fills, Kraken orders and the live preflight |
| `duo/backtest.py` | Tests trading rules on real price history, after fees |
| `duo/research.py` | Lets the agents test a rule during a conversation |
| `duo/market.py`, `duo/kraken.py` | Kraken prices and API client |
| `duo/web.py`, `duo/static/index.html` | The dashboard |
| `duo/systematic.py` | The systematic runner: timing, decisions, orders, the decision log, heartbeat |
| `duo/sys_risk.py` | The runner's hard limits, and the one function every order goes through |
| `duo/sys_research.py` | Strategy ids, targets and DCA from the research's own code, read-only |
| `duo/sys_data.py`, `duo/sys_market.py` | Binance public daily klines into the research's panel, and Kraken public quotes |
| `duo/compare.py` | Paper against the research backtest |
| `duo/watchdog.py`, `ops/` | The watchdog, and launchd templates (not installed, and in the private working copy only) |
| `.github/workflows/paper.yml`, `ops/actions/` | The paper test on GitHub Actions, and the scripts that restore and save its state branch |
| `duo/site.py`, `duo/static/site.html` | The status page GitHub Pages shows (`python -m duo site`) |
| `requirements-paper.txt` | What the GitHub Actions run installs: numpy, pinned by version and by the hash of its file |
| `ops/publish_repo.py` | Copies the published files into the public repository, after checking them |
| `duo/AUDIT_STATUS.md` | Each audit finding: fixed, not applicable, or left for live (kept privately) |

The public repository holds only the files its paper run and tests need. `RESULTS.md`, `AUDIT.md`, `duo/AUDIT_STATUS.md`, the research write-ups (`research/*.md`) and the research's results under `research/artifacts/`, apart from the cost model, are kept privately; audit findings are numbered as in `AUDIT.md`.
