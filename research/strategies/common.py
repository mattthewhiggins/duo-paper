"""What the four families in research/STRATEGY_SPEC.md share: the two cost tracks, the data, the window rule and ids.

Every rolling figure here is worked out window by window, adding the same values in the same order at every row, so a
row's value can't move by a bit when later rows are cut off. A running total would be faster, but a total carried from
a coin's 2018 price into its 2022 price loses the small numbers it's later subtracted down to, and the truncation test
is byte for byte.

Where the spec leaves a choice open, these are the readings taken:

- A window counts rows, so a panel whose rows aren't evenly spaced raises ValueError rather than let a window of L rows
  span more than L bars. The real panels have no missing row: spot_1d and perps_1d are daily without a gap, and
  majors_15m has all 96 rows of every day, with NaN where there was no trade.
- A NaN anywhere in a window makes its sum, mean, std, max and min NaN, which is the spec's "needs L non-NaN values",
  and a comparison with NaN is false, so a signal on it is off.
- variant_id's parts come from each family's parts(params), from the base grid's values. A neighbour's id is its base
  id with what changed added, as the spec says, and that's the driver's to write.
- The cost models are loaded fresh on each call, from the root data.py uses, so a test's RESEARCH_ROOT moves them with
  the panels. Whether the file is still its frozen hash is the registry's check, made at every run.
"""
from __future__ import annotations

import math

import numpy as np

from research.harness import clean
from research.harness.costs import CostModel
from research.harness.data import _root, _utc

F3_START_TS = _utc(2020, 4, 7)  # Amendment 5 item 7: the first day the TOP5 perp universe can be traded.
DAY = 86400
ANNUAL = math.sqrt(365)

TRACKS = {
    "K": {"cost_model": "cost_model.json", "fee_level": "tier3_taker", "sensitivities": ("tier1_taker", "tier3_maker")},
    "R": {"cost_model": "cost_model_revx.json", "fee_level": "revx_taker", "sensitivities": ("revx_maker",)},
}

_loaded: dict = {}


def load_cost_model(track: str) -> CostModel:
    """The track's frozen cost model, under RESEARCH_ROOT if that's set."""
    return CostModel.load(_root() / "research" / "artifacts" / TRACKS[track]["cost_model"])


def load_data(split: str = "dev", key=None) -> dict:
    """The cleaned panels and BTC's and ETH's daily closes, loaded once per process for each root, split and key.

    {"spot", "majors", "perps"} are Panels from clean.py, and "btc_close" and "eth_close" are spot_1d's columns, on
    spot's rows.
    """
    place = (str(_root()), split, key)
    if place not in _loaded:
        spot = clean.spot_panel(split, key)
        _loaded[place] = {
            "spot": spot,
            "majors": clean.majors_panel(split, key),
            "perps": clean.perp_panel(spot, split, key),
            "btc_close": spot.close[:, column(spot, "BTC")],
            "eth_close": spot.close[:, column(spot, "ETH")],
        }
    return _loaded[place]


def variant_id(family: str, parts, track: str) -> str:
    """F1__ret_60__BTC_ETH__vol40 on track K, with __R added on track R."""
    if track not in TRACKS:
        raise ValueError(f"track must be one of {sorted(TRACKS)}, not {track!r}")
    return "__".join([family, *parts]) + ("__R" if track == "R" else "")


def column(panel, name: str) -> int:
    """The column of BTC or ETH. The one use of a name a strategy may make."""
    if name not in panel.assets:
        raise ValueError(f"{panel.name} has no {name} column")
    return panel.assets.index(name)


def check_spacing(panel, seconds: int | None = None) -> None:
    """Rows evenly spaced, and `seconds` apart if given, so that L rows are L bars."""
    gaps = np.unique(np.diff(panel.ts))
    if gaps.size > 1 or (seconds is not None and gaps.size and gaps[0] != seconds):
        raise ValueError(f"{panel.name} has rows {gaps.tolist()} seconds apart, so a window of rows isn't of bars")


def from_start(ts: np.ndarray, start_ts: int, bar: int = DAY) -> np.ndarray:
    """Rows from the bar before start_ts on, which decides the first fill. Read from ts, so a cut can't move it."""
    return np.asarray(ts) >= start_ts - bar


def round_half_up(value: float) -> int:
    """Halves go up, as the spec says for Donchian's X and max_bars. Python's round() sends them to the even number."""
    return int(math.floor(value + 0.5))


def all_present(values: np.ndarray, length: int) -> np.ndarray:
    """ok[t, a]: the `length` rows ending at t are all non-NaN. Counted in integers, so it's exact."""
    rows = values.shape[0]
    missing = np.zeros((rows + 1, *values.shape[1:]), dtype=np.int64)
    missing[1:] = np.cumsum(np.isnan(values), axis=0)
    ok = np.zeros(values.shape, dtype=bool)
    if length <= rows:
        ok[length - 1 :] = missing[length:] == missing[: rows - length + 1]
    return ok


def _rolled(values: np.ndarray, length: int, combine) -> np.ndarray:
    """out[t] = combine over rows t-length+1 .. t, in that order, NaN where the window doesn't fit."""
    rows = values.shape[0]
    out = np.full(values.shape, np.nan)
    if 1 <= length <= rows:
        count = rows - length + 1
        total = values[:count].astype(np.float64, copy=True)
        for k in range(1, length):
            total = combine(total, values[k : k + count])
        out[length - 1 :] = total
    return out


def rolling_sum(values: np.ndarray, length: int) -> np.ndarray:
    return _rolled(values, length, np.add)


def rolling_mean(values: np.ndarray, length: int) -> np.ndarray:
    return rolling_sum(values, length) / length


def rolling_std(values: np.ndarray, length: int) -> np.ndarray:
    """std with ddof=1 of the `length` values ending at t: two passes, so a large mean doesn't swamp the spread."""
    mean = rolling_mean(values, length)
    rows = values.shape[0]
    out = np.full(values.shape, np.nan)
    if 2 <= length <= rows:
        count = rows - length + 1
        centre = mean[length - 1 :]
        total = (values[:count] - centre) ** 2
        for k in range(1, length):
            total = total + (values[k : k + count] - centre) ** 2
        out[length - 1 :] = np.sqrt(total / (length - 1))
    return out


def rolling_max(values: np.ndarray, length: int) -> np.ndarray:
    return _rolled(values, length, np.maximum)  # np.maximum passes a NaN on, so a gap in the window gives NaN.


def rolling_min(values: np.ndarray, length: int) -> np.ndarray:
    return _rolled(values, length, np.minimum)


def lagged(values: np.ndarray, rows: int = 1) -> np.ndarray:
    """values[t - rows] at t, NaN where there's no such row."""
    out = np.full(values.shape, np.nan)
    if rows < values.shape[0]:
        out[rows:] = values[: values.shape[0] - rows]
    return out


def log_returns(close: np.ndarray) -> np.ndarray:
    """ln(C[t] / C[t-1]), NaN if either close is missing, so a return never bridges a gap."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.log(close / lagged(close))


def annual_vol(close: np.ndarray, window: int) -> np.ndarray:
    """std (ddof=1) of the last `window` daily log returns ending at t, times sqrt(365). Needs window + 1 closes."""
    return rolling_std(log_returns(close), window) * ANNUAL


def vol_scale(vol: np.ndarray, target: float) -> np.ndarray:
    """min(1, target / vol), and 0 where vol is unknown: a set target with no vol means no position."""
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.minimum(1.0, target / vol)
    return np.where(np.isnan(vol), 0.0, scale)
