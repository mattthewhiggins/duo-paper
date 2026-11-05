"""Stand-ins for the market and for Claude, so tests never touch a network or spend money."""
from __future__ import annotations

import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from duo import config as config_module
from duo.brokers import PaperBroker
from duo.db import DB
from duo.engine import Engine
from duo.market import Candle, PairInfo, Quote


def make_config(**overrides):
    config = config_module.load(data_dir=Path(tempfile.mkdtemp()))
    # config.toml now switches the systematic runner on, which takes trading away from the agents. These tests are of
    # the agents' own paths, so they run with it off unless a test turns it on. tests/test_systematic.py covers it.
    if config.systematic is not None and "systematic" not in overrides:
        overrides["systematic"] = replace(config.systematic, enabled=False)
    return replace(config, mode="paper", **overrides)


def make_pairs() -> dict[str, PairInfo]:
    def pair(asset, key, altname, base, ordermin, decimals):
        return PairInfo(asset, key, altname, base, "ZUSD", 8, decimals, Decimal(ordermin), Decimal("0.5"), True)
    return {
        "BTC": pair("BTC", "XXBTZUSD", "XBTUSD", "XXBT", "0.00006", 1),
        "ETH": pair("ETH", "XETHZUSD", "ETHUSD", "XETH", "0.002", 2),
        "SOL": pair("SOL", "SOLUSD", "SOLUSD", "SOL", "0.05", 2),
    }


def quote(asset: str, price, spread="1", ts=None) -> Quote:
    price = Decimal(str(price))
    half = Decimal(spread) / 2
    return Quote(asset, price - half, price + half, price, time.time() if ts is None else ts)


class FakeMarket:
    def __init__(self, assets=("BTC", "ETH", "SOL"), prices=None):
        self.assets = assets
        self.tracked = assets
        self.quote = "USD"
        self.pairs = make_pairs()
        self.prices = prices or {"BTC": 80000, "ETH": 4000, "SOL": 200}
        self.quotes: dict[str, Quote] = {}
        self.fail = False
        self.refresh()

    def refresh(self):
        if self.fail:
            from duo.kraken import KrakenNetworkError
            raise KrakenNetworkError("offline")
        self.quotes = {a: quote(a, p) for a, p in self.prices.items()}
        return self.quotes

    def load_pairs(self):
        pass

    def candles(self, asset, interval, count=720):
        price = float(self.prices[asset])
        return [Candle(1700000000 + i * interval * 60, price, price * 1.01, price * 0.99, price, 10.0) for i in range(count)]

    def summary(self, asset):
        price = float(self.quotes[asset].mid)
        return {
            "price": price, "spread_pct": 0.01, "change_1h_pct": 0.1, "change_24h_pct": 1.0, "change_7d_pct": 2.0,
            "change_30d_pct": 3.0, "volatility_24h_pct": 2.0, "sma_20d": price, "sma_50d": price,
            "high_30d": price * 1.1, "low_30d": price * 0.9,
        }

    def usd_to_quote(self):
        return Decimal(1)


class Block(SimpleNamespace):
    pass


def tool_use(name: str, payload: dict, block_id: str = "toolu_1") -> Block:
    return Block(type="tool_use", name=name, input=payload, id=block_id)


def fake_response(*blocks, stop_reason="tool_use", model="claude-opus-5-5", input_tokens=1000, output_tokens=500):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, model=model, usage=usage, stop_details=None)


class ScriptedClient:
    """Plays back canned responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.requests.append({**params, "messages": list(params["messages"])})
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def strategist(message="Plan.", orders=None, stops=None, strategy=None, next_check=240, summary="Summary.", block_id="s1", cancel=None):
    return fake_response(tool_use("strategist_reply", {
        "message": message, "orders": orders or [], "stop_updates": stops or [], "cancel_plans": cancel or [], "strategy_update": strategy,
        "next_check_in_minutes": next_check, "summary": summary,
    }, block_id))


def risk(verdict="approve", message="Looks fine.", block_id="r1"):
    return fake_response(tool_use("risk_reply", {"message": message, "verdict": verdict}, block_id))


def buy(asset="BTC", amount=80, stop=72000, reason="Test buy", target=1_000_000, execution="immediate", trigger=None, hours=None):
    return {"asset": asset, "side": "buy", "quote_amount": amount, "sell_fraction": None, "stop_price": stop,
            "target_price": target, "execution": execution, "trigger_price": trigger, "trigger_hours": hours, "reason": reason}


def sell(asset="BTC", fraction=1, reason="Test sell", execution="immediate"):
    return {"asset": asset, "side": "sell", "quote_amount": None, "sell_fraction": fraction, "stop_price": None,
            "target_price": None, "execution": execution, "trigger_price": None, "trigger_hours": None, "reason": reason}


def make_engine(responses=(), config=None, market=None, broker=None):
    config = config or make_config()
    market = market or FakeMarket(config.assets)
    db = DB(config.db_path)
    client = ScriptedClient(responses)
    engine = Engine(config, db, market, broker or PaperBroker(config), client)
    return engine, client


def run_ticks(engine, count=1):
    """Ticks and waits for any session to finish, so tests stay deterministic."""
    for _ in range(count):
        engine.tick()
        thread = engine._session_thread
        if thread is not None:
            thread.join(timeout=10)
