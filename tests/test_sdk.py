"""Runs a session through the real Anthropic SDK against a local stand-in for the API.

This proves the SDK accepts our parameters, sends them as intended and parses replies.
It can't prove the real API accepts them. Only a real key can do that.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

from duo.brokers import PaperBroker
from duo.db import DB
from duo.engine import Engine

from .helpers import FakeMarket, buy, make_config, run_ticks


def message(blocks, stop_reason="tool_use", message_id="msg_1"):
    return {
        "id": message_id, "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": blocks, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 2000, "output_tokens": 800, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 500},
    }


THINKING = {"type": "thinking", "thinking": "BTC is near its 20 day average.", "signature": "sig-abc"}


def strategist_block(block_id="toolu_s"):
    return {
        "type": "tool_use", "id": block_id, "name": "strategist_reply",
        "input": {
            "message": "Small starter position in BTC.", "orders": [buy()], "stop_updates": [],
            "strategy_update": "Hold majors above the 50 day average.", "next_check_in_minutes": 240,
            "summary": "Opened a starter position.",
        },
    }


class StubAPI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps(stub.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


class RealSDK(unittest.TestCase):
    def run_session(self, replies):
        stub = StubAPI(replies)
        self.addCleanup(stub.close)
        config = make_config()
        client = anthropic.Anthropic(api_key="test-key", base_url=stub.url, max_retries=0)
        engine = Engine(config, DB(config.db_path), FakeMarket(config.assets), PaperBroker(config), client)
        run_ticks(engine)
        return engine, stub

    def test_full_session(self):
        engine, stub = self.run_session([
            message([THINKING, {"type": "tool_use", "id": "toolu_c", "name": "get_candles",
                                "input": {"asset": "BTC", "interval_minutes": 60, "count": 5}}], message_id="msg_1"),
            message([strategist_block()], message_id="msg_2"),
            message([{"type": "tool_use", "id": "toolu_r", "name": "risk_reply",
                      "input": {"message": "Fine at this size.", "verdict": "approve"}}], message_id="msg_3"),
        ])
        session = engine.db.one("SELECT * FROM sessions")
        self.assertIsNone(engine.db.get("agents_error"), engine.db.get("agents_error"))
        self.assertEqual(session["outcome"], "traded")
        self.assertEqual(len(engine.db.all("SELECT * FROM fills")), 1)

        first = stub.requests[0]
        self.assertTrue(first["path"].startswith("/v1/messages"))
        self.assertIn("server-side-fallback-2026-07-01", first["headers"]["anthropic-beta"])
        body = first["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["tool_choice"], {"type": "auto"})
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("temperature", body)
        for tool in body["tools"]:
            self.assertTrue(tool["strict"])

        # Thinking blocks must come back untouched on the next request, or the API rejects the history.
        replayed = stub.requests[1]["body"]["messages"][1]["content"]
        self.assertEqual(replayed[0], THINKING)
        self.assertEqual(replayed[1]["id"], "toolu_c")
        results = stub.requests[1]["body"]["messages"][2]["content"]
        self.assertEqual(results[0]["tool_use_id"], "toolu_c")

        # Thinking summaries reach the dashboard. Costs: Opus 5.5 at $4/$20, cache write 1.25x, cache read $0.20.
        reasoning = engine.db.one("SELECT reasoning FROM messages WHERE speaker = 'strategist'")["reasoning"]
        self.assertIn("20 day average", reasoning)
        per_call = (2000 * 4 + 800 * 20 + 1000 * 4 * 1.25 + 500 * 0.20) / 1_000_000
        self.assertAlmostEqual(engine.budget.total(), per_call * 3, places=9)

    def test_api_errors_fail_safely(self):
        config = make_config()
        client = anthropic.Anthropic(api_key="test-key", base_url="http://127.0.0.1:9", max_retries=0, timeout=2)
        engine = Engine(config, DB(config.db_path), FakeMarket(config.assets), PaperBroker(config), client)
        run_ticks(engine)
        self.assertEqual(engine.db.one("SELECT outcome FROM sessions")["outcome"], "failed")
        self.assertEqual(engine.db.all("SELECT * FROM fills"), [])

    def test_haiku_skips_fallbacks(self):
        from dataclasses import replace
        config = make_config()
        config = replace(config, agents=replace(config.agents, model="claude-haiku-5-5"))
        stub = StubAPI([message([strategist_block()]),
                        message([{"type": "tool_use", "id": "r", "name": "risk_reply", "input": {"message": "ok", "verdict": "reject"}}])])
        self.addCleanup(stub.close)
        client = anthropic.Anthropic(api_key="test-key", base_url=stub.url, max_retries=0)
        engine = Engine(config, DB(config.db_path), FakeMarket(config.assets), PaperBroker(config), client)
        run_ticks(engine)
        self.assertNotIn("fallbacks", stub.requests[0]["body"])


if __name__ == "__main__":
    unittest.main()
