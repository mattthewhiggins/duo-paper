"""The review's findings against the systematic runner, one test (or more) each. Each finding's tests fail with its fix
undone (checked by undoing each fix alone in a scratch copy). Two tests pin down what must still hold and would pass
either way: CompareExits.test_compare_prices_an_exit_at_the_next_open_not_as_a_delisting (compare's arithmetic on the
log the runner now writes) and Outages.test_a_short_wobble_is_not_notified (no alert for a few minutes' outage).

No network: Binance and Kraken are the stand-ins in tests/sys_helpers.py, or urlopen patched to a canned reply.
"""
import http.client
import io
import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from duo import compare as compare_module
from duo import sys_data, sys_research
from duo.brokers import PaperBroker
from duo.db import DB
from duo.kraken import KrakenClient, KrakenNetworkError
from duo.market import Quote
from duo.sys_market import SystematicMarket
from duo.sys_risk import CONTROLS, Child, Controls, Halts
from duo.systematic import SCHEMA, SystematicRunner, export_decisions, utc
from duo.watchdog import check as watchdog_check

from .sys_helpers import D0, DAY, make_env, make_sys_config, run_until, series, step
from .test_systematic import book_for, children, decisions, fill_all_makers, fills


def holding():
    """BTC and ETH bought at the first decision, as KillSwitch's setUp does."""
    env = make_env()
    run_until(env, D0 + 60, D0 + 300, every=60)
    fill_all_makers(env, D0 + 400)
    return env


def held(env):
    return {a: p.qty for a, p in env.runner.portfolio().positions.items() if p.qty > 0}


def restart(env):
    """A new runner on the same database, as after a crash or a launchd restart."""
    env.runner = SystematicRunner(env.config, env.db, env.market, PaperBroker(env.config), env.binance, env.strategy,
                                  env.start)
    return env.runner


AC = lambda *a, **k: SimpleNamespace(stdout="Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged\n")


class KillSwitchFlatten(unittest.TestCase):
    def test_flatten_with_a_holding_unpriced_still_sells_everything(self):
        # kill-flatten-none-target-change: Sell everything on a tick when ETH had no Kraken quote.
        env = holding()
        now = D0 + 600
        eth = env.market.prices.pop("ETH")
        env.market.now = env.binance.now = now
        env.market.refresh()
        self.assertNotIn("ETH", env.market.quotes)
        env.runner.stop_trading(now, "sell everything was requested from the dashboard", flatten=True)
        env.runner.on_tick(now)
        env.market.prices["ETH"] = eth
        run_until(env, now + 60, now + 3600, every=60)
        self.assertEqual(held(env), {})
        self.assertEqual({f["asset"] for f in fills(env) if f["side"] == "sell"}, {"BTC", "ETH"})
        self.assertFalse([c for c in children(env, status="blocked") if "order_cap" in (c["note"] or "")])
        for order in env.db.all("SELECT target_change FROM sys_orders WHERE source = 'kill_switch'"):
            self.assertNotEqual(order["target_change"], "None")

    def test_drawdown_kill_still_flattens_after_a_manual_pause(self):
        # manual-stop-suppresses-drawdown-kill
        env = holding()
        env.runner.stop_trading(D0 + 500, "stopped from the dashboard", flatten=False)
        # Holdings are about 69% of equity, so a 70% fall is about 48% of equity: past the 45% limit.
        run_until(env, D0 + 600, D0 + 3600, every=60, prices={a: p * 0.3 for a, p in env.market.prices.items()})
        self.assertEqual(held(env), {})
        self.assertIn("45% limit", env.db.get("sys_kill_reason"))
        self.assertIn("already paused: stopped from the dashboard", env.db.get("sys_kill_reason"))
        self.assertEqual(env.db.get("sys_kill"), "1", "still halted until the user resumes")
        # Once flattened it doesn't flatten again every loop.
        self.assertEqual(env.db.one("SELECT COUNT(*) n FROM sys_orders WHERE source = 'kill_switch'")["n"], 2)

    def test_a_flatten_completes_when_the_coin_rallies_while_it_waits(self):
        # order-cap-blocks-risk-reducing-exit: ETH's flatten waits on a frozen quote, then ETH comes back 15% higher.
        env = holding()
        now = D0 + 600
        env.market.frozen = {"ETH": 700.0}
        step(env, now)
        env.runner.stop_trading(now, "sell everything was requested from the dashboard", flatten=True)
        run_until(env, now + 60, now + 900, every=60)
        self.assertIn("ETH", held(env))
        env.market.frozen = {}
        run_until(env, now + 960, now + 3 * 3600, every=60, prices={"ETH": env.market.prices["ETH"] * 1.15})
        self.assertEqual(held(env), {})
        self.assertFalse([c for c in children(env, status="blocked") if "order_cap" in (c["note"] or "")])

    def test_a_sell_is_capped_by_its_orders_quantity_not_by_todays_weight(self):
        controls = Controls(make_sys_config().systematic)
        book = book_for(held={"BTC": Decimal(10)})  # 10 BTC at 100 is 10% of equity
        # Its order was made for 3% of equity, the coin has since tripled against the book: still sells the 10.
        sell = Child(1, "BTC", "sell", Decimal(10), Decimal("100.1"), "taker", "decision", Decimal("0.03"),
                     qty_cap=Decimal(10))
        self.assertTrue(all(c.passed for c in controls.check_order(sell, book)))
        # But never more than the order set out to sell, and never more than is held.
        over = Child(1, "BTC", "sell", Decimal(10), Decimal("100.1"), "taker", "decision", None, qty_cap=Decimal(9))
        self.assertIn("order_cap", {c.name for c in controls.check_order(over, book) if not c.passed})
        short = Child(1, "BTC", "sell", Decimal(11), Decimal("100.1"), "taker", "decision", None, qty_cap=Decimal(11))
        self.assertIn("gross_exposure", {c.name for c in controls.check_order(short, book) if not c.passed})
        unknown = Child(1, "BTC", "sell", Decimal(1), Decimal("100.1"), "taker", "decision", None)
        self.assertIn("order_cap", {c.name for c in controls.check_order(unknown, book) if not c.passed})


class BadQuotes(unittest.TestCase):
    def test_a_crossed_quote_neither_lifts_the_peak_nor_trips_the_kill_switch(self):
        # insane-quote-inflates-peak-trips-kill
        env = holding()
        peak_before = Decimal(env.db.get("sys_peak_equity"))
        now = D0 + 720
        env.runner.next_loop = 0
        env.market.now = env.binance.now = now
        env.market.refresh()
        q = env.market.quotes["ETH"]
        env.market.quotes["ETH"] = Quote("ETH", q.bid * 10, q.bid * Decimal("9.99"), q.last, q.ts)
        env.runner.on_tick(now)
        self.assertEqual(Decimal(env.db.get("sys_peak_equity")), peak_before)
        beat = env.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        self.assertIsNone(beat["equity"], "equity unknown while a holding has no sane price")
        self.assertTrue(env.runner.halts(now).daily_loss is None, "and buys are blocked meanwhile")
        run_until(env, now + 60, now + 900, every=60)
        self.assertNotEqual(env.db.get("sys_kill"), "1")
        self.assertEqual([f for f in fills(env) if f["side"] == "sell"], [])

    def test_a_stale_quote_does_not_move_the_peak(self):
        env = holding()
        peak_before = Decimal(env.db.get("sys_peak_equity"))
        env.market.lag = -600.0  # Quotes ten minutes old, at a price far above the last.
        run_until(env, D0 + 720, D0 + 1200, every=60, prices={a: p * 3 for a, p in env.market.prices.items()})
        self.assertEqual(Decimal(env.db.get("sys_peak_equity")), peak_before)


class DayStart(unittest.TestCase):
    def test_day_start_after_downtime_counts_the_loss_since_midnight(self):
        # day-start-after-downtime-misses-loss: last loop at 23:00, back at 06:00 with holdings down 20%.
        env = holding()
        run_until(env, D0 + DAY - 3600, D0 + DAY - 3000, every=60)
        # Kraken at the fake Binance prices, so the two sources agree as they do in life.
        closes = {a: [r for r in rows if r[0] == D0][0][4] for a, rows in env.bars.items()}
        t = D0 + DAY + 6 * 3600
        run_until(env, t, t + 1800, every=60, prices={a: c * 0.80 for a, c in closes.items()})
        start = json.loads(env.db.get("sys_day_start"))
        self.assertIn("Binance's closes", start["how"])
        self.assertEqual(start["closes"], closes)
        self.assertTrue(env.runner.halts(t + 1800).daily_loss)
        self.assertEqual([c for c in children(env, side="buy") if c["placed_at"] >= t], [])

    def test_day_start_unknown_when_the_closes_cannot_be_had_blocks_buys(self):
        env = holding()
        env.binance.missing = {D0}
        t = D0 + DAY + 6 * 3600
        run_until(env, t, t + 900, every=60)
        start = json.loads(env.db.get("sys_day_start") or "null")
        self.assertTrue(start is None or start["day"] != D0 + DAY, "no day start made up")
        self.assertIsNone(env.runner.halts(t + 900).daily_loss, "unknown: no buys")


class ResearchNames(unittest.TestCase):
    def test_the_breaks_table_names_live_columns_as_the_research_does(self):
        # live-panel-column-names-diverge-luna, at the panel: a window opening after LUNA's 2022 break.
        self.assertIn("LUNA", sys_data.known_cuts())
        cut = sys_data.known_cuts()["LUNA"][0]
        last = cut + 900 * DAY
        bars = {"LUNA": series(1.0, last, 600), "LUNC": series(0.0001, last, 600), "BTC": series(20000.0, last, 600)}
        panel = sys_data.build_panel(bars, last, 600)
        self.assertEqual(panel.assets, ("BTC", "LUNA~2", "LUNC"))
        model, table = sys_research.load_cost_model(), sys_research.cost_table()
        luna2 = sys_research.venue("LUNA~2", model, table)
        self.assertFalse(luna2.tradable)
        self.assertIn("another coin", luna2.reason)
        # Before the fix: the bare name, which the cost model sends to Kraken's LUNA book (Terra Classic).
        old = sys_data.build_panel(bars, last, 600, cuts={})
        self.assertEqual(old.assets, ("BTC", "LUNA", "LUNC"))
        self.assertTrue(sys_research.venue("LUNA", model, table).tradable)
        # A break inside the window still counts from the research's numbering.
        inside = dict(bars, LUNA=[r for r in bars["LUNA"] if not (last - 200 * DAY < r[0] < last - 150 * DAY)])
        self.assertIn("LUNA~3", sys_data.build_panel(inside, last, 600).assets)

    def test_a_luna_sleeve_stays_in_cash_through_the_runner(self):
        names = ["BTC", "ETH", "LUNA", "LUNC"]
        last = D0 + 10 * DAY
        bars = {a: series(10.0 * (i + 1), last, 710, drift=0.004) for i, a in enumerate(names)}
        env = make_env("F1__ret_60__TOP10__vol40", bars=bars)
        run_until(env, D0 + 60, D0 + 300, every=60)
        row = decisions(env)[0]
        self.assertIn("LUNA~2", json.loads(row["panel"])["universe"])
        untradable = {u["column"]: u for u in json.loads(row["untradable"])}
        self.assertIn("LUNA~2", untradable)
        intended = [i for i in json.loads(row["intended"]) if i["asset"] == "LUNA"]
        self.assertEqual(len(intended), 1, "only LUNC's sleeve trades on Kraken's LUNA book")
        self.assertEqual(json.loads(row["targets"])["LUNA"]["column"], "LUNC")

    def test_two_columns_on_one_kraken_book_stay_in_cash(self):
        names = ["BTC", "ETH", "LUNA", "LUNC"]
        last = D0 + 10 * DAY
        bars = {a: series(10.0 * (i + 1), last, 710, drift=0.004) for i, a in enumerate(names)}
        env = make_env("F1__ret_60__TOP10__vol40", bars=bars)
        served = {a: [r for r in rows if r[0] < D0] for a, rows in bars.items()}
        panel = sys_data.build_panel(served, D0 - DAY, 600, cuts={})  # Named as before the fix: LUNA and LUNC.
        plan = env.runner._plan(panel, D0 + 120, D0)
        self.assertNotIn("LUNA", plan["targets"])
        self.assertEqual(sorted(u["column"] for u in plan["untradable"]), ["LUNA", "LUNC"])
        self.assertIn("can't be told", plan["untradable"][0]["reason"])


class CompareExits(unittest.TestCase):
    def test_a_column_that_leaves_the_universe_keeps_its_bars_in_the_log(self):
        # compare-fake-delisting-haircut-on-universe-exit, the runner's side.
        names = [f"C{i:02d}" for i in range(11)] + ["BTC"]
        last = D0 + 10 * DAY
        bars = {a: series(10.0 + i, last, 710, drift=0.003) for i, a in enumerate(names)}
        for i, a in enumerate(names):
            bars[a] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6] * (20 - i)) for r in bars[a]]
        env = make_env("F1__ret_60__TOP10__vol40", bars=bars)
        run_until(env, D0 + 60, D0 + 300, every=60)
        first = decisions(env)[0]
        leaving = sorted(json.loads(first["panel"])["universe"])[0]
        self.assertIn(leaving, json.loads(first["bars"]))
        # Its volume collapses, so the next decision's TOP10 leaves it out. Paper never filled it.
        env.binance.bars[leaving] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6] / 1e6) for r in bars[leaving]]
        run_until(env, D0 + DAY + 60, D0 + DAY + 300, every=60)
        second = decisions(env)[1]
        self.assertNotIn(leaving, json.loads(second["panel"])["universe"])
        logged = json.loads(second["bars"])[leaving]
        self.assertEqual(len(logged["close"]), 3)
        self.assertEqual(logged["first_ts"], D0 - 2 * DAY)

    def test_compare_prices_an_exit_at_the_next_open_not_as_a_delisting(self):
        # The compare side: decision 3 carries SOL's bars, as the runner now logs them. Prices are flat throughout.
        db = DB(Path(tempfile.mkdtemp()) / "paper_systematic.db")
        db.conn.executescript(SCHEMA)
        db.set("sys_start", json.dumps({"strategy_id": "F1__sma_50__TOP10__vol40", "quote": "USD", "gbp_usd": "1.25",
                                        "starting_capital_gbp": "20000", "starting_capital": "25000",
                                        "started_at": D0}))
        plan = [({"BTC": 0.1, "SOL": 0.1}, ["BTC", "SOL"]), ({"BTC": 0.1, "SOL": 0.0}, ["BTC", "SOL"]),
                ({"BTC": 0.1}, ["BTC", "SOL"])]
        for k, (targets, logged) in enumerate(plan):
            bars = {a: {"first_ts": D0 - DAY, "open": [100.0] * (k + 2), "close": [100.0] * (k + 2)} for a in logged}
            db.execute("INSERT INTO sys_decisions(day_ts, decided_at, strategy_id, research_code_sha256, "
                       "duo_code_sha256, signal_source, bars_sha256, panel, bars, signals, targets, bands, holdings, "
                       "equity, cash, intended, risk, untradable, gbp_usd) VALUES(?, ?, 's', 'h', 'h', 'src', 'x', '{}', "
                       "?, '{}', ?, ?, '{}', '25000', '25000', '[]', '{}', '[]', '1.25')",
                       (D0 + k * DAY, D0 + (k + 1) * DAY + 120, json.dumps(bars),
                        json.dumps({a: {"weight": w, "column": a} for a, w in targets.items()}),
                        json.dumps({a: 0.01 for a in targets})))
        days = {d["day"]: d for d in compare_module.compare(db)["days"]}
        model = sys_research.load_cost_model()
        sell = 0.1 * model.spot_cost_bps("SOL", 0.1 * 20000, D0 + 2 * DAY, "tier3_taker") / 1e4
        self.assertAlmostEqual(days["2026-11-04"]["backtest"], -sell, places=6, msg="a sale at cost, no 5% haircut")


class CappedTargets(unittest.TestCase):
    def test_a_strategy_that_can_pass_the_asset_cap_is_refused_unless_allowed(self):
        # per-asset-cap-silently-changes-targets-and-compare-uses-uncapped
        for sid in ("F1__ret_20__BTC__none", "F2__TOP10__L28__k3__invvol__nofilter"):
            with self.subTest(sid), self.assertRaises(SystemExit) as caught:
                make_env(sid)
            self.assertIn("max_asset_pct", str(caught.exception))
            self.assertIn("allow_capped_targets", str(caught.exception))
        make_env("F1__ret_20__BTC__none", max_asset_pct=Decimal(100))  # Raised: runs, uncapped.
        make_env("F2__TOP10__L28__k3__equal__nofilter")  # 1/3 a coin: under the cap, runs.

    def test_allowed_capped_targets_are_logged_and_compare_replays_them(self):
        env = make_env("F1__ret_20__BTC__none", allow_capped_targets=True)
        self.assertTrue(env.start["targets_capped"])
        for k in range(4):
            run_until(env, D0 + k * DAY + 60, D0 + k * DAY + 300, every=60)
            fill_all_makers(env, D0 + k * DAY + 400)
        target = json.loads(decisions(env)[0]["targets"])["BTC"]
        self.assertEqual((target["weight"], target["aimed"]), (1.0, 0.5))
        report = compare_module.compare(env.db)
        closes = {r[0]: r for r in env.bars["BTC"]}
        for day in report["days"]:
            ts = int(np.datetime64(day["day"]).astype("datetime64[s]").astype(int))
            move = closes[ts][4] / closes[ts][1] - 1  # A day inside the band: the replay holds 0.5, not 1.0.
            if abs(move) > 0.01:
                self.assertLess(abs(day["backtest"]), abs(move) * 0.6)
        self.assertIsNotNone(report["summary"]["backtest_total_research_targets"])
        self.assertTrue(any("per-asset limit" in n for n in report["notes"]))


class DcaCatchUp(unittest.TestCase):
    def test_a_missed_purchase_day_is_bought_at_the_next_decision(self):
        # dca-missed-tranche-never-made-up and dca-missed-purchase-day: the bot is down for all of day 7.
        env = make_env("dca_BTC_ETH", extra_days=20)
        for k in range(16):
            if k != 7:
                run_until(env, D0 + k * DAY + 60, D0 + k * DAY + 300, every=60)
        rows = {(d["day_ts"] + DAY - D0) // DAY: d for d in decisions(env)}
        tranche = Decimal(env.start["starting_capital"]) / 52 / 2
        late = json.loads(rows[8]["signals"])
        self.assertEqual((late["purchase_number"], late["tranches"], late["late"]), (2, 1, [utc(D0 + 7 * DAY)]))
        self.assertEqual([Decimal(i["notional"]) for i in json.loads(rows[8]["intended"])], [tranche, tranche])
        self.assertEqual(json.loads(rows[14]["signals"])["purchase_number"], 3)
        bought = sum(json.loads(d["signals"])["tranches"] for d in decisions(env))
        self.assertEqual(bought, sys_research.dca_due_count(env.strategy, D0, D0 + 15 * DAY))
        self.assertEqual(bought, 3)


class Restarts(unittest.TestCase):
    def test_after_a_gap_a_resting_order_the_market_passed_through_does_not_fill(self):
        # gap-fill-55: resting buys at 00:02, then three hours with no loop, and the price 3% under their limits.
        env = make_env()
        step(env, D0 + 120)
        limits = {c["asset"]: float(c["limit_price"]) for c in children(env, status="open")}
        step(env, D0 + 3 * 3600 + 120, prices={a: p * 0.97 for a, p in limits.items()})
        makers = children(env, kind="maker")
        self.assertEqual({c["status"] for c in makers}, {"missed"})
        self.assertEqual([f for f in fills(env) if f["liquidity"] == "maker"], [])

    def test_a_gap_shorter_than_the_orders_life_still_closes_it_unfilled(self):
        # 22 minutes with no tick: past two loops, so a gap, but inside the 60-minute life the order was given.
        env = make_env()
        step(env, D0 + 120)
        limits = {c["asset"]: float(c["limit_price"]) for c in children(env, status="open")}
        step(env, D0 + 120 + 22 * 60, prices={a: p * 0.97 for a, p in limits.items()})
        self.assertEqual({c["status"] for c in children(env, kind="maker") if c["placed_at"] == D0 + 120}, {"missed"})
        self.assertEqual([f for f in fills(env) if f["liquidity"] == "maker"], [])

    def test_a_resting_order_past_its_life_never_fills(self):
        env = make_env()
        step(env, D0 + 120)
        child = children(env, status="open")[0]
        env.runner.check_fills(D0 + 120 + env.runner.gate.ttl_seconds + 1)
        self.assertEqual(children(env, asset=child["asset"])[0]["status"], "missed")

    def test_a_crash_between_taker_and_fill_leaves_no_phantom_maker(self):
        # crash-taker-phantom-maker
        env = make_env()
        step(env, D0 + 120)
        run_until(env, D0 + 180, D0 + 120 + 7200 - 60, every=300)
        original = env.runner._book_fill

        class Killed(BaseException):
            pass

        def crash(child, qty, gross, fee, liquidity, *rest, **kw):
            if liquidity == "taker":
                raise Killed()
            return original(child, qty, gross, fee, liquidity, *rest, **kw)

        env.runner._book_fill = crash
        with self.assertRaises(Killed):
            step(env, D0 + 120 + 7200)
        self.assertEqual(children(env, kind="taker", status="open"), [], "the taker's row went with its fill")
        restart(env)
        run_until(env, D0 + 120 + 7260, D0 + 120 + 7800, every=60)
        for fill in fills(env):
            child = [c for c in children(env) if c["venue_id"] == fill["venue_id"]][0]
            if fill["liquidity"] == "maker":
                self.assertLessEqual(Decimal(fill["price"]), Decimal(child["ask"]) if child["side"] == "buy" else 10**9)
            self.assertEqual(child["kind"], fill["liquidity"])

    def test_an_open_taker_left_by_an_older_version_closes_unfilled_at_start(self):
        env = make_env()
        step(env, D0 + 120)
        order = env.db.one("SELECT id FROM sys_orders LIMIT 1")["id"]
        env.db.execute("INSERT INTO sys_children(order_id, kind, asset, side, qty, limit_price, bid, ask, placed_at, "
                       "status) VALUES(?, 'taker', 'BTC', 'buy', '0.01', '200000', '100', '101', ?, 'open')",
                       (order, D0 + 130))
        restart(env)
        run_until(env, D0 + 180, D0 + 600, every=60)
        taker = children(env, kind="taker")[0]
        self.assertEqual(taker["status"], "unfilled")
        self.assertEqual([f for f in fills(env) if Decimal(f["price"]) == Decimal("200000")], [])

    def test_a_restart_remembers_the_bar_it_decided_on(self):
        # restart-forgets-bar-seen: Binance down from 00:30, and the bot restarts.
        env = make_env()
        run_until(env, D0 + 60, D0 + 1800, every=60)
        env.binance.fail = True
        restart(env)
        run_until(env, D0 + 1860, D0 + 3 * 3600, every=60)
        beat = env.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        self.assertNotIn("daily bar missing", beat["halts"])
        self.assertEqual(env.db.all("SELECT * FROM sys_orders WHERE status = 'working'"), [])
        self.assertEqual({f["asset"] for f in fills(env)}, {"BTC", "ETH"})

    def test_a_frozen_feed_stays_frozen_across_a_restart(self):
        # frozen-quote-resets-on-restart, with the real SystematicMarket and an unchanging ticker.
        class Frozen:
            def public(self, method, **kw):
                if method == "AssetPairs":
                    return {"XXBTZUSD": {"altname": "XBTUSD", "base": "XXBT", "quote": "ZUSD", "lot_decimals": 8,
                                         "pair_decimals": 1, "ordermin": "0.00005", "costmin": "0.5"}}
                return {"XXBTZUSD": {"b": ["80000.0", "1", "1"], "a": ["80000.1", "1", "1"], "c": ["80000.0", "0.1"],
                                     "v": ["10", "100"], "t": [5, 50]}}

        env = make_env()
        market = SystematicMarket(Frozen(), "USD", {"BTC": "XBT"})
        env.runner.market = market
        env.runner.next_loop = float("inf")  # Only the tick's own work: fills and keeping the feed's state.
        market.refresh()
        first = market.unchanged_since["BTC"]
        env.runner.on_tick(time.time())
        fresh = SystematicMarket(Frozen(), "USD", {"BTC": "XBT"})
        SystematicRunner(env.config, env.db, fresh, PaperBroker(env.config), env.binance, env.strategy, env.start)
        fresh.refresh()
        self.assertEqual(fresh.unchanged_since["BTC"], first)
        self.assertGreaterEqual(fresh.frozen_seconds("BTC", first + 700), 700)


class Outages(unittest.TestCase):
    def watch(self, env, now, **kw):
        notes = []
        ok, _ = watchdog_check(env.runner.heartbeat_path, 15, now=now, run=AC,
                               notify_fn=lambda t, m: notes.append(m), **kw)
        return ok, notes

    def test_a_long_kraken_outage_is_notified(self):
        # watchdog-silent-on-outage, case A.
        env = make_env()
        env.market.fail = True
        run_until(env, D0 + 60, D0 + 6 * 3600, every=60)
        ok, notes = self.watch(env, D0 + 6 * 3600 + 30)
        self.assertFalse(ok)
        self.assertTrue(any("no new orders for" in n for n in notes))
        self.assertTrue(any("overdue" in n for n in notes))

    def test_a_missing_daily_bar_is_notified(self):
        # Case B: the bar never arrives. Alive and beating, but no decision.
        env = make_env()
        env.binance.missing.add(D0 - DAY)
        run_until(env, D0 + 60, D0 + 2 * 3600, every=300)
        ok, notes = self.watch(env, D0 + 2 * 3600 + 30)
        self.assertFalse(ok)
        self.assertTrue(any("overdue" in n for n in notes))

    def test_a_short_wobble_is_not_notified(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 600, every=60)
        env.market.fail = True
        run_until(env, D0 + 660, D0 + 1200, every=60)
        ok, notes = self.watch(env, D0 + 1230)
        self.assertTrue(ok, notes)

    def test_a_truncated_binance_reply_is_an_api_error_and_the_loop_still_beats(self):
        # binance-incompleteread-escapes
        class Truncated(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                raise http.client.IncompleteRead(b"[[1", 100)

        with mock.patch("urllib.request.urlopen", lambda *a, **k: Truncated()):
            with self.assertRaises(sys_data.DataError):
                sys_data.BinancePublic(base_url="http://binance.invalid").klines("BTCUSDT", 3)
            with self.assertRaises(KrakenNetworkError):
                KrakenClient(base_url="http://kraken.invalid").public("Ticker", pair="XBTUSD")
        env = make_env()

        def broken(*a, **k):
            raise http.client.BadStatusLine("")

        env.binance.klines = broken  # Anything unexpected from the data source, even unwrapped.
        for t in (D0 + 120, D0 + 420, D0 + 720):
            step(env, t)
        self.assertEqual(env.db.one("SELECT COUNT(*) n FROM sys_heartbeats")["n"], 3)
        self.assertEqual(json.loads(env.db.get("sys_api_errors"))["binance"], 3)


class Records(unittest.TestCase):
    def test_every_control_is_on_every_decision_with_or_without_orders(self):
        # decision-log-missing-controls
        env = make_env()
        for k in range(3):
            run_until(env, D0 + k * DAY + 60, D0 + k * DAY + 9000, every=300)
        rows = decisions(env)
        self.assertEqual([len(json.loads(r["intended"])) > 0 for r in rows], [True, False, False])
        for row in rows:
            risk = json.loads(row["risk"])
            self.assertEqual([c["control"] for c in risk["portfolio"]],
                             ["kill_switch", "daily_loss", "stale_data", "quote_fresh", "asset_limit",
                              "gross_exposure", "order_cap"])
            self.assertEqual({c["control"] for c in risk["portfolio"]}, set(CONTROLS))
            for order in risk["orders"]:
                values = {c["control"]: c["value"] for c in order["checks"]}
                self.assertIsNotNone(values["kill_switch"])
                self.assertIsNotNone(values["daily_loss"])

    def test_flatten_orders_and_their_fills_are_in_the_export(self):
        # killswitch-not-exported
        env = holding()
        run_until(env, D0 + 420, D0 + 900, every=60, prices={a: p * 0.3 for a, p in env.market.prices.items()})
        self.assertEqual(env.db.get("sys_kill"), "1")
        exported = export_decisions(env.db)
        orders = [o for d in exported for o in d["orders"]]
        self.assertEqual(sum(o["source"] == "kill_switch" for o in orders), 2)
        sells = [(f["side"], f["asset"]) for o in orders for c in o["children"] for f in c["fills"] if f["side"] == "sell"]
        self.assertEqual(sorted(sells), [("sell", "BTC"), ("sell", "ETH")])
        report = compare_module.compare(env.db)
        for order in report["orders"]:
            if order["source"] == "kill_switch":
                self.assertIsNone(order["assumed_open"], "a flatten has no backtest counterpart")


if __name__ == "__main__":
    unittest.main()
