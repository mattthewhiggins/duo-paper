"""Where orders go: simulated fills in paper mode, Kraken in live mode. The rest of the bot can't tell them apart."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from .config import D, Config
from .kraken import KrakenClient, KrakenError, KrakenNetworkError
from .ledger import Portfolio
from .market import Market, Quote
from .risk import Ticket

ORDER_POLL_SECONDS = 20
CANCEL_POLL_SECONDS = 5


class OrderRejected(Exception):
    """The order definitely didn't happen."""


class OrderStateUnknown(Exception):
    """The order may have happened. The bot halts rather than risk doubling up."""


@dataclass(frozen=True)
class Fill:
    qty: Decimal
    price: Decimal
    gross: Decimal
    fee: Decimal
    venue_id: str | None = None
    liquidity: str = "taker"


@dataclass(frozen=True)
class RestingStatus:
    """A resting order's running totals so far, and whether it's finished."""
    qty: Decimal
    gross: Decimal
    fee: Decimal
    done: bool


NOTHING_FILLED = RestingStatus(Decimal(0), Decimal(0), Decimal(0), True)
STILL_OPEN = RestingStatus(Decimal(0), Decimal(0), Decimal(0), False)


class PaperBroker:
    """Simulates fills at live prices, leaning pessimistic.

    Immediate orders fill at the bid or ask plus assumed slippage. A resting order only fills once the
    market has traded clearly past it, because merely touching its price wouldn't reach the front of the
    queue. Prices are only checked at each poll, so brief moves between polls count as misses.
    """

    def __init__(self, config: Config):
        self.taker_rate = config.taker_pct / 100
        self.maker_rate = config.maker_pct / 100
        self.slippage = config.paper_slippage_pct / 100
        self.trade_through = config.paper_trade_through_pct / 100

    def execute(self, ticket: Ticket, quote: Quote) -> Fill | None:
        if ticket.side == "buy":
            price = quote.ask * (1 + self.slippage)
            if price > ticket.limit_price:
                return None
        else:
            price = quote.bid * (1 - self.slippage)
            if price < ticket.limit_price:
                return None
        gross = ticket.qty * price
        return Fill(ticket.qty, price, gross, gross * self.taker_rate, f"paper-{uuid.uuid4().hex[:12]}")

    def place_resting(self, ticket: Ticket, quote: Quote, ttl_seconds: int) -> str:
        crosses = ticket.limit_price >= quote.ask if ticket.side == "buy" else ticket.limit_price <= quote.bid
        if crosses:
            raise OrderRejected(f"a resting {ticket.side} at {ticket.limit_price} would have crossed the market")
        return f"paper-{uuid.uuid4().hex[:12]}"

    def check_resting(self, order, quote: Quote) -> RestingStatus | None:
        limit = D(order["limit_price"])
        if order["side"] == "buy":
            filled = quote.ask <= limit * (1 - self.trade_through)
        else:
            filled = quote.bid >= limit * (1 + self.trade_through)
        if not filled:
            return STILL_OPEN
        qty = D(order["qty"])
        gross = qty * limit
        return RestingStatus(qty, gross, gross * self.maker_rate, True)

    def cancel_resting(self, order, quote: Quote) -> RestingStatus | None:
        status = self.check_resting(order, quote)
        return status if status.done else NOTHING_FILLED

    def reconcile(self, portfolio: Portfolio) -> str | None:
        return None


class KrakenBroker:
    """Immediate orders are immediate-or-cancel limits, so a fill can never be worse than the ticket's price.

    Resting orders are post-only, so they can only ever pay the maker fee, and they carry an expiry on
    Kraken's side. If the bot dies, Kraken still cancels them, so nothing fills later without a stop watching it.
    """

    def __init__(self, client: KrakenClient, market: Market):
        self.client = client
        self.market = market

    def execute(self, ticket: Ticket, quote: Quote) -> Fill | None:
        pair = self.market.pairs[ticket.asset]
        client_id = str(uuid.uuid4())
        try:
            result = self.client.private(
                "AddOrder",
                pair=pair.altname,
                type=ticket.side,
                ordertype="limit",
                timeinforce="IOC",
                volume=format(ticket.qty, "f"),
                price=format(ticket.limit_price, "f"),
                oflags="fciq",  # Always charge fees in the quote currency, so the ledger matches.
                cl_ord_id=client_id,
            )
        except KrakenError as error:
            raise OrderRejected(str(error)) from error
        except KrakenNetworkError as error:
            raise OrderStateUnknown(f"AddOrder for {ticket.asset} got no answer, client id {client_id}: {error}") from error
        order_id = result["txid"][0]

        deadline = time.time() + ORDER_POLL_SECONDS
        while time.time() < deadline:
            try:
                order = self.client.private("QueryOrders", txid=order_id, trades="false")[order_id]
            except (KrakenError, KrakenNetworkError):
                time.sleep(1)
                continue
            if order["status"] in ("closed", "canceled", "expired"):
                qty = D(order["vol_exec"])
                if qty <= 0:
                    return None
                gross = D(order["cost"])
                return Fill(qty, gross / qty, gross, D(order["fee"]), order_id)
            time.sleep(1)
        raise OrderStateUnknown(f"order {order_id} for {ticket.asset} didn't report a final state in {ORDER_POLL_SECONDS}s")

    def place_resting(self, ticket: Ticket, quote: Quote, ttl_seconds: int) -> str:
        pair = self.market.pairs[ticket.asset]
        client_id = str(uuid.uuid4())
        try:
            result = self.client.private(
                "AddOrder",
                pair=pair.altname,
                type=ticket.side,
                ordertype="limit",
                timeinforce="GTD",
                expiretm=f"+{ttl_seconds}",
                volume=format(ticket.qty, "f"),
                price=format(ticket.limit_price, "f"),
                oflags="post,fciq",
                cl_ord_id=client_id,
            )
        except KrakenError as error:
            raise OrderRejected(str(error)) from error
        except KrakenNetworkError as error:
            raise OrderStateUnknown(
                f"a resting {ticket.side} for {ticket.asset} got no answer, client id {client_id}: {error}. "
                f"If Kraken took it, it expires by itself in {ttl_seconds}s, but any fill won't be in the bot's books"
            ) from error
        return result["txid"][0]

    def check_resting(self, order, quote: Quote) -> RestingStatus | None:
        """Returns None if Kraken can't say right now, so the next poll tries again."""
        order_id = order["venue_id"]
        try:
            info = self.client.private("QueryOrders", txid=order_id, trades="false")[order_id]
        except (KrakenError, KrakenNetworkError, KeyError):
            return None
        return RestingStatus(D(info["vol_exec"]), D(info["cost"]), D(info["fee"]),
                             info["status"] in ("closed", "canceled", "expired"))

    def cancel_resting(self, order, quote: Quote) -> RestingStatus | None:
        """Returns the final totals, or None if the order isn't confirmed closed yet."""
        try:
            self.client.private("CancelOrder", txid=order["venue_id"])
        except (KrakenError, KrakenNetworkError):
            pass  # It may already be closed. Asking for its state settles it either way.
        deadline = time.time() + CANCEL_POLL_SECONDS
        while True:
            status = self.check_resting(order, quote)
            if status is not None and status.done:
                return status
            if time.time() >= deadline:
                return None
            time.sleep(0.5)

    def reconcile(self, portfolio: Portfolio) -> str | None:
        """Returns a problem if the account holds less than the ledger says the bot owns."""
        try:
            balances = self.client.private("Balance")
        except (KrakenError, KrakenNetworkError):
            return None  # An outage is handled by the stale-price check; only real mismatches halt.
        for asset, position in portfolio.positions.items():
            code = self.market.pairs[asset].base_code
            held = D(balances.get(code, 0))
            if position.qty > 0 and held < position.qty:
                return f"the ledger says {position.qty} {asset} but Kraken shows {held}"
        return None


def _permission_denied(error: KrakenError) -> bool:
    return any("Permission denied" in e for e in error.errors)


def preflight(config: Config, client: KrakenClient, market: Market) -> list[str]:
    """Checks the live setup without trading. Returns problems; an empty list means it's safe to start."""
    problems = []
    try:
        balances = client.private("Balance")
    except (KrakenError, KrakenNetworkError) as error:
        return [f"Couldn't read the Kraken balance: {error}"]

    # WithdrawInfo needs the Withdraw permission and only quotes a fee, so a made-up
    # withdrawal key can't move money. Only "Permission denied" proves the key can't withdraw;
    # anything else, including "unknown withdraw key", means it might.
    try:
        client.private("WithdrawInfo", asset="XBT", key="duo-preflight-does-not-exist", amount="0.01")
        problems.append("This Kraken key has the Withdraw permission. Create a new key without it.")
    except KrakenError as error:
        if not _permission_denied(error):
            problems.append(f"This Kraken key may have the Withdraw permission (Kraken said: {error}). Create a new key without it.")
    except KrakenNetworkError as error:
        problems.append(f"Couldn't confirm the key can't withdraw: {error}")

    # These name an order that doesn't exist, so they can't touch anything. Any answer except
    # "Permission denied" means the key is allowed to make the call.
    fake_order = "OAAAAA-AAAAA-AAAAAA"
    for method, purpose in (("QueryOrders", "check on its orders"), ("CancelOrder", "cancel its resting orders")):
        try:
            client.private(method, txid=fake_order)
        except KrakenError as error:
            if _permission_denied(error):
                problems.append(f"This Kraken key can't {purpose}. Add that permission to the key.")
        except KrakenNetworkError as error:
            problems.append(f"Couldn't confirm the key can {purpose}: {error}")

    market.refresh()
    pairs = market.pairs
    any_pair = next(iter(pairs.values()))
    cash = D(balances.get(any_pair.quote_code, 0))
    if cash < config.starting_capital:
        problems.append(f"starting_capital is {config.starting_capital} {config.quote} but Kraken only shows {cash} free")

    # validate=true checks the order without trading. As a second guard, each price is half
    # the market, so neither could fill even if Kraken ignored validate. The resting version
    # also expires by itself within a minute.
    for asset in config.assets:
        pair, quote = pairs[asset], market.quotes[asset]
        price = (quote.bid / 2).quantize(Decimal(1).scaleb(-pair.pair_decimals))
        common = dict(pair=pair.altname, type="buy", ordertype="limit",
                      volume=format(pair.ordermin * 3, "f"), price=format(price, "f"), validate="true")
        try:
            client.private("AddOrder", timeinforce="IOC", **common)
            client.private("AddOrder", timeinforce="GTD", expiretm="+60", oflags="post,fciq", **common)
        except (KrakenError, KrakenNetworkError) as error:
            problems.append(f"Kraken wouldn't validate a {asset} order: {error}")
    return problems
