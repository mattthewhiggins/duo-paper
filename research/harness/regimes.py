"""Section 8's regimes, from BTC's daily closes, and how a strategy's returns did in each of them.

A label for day t uses closes up to t-1 and nothing later, so it's known before day t's open. Where the rules leave a
choice open, these are the readings taken:

- "Any window holding a NaN" means the windows a figure is worked out from: the 61 closes behind x and the 31 behind v.
  The median's 365 days aren't one of those. The specification has it "needing at least 180 valid values" and section 8
  asks for 180 days of history, so it skips a NaN v, and one missing close turns high_vol and low_vol off for 31 days.
  Taking "any window" literally would make that 395, which looks better where it actually bites. BTC's 15-minute panel
  has no bars on 2018-02-09, so F4's lowvol filter would be off for the first 254 of the 2,208 evaluation days, and a
  strategy with no skill pays less and shows a profit by luck more often when it trades less. For G6 the longer blank is
  the harsher one, but BTC has no missing day in the daily panel, so both readings agree there as long as G6's labels
  come from that panel and not from 15-minute closes.
- A day with no row counts as a NaN close. That's what ts is for: without it, a 60-row window could span 61 days.
- Prices that don't move give x = 0/0, which isn't at or above 1, so the day is ranging.
- A NaN return can't work, and it counts as failing badly, because it can't be shown not to.
- A regime with no days has NaN figures, and one with a single day has a NaN sharpe, as in metrics.py. Neither has the
  60 days to work or fail badly.
"""
from __future__ import annotations

import math

import numpy as np

from research.harness.metrics import annual_sharpe, annualise, compound

REGIMES = ("trending", "ranging", "high_vol", "low_vol")
TREND_WINDOW = 60  # Daily log returns behind x.
VOL_WINDOW = 30  # Daily log returns behind v.
MEDIAN_WINDOW = 365  # Days of v behind m.
MIN_MEDIAN_VALUES = 180
MIN_REGIME_DAYS = 60
BAD_SHARPE = -1.0
BAD_ANNUAL_RETURN = -0.30
_SECONDS_PER_DAY = 86400


def _on_calendar(btc_close, ts) -> tuple[np.ndarray, np.ndarray]:
    """The closes laid out on an unbroken run of days, with NaN on any day without a row, and each row's day."""
    close = np.asarray(btc_close, dtype=np.float64)
    ts = np.asarray(ts)
    if close.ndim != 1 or ts.shape != close.shape:
        raise ValueError(f"closes have shape {close.shape} and their times {ts.shape}")
    known = close[~np.isnan(close)]
    if not np.all(np.isfinite(known) & (known > 0)):
        raise ValueError("closes must be positive and finite, or NaN for a gap")
    gaps = np.diff(ts)
    if np.any(gaps <= 0) or np.any(gaps % _SECONDS_PER_DAY != 0):
        raise ValueError("regime labels need daily bars, with ts strictly increasing in whole days")
    if not ts.size:
        return close, np.zeros(0, dtype=np.int64)
    day = np.rint((ts - ts[0]) / _SECONDS_PER_DAY).astype(np.int64)
    calendar = np.full(int(day[-1]) + 1, np.nan)
    calendar[day] = close
    return calendar, day


def _rolling_std(log_returns: np.ndarray, window: int, size: int) -> np.ndarray:
    """out[s] is the std (ddof=1) of the `window` log returns ending at close s. NaN if they don't all exist."""
    out = np.full(size, np.nan)
    if log_returns.size >= window:
        # Each window is worked out on its own, so no later value can reach an earlier one through a running total.
        out[window:] = np.lib.stride_tricks.sliding_window_view(log_returns, window).std(axis=1, ddof=1)
    return out


def _trend_flags(closes: np.ndarray, log_returns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """What each close says: trending or ranging, from x. Both False where the 61 closes don't all exist."""
    s60 = _rolling_std(log_returns, TREND_WINDOW, closes.size)
    x = np.full(closes.size, np.nan)
    if closes.size > TREND_WINDOW:
        drift = np.abs(np.log(closes[TREND_WINDOW:] / closes[:-TREND_WINDOW]))
        with np.errstate(divide="ignore", invalid="ignore"):  # Prices that don't move give 0/0.
            x[TREND_WINDOW:] = drift / (s60[TREND_WINDOW:] * math.sqrt(TREND_WINDOW))
    trending = x >= 1
    return trending, ~np.isnan(s60) & ~trending


def _vol_flags(closes: np.ndarray, log_returns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """What each close says: high or low volatility, from v against its trailing median."""
    v = _rolling_std(log_returns, VOL_WINDOW, closes.size)
    high, low = np.zeros(closes.size, dtype=bool), np.zeros(closes.size, dtype=bool)
    for s in np.flatnonzero(~np.isnan(v)):
        window = v[max(0, s - MEDIAN_WINDOW + 1) : s + 1]
        window = window[~np.isnan(window)]  # A NaN v is skipped, for the reason in the notes at the top.
        if window.size >= MIN_MEDIAN_VALUES:
            high[s] = v[s] > np.median(window)
            low[s] = not high[s]
    return high, low


def regime_labels(btc_close, ts) -> dict:
    """Bool arrays for trending, ranging, high_vol and low_vol, each saying which label applies to day t's return.

    Each comes from closes up to t-1. A pair is False on a day it has too little history, or a NaN in a window.
    """
    closes, day = _on_calendar(btc_close, ts)
    log_returns = np.log(closes[1:] / closes[:-1])  # A NaN close makes both returns that touch it NaN.
    flags = dict(zip(REGIMES, (*_trend_flags(closes, log_returns), *_vol_flags(closes, log_returns))))
    labels = {}
    for name, says in flags.items():
        # says[s] is known at the close of s, so it can only apply to the day after.
        applies = np.zeros(closes.size, dtype=bool)
        applies[1:] = says[:-1]
        labels[name] = applies[day]
    return labels


def _regime_figures(returns: np.ndarray) -> dict:
    days = int(returns.size)
    annual_return = annualise(compound(returns), days) if days else math.nan
    sharpe = annual_sharpe(returns)
    enough = days >= MIN_REGIME_DAYS
    return {
        "days": days,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "works": enough and annual_return > 0 and sharpe > 0,
        # Written as "not all clear" so that a NaN fails. "sharpe < -1.0 or ..." would let a NaN pass as not failing.
        "fails_badly": enough and not (sharpe >= BAD_SHARPE and annual_return >= BAD_ANNUAL_RETURN),
    }


def by_regime(returns, labels) -> dict:
    """Each of the 4 regimes' days, annual_return, sharpe, works and fails_badly, over the days labelled with it.

    returns and the labels must line up day for day. labels from regime_labels on a whole panel need cutting to the
    Result's rows first.
    """
    returns = np.asarray(returns, dtype=np.float64)
    out = {}
    for name in REGIMES:
        mask = np.asarray(labels[name], dtype=bool)
        if mask.shape != returns.shape or returns.ndim != 1:
            raise ValueError(f"{name} has shape {mask.shape} and the returns {returns.shape}")
        out[name] = _regime_figures(returns[mask])
    return out
