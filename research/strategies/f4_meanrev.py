"""F4: mean reversion on intraday bars with hard stops. research/STRATEGY_SPEC.md defines it.

At each bar's close: z = (C - SMA(L)) / S(L), enter below Z if the filter allows, exit at C >= SMA(L) or after
round(multiple x L) bars, stop P below the entry. Size is sleeve x min(1, 0.40 / vol). Vol, uptrend and lowvol come from
spot_1d's daily closes up to the last completed UTC day before the bar's own day, never from resampled bars.

Where the spec leaves a choice open, these are the readings taken:

- uptrend reads each asset's own daily close against its own SMA(200), so ETH's entry looks at ETH. lowvol reads BTC for
  both assets, as the spec says.
- "The last completed UTC day before the bar's own" is the calendar day before the bar's open day. If the daily panel
  has no row for that day, vol, uptrend and lowvol are all unknown for the bar, so it can't enter, rather than reach
  back to an older day.
- lowvol for a bar on day d is regime_labels' low_vol for day d, which uses closes up to d-1. It's worked out with one
  empty day added after the daily panel's last row, so a bar can read it while day d is still running.
- lowvol's two lengths, section 8's 30-day realised vol and its 365-day median, are parameters of lowvol variants
  (lowvol_window, lowvol_median), as trend_sma is of uptrend ones. Amendment 2 item 3 varies "filter lengths", and the
  preregistration wins over STRATEGY_SPEC.md's list, which leaves them out. regime_labels has no length arguments, so
  low_vol here is its rule with the two lengths passed in, and a test holds it to regime_labels byte for byte at 30
  and 365. Section 8's "at least 180 days of history" isn't varied: it's how much history a label needs, not a length
  of the filter, and on spot_1d, where BTC has no missing day from 2017-09-01, every label from 2018-07-01 on has more
  than 216 values behind it either way, so its neighbours would be copies of the base, which pull the median Sharpe of
  test 3's score towards the base's.
- A bar's own day is the day its open falls on, so a 4h bar from 20:00 belongs to that day even though it closes at
  midnight.
- Where vol is unknown, entry is false and size is 0. A vol of exactly 0 gives min(1, 0.40 / 0) = 1.
- multiple x L rounds half up to whole bars, as Donchian's X does. Z's "+" neighbour is -2.4, further from 0.
- Columns other than the variant's assets get no entry, so the result carries them at weight 0.
"""
from __future__ import annotations

import numpy as np

from research.harness.engine import EVAL_START_TS, backtest_trades
from research.harness.panels import resample
from research.harness.regimes import MEDIAN_WINDOW, MIN_MEDIAN_VALUES, VOL_WINDOW
from research.strategies.common import (
    DAY,
    annual_vol,
    check_spacing,
    column,
    rolling_mean,
    rolling_std,
    round_half_up,
    vol_scale,
)

FAMILY = "F4"
PANELS = ("majors_15m", "spot_1d")
BAR_MINUTES = {"15m": None, "1h": 60, "4h": 240}
ASSETS = {"BTC": ("BTC",), "ETH": ("ETH",), "BTC_ETH": ("BTC", "ETH")}
NUMERIC = {
    "L": {"integer": True}, "Z": {"integer": False}, "P": {"integer": False}, "multiple": {"integer": False},
    "target": {"integer": False}, "W": {"integer": True}, "trend_sma": {"integer": True},
    "lowvol_window": {"integer": True}, "lowvol_median": {"integer": True},
}


def base_variants() -> list[dict]:
    """The 216 grid points: bar, assets, L, Z, P, filter, each in the spec's order."""
    out = []
    for bar in BAR_MINUTES:
        for assets in ASSETS:
            for length in (20, 50):
                for z in (-2.0, -3.0):
                    for stop in (0.03, 0.05):
                        for market in ("none", "uptrend", "lowvol"):
                            params = {"bar": bar, "assets": assets, "L": length, "Z": z, "P": stop,
                                      "multiple": 2.0, "target": 0.40, "W": 30, "filter": market}
                            if market == "uptrend":
                                params["trend_sma"] = 200
                            if market == "lowvol":
                                params.update(lowvol_window=VOL_WINDOW, lowvol_median=MEDIAN_WINDOW)
                            out.append(params)
    return out


def parts(params: dict) -> list[str]:
    """The id's parts, such as ["1h", "BTC_ETH", "L20", "Z-2", "P3", "lowvol"]."""
    return [params["bar"], params["assets"], f"L{params['L']}", f"Z{params['Z']:g}", f"P{round(params['P'] * 100)}",
            params["filter"]]


def max_bars(params: dict) -> int:
    return round_half_up(params["multiple"] * params["L"])


def bar_panel(params: dict, majors):
    """The 15-minute panel, or resample() of it to 1h or 4h."""
    minutes = BAR_MINUTES[params["bar"]]
    return majors if minutes is None else resample(majors, minutes)


def low_vol(btc_close: np.ndarray, window: int, median_days: int) -> np.ndarray:
    """low_vol[t] for day t from daily closes up to t-1: regime_labels' rule, with its two lengths as arguments.

    v is the std (ddof=1) of the `window` log returns ending at a close, NaN if any is missing. It's low if it isn't
    above the median of the v's over the `median_days` closes ending there, skipping NaN and needing 180 of them.
    Rows must be a day apart, which _daily_values checks.
    """
    closes = np.asarray(btc_close, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        returns = np.log(closes[1:] / closes[:-1])
    v = np.full(closes.size, np.nan)
    if returns.size >= window:
        # As regimes.py works it out, so that at its lengths the labels are the same to the bit.
        v[window:] = np.lib.stride_tricks.sliding_window_view(returns, window).std(axis=1, ddof=1)
    says = np.zeros(closes.size, dtype=bool)
    for s in np.flatnonzero(~np.isnan(v)):
        past = v[max(0, s - median_days + 1) : s + 1]
        past = past[~np.isnan(past)]
        if past.size >= MIN_MEDIAN_VALUES:
            says[s] = not v[s] > np.median(past)
    out = np.zeros(closes.size, dtype=bool)
    out[1:] = says[:-1]  # Known at the close of t-1, so it applies to day t.
    return out


def _daily_values(params: dict, daily, names) -> tuple[np.ndarray, np.ndarray]:
    """(vol, allowed) per daily row and asset, each as known at that day's close and meant for bars of the next day."""
    check_spacing(daily, DAY)
    close = daily.close[:, [column(daily, name) for name in names]]
    vol = annual_vol(close, params["W"])
    allowed = np.ones(close.shape, dtype=bool)
    if params["filter"] == "uptrend":
        with np.errstate(invalid="ignore"):
            allowed = close > rolling_mean(close, params["trend_sma"])
    elif params["filter"] == "lowvol" and daily.ts.size:
        btc = daily.close[:, column(daily, "BTC")]
        # One empty day after the last, so its label, which reads only the days before it, exists.
        labels = low_vol(np.r_[btc, np.nan], params.get("lowvol_window", VOL_WINDOW),
                         params.get("lowvol_median", MEDIAN_WINDOW))
        allowed = np.repeat(labels[1:, None], len(names), axis=1)
    elif params["filter"] not in ("none", "lowvol"):
        raise ValueError(f"unknown filter {params['filter']!r}")
    return vol, allowed


def signals(params: dict, bars, daily) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(entry, exit, size), each T x A on the bar panel's rows and columns. daily is spot_1d, or a cut of it."""
    check_spacing(bars)
    names = ASSETS[params["assets"]]
    columns = [column(bars, name) for name in names]
    close = bars.close[:, columns]
    sma = rolling_mean(close, params["L"])
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (close - sma) / rolling_std(close, params["L"])
        leave = close >= sma

    vol, allowed = _daily_values(params, daily, names)
    # One more row, unknown and not allowed, for bars whose day before has no daily row.
    vol = np.vstack([vol, np.full((1, len(names)), np.nan)])
    allowed = np.vstack([allowed, np.zeros((1, len(names)), dtype=bool)])
    day_before = bars.ts // DAY * DAY - DAY
    row = np.searchsorted(daily.ts, day_before)
    found = np.zeros(row.size, dtype=bool)
    found[row < daily.ts.size] = daily.ts[row[row < daily.ts.size]] == day_before[row < daily.ts.size]
    row = np.where(found, row, daily.ts.size)
    bar_vol, bar_allowed = vol[row], allowed[row]

    size = vol_scale(bar_vol, params["target"]) / len(names)
    with np.errstate(invalid="ignore"):
        enter = (z < params["Z"]) & bar_allowed & (size > 0)
    shape = bars.close.shape
    entry, exit_, sizes = np.zeros(shape, dtype=bool), np.zeros(shape, dtype=bool), np.zeros(shape)
    entry[:, columns], exit_[:, columns], sizes[:, columns] = enter, leave, size
    return entry, exit_, sizes


def universe_mask(params: dict, data: dict):
    return None  # F4 trades fixed assets, and random_trades takes no mask.


def run(params: dict, data: dict, cost_model, fee_level: str, cost_scale: float = 1.0, fee_bps=None):
    bars = bar_panel(params, data["majors"])
    entry, exit_, size = signals(params, bars, data["spot"])
    return backtest_trades(bars, entry, exit_, size, stop_pct=params["P"], max_bars=max_bars(params),
                           name="__".join([FAMILY, *parts(params)]), start_ts=EVAL_START_TS, cost_model=cost_model,
                           fee_level=fee_level, cost_scale=cost_scale, fee_bps=fee_bps)
