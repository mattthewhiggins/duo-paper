"""What every backtest returns. Metrics, the registry, walk-forward and the gate all read this one shape."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

_DAILY = ("returns", "gross_returns", "turnover", "exposure", "costs")


@dataclass(frozen=True, eq=False)  # eq=False because arrays have no usable == or hash.
class Result:
    """One backtest, one row per day. Arrays are read-only, so a report can't change what it reports."""

    name: str
    ts: np.ndarray  # D day-open times in seconds, int64, strictly increasing.
    returns: np.ndarray  # D after-cost simple returns, marked at each day's close.
    gross_returns: np.ndarray  # D the same days before costs.
    turnover: np.ndarray  # D sum of absolute weight changes traded that day.
    exposure: np.ndarray  # D sum of absolute weights at that day's close.
    costs: np.ndarray  # D costs charged that day, as a fraction of equity.
    weights: np.ndarray  # D x A weights at each day's close, after drift.
    assets: tuple[str, ...]
    info: dict = field(default_factory=dict)  # Counts and event lists: delistings, cost model flags, trades.

    def __post_init__(self):
        ts = np.asarray(self.ts, dtype=np.int64)
        days = ts.size
        if ts.ndim != 1 or np.any(np.diff(ts) <= 0):
            raise ValueError(f"{self.name}: ts must be one-dimensional and strictly increasing")
        object.__setattr__(self, "assets", tuple(self.assets))
        for key, values in (("ts", ts), *((key, np.asarray(getattr(self, key), dtype=np.float64)) for key in _DAILY)):
            if values.shape != (days,):
                raise ValueError(f"{self.name}: {key} has shape {values.shape}, expected {(days,)}")
            view = values.view()
            view.flags.writeable = False
            object.__setattr__(self, key, view)
        weights = np.asarray(self.weights, dtype=np.float64)
        if weights.shape != (days, len(self.assets)):
            raise ValueError(f"{self.name}: weights has shape {weights.shape}, expected {(days, len(self.assets))}")
        view = weights.view()
        view.flags.writeable = False
        object.__setattr__(self, "weights", view)
