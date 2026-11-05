"""Live prices and candles from Kraken's public API."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from .config import D
from .kraken import KrakenClient

BENCHMARK_ASSET = "BTC"
CANDLE_INTERVALS = (15, 60, 240, 1440)  # minutes, the ones the agents can ask for
MINUTE = 1  # Kraken's 1-minute candles too, which the systematic runner's scheduled runs judge paper fills on
_KRAKEN_NAMES = {"BTC": "XBT"}


@dataclass(frozen=True)
class Quote:
    asset: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: float

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class PairInfo:
    asset: str
    key: str            # Key in Kraken's responses, e.g. XXBTZUSD
    altname: str        # Name used in requests, e.g. XBTUSD
    base_code: str      # Key in the Balance response, e.g. XXBT
    quote_code: str     # e.g. ZUSD
    lot_decimals: int
    pair_decimals: int
    ordermin: Decimal
    costmin: Decimal
    online: bool


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Market:
    def __init__(self, client: KrakenClient, assets: tuple[str, ...], quote: str):
        self.client = client
        self.assets = assets
        self.quote = quote
        self.tracked = tuple(dict.fromkeys((*assets, BENCHMARK_ASSET)))
        self.pairs: dict[str, PairInfo] = {}
        self.quotes: dict[str, Quote] = {}
        self._lock = threading.Lock()
        self._candles: dict[tuple[str, int], tuple[float, list[Candle]]] = {}
        self._usd_rate: tuple[float, float] | None = None

    def load_pairs(self) -> None:
        names = {f"{_KRAKEN_NAMES.get(a, a)}{self.quote}": a for a in self.tracked}
        result = self.client.public("AssetPairs", pair=",".join(names))
        pairs = {}
        for key, info in result.items():
            asset = names.get(info["altname"])
            if asset:
                pairs[asset] = PairInfo(
                    asset=asset,
                    key=key,
                    altname=info["altname"],
                    base_code=info["base"],
                    quote_code=info["quote"],
                    lot_decimals=int(info["lot_decimals"]),
                    pair_decimals=int(info["pair_decimals"]),
                    ordermin=D(info.get("ordermin", 0)),
                    costmin=D(info.get("costmin", 0)),
                    online=info.get("status", "online") == "online",
                )
        missing = set(self.tracked) - set(pairs)
        if missing:
            raise ValueError(f"Kraken has no {self.quote} pair for: {', '.join(sorted(missing))}")
        self.pairs = pairs

    def refresh(self) -> dict[str, Quote]:
        with self._lock:
            if not self.pairs:
                self.load_pairs()
            by_key = {info.key: asset for asset, info in self.pairs.items()}
            result = self.client.public("Ticker", pair=",".join(p.altname for p in self.pairs.values()))
            now = time.time()
            quotes = {}
            for key, ticker in result.items():
                asset = by_key.get(key)
                if asset:
                    quotes[asset] = Quote(asset, D(ticker["b"][0]), D(ticker["a"][0]), D(ticker["c"][0]), now)
            self.quotes = quotes
            return quotes

    def candles(self, asset: str, interval: int, count: int = 720) -> list[Candle]:
        """Most recent candles, oldest first, each stamped with the start of its interval in seconds. The newest one is
        still forming. Kraken returns at most the 720 most recent."""
        if interval not in (MINUTE, *CANDLE_INTERVALS):
            raise ValueError(f"interval must be one of {(MINUTE, *CANDLE_INTERVALS)}")
        cache_key = (asset, interval)
        cached = self._candles.get(cache_key)
        if cached and time.time() - cached[0] < 120:
            return cached[1][-count:]
        pair = self.pairs[asset]
        result = self.client.public("OHLC", pair=pair.altname, interval=interval)
        rows = result.get(pair.key) or next(v for k, v in result.items() if k != "last")
        candles = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in rows]
        self._candles[cache_key] = (time.time(), candles)
        return candles[-count:]

    def summary(self, asset: str) -> dict:
        """The headline numbers for the agents' briefing."""
        quote = self.quotes[asset]
        hourly = self.candles(asset, 60)
        daily = self.candles(asset, 1440)
        price = float(quote.mid)

        def change(candles: list[Candle], back: int) -> float | None:
            if len(candles) <= back:
                return None
            return (price / candles[-1 - back].close - 1) * 100

        def sma(candles: list[Candle], length: int) -> float | None:
            if len(candles) < length:
                return None
            return sum(c.close for c in candles[-length:]) / length

        returns = [math.log(b.close / a.close) for a, b in zip(hourly[-25:-1], hourly[-24:])]
        volatility = None
        if len(returns) > 1:
            mean = sum(returns) / len(returns)
            volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) * math.sqrt(24) * 100
        recent = daily[-30:]
        return {
            "price": price,
            "spread_pct": float((quote.ask - quote.bid) / quote.mid * 100),
            "change_1h_pct": change(hourly, 1),
            "change_24h_pct": change(hourly, 24),
            "change_7d_pct": change(daily, 7),
            "change_30d_pct": change(daily, 30),
            "volatility_24h_pct": volatility,
            "sma_20d": sma(daily, 20),
            "sma_50d": sma(daily, 50),
            "high_30d": max((c.high for c in recent), default=None),
            "low_30d": min((c.low for c in recent), default=None),
        }

    def usd_to_quote(self) -> Decimal:
        """How much of the portfolio's currency one US dollar buys. API costs are billed in USD."""
        if self.quote == "USD":
            return Decimal(1)
        if self._usd_rate and time.time() - self._usd_rate[0] < 3600:
            return D(self._usd_rate[1])
        result = self.client.public("Ticker", pair=f"{self.quote}USD")
        ticker = next(iter(result.values()))
        rate = 1 / float(ticker["c"][0])
        self._usd_rate = (time.time(), rate)
        return D(rate)
