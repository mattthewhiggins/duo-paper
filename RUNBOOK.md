# Paper-trading runbook

**The holdout verdict: no spot strategy passed the gate.** The holdout was unlocked once, at 2026-11-05T03:42:38Z, and the gate was applied as preregistered and not loosened (preregistration section 12: "If nothing passes, the answer is that nothing passes"). The one strategy that passed, on both cost tracks, is `F3__BTC_ETH__D30__E5`, a funding-rate carry trade on Bitcoin and Ether. It's ruled out: it needs perpetual futures, which firms may not sell to UK retail clients (FCA COBS 22.6, research/VENUES.md), the backtest doesn't model liquidation, and its holdout return was small, 8.2% in USD and 4.8% in the GBP view over the 837 days from 2024-07-17 (track K), so this runner refuses it. Every spot strategy failed: the trend strategy chosen on development, `F1__sma_50__BTC__vol20`, failed one gate condition (G2) on both tracks, and both preregistered fallbacks failed several, FALLBACK_TREND (`F1__ENS__BTC_ETH__vol40`, -1.3% on the holdout) and FALLBACK_DCA (-13.4%). The AI agents added nothing in any role tested: of 7 layer-model pairs, one passed on development (`LLM__L0__haiku__F1__sma_50__BTC__vol20`), and on the holdout its Sharpe was -0.17 against its base's 0.39, so it didn't stay. What this runbook paper trades is therefore a fallback that **did not pass the gate**, `F1__sma_50__BTC__vol20` (section 1 says why), for paper trading only; not trading at all is an equally reasonable choice, and nothing tested makes a profit every day. Figures are after costs, in USD unless the GBP view is named, from `research/artifacts/holdout_results.md` and `holdout_results.json` (the agents' from `research/artifacts/llm_dev.json` and the LLM section of `holdout_results.md`), research files that are kept privately and aren't in the public repository. This isn't financial advice, and nothing goes live without your explicit decision.

This is the day-to-day guide to running Duo's **systematic runner** on paper: a strategy from the research, decided once a day by code, traded against live Kraken prices with simulated fills. No real money is involved at any point, and nothing here places a real order. To run it on GitHub Actions instead, so the laptop can be shut down, see section 12.

It assumes no trading or programming background. Every command is typed in the Mac's **Terminal** app, from inside the project folder. To get there, open Terminal, type `cd ` (with a space after it), drag the project folder from Finder into the Terminal window, and press Return.

This isn't financial advice. Whoever wrote the research and this runbook isn't a licensed adviser. The research's gate decides which strategies pass, and whether to trade with real money is your decision alone.

The places that could only be decided by the research's one-time **holdout verdict** are now filled in from `research/artifacts/holdout_results.json`, `holdout_results.md` and `selection_K.json` (development). Nothing else is left open. Those files, like the other research write-ups and results this runbook cites (`RESULTS.md`, `research/VENUES.md` and the result files under `research/artifacts/`), are kept privately: they're in your working copy on the Mac, not in the public repository.

---

## 1. What the paper run is for

The research built a backtest: a simulation of how a strategy would have done on past prices, with an assumed cost for every trade. A backtest can be wrong in ways that only show up when the strategy runs forward in time against a real order book. The paper run checks for that.

- **It runs for at least 4 weeks.** Preregistration section 14: "Paper trading lasts at least 4 weeks and is compared with backtest expectations." In practice that means at least 28 *finished days* in the weekly comparison (section 6), so allow about 30 calendar days from the first start, more if the bot was down (the Mac off, or GitHub's runs stopped) for any of it. On GitHub Actions the status page counts them, and the runs carry on until you end the test (section 12.6).
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

**The bot's own tests don't depend on this setting.** They set the limits they test themselves (`TEST_LIMITS` in `tests/sys_helpers.py`), so the two `CappedTargets` tests that `duo/AUDIT_STATUS.md` (kept privately, with the audit) cites still check a 50% cap whatever `config.toml` says. Run on 5 November 2026 with the shipped `max_asset_pct = 100`, the full suite (README, "Tests") ran 418 tests: OK.

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

### When it trades

The research uses daily price bars that close at **00:00 UTC**, which is midnight in the UK in winter (GMT) and 01:00 in summer (BST). The bot checks things every 5 minutes. It makes the day's one decision at the first check from **00:02 UTC** at which the just-closed bar is available. Each order starts as a resting order at the best price, is moved to the new best price every 15 minutes, and after 120 minutes crosses the spread for whatever is left, at no worse than 0.5% from the quoted price. So on a normal day, everything has finished by about 02:05 UTC. On many days nothing trades at all, because a holding only changes when its target moves by more than the research's band.

---

## 4. Keeping it running

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
| **Stale data** (`signal_stale_minutes = 30`, `max_api_errors = 5`) | Yesterday's daily bar still missing 30 minutes after 00:00 UTC, or 5 failed requests to Binance or Kraken in a row | No new orders until it clears, which it does by itself | Check the Mac's internet (on GitHub Actions: run the workflow with action `probe`, section 12.3). If it lasts more than 30 minutes the watchdog tells you. See section 8 |
| **Quote fresh** (`max_quote_age_seconds = 90`, `max_frozen_quote_seconds = 600`) | A coin's Kraken price is over 90 seconds old, hasn't changed at all for 10 minutes, or makes no sense | No new orders in that coin, and paper orders don't fill on it, until it clears | Usually nothing. If it lasts, check the internet (on GitHub Actions, run `probe`) and whether Kraken is having problems |
| **Asset limit** (`max_asset_pct = 100` for the Bitcoin-only strategy, see section 2; `asset_margin_pct = 2`) | A buy would take one coin above the strategy's largest holding plus 2 points, and never above `max_asset_pct` | Cuts the buy down, or refuses it | Nothing on a single day. If it trips on most decisions, the strategy needs the setting in section 2 |
| **Gross exposure** (`max_gross_pct = 100`) | Holdings would pass 100% of the account, a sell is bigger than what's held, or a buy is bigger than the cash | Refuses the order. No borrowing and no short selling, ever | This shouldn't happen in normal running. Export the decision log (section 7) and have it looked at |
| **Order cap** (`order_margin_pct = 2`) | A buy bigger than its target change plus 2% of the account, or a sell bigger than its order set out to sell | Refuses the order | As for gross exposure: export the log and have it looked at |

The dashboard's two buttons also halt it. **Stop trading** cancels every order and places nothing new until you resume; the kill switch stays armed underneath. **Sell everything** does the same and sells every holding. Both make paper differ from the backtest, which `compare` will then show, so use them only if you mean to.

### Resuming after the kill switch

The kill switch firing is itself an important result: a 45% fall from the peak is close to the research's own 50% limit (gate condition G3). On GitHub Actions, section 12.5 says how to take these steps there: the dashboard and the commands below only reach the Mac's own paper run. Before resuming:

1. Read why: the dashboard shows "Halted: *reason*", and `.venv/bin/python -m duo status` shows the `HALTED` line.
2. Check it was a real market fall, not a problem with the Mac or its connection: look at the Activity list and the prices.
3. Export the decision log and run `compare` (sections 6 and 7), and keep both files, so the moment is recorded before anything changes.
4. Decide whether the paper run should carry on. If it does, resume.

To resume, either press **Resume** on the halt message in the dashboard's Systematic runner panel, or run the command below. After you used **Stop trading** yourself, use the header's **Resume trading** button.

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

Do this weekly with `compare`, before resuming from the kill switch, and at the end of the run. The dashboard's **Download CSV** link, under Trades, gives a simpler spreadsheet of fills (time, coin, side, quantity, price, value, fee). The bot doesn't do UK tax matching, so keep these files.

---

## 8. When the Mac sleeps, loses power or loses the internet

**If the Mac sleeps** (lid closed, or on battery): nothing is checked or traded, and nothing fills on paper. When it wakes, the bot records the gap, closes any resting order from before it as unfilled, and carries on. If yesterday's bar hasn't been decided yet, it decides at the first check, whatever the time of day. If the Mac slept through a whole UTC day, that day's decision is skipped. `compare` then holds the previous target for that day, as paper did, and counts the day in its notes. For the DCA fallback, a weekly purchase whose day was missed is bought late at the next decision.

**If it loses power or restarts:** the bot stops. When the Mac is back, log in, check it's plugged in, and start the bot again with `./start.sh`. If you installed the bot's launchd agent, it starts by itself once you log in. Then run `.venv/bin/python -m duo status` to check the heartbeat is fresh and the last decision is yesterday's.

**If the internet drops:** the bot keeps running and its heartbeat keeps going, but it can't get prices. It shows as degraded ("API errors in a row" or "Kraken quote missing, old or frozen") and places no new orders. Resting orders don't fill on stale prices. After 30 minutes like this, the watchdog notifies you, and again if the day's decision is overdue after 00:30 UTC. When the connection comes back, it clears by itself and makes any decision it's owed at the next check. You don't need to restart it.

In every case: don't try to make up for missed days by hand. Note what happened and when, and let `compare` show it.

---

## 9. Costs

- **API spend: none.** `agents_enabled = false` in `[systematic]`, so the Claude agents are off (the start-up message says `Agents: off.`), and no Anthropic API calls are made. Leave it off. Even switched on, the agents can never trade in this mode; they'd only cost money.
- **Market data: free.** The runner reads only public Binance and Kraken price data, with no account and no key.
- **Trading fees: none in reality.** The fees on paper (0.22% maker, 0.38% taker) are simulated, so the comparison is fair. No money moves.
- **What it does cost:** the Mac staying on, plugged in and awake for the 4 or more weeks.

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
| `.venv/bin/python -m duo resume` | Lifts the kill switch, Stop trading or Sell everything, in the Mac's own paper run (for GitHub's, see section 12.3) | Resumes trading |
| `.venv/bin/python -m duo tick` | One poll, then exits: what each GitHub Actions run does (section 12). With no paper run it does nothing and exits 3, unless given `--first-run`; `--flatten` sells everything and halts | Polls and trades on paper |
| `.venv/bin/python -m duo probe` | Can this machine reach Kraken's and Binance's public prices? | No |
| `.venv/bin/python -m duo site --out DIR` | Writes the status page GitHub Pages shows into DIR, from the database | Writes that folder |
| `pmset -g batt` | Mains or battery? | No |
| `pmset -g assertions \| grep -i caffeinate` | Is some caffeinated program keeping the Mac awake? (Match its pid with `pgrep -lf 'duo run'`) | No |
| `launchctl list \| grep com.duo` | Which Duo launchd agents are loaded | No |

How these were checked when this runbook was written (5 November 2026): `status`, `watchdog`, `compare` and `decisions` (with and without `--out`) were run against a running paper preview, and with `resume` and the move-aside commands against a copy of its database in a separate folder, including a simulated kill switch. The strategy refusals quoted in section 2 were produced by the runner's own start-up checks on temporary copies of `config.toml`. The launchd templates were filled in with `sed` and checked with `plutil -lint` in a temporary folder, and `pmset` and `launchctl list` were run. These were not run, because each would have started a second bot beside the preview, installed or removed a background service on this Mac, or read a log that only exists once a service is installed: `./start.sh` itself; `mkdir -p ~/Library/LaunchAgents`; the `sed ... > ~/Library/LaunchAgents/...` copies and `plutil -lint` on the copies there; the `launchctl bootstrap` and `launchctl bootout` lines; the `rm ~/Library/LaunchAgents/...` undo lines; `tail -n 20 data/watchdog.log` and `tail -n 50 data/bot.log`. They're the commands README gives, and you run them yourself. The move-aside command in section 2 was run in a scratch folder, including a second time with the same folder name, where it stopped with `File exists` and moved nothing. `pgrep -lf 'duo run'` and `pmset -g assertions` were run beside the preview.

---

## 12. Running it on GitHub Actions, with the laptop off

The paper run can also run on GitHub's machines, so the Mac doesn't have to stay on. This section is the guide for that. Apart from the one-time setup in 12.2, everything happens on github.com, in your web browser.

### 12.1 How it works

- **GitHub Actions runs the bot, and GitHub Pages shows the page.** Pages can only host static files, so it can't run anything itself. Actions starts a fresh machine on a schedule, and each run does the same five things: it restores the paper run's state, runs one poll of the runner (`python -m duo tick`, stopped if it's still going after 270 seconds), saves the state, rebuilds the status page and publishes it to Pages. The instructions for this are the workflow file `.github/workflows/paper.yml`, which GitHub shows as **Paper run**.
- **The repository** is `mattthewhiggins/duo-paper`, and it's public. So is the page, **https://mattthewhiggins.github.io/duo-paper/**: anyone with the link can see the paper trades and the paper account's value. It's all paper, and nothing in the repository holds a key or a password.
- **The schedule**, in UTC (add an hour for UK summer time):

  | When | What for |
  |---|---|
  | Every 15 minutes from 00:05 to 03:50 | The day's decision, then its orders: moved to the best price every 15 minutes, and crossing the spread after 2 hours |
  | Hourly at :35, from 04:35 to 23:35 | The risk checks, the heartbeat and the page |
  | 23:47 | So the next day's loss limit starts from a reading taken just before midnight |

  That's 37 short runs a day. GitHub starts scheduled runs late when it's busy, often by 5 to 60 minutes, and now and then skips one. That's normal, and the runner allows for it.
- **The state** is the `data/` folder: the database `paper_systematic.db` and the heartbeat file. It's kept on a branch of its own, `paper-state`. Each run takes it from there and saves it back when it's done. The branch keeps one commit (one saved version) per UTC day: later runs on the same day replace that day's commit, so each commit is how the run stood at the end of a day. The code is on `main`, and runs never change it.
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

   It copies only what the paper run and its tests need: the code, the tests, `config.toml`, the research cost model, README and this runbook. Not `data/`, not `.env`, and not `RESULTS.md`, `AUDIT.md` or the research write-ups and results, which stay private. It checks every file first. It stops, says which file (and line), and changes nothing if a file is called `.env` or isn't UTF-8 text, if a file on its list is a link, or if a file holds a path under /Users (also written with dashes for slashes, as in the name of a Claude session folder), an email address other than a GitHub no-reply or example one, or something shaped like a key (an Anthropic, GitHub or AWS key, a private key, or one of this project's key settings with a value). It checks for nothing else, so the look in step 4 matters. It never commits or pushes.
4. Look over what it copied, then commit and push it:

   ```
   git -C ../duo-paper status
   git -C ../duo-paper add -A
   git -C ../duo-paper commit -m "Paper test on GitHub Actions"
   git -C ../duo-paper branch -M main
   git -C ../duo-paper push -u origin main
   ```

   If git refuses the push because its login lacks the `workflow` scope, which adding `.github/workflows/paper.yml` needs, run `gh auth setup-git`, so that git uses the login of GitHub's command-line tool, and push again. If that's refused too, `gh auth refresh -s workflow` adds the scope to that login.
5. On github.com, in the repository: **Settings**, **Pages**, and under Build and deployment set **Source** to **GitHub Actions**. Without it the page can't be published. Nothing else in the repository needs setting: the workflow asks for the permissions it needs itself, and uses no secrets.
6. Make sure GitHub emails you when a run fails, because that's the only alert: on github.com, your profile picture, **Settings**, **Notifications**, then under **System**, **Actions**: turn on **Email**, with **Only notify for failed workflows**.
7. On the Actions tab, **Paper run** should be listed. If GitHub says "Invalid workflow file", it hasn't accepted `.github/workflows/paper.yml`, and nothing runs until that's fixed: the message gives the line. Then check that GitHub's machines can reach the prices: run the workflow with `probe` (12.3 says how). It reads Binance's and Kraken's public prices and changes nothing. Its log lists each source, and ends with "The runner can poll from here."
8. Start the paper run: run the workflow with `start`. **Once.**

To publish a later change to the code or these documents, run step 3 again, then step 4 without the `branch -M` line. A change to the workflow file applies from the next run. A new numpy version in `requirements-paper.txt` needs its file's hash there too (the comment there says which file), or the Install step refuses it.

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

A run you start waits while another run is going. Waiting runs go in the order they came, and none is cancelled while it waits (GitHub keeps up to 100 waiting), so a `flatten` or `resume` you start still runs if scheduled runs come due meanwhile. To run an action again, start a new run: not Re-run jobs (12.2 says why).

Until `start` has run, scheduled runs do nothing and say "Paper run not started" in their summary. They don't fail, and don't email. If `start` fails, or its state can't be saved, nothing is kept, so the paper run hasn't begun: the run fails and says "The start failed" or "The start wasn't saved", and you run `start` again. That's so on a `paper-state` whose database is gone, too (12.5).

### 12.4 The daily check (about 2 minutes)

Do it every day, any time after about 03:00 UTC, when the day's decision and its orders should be done (later if GitHub was slow). It's also the only way to notice that runs have stopped, since that sends no email (12.5).

1. **Open the page**, https://mattthewhiggins.github.io/duo-paper/. The line under its title says how far the test has got ("day N, and M of 28 finished days compared"), and the box below that how the run is:
   - **Running normally**: the last run, and when. Every check passed.
   - **Needs attention**: the latest run's tick didn't finish, today's decision is overdue (still none by 02:00 UTC, although runs are happening), or at the last run the runner wasn't placing new orders, for the reasons it gives. Most of these clear by themselves at a later run.
   - **Halted**: the kill switch or a flatten. Nothing new trades until you resume it (12.5 says how, and what to check first).
   - **Stale**: no run has updated the page for about 3¼ hours (195 minutes), or runs update it but none has checked the market for that long. Look at the Actions tab (12.5).
   - **Finished**: the 28-day test is complete, and no run has updated the page since, which is what disabling the workflow at the end does (12.6).

   Below the box are the paper account's value against just holding Bitcoin, what's held, the strategy's latest decision ("The strategy today (paper)" once today's is made), the latest decisions, orders and fills, paper against the backtest, and **Run health**: the last and next runs, whether today's decision is made, and the runs in the last 24 hours and 7 days against the scheduled slots. Runs started by hand count as runs too, so fewer runs than slots usually means GitHub skipped some, or some ticks failed.
2. **The Actions tab** lists every run, newest first. A green tick is fine; a red cross failed, and GitHub has emailed you (12.5). Click a run, and its summary says what that tick did: its status, any decision, any fills and any halt.
3. Look for what section 5 says to look for: one decision for yesterday's bar, holdings close to their targets once the day's orders are done, and nothing still working hours after the decision.

The weekly comparison (section 6) and the decision log (section 7) are published with the page and kept up to date, at https://mattthewhiggins.github.io/duo-paper/compare.md and https://mattthewhiggins.github.io/duo-paper/decisions.jsonl. To keep each week's copy, as sections 6 and 7 suggest:

```
curl -o "data/compare_actions_$(date +%F).md" https://mattthewhiggins.github.io/duo-paper/compare.md
curl -o "data/decisions_actions_$(date +%F).jsonl" https://mattthewhiggins.github.io/duo-paper/decisions.jsonl
```

### 12.5 When GitHub emails you

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

Three messages say what to do next. "No paper state" means the tick found no database on `paper-state`: a new paper run never starts by itself, so nothing ran. If the run's database really is gone, `start` begins a new run there. "The start failed" and "The start wasn't saved" mean the start was thrown away, so the paper run hasn't begun: run `start` again. Until you do, later runs only say "Paper run not started", without failing, or fail with "No paper state" where `paper-state` already existed.

### 12.6 Stopping it

**The runs don't stop by themselves.** When the line under the page's title says "the 28-day test is complete (28 finished days compared)", the test has reached its end, and these steps end it:

1. Keep the final comparison and decision log, with the two `curl` lines in 12.4.
2. If you want the paper account to end with no holdings, run `flatten` and wait for that run to finish. It fails and emails you, because the runner is then halted: that's expected. Disabling alone sells nothing.
3. Disable the workflow: Actions tab, **Paper run**, the **...** menu at the top right, **Disable workflow**. Or in Terminal: `gh workflow disable "Paper run" -R mattthewhiggins/duo-paper`.

Step 3 also stops the runs at any other time. Nothing runs after that, so nothing is checked or traded. The page keeps showing the last run, and once no run has updated it for about 3¼ hours it says "Finished" if the 28-day test was complete, or "Stale: not updated lately" if it wasn't. **Enable workflow**, in the same place, carries on from the saved state.

GitHub also turns off a public repository's schedules by itself after 60 days with no activity in the repository, and emails a warning first. If that happens, enable the workflow again.

### 12.7 Where the state is, and reading it on the Mac

The database is `data/paper_systematic.db` on the `paper-state` branch. On github.com, switch the branch menu from `main` to `paper-state` to see it, and its commits list one per day. To read it on the Mac with the usual commands (sections 6 and 7), copy it out:

```
git -C ../duo-paper fetch origin paper-state
mkdir -p data/actions
git -C ../duo-paper show origin/paper-state:data/paper_systematic.db > data/actions/paper_systematic.db
DUO_DATA_DIR=data/actions .venv/bin/python -m duo status
```

`compare` and `decisions` work the same way. It's a copy: nothing done to it reaches GitHub. The Mac's own `data/paper_systematic.db` is a separate paper run, and `./start.sh` carries on that one, not GitHub's. Don't copy a database from the Mac onto `paper-state`: the branch is public, and a database the Mac wrote can hold the Mac's folder paths in the errors it logged.

### 12.8 How it differs from the laptop

- **The decision** is made at the first run after 00:02 UTC that sees the just-closed daily bar: normally the 00:05 run, but GitHub often starts it later, and a skipped run moves it to the next. The bar and the targets are the same as on the laptop; only the time the orders start moves. The overdue alert waits until 2 hours after 00:00 UTC.
- **Resting orders** can't be watched between runs, so each run judges a resting paper order on Kraken's 1-minute candles since it was placed, by the same paper rule (it fills at its price only if the market traded 0.02% through it), and books the fill at the minute it happened. The laptop checks a fresh quote every 10 seconds instead. If a run can't get those candles from Kraken, a resting order that's due to be re-priced or to cross is left as it is for the next run to judge, as long as it's within its 60-minute life on the book. So while the candles can't be had, re-pricing and the switch to taker can come up to about an hour late.
- **Outages:** only a gap of more than 3 hours since the last run counts as one (`cron_gap_minutes` in `config.toml`). The run logs it, and each resting order from before the gap is still judged on the candles over its life, closing unfilled if nothing traded through it. (On the laptop, a gap closes them unfilled without that check.)
- **Risk checks** (the kill switch, the daily loss limit, stale data) run once per run, every 15 to 60 minutes, rather than every 5 minutes, so a fall between runs is acted on at the next run. "5 API errors in a row" comes after 3 to 5 failed runs, because one failed run can count twice: Kraken on the morning of a decision (the poll and the decision each ask it), and Binance before the day's bar is seen.
- **Binance's market-data host:** daily bars come from data-api.binance.vision, which serves the same klines as api.binance.com. api.binance.com refuses US addresses (HTTP 451), and GitHub's machines are mostly in the US, so it's only the fallback. Each decision records which host it used.
- **No dashboard.** The page is read-only. `resume` and `flatten` stand in for the dashboard's Resume and Sell everything. There's no Stop trading: disabling the workflow stops everything, checks included. The Mac's dashboard and its `duo` commands work on the Mac's own `data/`, never on GitHub's run (12.5, 12.7).
- **The watchdog** runs at the end of every run, and its alert is GitHub's failure email instead of a notification on the Mac. Unlike the Mac's, it can't tell you when runs stop happening (12.5).

### 12.9 Costs, and GitHub's terms

- **Cost: none.** Actions on GitHub's standard machines, and Pages, are free for public repositories.
- **GitHub's terms** allow Actions to be used for developing and testing the software project in the repository. This paper run is a time-boxed test of this project, stopped at its end by disabling the workflow (12.6), so it fits. A live trading bot isn't a test: it would need its own server (README, "Moving to a server"), as well as everything in section 10.
- **No real orders, and no keys.** The workflow has no secrets, it reads only public prices from Binance and Kraken, and the runner refuses live mode in code.

How this was checked when it was written (5 November 2026): the scripts that restore and save `paper-state`, and `publish_repo.py`, are tested on the Mac against a stand-in for GitHub (`tests/test_actions_scripts.py`), and the workflow file was checked as YAML. `tests/test_workflow.py` checks that every command the workflow runs is one the bot takes, and that its exit codes, folders and schedule match the code and the page. Then the workflow's steps were rehearsed on the Mac as GitHub would run them, with stand-ins for Kraken and Binance and a folder standing in for GitHub: what `publish_repo.py` copied was committed as `main`, and each run was a fresh copy of it and new processes (restore, tick, save, page). There was a `start` at 00:07 UTC, then 95 scheduled runs over the next 2 days and 10 hours, each started up to 45 minutes late, with a few hourly ones skipped and one run with Kraken down, then a `tick` by hand. Every run was saved and its page built; the three decisions, the maker buy, the re-priced sell that crossed as a taker after 2 hours and the page's figures came out as expected, and `paper-state` held one commit per day. `flatten`, `resume`, and a scheduled run before `start` were rehearsed the same way. Three later changes were checked on the Mac only: the Tick step's 270-second limit with a stand-in for Linux's `timeout`, the save's check for a database still open with a folder standing in for Linux's `/proc`, and the order runs wait in (`queue: max`) against GitHub's published schema for workflow files. On GitHub itself, the first `probe` (5 November 2026, before those three changes) reached Kraken and Binance's data-api.binance.vision, while api.binance.com refused with HTTP 451, as expected. The first `start` is the first real test of the rest.
