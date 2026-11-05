"""The systematic runner's hard limits, and the one function every new order goes through.

Each control has its own config value in [systematic], is logged pass or fail with its value and threshold, and fails
closed: a missing or non-finite input is a fail, never a pass.

    kill_switch     equity this far below its peak: cancel, flatten, halt until the user resumes
    daily_loss      equity this far below the UTC day's start: no buys until tomorrow, sells allowed
    stale_data      the just-closed daily bar missing too long after 00:00, or too many API errors in a row
    quote_fresh     the asset's Kraken quote older than its limit, frozen, or not a sane book
    asset_limit     an asset above the strategy's largest sleeve + margin, capped at max_asset_pct
    gross_exposure  holdings above max_gross_pct of equity, a sell of more than is held, or a buy past the cash
    order_cap       a buy bigger than its target change + margin of equity, or a sell bigger than what its order set
                    out to sell (a sell's size is fixed in quantity when its order is made, so a move in the price
                    while it works can't block the exit; gross_exposure stops it selling more than is held)

OrderGate holds the broker privately. The runner never sees the broker, so the only way to place an order is
OrderGate.place, which runs every check first. Cancels and fill checks pass straight through, because they can only
reduce risk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from .brokers import Fill, OrderRejected, RestingStatus
from .config import SystematicConfig
from .market import Quote
from .risk import RESTING, IMMEDIATE, Ticket

# How long after the loop's clock a quote may be stamped and still be fresh. A tick fixes its now when it starts, and
# its own calls (retries, timeouts, decide's second refresh) can take minutes. The workflow's Tick step stops a tick
# before 300 s, so a quote stamped later than that is a clock problem.
FUTURE_TOLERANCE_SECONDS = 300
CONTROLS = ("kill_switch","daily_loss", "stale_data", "quote_fresh", "asset_limit", "gross_exposure", "order_cap")


@dataclass(frozen=True)
class Check:
    name: str
    value: float | None
    threshold: float | None
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"control": self.name, "value": self.value, "threshold": self.threshold,
                "outcome": "pass" if self.passed else "fail", "detail": self.detail}


def finite(value) -> bool:
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _f(value) -> float | None:
    return float(value) if finite(value) else None


@dataclass(frozen=True)
class Halts:
    """The portfolio-level state the runner keeps. None means unknown, which fails closed."""
    kill: bool = False
    kill_reason: str = ""
    daily_loss: bool | None = False
    signal_stale: bool | None = False
    api_errors: int | None = 0
    drawdown_pct: float | None = None   # The last loop's readings, so each order's log carries them too.
    day_loss_pct: float | None = None


@dataclass(frozen=True)
class Child:
    """One order the runner wants placed."""
    order_id: int
    asset: str
    side: str
    qty: Decimal
    limit: Decimal
    kind: str                       # maker (post-only at the best price) or taker (marketable, capped)
    source: str                     # decision, dca or kill_switch
    target_change: Decimal | None   # abs(target weight - weight now), as a share of equity
    reason: str = ""
    qty_cap: Decimal | None = None  # A sell: what its order still has to sell, the most this child may be.


@dataclass(frozen=True)
class Book:
    """The account as the gate sees it. Quantities are the ledger's, marks are Kraken mids."""
    now: float
    equity: Decimal | None
    cash: Decimal | None
    held: dict = field(default_factory=dict)            # asset -> qty
    marks: dict = field(default_factory=dict)           # asset -> mid
    open_buy_qty: dict = field(default_factory=dict)    # asset -> qty still resting on buys
    open_buy_cash: Decimal = Decimal(0)                 # cash those buys could take, fees included
    open_sell_qty: dict = field(default_factory=dict)   # asset -> qty still resting on sells
    asset_limit: dict = field(default_factory=dict)     # asset -> share of equity
    quotes: dict = field(default_factory=dict)          # asset -> Quote
    frozen: dict = field(default_factory=dict)          # asset -> seconds unchanged
    halts: Halts = Halts()


class Controls:
    def __init__(self, config: SystematicConfig):
        self.c = config
        self.maker_rate = config.maker_pct / 100
        self.taker_rate = config.taker_pct / 100

    # ---- Portfolio level, evaluated on every loop and logged on every decision ----

    def drawdown(self, equity, peak) -> Check:
        threshold = float(self.c.kill_drawdown_pct)
        if not (finite(equity) and finite(peak)) or float(peak) <= 0:
            return Check("kill_switch", None, threshold, False, "equity or peak unknown")
        value = max(0.0, (float(peak) - float(equity)) / float(peak) * 100)
        return Check("kill_switch", value, threshold, value < threshold, f"peak {float(peak):.2f}")

    def day_loss(self, equity, day_start) -> Check:
        threshold = float(self.c.daily_loss_pct)
        if not (finite(equity) and finite(day_start)) or float(day_start) <= 0:
            return Check("daily_loss", None, threshold, False, "equity or the day's start unknown")
        value = max(0.0, (float(day_start) - float(equity)) / float(day_start) * 100)
        return Check("daily_loss", value, threshold, value < threshold, f"day start {float(day_start):.2f}")

    def staleness(self, halts: Halts, bar_missing_minutes) -> Check:
        problems = []
        if halts.signal_stale is None or halts.signal_stale:
            problems.append("the just-closed daily bar is missing" if halts.signal_stale else "signal freshness unknown")
        if halts.api_errors is None or halts.api_errors >= self.c.max_api_errors:
            problems.append(f"{halts.api_errors} API errors in a row")
        return Check("stale_data", _f(bar_missing_minutes), float(self.c.signal_stale_minutes), not problems,
                     "; ".join(problems) or f"API errors in a row: {halts.api_errors}")

    def holdings(self, book: Book) -> list[Check]:
        """Current per-asset and gross exposure, against their limits."""
        if not finite(book.equity) or book.equity <= 0:
            return [Check("asset_limit", None, None, False, "equity unknown"),
                    Check("gross_exposure", None, float(self.c.max_gross_pct), False, "equity unknown")]
        worst, worst_asset, gross = 0.0, "", 0.0
        for asset, qty in book.held.items():
            if qty <= 0:
                continue
            mark = book.marks.get(asset)
            if not finite(mark):
                return [Check("asset_limit", None, None, False, f"no price for {asset}"),
                        Check("gross_exposure", None, float(self.c.max_gross_pct), False, f"no price for {asset}")]
            share = float(qty * mark / book.equity * 100)
            gross += share
            limit = book.asset_limit.get(asset)
            over = share - float(limit) * 100 if finite(limit) else share
            if over > worst or not worst_asset:
                worst, worst_asset = over, asset
        cap = float(self.c.max_asset_pct)
        largest = max((float(q * book.marks[a] / book.equity * 100) for a, q in book.held.items() if q > 0), default=0.0)
        return [
            Check("asset_limit", largest, cap, worst <= 1e-9 or not worst_asset,
                  f"largest holding {largest:.2f}% of equity" + (f"; {worst_asset} over its limit by {worst:.2f} points"
                                                                 if worst > 1e-9 and worst_asset else "")),
            Check("gross_exposure", gross, float(self.c.max_gross_pct), gross <= float(self.c.max_gross_pct) + 1e-9,
                  "no shorts or leverage: the ledger can't hold either"),
        ]

    # ---- Order level: every check, every order ----

    def check_order(self, child: Child, book: Book) -> list[Check]:
        c = self.c
        checks = []
        h = book.halts
        checks.append(Check("kill_switch", h.drawdown_pct, float(c.kill_drawdown_pct),
                            not h.kill or child.source == "kill_switch",
                            f"halted: {h.kill_reason}" if h.kill else ""))
        stale = (h.signal_stale is None or h.signal_stale or h.api_errors is None or h.api_errors >= c.max_api_errors)
        # Flattening after the kill switch doesn't need the daily signal, only a sane quote.
        checks.append(Check("stale_data", _f(h.api_errors), float(c.max_api_errors),
                            not stale or child.source == "kill_switch",
                            "signal data stale or unknown, or too many API errors" if stale else ""))
        checks.append(self._quote_check(child, book))
        daily = h.daily_loss is None or h.daily_loss
        checks.append(Check("daily_loss", h.day_loss_pct, float(c.daily_loss_pct), child.side == "sell" or not daily,
                            "no buys for the rest of the UTC day" if daily else ""))
        checks += self._size_checks(child, book)
        return checks

    def _quote_check(self, child: Child, book: Book) -> Check:
        limit = float(self.c.max_quote_age_seconds)
        quote: Quote | None = book.quotes.get(child.asset)
        if quote is None or not (finite(quote.bid) and finite(quote.ask) and finite(quote.ts)):
            return Check("quote_fresh", None, limit, False, f"no usable quote for {child.asset}")
        age = book.now - float(quote.ts)
        if quote.bid <= 0 or quote.ask < quote.bid:
            return Check("quote_fresh", age, limit, False, f"quote looks wrong: bid {quote.bid}, ask {quote.ask}")
        frozen = book.frozen.get(child.asset)
        if not finite(frozen):
            return Check("quote_fresh", age, limit, False, "can't tell how long the quote has been unchanged")
        if frozen > self.c.max_frozen_quote_seconds:
            return Check("quote_fresh", age, limit, False,
                         f"quote unchanged for {frozen:.0f}s, over {self.c.max_frozen_quote_seconds}s: frozen feed?")
        # A quote fetched during this loop is stamped after the loop's own clock, so its age is a little negative (up to
        # minutes in a scheduled run). That's fresh. One from well in the future means a clock problem, which fails.
        fresh = -FUTURE_TOLERANCE_SECONDS <= age <= limit
        return Check("quote_fresh", max(age, 0.0), limit, fresh,
                     f"quote {max(age, 0.0):.0f}s old" if age >= -FUTURE_TOLERANCE_SECONDS
                     else f"quote stamped {-age:.0f}s after this loop: clock problem")

    def _size_checks(self, child: Child, book: Book) -> list[Check]:
        if child.side == "sell":
            return self._sell_size_checks(child, book)
        c = self.c
        names = ("asset_limit", "gross_exposure", "order_cap")
        equity, cash = book.equity, book.cash
        mark = book.marks.get(child.asset)
        if not (finite(equity) and finite(cash) and finite(mark) and finite(child.qty) and finite(child.limit)):
            return [Check(name, None, None, False, "missing or non-finite input") for name in names]
        if equity <= 0 or child.qty <= 0 or child.limit <= 0 or mark <= 0:
            return [Check(name, None, None, False, "equity, size or price not above 0") for name in names]
        out = []
        held = book.held.get(child.asset, Decimal(0))
        notional = child.qty * child.limit
        limit = book.asset_limit.get(child.asset)
        pending = book.open_buy_qty.get(child.asset, Decimal(0))
        after = (held + pending + child.qty) * mark / equity
        if not finite(limit):
            out.append(Check("asset_limit", float(after * 100), None, False, f"no limit known for {child.asset}"))
        else:
            out.append(Check("asset_limit", float(after * 100), float(limit) * 100,
                             after <= Decimal(str(limit)) + Decimal("1e-9"), f"{child.asset} after this buy"))
        rate = self.maker_rate if child.kind == "maker" else self.taker_rate
        cost = notional * (1 + rate)
        room = cash - book.open_buy_cash
        value = sum((q * book.marks[a] for a, q in book.held.items() if q > 0 and a in book.marks), Decimal(0))
        pending_value = sum((q * book.marks.get(a, Decimal(0)) for a, q in book.open_buy_qty.items()), Decimal(0))
        gross = (value + pending_value + child.qty * mark) / equity
        ok = cost <= room and gross * 100 <= c.max_gross_pct + Decimal("1e-9")
        out.append(Check("gross_exposure", float(gross * 100), float(c.max_gross_pct), ok,
                         "" if cost <= room else f"needs {cost:.2f} with {room:.2f} of cash free: no leverage"))
        if not finite(child.target_change):
            out.append(Check("order_cap", None, None, False, "target change unknown"))
        else:
            cap = (Decimal(str(child.target_change)) + c.order_margin_pct / 100) * equity
            out.append(Check("order_cap", float(notional / equity * 100), float(cap / equity * 100), notional <= cap,
                             f"order {notional:.2f}, cap {cap:.2f}"))
        return out

    def _sell_size_checks(self, child: Child, book: Book) -> list[Check]:
        """A sell needs only its own size and price and what's held: equity isn't an input to any of its checks, so a
        flatten still runs when another holding has no price.

        Its cap is its order's own quantity, fixed from the target change when the order was made. Re-reading it as a
        share of today's equity would block a sale to 0 or a kill-switch flatten whenever the coin rose against the
        rest of the book while the order worked, so the one order meant to cut risk would be the one stopped.
        """
        names = ("asset_limit", "gross_exposure", "order_cap")
        if not (finite(child.qty) and finite(child.limit)):
            return [Check(name, None, None, False, "missing or non-finite input") for name in names]
        if child.qty <= 0 or child.limit <= 0:
            return [Check(name, None, None, False, "size or price not above 0") for name in names]
        held = book.held.get(child.asset, Decimal(0))
        selling = book.open_sell_qty.get(child.asset, Decimal(0)) + child.qty
        out = [Check("asset_limit", None, None, True, "sells only reduce holdings"),
               Check("gross_exposure", float(selling), float(held), selling <= held,
                     "" if selling <= held else f"would sell {selling} with {held} held: no shorts")]
        if finite(child.qty_cap):
            cap = Decimal(str(child.qty_cap))
            out.append(Check("order_cap", float(child.qty), float(cap), child.qty <= cap,
                             f"sell {child.qty} of the {cap} its order still has to sell"))
        elif finite(child.target_change) and finite(book.equity) and book.equity > 0:
            cap = (Decimal(str(child.target_change)) + self.c.order_margin_pct / 100) * book.equity
            notional = child.qty * child.limit
            out.append(Check("order_cap", float(notional / book.equity * 100), float(cap / book.equity * 100),
                             notional <= cap, f"order {notional:.2f}, cap {cap:.2f}"))
        else:
            out.append(Check("order_cap", None, None, False, "neither the order's quantity nor its target change known"))
        return out


@dataclass(frozen=True)
class Placed:
    ok: bool
    checks: list
    venue_id: str | None = None
    fill: Fill | None = None
    error: str = ""


class OrderGate:
    """The only way an order reaches the broker. Every check runs first, on every order."""

    def __init__(self, broker, controls: Controls, ttl_seconds: int):
        self.__broker = broker
        self.controls = controls
        self.ttl_seconds = ttl_seconds

    def place(self, child: Child, book: Book) -> Placed:
        checks = self.controls.check_order(child, book)
        if not all(check.passed for check in checks):
            return Placed(False, checks, error="; ".join(f"{k.name}: {k.detail}" for k in checks if not k.passed))
        quote = book.quotes[child.asset]
        ticket = Ticket(child.asset, child.side, child.qty, child.limit, reason=child.reason,
                        execution=RESTING if child.kind == "maker" else IMMEDIATE)
        try:
            if child.kind == "maker":
                return Placed(True, checks, venue_id=self.__broker.place_resting(ticket, quote, self.ttl_seconds))
            return Placed(True, checks, fill=self.__broker.execute(ticket, quote))
        except OrderRejected as error:
            return Placed(False, checks, error=f"rejected: {error}")

    def check(self, order: dict, quote: Quote) -> RestingStatus | None:
        return self.__broker.check_resting(order, quote)

    def cancel(self, order: dict, quote: Quote | None) -> RestingStatus | None:
        if quote is None:
            return None
        return self.__broker.cancel_resting(order, quote)
