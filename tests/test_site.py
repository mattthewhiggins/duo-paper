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

from .sys_helpers import D0, DAY, cron_run, make_env, run_until, step

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


def build_site(env, now):
    out = Path(tempfile.mkdtemp())
    return site.build(env.config, env.db, out, now=now), out


def undecided(test, runs):
    """A run started at 10:00 UTC the day before, with hourly runs to 23:47, whose runs after 00:00 all fail to make
    the day's decision (its targets raise), at these minutes past 00:00 and 30 s late. Their heartbeats stay ok: a
    failed decision only logs an event."""
    env = btc_env(test, now=D0 - DAY + 10 * 3600)
    for minutes in [10 * 60] + [hour * 60 + 35 for hour in range(10, 24)] + [23 * 60 + 47]:
        cron_run(env, D0 - DAY + minutes * 60)
    with mock.patch("duo.systematic.SystematicRunner._plan", side_effect=RuntimeError("targets() raised")):
        for minutes in runs:
            cron_run(env, D0 + minutes * 60 + 30)
    return env


# The page's own script, run in node once per case on a status.json at a fixed clock, with just enough of a DOM. For
# each case it prints the text the script put in each element with an id (a line per block), the banner's state and
# anything it logged as an error.
RENDER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const [pagePath, casesPath] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, "utf8");
const script = html.slice(html.indexOf("<script>") + "<script>".length, html.lastIndexOf("</script>"));
const BLOCKS = new Set(["p", "li", "dt", "dd", "h3", "tr", "article", "br"]);

class Node {}
class Text extends Node {
  constructor(text) { super(); this.text = text; }
  get textContent() { return this.text; }
}
class Element extends Node {
  constructor(tag) {
    super();
    Object.assign(this, { tag, kids: [], dataset: {}, style: {}, hidden: false, className: "" });
  }
  setAttribute(name) { if (name === "hidden") this.hidden = true; }
  append(...kids) { for (const kid of kids) this.kids.push(typeof kid === "string" ? new Text(kid) : kid); }
  replaceChildren(...kids) { this.kids = []; this.append(...kids); }
  set textContent(text) { this.kids = [new Text(String(text))]; }
  get textContent() {
    const text = this.hidden ? "" : this.kids.map((kid) => kid.textContent).join("");
    return BLOCKS.has(this.tag) || this.className === "line-2" ? `\n${text}\n` : text;
  }
  set innerHTML(markup) { this.kids = []; }
  addEventListener() {}
  getBoundingClientRect() { return { width: 640, left: 0 }; }
  get clientWidth() { return 640; }
  get offsetWidth() { return 160; }
}

(async () => {
  const pages = [];
  for (const { status, now } of JSON.parse(fs.readFileSync(casesPath, "utf8"))) {
    const ids = new Map([...html.matchAll(/\sid="([\w-]+)"/g)].map((match) => [match[1], new Element("div")]));
    const errors = [];
    class FixedDate extends Date {
      constructor(...args) { if (args.length) super(...args); else super(now * 1000); }
      static now() { return now * 1000; }
    }
    vm.runInNewContext(script, {
      document: { getElementById: (id) => ids.get(id) || null, createElement: (tag) => new Element(tag),
                  createElementNS: (ns, tag) => new Element(tag), addEventListener() {}, hidden: false },
      window: { addEventListener() {} }, location: { protocol: "https:" }, Node, Date: FixedDate, Intl,
      console: { error: (...args) => errors.push(args.join(" ")) },
      setInterval() {}, setTimeout() {}, clearTimeout() {},
      fetch: async () => ({ ok: true, json: async () => status }),
    });
    await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
    const text = {};
    for (const [id, node] of ids) {
      text[id] = node.textContent.split("\n").map((line) => line.trim()).filter(Boolean).join("\n");
    }
    pages.push({ text, state: ids.get("banner").dataset.state, errors });
  }
  console.log(JSON.stringify(pages));
})();
"""


def render(test, cases):
    """[(status, now)] -> what the page shows for each: {"text": {id: text}, "state": the banner's, "errors": [...]}."""
    folder = Path(tempfile.mkdtemp())
    (folder / "render.js").write_text(RENDER)
    (folder / "cases.json").write_text(json.dumps([{"status": status, "now": now} for status, now in cases]))
    result = subprocess.run(["node", str(folder / "render.js"), str(site.PAGE), str(folder / "cases.json")],
                            capture_output=True, text=True, timeout=60)
    test.assertEqual(result.returncode, 0, result.stderr)
    pages = json.loads(result.stdout)
    for page in pages:
        test.assertEqual(page["errors"], [], "the page's script logged an error")
    return pages


def at(ts) -> str:
    """A time as the page writes it: Mon 2 Nov, 00:07 UTC."""
    when = datetime.fromtimestamp(ts, timezone.utc)
    return f"{when:%a} {when.day} {when:%b, %H:%M} UTC"


class Build(unittest.TestCase):
    def build(self, env, now):
        return build_site(env, now)

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
        self.assertEqual((start["days_compared"], start["complete"]), (1, False), "compare's finished days")

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
        # The start's own run is in the total, but not in the runs counted against the slots since the start.
        self.assertEqual((status["runs"]["total"], status["runs"]["last_24h"]), (1, 0))
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
        self.assertEqual(status["runs"]["last_24h"], 1300, "all but the start's own run")

    def test_the_chart_keeps_each_days_last_run(self):
        # 602 runs over three days, so 1 run in 2 is drawn. The first two days' last runs fall between the ones drawn.
        env = btc_env(self)
        step(env, D0 + 60)
        rows = [(D0 + day * DAY + 300 * (k + 1), "ok", "24950.00", "[]", "[]")
                for day, count in enumerate((201, 200, 200)) for k in range(count)]
        env.db.conn.executemany("INSERT INTO sys_heartbeats(ts, status, equity, halts, controls) VALUES(?, ?, ?, ?, ?)",
                                rows)
        status, _ = self.build(env, rows[-1][0] + 60)
        self.assertEqual(status["chart"]["every_nth_run"], 2)
        last_run, last_drawn = {}, {}
        for ts in [D0 + 60] + [row[0] for row in rows]:
            last_run[ts // DAY] = ts
        for point in status["chart"]["points"]:
            last_drawn[point["at"] // DAY] = point["at"]
        self.assertEqual(last_drawn, last_run)
        self.assertLessEqual(len(status["chart"]["points"]), site.MAX_CHART_POINTS)

    def test_an_overdue_decision_counts_from_when_the_watchdog_does(self):
        # The watchdog counts a scheduled run's decision overdue from cron_decision_grace_minutes (2 hours) after 00:00
        # UTC, not from signal_stale_minutes (30): so must the page.
        env = undecided(self, (1, 5, 20, 35, 50, 65, 80, 95, 110))
        status, _ = self.build(env, D0 + 111 * 60)
        self.assertEqual((status["today"]["decided"], status["today"]["overdue_minutes"]), (False, None))
        env = undecided(self, (1, 5, 20, 35, 50, 65, 80, 95, 110, 125, 140))
        beat = json.loads((env.config.data_dir / "heartbeat_systematic.json").read_text())
        self.assertEqual((beat["status"], beat["ts"]), ("ok", D0 + 140 * 60 + 30))
        status, _ = self.build(env, beat["ts"])
        self.assertEqual((status["today"]["decided"], status["today"]["overdue_from"]), (False, D0 + 120 * 60))
        self.assertAlmostEqual(status["today"]["overdue_minutes"], beat["decision_overdue_minutes"])
        self.assertEqual(status["heartbeat"]["status"], "ok")

    def test_no_decision_is_due_while_halted(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        env.runner.stop_trading(D0 + 700, "paused for a test", flatten=False)
        step(env, D0 + DAY + 300)
        step(env, D0 + DAY + 9 * 3600)
        status, _ = self.build(env, D0 + DAY + 9 * 3600 + 60)
        today = status["today"]
        self.assertEqual((today.get("halted"), today["decided"], today["due_from"], today["overdue_minutes"]),
                         (True, False, None, None))
        self.assertEqual(status["heartbeat"]["status"], "halted")

    def test_the_28_days_are_finished_days_compared(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)

        def compared(days):  # compare's summary, as if it had found this many finished days.
            report = {"ok": True, "error": None, "decisions": days + 1, "summary": {"days": days}, "notes": [],
                      "days": []}
            return mock.patch("duo.site._compare", return_value=(report, "# Paper against backtest\n"))

        with compared(27):
            status, _ = self.build(env, D0 + 40 * DAY)  # Long past 28 calendar days, but a finished day short.
        self.assertEqual((status["start"]["days_compared"], status["start"]["complete"]), (27, False))
        with compared(28):
            status, _ = self.build(env, D0 + 700)
        self.assertEqual((status["start"]["days_compared"], status["start"]["complete"]), (28, True))
        # The 28th finished day counts at day 29's decision, at the first run after 00:02 UTC at the earliest.
        self.assertEqual(status["start"]["ends_at"], D0 + 28 * DAY + 120)
        with mock.patch("duo.compare.compare", side_effect=RuntimeError("cost model missing")):
            status, _ = self.build(env, D0 + 40 * DAY)
        self.assertEqual((status["start"]["days_compared"], status["start"]["complete"]), (None, False))

    def test_runs_are_counted_against_the_slots_from_after_the_start(self):
        env = btc_env(self)  # Started at 00:01 UTC, before the day's first slot at 00:05.
        step(env, D0 + 60)

        def counts(now):
            runs = self.build(env, now)[0]["runs"]
            return runs["last_24h"], runs["scheduled_24h"], runs["last_7d"], runs["scheduled_7d"]

        self.assertEqual(counts(D0 + 90), (0, 0, 0, 0), "the start's own run isn't one of the slots")
        cron_run(env, D0 + 200)  # Started by hand, before the first slot.
        self.assertEqual(counts(D0 + 230), (1, 0, 1, 0))
        cron_run(env, D0 + 330)  # The 00:05 slot's run.
        self.assertEqual(counts(D0 + 360), (2, 1, 2, 1))
        self.assertEqual(counts(D0 + DAY + 360), (0, 37, 2, 38), "a day on, the last 24 hours have had no run")


class Words(unittest.TestCase):
    def test_reasons_in_plain_words(self):
        # The usual cause is no usable price for the holdings now: the day's start is known from its first run.
        self.assertEqual(site.plain_reason("daily loss unknown: no buys"),
                         "Today's loss can't be measured right now (no usable Kraken price for the holdings, or none "
                         "for the start of the day), so no buys for now. Sells still run.")
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


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class Rendered(unittest.TestCase):
    """What the page's script shows for status.json files built here, at a given time on the reader's clock."""

    HALTED = "No decision is made while the run is halted, until it's resumed with the workflow's resume action."

    def test_a_run_whose_tick_wrote_no_heartbeat_needs_attention(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        beat = env.db.one("SELECT MAX(ts) AS ts FROM sys_heartbeats")["ts"]
        # A run builds the page within minutes of its own heartbeat. 11 minutes on, the latest run's tick wrote none.
        fine, failed = build_site(env, beat + 300)[0], build_site(env, beat + 660)[0]
        ok, attention = render(self, [(fine, beat + 360), (failed, beat + 720)])
        self.assertEqual((ok["state"], ok["text"]["banner-title"]), ("ok", "Running normally"))
        self.assertEqual((attention["state"], attention["text"]["banner-title"]), ("attention", "Needs attention"))
        self.assertIn("The latest run's tick didn't finish: its Tick step, on the repository's Actions tab, says why. "
                      f"The figures below are from the last run that did, {at(beat)}", attention["text"]["banner-body"])

    def test_a_failed_tick_is_told_by_when_the_latest_tick_began(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        beat = env.db.one("SELECT MAX(ts) AS ts FROM sys_heartbeats")["ts"]
        env.db.set("sys_tick_at", beat)  # As python -m duo tick records it, before the heartbeat.
        slow_save = build_site(env, beat + 610)[0]  # A slow push: built 10 minutes after its own tick began.
        never_began = build_site(env, beat + 960)[0]  # After that tick's 15-minute job: this run's tick never began.
        env.db.set("sys_tick_at", beat + 300)  # A run 5 minutes after a late one, whose tick wrote no heartbeat.
        failed = build_site(env, beat + 330)[0]
        ok, *attention = render(self, [(slow_save, beat + 620), (never_began, beat + 970), (failed, beat + 340)])
        self.assertEqual((ok["state"], ok["text"]["banner-title"]), ("ok", "Running normally"))
        for page in attention:
            self.assertEqual((page["state"], page["text"]["banner-title"]), ("attention", "Needs attention"))
            self.assertIn("The latest run's tick didn't finish: its Tick step, on the repository's Actions tab, says "
                          f"why. The figures below are from the last run that did, {at(beat)}",
                          page["text"]["banner-body"])

    def test_an_overdue_decision_needs_attention(self):
        env = undecided(self, (1, 5, 20, 35, 50, 65, 80, 95, 110, 125, 140))
        status, _ = build_site(env, D0 + 141 * 60)
        self.assertEqual(status["heartbeat"]["status"], "ok")
        page, = render(self, [(status, D0 + 142 * 60)])
        text = page["text"]
        self.assertEqual((page["state"], text["banner-title"]), ("attention", "Needs attention"))
        self.assertIn("Today's decision is 22 minutes overdue, although runs are happening: the Tick step's log",
                      text["banner-body"])
        self.assertIn("Today's decision is 22 minutes overdue: see Run health below.", text["tiles"])
        self.assertIn("Today's decision\nNot made yet\nOverdue by 22 minutes: runs have happened since it was due, "
                      "and none has made it.", text["health-facts"])
        self.assertNotIn("due at the first run after", text["tiles"] + text["health-facts"])
        self.assertNotIn("may be late", text["health-facts"])

    def test_no_decision_is_due_while_halted(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        env.runner.stop_trading(D0 + 700, "paused for a test", flatten=False)
        same_day, _ = build_site(env, D0 + 760)  # Halted after the day's decision.
        step(env, D0 + DAY + 9 * 3600)
        status, _ = build_site(env, D0 + DAY + 9 * 3600 + 60)
        decided, page = render(self, [(same_day, D0 + 800), (status, D0 + DAY + 9 * 3600 + 120)])
        self.assertIn(f"Today's decision\nMade\n{self.HALTED}", decided["text"]["health-facts"])
        self.assertEqual(page["state"], "halted")
        self.assertIn(self.HALTED, page["text"]["tiles"])
        self.assertIn(f"Today's decision\nNone while halted\n{self.HALTED}", page["text"]["health-facts"])
        self.assertNotIn("due at the first run after", page["text"]["tiles"] + page["text"]["health-facts"])

    def test_today_is_the_readers_day(self):
        # Built at 23:40 UTC, after that day's decision, and read before and after the next 00:00.
        env, _ = two_days(self)
        step(env, D0 + DAY + 23 * 3600 + 35 * 60)
        status, _ = build_site(env, D0 + DAY + 23 * 3600 + 40 * 60)
        evening, night = render(self, [(status, D0 + DAY + 23 * 3600 + 50 * 60), (status, D0 + 2 * DAY + 25 * 60)])
        self.assertIn("The strategy today (paper)", evening["text"]["tiles"])
        self.assertIn("Today's decision\nMade", evening["text"]["health-facts"])
        self.assertIn(", started 2 November 2026: day 2, and 1 of 28 finished days compared.", evening["text"]["lede"])
        self.assertIn("The strategy's latest decision (paper)", night["text"]["tiles"])
        self.assertIn("Today's decision is due at the first run after Wed 4 Nov, 00:02 UTC.", night["text"]["tiles"])
        self.assertIn("Today's decision\nNot made yet\nDue at the first run after Wed 4 Nov, 00:02 UTC.",
                      night["text"]["health-facts"])
        self.assertIn(": day 3, and 1 of 28 finished days compared.", night["text"]["lede"])

    def test_finished_not_stale_once_the_28_days_are_compared(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        built = D0 + 700
        status, _ = build_site(env, built)
        report = {"ok": True, "error": None, "decisions": 29, "summary": {"days": 28}, "notes": [], "days": []}
        with mock.patch("duo.site._compare", return_value=(report, "# Paper against backtest\n")):
            complete, _ = build_site(env, built)
        late, stale, done, finished = render(self, [(status, built + 151 * 60), (status, built + 196 * 60),
                                                    (complete, built + 600), (complete, built + 196 * 60)])
        # A skipped hourly run and the next one an hour late is normal: 180 minutes, and the build and deploy.
        self.assertEqual(late["state"], "ok")
        self.assertEqual((stale["state"], stale["text"]["banner-title"]), ("stale", "Stale: not updated lately"))
        self.assertIn(": day 1, and 0 of 28 finished days compared.", late["text"]["lede"])
        self.assertIn("The 28th counts at the first run after Mon 30 Nov, 00:02 UTC, at the earliest.",
                      late["text"]["about-facts"])
        self.assertIn(": the 28-day test is complete (28 finished days compared), and the runs carry on until the "
                      "workflow is disabled (RUNBOOK section 12.6).", done["text"]["lede"])
        self.assertEqual(done["state"], "ok")
        self.assertEqual((finished["state"], finished["text"]["banner-title"]), ("finished", "Finished"))
        self.assertIn("RUNBOOK section 12.6", finished["text"]["banner-body"])

    def test_runs_against_the_slots_on_day_one(self):
        env = btc_env(self)  # Started at 00:01 UTC, before the day's first slot at 00:05.
        step(env, D0 + 60)
        first, _ = build_site(env, D0 + 90)
        cron_run(env, D0 + 200)  # Started by hand.
        by_hand, _ = build_site(env, D0 + 230)
        cron_run(env, D0 + 330)  # The 00:05 slot's run.
        slot, _ = build_site(env, D0 + 360)
        pages = render(self, [(first, D0 + 100), (by_hand, D0 + 240), (slot, D0 + 370)])
        said = [page["text"]["health-facts"] for page in pages]
        self.assertIn("Runs in the last 24 hours\nNone yet: no run has been scheduled since the start", said[0])
        self.assertIn("Runs in the last 24 hours\n1 run started by hand: no run has been scheduled since the start",
                      said[1])
        self.assertIn("Runs in the last 24 hours\n2 runs (including any started by hand) for 1 scheduled slot", said[2])

    def test_the_chart_note_counts_in_ordinals(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        status, _ = build_site(env, D0 + 700)
        words = {2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 23: "23rd",
                 101: "101st", 111: "111th", 112: "112th"}
        cases = []
        for n in words:
            edited = json.loads(json.dumps(status))
            edited["chart"]["every_nth_run"] = n
            cases.append((edited, D0 + 760))
        for page, word in zip(render(self, cases), words.values()):
            self.assertIn(f"Every {word} run is drawn", page["text"]["chart-note"])

    def test_the_tiles_say_what_the_paper_strategy_holds(self):
        env = btc_env(self)
        run_until(env, D0 + 60, D0 + 600, every=60)
        status, _ = build_site(env, D0 + 700)
        page, = render(self, [(status, D0 + 760)])
        tiles = page["text"]["tiles"]
        self.assertIn("The strategy today (paper)\nIn BTC\nSignal on: close", tiles)
        self.assertNotIn("Today's signal", tiles)
        since = tiles[tiles.index("Since the start"):]
        self.assertIn(status["equity"]["gbp_note"], since.split("Against just holding BTC")[0], "why pounds differ")
        self.assertIn("Not financial advice, and not a recommendation to buy, sell or hold anything.",
                      page["text"]["lede"])
        self.assertIn("Not financial advice, and not a recommendation to buy, sell or hold anything.",
                      page["text"]["about-body"])


class CommandLine(unittest.TestCase):
    def test_python_m_duo_site_builds_from_the_configured_database(self):
        env = btc_env(self)
        step(env, D0 + 60)
        out = Path(tempfile.mkdtemp()) / "site"
        with mock.patch("duo.config.load", return_value=env.raw_config), mock.patch("builtins.print") as said:
            site.main(["--out", str(out)])
        self.assertTrue((out / "status.json").exists() and (out / "index.html").exists())
        self.assertIn("Status page written", said.call_args[0][0])

    def test_a_database_as_saved_on_paper_state(self):
        # WAL mode, with no -wal or -shm beside it, as the workflow saves it: read as it is, making neither.
        env = btc_env(self)
        step(env, D0 + 60)
        env.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        saved = Path(tempfile.mkdtemp())
        shutil.copyfile(env.db.path, saved / env.db.path.name)
        self.assertEqual((saved / env.db.path.name).read_bytes()[18], 2, "a WAL database")
        out = Path(tempfile.mkdtemp()) / "site"
        with mock.patch("duo.config.load", return_value=replace(env.raw_config, data_dir=saved)), \
                mock.patch("builtins.print"):
            site.main(["--out", str(out)])
        self.assertEqual(strict((out / "status.json").read_text())["heartbeat"]["at"], D0 + 60)
        self.assertEqual([path.name for path in saved.iterdir()], [env.db.path.name], "nothing made beside it")

    def test_no_database_is_an_error_not_a_new_run(self):
        env = btc_env(self)
        missing = replace(env.raw_config, data_dir=Path(tempfile.mkdtemp()) / "nothing")
        with mock.patch("duo.config.load", return_value=missing), self.assertRaises(SystemExit) as stop:
            site.main(["--out", str(Path(tempfile.mkdtemp()))])
        self.assertIn("doesn't exist", str(stop.exception))
        self.assertFalse(missing.db_path.exists())


if __name__ == "__main__":
    unittest.main()
