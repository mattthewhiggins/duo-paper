"""Section 7's daily metrics, worked out from a Result, and the GBP view of one.

Nothing here reads a file or a price. Where the rules leave a choice open, these are the readings taken:

- A window with no days has no figures. Everything but days is NaN, which fails every gate test, where a drawdown of
  0.0 would pass one.
- One day has no std(ddof=1), so its sharpe is NaN, not the 0.0 a std of 0 gets.
- A std or downside deviation too small to be anything but rounding counts as 0. numpy gives identical returns a std
  near 1e-19 rather than 0, and dividing by that would report a Sharpe in the trillions.
- An account that reaches zero stays there, so two days below -100% can't multiply back into a profit.
- A NaN in the returns gives NaN figures. It's never skipped.
- gross_returns get the same currency move as returns, or cost_drag would mix two currencies. Turnover, exposure and
  weights are shares of equity, so they don't change.
- The GBP view pays the FX conversion once each way, on its first and last rows, because Amendment 5 of the
  preregistration says it does. The specification came first and has the first row use r alone, and the preregistration
  wins. It's a cost, so it goes in returns and costs but not gross_returns. USD results never carry it.
- trades counts all of info["trades"], as the specification says, even in a sub-window. open_exit_cost is the engines'
  figure for the end of the whole Result, and goes beside the others so it reaches the record. It changes none of them.
"""
from __future__ import annotations

import math

import numpy as np

from research.harness.result import Result

DAYS_PER_YEAR = 365
FX_CONVERSION_BPS = 20.0  # One way, from section 5 of the preregistration. Funding and withdrawing each pay it.
_ROUNDING = 1e-7  # A spread under this share of the returns' own size can't be told apart from rounding.
_FIGURES = (
    "total_return", "gross_total_return", "cagr", "gross_cagr", "cost_drag", "sharpe", "sortino", "max_drawdown",
    "calmar", "turnover", "avg_exposure", "total_costs",
)


def _is_rounding(spread: float, returns: np.ndarray) -> bool:
    # A real strategy's spread is the same order as its returns. A NaN compares false, so it stays a NaN.
    return spread <= _ROUNDING * math.sqrt(np.mean(returns * returns))


def compound(returns) -> float:
    """The product of (1 + r). Once a factor reaches zero the account is gone, whatever the days after it say."""
    factors = 1.0 + np.asarray(returns, dtype=np.float64)
    product = float(np.prod(factors))
    return min(product, 0.0) if (factors <= 0).any() else product


def annualise(growth: float, days: int) -> float:
    """growth ^ (365 / days) - 1. A total loss is -1, because a negative growth has no real power."""
    if growth <= 0:
        return -1.0
    with np.errstate(over="ignore"):  # Absurd growth over a few days becomes inf, not an exception.
        return float(np.power(growth, DAYS_PER_YEAR / days)) - 1.0


def annual_sharpe(returns) -> float:
    """mean / std(ddof=1) * sqrt(365). 0.0 if std is 0, and NaN under 2 days, which have no std."""
    returns = np.asarray(returns, dtype=np.float64)
    if returns.size < 2:
        return math.nan
    std = float(returns.std(ddof=1))
    if _is_rounding(std, returns):
        return 0.0
    return float(returns.mean()) / std * math.sqrt(DAYS_PER_YEAR)


def _annual_sortino(returns: np.ndarray) -> float:
    # The mean runs over all days, so a rare loss counts for less than the same loss every day.
    downside = math.sqrt(np.mean(np.minimum(returns, 0.0) ** 2))
    if _is_rounding(downside, returns):
        return 0.0
    return float(returns.mean()) * DAYS_PER_YEAR / (downside * math.sqrt(DAYS_PER_YEAR))


def _max_drawdown(returns: np.ndarray) -> float:
    # The curve starts at 1.0 before the first day, or a loss on the first day would be its own starting point.
    equity = np.cumprod(np.concatenate(([1.0], 1.0 + returns)))
    return float(np.max(1.0 - equity / np.maximum.accumulate(equity)))


def summarise(result: Result, start_ts=None, end_ts=None) -> dict:
    """Section 7's figures over the rows with start_ts <= ts < end_ts. A bound of None leaves that side open."""
    keep = np.ones(result.ts.size, dtype=bool)
    if start_ts is not None:
        keep &= result.ts >= start_ts
    if end_ts is not None:
        keep &= result.ts < end_ts
    days = int(keep.sum())
    if days == 0:
        out = {"days": 0, **dict.fromkeys(_FIGURES, math.nan)}
    else:
        returns = result.returns[keep]
        growth, gross_growth = compound(returns), compound(result.gross_returns[keep])
        cagr, gross_cagr = annualise(growth, days), annualise(gross_growth, days)
        drawdown = _max_drawdown(returns)
        out = {
            "days": days,
            "total_return": growth - 1.0,
            "gross_total_return": gross_growth - 1.0,
            "cagr": cagr,
            "gross_cagr": gross_cagr,
            "cost_drag": gross_cagr - cagr,
            "sharpe": annual_sharpe(returns),
            "sortino": _annual_sortino(returns),
            "max_drawdown": drawdown,
            "calmar": 0.0 if drawdown == 0 else cagr / drawdown,
            "turnover": float(result.turnover[keep].sum()) * DAYS_PER_YEAR / days,
            "avg_exposure": float(result.exposure[keep].mean()),
            "total_costs": float(result.costs[keep].sum()),
        }
    if "trades" in result.info:
        out["trades"] = len(result.info["trades"])
    if "open_exit_cost" in result.info:
        out["open_exit_cost"] = result.info["open_exit_cost"]
    return out


def gbp_view(result: Result, usd_per_gbp_ts, usd_per_gbp, *, conversion_bps=FX_CONVERSION_BPS) -> Result:
    """The same Result with returns in GBP: (1 + r) * rate[t-1] / rate[t] - 1, and r alone on the first row.

    rate is USD per GBP at each close. Each day takes the latest rate at or before its ts, so a NaN or a missing row is
    carried over and no later rate is ever read. A day with no such rate raises ValueError.

    The account converts once when funded and once when withdrawn, so the first and last rows each pay conversion_bps,
    and 0.0 gives the currency move alone. A view therefore covers one period: summarising part of it can leave a
    conversion out, so build a view for each period reported.
    """
    if not 0 <= conversion_bps < 1e4:
        raise ValueError(f"conversion_bps must be a share of equity in bps, got {conversion_bps}")
    rate_ts = np.asarray(usd_per_gbp_ts)
    rates = np.asarray(usd_per_gbp, dtype=np.float64)
    if rate_ts.ndim != 1 or rates.shape != rate_ts.shape:
        raise ValueError(f"rates have shape {rates.shape} and their times {rate_ts.shape}")
    # searchsorted on unsorted times would answer with the wrong day, without a word.
    if np.any(np.diff(rate_ts) <= 0):
        raise ValueError("rate times must be strictly increasing")
    known = ~np.isnan(rates)
    if not np.all(np.isfinite(rates[known]) & (rates[known] > 0)):
        raise ValueError("rates must be positive and finite, or NaN for a gap")
    rate_ts, rates = rate_ts[known], rates[known]
    latest = np.searchsorted(rate_ts, result.ts, side="right") - 1
    if latest.size and latest[0] < 0:
        raise ValueError(f"no rate at or before the first day, ts {result.ts[0]}")
    rate = rates[latest]
    returns, gross, costs = np.array(result.returns), np.array(result.gross_returns), np.array(result.costs)
    move = rate[:-1] / rate[1:]
    returns[1:] = (1.0 + returns[1:]) * move - 1.0
    gross[1:] = (1.0 + gross[1:]) * move - 1.0
    if conversion_bps and returns.size:
        kept = 1.0 - conversion_bps / 1e4
        for row in (0, -1):  # A one-day Result pays both on its one row.
            returns[row] = (1.0 + returns[row]) * kept - 1.0
            costs[row] = 1.0 - (1.0 - costs[row]) * kept  # Keeps 1 + r = (1 + gross) * (1 - cost) true on the row.
    return Result(
        f"{result.name}:gbp", result.ts, returns, gross, result.turnover, result.exposure, costs, result.weights,
        result.assets, dict(result.info),
    )
