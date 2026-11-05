"""F2: cross-sectional momentum, long-only, rebalanced every 7th bar. research/STRATEGY_SPEC.md defines it.

Where the spec leaves a choice open, these are the readings taken:

- The rebalance rows are counted in bars from the bar before EVAL_START_TS, read from ts rather than from a row number,
  so a panel cut before that date has no rebalance at all. Rows are checked to be one day apart, so bars are days.
- The return over L "never bridges a NaN", so a candidate needs all L + 1 closes from t-L to t.
- "Ties going to the earlier name" is the earlier column. clean.py's panels hold their columns in name order, so this
  needs no name to be read.
- The rule that drops a name whose vol can't be computed is read as invvol's: equal weights use no vol. A dropped name
  isn't replaced by the next in rank, and held / k counts the names left, so its 1/k stays in cash. A vol of exactly 0
  can't be inverted, so it counts as unknown too, which can only leave more in cash.
- Before the first rebalance every target is 0, and a name with a NaN close between rebalances has target 0 on that
  row only. The band of 10.0 stops it buying back, as the spec intends.
"""
from __future__ import annotations

import numpy as np

from research.harness.data import top_n_mask
from research.harness.engine import EVAL_START_TS, backtest_weights
from research.strategies.common import (
    DAY,
    all_present,
    annual_vol,
    check_spacing,
    column,
    lagged,
    rolling_mean,
)

FAMILY = "F2"
PANELS = ("spot_1d",)
INTERVAL = 7
HOLD_BAND = 10.0  # Above any change a weight can make, so only forced exits trade between rebalances.
NUMERIC = {name: {"integer": True} for name in ("n", "L", "k", "interval", "W", "sma")}


def base_variants() -> list[dict]:
    """The 80 grid points: universe, lookback, hold, weighting, filter, each in the spec's order."""
    out = []
    for n in (10, 30):
        for lookback in (7, 14, 28, 56, 84):
            for k in (3, 5):
                for weighting in ("equal", "invvol"):
                    for market in ("nofilter", "filter"):
                        params = {"n": n, "L": lookback, "k": k, "interval": INTERVAL, "weighting": weighting,
                                  "filter": market}
                        if weighting == "invvol":
                            params["W"] = 30
                        if market == "filter":
                            params["sma"] = 100
                        out.append(params)
    return out


def parts(params: dict) -> list[str]:
    """The id's parts, such as ["TOP10", "L28", "k3", "invvol", "filter"]."""
    return [f"TOP{params['n']}", f"L{params['L']}", f"k{params['k']}", params["weighting"], params["filter"]]


def rebalance_rows(ts: np.ndarray, interval: int) -> np.ndarray:
    """Every interval-th bar, counting the bar before EVAL_START_TS as the first."""
    anchor = EVAL_START_TS - DAY
    return (ts >= anchor) & (((ts - anchor) // DAY) % interval == 0)


def _picks(params: dict, ret: np.ndarray, known: np.ndarray, vol, columns: int) -> np.ndarray:
    """One rebalance row's weights: the top k by return, ties to the earlier column, equal or inverse-vol."""
    weights = np.zeros(columns)
    candidates = np.flatnonzero(known)
    ranked = candidates[np.lexsort((candidates, -ret[candidates]))]
    held = ranked[: params["k"]]
    if params["weighting"] == "equal":
        weights[held] = 1.0 / params["k"]
        return weights
    with np.errstate(invalid="ignore"):
        held = held[vol[held] > 0]  # A NaN vol isn't above 0, so it's dropped and its weight stays in cash.
    raw = 1.0 / vol[held]
    if held.size:
        weights[held] = raw / raw.sum() * held.size / params["k"]
    return weights


def targets(params: dict, spot) -> tuple[np.ndarray, np.ndarray]:
    """(target, band), each T x A on spot's rows and columns."""
    check_spacing(spot, DAY)
    close = spot.close
    rows, columns = close.shape
    mask = top_n_mask(spot, params["n"])
    known = mask & all_present(close, params["L"] + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close / lagged(close, params["L"]) - 1
    vol = annual_vol(close, params["W"]) if params["weighting"] == "invvol" else None
    if params["filter"] == "filter":
        btc = close[:, [column(spot, "BTC")]]
        with np.errstate(invalid="ignore"):
            market_on = (btc > rolling_mean(btc, params["sma"]))[:, 0]
    else:
        market_on = np.ones(rows, dtype=bool)

    rebalance = rebalance_rows(spot.ts, params["interval"])
    target = np.zeros((rows, columns))
    held = np.zeros(columns)
    for t in range(rows):
        if rebalance[t]:
            held = _picks(params, ret[t], known[t], None if vol is None else vol[t], columns) if market_on[t] \
                else np.zeros(columns)
        target[t] = held
    target[np.isnan(close)] = 0.0  # No data at t, target 0, and the band can't stop that exit.
    band = np.where(rebalance[:, None], 0.0, HOLD_BAND) * np.ones((1, columns))
    return target, band


def universe_mask(params: dict, data: dict):
    """The day's TOP-n, from which random_picks draws its names."""
    return top_n_mask(data["spot"], params["n"])


def run(params: dict, data: dict, cost_model, fee_level: str, cost_scale: float = 1.0, fee_bps=None):
    spot = data["spot"]
    target, band = targets(params, spot)
    return backtest_weights(spot, target, name="__".join([FAMILY, *parts(params)]), band=band,
                            start_ts=EVAL_START_TS, cost_model=cost_model, fee_level=fee_level,
                            cost_scale=cost_scale, fee_bps=fee_bps)
