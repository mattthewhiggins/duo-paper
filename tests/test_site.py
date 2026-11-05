"""The status page for GitHub Pages (duo/site.py): what status.json holds, that every number in it is finite, that a
failing compare is reported and not raised, that building it never changes the run's database, and that the page is
one self-contained file. No network: the runner's stand-ins (tests/sys_helpers.py) make the decisions and fills,
through the engine as a scheduled run does, so the hold-BTC comparison has its benchmark and snapshots too."""
import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from duo import site
from duo.db import DB
from duo.engine import Engine
from duo.ledger import Portfolio

from .sys_helpers import D0, DAY, make_env, run_until, step

STRATEGY = "F1__sma_50__BTC__vol20"  # config.toml's strategy.
KEYS = {"generated_at", "generated_at_utc", "strategy", "start", "heartbeat", "today", "holdings", "equity", "hold_btc",
        "chart", "decisions", "orders", "fills", "fill_totals", "runs", "compare", "files", "notes", "errors"}


def btc_env(test, strategy=STRATEGY, **overrides):
    # max_asset_pct as config.toml has it: the strategy's one sleeve is the whole account.
    env = make_env(strategy, max_asset_pct=Decimal(100), **overrides)
    test.addCleanup(env.db.conn.close)
    return env


def tick(env, engine, t, prices=None):
    """One engine poll at t: Kraken first, then the runner, then the engine's snapshot."""
    if prices:
        env.market.prices.update(prices)
    env.market.now = env.binance.now = t
    engine.tick(t)


def ticks(env, engine, start, end, every):
    t = start
    while t <= end:
        tick(env, engine, t)
        t += every


def two_days(test):
    """Day 1 buys BTC and the order fills as a maker. On day 2 Kraken's price is well up, so BTC's weight is past its
    band and a sell works until it crosses the spread as a taker. Returns the env and a time after it all."""
    env = btc_env(test)
    engine = Engine(env.config, env.db, env.market, env.broker, systematic=env.runner)
    ticks(env, engine, D0 + 60, D0 + 300, 60)
    child = env.db.one("SELECT * FROM sys_children WHERE status = 'open'")
    tick(env, engine, D0 + 360, {"BTC": float(child["limit_price"]) * 0.999})  # Traded through: fills as a maker.
    ticks(env, engine, D0 + 3600, D0 + DAY - 600, 3600)
    tick(env, engine, D0 + DAY + 60, {"BTC": env.market.prices["BTC"] * 2})
    ticks(env, engine, D0 + DAY + 120, D0 + DAY + 120 + 7500, 300)
    return env, D0 + DAY + 3 * 3600


def numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from numbers(item)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


def times(value):
    """Every (epoch, ISO) pair the file gives for one time: a key and the same key + _utc."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_utc") and key[:-4] in value:
                yield value[key[:-4]], item
            yield from times(item)
    elif isinstance(value, list):
        for item in value:
            yield from times(item)


def strict(text):
    """status.json as a strict reader sees it: NaN and Infinity aren't JSON."""
    def refuse(constant):
        raise ValueError(f"{constant} in status.json")
    return json.loads(text, parse_constant=refuse)


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class Build(unittest.TestCase):
    def build(self, env, now):
        out = Path(tempfile.mkdtemp())
        return site.build(env.config, env.db, out, now=now), out

    def check_file(self, status, out):
        loaded = strict((out / "status.json").read_text())
        self.assertEqual(loaded, status)
        self.assertTrue(KEYS <= set(status), KEYS - set(status))
        self.assertTrue(all(math.isfinite(n) for n in numbers(status)))
        for ts, iso in times(status):
            expected = None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertEqual(iso, expected)
        self.assertEqual((out / "index.html").read_bytes(), site.PAGE.read_bytes())
        self.assertEqual(status["errors"], {})

    def test_a_run_with_decisions_and_fills(self):
        env, now = two_days(self)
        status, out = self.build(env, now)
        self.check_file(status, out)
        db = env.db
        fills = db.all("SELECT * FROM fills ORDER BY id")
        self.assertEqual([f["liquidity"] for f in fills], ["maker", "taker"], "the scenario: a maker buy, a taker sell")

        self.assertEqual(status["strategy"]["id"], STRATEGY)
        self.assertIn("50-day average", status["strategy"]["description"])
        self.assertIn("20% a year", status["strategy"]["description"])
        start = status["start"]
        self.assertEqual((start["capital_gbp"], start["gbp_usd"], start["maker_pct"], start["taker_pct"]),
                         (20000, 1.25, 0.22, 0.38))
        self.assertEqual(start["capital_usd"], float(env.start["starting_capital"]))
        self.assertEqual((start["day"], start["target_days"], start["date"]), (2, 28, "2026-11-02"))

        beats = db.all("SELECT * FROM sys_heartbeats ORDER BY id")
        self.assertEqual(status["heartbeat"]["at"], beats[-1]["ts"])
        self.assertEqual(status["heartbeat"]["status"], "ok")
        self.assertEqual(status["heartbeat"]["reasons"], [])

        # The books: the ledger's cash and BTC, at the runner's own valuation.
        book = Portfolio.replay(Decimal(env.start["starting_capital"]), fills)
        held = status["holdings"]
        self.assertAlmostEqual(held["btc_qty"], float(book.qty("BTC")), places=12)
        self.assertAlmostEqual(held["cash"], float(book.cash), places=6)
        equity = status["equity"]
        self.assertAlmostEqual(equity["usd"], float(beats[-1]["equity"]), places=6)
        self.assertAlmostEqual(held["btc_value"] + held["cash"], equity["usd"], places=6)
        self.assertAlmostEqual(held["btc_weight"], held["btc_value"] / equity["usd"], places=9)
        self.assertEqual(equity["gbp_usd"]["source"], "decision")
        self.assertAlmostEqual(equity["gbp"], equity["usd"] / 1.25, places=6)
        self.assertAlmostEqual(equity["return_usd"], equity["usd"] / start["capital_usd"] - 1, places=12)

        # Just holding BTC: the engine's benchmark at its last snapshot.
        hold = status["hold_btc"]
        snap = db.one("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1")
        self.assertAlmostEqual(hold["qty"], float(db.get("benchmark_qty")), places=12)
        self.assertAlmostEqual(hold["usd"], snap["benchmark"], places=6)
        self.assertAlmostEqual(hold["vs_hold"], equity["return_usd"] - hold["return_usd"], places=12)

        chart = status["chart"]
        self.assertEqual(len(chart["points"]), len(beats))
        self.assertEqual(chart["points"][-1]["at"], beats[-1]["ts"])
        self.assertTrue(all(p["hold"] is not None for p in chart["points"]), "each run has its snapshot")
        self.assertIsNotNone(chart["hold_source"])

        # Decisions, newest first, with the figures the strategy used.
        decisions = status["decisions"]
        self.assertEqual([d["bar_date"] for d in decisions], ["2026-11-02", "2026-11-01"])
        latest = decisions[0]
        closes = [r[4] for r in env.bars["BTC"] if r[0] <= D0]
        self.assertAlmostEqual(latest["close"], closes[-1], places=6)
        self.assertAlmostEqual(latest["average"], sum(closes[-50:]) / 50, places=6)
        self.assertEqual((latest["average_days"], latest["signal_on"]), (50, True))
        row = db.one("SELECT * FROM sys_decisions WHERE day_ts = ?", (D0,))
        self.assertEqual(latest["volatility"], json.loads(row["signals"])["BTC"]["vol"])
        self.assertEqual(latest["target_weight"], json.loads(row["targets"])["BTC"]["weight"])
        self.assertTrue(latest["summary"].startswith("Sell about"), latest["summary"])
        self.assertTrue(decisions[1]["summary"].startswith("Buy about"), decisions[1]["summary"])
        self.assertEqual([o["status"] for o in decisions[1]["orders"]], ["filled"])
        self.assertIn("as a maker", decisions[1]["orders"][0]["outcome"])

        self.assertEqual([f["liquidity"] for f in status["fills"]], ["taker", "maker"], "newest first")
        totals = status["fill_totals"]
        self.assertEqual((totals["fills"], totals["maker"], totals["taker"]), (2, 1, 1))
        self.assertAlmostEqual(totals["fees"], float(sum(Decimal(f["fee"]) for f in fills)), places=9)
        self.assertEqual(len(status["orders"]), len(db.all("SELECT id FROM sys_orders")))

        runs = status["runs"]
        self.assertEqual(runs["total"], len(beats))
        self.assertEqual(runs["last_24h"], sum(now - DAY < b["ts"] <= now for b in beats))
        self.assertEqual(runs["last_at"], beats[-1]["ts"])
        self.assertEqual(runs["schedule"]["per_day"], 37)

        self.assertTrue(status["compare"]["ok"])
        self.assertEqual(status["compare"]["summary"]["days"], 1)
        self.assertEqual(status["compare"]["summary"]["maker_fills"], 1)
        self.assertTrue((out / "compare.md").read_text().startswith("# Paper against backtest"))
        lines = (out / "decisions.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["day_ts"] for line in lines], [D0 - DAY, D0])
        self.assertEqual(status["files"], {"page": "index.html", "status": "status.json", "compare": "compare.md",
                                           "decisions": "decisions.jsonl", "decisions_error": None})
        self.assertTrue(status["notes"]["paper"].startswith("Paper money only."))
        self.assertIn("holdout", status["notes"]["holdout"])
        self.assertEqual(len(status["notes"]["differences"]), 3)

    def test_a_brand_new_run_with_no_decision_yet(self):
        env = btc_env(self)
        status, out = self.build(env, D0 + 60)
        self.check_file(status, out)
        self.assertIsNone(status["heartbeat"])
        self.assertEqual((status["decisions"], status["fills"], status["chart"]["points"]), ([], [], []))
        self.assertEqual(status["equity"]["usd"], float(env.start["starting_capital"]), "all cash")
        self.assertEqual(status["equity"]["gbp_usd"]["source"], "start")
        self.assertEqual((status["holdings"]["btc_qty"], status["holdings"]["btc_weight"]), (0, 0))
        self.assertIsNone(status["hold_btc"], "no engine tick yet, so no benchmark")
        self.assertEqual(status["start"]["day"], 1)
        self.assertTrue(status["compare"]["ok"])
        self.assertEqual(status["compare"]["notes"], ["No decisions yet."])

        step(env, D0 + 60)  # One run before the settle: a heartbeat, and still no decision.
        status, out = self.build(env, D0 + 90)
        self.check_file(status, out)
        self.assertEqual(status["heartbeat"]["status"], "ok")
        self.assertEqual(status["decisions"], [])
        self.assertEqual((status["today"]["decided"], status["today"]["due_from"]), (False, D0 + 120))
        self.assertEqual((status["runs"]["total"], status["runs"]["last_24h"]), (1, 1))
        self.assertEqual(len(status["chart"]["points"]), 1)

    def test_a_database_with_no_run_in_it(self):
        env = btc_env(self)
        empty = DB(Path(tempfile.mkdtemp()) / "paper_systematic.db")
        self.addCleanup(empty.conn.close)
        out = Path(tempfile.mkdtemp())
        status = site.build(env.config, empty, out, now=D0)
        self.check_file(status, out)
        self.assertIsNone(status["start"])
        self.assertEqual(status["compare"]["notes"], ["No paper run yet."])
        self.assertFalse((out / "compare.md").exists())
        self.assertIsNone(status["files"]["decisions"])

    def test_a_failing_compare_is_reported_not_raised(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        out = Path(tempfile.mkdtemp())
        site.build(env.config, env.db, out, now=D0 + 700)
        self.assertTrue((out / "compare.md").exists())
        with mock.patch("duo.compare.compare", side_effect=RuntimeError("cost model missing")):
            status = site.build(env.config, env.db, out, now=D0 + 700)
        self.assertEqual(status["compare"]["ok"], False)
        self.assertEqual(status["compare"]["error"], "RuntimeError: cost model missing")
        self.assertFalse((out / "compare.md").exists(), "no link to an old report")
        self.assertIsNone(status["files"]["compare"])
        self.assertEqual(strict((out / "status.json").read_text()), status)
        self.assertTrue((out / "index.html").exists() and (out / "decisions.jsonl").exists())
        self.assertEqual(len(status["decisions"]), 1, "everything else is still built")
        self.assertEqual(status["errors"], {})

    def test_a_failing_section_is_reported_and_the_rest_still_built(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        with mock.patch("duo.site._chart", side_effect=KeyError("benchmark")):
            status, out = self.build(env, D0 + 700)
        self.assertEqual(status["errors"], {"chart": "KeyError: 'benchmark'"})
        self.assertIsNone(status["chart"])
        self.assertEqual(len(status["decisions"]), 1)
        self.assertEqual(strict((out / "status.json").read_text()), status)

    def test_building_never_changes_the_database(self):
        env, now = two_days(self)
        path = env.db.path
        wal = Path(f"{path}-wal")

        def state():
            reader = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                return sha(path), sha(wal), "\n".join(reader.iterdump())
            finally:
                reader.close()

        before = state()
        self.build(env, now)
        self.assertEqual(state(), before)
        reader = site.ReadOnlyDB(path)
        with self.assertRaises(sqlite3.OperationalError):
            reader.conn.execute("INSERT INTO state(key, value) VALUES('x', 'y')")
        reader.close()

    def test_gbp_uses_the_newest_rate_and_says_which(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        env.db.set("sys_gbpusd", json.dumps({"ts": D0 + 650, "rate": "1.30"}))
        status, _ = self.build(env, D0 + 700)
        fx = status["equity"]["gbp_usd"]
        self.assertEqual((fx["source"], fx["rate"], fx["at"]), ("latest", 1.3, D0 + 650))
        self.assertAlmostEqual(status["equity"]["gbp"], status["equity"]["usd"] / 1.3, places=6)
        self.assertIn("GBP/USD 1.3000", status["equity"]["gbp_note"])
        env.db.set("sys_gbpusd", json.dumps({"ts": D0 - 60, "rate": "1.30"}))  # Older than the decision's.
        status, _ = self.build(env, D0 + 700)
        self.assertEqual((status["equity"]["gbp_usd"]["source"], status["equity"]["gbp_usd"]["rate"]), ("decision", 1.25))
        env.db.set("sys_gbpusd", "not json")
        status, _ = self.build(env, D0 + 700)
        self.assertEqual(status["equity"]["gbp_usd"]["source"], "decision")

    def test_a_long_run_is_drawn_in_at_most_about_500_points(self):
        env = btc_env(self)
        step(env, D0 + 60)
        rows = [(D0 + 120 + k * 60, "ok", "24950.00", "[]", "[]") for k in range(1300)]
        env.db.conn.executemany("INSERT INTO sys_heartbeats(ts, status, equity, halts, controls) VALUES(?, ?, ?, ?, ?)",
                                rows)
        status, _ = self.build(env, rows[-1][0] + 60)
        points = status["chart"]["points"]
        self.assertLessEqual(len(points), site.MAX_CHART_POINTS + 1)
        self.assertEqual(points[-1]["at"], rows[-1][0], "always the latest run")
        self.assertEqual(status["chart"]["runs_valued"], 1301)
        self.assertEqual(status["runs"]["last_24h"], 1301)


class Words(unittest.TestCase):
    def test_reasons_in_plain_words(self):
        self.assertIn("no buys", site.plain_reason("daily loss unknown: no buys"))
        self.assertEqual(site.plain_reason("daily loss limit: no buys today", Decimal(8)),
                         "Down 8% or more since 00:00 UTC, so no buys until tomorrow. Sells still run.")
        self.assertIn("5 requests to the exchanges failed in a row", site.plain_reason("5 API errors in a row: no new orders"))
        self.assertIn("Kraken's price for BTC", site.plain_reason(
            "Kraken quote missing, old or frozen for BTC: no new orders on those"))
        self.assertTrue(site.plain_reason("kill switch: equity 1.00 is 50% below its peak").startswith(
            "The kill switch is on: equity 1.00"))
        self.assertEqual(site.plain_reason("something new"), "Something new", "unknown ones are shown as written")

    def test_the_schedule(self):
        self.assertEqual(len(site.schedule_slots()), 37)
        self.assertEqual(site.next_run(D0), D0 + 5 * 60)
        self.assertEqual(site.next_run(D0 + 3 * 3600 + 50 * 60), D0 + 4 * 3600 + 35 * 60, "03:50, then 04:35")
        self.assertEqual(site.next_run(D0 + 23 * 3600 + 40 * 60), D0 + 23 * 3600 + 47 * 60)
        self.assertEqual(site.next_run(D0 + 23 * 3600 + 50 * 60), D0 + DAY + 5 * 60)
        self.assertEqual(site.scheduled_between(D0, D0 + DAY), 37)
        self.assertEqual(site.scheduled_between(D0, D0 + 3600), 4)

    def test_describe(self):
        start = btc_env(self).start
        self.assertEqual(site.describe(start), "Holds BTC while its daily close is above its 50-day average, sized so the "
                                               "account swings about 20% a year, and holds cash otherwise. One decision "
                                               "a day, just after 00:00 UTC.")
        self.assertIn("F1__ENS__BTC_ETH__vol40", site.describe(btc_env(self, "F1__ENS__BTC_ETH__vol40").start))


class Elements(HTMLParser):
    """Tags opened and closed, ids, and the text of each script and style."""

    VOID = {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.ids, self.scripts, self.styles, self.attrs, self.errors = [], set(), [], [], [], []
        self._in = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.attrs.append((tag, attrs))
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag not in self.VOID:
            self.stack.append(tag)
        self._in = tag if tag in ("script", "style") else None

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")
        self._in = None

    def handle_data(self, data):
        if self._in == "script":
            self.scripts.append(data)
        elif self._in == "style":
            self.styles.append(data)


class Page(unittest.TestCase):
    def setUp(self):
        self.text = site.PAGE.read_text()
        self.page = Elements()
        self.page.feed(self.text)
        self.script = "".join(self.page.scripts)

    def test_it_parses_and_is_titled(self):
        self.assertEqual(self.page.errors, [])
        self.assertEqual(self.page.stack, [])
        self.assertIn("<title>Duo paper run</title>", self.text)
        self.assertEqual(len(self.page.scripts), 1)

    def test_it_makes_no_external_requests(self):
        for tag, attrs in self.page.attrs:
            for name in ("src", "href"):
                self.assertFalse(re.match(r"\s*(https?:)?//", attrs.get(name) or ""), f"<{tag} {name}={attrs.get(name)}>")
        css = "".join(self.page.styles)
        self.assertNotIn("@import", css)
        self.assertNotRegex(css, r"url\(\s*['\"]?(https?:)?//")
        self.assertNotRegex(self.script, r"fetch\(\s*['\"`](https?:)?//")
        self.assertIn("connect-src 'self'", self.text)
        self.assertRegex(self.script, r"fetch\(`status\.json\?t=\$\{Date\.now\(\)\}`")

    def test_every_element_the_script_fills_is_on_the_page(self):
        made = {"chart-svg-title", "chart-svg-desc", "compare-days"}  # Drawn by the script itself.
        wanted = set(re.findall(r"""(?:byId|fill|table|facts|paragraphs)\(\s*["']([\w-]+)["']""", self.script)) - made
        self.assertTrue(wanted)
        self.assertEqual(wanted - self.page.ids, set())

    def test_colours_are_tokens_with_a_dark_variant(self):
        css = "".join(self.page.styles)
        self.assertRegex(css, r":root\s*\{[^}]*--page:")
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertRegex(css, r"body\s*\{[^}]*background: var\(--page\)")

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_the_script_has_no_syntax_errors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as out:
            out.write(self.script)
        result = subprocess.run(["node", "--check", out.name], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class CommandLine(unittest.TestCase):
    def test_python_m_duo_site_builds_from_the_configured_database(self):
        env = btc_env(self)
        step(env, D0 + 60)
        out = Path(tempfile.mkdtemp()) / "site"
        with mock.patch("duo.config.load", return_value=env.raw_config), mock.patch("builtins.print") as said:
            site.main(["--out", str(out)])
        self.assertTrue((out / "status.json").exists() and (out / "index.html").exists())
        self.assertIn("Status page written", said.call_args[0][0])

    def test_no_database_is_an_error_not_a_new_run(self):
        env = btc_env(self)
        missing = replace(env.raw_config, data_dir=Path(tempfile.mkdtemp()) / "nothing")
        with mock.patch("duo.config.load", return_value=missing), self.assertRaises(SystemExit) as stop:
            site.main(["--out", str(Path(tempfile.mkdtemp()))])
        self.assertIn("doesn't exist", str(stop.exception))
        self.assertFalse(missing.db_path.exists())


if __name__ == "__main__":
    unittest.main()
