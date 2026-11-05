"""The bot's own books, rebuilt from its fills. In live mode this keeps it from touching anything else in the account."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .config import D


@dataclass
class Position:
    qty: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)  # Includes buy fees.

    @property
    def avg_cost(self) -> Decimal:
        return self.cost / self.qty if self.qty else Decimal(0)


@dataclass
class Portfolio:
    starting: Decimal
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    realized: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)

    @classmethod
    def replay(cls, starting: Decimal, fills: Iterable) -> "Portfolio":
        portfolio = cls(starting=starting, cash=starting)
        for row in fills:
            portfolio.apply(row["asset"], row["side"], D(row["qty"]), D(row["gross"]), D(row["fee"]))
        return portfolio

    def copy(self) -> "Portfolio":
        return copy.deepcopy(self)

    def qty(self, asset: str) -> Decimal:
        position = self.positions.get(asset)
        return position.qty if position else Decimal(0)

    def apply(self, asset: str, side: str, qty: Decimal, gross: Decimal, fee: Decimal) -> Decimal:
        """Books a fill at average cost and returns the profit it realized."""
        position = self.positions.setdefault(asset, Position())
        self.fees += fee
        if side == "buy":
            self.cash -= gross + fee
            position.qty += qty
            position.cost += gross + fee
            return Decimal(0)
        if qty > position.qty:
            raise ValueError(f"Can't sell {qty} {asset}, only {position.qty} held")
        cost_out = position.cost * qty / position.qty
        realized = gross - fee - cost_out
        self.cash += gross - fee
        position.qty -= qty
        position.cost -= cost_out
        if position.qty == 0:
            position.cost = Decimal(0)
        self.realized += realized
        return realized

    def holdings_value(self, marks: dict[str, Decimal]) -> Decimal:
        return sum((p.qty * marks[a] for a, p in self.positions.items() if p.qty), Decimal(0))

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + self.holdings_value(marks)

    def unrealized(self, marks: dict[str, Decimal]) -> Decimal:
        return sum((p.qty * marks[a] - p.cost for a, p in self.positions.items() if p.qty), Decimal(0))
