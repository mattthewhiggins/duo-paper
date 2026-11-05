"""What every strategy is measured against: section 9's baselines, and the null distributions behind section 11's test
5.

Everything here takes the panel's own prices and the same costs as the strategy, and only the nulls use randomness, with
np.random.default_rng(seed + i) for run i. Where the rules leave a choice open, these are the readings taken:

- Band 1.0 can't buy. The engine trades on abs(target - h) > band, and 1 - 0 isn't above 1.0, so a band of 1.0 from the
  start never trades at all. So the band is 0 on the row that decides a trade, and above any possible change elsewhere.
- The target can't say "hold" through a gap. A NaN close must have target 0, and a target of 0 on a held asset always
  sells at the next bar, so one missing day would sell a buy-and-hold, and a band would stop it buying back. So gap
  days, between an asset's first and last close, get a flat bar at the last close, which is the same arithmetic as the
  engine's own carry. No order is ever filled on one: it's dropped, as it would be on a day with no bar.
- "Setting the target to the drifted weight" is done by leaving the target as it was set, under a band nothing can
  cross, as research/STRATEGY_SPEC.md does for F2. The target only has to stay above 0 for the exit rule not to fire,
  and with no trade the drift is the engine's own.
- equal_weight with rebalance=None buys once and never trades again, as "buy-and-hold" in G4 and "no rebalancing" in
  research/STRATEGY_SPEC.md say. Rebalancing daily would pay costs that make the comparison easier to beat. It decides
  on the closes it has at that row, and an asset with none then stays out for good.
- A monthly baseline trades only on the first bar of a month, so one started mid-month waits in cash until the next
  month's first bar, and one started on a panel's first bar waits too, because that bar has no close before it.
- dca: the tranche is 1/weeks of starting capital, whatever the account has grown to. It uses backtest_weights' own
  steps, so costs come off equity and leave weights as they are. Section 6 allows no leverage, so a purchase is cut to
  what's left when costs have used some of the cash: the last tranche ends up a little short, and the sum of weights
  never passes 1. An asset with no bar on a purchase day has that tranche dropped, and it stays in cash. info counts
  them.
- A tranche due on a day with no row at all is bought on the next row. Several due on one row buy together.
- A spell's length counts days with data, so a gap inside it doesn't count, and a spell re-placed over a gap holds
  through it. Spells are kept at least one day with data apart, or two would merge into one and the count would change.
  A spell's first day needs a close on the day before it, so an asset's first bar can't be one. A spell always enters on
  its first day, however small its size is against the band, because a spell the band swallows would no longer be
  matched to the strategy.
- Amendment 6: with universe_mask, a spell can hold an asset on a day only if the mask has it at the close before, the
  close that decided, as the strategy's own target needs. So the strategy's own spells always fit. A spell placed across
  days outside the mask leaves them flat, and pays to come back after. Spells the strategy only parted by such days may
  not fit a day apart, and then they're placed with no day between, so two can touch and a re-entry becomes a resize. A
  day with no bar is never in a universe, so a spell is sold at the first bar after a gap, as the strategy's is.
- Amendment 6: the strategy's Sharpe compared with the null has its open_exit_cost taken off its last row, because a
  re-placed spell almost always sells and the strategy's own last spell didn't. Where that would raise the Sharpe, which
  one huge last day can make it do, the Sharpe as it stands is used, because paying to leave can only lower it.
- A variant in position on every day of its window, in every asset it holds, has one placement, so its null is itself
  and shows nothing about timing. Its percentile is 0 (Amendment 6), even where its weight wanders and a constant-size
  copy would land on one side of it by chance.
- random_spells refuses a carry Result: it would compare a hedged position with randomly timed spot.
  random_carry_spells is F3's null: the same rules through backtest_carry, on days both legs have a bar. A spell's
  sleeve is twice its spot weight, because the spot leg is half the sleeve.
- random_picks takes a pick at the close before the strategy's new holdings first show, because that's when the strategy
  chose, and mask[t] needs t's own volume, so it can only be traded from t+1. Using the day the holdings show would read
  the future. A day counts as changed when the set of names held changes, not on a rebalance back to the same names.
- Amendment 6: on a day the strategy changed its names, the null drops as many of its picks as the strategy sold, at
  random, and draws as many new ones as it needs to hold as many as the strategy, from that day's universe and not
  already held. The rest stay. A pick with no data left has gone already, and counts towards what's dropped. Weights go
  by position: the first pick gets the largest weight, and a new name takes the place of the one it replaced, so a kept
  name keeps its weight and the turnover is the strategy's, not 2.5 times it. With fewer names in a universe than it
  needs, names it has just dropped can come back, and then it holds what there is, with the largest weights.
- random_trades' holding length is in bars with data, and a trade is sold at the open that many bars after its entry. A
  stop filled inside a bar held through that bar, so it counts. Trades may touch: one can buy at the open the last one
  sold at, because in-bar stops can leave no flat bar for a re-placed trade to use. Every trade has a bar to sell at, so
  a trade the strategy held to the end, or to a delisting, is cut to fit if it has to be. The earliest entry is the
  second bar, because the first bar's close is the first decision in the window.
- "The same costs": a trade the strategy still held at the end paid no exit cost, so its re-placed trade pays none
  either, and that's the one way the null gets a cost it didn't earn. A trade that ended in a delisting pays the normal
  exit cost and no haircut, which makes the null slightly stronger. A buy and its cost together take at most the cash,
  as in backtest_trades, because section 6 allows no leverage, so a run has no more buying power than the strategy had.
- Its costs are the same arithmetic as CostModel.spot_cost_detail, worked out on arrays, because that call can't be made
  millions of times. research/tests/test_h_baselines.py checks the two agree, and checks whole runs against
  backtest_trades.
- The percentile is the share of runs strictly below the strategy's Sharpe (Amendment 6), so a tie counts against it. A
  run with a NaN Sharpe isn't below, and a strategy with a NaN Sharpe has a percentile of 0.
"""
from __future__ import annotations

import bisect
import dataclasses

import numpy as np

from research.harness.costs import _interpolate
from research.harness.data import Panel, top_n_mask
from research.harness.engine import (
    CAPITAL_GBP,
    DELIST_HAIRCUT,
    EVAL_START_TS,
    _SETTLE_STEPS,
    _SETTLED,
    _check_cost_inputs,
    _check_daily,
    _check_prices,
    _checked_band,
    _exit_cost,
    _first_row,
    _forward_filled,
    _last_row_with_data,
    _matched_spot,
    _spot_bps,
    _where,
    backtest_carry,
    backtest_weights,
)
from research.harness.metrics import annual_sharpe, compound
from research.harness.panels import truncate
from research.harness.result import Result

_DAY = 86400
_HELD = 1e-9  # A weight above this is a holding.
_NO_TRADE_BAND = 10.0  # No change in a weight can be this big, so nothing trades under it except an exit.
_HOLD = 1e-12  # A target above 0, so the exit rule doesn't fire, and too small to matter to the row limit of 1 + 1e-9.
_NO_SIZE = 1e10  # In GBP. No trade is this big, so each asset's cost curve can have its own stretch of a number line.
_FILLS_IN_MEMORY = 40_000_000  # How many fills one batch of runs may hold in random_trades: about 0.8 GB at worst.


def _columns(panel: Panel, assets) -> list[int]:
    assets = list(assets)
    unknown = [asset for asset in assets if asset not in panel.assets]
    if unknown:
        raise ValueError(f"{panel.name} has no assets named {unknown}")
    if len(set(assets)) != len(assets) or not assets:
        raise ValueError(f"assets must be one or more different names, got {assets}")
    return [panel.assets.index(asset) for asset in assets]


def _gaps_filled(panel: Panel) -> tuple[Panel, np.ndarray]:
    """The panel with a flat bar at the last close on each gap day, and where the row after is such a day.

    A bar with only one of open and close NaN stays as it is, for the engine to refuse.
    """
    no_open, no_close = np.isnan(panel.open), np.isnan(panel.close)
    rows = np.arange(panel.ts.size)[:, None]
    gap = no_open & no_close & (rows > no_close.argmin(axis=0)) & (rows < _last_row_with_data(panel.close))
    before_gap = np.zeros(gap.shape, dtype=bool)
    before_gap[:-1] = gap[1:]
    if not gap.any():
        return panel, before_gap
    closes = np.where(gap, _forward_filled(panel.close), panel.close)
    return dataclasses.replace(panel, open=np.where(gap, closes, panel.open), close=closes), before_gap


def _run_held(filled: Panel, before_gap: np.ndarray, desired: np.ndarray, trade_rows, **options) -> Result:
    """A portfolio that trades only when told: desired[row] fills at the open after each of trade_rows. It drifts."""
    target = np.zeros(filled.close.shape)
    band = np.full(target.shape, _NO_TRADE_BAND)
    rows = sorted(set(trade_rows))
    held = np.zeros(target.shape[1], dtype=bool)
    for row, next_row in zip(rows, [*rows[1:], target.shape[0]]):
        wanted = desired[row].copy()
        # An order for a gap day is dropped, so what's held stays held. _HOLD says so without using up the row's sum.
        dropped = before_gap[row]
        wanted[dropped] = np.where(held[dropped], _HOLD, 0.0)
        target[row:next_row] = wanted
        band[row] = np.where(dropped, _NO_TRADE_BAND, 0.0)
        held = wanted > 0.0
    target[np.isnan(filled.close)] = 0.0
    return backtest_weights(filled, target, band=band, **options)


def _first_fillable_row(panel: Panel, columns: list[int], start_ts) -> int | None:
    """The first row, from the one before start_ts, whose close some asset has and whose next bar it has too."""
    s = _first_row(panel.ts, start_ts)
    has_bar = ~np.isnan(panel.close[:, columns])
    fillable = np.flatnonzero((has_bar[:-1] & has_bar[1:]).any(axis=1))
    fillable = fillable[fillable >= s - 1]
    return int(fillable[0]) if fillable.size else None


def buy_and_hold(panel, asset, *, start_ts=EVAL_START_TS, **options) -> Result:
    """All of equity in one asset from the first open it can be bought at, on or after start_ts. It trades once."""
    column = _columns(panel, [asset])[0]
    filled, before_gap = _gaps_filled(panel)
    desired = np.zeros(panel.close.shape)
    row = _first_fillable_row(panel, [column], start_ts)
    trade_rows = [] if row is None else [row]
    if row is not None:
        desired[row, column] = 1.0
    return _run_held(filled, before_gap, desired, trade_rows, start_ts=start_ts, **options)


def _equal_shares(available: np.ndarray) -> np.ndarray:
    """1 / the number available, on each row, for those that are."""
    return available / np.maximum(available.sum(axis=1, keepdims=True), 1)


def _rows_before_month_starts(panel: Panel, start_ts) -> np.ndarray:
    """The row before each first bar of a UTC month, from start_ts on. Its close decides that bar's trades."""
    months = panel.ts.astype("datetime64[s]").astype("datetime64[M]")
    first_bars = np.flatnonzero(months[1:] != months[:-1]) + 1
    return first_bars[first_bars >= _first_row(panel.ts, start_ts)] - 1


def equal_weight(panel, assets, rebalance=None, *, start_ts=EVAL_START_TS, **options) -> Result:
    """1/N, among those with a close. None buys once and holds, and "monthly" trades back on each month's first bar."""
    if rebalance not in (None, "monthly"):
        raise ValueError(f"rebalance must be None or 'monthly', not {rebalance!r}")
    columns = _columns(panel, assets)
    filled, before_gap = _gaps_filled(panel)
    available = np.zeros(panel.close.shape, dtype=bool)
    available[:, columns] = ~np.isnan(panel.close[:, columns])
    if rebalance == "monthly":
        trade_rows = _rows_before_month_starts(panel, start_ts).tolist()
    else:
        row = _first_fillable_row(panel, columns, start_ts)
        trade_rows = [] if row is None else [row]
    return _run_held(filled, before_gap, _equal_shares(available), trade_rows, start_ts=start_ts, **options)


def top_n_monthly(panel, n=10, *, start_ts=EVAL_START_TS, **options) -> Result:
    """Equal weight over the top n at the close before each month's first bar. One that drops out goes at the next."""
    filled, before_gap = _gaps_filled(panel)
    available = top_n_mask(panel, n) & ~np.isnan(panel.close)
    trade_rows = _rows_before_month_starts(panel, start_ts).tolist()
    return _run_held(filled, before_gap, _equal_shares(available), trade_rows, start_ts=start_ts, **options)


def dca(panel, assets, weeks=52, *, name="", start_ts=EVAL_START_TS, capital_gbp=CAPITAL_GBP, cost_model,
        fee_level="tier3_taker", cost_scale=1.0, fee_bps=None) -> Result:
    """From start_ts, every 7 days for `weeks` purchases, 1/weeks of the starting capital split over assets, then holds.

    The steps are backtest_weights', except the size of a trade, which its target form can't state in dollars.
    """
    columns = _columns(panel, assets)
    if weeks != int(weeks) or weeks < 1:
        raise ValueError(f"weeks must be a whole number of 1 or more, got {weeks}")
    _check_cost_inputs(capital_gbp, cost_scale, fee_bps)
    _check_daily(panel)
    _check_prices(panel)
    no_open, no_close = np.isnan(panel.open), np.isnan(panel.close)
    if (no_open != no_close).any():
        raise ValueError(f"only one of open and close is NaN at {_where(no_open != no_close, panel)}")

    rows, width = panel.close.shape
    s = _first_row(panel.ts, start_ts)
    stamps = panel.ts.tolist()
    due_rows = np.searchsorted(panel.ts, panel.ts[s] + 7 * _DAY * np.arange(int(weeks)))
    purchases = np.bincount(due_rows, minlength=rows + 1).tolist()
    tranche = 1.0 / int(weeks) / len(columns)
    gaps = np.zeros((rows, width))
    gaps[1:] = panel.open[1:] / _forward_filled(panel.close)[:-1] - 1.0
    gaps[np.isnan(gaps)] = 0.0
    moves = panel.close / panel.open - 1.0
    moves[np.isnan(moves)] = 0.0
    no_more_closes = np.arange(rows)[:, None] > _last_row_with_data(panel.close)

    returns, gross_returns, turnover = np.zeros(rows - s), np.zeros(rows - s), np.zeros(rows - s)
    exposure, costs = np.zeros(rows - s), np.zeros(rows - s)
    weights = np.zeros((rows - s, width))
    delistings, flags, by_flag = [], {}, {}
    traded_notional = 0.0
    dropped = 0
    h = np.zeros(width)
    equity = 1.0
    for t in range(s, rows):
        i = t - s
        if equity <= 0.0:
            continue
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

        before_costs = equity * G
        d = np.zeros(width)
        if purchases[t]:
            buying = [a for a in columns if not no_close[t, a]]
            dropped += purchases[t] * (len(columns) - len(buying))
            d[buying] = purchases[t] * tranche / before_costs
            room = max(0.0, 1.0 - h.sum())
            if d.sum() > room:
                d *= room / d.sum()
        cost = 0.0
        for a in np.flatnonzero(d).tolist():
            size = float(d[a])
            notional = size * before_costs * capital_gbp
            bps = _spot_bps(cost_model, panel.assets[a], notional, stamps[t], fee_level, fee_bps, flags, by_flag, size)
            cost += size * bps * cost_scale / 1e4
            traded_notional += size

        w = h + d
        r = moves[t]
        I = 1.0 + (w * r).sum()
        h = w * (1.0 + r) / I

        kept = max(0.0, 1.0 - cost)
        returns[i] = G * kept * I - 1.0
        gross_returns[i] = G * I - 1.0
        costs[i] = cost
        turnover[i] = d.sum() + delisted_weight
        exposure[i] = h.sum()
        weights[i] = h
        equity *= G * kept * I

    info = {
        "delistings": delistings,
        "cost_flags": dict(sorted(flags.items())),
        "dropped_purchases": dropped,
        "open_exit_cost": _exit_cost(
            cost_model, panel.assets, h, equity, stamps[-1], capital_gbp, fee_level, cost_scale, fee_bps
        ),
        "notional_by_flag": dict(sorted(by_flag.items())),
        "traded_notional": traded_notional,
    }
    return Result(name, panel.ts[s:], returns, gross_returns, turnover, exposure, costs, weights, panel.assets, info)


def _arranged(rng, lengths: np.ndarray, room: int, apart: int) -> np.ndarray:
    """Start positions for runs of these lengths in `room` positions, `apart` or more from each other, no overlaps.

    Every arrangement is equally likely. The spare positions and the runs make a sequence, so choosing which of its
    places hold a run settles everything, and a random order settles which run goes where.
    """
    count = lengths.size
    spare = room - int(lengths.sum()) - apart * (count - 1)
    if spare < 0:
        raise ValueError(f"{count} runs of total length {int(lengths.sum())} don't fit in {room} positions")
    order = rng.permutation(count)
    places = np.sort(rng.choice(spare + count, size=count, replace=False, shuffle=False))
    used_before = np.cumsum(np.r_[0, lengths[order][:-1] + apart])
    starts = np.empty(count, dtype=np.int64)
    starts[order] = places - np.arange(count) + used_before
    return starts


def _window(result: Result, panel: Panel) -> tuple[Panel, np.ndarray]:
    """The panel cut at the result's last day, because a run must cover the same days, and the result's rows in it."""
    rows = np.searchsorted(panel.ts, result.ts)
    matches = rows.size and rows[-1] < panel.ts.size and np.array_equal(panel.ts[rows], result.ts)
    if not matches or (np.diff(rows) != 1).any():
        raise ValueError(f"{result.name}'s days aren't an unbroken run of {panel.name}'s bars")
    return truncate(panel, int(rows[-1]) + 1), rows


def _checked_mask(universe_mask, panel: Panel, cut: Panel) -> np.ndarray | None:
    """The mask over the cut panel's rows, false wherever there's no close, or None for no mask."""
    if universe_mask is None:
        return None
    mask = np.asarray(universe_mask)
    if mask.shape != panel.close.shape or mask.dtype != np.bool_:
        raise ValueError(f"universe_mask must be a bool array of shape {panel.close.shape}")
    return mask[: cut.ts.size] & ~np.isnan(cut.close)


def spell_plans(result: Result, panel: Panel, rows: np.ndarray, universe_mask=None) -> list[tuple]:
    """Per asset with spells: (column, the days a spell can hold it, lengths in days with data, sizes, the next bar
    after each of those days, and where those days stop running on).

    Without a mask the days are every day with data, and they always run on. With one, a day needs the mask at the close
    before it, and two days in a row of that list that aren't next to each other in bars have days outside the universe
    between them.
    """
    has_bar = ~np.isnan(panel.close)
    plans = []
    for held_column, asset in enumerate(result.assets):
        weights = result.weights[:, held_column]
        edges = np.flatnonzero(np.diff(np.r_[False, weights > _HELD, False].astype(np.int8)))
        if not edges.size:
            continue
        column = _columns(panel, [asset])[0]
        starts, ends = edges[::2], edges[1::2]
        data_days = np.r_[0, np.cumsum(has_bar[rows, column])]
        lengths = np.maximum(data_days[ends] - data_days[starts], 1)
        sizes = np.array([weights[start:end].mean() for start, end in zip(starts.tolist(), ends.tolist())])
        days = rows[has_bar[rows, column] & (rows > has_bar[:, column].argmax())]
        if universe_mask is not None:
            days = days[universe_mask[days - 1, column]]
        bars = np.r_[np.flatnonzero(has_bar[:, column]), panel.ts.size + 1]
        next_bars = bars[np.searchsorted(bars, days, side="right")]
        breaks = np.flatnonzero(next_bars[:-1] != days[1:])
        plans.append((column, days, lengths, sizes, next_bars, breaks.tolist()))
    return plans


def placed_spells(plans: list[tuple], shape: tuple[int, int], rng) -> tuple[np.ndarray, np.ndarray]:
    """One random placement of the spells: a target array, and where each spell's entry is decided.

    A spell of L days starting on day p holds at the close of days p to p+L-1. So its target runs from the row before
    day p up to the row before the next day with data, where it is 0, and that's the bar it sells at. Where its days
    stop running on, it does that and starts again at the next of them.
    """
    target = np.zeros(shape)
    entries = np.zeros(shape, dtype=bool)
    for column, days, lengths, sizes, next_bars, breaks in plans:
        fits_apart = int(lengths.sum()) + lengths.size - 1 <= days.size
        starts = _arranged(rng, lengths, days.size, 1 if fits_apart or not breaks else 0)
        for start, length, size in zip(starts.tolist(), lengths.tolist(), sizes.tolist()):
            last = start + length - 1
            stops = breaks[bisect.bisect_left(breaks, start) : bisect.bisect_left(breaks, last)]
            for stop in [*stops, last]:
                first = int(days[start])
                target[first - 1 : int(next_bars[stop]) - 1, column] = size
                entries[first - 1, column] = True
                start = stop + 1
    return target, entries


def _has_one_placement(plans: list[tuple]) -> bool:
    """Whether every asset's one spell fills all its days, so the null can only be the strategy again."""
    return all(lengths.size == 1 and lengths[0] == days.size for _, days, lengths, *_ in plans)


def _sharpe_paying_to_leave(result: Result) -> float:
    """The strategy's Sharpe with its open_exit_cost off its last row, or as it stands if that would be higher."""
    as_marked = annual_sharpe(result.returns)
    exit_cost = result.info.get("open_exit_cost") or 0.0
    if not exit_cost or not result.returns.size:
        return as_marked
    returns = np.array(result.returns, dtype=np.float64)
    returns[-1] = (1.0 + returns[-1]) * (1.0 - exit_cost) - 1.0
    return min(as_marked, annual_sharpe(returns))


def _null_outcome(result: Result, sharpes: np.ndarray, total_returns: np.ndarray, strategy_sharpe=None,
                  one_placement=False) -> dict:
    strategy_sharpe = annual_sharpe(result.returns) if strategy_sharpe is None else strategy_sharpe
    return {
        "sharpes": sharpes,
        "total_returns": total_returns,
        # Strictly below, so a tie counts against the strategy. A NaN compares false, so it never raises a percentile.
        "percentile": 0.0 if one_placement else float(np.mean(sharpes < strategy_sharpe)),
    }


def _check_seeds(seeds) -> None:
    if seeds != int(seeds) or seeds < 1:
        raise ValueError(f"seeds must be a whole number of 1 or more, got {seeds}")


def _scaled_to_1(target: np.ndarray) -> np.ndarray:
    totals = target.sum(axis=1)
    over = totals > 1.0
    target[over] /= totals[over, None]
    return target


def random_spells(result, panel, *, universe_mask=None, seeds=1000, band=0.0, seed=0, **options) -> dict:
    """The strategy's holding spells re-placed at random in each asset's days with data, through backtest_weights.

    With universe_mask, a T x A bool array over the panel's rows, only on days the mask allowed at the close before.
    """
    _check_seeds(seeds)
    if result.info.get("carry"):
        raise ValueError(f"{result.name} is a carry result, and its spells need backtest_carry, not long-only spot")
    cut, rows = _window(result, panel)
    mask = _checked_mask(universe_mask, panel, cut)
    filled, before_gap = _gaps_filled(cut)
    no_close = np.isnan(filled.close)
    shape = cut.close.shape
    bands = np.array(_checked_band(band[: shape[0]] if np.ndim(band) == 2 else band, shape))
    bands[before_gap] = _NO_TRADE_BAND
    plans = spell_plans(result, cut, rows, mask)
    sharpes, total_returns = np.empty(int(seeds)), np.empty(int(seeds))
    for run in range(int(seeds)):
        target, entries = placed_spells(plans, shape, np.random.default_rng(seed + run))
        target[no_close] = 0.0
        target = _scaled_to_1(target)
        run_bands = bands.copy()
        run_bands[entries] = 0.0
        outcome = backtest_weights(filled, target, band=run_bands, start_ts=int(result.ts[0]), **options)
        sharpes[run] = annual_sharpe(outcome.returns)
        total_returns[run] = compound(outcome.returns) - 1.0
    return _null_outcome(result, sharpes, total_returns, _sharpe_paying_to_leave(result), _has_one_placement(plans))


def random_carry_spells(result, spot_panel, perp_panel, *, universe_mask=None, seeds=1000, seed=0, **options) -> dict:
    """F3's null: a carry result's spells re-placed as random_spells does, through backtest_carry.

    A day with data is one both legs have a bar, because a position can only open or close on one. universe_mask is over
    perp_panel's rows and assets.
    """
    _check_seeds(seeds)
    if not result.info.get("carry"):
        raise ValueError(f"{result.name} isn't a carry result, so its null is random_spells")
    cut, rows = _window(result, perp_panel)
    mask = _checked_mask(universe_mask, perp_panel, cut)
    spot_open, spot_close = _matched_spot(spot_panel, cut)
    both = ~(np.isnan(spot_open) | np.isnan(spot_close) | np.isnan(cut.open) | np.isnan(cut.close))
    tradable = dataclasses.replace(cut, close=np.where(both, cut.close, np.nan))
    # The spot leg is half the sleeve, so a spell's sleeve is twice its mean spot weight.
    plans = [(*plan[:3], 2.0 * plan[3], *plan[4:]) for plan in spell_plans(result, tradable, rows, mask)]
    sharpes, total_returns = np.empty(int(seeds)), np.empty(int(seeds))
    for run in range(int(seeds)):
        target, _ = placed_spells(plans, cut.close.shape, np.random.default_rng(seed + run))
        outcome = backtest_carry(spot_panel, cut, _scaled_to_1(target), start_ts=int(result.ts[0]), **options)
        sharpes[run] = annual_sharpe(outcome.returns)
        total_returns[run] = compound(outcome.returns) - 1.0
    return _null_outcome(result, sharpes, total_returns, _sharpe_paying_to_leave(result), _has_one_placement(plans))


def _next_picks(rng, picks: list, alive: np.ndarray, universe: np.ndarray, sold: int, count: int) -> list:
    """The null's names after a day the strategy sold `sold` names and ended holding `count`, in their places.

    A new name takes a dropped name's place, so the names kept keep their places, and with them their weights.
    """
    kept = [name for name in picks if alive[name]]
    drop = min(len(kept), max(sold - (len(picks) - len(kept)), len(kept) - count))
    places = sorted(rng.choice(len(kept), size=drop, replace=False).tolist()) if drop else []
    dropped = [kept[place] for place in places]
    room = count - (len(kept) - drop)
    held = set(picks)
    pool = [name for name in universe.tolist() if name not in held]
    fresh = rng.choice(pool, size=min(room, len(pool)), replace=False).tolist() if room > 0 and pool else []
    if len(fresh) < room:
        # A universe too small to hold enough new names: one just dropped can come back, rather than leave a gap.
        fresh += [name for name in dropped if name in set(universe.tolist())][: room - len(fresh)]
    for place in places:
        kept[place] = fresh.pop(0) if fresh else None
    return [name for name in kept if name is not None] + fresh


def random_picks(result, panel, universe_mask, *, seeds=1000, seed=0, **options) -> dict:
    """On each day the strategy changed its names, the same number swapped for random names from the universe."""
    _check_seeds(seeds)
    if universe_mask is None:
        raise ValueError("random_picks draws from a universe, so it needs universe_mask")
    cut, rows = _window(result, panel)
    mask = _checked_mask(universe_mask, panel, cut)
    filled, before_gap = _gaps_filled(cut)
    alive = ~np.isnan(filled.close)
    held = result.weights > _HELD
    before = np.r_[np.zeros((1, held.shape[1]), dtype=bool), held[:-1]]
    days = np.flatnonzero((held != before).any(axis=1)).tolist()
    decided = [max(int(rows[day]) - 1, 0) for day in days]
    choices = [
        (row, alive[row], np.flatnonzero(mask[row]), int((before[day] & ~held[day]).sum()), int(held[day].sum()),
         np.sort(result.weights[day, held[day]])[::-1])
        for row, day in zip(decided, days)
    ]
    sharpes, total_returns = np.empty(int(seeds)), np.empty(int(seeds))
    for run in range(int(seeds)):
        rng = np.random.default_rng(seed + run)
        desired = np.zeros(cut.close.shape)
        picks = []
        for row, alive_now, universe, sold, count, weights in choices:
            picks = _next_picks(rng, picks, alive_now, universe, sold, count)
            desired[row] = 0.0
            desired[row, picks] = weights[: len(picks)]
        outcome = _run_held(filled, before_gap, desired, decided, start_ts=int(result.ts[0]), **options)
        sharpes[run] = annual_sharpe(outcome.returns)
        total_returns[run] = compound(outcome.returns) - 1.0
    return _null_outcome(result, sharpes, total_returns)


def _trade_plans(trades: list[dict], panel: Panel, first: int, end: int) -> tuple[list[tuple], np.ndarray, np.ndarray]:
    """(column, the window's rows with an open, lengths, ids), per asset with trades, and each trade's size and charge.

    The charge is what share of the normal exit cost it pays: 1, or 0 for a trade that was never sold.
    """
    by_asset: dict[int, list[tuple[int, int]]] = {}
    sizes, exit_charges = np.empty(len(trades)), np.empty(len(trades))
    bars = panel.open[first:end]
    usable = {}
    for trade_id, trade in enumerate(trades):
        column = _columns(panel, [trade["asset"]])[0]
        if column not in usable:
            usable[column] = np.flatnonzero(~np.isnan(bars[1:, column])) + 1
        stamps = panel.ts[first:end]
        entry_row, exit_row = np.searchsorted(stamps, [trade["entry_ts"], trade["exit_ts"]]).tolist()
        on_bars = max(entry_row, exit_row) < bars.shape[0] and (
            stamps[entry_row] == trade["entry_ts"] and stamps[exit_row] == trade["exit_ts"]
        )
        if not on_bars:
            raise ValueError(f"a trade's times aren't bars of {panel.name} in this result's window: {trade}")
        entry_at, exit_at = np.searchsorted(usable[column], [entry_row, exit_row]).tolist()
        reason = trade["reason"]
        # A market sell or a stop that gapped is out at that open, and a stop inside the bar held through part of it.
        length = exit_at - entry_at + (reason == "stop" and trade["exit_price"] != bars[exit_row, column])
        # One bar with data has to be left to sell at.
        by_asset.setdefault(column, []).append((trade_id, max(1, min(length, usable[column].size - 1 - entry_at))))
        sizes[trade_id] = trade["size"]
        exit_charges[trade_id] = 0.0 if reason == "end" else 1.0
    plans = []
    for column in sorted(by_asset):
        ids, lengths = zip(*by_asset[column])
        plans.append((column, usable[column], np.array(lengths, dtype=np.int64), np.array(ids, dtype=np.int64)))
    return plans, sizes, exit_charges


def _cost_rates(cost_model, panel: Panel, traded: set[int], stamps: np.ndarray, fee_level, fee_bps, cost_scale):
    """A function from arrays of (column, notional in GBP, row) to one-way costs as fractions of the notional."""
    multipliers = np.array([cost_model.multiplier(stamp) for stamp in stamps.tolist()])
    xs, ys, fx = [], [], []
    for column, asset in enumerate(panel.assets):
        if column in traded:
            # Refuses a bad fee level or a Kraken name, as the engine would when it came to trade one.
            cost_model.spot_cost_detail(asset, 0.0, int(stamps[0]), fee_level)
        curve = cost_model._curves.get(cost_model.book_name(asset))
        if curve is None:
            points, fx_bps = [(1, cost_model.default_spread_impact_bps), (2, cost_model.default_spread_impact_bps)], 0.0
        else:
            points, fx_bps = curve.points, curve.fx_bps
        last = _NO_SIZE * 0.9
        xs.extend(column * _NO_SIZE + x for x in (0.0, *(point[0] for point in points), last))
        ys.extend((points[0][1], *(point[1] for point in points), _interpolate(points, last)))
        fx.append(fx_bps)
    xs, ys, fx = np.array(xs), np.array(ys), np.array(fx)
    fee_part = cost_model.fee_levels.get(fee_level) if fee_bps is None else fee_bps
    floor = cost_model.floor_bps

    def rates(columns: np.ndarray, notional: np.ndarray, rows: np.ndarray) -> np.ndarray:
        # One call over all the assets' curves laid end to end, because a call per asset per fill is too slow.
        level = np.interp(columns * _NO_SIZE + np.minimum(notional, _NO_SIZE * 0.9), xs, ys)
        return (fee_part + fx[columns] + np.maximum(floor, multipliers[rows] * level)) * cost_scale / 1e4

    return rates


def _placed_trades(rng, plans: list[tuple]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """One random placement of the trades, per plan: the rows they buy at, and the rows they sell at."""
    entries, sells = [], []
    for _, usable, lengths, _ in plans:
        starts = _arranged(rng, lengths, usable.size - 1, 0)
        entries.append(usable[starts])
        sells.append(usable[starts + lengths])
    return entries, sells


def _fill_table(plans: list[tuple], sizes: np.ndarray, exit_charges: np.ndarray) -> tuple[np.ndarray, ...]:
    """What each fill is, by a number that sorts as the engine works through a bar: in asset order, with a sell first.

    The plans are in asset order, so numbering each one's sells and then its buys does that. Returns each fill's column,
    whether it's a buy, its trade's size, and the share of the normal cost it pays.
    """
    columns, buying, fill_sizes, charges = [], [], [], []
    for column, _, _, ids in plans:
        columns.append(np.full(2 * ids.size, column))
        buying.append(np.r_[np.zeros(ids.size, dtype=bool), np.ones(ids.size, dtype=bool)])
        fill_sizes.extend((np.zeros(ids.size), sizes[ids]))
        charges.extend((exit_charges[ids], np.ones(ids.size)))
    return np.concatenate(columns), np.concatenate(buying), np.concatenate(fill_sizes), np.concatenate(charges)


def _fill_arrays(run_seeds: list[int], plans: list[tuple]) -> tuple[np.ndarray, np.ndarray]:
    """Every run's fills in the order they happen, as E x runs arrays: their rows and their numbers in _fill_table.

    Row and number go in one integer, because sorting those is far quicker than sorting the rows and carrying the
    numbers along.
    """
    total = 2 * sum(plan[3].size for plan in plans)
    numbers = np.arange(total)
    keys = np.empty((len(run_seeds), total), dtype=np.int64)
    for run, seed in enumerate(run_seeds):
        entries, sells = _placed_trades(np.random.default_rng(seed), plans)
        keys[run] = np.concatenate([part for pair in zip(sells, entries) for part in pair]) * total + numbers
    keys.sort(axis=1)
    # In C order, because each step reads one row, and a row of the transposed array would be scattered through memory.
    rows_of_fills = (keys // total).T.astype(np.int32, order="C")
    keys %= total
    return rows_of_fills, keys.T.astype(np.int32, order="C")


def _settled_buys(dollars, cash, asset, bar, rates, capital_gbp) -> np.ndarray:
    """What backtest_trades buys when the cost won't also fit in the cash: they share it, so d x (1 + rate) = cash.

    A smaller buy can have a lower rate, so each one repeats until its size settles, as it does in the engine.
    """
    dollars = dollars.copy()
    waiting = np.arange(dollars.size)
    for _ in range(_SETTLE_STEPS):
        settled = cash[waiting] / (1.0 + rates(asset[waiting], dollars[waiting] * capital_gbp, bar[waiting]))
        moved = np.abs(settled - dollars[waiting]) > _SETTLED * cash[waiting]
        waiting = waiting[moved]
        if not waiting.size:
            break
        dollars[waiting] = settled[moved]
    return dollars


def _trade_equity(fills, table, opens, marks, day_of_row, days, rates, capital_gbp) -> np.ndarray:
    """Each run's equity at each day's end, runs x days. Every run takes its next fill at once, whichever bar that's on.

    This is backtest_trades' ledger, in the same order and with the same arithmetic. The runs have the same number of
    fills, so numpy can do each step for all of them, where going bar by bar would need a Python loop over the bars.
    """
    bars, numbers = fills
    fill_columns, fill_buys, fill_sizes, fill_charges = table
    runs = bars.shape[1]
    width = opens.shape[1]
    everyone = np.arange(runs)
    cash = np.ones(runs)
    units = np.zeros((runs, width))
    at_close = np.ones(runs)  # Equity at the last close, before anything fills at this open.
    last_bar = np.full(runs, -1)
    cash_by_day = np.full((runs, days), np.nan)
    units_by_day = np.full((runs, days, width), np.nan)
    for step in range(bars.shape[0]):
        bar, number = bars[step], numbers[step]
        asset, buying = fill_columns[number], fill_buys[number]
        # A fill that isn't its bar's first keeps the figure from before that bar's first fill.
        at_close = np.where(bar != last_bar, cash + (units * marks[bar - 1]).sum(axis=1), at_close)
        last_bar = bar
        price = opens[bar, asset]
        wanted = np.maximum(np.minimum(fill_sizes[number] * at_close, cash), 0.0)
        dollars = np.where(buying, wanted, units[everyone, asset] * price)
        paid = dollars * rates(asset, dollars * capital_gbp, bar) * fill_charges[number]
        short = buying & (dollars > 0.0) & (dollars + paid > cash)
        if short.any():
            dollars[short] = _settled_buys(dollars[short], cash[short], asset[short], bar[short], rates, capital_gbp)
        cash -= np.where(buying, dollars + paid, paid - dollars)
        cash[short] = 0.0  # Exactly, as in the engine, however the size settled.
        held = np.where(buying, dollars / price, 0.0)
        units[everyone, asset] = held
        day = day_of_row[bar]
        cash_by_day[everyone, day] = cash
        units_by_day[everyone, day, asset] = held

    equity = _forward_filled(cash_by_day.T).T
    equity[np.isnan(equity)] = 1.0
    ends = np.r_[np.flatnonzero(np.diff(day_of_row)), day_of_row.size - 1]
    for column in range(width):
        held = _forward_filled(units_by_day[:, :, column].T).T
        held[np.isnan(held)] = 0.0
        equity += held * marks[ends, column]
    return equity


def random_trades(result, panel, *, seeds=1000, seed=0, cost_model, capital_gbp=CAPITAL_GBP, fee_level="tier3_taker",
                  cost_scale=1.0, fee_bps=None) -> dict:
    """The strategy's trades re-placed at random, with the same holding length and size, in and out at opens."""
    _check_seeds(seeds)
    trades = result.info.get("trades")
    if trades is None:
        raise ValueError(f"{result.name} has no info['trades'], so it didn't come from backtest_trades")
    _check_cost_inputs(capital_gbp, cost_scale, fee_bps)
    _check_prices(panel)
    first, end = np.searchsorted(panel.ts, [result.ts[0], result.ts[-1] + _DAY]).tolist() if result.ts.size else (0, 0)
    if first == end:
        raise ValueError(f"{panel.name} has no bars on {result.name}'s days")
    stamps = panel.ts[first:end]
    opens = panel.open[first:end]
    # Only a position's own asset is ever marked, and it has a close by then, so 0 stands for "not listed yet".
    marks = np.nan_to_num(_forward_filled(panel.close)[first:end])
    plans, sizes, exit_charges = _trade_plans(trades, panel, first, end)
    table = _fill_table(plans, sizes, exit_charges) if trades else None
    rates = _cost_rates(cost_model, panel, {plan[0] for plan in plans}, stamps, fee_level, fee_bps, cost_scale)
    calendar = stamps // _DAY
    day_of_row = np.r_[0, np.cumsum(calendar[1:] != calendar[:-1])]
    days = int(day_of_row[-1]) + 1

    seeds = int(seeds)
    sharpes, total_returns = np.empty(seeds), np.empty(seeds)
    batch = max(1, min(seeds, _FILLS_IN_MEMORY // max(1, 2 * len(trades))))
    for begin in range(0, seeds, batch):
        run_seeds = [seed + run for run in range(begin, min(begin + batch, seeds))]
        if trades:
            fills = _fill_arrays(run_seeds, plans)
            equity = _trade_equity(fills, table, opens, marks, day_of_row, days, rates, capital_gbp)
        else:
            equity = np.ones((len(run_seeds), days))
        # As in the engine: an account at zero stays there.
        equity = np.where(np.minimum.accumulate(equity > 0.0, axis=1), equity, 0.0)
        previous = np.c_[np.ones(len(run_seeds)), equity[:, :-1]]
        returns = np.divide(equity, previous, out=np.ones(equity.shape), where=previous > 0.0) - 1.0
        for offset in range(len(run_seeds)):
            sharpes[begin + offset] = annual_sharpe(returns[offset])
            total_returns[begin + offset] = compound(returns[offset]) - 1.0
    return _null_outcome(result, sharpes, total_returns)
