"""The hard limits. Everything the agents ask for passes through here, and nothing here reads their reasoning."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .config import RiskConfig
from .ledger import Portfolio
from .market import PairInfo, Quote

IMMEDIATE = "immediate"     # Crosses the book now and pays the taker fee.
RESTING = "resting"         # Waits in the book, pays the maker fee, and may not fill.


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class Order:
    """What an agent asks for."""
    asset: str
    side: str
    quote_amount: Decimal | None = None     # Buys: how much to spend.
    sell_fraction: Decimal | None = None    # Sells: how much of the position, 0 to 1.
    stop_price: Decimal | None = None       # Buys: required.
    reason: str = ""
    target_price: Decimal | None = None     # Buys: required, and has to clear the fees.
    execution: str = IMMEDIATE
    trigger_price: Decimal | None = None    # Buys: wait for this price before acting.
    trigger_hours: int | None = None


@dataclass(frozen=True)
class Ticket:
    """A sized order that passed every check and is ready for the broker."""
    asset: str
    side: str
    qty: Decimal
    limit_price: Decimal
    stop_price: Decimal | None = None
    reason: str = ""
    target_price: Decimal | None = None
    execution: str = IMMEDIATE


@dataclass(frozen=True)
class RiskContext:
    now: float
    halted: bool = False
    halt_reason: str = ""
    buys_paused: bool = False
    buys_today: int = 0
    fees_30d: Decimal = Decimal(0)
    reserved_cash: Decimal = Decimal(0)     # Spoken for by resting buys that haven't filled yet.
    reserved_qty: dict[str, Decimal] = field(default_factory=dict)
    active_plans: int = 0


def _round(value: Decimal, decimals: int, rounding) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)


def marks_from(quotes: dict[str, Quote]) -> dict[str, Decimal]:
    return {asset: quote.mid for asset, quote in quotes.items()}


class RiskGate:
    def __init__(self, config: RiskConfig, maker_pct: Decimal, taker_pct: Decimal,
                 assets: tuple[str, ...], pairs: dict[str, PairInfo]):
        self.config = config
        self.maker_rate = maker_pct / 100
        self.taker_rate = taker_pct / 100
        self.assets = assets
        self.pairs = pairs

    def entry_rate(self, execution: str) -> Decimal:
        return self.maker_rate if execution == RESTING else self.taker_rate

    def target_hurdle_pct(self, execution: str) -> Decimal:
        """The smallest allowed profit target. Exits at a target rest in the book, so they pay the maker fee."""
        return (self.entry_rate(execution) + self.maker_rate) * self.config.min_target_fee_multiple * 100

    def _check_basics(self, order: Order, ctx: RiskContext) -> PairInfo:
        if ctx.halted:
            raise Rejected(f"trading is halted: {ctx.halt_reason}")
        if order.asset not in self.assets:
            raise Rejected(f"{order.asset} isn't in the allowed list: {', '.join(self.assets)}")
        pair = self.pairs.get(order.asset)
        if pair is None or not pair.online:
            raise Rejected(f"{order.asset} isn't currently tradable on Kraken")
        if order.execution not in (IMMEDIATE, RESTING):
            raise Rejected(f"unknown execution {order.execution!r}")
        return pair

    def size(self, order: Order, portfolio: Portfolio, quotes: dict[str, Quote], ctx: RiskContext) -> Ticket:
        """Turns an order into a Ticket, or raises Rejected with the reason."""
        pair = self._check_basics(order, ctx)
        quote = self._fresh_quote(order.asset, quotes, ctx)
        if order.side == "buy":
            return self._size_buy(order, portfolio, quotes, quote, pair, ctx)
        if order.side == "sell":
            return self._size_sell(order, portfolio, quote, pair)
        raise Rejected(f"unknown side {order.side!r}")

    def _fresh_quote(self, asset: str, quotes: dict[str, Quote], ctx: RiskContext) -> Quote:
        quote = quotes.get(asset)
        if quote is None:
            raise Rejected(f"no price for {asset}")
        if ctx.now - quote.ts > self.config.max_quote_age_seconds:
            raise Rejected(f"the {asset} price is stale")
        if quote.bid <= 0 or quote.ask < quote.bid:
            raise Rejected(f"the {asset} quote looks wrong (bid {quote.bid}, ask {quote.ask})")
        return quote

    def _check_amount(self, amount: Decimal | None, equity: Decimal) -> Decimal:
        if amount is None or not amount.is_finite() or amount <= 0:
            raise Rejected("a buy needs a positive quote_amount")
        largest = equity * self.config.max_order_pct / 100
        if amount > largest:
            raise Rejected(f"{amount:.2f} is over the {self.config.max_order_pct}% per-order limit ({largest:.2f})")
        return amount

    def _size_buy(self, order, portfolio, quotes, quote, pair, ctx) -> Ticket:
        c = self.config
        if ctx.buys_paused:
            raise Rejected(f"buys are paused until tomorrow (UTC): the {c.daily_loss_pause_pct}% daily loss limit was hit")
        if ctx.buys_today >= c.max_buys_per_day:
            raise Rejected(f"already made {ctx.buys_today} of {c.max_buys_per_day} allowed buys today")

        marks = marks_from(quotes)
        equity = portfolio.equity(marks)
        amount = self._check_amount(order.quote_amount, equity)
        resting = order.execution == RESTING
        rate = self.entry_rate(order.execution)
        entry = quote.bid if resting else quote.ask
        self.validate_stop(order.asset, order.stop_price, quote.bid)
        self.validate_target(order.asset, order.target_price, entry, order.execution)

        if resting:
            limit = _round(quote.bid, pair.pair_decimals, ROUND_DOWN)
            if limit >= quote.ask:
                raise Rejected(f"there's no room to rest a {order.asset} buy below the ask right now")
        else:
            limit = _round(quote.ask * (1 + c.max_limit_slippage_pct / 100), pair.pair_decimals, ROUND_DOWN)
        qty = _round(amount / entry, pair.lot_decimals, ROUND_DOWN)
        if qty < pair.ordermin or qty * entry < pair.costmin:
            raise Rejected(f"{amount:.2f} is below Kraken's minimum order for {order.asset}")

        # Checked at the worst price the order could fill at, fee included, after anything already spoken for.
        worst_cost = qty * limit * (1 + rate)
        floor = equity * c.min_cash_pct / 100
        if portfolio.cash - ctx.reserved_cash - worst_cost < floor:
            raise Rejected(f"would leave cash below the {c.min_cash_pct}% minimum ({floor:.2f})")
        pending = ctx.reserved_qty.get(order.asset, Decimal(0))
        exposure = (portfolio.qty(order.asset) + pending + qty) * quote.mid
        ceiling = equity * c.max_asset_pct / 100
        if exposure > ceiling:
            raise Rejected(f"{order.asset} would be {exposure:.2f}, over the {c.max_asset_pct}% limit ({ceiling:.2f})")
        # Assumes the eventual sell pays the taker fee, because a stop-loss would.
        round_trip_fees = qty * limit * (rate + self.taker_rate)
        budget = equity * c.max_fees_30d_pct / 100
        if ctx.fees_30d + round_trip_fees > budget:
            raise Rejected(
                f"fees over 30 days would reach {ctx.fees_30d + round_trip_fees:.2f}, "
                f"past the {c.max_fees_30d_pct}% budget ({budget:.2f})"
            )
        return Ticket(order.asset, "buy", qty, limit, order.stop_price, order.reason, order.target_price, order.execution)

    def _size_sell(self, order, portfolio, quote, pair) -> Ticket:
        fraction = order.sell_fraction
        if fraction is None or not fraction.is_finite() or not (0 < fraction <= 1):
            raise Rejected("a sell needs a sell_fraction above 0 and up to 1")
        held = portfolio.qty(order.asset)
        if held <= 0:
            raise Rejected(f"no {order.asset} held")
        qty = _round(held * fraction, pair.lot_decimals, ROUND_DOWN)
        if 0 < held - qty < pair.ordermin:
            qty = _round(held, pair.lot_decimals, ROUND_DOWN)  # Don't strand an unsellable remainder.
        if qty < pair.ordermin:
            raise Rejected(f"{qty} {order.asset} is below Kraken's minimum order")
        if order.execution == RESTING:
            limit = _round(quote.ask, pair.pair_decimals, ROUND_UP)
            if limit <= quote.bid:
                raise Rejected(f"there's no room to rest a {order.asset} sell above the bid right now")
        else:
            limit = _round(quote.bid * (1 - self.config.max_limit_slippage_pct / 100), pair.pair_decimals, ROUND_UP)
        return Ticket(order.asset, "sell", qty, limit, None, order.reason, None, order.execution)

    def validate_stop(self, asset: str, stop: Decimal | None, price: Decimal) -> None:
        if stop is None or not stop.is_finite() or stop <= 0:
            raise Rejected(f"{asset} needs a stop_price")
        if stop >= price:
            raise Rejected(f"the {asset} stop {stop} has to be below the price ({price})")
        distance = (price - stop) / price * 100
        c = self.config
        if not (c.min_stop_distance_pct <= distance <= c.max_stop_distance_pct):
            raise Rejected(
                f"the {asset} stop is {distance:.1f}% away; it has to be "
                f"{c.min_stop_distance_pct}% to {c.max_stop_distance_pct}% below the price"
            )

    def validate_target(self, asset: str, target: Decimal | None, entry: Decimal, execution: str) -> None:
        if target is None or not target.is_finite() or target <= 0:
            raise Rejected(f"{asset} needs a target_price")
        needed = self.target_hurdle_pct(execution)
        gain = (target - entry) / entry * 100
        if gain < needed:
            raise Rejected(
                f"the {asset} target is {gain:.1f}% above the entry price; "
                f"a {execution} buy needs at least {needed:.1f}% to justify the fees"
            )

    def check_stop_update(self, asset: str, stop: Decimal | None, portfolio: Portfolio,
                          quotes: dict[str, Quote], ctx: RiskContext, target: Decimal | None = None) -> None:
        if ctx.halted:
            raise Rejected(f"trading is halted: {ctx.halt_reason}")
        if portfolio.qty(asset) <= 0:
            raise Rejected(f"no {asset} held, so there's nothing to protect")
        quote = self._fresh_quote(asset, quotes, ctx)
        self.validate_stop(asset, stop, quote.bid)
        # The entry fee is already paid on a held position, so a new target only has to beat the price.
        if target is not None and (not target.is_finite() or target <= quote.bid):
            raise Rejected(f"the {asset} target {target} has to be above the price ({quote.bid})")

    def check_plan(self, order: Order, portfolio: Portfolio, quotes: dict[str, Quote], ctx: RiskContext) -> str:
        """Checks a price-triggered buy when it's proposed, and returns its direction: below or above.

        It's sized and checked again when it fires, at that moment's prices and balances.
        """
        pair = self._check_basics(order, ctx)
        c = self.config
        if order.side != "buy":
            raise Rejected("only buys can wait for a price; sells happen through the stop and target")
        if ctx.active_plans >= c.max_active_plans:
            raise Rejected(f"already {ctx.active_plans} of {c.max_active_plans} allowed plans waiting")
        trigger = order.trigger_price
        if trigger is None or not trigger.is_finite() or trigger <= 0:
            raise Rejected(f"{order.asset} needs a positive trigger_price")
        quote = self._fresh_quote(order.asset, quotes, ctx)
        distance = abs(trigger - quote.mid) / quote.mid * 100
        if distance > c.max_trigger_distance_pct:
            raise Rejected(f"the {order.asset} trigger is {distance:.1f}% away; the limit is {c.max_trigger_distance_pct}%")
        direction = "below" if trigger < quote.mid else "above"
        if direction == "above" and order.execution == RESTING:
            raise Rejected("a buy triggered above the price has to be immediate, because a resting order won't fill while the price is rising")
        amount = self._check_amount(order.quote_amount, portfolio.equity(marks_from(quotes)))
        self.validate_stop(order.asset, order.stop_price, trigger)
        self.validate_target(order.asset, order.target_price, trigger, order.execution)
        qty = amount / trigger
        if qty < pair.ordermin or amount < pair.costmin:
            raise Rejected(f"{amount:.2f} is below Kraken's minimum order for {order.asset}")
        return direction

    def exit_ticket(self, asset: str, portfolio: Portfolio, quote: Quote, reason: str,
                    slippage_pct: Decimal | None = None) -> Ticket | None:
        """Sell the whole position now, by default with more room on price. Runs even while halted."""
        pair = self.pairs[asset]
        qty = _round(portfolio.qty(asset), pair.lot_decimals, ROUND_DOWN)
        if qty <= 0 or qty < pair.ordermin:
            return None
        room = self.config.stop_limit_slippage_pct if slippage_pct is None else slippage_pct
        limit = _round(quote.bid * (1 - room / 100), pair.pair_decimals, ROUND_UP)
        return Ticket(asset, "sell", qty, limit, None, reason)

    def resting_exit_ticket(self, asset: str, portfolio: Portfolio, quote: Quote, reason: str) -> Ticket | None:
        """Offer the whole position at the ask, for exits that can afford to wait for the lower fee."""
        pair = self.pairs[asset]
        qty = _round(portfolio.qty(asset), pair.lot_decimals, ROUND_DOWN)
        if qty <= 0 or qty < pair.ordermin:
            return None
        limit = _round(quote.ask, pair.pair_decimals, ROUND_UP)
        if limit <= quote.bid:
            return None
        return Ticket(asset, "sell", qty, limit, None, reason, None, RESTING)

    def precheck(self, orders: list[Order], portfolio: Portfolio, quotes: dict[str, Quote],
                 ctx: RiskContext) -> list[str | None]:
        """Dry-runs a batch, sells first, so the agents hear about limit problems before asking to execute.

        Returns one entry per order, in the original order: None if it passes, else the reason.
        """
        simulated = portfolio.copy()
        buys, fees, plans = ctx.buys_today, ctx.fees_30d, ctx.active_plans
        results: list[str | None] = [None] * len(orders)
        for index in sorted(range(len(orders)), key=lambda i: orders[i].side != "sell"):
            order = orders[index]
            trial = replace(ctx, buys_today=buys, fees_30d=fees, active_plans=plans)
            try:
                if order.trigger_price is not None:
                    self.check_plan(order, simulated, quotes, trial)
                    plans += 1
                    continue
                ticket = self.size(order, simulated, quotes, trial)
            except Rejected as rejection:
                results[index] = str(rejection)
                continue
            quote = quotes[order.asset]
            if ticket.execution == RESTING:
                price = quote.bid if ticket.side == "buy" else quote.ask
            else:
                price = quote.ask if ticket.side == "buy" else quote.bid
            gross = ticket.qty * price
            fee = gross * self.entry_rate(ticket.execution)
            simulated.apply(ticket.asset, ticket.side, ticket.qty, gross, fee)
            fees += fee
            buys += ticket.side == "buy"
        return results
