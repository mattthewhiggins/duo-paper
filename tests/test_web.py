"""The dashboard's API and its protections."""
import base64
import json
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
import threading

from duo.engine import Engine
from duo.sys_risk import Controls, Halts
from duo.web import STATIC, Dashboard, make_handler

from .helpers import ScriptedClient, buy, make_engine, risk, run_ticks, strategist
from .sys_helpers import D0, make_env
from .test_desk import desk, review

AGENT_LIMITS = ["Buys today", "Fees, last 30 days", "Largest holding", "Loss today", "Drawdown from peak",
                "Opus decisions today", "API spend today", "API spend this month"]
AGENT_PAGE_PARTS = ['id="wake"', ">Wake agents<", ">Conversation<", ">Waiting orders<", ">Current strategy<",
                    ">Research notes<", ">Enforced by code<", 'data-kind="decision"', 'data-kind="discussion"']


def serve_dashboard(test, dashboard):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dashboard, None, True))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


class DashboardAPI(unittest.TestCase):
    def start(self, token=None, loopback_only=True):
        self.engine, _ = make_engine([
            strategist(orders=[buy(reason="=SUM(A1) <script>alert(1)</script>")], strategy="Plan."),
            risk("approve"),
        ])
        run_ticks(self.engine)
        self.dashboard = Dashboard(self.engine, self.engine.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dashboard, token, loopback_only))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers

    def post(self, path, headers=None):
        request = urllib.request.Request(self.base + path, method="POST", data=b"", headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status

    def status_of(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except urllib.error.HTTPError as error:
            return error.code

    def test_state_has_everything_the_page_needs(self):
        self.start()
        _, body, _ = self.get("/api/state")
        state = json.loads(body)
        self.assertEqual(state["mode"], "paper")
        m = state["metrics"]
        self.assertAlmostEqual(m["net_pnl"], m["equity"] - m["starting"] - m["api_cost"], places=6)
        self.assertAlmostEqual(m["equity"], m["starting"] + m["realized"] + m["unrealized"], places=6)
        self.assertEqual([b["asset"] for b in state["balances"]], ["USD", "BTC"])
        self.assertAlmostEqual(sum(b["share_pct"] for b in state["balances"]), 100, places=6)
        self.assertEqual(state["balances"][1]["stop"], 72000)
        self.assertEqual(len(state["limits"]), 8)
        self.assertEqual(state["balances"][1]["target"], 1_000_000)
        self.assertEqual(state["metrics"]["finished_days"], 0)
        self.assertAlmostEqual(state["metrics"]["today_pnl"], m["net_pnl"], places=6)
        self.assertEqual(state["strategy"]["text"], "Plan.")

    def test_daily_results_count_finished_days_after_api_costs(self):
        self.start()
        today = int(__import__("time").time() // 86400)
        for days_ago, equity in ((3, 1010), (2, 1005), (1, 1020)):
            self.engine.db.execute(
                "INSERT INTO snapshots(ts, equity, cash, benchmark, realized, unrealized, fees, api_cost) VALUES(?, ?, 0, 0, 0, 0, 0, 0)",
                ((today - days_ago) * 86400 + 100, equity),
            )
        m = json.loads(self.get("/api/state")[1])["metrics"]
        self.assertEqual((m["profitable_days"], m["losing_days"], m["finished_days"]), (2, 1, 3))
        self.assertAlmostEqual(m["today_pnl"], m["equity"] - m["api_cost"] - 1020, places=6)

    def test_conversation_trades_and_events(self):
        self.start()
        sessions = json.loads(self.get("/api/sessions")[1])
        self.assertEqual([m["speaker"] for m in sessions[0]["messages"]], ["strategist", "risk", "system"])
        self.assertEqual(sessions[0]["messages"][0]["proposal"]["orders"][0]["asset"], "BTC")
        self.assertGreater(sessions[0]["cost_usd"], 0)
        trades = json.loads(self.get("/api/trades")[1])
        self.assertEqual(trades[0]["side"], "buy")
        self.assertIsInstance(json.loads(self.get("/api/events")[1]), list)
        points = json.loads(self.get("/api/equity?range=24h")[1])
        self.assertGreaterEqual(len(points["points"]), 1)

    def test_csv_export_neutralizes_formulas(self):
        self.start()
        _, body, headers = self.get("/trades.csv")
        text = body.decode()
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("time_utc,asset,side", text)
        self.assertIn("order_type", text)
        self.assertIn(",immediate,", text)
        self.assertIn("'=SUM(A1)", text)

    def test_actions_need_the_custom_header(self):
        self.start()
        self.assertEqual(self.status_of(self.post, "/api/halt"), 403)
        self.assertNotEqual(self.engine.db.get("halted"), "1")
        self.assertEqual(self.post("/api/halt", {"X-Duo-Action": "1"}), 200)
        self.assertEqual(self.engine.db.get("halted"), "1")
        self.assertEqual(self.post("/api/resume", {"X-Duo-Action": "1"}), 200)
        self.assertEqual(self.engine.db.get("halted"), "0")
        self.assertEqual(self.post("/api/wake", {"X-Duo-Action": "1"}), 200)
        self.assertEqual(self.engine.db.get("wake_requested"), "1")
        self.assertEqual(self.post("/api/flatten", {"X-Duo-Action": "1"}), 200)
        self.assertEqual(self.engine.db.get("flatten_requested"), "1")
        self.assertEqual(self.status_of(self.post, "/api/nope", {"X-Duo-Action": "1"}), 404)

    def test_foreign_host_header_is_refused(self):
        self.start()
        self.assertEqual(self.status_of(self.get, "/api/state", {"Host": "evil.example.com"}), 403)
        self.assertEqual(self.status_of(self.post, "/api/flatten", {"Host": "evil.example.com", "X-Duo-Action": "1"}), 403)

    def test_token_is_required_when_set(self):
        self.start(token="secret", loopback_only=False)
        self.assertEqual(self.status_of(self.get, "/api/state"), 401)
        wrong = {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()}
        self.assertEqual(self.status_of(self.get, "/api/state", wrong), 401)
        right = {"Authorization": "Basic " + base64.b64encode(b"me:secret").decode()}
        self.assertEqual(self.get("/api/state", right)[0], 200)

    def test_orders_appear_and_a_plan_can_be_cancelled(self):
        self.start()
        self.engine.db.execute(
            "INSERT INTO plans(created_at, asset, quote_amount, trigger_price, direction, execution, stop_price, target_price, expires_at, status) "
            "VALUES(?, 'BTC', '80', '75000', 'below', 'resting', '65000', '90000', ?, 'active')",
            (__import__("time").time(), __import__("time").time() + 3600),
        )
        orders = json.loads(self.get("/api/state")[1])["orders"]
        self.assertEqual(len(orders["plans"]), 1)
        self.assertAlmostEqual(orders["plans"][0]["away_pct"], 6.25, places=1)
        self.assertEqual(orders["stats"], {"closed": 0, "filled": 0, "fees_saved": 0.0})
        plan_id = orders["plans"][0]["id"]
        self.assertEqual(self.status_of(self.post, f"/api/plans/{plan_id}/cancel"), 403)  # Needs the custom header.
        self.assertEqual(self.post(f"/api/plans/{plan_id}/cancel", {"X-Duo-Action": "1"}), 200)
        self.assertEqual(json.loads(self.get("/api/state")[1])["orders"]["plans"], [])
        self.assertEqual(self.engine.db.one("SELECT status FROM plans")["status"], "cancelled")

    def test_page_has_a_locked_down_content_policy(self):
        self.start()
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"innerHTML", body)

    def test_agent_mode_keeps_the_agents_limits_and_panels(self):
        """Systematic mode off: the agents trade, and the page shows what it always did."""
        self.start()
        state = json.loads(self.get("/api/state")[1])
        self.assertFalse(state["systematic_mode"])
        self.assertIsNone(state["systematic"])
        self.assertEqual(state["agents"], {"enabled": True, "can_trade": True, "talking": True, "label": ""})
        self.assertEqual(state["limits_kind"], "agents")
        self.assertEqual(state["limits_note"], "Enforced by code")
        self.assertEqual([limit["label"] for limit in state["limits"]], AGENT_LIMITS)
        self.assertFalse(any("note" in limit for limit in state["limits"]), "agent limits render exactly as before")
        self.assertEqual({limit["unit"] for limit in state["limits"]}, {"count", "pct", "usd"})
        page = self.get("/")[1].decode()
        for part in AGENT_PAGE_PARTS:
            self.assertIn(part, page)
        self.assertEqual(self.post("/api/wake", {"X-Duo-Action": "1"}), 200)


class SystematicDashboard(unittest.TestCase):
    """Systematic mode: the runner's own limits, and nothing that says the agents decide anything."""

    @classmethod
    def setUpClass(cls):
        cls.env = make_env()
        cls.engine = Engine(cls.env.config, cls.env.db, cls.env.market, cls.env.broker, systematic=cls.env.runner)
        # The decision, then two and a half hours of loops: what the resting buys leave crosses at 120 minutes.
        for t in [*range(D0 + 60, D0 + 400, 10), *range(D0 + 600, D0 + 9000, 300)]:
            cls.env.market.now = cls.env.binance.now = t
            cls.engine.tick(t)
        cls.last_tick = t
        # Read on the fake clock, a minute after the last loop, so the loop counts as current.
        with mock.patch("time.time", return_value=t + 60):
            cls.state = json.loads(json.dumps(Dashboard(cls.engine, cls.env.config).state()))

    def limit(self, label):
        matches = [limit for limit in self.state["limits"] if limit["label"].startswith(label)]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def test_limits_are_the_runners_own_with_live_readings(self):
        s, state = self.env.config.systematic, self.state
        self.assertTrue(state["systematic_mode"])
        self.assertEqual(state["limits_kind"], "systematic")
        self.assertIn("Systematic runner", state["limits_note"])
        labels = [limit["label"] for limit in state["limits"]]
        self.assertFalse({"Buys today", "Fees, last 30 days", "Largest holding", "Drawdown from peak",
                          "Opus decisions today", "API spend today", "API spend this month"} & set(labels))
        controls = {c["control"]: c for c in state["systematic"]["controls"]}
        self.assertEqual(set(controls), {"kill_switch", "daily_loss", "stale_data", "asset_limit", "gross_exposure"})

        gross = self.limit("Gross exposure")
        self.assertEqual(gross["max"], float(s.max_gross_pct))
        self.assertEqual(gross["used"], controls["gross_exposure"]["value"])
        self.assertGreater(gross["used"], 0, "the decision bought something")
        daily = self.limit("Loss today")
        self.assertEqual((daily["used"], daily["max"]), (controls["daily_loss"]["value"], float(s.daily_loss_pct)))
        kill = self.limit("Kill switch")
        self.assertEqual((kill["used"], kill["max"]), (controls["kill_switch"]["value"], float(s.kill_drawdown_pct)))
        bar = self.limit("Daily bar missing")
        self.assertEqual((bar["used"], bar["max"], bar["unit"]),
                         (controls["stale_data"]["value"], float(s.signal_stale_minutes), "minutes"))
        quote_age = self.limit("Kraken quote age")
        self.assertEqual((quote_age["used"], quote_age["max"], quote_age["unit"]),
                         (max(state["systematic"]["heartbeat"]["quote_age_s"], 0.0), float(s.max_quote_age_seconds), "seconds"))
        self.assertIn(str(s.max_frozen_quote_seconds), quote_age["note"])
        errors = self.limit("API errors in a row")
        self.assertEqual((errors["used"], errors["max"]), (0, s.max_api_errors))
        order = self.limit("Per order")
        self.assertEqual(order["unit"], "rule")
        self.assertIn(f"{s.order_margin_pct}% of equity", order["text"])

        per_asset = self.limit("Per asset (")
        asset = per_asset["label"][len("Per asset ("):-1]
        holding = next(r for r in state["systematic"]["holdings"] if r["asset"] == asset)
        limits = state["systematic"]["asset_limits"]
        self.assertAlmostEqual(per_asset["used"], holding["weight"] * 100, places=9)
        self.assertAlmostEqual(per_asset["max"], limits[asset] * 100, places=9)
        self.assertLessEqual(per_asset["max"], float(s.max_asset_pct))
        others = [r for r in state["systematic"]["holdings"] if r["qty"]]
        self.assertTrue(all(r["weight"] / limits[r["asset"]] <= holding["weight"] / limits[asset] for r in others),
                        "the holding shown is the one nearest its limit")

    def test_thresholds_follow_the_config(self):
        env = make_env(daily_loss_pct=6, kill_drawdown_pct=30, max_api_errors=3, max_quote_age_seconds=45)
        engine = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
        # Before the first loop and with no prices yet: the limits still show, at the config's thresholds.
        limits = {limit["label"]: limit for limit in Dashboard(engine, env.config).state()["limits"]}
        self.assertEqual(limits["Loss today"]["max"], 6.0)
        self.assertEqual(limits["Kill switch: drawdown from peak"]["max"], 30.0)
        self.assertEqual(limits["API errors in a row"]["max"], 3)
        self.assertEqual(limits["Kraken quote age"]["max"], 45.0)

    def test_readings_the_runner_hasnt_taken_show_as_unknown(self):
        """The runner fails closed on an unknown reading, so the page mustn't show it as 0."""
        limits = Dashboard(self.engine, self.env.config)._sys_limits(None)
        self.assertEqual([limit["used"] for limit in limits], [None] * len(limits))
        self.assertTrue(all(limit["max"] is not None for limit in limits))

    def test_a_stale_loop_shows_every_reading_as_unknown(self):
        """Old readings mustn't look current and healthy when the loop hasn't run lately and nothing is checked."""
        dashboard = Dashboard(self.engine, self.env.config)
        heartbeat_ts = self.state["systematic"]["heartbeat"]["ts"]
        stale_after = self.env.config.systematic.loop_minutes * 60 * 2 + 60
        with mock.patch("time.time", return_value=heartbeat_ts + 60):
            fresh_snapshot = self.engine.systematic.snapshot()
        fresh = dashboard._sys_limits(fresh_snapshot, now=heartbeat_ts + 60)
        self.assertTrue(any(limit["used"] is not None for limit in fresh))
        self.assertIn("Last loop", dashboard._sys_limits_note(fresh_snapshot, now=heartbeat_ts + 60))
        stale = dashboard._sys_limits(fresh_snapshot, now=heartbeat_ts + stale_after + 1)
        self.assertEqual([limit["used"] for limit in stale], [None] * len(stale))
        self.assertTrue(all(limit["max"] is not None for limit in stale))
        self.assertIn("too long ago", dashboard._sys_limits_note(fresh_snapshot, now=heartbeat_ts + stale_after + 1))
        # The same through the API: three days after the fake loops, the header and the limits agree.
        with mock.patch("time.time", return_value=heartbeat_ts + 3 * 86400):
            state = json.loads(json.dumps(dashboard.state()))
        self.assertEqual(state["status"]["code"], "warn")
        self.assertEqual([limit["used"] for limit in state["limits"]], [None] * len(state["limits"]))
        self.assertIn("unknown", state["limits_note"])

    def test_limit_notes_say_the_runner_blocks_at_the_threshold(self):
        s = self.env.config.systematic
        controls = Controls(s)
        self.assertFalse(controls.staleness(Halts(api_errors=s.max_api_errors), 0.0).passed)
        self.assertFalse(controls.drawdown(100 - float(s.kill_drawdown_pct), 100).passed)
        self.assertFalse(controls.day_loss(100 - float(s.daily_loss_pct), 100).passed)
        notes = {limit["label"]: limit.get("note", "") for limit in self.state["limits"]}
        self.assertTrue(notes["API errors in a row"].startswith("At this many"))
        self.assertTrue(notes["Daily bar missing after 00:00 UTC"].startswith("At or past this"))
        self.assertTrue(notes["Kill switch: drawdown from peak"].startswith("At or past this"))
        self.assertTrue(notes["Loss today"].startswith("At or past this"))
        self.assertFalse(any(note.startswith("Past this") for note in notes.values()))

    def test_stop_and_resume_say_nothing_about_agent_trading(self):
        env = make_env()
        engine = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
        for t in range(D0 + 60, D0 + 400, 10):
            env.market.now = env.binance.now = t
            engine.tick(t)
        base = serve_dashboard(self, Dashboard(engine, env.config))
        for path, kill in (("/api/halt", "1"), ("/api/resume", "0")):
            request = urllib.request.Request(base + path, method="POST", data=b"", headers={"X-Duo-Action": "1"})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(env.db.get("sys_kill"), kill)
        with urllib.request.urlopen(base + "/api/events", timeout=5) as response:
            texts = [event["text"] for event in json.load(response)]
        self.assertFalse([text for text in texts if "Agent trading" in text], texts)
        self.assertTrue(any(text.startswith("Systematic trading halted: stopped from the dashboard") for text in texts))
        self.assertTrue(any(text.startswith("Systematic trading resumed by you") for text in texts))

    def test_an_escalating_desk_chat_asks_for_no_decision(self):
        env = make_env(agents_enabled=True)
        client = ScriptedClient([desk(text="BTC is above its 50-day line."),
                                 review(text="Agreed, worth a look.", escalate="BTC broke out; consider adding.")])
        engine = Engine(env.config, env.db, env.market, env.broker, client, systematic=env.runner)
        env.db.set("last_discussion", D0 + 10 ** 7)  # No desk chat on its own thread while the loops run.
        for t in range(D0 + 60, D0 + 400, 10):
            env.market.now = env.binance.now = t
            engine.tick(t)
        engine._run_discussion()
        dashboard = Dashboard(engine, env.config)
        session = dashboard.sessions("discussion")[0]
        self.assertEqual(session["outcome"], "flagged")
        self.assertEqual([m["verdict"] for m in session["messages"]], [None, "escalate"])
        self.assertIsNone(env.db.get("escalation"), "nothing waits on the flag")
        texts = [event["text"] for event in dashboard.events()]
        self.assertFalse([text for text in texts if "decision session" in text], texts)
        self.assertTrue(any(text.startswith("The desk flagged: BTC broke out") and "can't trade" in text
                            for text in texts))
        page = (STATIC / "index.html").read_text()
        self.assertIn('message.verdict === "escalate" && current && isSystematic(current)', page)
        self.assertIn("Flagged (no decision runs in this mode)", page)

    def test_agents_are_off_and_cant_trade(self):
        agents = self.state["agents"]
        self.assertEqual((agents["enabled"], agents["can_trade"], agents["talking"]), (False, False, False))
        self.assertIn("can't trade", agents["label"])
        self.assertFalse(self.state["status"]["agents_ready"])

    def test_wake_is_refused_and_the_page_can_hide_the_agent_panels(self):
        base = serve_dashboard(self, Dashboard(self.engine, self.env.config))
        request = urllib.request.Request(base + "/api/wake", method="POST", data=b"", headers={"X-Duo-Action": "1"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 409)
        self.assertIn(b"can't trade", caught.exception.read())
        self.assertNotEqual(self.env.db.get("wake_requested"), "1")
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            page = response.read().decode()
        for element in ("agents-note", "agent-grid", "feed-card", "feed-sub", "orders-card", "strategy-card",
                        "notes-card", "limits-card", "limits-note"):
            self.assertIn(f'id="{element}"', page)
        self.assertIn("state.systematic_mode", page)
        self.assertIn('state.limits_kind !== "systematic"', page)

    def test_switched_on_discussions_show_but_are_labelled_as_unable_to_trade(self):
        env = make_env(agents_enabled=True)
        quiet = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
        agents = Dashboard(quiet, env.config).state()["agents"]
        self.assertEqual((agents["enabled"], agents["can_trade"], agents["talking"]), (True, False, False))
        self.assertIn("can't trade", agents["label"])
        self.assertIn("ANTHROPIC_API_KEY", agents["label"])
        talking = Engine(env.config, env.db, env.market, env.broker, ScriptedClient([]), systematic=env.runner)
        agents = Dashboard(talking, env.config).state()["agents"]
        self.assertEqual((agents["enabled"], agents["can_trade"], agents["talking"]), (True, False, True))
        self.assertIn("can't trade", agents["label"])
        self.assertNotIn("ANTHROPIC_API_KEY", agents["label"])


if __name__ == "__main__":
    unittest.main()
