"""The 5-minute desk discussion: it talks on a cheap model, keeps notes and can ask for a decision, but can't change anything."""
import gzip
import json
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import anthropic

from duo import research
from duo.brokers import PaperBroker
from duo.db import DB
from duo.engine import Engine

from .helpers import FakeMarket, buy, fake_response, make_config, make_engine, risk, run_ticks, strategist, tool_use
from .test_sdk import StubAPI, message


def desk(text="BTC is flat. Looking at the 30-day low.", note=None, suggest=None, block_id="d1"):
    return fake_response(tool_use("desk_reply", {"message": text, "note": note, "suggest_change": suggest}, block_id),
                         model="claude-haiku-5-5", input_tokens=2000, output_tokens=300)


def review(text="That low has held twice. Keep watching.", note=None, escalate=None, block_id="d2"):
    return fake_response(tool_use("desk_review", {"message": text, "note": note, "escalate": escalate}, block_id),
                         model="claude-haiku-5-5", input_tokens=2000, output_tokens=300)


def after_first_decision(*responses, config=None):
    engine, client = make_engine([strategist(strategy="Plan."), risk("approve"), *responses], config=config)
    run_ticks(engine)
    client.after_decision = len(client.requests)
    return engine, client


def discussion_due(engine):
    engine.db.set("last_discussion", "0")


def discussions(engine):
    return engine.db.all("SELECT * FROM sessions WHERE kind = 'discussion' ORDER BY id")


class Discussion(unittest.TestCase):
    def test_it_talks_on_the_cheap_model_with_no_fallback(self):
        engine, client = after_first_decision(desk(), review())
        discussion_due(engine)
        run_ticks(engine)
        requests = client.requests[client.after_decision:]
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["model"], "claude-haiku-5-5")
            self.assertNotIn("fallbacks", request)
            self.assertNotIn("betas", request)
            self.assertEqual(request["tool_choice"], {"type": "auto"})
        session = discussions(engine)[0]
        self.assertEqual(session["outcome"], "discussed")
        speakers = [m["speaker"] for m in engine.db.all("SELECT speaker FROM messages WHERE session_id = ? ORDER BY id", (session["id"],))]
        self.assertEqual(speakers, ["strategist", "risk"])

    def test_the_risk_manager_hears_the_strategist(self):
        engine, client = after_first_decision(desk("Watching 76155.", suggest="Lower the plan by 1%."), review())
        discussion_due(engine)
        run_ticks(engine)
        last = client.requests[-1]["messages"][-1]["content"][-1]["text"]
        self.assertIn("Watching 76155.", last)
        self.assertIn("Lower the plan by 1%.", last)

    def test_it_waits_for_its_turn(self):
        engine, client = after_first_decision()
        run_ticks(engine, 3)
        self.assertEqual(len(client.requests), client.after_decision)
        self.assertEqual(discussions(engine), [])

    def test_it_comes_round_again_after_the_interval(self):
        engine, client = after_first_decision(desk(), review(), desk(block_id="d3"), review(block_id="d4"))
        discussion_due(engine)
        run_ticks(engine)
        run_ticks(engine)
        self.assertEqual(len(discussions(engine)), 1)
        engine.db.set("last_discussion", time.time() - 301)
        run_ticks(engine)
        self.assertEqual(len(discussions(engine)), 2)

    def test_it_has_no_way_to_trade(self):
        engine, client = after_first_decision(
            fake_response(tool_use("strategist_reply", {
                "message": "Buy now", "orders": [buy()], "stop_updates": [], "cancel_plans": [], "strategy_update": "New",
                "next_check_in_minutes": 30, "summary": "x"}, "sneaky"), model="claude-haiku-5-5"),
            desk(), review(),
        )
        fills_before = len(engine.db.all("SELECT * FROM fills"))
        strategy_before = engine.db.one("SELECT COUNT(*) AS n FROM strategy")["n"]
        discussion_due(engine)
        run_ticks(engine)
        names = {tool["name"] for tool in client.requests[client.after_decision]["tools"]}
        self.assertEqual(names, {"get_candles", "test_rule", "desk_reply"})
        rejected = client.requests[client.after_decision + 1]["messages"][-1]["content"][0]
        self.assertTrue(rejected["is_error"])
        self.assertEqual(len(engine.db.all("SELECT * FROM fills")), fills_before)
        self.assertEqual(engine.waiting_plans(), [])
        self.assertEqual(engine.db.one("SELECT COUNT(*) AS n FROM strategy")["n"], strategy_before)

    def test_it_doesnt_move_the_decision_schedule(self):
        engine, _ = after_first_decision(desk(), review())
        before = (engine.db.get("next_check_in"), engine.db.get("last_session_end"), engine.db.get("last_session_prices"))
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(before, (engine.db.get("next_check_in"), engine.db.get("last_session_end"), engine.db.get("last_session_prices")))

    def test_notes_are_kept_and_reach_later_conversations_and_decisions(self):
        engine, client = after_first_decision(desk(note="Dips of 5% worked in 2023."), review(note="But not in 2022."),
                                              desk(block_id="d3"), review(block_id="d4"))
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual([r["text"] for r in engine.db.all("SELECT text FROM research_notes ORDER BY id")],
                         ["Dips of 5% worked in 2023.", "But not in 2022."])
        discussion_due(engine)
        run_ticks(engine)
        brief = client.requests[-2]["messages"][0]["content"][-1]["text"]
        self.assertIn("Dips of 5% worked in 2023.", brief)
        self.assertIn("LATEST DESK CONVERSATION", brief)
        self.assertIn("Looking at the 30-day low.", brief)
        self.assertIn("Dips of 5% worked in 2023.", engine.build_brief("test"))

    def test_decision_journal_leaves_out_discussions(self):
        engine, _ = after_first_decision(desk("Chatter."), review())
        discussion_due(engine)
        run_ticks(engine)
        brief = engine.build_brief("test")
        journal = brief.split("YOUR JOURNAL")[1].split("DESK")[0]
        self.assertNotIn("Chatter", journal)

    def test_a_long_message_is_trimmed_in_the_briefing(self):
        engine, _ = after_first_decision(desk("x" * 5000), review())
        discussion_due(engine)
        run_ticks(engine)
        self.assertLess(len(engine.build_brief("test")), 6000)


class Escalation(unittest.TestCase):
    def flagged(self, *more):
        engine, client = after_first_decision(desk(suggest="Cut the plan."), review(escalate="The low broke."), *more)
        discussion_due(engine)
        run_ticks(engine)
        return engine, client

    def test_only_the_risk_manager_can_ask_for_a_decision(self):
        engine, client = after_first_decision(desk(suggest="Buy everything."), review())
        discussion_due(engine)
        run_ticks(engine)
        self.assertIsNone(engine.db.get("escalation"))
        self.assertEqual(discussions(engine)[0]["outcome"], "discussed")

    def test_a_flag_waits_for_the_minimum_gap_then_starts_an_opus_decision(self):
        engine, client = self.flagged(strategist(block_id="s9"), risk("approve", block_id="r9"))
        self.assertEqual(discussions(engine)[0]["outcome"], "flagged")
        self.assertEqual(engine.db.get("escalation"), "The low broke.")
        requests = len(client.requests)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests)  # Too soon after the last decision.

        engine.db.set("last_session_end", time.time() - 3600)
        run_ticks(engine)
        decision = engine.db.one("SELECT * FROM sessions WHERE kind = 'decision' ORDER BY id DESC LIMIT 1")
        self.assertIn("The low broke.", decision["trigger"])
        self.assertEqual(client.requests[requests]["model"], "claude-opus-5-5")
        self.assertEqual(client.requests[requests]["fallbacks"], "default")
        self.assertIn("The low broke.", client.requests[requests]["messages"][0]["content"][-1]["text"])
        self.assertIsNone(engine.db.get("escalation"))

    def test_flags_stop_at_the_daily_limit_and_say_so(self):
        engine, client = self.flagged()
        now = time.time()
        for _ in range(4):
            engine.db.execute("INSERT INTO sessions(started_at, ended_at, trigger, outcome) VALUES(?, ?, 'x', 'held')", (now, now))
        engine.db.set("last_session_end", now - 3600)
        requests = len(client.requests)
        engine.db.set("last_discussion", now)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests)
        self.assertIsNone(engine.db.get("escalation"))
        self.assertTrue(engine.db.one("SELECT * FROM events WHERE kind = 'desk' AND text LIKE '%decision sessions are used%'"))

    def test_scheduled_checkins_still_happen_after_the_flag_limit(self):
        engine, client = after_first_decision(strategist(block_id="s9"), risk("approve", block_id="r9"))
        now = time.time()
        for _ in range(4):
            engine.db.execute("INSERT INTO sessions(started_at, ended_at, trigger, outcome) VALUES(?, ?, 'x', 'held')", (now, now))
        engine.db.set("next_check_in", now - 1)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT trigger FROM sessions ORDER BY id DESC LIMIT 1")["trigger"], "Scheduled check-in")


class WhenItDoesntRun(unittest.TestCase):
    def test_halt_stops_the_desk(self):
        engine, client = after_first_decision()
        engine.halt("drill")
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine), [])
        self.assertIsNone(engine.next_discussion())

    def test_budget_stops_the_desk(self):
        engine, client = after_first_decision()
        engine.db.execute(
            "INSERT INTO api_usage(ts, session_id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd) "
            "VALUES(?, NULL, 'x', 0, 0, 0, 0, 999)", (time.time(),))
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine), [])

    def test_it_can_be_switched_off(self):
        config = make_config()
        config = replace(config, agents=replace(config.agents, discussion_minutes=0))
        engine, client = after_first_decision(config=config)
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine), [])

    def test_no_api_key_means_no_desk(self):
        engine, _ = make_engine()
        engine.runner = None
        run_ticks(engine)
        self.assertIsNone(engine.next_discussion())

    def test_failure_is_recorded_and_waits_for_the_next_turn(self):
        engine, client = after_first_decision(RuntimeError("overloaded"), desk(), review())
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine)[0]["outcome"], "failed")
        self.assertIn("overloaded", engine.db.get("agents_error"))
        requests = len(client.requests)
        run_ticks(engine)
        self.assertEqual(len(client.requests), requests)
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine)[1]["outcome"], "discussed")
        self.assertIsNone(engine.db.get("agents_error"))

    def test_refusal_is_a_failure_not_a_crash(self):
        engine, _ = after_first_decision(fake_response(stop_reason="refusal", model="claude-haiku-5-5"))
        discussion_due(engine)
        run_ticks(engine)
        self.assertEqual(discussions(engine)[0]["outcome"], "failed")

    def test_stops_still_run_while_the_desk_is_talking(self):
        engine, client = make_engine([strategist(orders=[buy()], strategy="Plan."), risk("approve"), desk(), review()])
        run_ticks(engine)
        talking, release = threading.Event(), threading.Event()
        original = client.beta.messages.create

        def slow_call(**params):
            talking.set()
            release.wait(timeout=10)
            return original(**params)

        client.beta.messages.create = slow_call
        discussion_due(engine)
        engine.tick()
        self.assertTrue(talking.wait(timeout=10))
        engine.market.prices["BTC"] = 71000
        engine.tick()  # The desk is still mid-call on another thread.
        self.assertTrue(engine.session_running())
        self.assertEqual(engine.db.one("SELECT source FROM fills ORDER BY id DESC LIMIT 1")["source"], "stop")
        self.assertEqual(engine.portfolio().qty("BTC"), 0)
        release.set()
        engine._session_thread.join(timeout=10)
        self.assertEqual(discussions(engine)[0]["outcome"], "discussed")


def write_history(directory: Path, asset: str, months: int, days: int = 800):
    """Prices that drift up, dip 6% each week and recover, so a dip rule has something to find."""
    directory.mkdir(parents=True, exist_ok=True)
    start = 86400 * 19000
    with gzip.open(directory / f"{asset}-5m-{months}mo.csv.gz", "wt") as file:
        for bar in range(days * 288):
            week_position = bar % (288 * 7)
            price = 100.0 * (0.94 if 288 <= week_position < 288 * 2 else 1.0)
            file.write(f"{start + bar * 300},{price},{price * 1.001},{price * 0.999},{price}\n")


class RuleTesting(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.db = DB(self.config.db_path)
        write_history(self.config.data_dir / "history", "BTC", 96)
        write_history(self.config.data_dir / "history", "BTC", 25, days=20)

    def dip(self, **overrides):
        return {"asset": "BTC", "rule": "dip", "stop_pct": 8, "entry_pct": 3, "target_pct": 5, "average_hours": 24,
                "only_in_uptrend": False, "lookback_hours": None, "trail_pct": None, **overrides}

    def test_it_uses_the_longest_history(self):
        self.assertTrue(research.history_file(self.config.data_dir, "BTC").name.endswith("-96mo.csv.gz"))
        self.assertIsNone(research.history_file(self.config.data_dir, "DOGE"))

    def test_results_come_by_year_with_fees_and_a_warning(self):
        text, label = research.test_rule(self.config, self.db, self.dip())
        self.assertIn("year,net_pct_of_position_after_fees,round_trips,asset_held_pct", text)
        self.assertRegex(text, r"\n2022,[+-]")
        self.assertIn("Average", text)
        self.assertIn("Variations tested so far, by anyone on the desk: 1", text)
        self.assertIn("lead and not as proof", text)
        self.assertIn("Tested BTC dip rule", label)

    def test_it_counts_how_many_variations_have_been_tried(self):
        research.test_rule(self.config, self.db, self.dip())
        text, _ = research.test_rule(self.config, self.db, self.dip(target_pct=3))
        self.assertIn("so far, by anyone on the desk: 2", text)
        self.assertEqual(self.db.get("rule_tests"), "2")

    def test_fees_lower_the_result(self):
        free = replace(self.config, maker_pct=self.config.maker_pct * 0, taker_pct=self.config.taker_pct * 0)
        average = lambda text: float(text.split("Average ")[1].split("%")[0])
        self.assertGreater(average(research.test_rule(free, self.db, self.dip())[0]),
                           average(research.test_rule(self.config, self.db, self.dip())[0]))

    def test_breakout_rule_runs(self):
        text, _ = research.test_rule(self.config, self.db, {
            "asset": "BTC", "rule": "breakout", "stop_pct": 4, "entry_pct": None, "target_pct": None, "average_hours": None,
            "only_in_uptrend": None, "lookback_hours": 24, "trail_pct": 4})
        self.assertIn("breakout rule", text)

    def test_bad_inputs_get_a_clear_answer(self):
        for bad, words in ((dict(entry_pct=None), "entry_pct is required"), (dict(stop_pct=500), "between"), (dict(rule="magic"), "dip or breakout"),
                           (dict(asset="DOGE"), "No price history"), (dict(average_hours=True), "average_hours is required")):
            with self.assertRaises(ValueError, msg=bad) as caught:
                research.test_rule(self.config, self.db, self.dip(**bad))
            self.assertIn(words, str(caught.exception))

    def test_agents_can_call_it_and_mistakes_go_back_to_them(self):
        config = self.config
        call = {"asset": "BTC", "rule": "dip", "stop_pct": 8, "entry_pct": 3, "target_pct": 5, "average_hours": 24,
                "only_in_uptrend": False, "lookback_hours": None, "trail_pct": None}
        engine, client = make_engine([
            strategist(strategy="Plan."), risk("approve"),
            fake_response(tool_use("test_rule", call, "t1"), tool_use("test_rule", {**call, "entry_pct": None}, "t2"), model="claude-haiku-5-5"),
            desk(), review(),
        ], config=config)
        run_ticks(engine)
        discussion_due(engine)
        run_ticks(engine)
        results = client.requests[3]["messages"][-1]["content"]
        self.assertIn("year,net_pct", results[0]["content"])
        self.assertTrue(results[1]["is_error"])
        self.assertIn("entry_pct is required", results[1]["content"])
        tools = engine.db.one("SELECT tools FROM messages WHERE speaker = 'strategist' ORDER BY id DESC LIMIT 1")["tools"]
        self.assertIn("Tested BTC dip rule", tools)


class ThroughTheRealSDK(unittest.TestCase):
    def test_haiku_request_is_well_formed(self):
        decision = message([{"type": "tool_use", "id": "s", "name": "strategist_reply", "input": {
            "message": "Hold.", "orders": [], "stop_updates": [], "cancel_plans": [], "strategy_update": "Wait.",
            "next_check_in_minutes": 240, "summary": "Holding."}}])
        approve = message([{"type": "tool_use", "id": "r", "name": "risk_reply", "input": {"message": "Fine.", "verdict": "approve"}}])
        opening = message([{"type": "thinking", "thinking": "Flat.", "signature": "sig"},
                           {"type": "tool_use", "id": "d1", "name": "desk_reply", "input": {"message": "Flat.", "note": None, "suggest_change": None}}])
        reply = message([{"type": "tool_use", "id": "d2", "name": "desk_review", "input": {"message": "Agreed.", "note": "Nothing new.", "escalate": None}}])
        stub = StubAPI([decision, approve, opening, reply])
        self.addCleanup(stub.close)
        config = make_config()
        client = anthropic.Anthropic(api_key="test-key", base_url=stub.url, max_retries=0)
        engine = Engine(config, DB(config.db_path), FakeMarket(config.assets), PaperBroker(config), client)
        run_ticks(engine)
        discussion_due(engine)
        run_ticks(engine)
        self.assertIsNone(engine.db.get("agents_error"), engine.db.get("agents_error"))
        self.assertEqual(discussions(engine)[0]["outcome"], "discussed")
        request = stub.requests[2]
        self.assertEqual(request["body"]["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", request["body"])
        self.assertNotIn("anthropic-beta", {k.lower() for k in request["headers"]})
        self.assertEqual(request["body"]["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(request["body"]["output_config"], {"effort": "medium"})
        self.assertIn("server-side-fallback-2026-07-01", stub.requests[0]["headers"]["anthropic-beta"])
        self.assertTrue(all(tool["strict"] for tool in request["body"]["tools"]))


class Dashboard(unittest.TestCase):
    def test_kinds_are_kept_apart_and_notes_shown(self):
        from duo.web import Dashboard as Board
        engine, _ = after_first_decision(desk(note="A note."), review())
        discussion_due(engine)
        run_ticks(engine)
        board = Board(engine, engine.config)
        self.assertEqual([s["kind"] for s in board.sessions()], ["decision"])
        self.assertEqual([s["kind"] for s in board.sessions("discussion")], ["discussion"])
        state = board.state()
        self.assertEqual(state["notes"][0]["text"], "A note.")
        self.assertGreater(state["status"]["next_discussion"], time.time())
        self.assertIn("Opus decisions today", [row["label"] for row in state["limits"]])
        json.dumps(state)


if __name__ == "__main__":
    unittest.main()
