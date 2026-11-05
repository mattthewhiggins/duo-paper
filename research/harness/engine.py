"""The backtest engines and the lookahead checks. research/HARNESS_SPEC.md is the contract for all of it.

Every result in this research comes through here. The engines read only what they are given: no files, no clock and
no randomness, so the same inputs give the same bytes.

Where the rules leave a choice open, these are the readings taken:

- backtest_weights: a delisting's haircut lowers the G that step 2's rescaling divides by, because that's the only way
  the other assets keep their units when equity falls. Its turnover is its weight at its last close, the h the haircut
  is computed on. A T x A band is read at row t-1, the row of the target it applies to.
- backtest_trades: "reduced to the cash available" counts a buy's cost as well as its notional, because section 6 of the
  preregistration allows no leverage and weights summing to at most 1, and that wins. So a buy and its cost together
  take at most the cash, and cash never goes below 0 from buying. A stop_pct must be strictly between 0 and 1: at 0 the
  stop is the entry price and fires at once, and at 1 or more it can never fire, so a variant would look protected when
  it isn't. A position whose asset has no further data is sold at its last close with the 5% haircut, as section 3 of
  the preregistration says for any held asset, with reason "delisted". Fills at a bar's open go in asset order, then
  stops inside the bar in asset order, so a buy at the open can't spend what a stop only frees up later in that bar. A
  cost_scale above 1 scales the stop slippage too, because "base costs x2" is all the costs, and one below 1 doesn't
  reduce it.
- backtest_carry: a forced exit pays the normal exit costs as well as the haircut, because it's an exit. An order
  waiting for a bar stays as decided, whatever the sleeve does in the meantime, so a flip during a gap pays for both
  trades. A sleeve above 0 at s-1 enters at the open of s, as in backtest_weights, because the run starts flat.
  Turnover counts both legs, as exposure does. Funding counts only on days the perp has a bar, as Amendment 5 item 5
  says, even if a panel that skipped cleaning holds a figure for such a day. That day and a NaN funding day earn
  nothing, and info counts them.
- All three: section 7 marks at the close, and the specification charges no exit for a position still open at the end,
  so a result has paid half of that round trip. info["open_exit_cost"] says what the other half would be, as a share of
  final equity, and it comes off nothing. A total return within it of zero isn't clear of zero.
- All three: info["notional_by_flag"] is {flag: the sum of traded weight that carried it}, and info["traded_notional"]
  is the sum of all traded weight, so a report can say what share of notional paid the 50 bps default. A delisting's in
  turnover but it isn't here, because it's no trade and pays no cost. In backtest_trades a share is of the previous
  day's end, as turnover is. In backtest_carry it's the spot leg alone, because that's what weights holds and the only
  leg with flags, so the default's share isn't thinned out by a perp leg that can never pay it.
- The lookahead checks: n_cuts counts the cuts, and the seed picks them, but "any time t" in section 6 can't be
  established by 18 random rows if a peek only shows on a cut ending on the row it changed. On a rare signal most cuts
  miss all of those, so two thirds of the random cuts are aimed at rows where an output turned on or off, shared between
  the outputs that do, and the rest are uniform. That's the one kind of row where an on-off signal's peek always shows
  if the peek reads False past the end of the data. One that reads True there shows on a turn-off, but not on a row
  where it kept a signal off all along, so nothing short of every row catches all of it. Pass n_cuts above the row count
  for that, which a daily panel can afford.
- A companion panel, such as the daily closes F4 takes from clean.spot_panel, is cut with the bars, to the bars of its
  own that had finished by the last bar's close. Merely started isn't enough: a day's row holds that day's close.
"""
from __future__ import annotations

import math

import numpy as np

from research.harness.data import _utc
from research.harness.panels import truncate
from research.harness.result import Result

EVAL_START_TS = _utc(2018, 7, 1)
DELIST_HAIRCUT = 0.05
STOP_SLIPPAGE_BPS = 10
CAPITAL_GBP = 20000

_DAY = 86400
_ROW_TOLERANCE = 1e-9
_ENTRY_DAY_FUNDING = 2 / 3  # Of the three daily settlements, the 00:00 one falls at or before a fill at the open.
_BUY, _SELL_AT_OPEN, _SELL_AT_STOP, _DELIST = range(4)  # The next thing due to happen to an asset in backtest_trades.
_SETTLE_STEPS = 100  # Far more than a real cost curve needs for a buy's size to settle. See buy in backtest_trades.
_SETTLED = 1e-15  # As a share of the cash, so it's still reachable by floats however far the account has grown.
_AIMED_SHARE = 2 / 3  # Of a lookahead check's random cuts, how many go to rows where an output turned on or off.


def _where(mask: np.ndarray, panel) -> str:
    """Names the first True cell, so an error points at the bad data."""
    row, column = np.argwhere(mask)[0]
    return f"row {row}, asset {panel.assets[column]}"


def _checked_shares(values, panel, label: str) -> np.ndarray:
    """Targets, sizes and sleeves are all shares of one account's equity, so the same rules hold for each."""
    values = np.asarray(values, dtype=np.float64)
    if values.shape != panel.close.shape:
        raise ValueError(f"{label} has shape {values.shape}, expected {panel.close.shape}")
    if np.isnan(values).any():
        raise ValueError(f"{label} has a NaN at {_where(np.isnan(values), panel)}")
    if (values < 0).any():
        raise ValueError(f"{label} is below 0 at {_where(values < 0, panel)}")
    above = values.sum(axis=1) > 1 + _ROW_TOLERANCE
    if above.any():
        raise ValueError(f"{label} sums above 1 at row {int(above.argmax())}")
    return values


def _checked_band(band, shape: tuple[int, int]) -> np.ndarray:
    band = np.asarray(band, dtype=np.float64)
    if band.ndim == 0:
        band = np.full(shape, float(band))
    if band.shape != shape:
        raise ValueError(f"band has shape {band.shape}, expected a scalar or {shape}")
    # abs(change) > NaN is never true and abs(change) > -1 always is, so either would quietly change the trading.
    if np.isnan(band).any() or (band < 0).any():
        raise ValueError("band must be 0 or more, with no NaN")
    return band


def _check_cost_inputs(capital_gbp, cost_scale, fee_bps) -> None:
    # A negative or NaN cost would flatter the result without a word.
    if not 0 < capital_gbp < math.inf:
        raise ValueError(f"capital_gbp must be above 0, got {capital_gbp}")
    if not 0 <= cost_scale < math.inf:
        raise ValueError(f"cost_scale must be 0 or more, got {cost_scale}")
    if fee_bps is not None and not 0 <= fee_bps < math.inf:
        raise ValueError(f"fee_bps must be 0 or more, got {fee_bps}")


def _check_daily(panel) -> None:
    # A Result has one row per bar here, and its returns are read as daily. A day with no row at all is allowed.
    if (np.diff(panel.ts) % _DAY).any():
        raise ValueError(f"{panel.name} must be daily, but its bars aren't whole days apart")


def _check_prices(panel) -> None:
    # Prices get divided by, and a zero would turn a return into NaN or infinity many rows later.
    for label, values in (("open", panel.open), ("close", panel.close)):
        if (values <= 0).any():
            raise ValueError(f"{label} isn't positive at {_where(values <= 0, panel)}")


def _first_row(ts: np.ndarray, start_ts) -> int:
    s = int(np.searchsorted(ts, start_ts, side="left"))
    if s == ts.size:
        raise ValueError(f"no bar at or after start_ts {start_ts}")
    return s


def _forward_filled(values: np.ndarray) -> np.ndarray:
    """Each column's most recent non-NaN value at or before each row. NaN before its first."""
    latest = np.maximum.accumulate(np.where(np.isnan(values), -1, np.arange(values.shape[0])[:, None]), axis=0)
    filled = values[latest, np.arange(values.shape[1])]
    filled[latest < 0] = np.nan
    return filled


def _last_row_with_data(values: np.ndarray) -> np.ndarray:
    """Each column's last non-NaN row, -1 if it has none."""
    rows = np.where(np.isnan(values), -1, np.arange(values.shape[0])[:, None])
    return rows.max(axis=0, initial=-1)


def _spot_bps(cost_model, asset, notional_gbp, ts, fee_level, fee_bps, flags: dict, by_flag=None, weight=0.0) -> float:
    """One trade's one-way cost in bps, before cost_scale. The flags are counted here, not read from the model, so a
    model used for two runs doesn't change the second run's output. by_flag adds up the weight traded under each
    flag."""
    detail = cost_model.spot_cost_detail(asset, notional_gbp, ts, fee_level)
    for flag in detail.flags:
        key = f"{asset}:{flag}"
        flags[key] = flags.get(key, 0) + 1
        if by_flag is not None:
            by_flag[flag] = by_flag.get(flag, 0.0) + weight
    if fee_bps is None:
        return detail.total_bps
    return fee_bps + detail.fx_bps + detail.spread_impact_bps


def _perp_bps(cost_model, ts) -> float:
    """The perp's one-way cost in bps on a date. Its half-spread takes the history multiplier, and its fee doesn't."""
    return cost_model.perp_fee_bps + cost_model.perp_half_spread_bps * cost_model.multiplier(ts)


def _exit_cost(cost_model, assets, weights, equity, ts, capital_gbp, fee_level, cost_scale, fee_bps) -> float:
    """The cost of selling these weights at the last close, as a share of equity. Nobody pays it, so no flag counts."""
    if equity <= 0.0:
        return 0.0
    cost = 0.0
    for a in np.flatnonzero(weights > 0.0).tolist():
        size = float(weights[a])
        detail = cost_model._priced(assets[a], size * equity * capital_gbp, ts, fee_level)
        bps = detail.total_bps if fee_bps is None else fee_bps + detail.fx_bps + detail.spread_impact_bps
        cost += size * bps * cost_scale / 1e4
    return cost


def backtest_weights(panel, target, *, name="", band=0.0, start_ts=EVAL_START_TS, capital_gbp=CAPITAL_GBP,
                     cost_model, fee_level="tier3_taker", cost_scale=1.0, fee_bps=None) -> Result:
    """A daily portfolio. target[t] is decided at the close of t and executes at the open of t+1."""
    target = _checked_shares(target, panel, "target")
    bands = _checked_band(band, panel.close.shape)
    _check_cost_inputs(capital_gbp, cost_scale, fee_bps)
    _check_daily(panel)
    _check_prices(panel)
    no_open, no_close = np.isnan(panel.open), np.isnan(panel.close)
    if (no_open != no_close).any():
        raise ValueError(f"only one of open and close is NaN at {_where(no_open != no_close, panel)}")
    if ((target > 0) & no_close).any():
        raise ValueError(f"target is above 0 with no close at {_where((target > 0) & no_close, panel)}")

    rows, columns = target.shape
    s = _first_row(panel.ts, start_ts)
    stamps = panel.ts.tolist()
    has_bar = ~no_close
    last_close = _forward_filled(panel.close)
    # Worked out once for every cell, 0 where an asset has no bar, so a held asset with no bar just doesn't move.
    gaps = np.zeros((rows, columns))
    gaps[1:] = panel.open[1:] / last_close[:-1] - 1.0
    gaps[np.isnan(gaps)] = 0.0
    moves = panel.close / panel.open - 1.0
    moves[np.isnan(moves)] = 0.0
    no_more_closes = np.arange(rows)[:, None] > _last_row_with_data(panel.close)

    returns, gross_returns, turnover = np.zeros(rows - s), np.zeros(rows - s), np.zeros(rows - s)
    exposure, costs = np.zeros(rows - s), np.zeros(rows - s)
    weights = np.zeros((rows - s, columns))
    delistings, flags, by_flag = [], {}, {}
    traded_notional = 0.0
    nothing = np.zeros(columns)  # Bar 0 has no bar before it. target[-1] would be the last row, which is the future.
    h = np.zeros(columns)
    equity = 1.0
    for t in range(s, rows):
        i = t - s
        if equity <= 0.0:  # Costs took everything, and there's nothing left to trade.
            continue
        wanted = target[t - 1] if t else nothing
        limit = bands[t - 1] if t else nothing

        # 1. Decide, on the weights at the last close. An asset with no bar can't trade, so its order is dropped.
        trading = has_bar[t] & ((np.abs(wanted - h) > limit) | ((wanted == 0.0) & (h > 0.0)))

        # 2 and 3. The gap, and any delisting. Both change equity, so the rescaling uses the G that includes both.
        g = gaps[t]
        G = 1.0 + (h * g).sum()
        delisted = (h > 0.0) & no_more_closes[t]
        delisted_weight = 0.0
        if delisted.any():
            delisted_weight = float(h[delisted].sum())
            G -= DELIST_HAIRCUT * delisted_weight
            delistings.extend((panel.assets[a], stamps[t]) for a in np.flatnonzero(delisted).tolist())
        h = h * (1.0 + g) / G
        h[delisted] = 0.0

        # 4. Trade. Assets left out by the band can push the sum over 1, so only the buys give way.
        w = np.where(trading, wanted, h)
        total = w.sum()
        if total > 1.0:
            buying = w > h
            rise = (w[buying] - h[buying]).sum()
            if rise > 0.0:
                w[buying] = h[buying] + (w[buying] - h[buying]) * max(0.0, 1.0 - (total - 1.0) / rise)
        d = w - h
        before_costs = equity * G
        cost = 0.0
        for a in np.flatnonzero(d).tolist():
            size = abs(float(d[a]))
            bps = _spot_bps(cost_model, panel.assets[a], size * before_costs * capital_gbp, stamps[t], fee_level,
                            fee_bps, flags, by_flag, size)
            cost += size * bps * cost_scale / 1e4
            traded_notional += size

        # 5. Through the day to the close.
        r = moves[t]
        I = 1.0 + (w * r).sum()
        h = w * (1.0 + r) / I

        # 6. Record. Costs above all of equity are a total loss, not a negative balance.
        kept = max(0.0, 1.0 - cost)
        returns[i] = G * kept * I - 1.0
        gross_returns[i] = G * I - 1.0
        costs[i] = cost
        turnover[i] = np.abs(d).sum() + delisted_weight
        exposure[i] = h.sum()
        weights[i] = h
        equity *= G * kept * I

    info = {
        "delistings": delistings,
        "cost_flags": dict(sorted(flags.items())),
        "open_exit_cost": _exit_cost(
            cost_model, panel.assets, h, equity, stamps[-1], capital_gbp, fee_level, cost_scale, fee_bps
        ),
        "notional_by_flag": dict(sorted(by_flag.items())),
        "traded_notional": traded_notional,
    }
    return Result(name, panel.ts[s:], returns, gross_returns, turnover, exposure, costs, weights, panel.assets, info)


def _checked_signal(values, panel, label: str) -> np.ndarray:
    values = np.asarray(values)
    if values.shape != panel.close.shape:
        raise ValueError(f"{label} has shape {values.shape}, expected {panel.close.shape}")
    # Coercing would turn a NaN from an unfinished signal into True, which is a trade nobody decided on.
    if values.dtype != np.bool_:
        raise ValueError(f"{label} must be a bool array, not {values.dtype}")
    return values


def _next_true(condition: np.ndarray) -> list[int]:
    """For each row, the first row at or after it where the condition holds. One extra entry so row T can be asked."""
    rows = condition.size
    index = np.where(condition, np.arange(rows), rows)
    return np.r_[np.minimum.accumulate(index[::-1])[::-1], rows].tolist()


def _first_at_or_below(values: np.ndarray, first: int, last: int, level: float) -> int:
    """The first row in [first, last] with values[row] <= level, or -1.

    The blocks grow as they go, because the answer is usually close and a position's whole future shouldn't be
    compared for each trade, while a position with no event for a long time still costs only a few numpy calls.
    """
    block = 64
    while first <= last:
        end = min(first + block, last + 1)
        hits = values[first:end] <= level
        at = hits.argmax()
        if hits[at]:
            return first + int(at)
        first = end
        block *= 4
    return -1


def _values_at(event_rows: list[int], event_values: list[float], rows: np.ndarray) -> np.ndarray:
    """What a value held at the end of each row, from a list of changes that always has one before the first row."""
    return np.asarray(event_values)[np.searchsorted(event_rows, rows, side="right") - 1]


def _trades_daily(panel, s, marks, cash_rows, cash_values, unit_rows, unit_values, fill_rows, fill_costs, fill_dollars):
    """The daily arrays from backtest_trades' ledger. Equity is only needed at each day's last bar."""
    days = panel.ts[s:] // _DAY
    last_of_day = np.r_[days[1:] != days[:-1], True]
    ends = s + np.flatnonzero(last_of_day)
    count = ends.size
    day_of_row = np.cumsum(np.r_[False, last_of_day[:-1]])

    held = np.zeros((count, len(panel.assets)))
    for a in range(held.shape[1]):
        held[:, a] = _values_at(unit_rows[a], unit_values[a], ends)
    values = held * marks[ends]
    values[held == 0.0] = 0.0  # Flat in an asset that hasn't started yet: 0 units of a NaN price is nothing.
    equity = _values_at(cash_rows, cash_values, ends) + values.sum(axis=1)
    if not np.isfinite(equity).all():
        raise ValueError(f"{panel.name}: a position has no close to be marked at")

    # Buying can't take cash below 0, but a sale can if its cost is more than the sale brings in, as with a fee above
    # 100%. The account is gone, and stays gone.
    alive = np.minimum.accumulate(equity > 0.0)
    equity = np.where(alive, equity, 0.0)
    previous = np.r_[1.0, equity[:-1]]
    open_days = previous > 0.0
    returns = np.divide(equity, previous, out=np.ones(count), where=open_days) - 1.0

    where_filled = day_of_row[np.asarray(fill_rows, dtype=np.int64) - s]
    day_costs = np.bincount(where_filled, weights=np.asarray(fill_costs, dtype=np.float64), minlength=count)
    day_dollars = np.bincount(where_filled, weights=np.asarray(fill_dollars, dtype=np.float64), minlength=count)
    costs = np.divide(day_costs, previous, out=np.zeros(count), where=open_days)
    turnover = np.divide(day_dollars, previous, out=np.zeros(count), where=open_days)
    weights = np.divide(values, equity[:, None], out=np.zeros(values.shape), where=equity[:, None] > 0.0)
    fill_shares = np.asarray(fill_dollars, dtype=np.float64) / np.where(open_days, previous, np.inf)[where_filled]
    exposure = weights.sum(axis=1)
    return days[last_of_day] * _DAY, returns, returns + costs, turnover, exposure, costs, weights, fill_shares


def backtest_trades(panel, entry, exit, size, *, stop_pct, max_bars=None, name="", start_ts=EVAL_START_TS,
                    capital_gbp=CAPITAL_GBP, cost_model, fee_level="tier3_taker", cost_scale=1.0, fee_bps=None,
                    stop_slippage_bps=STOP_SLIPPAGE_BPS) -> Result:
    """One long position at a time per asset, on intraday bars, with hard stops.

    It jumps from one event to the next instead of visiting every bar: nothing can change between events, so each
    asset's next fill is found once, when its last one happens, and the daily marks are worked out at the end.
    """
    entry = _checked_signal(entry, panel, "entry")
    exit = _checked_signal(exit, panel, "exit")
    size = _checked_shares(size, panel, "size")
    _check_cost_inputs(capital_gbp, cost_scale, fee_bps)
    _check_prices(panel)
    stop_pct = float(stop_pct)
    # At or below 0 it sits at or above the entry, and fills in the first bar. At 1 or more its price is 0 or less, so
    # it can never fill, and the variant would look stop-protected when it isn't. The comparison also refuses NaN.
    if not 0 < stop_pct < 1:
        raise ValueError(f"stop_pct must be strictly between 0 and 1, got {stop_pct}")
    if not 0 <= stop_slippage_bps < math.inf:
        raise ValueError(f"stop_slippage_bps must be 0 or more, got {stop_slippage_bps}")
    if max_bars is not None:
        if max_bars != int(max_bars) or max_bars < 1:
            raise ValueError(f"max_bars must be None or a whole number of 1 or more, got {max_bars}")
        max_bars = int(max_bars)

    rows, columns = panel.close.shape
    s = _first_row(panel.ts, start_ts)
    assets = panel.assets
    stamps = panel.ts.tolist()
    # Lists, because reading one value from a list is many times faster than reading it from an array.
    opens = panel.open.T.tolist()
    lows = panel.low.T.tolist()
    marks = _forward_filled(panel.close)
    last_marks = marks.T.tolist()
    # A bar with no open is a NaN bar, and a position isn't stop-checked on one, whatever its low says.
    stop_levels = np.where(np.isnan(panel.open), np.nan, np.fmin(panel.open, panel.low)).T.copy()
    has_open = ~np.isnan(panel.open)
    signal = entry & (size > 0.0)  # Lookups start from s, so a signal before the window is never seen.
    signal[:-1] &= has_open[1:]
    signal[-1:] = False
    next_signal = [_next_true(signal[:, a]) for a in range(columns)]
    next_exit = [_next_true(exit[:, a]) for a in range(columns)]
    next_open = [_next_true(has_open[:, a]) for a in range(columns)]
    # The first row after an asset's last data. It's past the end of the panel if the asset never stops.
    delist_rows = (np.maximum(_last_row_with_data(panel.open), _last_row_with_data(panel.close)) + 1).tolist()

    # Dollars are in units of starting equity, so a trade's notional in GBP is simply dollars x capital_gbp: dollars
    # as a fraction of equity, times equity as the account's growth so far.
    cash = 1.0
    units = [0.0] * columns
    cash_rows, cash_values = [s - 1], [1.0]
    unit_rows = [[s - 1] for _ in range(columns)]
    unit_values = [[0.0] for _ in range(columns)]
    fill_rows, fill_costs, fill_dollars, fill_flags = [], [], [], []
    trades, delistings, flags = [], [], {}

    # Per asset: the row of its next event, what that event is, and the open position's details.
    next_row = [next_signal[a][s] + 1 for a in range(columns)]
    next_kind = [_BUY] * columns
    next_reason = [""] * columns
    entry_rows, entry_prices, entry_sizes = [0] * columns, [0.0] * columns, [0.0] * columns
    entry_costs, stops = [0.0] * columns, [0.0] * columns

    def plan_entry(a, flat_from):
        # Flat at the close of flat_from, so a signal from that bar on can buy at the next open.
        next_row[a] = next_signal[a][flat_from] + 1
        next_kind[a] = _BUY

    def plan_close(a, opened):
        # What ends this position first. Its stop is live through every bar up to and including the exit decision.
        stop = stops[a]
        decision = next_exit[a][opened]
        reason = "exit"
        if max_bars is not None and opened + max_bars - 1 < decision:  # Exit wins if both fall on one close.
            decision = opened + max_bars - 1
            reason = "time"
        stopped = opened if lows[a][opened] <= stop else _first_at_or_below(
            stop_levels[a], opened + 1, min(decision, rows - 1), stop)
        if stopped >= 0:
            gapped = stopped > opened and opens[a][stopped] <= stop
            next_row[a], next_kind[a], next_reason[a] = stopped, _SELL_AT_OPEN if gapped else _SELL_AT_STOP, "stop"
            return
        # Any bars before the next open are NaN bars, so nothing can stop it on the way to that market sell.
        fill = next_open[a][decision + 1] if decision + 1 < rows else rows
        if fill < rows:
            next_row[a], next_kind[a], next_reason[a] = fill, _SELL_AT_OPEN, reason
        else:
            next_row[a], next_kind[a] = delist_rows[a], _DELIST

    def post(a, row, dollars, paid, paid_flags):
        # paid_flags is None for a delisting: it's in turnover, but it's no trade and pays no cost.
        fill_flags.append(paid_flags)
        cash_rows.append(row)
        cash_values.append(cash)
        unit_rows[a].append(row)
        unit_values[a].append(units[a])
        fill_rows.append(row)
        fill_costs.append(paid)
        fill_dollars.append(dollars)

    def record_trade(a, row, price, reason, paid):
        trades.append({
            "asset": assets[a], "entry_ts": stamps[entry_rows[a]], "entry_price": entry_prices[a],
            "exit_ts": stamps[row], "exit_price": price, "reason": reason, "size": entry_sizes[a],
            "cost": entry_costs[a] + paid,
        })

    def buy_cost(a, row, dollars):
        # Its flags come back separately, because a size that's then reduced was never traded, so they don't count.
        counted, by_flag = {}, {}
        bps = _spot_bps(cost_model, assets[a], dollars * capital_gbp, stamps[row], fee_level, fee_bps, counted, by_flag)
        return dollars * bps * cost_scale / 1e4, counted, tuple(by_flag)

    def buy(a, row, equity_at_close):
        nonlocal cash
        share = float(size[row - 1, a])
        dollars = min(share * equity_at_close, cash)
        if dollars <= 0.0:  # No cash left, so the order comes to nothing and the asset stays flat.
            plan_entry(a, row)
            return
        price = opens[a][row]
        paid, counted, paid_flags = buy_cost(a, row, dollars)
        if dollars + paid > cash:
            # No leverage (section 6), so the cost has to come out of the cash as well. A smaller buy can have a lower
            # cost rate, so this repeats until its size settles. Then the buy and its cost take all the cash, exactly,
            # so no dust is left for another asset to open a position with.
            for _ in range(_SETTLE_STEPS):
                settled = cash * dollars / (dollars + paid)
                if abs(settled - dollars) <= _SETTLED * cash:
                    break
                dollars = settled
                paid, counted, paid_flags = buy_cost(a, row, dollars)
            paid = cash - dollars
            cash = 0.0
        else:
            cash -= dollars + paid
        for key, count in counted.items():
            flags[key] = flags.get(key, 0) + count
        units[a] = dollars / price
        entry_rows[a], entry_prices[a], entry_costs[a] = row, price, paid
        entry_sizes[a] = share if dollars == share * equity_at_close else dollars / equity_at_close
        stops[a] = price * (1.0 - stop_pct)
        post(a, row, dollars, paid, paid_flags)
        plan_close(a, row)

    def sell(a, row, price):
        nonlocal cash
        reason = next_reason[a]
        dollars = units[a] * price
        by_flag = {}
        bps = _spot_bps(cost_model, assets[a], dollars * capital_gbp, stamps[row], fee_level, fee_bps, flags, by_flag)
        paid = dollars * bps * cost_scale / 1e4
        if reason == "stop":
            # "Base costs x2" is all the costs, so a higher scale raises the slippage, and a lower one leaves it alone.
            paid += dollars * stop_slippage_bps * max(1.0, cost_scale) / 1e4
        cash += dollars - paid
        units[a] = 0.0
        post(a, row, dollars, paid, tuple(by_flag))
        record_trade(a, row, price, reason, paid)
        plan_entry(a, row)

    def delist(a, row):
        # No fee and no further bar: its proceeds are its last close less the haircut, which isn't a cost.
        nonlocal cash
        price = last_marks[a][row - 1]
        dollars = units[a] * price
        cash += dollars * (1.0 - DELIST_HAIRCUT)
        units[a] = 0.0
        delistings.append((assets[a], stamps[row]))
        post(a, row, dollars, 0.0, None)
        record_trade(a, row, price * (1.0 - DELIST_HAIRCUT), "delisted", 0.0)
        plan_entry(a, row)

    while True:
        row = min(next_row, default=rows)
        if row >= rows:
            break
        # Before anything fills at this open, because size[t] is a share of equity at the close of t.
        equity_at_close = cash
        for a in range(columns):
            if units[a]:
                equity_at_close += units[a] * last_marks[a][row - 1]
        # At the open, in asset order.
        for a in range(columns):
            if next_row[a] == row:
                kind = next_kind[a]
                if kind == _BUY:
                    buy(a, row, equity_at_close)
                elif kind == _SELL_AT_OPEN:
                    sell(a, row, opens[a][row])
                elif kind == _DELIST:
                    delist(a, row)
        # Then inside the bar, in asset order, including anything just bought.
        for a in range(columns):
            if next_row[a] == row and next_kind[a] == _SELL_AT_STOP:
                sell(a, row, stops[a])

    # Still open at the last bar: marked at that close, with no exit cost.
    final_dollars = np.zeros(columns)
    for a in range(columns):
        if units[a]:
            record_trade(a, rows - 1, last_marks[a][rows - 1], "end", 0.0)
            final_dollars[a] = units[a] * last_marks[a][rows - 1]
    final_equity = cash + final_dollars.sum()
    open_exit = _exit_cost(cost_model, assets, final_dollars / final_equity if final_equity > 0.0 else final_dollars,
                           final_equity, stamps[-1], capital_gbp, fee_level, cost_scale, fee_bps)
    order = {asset: a for a, asset in enumerate(assets)}
    trades.sort(key=lambda trade: (trade["entry_ts"], order[trade["asset"]]))

    day_ts, returns, gross_returns, turnover, exposure, costs, weights, fill_shares = _trades_daily(
        panel, s, marks, cash_rows, cash_values, unit_rows, unit_values, fill_rows, fill_costs, fill_dollars)
    notional_by_flag, traded_notional = {}, 0.0
    for share, paid_flags in zip(fill_shares.tolist(), fill_flags):
        if paid_flags is not None:
            traded_notional += share
            for flag in paid_flags:
                notional_by_flag[flag] = notional_by_flag.get(flag, 0.0) + share
    info = {
        "trades": trades,
        "delistings": delistings,
        "cost_flags": dict(sorted(flags.items())),
        "open_exit_cost": open_exit,
        "notional_by_flag": dict(sorted(notional_by_flag.items())),
        "traded_notional": traded_notional,
    }
    return Result(name, day_ts, returns, gross_returns, turnover, exposure, costs, weights, assets, info)


def _matched_spot(spot_panel, perp_panel) -> tuple[np.ndarray, np.ndarray]:
    """Spot's open and close on the perp panel's rows and columns, NaN where spot has no such bar."""
    missing = [asset for asset in perp_panel.assets if asset not in spot_panel.assets]
    if missing:
        raise ValueError(f"{spot_panel.name} has no spot column for {missing}")
    columns = [spot_panel.assets.index(asset) for asset in perp_panel.assets]
    _, at_perp, at_spot = np.intersect1d(perp_panel.ts, spot_panel.ts, assume_unique=True, return_indices=True)
    spot_open, spot_close = np.full(perp_panel.close.shape, np.nan), np.full(perp_panel.close.shape, np.nan)
    spot_open[at_perp] = spot_panel.open[at_spot][:, columns]
    spot_close[at_perp] = spot_panel.close[at_spot][:, columns]
    return spot_open, spot_close


def backtest_carry(spot_panel, perp_panel, sleeves, *, name="", start_ts, capital_gbp=CAPITAL_GBP, cost_model,
                   fee_level="tier3_taker", cost_scale=1.0, fee_bps=None) -> Result:
    """Long spot, short perp, daily. The one place shorts exist, so info says so, and liquidation isn't modelled."""
    sleeves = _checked_shares(sleeves, perp_panel, "sleeves")
    _check_cost_inputs(capital_gbp, cost_scale, fee_bps)
    _check_daily(perp_panel)
    _check_prices(spot_panel)
    _check_prices(perp_panel)
    if "funding_sum" not in perp_panel.extras:
        raise ValueError(f"{perp_panel.name} has no funding_sum")
    spot_open, spot_close = _matched_spot(spot_panel, perp_panel)
    perp_open, perp_close = perp_panel.open, perp_panel.close

    rows, columns = sleeves.shape
    s = _first_row(perp_panel.ts, start_ts)
    assets = perp_panel.assets
    stamps = perp_panel.ts.tolist()
    no_perp_bar = np.isnan(perp_open) | np.isnan(perp_close)
    perp_has_a_bar = (~no_perp_bar).tolist()
    both_have_a_bar = (~(np.isnan(spot_open) | np.isnan(spot_close) | no_perp_bar)).tolist()
    last_both_close = np.minimum(_last_row_with_data(spot_close), _last_row_with_data(perp_close)).tolist()
    spot_last, perp_last = _forward_filled(spot_close).tolist(), _forward_filled(perp_close).tolist()
    spot_opens, perp_opens = spot_open.tolist(), perp_open.tolist()
    funding_sum = perp_panel.extras["funding_sum"].tolist()
    sleeves_rows = sleeves.tolist()

    returns, gross_returns, turnover = np.zeros(rows - s), np.zeros(rows - s), np.zeros(rows - s)
    exposure, costs = np.zeros(rows - s), np.zeros(rows - s)
    weights = np.zeros((rows - s, columns))
    delistings, flags, by_flag = [], {}, {}
    traded_notional = 0.0
    funding_gaps = 0
    units = [0.0] * columns
    # Orders decided but not yet filled, because a leg has no bar: the entry's spot dollars, and whether an exit is due.
    entry_due, exit_due = [0.0] * columns, [False] * columns
    equity = 1.0

    def legs_cost(a, spot_dollars, perp_dollars, t):
        # Each leg pays on its own notional, and cost_scale covers both. fee_bps reaches only the spot fee.
        nonlocal traded_notional
        share = spot_dollars / day_start
        bps = _spot_bps(cost_model, assets[a], spot_dollars * capital_gbp, stamps[t], fee_level, fee_bps, flags,
                        by_flag, share)
        traded_notional += share
        return (spot_dollars * bps + perp_dollars * _perp_bps(cost_model, stamps[t])) * cost_scale / 1e4

    for t in range(s, rows):
        i = t - s
        if equity <= 0.0:
            continue
        day_start = equity
        pnl = cost = traded = 0.0
        for a in range(columns):
            sleeve = sleeves_rows[t - 1][a] if t else 0.0
            held = units[a]
            if held > 0.0 and sleeve == 0.0:
                exit_due[a] = True
            elif held == 0.0 and sleeve > 0.0 and not entry_due[a]:
                entry_due[a] = sleeve * day_start / 2.0
            # Funding is taken from the real series, so a day it doesn't cover earns nothing, and info counts it. Nor
            # does a day the perp has no bar, whatever figure the panel holds for it (Amendment 5, item 5).
            funding = funding_sum[t][a]
            funding_known = perp_has_a_bar[t][a] and not math.isnan(funding)
            if not funding_known:
                funding = 0.0
            if held > 0.0 and t > last_both_close[a]:
                # A leg has no further close, so both go at their last marks with the haircut on both notionals.
                spot_dollars, perp_dollars = held * spot_last[t - 1][a], held * perp_last[t - 1][a]
                pnl -= DELIST_HAIRCUT * (spot_dollars + perp_dollars)
                cost += legs_cost(a, spot_dollars, perp_dollars, t)
                traded += spot_dollars + perp_dollars
                units[a], exit_due[a] = 0.0, False
                delistings.append((assets[a], stamps[t]))
            elif held > 0.0 and exit_due[a] and both_have_a_bar[t][a]:
                # Out at the opens. The day's settlements come after the fill, so no funding.
                spot_price, perp_price = spot_opens[t][a], perp_opens[t][a]
                pnl += held * (spot_price - spot_last[t - 1][a]) - held * (perp_price - perp_last[t - 1][a])
                cost += legs_cost(a, held * spot_price, held * perp_price, t)
                traded += held * spot_price + held * perp_price
                units[a], exit_due[a] = 0.0, False
            elif held > 0.0:
                funding_gaps += not funding_known
                pnl += (held * (spot_last[t][a] - spot_last[t - 1][a]) - held * (perp_last[t][a] - perp_last[t - 1][a])
                        + held * perp_last[t][a] * funding)
            elif entry_due[a] and both_have_a_bar[t][a]:
                funding_gaps += not funding_known
                spot_price, perp_price = spot_opens[t][a], perp_opens[t][a]
                held = entry_due[a] / spot_price
                units[a], entry_due[a] = held, 0.0
                pnl += (held * (spot_last[t][a] - spot_price) - held * (perp_last[t][a] - perp_price)
                        + held * perp_last[t][a] * funding * _ENTRY_DAY_FUNDING)
                cost += legs_cost(a, held * spot_price, held * perp_price, t)
                traded += held * spot_price + held * perp_price

        equity = max(0.0, day_start + pnl - cost)
        returns[i] = equity / day_start - 1.0
        costs[i] = cost / day_start
        gross_returns[i] = returns[i] + costs[i]
        turnover[i] = traded / day_start
        if equity > 0.0:
            held = np.asarray(units)
            weights[i] = held * np.nan_to_num(spot_last[t]) / equity
            exposure[i] = weights[i].sum() + (held * np.nan_to_num(perp_last[t])).sum() / equity

    open_exit = 0.0
    if equity > 0.0:
        held = np.asarray(units)
        open_exit = _exit_cost(cost_model, assets, held * np.nan_to_num(spot_last[-1]) / equity, equity, stamps[-1],
                               capital_gbp, fee_level, cost_scale, fee_bps)
        perp_share = (held * np.nan_to_num(perp_last[-1])).sum() / equity
        open_exit += perp_share * _perp_bps(cost_model, stamps[-1]) * cost_scale / 1e4
    info = {
        "carry": True,
        "liquidation_modelled": False,
        "delistings": delistings,
        "cost_flags": dict(sorted(flags.items())),
        "funding_gaps": funding_gaps,
        "open_exit_cost": float(open_exit),
        "notional_by_flag": dict(sorted(by_flag.items())),
        "traded_notional": traded_notional,
    }
    return Result(name, perp_panel.ts[s:], returns, gross_returns, turnover, exposure, costs, weights, assets, info)


def _switch_rows(part: np.ndarray) -> np.ndarray:
    """The rows where this output turned on or off in any column, with NaN counted as off."""
    on = np.nan_to_num(np.asarray(part, dtype=np.float64)) != 0.0
    return np.flatnonzero((on[1:] != on[:-1]).any(axis=1)) + 1


def _cut_rows(panel, n_cuts: int, seed: int, outputs=()) -> list[int]:
    """How many rows to keep at each cut, ascending: to the first row with data, all of them, and random ones between.

    n_cuts counts both ends, so the seed picks n_cuts - 2 and a short panel simply gets every row once. Of those, two
    thirds go to cuts ending on a row where an output turned on or off, taking each output in turn, so a frequent one
    can't crowd out a rare one. They're uniform if there aren't enough such rows, and the rest always are.
    """
    total = panel.ts.size
    has_data = ~np.isnan(panel.close).all(axis=1)
    first = int(has_data.argmax()) + 1 if has_data.any() else min(total, 1)
    between = np.arange(first + 1, total)
    count = min(max(n_cuts - 2, 0), between.size)
    if count == between.size:
        return sorted({first, total, *between.tolist()})
    rng = np.random.default_rng(seed)
    turns = [rows[(rows > first) & (rows < total)] for rows in (_switch_rows(part) + 1 for part in outputs)]
    queues = [rng.permutation(rows).tolist() for rows in turns if rows.size]
    aimed = int(count * _AIMED_SHARE)
    picked = set()
    while len(picked) < aimed and any(queues):
        for queue in queues:
            if queue and len(picked) < aimed:
                picked.add(queue.pop())
    if count > len(picked):
        others = np.setdiff1d(between, np.fromiter(picked, dtype=between.dtype), assume_unique=True)
        picked.update(rng.choice(others, size=count - len(picked), replace=False).tolist())
    return sorted({first, total, *picked})


def _require_same(full: np.ndarray, cut: np.ndarray, rows: int, panel, label: str) -> None:
    """Compares bytes, because the truncation test is byte for byte: NaN must equal NaN, and 0.0 isn't -0.0."""
    expected = full[:rows]
    if cut.dtype != expected.dtype or cut.shape != expected.shape:
        raise AssertionError(
            f"lookahead check{label}: cut to {rows} rows it gave {cut.dtype} {cut.shape}, "
            f"expected {expected.dtype} {expected.shape}"
        )
    seen, wanted = cut.tobytes(), expected.tobytes()
    if seen == wanted:
        return
    differs = np.frombuffer(seen, dtype=np.uint8) != np.frombuffer(wanted, dtype=np.uint8)
    row, column = np.unravel_index(int(differs.argmax()) // expected.dtype.itemsize, expected.shape)
    raise AssertionError(
        f"lookahead check{label}: first bad row {row} (ts {int(panel.ts[row])}), asset {panel.assets[column]}: "
        f"its value changes when the panel is cut to {rows} rows, so it depends on later data"
    )


def _bar_seconds(panel) -> int:
    return int(np.diff(panel.ts).min()) if panel.ts.size > 1 else _DAY


def _finished_by(companion, moment: int):
    """A companion panel as it stood at this moment: only the bars that had closed by then."""
    return truncate(companion, int(np.searchsorted(companion.ts + _bar_seconds(companion), moment, side="right")))


def _check_cuts(strategy, panel, n_cuts: int, seed: int, unpack, companions) -> None:
    bar = _bar_seconds(panel)

    def outputs(cut):
        closed_at = int(cut.ts[-1]) + bar if cut.ts.size else 0
        return unpack(strategy(cut, *(_finished_by(companion, closed_at) for companion in companions)))

    full = [np.asarray(part) for part in outputs(panel)]
    for number, part in enumerate(full):
        label = f" (output {number})" if len(full) > 1 else ""
        if part.shape != panel.close.shape:
            raise AssertionError(f"lookahead check{label}: it gave shape {part.shape}, expected {panel.close.shape}")
    # Ascending, so the first cut to fail is the one nearest the first bad row.
    for rows in _cut_rows(panel, n_cuts, seed, full):
        parts = outputs(truncate(panel, rows))
        if len(parts) != len(full):
            raise AssertionError(f"lookahead check: cut to {rows} rows it gave {len(parts)} outputs, not {len(full)}")
        for number, (part, cut) in enumerate(zip(full, parts)):
            label = f" (output {number})" if len(full) > 1 else ""
            _require_same(part, np.asarray(cut), rows, panel, label)


def check_no_lookahead(strategy, panel, n_cuts=20, seed=0, *, companions=()) -> None:
    """strategy(panel) gives a T x A array, and its first rows must not change when later bars don't exist.

    With companions, it's strategy(panel, *companions), and each of them is cut with the panel. A strategy reading
    another panel any other way, such as closed over, has it whole at every cut, so a peek through it can't show.
    """
    _check_cuts(strategy, panel, n_cuts, seed, lambda output: (output,), companions)


def check_signals_no_lookahead(strategy, panel, n_cuts=20, seed=0, *, companions=()) -> None:
    """The same, for a strategy that gives a tuple of T x A arrays, such as (entry, exit, size)."""
    _check_cuts(strategy, panel, n_cuts, seed, tuple, companions)
