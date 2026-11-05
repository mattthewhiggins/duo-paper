"""The whole loop with a scripted Claude: conversations, trades, stops, halts and failures."""
import time
import unittest
from decimal import Decimal as Dec
from types import SimpleNamespace

from duo.brokers import Fill, OrderRejected, OrderStateUnknown, PaperBroker
from duo.config import D

from .helpers import buy, fake_response, make_config, make_engine, risk, run_ticks, sell, strategist, tool_use


def fills(engine):
    return engine.db.all("SELECT * FROM fills ORDER BY id")


def speakers(engine):
    return [m["speaker"] for m in engine.db.all("SELECT speaker FROM messages ORDER BY id")]


def last_session(engine):
    return engine.db.one("SELECT * FROM sessions ORDER BY id DESC LIMIT 1")


def wake_again(engine):
    engine.db.set("wake_requested", "1")


class Conversations(unittest.TestCase):
    def test_approved_trade_executes_and_shows_up_everywhere(self):
        engine, client = make_engine([
            strategist(orders=[buy()], strategy="Buy pullbacks."),
            risk("approve"),
        ])
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "buy")
        self.assertEqual(trades[0]["source"], "agents")
        self.assertEqual(last_session(engine)["outcome"], "traded")
        self.assertEqual(speakers(engine), ["strategist", "risk", "system"])
        self.assertEqual(engine.db.one("SELECT text FROM strategy")["text"], "Buy pullbacks.")
        self.assertEqual(engine.db.one("SELECT stop_price FROM stops")["stop_price"], "72000")

        metrics = engine.metrics()
        self.assertLess(metrics["cash"], Dec(1000))
        self.assertGreater(metrics["fees"], 0)
        self.assertGreater(metrics["api_cost"], 0)
        self.assertLess(metrics["net_pnl"], 0)  # Fees and API costs, before any price move.

    def test_nothing_executes_without_approval(self):
        for verdict, outcome in (("reject", "rejected"),):
            engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), risk(verdict)])
            run_ticks(engine)
            self.assertEqual(fills(engine), [])
            self.assertEqual(last_session(engine)["outcome"], outcome)
            self.assertIsNone(engine.db.one("SELECT * FROM strategy"))

    def test_revise_goes_back_and_forth_then_executes_the_revised_proposal(self):
        engine, client = make_engine([
            strategist(orders=[buy(amount=100)], strategy="Plan.", block_id="s1"),
            risk("revise", "Too big.", block_id="r1"),
            strategist(orders=[buy(amount=50)], block_id="s2"),
            risk("approve", block_id="r2"),
        ])
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual(len(trades), 1)
        self.assertLessEqual(D(trades[0]["gross"]), Dec("50.1"))
        self.assertEqual(speakers(engine), ["strategist", "risk", "strategist", "risk", "system"])
        # The Strategist's third request has to hear the Risk Manager's objection.
        last_user = client.requests[2]["messages"][-1]["content"]
        self.assertTrue(any("Too big." in part.get("text", "") for part in last_user))
        self.assertEqual(last_user[0]["type"], "tool_result")
        self.assertEqual(last_user[0]["tool_use_id"], "s1")

    def test_endless_revising_ends_without_a_trade(self):
        responses = []
        for i in range(3):
            responses += [strategist(orders=[buy()], strategy="Plan.", block_id=f"s{i}"), risk("revise", block_id=f"r{i}")]
        engine, _ = make_engine(responses)
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        self.assertEqual(last_session(engine)["outcome"], "no_agreement")

    def test_risk_manager_is_skipped_when_nothing_is_open_or_proposed(self):
        engine, client = make_engine([
            strategist(strategy="Wait.", block_id="s1"), risk("approve", block_id="r1"),
            strategist(block_id="s2"),
        ])
        run_ticks(engine)
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(last_session(engine)["outcome"], "held")

    def test_candle_lookup_then_reply_and_tool_results_are_answered(self):
        engine, client = make_engine([
            fake_response(tool_use("get_candles", {"asset": "BTC", "interval_minutes": 60, "count": 3}, "t1")),
            strategist(strategy="Plan.", block_id="s1"),
            risk("approve"),
        ])
        run_ticks(engine)
        results = client.requests[1]["messages"][-1]["content"]
        self.assertEqual(results[0]["tool_use_id"], "t1")
        self.assertIn("time_utc,open,high,low,close,volume", results[0]["content"])
        self.assertEqual(len(results[0]["content"].splitlines()), 4)
        message = engine.db.one("SELECT tools FROM messages WHERE speaker = 'strategist'")
        self.assertIn("BTC 60m candles", message["tools"])

    def test_invalid_reply_gets_an_error_and_a_retry(self):
        engine, client = make_engine([
            fake_response(tool_use("strategist_reply", {"message": "oops"}, "bad")),
            strategist(strategy="Plan.", block_id="s1"),
            risk("approve"),
        ])
        run_ticks(engine)
        error = client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(error["is_error"])
        self.assertEqual(last_session(engine)["outcome"], "approved")

    def test_text_only_reply_gets_nudged(self):
        engine, client = make_engine([
            fake_response(SimpleNamespace(type="text", text="Hmm."), stop_reason="end_turn"),
            strategist(strategy="Plan.", block_id="s1"),
            risk("approve"),
        ])
        run_ticks(engine)
        nudge = client.requests[1]["messages"][-1]["content"][0]["text"]
        self.assertIn("strategist_reply", nudge)

    def test_request_shape_follows_current_api_rules(self):
        engine, client = make_engine([strategist(strategy="Plan."), risk("approve")])
        run_ticks(engine)
        request = client.requests[0]
        self.assertEqual(request["tool_choice"], {"type": "auto"})  # Forced tool use is a 400 on this model.
        self.assertEqual(request["thinking"]["type"], "adaptive")
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["betas"], ["server-side-fallback-2026-07-01"])
        self.assertTrue(all(tool["strict"] for tool in request["tools"]))
        self.assertNotIn("temperature", request)

    def test_system_prompt_never_changes_between_sessions(self):
        engine, client = make_engine([
            strategist(strategy="Plan.", block_id="s1"), risk("approve"),
            strategist(block_id="s2"),
        ])
        run_ticks(engine)
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(client.requests[0]["system"], client.requests[2]["system"])
        self.assertEqual(client.requests[0]["tools"], client.requests[2]["tools"])


class Failures(unittest.TestCase):
    def test_api_failure_trades_nothing_and_backs_off(self):
        engine, _ = make_engine([RuntimeError("boom")])
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        self.assertEqual(last_session(engine)["outcome"], "failed")
        self.assertIn("boom", engine.db.get("agents_error"))
        self.assertGreater(float(engine.db.get("next_check_in")), time.time() + 800)

    def test_failure_after_strategist_proposes_trades_nothing(self):
        engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), RuntimeError("risk manager down")])
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        self.assertEqual(last_session(engine)["outcome"], "failed")

    def test_refusal_and_cutoff_trade_nothing(self):
        for stop_reason in ("refusal", "max_tokens"):
            engine, _ = make_engine([fake_response(stop_reason=stop_reason)])
            run_ticks(engine)
            self.assertEqual(last_session(engine)["outcome"], "failed")
            self.assertEqual(fills(engine), [])

    def test_limit_blocks_approved_order(self):
        engine, _ = make_engine([strategist(orders=[buy(amount=500)], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        note = engine.db.one("SELECT text FROM messages WHERE speaker = 'system'")["text"]
        self.assertIn("per-order limit", note)
        # The agents were told before they decided.
        proposal = engine.db.one("SELECT orders FROM messages WHERE speaker = 'strategist'")["orders"]
        self.assertIn("per-order limit", proposal)

    def test_unknown_order_state_halts_trading(self):
        class Broken(PaperBroker):
            def execute(self, ticket, quote):
                raise OrderStateUnknown("no answer")
        config = make_config()
        engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")], config=config, broker=Broken(config))
        run_ticks(engine)
        self.assertEqual(engine.db.get("halted"), "1")
        self.assertIn("unknown", engine.db.get("halt_reason"))
        self.assertEqual(fills(engine), [])

    def test_exchange_rejection_doesnt_book_a_fill(self):
        class Rejecting(PaperBroker):
            def execute(self, ticket, quote):
                raise OrderRejected("EOrder:Insufficient funds")
        config = make_config()
        engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")], config=config, broker=Rejecting(config))
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        self.assertNotEqual(engine.db.get("halted"), "1")

    def test_market_outage_is_logged_once_and_clears(self):
        engine, client = make_engine()
        engine.market.fail = True
        run_ticks(engine, 3)
        self.assertEqual(engine.db.one("SELECT COUNT(*) AS n FROM events WHERE kind = 'market'")["n"], 1)
        self.assertEqual(client.requests, [])
        engine.market.fail = False
        engine.runner = None
        run_ticks(engine)
        self.assertIsNone(engine.db.get("market_down_since"))

    def test_budget_stops_new_sessions_but_stops_keep_working(self):
        engine, client = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        engine.db.execute(
            "INSERT INTO api_usage(ts, session_id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
            "VALUES(?, NULL, 'x', 0, 0, 0, 0, 999)", (time.time(),))
        wake_again(engine)
        requests_before = len(client.requests)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests_before)
        engine.market.prices["BTC"] = 70000
        run_ticks(engine)
        self.assertEqual([f["source"] for f in fills(engine)], ["agents", "stop"])

    def test_budget_hit_mid_session_trades_nothing(self):
        config = make_config()
        engine, client = make_engine([strategist(orders=[buy()], strategy="Plan.", block_id="s1")], config=config)
        # The strategist's call uses up the budget, so the Risk Manager never gets asked.
        engine.runner.budget.agents = type(config.agents)(**{**config.agents.__dict__, "session_budget_usd": 0.000001})
        run_ticks(engine)
        self.assertEqual(last_session(engine)["outcome"], "budget")
        self.assertEqual(fills(engine), [])
        self.assertEqual(len(client.requests), 1)


class Safeguards(unittest.TestCase):
    def bought(self):
        engine, client = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertEqual(len(fills(engine)), 1)
        return engine, client

    def test_stop_loss_sells_without_asking_the_agents(self):
        engine, client = self.bought()
        requests_before = len(client.requests)
        engine.market.prices["BTC"] = 71000
        engine.runner = None  # The agents can't even be reached.
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual([t["source"] for t in trades], ["agents", "stop"])
        self.assertEqual(trades[1]["side"], "sell")
        self.assertLess(D(trades[1]["realized"]), 0)
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        self.assertIsNone(engine.db.one("SELECT * FROM stops"))
        self.assertEqual(len(client.requests), requests_before)

    def test_target_takes_profit_without_the_agents(self):
        engine, client = make_engine([strategist(orders=[buy(target=85000)], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT target_price FROM stops")["target_price"], "85000")
        engine.market.prices["BTC"] = 86000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(len(fills(engine)), 1)  # Reaching the target puts a resting sell in the book.
        order = engine.db.one("SELECT * FROM resting_orders")
        self.assertEqual((order["side"], order["source"], order["status"]), ("sell", "target", "open"))
        engine.market.prices["BTC"] = 86100
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual([t["source"] for t in trades], ["agents", "target"])
        self.assertEqual(trades[1]["liquidity"], "maker")
        self.assertAlmostEqual(float(trades[1]["fee"]) / float(trades[1]["gross"]), 0.004, places=6)
        self.assertGreater(D(trades[1]["realized"]), 0)
        self.assertIsNone(engine.db.one("SELECT * FROM stops"))
        self.assertIn("Profit target", engine.db.get("exit_hit"))

    def test_target_below_the_fee_hurdle_is_blocked_and_the_agents_are_told(self):
        engine, _ = make_engine([strategist(orders=[buy(target=80500)], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertEqual(fills(engine), [])
        self.assertIn("justify the fees", engine.db.one("SELECT orders FROM messages WHERE speaker = 'strategist'")["orders"])

    def test_fee_budget_includes_fees_already_paid(self):
        engine, client = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertAlmostEqual(float(engine.fees_30d(time.time())), 80 * 0.008, delta=0.05)
        engine.db.execute("UPDATE fills SET ts = ts - ?", (31 * 86400,))
        self.assertEqual(engine.fees_30d(time.time()), 0)

    def test_stop_update_can_change_the_target_and_keeps_it_otherwise(self):
        engine, client = self.bought()
        client.responses += [strategist(stops=[{"asset": "BTC", "stop_price": 76000, "target_price": 95000}], block_id="s2"), risk("approve", block_id="r2")]
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT target_price FROM stops")["target_price"], "95000")
        client.responses += [strategist(stops=[{"asset": "BTC", "stop_price": 77000, "target_price": None}], block_id="s3"), risk("approve", block_id="r3")]
        wake_again(engine)
        run_ticks(engine)
        row = engine.db.one("SELECT stop_price, target_price FROM stops")
        self.assertEqual((row["stop_price"], row["target_price"]), ("77000", "95000"))

    def test_stop_doesnt_fire_above_its_price(self):
        engine, _ = self.bought()
        engine.market.prices["BTC"] = 73000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(len(fills(engine)), 1)

    def test_stop_still_fires_while_halted(self):
        engine, _ = self.bought()
        engine.halt("drill")
        engine.market.prices["BTC"] = 71000
        run_ticks(engine)
        self.assertEqual([t["source"] for t in fills(engine)], ["agents", "stop"])

    def test_stop_that_cant_fill_retries_next_tick(self):
        class Flaky(PaperBroker):
            refuse = False
            def execute(self, ticket, quote):
                if Flaky.refuse:
                    return None
                return super().execute(ticket, quote)
        config = make_config()
        engine, _ = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve")], config=config, broker=Flaky(config))
        run_ticks(engine)
        Flaky.refuse = True
        engine.market.prices["BTC"] = 71000
        engine.runner = None
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC") > 0, True)
        Flaky.refuse = False
        run_ticks(engine)
        self.assertEqual(engine.portfolio().qty("BTC"), 0)

    def test_stop_update_needs_approval_and_passes_limits(self):
        engine, client = self.bought()
        client.responses += [strategist(stops=[{"asset": "BTC", "stop_price": 76000}], block_id="s2"), risk("approve", block_id="r2")]
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT stop_price FROM stops")["stop_price"], "76000")

        client.responses += [strategist(stops=[{"asset": "BTC", "stop_price": 90000}], block_id="s3"), risk("approve", block_id="r3")]
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT stop_price FROM stops")["stop_price"], "76000")

    def test_daily_loss_pauses_buys_but_not_sells(self):
        engine, client = make_engine([strategist(orders=[buy(amount=90), buy(asset="ETH", amount=90, stop=3600)], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        engine.db.execute("UPDATE stops SET stop_price = '1'")
        engine.db.execute("UPDATE snapshots SET equity = 1100")  # The day opened higher.
        run_ticks(engine)
        self.assertTrue(engine.risk_context(time.time()).buys_paused)

        client.responses += [strategist(orders=[buy(amount=50), sell()], block_id="s2"), risk("approve", block_id="r2")]
        wake_again(engine)
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual(trades[-1]["side"], "sell")
        self.assertEqual(sum(t["side"] == "buy" for t in trades), 2)

    def test_drawdown_halts_and_resume_resets_the_peak(self):
        engine, client = self.bought()
        engine.db.set("peak_equity", "2000")
        run_ticks(engine)
        self.assertEqual(engine.db.get("halted"), "1")
        requests_before = len(client.requests)
        wake_again(engine)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests_before)  # Halted means no sessions.

        engine.db.clear("wake_requested")
        engine.resume()
        client.responses += [strategist(block_id="s9"), risk("approve", block_id="r9")]
        engine.db.set("next_check_in", time.time() + 9999)
        run_ticks(engine)
        self.assertEqual(engine.db.get("halted"), "0")

    def test_halt_during_session_blocks_its_orders(self):
        engine, client = make_engine()
        original = client.beta.messages.create
        client.responses += [strategist(orders=[buy()], strategy="Plan."), risk("approve")]

        def halting(**params):
            response = original(**params)
            if len(client.requests) == 2:
                engine.halt("user pressed stop mid-session")
            return response
        client.beta.messages.create = halting
        run_ticks(engine)
        self.assertEqual(fills(engine), [])

    def test_flatten_sells_everything_and_halts(self):
        engine, client = make_engine([strategist(orders=[buy(), buy(asset="ETH", stop=3600)], strategy="Plan."), risk("approve")])
        run_ticks(engine)
        self.assertEqual(len(fills(engine)), 2)
        engine.request_flatten()
        run_ticks(engine)
        trades = fills(engine)
        self.assertEqual([t["source"] for t in trades[-2:]], ["flatten", "flatten"])
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        self.assertEqual(engine.portfolio().qty("ETH"), 0)
        self.assertEqual(engine.db.get("halted"), "1")
        self.assertIsNone(engine.db.get("flatten_requested"))
        self.assertIsNone(engine.db.one("SELECT * FROM stops"))

    def test_price_move_wakes_the_agents_after_the_minimum_gap(self):
        engine, client = make_engine([strategist(strategy="Plan.", block_id="s1"), risk("approve", block_id="r1")])
        run_ticks(engine)
        requests_before = len(client.requests)
        engine.market.prices["BTC"] = 85000
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests_before)  # Too soon after the last session.

        client.responses += [strategist(block_id="s2")]
        engine.db.set("last_session_end", time.time() - 3600 * 2)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests_before + 1)
        self.assertIn("moved", last_session(engine)["trigger"])

    def test_next_check_in_is_clamped(self):
        engine, _ = make_engine([strategist(strategy="Plan.", next_check=1), risk("approve")])
        run_ticks(engine)
        minimum = engine.config.agents.min_check_in_minutes * 60
        self.assertGreater(float(engine.db.get("next_check_in")), time.time() + minimum - 5)

    def test_benchmark_is_fixed_at_the_first_price(self):
        engine, _ = make_engine()
        engine.runner = None
        run_ticks(engine)
        first = engine.db.get("benchmark_qty")
        engine.market.prices["BTC"] = 88000
        run_ticks(engine)
        self.assertEqual(engine.db.get("benchmark_qty"), first)
        self.assertGreater(engine.metrics()["benchmark_pnl"], 0)

    def test_restart_recovers_books_and_marks_open_session_interrupted(self):
        engine, _ = self.bought()
        engine.db.execute("INSERT INTO sessions(started_at, trigger) VALUES(?, 'x')", (time.time(),))
        before = engine.portfolio().cash
        engine._recover()
        self.assertEqual(last_session(engine)["outcome"], "interrupted")
        self.assertEqual(engine.portfolio().cash, before)


if __name__ == "__main__":
    unittest.main()
