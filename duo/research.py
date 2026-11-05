"""Lets the agents test a strategy idea on real price history, so their discussions rest on evidence."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import backtest
from .config import Config
from .db import DB


def history_file(data_dir: Path, asset: str) -> Path | None:
    """The longest history downloaded for an asset, if any."""
    best, best_months = None, 0
    for path in (Path(data_dir) / "history").glob(f"{asset}-5m-*mo.csv.gz"):
        match = re.search(r"-(\d+)mo\.csv\.gz$", path.name)
        if match and int(match.group(1)) > best_months:
            best, best_months = path, int(match.group(1))
    return best


def _bounded(inputs: dict, name: str, low: float, high: float) -> float:
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is required for this rule")
    if not low <= value <= high:
        raise ValueError(f"{name} has to be between {low} and {high}")
    return float(value)


def test_rule(config: Config, db: DB, inputs: dict) -> tuple[str, str]:
    """Runs one rule on the full history. Returns (results for the agent, short label for the dashboard).

    Raises ValueError with a message the agent can act on.
    """
    asset, rule = inputs.get("asset"), inputs.get("rule")
    path = history_file(config.data_dir, asset)
    if path is None:
        raise ValueError(f"No price history is downloaded for {asset} yet. Run the backtest command first.")
    stop = _bounded(inputs, "stop_pct", 0.5, 30)
    bars = backtest.load_bars(str(path))

    if rule == "dip":
        entry = _bounded(inputs, "entry_pct", 0.1, 20)
        target = _bounded(inputs, "target_pct", 0.3, 50)
        hours = int(_bounded(inputs, "average_hours", 1, 240))
        uptrend = bool(inputs.get("only_in_uptrend"))
        lookback = hours * 12
        trend = backtest.sma(bars.close, backtest.BARS_PER_DAY * 7) if uptrend else ()
        run = backtest.run_dip(bars, asset, backtest.sma(bars.close, lookback), trend, entry, target, stop,
                               lookback, uptrend, backtest.YEARLY_HOLD_BARS)
        description = (f"{asset} dip rule: resting buy {entry}% below the {hours}h average"
                       f"{' while above the 7-day average' if uptrend else ''}, target {target}%, stop {stop}%")
    elif rule == "breakout":
        hours = int(_bounded(inputs, "lookback_hours", 1, 240))
        trail = _bounded(inputs, "trail_pct", 0.3, 30)
        run = backtest.run_breakout(bars, asset, backtest.rolling_high(bars.high, hours * 12), hours * 12, stop, trail)
        description = f"{asset} breakout rule: immediate buy above the {hours}h high, stop {stop}%, trailing stop {trail}%"
    else:
        raise ValueError("rule has to be dip or breakout")

    maker, taker = float(config.maker_pct), float(config.taker_pct)
    net = backtest.net_daily(run, maker, taker)
    first_day = bars.ts[0] // 86400
    closes: dict[int, float] = {}
    for day, close in zip(bars.day, bars.close):
        closes[day] = close

    def stamp(day: int) -> str:
        return datetime.fromtimestamp((first_day + day) * 86400, timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"{description}. Fees {maker}% resting, {taker}% immediate.",
        f"Prices: 5-minute candles from Coinbase, {stamp(0)} to {stamp(bars.days - 1)}.",
        "year,net_pct_of_position_after_fees,round_trips,asset_held_pct",
    ]
    for year, start, end in backtest.year_spans(first_day, 0, bars.days):
        opening = next((closes[d] for d in range(max(start - 1, 0), end) if d in closes), None)
        closing = next((closes[d] for d in range(end - 1, start - 1, -1) if d in closes), None)
        held = (closing / opening - 1) * 100 if opening and closing else 0.0
        lines.append(f"{year},{sum(net[start:end]) * 100:+.1f},{sum(run.trades[start:end]):.0f},{held:+.0f}")

    years = bars.days / 365
    winning, losing = sum(v > 0 for v in net), sum(v < 0 for v in net)
    fees = (sum(run.maker) * maker + sum(run.taker) * taker) / 100
    lines.append(
        f"Average {sum(net) / years * 100:+.1f}% a year, {sum(run.trades) / years:.0f} round trips a year, "
        f"fees {fees / years * 100:.1f}% a year. Winning days {winning}, losing days {losing}."
    )
    count = int(db.get("rule_tests", "0")) + 1
    db.set("rule_tests", count)
    lines.append(
        f"Variations tested so far, by anyone on the desk: {count}. The best of many tries usually looks better than "
        "it is, and this ignores your place in the order queue, so treat a good result as a lead and not as proof."
    )
    return "\n".join(lines), f"Tested {description}"
