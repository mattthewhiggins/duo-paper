"""The systematic runner: a research strategy, decided once a day by code and traded on paper, maker first.

Timing. The daily bar closes at 00:00 UTC. A loop runs every loop_minutes, aligned so that one falls at 00:00 +
settle_minutes, for health checks, data freshness and order management. A day's decision is made once, at the first
loop from 00:00 + settle_minutes at which the just-closed bar is in the signal data, using bars up to and including it
and never the one still forming. Kraken quotes are refreshed by the engine every poll, and resting paper orders are
checked against them then, the way a resting order on a real book can fill at any moment.

Trades. An asset trades only where the change to its target passes the research's band, and a target of 0 always
trades, as research/harness/engine.py's backtest_weights decides. Each change becomes a post-only limit at the best bid
(buy) or ask (sell), moved to the current best every reprice_minutes, and after taker_after_minutes a marketable limit
no worse than taker_cap_pct from the quote. Paper fills follow PaperBroker's rules at the configured fees.

Risk. Every new order goes through sys_risk.OrderGate.place. The kill switch, daily loss limit and stale-data halt are
evaluated here every loop, and every control's value, threshold and outcome is logged on every decision.

Records. Each decision is one row of sys_decisions holding everything needed to audit it. Each loop writes a heartbeat
row and the heartbeat file the watchdog reads. Fills go to the ledger's fills table, so the dashboard's books are the
same books.

Scheduled runs. python -m duo tick runs one poll in a fresh process, every 15 to 60 minutes (GitHub Actions), with
scheduled=True. Nothing watches the book between runs, so a resting paper order is judged on Kraken's 1-minute candles
since it was placed, by the same paper rule, and booked at the minute it filled (_fill_on_path). Only a gap longer than
cron_gap_minutes counts as an outage, re-pricing and the switch to taker come cron_slack_seconds early to absorb the
scheduler's jitter, and the decision-overdue alert waits cron_decision_grace_minutes after 00:00 UTC.

Paper only. prepare() refuses live mode, and nothing here holds a key or calls a private endpoint.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

import numpy as np

from . import sys_data, sys_research
from .brokers import PaperBroker
from .config import D, Config
from .db import DB
from .kraken import KrakenError, KrakenNetworkError
from .ledger import Portfolio
from .market import BENCHMARK_ASSET, Quote
from .sys_risk import CONTROLS, FUTURE_TOLERANCE_SECONDS, Book, Check, Child, Controls, Halts, OrderGate, finite
from .sys_research import RUNBOOK, Strategy, StrategyRefused

DAY = 86400
CLOSES_LOGGED = 300
CARRY_BARS = 3
LIVE_REFUSAL = (
    "The systematic runner is paper only, and live mode is refused. Live trading needs exchange-side stops and a "
    f"restart path that holds up (audit findings 34 and 48) and your explicit approval: see {RUNBOOK}."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_ts INTEGER NOT NULL UNIQUE,     -- open time of the just-closed daily bar the decision used
    decided_at REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    research_code_sha256 TEXT NOT NULL,
    duo_code_sha256 TEXT NOT NULL,
    signal_source TEXT NOT NULL,
    bars_sha256 TEXT NOT NULL,
    panel TEXT NOT NULL,                -- JSON: rows, first and last bar, assets
    bars TEXT NOT NULL,                 -- JSON: last 300 opens and closes per relevant asset
    signals TEXT NOT NULL,
    targets TEXT NOT NULL,
    bands TEXT NOT NULL,
    holdings TEXT NOT NULL,             -- before the decision
    equity TEXT,
    cash TEXT,
    intended TEXT NOT NULL,             -- orders the decision meant to place
    risk TEXT NOT NULL,                 -- every control: value, threshold, outcome
    untradable TEXT NOT NULL,           -- sleeves left in cash, and why
    gbp_usd TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS sys_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES sys_decisions(id),
    source TEXT NOT NULL,               -- decision, dca or kill_switch
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    qty_wanted TEXT,                    -- sells
    notional_wanted TEXT,               -- buys
    target_weight TEXT,
    weight_before TEXT,
    target_change TEXT,
    band TEXT,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,               -- working, filled, superseded, blocked, cancelled or dust
    filled_qty TEXT NOT NULL DEFAULT '0',
    filled_gross TEXT NOT NULL DEFAULT '0',
    closed_at REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS sys_children (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sys_orders(id),
    venue_id TEXT,
    kind TEXT NOT NULL,                 -- maker or taker
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    bid TEXT, ask TEXT,                 -- the quote it was priced from
    placed_at REAL NOT NULL,
    closed_at REAL,
    status TEXT NOT NULL,               -- open, filled, cancelled, missed, unfilled or blocked
    fill_price TEXT, fill_fee TEXT,
    fill_bid TEXT, fill_ask TEXT,       -- the quote it filled on, or for a path fill the low and high of its candle
    checks TEXT,                        -- the gate's checks on this order
    note TEXT
);
CREATE TABLE IF NOT EXISTS sys_heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    signal_age_s REAL,
    quote_age_s REAL,
    equity TEXT,
    halts TEXT NOT NULL,
    controls TEXT NOT NULL,
    loop_gap_s REAL
);
CREATE INDEX IF NOT EXISTS idx_sys_orders_status ON sys_orders(status);
CREATE INDEX IF NOT EXISTS idx_sys_children_status ON sys_children(status);
CREATE INDEX IF NOT EXISTS idx_sys_heartbeats_ts ON sys_heartbeats(ts);
"""


def day_start(now: float) -> int:
    return int(now - now % DAY)


def utc(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _round(value: Decimal, decimals: int, rounding) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _fl(value) -> float | None:
    return float(value) if finite(value) else None


# ---- Start: what's fixed for the life of a paper run ----

def refuse_live(config: Config) -> None:
    if config.mode != "paper":
        raise SystemExit(LIVE_REFUSAL)


def prepare(config: Config, db: DB, market, now: float | None = None) -> tuple[Config, Strategy, dict]:
    """Checks the setup and returns (the config the engine runs on, the strategy, the start record).

    The start record is written once, on the first start, and every later start must match it: one paper run holds one
    strategy at one starting capital, so results can't mix (audit finding 49).
    """
    refuse_live(config)
    s = config.systematic
    try:
        strategy = sys_research.resolve(s.strategy)
    except StrategyRefused as error:
        raise SystemExit(f"Not starting the systematic runner. {error}") from None
    bound = sys_research.max_target(strategy)
    if bound > float(s.max_asset_pct) / 100 + 1e-12 and not s.allow_capped_targets:
        raise SystemExit(
            f"Not starting the systematic runner. {strategy.id} can put up to {bound:.0%} of equity in one asset, above "
            f"[systematic] max_asset_pct = {s.max_asset_pct}. Paper would then hold less than the research's strategy "
            f"on those days, and compare would count the cap as tracking error. Raise max_asset_pct to "
            f"{math.ceil(bound * 100)} (the per-asset limit stays the strategy's own largest sleeve + "
            f"{s.asset_margin_pct} points), or set allow_capped_targets = true to run it capped: each decision then "
            f"logs the research's target and the capped one, and compare replays the capped one.")
    db.conn.executescript(SCHEMA)
    model, table = sys_research.load_cost_model(), sys_research.cost_table()
    if strategy.fixed_assets:
        venues = [sys_research.venue(a, model, table) for a in strategy.fixed_assets]
        books = {v.column: v.quote for v in venues if v.tradable}
    else:
        books = {name: entry["book"] for name, entry in table["assets"].items() if entry.get("basis") == "measured"}
    quote = sys_research.book_currency(sorted(set(books.values())))

    existing = db.get("sys_start")
    if existing:
        start = json.loads(existing)
        if start["strategy_id"] != strategy.id or start["quote"] != quote:
            raise SystemExit(
                f"{config.db_path} holds a paper run of {start['strategy_id']} in {start['quote']}, but config.toml asks "
                f"for {strategy.id}. One paper run holds one strategy. Move that file aside to start a new run.")
    else:
        fx_bps = Decimal(str(table["fx_conversion_bps"]["value"]))
        rate = Decimal(1)
        if quote == "USD":
            rate = market.gbp_usd()
        capital = (s.starting_capital_gbp * rate * (1 - fx_bps / 10000)).quantize(Decimal("0.01"))
        start = {
            "strategy_id": strategy.id, "family": strategy.family, "params": strategy.params,
            "notes": list(strategy.notes), "quote": quote, "books": books,
            "starting_capital_gbp": str(s.starting_capital_gbp), "gbp_usd": str(rate),
            "funding_fx_bps": str(fx_bps if quote == "USD" else 0),
            "starting_capital": str(capital), "maker_pct": str(s.maker_pct), "taker_pct": str(s.taker_pct),
            "started_at": time.time() if now is None else now,
            "research_code_sha256": sys_research.code_sha256(), "duo_code_sha256": sys_research.duo_code_sha256(),
            "cost_model": str(sys_research.COST_MODEL_PATH.relative_to(sys_research.PROJECT)),
            "max_target": bound, "max_asset_pct": str(s.max_asset_pct),
            "targets_capped": bound > float(s.max_asset_pct) / 100 + 1e-12,
        }
        if quote != "USD":
            start["funding_fx_bps"] = "0"
            start["starting_capital"] = str(s.starting_capital_gbp)
        db.set("sys_start", _dumps(start))
        db.event("info", "systematic",
                 f"Paper run of {strategy.id} started: GBP {s.starting_capital_gbp} as {start['starting_capital']} "
                 f"{quote} at GBP/USD {rate}, fees {s.maker_pct}% maker and {s.taker_pct}% taker.")
    effective = replace(
        config, quote=quote, starting_capital=D(start["starting_capital"]),
        maker_pct=D(start["maker_pct"]), taker_pct=D(start["taker_pct"]),
        assets=tuple(strategy.fixed_assets) or (BENCHMARK_ASSET,),
    )
    return effective, strategy, start


class SystematicRunner:
    def __init__(self, config: Config, db: DB, market, broker, binance, strategy: Strategy, start: dict,
                 cost_model=None, cost_table=None, scheduled: bool = False):
        refuse_live(config)
        if not isinstance(broker, PaperBroker):
            raise SystemExit(LIVE_REFUSAL)
        self.config = config
        self.s = config.systematic
        self.db = db
        self.market = market
        self.binance = binance
        self.strategy = strategy
        self.start = start
        self.scheduled = scheduled  # One poll per process on a schedule (python -m duo tick), not the engine's loop.
        self.capital = D(start["starting_capital"])
        db.conn.executescript(SCHEMA)
        self.cost_model = cost_model or sys_research.load_cost_model()
        self.cost_table = cost_table or sys_research.cost_table()
        self.controls = Controls(self.s)
        self.gate = OrderGate(broker, self.controls, self.s.reprice_minutes * 60 * 4)
        self.heartbeat_path = Path(config.data_dir) / "heartbeat_systematic.json"
        self.next_loop = 0.0
        self.last_error: str | None = None
        self._venues: dict[str, sys_research.Venue] = {}
        # A bar seen before a restart stays seen: every decided bar, and the last one check_signal found. Without
        # this, a Binance outage that outlasts a restart would block the orders of a decision already made.
        self._bar_seen: set[int] = {row["day_ts"] for row in db.all("SELECT day_ts FROM sys_decisions")}
        if finite(db.get("sys_last_bar_seen")):
            self._bar_seen.add(int(float(db.get("sys_last_bar_seen"))))
        if strategy.fixed_assets:
            self.sentinels = {a: f"{a}USDT" for a in strategy.fixed_assets}
        else:
            self.sentinels = {"BTC": "BTCUSDT"}
        if hasattr(market, "restore_frozen_state"):
            market.restore_frozen_state(json.loads(db.get("sys_frozen_quotes", "{}") or "{}"))
        # A paper taker fills the moment it's placed, and its row is written in the same transaction as the fill. An
        # open taker row can only be from a version that wrote it first and died before booking: it never filled.
        db.execute("UPDATE sys_children SET status = 'unfilled', closed_at = placed_at, note = 'found open at start: "
                   "a taker is booked with its fill, so this one never filled' WHERE status = 'open' AND kind = 'taker'")

    # ---- The books ----

    def portfolio(self) -> Portfolio:
        return Portfolio.replay(self.capital, self.db.all("SELECT * FROM fills ORDER BY id"))

    def venue(self, column: str) -> sys_research.Venue:
        if column not in self._venues:
            self._venues[column] = sys_research.venue(column, self.cost_model, self.cost_table)
        return self._venues[column]

    def open_children(self) -> list:
        return self.db.all("SELECT * FROM sys_children WHERE status = 'open' ORDER BY id")

    def working_orders(self) -> list:
        return self.db.all("SELECT * FROM sys_orders WHERE status = 'working' ORDER BY id")

    def halts(self, now: float) -> Halts:
        errors = self._api_errors()
        day = day_start(now)
        daily = self.db.get("sys_daily_loss_day") == str(day)
        if self.db.get("sys_daily_loss_unknown") == str(day):
            daily = None
        stale = self._signal_stale(now)
        drawdown, day_loss = self.db.get("sys_last_drawdown_pct"), self.db.get("sys_last_day_loss_pct")
        return Halts(kill=self.db.get("sys_kill") == "1", kill_reason=self.db.get("sys_kill_reason", ""),
                     daily_loss=daily, signal_stale=stale, api_errors=max(errors.values(), default=0),
                     drawdown_pct=float(drawdown) if finite(drawdown) else None,
                     day_loss_pct=float(day_loss) if finite(day_loss) else None)

    def book(self, now: float, halts: Halts | None = None) -> Book:
        portfolio = self.portfolio()
        quotes = dict(self.market.quotes)
        # A book that isn't a book (bid not above 0, ask below bid) prices nothing: the gate already refuses to trade on
        # one, and valuing the holdings at its mid could lift the drawdown peak for good (or hide a loss).
        marks = {a: q.mid for a, q in quotes.items() if sane_quote(q)}
        held = {a: p.qty for a, p in portfolio.positions.items() if p.qty > 0}
        equity = portfolio.cash
        for asset, qty in held.items():
            if asset not in marks or not finite(marks[asset]):
                equity = None
                break
            equity += qty * marks[asset]
        open_buy_qty, open_sell_qty, open_buy_cash = {}, {}, Decimal(0)
        maker_rate = self.config.maker_pct / 100
        for child in self.open_children():
            qty = D(child["qty"])
            if child["side"] == "buy":
                open_buy_qty[child["asset"]] = open_buy_qty.get(child["asset"], Decimal(0)) + qty
                open_buy_cash += qty * D(child["limit_price"]) * (1 + maker_rate)
            else:
                open_sell_qty[child["asset"]] = open_sell_qty.get(child["asset"], Decimal(0)) + qty
        limits = json.loads(self.db.get("sys_asset_limits", "{}"))
        frozen = {a: self.market.frozen_seconds(a, now) for a in quotes}
        return Book(now=now, equity=equity, cash=portfolio.cash, held=held, marks=marks, open_buy_qty=open_buy_qty,
                    open_buy_cash=open_buy_cash, open_sell_qty=open_sell_qty,
                    asset_limit={a: D(str(v)) for a, v in limits.items()}, quotes=quotes, frozen=frozen,
                    halts=halts if halts is not None else self.halts(now))

    def marks_usable(self, book: Book) -> tuple[bool, str]:
        """Whether every holding is priced by a quote the gate would trade on: a sane book, fresh and not frozen.

        Only such marks may move the drawdown peak, trip the kill switch or set the day's start. Otherwise those read
        equity as unknown: the peak stays, nothing trips on it, and buys stay blocked (daily loss unknown).
        """
        bad = []
        for asset in sorted(book.held):
            quote = book.quotes.get(asset)
            frozen = book.frozen.get(asset)
            if quote is None or asset not in book.marks:
                bad.append(f"{asset}: no sane quote")
            elif not finite(quote.ts) or not (-FUTURE_TOLERANCE_SECONDS <= book.now - float(quote.ts)
                                                <= self.s.max_quote_age_seconds):
                bad.append(f"{asset}: quote {book.now - float(quote.ts):.0f}s old")
            elif not finite(frozen) or frozen > self.s.max_frozen_quote_seconds:
                bad.append(f"{asset}: quote frozen or of unknown age")
        return not bad, "; ".join(bad)

    # ---- API errors: consecutive failures per source, cleared by a success ----

    def _api_errors(self) -> dict:
        return json.loads(self.db.get("sys_api_errors", '{"binance": 0, "kraken": 0}'))

    def api_result(self, source: str, ok: bool, error: str | None = None) -> None:
        errors = self._api_errors()
        errors[source] = 0 if ok else errors.get(source, 0) + 1
        self.db.set("sys_api_errors", _dumps(errors))
        if not ok:
            self.last_error = f"{source}: {error}"
            if errors[source] == self.s.max_api_errors:
                self.db.event("warn", "systematic", f"{errors[source]} {source} errors in a row, so no new orders "
                                                    f"until it answers again. Last: {error}")

    # ---- Signal freshness ----

    def _signal_stale(self, now: float) -> bool | None:
        day = day_start(now)
        if (day - DAY) in self._bar_seen:
            return False
        if now < day + self.s.signal_stale_minutes * 60:
            return False
        return True

    def check_signal(self, now: float) -> None:
        """Looks for the just-closed bar on the sentinel assets until it's seen. Cheap, and only until then."""
        bar_ts = day_start(now) - DAY
        if bar_ts in self._bar_seen:
            return
        try:
            present = sys_data.has_bar(self.binance, self.sentinels, bar_ts, now)
        except sys_data.DataError as error:
            self.api_result("binance", False, str(error))
            return
        self.api_result("binance", True)
        if present:
            self._bar_seen.add(bar_ts)
            self.db.set("sys_last_bar_seen", bar_ts)
            if self.db.get("sys_signal_stale_day") == str(bar_ts):
                self.db.clear("sys_signal_stale_day")
                self.db.event("info", "systematic", "The just-closed daily bar has arrived, so orders can resume.")
        elif self._signal_stale(now) and self.db.get("sys_signal_stale_day") != str(bar_ts):
            self.db.set("sys_signal_stale_day", bar_ts)
            self.db.event("warn", "systematic", f"The daily bar that closed at {utc(bar_ts + DAY)} still isn't in "
                                                f"Binance's data, so no new orders until it is.")

    # ---- The engine calls this on every poll ----

    def on_tick(self, now: float, error: str | None = None) -> None:
        # Before any fill check: after a gap, a resting order nobody managed closes unfilled, never fills at its old
        # price on the first quote after the gap (audit finding 55). A scheduled run judges it on the price path first.
        self._close_after_gap(now)
        if error is not None:
            self.api_result("kraken", False, error)
        else:
            self.api_result("kraken", True)
            if hasattr(self.market, "frozen_state"):
                self.db.set("sys_frozen_quotes", _dumps(self.market.frozen_state()))
            self.check_fills(now)
        if now >= self.next_loop:
            try:
                self.loop(now)
            finally:
                self.next_loop = self.next_loop_after(now)

    def next_loop_after(self, now: float) -> float:
        offset, period = self.s.settle_minutes * 60, self.s.loop_minutes * 60
        return (math.floor((now - offset) / period) + 1) * period + offset

    def _gap_after(self, now: float):
        """The last heartbeat, if it's old enough that no loop ran in between: an outage. In the loop that's two loops
        and a minute. Scheduled runs are 15 to 60 minutes apart and start late at busy times, so for them it's
        cron_gap_minutes."""
        limit = self.s.cron_gap_minutes * 60 if self.scheduled else 2 * self.s.loop_minutes * 60 + 60
        last = self.db.one("SELECT ts FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        if last is not None and now - last["ts"] > limit:
            return last
        return None

    def _close_after_gap(self, now: float) -> None:
        if self._gap_after(now) is not None:
            self._expire_stale_children(now, "the bot wasn't running to manage it")

    def loop(self, now: float) -> None:
        last = self._gap_after(now)
        gap = None
        if last is not None:
            gap = now - last["ts"]
            self.db.event("warn", "systematic", f"No loop ran for {gap / 60:.0f} minutes, from {utc(last['ts'])} to "
                                                f"{utc(now)}. Nothing was checked or traded in that gap.")
            self._expire_stale_children(now, "the bot wasn't running to manage it")
        self._track()
        try:
            self.check_signal(now)
        except Exception as error:  # Anything unexpected from the data source counts as an API error, and the loop
            self.api_result("binance", False, f"{type(error).__name__}: {error}")  # still manages orders and beats.
        halts = self.halts(now)
        book = self.book(now, halts)
        controls = self._portfolio_controls(now, book)
        if not self.halts(now).kill:
            try:
                self.decide(now)
            except Exception as error:  # A failed decision mustn't stop order management or the heartbeat.
                self.last_error = f"decision: {error}"
                self.db.event("critical", "systematic", f"The daily decision failed and will be retried at the next "
                                                        f"loop:\n{traceback.format_exc(limit=4)}")
        self.manage(now)
        self.heartbeat(now, gap, controls)

    def _track(self) -> None:
        assets = set(self.book_assets())
        bases = {}
        for asset in assets:
            entry = self.cost_table["assets"].get(asset)
            if entry:
                bases[asset] = entry["kraken_base"]
        self.market.track(assets, bases)

    def book_assets(self) -> set[str]:
        held = {a for a, p in self.portfolio().positions.items() if p.qty > 0}
        working = {row["asset"] for row in self.working_orders()}
        targeted = set(json.loads(self.db.get("sys_targeted", "[]")))
        return held | working | targeted | set(self.strategy.fixed_assets)

    # ---- Kill switch and daily loss ----

    def _portfolio_controls(self, now: float, book: Book) -> list[Check]:
        # Equity read only from marks the gate would trade on. Otherwise it's unknown here: the peak stays where it is,
        # nothing trips on it, and the daily loss reads unknown, which blocks buys.
        usable, why = self.marks_usable(book)
        equity = book.equity if usable else None
        peak = D(self.db.get("sys_peak_equity", "0"))
        if finite(equity) and equity > peak:
            peak = equity
            self.db.set("sys_peak_equity", peak)
        drawdown = self.controls.drawdown(equity, peak if peak > 0 else None)
        if not usable:
            drawdown = replace(drawdown, detail=f"holdings not priced by usable quotes: {why}")
        # The drawdown limit sells whenever it fails and nothing has been flattened since the last resume, even while
        # trading is paused from the dashboard: a pause must never switch off the kill switch.
        if drawdown.value is not None and not drawdown.passed and self.db.get("sys_kill_flattened") != "1":
            paused = self.db.get("sys_kill_reason") if self.db.get("sys_kill") == "1" else None
            self.trip_kill(now, f"equity {float(equity):.2f} is {drawdown.value:.1f}% below its peak of "
                                f"{float(peak):.2f}, past the {self.s.kill_drawdown_pct}% limit"
                                + (f" (trading was already paused: {paused})" if paused else ""))
        self.db.set("sys_last_drawdown_pct", "" if drawdown.value is None else drawdown.value)

        day = day_start(now)
        start = json.loads(self.db.get("sys_day_start", "null") or "null")
        if (start is None or start["day"] != day) and finite(equity):
            start = self._day_start(now, day, book, equity)
            if start is not None:
                self.db.set("sys_day_start", _dumps(start))
        start_equity = start["equity"] if start is not None and start["day"] == day else None
        daily = self.controls.day_loss(equity, start_equity)
        self.db.set("sys_last_day_loss_pct", "" if daily.value is None else daily.value)
        if daily.value is None:
            self.db.set("sys_daily_loss_unknown", day)  # Fails closed: no buys while the loss can't be measured.
        else:
            self.db.clear("sys_daily_loss_unknown")
            if not daily.passed and self.db.get("sys_daily_loss_day") != str(day):
                self.db.set("sys_daily_loss_day", day)
                self.db.event("warn", "systematic", f"Down {daily.value:.1f}% today, past the {self.s.daily_loss_pct}% "
                                                    f"limit: no buys until 00:00 UTC. Sells still run.")
                self._stop_buys(now, "daily loss limit")
        halts = self.halts(now)
        return [drawdown, daily, self.controls.staleness(halts, self._bar_missing_minutes(now)),
                *self.controls.holdings(replace(book, halts=halts))]

    def _day_start(self, now: float, day: int, book: Book, equity) -> dict | None:
        """Equity at 00:00 UTC, for the daily loss limit (audit finding 44).

        The last loop before midnight, if one ran in the 15 minutes before it. Otherwise, if nothing is held, the cash,
        which nothing has moved since (no loop ran). Otherwise the holdings at Binance's close of the bar that ended at
        midnight, plus the cash: a loss between midnight and a late first loop then still counts. If those closes can't
        be had, None: the day's start is unknown, which blocks buys, and the next loop tries again.
        """
        last = self.db.one("SELECT ts, equity FROM sys_heartbeats WHERE ts < ? AND ts >= ? AND equity IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1", (day, day - 900))
        if last is not None:
            return {"day": day, "equity": last["equity"], "how": f"last loop before 00:00 UTC, at {utc(last['ts'])}"}
        late = (now - day) / 60
        if not book.held:
            return {"day": day, "equity": str(equity),
                    "how": f"first loop of the day, {late:.0f} minutes after 00:00 UTC, with nothing held: the cash"}
        if self.config.quote != "USD":
            return None  # Binance's closes are in USDT. A GBP book would need a rate this doesn't have.
        value, closes = book.cash, {}
        try:
            for asset, qty in sorted(book.held.items()):
                symbol = f"{self._column_of(asset).split('~', 1)[0]}USDT"
                rows = sys_data.parse_klines(self.binance.klines(symbol, 3), now)
                row = next((r for r in rows if r[0] == day - DAY), None)
                if row is None:
                    return None
                closes[asset] = row[4]
                value += qty * D(repr(row[4]))
        except sys_data.DataError as error:
            self.api_result("binance", False, str(error))
            return None
        return {"day": day, "equity": str(value), "closes": closes,
                "how": f"no loop ran in the 15 minutes before 00:00 UTC, so the holdings are valued at Binance's "
                       f"closes of the bar that ended then (first loop {late:.0f} minutes after 00:00 UTC)"}

    def _column_of(self, book_asset: str) -> str:
        """The research column a Kraken book was last traded as, for its Binance symbol."""
        last = self.db.one("SELECT targets FROM sys_decisions ORDER BY id DESC LIMIT 1")
        if last is not None:
            column = json.loads(last["targets"]).get(book_asset, {}).get("column")
            if column:
                return column
        return book_asset

    def _stop_buys(self, now: float, why: str) -> None:
        """Cancels resting buys and blocks buy orders for the day (audit finding 45)."""
        for child in self.open_children():
            if child["side"] == "buy":
                self._cancel_child(child, now, f"cancelled: {why}")
        self.db.execute("UPDATE sys_orders SET status = 'blocked', closed_at = ?, note = ? "
                        "WHERE status = 'working' AND side = 'buy'", (now, why))

    def trip_kill(self, now: float, reason: str) -> None:
        self.stop_trading(now, reason, flatten=True, source="drawdown")

    def stop_trading(self, now: float, reason: str, flatten: bool, source: str = "manual") -> None:
        """The kill switch, or the dashboard's Stop trading (flatten=False) and Sell everything (flatten=True).

        Every order is cancelled and nothing new is placed until you resume, except the sells that flatten. A pause
        without a flatten leaves the drawdown limit armed: if it fails later, it still sells everything.
        """
        self.db.set("sys_kill", "1")
        self.db.set("sys_kill_reason", reason)
        self.db.set("sys_kill_source", source)
        if flatten:
            self.db.set("sys_kill_flattened", "1")
        self.db.event("critical", "kill_switch",
                      f"Systematic trading halted: {reason}. Every order cancelled"
                      + (", and everything is being sold at market, capped." if flatten else ".")
                      + " It stays halted until you resume it.")
        for child in self.open_children():
            self._cancel_child(child, now, "cancelled: kill switch")
        self.db.execute("UPDATE sys_orders SET status = 'cancelled', closed_at = ?, note = ? "
                        "WHERE status = 'working'", (now, f"halted: {reason}"))
        if not flatten:
            return
        book = self.book(now)
        # Linked to the latest decision, so the decisions export carries the flatten and its fills.
        latest = self.db.one("SELECT id FROM sys_decisions ORDER BY id DESC LIMIT 1")
        for asset, qty in book.held.items():
            mark = book.marks.get(asset)
            weight = (qty * mark / book.equity if finite(book.equity) and book.equity > 0 and finite(mark) else None)
            # The weight is for the record only. The sells are capped by quantity (what's held), so a holding with no
            # price right now still sells as soon as it has one.
            self.db.execute(
                "INSERT INTO sys_orders(decision_id, source, asset, side, qty_wanted, target_weight, weight_before, "
                "target_change, created_at, status, note) VALUES(?, 'kill_switch', ?, 'sell', ?, '0', ?, ?, ?, "
                "'working', 'flatten at market, capped')",
                (latest["id"] if latest else None, asset, str(qty), None if weight is None else str(weight),
                 None if weight is None else str(weight), now))

    def resume(self, now: float | None = None) -> bool:
        """Clears the kill switch. The peak resets to current equity, or the old peak would trip it again at once."""
        now = time.time() if now is None else now
        if self.db.get("sys_kill") != "1":
            return False
        book = self.book(now)
        self.db.set("sys_kill", "0")
        for key in ("sys_kill_reason", "sys_kill_source", "sys_kill_flattened"):
            self.db.clear(key)
        if finite(book.equity) and self.marks_usable(book)[0]:
            self.db.set("sys_peak_equity", book.equity)
        else:
            self.db.clear("sys_peak_equity")
        self.db.execute("UPDATE sys_orders SET status = 'cancelled', closed_at = ?, note = 'resumed before flat' "
                        "WHERE status = 'working' AND source = 'kill_switch'", (now,))
        self.db.event("info", "kill_switch", "Systematic trading resumed by you. The drawdown peak was reset to "
                                             "current equity. The next daily decision trades as usual.")
        return True

    # ---- The daily decision ----

    def next_decision_at(self, now: float) -> float:
        day = day_start(now)
        settle = day + self.s.settle_minutes * 60
        decided = self.db.one("SELECT 1 FROM sys_decisions WHERE day_ts = ?", (day - DAY,))
        if decided or self.db.get("sys_kill") == "1":
            return settle + DAY
        return max(settle, self.next_loop_after(now) if now >= settle else settle)

    def decide(self, now: float) -> int | None:
        day = day_start(now)
        bar_ts = day - DAY
        if now < day + self.s.settle_minutes * 60:
            return None
        if self.db.one("SELECT 1 FROM sys_decisions WHERE day_ts = ?", (bar_ts,)):
            return None
        if bar_ts not in self._bar_seen:
            return None
        try:
            panel, symbols = self._panel(now, bar_ts)
        except sys_data.DataError as error:
            self.api_result("binance", False, str(error))
            return None
        self.api_result("binance", True)
        if panel is None:
            return None
        if panel.ts[-1] != bar_ts or (panel.ts >= day).any():
            raise RuntimeError(f"the panel ends at {utc(panel.ts[-1])}, not on the just-closed bar {utc(bar_ts)}")
        plan = self._plan(panel, now, day)
        self.db.set("sys_targeted", _dumps(sorted(plan["targeted"])))
        self._track()
        try:
            self.market.refresh()
            self.api_result("kraken", True)
        except (KrakenError, KrakenNetworkError, ValueError) as error:
            self.api_result("kraken", False, str(error))
            self.db.set("sys_waiting", f"Kraken prices for the decision: {error}")
            return None
        halts = self.halts(now)
        book = self.book(now, halts)
        needed = {a for a in plan["targets"]} | set(book.held)
        missing = [a for a in sorted(needed) if a not in book.quotes and a not in self.market.unavailable]
        if not finite(book.equity) or missing:
            self.db.set("sys_waiting", f"waiting for Kraken prices for {', '.join(missing) or 'the holdings'}")
            return None
        self.db.clear("sys_waiting")
        return self._record_decision(now, bar_ts, panel, symbols, plan, book)

    def _panel(self, now: float, bar_ts: int):
        if self.strategy.fixed_assets:
            symbols = dict(self.sentinels)
        else:
            symbols = self.binance.usdt_symbols()
            for asset in self.sentinels:
                symbols.setdefault(asset, f"{asset}USDT")
        served: set[str] = set()  # The Binance host or hosts that answered, which the decision records.
        bars = sys_data.fetch_bars(self.binance, symbols, self.s.history_days, now, pause=self._pause(), served=served)
        if any(not any(row[0] == bar_ts for row in bars.get(a, [])) for a in self.sentinels):
            return None, symbols
        return sys_data.build_panel(bars, bar_ts, self.s.history_days, source=sys_data.source_for(served)), symbols

    def _pause(self) -> float:
        return 0.05 if isinstance(self.binance, sys_data.BinancePublic) else 0.0

    def _plan(self, panel, now: float, day: int) -> dict:
        """Targets per Kraken book from the research's own functions, with the sleeves that can't trade left out."""
        strategy = self.strategy
        untradable, targets, bands, column_of = [], {}, {}, {}
        signals, mask_row, sleeve = {}, None, 0.0
        if strategy.family == "DCA":
            first = self.db.get("sys_dca_start")
            if first is None:
                first = str(day)
                self.db.set("sys_dca_start", first)
            # Every tranche research's dca() has bought by today and paper hasn't yet: normally the one due today, but
            # also any whose day passed with no decision (the Mac asleep, a feed down), bought late rather than never.
            start_day = int(first)
            due_total = sys_research.dca_due_count(strategy, start_day, day)
            bought = self._dca_tranches_bought()
            owed = max(0, due_total - bought)
            tranche = self.capital / strategy.params["weeks"] / len(strategy.fixed_assets)
            for asset in strategy.fixed_assets:
                v = self.venue(asset)
                if not v.tradable:
                    untradable.append({"column": asset, "book": v.book, "reason": v.reason,
                                       "tranche": str(tranche * owed)})
                    continue
                if owed:
                    targets[v.book] = {"tranche": tranche * owed}
                column_of[v.book] = asset
            late = [utc(start_day + (n - 1) * 7 * DAY) for n in range(bought + 1, due_total + 1)
                    if start_day + (n - 1) * 7 * DAY < day]
            signals = {"dca_start_day": utc(start_day), "purchase_due": owed > 0,
                       "purchase_number": due_total if owed else None, "tranches": owed,
                       "tranches_bought_before": bought, "late": late,
                       "of": strategy.params["weeks"], "tranche_each": str(tranche)}
            sleeve = sys_research.largest_sleeve(strategy, np.zeros(0), None)
            relevant = [panel.assets.index(a) for a in strategy.fixed_assets if a in panel.assets]
        else:
            target, band = sys_research.targets(strategy, panel)
            row_t, row_b = target[-1], band[-1]
            mask_row = sys_research.universe_mask_last_row(strategy, panel)
            sleeve = sys_research.largest_sleeve(strategy, row_t, mask_row)
            relevant = set(np.flatnonzero(row_t > 0).tolist())
            if mask_row is not None:
                relevant |= set(np.flatnonzero(mask_row).tolist())
            if strategy.fixed_assets:
                relevant |= {panel.assets.index(a) for a in strategy.fixed_assets if a in panel.assets}
            held = {a for a, p in self.portfolio().positions.items() if p.qty > 0}
            for c, column in enumerate(panel.assets):
                v = self.venue(column)
                if v.book in held:
                    relevant.add(c)
            relevant = sorted(relevant)
            # Two columns on one Kraken book means one of them isn't the coin that book trades. Neither is traded.
            claims: dict[str, list[int]] = {}
            for c in relevant:
                v = self.venue(panel.assets[c])
                if v.tradable:
                    claims.setdefault(v.book, []).append(c)
            shared = {b: cs for b, cs in claims.items() if len(cs) > 1 and any(row_t[c] > 0 for c in cs)}
            for c in relevant:
                column, weight = panel.assets[c], float(row_t[c])
                v = self.venue(column)
                if not v.tradable or v.book in shared:
                    if weight > 0:
                        reason = v.reason if not v.tradable else (
                            f"{' and '.join(panel.assets[k] for k in shared[v.book])} would all trade on Kraken's "
                            f"{v.book} book, so which coin that book is can't be told")
                        untradable.append({"column": column, "book": v.book, "target": weight, "reason": reason,
                                           "note": "this sleeve stays in cash"})
                    continue
                if v.book in targets and weight <= 0:
                    continue
                targets[v.book] = {"weight": weight}
                bands[v.book] = float(row_b[c])
                column_of[v.book] = column
            signals = sys_research.signals(strategy, panel, relevant)
        limit = min(Decimal(str(sleeve)) + self.s.asset_margin_pct / 100, self.s.max_asset_pct / 100)
        return {"targets": targets, "bands": bands, "untradable": untradable, "column_of": column_of,
                "signals": signals, "sleeve": sleeve, "asset_limit": limit, "relevant": relevant,
                "mask": [panel.assets[c] for c in np.flatnonzero(mask_row).tolist()] if mask_row is not None else None,
                "targeted": {b for b, t in targets.items() if t.get("weight", 0) > 0 or "tranche" in t}}

    def _dca_tranches_bought(self) -> int:
        bought = 0
        for row in self.db.all("SELECT signals FROM sys_decisions"):
            signals = json.loads(row["signals"])
            bought += int(signals.get("tranches", 1 if signals.get("purchase_due") else 0) or 0)
        return bought

    def _record_decision(self, now: float, bar_ts: int, panel, symbols: dict, plan: dict, book: Book) -> int:
        equity, limit = book.equity, plan["asset_limit"]
        held_books = set(book.held)
        limits = {b: str(limit) for b in set(plan["targets"]) | held_books}
        self.db.set("sys_asset_limits", _dumps(limits))
        book = replace(book, asset_limit={b: limit for b in limits})
        holdings = {"cash": str(book.cash), "equity": str(equity), "positions": {}}
        weights_now = {}
        for asset, qty in book.held.items():
            mark = book.marks[asset]
            weights_now[asset] = qty * mark / equity
            holdings["positions"][asset] = {"qty": str(qty), "mark": str(mark), "value": str(qty * mark),
                                            "weight": float(weights_now[asset])}

        intended, notes, capped = [], [], {}
        if self.strategy.family == "DCA":
            room = book.cash
            for asset, t in plan["targets"].items():
                notional = min(t["tranche"], max(room, Decimal(0)))
                if asset in self.market.unavailable:
                    plan["untradable"].append({"column": plan["column_of"].get(asset, asset), "book": asset,
                                               "reason": self.market.unavailable[asset], "tranche": str(t["tranche"])})
                    continue
                room -= notional
                if notional > 0:
                    intended.append({"asset": asset, "side": "buy", "notional": notional, "qty": None,
                                     "target_weight": None, "weight_before": weights_now.get(asset, Decimal(0)),
                                     "target_change": notional / equity, "band": None, "source": "dca"})
        else:
            wanted = {}
            for asset in sorted(set(plan["targets"]) | held_books):
                if asset in self.market.unavailable:
                    plan["untradable"].append({"column": plan["column_of"].get(asset, asset), "book": asset,
                                               "reason": self.market.unavailable[asset],
                                               "target": plan["targets"].get(asset, {}).get("weight", 0.0),
                                               "note": "no Kraken pair today, so this sleeve stays as it is"})
                    continue
                target = Decimal(str(plan["targets"].get(asset, {}).get("weight", 0.0)))
                if target > limit:
                    notes.append(f"{asset}'s target {float(target):.4f} is capped at the per-asset limit "
                                 f"{float(limit):.4f}")
                    target = limit
                    capped[asset] = limit
                wanted[asset] = target
            gross = sum(wanted.values(), Decimal(0))
            cap = self.s.max_gross_pct / 100
            if gross > cap:  # The research engine does the same: only the buys give way.
                rise = sum((t - weights_now.get(a, Decimal(0)) for a, t in wanted.items()
                            if t > weights_now.get(a, Decimal(0))), Decimal(0))
                scale = max(Decimal(0), 1 - (gross - cap) / rise) if rise > 0 else Decimal(0)
                for a, t in list(wanted.items()):
                    h = weights_now.get(a, Decimal(0))
                    if t > h:
                        wanted[a] = h + (t - h) * scale
                notes.append(f"targets summed to {float(gross):.4f}, so buys were scaled to keep gross at {float(cap)}")
            for asset, target in wanted.items():
                h = weights_now.get(asset, Decimal(0))
                band = Decimal(str(plan["bands"].get(asset, 0.0)))
                if not band_trades(float(target), float(h), float(band)):
                    continue
                side = "buy" if target > h else "sell"
                change = abs(target - h)
                qty = book.held.get(asset, Decimal(0)) if target == 0 else None
                if side == "sell" and qty is None:
                    qty = change * equity / book.marks[asset]
                intended.append({"asset": asset, "side": side, "notional": change * equity if side == "buy" else None,
                                 "qty": qty, "target_weight": target, "weight_before": h, "target_change": change,
                                 "band": band, "source": "decision"})

        # Each intended order's checks as things stand, logged whatever happens next.
        order_checks = []
        for item in intended:
            quote = book.quotes.get(item["asset"])
            price = (quote.bid if item["side"] == "buy" else quote.ask) if quote else None
            qty = item["qty"] if item["qty"] is not None else (item["notional"] / price if finite(price) and price else None)
            child = Child(0, item["asset"], item["side"], qty if finite(qty) else Decimal("NaN"),
                          price if finite(price) else Decimal("NaN"), "maker", item["source"], item["target_change"],
                          qty_cap=item["qty"] if item["side"] == "sell" else None)
            order_checks.append({"asset": item["asset"], "side": item["side"],
                                 "checks": [c.as_dict() for c in self.controls.check_order(child, book)]})
        priced = sorted((set(plan["targets"]) | held_books) - set(self.market.unavailable))
        controls = self._decision_controls(now, book, priced, order_checks)
        risk = {"portfolio": [c.as_dict() for c in controls], "orders": order_checks,
                "asset_limit": float(limit), "largest_sleeve": plan["sleeve"],
                "max_asset_pct": float(self.s.max_asset_pct)}

        for child in self.open_children():  # Yesterday's leftovers give way to today's decision.
            self._cancel_child(child, now, "superseded by a new decision")
        self.db.execute("UPDATE sys_orders SET status = 'superseded', closed_at = ? WHERE status = 'working' "
                        "AND source != 'kill_switch'", (now,))

        bars = self._bars_logged(panel, plan["relevant"])
        # Columns an earlier decision of this run logged keep a few bars here, so compare can price the backtest's
        # exit from them. Without them, a column that left the universe would read as a delisting.
        carried = set(json.loads(self.db.get("sys_logged_columns", "[]")))
        for column in sorted(carried - set(bars)):
            if column in panel.assets:
                bars.update(self._bars_logged(panel, [panel.assets.index(column)], CARRY_BARS))
        gbp_usd = None
        if self.config.quote == "USD":
            try:
                gbp_usd = str(self.market.gbp_usd())
            except Exception as error:  # Only for converting figures to GBP later. Not a trading input.
                notes.append(f"GBP/USD not read: {error}")
        targets_json = {a: ({"weight": float(t["weight"])} if "weight" in t else {"tranche": str(t["tranche"])})
                        for a, t in plan["targets"].items()}
        targets_json = {a: {**v, "column": plan["column_of"].get(a)} for a, v in targets_json.items()}
        for asset, limit_ in capped.items():  # What paper aimed for, under the per-asset limit. compare replays it.
            targets_json[asset]["aimed"] = float(limit_)
        with self.db.tx():
            decision_id = self.db.execute(
                "INSERT INTO sys_decisions(day_ts, decided_at, strategy_id, research_code_sha256, duo_code_sha256, "
                "signal_source, bars_sha256, panel, bars, signals, targets, bands, holdings, equity, cash, intended, "
                "risk, untradable, gbp_usd, notes) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bar_ts, now, self.strategy.id, sys_research.code_sha256(), sys_research.duo_code_sha256(),
                 panel.meta.get("source", sys_data.SOURCE), sys_data.panel_sha256(panel),
                 _dumps({"rows": int(panel.ts.size), "first_bar": utc(int(panel.ts[0])), "last_bar": utc(int(panel.ts[-1])),
                         "assets": len(panel.assets), "symbols_fetched": len(symbols), "universe": plan["mask"]}),
                 _dumps(bars), _dumps(plan["signals"]), _dumps(targets_json), _dumps(plan["bands"]), _dumps(holdings),
                 str(equity), str(book.cash), _dumps([{k: (str(v) if isinstance(v, Decimal) else v) for k, v in i.items()}
                                                      for i in intended]),
                 _dumps(risk), _dumps(plan["untradable"]), gbp_usd, " ".join(notes) or None),
            ).lastrowid
            for item in intended:
                self.db.execute(
                    "INSERT INTO sys_orders(decision_id, source, asset, side, qty_wanted, notional_wanted, target_weight, "
                    "weight_before, target_change, band, created_at, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "'working')",
                    (decision_id, item["source"], item["asset"], item["side"],
                     None if item["qty"] is None else str(item["qty"]),
                     None if item["notional"] is None else str(item["notional"]),
                     None if item["target_weight"] is None else str(item["target_weight"]),
                     str(item["weight_before"]), str(item["target_change"]),
                     None if item["band"] is None else str(item["band"]), now))
            self.db.set("sys_logged_columns", _dumps(sorted(carried | {panel.assets[c] for c in plan["relevant"]})))
        self.db.event("info", "systematic", f"Decision for the bar closing {utc(bar_ts + DAY)}: "
                                            f"{len(intended)} order(s) to work. " + " ".join(notes))
        return decision_id

    def _decision_controls(self, now: float, book: Book, priced: list, order_checks: list) -> list[Check]:
        """Every control in CONTROLS, on every decision, orders or not."""
        peak = D(self.db.get("sys_peak_equity", "0"))
        start = json.loads(self.db.get("sys_day_start", "null") or "null")
        start_equity = start["equity"] if start and start["day"] == day_start(now) else None
        quotes = [self.controls._quote_check(Child(0, a, "buy", Decimal(1), Decimal(1), "maker", "decision", None), book)
                  for a in priced]
        bad = [f"{a}: {q.detail}" for a, q in zip(priced, quotes) if not q.passed]
        ages = [q.value for q in quotes if q.value is not None]
        quote_fresh = Check("quote_fresh", max(ages) if len(ages) == len(quotes) and ages else None,
                            float(self.s.max_quote_age_seconds), not bad,
                            "; ".join(bad) or f"{len(priced)} quote(s) fresh, sane and moving: {', '.join(priced)}")
        caps = [c for o in order_checks for c in o["checks"] if c["control"] == "order_cap"]
        if not caps:
            order_cap = Check("order_cap", None, float(self.s.order_margin_pct), True, "no orders intended")
        else:
            failed = [f"{o['asset']} {o['side']}: {c['detail']}" for o in order_checks for c in o["checks"]
                      if c["control"] == "order_cap" and c["outcome"] != "pass"]
            order_cap = Check("order_cap", None, float(self.s.order_margin_pct), not failed,
                              "; ".join(failed) or f"{len(caps)} intended order(s), each within its cap")
        return [self.controls.drawdown(book.equity, peak if peak > 0 else None),
                self.controls.day_loss(book.equity, start_equity),
                self.controls.staleness(book.halts, self._bar_missing_minutes(now)),
                quote_fresh, *self.controls.holdings(book), order_cap]

    def _bars_logged(self, panel, relevant, count: int = CLOSES_LOGGED) -> dict:
        out = {}
        for c in relevant:
            closes = panel.close[-count:, c]
            opens = panel.open[-count:, c]
            out[panel.assets[c]] = {
                "first_ts": int(panel.ts[-closes.size]),
                "close": [None if math.isnan(x) else float(x) for x in closes.tolist()],
                "open": [None if math.isnan(x) else float(x) for x in opens.tolist()],
            }
        return out

    def _bar_missing_minutes(self, now: float) -> float:
        """How long the just-closed daily bar has been due and not seen: 0 once it's in. stale_data's value."""
        day = day_start(now)
        return 0.0 if (day - DAY) in self._bar_seen else (now - day) / 60

    def _signal_age(self, now: float) -> float | None:
        seen = max(self._bar_seen, default=None)
        return None if seen is None else now - (seen + DAY)

    # ---- Orders: maker first, re-priced, then taker with a cap ----

    def manage(self, now: float) -> None:
        for order in self.working_orders():
            try:
                self._work(order, now)
            except Exception:
                self.db.event("warn", "systematic", f"Order {order['id']} couldn't be worked:\n"
                                                    f"{traceback.format_exc(limit=3)}")

    def _work(self, order, now: float) -> None:
        open_child = self.db.one("SELECT * FROM sys_children WHERE order_id = ? AND status = 'open'", (order["id"],))
        age = now - order["created_at"]
        # A scheduled run starts a little early or late against the last one, so its thresholds give a little: a run 14
        # minutes after the last still re-prices, and the switch to taker doesn't wait a whole extra run.
        slack = self.s.cron_slack_seconds if self.scheduled else 0
        taker = order["source"] == "kill_switch" or age >= self.s.taker_after_minutes * 60 - slack
        if open_child is not None:
            reprice = now - open_child["placed_at"] >= self.s.reprice_minutes * 60 - slack
            if not (taker or reprice):
                return
            if not self._cancel_child(open_child, now, "re-priced" if not taker else "moved to taker"):
                return
            order = self.db.one("SELECT * FROM sys_orders WHERE id = ?", (order["id"],))
            if order["status"] != "working":
                return
        self._place_next(order, now, "taker" if taker else "maker")

    def _remaining(self, order) -> tuple[Decimal | None, Decimal | None]:
        """(qty for a sell, notional for a buy) still to do."""
        if order["side"] == "sell":
            return D(order["qty_wanted"]) - D(order["filled_qty"]), None
        return None, D(order["notional_wanted"]) - D(order["filled_gross"])

    def _place_next(self, order, now: float, kind: str) -> None:
        asset, side = order["asset"], order["side"]
        pair = self.market.pairs.get(asset)
        book = self.book(now)
        quote = book.quotes.get(asset)
        if pair is None or quote is None:
            self._note(order, f"no Kraken price for {asset} yet")
            return
        cap = self.s.taker_cap_pct / 100
        if kind == "maker":
            limit = _round(quote.bid, pair.pair_decimals, ROUND_DOWN) if side == "buy" \
                else _round(quote.ask, pair.pair_decimals, ROUND_UP)
            if (side == "buy" and limit >= quote.ask) or (side == "sell" and limit <= quote.bid):
                self._note(order, "no room to rest inside the spread right now")
                return
        else:
            limit = _round(quote.ask * (1 + cap), pair.pair_decimals, ROUND_DOWN) if side == "buy" \
                else _round(quote.bid * (1 - cap), pair.pair_decimals, ROUND_UP)
        qty_left, notional_left = self._remaining(order)
        if side == "sell":
            held = book.held.get(asset, Decimal(0)) - book.open_sell_qty.get(asset, Decimal(0))
            qty = _round(min(qty_left, held), pair.lot_decimals, ROUND_DOWN)
        else:
            rate = self.config.maker_pct / 100 if kind == "maker" else self.config.taker_pct / 100
            room = (book.cash - book.open_buy_cash) / (1 + rate)
            notional = min(notional_left, room)
            qty = notional / limit if limit > 0 else Decimal(0)
            limit_share = book.asset_limit.get(asset)
            if finite(book.equity) and finite(limit_share) and book.marks.get(asset):
                ceiling = (limit_share * book.equity / book.marks[asset] - book.held.get(asset, Decimal(0))
                           - book.open_buy_qty.get(asset, Decimal(0)))
                qty = min(qty, max(ceiling, Decimal(0)))
            qty = _round(qty, pair.lot_decimals, ROUND_DOWN)
        price = quote.bid if side == "sell" else quote.ask
        if qty <= 0 or qty < pair.ordermin or qty * price < pair.costmin:
            left = qty_left * price if side == "sell" else notional_left
            if left < max(pair.costmin, pair.ordermin * price) * 2 or (side == "sell" and qty_left <= 0):
                self._close_order(order, "dust", now, f"what's left ({left:.2f}) is below Kraken's minimum order")
            else:
                self._note(order, "waiting: no cash free yet, or at the per-asset limit" if side == "buy"
                           else "waiting for the holding to be free")
            return
        child = Child(order["id"], asset, side, qty, limit, kind, order["source"],
                      D(order["target_change"]) if order["target_change"] not in (None, "None") else None,
                      f"order {order['id']}", qty_cap=qty_left if side == "sell" else None)
        result = self.gate.place(child, book)
        checks = _dumps([c.as_dict() for c in result.checks])
        if not result.ok:
            self.db.execute(
                "INSERT INTO sys_children(order_id, kind, asset, side, qty, limit_price, bid, ask, placed_at, closed_at, "
                "status, checks, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked', ?, ?)",
                (order["id"], kind, asset, side, str(qty), str(limit), str(quote.bid), str(quote.ask), now, now, checks,
                 result.error))
            self._note(order, f"blocked: {result.error}")
            return
        if kind == "maker":
            self.db.execute(
                "INSERT INTO sys_children(order_id, venue_id, kind, asset, side, qty, limit_price, bid, ask, placed_at, "
                "status, checks) VALUES(?, ?, 'maker', ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (order["id"], result.venue_id, asset, side, str(qty), str(limit), str(quote.bid), str(quote.ask), now,
                 checks))
            return
        if result.fill is None:
            self.db.execute(
                "INSERT INTO sys_children(order_id, kind, asset, side, qty, limit_price, bid, ask, placed_at, closed_at, "
                "status, checks, note) VALUES(?, 'taker', ?, ?, ?, ?, ?, ?, ?, ?, 'unfilled', ?, ?)",
                (order["id"], asset, side, str(qty), str(limit), str(quote.bid), str(quote.ask), now, now, checks,
                 "the market was past the price cap"))
            return
        # The taker's row and its fill are written in one transaction: a crash leaves both or neither, never an open
        # taker that a restart could mistake for a resting order.
        self._book_fill(None, result.fill.qty, result.fill.gross, result.fill.fee, "taker", quote, now,
                        new_child=(order["id"], result.fill.venue_id, asset, side, str(qty), str(limit),
                                   str(quote.bid), str(quote.ask), now, checks))

    def _note(self, order, text: str) -> None:
        if order["note"] != text:
            self.db.execute("UPDATE sys_orders SET note = ? WHERE id = ?", (text, order["id"]))

    def _close_order(self, order, status: str, now: float, note: str) -> None:
        self.db.execute("UPDATE sys_orders SET status = ?, closed_at = ?, note = ? WHERE id = ?",
                        (status, now, note, order["id"]))

    def check_fills(self, now: float) -> None:
        """Paper resting orders fill by PaperBroker's rule, checked against fresh, unfrozen quotes only. A scheduled run
        first judges each on the price path since it was placed (_fill_on_path)."""
        for child in self.open_children():
            if child["kind"] != "maker":
                continue
            why = "nobody managed it"
            if self.scheduled:
                filled, why = self._fill_on_path(child, now)
                if filled:
                    continue
            if now - child["placed_at"] > self.gate.ttl_seconds:
                self._expire_child(child, now, why)
                continue
            quote = self.market.quotes.get(child["asset"])
            frozen = self.market.frozen_seconds(child["asset"], now)
            if quote is None or now - quote.ts > self.s.max_quote_age_seconds or frozen is None \
                    or frozen > self.s.max_frozen_quote_seconds:
                continue
            status = self.gate.check({"side": child["side"], "limit_price": child["limit_price"], "qty": child["qty"]},
                                     quote)
            if status is not None and status.done and status.qty > 0:
                self._book_fill(child, status.qty, status.gross, status.fee, "maker", quote, now)

    def _cancel_child(self, child, now: float, why: str) -> bool:
        """Cancels a resting child, booking a fill if the cancel found one. False if it couldn't be confirmed."""
        if child["kind"] != "maker":  # A taker is never resting: it filled when placed or it didn't.
            self.db.execute("UPDATE sys_children SET status = 'unfilled', closed_at = ?, note = ? WHERE id = ?",
                            (now, f"{why}; a taker left open never filled", child["id"]))
            return True
        if self.scheduled:  # A fill that already happened on the path since the last run is booked, not cancelled away.
            filled, why_not = self._fill_on_path(child, now)
            if filled:
                return True
            why = f"{why}; {why_not}"
        if now - child["placed_at"] > self.gate.ttl_seconds:
            self._expire_child(child, now, why)
            return True
        quote = self.market.quotes.get(child["asset"])
        fresh = quote is not None and now - quote.ts <= self.s.max_quote_age_seconds
        status = self.gate.cancel({"side": child["side"], "limit_price": child["limit_price"], "qty": child["qty"]},
                                  quote if fresh else None)
        if status is None:
            # No fresh quote to settle against: paper calls it missed, which can only be pessimistic.
            self.db.execute("UPDATE sys_children SET status = 'missed', closed_at = ?, note = ? WHERE id = ?",
                            (now, f"{why}; no fresh quote, so counted as unfilled", child["id"]))
            return True
        if status.qty > 0:
            self._book_fill(child, status.qty, status.gross, status.fee, "maker", quote, now)
            return True
        self.db.execute("UPDATE sys_children SET status = 'cancelled', closed_at = ?, note = ? WHERE id = ?",
                        (now, why, child["id"]))
        return True

    def _expire_child(self, child, now: float, why: str) -> None:
        """Past its life on the book (the expiry a real order would carry): closed unfilled, with no fill check."""
        self.db.execute("UPDATE sys_children SET status = 'missed', closed_at = ?, note = ? WHERE id = ?",
                        (now, f"closed unfilled: {why}, and it was past its {self.gate.ttl_seconds / 60:.0f}-minute "
                              f"life on the book", child["id"]))

    def _expire_stale_children(self, now: float, why: str) -> None:
        """After a gap, a resting child is closed as unfilled without a fill check (audit finding 55). A scheduled run
        first judges each on the price path over its life, which is a record of what the book did while nothing ran."""
        for child in self.open_children():
            if self.scheduled and child["kind"] == "maker" and self._fill_on_path(child, now)[0]:
                continue
            self.db.execute("UPDATE sys_children SET status = 'missed', closed_at = ?, note = ? WHERE id = ?",
                            (now, f"closed unfilled: {why}", child["id"]))

    def _fill_on_path(self, child, now: float) -> tuple[bool, str]:
        """A scheduled run's fill check for a resting paper child: Kraken's 1-minute candles over its life so far. Books
        the fill if there is one. Returns (filled, why not), for the note of a child that closes unfilled.

        No poll watched the book since the last run, 15 to 60 minutes ago, so the price path stands in for those polls.
        The window is every candle that starts from the whole minute after the child was placed (the minute it was
        placed in holds trades from before it) and before the end of its life or now, whichever comes first.
        PaperBroker's rule decides, through the gate like every fill check: a buy fills on the first candle whose low is
        the trade-through below its limit, a sell on the first whose high is that far above, in full, at the limit, at
        the maker fee. It's booked at that candle's start. A candle with no volume had no trades to fill against.

        Missing candles never make a fill. If they can't be read, or a minute is missing between the start of the window
        and the candle that would fill it, nothing is booked: the child stays open while it has life left, and once past
        its life it closes unfilled, which can only be pessimistic.
        """
        start = math.ceil(child["placed_at"] / 60) * 60
        end = min(now, child["placed_at"] + self.gate.ttl_seconds)
        if start >= end:
            return False, "not a whole minute on the book yet"
        try:
            candles = self.market.minute_candles(child["asset"])
        except Exception as error:  # Whatever went wrong, there's no path to judge it on.
            return False, f"Kraken's 1-minute candles couldn't be read ({error})"
        order = {"side": child["side"], "limit_price": child["limit_price"], "qty": child["qty"]}
        expected = start
        for candle in sorted((c for c in candles if start <= c.ts < end), key=lambda c: c.ts):
            if candle.ts != expected:
                return False, f"Kraken's 1-minute candles are missing the one from {hhmm(expected)} UTC"
            expected += 60
            if not candle.volume > 0:
                continue
            # The lowest trade in the minute stands in for a buy's ask, the highest for a sell's bid.
            through = Quote(child["asset"], D(candle.high), D(candle.low), D(candle.close), float(candle.ts))
            status = self.gate.check(order, through)
            if status is not None and status.done and status.qty > 0:
                low, high = D(candle.low), D(candle.high)
                self._book_fill(child, status.qty, status.gross, status.fee, "maker",
                                Quote(child["asset"], low, high, D(candle.close), float(candle.ts)), now,
                                at=float(candle.ts),
                                note=f"filled on Kraken's 1-minute candle starting {hhmm(candle.ts)} UTC, low {low} "
                                     f"and high {high}, which fill_bid and fill_ask hold")
                return True, ""
        if expected < end:
            return False, f"Kraken's 1-minute candles have nothing from {hhmm(expected)} UTC"
        return False, "no trade through its limit on Kraken's 1-minute candles"

    def _book_fill(self, child, qty: Decimal, gross: Decimal, fee: Decimal, liquidity: str, quote, now: float,
                   new_child: tuple | None = None, at: float | None = None, note: str | None = None) -> None:
        """Books a fill in one transaction: the ledger's row, the child and its order. quote is the book it filled on,
        kept as fill_bid and fill_ask. at is when it filled, if before now: a scheduled run's fill on the price path."""
        when = now if at is None else at
        order = self.db.one("SELECT * FROM sys_orders WHERE id = ?",
                            (child["order_id"] if new_child is None else new_child[0],))
        with self.db.tx():
            if new_child is not None:
                child_id = self.db.execute(
                    "INSERT INTO sys_children(order_id, venue_id, kind, asset, side, qty, limit_price, bid, ask, "
                    "placed_at, status, checks) VALUES(?, ?, 'taker', ?, ?, ?, ?, ?, ?, ?, 'open', ?)", new_child).lastrowid
                child = self.db.one("SELECT * FROM sys_children WHERE id = ?", (child_id,))
            portfolio = self.portfolio()
            realized = portfolio.apply(child["asset"], child["side"], qty, gross, fee)
            self.db.execute(
                "INSERT INTO fills(ts, session_id, source, asset, side, qty, price, gross, fee, realized, venue_id, "
                "reason, liquidity) VALUES(?, NULL, 'systematic', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (when, child["asset"], child["side"], str(qty), str(gross / qty), str(gross), str(fee), str(realized),
                 child["venue_id"] or f"sys-child-{child['id']}",
                 f"decision {order['decision_id']}, order {order['id']} ({order['source']})", liquidity))
            self.db.execute(
                "UPDATE sys_children SET status = 'filled', closed_at = ?, fill_price = ?, fill_fee = ?, fill_bid = ?, "
                "fill_ask = ?, venue_id = COALESCE(venue_id, ?), note = COALESCE(?, note) WHERE id = ?",
                (when, str(gross / qty), str(fee), str(quote.bid), str(quote.ask), f"sys-child-{child['id']}", note,
                 child["id"]))
            filled_qty = D(order["filled_qty"]) + qty
            filled_gross = D(order["filled_gross"]) + gross
            done = (order["side"] == "sell" and filled_qty >= D(order["qty_wanted"])) or \
                   (order["side"] == "buy" and filled_gross >= D(order["notional_wanted"]) * Decimal("0.999"))
            self.db.execute("UPDATE sys_orders SET filled_qty = ?, filled_gross = ?, status = ?, closed_at = ? "
                            "WHERE id = ?", (str(filled_qty), str(filled_gross), "filled" if done else order["status"],
                                             when if done else None, order["id"]))
        self.db.event("info", "systematic", f"Paper {liquidity} fill: {child['side']} {qty.normalize():f} "
                                            f"{child['asset']} at {gross / qty:.2f}, fee {fee:.2f}."
                                            + (f" It filled at {utc(at)}, on the price path since the last run."
                                               if at is not None else ""))

    # ---- Heartbeat ----

    def heartbeat(self, now: float, gap: float | None, controls: list[Check]) -> None:
        halts = self.halts(now)
        book = self.book(now, halts)
        quote_ages = [now - q.ts for q in book.quotes.values()]
        reasons = []
        if halts.kill:
            reasons.append(f"kill switch: {halts.kill_reason}")
        if halts.daily_loss is None or halts.daily_loss:
            reasons.append("daily loss limit: no buys today" if halts.daily_loss else "daily loss unknown: no buys")
        if halts.signal_stale:
            reasons.append("daily bar missing: no new orders")
        if halts.api_errors >= self.s.max_api_errors:
            reasons.append(f"{halts.api_errors} API errors in a row: no new orders")
        if not finite(book.equity):
            reasons.append("equity unknown: no new orders")
        stale = sorted(a for a in self.book_assets()
                       if a not in book.quotes or now - book.quotes[a].ts > self.s.max_quote_age_seconds
                       or (self.market.frozen_seconds(a, now) or 0) > self.s.max_frozen_quote_seconds)
        stale = [a for a in stale if a not in self.market.unavailable]
        if stale:
            reasons.append(f"Kraken quote missing, old or frozen for {', '.join(stale)}: no new orders on those")
        if self.db.get("sys_waiting"):
            reasons.append(self.db.get("sys_waiting"))
        status = "halted" if halts.kill else ("degraded" if reasons else "ok")
        signal_age = self._signal_age(now)
        # For the watchdog: how long it has been degraded, and whether today's decision is overdue. An exchange outage
        # keeps the loop beating, so a stale heartbeat alone would never say so.
        if status == "degraded":
            if not finite(self.db.get("sys_degraded_since")):
                self.db.set("sys_degraded_since", now)
            degraded_since = float(self.db.get("sys_degraded_since"))
        else:
            self.db.clear("sys_degraded_since")
            degraded_since = None
        last_decision = self.db.one("SELECT day_ts FROM sys_decisions ORDER BY day_ts DESC LIMIT 1")
        due_minutes = self.s.signal_stale_minutes
        if self.scheduled:  # The first run after 00:00 UTC can start an hour late or be skipped without anything wrong.
            due_minutes = max(due_minutes, self.s.cron_decision_grace_minutes)
        due_at = day_start(now) + due_minutes * 60
        overdue = None
        if not halts.kill and (last_decision is None or last_decision["day_ts"] < day_start(now) - DAY) and now > due_at:
            overdue = (now - due_at) / 60
        self.db.execute(
            "INSERT INTO sys_heartbeats(ts, status, signal_age_s, quote_age_s, equity, halts, controls, loop_gap_s) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (now, status, signal_age, max(quote_ages) if quote_ages else None,
             str(book.equity) if finite(book.equity) else None, _dumps(reasons),
             _dumps([c.as_dict() for c in controls]), gap))
        payload = {
            "ts": now, "utc": utc(now), "pid": os.getpid(), "status": status, "halts": reasons,
            "strategy": self.strategy.id, "mode": self.config.mode,
            "equity": str(book.equity) if finite(book.equity) else None,
            "signal_age_s": signal_age, "next_decision": utc(self.next_decision_at(now)),
            "loop_minutes": self.s.loop_minutes, "schedule": "cron" if self.scheduled else "loop",
            "degraded_since": degraded_since, "degraded_alert_minutes": self.s.degraded_alert_minutes,
            "last_decision_bar": utc(last_decision["day_ts"]) if last_decision else None,
            "decision_overdue_minutes": overdue,
        }
        write_heartbeat(self.heartbeat_path, payload)

    # ---- What the dashboard and the exports read ----

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        halts = self.halts(now)
        book = self.book(now, halts)
        last = self.db.one("SELECT * FROM sys_decisions ORDER BY id DESC LIMIT 1")
        targets = json.loads(last["targets"]) if last else {}
        rows = []
        for asset in sorted(set(targets) | set(book.held)):
            qty = book.held.get(asset, Decimal(0))
            mark = book.marks.get(asset)
            value = qty * mark if mark is not None else None
            rows.append({
                "asset": asset, "qty": _fl(qty), "price": _fl(mark), "value": _fl(value),
                "weight": _fl(value / book.equity) if value is not None and finite(book.equity) and book.equity else None,
                "target": targets.get(asset, {}).get("weight"), "tranche": targets.get(asset, {}).get("tranche"),
                "column": targets.get(asset, {}).get("column"),
            })
        heartbeat = self.db.one("SELECT * FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        decisions = []
        for row in self.db.all("SELECT * FROM sys_decisions ORDER BY id DESC LIMIT 7"):
            decisions.append({
                "id": row["id"], "bar": utc(row["day_ts"]), "decided_at": row["decided_at"],
                "targets": json.loads(row["targets"]), "intended": json.loads(row["intended"]),
                "risk": json.loads(row["risk"])["portfolio"], "untradable": json.loads(row["untradable"]),
                "notes": row["notes"], "equity": _fl(D(row["equity"])) if row["equity"] else None,
            })
        orders = []
        for row in self.db.all("SELECT * FROM sys_orders ORDER BY id DESC LIMIT 20"):
            children = [dict(c) for c in self.db.all("SELECT id, kind, qty, limit_price, bid, ask, placed_at, status, "
                                                     "fill_price, fill_fee, note FROM sys_children WHERE order_id = ? "
                                                     "ORDER BY id", (row["id"],))]
            orders.append({**dict(row), "children": children})
        return {
            "strategy": self.strategy.id, "family": self.strategy.family, "start": self.start,
            "now": now, "next_decision": self.next_decision_at(now), "next_loop": self.next_loop,
            "halts": {"kill": halts.kill, "kill_reason": halts.kill_reason, "daily_loss": halts.daily_loss,
                      "signal_stale": halts.signal_stale, "api_errors": halts.api_errors,
                      "waiting": self.db.get("sys_waiting")},
            "how_to_resume": "The kill switch stays on until you press Resume on this panel or run "
                             "python -m duo resume. Every other halt clears by itself when its cause does.",
            "controls": json.loads(heartbeat["controls"]) if heartbeat else [],
            "heartbeat": dict(heartbeat) if heartbeat else None,
            "equity": _fl(book.equity), "cash": _fl(book.cash), "holdings": rows,
            "asset_limits": {a: float(v) for a, v in book.asset_limit.items()},
            "decisions": decisions, "orders": orders,
        }


def resume_in_db(db: DB, now: float | None = None) -> str:
    """python -m duo resume. Works whether or not the bot is running: it reads these keys on every loop.

    The peak is cleared rather than set, because this has no prices. The next loop sets it to equity then.
    """
    now = time.time() if now is None else now
    db.conn.executescript(SCHEMA)
    if db.get("sys_kill") != "1":
        return "The systematic runner isn't halted, so there's nothing to resume."
    reason = db.get("sys_kill_reason", "")
    db.set("sys_kill", "0")
    for key in ("sys_kill_reason", "sys_kill_source", "sys_kill_flattened", "sys_peak_equity"):
        db.clear(key)
    db.execute("UPDATE sys_orders SET status = 'cancelled', closed_at = ?, note = 'resumed before flat' "
               "WHERE status = 'working' AND source = 'kill_switch'", (now,))
    db.event("info", "kill_switch", f"Systematic trading resumed from the command line (it was halted: {reason}). "
                                    f"The drawdown peak restarts from equity at the next loop.")
    return f"Resumed. It was halted: {reason}. The next daily decision trades as usual."


def sane_quote(quote) -> bool:
    try:
        return finite(quote.bid) and finite(quote.ask) and quote.bid > 0 and quote.ask >= quote.bid
    except AttributeError:
        return False


def band_trades(target: float, current: float, band: float) -> bool:
    """research/harness/engine.py's rule: trade when the change passes the band, and always when the target is 0."""
    return abs(target - current) > band or (target == 0.0 and current > 0.0)


def write_heartbeat(path: Path, payload: dict) -> None:
    """Atomically, so the watchdog never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat-")
    with os.fdopen(fd, "w") as out:
        json.dump(payload, out, default=str)
    os.replace(temp, path)


def export_decisions(db: DB) -> list[dict]:
    """Every decision with the orders, child orders and fills that followed it."""
    out = []
    for row in db.all("SELECT * FROM sys_decisions ORDER BY id"):
        item = dict(row)
        for key in ("panel", "bars", "signals", "targets", "bands", "holdings", "intended", "risk", "untradable"):
            item[key] = json.loads(item[key])
        item["bar_utc"] = utc(row["day_ts"])
        item["decided_utc"] = utc(row["decided_at"])
        item["orders"] = [_export_order(db, order) for order in
                          db.all("SELECT * FROM sys_orders WHERE decision_id = ? ORDER BY id", (row["id"],))]
        out.append(item)
    loose = db.all("SELECT * FROM sys_orders WHERE decision_id IS NULL ORDER BY id")
    if loose:  # Only from before flatten orders were linked to the latest decision. Exported all the same.
        out.append({"record": "orders_without_a_decision", "orders": [_export_order(db, o) for o in loose]})
    return out


def _export_order(db: DB, order) -> dict:
    children = []
    for child in db.all("SELECT * FROM sys_children WHERE order_id = ? ORDER BY id", (order["id"],)):
        c = dict(child)
        c["checks"] = json.loads(c["checks"]) if c["checks"] else None
        c["fills"] = [dict(f) for f in db.all("SELECT * FROM fills WHERE venue_id = ? AND source = 'systematic'",
                                              (child["venue_id"],))] if child["venue_id"] else []
        children.append(c)
    return {**dict(order), "children": children}
