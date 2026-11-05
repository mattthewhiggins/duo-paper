"""The systematic runner: timing, the band rule, maker-first orders, every risk control, the decision log, DCA,
refusals, the agents' lack of authority, the watchdog and compare. No network: Binance and Kraken are stand-ins."""
import json
import math
import os
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from duo import compare as compare_module
from duo import sys_data, sys_research
from duo.brokers import PaperBroker
from duo.db import DB
from duo.engine import Engine
from duo.sys_risk import CONTROLS, Book, Child, Controls, Halts, OrderGate
from duo.systematic import SCHEMA, SystematicRunner, band_trades, export_decisions, prepare, resume_in_db
from duo.watchdog import check as watchdog_check
from duo.web import Dashboard

from .helpers import ScriptedClient, buy, make_engine, risk, run_ticks, strategist
from .sys_helpers import (D0, DAY, FakeBinance, FakeSysMarket, kline, make_env, make_sys_config, rows_close_before,
                          run_until, series, step)


def decisions(env):
    return env.db.all("SELECT * FROM sys_decisions ORDER BY id")


def children(env, **where):
    rows = env.db.all("SELECT * FROM sys_children ORDER BY id")
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def fills(env):
    return env.db.all("SELECT * FROM fills ORDER BY id")


def fill_all_makers(env, now):
    """Moves every working buy's market far enough below its limit that the paper rule fills it."""
    for child in children(env, status="open"):
        price = float(child["limit_price"]) * (0.999 if child["side"] == "buy" else 1.001)
        env.market.prices[child["asset"]] = price
    step(env, now)


class Timing(unittest.TestCase):
    def test_decides_once_after_the_settle_using_the_just_closed_bar_and_not_the_forming_one(self):
        env = make_env()
        # The forming bar (D0) is absurd. If anything read it, the targets would show it.
        for asset in env.bars:
            env.bars[asset] = [r if r[0] != D0 else (r[0], r[1] * 50, r[2] * 50, r[3], r[4] * 50, r[5], r[6])
                               for r in env.bars[asset]]
        step(env, D0 + 60)
        self.assertEqual(decisions(env), [], "no decision before 00:00 + 2 minutes")
        self.assertEqual(env.runner.next_loop, D0 + 120, "a loop falls exactly at the settle")
        run_until(env, D0 + 120, D0 + 6 * 3600, every=300)
        rows = decisions(env)
        self.assertEqual(len(rows), 1, "one decision per day")
        self.assertEqual(rows[0]["day_ts"], D0 - DAY)
        self.assertEqual(rows[0]["decided_at"], D0 + 120)
        panel_info = json.loads(rows[0]["panel"])
        self.assertEqual(panel_info["last_bar"], "2026-11-01 00:00:00 UTC")
        # The research's own targets on bars up to and including D0 - DAY.
        bars = {a: [r for r in rows_ if r[0] <= D0 - DAY] for a, rows_ in env.bars.items()}
        panel = sys_data.build_panel(bars, D0 - DAY, env.config.systematic.history_days)
        target = sys_research.targets(env.strategy, panel)[0][-1]
        logged = json.loads(rows[0]["targets"])
        for c, asset in enumerate(panel.assets):
            self.assertAlmostEqual(logged[asset]["weight"], target[c], places=12)
        # The next day decides again, once.
        run_until(env, D0 + DAY, D0 + DAY + 3600, every=300)
        self.assertEqual([r["day_ts"] for r in decisions(env)], [D0 - DAY, D0])

    def test_parse_drops_the_forming_bar(self):
        rows = [kline(r) for r in series(100.0, D0, 5)]
        parsed = sys_data.parse_klines(rows, now=D0 + 120)
        self.assertEqual(parsed[-1][0], D0 - DAY)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(len(sys_data.parse_klines(rows, now=D0 + DAY + 1)), 5)

    def test_panel_must_end_on_the_just_closed_bar(self):
        env = make_env()
        env.binance.missing = {D0 - DAY}
        run_until(env, D0 + 60, D0 + 1800, every=300)
        self.assertEqual(decisions(env), [])


class BandRule(unittest.TestCase):
    def test_band_rule_matches_the_research_engine(self):
        self.assertFalse(band_trades(0.30, 0.27, 0.05))
        self.assertTrue(band_trades(0.30, 0.24, 0.05))
        self.assertTrue(band_trades(0.0, 0.01, 0.05), "a target of 0 always trades")
        self.assertFalse(band_trades(0.0, 0.0, 0.05))
        self.assertFalse(band_trades(0.5, 0.0, 10.0), "F2's hold band stops everything but exits")

    def test_second_day_inside_the_band_places_nothing(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 300, every=60)
        fill_all_makers(env, D0 + 400)
        self.assertEqual(len(fills(env)), 2)
        run_until(env, D0 + DAY, D0 + DAY + 600, every=300)
        second = decisions(env)[1]
        self.assertEqual(json.loads(second["intended"]), [])


class MakerFirst(unittest.TestCase):
    def test_post_only_at_the_bid_repriced_then_taker_with_the_cap(self):
        env = make_env()
        step(env, D0 + 120)
        first = children(env, asset="BTC")
        self.assertEqual([c["kind"] for c in first], ["maker"])
        self.assertEqual(Decimal(first[0]["limit_price"]), Decimal(first[0]["bid"]).quantize(Decimal("0.1"), "ROUND_DOWN"))
        # Fifteen minutes on, the price has moved and the order follows the bid.
        price = env.market.prices["BTC"] * 1.001
        run_until(env, D0 + 180, D0 + 120 + 900, every=60, prices={"BTC": price})
        btc = children(env, asset="BTC")
        self.assertEqual(btc[0]["status"], "cancelled")
        self.assertEqual(btc[1]["kind"], "maker")
        self.assertGreater(Decimal(btc[1]["limit_price"]), Decimal(btc[0]["limit_price"]))
        # At 120 minutes what's left crosses, capped at 0.5% from the quote.
        run_until(env, D0 + 120 + 960, D0 + 120 + 7200, every=60)
        btc = children(env, asset="BTC")
        taker = [c for c in btc if c["kind"] == "taker"]
        self.assertEqual(len(taker), 1)
        self.assertEqual(taker[0]["placed_at"], D0 + 120 + 7200)
        self.assertTrue(all(c["status"] in ("cancelled", "missed") for c in btc if c["kind"] == "maker"))
        self.assertLessEqual(Decimal(taker[0]["limit_price"]), Decimal(taker[0]["ask"]) * Decimal("1.005"))
        self.assertGreater(Decimal(taker[0]["limit_price"]), Decimal(taker[0]["ask"]) * Decimal("1.0049"))
        fill = [f for f in fills(env) if f["asset"] == "BTC"][0]
        self.assertEqual(fill["liquidity"], "taker")
        self.assertAlmostEqual(float(Decimal(fill["fee"]) / Decimal(fill["gross"])), 0.0038, places=10)
        self.assertEqual(len([c for c in btc if c["kind"] == "maker"]), 8, "placed at 00:02, re-priced 7 times")

    def test_resting_order_fills_as_maker_by_the_paper_rule(self):
        env = make_env()
        step(env, D0 + 120)
        child = children(env, asset="ETH")[0]
        limit = float(child["limit_price"])
        env.market.prices["ETH"] = limit * 1.00001  # Touching isn't enough.
        step(env, D0 + 150)
        self.assertEqual(fills(env), [])
        env.market.prices["ETH"] = limit * 0.999
        step(env, D0 + 160)
        fill = fills(env)[0]
        self.assertEqual((fill["asset"], fill["liquidity"]), ("ETH", "maker"))
        self.assertEqual(Decimal(fill["price"]), Decimal(child["limit_price"]))
        self.assertAlmostEqual(float(Decimal(fill["fee"]) / Decimal(fill["gross"])), 0.0022, places=10)
        self.assertIsNotNone(children(env, asset="ETH")[0]["fill_bid"], "the quote at the fill is kept (finding 81)")


def book_for(**kw):
    base = dict(now=1000.0, equity=Decimal(10000), cash=Decimal(10000), held={}, marks={"BTC": Decimal(100)},
                asset_limit={"BTC": Decimal("0.52")}, quotes={"BTC": SimpleNamespace(bid=Decimal("99.9"),
                                                                                      ask=Decimal("100.1"), ts=990.0)},
                frozen={"BTC": 0.0}, halts=Halts())
    base.update(kw)
    return Book(**base)


def buy_child(qty="30", limit="99.9", change="0.30", source="decision", side="buy", kind="maker"):
    return Child(1, "BTC", side, Decimal(qty), Decimal(limit), kind, source,
                 None if change is None else Decimal(change))


class SpyBroker(PaperBroker):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []

    def place_resting(self, ticket, quote, ttl):
        self.calls.append(ticket)
        return super().place_resting(ticket, quote, ttl)

    def execute(self, ticket, quote):
        self.calls.append(ticket)
        return super().execute(ticket, quote)


class RiskControls(unittest.TestCase):
    def setUp(self):
        self.config = make_sys_config()
        self.controls = Controls(self.config.systematic)
        self.broker = SpyBroker(replace(self.config, maker_pct=Decimal("0.22"), taker_pct=Decimal("0.38")))
        self.gate = OrderGate(self.broker, self.controls, 3600)

    def failed(self, child, book):
        return {c.name for c in self.controls.check_order(child, book) if not c.passed}

    def test_a_clean_order_passes_every_control(self):
        checks = self.controls.check_order(buy_child(), book_for())
        self.assertEqual({c.name for c in checks}, set(CONTROLS) - {"kill_switch"} | {"kill_switch"})
        self.assertTrue(all(c.passed for c in checks))
        self.assertTrue(self.gate.place(buy_child(), book_for()).ok)

    def test_each_control_trips(self):
        cases = {
            "kill_switch": (buy_child(), book_for(halts=Halts(kill=True, kill_reason="drawdown"))),
            "daily_loss": (buy_child(), book_for(halts=Halts(daily_loss=True))),
            "stale_data": (buy_child(), book_for(halts=Halts(signal_stale=True))),
            "quote_fresh": (buy_child(), book_for(now=1200.0)),
            "asset_limit": (buy_child(qty="60", change="0.60"), book_for()),
            "gross_exposure": (buy_child(qty="45", change="0.50"), book_for(cash=Decimal(4000))),
            "order_cap": (buy_child(qty="40"), book_for()),
        }
        for name, (child, book) in cases.items():
            with self.subTest(name):
                self.assertIn(name, self.failed(child, book))
                self.assertFalse(self.gate.place(child, book).ok)
        self.assertEqual(self.broker.calls, [], "no blocked order ever reached the broker")

    def test_a_quote_fetched_just_after_the_loop_began_is_fresh(self):
        # Found by the live smoke run: quotes are stamped when fetched, after the loop's clock, so their age is
        # a little below 0. That must pass, and a quote stamped far in the future must not.
        later = {"BTC": SimpleNamespace(bid=Decimal("99.9"), ask=Decimal("100.1"), ts=1002.5)}
        self.assertNotIn("quote_fresh", self.failed(buy_child(), book_for(quotes=later)))
        future = {"BTC": SimpleNamespace(bid=Decimal("99.9"), ask=Decimal("100.1"), ts=1500.0)}
        self.assertIn("quote_fresh", self.failed(buy_child(), book_for(quotes=future)))
        env = make_env()
        env.market.lag = 2.0
        run_until(env, D0 + 60, D0 + 180, every=60)
        self.assertEqual(len(children(env, status="open")), 2, "orders go out at the decision loop")

    def test_api_errors_and_frozen_quotes_halt_new_orders(self):
        self.assertIn("stale_data", self.failed(buy_child(), book_for(halts=Halts(api_errors=5))))
        self.assertNotIn("stale_data", self.failed(buy_child(), book_for(halts=Halts(api_errors=4))))
        self.assertIn("quote_fresh", self.failed(buy_child(), book_for(frozen={"BTC": 601.0})))

    def test_fails_closed_on_missing_or_non_finite_inputs(self):
        cases = {
            "equity None": book_for(equity=None),
            "equity NaN": book_for(equity=Decimal("NaN")),
            "cash None": book_for(cash=None),
            "no mark": book_for(marks={}),
            "no quote": book_for(quotes={}),
            "frozen unknown": book_for(frozen={}),
            "no asset limit": book_for(asset_limit={}),
            "daily loss unknown": book_for(halts=Halts(daily_loss=None)),
            "signal unknown": book_for(halts=Halts(signal_stale=None)),
            "api errors unknown": book_for(halts=Halts(api_errors=None)),
        }
        for label, book in cases.items():
            with self.subTest(label):
                self.assertTrue(self.failed(buy_child(), book))
        self.assertTrue(self.failed(buy_child(change=None), book_for()))
        self.assertTrue(self.failed(buy_child(qty="NaN"), book_for()))
        self.assertFalse(self.controls.drawdown(None, Decimal(100)).passed)
        self.assertFalse(self.controls.drawdown(Decimal("NaN"), Decimal(100)).passed)
        self.assertFalse(self.controls.day_loss(Decimal(100), None).passed)
        self.assertFalse(all(c.passed for c in self.controls.holdings(book_for(equity=None))))
        self.assertEqual(self.broker.calls, [])

    def test_sells_survive_the_daily_loss_limit_but_never_short(self):
        held = book_for(held={"BTC": Decimal(10)}, halts=Halts(daily_loss=True))
        sell = buy_child(qty="10", limit="100.1", side="sell", change="0.10")
        self.assertEqual(self.failed(sell, held), set())
        self.assertIn("gross_exposure", self.failed(buy_child(qty="11", limit="100.1", side="sell", change="0.11"), held))

    def test_kill_switch_flatten_is_the_only_order_while_halted(self):
        book = book_for(held={"BTC": Decimal(10)}, halts=Halts(kill=True, kill_reason="x", signal_stale=True))
        flatten = buy_child(qty="10", limit="99.4", side="sell", change="0.10", source="kill_switch", kind="taker")
        self.assertEqual(self.failed(flatten, book), set())
        self.assertIn("kill_switch", self.failed(buy_child(qty="10", limit="100.1", side="sell", change="0.1"), book))

    def test_the_runner_cannot_reach_the_broker_except_through_the_gate(self):
        env = make_env()
        self.assertFalse(any(isinstance(v, PaperBroker) for v in vars(env.runner).values()))
        self.assertFalse(any(isinstance(v, PaperBroker) for k, v in vars(env.runner.gate).items()
                             if not k.startswith("_OrderGate__")))
        placed = []
        original = OrderGate.place

        def counting(gate, child, book):
            placed.append(child)
            return original(gate, child, book)

        OrderGate.place = counting
        try:
            run_until(env, D0 + 60, D0 + 3 * 3600, every=300)
        finally:
            OrderGate.place = original
        made = [c for c in children(env) if c["status"] != "blocked"]
        self.assertGreater(len(made), 2)
        self.assertEqual(len(made) + len(children(env, status="blocked")), len(placed))
        for child in children(env):
            self.assertTrue(json.loads(child["checks"]), "every placed order carries its checks")


class KillSwitch(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        run_until(self.env, D0 + 60, D0 + 300, every=60)
        fill_all_makers(self.env, D0 + 400)

    def test_flattens_at_market_with_the_cap_and_stays_halted_until_resumed(self):
        env = self.env
        held = {f["asset"] for f in fills(env)}
        self.assertEqual(held, {"BTC", "ETH"})
        crash = {a: p * 0.3 for a, p in env.market.prices.items()}
        run_until(env, D0 + 420, D0 + 900, every=60, prices=crash)
        self.assertEqual(env.db.get("sys_kill"), "1")
        sells = [f for f in fills(env) if f["side"] == "sell"]
        self.assertEqual({f["asset"] for f in sells}, held)
        self.assertTrue(all(f["liquidity"] == "taker" for f in sells))
        for child in children(env, side="sell"):
            self.assertGreaterEqual(Decimal(child["limit_price"]), Decimal(child["bid"]) * Decimal("0.995"))
        self.assertEqual(env.runner.portfolio().qty("BTC"), 0)
        # Halted: the next day makes no decision, however long it runs.
        run_until(env, D0 + DAY, D0 + DAY + 3600, every=300)
        self.assertEqual(len(decisions(env)), 1)
        self.assertEqual(env.db.one("SELECT status FROM sys_heartbeats ORDER BY id DESC LIMIT 1")["status"], "halted")
        # Resumed by the user: the pending decision is made at the next loop, and the peak restarts.
        self.assertTrue(env.runner.resume(D0 + DAY + 3700))
        run_until(env, D0 + DAY + 3720, D0 + DAY + 4200, every=60)
        self.assertEqual(env.db.get("sys_kill"), "0")
        self.assertEqual(len(decisions(env)), 2)

    def test_resume_from_the_command_line(self):
        env = self.env
        env.runner.stop_trading(D0 + 500, "stopped from the dashboard", flatten=False)
        self.assertIn("Resumed", resume_in_db(env.db, D0 + 600))
        self.assertEqual(env.db.get("sys_kill"), "0")
        self.assertIn("isn't halted", resume_in_db(env.db, D0 + 700))


class DailyLossAndStaleData(unittest.TestCase):
    def test_daily_loss_blocks_buys_cancels_resting_ones_and_allows_sells(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 300, every=60)
        fill_all_makers(env, D0 + 400)
        # The next day: a fall of about 10% of equity by 00:07, and a decision with a buy still working.
        env.market.prices = {a: p for a, p in env.market.prices.items()}
        run_until(env, D0 + DAY, D0 + DAY + 130, every=60)
        fall = {a: p * 0.85 for a, p in env.market.prices.items()}
        run_until(env, D0 + DAY + 180, D0 + DAY + 600, every=60, prices=fall)
        self.assertEqual(env.db.get("sys_daily_loss_day"), str(D0 + DAY))
        self.assertNotEqual(env.db.get("sys_kill"), "1")
        self.assertEqual(children(env, status="open", side="buy"), [])
        book = env.runner.book(D0 + DAY + 600)
        sell = Child(0, "BTC", "sell", book.held["BTC"] / 2, book.quotes["BTC"].ask, "maker", "decision", Decimal("0.2"))
        buy_ = Child(0, "BTC", "buy", Decimal("0.001"), book.quotes["BTC"].bid, "maker", "decision", Decimal("0.2"))
        controls = env.runner.controls
        self.assertTrue(all(c.passed for c in controls.check_order(sell, book)))
        self.assertIn("daily_loss", {c.name for c in controls.check_order(buy_, book) if not c.passed})
        # A new UTC day lifts it. The loop runs up to midnight, so the day's start is its last loop. (Skipping straight
        # to the next day would value the holdings at the fake Binance closes, which this test's Kraken prices sit 15%
        # below, and read that as a loss: DownTime.test_day_start_after_downtime_counts_the_loss_since_midnight.)
        run_until(env, D0 + 2 * DAY - 600, D0 + 2 * DAY - 60, every=60)
        step(env, D0 + 2 * DAY + 120)
        self.assertFalse(env.runner.halts(D0 + 2 * DAY + 120).daily_loss)

    def test_day_start_is_the_last_loop_before_midnight_when_recent(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 300, every=60)
        run_until(env, D0 + DAY - 600, D0 + DAY - 60, every=60)
        step(env, D0 + DAY + 1800)
        start = json.loads(env.db.get("sys_day_start"))
        self.assertIn("last loop before 00:00 UTC", start["how"])
        env2 = make_env()
        step(env2, D0 + 3600)
        self.assertIn("60 minutes after", json.loads(env2.db.get("sys_day_start"))["how"])

    def test_stale_signal_halts_new_orders_then_resumes_by_itself(self):
        env = make_env()
        env.binance.missing = {D0 - DAY}
        run_until(env, D0 + 60, D0 + 1920, every=60)
        self.assertEqual(decisions(env), [])
        halts = env.runner.halts(D0 + 1920)
        self.assertTrue(halts.signal_stale)
        stale = [c for c in json.loads(env.db.one("SELECT controls FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
                                       ["controls"]) if c["control"] == "stale_data"][0]
        self.assertEqual((stale["outcome"], stale["value"], stale["threshold"]), ("fail", 32.0, 30.0), "the 00:32 loop: 32 minutes missing")
        beat = env.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        self.assertEqual(beat["status"], "degraded")
        self.assertIn("daily bar missing", beat["halts"])
        self.assertIn("stale_data", {c.name for c in env.runner.controls.check_order(buy_child(), book_for(halts=halts))
                                     if not c.passed})
        env.binance.missing = set()
        run_until(env, D0 + 1980, D0 + 2520, every=60)
        self.assertEqual(len(decisions(env)), 1)
        self.assertFalse(env.runner.halts(D0 + 2520).signal_stale)
        self.assertTrue(children(env, status="open"))
        self.assertEqual(env.db.one("SELECT status FROM sys_heartbeats ORDER BY id DESC LIMIT 1")["status"], "ok")

    def test_five_api_errors_halt_and_a_success_clears_them(self):
        env = make_env()
        step(env, D0 + 60)
        env.market.fail = True
        for k in range(5):
            step(env, D0 + 61 + k)
        self.assertEqual(env.runner.halts(D0 + 70).api_errors, 5)
        env.market.fail = False
        step(env, D0 + 80)
        self.assertEqual(env.runner.halts(D0 + 80).api_errors, 0)

    def test_stale_and_frozen_quotes_place_nothing(self):
        env = make_env()
        env.market.frozen = {"BTC": 700.0, "ETH": 700.0}
        run_until(env, D0 + 60, D0 + 600, every=60)
        self.assertEqual(len(decisions(env)), 1)
        self.assertEqual(children(env, status="open"), [])
        self.assertTrue(children(env, status="blocked"))
        self.assertTrue(all("frozen" in c["note"] for c in children(env, status="blocked")))


class DecisionLog(unittest.TestCase):
    def test_every_field_is_there(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 300, every=60)
        fill_all_makers(env, D0 + 400)
        row = dict(decisions(env)[0])
        for key in ("decided_at", "strategy_id", "research_code_sha256", "duo_code_sha256", "signal_source", "bars_sha256",
                    "panel", "bars", "signals", "targets", "bands", "holdings", "equity", "cash", "intended", "risk",
                    "untradable", "gbp_usd"):
            self.assertIsNotNone(row[key], key)
        self.assertEqual(row["strategy_id"], "F1__ENS__BTC_ETH__vol40")
        self.assertEqual(row["research_code_sha256"], sys_research.code_sha256())
        self.assertEqual(len(row["bars_sha256"]), 64)
        bars = json.loads(row["bars"])
        self.assertEqual(set(bars), {"BTC", "ETH"})
        self.assertEqual(len(bars["BTC"]["close"]), 300)
        self.assertEqual(bars["BTC"]["close"][-1], rows_close_before(env.bars["BTC"], D0))
        signals = json.loads(row["signals"])
        self.assertEqual(signals["BTC"]["signal"], 1.0)
        self.assertIsNotNone(signals["BTC"]["vol"])
        risk_log = json.loads(row["risk"])
        # Every control on every decision (was five of the seven: review finding decision-log-missing-controls).
        self.assertEqual({c["control"] for c in risk_log["portfolio"]}, set(CONTROLS))
        for c in risk_log["portfolio"]:
            self.assertIn(c["outcome"], ("pass", "fail"))
            self.assertIn("threshold", c)
            self.assertIn("value", c)
        for order in risk_log["orders"]:
            self.assertEqual({c["control"] for c in order["checks"]}, set(CONTROLS))
        self.assertEqual(json.loads(row["holdings"])["positions"], {})
        self.assertEqual(len(json.loads(row["intended"])), 2)
        exported = export_decisions(env.db)
        self.assertEqual(len(exported[0]["orders"]), 2)
        linked = [f for o in exported[0]["orders"] for c in o["children"] for f in c["fills"]]
        self.assertEqual(len(linked), 2, "each fill links back to its order and decision")
        json.dumps(exported, default=str)

    def test_heartbeat_every_loop_and_the_heartbeat_file(self):
        env = make_env()
        run_until(env, D0 + 60, D0 + 1800, every=60)
        beats = env.db.all("SELECT * FROM sys_heartbeats")
        self.assertEqual(len(beats), 1 + 6, "00:01, then every 5 minutes from 00:02")
        payload = json.loads(env.runner.heartbeat_path.read_text())
        self.assertEqual(payload["ts"], beats[-1]["ts"])
        self.assertEqual(payload["status"], "ok")

    def test_a_loop_gap_is_recorded_and_stale_resting_orders_close_unfilled(self):
        env = make_env()
        step(env, D0 + 120)
        self.assertTrue(children(env, status="open"))
        step(env, D0 + 3 * 3600 + 120)
        beat = env.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        self.assertGreater(beat["loop_gap_s"], 3 * 3600 - 60)
        self.assertTrue(any("No loop ran" in e["text"] for e in env.db.all("SELECT text FROM events")))
        self.assertTrue(all(c["status"] in ("missed", "filled") or c["placed_at"] > D0 + 3600 for c in children(env)))
        self.assertTrue(any("bot wasn't running" in (c["note"] or "") for c in children(env)))


class TopUniverse(unittest.TestCase):
    def test_untradable_sleeves_stay_in_cash_and_the_log_says_so(self):
        last = D0 + 10 * DAY
        names = ["BTC", "ETH", "XMR", "SOL", "ADA"]
        bars = {a: series(10.0 * (i + 1), last, 710, drift=0.002 + 0.0005 * i) for i, a in enumerate(names)}
        for i, a in enumerate(names):  # Volumes rank them, highest first in list order.
            bars[a] = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6] * (10 - i)) for r in bars[a]]
        env = make_env("F1__ret_60__TOP10__vol40", bars=bars)
        run_until(env, D0 + 60, D0 + 300, every=60)
        row = decisions(env)[0]
        untradable = json.loads(row["untradable"])
        self.assertEqual([u["column"] for u in untradable], ["XMR"])
        self.assertIn("UK clients", untradable[0]["reason"])
        self.assertEqual(json.loads(row["panel"])["universe"], sorted(names))
        intended = {i["asset"] for i in json.loads(row["intended"])}
        self.assertNotIn("XMR", intended)
        self.assertEqual(intended, {"BTC", "ETH", "SOL", "ADA"})
        self.assertAlmostEqual(json.loads(row["risk"])["asset_limit"], 0.2 + 0.02, places=9)


class DCA(unittest.TestCase):
    def test_schedule(self):
        strategy = sys_research.resolve("dca_BTC_ETH")
        due = [k for k in range(400) if sys_research.dca_purchase(strategy, D0, D0 + k * DAY)]
        self.assertEqual(due, list(range(0, 52 * 7, 7)))
        self.assertEqual(sys_research.dca_purchase(strategy, D0, D0 - DAY), 0)

    def test_buys_a_tranche_of_each_every_seventh_day(self):
        env = make_env("dca_BTC_ETH")
        for k in range(8):
            run_until(env, D0 + k * DAY + 60, D0 + k * DAY + 300, every=60)
        counts = [len(json.loads(d["intended"])) for d in decisions(env)]
        self.assertEqual(counts, [2, 0, 0, 0, 0, 0, 0, 2])
        tranche = Decimal(env.start["starting_capital"]) / 52 / 2
        for item in json.loads(decisions(env)[0]["intended"]):
            self.assertEqual(Decimal(item["notional"]), tranche)
        self.assertEqual(json.loads(decisions(env)[7]["signals"])["purchase_number"], 2)


class Refusals(unittest.TestCase):
    def test_f3_f4_f5_and_unknown_ids_are_refused_at_start(self):
        for sid, word in (("F3__BTC__D7__E10", "FCA"), ("F4__1h__BTC__L20__Z-2__P3__none", "intraday"),
                          ("F5__a+b__equal", "F5"), ("nonsense", "Use an F1")):
            config = make_sys_config(sid)
            with self.subTest(sid), self.assertRaises(SystemExit) as caught:
                prepare(config, DB(config.db_path), FakeSysMarket({"BTC": 1}, 0))
            self.assertIn(word, str(caught.exception))

    def test_live_mode_is_refused_with_the_runbook(self):
        config = make_sys_config(mode="live")
        with self.assertRaises(SystemExit) as caught:
            prepare(config, DB(config.db_path), FakeSysMarket({"BTC": 1}, 0))
        self.assertIn("Going live with the systematic runner", str(caught.exception))
        self.assertFalse(config.db_path.exists() and DB(config.db_path).get("sys_start"))
        env = make_env()
        with self.assertRaises(SystemExit):
            SystematicRunner(replace(env.config, mode="live"), env.db, env.market, env.broker, env.binance,
                             env.strategy, env.start)
        with self.assertRaises(SystemExit):
            SystematicRunner(env.config, env.db, env.market, object(), env.binance, env.strategy, env.start)

    def test_one_paper_run_holds_one_strategy(self):
        env = make_env()
        other = replace(env.raw_config, systematic=replace(env.raw_config.systematic, strategy="dca_BTC_ETH"))
        with self.assertRaises(SystemExit) as caught:
            prepare(other, env.db, env.market)
        self.assertIn("One paper run holds one strategy", str(caught.exception))
        again, _, start = prepare(env.raw_config, env.db, env.market)
        self.assertEqual(start["starting_capital"], env.start["starting_capital"])

    def test_paper_runs_at_the_research_fees_and_capital_in_its_own_database(self):
        env = make_env()
        self.assertEqual(env.config.db_path.name, "paper_systematic.db")
        self.assertEqual((env.config.maker_pct, env.config.taker_pct), (Decimal("0.22"), Decimal("0.38")))
        self.assertEqual(env.start["quote"], "USD")
        self.assertEqual(Decimal(env.start["starting_capital"]), Decimal("24950.00"))  # 20,000 x 1.25 less 20 bps FX.


class Agents(unittest.TestCase):
    def test_agents_are_off_by_default_and_cannot_order(self):
        config = make_sys_config()
        engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")], config=config)
        self.assertIsNone(engine.runner, "no client is used while agents_enabled is false")
        self.assertIsNone(engine.next_discussion())

    def test_even_switched_on_they_cannot_trade(self):
        config = make_sys_config(agents_enabled=True)
        engine, client = make_engine([], config=config)
        self.assertIsNotNone(engine.runner)
        self.assertIsNone(engine._wake_reason(engine.market.quotes, time.time()),
                          "systematic mode never wakes a decision session")
        self.assertIsNotNone(engine.next_discussion(), "the desk may talk when switched on")
        run_ticks(engine)  # A tick sets up the limits the briefing quotes. Its desk chat has no replies and fails.
        self.assertEqual(engine.db.one("SELECT kind FROM sessions")["kind"], "discussion")
        client.responses = [strategist(orders=[buy()], strategy="Plan."), risk("approve")]
        engine._run_session("test")  # Even an approved session applies nothing.
        self.assertEqual(engine.db.one("SELECT outcome FROM sessions ORDER BY id DESC LIMIT 1")["outcome"], "approved")
        self.assertEqual(engine.db.all("SELECT * FROM fills"), [])
        self.assertEqual(engine.db.all("SELECT * FROM resting_orders"), [])
        from duo.risk import Ticket
        quote = engine.market.quotes["BTC"]
        filled, text = engine._execute(Ticket("BTC", "buy", Decimal("0.001"), quote.ask * 2), quote, "agents", None)
        self.assertFalse(filled)
        self.assertIn("no authority", text)
        placed, _ = engine._place_resting(Ticket("BTC", "buy", Decimal("0.001"), quote.bid), quote, "plan", None)
        self.assertFalse(placed)
        self.assertEqual(engine.db.all("SELECT * FROM fills"), [])


class EngineAndDashboard(unittest.TestCase):
    def test_engine_loop_runs_the_runner_and_the_dashboard_shows_it(self):
        env = make_env()
        engine = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
        for t in range(D0 + 60, D0 + 400, 10):
            env.market.now = env.binance.now = t
            engine.tick(t)
        self.assertEqual(len(decisions(env)), 1)
        state = Dashboard(engine, env.config).state()
        json.dumps(state)
        sys_state = state["systematic"]
        self.assertEqual(sys_state["strategy"], "F1__ENS__BTC_ETH__vol40")
        self.assertEqual({r["asset"] for r in sys_state["holdings"]}, {"BTC", "ETH"})
        self.assertIn("kill_switch", {c["control"] for c in sys_state["controls"]})
        self.assertIn("python -m duo resume", sys_state["how_to_resume"])
        self.assertIn("compare", sys_state)
        self.assertEqual(state["status"]["code"], "warn" if time.time() - D0 > 3600 else "active",
                         "an old heartbeat reads as the loop not running")
        engine.halt("stopped from the dashboard")
        self.assertEqual(env.db.get("sys_kill"), "1")
        engine.resume()
        self.assertEqual(env.db.get("sys_kill"), "0")

    def test_a_broken_database_doesnt_end_the_main_loop(self):
        env = make_env()
        engine = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
        engine.tick = lambda now=None: (_ for _ in ()).throw(RuntimeError("disk full"))
        engine.db.event = lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked"))
        engine._stopping.set()  # So the back-off doesn't wait.
        self.assertFalse(engine.run_once())


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "heartbeat_systematic.json"
        self.notes = []

    def pmset(self, source):
        text = f"Now drawing from '{source}'\n -InternalBattery-0 (id=1)\t64%; discharging; 3:10 remaining\n"
        return lambda *a, **k: SimpleNamespace(stdout=text)

    def test_stale_heartbeat_is_reported_and_notified(self):
        self.path.write_text(json.dumps({"ts": 1000.0, "utc": "then", "status": "ok"}))
        ok, lines = watchdog_check(self.path, 15, now=1000.0 + 16 * 60, run=self.pmset("AC Power"),
                                   notify_fn=lambda t, m: self.notes.append(m))
        self.assertFalse(ok)
        self.assertIn("down", self.notes[0])
        self.assertIn("16 minutes", lines[0])

    def test_missing_heartbeat_is_down(self):
        ok, _ = watchdog_check(self.path, 15, now=1000.0, run=self.pmset("AC Power"),
                               notify_fn=lambda t, m: self.notes.append(m))
        self.assertFalse(ok)
        self.assertEqual(len(self.notes), 1)

    def test_battery_is_reported_and_notified(self):
        self.path.write_text(json.dumps({"ts": 1000.0, "status": "ok"}))
        ok, lines = watchdog_check(self.path, 15, now=1060.0, run=self.pmset("Battery Power"),
                                   notify_fn=lambda t, m: self.notes.append(m))
        self.assertFalse(ok)
        self.assertIn("battery at 64%", self.notes[0])

    def test_all_well_on_mains(self):
        self.path.write_text(json.dumps({"ts": 1000.0, "status": "ok", "halts": []}))
        ok, lines = watchdog_check(self.path, 15, now=1060.0, run=self.pmset("AC Power"),
                                   notify_fn=lambda t, m: self.notes.append(m))
        self.assertTrue(ok)
        self.assertEqual(self.notes, [])

    def test_off_a_mac_the_power_isnt_checked(self):
        # GitHub's Linux machines have no pmset: the run is all that's checked, and it says so.
        def no_pmset(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "/usr/bin/pmset")
        self.path.write_text(json.dumps({"ts": 1000.0, "status": "ok", "halts": []}))
        ok, lines = watchdog_check(self.path, 15, now=1060.0, run=no_pmset, notify_fn=lambda t, m: self.notes.append(m))
        self.assertTrue(ok)
        self.assertIn("Power: not checked, as this machine isn't a Mac.", lines)
        self.path.write_text(json.dumps({"ts": 1000.0, "status": "halted", "halts": ["kill switch: test"]}))
        ok, lines = watchdog_check(self.path, 15, now=1060.0, run=no_pmset, notify_fn=lambda t, m: self.notes.append(m))
        self.assertFalse(ok, "a halt still fails it, which on GitHub fails the run")

    def test_notification_goes_through_osascript_with_quoting(self):
        from duo.watchdog import notify
        calls = []
        notify("Duo", 'a "quoted" message', run=lambda args, **k: calls.append(args))
        self.assertEqual(calls[0][:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('\\"quoted\\"', calls[0][2])


class Compare(unittest.TestCase):
    """A synthetic paper period of three decisions, with answers worked out by hand."""

    def test_known_answers(self):
        db = DB(Path(tempfile.mkdtemp()) / "paper_systematic.db")
        db.conn.executescript(SCHEMA)
        db.set("sys_start", json.dumps({"strategy_id": "F1__ENS__BTC_ETH__vol40", "quote": "USD", "gbp_usd": "1.25",
                                        "starting_capital_gbp": "20000", "starting_capital": "25000",
                                        "started_at": D0}))
        opens, closes = [100.0, 101.0, 108.0, 100.0], [100.0, 110.0, 99.0, 105.0]
        days = [D0, D0 + DAY, D0 + 2 * DAY]
        equity = ["25000", "25750", "25250"]
        for k, day in enumerate(days):
            n = k + 2
            bars = {"BTC": {"first_ts": D0 - DAY, "open": opens[:n], "close": closes[:n]}}
            db.execute("INSERT INTO sys_decisions(day_ts, decided_at, strategy_id, research_code_sha256, duo_code_sha256, "
                       "signal_source, bars_sha256, panel, bars, signals, targets, bands, holdings, equity, cash, "
                       "intended, risk, untradable, gbp_usd) VALUES(?, ?, 's', 'h', 'h', 'src', 'x', '{}', ?, '{}', ?, ?, "
                       "'{}', ?, '0', '[]', '{}', '[]', '1.25')",
                       (day, day + DAY + 120, json.dumps(bars), json.dumps({"BTC": {"weight": 0.5, "column": "BTC"}}),
                        json.dumps({"BTC": 0.05}), equity[k]))
        db.execute("INSERT INTO sys_orders(decision_id, source, asset, side, notional_wanted, created_at, status) "
                   "VALUES(1, 'decision', 'BTC', 'buy', '12500', 0, 'filled')")
        db.execute("INSERT INTO sys_children(order_id, venue_id, kind, asset, side, qty, limit_price, placed_at, status) "
                   "VALUES(1, 'paper-1', 'maker', 'BTC', 'buy', '100', '108.5', 0, 'filled')")
        db.execute("INSERT INTO sys_children(order_id, venue_id, kind, asset, side, qty, limit_price, placed_at, status) "
                   "VALUES(1, 'paper-2', 'taker', 'BTC', 'buy', '15', '109', 0, 'filled')")
        db.execute("INSERT INTO fills(ts, source, asset, side, qty, price, gross, fee, realized, venue_id, liquidity) "
                   "VALUES(0, 'systematic', 'BTC', 'buy', '100', '108.5', '10850', '23.87', '0', 'paper-1', 'maker')")
        db.execute("INSERT INTO fills(ts, source, asset, side, qty, price, gross, fee, realized, venue_id, liquidity) "
                   "VALUES(0, 'systematic', 'BTC', 'buy', '15', '109', '1635', '6.213', '0', 'paper-2', 'taker')")
        report = compare_module.compare(db)

        model = sys_research.load_cost_model()
        bps = model.spot_cost_bps("BTC", 0.5 * 20000, D0 + DAY, "tier3_taker")
        day1 = (1 - 0.5 * bps / 1e4) * (1 + 0.5 * (99 / 108 - 1)) - 1
        h = 0.5 * (99 / 108) / (1 + 0.5 * (99 / 108 - 1))
        g = 100 / 99 - 1
        G = 1 + h * g
        h2 = h * (1 + g) / G
        self.assertLess(abs(0.5 - h2), 0.05, "inside the band, so day 2 doesn't trade")
        day2 = G * (1 + h2 * (105 / 100 - 1)) - 1
        paper = [25750 / 25000 - 1, 25250 / 25750 - 1]
        got = report["days"]
        self.assertEqual([d["day"] for d in got], ["2026-11-03", "2026-11-04"], "decided on bar D0, traded on D0 + 1")
        for d, b, p in zip(got, (day1, day2), paper):
            self.assertAlmostEqual(d["backtest"], b, places=12)
            self.assertAlmostEqual(d["paper"], p, places=12)
            self.assertAlmostEqual(d["difference"], p - b, places=12)
        diffs = np.array([p - b for p, b in zip(paper, (day1, day2))])
        s = report["summary"]
        self.assertAlmostEqual(s["mean_difference"], diffs.mean(), places=12)
        self.assertAlmostEqual(s["tracking_error_daily"], diffs.std(ddof=1), places=12)
        self.assertAlmostEqual(s["tracking_error_annual"], diffs.std(ddof=1) * math.sqrt(365), places=12)
        self.assertEqual((s["fills"], s["maker_fills"]), (2, 1))
        self.assertAlmostEqual(s["maker_share_of_notional"], 10850 / 12485, places=12)

        order = report["orders"][0]
        self.assertEqual(order["assumed_open"], 108.0)
        self.assertAlmostEqual(order["assumed_cost_bps"], model.spot_cost_bps("BTC", 12485 / 1.25, D0 + DAY), places=9)
        self.assertAlmostEqual(order["paper_price"], 12485 / 115, places=9)
        self.assertAlmostEqual(order["paper_fee_bps"], (23.87 + 6.213) / 12485 * 1e4, places=9)
        self.assertAlmostEqual(order["price_vs_open_bps"], (12485 / 115 / 108 - 1) * 1e4, places=9)
        self.assertAlmostEqual(order["maker_share"], 0.5)
        text = compare_module.markdown(report)
        self.assertIn("Paper against backtest", text)
        self.assertIn("| 2026-11-03 |", text)

    def test_compare_on_a_runner_period(self):
        env = make_env()
        for k in range(3):
            run_until(env, D0 + k * DAY + 60, D0 + k * DAY + 300, every=60)
            fill_all_makers(env, D0 + k * DAY + 400)
        report = compare_module.compare(env.db)
        self.assertEqual(report["summary"]["days"], 2)
        self.assertEqual(report["summary"]["maker_share"], 1.0)
        self.assertTrue(all(o["assumed_open"] is not None for o in report["orders"]))


if __name__ == "__main__":
    unittest.main()
