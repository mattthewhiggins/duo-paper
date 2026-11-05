"""Resting orders and price-triggered plans, end to end in paper mode."""
import time
import unittest
from decimal import Decimal as Dec

from duo.brokers import PaperBroker, RestingStatus

from .helpers import buy, make_config, make_engine, risk, run_ticks, sell, strategist


def fills(engine):
    return engine.db.all("SELECT * FROM fills ORDER BY id")


def agree(*orders, block="s1", strategy="Plan.", **kwargs):
    return [strategist(orders=list(orders), strategy=strategy, block_id=block, **kwargs), risk("approve", block_id=block + "r")]


def resting_buy(**kwargs):
    return buy(**{"execution": "resting", "target": 90000, **kwargs})


def dip_plan(**kwargs):
    defaults = dict(trigger=75000, stop=65000, target=90000, execution="resting")
    return buy(**{**defaults, **kwargs})


def engine_with(*responses, **kwargs):
    engine, client = make_engine(list(responses), **kwargs)
    run_ticks(engine)
    return engine, client


def wake(engine):
    engine.db.set("wake_requested", "1")


class RestingOrders(unittest.TestCase):
    def test_nothing_is_booked_until_the_market_trades_through(self):
        engine, _ = engine_with(*agree(resting_buy()))
        order = engine.db.one("SELECT * FROM resting_orders")
        self.assertEqual((order["status"], order["limit_price"]), ("open", "79999.5"))
        self.assertEqual(fills(engine), [])
        self.assertIsNone(engine.db.one("SELECT * FROM stops"))

        engine.market.prices["BTC"] = 79995  # Touching isn't enough.
        run_ticks(engine)
        self.assertEqual(fills(engine), [])

        engine.market.prices["BTC"] = 79970
        run_ticks(engine)
        trade = fills(engine)[0]
        self.assertEqual(trade["liquidity"], "maker")
        self.assertEqual(D_(trade["price"]), Dec("79999.5"))  # Fills at its own price, with no slippage.
        self.assertAlmostEqual(float(trade["fee"]) / float(trade["gross"]), 0.004, places=8)
        self.assertEqual(engine.db.one("SELECT stop_price FROM stops")["stop_price"], "72000")
        self.assertEqual(engine.db.one("SELECT status FROM resting_orders")["status"], "filled")

    def test_order_that_never_fills_is_cancelled_and_recorded_as_missed(self):
        engine, _ = engine_with(*agree(resting_buy()))
        engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM resting_orders")["status"], "missed")
        self.assertEqual(fills(engine), [])
        self.assertTrue(engine.db.one("SELECT * FROM events WHERE text LIKE '%didn''t fill%'"))
        self.assertEqual(engine.resting_stats(), {"closed": 1, "filled": 0, "fees_saved": Dec(0)})
        self.assertIn("Missed in the last day", engine.build_brief("test"))

    def test_waiting_buys_reserve_cash_exposure_and_a_buy_slot(self):
        engine, client = engine_with(*agree(resting_buy(amount=95)))
        self.assertEqual(engine.buys_today(time.time()), 1)
        ctx = engine.risk_context(time.time())
        self.assertGreater(ctx.reserved_cash, Dec(95))
        self.assertGreater(ctx.reserved_qty["BTC"], 0)

    def test_a_filled_order_isnt_double_counted_as_a_buy(self):
        engine, _ = engine_with(*agree(resting_buy()))
        engine.market.prices["BTC"] = 79970
        run_ticks(engine)
        self.assertEqual(engine.buys_today(time.time()), 1)
        self.assertEqual(engine.risk_context(time.time()).reserved_cash, 0)

    def test_stats_show_fill_rate_and_fees_saved(self):
        engine, _ = engine_with(*agree(resting_buy()))
        engine.market.prices["BTC"] = 79970
        run_ticks(engine)
        stats = engine.resting_stats()
        self.assertEqual((stats["closed"], stats["filled"]), (1, 1))
        self.assertAlmostEqual(float(stats["fees_saved"]), float(fills(engine)[0]["gross"]) * 0.004, places=6)

    def test_agent_sell_can_rest_and_cancels_whats_in_the_way(self):
        engine, client = engine_with(*agree(buy(target=85000)))
        engine.market.prices["BTC"] = 86000  # Puts a resting target sell in the book.
        run_ticks(engine)
        self.assertEqual(len(engine.open_resting("BTC")), 1)
        client.responses += agree(sell(), block="s2", strategy=None)
        wake(engine)
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        self.assertEqual(engine.open_resting("BTC"), [])

    def test_stop_cancels_a_resting_target_then_sells_immediately(self):
        engine, _ = engine_with(*agree(buy(target=85000)))
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(len(engine.open_resting("BTC")), 1)
        engine.market.prices["BTC"] = 71000
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual([t["source"] for t in trades], ["agents", "stop"])
        self.assertEqual(trades[1]["liquidity"], "taker")
        self.assertEqual(engine.open_resting("BTC"), [])
        self.assertEqual(engine.db.one("SELECT status FROM resting_orders")["status"], "missed")

    def test_target_tries_again_if_the_first_offer_expires(self):
        engine, _ = engine_with(*agree(buy(target=85000)))
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        run_ticks(engine)
        rows = engine.db.all("SELECT status FROM resting_orders ORDER BY id")
        self.assertEqual([r["status"] for r in rows], ["missed", "open"])

    def test_target_stands_down_if_the_price_falls_back(self):
        engine, _ = engine_with(*agree(buy(target=85000)))
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        engine.market.prices["BTC"] = 84000
        engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        run_ticks(engine)
        self.assertEqual(engine.open_resting("BTC"), [])
        self.assertGreater(engine.portfolio().qty("BTC"), 0)
        self.assertEqual(engine.db.one("SELECT target_price FROM stops")["target_price"], "85000")

    def test_halt_cancels_waiting_buys_but_not_waiting_sells(self):
        engine, _ = engine_with(*agree(buy(target=85000), resting_buy(asset="ETH", stop=3600, target=4500)))
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(len(engine.open_resting()), 2)
        engine.halt("drill")
        run_ticks(engine)
        remaining = engine.open_resting()
        self.assertEqual([(o["asset"], o["side"]) for o in remaining], [("BTC", "sell")])

    def test_flatten_cancels_everything_first(self):
        engine, _ = engine_with(*agree(buy(target=85000), resting_buy(asset="ETH", stop=3600, target=4500), dip_plan(asset="SOL", trigger=190, stop=170, target=230)))
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        engine.request_flatten()
        run_ticks(engine)
        self.assertEqual(engine.open_resting(), [])
        self.assertEqual(engine.waiting_plans(), [])
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        self.assertIsNone(engine.db.get("flatten_requested"))

    def test_partial_fill_counts_towards_position_and_later_pieces_dont_double_book(self):
        class Partial(PaperBroker):
            status = RestingStatus(Dec(0), Dec(0), Dec(0), False)
            def check_resting(self, order, quote):
                return Partial.status
            def cancel_resting(self, order, quote):
                return RestingStatus(Partial.status.qty, Partial.status.gross, Partial.status.fee, True)
        config = make_config()
        engine, _ = engine_with(*agree(resting_buy()), config=config, broker=Partial(config))
        Partial.status = RestingStatus(Dec("0.0004"), Dec(32), Dec("0.128"), False)
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC"), Dec("0.0004"))
        reserved = engine.risk_context(time.time()).reserved_qty["BTC"]
        self.assertEqual(reserved, Dec(str(engine.db.one("SELECT qty FROM resting_orders")["qty"])) - Dec("0.0004"))
        engine.market.prices["BTC"] = 71000
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC"), 0)  # The stop protects the part that filled.


def D_(value):
    return Dec(str(value))


class Plans(unittest.TestCase):
    def planned(self, *orders, **kwargs):
        engine, client = engine_with(*agree(*(orders or [dip_plan()])), **kwargs)
        self.requests_after_session = len(client.requests)
        return engine, client

    def test_plan_is_created_only_after_approval(self):
        engine, _ = engine_with(strategist(orders=[dip_plan()], strategy="Plan."), risk("reject"))
        self.assertEqual(engine.waiting_plans(), [])
        engine, _ = engine_with(*agree(dip_plan()))
        plan = engine.waiting_plans()[0]
        self.assertEqual((plan["status"], plan["direction"], plan["trigger_price"]), ("active", "below", "75000"))

    def test_blocked_plan_isnt_created_and_the_agents_are_told(self):
        engine, _ = engine_with(*agree(dip_plan(stop=74999)))
        self.assertEqual(engine.waiting_plans(), [])
        self.assertIn("Blocked plan", engine.db.one("SELECT text FROM messages WHERE speaker = 'system'")["text"])
        self.assertIn("% away", engine.db.one("SELECT orders FROM messages WHERE speaker = 'strategist'")["orders"])

    def test_hours_are_clamped_to_the_limit(self):
        engine, _ = engine_with(*agree(dip_plan(hours=9999)))
        left = engine.waiting_plans()[0]["expires_at"] - time.time()
        self.assertLessEqual(left, 72 * 3600)
        self.assertGreater(left, 71 * 3600)

    def test_plan_does_nothing_until_the_price_arrives(self):
        engine, client = self.planned()
        engine.market.prices["BTC"] = 76000
        run_ticks(engine, 3)
        self.assertEqual(engine.open_resting(), [])
        self.assertEqual(fills(engine), [])

    def test_dip_plan_rests_when_triggered_and_needs_no_api_call(self):
        engine, client = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        self.assertEqual(engine.waiting_plans()[0]["status"], "working")
        order = engine.open_resting()[0]
        self.assertEqual((order["side"], order["source"], order["limit_price"]), ("buy", "plan", "74989.5"))
        engine.market.prices["BTC"] = 74960
        run_ticks(engine)
        trade = fills(engine)[0]
        self.assertEqual((trade["source"], trade["liquidity"]), ("plan", "maker"))
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "filled")
        self.assertEqual(engine.db.one("SELECT target_price FROM stops")["target_price"], "90000")
        self.assertEqual(len(client.requests), self.requests_after_session)

    def test_breakout_plan_buys_immediately(self):
        engine, _ = self.planned(buy(trigger=85000, stop=80000, target=95000, execution="immediate"))
        self.assertEqual(engine.waiting_plans()[0]["direction"], "above")
        engine.market.prices["BTC"] = 84000
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        engine.market.prices["BTC"] = 85100
        run_ticks(engine)
        trade = fills(engine)[0]
        self.assertEqual((trade["source"], trade["liquidity"]), ("plan", "taker"))
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "filled")

    def test_missed_order_rearms_the_plan(self):
        engine, _ = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        engine.market.prices["BTC"] = 76000  # It bounced away before filling.
        engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "active")
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "working")
        self.assertEqual(len(engine.db.all("SELECT * FROM resting_orders")), 2)

    def test_plan_expires(self):
        engine, _ = self.planned()
        engine.db.execute("UPDATE plans SET expires_at = 0")
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "expired")
        engine.market.prices["BTC"] = 74000
        run_ticks(engine)
        self.assertEqual(engine.open_resting(), [])

    def test_order_missed_after_plan_expiry_closes_the_plan(self):
        engine, _ = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        engine.db.execute("UPDATE plans SET expires_at = 0")
        engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        engine.market.prices["BTC"] = 76000
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "expired")

    def test_limits_apply_again_when_a_plan_fires_and_a_block_is_logged_once(self):
        engine, _ = self.planned(dip_plan(stop=70000))
        engine.market.prices["BTC"] = 69000  # It gapped straight through the plan's stop.
        run_ticks(engine, 3)
        self.assertEqual(fills(engine), [])
        self.assertEqual(engine.open_resting(), [])
        plan = engine.db.one("SELECT * FROM plans")
        self.assertEqual(plan["status"], "active")
        self.assertIn("stop", plan["note"])
        self.assertEqual(engine.db.one("SELECT COUNT(*) AS n FROM events WHERE kind = 'plan' AND text LIKE '%blocked%'")["n"], 1)
        self.assertIn("Blocked when it triggered", engine.build_brief("test"))

    def test_block_clears_if_the_price_recovers_into_range(self):
        engine, _ = self.planned(dip_plan(stop=70000))
        engine.market.prices["BTC"] = 69000
        run_ticks(engine)
        engine.market.prices["BTC"] = 74000
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "working")

    def test_plans_wait_during_a_halt_and_continue_after(self):
        engine, _ = self.planned()
        engine.halt("drill")
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        self.assertEqual(engine.open_resting(), [])
        self.assertEqual(engine.waiting_plans()[0]["status"], "active")
        engine.resume()
        engine.db.set("next_check_in", time.time() + 9999)
        run_ticks(engine)
        self.assertEqual(engine.waiting_plans()[0]["status"], "working")

    def test_halt_sends_a_working_plan_back_to_waiting(self):
        engine, _ = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        engine.halt("drill")
        run_ticks(engine)
        self.assertEqual(engine.open_resting(), [])
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "active")

    def test_plans_fire_when_the_api_budget_is_used_up(self):
        engine, client = self.planned()
        engine.db.execute(
            "INSERT INTO api_usage(ts, session_id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
            "VALUES(?, NULL, 'x', 0, 0, 0, 0, 999)", (time.time(),))
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "working")

    def test_stop_loss_cancels_plans_on_that_asset_only(self):
        engine, _ = self.planned(buy(amount=80, target=90000), dip_plan(trigger=74000, stop=65000), dip_plan(asset="ETH", trigger=3800, stop=3400, target=4500))
        self.assertEqual(len(engine.waiting_plans()), 2)
        engine.market.prices["BTC"] = 71000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        self.assertEqual([p["asset"] for p in engine.waiting_plans()], ["ETH"])
        cancelled = engine.db.one("SELECT note FROM plans WHERE status = 'cancelled'")
        self.assertIn("stop-loss", cancelled["note"])

    def test_agents_can_cancel_a_plan(self):
        engine, client = self.planned()
        plan_id = engine.waiting_plans()[0]["id"]
        self.assertIn(f"Plan {plan_id}", engine.build_brief("test"))
        client.responses += agree(block="s2", strategy=None, cancel=[plan_id])
        wake(engine)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "cancelled")
        self.assertEqual(engine.db.one("SELECT orders FROM messages WHERE speaker = 'strategist' ORDER BY id DESC LIMIT 1")["orders"].count("cancel_plans"), 1)

    def test_cancelling_a_working_plan_cancels_its_order(self):
        engine, _ = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        self.assertTrue(engine.cancel_plan(engine.waiting_plans()[0]["id"]))
        self.assertEqual(engine.open_resting(), [])
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "cancelled")
        self.assertFalse(engine.cancel_plan(99))

    def test_cancelling_frees_a_plan_slot_in_the_same_proposal(self):
        orders = [dip_plan(trigger=75000 - i, hours=1) for i in range(5)]
        engine, client = self.planned(*orders)
        self.assertEqual(len(engine.waiting_plans()), 5)
        first = engine.waiting_plans()[0]["id"]
        client.responses += agree(dip_plan(trigger=74000), block="s2", strategy=None, cancel=[first])
        wake(engine)
        run_ticks(engine)
        self.assertEqual(len(engine.waiting_plans()), 5)
        self.assertEqual(engine.waiting_plans()[-1]["trigger_price"], "74000")

    def test_risk_manager_is_consulted_while_plans_are_waiting(self):
        engine, client = self.planned()
        client.responses += [strategist(block_id="s2"), risk("approve", block_id="r2")]
        wake(engine)
        run_ticks(engine)
        self.assertEqual(len(client.requests), self.requests_after_session + 2)

    def test_restart_keeps_plans_and_orders_working(self):
        engine, _ = self.planned()
        engine.market.prices["BTC"] = 74990
        run_ticks(engine)
        engine._recover()
        engine.market.prices["BTC"] = 74960
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT status FROM plans")["status"], "filled")

    def test_failed_placement_waits_before_trying_again(self):
        from duo.brokers import OrderRejected
        calls = []
        class Rejecting(PaperBroker):
            def place_resting(self, ticket, quote, ttl):
                calls.append(1)
                raise OrderRejected("post only")
        config = make_config()
        engine, _ = engine_with(*agree(dip_plan()), config=config, broker=Rejecting(config))
        engine.market.prices["BTC"] = 74990
        run_ticks(engine, 3)
        self.assertEqual(len(calls), 1)


class Migration(unittest.TestCase):
    def test_a_database_from_before_this_version_gets_the_new_columns(self):
        import sqlite3, tempfile
        from pathlib import Path
        from duo.db import DB
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY, ts REAL NOT NULL, session_id INTEGER, source TEXT NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL, gross TEXT NOT NULL, fee TEXT NOT NULL, realized TEXT NOT NULL, venue_id TEXT, reason TEXT)")
        conn.execute("CREATE TABLE stops (asset TEXT PRIMARY KEY, stop_price TEXT NOT NULL, set_at REAL NOT NULL, session_id INTEGER)")
        conn.commit(); conn.close()
        db = DB(path)
        self.assertIn("liquidity", {r["name"] for r in db.all("PRAGMA table_info(fills)")})
        self.assertIn("target_price", {r["name"] for r in db.all("PRAGMA table_info(stops)")})


if __name__ == "__main__":
    unittest.main()
