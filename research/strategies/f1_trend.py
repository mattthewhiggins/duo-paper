"""F1: time-series trend with volatility targeting, daily, long or flat. research/STRATEGY_SPEC.md defines it.

target[t, a] = sleeve[t] * signal[t, a] * scale[t, a], decided at the close of t, and band[t, a] = 0.10 * sleeve[t].

Where the spec leaves a choice open, these are the readings taken:

- ret_L "never bridges a NaN", so it needs all L + 1 closes from t-L to t, not only the two it divides.
- Donchian's state starts off on the panel's first row and runs from there, so on 2018-07-01 it's what a rule running
  since the data began would hold. It's off on any row where C[t] is NaN or either window holds a NaN, and on a row
  where both its tests were true at once it would turn on, which can only happen if a bar's low is above its high.
- A variant with no vol target has no "target" or "W" in its params, so neither is varied for it.
- X isn't a parameter of its own. The spec says it follows N, so N's neighbours carry their own round-half-up X.
- vol of exactly 0 gives min(1, target / 0) = 1.
- Section 9's "daily checks" are a parameter, interval = 1 day, because Amendment 2 item 3 varies every numeric
  parameter including the rebalance interval, and the preregistration wins over STRATEGY_SPEC.md's list. Its only
  neighbour is 2 (20% of 1 rounds back to 1, and never below 1). Checks fall on every interval-th bar counted from the
  bar before EVAL_START_TS, read from ts as F2's rebalances are, and on every row before that, of which only the last
  is ever executed. Between checks the target stays as set, except that a NaN close or a name out of the mask forces
  0, and the band is 10.0, so only that exit trades. With interval 1 every row is a check, which is the spec's rule.
  A params dict without interval checks daily.
- For TOP, signals are only worked out on columns the mask holds at some row of the panel it's given. The target is 0
  everywhere else, so this changes no output, and it keeps the truncation test, which runs every row, to minutes.
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
    rolling_max,
    rolling_mean,
    rolling_min,
    round_half_up,
    vol_scale,
)

FAMILY = "F1"
PANELS = ("spot_1d",)
SIGNALS = (
    {"signal": "ret", "L": 20}, {"signal": "ret", "L": 60}, {"signal": "ret", "L": 120}, {"signal": "ret", "L": 250},
    {"signal": "sma", "L": 20}, {"signal": "sma", "L": 50}, {"signal": "sma", "L": 100}, {"signal": "sma", "L": 200},
    {"signal": "cross", "F": 20, "S": 100}, {"signal": "cross", "F": 50, "S": 200},
    {"signal": "donchian", "N": 20}, {"signal": "donchian", "N": 55}, {"signal": "donchian", "N": 100},
    {"signal": "ENS", "L1": 20, "L2": 60, "L3": 120, "L4": 250},
)
UNIVERSES = ({"universe": "BTC"}, {"universe": "BTC_ETH"}, {"universe": "TOP", "n": 10})
VOL_TARGETS = ({"target": 0.20, "W": 30}, {"target": 0.40, "W": 30}, {})
BAND = 0.10
INTERVAL = 1  # Section 9's daily checks, in bars.
HOLD_BAND = 10.0  # Above any change a weight can make, so only forced exits trade between checks.
NUMERIC = {
    **{name: {"integer": True} for name in ("L", "F", "S", "N", "L1", "L2", "L3", "L4", "W", "n", "interval")},
    "target": {"integer": False},
    "band": {"integer": False},
}


def base_variants() -> list[dict]:
    """The 126 grid points: signal, then universe, then vol target, each in the spec's order."""
    return [{**signal, **universe, **vol, "band": BAND, "interval": INTERVAL} for signal in SIGNALS
            for universe in UNIVERSES for vol in VOL_TARGETS]


def parts(params: dict) -> list[str]:
    """The id's parts, such as ["ret_60", "BTC_ETH", "vol40"]."""
    kind = params["signal"]
    signal = {
        "ret": lambda: f"ret_{params['L']}", "sma": lambda: f"sma_{params['L']}",
        "cross": lambda: f"cross_{params['F']}_{params['S']}", "donchian": lambda: f"donchian_{params['N']}",
        "ENS": lambda: "ENS",
    }[kind]()
    universe = f"TOP{params['n']}" if params["universe"] == "TOP" else params["universe"]
    vol = f"vol{round(params['target'] * 100)}" if "target" in params else "none"
    return [signal, universe, vol]


def _ret_on(close: np.ndarray, lookback: int) -> np.ndarray:
    """1.0 where C[t] / C[t-L] - 1 > 0 with all L + 1 closes present, else 0.0, and whether it could be computed."""
    known = all_present(close, lookback + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        on = known & (close / lagged(close, lookback) - 1 > 0)
    return on.astype(np.float64), known


def donchian(close: np.ndarray, high: np.ndarray, low: np.ndarray, n: int) -> np.ndarray:
    """On after C[t] > the highest high of t-N..t-1, off after C[t] < the lowest low of t-X..t-1, X = N/2 rounded up."""
    highest = lagged(rolling_max(high, n))
    lowest = lagged(rolling_min(low, round_half_up(n / 2)))
    usable = ~(np.isnan(highest) | np.isnan(lowest) | np.isnan(close))
    with np.errstate(invalid="ignore"):
        breakout, breakdown = close > highest, close < lowest
    out = np.zeros(close.shape)
    on = np.zeros(close.shape[1], dtype=bool)
    for t in range(close.shape[0]):
        on = usable[t] & ((on & ~breakdown[t]) | breakout[t])
        out[t] = on
    return out


def signal_values(params: dict, close: np.ndarray, high: np.ndarray, low: np.ndarray) -> np.ndarray:
    """signal[t, a] in [0, 1] for the columns given."""
    kind = params["signal"]
    with np.errstate(invalid="ignore"):
        if kind == "ret":
            return _ret_on(close, params["L"])[0]
        if kind == "sma":
            return (close > rolling_mean(close, params["L"])).astype(np.float64)
        if kind == "cross":
            return (rolling_mean(close, params["F"]) > rolling_mean(close, params["S"])).astype(np.float64)
    if kind == "donchian":
        return donchian(close, high, low, params["N"])
    if kind == "ENS":
        legs = [_ret_on(close, params[name]) for name in ("L1", "L2", "L3", "L4")]
        known = np.logical_and.reduce([leg[1] for leg in legs])
        return np.where(known, sum(leg[0] for leg in legs) / 4, 0.0)
    raise ValueError(f"unknown signal {kind!r}")


def _universe(params: dict, spot) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(columns, inside[t, column], sleeve[t]). The sleeve is 1/N, N for TOP being the names in the mask that day."""
    rows = spot.ts.size
    if params["universe"] == "BTC":
        columns = [column(spot, "BTC")]
    elif params["universe"] == "BTC_ETH":
        columns = [column(spot, "BTC"), column(spot, "ETH")]
    elif params["universe"] == "TOP":
        mask = top_n_mask(spot, params["n"])
        count = mask.sum(axis=1)
        columns = np.flatnonzero(mask.any(axis=0))
        sleeve = np.where(count > 0, 1.0 / np.maximum(count, 1), 0.0)
        return columns, mask[:, columns], sleeve
    else:
        raise ValueError(f"unknown universe {params['universe']!r}")
    columns = np.array(columns)
    return columns, np.ones((rows, columns.size), dtype=bool), np.full(rows, 1.0 / columns.size)


def targets(params: dict, spot) -> tuple[np.ndarray, np.ndarray]:
    """(target, band), each T x A on spot's rows and columns."""
    check_spacing(spot, DAY)
    columns, inside, sleeve = _universe(params, spot)
    close = spot.close[:, columns]
    signal = signal_values(params, close, spot.high[:, columns], spot.low[:, columns])
    scale = vol_scale(annual_vol(close, params["W"]), params["target"]) if "target" in params else 1.0
    weight = sleeve[:, None] * signal * scale * inside
    check = check_rows(spot.ts, params.get("interval", INTERVAL))
    for t in np.flatnonzero(~check):
        weight[t] = weight[t - 1] * inside[t]  # Row 0 is always a check, so t - 1 exists.
    weight[np.isnan(close)] = 0.0  # No data at t, target 0.
    target = np.zeros(spot.close.shape)
    target[:, columns] = weight
    band = np.repeat(np.where(check, params["band"] * sleeve, HOLD_BAND)[:, None], spot.close.shape[1], axis=1)
    return target, band


def check_rows(ts: np.ndarray, interval: int) -> np.ndarray:
    """Every interval-th bar from the bar before EVAL_START_TS, and every row before it."""
    anchor = EVAL_START_TS - DAY
    return (ts < anchor) | (((ts - anchor) // DAY) % interval == 0)


def universe_mask(params: dict, data: dict):
    """TOP's mask for the null, which may place spells only where the variant could hold. None for BTC and BTC_ETH."""
    return top_n_mask(data["spot"], params["n"]) if params["universe"] == "TOP" else None


def run(params: dict, data: dict, cost_model, fee_level: str, cost_scale: float = 1.0, fee_bps=None):
    spot = data["spot"]
    target, band = targets(params, spot)
    return backtest_weights(spot, target, name="__".join([FAMILY, *parts(params)]), band=band,
                            start_ts=EVAL_START_TS, cost_model=cost_model, fee_level=fee_level,
                            cost_scale=cost_scale, fee_bps=fee_bps)
