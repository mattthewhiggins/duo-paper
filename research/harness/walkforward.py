"""Section 10's walk-forward, a family's honest number: pick on the past, record the future, pay to change your mind.

Where the rules leave a choice open, these are the readings taken:

- "At least lookback_years of rows" is by the calendar: the first row must be on or before the date lookback_years
  before the step. Rows from 2018-07-02 aren't two years before 2020-07-01, so the first step is then 2021-01-01.
- The first step records the chosen variant from a position it entered earlier, and the specification charges nothing
  for that. So the stitched series starts already invested, without the one entry a new account would pay.
  charge_first_entry=True prices that entry as a switch from cash, and section 11's selection uses it (Amendment 6).
- A trailing step is recorded however few days are left, because a dropped stretch could be the bad one. A step with no
  rows in its period, which takes a gap of at least a whole step, has nothing to record and isn't a pick.
- Sharpes within 1e-12 count as tied, as in stats.py: a column and 3 times it have the same Sharpe on paper, but not
  always in floating point. A variant with a NaN Sharpe can't be picked.
- The charge comes off the step's first day as (1 + r) * (1 - cost) - 1, the way backtest_weights takes costs.
- "Growth so far" is the stitched account's, from 1.0 at the first step, with its earlier charges taken off. The time
  passed to the cost model is the step's first row.
- Between carry variants (info["carry"]) the formula prices only the spot leg, because that's what weights holds, while
  a real account also moves the short perp by the same weight. Amendment 2 says a switch pays what a real account would,
  so the change in such a variant's weight also pays the perp fee and half-spread, which takes that date's history
  multiplier (Amendment 6). This can only lower the result.
- fee_level is an addition to the specification's signature. Track R's cost model has no tier3_taker, so its
  walk-forward would raise without it.
- switch_costs lines up with picks: one fraction of equity per step, 0.0 where nothing was charged.
- A NaN weight at a switch raises. Read as no change it would make the switch free, the one way a NaN could improve a
  result. The engines write zeros for a dead account, so only a damaged Result can have one.
- A walk-forward of one day has no Sharpe, so sharpe, gap and best_in_sample's Sharpe are NaN, which fails every gate
  test.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from research.harness.data import _utc
from research.harness.engine import CAPITAL_GBP, _perp_bps
from research.harness.metrics import annual_sharpe, compound

_TIE = 1e-12


def _steps(ts: np.ndarray, step_months: int, lookback_years: int) -> list[tuple[int, int]]:
    """(step date, start of its lookback) for every step date with a full lookback and rows on or after it."""
    first_year = datetime.fromtimestamp(int(ts[0]), timezone.utc).year
    last_year = datetime.fromtimestamp(int(ts[-1]), timezone.utc).year
    steps = []
    for year in range(first_year + lookback_years, last_year + 1):
        for month in range(1, 13, step_months):
            date, lookback_start = _utc(year, month, 1), _utc(year - lookback_years, month, 1)
            if ts[0] <= lookback_start and date <= ts[-1]:
                steps.append((date, lookback_start))
    return steps


def _best(results: dict, names: list[str], rows: slice) -> tuple[str | None, float]:
    """The highest Sharpe over these rows, or (None, NaN). The names are sorted, so a tie stays with the first."""
    pick, best = None, math.nan
    for name in names:
        sharpe = annual_sharpe(results[name].returns[rows])
        if pick is None and not math.isnan(sharpe) or sharpe > best + _TIE:
            pick, best = name, sharpe
    return pick, best


def _weights_at(result, row: int) -> dict:
    return dict(zip(result.assets, result.weights[row].tolist()))


def _switch_cost(old, new, row: int, growth: float, stamp: int, cost_model, capital_gbp, fee_level) -> float:
    """What moving from one variant's weights at the previous close to the other's costs, as a fraction of equity.

    With no old variant, that's the move from cash.
    """
    w_old, w_new = _weights_at(old, row - 1) if old is not None else {}, _weights_at(new, row - 1)
    old_carry = old is not None and bool(old.info.get("carry"))
    cost = 0.0
    for asset in sorted(w_old.keys() | w_new.keys()):
        before, after = w_old.get(asset, 0.0), w_new.get(asset, 0.0)
        change = abs(after - before)
        if change != 0.0:  # Not "> 0", so a NaN reaches the cost model, which refuses it, instead of costing nothing.
            bps = cost_model.spot_cost_bps(asset, change * capital_gbp * growth, stamp, fee_level)
            cost += change * bps / 1e4
        perp_change = abs(after * bool(new.info.get("carry")) - before * old_carry)
        if perp_change > 0.0:
            cost += perp_change * _perp_bps(cost_model, stamp) / 1e4
    return cost


def walk_forward(results, *, cost_model, capital_gbp=CAPITAL_GBP, step_months=6, lookback_years=2,
                 fee_level="tier3_taker", charge_first_entry=False) -> dict:
    """At each step, the best after-cost Sharpe over the previous lookback_years, recorded until the next step.

    Returns {"ts", "returns", "picks": [(step_ts, name)], "sharpe", "best_in_sample": (name, sharpe), "gap",
    "switch_costs"}. best_in_sample is over the walk-forward's own days, and gap is its Sharpe less the stitched one's.
    """
    if not results:
        raise ValueError("walk_forward needs at least one variant")
    if step_months not in (1, 2, 3, 4, 6, 12):
        raise ValueError(f"step_months must divide a year, so steps fall on the same dates each year: {step_months}")
    if lookback_years != int(lookback_years) or lookback_years < 1:
        raise ValueError(f"lookback_years must be a whole number of 1 or more, got {lookback_years}")
    names = sorted(results)
    ts = results[names[0]].ts
    for name in names:
        if not np.array_equal(results[name].ts, ts):
            raise ValueError(f"{name} doesn't share {names[0]}'s ts")
    steps = _steps(ts, step_months, int(lookback_years)) if ts.size else []
    starts = np.searchsorted(ts, [date for date, _ in steps]).tolist()
    ends = [*starts[1:], ts.size]
    recorded = [index for index, (start, end) in enumerate(zip(starts, ends)) if start < end]
    if not recorded:
        raise ValueError(f"no step: {ts.size} rows don't reach a step date with {lookback_years} years before it")

    first_row = starts[recorded[0]]
    returns = np.array(results[names[0]].returns[first_row:], dtype=np.float64)
    picks, switch_costs = [], []
    growth = 1.0
    current = None
    for index in recorded:
        (date, lookback_start), start, end = steps[index], starts[index], ends[index]
        pick, _ = _best(results, names, slice(int(np.searchsorted(ts, lookback_start)), start))
        if pick is None:
            raise ValueError(f"no variant has a Sharpe over the {lookback_years} years before ts {date}")
        stretch = np.array(results[pick].returns[start:end], dtype=np.float64)
        cost = 0.0
        if pick != current and (current is not None or charge_first_entry):
            old = results[current] if current is not None else None
            cost = _switch_cost(old, results[pick], start, growth, int(ts[start]), cost_model, capital_gbp, fee_level)
            stretch[0] = (1.0 + stretch[0]) * max(0.0, 1.0 - cost) - 1.0
        returns[start - first_row : end - first_row] = stretch
        growth = max(0.0, growth * compound(stretch))
        picks.append((date, pick))
        switch_costs.append(cost)
        current = pick

    sharpe = annual_sharpe(returns)
    best_name, best_sharpe = _best(results, names, slice(first_row, ts.size))  # One day has no Sharpe, so that's NaN.
    return {
        "ts": np.array(ts[first_row:]),
        "returns": returns,
        "picks": picks,
        "sharpe": sharpe,
        "best_in_sample": (best_name, best_sharpe),
        "gap": best_sharpe - sharpe,
        "switch_costs": np.array(switch_costs),
    }
