"""The paper run's status page: a folder of static files, rebuilt from the run's database by whichever host runs it.

It knows two hosts, and judges the run by the schedule of the one it's on:
- cron, on GitHub Actions: each scheduled run is a fresh machine. It restores the run's database, polls once, saves the
  database, then builds this folder and publishes it to GitHub Pages.
- loop, on Render: python -m duo run, the laptop's own loop, runs nonstop beside the database on the service's disk,
  and the service rebuilds this folder every 5 minutes and serves it.
It's loop when the build is told so (--schedule loop) or the database has a heartbeat after the last tick (sys_tick_at,
which only python -m duo tick writes), and cron otherwise.

build() reads the database through a second connection that can't write, so nothing here can change the run, and
writes:

- status.json: everything the page shows. Every number is finite, and every time is given twice, as epoch seconds and
  as ISO 8601 UTC under the same name + _utc.
- index.html: duo/static/site.html as it is. It fetches status.json and draws it, and makes no other request.
- compare.md and decisions.jsonl: what python -m duo compare and python -m duo decisions write, when they work. If one
  fails, status.json says why and everything else is still written.

The figures are the runner's own records: the start record (state key sys_start), heartbeats, decisions, orders and
fills, and for the hold-BTC comparison the engine's benchmark_qty and snapshots. Nothing here calls an exchange.

python -m duo site --out DIR builds it from the database config.toml points at, as the GitHub workflow does. That
command also opens the database read-write (DB()) and checkpoints it on the way out (PRAGMA wal_checkpoint(TRUNCATE)),
which is fine where nothing else runs, and never beside a live loop. python -m duo.site --out DIR [--schedule loop]
builds the same page from config.toml alone, never .env, and opens the database read only: it's the build to run beside
a live loop, as Render's service does. With --schedule loop and no database yet, as on Render before the cutover, it
writes a page that says so.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import D, SYSTEMATIC_DEFAULTS, Config, ConfigError
from .db import DB
from .ledger import Portfolio
from .market import BENCHMARK_ASSET
from .sys_risk import finite
from .systematic import DAY, day_start, export_decisions

PAGE = Path(__file__).parent / "static" / "site.html"
TARGET_DAYS = 28                # The finished days compared the owner set out to watch (RUNBOOK section 1).
STALE_MINUTES = 195             # On GitHub the page calls itself stale past this: an hourly slot, a skipped run and an
                                # hour of GitHub's delay (180, the runner's cron_gap_minutes), plus the build and the
                                # deploy. On Render it's rebuilt every loop, so it's heartbeat_stale_minutes and a loop.
HOLD_FILE = "HOLD"              # On Render the bot stays stopped while this is in the data folder (RUNBOOK 13.6).
SEED_FILE = "render_seed.json"  # Render's seed marker, in the data folder: where GitHub's runs ended, at the cutover.
MAX_CHART_POINTS = 500          # Past this, every Nth run is drawn, and each day's last.
RECENT_DECISIONS = 10
RECENT_ORDERS = 10
RECENT_FILLS = 30
COMPARE_DAYS = 14
SNAPSHOT_MATCH_SECONDS = 600    # A snapshot this close to a heartbeat prices holding BTC at that heartbeat.
# The GitHub workflow's cron lines, in UTC. Copied from it by hand, so the page can say when the next run is due.
SCHEDULE = ("5,20,35,50 0-3 * * *", "35 4-23 * * *", "47 23 * * *")
SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€"}

HEARTBEAT_STATUS = {"ok": "Running normally", "degraded": "Needs attention", "halted": "Halted"}
ORDER_STATUS = {
    "working": "Working", "filled": "Filled", "superseded": "Replaced by the next decision",
    "blocked": "Blocked by a risk limit", "cancelled": "Cancelled", "dust": "Too small to trade",
}
# The heartbeat's portfolio controls (duo/sys_risk.py), named for the page: what each reads, and in what.
CONTROLS = {
    "kill_switch": ("Drawdown from the peak (the kill switch sells everything at its limit)", "%"),
    "daily_loss": ("Loss since 00:00 UTC (no buys past its limit)", "%"),
    "stale_data": ("Daily bar overdue (no new orders past its limit)", "minutes"),
    "quote_fresh": ("Age of Kraken's price", "seconds"),
    "asset_limit": ("Largest holding, share of the account", "%"),
    "gross_exposure": ("Everything held, share of the account", "%"),
    "order_cap": ("Order size, share of the account", "%"),
}
NOTES = {
    "paper": "Paper money only. Orders are simulated against real public prices: no money is at risk, no exchange "
             "account or key is used, and nothing here can place a real order. Not financial advice, and not a "
             "recommendation to buy, sell or hold anything.",
    "holdout": "This strategy did not pass the research's holdout gate: its Sharpe ratio on the held-out years fell "
               "below half its Sharpe ratio on the development years. This run is to watch how it behaves on live "
               "prices, not proof that it works.",
    "schedule": "It runs on GitHub Actions: every 15 minutes from 00:05 to 03:50 UTC, then hourly at 35 minutes past "
                "the hour, and at 23:47. Each run restores the database, checks the market once, saves the database "
                "and rebuilds this page. GitHub starts scheduled runs late when it's busy, often by 5 to 60 minutes, "
                "and sometimes skips them.",
    "differences": [
        "The daily decision is made at the first run after 00:02 UTC, not at 00:02 exactly, so it can trade at later "
        "prices than the laptop would have.",
        "A resting order is judged on Kraken's 1-minute candles for the time between runs: it counts as filled only "
        "if the price traded through it. The laptop checked live quotes every 10 seconds.",
        "The risk checks (kill switch, daily loss limit, stale data) run once per run, not every 5 minutes, so a move "
        "between runs is only acted on at the next one.",
    ],
}
# The same notes on Render, where the laptop's own loop runs: its schedule, and the little that differs from the laptop.
NOTES_LOOP = {
    **NOTES,
    "schedule": "It runs nonstop on a Render web service, as it ran on the laptop: python -m duo run checks Kraken's "
                "prices every 10 seconds and runs the strategy's loop every 5 minutes from 00:02 UTC, with a heartbeat "
                "each time. The database stays on the service's disk, and the service rebuilds this page from it, read "
                "only, every 5 minutes.",
    "differences": [
        "The daily decision, the fills and the risk checks work as on the laptop: the decision is made at the first "
        "loop from 00:02 UTC, a resting order is checked against Kraken's live quotes every 10 seconds, and the kill "
        "switch, daily loss limit and stale data checks run every 5 minutes.",
        "Its dashboard can't be reached, so halting, flattening and resuming are typed in the service's Shell on "
        "Render (RUNBOOK section 13.5).",
        "Render stops and restarts the service for deploys and maintenance. A gap of over 11 minutes between checks "
        "counts as an outage: a resting order from before it closes unfilled, as nothing watched it in between.",
        "A failed request to Kraken counts at each 10-second check, so about 50 seconds of Kraken errors in a row "
        "stops new orders until it answers again.",
        "GBP/USD is read with each day's decision, so the figures in pounds follow it once a day.",
        "The days before the move ran on GitHub Actions, once per scheduled run: the decision at the first run after "
        "00:02 UTC, resting orders judged on Kraken's 1-minute candles, and the risk checks once per run.",
    ],
}


class ReadOnlyDB:
    """The run's database through a connection that can't write: whatever is called with it, the run can't change.

    It has the read side of DB (all, one, get), which is all compare and export_decisions use.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        # A WAL database opens read-only only beside its -wal and -shm files. Without them, as it's saved on
        # paper-state, everything is in the main file, and immutable reads it as it is, creating neither.
        mode = "?mode=ro" if Path(f"{self.path}-wal").exists() else "?mode=ro&immutable=1"
        self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}{mode}", uri=True, timeout=30,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.tables = {row["name"] for row in self.all("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM state WHERE key = ?", (key,)) if "state" in self.tables else None
        return row["value"] if row else default

    def close(self) -> None:
        self.conn.close()


# ---- Small helpers ----

def _n(value) -> float | None:
    """A number for status.json: a float, or None when it's missing or not finite."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _dec(value) -> Decimal | None:
    return D(value) if finite(value) else None


def _iso(ts) -> str | None:
    ts = _n(ts)
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(name: str, ts) -> dict:
    """A time, twice: {name: epoch seconds, name_utc: ISO 8601}. Both None when it isn't known."""
    return {name: _n(ts), f"{name}_utc": _iso(ts)}


def _date(ts) -> str | None:
    return None if _n(ts) is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _clock(ts) -> str:
    when = datetime.fromtimestamp(ts, timezone.utc)
    return f"{when.day} {when:%b %H:%M} UTC"


def _money(value, quote: str) -> str:
    symbol = SYMBOLS.get(quote)
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {quote}"


def _loads(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _setting(config: Config, name: str):
    """A [systematic] setting, or its default when the config has no such section or key."""
    return getattr(config.systematic, name, SYSTEMATIC_DEFAULTS.get(name))


def _finite(value):
    """status.json as JSON can hold it: Decimals as floats, and anything not finite as null."""
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, (float, Decimal)):
        return _n(value)
    return str(value)


def _put(path: Path, text: str | None) -> None:
    """Writes atomically, so the site never serves half a file. None removes it, so nothing links to an old one."""
    if text is None:
        path.unlink(missing_ok=True)
        return
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temp, 0o644)  # mkstemp makes it private, and Pages serves what it can read.
    os.replace(temp, path)


# ---- The schedule ----

def _cron_field(field: str, size: int) -> list[int]:
    if field == "*":
        return list(range(size))
    out = []
    for part in field.split(","):
        low, _, high = part.partition("-")
        out.extend(range(int(low), int(high or low) + 1))
    return out


def schedule_slots() -> list[int]:
    """Minutes after 00:00 UTC at which a run is scheduled, from the workflow's cron lines."""
    minutes = set()
    for line in SCHEDULE:
        minute, hour = line.split()[:2]
        minutes.update(h * 60 + m for h in _cron_field(hour, 24) for m in _cron_field(minute, 60))
    return sorted(minutes)


def next_run(now: float) -> float:
    """When the next scheduled run is due. GitHub often starts it later, and sometimes not at all."""
    slots, day = schedule_slots(), day_start(now)
    later = [day + m * 60 for m in slots if day + m * 60 > now]
    return later[0] if later else day + DAY + slots[0] * 60


def scheduled_between(since: float, until: float) -> int:
    """How many runs were scheduled after `since`, up to and including `until`."""
    slots, count, day = schedule_slots(), 0, day_start(since)
    while day <= until:
        count += sum(since < day + m * 60 <= until for m in slots)
        day += DAY
    return count


# ---- Words ----

def plain_reason(reason: str, daily_loss_pct=8) -> str:
    """A heartbeat's reason for not trading normally, in plain words. One this doesn't know is shown as written."""
    text = reason.strip()
    if text.startswith("kill switch: "):
        return f"The kill switch is on: {text[len('kill switch: '):]}. Nothing new trades until it's resumed."
    if text == "daily loss limit: no buys today":
        return f"Down {daily_loss_pct}% or more since 00:00 UTC, so no buys until tomorrow. Sells still run."
    if text == "daily loss unknown: no buys":
        return ("Today's loss can't be measured right now (no usable Kraken price for the holdings, or none for the "
                "start of the day), so no buys for now. Sells still run.")
    if text == "daily bar missing: no new orders":
        return "Yesterday's daily bar hasn't arrived from Binance yet, so no new orders until it does."
    if text == "equity unknown: no new orders":
        return "The account can't be valued right now (no usable Kraken price), so no new orders."
    errors = re.fullmatch(r"(\d+) API errors in a row: no new orders", text)
    if errors:
        return f"{errors[1]} requests to the exchanges failed in a row, so no new orders until one works."
    frozen = "Kraken quote missing, old or frozen for "
    if text.startswith(frozen):
        assets = text[len(frozen):].split(":", 1)[0]
        return f"Kraken's price for {assets} is missing, too old or not moving, so no new orders on it."
    for prefix in ("Kraken prices for the decision: ", "waiting for Kraken prices for "):
        if text.startswith(prefix):
            return f"Today's decision is waiting for Kraken's prices: {text[len(prefix):]}."
    return text[:1].upper() + text[1:]


def describe(start: dict) -> str:
    """The strategy in a sentence, from its research parameters."""
    p, family = start.get("params") or {}, start.get("family")
    if family == "F1" and p.get("universe") == "BTC":
        rule = {
            "sma": lambda: f"its daily close is above its {p['L']}-day average",
            "ret": lambda: f"its daily close is above its close {p['L']} days earlier",
            "cross": lambda: f"its {p['F']}-day average is above its {p['S']}-day average",
        }.get(p.get("signal"))
        if rule:
            size = (f", sized so the account swings about {p['target']:.0%} a year" if "target" in p
                    else ", with the whole account")
            return f"Holds BTC while {rule()}{size}, and holds cash otherwise. One decision a day, just after 00:00 UTC."
    return f"The research strategy {start.get('strategy_id')}, decided by code once a day, just after 00:00 UTC."


# ---- Sections ----

def _start(config: Config, start: dict, now: float) -> dict:
    quote = start.get("quote", "USD")
    capital, rate = D(start["starting_capital"]), D(start.get("gbp_usd") or 1)
    started = float(start["started_at"])
    # Day 1 is the start's UTC day. Day 28 is the 28th finished day once the next day's decision compares it: at the
    # first run after this, or later if a day had no decision.
    ends = day_start(started) + TARGET_DAYS * DAY + _setting(config, "settle_minutes") * 60
    return {
        **_at("at", started), "date": _date(started), "quote": quote,
        "capital_gbp": D(start["starting_capital_gbp"]), "capital": capital,
        "capital_usd": capital if quote == "USD" else capital * rate,
        "gbp_usd": rate, "funding_fx_bps": D(start.get("funding_fx_bps") or 0),
        "maker_pct": D(start["maker_pct"]), "taker_pct": D(start["taker_pct"]),
        "trade_through_pct": config.paper_trade_through_pct,
        "day": max(1, (day_start(now) - day_start(started)) // DAY + 1), "target_days": TARGET_DAYS,
        **_at("ends_at", ends), "days_compared": None, "complete": False,  # build() sets both, from compare.
    }


def _strategy(ro: ReadOnlyDB, start: dict, loop: bool) -> dict:
    latest = ro.one("SELECT signal_source FROM sys_decisions ORDER BY id DESC LIMIT 1") \
        if "sys_decisions" in ro.tables else None
    return {
        "id": start["strategy_id"], "family": start.get("family"), "params": start.get("params") or {},
        "description": describe(start),
        "decides": f"Once a day, at {'the first loop from' if loop else 'the first run after'} 00:02 UTC, on the daily "
                   f"bar that closed at 00:00 UTC.",
        "signal_source": latest["signal_source"] if latest else None,
        "price_source": "Kraken's public BTC/USD prices, for the paper fills and the account's value.",
        "notes": start.get("notes") or [],
        "research_code_sha256": start.get("research_code_sha256"),
    }


def _heartbeat(config: Config, ro: ReadOnlyDB) -> dict | None:
    row = ro.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
    if row is None:
        return None
    reasons = _loads(row["halts"]) or []
    # The heartbeat file says how the runner was scheduled, but only speaks for this heartbeat if it's the same one.
    payload = _loads(_read(Path(config.data_dir) / "heartbeat_systematic.json")) or {}
    same = _n(payload.get("ts")) is not None and abs(_n(payload.get("ts")) - row["ts"]) < 1
    controls = []
    for check in _loads(row["controls"]) or []:
        label, unit = CONTROLS.get(check.get("control"), (check.get("control"), ""))
        controls.append({"control": check.get("control"), "label": label, "unit": unit, "value": check.get("value"),
                         "threshold": check.get("threshold"), "passed": check.get("outcome") == "pass",
                         "detail": check.get("detail")})
    gap = _n(row["loop_gap_s"])
    return {
        **_at("at", row["ts"]), "status": row["status"],
        "status_text": HEARTBEAT_STATUS.get(row["status"], row["status"]),
        "reasons": [plain_reason(r, _setting(config, "daily_loss_pct")) for r in reasons], "reasons_raw": reasons,
        "equity": _dec(row["equity"]), "gap_minutes": None if gap is None else gap / 60,
        "schedule": payload.get("schedule") if same else None,
        "kill_switch": {"on": ro.get("sys_kill") == "1", "reason": ro.get("sys_kill_reason")},
        "controls": controls,
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _fx(ro: ReadOnlyDB, start: dict) -> dict:
    """GBP/USD for the figures in pounds: the newest reading there is, and which one it was."""
    candidates = [(float(start["started_at"]), D(start.get("gbp_usd") or 1), "start",
                   "the start's GBP/USD, as there's no later reading")]
    if "sys_decisions" in ro.tables:
        row = ro.one("SELECT decided_at, gbp_usd FROM sys_decisions WHERE gbp_usd IS NOT NULL ORDER BY id DESC LIMIT 1")
        if row is not None and finite(row["gbp_usd"]) and D(row["gbp_usd"]) > 0:
            candidates.append((row["decided_at"], D(row["gbp_usd"]), "decision",
                               "the GBP/USD logged with the latest decision"))
    latest = _loads(ro.get("sys_gbpusd"))
    if isinstance(latest, dict) and finite(latest.get("rate")) and D(latest["rate"]) > 0 and finite(latest.get("ts")):
        candidates.append((float(latest["ts"]), D(latest["rate"]), "latest", "the latest GBP/USD reading"))
    ts, rate, source, text = max(candidates, key=lambda c: c[0])
    return {"rate": rate, **_at("at", ts), "source": source, "text": text}


def _valuation(ro: ReadOnlyDB, start: dict) -> dict:
    """The books now: the ledger's cash and holdings, priced as of the last run that could price them.

    A holding's price is the one the runner's own valuation implies (its heartbeat's equity less the cash then), or for
    BTC the engine's snapshot (benchmark / benchmark_qty, a Kraken mid), whichever is newer. All cash needs no price.
    """
    capital = D(start["starting_capital"])
    fills = ro.all("SELECT * FROM fills ORDER BY id")
    book = Portfolio.replay(capital, fills)
    held = {a: p.qty for a, p in book.positions.items() if p.qty > 0}
    marks = {}
    beat = ro.one("SELECT ts, equity FROM sys_heartbeats WHERE equity IS NOT NULL ORDER BY id DESC LIMIT 1") \
        if "sys_heartbeats" in ro.tables else None
    if beat is not None and finite(beat["equity"]):
        then = Portfolio.replay(capital, [f for f in fills if f["ts"] <= beat["ts"]])
        held_then = {a: p.qty for a, p in then.positions.items() if p.qty > 0}
        if len(held_then) == 1:
            (asset, qty), = held_then.items()
            marks[asset] = ((D(beat["equity"]) - then.cash) / qty, beat["ts"],
                            "the runner's own valuation at its last run, at Kraken's mid price")
    benchmark_qty = _dec(ro.get("benchmark_qty"))
    snap = ro.one("SELECT ts, benchmark FROM snapshots ORDER BY ts DESC LIMIT 1") if "snapshots" in ro.tables else None
    if snap is not None and benchmark_qty and benchmark_qty > 0 and finite(snap["benchmark"]) and (
            BENCHMARK_ASSET not in marks or snap["ts"] > marks[BENCHMARK_ASSET][1]):
        marks[BENCHMARK_ASSET] = (D(snap["benchmark"]) / benchmark_qty, snap["ts"], "Kraken's mid price at the last run")
    last_run = ro.one("SELECT MAX(ts) AS ts FROM sys_heartbeats")["ts"] if "sys_heartbeats" in ro.tables else None
    if not held:
        equity, at, how = book.cash, last_run or float(start["started_at"]), "all cash, so no price is needed"
    elif all(a in marks for a in held):
        equity = book.cash + sum((qty * marks[a][0] for a, qty in held.items()), Decimal(0))
        at = min(marks[a][1] for a in held)
        how = "; ".join(sorted({marks[a][2] for a in held}))
    elif beat is not None:  # More than one holding and no price for each: the runner's own total stands.
        equity, at, how = D(beat["equity"]), beat["ts"], "the runner's own valuation at its last run"
    else:
        equity, at, how = None, None, "no run has priced the holdings yet"
    return {"book": book, "held": held, "marks": marks, "equity": equity, "at": at, "how": how,
            "benchmark_qty": benchmark_qty}


def _holdings(valuation: dict) -> dict:
    book, equity, marks = valuation["book"], valuation["equity"], valuation["marks"]
    rows = []
    for asset in sorted(set(valuation["held"]) | {BENCHMARK_ASSET}):
        qty = valuation["held"].get(asset, Decimal(0))
        price, priced_at = marks[asset][:2] if asset in marks else (None, None)
        value = qty * price if price is not None else (Decimal(0) if qty == 0 else None)
        rows.append({"asset": asset, "qty": qty, "price": price, **_at("priced_at", priced_at), "value": value,
                     "weight": value / equity if value is not None and equity else None})
    btc = next(r for r in rows if r["asset"] == BENCHMARK_ASSET)
    return {"assets": rows, "cash": book.cash, "cash_weight": book.cash / equity if equity else None,
            "btc_qty": btc["qty"], "btc_price": btc["price"], "btc_value": btc["value"], "btc_weight": btc["weight"],
            **_at("btc_priced_at", btc["priced_at"]),
            "price_source": marks[BENCHMARK_ASSET][2] if BENCHMARK_ASSET in marks else None,
            "fees_paid": book.fees, "realized": book.realized}


def _equity(start: dict, valuation: dict, fx: dict) -> dict | None:
    equity = valuation["equity"]
    if equity is None:
        return None
    quote, rate = start.get("quote", "USD"), fx["rate"]
    capital, capital_gbp = D(start["starting_capital"]), D(start["starting_capital_gbp"])
    if quote == "GBP":
        gbp, usd, capital_usd = equity, equity * rate, capital * D(start.get("gbp_usd") or 1)
    else:
        usd, gbp, capital_usd = equity, equity / rate, capital
    fx_cost = D(start.get("funding_fx_bps") or 0) / 100
    return {
        "quote": quote, "value": equity, "usd": usd, "gbp": gbp, **_at("at", valuation["at"]), "how": valuation["how"],
        "start_usd": capital_usd, "start_gbp": capital_gbp,
        "change_usd": usd - capital_usd, "return_usd": usd / capital_usd - 1,
        "change_gbp": gbp - capital_gbp, "return_gbp": gbp / capital_gbp - 1,
        "gbp_usd": fx,
        "gbp_note": (f"Pounds are the account's dollars at GBP/USD {rate:.4f} ({fx['text']}). The change in pounds also "
                     f"counts the {fx_cost:.2f}% it cost to turn the £{capital_gbp:,} into dollars at the start, and "
                     f"any move in GBP/USD since." if quote == "USD" else None),
    }


def _hold_btc(ro: ReadOnlyDB, start: dict, valuation: dict, equity: dict | None, fx: dict) -> dict | None:
    """Just holding BTC: the engine's benchmark, all the starting capital in BTC at the first price the run saw."""
    qty, mark = valuation["benchmark_qty"], valuation["marks"].get(BENCHMARK_ASSET)
    if not qty or qty <= 0 or mark is None or start.get("quote", "USD") != "USD":
        return None
    first = ro.one("SELECT MIN(ts) AS ts FROM snapshots")["ts"] if "snapshots" in ro.tables else None
    capital = D(start["starting_capital"])
    value = qty * mark[0]
    hold_return = value / capital - 1
    return {
        "qty": qty, "usd": value, "gbp": value / fx["rate"], "price": mark[0], **_at("at", mark[1]),
        **_at("since", first if first is not None else _n(ro.get("started_at"))),
        "change_usd": value - capital, "return_usd": hold_return,
        "vs_hold": equity["return_usd"] - hold_return if equity else None,
        "how": "All the starting capital in BTC at the first Kraken price the run saw, taker fee included, valued at "
               "Kraken's mid price.",
    }


def _chart(ro: ReadOnlyDB, start: dict, valuation: dict) -> dict:
    """The account's value at each run (the heartbeats), and holding BTC's at the same runs (the engine's snapshots)."""
    beats = [(row["ts"], _dec(row["equity"])) for row in
             ro.all("SELECT ts, equity FROM sys_heartbeats WHERE equity IS NOT NULL ORDER BY ts")] \
        if "sys_heartbeats" in ro.tables else []
    beats = [(ts, equity) for ts, equity in beats if equity is not None]
    step = max(1, math.ceil(len(beats) / MAX_CHART_POINTS))
    # Every step-th run, and each UTC day's last (the latest is one), so the page's by-day table has each day's close.
    lasts = {day_start(ts): i for i, (ts, _) in enumerate(beats)}
    kept = [beats[i] for i in sorted(set(range(0, len(beats), step)) | set(lasts.values()))]
    snaps = ro.all("SELECT ts, benchmark FROM snapshots ORDER BY ts") \
        if valuation["benchmark_qty"] and "snapshots" in ro.tables else []
    times = [s["ts"] for s in snaps]
    points = []
    for ts, equity in kept:
        hold = None
        if snaps:
            i = bisect.bisect_left(times, ts)
            k = min((j for j in (i - 1, i) if 0 <= j < len(snaps)), key=lambda j: abs(times[j] - ts))
            if abs(times[k] - ts) <= SNAPSHOT_MATCH_SECONDS:
                hold = _dec(snaps[k]["benchmark"])
        points.append({**_at("at", ts), "equity": equity, "hold": hold})
    priced = any(p["hold"] is not None for p in points)
    return {
        "quote": start.get("quote", "USD"), "start_value": D(start["starting_capital"]), "points": points,
        "runs_valued": len(beats), "every_nth_run": step,
        "equity_source": "The runner's own valuation at each run: cash plus BTC at Kraken's mid price (its heartbeats).",
        "hold_source": ("Holding BTC at each run: the BTC the starting capital bought at the first price the run saw "
                        "(taker fee included), at Kraken's mid price then (the engine's snapshots)." if priced else None),
        "hold_note": None if priced else "No hold-BTC line: this database has no engine snapshots near the runs to "
                                         "price it with.",
    }


def _today(config: Config, ro: ReadOnlyDB, now: float, loop: bool) -> dict:
    day = day_start(now)
    settle = day + _setting(config, "settle_minutes") * 60
    last = ro.one("SELECT day_ts FROM sys_decisions ORDER BY day_ts DESC LIMIT 1") \
        if "sys_decisions" in ro.tables else None
    decided = last is not None and last["day_ts"] >= day - DAY
    halted = ro.get("sys_kill") == "1"  # No decision is made while the kill switch is on, until it's resumed.
    # Overdue from when the heartbeat counts it, so the page and the watchdog agree: for a scheduled run (python -m duo
    # tick), as GitHub's runs are, after the scheduler's grace too; for the loop, from signal_stale_minutes alone. The
    # loop's heartbeat counts it at its own time, which is what the watchdog reads, so it's overdue here only once the
    # loop has checked since: a page built while the loop is held or stopped doesn't call it overdue or say the loop has
    # checked since it was due.
    grace = _setting(config, "signal_stale_minutes")
    if not loop:
        grace = max(grace, _setting(config, "cron_decision_grace_minutes"))
    late_from = day + grace * 60
    newest = ro.one("SELECT MAX(ts) AS ts FROM sys_heartbeats")["ts"] \
        if loop and "sys_heartbeats" in ro.tables else None
    checked = not loop or (newest is not None and newest > late_from)
    overdue = (now - late_from) / 60 if not decided and not halted and now > late_from and checked else None
    return {"bar_date": _date(day - DAY), **_at("bar_at", day - DAY), "decided": decided, "halted": halted,
            **_at("due_from", None if halted else settle + DAY if decided else settle),
            **_at("overdue_from", late_from), "overdue_minutes": overdue}


def _order(config: Config, ro: ReadOnlyDB, row, quote: str, loop: bool) -> dict:
    fills = ro.all("SELECT f.liquidity, f.qty, f.gross, f.fee FROM fills f JOIN sys_children c ON c.venue_id = "
                   "f.venue_id WHERE c.order_id = ? AND f.source = 'systematic' ORDER BY f.id", (row["id"],))
    resting = ro.one("SELECT limit_price, placed_at FROM sys_children WHERE order_id = ? AND status = 'open' "
                     "AND kind = 'maker' ORDER BY id DESC LIMIT 1", (row["id"],))
    makers = ro.one("SELECT COUNT(*) AS n FROM sys_children WHERE order_id = ? AND kind = 'maker' "
                    "AND status != 'blocked'", (row["id"],))["n"]
    qty, gross = D(row["filled_qty"]), D(row["filled_gross"])
    fees = sum((D(f["fee"]) for f in fills), Decimal(0))
    maker = sum(f["liquidity"] == "maker" for f in fills)
    average = gross / qty if qty > 0 else None
    # A scheduled run switches to taker cron_slack_seconds early, at the first run from then, and the loop on time
    # (systematic.py's _work).
    taker_after = _setting(config, "taker_after_minutes") * 60 - (0 if loop else _setting(config, "cron_slack_seconds"))
    working = row["status"] == "working" and row["source"] != "kill_switch"
    taker_from = row["created_at"] + taker_after if working else None
    status, asset, note = row["status"], row["asset"], row["note"]
    done = (f"{qty.normalize():f} {asset} at {_money(average, quote)} on average, "
            f"{_fill_count(maker, len(fills) - maker)}, fees {_money(fees, quote)}" if average is not None else None)
    if status == "filled":
        outcome = f"Filled: {done}."
    elif status == "working" and resting is not None:
        outcome = (f"Working: resting at {_money(D(resting['limit_price']), quote)} since {_clock(resting['placed_at'])}"
                   + (f", and crosses the spread from {_clock(taker_from)} if it hasn't filled" if taker_from else "")
                   + (f". So far {done}." if done else "."))
    elif status == "working":
        outcome = f"Working: {note or 'waiting for the next run'}." + (f" So far {done}." if done else "")
    else:
        outcome = f"{ORDER_STATUS.get(status, status)}" + (f": {note}" if note else "") + \
                  (f". It had filled {done}." if done else ".")
    return {
        "id": row["id"], "decision_id": row["decision_id"], "source": row["source"], "asset": asset,
        "side": row["side"], **_at("created_at", row["created_at"]), **_at("closed_at", row["closed_at"]),
        "status": status, "status_text": ORDER_STATUS.get(status, status),
        "wanted_value": _dec(row["notional_wanted"]), "wanted_qty": _dec(row["qty_wanted"]),
        "target_weight": _dec(row["target_weight"]), "weight_before": _dec(row["weight_before"]),
        "filled_qty": qty, "filled_value": gross, "average_price": average, "fees": fees,
        "maker_fills": maker, "taker_fills": len(fills) - maker, "makers_placed": makers,
        "resting_price": _dec(resting["limit_price"]) if resting else None,
        **_at("resting_since", resting["placed_at"] if resting else None), **_at("taker_from", taker_from),
        "note": note, "outcome": outcome,
    }


def _fill_count(maker: int, taker: int) -> str:
    total = maker + taker
    if total == 1:
        return f"in 1 fill as a {'maker' if maker else 'taker'}"
    if not maker or not taker:
        return f"in {total} fills, all as a {'maker' if maker else 'taker'}"
    return f"in {total} fills, {maker} as a maker and {taker} as a taker"


def _summary(intended: list, asset: str, target, before, band, signal_on, quote: str) -> str:
    """What a decision set out to do, in a sentence."""
    if not intended:
        if not target:
            why = "the signal is off" if signal_on is False else "the target is 0"
            return (f"Nothing to do: {why} and no {asset} is held." if not before else
                    f"Nothing ordered: {why}, but the {asset} held couldn't be sold today (see the decision's notes).")
        return (f"No trade: {asset} was {before:.1%} of the account, within {band * 100:.0f} points of its "
                f"{target:.1%} target, so the research's rule leaves it as it is.")
    parts = []
    for item in intended:
        name, goal, was = item.get("asset", asset), _n(item.get("target_weight")), _n(item.get("weight_before")) or 0.0
        span = f", to take it from {was:.1%} to {goal:.1%} of the account" if goal is not None else ""
        if item.get("side") == "buy":
            parts.append(f"Buy about {_money(D(item['notional']), quote)} of {name}{span}.")
        elif goal == 0:
            parts.append(f"Sell all {D(item['qty']).normalize():f} {name}"
                         + (", as the signal turned off." if signal_on is False else "."))
        else:
            parts.append(f"Sell about {D(item['qty']):.8f} {name}{span}.")
    return " ".join(parts)


def _average(closes: list, params: dict) -> float | None:
    """The moving average an sma signal compares the close with, from the closes the decision logged."""
    length = params.get("L") if params.get("signal") == "sma" else None
    window = closes[-length:] if length else []
    if not length or len(window) < length or any(c is None for c in window):
        return None
    return sum(window) / length  # In order, as research/strategies/common.py's rolling_mean adds them.


def _decisions(config: Config, ro: ReadOnlyDB, start: dict, loop: bool) -> list[dict]:
    params, quote = start.get("params") or {}, start.get("quote", "USD")
    out = []
    for row in ro.all("SELECT * FROM sys_decisions ORDER BY day_ts DESC LIMIT ?", (RECENT_DECISIONS,)):
        targets, signals, bars = json.loads(row["targets"]), json.loads(row["signals"]), json.loads(row["bars"])
        intended, holdings, bands = json.loads(row["intended"]), json.loads(row["holdings"]), json.loads(row["bands"])
        positions = holdings.get("positions") or {}
        asset = BENCHMARK_ASSET if BENCHMARK_ASSET in targets or BENCHMARK_ASSET in positions \
            else next(iter(targets), BENCHMARK_ASSET)
        column = (targets.get(asset) or {}).get("column") or asset
        closes = (bars.get(column) or {}).get("close") or []
        signal = signals.get(column) or {}
        target = _n((targets.get(asset) or {}).get("weight"))
        before = _n((positions.get(asset) or {}).get("weight")) or 0.0
        on = None if _n(signal.get("signal")) is None else _n(signal.get("signal")) > 0
        orders = ro.all("SELECT * FROM sys_orders WHERE decision_id = ? ORDER BY id", (row["id"],))
        out.append({
            "id": row["id"], "asset": asset, "bar_date": _date(row["day_ts"]), **_at("bar_at", row["day_ts"]),
            **_at("decided_at", row["decided_at"]), "close": _n(closes[-1]) if closes else None,
            "close_source": f"Binance's daily {column}/USDT close, as the decision logged it",
            "average": _average(closes, params),
            "average_days": params.get("L") if params.get("signal") == "sma" else None,
            "signal": _n(signal.get("signal")), "signal_on": on,
            "volatility": _n(signal.get("vol")), "vol_target": _n(signal.get("vol_target")),
            "target_weight": target, "weight_before": before, "band": _n(bands.get(asset)),
            "equity": _dec(row["equity"]),
            "summary": _summary(intended, asset, target, before, _n(bands.get(asset)) or 0.0, on, quote),
            "orders": [_order(config, ro, o, quote, loop) for o in orders],
            "untradable": json.loads(row["untradable"] or "[]"), "notes": row["notes"],
            "signal_source": row["signal_source"],
        })
    return out


def _orders(config: Config, ro: ReadOnlyDB, start: dict, loop: bool) -> list[dict]:
    rows = ro.all("SELECT * FROM sys_orders ORDER BY id DESC LIMIT ?", (RECENT_ORDERS,))
    out = []
    for row in rows:
        order = _order(config, ro, row, start.get("quote", "USD"), loop)
        bar = ro.one("SELECT day_ts FROM sys_decisions WHERE id = ?", (row["decision_id"],)) \
            if row["decision_id"] is not None else None
        order["bar_date"] = _date(bar["day_ts"]) if bar else None
        out.append(order)
    return out


def _fills(ro: ReadOnlyDB) -> list[dict]:
    return [{
        "id": f["id"], **_at("at", f["ts"]), "asset": f["asset"], "side": f["side"], "qty": _dec(f["qty"]),
        "price": _dec(f["price"]), "value": _dec(f["gross"]), "fee": _dec(f["fee"]), "liquidity": f["liquidity"],
        "type_text": {"maker": "Maker: rested in the book", "taker": "Taker: crossed the spread"}.get(
            f["liquidity"], f["liquidity"] or "unknown"),
        "reason": f["reason"],
    } for f in ro.all("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (RECENT_FILLS,))]


def _fill_totals(ro: ReadOnlyDB, start: dict) -> dict:
    rows = ro.all("SELECT liquidity, gross, fee FROM fills")
    kinds = {kind: [r for r in rows if r["liquidity"] == kind] for kind in ("maker", "taker")}
    total = lambda items, key: sum((D(r[key]) for r in items), Decimal(0))
    fees = total(rows, "fee")
    return {
        "fills": len(rows), "maker": len(kinds["maker"]), "taker": len(kinds["taker"]),
        "value": total(rows, "gross"), "maker_value": total(kinds["maker"], "gross"),
        "fees": fees, "maker_fees": total(kinds["maker"], "fee"), "taker_fees": total(kinds["taker"], "fee"),
        "fees_share_of_start": fees / D(start["starting_capital"]),
    }


def _runs(ro: ReadOnlyDB, start: dict, now: float, host: dict) -> dict:
    """Each run is one poll and writes one heartbeat, so heartbeats count the runs. Over a window they're counted from
    the same time as its scheduled slots: strictly after the start, whose own run isn't one of them. On Render the
    loop's checks are counted against its own grid instead (_loop_runs)."""
    if host["schedule"] == "loop":
        return _loop_runs(ro, start, now, host)
    started = float(start["started_at"])
    since = lambda span: max(now - span, started)
    count = lambda after: ro.one("SELECT COUNT(*) AS n FROM sys_heartbeats WHERE ts > ? AND ts <= ?",
                                 (after, now))["n"]
    last = ro.one("SELECT ts, status FROM sys_heartbeats ORDER BY ts DESC LIMIT 1")
    first = ro.one("SELECT MIN(ts) AS ts, COUNT(*) AS n FROM sys_heartbeats")
    recent = [r["ts"] for r in ro.all("SELECT ts FROM sys_heartbeats WHERE ts > ? AND ts <= ? ORDER BY ts",
                                      (now - DAY, now))]
    before = ro.one("SELECT ts FROM sys_heartbeats WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (now - DAY,))
    times = ([before["ts"]] if before else []) + recent
    gaps = [b - a for a, b in zip(times, times[1:])]
    slots = schedule_slots()
    return {
        **_at("last_at", last["ts"] if last else None), "last_status": last["status"] if last else None,
        **_at("first_at", first["ts"]), "total": first["n"],
        "last_24h": count(since(DAY)), "scheduled_24h": scheduled_between(since(DAY), now),
        "last_7d": count(since(7 * DAY)), "scheduled_7d": scheduled_between(since(7 * DAY), now),
        "longest_gap_24h_minutes": max(gaps) / 60 if gaps else None,
        "scheduled_longest_gap_minutes": max((b - a for a, b in zip(slots, slots[1:] + [slots[0] + 1440])),
                                             default=None),
        **_at("next_expected_at", next_run(now)),
        "schedule": {"cron": list(SCHEDULE), "slots_utc": [f"{m // 60:02d}:{m % 60:02d}" for m in slots],
                     "per_day": len(slots), "text": NOTES["schedule"],
                     "summary": "Every 15 minutes from 00:05 to 03:50 UTC, hourly at 35 minutes past from 04:35 to "
                                "23:35, and at 23:47."},
    }


def _loop_runs(ro: ReadOnlyDB, start: dict, now: float, host: dict) -> dict:
    """The loop's checks (one heartbeat each) against the grid it keeps: one every loop_minutes from 00:00 UTC +
    settle_minutes, as next_loop_after has it. Both are counted from the move at the earliest, since nothing before it
    was the loop's, and the move's own gap isn't a gap between checks. A start or restart adds one check off the grid,
    straight away, and the page says so."""
    period, offset, moved = host["loop_minutes"] * 60, host["settle_minutes"] * 60, host["moved_at"]
    grid = lambda ts: math.floor((ts - offset) / period)  # The grid points up to ts, counted from the epoch.
    since = lambda span: None if moved is None else max(now - span, float(start["started_at"]), moved)
    count = lambda after: 0 if after is None else ro.one(
        "SELECT COUNT(*) AS n FROM sys_heartbeats WHERE ts >= ? AND ts <= ?", (after, now))["n"]
    due = lambda after: 0 if after is None else max(0, grid(now) - grid(after))
    last = ro.one("SELECT ts, status FROM sys_heartbeats ORDER BY ts DESC LIMIT 1")
    first = ro.one("SELECT MIN(ts) AS ts, COUNT(*) AS n FROM sys_heartbeats")
    times = []
    if moved is not None:
        recent = [r["ts"] for r in ro.all("SELECT ts FROM sys_heartbeats WHERE ts > ? AND ts >= ? AND ts <= ? "
                                          "ORDER BY ts", (now - DAY, moved, now))]
        before = ro.one("SELECT ts FROM sys_heartbeats WHERE ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
                        (now - DAY, moved))
        times = ([before["ts"]] if before else []) + recent
    gaps = [b - a for a, b in zip(times, times[1:])]
    settle = host["settle_minutes"]
    return {
        **_at("last_at", last["ts"] if last else None), "last_status": last["status"] if last else None,
        **_at("first_at", first["ts"]), "total": first["n"],
        "last_24h": count(since(DAY)), "scheduled_24h": due(since(DAY)),
        "last_7d": count(since(7 * DAY)), "scheduled_7d": due(since(7 * DAY)),
        "since_move_24h": moved is not None and since(DAY) == moved,  # The window was cut at the move.
        "since_move_7d": moved is not None and since(7 * DAY) == moved,
        "longest_gap_24h_minutes": max(gaps) / 60 if gaps else None,
        "scheduled_longest_gap_minutes": host["loop_minutes"],
        **_at("next_expected_at", (grid(now) + 1) * period + offset),
        "schedule": {"cron": [], "slots_utc": [], "per_day": 1440 // host["loop_minutes"],
                     "text": NOTES_LOOP["schedule"],
                     "summary": f"Every {host['loop_minutes']} minutes, from {settle // 60:02d}:{settle % 60:02d} UTC"},
    }


def _compare(ro: ReadOnlyDB) -> tuple[dict, str | None]:
    """compare's summary for the page, and its markdown report. A failure is reported, never raised."""
    try:
        from . import compare
        report = compare.compare(ro)
        text = compare.markdown(report)
    except Exception as error:  # The rest of the site is still written, with this on it.
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "summary": {}, "notes": [], "days": []}, None
    keys = ("days", "paper_total", "backtest_total", "backtest_total_research_targets", "mean_difference",
            "tracking_error_daily", "tracking_error_annual", "fills", "maker_fills", "maker_share",
            "maker_share_of_notional", "fee_level", "capital_gbp")
    summary = report.get("summary") or {}
    return {"ok": True, "error": None, "decisions": report.get("decisions"),
            "summary": {k: summary.get(k) for k in keys}, "notes": report.get("notes") or [],
            "days": (report.get("days") or [])[-COMPARE_DAYS:], "orders_compared": len(report.get("orders") or [])}, text


def _decisions_export(ro: ReadOnlyDB) -> tuple[str | None, str | None]:
    """python -m duo decisions' JSONL, line for line, or why it couldn't be made."""
    try:
        lines = [json.dumps(item, sort_keys=True, default=str) for item in export_decisions(ro)]
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"
    return "".join(line + "\n" for line in lines), None


def _attempt(status: dict, name: str, make, *args):
    """make(*args), or None with the error on the page: one part failing never stops the others being built."""
    try:
        return make(*args)
    except Exception as error:
        status["errors"][name] = f"{type(error).__name__}: {error}"
        return None


def _host(config: Config, ro: ReadOnlyDB | None, schedule: str | None) -> dict:
    """Which host runs the run, whose schedule the page judges it by, and where it moved from GitHub to Render.

    It's loop when the build says so (schedule) or the database has a heartbeat after the last tick, and cron
    otherwise. A tick records sys_tick_at before anything else and its heartbeat has that same ts, so the heartbeats up
    to it are GitHub's and those after it the loop's. Render's seed marker records that switch once, at the cutover,
    and comes first: a tick run later would move sys_tick_at. ro is None when there's no database yet.
    """
    data, beats = Path(config.data_dir), ro is not None and "sys_heartbeats" in ro.tables
    seed = _loads(_read(data / SEED_FILE))
    seed = seed if isinstance(seed, dict) else None
    tick_at = _n(seed.get("sys_tick_at")) if seed is not None else _n(ro.get("sys_tick_at")) if ro is not None else None
    if seed is not None:
        last_cron, cron_runs = _n(seed.get("last_cron_at")), seed.get("cron_runs")
    elif beats and tick_at is not None:
        row = ro.one("SELECT MAX(ts) AS ts, COUNT(*) AS n FROM sys_heartbeats WHERE ts <= ?", (tick_at,))
        last_cron, cron_runs = row["ts"], row["n"]
    else:
        last_cron = cron_runs = None
    latest = ro.one("SELECT MAX(ts) AS ts FROM sys_heartbeats")["ts"] if beats else None
    if schedule is None:
        schedule = "loop" if tick_at is not None and latest is not None and latest > tick_at else "cron"
    loop = schedule == "loop"
    # The loop's first check: the first heartbeat after GitHub's last, or the first of all if GitHub never ran it.
    moved = ro.one("SELECT MIN(ts) AS ts FROM sys_heartbeats WHERE ts > ?",
                   (-math.inf if last_cron is None else last_cron,))["ts"] if loop and beats else None
    hold = data / HOLD_FILE
    held = hold.exists() if loop else None
    try:
        held_since = hold.stat().st_mtime if held else None
    except OSError:  # Taken off since: the next build says so.
        held_since = None
    loop_minutes = _setting(config, "loop_minutes")
    return {
        "schedule": schedule, "name": "Render" if loop else "GitHub Actions",
        "loop_minutes": loop_minutes, "settle_minutes": _setting(config, "settle_minutes"),
        "beat_late_minutes": 2 * loop_minutes + 1,  # The runner's own outage rule: two loops and a minute.
        "beat_stale_minutes": _setting(config, "heartbeat_stale_minutes"),  # The watchdog calls the bot down past it.
        **_at("last_cron_at", last_cron), **_at("moved_at", moved),
        "cron_runs": cron_runs if type(cron_runs) is int else None,
        "move_gap_minutes": None if moved is None or last_cron is None else (moved - last_cron) / 60,
        "held": held, **_at("held_since", held_since),
        "seed_sha": seed.get("sha") if loop and seed is not None and isinstance(seed.get("sha"), str) else None,
    }


def gather(config: Config, ro: ReadOnlyDB | None, now: float, schedule: str | None = None) -> dict:
    """Everything status.json holds except the files written beside it. ro is None when there's no database yet, as on
    Render before the cutover, and schedule is the host's when the build knows it (_host)."""
    start = _loads(ro.get("sys_start")) if ro is not None else None
    host = _host(config, ro, schedule)
    loop = host["schedule"] == "loop"
    status = {
        "schema": 1, "title": "Duo paper run", **_at("generated_at", now),
        **_at("tick_at", ro.get("sys_tick_at") if ro is not None else None), "mode": config.mode, "host": host,
        # On Render the page is rebuilt every loop: it's stale once the watchdog would call the bot down, and a loop on.
        "stale_after_minutes": host["beat_stale_minutes"] + host["loop_minutes"] if loop else STALE_MINUTES,
        "target_days": TARGET_DAYS,
        "strategy": None, "start": None, "heartbeat": None, "today": None, "holdings": None, "equity": None,
        "hold_btc": None, "chart": None, "decisions": [], "orders": [], "fills": [], "fill_totals": None,
        "runs": None, "compare": None, "files": None, "notes": NOTES_LOOP if loop else NOTES, "errors": {},
    }
    if not isinstance(start, dict):
        return status  # No paper run yet, and the page says so.

    def section(name: str, make, *args):
        value = _attempt(status, name, make, *args)
        if value is not None:
            status[name] = value
        return value

    section("start", _start, config, start, now)
    section("strategy", _strategy, ro, start, loop)
    section("fill_totals", _fill_totals, ro, start)
    section("fills", _fills, ro)
    fx = _attempt(status, "gbp_usd", _fx, ro, start)
    valuation = _attempt(status, "books", _valuation, ro, start)
    if valuation is not None:
        section("holdings", _holdings, valuation)
        section("chart", _chart, ro, start, valuation)
        if fx is not None:
            equity = section("equity", _equity, start, valuation, fx)
            section("hold_btc", _hold_btc, ro, start, valuation, equity, fx)
    if {"sys_decisions", "sys_orders", "sys_children", "sys_heartbeats"} <= ro.tables:  # Once prepare() has run.
        section("heartbeat", _heartbeat, config, ro)
        section("today", _today, config, ro, now, loop)
        section("decisions", _decisions, config, ro, start, loop)
        section("orders", _orders, config, ro, start, loop)
        section("runs", _runs, ro, start, now, host)
    return status


def build(config: Config, db: DB | ReadOnlyDB | None, out_dir: Path, now: float | None = None,
          schedule: str | None = None) -> dict:
    """Writes the site into out_dir and returns what status.json holds.

    Only db's path is used: the database is opened again, read only, so the run can't be changed from here. db is None
    when there's no database yet, as on Render before the cutover, and the page then says there's no paper run.
    schedule ("cron" or "loop") is the host's, when the caller knows it; otherwise the database says (_host).
    """
    now = time.time() if now is None else now
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ro = ReadOnlyDB(db.path) if db is not None else None
    try:
        if ro is not None:
            ro.conn.execute("BEGIN")  # One read transaction, so every figure is from the same moment even mid-write.
        status = gather(config, ro, now, schedule)
        report = decisions = decisions_error = None
        if ro is not None and isinstance(_loads(ro.get("sys_start")), dict):
            status["compare"], report = _compare(ro)
            decisions, decisions_error = _decisions_export(ro)
            if status["start"]:  # The 28 days are finished days compared, as RUNBOOK section 1 counts them.
                days = status["compare"]["summary"].get("days")
                status["start"].update(days_compared=days, complete=(days or 0) >= TARGET_DAYS)
        else:
            status["compare"] = {"ok": False, "error": None, "summary": {}, "notes": ["No paper run yet."], "days": []}
    finally:
        if ro is not None:
            ro.close()
    _put(out / "compare.md", report)
    _put(out / "decisions.jsonl", decisions)
    status["files"] = {"page": "index.html", "status": "status.json",
                       "compare": "compare.md" if report is not None else None,
                       "decisions": "decisions.jsonl" if decisions is not None else None,
                       "decisions_error": decisions_error}
    shutil.copyfile(PAGE, out / "index.html")
    status = _finite(status)
    _put(out / "status.json", json.dumps(status, indent=1, allow_nan=False) + "\n")
    return status


def problems(status: dict) -> list[str]:
    """The parts build() couldn't make, for the command's output. The page shows them too."""
    found = [f"{name}: {error}" for name, error in (status.get("errors") or {}).items()]
    if (status.get("compare") or {}).get("error"):
        found.append(f"compare: {status['compare']['error']}")
    if (status.get("files") or {}).get("decisions_error"):
        found.append(f"decisions.jsonl: {status['files']['decisions_error']}")
    return found


def main(argv=None) -> None:
    from . import config as config_module
    parser = argparse.ArgumentParser(prog="python -m duo.site",
                                     description="Builds the paper run's status page from its database, read only.")
    parser.add_argument("--out", default="site", help="the folder to write (default: site)")
    parser.add_argument("--schedule", choices=("cron", "loop"),
                        help="the host's schedule, to judge the run by: cron for GitHub Actions' scheduled runs, loop "
                             "for Render's nonstop loop (default: loop if the database has a heartbeat after the last "
                             "tick, else cron)")
    args = parser.parse_args(argv)
    try:
        config = config_module.load()  # config.toml only: the page needs no keys, so .env is never read.
    except ConfigError as error:
        sys.exit(f"Config problem: {error}")
    if not config.systematic_on:
        sys.exit("The systematic runner is off in config.toml ([systematic] enabled = false).")
    if not config.db_path.exists():
        if args.schedule != "loop":
            sys.exit(f"No systematic paper run yet: {config.db_path} doesn't exist.")
        # Render before the cutover: a page that says there's no run on the disk yet, rather than no page at all.
        build(config, None, Path(args.out), schedule=args.schedule)
        print(f"Status page written to {args.out}, saying there's no paper run yet: {config.db_path} doesn't exist.")
        return
    reader = ReadOnlyDB(config.db_path)  # Not DB(): that one can write, and this command never needs to.
    try:
        status = build(config, reader, Path(args.out), schedule=args.schedule)
    finally:
        reader.close()
    print(f"Status page written to {args.out}." + "".join(f"\n- {p}" for p in problems(status)))


if __name__ == "__main__":
    main()
