"""Command line: python -m duo run | tick | preflight | status | reset-paper | backtest | backtest-years
                 | compare | decisions | watchdog | resume | probe | site"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import signal
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from . import config as config_module
from .agents import has_credentials
from .brokers import KrakenBroker, PaperBroker, preflight
from .config import D, LIVE_CONFIRM_PHRASE, Config, ConfigError
from .db import DB
from .engine import Engine
from .kraken import API_URL as KRAKEN_URL, KrakenClient, KrakenError, KrakenNetworkError
from .market import Market
from .web import Dashboard, serve

NO_RUN = 3  # python -m duo tick's exit code when there's no paper run to carry on.
KRAKEN_RETRY_PAUSES = (2, 3, 5)  # A tick's failed Kraken refresh is tried again after each of these: about 10 s in all.


def live_client() -> KrakenClient:
    return KrakenClient(os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET"))


def check_live(config: Config, client: KrakenClient, market: Market) -> None:
    """Refuses to start live trading unless every check passes."""
    if os.environ.get("DUO_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
        raise SystemExit(f'Live mode needs DUO_LIVE_CONFIRM="{LIVE_CONFIRM_PHRASE}" in .env.')
    problems = preflight(config, client, market)
    if problems:
        raise SystemExit("Not starting live trading:\n- " + "\n- ".join(problems))


def run_systematic(config: Config) -> None:
    """The systematic runner on the engine's loop. Paper only: prepare() refuses anything else."""
    from .sys_data import BinancePublic
    from .sys_market import SystematicMarket
    from .systematic import SystematicRunner, prepare, refuse_live

    refuse_live(config)
    db = DB(config.db_path)
    market = SystematicMarket(KrakenClient(), "USD", {"BTC": "XBT"})  # Public endpoints only: no key is given.
    effective, strategy, start = prepare(config, db, market)
    market.quote = effective.quote
    broker = PaperBroker(effective)
    runner = SystematicRunner(effective, db, market, broker, BinancePublic(config.systematic.binance_urls), strategy,
                              start)

    anthropic_client = None
    if config.systematic.agents_enabled and has_credentials():
        import anthropic
        anthropic_client = anthropic.Anthropic(timeout=300.0)
    engine = Engine(effective, db, market, broker, anthropic_client, systematic=runner)
    server = serve(Dashboard(engine, effective))

    print(f"Duo is running the systematic runner in PAPER mode: {strategy.id}.")
    for note in strategy.notes:
        print(f"Note: {note}")
    print(f"Paper capital {start['starting_capital']} {start['quote']} (GBP {start['starting_capital_gbp']}), "
          f"fees {start['maker_pct']}% maker, {start['taker_pct']}% taker. Database: {config.db_path}")
    print(f"Agents: {'talking, but they can never trade' if anthropic_client else 'off'}.")
    print(f"Dashboard: http://{config.host}:{config.port}")
    print("Press Ctrl-C to stop. While it's stopped, nothing is checked or traded. "
          "Run python -m duo watchdog to be told when it's down.")

    def shut_down(*_):
        engine.stop()

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    engine.run()
    server.shutdown()
    print("Stopped.")


# ---- python -m duo tick: one poll per process, for a scheduler (GitHub Actions) instead of the loop ----

def start_record_problem(path: Path) -> str | None:
    """Why path holds no paper run to carry on, or None if it does. Reads it without creating or changing anything."""
    if not path.exists():
        return f"{path} doesn't exist"
    # A WAL database opens read-only only beside its -wal and -shm files. Without them everything is in the main file,
    # and immutable reads it as it is, creating neither.
    uri = path.resolve().as_uri() + ("?mode=ro" if Path(f"{path}-wal").exists() else "?mode=ro&immutable=1")
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            found = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state'").fetchone() \
                and conn.execute("SELECT 1 FROM state WHERE key = 'sys_start'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as error:
        return f"{path} couldn't be read ({error})"
    return None if found else f"{path} holds no paper run (it has no start record)"


def retrying(refresh, sleep=time.sleep):
    """A tick's Kraken refresh: one that fails is tried again up to three times, about 10 s in all. The loop gets
    another go 10 s later anyway, but a scheduled run's next chance is 15 minutes or more away."""
    def refresh_with_retries():
        for pause in (*KRAKEN_RETRY_PAUSES, None):
            try:
                return refresh()
            except (KrakenError, KrakenNetworkError, ValueError):
                if pause is None:
                    raise
                sleep(pause)
    return refresh_with_retries


def close_db(db: DB) -> None:
    """Every commit into the main file, then closed: the .db file alone is then the whole state to save."""
    try:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.conn.close()


def tick_once(config: Config, market=None, binance=None, now: float | None = None, first_run: bool = False,
              flatten: bool = False, sleep=time.sleep) -> int:
    """python -m duo tick: one poll of the systematic runner, then out, for a scheduler that starts a fresh machine
    every 15 to 60 minutes with the database restored from the run before (GitHub Actions). market and binance default
    to Kraken's and Binance's public endpoints.

    Returns the exit code: 0 when this run wrote a heartbeat, 1 when the tick failed or wrote none, and 3 (NO_RUN) when
    there's no paper run to carry on. A database that wasn't restored must never start a new paper run by itself, so
    the first run has to say so: first_run, --first-run on the command line.
    """
    from .sys_data import BinancePublic
    from .sys_market import SystematicMarket
    from .systematic import refuse_live

    refuse_live(config)
    if not config.systematic_on:
        sys.exit("The systematic runner is off in config.toml ([systematic] enabled = false).")
    problem = start_record_problem(config.db_path)
    if problem and not first_run:
        print(f"Not ticking: {problem}. A database that wasn't restored must never start a new paper run by itself, "
              f"so nothing was done. If this really is the first run, say so: python -m duo tick --first-run")
        return NO_RUN
    if first_run and not problem:
        print(f"--first-run, but {config.db_path} already holds a paper run: carrying on with it.")
    if config.systematic.agents_enabled:
        print("Agent discussions don't run in a tick: one couldn't outlive the run.")
    now = time.time() if now is None else now
    if market is None:
        market = SystematicMarket(KrakenClient(), "USD", {"BTC": "XBT"})  # Public endpoints only: no key is given.
    market.refresh = retrying(market.refresh, sleep)
    if binance is None:
        binance = BinancePublic(config.systematic.binance_urls)
    db = DB(config.db_path)
    try:
        return _tick(config, db, market, binance, now, flatten)
    finally:
        close_db(db)


def _tick(config: Config, db: DB, market, binance, now: float, flatten: bool) -> int:
    from .systematic import SystematicRunner, prepare

    failure, since = None, None  # since: the newest heartbeat and fill from before this run, once it's got that far.
    try:
        db.set("sys_tick_at", now)  # The status page tells a tick that wrote no heartbeat by this.
        effective, strategy, start = prepare(config, db, market, now=now)
        market.quote = effective.quote
        broker = PaperBroker(effective)
        runner = SystematicRunner(effective, db, market, broker, binance, strategy, start, scheduled=True)
        engine = Engine(effective, db, market, broker, systematic=runner)  # No agents, so no discussion threads.
        since = {"heartbeat": db.one("SELECT COALESCE(MAX(id), 0) n FROM sys_heartbeats")["n"],
                 "fill": db.one("SELECT COALESCE(MAX(id), 0) n FROM fills")["n"]}
        if flatten:
            engine.request_flatten("the command line (python -m duo tick --flatten)")
        runner._track()  # A fresh market tracks BTC alone, so the tick's own refresh must be told the whole book.
        engine.tick(now)  # Once, and not through run_once, which would swallow a failure.
    except Exception:
        failure = traceback.format_exc(limit=6)
        try:
            db.event("critical", "error", f"The scheduled tick failed:\n{failure}")
        except Exception:
            pass  # The database may be what's broken. The exit code still says so.
    # decide() refreshes Kraken a second time, after on_tick saved what frozen-feed detection knows: saved again here,
    # or the next run would compare against the first refresh.
    try:
        if hasattr(market, "frozen_state"):
            db.set("sys_frozen_quotes", json.dumps(market.frozen_state(), sort_keys=True, default=str))
    except Exception:
        failure = failure or traceback.format_exc(limit=6)
    try:
        db.set("sys_gbpusd", json.dumps({"ts": now, "rate": str(market.gbp_usd())}))
    except Exception:  # Only for showing figures in pounds. Not a trading input.
        pass
    try:
        wrote, text, markdown = tick_summary(db, now, since, failure)
    except Exception:
        wrote, text, markdown = False, f"The tick's summary couldn't be read:\n{traceback.format_exc(limit=4)}", ""
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and markdown:
        try:
            with open(summary_path, "a") as out:
                out.write(markdown)
        except OSError as error:
            print(f"Couldn't add to the job summary at {summary_path}: {error}")
    return 0 if wrote and failure is None else 1


def tick_summary(db: DB, now: float, since: dict | None, failure: str | None) -> tuple[bool, str, str]:
    """(whether this run wrote a heartbeat, what it did as text, the same as markdown for the job's summary page)."""
    from .systematic import utc
    since = since or {"heartbeat": math.inf, "fill": math.inf}  # It failed before it began: nothing is its own.
    beat = db.one("SELECT * FROM sys_heartbeats WHERE id > ? ORDER BY id DESC LIMIT 1", (since["heartbeat"],))
    start = json.loads(db.get("sys_start", "null") or "null") or {}
    status = beat["status"] if beat else "no heartbeat written"
    head = f"Paper tick at {utc(now)}, {start.get('strategy_id', 'no paper run')}: {status}"
    lines = []
    if beat and beat["equity"]:
        lines.append(f"Equity {D(beat['equity']):.2f} {start.get('quote', '')}.")
    decision = db.one("SELECT day_ts, intended FROM sys_decisions WHERE decided_at = ?", (now,))
    if decision:
        lines.append(f"Decided on the bar of {utc(decision['day_ts'])[:10]}: "
                     f"{len(json.loads(decision['intended']))} order(s) to work.")
    for fill in db.all("SELECT * FROM fills WHERE id > ? ORDER BY id", (since["fill"],)):
        lines.append(f"Paper {fill['liquidity']} fill at {utc(fill['ts'])}: {fill['side']} "
                     f"{D(fill['qty']).normalize():f} {fill['asset']} at {D(fill['price']):.2f}, "
                     f"fee {D(fill['fee']):.2f}.")
    working = db.one("SELECT COUNT(*) n FROM sys_orders WHERE status = 'working'")["n"]
    resting = db.one("SELECT COUNT(*) n FROM sys_children WHERE status = 'open'")["n"]
    lines.append(f"{working} order(s) working, {resting} resting on the book.")
    lines += [f"Halt: {halt}" for halt in (json.loads(beat["halts"]) if beat else [])]
    if failure:
        lines.append("The tick failed. See the traceback below.")
    text = "\n".join([head + ".", *lines] + ([failure] if failure else []))
    markdown = "\n".join([f"### {head}", "", *(f"- {line}" for line in lines)]
                         + ([f"\n```\n{failure}```"] if failure else [])) + "\n\n"
    return beat is not None, text, markdown


# ---- python -m duo probe and site ----

def probe(config: Config, urlopen=urllib.request.urlopen, clock=time.monotonic) -> int:
    """python -m duo probe: can this machine reach the public market data the runner reads? One GET to each
    binance_urls host's daily klines, and Kraken's public Ticker and 1-minute OHLC for XBTUSD. No key, nothing
    private. Exit 0 if Kraken answers both and at least one Binance host answers, else 1."""
    from .sys_data import host
    from .systematic import utc

    def get(url: str):
        began = clock()
        try:
            with urlopen(url, timeout=15) as response:
                status, payload = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return f"HTTP {error.code}", None, (clock() - began) * 1000, str(error.reason)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as error:
            return "no answer", None, (clock() - began) * 1000, f"{type(error).__name__}: {error}"
        return f"HTTP {status}", payload, (clock() - began) * 1000, ""

    def kraken_result(payload):
        if isinstance(payload, dict) and not payload.get("error") and isinstance(payload.get("result"), dict):
            return {k: v for k, v in payload["result"].items() if k != "last"}
        return None

    working = []
    urls = config.systematic.binance_urls if config.systematic else config_module.BINANCE_URLS
    for base in urls:
        status, payload, ms, detail = get(f"{base}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=2")
        if isinstance(payload, list) and payload and isinstance(payload[-1], list):
            working.append(host(base))
            detail = f"newest bar {utc(int(payload[-1][0]) // 1000)}"
        print(f"Binance {host(base):28} {status:10} {ms:6.0f} ms  {detail or 'not daily klines'}")
    kraken_ok = True
    for what, path in (("Ticker", "Ticker?pair=XBTUSD"), ("OHLC 1m", "OHLC?pair=XBTUSD&interval=1")):
        status, payload, ms, detail = get(f"{KRAKEN_URL}/0/public/{path}")
        result = kraken_result(payload)
        rows = next(iter(result.values()), None) if result else None
        if what == "Ticker" and isinstance(rows, dict) and rows.get("c"):
            detail = f"last trade {rows['c'][0]}"
        elif what == "OHLC 1m" and isinstance(rows, list) and rows:
            detail = f"newest bar {utc(int(rows[-1][0]))}"
        else:
            kraken_ok = False
            detail = detail or f"no {what} in the reply: {str(payload)[:120]}"
        print(f"Kraken  {host(KRAKEN_URL) + ' ' + what:28} {status:10} {ms:6.0f} ms  {detail}")
    ok = kraken_ok and bool(working)
    print(("Binance answers from " + ", ".join(working) if working else "No Binance host answers")
          + (". Kraken answers." if kraken_ok else ". Kraken doesn't answer.")
          + (" The runner can poll from here." if ok else " The runner can't poll from here."))
    return 0 if ok else 1


def build_site(config: Config, out: Path) -> dict:
    """python -m duo site: the static status page, from the database alone (duo/site.py)."""
    from .site import build  # Imported only when asked for.
    db = systematic_db(config)
    try:
        return build(config, db, Path(out))
    finally:
        close_db(db)


def systematic_db(config: Config, command: str | None = None) -> DB:
    if not config.systematic_on:
        sys.exit("The systematic runner is off in config.toml ([systematic] enabled = false).")
    if not config.db_path.exists():
        # A copy of the GitHub run's database is only for reading: resuming one changes the copy, not the run.
        mac = ("On the Mac, this can't reach the GitHub run: resume that with the Paper run workflow's action resume "
               "(RUNBOOK 12.5)." if command == "resume" else
               "On the Mac, copy the GitHub run's database out first (RUNBOOK 12.7), or start a local run with "
               "./start.sh.")
        sys.exit(f"No systematic paper run here: {config.db_path} doesn't exist. On GitHub Actions, that means "
                 f"paper-state has no database: start the run with the Paper run workflow's action start (RUNBOOK "
                 f"12.3). {mac}")
    return DB(config.db_path)


def systematic_status(config: Config) -> None:
    import json
    from .systematic import utc
    db = systematic_db(config)
    start = json.loads(db.get("sys_start", "null") or "null")
    heartbeat = db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
    decision = db.one("SELECT day_ts, decided_at, equity, intended FROM sys_decisions ORDER BY id DESC LIMIT 1")
    print(f"Strategy    {start['strategy_id'] if start else config.systematic.strategy}")
    if start:
        print(f"Capital     {start['starting_capital']} {start['quote']} (GBP {start['starting_capital_gbp']})")
    if heartbeat:
        print(f"Heartbeat   {utc(heartbeat['ts'])}, {heartbeat['status']}, equity {heartbeat['equity']}")
        for halt in json.loads(heartbeat["halts"]):
            print(f"            {halt}")
    if decision:
        print(f"Decision    bar of {utc(decision['day_ts'])}, made {utc(decision['decided_at'])}, "
              f"{len(json.loads(decision['intended']))} order(s)")
    if db.get("sys_kill") == "1":
        print(f"HALTED      {db.get('sys_kill_reason')}. Resume with: python -m duo resume")


def run(config: Config) -> None:
    if config.systematic_on:
        run_systematic(config)
        return
    if config.mode == "live":
        client = live_client()
        market = Market(client, config.assets, config.quote)
        check_live(config, client, market)
        broker = KrakenBroker(client, market)
    else:
        market = Market(KrakenClient(), config.assets, config.quote)
        broker = PaperBroker(config)

    anthropic_client = None
    if has_credentials():
        import anthropic
        anthropic_client = anthropic.Anthropic(timeout=300.0)

    db = DB(config.db_path)
    engine = Engine(config, db, market, broker, anthropic_client)
    server = serve(Dashboard(engine, config))

    print(f"Duo is running in {config.mode.upper()} mode.")
    print(f"Dashboard: http://{config.host}:{config.port}")
    if anthropic_client is None:
        print("The agents are off until ANTHROPIC_API_KEY is in .env. Prices are still being tracked.")
    print("Press Ctrl-C to stop. While it's stopped, no orders are placed and stop-losses don't run.")

    def shut_down(*_):
        engine.stop()

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    engine.run()
    server.shutdown()
    print("Stopped.")


def status(config: Config) -> None:
    if config.systematic_on:
        systematic_status(config)
        return
    db_path = config.db_path
    if not db_path.exists():
        print(f"No {config.mode} data yet. Start it with: python -m duo run")
        return
    market = Market(KrakenClient(), config.assets, config.quote)
    market.refresh()
    engine = Engine(config, DB(db_path), market, PaperBroker(config))
    metrics = engine.metrics()
    if metrics is None:
        print("No prices available.")
        return
    print(f"Mode        {config.mode}")
    print(f"Equity      {metrics['equity']:.2f} {config.quote}")
    print(f"Net P&L     {metrics['net_pnl']:+.2f} ({metrics['net_pnl_pct']:+.2f}%)")
    print(f"Hold BTC    {metrics['benchmark_pnl']:+.2f}")
    print(f"Fees        {metrics['fees']:.2f}")
    print(f"API costs   {metrics['api_cost']:.2f}")


def reset_paper(config: Config) -> None:
    path = config.data_dir / "paper.db"
    if not path.exists():
        print("There's no paper history to delete.")
        return
    answer = input(f"Delete all paper trades and conversations in {path}? Type yes: ")
    if answer.strip().lower() != "yes":
        print("Nothing deleted.")
        return
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    print("Paper history deleted.")


def command_line() -> argparse.ArgumentParser:
    """The commands and their options. The workflow (.github/workflows/paper.yml) runs some of them, and
    tests/test_workflow.py checks each one it runs is here."""
    parser = argparse.ArgumentParser(prog="duo", description="Two Claude agents trading a small crypto portfolio.")
    parser.add_argument("command", choices=["run", "tick", "preflight", "status", "reset-paper", "backtest",
                                            "backtest-years", "compare", "decisions", "watchdog", "resume", "probe",
                                            "site"])
    parser.add_argument("--out", help="compare: the markdown file (default data/compare_systematic.md). "
                                      "decisions: the JSONL file (default data/decisions_systematic.jsonl), - for "
                                      "stdout. site: the folder for the status page (default data/site)")
    parser.add_argument("--first-run", action="store_true",
                        help="tick: start a new paper run if there's none. Without it a tick with no run does nothing")
    parser.add_argument("--flatten", action="store_true", help="tick: sell everything and halt, as the dashboard's "
                                                              "Sell everything does")
    return parser


def main() -> None:
    args = command_line().parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # So messages reach service logs right away.

    config_module.load_env()
    try:
        config = config_module.load()
    except ConfigError as error:
        sys.exit(f"Config problem: {error}")

    if args.command == "run":
        run(config)
    elif args.command == "tick":
        sys.exit(tick_once(config, first_run=args.first_run, flatten=args.flatten))
    elif args.command == "probe":
        sys.exit(probe(config))
    elif args.command == "site":
        from .site import problems as site_problems
        out = Path(args.out or config.data_dir / "site")
        built = build_site(config, out)
        # A part that couldn't be built is on the page, and named here for the job's log. The rest is still written.
        print(f"Status page written to {out}." + "".join(f"\n- {p}" for p in site_problems(built)))
    elif args.command == "preflight":
        client = live_client()
        market = Market(client, config.assets, config.quote)
        problems = preflight(config, client, market)
        if problems:
            sys.exit("Problems found:\n- " + "\n- ".join(problems))
        print("All live checks passed. No trades were placed.")
    elif args.command == "status":
        status(config)
    elif args.command == "reset-paper":
        reset_paper(config)
    elif args.command == "backtest":
        from . import backtest
        backtest.main(config.data_dir)
    elif args.command == "backtest-years":
        from . import backtest
        backtest.yearly_main(config.data_dir)
    elif args.command == "compare":
        from . import compare
        report = compare.markdown(compare.compare(systematic_db(config)))
        out = Path(args.out or config.data_dir / "compare_systematic.md")
        out.write_text(report)
        print(report)
        print(f"Written to {out}")
    elif args.command == "decisions":
        import json
        from .systematic import export_decisions
        lines = [json.dumps(item, sort_keys=True, default=str) for item in export_decisions(systematic_db(config))]
        if args.out == "-":
            print("\n".join(lines))
        else:
            out = Path(args.out or config.data_dir / "decisions_systematic.jsonl")
            out.write_text("".join(line + "\n" for line in lines))
            print(f"{len(lines)} decision(s) written to {out}")
    elif args.command == "watchdog":
        from .watchdog import check
        stale = config.systematic.heartbeat_stale_minutes if config.systematic else 15
        degraded = config.systematic.degraded_alert_minutes if config.systematic else 30
        ok, lines = check(config.data_dir / "heartbeat_systematic.json", stale, degraded_minutes=degraded)
        print("\n".join(lines))
        sys.exit(0 if ok else 1)
    elif args.command == "resume":
        from .systematic import resume_in_db
        print(resume_in_db(systematic_db(config, "resume")))


if __name__ == "__main__":
    main()
