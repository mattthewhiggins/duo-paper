# Paper-trading runbook

**The holdout verdict: no spot strategy passed the gate.** The holdout was unlocked once, at 2026-11-05T03:42:38Z, and the gate was applied as preregistered and not loosened (preregistration section 12: "If nothing passes, the answer is that nothing passes"). The one strategy that passed, on both cost tracks, is `F3__BTC_ETH__D30__E5`, a funding-rate carry trade on Bitcoin and Ether. It's ruled out: it needs perpetual futures, which firms may not sell to UK retail clients (FCA COBS 22.6, research/VENUES.md), the backtest doesn't model liquidation, and its holdout return was small, 8.2% in USD and 4.8% in the GBP view over the 837 days from 2024-07-17 (track K), so this runner refuses it. Every spot strategy failed: the trend strategy chosen on development, `F1__sma_50__BTC__vol20`, failed one gate condition (G2) on both tracks, and both preregistered fallbacks failed several, FALLBACK_TREND (`F1__ENS__BTC_ETH__vol40`, -1.3% on the holdout) and FALLBACK_DCA (-13.4%). The AI agents added nothing in any role tested: of 7 layer-model pairs, one passed on development (`LLM__L0__haiku__F1__sma_50__BTC__vol20`), and on the holdout its Sharpe was -0.17 against its base's 0.39, so it didn't stay. What this runbook paper trades is therefore a fallback that **did not pass the gate**, `F1__sma_50__BTC__vol20` (section 1 says why), for paper trading only; not trading at all is an equally reasonable choice, and nothing tested makes a profit every day. Figures are after costs, in USD unless the GBP view is named, from `research/artifacts/holdout_results.md` and `holdout_results.json` (the agents' from `research/artifacts/llm_dev.json` and the LLM section of `holdout_results.md`), research files that are kept privately and aren't in the public repository. This isn't financial advice, and nothing goes live without your explicit decision.

This is the day-to-day guide to running Duo's **systematic runner** on paper: a strategy from the research, decided once a day by code, traded against live Kraken prices with simulated fills. No real money is involved at any point, and nothing here places a real order. To run it with the laptop shut down, see section 13: from the cutover on, it runs on a Render web service. Section 12 is how it ran on GitHub Actions before that, and the way back.

It assumes no trading or programming background. Every command is typed in the Mac's **Terminal** app, from inside the project folder. To get there, open Terminal, type `cd ` (with a space after it), drag the project folder from Finder into the Terminal window, and press Return. On Render, the commands in section 13 are typed in the service's **Shell** instead, a terminal on Render's website.

This isn't financial advice. Whoever wrote the research and this runbook isn't a licensed adviser. The research's gate decides which strategies pass, and whether to trade with real money is your decision alone.

The places that could only be decided by the research's one-time **holdout verdict** are now filled in from `research/artifacts/holdout_results.json`, `holdout_results.md` and `selection_K.json` (development). Nothing else is left open. Those files, like the other research write-ups and results this runbook cites (`RESULTS.md`, `research/VENUES.md` and the result files under `research/artifacts/`), are kept privately: they're in your working copy on the Mac, not in the public repository.

---

## 1. What the paper run is for

The research built a backtest: a simulation of how a strategy would have done on past prices, with an assumed cost for every trade. A backtest can be wrong in ways that only show up when the strategy runs forward in time against a real order book. The paper run checks for that.

- **It runs for at least 4 weeks.** Preregistration section 14: "Paper trading lasts at least 4 weeks and is compared with backtest expectations." In practice that means at least 28 *finished days* in the weekly comparison (section 6), so allow about 30 calendar days from the first start, more if the bot was down (the Mac off, GitHub's runs stopped, or the Render service stopped or held) for any of it. The status page counts them, and the run carries on until you end the test (section 13.8 on Render, 12.6 on GitHub Actions).
- **It compares paper with the backtest of the same decisions.** Each week, `compare` re-runs the research's own backtest engine on exactly the data and targets the paper run used, and puts the two side by side: what each order was assumed to cost against what it cost on paper, and each day's paper return against the backtest's.
- **It can't tell you whether the strategy makes money.** Four weeks is far too short for that. It only tells you whether paper *behaved* as the research said it would. Whether the strategy has an edge is the holdout gate's question (preregistration section 12), not this one's.
- **It is paper only.** `config.toml` says `mode = "paper"`, and the runner refuses to start in any other mode. With `mode = "live"` it stops at once with:

  > The systematic runner is paper only, and live mode is refused. Live trading needs exchange-side stops and a restart path that holds up (audit findings 34 and 48) and your explicit approval: see README.md, section "Going live with the systematic runner".

  It reads only public price data from Binance and Kraken. It holds no exchange key and never calls a private exchange endpoint.
- **Paper capital** is GBP 20,000, the research's account size, converted once into US dollars at the start, less 0.20% for the conversion, because the research's cost model trades on Kraken's USD books. On a test start on 5 November 2026 that came to USD 26,919.05. Paper fees are Kraken Pro Tier 3: 0.22% when an order rests in the book (maker) and 0.38% when it crosses the spread (taker).

What to expect from the strategy that's chosen:

| | |
|---|---|
| Strategy | `F1__sma_50__BTC__vol20`: Bitcoin only, long when the price is above its 50-day average, sized to a 20% volatility target. The recommended fallback for paper trading. It **did not pass the gate** |
| Expected maximum drawdown (worst fall from a peak) | 28.7% on development (2,208 days from 2018-07-01, `selection_K.json`) and 20.6% on the holdout (837 days from 2024-07-17, `holdout_results.json`). The gate's limit (G3) is 50%. These are past falls, not a ceiling: a future one can be deeper |
| Expected turnover (how much it trades in a year, as a multiple of the account) | 8.44 times a year on development and 13.94 times a year on the holdout (same files; the sum of each day's traded weight, times 365, divided by the days) |

All figures are track K (Kraken Pro Tier 3 costs, the same fees this runner uses), after costs, in USD.

**It did not pass the gate.** It passed G1 and G3 to G6, and failed G2: its holdout Sharpe was 0.44, and G2 needed at least 0.70, half its development Sharpe of 1.40. On track R (Revolut X's costs) it failed G2 too, 0.66 against at least 0.75. Over the holdout it returned 14.0% (10.4% in the GBP view). Bitcoin bought and held over the holdout, from 2024-07-17 (`BASE__G4__buy_and_hold_BTC__2024-07-17`), returned more, 18.8%, with a much deeper fall, 53.0%, and a lower Sharpe, 0.39. G4 compared the strategy's Sharpe with the higher of two Bitcoin holds read over the holdout, that one and Bitcoin held from the start of development, 2018-07-01 (`BASE__G4__buy_and_hold_BTC__2018-07-01`); both had a Sharpe of 0.39 on each track (`holdout_results.json`, `g4_readings`), so the strategy (0.44 on track K, 0.66 on track R) passed G4 either way. On average the strategy held 20.2% of the account in Bitcoin on development and 29.9% on the holdout, and cash otherwise.

**Why it's the paper candidate anyway.** No spot strategy passed, so this is a fallback, not a strategy with a demonstrated edge. It's the F1 winner, chosen on development data before the holdout was opened. The preregistered fallback, FALLBACK_TREND (`F1__ENS__BTC_ETH__vol40`), did worse on the holdout (-1.3%, failing G1, G2, G4 and G5 on track K), and preferring this one over it uses what the holdout showed, so this strategy's holdout figures flatter it a little. Only the forward paper run is a clean test. Not trading at all is an equally reasonable choice, and nothing tested makes a profit every day. This isn't financial advice.

---

## 2. Choosing the strategy

### The strategy line

In `config.toml`, under `[systematic]`, the line is now

```
strategy = "F1__sma_50__BTC__vol20"
```

with `max_asset_pct = 100` (below says why). Until the holdout verdict it held a placeholder, the preregistered fallback `F1__ENS__BTC_ETH__vol40`. The verdict passed no spot strategy (top of this runbook), so the line holds the researcher's recommended fallback for paper trading: `F1__sma_50__BTC__vol20`, which **did not pass the gate** (it failed G2; section 1). Whether to paper trade it, or not to trade at all, is your decision. The runner's start-up checks were run on this config in a temporary folder on 5 November 2026 and accepted it; the bot itself wasn't started.

### How to set it

1. Stop the bot if it's running (section 3).
2. Open `config.toml` in a text editor (in Finder, right-click it, then Open With, then TextEdit).
3. Change the text between the quotes on the `strategy = ` line to the strategy id, exactly as the research gives it. Save.
4. If a paper run already exists, move it aside first (below). One paper run holds one strategy.
5. Start the bot (section 3). If anything about the strategy is wrong, it refuses to start and says why, in plain words. Nothing is traded until it starts.

What it accepts: the F1 and F2 strategy ids in `research/artifacts/variants.jsonl` (the research's record of every variant it ran, kept privately), including their parameter-neighbour forms (ending in, for example, `__L=16` or `__band=0.08`) and their track-R forms (ending in `__R`), and the DCA fallbacks `dca_BTC` and `dca_BTC_ETH`. It refuses the cost-stress rows in that file, whose ids end in `__x2`, `__tier1_taker`, `__tier3_maker` or `__fee_bps=` followed by a number (with or without `__R` after it). Those rows re-price a strategy under other costs; they aren't strategies of their own. One is refused with, for example, `F1__ret_20__BTC_ETH__vol20__x2: 'x2' isn't a parameter change this runner understands`. It also refuses F3 (it needs perpetual futures, which UK retail clients can't trade), F4 (intraday, which a once-a-day runner can't reproduce) and F5. A mistyped id is refused with:

> Not starting the systematic runner. ...: not a strategy this runner knows. Use an F1 or F2 variant id from research/artifacts/variants.jsonl, or one of dca_BTC, dca_BTC_ETH.

### A strategy that can put more than half the account in one coin

`config.toml` now has `max_asset_pct = 100`, because `F1__sma_50__BTC__vol20` holds Bitcoin alone, so its one asset is its whole sleeve; at 50 the runner refuses it, exactly as below. The cap isn't what sizes it: its own volatility sizing sets its Bitcoin weight each day, as in the backtest (on average 20.2% of the account on development and 29.9% on the holdout). If you switch to a strategy that never goes above 50% in one coin, you can put it back to 50.

**The bot's own tests don't depend on this setting.** They set the limits they test themselves (`TEST_LIMITS` in `tests/sys_helpers.py`), so the two `CappedTargets` tests that `duo/AUDIT_STATUS.md` (kept privately, with the audit) cites still check a 50% cap whatever `config.toml` says. Run on 7 November 2026 with the shipped `max_asset_pct = 100`, the full suite (README, "Tests") ran 603 tests: OK.

`max_asset_pct = 50` caps any one coin at 50% of the account. Some strategies want more: an F1 variant on Bitcoin alone can hold 100% in Bitcoin, an F2 variant with `invvol` weights can too, and so does the DCA fallback `dca_BTC`, which only ever buys Bitcoin. Paper would then hold less than the research's strategy, so the runner refuses to start and tells you exactly what to change. For example, for `F1__ret_20__BTC__vol40`:

> Not starting the systematic runner. F1__ret_20__BTC__vol40 can put up to 100% of equity in one asset, above [systematic] max_asset_pct = 50. ... Raise max_asset_pct to 100 (the per-asset limit stays the strategy's own largest sleeve + 2 points), or set allow_capped_targets = true to run it capped ...

You have two choices, both in `[systematic]` in `config.toml`:

- **Run it as the research did:** set `max_asset_pct` to the number the message gives (100 in that example). The per-coin limit then becomes the strategy's own largest holding plus 2 percentage points, so it still blocks anything the strategy wouldn't do. This is the better choice for a paper run whose job is to match the backtest.
- **Run it capped:** set `allow_capped_targets = true` and leave `max_asset_pct` alone. Each decision then records both the research's target and the capped one, and `compare` replays the capped one, with the uncapped backtest shown beside it so you can see what the cap cost.

Strategies that never go above 50% in one coin (the earlier placeholder `F1__ENS__BTC_ETH__vol40`, the F2 equal-weight variants, `dca_BTC_ETH`) start at `max_asset_pct = 50` with no change.

### Changing the strategy after a run has started

The database records the strategy at the first start, and a start with a different strategy is refused, so results from two strategies can never mix. To start a new run, stop the bot and move the old run into its own folder. Type it as one line:

```
mkdir data/old_run_1 && mv -n data/paper_systematic.db* data/old_run_1/
```

Use a new folder name each time (`old_run_2` and so on). The command is written so that it can't overwrite an earlier archived run: if the folder already exists, it stops with `mkdir: data/old_run_1: File exists` and moves nothing. Then run it again with the next number. Afterwards, `ls data` should show no `paper_systematic.db` file left in `data` itself. The old run stays readable: `DUO_DATA_DIR=data/old_run_1 .venv/bin/python -m duo status` (and the same with `compare` or `decisions`) reads it there.

Don't touch `data/paper.db`. That's the separate history of the agent bot, which the systematic runner never uses.

---

## 3. Starting and stopping

### Starting

```
./start.sh
```

The very first time, it installs what it needs and creates a settings file called `.env`, then stops and asks you to run it again. The systematic runner needs nothing in `.env`: just run `./start.sh` a second time.

When it's running, the Terminal window shows something like the example below. It's from the test start on 5 November 2026, made with the earlier placeholder strategy. With the current `config.toml`, the first line names `F1__sma_50__BTC__vol20` instead, and the USD capital is worked out again at the exchange rate of that start:

```
Duo is running the systematic runner in PAPER mode: F1__ENS__BTC_ETH__vol40.
Paper capital 26919.05 USD (GBP 20000), fees 0.22% maker, 0.38% taker. Database: .../data/paper_systematic.db
Agents: off.
Dashboard: http://127.0.0.1:8787
Press Ctrl-C to stop. While it's stopped, nothing is checked or traded. Run python -m duo watchdog to be told when it's down.
```

Leave that Terminal window open. Open the dashboard by typing **http://127.0.0.1:8787** into your web browser. It's only reachable from this Mac.

If the Mac is on battery, `start.sh` warns you first. Plug it in.

### Stopping

Click on the Terminal window where it's running and press **Ctrl-C** (hold Control, press C). It finishes what it's doing and prints `Stopped.` Closing the Terminal window also stops it, less tidily.

While it's stopped, nothing is checked or traded, and on paper nothing fills. Starting it again with `./start.sh` carries on the same paper run where it left off. The gap is recorded, and any resting order from before the gap is closed as unfilled rather than filled at an old price.

If you've installed the bot's launchd agent (section 4), it isn't running in a Terminal window, and Ctrl-C doesn't apply. Stop it with `launchctl bootout` as shown there.

On Render (section 13), none of this applies: there's no `start.sh`, Ctrl-C or launchd. The service's supervisor starts the bot once the run has been seeded onto its disk (13.3), and starts it again if it stops. `python3 ops/render/ctl.py hold`, in the service's Shell, stops it and keeps it stopped (13.6).

### When it trades

The research uses daily price bars that close at **00:00 UTC**, which is midnight in the UK in winter (GMT) and 01:00 in summer (BST). The bot checks things every 5 minutes. It makes the day's one decision at the first check from **00:02 UTC** at which the just-closed bar is available. Each order starts as a resting order at the best price, is moved to the new best price every 15 minutes, and after 120 minutes crosses the spread for whatever is left, at no worse than 0.5% from the quoted price. So on a normal day, everything has finished by about 02:05 UTC. On many days nothing trades at all, because a holding only changes when its target moves by more than the research's band.

---

## 4. Keeping it running

This section is for the Mac. On Render (section 13), the supervisor starts the bot again when it stops, Render starts the service again, and the watchdog runs after every rebuild of the page, its verdict being the service's `/ok` (13.4).

A Mac on battery can sleep or run flat, and the bot then stops with no alert (audit finding 77). While the bot is stopped or the Mac is asleep, nothing is checked or traded, so missed days weaken the comparison.

### Keep the Mac awake and on power

- **Keep it plugged in.** `start.sh` runs the bot under macOS's `caffeinate -i -s`, which stops the Mac sleeping while it's on mains power. On battery it can still sleep, and when the battery runs out the bot stops.
- **Keep the lid open.** Closing the lid can still put the Mac to sleep.
- **After any restart** (a power cut, or a macOS update that restarts overnight), log in and start it again, unless you've installed the bot's launchd agent below.

To check from Terminal:

```
pmset -g batt
```

says `Now drawing from 'AC Power'` when it's plugged in, and `'Battery Power'` when it isn't.

```
pmset -g assertions | grep -i caffeinate
```

shows a line such as `caffeinate asserting on behalf of '.venv/bin/python' (pid 12345)` when some Python program is keeping the Mac awake through `caffeinate`. That alone doesn't show it's the bot: any other job started the same way prints the same kind of line. To tie it to the bot, run

```
pgrep -lf 'duo run'
```

which lists the bot's processes, each with its number first. The pid in brackets on the caffeinate line should be the number on the line that starts with the Python path and ends in `-m duo run` (not the line that starts with `caffeinate`). Also check that `.venv/bin/python -m duo status` shows a heartbeat a few minutes old. No output from the `pmset` line means nothing is being kept awake by `caffeinate`.

### Optional: the watchdog (recommended)

The watchdog is a separate small check, run every 5 minutes by macOS's own scheduler (launchd), outside the bot. It reads the heartbeat file the bot writes on every check, and the power source, and posts a macOS notification if:

- the bot's last heartbeat is more than 15 minutes old, or missing ("The bot is down"),
- the runner is halted (the kill switch, or you pressed Stop trading),
- it has placed no new orders for more than 30 minutes (an exchange or internet outage keeps the heartbeat going, so this is how an outage shows). This one also fires on a day the daily loss limit has tripped, or when the day's start is unknown after downtime: the bot is working as designed then, but it places no buys, so you'll be notified every 5 minutes until 00:00 UTC. The notification gives the reason (for example `daily loss limit: no buys today`). Check it with `.venv/bin/python -m duo status` or the dashboard before treating it as an outage,
- today's decision is overdue, or
- the Mac is on battery.

It only reads and notifies, and changes nothing. While a problem lasts, it notifies again every 5 minutes.

The launchd templates used below, `ops/com.duo.watchdog.plist` and `ops/com.duo.bot.plist`, are in your working copy on the Mac; the public repository leaves them out.

You can run the same check by hand at any time:

```
.venv/bin/python -m duo watchdog
```

It prints what it found, for example `Bot alive: heartbeat 83s ago, status ok.` and `Power: on mains.`, or the problem.

**To install it,** run these from the project folder, one at a time:

```
mkdir -p ~/Library/LaunchAgents
sed "s|__PROJECT__|$(pwd)|g" ops/com.duo.watchdog.plist > ~/Library/LaunchAgents/com.duo.watchdog.plist
plutil -lint ~/Library/LaunchAgents/com.duo.watchdog.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.duo.watchdog.plist
```

The second line copies the template, filling in the project folder's location. The third should end in `OK`. The fourth starts it, and it then also starts by itself each time you log in. To check it's loaded:

```
launchctl list | grep com.duo
```

Its output goes to `data/watchdog.log`, which you can read with `tail -n 20 data/watchdog.log`. Run the hand check above once with the Mac unplugged to see that a notification appears.

**To undo it:**

```
launchctl bootout gui/$(id -u)/com.duo.watchdog
rm ~/Library/LaunchAgents/com.duo.watchdog.plist
```

The template in `ops/` stays where it is.

### Optional: restart the bot automatically (KeepAlive)

The bot's launchd agent runs the bot in the background instead of in a Terminal window, starts it when you log in, and restarts it whenever it stops (launchd waits at least 30 seconds between starts). It also keeps the Mac awake with `caffeinate -i -s`, like `start.sh`.

**Never run `./start.sh` while this agent is loaded,** or two copies will try to run. Stop any `start.sh` run with Ctrl-C first. Then, from the project folder:

```
mkdir -p ~/Library/LaunchAgents
sed "s|__PROJECT__|$(pwd)|g" ops/com.duo.bot.plist > ~/Library/LaunchAgents/com.duo.bot.plist
plutil -lint ~/Library/LaunchAgents/com.duo.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.duo.bot.plist
```

What it would have printed in Terminal goes to `data/bot.log` instead: `tail -n 50 data/bot.log` shows the latest lines.

**To stop it,** use this, not Ctrl-C, because KeepAlive restarts a bot that's simply killed:

```
launchctl bootout gui/$(id -u)/com.duo.bot
```

**To undo it completely,** run that, then `rm ~/Library/LaunchAgents/com.duo.bot.plist`. After that, `./start.sh` is how you start it again.

Neither agent helps while the Mac is asleep, shut down or not logged in. They make sure you find out, and that the bot comes back when the Mac does.

---

## 5. The daily routine (about 5 minutes)

Any time after about 02:30 UTC (02:30 in the UK in winter, 03:30 in summer) the day's decision and orders should be finished, so a morning check works well.

On Render, the dashboard runs inside the service, where nothing outside can reach it: the status page (13.4), and `python3 ops/render/ctl.py status` in the service's Shell, stand in for it. Its Activity list is `python3 ops/render/ctl.py events` there (13.4), so on Render, step 2 of "Resuming after the kill switch" below means reading that.

### On the dashboard (http://127.0.0.1:8787)

1. **The header** says "Systematic runner on *strategy*. Next decision *time*." when all is well. A warning means one of two things. "The systematic loop hasn't run lately" means nothing is being checked or traded (see section 8). "Systematic runner: ..." means the loop is running but placing no new orders, for the reason it gives: the daily loss limit, API errors in a row, a missing daily bar, a stale or frozen quote, or waiting for prices. The table below says what each one means. "Halted" means the kill switch or Stop trading.
2. **Systematic runner panel, Strategy:** the right strategy, and the start date and capital you expect.
3. **Risk controls, last loop:** the 5 controls checked on the whole account at every loop (kill switch, daily loss, stale data, asset limit, gross exposure), each pass or fail. All should normally pass. The other two in the table below, quote fresh and order cap, are checked on each order, so they don't appear here: a failure shows on that order in **Working and recent orders** (status `blocked`, with the reason in its note), under "Controls failing" in **Latest decisions**, and in the decision export (section 7).
4. **Latest decisions:** there should be one for yesterday's bar (made shortly after 00:02 UTC today). If there isn't, see section 8.
5. **Targets against holdings:** once the day's orders are done, each coin's weight should be close to its target. A small gap is normal: a coin only trades when its target moves by more than the band.
6. **Working and recent orders:** nothing should still be working hours after the decision. Every order should be filled, or closed with a note saying why.
7. **Orders and fills: assumed against actual** and **Equity and P&L against the backtest replay:** the same figures as the weekly `compare`, kept up to date. Glance at them; judge them weekly.

### Or in Terminal

```
.venv/bin/python -m duo status
```

It prints the strategy, the capital, the last heartbeat and its status, and the last decision. It uses no network. This is from the test start on 5 November 2026, made with the earlier placeholder strategy, which began at 02:18 UTC, so its first decision was made at that start rather than just after 00:02 UTC:

```
Strategy    F1__ENS__BTC_ETH__vol40
Capital     26919.05 USD (GBP 20000)
Heartbeat   2026-11-05 02:27:02 UTC, ok, equity 26888.32956371416910
Decision    bar of 2026-11-04 00:00:00 UTC, made 2026-11-05 02:18:06 UTC, 2 order(s)
```

The heartbeat should be minutes old and `ok`. The decision's bar should be yesterday's date, and on a normal day it was made a few minutes after 00:02 UTC today. If the kill switch has fired, a last line starts with `HALTED`.

Then run `.venv/bin/python -m duo watchdog` (section 4) if you haven't installed it.

### The risk controls, and what to do when one trips

Each limit is in code, has its own setting in `[systematic]`, is checked on every order and recorded on every decision, and fails safe: if an input is missing or broken, it blocks trading rather than allowing it. Sells are never blocked by the daily loss limit, so the bot can always reduce risk.

| Control | What trips it | What the bot does | What you do |
|---|---|---|---|
| **Kill switch** (`kill_drawdown_pct = 45`) | The account is 45% below its highest value. Measured only on sane, fresh, moving prices, so a bad quote can't trip it | Cancels every order, sells everything at market (no worse than 0.5% from the quote), and stays halted until you resume. It fires even if you had paused trading | Don't resume straight away. See below |
| **Daily loss** (`daily_loss_pct = 8`) | The account is 8% below where it started the UTC day | Cancels resting buys and places no buys until 00:00 UTC. Sells carry on | Nothing. It clears itself at midnight UTC. Note the date. If the dashboard says the daily loss is *unknown* (equity or the day's start unknown), buys are blocked for the same reason: there's no usable Kraken price for the holdings right now, or none for the start of the day, usually after downtime. If the watchdog is installed, either case sets off its "placed no new orders" notification after 30 minutes and every 5 minutes after that until 00:00 UTC. Confirm the reason with `duo status` or the dashboard rather than treating it as an outage |
| **Stale data** (`signal_stale_minutes = 30`, `max_api_errors = 5`) | Yesterday's daily bar still missing 30 minutes after 00:00 UTC, or 5 failed requests to Binance or Kraken in a row | No new orders until it clears, which it does by itself | Check the Mac's internet (on GitHub Actions: run the workflow with action `probe`, section 12.3; on Render: `python3 ops/render/ctl.py probe` in the service's Shell, 13.5). If it lasts more than 30 minutes the watchdog tells you. See section 8 |
| **Quote fresh** (`max_quote_age_seconds = 90`, `max_frozen_quote_seconds = 600`) | A coin's Kraken price is over 90 seconds old, hasn't changed at all for 10 minutes, or makes no sense | No new orders in that coin, and paper orders don't fill on it, until it clears | Usually nothing. If it lasts, check the internet (on GitHub Actions, run `probe`; on Render, `ctl.py probe`) and whether Kraken is having problems |
| **Asset limit** (`max_asset_pct = 100` for the Bitcoin-only strategy, see section 2; `asset_margin_pct = 2`) | A buy would take one coin above the strategy's largest holding plus 2 points, and never above `max_asset_pct` | Cuts the buy down, or refuses it | Nothing on a single day. If it trips on most decisions, the strategy needs the setting in section 2 |
| **Gross exposure** (`max_gross_pct = 100`) | Holdings would pass 100% of the account, a sell is bigger than what's held, or a buy is bigger than the cash | Refuses the order. No borrowing and no short selling, ever | This shouldn't happen in normal running. Export the decision log (section 7) and have it looked at |
| **Order cap** (`order_margin_pct = 2`) | A buy bigger than its target change plus 2% of the account, or a sell bigger than its order set out to sell | Refuses the order | As for gross exposure: export the log and have it looked at |

The dashboard's two buttons also halt it. **Stop trading** cancels every order and places nothing new until you resume; the kill switch stays armed underneath. **Sell everything** does the same and sells every holding. Both make paper differ from the backtest, which `compare` will then show, so use them only if you mean to. On Render, `python3 ops/render/ctl.py halt` and `python3 ops/render/ctl.py flatten` do the same from the service's Shell (13.5).

### Resuming after the kill switch

The kill switch firing is itself an important result: a 45% fall from the peak is close to the research's own 50% limit (gate condition G3). On GitHub Actions, section 12.5 says how to take these steps there, and on Render, section 13.5: the dashboard and the commands below only reach the Mac's own paper run. Before resuming:

1. Read why: the dashboard shows "Halted: *reason*", and `.venv/bin/python -m duo status` shows the `HALTED` line.
2. Check it was a real market fall, not a problem with the Mac or its connection: look at the Activity list and the prices.
3. Export the decision log and run `compare` (sections 6 and 7), and keep both files, so the moment is recorded before anything changes.
4. Decide whether the paper run should carry on. If it does, resume.

To resume, either press **Resume** on the halt message in the dashboard's Systematic runner panel, or run the command below. After you used **Stop trading** yourself, use the header's **Resume trading** button. On Render, those buttons can't be reached: type `python3 ops/render/ctl.py resume` in the service's Shell, which runs this same command on the service's disk (13.5).

```
.venv/bin/python -m duo resume
```

It prints, for example:

```
Resumed. It was halted: <the reason>. The next daily decision trades as usual.
```

It works whether or not the bot is running. The highest-value mark restarts from the account's value at the bot's next check (within 5 minutes if it's running, or when it next starts if it isn't), otherwise the old peak would trip it again at once. The dashboard's **Resume** resets it at that moment instead. If nothing is halted, it says `The systematic runner isn't halted, so there's nothing to resume.` and changes nothing.

Every other control clears by itself when its cause does. Only the kill switch, Stop trading and Sell everything need you.

---

## 6. The weekly routine

Once a week, run:

```
.venv/bin/python -m duo compare
```

It prints a report and saves it as `data/compare_systematic.md` (each run replaces the last). To keep each week's copy, give it a dated name instead:

```
.venv/bin/python -m duo compare --out "data/compare_$(date +%F).md"
```

It reads only the paper database and the research's own files, needs no internet, and finished in under a second on a run with one decision. It reads the database without changing it, so it's safe to run while the bot is running.

On the Mac it reads the Mac's own paper run. When the run is on GitHub Actions or Render, the same report is published with its page and kept up to date: 12.4 and 13.4 say where, and how to keep each week's copy.

### What the report shows

- **Finished paper days compared.** A day counts once the next day's decision has been made, so this needs at least two decisions. The 4-week minimum means at least 28 here.
- **Paper return over them** and **Backtest return over them.** The backtest is the research's own engine, re-run on the same price data and the same targets the paper run used, with the research's cost model at Kraken Tier 3 taker fees. If they're close, paper is behaving as the research assumed.
- **Mean daily difference (paper - backtest).** The average of each day's paper return minus the backtest's. Negative means paper falls behind on average.
- **Tracking error, daily** and **annualised.** How much that daily difference typically wobbles from day to day (its standard deviation). Some wobble is unavoidable: paper's day runs from one decision to the next at Kraken's prices, a few minutes after the backtest's midnight-to-midnight at Binance's, and paper orders fill during the morning rather than exactly at the open. The annualised figure is the daily one times 19.1 (the square root of 365).
- **Paper fills** and **Filled as maker.** The share of fills that rested in the book and paid the 0.22% maker fee, rather than crossing the spread at 0.38%. The backtest assumes every trade pays the taker fee plus the measured spread and market impact for that size (with a minimum), plus any currency-conversion cost for that book, so a high maker share means paper is trading more cheaply than assumed. A low one isn't wrong in itself.
- **Orders: assumed against actual.** One line per order: the backtest's assumed price (the next day's open) and cost, against the paper price and fee. **Gap** is, in basis points (hundredths of a percent), how far paper's fill price was from the assumed open, plus paper's fee, minus the assumed cost. Positive means paper did worse than the research assumed. It isn't only cost: it also holds the price move between the 00:00 open and the paper fill (minutes to 2 hours later) and the small difference between Binance's USDT prices, which the open comes from, and Kraken's USD prices. So a single order's Gap can be large either way from the market's move alone.
- **Days.** Each finished day's paper and backtest return side by side.
- **Notes** under the table, for example days with no decision because the bot was down.

### What would count as paper not matching the backtest

The preregistration doesn't set a number for "close", so use these, and decide before you look at the results that these are the tests:

1. **Paper falls behind by more than noise.** After at least 28 finished days, paper isn't matching if the mean daily difference is further below zero than **2 × daily tracking error ÷ √(days)**. That multiplier works out as:

   | Finished days | Multiply the daily tracking error by |
   |---:|---:|
   | 14 | 0.535 |
   | 21 | 0.436 |
   | 28 | 0.378 |
   | 42 | 0.309 |
   | 56 | 0.267 |

   For example, with a daily tracking error of 0.20% over 28 days, a mean daily difference below -0.0756% would count as not matching. A shortfall smaller than that can't be told apart from chance.
2. **Paper pays more than assumed.** The average Gap over many orders is clearly above zero. Because each Gap also holds the price move since the open, judge the average over many orders rather than the share that are positive: a trend strategy that buys after rises can show positive Gaps from the market's drift alone, and 4 weeks may hold only a handful of orders. A clearly positive average over many orders suggests the research's cost model is too kind for this strategy.
3. **Holdings don't follow targets.** On the dashboard, weights stay away from their targets after the day's orders are done, or orders often end unfilled.
4. **Missed days.** The notes report days with no decision. More than a handful means the run is measuring a Mac that sleeps, not the strategy. Fix that (section 4) and run longer, so that 28 finished days are mostly days the bot was up.
5. **Controls that shouldn't trip.** Gross exposure or order cap failing at all. Gross exposure shows on the dashboard's "Risk controls, last loop"; order cap is checked on each order, so look under "Controls failing" in Latest decisions, for `blocked` orders in Working and recent orders, and in the decision export.

If paper doesn't match, don't change the strategy's settings to make it fit. That's the after-the-fact tuning the preregistration exists to prevent. Keep the reports and find out why (usually costs or timing). The preregistration's condition for going further, paper close to the backtest over at least 4 weeks, then isn't met.

If it does match over 4 weeks, that says the backtest's assumptions held up on paper. It still says nothing about whether the strategy will make money.

---

## 7. Exporting the decision log

Every decision is recorded with everything needed to audit it: the time, the strategy, a fingerprint of the research code, the price data it used, the signals and targets, holdings, the orders it meant to place, every risk control's value, limit and outcome, and every order, part-order and fill that followed. To export it all:

```
.venv/bin/python -m duo decisions
```

It writes `data/decisions_systematic.jsonl` (one decision per line, a standard machine-readable format) and prints how many decisions it wrote. To keep a dated copy:

```
.venv/bin/python -m duo decisions --out "data/decisions_$(date +%F).jsonl"
```

Do this weekly with `compare`, before resuming from the kill switch, and at the end of the run. On GitHub Actions and Render, `decisions.jsonl` is published with the page instead (12.4, 13.4). The dashboard's **Download CSV** link, under Trades, gives a simpler spreadsheet of fills (time, coin, side, quantity, price, value, fee). The bot doesn't do UK tax matching, so keep these files.

---

## 8. When the Mac sleeps, loses power or loses the internet

**If the Mac sleeps** (lid closed, or on battery): nothing is checked or traded, and nothing fills on paper. When it wakes, the bot records the gap, closes any resting order from before it as unfilled, and carries on. If yesterday's bar hasn't been decided yet, it decides at the first check, whatever the time of day. If the Mac slept through a whole UTC day, that day's decision is skipped. `compare` then holds the previous target for that day, as paper did, and counts the day in its notes. For the DCA fallback, a weekly purchase whose day was missed is bought late at the next decision.

**If it loses power or restarts:** the bot stops. When the Mac is back, log in, check it's plugged in, and start the bot again with `./start.sh`. If you installed the bot's launchd agent, it starts by itself once you log in. Then run `.venv/bin/python -m duo status` to check the heartbeat is fresh and the last decision is yesterday's.

**If the internet drops:** the bot keeps running and its heartbeat keeps going, but it can't get prices. It shows as degraded ("API errors in a row" or "Kraken quote missing, old or frozen") and places no new orders. Resting orders don't fill on stale prices. After 30 minutes like this, the watchdog notifies you, and again if the day's decision is overdue after 00:30 UTC. When the connection comes back, it clears by itself and makes any decision it's owed at the next check. You don't need to restart it.

**On Render** (section 13) there's no sleep and no battery, but the bot stops whenever Render stops the service: a deploy or a restart (13.6), Render's maintenance, or a move to another machine when one fails. The bot gets SIGTERM and about 20 seconds to finish its loop, and starts again on the new instance: within seconds after a deploy or a restart, and usually a few minutes later after Render's maintenance or a move to another machine. As on the Mac, the gap is recorded: one of over 11 minutes (two loops and a minute) counts as an outage, and closes any resting order from before it unfilled. That's why deploys wait until no order is working, and never fall from 00:00 to 00:10 UTC (13.6). A hold (13.6) stops the bot on purpose, with the same effect on the gap. If the service loses the internet, the bot behaves as above.

In every case: don't try to make up for missed days by hand. Note what happened and when, and let `compare` show it.

---

## 9. Costs

- **API spend: none.** `agents_enabled = false` in `[systematic]`, so the Claude agents are off (the start-up message says `Agents: off.`), and no Anthropic API calls are made. Leave it off. Even switched on, the agents can never trade in this mode; they'd only cost money.
- **Market data: free.** The runner reads only public Binance and Kraken price data, with no account and no key.
- **Trading fees: none in reality.** The fees on paper (0.22% maker, 0.38% taker) are simulated, so the comparison is fair. No money moves.
- **What it does cost:** on the Mac, the Mac staying on, plugged in and awake for the 4 or more weeks. On Render (section 13), the Starter instance at $7 a month and its 1 GB disk at $0.25 a month (render.com/pricing), charged by the second while the service exists, until it's deleted at the end of the test (13.8). GitHub Actions and Pages stay free for the public repository (12.9).
- **Render's bandwidth:** Render also meters outbound bandwidth, what the account's services send out, for the whole account: the Hobby plan includes 5 GB each calendar month, and each GB after that costs $0.15, charged to the card. No spend limit caps it: Render's spend limit covers only build minutes. Everything the service serves is public and never cached, the database at `/state/paper_systematic.db` included, which grows to about 6-7 MB by the end of the test, so anyone with the address can download it as often as they like. Normal use (the page refreshing every 5 minutes while it's open, four backup pulls a day, an outside monitor on `/ok`) stays around a gigabyte a month, inside the allowance. 13.4 says how to keep an eye on it.

---

## 10. Live trading: what it would need

**Nothing goes live without your explicit decision.** Preregistration section 14: "Nothing goes live from this work. ... Placing a live order or changing live capital needs your explicit approval." The systematic runner refuses live mode in code, and nothing in this runbook changes that.

Before the runner could trade real money, at least all of these would have to exist, each needing your approval (README, "Going live with the systematic runner"):

1. **Protection that doesn't depend on the bot running** (audit finding 34). The kill switch and the daily loss limit only work while the program runs, so a crash, sleep or outage leaves positions unwatched. Live needs protection on the exchange itself, or a server that never sleeps.
2. **A restart path that holds up** (finding 48): checking the real account against the bot's books after a restart, including when the account holds less spare cash than the bot thinks, without leaving positions unprotected.
3. **The live order problems** listed as "left for live" in `duo/AUDIT_STATUS.md` (kept privately, with the audit):
   - an order whose state is unknown because the exchange's reply was lost or unclear (findings 35, 36, 38 and 51),
   - a real fill that isn't booked (60),
   - problems with the exchange key (70),
   - the bot's books and the exchange disagreeing for a moment (53),
   - no limit on how wide the spread can be when an order crosses it (85),
   - stop sizing and stops during outages, which paper doesn't use (37 and 62),
   - private exchange queries (83),
   - UK tax matching, which isn't done; the GBP/USD rate is recorded so GBP figures can be rebuilt (82).
4. **At least 4 weeks of paper** with `compare` showing paper close to the backtest (section 6), and the holdout gate's verdict on the strategy: **fail**. `F1__sma_50__BTC__vol20` failed G2 on both tracks (holdout Sharpe 0.44 against at least 0.70 on track K, 0.66 against at least 0.75 on track R; `research/artifacts/holdout_results.md`, kept privately), so this condition isn't met, and no paper result can change that. No spot strategy passed the gate.

The research's reading of the UK rules that live trading would meet is in `research/VENUES.md`, kept privately. It isn't legal or tax advice.

This is not financial advice. Whether to trade is your decision.

---

## 11. Command reference

Run from the project folder.

| Command | What it does | Changes anything? |
|---|---|---|
| `./start.sh` | Starts the bot and keeps the Mac awake while it runs | Starts the bot |
| Ctrl-C in its window | Stops it | Stops the bot |
| `.venv/bin/python -m duo status` | Strategy, capital, last heartbeat, last decision, any halt | No |
| `.venv/bin/python -m duo watchdog` | Is the bot alive, and is the Mac on mains? Notifies if not | No |
| `.venv/bin/python -m duo compare` | Paper against backtest, saved to `data/compare_systematic.md` | Writes that file |
| `.venv/bin/python -m duo decisions` | The full decision log, saved to `data/decisions_systematic.jsonl` | Writes that file |
| `.venv/bin/python -m duo resume` | Lifts the kill switch, Stop trading or Sell everything, in the Mac's own paper run (for GitHub's, see section 12.3; on Render, `ctl.py resume` below) | Resumes trading |
| `.venv/bin/python -m duo tick` | One poll, then exits: what each GitHub Actions run does (section 12). With no paper run it does nothing and exits 3, unless given `--first-run`; `--flatten` sells everything and halts. Never on Render (13.10) | Polls and trades on paper |
| `.venv/bin/python -m duo probe` | Can this machine reach Kraken's and Binance's public prices? | No |
| `.venv/bin/python -m duo site --out DIR` | Writes the status page GitHub Pages shows into DIR, from the database. It opens the database writable and checkpoints it as it closes, so never beside a running bot, and never on Render | Writes that folder |
| `.venv/bin/python -m duo.site --out DIR` | The same page, opening the database read-only: the build to run beside a running bot. Render's supervisor runs it with `--schedule loop` | Writes that folder |
| `curl --create-dirs -o data/render/paper_systematic.db https://duo-paper.onrender.com/state/paper_systematic.db` | Downloads the Render service's latest backup (13.7) | Writes that file |
| `pmset -g batt` | Mains or battery? | No |
| `pmset -g assertions \| grep -i caffeinate` | Is some caffeinated program keeping the Mac awake? (Match its pid with `pgrep -lf 'duo run'`) | No |
| `launchctl list \| grep com.duo` | Which Duo launchd agents are loaded | No |

On Render, these are typed in the service's Shell, after `cd /opt/render/project/src` (section 13):

| Command | What it does | Changes anything? |
|---|---|---|
| `python3 ops/render/ctl.py check` | What the Shell sees: the Pythons, the disk, SQLite's self-test, `config.toml`, the supervisor, the bot, whether GitHub answers, the seed's marker and HOLD, and "Not ready" with the reason if the Shell isn't ready (13.2) | Only test files it removes |
| `python3 ops/render/ctl.py selftest` | SQLite's self-test alone, in the Python that runs it (`check` and `seed` run it in the service's own) | Only test files it removes |
| `python3 ops/render/ctl.py status` | `duo status` on the service's disk | No |
| `python3 ops/render/ctl.py events [--limit N]` | The bot's event log, newest last: what the dashboard's Activity list shows (13.4) | No |
| `python3 ops/render/ctl.py probe` | `duo probe` from the service | No |
| `python3 ops/render/ctl.py resume` | `duo resume` on the service's disk (13.5) | Resumes trading |
| `python3 ops/render/ctl.py halt` | Stops trading, as Stop trading does (13.5) | Halts |
| `python3 ops/render/ctl.py flatten` | Sells everything at the next loop, then stays halted (13.5) | Sells and halts |
| `python3 ops/render/ctl.py hold` | Stops the bot and keeps it stopped (13.6) | Stops the bot |
| `python3 ops/render/ctl.py unhold` | Lets the supervisor start it again (13.6) | Starts the bot |
| `python3 ops/render/ctl.py backup` | A backup now, and its manifest (13.7) | Adds a backup |
| `python3 ops/render/ctl.py seed SHA --sha256 HEX` | The cutover, once (13.3) | Puts the run on the disk |

How these were checked when this runbook was written (5 November 2026): `status`, `watchdog`, `compare` and `decisions` (with and without `--out`) were run against a running paper preview, and with `resume` and the move-aside commands against a copy of its database in a separate folder, including a simulated kill switch. The strategy refusals quoted in section 2 were produced by the runner's own start-up checks on temporary copies of `config.toml`. The launchd templates were filled in with `sed` and checked with `plutil -lint` in a temporary folder, and `pmset` and `launchctl list` were run. These were not run, because each would have started a second bot beside the preview, installed or removed a background service on this Mac, or read a log that only exists once a service is installed: `./start.sh` itself; `mkdir -p ~/Library/LaunchAgents`; the `sed ... > ~/Library/LaunchAgents/...` copies and `plutil -lint` on the copies there; the `launchctl bootstrap` and `launchctl bootout` lines; the `rm ~/Library/LaunchAgents/...` undo lines; `tail -n 20 data/watchdog.log` and `tail -n 50 data/bot.log`. They're the commands README gives, and you run them yourself. The move-aside command in section 2 was run in a scratch folder, including a second time with the same folder name, where it stopped with `File exists` and moved nothing. `pgrep -lf 'duo run'` and `pmset -g assertions` were run beside the preview. The Render commands, added on 7 November 2026, are tested on the Mac against stand-ins, and haven't been run on Render yet: section 13 ends with how they were checked.

---

## 12. Running it on GitHub Actions, with the laptop off

The paper run can also run on GitHub's machines, so the Mac doesn't have to stay on. It runs there from its start on 5 November 2026 until the cutover to Render (13.3), and again if it goes back (13.9). This section is the guide for that: how it runs on GitHub Actions before the move, and after going back. While it runs on Render, Paper run is disabled and section 13 applies instead; what still matters here then is publishing (12.2), notifications (12.2, step 6) and reading a copy of the run on the Mac (12.7). Apart from the one-time setup in 12.2, everything happens on github.com, in your web browser.

### 12.1 How it works

- **GitHub Actions runs the bot, and GitHub Pages shows the page.** Pages can only host static files, so it can't run anything itself. Actions starts a fresh machine on a schedule, and each run does the same five things: it restores the paper run's state, runs one poll of the runner (`python -m duo tick`, stopped if it's still going after 270 seconds), saves the state, rebuilds the status page and publishes it to Pages. The instructions for this are the workflow file `.github/workflows/paper.yml`, which GitHub shows as **Paper run**.
- **The repository** is `mattthewhiggins/duo-paper`, and it's public. So is the page, **https://mattthewhiggins.github.io/duo-paper/**: anyone with the link can see the paper trades and the paper account's value. It's all paper, and nothing in the repository holds a key or a password. After the cutover, that address shows a page that takes readers to the service on Render (13.1).
- **The schedule**, in UTC (add an hour for UK summer time):

  | When | What for |
  |---|---|
  | Every 15 minutes from 00:05 to 03:50 | The day's decision, then its orders: moved to the best price every 15 minutes, and crossing the spread after 2 hours |
  | Hourly at :35, from 04:35 to 23:35 | The risk checks, the heartbeat and the page |
  | 23:47 | So the next day's loss limit starts from a reading taken just before midnight |

  That's 37 short runs a day. GitHub starts scheduled runs late when it's busy, often by 5 to 60 minutes, and now and then skips one. That's normal, and the runner allows for it.
- **The state** is the `data/` folder: the database `paper_systematic.db` and the heartbeat file. It's kept on a branch of its own, `paper-state`. Each run takes it from there and saves it back when it's done. The branch keeps one commit (one saved version) per UTC day: later runs on the same day replace that day's commit, so each commit is how the run stood at the end of a day. The code is on `main`, and runs never change it. At the cutover, `data/` loses its database and heartbeat, in a commit that fences GitHub's copy off (13.3), and from then on the branch keeps the Render service's daily copy in `data/render/` (13.7).
- **Runs never overlap.** A run waits for the one before it to finish, and waiting runs go in the order they came, none of them cancelled, so two can never change the state at once.

### 12.2 Setting it up (once)

These steps are typed in Terminal, from the project folder, except where they say github.com.

1. Create an empty **public** repository called `duo-paper`: on github.com, the **+** menu, New repository, adding no README, licence or .gitignore. Or, with GitHub's command-line tool: `gh repo create mattthewhiggins/duo-paper --public`.
2. Copy it next to the project folder, and set its commit email to your GitHub no-reply address, so your own email address isn't published with every commit. github.com shows that address under Settings, Emails: it ends in `users.noreply.github.com`. Put it in place of `ADDRESS`:

   ```
   git clone https://github.com/mattthewhiggins/duo-paper.git ../duo-paper
   git -C ../duo-paper config user.email "ADDRESS"
   ```

3. Copy in the files the public repository gets:

   ```
   .venv/bin/python ops/publish_repo.py ../duo-paper
   ```

   It copies only what the paper run and its tests need: the code (with `ops/render/` and `.python-version`, which Render builds from, and both workflows), the tests, `config.toml`, the research cost model, README and this runbook. Not `data/`, not `.env`, and not `RESULTS.md`, `AUDIT.md` or the research write-ups and results, which stay private. It checks every file first. It stops, says which file (and line), and changes nothing if a file is called `.env` or isn't UTF-8 text, if a file on its list is a link, or if a file holds a path under /Users (also written with dashes for slashes, as in the name of a Claude session folder), an email address other than a GitHub no-reply or example one, or something shaped like a key (an Anthropic, GitHub or AWS key, a private key, or one of this project's key settings with a value). It checks for nothing else, so the look in step 4 matters. It never commits or pushes.
4. Look over what it copied, then commit and push it:

   ```
   git -C ../duo-paper status
   git -C ../duo-paper add -A
   git -C ../duo-paper commit -m "Paper test on GitHub Actions"
   git -C ../duo-paper branch -M main
   git -C ../duo-paper push -u origin main
   ```

   If git refuses the push because its login lacks the `workflow` scope, which adding or changing a workflow file (`.github/workflows/paper.yml` or `render-backup.yml`) needs, run `gh auth setup-git`, so that git uses the login of GitHub's command-line tool, and push again. If that's refused too, `gh auth refresh -s workflow` adds the scope to that login.
5. On github.com, in the repository: **Settings**, **Pages**, and under Build and deployment set **Source** to **GitHub Actions**. Without it the page can't be published. It stays so after the cutover, for the page that sends readers to Render (13.7). Nothing else in the repository needs setting: the workflows ask for the permissions they need themselves, and use no secrets.
6. Make sure GitHub emails you when a run fails, because that's the only alert from GitHub: on github.com, your profile picture, **Settings**, **Notifications**, then under **System**, **Actions**: turn on **Email**, with **Only notify for failed workflows**. After the cutover, the same setting covers the Render backup workflow (13.4).
7. On the Actions tab, **Paper run** should be listed. If GitHub says "Invalid workflow file", it hasn't accepted `.github/workflows/paper.yml`, and nothing runs until that's fixed: the message gives the line. Then check that GitHub's machines can reach the prices: run the workflow with `probe` (12.3 says how). It reads Binance's and Kraken's public prices and changes nothing. Its log lists each source, and ends with "The runner can poll from here."
8. Start the paper run: run the workflow with `start`. **Once.**

To publish a later change to the code or these documents, run step 3 again, then step 4 without the `branch -M` line. A change to the workflow file applies from the next run. After the cutover, a change reaches the Render service only when it's deployed there by hand (13.6). A new numpy version in `requirements-paper.txt` needs its file's hash there too (the comment there says which file), or the Install step refuses it.

**Don't use Re-run jobs** on a run in the Actions tab: start a new run with **Run workflow** instead (12.3). A re-run repeats that run's action with the code as it was then, and an old `start`, `resume` or `flatten` may not be what you want now. Its page upload can also clash with the one that run already made.

### 12.3 Running it by hand

On github.com, in the repository: **Actions** (along the top), **Paper run** (on the left), **Run workflow** (on the right). Leave "Use workflow from" on `main`, choose the action, and press the green **Run workflow** button.

| Action | What it does |
|---|---|
| `tick` | One poll now, exactly as a scheduled run does |
| `start` | Begins the paper run: its first poll records the start, with the capital converted at that day's GBP/USD rate. Run it once. On a run that has already started, it just carries on |
| `resume` | Lifts the kill switch, or a flatten, then polls at once. The drawdown peak restarts from the account's value at that poll |
| `flatten` | Sells everything at market and halts, like the dashboard's Sell everything |
| `probe` | Checks that Binance and Kraken answer from GitHub's machine. Changes nothing |

With GitHub's command-line tool, the same is, for example:

```
gh workflow run "Paper run" -R mattthewhiggins/duo-paper -f action=resume
gh run list -R mattthewhiggins/duo-paper -w "Paper run" -L 5
```

A run you start waits while another run is going. Waiting runs go in the order they came, and none is cancelled while it waits (GitHub keeps up to 100 waiting), so a `flatten` or `resume` you start still runs if scheduled runs come due meanwhile. To run an action again, start a new run: not Re-run jobs (12.2 says why). Once Paper run is disabled, as it is after the cutover, `gh run list` lists its runs only when given `-a` as well, and none of these actions can be run: on Render they're the Shell's `ctl.py` commands (13.5). Never run `start` after the cutover (13.10): while the note `data/MOVED_TO_RENDER.md` is on `paper-state` it's refused, as every action but `probe` is ("The paper run is on Render", 12.5), and once the note is gone, on a `paper-state` with no database in `data/`, it would begin a new paper run. Going back is 13.9.

Until `start` has run, scheduled runs do nothing and say "Paper run not started" in their summary. They don't fail, and don't email. If `start` fails, or its state can't be saved, nothing is kept, so the paper run hasn't begun: the run fails and says "The start failed" or "The start wasn't saved", and you run `start` again. That's so on a `paper-state` whose database is gone, too (12.5).

### 12.4 The daily check (about 2 minutes)

Do it every day, any time after about 03:00 UTC, when the day's decision and its orders should be done (later if GitHub was slow). It's also the only way to notice that runs have stopped, since that sends no email (12.5).

1. **Open the page**, https://mattthewhiggins.github.io/duo-paper/. The line under its title says how far the test has got ("day N, and M of 28 finished days compared"), and the box below that how the run is:
   - **Running normally**: the last run, and when. Every check passed.
   - **Needs attention**: the latest run's tick didn't finish, today's decision is overdue (still none by 02:00 UTC, although runs are happening), or at the last run the runner wasn't placing new orders, for the reasons it gives. Most of these clear by themselves at a later run.
   - **Halted**: the kill switch or a flatten. Nothing new trades until you resume it (12.5 says how, and what to check first).
   - **Stale**: no run has updated the page for about 3¼ hours (195 minutes), or runs update it but none has checked the market for that long. Look at the Actions tab (12.5).
   - **Finished**: the 28-day test is complete, and no run has updated the page since, which is what disabling the workflow at the end does (12.6).

   Below the box are the paper account's value against just holding Bitcoin, what's held, the strategy's latest decision ("The strategy today (paper)" once today's is made), the latest decisions, orders and fills, paper against the backtest, and **Run health**: the last and next runs, whether today's decision is made, and the runs in the last 24 hours and 7 days against the scheduled slots. Runs started by hand count as runs too, so fewer runs than slots usually means GitHub skipped some, or some ticks failed. After going back from Render, the loop's checks there count as runs too, for a while (13.9, step 7).
2. **The Actions tab** lists every run, newest first. A green tick is fine; a red cross failed, and GitHub has emailed you (12.5). Click a run, and its summary says what that tick did: its status, any decision, any fills and any halt.
3. Look for what section 5 says to look for: one decision for yesterday's bar, holdings close to their targets once the day's orders are done, and nothing still working hours after the decision.

The weekly comparison (section 6) and the decision log (section 7) are published with the page and kept up to date, at https://mattthewhiggins.github.io/duo-paper/compare.md and https://mattthewhiggins.github.io/duo-paper/decisions.jsonl. To keep each week's copy, as sections 6 and 7 suggest:

```
curl -o "data/compare_actions_$(date +%F).md" https://mattthewhiggins.github.io/duo-paper/compare.md
curl -o "data/decisions_actions_$(date +%F).jsonl" https://mattthewhiggins.github.io/duo-paper/decisions.jsonl
```

After the cutover, github.io has neither file: the service on Render serves both, and 13.4 has the `curl` lines for them.

### 12.5 When GitHub emails you

This is for Paper run, before the cutover and after going back. On Render, 13.4 says what alerts you.

GitHub emails you when a run fails, if your notifications are set as in 12.2, step 6. That's the only alert, and only a run that happened can send it: **a run that never starts can't fail.** If GitHub stops starting the scheduled runs (a problem at GitHub, or the 60-day switch-off in 12.6), no run fails, so no email comes. The page's "Stale" notice, after about 3¼ hours, and the Actions list are then the only signs, which is why the check in 12.4 is daily.

A run fails when:

- **the tick failed.** Open the run and click the **Tick** step: the error is at the end. A tick that's still going after 270 seconds (a request that hangs, say) is stopped, and the step ends with exit code 124, or 137 if it had to be killed 20 seconds later. What it did until then is saved, as after any failed tick (a new run's start apart: see below), and the next run carries on.
- **the watchdog found something that needs you.** The runner is halted (the kill switch, or a flatten); it has placed no new orders for more than 30 minutes (an exchange or data outage, or the daily loss limit); the day's decision is overdue (still none 2 hours after 00:00 UTC); or the run wrote no heartbeat. The **Watchdog** step says which.
- **the state couldn't be restored or saved.** The **Restore the paper state** or **Save the paper state** step says why. The save also refuses a database that something still has open ("... is still open in process N, so it may be half-written. Not saving it."). `paper-state` keeps the last state that was saved, and the next run carries on from that, so at worst one run's work is lost. The page only ever shows a saved state, so that run doesn't update it. A new run's start that isn't saved is different: there's no paper run yet (see below).
- **the page couldn't be built, uploaded or published.** The **Build the status page** or **Upload the status page** step says why, or else the separate **deploy** job, which publishes the page to Pages. If deploy fails, check that the repository's Settings, Pages, Source is still **GitHub Actions** (12.2, step 5); otherwise it's usually a problem at GitHub. The state was saved first, so the paper run carries on, and a later run publishes the page again.
- **the machine couldn't be set up:** the checkout, Python, or the **Install** step, which refuses a numpy file whose hash doesn't match `requirements-paper.txt`. Nothing ran, and the next run tries again.
- **a `probe` found the runner can't poll from GitHub's machine.** Its **Probe the data sources** step lists each source.

While the cause lasts, every run fails and emails you, so a halt means an email every run until you resume or stop the workflow. The daily loss limit clears by itself at 00:00 UTC, and most other causes clear by themselves at a later run.

**Work out the cause on GitHub, not on the Mac.** The Mac's dashboard, `duo status` and `duo resume` don't reach the GitHub run: they read the Mac's own `data/`, so they show the Mac's own paper run, if it has one, or say there's no paper run there.

1. **Read the reason** on the page (its box at the top, and Run health) or in the failed run's **Watchdog** or **Tick** step.
2. **If it's stale data or API errors,** run the workflow with action `probe` (12.3). It says whether GitHub's machines can reach Binance and Kraken now.
3. **After the kill switch, keep the evidence** before anything changes: the two `curl` lines in 12.4 download the page's `compare.md` and `decisions.jsonl`. Or copy the database to the Mac as in 12.7 and run:

   ```
   DUO_DATA_DIR=data/actions .venv/bin/python -m duo compare --out "data/compare_actions_$(date +%F).md"
   DUO_DATA_DIR=data/actions .venv/bin/python -m duo decisions --out "data/decisions_actions_$(date +%F).jsonl"
   ```

   Then decide, as section 5's "Resuming after the kill switch" says, whether it was a real market fall and whether the paper run should carry on, from the page's figures and the runs' logs.
4. **To resume,** run the workflow with action `resume`: Actions, **Paper run**, **Run workflow**, `resume`. Or in Terminal: `gh workflow run "Paper run" -R mattthewhiggins/duo-paper -f action=resume`.

Four messages say what to do next. "No paper state" means the tick found no database on `paper-state`: a new paper run never starts by itself, so nothing ran. If the run's database really is gone, `start` begins a new run there. "The start failed" and "The start wasn't saved" mean the start was thrown away, so the paper run hasn't begun: run `start` again. Until you do, later runs only say "Paper run not started", without failing, or fail with "No paper state" where `paper-state` already existed. "The paper run is on Render" comes from a Paper run that started after the cutover, by mistake or late: it stopped at **Restore the paper state**, so nothing ran or was saved. Keep Paper run disabled (13.10), and see 13.3, step 9.

### 12.6 Stopping it

**The runs don't stop by themselves.** When the line under the page's title says "the 28-day test is complete (28 finished days compared)", the test has reached its end, and these steps end it:

1. Keep the final comparison and decision log, with the two `curl` lines in 12.4.
2. If you want the paper account to end with no holdings, run `flatten` and wait for that run to finish. It fails and emails you, because the runner is then halted: that's expected. Disabling alone sells nothing.
3. Disable the workflow: Actions tab, **Paper run**, the **...** menu at the top right, **Disable workflow**. Or in Terminal: `gh workflow disable "Paper run" -R mattthewhiggins/duo-paper`.

Step 3 also stops the runs at any other time, and it's the cutover's step 2 (13.3). Nothing runs after that, so nothing is checked or traded. The page keeps showing the last run, and once no run has updated it for about 3¼ hours it says "Finished" if the 28-day test was complete, or "Stale: not updated lately" if it wasn't. **Enable workflow**, in the same place, carries on from the saved state. After the cutover, though, `paper-state` has no database in `data/` to carry on from, only the note, and while it's there every run but a `probe` stops with "The paper run is on Render", so enabling it alone isn't going back: 13.9 is. These steps end the test only while it's on GitHub; on Render, 13.8 does.

GitHub also turns off a public repository's schedules by itself after 60 days with no activity in the repository, and emails a warning first. If that happens, enable the workflow again. After the cutover the same goes for the Render backup workflow's schedule (13.7).

### 12.7 Where the state is, and reading it on the Mac

The database is `data/paper_systematic.db` on the `paper-state` branch. On github.com, switch the branch menu from `main` to `paper-state` to see it, and its commits list one per day. After the cutover, `data/` holds only `MOVED_TO_RENDER.md`, and the copy of the Render service's database is `data/render/paper_systematic.db`: 13.7 says how to read it. Before the cutover, or after going back, to read it on the Mac with the usual commands (sections 6 and 7), copy it out:

```
git -C ../duo-paper fetch origin paper-state
mkdir -p data/actions
git -C ../duo-paper show origin/paper-state:data/paper_systematic.db > data/actions/paper_systematic.db
DUO_DATA_DIR=data/actions .venv/bin/python -m duo status
```

`compare` and `decisions` work the same way. It's a copy: nothing done to it reaches GitHub. The Mac's own `data/paper_systematic.db` is a separate paper run, and `./start.sh` carries on that one, not GitHub's. Don't copy a database from the Mac onto `paper-state`: the branch is public, and a database the Mac wrote can hold the Mac's folder paths in the errors it logged. Nor onto the Render service's disk: the only way a database gets there is the seed, from `paper-state` (13.3).

### 12.8 How it differs from the laptop

- **The decision** is made at the first run after 00:02 UTC that sees the just-closed daily bar: normally the 00:05 run, but GitHub often starts it later, and a skipped run moves it to the next. The bar and the targets are the same as on the laptop; only the time the orders start moves. The overdue alert waits until 2 hours after 00:00 UTC.
- **Resting orders** can't be watched between runs, so each run judges a resting paper order on Kraken's 1-minute candles since it was placed, by the same paper rule (it fills at its price only if the market traded 0.02% through it), and books the fill at the minute it happened. The laptop checks a fresh quote every 10 seconds instead. If a run can't get those candles from Kraken, a resting order that's due to be re-priced or to cross is left as it is for the next run to judge, as long as it's within its 60-minute life on the book. So while the candles can't be had, re-pricing and the switch to taker can come up to about an hour late.
- **Outages:** only a gap of more than 3 hours since the last run counts as one (`cron_gap_minutes` in `config.toml`). The run logs it, and each resting order from before the gap is still judged on the candles over its life, closing unfilled if nothing traded through it. (On the laptop, a gap closes them unfilled without that check.)
- **Risk checks** (the kill switch, the daily loss limit, stale data) run once per run, every 15 to 60 minutes, rather than every 5 minutes, so a fall between runs is acted on at the next run. "5 API errors in a row" comes after 3 to 5 failed runs, because one failed run can count twice: Kraken on the morning of a decision (the poll and the decision each ask it), and Binance before the day's bar is seen.
- **Binance's market-data host:** daily bars come from data-api.binance.vision, which serves the same klines as api.binance.com. api.binance.com refuses US addresses (HTTP 451), and GitHub's machines are mostly in the US, so it's only the fallback. Each decision records which host it used.
- **No dashboard.** The page is read-only. `resume` and `flatten` stand in for the dashboard's Resume and Sell everything. There's no Stop trading: disabling the workflow stops everything, checks included. The Mac's dashboard and its `duo` commands work on the Mac's own `data/`, never on GitHub's run (12.5, 12.7). (On Render there's a halt, `ctl.py halt`, and a hold, which stops the bot itself: 13.5 and 13.6.)
- **The watchdog** runs at the end of every run, and its alert is GitHub's failure email instead of a notification on the Mac. Unlike the Mac's, it can't tell you when runs stop happening (12.5). (On Render it runs after every rebuild of the page, and its verdict is the service's `/ok`: 13.4.)

Render runs the laptop's own loop, so on Render none of these differences apply: 13.1 says how it runs.

### 12.9 Costs, and GitHub's terms

- **Cost: none.** Actions on GitHub's standard machines, and Pages, are free for public repositories. Render's costs are in section 9.
- **GitHub's terms** allow Actions to be used for developing and testing the software project in the repository. This paper run is a time-boxed test of this project, stopped at its end by disabling the workflow (12.6), so it fits. After the cutover, what's left on GitHub is the Render backup workflow's copy and a static page, both part of the same test, which ends on 3 December 2026 (13.8). A live trading bot isn't a test: it would need its own server (README, "Moving to a server"), as well as everything in section 10.
- **No real orders, and no keys.** The workflows have no secrets, they read only public prices from Binance and Kraken (and the Render backup, only the service's own backup and page), and the runner refuses live mode in code.

How this was checked when it was written (5 November 2026): the scripts that restore and save `paper-state`, and `publish_repo.py`, are tested on the Mac against a stand-in for GitHub (`tests/test_actions_scripts.py`), and the workflow file was checked as YAML. `tests/test_workflow.py` checks that every command the workflow runs is one the bot takes, and that its exit codes, folders and schedule match the code and the page. Then the workflow's steps were rehearsed on the Mac as GitHub would run them, with stand-ins for Kraken and Binance and a folder standing in for GitHub: what `publish_repo.py` copied was committed as `main`, and each run was a fresh copy of it and new processes (restore, tick, save, page). There was a `start` at 00:07 UTC, then 95 scheduled runs over the next 2 days and 10 hours, each started up to 45 minutes late, with a few hourly ones skipped and one run with Kraken down, then a `tick` by hand. Every run was saved and its page built; the three decisions, the maker buy, the re-priced sell that crossed as a taker after 2 hours and the page's figures came out as expected, and `paper-state` held one commit per day. `flatten`, `resume`, and a scheduled run before `start` were rehearsed the same way. Three later changes were checked on the Mac only: the Tick step's 270-second limit with a stand-in for Linux's `timeout`, the save's check for a database still open with a folder standing in for Linux's `/proc`, and the order runs wait in (`queue: max`) against GitHub's published schema for workflow files. On GitHub itself, the first `probe` (5 November 2026, before those three changes) reached Kraken and Binance's data-api.binance.vision, while api.binance.com refused with HTTP 451, as expected. The first `start` is the first real test of the rest.

---

## 13. Running it on Render

From the cutover on (13.3), the paper run lives on a Render web service instead of GitHub Actions. It runs the laptop's own loop there, nonstop, on a server that doesn't sleep, so the Mac can stay off. This section is the guide for that. Section 12 is how the run works on GitHub Actions before the move, and again if it goes back (13.9).

**Who does what.** You make the Render account, add a card, create the service, and type every command in the service's Shell. The GitHub steps (publishing the code, the cutover's steps on GitHub, running the backup workflow by hand) are run by the assistant, Claude, on the Mac, or by you with GitHub's command-line tool `gh`, logged in with the `workflow` scope (12.2, step 4). Nothing here places a real order or needs an exchange key.

**The Shell** is a terminal on Render's website: open the service, then **Shell** on the left. It runs inside the service's own instance, so it sees the disk. Every Shell command below starts from the repository's folder:

```
cd /opt/render/project/src
```

Render doesn't document which folder the Shell starts in or what settings it gets, so `ops/render/ctl.py` doesn't rely on them: it uses `/var/data` when `DUO_DATA_DIR` isn't set, and runs duo's commands with the service's own Python. A deploy or a restart closes the Shell. Each session takes a little of the instance's 512 MB, so close it when you're done.

### 13.1 How it works

- **The service** is a Render web service called `duo-paper`, in Frankfurt, on the Starter instance (0.5 CPU, 512 MB). Render builds it from the public repository's `main` branch, and only when you deploy it by hand. It has a 1 GB disk mounted at `/var/data`, which is where the paper run lives: everything else on the instance is replaced at every deploy and restart.
- **Its start command** runs `ops/render/serve.py`, the supervisor. It answers on Render's port at once, and runs the bot, `python -m duo run`, as its child, with `DUO_DATA_DIR=/var/data`. The bot is the laptop's own loop (sections 3 to 5): it checks Kraken's prices every 10 seconds, runs the strategy's loop every 5 minutes, at minute 2, 7, 12 and so on of each hour, and makes the day's decision at the first loop from 00:02 UTC. Its dashboard is on 127.0.0.1:8787 inside the instance, which only the Shell can reach.
- **The bot starts only on this paper run.** `python -m duo run` starts a new paper run on any database without one, so the supervisor starts the bot only while all of these hold, and otherwise waits and says why (the first one that fails is its reason): the disk is mounted at `/var/data`; there's no `HOLD` file there (13.6); `paper_systematic.db` is there, holding a start record for `config.toml`'s strategy and quote, and `config.toml` has `[systematic] enabled = true`; `render_seed.json`, which only the seed writes (13.3), names that start record; nothing answers on 127.0.0.1:8787 already, so no other bot is running; and the supervisor holds the disk's lock, `.supervisor.lock`, so no other supervisor does.
- **It keeps the bot running.** A bot that exits by itself is started again after 30 seconds, then 60, 120 and 300, then every 600 while it keeps exiting (from 30 again once one has run for 30 minutes). A bot whose heartbeat is over 20 minutes old counts as stuck, and is stopped and started again. If the supervisor itself ever exits, Render starts the service again.
- **Stopping.** Render stops the service with SIGTERM for a deploy, a restart or maintenance, and kills it 30 seconds later. The supervisor gives the bot 20 seconds to finish its loop (it prints `Stopped.`), kills it if it hasn't, and exits within 25 seconds.
- **The page.** 45 seconds after each loop, the supervisor rebuilds the status page with `python -m duo.site --schedule loop`, which only reads the database, into a new folder that's served once it's complete, and then runs `python -m duo watchdog` (section 4's check, without the power source). A build that fails leaves the last good page up.
- **Backups.** The supervisor copies the database into `/var/data/backups` every 6 hours and when asked, and GitHub copies the latest into the `paper-state` branch (13.7).
- **The log.** Every event goes to the service's **Logs** on Render as a line starting `supervisor:`, and the bot's own output as lines starting `bot:`. The supervisor's lines are also kept on the disk, in `/var/data/supervisor.log`.

What it serves, at https://duo-paper.onrender.com:

| Address | What it is |
|---|---|
| `/` | The status page, with its `status.json`, `compare.md` and `decisions.jsonl` beside it |
| `/healthz` | Render's health check: `alive` while the supervisor runs, whatever the bot is doing |
| `/ok` | `ok` (HTTP 200) only while the bot runs and the watchdog's last pass and the page are both under 10 minutes old. Otherwise HTTP 503 and one line saying why (13.4) |
| `/supervisor.json` | What the supervisor is doing and why: its state and reason, the bot's process, restarts, last exit and memory, the page build, the watchdog, the backup, the probe, the disk, the seed's marker, and its own process |
| `/state/manifest.json`, `/state/paper_systematic.db`, `/state/heartbeat_systematic.json` | The latest backup (13.7) |

The page, like the repository and the old page, is public. It's the page 12.4 describes, but it knows it's on Render and judges the run by the loop's own limits: "Needs attention" once the last check is over 11 minutes old (two loops and a minute, the runner's outage rule), "Stale" once it's over 15 minutes old (the watchdog's limit) or the page itself is over 20 minutes old, and today's decision overdue from 00:30 UTC. It says "Held" during a hold, and its Run health says when the run moved to Render and how many runs GitHub Actions made before that.

The supervisor's state, in `/supervisor.json` and the Logs, is one of `waiting` (the bot can't start yet, for the reason given), `running`, `stopping`, `held` (13.6), `backoff` (the bot exited, and starts again at the time given) and `shutting-down`.

**On GitHub, after the cutover,** Paper run is disabled, but stays in the repository for going back (13.9). `paper-state` has no database in `data/` any more, only a note, `data/MOVED_TO_RENDER.md`. While the note is there, a Paper run that runs by mistake, `start` included, stops at its step **Restore the paper state** with "The paper run is on Render": it runs nothing, saves nothing and publishes no page. The Render backup workflow keeps the service's copy in `data/render/` instead (13.7), and the tag `render-seed` marks the commit the service was seeded from. https://mattthewhiggins.github.io/duo-paper/ shows a page that takes readers to the service.

### 13.2 Setting up the service (once)

Before the service is made, the public repository must hold the Render code: `ops/render/` (the supervisor `serve.py`, the Shell's controls `ctl.py`, and `pull_backup.py`, which the backup workflow runs), `.python-version` and `.github/workflows/render-backup.yml`. Publishing them is 12.2's steps 3 and 4, done by the assistant, and the push needs the `workflow` scope, because it adds a workflow file. Until the cutover, the Render backup workflow does nothing: each run finds Paper run still active, says so in a notice and passes. The same publish changes files in `duo/` (the status page's Render mode among them), so GitHub's decisions from then on record a new fingerprint of the code (`duo_code_sha256`). That's expected, and nothing checks it.

**You, on render.com:**

1. Make an account. Its plan can stay Hobby, which has no fee of its own: the service is what's paid for, and any outbound bandwidth past the 5 GB a month that Hobby includes, at $0.15 a GB, which no spend limit caps (section 9). Add a card under Billing.
2. Turn on failure emails: **Integrations**, **Notifications**, destination **Email**, and **Only failure notifications**. Render doesn't say what a new account starts with, so set it. Render then emails you when a deploy fails, when the service becomes unhealthy, when its disk is over 80% full, and when it suspends the service after it repeatedly failed to start. Notices of maintenance come by email too.
3. **New**, **Web Service**, then the **Public Git Repository** tab: `https://github.com/mattthewhiggins/duo-paper`, and **Connect**. Fill in the form as below, then **Create Web Service**.

| Setting | Value |
|---|---|
| Name | `duo-paper`. Its address is then https://duo-paper.onrender.com, unless Render adds something to the name because it's taken. If it does, use the address Render shows wherever this runbook says duo-paper.onrender.com, and have the assistant change the `RENDER_URL` line in `render-backup.yml` to it and publish that, before the cutover |
| Language | Python 3 |
| Branch | `main` |
| Region | Frankfurt. It can't be changed later |
| Root Directory | Blank |
| Build Command | `pip install -r requirements-paper.txt`, in place of the `requirements.txt` one Render suggests |
| Start Command | `python -u ops/render/serve.py` |
| Instance Type | Starter: 0.5 CPU, 512 MB, $7 a month |
| Environment Variables | `DUO_DATA_DIR` = `/var/data`, and nothing else |
| Advanced, Disk | Name `duo-data`, Mount Path `/var/data`, Size 1 GB, $0.25 a month |
| Advanced, Health Check Path | `/healthz` |
| Advanced, Auto-Deploy | Off, if the form offers it. A service made from a public repository's address is only ever deployed by hand anyway, which is what's wanted, because every deploy restarts the bot (13.6) |

Python's version comes from `.python-version` at the repository's root, which says `3.13`. Render's default is 3.14, and `requirements-paper.txt` pins numpy 2.5.4's wheel for Python 3.13 by its hash, so pip would refuse any other. **Don't set `PYTHON_VERSION`**: it overrides the file, and must be a full version such as 3.13.5. Add no Secret Files either (13.10).

**The first deploy.** The service's **Events** list the build and the deploy. The build's log should show Python 3.13 and numpy 2.5.4 installed from its `cp313` wheel. A hash error there means the build didn't get Python 3.13: check that `.python-version` is in the repository and that `PYTHON_VERSION` isn't set. Once the service is live, its **Logs** show the supervisor starting, with lines like these, not necessarily in this order:

```
supervisor: starting: pid ..., Python 3.13.x, commit ...
supervisor: serving on 0.0.0.0:10000
supervisor: took the lock on /var/data/.supervisor.lock
supervisor: waiting: no paper run on the disk yet: /var/data/paper_systematic.db isn't there. It's seeded once, at the cutover (RUNBOOK 13.3), and the bot never starts one
supervisor: page built and served: ...
supervisor: watchdog fails: The bot is down: no readable heartbeat at /var/data/heartbeat_systematic.json.
supervisor: probe: exit code 0, Binance answers from data-api.binance.vision, api.binance.com. Kraken answers. The runner can poll from here.
```

The watchdog fails until the seed: there's no bot yet. The page, https://duo-paper.onrender.com/, says "No paper run yet".

**The checks before any seed.** All three must pass. If one doesn't, don't seed: the run stays on GitHub, as it is, while the assistant finds out why.

1. **`/supervisor.json`**, at https://duo-paper.onrender.com/supervisor.json, shows:

   | Field | What it should show |
   |---|---|
   | `state`, `reason` | `waiting`, "no paper run on the disk yet: ..." |
   | `supervisor.python` | 3.13, and a patch number |
   | `supervisor.sqlite_version` | The SQLite inside that Python, which reads the run's database for the supervisor's checks and backups. `ctl.py check`'s self-test (below) shows it works |
   | `data_dir.is_mount` | `true`: `/var/data` is the disk. `total_bytes` and `free_bytes` say about 1 GB |
   | `supervisor.lock` | `true` |
   | `supervisor.pid`, `ppid`, `pgid`, `proc1_cmdline` | How Render runs the start command: whether the supervisor is the instance's first process (`pid` 1) or a shell's child, and in which process group. Note them for the assistant. The test restart below is what shows the stop signal reaches it |
   | `probe.exit_code`, `probe.output` | `0`, with "The runner can poll from here." last: Binance and Kraken answer from Render's addresses in Frankfurt |
   | `child.vmrss_kb` | `null` until the seed. Once the bot runs, its memory in kB, which has to fit in the instance's 512 MB with the page builds and any Shell session |
   | `supervisor.commit` | The commit of `main` that was deployed |

2. **`ctl.py check`, in the Shell:**

   ```
   cd /opt/render/project/src
   python3 ops/render/ctl.py check
   ```

   It changes nothing beyond a write test and a self-test of SQLite, which remove what they make. It prints what the Shell sees: its working folder, its Python, the service's own Python and its version, `DUO_DATA_DIR`, the data folder, whether that's a mount, its free space, the write test, the SQLite self-test, what `config.toml` asks for (the strategy and quote, with the runner on), the supervisor's state, whether the bot's dashboard answers, whether GitHub answers, the seed's marker and HOLD. The self-test runs in the service's own Python, the one the supervisor runs in: it makes a small database on the disk as the seed leaves the real one (in WAL mode, with no `-wal` or `-shm` beside it), opens it read-only as the supervisor does, reads it back and removes it. Its row names the SQLite version. (`python3 ops/render/ctl.py selftest` runs the self-test alone, in the Python that runs it.) The `GitHub` row asks once each of the two addresses on GitHub the seed downloads from, the list of the repository's branches and tags and its raw files, and says whether they answer from here. `DUO_DATA_DIR` says whether the Shell gets the service's settings (if it says "not set, so /var/data", the commands use the disk all the same).

   `check` says itself whether the Shell is ready. When every check passes, it ends with "The disk is mounted and can be written to, so the other commands can run (RUNBOOK 13)." and exits 0. Otherwise it prints a line starting "Not ready:" for each that fails, and exits 1:

   - "Not ready: the other commands refuse until the data folder is the service's disk ...": the data folder isn't the disk, or can't be written to.
   - "Not ready: the SQLite self-test failed, so the seed must not go ahead: ...": the supervisor could never read the run's database on this service, so the bot would never start. `ctl.py seed` refuses then too, and the code has to change first.
   - "Not ready:" and `config.toml`'s problem, then "The seed refuses it, and the supervisor won't start the bot.": `config.toml` on `main` doesn't run this paper run, or can't be read (13.4's table of `waiting` reasons says what each means, and 13.10).
   - "Not ready: Render can't download from GitHub now (...)": one of those two addresses doesn't answer from here, because GitHub is refusing too many requests, say, or is down. Run `check` again later. This one counts only before the seed: once the disk is seeded, the `GitHub` row still shows the problem, but `check` doesn't say "Not ready" for it.
   - "Not ready: this Shell can't reach the supervisor at ...": the Shell doesn't reach the supervisor on 127.0.0.1, so `hold`, `unhold` and `backup` couldn't confirm what they did, and `halt` and `flatten` couldn't reach the bot.
   - "Not ready: the supervisor's data folder is ..., but this Shell's is ...": the Shell would work on another folder than the supervisor's. Set `DUO_DATA_DIR` in the Shell as the line says (`export DUO_DATA_DIR=/var/data`), and run `check` again. If it says the supervisor's data folder "is not given", the service's own `DUO_DATA_DIR` isn't right: the supervisor's reason says what to do.

   If it says "Not ready", don't seed.
3. **A test restart.** On the service's page on Render: **Manual Deploy**, **Restart service**. In the **Logs**, the old instance must log

   ```
   supervisor: shutting-down: SIGTERM
   supervisor: stopped
   ```

   before the new one logs `supervisor: starting: ...`. That shows Render's stop signal reaches the supervisor, so a deploy lets the bot finish its loop. Without those two lines it doesn't: don't seed. Render's advice for a start command whose process doesn't get the signal is to start it with `exec`, so the next thing to try is the Start Command `exec python -u ops/render/serve.py`, then this test again. The restart closes the Shell. Once the bot runs, a restart also logs `bot: Stopped.` and `supervisor: the bot stopped, exit code 0` between those two lines.

### 13.3 The cutover

The cutover moves the run from GitHub to the service: GitHub's runs stop, the last state GitHub saved is fenced off on `paper-state`, and the service is seeded from it. The assistant and you do it together. Each step says who does it, what success looks like, and what to do if it fails. Up to step 4, nothing has changed that enabling Paper run again doesn't undo. From step 5 on, going back is 13.9.

**When.** In daytime UTC, outside 00:00-04:00 UTC, once the day's decision has been made and its orders have finished, so nothing is working or resting. The page on github.io should say "Running normally", with today's decision made. If the last run said "Needs attention" because the runner was degraded, wait for a run that's ok: how long it has been degraded carries over to the service, and its watchdog would count from GitHub's time.

**Before.** 13.2 is done: the service is live and waiting, `/supervisor.json` shows Python 3.13 and the disk mounted, the probe exited 0, `ctl.py check` passed in the Shell, its `SQLite self-test`, `Supervisor` and `GitHub` rows included, and the test restart logged `shutting-down: SIGTERM`.

The commands for the Mac are typed in Terminal, from the project folder, with the clone of the public repository at `../duo-paper`, as in 12.2. The assistant checks the clone first (13.7, 13.8 and 13.9 use it too):

```
git -C ../duo-paper remote get-url origin
git -C ../duo-paper config user.email
```

The first must print `https://github.com/mattthewhiggins/duo-paper.git`, and the second your GitHub no-reply address, ending in `users.noreply.github.com`. If `../duo-paper` isn't there, make it with both lines of 12.2's step 2: the clone used to publish may be somewhere else, or gone, since macOS clears temporary folders. If the address is anything else, set it as 12.2's step 2 says, before any commit. `paper-state` is public, and a save later the same day keeps the commit's author, so an address committed there stays in its history.

**A second cutover.** If the run went back to GitHub before (13.9), that service was deleted. Set up a new one as in 13.2, its checks included (if Render gives it another address, change `RENDER_URL` as 13.2 says), and enable Render backup again, which 13.9's step 3 disabled:

```
gh workflow enable "Render backup" -R mattthewhiggins/duo-paper
```

While Paper run is enabled, Render backup's runs do nothing. Step 5 moves the tag `render-seed` to the new SHA.

1. **Stop anything else that starts Paper run** (the assistant, on the Mac). While GitHub starts this account's scheduled runs hours late, a stopgap on the Mac, `stopgap_dispatch.sh` (a script outside the project), starts a tick at each scheduled slot. It was started as `bash SCRIPT DEADLINE STOP_FILE`, and it stops by itself, with exit code 0, at its deadline (a Unix time) or at its first look once its stop file exists. First record how it was started, because this runbook can't print its paths, and once it has stopped `ps` can't show them:

   ```
   ps -o args= -p "$(pgrep -f stopgap_dispatch.sh)"
   ```

   That's its command line: the script, its deadline and its stop file (`stopgap.stop`). Keep it. Then create the stop file it names (`touch STOP_FILE`), and:

   ```
   pgrep -f stopgap_dispatch.sh
   ```

   Success: it prints nothing. The stopgap looks for its stop file every 15 seconds, and can send one last tick as it stops, so wait until `pgrep` prints nothing before going on. If anything else was ever set up to start Paper run, such as a job on cron-job.org, turn that off too.

   **To start the stopgap again** (step 4 and 13.9's step 5 send you here), once Paper run is enabled: remove the stop file, start the script in the background with the command line recorded above, but with a new deadline after the test's end, such as `1796342400` (4 December 2026, 00:00 UTC), and check it's running:

   ```
   rm -f STOP_FILE
   nohup bash SCRIPT 1796342400 STOP_FILE >> LOG 2>&1 &
   pgrep -f stopgap_dispatch.sh
   ```

   SCRIPT and STOP_FILE are the paths recorded, and LOG is a file beside the script for what it prints, such as `stopgap.log`. Success: `pgrep` prints its process number. Until it does, start each slot's tick by hand, at the times in 12.1's schedule:

   ```
   gh workflow run "Paper run" -R mattthewhiggins/duo-paper -f action=tick
   ```

2. **Disable Paper run** (the assistant):

   ```
   gh workflow disable "Paper run" -R mattthewhiggins/duo-paper
   gh workflow list -R mattthewhiggins/duo-paper --all --json name,state
   ```

   Success: Paper run's state is `disabled_manually`. Note the time, in UTC, for step 9. If it fails, nothing has changed: check `gh auth status`, and try again.

   **Render backup's runs from here on.** From the disable until step 8's copy is saved, a scheduled Render backup run may start at any time: its schedule runs hours late, and one may be waiting behind Paper run's last runs, in the queue they share. Such a run fails, and GitHub emails you about it. That's expected: it saves nothing, and leaves `paper-state` alone. What it says depends on when it lands: before step 5's tag, "Couldn't fetch the tag render-seed"; after the tag but before the seed, that `state/manifest.json` answered 404; in both, also "The bot needs you: ... waiting: no paper run on the disk yet"; and between the seed and the supervisor's own backup after the loop's first check, that the backup's newest check "isn't later than GitHub's last". Step 8's run, started by hand, is the one that must pass.

3. **Wait until no Paper run is left to run** (the assistant). Disabling a workflow doesn't cancel runs that have already started or are waiting, so this waits for them:

   ```
   gh run list -R mattthewhiggins/duo-paper -a -w paper.yml -L 100 --json status -q '[.[] | select(.status != "completed")] | length'
   ```

   Success: it prints `0`. Until it does, run it again every minute or so.

4. **Take the last state GitHub saved, and check it** (the assistant):

   ```
   SHA=$(git ls-remote https://github.com/mattthewhiggins/duo-paper refs/heads/paper-state | cut -f1)
   echo "$SHA"
   gh api "repos/mattthewhiggins/duo-paper/git/trees/$SHA?recursive=1" --jq '.tree[].path'
   ```

   The tree must hold `data/paper_systematic.db` and `data/heartbeat_systematic.json`, and nothing ending in `-wal` or `-shm`. Then download the database, take its sha256 and check it:

   ```
   mkdir -p data/cutover
   curl -fsSo data/cutover/paper_systematic.db "https://raw.githubusercontent.com/mattthewhiggins/duo-paper/$SHA/data/paper_systematic.db"
   HEX=$(shasum -a 256 data/cutover/paper_systematic.db | cut -d' ' -f1)
   echo "$HEX"
   .venv/bin/python - data/cutover/paper_systematic.db <<'EOF'
   import sqlite3, sys
   db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
   one = lambda sql: db.execute(sql).fetchone()
   print("integrity_check:", one("PRAGMA integrity_check")[0])
   print("resting:", one("SELECT COUNT(*) FROM sys_children WHERE status = 'open'")[0],
         "working:", one("SELECT COUNT(*) FROM sys_orders WHERE status = 'working'")[0])
   print("flatten_requested:", (one("SELECT value FROM state WHERE key = 'flatten_requested'") or ["not set"])[0],
         "sys_kill:", (one("SELECT value FROM state WHERE key = 'sys_kill'") or ["not set"])[0])
   print("last heartbeat:", *one("SELECT datetime(ts, 'unixepoch'), status FROM sys_heartbeats ORDER BY ts DESC LIMIT 1"))
   EOF
   ```

   Success: `integrity_check: ok`, `resting: 0 working: 0`, `flatten_requested` not `1`, `sys_kill` not `1` (a halted run would arrive halted), and the last heartbeat is the last run's, `ok`. If anything else shows, stop here: enable Paper run again (`gh workflow enable "Paper run" -R mattthewhiggins/duo-paper`), start the stopgap again as step 1 says, and try another day.

5. **Fence off GitHub's copy** (the assistant). One commit on `paper-state`, on top of SHA, removes the database and the heartbeat from `data/` and puts a note in their place. It's pushed with a lease on SHA, so it's accepted only while SHA is still the branch's tip, which proves SHA is the last state GitHub saved. A Paper run that runs after it, by mistake or late, stops at **Restore the paper state** with "The paper run is on Render", so nothing runs or is saved, and its page isn't published. While the note is there, even its `start` is refused.

   ```
   git -C ../duo-paper fetch origin "$SHA"
   git -C ../duo-paper worktree add --detach ../duo-paper-fence "$SHA"
   cat > ../duo-paper-fence/data/MOVED_TO_RENDER.md <<EOF
   # The paper run moved to Render

   On $(date -u '+%Y-%m-%d at %H:%M UTC') the paper run moved to the Render service at https://duo-paper.onrender.com,
   seeded from $SHA, which the tag render-seed marks. Paper run is disabled, and data/ holds no database, so a Paper
   run that ran by mistake would find none and do nothing. The Render backup workflow keeps the service's latest backup
   in data/render/. RUNBOOK 13.3 says how the move was made, and 13.9 how to go back.
   EOF
   git -C ../duo-paper-fence rm -q data/paper_systematic.db data/heartbeat_systematic.json
   git -C ../duo-paper-fence add data/MOVED_TO_RENDER.md
   git -C ../duo-paper-fence commit -m "The paper run moved to Render"
   git -C ../duo-paper-fence log -1 --format='%an <%ae>'
   ```

   The last line must show your GitHub no-reply address (**Before**, above, says why). If it shows another, set the address as 12.2's step 2 says, then `git -C ../duo-paper-fence commit --amend --reset-author --no-edit`, and look again. Then push it:

   ```
   git -C ../duo-paper-fence push --force-with-lease=refs/heads/paper-state:"$SHA" origin HEAD:refs/heads/paper-state
   ```

   Success: the push is accepted. On a second cutover, the tag `render-seed` still marks the earlier seed. To keep that one for the record, copy it to `render-seed-1` now:

   ```
   git -C ../duo-paper fetch origin refs/tags/render-seed:refs/tags/render-seed-1
   git -C ../duo-paper push origin refs/tags/render-seed-1
   ```

   Then mark SHA with the tag, and check both on GitHub:

   ```
   git -C ../duo-paper tag -f render-seed "$SHA"
   git -C ../duo-paper push -f origin refs/tags/render-seed
   git ls-remote https://github.com/mattthewhiggins/duo-paper refs/heads/paper-state refs/tags/render-seed
   git -C ../duo-paper-fence rev-parse HEAD
   ```

   Success: `ls-remote` shows `paper-state` at the fence's commit, which `rev-parse` prints, and `render-seed` at SHA. On a first cutover, the `-f` of `tag` and `push` changes nothing. On a second, it moves the tag off the earlier seed, which is right, because the lease has just shown that SHA is the last state GitHub saved. A plain push of a tag that's already there says "Everything up-to-date", which proves nothing, so `ls-remote` is the check. The seed (step 6) checks the fence and the tag on GitHub itself, and refuses until both are there, so if the tag's push fails, push it again until `ls-remote` shows it. Then tidy up: `git -C ../duo-paper worktree remove ../duo-paper-fence`.

   **If the push is refused** (`rejected ... (stale info)`), or the fetch can't find SHA, a run saved after SHA: remove the worktree (`git -C ../duo-paper worktree remove --force ../duo-paper-fence`) and go back to step 3, then step 4 for the new tip. SHA and HEX from this pass are void then: the seed takes those of the pass whose fence was accepted.

6. **Seed the service** (you, in the Shell), only once step 5's push of the fence and push of the tag were both accepted. Use SHA and HEX from step 4 of the pass whose fence was accepted, which the assistant gives you then: a pair from a pass whose lease was refused is void.

   ```
   cd /opt/render/project/src
   python3 ops/render/ctl.py seed SHA --sha256 HEX
   ```

   Before it changes anything, it checks, in this order:
   - what's already on the disk. The seed never writes over anything, so a database, its `-wal` or `-shm`, a heartbeat or a seed marker there stops it, unless the marker says this very seed landed already (below);
   - the SQLite self-test that `check` runs;
   - that `config.toml` has the runner on;
   - GitHub, read as it is at that moment, as `git ls-remote` reads it: `paper-state`'s tip must be the fence (no longer SHA, with the note and no database), and the tag `render-seed` must mark SHA. Then it says "GitHub: the fence is on paper-state ...".

   Then it makes HOLD, so the bot can't start halfway; downloads `paper-state`'s database and heartbeat at SHA into `/var/data/.incoming`, trying each up to three times; checks the database's sha256 is HEX, and that SHA has no `-wal` beside it; checks the database: SQLite's integrity check, a start record that matches `config.toml`, nothing resting or working, no sell-everything request waiting, and a check from GitHub at or before its last tick; prints what carries over (any halt, `sys_waiting`, since when it has been degraded, GitHub's last tick and its newest heartbeat); then moves the heartbeat and the database into place, writes `render_seed.json`, and removes the HOLD it made. Last, it waits up to 4 minutes for the loop's first check, printing the supervisor's state each time it changes.

   Success: "Seeded, and the bot is running on Render: the loop's first check was at ...", then what's next (this section's steps 7 and 8), and exit 0.

   Otherwise, what it says is what to do:
   - A "Not seeded: ..." about GitHub, which cites step 5: the fence isn't on `paper-state`, or the tag `render-seed` isn't at SHA, so step 5 isn't complete. Finish it, or move the tag as step 5 says, and seed again. If it only couldn't read GitHub, run it again later. Nothing was done.
   - "Already seeded from SHA at ...": the seed had landed already, and this was a second run of it, after the Shell dropped, say. Nothing was changed. If it then says the seed was cut off before it took its HOLD away, `python3 ops/render/ctl.py unhold` starts the bot.
   - A "Not seeded: ..." that ends "Delete nothing (RUNBOOK 13.10): have the assistant look": the disk already holds something, such as a seed from another commit ("the disk was already seeded from ...") or a database that a cut-off seed placed ("... a seed was cut off after it placed the database"). Delete nothing (13.10), and have the assistant look.
   - Any other "Not seeded: ...", such as a download, the sha256 or one of the database's checks: nothing was placed, and everything the seed made is gone. Put right what it names, and run it again (the fence stays as it is). A `/var/data/.incoming` left by a seed that a deploy or restart cut short has to be removed first, as its message says.
   - "The seed failed while moving its files into place ...": it keeps HOLD, so the bot can't start, and lists what's on the disk. Delete nothing (13.10), and have the assistant look: the disk has to be put right before anything else.
   - "Seeded, but the bot exited at once: ..." or "Seeded, but the loop hasn't checked after 4 minutes: ...": the run is on the disk, and the supervisor keeps trying to start the bot. The rest of the line is the supervisor's state and reason, and the bot's last lines (13.4 says what each reason means).
   - Ctrl-C while it waits prints "Stopped waiting. The seed is done ...": the supervisor starts the bot by itself, and step 7 shows how it goes.

   A `python3 ops/render/ctl.py hold` typed while the seed runs takes over the HOLD the seed made: the seed then leaves it, says so, and exits 0 without waiting, and the bot stays stopped until `unhold` (13.6).

   Whatever can't be put right the same day: go back (13.9).

7. **Watch the first checks** (you and the assistant). The bot checks once as soon as it starts, then at each 5-minute loop:
   - https://duo-paper.onrender.com/supervisor.json says `running`, with the bot's `pid` and its memory in `child.vmrss_kb`, and `seed` holds the marker the seed printed;
   - the page says "Running normally" ("Waiting for the loop's first check" until then), and its Run health says "Render since" that first check, with GitHub Actions' runs before it;
   - https://duo-paper.onrender.com/ok answers `ok` after the next page build, 45 seconds after a loop.

   Success: all three within two loops (10 minutes). If not, `/ok`'s reason says why (13.4). If the bot can't run on Render, go back (13.9), and before 00:00 UTC.

8. **Copy the service's backup to GitHub** (the assistant). The supervisor backed up the moment the seed finished, before the loop's first check, and GitHub keeps only a backup with a check from Render in it, so the supervisor backs up again by itself within about 5 minutes of that first check. Its Logs then show a second `supervisor: backup ... taken` line, whose newest heartbeat is the loop's, and `backup.at` in `/supervisor.json` moves. (`python3 ops/render/ctl.py backup` in the Shell forces one.) Then:

   ```
   gh workflow run "Render backup" -R mattthewhiggins/duo-paper
   gh run list -R mattthewhiggins/duo-paper -w "Render backup" -L 3
   ```

   Success: the run succeeds, both its jobs, and the copy is on `paper-state`, where this lists `heartbeat_systematic.json`, `manifest.json` and `paper_systematic.db`:

   ```
   gh api "repos/mattthewhiggins/duo-paper/contents/data/render?ref=paper-state" --jq '.[].name'
   ```

   And https://mattthewhiggins.github.io/duo-paper/ says "The paper test has moved", and takes readers to the service. Only a run started by hand, like this one, puts that page up, and only once its pull has worked: a run whose pull failed leaves github.io as it was. If the run fails, its error says why and what to do (13.4, "Render backup's emails"), and the run on Render carries on meanwhile. If it was the pull that failed, run it again once the supervisor's second backup is there.

9. **About 9 hours after the disable, check that no Paper run started after it** (the assistant). GitHub starts this account's scheduled runs 7 to 8 hours late, and whether it can still start one for a slot that came due before the disable isn't documented. With the time noted in step 2, as `YYYY-MM-DDTHH:MM:SSZ`:

   ```
   gh run list -R mattthewhiggins/duo-paper -a -w paper.yml --created ">=YYYY-MM-DDTHH:MM:SSZ"
   ```

   Success: no run is listed. If one is, the fence made it harmless: it failed at **Restore the paper state** with "The paper run is on Render", so nothing ran or was saved, and its page wasn't published. It failed, so GitHub emailed you about it. Nothing needs doing.

From then on, the daily check is 13.4.

### 13.4 Day to day

**The daily check (about 2 minutes).** Any time after about 02:30 UTC (02:30 in the UK in winter, 03:30 in summer), when the day's decision and its orders should be done:

1. Open https://duo-paper.onrender.com/. The line under the title says how far the test has got, and the box below it how the run is:
   - **Running normally**: the last check, and when. Every check passed.
   - **Needs attention**: the loop has missed its last checks (the last is over 11 minutes old), today's decision is overdue (none by 00:30 UTC, although the loop is running: `python3 ops/render/ctl.py events`, below, says why), or at the last check the runner wasn't placing new orders, for the reasons it gives. Most of these clear by themselves.
   - **Halted**: the kill switch, a halt or a flatten. Nothing new trades until it's resumed (13.5).
   - **Held**: on purpose, with HOLD (13.6).
   - **Stale**: the loop has stopped (its last check is over 15 minutes old, though the page is being rebuilt), or the page itself hasn't been rebuilt for over 20 minutes. Look at the service on Render (below).
   - **Finished**: the 28-day test is complete and the loop is held, which is how the test ends (13.8).
2. Look for what section 5 says to look for: one decision for yesterday's bar, holdings close to their targets, and nothing still working hours after the decision. Run health counts the loop's checks in the last 24 hours and 7 days against those due (one every 5 minutes, 288 a day; a start or restart adds one), with the longest gap between them.
3. If you like, https://duo-paper.onrender.com/ok: `ok`.

The weekly comparison (section 6) and the decision log (section 7) are rebuilt with the page. To keep each week's copy, on the Mac:

```
curl -o "data/compare_render_$(date +%F).md" https://duo-paper.onrender.com/compare.md
curl -o "data/decisions_render_$(date +%F).jsonl" https://duo-paper.onrender.com/decisions.jsonl
```

**Once a week, the bandwidth.** Render charges for what the service sends out past the 5 GB a month that the Hobby plan includes, and anyone with the service's address can download the database as often as they like (section 9). So once a week, look at the service's **Metrics**, its Outbound Bandwidth graph, and at the account's **Billing**, which has the month's total. Normal use stays around a gigabyte a month. If it climbs well past that, someone is downloading repeatedly: have the assistant look.

**`/ok`, and what it says.** It answers `ok` (HTTP 200) only while the bot runs and the watchdog's last pass and the page are both under 10 minutes old. Otherwise it answers HTTP 503 with one line:

| `/ok` says | What it means | What to do |
|---|---|---|
| `held` | HOLD is on the disk: the bot is stopped on purpose | Nothing, if you held it (13.6). Otherwise `python3 ops/render/ctl.py unhold` |
| `waiting: ...` | The bot can't start: the first of the supervisor's checks (13.1) that fails | See the reasons below |
| `backoff: the bot exited with code N; ...` | The bot keeps exiting, and is started again at the time given | Its last lines, in the Logs (`bot:`) and in `child.last_exit` in `/supervisor.json`, say why |
| `stopping: ...` | The supervisor is stopping the bot, for HOLD, or because it's stuck (`wedged: ...`) | Look again in a few minutes |
| `shutting-down: SIGTERM` | A deploy, restart or maintenance is under way | Look again once it's live |
| `page not built yet`, `page not rebuilt for N minutes` | Page builds are failing. The last good page stays up | `page.error` in `/supervisor.json` |
| `watchdog: The bot is down: ...` | Its heartbeat is over 15 minutes old, or missing | The Logs |
| `watchdog: The bot is running but halted: ...` | The kill switch, a halt or a flatten | 13.5 |
| `watchdog: The bot is running but has placed no new orders for N minutes: ...` | Degraded for over 30 minutes: an exchange or data outage, or the daily loss limit, which keeps this up from 30 minutes after it trips until 00:00 UTC while the bot works as designed, placing no buys | Section 5's table of controls. `python3 ops/render/ctl.py probe` for an outage |
| `watchdog: Today's daily decision is N minutes overdue ...` | No decision by 00:30 UTC, although the loop is running | The page's box gives any reason the runner has: a late daily bar shows as "Yesterday's daily bar hasn't arrived from Binance yet", and failed requests as their own reason. If it gives none, the decision itself is failing at each loop: `python3 ops/render/ctl.py events` (below) shows the error it records, and the assistant should look |
| `watchdog: not run yet`, `watchdog: last ran N minutes ago` | The watchdog hasn't run after a good build lately | `watchdog` and `page` in `/supervisor.json` |

What a `waiting` reason means, in the order the supervisor checks them (13.1):

| `waiting: ...` says | What it means | What to do |
|---|---|---|
| "DUO_DATA_DIR isn't set", or "DUO_DATA_DIR is ..., which isn't an absolute path" | The service's environment variable is missing or wrong | Set it to `/var/data` in the service's Environment, and deploy (13.2) |
| "/var/data isn't a mount point, so the service's disk isn't there" | The disk isn't attached, or its mount path isn't `/var/data` | The service's Disk settings, and its Events (13.2) |
| "no paper run on the disk yet: ..." | The disk hasn't been seeded | Nothing: the cutover seeds it (13.3) |
| "/var/data/paper_systematic.db couldn't be read (...)" | The supervisor's read-only open of the database failed, for the reason in brackets | If `ctl.py check`'s SQLite self-test fails, this service's SQLite is why (13.2). "file is not a database" or "malformed" means the file is damaged: hold (13.6), touch nothing, and have the assistant look |
| "/var/data/paper_systematic.db holds no paper run: it has no start record", or "/var/data/paper_systematic.db's start record isn't a JSON object" | The database on the disk isn't the seeded run | Hold, touch nothing, and have the assistant look (13.10) |
| "config.toml couldn't be read (...)", or "config.toml is missing '...'" | `config.toml` on `main` is broken: a TOML error, at the line and column given, or a setting taken out. `ctl.py check`'s `config.toml` row says the same | Put it right on `main`, and deploy, outside 00:00-00:10 UTC (13.6, 13.10) |
| "/var/data/paper_systematic.db holds a paper run of ... but config.toml asks for ...", "config.toml has the systematic runner off ...", or "config.toml's [systematic] enabled is ..., which isn't true or false ..." | `config.toml` on `main` was changed | Put it back, and deploy (13.10) |
| "/var/data/render_seed.json isn't there, so the database didn't come through the seed" | The database didn't come through the seed, or a seed was cut off after it placed it | 13.3, step 6. Never start the bot on it by hand |
| "/var/data/render_seed.json couldn't be read (...)" | The seed's marker is damaged | Never edit it (13.10): have the assistant look |
| "/var/data/render_seed.json was written for another start record ..." | The marker and the database don't belong together | Have the assistant look |
| "something already listens on 127.0.0.1:8787, so another bot may be running" | Another bot is running in the instance | 13.10 |
| "another supervisor holds the lock" | Another instance is running on the same disk. One is normally gone within 30 seconds of an old instance stopping | If it lasts, have the assistant look |
| "couldn't open /var/data/.supervisor.lock (...)", or "couldn't lock /var/data/.supervisor.lock (...)" | The disk can't be written to | `ctl.py check`'s `Write test` row, and the service's Disk settings (13.2) |

**What to look at when `/ok` isn't 200:**

1. Its reason, above.
2. https://duo-paper.onrender.com/supervisor.json: `state` and `reason`; `child` (`restarts`, and `last_exit` with the bot's last lines); `page.error`; `watchdog.output`; `backup.error`; and `log_tail`, the supervisor's last 40 lines.
3. The service's **Logs** on Render (the supervisor's `supervisor:` lines, and the bot's `bot:` lines: what it prints as it starts, and as it stops or fails; Render keeps 7 days) and its **Events** (deploys, restarts, maintenance and failures).
4. In the Shell, `python3 ops/render/ctl.py status` (the bot's own view: its heartbeat, last decision and any halt), `python3 ops/render/ctl.py events` (its event log, below) and `python3 ops/render/ctl.py check`.

**The bot's event log.** The bot's own warnings, such as a late daily bar, "No loop ran for N minutes" or a decision that failed, go to its event log in the database, not to Render's Logs. On the Mac, that log is the dashboard's Activity list. On Render, in the Shell:

```
python3 ops/render/ctl.py events
python3 ops/render/ctl.py events --limit 100
```

It prints the newest 20 events (or as many as `--limit` says), oldest first, one to an entry: its UTC time, its level (`info`, `warn` or `critical`), its kind and its text. It only reads the database, so it's safe beside the loop. The page points here when today's decision is overdue although the loop is running.

**Render backup's emails.** GitHub emails you when a Render backup run fails, with your notifications set as in 12.2, step 6. Its schedules start hours late, as Paper run's did, so an email can come hours after the cause: look at `/ok` and the page for how things are now. A run fails with one of these:

- **"The bot needs you: ... /ok answered 503: ..."** (its last step, **Check the bot**): the copy was saved, but the bot needs looking at, as the reason says (above). It's expected while the bot is held or halted on purpose.
- **"The bot needs you: ... /ok didn't answer"**: the service may be down. Its Events and Logs on Render say why.
- **"Render backup not saved"** (**Pull the backup**): the service's backup wasn't saved on `paper-state`, for the reason given, which says what to do. Those you may meet: the service didn't answer; its latest backup is over 26 hours old, so its backups have stopped (`backup.error` in `/supervisor.json` says why); its newest check isn't later than GitHub's last, so the backup was taken before the loop's first check on Render (wait for the next one, or take one with `python3 ops/render/ctl.py backup`). One older than the copy already saved, or holding another run, shouldn't happen: have the assistant look.
- **"Couldn't fetch the tag render-seed"** after the cutover, or **"There's no paper-state branch"**: something happened to the tag or the branch on GitHub. Have the assistant look. (During the cutover, before its step 5's tag, the first is expected: 13.3, step 2.)

A run that does nothing, with the notice "Paper run still runs the test", hasn't failed: it's before the cutover, or after going back.

**Render's own emails**, with 13.2's setting: a deploy failed; the service became unhealthy (`/healthz` failed for about a minute, and Render restarted it); the disk is over 80% full; Render suspended the service, after it repeatedly failed to start and become healthy over 24 hours; and notice of maintenance (13.6). None of them knows about the bot: a halted or stopped bot under a healthy supervisor brings no email from Render.

**An outside monitor** (optional, and the quickest alert). Render emails nothing about the bot itself, and Render backup's check comes hours late, so a monitor outside Render that asks for `/ok` every few minutes is the way to hear quickly. Render suggests some, such as Better Stack and HeyOnCall. You make its account, point it at https://duo-paper.onrender.com/ok, and have it alert you when the answer isn't 200. Pause it before a hold you make on purpose (13.6) and at the end of the test (13.8). Never make `/ok` Render's own health check (13.10).

### 13.5 Halting, flattening and resuming

The dashboard's buttons can't be reached on Render, so their commands are typed in the Shell, after `cd /opt/render/project/src`:

| Command | What it does to the run |
|---|---|
| `python3 ops/render/ctl.py halt` | Stops trading at once, as the dashboard's Stop trading does: every working order is cancelled, and nothing new is placed. The checks, the heartbeat and the kill switch carry on. It stays halted until you resume it |
| `python3 ops/render/ctl.py flatten` | Asks you to type `FLATTEN` (or give `--yes`). The bot halts, and at its next loop, within 5 minutes, sells everything at market, no worse than 0.5% from the quote, as Sell everything does. It then stays halted until you resume it |
| `python3 ops/render/ctl.py resume` | Runs `python -m duo resume` on the disk: lifts the kill switch, a halt or a flatten, as in section 5. It's built to run beside the loop, which picks it up at its next check, and the drawdown peak restarts from the account's value then. If nothing is halted, it says so and changes nothing |
| `python3 ops/render/ctl.py status` | Runs `python -m duo status` on the disk: the strategy, the last heartbeat and decision, and any halt. It uses no network |
| `python3 ops/render/ctl.py probe` | Runs `python -m duo probe`: can the service reach Binance and Kraken? It changes nothing |

`halt` and `flatten` go to the bot's dashboard on 127.0.0.1:8787, inside the instance. If they say nothing answers there, the bot isn't running (it's held, waiting or in backoff: `ctl.py check` shows which), and nothing was sent.

While it's halted, the page says "Halted", `/ok` answers 503, and Render backup's runs fail and email you, until you resume. **Before resuming after the kill switch,** take section 5's steps: read why, check it was a real market fall, and keep the evidence first, with the two `curl` lines in 13.4. The dashboard's Activity list, which section 5 has you look at, is `python3 ops/render/ctl.py events` here (13.4). Never use `python -m duo tick --flatten` on Render (13.10).

### 13.6 Holding, deploys and maintenance

**Holding** stops the bot and keeps it stopped, through deploys and restarts, until you take the hold off. In the Shell:

```
python3 ops/render/ctl.py hold
python3 ops/render/ctl.py unhold
```

`hold` creates `/var/data/HOLD`. The supervisor stops the bot (SIGTERM, up to 180 seconds for it to finish its loop, then SIGKILL), and `hold` waits up to 4 minutes for `/supervisor.json` to say `held`. The file is on the disk, so a new instance starts held too. A `hold` typed while the seed runs (13.3, step 6) takes over the HOLD the seed made, so the bot stays stopped after the seed, until `unhold`. `unhold` removes HOLD, and the supervisor starts the bot at once, dropping any wait after a crash. `unhold` then waits up to 4 minutes for the loop's first check, and says "Running: the loop checked at ...". If the bot exits as it starts, `unhold` stops waiting, and says "Not running: the bot exited at once: ..." with the supervisor's reason and the bot's last lines (13.4 says what the reasons mean); after 4 minutes with no check, it says "Not running after 4 minutes: ..." with the supervisor's state. With no HOLD to remove and the bot already running, it says "There was no HOLD, and the bot is running." and stops at once. While it's held, nothing is checked or traded, the page says "Held", `/ok` answers `held` (503), and Render backup's runs fail at their last step and email you. The page and backups carry on. A hold of over 11 minutes is an outage to the loop: when it starts again, it records "No loop ran for N minutes" in its event log (`ctl.py events`, 13.4) and closes any resting order from before the hold unfilled. So hold only when no order is working, and not for longer than you need.

**Deploys** are how a change reaches the service: publish it to `main` (12.2, steps 3 and 4), then, on the service's page on Render, **Manual Deploy**, **Deploy latest commit**. That goes for a change to `config.toml` too, whose first line says to restart the bot after changing it. Because the service has a disk, Render stops the old instance before it starts the new one: the bot gets SIGTERM and at most about 20 seconds to finish its loop, and nothing is checked from then until the new instance is up, usually a few seconds, or a few minutes if Render moves the disk to another machine. So:

- never deploy, restart or change the plan from 00:00 to 00:10 UTC, while the day's decision is made;
- never while an order is working (the day's orders can work until about 02:05 UTC: the page's Recent orders show any);
- if in doubt, deploy outside 00:00-04:00 UTC, and hold first, which gives the loop up to 180 seconds to finish instead of 20. Then deploy, wait until the Logs show `supervisor: starting: ...` with the new commit, open a new Shell and `unhold`;
- afterwards, check that the page says "Running normally" again within two loops, and that `supervisor.commit` in `/supervisor.json` is the new commit.

**Restart service**, under Manual Deploy, starts the same commit again, by the same rules. It keeps the settings of the last deploy, so a change to the service's environment variables needs a deploy: save it with **Save only**, the one choice that doesn't deploy, then deploy at a safe time. **Save and deploy** and **Save, rebuild, and deploy** both deploy at once, so never use either in the decision window.

**Maintenance.** Before maintenance that needs a service with a disk to stop, Render emails a notice and shows it on the dashboard; that's usually no more than once every three months. Render then stops the instance completely before its replacement starts, which usually takes a few minutes. It can usually be rescheduled to within a few days, from the dashboard: move it outside 00:00-04:00 UTC, hold the bot before it starts, and unhold once it's done. Redeploying the service or changing its plan while maintenance is waiting starts the maintenance at once. So before any deploy or change of plan, look on the service's dashboard for waiting maintenance, and if there is some, treat the deploy as that maintenance: outside 00:00-04:00 UTC, with the bot held first, and unheld once the new instance logs `supervisor: starting: ...`. When a machine fails, Render may also move the service to another one without notice, which can take a few minutes; the loop counts that gap like any other.

### 13.7 Backups

**On the service.** The supervisor copies the run's database into `/var/data/backups/`, a folder for each named by its UTC time, with `backups/latest` pointing at the newest, and keeps the newest 8. It takes one:

- at the seed, as soon as `render_seed.json` is there;
- again within about 5 minutes of the loop's first check, because the seed's copy has no check from Render in it, and GitHub keeps only a copy that has;
- every 6 hours after the last;
- at once when asked: `python3 ops/render/ctl.py backup` creates `BACKUP_NOW`, waits up to 3 minutes, and prints the new backup's manifest. Before the seed it says there's nothing to back up yet, and does nothing.

A backup that fails is tried again every 5 minutes, and `BACKUP_NOW` stays until one works. The last good backup stays, and `backup.error` in `/supervisor.json` says why. Backups are taken while the bot is held too.

Each is a consistent copy: SQLite's backup API, from a read-only connection, in one step, then taken out of WAL mode so it's one self-contained file, and checked (`integrity_check`, and the start record the seed's marker names). Beside it go the heartbeat file and `manifest.json`: each file's sha256 and size, when it was taken, the newest heartbeat in it, the seed's marker and the deployed commit. It's made in a staging folder on the disk and renamed into place, so a failure leaves nothing half-made behind. The database was 192,512 bytes on 7 November 2026, so 8 copies use little of the 1 GB disk; Render emails if it passes 80%.

The latest backup is served at https://duo-paper.onrender.com/state/manifest.json, `/state/paper_systematic.db` and `/state/heartbeat_systematic.json` (public, like the page; not found until the first backup).

Render's own daily snapshots of the disk aren't a backup for this: restoring one replaces the whole disk, and Render warns against it for databases (13.10).

**On GitHub.** The **Render backup** workflow, `.github/workflows/render-backup.yml`, copies the latest backup into `data/render/` on the `paper-state` branch. It's scheduled four times a day, at 03:23, 09:23, 15:23 and 21:23 UTC (GitHub starts this account's schedules hours late, and one run a day is enough), and can be run by hand:

```
gh workflow run "Render backup" -R mattthewhiggins/duo-paper
```

Each run:

1. does nothing unless Paper run has been disabled, as the cutover leaves it. Before the cutover, and after going back, Paper run writes `paper-state` itself, so this says so in a notice and passes;
2. fetches the database at the tag `render-seed`, the run the service was seeded with;
3. downloads the service's latest backup and checks it: each file against the manifest, SQLite's integrity check, the same start record as `render-seed`'s database, a newest check later than GitHub's last before the move and no older than the copy saved last time, and taken under 26 hours ago;
4. saves it in `data/render/` on `paper-state`, one commit per UTC day as before: a later run the same day replaces that day's commit, and the cutover day's first copy goes into the fence's commit. Never in `data/` itself, so the fence stays;
5. last, checks the bot: `/ok` must answer 200, or the run fails, and GitHub emails you (13.4).

A run started by hand also puts the moved page on github.io, once its pull has worked, or, with `page: final`, a copy of the service's own page, whatever the pull did (13.8). A scheduled run never touches the page. What its failures mean is in 13.4. The 60 days without activity after which GitHub switches a public repository's schedules off (12.6) can't run out before the test ends.

**Reading a copy on the Mac**, as in 12.7, but from `data/render/`, with the clone at `../duo-paper` checked as 13.3's **Before** says:

```
git -C ../duo-paper fetch origin paper-state
mkdir -p data/render
git -C ../duo-paper show origin/paper-state:data/render/paper_systematic.db > data/render/paper_systematic.db
DUO_DATA_DIR=data/render .venv/bin/python -m duo status
```

or the latest backup straight from the service:

```
curl --create-dirs -o data/render/paper_systematic.db https://duo-paper.onrender.com/state/paper_systematic.db
```

`compare` and `decisions` work on it the same way. It's a copy: nothing done to it reaches the service.

Keep the tag `render-seed`: every Render backup run checks the copy against it.

### 13.8 Ending the test

The 28-day test is complete with the decision made at the first loop from 00:02 UTC on 3 December 2026. The line under the page's title then says "the 28-day test is complete (28 finished days compared), and the loop carries on until it's stopped at the end (RUNBOOK section 13.8)". The loop doesn't stop by itself. Once that day's orders are done, after about 02:30 UTC, end it:

1. Keep the final comparison and decision log, with the two `curl` lines in 13.4.
2. If you want the paper account to end with no holdings, `python3 ops/render/ctl.py flatten` (13.5), and wait for the sells to show on the page. It then stays halted, as expected.
3. Check the backup and its copy work while the bot still runs: `python3 ops/render/ctl.py backup`, then run Render backup by hand (13.7), and wait until its copy is saved. Its last step passes, unless you flattened in step 2: then it fails, because the bot is halted, and that's expected.
4. Hold the bot: `python3 ops/render/ctl.py hold`. The page then says "Finished". Pause the outside monitor, if you have one.
5. Take the final backup, with the run's last check in it: `python3 ops/render/ctl.py backup`.
6. Copy it to GitHub, with the final page:

   ```
   gh workflow run "Render backup" -R mattthewhiggins/duo-paper -f page=final
   ```

   Its backup job saves the final copy in `data/render/`, then fails at its last step because the bot is held, as expected, and GitHub emails you about it. Its pages job runs all the same, and puts a copy of the service's page, which says "Finished", on https://mattthewhiggins.github.io/duo-paper/: its `index.html` and `status.json`, and its `compare.md` and `decisions.jsonl` when the service's page has them. If it lacks one, the run says so in a warning, "Page copied without ...", and still copies the rest; the final database in `data/render/` can make the missing file again with 13.7's commands. Check the page is there, and that `data/render/manifest.json` on `paper-state` was taken after the hold (`git -C ../duo-paper fetch origin paper-state`, then `git -C ../duo-paper show origin/paper-state:data/render/manifest.json`, in the clone checked as 13.3's **Before** says).
7. Disable Render backup, or its runs fail every time once the service is gone:

   ```
   gh workflow disable "Render backup" -R mattthewhiggins/duo-paper
   ```

   A Render backup run that was already waiting, or that GitHub starts late for a slot from before the disable, can still run within about 9 hours. With the service gone it fails (its pull is answered 404, and its bot check finds no `/ok`) and emails you. It saves nothing, and needs nothing.

8. **You delete the service**, from its Settings on Render. That stops its billing, and deletes its disk with everything on it, which is why it comes last. Then check that Render's Billing shows no service still running, and delete the outside monitor, if you have one.

What's kept: `paper-state`'s `data/render/` (the final database, heartbeat and manifest), the tag `render-seed`, and the final page on github.io. Paper run stays disabled.

### 13.9 Going back to GitHub

Going back is for when the run can't carry on on Render: the bot can't start or keeps failing there, or Render is down for long. It works from any point after the cutover's step 5, and the steps are the same each time. Do it outside 00:00-04:00 UTC if you can, with no order working. The assistant's commands use the clone at `../duo-paper`, checked first as 13.3's **Before** says.

1. **Hold the service** (you, in the Shell): `python3 ops/render/ctl.py hold`, whenever the Shell can be reached, even if the bot never ran: a seeded disk starts the bot at the next restart. HOLD is the only thing that keeps a seeded service from starting the bot. If the service starts again without one (after a restart, maintenance, or Render recovering from an outage), the supervisor starts the bot on the disk's copy, and the run carries on in two places, unseen: Render backup is disabled at step 3, and the service's `/ok` answers 200.

   If the Shell can't be reached, stop the service from Render's dashboard before step 4, in one of two ways. Suspend it, which runs nothing until it's resumed and keeps its disk, then check that the dashboard shows it suspended. Render's changelog describes suspending services from the dashboard's list of them (tick the service, then **Suspend**), but its documentation doesn't say exactly where the control is otherwise, or whether a suspended service's disk is still billed. Or delete it, as in 13.8's step 8, which deletes its disk too, with anything the bot did after GitHub's last copy. If the service is ever resumed after going back, hold it in the Shell straight away. If even Render's dashboard can't be reached, the service will start the bot by itself when Render recovers, so suspend or delete it as soon as the dashboard works.
2. **Take a backup, and copy it to GitHub** (you, then the assistant): `python3 ops/render/ctl.py backup` in the Shell, then, on the Mac:

   ```
   gh workflow run "Render backup" -R mattthewhiggins/duo-paper
   gh run list -R mattthewhiggins/duo-paper -w "Render backup" -L 3
   git -C ../duo-paper fetch origin paper-state
   git -C ../duo-paper show origin/paper-state:data/render/manifest.json
   ```

   The run's backup job saves the copy, then fails at its last step because the bot is held, as expected. The manifest's `taken_at_utc` must be after the hold. If the service can't be reached, skip this step: the copy already in `data/render/` goes back, and whatever the bot did after it is lost. If there's no copy in `data/render/` at all (the cutover stopped at its step 6 or before, or the service was lost before its first copy), step 4 takes the database at `render-seed` instead, and whatever the bot did on Render is lost.
3. **Disable Render backup** (the assistant), and wait until none of its runs is left to run, as 13.3's step 3 does for Paper run: disabling doesn't cancel runs that are already waiting.

   ```
   gh workflow disable "Render backup" -R mattthewhiggins/duo-paper
   gh run list -R mattthewhiggins/duo-paper -a -w render-backup.yml -L 20 --json status -q '[.[] | select(.status != "completed")] | length'
   ```

   Run the second line again every minute or so until it prints `0`, and only then go on to step 4. From then on, Render backup doesn't copy again while the files go back. A run GitHub creates late for a slot from before the disable could still put `data/render/` back beside the run before step 5 enables Paper run. That's harmless, since Paper run ticks from `data/`, and it can be left.

4. **Put the run back in `data/` on `paper-state`** (the assistant), in place of the fence's note:

   ```
   git -C ../duo-paper fetch origin paper-state
   TIP=$(git -C ../duo-paper rev-parse origin/paper-state)
   git -C ../duo-paper worktree add --detach ../duo-paper-back "$TIP"
   git -C ../duo-paper-back mv data/render/paper_systematic.db data/paper_systematic.db
   git -C ../duo-paper-back mv data/render/heartbeat_systematic.json data/heartbeat_systematic.json
   git -C ../duo-paper-back rm -q -r data/render data/MOVED_TO_RENDER.md
   git -C ../duo-paper-back commit -m "The paper run is back on GitHub Actions"
   git -C ../duo-paper-back log -1 --format='%an <%ae>'
   ```

   With no copy in `data/render/`, the two `mv` lines and the `rm` line are these instead:

   ```
   git -C ../duo-paper fetch origin refs/tags/render-seed:refs/tags/render-seed
   git -C ../duo-paper-back checkout render-seed -- data/paper_systematic.db data/heartbeat_systematic.json
   git -C ../duo-paper-back rm -q data/MOVED_TO_RENDER.md
   ```

   Both remove the note, `data/MOVED_TO_RENDER.md`: while it's on `paper-state`, every Paper run but a `probe` stops at **Restore the paper state** with "The paper run is on Render", so removing it is what lets Paper run run again. The `log` line must show your GitHub no-reply address, as in 13.3's step 5; if it shows another, set the address as 12.2's step 2 says, then `git -C ../duo-paper-back commit --amend --reset-author --no-edit`, and look again. Then push it, and tidy up:

   ```
   git -C ../duo-paper-back push --force-with-lease=refs/heads/paper-state:"$TIP" origin HEAD:refs/heads/paper-state
   git -C ../duo-paper worktree remove ../duo-paper-back
   ```

   Success: the push is accepted. If it's refused, something saved in between: remove the worktree (`git -C ../duo-paper worktree remove --force ../duo-paper-back`) and do this step again.
5. **Enable Paper run** (the assistant):

   ```
   gh workflow enable "Paper run" -R mattthewhiggins/duo-paper
   ```

   If GitHub still starts this account's scheduled runs hours late, start the stopgap on the Mac again, as 13.3's step 1 says.

6. **Start a tick** (the assistant), and wait for it:

   ```
   gh workflow run "Paper run" -R mattthewhiggins/duo-paper -f action=tick
   gh run list -R mattthewhiggins/duo-paper -w "Paper run" -L 3
   ```

   Never `start` here: on a `paper-state` with no database it would begin a new paper run (12.3). The tick carries the run on from where it was left. A gap of over 3 hours since its last check counts as an outage (12.8). The run's deploy job puts the status page back on github.io, in place of the moved page.
7. **Check the page**, https://mattthewhiggins.github.io/duo-paper/: the same strategy and start date, "Running normally" (or "Needs attention" with a reason that clears at a later run), and Run health showing GitHub's schedule again. From here, section 12 applies.

   For a while, Run health counts the loop's checks on Render as runs: GitHub builds the page in its own mode, where every heartbeat is a run, so there are up to 288 more for each day the run spent on Render. "Runs in the last 24 hours" is high until a day after the hold, "Runs in the last 7 days" until a week after it, and "Runs since the start" keeps them for good. "(including any started by hand)" then doesn't mean any run was started by hand, and nothing else is starting Paper run: the Actions tab lists GitHub's real runs. Nothing else on the page is affected.
8. **You delete the service** on Render, as in 13.8, step 8, once the page is right. Until then, the hold, or the suspension, keeps the bot stopped.

Trying the move again later is 13.3 from the start, with its **Before**'s notes for a second cutover.

### 13.10 Never on Render

- **Never run a bot by hand:** no `python -m duo run`, `python -m duo tick`, `tick --flatten` or `tick --first-run` in the Shell. The supervisor's child is the only bot. Another would be a second writer to the database, `run` starts a new paper run wherever it finds none, and a tick moves the marker the page uses to tell GitHub's checks from the loop's. Halt and flatten with `ctl.py` (13.5).
- **Never `python -m duo site`:** it opens the database writable and checkpoints it as it closes. The supervisor builds the page with `python -m duo.site`, which only reads.
- **Never a second `serve.py`,** in the Shell or anywhere else.
- **Never `python -m duo reset-paper`, and never move, change or delete the run's files on `/var/data` by hand** (the database and its `-wal` and `-shm`, the heartbeat, `render_seed.json`, `backups/`). `HOLD` and `BACKUP_NOW` are made through `ctl.py`. Open the database with `sqlite3` only as `sqlite3 -readonly`.
- **Never publish a `config.toml` with the runner off** (`[systematic] enabled = false`), or with another strategy or `[portfolio] quote`. With the runner off, `python -m duo run` would run the agents' paper engine on `paper.db` instead, so the supervisor won't start the bot, and its reason says so (13.4); with another strategy or quote it won't start either.
- **Never add a Secret File** to the service, and above all not one named `.env`: Render puts Secret Files in the repository's folder, where every `python -m duo` command reads `.env`. The service needs no secrets.
- **Never set `PYTHON_VERSION`:** it overrides `.python-version` (13.2).
- **Never make `/ok` the Health Check Path:** Render would restart the instance whenever the bot is halted, held or degraded. Keep `/healthz`.
- **Never use maintenance mode:** Render then answers every request with 503 while the bot carries on, so the page, `/ok` and the backup copy all fail, and nothing is stopped. Hold instead (13.6).
- **Never deploy, restart or change the plan from 00:00 to 00:10 UTC,** while the day's decision is made, or while an order is working (13.6).
- **Never restore one of Render's snapshots of the disk:** it replaces the whole disk, and Render warns against it for databases. Going back is 13.9.
- **Never use `render ssh --ephemeral` or a one-off job** for these commands: they run on a separate instance, without the disk.
- **Never run Paper run's `start` after the cutover, and keep Paper run disabled** (`gh workflow disable "Paper run" -R mattthewhiggins/duo-paper`, if it's enabled). While the note `data/MOVED_TO_RENDER.md` is on `paper-state`, `start` is refused with "The paper run is on Render", as every action but `probe` is, and once the note is gone, with no database in `data/`, it would begin a new paper run on GitHub (12.3). Going back is 13.9.

How this was checked when it was written (7 November 2026): nothing in this section has run on Render yet. The service doesn't exist until 13.2 is done, so every fact about Render here comes from Render's documentation, and the checks in 13.2 are where the ones that matter are confirmed: that `/var/data` is a mount, how the start command is run and whether SIGTERM reaches it, what the Shell sees, and which SQLite the service's Python has. On the Mac, the supervisor, the Shell's controls and the backup pull are tested against local stand-ins (`tests/test_render_serve.py`, `tests/test_render_ctl.py` and `tests/test_render_backup.py`), and the page's Render mode in `tests/test_site.py`. They were also run together from a fresh publish of the code, with a copy of `paper-state`'s database, a stand-in for the bot, the watchdog and GitHub, and SQLite 3.53.3, built as on Linux, because the Mac's own SQLite refuses the read-only open the supervisor relies on: the seed, the supervisor starting the bot, backups, the pull and both pages on GitHub's side, hold, unhold, and shutting down in under a second. The real `python -m duo run` hasn't run under the supervisor. The git commands in 13.3 and 13.9 were rehearsed against a local repository standing in for GitHub, including a fence refused by its lease and then pushed on the new tip, a fence committed with another address and amended, and a second cutover after going back, which moved the tag `render-seed` and kept the first as `render-seed-1`. The read-only ones in 13.3 (`git ls-remote`, the tree, the count of runs left, `--created`, and the stopgap's command line) were run against the real repository and the Mac, as was step 4's check of the database, on a copy of it. Not run: anything that writes to GitHub (disabling or enabling a workflow, starting a run, the pushes) and anything typed in Render's Shell.
