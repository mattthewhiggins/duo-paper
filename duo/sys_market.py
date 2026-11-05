"""Kraken prices for the systematic runner: public endpoints only, for whichever assets the strategy needs today.

A TOP-n universe changes from day to day, so the set of tracked assets does too: BTC (the benchmark), everything held,
everything targeted and everything with an order working. Kraken's name for each coin comes from the research cost
model (XBT for BTC, XDG for DOGE), not a guess.

A quote's age is measured from when it was fetched, which can't see a feed that keeps answering with the same numbers
(audit finding 57). So each quote also carries how long its bid, ask, last price, 24-hour volume and 24-hour trade count
have gone without changing at all. A liquid market changes every few seconds. The runner refuses new orders on a quote
frozen for longer than max_frozen_quote_seconds.

A scheduled run (python -m duo tick) sees one quote every 15 to 60 minutes, so it judges resting paper orders on the
price path in between instead: Kraken's 1-minute candles, read once per run.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal

from .kraken import KrakenClient
from .market import BENCHMARK_ASSET, MINUTE, Candle, Market, PairInfo, Quote, kraken_number


class SystematicMarket(Market):
    def __init__(self, client: KrakenClient, quote: str, kraken_bases: dict[str, str]):
        super().__init__(client, (BENCHMARK_ASSET,), quote)
        self.kraken_bases = dict(kraken_bases)
        self.unavailable: dict[str, str] = {}
        self.unchanged_since: dict[str, float] = {}
        self._signatures: dict[str, tuple] = {}
        self._track_lock = threading.Lock()
        self._minutes: dict[str, list[Candle] | str] = {}
        self._minutes_lock = threading.Lock()

    def track(self, assets, kraken_bases: dict[str, str] | None = None) -> None:
        """Sets what refresh() fetches. BTC is always in, for the benchmark."""
        with self._track_lock:
            if kraken_bases:
                self.kraken_bases.update(kraken_bases)
            wanted = tuple(dict.fromkeys((BENCHMARK_ASSET, *sorted(assets))))
            self.tracked = wanted
            self.assets = wanted

    def _altname(self, asset: str) -> str:
        return f"{self.kraken_bases.get(asset, asset)}{self.quote}"

    def load_pairs(self) -> None:
        missing = [a for a in self.tracked if a not in self.pairs and a not in self.unavailable]
        if not missing:
            return
        names = {self._altname(a): a for a in missing}
        result = self.client.public("AssetPairs", pair=",".join(names))
        pairs = dict(self.pairs)
        for key, info in result.items():
            asset = names.get(info.get("altname"))
            if asset:
                pairs[asset] = PairInfo(
                    asset=asset, key=key, altname=info["altname"], base_code=info["base"], quote_code=info["quote"],
                    lot_decimals=int(info["lot_decimals"]), pair_decimals=int(info["pair_decimals"]),
                    ordermin=kraken_number(info.get("ordermin", 0), f"{key}'s ordermin"),
                    costmin=kraken_number(info.get("costmin", 0), f"{key}'s costmin"),
                    online=info.get("status", "online") == "online",
                )
        for asset in missing:
            if asset not in pairs:
                if asset == BENCHMARK_ASSET:
                    raise ValueError(f"Kraken has no {self._altname(asset)} pair")
                self.unavailable[asset] = f"Kraken has no {self._altname(asset)} pair"
        self.pairs = pairs

    def refresh(self) -> dict[str, Quote]:
        with self._lock:
            self.load_pairs()
            wanted = [self.pairs[a] for a in self.tracked if a in self.pairs]
            by_key = {info.key: info.asset for info in wanted}
            result = self.client.public("Ticker", pair=",".join(p.altname for p in wanted))
            now = time.time()
            quotes = {}
            for key, ticker in result.items():
                asset = by_key.get(key)
                if not asset:
                    continue
                quotes[asset] = Quote(asset, kraken_number(ticker["b"][0], f"{key}'s bid"),
                                      kraken_number(ticker["a"][0], f"{key}'s ask"),
                                      kraken_number(ticker["c"][0], f"{key}'s last trade"), now)
                signature = tuple(str(x) for x in (ticker["b"][0], ticker["a"][0], ticker["c"][0], ticker.get("v"),
                                                   ticker.get("t")))
                if self._signatures.get(asset) != signature:
                    self._signatures[asset] = signature
                    self.unchanged_since[asset] = now
            self.quotes = quotes
            return quotes

    def frozen_state(self) -> dict:
        """What frozen-feed detection knows, for the runner to keep in its database across a restart."""
        with self._lock:
            return {a: {"signature": list(sig), "since": self.unchanged_since[a]}
                    for a, sig in self._signatures.items() if a in self.unchanged_since}

    def restore_frozen_state(self, state: dict) -> None:
        """After a restart: a feed that answers exactly as it did before the restart has been frozen since then, not
        since the restart. Only fills in what this process hasn't seen yet."""
        with self._lock:
            for asset, entry in (state or {}).items():
                try:
                    signature, since = tuple(str(x) for x in entry["signature"]), float(entry["since"])
                except (KeyError, TypeError, ValueError):
                    continue
                if asset not in self._signatures:
                    self._signatures[asset] = signature
                    self.unchanged_since[asset] = since

    def frozen_seconds(self, asset: str, now: float) -> float | None:
        since = self.unchanged_since.get(asset)
        return None if since is None else max(0.0, now - since)

    def minute_candles(self, asset: str) -> list[Candle]:
        """Kraken's 1-minute candles for an asset, oldest first: at most the 720 most recent (12 hours), each stamped
        with its minute's start in seconds, the last one still forming.

        Read once per process and kept, failure included: a scheduled run is one process, so every resting order it
        judges is judged on the same path, and a feed that's down isn't asked again for each one. If no refresh has
        loaded the pairs yet (every refresh of the run so far failed), they're loaded here first, so the answer kept is
        Kraken's own and not just that no pair was known.
        """
        with self._minutes_lock:
            if asset not in self._minutes:
                try:
                    if asset not in self.pairs:
                        with self._lock:
                            self.load_pairs()
                    if asset not in self.pairs:
                        raise ValueError(f"no Kraken pair known for {asset}")
                    self._minutes[asset] = self.candles(asset, MINUTE, 720)
                except Exception as error:  # Whatever went wrong, the answer for this run is "no candles".
                    self._minutes[asset] = f"{type(error).__name__}: {error}"
            kept = self._minutes[asset]
        if isinstance(kept, str):
            raise ValueError(kept)
        return kept

    def gbp_usd(self) -> Decimal:
        """US dollars per pound, from Kraken's public GBPUSD ticker."""
        result = self.client.public("Ticker", pair="GBPUSD")
        ticker = next(iter(result.values()))
        rate = kraken_number(ticker["c"][0], "the GBPUSD rate")
        if rate <= 0:
            raise ValueError(f"GBPUSD rate {rate} isn't usable")
        return rate
