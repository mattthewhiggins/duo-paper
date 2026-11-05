"""The main loop: watches prices, enforces stops and limits, runs plans, wakes the agents, and is the only code that places orders."""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from .agents import BRIEF_TEXT_LIMIT, AgentFailed, Budget, BudgetExceeded, Proposal, SessionResult, SessionRunner
from .brokers import Fill, OrderRejected, OrderStateUnknown, RestingStatus
from .config import D, Config
from .db import DB
from .kraken import KrakenError, KrakenNetworkError
from .ledger import Portfolio
from .market import BENCHMARK_ASSET, Market, Quote
from .risk import RESTING, Order, Rejected, RiskContext, RiskGate, Ticket, marks_from

FAILURE_BACKOFF_SECONDS = 900
JOURNAL_ENTRIES = 8
RECENT_FILLS = 10
PLAN_RETRY_SECONDS = 60
DESK_MESSAGES_IN_BRIEF = 6
DESK_NOTES_IN_BRIEF = 12


def day_start(now: float) -> float:
    return now - now % 86400


SYSTEMATIC_REFUSAL = "Systematic mode is on, so only the systematic runner can trade. The agents have no authority to."
ERROR_BACKOFF_SECONDS = 30


class Engine:
    def __init__(self, config: Config, db: DB, market: Market, broker, client=None, systematic=None):
        self.config = config
        self.db = db
        self.market = market
        self.broker = broker
        self.budget = Budget(db, config)
        # In systematic mode the agents only talk, and only if switched on. They can never trade: see _refuse_in_systematic.
        self.systematic_mode = config.systematic_on
        self.systematic = systematic
        if self.systematic_mode and not config.systematic.agents_enabled:
            client = None
        self.runner = SessionRunner(config, db, market, client) if client is not None else None
        self.gate: RiskGate | None = None
        self._order_lock = threading.RLock()
        self._last_reconcile = 0.0
        self._plan_retry_at: dict[int, float] = {}
        self._kick = threading.Event()
        self._stopping = threading.Event()
        self._session_thread: threading.Thread | None = None

    # ---- Books and metrics ----

    def portfolio(self) -> Portfolio:
        return Portfolio.replay(self.config.starting_capital, self.db.all("SELECT * FROM fills ORDER BY id"))

    def api_cost_in_quote(self) -> Decimal:
        total = D(round(self.budget.total(), 6))
        try:
            rate = self.market.usd_to_quote()
            self.db.set("usd_to_quote", rate)
        except (KrakenError, KrakenNetworkError, ValueError):
            rate = D(self.db.get("usd_to_quote", "1"))
        return total * rate

    def metrics(self, quotes: dict[str, Quote] | None = None) -> dict | None:
        """Every number shown on the dashboard. All P&L here is in the quote currency."""
        quotes = self.market.quotes if quotes is None else quotes
        if BENCHMARK_ASSET not in quotes:
            return None
        portfolio = self.portfolio()
        marks = marks_from(quotes)
        if any(a not in marks for a, p in portfolio.positions.items() if p.qty):
            return None
        starting = self.config.starting_capital
        equity = portfolio.equity(marks)
        api_cost = self.api_cost_in_quote()
        benchmark_qty = D(self.db.get("benchmark_qty", "0"))
        benchmark = benchmark_qty * marks[BENCHMARK_ASSET] if benchmark_qty else starting
        net = equity - starting - api_cost
        benchmark_pnl = benchmark - starting
        return {
            "portfolio": portfolio,
            "marks": marks,
            "equity": equity,
            "starting": starting,
            "cash": portfolio.cash,
            "realized": portfolio.realized,
            "unrealized": portfolio.unrealized(marks),
            "fees": portfolio.fees,
            "api_cost": api_cost,
            "net_pnl": net,
            "net_pnl_pct": net / starting * 100,
            "benchmark": benchmark,
            "benchmark_pnl": benchmark_pnl,
            "vs_benchmark": net - benchmark_pnl,
        }

    def buys_today(self, now: float) -> int:
        """Counts each order once, however many pieces it filled in, plus resting buys that could still fill."""
        return self.db.one(
            "SELECT COUNT(*) AS n FROM ("
            "SELECT COALESCE(venue_id, 'fill-' || id) AS ref FROM fills "
            "WHERE side = 'buy' AND source IN ('agents', 'plan') AND ts >= ? "
            "UNION SELECT venue_id FROM resting_orders "
            "WHERE side = 'buy' AND status = 'open' AND source IN ('agents', 'plan'))",
            (day_start(now),),
        )["n"]

    def fees_30d(self, now: float) -> Decimal:
        rows = self.db.all("SELECT fee FROM fills WHERE ts >= ?", (now - 30 * 86400,))
        return sum((D(r["fee"]) for r in rows), Decimal(0))

    def open_resting(self, asset: str | None = None) -> list:
        if asset is None:
            return self.db.all("SELECT * FROM resting_orders WHERE status = 'open' ORDER BY id")
        return self.db.all("SELECT * FROM resting_orders WHERE status = 'open' AND asset = ? ORDER BY id", (asset,))

    def waiting_plans(self) -> list:
        return self.db.all("SELECT * FROM plans WHERE status IN ('active', 'working') ORDER BY id")

    def resting_stats(self) -> dict:
        """How the fee-saving trade-off is working out: how often resting orders fill, and what that saved."""
        closed = [row["status"] for row in self.db.all("SELECT status FROM resting_orders WHERE status != 'open'")]
        maker_value = sum((D(row["gross"]) for row in self.db.all("SELECT gross FROM fills WHERE liquidity = 'maker'")), Decimal(0))
        return {
            "closed": len(closed),
            "filled": sum(status != "missed" for status in closed),
            "fees_saved": maker_value * (self.config.taker_pct - self.config.maker_pct) / 100,
        }

    def _reservations(self) -> tuple[Decimal, dict[str, Decimal]]:
        cash = Decimal(0)
        quantities: dict[str, Decimal] = {}
        for order in self.open_resting():
            if order["side"] != "buy":
                continue
            left = D(order["qty"]) - D(order["booked_qty"])
            cash += left * D(order["limit_price"]) * (1 + self.config.maker_pct / 100)
            quantities[order["asset"]] = quantities.get(order["asset"], Decimal(0)) + left
        return cash, quantities

    def risk_context(self, now: float) -> RiskContext:
        paused_day = self.db.get("buys_paused_day")
        reserved_cash, reserved_qty = self._reservations()
        return RiskContext(
            now=now,
            halted=self.db.get("halted") == "1",
            halt_reason=self.db.get("halt_reason", ""),
            buys_paused=paused_day is not None and float(paused_day) == day_start(now),
            buys_today=self.buys_today(now),
            fees_30d=self.fees_30d(now),
            reserved_cash=reserved_cash,
            reserved_qty=reserved_qty,
            active_plans=len(self.waiting_plans()),
        )

    # ---- Controls ----

    def halt(self, reason: str) -> None:
        # In systematic mode the runner logs its own halt, and the agents never trade, so there's no agent trading to
        # report stopped. The key is still set, quietly, so the desk chat pauses while the runner is halted.
        quiet = self.systematic is not None and reason == "stopped from the dashboard"
        if quiet:
            self.systematic.stop_trading(time.time(), reason, flatten=False)
        if self.db.get("halted") != "1":
            self.db.set("halted", "1")
            self.db.set("halt_reason", reason)
            if not quiet:
                self.db.event("critical", "halt", f"Agent trading stopped: {reason}")

    def resume(self) -> None:
        if self.systematic is not None:
            self.systematic.resume()  # It logs its own resume, so nothing here says agent trading resumed.
        if self.db.get("halted") == "1":
            self.db.set("halted", "0")
            self.db.clear("halt_reason")
            metrics = self.metrics()
            if metrics:  # Otherwise the old peak would trip the drawdown limit straight away.
                self.db.set("peak_equity", metrics["equity"])
            if self.systematic is None:
                self.db.event("info", "resume", "Agent trading resumed from the dashboard. The drawdown peak was reset to current equity.")
            self.kick()

    def request_wake(self) -> None:
        self.db.set("wake_requested", "1")
        self.kick()

    def request_flatten(self, by: str = "the dashboard") -> None:
        self.db.set("flatten_requested", "1")
        self.db.set("flatten_requested_by", by)
        self.db.event("critical", "flatten", f"Sell everything requested from {by}.")
        self.kick()

    def cancel_plan(self, plan_id: int) -> bool:
        with self._order_lock:
            cancelled = self._cancel_plan(plan_id, "Cancelled from the dashboard")
        if cancelled:
            self.db.event("info", "plan", f"Plan {plan_id} cancelled from the dashboard.")
        return cancelled

    def kick(self) -> None:
        self._kick.set()

    # ---- Main loop ----

    def run(self) -> None:
        self._recover()
        while not self._stopping.is_set():
            self.run_once()
            self._kick.wait(self.config.poll_seconds)
            self._kick.clear()

    def run_once(self) -> bool:
        """One tick that can't end the loop. Returns False if it failed.

        If the error handler itself fails, as it does when the database is what's broken, the loop waits and carries on
        rather than dying with every limit and stop in it (audit finding 58).
        """
        try:
            self.tick()
            return True
        except Exception:
            text = f"Unexpected error in the main loop:\n{traceback.format_exc(limit=5)}"
            try:
                self.db.event("critical", "error", text)
            except Exception:
                print(text, flush=True)
                self._stopping.wait(ERROR_BACKOFF_SECONDS)
            return False

    def stop(self) -> None:
        self._stopping.set()
        self._kick.set()
        thread = self._session_thread
        if thread and thread.is_alive():
            thread.join(timeout=30)

    def _recover(self) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = ?, outcome = 'interrupted' WHERE ended_at IS NULL", (time.time(),)
        )
        self.db.set("session_running", "0")

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            quotes = self.market.refresh()
        except (KrakenError, KrakenNetworkError, ValueError) as error:
            if self.db.get("market_down_since") is None:
                self.db.set("market_down_since", now)
                self.db.event("warn", "market", f"Can't get prices from Kraken: {error}. Stop-losses can't fire until this clears.")
            if self.systematic is not None:
                self.systematic.on_tick(now, error=str(error))
            return
        if self.db.get("market_down_since") is not None:
            self.db.clear("market_down_since")
            self.db.event("info", "market", "Prices are back.")
        if self.gate is None:
            self.gate = RiskGate(self.config.risk, self.config.maker_pct, self.config.taker_pct,
                                 self.config.assets, self.market.pairs)

        self._ensure_benchmark(quotes)
        if self.systematic is not None:
            if self.db.get("flatten_requested") == "1":
                by = self.db.get("flatten_requested_by") or "the dashboard"
                self.systematic.stop_trading(now, f"sell everything was requested from {by}", flatten=True)
                self.db.clear("flatten_requested")
                self.db.clear("flatten_requested_by")
            self.systematic.on_tick(now)
            quotes = self.market.quotes  # The runner may have widened what's tracked.
        else:
            self._check_resting(quotes, now)
            self._check_stops(quotes)
            self._check_flatten(quotes)
            self._check_plans(quotes, now)
        metrics = self.metrics(quotes)
        if metrics is None:
            return
        self._snapshot(metrics, now)
        if not self.systematic_mode:  # The runner has its own limits, in duo/sys_risk.py.
            self._check_loss_limits(metrics, now)
        if now - self._last_reconcile >= self.config.reconcile_seconds:
            self._last_reconcile = now
            self._reconcile(metrics)
        self._maybe_wake(quotes, now)

    def _ensure_benchmark(self, quotes: dict[str, Quote]) -> None:
        """The comparison portfolio: all the starting capital in BTC at the first price seen, fee included."""
        if self.db.get("benchmark_qty") is None:
            ask = quotes[BENCHMARK_ASSET].ask
            fee = self.config.taker_pct / 100
            self.db.set("benchmark_qty", self.config.starting_capital / (ask * (1 + fee)))
            self.db.set("started_at", time.time())

    def _snapshot(self, metrics: dict, now: float, force: bool = False) -> None:
        last = float(self.db.get("last_snapshot", "0"))
        if not force and now - last < self.config.snapshot_minutes * 60:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots(ts, equity, cash, benchmark, realized, unrealized, fees, api_cost) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (now, float(metrics["equity"]), float(metrics["cash"]), float(metrics["benchmark"]),
             float(metrics["realized"]), float(metrics["unrealized"]), float(metrics["fees"]), float(metrics["api_cost"])),
        )
        self.db.set("last_snapshot", now)

    def day_open_equity(self, now: float) -> Decimal | None:
        row = self.db.one("SELECT equity FROM snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (day_start(now),))
        return D(row["equity"]) if row else None

    def _check_loss_limits(self, metrics: dict, now: float) -> None:
        equity = metrics["equity"]
        risk = self.config.risk
        peak = max(D(self.db.get("peak_equity", "0")), equity)
        self.db.set("peak_equity", peak)
        if peak > 0 and (peak - equity) / peak * 100 >= risk.drawdown_halt_pct:
            self.halt(f"equity fell {risk.drawdown_halt_pct}% from its peak of {peak:.2f}")

        day_open = self.day_open_equity(now)
        today = day_start(now)
        if day_open and day_open > 0 and (day_open - equity) / day_open * 100 >= risk.daily_loss_pause_pct:
            if self.db.get("buys_paused_day") != str(today):
                self.db.set("buys_paused_day", today)
                self.db.event("warn", "daily_loss", f"Down {risk.daily_loss_pause_pct}% today, so buys are paused until 00:00 UTC.")

    def _reconcile(self, metrics: dict) -> None:
        problem = self.broker.reconcile(metrics["portfolio"])
        if problem:
            self.halt(f"balances don't match: {problem}")

    # ---- Orders ----

    def _record_fill(self, asset: str, side: str, fill: Fill, source: str, session_id: int | None, reason: str | None,
                     stop: Decimal | None, target: Decimal | None) -> None:
        """Books a fill. Callers wrap this in a transaction with anything that has to change alongside it."""
        portfolio = self.portfolio()
        realized = portfolio.apply(asset, side, fill.qty, fill.gross, fill.fee)
        self.db.execute(
            "INSERT INTO fills(ts, session_id, source, asset, side, qty, price, gross, fee, realized, venue_id, reason, liquidity) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), session_id, source, asset, side, str(fill.qty), str(fill.price),
             str(fill.gross), str(fill.fee), str(realized), fill.venue_id, reason, fill.liquidity),
        )
        if side == "buy" and stop is not None:
            self._set_stop(asset, stop, session_id, target)
        if portfolio.qty(asset) <= 0:
            self.db.execute("DELETE FROM stops WHERE asset = ?", (asset,))

    def _after_fill(self) -> None:
        metrics = self.metrics()
        if metrics:
            self._snapshot(metrics, time.time(), force=True)

    def _describe(self, side: str, fill: Fill) -> str:
        return f"{fill.qty.normalize():f} at {fill.price:.2f} ({fill.gross:.2f} {self.config.quote}, fee {fill.fee:.2f})"

    def _refuse_in_systematic(self, ticket: Ticket, source: str) -> tuple[bool, str] | None:
        """The hard check: while systematic mode is on, nothing on the agents' paths reaches a broker."""
        if not self.systematic_mode:
            return None
        text = f"Refused {ticket.side} {ticket.asset} from {source}: {SYSTEMATIC_REFUSAL}"
        self.db.event("warn", "limits", text)
        return False, text

    def _execute(self, ticket: Ticket, quote: Quote, source: str, session_id: int | None) -> tuple[bool, str]:
        """Sends an immediate order to the broker and books the result. Returns (filled, description)."""
        refused = self._refuse_in_systematic(ticket, source)
        if refused:
            return refused
        try:
            fill = self.broker.execute(ticket, quote)
        except OrderRejected as error:
            text = f"Kraken rejected {ticket.side} {ticket.asset}: {error}"
            self.db.event("warn", "order", text)
            return False, text
        except OrderStateUnknown as error:
            text = f"Check Kraken by hand. {error}"
            self.halt(f"an order's outcome is unknown. {text}")
            return False, text
        if fill is None:
            text = f"{ticket.side.capitalize()} {ticket.asset} didn't fill within its price limit of {ticket.limit_price}"
            self.db.event("info", "order", text)
            return False, text
        with self.db.tx():
            self._record_fill(ticket.asset, ticket.side, fill, source, session_id, ticket.reason, ticket.stop_price, ticket.target_price)
        self._after_fill()
        return True, f"{ticket.side.capitalize()} {ticket.asset}: {self._describe(ticket.side, fill)}"

    def _place_resting(self, ticket: Ticket, quote: Quote, source: str, session_id: int | None,
                       plan_id: int | None = None) -> tuple[bool, str]:
        """Puts a post-only order in the book. Fills are booked as they happen, by _check_resting."""
        refused = self._refuse_in_systematic(ticket, source)
        if refused:
            return refused
        minutes = self.config.risk.resting_ttl_minutes
        try:
            venue_id = self.broker.place_resting(ticket, quote, minutes * 60)
        except OrderRejected as error:
            text = f"Kraken rejected a resting {ticket.side} of {ticket.asset}: {error}"
            self.db.event("warn", "order", text)
            return False, text
        except OrderStateUnknown as error:
            text = f"Check Kraken by hand. {error}"
            self.halt(f"an order's outcome is unknown. {text}")
            return False, text
        now = time.time()
        self.db.execute(
            "INSERT INTO resting_orders(venue_id, placed_at, expires_at, session_id, plan_id, source, asset, side, qty, "
            "limit_price, stop_price, target_price, reason, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (venue_id, now, now + minutes * 60, session_id, plan_id, source, ticket.asset, ticket.side, str(ticket.qty),
             str(ticket.limit_price),
             str(ticket.stop_price) if ticket.stop_price is not None else None,
             str(ticket.target_price) if ticket.target_price is not None else None,
             ticket.reason),
        )
        return True, f"Resting {ticket.side} of {ticket.qty.normalize():f} {ticket.asset} at {ticket.limit_price}, for up to {minutes} minutes"

    def _settle(self, order, status: RestingStatus) -> None:
        """Books whatever filled since the last look, and closes the order if it's finished."""
        new_qty = status.qty - D(order["booked_qty"])
        asset, side = order["asset"], order["side"]
        fill = None
        with self.db.tx():
            if new_qty > 0:
                gross = status.gross - D(order["booked_gross"])
                fee = status.fee - D(order["booked_fee"])
                fill = Fill(new_qty, gross / new_qty, gross, fee, order["venue_id"], "maker")
                self._record_fill(
                    asset, side, fill, order["source"], order["session_id"], order["reason"],
                    D(order["stop_price"]) if order["stop_price"] else None,
                    D(order["target_price"]) if order["target_price"] else None,
                )
                self.db.execute(
                    "UPDATE resting_orders SET booked_qty = ?, booked_gross = ?, booked_fee = ? WHERE id = ?",
                    (str(status.qty), str(status.gross), str(status.fee), order["id"]),
                )
            if status.done:
                outcome = "missed" if status.qty <= 0 else "filled" if status.qty >= D(order["qty"]) else "partial"
                self.db.execute("UPDATE resting_orders SET status = ?, closed_at = ? WHERE id = ?",
                                (outcome, time.time(), order["id"]))
                if order["plan_id"]:
                    self._plan_after_order(order["plan_id"], outcome)
        if fill:
            self.db.event("info", "order", f"Resting {side} of {asset} filled: {self._describe(side, fill)}.")
            self._after_fill()
            if order["source"] == "target":
                self.db.set("exit_hit", f"Profit target hit on {asset}")
        elif status.done and not order["plan_id"] and order["source"] == "agents":
            self.db.event("info", "order", f"Resting {side} of {asset} at {order['limit_price']} didn't fill, so it was cancelled.")

    def _plan_after_order(self, plan_id: int, outcome: str) -> None:
        plan = self.db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan is None or plan["status"] != "working":
            return
        if outcome != "missed":
            self._close_plan(plan_id, "filled")
        elif time.time() >= plan["expires_at"]:
            self._close_plan(plan_id, "expired")
        else:
            self.db.execute("UPDATE plans SET status = 'active' WHERE id = ?", (plan_id,))

    def _check_resting(self, quotes: dict[str, Quote], now: float) -> None:
        halted = self.db.get("halted") == "1"
        with self._order_lock:
            for order in self.open_resting():
                quote = quotes.get(order["asset"])
                if quote is None:
                    continue
                # A halt stops new buying, so waiting buys go too. Waiting sells only reduce risk.
                if now >= order["expires_at"] or (halted and order["side"] == "buy"):
                    status = self.broker.cancel_resting(order, quote)
                else:
                    status = self.broker.check_resting(order, quote)
                if status is not None:
                    self._settle(order, status)

    def _cancel_asset_orders(self, asset: str, quote: Quote) -> bool:
        """Cancels everything waiting on an asset. Returns False if any order couldn't be confirmed closed."""
        all_closed = True
        for order in self.open_resting(asset):
            status = self.broker.cancel_resting(order, quote)
            if status is None or not status.done:
                all_closed = False
                continue
            self._settle(order, status)
        return all_closed

    def _set_stop(self, asset: str, stop: Decimal, session_id: int | None, target: Decimal | None = None) -> None:
        self.db.execute(
            "INSERT INTO stops(asset, stop_price, target_price, set_at, session_id) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(asset) DO UPDATE SET stop_price = excluded.stop_price, "
            "target_price = COALESCE(excluded.target_price, stops.target_price), "
            "set_at = excluded.set_at, session_id = excluded.session_id",
            (asset, str(stop), str(target) if target is not None else None, time.time(), session_id),
        )

    def _check_stops(self, quotes: dict[str, Quote]) -> None:
        for row in self.db.all("SELECT * FROM stops"):
            asset, stop = row["asset"], D(row["stop_price"])
            target = D(row["target_price"]) if row["target_price"] else None
            quote = quotes.get(asset)
            if quote is None:
                continue
            with self._order_lock:
                if quote.bid <= stop:
                    self._exit_on_stop(asset, stop, quote)
                elif target is not None and quote.bid >= target:
                    self._work_target(asset, target, quote)

    def _exit_on_stop(self, asset: str, stop: Decimal, quote: Quote) -> None:
        """Stops pay the higher fee to get out now. Anything waiting on the asset has to go first."""
        label = f"Stop-loss at {stop}"
        if not self._cancel_asset_orders(asset, quote):
            self.db.event("warn", "stop", f"{label} hit on {asset}, but an open order couldn't be cancelled yet. Retrying.")
            return
        # The stop says the idea behind any waiting buys has failed, so they don't get to re-enter on their own.
        for plan in self.db.all("SELECT id FROM plans WHERE asset = ? AND status IN ('active', 'working')", (asset,)):
            self._cancel_plan(plan["id"], f"Cancelled because the {asset} stop-loss fired")
        ticket = self.gate.exit_ticket(asset, self.portfolio(), quote, f"{label} hit (bid {quote.bid})")
        if ticket is None:
            self.db.execute("DELETE FROM stops WHERE asset = ?", (asset,))
            return
        filled, text = self._execute(ticket, quote, "stop", None)
        self.db.event("warn", "stop", f"{label} hit on {asset}. {text}." if filled else f"{label} hit on {asset} but not sold yet, retrying: {text}")
        if filled:
            self.db.set("exit_hit", f"{label} hit on {asset}")

    def _work_target(self, asset: str, target: Decimal, quote: Quote) -> None:
        """Offers the position in the book for the lower fee. If it doesn't fill, this tries again while the price holds."""
        if any(order["side"] == "sell" for order in self.open_resting(asset)):
            return
        ticket = self.gate.resting_exit_ticket(asset, self.portfolio(), quote, f"Profit target at {target} hit (bid {quote.bid})")
        if ticket is None:
            return
        placed, text = self._place_resting(ticket, quote, "target", None)
        if placed:
            self.db.event("info", "target", f"Profit target at {target} reached on {asset}. {text}.")

    def _check_flatten(self, quotes: dict[str, Quote]) -> None:
        if self.db.get("flatten_requested") != "1":
            return
        self.halt("sell everything was requested from the dashboard")
        with self._order_lock:
            for plan in self.waiting_plans():
                self._cancel_plan(plan["id"], "Cancelled by sell everything")
            all_closed = True
            for asset in {order["asset"] for order in self.open_resting()}:
                all_closed = (asset in quotes and self._cancel_asset_orders(asset, quotes[asset])) and all_closed
            for asset, position in list(self.portfolio().positions.items()):
                if position.qty <= 0 or asset not in quotes:
                    continue
                ticket = self.gate.exit_ticket(asset, self.portfolio(), quotes[asset], "Sell everything requested from the dashboard")
                if ticket:
                    _, text = self._execute(ticket, quotes[asset], "flatten", None)
                    self.db.event("warn", "flatten", text)
            portfolio = self.portfolio()
        leftover = [
            asset for asset, position in portfolio.positions.items()
            if position.qty > 0 and (asset not in quotes or self.gate.exit_ticket(asset, portfolio, quotes[asset], ""))
        ]
        if all_closed and not leftover:
            self.db.clear("flatten_requested")
            self.db.clear("flatten_requested_by")
            self.db.execute("DELETE FROM stops")
            self.db.event("info", "flatten", "All positions are sold.")

    # ---- Plans: buys the agents approved in advance, carried out by code when the price arrives ----

    def _close_plan(self, plan_id: int, status: str, note: str | None = None) -> None:
        self.db.execute(
            "UPDATE plans SET status = ?, closed_at = ?, note = COALESCE(?, note) WHERE id = ?",
            (status, time.time(), note, plan_id),
        )
        self._plan_retry_at.pop(plan_id, None)

    def _cancel_plan(self, plan_id: int, note: str) -> bool:
        plan = self.db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan is None or plan["status"] not in ("active", "working"):
            return False
        for order in self.db.all("SELECT * FROM resting_orders WHERE plan_id = ? AND status = 'open'", (plan_id,)):
            quote = self.market.quotes.get(order["asset"])
            status = self.broker.cancel_resting(order, quote) if quote else None
            if status is None or not status.done:
                return False
            self._settle(order, status)
        plan = self.db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan["status"] in ("active", "working"):
            self._close_plan(plan_id, "cancelled", note)
        return True

    def _check_plans(self, quotes: dict[str, Quote], now: float) -> None:
        halted = self.db.get("halted") == "1"
        for plan in self.db.all("SELECT * FROM plans WHERE status = 'active' ORDER BY id"):
            if now >= plan["expires_at"]:
                self._close_plan(plan["id"], "expired")
                self.db.event("info", "plan", f"Plan {plan['id']} to buy {plan['asset']} expired without being filled.")
                continue
            quote = quotes.get(plan["asset"])
            if halted or quote is None or now < self._plan_retry_at.get(plan["id"], 0):
                continue
            trigger = D(plan["trigger_price"])
            if quote.bid <= trigger if plan["direction"] == "below" else quote.ask >= trigger:
                with self._order_lock:
                    self._fire_plan(plan, quotes, quote, now)

    def _fire_plan(self, plan, quotes: dict[str, Quote], quote: Quote, now: float) -> None:
        """Sizes the plan at this moment's prices and balances. Every limit applies again here."""
        order = Order(plan["asset"], "buy", D(plan["quote_amount"]), None, D(plan["stop_price"]), plan["reason"] or "",
                      D(plan["target_price"]), plan["execution"])
        try:
            ticket = self.gate.size(order, self.portfolio(), quotes, self.risk_context(time.time()))
        except Rejected as rejection:
            # It stays active, because the block may clear before the plan expires. Logged once per reason.
            if plan["note"] != str(rejection):
                self.db.execute("UPDATE plans SET note = ? WHERE id = ?", (str(rejection), plan["id"]))
                self.db.event("info", "plan", f"Plan {plan['id']} reached its trigger but was blocked: {rejection}.")
            return
        if ticket.execution == RESTING:
            done, text = self._place_resting(ticket, quote, "plan", plan["session_id"], plan["id"])
            if done:
                self.db.execute("UPDATE plans SET status = 'working', note = NULL WHERE id = ?", (plan["id"],))
        else:
            done, text = self._execute(ticket, quote, "plan", plan["session_id"])
            if done:
                self._close_plan(plan["id"], "filled")
        self.db.event("info", "plan", f"Plan {plan['id']} triggered. {text}.")
        if not done:
            self._plan_retry_at[plan["id"]] = now + PLAN_RETRY_SECONDS

    # ---- Waking the agents ----

    def session_running(self) -> bool:
        return self._session_thread is not None and self._session_thread.is_alive()

    def _wake_reason(self, quotes: dict[str, Quote], now: float) -> str | None:
        # Decision sessions exist to trade, so systematic mode never wakes one. The desk may still talk if switched on.
        if self.runner is None or self.systematic_mode or self.session_running() or self.db.get("halted") == "1":
            return None
        blocker = self.budget.blocker()
        if blocker:
            if self.db.get("budget_note_day") != str(day_start(now)):
                self.db.set("budget_note_day", day_start(now))
                self.db.event("warn", "budget", f"Agents are asleep: {blocker}. Stop-losses keep running.")
            return None
        if self.db.get("wake_requested") == "1":
            self.db.clear("wake_requested")
            return "Requested from the dashboard"
        next_check_in = self.db.get("next_check_in")
        if next_check_in is None:
            return "First session"

        last_end = float(self.db.get("last_session_end", "0"))
        if now - last_end >= self.config.agents.min_check_in_minutes * 60:
            flagged = self.db.get("escalation")
            if flagged:
                self.db.clear("escalation")
                limit = self.config.agents.max_decisions_per_day
                if self.decisions_today(now) < limit:
                    return f"The desk flagged: {flagged}"
                self.db.event("info", "desk", f"The desk flagged something, but today's {limit} decision sessions are used: {flagged}")
            exit_hit = self.db.get("exit_hit")
            if exit_hit:
                self.db.clear("exit_hit")
                return exit_hit
            for pair in self.db.get("last_session_prices", "").split(","):
                if ":" not in pair:
                    continue
                asset, price = pair.split(":")
                if asset in quotes and D(price) > 0:
                    move = (quotes[asset].mid - D(price)) / D(price) * 100
                    if abs(move) >= self.config.agents.wake_on_move_pct:
                        return f"{asset} moved {move:+.1f}% since the last session"
        if now >= float(next_check_in):
            return "Scheduled check-in"
        return None

    def decisions_today(self, now: float) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sessions WHERE kind = 'decision' AND started_at >= ?",
                           (day_start(now),))["n"]

    def next_discussion(self) -> float | None:
        """When the desk talks next, or None if it can't: switched off, no key, halted or out of budget."""
        minutes = self.config.agents.discussion_minutes
        if self.runner is None or minutes <= 0 or self.db.get("halted") == "1" or self.budget.blocker():
            return None
        return float(self.db.get("last_discussion", "0")) + minutes * 60

    def _maybe_wake(self, quotes: dict[str, Quote], now: float) -> None:
        reason = self._wake_reason(quotes, now)
        if reason:
            self._session_thread = threading.Thread(target=self._run_session, args=(reason,), daemon=True, name="session")
            self._session_thread.start()
            return
        due = self.next_discussion()
        if due is not None and now >= due and not self.session_running():
            self._session_thread = threading.Thread(target=self._run_discussion, daemon=True, name="discussion")
            self._session_thread.start()

    def _run_discussion(self) -> None:
        session_id = self.db.execute(
            "INSERT INTO sessions(started_at, trigger, kind) VALUES(?, 'Desk discussion', 'discussion')", (time.time(),)
        ).lastrowid
        self.db.set("session_running", "1")
        outcome, summary = "failed", None
        try:
            self.market.refresh()
            result = self.runner.run_discussion(session_id, self.build_discussion_brief())
            outcome, summary = ("flagged" if result.escalate else "discussed"), result.summary
            if result.escalate and self.systematic_mode:
                # No decision session runs in this mode, so nothing is asked for and nothing waits on the flag.
                self.db.event("info", "desk", f"The desk flagged: {result.escalate.rstrip(' .')}. The agents can't trade in "
                                              "systematic mode, so nothing acts on it.")
            elif result.escalate:
                self.db.set("escalation", result.escalate)
                self.db.event("info", "desk", f"The desk asked for a decision session: {result.escalate}")
            self.db.clear("agents_error")
        except BudgetExceeded as error:
            outcome, summary = "budget", str(error)
            self.runner.log_system(session_id, f"Discussion stopped: {error}.")
        except Exception as error:  # A failed discussion costs nothing but the conversation.
            summary = str(error) if isinstance(error, AgentFailed) else f"{type(error).__name__}: {error}"
            self.db.set("agents_error", summary)
            self.db.event("warn", "desk", f"Discussion {session_id} failed: {summary}")
            self.runner.log_system(session_id, f"Discussion failed: {summary}")
        finally:
            now = time.time()
            self.db.execute("UPDATE sessions SET ended_at = ?, outcome = ?, summary = ? WHERE id = ?",
                            (now, outcome, summary, session_id))
            self.db.set("last_discussion", now)
            self.db.set("session_running", "0")
            self.kick()

    def _finish_session(self, session_id: int, outcome: str, summary: str | None, next_in_seconds: float) -> None:
        now = time.time()
        self.db.execute(
            "UPDATE sessions SET ended_at = ?, outcome = ?, summary = ? WHERE id = ?",
            (now, outcome, summary, session_id),
        )
        self.db.set("next_check_in", now + next_in_seconds)
        self.db.set("last_session_end", now)
        self.db.set("last_session_prices", ",".join(f"{a}:{q.mid}" for a, q in self.market.quotes.items()))
        self.db.set("last_discussion", now)  # Everyone has just talked, so the desk can wait its turn.
        self.db.set("session_running", "0")

    def _run_session(self, trigger: str) -> None:
        session_id = self.db.execute(
            "INSERT INTO sessions(started_at, trigger) VALUES(?, ?)", (time.time(), trigger)
        ).lastrowid
        self.db.set("session_running", "1")
        self.db.clear("exit_hit")
        try:
            self.market.refresh()
            portfolio = self.portfolio()
            must_review = (
                self._strategy_text() is None
                or any(p.qty > 0 for p in portfolio.positions.values())
                or bool(self.waiting_plans())
                or bool(self.open_resting())
            )
            result = self.runner.run(session_id, self.build_brief(trigger), must_review, self._check_proposal)
            self._apply(session_id, result)
            self.db.clear("agents_error")
        except BudgetExceeded as error:
            self.runner.log_system(session_id, f"Session stopped without trading: {error}.")
            self._finish_session(session_id, "budget", str(error), FAILURE_BACKOFF_SECONDS)
        except Exception as error:  # Any failure ends the session without trading.
            text = str(error) if isinstance(error, AgentFailed) else f"{type(error).__name__}: {error}"
            self.db.set("agents_error", text)
            self.db.event("warn", "agents", f"Session {session_id} failed: {text}")
            self.runner.log_system(session_id, f"Session failed, so nothing was traded: {text}")
            self._finish_session(session_id, "failed", text, FAILURE_BACKOFF_SECONDS)
        finally:
            self.db.set("session_running", "0")
            self.kick()

    def _check_proposal(self, proposal: Proposal) -> tuple[list[str | None], list[str | None]]:
        now = time.time()
        quotes, portfolio, ctx = self.market.quotes, self.portfolio(), self.risk_context(now)
        waiting = {plan["id"] for plan in self.waiting_plans()}
        ctx = replace(ctx, active_plans=len(waiting - set(proposal.cancel_plans)))
        checks = self.gate.precheck(proposal.orders, portfolio, quotes, ctx)
        stop_checks: list[str | None] = []
        for asset, stop, target in proposal.stop_updates:
            try:
                self.gate.check_stop_update(asset, stop, portfolio, quotes, ctx, target)
                stop_checks.append(None)
            except Rejected as rejection:
                stop_checks.append(str(rejection))
        return checks, stop_checks

    def _apply(self, session_id: int, result: SessionResult) -> None:
        """Carries out what the agents agreed. Every order is checked again here, at the latest prices."""
        notes = []
        traded = False
        if result.outcome == "approved" and self.systematic_mode:
            notes.append(f"Nothing applied: {SYSTEMATIC_REFUSAL}")
            self.db.event("warn", "limits", f"Session {session_id}'s approved proposal was refused: {SYSTEMATIC_REFUSAL}")
        elif result.outcome == "approved":
            if result.strategy:
                self.db.execute("INSERT INTO strategy(ts, session_id, text) VALUES(?, ?, ?)",
                                (time.time(), session_id, result.strategy))
                notes.append("Strategy updated.")
            quotes = self.market.refresh() if result.orders else self.market.quotes
            with self._order_lock:
                for plan_id in result.cancel_plans:
                    if self._cancel_plan(plan_id, "Cancelled by the agents"):
                        notes.append(f"Plan {plan_id} cancelled.")
                    else:
                        notes.append(f"Plan {plan_id} couldn't be cancelled: it's already closed or doesn't exist.")
                for order in sorted(result.orders, key=lambda o: o.side != "sell"):
                    if order.trigger_price is not None:
                        notes.append(self._create_plan(order, session_id, quotes))
                        continue
                    # Anything already waiting on the asset would be working against this sell, or hold the coins it needs.
                    if order.side == "sell" and order.asset in quotes and not self._cancel_asset_orders(order.asset, quotes[order.asset]):
                        notes.append(f"Blocked sell {order.asset}: an open order couldn't be cancelled first.")
                        continue
                    try:
                        ticket = self.gate.size(order, self.portfolio(), quotes, self.risk_context(time.time()))
                    except Rejected as rejection:
                        notes.append(f"Blocked {order.side} {order.asset}: {rejection}.")
                        self.db.event("info", "limits", notes[-1])
                        continue
                    if ticket.execution == RESTING:
                        _, text = self._place_resting(ticket, quotes[order.asset], "agents", session_id)
                    else:
                        filled, text = self._execute(ticket, quotes[order.asset], "agents", session_id)
                        traded = traded or filled
                    notes.append(f"{text}.")
                for asset, stop, target in result.stop_updates:
                    try:
                        self.gate.check_stop_update(asset, stop, self.portfolio(), quotes, self.risk_context(time.time()), target)
                    except Rejected as rejection:
                        notes.append(f"Blocked the {asset} stop change: {rejection}.")
                        continue
                    self._set_stop(asset, stop, session_id, target)
                    notes.append(f"{asset} stop moved to {stop}" + (f", target to {target}." if target is not None else "."))

        agents = self.config.agents
        minutes = max(agents.min_check_in_minutes, min(result.next_check_in_minutes, agents.max_check_in_minutes))
        hours = f"{minutes / 60:.1f}".removesuffix(".0")
        notes.append(f"Next check-in in {hours}h unless a price move, stop or target wakes them sooner.")
        self.runner.log_system(session_id, " ".join(notes))
        outcome = "traded" if traded else result.outcome
        self._finish_session(session_id, outcome, result.summary, minutes * 60)

    def _create_plan(self, order: Order, session_id: int, quotes: dict[str, Quote]) -> str:
        try:
            direction = self.gate.check_plan(order, self.portfolio(), quotes, self.risk_context(time.time()))
        except Rejected as rejection:
            text = f"Blocked plan to buy {order.asset}: {rejection}."
            self.db.event("info", "limits", text)
            return text
        limit = self.config.risk.max_plan_hours
        hours = limit if order.trigger_hours is None else max(1, min(order.trigger_hours, limit))
        now = time.time()
        plan_id = self.db.execute(
            "INSERT INTO plans(created_at, session_id, asset, quote_amount, trigger_price, direction, execution, "
            "stop_price, target_price, expires_at, status, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (now, session_id, order.asset, str(order.quote_amount), str(order.trigger_price), direction, order.execution,
             str(order.stop_price), str(order.target_price), now + hours * 3600, order.reason),
        ).lastrowid
        verb = "falls to" if direction == "below" else "rises to"
        return f"Plan {plan_id}: buy {order.quote_amount} of {order.asset} if the price {verb} {order.trigger_price} ({order.execution}), for up to {hours} hours."

    # ---- The briefing ----

    def _strategy_text(self) -> str | None:
        row = self.db.one("SELECT text FROM strategy ORDER BY id DESC LIMIT 1")
        return row["text"] if row else None

    def build_brief(self, trigger: str) -> str:
        now = time.time()
        metrics = self.metrics()
        if metrics is None:
            raise AgentFailed("no prices yet")
        config, risk, q = self.config, self.config.risk, self.config.quote
        portfolio: Portfolio = metrics["portfolio"]
        marks = metrics["marks"]
        ctx = self.risk_context(now)
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        exits = {r["asset"]: r for r in self.db.all("SELECT asset, stop_price, target_price FROM stops")}

        lines = [
            f"Session start: {stamp}. Woken because: {trigger}.",
            f"Mode: {config.mode}" + (" (simulated fills at live prices)." if config.mode == "paper" else " (real money)."),
            "",
            f"PORTFOLIO ({q})",
            f"Equity {metrics['equity']:.2f}, cash {metrics['cash']:.2f}, started with {metrics['starting']:.2f}.",
            f"Realized {metrics['realized']:+.2f}, unrealized {metrics['unrealized']:+.2f}, fees paid {metrics['fees']:.2f}, API costs {metrics['api_cost']:.2f}.",
            f"Net P&L {metrics['net_pnl']:+.2f} ({metrics['net_pnl_pct']:+.2f}%). Buy-and-hold BTC would be {metrics['benchmark_pnl']:+.2f}. You are {metrics['vs_benchmark']:+.2f} against it.",
        ]
        held = [(a, p) for a, p in portfolio.positions.items() if p.qty > 0]
        for asset, position in held:
            value = position.qty * marks[asset]
            lines.append(
                f"Holding {position.qty.normalize():f} {asset}: worth {value:.2f}, average cost {position.avg_cost:.2f}, "
                f"unrealized {value - position.cost:+.2f}, stop {exits[asset]['stop_price'] if asset in exits else 'none'}, "
                f"target {(exits[asset]['target_price'] or 'none') if asset in exits else 'none'}."
            )
        if not held:
            lines.append("No open positions.")

        lines += [
            "",
            "LIMITS (enforced by code)",
            f"Each buy: up to {risk.max_order_pct}% of equity, with a stop {risk.min_stop_distance_pct}% to {risk.max_stop_distance_pct}% below the price "
            f"and a target at least {self.gate.target_hurdle_pct(RESTING):.1f}% above entry for a resting buy, or {self.gate.target_hurdle_pct('immediate'):.1f}% for an immediate one. Code exits at either one.",
            f"Each asset: up to {risk.max_asset_pct}% of equity. Cash after any buy: at least {risk.min_cash_pct}% of equity.",
            f"Buys today: {ctx.buys_today} of {risk.max_buys_per_day}. Buys pause for the day after a {risk.daily_loss_pause_pct}% daily loss" + (" (paused now)." if ctx.buys_paused else "."),
            f"Agent trading halts at a {risk.drawdown_halt_pct}% drawdown from peak equity.",
            f"Fees: resting orders pay {config.maker_pct}% and may not fill; immediate orders pay {config.taker_pct}%. Fees in the last 30 days: {ctx.fees_30d:.2f} of a budget of {metrics['equity'] * risk.max_fees_30d_pct / 100:.2f}, counting the sell each new buy will need.",
            f"Plans: {ctx.active_plans} of {risk.max_active_plans} waiting. Each can wait up to {risk.max_plan_hours} hours, with a trigger within {risk.max_trigger_distance_pct}% of the price.",
            f"API budget left: ${max(config.agents.daily_budget_usd - self.budget.today(), 0):.2f} today, ${max(config.agents.monthly_budget_usd - self.budget.this_month(), 0):.2f} this month.",
            f"Next check-in can be {config.agents.min_check_in_minutes} to {config.agents.max_check_in_minutes} minutes away. A {config.agents.wake_on_move_pct}% move in any price wakes you sooner.",
            "",
            f"MARKET ({q}, computed from Kraken data)",
        ]
        for asset in self.market.tracked:
            try:
                s = self.market.summary(asset)
            except Exception as error:
                lines.append(f"{asset}: couldn't load history ({error}).")
                continue
            lines.append(f"{asset}: price {s['price']:.2f}, spread {s['spread_pct']:.3f}%, " + ", ".join(
                f"{label} {s[key]:+.2f}%" for label, key in (
                    ("1h", "change_1h_pct"), ("24h", "change_24h_pct"), ("7d", "change_7d_pct"), ("30d", "change_30d_pct")
                ) if s[key] is not None
            ) + ", " + ", ".join(
                f"{label} {s[key]:.2f}" for label, key in (
                    ("24h volatility %", "volatility_24h_pct"), ("20d avg", "sma_20d"), ("50d avg", "sma_50d"),
                    ("30d high", "high_30d"), ("30d low", "low_30d")
                ) if s[key] is not None
            ) + ".")

        lines += self._brief_orders(now)
        strategy = self._strategy_text()
        lines += ["", "CURRENT STRATEGY", strategy or "None yet. This is the first session, so draft one in strategy_update."]

        journal = self.db.all(
            "SELECT started_at, outcome, summary FROM sessions WHERE summary IS NOT NULL AND kind = 'decision' ORDER BY id DESC LIMIT ?",
            (JOURNAL_ENTRIES,),
        )
        if journal:
            lines += ["", "YOUR JOURNAL (newest first)"]
            for row in journal:
                when = datetime.fromtimestamp(row["started_at"], timezone.utc).strftime("%m-%d %H:%M")
                lines.append(f"{when} [{row['outcome']}] {row['summary']}")

        fills = self.db.all("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (RECENT_FILLS,))
        if fills:
            lines += ["", "RECENT TRADES (newest first)"]
            for row in fills:
                when = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%m-%d %H:%M")
                lines.append(
                    f"{when} {row['side']} {D(row['qty']).normalize():f} {row['asset']} at {D(row['price']):.2f}, "
                    f"fee {D(row['fee']):.2f}, realized {D(row['realized']):+.2f} [{row['source']}]"
                )
        lines += self._brief_desk()
        return "\n".join(lines)

    def build_discussion_brief(self) -> str:
        return self.build_brief("the regular desk discussion")

    def _brief_desk(self) -> list[str]:
        notes = self.db.all("SELECT ts, speaker, text FROM research_notes ORDER BY id DESC LIMIT ?", (DESK_NOTES_IN_BRIEF,))
        messages = self.db.all(
            "SELECT m.ts, m.speaker, m.text FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.kind = 'discussion' AND m.speaker != 'system' ORDER BY m.id DESC LIMIT ?",
            (DESK_MESSAGES_IN_BRIEF,),
        )
        lines = []
        if notes:
            lines += ["", "DESK RESEARCH NOTES (oldest first)"]
            for row in reversed(notes):
                when = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%m-%d %H:%M")
                lines.append(f"{when} {row['text'][:BRIEF_TEXT_LIMIT]}")
        if messages:
            lines += ["", "LATEST DESK CONVERSATION (oldest first)"]
            for row in reversed(messages):
                when = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%H:%M")
                speaker = "Strategist" if row["speaker"] == "strategist" else "Risk Manager"
                lines.append(f"{when} {speaker}: {row['text'][:BRIEF_TEXT_LIMIT]}")
        return lines

    def _brief_orders(self, now: float) -> list[str]:
        lines = ["", "PLANS AND OPEN ORDERS"]
        plans, resting = self.waiting_plans(), self.open_resting()
        for plan in plans:
            verb = "falls to" if plan["direction"] == "below" else "rises to"
            line = (f"Plan {plan['id']}: buy {plan['quote_amount']} of {plan['asset']} if the price {verb} {plan['trigger_price']}, "
                    f"{plan['execution']}, stop {plan['stop_price']}, target {plan['target_price']}, "
                    f"{(plan['expires_at'] - now) / 3600:.1f} hours left.")
            if plan["status"] == "working":
                line += " Triggered: its order is in the book now."
            elif plan["note"]:
                line += f" Blocked when it triggered: {plan['note']}."
            lines.append(line)
        for order in resting:
            lines.append(
                f"Resting {order['side']} of {order['qty']} {order['asset']} at {order['limit_price']}, "
                f"{D(order['booked_qty']).normalize():f} filled, {max((order['expires_at'] - now) / 60, 0):.0f} minutes left."
            )
        if not plans and not resting:
            lines.append("Nothing is waiting.")
        stats = self.resting_stats()
        if stats["closed"]:
            lines.append(f"Resting orders so far: {stats['filled']} of {stats['closed']} filled, saving {stats['fees_saved']:.2f} in fees.")
        missed = self.db.all(
            "SELECT side, asset, limit_price, source FROM resting_orders WHERE status = 'missed' AND closed_at >= ? ORDER BY id DESC LIMIT 5",
            (now - 86400,),
        )
        for order in missed:
            lines.append(f"Missed in the last day: resting {order['side']} of {order['asset']} at {order['limit_price']} ({order['source']}).")
        return lines
