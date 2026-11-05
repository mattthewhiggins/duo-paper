"""Tests fast trading rules on real 5-minute prices, after fees, on data the rules weren't chosen on.

    .venv/bin/python -m duo backtest         # Two years, every frequency, every fee tier.
    .venv/bin/python -m duo backtest-years   # About eight years, reported year by year.

Rules are picked using the previous 12 months only, then scored on the next 3 months, repeatedly.
History comes from Coinbase's public candles because Kraken only serves the latest 720 candles.
Prices on the two track each other closely, but this measures price moves, not Kraken's order book.
"""
from __future__ import annotations

import gzip
import itertools
import json
import multiprocessing
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

BAR_SECONDS = 300
BARS_PER_DAY = 288
HISTORY_URL = "https://api.exchange.coinbase.com/products/{asset}-USD/candles"

# Conservative fill assumptions.
TAKER_SLIPPAGE = 0.0005     # Orders that cross the book fill this much worse than the price.
MAKER_TRADE_THROUGH = 0.0002  # Resting orders only count as filled if price trades this far past them.
MAX_HOLD_BARS = BARS_PER_DAY * 3

TRAIN_DAYS = 365
TEST_DAYS = 90
MIN_TRAIN_TRADES = 10
PORTFOLIO_SLEEVE = 0.8 / 3  # A third of the 80% that the 20% minimum cash limit leaves, per asset.

# Kraken Pro spot rates, effective July 9, 2026: name, maker %, taker %, 30-day volume needed.
FEE_TIERS = (
    ("Tier 1", 0.40, 0.80, 0),
    ("Tier 2", 0.30, 0.60, 2_500),
    ("Tier 3", 0.22, 0.38, 10_000),
    ("Tier 6", 0.12, 0.25, 100_000),
    ("No fees", 0.0, 0.0, 0),
)
BANDS = (
    ("Under 1 a week", 0, 1 / 7),
    ("1 a week to 1 a day", 1 / 7, 1),
    ("1 to 3 a day", 1, 3),
    ("Over 3 a day", 3, float("inf")),
)


# ---- Price history ----

def _fetch_page(asset: str, start: datetime, end: datetime) -> list[list[float]]:
    query = (f"?granularity={BAR_SECONDS}&start={start:%Y-%m-%dT%H:%M:%SZ}&end={end:%Y-%m-%dT%H:%M:%SZ}")
    request = urllib.request.Request(HISTORY_URL.format(asset=asset) + query, headers={"User-Agent": "duo-backtest"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Couldn't download {asset} candles for {start}")


def download(asset: str, months: int, path: Path) -> None:
    """Saves candles as gzipped CSV: time, open, high, low, close."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=months * 30)
    rows: dict[int, tuple] = {}
    cursor = start
    step = timedelta(seconds=BAR_SECONDS * 299)
    while cursor < end:
        page_end = min(cursor + step, end)
        for ts, low, high, opened, close, _volume in _fetch_page(asset, cursor, page_end):
            rows[int(ts)] = (opened, high, low, close)
        cursor = page_end
        time.sleep(0.25)  # With three assets at once, this stays under Coinbase's public rate limit.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with gzip.open(temporary, "wt") as file:
        for ts in sorted(rows):
            file.write(f"{ts},{','.join(str(v) for v in rows[ts])}\n")
    temporary.replace(path)
    print(f"{asset}: {len(rows):,} candles saved", file=sys.stderr, flush=True)


def ensure_history(assets, months: int, directory: Path) -> dict[str, Path]:
    paths = {asset: directory / f"{asset}-5m-{months}mo.csv.gz" for asset in assets}
    threads = [threading.Thread(target=download, args=(a, months, p)) for a, p in paths.items() if not p.exists()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise RuntimeError(f"History download failed: {missing}")
    return paths


@dataclass
class Bars:
    ts: array
    open: array
    high: array
    low: array
    close: array
    day: array
    days: int


def parse_bars(rows, first_day: int | None = None) -> Bars:
    """Compact arrays, because years of 5-minute bars are large. first_day puts several assets on one calendar."""
    ts, opens, highs, lows, closes = array("q"), array("d"), array("d"), array("d"), array("d")
    for t, o, h, l, c in rows:
        ts.append(int(t)); opens.append(o); highs.append(h); lows.append(l); closes.append(c)
    first = ts[0] // 86400 if first_day is None else first_day
    day = array("i", (t // 86400 - first for t in ts))
    return Bars(ts, opens, highs, lows, closes, day, day[-1] + 1)


@lru_cache(maxsize=8)
def load_bars(path: str, first_day: int | None = None) -> Bars:
    with gzip.open(path, "rt") as file:
        rows = ([float(x) for x in line.split(",")] for line in file)
        return parse_bars(rows, first_day)


# ---- Indicators. Each value only uses data up to and including its own bar. ----

def sma(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        if i >= length - 1:
            out[i] = total / length
    return out


def rolling_high(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    window: deque[int] = deque()
    for i, value in enumerate(values):
        while window and values[window[-1]] <= value:
            window.pop()
        window.append(i)
        if window[0] <= i - length:
            window.popleft()
        if i >= length - 1:
            out[i] = values[window[0]]
    return out


# ---- Strategies. All long-only, one position at a time, $1 per trade, no compounding. ----

@dataclass
class Run:
    asset: str
    family: str
    params: tuple
    gross: array        # Mark-to-market P&L per day, before fees, including slippage.
    maker: array        # Notional traded per day with maker orders.
    taker: array        # Notional traded per day with taker orders.
    trades: array       # Round trips closed per day.


def _empty(bars: Bars, asset: str, family: str, params: tuple) -> Run:
    zeros = [0.0] * bars.days
    return Run(asset, family, params, array("d", zeros), array("d", zeros), array("d", zeros), array("d", zeros))


def run_dip(bars: Bars, asset: str, reference, trend, entry_pct: float, target_pct: float, stop_pct: float,
            lookback: int, use_trend: bool, max_hold: int = MAX_HOLD_BARS) -> Run:
    """Rests a maker bid below a moving average, takes profit with a resting sell, stops out as a taker."""
    run = _empty(bars, asset, "dip", (entry_pct, target_pct, stop_pct, lookback, use_trend))
    gross, maker, taker, trades = run.gross, run.maker, run.taker, run.trades
    opens, highs, lows, closes, days = bars.open, bars.high, bars.low, bars.close, bars.day
    entry_gap, target_gap, stop_gap = entry_pct / 100, target_pct / 100, stop_pct / 100
    in_position = False
    qty = mark = entry = target = stop = 0.0
    held = 0
    current_day = days[0]
    for i in range(1, len(closes)):
        day = days[i]
        if day != current_day:
            if in_position:
                gross[current_day] += qty * (closes[i - 1] - mark)
                mark = closes[i - 1]
            current_day = day
        if not in_position:
            base = reference[i - 1]
            if base is None or (use_trend and (trend[i - 1] is None or closes[i - 1] <= trend[i - 1])):
                continue
            bid = base * (1 - entry_gap)
            if opens[i] <= bid:
                continue  # The market is already below the bid, so it couldn't rest as a maker.
            if lows[i] < bid * (1 - MAKER_TRADE_THROUGH):
                entry = mark = bid
                qty = 1 / entry
                target, stop = entry * (1 + target_gap), entry * (1 - stop_gap)
                held = 0
                in_position = True
                maker[day] += 1.0
                if lows[i] <= stop:  # Same bar, assume the worst: it kept falling to the stop.
                    price = stop * (1 - TAKER_SLIPPAGE)
                    gross[day] += qty * (price - mark)
                    taker[day] += qty * price
                    trades[day] += 1
                    in_position = False
            continue
        held += 1
        if opens[i] <= stop:
            price = opens[i] * (1 - TAKER_SLIPPAGE)
            was_maker = False
        elif lows[i] <= stop:  # If a bar touches both, assume the stop came first.
            price = stop * (1 - TAKER_SLIPPAGE)
            was_maker = False
        elif highs[i] > target * (1 + MAKER_TRADE_THROUGH):
            price = target
            was_maker = True
        elif held >= max_hold:
            price = closes[i] * (1 - TAKER_SLIPPAGE)
            was_maker = False
        else:
            continue
        gross[day] += qty * (price - mark)
        if was_maker:
            maker[day] += qty * price
        else:
            taker[day] += qty * price
        trades[day] += 1
        in_position = False
    if in_position:
        gross[current_day] += qty * (closes[-1] - mark)
    return run


def run_breakout(bars: Bars, asset: str, prior_high, lookback: int, stop_pct: float, trail_pct: float) -> Run:
    """Buys a close above the recent high, exits on a trailing stop. Taker both ways."""
    run = _empty(bars, asset, "breakout", (lookback, stop_pct, trail_pct))
    gross, taker, trades = run.gross, run.taker, run.trades
    opens, highs, lows, closes, days = bars.open, bars.high, bars.low, bars.close, bars.day
    stop_gap, trail_gap = stop_pct / 100, trail_pct / 100
    in_position = False
    qty = mark = stop = peak = 0.0
    current_day = days[0]
    for i in range(2, len(closes)):
        day = days[i]
        if day != current_day:
            if in_position:
                gross[current_day] += qty * (closes[i - 1] - mark)
                mark = closes[i - 1]
            current_day = day
        if not in_position:
            high = prior_high[i - 2]
            if high is not None and closes[i - 1] > high:
                entry = opens[i] * (1 + TAKER_SLIPPAGE)
                mark = peak = entry
                qty = 1 / entry
                stop = entry * (1 - stop_gap)
                taker[day] += 1.0
                in_position = True
            continue
        level = max(stop, peak * (1 - trail_gap))
        if opens[i] <= level or lows[i] <= level:
            price = min(opens[i], level) * (1 - TAKER_SLIPPAGE)
            gross[day] += qty * (price - mark)
            taker[day] += qty * price
            trades[day] += 1
            in_position = False
            continue
        peak = max(peak, highs[i])
    if in_position:
        gross[current_day] += qty * (closes[-1] - mark)
    return run


def run_trend(bars: Bars, asset: str, fast, slow, fast_len: int, slow_len: int) -> Run:
    """Holds while the fast average is above the slow one. Taker both ways."""
    run = _empty(bars, asset, "trend", (fast_len, slow_len))
    gross, taker, trades = run.gross, run.taker, run.trades
    opens, closes, days = bars.open, bars.close, bars.day
    in_position = False
    qty = mark = 0.0
    current_day = days[0]
    for i in range(1, len(closes)):
        day = days[i]
        if day != current_day:
            if in_position:
                gross[current_day] += qty * (closes[i - 1] - mark)
                mark = closes[i - 1]
            current_day = day
        f, s = fast[i - 1], slow[i - 1]
        if f is None or s is None:
            continue
        if not in_position and f > s:
            entry = opens[i] * (1 + TAKER_SLIPPAGE)
            mark, qty = entry, 1 / entry
            taker[day] += 1.0
            in_position = True
        elif in_position and f <= s:
            price = opens[i] * (1 - TAKER_SLIPPAGE)
            gross[day] += qty * (price - mark)
            taker[day] += qty * price
            trades[day] += 1
            in_position = False
    if in_position:
        gross[current_day] += qty * (closes[-1] - mark)
    return run


# ---- Running the grid ----

DIP_GRID = list(itertools.product(
    (0.5, 1, 2, 3, 5),          # Bid this % below the average.
    (0.8, 1.5, 3, 5),           # Take profit this % above entry.
    (2, 4, 8),                  # Stop this % below entry.
    (12, 48, 288),              # Average over 1 hour, 4 hours or 1 day.
    (False, True),              # Only buy while above the 7-day average?
))
FAST_DIP_GRID = list(itertools.product(
    (0.2, 0.3, 0.5, 0.8),
    (0.3, 0.5, 0.8, 1.2),
    (0.5, 1, 2),
    (6, 12, 24, 48),            # Average over 30 minutes to 4 hours.
    (False, True),
))
BREAKOUT_GRID = list(itertools.product(
    (12, 48, 144, 288, 864),    # Break above the 1 hour to 3 day high.
    (2, 4, 8),                  # Initial stop %.
    (1, 2, 4, 8),               # Trailing stop %.
))
FAST_BREAKOUT_GRID = list(itertools.product((6, 12, 24), (0.5, 1), (0.3, 0.5, 1)))
TREND_GRID = [(48, 288), (144, 864), (288, 2016), (288, 4032)]


@lru_cache(maxsize=32)
def _indicator(path: str, kind: str, length: int):
    bars = load_bars(path)
    return sma(bars.close, length) if kind == "sma" else rolling_high(bars.high, length)


def _run_task(task: tuple) -> Run:
    asset, path, family, params = task
    bars = load_bars(path)
    if family == "dip":
        entry, target, stop, lookback, use_trend = params
        return run_dip(bars, asset, _indicator(path, "sma", lookback), _indicator(path, "sma", BARS_PER_DAY * 7),
                       entry, target, stop, lookback, use_trend)
    if family == "breakout":
        lookback, stop, trail = params
        return run_breakout(bars, asset, _indicator(path, "high", lookback), lookback, stop, trail)
    fast, slow = params
    return run_trend(bars, asset, _indicator(path, "sma", fast), _indicator(path, "sma", slow), fast, slow)


def tasks_for(paths: dict[str, Path]) -> list[tuple]:
    tasks = []
    for asset, path in paths.items():
        location = str(path)
        tasks += [(asset, location, "dip", params) for params in dict.fromkeys(DIP_GRID + FAST_DIP_GRID)]
        tasks += [(asset, location, "breakout", params) for params in dict.fromkeys(BREAKOUT_GRID + FAST_BREAKOUT_GRID)]
        tasks += [(asset, location, "trend", params) for params in TREND_GRID]
    return tasks


# ---- Scoring ----

def net_daily(run: Run, maker_pct: float, taker_pct: float) -> list[float]:
    maker_rate, taker_rate = maker_pct / 100, taker_pct / 100
    return [g - m * maker_rate - t * taker_rate for g, m, t in zip(run.gross, run.maker, run.taker)]


def prefix(values) -> list[float]:
    return [0.0, *itertools.accumulate(values)]


def price_moves(bars: Bars, threshold_pct: float) -> dict:
    """How often price travels far enough to pay for a round trip, in either direction."""
    gap = threshold_pct / 100
    anchor = bars.close[0]
    start = 0
    durations = []
    for i in range(1, len(bars.close)):
        if bars.high[i] >= anchor * (1 + gap) or bars.low[i] <= anchor * (1 - gap):
            durations.append((i - start) * BAR_SECONDS / 3600)
            anchor = bars.close[i]
            start = i
    return {
        "threshold_pct": threshold_pct,
        "moves_per_day": len(durations) / bars.days,
        "median_hours": statistics.median(durations) if durations else None,
    }


def stats(daily: list[float], trades: float, gross_total: float, fees_total: float) -> dict:
    days = len(daily)
    cumulative = peak = worst_drawdown = 0.0
    for value in daily:
        cumulative += value
        peak = max(peak, cumulative)
        worst_drawdown = min(worst_drawdown, cumulative - peak)
    green = sum(v > 0 for v in daily)
    red = sum(v < 0 for v in daily)
    return {
        "days": days,
        "return_pct": cumulative * 100,
        "annual_pct": cumulative * 100 * 365 / days,
        "max_drawdown_pct": worst_drawdown * 100,
        "green_days_pct": green / days * 100,
        "red_days_pct": red / days * 100,
        "worst_day_pct": min(daily) * 100,
        "best_day_pct": max(daily) * 100,
        "trades_per_day": trades / days,
        "gross_pct": gross_total * 100,
        "fees_pct": fees_total * 100,
    }


def score_runs(runs: dict[str, list[Run]], tier: tuple) -> dict[str, list[tuple]]:
    _, maker_pct, taker_pct, _ = tier
    scored = {}
    for asset, asset_runs in runs.items():
        scored[asset] = []
        for run in asset_runs:
            net = net_daily(run, maker_pct, taker_pct)
            scored[asset].append((run, net, prefix(net), prefix(run.trades)))
    return scored


def walk_forward(runs: dict[str, list[Run]], tier: tuple, days: int, band: tuple | None = None,
                 scored_runs: dict[str, list[tuple]] | None = None, keep_daily: bool = False) -> dict:
    """Each quarter, per asset: pick the best rule on the prior year, or stay in cash if none made money."""
    _, maker_pct, taker_pct, _ = tier
    scored_runs = scored_runs or score_runs(runs, tier)
    portfolio = [0.0] * days
    trades = gross_total = fees_total = 0.0
    chosen: list[dict] = []
    trades_daily = [0.0] * days
    fees_daily = [0.0] * days
    for asset, scored in scored_runs.items():
        for start in range(TRAIN_DAYS, days, TEST_DAYS):
            end = min(start + TEST_DAYS, days)
            best, best_net = None, 0.0
            for run, net, net_sum, trade_sum in scored:
                if len(net) != days:
                    continue
                train_trades = trade_sum[start] - trade_sum[start - TRAIN_DAYS]
                if train_trades < MIN_TRAIN_TRADES:
                    continue
                if band and not (band[1] <= train_trades / TRAIN_DAYS < band[2]):
                    continue
                value = net_sum[start] - net_sum[start - TRAIN_DAYS]
                if value > best_net:
                    best, best_net = (run, net, trade_sum), value
            chosen.append({"asset": asset, "from_day": start, "rule": (best[0].family, best[0].params) if best else "cash"})
            if best is None:
                continue
            run, net, trade_sum = best
            for day in range(start, end):
                portfolio[day] += net[day] * PORTFOLIO_SLEEVE
                trades_daily[day] += run.trades[day]
                fees_daily[day] += (run.maker[day] * maker_pct + run.taker[day] * taker_pct) / 100 * PORTFOLIO_SLEEVE
            trades += trade_sum[end] - trade_sum[start]
            gross_total += sum(run.gross[start:end]) * PORTFOLIO_SLEEVE
            fees_total += (sum(run.maker[start:end]) * maker_pct + sum(run.taker[start:end]) * taker_pct) / 100 * PORTFOLIO_SLEEVE
    result = stats(portfolio[TRAIN_DAYS:], trades, gross_total, fees_total)
    result["in_cash_pct"] = sum(c["rule"] == "cash" for c in chosen) / len(chosen) * 100
    result["monthly_volume_per_1000"] = (trades * 2 * PORTFOLIO_SLEEVE * 1000) / result["days"] * 30
    result["chosen"] = chosen
    if keep_daily:
        result.update(daily=portfolio, trades_daily=trades_daily, fees_daily=fees_daily)
    return result


def hold_benchmark(bars_by_asset: dict[str, Bars], days: int) -> dict:
    """Buying all three on the first test day and holding, in the same portfolio sizes, paying one taker fee."""
    portfolio = [0.0] * days
    for bars in bars_by_asset.values():
        closes: dict[int, float] = {}
        for day, close in zip(bars.day, bars.close):
            closes[day] = close
        previous = closes.get(TRAIN_DAYS - 1)
        base = previous
        for day in range(TRAIN_DAYS, days):
            close = closes.get(day, previous)
            portfolio[day] += (close - previous) / base * PORTFOLIO_SLEEVE
            previous = close
    fee = 3 * PORTFOLIO_SLEEVE * FEE_TIERS[0][2] / 100
    portfolio[TRAIN_DAYS] -= fee
    return stats(portfolio[TRAIN_DAYS:], 0, sum(portfolio[TRAIN_DAYS:]) + fee, fee)


def band_summary(runs: dict[str, list[Run]], tier: tuple, start: int = 0, end: int | None = None) -> list[dict]:
    """Every rule tested, grouped by how often it trades over the whole history. No selection, so no hindsight."""
    _, maker_pct, taker_pct, _ = tier
    grouped: dict[str, list[tuple[float, float]]] = {band[0]: [] for band in BANDS}
    for asset_runs in runs.values():
        for run in asset_runs:
            per_day = sum(run.trades) / len(run.trades)
            stop = end if end is not None else len(run.trades)
            trades = sum(run.trades[start:stop])
            if not trades:
                continue
            net = sum(net_daily(run, maker_pct, taker_pct)[start:stop])
            for name, low, high in BANDS:
                if low <= per_day < high:
                    grouped[name].append((net / trades * 100, net / (stop - start) * 365 * 100))
    rows = []
    for name, *_ in BANDS:
        values = grouped[name]
        rows.append({
            "band": name,
            "rules": len(values),
            "profitable_pct": sum(v[0] > 0 for v in values) / len(values) * 100 if values else None,
            "median_net_per_trade_pct": statistics.median(v[0] for v in values) if values else None,
            "median_annual_pct": statistics.median(v[1] for v in values) if values else None,
        })
    return rows


def winning_days_vs_profit(runs: dict[str, list[Run]], tier: tuple) -> dict:
    """Do the rules with the most winning days actually make the most money?"""
    _, maker_pct, taker_pct, _ = tier
    rows = []
    for asset_runs in runs.values():
        for run in asset_runs:
            net = net_daily(run, maker_pct, taker_pct)
            winning = sum(v > 0 for v in net)
            losing = sum(v < 0 for v in net)
            if winning + losing >= 100:
                rows.append((winning / (winning + losing) * 100, sum(net) / len(net) * 365 * 100, sum(run.trades) / len(net)))
    rows.sort()
    decile = max(1, len(rows) // 10)
    top, rest = rows[-decile:], rows[:-decile]
    return {
        "rules": len(rows),
        "most_winning_days": max(r[0] for r in rows),
        "top_decile_winning_days": statistics.median(r[0] for r in top),
        "top_decile_median_annual": statistics.median(r[1] for r in top),
        "top_decile_profitable_pct": sum(r[1] > 0 for r in top) / len(top) * 100,
        "rest_median_annual": statistics.median(r[1] for r in rest),
        "rules_over_60pct_winning_days": sum(r[0] >= 60 for r in rows),
        "profitable_over_60pct": sum(r[0] >= 60 and r[1] > 0 for r in rows),
    }


def best_rules(runs: dict[str, list[Run]], tier: tuple, count: int = 8) -> list[dict]:
    """The best rules in hindsight. Useful for seeing what kind of rule survives, not as a forecast."""
    _, maker_pct, taker_pct, _ = tier
    rows = []
    for asset_runs in runs.values():
        for run in asset_runs:
            net = net_daily(run, maker_pct, taker_pct)
            half = len(net) // 2
            rows.append({
                "asset": run.asset, "family": run.family, "params": run.params,
                "annual_pct": sum(net) / len(net) * 365 * 100,
                "first_half_pct": sum(net[:half]) * 100,
                "second_half_pct": sum(net[half:]) * 100,
                "trades_per_day": sum(run.trades) / len(net),
            })
    rows.sort(key=lambda r: r["annual_pct"], reverse=True)
    return rows[:count]


def asset_change(bars: Bars, start_day: int, end_day: int) -> float:
    closes = {day: close for day, close in zip(bars.day, bars.close)}
    return (closes[end_day] / closes[start_day] - 1) * 100


def analyse(runs: dict[str, list[Run]], bars_by_asset: dict[str, Bars]) -> dict:
    days = min(bars.days for bars in bars_by_asset.values())
    runs = {asset: [r for r in asset_runs] for asset, asset_runs in runs.items()}
    for asset_runs in runs.values():
        for run in asset_runs:
            for name in ("gross", "maker", "taker", "trades"):
                setattr(run, name, getattr(run, name)[:days])
    middle = days // 2
    report = {
        "days": days,
        "periods": {
            asset: {"first_half_pct": asset_change(bars, 0, middle), "second_half_pct": asset_change(bars, middle, days - 1)}
            for asset, bars in bars_by_asset.items()
        },
        "rules_tested": sum(len(v) for v in runs.values()),
        "price_moves": {
            asset: [price_moves(bars, t) for t in (0.44, 0.8, 1.2, 1.6)] for asset, bars in bars_by_asset.items()
        },
        "hold": hold_benchmark(bars_by_asset, days),
        "tiers": {},
    }
    for tier in FEE_TIERS:
        scored = score_runs(runs, tier)
        report["tiers"][tier[0]] = {
            "maker_pct": tier[1],
            "taker_pct": tier[2],
            "walk_forward": walk_forward(runs, tier, days, scored_runs=scored),
            "bands_walk_forward": {band[0]: walk_forward(runs, tier, days, band, scored) for band in BANDS},
            "bands_all_rules": band_summary(runs, tier),
            "bands_first_half": band_summary(runs, tier, 0, middle),
            "bands_second_half": band_summary(runs, tier, middle, days),
            "winning_days": winning_days_vs_profit(runs, tier),
            "best_rules": best_rules(runs, tier),
        }
    return report


def _print_report(report: dict) -> None:
    print(f"\n{report['rules_tested']:,} rule runs on {report['days']} days of 5-minute prices.")
    print(f"Scored on the last {report['days'] - TRAIN_DAYS} days, using rules picked only from earlier data.\n")
    print("How often price moves far enough to pay for a round trip")
    for asset, rows in report["price_moves"].items():
        for row in rows:
            print(f"  {asset} {row['threshold_pct']:.2f}%: {row['moves_per_day']:5.1f} a day, median {row['median_hours']:5.1f} hours")
    for asset, change in report["periods"].items():
        print(f"  {asset}: {change['first_half_pct']:+.0f}% in the first half, {change['second_half_pct']:+.0f}% in the second")
    hold = report["hold"]
    print(f"\nBuy and hold: {hold['annual_pct']:+.1f}% a year, green days {hold['green_days_pct']:.0f}%, worst drawdown {hold['max_drawdown_pct']:.1f}%\n")
    for name, tier in report["tiers"].items():
        w = tier["walk_forward"]
        print(f"{name} ({tier['maker_pct']}% maker, {tier['taker_pct']}% taker)")
        print(f"  Picked rules: {w['annual_pct']:+.1f}% a year, {w['trades_per_day']:.2f} trades a day, green days {w['green_days_pct']:.0f}%, "
              f"red days {w['red_days_pct']:.0f}%, drawdown {w['max_drawdown_pct']:.1f}%, fees {w['fees_pct']:.1f}%, in cash {w['in_cash_pct']:.0f}%")
        for band, result in tier["bands_walk_forward"].items():
            print(f"    Only {band}: {result['annual_pct']:+.1f}% a year, {result['trades_per_day']:.2f} trades a day, "
                  f"green days {result['green_days_pct']:.0f}%, fees {result['fees_pct']:.1f}%")
        for label, key in (("All", "bands_all_rules"), ("First half", "bands_first_half"), ("Second half", "bands_second_half")):
            for row in tier[key]:
                if row["rules"]:
                    print(f"    {label}, {row['band']} ({row['rules']} rules): {row['profitable_pct']:.0f}% profitable, "
                          f"median {row['median_net_per_trade_pct']:+.2f}% a trade, median {row['median_annual_pct']:+.0f}% a year")
        days = tier["winning_days"]
        print(f"    Winning days: best rule {days['most_winning_days']:.0f}%. Top tenth of rules by winning days: "
              f"median {days['top_decile_median_annual']:+.0f}% a year, {days['top_decile_profitable_pct']:.0f}% profitable. "
              f"Others: median {days['rest_median_annual']:+.0f}% a year. "
              f"Over 60% winning days: {days['rules_over_60pct_winning_days']} rules, {days['profitable_over_60pct']} profitable.")
        for rule in tier["best_rules"][:4]:
            print(f"    Hindsight best: {rule['asset']} {rule['family']} {rule['params']}: {rule['annual_pct']:+.0f}% a year, "
                  f"{rule['trades_per_day']:.2f} trades a day, first half {rule['first_half_pct']:+.0f}%, second half {rule['second_half_pct']:+.0f}%")


def main(data_dir: Path, assets=("BTC", "ETH", "SOL"), months: int = 25) -> dict:
    paths = ensure_history(assets, months, data_dir / "history")
    bars_by_asset = {asset: load_bars(str(path)) for asset, path in paths.items()}
    context = multiprocessing.get_context("spawn")
    with context.Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
        results = pool.map(_run_task, tasks_for(paths), chunksize=8)
    runs: dict[str, list[Run]] = {asset: [] for asset in assets}
    for run in results:
        runs[run.asset].append(run)
    report = analyse(runs, bars_by_asset)
    output = data_dir / "backtest.json"
    output.write_text(json.dumps(report, indent=2))
    _print_report(report)
    print(f"\nFull results: {output}")
    return report


# ---- Year by year, over a longer history ----

# Only rules the bot's own limits would allow: targets clear the 1.6% hurdle for resting buys, and stops
# are required. The fast rules are left out because none made money in the two-year test, even with no fees.
YEARLY_DIP_GRID = list(itertools.product((0.5, 1, 2, 3, 5), (2, 3, 5, 8), (2, 4, 8), (12, 48, 288), (False, True)))
YEARLY_HOLD_BARS = BARS_PER_DAY * 14
YEARLY_TIERS = (FEE_TIERS[0], FEE_TIERS[2], FEE_TIERS[4])


def _run_year_task(task: tuple) -> Run:
    asset, path, family, params, first_day = task
    bars = load_bars(path, first_day)
    if family == "dip":
        entry, target, stop, lookback, use_trend = params
        return run_dip(bars, asset, _indicator(path, "sma", lookback), _indicator(path, "sma", BARS_PER_DAY * 7),
                       entry, target, stop, lookback, use_trend, YEARLY_HOLD_BARS)
    if family == "breakout":
        lookback, stop, trail = params
        return run_breakout(bars, asset, _indicator(path, "high", lookback), lookback, stop, trail)
    fast, slow = params
    return run_trend(bars, asset, _indicator(path, "sma", fast), _indicator(path, "sma", slow), fast, slow)


def year_spans(first_day: int, start: int, days: int) -> list[tuple[int, int, int]]:
    """Calendar years as (year, first index, index after last), from index start up to days."""
    spans: list[tuple[int, int, int]] = []
    for index in range(start, days):
        year = datetime.fromtimestamp((first_day + index) * 86400, timezone.utc).year
        if spans and spans[-1][0] == year:
            spans[-1] = (year, spans[-1][1], index + 1)
        else:
            spans.append((year, index, index + 1))
    return spans


def yearly_rows(result: dict, runs: dict[str, list[Run]], tier: tuple, bars_by_asset: dict[str, Bars],
                first_day: int, days: int) -> list[dict]:
    """One row per calendar year, all scored only on rules picked from earlier data."""
    _, maker_pct, taker_pct, _ = tier
    nets = [(net_daily(run, maker_pct, taker_pct), run.trades) for asset_runs in runs.values() for run in asset_runs]
    closes_by_asset = {}
    for asset, bars in bars_by_asset.items():
        closes: dict[int, float] = {}
        for day, close in zip(bars.day, bars.close):
            closes[day] = close
        closes_by_asset[asset] = closes

    def hold_change(asset: str, start: int, end: int) -> float | None:
        closes = closes_by_asset[asset]
        opening = next((closes[d] for d in range(start - 1, end) if d in closes), None)
        closing = next((closes[d] for d in range(end - 1, start - 1, -1) if d in closes), None)
        return None if opening is None or closing is None else (closing / opening - 1) * 100

    rows = []
    for year, start, end in year_spans(first_day, TRAIN_DAYS, days):
        rule_returns = [sum(net[start:end]) * 100 for net, trades in nets if sum(trades[start:end]) > 0]
        holds = {asset: hold_change(asset, start, end) for asset in bars_by_asset}
        listed = [v for v in holds.values() if v is not None]
        rows.append({
            "year": year,
            "days": end - start,
            "picked_pct": sum(result["daily"][start:end]) * 100,
            "fees_pct": sum(result["fees_daily"][start:end]) * 100,
            "trades": sum(result["trades_daily"][start:end]),
            "profitable_days_pct": sum(v > 0 for v in result["daily"][start:end]) / (end - start) * 100,
            "hold_btc_pct": holds.get("BTC"),
            # The same size of position in each asset the rules could trade, so it compares like with like.
            "hold_same_size_pct": sum(listed) * PORTFOLIO_SLEEVE,
            "rules_active": len(rule_returns),
            "rules_profitable_pct": sum(v > 0 for v in rule_returns) / len(rule_returns) * 100 if rule_returns else None,
            "median_rule_pct": statistics.median(rule_returns) if rule_returns else None,
        })
    return rows


def yearly_main(data_dir: Path, assets=("BTC", "ETH", "SOL"), months: int = 96) -> dict:
    paths = ensure_history(assets, months, data_dir / "history")
    first_day = min(load_bars(str(path)).ts[0] // 86400 for path in paths.values())
    load_bars.cache_clear()
    runs: dict[str, list[Run]] = {asset: [] for asset in assets}
    grid = ([("dip", p) for p in YEARLY_DIP_GRID] + [("breakout", p) for p in BREAKOUT_GRID]
            + [("trend", p) for p in TREND_GRID])
    context = multiprocessing.get_context("spawn")
    for asset, path in paths.items():
        # One asset at a time, so each worker only ever holds one asset's history in memory.
        tasks = [(asset, str(path), family, params, first_day) for family, params in grid]
        with context.Pool(max(1, min(6, (os.cpu_count() or 2) - 1))) as pool:
            runs[asset] = pool.map(_run_year_task, tasks, chunksize=8)
        print(f"{asset}: {len(tasks)} rules tested", file=sys.stderr, flush=True)

    bars_by_asset = {asset: load_bars(str(path), first_day) for asset, path in paths.items()}
    days = min(bars.days for bars in bars_by_asset.values())
    for asset_runs in runs.values():
        for run in asset_runs:
            for name in ("gross", "maker", "taker", "trades"):
                setattr(run, name, getattr(run, name)[:days])

    report: dict = {"first_day": first_day, "days": days, "rules_per_asset": len(grid), "tiers": {}}
    for tier in YEARLY_TIERS:
        result = walk_forward(runs, tier, days, keep_daily=True)
        rows = yearly_rows(result, runs, tier, bars_by_asset, first_day, days)
        scored_years = (days - TRAIN_DAYS) / 365
        report["tiers"][tier[0]] = {
            "maker_pct": tier[1],
            "taker_pct": tier[2],
            "years": rows,
            "average_per_year_pct": sum(r["picked_pct"] for r in rows) / scored_years,
            "average_hold_btc_pct": sum(r["hold_btc_pct"] or 0 for r in rows) / scored_years,
            "average_hold_same_size_pct": sum(r["hold_same_size_pct"] for r in rows) / scored_years,
            "losing_years": sum(r["picked_pct"] < 0 for r in rows),
            "max_drawdown_pct": result["max_drawdown_pct"],
            "green_days_pct": result["green_days_pct"],
            "red_days_pct": result["red_days_pct"],
            "fees_per_year_pct": result["fees_pct"] / scored_years,
            "trades_per_year": sum(r["trades"] for r in rows) / scored_years,
            "in_cash_pct": result["in_cash_pct"],
            "chosen": result["chosen"],
        }
    (data_dir / "backtest_years.json").write_text(json.dumps(report, indent=2))
    _print_yearly(report)
    return report


def _print_yearly(report: dict) -> None:
    def show(value, digits=1):
        return "n/a" if value is None else f"{value:+.{digits}f}%"

    print(f"\n{report['rules_per_asset']} rules per asset. Each quarter uses only the rule that did best over the previous year.")
    print("Portfolio: up to 26.7% of capital per asset. Percentages are of starting capital, without compounding. No API costs.\n")
    for name, tier in report["tiers"].items():
        print(f"{name} ({tier['maker_pct']}% maker, {tier['taker_pct']}% taker)")
        print(f"  {'Year':<6}{'Days':>5}{'Picked rules':>14}{'Same size held':>16}{'BTC held':>11}{'Median rule':>13}{'Rules up':>10}{'Trades':>8}{'Fees':>8}")
        for r in tier["years"]:
            rules_up = "n/a" if r["rules_profitable_pct"] is None else f"{r['rules_profitable_pct']:.0f}%"
            print(f"  {r['year']:<6}{r['days']:>5}{show(r['picked_pct']):>14}{show(r['hold_same_size_pct']):>16}{show(r['hold_btc_pct'], 0):>11}"
                  f"{show(r['median_rule_pct']):>13}{rules_up:>10}{r['trades']:>8.0f}{r['fees_pct']:>7.1f}%")
        print(f"  Average per year: picked rules {show(tier['average_per_year_pct'])}, same size held {show(tier['average_hold_same_size_pct'])}, "
              f"BTC held {show(tier['average_hold_btc_pct'], 0)}.")
        print(f"  Losing years {tier['losing_years']} of {len(tier['years'])}. Worst drawdown {tier['max_drawdown_pct']:.1f}%. "
              f"Up days {tier['green_days_pct']:.0f}%, down days {tier['red_days_pct']:.0f}%, in cash {tier['in_cash_pct']:.0f}% of quarters. "
              f"Fees {tier['fees_per_year_pct']:.1f}% a year, {tier['trades_per_year']:.0f} trades a year.\n")
