"""The systematic runner's only door into research/: strategy ids, targets, the DCA schedule, the cost model and hashes.

Everything here calls the research's own functions on a panel built the research's way (duo/sys_data.py), so the
runner's targets are the research's targets. Nothing here writes under research/: no registry runs, no load_panel
(which appends to research/artifacts/panel_reads.jsonl), and no bytecode files, which is why dont_write_bytecode is set
before the first research import.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True  # Importing research must not leave .pyc files under research/.

import numpy as np  # noqa: E402

from research.harness import data as research_data  # noqa: E402
from research.harness.costs import CostModel  # noqa: E402
from research.strategies import common, f1_trend, f2_xsmom  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
COST_MODEL_PATH = PROJECT / "research" / "artifacts" / "cost_model.json"
FALLBACK_TREND = "F1__ENS__BTC_ETH__vol40"
DCA_IDS = {"dca_BTC": ("BTC",), "dca_BTC_ETH": ("BTC", "ETH")}
DCA_WEEKS = 52
# research/VENUES.md: "XMR is in cost_model.json, but UK clients can't trade it on Kraken."
UK_UNTRADABLE = {"XMR": "UK clients can't trade XMR on Kraken (research/VENUES.md)"}
RUNBOOK = "README.md, section \"Going live with the systematic runner\""


class StrategyRefused(Exception):
    """The configured strategy can't be run by this runner. The message says why."""


@dataclass(frozen=True)
class Strategy:
    id: str
    family: str                         # F1, F2 or DCA
    params: dict = field(default_factory=dict)
    fixed_assets: tuple[str, ...] = ()  # BTC, BTC+ETH, or DCA's. Empty for a TOP universe.
    track: str = "K"
    notes: tuple[str, ...] = ()

    @property
    def needs_universe(self) -> bool:
        return not self.fixed_assets


def _refuse(strategy_id: str, family: str) -> None:
    reasons = {
        "F3": "F3 is funding carry: long spot and short perpetual futures. UK retail clients can't trade crypto "
              "derivatives under the FCA ban (preregistration section 9), so this runner won't run it.",
        "F4": "F4 is intraday mean reversion on 15-minute to 4-hour bars with hard stops inside the bar. This runner "
              "makes one decision a day on daily bars, so it can't reproduce F4.",
        "F5": "F5 mixes its members' after-cost daily returns, and its weights follow each member's own simulated "
              "equity, so it has no daily target weights this runner could trade without changing it. Run a member "
              "on its own instead.",
    }
    if family in reasons:
        raise StrategyRefused(f"{strategy_id}: {reasons[family]}")
    raise StrategyRefused(f"{strategy_id}: not a strategy this runner knows. Use an F1 or F2 variant id from "
                          f"research/artifacts/variants.jsonl, or one of {', '.join(sorted(DCA_IDS))}.")


def _overrides(parts: list[str], numeric: dict) -> dict:
    """A neighbour's "__L=72" parts, typed as the family's NUMERIC says."""
    out = {}
    for part in parts:
        name, sep, value = part.partition("=")
        if not sep or name not in numeric:
            raise StrategyRefused(f"{part!r} isn't a parameter change this runner understands")
        try:
            out[name] = int(value) if numeric[name]["integer"] else float(value)
        except ValueError:
            raise StrategyRefused(f"{part!r} has no usable value") from None
    return out


def resolve(strategy_id: str) -> Strategy:
    """The strategy behind a research id, or StrategyRefused saying why not."""
    sid = strategy_id.strip()
    track = "K"
    if sid.endswith("__R"):
        sid, track = sid[: -len("__R")], "R"
    if sid.startswith("BASE__"):  # How select.py records FALLBACK_DCA: BASE__dca_BTC_ETH__2018-07-01.
        sid = sid.split("__")[1]
    if sid in DCA_IDS:
        return Strategy(strategy_id, "DCA", {"weeks": DCA_WEEKS, "assets": list(DCA_IDS[sid])}, DCA_IDS[sid], track,
                        _track_note(track))
    family = sid.split("__", 1)[0]
    module = {"F1": f1_trend, "F2": f2_xsmom}.get(family)
    if module is None:
        _refuse(strategy_id, family)
    grid = module.base_variants()
    for base in grid:
        base_id = common.variant_id(family, module.parts(base), "K")
        if sid == base_id or sid.startswith(base_id + "__"):
            extra = sid[len(base_id):].split("__")[1:]
            try:
                params = {**base, **_overrides(extra, module.NUMERIC)}
            except StrategyRefused as error:
                raise StrategyRefused(f"{strategy_id}: {error}") from None
            fixed = ()
            if family == "F1" and params["universe"] == "BTC":
                fixed = ("BTC",)
            elif family == "F1" and params["universe"] == "BTC_ETH":
                fixed = ("BTC", "ETH")
            return Strategy(strategy_id, family, params, fixed, track, _track_note(track))
    _refuse(strategy_id, family if family in ("F3", "F4", "F5") else "?")


def _track_note(track: str) -> tuple[str, ...]:
    if track == "R":
        return ("A track R id: the targets are the same strategy's, but the research priced it at Revolut X. This "
                "runner paper-trades Kraken, and compare uses track K's costs.",)
    return ()


def targets(strategy: Strategy, panel) -> tuple[np.ndarray, np.ndarray]:
    """(target, band) for every row of the panel, from the research module's own targets()."""
    if strategy.family == "F1":
        return f1_trend.targets(strategy.params, panel)
    if strategy.family == "F2":
        return f2_xsmom.targets(strategy.params, panel)
    raise ValueError(f"{strategy.family} has no target weights")


def signals(strategy: Strategy, panel, columns: list[int]) -> dict:
    """What the strategy saw at the last row, per research column, for the decision log."""
    out = {}
    last = panel.ts.size - 1
    if strategy.family == "F1":
        p = strategy.params
        close = panel.close[:, columns]
        sig = f1_trend.signal_values(p, close, panel.high[:, columns], panel.low[:, columns])
        vol = common.annual_vol(close, p["W"]) if "target" in p else None
        for i, c in enumerate(columns):
            out[panel.assets[c]] = {
                "signal": _num(sig[last, i]),
                "vol": _num(vol[last, i]) if vol is not None else None,
                "vol_target": p.get("target"),
            }
    elif strategy.family == "F2":
        p = strategy.params
        close = panel.close
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = close[last] / close[last - p["L"]] - 1 if last >= p["L"] else np.full(close.shape[1], np.nan)
        rebalance = bool(f2_xsmom.rebalance_rows(panel.ts, p["interval"])[last])
        for c in columns:
            out[panel.assets[c]] = {"return_L": _num(ret[c]), "rebalance_row": rebalance}
        if p["filter"] == "filter":
            btc = close[:, common.column(panel, "BTC")]
            sma = common.rolling_mean(btc[:, None], p["sma"])[last, 0]
            out["_market_filter"] = {"btc_close": _num(btc[last]), "btc_sma": _num(sma),
                                     "on": bool(btc[last] > sma) if np.isfinite(sma) else False}
    return out


def universe_mask_last_row(strategy: Strategy, panel) -> np.ndarray | None:
    """The TOP-n mask at the decision row, or None for a fixed universe."""
    if strategy.family == "F1" and strategy.params["universe"] == "TOP":
        return research_data.top_n_mask(panel, strategy.params["n"])[-1]
    if strategy.family == "F2":
        return research_data.top_n_mask(panel, strategy.params["n"])[-1]
    return None


def largest_sleeve(strategy: Strategy, target_row: np.ndarray, mask_row: np.ndarray | None) -> float:
    """The strategy's own largest sleeve on the decision day, for the per-asset limit."""
    if strategy.family == "F1":
        if strategy.fixed_assets:
            return 1.0 / len(strategy.fixed_assets)
        count = int(mask_row.sum()) if mask_row is not None else 0
        return 1.0 / count if count else 0.0
    if strategy.family == "F2":
        equal = 1.0 / strategy.params["k"]
        return max(equal, float(np.max(target_row))) if target_row.size else equal
    if strategy.family == "DCA":
        return 1.0 / len(strategy.fixed_assets)
    return 0.0


def max_target(strategy: Strategy) -> float:
    """The most one asset's target can ever be, from the strategy's construction, for the start's check against
    max_asset_pct. F1 never passes its sleeve (vol_scale is at most 1). F2's equal weights are 1/k, but inverse-volatility
    weights can put up to all of it in one coin."""
    if strategy.family == "F1":
        return 1.0 / len(strategy.fixed_assets) if strategy.fixed_assets else 1.0 / strategy.params["n"]
    if strategy.family == "F2":
        return 1.0 if strategy.params["weighting"] == "invvol" else 1.0 / strategy.params["k"]
    if strategy.family == "DCA":
        return 1.0 / len(strategy.fixed_assets)
    return 1.0


def dca_due_count(strategy: Strategy, start_day_ts: int, day_ts: int) -> int:
    """How many tranches research's dca() has bought by this fill day: one on the first day and every 7 days after,
    52 in all. A tranche whose day the runner missed is still owed, and the next decision buys it late."""
    if day_ts < start_day_ts:
        return 0
    return min(strategy.params["weeks"], (day_ts - start_day_ts) // 86400 // 7 + 1)


def dca_purchase(strategy: Strategy, start_day_ts: int, day_ts: int) -> int:
    """How many tranches fall due on this fill day: research's dca() buys every 7 days from its first row, 52 times."""
    if day_ts < start_day_ts:
        return 0
    days = (day_ts - start_day_ts) // 86400
    if days % 7 or days // 7 >= strategy.params["weeks"]:
        return 0
    return 1


def code_sha256() -> str:
    """research.harness.registry._code_sha256(): the hash the research records with every run."""
    from research.harness import registry
    return registry._code_sha256()


def duo_code_sha256() -> str:
    digest = hashlib.sha256()
    # The runner's code, and the breaks table that names its columns.
    for path in sorted([*(PROJECT / "duo").glob("*.py"), PROJECT / "duo" / "research_breaks.json"]):
        digest.update(path.name.encode() + b"\n" + path.read_bytes())
    return digest.hexdigest()


def load_cost_model() -> CostModel:
    return CostModel.load(COST_MODEL_PATH)


def cost_table() -> dict:
    return json.loads(COST_MODEL_PATH.read_text())


@dataclass(frozen=True)
class Venue:
    """Where a research column trades on Kraken, or why it can't."""
    column: str
    book: str | None        # The cost model's name for the coin, which is the ledger's name for it.
    kraken_base: str | None
    quote: str | None       # The cost model's book currency.
    tradable: bool
    reason: str = ""


def venue(column: str, model: CostModel, table: dict) -> Venue:
    book = model.book_name(column)
    if book is None:
        return Venue(column, None, None, None, False, "the cost model says Kraken's book of that name is another coin")
    entry = table["assets"].get(book)
    if entry is None or entry.get("basis") != "measured" or book not in model._curves:
        return Venue(column, book, None, None, False, "no Kraken book in the research cost model")
    if book in UK_UNTRADABLE:
        return Venue(column, book, entry["kraken_base"], entry["book"], False, UK_UNTRADABLE[book])
    return Venue(column, book, entry["kraken_base"], entry["book"], True)


def book_currency(books: list[str]) -> str:
    """GBP only if every book the strategy can use is GBP. Otherwise USD, which Amendment 5 says the account holds."""
    return "GBP" if books and all(b == "GBP" for b in books) else "USD"


def _num(value) -> float | None:
    value = float(value)
    return value if np.isfinite(value) else None
