"""Ledger, Kraken signing and the hard limits."""
import time
import unittest
from decimal import Decimal as Dec

from duo.kraken import sign
from duo.ledger import Portfolio
from duo.risk import Order, Rejected, RiskContext, RiskGate

from .helpers import make_config, make_pairs, quote


class KrakenSigning(unittest.TestCase):
    def test_matches_krakens_documented_example(self):
        signature = sign(
            "/0/private/AddOrder",
            "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
            1616492376594,
            "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==",
        )
        self.assertEqual(signature, "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")


class Ledger(unittest.TestCase):
    def test_round_trip_profit_includes_both_fees(self):
        p = Portfolio(starting=Dec(1000), cash=Dec(1000))
        p.apply("BTC", "buy", Dec("0.01"), Dec(800), Dec("6.4"))
        self.assertEqual(p.cash, Dec("193.6"))
        realized = p.apply("BTC", "sell", Dec("0.01"), Dec(900), Dec("7.2"))
        self.assertEqual(realized, Dec("86.4"))  # 900 - 7.2 - (800 + 6.4)
        self.assertEqual(p.cash, Dec("1086.4"))
        self.assertEqual(p.fees, Dec("13.6"))
        self.assertEqual(p.qty("BTC"), 0)

    def test_partial_sell_uses_average_cost(self):
        p = Portfolio(starting=Dec(1000), cash=Dec(1000))
        p.apply("ETH", "buy", Dec(1), Dec(100), Dec(0))
        p.apply("ETH", "buy", Dec(1), Dec(300), Dec(0))
        self.assertEqual(p.positions["ETH"].avg_cost, Dec(200))
        self.assertEqual(p.apply("ETH", "sell", Dec(1), Dec(250), Dec(0)), Dec(50))
        self.assertEqual(p.positions["ETH"].cost, Dec(200))

    def test_equity_always_equals_start_plus_realized_plus_unrealized(self):
        p = Portfolio(starting=Dec(1000), cash=Dec(1000))
        p.apply("BTC", "buy", Dec("0.002"), Dec(160), Dec("1.28"))
        p.apply("ETH", "buy", Dec("0.05"), Dec(200), Dec("1.6"))
        p.apply("BTC", "sell", Dec("0.001"), Dec(85), Dec("0.68"))
        marks = {"BTC": Dec(81000), "ETH": Dec(3900)}
        self.assertAlmostEqual(p.equity(marks), p.starting + p.realized + p.unrealized(marks), places=12)

    def test_cant_sell_more_than_held(self):
        p = Portfolio(starting=Dec(1000), cash=Dec(1000))
        p.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        with self.assertRaises(ValueError):
            p.apply("BTC", "sell", Dec("0.002"), Dec(160), Dec(0))

    def test_replay_matches_live_books(self):
        fills = [
            {"asset": "BTC", "side": "buy", "qty": "0.002", "gross": "160", "fee": "1.28"},
            {"asset": "BTC", "side": "sell", "qty": "0.002", "gross": "170", "fee": "1.36"},
        ]
        p = Portfolio.replay(Dec(1000), fills)
        self.assertEqual(p.cash, Dec("1007.36"))
        self.assertEqual(p.realized, Dec("7.36"))


class Limits(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.gate = RiskGate(self.config.risk, self.config.maker_pct, self.config.taker_pct, self.config.assets, make_pairs())
        self.portfolio = Portfolio(starting=Dec(1000), cash=Dec(1000))
        self.quotes = {"BTC": quote("BTC", 80000), "ETH": quote("ETH", 4000), "SOL": quote("SOL", 200)}
        self.ctx = RiskContext(now=time.time())

    def buy(self, amount="80", stop="72000", asset="BTC", target="90000", execution="immediate", trigger=None):
        return Order(asset, "buy", Dec(amount), None, Dec(stop) if stop else None, "test", Dec(target) if target else None,
                     execution, Dec(trigger) if trigger else None)

    def rejects(self, order, ctx=None, match=""):
        with self.assertRaises(Rejected) as caught:
            self.gate.size(order, self.portfolio, self.quotes, ctx or self.ctx)
        self.assertIn(match, str(caught.exception))

    def test_valid_buy_is_sized_down_to_lot_size(self):
        ticket = self.gate.size(self.buy(), self.portfolio, self.quotes, self.ctx)
        self.assertEqual(ticket.side, "buy")
        self.assertLessEqual(ticket.qty * self.quotes["BTC"].ask, Dec(80))
        self.assertEqual(ticket.qty.as_tuple().exponent, -8)
        self.assertGreater(ticket.limit_price, self.quotes["BTC"].ask)

    def test_order_size_limit(self):
        self.rejects(self.buy(amount="101"), match="per-order limit")

    def test_buy_needs_a_stop(self):
        self.rejects(self.buy(stop=None), match="needs a stop_price")

    def test_buy_needs_a_target(self):
        self.rejects(self.buy(target=None), match="needs a target_price")

    def test_target_has_to_clear_the_fees(self):
        # An immediate buy pays 0.80% in and 0.40% out at the target. The default multiple of 2 makes that 2.4%.
        self.rejects(self.buy(target="81900"), match="to justify the fees")
        ticket = self.gate.size(self.buy(target="82000"), self.portfolio, self.quotes, self.ctx)
        self.assertEqual(ticket.target_price, Dec("82000"))

    def test_resting_buys_get_a_lower_target_hurdle(self):
        # A resting buy pays 0.40% each way, so twice that is 1.6%.
        self.rejects(self.buy(target="81200", execution="resting"), match="to justify the fees")
        self.gate.size(self.buy(target="81300", execution="resting"), self.portfolio, self.quotes, self.ctx)
        self.rejects(self.buy(target="81300"), match="to justify the fees")

    def test_resting_buy_rests_at_the_bid_below_the_ask(self):
        ticket = self.gate.size(self.buy(execution="resting"), self.portfolio, self.quotes, self.ctx)
        self.assertEqual(ticket.execution, "resting")
        self.assertEqual(ticket.limit_price, Dec("79999.5"))
        self.assertLess(ticket.limit_price, self.quotes["BTC"].ask)

    def test_resting_sell_rests_at_the_ask_above_the_bid(self):
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        ticket = self.gate.size(Order("BTC", "sell", None, Dec(1), None, "x", None, "resting"), self.portfolio, self.quotes, self.ctx)
        self.assertEqual(ticket.limit_price, Dec("80000.5"))
        self.assertGreater(ticket.limit_price, self.quotes["BTC"].bid)

    def test_locked_market_leaves_no_room_to_rest(self):
        q = self.quotes["BTC"]
        self.quotes["BTC"] = type(q)("BTC", Dec(80000), Dec(80000), Dec(80000), time.time())
        self.rejects(self.buy(execution="resting"), match="no room")

    def test_unknown_execution(self):
        self.rejects(self.buy(execution="sideways"), match="unknown execution")

    def test_resting_buys_reserve_their_cash(self):
        self.portfolio = Portfolio(starting=Dec(1000), cash=Dec(1000))
        ctx = RiskContext(now=time.time(), reserved_cash=Dec(750))
        self.rejects(self.buy(), ctx, match="below the 20")

    def test_resting_buys_count_towards_the_asset_limit(self):
        ctx = RiskContext(now=time.time(), reserved_qty={"BTC": Dec("0.0040")})
        self.rejects(self.buy(), ctx, match="over the 35")

    def test_fee_budget_blocks_buys_but_counts_the_whole_round_trip(self):
        # Budget is 3% of 1000 = 30. An 80 buy costs about 1.29 in round-trip fees.
        allowed = RiskContext(now=time.time(), fees_30d=Dec("28"))
        self.gate.size(self.buy(), self.portfolio, self.quotes, allowed)
        blocked = RiskContext(now=time.time(), fees_30d=Dec("29"))
        self.rejects(self.buy(), blocked, match="budget")

    def test_fee_budget_never_blocks_sells(self):
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        ctx = RiskContext(now=time.time(), fees_30d=Dec(500))
        self.assertEqual(self.gate.size(Order("BTC", "sell", None, Dec(1)), self.portfolio, self.quotes, ctx).qty, Dec("0.001"))

    def test_precheck_counts_fees_across_the_batch(self):
        ctx = RiskContext(now=time.time(), fees_30d=Dec("28.5"))
        results = self.gate.precheck([self.buy(), self.buy()], self.portfolio, self.quotes, ctx)
        self.assertIsNone(results[0])
        self.assertIn("budget", results[1])

    def plan(self, **kwargs):
        defaults = dict(amount="80", stop="65000", target="90000", execution="resting", trigger="75000")
        return self.buy(**{**defaults, **kwargs})

    def test_plan_below_the_price(self):
        self.assertEqual(self.gate.check_plan(self.plan(), self.portfolio, self.quotes, self.ctx), "below")

    def test_plan_above_the_price_has_to_be_immediate(self):
        with self.assertRaises(Rejected) as caught:
            self.gate.check_plan(self.plan(trigger="85000", stop="80000", target="95000"), self.portfolio, self.quotes, self.ctx)
        self.assertIn("has to be immediate", str(caught.exception))
        direction = self.gate.check_plan(self.plan(trigger="85000", stop="80000", target="95000", execution="immediate"),
                                         self.portfolio, self.quotes, self.ctx)
        self.assertEqual(direction, "above")

    def test_plan_stop_and_target_are_judged_from_the_trigger(self):
        for bad, match in ((dict(stop="76000"), "below the price"), (dict(target="75500"), "to justify the fees"),
                           (dict(trigger="30000", stop="28000", target="40000"), "% away"), (dict(amount="500"), "per-order limit"),
                           (dict(amount="0.5"), "minimum"), (dict(target=None), "needs a target"), (dict(stop=None), "needs a stop")):
            with self.assertRaises(Rejected, msg=bad) as caught:
                self.gate.check_plan(self.plan(**bad), self.portfolio, self.quotes, self.ctx)
            self.assertIn(match, str(caught.exception))

    def test_plan_cap_and_halt(self):
        with self.assertRaises(Rejected):
            self.gate.check_plan(self.plan(), self.portfolio, self.quotes, RiskContext(now=time.time(), active_plans=5))
        with self.assertRaises(Rejected):
            self.gate.check_plan(self.plan(), self.portfolio, self.quotes, RiskContext(now=time.time(), halted=True, halt_reason="x"))

    def test_only_buys_can_be_plans(self):
        order = Order("BTC", "sell", None, Dec(1), None, "x", None, "resting", Dec(75000))
        results = self.gate.precheck([order], self.portfolio, self.quotes, self.ctx)
        self.assertIn("only buys", results[0])

    def test_precheck_counts_new_plans_against_the_cap(self):
        ctx = RiskContext(now=time.time(), active_plans=4)
        results = self.gate.precheck([self.plan(), self.plan()], self.portfolio, self.quotes, ctx)
        self.assertIsNone(results[0])
        self.assertIn("allowed plans", results[1])

    def test_stop_update_target_only_has_to_beat_the_price(self):
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        self.gate.check_stop_update("BTC", Dec("75000"), self.portfolio, self.quotes, self.ctx, Dec("80500"))
        with self.assertRaises(Rejected):
            self.gate.check_stop_update("BTC", Dec("75000"), self.portfolio, self.quotes, self.ctx, Dec("79000"))

    def test_stop_above_price(self):
        self.rejects(self.buy(stop="90000"), match="below the price")

    def test_stop_too_close_or_too_far(self):
        self.rejects(self.buy(stop="79500"), match="% away")
        self.rejects(self.buy(stop="40000"), match="% away")

    def test_unknown_asset(self):
        self.rejects(self.buy(asset="DOGE"), match="allowed list")

    def test_negative_nan_and_infinite_amounts(self):
        for bad in ("-5", "0", "NaN", "Infinity"):
            self.rejects(self.buy(amount=bad), match="positive quote_amount")

    def test_daily_buy_cap(self):
        self.rejects(self.buy(), RiskContext(now=time.time(), buys_today=4), match="allowed buys")

    def test_buys_paused(self):
        self.rejects(self.buy(), RiskContext(now=time.time(), buys_paused=True), match="paused")

    def test_halt_blocks_everything(self):
        ctx = RiskContext(now=time.time(), halted=True, halt_reason="drill")
        self.rejects(self.buy(), ctx, match="halted")
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        self.rejects(Order("BTC", "sell", None, Dec(1)), ctx, match="halted")

    def test_stale_price(self):
        self.quotes["BTC"] = quote("BTC", 80000, ts=time.time() - 3600)
        self.rejects(self.buy(), match="stale")

    def test_crossed_quote(self):
        q = self.quotes["BTC"]
        self.quotes["BTC"] = type(q)("BTC", Dec(81000), Dec(80000), Dec(80000), time.time())
        self.rejects(self.buy(), match="looks wrong")

    def test_cash_floor(self):
        self.portfolio.cash = Dec(250)
        self.portfolio.apply("ETH", "buy", Dec("0.1875"), Dec(750), Dec(0))
        self.portfolio.cash += Dec(750)  # 250 cash, 750 of ETH
        self.rejects(self.buy(amount="60"), match="below the 20")

    def test_asset_concentration(self):
        self.portfolio.apply("BTC", "buy", Dec("0.0040"), Dec(320), Dec(0))
        self.rejects(self.buy(amount="80"), match="over the 35")

    def test_below_kraken_minimum(self):
        self.rejects(self.buy(amount="1"), match="minimum")

    def test_sell_needs_a_position_and_valid_fraction(self):
        self.rejects(Order("BTC", "sell", None, Dec(1)), match="no BTC held")
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        for bad in ("0", "1.5", "-1"):
            self.rejects(Order("BTC", "sell", None, Dec(bad)), match="sell_fraction")

    def test_sell_never_exceeds_holding_or_strands_dust(self):
        self.portfolio.apply("BTC", "buy", Dec("0.00100001"), Dec(80), Dec(0))
        ticket = self.gate.size(Order("BTC", "sell", None, Dec("0.99")), self.portfolio, self.quotes, self.ctx)
        self.assertEqual(ticket.qty, Dec("0.00100001"))  # The remainder would be unsellable, so it all goes.

    def test_sells_still_work_when_buys_are_blocked(self):
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        ctx = RiskContext(now=time.time(), buys_paused=True, buys_today=99)
        ticket = self.gate.size(Order("BTC", "sell", None, Dec("0.5")), self.portfolio, self.quotes, ctx)
        self.assertEqual(ticket.qty, Dec("0.0005"))

    def test_precheck_counts_earlier_orders_in_the_batch(self):
        orders = [self.buy() for _ in range(5)]
        results = self.gate.precheck(orders, self.portfolio, self.quotes, self.ctx)
        self.assertEqual([r is None for r in results], [True, True, True, True, False])
        self.assertIn("allowed buys", results[4])

    def test_precheck_runs_sells_first_so_they_free_up_room(self):
        self.portfolio.apply("BTC", "buy", Dec("0.00437"), Dec(350), Dec(0))
        orders = [self.buy(amount="60"), Order("BTC", "sell", None, Dec(1), None, "sell")]
        self.assertEqual(self.gate.precheck(orders, self.portfolio, self.quotes, self.ctx), [None, None])

    def test_exit_ticket_ignores_halt_but_has_a_price_floor(self):
        self.portfolio.apply("BTC", "buy", Dec("0.001"), Dec(80), Dec(0))
        ticket = self.gate.exit_ticket("BTC", self.portfolio, self.quotes["BTC"], "stop")
        self.assertEqual(ticket.qty, Dec("0.001"))
        self.assertLess(ticket.limit_price, self.quotes["BTC"].bid)
        self.assertGreaterEqual(ticket.limit_price, self.quotes["BTC"].bid * Dec("0.98"))


if __name__ == "__main__":
    unittest.main()
