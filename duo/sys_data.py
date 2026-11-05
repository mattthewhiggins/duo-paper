"""Signal data for the systematic runner: Binance public daily klines, built into a panel the research's way.

The research's spot_1d panel is Binance's USDT pairs, daily, one row per UTC day, assets in name order, with open, high,
low, close, volume and quote_volume (research/tools/fetch_binance.py), cleaned by research/harness/clean.py. This module
rebuilds the same structure from the live API, for the same exclusions, and cleans it with clean.py's own functions.

Only two public market-data endpoints are ever called: /api/v3/exchangeInfo and /api/v3/klines, on the hosts in
[systematic] binance_urls, tried in order (data-api.binance.vision, Binance's market-data-only host, then
api.binance.com). Both serve Binance's own spot klines. No key, no account, nothing private. Each decision records the
host that actually served its bars.

The forming bar is never used. Binance's newest kline is the day still trading, and every row whose close time hasn't
passed is dropped before the panel is built, then the panel is checked to end on the just-closed bar.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

from duo import sys_research  # noqa: F401  (sets dont_write_bytecode before research imports)
from duo.config import BINANCE_URLS
from research.harness import clean
from research.harness.data import CORE_FIELDS, HOLDOUT_START_TS, Panel
from research.tools.fetch_binance import EXCLUDED, QUOTE

DAY = 86400
DAY_MS = DAY * 1000
EXCLUDED_NAMES = {name for entry in EXCLUDED.values() for name in entry["names"]}
BREAKS_PATH = Path(__file__).resolve().parent / "research_breaks.json"
# A host that refuses (403, 418 for an IP ban, 429 for too many requests, 451 for a restricted location) or fails (5xx)
# is passed over for the next one. Any other answer, a 400 for a bad symbol say, would be the same on every host.
NEXT_HOST = {403, 418, 429, 451}


def source_for(hosts=()) -> str:
    """What a decision records as its signal source: the bars, and the host or hosts that actually served them."""
    where = f" from {' and '.join(sorted(hosts))}" if hosts else ""
    return f"Binance public spot klines, 1d, /api/v3/klines{where} (the research's source: spot_1d)"


SOURCE = source_for()


class DataError(Exception):
    """The signal data couldn't be fetched or didn't look right. Counts as an API error."""


def host(base_url: str) -> str:
    return urllib.parse.urlsplit(base_url).netloc or base_url


class BinancePublic:
    """GETs two public endpoints and nothing else, from the first of its hosts that answers."""

    PATHS = {"exchangeInfo", "klines"}

    def __init__(self, base_urls=BINANCE_URLS, timeout: float = 15, base_url: str | None = None):
        # base_url: a single host, as before there was a list.
        self.base_urls = tuple(u.rstrip("/") for u in ((base_url,) if base_url else base_urls))
        if not self.base_urls:
            raise ValueError("BinancePublic needs at least one host")
        self.timeout = timeout
        self.served_by: str | None = None  # The host that answered the last call.

    def _get(self, path: str, **params):
        if path not in self.PATHS:
            raise ValueError(f"{path} isn't one of the public endpoints this client may call")
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        failed = []
        for base in self.base_urls:
            try:
                with urllib.request.urlopen(f"{base}/api/v3/{path}{query}", timeout=self.timeout) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as error:
                failed.append(f"{host(base)}: HTTP {error.code} {error.reason}")
                if error.code in NEXT_HOST or error.code >= 500:
                    continue
                raise DataError(f"Binance {path}: {failed[-1]}") from error
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as error:
                # HTTPException covers a truncated reply (IncompleteRead) or a bad status line, which aren't OSErrors.
                failed.append(f"{host(base)}: {type(error).__name__}: {error}")
                continue
            self.served_by = host(base)
            return payload
        raise DataError(f"Binance {path}: " + "; ".join(failed))

    def klines(self, symbol: str, limit: int) -> list:
        rows = self._get("klines", symbol=symbol, interval="1d", limit=limit)
        if not isinstance(rows, list):
            raise DataError(f"Binance klines for {symbol} wasn't a list")
        return rows

    def usdt_symbols(self) -> dict[str, str]:
        """{asset: symbol} for every USDT pair trading now, less the research's exclusions."""
        info = self._get("exchangeInfo")
        symbols = info.get("symbols") if isinstance(info, dict) else None
        if not isinstance(symbols, list):
            raise DataError("Binance exchangeInfo had no symbol list")
        out = {}
        for entry in symbols:
            if not isinstance(entry, dict) or entry.get("quoteAsset") != QUOTE or entry.get("status") != "TRADING":
                continue
            asset, symbol = str(entry.get("baseAsset", "")), str(entry.get("symbol", ""))
            if asset and symbol == f"{asset}{QUOTE}" and asset.isalnum() and asset not in EXCLUDED_NAMES:
                out[asset] = symbol
        return out


def parse_klines(rows: list, now: float) -> list[tuple]:
    """(ts, open, high, low, close, volume, quote_volume) for each closed daily bar, oldest first.

    The same checks as research/tools/fetch_binance.py's parse_rows: a bar on the day grid, finite numbers, prices
    above 0, volumes not negative, and OHLC order on rows before the holdout start, as there. A row still forming,
    whose close time hasn't passed, is dropped.
    """
    out, seen = [], set()
    now_ms = now * 1000
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        try:
            open_ms, close_ms = int(row[0]), int(row[6])
            values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7)]
        except (TypeError, ValueError):
            continue
        if open_ms % DAY_MS or close_ms >= now_ms or close_ms > open_ms + DAY_MS:
            continue
        bar_open, high, low, bar_close, volume, quote_volume = values
        ts = open_ms // 1000
        if ts in seen or not all(map(math.isfinite, values)) or min(bar_open, high, low, bar_close) <= 0:
            continue
        if min(volume, quote_volume) < 0:
            continue
        if ts < HOLDOUT_START_TS and not (low <= min(bar_open, bar_close) and max(bar_open, bar_close) <= high):
            continue
        seen.add(ts)
        out.append((ts, bar_open, high, low, bar_close, volume, quote_volume))
    out.sort()
    return out


def known_cuts() -> dict[str, list[int]]:
    """The research's breaks over its whole development panel: asset -> first bar of each later part."""
    return {a: [int(t) for t in v] for a, v in json.loads(BREAKS_PATH.read_text())["cuts"].items()}


def name_like_research(panel: Panel, cuts: dict[str, list[int]]) -> Panel:
    """Renames each column the way the research's full-history panel names it.

    clean.split_breaks numbers the parts of an asset X, X~2, X~3, from the start of the data it's given. A 600-day window
    can't see a break older than itself, so on its own it would call LUNA (Terra 2.0, after the May 2022 break) plain
    LUNA, which the research calls LUNA~2 and the cost model sends to a different Kraken book. A part's research name
    counts every break up to its first bar: the research's known ones and any inside the window.
    """
    window_cuts: dict[str, set[int]] = {}
    for split in panel.meta.get("splits", []):
        window_cuts.setdefault(split["asset"], set()).add(int(split["first_bar_ts"]))
    names = []
    for column, name in enumerate(panel.assets):
        base = name.split("~", 1)[0]
        bars = np.flatnonzero(~np.isnan(panel.close[:, column]))
        if base not in cuts and base not in window_cuts or bars.size == 0:
            names.append(name)
            continue
        first = int(panel.ts[bars[0]])
        before = {t for t in set(cuts.get(base, ())) | window_cuts.get(base, set()) if t <= first}
        names.append(base if not before else f"{base}~{len(before) + 1}")
    if names == list(panel.assets):
        return panel
    if len(set(names)) != len(names):
        raise DataError(f"two live columns would take one research name: {sorted(names)}")
    return clean._rebuilt(panel, names, clean._fields(panel), dict(panel.meta))


def build_panel(bars: dict[str, list[tuple]], last_ts: int, days: int, cuts: dict | None = None,
                source: str = SOURCE) -> Panel:
    """The research's spot_1d structure over `days` rows ending at last_ts, cleaned by clean.py's own functions, with
    columns named as the research names them (name_like_research). meta["source"] says where the bars came from."""
    first_ts = last_ts - (days - 1) * DAY
    ts = np.arange(first_ts, last_ts + DAY, DAY, dtype=np.int64)
    blocks = {}
    for asset, rows in bars.items():
        if not rows:
            continue
        block = np.asarray(rows, dtype=np.float64)
        inside = (block[:, 0] >= first_ts) & (block[:, 0] <= last_ts)
        if inside.any():
            blocks[asset] = block[inside]
    assets = sorted(blocks)
    grid = {key: np.full((ts.size, len(assets)), np.nan) for key in CORE_FIELDS}
    for column, asset in enumerate(assets):
        block = blocks[asset]
        index = ((block[:, 0].astype(np.int64) - first_ts) // DAY)
        for k, key in enumerate(CORE_FIELDS, start=1):
            grid[key][index, column] = block[:, k]
    panel = Panel("spot_1d", ts, tuple(assets), *(grid[key] for key in CORE_FIELDS),
                  meta={"source": source, "quote": QUOTE})
    panel = clean.split_breaks(clean.fix_bcc_fork(panel))
    return name_like_research(panel, known_cuts() if cuts is None else cuts)


def panel_sha256(panel: Panel) -> str:
    """A hash of every bar the decision used: rows, names and the six fields, in order."""
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(panel.ts, dtype=np.int64).tobytes())
    digest.update("\n".join(panel.assets).encode())
    for key in CORE_FIELDS:
        digest.update(np.ascontiguousarray(getattr(panel, key), dtype=np.float64).tobytes())
    return digest.hexdigest()


def fetch_bars(client: BinancePublic, symbols: dict[str, str], days: int, now: float,
               pause: float = 0.05, served: set | None = None) -> dict[str, list[tuple]]:
    """Closed daily bars for each asset. limit is days + 1 because the newest row returned is still forming. The host
    that answered each call goes into `served`, for the decision log."""
    out = {}
    for asset, symbol in sorted(symbols.items()):
        out[asset] = parse_klines(client.klines(symbol, days + 1), now)
        if served is not None and getattr(client, "served_by", None):
            served.add(client.served_by)
        if pause:
            time.sleep(pause)  # Gentle on the public API: one call per symbol, once a day.
    return out


def has_bar(client: BinancePublic, symbols: dict[str, str], bar_ts: int, now: float) -> bool:
    """Whether every one of these assets has the given closed daily bar. Cheap: 3 rows each."""
    for symbol in symbols.values():
        if not any(row[0] == bar_ts for row in parse_klines(client.klines(symbol, 3), now)):
            return False
    return True
