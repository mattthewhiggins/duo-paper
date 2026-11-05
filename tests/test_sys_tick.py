"""Scheduled runs (python -m duo tick): one poll per fresh process every 15 to 60 minutes, as on GitHub Actions.

Resting paper orders judged on Kraken's 1-minute candles between runs, gaps that are normal on a schedule, the slack on
re-pricing and the taker switch, Binance's second host, the tick command's guards and exit codes, the probe and the
status page command, and three days of scheduled runs on one database with every run built fresh.

No network: Binance and Kraken are the stand-ins in tests/sys_helpers.py, or urlopen patched to a canned reply.
"""
import contextlib
import http.client
import io
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest import mock

from duo import __main__ as cli
from duo import config as config_module
from duo import sys_data
from duo.config import ConfigError
from duo.db import DB
from duo.kraken import KrakenNetworkError
from duo.market import Market
from duo.sys_market import SystematicMarket

from .sys_helpers import (D0, DAY, FakeBinance, FakeHosts, PathMarket, Reply, cron_run, flat, make_env, make_sys_config,
                          rows_close_before, run_until, series, step)

THROUGH = 0.9997  # 3 bps under a buy's limit: past the paper rule's 0.02% trade-through.


def default_bars():
    last = D0 + 10 * DAY
    return {"BTC": series(20000.0, last, 710), "ETH": series(1500.0, last, 710)}


def cron_env(**kw):
    """make_env in cron mode, on a market whose price stays at the last closes unless a test sets a candle."""
    bars = default_bars()
    prices = {a: rows_close_before(rows, D0) for a, rows in bars.items()}
    return make_env(bars=bars, market=PathMarket(flat(prices), D0 + 60), scheduled=True, **kw)


def open_child(env, asset):
    return env.db.one("SELECT * FROM sys_children WHERE asset = ? AND status = 'open'", (asset,))


def child_row(env, child_id):
    return env.db.one("SELECT * FROM sys_children WHERE id = ?", (child_id,))


def fills(env, **where):
    rows = env.db.all("SELECT * FROM fills ORDER BY id")
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def events(db, text):
    return [e["text"] for e in db.all("SELECT text FROM events ORDER BY id") if text in e["text"]]


def dip(env, asset, ts, limit, factor=THROUGH):
    """A 1-minute candle at ts whose low is `factor` of the limit."""
    env.market.candles[(asset, ts)] = (limit, limit, limit * factor, limit)


def rest(env, side, asset, qty, limit, placed_at):
    """A working order with one resting maker child, as manage() leaves one."""
    order = env.db.execute(
        "INSERT INTO sys_orders(decision_id, source, asset, side, qty_wanted, notional_wanted, target_change, "
        "created_at, status) VALUES(NULL, 'decision', ?, ?, ?, ?, '0.1', ?, 'working')",
        (asset, side, str(qty) if side == "sell" else None, str(qty * limit) if side == "buy" else None,
         placed_at)).lastrowid
    return env.db.execute(
        "INSERT INTO sys_children(order_id, kind, asset, side, qty, limit_price, bid, ask, placed_at, status) "
        "VALUES(?, 'maker', ?, ?, ?, ?, ?, ?, ?, 'open')",
        (order, asset, side, str(qty), str(limit), str(limit), str(limit), placed_at)).lastrowid


@contextlib.contextmanager
def quiet(summary_path=None):
    """No printing, and no job summary unless a test asks for one, even when the suite itself runs on Actions."""
    environ = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    if summary_path:
        environ["GITHUB_STEP_SUMMARY"] = str(summary_path)
    out = io.StringIO()
    with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(out):
        yield out


class PathFills(unittest.TestCase):
    """A resting child is judged on the 1-minute candles from the whole minute after it was placed to the end of its
    life, by the paper rule: through its limit by the trade-through, in full, at the limit, as a maker."""

    def setUp(self):
        self.env = cron_env()
        cron_run(self.env, D0 + 330)  # 00:05:30: the decision, and a resting buy for each asset.
        self.btc = open_child(self.env, "BTC")
        self.limit = float(self.btc["limit_price"])

    def test_a_buy_fills_at_its_limit_on_the_first_candle_through_it(self):
        env, limit = self.env, self.limit
        dip(env, "BTC", D0 + 600, limit, 0.99985)  # 00:10: 1.5 bps under. Touching isn't enough.
        dip(env, "BTC", D0 + 660, limit)           # 00:11: through.
        dip(env, "BTC", D0 + 720, limit, 0.99)     # 00:12: deeper, but later.
        cron_run(env, D0 + 1230)
        fill = fills(env, asset="BTC")[0]
        self.assertEqual((fill["liquidity"], fill["side"], fill["ts"]), ("maker", "buy", D0 + 660))
        self.assertEqual(Decimal(fill["price"]), Decimal(self.btc["limit_price"]))
        self.assertAlmostEqual(float(Decimal(fill["fee"]) / Decimal(fill["gross"])), 0.0022, places=10)
        child = child_row(env, self.btc["id"])
        self.assertEqual((child["status"], child["closed_at"]), ("filled", D0 + 660))
        self.assertIn("1-minute candle starting 00:11 UTC", child["note"])
        self.assertEqual((Decimal(child["fill_bid"]), Decimal(child["fill_ask"])),
                         (Decimal(repr(limit * THROUGH)), Decimal(repr(limit))), "the candle's low and high")
        order = env.db.one("SELECT * FROM sys_orders WHERE id = ?", (child["order_id"],))
        self.assertEqual((order["status"], order["closed_at"]), ("filled", D0 + 660))
        self.assertIsNone(open_child(env, "BTC"), "nothing new placed for a filled order")

    def test_a_sell_fills_on_the_first_candle_whose_high_is_through_it(self):
        env = self.env
        dip(env, "BTC", D0 + 660, self.limit)
        cron_run(env, D0 + 1230)  # Bought, as above.
        held = env.runner.portfolio().qty("BTC")
        limit = Decimal(str(self.limit)) * Decimal("1.001")
        child = rest(env, "sell", "BTC", held / 2, limit, D0 + 1230.5)
        high = float(limit) * 1.0003
        env.market.candles[("BTC", D0 + 1260)] = (float(limit), float(limit) * 1.0001, float(limit), float(limit))
        env.market.candles[("BTC", D0 + 1320)] = (float(limit), high, float(limit), float(limit))
        env.market.now = D0 + 1500
        env.runner.check_fills(D0 + 1500)
        sell = fills(env, side="sell")[0]
        self.assertEqual((sell["liquidity"], sell["ts"], Decimal(sell["price"])), ("maker", D0 + 1320, limit))
        self.assertAlmostEqual(float(Decimal(sell["fee"]) / Decimal(sell["gross"])), 0.0022, places=10)
        self.assertEqual(Decimal(child_row(env, child)["fill_ask"]), Decimal(repr(high)))
        self.assertEqual(env.runner.portfolio().qty("BTC"), held - held / 2)

    def test_a_trade_through_before_it_was_placed_does_not_fill(self):
        env, limit = self.env, self.limit
        dip(env, "BTC", D0 + 240, limit, 0.99)  # 00:04, before it was placed.
        dip(env, "BTC", D0 + 300, limit, 0.99)  # 00:05, the minute it was placed in, whose trades may be from before.
        cron_run(env, D0 + 1230)
        self.assertEqual(fills(env), [])
        self.assertEqual(child_row(env, self.btc["id"])["status"], "cancelled", "re-priced 15 minutes on")

    def test_a_trade_through_after_its_life_does_not_fill(self):
        env, limit = self.env, self.limit
        life = env.runner.gate.ttl_seconds
        self.assertEqual(life, 3600)
        dip(env, "BTC", D0 + 330 + life + 30, limit, 0.99)  # 01:06, after its life ended at 01:05:30.
        cron_run(env, D0 + 330 + 75 * 60)  # The next run is 75 minutes on: late, but not an outage.
        self.assertEqual(fills(env), [])
        child = child_row(env, self.btc["id"])
        self.assertEqual(child["status"], "missed")
        self.assertIn("no trade through its limit on Kraken's 1-minute candles", child["note"])
        self.assertIn("past its 60-minute life", child["note"])
        self.assertFalse(events(env.db, "No loop ran"))

    def test_a_missing_minute_never_fills(self):
        env, limit = self.env, self.limit
        env.market.gaps = {D0 + 360}  # 00:06, the first whole minute of its life.
        dip(env, "BTC", D0 + 480, limit, 0.99)  # 00:08
        cron_run(env, D0 + 1230)
        self.assertEqual(fills(env), [])
        child = child_row(env, self.btc["id"])
        self.assertEqual(child["status"], "cancelled")
        self.assertIn("missing the one from 00:06 UTC", child["note"])
        # One missing later, before the candle that would fill it, doesn't count either.
        second = open_child(env, "BTC")
        env.market.gaps = {D0 + 1320}  # 00:22
        dip(env, "BTC", D0 + 1380, float(second["limit_price"]), 0.99)  # 00:23
        cron_run(env, D0 + 2130)
        self.assertEqual(fills(env), [])

    def test_candles_that_cant_be_had_never_fill_and_close_it_only_after_its_life(self):
        env, limit = self.env, self.limit
        env.market.candles_fail = True
        dip(env, "BTC", D0 + 660, limit, 0.99)  # It would have filled.
        cron_run(env, D0 + 330 + 600)  # Ten minutes on, too soon to re-price: still open.
        self.assertEqual(child_row(env, self.btc["id"])["status"], "open")
        cron_run(env, D0 + 330 + 65 * 60)  # Past its life.
        child = child_row(env, self.btc["id"])
        self.assertEqual(child["status"], "missed")
        self.assertIn("couldn't be read", child["note"])
        self.assertEqual(fills(env), [])

    def test_each_resting_order_is_judged_on_its_own_assets_candles(self):
        env = self.env
        dip(env, "ETH", D0 + 660, float(open_child(env, "ETH")["limit_price"]))
        cron_run(env, D0 + 1230)
        self.assertEqual(sorted(set(env.market.candle_calls)), ["BTC", "ETH"])
        self.assertEqual([(f["asset"], f["ts"]) for f in fills(env)], [("ETH", D0 + 660)], "BTC's own path stayed put")


class CronGaps(unittest.TestCase):
    def test_gaps_of_15_to_60_minutes_are_normal(self):
        env = cron_env()
        t = D0 + 330
        cron_run(env, t)
        for minutes in (15, 30, 45, 60):
            t += minutes * 60
            cron_run(env, t)
        self.assertFalse(events(env.db, "No loop ran"))
        makers = env.db.all("SELECT * FROM sys_children WHERE kind = 'maker'")
        self.assertEqual({c["status"] for c in makers}, {"cancelled"}, "re-priced each run, never closed as missed")
        self.assertTrue(all(b["loop_gap_s"] is None for b in env.db.all("SELECT loop_gap_s FROM sys_heartbeats")))
        self.assertEqual({c["status"] for c in env.db.all("SELECT * FROM sys_children WHERE kind = 'taker'")},
                         {"filled"}, "past 118 minutes at the last run, what's left crossed")

    def test_a_gap_past_cron_gap_minutes_is_an_outage_judged_on_the_path_first(self):
        env = cron_env()
        cron_run(env, D0 + 330)
        btc, eth = open_child(env, "BTC"), open_child(env, "ETH")
        dip(env, "BTC", D0 + 1200, float(btc["limit_price"]))  # 00:20, inside its life.
        self.assertEqual(env.config.systematic.cron_gap_minutes, 180)
        cron_run(env, D0 + 330 + 181 * 60)
        self.assertEqual(len(events(env.db, "No loop ran for 181 minutes")), 1)
        self.assertEqual((child_row(env, btc["id"])["status"], child_row(env, btc["id"])["closed_at"]),
                         ("filled", D0 + 1200))
        self.assertEqual(fills(env, asset="BTC")[0]["ts"], D0 + 1200)
        eth = child_row(env, eth["id"])
        self.assertEqual((eth["status"], eth["note"]),
                         ("missed", "closed unfilled: the bot wasn't running to manage it"))
        beat = env.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        self.assertEqual(beat["loop_gap_s"], 181 * 60)


class Thresholds(unittest.TestCase):
    def test_reprice_and_taker_switch_come_cron_slack_seconds_early(self):
        env = cron_env()
        self.assertEqual(env.config.systematic.cron_slack_seconds, 120)
        start = D0 + 330
        for minutes in (0, 14, 28, 42, 56, 70, 84, 98, 112, 118):
            cron_run(env, start + minutes * 60)
        btc = env.db.all("SELECT * FROM sys_children WHERE asset = 'BTC' ORDER BY id")
        makers = [c for c in btc if c["kind"] == "maker"]
        self.assertEqual([c["placed_at"] - start for c in makers],
                         [m * 60 for m in (0, 14, 28, 42, 56, 70, 84, 98, 112)], "a run 14 minutes on re-prices")
        self.assertEqual({c["status"] for c in makers}, {"cancelled"})
        taker = [c for c in btc if c["kind"] == "taker"]
        self.assertEqual(taker[0]["placed_at"], start + 118 * 60, "118 minutes on: taker_after_minutes less the slack")

    def test_the_loop_keeps_its_own_thresholds(self):
        env = make_env()
        run_until(env, D0 + 120, D0 + 120 + 840, every=60)
        env.runner.manage(D0 + 120 + 840)
        self.assertEqual(len(env.db.all("SELECT * FROM sys_children WHERE asset = 'BTC'")), 1, "14 minutes: not yet")
        env.runner.manage(D0 + 120 + 900)
        self.assertEqual(len(env.db.all("SELECT * FROM sys_children WHERE asset = 'BTC'")), 2)


class CancelsBookPathFills(unittest.TestCase):
    """A re-price or the switch to taker first books a fill that already happened on the path. Here the run's ticker
    fails, so no fill check runs before them."""

    def test_a_reprice_books_a_fill_that_already_happened(self):
        env = cron_env()
        cron_run(env, D0 + 330)
        btc = open_child(env, "BTC")
        dip(env, "BTC", D0 + 720, float(btc["limit_price"]))  # 00:12
        env.market.refresh_failures = 1
        cron_run(env, D0 + 330 + 900)
        self.assertEqual(child_row(env, btc["id"])["status"], "filled")
        self.assertEqual([(f["liquidity"], f["ts"]) for f in fills(env)], [("maker", D0 + 720)])
        order = env.db.one("SELECT * FROM sys_orders WHERE id = ?", (btc["order_id"],))
        self.assertEqual(order["status"], "filled")
        self.assertEqual(len(env.db.all("SELECT * FROM sys_children WHERE asset = 'BTC'")), 1)

    def test_the_switch_to_taker_books_a_fill_that_already_happened(self):
        env = cron_env()
        start = D0 + 330
        for k in range(8):  # 00:05:30 to 01:50:30, every 15 minutes.
            cron_run(env, start + k * 900)
        btc = open_child(env, "BTC")
        self.assertEqual(btc["placed_at"], start + 7 * 900)
        dip(env, "BTC", D0 + 6900, float(btc["limit_price"]))  # 01:55
        env.market.refresh_failures = 1
        cron_run(env, start + 7200 - 120)  # Taker time.
        self.assertEqual(child_row(env, btc["id"])["status"], "filled")
        self.assertEqual([(f["liquidity"], f["ts"]) for f in fills(env, asset="BTC")], [("maker", D0 + 6900)])
        self.assertEqual(env.db.all("SELECT * FROM sys_children WHERE asset = 'BTC' AND kind = 'taker'"), [])


class CronHeartbeat(unittest.TestCase):
    def test_the_decision_alert_waits_out_the_grace_and_the_payload_says_cron(self):
        env = cron_env()
        env.binance.missing = {D0 - DAY}
        cron_run(env, D0 + 3600)
        payload = json.loads(env.runner.heartbeat_path.read_text())
        self.assertEqual(payload["schedule"], "cron")
        self.assertIsNone(payload["decision_overdue_minutes"], "01:00 is inside the 120 minutes' grace")
        cron_run(env, D0 + 9000)
        self.assertAlmostEqual(json.loads(env.runner.heartbeat_path.read_text())["decision_overdue_minutes"], 30.0)
        loop = make_env()
        loop.binance.missing = {D0 - DAY}
        step(loop, D0 + 3600)
        payload = json.loads(loop.runner.heartbeat_path.read_text())
        self.assertEqual((payload["schedule"], payload["decision_overdue_minutes"]), ("loop", 30.0))


class BinanceHosts(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBinance({"BTC": series(20000.0, D0, 10)}, D0 + 120)
        self.client = sys_data.BinancePublic(["https://first.test", "https://second.test/"])

    def get(self, answers):
        hosts = FakeHosts(self.fake, answers)
        with mock.patch("urllib.request.urlopen", hosts):
            return self.client.klines("BTCUSDT", 3), hosts.calls

    def test_a_host_that_refuses_or_fails_is_passed_over_for_the_next(self):
        for answer in (403, 418, 429, 451, 500, 503, urllib.error.URLError("no route to host"),
                       TimeoutError("timed out"), http.client.IncompleteRead(b"[[1", 100)):
            with self.subTest(answer=answer):
                rows, calls = self.get({"first.test": answer})
                self.assertEqual(len(rows), 3)
                self.assertEqual(calls, ["first.test", "second.test"])
                self.assertEqual(self.client.served_by, "second.test")

    def test_an_answer_that_would_be_the_same_everywhere_is_not_retried(self):
        with self.assertRaises(sys_data.DataError) as caught:
            self.get({"first.test": 400})
        self.assertIn("first.test: HTTP 400", str(caught.exception))
        hosts = FakeHosts(self.fake, {"first.test": 400})
        with mock.patch("urllib.request.urlopen", hosts), self.assertRaises(sys_data.DataError):
            self.client.klines("BTCUSDT", 3)
        self.assertEqual(hosts.calls, ["first.test"])

    def test_it_fails_only_when_every_host_does(self):
        with self.assertRaises(sys_data.DataError) as caught:
            self.get({"first.test": 451, "second.test": urllib.error.URLError("offline")})
        self.assertIn("first.test: HTTP 451", str(caught.exception))
        self.assertIn("second.test: URLError", str(caught.exception))

    def test_the_default_hosts_are_binances_market_data_host_then_api(self):
        self.assertEqual(sys_data.BinancePublic().base_urls,
                         ("https://data-api.binance.vision", "https://api.binance.com"))
        self.assertEqual(sys_data.BinancePublic(base_url="https://only.test").base_urls, ("https://only.test",))

    def test_the_decision_records_the_host_that_served_its_bars(self):
        env = make_env()
        env.runner.binance = sys_data.BinancePublic(["https://refused.test", "https://serves.test"])
        with mock.patch("urllib.request.urlopen", FakeHosts(env.binance, {"refused.test": 451})):
            step(env, D0 + 120)
        source = env.db.one("SELECT signal_source FROM sys_decisions")["signal_source"]
        self.assertEqual(source, "Binance public spot klines, 1d, /api/v3/klines from serves.test "
                                 "(the research's source: spot_1d)")
        self.assertNotIn("api.binance.com", source)


class KrakenMinutes(unittest.TestCase):
    class Kraken:
        def __init__(self, ohlc_fails=False):
            self.ohlc_fails = ohlc_fails
            self.calls = []

        def public(self, method, **kw):
            self.calls.append((method, kw))
            if method == "AssetPairs":
                return {"XXBTZUSD": {"altname": "XBTUSD", "base": "XXBT", "quote": "ZUSD", "lot_decimals": 8,
                                     "pair_decimals": 1, "ordermin": "0.00005", "costmin": "0.5"}}
            if method == "Ticker":
                return {"XXBTZUSD": {"b": ["70000.0", "1", "1"], "a": ["70000.1", "1", "1"], "c": ["70000.0", "0.1"],
                                     "v": ["10", "100"], "t": [5, 50]}}
            if self.ohlc_fails:
                raise KrakenNetworkError("timed out")
            rows = [[D0 + 60 * k, "70000.0", "70010.0", "69990.0", "70005.0", "70001.0", "1.5", 12] for k in range(720)]
            return {"XXBTZUSD": rows, "last": D0 + 60 * 719}

    def test_one_minute_candles_once_per_run(self):
        client = self.Kraken()
        market = SystematicMarket(client, "USD", {"BTC": "XBT"})
        market.refresh()
        candles = market.minute_candles("BTC")
        self.assertEqual(len(candles), 720)
        first = candles[0]
        self.assertEqual((first.ts, first.low, first.high, first.volume), (D0, 69990.0, 70010.0, 1.5))
        self.assertIs(market.minute_candles("BTC"), candles)
        self.assertEqual([kw for m, kw in client.calls if m == "OHLC"], [{"pair": "XBTUSD", "interval": 1}])

    def test_a_failure_is_kept_for_the_run_too(self):
        client = self.Kraken(ohlc_fails=True)
        market = SystematicMarket(client, "USD", {"BTC": "XBT"})
        with self.assertRaises(ValueError) as caught:
            market.minute_candles("BTC")
        self.assertIn("no Kraken pair known", str(caught.exception))
        fresh = SystematicMarket(client, "USD", {"BTC": "XBT"})
        fresh.refresh()
        for _ in range(2):
            with self.assertRaises(ValueError):
                fresh.minute_candles("BTC")
        self.assertEqual(sum(m == "OHLC" for m, _ in client.calls), 1)

    def test_the_other_intervals_are_as_before(self):
        market = Market(self.Kraken(), ("BTC",), "USD")
        with self.assertRaises(ValueError):
            market.candles("BTC", 5)


class Config(unittest.TestCase):
    def systematic(self, **raw):
        return config_module._systematic({"enabled": True, **raw})

    def test_the_new_settings_and_their_defaults(self):
        config = make_sys_config().systematic
        self.assertEqual(config.binance_urls, ("https://data-api.binance.vision", "https://api.binance.com"))
        self.assertEqual((config.cron_gap_minutes, config.cron_decision_grace_minutes, config.cron_slack_seconds),
                         (180, 120, 120))
        self.assertEqual(self.systematic().binance_urls, config.binance_urls)
        self.assertEqual(self.systematic(binance_urls=["https://a.test/"]).binance_urls, ("https://a.test",))

    def test_bad_values_are_refused(self):
        for raw in ({"binance_urls": []}, {"binance_urls": "https://a.test"}, {"binance_urls": ["http://a.test"]},
                    {"binance_urls": ["https://user@a.test"]}, {"binance_urls": ["https://a.test/?x=1"]},
                    {"binance_urls": [3]}, {"cron_gap_minutes": 0}, {"cron_decision_grace_minutes": 2000},
                    {"cron_slack_seconds": -1}, {"cron_slack_seconds": 900}, {"cron_slack_seconds": 1.5}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                self.systematic(**raw)


class Tick(unittest.TestCase):
    """tick_once, the function behind python -m duo tick, on the test stand-ins."""

    def setUp(self):
        self.config = make_sys_config()
        self.bars = default_bars()
        self.prices = {a: rows_close_before(rows, D0) for a, rows in self.bars.items()}
        self.sleeps = []

    def tick(self, now, market=None, summary=None, **kw):
        market = market or PathMarket(flat(self.prices), now)
        with quiet(summary) as out:
            code = cli.tick_once(self.config, market=market, binance=FakeBinance(self.bars, now), now=now,
                                 sleep=self.sleeps.append, **kw)
        self.out = out.getvalue()
        return code

    def db(self):
        return DB(self.config.db_path)

    def test_no_database_does_nothing_and_exits_3(self):
        self.assertEqual(self.tick(D0 + 300), cli.NO_RUN)
        self.assertIn("python -m duo tick --first-run", self.out)
        self.assertFalse(self.config.db_path.exists())
        self.assertEqual(list(self.config.data_dir.iterdir()), [])

    def test_a_database_without_a_start_record_does_nothing_and_exits_3(self):
        db = self.db()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.conn.close()
        for side in ("-wal", "-shm"):
            Path(f"{self.config.db_path}{side}").unlink(missing_ok=True)
        before = self.config.db_path.read_bytes()
        self.assertEqual(self.tick(D0 + 300), cli.NO_RUN)
        self.assertIn("no start record", self.out)
        self.assertEqual(self.config.db_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.config.data_dir.iterdir()), ["paper_systematic.db"])

    def test_first_run_starts_the_run_and_the_next_carries_it_on(self):
        self.assertEqual(self.tick(D0 + 300, first_run=True), 0)
        self.assertIn("Decided on the bar of 2026-11-01", self.out)
        self.assertEqual(self.tick(D0 + 1200), 0)
        db = self.db()
        self.assertIsNotNone(db.get("sys_start"))
        self.assertEqual(len(db.all("SELECT * FROM sys_decisions")), 1)
        self.assertEqual([b["ts"] for b in db.all("SELECT ts FROM sys_heartbeats")], [D0 + 300, D0 + 1200])
        self.assertEqual(len(events(db, "Paper run of")), 1, "started once")

    def test_flatten_sells_everything_and_halts(self):
        self.tick(D0 + 330, first_run=True)
        market = PathMarket(flat(self.prices), D0 + 1230)
        db = self.db()
        for child in db.all("SELECT * FROM sys_children WHERE status = 'open'"):
            limit = float(child["limit_price"])
            market.candles[(child["asset"], D0 + 660)] = (limit, limit, limit * THROUGH, limit)
        db.conn.close()
        self.tick(D0 + 1230, market=market)
        db = self.db()
        self.assertEqual({f["asset"] for f in db.all("SELECT * FROM fills WHERE liquidity = 'maker'")}, {"BTC", "ETH"})
        db.conn.close()
        self.assertEqual(self.tick(D0 + 2130, flatten=True), 0)
        db = self.db()
        sells = db.all("SELECT * FROM fills WHERE side = 'sell'")
        self.assertEqual({(f["asset"], f["liquidity"]) for f in sells}, {("BTC", "taker"), ("ETH", "taker")})
        self.assertEqual(db.get("sys_kill"), "1")
        self.assertIn("command line", db.get("sys_kill_reason"))
        self.assertIsNone(db.get("flatten_requested"))
        self.assertEqual(db.one("SELECT status FROM sys_heartbeats ORDER BY id DESC LIMIT 1")["status"], "halted")

    def test_the_feed_state_is_saved_after_the_decisions_own_refresh(self):
        market = PathMarket(flat(self.prices), D0 + 300)
        self.tick(D0 + 300, market=market, first_run=True)
        self.assertEqual(market.refreshes, 2, "the poll's refresh, then the decision's")
        saved = json.loads(self.db().get("sys_frozen_quotes"))
        self.assertEqual({a: s["refresh"] for a, s in saved.items()}, {"BTC": 2, "ETH": 2})
        fresh = PathMarket(flat(self.prices), D0 + 1200)
        self.tick(D0 + 1200, market=fresh)
        self.assertEqual(fresh.restored["BTC"]["refresh"], 2, "the next run starts from it")

    def test_the_database_file_alone_is_the_whole_state(self):
        self.tick(D0 + 300, first_run=True)
        wal = Path(f"{self.config.db_path}-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0, "checkpointed: nothing left in the WAL")
        copy = Path(tempfile.mkdtemp()) / "paper_systematic.db"
        shutil.copy(self.config.db_path, copy)  # As a state branch would carry it: the .db file and nothing else.
        conn = sqlite3.connect(copy)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sys_heartbeats").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sys_decisions").fetchone()[0], 1)
        conn.close()

    def test_a_tick_that_raises_exits_1_and_says_so(self):
        self.tick(D0 + 300, first_run=True)
        market = PathMarket(flat(self.prices), D0 + 1200)
        market.refresh = lambda: (_ for _ in ()).throw(RuntimeError("something unexpected"))
        self.assertEqual(self.tick(D0 + 1200, market=market), 1)
        self.assertIn("RuntimeError: something unexpected", self.out)
        db = self.db()
        self.assertTrue(events(db, "The scheduled tick failed"))
        self.assertEqual(len(db.all("SELECT * FROM sys_heartbeats")), 1, "no heartbeat from the failed run")
        wal = Path(f"{self.config.db_path}-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0, "still checkpointed")

    def test_a_failed_kraken_refresh_is_tried_again(self):
        market = PathMarket(flat(self.prices), D0 + 300)
        market.refresh_failures = 2
        self.assertEqual(self.tick(D0 + 300, market=market, first_run=True), 0)
        self.assertEqual(self.sleeps, [2, 3])
        db = self.db()
        self.assertEqual(json.loads(db.get("sys_api_errors"))["kraken"], 0)
        self.assertEqual(db.one("SELECT status FROM sys_heartbeats")["status"], "ok")
        db.conn.close()
        down = PathMarket(flat(self.prices), D0 + 1200)
        down.refresh_failures = 100
        self.sleeps.clear()
        self.assertEqual(self.tick(D0 + 1200, market=down), 0, "a heartbeat was written: the run itself worked")
        self.assertEqual(self.sleeps[:3], [2, 3, 5])
        self.assertEqual(self.db().one("SELECT status FROM sys_heartbeats ORDER BY id DESC LIMIT 1")["status"],
                         "degraded")

    def test_the_job_summary_and_the_pound(self):
        summary = Path(tempfile.mkdtemp()) / "summary.md"
        self.tick(D0 + 300, first_run=True, summary=summary)
        text = summary.read_text()
        self.assertTrue(text.startswith("### Paper tick at 2026-11-02 00:05:00 UTC, F1__ENS__BTC_ETH__vol40: ok"))
        self.assertIn("- Decided on the bar of 2026-11-01: 2 order(s) to work.", text)
        self.assertIn("Paper tick at 2026-11-02 00:05:00 UTC", self.out)
        self.assertEqual(json.loads(self.db().get("sys_gbpusd")), {"ts": D0 + 300, "rate": "1.25"})

    def test_live_is_refused(self):
        from dataclasses import replace
        with self.assertRaises(SystemExit), quiet():
            cli.tick_once(replace(self.config, mode="live"), market=PathMarket(flat(self.prices), D0), first_run=True)
        self.assertFalse(self.config.db_path.exists())

    def test_the_command_line(self):
        out = io.TextIOWrapper(io.BytesIO(), write_through=True)
        with mock.patch.object(config_module, "load_env"), mock.patch.object(sys, "argv", ["duo", "tick"]), \
                mock.patch.dict(os.environ, {"DUO_DATA_DIR": str(self.config.data_dir)}), \
                mock.patch.object(sys, "stdout", out), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, cli.NO_RUN)
        self.assertIn("--first-run", out.buffer.getvalue().decode())
        self.assertFalse(self.config.db_path.exists())


class Probe(unittest.TestCase):
    def urlopen(self, binance: dict, kraken_error=None):
        def fake(url, timeout=None):
            host = url.split("/")[2]
            if host in binance and isinstance(binance[host], int):
                raise urllib.error.HTTPError(url, binance[host], "Unavailable For Legal Reasons", {}, io.BytesIO())
            if "klines" in url:
                return Reply(json.dumps([[(D0 - DAY) * 1000, "1"], [D0 * 1000, "1"]]).encode())
            if kraken_error:
                return Reply(json.dumps({"error": [kraken_error]}).encode())
            if "Ticker" in url:
                return Reply(json.dumps({"error": [], "result": {"XXBTZUSD": {"c": ["70000.1", "0.1"]}}}).encode())
            return Reply(json.dumps({"error": [], "result": {"XXBTZUSD": [[D0 + 60, "1"]], "last": D0}}).encode())
        return fake

    def probe(self, *a, **kw):
        with quiet() as out:
            code = cli.probe(make_sys_config(), urlopen=self.urlopen(*a, **kw))
        return code, out.getvalue()

    def test_kraken_and_one_binance_host_are_enough(self):
        code, out = self.probe({"data-api.binance.vision": 451})
        self.assertEqual(code, 0)
        self.assertIn("data-api.binance.vision", out)
        self.assertIn("HTTP 451", out)
        self.assertIn("newest bar 2026-11-02 00:00:00 UTC", out)
        self.assertIn("newest bar 2026-11-02 00:01:00 UTC", out)
        self.assertIn("Binance answers from api.binance.com", out)

    def test_no_binance_host_or_no_kraken_is_a_failure(self):
        self.assertEqual(self.probe({"data-api.binance.vision": 451, "api.binance.com": 451})[0], 1)
        code, out = self.probe({}, kraken_error="EService:Unavailable")
        self.assertEqual(code, 1)
        self.assertIn("Kraken doesn't answer", out)


class Site(unittest.TestCase):
    def test_site_builds_the_status_page_from_the_database(self):
        env = make_env()
        step(env, D0 + 120)
        calls = []
        module = types.ModuleType("duo.site")
        module.build = lambda config, db, out_dir, now=None: calls.append((config, db, out_dir)) or {"files": 1}
        out = Path(tempfile.mkdtemp()) / "site"
        with mock.patch.dict(sys.modules, {"duo.site": module}):
            self.assertEqual(cli.build_site(env.raw_config, out), {"files": 1})
        config, db, out_dir = calls[0]
        self.assertEqual(out_dir, out)
        self.assertEqual(db.path, env.config.db_path)


def cron_schedule(first_day: int, days: int, seed: int = 7) -> list[float]:
    """When GitHub would start the jobs of '5,20,35,50 0-3 * * *', '35 4-23 * * *' and '47 23 * * *': each up to 20
    minutes late, now and then an hourly one not at all (never two in a row: that's 3 hours, an outage), and one at a
    time (a concurrency group), about a minute each."""
    rng = random.Random(seed)
    starts = []
    for day in range(first_day, first_day + days * DAY, DAY):
        slots = [day + h * 3600 + m * 60 for h in range(4) for m in (5, 20, 35, 50)]
        hourly = [day + h * 3600 + 35 * 60 for h in range(4, 24)]
        skipped = {t for t in hourly[1::2] if rng.random() < 0.2}
        slots += [t for t in hourly if t not in skipped]
        slots.append(day + 23 * 3600 + 47 * 60)
        starts += [t + rng.uniform(0, 20 * 60) for t in slots]
    runs = []
    for t in sorted(starts):
        runs.append(max(t, runs[-1] + 60) if runs else t)
    return runs


class ThreeScheduledDays(unittest.TestCase):
    """Every run is a fresh process: a new market, Binance client, database connection, broker, runner and engine, on
    the one database file, at the times a GitHub schedule would start them."""

    def test_three_days_of_scheduled_runs(self):
        config = make_sys_config()
        bars = default_bars()
        # ETH falls 12% on the first day, so the second day's decision sells some.
        bars["ETH"] = [(t, o, h, low * 0.88, c * 0.88, v, q) if t == D0 else
                       (t, o * 0.88, h * 0.88, low * 0.88, c * 0.88, v, q * 0.88) if t > D0 else (t, o, h, low, c, v, q)
                       for t, o, h, low, c, v, q in bars["ETH"]]
        closes = {a: {r[0]: r[4] for r in rows} for a, rows in bars.items()}

        def path(asset, t):
            day = int(t - t % DAY)
            base, minutes = closes[asset][day - DAY], (t - day) / 60
            if asset == "BTC":  # Swings 0.2% either way every half hour: a buy resting at the bid fills within two.
                return base * (1 + 0.002 * math.sin(2 * math.pi * minutes / 30))
            return base * (1 + 0.0002 * min(minutes, 180))  # ETH climbs for three hours: away from a buy, into a sell.

        runs = cron_schedule(D0, 3)
        self.assertLess(max(b - a for a, b in zip(runs, runs[1:])), config.systematic.cron_gap_minutes * 60,
                        "late and skipped runs, but no outage")
        for i, now in enumerate(runs):
            with quiet():
                code = cli.tick_once(config, market=PathMarket(path, now), binance=FakeBinance(bars, now), now=now,
                                     first_run=i == 0, sleep=lambda s: None)
            self.assertEqual(code, 0, f"run {i} at {now}")
        db = DB(config.db_path)

        # One decision a day, at the day's first run from 00:02. The last 23:47 job can start after midnight.
        firsts = {}
        for t in runs:
            if t % DAY >= 120:
                firsts.setdefault(int(t - t % DAY), t)
        decisions = db.all("SELECT day_ts, decided_at FROM sys_decisions ORDER BY id")
        self.assertEqual([(d["day_ts"], d["decided_at"]) for d in decisions],
                         [(day - DAY, t) for day, t in sorted(firsts.items())])
        self.assertGreaterEqual(len(decisions), 3)
        beats = db.all("SELECT * FROM sys_heartbeats ORDER BY id")
        self.assertEqual([b["ts"] for b in beats], runs, "a heartbeat every run")
        self.assertEqual({b["status"] for b in beats}, {"ok"})
        self.assertTrue(all(b["loop_gap_s"] is None for b in beats))
        self.assertEqual(db.all("SELECT * FROM sys_children WHERE status = 'missed'"), [])
        self.assertFalse(events(db, "No loop ran"))

        makers = db.all("SELECT f.*, c.note FROM fills f JOIN sys_children c ON c.venue_id = f.venue_id "
                        "WHERE f.liquidity = 'maker'")
        self.assertEqual({(f["asset"], f["side"]) for f in makers}, {("BTC", "buy"), ("ETH", "sell")},
                         "resting orders fill on the candle path")
        for fill in makers:
            self.assertIn("Kraken's 1-minute candle starting", fill["note"])
            self.assertEqual(fill["ts"] % 60, 0)

        slack = config.systematic.taker_after_minutes * 60 - config.systematic.cron_slack_seconds
        crossed = 0
        for order in db.all("SELECT * FROM sys_orders WHERE source = 'decision'"):
            due = next(t for t in runs if t - order["created_at"] >= slack)
            children = db.all("SELECT * FROM sys_children WHERE order_id = ? ORDER BY id", (order["id"],))
            self.assertFalse([c for c in children if c["kind"] == "maker" and c["placed_at"] >= due])
            if order["closed_at"] is None or order["closed_at"] >= due:  # Still to do when taker time came.
                self.assertEqual([c for c in children if c["kind"] == "taker"][0]["placed_at"], due)
                crossed += 1
        self.assertGreater(crossed, 0, "ETH never came down to its resting buy, so it crossed at taker time")
        self.assertEqual(db.all("SELECT * FROM sys_orders WHERE status = 'working'"), [])


if __name__ == "__main__":
    unittest.main()
