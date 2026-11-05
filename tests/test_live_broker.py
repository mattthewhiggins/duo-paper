"""The live code path against a local fake of Kraken's private API. Nothing here reaches Kraken."""
import json
import threading
import time
import unittest
from decimal import Decimal as Dec
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from duo.brokers import KrakenBroker, OrderRejected, OrderStateUnknown, preflight
from duo.db import DB
from duo.engine import Engine
from duo.kraken import KrakenClient, sign
from duo.ledger import Portfolio
from duo.risk import Ticket

from .helpers import FakeMarket, make_config, quote

SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="


class FakeKraken:
    """Answers private calls and records them, checking every signature the way Kraken would."""

    def __init__(self):
        self.calls = []
        self.withdraw_allowed = False
        self.balance = {"ZUSD": "5000", "XXBT": "1"}
        self.add_order_error = None
        self.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.64", "price": "80000"}
        self.denied = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                params = dict(parse_qsl(body))
                path = urlparse(self.path).path
                expected = sign(path, body, params["nonce"], SECRET)
                if self.headers["API-Sign"] != expected or self.headers["API-Key"] != "key":
                    return self.reply({"error": ["EAPI:Invalid signature"]})
                method = path.rsplit("/", 1)[1]
                fake.calls.append((method, params))
                self.reply(fake.answer(method, params))

            def reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def answer(self, method, params):
        if method in self.denied:
            return {"error": ["EGeneral:Permission denied"]}
        if method == "CancelOrder":
            if params.get("txid") == "OABC-123":
                self.order = {**self.order, "status": "canceled"}
                return {"error": [], "result": {"count": 1}}
            return {"error": ["EOrder:Unknown order"]}
        if method == "QueryOrders" and params.get("txid") != "OABC-123":
            return {"error": ["EOrder:Invalid order"]}
        if method == "Balance":
            return {"error": [], "result": self.balance}
        if method == "WithdrawInfo":
            if self.withdraw_allowed:
                return {"error": ["EFunding:Unknown withdraw key"]}
            return {"error": ["EGeneral:Permission denied"]}
        if method == "AddOrder":
            if self.add_order_error:
                return {"error": [self.add_order_error]}
            return {"error": [], "result": {"txid": ["OABC-123"], "descr": {"order": "test"}}}
        if method == "QueryOrders":
            return {"error": [], "result": {"OABC-123": self.order}}
        return {"error": ["EGeneral:Unknown method"]}

    def close(self):
        self.server.shutdown()


class LiveBroker(unittest.TestCase):
    def setUp(self):
        self.kraken = FakeKraken()
        self.addCleanup(self.kraken.close)
        self.client = KrakenClient("key", SECRET, base_url=self.kraken.url, timeout=3)
        self.market = FakeMarket()
        self.broker = KrakenBroker(self.client, self.market)
        self.ticket = Ticket("BTC", "buy", Dec("0.001"), Dec("80400.5"), Dec("72000"), "test")

    def test_order_parameters(self):
        fill = self.broker.execute(self.ticket, quote("BTC", 80000))
        method, params = next(c for c in self.kraken.calls if c[0] == "AddOrder")
        self.assertEqual(params["pair"], "XBTUSD")
        self.assertEqual(params["type"], "buy")
        self.assertEqual(params["ordertype"], "limit")
        self.assertEqual(params["timeinforce"], "IOC")  # Never rests on the book.
        self.assertEqual(params["volume"], "0.001")
        self.assertEqual(params["price"], "80400.5")
        self.assertEqual(params["oflags"], "fciq")
        self.assertNotIn("leverage", params)
        self.assertEqual(fill.qty, Dec("0.001"))
        self.assertEqual(fill.gross, Dec("80.00"))
        self.assertEqual(fill.fee, Dec("0.64"))
        self.assertEqual(fill.price, Dec("80000"))

    def test_small_numbers_are_never_sent_in_scientific_notation(self):
        ticket = Ticket("BTC", "buy", Dec("0.00001000"), Dec("80400.5"))
        self.broker.execute(ticket, quote("BTC", 80000))
        params = next(p for m, p in self.kraken.calls if m == "AddOrder")
        self.assertEqual(params["volume"], "0.00001000")

    def test_partial_fill_reports_what_filled(self):
        self.kraken.order = {"status": "canceled", "vol_exec": "0.0004", "cost": "32.00", "fee": "0.256"}
        fill = self.broker.execute(self.ticket, quote("BTC", 80000))
        self.assertEqual(fill.qty, Dec("0.0004"))

    def test_no_fill_returns_none(self):
        self.kraken.order = {"status": "canceled", "vol_exec": "0", "cost": "0", "fee": "0"}
        self.assertIsNone(self.broker.execute(self.ticket, quote("BTC", 80000)))

    def test_exchange_error_means_rejected(self):
        self.kraken.add_order_error = "EOrder:Insufficient funds"
        with self.assertRaises(OrderRejected):
            self.broker.execute(self.ticket, quote("BTC", 80000))

    def test_lost_connection_means_unknown_not_retried(self):
        self.client.base_url = "http://127.0.0.1:9"  # Nothing listens here.
        with self.assertRaises(OrderStateUnknown):
            self.broker.execute(self.ticket, quote("BTC", 80000))

    def test_order_that_never_finishes_means_unknown(self):
        self.kraken.order = {"status": "open", "vol_exec": "0", "cost": "0", "fee": "0"}
        import duo.brokers
        original = duo.brokers.ORDER_POLL_SECONDS
        duo.brokers.ORDER_POLL_SECONDS = 1
        self.addCleanup(setattr, duo.brokers, "ORDER_POLL_SECONDS", original)
        with self.assertRaises(OrderStateUnknown):
            self.broker.execute(self.ticket, quote("BTC", 80000))

    def test_reconcile_spots_missing_coins_but_ignores_extra_ones(self):
        portfolio = Portfolio(starting=Dec(1000), cash=Dec(1000))
        portfolio.apply("BTC", "buy", Dec("0.5"), Dec(100), Dec(0))
        self.assertIsNone(self.broker.reconcile(portfolio))
        self.kraken.balance["XXBT"] = "0.4"
        self.assertIn("0.4", self.broker.reconcile(portfolio))

    def test_reconcile_doesnt_halt_on_an_outage(self):
        self.client.base_url = "http://127.0.0.1:9"
        portfolio = Portfolio(starting=Dec(1000), cash=Dec(1000))
        portfolio.apply("BTC", "buy", Dec("0.5"), Dec(100), Dec(0))
        self.assertIsNone(self.broker.reconcile(portfolio))

    def test_nonces_always_increase(self):
        for _ in range(5):
            self.client.private("Balance")
        nonces = [int(p["nonce"]) for m, p in self.kraken.calls]
        self.assertEqual(nonces, sorted(set(nonces)))


class LiveResting(unittest.TestCase):
    """A resting order's whole life, through the engine, against the fake Kraken."""

    def setUp(self):
        self.kraken = FakeKraken()
        self.addCleanup(self.kraken.close)
        self.client = KrakenClient("key", SECRET, base_url=self.kraken.url, timeout=3)
        self.market = FakeMarket()
        self.broker = KrakenBroker(self.client, self.market)
        self.config = make_config()
        self.engine = Engine(self.config, DB(self.config.db_path), self.market, self.broker)
        self.ticket = Ticket("BTC", "buy", Dec("0.001"), Dec("79999.5"), Dec("72000"), "test", Dec("90000"), "resting")
        self.kraken.order = {"status": "open", "vol_exec": "0", "cost": "0", "fee": "0"}

    def place(self):
        placed, text = self.engine._place_resting(self.ticket, self.market.quotes["BTC"], "agents", None)
        self.assertTrue(placed, text)

    def test_order_is_post_only_with_an_expiry_on_krakens_side(self):
        self.place()
        params = next(p for m, p in self.kraken.calls if m == "AddOrder")
        self.assertEqual(params["oflags"], "post,fciq")
        self.assertEqual(params["timeinforce"], "GTD")
        self.assertEqual(params["expiretm"], "+600")
        self.assertEqual(params["price"], "79999.5")
        self.assertEqual(params["ordertype"], "limit")

    def test_open_order_books_nothing(self):
        self.place()
        self.engine.tick()
        self.assertEqual(self.engine.db.all("SELECT * FROM fills"), [])

    def test_partial_fills_are_booked_as_they_happen_and_never_twice(self):
        self.place()
        self.kraken.order = {"status": "open", "vol_exec": "0.0004", "cost": "32.00", "fee": "0.128"}
        self.engine.tick()
        self.assertEqual(self.engine.portfolio().qty("BTC"), Dec("0.0004"))
        self.assertEqual(self.engine.db.one("SELECT stop_price FROM stops")["stop_price"], "72000")  # Protected from the first piece.
        self.engine.tick()
        self.assertEqual(len(self.engine.db.all("SELECT * FROM fills")), 1)

        self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.32"}
        self.engine.tick()
        rows = self.engine.db.all("SELECT * FROM fills ORDER BY id")
        self.assertEqual([r["qty"] for r in rows], ["0.0004", "0.0006"])
        self.assertEqual(self.engine.portfolio().fees, Dec("0.32"))
        self.assertEqual(self.engine.portfolio().qty("BTC"), Dec("0.001"))
        self.assertEqual({r["liquidity"] for r in rows}, {"maker"})
        self.assertEqual(self.engine.db.one("SELECT status FROM resting_orders")["status"], "filled")
        self.assertEqual(self.engine.buys_today(time.time()), 1)  # Two pieces, but one order.

    def test_timed_out_order_is_cancelled_and_keeps_what_filled(self):
        self.place()
        self.kraken.order = {"status": "open", "vol_exec": "0.0004", "cost": "32.00", "fee": "0.128"}
        self.engine.db.execute("UPDATE resting_orders SET expires_at = 0")
        self.engine.tick()
        self.assertTrue(any(m == "CancelOrder" for m, _ in self.kraken.calls))
        self.assertEqual(self.engine.db.one("SELECT status FROM resting_orders")["status"], "partial")
        self.assertEqual(self.engine.portfolio().qty("BTC"), Dec("0.0004"))

    def test_order_kraken_expired_itself_closes_cleanly(self):
        self.place()
        self.kraken.order = {"status": "expired", "vol_exec": "0", "cost": "0", "fee": "0"}
        self.engine.tick()
        self.assertEqual(self.engine.db.one("SELECT status FROM resting_orders")["status"], "missed")

    def test_post_only_order_kraken_rejects_or_cancels_counts_as_missed_not_a_fill(self):
        self.kraken.add_order_error = "EOrder:Post only order"
        placed, _ = self.engine._place_resting(self.ticket, self.market.quotes["BTC"], "agents", None)
        self.assertFalse(placed)
        self.assertNotEqual(self.engine.db.get("halted"), "1")
        self.kraken.add_order_error = None
        self.place()
        self.kraken.order = {"status": "canceled", "reason": "Post only order", "vol_exec": "0", "cost": "0", "fee": "0"}
        self.engine.tick()
        self.assertEqual(self.engine.db.one("SELECT status FROM resting_orders WHERE status != 'open'")["status"], "missed")

    def test_lost_connection_while_placing_halts(self):
        self.client.base_url = "http://127.0.0.1:9"
        placed, text = self.engine._place_resting(self.ticket, self.market.quotes["BTC"], "agents", None)
        self.assertFalse(placed)
        self.assertEqual(self.engine.db.get("halted"), "1")
        self.assertIn("expires by itself", self.engine.db.get("halt_reason"))

    def test_outage_while_checking_books_nothing_and_recovers(self):
        self.place()
        working_url = self.client.base_url
        self.client.base_url = "http://127.0.0.1:9"
        self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.32"}
        self.engine.tick()
        self.assertEqual(self.engine.db.all("SELECT * FROM fills"), [])
        self.assertEqual(self.engine.db.one("SELECT status FROM resting_orders")["status"], "open")
        self.client.base_url = working_url
        self.engine.tick()
        self.assertEqual(len(self.engine.db.all("SELECT * FROM fills")), 1)

    def test_restart_picks_up_a_fill_that_happened_while_down(self):
        self.place()
        self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.32"}
        restarted = Engine(self.config, DB(self.config.db_path), self.market, self.broker)
        restarted._recover()
        restarted.tick()
        self.assertEqual(restarted.portfolio().qty("BTC"), Dec("0.001"))
        self.assertEqual(restarted.db.one("SELECT stop_price FROM stops")["stop_price"], "72000")

    def test_stop_cancels_a_waiting_sell_before_selling(self):
        self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.32"}
        self.place()
        self.engine.tick()
        self.kraken.order = {"status": "open", "vol_exec": "0", "cost": "0", "fee": "0"}
        sell = Ticket("BTC", "sell", Dec("0.001"), Dec("90000"), None, "target", None, "resting")
        self.engine._place_resting(sell, self.market.quotes["BTC"], "target", None)
        self.kraken.calls.clear()
        self.market.prices["BTC"] = 71000

        # After the cancel, the immediate sell gets its own answer from the fake.
        original = self.kraken.answer
        def answer(method, params):
            result = original(method, params)
            if method == "CancelOrder":
                self.kraken.order = {"status": "canceled", "vol_exec": "0", "cost": "0", "fee": "0"}
            if method == "AddOrder":
                self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "71.00", "fee": "0.568"}
            return result
        self.kraken.answer = answer
        self.engine.tick()
        methods = [m for m, _ in self.kraken.calls]
        self.assertLess(methods.index("CancelOrder"), methods.index("AddOrder"))
        self.assertEqual(next(p for m, p in self.kraken.calls if m == "AddOrder")["timeinforce"], "IOC")
        self.assertEqual(self.engine.portfolio().qty("BTC"), 0)

    def test_stop_waits_if_the_cancel_cant_be_confirmed(self):
        self.kraken.order = {"status": "closed", "vol_exec": "0.001", "cost": "80.00", "fee": "0.32"}
        self.place()
        self.engine.tick()
        self.kraken.order = {"status": "open", "vol_exec": "0", "cost": "0", "fee": "0"}
        sell = Ticket("BTC", "sell", Dec("0.001"), Dec("90000"), None, "target", None, "resting")
        self.engine._place_resting(sell, self.market.quotes["BTC"], "target", None)
        self.kraken.calls.clear()
        self.kraken.denied = {"CancelOrder"}
        import duo.brokers
        original = duo.brokers.CANCEL_POLL_SECONDS
        duo.brokers.CANCEL_POLL_SECONDS = 0
        self.addCleanup(setattr, duo.brokers, "CANCEL_POLL_SECONDS", original)
        self.market.prices["BTC"] = 71000
        self.engine.tick()
        self.assertFalse(any(m == "AddOrder" for m, _ in self.kraken.calls))
        self.assertEqual(self.engine.portfolio().qty("BTC"), Dec("0.001"))


class Preflight(unittest.TestCase):
    def setUp(self):
        self.kraken = FakeKraken()
        self.addCleanup(self.kraken.close)
        self.client = KrakenClient("key", SECRET, base_url=self.kraken.url, timeout=3)
        self.market = FakeMarket()
        self.config = make_config()

    def run_check(self):
        return preflight(self.config, self.client, self.market)

    def test_passes_with_a_safe_key_and_enough_cash(self):
        self.assertEqual(self.run_check(), [])

    def test_only_ever_sends_orders_that_cant_trade(self):
        self.run_check()
        orders = [p for m, p in self.kraken.calls if m == "AddOrder"]
        self.assertEqual(len(orders), 6)
        for params in orders:
            self.assertEqual(params["validate"], "true")
        self.assertEqual(sum(p["timeinforce"] == "IOC" for p in orders), 3)
        for params in [p for p in orders if p["timeinforce"] == "GTD"]:
            self.assertEqual(params["expiretm"], "+60")
            self.assertIn("post", params["oflags"])
        btc = next(p for p in orders if p["pair"] == "XBTUSD")
        self.assertLess(Dec(btc["price"]), Dec(41000))  # Half the market, so it couldn't fill anyway.

    def test_refuses_a_key_that_cant_cancel_or_check_orders(self):
        for method, words in (("CancelOrder", "cancel its resting orders"), ("QueryOrders", "check on its orders")):
            self.kraken.denied = {method}
            self.assertTrue(any(words in p for p in self.run_check()), method)

    def test_refuses_a_key_that_can_withdraw(self):
        self.kraken.withdraw_allowed = True
        self.assertTrue(any("Withdraw permission" in p for p in self.run_check()))

    def test_refuses_when_starting_capital_exceeds_balance(self):
        self.kraken.balance["ZUSD"] = "10"
        self.assertTrue(any("only shows" in p for p in self.run_check()))

    def test_refuses_when_kraken_is_unreachable(self):
        self.client.base_url = "http://127.0.0.1:9"
        self.assertTrue(self.run_check())


if __name__ == "__main__":
    unittest.main()
