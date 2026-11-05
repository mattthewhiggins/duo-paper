"""Stand-ins for Binance's public klines and Kraken's public quotes, so the systematic runner's tests never touch a network."""
from __future__ import annotations

import io
import json
import tempfile
import urllib.error
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from duo import config as config_module
from duo.brokers import PaperBroker
from duo.db import DB
from duo.kraken import KrakenNetworkError
from duo.market import Candle, PairInfo, Quote
from duo.systematic import SystematicRunner, prepare

DAY = 86400
D0 = int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())  # A Monday 00:00 UTC.


# The limits these tests were written against. config.toml is the owner's paper setup and changes with the strategy
# they choose, so the tests set these themselves rather than move with it.
TEST_LIMITS = {"max_asset_pct": Decimal(50), "allow_capped_targets": False}


def make_sys_config(strategy="F1__ENS__BTC_ETH__vol40", mode="paper", **overrides):
    config = config_module.load(data_dir=Path(tempfile.mkdtemp()))
    systematic = replace(config.systematic, enabled=True, strategy=strategy, **{**TEST_LIMITS, **overrides})
    return replace(config, mode=mode, systematic=systematic)


def series(start_price: float, last_ts: int, days: int, drift=0.003, swing=0.03) -> list[tuple]:
    """Daily bars ending at last_ts: a steady drift with an alternating swing, so ENS is on and vol is about 57%."""
    rows, close = [], start_price
    first = last_ts - (days - 1) * DAY
    for i in range(days):
        ts = first + i * DAY
        bar_open = close
        close = bar_open * (1 + drift + (swing if i % 2 else -swing))
        rows.append((ts, bar_open, max(bar_open, close) * 1.01, min(bar_open, close) * 0.99, close, 1000.0,
                     1000.0 * close))
    return rows


def kline(row: tuple) -> list:
    ts, o, h, low, c, v, qv = row
    return [ts * 1000, repr(o), repr(h), repr(low), repr(c), repr(v), ts * 1000 + DAY * 1000 - 1, repr(qv), 10,
            "0", "0", "0"]


class FakeBinance:
    """Serves rows whose bar has opened by `now`, newest last, as the real endpoint does: the last one is forming."""

    def __init__(self, bars: dict[str, list[tuple]], now: float):
        self.bars = bars
        self.now = now
        self.missing: set[int] = set()
        self.fail = False
        self.calls = []

    def klines(self, symbol: str, limit: int) -> list:
        from duo.sys_data import DataError
        self.calls.append(("klines", symbol, limit))
        if self.fail:
            raise DataError("Binance klines: offline")
        asset = symbol[: -len("USDT")]
        rows = [kline(r) for r in self.bars.get(asset, []) if r[0] <= self.now and r[0] not in self.missing]
        return rows[-limit:]

    def usdt_symbols(self) -> dict[str, str]:
        self.calls.append(("exchangeInfo",))
        return {asset: f"{asset}USDT" for asset in self.bars}


def _pair(asset, ordermin="0.0001", pair_decimals=2, lot_decimals=8):
    return PairInfo(asset, f"X{asset}ZUSD", f"{asset}USD", f"X{asset}", "ZUSD", lot_decimals, pair_decimals,
                    Decimal(ordermin), Decimal("0.5"), True)


class FakeSysMarket:
    """Kraken's public quotes for whatever is tracked, at self.prices with a small spread, stamped at self.now."""

    def __init__(self, prices: dict[str, float], now: float, spread_bps=2):
        self.prices = dict(prices)
        self.now = now
        self.spread_bps = spread_bps
        self.quote = "USD"
        self.tracked = ("BTC",)
        self.assets = self.tracked
        self.pairs = {"BTC": _pair("BTC", "0.00006", 1)}
        self.quotes: dict[str, Quote] = {}
        self.unavailable: dict[str, str] = {}
        self.frozen: dict[str, float] = {}
        self.fail = False
        self.no_pair: set[str] = set()
        self.lag = 0.0  # Real quotes are stamped when fetched, a moment after the loop's own clock.

    def track(self, assets, kraken_bases=None):
        self.tracked = tuple(dict.fromkeys(("BTC", *sorted(assets))))
        self.assets = self.tracked
        for asset in self.tracked:
            if asset in self.no_pair:
                self.unavailable[asset] = f"Kraken has no {asset}USD pair"
            elif asset not in self.pairs:
                self.pairs[asset] = _pair(asset)

    def refresh(self):
        if self.fail:
            raise KrakenNetworkError("offline")
        quotes = {}
        for asset in self.tracked:
            if asset in self.prices and asset in self.pairs:
                mid = Decimal(str(self.prices[asset]))
                half = mid * Decimal(self.spread_bps) / Decimal(20000)
                quotes[asset] = Quote(asset, mid - half, mid + half, mid, self.now + self.lag)
        self.quotes = quotes
        return quotes

    def load_pairs(self):
        pass

    def frozen_seconds(self, asset, now):
        return self.frozen.get(asset, 0.0)

    def gbp_usd(self):
        return Decimal("1.25")

    def usd_to_quote(self):
        return Decimal(1)


class PathMarket(FakeSysMarket):
    """FakeSysMarket with Kraken's 1-minute candles too, along a price path: path(asset, t) is the mid at second t, and
    each refresh quotes the path at `now`. A candle runs from the path at its minute's start to the path at its end (at
    `now`, for the one still forming), its high and low a hair (wick) beyond those. Any minute can be set by hand
    (candles), left out (gaps), or every call made to fail (candles_fail). A new one stands for a new process."""

    def __init__(self, path, now: float, assets=("BTC", "ETH"), wick=0.00002, spread_bps=2):
        super().__init__({a: path(a, now) for a in assets}, now, spread_bps)
        self.path = path
        self.wick = wick
        self.candles: dict[tuple[str, int], tuple[float, float, float, float]] = {}  # (asset, minute): o, h, l, c
        self.gaps: set[int] = set()
        self.candles_fail = False
        self.candle_calls: list[str] = []
        self.refresh_failures = 0  # This many refreshes fail before one works.
        self.refreshes = 0
        self.restored = None

    def refresh(self):
        self.refreshes += 1
        if self.refresh_failures > 0:
            self.refresh_failures -= 1
            raise KrakenNetworkError("offline for a moment")
        self.prices = {a: self.path(a, self.now) for a in self.prices}
        return super().refresh()

    def minute_candles(self, asset):
        self.candle_calls.append(asset)
        if self.candles_fail:
            raise KrakenNetworkError("OHLC: offline")
        newest = int(self.now // 60 * 60)
        out = []
        for ts in range(newest - 719 * 60, newest + 60, 60):
            if ts in self.gaps:
                continue
            if (asset, ts) in self.candles:
                o, h, low, c = self.candles[(asset, ts)]
            else:
                o, c = self.path(asset, ts), self.path(asset, min(ts + 60, self.now))
                h, low = max(o, c) * (1 + self.wick), min(o, c) * (1 - self.wick)
            out.append(Candle(ts, o, h, low, c, 1.0))
        return out

    def frozen_state(self):
        """Here, which refresh the quotes came from, so a test can tell which one was saved."""
        return {a: {"signature": [str(q.bid), str(q.ask)], "since": q.ts, "refresh": self.refreshes}
                for a, q in self.quotes.items()}

    def restore_frozen_state(self, state):
        self.restored = state


def flat(prices: dict[str, float]):
    """A price path that stays put."""
    return lambda asset, t: prices[asset]


class Reply(io.BytesIO):
    status = 200


class FakeHosts:
    """urllib.request.urlopen for BinancePublic, serving a FakeBinance's klines and symbols from any host, except that
    a host in `answers` refuses with that HTTP status or raises that exception."""

    def __init__(self, binance: FakeBinance, answers: dict | None = None):
        self.binance = binance
        self.answers = dict(answers or {})
        self.calls: list[str] = []

    def __call__(self, url, timeout=None):
        parts = urllib.parse.urlsplit(url)
        self.calls.append(parts.netloc)
        answer = self.answers.get(parts.netloc)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "refused", {}, io.BytesIO(b"{}"))
        if isinstance(answer, BaseException):
            raise answer
        params = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path.endswith("/klines"):
            body = self.binance.klines(params["symbol"], int(params["limit"]))
        else:
            body = {"symbols": [{"symbol": s, "baseAsset": a, "quoteAsset": "USDT", "status": "TRADING"}
                                for a, s in self.binance.usdt_symbols().items()]}
        return Reply(json.dumps(body).encode())


def make_env(strategy="F1__ENS__BTC_ETH__vol40", days=700, extra_days=10, bars=None, prices=None, now=D0 + 60,
             market=None, scheduled=False, **overrides):
    """A runner on fake data. Bars run to D0 + extra_days, but only those opened by `now` are ever served."""
    config = make_sys_config(strategy, **overrides)
    if bars is None:
        last = D0 + extra_days * DAY
        bars = {"BTC": series(20000.0, last, days + extra_days), "ETH": series(1500.0, last, days + extra_days)}
    if prices is None:
        prices = {a: rows_close_before(rows, D0) for a, rows in bars.items()}
    db = DB(config.db_path)
    market = market or FakeSysMarket(prices, now)
    effective, strategy_obj, start = prepare(config, db, market, now=now)
    broker = PaperBroker(effective)
    binance = FakeBinance(bars, now)
    runner = SystematicRunner(effective, db, market, broker, binance, strategy_obj, start, scheduled=scheduled)
    return SimpleNamespace(config=effective, raw_config=config, db=db, market=market, broker=broker, binance=binance,
                           runner=runner, bars=bars, strategy=strategy_obj, start=start)


def rows_close_before(rows, ts):
    return [r for r in rows if r[0] < ts][-1][4]


def step(env, now, prices=None):
    """One engine poll at `now`: fresh quotes, then the runner. A loop runs if one is due."""
    if prices:
        env.market.prices.update(prices)
    env.market.now = now
    env.binance.now = now
    try:
        env.market.refresh()
        env.runner.on_tick(now)
    except KrakenNetworkError as error:
        env.runner.on_tick(now, error=str(error))


def run_until(env, start, end, every=60, prices=None):
    t = start
    while t <= end:
        step(env, t, prices)
        t += every


def cron_run(env, now):
    """One scheduled run at `now` on env's database, as python -m duo tick makes it: a fresh runner with scheduled=True
    (so its loop runs whatever the time) on a market with no quotes yet, every book asset tracked before the refresh,
    then one poll."""
    env.market.now = env.binance.now = now
    env.market.quotes = {}
    env.runner = SystematicRunner(env.config, env.db, env.market, PaperBroker(env.config), env.binance, env.strategy,
                                  env.start, scheduled=True)
    env.runner._track()
    try:
        env.market.refresh()
    except KrakenNetworkError as error:
        env.runner.on_tick(now, error=str(error))
        return
    env.runner.on_tick(now)
