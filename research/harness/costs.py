"""One-way trading costs for the backtests, read from research/artifacts/cost_model.json.

research/tools/build_cost_model.py writes that file from section 5 of the preregistration and the sampled
Kraken books. Every number lives in the file, and this module only holds the arithmetic:

    total = fee + FX + max(floor, history multiplier x spread and impact at this size)

Spread and impact follow straight lines between the measured sizes. Where the rules leave a choice open,
these are the readings taken:

- The floor comes after the multiplier, because it limits the cost actually charged.
- FX is neither multiplied nor floored. It's a charge for converting GBP, not a book that was thinner in 2018.
- Below the smallest measured size, the cost stays at that size's level. Nothing smaller was measured, and a
  line carried on downwards could fall under the half-spread.
- A null size sits on the curve like any other point. The first is at the largest measured level with its impact
  doubled, and each one after it doubles the impact again, so the last line carried on keeps rising: Amendment 2 says a
  bigger account pays more impact, and level points would make the thinnest books the only ones where it doesn't.
  Impact is the level minus the half-spread, and the half-spread itself isn't doubled. A book that showed no impact
  before it ran out has only its half-spread to go on, so that's what doubles. Sizes in between are interpolated, so
  the cost doesn't jump just past the largest measured size.
- An asset with no measured level at all is priced like one not on Kraken, at the flat default without FX.

The panels use Binance's names at the time, and a venue's book may carry the coin's name today. Amendment 6's table in
the file says which book a panel name uses. A later part of a column, X~2, is the same coin as X unless the table says
not, and a part after that follows the nearest earlier part the table lists, so X~3 pays no book if X~2 has none.

Every number must be finite and not negative, or the model refuses to load. max(floor, NaN) keeps the floor, so a
damaged level would price a trade at the cheapest cost there is, and a negative one lands in the same place. That goes
further than the owner's ruling, which names NaN and infinity. It can only refuse more.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from research.harness.data import _remember_file, _root

# Why a cost isn't simply a measured level. Reports should show these, so a result resting on them is visible.
NOT_MEASURED = "not_measured"  # The flat default, an assumption.
NULL_SIZE = "null_size"  # Past the largest size this book can fill, so it rests on the doubled impact.
ABOVE_LARGEST = "above_largest"  # Past the largest size sampled, so it rests on the last line carried on.


@dataclass(frozen=True)
class SpotCost:
    """One trade's cost, split up so a report can show where it came from. All in bps, one way."""

    fee_bps: float
    fx_bps: float
    spread_impact_bps: float  # After the multiplier and the floor.
    multiplier: float
    total_bps: float
    flags: tuple[str, ...]  # Empty means it comes only from measured levels.


class _Curve(NamedTuple):
    points: list[tuple[int, float]]  # (size in GBP, spread and impact in bps), rising in size.
    largest_measured: int
    fx_bps: float


def _a_cost(value, label: str) -> float:
    number = float(value)
    if not 0 <= number < math.inf:
        raise ValueError(f"{label} in the cost model must be a finite number of 0 or more, got {value}")
    return number


def _curve(entry: dict) -> _Curve | None:
    """The points one asset's cost follows, or None if it has no measured level."""
    levels = sorted((int(size), level) for size, level in entry["spread_impact_bps"].items())
    measured = [(size, _a_cost(level, "a spread level")) for size, level in levels if level is not None]
    if not measured:
        return None
    # A walk that fills a size also fills every smaller one, so a gap under a measured size means a bad file.
    if len(levels) < 2 or [point[0] for point in measured] != [point[0] for point in levels[: len(measured)]]:
        raise ValueError(f"bad levels in the cost model: {entry['spread_impact_bps']}")
    largest, level = measured[-1]
    half_spread = _a_cost(entry["half_spread_bps"], "a half-spread")
    step = level - half_spread if level > half_spread else level
    points = []
    for size, value in levels:
        if value is None:
            value = points[-1][1] + step
            step *= 2
        points.append((size, float(value)))
    return _Curve(points, largest, _a_cost(entry["fx_bps"], "an FX charge"))


def _interpolate(points: list[tuple[int, float]], size: float) -> float:
    """Straight lines between the points, flat below the first, and the last line carried on above the last."""
    if size <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if size <= x1:
            break
    return y0 + (y1 - y0) * (size - x0) / (x1 - x0)


class CostModel:
    """Use one per run: flag_counts builds up as costs are asked for, and the costs themselves never change."""

    def __init__(self, model: dict):
        self.samples_sha256 = model["source"]["samples_sha256"]
        fee_levels = model["fee_levels_bps"].items()
        self.fee_levels = {name: _a_cost(level["value"], f"fee {name}") for name, level in fee_levels}
        self.default_spread_impact_bps = _a_cost(model["default_spread_impact_bps"]["value"], "the default")
        self.floor_bps = _a_cost(model["floor_bps"]["value"], "the floor")
        self.perp_fee_bps = _a_cost(model["perp_fee_bps"]["value"], "the perp fee")
        self.perp_half_spread_bps = _a_cost(model["perp_half_spread_bps"]["value"], "the perp half-spread")
        self.flag_counts = Counter()  # (asset, flag) -> how many costs have carried it.
        periods = model["history_multipliers"]["periods"]
        self._periods = [(period["until_ts"], _a_cost(period["multiplier"], "a multiplier")) for period in periods]
        assets = model["assets"]
        self._curves = {ticker: curve for ticker, entry in assets.items() if (curve := _curve(entry))}
        self._kraken_names = {entry["kraken_base"] for entry in assets.values()} - set(assets)
        self._panel_names = model.get("panel_names", {})

    @classmethod
    def load(cls, path=None) -> CostModel:
        """Reads the JSON file. The default is the frozen file, under RESEARCH_ROOT if that's set."""
        path = path or _root() / "research" / "artifacts" / "cost_model.json"
        content = Path(path).read_bytes()
        _remember_file(path, content)  # So the registry can tell if this file changes under the model loaded here.
        return cls(json.loads(content))

    def book_name(self, asset: str) -> str | None:
        """The name of the book a panel's column pays for, or None if the table says the venue has no such coin."""
        if asset in self._panel_names:
            return self._panel_names[asset]
        coin, _, part = asset.partition("~")
        # Nearest first, because the table can say a part is a different coin from the part before it.
        for earlier in range(int(part) - 1, 1, -1) if part.isdigit() else ():
            if f"{coin}~{earlier}" in self._panel_names:
                return self._panel_names[f"{coin}~{earlier}"]
        return self._panel_names.get(coin, coin)

    def multiplier(self, ts) -> float:
        """The history multiplier for a time in seconds."""
        ts = float(ts)
        # Milliseconds would look like the far future and NaN passes for "after", either way x1 without a word.
        if not ts < 1e11:
            raise ValueError(f"ts must be in seconds, got {ts}")
        for until, multiplier in self._periods:
            if until is None or ts < until:
                return multiplier
        raise ValueError("the cost model's history periods don't end in an open one")

    def spot_cost_detail(self, asset, notional_gbp, ts, fee_level="tier3_taker") -> SpotCost:
        """The cost of one trade with its parts and flags. spot_cost_bps is this, reduced to the total."""
        detail = self._priced(asset, notional_gbp, ts, fee_level)
        for flag in detail.flags:
            self.flag_counts[asset, flag] += 1
        return detail

    def _priced(self, asset, notional_gbp, ts, fee_level) -> SpotCost:
        """The same without counting its flags, for a cost asked about that nobody paid."""
        notional = float(notional_gbp)
        if not 0 <= notional < math.inf:
            raise ValueError(f"notional_gbp must be a size of 0 or more, buy or sell, got {notional_gbp}")
        if fee_level not in self.fee_levels:
            raise ValueError(f"fee_level must be one of {sorted(self.fee_levels)}, got {fee_level!r}")
        # Treated as unknown, Kraken's name would quietly get the default instead of its measured cost.
        if asset in self._kraken_names:
            raise ValueError(f"{asset} is Kraken's name. The cost model uses standard tickers")
        multiplier = self.multiplier(ts)
        curve = self._curves.get(self.book_name(asset))
        flags = []
        if curve is None:
            level, fx = self.default_spread_impact_bps, 0.0
            flags.append(NOT_MEASURED)
        else:
            level, fx = _interpolate(curve.points, notional), curve.fx_bps
            largest_sampled = curve.points[-1][0]
            if curve.largest_measured < largest_sampled and notional > curve.largest_measured:
                flags.append(NULL_SIZE)
            if notional > largest_sampled:
                flags.append(ABOVE_LARGEST)
        spread_impact = max(self.floor_bps, multiplier * level)
        fee = self.fee_levels[fee_level]
        return SpotCost(fee, fx, spread_impact, multiplier, fee + fx + spread_impact, tuple(flags))

    def spot_cost_bps(self, asset, notional_gbp, ts, fee_level="tier3_taker") -> float:
        """Total one-way cost in bps of one trade: fee, plus spread and impact, plus FX on a USD book.

        asset is a standard ticker. notional_gbp is the trade's size in GBP, positive for a buy or a sell.
        ts is in seconds. The default fee_level is the base case. tier3_maker is for the sensitivity only.
        """
        return self.spot_cost_detail(asset, notional_gbp, ts, fee_level).total_bps
